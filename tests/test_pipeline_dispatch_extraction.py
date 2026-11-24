"""Tests pinning the :mod:`ibkr_fa.pipeline_dispatch` extraction (issue #155).

Sister to the per-schedule extraction tests — verifies the final
shared-core extraction (dispatcher helpers + section constants).
This extraction closes #155: the 2746-line pre-extraction
pipeline.py has been refactored into a thin shared core (~520 lines)
+ 5 per-subcommand modules (FA, OS, CG, CG-LTCG, dispatch).

Contract:
* Direct imports work (new caller path).
* Legacy ``from ibkr_fa.pipeline import _dispatch_…`` and
  ``ibkr_fa.pipeline._dispatch_…`` keep working through the
  re-exports in pipeline.py.
* Both import paths resolve to the SAME Python object.
* Module docstring references issue #155 for git-blame anchor.
"""

from __future__ import annotations

import dataclasses
import functools
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import ibkr_fa.pipeline
import ibkr_fa.pipeline_dispatch
from ibkr_fa.compute.source_coverage import require_calendar_year_period
from ibkr_fa.exceptions import ParseError
from ibkr_fa.models import DividendEvidence, OpenLot, SourceBundle, StatementPeriod
from ibkr_fa.models.net_asset_value import NavRefusalReason, NetAssetValueSummary
from ibkr_fa.models.source import FlexPeriod
from ibkr_fa.pipeline_dispatch import _dispatch_activity_statement, _dispatch_flex_query
from tests._flex_records import eof_record

_FIXTURES = Path(__file__).parent / "fixtures"


def test_direct_import_exposes_dispatchers() -> None:
    """The new module exposes the two top-level dispatchers."""
    assert callable(ibkr_fa.pipeline_dispatch._dispatch_flex_query)
    assert callable(ibkr_fa.pipeline_dispatch._dispatch_activity_statement)


def test_direct_import_exposes_cross_check_helper() -> None:
    """The account-ID safety net is shared by every per-schedule
    pipeline; it must be exposed at the same name in the new home."""
    assert callable(ibkr_fa.pipeline_dispatch._cross_check_account_id)


def test_direct_import_exposes_section_constants() -> None:
    """Section-set constants moved to pipeline_dispatch.py with the
    helpers that consume them."""
    assert isinstance(ibkr_fa.pipeline_dispatch._FA_DEFAULT_REQUIRED_ACTIVITY_SECTIONS, frozenset)
    assert isinstance(ibkr_fa.pipeline_dispatch._OS_DEFAULT_REQUIRED_ACTIVITY_SECTIONS, frozenset)
    assert isinstance(ibkr_fa.pipeline_dispatch._CG_DEFAULT_REQUIRED_ACTIVITY_SECTIONS, frozenset)


def test_legacy_pipeline_import_path_still_works_for_dispatchers() -> None:
    """Backward-compat: pipeline.py re-exports both dispatchers and the
    account cross-check, so ``from ibkr_fa.pipeline import
    _dispatch_activity_statement`` and ``ibkr_fa.pipeline._dispatch_…``
    keep working. The next test checks that both paths give the same
    objects."""
    assert hasattr(ibkr_fa.pipeline, "_dispatch_activity_statement")
    assert hasattr(ibkr_fa.pipeline, "_dispatch_flex_query")
    assert hasattr(ibkr_fa.pipeline, "_cross_check_account_id")


def test_legacy_and_direct_imports_are_the_same_object() -> None:
    """Identity check: both import paths MUST resolve to the same
    Python object. Critical for monkeypatch / mock interactions."""
    assert ibkr_fa.pipeline._dispatch_flex_query is ibkr_fa.pipeline_dispatch._dispatch_flex_query
    assert (
        ibkr_fa.pipeline._dispatch_activity_statement
        is ibkr_fa.pipeline_dispatch._dispatch_activity_statement
    )
    assert (
        ibkr_fa.pipeline._cross_check_account_id
        is ibkr_fa.pipeline_dispatch._cross_check_account_id
    )


def test_extraction_module_docstring_references_issue_155() -> None:
    """The extraction module's docstring must reference issue #155 so
    a future reader doing ``git blame`` finds the rationale for the
    split."""
    docstring = ibkr_fa.pipeline_dispatch.__doc__ or ""
    assert "#155" in docstring


def test_source_bundle_fields_and_frozen() -> None:
    """`SourceBundle` (#355, #625) is the seam every IBKR dispatcher
    returns; callers read it by attribute, so only the field set matters."""
    assert [f.name for f in dataclasses.fields(SourceBundle)] == [
        "account_info",
        "open_lots",
        "closed_lots",
        "dividends",
        "withholding_tax",
        "interest_credits",
        "interest_non_credits",
        "identity_fallback_used",
        "dividend_evidence",
        "net_asset_value",
    ]
    assert all(f.kw_only for f in dataclasses.fields(SourceBundle))
    bundle = _dispatch_flex_query(
        _FIXTURES / "e2e_year_end_2025.csv",
        snapshot_validator=lambda _lots: None,
        period_validator=lambda _period: None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        bundle.dividends = []  # ty: ignore[invalid-assignment]


def test_activity_statement_period_reaches_the_period_validator() -> None:
    """The dispatcher checks the statement's own parsed period."""
    periods: list[StatementPeriod] = []
    _dispatch_activity_statement(
        _FIXTURES / "e2e_activity_statement_2025.csv",
        period_validator=periods.append,
    )

    assert periods == [StatementPeriod(date(2025, 1, 1), date(2025, 12, 31))]


def test_flex_period_reaches_the_period_validator() -> None:
    """The dispatcher hands the validator the ``BOF`` record's period (#1313)."""
    periods: list[FlexPeriod | None] = []
    _dispatch_flex_query(
        _FIXTURES / "e2e_year_end_2025.csv",
        snapshot_validator=lambda _lots: None,
        period_validator=periods.append,
    )

    assert periods == [FlexPeriod(date(2025, 1, 1), date(2025, 12, 31), line=0)]


def test_flex_period_validator_follows_the_closed_lots(monkeypatch: pytest.MonkeyPatch) -> None:
    """The period checks run last, so a closed-lot refusal comes first (#1313)."""
    refusal = ParseError(
        section="Trades", row_index=1, column="Buy/Sell", message="Lots refused", raw_value=""
    )

    def refuse_closed_lots(*_args: object, **_kwargs: object) -> None:
        raise refusal

    def fail_period(_period: FlexPeriod | None) -> None:
        pytest.fail("The period must not be checked before the closed lots parse")

    monkeypatch.setattr(ibkr_fa.pipeline_dispatch, "parse_closed_lots", refuse_closed_lots)
    with pytest.raises(ParseError) as exc:
        _dispatch_flex_query(
            _FIXTURES / "e2e_year_end_2025.csv",
            snapshot_validator=lambda _lots: None,
            period_validator=fail_period,
        )
    assert exc.value is refusal


@pytest.mark.parametrize(
    ("fixture", "evidence"),
    [
        ("e2e_activity_statement_2025.csv", DividendEvidence.ABSENT),
        ("e2e_activity_statement_with_dividends_2025.csv", DividendEvidence.PRESENT),
    ],
)
def test_activity_statement_bundle_records_dividend_evidence(
    fixture: str, evidence: DividendEvidence
) -> None:
    bundle = _dispatch_activity_statement(
        _FIXTURES / fixture, period_validator=lambda _period: None
    )

    assert bundle.dividend_evidence is evidence


def test_flex_query_dividend_evidence_is_not_determinable() -> None:
    bundle = _dispatch_flex_query(
        _FIXTURES / "e2e_year_end_2025.csv",
        snapshot_validator=lambda _lots: None,
        period_validator=lambda _period: None,
    )

    assert bundle.dividend_evidence is DividendEvidence.NOT_DETERMINABLE


def test_dispatchers_parse_nav_only_when_asked() -> None:
    statement = _FIXTURES / "e2e_activity_statement_with_nav_2025.csv"
    flex = _FIXTURES / "e2e_year_end_2025.csv"

    def activity(*, parse_nav: bool) -> SourceBundle:
        return _dispatch_activity_statement(
            statement, period_validator=lambda _period: None, parse_nav=parse_nav
        )

    def flex_query(*, parse_nav: bool) -> SourceBundle:
        return _dispatch_flex_query(
            flex,
            snapshot_validator=lambda _lots: None,
            period_validator=lambda _period: None,
            parse_nav=parse_nav,
        )

    assert activity(parse_nav=False).net_asset_value is None
    assert flex_query(parse_nav=False).net_asset_value is None
    nav = activity(parse_nav=True).net_asset_value
    assert nav is not None
    # The fixture's NAV Total row: 100.00 cash + 2007.60 stock (954.00 + 1053.60).
    assert nav.closing_total == Decimal("2107.60")
    assert flex_query(parse_nav=True).net_asset_value == NetAssetValueSummary(
        closing_total=None, refusal_reason=NavRefusalReason.FLEX_QUERY
    )


def test_flex_snapshot_refusal_follows_all_lots_and_precedes_closed_lots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refusal = ParseError(
        section="Open Positions (LOT)",
        row_index=1,
        column="ReportDate",
        message="Snapshot refused",
        raw_value="20251231",
    )

    def refuse_snapshot(lots: list[OpenLot]) -> None:
        assert [lot.symbol for lot in lots] == ["XQBB", "XQBE"]
        raise refusal

    def fail_closed_lot_parsing(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Closed lots must not parse before snapshot validation")

    monkeypatch.setattr(ibkr_fa.pipeline_dispatch, "parse_closed_lots", fail_closed_lot_parsing)
    with pytest.raises(ParseError) as exc:
        _dispatch_flex_query(
            _FIXTURES / "e2e_year_end_2025.csv",
            snapshot_validator=refuse_snapshot,
            period_validator=lambda _period: None,
        )
    assert exc.value is refusal


def test_flex_snapshot_refusal_precedes_the_missing_trades_refusal(tmp_path: Path) -> None:
    """#1286 checks for the Trades section after the snapshot, so a Flex Query
    without one still gets the snapshot refusal first; with the snapshot
    accepted, it refuses at the missing section."""
    sections, _trades = (
        (_FIXTURES / "e2e_year_end_2025.csv").read_text(encoding="utf-8").rsplit("\n\n", 1)
    )
    flex = tmp_path / "flex.csv"
    flex.write_text(sections + "\n" + eof_record(), encoding="utf-8")
    refusal = ParseError(
        section="Open Positions (LOT)",
        row_index=0,
        column="ReportDate",
        message="Snapshot refused",
        raw_value="",
    )

    def refuse_snapshot(_lots: list[OpenLot]) -> None:
        raise refusal

    with pytest.raises(ParseError) as exc:
        _dispatch_flex_query(
            flex, snapshot_validator=refuse_snapshot, period_validator=lambda _period: None
        )
    assert exc.value is refusal
    with pytest.raises(ParseError) as exc:
        _dispatch_flex_query(
            flex, snapshot_validator=lambda _lots: None, period_validator=lambda _period: None
        )
    assert (exc.value.section, exc.value.row_index, exc.value.column) == ("Trades", 0, None)


def test_activity_period_refusal_precedes_account_and_trades_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_later_parsing(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Account and Trades must not parse before period validation")

    monkeypatch.setattr(
        ibkr_fa.pipeline_dispatch, "parse_activity_statement_account_info", fail_later_parsing
    )
    monkeypatch.setattr(
        ibkr_fa.pipeline_dispatch, "parse_activity_statement_trades", fail_later_parsing
    )
    with pytest.raises(ParseError) as exc:
        _dispatch_activity_statement(
            _FIXTURES / "e2e_activity_statement_2025.csv",
            period_validator=functools.partial(require_calendar_year_period, calendar_year=2026),
        )
    assert exc.value.section == "Statement"
    assert exc.value.column == "Period"


def test_direct_import_works_without_pipeline_preloaded() -> None:
    """Regression-lock: ``import ibkr_fa.pipeline_dispatch`` BEFORE
    ``import ibkr_fa.pipeline`` must NOT trigger a circular
    ImportError. pipeline_dispatch is a leaf-level module (only
    imports stdlib + ibkr_fa.exceptions / .invariants / .models /
    .parser / .pii) so there's no circular dependency back to
    pipeline, but the test pins the leaf property — if a future
    refactor adds a pipeline-back-import to pipeline_dispatch, this
    test catches it."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ibkr_fa.pipeline_dispatch as pd; "
            "assert callable(pd._dispatch_flex_query); "
            "assert callable(pd._dispatch_activity_statement); "
            "assert callable(pd._cross_check_account_id); "
            "print('OK')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"direct import of pipeline_dispatch failed:\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"
