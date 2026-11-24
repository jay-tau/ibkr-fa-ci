"""The ``netbenefits-fa`` pipeline tests' config, price clock, session price
fetcher, Quicko template writer, summary lines and Dec-31 close refusal routes
that several test modules share. Every value is synthetic.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZipFile

from openpyxl import Workbook

from ibkr_fa.models import FaA3PeakBlankReason
from ibkr_fa.output.netbenefits_quicko_fa_xlsx import HEADERS as QUICKO_HEADERS
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.prices.yahoo import DailyCloseFetcher
from ibkr_fa.reference.us_market_calendar import us_market_sessions
from tests._dec31_close import MARKET_SESSION_REFUSAL, OVERRIDE_SESSION_REFUSAL
from tests._fa_blank_texts import A3_PEAK_BLANK_TEXT
from tests._netbenefits import INSTITUTION_TOML, PARTICIPANT_TOML, SECURITY_TOML

# The ``netbenefits-fa`` tests' config: the NetBenefits account opened on
# Dec-31-2025, and a Dec-31 price override.
BASE_CONFIG = f"""\
[account]
number = "U1234567"
holder_name = "Sample Holder"
opening_date = 2024-02-01
status = "BeneficialOwner"

{PARTICIPANT_TOML}
{INSTITUTION_TOML}account_opening_date = 2025-12-31
status = "BeneficialOwner"

{SECURITY_TOML}
[[netbenefits.price_overrides]]
price_date = 2025-12-31
price_per_share_usd = "213.47"
source = "SPS year-end statement"
reason = "12-31 lot unpriced in CSV"
"""

# The ``price_now`` the tests pass beside an injected price fetcher.
PRICE_NOW = datetime(2026, 6, 1, tzinfo=dt.UTC)


def session_fetcher(*, dec30: str = "213.47", dec31: str | None = "213.47") -> DailyCloseFetcher:
    """Closes of 213.47 on the US market sessions in the requested range only,
    except ``dec30`` on Dec-30-2025 and ``dec31`` on Dec-31-2025, or no Dec-31
    bar when ``dec31`` is ``None`` (#1165)."""

    def fetch(ticker, *, start, end, now, fetch_symbol=None):
        del fetch_symbol
        closes = dict.fromkeys(us_market_sessions(start, end), Decimal("213.47"))
        closes[dt.date(2025, 12, 30)] = Decimal(dec30)
        del closes[dt.date(2025, 12, 31)]
        if dec31 is not None:
            closes[dt.date(2025, 12, 31)] = Decimal(dec31)
        return PriceSeries(
            ticker=ticker,
            currency="USD",
            closes=closes,
            has_split_in_range=False,
            source="stub",
            fetched_at=now,
        )

    return fetch


def write_quicko_template(
    path: Path,
    headers: tuple[str, ...] = QUICKO_HEADERS,
    *,
    edit: Callable[[Workbook], None] | None = None,
) -> None:
    """Write a fillable template; ``edit`` changes the workbook before it is saved."""
    workbook = Workbook()
    instructions = workbook.active
    assert instructions is not None
    instructions.title = "Instructions"
    instructions["B32"] = "United States Of America"
    instructions["B33"] = "United Kingdom Of Great Britain And Northern Ireland"
    worksheet = workbook.create_sheet("Shares & Securities")
    worksheet.append(headers)
    for row in (2, 3):
        for column in range(1, len(headers) - 1):
            worksheet.cell(row, column).number_format = "@"
    if edit is not None:
        edit(workbook)
    workbook.save(path)

    with ZipFile(path) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}
    sheet = payloads["xl/worksheets/sheet2.xml"].decode("utf-8")
    for index, _header in enumerate(headers):
        column = chr(ord("A") + index)
        pattern = rf'<c r="{column}1"[^>]*t="inlineStr">.*?</c>'
        replacement = f'<c r="{column}1" t="s"><v>{index}</v></c>'
        sheet = re.sub(pattern, replacement, sheet, count=1)
    payloads["xl/worksheets/sheet2.xml"] = sheet.encode("utf-8")
    shared = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(headers)}" uniqueCount="{len(headers)}">'
        + "".join(f"<si><t>{escape(header)}</t></si>" for header in headers)
        + "</sst>"
    )
    content_types = (
        payloads["[Content_Types].xml"]
        .decode("utf-8")
        .replace(
            "</Types>",
            '<Override PartName="/xl/sharedStrings.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.sharedStrings+xml"/></Types>',
        )
    )
    relationships = (
        payloads["xl/_rels/workbook.xml.rels"]
        .decode("utf-8")
        .replace(
            "</Relationships>",
            '<Relationship Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/sharedStrings" Target="sharedStrings.xml" '
            'Id="rId99"/></Relationships>',
        )
    )
    payloads["[Content_Types].xml"] = content_types.encode("utf-8")
    payloads["xl/_rels/workbook.xml.rels"] = relationships.encode("utf-8")
    temporary = path.with_suffix(".tmp")
    with ZipFile(temporary, "w") as output:
        for info in infos:
            output.writestr(info, payloads[info.filename])
        output.writestr("xl/sharedStrings.xml", shared)
    temporary.replace(path)


# #1330: the summary lines a run without [netbenefits.security].entity_zip adds.
A3_ZIP_LINE = (
    "  A3 ZIP Code: MANUAL-FILL — set [netbenefits.security].entity_zip to the issuer's "
    "postal code, verified from authoritative issuer material, and re-run, or fill it in "
    "by hand before filing."
)
QUICKO_ZIP_LINE = (
    "  1 Quicko ZIP Code value(s): MANUAL-FILL — add the issuer postal code before upload."
)


def nb_peak_line(reason: FaA3PeakBlankReason, *, label: str = "A3", rows: str = "1") -> str:
    """NetBenefits' line for the A3 peaks ``reason`` leaves blank in ``rows``:
    the reason's shared cause and remedy (#1549)."""
    cause, remedy = A3_PEAK_BLANK_TEXT[reason]
    return (
        f"  {label} Peak value(s) for row(s) {rows}: MANUAL-FILL — the peak is blank because "
        f"{cause}. {remedy}"
    )


# The routes NetBenefits gives its Dec-31 close refusals, for CY 2025, whose
# required US market session is 2025-12-31 (#1549).
NB_BY_HAND = "complete the NetBenefits Schedule FA by hand"
_FMV_PER_SHARE = (
    "the purchase confirmation's Market Value at Purchase divided by the shares purchased, "
    "to the cent"
)
NB_MARKET_SESSION_REFUSAL = (
    f"{MARKET_SESSION_REFUSAL}; the required session is 2025-12-31: move this security's "
    "price cache file, and any .lock beside it, out of the price cache directory and rerun "
    "with price fetching on; if the fresh series still has no usable close for that "
    f"session, {NB_BY_HAND}"
)
NB_OVERRIDE_SESSION_REFUSAL = (
    f"{OVERRIDE_SESSION_REFUSAL}; set that [[netbenefits.price_overrides]] entry's "
    "price_date to 2025-12-31, the required session, and its price_per_share_usd to "
    f"{_FMV_PER_SHARE}; if the rounded value is refused as price_per_share_usd, or the "
    f"market close then differs, {NB_BY_HAND}"
)
NB_MISMATCH_REFUSAL = (
    "Dec-31 close mismatch: the override and yahoo_fallback totals differ by more than the "
    "tolerance; refusing rather than file an unverified closing value; check the "
    "price_per_share_usd of the [[netbenefits.price_overrides]] entry dated 2025-12-31 "
    f"against {_FMV_PER_SHARE}, and correct it if it differs; if the rounded value is "
    "refused as price_per_share_usd, or it matches and the market close still differs, "
    f"{NB_BY_HAND}"
)
# After a close from an override that differs from the confirmation FMV.
NB_OVERRIDE_ANCHOR_ROUTE = (
    "set the price_per_share_usd of the [[netbenefits.price_overrides]] entry for this "
    f"close to {_FMV_PER_SHARE}; if the rounded value is refused as price_per_share_usd, "
    f"or the market close then differs, {NB_BY_HAND}"
)
# With no override and no price series (#1565): under the close basis, and
# under peak_basis = "high" after a fetch and with --no-fetch-prices.
NB_NO_CLOSE_REFUSAL = (
    "no Dec-31 close available for 2025-12-31: there is no statement total, no override "
    "and no usable Yahoo close; supply a [[netbenefits.price_overrides]] entry for "
    "2025-12-31"
)
_NB_HIGH_PEAK = (
    'the A3 peak under [schedule_fa] peak_basis = "high" needs that day\'s intraday high, '
    "which only a price series supplies"
)
NB_NO_CLOSE_FETCHED_HIGH_REFUSAL = (
    f"{NB_NO_CLOSE_REFUSAL}, then enter the A3 peak by hand: {_NB_HIGH_PEAK}"
)
NB_NO_CLOSE_NOT_FETCHED_HIGH_REFUSAL = (
    "no Dec-31 close available for 2025-12-31: there is no statement total, no override "
    "and no usable Yahoo close; rerun without --no-fetch-prices: "
    f"{_NB_HIGH_PEAK}, and the fetched series gives the close too"
)
