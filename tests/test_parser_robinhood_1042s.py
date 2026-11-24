"""Synthetic contract checks for the Robinhood Form 1042-S parser."""

from __future__ import annotations

import subprocess
from collections import defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.models.form_1042s import Form1042SAmountKind, Form1042SCopyType
from ibkr_fa.parser.robinhood_1042s import (
    Robinhood1042SPacket,
    Robinhood1042SPageKind,
    parse_robinhood_1042s_packet,
    read_robinhood_1042s_packet,
)
from tests._private_checks import private_test
from tests._robinhood_1042s import SPACED_DOB as _SPACED_DOB
from tests._robinhood_1042s import form_line as _line
from tests._robinhood_1042s import replace_on_both_copies as _both_copies
from tests._robinhood_1042s import robinhood_1042s_text as _packet
from tests._robinhood_private import private_pdf_page_count, private_robinhood_pdfs


def _swap_last_two_pages(text: str) -> str:
    pages = text.removesuffix("\f").split("\f")
    pages[-1], pages[-2] = pages[-2], pages[-1]
    return "\f".join(pages) + "\f"


def _duplicate_last_instruction_page(text: str) -> str:
    pages = text.removesuffix("\f").split("\f")
    pages[-2] = pages[-1]
    return "\f".join(pages) + "\f"


def _append_summary_line(text: str) -> str:
    pages = text.removesuffix("\f").split("\f")
    pages[-4] += "\n1.00 2.00"
    return "\f".join(pages) + "\f"


def test_parse_one_economic_form_and_preserve_copy_occurrences() -> None:
    packet = parse_robinhood_1042s_packet(_packet())

    assert [page.kind for page in packet.pages] == [
        Robinhood1042SPageKind.FORM_B,
        Robinhood1042SPageKind.FORM_VERSO,
        Robinhood1042SPageKind.FORM_C,
        Robinhood1042SPageKind.FORM_VERSO,
        Robinhood1042SPageKind.BROKER_SUMMARY,
        Robinhood1042SPageKind.IRS_RECIPIENT_INSTRUCTIONS,
        Robinhood1042SPageKind.IRS_CODES_CH3,
        Robinhood1042SPageKind.IRS_CODES_CH4,
    ]
    assert [copy.copy_type for copy in packet.copies] == [
        Form1042SCopyType.B,
        Form1042SCopyType.C,
    ]
    assert packet.copies[0].economic_fingerprint() == packet.copies[1].economic_fingerprint()
    assert packet.copies[0].withholding_allowance.kind is Form1042SAmountKind.BLANK
    assert packet.copies[0].tax_withheld_other_agents.kind is (Form1042SAmountKind.DASH_ZERO)
    assert packet.copies[0].overwithheld_tax_repaid.kind is Form1042SAmountKind.AMOUNT
    assert packet.copies[0].overwithheld_tax_repaid.value == Decimal("0.00")
    assert packet.copies[0].recipient.account_reference == "ACCOUNT-TEST"
    assert packet.copies[0].agent.phone == "Phone: (000) 000-0000"
    assert packet.summary.account_reference == "ACCOUNT-TEST"
    assert packet.summary.rows[0].gross_income.value == Decimal("100.00")


def test_variable_economic_form_count_is_not_hardcoded() -> None:
    packet = parse_robinhood_1042s_packet(_packet(ufis=("1234567890", "0987654321")))
    assert len(packet.copies) == 4
    assert len(packet.summary.rows) == 2


def test_column_isolation_keeps_blank_left_boxes_blank() -> None:
    copy = parse_robinhood_1042s_packet(_packet()).copies[0]
    assert copy.withholding_allowance.kind is Form1042SAmountKind.BLANK
    assert copy.net_income.kind is Form1042SAmountKind.BLANK
    assert copy.recipient.us_tin == "XXX-XX-9999"
    assert copy.recipient.account_reference == "ACCOUNT-TEST"


def test_packet_code_tables_validate_both_columns_and_shared_statuses() -> None:
    packet = parse_robinhood_1042s_packet(
        _packet(
            chapter3_exemption="12",
            chapter4_exemption="15",
            agent_chapter3_status="01",
        )
    )
    assert packet.copies[0].chapter3_exemption_code == "12"
    assert packet.copies[0].chapter4_exemption_code == "15"
    assert packet.copies[0].agent.chapter3_status_code == "01"


@pytest.mark.parametrize(
    "text",
    [
        _packet(chapter4_exemption="12"),
        _packet(chapter3_exemption="15"),
        _packet(agent_chapter3_status="16"),
    ],
    ids=[
        "chapter3-exemption-in-chapter4-column",
        "chapter4-exemption-in-chapter3-column",
        "chapter4-status-as-chapter3-status",
    ],
)
def test_a_code_listed_only_in_another_columns_table_refuses(text: str) -> None:
    """The packet's instructions list 12 only as a Chapter 3 exemption, 15 as a
    Chapter 4 exemption and a Chapter 3 status, and 16 only as a Chapter 4
    status, so each code is missing from its own column's table."""
    with pytest.raises(
        ParseError, match="Form 1042-S code is absent from the packet's code tables"
    ):
        parse_robinhood_1042s_packet(text)


def test_conditional_box_arithmetic() -> None:
    text = (
        _packet()
        .replace(
            "5 Withholding allowance",
            _line((0, "5 Withholding allowance"), (90, "10.00")),
        )
        .replace(
            "6 Net income",
            _line((0, "6 Net income"), (90, "90.00")),
        )
    )
    assert parse_robinhood_1042s_packet(text).copies[0].net_income.value == 90

    with pytest.raises(ParseError, match="Boxes 2/5"):
        parse_robinhood_1042s_packet(text.replace("90.00", "89.00"))
    with pytest.raises(ParseError, match="Boxes 7a/8/9"):
        parse_robinhood_1042s_packet(_packet().replace("25.00", "24.00", 1))


@pytest.mark.parametrize(
    "bad",
    [
        _packet().removesuffix("\f"),
        _packet().replace("This page intentionally left blank.", "Unexpected verso", 1),
        _packet().replace("COPY B", "COPY D", 1),
        _packet().replace("Form 1042-S (2025)", "Form 1042-S (2026)", 1),
        _packet(mismatch_copy=True),
        _packet(amended=True),
        _packet().replace("AMENDED  AMENDMENT NO.", "AMENDED ☑ AMENDMENT NO.", 1),
        _packet().replace(
            "because",
            "because ☑",
            1,
        ),
        _packet().replace(
            "15 Check if pro-rata basis reporting",
            "15 Check if pro-rata basis reporting ☑",
            1,
        ),
        _packet().replace(
            "10 Total withholding credit (combine boxes 7a, 8, and 9)",
            "10 Total withholding credit (combine boxes 7a, 8, and 9) 25.00",
            1,
        ),
        _packet().replace(
            "9 Overwithheld tax repaid to recipient pursuant to adjustment procedures "
            "(see instructions)",
            "9 Overwithheld tax repaid to recipient pursuant to adjustment procedures "
            "(see instructions) 1.00",
            1,
        ),
        _packet().replace(
            "13a Recipient's name",
            "EXTRA RECIPIENT LINE\n13a Recipient's name",
            1,
        ),
        _packet(ufis=("1234567890", "0987654321")).replace(
            "INCOME CODE 34",
            "INCOME CODE 34 EXTRA",
            1,
        ),
        _append_summary_line(_packet()),
        _packet().replace("1 2 3 4 5 6 7 8 9 0", "1 2 3", 1),
        _packet().replace("U.S. Income Tax Filing Requirements", "Unknown Instructions"),
        _packet().replace("06 Listed income code", ""),
        _packet(ufis=("1234567890", "1234567890")),
        _swap_last_two_pages(_packet()),
        _duplicate_last_instruction_page(_packet()),
        _packet().replace("100.00", "(100.00)", 1),
        _packet().replace(
            _line((101, "16a Payer's name"), (164, "16b Payer's TIN")),
            _line(
                (0, "POPULATED"),
                (101, "16a Payer's name"),
                (164, "16b Payer's TIN"),
            ),
            1,
        ),
        _packet().replace(
            _line(
                (101, "15f Country code"),
                (128, "15g Foreign tax identification number, if any"),
            ),
            _line(
                (0, "POPULATED"),
                (101, "15f Country code"),
                (128, "15g Foreign tax identification number, if any"),
            ),
            1,
        ),
        _packet().replace(
            "Foreign Person's U.S. Source Income Subject to Withholding",
            "Unknown Summary",
        ),
    ],
    ids=[
        "missing-trailing-delimiter",
        "bad-verso",
        "unknown-copy",
        "unsupported-year",
        "copy-disagreement",
        "amended",
        "alternate-amended-mark",
        "alternate-option-mark",
        "alternate-pro-rata-mark",
        "box10-value-on-label-row",
        "box9-value-on-label-row",
        "shifted-recipient-block",
        "malformed-summary-income-heading",
        "unclassified-summary-line",
        "malformed-ufi",
        "unknown-instruction-page",
        "unlisted-code",
        "duplicate-ufi",
        "reordered-instructions",
        "duplicated-instruction-page",
        "parenthesized-non-box9",
        "populated-blank-region",
        "populated-box15e",
        "unknown-summary",
    ],
)
def test_unknown_packet_or_form_shapes_refuse(bad: str) -> None:
    with pytest.raises(ParseError):
        parse_robinhood_1042s_packet(bad)


# ---- #1099: the UFI and the birth date are read as whole spaced-digit values

_SPACED_UFI = "1 2 3 4 5 6 7 8 9 0"
_UFI_ROW_HEADER = "Internal Revenue Service"
_UFI_ROW_SHIFTED = "Form 1042-S UFI row is missing or shifted"
_UFI_MALFORMED = "Form 1042-S UFI is malformed"
_DOB_MALFORMED = "Form 1042-S recipient date of birth is malformed"
_HOLDER_MARKER = "Synthetic Holder Two"
_DOB_MARKER = "Synthetic Marker Q"
_UNECHOED = (
    "1234567890",
    _SPACED_UFI,
    "19900101",
    _SPACED_DOB,
)


def _copy_c_only(old: str, new: str) -> str:
    """The test packet with ``old`` replaced on Copy C (page 3) only."""
    text = _packet()
    assert text.count(old) == 2
    at = text.rindex(old)
    return text[:at] + new + text[at + len(old) :]


def _refusal(text: str) -> ParseError:
    with pytest.raises(ParseError) as raised:
        parse_robinhood_1042s_packet(text)
    return raised.value


@pytest.mark.parametrize(
    ("bad", "page", "column", "message"),
    [
        pytest.param(
            _both_copies(_SPACED_UFI, "1X2X3X4X5X6X7X8X9X0"),
            1,
            None,
            _UFI_MALFORMED,
            id="ufi-letter-separators",
        ),
        pytest.param(
            _both_copies(_SPACED_UFI, "1,2,3,4,5,6,7,8,9,0"),
            1,
            None,
            _UFI_MALFORMED,
            id="ufi-comma-separated",
        ),
        pytest.param(
            _both_copies(_SPACED_UFI, "1234567890"),
            1,
            None,
            _UFI_MALFORMED,
            id="ufi-glued",
        ),
        pytest.param(
            _both_copies(_SPACED_UFI, f"{_SPACED_UFI} XY"),
            1,
            None,
            _UFI_MALFORMED,
            id="ufi-followed-by-letters",
        ),
        pytest.param(
            _both_copies(_SPACED_UFI, f"1 2 3 4 5 {_HOLDER_MARKER} 6 7 8 9 0"),
            1,
            None,
            _UFI_MALFORMED,
            id="ufi-privacy-marker",
        ),
        pytest.param(
            _copy_c_only(_SPACED_UFI, "1X2X3X4X5X6X7X8X9X0"),
            3,
            None,
            _UFI_MALFORMED,
            id="copy-c-ufi-letter-separators",
        ),
        pytest.param(
            _both_copies(_UFI_ROW_HEADER, "Synthetic Agency Name"),
            1,
            None,
            _UFI_ROW_SHIFTED,
            id="ufi-header-words-replaced",
        ),
        pytest.param(
            _both_copies(f"{_UFI_ROW_HEADER} ", ""),
            1,
            None,
            _UFI_ROW_SHIFTED,
            id="ufi-header-words-removed",
        ),
        pytest.param(
            _both_copies(_SPACED_DOB, "1X9X9X0X0X1X0X1"),
            1,
            "Box 13l",
            _DOB_MALFORMED,
            id="dob-letter-separators",
        ),
        pytest.param(
            _both_copies(_SPACED_DOB, "19900101"),
            1,
            "Box 13l",
            _DOB_MALFORMED,
            id="dob-glued",
        ),
        pytest.param(
            _both_copies(_SPACED_DOB, "1990-01-01"),
            1,
            "Box 13l",
            _DOB_MALFORMED,
            id="dob-iso-dashes",
        ),
        pytest.param(
            _both_copies(_SPACED_DOB, "1990/01/01"),
            1,
            "Box 13l",
            _DOB_MALFORMED,
            id="dob-slashes",
        ),
        pytest.param(
            _both_copies(_SPACED_DOB, f"{_SPACED_DOB} {_DOB_MARKER}"),
            1,
            "Box 13l",
            _DOB_MALFORMED,
            id="dob-privacy-marker",
        ),
        pytest.param(
            _copy_c_only(_SPACED_DOB, "1-9-9-0-0-1-0-1"),
            3,
            "Box 13l",
            _DOB_MALFORMED,
            id="copy-c-dob-dash-separators",
        ),
    ],
)
def test_ufi_and_birth_date_must_be_whole_spaced_digits(
    bad: str, page: int, column: str | None, message: str
) -> None:
    """Each case changes one thing: header-word cases keep well-formed digits,
    and digit cases keep the header words. The privacy markers hold no digit,
    so dropping them still leaves the digits a well-formed count."""
    error = _refusal(bad)

    assert (error.message, error.row_index, error.column, error.raw_value) == (
        message,
        page,
        column,
        "",
    )
    rendered = str(error)
    assert not [value for value in _UNECHOED if value in rendered]
    for marker in (_HOLDER_MARKER, _DOB_MARKER):
        if marker in bad:
            assert marker not in rendered


def test_spaced_birth_date_naming_no_real_date_refuses() -> None:
    error = _refusal(_both_copies(_SPACED_DOB, "2 0 2 5 0 2 3 0"))

    assert (error.message, error.row_index, error.column, error.raw_value) == (
        "Form 1042-S recipient date of birth is invalid",
        1,
        "Box 13l",
        "",
    )
    rendered = str(error)
    assert "20250230" not in rendered
    assert "2 0 2 5 0 2 3 0" not in rendered


def test_wider_digit_spacing_parses_like_single_spacing() -> None:
    packet = parse_robinhood_1042s_packet(_packet())
    wide = _both_copies(_SPACED_UFI, "1  2  3  4  5  6  7  8  9  0").replace(
        _SPACED_DOB, "1  9  9  0  0  1  0  1"
    )

    assert parse_robinhood_1042s_packet(wide) == packet
    assert [copy.unique_form_identifier for copy in packet.copies] == ["1234567890"] * 2
    assert [copy.recipient.date_of_birth for copy in packet.copies] == [date(1990, 1, 1)] * 2


def test_models_and_reader_errors_are_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = parse_robinhood_1042s_packet(_packet())
    assert "ACCOUNT-TEST" not in repr(packet)
    assert "TAX PAYER" not in repr(packet)

    private_path = tmp_path / "SECRET-TAX-PACKET.pdf"

    def fail(*args: object, **kwargs: object) -> object:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=["pdftotext", str(private_path)],
            stderr=f"failed to open {private_path}",
        )

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ParseError) as caught:
        read_robinhood_1042s_packet(private_path)
    rendered = str(caught.value)
    assert str(private_path) not in rendered
    assert "failed to open" not in rendered


def _packet_structure_holds(packet: Robinhood1042SPacket, page_count: int) -> bool:
    """One page record per physical page, in order; each copy sits on the
    form page its record names, with the same identifier and the packet's
    tax year; each identifier is on exactly one Copy B and one Copy C; and
    the summary sits on the summary page."""
    pages = {page.ordinal: page for page in packet.pages}
    form_page_kind = {
        Form1042SCopyType.B: Robinhood1042SPageKind.FORM_B,
        Form1042SCopyType.C: Robinhood1042SPageKind.FORM_C,
    }
    copy_types: dict[str | None, list[Form1042SCopyType]] = defaultdict(list)
    for copy in packet.copies:
        copy_types[copy.unique_form_identifier].append(copy.copy_type)
    summary_page = pages.get(packet.summary.page_ordinal)
    return (
        [page.ordinal for page in packet.pages] == list(range(1, page_count + 1))
        and sorted(copy.page_ordinal for copy in packet.copies)
        == [page.ordinal for page in packet.pages if page.kind in form_page_kind.values()]
        and all(
            (page := pages.get(copy.page_ordinal)) is not None
            and page.kind is form_page_kind.get(copy.copy_type)
            and page.unique_form_identifier == copy.unique_form_identifier
            and bool(copy.unique_form_identifier)
            and copy.tax_year == packet.tax_year
            for copy in packet.copies
        )
        and all(
            sorted(types) == [Form1042SCopyType.B, Form1042SCopyType.C]
            for types in copy_types.values()
        )
        and summary_page is not None
        and summary_page.kind is Robinhood1042SPageKind.BROKER_SUMMARY
    )


def test_packet_retention_requires_each_form_page_copy() -> None:
    text = _packet()
    packet = parse_robinhood_1042s_packet(text)
    page_count = len(text.removesuffix("\f").split("\f"))
    assert _packet_structure_holds(packet, page_count)
    assert not _packet_structure_holds(replace(packet, copies=()), page_count)
    assert not _packet_structure_holds(replace(packet, copies=packet.copies[2:]), page_count)


@private_test
def test_private_tax_packets_are_fully_classified() -> None:
    """Every PDF in a tax folder parses as a packet whose page records match
    the PDF's own page tree."""
    _root, paths = private_robinhood_pdfs("tax")
    for path in paths:
        try:
            packet = read_robinhood_1042s_packet(path)
        except ParseError:
            packet = None
        # Fail after the except block so pytest prints no source exception.
        if packet is None:
            pytest.fail("private tax packet audit failed", pytrace=False)
        if not _packet_structure_holds(packet, private_pdf_page_count(path)):
            pytest.fail("private tax packet retention audit failed", pytrace=False)
