"""Synthetic NetBenefits Stock Plan Services statement text, in the
statement's ``pdftotext -layout`` layout, shared by the statement PDF parser
tests and the NetBenefits end-to-end, number-limit, refusal-chain and FA
pipeline tests (#1801). The custodian disclosure is public boilerplate.

:func:`statement_text` builds a whole statement: page 1, the continuation
pages, a body and the custodian footer. :func:`income_statement_text`,
:func:`dec31_unpriced_text` and :func:`year_end_text` build the income, the
unpriced Dec-31 and the year-end reports from it.
"""

from __future__ import annotations

from decimal import Decimal

from ibkr_fa.parser.netbenefits.statement_pdf import StatementHolding

PARTICIPANT_NO = "I99999999"
CUSTODIAN = "Fidelity Stock Plan Services LLC"


# A holdings data row + the stocks-only subtotal (referenced by several tests).
XESP_ROW = (
    "EXAMPLE CORP (XESP)   unavailable   3.482   $187.6400   $653.36   $668.96   -$15.60   $5.85\n"
)
TOTAL_STOCKS_LINE = (
    "Total Stocks (100% of account holdings)   unavailable   $653.36   $668.96   -$15.60   $5.85\n"
)
TOTAL_HOLDINGS_LINE = "Total Holdings   $653.36   $668.96   -$15.60   $5.85\n"
# A priced statement body: the account NAV + a Holdings table with one stock row
# (XESP 3.482 @ $187.64 -> $653.36) and the stocks-only Total Stocks subtotal.
PRICED_BODY = (
    "       Your Stock Plan Account Value:                 $653.36\n"
    "          Ending Account Value Incl. AI               $653.36\n"
    "Holdings\n"
    "Stocks\n"
    "Common Stock\n" + XESP_ROW + " 0.895%\n" + TOTAL_STOCKS_LINE + TOTAL_HOLDINGS_LINE
)
# An unpriced statement body: the account-value line shows a dash (no holdings).
UNPRICED_BODY = "       Your Stock Plan Account Value:                 -\n"

HOLDING_XESP = StatementHolding(
    description="EXAMPLE CORP",
    symbol="XESP",
    quantity=Decimal("3.482"),
    price_per_unit_usd=Decimal("187.6400"),
    ending_market_value_usd=Decimal("653.36"),
    total_cost_basis_usd=Decimal("668.96"),
    unrealized_gain_loss_usd=Decimal("-15.60"),
    eai_usd=Decimal("5.85"),
)


def page1_header(report_line: str, period: str) -> str:
    # The page-1 header: report type, period, blank line, holder, participant.
    return (
        f"                 {report_line}\n"
        f"                 {period}\n"
        f"\n"
        f"                 TAX PAYER\n"
        f"                   Participant Number: {PARTICIPANT_NO}\n"
    )


def continuation_header(report_line: str, period: str) -> str:
    # A continuation page: form-feed, the repeated report+period and participant,
    # but NO holder line (as the statement layout prints pages 2+).
    return (
        f"\f                 {report_line}\n"
        f"                 {period}\n"
        f"\n"
        f"                   Participant Number: {PARTICIPANT_NO}\n"
    )


def custodian_footer() -> str:
    return (
        'Items shown under "Stock Plans" represent your interests under your company\'s '
        f"stock plans, for which {CUSTODIAN} provides administrative and record keeping "
        "services.\n"
    )


def statement_text(
    report_line: str, period: str, *, body: str = PRICED_BODY, pages: int = 3
) -> str:
    # Page 1 then realistic continuation pages, then the valuation body + footer.
    return (
        page1_header(report_line, period)
        + continuation_header(report_line, period) * (pages - 1)
        + body
        + custodian_footer()
    )


MONTHLY = statement_text("STOCK PLAN SERVICES REPORT", "January 1, 2026 - January 31, 2026")


TOTAL_CORE_LINE = "Total Core Account (0% of account   unavailable   $1.09   $0.06\n"


DIV_PERIOD = "February 1, 2026 - March 31, 2026"
# The three income sections, balanced (1.46 gross - 0.37 withheld = 1.09 swept).
# Columns are single-spaced (the parser uses ``[ \t]+``, not column positions).
DIV_TOTAL = "Total Dividends, Interest & Other Income $1.46\n"
WHT_TOTAL = "Total Taxes Withheld -$0.37\n"
MMKT_TOTAL = "Total Core Fund Activity $1.09\n"
DIV_ROW = "03/05 EXAMPLE CORP 99999E104 Dividend Received - - $1.46\n"
WHT_ROW = "03/05 EXAMPLE CORP Non-Resident Tax -$0.37\n"
MMKT_ROW = "03/05 CASH You Bought FID TREASURY ONLY MMKT FUND CL OUS 1.090 $1.0000 $1.09 $1.09\n"
INCOME_ACTIVITY = (
    "Dividends, Interest & Other Income\n"
    "(Includes dividend reinvestment)\n"
    "Date Security Name CUSIP Description Quantity Price Amount\n"
    + DIV_ROW
    + DIV_TOTAL
    + "Taxes Withheld\n"
    "Date Security Description Amount\n"
    + WHT_ROW
    + "Total Federal Taxes Withheld -0.37\n"
    + WHT_TOTAL
    + "Core Fund Activity\n"
    "Date Type Transaction Description Quantity Price Amount Balance\n" + MMKT_ROW + MMKT_TOTAL
)


def _usd(amount: str) -> str:
    """A first-row summary amount: ``$`` before an amount, a bare dash for none."""
    return amount if amount == "-" else f"${amount}"


def account_summary(withheld: str = "-0.37", ytd: str | None = None) -> str:
    """The account summary's ``Taxes Withheld`` line under ``Subtractions`` (#903).

    Both carry a This Period and a Year-to-Date column; ``ytd`` defaults to this
    period, as on the year's first income.
    """
    ytd = withheld if ytd is None else ytd
    return (
        "                                              This Period      Year-to-Date\n"
        f"Subtractions                                  {withheld:>10}        {ytd:>10}\n"
        f"  Taxes Withheld                              {withheld:>10}        {ytd:>10}\n"
        "\n"
    )


INCOME_SUMMARY_PAD = " " * 60


def income_summary(dividends: str = "1.46", ytd: str | None = None) -> str:
    """The right-hand ``Income Summary`` block (#903): ``Taxable``, an indented
    ``Dividends`` and ``Total``, each with This Period and Year-to-Date columns."""
    ytd = dividends if ytd is None else ytd
    return (
        f"{INCOME_SUMMARY_PAD}Income Summary\n"
        f"{INCOME_SUMMARY_PAD}                    This Period   Year-to-Date\n"
        f"{INCOME_SUMMARY_PAD}Taxable             {_usd(dividends):>10}     {_usd(ytd):>10}\n"
        f"{INCOME_SUMMARY_PAD}   Dividends        {dividends:>10}     {ytd:>10}\n"
        f"{INCOME_SUMMARY_PAD}Total               {_usd(dividends):>10}     {_usd(ytd):>10}\n"
    )


def period_summaries(dividends: str = "1.46", withheld: str = "-0.37") -> str:
    """Both period summaries, as a Stock Plan Services report prints them."""
    return account_summary(withheld) + income_summary(dividends)


def income_statement_text(
    activity: str = INCOME_ACTIVITY,
    *,
    period: str = DIV_PERIOD,
    core_ending: Decimal = Decimal("1.09"),
    summary: str | None = None,
) -> str:
    nav = Decimal("653.36") + core_ending
    body = (
        f"       Your Stock Plan Account Value:                 ${nav:.2f}\n"
        f"          Ending Account Value Incl. AI               ${nav:.2f}\n"
        + (period_summaries() if summary is None else summary)
        + "Holdings\n"
        "Core Account\n"
        f"FID TREASURY ONLY MMKT FUND CL   unavailable   {core_ending:.3f}   "
        f"$1.0000   ${core_ending:.2f}   not applicable   not applicable   $0.06\n"
        "OUS (XQCP)                                      3.725%\n"
        f"Total Core Account (0% of account   unavailable   ${core_ending:.2f}   $0.06\n"
        "holdings)\n"
        "Stocks\n"
        + XESP_ROW
        + TOTAL_STOCKS_LINE
        + f"Total Holdings   ${nav:.2f}   $668.96   -$15.60   $5.91\n"
        + activity
    )
    return statement_text("STOCK PLAN SERVICES REPORT", period, body=body)


YEAR_END_PERIOD = "January 1, 2025 - December 31, 2025"


# Cash the three income sections don't list. The row carries distinct privacy
# markers as its description and reference; its date is never read.
CASH_ROW = "12/31 CASH Synthetic Holder Two Synthetic Marker Q $500.00\n"
CASH_TOTAL = "Total Other Cash Activity $500.00\n"
OTHER_CASH_ACTIVITY = "Other Cash Activity\nDate Type Description Amount\n" + CASH_ROW + CASH_TOTAL


def dec31_unpriced_text(extra: str = "") -> str:
    """The unpriced ``January 1, 2025 - December 31, 2025`` Stock Plan Services
    report the A2 cash proof reads, with ``extra`` after its account value."""
    return statement_text("STOCK PLAN SERVICES REPORT", YEAR_END_PERIOD, body=UNPRICED_BODY + extra)


# The year-end report of the parser tests' ``test_year_end_report_skips_the_summary_check``.
YEAR_END_WHT_LINE = "  Taxes Withheld   -0.37\n"


def year_end_text(summaries: str) -> str:
    return statement_text(
        "2025 YEAR-END INVESTMENT REPORT", YEAR_END_PERIOD, body=UNPRICED_BODY + summaries
    )


UNREAD_TOTALS = {
    "common-stock": "Total Common Stock (100% of account   $600.00   $653.36   $668.96"
    "   -$15.60   $5.85\nholdings)\n",
    "common-stock-unavailable": "Total Common Stock (99.5% of account   unavailable   $653.36"
    "   $668.96   -$15.60   $5.85\n",
    "including-other-holdings-one": "Total Including Other Holdings   $653.36\n",
    "including-other-holdings-two": "Total Including Other Holdings   $600.00   $653.36\n",
    "including-other-holdings-dash": "Total Including Other Holdings   -   $653.36\n",
    "including-other-holdings-dashes": "Total Including Other Holdings   -   -\n",
    "including-other-holdings-allocation": "Total Including Other Holdings   $600.00"
    "   Total   $1,653   100%\n",
    "allocation": "Total   $653   100%\n",
    "allocation-page-marker": "Total   $1,653   100%   S\n",
    "offering-periods": "Total for all Offering Periods   $512.34 1   $512.34 2   $192.1200\n",
    "offering-periods-quantity": "Total for all Offering Periods   2.667   $512.34\n",
    "cost-basis-footnote": "Total Cost Basis does not include the cost basis on core, money "
    "market or other positions where cost basis is unknown or not applicable.\n",
}
