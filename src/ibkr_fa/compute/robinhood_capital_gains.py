"""Convert reconciled Robinhood equity disposals into Schedule CG results."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal, localcontext
from enum import StrEnum
from fractions import Fraction
from typing import Final

from ibkr_fa.compliance.schedule_cg_filing_position import ROBINHOOD_SCHEDULE_CG_FILING_POSITION
from ibkr_fa.compute.exact_inr import review_amount
from ibkr_fa.compute.robinhood_inputs import (
    holds_two_accounts,
    is_one_financial_year,
    same_distinct_account_numbers,
)
from ibkr_fa.compute.robinhood_lots import (
    LOT_BLOCKER_CAUSES,
    SAME_DAY_EVIDENCE,
    RobinhoodLotBlocker,
    RobinhoodLotBlockerKind,
    RobinhoodLotSlice,
    RobinhoodLotSliceKind,
    RobinhoodSecurityLotResult,
    allocate_disposal_proceeds,
    reconcile_robinhood_security_lots,
)
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionCapitalGainBucket,
    RobinhoodOptionCapitalGainLine,
)
from ibkr_fa.compute.robinhood_security_identities import RobinhoodSecurityIdentities
from ibkr_fa.compute.schedule_cg import (
    ScheduleCgStcgComputeResult,
    compute_schedule_cg_stcg_lines,
)
from ibkr_fa.compute.schedule_cg_filing import (
    ScheduleCgFilingBasis,
    ScheduleCgFilingResult,
    ScheduleCgFilingRoutes,
    build_schedule_cg_filing_result,
    schedule_cg_filing_blocked_text,
    schedule_cg_filing_blockers,
)
from ibkr_fa.compute.schedule_cg_ltcg import (
    ScheduleCgLtcgComputeResult,
    compute_schedule_cg_ltcg_lines,
)
from ibkr_fa.compute.schedule_cg_portal_amounts import require_schedule_cg_filed_amounts
from ibkr_fa.config import RobinhoodConfig, RobinhoodSecurityConfig
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.policy import AuditedFxPolicy
from ibkr_fa.invariants import invariant_error
from ibkr_fa.models.closed_lot import BasisProvenance, ClosedLot
from ibkr_fa.models.schedule_cg_ltcg_row import NEW_REGIME_CUTOVER_DATE
from ibkr_fa.models.schedule_cg_row import is_short_term_holding
from ibkr_fa.normalize.robinhood.accounts import (
    RobinhoodAccountsReconciliation,
)
from ibkr_fa.normalize.robinhood.activity import robinhood_half_ulp
from ibkr_fa.normalize.robinhood.inventory import (
    RobinhoodSecurityQuantityResult,
    resolve_robinhood_security_quantities,
)
from ibkr_fa.normalize.robinhood.security import (
    RobinhoodSecurityEventReference,
    RobinhoodSecurityIdentityBlocker,
    resolve_robinhood_security_identities,
)
from ibkr_fa.parser.robinhood import ROBINHOOD_EQUITY_SALE_KINDS, RobinhoodEvent

_ARITHMETIC_CONTEXT = Context(prec=60)

type _GroupKey = tuple[int, str, date]

# The Robinhood evidence claims the filing workbook's Basis cells make
# (owner decision 4 on #642).
_FILING_BASIS: Final[ScheduleCgFilingBasis] = ScheduleCgFilingBasis(
    consideration=(
        "Exact Robinhood recorded sale Amount converted at transfer-month TTBR; "
        "the tax position note above says why."
    ),
    cost="FIFO USD basis converted at the same transfer-month TTBR.",
    transfer_expenditure=(
        "No fee is inferred separately from Robinhood's exact recorded Amount; "
        "the tax position note above says why."
    ),
    section_94_7_8=(
        "Foreign distributions are taxable; the accepted Robinhood evidence "
        "contains no bonus allotment."
    ),
    schedule_dcg="Schedule DCG and sections 54G/54GA are nil for Robinhood securities.",
    # ADR 0006 section 7 (owner decision 2026-11-14, #1383).
    consideration_position=(
        "Robinhood's recorded sale Amount is filed as each sale's full value of "
        "consideration, with 0 transfer expenditure (owner decision, 2026-11-14). "
        "Robinhood passes FINRA TAF and SEC fees on sales but doesn't say whether "
        "Amount is before or after them. If a fee is inside Amount, consideration "
        "is lower by that fee and the gain is still right. If a fee was collected "
        "outside Amount, the gain is higher by that fee: slightly more tax, never less."
    ),
    covered_disposals="equity disposals",
    # ADR 0006 sends option capital gains to the same fields, by hand (#1322).
    uncovered_disposals=(
        "Robinhood option capital gains are not in this workbook; enter them by "
        "hand from robinhood-options.csv, which robinhood-options and robinhood-all write"
    ),
    table_f_other_files="other brokers, robinhood-options.csv's option gains, and Indian sales",
)


class RobinhoodCapitalGainsError(IbkrFaError):
    """Reconciled Robinhood evidence cannot produce a certain CG result."""


class RobinhoodCapitalGainsBlockerKind(StrEnum):
    """The unresolved fact withholding one or all current-FY sale groups."""

    EVIDENCE_COVERAGE = "evidence-coverage"
    IDENTITY = "identity"
    LOT_ATTRIBUTION = "lot-attribution"
    ORDERING_AMBIGUITY = "ordering-ambiguity"
    PRE_CUTOVER_HOLDING_RULE = "pre-cutover-holding-rule"
    UNSUPPORTED_SECURITY_CLASS = "unsupported-security-class"


# The kinds whose sale groups stop before any later check (#1570).
_UNRESOLVED_GROUP_KINDS: Final = frozenset(
    {RobinhoodCapitalGainsBlockerKind.IDENTITY, RobinhoodCapitalGainsBlockerKind.LOT_ATTRIBUTION}
)


class RobinhoodUnsupportedSecurityCondition(StrEnum):
    """Which part of ADR 0006's reviewed equity class a configured security fails."""

    ASSET_CLASS = "asset-class"
    ISSUER_COUNTRY = "issuer-country"
    ISIN = "isin"


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodCapitalGainsBlocker:
    """One privacy-safe CG blocker with resolvable source handles."""

    kind: RobinhoodCapitalGainsBlockerKind
    account_index: int | None
    disposal_references: tuple[RobinhoodSecurityEventReference, ...] = ()
    lot_ids: tuple[RobinhoodSecurityEventReference, ...] = ()
    source_lot_blockers: tuple[RobinhoodLotBlocker, ...] = ()
    source_identity_blockers: tuple[RobinhoodSecurityIdentityBlocker, ...] = ()
    unsupported_conditions: tuple[RobinhoodUnsupportedSecurityCondition, ...] = ()
    """For ``UNSUPPORTED_SECURITY_CLASS``, each reviewed condition the security fails."""


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodSalePriceCheck:
    """Whether one sale's source cash can equal its displayed price product."""

    event_reference: RobinhoodSecurityEventReference
    source_cash_within_display_price_band: bool


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodCapitalGainLot:
    """One emitted generic lot tied back to its complete source allocation."""

    source_slice: RobinhoodLotSlice
    closed_lot: ClosedLot


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodCapitalGainsResult:
    """Writer-ready schedules plus provenance, exclusions, and typed blockers."""

    stcg: ScheduleCgStcgComputeResult
    ltcg: ScheduleCgLtcgComputeResult
    attributed_lots: tuple[RobinhoodCapitalGainLot, ...]
    blockers: tuple[RobinhoodCapitalGainsBlocker, ...]
    excluded_out_of_fy_disposal_references: tuple[tuple[RobinhoodSecurityEventReference, ...], ...]
    sale_price_checks: tuple[RobinhoodSalePriceCheck, ...]

    def __post_init__(self) -> None:
        # Each lot's recorded Amount is its gross (#1383), so a lot without one
        # lost it; no path leaves its consideration for manual entry any more.
        if any(item.closed_lot.gross_proceeds_money is None for item in self.attributed_lots):
            raise invariant_error(
                what="a Robinhood capital-gain lot has no gross proceeds; each lot "
                "files its recorded Amount as its gross"
            )
        # #752: a proven sale charge would be transfer expenditure, which no
        # path files yet; refuse rather than drop the difference.
        differing = [
            item
            for item in self.attributed_lots
            if (item.closed_lot.gross_proceeds_money, item.closed_lot.gross_proceeds_money_exact)
            != (item.closed_lot.proceeds_money, item.closed_lot.proceeds_money_exact)
        ]
        if differing:
            raise _fail(
                "a Robinhood sale's proven gross differs from its recorded Amount, and no "
                f"transfer-expenditure path files the difference. {_SALE_REFERENCES}"
                f"{_sale_references(differing)}"
            )

    def filing(
        self, option_lines: tuple[RobinhoodOptionCapitalGainLine, ...]
    ) -> ScheduleCgFilingResult | None:
        """The Schedule CG filing result; ``None`` while :attr:`blockers` withhold the schedules.

        ``option_lines`` are the run's option capital-gain lines. The
        workbook leaves them out, so a section their buckets name says it
        needs manual entry instead of none (#1322).

        Built on each call, like the engines' ``portal_amounts``, so its
        refusals stay with the filing workbook rather than the other
        schedules that read this result. Its own blockers stay on
        ``filing.blockers``.
        """
        if self.blockers:
            return None
        buckets = {line.bucket for line in option_lines}
        return build_schedule_cg_filing_result(
            self.stcg,
            self.ltcg,
            (item.closed_lot for item in self.attributed_lots),
            position=ROBINHOOD_SCHEDULE_CG_FILING_POSITION,
            basis=_FILING_BASIS,
            stcg_uncovered_disposals=RobinhoodOptionCapitalGainBucket.STCG_OTHER_ASSETS in buckets,
            ltcg_uncovered_disposals=RobinhoodOptionCapitalGainBucket.LTCG_OTHER_ASSETS in buckets,
            routes=_filing_routes(self.attributed_lots),
        )


def _sale_references(items: list[RobinhoodCapitalGainLot]) -> str:
    """Name the sales of ``items``, in sale-date, account and CUSIP order, by
    their source movement references, never by symbol, CUSIP or date (#1044);
    FIFO slices of one sale collapse to one entry."""
    ordered = sorted(
        items,
        key=lambda item: (
            item.closed_lot.close_date_time,
            item.source_slice.account_index,
            item.source_slice.cusip,
            item.source_slice.movement_references,
        ),
    )
    return "; ".join(
        dict.fromkeys(
            ", ".join(
                f"{reference.account_index}:{reference.event_ordinal}"
                for reference in item.source_slice.movement_references
            )
            for item in ordered
        )
    )


@dataclass(frozen=True, slots=True, repr=False)
class _SaleGroup:
    account_index: int
    cusip: str
    activity_date: date
    items: tuple[tuple[RobinhoodSecurityEventReference, RobinhoodEvent], ...]

    @property
    def references(self) -> tuple[RobinhoodSecurityEventReference, ...]:
        return tuple(reference for reference, _event in self.items)


def _fail(message: str) -> RobinhoodCapitalGainsError:
    return RobinhoodCapitalGainsError(message)


def _validate_inputs(
    paired: RobinhoodAccountsReconciliation,
    config: RobinhoodConfig,
    financial_year_start: date,
    financial_year_end: date,
) -> None:
    if not is_one_financial_year(financial_year_start, financial_year_end):
        raise _fail("capital gains require one complete April-to-March financial year")
    if not holds_two_accounts(paired, config):
        raise _fail("capital gains require the reconciled two-account Robinhood bundle")
    if not same_distinct_account_numbers(paired, config):
        raise _fail("capital-gains config does not match the reconciled account identities")


def _sale_fields(event: RobinhoodEvent) -> tuple[str, Decimal, Decimal, Decimal]:
    if (
        event.kind not in ROBINHOOD_EQUITY_SALE_KINDS
        or event.cusip is None
        or event.quantity is None
        or event.price_usd is None
        or event.amount_usd is None
        or not event.quantity.is_finite()
        or not event.price_usd.is_finite()
        or not event.amount_usd.is_finite()
        or event.quantity <= 0
        or event.price_usd <= 0
        or event.amount_usd <= 0
    ):
        raise _fail("resolved sale event lacks valid quantity, price, cash, or identity")
    return event.cusip, event.quantity, event.price_usd, event.amount_usd


def _source_cash_within_display_price_band(event: RobinhoodEvent) -> bool:
    _cusip, quantity, price, amount = _sale_fields(event)
    quantity_band = robinhood_half_ulp(quantity)
    price_band = robinhood_half_ulp(price)
    amount_band = robinhood_half_ulp(amount)
    product_low = (quantity - quantity_band) * (price - price_band)
    product_high = (quantity + quantity_band) * (price + price_band)
    return product_low <= amount + amount_band and amount - amount_band <= product_high


def robinhood_sale_price_checks(
    paired: RobinhoodAccountsReconciliation,
) -> tuple[RobinhoodSalePriceCheck, ...]:
    """Check every sale in the bundle, in any year, against its displayed price band.

    Needs no FX and no lots. Runs at this module's arithmetic precision
    whatever the caller's context, since the band edges depend on it.
    """
    with localcontext(_ARITHMETIC_CONTEXT):
        checks = [
            RobinhoodSalePriceCheck(
                event_reference=RobinhoodSecurityEventReference(
                    account_index, event.source_record_index
                ),
                source_cash_within_display_price_band=(
                    _source_cash_within_display_price_band(event)
                ),
            )
            for account_index, account in enumerate(paired.accounts)
            for event in account.evidence.events
            if event.kind in ROBINHOOD_EQUITY_SALE_KINDS
        ]
    return tuple(sorted(checks, key=lambda check: check.event_reference))


def _source_groups(
    paired: RobinhoodAccountsReconciliation,
) -> tuple[
    dict[RobinhoodSecurityEventReference, RobinhoodEvent],
    tuple[_SaleGroup, ...],
    tuple[RobinhoodSalePriceCheck, ...],
]:
    event_map: dict[RobinhoodSecurityEventReference, RobinhoodEvent] = {}
    grouped: dict[_GroupKey, list[tuple[RobinhoodSecurityEventReference, RobinhoodEvent]]] = (
        defaultdict(list)
    )
    for account_index, account in enumerate(paired.accounts):
        for event in account.evidence.events:
            reference = RobinhoodSecurityEventReference(account_index, event.source_record_index)
            if reference in event_map:
                raise _fail("reconciled activity contains a duplicate event reference")
            event_map[reference] = event
            if event.kind not in ROBINHOOD_EQUITY_SALE_KINDS:
                continue
            cusip, _quantity, _price, _amount = _sale_fields(event)
            grouped[(account_index, cusip, event.activity_date)].append((reference, event))

    groups = tuple(
        _SaleGroup(
            account_index=key[0],
            cusip=key[1],
            activity_date=key[2],
            items=tuple(sorted(items, key=lambda item: item[0])),
        )
        for key, items in sorted(grouped.items())
    )
    return event_map, groups, robinhood_sale_price_checks(paired)


def _validate_references(
    event_map: dict[RobinhoodSecurityEventReference, RobinhoodEvent],
    lot_result: RobinhoodSecurityLotResult,
) -> None:
    references = {
        reference
        for item in lot_result.slices
        for reference in (
            item.lot_id,
            *item.movement_references,
            *item.residual_references,
        )
    } | {
        reference
        for blocker in lot_result.blockers
        for reference in (*blocker.event_references, *blocker.lot_ids)
    }
    if not references <= event_map.keys():
        raise _fail("lot result contains a source reference outside the paired reconciliation")


def _identity_index_by_reference(
    paired: RobinhoodAccountsReconciliation,
    securities: tuple[RobinhoodSecurityConfig, ...],
    lot_result: RobinhoodSecurityLotResult,
) -> tuple[
    dict[RobinhoodSecurityEventReference, int],
    tuple[RobinhoodSecurityIdentityBlocker, ...],
]:
    identities = resolve_robinhood_security_identities(paired, securities)
    if identities.blockers != lot_result.identity_blockers:
        raise _fail("lot result and current security identity resolution disagree")
    by_reference: dict[RobinhoodSecurityEventReference, int] = {}
    for resolved in identities.resolved:
        for reference in resolved.event_references:
            if reference in by_reference:
                raise _fail("one source event resolves to multiple configured securities")
            by_reference[reference] = resolved.config_index
    return by_reference, identities.blockers


def _lot_blockers_for(
    group: _SaleGroup,
    blockers: tuple[RobinhoodLotBlocker, ...],
    event_map: dict[RobinhoodSecurityEventReference, RobinhoodEvent],
) -> tuple[RobinhoodLotBlocker, ...]:
    matched: list[RobinhoodLotBlocker] = []
    for blocker in blockers:
        if blocker.cusip is not None:
            if blocker.cusip == group.cusip:
                matched.append(blocker)
            continue
        references = (*blocker.event_references, *blocker.lot_ids)
        if not references:
            # ponytail: an identity-free upstream blocker stays global; add
            # disposal-scoped recovery only if lots.py retains that evidence.
            matched.append(blocker)
            continue
        if any(
            reference in group.references or event_map[reference].cusip in {None, group.cusip}
            for reference in references
        ):
            matched.append(blocker)
    return tuple(matched)


def _identity_blockers_for(
    group: _SaleGroup,
    blockers: tuple[RobinhoodSecurityIdentityBlocker, ...],
) -> tuple[RobinhoodSecurityIdentityBlocker, ...]:
    references = set(group.references)
    return tuple(
        blocker for blocker in blockers if references.intersection(blocker.event_references)
    )


def _validate_slice(
    item: RobinhoodLotSlice,
    group: _SaleGroup,
    event_map: dict[RobinhoodSecurityEventReference, RobinhoodEvent],
    identity_by_reference: dict[RobinhoodSecurityEventReference, int],
    config_index: int,
) -> None:
    lot_event = event_map[item.lot_id]
    if (
        item.kind is not RobinhoodLotSliceKind.DISPOSAL
        or item.account_index != group.account_index
        or item.cusip != group.cusip
        or item.movement_references != group.references
        or not item.quantity.is_finite()
        or not item.cost_usd.is_finite()
        or item.quantity <= 0
        or item.cost_usd < 0
        or lot_event.cusip != group.cusip
        or lot_event.activity_date != item.original_date
        or not (item.original_date <= item.entry_date <= group.activity_date)
        or identity_by_reference.get(item.lot_id) != config_index
        or any(event_map[reference].cusip != group.cusip for reference in item.residual_references)
    ):
        raise _fail("resolved disposal slice contradicts its source events")


def _unsupported_conditions(
    security: RobinhoodSecurityConfig,
) -> tuple[RobinhoodUnsupportedSecurityCondition, ...]:
    # A Robinhood CUSIP plus a configured non-IN identity is the bounded
    # foreign, non-India-listed equity class reviewed by ADR 0006.
    failed = (
        (RobinhoodUnsupportedSecurityCondition.ASSET_CLASS, security.asset_class != "STK"),
        (
            RobinhoodUnsupportedSecurityCondition.ISSUER_COUNTRY,
            security.instrument.issuer_country_code == "IN",
        ),
        (
            RobinhoodUnsupportedSecurityCondition.ISIN,
            str(security.instrument.isin).startswith("IN"),
        ),
    )
    return tuple(condition for condition, fails in failed if fails)


def _sale_order_splits_terms(group: _SaleGroup, slices: tuple[RobinhoodLotSlice, ...]) -> bool:
    """Whether the order of ``group``'s sales decides its short/long-term split (#1540).

    The activity report gives each sale a date but no time, and the sale that
    ran first takes the oldest lots. When the group sells both short-term and
    long-term lots and its sales' cash per share differs, compared exactly as
    ``cash_a * quantity_b == cash_b * quantity_a``, that order moves gain
    between the two terms.
    """
    terms = {is_short_term_holding(item.original_date, group.activity_date) for item in slices}
    sales = [_sale_fields(event) for _reference, event in group.items]
    _cusip, first_quantity, _price, first_cash = sales[0]
    return len(terms) > 1 and any(
        Fraction(cash) * Fraction(first_quantity) != Fraction(first_cash) * Fraction(quantity)
        for _sale_cusip, quantity, _sale_price, cash in sales[1:]
    )


def ordering_ambiguity_reason(blockers: Iterable[RobinhoodCapitalGainsBlocker]) -> str | None:
    """The refusal line for ``ORDERING_AMBIGUITY`` blockers, naming each group's
    sales and lots by source reference (#1540); ``None`` when there are none."""
    groups = "; ".join(
        f"sales {_references(blocker.disposal_references)} (lots {_references(blocker.lot_ids)})"
        for blocker in blockers
        if blocker.kind is RobinhoodCapitalGainsBlockerKind.ORDERING_AMBIGUITY
    )
    if not groups:
        return None
    return (
        "Robinhood's activity report gives each sale a date but no time. Sales of one "
        "security on one day got different cash per share and sold both short-term and "
        "long-term lots, so the order they were executed in decides how the gain splits "
        f"between short-term and long-term. {_SALE_REFERENCES}{groups}. Robinhood's trade "
        f"confirmations give each sale's execution time. {_BY_HAND}, matching the lots, "
        "oldest first, to the sales in execution order."
    )


def _references(references: Iterable[RobinhoodSecurityEventReference]) -> str:
    return ", ".join(
        f"{reference.account_index}:{reference.event_ordinal}" for reference in references
    )


_SALE_REFERENCES = "Internal account_index:event_ordinal references (not account numbers): "
_BY_HAND = (
    "schedule-cg.xlsx isn't written while this stands, so enter Robinhood's equity "
    "Schedule CG by hand"
)


def _filing_routes(lots: tuple[RobinhoodCapitalGainLot, ...]) -> ScheduleCgFilingRoutes:
    """The filing workbook's route for each blocker ``lots`` can reach (#1555).

    The unreviewed-venue line names the sales on such a venue by source
    reference, never the venue (#1047). Positive LTCG's sales can't be
    named: its engine lines don't say which lot each came from.
    """
    *others, last = sorted(ROBINHOOD_SCHEDULE_CG_FILING_POSITION.reviewed_listing_venues)
    return ScheduleCgFilingRoutes(
        unreviewed_listing_venue=lambda positions: (
            f"Robinhood's filing position has reviewed only {', '.join(others)} and {last} "
            f"listings. {_SALE_REFERENCES}"
            f"{_sale_references([lots[index] for index in positions])}. If a sold "
            "security's [[robinhood.securities]] entry sets the wrong listing_exchange, "
            "correct it to the exchange that actually lists the security, never just to get "
            f"past this check, and re-run. Otherwise, {_BY_HAND}."
        ),
        section_54_decision_pending=(
            "No setting records the section 54F decision, which depends on facts outside "
            f"the Robinhood evidence. {_BY_HAND}, claiming any section 54F deduction."
        ),
    )


def resolved_filing_reasons(result: RobinhoodCapitalGainsResult) -> tuple[str, ...]:
    """While an identity or lot blocker withholds ``result``'s schedules, what
    the filing workbook's refusal prints for the sales already resolved, as
    one entry (#1570); empty when no sale is resolved, there is no such
    blocker, or nothing blocks those sales' filing.

    Each check reads one resolved lot or line. Fixing the blocked groups
    adds lots and lines but changes none of these: each security's lots come
    from its own FIFO run, each lot is one line, and user.toml refuses a
    second entry for a configured CUSIP, ISIN or symbol. So every stop named
    here still applies to those sales after the fix.
    """
    lots = result.attributed_lots
    if not lots or not any(item.kind in _UNRESOLVED_GROUP_KINDS for item in result.blockers):
        return ()
    blockers = schedule_cg_filing_blockers(
        result.stcg,
        result.ltcg,
        tuple(item.closed_lot for item in lots),
        position=ROBINHOOD_SCHEDULE_CG_FILING_POSITION,
        routes=_filing_routes(lots),
    )
    return (schedule_cg_filing_blocked_text(blockers),) if blockers else ()


# Each lot-engine cause's line, ``{sales}`` naming the sale groups it blocks
# (#1546): the shared cause, the references and the route.
_LOT_CAUSES: Final[dict[RobinhoodLotBlockerKind, str]] = {
    kind: (
        f"{cause.format(moved='sales')} {_SALE_REFERENCES}{{sales}}. "
        + (
            f"{SAME_DAY_EVIDENCE} {_BY_HAND}, matching the lots to the sales first in, "
            "first out, in the order the trades and transfers ran."
            if kind is RobinhoodLotBlockerKind.SAME_DAY_AMBIGUITY
            else f"{_BY_HAND}."
        )
    )
    for kind, cause in LOT_BLOCKER_CAUSES.items()
}
_UNSUPPORTED_CONDITIONS: Final[dict[RobinhoodUnsupportedSecurityCondition, str]] = {
    RobinhoodUnsupportedSecurityCondition.ASSET_CLASS: 'asset_class isn\'t "STK"',
    RobinhoodUnsupportedSecurityCondition.ISSUER_COUNTRY: 'issuer_country_code is "IN"',
    RobinhoodUnsupportedSecurityCondition.ISIN: 'ISIN starts with "IN"',
}


def _sales(
    blocker: RobinhoodCapitalGainsBlocker, lot_ids: Iterable[RobinhoodSecurityEventReference]
) -> str:
    lots = _references(lot_ids)
    return f"sales {_references(blocker.disposal_references)}" + (f" (lots {lots})" if lots else "")


def _lot_attribution_reasons(blockers: tuple[RobinhoodCapitalGainsBlocker, ...]) -> list[str]:
    """One line per lot-engine cause behind ``blockers``, naming each sale
    group it blocks and that cause's own lots. A group with an unresolved
    identity carries its lot causes too, so they print beside the identity
    guidance (#1550)."""
    reasons = []
    for kind, template in _LOT_CAUSES.items():
        groups = []
        for blocker in blockers:
            causes = [item for item in blocker.source_lot_blockers if item.kind is kind]
            if causes:
                lot_ids = sorted({lot_id for item in causes for lot_id in item.lot_ids})
                groups.append(_sales(blocker, lot_ids))
        if groups:
            reasons.append(template.format(sales="; ".join(groups)))
    return reasons


def blocker_reasons(blockers: Iterable[RobinhoodCapitalGainsBlocker]) -> tuple[str, ...]:
    """The refusal lines for ``blockers``, one per explained kind present, in kind order,
    and one per lot-engine cause any of them carries (#1546, #1550).

    Each names its sale groups and available lots by source reference, the cause
    and a route. ``IDENTITY`` adds only its lot causes here: the pipeline adds its
    guidance line. ``EVIDENCE_COVERAGE`` has none: the loader refuses short evidence first.
    """
    items = tuple(blockers)
    reasons = _lot_attribution_reasons(items)
    if (ordering := ordering_ambiguity_reason(items)) is not None:
        reasons.append(ordering)
    pre_cutover = [
        _sales(blocker, blocker.lot_ids)
        for blocker in items
        if blocker.kind is RobinhoodCapitalGainsBlockerKind.PRE_CUTOVER_HOLDING_RULE
    ]
    if pre_cutover:
        reasons.append(
            f"Sales made before {NEW_REGIME_CUTOVER_DATE.isoformat()} fall under the earlier "
            "holding-period rules, and gains under them aren't computed. "
            f"{_SALE_REFERENCES}{'; '.join(pre_cutover)}. {_BY_HAND}, computing those "
            "sales' gains under the earlier rules."
        )
    unsupported = [
        f"{_sales(blocker, blocker.lot_ids)}: "
        + ", ".join(_UNSUPPORTED_CONDITIONS[item] for item in blocker.unsupported_conditions)
        for blocker in items
        if blocker.kind is RobinhoodCapitalGainsBlockerKind.UNSUPPORTED_SECURITY_CLASS
    ]
    if unsupported:
        reasons.append(
            "Robinhood equity capital gains are computed only for equity shares "
            '(asset_class "STK") of a non-Indian issuer with a non-Indian ISIN, and these '
            "sales' configured securities are of a class the tool doesn't compute. "
            f"{_SALE_REFERENCES}{'; '.join(unsupported)}. {_BY_HAND}."
        )
    return tuple(reasons)


def _allocate_group(
    group: _SaleGroup,
    slices: tuple[RobinhoodLotSlice, ...],
    security: RobinhoodSecurityConfig,
) -> tuple[RobinhoodCapitalGainLot, ...]:
    source_quantity = sum((_sale_fields(event)[1] for _reference, event in group.items), Decimal())
    source_cash = sum((_sale_fields(event)[3] for _reference, event in group.items), Decimal())
    slice_quantity = sum((item.quantity for item in slices), Decimal())
    if source_quantity != slice_quantity:
        raise _fail("resolved sale-group quantity does not equal its source quantity")

    result: list[RobinhoodCapitalGainLot] = []
    for item, allocated_proceeds, exact_proceeds in allocate_disposal_proceeds(
        slices,
        source_quantity=source_quantity,
        source_cash=source_cash,
    ):
        cost, cost_exact = review_amount(item.cost_usd, item.exact_cost_usd)
        proceeds, proceeds_exact = review_amount(allocated_proceeds, exact_proceeds)
        event = group.items[0][1]
        if event.issuer_name is None:
            raise _fail("resolved sale event lacks issuer provenance")
        lot = ClosedLot(
            currency_primary="USD",
            sub_category=security.sub_category,
            symbol=security.instrument.symbol,
            description=event.issuer_name,
            isin=security.instrument.isin,
            listing_exchange=security.instrument.listing_exchange,
            issuer_country_code=security.instrument.issuer_country_code,
            quantity=item.quantity,
            cost_basis_money=cost,
            proceeds_money=proceeds,
            open_date_time=item.original_date,
            close_date_time=group.activity_date,
            # The recorded Amount is the sale's full value of consideration
            # (owner decision 2026-11-14, #1383; ADR 0006 section 7).
            gross_proceeds_money=proceeds,
            basis_provenance=BasisProvenance.ENGINE_FIFO,
            cost_basis_money_exact=cost_exact,
            proceeds_money_exact=proceeds_exact,
            gross_proceeds_money_exact=proceeds_exact,
        )
        result.append(RobinhoodCapitalGainLot(source_slice=item, closed_lot=lot))
    return tuple(result)


def _compute_schedules(
    attributed: tuple[RobinhoodCapitalGainLot, ...],
    *,
    financial_year_start: date,
    financial_year_end: date,
    fx_policy: AuditedFxPolicy,
) -> tuple[ScheduleCgStcgComputeResult, ScheduleCgLtcgComputeResult]:
    lots = tuple(item.closed_lot for item in attributed)
    stcg = compute_schedule_cg_stcg_lines(
        lots,
        financial_year_start=financial_year_start,
        financial_year_end=financial_year_end,
        fx_policy=fx_policy,
        strict=True,
    )
    ltcg = compute_schedule_cg_ltcg_lines(
        lots,
        financial_year_start=financial_year_start,
        financial_year_end=financial_year_end,
        fx_policy=fx_policy,
        strict=True,
    )
    require_schedule_cg_filed_amounts(stcg.stcg_lines, schedule="STCG")
    require_schedule_cg_filed_amounts(ltcg.ltcg_lines, schedule="LTCG")
    return stcg, ltcg


def _coverage_blockers(
    paired: RobinhoodAccountsReconciliation,
    config: RobinhoodConfig,
    groups: tuple[_SaleGroup, ...],
    financial_year_start: date,
    financial_year_end: date,
) -> tuple[RobinhoodCapitalGainsBlocker, ...]:
    opening_dates = {account.number: account.opening_date for account in config.accounts}
    incomplete_accounts: list[int] = []
    for account_index, account in enumerate(paired.accounts):
        evidence = account.evidence
        number = account.activity.primary_bundle.account_number
        required_start = max(financial_year_start, opening_dates[number])
        if evidence.start_date <= required_start and evidence.end_date >= financial_year_end:
            continue
        incomplete_accounts.append(account_index)
    if not incomplete_accounts:
        return ()
    return (
        RobinhoodCapitalGainsBlocker(
            kind=RobinhoodCapitalGainsBlockerKind.EVIDENCE_COVERAGE,
            account_index=(incomplete_accounts[0] if len(incomplete_accounts) == 1 else None),
            disposal_references=tuple(
                reference
                for group in groups
                if financial_year_start <= group.activity_date <= financial_year_end
                for reference in group.references
            ),
        ),
    )


def _unresolved_group_blockers(
    group: _SaleGroup,
    source_identity_blockers: tuple[RobinhoodSecurityIdentityBlocker, ...],
    source_lot_blockers: tuple[RobinhoodLotBlocker, ...],
) -> tuple[RobinhoodCapitalGainsBlocker, ...]:
    """The blockers of ``group``, whose identity or lots are unresolved: that
    one, then, for a FY 2024-25 sale before the cutover, the pre-cutover
    blocker. That stop needs only the sale date, so it stands however the
    identity or lots are fixed (#1570); it names no lots, which aren't resolved."""
    unresolved = RobinhoodCapitalGainsBlocker(
        kind=(
            RobinhoodCapitalGainsBlockerKind.IDENTITY
            if source_identity_blockers
            else RobinhoodCapitalGainsBlockerKind.LOT_ATTRIBUTION
        ),
        account_index=group.account_index,
        disposal_references=group.references,
        lot_ids=tuple(
            sorted({lot_id for blocker in source_lot_blockers for lot_id in blocker.lot_ids})
        ),
        source_lot_blockers=source_lot_blockers,
        source_identity_blockers=source_identity_blockers,
    )
    if not (
        date(NEW_REGIME_CUTOVER_DATE.year, 4, 1) <= group.activity_date < NEW_REGIME_CUTOVER_DATE
    ):
        return (unresolved,)
    return (
        unresolved,
        RobinhoodCapitalGainsBlocker(
            kind=RobinhoodCapitalGainsBlockerKind.PRE_CUTOVER_HOLDING_RULE,
            account_index=group.account_index,
            disposal_references=group.references,
        ),
    )


def _compute(
    paired: RobinhoodAccountsReconciliation,
    lot_result: RobinhoodSecurityLotResult,
    *,
    config: RobinhoodConfig,
    securities: tuple[RobinhoodSecurityConfig, ...],
    financial_year_start: date,
    financial_year_end: date,
    fx_policy: AuditedFxPolicy,
) -> RobinhoodCapitalGainsResult:
    _validate_inputs(paired, config, financial_year_start, financial_year_end)
    event_map, groups, price_checks = _source_groups(paired)
    _validate_references(event_map, lot_result)
    excluded = tuple(
        group.references
        for group in groups
        if not (financial_year_start <= group.activity_date <= financial_year_end)
    )
    coverage_blockers = _coverage_blockers(
        paired,
        config,
        groups,
        financial_year_start,
        financial_year_end,
    )
    if coverage_blockers:
        stcg, ltcg = _compute_schedules(
            (),
            financial_year_start=financial_year_start,
            financial_year_end=financial_year_end,
            fx_policy=fx_policy,
        )
        return RobinhoodCapitalGainsResult(
            stcg=stcg,
            ltcg=ltcg,
            attributed_lots=(),
            blockers=coverage_blockers,
            excluded_out_of_fy_disposal_references=excluded,
            sale_price_checks=price_checks,
        )

    identity_by_reference, identity_blockers = _identity_index_by_reference(
        paired, securities, lot_result
    )
    disposal_slices: dict[tuple[RobinhoodSecurityEventReference, ...], list[RobinhoodLotSlice]] = (
        defaultdict(list)
    )
    for item in lot_result.slices:
        if item.kind is RobinhoodLotSliceKind.DISPOSAL:
            disposal_slices[item.movement_references].append(item)

    attributed: list[RobinhoodCapitalGainLot] = []
    blockers: list[RobinhoodCapitalGainsBlocker] = []
    for group in groups:
        if not (financial_year_start <= group.activity_date <= financial_year_end):
            continue
        source_identity_blockers = _identity_blockers_for(group, identity_blockers)
        source_lot_blockers = _lot_blockers_for(group, lot_result.blockers, event_map)
        if source_identity_blockers or source_lot_blockers:
            blockers.extend(
                _unresolved_group_blockers(group, source_identity_blockers, source_lot_blockers)
            )
            continue

        slices = tuple(disposal_slices.get(group.references, ()))
        if not slices:
            raise _fail("current-FY sale group is neither lot-resolved nor explicitly blocked")
        config_indexes = {identity_by_reference.get(reference) for reference in group.references}
        if len(config_indexes) != 1 or None in config_indexes:
            raise _fail("sale group does not resolve to one configured security")
        config_index = config_indexes.pop()
        assert config_index is not None
        security = securities[config_index]
        for item in slices:
            _validate_slice(item, group, event_map, identity_by_reference, config_index)

        lot_ids = tuple(sorted({item.lot_id for item in slices}))
        if group.activity_date < NEW_REGIME_CUTOVER_DATE:
            blockers.append(
                RobinhoodCapitalGainsBlocker(
                    kind=RobinhoodCapitalGainsBlockerKind.PRE_CUTOVER_HOLDING_RULE,
                    account_index=group.account_index,
                    disposal_references=group.references,
                    lot_ids=lot_ids,
                )
            )
            continue
        if conditions := _unsupported_conditions(security):
            blockers.append(
                RobinhoodCapitalGainsBlocker(
                    kind=RobinhoodCapitalGainsBlockerKind.UNSUPPORTED_SECURITY_CLASS,
                    account_index=group.account_index,
                    disposal_references=group.references,
                    lot_ids=lot_ids,
                    unsupported_conditions=conditions,
                )
            )
            continue
        if _sale_order_splits_terms(group, slices):
            # Schedule FA keeps the quantity split, which has no term (#1540).
            blockers.append(
                RobinhoodCapitalGainsBlocker(
                    kind=RobinhoodCapitalGainsBlockerKind.ORDERING_AMBIGUITY,
                    account_index=group.account_index,
                    disposal_references=group.references,
                    lot_ids=lot_ids,
                )
            )
            continue
        attributed.extend(_allocate_group(group, slices, security))

    attributed_tuple = tuple(attributed)
    stcg, ltcg = _compute_schedules(
        attributed_tuple,
        financial_year_start=financial_year_start,
        financial_year_end=financial_year_end,
        fx_policy=fx_policy,
    )
    return RobinhoodCapitalGainsResult(
        stcg=stcg,
        ltcg=ltcg,
        attributed_lots=attributed_tuple,
        blockers=tuple(blockers),
        excluded_out_of_fy_disposal_references=excluded,
        sale_price_checks=price_checks,
    )


def compute_robinhood_capital_gains(
    paired: RobinhoodAccountsReconciliation,
    *,
    config: RobinhoodConfig,
    securities: RobinhoodSecurityIdentities,
    financial_year_start: date,
    financial_year_end: date,
    fx_policy: AuditedFxPolicy,
    quantities: RobinhoodSecurityQuantityResult | None = None,
) -> RobinhoodCapitalGainsResult:
    """Compute original-date FIFO Robinhood equity CG for one complete FY."""

    with localcontext(_ARITHMETIC_CONTEXT):
        if quantities is None:
            identities = resolve_robinhood_security_identities(paired, securities.securities)
            quantities = resolve_robinhood_security_quantities(
                paired,
                identities,
                config.security_receipt_resolutions,
            )
        lot_result = reconcile_robinhood_security_lots(quantities)
        return _compute(
            paired,
            lot_result,
            config=config,
            securities=securities.securities,
            financial_year_start=financial_year_start,
            financial_year_end=financial_year_end,
            fx_policy=fx_policy,
        )
