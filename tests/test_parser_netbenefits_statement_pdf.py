"""Tests for the Fidelity SPS statement PDF reader (#311 identity, #312 holdings/NAV).

Fixtures are synthetic ``pdftotext -layout`` text in the statement's layout (the
custodian disclosure is public boilerplate). The header block is repeated to
mirror the statement's per-page repetition. The ``pdftotext_layout`` shim is
exercised with a monkeypatched ``subprocess.run`` so the tests need neither
``pdftotext`` nor a binary PDF.

The value-refusal helper pins the fixed message, empty ``raw_value`` and exact
location. Per-row value checks name the table, field and 0-based table row;
whole-document and whole-table value cross-checks use row 0 and state their scope.
Distinctive input markers are checked absent from the rendered refusal (#1041).
"""

from __future__ import annotations

import errno
import inspect
import re
import subprocess
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser._pdf import pdftotext_layout
from ibkr_fa.parser.netbenefits.statement_pdf import (
    ReportType,
    StatementData,
    StatementPurchase,
    _money,
    parse_statement,
)
from tests._crash_reports import assert_unchained_refusal
from tests._netbenefits import BOUGHT_HEADER_1 as _BOUGHT_HEADER_1
from tests._netbenefits import BOUGHT_HEADER_2 as _BOUGHT_HEADER_2
from tests._netbenefits import BOUGHT_TITLE as _BOUGHT_TITLE
from tests._netbenefits import DATED_LINE_UNREAD as _DATED_UNREAD
from tests._netbenefits import assert_refusal as _assert_refusal
from tests._netbenefits import core_only_body, purchase_cash, purchase_row, spp_credit
from tests._netbenefits_statement_pdf import CASH_ROW as _CASH_ROW
from tests._netbenefits_statement_pdf import CASH_TOTAL as _CASH_TOTAL
from tests._netbenefits_statement_pdf import CUSTODIAN as _CUSTODIAN
from tests._netbenefits_statement_pdf import DIV_PERIOD as _DIV_PERIOD
from tests._netbenefits_statement_pdf import DIV_ROW as _DIV_ROW
from tests._netbenefits_statement_pdf import DIV_TOTAL as _DIV_TOTAL
from tests._netbenefits_statement_pdf import HOLDING_XESP as _HOLDING_XESP
from tests._netbenefits_statement_pdf import INCOME_ACTIVITY as _ACTIVITY
from tests._netbenefits_statement_pdf import INCOME_SUMMARY_PAD as _IS_PAD
from tests._netbenefits_statement_pdf import MMKT_ROW as _MMKT_ROW
from tests._netbenefits_statement_pdf import MMKT_TOTAL as _MMKT_TOTAL
from tests._netbenefits_statement_pdf import MONTHLY
from tests._netbenefits_statement_pdf import OTHER_CASH_ACTIVITY as _OTHER_CASH_ACTIVITY
from tests._netbenefits_statement_pdf import PARTICIPANT_NO as _PNO
from tests._netbenefits_statement_pdf import PRICED_BODY as _PRICED_BODY
from tests._netbenefits_statement_pdf import TOTAL_CORE_LINE as _TOTAL_CORE_LINE
from tests._netbenefits_statement_pdf import TOTAL_HOLDINGS_LINE as _TOTAL_HOLDINGS_LINE
from tests._netbenefits_statement_pdf import TOTAL_STOCKS_LINE as _TOTAL_STOCKS_LINE
from tests._netbenefits_statement_pdf import UNPRICED_BODY as _UNPRICED_BODY
from tests._netbenefits_statement_pdf import UNREAD_TOTALS as _UNREAD_TOTALS
from tests._netbenefits_statement_pdf import WHT_ROW as _WHT_ROW
from tests._netbenefits_statement_pdf import WHT_TOTAL as _WHT_TOTAL
from tests._netbenefits_statement_pdf import XESP_ROW as _XESP_ROW
from tests._netbenefits_statement_pdf import YEAR_END_PERIOD as _YEAR_END_PERIOD
from tests._netbenefits_statement_pdf import YEAR_END_WHT_LINE as _YEAR_END_WHT_LINE
from tests._netbenefits_statement_pdf import account_summary as _account_summary
from tests._netbenefits_statement_pdf import continuation_header as _continuation
from tests._netbenefits_statement_pdf import custodian_footer as _footer
from tests._netbenefits_statement_pdf import dec31_unpriced_text as _dec31_unpriced
from tests._netbenefits_statement_pdf import income_statement_text as _income
from tests._netbenefits_statement_pdf import income_summary as _income_summary
from tests._netbenefits_statement_pdf import page1_header as _page1
from tests._netbenefits_statement_pdf import period_summaries as _summary
from tests._netbenefits_statement_pdf import statement_text as _statement
from tests._netbenefits_statement_pdf import year_end_text as _year_end
from tests._number_limits import MONEY

# ``parse_statement``'s default document label, every refusal's ``section``.
_SOURCE = "Fidelity SPS statement"
# A whole-document or whole-table check is located at row 0 and says so (#1041).
_WHOLE_DOCUMENT = "; the whole document was checked"
_WHOLE_TABLE = "; the whole table was checked"
# Distinctive amounts (#1041): they can't collide with fixed text or a location.
_MARKER_MONEY = "$987,654.32"
_MARKER_AMOUNTS = ("987,654.32", "987654.32")


def _refusal(text: str, *, expected_participant_no: str | None = None) -> ParseError:
    with pytest.raises(ParseError) as raised:
        parse_statement(text, expected_participant_no=expected_participant_no)
    return raised.value


def _privacy_values(text: str) -> tuple[str, ...]:
    """The synthetic markers, decimal values and MM/DD dates actually in this input."""
    return tuple(
        re.findall(
            r"(?i:synthetic holder two|synthetic marker q)|99997Q10[4]?|"
            r"[0-9][0-9,]*\.[0-9]+|[0-9]{2}/[0-9]{2}",
            text,
        )
    )


MULTI_MONTH = _statement(
    "STOCK PLAN SERVICES REPORT", "August 18, 2025 - October 31, 2025", body=_UNPRICED_BODY
)
YEAR_END = _statement(
    "2025 YEAR-END INVESTMENT REPORT", "January 1, 2025 - December 31, 2025", body=_UNPRICED_BODY
)


def test_parse_monthly_identity_and_period() -> None:
    assert parse_statement(MONTHLY) == StatementData(
        report_type=ReportType.STOCK_PLAN_SERVICES,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        participant_no=_PNO,
        account_holder="TAX PAYER",
        custodian=_CUSTODIAN,
        holdings=(_HOLDING_XESP,),
        account_nav_usd=Decimal("653.36"),
    )


def test_parse_multi_month_period() -> None:
    data = parse_statement(MULTI_MONTH)
    assert data.report_type is ReportType.STOCK_PLAN_SERVICES
    assert data.period_start == date(2025, 8, 18)
    assert data.period_end == date(2025, 10, 31)


def test_parse_year_end_identity_and_period() -> None:
    data = parse_statement(YEAR_END)
    assert data.report_type is ReportType.YEAR_END
    assert data.period_start == date(2025, 1, 1)
    assert data.period_end == date(2025, 12, 31)


def test_participant_cross_check_passes() -> None:
    data = parse_statement(MONTHLY, expected_participant_no=_PNO)
    assert data.participant_no == _PNO


def test_custodian_comma_variant_accepted() -> None:
    # Fidelity also uses the comma form "Fidelity Stock Plan Services, LLC"; a
    # legitimate statement using it must parse, storing the matched text verbatim.
    text = MONTHLY.replace(_CUSTODIAN, "Fidelity Stock Plan Services, LLC")
    assert parse_statement(text).custodian == "Fidelity Stock Plan Services, LLC"


def test_missing_custodian_disclosure_refuses() -> None:
    text = MONTHLY.replace(_CUSTODIAN, "Some Other Custodian Inc")
    with pytest.raises(ParseError, match="custodian disclosure"):
        parse_statement(text)


def test_participant_cross_check_mismatch_refuses() -> None:
    # It names the config field by its path, never either number (#1041).
    exc = _refusal(MONTHLY.replace(_PNO, "I12345678"), expected_participant_no="I00000001")
    _assert_refusal(
        exc,
        section=_SOURCE,
        column="Participant Number",
        row_index=0,
        message="the Participant Number differs from netbenefits.participant_no in user.toml"
        + _WHOLE_DOCUMENT,
        absent=("I12345678", "I00000001"),
    )


def test_multipage_continuation_parses() -> None:
    # A page-1 header + continuation pages that repeat report+period+participant
    # but omit the holder (the statement layout). Holder comes from page 1; the
    # repeated signals are consistent, so it parses.
    data = parse_statement(MONTHLY)
    assert data.account_holder == "TAX PAYER"
    assert data.participant_no == _PNO
    assert data.period_start == date(2026, 1, 1)


# --- refusal guards (it's taxes — never be wrong) -------------------------------


def test_no_header_refuses() -> None:
    with pytest.raises(ParseError, match="no Fidelity SPS statement header"):
        parse_statement("some unrelated PDF text\n" + _footer())


_JAN_2026 = "January 1, 2026 - January 31, 2026"
_FEB_2026 = "February 1, 2026 - February 28, 2026"


def _header_pages(*headers: tuple[str, str]) -> str:
    """Page 1, then one continuation page per further header, each header a
    ``(period, Participant Number)`` pair."""
    (first_period, first_pno), *more = headers
    text = _page1("STOCK PLAN SERVICES REPORT", first_period).replace(_PNO, first_pno)
    for period, pno in more:
        text += _continuation("STOCK PLAN SERVICES REPORT", period).replace(_PNO, pno)
    return text + _footer()


@pytest.mark.parametrize(
    ("headers", "row_index"),
    [
        pytest.param([(_JAN_2026, _PNO), (_FEB_2026, _PNO)], 1, id="second-page"),
        pytest.param([(_JAN_2026, _PNO), (_JAN_2026, _PNO), (_FEB_2026, _PNO)], 2, id="third-page"),
    ],
)
def test_inconsistent_period_across_pages_refuses(
    headers: list[tuple[str, str]], row_index: int
) -> None:
    # A stapled / merged PDF: a continuation page with a different period. The
    # refusal names the first differing header occurrence, never either period (#1041).
    exc = _refusal(_header_pages(*headers))
    _assert_refusal(
        exc,
        section=_SOURCE,
        column="report header",
        row_index=row_index,
        message="inconsistent report header across pages: this occurrence differs from the first",
        absent=("January", "February", "2026", "STOCK PLAN SERVICES REPORT"),
    )


@pytest.mark.parametrize(
    ("headers", "row_index", "absent"),
    [
        pytest.param(
            [(_JAN_2026, _PNO), (_JAN_2026, "I88888888")],
            1,
            (_PNO, "I88888888"),
            id="second-page",
        ),
        pytest.param(
            [(_JAN_2026, "I12345678"), (_JAN_2026, "I12345678"), (_JAN_2026, "I00000001")],
            2,
            ("I12345678", "I00000001"),
            id="third-page",
        ),
    ],
)
def test_inconsistent_participant_across_pages_refuses(
    headers: list[tuple[str, str]], row_index: int, absent: tuple[str, ...]
) -> None:
    # A continuation page with a different Participant Number: located at the first
    # differing header occurrence; neither number is printed (#1041).
    exc = _refusal(_header_pages(*headers))
    _assert_refusal(
        exc,
        section=_SOURCE,
        column="Participant Number",
        row_index=row_index,
        message=(
            "inconsistent Participant Number across pages: this occurrence differs from the first"
        ),
        absent=absent,
    )


def test_missing_custodian_refuses() -> None:
    text = _page1("STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026")
    with pytest.raises(ParseError, match="custodian disclosure"):
        parse_statement(text)


def test_period_start_after_end_refuses() -> None:
    text = _statement("STOCK PLAN SERVICES REPORT", "January 31, 2026 - January 1, 2026")
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="report header: period",
        row_index=0,
        message="the period start is after its end" + _WHOLE_DOCUMENT,
        absent=("January", "2026"),
    )


def test_year_end_not_full_year_refuses() -> None:
    text = _statement("2025 YEAR-END INVESTMENT REPORT", "January 1, 2025 - November 30, 2025")
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="report header: period",
        row_index=0,
        message="the year-end report period is not a full Jan 1 - Dec 31 year" + _WHOLE_DOCUMENT,
        absent=("November", "2025", "11-30"),
    )


def test_year_end_title_year_must_match_period() -> None:
    # A "2025 YEAR-END" title over a full 2024 calendar year is a contradiction.
    text = _statement("2025 YEAR-END INVESTMENT REPORT", "January 1, 2024 - December 31, 2024")
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="report header: title year",
        row_index=0,
        message="the year-end title year does not match the period year" + _WHOLE_DOCUMENT,
        absent=("2024", "2025"),
    )


def test_year_prefixed_monthly_title_refuses() -> None:
    # Monthly/multi-month titles never carry a year prefix; one that does is not a
    # valid SPS header.
    text = _statement("2025 STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026")
    with pytest.raises(ParseError, match="no Fidelity SPS statement header"):
        parse_statement(text)


def test_participant_line_trailing_token_refuses() -> None:
    # A trailing field after the participant number must refuse, not be ignored.
    text = _statement("STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026").replace(
        f"Participant Number: {_PNO}", f"Participant Number: {_PNO} STALE"
    )
    with pytest.raises(ParseError, match="no Participant Number found"):
        parse_statement(text)


def test_single_page_malformed_participant_refuses() -> None:
    # Page 1 + page 3 are clean; one continuation page has a trailing token. The
    # malformed line must be noticed, not silently skipped while clean pages agree.
    text = (
        _page1("STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026")
        + _continuation("STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026").replace(
            f"Participant Number: {_PNO}", f"Participant Number: {_PNO} STALE"
        )
        + _continuation("STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026")
        + _footer()
    )
    with pytest.raises(ParseError, match="Participant Number line is malformed"):
        parse_statement(text)


def test_single_page_malformed_report_refuses() -> None:
    # One continuation page's period is corrupted (trailing token after the year).
    text = (
        _page1("STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026")
        + _continuation("STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026 EXTRA")
        + _footer()
    )
    with pytest.raises(ParseError, match="report-type header is malformed"):
        parse_statement(text)


@pytest.mark.parametrize(
    ("period", "column"),
    [
        pytest.param(
            "February 30, 2026 - March 31, 2026", "report header: period start", id="start"
        ),
        pytest.param("February 1, 2026 - February 30, 2026", "report header: period end", id="end"),
    ],
)
def test_invalid_calendar_date_refuses(period: str, column: str) -> None:
    # Neither the date nor the calendar's own error text is printed (#1041).
    _assert_refusal(
        _refusal(_statement("STOCK PLAN SERVICES REPORT", period)),
        section=_SOURCE,
        column=column,
        row_index=0,
        message="not a valid calendar date",
        absent=("February 30", "2026", "out of range"),
    )


@pytest.mark.parametrize(
    ("period", "column"),
    [
        pytest.param("Smarch 1, 2026 - March 31, 2026", "report header: period start", id="start"),
        pytest.param("March 1, 2026 - Smarch 31, 2026", "report header: period end", id="end"),
    ],
)
def test_invalid_month_name_refuses(period: str, column: str) -> None:
    # A date that fits the layout but names no real month must refuse.
    _assert_refusal(
        _refusal(_statement("STOCK PLAN SERVICES REPORT", period)),
        section=_SOURCE,
        column=column,
        row_index=0,
        message="not a Fidelity statement date (expected 'Month D, YYYY')",
        absent=("Smarch", "2026"),
    )


def test_truncated_participant_refuses() -> None:
    # A trailing extra char must refuse, not silently truncate the participant.
    text = _statement("STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026").replace(
        f"Participant Number: {_PNO}", f"Participant Number: {_PNO}x"
    )
    with pytest.raises(ParseError, match="no Participant Number found"):
        parse_statement(text)


def test_no_account_holder_refuses() -> None:
    # Continuation pages only (report + period + participant, no holder line).
    text = (
        _continuation("STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026") * 2
        + _footer()
    )
    with pytest.raises(ParseError, match="no account holder found"):
        parse_statement(text)


def test_period_line_not_mistaken_for_holder() -> None:
    # A malformed header with the period directly above the participant (no holder,
    # no blank line) must refuse — never record the period string as the holder.
    text = (
        "STOCK PLAN SERVICES REPORT\n"
        "January 1, 2026 - January 31, 2026\n"
        f"Participant Number: {_PNO}\n"
    ) + _footer()
    with pytest.raises(ParseError, match="no account holder found"):
        parse_statement(text)


def test_period_shaped_holder_line_refuses() -> None:
    # A malformed header where a period line sits in the holder position (blank
    # above it) must refuse, not record the period string as the account holder.
    text = (
        "                 STOCK PLAN SERVICES REPORT\n"
        "                 January 1, 2026 - January 31, 2026\n"
        "\n"
        "                 January 1, 2026 - January 31, 2026\n"
        f"                   Participant Number: {_PNO}\n"
    ) + _footer()
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="account holder",
        row_index=0,
        message="the account holder line looks like a statement period; malformed header",
        absent=("January", "2026"),
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param("TAX PAYER", "OTHER PERSON", id="two-names"),
        pytest.param("Synthetic Holder Two", "ANN EXAMPLE", id="markers"),
    ],
)
def test_inconsistent_account_holder_refuses(first: str, second: str) -> None:
    # The refusal says there is more than one holder without naming either (#1041).
    text = (
        _page1("STOCK PLAN SERVICES REPORT", _JAN_2026).replace("TAX PAYER", first)
        + _page1("STOCK PLAN SERVICES REPORT", _JAN_2026).replace("TAX PAYER", second)
        + _footer()
    )
    exc = _refusal(text)
    _assert_refusal(
        exc,
        section=_SOURCE,
        column="account holder",
        row_index=0,
        message="the statement names more than one account holder" + _WHOLE_DOCUMENT,
        absent=(first, second),
    )
    # "ANN" is checked as a whole token: fixed text may contain those letters.
    assert re.search(r"(?i)\bANN\b", str(exc)) is None


# --- holdings + NAV (#312) ------------------------------------------------------


def test_priced_holdings_and_nav() -> None:
    data = parse_statement(MONTHLY)
    assert data.account_nav_usd == Decimal("653.36")
    assert data.holdings == (_HOLDING_XESP,)


def test_unpriced_statement_has_no_holdings_or_nav() -> None:
    data = parse_statement(YEAR_END)
    assert data.account_nav_usd is None
    assert data.holdings == ()


def test_money_market_row_excluded_from_stock_total() -> None:
    # The core / money-market fund row ("not applicable" cost/gain, its "(SYMBOL)" on
    # the wrapped continuation line) is NOT a stock holding; it is excluded from
    # ``holdings`` and from the stocks-only Total Stocks cross-check, and is surfaced
    # separately as ``core_holding`` while the account NAV includes its value.
    body = (
        "       Your Stock Plan Account Value:                 $654.45\n"
        "          Ending Account Value Incl. AI               $654.45\n"
        "Holdings\n"
        "Core Account\n"
        "SAMPLE ISSUER CP MONEY MARKET FUND CL   unavailable   1.090   $1.0000   $1.09"
        "   not applicable   not applicable   $0.06\n"
        "OUS (XQCP)                                      3.725%\n"
        "Total Core Account (0% of account   unavailable   $1.09   $0.06\n"
        "holdings)\n"
        "Stocks\n"
        + _XESP_ROW
        + _TOTAL_STOCKS_LINE
        + "Total Holdings   $654.45   $668.96   -$15.60   $5.91\n"
    )
    data = parse_statement(
        _statement("STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026", body=body)
    )
    assert data.holdings == (_HOLDING_XESP,)
    assert data.account_nav_usd == Decimal("654.45")
    assert data.core_holding is not None
    # The wrapped name rejoins across the continuation line.
    assert data.core_holding.description == "SAMPLE ISSUER CP MONEY MARKET FUND CL OUS"
    assert data.core_holding.symbol == "XQCP"
    assert data.core_holding.quantity == Decimal("1.090")
    assert data.core_holding.ending_market_value_usd == Decimal("1.09")


def test_stocks_plus_core_must_equal_nav() -> None:
    """A core position the parser fails to see must refuse, not vanish.

    The core fund is a Schedule FA asset in its own right, so reconciling the parts
    against the stated NAV is what turns a future section rename into a refusal
    rather than a silently dropped disclosure.
    """
    body = (
        "       Your Stock Plan Account Value:                 $654.45\n"
        "          Ending Account Value Incl. AI               $654.45\n"
        "Holdings\n"
        + _XESP_ROW
        + _TOTAL_STOCKS_LINE
        + "Total Holdings   $654.45   $668.96   -$15.60   $5.91\n"
    )
    text = _statement("STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026", body=body)
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Ending Account Value Incl. AI",
        row_index=0,
        message="the stock holdings plus the Core Account do not equal the account NAV; an "
        "account position is unaccounted for" + _WHOLE_DOCUMENT,
        absent=("654.45", "653.36", "1.09"),
    )


def _priced(body_mutation) -> str:
    body = body_mutation(_PRICED_BODY)
    assert body != _PRICED_BODY
    return _statement(
        "STOCK PLAN SERVICES REPORT",
        "January 1, 2026 - January 31, 2026",
        body=body,
    )


def test_no_account_value_line_refuses() -> None:
    text = _statement(
        "STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026", body="Holdings\n"
    )
    with pytest.raises(ParseError, match="no 'Your Stock Plan Account Value:' line"):
        parse_statement(text)


def test_inconsistent_account_value_state_refuses() -> None:
    text = _priced(lambda b: b + "       Your Stock Plan Account Value:                 -\n")
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Your Stock Plan Account Value",
        row_index=0,
        message="inconsistent account-value state across pages" + _WHOLE_DOCUMENT,
        absent=("653.36",),
    )


def test_priced_missing_nav_line_refuses() -> None:
    text = _priced(
        lambda b: b.replace("          Ending Account Value Incl. AI               $653.36\n", "")
    )
    with pytest.raises(ParseError, match=r"missing the 'Ending Account Value Incl\. AI'"):
        parse_statement(text)


def test_inconsistent_nav_across_pages_refuses() -> None:
    text = _priced(
        lambda b: b + f"          Ending Account Value Incl. AI               {_MARKER_MONEY}\n"
    )
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Ending Account Value Incl. AI",
        row_index=0,
        message="inconsistent account NAV across pages" + _WHOLE_DOCUMENT,
        absent=(*_MARKER_AMOUNTS, "653.36"),
    )


@pytest.mark.parametrize(
    ("line", "value", "column"),
    [
        pytest.param(
            "          Ending Account Value Incl. AI               {}\n",
            "$0.00",
            "Ending Account Value Incl. AI",
            id="nav-zero",
        ),
        pytest.param(
            "          Ending Account Value Incl. AI               {}\n",
            f"-{_MARKER_MONEY}",
            "Ending Account Value Incl. AI",
            id="nav-negative",
        ),
        pytest.param(
            "       Your Stock Plan Account Value:                 {}\n",
            "$0.00",
            "Your Stock Plan Account Value",
            id="account-value-zero",
        ),
        pytest.param(
            "       Your Stock Plan Account Value:                 {}\n",
            f"-{_MARKER_MONEY}",
            "Your Stock Plan Account Value",
            id="account-value-negative",
        ),
    ],
)
def test_non_positive_nav_or_account_value_refuses(line: str, value: str, column: str) -> None:
    # A whole-document figure: located at row 0, and the message says so (#1041).
    text = _priced(lambda b: b.replace(line.format("$653.36"), line.format(value)))
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column=column,
        row_index=0,
        message=f"{column} must be > 0" + _WHOLE_DOCUMENT,
        # Zero is covered by the exact message and location; only the marker is checked.
        absent=_MARKER_AMOUNTS if _MARKER_MONEY in value else (),
    )


def test_priced_with_no_holdings_rows_refuses() -> None:
    # Priced (account value + NAV) but the holdings table has no data row.
    text = _priced(lambda b: b.replace(_XESP_ROW, ""))
    with pytest.raises(ParseError, match="no parseable Holdings rows"):
        parse_statement(text)


def test_unpriced_with_stray_priced_content_refuses() -> None:
    text = _statement(
        "STOCK PLAN SERVICES REPORT",
        "January 1, 2026 - January 31, 2026",
        body=_UNPRICED_BODY + _XESP_ROW,
    )
    with pytest.raises(ParseError, match="unexpectedly contains a priced holdings"):
        parse_statement(text)


@pytest.mark.parametrize(
    "fragment",
    (
        "SAMPLE ISSUER CP MONEY MARKET FUND CL   unavailable   1.090   $1.0000   $1.09"
        "   not applicable   not applicable   $0.06\nOUS (XQCP)\n",
        "Total Core Account (0% of account   unavailable   $1.09   $0.06\nholdings)\n",
    ),
)
def test_unpriced_with_stray_core_content_refuses(fragment: str) -> None:
    text = _statement(
        "STOCK PLAN SERVICES REPORT",
        "January 1, 2026 - January 31, 2026",
        body=_UNPRICED_BODY + fragment,
    )
    with pytest.raises(ParseError, match="unexpectedly contains a priced holdings"):
        parse_statement(text)


@pytest.mark.parametrize(
    ("old", "new", "absent"),
    [
        # Corrupt the price so ending != quantity x price by dollars.
        pytest.param("$187.6400", "$500.0000", ("653.36", "1741", "1087.64"), id="price"),
        # Or the ending value itself, to a distinctive amount (#1041).
        pytest.param(
            "$187.6400   $653.36",
            f"$187.6400   {_MARKER_MONEY}",
            (*_MARKER_AMOUNTS, "653.36"),
            id="ending-marker",
        ),
    ],
)
def test_holding_ending_vs_qty_price_mismatch_refuses(
    old: str, new: str, absent: tuple[str, ...]
) -> None:
    text = _priced(lambda b: b.replace(old, new))
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Holdings: ending market value",
        row_index=0,
        message="the ending market value and quantity x price differ by more than the rounding "
        "tolerance",
        absent=absent,
    )


_GAIN_LOSS_IDENTITY = (
    "the ending market value minus the cost basis must equal the unrealized gain/loss"
)


def test_holding_gain_loss_mismatch_refuses() -> None:
    # ending - cost must equal the unrealized gain/loss; the row's distinctive
    # gain/loss is not printed (#1041).
    text = _priced(lambda b: b.replace("-$15.60   $5.85\n", f"-{_MARKER_MONEY}   $5.85\n", 1))
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Holdings: unrealized gain/loss",
        row_index=0,
        message=_GAIN_LOSS_IDENTITY,
        absent=(*_MARKER_AMOUNTS, "15.60"),
    )


def test_holding_gain_loss_off_by_one_cent_refuses() -> None:
    # The ending - cost == gain/loss identity is exact (no 1-cent slack).
    text = _priced(lambda b: b.replace("-$15.60", "-$15.59"))
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Holdings: unrealized gain/loss",
        row_index=0,
        message=_GAIN_LOSS_IDENTITY,
        absent=("15.59", "15.60"),
    )


def test_truncated_quantity_refuses() -> None:
    # A stock quantity must be exactly 3 dp; a truncated 3.4 is a malformed row (it
    # is still a stock-row candidate, so it refuses rather than parsing loosely).
    text = _priced(lambda b: b.replace("   3.482   ", "   3.4   "))
    with pytest.raises(ParseError, match="Holdings stock row is malformed"):
        parse_statement(text)


def test_nav_with_trailing_money_token_refuses() -> None:
    # A NAV value followed by a second money token is ambiguous and must refuse.
    text = _priced(
        lambda b: b + "          Ending Account Value Incl. AI               $653.36   $999.00\n"
    )
    with pytest.raises(ParseError, match="NAV line is malformed"):
        parse_statement(text)


def test_nav_with_trailing_digit_refuses() -> None:
    # An extra trailing digit ($653.360) must not be truncated to $653.36.
    text = _priced(lambda b: b + "          Ending Account Value Incl. AI               $653.360\n")
    with pytest.raises(ParseError, match="NAV line is malformed"):
        parse_statement(text)


def test_nav_with_comma_delimited_second_money_refuses() -> None:
    # A second money token after a non-space delimiter is still ambiguous.
    text = _priced(
        lambda b: b + "          Ending Account Value Incl. AI               $653.36, $999.00\n"
    )
    with pytest.raises(ParseError, match="NAV line is malformed"):
        parse_statement(text)


def test_total_holdings_not_equal_nav_refuses() -> None:
    text = _priced(
        lambda b: b.replace("Total Holdings   $653.36", f"Total Holdings   {_MARKER_MONEY}")
    )
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Holdings: Total Holdings",
        row_index=0,
        message="the 'Total Holdings' ending value does not equal the account NAV"
        + _WHOLE_DOCUMENT,
        absent=(*_MARKER_AMOUNTS, "653.36"),
    )


def test_missing_total_holdings_refuses() -> None:
    text = _priced(lambda b: b.replace(_TOTAL_HOLDINGS_LINE, ""))
    with pytest.raises(ParseError, match="exactly one 'Total Holdings' row"):
        parse_statement(text)


def test_malformed_total_holdings_row_refuses() -> None:
    # A Total Holdings row with a corrupt layout (trailing junk / missing columns)
    # must refuse, not silently match only the first money token.
    text = _priced(
        lambda b: b.replace(_TOTAL_HOLDINGS_LINE, "Total Holdings   $653.36   JUNKTOKEN\n")
    )
    with pytest.raises(ParseError, match="exactly one 'Total Holdings' row"):
        parse_statement(text)


def test_malformed_account_value_line_refuses() -> None:
    text = _priced(
        lambda b: b + "       Your Stock Plan Account Value:                 $653.36 EXTRA\n"
    )
    with pytest.raises(ParseError, match="'Your Stock Plan Account Value:' line is malformed"):
        parse_statement(text)


def test_truncated_after_beginning_column_holding_refuses() -> None:
    # A stock row truncated right after the beginning column ((XQBN) unavailable, e.g.
    # a page split) is still a candidate and must refuse, not be silently dropped.
    text = _priced(lambda b: b + "SAMPLE ISSUER BN INC (XQBN)   unavailable\n")
    with pytest.raises(ParseError, match="Holdings stock row is malformed"):
        parse_statement(text)


def test_malformed_beginning_column_holding_refuses() -> None:
    # A stock row whose *beginning* column is malformed money ($1,23.00) is still a
    # stock-row candidate, so it must refuse rather than be silently dropped.
    text = _priced(
        lambda b: (
            b
            + "SAMPLE ISSUER BN INC (XQBN)   $1,23.00   1.000   $100.0000   $100.00   $90.00"
            + "   $10.00   $1.00\n"
        )
    )
    with pytest.raises(ParseError, match="Holdings stock row is malformed"):
        parse_statement(text)


def test_non_ascii_digits_refused() -> None:
    # Unicode (e.g. Arabic-Indic) digits must not be silently normalized to a value.
    text = _priced(lambda b: b.replace("$187.6400", "$\u0661\u0668\u0667.\u0666\u0664\u0660\u0660"))
    with pytest.raises(ParseError, match="Holdings stock row is malformed"):
        parse_statement(text)


def test_hyphenated_ticker_parses() -> None:
    # Hyphenated tickers (a class suffix such as -B) are valid Fidelity symbols and must
    # parse, matching the repo-wide ticker convention.
    body = (
        "       Your Stock Plan Account Value:                 $100.00\n"
        "          Ending Account Value Incl. AI               $100.00\n"
        "Holdings\nStocks\n"
        "SAMPLE ISSUER CZ CORP (XQCZ-B)   unavailable   1.000   $100.0000   $100.00   $90.00"
        "   $10.00   $2.00\n"
        "Total Stocks (100% of account holdings)   unavailable   $100.00   $90.00"
        "   $10.00   $2.00\n"
        "Total Holdings   $100.00   $90.00   $10.00   $2.00\n"
    )
    data = parse_statement(
        _statement("STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026", body=body)
    )
    assert data.holdings[0].symbol == "XQCZ-B"
    assert data.holdings[0].description == "SAMPLE ISSUER CZ CORP"


def test_digit_leading_description_parses() -> None:
    # A stock description may start with a digit.
    body = (
        "       Your Stock Plan Account Value:                 $100.00\n"
        "          Ending Account Value Incl. AI               $100.00\n"
        "Holdings\nStocks\n"
        "9 SAMPLE ISSUER DE CO (XQDE)   unavailable   1.000   $100.0000   $100.00   $90.00"
        "   $10.00   $2.00\n"
        "Total Stocks (99.5% of account holdings)   unavailable   $100.00   $90.00"
        "   $10.00   $2.00\n"
        "Total Holdings   $100.00   $90.00   $10.00   $2.00\n"
    )
    data = parse_statement(
        _statement("STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026", body=body)
    )
    assert data.holdings[0].symbol == "XQDE"
    assert data.holdings[0].description == "9 SAMPLE ISSUER DE CO"


@pytest.mark.parametrize(
    ("old", "new", "label", "absent"),
    [
        # Corrupt only the Total Stocks ending so it no longer equals the row sum.
        pytest.param("$653.36", "$900.00", "ending value", ("900.00", "653.36"), id="ending"),
        # The subtotal is the sum of the displayed rows, so even a one-cent gap refuses.
        pytest.param(
            "$653.36", "$653.37", "ending value", ("653.37", "653.36"), id="ending-one-cent"
        ),
        pytest.param("$668.96", _MARKER_MONEY, "cost", (*_MARKER_AMOUNTS, "668.96"), id="cost"),
        pytest.param("-$15.60", "-$15.61", "gain/loss", ("15.61", "15.60"), id="gain-loss"),
        pytest.param("$5.85", "$5.86", "EAI", ("5.86", "5.85"), id="eai"),
    ],
)
def test_total_stocks_sum_mismatch_refuses(
    old: str, new: str, label: str, absent: tuple[str, ...]
) -> None:
    assert old in _TOTAL_STOCKS_LINE
    text = _priced(lambda b: b.replace(_TOTAL_STOCKS_LINE, _TOTAL_STOCKS_LINE.replace(old, new)))
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Holdings: Total Stocks",
        row_index=0,
        message=f"the sum of the holdings' {label} does not equal 'Total Stocks'" + _WHOLE_TABLE,
        absent=absent,
    )


def test_malformed_holding_row_refuses() -> None:
    # A second stock row that fails to strict-match (trailing junk) must refuse via
    # the loose candidate-count guard, not be silently skipped.
    text = _priced(lambda b: b + _XESP_ROW.rstrip("\n") + " JUNKTOKEN\n")
    with pytest.raises(ParseError, match="Holdings stock row is malformed"):
        parse_statement(text)


def test_missing_total_stocks_row_refuses() -> None:
    text = _priced(lambda b: b.replace(_TOTAL_STOCKS_LINE, ""))
    with pytest.raises(ParseError, match="exactly one 'Total Stocks' subtotal row"):
        parse_statement(text)


def test_duplicate_total_stocks_row_refuses() -> None:
    # Two identical Total Stocks rows (e.g. a stapled/merged PDF) must refuse, not
    # be silently collapsed.
    text = _priced(lambda b: b + _TOTAL_STOCKS_LINE)
    with pytest.raises(ParseError, match="exactly one 'Total Stocks' subtotal row"):
        parse_statement(text)


def test_malformed_total_stocks_row_refuses() -> None:
    # A second Total Stocks line that fails to strict-match (trailing junk token)
    # must refuse via the loose label-count guard, not be silently skipped.
    text = _priced(
        lambda b: (
            b + "Total Stocks (100% of account holdings)   unavailable   $653.36   $668.96"
            "   -$15.60   $5.85   JUNKTOKEN\n"
        )
    )
    with pytest.raises(ParseError, match="exactly one 'Total Stocks' subtotal row"):
        parse_statement(text)


def test_account_value_not_equal_nav_refuses() -> None:
    # "Your Stock Plan Account Value:" must equal the NAV; a divergence refuses.
    text = _priced(
        lambda b: b.replace(
            "       Your Stock Plan Account Value:                 $653.36\n",
            f"       Your Stock Plan Account Value:                 {_MARKER_MONEY}\n",
        )
    )
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Your Stock Plan Account Value",
        row_index=0,
        message="'Your Stock Plan Account Value:' does not equal the account NAV" + _WHOLE_DOCUMENT,
        absent=(*_MARKER_AMOUNTS, "653.36"),
    )


def test_negative_eai_accepted() -> None:
    # Fidelity's footer states EAI may be negative (short / EY positions), so a signed
    # EAI is valid data and must parse (it is still cross-checked against Total Stocks).
    text = _priced(lambda b: b.replace("$5.85", "-$5.85"))
    data = parse_statement(text)
    assert data.holdings[0].eai_usd == Decimal("-5.85")


def test_zero_ending_value_refuses() -> None:
    text = _priced(lambda b: b.replace("$187.6400   $653.36", "$187.6400   $0.00"))
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Holdings: ending market value",
        row_index=0,
        message="Holdings: ending market value must be > 0",
    )


# A second stock row, balanced: 1.000 x $100.0000 = $100.00; $100.00 - $90.00 = $10.00.
_XQBN_ROW = (
    "SAMPLE ISSUER BN INC (XQBN)   unavailable   1.000   $100.0000   $100.00   $90.00"
    "   $10.00   $1.00\n"
)


def _two_holdings(first: str = _XESP_ROW, second: str = _XQBN_ROW) -> str:
    """A statement whose Holdings table has two stock rows (#1041)."""
    body = (
        "       Your Stock Plan Account Value:                 $753.36\n"
        "          Ending Account Value Incl. AI               $753.36\n"
        "Holdings\nStocks\nCommon Stock\n"
        + first
        + second
        + "Total Stocks (100% of account holdings)   unavailable   $753.36   $758.96"
        "   -$5.60   $6.85\n"
        "Total Holdings   $753.36   $758.96   -$5.60   $6.85\n"
    )
    return _statement("STOCK PLAN SERVICES REPORT", _JAN_2026, body=body)


def test_two_holdings_parse() -> None:
    assert [h.symbol for h in parse_statement(_two_holdings()).holdings] == ["XESP", "XQBN"]


@pytest.mark.parametrize(
    ("zero_row", "row_index"),
    [pytest.param("second", 1, id="second-row"), pytest.param("first", 0, id="first-row")],
)
def test_zero_price_is_located_at_its_holdings_row(zero_row: str, row_index: int) -> None:
    # The row is located by its 0-based position in the Holdings table, and
    # ``section`` is the document label the caller passes (#1041).
    if zero_row == "second":
        text = _two_holdings(second=_XQBN_ROW.replace("$100.0000", "$0.0000"))
    else:
        text = _two_holdings(first=_XESP_ROW.replace("$187.6400", "$0.0000"))
    with pytest.raises(ParseError) as raised:
        parse_statement(text, source="statement first/statement.pdf")
    _assert_refusal(
        raised.value,
        section="statement first/statement.pdf",
        column="Holdings: price",
        row_index=row_index,
        message="Holdings: price must be > 0",
    )
    assert str(raised.value) == (
        "ParseError in section 'statement first/statement.pdf' at "
        f"row {row_index}, column 'Holdings: price': Holdings: price must be > 0"
    )


@pytest.mark.parametrize(
    ("old", "new", "column", "message", "absent"),
    [
        pytest.param(
            "   1.000   ",
            "   0.000   ",
            "Holdings: quantity",
            "Holdings: quantity must be > 0",
            (),
            id="quantity",
        ),
        pytest.param(
            "$100.0000", "$0.0000", "Holdings: price", "Holdings: price must be > 0", (), id="price"
        ),
        pytest.param(
            "$100.00   $90.00",
            "$0.00   $90.00",
            "Holdings: ending market value",
            "Holdings: ending market value must be > 0",
            (),
            id="ending-market-value",
        ),
        pytest.param(
            "$90.00",
            "$0.00",
            "Holdings: cost basis",
            "Holdings: cost basis must be > 0",
            (),
            id="cost-basis",
        ),
        pytest.param(
            "$100.0000",
            "$200.0000",
            "Holdings: ending market value",
            "the ending market value and quantity x price differ by more than the rounding "
            "tolerance",
            ("100.00", "200.00"),
            id="ending-vs-quantity-x-price",
        ),
        pytest.param(
            "$10.00   $1.00",
            "$11.00   $1.00",
            "Holdings: unrealized gain/loss",
            _GAIN_LOSS_IDENTITY,
            ("10.00", "11.00", "90.00"),
            id="gain-loss-identity",
        ),
    ],
)
def test_second_holdings_row_refusal_is_located_at_row_1(
    old: str, new: str, column: str, message: str, absent: tuple[str, ...]
) -> None:
    assert old in _XQBN_ROW
    exc = _refusal(_two_holdings(second=_XQBN_ROW.replace(old, new)))
    _assert_refusal(
        exc, section=_SOURCE, column=column, row_index=1, message=message, absent=absent
    )


# The Core Account row of ``_income()``: 1.090 x $1.0000 = $1.09.
_CORE_ROW_VALUES = "unavailable   1.090   $1.0000   $1.09   not applicable"


@pytest.mark.parametrize(
    ("old", "new", "column", "message", "absent"),
    [
        pytest.param(
            _CORE_ROW_VALUES,
            _CORE_ROW_VALUES.replace("1.090", "0.000"),
            "Core Account: quantity",
            "Core Account: quantity must be > 0",
            (),
            id="quantity",
        ),
        pytest.param(
            _CORE_ROW_VALUES,
            _CORE_ROW_VALUES.replace("$1.0000", "$0.0000"),
            "Core Account: price",
            "Core Account: price must be > 0",
            (),
            id="price",
        ),
        pytest.param(
            _CORE_ROW_VALUES,
            _CORE_ROW_VALUES.replace("$1.09", "$0.00"),
            "Core Account: ending market value",
            "Core Account: ending market value must be > 0",
            (),
            id="ending-market-value",
        ),
        pytest.param(
            _CORE_ROW_VALUES,
            _CORE_ROW_VALUES.replace("1.090", "5.000"),
            "Core Account: ending market value",
            "the ending market value and quantity x price differ by more than the rounding "
            "tolerance",
            ("1.09", "5.000", "3.91"),
            id="ending-vs-quantity-x-price",
        ),
        pytest.param(
            _TOTAL_CORE_LINE,
            _TOTAL_CORE_LINE.replace("$0.06", "$0.07"),
            "Core Account: Total Core Account",
            "the Core Account holding's ending market value and EAI differ from the "
            "'Total Core Account' subtotal" + _WHOLE_TABLE,
            ("1.09", "0.06", "0.07"),
            id="total-core-account",
        ),
    ],
)
def test_core_account_refusal_is_located(
    old: str, new: str, column: str, message: str, absent: tuple[str, ...]
) -> None:
    # The Core Account table has one row, so a row check is at row 0; the foot
    # check covers the whole table and says so (#1041).
    text = _income()
    assert text.count(old) == 1
    _assert_refusal(
        _refusal(text.replace(old, new)),
        section=_SOURCE,
        column=column,
        row_index=0,
        message=message,
        absent=absent,
    )


def test_money_backstop_refuses_without_the_token() -> None:
    # The statement regexes pre-validate every money token, so this refusal is a
    # backstop; it keeps the caller's location and never prints the token (#1041).
    with pytest.raises(ParseError) as raised:
        _money("$987,654.3X", source=_SOURCE, column="Holdings: price", row_index=1)
    _assert_refusal(
        raised.value,
        section=_SOURCE,
        column="Holdings: price",
        row_index=1,
        message="not a valid money amount",
        absent=("987,654.3X", "987654.3X", "987,654", "987654"),
    )


# --- pdftotext_layout (subprocess shim) -----------------------------------------

# The caller's document label; pdftotext's path, stderr and the exception's own
# text (strerror, file name) are never printed (#1041).
_PDF_SOURCE = "statement first/statement.pdf"
_STRERROR_MARKER = "Synthetic Strerror Marker"
_FILENAME_MARKER = "Synthetic Holder Two"


def test_pdftotext_layout_requires_a_source_label() -> None:
    # Without a default, no caller can fall back to labelling a refusal by its path.
    source = inspect.signature(pdftotext_layout).parameters["source"]
    assert (source.kind, source.default) == (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.empty,
    )


@pytest.mark.parametrize(
    ("error", "message"),
    [
        pytest.param(
            FileNotFoundError(errno.ENOENT, _STRERROR_MARKER, f"{_FILENAME_MARKER}/pdftotext"),
            "'pdftotext' not found on PATH (ENOENT); install poppler-utils to read "
            "NetBenefits PDFs",
            id="missing-pdftotext",
        ),
        # A bare FileNotFoundError, as this file raised before #1041: no errno to name.
        pytest.param(
            FileNotFoundError(),
            "'pdftotext' not found on PATH (unknown error); install poppler-utils to read "
            "NetBenefits PDFs",
            id="missing-pdftotext-no-errno",
        ),
        # pdftotext present but not runnable must become a ParseError, not a raw
        # OS traceback.
        pytest.param(
            PermissionError(errno.EACCES, _STRERROR_MARKER, f"{_FILENAME_MARKER}/x.pdf"),
            "could not run pdftotext (EACCES)",
            id="not-executable",
        ),
        # Undecodable pdftotext output must become a ParseError, not a raw traceback.
        pytest.param(
            UnicodeDecodeError(
                "utf-8", f"{_FILENAME_MARKER} \xff".encode("latin-1"), 21, 22, _STRERROR_MARKER
            ),
            "pdftotext output was not valid UTF-8",
            id="non-utf8",
        ),
        pytest.param(
            subprocess.TimeoutExpired(
                ["pdftotext", f"{_FILENAME_MARKER}/x.pdf"], 30, stderr=_STRERROR_MARKER
            ),
            f"pdftotext timed out reading {_PDF_SOURCE}",
            id="timeout",
        ),
        pytest.param(
            subprocess.CalledProcessError(
                1,
                ["pdftotext", f"{_FILENAME_MARKER}/x.pdf"],
                output="",
                stderr=f"{_STRERROR_MARKER}: {_FILENAME_MARKER}/x.pdf",
            ),
            "pdftotext failed",
            id="failed",
        ),
    ],
)
def test_pdftotext_layout_refusal_names_only_the_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception, message: str
) -> None:
    # The non-UTF-8, timeout and failed cases also pass before #1041: with a
    # ``source`` the old shim already printed fixed text. What #1041 changes there is
    # that ``source`` is required (above); these pin the texts it now always prints.
    def boom(*args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr(subprocess, "run", boom)
    path = tmp_path / _FILENAME_MARKER / "statement.pdf"
    with pytest.raises(ParseError) as raised:
        pdftotext_layout(path, doc_kind="NetBenefits PDFs", source=_PDF_SOURCE)
    exc = raised.value
    _assert_refusal(
        exc,
        section=_PDF_SOURCE,
        column=None,
        row_index=0,
        message=message,
        absent=(_STRERROR_MARKER, _FILENAME_MARKER, str(tmp_path)),
    )
    # Raised ``from None``: no chained traceback carries the exception's text.
    assert exc.__cause__ is None
    assert exc.__context__ is None


# --- income activity: dividends / withholding / MMKT sweep (#I9) -----------------

# Fixed refusal texts of the income tables (#1041).
_SWEEP_MISMATCH = (
    "the net dividend (dividends plus withholding) does not equal the MMKT sweep on this "
    "activity date; its rows: "
)
_RUNNING_BALANCE = (
    "the running balance does not equal the previous row's balance plus this row's amount"
)
_MMKT_AMOUNT_VS_QTY_PRICE = (
    "the buy amount and quantity x price differ by more than the rounding tolerance"
)
_TXN_DATE_OUTSIDE = "the transaction date is not uniquely within the statement period"


def test_parse_income_activity() -> None:
    data = parse_statement(_income())
    (div,) = data.dividends
    assert (div.date, div.security_name, div.cusip, div.gross_amount_usd) == (
        date(2026, 3, 5),
        "EXAMPLE CORP",
        "99999E104",
        Decimal("1.46"),
    )
    (wht,) = data.withholdings
    assert (wht.date, wht.security_name, wht.amount_usd) == (
        date(2026, 3, 5),
        "EXAMPLE CORP",
        Decimal("-0.37"),
    )
    (mmkt,) = data.mmkt_buys
    assert (mmkt.date, mmkt.fund_name, mmkt.quantity, mmkt.price_usd, mmkt.amount_usd) == (
        date(2026, 3, 5),
        "FID TREASURY ONLY MMKT FUND CL OUS",
        Decimal("1.090"),
        Decimal("1.0000"),
        Decimal("1.09"),
    )


def test_parse_priced_core_only_account() -> None:
    body = (
        "       Your Stock Plan Account Value:                 $1.09\n"
        "          Ending Account Value Incl. AI               $1.09\n" + _summary() + "Holdings\n"
        "Core Account\n"
        "FID TREASURY ONLY MMKT FUND CL   unavailable   1.090   "
        "$1.0000   $1.09   not applicable   not applicable   $0.06\n"
        "OUS (XQCP)                                      3.725%\n"
        "Total Core Account (100% of account   unavailable   $1.09   $0.06\n"
        "holdings)\n"
        "Total Holdings   $1.09   $0.00   $0.00   $0.06\n" + _ACTIVITY
    )

    data = parse_statement(_statement("STOCK PLAN SERVICES REPORT", _DIV_PERIOD, body=body))

    assert data.holdings == ()
    assert data.core_holding is not None
    assert data.core_holding.ending_market_value_usd == Decimal("1.09")


def test_no_income_activity_is_empty() -> None:
    data = parse_statement(MONTHLY)
    assert data.dividends == ()
    assert data.withholdings == ()
    assert data.mmkt_buys == ()


# Four template variations at once, all in the core money-market fund's own rows:
# a SECOND dividend row for a security other than the plan's, amounts/prices
# printed WITHOUT the ``$`` (Fidelity prints it only on a column's first row), a
# security name WRAPPED onto the next line, and the ``Reinvestment`` transaction
# verb alongside ``You Bought``. Balanced as
# 3.41 + 0.02 gross - 0.85 withheld = 2.58 swept (2.56 + 0.02).
_CORE_FUND_ACTIVITY = (
    "Dividends, Interest & Other Income\n"
    "(Includes dividend reinvestment)\n"
    "Date Security Name CUSIP Description Quantity Price Amount\n"
    "05/28 EXAMPLE CORP 99999E104 Dividend Received - - $3.41\n"
    "06/30 FID TREASURY ONLY MMKT FUND CL 99971X104 Dividend Received - - 0.02\n"
    "OUS\n"
    "Total Dividends, Interest & Other Income $3.43\n"
    "Taxes Withheld\n"
    "Date Security Description Amount\n"
    "05/28 EXAMPLE CORP Non-Resident Tax -$0.85\n"
    "Total Federal Taxes Withheld -0.85\n"
    "Total Taxes Withheld -$0.85\n"
    "Core Fund Activity\n"
    "Date Type Transaction Description Quantity Price Amount Balance\n"
    "05/28 CASH You Bought FID TREASURY ONLY MMKT FUND CL OUS 2.560 $1.0000 $2.56 $3.65\n"
    "06/30 CASH Reinvestment FID TREASURY ONLY MMKT FUND CL OUS 0.020 1.0000 0.02 3.67\n"
    "Total Core Fund Activity $2.58\n"
)


def test_core_fund_dividend_and_reinvestment_parse() -> None:
    """The core money-market fund's own dividend and reinvestment rows parse.

    Guards all four template variations at once. Critically, the
    wrapped name must rejoin to the SAME string the Core Fund Activity row prints
    unwrapped — a truncated ``...CL`` would drift against an overlapping statement
    and break the (date, name) withholding pairing downstream.
    """
    data = parse_statement(
        _income(
            _CORE_FUND_ACTIVITY,
            period="May 1, 2026 - June 30, 2026",
            core_ending=Decimal("3.67"),
            summary=_summary("3.43", "-0.85"),
        )
    )
    assert [(d.security_name, d.cusip, d.gross_amount_usd) for d in data.dividends] == [
        ("EXAMPLE CORP", "99999E104", Decimal("3.41")),
        ("FID TREASURY ONLY MMKT FUND CL OUS", "99971X104", Decimal("0.02")),
    ]
    # One withholding for two dividends: the fund's distribution is not withheld on.
    assert [(w.security_name, w.amount_usd) for w in data.withholdings] == [
        ("EXAMPLE CORP", Decimal("-0.85"))
    ]
    assert [(m.quantity, m.price_usd, m.amount_usd) for m in data.mmkt_buys] == [
        (Decimal("2.560"), Decimal("1.0000"), Decimal("2.56")),
        (Decimal("0.020"), Decimal("1.0000"), Decimal("0.02")),
    ]
    # The wrapped dividend name must equal the unwrapped Core Fund Activity name.
    assert data.dividends[1].security_name == data.mmkt_buys[0].fund_name


# The second row of each income table, dated 03/20 and printed without the ``$``.
_DIV_ROW_2 = "03/20 EXAMPLE CORP 99999E104 Dividend Received - - 1.00\n"
_WHT_ROW_2 = "03/20 EXAMPLE CORP Non-Resident Tax -0.25\n"
_MMKT_ROW_2 = "03/20 CASH You Bought FID TREASURY ONLY MMKT FUND CL OUS 0.750 1.0000 0.75 1.84\n"
# Two rows per income table, balanced per date: 1.46 - 0.37 = 1.09, 1.00 - 0.25 = 0.75.
_TWO_ROW_ACTIVITY = (
    "Dividends, Interest & Other Income\n"
    "Date Security Name CUSIP Description Quantity Price Amount\n"
    "03/05 EXAMPLE CORP 99999E104 Dividend Received - - $1.46\n"
    + _DIV_ROW_2
    + "Total Dividends, Interest & Other Income $2.46\n"
    "Taxes Withheld\n"
    "Date Security Description Amount\n"
    "03/05 EXAMPLE CORP Non-Resident Tax -$0.37\n"
    + _WHT_ROW_2
    + "Total Federal Taxes Withheld -0.62\n"
    "Total Taxes Withheld -$0.62\n"
    "Core Fund Activity\n"
    "Date Type Transaction Description Quantity Price Amount Balance\n"
    "03/05 CASH You Bought FID TREASURY ONLY MMKT FUND CL OUS "
    "1.090 $1.0000 $1.09 $1.09\n" + _MMKT_ROW_2 + "Total Core Fund Activity $1.84\n"
)


def _two_row_income(activity: str = _TWO_ROW_ACTIVITY) -> str:
    return _income(activity, core_ending=Decimal("1.84"), summary=_summary("2.46", "-0.62"))


def test_second_withholding_row_may_omit_dollar_sign() -> None:
    data = parse_statement(_two_row_income())
    assert [w.amount_usd for w in data.withholdings] == [Decimal("-0.37"), Decimal("-0.25")]


def test_income_reconciliation_is_per_date() -> None:
    # Preserve every section total and the final/running balance while swapping the
    # two sweep amounts across dates. An aggregate-only check would accept this.
    assert "2.560 $1.0000 $2.56 $3.65" in _CORE_FUND_ACTIVITY
    assert "0.020 1.0000 0.02 3.67" in _CORE_FUND_ACTIVITY
    activity = _CORE_FUND_ACTIVITY.replace(
        "2.560 $1.0000 $2.56 $3.65", "0.020 $1.0000 $0.02 $1.11"
    ).replace("0.020 1.0000 0.02 3.67", "2.560 1.0000 2.56 3.67")
    assert activity != _CORE_FUND_ACTIVITY
    text = _income(
        activity,
        period="May 1, 2026 - June 30, 2026",
        core_ending=Decimal("3.67"),
        summary=_summary("3.43", "-0.85"),
    )
    # The first date fails; its rows are named by table and position, never the date
    # or the amounts (#1041).
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Income activity: daily sweep",
        row_index=0,
        message=_SWEEP_MISMATCH + "Dividends, Interest & Other Income row 0; "
        "Taxes Withheld row 0; Core Fund Activity row 0",
        absent=("2026-05-28", "05/28", "May 28", "3.41", "0.85", "2.56", "0.02"),
    )


def test_mmkt_running_balance_must_follow_each_buy() -> None:
    assert "$2.56 $3.65" in _CORE_FUND_ACTIVITY
    activity = _CORE_FUND_ACTIVITY.replace("$2.56 $3.65", "$2.56 $9.99")
    text = _income(
        activity,
        period="May 1, 2026 - June 30, 2026",
        core_ending=Decimal("3.67"),
        summary=_summary("3.43", "-0.85"),
    )
    # The first row sets the balance; the second doesn't follow it.
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Core Fund Activity: balance",
        row_index=1,
        message=_RUNNING_BALANCE,
        absent=("9.99", "3.67", "10.01"),
    )


def test_final_mmkt_balance_must_match_core_holding() -> None:
    assert "$1.09 $1.09\n" in _ACTIVITY
    activity = _ACTIVITY.replace("$1.09 $1.09\n", "$1.09 $9.99\n")
    _assert_refusal(
        _refusal(_income(activity)),
        section=_SOURCE,
        column="Core Fund Activity: balance",
        row_index=0,
        message="the final Core Fund Activity balance does not equal the Core Account holding's "
        "ending market value" + _WHOLE_DOCUMENT,
        absent=("9.99", "1.09"),
    )


def test_wrapped_name_does_not_swallow_the_total_line() -> None:
    """A dividend row NOT followed by a continuation keeps its printed name.

    The optional continuation group is constrained to ALL-CAPS text so it cannot
    absorb the Title-Case ``Total ...`` foot that follows the last row.
    """
    (div,) = parse_statement(_income()).dividends
    assert div.security_name == "EXAMPLE CORP"


@pytest.mark.parametrize(("row", "events"), ((_DIV_ROW, "dividends"), (_WHT_ROW, "withholdings")))
def test_wrapped_name_does_not_swallow_repeated_page_header(row: str, events: str) -> None:
    page_header = _continuation("STOCK PLAN SERVICES REPORT", _DIV_PERIOD)
    assert row in _ACTIVITY
    data = parse_statement(_income(_ACTIVITY.replace(row, row + page_header)))
    (event,) = getattr(data, events)
    assert event.security_name == "EXAMPLE CORP"


def test_dividend_total_mismatch_refuses() -> None:
    assert _DIV_TOTAL in _ACTIVITY
    text = _income(
        _ACTIVITY.replace(_DIV_TOTAL, "Total Dividends, Interest & Other Income $2.00\n")
    )
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Dividends, Interest & Other Income: Total",
        row_index=0,
        message="the sum of the dividend rows does not equal the section's 'Total'" + _WHOLE_TABLE,
        absent=("2.00", "1.46"),
    )


def test_withholding_total_mismatch_refuses() -> None:
    assert _WHT_TOTAL in _ACTIVITY
    text = _income(_ACTIVITY.replace(_WHT_TOTAL, "Total Taxes Withheld -$0.99\n"))
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Taxes Withheld: Total",
        row_index=0,
        message="the sum of the withholding rows does not equal the section's 'Total'"
        + _WHOLE_TABLE,
        absent=("0.99", "0.37"),
    )


def test_mmkt_total_mismatch_refuses() -> None:
    assert _MMKT_TOTAL in _ACTIVITY
    text = _income(_ACTIVITY.replace(_MMKT_TOTAL, f"Total Core Fund Activity {_MARKER_MONEY}\n"))
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Core Fund Activity: Total",
        row_index=0,
        message="the sum of the MMKT buys does not equal the section's 'Total'" + _WHOLE_TABLE,
        absent=(*_MARKER_AMOUNTS, "1.09"),
    )


def test_coupling_mismatch_refuses() -> None:
    # gross 2.00 - withheld 0.37 = 1.63 != swept 1.09 (each section self-consistent).
    assert _DIV_ROW in _ACTIVITY and _DIV_TOTAL in _ACTIVITY
    activity = _ACTIVITY.replace(
        _DIV_ROW, "03/05 EXAMPLE CORP 99999E104 Dividend Received - - $2.00\n"
    ).replace(_DIV_TOTAL, "Total Dividends, Interest & Other Income $2.00\n")
    _assert_refusal(
        _refusal(_income(activity, summary=_summary("2.00", "-0.37"))),
        section=_SOURCE,
        column="Income activity: daily sweep",
        row_index=0,
        message=_SWEEP_MISMATCH + "Dividends, Interest & Other Income row 0; "
        "Taxes Withheld row 0; Core Fund Activity row 0",
        absent=("2026-03-05", "03/05", "March 5", "2.00", "0.37", "1.63", "1.09"),
    )


def test_mmkt_amount_not_quantity_times_price_refuses() -> None:
    # Amount 5.00 cannot come from 1.090 x $1.0000; the per-buy cross-check refuses.
    assert _MMKT_ROW in _ACTIVITY and _MMKT_TOTAL in _ACTIVITY
    activity = _ACTIVITY.replace(
        _MMKT_ROW,
        "03/05 CASH You Bought FID TREASURY ONLY MMKT FUND CL OUS 1.090 $1.0000 $5.00 $5.00\n",
    ).replace(_MMKT_TOTAL, "Total Core Fund Activity $5.00\n")
    _assert_refusal(
        _refusal(_income(activity)),
        section=_SOURCE,
        column="Core Fund Activity: amount",
        row_index=0,
        message=_MMKT_AMOUNT_VS_QTY_PRICE,
        absent=("5.00", "1.09", "3.91"),
    )


def test_unsupported_income_row_refuses() -> None:
    # An extra dated row that is not "Dividend Received" must refuse, not be skipped.
    extra = "03/15 ACME BOND CORP 99971R107 Interest Received - - $0.10\n"
    assert _DIV_TOTAL in _ACTIVITY
    with pytest.raises(ParseError, match=r"unsupported or malformed row"):
        parse_statement(_income(_ACTIVITY.replace(_DIV_TOTAL, extra + _DIV_TOTAL)))


def test_positive_withholding_refuses() -> None:
    assert _WHT_ROW in _ACTIVITY and _WHT_TOTAL in _ACTIVITY
    activity = _ACTIVITY.replace(_WHT_ROW, "03/05 EXAMPLE CORP Non-Resident Tax $0.37\n").replace(
        _WHT_TOTAL, "Total Taxes Withheld $0.37\n"
    )
    _assert_refusal(
        _refusal(_income(activity)),
        section=_SOURCE,
        column="Taxes Withheld: amount",
        row_index=0,
        message="Non-Resident Tax must be a negative (withheld) amount",
        absent=("0.37",),
    )


def test_section_missing_total_refuses() -> None:
    assert _DIV_TOTAL in _ACTIVITY
    with pytest.raises(ParseError, match=r"expected exactly one .* 'Total' line; found 0"):
        parse_statement(_income(_ACTIVITY.replace(_DIV_TOTAL, "")))


def test_malformed_section_header_refuses() -> None:
    # A header carrying trailing text must refuse, not silently drop the section's
    # income (the strict header would miss it while the rows/total are present).
    activity = _ACTIVITY.replace(
        "Dividends, Interest & Other Income\n",
        "Dividends, Interest & Other Income SUMMARY\n",
        1,
    )
    with pytest.raises(ParseError, match=r"section header is malformed"):
        parse_statement(_income(activity))


def test_malformed_section_total_refuses() -> None:
    assert _DIV_TOTAL in _ACTIVITY
    activity = _ACTIVITY.replace(
        _DIV_TOTAL, "Total Dividends, Interest & Other Income $1.46 SEE NOTE\n"
    )
    with pytest.raises(ParseError, match=r"section 'Total' line is malformed"):
        parse_statement(_income(activity))


def test_total_before_header_refuses() -> None:
    # The (single) Total line appearing before its header must refuse.
    assert _DIV_ROW + _DIV_TOTAL in _ACTIVITY
    activity = _ACTIVITY.replace(
        "Dividends, Interest & Other Income\n",
        _DIV_TOTAL + "Dividends, Interest & Other Income\n",
        1,
    ).replace(_DIV_ROW + _DIV_TOTAL, _DIV_ROW)
    with pytest.raises(ParseError, match=r"'Total' line precedes its header"):
        parse_statement(_income(activity))


def test_digit_leading_security_income() -> None:
    # A security whose name starts with a digit must parse, matching the
    # holdings parser's digit-leading allowance.
    assert "EXAMPLE CORP 99999E104" in _ACTIVITY
    assert "EXAMPLE CORP Non-Resident Tax" in _ACTIVITY
    activity = _ACTIVITY.replace(
        "EXAMPLE CORP 99999E104", "9 SAMPLE ISSUER DE CO 99972M107"
    ).replace("EXAMPLE CORP Non-Resident Tax", "9 SAMPLE ISSUER DE CO Non-Resident Tax")
    data = parse_statement(_income(activity))
    assert data.dividends[0].security_name == "9 SAMPLE ISSUER DE CO"
    assert data.dividends[0].cusip == "99972M107"
    assert data.withholdings[0].security_name == "9 SAMPLE ISSUER DE CO"


def test_section_with_no_rows_refuses() -> None:
    assert _DIV_ROW in _ACTIVITY and _DIV_TOTAL in _ACTIVITY
    activity = _ACTIVITY.replace(_DIV_ROW, "").replace(
        _DIV_TOTAL, "Total Dividends, Interest & Other Income $0.00\n"
    )
    with pytest.raises(ParseError, match=r"present but has no rows"):
        parse_statement(_income(activity))


def test_duplicate_dividend_section_refuses() -> None:
    with pytest.raises(ParseError, match=r"expected exactly one .* section"):
        parse_statement(_income("Dividends, Interest & Other Income\n" + _ACTIVITY))


def test_txn_date_out_of_period_refuses() -> None:
    # 05/26 is outside the Feb-Mar period. The dividend table is read first, so its
    # row refuses (#1041).
    assert "03/05 EXAMPLE" in _ACTIVITY
    _assert_refusal(
        _refusal(_income(_ACTIVITY.replace("03/05 EXAMPLE", "05/26 EXAMPLE"))),
        section=_SOURCE,
        column="Dividends, Interest & Other Income: date",
        row_index=0,
        message=_TXN_DATE_OUTSIDE,
        absent=("05/26", "2026-05-26", "2026-02-01", "2026-03-31"),
    )


def test_ambiguous_txn_date_refuses() -> None:
    # A >1-year period in which 12/15 falls in BOTH the start and end year.
    assert "03/05" in _ACTIVITY
    text = _income(
        _ACTIVITY.replace("03/05", "12/15"), period="December 1, 2025 - December 31, 2026"
    )
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Dividends, Interest & Other Income: date",
        row_index=0,
        message=_TXN_DATE_OUTSIDE,
        absent=("12/15", "2025-12-15", "2026-12-15", "2025-12-01", "2026-12-31"),
    )


def test_invalid_txn_date_refuses() -> None:
    # 02/30 is not a calendar date in any candidate year.
    assert "03/05" in _ACTIVITY
    _assert_refusal(
        _refusal(_income(_ACTIVITY.replace("03/05", "02/30"))),
        section=_SOURCE,
        column="Dividends, Interest & Other Income: date",
        row_index=0,
        message=_TXN_DATE_OUTSIDE,
        absent=("02/30", "2026-02-01", "2026-03-31"),
    )


@pytest.mark.parametrize("row", [_DIV_ROW, _WHT_ROW, _MMKT_ROW], ids=["dividends", "taxes", "core"])
@pytest.mark.parametrize("page_break", ["", "Synthetic Marker Q\f"], ids=["same-page", "next-page"])
def test_espp_you_bought_inside_an_income_section_refuses(row: str, page_break: str) -> None:
    # An ESPP stock "You Bought" row placed INSIDE an income section's slice (a dated
    # row that is not that section's row) must refuse via the row-count guard —
    # proving the section regexes genuinely reject the ESPP format (#1088). The row is
    # in Fidelity's layout, footnote marker first (#1149).
    espp = _purchase_row(day="03/02")
    assert row in _ACTIVITY
    activity = _ACTIVITY.replace(row, row + page_break + espp)
    section = {
        _DIV_ROW: "Dividends, Interest & Other Income",
        _WHT_ROW: "Taxes Withheld",
        _MMKT_ROW: "Core Fund Activity",
    }[row]
    _assert_refusal(
        _refusal(_income(activity)),
        section=_SOURCE,
        column=None,
        row_index=0,
        message=f"{section} section has an unsupported or malformed row (2 dated rows, 1 parsed)",
        absent=_privacy_values(activity),
    )


def test_orphan_total_without_header_refuses() -> None:
    # A 'Total' line whose header is missing/displaced must refuse, not silently drop
    # the section's income (here the dividend header is pushed off line-start).
    activity = _ACTIVITY.replace(
        "Dividends, Interest & Other Income\n", "SUMMARY Dividends, Interest & Other Income\n"
    )
    with pytest.raises(ParseError, match=r"present without its header"):
        parse_statement(_income(activity))


def test_orphan_rows_without_header_or_total_refuses() -> None:
    # Income rows present but BOTH the header and the 'Total' missing/displaced must
    # refuse, not be silently dropped.
    activity = (
        "SUMMARY Dividends, Interest & Other Income\n"
        + _DIV_ROW
        + "Subtotal SUMMARY Dividends $1.46\n"
    )
    with pytest.raises(ParseError, match=r"present without its header"):
        parse_statement(_income(activity))


def test_subtractions_taxes_withheld_dash_line_not_a_header() -> None:
    # The "Subtractions" box line "Taxes Withheld  -  -" (no withholding that period)
    # must NOT be taken as a malformed Taxes Withheld section header, and its dash
    # agrees with the absent section (#903).
    body = _PRICED_BODY + "Subtractions\n  Taxes Withheld   -   -\n"
    data = parse_statement(_statement("STOCK PLAN SERVICES REPORT", _DIV_PERIOD, body=body))
    assert data.withholdings == ()


def test_dividend_row_after_total_refuses() -> None:
    # A supported row appearing AFTER the section total (outside the slice) must
    # refuse, not be silently dropped.
    extra = "03/15 ACME CORP 99966A101 Dividend Received - - $0.10\n"
    assert _DIV_TOTAL in _ACTIVITY
    with pytest.raises(ParseError, match=r"row found outside its section"):
        parse_statement(_income(_ACTIVITY.replace(_DIV_TOTAL, _DIV_TOTAL + extra)))


def test_federal_subtotal_mismatch_refuses() -> None:
    # A contradictory "Total Federal Taxes Withheld" must refuse.
    assert "Total Federal Taxes Withheld -0.37\n" in _ACTIVITY
    activity = _ACTIVITY.replace(
        "Total Federal Taxes Withheld -0.37\n", "Total Federal Taxes Withheld -9.99\n"
    )
    _assert_refusal(
        _refusal(_income(activity)),
        section=_SOURCE,
        column="Taxes Withheld: Total Federal Taxes Withheld",
        row_index=0,
        message="'Total Federal Taxes Withheld' does not equal the sum of the withholding rows"
        + _WHOLE_TABLE,
        absent=("9.99", "0.37"),
    )


def test_missing_federal_subtotal_refuses() -> None:
    assert "Total Federal Taxes Withheld -0.37\n" in _ACTIVITY
    activity = _ACTIVITY.replace("Total Federal Taxes Withheld -0.37\n", "")
    with pytest.raises(
        ParseError, match=r"expected exactly one 'Total Federal Taxes Withheld' line; found 0"
    ):
        parse_statement(_income(activity))


def test_malformed_federal_subtotal_refuses() -> None:
    assert "Total Federal Taxes Withheld -0.37\n" in _ACTIVITY
    activity = _ACTIVITY.replace(
        "Total Federal Taxes Withheld -0.37\n", "Total Federal Taxes Withheld -0.37 SEE NOTE\n"
    )
    with pytest.raises(ParseError, match=r"'Total Federal Taxes Withheld' line is malformed"):
        parse_statement(_income(activity))


def test_misplaced_federal_subtotal_refuses() -> None:
    # The Federal subtotal moved OUTSIDE the Taxes Withheld section (after the MMKT
    # section) must refuse — the in-section subtotal is then missing.
    assert "Total Federal Taxes Withheld -0.37\n" in _ACTIVITY
    activity = _ACTIVITY.replace("Total Federal Taxes Withheld -0.37\n", "")
    activity += "Total Federal Taxes Withheld -0.37\n"
    with pytest.raises(
        ParseError, match=r"expected exactly one 'Total Federal Taxes Withheld' line; found 0"
    ):
        parse_statement(_income(activity))


# Each income table's per-row refusals, on its SECOND row: located at row 1 of that
# table, with the table and field as ``column`` (#1041).
_ROW_2_DATE_ABSENT = ("05/26", "2026-05-26", "2026-02-01", "2026-03-31")


@pytest.mark.parametrize(
    ("row", "bad_row", "column", "message", "absent"),
    [
        pytest.param(
            _DIV_ROW_2,
            _DIV_ROW_2.replace(" 1.00\n", " 0.00\n"),
            "Dividends, Interest & Other Income: amount",
            "Dividends, Interest & Other Income: amount must be > 0",
            (),
            id="dividend-amount",
        ),
        pytest.param(
            _DIV_ROW_2,
            _DIV_ROW_2.replace("03/20", "05/26"),
            "Dividends, Interest & Other Income: date",
            _TXN_DATE_OUTSIDE,
            _ROW_2_DATE_ABSENT,
            id="dividend-date",
        ),
        pytest.param(
            _WHT_ROW_2,
            _WHT_ROW_2.replace("-0.25", "0.25"),
            "Taxes Withheld: amount",
            "Non-Resident Tax must be a negative (withheld) amount",
            ("0.25",),
            id="withholding-sign",
        ),
        pytest.param(
            _WHT_ROW_2,
            _WHT_ROW_2.replace("03/20", "05/26"),
            "Taxes Withheld: date",
            _TXN_DATE_OUTSIDE,
            _ROW_2_DATE_ABSENT,
            id="withholding-date",
        ),
        *[
            pytest.param(
                _MMKT_ROW_2,
                _MMKT_ROW_2.replace(old, new),
                f"Core Fund Activity: {field}",
                f"Core Fund Activity: {field} must be > 0",
                (),
                id=f"mmkt-{field}",
            )
            for field, old, new in (
                ("quantity", " 0.750 ", " 0.000 "),
                ("price", " 1.0000 ", " 0.0000 "),
                ("amount", " 0.75 ", " 0.00 "),
                ("balance", " 1.84\n", " 0.00\n"),
            )
        ],
        # A distinctive quantity, printed as the statement's 3-dp quantity token.
        pytest.param(
            _MMKT_ROW_2,
            _MMKT_ROW_2.replace(" 0.750 ", " 987,654.321 "),
            "Core Fund Activity: amount",
            _MMKT_AMOUNT_VS_QTY_PRICE,
            ("987,654.321", "987654.321", "0.75"),
            id="mmkt-amount-vs-quantity-x-price",
        ),
        pytest.param(
            _MMKT_ROW_2,
            _MMKT_ROW_2.replace(" 1.84\n", " 9.99\n"),
            "Core Fund Activity: balance",
            _RUNNING_BALANCE,
            ("9.99", "1.84", "1.09"),
            id="mmkt-running-balance",
        ),
        pytest.param(
            _MMKT_ROW_2,
            _MMKT_ROW_2.replace("03/20", "05/26"),
            "Core Fund Activity: date",
            _TXN_DATE_OUTSIDE,
            _ROW_2_DATE_ABSENT,
            id="mmkt-date",
        ),
    ],
)
def test_second_income_row_refusal_is_located_at_row_1(
    row: str, bad_row: str, column: str, message: str, absent: tuple[str, ...]
) -> None:
    assert bad_row != row
    exc = _refusal(_two_row_income(_TWO_ROW_ACTIVITY.replace(row, bad_row)))
    _assert_refusal(
        exc, section=_SOURCE, column=column, row_index=1, message=message, absent=absent
    )


# Two activity dates (#1041): 03/05 reconciles (1.46 - 0.37 = 1.09 swept), but on
# 03/20 two dividends (4.32 + 1.23 = 5.55) are swept as only 5.50.
_SWEEP_ON_0320 = "03/20 CASH You Bought FID TREASURY ONLY MMKT FUND CL OUS 5.500 1.0000 5.50 6.59\n"
_SWEEP_MISMATCH_ACTIVITY = (
    "Dividends, Interest & Other Income\n"
    "Date Security Name CUSIP Description Quantity Price Amount\n"
    + _DIV_ROW
    + "03/20 EXAMPLE CORP 99999E104 Dividend Received - - 4.32\n"
    "03/20 SAMPLE ISSUER BN INC 99993A108 Dividend Received - - 1.23\n"
    "Total Dividends, Interest & Other Income $7.01\n"
    "Taxes Withheld\n"
    "Date Security Description Amount\n"
    + _WHT_ROW
    + "Total Federal Taxes Withheld -0.37\n"
    + _WHT_TOTAL
    + "Core Fund Activity\n"
    "Date Type Transaction Description Quantity Price Amount Balance\n"
    + _MMKT_ROW
    + _SWEEP_ON_0320
    + "Total Core Fund Activity $6.59\n"
)


def test_daily_sweep_refusal_names_the_failing_dates_rows() -> None:
    def statement(activity: str, core_ending: str) -> str:
        return _income(
            activity, core_ending=Decimal(core_ending), summary=_summary("7.01", "-0.37")
        )

    # The same statement sweeping the full 5.55 on 03/20 parses: only the sweep fails.
    balanced = _SWEEP_MISMATCH_ACTIVITY.replace(
        _SWEEP_ON_0320, _SWEEP_ON_0320.replace("5.500 1.0000 5.50 6.59", "5.550 1.0000 5.55 6.64")
    ).replace("Total Core Fund Activity $6.59", "Total Core Fund Activity $6.64")
    assert len(parse_statement(statement(balanced, "6.64")).dividends) == 3

    exc = _refusal(statement(_SWEEP_MISMATCH_ACTIVITY, "6.59"))
    # 03/20 is the second activity date in sorted order; its three rows are named by
    # table and position, without the date or the amounts.
    _assert_refusal(
        exc,
        section=_SOURCE,
        column="Income activity: daily sweep",
        row_index=1,
        message=_SWEEP_MISMATCH
        + "Dividends, Interest & Other Income rows 1 and 2; Core Fund Activity row 1",
        absent=(
            *("2026-03-20", "03/20", "3/20", "03-20", "March 20", "Mar 20", "20 Mar"),
            *("2026-03-05", "03/05", "2026"),
            *("4.32", "1.23", "5.55", "5.50", "5.5", "0.05"),
        ),
    )


# --- period summaries vs the detail sections (#903) -------------------------------

_WHT_SECTION = (
    "Taxes Withheld\n"
    "Date Security Description Amount\n"
    + _WHT_ROW
    + "Total Federal Taxes Withheld -0.37\n"
    + _WHT_TOTAL
)


def test_summaries_matching_their_sections_parse() -> None:
    data = parse_statement(_income(summary=_summary("1.46", "-0.37")))
    assert [d.gross_amount_usd for d in data.dividends] == [Decimal("1.46")]
    assert [w.amount_usd for w in data.withholdings] == [Decimal("-0.37")]


# The three ``_check_summary`` refusals, for each summary (#1041): the summary is
# named by its line and its column, never by the amounts it compared.
_INCOME_SUMMARY_TOTAL = "the 'Income Summary' Total"
_WHT_SUMMARY_LINE = "the account-summary 'Taxes Withheld' line"
_DIV_SECTION = "'Dividends, Interest & Other Income'"


def test_income_summary_without_its_dividend_section_refuses() -> None:
    # The detail page is missing: every income section is gone, but the summaries
    # still show the period's 1.46 dividend. Parsing it as zero would drop the income.
    _assert_refusal(
        _refusal(_income("")),
        section=_SOURCE,
        column="Income Summary: Total",
        row_index=0,
        message=f"{_INCOME_SUMMARY_TOTAL} shows a nonzero amount this period, but the statement "
        f"has no {_DIV_SECTION} section" + _WHOLE_DOCUMENT,
        absent=("1.46",),
    )


def test_taxes_withheld_summary_without_its_section_refuses() -> None:
    assert _WHT_SECTION in _ACTIVITY
    _assert_refusal(
        _refusal(_income(_ACTIVITY.replace(_WHT_SECTION, ""))),
        section=_SOURCE,
        column="account summary: Taxes Withheld",
        row_index=0,
        message=f"{_WHT_SUMMARY_LINE} shows a nonzero amount this period, but the statement "
        "has no 'Taxes Withheld' section" + _WHOLE_DOCUMENT,
        absent=("0.37",),
    )


def test_year_to_date_only_summaries_without_sections_parse() -> None:
    # Income earlier in the year: both summaries print, This Period is a dash.
    summary = _account_summary("-", ytd="-0.37") + _income_summary("-", ytd="1.46")
    data = parse_statement(_income("", summary=summary))
    assert (data.dividends, data.withholdings, data.mmkt_buys) == ((), (), ())


_INCOME_SUMMARY_UNEQUAL = (
    f"{_INCOME_SUMMARY_TOTAL} this period does not equal the {_DIV_SECTION} section's 'Total'"
    + _WHOLE_DOCUMENT
)
_WHT_SUMMARY_UNEQUAL = (
    f"{_WHT_SUMMARY_LINE} this period does not equal the 'Taxes Withheld' section's 'Total'"
    + _WHOLE_DOCUMENT
)


@pytest.mark.parametrize(
    ("summary", "column", "message", "absent"),
    [
        pytest.param(
            _summary("1.47", "-0.37"),
            "Income Summary: Total",
            _INCOME_SUMMARY_UNEQUAL,
            ("1.47", "1.46"),
            id="income-summary-one-cent-off",
        ),
        pytest.param(
            _summary("1.46", "-0.38"),
            "account summary: Taxes Withheld",
            _WHT_SUMMARY_UNEQUAL,
            ("0.38", "0.37"),
            id="taxes-withheld-one-cent-off",
        ),
        pytest.param(
            _summary("-", "-0.37"),
            "Income Summary: Total",
            _INCOME_SUMMARY_UNEQUAL,
            ("1.46",),
            id="income-summary-dash",
        ),
        pytest.param(
            _summary("0.00", "-0.37"),
            "Income Summary: Total",
            _INCOME_SUMMARY_UNEQUAL,
            ("1.46",),
            id="income-summary-zero",
        ),
        pytest.param(
            _summary("1.46", "-"),
            "account summary: Taxes Withheld",
            _WHT_SUMMARY_UNEQUAL,
            ("0.37",),
            id="taxes-withheld-dash",
        ),
        pytest.param(
            _summary("1.46", "0.00"),
            "account summary: Taxes Withheld",
            _WHT_SUMMARY_UNEQUAL,
            ("0.37",),
            id="taxes-withheld-zero",
        ),
        pytest.param(
            _account_summary(),
            "Income Summary: Total",
            f"{_INCOME_SUMMARY_TOTAL} is missing, but the statement has a {_DIV_SECTION} "
            "section" + _WHOLE_DOCUMENT,
            ("1.46",),
            id="income-summary-missing",
        ),
        pytest.param(
            _income_summary(),
            "account summary: Taxes Withheld",
            f"{_WHT_SUMMARY_LINE} is missing, but the statement has a 'Taxes Withheld' section"
            + _WHOLE_DOCUMENT,
            ("0.37",),
            id="taxes-withheld-missing",
        ),
    ],
)
def test_summary_disagreeing_with_its_section_refuses(
    summary: str, column: str, message: str, absent: tuple[str, ...]
) -> None:
    _assert_refusal(
        _refusal(_income(summary=summary)),
        section=_SOURCE,
        column=column,
        row_index=0,
        message=message,
        absent=absent,
    )


# The default block's lines, for the strictness cases below to edit.
_IS_TITLE, _IS_HEADER, _IS_TAXABLE_ROW, _IS_DIVIDENDS_ROW, _IS_TOTAL_ROW = (
    _income_summary().splitlines(keepends=True)
)
assert _IS_DIVIDENDS_ROW in _summary() and _IS_TOTAL_ROW in _summary()


@pytest.mark.parametrize(
    ("summary", "message"),
    [
        pytest.param(
            _summary() + "  Taxes Withheld   -0.37   -0.37\n",
            r"expected at most one account-summary 'Taxes Withheld' line; found 2",
            id="taxes-withheld-twice",
        ),
        pytest.param(
            _income_summary() + "  Taxes Withheld   -0.37\n",
            r"account-summary 'Taxes Withheld' line is malformed",
            id="taxes-withheld-one-column",
        ),
        pytest.param(
            _income_summary() + "  Taxes Withheld   -0.37   -0.37   NOTE\n",
            r"account-summary 'Taxes Withheld' line is malformed",
            id="taxes-withheld-trailing-token",
        ),
        pytest.param(
            _income_summary() + "Left column text   Taxes Withheld   -0.37   -0.37\n",
            r"account-summary 'Taxes Withheld' line is malformed",
            id="taxes-withheld-displaced",
        ),
        *[
            pytest.param(
                _income_summary() + f"left {row}\n",
                r"account-summary 'Taxes Withheld' line is malformed",
                id=f"taxes-withheld-{case}",
            )
            for case, row in (
                ("displaced-federal", "Federal Taxes Withheld -0.37 -0.37"),
                ("displaced-total", "Total Taxes Withheld -$0.37 -$0.37"),
                ("form-feed-separator", "Taxes Withheld\f-0.37 -0.37"),
                ("colon", "Taxes Withheld: -0.37 -0.37"),
                ("intervening-text", "Taxes Withheld NOTE -0.37 -0.37"),
                ("no-separator", "Taxes Withheld-0.37 -0.37"),
                ("unicode-amount", "Taxes Withheld \u0660.\u0663\u0667 \u0660.\u0663\u0667"),
            )
        ],
        pytest.param(
            _summary() + _income_summary(),
            r"expected at most one 'Income Summary' block; found 2",
            id="income-summary-twice",
        ),
        pytest.param(
            _summary().replace(_IS_TITLE, _IS_TITLE.replace("Summary", "Summary (continued)")),
            r"an 'Income Summary' title line is malformed",
            id="income-summary-title-trailing-text",
        ),
        *[
            pytest.param(
                _summary().replace(_IS_HEADER, header),
                r"'Income Summary' must have a This Period / Year-to-Date column header",
                id=f"income-summary-header-{case}",
            )
            for case, header in (
                ("missing", ""),
                ("reversed", f"{_IS_PAD}Year-to-Date   This Period\n"),
                ("trailing-text", _IS_HEADER.rstrip("\n") + " NOTE\n"),
            )
        ],
        pytest.param(
            _summary().replace(
                _IS_TOTAL_ROW, f"{_IS_PAD}   Interest   0.10   0.10\n" + _IS_TOTAL_ROW
            ),
            r"its 'Total' row is missing, malformed or follows an unsupported row",
            id="income-summary-unsupported-row",
        ),
        pytest.param(
            _summary().replace(_IS_DIVIDENDS_ROW, ""),
            r"its 'Dividends' row is missing, malformed or follows an unsupported row",
            id="income-summary-missing-row",
        ),
        pytest.param(
            _summary().replace(_IS_TOTAL_ROW, ""),
            r"its 'Total' row is missing, malformed or follows an unsupported row",
            id="income-summary-missing-total",
        ),
        pytest.param(
            _summary().replace(_IS_TOTAL_ROW, f"{_IS_PAD}Total   $1.46   $1.46   NOTE\n"),
            r"its 'Total' row is missing, malformed or follows an unsupported row",
            id="income-summary-malformed-row",
        ),
        pytest.param(
            _summary().replace(_IS_TITLE, ""),
            r"an 'Income Summary' row is present without its title",
            id="income-summary-without-title",
        ),
        pytest.param(
            _summary() + "   Dividends   1.46   1.46\n",
            r"an 'Income Summary' row appears outside its block",
            id="income-summary-row-outside-block",
        ),
        *[
            pytest.param(
                _summary() + f"{row}\n",
                r"an 'Income Summary' row appears outside its block",
                id=f"income-summary-outside-row-{case}",
            )
            for case, row in (
                ("colon", "Dividends: 9.00 9.00"),
                ("intervening-text", "Dividends NOTE 9.00 9.00"),
                ("comma", "Dividends, 9.00 9.00"),
            )
        ],
    ],
)
def test_malformed_summary_refuses(summary: str, message: str) -> None:
    with pytest.raises(ParseError, match=message):
        parse_statement(_income(summary=summary))


@pytest.mark.parametrize(
    ("dividends_row", "absent"),
    [
        pytest.param(
            f"{_IS_PAD}   Dividends   1.42   1.46\n", ("1.42", "1.46"), id="this-period-disagrees"
        ),
        pytest.param(
            f"{_IS_PAD}   Dividends   1.46   1.47\n", ("1.47", "1.46"), id="ytd-disagrees"
        ),
    ],
)
def test_income_summary_rows_disagreeing_refuse(
    dividends_row: str, absent: tuple[str, ...]
) -> None:
    # The rows are named by label, never by the amounts they print (#1041).
    summary = _summary().replace(_IS_DIVIDENDS_ROW, dividends_row)
    _assert_refusal(
        _refusal(_income(summary=summary)),
        section=_SOURCE,
        column="Income Summary",
        row_index=0,
        message="the 'Income Summary' rows Taxable, Dividends and Total disagree (this period or "
        "year to date)" + _WHOLE_TABLE,
        absent=absent,
    )


@pytest.mark.parametrize("columns", ["", "N/A N/A", "€NaN €NaN"])
def test_withholding_summary_with_unrecognizable_columns_refuses(columns: str) -> None:
    # No detail: a damaged summary must not be mistaken for an absent, zero summary.
    summary = f"left Taxes Withheld {columns}\n"
    with pytest.raises(ParseError, match=r"account-summary 'Taxes Withheld' line is malformed"):
        parse_statement(_income("", summary=summary))


@pytest.mark.parametrize(
    "row",
    [
        "Taxable\f$1.46 $1.46",
        "Dividends$1.46 $1.46",
        "Dividends1.46 1.46",
        "Taxable \u0661.\u0664\u0666 \u0661.\u0664\u0666",
        "Dividends \u0661.\u0664\u0666 \u0661.\u0664\u0666",
    ],
)
def test_malformed_income_row_without_title_refuses(row: str) -> None:
    with pytest.raises(ParseError, match=r"'Income Summary' row is present without its title"):
        parse_statement(_income("", summary=row + "\n"))


@pytest.mark.parametrize(
    "amount", ["\u0661.\u0660\u0660", "+0.10", "\u22120.10", "$-0.10", "€0.10", "($0.10)"]
)
def test_unsupported_income_row_with_malformed_numeric_columns_refuses(amount: str) -> None:
    summary = _summary().replace(
        _IS_TAXABLE_ROW, f"{_IS_PAD}Interest {amount} {amount}\n" + _IS_TAXABLE_ROW
    )
    with pytest.raises(ParseError, match=r"'Income Summary'.*its 'Taxable' row"):
        parse_statement(_income(summary=summary))


def test_reversed_income_header_cannot_hide_this_period_income() -> None:
    summary = _income_summary("-", ytd="1.46").replace(
        _IS_HEADER, f"{_IS_PAD}Year-to-Date   This Period\n"
    )
    with pytest.raises(ParseError, match=r"'Income Summary'.*column header"):
        parse_statement(_income("", summary=summary))


@pytest.mark.parametrize(
    ("label", "header"),
    [
        ("Taxable", "Year-to-Date   This Period"),
        ("   Dividends", "This Period   Year-to-Date"),
        ("Total", "Year-to-Date   This Period"),
    ],
    ids=["before-taxable", "before-dividends", "before-total"],
)
def test_second_income_header_refuses(label: str, header: str) -> None:
    marker = _IS_PAD + label
    summary = _income_summary("-", ytd="1.46").replace(marker, f"{_IS_PAD}{header}\n" + marker)
    with pytest.raises(ParseError, match=r"'Income Summary'.*column header"):
        parse_statement(_income("", summary=summary))


@pytest.mark.parametrize("row_kind", ["stock", "core", "income", "sweep"])
@pytest.mark.parametrize(
    "name",
    [
        "Taxable Growth Corp",
        "Dividends Growth Corp",
        "ABC Taxes Withheld Corp",
        "Income Summary Corp",
    ],
)
def test_security_names_do_not_count_as_summary_labels(row_kind: str, name: str) -> None:
    text = _income()
    if row_kind == "stock":
        text = text.replace("EXAMPLE CORP (XESP)", f"{name} (XESP)")
    elif row_kind == "core":
        text = text.replace("FID TREASURY ONLY MMKT FUND CL   unavailable", f"{name}   unavailable")
    elif row_kind == "income":
        text = text.replace("03/05 EXAMPLE CORP", f"03/05 {name}")
    else:
        text = text.replace(
            "FID TREASURY ONLY MMKT FUND CL OUS 1.090",
            f"FID TREASURY ONLY MMKT FUND CL OUS {name} 1.090",
        )
    assert text != _income()
    data = parse_statement(text)
    assert data.holdings[0].ending_market_value_usd == Decimal("653.36")
    assert [d.gross_amount_usd for d in data.dividends] == [Decimal("1.46")]
    assert [w.amount_usd for w in data.withholdings] == [Decimal("-0.37")]
    assert [m.amount_usd for m in data.mmkt_buys] == [Decimal("1.09")]


def test_supported_holding_row_inside_summary_still_refuses() -> None:
    # Known rows are excluded only from loose counts, not the block's row selection.
    assert _XESP_ROW in _income() and _IS_DIVIDENDS_ROW in _income()
    text = (
        _income().replace(_XESP_ROW, "").replace(_IS_DIVIDENDS_ROW, _XESP_ROW + _IS_DIVIDENDS_ROW)
    )
    with pytest.raises(ParseError, match=r"'Income Summary'.*its 'Dividends' row"):
        parse_statement(text)


assert "$1.46" in _summary() and "-0.37" in _summary()


@pytest.mark.parametrize(
    "summary",
    [
        _summary().replace("$1.46", "1.46").replace("-0.37", "-$0.37"),
        "\f" + _summary().replace("\n", "\n\f"),
    ],
    ids=["bare-dividends-dollar-withholding", "page-starts"],
)
def test_summary_money_forms_and_page_starts_parse(summary: str) -> None:
    assert parse_statement(_income(summary=summary)) == parse_statement(_income())


def test_income_summary_title_and_header_may_share_lines_with_left_column_text() -> None:
    # A left-hand footnote runs beside the block: it shares the title's and the column
    # header's lines, and fills a line of its own between them.
    left = "* Left-column footnote text"
    summary = (
        _summary()
        .replace(_IS_TITLE, f"{left}{' ' * 33}Income Summary\n")
        .replace(_IS_HEADER, f"  continues on this line\n{left:<80}This Period   Year-to-Date\n")
    )
    data = parse_statement(_income(summary=summary))
    assert [d.gross_amount_usd for d in data.dividends] == [Decimal("1.46")]


def test_income_header_below_the_taxable_row_refuses() -> None:
    assert _IS_TAXABLE_ROW in _summary()
    summary = (
        _summary().replace(_IS_HEADER, "").replace(_IS_TAXABLE_ROW, _IS_TAXABLE_ROW + _IS_HEADER)
    )
    with pytest.raises(ParseError, match=r"'Income Summary'.*column header"):
        parse_statement(_income(summary=summary))


def test_second_income_header_on_the_title_line_refuses() -> None:
    summary = _summary().replace(_IS_TITLE, f"This Period   Year-to-Date{' ' * 30}Income Summary\n")
    with pytest.raises(ParseError, match=r"'Income Summary'.*column header"):
        parse_statement(_income(summary=summary))


def test_dividends_label_with_an_amount_refuses() -> None:
    # The account summary carries no dividend line (the Income Summary block holds the
    # period's dividends), so a line starting with the section label and carrying an
    # amount is a malformed header, not a summary line to skip.
    body = _PRICED_BODY + "Additions\n  Dividends, Interest & Other Income   1.46   1.46\n"
    with pytest.raises(
        ParseError, match=r"Dividends, Interest & Other Income section header is malformed"
    ):
        parse_statement(_statement("STOCK PLAN SERVICES REPORT", _DIV_PERIOD, body=body))


@pytest.mark.parametrize(
    ("report_line", "period"),
    [
        pytest.param("2025 YEAR-END INVESTMENT REPORT", _YEAR_END_PERIOD, id="year-end-parses"),
        pytest.param("STOCK PLAN SERVICES REPORT", _YEAR_END_PERIOD, id="sps-refuses"),
    ],
)
def test_year_end_report_skips_the_summary_check(report_line: str, period: str) -> None:
    # A year-end report prints no detail sections, so its yearly summaries (a
    # one-column Taxes Withheld line, an Income Summary) have nothing to match and
    # are not read. The same text as a Stock Plan Services report refuses.
    body = _UNPRICED_BODY + "  Taxes Withheld   -0.37\n" + _income_summary()
    text = _statement(report_line, period, body=body)
    if report_line == "STOCK PLAN SERVICES REPORT":
        _assert_refusal(
            _refusal(text),
            section=_SOURCE,
            column="Income Summary: Total",
            row_index=0,
            message=f"{_INCOME_SUMMARY_TOTAL} shows a nonzero amount this period, but the "
            f"statement has no {_DIV_SECTION} section" + _WHOLE_DOCUMENT,
            absent=("1.46",),
        )
        return
    data = parse_statement(text)
    assert (data.report_type, data.dividends, data.withholdings) == (ReportType.YEAR_END, (), ())


# --- This Period vs Year-to-Date on a statement from January 1 (#1091) ------------

# Distinct privacy markers: the holder, and the participant number on every page.
_YTD_HOLDER = "Synthetic Holder Two"
_YTD_PNO = "SYNTHETICMARKERQ"
_YTD_ABSENT = (_YTD_HOLDER, _YTD_PNO, "500.00", "125.00", "9.99", "2.50")
_SAME_DAYS = (
    "'s This Period and Year-to-Date columns differ, but the statement starts on "
    "January 1, so both cover the same days"
)
_INCOME_YTD_DIFFERS = _INCOME_SUMMARY_TOTAL + _SAME_DAYS + _WHOLE_TABLE
_WHT_YTD_DIFFERS = _WHT_SUMMARY_LINE + _SAME_DAYS + _WHOLE_DOCUMENT
_INCOME_WITHOUT_SECTION = (
    f"{_INCOME_SUMMARY_TOTAL} shows a nonzero amount this period, but the statement has no "
    f"{_DIV_SECTION} section" + _WHOLE_DOCUMENT
)
_WHT_WITHOUT_SECTION = (
    f"{_WHT_SUMMARY_LINE} shows a nonzero amount this period, but the statement has no "
    "'Taxes Withheld' section" + _WHOLE_DOCUMENT
)
_JAN_TO_MAR = "January 1, 2026 - March 31, 2026"
_JAN_1_ONLY = "January 1, 2026 - January 1, 2026"
_INCOME_COLUMN = "Income Summary: Total"
_WHT_COLUMN = "account summary: Taxes Withheld"
assert _ACTIVITY.count("03/05 ") == 3
_JAN_ACTIVITY = _ACTIVITY.replace("03/05 ", "01/05 ")


def _marked(text: str) -> str:
    """``text`` with the privacy markers as its holder and participant number."""
    assert text.count("TAX PAYER") == 1 and text.count(_PNO) == 3
    return text.replace("TAX PAYER", _YTD_HOLDER).replace(_PNO, _YTD_PNO)


def _no_sections(summary: str, period: str = _JAN_2026) -> str:
    """An unpriced Stock Plan Services report with no detail sections."""
    return _marked(_statement("STOCK PLAN SERVICES REPORT", period, body=_UNPRICED_BODY + summary))


def _january_income(summary: str) -> str:
    """A priced January report whose detail sections total 1.46 and -0.37."""
    return _marked(_income(_JAN_ACTIVITY, period=_JAN_2026, summary=summary))


@pytest.mark.parametrize(
    ("text", "column", "message"),
    [
        pytest.param(
            _no_sections(_income_summary("-", ytd="500.00")),
            _INCOME_COLUMN,
            _INCOME_YTD_DIFFERS,
            id="income-year-to-date-only",
        ),
        pytest.param(
            _no_sections(_account_summary("-", ytd="-125.00") + _income_summary("-")),
            _WHT_COLUMN,
            _WHT_YTD_DIFFERS,
            id="withholding-year-to-date-only",
        ),
        pytest.param(
            _january_income(
                _account_summary("-0.37", ytd="-2.50") + _income_summary("1.46", ytd="9.99")
            ),
            _INCOME_COLUMN,
            _INCOME_YTD_DIFFERS,
            id="sections-match-this-period",
        ),
        pytest.param(
            _no_sections(_income_summary("-", ytd="500.00"), _JAN_TO_MAR),
            _INCOME_COLUMN,
            _INCOME_YTD_DIFFERS,
            id="january-to-march",
        ),
        pytest.param(
            _no_sections(_account_summary("-", ytd="-125.00"), _JAN_1_ONLY),
            _WHT_COLUMN,
            _WHT_YTD_DIFFERS,
            id="one-day",
        ),
        # The existing summary checks keep their precedence over the new ones.
        pytest.param(
            _no_sections(_account_summary("-", ytd="-125.00") + _income_summary("1.46")),
            _INCOME_COLUMN,
            _INCOME_WITHOUT_SECTION,
            id="existing-income-check-first",
        ),
        pytest.param(
            _no_sections(_account_summary("-0.37") + _income_summary("-", ytd="500.00")),
            _WHT_COLUMN,
            _WHT_WITHOUT_SECTION,
            id="existing-withholding-check-first",
        ),
    ],
)
def test_summary_columns_differing_from_january_1_refuse(
    text: str, column: str, message: str
) -> None:
    # From January 1 to a day in the same year, both columns cover the same days.
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column=column,
        row_index=0,
        message=message,
        absent=_YTD_ABSENT,
    )


@pytest.mark.parametrize(
    ("text", "dividends", "withholdings"),
    [
        pytest.param(
            _no_sections(_account_summary("-") + _income_summary("-")), [], [], id="dashes"
        ),
        pytest.param(
            _no_sections(_account_summary("-", ytd="0.00") + _income_summary("-", ytd="0.00")),
            [],
            [],
            id="dash-and-zero",
        ),
        pytest.param(
            _january_income(_summary()),
            [Decimal("1.46")],
            [Decimal("-0.37")],
            id="equal-amounts",
        ),
        # The rule covers only a period that starts on January 1 and ends that year.
        pytest.param(
            _no_sections(
                _account_summary("-", ytd="-125.00") + _income_summary("-", ytd="500.00"),
                "January 1, 2026 - January 31, 2027",
            ),
            [],
            [],
            id="ends-the-next-year",
        ),
        pytest.param(
            _no_sections(
                _account_summary("-", ytd="-125.00") + _income_summary("-", ytd="500.00"),
                "February 1, 2026 - February 28, 2026",
            ),
            [],
            [],
            id="starts-february-1",
        ),
        pytest.param(
            _marked(
                _statement(
                    "2025 YEAR-END INVESTMENT REPORT",
                    _YEAR_END_PERIOD,
                    body=_UNPRICED_BODY + _income_summary("-", ytd="500.00"),
                )
            ),
            [],
            [],
            id="year-end",
        ),
    ],
)
def test_agreeing_or_uncovered_summary_columns_parse(
    text: str, dividends: list[Decimal], withholdings: list[Decimal]
) -> None:
    data = parse_statement(text)
    assert data.account_holder == _YTD_HOLDER
    assert data.participant_no == _YTD_PNO
    assert [d.gross_amount_usd for d in data.dividends] == dividends
    assert [w.amount_usd for w in data.withholdings] == withholdings


def test_wrapped_withholding_name_rejoins_to_match_its_dividend() -> None:
    """A withholding row's security name wraps just as the dividend row's does.

    Load-bearing: the withholding is matched to its dividend on
    (date, security_name), so a name truncated on one side and not the other
    silently fails to pair and refuses at filing time. Balanced as
    1.12 gross - 0.28 withheld = 0.84 swept.
    """
    activity = (
        "Dividends, Interest & Other Income\n"
        "(Includes dividend reinvestment)\n"
        "Date Security Name CUSIP Description Quantity Price Amount\n"
        "06/30 FID TREASURY ONLY MMKT FUND CL 99971X104 Dividend Received - - $1.12\n"
        "OUS\n"
        "Total Dividends, Interest & Other Income $1.12\n"
        "Taxes Withheld\n"
        "Date Security Description Amount\n"
        "06/30 FID TREASURY ONLY MMKT FUND CL Non-Resident Tax -$0.28\n"
        "OUS\n"
        "Total Federal Taxes Withheld -0.28\n"
        "Total Taxes Withheld -$0.28\n"
        "Core Fund Activity\n"
        "Date Type Transaction Description Quantity Price Amount Balance\n"
        "06/30 CASH Reinvestment FID TREASURY ONLY MMKT FUND CL OUS 0.840 $1.0000 $0.84 $0.84\n"
        "Total Core Fund Activity $0.84\n"
    )
    data = parse_statement(
        _income(
            activity,
            period="May 1, 2026 - June 30, 2026",
            core_ending=Decimal("0.84"),
            summary=_summary("1.12", "-0.28"),
        )
    )
    (div,) = data.dividends
    (wht,) = data.withholdings
    assert div.security_name == "FID TREASURY ONLY MMKT FUND CL OUS"
    # The pairing key downstream is (date, security_name) — these MUST be equal.
    assert wht.security_name == div.security_name
    (buy,) = data.mmkt_buys
    assert (div.gross_amount_usd, wht.amount_usd, buy.amount_usd) == (
        Decimal("1.12"),
        Decimal("-0.28"),
        Decimal("0.84"),
    )
    assert (buy.quantity, buy.price_usd) == (Decimal("0.840"), Decimal("1.0000"))


def test_unwrapped_withholding_name_is_unaffected() -> None:
    """The optional continuation must not swallow the Title-Case total that follows."""
    (wht,) = parse_statement(_income()).withholdings
    assert wht.security_name == "EXAMPLE CORP"


# --- dated and Total lines across the whole statement (#1088) ---------------------

_TOTAL_UNREAD = (
    "a 'Total' line belongs to no section, holding total or summary this parser reads"
    + _WHOLE_DOCUMENT
)
_ESPP_MALFORMED = "an ESPP purchase row is malformed" + _WHOLE_DOCUMENT
_UNREAD_ABSENT = (
    "Synthetic Holder Two",
    "Synthetic Marker Q",
    "Other Cash Activity",
    "500.00",
    "12/31",
)
_DIV_HEADER = "Dividends, Interest & Other Income\n"
# A line in a section's slice that the section's row pattern doesn't read (#1149).
_DIV_UNSUPPORTED = (
    "Dividends, Interest & Other Income section has an unsupported or malformed row "
    "(2 dated rows, 1 parsed)"
)


assert _DIV_TOTAL in _ACTIVITY and _ACTIVITY.startswith(_DIV_HEADER)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param(
            _dec31_unpriced(_OTHER_CASH_ACTIVITY), _DATED_UNREAD, id="block-unpriced-dec31"
        ),
        pytest.param(
            _income(_ACTIVITY + _OTHER_CASH_ACTIVITY), _DATED_UNREAD, id="block-priced-income"
        ),
        pytest.param(
            _income(_ACTIVITY.replace(_DIV_TOTAL, _DIV_TOTAL + _CASH_ROW)),
            _DATED_UNREAD,
            id="dated-row-between-sections",
        ),
        pytest.param(_dec31_unpriced(_CASH_TOTAL), _TOTAL_UNREAD, id="total-without-rows"),
        pytest.param(
            _dec31_unpriced("Total Other Cash Activity\n"), _TOTAL_UNREAD, id="cut-short-total"
        ),
        pytest.param(_dec31_unpriced("12/31\n"), _DATED_UNREAD, id="bare-date"),
        *[
            pytest.param(
                _dec31_unpriced(_CASH_ROW.replace("12/31 ", f"12/31{delimiter}")),
                _DATED_UNREAD,
                id=f"date-before-{kind}",
            )
            for kind, delimiter in (
                ("form-feed", "\f"),
                ("carriage-return", "\r"),
                ("vertical-tab", "\v"),
                ("nonbreaking-space", "\u00a0"),
            )
        ],
        pytest.param(
            _dec31_unpriced("Synthetic Marker Q\f" + _CASH_ROW),
            _DATED_UNREAD,
            id="date-after-inline-page-break",
        ),
        pytest.param(
            _dec31_unpriced("Synthetic Marker Q\f" + _CASH_TOTAL),
            _TOTAL_UNREAD,
            id="total-after-inline-page-break",
        ),
        # A bare date in a section's slice is one of its dated lines (#1149).
        pytest.param(
            _income(_ACTIVITY.replace(_DIV_HEADER, _DIV_HEADER + "12/31\n", 1)),
            _DIV_UNSUPPORTED,
            id="bare-date-after-the-dividend-header",
        ),
        # A wrapped name's second line isn't a row, so a date there isn't read either.
        pytest.param(
            _income(_ACTIVITY.replace(_DIV_ROW, _DIV_ROW + "12/31\n")),
            _DIV_UNSUPPORTED,
            id="bare-date-as-a-wrapped-name",
        ),
    ],
)
def test_unread_dated_or_total_line_refuses(text: str, message: str) -> None:
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column=None,
        row_index=0,
        message=message,
        absent=_UNREAD_ABSENT,
    )


def test_statements_without_the_unread_lines_parse() -> None:
    # The refusals above come from the added lines alone.
    assert parse_statement(_dec31_unpriced()).period_end == date(2025, 12, 31)
    assert [d.gross_amount_usd for d in parse_statement(_income()).dividends] == [Decimal("1.46")]


# A sale this parser doesn't read (#1149), as Fidelity would print it: a one-letter
# footnote marker, then a dated row. The name and identifier are privacy markers; its
# date and amounts are never read.
_SOLD_ROW = "t 03/20 SYNTHETIC HOLDER TWO 99997Q104 You Sold -1.000 $400.00000 - $400.00\n"
_SOLD_ESPP_ROW = _SOLD_ROW.replace(" 99997Q104", " ESPP### 99997Q104")
# The purchase row's shape before #1149, which isn't Fidelity's layout.
_OLD_ESPP_ROW = "12/31 SYNTHETIC HOLDER TWO ESPP### 99997Q104 You Bought 3.482 $192.12\n"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param(
            _income(_ACTIVITY.replace(_DIV_TOTAL, _DIV_TOTAL + _SOLD_ROW)),
            _DATED_UNREAD,
            id="marker-between-sections",
        ),
        *[
            pytest.param(
                _income(_ACTIVITY.replace(row, row + _SOLD_ROW)),
                f"{section} section has an unsupported or malformed row (2 dated rows, 1 parsed)",
                id=f"marker-inside-{kind}",
            )
            for kind, row, section in (
                ("dividends", _DIV_ROW, "Dividends, Interest & Other Income"),
                ("taxes", _WHT_ROW, "Taxes Withheld"),
                ("core", _MMKT_ROW, "Core Fund Activity"),
            )
        ],
        pytest.param(_income(_ACTIVITY + _SOLD_ROW.removeprefix("t ")), _DATED_UNREAD, id="none"),
        pytest.param(_income(_ACTIVITY + _SOLD_ROW), _DATED_UNREAD, id="marker"),
        pytest.param(_income(_ACTIVITY + _SOLD_ESPP_ROW), _ESPP_MALFORMED, id="marker-espp"),
        pytest.param(
            _income(_ACTIVITY + _SOLD_ROW.replace("t ", "*** ", 1)), _DATED_UNREAD, id="stars"
        ),
        pytest.param(
            _income(_ACTIVITY + _SOLD_ROW.replace("t ", "Synthetic Marker Q ", 1)),
            _DATED_UNREAD,
            id="words",
        ),
        pytest.param(
            _income(_ACTIVITY + "Synthetic Marker Q\f" + _SOLD_ROW),
            _DATED_UNREAD,
            id="marker-after-an-inline-page-break",
        ),
        pytest.param(
            _income(_ACTIVITY + "Synthetic Marker Q\f" + _SOLD_ESPP_ROW),
            _ESPP_MALFORMED,
            id="marker-espp-after-an-inline-page-break",
        ),
        pytest.param(_dec31_unpriced(_OLD_ESPP_ROW), _ESPP_MALFORMED, id="old-purchase-row"),
        pytest.param(
            _dec31_unpriced(_OLD_ESPP_ROW.replace("12/31", "02/30")),
            _ESPP_MALFORMED,
            id="old-purchase-row-invalid-date",
        ),
    ],
)
def test_dated_line_refuses_whatever_precedes_its_date(text: str, message: str) -> None:
    # #1149: a dated line is one holding an ``MM/DD`` token anywhere, so text before
    # its date can't hide a row the parser doesn't read.
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column=None,
        row_index=0,
        message=message,
        absent=_privacy_values(text),
    )


@pytest.mark.parametrize("prefix", ["***", "Synthetic Marker Q"])
def test_dated_text_after_a_page_break_on_the_holder_line_refuses(prefix: str) -> None:
    # #1149: dated text after a form feed gets a line of its own before any line is set
    # aside as identity, so the holder line can't carry it; the holder is then no longer
    # directly above ``Participant Number:``.
    text = _income()
    assert text.count("TAX PAYER\n") == 1
    dated = _SOLD_ROW.replace("t ", f"{prefix} ", 1)
    _assert_refusal(
        _refusal(text.replace("TAX PAYER\n", f"TAX PAYER\f{dated}")),
        section=_SOURCE,
        column=None,
        row_index=0,
        message="no account holder found above the Participant Number",
        absent=_privacy_values(dated),
    )


@pytest.mark.parametrize("holder", ["Total Synthetic Holdings", "12/31 SYNTHETIC HOLDER"])
def test_holder_line_starting_like_activity_parses(holder: str) -> None:
    # The holder line is identity, not activity, whatever it starts with.
    assert "TAX PAYER" in MONTHLY
    data = parse_statement(MONTHLY.replace("TAX PAYER", holder))
    assert data == replace(parse_statement(MONTHLY), account_holder=holder)


@pytest.mark.parametrize("description", ["Total Synthetic Fund", "12/31 SYNTHETIC FUND"])
def test_stock_holding_described_like_activity_parses(description: str) -> None:
    assert "EXAMPLE CORP (XESP)" in MONTHLY
    data = parse_statement(MONTHLY.replace("EXAMPLE CORP (XESP)", f"{description} (XESP)"))
    assert data.holdings == (replace(_HOLDING_XESP, description=description),)
    assert data.account_nav_usd == Decimal("653.36")


def _core_only(description: str) -> str:
    """A priced statement whose only holding is the core fund, described as given."""
    body = core_only_body(description, percent="100")
    return _statement("STOCK PLAN SERVICES REPORT", _DIV_PERIOD, body=body)


@pytest.mark.parametrize("description", ["Total Synthetic Fund", "12/31 SYNTHETIC FUND"])
def test_core_holding_described_like_activity_parses(description: str) -> None:
    data = parse_statement(_core_only(description))
    assert data.holdings == ()
    assert data.core_holding is not None
    assert data.core_holding.description == f"{description} OUS"
    assert data.core_holding.ending_market_value_usd == data.account_nav_usd == Decimal("1.09")


@pytest.mark.parametrize("page_break", ["\f", " \t\f \t\f"])
def test_core_holding_continuation_after_a_page_break_is_unchanged(page_break: str) -> None:
    text = _core_only("Total Synthetic Fund")
    assert "\nOUS (XQCP)" in text
    assert parse_statement(text.replace("\nOUS (XQCP)", f"\n{page_break}OUS (XQCP)")) == (
        parse_statement(text)
    )


@pytest.mark.parametrize("page_break", ["", "\f"], ids=["same-line", "page-break"])
@pytest.mark.parametrize("row", [_SOLD_ROW, _SOLD_ESPP_ROW], ids=["sale", "espp-sale"])
def test_dated_suffix_on_a_core_holding_continuation_refuses(page_break: str, row: str) -> None:
    text = _core_only("Total Synthetic Fund")
    continuation = "OUS (XQCP)                                      3.725%"
    assert text.count(continuation) == 1
    extra = "Synthetic Marker Q " + page_break + row.strip()
    edited = text.replace(continuation, "OUS (XQCP) " + extra)
    _assert_refusal(
        _refusal(edited),
        section=_SOURCE,
        column=None,
        row_index=0,
        message=_ESPP_MALFORMED if "ESPP" in row else _DATED_UNREAD,
        absent=_privacy_values(extra),
    )


@pytest.mark.parametrize(
    "anchor",
    [
        "January 1, 2026 - January 31, 2026",
        "Participant Number: I99999999",
        "Your Stock Plan Account Value:                 $653.36",
        "Ending Account Value Incl. AI               $653.36",
        _TOTAL_STOCKS_LINE.strip(),
        _TOTAL_HOLDINGS_LINE.strip(),
    ],
)
def test_page_break_does_not_repair_a_malformed_statement_signal(anchor: str) -> None:
    text = MONTHLY.replace(_XESP_ROW, "")
    row = _XESP_ROW.replace("EXAMPLE CORP", "SYNTHETIC HOLDER TWO 03/20")
    assert anchor + "\n" in text
    extra = anchor + "\f" + row
    edited = text.replace(anchor + "\n", extra, 1)
    _assert_refusal(
        _refusal(edited),
        section=_SOURCE,
        column=None,
        row_index=0,
        message=_DATED_UNREAD,
        absent=_privacy_values(extra),
    )


@pytest.mark.parametrize(
    "anchor",
    [
        next(line.strip() for line in _account_summary().split("\n") if "Taxes Withheld" in line),
        next(
            line.strip()
            for line in _income_summary().split("\n")
            if line.strip().startswith("Total ")
        ),
        "FID TREASURY ONLY MMKT FUND CL   unavailable   1.090   $1.0000   $1.09"
        "   not applicable   not applicable   $0.06",
        "OUS (XQCP)                                      3.725%",
        "Total Core Account (0% of account   unavailable   $1.09   $0.06",
        "holdings)",
        _DIV_TOTAL.strip(),
        "Total Taxes Withheld -$0.37",
        "Total Core Fund Activity $1.09",
    ],
)
def test_page_break_does_not_repair_an_income_signal(anchor: str) -> None:
    text = _income().replace(_XESP_ROW, "")
    row = _XESP_ROW.replace("EXAMPLE CORP", "SYNTHETIC HOLDER TWO 03/20")
    assert anchor + "\n" in text
    extra = anchor + "\f" + row
    edited = text.replace(anchor + "\n", extra, 1)
    _assert_refusal(
        _refusal(edited),
        section=_SOURCE,
        column=None,
        row_index=0,
        message=_DATED_UNREAD,
        absent=_privacy_values(extra),
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "Synthetic Marker Q",
        "Taxes Withheld - -",
        pytest.param("\v", id="vertical-tab"),
        pytest.param("\r", id="carriage-return"),
        pytest.param("\u2003", id="unicode-space"),
    ],
)
def test_dated_holding_after_a_page_break_remnant_refuses(prefix: str) -> None:
    # #1149: the split lets the holding be read, but the line as printed is dated text
    # after a page-break remnant.
    row = _XESP_ROW.replace("EXAMPLE CORP", "SYNTHETIC HOLDER TWO 03/20")
    extra = prefix + "\f" + row
    _assert_refusal(
        _refusal(MONTHLY.replace(_XESP_ROW, extra)),
        section=_SOURCE,
        column=None,
        row_index=0,
        message=_DATED_UNREAD,
        absent=_privacy_values(extra),
    )


def test_page_break_remnant_above_a_participant_line_refuses() -> None:
    row = _XESP_ROW.replace("EXAMPLE CORP", "SYNTHETIC HOLDER TWO 03/20")
    extra = "\n\nSynthetic Marker Q\f" + row + "Participant Number: I99999999\n"
    text = MONTHLY.replace(_XESP_ROW, "") + extra
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column=None,
        row_index=0,
        message=_DATED_UNREAD,
        absent=_privacy_values(extra),
    )


@pytest.mark.parametrize("label", ["Securities\fBought", "Net\fSecurities"])
def test_page_break_inside_a_purchase_table_label_refuses(label: str) -> None:
    row = _XESP_ROW.replace("EXAMPLE CORP", "SYNTHETIC HOLDER TWO SYNTHETIC MARKER Q 03/20")
    extra = label + " " + row
    text = MONTHLY.replace(_XESP_ROW, extra)
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column=None,
        row_index=0,
        message=_DATED_UNREAD,
        absent=_privacy_values(extra),
    )


assert _IS_TOTAL_ROW in _income_summary()


@pytest.mark.parametrize(
    ("summaries", "message"),
    [
        pytest.param(
            _YEAR_END_WHT_LINE + _income_summary() + _CASH_TOTAL, _TOTAL_UNREAD, id="extra-total"
        ),
        pytest.param(
            _YEAR_END_WHT_LINE
            + _income_summary().replace(
                _IS_TOTAL_ROW, f"{_IS_PAD}   Interest   0.10   0.10\n" + _IS_TOTAL_ROW
            ),
            "'Income Summary' must list exactly the rows Taxable, Dividends and Total; its "
            "'Total' row is missing, malformed or follows an unsupported row",
            id="fourth-income-summary-row",
        ),
        pytest.param(
            "  Taxes Withheld   -0.37   -0.37\n" + _income_summary(),
            "an account-summary 'Taxes Withheld' line is malformed",
            id="two-column-taxes-withheld",
        ),
        pytest.param(
            "  Taxes Withheld   -0.37 Synthetic Marker Q\n" + _income_summary(),
            "an account-summary 'Taxes Withheld' line is malformed",
            id="taxes-withheld-trailing-text",
        ),
    ],
)
def test_year_end_summary_of_an_unsupported_shape_refuses(summaries: str, message: str) -> None:
    # A year-end report's summaries aren't read, but only their supported shape passes.
    assert parse_statement(_year_end(_YEAR_END_WHT_LINE + _income_summary())).dividends == ()
    _assert_refusal(
        _refusal(_year_end(summaries)),
        section=_SOURCE,
        column=None,
        row_index=0,
        message=message,
        absent=(*_UNREAD_ABSENT, "0.37", "0.10", "1.46"),
    )


# --- the ESPP purchase: its cash and its table's rows (owner decisions, #1088, #1149) -


def _purchase_row(
    day: str = "12/31", quantity: str = "3.482", price: str = "192.12000", amount: str = "668.96"
) -> str:
    """Fidelity's ESPP purchase row (#1149) settled on ``day``. The name and
    identifier are privacy markers."""
    return purchase_row(
        day=day, quantity=quantity, price=price, amount=amount, identifier="99997Q104"
    )


# 3.482 x $192.12 is $668.96 to the cent.
_ESPP_ROW = _purchase_row()
_ESPP_BOUGHT_TOTAL = "Total Securities Bought - -$668.96\n"
_NET_BOUGHT = "Net Securities Bought & Sold - -$668.96\n"
_SPP_CREDIT = spp_credit(day="12/31", amount="668.96")
_SPP_CREDIT_TOTAL = "Total Other Activity In - $668.96\n"
# The marker's legend; the text after its sentence is a privacy marker.
_LEGEND = "t   Purchase of stock through your company stock plan. Synthetic Marker Q\n"
# The purchase as Fidelity prints it: the table, the cash it spends (whose section
# prints the same column header) and the legend.
_ESPP_CASH = purchase_cash(_ESPP_ROW, _SPP_CREDIT, total="668.96", legend=_LEGEND)
_ESPP_CASH_PRESENCE = (
    "an ESPP purchase's cash must show 'SPP PURCHASE CREDIT' rows with one 'Total Other "
    "Activity In' and one 'Total Securities Bought', or none of them" + _WHOLE_DOCUMENT
)
_BOUGHT_STRUCTURE = (
    "a 'Securities Bought & Sold' table must show its title, its two-line column header, its "
    "rows, one 'Total Securities Bought' line and one 'Net Securities Bought & Sold' line, in "
    "that order" + _WHOLE_DOCUMENT
)
_LEGEND_NEEDED = (
    "an ESPP purchase row's footnote marker needs exactly one 'Purchase of stock through your "
    "company stock plan.' legend line" + _WHOLE_DOCUMENT
)
# Two purchases that spend the same $668.96: 1.000 x $192.12 and 2.482 x $192.12.
_TWO_PURCHASES = (
    {"day": "12/30", "quantity": "1.000", "amount": "192.12"},
    {"day": "12/31", "quantity": "2.482", "amount": "476.84"},
)
_TWO_ROWS = "".join(_purchase_row(**row) for row in _TWO_PURCHASES)


def _purchase(
    day: date = date(2025, 12, 31),
    quantity: str = "3.482",
    price: str = "192.12000",
    amount: str = "668.96",
) -> StatementPurchase:
    """The row :func:`_purchase_row` prints, as parsed (#1151): the amount is an outflow."""
    return StatementPurchase(
        day, "99997Q104", Decimal(quantity), Decimal(price), Decimal(f"-{amount}")
    )


def _with_purchases(text: str, *purchases: StatementPurchase) -> StatementData:
    """``text`` parsed, with ``purchases`` as its purchase rows."""
    return replace(parse_statement(text), purchases=purchases)


@pytest.mark.parametrize(
    "cash",
    [
        pytest.param(_ESPP_CASH, id="one-credit"),
        pytest.param(
            _ESPP_CASH.replace(
                _SPP_CREDIT,
                "12/15 SPP PURCHASE CREDIT Journaled - - - $300.00\n"
                "12/31 SPP PURCHASE CREDIT Journaled - - - 368.96\n",
            ),
            id="two-credits",
        ),
    ],
)
def test_espp_purchase_cash_that_cancels_parses(cash: str) -> None:
    assert parse_statement(_dec31_unpriced(cash)) == _with_purchases(_dec31_unpriced(), _purchase())


def test_espp_purchase_cash_beyond_default_exponent_range_refuses_when_read() -> None:
    """A purchase whose cash, price and amount have more than a million whole
    digits, past Decimal's default exponent range. It parsed exactly before #1275; now the
    first SPP PURCHASE CREDIT amount refuses when it's read, with the money digit
    limit and no value. No in-limit amount reaches the exponent range."""
    amount = "7242" + "0" * 1_000_000 + ".00"
    row = _purchase_row(quantity="1.000", price=f"{amount}000")
    cash = _ESPP_CASH.replace(_ESPP_ROW, row).replace("668.96", amount)
    column = "Other Activity In: amount"
    assert_unchained_refusal(
        _refusal(_dec31_unpriced(cash)),
        ParseError(section=_SOURCE, row_index=0, column=column, message=MONEY, raw_value=""),
        f"ParseError in section {_SOURCE!r} at row 0, column {column!r}: {MONEY}",
        markers=(amount[:12],),
    )


@pytest.mark.parametrize("total", ["999999999999999.98", "999999999999999.99"])
def test_espp_purchase_cash_sum_keeps_every_cent(total: str) -> None:
    """Two in-limit credits sum to the cent: their
    exact total parses, a total one cent more refuses. Before #1275 the credits
    had 28 whole digits, so the sum also had to keep digits Decimal's default 28
    would drop; a money cell may now have at most 15 whole digits, and the
    reader refuses such a credit when it's read."""
    amount = "499999999999999.99"
    cash = _ESPP_CASH.replace(_SPP_CREDIT, _SPP_CREDIT.replace("668.96", amount) * 2)
    # 1,000,000 x $999,999,999.99999 is within the row's tolerance of the total.
    row = _purchase_row(quantity="1000000.000", price="999999999.99999")
    cash = cash.replace(_ESPP_ROW, row).replace("668.96", total)
    text = _dec31_unpriced(cash)
    if total == "999999999999999.98":
        assert parse_statement(text) == _with_purchases(
            _dec31_unpriced(),
            _purchase(quantity="1000000.000", price="999999999.99999", amount=total),
        )
        return
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column="Other Activity In: Total",
        row_index=0,
        message="the SPP PURCHASE CREDIT rows do not sum to the section's 'Total'" + _WHOLE_TABLE,
        absent=_privacy_values(cash),
    )


@pytest.mark.parametrize(
    ("cash", "column", "row_index", "message"),
    [
        pytest.param(
            _ESPP_CASH.replace(_SPP_CREDIT, ""), None, 0, _ESPP_CASH_PRESENCE, id="no-credit-row"
        ),
        pytest.param(
            _ESPP_CASH.replace(_SPP_CREDIT_TOTAL, ""),
            None,
            0,
            _ESPP_CASH_PRESENCE,
            id="no-credit-total",
        ),
        pytest.param(
            _ESPP_CASH.replace(_ESPP_BOUGHT_TOTAL, ""),
            None,
            0,
            _ESPP_CASH_PRESENCE,
            id="no-bought-total",
        ),
        pytest.param(
            _ESPP_CASH + _ESPP_BOUGHT_TOTAL, None, 0, _ESPP_CASH_PRESENCE, id="two-bought-totals"
        ),
        # A malformed credit row isn't read, so its total stands alone.
        pytest.param(
            _ESPP_CASH.replace("- - - $668.96", "- - $668.96"),
            None,
            0,
            _ESPP_CASH_PRESENCE,
            id="malformed-credit-row",
        ),
        pytest.param(
            _ESPP_CASH.replace(_SPP_CREDIT, _SPP_CREDIT.replace("668.96", "500.00")),
            "Other Activity In: Total",
            0,
            "the SPP PURCHASE CREDIT rows do not sum to the section's 'Total'" + _WHOLE_TABLE,
            id="rows-short-of-the-total",
        ),
        pytest.param(
            _ESPP_CASH.replace(_ESPP_BOUGHT_TOTAL, "Total Securities Bought - $668.96\n"),
            "Securities Bought: Total",
            0,
            "'Total Securities Bought' does not spend exactly the 'Total Other Activity In' "
            "credit" + _WHOLE_DOCUMENT,
            id="bought-as-an-inflow",
        ),
        pytest.param(
            _ESPP_CASH.replace(_ESPP_BOUGHT_TOTAL, "Total Securities Bought - -$500.00\n"),
            "Securities Bought: Total",
            0,
            "'Total Securities Bought' does not spend exactly the 'Total Other Activity In' "
            "credit" + _WHOLE_DOCUMENT,
            id="bought-less-than-the-credit",
        ),
        pytest.param(
            _ESPP_CASH.replace(_SPP_CREDIT, _SPP_CREDIT.replace("$668.96", "-$668.96")).replace(
                _SPP_CREDIT_TOTAL, "Total Other Activity In - -$668.96\n"
            ),
            "Other Activity In: amount",
            0,
            "Other Activity In: amount must be > 0",
            id="negative-credit",
        ),
        pytest.param(
            _ESPP_CASH.replace(_SPP_CREDIT, _SPP_CREDIT.replace("12/31", "02/30")),
            "Other Activity In: date",
            0,
            _TXN_DATE_OUTSIDE,
            id="credit-date-not-in-the-period",
        ),
    ],
)
def test_espp_purchase_cash_that_does_not_cancel_refuses(
    cash: str, column: str | None, row_index: int, message: str
) -> None:
    assert cash != _ESPP_CASH
    _assert_refusal(
        _refusal(_dec31_unpriced(cash)),
        section=_SOURCE,
        column=column,
        row_index=row_index,
        message=message,
        absent=_privacy_values(cash),
    )


def test_second_credit_row_refusal_is_located_at_row_1() -> None:
    cash = _ESPP_CASH.replace(
        _SPP_CREDIT,
        "12/15 SPP PURCHASE CREDIT Journaled - - - $668.96\n"
        "12/31 SPP PURCHASE CREDIT Journaled - - - 0.00\n",
    )
    _assert_refusal(
        _refusal(_dec31_unpriced(cash)),
        section=_SOURCE,
        column="Other Activity In: amount",
        row_index=1,
        message="Other Activity In: amount must be > 0",
        absent=_privacy_values(cash),
    )


@pytest.mark.parametrize(
    ("cash", "purchases"),
    [
        pytest.param(
            _ESPP_CASH.replace(_ESPP_ROW, _TWO_ROWS),
            (
                _purchase(date(2025, 12, 30), "1.000", amount="192.12"),
                _purchase(date(2025, 12, 31), "2.482", amount="476.84"),
            ),
            id="two-rows",
        ),
        pytest.param(
            _ESPP_CASH.replace(
                _BOUGHT_TITLE,
                _BOUGHT_TITLE + "\n\n                  SYNTHETIC MARKER Q 99991231\n\n",
            ),
            (_purchase(),),
            id="page-margin-line",
        ),
        pytest.param(
            _ESPP_CASH.replace(_ESPP_ROW, "\f" + _ESPP_ROW), (_purchase(),), id="row-starts-a-page"
        ),
        pytest.param(
            _ESPP_CASH.replace(_LEGEND, _LEGEND + "    Synthetic Marker Q, wrapped.\n"),
            (_purchase(),),
            id="legend-runs-on",
        ),
    ],
)
def test_espp_purchase_table_returns_its_rows(
    cash: str, purchases: tuple[StatementPurchase, ...]
) -> None:
    # #1149 reads and checks the table's rows; #1151 returns them in table order.
    assert parse_statement(_dec31_unpriced(cash)) == _with_purchases(_dec31_unpriced(), *purchases)


def test_year_end_report_returns_its_purchase_rows() -> None:
    # #1151: the table rule covers both report types, so a year-end report's row is
    # returned too.
    year_end = _statement("2025 YEAR-END INVESTMENT REPORT", _YEAR_END_PERIOD, body=_UNPRICED_BODY)
    with_row = year_end.replace(_UNPRICED_BODY, _UNPRICED_BODY + _ESPP_CASH)
    parsed = parse_statement(with_row)
    assert parsed.report_type is ReportType.YEAR_END
    assert parsed == _with_purchases(year_end, _purchase())


def test_espp_purchase_after_the_income_sections_returns_its_row() -> None:
    cash = _ESPP_CASH.replace("12/31", "03/31")
    assert parse_statement(_income(_ACTIVITY + cash)) == _with_purchases(
        _income(), _purchase(date(2026, 3, 31))
    )


_TABLE_START = _BOUGHT_TITLE + _BOUGHT_HEADER_1 + _BOUGHT_HEADER_2
assert _ESPP_CASH.startswith(_TABLE_START)


def _trailing(line: str) -> str:
    """``line`` with a privacy marker after its text."""
    return line.replace("\n", " Synthetic Marker Q\n")


@pytest.mark.parametrize(
    ("cash", "message"),
    [
        # Net lines.
        pytest.param(
            _NET_BOUGHT + "Synthetic Holder Two Synthetic Marker Q\n",
            _BOUGHT_STRUCTURE,
            id="net-without-a-table",
        ),
        pytest.param(
            _ESPP_CASH.replace(_NET_BOUGHT, _NET_BOUGHT * 2), _BOUGHT_STRUCTURE, id="two-nets"
        ),
        pytest.param(
            _ESPP_CASH.replace(_ESPP_BOUGHT_TOTAL + _NET_BOUGHT, _NET_BOUGHT + _ESPP_BOUGHT_TOTAL),
            _BOUGHT_STRUCTURE,
            id="net-above-the-total",
        ),
        pytest.param(
            _ESPP_CASH.replace(_NET_BOUGHT, _trailing(_NET_BOUGHT)),
            _BOUGHT_STRUCTURE,
            id="malformed-net",
        ),
        # The table's parts and their order.
        pytest.param(_ESPP_CASH.replace(_BOUGHT_TITLE, ""), _BOUGHT_STRUCTURE, id="no-title"),
        pytest.param(
            _ESPP_CASH.replace(_BOUGHT_HEADER_1, "", 1), _BOUGHT_STRUCTURE, id="no-header-line-1"
        ),
        pytest.param(
            _ESPP_CASH.replace(_BOUGHT_HEADER_2, "", 1), _BOUGHT_STRUCTURE, id="no-header-line-2"
        ),
        pytest.param(_ESPP_CASH.replace(_NET_BOUGHT, ""), _BOUGHT_STRUCTURE, id="no-net"),
        pytest.param(
            _ESPP_CASH.replace(_BOUGHT_TITLE, _BOUGHT_TITLE * 2), _BOUGHT_STRUCTURE, id="two-titles"
        ),
        pytest.param(
            _ESPP_CASH.replace(_BOUGHT_HEADER_1, _BOUGHT_HEADER_1 * 2, 1),
            _BOUGHT_STRUCTURE,
            id="two-header-line-1",
        ),
        pytest.param(
            _ESPP_CASH.replace(_BOUGHT_HEADER_2, _BOUGHT_HEADER_2 * 2, 1),
            _BOUGHT_STRUCTURE,
            id="two-header-line-2",
        ),
        pytest.param(
            _ESPP_CASH.replace(_BOUGHT_TITLE, _trailing(_BOUGHT_TITLE)),
            _BOUGHT_STRUCTURE,
            id="malformed-title",
        ),
        pytest.param(
            _ESPP_CASH.replace(_BOUGHT_HEADER_1, _trailing(_BOUGHT_HEADER_1), 1),
            _BOUGHT_STRUCTURE,
            id="malformed-header-line-1",
        ),
        pytest.param(
            _ESPP_CASH.replace(_BOUGHT_HEADER_2, _trailing(_BOUGHT_HEADER_2), 1),
            _BOUGHT_STRUCTURE,
            id="malformed-header-line-2",
        ),
        pytest.param(
            _ESPP_CASH.replace(
                _TABLE_START,
                _BOUGHT_TITLE
                + _trailing(_BOUGHT_HEADER_1)
                + _TABLE_START.removeprefix(_BOUGHT_TITLE),
            ),
            _BOUGHT_STRUCTURE,
            id="malformed-copy-of-header-line-1",
        ),
        pytest.param(
            _ESPP_CASH.replace(
                _TABLE_START,
                _BOUGHT_TITLE
                + _trailing(_BOUGHT_HEADER_2)
                + _TABLE_START.removeprefix(_BOUGHT_TITLE),
            ),
            _BOUGHT_STRUCTURE,
            id="malformed-copy-of-header-line-2",
        ),
        # Without any purchase cash, a missing total is the table's to report.
        pytest.param(
            _TABLE_START + _ESPP_ROW + _NET_BOUGHT + _LEGEND,
            _BOUGHT_STRUCTURE,
            id="no-total-without-cash",
        ),
        pytest.param(
            _ESPP_CASH + _TABLE_START + _ESPP_ROW + _NET_BOUGHT,
            _BOUGHT_STRUCTURE,
            id="second-table",
        ),
        pytest.param(_ESPP_ROW + _ESPP_CASH, _BOUGHT_STRUCTURE, id="row-before-the-table"),
        pytest.param(_ESPP_CASH + _ESPP_ROW, _BOUGHT_STRUCTURE, id="row-after-the-table"),
        pytest.param(
            _ESPP_CASH.replace(_ESPP_ROW, _ESPP_ROW + "Synthetic Marker Q\n"),
            _BOUGHT_STRUCTURE,
            id="text-among-the-rows",
        ),
        # The cash check runs first, so it reports its own total.
        pytest.param(
            _ESPP_CASH.replace(_ESPP_BOUGHT_TOTAL, _ESPP_BOUGHT_TOTAL * 2),
            _ESPP_CASH_PRESENCE,
            id="two-totals",
        ),
        pytest.param(
            _ESPP_CASH.replace(_ESPP_BOUGHT_TOTAL, _trailing(_ESPP_BOUGHT_TOTAL)),
            _ESPP_CASH_PRESENCE,
            id="malformed-total",
        ),
        # A dated line among the rows refuses as one.
        pytest.param(
            _ESPP_CASH.replace(_ESPP_ROW, _ESPP_ROW + _SOLD_ROW.replace("03/20", "12/31")),
            _DATED_UNREAD,
            id="sale-among-the-rows",
        ),
        # A page break after other text on a line leaves a remnant before its date.
        pytest.param(
            _ESPP_CASH.replace(
                _BOUGHT_HEADER_2 + _ESPP_ROW, _BOUGHT_HEADER_2.rstrip("\n") + "\f" + _ESPP_ROW, 1
            ),
            _ESPP_MALFORMED,
            id="row-after-a-page-break-remnant",
        ),
        pytest.param(
            _ESPP_CASH.replace(
                _TABLE_START + _ESPP_ROW, "Net\f" + _TABLE_START.replace("\n", "\f") + _ESPP_ROW
            ),
            _ESPP_MALFORMED,
            id="table-after-page-break-remnants",
        ),
        # The legend.
        pytest.param(_ESPP_CASH.replace(_LEGEND, ""), _LEGEND_NEEDED, id="no-legend"),
        pytest.param(_ESPP_CASH.replace(_LEGEND, _LEGEND * 2), _LEGEND_NEEDED, id="two-legends"),
        pytest.param(
            _ESPP_CASH.replace(_LEGEND, _LEGEND.replace("your company", "Synthetic Marker Q")),
            _LEGEND_NEEDED,
            id="malformed-legend",
        ),
        pytest.param(
            _ESPP_CASH.replace(
                _LEGEND, _LEGEND + _LEGEND.replace("your company", "Synthetic Marker Q")
            ),
            _LEGEND_NEEDED,
            id="malformed-second-legend",
        ),
        pytest.param(
            _ESPP_CASH.replace(_LEGEND, _LEGEND.replace("t ", "q ", 1)),
            _LEGEND_NEEDED,
            id="legend-for-another-marker",
        ),
        # The legend's run-on text isn't read, but a date there still refuses.
        pytest.param(
            _ESPP_CASH.replace(_LEGEND, _LEGEND + "    Synthetic Marker Q 12/31 fees.\n"),
            _DATED_UNREAD,
            id="dated-legend-continuation",
        ),
    ],
)
def test_espp_purchase_table_of_another_shape_refuses(cash: str, message: str) -> None:
    assert cash != _ESPP_CASH
    _assert_refusal(
        _refusal(_dec31_unpriced(cash)),
        section=_SOURCE,
        column=None,
        row_index=0,
        message=message,
        absent=_privacy_values(cash),
    )


@pytest.mark.parametrize(
    ("before", "table_end"),
    [
        pytest.param(
            "Other Activity In\n" + _SPP_CREDIT + _SPP_CREDIT_TOTAL,
            _ESPP_ROW + _ESPP_BOUGHT_TOTAL,
            id="no-net-after-the-total",
        ),
        pytest.param("", _ESPP_ROW, id="no-total-after-the-rows"),
    ],
)
def test_espp_purchase_table_that_ends_the_text_early_refuses(before: str, table_end: str) -> None:
    # A table cut short by the end of the text refuses like any other missing line.
    text = _dec31_unpriced(before + _LEGEND) + _TABLE_START + table_end
    _assert_refusal(
        _refusal(text),
        section=_SOURCE,
        column=None,
        row_index=0,
        message=_BOUGHT_STRUCTURE,
        absent=_privacy_values(text),
    )


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(_ESPP_ROW.removeprefix("t "), id="missing-marker"),
        pytest.param(_ESPP_ROW.replace(" 12/31", ""), id="missing-date"),
        pytest.param(_ESPP_ROW.replace(" SYNTHETIC HOLDER TWO", ""), id="missing-name"),
        pytest.param(_ESPP_ROW.replace(" ESPP###", ""), id="missing-espp"),
        pytest.param(_ESPP_ROW.replace(" 99997Q104", ""), id="missing-identifier"),
        pytest.param(_ESPP_ROW.replace(" You Bought", ""), id="missing-you-bought"),
        pytest.param(_ESPP_ROW.replace(" 3.482", ""), id="missing-quantity"),
        pytest.param(_ESPP_ROW.replace(" $192.12000", ""), id="missing-price"),
        pytest.param(_ESPP_ROW.replace(" - -$", " -$"), id="missing-transaction-cost"),
        pytest.param(_ESPP_ROW.replace(" -$668.96", ""), id="missing-amount"),
        pytest.param(_ESPP_ROW.replace("t ", "tt ", 1), id="two-marker-letters"),
        pytest.param(_ESPP_ROW.replace("t ", "T ", 1), id="upper-case-marker"),
        pytest.param(_ESPP_ROW.replace("3.482", "3.48"), id="two-decimal-quantity"),
        pytest.param(_ESPP_ROW.replace("$192.12000", "$192.12"), id="two-decimal-price"),
        pytest.param(_ESPP_ROW.replace("$192.12000", "$192.1200"), id="four-decimal-price"),
        pytest.param(_ESPP_ROW.replace("$192.12000", "192.12000"), id="price-without-dollar"),
        pytest.param(_ESPP_ROW.replace("-$668.96", "$668.96"), id="unsigned-amount"),
        pytest.param(
            _ESPP_ROW.replace("SYNTHETIC HOLDER TWO", "Synthetic Holder Two"), id="lower-case-name"
        ),
        pytest.param(_ESPP_ROW.replace("99997Q104", "99997Q10"), id="eight-character-identifier"),
        pytest.param(_trailing(_ESPP_ROW), id="text-after-the-amount"),
    ],
)
def test_malformed_espp_purchase_row_refuses(row: str) -> None:
    assert row != _ESPP_ROW
    _assert_refusal(
        _refusal(_dec31_unpriced(_ESPP_CASH.replace(_ESPP_ROW, row))),
        section=_SOURCE,
        column=None,
        row_index=0,
        message=_ESPP_MALFORMED,
        absent=_privacy_values(_ESPP_CASH.replace(_ESPP_ROW, row)),
    )


_DECEMBER = "December 1, 2025 - December 31, 2025"
_TWO_DECEMBERS = "December 1, 2025 - December 31, 2026"


@pytest.mark.parametrize("row_index", [0, 1])
@pytest.mark.parametrize(
    ("change", "period", "field", "message"),
    [
        pytest.param({"day": "02/30"}, _DECEMBER, "date", _TXN_DATE_OUTSIDE, id="invalid-date"),
        pytest.param(
            {"day": "11/30"}, _DECEMBER, "date", _TXN_DATE_OUTSIDE, id="date-outside-the-period"
        ),
        pytest.param(
            {"day": "12/15"}, _TWO_DECEMBERS, "date", _TXN_DATE_OUTSIDE, id="ambiguous-date"
        ),
        pytest.param(
            {"quantity": "0.000"},
            _DECEMBER,
            "quantity",
            "Securities Bought: quantity must be > 0",
            id="zero-quantity",
        ),
        pytest.param(
            {"price": "0.00000"},
            _DECEMBER,
            "price",
            "Securities Bought: price must be > 0",
            id="zero-price",
        ),
        pytest.param(
            {"amount": "0.00"},
            _DECEMBER,
            "amount",
            "an ESPP purchase row's amount is not an outflow",
            id="zero-amount",
        ),
        pytest.param(
            {"quantity": "9.000"},
            _DECEMBER,
            "amount",
            "an ESPP purchase row's quantity times its price does not equal its amount",
            id="product-off",
        ),
    ],
)
def test_espp_purchase_row_refusal_is_located_at_its_row(
    row_index: int, change: dict[str, str], period: str, field: str, message: str
) -> None:
    # In a two-year period a December date is in both years, so the other dates move
    # to June, which only the second year's period holds.
    other_day = "06/30" if period == _TWO_DECEMBERS else None
    rows = [{**row, "day": other_day or row["day"]} for row in _TWO_PURCHASES]
    rows[row_index].update(change)
    cash = _ESPP_CASH.replace(_ESPP_ROW, "".join(_purchase_row(**row) for row in rows))
    if other_day:
        cash = cash.replace(_SPP_CREDIT, _SPP_CREDIT.replace("12/31", other_day))
    _assert_refusal(
        _refusal(_statement("STOCK PLAN SERVICES REPORT", period, body=_UNPRICED_BODY + cash)),
        section=_SOURCE,
        column=f"Securities Bought: {field}",
        row_index=row_index,
        message=message,
        absent=_privacy_values(cash),
    )


@pytest.mark.parametrize(
    ("cash", "column", "message"),
    [
        pytest.param(
            _ESPP_CASH.replace(_ESPP_ROW, _TWO_ROWS.replace("476.84", "476.85")),
            "Securities Bought: Total",
            "the ESPP purchase rows do not sum to 'Total Securities Bought'" + _WHOLE_TABLE,
            id="rows-differ-from-the-total",
        ),
        pytest.param(
            _ESPP_CASH.replace(_NET_BOUGHT, _NET_BOUGHT.replace("668.96", "668.97")),
            "Securities Bought & Sold: Net",
            "'Net Securities Bought & Sold' does not equal 'Total Securities Bought', so the "
            "table can't prove every activity row was read" + _WHOLE_TABLE,
            id="net-differs-from-the-total",
        ),
    ],
)
def test_espp_purchase_table_that_does_not_add_up_refuses(
    cash: str, column: str, message: str
) -> None:
    _assert_refusal(
        _refusal(_dec31_unpriced(cash)),
        section=_SOURCE,
        column=column,
        row_index=0,
        message=message,
        absent=_privacy_values(cash),
    )


# --- Total lines accepted by exact shape, unread (owner decision, #1088) -----------


@pytest.mark.parametrize("line", list(_UNREAD_TOTALS.values()), ids=list(_UNREAD_TOTALS))
def test_total_line_of_an_accepted_shape_is_not_read(line: str) -> None:
    assert parse_statement(
        _statement("STOCK PLAN SERVICES REPORT", _JAN_2026, body=_PRICED_BODY + line)
    ) == (parse_statement(MONTHLY))


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(line.replace("\n", " Synthetic Marker Q\n", 1), id=f"{kind}-trailing-text")
        for kind, line in _UNREAD_TOTALS.items()
    ]
    + [
        pytest.param(
            _UNREAD_TOTALS["common-stock"].replace("   $5.85", ""), id="common-stock-one-short"
        ),
        pytest.param(
            "Total Including Other Holdings   $600.00   $653.36   $500.00\n",
            id="including-other-holdings-three",
        ),
        pytest.param("Total   $653.36   100%\n", id="allocation-in-cents"),
        pytest.param("Total   $653   99%\n", id="allocation-not-100"),
        pytest.param(
            _UNREAD_TOTALS["offering-periods"].replace("$192.1200", "$192.12"),
            id="offering-periods-two-decimal-price",
        ),
        pytest.param(
            _UNREAD_TOTALS["offering-periods"].replace(" 1 ", " 12 "),
            id="offering-periods-two-digit-marker",
        ),
        pytest.param(
            _UNREAD_TOTALS["cost-basis-footnote"].replace("core,", "Synthetic Marker Q,"),
            id="cost-basis-footnote-reworded",
        ),
    ],
)
def test_total_line_of_another_shape_refuses(line: str) -> None:
    _assert_refusal(
        _refusal(_statement("STOCK PLAN SERVICES REPORT", _JAN_2026, body=_PRICED_BODY + line)),
        section=_SOURCE,
        column=None,
        row_index=0,
        message=_TOTAL_UNREAD,
        absent=("Synthetic Marker Q", "653.36", "1,653", "512.34", "192.12"),
    )
