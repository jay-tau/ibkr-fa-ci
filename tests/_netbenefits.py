"""Synthetic NetBenefits inputs, the ``pdftotext`` stub, the refusal check and the
refusal texts that several test modules share.

Every value is synthetic. A module that needs another layout passes the
builders' keywords; scenario edits stay at their callers. The refusal texts are
written out here, never read from ``src``, so a test compares the code's text
with an independent copy.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.netbenefits.confirmation_pdf import EspPurchase
from ibkr_fa.parser.netbenefits.lots_csv import EspLot
from ibkr_fa.parser.netbenefits.statement_pdf import (
    ReportType,
    StatementCoreHolding,
    StatementData,
    StatementDividend,
    StatementMmktBuy,
    StatementPurchase,
    StatementWithholding,
)

# The [netbenefits] participant block, the [netbenefits.account] institution
# fields and the [netbenefits.security] block shared by the FA, income, CG and
# E2E configs. Each config writes its own [account] identity, opening date and
# other blocks around them.
PARTICIPANT_TOML = """\
[netbenefits]
espp_discount_pct = "10"
participant_no = "I99999999"
"""
INSTITUTION_TOML = """\
[netbenefits.account]
institution_name = "Fidelity Stock Plan Services LLC"
address = "245 Summer Street, Boston, MA"
zip = "02210"
country_iso2 = "US"
"""
SECURITY_TOML = """\
[netbenefits.security]
symbol = "XESP"
cusip = "99999E104"
isin = "US99999E1047"
entity_address = "1 Sample Issuer Street, Sample City, XX"
entity_zip = "00000"
listing_exchange = "NASDAQ"
issuer_country_code = "US"
nature_of_entity = "Listed company shares"
"""

# The open-lots export's shape, with the UTF-8 BOM and the trailing blank line + USD footer.
OPEN_CSV = (
    "\ufeffDate acquired,Quantity,Cost basis,Cost basis/share,Value,Gain/loss,"
    "Sale availability date,Transfer availability date,Grant date,Share source,Holding period\n"
    "Mar-31-2026,4.6315,755.31,163.08,832.98,77.67,-,-,Jan-05-2026,SP,Short\n"
    "Dec-31-2025,3.4820,668.96,192.12,626.24,-42.72,-,-,Nov-03-2025,SP,Short\n"
    ",\n"
    "The values are displayed in USD\n"
)

# The closed-lots export's header.
CLOSED_HEADER = (
    "Date acquired",
    "Quantity",
    "Date sold or transferred",
    "Proceeds",
    "Cost basis",
    "Gain/loss",
    "Term",
)

CLOSED_EMPTY_CSV = (
    "\ufeffDate acquired,Quantity,Date sold or transferred,Proceeds,Cost basis,Gain/loss,Term\n"
    ",\n"
    "The values are displayed in USD\n"
)

# A closed lot whose Dec-31-2025 acquisition was disposed AFTER year-end → the
# download-dated open snapshot can no longer prove the Dec-31 quantity.
CLOSED_GAP_CSV = (
    "\ufeffDate acquired,Quantity,Date sold or transferred,Proceeds,Cost basis,Gain/loss,Term\n"
    "Dec-31-2025,1.0000,Feb-02-2026,500.00,192.12,307.88,Short\n"
    ",\n"
    "The values are displayed in USD\n"
)

# #911: a newer closed-lots export that lists a sale but renames "Proceeds". Beside
# CLOSED_EMPTY_CSV, ignoring it would file no sales.
CLOSED_RENAMED_PROCEEDS_CSV = CLOSED_GAP_CSV.replace(",Proceeds,", ",Proceeds (USD),")

# #911: an open-lots export whose header changed case only, so every column is
# still an open-lots column.
OPEN_RECASED_CSV = OPEN_CSV.replace(",Holding period\n", ",Holding Period\n")

# #1041: the bundle's refusal of CLOSED_RENAMED_PROCEEDS_CSV after the file's
# label. It names the expected header (code constants) and only counts the
# unexpected columns, so the renamed 'Proceeds (USD)' cell is never printed.
CLOSED_RENAMED_PROCEEDS_REFUSAL = (
    "looks like a Fidelity lots export but matches neither lots header: compared with "
    f"the closed-lots header {CLOSED_HEADER}, missing: 'Proceeds'; unexpected: "
    "1 column(s). If Fidelity changed the export, ibkr-fa must be updated to read it; "
    "otherwise move the file out of the input directory"
)

# A closed lot acquired AND disposed within the CY (a real intra-CY holding that
# needs the #I13 proceeds path) — refused up front ("overlaps CY"), never a
# gap-window reconstruction.
CLOSED_INTRA_CY_CSV = (
    "\ufeffDate acquired,Quantity,Date sold or transferred,Proceeds,Cost basis,Gain/loss,Term\n"
    "Jun-30-2025,1.0000,Sep-30-2025,500.00,192.12,307.88,Short\n"
    ",\n"
    "The values are displayed in USD\n"
)

# Synthetic closed export with one disposal, its "Date sold or transferred" header
# cell wrapped in the export's HTML <span>.
CLOSED_WITH_ROW_CSV = (
    '\ufeffDate acquired,Quantity,<span style="color: rgb(0, 0, 51);">'
    "Date sold or transferred</span>,Proceeds,Cost basis,Gain/loss,Term\n"
    "Mar-31-2026,2.0000,Jun-15-2026,900.00,755.31,144.69,Short\n"
    ",\n"
    "The values are displayed in USD\n"
)


def confirmation(
    *,
    purchase: str,
    offering: str,
    mv: str,
    contrib: str,
    shares: str,
    price: str,
    gain: str,
    participant: str = "I99999999",
    cusip: str = "99999E104",
    symbol: str = "XESP",
    name: str = "EXAMPLE CORP",
    trade_settle: str = "12-31-25 01-05-26",
    trans_no: str = "9XQ2TZ",
    street: bool = False,
    aligned: bool = False,
    blank_lines: bool = True,
    run_together: bool = False,
) -> str:
    """An ESPP purchase confirmation's ``pdftotext -layout`` text.

    By default it has no street line, 31 spaces follow the security name and
    blank lines separate the blocks. ``street`` adds the holder's street line,
    ``aligned`` pads the security name to the amounts' column,
    ``blank_lines=False`` drops the blank lines and ``run_together`` prints
    ``PURCHASE   PRICEGain`` for ``PURCHASE PRICE   Gain``.
    """
    street_line = "   1 MAIN STREET\n" if street else ""
    blank = "\n" if blank_lines else ""
    price_and_gain = "PURCHASE   PRICEGain" if run_together else "PURCHASE PRICE   Gain"
    name_cell = name.ljust(45) if aligned else f"{name}{' ' * 31}"
    return (
        "EXAMPLE CORPORATION\n"
        "              PARTICIPANT NO.\n"
        f"              {participant}\n"
        "   TAX PAYER\n"
        f"{street_line}"
        "     CUSTOMER NO.   PARTICIPANT ID.   TYPE   TRADE DATE   SETTLEMENT DATE"
        "   TRANS NO.   CUSIP NO.\n"
        f"              {participant}   1 000   {trade_settle}   {trans_no} {cusip}\n"
        f"{blank}"
        "Confirmation of purchase made through your\n"
        f"EXAMPLE ESPP PLAN on {purchase}.        PURCHASE INFORMATION\n"
        f"{blank}"
        f"Offering period: {offering}   Market Value at Purchase\u00b9   ${mv}\n"
        f"                                             Accumulated Contributions*  ${contrib}\n"
        f"YOU PURCHASED {shares} AT ${price} {price_and_gain}\u00b2   ${gain}\n"
        f"{blank}"
        f"SECURITY DESCRIPTION   SYMBOL: {symbol}           EXPLANATION OF PROCEEDS\n"
        f"{name_cell}Accumulated Contributions   ${contrib}\n"
        f"                                             Share Proceeds**   {shares}\n"
        "* Accumulated Contributions are the amount of money contributed to your Plan.\n"
    )


# The Dec-31-2025 ESPP purchase (FMV 743.30, contribution 668.96, 10% discount).
dec_confirmation = partial(
    confirmation,
    purchase="DEC/31/2025",
    offering="NOV/03/2025 - DEC/31/2025",
    mv="743.30",
    contrib="668.96",
    shares="3.482",
    price="192.1200",
    gain="74.34",
)

# The Mar-31-2026 ESPP purchase (FMV 839.23, contribution 755.31) — same FY 2025-26.
mar_confirmation = partial(
    confirmation,
    purchase="MAR/31/2026",
    offering="JAN/05/2026 - MAR/31/2026",
    mv="839.23",
    contrib="755.31",
    shares="4.6315",
    price="163.0800",
    gain="83.92",
    trade_settle="03-31-26 04-02-26",
)

CONF_DEC = dec_confirmation()
CONF_MAR = mar_confirmation()
# The Dec purchase with the holder's street line and the security name padded to
# the amounts' column, both of which CONF_DEC lacks.
CONF_DEC_STREET_ALIGNED = dec_confirmation(street=True, aligned=True)


def statement_data(
    period_start: date,
    period_end: date,
    *,
    report_type: ReportType = ReportType.STOCK_PLAN_SERVICES,
    participant_no: str = "I99999999",
    dividends: tuple[StatementDividend, ...] = (),
    withholdings: tuple[StatementWithholding, ...] = (),
    mmkt_buys: tuple[StatementMmktBuy, ...] = (),
    purchases: tuple[StatementPurchase, ...] = (),
    core_holding: StatementCoreHolding | None = None,
) -> StatementData:
    """A parsed unpriced statement of ``TAX PAYER`` that holds no stock, with the
    given rows."""
    return StatementData(
        report_type=report_type,
        period_start=period_start,
        period_end=period_end,
        participant_no=participant_no,
        account_holder="TAX PAYER",
        custodian="Fidelity Stock Plan Services LLC",
        holdings=(),
        account_nav_usd=None,
        dividends=dividends,
        withholdings=withholdings,
        mmkt_buys=mmkt_buys,
        purchases=purchases,
        core_holding=core_holding,
    )


def esp_purchase(**overrides: object) -> EspPurchase:
    """The shared default test purchase: 3.482 shares at $192.12 on Dec-31-2025,
    with ``overrides``."""
    fields: dict[str, object] = {
        "purchase_date": date(2025, 12, 31),
        "settlement_date": date(2025, 12, 31),
        "offering_period_start": date(2025, 11, 3),
        "offering_period_end": date(2025, 12, 31),
        "symbol": "XESP",
        "security_description": "EXAMPLE CORP",
        "cusip": "99999E104",
        "participant_no": "I99999999",
        "shares": Decimal("3.482"),
        "purchase_price_per_share_usd": Decimal("192.12"),
        "fmv_total_usd": Decimal("743.30"),
        "contribution_usd": Decimal("668.96"),
        "perquisite_usd": Decimal("74.34"),
        **overrides,
    }
    return EspPurchase(**fields)


def esp_open_lot(**overrides: object) -> EspLot:
    """The open lot of the shared default purchase: 3.4820 shares, contribution
    $668.96 at $192.12, acquired Dec-31-2025, with ``overrides``."""
    defaults: dict[str, object] = {
        "acquire_date": date(2025, 12, 31),
        "quantity": Decimal("3.4820"),
        "contribution_usd": Decimal("668.96"),
        "cost_per_share_paid": Decimal("192.12"),
        "fidelity_term": "Short",
    }
    defaults.update(overrides)
    return EspLot(**defaults)  # type: ignore[arg-type]


# --- statement layout (#1149) --------------------------------------------------

# The name on a synthetic ESPP purchase row, a privacy marker.
PURCHASE_ROW_NAME = "SYNTHETIC HOLDER TWO"
BOUGHT_TITLE = "Securities Bought & Sold\n"
BOUGHT_HEADER_1 = "Settlement Symbol/ Transaction\n"
BOUGHT_HEADER_2 = "Date Security Name CUSIP Description Quantity Price Cost Amount\n"


def purchase_row(*, day: str, quantity: str, price: str, amount: str, identifier: str) -> str:
    """Fidelity's ESPP purchase row (#1149): footnote marker, settlement date, name,
    ``ESPP###``, identifier, ``You Bought``, quantity, price, the ``Transaction Cost``
    dash and the amount as an outflow."""
    return (
        f"t {day} {PURCHASE_ROW_NAME} ESPP### {identifier} You Bought {quantity} ${price} - "
        f"-${amount}\n"
    )


def spp_credit(*, day: str, amount: str) -> str:
    """The ``Other Activity In`` row of the cash that pays for a purchase settled on
    ``day``."""
    return f"{day} SPP PURCHASE CREDIT Journaled - - - ${amount}\n"


def purchase_cash(rows: str, cash_rows: str, *, total: str, legend: str) -> str:
    """The ``Securities Bought & Sold`` table printing ``rows``, then the cash that pays
    for them (whose section prints the same column header) printing ``cash_rows``,
    each section totalling ``total``, then the footnote marker's ``legend`` line."""
    header = BOUGHT_HEADER_1 + BOUGHT_HEADER_2
    return (
        BOUGHT_TITLE
        + header
        + rows
        + f"Total Securities Bought - -${total}\n"
        + f"Net Securities Bought & Sold - -${total}\n"
        + "Other Activity In\n"
        + header
        + cash_rows
        + f"Total Other Activity In - ${total}\n"
        + legend
    )


def core_only_body(description: str, *, percent: str) -> str:
    """A priced statement body whose only holding is the Core Account money-market
    fund, printed as ``description``, its subtotal ``percent`` of the account's
    holdings."""
    return (
        "       Your Stock Plan Account Value:                 $1.09\n"
        "          Ending Account Value Incl. AI               $1.09\n"
        "Holdings\n"
        "Core Account\n"
        f"{description}   unavailable   1.090   "
        "$1.0000   $1.09   not applicable   not applicable   $0.06\n"
        "OUS (XQCP)                                      3.725%\n"
        f"Total Core Account ({percent}% of account   unavailable   $1.09   $0.06\n"
        "holdings)\n"
        "Total Holdings   $1.09   $0.00   $0.00   $0.06\n"
    )


# --- refusal texts several modules expect ---------------------------------------

# How refusals number statements: every report, year-end reports included (#1042).
ALL_REPORTS = "by period start, year-end reports included"


def statement_label(number: int) -> str:
    """The ``number``-th statement among every report."""
    return f"statement {number} ({ALL_REPORTS})"


def table_row(number: int, row: int, table: str) -> str:
    """Where an event is listed: ``row`` of ``table`` in statement ``number``."""
    return f"{statement_label(number)}, row {row} of its {table} table"


def a2_credit_refusal(kind: str, where: str) -> str:
    """The A2 cash proof's refusal for the dividend or withholding ``kind`` at
    ``where``."""
    return (
        f"the {kind} at {where} credits the account on/before year-end; its Dec-31 cash "
        "balance can't be proven zero — file Table A2 manually"
    )


def a2_sweep_refusal(where: str) -> str:
    """The A2 cash proof's refusal for the money-market sweep at ``where``."""
    return (
        f"the money-market sweep at {where}, dated on/before year-end, created a FID "
        "Treasury MMKT fund holding (the net dividend is swept into the core fund) — a "
        "SEPARATE foreign asset this tool does not yet auto-disclose; file the fund's own "
        "Schedule FA Table A3 holding and include its value in the A2 account balances "
        "manually"
    )


# The window the statements must cover for capital gains, as refusals name it.
CG_WINDOW = (
    "the window from the later of the FY start and "
    "netbenefits.account.account_opening_date through ten days after the FY end"
)
CG_SHORT_REFUSAL = (
    "the statements (year-end reports excluded) end before ten days after the FY end, so "
    f"they don't cover {CG_WINDOW}; supply the statement(s) covering the gap"
)


def cg_opening_refusal(fy: str) -> str:
    """The refusal of an opening date after the end of FY ``fy``."""
    return (
        f"netbenefits.account.account_opening_date is after the end of FY {fy}, so the "
        "statements can't prove that FY had no sale; correct the date, or don't run "
        "NetBenefits capital gains for that FY"
    )


def in_fy_sale_refusal(row: int, fy: str) -> str:
    """The refusal of the closed-lots export's data ``row``, sold in FY ``fy`` (#666)."""
    return (
        f"closed-lots data row {row}, Date sold or transferred: a sale or transfer in "
        f"FY {fy}; ibkr-fa can't yet check NetBenefits sales against the statements, so it "
        "files no capital gains for that FY"
    )


# #1149's refusal for a dated line the statement parser doesn't read, such as a sale.
DATED_LINE_UNREAD = (
    "a dated line was not consumed by a supported activity section and is not a "
    "supported ESPP purchase row; the whole document was checked"
)


def patch_pdftotext(
    monkeypatch: pytest.MonkeyPatch, texts: Mapping[str, str], *, by_source: bool = False
) -> list[str]:
    """Make the bundle's ``pdftotext_layout`` return ``texts[key]`` for each PDF,
    where ``key`` is the PDF's file name or, with ``by_source``, the label the
    bundle passes: its path relative to the bundle, so two nested PDFs with the
    same basename get different texts. Return the keys read, in order."""
    read: list[str] = []

    def fake_pdf(path: Path, *, doc_kind: str, source: str) -> str:
        key = source if by_source else path.name
        read.append(key)
        return texts[key]

    monkeypatch.setattr("ibkr_fa.parser.netbenefits.bundle.pdftotext_layout", fake_pdf)
    return read


def assert_refusal(
    error: ParseError,
    *,
    section: str,
    row_index: int,
    column: str | None,
    message: str,
    absent: tuple[str, ...] = (),
) -> None:
    """``error`` is the exact fixed refusal at this location, with an empty raw
    value, and prints none of ``absent``, ignoring case (#1041)."""
    assert (error.section, error.row_index, error.column, error.message, error.raw_value) == (
        section,
        row_index,
        column,
        message,
        "",
    )
    rendered = str(error).casefold()
    for value in absent:
        assert value.casefold() not in rendered, value
