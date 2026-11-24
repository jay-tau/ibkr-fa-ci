"""Shared Robinhood capital-gains results built through the real CG engine (#752, #1383)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

from ibkr_fa.compute.robinhood_capital_gains import (
    RobinhoodCapitalGainLot,
    RobinhoodCapitalGainsResult,
)
from ibkr_fa.compute.robinhood_lots import RobinhoodLotSlice, RobinhoodLotSliceKind
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionCapitalGainBucket,
    RobinhoodOptionCapitalGainLine,
    RobinhoodOptionsResult,
)
from ibkr_fa.compute.schedule_cg import compute_schedule_cg_stcg_lines
from ibkr_fa.compute.schedule_cg_ltcg import compute_schedule_cg_ltcg_lines
from ibkr_fa.models import BasisProvenance
from ibkr_fa.models.closed_lot import ClosedLot
from ibkr_fa.normalize.robinhood.security import RobinhoodSecurityEventReference
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._fx_stubs import AuditedFxStub
from tests._robinhood_refusals import EVENT_REFERENCES

# The privacy-safe labels of a typical two-account bundle.
ACCOUNT_LABELS = {0: "Individual", 1: "Managed Individual"}
# One USD is ₹100, so a sale's INR amounts are its USD amounts times 100.
INR_PER_USD = Decimal("100")
SALE_DATE = date(2025, 6, 10)
# The route line a blocked filing's refusal prints for each kind an input can
# reach (#1555). ``{sales}`` is the sales on an unreviewed venue: ``sale``'s
# default lot is sold by row 0:2.
FILING_BY_HAND = (
    "schedule-cg.xlsx isn't written while this stands, so enter Robinhood's equity "
    "Schedule CG by hand"
)
FILING_VENUE_ROUTE = (
    "Robinhood's filing position has reviewed only ARCA, NASDAQ and NYSE listings. "
    f"{EVENT_REFERENCES}{{sales}}. If a "
    "sold security's [[robinhood.securities]] entry sets the wrong listing_exchange, correct "
    "it to the exchange that actually lists the security, never just to get past this "
    f"check, and re-run. Otherwise, {FILING_BY_HAND}."
)
FILING_LTCG_ROUTE = (
    "No setting records the section 54F decision, which depends on facts outside the "
    f"Robinhood evidence. {FILING_BY_HAND}, claiming any section 54F deduction."
)
# The fixed line a CG blocker refusal starts with.
CG_UNRESOLVED = "Robinhood equity capital gains contain unresolved blockers"
# The causes a CG refusal prints for a sale before the 2024-07-23 cutover, a
# security class it doesn't compute and lots that don't cover the shares.
# Fill ``sales`` with what follows "sales "; a shortfall's ``references``
# start with their own kind.
PRE_CUTOVER_CAUSE = (
    "Sales made before 2024-07-23 fall under the earlier holding-period rules, and gains "
    f"under them aren't computed. {EVENT_REFERENCES}sales {{sales}}. {FILING_BY_HAND}, "
    "computing those sales' gains under the earlier rules."
)
UNSUPPORTED_CLASS_CAUSE = (
    "Robinhood equity capital gains are computed only for equity shares "
    '(asset_class "STK") of a non-Indian issuer with a non-Indian ISIN, and these sales\' '
    "configured securities are of a class the tool doesn't compute. "
    f"{EVENT_REFERENCES}sales {{sales}}: {{conditions}}. {FILING_BY_HAND}."
)
LOT_SHORTFALL_CAUSE = (
    "The lots from the activity report don't cover the shares of a security it sold or "
    "moved, or don't add up to the shares it still held. "
    f"{EVENT_REFERENCES}{{references}}. {FILING_BY_HAND}."
)
# What a blocked filing's refusal prints for each blocker kind (#1047).
FILING_VENUE_KIND = "a lot is listed on a foreign exchange the filing position hasn't reviewed"
FILING_CLASS_KIND = "a lot is in a security class the filing position hasn't reviewed"
FILING_LTCG_KIND = "positive LTCG requires a reviewed section 54F deduction decision"
# The section 50CA note robinhood-cg's refusal ends with while any sale is
# resolved.
FILING_50CA_NOTE = (
    "Robinhood foreign exchange-listed securities are reported in the Schedule CG field for "
    "assets other than unquoted shares. Section 50CA prescribed FMV was not computed; no CBDT "
    "guidance or binding ruling directly resolves this classification."
)
# The lot engine's same-day cause; fill ``moved`` with what the blocker's
# lots were taken by. Its route starts with SAME_DAY_EVIDENCE.
SAME_DAY_LOT_CAUSE = (
    "Robinhood's activity report gives each trade and transfer a date but no time, and the "
    "tool doesn't resolve which lots these {moved} took when a sale or transfer "
    "took part of the lots acquired on one day at different costs per share, or shares moved "
    "between the two accounts on a day one of them also sold or moved shares of that "
    "security."
)
SAME_DAY_EVIDENCE = "Robinhood's trade confirmations give each trade's execution time."
# The same-day lot cause and route a CG refusal prints around its sale
# references (#1546).
SAME_DAY_CAUSE = SAME_DAY_LOT_CAUSE.format(moved="sales") + " "
SAME_DAY_ROUTE = (
    f". {SAME_DAY_EVIDENCE} "
    f"{FILING_BY_HAND}, matching the lots to the sales first in, first out, in the order the "
    "trades and transfers ran."
)
# robinhood-income's FTC line while a CG blocker stops its schedules (#1545).
CG_FTC_CAUSE = (
    "Schedule FSI, Schedule TR and Form 67 include the Robinhood equity capital gains, "
    "which are blocked as below; while they stay blocked, complete Schedule FSI, "
    "Schedule TR and Form 67 by hand"
)
# The filing position each section sheet's tax position note ends with (#1383).
CONSIDERATION_POSITION = (
    "Robinhood's recorded sale Amount is filed as each sale's full value of consideration, "
    "with 0 transfer expenditure (owner decision, 2026-11-14). Robinhood passes FINRA TAF "
    "and SEC fees on sales but doesn't say whether Amount is before or after them. If a fee "
    "is inside Amount, consideration is lower by that fee and the gain is still right. If a "
    "fee was collected outside Amount, the gain is higher by that fee: slightly more tax, "
    "never less."
)
# The Basis of (a)(ii) and (b)(iii): both point to the tax position note (#1383).
CONSIDERATION_BASIS = (
    "Exact Robinhood recorded sale Amount converted at transfer-month TTBR; the tax "
    "position note above says why."
)
TRANSFER_EXPENDITURE_BASIS = (
    "No fee is inferred separately from Robinhood's exact recorded Amount; the tax "
    "position note above says why."
)
# The capital-gains refusal for a sale whose proven gross differs from its
# recorded Amount (#752, #1383); its event references follow.
PROVEN_GROSS_REFUSAL = (
    "a Robinhood sale's proven gross differs from its recorded Amount, and no "
    "transfer-expenditure path files the difference."
)
_SHORT_TERM_OPEN = date(2025, 5, 1)
_LONG_TERM_OPEN = date(2023, 1, 1)


def sale(
    cost_usd: str,
    proceeds_usd: str,
    *,
    long_term: bool = False,
    sale_date: date = SALE_DATE,
    account_index: int = 0,
    cusip: str = "99972B101",
    country: str = "US",
    lot_ordinal: int = 1,
    sale_ordinal: int = 2,
) -> RobinhoodCapitalGainLot:
    """One FIFO slice of a sale, lot ``lot_ordinal`` sold by event ``sale_ordinal``.

    Its ``gross_proceeds_money`` is its recorded Amount, as every Robinhood
    lot's is (#1383).
    """
    open_date = _LONG_TERM_OPEN if long_term else _SHORT_TERM_OPEN
    proceeds = Decimal(proceeds_usd)
    closed_lot = ClosedLot(
        currency_primary="USD",
        sub_category="COMMON",
        symbol=Ticker("XQCA"),
        description="SAMPLE ISSUER CA",
        isin=Isin("US99972B1017" if country == "US" else f"{country}99970J1065"),
        listing_exchange="NASDAQ",
        issuer_country_code=CountryCode(country),
        quantity=Decimal("1"),
        cost_basis_money=Decimal(cost_usd),
        proceeds_money=proceeds,
        open_date_time=open_date,
        close_date_time=sale_date,
        gross_proceeds_money=proceeds,
        basis_provenance=BasisProvenance.ENGINE_FIFO,
    )
    source_slice = RobinhoodLotSlice(
        lot_id=RobinhoodSecurityEventReference(account_index, lot_ordinal),
        account_index=account_index,
        cusip=cusip,
        movement_references=(RobinhoodSecurityEventReference(account_index, sale_ordinal),),
        quantity=closed_lot.quantity,
        cost_usd=closed_lot.cost_basis_money,
        original_date=open_date,
        entry_date=open_date,
        kind=RobinhoodLotSliceKind.DISPOSAL,
    )
    return RobinhoodCapitalGainLot(source_slice, closed_lot)


def cg_result(*lots: RobinhoodCapitalGainLot) -> RobinhoodCapitalGainsResult:
    """A blocker-free FY 2025-26 result whose lines the real engine computes from ``lots``."""
    closed_lots = [item.closed_lot for item in lots]
    start, end, fx_policy = date(2025, 4, 1), date(2026, 3, 31), AuditedFxStub(INR_PER_USD)
    return RobinhoodCapitalGainsResult(
        stcg=compute_schedule_cg_stcg_lines(
            closed_lots,
            financial_year_start=start,
            financial_year_end=end,
            fx_policy=fx_policy,
        ),
        ltcg=compute_schedule_cg_ltcg_lines(
            closed_lots,
            financial_year_start=start,
            financial_year_end=end,
            fx_policy=fx_policy,
        ),
        attributed_lots=lots,
        blockers=(),
        excluded_out_of_fy_disposal_references=(),
        sale_price_checks=(),
    )


def option_lines(
    *buckets: RobinhoodOptionCapitalGainBucket,
) -> tuple[RobinhoodOptionCapitalGainLine, ...]:
    """Stand-in option capital-gain lines, one per bucket in ``buckets``.

    The CG filing reads only each line's bucket (#1322).
    """
    return cast(
        "tuple[RobinhoodOptionCapitalGainLine, ...]",
        tuple(SimpleNamespace(bucket=bucket) for bucket in buckets),
    )


# A stand-in options result for a run with no option capital-gain lines.
NO_OPTIONS = cast("RobinhoodOptionsResult", SimpleNamespace(capital_gain_lines=()))
