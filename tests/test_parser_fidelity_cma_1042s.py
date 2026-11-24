"""Synthetic exact-shape tests for the observed Fidelity 1042-S packet."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.models.form_1042s import Form1042SAmountKind
from ibkr_fa.normalize.form_1042s import Form1042SViolation, form_1042s_violations
from ibkr_fa.parser.fidelity_cma_1042s import parse_1042s_packet
from tests._fidelity_cma_1042s import packet_text
from tests._repr_text import repr_without_addresses

_ACCOUNT = "Z99-999999"
_HOLDER = "TAX PAYER"
_RECORDS = (
    ("1234567890", "01", "02", "0.00", 2, 0),
    ("2234567890", "06", "02", "0.00", 2, 0),
    ("3234567890", "06", "00", "25.00", 8, 2),
)
_KEYS = ["01:0.00", "06:0.00", "06:25.00"]


def _packet() -> str:
    return packet_text(_RECORDS, account=_ACCOUNT, holder=_HOLDER)


def _put(
    text: str,
    anchor: str,
    column: int,
    value: str,
    *,
    below: int = 0,
    copies: str = "BCD",
) -> str:
    """Write ``value`` at ``column`` on the line ``below`` each ``anchor`` line."""
    pages = text.split("\f")
    edited = 0
    for page_index, page in enumerate(pages):
        if not any(f"COPY {copy}" in page for copy in copies):
            continue
        lines = page.split("\n")
        for index, line in enumerate(lines):
            if anchor in line:
                target = lines[index + below].ljust(column + len(value))
                lines[index + below] = target[:column] + value + target[column + len(value) :]
                edited += 1
        pages[page_index] = "\n".join(lines)
    assert edited, anchor
    return "\f".join(pages)


def _box_line(text: str, label: str, old: str, new: str) -> str:
    """Replace the end of every line that starts with ``label``."""
    lines = text.split("\n")
    edited = 0
    for index, line in enumerate(lines):
        if line.startswith(label) and line.endswith(old):
            lines[index] = line[: -len(old)] + new
            edited += 1
    assert edited, label
    return "\n".join(lines)


def test_parse_and_deduplicate_observed_packet() -> None:
    packet = parse_1042s_packet(_packet())

    assert packet.tax_year == 2025
    assert packet.account_id == _ACCOUNT
    assert [record.record_key for record in packet.records] == _KEYS
    form = packet.records[-1].form
    assert form.federal_tax_withheld.value == 2
    assert form.total_withholding_credit.value == 2
    assert form.chapter_indicator == "3"
    assert (
        form.federal_tax_not_deposited,
        form.subsequent_year_withholding,
        form.qualified_intermediary,
        form.pro_rata_reporting,
    ) == (False, False, False, False)
    assert all(form_1042s_violations(record.form) == () for record in packet.records)
    assert "1234567890" not in repr_without_addresses(packet)


_WHOLE = "must contain one bounded whole-dollar amount"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        pytest.param(
            lambda text: _put(text, "AMENDED", 155, "X"),
            "amended",
            id="amended-mark",
        ),
        pytest.param(
            lambda text: _put(text, "AMENDMENT NO.", 181, "1", copies="B"),
            "amended",
            id="amendment-number",
        ),
        pytest.param(
            lambda text: _put(text, "AMENDED", 150, "X", below=1),
            "amended",
            id="amended-mark-below",
        ),
        pytest.param(
            lambda text: _put(text, "Department of the Treasury", 60, "X", copies="D"),
            "Form 1042-S header is marked or changed",
            id="header-mark",
        ),
        pytest.param(
            lambda text: _put(text, "UNIQUE FORM IDENTIFIER", 44, "1 2 3", copies="C"),
            "Form 1042-S header is marked or changed",
            id="copy-c-ufi-line",
        ),
        pytest.param(
            lambda text: _put(text, "UNIQUE FORM IDENTIFIER", 66, "X", copies="B"),
            "Copy B/D UFI is missing or malformed",
            id="ufi-extra-token",
        ),
        pytest.param(
            lambda text: text.replace(
                "2.00            3a Exemption", "2.50            3a Exemption"
            ),
            f"Box 2 {_WHOLE}",
            id="box-2-cents",
        ),
        pytest.param(
            lambda text: _box_line(text, "7a Federal tax withheld", " 2.00", "-2.00").replace(
                "\n" + " " * 100 + "2.00\n", "\n" + " " * 99 + "-2.00\n"
            ),
            f"Box 7a {_WHOLE}",
            id="box-7a-and-10-negative",
        ),
        pytest.param(
            lambda text: _put(text, "5 Withholding allowance", 106, "0"),
            f"Box 5 {_WHOLE}",
            id="box-5-second-token",
        ),
        pytest.param(
            lambda text: _put(text, "5 Withholding allowance", 80, "6 Net income"),
            f"Box 5 {_WHOLE}",
            id="box-5-other-box-label",
        ),
        pytest.param(
            lambda text: _box_line(text, "6 Net income", "0.00", "   0"),
            f"Box 6 {_WHOLE}",
            id="box-6-no-decimals",
        ),
        pytest.param(
            lambda text: _box_line(text, "8 Tax withheld by other agents", "0.00", "0.0O"),
            f"Box 8 {_WHOLE}",
            id="box-8-other-text",
        ),
        pytest.param(
            lambda text: _put(text, "procedures(see instructions)", 3, " ", below=3),
            "Box 9 must contain one bounded whole-dollar amount",
            id="box-9-no-parentheses",
        ),
        pytest.param(
            lambda text: _put(text, "11 Tax paid by withholding agent", 103, "2"),
            f"Box 11 {_WHOLE}",
            id="box-11-no-decimals",
        ),
        pytest.param(
            lambda text: _put(text, "11 Tax paid by withholding agent", 100, "0.00"),
            None,
            id="box-11-zero-on-label-line",
        ),
        pytest.param(
            lambda text: _put(
                _put(text, "11 Tax paid by withholding agent", 100, "0.00"),
                "11 Tax paid by withholding agent",
                100,
                "0.00",
                below=1,
            ),
            f"Box 11 {_WHOLE}",
            id="box-11-two-values",
        ),
        pytest.param(
            lambda text: _put(text, "3b Tax rate", 69, "%", copies="D"),
            "Box 3b must contain one tax rate",
            id="box-3b-other-text",
        ),
        pytest.param(
            lambda text: _put(text, "13k Recipient", 136, "X", below=1),
            "recipient account is missing or malformed",
            id="account-extra-token",
        ),
        pytest.param(
            lambda text: _put(text, "12a Withholding agent", 101, "X", below=1),
            "withholding-agent status row is malformed",
            id="agent-row-extra-token",
        ),
        pytest.param(
            lambda text: text.replace("\f\f", "\funexpected back-page text\f", 1),
            "unexpected text-bearing page",
            id="back-page-text",
        ),
    ],
)
def test_packet_refusal_paths(mutation: Callable[[str], str], message: str | None) -> None:
    text = mutation(_packet())
    if message is None:
        assert [record.record_key for record in parse_1042s_packet(text).records] == _KEYS
        return
    with pytest.raises(ParseError, match=message):
        parse_1042s_packet(text)


_MARKS = ("X", "✔", "☒", "✓")


@pytest.mark.parametrize("mark", _MARKS)
@pytest.mark.parametrize(
    ("box", "anchor", "column"),
    [
        ("Box 7b", "escrow procedures were applied", 112),
        ("Box 7c", "   partnership interest", 113),
        ("Box 7d", "1042-S to report to a specific recipient", 112),
        ("Box 7b", "escrow procedures were applied", 117),
        ("Box 15", "15 Check if pro-rata basis reporting", 206),
        ("Box 15", "15 Check if pro-rata basis reporting", 165),
    ],
)
def test_checked_condition_box_refuses_naming_the_box(
    box: str,
    anchor: str,
    column: int,
    mark: str,
) -> None:
    with pytest.raises(ParseError) as caught:
        parse_1042s_packet(_put(_packet(), anchor, column, mark))
    assert caught.value.message == f"Form 1042-S {box} area is marked or changed"


def test_box_3_chapter_4_indicator_refuses() -> None:
    text = _put(_packet(), "3 Chapter indicator", 98, "4")
    with pytest.raises(ParseError) as caught:
        parse_1042s_packet(text)
    assert caught.value.message == "Form 1042-S Box 3 chapter indicator must be 3"


def test_copies_that_disagree_on_a_condition_box_refuse() -> None:
    text = _put(_packet(), "15 Check if pro-rata basis reporting", 206, "X", copies="C")
    with pytest.raises(ParseError, match="Box 15 area is marked or changed"):
        parse_1042s_packet(text)


@pytest.mark.parametrize(
    ("box", "label"),
    [
        ("Box 1", "1 Income"),
        ("Box 2", "2 Gross income"),
        ("Box 3", "3 Chapter indicator"),
        ("Box 3a", "3a Exemption code"),
        ("Box 3b", "3b Tax rate"),
        ("Box 4a", "4a Exemption code"),
        ("Box 4b", "4b Tax rate"),
        ("Box 5", "5 Withholding allowance"),
        ("Box 6", "6 Net income"),
        ("Box 7a", "7a Federal tax withheld"),
        ("Box 7b", "7b Check if"),
        ("Box 7c", "7c Check if"),
        ("Box 7d", "7d Check if"),
        ("Box 8", "8 Tax withheld by other agents"),
        ("Box 9", "9 Overwithheld tax repaid"),
        ("Box 10", "10 Total withholding credit"),
        ("Box 11", "11 Tax paid by withholding agent"),
        ("Box 15", "15 Check if pro-rata basis reporting"),
    ],
)
def test_duplicate_box_label_below_footer_refuses(box: str, label: str) -> None:
    text = _packet().replace("Form 1042-S (2025)", f"Form 1042-S (2025)\n{label} 7.00")
    with pytest.raises(ParseError, match=f"{box} label is duplicated"):
        parse_1042s_packet(text)


@pytest.mark.parametrize(
    ("column", "separator"),
    [(110, " "), (115, " "), (115, "\t"), (115, "  "), (115, "\u00a0")],
)
def test_duplicate_box_label_crossing_column_boundary_refuses(column: int, separator: str) -> None:
    duplicate = " " * column + f"5{separator}Withholding allowance 7.00"
    text = _packet().replace("Form 1042-S (2025)", f"Form 1042-S (2025)\n{duplicate}")
    with pytest.raises(ParseError, match="Box 5 label is duplicated"):
        parse_1042s_packet(text)


def test_tax_label_in_right_column_identity_value_is_not_a_duplicate() -> None:
    text = _put(_packet(), "  code", 116, "5 Withholding allowance 7.00")
    assert [record.record_key for record in parse_1042s_packet(text).records] == _KEYS


def test_duplicate_generic_rate_label_below_footer_refuses() -> None:
    text = _packet().replace("Form 1042-S (2025)", "Form 1042-S (2025)\nTax rate 7.00")
    with pytest.raises(ParseError, match="Boxes 3b/4b tax-rate row is duplicated"):
        parse_1042s_packet(text)


def test_conflicting_copy_refuses_without_leaking_identity() -> None:
    secret = "SECRET PERSON"
    text = _packet().replace(_HOLDER, secret)
    marker = "COPY D"
    index = text.rindex(marker)
    text = text[:index] + text[index:].replace(
        "8.00            3a Exemption", "9.00            3a Exemption", 1
    )

    with pytest.raises(ParseError) as caught:
        parse_1042s_packet(text)
    assert secret not in str(caught.value)
    assert _ACCOUNT not in str(caught.value)


def test_blank_box_11_stays_blank_and_matches_a_printed_zero() -> None:
    packet = parse_1042s_packet(_packet())
    assert {record.form.tax_paid_by_agent.kind for record in packet.records} == {
        Form1042SAmountKind.BLANK
    }

    text = _put(_packet(), "11 Tax paid by withholding agent", 100, "0.00", below=1, copies="C")
    assert [record.record_key for record in parse_1042s_packet(text).records] == _KEYS


def test_box_11_value_on_the_next_line_is_read_and_refused_by_the_shared_check() -> None:
    text = _put(_packet(), "11 Tax paid by withholding agent", 100, "2.00", below=1)
    packet = parse_1042s_packet(text)
    assert [record.record_key for record in packet.records] == _KEYS
    for record in packet.records:
        assert record.form.tax_paid_by_agent.value == 2
        assert form_1042s_violations(record.form) == (Form1042SViolation.TAX_PAID_BY_AGENT,)
