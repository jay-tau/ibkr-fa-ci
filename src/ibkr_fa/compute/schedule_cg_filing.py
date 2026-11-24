"""Broker-neutral Schedule CG filing result: workbook amounts and gates (#642).

The STCG and LTCG engines compute every line. A portal-shaped filing
workbook also needs the #615 rounded portal components, every amount each
section prints (#1078), the listing venues and security count behind the
section 50CA classification advisory, the source's evidence wording, the
sections holding disposals the workbook doesn't cover (#1322), each
section's Table F date bands (#1360), and the filing-position blockers that
keep a workbook from being written. This module builds all of that; writers
only render it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal

from ibkr_fa.compliance.schedule_cg_filing_position import (
    SCHEDULE_CG_CLASSIFICATION_ADVISORY,
    ScheduleCgFilingPosition,
)
from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from ibkr_fa.compute.schedule_cg_ltcg import ScheduleCgLtcgComputeResult
from ibkr_fa.compute.schedule_cg_portal_amounts import ScheduleCgPortalAmounts
from ibkr_fa.compute.schedule_cg_table_f import ScheduleCgTableFCase, ScheduleCgTableFRow
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.invariants import invariant_error, require
from ibkr_fa.models._row_checks import require_bools, require_whole_rupee_amounts
from ibkr_fa.models.closed_lot import ClosedLot

__all__ = [
    "ScheduleCgFilingBasis",
    "ScheduleCgFilingBlocker",
    "ScheduleCgFilingBlockerKind",
    "ScheduleCgFilingResult",
    "ScheduleCgFilingRoutes",
    "ScheduleCgFilingSection",
    "build_schedule_cg_filing_result",
    "require_unblocked_schedule_cg_filing",
    "schedule_cg_filing_blocked_text",
    "schedule_cg_filing_blockers",
]


@dataclass(frozen=True, slots=True)
class ScheduleCgFilingBasis:
    """One source's Basis text for the workbook cells that state source evidence.

    Attributes:
        consideration: (a)(ii), full value of consideration.
        cost: (b)(i), cost of acquisition.
        transfer_expenditure: (b)(iii), expenditure in connection with transfer.
        section_94_7_8: STCG (d), the section 94(7)/(8) loss disallowance.
        schedule_dcg: ITR-3 A6(g)'s Schedule DCG and sections 54G/54GA claim.
        consideration_position: The filing position on consideration and
            transfer expenditure, which ends each section sheet's tax position
            note (#1383).
        covered_disposals: The disposals the workbook covers, as in "No
            <source> <covered_disposals> were attributed ..." (#1322).
        uncovered_disposals: Which of the source's capital disposals the
            workbook leaves out and where to enter them from, as in
            "Manual entry in A5: <uncovered_disposals>." (#1322).
        table_f_other_files: The other capital-gains files whose amounts a
            Table F band adds, as in "Add the same band's amounts from every
            other capital-gains file: <table_f_other_files>." (#1360).
    """

    consideration: str
    cost: str
    transfer_expenditure: str
    section_94_7_8: str
    schedule_dcg: str
    consideration_position: str
    covered_disposals: str
    uncovered_disposals: str
    table_f_other_files: str


# The amount fields of ScheduleCgFilingSection, in the order its checks run.
_SECTION_AMOUNT_FIELDS: Final[tuple[str, ...]] = (
    "consideration_inr",
    "total_consideration_inr",
    "cost_of_acquisition_inr",
    "transfer_expenditure_inr",
    "total_deductions_inr",
    "balance_inr",
    "section_94_7_8_disallowed_loss_inr",
    "result_inr",
)
# The fields the ITR refuses as negative; balance and result may be.
_SECTION_UNSIGNED_FIELDS: Final[tuple[str, ...]] = (
    "consideration_inr",
    "total_consideration_inr",
    "cost_of_acquisition_inr",
    "transfer_expenditure_inr",
    "total_deductions_inr",
    "section_94_7_8_disallowed_loss_inr",
)


@dataclass(frozen=True, slots=True)
class ScheduleCgFilingSection:
    """Every amount one Schedule CG section of the filing workbook prints (#1078).

    The workbook's field refs are the ITR-2 A5/B8 and ITR-3 A6/B9 rows.

    Attributes:
        schedule: ``"STCG"`` or ``"LTCG"``.
        consideration_inr: (a)(ii), full value of consideration.
        total_consideration_inr: (a)(iii), total full value of consideration.
        cost_of_acquisition_inr: (b)(i), cost of acquisition.
        transfer_expenditure_inr: (b)(iii), expenditure in connection with
            transfer.
        total_deductions_inr: (b)(iv), total section 48 deductions.
        balance_inr: (c), total consideration less total deductions.
        section_94_7_8_disallowed_loss_inr: STCG (d), the loss disallowed
            under section 94(7)/(8); ``None`` in an LTCG section.
        result_inr: A5(e), A6(g) or the LTCG (e) result.

    Raises:
        InvariantError: At construction, if an amount isn't a whole-rupee
            ``Decimal`` within the CBDT bound, an unsigned amount is
            negative, the disallowance is missing in an STCG section or
            present in an LTCG one, a total doesn't add up, or a zero
            consideration has non-zero deductions.
    """

    schedule: Literal["STCG", "LTCG"]
    consideration_inr: Decimal
    total_consideration_inr: Decimal
    cost_of_acquisition_inr: Decimal
    transfer_expenditure_inr: Decimal
    total_deductions_inr: Decimal
    balance_inr: Decimal
    section_94_7_8_disallowed_loss_inr: Decimal | None
    result_inr: Decimal

    def __post_init__(self) -> None:
        for name in _SECTION_AMOUNT_FIELDS:
            require_whole_rupee_amounts(
                self, name, optional=name == "section_94_7_8_disallowed_loss_inr"
            )
        require(
            self.schedule in ("STCG", "LTCG"),
            what="ScheduleCgFilingSection.schedule must be STCG or LTCG",
        )
        for name in _SECTION_UNSIGNED_FIELDS:
            value = getattr(self, name)
            require(
                value is None or value >= 0,
                what=f"ScheduleCgFilingSection.{name} must not be negative",
                value=value,
            )
        stcg = self.schedule == "STCG"
        require(
            (self.section_94_7_8_disallowed_loss_inr is not None) is stcg,
            what=(
                "ScheduleCgFilingSection.section_94_7_8_disallowed_loss_inr must be "
                + ("present in an STCG section" if stcg else "blank in an LTCG section")
            ),
        )
        consideration, total_consideration = self.consideration_inr, self.total_consideration_inr
        transfer, deductions = self.transfer_expenditure_inr, self.total_deductions_inr
        balance, disallowed, result = (
            self.balance_inr,
            self.section_94_7_8_disallowed_loss_inr,
            self.result_inr,
        )
        require(
            total_consideration == consideration,
            what="ScheduleCgFilingSection.total_consideration_inr must equal consideration_inr",
        )
        require(
            deductions == self.cost_of_acquisition_inr + transfer,
            what=(
                "ScheduleCgFilingSection.total_deductions_inr must equal "
                "cost_of_acquisition_inr plus transfer_expenditure_inr"
            ),
        )
        require(
            balance == total_consideration - deductions,
            what=(
                "ScheduleCgFilingSection.balance_inr must equal total_consideration_inr "
                "less total_deductions_inr"
            ),
        )
        if stcg:
            require(
                disallowed is not None and result == balance + disallowed,
                what=(
                    "ScheduleCgFilingSection.result_inr must equal balance_inr plus "
                    "section_94_7_8_disallowed_loss_inr in an STCG section"
                ),
            )
        else:
            require(
                result == balance,
                what="ScheduleCgFilingSection.result_inr must equal balance_inr in an LTCG section",
            )
        # As schedule_cg_portal_amounts refuses a zero consideration against a cost.
        require(
            consideration != 0 or deductions == 0,
            what=(
                "ScheduleCgFilingSection.total_deductions_inr must be zero when "
                "consideration_inr is zero; the ITR refuses deductions against zero "
                "consideration"
            ),
        )


class ScheduleCgFilingBlockerKind(StrEnum):
    """The filing position or gate that keeps a workbook from being written."""

    UNREVIEWED_LISTING_VENUE = "unreviewed-listing-venue"
    UNREVIEWED_SECURITY_CLASS = "unreviewed-security-class"
    SECTION_54_DECISION_PENDING = "section-54-decision-pending"


@dataclass(frozen=True, slots=True)
class ScheduleCgFilingBlocker:
    """One blocker, its stored refusal reason and its caller's route.

    ``reason`` can name input, such as an unreviewed exchange, so the
    refusal keeps only its context key and prints the kind's fixed text.
    ``route`` is the line the caller's :class:`ScheduleCgFilingRoutes` gave
    the blocker, which the refusal prints too; empty when it gave none.
    """

    kind: ScheduleCgFilingBlockerKind
    reason: str
    route: str = ""


@dataclass(frozen=True, slots=True)
class ScheduleCgFilingRoutes:
    """A caller's route for each filing blocker its input can reach (#1555).

    Each gives the line a blocked filing's refusal prints for that blocker,
    after the blocked kinds. Like ``Dec31SessionRoutes``, a route comes from
    the caller: only it knows its lots' source references and which
    settings, if any, can act on a blocker.

    Attributes:
        unreviewed_listing_venue: Gets the 0-based positions, in
            ``closed_lots``, of the lots on an unreviewed listing venue.
        section_54_decision_pending: For positive LTCG.
    """

    unreviewed_listing_venue: Callable[[tuple[int, ...]], str]
    section_54_decision_pending: str


# What a blocked filing's refusal prints for each blocker kind (#1047).
_BLOCKER_KIND_TEXT: Final[Mapping[ScheduleCgFilingBlockerKind, str]] = {
    ScheduleCgFilingBlockerKind.UNREVIEWED_LISTING_VENUE: (
        "a lot is listed on a foreign exchange the filing position hasn't reviewed"
    ),
    ScheduleCgFilingBlockerKind.UNREVIEWED_SECURITY_CLASS: (
        "a lot is in a security class the filing position hasn't reviewed"
    ),
    ScheduleCgFilingBlockerKind.SECTION_54_DECISION_PENDING: (
        "positive LTCG requires a reviewed section 54F deduction decision"
    ),
}


def schedule_cg_filing_blocked_text(blockers: Sequence[ScheduleCgFilingBlocker]) -> str:
    """What a filing blocked on ``blockers`` prints: the kinds in fixed text,
    then each blocker's route on its own line, in blocker order (#1555).

    Never a blocker's ``reason``, which can name input (#1047)."""
    return (
        "Schedule CG filing is blocked: "
        + "; ".join(_BLOCKER_KIND_TEXT[blocker.kind] for blocker in blockers)
        + "".join(f"\n{blocker.route}" for blocker in blockers if blocker.route)
    )


def require_unblocked_schedule_cg_filing(blockers: Sequence[ScheduleCgFilingBlocker]) -> None:
    """Refuse a filing that has blockers, with
    :func:`schedule_cg_filing_blocked_text`.

    The error keeps the blockers' ``reasons`` key, never their text, which
    can name input (#1047, #1252).

    Raises:
        InvariantError: If ``blockers`` is not empty.
    """
    if blockers:
        raise invariant_error(
            what=schedule_cg_filing_blocked_text(blockers),
            reasons="; ".join(blocker.reason for blocker in blockers),
        )


@dataclass(frozen=True, slots=True)
class ScheduleCgFilingResult:
    """Everything a Schedule CG filing workbook renders.

    The builder supplies only zero transfer expenditure and zero STCG
    disallowance. Non-zero values need source evidence and review (#1078).

    Attributes:
        stcg: The STCG engine result.
        ltcg: The LTCG engine result.
        stcg_amounts: ``stcg``'s whole-rupee portal components (#615).
        ltcg_amounts: ``ltcg``'s whole-rupee portal components (#615).
        stcg_section: Every amount the STCG section prints (#1078).
        ltcg_section: Every amount the LTCG section prints (#1078).
        stcg_table_f: ``stcg``'s Table F row 3 (#1360).
        ltcg_table_f: ``ltcg``'s Table F row 5 (#1360).
        listing_venues: The lots' listing venues, sorted.
        security_count: The number of distinct ISINs among the lots.
        basis: The source's evidence wording.
        stcg_uncovered_disposals: Whether the source has STCG disposals
            this workbook doesn't cover, which need manual entry (#1322).
        ltcg_uncovered_disposals: The same for the LTCG section.
        blockers: Why no workbook may be written; empty when one may.

    Raises:
        InvariantError: At construction, if a section is not a
            ``ScheduleCgFilingSection`` for its schedule, a Table F row isn't
            a ``ScheduleCgTableFRow`` or is ``MANUAL_SECTION``, which no
            filing section is (#1383), or an uncovered-disposals flag isn't a
            ``bool``.
    """

    stcg: ScheduleCgStcgComputeResult
    ltcg: ScheduleCgLtcgComputeResult
    stcg_amounts: ScheduleCgPortalAmounts
    ltcg_amounts: ScheduleCgPortalAmounts
    stcg_section: ScheduleCgFilingSection
    ltcg_section: ScheduleCgFilingSection
    stcg_table_f: ScheduleCgTableFRow
    ltcg_table_f: ScheduleCgTableFRow
    listing_venues: tuple[str, ...]
    security_count: int
    basis: ScheduleCgFilingBasis
    stcg_uncovered_disposals: bool
    ltcg_uncovered_disposals: bool
    blockers: tuple[ScheduleCgFilingBlocker, ...]

    def __post_init__(self) -> None:
        require_bools(self, "stcg_uncovered_disposals", "ltcg_uncovered_disposals")
        for name, section, schedule in (
            ("stcg_section", self.stcg_section, "STCG"),
            ("ltcg_section", self.ltcg_section, "LTCG"),
        ):
            require(
                isinstance(section, ScheduleCgFilingSection) and section.schedule == schedule,
                what=f"ScheduleCgFilingResult.{name} must be the {schedule} section",
            )
        for name, table_f in (
            ("stcg_table_f", self.stcg_table_f),
            ("ltcg_table_f", self.ltcg_table_f),
        ):
            require(
                isinstance(table_f, ScheduleCgTableFRow)
                and table_f.case is not ScheduleCgTableFCase.MANUAL_SECTION,
                what=f"ScheduleCgFilingResult.{name} must be a Table F row that isn't manual",
            )

    @property
    def classification_advisory(self) -> str:
        """The section 50CA classification note; empty when there are no lots."""
        if not self.security_count:
            return ""
        return SCHEDULE_CG_CLASSIFICATION_ADVISORY.format(
            security_count=self.security_count,
            listing_venues="/".join(self.listing_venues),
        )


# Zero is the only transfer expenditure and section 94(7)/(8) disallowance
# any source supports; build_schedule_cg_filing_result says why (#1078).
_TRANSFER_EXPENDITURE_INR: Final[Decimal] = Decimal("0")
_SECTION_94_7_8_DISALLOWED_LOSS_INR: Final[Decimal] = Decimal("0")


def _filing_section(
    schedule: Literal["STCG", "LTCG"], amounts: ScheduleCgPortalAmounts
) -> ScheduleCgFilingSection:
    """The section's amounts, from its portal components (#1078)."""
    disallowed = _SECTION_94_7_8_DISALLOWED_LOSS_INR if schedule == "STCG" else None
    cost = amounts.cost_of_acquisition_inr
    balance = amounts.balance_inr
    return ScheduleCgFilingSection(
        schedule=schedule,
        consideration_inr=amounts.consideration_inr,
        total_consideration_inr=amounts.consideration_inr,
        cost_of_acquisition_inr=cost,
        transfer_expenditure_inr=_TRANSFER_EXPENDITURE_INR,
        total_deductions_inr=cost + _TRANSFER_EXPENDITURE_INR,
        balance_inr=balance,
        section_94_7_8_disallowed_loss_inr=disallowed,
        result_inr=balance if disallowed is None else balance + disallowed,
    )


def build_schedule_cg_filing_result(
    stcg: ScheduleCgStcgComputeResult,
    ltcg: ScheduleCgLtcgComputeResult,
    closed_lots: Iterable[ClosedLot],
    *,
    position: ScheduleCgFilingPosition,
    basis: ScheduleCgFilingBasis,
    stcg_uncovered_disposals: bool,
    ltcg_uncovered_disposals: bool,
    routes: ScheduleCgFilingRoutes | None = None,
) -> ScheduleCgFilingResult:
    """Build the filing result of the lots that produced ``stcg`` and ``ltcg``.

    ``closed_lots`` supplies what the lines don't carry: listing venue,
    security class and ISIN. ``position`` is the source's reviewed scope.
    ``routes``, when given, supplies the unreviewed-venue and positive-LTCG
    blockers' route lines (#1555); the unreviewed-class blocker has none.
    ``stcg_uncovered_disposals`` / ``ltcg_uncovered_disposals`` say whether
    the source has capital disposals in that section that these lots don't
    cover, such as Robinhood's option capital gains; the workbook then never
    says the section needs no entry (#1322).

    Each :class:`ScheduleCgFilingSection` holds every amount its section
    prints (#1078). It takes consideration, cost and balance from the portal
    components and has zero transfer expenditure, and every STCG section
    keeps zero section 94(7)/(8) disallowance. Zero is the only transfer
    expenditure and disallowance any source supports: Robinhood is the only
    source with a reviewed filing position, and it files each sale's
    recorded Amount as consideration with zero transfer expenditure (#1383).
    It refuses a sale whose proven gross differs from that Amount, because
    no path files the difference as transfer expenditure. A non-zero value
    needs its own evidence and review.

    Each section's Table F row is its engine result's ``table_f`` (#1360):
    its gains split by transfer date to the section's balance, the result it
    files beside the zero disallowance.

    Raises:
        InvariantError: If a lot is in neither schedule, a lot states no
            listing venue or security class, a
            schedule's portal components are invalid
            (:func:`~ibkr_fa.compute.schedule_cg_portal_amounts.schedule_cg_portal_amounts`),
            a section's amounts are (:class:`ScheduleCgFilingSection`), or
            its Table F row can't be built
            (:func:`~ibkr_fa.compute.schedule_cg_table_f.schedule_cg_table_f_row`).
            Invalid components, amounts or rows beside a filing blocker
            refuse naming the blockers instead.
    """
    lots = tuple(closed_lots)
    blockers = schedule_cg_filing_blockers(stcg, ltcg, lots, position=position, routes=routes)
    try:
        stcg_amounts = stcg.portal_amounts
        stcg_section = _filing_section("STCG", stcg_amounts)
        ltcg_amounts = ltcg.portal_amounts
        ltcg_section = _filing_section("LTCG", ltcg_amounts)
        stcg_table_f = stcg.table_f
        ltcg_table_f = ltcg.table_f
    except InvariantError:
        # Earlier filing refusals must not be hidden by invalid portal amounts,
        # section amounts or Table F rows.
        require_unblocked_schedule_cg_filing(blockers)
        raise
    return ScheduleCgFilingResult(
        stcg=stcg,
        ltcg=ltcg,
        stcg_amounts=stcg_amounts,
        ltcg_amounts=ltcg_amounts,
        stcg_section=stcg_section,
        ltcg_section=ltcg_section,
        stcg_table_f=stcg_table_f,
        ltcg_table_f=ltcg_table_f,
        listing_venues=tuple(sorted({lot.listing_exchange for lot in lots})),
        security_count=len({lot.isin for lot in lots}),
        basis=basis,
        stcg_uncovered_disposals=stcg_uncovered_disposals,
        ltcg_uncovered_disposals=ltcg_uncovered_disposals,
        blockers=blockers,
    )


def schedule_cg_filing_blockers(
    stcg: ScheduleCgStcgComputeResult,
    ltcg: ScheduleCgLtcgComputeResult,
    closed_lots: Sequence[ClosedLot],
    *,
    position: ScheduleCgFilingPosition,
    routes: ScheduleCgFilingRoutes | None = None,
) -> tuple[ScheduleCgFilingBlocker, ...]:
    """The filing-position blockers of the lots that produced ``stcg`` and
    ``ltcg``, which :func:`build_schedule_cg_filing_result` stores.

    Each checks one lot or one line: a lot's listing venue, a lot's
    security class, or a positive LTCG line. A caller can run them on the
    lots it has resolved while others stay unresolved (#1570). ``routes``
    is as for :func:`build_schedule_cg_filing_result`.

    Raises:
        InvariantError: If a lot is in neither schedule, or a lot states no
            listing venue or security class.
    """
    require(
        len(closed_lots) == len(stcg.stcg_lines) + len(ltcg.ltcg_lines),
        what="Schedule CG filing requires every closed lot in STCG or LTCG",
    )
    venues = {lot.listing_exchange for lot in closed_lots}
    classes = {lot.sub_category for lot in closed_lots}
    # Checked before sorting: sorted() can't order None among strings.
    require(
        None not in venues and None not in classes,
        what="Schedule CG filing requires every lot to state its exchange and security class",
    )
    blockers: list[ScheduleCgFilingBlocker] = []
    if unreviewed_venues := sorted(venues - position.reviewed_listing_venues):
        blockers.append(
            ScheduleCgFilingBlocker(
                ScheduleCgFilingBlockerKind.UNREVIEWED_LISTING_VENUE,
                "Schedule CG filing position covers only reviewed foreign exchanges: "
                + ", ".join(unreviewed_venues),
                ""
                if routes is None
                else routes.unreviewed_listing_venue(
                    tuple(
                        index
                        for index, lot in enumerate(closed_lots)
                        if lot.listing_exchange not in position.reviewed_listing_venues
                    )
                ),
            )
        )
    if unreviewed_classes := sorted(classes - position.reviewed_security_classes):
        blockers.append(
            ScheduleCgFilingBlocker(
                ScheduleCgFilingBlockerKind.UNREVIEWED_SECURITY_CLASS,
                "Schedule CG filing position covers only reviewed security classes: "
                + ", ".join(unreviewed_classes),
            )
        )
    # ponytail: add section 54F inputs before allowing a positive LTCG line.
    if any(line.realized_gain_inr > 0 for line in ltcg.ltcg_lines):
        blockers.append(
            ScheduleCgFilingBlocker(
                ScheduleCgFilingBlockerKind.SECTION_54_DECISION_PENDING,
                "Schedule CG filing with positive LTCG requires a reviewed "
                "section 54F deduction decision",
                "" if routes is None else routes.section_54_decision_pending,
            )
        )
    return tuple(blockers)
