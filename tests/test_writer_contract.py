"""Tests for writer-level safety contract helpers (NUL, CSV formula injection and
workbook control characters)."""

from __future__ import annotations

import csv
import dataclasses
import io
from collections.abc import Iterable
from datetime import date
from decimal import Decimal

import pytest
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.output._writer_contract import (
    WriterContractError,
    _assert_no_csv_formula_injection,
    _assert_no_nul_in_row,
    write_checked_rows,
)
from ibkr_fa.output._xlsx_contract import _assert_no_illegal_xlsx_character

_FORMULA_TRIGGERS_ASCII = ("=", "+", "-", "@")
_FORMULA_TRIGGERS_C0 = ("\t", "\v", "\r", "\n")
_FORMULA_TRIGGERS_FULLWIDTH = ("\uff1d", "\uff0b", "\uff0d", "\uff20")

# Synthetic values a cell could hold (#1048): an entity name and an account
# number. No refusal may print them.
_HOLDER = "Synthetic Holder Two"
_ACCOUNT = "U1234567"


def _assert_omits(message: str, *values: str) -> None:
    """``message`` contains none of ``values``, compared case-insensitively."""
    for value in values:
        assert value.lower() not in message.lower()


class TestWriterContractError:
    def test_is_ibkr_fa_error_subclass(self) -> None:
        err = WriterContractError(
            violation="nul_byte",
            row_index=0,
            cell_index=0,
            trigger="",
        )
        assert isinstance(err, IbkrFaError)

    def test_is_frozen(self) -> None:
        err = WriterContractError(
            violation="nul_byte",
            row_index=0,
            cell_index=0,
            trigger="",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            err.row_index = 99  # ty: ignore[invalid-assignment]

    def test_has_slots(self) -> None:
        # Frozen-slots dataclass: __slots__ must exist so instances cannot
        # gain arbitrary attributes via __dict__. The dataclass(frozen=True)
        # already makes any setattr raise FrozenInstanceError before the
        # slot machinery kicks in, so we assert __slots__ presence directly
        # rather than trying to trigger AttributeError at runtime.
        assert hasattr(WriterContractError, "__slots__")
        slot_names = set(WriterContractError.__slots__)
        assert {"violation", "row_index", "cell_index", "trigger"} <= slot_names
        # The cell's value is not kept at all, so nothing can print it (#1048).
        assert "cell_value_excerpt" not in slot_names

    def test_str_for_nul_byte_branch(self) -> None:
        err = WriterContractError(
            violation="nul_byte",
            row_index=42,
            cell_index=3,
            trigger="",
        )
        assert str(err) == "WriterContractError: row 42 cell at index 3 contains a NUL byte"

    def test_str_for_formula_trigger_branch(self) -> None:
        err = WriterContractError(
            violation="formula_trigger",
            row_index=7,
            cell_index=2,
            trigger="=",
        )
        assert str(err) == (
            "WriterContractError: row 7 cell at index 2 starts with formula-trigger character '='"
        )

    def test_str_for_unrepresentable_number_branch(self) -> None:
        err = WriterContractError(
            violation="unrepresentable_number",
            row_index=3,
            cell_index=16,
            trigger="",
        )
        assert str(err) == (
            "WriterContractError: row 3 cell at index 16 holds a number the workbook cannot"
            " store: it would read back as infinite, empty or zero"
        )

    def test_str_for_text_too_long_branch(self) -> None:
        err = WriterContractError(
            violation="text_too_long",
            row_index=1,
            cell_index=0,
            trigger="",
        )
        assert str(err) == (
            "WriterContractError: row 1 cell at index 0 is longer than a workbook cell can"
            " hold (32,767 characters)"
        )

    def test_str_for_illegal_character_branch(self) -> None:
        err = WriterContractError(
            violation="illegal_character",
            row_index=4,
            cell_index=11,
            trigger="",
        )
        assert str(err) == (
            "WriterContractError: row 4 cell at index 11 holds a control character a workbook"
            " cell can't store"
        )

    def test_str_defensive_branch_for_unknown_violation(self) -> None:
        err = WriterContractError(
            violation="bogus_violation",  # ty: ignore[invalid-argument-type]
            row_index=1,
            cell_index=4,
            trigger="x",
        )
        assert str(err) == "WriterContractError: unknown violation kind at row 1 cell 4"

    def test_str_defensive_branch_prints_neither_the_kind_nor_the_trigger(self) -> None:
        """#1048: an unknown kind, built past the type system, prints only its
        position: not the kind, the trigger or anything else it was given."""
        err = WriterContractError(
            violation=_HOLDER,  # ty: ignore[invalid-argument-type]
            row_index=2,
            cell_index=5,
            trigger=_ACCOUNT,
        )
        message = str(err)
        assert message == "WriterContractError: unknown violation kind at row 2 cell 5"
        # Base echoed the kind but already omitted the trigger; its absence guards that backstop.
        _assert_omits(message, _HOLDER, _ACCOUNT)


class TestAssertNoNulInRow:
    def test_passes_on_empty_row(self) -> None:
        _assert_no_nul_in_row([], row_index=0)

    def test_passes_on_clean_row(self) -> None:
        _assert_no_nul_in_row(["a", "b", "c", ""], row_index=0)

    def test_raises_on_nul_in_first_cell(self) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_nul_in_row(["a\x00b", "ok"], row_index=5)
        err = exc_info.value
        assert err.violation == "nul_byte"
        assert err.row_index == 5
        assert err.cell_index == 0
        assert err.trigger == ""
        assert not hasattr(err, "cell_value_excerpt")
        assert str(err) == "WriterContractError: row 5 cell at index 0 contains a NUL byte"

    def test_raises_on_nul_in_middle_cell(self) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_nul_in_row(["ok", "still ok", "bad\x00cell"], row_index=2)
        err = exc_info.value
        assert err.violation == "nul_byte"
        assert err.cell_index == 2
        assert err.row_index == 2

    def test_raises_on_nul_in_last_cell(self) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_nul_in_row(["a", "b", "\x00"], row_index=0)
        assert exc_info.value.cell_index == 2

    def test_stops_at_first_nul_cell(self) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_nul_in_row(["\x00first", "\x00second"], row_index=0)
        assert exc_info.value.cell_index == 0

    def test_propagates_row_index(self) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_nul_in_row(["\x00"], row_index=1234)
        assert exc_info.value.row_index == 1234
        assert "row 1234" in str(exc_info.value)

    def test_message_omits_the_cell(self) -> None:
        """#1048: the message names the row and the cell index, never the cell,
        however long it is."""
        long_cell = f"{_HOLDER} " * 20 + "\x00"
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_nul_in_row(["ok", long_cell], row_index=0)
        message = str(exc_info.value)
        assert message == "WriterContractError: row 0 cell at index 1 contains a NUL byte"
        # Base echoed the holder; its 80-character excerpt already omitted the trailing NUL.
        _assert_omits(message, _HOLDER, "\\x00")
        assert not hasattr(exc_info.value, "cell_value_excerpt")


class TestAssertNoCsvFormulaInjection:
    def test_passes_on_empty_row(self) -> None:
        _assert_no_csv_formula_injection([], row_index=0)

    def test_passes_on_clean_row(self) -> None:
        _assert_no_csv_formula_injection(
            ["XQAA", "US99990A3095", "2025-05-15", "100.00"],
            row_index=0,
        )

    def test_passes_on_empty_string_cell(self) -> None:
        _assert_no_csv_formula_injection(["", "", "", ""], row_index=0)

    def test_passes_on_whitespace_only_ascii_space_cell(self) -> None:
        _assert_no_csv_formula_injection([" ", "   ", "ok"], row_index=0)

    def test_passes_on_text_starting_with_safe_char(self) -> None:
        _assert_no_csv_formula_injection(["A", "Z", "0", "9", "_", "/", "."], row_index=0)

    @pytest.mark.parametrize("trigger", _FORMULA_TRIGGERS_ASCII)
    def test_raises_on_ascii_trigger_as_first_char(self, trigger: str) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_csv_formula_injection(["ok", f"{trigger}payload"], row_index=3)
        err = exc_info.value
        assert err.violation == "formula_trigger"
        assert err.row_index == 3
        assert err.cell_index == 1
        assert err.trigger == trigger

    @pytest.mark.parametrize("trigger", _FORMULA_TRIGGERS_C0)
    def test_raises_on_c0_whitespace_trigger_as_first_char(self, trigger: str) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_csv_formula_injection([f"{trigger}injected"], row_index=0)
        err = exc_info.value
        assert err.violation == "formula_trigger"
        assert err.cell_index == 0
        assert err.trigger == trigger

    @pytest.mark.parametrize("trigger", _FORMULA_TRIGGERS_FULLWIDTH)
    def test_raises_on_fullwidth_trigger_as_first_char(self, trigger: str) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_csv_formula_injection([f"{trigger}attack"], row_index=0)
        err = exc_info.value
        assert err.violation == "formula_trigger"
        assert err.cell_index == 0
        assert err.trigger == trigger

    @pytest.mark.parametrize("trigger", _FORMULA_TRIGGERS_ASCII)
    def test_raises_when_trigger_follows_leading_whitespace(self, trigger: str) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_csv_formula_injection([f"   {trigger}payload"], row_index=0)
        err = exc_info.value
        assert err.violation == "formula_trigger"
        assert err.trigger == trigger

    @pytest.mark.parametrize("trigger", _FORMULA_TRIGGERS_ASCII)
    def test_raises_when_trigger_follows_leading_tab(self, trigger: str) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_csv_formula_injection([f"\t{trigger}payload"], row_index=0)
        err = exc_info.value
        assert err.violation == "formula_trigger"
        assert err.trigger == "\t"

    def test_raises_on_standalone_leading_tab_cell(self) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_csv_formula_injection(["\t"], row_index=0)
        err = exc_info.value
        assert err.violation == "formula_trigger"
        assert err.trigger == "\t"

    def test_raises_on_standalone_leading_cr_cell(self) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_csv_formula_injection(["\r"], row_index=0)
        err = exc_info.value
        assert err.violation == "formula_trigger"
        assert err.trigger == "\r"

    def test_raises_on_standalone_leading_lf_cell(self) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_csv_formula_injection(["\n"], row_index=0)
        err = exc_info.value
        assert err.violation == "formula_trigger"
        assert err.trigger == "\n"

    def test_raises_on_standalone_leading_vt_cell(self) -> None:
        # Vertical-tab (U+000B) is in the C0 whitespace trigger set per
        # the writer-contract spec; verify standalone-cell handling here
        # since the parametrised C0 test exercises the {trigger}payload
        # shape, not the lstrip-empty short-circuit case.
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_csv_formula_injection(["\v"], row_index=0)
        err = exc_info.value
        assert err.violation == "formula_trigger"
        assert err.trigger == "\v"

    @pytest.mark.parametrize("trigger", _FORMULA_TRIGGERS_ASCII)
    def test_raises_when_trigger_follows_leading_vt(self, trigger: str) -> None:
        # A leading vertical-tab followed by an ASCII trigger must raise
        # because the raw-cell[0] path catches the \v itself before any
        # lstrip-based check could see the downstream =/+/-/@.
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_csv_formula_injection([f"\v{trigger}payload"], row_index=0)
        err = exc_info.value
        assert err.violation == "formula_trigger"
        assert err.trigger == "\v"

    def test_message_names_the_trigger_but_not_the_cell(self) -> None:
        """#1048: the trigger character is the rule broken, so it is named; the
        rest of the cell is not."""
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_csv_formula_injection(["=cmd|'/c calc'!A0"], row_index=0)
        message = str(exc_info.value)
        assert message == (
            "WriterContractError: row 0 cell at index 0 starts with formula-trigger character '='"
        )
        _assert_omits(message, "cmd", "calc")
        assert not hasattr(exc_info.value, "cell_value_excerpt")

    def test_message_omits_a_long_cell(self) -> None:
        long_payload = "=" + (_HOLDER * 20)
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_csv_formula_injection([long_payload], row_index=0)
        message = str(exc_info.value)
        assert message == (
            "WriterContractError: row 0 cell at index 0 starts with formula-trigger character '='"
        )
        _assert_omits(message, _HOLDER)

    def test_propagates_row_index_to_exception_and_str(self) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_csv_formula_injection(["=BAD"], row_index=999)
        assert exc_info.value.row_index == 999
        assert "row 999" in str(exc_info.value)

    def test_stops_at_first_offending_cell(self) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_csv_formula_injection(["=first", "+second"], row_index=0)
        assert exc_info.value.cell_index == 0
        assert exc_info.value.trigger == "="

    def test_adversarial_excel_payload_is_rejected(self) -> None:
        adversarial = "=cmd|'/c calc'!A0"
        with pytest.raises(WriterContractError) as exc_info:
            _assert_no_csv_formula_injection(
                ["XQAA", "US99990A3095", "2025-05-15", adversarial],
                row_index=1,
            )
        err = exc_info.value
        assert err.violation == "formula_trigger"
        assert err.cell_index == 3
        assert err.trigger == "="

    def test_padded_cells_after_offending_cell_do_not_mask(self) -> None:
        with pytest.raises(WriterContractError):
            _assert_no_csv_formula_injection(
                ["safe", " =maybe_safe_with_lead_space", "safe2"],
                row_index=0,
            )


# The characters openpyxl 3.1.5 can't store in a cell (#1357).
_ILLEGAL_XLSX_CHARACTERS = frozenset(
    chr(code) for code in (*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20))
)


class TestAssertNoIllegalXlsxCharacter:
    def test_openpyxl_refuses_exactly_the_pinned_set(self) -> None:
        """An openpyxl upgrade that changes its set fails here."""
        every_code_point = "".join(map(chr, range(0x110000)))
        assert len(_ILLEGAL_XLSX_CHARACTERS) == 29
        assert set(ILLEGAL_CHARACTERS_RE.findall(every_code_point)) == _ILLEGAL_XLSX_CHARACTERS

    @pytest.mark.parametrize("character", sorted(_ILLEGAL_XLSX_CHARACTERS), ids=ascii)
    def test_raises_on_each_pinned_character(self, character: str) -> None:
        with pytest.raises(WriterContractError) as raised:
            _assert_no_illegal_xlsx_character(["a", f"b{character}c"], row_index=3)
        assert raised.value == WriterContractError(
            violation="illegal_character", row_index=3, cell_index=1, trigger=""
        )

    def test_passes_on_every_other_code_point(self) -> None:
        """Tab, line feed, carriage return, DEL and every other character pass."""
        others = "".join(
            chr(code) for code in range(0x110000) if chr(code) not in _ILLEGAL_XLSX_CHARACTERS
        )
        _assert_no_illegal_xlsx_character([others], row_index=0)

    def test_skips_cells_that_are_not_text(self) -> None:
        _assert_no_illegal_xlsx_character(
            [None, 1, 1.5, Decimal("2"), date(2025, 1, 1)], row_index=0
        )

    def test_names_the_first_such_cell_by_its_index_in_the_whole_row(self) -> None:
        cells = [Decimal("1"), date(2025, 1, 1), "clean", "x\x1fy", "x\x01y"]
        with pytest.raises(WriterContractError) as raised:
            _assert_no_illegal_xlsx_character(cells, row_index=7)
        assert (raised.value.row_index, raised.value.cell_index) == (7, 3)

    def test_checks_past_the_first_32767_characters(self) -> None:
        """openpyxl checks only the part of a cell it keeps; this checks all of it."""
        with pytest.raises(WriterContractError):
            _assert_no_illegal_xlsx_character(["x" * 32_767 + "\x01"], row_index=0)

    def test_message_omits_the_cell(self) -> None:
        with pytest.raises(WriterContractError) as raised:
            _assert_no_illegal_xlsx_character([f"{_HOLDER}\x01{_ACCOUNT}"], row_index=1)
        _assert_omits(str(raised.value), _HOLDER, _ACCOUNT)
        _assert_omits(repr(raised.value), _HOLDER, _ACCOUNT)


class _RecordingWriter:
    def __init__(self) -> None:
        self.rows: list[list[str]] = []

    def writerow(self, row: Iterable[str]) -> None:
        self.rows.append(list(row))


class TestWriteCheckedRows:
    def test_renders_cells_like_csv_writer(self) -> None:
        row = ["text", Decimal("1.50"), 3, None, ""]
        expected = io.StringIO()
        csv.writer(expected).writerow(row)
        actual = io.StringIO()

        write_checked_rows(csv.writer(actual), [row], start_index=0)

        assert actual.getvalue() == expected.getvalue()

    def test_writes_each_row_with_one_writerow_call(self) -> None:
        writer = _RecordingWriter()

        write_checked_rows(writer, [["a", 1], ("b", None)], start_index=0)

        assert writer.rows == [["a", "1"], ["b", ""]]

    def test_checks_rendered_cells(self) -> None:
        with pytest.raises(WriterContractError) as exc_info:
            write_checked_rows(_RecordingWriter(), [["ok", Decimal("-1")]], start_index=0)
        assert exc_info.value.violation == "formula_trigger"
        assert exc_info.value.cell_index == 1

    @pytest.mark.parametrize(
        ("bad_cell", "violation"), [("=x", "formula_trigger"), ("a\x00b", "nul_byte")]
    )
    def test_stops_before_the_offending_row(self, bad_cell: str, violation: str) -> None:
        writer = _RecordingWriter()

        with pytest.raises(WriterContractError) as exc_info:
            write_checked_rows(writer, [["ok"], ["ok", bad_cell], ["never"]], start_index=5)

        assert exc_info.value.violation == violation
        assert exc_info.value.row_index == 6
        assert exc_info.value.cell_index == 1
        assert writer.rows == [["ok"]]

    @pytest.mark.parametrize(
        ("bad_cell", "marker", "expected"),
        [
            (
                f"={_HOLDER}",
                _HOLDER,
                "WriterContractError: row 6 cell at index 2 starts with formula-trigger"
                " character '='",
            ),
            (
                f" +{_ACCOUNT}",
                _ACCOUNT,
                "WriterContractError: row 6 cell at index 2 starts with formula-trigger"
                " character '+'",
            ),
            (
                f"{_HOLDER}\x00",
                _HOLDER,
                "WriterContractError: row 6 cell at index 2 contains a NUL byte",
            ),
        ],
        ids=["formula-trigger", "formula-trigger-after-lstrip", "nul-byte"],
    )
    def test_refusal_names_the_row_and_cell_index_not_the_cell(
        self, bad_cell: str, marker: str, expected: str
    ) -> None:
        """#1048: a cell can hold a name or an account number, so the refusal
        prints its position and the rule it broke, never its value."""
        writer = _RecordingWriter()

        with pytest.raises(WriterContractError) as exc_info:
            write_checked_rows(writer, [["ok"], ["ok", "fine", bad_cell]], start_index=5)

        message = str(exc_info.value)
        assert message == expected
        _assert_omits(message, marker)
        assert not hasattr(exc_info.value, "cell_value_excerpt")
        assert writer.rows == [["ok"]]

    def test_formula_columns_limits_only_the_formula_check(self) -> None:
        writer = _RecordingWriter()

        write_checked_rows(writer, [["a", "b", "-1"]], start_index=0, formula_columns=2)

        assert writer.rows == [["a", "b", "-1"]]
        with pytest.raises(WriterContractError) as exc_info:
            write_checked_rows(writer, [["a", "b", "\x00"]], start_index=0, formula_columns=2)
        assert exc_info.value.violation == "nul_byte"
        assert exc_info.value.cell_index == 2
