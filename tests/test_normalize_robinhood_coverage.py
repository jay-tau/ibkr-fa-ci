"""Pure Robinhood bundle checks: report coverage, inventory routing, corrections."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import cast

import pytest

from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountsReconciliation,
    RobinhoodBundleError,
    RobinhoodSecurityEventReference,
    RobinhoodSecurityQuantityResult,
    validate_robinhood_corrected_statement,
    validate_robinhood_inventory_partition,
    validate_robinhood_report_coverage,
)
from ibkr_fa.parser.robinhood_statement import RobinhoodStatement, parse_robinhood_statement
from tests._robinhood_refusals import CORRECTED_STATEMENT_MISMATCH
from tests._robinhood_statement_pages import brokerage_statement as _statement

_FY = FinancialYear(2025)
_UNION_END = date(2026, 3, 31)
_OPENING = date(2024, 6, 3)
# With the tax-forms test below, the literal pins of evidence_period_remedy;
# other modules build theirs from it (#1580).
_REMEDY = (
    "For each account named, supply an activity report and monthly statements from the "
    "account's opening date to 31 March 2026, the end of financial year 2025-26, with that "
    "report's [[robinhood.reports]] start_date and end_date set to match, and re-run"
)


def _coverage(
    labels: tuple[str, str] = ("Individual", "Managed Individual"),
    tax_forms: bool = False,
    /,
    *,
    start: date = _OPENING,
    end: date = _UNION_END,
    coverage_end: date = _UNION_END,
    second_end: date | None = None,
) -> None:
    """Both accounts, by statement family ``labels``, have evidence from
    ``start`` to ``end``, the second's to ``second_end`` when given; the run
    reads Forms 1042-S when ``tax_forms``."""
    accounts = (("100000001", _OPENING), ("200000002", _OPENING))
    ends = (end, end if second_end is None else second_end)
    paired = SimpleNamespace(
        accounts=tuple(
            SimpleNamespace(
                activity=SimpleNamespace(
                    primary_bundle=SimpleNamespace(account_number=number, coverage_end=coverage_end)
                ),
                evidence=SimpleNamespace(start_date=start, end_date=account_end),
            )
            for (number, _opening), account_end in zip(accounts, ends, strict=True)
        ),
        account_labels=dict(enumerate(labels)),
    )
    config = SimpleNamespace(
        accounts=tuple(
            SimpleNamespace(number=number, opening_date=opening) for number, opening in accounts
        )
    )
    validate_robinhood_report_coverage(
        cast("RobinhoodAccountsReconciliation", paired),
        cast("RobinhoodConfig", config),
        _FY,
        tax_forms=tax_forms,
    )


def test_report_coverage_accepts_exact_opening_and_union_end() -> None:
    _coverage()


@pytest.mark.parametrize(
    ("dates", "message"),
    [
        (
            {"start": date(2024, 6, 4)},
            "activity report starts after its reconciled account opening date",
        ),
        (
            {"end": date(2026, 3, 30)},
            "activity report ends before 31 March 2026 (Individual and Managed Individual "
            f"accounts). {_REMEDY}",
        ),
        (
            {"coverage_end": date(2026, 3, 30)},
            "issued brokerage statements end before the requested CY/FY union",
        ),
    ],
)
def test_report_coverage_refuses_each_gap(dates: dict[str, date], message: str) -> None:
    with pytest.raises(RobinhoodBundleError) as raised:
        _coverage(**dates)
    assert str(raised.value) == message


@pytest.mark.parametrize(
    ("labels", "named"),
    [
        (("Individual", "Managed Individual"), "Managed Individual account"),
        (("Individual", "Individual"), "Individual (index 1) account"),
    ],
    ids=["own-label", "shared-label"],
)
def test_short_evidence_names_only_the_accounts_that_end_early(
    labels: tuple[str, str], named: str
) -> None:
    """#1498: only the second account's evidence ends early, so only it is
    named, by statement family, with its index when both share one."""
    with pytest.raises(RobinhoodBundleError) as raised:
        _coverage(labels, second_end=date(2026, 2, 28))
    assert str(raised.value) == f"activity report ends before 31 March 2026 ({named}). {_REMEDY}"


def test_short_evidence_in_a_run_reading_tax_forms_names_the_form_year_end() -> None:
    """#1503: income compute needs evidence through 31 December 2026 for an
    account whose 2026 Form 1042-S the run reads, so the remedy says so."""
    with pytest.raises(RobinhoodBundleError) as raised:
        _coverage(("Individual", "Managed Individual"), True, second_end=date(2026, 2, 28))
    assert str(raised.value) == (
        "activity report ends before 31 March 2026 (Managed Individual account). For each "
        "account named, supply an activity report and monthly statements from the account's "
        "opening date to 31 March 2026, the end of financial year 2025-26 (31 December 2026 for "
        "any account whose Form 1042-S for 2026 is in the input's tax-forms folder, even if "
        "not named in this refusal), with that "
        "report's [[robinhood.reports]] start_date and end_date set to match, and re-run"
    )
    _coverage(
        ("Individual", "Managed Individual"),
        True,
        second_end=date(2026, 12, 31),
        coverage_end=date(2026, 12, 31),
    )


def _ref(account_index: int, event_ordinal: int) -> RobinhoodSecurityEventReference:
    return RobinhoodSecurityEventReference(account_index, event_ordinal)


def _partition(
    *,
    option_refs: tuple[RobinhoodSecurityEventReference, ...] = (_ref(0, 3),),
    neutral_refs: tuple[RobinhoodSecurityEventReference, ...] = (_ref(0, 4), _ref(1, 3)),
) -> None:
    paired = SimpleNamespace(
        accounts=tuple(
            SimpleNamespace(
                evidence=SimpleNamespace(
                    events=tuple(SimpleNamespace(source_record_index=index) for index in indices)
                )
            )
            for indices in ((1, 2, 3, 4), (1, 2, 3))
        )
    )
    quantities = SimpleNamespace(
        acquisitions=(
            SimpleNamespace(event_reference=_ref(0, 1)),
            SimpleNamespace(event_reference=_ref(1, 1)),
        ),
        movements=(
            # An acquisition is also a movement; the check compares sets.
            SimpleNamespace(event_references=(_ref(0, 1),)),
            SimpleNamespace(event_references=(_ref(0, 2), _ref(1, 2))),
        ),
        option_event_references=option_refs,
        neutral_event_references=neutral_refs,
    )
    validate_robinhood_inventory_partition(
        cast("RobinhoodAccountsReconciliation", paired),
        cast("RobinhoodSecurityQuantityResult", quantities),
    )


def test_inventory_partition_accepts_every_route_across_both_accounts() -> None:
    _partition()


@pytest.mark.parametrize(
    "routes",
    [
        {"neutral_refs": (_ref(0, 4),)},
        {"option_refs": (_ref(0, 3), _ref(1, 9))},
    ],
    ids=["parsed-event-unrouted", "routed-reference-unparsed"],
)
def test_inventory_partition_refuses_uncovered_or_unknown_events(
    routes: dict[str, tuple[RobinhoodSecurityEventReference, ...]],
) -> None:
    with pytest.raises(RobinhoodBundleError) as raised:
        _partition(**routes)
    assert str(raised.value) == "Robinhood inventory routing does not cover every parsed event"


def _parsed_statement(**changes: object) -> RobinhoodStatement:
    return replace(parse_robinhood_statement(_statement()), **changes)


def test_corrected_statement_matching_account_and_period_is_accepted() -> None:
    validate_robinhood_corrected_statement(_parsed_statement(), _parsed_statement())


@pytest.mark.parametrize(
    "changes",
    [
        {"account_number": "222222222"},
        {"period_start": date(2024, 12, 31)},
        {"period_end": date(2025, 2, 1)},
    ],
    ids=["account", "period-start", "period-end"],
)
def test_corrected_statement_refuses_a_different_account_or_period(
    changes: dict[str, object],
) -> None:
    with pytest.raises(RobinhoodBundleError) as raised:
        validate_robinhood_corrected_statement(_parsed_statement(), _parsed_statement(**changes))
    assert str(raised.value) == CORRECTED_STATEMENT_MISMATCH
