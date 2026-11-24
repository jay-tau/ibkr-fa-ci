"""Tests for the Fidelity NetBenefits ESPP trade-confirmation PDF parser.

Fixtures are synthetic ``pdftotext -layout`` text in the confirmation's layout.
The ``pdftotext_layout`` shim is exercised with a monkeypatched ``subprocess.run``
so the tests need neither ``pdftotext`` nor a binary PDF.

The value-refusal helper pins the exact location and message and an empty
``raw_value``. Distinctive input markers are checked absent from the rendered
refusal (#1041).
"""

from __future__ import annotations

import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser._pdf import _TIMEOUT_SECONDS, pdftotext_layout
from ibkr_fa.parser.netbenefits.confirmation_pdf import (
    EspPurchase,
    _money,
    parse_confirmation,
)
from tests._netbenefits import CONF_DEC_STREET_ALIGNED as CONF_DEC
from tests._netbenefits import assert_refusal as _assert_refusal
from tests._netbenefits import mar_confirmation
from tests._text_edits import replace_once as _replace_once

# The Mar purchase, reproducing the layout wart where
# "PURCHASE", "PRICE" and "Gain" run together (`...PURCHASE   PRICEGain   $83.92`),
# with no blank lines and another TRANS NO.
CONF_MAR = mar_confirmation(trans_no="7KM3PD", aligned=True, blank_lines=False, run_together=True)

# The bundle's label for a nested confirmation (#1041): its path relative to --input.
_LABEL = "confirmation first/confirmation.pdf"


def _refusal(text: str) -> ParseError:
    with pytest.raises(ParseError) as caught:
        parse_confirmation(text, source=_LABEL)
    return caught.value


def test_parse_dec_confirmation_exact() -> None:
    assert parse_confirmation(CONF_DEC) == EspPurchase(
        purchase_date=date(2025, 12, 31),
        settlement_date=date(2026, 1, 5),
        offering_period_start=date(2025, 11, 3),
        offering_period_end=date(2025, 12, 31),
        symbol="XESP",
        security_description="EXAMPLE CORP",
        cusip="99999E104",
        participant_no="I99999999",
        shares=Decimal("3.482"),
        purchase_price_per_share_usd=Decimal("192.1200"),
        fmv_total_usd=Decimal("743.30"),
        contribution_usd=Decimal("668.96"),
        perquisite_usd=Decimal("74.34"),
    )


def test_parse_mar_confirmation_run_together_layout() -> None:
    purchase = parse_confirmation(CONF_MAR)
    assert purchase.shares == Decimal("4.6315")
    assert purchase.purchase_price_per_share_usd == Decimal("163.0800")
    assert purchase.fmv_total_usd == Decimal("839.23")
    assert purchase.perquisite_usd == Decimal("83.92")
    # #1151: the settlement date is kept, from the data row's second date.
    assert purchase.settlement_date == date(2026, 4, 2)
    assert purchase.purchase_date == date(2026, 3, 31)


def test_fmv_per_share_is_total_over_shares() -> None:
    # Derived, not stored; the document evidences only the total.
    cent = Decimal("0.01")
    assert parse_confirmation(CONF_DEC).fmv_per_share_usd.quantize(cent) == Decimal("213.47")
    assert parse_confirmation(CONF_MAR).fmv_per_share_usd.quantize(cent) == Decimal("181.20")


def test_security_description_survives_page_break_and_indent() -> None:
    # A page break lands a form-feed before the "SECURITY DESCRIPTION" header and
    # may indent the name line; the issuer name must still be captured (not refused).
    header = "SECURITY DESCRIPTION   SYMBOL: XESP           EXPLANATION OF PROCEEDS"
    text = CONF_DEC.replace(
        f"{header}\nEXAMPLE CORP",
        f"\f  {header}\n   EXAMPLE CORP",
    )
    assert text != CONF_DEC
    assert parse_confirmation(text).security_description == "EXAMPLE CORP"


def test_blank_issuer_name_refuses_not_capturing_right_column() -> None:
    # If the left-column issuer name is missing, the parser must REFUSE — never
    # skip the empty column and capture the far right-column label as the name.
    text = CONF_DEC.replace("\nEXAMPLE CORP", "\n" + " " * 12)
    assert text != CONF_DEC
    with pytest.raises(ParseError, match="SECURITY DESCRIPTION"):
        parse_confirmation(text)


# --- refusal guards (it's taxes — never be wrong) -------------------------------


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        (lambda t: t.replace("SYMBOL: XESP", ""), "SYMBOL"),
        (lambda t: t.replace("YOU PURCHASED", "YOU BOUGHT"), "YOU PURCHASED"),
        (lambda t: t.replace("Market Value at Purchase¹   $743.30", ""), "Market Value"),
        (lambda t: t.replace("Offering period:", "Window:"), "Offering period"),
        (lambda t: t.replace("ESPP PLAN on DEC/31/2025.", "ESPP PLAN done"), "purchase date"),
        (lambda t: t.replace("Share Proceeds**   3.482", ""), "Share Proceeds"),
        (lambda t: t.replace("9XQ2TZ 99999E104", "9XQ2TZ"), "confirmation data row"),
        (lambda t: t.replace("SECURITY DESCRIPTION", "SECURITY INFO"), "SECURITY DESCRIPTION"),
    ],
)
def test_missing_field_refuses(mutation, needle: str) -> None:
    text = mutation(CONF_DEC)
    assert text != CONF_DEC
    with pytest.raises(ParseError, match=needle):
        parse_confirmation(text)


def test_duplicate_singleton_refuses() -> None:
    # A concatenated/spoofed document with two SYMBOL lines must not silently
    # parse only the first.
    doubled = CONF_DEC.replace("SYMBOL: XESP", "SYMBOL: XESP\nSYMBOL: XQBN")
    assert doubled != CONF_DEC
    with pytest.raises(ParseError, match="exactly one SYMBOL"):
        parse_confirmation(doubled)


def test_participant_header_vs_row_mismatch_names_neither_number() -> None:
    # #1041: the refusal says the two numbers differ and prints neither.
    text = _replace_once(CONF_DEC, "              I99999999\n", "              I12345678\n")
    text = _replace_once(text, "I99999999   1 000", "I00000001   1 000")
    _assert_refusal(
        _refusal(text),
        section=_LABEL,
        row_index=0,
        column="PARTICIPANT NO.",
        message=(
            "the PARTICIPANT NO. header differs from the participant number on the "
            "confirmation data row"
        ),
        absent=("I12345678", "I00000001"),
    )


def test_contributions_must_match() -> None:
    text = CONF_DEC.replace(
        "Accumulated Contributions   $668.96", "Accumulated Contributions   $999.99"
    )
    assert text != CONF_DEC
    with pytest.raises(ParseError, match="figures differ"):
        parse_confirmation(text)


def test_single_contribution_refuses() -> None:
    text = CONF_DEC.replace("Accumulated Contributions   $668.96\n", "", 1)
    assert text != CONF_DEC
    with pytest.raises(ParseError, match="exactly two Accumulated Contributions"):
        parse_confirmation(text)


def test_share_proceeds_must_match_shares() -> None:
    # #1041: neither share count is printed.
    text = _replace_once(CONF_DEC, "Share Proceeds**   3.482", "Share Proceeds**   987654.321")
    _assert_refusal(
        _refusal(text),
        section=_LABEL,
        row_index=0,
        column="Share Proceeds",
        message="Share Proceeds does not equal the shares purchased",
        absent=("987654.321", "3.482"),
    )


def test_gain_must_equal_fmv_minus_contribution() -> None:
    # #1041: neither the Gain figure nor Market Value minus Contributions is printed.
    text = _replace_once(CONF_DEC, "$74.34", "$987,654.32")
    _assert_refusal(
        _refusal(text),
        section=_LABEL,
        row_index=0,
        column="Gain",
        message="Market Value at Purchase minus Accumulated Contributions does not equal Gain",
        absent=("987654.32", "987,654.32", "74.34", "743.30", "668.96"),
    )


def test_shares_times_price_must_equal_contribution() -> None:
    # Corrupt the contribution by a dollar — well beyond the rounding tolerance.
    assert "$668.96" in CONF_DEC and "$74.34" in CONF_DEC
    text = CONF_DEC.replace("$668.96", "$658.96").replace("$74.34", "$84.34")
    # #1041: the gap (10.0018400) and the tolerance (0.10106000) used to be printed.
    _assert_refusal(
        _refusal(text),
        section=_LABEL,
        row_index=0,
        column="Accumulated Contributions",
        message=(
            "shares x purchase price should equal Contributions, but they differ by more "
            "than the rounding tolerance"
        ),
        absent=("10.00184", "0.10106", "658.96", "3.482", "192.12"),
    )


@pytest.mark.parametrize(
    ("old", "new", "column", "message", "absent"),
    [
        pytest.param(
            "12-31-25 01-05-26",
            "12-30-25 01-05-26",
            "trade date",
            "the trade date does not equal the purchase date",
            ("2025-12-30", "2025-12-31", "12-30-25", "DEC/31/2025"),
            id="trade-date-not-purchase-date",
        ),
        pytest.param(
            "12-31-25 01-05-26",
            "12-31-25 12-30-25",
            "settlement date",
            "the settlement date precedes the trade date",
            ("2025-12-30", "2025-12-31", "12-30-25", "12-31-25"),
            id="settlement-before-trade",
        ),
        pytest.param(
            "NOV/03/2025 - DEC/31/2025",
            "NOV/03/2025 - NOV/30/2025",
            "purchase date",
            "the purchase date is outside the offering period",
            ("2025-12-31", "2025-11-03", "2025-11-30", "NOV/03/2025", "NOV/30/2025", "DEC/31/2025"),
            id="purchase-outside-offering",
        ),
    ],
)
def test_date_cross_check_refuses_without_the_dates(
    old: str, new: str, column: str, message: str, absent: tuple[str, ...]
) -> None:
    # #1041: the cross-checks name the dates they compared, never their values.
    _assert_refusal(
        _refusal(_replace_once(CONF_DEC, old, new)),
        section=_LABEL,
        row_index=0,
        column=column,
        message=message,
        absent=absent,
    )


def test_malformed_money_refuses() -> None:
    # "$1,23.45" is not a valid grouped amount; the field regex matches nothing
    # rather than silently re-grouping it.
    text = CONF_DEC.replace("$743.30", "$1,23.45")
    assert text != CONF_DEC
    with pytest.raises(ParseError, match="Market Value"):
        parse_confirmation(text)


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        # trailing garbage must refuse, never silently truncate to a valid prefix
        (lambda t: t.replace("$743.30", "$743.30x"), "Market Value"),
        (lambda t: t.replace("$743.30", "$743.300"), "Market Value"),
        # #914: punctuation after a number refuses too (a trailing sign or comma
        # used to be dropped, leaving +74.34 or 743.30).
        (lambda t: t.replace("$74.34", "$74.34-"), "Gain"),
        (lambda t: t.replace("$743.30", "$743.30,00"), "Market Value"),
        (lambda t: t.replace("$668.96", "$668.96;", 1), "Accumulated Contributions"),
        (
            lambda t: t.replace("Share Proceeds**   3.482", "Share Proceeds**   3.482-"),
            "Share Proceeds",
        ),
        (lambda t: t.replace("YOU PURCHASED 3.482 AT", "YOU PURCHASED 3.482x AT"), "YOU PURCHASED"),
        (lambda t: t.replace("9XQ2TZ 99999E104", "9XQ2TZ 99999E104X"), "confirmation data row"),
        (lambda t: t.replace("9XQ2TZ 99999E104", "9XQ2TZ 99999E104x"), "confirmation data row"),
        (lambda t: t.replace("9XQ2TZ 99999E104", "9XQ2TZ 99999E104_"), "confirmation data row"),
        (lambda t: t.replace("9XQ2TZ 99999E104", "9XQ2TZ 99999E104."), "confirmation data row"),
        (lambda t: t.replace("PLAN on DEC/31/2025.", "PLAN on DEC/31/20255."), "purchase date"),
        (lambda t: t.replace("I99999999\n", "I99999999x\n", 1), "PARTICIPANT NO"),
        (lambda t: t.replace("I99999999\n", "I99999999.\n", 1), "PARTICIPANT NO"),
    ],
)
def test_trailing_garbage_refuses(mutation, needle: str) -> None:
    text = mutation(CONF_DEC)
    assert text != CONF_DEC
    with pytest.raises(ParseError, match=needle):
        parse_confirmation(text)


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        pytest.param("YOU PURCHASED 3.482 AT", "YOU PURCHASED 0.000 AT", "shares", id="shares"),
        pytest.param("AT $192.1200", "AT $0.0000", "purchase price", id="purchase-price"),
        pytest.param(
            "Market Value at Purchase¹   $743.30",
            "Market Value at Purchase¹   $0.00",
            "Market Value at Purchase",
            id="market-value",
        ),
        pytest.param("Gain²   $74.34", "Gain²   $0.00", "Gain", id="gain"),
        pytest.param(
            "Accumulated Contributions*  $668.96",
            "Accumulated Contributions*  $0.00",
            "Accumulated Contributions",
            id="contributions",
        ),
        pytest.param(
            "Share Proceeds**   3.482",
            "Share Proceeds**   0",
            "Share Proceeds",
            id="share-proceeds",
        ),
    ],
)
def test_zero_amount_refuses(old: str, new: str, field: str) -> None:
    # #1041: a zero collides with fixed text, so the exact message, the empty
    # raw value and the field cover it (no absence check).
    _assert_refusal(
        _refusal(_replace_once(CONF_DEC, old, new)),
        section=_LABEL,
        row_index=0,
        column=field,
        message=f"{field} must be a positive amount",
    )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        # The ``# pragma: no cover`` backstop: the format regex runs first.
        pytest.param("Synthetic Holder Two", "Gain is not a number", id="not-a-number"),
        pytest.param("-987654.32", "Gain must be a positive amount", id="negative"),
        pytest.param("NaN987654", "Gain must be a positive amount", id="non-finite"),
    ],
)
def test_money_helper_refuses_without_the_token(raw: str, message: str) -> None:
    # #1041: the regex keeps these tokens from the parser; the helper still
    # refuses them without printing them.
    with pytest.raises(ParseError) as caught:
        _money(raw, field="Gain", source=_LABEL)
    _assert_refusal(
        caught.value,
        section=_LABEL,
        row_index=0,
        column="Gain",
        message=message,
        absent=(raw.lstrip("-"),),
    )


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        # _mdy: an unknown month (KeyError) and impossible days (ValueError).
        pytest.param("NOV/03/2025", "XYZ/03/2025", "offering start", id="offering-start"),
        pytest.param("- DEC/31/2025", "- DEC/32/2025", "offering end", id="offering-end"),
        pytest.param("on DEC/31/2025.", "on FEB/30/2025.", "purchase date", id="purchase"),
        # _short_date on the confirmation data row.
        pytest.param("000   12-31-25", "000   13-31-25", "trade date", id="trade"),
        pytest.param("12-31-25 01-05-26", "12-31-25 01-32-26", "settlement date", id="settlement"),
    ],
)
def test_invalid_date_refuses_without_the_date(old: str, new: str, field: str) -> None:
    # #1041: the date token used to be printed and returned as the raw value.
    _assert_refusal(
        _refusal(_replace_once(CONF_DEC, old, new)),
        section=_LABEL,
        row_index=0,
        column=field,
        message=f"{field} is not a valid date",
        absent=(new.rsplit(maxsplit=1)[-1].rstrip("."),),
    )


# --- pdftotext_layout (subprocess shim) -----------------------------------------


# The bundle passes pdftotext the PDF's path relative to --input (#1041); the
# absolute path carries markers that must never reach the refusal.
_PDF_SOURCE = "first/confirmation.pdf"
_PRIVATE_PDF = Path("/private/Synthetic Holder Two/U1234567.pdf")


def test_pdftotext_layout_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args == (["pdftotext", "-enc", "UTF-8", "-layout", str(_PRIVATE_PDF), "-"],)
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "check": True,
            "timeout": _TIMEOUT_SECONDS,
        }
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=CONF_DEC)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert (
        pdftotext_layout(_PRIVATE_PDF, doc_kind="NetBenefits PDFs", source=_PDF_SOURCE) == CONF_DEC
    )


# The statement tests' test_pdftotext_layout_refusal_names_only_the_source covers
# the shim's other failures, the timeout included; only here does the failing
# pdftotext print a marker to stdout.
@pytest.mark.parametrize(
    ("failure", "message"),
    [
        pytest.param(
            subprocess.CalledProcessError(
                returncode=1,
                cmd=["pdftotext", str(_PRIVATE_PDF)],
                output="Synthetic Holder Two",
                stderr=(
                    f"Syntax Error: Couldn't open file '{_PRIVATE_PDF}': Synthetic Strerror Marker"
                ),
            ),
            "pdftotext failed",
            id="pdftotext-error",
        ),
    ],
)
def test_pdftotext_layout_failure_names_the_source_only(
    monkeypatch: pytest.MonkeyPatch, failure: subprocess.SubprocessError, message: str
) -> None:
    # #1041: the refusal names the source label, never the path, pdftotext's
    # stderr or the exception's text. The base code already hid these when a
    # caller passed ``source``; what this change adds is that every caller must
    # (see the statement tests' test_pdftotext_layout_requires_a_source_label).
    def boom(*args: object, **kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ParseError) as caught:
        pdftotext_layout(_PRIVATE_PDF, doc_kind="NetBenefits PDFs", source=_PDF_SOURCE)
    error = caught.value
    assert (error.section, error.row_index, error.column, error.message, error.raw_value) == (
        _PDF_SOURCE,
        0,
        None,
        message,
        "",
    )
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = str(error).casefold()
    for value in (
        str(_PRIVATE_PDF),
        "Synthetic Holder Two",
        "U1234567",
        "Synthetic Strerror Marker",
        "Syntax Error",
        "/private",
    ):
        assert value.casefold() not in rendered, value
