"""Tests for the broker-neutral Schedule CG filing result (#642)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

import pytest

from ibkr_fa.compliance.schedule_cg_filing_position import (
    ROBINHOOD_SCHEDULE_CG_FILING_POSITION,
    ScheduleCgFilingPosition,
)
from ibkr_fa.compute.robinhood_capital_gains import RobinhoodCapitalGainLot
from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from ibkr_fa.compute.schedule_cg_filing import (
    ScheduleCgFilingBasis,
    ScheduleCgFilingBlockerKind,
    ScheduleCgFilingResult,
    ScheduleCgFilingSection,
    build_schedule_cg_filing_result,
)
from ibkr_fa.compute.schedule_cg_ltcg import ScheduleCgLtcgComputeResult
from ibkr_fa.compute.schedule_cg_portal_amounts import ScheduleCgPortalAmounts
from ibkr_fa.compute.schedule_cg_table_f import ScheduleCgTableFCase, ScheduleCgTableFRow
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.invariants import invariant_error
from tests._robinhood_cg import (
    FILING_CLASS_KIND,
    FILING_LTCG_KIND,
    FILING_VENUE_KIND,
    cg_result,
    sale,
)

_BASIS = ScheduleCgFilingBasis(
    consideration="consideration",
    cost="cost",
    transfer_expenditure="transfer expenditure",
    section_94_7_8="section 94(7)/(8)",
    schedule_dcg="Schedule DCG",
    consideration_position="consideration position",
    covered_disposals="covered disposals",
    uncovered_disposals="uncovered disposals need entry",
    table_f_other_files="other files",
)


def _build(
    *lots: RobinhoodCapitalGainLot,
    position: ScheduleCgFilingPosition = ROBINHOOD_SCHEDULE_CG_FILING_POSITION,
) -> ScheduleCgFilingResult:
    """The filing result of ``lots``."""
    engines = cg_result(*lots)
    return build_schedule_cg_filing_result(
        engines.stcg,
        engines.ltcg,
        (item.closed_lot for item in lots),
        position=position,
        basis=_BASIS,
        stcg_uncovered_disposals=False,
        ltcg_uncovered_disposals=False,
    )


def _on(lot: RobinhoodCapitalGainLot, **fields: object) -> RobinhoodCapitalGainLot:
    return replace(lot, closed_lot=replace(lot.closed_lot, **fields))


def test_blocker_free_result_carries_the_engines_amounts_and_advisory() -> None:
    lots = (
        sale("12", "10"),
        _on(sale("12", "10", country="GB", lot_ordinal=3), listing_exchange="NYSE"),
        sale("12", "10", long_term=True, lot_ordinal=5, sale_ordinal=6),
    )

    result = _build(*lots)

    engines = cg_result(*lots)
    assert result.blockers == ()
    assert result.stcg_amounts == engines.stcg.portal_amounts
    assert result.ltcg_amounts == engines.ltcg.portal_amounts
    assert result.listing_venues == ("NASDAQ", "NYSE")
    assert result.security_count == 2
    assert result.basis is _BASIS
    assert result.classification_advisory == (
        "Tax position: 2 foreign exchange-listed securities on NASDAQ/NYSE are "
        "reported as assets other than unquoted shares. Section 50CA prescribed FMV "
        "is not computed. No CBDT guidance or binding ruling directly resolves this "
        "foreign-exchange classification."
    )


def test_a_result_without_lots_has_no_advisory() -> None:
    result = _build()

    assert result.blockers == ()
    assert (result.listing_venues, result.security_count) == ((), 0)
    assert result.classification_advisory == ""


@pytest.mark.parametrize(
    "lots",
    [
        (sale("12", "0"),),
        (sale("12", "0", long_term=True),),
        # Positive STCG no longer blocks the filing (#1360), so the LTCG
        # amounts' own refusal shows.
        (sale("10", "12"), sale("12", "0", long_term=True, lot_ordinal=3, sale_ordinal=4)),
    ],
    ids=["stcg", "ltcg", "ltcg-beside-positive-stcg"],
)
def test_invalid_amounts_without_a_filing_blocker_keep_the_original_refusal(
    lots: tuple[RobinhoodCapitalGainLot, ...],
) -> None:
    with pytest.raises(InvariantError, match="rounded consideration") as raised:
        _build(*lots)

    assert ("schedule", None) in raised.value.context


_BLOCKED = {
    "unreviewed-venue": (
        lambda: _on(sale("12", "10"), listing_exchange="OTC"),
        ScheduleCgFilingBlockerKind.UNREVIEWED_LISTING_VENUE,
        "Schedule CG filing position covers only reviewed foreign exchanges: OTC",
    ),
    "unreviewed-security-class": (
        lambda: _on(sale("12", "10"), sub_category="PREFERRED"),
        ScheduleCgFilingBlockerKind.UNREVIEWED_SECURITY_CLASS,
        "Schedule CG filing position covers only reviewed security classes: PREFERRED",
    ),
    "positive-ltcg": (
        lambda: sale("10", "12", long_term=True),
        ScheduleCgFilingBlockerKind.SECTION_54_DECISION_PENDING,
        f"Schedule CG filing with {FILING_LTCG_KIND}",
    ),
}


@pytest.mark.parametrize(("make_lot", "kind", "reason"), _BLOCKED.values(), ids=_BLOCKED.keys())
def test_each_gate_is_a_typed_blocker(
    make_lot: Callable[[], RobinhoodCapitalGainLot], kind: ScheduleCgFilingBlockerKind, reason: str
) -> None:
    result = _build(make_lot())

    assert [(blocker.kind, blocker.reason) for blocker in result.blockers] == [(kind, reason)]


def test_every_blocker_is_recorded_with_its_offending_values_sorted() -> None:
    result = _build(
        _on(sale("10", "12", long_term=True), listing_exchange="PINK", sub_category="WARRANT"),
        _on(sale("10", "12", lot_ordinal=3, sale_ordinal=4), listing_exchange="OTC"),
    )

    assert [blocker.reason for blocker in result.blockers] == [
        "Schedule CG filing position covers only reviewed foreign exchanges: OTC, PINK",
        "Schedule CG filing position covers only reviewed security classes: WARRANT",
        f"Schedule CG filing with {FILING_LTCG_KIND}",
    ]


# The unreviewed exchanges and classes the cases below feed in, as whole tokens.
_UNREVIEWED_VALUE = re.compile(r"(?i)\b(OTC|PINK|PREFERRED|WARRANT)\b")


def _blocked_refusal(*kind_texts: str) -> str:
    """The exact refusal of a filing blocked on ``kind_texts``, in blocker order."""
    return (
        f"InvariantError: Schedule CG filing is blocked: {'; '.join(kind_texts)} "
        "(context keys: reasons)"
    )


# Each case: lots whose portal amounts are invalid (a zero consideration) beside
# a filing blocker, and the kind texts the refusal prints.
_BLOCKED_WITH_INVALID_AMOUNTS = {
    "unreviewed-venue": (
        lambda: (_on(sale("12", "0"), listing_exchange="OTC"),),
        [FILING_VENUE_KIND],
    ),
    "unreviewed-security-class": (
        lambda: (_on(sale("12", "0"), sub_category="PREFERRED"),),
        [FILING_CLASS_KIND],
    ),
    "positive-ltcg-with-invalid-stcg": (
        lambda: (sale("12", "0"), sale("10", "12", long_term=True, lot_ordinal=3, sale_ordinal=4)),
        [FILING_LTCG_KIND],
    ),
    "every-reviewed-value-and-positive-ltcg": (
        lambda: (
            _on(sale("12", "0"), listing_exchange="PINK", sub_category="WARRANT"),
            _on(
                sale("10", "12", long_term=True, lot_ordinal=3, sale_ordinal=4),
                listing_exchange="OTC",
            ),
        ),
        [FILING_VENUE_KIND, FILING_CLASS_KIND, FILING_LTCG_KIND],
    ),
}


@pytest.mark.parametrize(
    ("make_lots", "kind_texts"),
    _BLOCKED_WITH_INVALID_AMOUNTS.values(),
    ids=_BLOCKED_WITH_INVALID_AMOUNTS.keys(),
)
def test_invalid_amounts_beside_a_filing_blocker_refuse_naming_each_blocker_kind(
    make_lots: Callable[[], tuple[RobinhoodCapitalGainLot, ...]],
    kind_texts: list[str],
) -> None:
    """#1047: invalid portal amounts don't hide an earlier filing refusal, which
    names each blocker's kind in fixed text. The blockers' reasons, which name
    the unreviewed exchanges and classes from the input, keep only their key in
    the context."""
    with pytest.raises(InvariantError) as raised:
        _build(*make_lots())

    assert str(raised.value) == _blocked_refusal(*kind_texts)
    assert raised.value.context == (("reasons", None),)
    assert _UNREVIEWED_VALUE.search(str(raised.value)) is None


def test_a_source_with_no_reviewed_scope_is_blocked() -> None:
    """Owner decision 1: each source carries its own reviewed lists."""
    result = _build(sale("12", "10"), position=ScheduleCgFilingPosition(frozenset(), frozenset()))

    assert [blocker.kind for blocker in result.blockers] == [
        ScheduleCgFilingBlockerKind.UNREVIEWED_LISTING_VENUE,
        ScheduleCgFilingBlockerKind.UNREVIEWED_SECURITY_CLASS,
    ]


_ASYMMETRY = {
    # LTCG blocks on any positive line at full precision (owner decision 2).
    "ltcg-gain-rounding-to-zero": (
        (sale("10.003", "10.004", long_term=True),),
        [ScheduleCgFilingBlockerKind.SECTION_54_DECISION_PENDING],
    ),
    "ltcg-gain-beside-a-larger-loss": (
        (
            sale("10", "12", long_term=True),
            sale("13", "10", long_term=True, lot_ordinal=3, sale_ordinal=4),
        ),
        [ScheduleCgFilingBlockerKind.SECTION_54_DECISION_PENDING],
    ),
    # STCG never blocks: Table F splits a positive result by date (#1360).
    "stcg-gain-rounding-to-zero": ((sale("10.003", "10.004"),), []),
    "stcg-gain-beside-a-larger-loss": (
        (sale("10", "12"), sale("13", "10", lot_ordinal=3, sale_ordinal=4)),
        [],
    ),
    "stcg-gain": ((sale("10", "12"),), []),
}


@pytest.mark.parametrize(("lots", "kinds"), _ASYMMETRY.values(), ids=_ASYMMETRY.keys())
def test_ltcg_gate_reads_each_line_and_stcg_has_no_gate(
    lots: tuple[RobinhoodCapitalGainLot, ...], kinds: list[ScheduleCgFilingBlockerKind]
) -> None:
    assert [blocker.kind for blocker in _build(*lots).blockers] == kinds


@pytest.mark.parametrize("field", ["stcg_uncovered_disposals", "ltcg_uncovered_disposals"])
def test_an_uncovered_disposals_flag_must_be_a_bool(field: str) -> None:
    """#1322: a truthy stand-in can't decide what the workbook promises."""
    with pytest.raises(InvariantError, match=f"{field} must be a bool"):
        replace(_build(), **{field: 1})


def test_a_lot_in_neither_schedule_is_refused() -> None:
    engines = cg_result(sale("12", "10"))

    with pytest.raises(InvariantError, match="every closed lot in STCG or LTCG"):
        build_schedule_cg_filing_result(
            engines.stcg,
            engines.ltcg,
            (item.closed_lot for item in engines.attributed_lots * 2),
            position=ROBINHOOD_SCHEDULE_CG_FILING_POSITION,
            basis=_BASIS,
            stcg_uncovered_disposals=False,
            ltcg_uncovered_disposals=False,
        )


@pytest.mark.parametrize("field", ["listing_exchange", "sub_category"])
def test_a_lot_without_exchange_or_security_class_is_refused(field: str) -> None:
    """#629: refused by name, before the reviewed-value checks sort the values."""
    with pytest.raises(InvariantError, match="state its exchange and security class"):
        _build(
            sale("12", "10"),
            _on(sale("12", "10", lot_ordinal=3, sale_ordinal=4), **{field: None}),
        )


def _section(schedule: str, *amounts: str | None) -> ScheduleCgFilingSection:
    """A section from its amounts in field order: (a)(ii), (a)(iii), (b)(i),
    (b)(iii), (b)(iv), (c), STCG (d) and the result."""
    values = [None if amount is None else Decimal(amount) for amount in amounts]
    fields = (
        "consideration_inr",
        "total_consideration_inr",
        "cost_of_acquisition_inr",
        "transfer_expenditure_inr",
        "total_deductions_inr",
        "balance_inr",
        "section_94_7_8_disallowed_loss_inr",
        "result_inr",
    )
    return ScheduleCgFilingSection(
        schedule="STCG" if schedule == "STCG" else "LTCG",
        **dict(zip(fields, values, strict=True)),  # ty: ignore[invalid-argument-type]
    )


# Each case: the lots, the section read, and every amount it prints (#1078).
_SECTIONS = {
    # The issue's example: the workbook prints these amounts in this order today.
    "proven-stcg": (
        (sale("12", "10"),),
        "stcg_section",
        _section("STCG", "1000", "1000", "1200", "0", "1200", "-200", "0", "-200"),
    ),
    "proven-ltcg-has-no-disallowance": (
        (sale("12", "10", long_term=True),),
        "ltcg_section",
        _section("LTCG", "1000", "1000", "1200", "0", "1200", "-200", None, "-200"),
    ),
    "empty-ltcg-beside-stcg": (
        (sale("12", "10"),),
        "ltcg_section",
        _section("LTCG", "0", "0", "0", "0", "0", "0", None, "0"),
    ),
}


@pytest.mark.parametrize(
    ("lots", "attribute", "expected"), _SECTIONS.values(), ids=_SECTIONS.keys()
)
def test_builder_computes_every_amount_each_section_prints(
    lots: tuple[RobinhoodCapitalGainLot, ...], attribute: str, expected: ScheduleCgFilingSection
) -> None:
    assert getattr(_build(*lots), attribute) == expected


# Hand-built sections on the synthetic amounts 13579 and 24680.
_PROVEN_STCG = _section("STCG", "13579", "13579", "24680", "0", "24680", "-11101", "0", "-11101")
_PROVEN_LTCG = replace(_PROVEN_STCG, schedule="LTCG", section_94_7_8_disallowed_loss_inr=None)
_AMOUNT_FIELDS = (
    "consideration_inr",
    "total_consideration_inr",
    "cost_of_acquisition_inr",
    "transfer_expenditure_inr",
    "total_deductions_inr",
    "balance_inr",
    "section_94_7_8_disallowed_loss_inr",
    "result_inr",
)
# Every amount but STCG (d), which an LTCG section leaves blank.
_REQUIRED_FIELDS = tuple(
    field for field in _AMOUNT_FIELDS if field != "section_94_7_8_disallowed_loss_inr"
)
_SYNTHETIC_AMOUNT = re.compile(r"13579|24680")


def _refusal(what: str, context_key: str | None = None) -> str:
    return f"InvariantError: ScheduleCgFilingSection.{what}" + (
        f" (context keys: {context_key})" if context_key else ""
    )


def _refused(base: ScheduleCgFilingSection, **fields: object) -> str:
    """The refusal of ``base`` with ``fields`` replaced; it names no amount."""
    with pytest.raises(InvariantError) as raised:
        replace(base, **fields)
    assert _SYNTHETIC_AMOUNT.search(str(raised.value)) is None
    return str(raised.value)


@pytest.mark.parametrize(
    "section",
    [
        _PROVEN_STCG,
        _PROVEN_LTCG,
        # A zero consideration with zero deductions, and a positive result.
        _section("STCG", "0", "0", "0", "0", "0", "0", "0", "0"),
        _section("LTCG", "24680", "24680", "13579", "0", "13579", "11101", None, "11101"),
    ],
)
def test_a_consistent_section_is_accepted(section: ScheduleCgFilingSection) -> None:
    assert replace(section) == section


@pytest.mark.parametrize("field", _AMOUNT_FIELDS)
@pytest.mark.parametrize(
    ("value", "what", "context_key"),
    [
        (Decimal("NaN"), "must be a finite Decimal", "type"),
        (Decimal("sNaN"), "must be a finite Decimal", "type"),
        (Decimal("Infinity"), "must be a finite Decimal", "type"),
        (5, "must be a finite Decimal", "type"),
        ("5", "must be a finite Decimal", "type"),
        (
            Decimal("100000000000000"),
            "must round to a whole-rupee amount within ±99,999,999,999,999 "
            "(the CBDT 14-digit bound)",
            "value",
        ),
        (
            Decimal("13579.5"),
            "must be a whole-rupee amount (no fraction, exponent or -0)",
            "value",
        ),
        (Decimal("-0"), "must be a whole-rupee amount (no fraction, exponent or -0)", "value"),
    ],
    ids=["nan", "snan", "infinity", "int", "str", "past-bound", "fraction", "minus-zero"],
)
def test_every_amount_must_be_a_whole_rupee_decimal_in_bound(
    field: str, value: object, what: str, context_key: str
) -> None:
    """The shared row checks run first, so a bad value gets a typed refusal.
    Only the disallowance field is optional; LTCG leaves it blank (#1383)."""
    if field not in _REQUIRED_FIELDS and context_key == "type":
        what += " or None"
    assert _refused(_PROVEN_STCG, **{field: value}) == _refusal(f"{field} {what}", context_key)


def test_schedule_must_be_stcg_or_ltcg() -> None:
    assert _refused(_PROVEN_STCG, schedule="XTCG") == _refusal("schedule must be STCG or LTCG")


_NEGATIVE = {
    # Totals consistent where the field allows it, so only this rule can fire.
    "consideration_inr": {
        "consideration_inr": Decimal("-13579"),
        "total_consideration_inr": Decimal("-13579"),
        "balance_inr": Decimal("-38259"),
        "result_inr": Decimal("-38259"),
    },
    "total_consideration_inr": {"total_consideration_inr": Decimal("-13579")},
    "cost_of_acquisition_inr": {
        "cost_of_acquisition_inr": Decimal("-24680"),
        "total_deductions_inr": Decimal("-24680"),
        "balance_inr": Decimal("38259"),
        "result_inr": Decimal("38259"),
    },
    "transfer_expenditure_inr": {
        "transfer_expenditure_inr": Decimal("-5"),
        "total_deductions_inr": Decimal("24675"),
        "balance_inr": Decimal("-11096"),
        "result_inr": Decimal("-11096"),
    },
    "total_deductions_inr": {"total_deductions_inr": Decimal("-24680")},
    "section_94_7_8_disallowed_loss_inr": {
        "section_94_7_8_disallowed_loss_inr": Decimal("-7"),
        "result_inr": Decimal("-11108"),
    },
}


@pytest.mark.parametrize(("field", "fields"), _NEGATIVE.items(), ids=_NEGATIVE.keys())
def test_each_unsigned_amount_must_not_be_negative(field: str, fields: dict[str, Decimal]) -> None:
    assert _refused(_PROVEN_STCG, **fields) == _refusal(f"{field} must not be negative", "value")


def test_only_an_stcg_section_has_a_disallowance() -> None:
    assert _refused(_PROVEN_STCG, section_94_7_8_disallowed_loss_inr=None) == _refusal(
        "section_94_7_8_disallowed_loss_inr must be present in an STCG section"
    )
    assert _refused(_PROVEN_LTCG, section_94_7_8_disallowed_loss_inr=Decimal("0")) == _refusal(
        "section_94_7_8_disallowed_loss_inr must be blank in an LTCG section"
    )


@pytest.mark.parametrize("field", _REQUIRED_FIELDS)
@pytest.mark.parametrize("base", [_PROVEN_STCG, _PROVEN_LTCG], ids=["STCG", "LTCG"])
def test_a_section_leaves_no_amount_blank_but_stcg_d(
    base: ScheduleCgFilingSection, field: str
) -> None:
    """No section is left for manual entry any more (#1383)."""
    assert _refused(base, **{field: None}) == _refusal(f"{field} must be a finite Decimal", "type")


_BROKEN_SUMS = {
    "total-consideration": (
        _PROVEN_STCG,
        {"total_consideration_inr": Decimal("24680")},
        "total_consideration_inr must equal consideration_inr",
    ),
    "total-deductions": (
        _PROVEN_STCG,
        {"total_deductions_inr": Decimal("24681")},
        "total_deductions_inr must equal cost_of_acquisition_inr plus transfer_expenditure_inr",
    ),
    "balance": (
        _PROVEN_STCG,
        {"balance_inr": Decimal("-11100")},
        "balance_inr must equal total_consideration_inr less total_deductions_inr",
    ),
    "stcg-result": (
        _PROVEN_STCG,
        {"result_inr": Decimal("-11100")},
        "result_inr must equal balance_inr plus section_94_7_8_disallowed_loss_inr "
        "in an STCG section",
    ),
    "ltcg-result": (
        _PROVEN_LTCG,
        {"result_inr": Decimal("-11100")},
        "result_inr must equal balance_inr in an LTCG section",
    ),
}


@pytest.mark.parametrize(("base", "fields", "what"), _BROKEN_SUMS.values(), ids=_BROKEN_SUMS.keys())
def test_each_total_must_add_up(
    base: ScheduleCgFilingSection, fields: dict[str, Decimal], what: str
) -> None:
    assert _refused(base, **fields) == _refusal(what)


def test_zero_consideration_refuses_non_zero_deductions() -> None:
    """As ``schedule_cg_portal_amounts`` refuses a zero consideration against a cost."""
    assert _refused(
        _PROVEN_STCG,
        consideration_inr=Decimal("0"),
        total_consideration_inr=Decimal("0"),
        balance_inr=Decimal("-24680"),
        result_inr=Decimal("-24680"),
    ) == _refusal(
        "total_deductions_inr must be zero when consideration_inr is zero; the ITR "
        "refuses deductions against zero consideration"
    )


@pytest.mark.parametrize("attribute", ["stcg_section", "ltcg_section"])
def test_a_result_section_must_be_its_schedules(attribute: str) -> None:
    result = _build(sale("12", "10"), sale("12", "10", long_term=True))
    schedule = attribute[:4].upper()
    what = f"InvariantError: ScheduleCgFilingResult.{attribute} must be the {schedule} section"
    swapped = result.ltcg_section if attribute == "stcg_section" else result.stcg_section

    for wrong in (None, 5, "STCG", swapped):
        with pytest.raises(InvariantError) as raised:
            replace(result, **{attribute: wrong})
        assert str(raised.value) == what


# A valid portal-amounts object whose section is invalid: a negative cost.
_NEGATIVE_COST_AMOUNTS = ScheduleCgPortalAmounts(
    consideration_inr=Decimal("13579"),
    cost_of_acquisition_inr=Decimal("-24680"),
    balance_inr=Decimal("38259"),
    gain_inr_full_precision=Decimal("38259"),
    gain_inr_rounded=Decimal("38259"),
    filed_total_inr=Decimal("38259"),
    component_rounding_difference_inr=Decimal("0"),
)
_NEGATIVE_COST_REFUSAL = _refusal("cost_of_acquisition_inr must not be negative", "value")

# Each case: the schedule whose section is invalid, the lots, and the refusal.
_INVALID_SECTION = {
    "beside-an-unreviewed-venue": (
        ScheduleCgStcgComputeResult,
        (_on(sale("12", "10"), listing_exchange="OTC"),),
        _blocked_refusal(FILING_VENUE_KIND),
    ),
    "beside-unreviewed-venue-and-class-and-positive-ltcg": (
        ScheduleCgStcgComputeResult,
        (
            _on(sale("12", "10"), listing_exchange="PINK", sub_category="WARRANT"),
            sale("10", "12", long_term=True, lot_ordinal=3, sale_ordinal=4),
        ),
        _blocked_refusal(FILING_VENUE_KIND, FILING_CLASS_KIND, FILING_LTCG_KIND),
    ),
    # Positive STCG no longer blocks the filing (#1360).
    "ltcg-beside-positive-stcg": (
        ScheduleCgLtcgComputeResult,
        (
            sale("10", "12"),
            sale("12", "10", long_term=True, lot_ordinal=3, sale_ordinal=4),
        ),
        _NEGATIVE_COST_REFUSAL,
    ),
    "without-a-blocker": (
        ScheduleCgStcgComputeResult,
        (sale("12", "10"),),
        _NEGATIVE_COST_REFUSAL,
    ),
}


@pytest.mark.parametrize(
    ("engine", "lots", "refusal"), _INVALID_SECTION.values(), ids=_INVALID_SECTION.keys()
)
def test_an_invalid_section_beside_a_filing_blocker_refuses_naming_each_blocker_kind(
    monkeypatch: pytest.MonkeyPatch,
    engine: type[ScheduleCgStcgComputeResult | ScheduleCgLtcgComputeResult],
    lots: tuple[RobinhoodCapitalGainLot, ...],
    refusal: str,
) -> None:
    """#1078: as invalid portal amounts do (#1047), an invalid section doesn't
    hide an earlier filing refusal."""
    monkeypatch.setattr(engine, "portal_amounts", property(lambda _: _NEGATIVE_COST_AMOUNTS))

    with pytest.raises(InvariantError) as raised:
        _build(*lots)

    assert str(raised.value) == refusal
    assert _UNREVIEWED_VALUE.search(str(raised.value)) is None


# ---- #1360: each section's Table F row ----


def test_builder_carries_each_engines_table_f_row() -> None:
    """An STCG gain files its bands and an LTCG loss five zeros (#1360, #1383)."""
    lots = (
        sale("10", "12"),
        sale("12", "10", long_term=True, lot_ordinal=3, sale_ordinal=4),
    )

    result = _build(*lots)

    engines = cg_result(*lots)
    assert result.stcg_table_f == engines.stcg.table_f
    assert result.ltcg_table_f == engines.ltcg.table_f
    assert result.stcg_table_f == ScheduleCgTableFRow(
        ScheduleCgTableFCase.COMPUTED, tuple(Decimal(band) for band in ("200", "0", "0", "0", "0"))
    )
    assert result.ltcg_table_f == ScheduleCgTableFRow(
        ScheduleCgTableFCase.COMPUTED, (Decimal("0"),) * 5
    )


@pytest.mark.parametrize("attribute", ["stcg_table_f", "ltcg_table_f"])
def test_a_table_f_row_must_not_be_manual(attribute: str) -> None:
    """No filing section is manual any more, so neither is its Table F row
    (#1383); a loss across bands, whose row is blank, is still accepted."""
    result = _build(sale("12", "10"), sale("12", "10", long_term=True))
    what = (
        f"InvariantError: ScheduleCgFilingResult.{attribute} must be a Table F row that "
        "isn't manual"
    )
    manual = ScheduleCgTableFRow(ScheduleCgTableFCase.MANUAL_SECTION, None)
    loss_across_bands = ScheduleCgTableFRow(ScheduleCgTableFCase.LOSS_ACROSS_BANDS, None)

    for wrong in (None, "manual-section", manual):
        with pytest.raises(InvariantError) as raised:
            replace(result, **{attribute: wrong})
        assert str(raised.value) == what
    assert getattr(replace(result, **{attribute: loss_across_bands}), attribute) == (
        loss_across_bands
    )


_TABLE_F_REFUSAL = "InvariantError: Schedule CG Table F: a stand-in refusal"


def _refusing_table_f(_: object) -> ScheduleCgTableFRow:
    raise invariant_error(what="Schedule CG Table F: a stand-in refusal")


@pytest.mark.parametrize(
    ("engine", "lots", "refusal"),
    [
        (
            ScheduleCgStcgComputeResult,
            (_on(sale("10", "12"), listing_exchange="OTC"),),
            _blocked_refusal(FILING_VENUE_KIND),
        ),
        (
            ScheduleCgLtcgComputeResult,
            (sale("10", "12", long_term=True),),
            _blocked_refusal(FILING_LTCG_KIND),
        ),
        (ScheduleCgStcgComputeResult, (sale("10", "12"),), _TABLE_F_REFUSAL),
    ],
    ids=["stcg-beside-an-unreviewed-venue", "ltcg-beside-positive-ltcg", "without-a-blocker"],
)
def test_a_table_f_refusal_beside_a_filing_blocker_refuses_naming_each_blocker_kind(
    monkeypatch: pytest.MonkeyPatch,
    engine: type[ScheduleCgStcgComputeResult | ScheduleCgLtcgComputeResult],
    lots: tuple[RobinhoodCapitalGainLot, ...],
    refusal: str,
) -> None:
    """As invalid portal amounts do (#1047), a Table F refusal doesn't hide an
    earlier filing refusal."""
    monkeypatch.setattr(engine, "table_f", property(_refusing_table_f))

    with pytest.raises(InvariantError) as raised:
        _build(*lots)

    assert str(raised.value) == refusal
