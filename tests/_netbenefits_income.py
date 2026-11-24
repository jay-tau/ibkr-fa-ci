"""The ``netbenefits-income`` tests' config, statements and bundles, the
``pdftotext`` + ``parse_statement`` patch and the income refusal texts that
several test modules share. The core money-market fund's name and CUSIP are the
real ones ``src`` keys on; every other security value is synthetic.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.netbenefits.statement_pdf import (
    ReportType,
    StatementData,
    StatementDividend,
    StatementWithholding,
)
from tests._form67_overrides import FORM67_READY_OVERRIDE
from tests._netbenefits import (
    CG_WINDOW,
    INSTITUTION_TOML,
    PARTICIPANT_TOML,
    SECURITY_TOML,
    cg_opening_refusal,
    in_fy_sale_refusal,
    patch_pdftotext,
    statement_data,
    statement_label,
)
from tests._text_edits import replace_once

PARTICIPANT_NO = "I99999999"

INCOME_CONFIG = f"""\
[account]
number = "U1234567"
holder_name = "Sample Holder"
opening_date = 2024-02-01
status = "BeneficialOwner"

[account.foreign_tins]
US = "123456789"

{PARTICIPANT_TOML}
[[netbenefits.cg_fmv_values]]
purchase_date = 2025-12-01
section17_gross_fmv_total_inr = "16000"
source = "Employer perquisite worksheet"

{INSTITUTION_TOML}account_opening_date = 2025-11-01
status = "BeneficialOwner"
withholding_agent_name = "National Financial Services LLC"

{SECURITY_TOML}"""

AGENT_LINE = 'withholding_agent_name = "National Financial Services LLC"\n'


def without_withholding_agent(config: str) -> str:
    return replace_once(config, AGENT_LINE, "")


def income_statement(
    period_start: date,
    period_end: date,
    *,
    dividends: tuple[StatementDividend, ...] = (),
    withholdings: tuple[StatementWithholding, ...] = (),
    participant: str = PARTICIPANT_NO,
    report_type: ReportType = ReportType.STOCK_PLAN_SERVICES,
) -> StatementData:
    return statement_data(
        period_start,
        period_end,
        report_type=report_type,
        participant_no=participant,
        dividends=dividends,
        withholdings=withholdings,
    )


MARCH_DIV = StatementDividend(date(2026, 3, 5), "EXAMPLE CORP", "99999E104", Decimal("1.46"))
MARCH_WHT = StatementWithholding(date(2026, 3, 5), "EXAMPLE CORP", Decimal("-0.36"))

# #1042: income refusals name a statement by its 1-based place among the
# statements the income step gets (period-start order, year-end reports
# excluded) and an event by its 1-based row in the table it came from.
DIVIDENDS_TABLE = "Dividends, Interest & Other Income"
TAXES_TABLE = "Taxes Withheld"


def nth_statement(number: int) -> str:
    return f"statement {number} (by period start, year-end reports excluded)"


def event_at(number: int, row: int, table: str) -> str:
    """Where an income event is listed: ``row`` of ``table`` in statement ``number``."""
    return f"{nth_statement(number)}, row {row} of its {table} table"


def missing_event_refusal(kind: str, lister: int, row: int, other: int) -> str:
    """The refusal for a ``kind`` event at ``row`` of statement ``lister`` that
    the overlapping statement ``other`` leaves out."""
    table = {"dividend": DIVIDENDS_TABLE, "withholding": TAXES_TABLE}[kind]
    return (
        f"{nth_statement(lister)} lists a {kind} at row {row} of its {table} table that the "
        f"{table} table of the overlapping {nth_statement(other)} leaves out; overlapping "
        "statements must list the same events, so one of them is wrong — refusing rather "
        "than guess which"
    )


# What an opening-date refusal's fix also needs (#1471).
COVER_FROM_CORRECTED_DATE = (
    "and the statements (year-end reports excluded) must then cover from the corrected "
    "date, or the FY start if later, through the FY end"
)
# #1486: when the withholding opening-date refusal is reached, no same-date dividend
# exists (one would refuse first), so it names the orphan refusal a fixed date meets.
NO_DIVIDEND_ON_ITS_DATE = (
    ". The statements also list no dividend on that withholding's date, so even then it "
    "matches no dividend, and no config change can fix that: check the statement that lists it"
)

MISMATCH_1042S_REFUSAL = (
    "FY 2025-26: the Form 1042-S withholding total given with "
    "--reconcile-1042s-fy-withholding-usd differs from the statements' in-FY withholding "
    "total. Correct the FY-allocated 1042-S total, or find the dividend or withholding the "
    "statements miss"
)
CORE_FUND_CONFIG_REFUSAL = (
    "netbenefits.security.cusip is the account's core money-market fund; configure the "
    "ESPP stock instead"
)

# A covering bundle for window [account opening 2025-11-01, FY end 2026-03-31].
COVERING: dict[str, StatementData] = {
    "statement_2025-11.pdf": income_statement(date(2025, 11, 1), date(2025, 12, 31)),
    "statement_2026-01.pdf": income_statement(date(2026, 1, 1), date(2026, 1, 31)),
    "statement_2026-02.pdf": income_statement(
        date(2026, 2, 1), date(2026, 3, 31), dividends=(MARCH_DIV,), withholdings=(MARCH_WHT,)
    ),
}

# With capital gains (#666), the statements must cover ten days past the FY end:
# the covering bundle plus an April report.
CG_COVERING: dict[str, StatementData] = {
    **COVERING,
    "statement_2026-04.pdf": income_statement(date(2026, 4, 1), date(2026, 4, 30)),
}

_STATEMENT_MARKER = "STOCK PLAN SERVICES REPORT"
_CONFIRMATION_MARKER = "YOU PURCHASED"
CONFIRMATION_STUB = f"EXAMPLE ESPP PLAN\n{_CONFIRMATION_MARKER} 1 AT $1\n"


def patch_statements(
    monkeypatch: pytest.MonkeyPatch,
    by_name: dict[str, StatementData],
    *,
    confirmations: tuple[str, ...] = (),
    confirmation_text: str = CONFIRMATION_STUB,
) -> None:
    """Patch pdftotext + parse_statement. Each statement PDF's text encodes its
    filename; confirmation PDFs get ``confirmation_text`` (by default only the
    confirmation marker, which income skips)."""
    texts = {name: f"{_STATEMENT_MARKER}\n::{name}::\n" for name in by_name}
    texts.update(dict.fromkeys(confirmations, confirmation_text))

    def fake_parse(
        text: str, *, expected_participant_no: str | None = None, source: str = ""
    ) -> StatementData:
        name = text.split("::")[1]
        stmt = by_name[name]
        if expected_participant_no is not None and expected_participant_no != stmt.participant_no:
            raise ParseError(
                section="statement",
                row_index=0,
                column="Participant Number",
                message=f"participant mismatch: expected {expected_participant_no}",
                raw_value=stmt.participant_no,
            )
        return stmt

    patch_pdftotext(monkeypatch, texts)
    monkeypatch.setattr("ibkr_fa.parser.netbenefits.bundle.parse_statement", fake_parse)


BLANK_DEDUCTOR = (
    'The Quicko block in schedule-tr.csv leaves "Name of the deductor" blank; add '
    "[netbenefits.account].withholding_agent_name, copied from your Form 1042-S: box 12d "
    "for tax in box 7a, or the agent box 14a names for tax in box 8"
)

# #1486: with capital gains, income's own coverage refusals name the capital-gains
# window, so supplying it also clears the capital-gains coverage check.
ALL_SHORT_COVERAGE_REFUSAL = (
    "the statements (year-end reports excluded) end before the FY end, so they don't "
    f"cover {CG_WINDOW}; supply the statement(s) covering the gap"
)
ALL_COVERAGE_GAP_REFUSAL = (
    f"the statements (year-end reports excluded) leave a gap in {CG_WINDOW} "
    "(coverage is needed to prove no dividend was missed there)"
)
# Their inputs: statements ending in January, or missing January and ending at
# the FY end. The capital-gains window also needs 1-10 April.
SHORT_OF_THE_FY_END = {k: v for k, v in COVERING.items() if k != "statement_2026-02.pdf"}
GAP_IN_JANUARY = {k: v for k, v in COVERING.items() if k != "statement_2026-01.pdf"}
TEN_DAYS_AFTER = {"statement_2026-04.pdf": income_statement(date(2026, 4, 1), date(2026, 4, 10))}
CG_OPENING_REFUSAL = cg_opening_refusal("2025-26")
# #666: the in-FY lot is the closed-lots export's first data row.
IN_FY_REFUSAL = in_fy_sale_refusal(1, "2025-26")

TIN_MARKER = "999-00-0001"
ACCOUNT_MARKER = "999900001"


def marked_ready_run(tin_country: str) -> tuple[str, dict[str, StatementData]]:
    """A ready Form 67 run's config and statements, with the TIN marker filed
    for ``tin_country`` and the account marker as the Participant No."""
    config = INCOME_CONFIG.replace('US = "123456789"', f'{tin_country} = "{TIN_MARKER}"').replace(
        f'participant_no = "{PARTICIPANT_NO}"', f'participant_no = "{ACCOUNT_MARKER}"'
    )
    statements = {
        name: replace(statement, participant_no=ACCOUNT_MARKER)
        for name, statement in CG_COVERING.items()
    }
    return config + FORM67_READY_OVERRIDE, statements


# #1478: with capital gains (netbenefits-all), the corrected run must also cover
# the capital-gains window, so the opening-date refusal names that window.
CG_COVER_FROM_CORRECTED_DATE = (
    f"and the statements (year-end reports excluded) must then cover {CG_WINDOW}"
)
# The issue's FY 2025-26 example: income in January, statements from January
# through March, and an opening date mistakenly set in February.
MISTAKEN_OPENING = INCOME_CONFIG.replace(
    "account_opening_date = 2025-11-01", "account_opening_date = 2026-02-01"
)
CORRECTED_OPENING = INCOME_CONFIG.replace(
    "account_opening_date = 2025-11-01", "account_opening_date = 2026-01-01"
)
_JANUARY_DIV = StatementDividend(date(2026, 1, 15), "EXAMPLE CORP", "99999E104", Decimal("1.46"))
_JANUARY_WHT = StatementWithholding(date(2026, 1, 15), "EXAMPLE CORP", Decimal("-0.36"))


def january_statements(kind: str) -> dict[str, StatementData]:
    """The example's statements. Its January ``kind`` event is a dividend with
    its withholding, or a withholding with no dividend: a same-date dividend
    would refuse first, as the dividends table is checked first.

    No real statement can hold that lone withholding: the daily sweep check in
    ``_parse_income_activity`` (src/ibkr_fa/parser/netbenefits/statement_pdf.py)
    refuses a date whose dividends plus withholding don't equal its MMKT sweep,
    and with no dividend that sum is negative while the sweep never is. So each
    ``withholding`` test pins normalize's withholding opening-date refusal, a
    defensive branch, through the patched parser (``patch_statements``)."""
    return {
        "statement_2026-01.pdf": income_statement(
            date(2026, 1, 1),
            date(2026, 1, 31),
            dividends=(_JANUARY_DIV,) if kind == "dividend" else (),
            withholdings=(_JANUARY_WHT,),
        ),
        "statement_2026-02.pdf": income_statement(date(2026, 2, 1), date(2026, 3, 31)),
    }


def opening_date_refusal(kind: str, cover: str) -> str:
    """The opening-date refusal for row 1 of statement 1's ``kind`` table,
    naming ``cover``; the withholding one then names its missing dividend."""
    if kind == "dividend":
        return (
            f"the dividend at {event_at(1, 1, DIVIDENDS_TABLE)} predates "
            "netbenefits.account.account_opening_date; income can't precede the account, and "
            "coverage before it isn't required — fix netbenefits.account.account_opening_date, "
            f"{cover}"
        )
    return (
        f"the withholding at {event_at(1, 1, TAXES_TABLE)} predates "
        "netbenefits.account.account_opening_date; fix netbenefits.account.account_opening_date, "
        f"{cover}{NO_DIVIDEND_ON_ITS_DATE}"
    )


# Privacy markers on a year-end report's income rows: the security name, the day
# and the amounts.
YE_NAME = "SYNTHQYEDIV"
YE_DIV = StatementDividend(date(2025, 12, 19), YE_NAME, "99999E104", Decimal("9.87"))
YE_WHT = StatementWithholding(date(2025, 12, 19), YE_NAME, Decimal("-2.47"))
YE_MARKERS = (YE_NAME, "99999E104", "9.87", "2.47", "2025-12-19")
YE_FILE = "statement_2025-year-end.pdf"
NOV_DEC = "statement_2025-11.pdf"
# The issue's bundle: the account opened 2025-11-01, and the Stock Plan Services
# reports cover November-December 2025 and January-March 2026 with no income.
QUIET: dict[str, StatementData] = {
    NOV_DEC: income_statement(date(2025, 11, 1), date(2025, 12, 31)),
    "statement_2026-01.pdf": income_statement(date(2026, 1, 1), date(2026, 3, 31)),
}


def year_end_report(
    *,
    dividends: tuple[StatementDividend, ...] = (YE_DIV,),
    withholdings: tuple[StatementWithholding, ...] = (YE_WHT,),
    year: int = 2025,
) -> StatementData:
    """The ``year`` year-end report, printing ``dividends`` and ``withholdings``."""
    return income_statement(
        date(year, 1, 1),
        date(year, 12, 31),
        dividends=dividends,
        withholdings=withholdings,
        report_type=ReportType.YEAR_END,
    )


def year_end_refusal(kind: str, row: int = 1) -> str:
    """The refusal for the ``kind`` at ``row`` of the year-end report, statement 1."""
    table = {"dividend": DIVIDENDS_TABLE, "withholding": TAXES_TABLE}[kind]
    return (
        f"{statement_label(1)} lists a {kind} at row "
        f"{row} of its {table} table that no Stock Plan Services report covering that day "
        "lists with the same amount; a year-end report's income must match the Stock Plan "
        "Services reports, so one of them is wrong — refusing rather than guess which"
    )


FUND_NAME = "FID TREASURY ONLY MMKT FUND CL OUS"
FUND_DIV = StatementDividend(date(2026, 3, 31), FUND_NAME, "31617H821", Decimal("1.12"))
FUND_WHT = StatementWithholding(date(2026, 3, 31), FUND_NAME, Decimal("-0.28"))
