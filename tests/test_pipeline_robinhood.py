"""Pipeline-level orchestration checks for Robinhood commands."""

from __future__ import annotations

import csv
import errno
import itertools
import re
import sys
from collections.abc import Callable
from dataclasses import fields, replace
from datetime import date
from decimal import Decimal
from functools import partial
from io import BytesIO, TextIOWrapper
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, NoReturn, cast

import pytest
from openpyxl import load_workbook

import ibkr_fa.parser.robinhood_1042s as tax_form_parser
import ibkr_fa.pipeline_robinhood as pipeline
import ibkr_fa.pipeline_robinhood_bundle as bundle_mod
import ibkr_fa.pipeline_robinhood_core as core
import ibkr_fa.pipeline_robinhood_outputs as outputs
import ibkr_fa.pipeline_robinhood_reporting as reporting
from ibkr_fa import cli
from ibkr_fa.compute.form67 import Form67ComputeResult, Form67Readiness
from ibkr_fa.compute.robinhood_a2_peak import compute_robinhood_a2_peaks
from ibkr_fa.compute.robinhood_capital_gains import (
    RobinhoodCapitalGainsBlocker,
    RobinhoodCapitalGainsBlockerKind,
    RobinhoodSalePriceCheck,
    robinhood_sale_price_checks,
)
from ibkr_fa.compute.robinhood_fa import (
    RobinhoodFaBlocker,
    RobinhoodFaBlockerKind,
    RobinhoodFaManualKind,
    RobinhoodFaManualWork,
    _a2_gross_evidence,
)
from ibkr_fa.compute.robinhood_income import (
    RobinhoodIncomeBlocker,
    RobinhoodIncomeBlockerCause,
    RobinhoodIncomeBlockerKind,
    RobinhoodIncomeEventReference,
    RobinhoodIncomeResult,
)
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionBlockerKind,
    RobinhoodOptionCapitalGainBucket,
    RobinhoodOptionClassificationError,
    RobinhoodOptionsError,
    RobinhoodOptionsResult,
    convert_robinhood_options,
    option_blocker_summary,
)
from ibkr_fa.compute.schedule_cg_table_f import ScheduleCgTableFCase, ScheduleCgTableFRow
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult, compute_schedule_tr_from_fsi
from ibkr_fa.config import (
    Form67Config,
    Form67TaxPayableOverride,
    FxConfig,
    RobinhoodOptionTreatment,
)
from ibkr_fa.exceptions import ConfigError, IbkrFaError, InvariantError, MissingFxRateError
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.fx.ttbr_loader import load_ttbr
from ibkr_fa.models.fa_a2_row import FaA2NatureOfAmount
from ibkr_fa.models.fa_a3_row import FaA3PeakBlankReason
from ibkr_fa.models.form67_entry import Form67Entry, Form67SourceOfIncome, Form67TreatyRateStatus
from ibkr_fa.models.schedule_cg_row import is_short_term_holding
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.normalize.robinhood import (
    RobinhoodReconciliationError,
    resolve_robinhood_security_quantities,
)
from ibkr_fa.normalize.robinhood.security import (
    RobinhoodSecurityEventReference,
)
from ibkr_fa.output._cleanup import (
    ClaimedOutputCleanup,
    FileIdentity,
    OutputCollisionError,
    OutputRemovalError,
)
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.base import Subcommand
from ibkr_fa.output.cbdt_json_fragment import (
    CbdtJsonIncompleteError,
    CbdtJsonUnreadableError,
    CbdtScheduleFaJsonWriter,
    preclear_own_cbdt_fragment,
)
from ibkr_fa.output.cbdt_json_schema import A3_DEFINITION, CbdtSchemaError
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import read_robinhood_statement
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_robinhood import (
    ROBINHOOD_FNO_FEMA_WARNING,
    RobinhoodLegStatus,
    RobinhoodPipelineError,
    _clear_all,
    _income_warnings,
    run_robinhood_all,
    run_robinhood_cg,
    run_robinhood_fa,
    run_robinhood_income,
    run_robinhood_options,
)
from ibkr_fa.pipeline_robinhood_bundle import identity_failure_guidance
from ibkr_fa.pipeline_robinhood_core import RobinhoodItrFormError
from ibkr_fa.robinhood_identity_sources import IdentitySourceAudit
from ibkr_fa.types import isin_check_digit
from tests._crash_reports import (
    assert_prints_no_marker,
    assert_replaced_unchained,
    internal_error_line,
)
from tests._exact_refusals import INEXACT_SUM
from tests._ftc_warnings import (
    BLANK_RELIEF_TIN_WARNING,
    refused_override,
    same_day_gap,
    source_rate_gap,
    unvendored_dtaa_gap,
)
from tests._ftc_warnings import FORM67_RELIEF_REFUSAL as _RELIEF_REFUSAL
from tests._ftc_warnings import PORTAL_RELIEF_REFUSAL as _PORTAL_RELIEF_REFUSAL
from tests._fx_stubs import AuditedFxStub
from tests._fx_stubs import rule115_rates as _rule115_rates
from tests._no_echo import ACCOUNT, assert_no_markers
from tests._output_removal_failures import fail_unlink as _fail_unlink
from tests._robinhood_1042s import robinhood_1042s_text as _tax_packet
from tests._robinhood_account_inputs import pair_with_security as _pair_with_security
from tests._robinhood_account_inputs import receipt_bundle as _receipt_bundle
from tests._robinhood_account_inputs import security_config as _security_config
from tests._robinhood_bundle_stubs import patch_bundle_sources as _patch_sources
from tests._robinhood_bundle_stubs import with_bundle_texts as _with_bundle_texts
from tests._robinhood_bundle_stubs import write_bundle as _write_bundle
from tests._robinhood_cg import (
    ACCOUNT_LABELS,
    CG_UNRESOLVED,
    CONSIDERATION_BASIS,
    CONSIDERATION_POSITION,
    FILING_CLASS_KIND,
    FILING_LTCG_KIND,
    FILING_LTCG_ROUTE,
    FILING_VENUE_KIND,
    FILING_VENUE_ROUTE,
    PROVEN_GROSS_REFUSAL,
    TRANSFER_EXPENDITURE_BASIS,
    cg_result,
    sale,
)
from tests._robinhood_cg_inputs import LONG_TERM_BUY as _LONG_TERM_BUY
from tests._robinhood_cg_inputs import SALE_AT_100 as _SALE_AT_100
from tests._robinhood_cg_inputs import SALE_AT_110 as _SALE_AT_110
from tests._robinhood_cg_inputs import SALE_ORDER_DAY as _SALE_DAY
from tests._robinhood_cg_inputs import SALE_ORDER_REASON as _SALE_ORDER_REASON
from tests._robinhood_cg_inputs import SHORT_TERM_BUY as _SHORT_TERM_BUY
from tests._robinhood_cg_inputs import cg_from_lots as _robinhood_cg_from_lots
from tests._robinhood_cg_inputs import cg_lot_result as _lot_result
from tests._robinhood_cg_inputs import cg_slice as _slice
from tests._robinhood_cg_inputs import cg_trade as _trade
from tests._robinhood_cg_inputs import sale_order_case as _sale_order_case
from tests._robinhood_cg_review import one_share_sales_cg as _xqaa_cg
from tests._robinhood_cg_review import review_rows as _review_rows
from tests._robinhood_cli import FINANCIAL_YEAR as _FY
from tests._robinhood_cli import LEG_LINE as _LEG_LINE
from tests._robinhood_cli import OWNED_FILENAMES as _OWNED
from tests._robinhood_cli import outputs_left as _left
from tests._robinhood_cli import pipeline_run_kwargs as _run_kwargs
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_cli import robinhood_main_all as _main_all
from tests._robinhood_exact_bundle import SECURITY as _XQAA
from tests._robinhood_fa_blocker_lines import FA_UNRESOLVED
from tests._robinhood_fa_json import schema_invalid_json_result as _schema_invalid_json_result
from tests._robinhood_identity_inputs import issuer_identity_sources as _identity_sources
from tests._robinhood_income_inputs import INCOME_PAYMENT_DATE as _PAYMENT_DATE
from tests._robinhood_income_inputs import NON_US_MARKER_AMOUNT as _NON_US_MARKER_AMOUNT
from tests._robinhood_income_inputs import NON_US_MARKERS as _NON_US_MARKERS
from tests._robinhood_income_inputs import ZERO_ACCOUNT as _ZERO_ACCOUNT
from tests._robinhood_income_inputs import ZERO_FILED as _ZERO_FILED
from tests._robinhood_income_inputs import ZERO_MARKERS as _ZERO_MARKERS
from tests._robinhood_income_inputs import ZERO_REFUSED as _ZERO_REFUSED
from tests._robinhood_income_inputs import income_base_events as _income_base_events
from tests._robinhood_income_inputs import income_config as _income_config
from tests._robinhood_income_inputs import income_event as _income_event
from tests._robinhood_income_inputs import income_packet as _income_packet
from tests._robinhood_income_inputs import income_paired as _income_paired
from tests._robinhood_income_inputs import non_us_income_config as _non_us_config
from tests._robinhood_income_inputs import non_us_income_events as _non_us_events
from tests._robinhood_income_inputs import shared_user_config as _shared_config
from tests._robinhood_income_inputs import zero_form as _zero_form
from tests._robinhood_income_inputs import zero_rows as _zero_rows
from tests._robinhood_income_stubs import (
    dividend_entry,
    form67_assembly,
)
from tests._robinhood_income_stubs import (  # noqa: F401 - autouse fixture
    silenced_default_income_warnings as _silence_default_income_warnings,
)
from tests._robinhood_option_inputs import SYNTH_MARKERS as _SYNTH_MARKERS
from tests._robinhood_option_inputs import closed_option_events as _closed_events
from tests._robinhood_option_inputs import compute_synthetic_options as _robinhood_options
from tests._robinhood_option_inputs import option_event as _option
from tests._robinhood_option_inputs import option_paired as _option_paired
from tests._robinhood_option_inputs import synth_option_trade as _synth_trade
from tests._robinhood_option_runs import BTO as _BTO
from tests._robinhood_option_runs import LOCATED_BLOCKERS as _LOCATED
from tests._robinhood_option_runs import MAY_JUNE_2025_TRADES as _MAY_JUNE_2025_TRADES
from tests._robinhood_option_runs import OPTION_DESCRIPTION as _OPTION_DESCRIPTION
from tests._robinhood_option_runs import OPTION_UNDERLYING as _OPTION_UNDERLYING
from tests._robinhood_option_runs import STC as _STC
from tests._robinhood_option_runs import blocked_options as _blocked_options
from tests._robinhood_option_runs import option_cg_run as _option_cg_run
from tests._robinhood_option_runs import option_run as _option_run
from tests._robinhood_option_runs import option_trade as _option_trade
from tests._robinhood_pipeline_stubs import A3_LOT as _A3_LOT
from tests._robinhood_pipeline_stubs import OTHER_ACCOUNT as _OTHER_ACCOUNT
from tests._robinhood_pipeline_stubs import TIN_MARKER as _TIN_MARKER
from tests._robinhood_pipeline_stubs import assembly_for as _assembly_for
from tests._robinhood_pipeline_stubs import assert_cg_refused as _assert_cg_refused
from tests._robinhood_pipeline_stubs import claim_staged as _claim
from tests._robinhood_pipeline_stubs import complete_fa as _complete_fa
from tests._robinhood_pipeline_stubs import json_fa as _json_fa
from tests._robinhood_pipeline_stubs import stand_in_bundle as _bundle
from tests._robinhood_pipeline_stubs import stand_in_cg as _cg
from tests._robinhood_pipeline_stubs import stand_in_fa_prices as _fa_prices
from tests._robinhood_pipeline_stubs import stand_in_options as _options
from tests._robinhood_pipeline_stubs import stub_cg_run as _stub_cg_run
from tests._robinhood_pipeline_stubs import stub_fa_around_income as _stub_fa_around_income
from tests._robinhood_pipeline_stubs import stub_fa_before_prices as _stub_fa_before_prices
from tests._robinhood_pipeline_stubs import stub_income_bundle as _stub_income_bundle
from tests._robinhood_pipeline_stubs import stub_relief_run as _stub_relief_run
from tests._robinhood_pipeline_stubs import stub_successful_all as _stub_successful_all
from tests._robinhood_pipeline_stubs import stub_writing_all as _stub_writing_all
from tests._robinhood_refusals import (
    EVENT_REFERENCES,
    FNO_FEMA_WARNING,
    FRACTIONAL_RATE_ACTION,
    FTC_OS_REMOVED,
    INTEREST_PAYER_RECIPE,
    OPTION_BLOCKER_MESSAGES,
    OS_UNRESOLVED,
)
from tests._robinhood_security import (
    IDENTITY_GUIDANCE_FILE,
    IDENTITY_GUIDANCE_HEADER,
    configured_securities,
    identity_guidance_line,
    robinhood_security,
)
from tests._robinhood_statement_pages import LEDGER_HEADER as _LEDGER_HEADER
from tests._robinhood_statement_pages import NOT_MONEY_BANK_ROW as _NOT_MONEY
from tests._robinhood_statement_pages import PROGRAM_BANK_ROW as _BANK_ROW
from tests._robinhood_statement_pages import brokerage_statement as _brokerage_statement
from tests._robinhood_statement_pages import statement_activity_header as _activity_header
from tests._robinhood_toml import (
    robinhood_account_toml,
    robinhood_report_toml,
    robinhood_security_toml,
    robinhood_toml,
)
from tests._schedule_cg_filing_workbook import values_by_field as _values_by_field
from tests._writer_stubs import StubWriter, patch_writers


def test_standalone_option_warnings_keep_fema_separate() -> None:
    warnings = reporting.option_warnings(
        _options(
            has_trades=True,
            treatment=RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
        )
    )

    assert warnings[0] == ROBINHOOD_FNO_FEMA_WARNING
    assert len(warnings) == 2
    assert "section 73" in warnings[1]


@pytest.mark.parametrize(
    ("has_options", "error"),
    [
        # Frozen dataclass exceptions reject attribute assignment, which a
        # contextlib.contextmanager generator does on re-raise.
        (
            False,
            WriterContractError(violation="nul_byte", row_index=0, cell_index=0, trigger=""),
        ),
        (True, ConfigError(field="robinhood", message=f"{ROBINHOOD_FNO_FEMA_WARNING} bad")),
        (True, KeyboardInterrupt()),
    ],
    ids=["no-options-frozen", "warned-frozen", "base-exception"],
)
def test_fno_warning_on_error_propagates_error_unchanged(
    has_options: bool, error: BaseException
) -> None:
    with pytest.raises(type(error)) as raised, core.fno_warning_on_error(has_options):
        raise error
    assert raised.value is error


def test_fno_warning_on_error_prefixes_warning_when_options_exist() -> None:
    """A domain error's text is relayed; any other error is named by its class
    and source line only (#1044)."""
    domain = ConfigError(field="robinhood", message="bad")
    with pytest.raises(RobinhoodPipelineError) as raised, core.fno_warning_on_error(True):
        raise domain
    assert str(raised.value) == f"{ROBINHOOD_FNO_FEMA_WARNING} {domain}"
    assert_replaced_unchained(raised.value)

    error = ValueError(ACCOUNT)
    with pytest.raises(RobinhoodPipelineError) as raised, core.fno_warning_on_error(True):
        raise error
    # Raised in this test, so no ibkr_fa frame names its line.
    assert str(raised.value) == (
        f"{ROBINHOOD_FNO_FEMA_WARNING} internal error (ValueError at an unknown line)"
    )
    assert_no_markers(str(raised.value))
    assert_replaced_unchained(raised.value, ValueError, markers=(ACCOUNT,))


def _sale_price_check(
    account_index: int,
    event_ordinal: int,
    *,
    within_band: bool,
) -> RobinhoodSalePriceCheck:
    return RobinhoodSalePriceCheck(
        RobinhoodSecurityEventReference(account_index, event_ordinal),
        within_band,
    )


def test_capital_gains_warnings_are_privacy_safe_and_sorted() -> None:
    result = _cg(
        checks=(
            _sale_price_check(1, 8, within_band=False),
            _sale_price_check(0, 3, within_band=True),
            _sale_price_check(0, 12, within_band=False),
        )
    )

    assert reporting.capital_gains_warnings(cast("Any", _cg())) == ()
    assert reporting.capital_gains_warnings(cast("Any", result)) == (
        "2 Robinhood equity sale event(s) had exact recorded Amount outside the "
        "displayed quantity x price precision band; exact Amount was retained unchanged "
        f"and no fee was inferred. {EVENT_REFERENCES}0:12, 1:8.",
    )


def test_option_workpaper_marks_unemitted_business_fsi_manual(tmp_path: Path) -> None:
    summary = SimpleNamespace(
        account_index=0,
        treatment=SimpleNamespace(value="speculative-business"),
        schedule_bp_bucket="speculative-business",
        filing_complete=False,
        matched_premium_paid_usd=Decimal("100"),
        net_pnl_usd=Decimal("25"),
        net_pnl_inr=Decimal("2125"),
    )
    options = SimpleNamespace(
        trades=(object(),),
        lot_slices=(),
        classifications=(),
        capital_gain_lines=(),
        table_f={},
        business_summaries=(summary,),
        blockers=(),
        business_fsi_manual_work_required=True,
    )

    path = pipeline._write_options(
        cast("Any", options),
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-3",
    )

    assert path is not None
    text = path.read_text(encoding="utf-8-sig")
    assert "Schedule FSI (manual)" in text
    assert "Schedule FSI emitted" not in text


def test_standalone_fa_price_error_retains_fema_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    monkeypatch.setattr(
        pipeline,
        "_reconstruct_options",
        lambda *_args: _options(has_trades=True),
    )
    _stub_fa_before_prices(monkeypatch)
    monkeypatch.setattr(
        pipeline, "_compute_source_income", lambda *_args: SimpleNamespace(blockers=())
    )
    price_calls: list[object] = []

    def refuse_prices(*args: object, **_kwargs: object) -> NoReturn:
        price_calls.append(args)
        raise RobinhoodPipelineError("equity close coverage is incomplete")

    monkeypatch.setattr(pipeline, "_fa_prices", refuse_prices)
    with pytest.raises(RobinhoodPipelineError) as raised:
        run_robinhood_fa(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path,
            financial_year=_FY,
            itr_form="ITR-3",
        )
    assert len(price_calls) == 1
    assert str(raised.value) == f"{FNO_FEMA_WARNING} equity close coverage is incomplete"


def test_clear_all_preserves_foreign_json_and_removes_stale_csv(tmp_path: Path) -> None:
    foreign = tmp_path / "schedule-fa.json"
    foreign.write_text('{"ScheduleFA": {}}', encoding="utf-8")
    stale = tmp_path / "schedule-cg.csv"
    stale.write_text("stale", encoding="utf-8")
    stale_os = tmp_path / "schedule-os.csv"
    stale_os.write_text("stale", encoding="utf-8")
    stale_os_xlsx = tmp_path / "schedule-os.xlsx"
    stale_os_xlsx.write_text("stale", encoding="utf-8")

    assert _clear_all(tmp_path) is False
    assert foreign.exists()
    assert not stale.exists()
    assert not stale_os.exists()
    assert not stale_os_xlsx.exists()


def test_ftc_cleanup_does_not_remove_completed_schedule_os(tmp_path: Path) -> None:
    os_path = tmp_path / "schedule-os.xlsx"
    os_path.write_text("complete", encoding="utf-8")

    pipeline._clear_leg(tmp_path, outputs.INCOME_FILENAMES)

    assert os_path.read_text(encoding="utf-8") == "complete"


def test_income_event_coverage_accepts_only_payment_or_explicit_blocker() -> None:
    event = SimpleNamespace(
        kind=RobinhoodEventKind.STOCK_LENDING_INCOME,
        source_record_index=7,
    )
    bundle = SimpleNamespace(
        paired=SimpleNamespace(
            accounts=(SimpleNamespace(evidence=SimpleNamespace(events=(event,))),)
        )
    )
    reference = RobinhoodIncomeEventReference(0, 7)

    def income(event_references: tuple[RobinhoodIncomeEventReference, ...]) -> SimpleNamespace:
        return SimpleNamespace(
            dividend_payments=(),
            interest_payments=(),
            stock_lending_payments=(),
            fee_payments=(),
            withholding_payments=(),
            blockers=(
                RobinhoodIncomeBlocker(
                    kind=RobinhoodIncomeBlockerKind.JURISDICTION,
                    event_references=event_references,
                ),
            ),
        )

    core.validate_owned_events(
        cast("Any", bundle),
        core.ComputedRobinhood(income=cast("Any", income((reference,)))),
    )
    with pytest.raises(RobinhoodPipelineError, match="income layer"):
        core.validate_owned_events(
            cast("Any", bundle),
            core.ComputedRobinhood(income=cast("Any", income(()))),
        )


def _cg_coverage_bundle() -> SimpleNamespace:
    """Account 0 holds an equity sale (7) and a dividend (8); account 1 a
    managed equity sale (3)."""

    def account(*events: tuple[RobinhoodEventKind, int]) -> SimpleNamespace:
        return SimpleNamespace(
            evidence=SimpleNamespace(
                events=tuple(
                    SimpleNamespace(kind=kind, source_record_index=ordinal)
                    for kind, ordinal in events
                )
            )
        )

    return SimpleNamespace(
        paired=SimpleNamespace(
            accounts=(
                account(
                    (RobinhoodEventKind.EQUITY_SELL, 7),
                    (RobinhoodEventKind.CASH_DIVIDEND, 8),
                ),
                account((RobinhoodEventKind.MANAGED_EQUITY_SELL, 3)),
            )
        )
    )


def _cg_checking(*references: tuple[int, int]) -> core.ComputedRobinhood:
    return core.ComputedRobinhood(
        cg=replace(
            cg_result(),
            sale_price_checks=tuple(
                _sale_price_check(account_index, ordinal, within_band=True)
                for account_index, ordinal in references
            ),
        )
    )


def test_cg_event_coverage_accepts_only_every_equity_sale() -> None:
    """#1417: a CG result that checks exactly the two sales passes."""
    core.validate_owned_events(cast("Any", _cg_coverage_bundle()), _cg_checking((0, 7), (1, 3)))


@pytest.mark.parametrize(
    "references",
    [
        pytest.param(((0, 7),), id="managed-sale-missing"),
        pytest.param(((1, 3),), id="individual-sale-missing"),
        pytest.param((), id="no-sale"),
        pytest.param(((0, 7), (1, 3), (0, 8)), id="non-sale-event"),
    ],
)
def test_cg_event_coverage_refuses_a_sale_set_that_differs(
    references: tuple[tuple[int, int], ...],
) -> None:
    """#1417: a CG result missing a sale, or checking a non-sale, refuses."""
    with pytest.raises(
        RobinhoodPipelineError, match=r"\Acapital-gains layer does not cover every equity sale\Z"
    ):
        core.validate_owned_events(cast("Any", _cg_coverage_bundle()), _cg_checking(*references))


def test_cg_preclears_before_bundle_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = tmp_path / "schedule-cg.csv"
    stale.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(
        pipeline,
        "_load",
        lambda **_kwargs: (_ for _ in ()).throw(RobinhoodPipelineError("broken bundle")),
    )
    with pytest.raises(RobinhoodPipelineError, match="broken bundle"):
        run_robinhood_cg(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path,
            financial_year=_FY,
            itr_form="ITR-2",
        )
    assert not stale.exists()


def test_cg_surfaces_sale_price_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cg = _cg(checks=(_sale_price_check(1, 8, within_band=False),))
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: _bundle())
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: _options())
    monkeypatch.setattr(pipeline, "_compute_cg", lambda *_args: cg)
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    monkeypatch.setattr(pipeline, "_write_cg", lambda *_args, **_kwargs: ())

    report = run_robinhood_cg(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-2",
    )

    assert report.warnings == reporting.capital_gains_warnings(cast("Any", cg))


def test_all_mode_cg_leg_removes_debug_audits_when_filing_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robinhood-all's CG leg with the real writer: the debug audits are written,
    the filing workbook refuses, and the writer removes them too (#745)."""
    written_before_refusal: list[str] = []

    class RefusingFilingWriter(StubWriter):
        def write(self, _result: object, _path: Path) -> NoReturn:
            written_before_refusal.extend(sorted(path.name for path in tmp_path.iterdir()))
            raise RobinhoodPipelineError("filing refused")

    patch_writers(monkeypatch, Subcommand.CG_FILING, RefusingFilingWriter)

    report = reporting.write_cg_leg(
        cg_result(sale("10", "11")),
        options=cast("Any", _options()),
        failure=None,
        warnings=(),
        output_dir=tmp_path,
        format_id="review-csv",
        financial_year=_FY,
        itr_form="ITR-3",
        debug=True,
        account_labels=ACCOUNT_LABELS,
        writer=outputs.write_cg,
    )

    assert written_before_refusal == ["schedule-cg-debug.csv", "schedule-cg-ltcg-debug.csv"]
    assert (report.status, report.reasons) == (RobinhoodLegStatus.ERROR, ("filing refused",))
    assert list(tmp_path.iterdir()) == []


_FAKE_GUIDANCE = '[[robinhood.securities]]\nisin = "REPLACE"'


def _assert_guidance_refusal(message: str, output_dir: Path) -> None:
    """The refusal names the private guidance file, which holds the TOML (#1044)."""
    path = output_dir / IDENTITY_GUIDANCE_FILE
    assert f"\n{identity_guidance_line(path)}" in message
    assert "not account numbers" in message
    assert "[[robinhood.securities]]" not in message
    assert path.read_text(encoding="utf-8") == _FAKE_GUIDANCE


def test_cg_identity_blocker_writes_private_toml_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = cast(Any, object())
    cg = SimpleNamespace(
        blockers=(
            SimpleNamespace(
                kind=RobinhoodCapitalGainsBlockerKind.IDENTITY,
                source_lot_blockers=(),
                source_identity_blockers=(object(),),
            ),
        ),
        sale_price_checks=(_sale_price_check(1, 8, within_band=False),),
        attributed_lots=(),
    )
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: _options())
    monkeypatch.setattr(pipeline, "_compute_cg", lambda *_args: cg)
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    monkeypatch.setattr(
        core,
        "identity_failure_guidance",
        lambda _bundle: _FAKE_GUIDANCE,
    )

    with pytest.raises(RobinhoodPipelineError) as raised:
        run_robinhood_cg(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path,
            financial_year=_FY,
            itr_form="ITR-2",
        )

    _assert_guidance_refusal(str(raised.value), tmp_path)


def test_fa_identity_blocker_writes_private_toml_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    fa = SimpleNamespace(
        blockers=(SimpleNamespace(kind=RobinhoodFaBlockerKind.A3_IDENTITY),),
    )
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    monkeypatch.setattr(pipeline, "_reconstruct_options", lambda *_args: _options())
    monkeypatch.setattr(pipeline, "_fa_prices", lambda *_args, **_kwargs: _fa_prices())
    _stub_fa_before_prices(monkeypatch)
    monkeypatch.setattr(
        pipeline,
        "_sale_price_checks",
        lambda _paired: (_sale_price_check(1, 8, within_band=False),),
    )
    monkeypatch.setattr(pipeline, "_compute_source_income", lambda *_args: object())
    monkeypatch.setattr(pipeline, "_compute_fa", lambda *_args, **_kwargs: fa)
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    monkeypatch.setattr(
        core,
        "identity_failure_guidance",
        lambda _bundle: _FAKE_GUIDANCE,
    )

    with pytest.raises(RobinhoodPipelineError) as raised:
        run_robinhood_fa(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path,
            financial_year=_FY,
            itr_form="ITR-2",
        )

    _assert_guidance_refusal(str(raised.value), tmp_path)


def _unresolved_receipt_run(monkeypatch: pytest.MonkeyPatch, *, configured: bool) -> Any:
    """Each command loads ``_pair_with_security``'s REC row with no receipt
    block and, unless ``configured``, no ``[[robinhood.securities]]`` entry.
    FY CG and the Schedule FA quantities run for real, up to the REC prompt.
    Returns the loaded bundle."""
    _stub_successful_all(monkeypatch, _complete_fa(cbdt_json_eligible=False))
    paired, accounts = _pair_with_security(include_receipt=True)
    bundle = _bundle()
    securities = (_security_config(),) if configured else ()
    vars(bundle).update(vars(_receipt_bundle(paired, accounts, securities, ())))
    bundle.config.fx = FxConfig()
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    monkeypatch.setattr(pipeline, "_compute_cg", core.compute_cg)
    monkeypatch.setattr(pipeline, "_fa_prices", core.fa_prices)
    return bundle


def _receipt_refusal(run: Callable[..., object], tmp_path: Path) -> str:
    with pytest.raises(RobinhoodReconciliationError) as raised:
        run(**_run_kwargs(tmp_path), fetch_prices=False)
    return str(raised.value)


_RECEIPT_PROMPT_SITES: dict[str, Callable[[Path], str]] = {
    "robinhood-cg": lambda tmp_path: _receipt_refusal(run_robinhood_cg, tmp_path),
    "robinhood-income": lambda tmp_path: _receipt_refusal(run_robinhood_income, tmp_path),
    "robinhood-fa": lambda tmp_path: _receipt_refusal(run_robinhood_fa, tmp_path),
    "robinhood-all equity-cg": lambda tmp_path: _reasons(
        _all_leg(tmp_path, "equity-cg"), RobinhoodLegStatus.ERROR
    ),
}


@pytest.mark.parametrize("configured", [False, True], ids=["unconfigured", "configured"])
@pytest.mark.parametrize("site", list(_RECEIPT_PROMPT_SITES))
def test_receipt_prompt_names_the_identity_guidance_at_each_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    configured: bool,
) -> None:
    """#1522: the REC receipt-block prompt also names the private identity
    guidance file, which holds the unconfigured security's block: a receipt
    block doesn't clear that stop. With every security configured, the
    prompt reads as on 5baa3de and no guidance file is written."""
    bundle = _unresolved_receipt_run(monkeypatch, configured=configured)
    with pytest.raises(RobinhoodReconciliationError) as prompt:
        resolve_robinhood_security_quantities(bundle.paired, bundle.identities, ())

    text = _RECEIPT_PROMPT_SITES[site](tmp_path)

    path = tmp_path / IDENTITY_GUIDANCE_FILE
    if configured:
        assert text == str(prompt.value)
        assert not path.exists()
    else:
        assert text == f"{prompt.value}\n\n{identity_guidance_line(path)}"
        guidance = path.read_text(encoding="utf-8")
        assert guidance.startswith(IDENTITY_GUIDANCE_HEADER)
        assert guidance == identity_failure_guidance(bundle)


def test_fa_surfaces_sale_price_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cg = _cg(checks=(_sale_price_check(1, 8, within_band=False),))
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: _bundle())
    monkeypatch.setattr(pipeline, "_reconstruct_options", lambda *_args: _options())
    monkeypatch.setattr(pipeline, "_fa_prices", lambda *_args, **_kwargs: _fa_prices())
    _stub_fa_before_prices(monkeypatch)
    monkeypatch.setattr(pipeline, "_sale_price_checks", lambda _paired: cg.sale_price_checks)
    monkeypatch.setattr(
        pipeline, "_compute_source_income", lambda *_args: SimpleNamespace(blockers=())
    )
    monkeypatch.setattr(
        pipeline,
        "_compute_fa",
        lambda *_args, **_kwargs: _complete_fa(cbdt_json_eligible=False),
    )
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    monkeypatch.setattr(pipeline, "_write_fa", lambda *_args, **_kwargs: ((), False, ()))

    report = run_robinhood_fa(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-2",
    )

    assert report.warnings == reporting.capital_gains_warnings(cast("Any", cg))


def test_fa_unresolved_a2_gross_credits_are_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An in-CY option blocker still leaves an account's A2 gross credits
    # unresolved; a sale's Amount no longer does, nor blanks A3 (#1383).
    fa = _json_fa(
        manual_work=(
            RobinhoodFaManualWork(
                kind=RobinhoodFaManualKind.A2_GROSS_CREDITS, account_indices=(0,)
            ),
        ),
        a2_changes={"nature_of_amount": None, "gross_amount_inr": None},
        eligible=False,
    )
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: _bundle())
    monkeypatch.setattr(pipeline, "_reconstruct_options", lambda *_args: _options())
    monkeypatch.setattr(pipeline, "_fa_prices", lambda *_args, **_kwargs: _fa_prices())
    _stub_fa_before_prices(monkeypatch)
    monkeypatch.setattr(pipeline, "_sale_price_checks", lambda _paired: ())
    monkeypatch.setattr(
        pipeline, "_compute_source_income", lambda *_args: SimpleNamespace(blockers=())
    )
    monkeypatch.setattr(pipeline, "_compute_fa", lambda *_args, **_kwargs: fa)
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)

    report = run_robinhood_fa(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-2",
        format_id="cbdt-json-fragment",
    )

    assert report.status is RobinhoodLegStatus.MANUAL
    assert report.reasons == ("a2-gross-credits",)
    assert report.outputs == ()
    assert not (tmp_path / "schedule-fa.json").exists()


def test_fa_ignores_a_refusal_in_the_fy_schedules_it_does_not_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#920: Schedule FA reads source income only, so a refusal while assembling
    the FY Schedule OS, FSI, TR and Form 67 doesn't fail it."""
    bundle = _bundle()
    # Everything compute_income reads before it assembles the FY schedules.
    bundle.packets = bundle.robinhood = bundle.securities = bundle.config.fx = None
    _stub_fa_around_income(monkeypatch, bundle)
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    monkeypatch.setattr(
        core,
        "compute_robinhood_income",
        lambda *_args, **_kwargs: RobinhoodIncomeResult(
            **{field.name: () for field in fields(RobinhoodIncomeResult)}
        ),
    )

    def refuse(*_args: object, **_kwargs: object) -> NoReturn:
        raise MissingFxRateError(
            currency="USD", last_available=date(2025, 12, 31), max_carry_forward_days=14
        )

    monkeypatch.setattr(core, "compute_income_schedules", refuse)

    report = run_robinhood_fa(**_run_kwargs(tmp_path))

    assert report.status is RobinhoodLegStatus.OK
    assert (tmp_path / "schedule-fa-a2.csv").is_file()
    assert (tmp_path / "schedule-fa-a3.csv").is_file()


def test_fa_ignores_a_refusal_in_the_fy_capital_gains_it_does_not_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1085: FA computes no FY equity capital gains, so a refusal there doesn't fail it."""
    _stub_fa_around_income(monkeypatch, _bundle())
    monkeypatch.setattr(
        pipeline, "_compute_source_income", lambda *_args: SimpleNamespace(blockers=())
    )
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)

    def refuse(*_args: object) -> NoReturn:
        raise MissingFxRateError(
            currency="USD", last_available=date(2025, 12, 31), max_carry_forward_days=14
        )

    monkeypatch.setattr(pipeline, "_compute_cg", refuse)

    report = run_robinhood_fa(**_run_kwargs(tmp_path))

    assert report.status is RobinhoodLegStatus.OK
    assert (tmp_path / "schedule-fa-a2.csv").is_file()
    assert (tmp_path / "schedule-fa-a3.csv").is_file()


# The month-end a February 10, 2026 event needs a rate for, and that event's date.
_MISSING_RATE_MARKERS = ("2026-01-31", "2026, 1, 31", "2026-02-10", "2026, 2, 10")


def test_a_missing_fy_rate_refuses_robinhood_income_but_not_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#920's example with the real income steps: Schedule FA for CY 2025 and a
    dividend paid in February 2026, with no TTBR in January 2026. Schedule OS
    needs the January 31, 2026 rate; Schedule FA needs none."""
    rates = {
        day: rate for day, rate in load_ttbr("USD").items() if (day.year, day.month) != (2026, 1)
    }
    monkeypatch.setattr("ibkr_fa.fx.policy_rule115.load_ttbr", lambda _currency: rates)
    config = _income_config()
    bundle = _bundle()
    bundle.paired = _income_paired(
        (
            _income_event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1"),
            _income_event(RobinhoodEventKind.CASH_DIVIDEND, 2, date(2026, 2, 10), "100.00"),
        )
    )
    bundle.packets = ()
    bundle.robinhood = config
    bundle.securities = configured_securities(config)
    bundle.config = _shared_config(tmp_path)
    _stub_fa_around_income(monkeypatch, bundle)

    report = run_robinhood_fa(**_run_kwargs(tmp_path))

    assert report.status is RobinhoodLegStatus.OK
    assert (tmp_path / "schedule-fa-a2.csv").is_file()
    assert (tmp_path / "schedule-fa-a3.csv").is_file()
    with pytest.raises(MissingFxRateError) as raised:
        run_robinhood_income(**_run_kwargs(tmp_path))
    # The refusal names the policy and the latest prior public rate. The error
    # keeps neither the requested date nor the dividend's date (#1047, #1368).
    assert str(raised.value) == (
        "MissingFxRateError: no USD rate available [Rule 115(1), Explanation (2)(e) "
        "(preceding-month-end for a dividend)]; latest available prior rate is "
        "2025-12-31, beyond the carry-forward limit of 14 days. The bundled TTBR "
        "archive may need updating; see https://github.com/jay-tau/ibkr-fa for the "
        "latest snapshot."
    )
    assert_prints_no_marker(raised.value, markers=_MISSING_RATE_MARKERS)
    assert raised.value == MissingFxRateError(
        currency="USD",
        last_available=date(2025, 12, 31),
        max_carry_forward_days=14,
        policy_kind="Rule 115(1), Explanation (2)(e) (preceding-month-end for a dividend)",
    )


def test_a_missing_fy_rate_refuses_robinhood_cg_but_not_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1085's example with the real CG compute: Schedule FA for CY 2025 and a
    sale on February 10, 2026, with no TTBR in January 2026. Schedule CG needs
    the January 31, 2026 rate; Schedule FA needs none, and still warns about
    the sale's Amount outside its displayed price band."""
    rates = {
        day: rate for day, rate in load_ttbr("USD").items() if (day.year, day.month) != (2026, 1)
    }
    monkeypatch.setattr("ibkr_fa.fx.policy_rule115.load_ttbr", lambda _currency: rates)
    config = _income_config()
    buy = _income_event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1")
    # 1 share at $1 displays a band of $0.25 to $2.25; $3.00 is outside it.
    sell = replace(
        buy,
        kind=RobinhoodEventKind.EQUITY_SELL,
        source_record_index=2,
        activity_date=date(2026, 2, 10),
        process_date=date(2026, 2, 10),
        settle_date=date(2026, 2, 10),
        trans_code="Sell",
        amount_usd=Decimal("3.00"),
    )
    bundle = _bundle()
    bundle.paired = _income_paired((buy, sell))
    bundle.packets = ()
    bundle.robinhood = config
    bundle.securities = configured_securities(config)
    bundle.config = _shared_config(tmp_path)
    _stub_fa_around_income(monkeypatch, bundle)
    # The stubs above replace both; this test runs the real ones.
    monkeypatch.setattr(pipeline, "_compute_cg", core.compute_cg)
    monkeypatch.setattr(pipeline, "_sale_price_checks", robinhood_sale_price_checks)
    # The lot layer: one slice, the buy fully sold.
    monkeypatch.setattr(core, "resolved_robinhood_quantities", lambda _bundle: object())
    monkeypatch.setattr(
        "ibkr_fa.compute.robinhood_capital_gains.reconcile_robinhood_security_lots",
        lambda _quantities: _lot_result(
            (_slice(1, (2,), quantity="1", cost="1", original_date=date(2025, 1, 2)),)
        ),
    )

    report = run_robinhood_fa(**_run_kwargs(tmp_path))

    assert report.status is RobinhoodLegStatus.OK
    assert (tmp_path / "schedule-fa-a2.csv").is_file()
    assert (tmp_path / "schedule-fa-a3.csv").is_file()
    assert report.warnings == (
        "1 Robinhood equity sale event(s) had exact recorded Amount outside the displayed "
        "quantity x price precision band; exact Amount was retained unchanged and no fee "
        f"was inferred. {EVENT_REFERENCES}0:2.",
    )
    with pytest.raises(MissingFxRateError) as raised:
        run_robinhood_cg(**_run_kwargs(tmp_path))
    # The error keeps neither the requested date nor the sale's date (#1368).
    assert str(raised.value) == (
        "MissingFxRateError: no USD rate available [Rule 115(1), Explanation (2)(f) "
        "(preceding-month-end for a capital-asset transfer)]; latest available prior "
        "rate is 2025-12-31, beyond the carry-forward limit of 14 days. The bundled "
        "TTBR archive may need updating; see https://github.com/jay-tau/ibkr-fa for "
        "the latest snapshot."
    )
    assert_prints_no_marker(raised.value, markers=_MISSING_RATE_MARKERS)
    assert raised.value == MissingFxRateError(
        currency="USD",
        last_available=date(2025, 12, 31),
        max_carry_forward_days=14,
        policy_kind=(
            "Rule 115(1), Explanation (2)(f) (preceding-month-end for a capital-asset transfer)"
        ),
    )


# ---- #1145: robinhood-fa runs the option reconstruction alone ----

# Synthetic markers: none may reach a refusal, its chained exceptions or the CLI output.
_OPTION_DIGIT_RUN = "615243"
_OPTION_MARKERS = (_OPTION_DESCRIPTION, _OPTION_UNDERLYING, _OPTION_DIGIT_RUN)
# Today's messages, written out.
_JANUARY_RATE_REFUSAL = (
    "MissingFxRateError: no USD rate available [Rule 115(1), Explanation (2)(f) "
    "(preceding-month-end for a capital-asset transfer)]; latest available prior rate is "
    "2025-12-31, beyond the carry-forward limit of 14 days. The bundled TTBR archive may "
    "need updating; see https://github.com/jay-tau/ibkr-fa for the latest snapshot."
)
_FY_END_RATE_REFUSAL = (
    "MissingFxRateError: no USD rate available [Rule 115(1), Explanation (2)(c) (last day "
    "of previous year 2026-03-31 for interest received)]; latest available prior rate is "
    "2026-01-09, beyond the carry-forward limit of 14 days. The bundled TTBR archive may "
    "need updating; see https://github.com/jay-tau/ibkr-fa for the latest snapshot."
)
_ROW_3_TOTAL_REFUSAL = (
    "Robinhood option activity row 3, column Amount: an option cost, proceeds or gain total "
    "needs more digits than exact option arithmetic allows"
)
_SPECULATIVE = RobinhoodOptionTreatment.SPECULATIVE_BUSINESS


# Traded after CY 2025: the gain needs the January 31, 2026 rate.
_FEBRUARY_2026_TRADES = (
    _option_trade(_BTO, 2, date(2026, 2, 2), "-615243.00"),
    _option_trade(_STC, 3, date(2026, 2, 20), "1615243.00"),
)


def test_a_missing_fy_rate_refuses_option_capital_gains_but_not_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1145: Schedule FA for CY 2025 and an option bought on February 2, 2026
    and sold on February 20, 2026, treated as capital gains, with no TTBR in
    January 2026. The gain needs the January 31, 2026 rate; Schedule FA, here
    with ITR-2, needs none."""
    _rule115_rates(monkeypatch, keep=lambda day: (day.year, day.month) != (2026, 1))
    bundle = _option_run(
        tmp_path, monkeypatch, RobinhoodOptionTreatment.CAPITAL_GAINS, _FEBRUARY_2026_TRADES
    )

    report = run_robinhood_fa(**_run_kwargs(tmp_path))

    assert report.status is RobinhoodLegStatus.OK
    assert (tmp_path / "schedule-fa-a2.csv").is_file()
    assert (tmp_path / "schedule-fa-a3.csv").is_file()
    fx = TaxFxContext.for_fy(_FY, bundle.config.fx)
    with pytest.raises(MissingFxRateError) as direct:
        convert_robinhood_options(
            core.reconstruct_options(bundle, _FY, "ITR-2"),
            capital_gains_fx_policy=fx.capital_gains,
            business_fx_policy=fx.other_sources,
        )
    assert str(direct.value) == _JANUARY_RATE_REFUSAL
    assert_prints_no_marker(direct.value, markers=_OPTION_MARKERS)
    for run in (run_robinhood_options, run_robinhood_cg):
        with pytest.raises(RobinhoodPipelineError) as raised:
            run(**_run_kwargs(tmp_path))
        assert type(raised.value) is RobinhoodPipelineError
        assert str(raised.value) == f"{FNO_FEMA_WARNING} {_JANUARY_RATE_REFUSAL}"
        assert_replaced_unchained(raised.value, markers=_OPTION_MARKERS)
    assert _main("robinhood-options", tmp_path) == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {FNO_FEMA_WARNING} {_JANUARY_RATE_REFUSAL}\n"
    assert list((tmp_path / "out").iterdir()) == []
    assert_no_markers(captured.out + captured.err, *_OPTION_MARKERS)


def test_a_missing_fy_end_rate_refuses_option_business_income_but_not_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1145: a speculative-business option bought and sold in May and June
    2025, with no TTBR after January 10, 2026. The business income needs the
    March 31, 2026 rate; Schedule FA for CY 2025 needs none."""
    _rule115_rates(monkeypatch, keep=lambda day: day <= date(2026, 1, 10))
    _option_run(tmp_path, monkeypatch, _SPECULATIVE, _MAY_JUNE_2025_TRADES)
    kwargs = _run_kwargs(tmp_path, "ITR-3")

    report = run_robinhood_fa(**kwargs)

    assert report.status is RobinhoodLegStatus.OK
    assert (tmp_path / "schedule-fa-a2.csv").is_file()
    assert (tmp_path / "schedule-fa-a3.csv").is_file()
    with pytest.raises(RobinhoodPipelineError) as raised:
        run_robinhood_income(**kwargs)
    assert type(raised.value) is RobinhoodPipelineError
    assert str(raised.value) == f"{FNO_FEMA_WARNING} {_FY_END_RATE_REFUSAL}"
    assert_replaced_unchained(raised.value, markers=_OPTION_MARKERS)
    assert _main("robinhood-income", tmp_path, "--itr-form", "ITR-3") == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {FNO_FEMA_WARNING} {_FY_END_RATE_REFUSAL}\n"
    assert list((tmp_path / "out").iterdir()) == []
    assert_no_markers(captured.out + captured.err, *_OPTION_MARKERS)


@pytest.mark.parametrize("fy_end_rate", [True, False], ids=["fy-end-rate", "no-fy-end-rate"])
@pytest.mark.parametrize(
    "treatment",
    [_SPECULATIVE, RobinhoodOptionTreatment.NON_SPECULATIVE_BUSINESS],
    ids=lambda treatment: treatment.value,
)
def test_robinhood_fa_still_refuses_itr2_for_business_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    treatment: RobinhoodOptionTreatment,
    fy_end_rate: bool,
) -> None:
    """#1145: the reconstruction reads the required form from the reviewed
    classification, so robinhood-fa refuses ITR-2 whether or not the FY-end
    rate exists. Without it, main refused first with the wrapped FX error."""
    if not fy_end_rate:
        _rule115_rates(monkeypatch, keep=lambda day: day <= date(2026, 1, 10))
    _option_run(tmp_path, monkeypatch, treatment, _MAY_JUNE_2025_TRADES)

    with pytest.raises(RobinhoodItrFormError) as raised:
        run_robinhood_fa(**_run_kwargs(tmp_path))

    assert str(raised.value) == (
        f"{FNO_FEMA_WARNING} reviewed Robinhood option treatment requires --itr-form ITR-3"
    )
    report = run_robinhood_fa(**_run_kwargs(tmp_path, "ITR-3"))
    assert report.status is RobinhoodLegStatus.OK
    # The same trades treated as capital gains need no ITR-3.
    _option_run(
        tmp_path, monkeypatch, RobinhoodOptionTreatment.CAPITAL_GAINS, _MAY_JUNE_2025_TRADES
    )
    assert run_robinhood_fa(**_run_kwargs(tmp_path)).status is RobinhoodLegStatus.OK


# 1002 digits before the point, past exact_fraction's digit cap (#1245). The
# leading 9 keeps the digit run whole in the Decimal's text.
_OPTION_PAST_THE_CAP = "9" + _OPTION_DIGIT_RUN + "E+995"


def test_an_option_share_past_the_digit_cap_still_refuses_robinhood_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Option shares stay in the reconstruction (#1145): a business option
    bought for 1002 digits of cash has no exact share (#1245), so robinhood-fa
    refuses, and robinhood-options exits 4 printing only the fixed refusal."""
    trades = (
        _option_trade(_BTO, 2, date(2025, 5, 1), f"-{_OPTION_PAST_THE_CAP}"),
        _option_trade(_STC, 3, date(2025, 5, 2), "0.01"),
    )
    assert _OPTION_DIGIT_RUN in str(trades[0].amount_usd)
    _option_run(tmp_path, monkeypatch, _SPECULATIVE, trades)

    with pytest.raises(RobinhoodPipelineError) as raised:
        run_robinhood_fa(**_run_kwargs(tmp_path, "ITR-3"))

    assert str(raised.value) == f"{FNO_FEMA_WARNING} {_ROW_3_TOTAL_REFUSAL}"
    assert_replaced_unchained(raised.value, markers=_OPTION_MARKERS)
    assert _main("robinhood-options", tmp_path) == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {FNO_FEMA_WARNING} {_ROW_3_TOTAL_REFUSAL}\n"
    assert list((tmp_path / "out").iterdir()) == []
    assert_no_markers(captured.out + captured.err, *_OPTION_MARKERS)


def test_an_option_rate_past_the_digit_cap_refuses_robinhood_options_but_not_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1145: a January 31, 2026 rate with 1001 decimal places, past the digit
    cap, has no exact rational to convert the option's capital gain at
    (#1245). Only the commands that file it refuse."""
    rate = Decimal("80." + (_OPTION_DIGIT_RUN * 167)[:1001])
    assert _OPTION_DIGIT_RUN in str(rate)
    _rule115_rates(monkeypatch, overrides={date(2026, 1, 31): rate})
    trades = (
        _option_trade(_BTO, 2, date(2026, 2, 2), "-615243.17"),
        _option_trade(_STC, 3, date(2026, 2, 20), "1615243.29"),
    )
    _option_run(tmp_path, monkeypatch, RobinhoodOptionTreatment.CAPITAL_GAINS, trades)

    report = run_robinhood_fa(**_run_kwargs(tmp_path))

    assert report.status is RobinhoodLegStatus.OK
    with pytest.raises(RobinhoodPipelineError) as raised:
        run_robinhood_options(**_run_kwargs(tmp_path))
    assert str(raised.value) == f"{FNO_FEMA_WARNING} {_ROW_3_TOTAL_REFUSAL}"
    assert_replaced_unchained(raised.value, markers=_OPTION_MARKERS)


_OPTION_TABLE_F_REASON = (
    "Schedule CG Table F row {row} needs its option date bands entered by hand; "
    "robinhood-options.csv leaves them blank"
)


@pytest.mark.parametrize(("itr_form", "bfla_item"), [("ITR-2", "3v"), ("ITR-3", "5viii")])
def test_robinhood_options_leaves_a_loss_across_bands_blank_without_echo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    itr_form: str,
    bfla_item: str,
) -> None:
    """#1362: one-contract capital-gains round trips closed in May and July
    2025 for gains, one sold at a marked price of $615,243.00, and one closed
    in October for a loss. A loss band beside two gain bands leaves Table F
    row 3 blank and named, and the already-manual leg (exit 3) gives the row
    as its reason. No marker reaches stdout or stderr."""
    trades = (
        _option_trade(_BTO, 2, date(2025, 5, 1), "-100.00"),
        _option_trade(_STC, 3, date(2025, 5, 20), "61524300.00"),
        _option_trade(_BTO, 4, date(2025, 7, 1), "-100.00"),
        _option_trade(_STC, 5, date(2025, 7, 20), "300.00"),
        _option_trade(_BTO, 6, date(2025, 10, 1), "-300.00"),
        _option_trade(_STC, 7, date(2025, 10, 20), "100.00"),
    )
    assert _OPTION_DIGIT_RUN in str(trades[1].price_usd)
    _option_run(tmp_path, monkeypatch, RobinhoodOptionTreatment.CAPITAL_GAINS, trades)

    assert _main("robinhood-options", tmp_path, "--itr-form", itr_form) == 3

    captured = capsys.readouterr()
    with (tmp_path / "out" / "robinhood-options.csv").open(encoding="utf-8-sig") as handle:
        rows = [row for row in csv.DictReader(handle) if row["record_type"] == "table-f"]
    assert [(row["treatment_or_kind"], row["amount_inr"]) for row in rows] == [
        ("stcg-other-assets", "")
    ] * 5
    for row in rows:
        assert f"Enter value from item {bfla_item} of schedule BFLA" in row["manual_target"]
        assert (
            "Blank: a date band nets a loss and two or more bands net gains."
            in row["manual_target"]
        )
    assert _OPTION_TABLE_F_REASON.format(row=3) in captured.out
    assert_no_markers(captured.out + captured.err, *_OPTION_MARKERS)


def test_robinhood_income_files_the_exact_option_business_income(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails on 02ff79d, which filed ₹280: 3 calls bought for $10.00 in March
    2025, 1 sold that March and 2 sold in May for $10.00. The FY 2025-26
    profit is exactly ($10 - $20/3) x 84.15 = ₹280.50, so Schedule FSI files
    ₹281 of business income (#1245). Source income is empty."""
    _rule115_rates(monkeypatch, overrides={date(2026, 3, 31): Decimal("84.15")})
    trades = (
        _option(_BTO, 2, date(2025, 3, 10), date(2025, 3, 11), quantity="3", amount="-10.00"),
        _option(_STC, 3, date(2025, 3, 20), date(2025, 3, 21), quantity="1", amount="4.00"),
        _option(_STC, 4, date(2025, 5, 15), date(2025, 5, 16), quantity="2", amount="10.00"),
    )
    trades = tuple(
        replace(trade, price_usd=trade.amount_usd.copy_abs() / trade.quantity / 100)
        for trade in trades
        if trade.amount_usd is not None and trade.quantity is not None
    )
    bundle = _option_run(tmp_path, monkeypatch, _SPECULATIVE, trades)
    bundle.paired = SimpleNamespace(
        accounts=bundle.paired.accounts, account_labels={0: "Individual", 1: "Managed"}
    )
    empty = RobinhoodIncomeResult(**{field.name: () for field in fields(RobinhoodIncomeResult)})
    monkeypatch.setattr(core, "compute_source_income", lambda *_args: empty)

    assert _main("robinhood-income", tmp_path, "--itr-form", "ITR-3") == 0

    rows = (tmp_path / "out" / "schedule-fsi.csv").read_text(encoding="utf-8-sig").splitlines()
    assert "Head of Income,Business or Profession" in rows
    assert "Income from outside India (included in PART B-TI),281" in rows


class _EuroFxStub(AuditedFxStub):
    currency: ClassVar[str] = "EUR"


@pytest.mark.parametrize("policy", ["capital_gains", "other_sources"])
def test_a_non_usd_option_fx_policy_refuses_the_inr_step_but_not_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
) -> None:
    """#1145 moves the USD-policy check into the INR step, so it no longer
    stops robinhood-fa."""
    bundle = _option_run(
        tmp_path, monkeypatch, RobinhoodOptionTreatment.CAPITAL_GAINS, _MAY_JUNE_2025_TRADES
    )
    policies = {"capital_gains": AuditedFxStub(), "other_sources": AuditedFxStub()}
    policies[policy] = _EuroFxStub()
    monkeypatch.setattr(
        TaxFxContext, "for_fy", classmethod(lambda _cls, *_args: SimpleNamespace(**policies))
    )

    report = run_robinhood_fa(**_run_kwargs(tmp_path))

    assert report.status is RobinhoodLegStatus.OK
    with pytest.raises(RobinhoodOptionsError) as direct:
        convert_robinhood_options(
            core.reconstruct_options(bundle, _FY, "ITR-2"),
            capital_gains_fx_policy=policies["capital_gains"],
            business_fx_policy=policies["other_sources"],
        )
    assert str(direct.value) == "Robinhood option FX policies must convert USD"
    with pytest.raises(RobinhoodPipelineError) as raised:
        run_robinhood_options(**_run_kwargs(tmp_path))
    assert str(raised.value) == f"{FNO_FEMA_WARNING} Robinhood option FX policies must convert USD"


class _FyPolicyUsedError(Exception):
    """An FY FX policy was used."""


class _UnusableFyPolicy:
    """An FY FX policy that fails on any attribute access or call."""

    def __getattribute__(self, name: str) -> NoReturn:
        raise _FyPolicyUsedError(name)

    def __call__(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise _FyPolicyUsedError


def test_robinhood_fa_converts_option_sales_with_its_own_policy_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1145: every FY policy fails on any use, yet robinhood-fa completes: its
    option step and sale price checks use none (source income takes no policy,
    #920), and Schedule FA's own calendar-year policy converts the June 2025
    option sale's credit at the December 31, 2025 rate."""
    _option_run(
        tmp_path, monkeypatch, RobinhoodOptionTreatment.CAPITAL_GAINS, _MAY_JUNE_2025_TRADES
    )
    monkeypatch.setattr(pipeline, "_sale_price_checks", robinhood_sale_price_checks)
    monkeypatch.setattr(pipeline, "_compute_fa", core.compute_fa)
    monkeypatch.setattr(
        TaxFxContext,
        "for_fy",
        classmethod(
            lambda _cls, *_args, **_kwargs: SimpleNamespace(
                capital_gains=_UnusableFyPolicy(), other_sources=_UnusableFyPolicy()
            )
        ),
    )
    converted: list[Decimal] = []

    def compute_robinhood_fa(
        paired: Any,
        *,
        income: Any,
        options: Any,
        calendar_year: int,
        fx_policy: Any,
        **_kwargs: object,
    ) -> SimpleNamespace:
        gross = _a2_gross_evidence(paired, income, options, 0, calendar_year, fx_policy)
        converted.append(gross.known_components[FaA2NatureOfAmount.PROCEEDS])
        return _complete_fa(cbdt_json_eligible=False)

    monkeypatch.setattr(core, "compute_robinhood_fa", compute_robinhood_fa)

    report = run_robinhood_fa(**_run_kwargs(tmp_path))

    assert report.status is RobinhoodLegStatus.OK
    assert converted == [Decimal("1615243.00") * load_ttbr("USD")[date(2025, 12, 31)]]
    with pytest.raises(RobinhoodPipelineError) as raised:
        run_robinhood_options(**_run_kwargs(tmp_path))
    assert_replaced_unchained(raised.value, _FyPolicyUsedError)


def test_income_summary_no_activity_supports_refuses_without_echo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1081 with the real source-income compute: the second account has only
    an ACH row, yet its 2025 statement prints a Capital Gains Distribution.
    Schedule OS refuses with fixed text naming neither amount nor account
    number, then the blocker's line naming the account's type (#1487)."""
    other_account = "999900001"
    config = _income_config(other_account=other_account)
    # The refusal names the account by its label.
    paired = _income_paired(
        _income_base_events(),
        other_account=other_account,
        statement_deltas={
            (other_account, date(2025, 1, 1), "Capital Gains Distributions"): (
                Decimal("4321.09"),
                Decimal("4321.09"),
            )
        },
    )
    _stub_income_bundle(monkeypatch, tmp_path, config, paired, (_income_packet(),))
    monkeypatch.setattr(pipeline, "_compute_os", core.compute_os)

    assert _main("robinhood-income", tmp_path) == 4

    captured = capsys.readouterr()
    assert captured.err == (
        f"error: {OS_UNRESOLVED}\n"
        "unsupported-income (Managed Individual account, 2025): a statement's Income summary "
        "shows Capital Gains Distributions, which ibkr-fa doesn't compute; complete Schedule "
        "OS, Schedule FSI, Schedule TR and Form 67 by hand\n"
    )
    assert_no_markers(captured.out + captured.err, "4,321.09", "4321.09", other_account)


def test_form_for_income_free_account_year_refuses_ftc_without_echo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1087 with the real source-income compute: the second account has only
    an ACH row, yet a 2025 form for it reports dividends. The form is reconciled
    rather than set aside, so the FTC leg refuses with fixed text naming neither
    amount nor account number, then the blocker's line naming the account's
    type (#1487)."""
    other_account = "999900001"
    config = _income_config(other_account=other_account)
    _stub_income_bundle(
        monkeypatch,
        tmp_path,
        config,
        _income_paired(_income_base_events(), other_account=other_account),
        (
            _income_packet(),
            _income_packet(account_reference=other_account, gross="4321.00", tax="3.00"),
        ),
    )

    report = run_robinhood_income(**_run_kwargs(tmp_path))

    assert report.status is RobinhoodLegStatus.ERROR
    assert report.reasons == (
        FTC_OS_REMOVED,
        "annual-form-reconciliation (Managed Individual account, 2025): a Form 1042-S doesn't "
        "reconcile to the dividends, interest and Stock Lending income in the activity report "
        "for its year, so ibkr-fa can't tell which is right; complete Schedule FSI, Schedule "
        "TR and Form 67 by hand",
    )
    assert _main("robinhood-income", tmp_path) == 4
    captured = capsys.readouterr()
    assert "Robinhood income-ftc: error" in captured.out
    assert_no_markers(captured.out + captured.err, "4,321.00", "4321.00", other_account)


def test_option_capital_lines_downgrade_income_to_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cg = _cg(checks=(_sale_price_check(1, 8, within_band=False),))
    monkeypatch.setattr(
        pipeline,
        "_load",
        lambda **_kwargs: _bundle(warnings=("Crypto excluded",)),
    )
    monkeypatch.setattr(
        pipeline,
        "_compute_options",
        lambda *_args: _options(capital=True, has_trades=True),
    )
    monkeypatch.setattr(
        pipeline,
        "_compute_cg",
        lambda *_args: cg,
    )
    monkeypatch.setattr(
        pipeline,
        "_compute_income",
        lambda *_args, **_kwargs: (
            SimpleNamespace(blockers=()),
            form67_assembly(),
            object(),
        ),
    )
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    monkeypatch.setattr(
        pipeline,
        "_write_income",
        lambda *_args, **_kwargs: ((tmp_path / "schedule-fsi.csv",), Form67Readiness.READY),
    )
    report = run_robinhood_income(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-2",
    )
    assert report.status is RobinhoodLegStatus.MANUAL
    assert "Schedule FSI" in report.reasons[0]
    assert report.warnings == (
        ROBINHOOD_FNO_FEMA_WARNING,
        *reporting.capital_gains_warnings(cast("Any", cg)),
        "Crypto excluded",
    )


# FTC-only income blockers: each leaves Schedule OS and FA complete.
_ISSUER_COUNTRY = RobinhoodIncomeBlocker(
    kind=RobinhoodIncomeBlockerKind.JURISDICTION,
    causes=(RobinhoodIncomeBlockerCause.ISSUER_COUNTRY,),
)
_FTC_ONLY_BLOCKERS = {
    "jurisdiction": (
        _ISSUER_COUNTRY,
        (
            "jurisdiction: a dividend's security has an issuer country that differs from its "
            "ISIN's country, so the dividend's source country is unproven; complete Schedule "
            "FSI, Schedule TR and Form 67 by hand",
        ),
    ),
    "interest-payer": (
        RobinhoodIncomeBlocker(
            kind=RobinhoodIncomeBlockerKind.INTEREST_PAYER_COUNTRY, account_indices=(0,)
        ),
        (INTEREST_PAYER_RECIPE,),
    ),
}


@pytest.mark.parametrize("layer", ["cli", "entry"])
@pytest.mark.parametrize("blocked", _FTC_ONLY_BLOCKERS)
def test_standalone_income_removes_schedule_os_when_ftc_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    layer: str,
    blocked: str,
) -> None:
    """Schedule OS is complete, but an exit-4 run leaves no output, as
    ``robinhood-all`` does (#719, #745). A payer-country blocker names
    itself (#698)."""
    os_path = tmp_path / "out" / "schedule-os.csv"
    written: list[Path] = []
    blocker, guidance = _FTC_ONLY_BLOCKERS[blocked]
    income = SimpleNamespace(blockers=(blocker,), partition_only_payer_accounts=())
    cg = _cg(checks=(_sale_price_check(1, 8, within_band=False),))
    monkeypatch.setattr(
        pipeline,
        "_load",
        lambda **_kwargs: _bundle(),
    )
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: _options())
    monkeypatch.setattr(
        pipeline,
        "_compute_cg",
        lambda *_args: cg,
    )
    monkeypatch.setattr(
        pipeline,
        "_compute_income",
        lambda *_args, **_kwargs: (income, None, None),
    )
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    monkeypatch.setattr(
        pipeline,
        "_annual_form_warnings",
        lambda _income: ("annual-form discrepancy",),
    )

    def write_os(*_args: object, **_kwargs: object) -> tuple[Path, ...]:
        os_path.write_text("complete", encoding="utf-8")
        written.append(os_path)
        return (os_path,)

    monkeypatch.setattr(pipeline, "_write_os", write_os)

    if layer == "cli":
        assert _main("robinhood-income", tmp_path) == 4
        output = capsys.readouterr().out
        assert "Robinhood income-ftc: error" in output
        assert "Wrote:" not in output
        assert "Schedule OS is complete but was removed" in output
        assert all(line in output for line in guidance)
        assert ("interest_payer_countries" in output) is (blocked == "interest-payer")
    else:
        report = run_robinhood_income(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path / "out",
            financial_year=_FY,
            itr_form="ITR-2",
        )
        assert report.status is RobinhoodLegStatus.ERROR
        assert report.outputs == ()
        assert report.run_outputs == pipeline.NO_RUN_OUTPUTS
        assert "Schedule OS is complete but was removed" in report.reasons[0]
        assert report.reasons[1:] == guidance
        assert report.warnings == (
            "annual-form discrepancy",
            *reporting.capital_gains_warnings(cast("Any", cg)),
        )

    assert written == [os_path]
    assert list((tmp_path / "out").iterdir()) == []


def test_standalone_option_error_retains_fema_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: object())
    monkeypatch.setattr(
        pipeline,
        "_compute_options",
        lambda *_args: _options(has_trades=True),
    )
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    monkeypatch.setattr(
        pipeline,
        "_hard_option_blockers",
        lambda _options: (_LOCATED[RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT][0],),
    )
    with pytest.raises(RobinhoodPipelineError, match="FEMA/LRS F&O warning"):
        run_robinhood_options(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path,
            financial_year=_FY,
            itr_form="ITR-2",
        )


def test_standalone_options_refuse_a_pre_cutover_capital_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kind is hard, so the leg refuses before the writer looks up portal
    labels, which AY 2025-26 doesn't have."""
    options = RobinhoodOptionsResult(
        financial_year_end=date(2025, 3, 31),
        trades=(),
        lot_slices=(),
        open_lots=(),
        valuation_anchors=(),
        classifications=(),
        capital_gain_slices=(),
        business_totals=(),
        capital_gain_lines=(),
        business_summaries=(),
        blockers=(_LOCATED[RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE][0],),
        excluded_out_of_fy_close_references=(),
        a2_gross_credit_references=(),
    )

    def write(*_args: object) -> NoReturn:
        raise AssertionError("the option workpaper must not be written")

    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: object())
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: options)
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    monkeypatch.setattr(pipeline, "_write_options", write)
    with pytest.raises(RobinhoodPipelineError, match="closed before 2024-07-23") as raised:
        run_robinhood_options(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path,
            financial_year=FinancialYear(2024),
            itr_form="ITR-2",
        )
    assert "evidence" not in str(raised.value)


_HARD_OPTION_BLOCKER_KINDS = tuple(
    kind
    for kind in RobinhoodOptionBlockerKind
    if kind is not RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION
)


def _stub_blocked_run(
    monkeypatch: pytest.MonkeyPatch,
    options: SimpleNamespace,
    *,
    fa: Any = None,
) -> list[object]:
    """Stub every leg to succeed except for ``options``; returns the ``_compute_fa`` calls."""
    _stub_successful_all(monkeypatch, _complete_fa(cbdt_json_eligible=False))
    calls: list[object] = []

    def compute_fa(*args: object, **kwargs: object) -> object:
        calls.append(args)
        return fa(*args, **kwargs) if fa is not None else _complete_fa(cbdt_json_eligible=False)

    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: _bundle(has_options=True))
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: options)
    monkeypatch.setattr(pipeline, "_reconstruct_options", lambda *_args: options)
    monkeypatch.setattr(pipeline, "_compute_fa", compute_fa)
    monkeypatch.setattr(pipeline, "_write_fa", lambda *_args, **_kwargs: ((), False, ()))
    return calls


def _refusal(run: Callable[..., object], tmp_path: Path, **kwargs: object) -> str:
    with pytest.raises(RobinhoodPipelineError) as raised:
        run(**_run_kwargs(tmp_path), **kwargs)
    return str(raised.value)


def _reasons(report: Any, status: RobinhoodLegStatus) -> str:
    assert report.status is status
    return " | ".join(report.reasons)


def _all_leg(tmp_path: Path, name: str) -> Any:
    report = run_robinhood_all(**_run_kwargs(tmp_path), fetch_prices=False)
    return next(item for item in report.legs if item.name == name)


_MANUAL, _ERROR = RobinhoodLegStatus.MANUAL, RobinhoodLegStatus.ERROR
_OPTION_BLOCKER_SITES: dict[str, Callable[[Path], str]] = {
    "robinhood-options": lambda tmp_path: _refusal(run_robinhood_options, tmp_path),
    "robinhood-fa": lambda tmp_path: _refusal(run_robinhood_fa, tmp_path, fetch_prices=False),
    "robinhood-cg": lambda tmp_path: _reasons(
        run_robinhood_cg(**_run_kwargs(tmp_path), fetch_prices=False), _MANUAL
    ),
    "robinhood-income": lambda tmp_path: _reasons(
        run_robinhood_income(**_run_kwargs(tmp_path), fetch_prices=False), _MANUAL
    ),
    "robinhood-all options": lambda tmp_path: _reasons(_all_leg(tmp_path, "options"), _ERROR),
    "robinhood-all schedule-fa": lambda tmp_path: _reasons(
        _all_leg(tmp_path, "schedule-fa"), _ERROR
    ),
    "robinhood-all income-ftc": lambda tmp_path: _reasons(
        _all_leg(tmp_path, "income-ftc"), _MANUAL
    ),
}


@pytest.mark.parametrize("site", list(_OPTION_BLOCKER_SITES))
@pytest.mark.parametrize("kind", _HARD_OPTION_BLOCKER_KINDS)
def test_each_hard_option_blocker_kind_is_named_at_each_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: RobinhoodOptionBlockerKind,
    site: str,
) -> None:
    """#802: a blocked slice can have complete evidence, such as a pre-cutover
    one; the refusal names its kind, never missing evidence."""
    _stub_blocked_run(monkeypatch, _blocked_options(kind))

    text = _OPTION_BLOCKER_SITES[site](tmp_path)

    assert _LOCATED[kind][1] in text
    assert "evidence" not in text


def test_robinhood_options_refusal_locates_a_fractional_contract_quantity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1134: a synthetic trade of 2.5 contracts at activity report row 7. The
    refusal names that row and column, and nothing the trade holds."""
    purchase = _synth_trade(
        RobinhoodEventKind.OPTION_BUY_TO_OPEN, 7, date(2025, 5, 1), quantity="2.5"
    )
    paired = _option_paired((purchase,), statements=())
    options = _robinhood_options(paired, RobinhoodOptionTreatment.CAPITAL_GAINS)
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: SimpleNamespace(paired=paired))
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: options)
    summary = (
        f"{OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT]} (1 blocker; "
        "first at account 1, activity report row 7; column Quantity)"
    )

    assert _main("robinhood-options", tmp_path) == 4

    captured = capsys.readouterr()
    assert option_blocker_summary(options.blockers) == summary
    assert captured.err == (
        f"error: {FNO_FEMA_WARNING} Robinhood options contain unresolved blockers: {summary}\n"
    )
    assert not any(marker in captured.out + captured.err for marker in _SYNTH_MARKERS)


def test_hard_option_blockers_exclude_only_open_business_positions() -> None:
    options = _blocked_options(*RobinhoodOptionBlockerKind)

    hard = core.hard_option_blockers(cast("RobinhoodOptionsResult", options))

    assert tuple(item.kind for item in hard) == _HARD_OPTION_BLOCKER_KINDS


def test_standalone_fa_refuses_a_pre_cutover_blocker_before_computing_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_blocked_run(
        monkeypatch, _blocked_options(RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE)
    )

    text = _refusal(run_robinhood_fa, tmp_path, fetch_prices=False)

    assert OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE] in text
    assert calls == []


def _a2_guard(
    _bundle: object, financial_year: FinancialYear, _income: object, options: Any, **_kwargs: object
) -> NoReturn:
    """Schedule FA up to the A2 peak's option guard, with the pipeline's options."""
    compute_robinhood_a2_peaks(
        cast("Any", SimpleNamespace(accounts=())),
        config=cast("Any", None),
        securities=cast("Any", None),
        quantities=cast("Any", SimpleNamespace(blockers=())),
        options=options,
        equity_prices={},
        option_prices={},
        calendar_year=financial_year.start_year,
        fx_policy=cast("Any", None),
    )
    raise AssertionError("the A2 peak must refuse an option blocker")


# The open position's stop, as robinhood-fa and robinhood-all's FA leg print it.
_OPEN_BUSINESS_STOP = (
    "option blockers prevent complete Schedule FA: "
    f"{_LOCATED[RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION][1]}"
)


@pytest.mark.parametrize("site", ["robinhood-fa", "robinhood-all schedule-fa"])
def test_open_business_position_alone_refuses_before_pricing_with_its_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
) -> None:
    """The A2 peak refuses any option blocker, so Schedule FA stops on an open
    business position before any price is fetched (#1567). On 789c17f it was
    priced and computed first, and refused at the A2 peak's guard as
    "Robinhood option blockers prevent the A2 peak: ..."."""
    calls = _stub_blocked_run(
        monkeypatch,
        _blocked_options(RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION),
        fa=_a2_guard,
    )
    priced: list[object] = []

    def prices(*args: object, **_kwargs: object) -> tuple[Any, ...]:
        priced.append(args)
        return _fa_prices()

    monkeypatch.setattr(pipeline, "_fa_prices", prices)

    text = _OPTION_BLOCKER_SITES[site](tmp_path)

    assert text.endswith(_OPEN_BUSINESS_STOP)
    assert (calls, priced) == ([], [])


@pytest.mark.parametrize("command", ["robinhood-fa", "robinhood-all"])
def test_open_business_position_alone_exits_4_before_pricing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """#1411: an open business position refuses Schedule FA, so robinhood-fa
    and robinhood-all each exit 4, now with the stop named before pricing
    (#1567)."""
    _stub_blocked_run(
        monkeypatch,
        _blocked_options(RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION),
        fa=_a2_guard,
    )

    assert _main(command, tmp_path, "--no-fetch-prices") == 4

    captured = capsys.readouterr()
    assert _OPEN_BUSINESS_STOP in captured.out + captured.err


def test_open_business_position_alone_does_not_refuse_robinhood_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leg stays manual, and its reason names the open position (#1554)."""
    _stub_blocked_run(
        monkeypatch, _blocked_options(RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION)
    )
    monkeypatch.setattr(
        pipeline, "_write_options", lambda *_args, **_kwargs: tmp_path / "options.csv"
    )

    report = run_robinhood_options(**_run_kwargs(tmp_path))

    assert report.status is RobinhoodLegStatus.MANUAL
    assert report.reasons == (
        "manual option schedules are required; option blockers: "
        f"{_LOCATED[RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION][1]}",
    )


@pytest.mark.parametrize(
    "case",
    [ScheduleCgTableFCase.LOSS_ACROSS_BANDS, ScheduleCgTableFCase.MANUAL_SECTION],
)
def test_a_blank_option_table_f_bucket_adds_one_reason_to_each_options_leg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: ScheduleCgTableFCase,
) -> None:
    """#1362: robinhood-options and robinhood-all's options leg each add one
    reason naming the blank bucket's Table F row, row 5 here, and none for
    the computed one. The leg stays manual."""
    options = _options(capital=True, has_trades=True)
    options.table_f = {
        RobinhoodOptionCapitalGainBucket.STCG_OTHER_ASSETS: ScheduleCgTableFRow(
            ScheduleCgTableFCase.COMPUTED, (Decimal(1),) * 5
        ),
        RobinhoodOptionCapitalGainBucket.LTCG_OTHER_ASSETS: ScheduleCgTableFRow(case, None),
    }
    _stub_blocked_run(monkeypatch, options)
    monkeypatch.setattr(
        pipeline, "_write_options", lambda *_args, **_kwargs: tmp_path / "options.csv"
    )
    expected = ("manual option schedules are required", _OPTION_TABLE_F_REASON.format(row=5))

    report = run_robinhood_options(**_run_kwargs(tmp_path))
    leg = _all_leg(tmp_path, "options")

    assert (report.status, report.reasons) == (RobinhoodLegStatus.MANUAL, expected)
    assert (leg.status, leg.reasons) == (RobinhoodLegStatus.MANUAL, expected)


def test_all_schedule_fa_keeps_its_own_error_beside_the_option_blockers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error before Schedule FA's prices, here the inventory gate's, prints
    beside the option blockers. Schedule FA is neither priced nor computed
    while they stand (#1560), so its own blockers wait for a later run."""
    fa = _complete_fa(
        blockers=(RobinhoodFaBlocker(kind=RobinhoodFaBlockerKind.A2_CLOSING, account_indices=(0,)),)
    )
    calls = _stub_blocked_run(
        monkeypatch,
        _blocked_options(RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT),
        fa=lambda *_args, **_kwargs: fa,
    )

    def refuse(*_args: object, **_kwargs: object) -> NoReturn:
        raise RobinhoodPipelineError("Robinhood equity inventory evidence is incomplete")

    monkeypatch.setattr(pipeline, "_fa_inventory", refuse)

    text = _OPTION_BLOCKER_SITES["robinhood-all schedule-fa"](tmp_path)

    assert "Robinhood equity inventory evidence is incomplete" in text
    assert _LOCATED[RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT][1] in text
    assert FA_UNRESOLVED not in text
    assert calls == []


@pytest.mark.parametrize("site", ["robinhood-income", "robinhood-all income-ftc"])
def test_income_keeps_the_fsi_instruction_and_names_the_option_blockers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
) -> None:
    _stub_blocked_run(
        monkeypatch,
        _blocked_options(
            RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE,
            RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION,
            RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE,
        ),
    )

    text = _OPTION_BLOCKER_SITES[site](tmp_path)

    # The open business position is named too, in kind order (#1554).
    assert text == (
        "remaining option items require manual Schedule FSI completion; option blockers: "
        f"{_LOCATED[RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION][1]}; "
        f"{OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE]} (2 blockers; "
        "first at account 1, activity report row 7; columns Description and Quantity)"
    )


def test_all_keeps_valid_legs_and_preserves_foreign_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign = tmp_path / "schedule-fa.json"
    original_json = '{"ScheduleFA": {"manual": true}}'
    foreign.write_text(original_json, encoding="utf-8")
    fa = _complete_fa(program_bank=(object(),))
    cg = _cg(checks=(_sale_price_check(1, 8, within_band=False),))
    _stub_successful_all(
        monkeypatch,
        fa,
        cg=cg,
        price_warnings=("option price audit",),
    )
    report = run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-2",
        fetch_prices=False,
    )
    assert report.status is RobinhoodLegStatus.OK
    assert foreign.read_text(encoding="utf-8") == original_json
    assert (tmp_path / "schedule-fa-a2.csv").is_file()
    assert (tmp_path / "schedule-fa-a3.csv").is_file()
    statuses = {item.name: item.status for item in report.legs}
    assert statuses["equity-cg"] is RobinhoodLegStatus.OK
    assert statuses["schedule-os"] is RobinhoodLegStatus.OK
    assert statuses["income-ftc"] is RobinhoodLegStatus.OK
    assert statuses["product-scope"] is RobinhoodLegStatus.NOT_REQUIRED
    equity_leg = next(item for item in report.legs if item.name == "equity-cg")
    assert equity_leg.warnings == reporting.capital_gains_warnings(cast("Any", cg))
    fa_leg = next(item for item in report.legs if item.name == "schedule-fa")
    assert fa_leg.status is RobinhoodLegStatus.OK
    assert fa_leg.reasons == ()
    assert fa_leg.warnings == (
        "option price audit",
        "automatic Schedule FA JSON was not written because the output directory "
        "contains a file not created by ibkr-fa",
    )
    a1_leg = next(item for item in report.legs if item.name == "fa-a1")
    assert a1_leg.status is RobinhoodLegStatus.NOT_REQUIRED
    assert a1_leg.warnings == ("Program Bank cash and interest are disclosed in Schedule FA A2",)

    fa.manual_work = (SimpleNamespace(kind=SimpleNamespace(value="manual-field")),)
    manual_report = run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-2",
        fetch_prices=False,
    )
    manual_leg = next(item for item in manual_report.legs if item.name == "schedule-fa")
    assert manual_leg.status is RobinhoodLegStatus.MANUAL
    assert manual_leg.reasons == ("manual-field",)
    warning = reporting.capital_gains_warnings(cast("Any", cg))[0]
    assert sum(warning in item.warnings for item in manual_report.legs) == 1


@pytest.mark.parametrize(
    ("outcome", "expected_status", "marker", "json_expected"),
    [
        ("success", RobinhoodLegStatus.OK, None, True),
        ("incomplete", RobinhoodLegStatus.ERROR, "required filing values", False),
        # Domain messages stay the same; relays no longer prepend their class (#1044).
        ("schema-error", RobinhoodLegStatus.ERROR, "failed CBDT schema validation", False),
        ("io-error", RobinhoodLegStatus.ERROR, "OSError", False),
        ("unexpected", RobinhoodLegStatus.ERROR, "RuntimeError", False),
    ],
)
def test_all_fails_on_automatic_fa_json_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_status: RobinhoodLegStatus,
    marker: str | None,
    json_expected: bool,
) -> None:
    fa = _complete_fa()
    _stub_successful_all(monkeypatch, fa)
    a2_path = tmp_path / "schedule-fa-a2.csv"
    a3_path = tmp_path / "schedule-fa-a3.csv"
    a2_path.write_text("stale", encoding="utf-8")
    a3_path.write_text("stale", encoding="utf-8")
    json_path = tmp_path / "schedule-fa.json"
    if outcome == "success":
        json_path.write_text(
            '{"_ibkrFa":{"format":"cbdt-json-fragment"}}',
            encoding="utf-8",
        )

    def emit_json(
        _self: object, _value: object, path: Path, *, on_staged: Callable[[FileIdentity], None]
    ) -> None:
        if outcome == "incomplete":
            raise CbdtJsonIncompleteError(["missing filing value"])
        if outcome == "schema-error":
            raise CbdtSchemaError(
                itr_form="ITR-2",
                definition=A3_DEFINITION,
                row_index=0,
                field="NameOfEntity",
                keyword="maxLength",
            )
        if outcome == "io-error":
            raise OSError("disk failure")
        if outcome == "unexpected":
            raise RuntimeError("unexpected writer failure")
        path.write_text('{"ScheduleFA": {}}', encoding="utf-8")
        _claim(on_staged, path)

    monkeypatch.setattr(CbdtScheduleFaJsonWriter, "write", emit_json)
    report = run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-2",
        fetch_prices=False,
    )

    leg = next(item for item in report.legs if item.name == "schedule-fa")
    assert leg.status is expected_status
    # A failed attempt fails the run, which removes the requested A2/A3 output too.
    assert a2_path.exists() is json_expected
    assert a3_path.exists() is json_expected
    assert json_path.exists() is json_expected
    assert (json_path in leg.outputs) is json_expected
    messages = (*leg.reasons, *leg.warnings)
    assert any(marker in item for item in messages) if marker else not messages


def test_all_warns_instead_of_attempting_json_for_unsupported_ay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fa = _complete_fa()
    _stub_successful_all(monkeypatch, fa)
    attempted = False

    def emit_json(_self: object, _value: object, _path: Path) -> None:
        nonlocal attempted
        attempted = True

    monkeypatch.setattr(CbdtScheduleFaJsonWriter, "write", emit_json)
    report = run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=FinancialYear(2026),
        itr_form="ITR-2",
        fetch_prices=False,
    )

    leg = next(item for item in report.legs if item.name == "schedule-fa")
    assert leg.status is RobinhoodLegStatus.OK
    assert leg.reasons == ()
    assert any("AY 2026-27, not AY 2027-28" in item for item in leg.warnings)
    assert not attempted
    assert not (tmp_path / "schedule-fa.json").exists()
    assert (tmp_path / "schedule-fa-a2.csv").is_file()
    assert (tmp_path / "schedule-fa-a3.csv").is_file()


@pytest.mark.parametrize("blocked", _FTC_ONLY_BLOCKERS)
def test_all_removes_schedule_os_when_ftc_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked: str,
) -> None:
    os_path = tmp_path / "schedule-os.csv"
    blocker, guidance = _FTC_ONLY_BLOCKERS[blocked]
    _stub_successful_all(
        monkeypatch,
        SimpleNamespace(blockers=(), manual_work=(), program_bank=(), cbdt_json_eligible=False),
    )
    monkeypatch.setattr(
        pipeline,
        "_compute_income",
        lambda *_args, **_kwargs: (
            SimpleNamespace(blockers=(blocker,), partition_only_payer_accounts=()),
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "_annual_form_warnings",
        lambda _income: ("annual-form discrepancy",),
    )
    monkeypatch.setattr(
        pipeline,
        "_write_cg",
        lambda *_args, **_kwargs: (tmp_path / "schedule-cg.csv",),
    )

    def write_os(*_args: object, **_kwargs: object) -> tuple[Path, ...]:
        os_path.write_text("complete", encoding="utf-8")
        return (os_path,)

    monkeypatch.setattr(pipeline, "_write_os", write_os)
    monkeypatch.setattr(
        pipeline,
        "_write_fa",
        lambda *_args, **_kwargs: ((tmp_path / "schedule-fa-a2.csv",), False, ()),
    )

    report = run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-2",
        fetch_prices=False,
    )

    statuses = {item.name: item.status for item in report.legs}
    assert statuses["schedule-os"] is RobinhoodLegStatus.OK
    assert statuses["schedule-fa"] is RobinhoodLegStatus.OK
    assert statuses["income-ftc"] is RobinhoodLegStatus.ERROR
    # Schedule OS was written, then removed with the rest of the failed run.
    assert not os_path.exists()
    os_leg = next(item for item in report.legs if item.name == "schedule-os")
    assert os_leg.outputs == ()
    assert os_leg.reasons == ("output removed because the run failed",)
    income_leg = next(item for item in report.legs if item.name == "income-ftc")
    assert income_leg.reasons == ("\n".join(("unresolved income blockers", *guidance)),)
    assert income_leg.warnings == ("annual-form discrepancy",)


def test_all_does_not_write_os_after_income_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    monkeypatch.setattr(
        core,
        "attempt_compute",
        lambda *_args, **_kwargs: (
            _options(),
            _cg(),
            SimpleNamespace(blockers=()),
            None,
            None,
            None,
            None,
            None,
            {"income": "income layer does not cover every income/fee event"},
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "_write_cg",
        lambda *_args, **_kwargs: (tmp_path / "schedule-cg.csv",),
    )
    os_written = False

    def write_os(*_args: object, **_kwargs: object) -> tuple[Path, ...]:
        nonlocal os_written
        os_written = True
        return (tmp_path / "schedule-os.csv",)

    monkeypatch.setattr(pipeline, "_write_os", write_os)
    monkeypatch.setattr(pipeline, "_write_options", lambda *_args, **_kwargs: None)

    report = run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-2",
        fetch_prices=False,
    )

    statuses = {item.name: item.status for item in report.legs}
    assert statuses["schedule-os"] is RobinhoodLegStatus.ERROR
    assert statuses["income-ftc"] is RobinhoodLegStatus.ERROR
    assert not os_written


def test_all_continues_after_one_leg_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cg = _cg(checks=(_sale_price_check(1, 8, within_band=False),))
    _stub_successful_all(
        monkeypatch,
        SimpleNamespace(blockers=(), manual_work=(), program_bank=(), cbdt_json_eligible=False),
        cg=cg,
    )
    monkeypatch.setattr(
        pipeline,
        "_write_cg",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk failure")),
    )
    monkeypatch.setattr(
        pipeline,
        "_write_income",
        lambda *_args, **_kwargs: ((tmp_path / "schedule-os.csv",), Form67Readiness.READY),
    )
    monkeypatch.setattr(
        pipeline,
        "_write_fa",
        lambda *_args, **_kwargs: ((tmp_path / "schedule-fa-a2.csv",), False, ()),
    )
    report = run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-2",
        fetch_prices=False,
    )
    statuses = {item.name: item.status for item in report.legs}
    assert statuses["equity-cg"] is RobinhoodLegStatus.ERROR
    assert statuses["income-ftc"] is RobinhoodLegStatus.OK
    assert statuses["schedule-fa"] is RobinhoodLegStatus.OK
    equity_leg = next(item for item in report.legs if item.name == "equity-cg")
    assert equity_leg.warnings == reporting.capital_gains_warnings(cast("Any", cg))


def test_all_keeps_sale_price_warning_when_cg_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cg = _cg(
        blockers=(
            SimpleNamespace(
                kind=RobinhoodCapitalGainsBlockerKind.LOT_ATTRIBUTION,
                source_lot_blockers=(),
                source_identity_blockers=(),
            ),
        ),
        checks=(_sale_price_check(1, 8, within_band=False),),
    )
    _stub_successful_all(monkeypatch, _complete_fa(cbdt_json_eligible=False), cg=cg)

    report = run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-2",
        fetch_prices=False,
    )

    equity_leg = next(item for item in report.legs if item.name == "equity-cg")
    assert equity_leg.status is RobinhoodLegStatus.ERROR
    assert equity_leg.warnings == reporting.capital_gains_warnings(cast("Any", cg))


def test_all_validation_failure_blocks_fa_and_options_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_all(
        monkeypatch,
        SimpleNamespace(blockers=(), manual_work=(), program_bank=(), cbdt_json_eligible=False),
    )

    def validate(_bundle, computed) -> None:
        if computed.options is not None and computed.cg is None:
            raise RobinhoodPipelineError("options layer does not cover every option event")

    monkeypatch.setattr(pipeline, "_validate_owned_events", validate)
    monkeypatch.setattr(
        pipeline,
        "_write_cg",
        lambda *_args, **_kwargs: (tmp_path / "schedule-cg.csv",),
    )
    monkeypatch.setattr(
        pipeline,
        "_write_income",
        lambda *_args, **_kwargs: ((tmp_path / "schedule-os.csv",), Form67Readiness.READY),
    )
    fa_written = False
    options_written = False

    def write_fa(*_args, **_kwargs):
        nonlocal fa_written
        fa_written = True
        return (), False, ()

    def write_options(*_args, **_kwargs):
        nonlocal options_written
        options_written = True

    monkeypatch.setattr(pipeline, "_write_fa", write_fa)
    monkeypatch.setattr(pipeline, "_write_options", write_options)
    report = run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-2",
        fetch_prices=False,
    )
    statuses = {item.name: item.status for item in report.legs}
    assert statuses["equity-cg"] is RobinhoodLegStatus.OK
    assert statuses["income-ftc"] is RobinhoodLegStatus.OK
    assert statuses["schedule-fa"] is RobinhoodLegStatus.ERROR
    assert statuses["options"] is RobinhoodLegStatus.ERROR
    # Schedule FA stops for the failed options validation, not a crash on the
    # stub bundle in its inventory gate.
    fa_leg = next(item for item in report.legs if item.name == "schedule-fa")
    assert fa_leg.reasons == ("options layer does not cover every option event",)
    assert fa_written is False
    assert options_written is False


def test_all_put_blocker_reports_each_leg_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(
        has_trades=True,
        treatment=RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )
    options.blockers = (_LOCATED[RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT][0],)
    options.fsi_manual_work_required = True
    _stub_successful_all(
        monkeypatch,
        _complete_fa(cbdt_json_eligible=False),
    )
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: _bundle(has_options=True))
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: options)

    report = run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-2",
        fetch_prices=False,
    )

    statuses = {item.name: item.status for item in report.legs}
    assert statuses["equity-cg"] is RobinhoodLegStatus.OK
    assert statuses["schedule-os"] is RobinhoodLegStatus.OK
    assert statuses["income-ftc"] is RobinhoodLegStatus.MANUAL
    assert statuses["schedule-fa"] is RobinhoodLegStatus.ERROR
    assert statuses["options"] is RobinhoodLegStatus.ERROR
    income_leg = next(item for item in report.legs if item.name == "income-ftc")
    assert income_leg.reasons == (
        "remaining option items require manual Schedule FSI completion; "
        f"option blockers: {_LOCATED[RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT][1]}",
    )
    options_leg = next(item for item in report.legs if item.name == "options")
    assert options_leg.warnings[0] == ROBINHOOD_FNO_FEMA_WARNING
    assert len(options_leg.warnings) == 2
    assert "section 73" in options_leg.warnings[1]


def test_all_option_error_still_emits_fema_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "_load",
        lambda **_kwargs: _bundle(has_options=True),
    )
    monkeypatch.setattr(
        pipeline,
        "_compute_options",
        lambda *_args: (_ for _ in ()).throw(RobinhoodPipelineError("missing treatment")),
    )
    monkeypatch.setattr(
        pipeline,
        "_compute_cg",
        lambda *_args: _cg(),
    )
    monkeypatch.setattr(
        pipeline,
        "_compute_income",
        lambda *_args, **_kwargs: (
            SimpleNamespace(blockers=()),
            form67_assembly(),
            object(),
        ),
    )
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    monkeypatch.setattr(
        pipeline,
        "_write_cg",
        lambda *_args, **_kwargs: (tmp_path / "schedule-cg.csv",),
    )
    monkeypatch.setattr(
        pipeline,
        "_write_income",
        lambda *_args, **_kwargs: ((tmp_path / "schedule-os.csv",), Form67Readiness.READY),
    )
    report = run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-2",
        fetch_prices=False,
    )
    options_leg = next(item for item in report.legs if item.name == "options")
    assert options_leg.status is RobinhoodLegStatus.ERROR
    assert options_leg.warnings == (ROBINHOOD_FNO_FEMA_WARNING,)


def test_all_missing_option_classification_is_run_level_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "_load",
        lambda **_kwargs: _bundle(has_options=True),
    )
    monkeypatch.setattr(
        pipeline,
        "_compute_options",
        lambda *_args: (_ for _ in ()).throw(
            RobinhoodOptionClassificationError("explicit treatment required")
        ),
    )
    with pytest.raises(RobinhoodOptionClassificationError, match="explicit treatment"):
        run_robinhood_all(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path,
            financial_year=_FY,
            itr_form="ITR-2",
            fetch_prices=False,
        )


def test_all_itr_form_mismatch_is_run_level_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "_load",
        lambda **_kwargs: SimpleNamespace(unverified_entity_addresses=()),
    )
    monkeypatch.setattr(
        pipeline,
        "_compute_options",
        lambda *_args: (_ for _ in ()).throw(RobinhoodItrFormError("requires --itr-form ITR-3")),
    )
    wrote_cg = False

    def write_cg(*_args, **_kwargs):
        nonlocal wrote_cg
        wrote_cg = True
        return ()

    monkeypatch.setattr(pipeline, "_write_cg", write_cg)
    with pytest.raises(RobinhoodItrFormError, match="ITR-3"):
        run_robinhood_all(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path,
            financial_year=_FY,
            itr_form="ITR-2",
            fetch_prices=False,
        )
    assert wrote_cg is False


def test_income_warnings_include_fx_and_portal_but_not_the_tin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank relief-row TIN is a manual reason, not a warning (#1084)."""
    monkeypatch.setattr(
        core,
        "portal_import_warnings",
        lambda _entries, **_kwargs: ("portal warning",),
    )
    assembled = SimpleNamespace(
        form67_result=SimpleNamespace(entries=()),
        fx_policy_appropriate_for_ay=False,
        wht_fx_policy_appropriate_for_ay=False,
        fsi_result=SimpleNamespace(
            rows=(
                SimpleNamespace(
                    foreign_tax_inr=1,
                    taxpayer_id_number=None,
                ),
            )
        ),
    )
    warnings = _income_warnings(cast("Any", assembled))
    assert "portal warning" in warnings
    assert any("FX policy" in item for item in warnings)
    assert not any("taxpayer" in item.lower() for item in warnings)


def test_annual_form_warning_is_privacy_safe() -> None:
    warnings = core.annual_form_warnings(
        cast(
            "Any",
            SimpleNamespace(
                annual_form_discrepancies=(object(),),
                pending_annual_form_evidence=(),
            ),
        )
    )

    assert len(warnings) == 1
    assert "code 01" in warnings[0]
    assert "exact statement interest was used" in warnings[0]


def test_pending_annual_form_warnings_cover_income_tax_and_unpaired_dividends() -> None:
    pending = SimpleNamespace(
        withholding_references=(object(), object()),
        unpaired_us_dividend_references=(object(),),
    )

    warnings = core.annual_form_warnings(
        cast(
            "Any",
            SimpleNamespace(
                annual_form_discrepancies=(),
                pending_annual_form_evidence=(pending,),
            ),
        )
    )

    assert len(warnings) == 3
    assert "exact statement income and withholding were used" in warnings[0]
    assert "2 FY-end withholding event(s)" in warnings[1]
    assert "1 FY-end US dividend event(s)" in warnings[2]


def test_non_json_fa_run_preclears_prior_owned_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    json_path = tmp_path / "schedule-fa.json"
    json_path.write_text(
        '{"_ibkrFa":{"format":"cbdt-json-fragment"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pipeline,
        "_load",
        lambda **_kwargs: (_ for _ in ()).throw(RobinhoodPipelineError("stop")),
    )
    with pytest.raises(RobinhoodPipelineError, match="stop"):
        run_robinhood_fa(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path,
            financial_year=_FY,
            itr_form="ITR-2",
            format_id="review-csv",
            fetch_prices=False,
        )
    assert not json_path.exists()


def test_all_fa_write_failure_preserves_foreign_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign = tmp_path / "schedule-fa.json"
    foreign.write_text('{"ScheduleFA": {"manual": true}}', encoding="utf-8")
    bundle = _bundle()
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: _options())
    monkeypatch.setattr(
        pipeline,
        "_compute_cg",
        lambda *_args: _cg(),
    )
    monkeypatch.setattr(
        pipeline,
        "_compute_income",
        lambda *_args, **_kwargs: (
            SimpleNamespace(blockers=()),
            form67_assembly(),
            object(),
        ),
    )
    monkeypatch.setattr(pipeline, "_fa_prices", lambda *_args, **_kwargs: _fa_prices())
    monkeypatch.setattr(
        pipeline,
        "_compute_fa",
        lambda *_args, **_kwargs: SimpleNamespace(
            blockers=(),
            manual_work=(),
            program_bank=(),
            cbdt_json_eligible=False,
        ),
    )
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    monkeypatch.setattr(
        pipeline,
        "_write_cg",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        pipeline,
        "_write_income",
        lambda *_args, **_kwargs: ((), Form67Readiness.READY),
    )
    monkeypatch.setattr(
        pipeline,
        "_write_fa",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk failure")),
    )
    report = run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form="ITR-2",
        fetch_prices=False,
    )
    assert foreign.exists()
    assert next(item for item in report.legs if item.name == "schedule-fa").status is (
        RobinhoodLegStatus.ERROR
    )


_ALL_LEGS = (
    "equity-cg",
    "schedule-os",
    "income-ftc",
    "schedule-fa",
    "fa-a1",
    "options",
    "product-scope",
)


@pytest.mark.parametrize(
    "failing", ["equity-cg", "schedule-os", "income-ftc", "schedule-fa", "options"]
)
def test_all_failed_leg_removes_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failing: str,
) -> None:
    out = tmp_path / "out"
    written = _stub_writing_all(monkeypatch, out, failing=failing)

    assert _main_all(tmp_path) == 4

    stdout = capsys.readouterr().out
    statuses = {name: status for status, name in _LEG_LINE.findall(stdout)}
    assert tuple(statuses) == _ALL_LEGS
    assert statuses[failing] == "error"
    assert {name for name, status in statuses.items() if status == "error"} == {failing}
    # Every leg that ran wrote (the CG debug audits too), and nothing is left.
    assert {path.name for path in written} >= {
        "schedule-cg.xlsx",
        "schedule-cg-debug.csv",
        "schedule-cg-ltcg-debug.csv",
        "schedule-os.xlsx",
        "schedule-fsi.csv",
        "schedule-fa-a2.csv",
        "robinhood-options.csv",
    }
    assert (out / "schedule-fa.json" in written) is (failing != "schedule-fa")
    assert not any(path.exists() for path in written)
    assert not any(str(path) in stdout for path in written)
    # The writer's OSError is named by fixed text, never its message (#1044).
    assert f"-> could not write the {failing} outputs in {out} (unknown error)\n" in stdout
    assert_no_markers(stdout, "disk failure")
    assert stdout.count("-> output removed because the run failed") == 4


@pytest.mark.parametrize(
    ("failing", "filenames"),
    [
        ("equity-cg", outputs.CG_FILING_FILENAMES),
        ("schedule-os", outputs.OS_FILENAMES),
        ("income-ftc", outputs.INCOME_FILENAMES),
        ("schedule-fa", outputs.FA_FILENAMES),
        ("options", outputs.OPTIONS_FILENAMES),
    ],
)
@pytest.mark.parametrize(
    ("cleanup_error", "cleanup_text"),
    [
        (
            PermissionError(errno.EACCES, "cannot clear output"),
            "could not remove the {leg} outputs in {out} (EACCES)",
        ),
        (RuntimeError("cleanup bug"), r"internal error \(RuntimeError at ibkr_fa/[\w/]+\.py:\d+\)"),
        (ConfigError("cleanup", "refused"), "ConfigError at field 'cleanup': refused"),
    ],
    ids=["permission", "unexpected", "frozen-domain"],
)
def test_all_cleanup_failure_still_removes_earlier_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failing: str,
    filenames: tuple[str, ...],
    cleanup_error: Exception,
    cleanup_text: str,
) -> None:
    # Cleanup failure must not stop later writers or hide the complete report.
    out = tmp_path / "out"
    written = _stub_writing_all(monkeypatch, out, failing=failing)
    clear_leg = pipeline._clear_leg

    def fail_cleanup(output_dir: Path, names: tuple[str, ...]) -> None:
        if names == filenames:
            raise cleanup_error
        clear_leg(output_dir, names)

    monkeypatch.setattr(pipeline, "_clear_leg", fail_cleanup)
    monkeypatch.setattr(outputs, "clear_leg", fail_cleanup)

    assert _main_all(tmp_path) == 4

    captured = capsys.readouterr()
    statuses = {name: status for status, name in _LEG_LINE.findall(captured.out)}
    assert tuple(statuses) == _ALL_LEGS
    assert {name for name, status in statuses.items() if status == "error"} == {failing}
    # Each failure is named by fixed text, a domain error by its own (#1044).
    cleanup = cleanup_text.format(leg=failing, out=out)
    if isinstance(cleanup_error, OSError):
        cleanup = re.escape(cleanup)
    assert re.search(
        rf"-> {re.escape(f'could not write the {failing} outputs in {out} (unknown error)')}"
        rf"; cleanup failed: {cleanup}\n",
        captured.out,
    )
    assert_no_markers(
        captured.out + captured.err, "disk failure", "cannot clear output", "cleanup bug"
    )
    assert "run outputs were removed" in captured.err
    assert not any(str(path) in captured.out for path in written)
    assert {path.name for path in written} >= {
        "schedule-cg.xlsx",
        "schedule-os.xlsx",
        "schedule-fsi.csv",
        "schedule-fa-a2.csv",
        "robinhood-options.csv",
    }
    assert out / "schedule-cg.xlsx" in written
    assert not any(path.exists() for path in written)

    with pytest.raises(pipeline.RobinhoodAllRemovalError) as raised:
        run_robinhood_all(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=out,
            financial_year=_FY,
            itr_form="ITR-2",
            fetch_prices=False,
            debug=True,
        )
    relayed = (
        re.escape(f"could not remove outputs in {out} (EACCES)")
        if isinstance(cleanup_error, OSError)
        else cleanup
    )
    assert re.fullmatch(
        rf"leg output cleanup failed: {relayed}; run outputs were removed", str(raised.value)
    )
    assert_replaced_unchained(
        raised.value,
        None if isinstance(cleanup_error, (OSError, IbkrFaError)) else type(cleanup_error),
        markers=("disk failure", str(cleanup_error))
        if not isinstance(cleanup_error, IbkrFaError)
        else ("disk failure",),
    )
    assert tuple(leg.name for leg in raised.value.report.legs) == _ALL_LEGS
    assert not any(leg.outputs for leg in raised.value.report.legs)


def test_all_failed_run_keeps_foreign_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    foreign = out / "schedule-fa.json"
    original_json = '{"ScheduleFA": {"manual": true}}'
    foreign.write_text(original_json, encoding="utf-8")
    written = _stub_writing_all(monkeypatch, out, failing="options")

    assert _main_all(tmp_path) == 4

    assert foreign.read_text(encoding="utf-8") == original_json
    assert foreign not in written
    assert not any(path.exists() for path in written)


@pytest.mark.parametrize(("manual", "expected_exit"), [(False, 0), (True, 3)])
def test_all_without_failed_leg_keeps_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    manual: bool,
    expected_exit: int,
) -> None:
    manual_work = (SimpleNamespace(kind=SimpleNamespace(value="manual-field")),) if manual else ()
    out = tmp_path / "out"
    written = _stub_writing_all(monkeypatch, out, fa=_complete_fa(manual_work=manual_work))

    assert _main_all(tmp_path) == expected_exit

    stdout = capsys.readouterr().out
    assert out / "schedule-fa.json" in written
    for path in written:
        assert path.exists()
        assert f"-> {path}" in stdout
    assert "output removed" not in stdout


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_all_removal_failure_names_the_file_left_behind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cleanup_failure: bool,
) -> None:
    out = tmp_path / "out"
    written = _stub_writing_all(monkeypatch, out, failing="income-ftc")
    if cleanup_failure:
        clear_leg = pipeline._clear_leg

        def fail_income_cleanup(output_dir: Path, filenames: tuple[str, ...]) -> None:
            if filenames == outputs.INCOME_FILENAMES:
                raise RuntimeError("income cleanup failed")
            clear_leg(output_dir, filenames)

        monkeypatch.setattr(pipeline, "_clear_leg", fail_income_cleanup)
    stuck = out / "schedule-cg.xlsx"
    unlink = Path.unlink

    def unlink_except_stuck(path: Path, *, missing_ok: bool = False) -> None:
        # The preclear finds no file here, and equity-cg succeeds, so only the
        # post-run removal reaches this raise.
        if path == stuck and path.exists():
            raise PermissionError(f"permission denied: {path}")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink_except_stuck)

    assert _main_all(tmp_path) == 4

    captured = capsys.readouterr()
    statuses = {name: status for status, name in _LEG_LINE.findall(captured.out)}
    assert tuple(statuses) == _ALL_LEGS
    assert "do NOT file this output" in captured.out
    if cleanup_failure:
        assert "cleanup failed: internal error (RuntimeError at ibkr_fa/" in captured.out
        assert_no_markers(captured.out, "income cleanup failed")
    assert captured.err.startswith("error: ")
    assert str(stuck) in captured.err
    assert "may still be there" in captured.err
    assert stuck.exists()
    assert not any(path.exists() for path in written if path != stuck)

    # Repeat with combined streams: stdout buffers, stderr writes immediately.
    unlink(stuck)
    buffer = BytesIO()
    stdout = TextIOWrapper(buffer, encoding="utf-8")
    stderr = TextIOWrapper(buffer, encoding="utf-8", write_through=True)
    with monkeypatch.context() as streams:
        streams.setattr(sys, "stdout", stdout)
        streams.setattr(sys, "stderr", stderr)
        assert _main_all(tmp_path) == 4
    stdout.flush()
    combined = buffer.getvalue().decode("utf-8")
    stdout.detach()
    stderr.detach()
    assert combined.startswith("ROBINHOOD SUMMARY")
    assert combined.index("ROBINHOOD SUMMARY") < combined.index("\nerror: ")


@pytest.mark.parametrize("runner", [run_robinhood_all, run_robinhood_fa], ids=["all", "fa"])
@pytest.mark.parametrize("link_target", [None, "schedule-fa.json", "missing.json"])
def test_all_refuses_unreadable_json_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: Any,
    link_target: str | None,
) -> None:
    unreadable = tmp_path / "schedule-fa.json"
    if link_target is None:
        unreadable.write_bytes(b"\xff not json")
    else:
        unreadable.symlink_to(link_target)
    loaded = False

    def load(**_kwargs: object) -> object:
        nonlocal loaded
        loaded = True
        raise AssertionError("bundle must not load when JSON cannot be inspected")

    monkeypatch.setattr(pipeline, "_load", load)
    expected_error = (
        OutputCollisionError if link_target == unreadable.name else CbdtJsonUnreadableError
    )
    with pytest.raises(expected_error, match=r"could not (inspect|check)"):
        runner(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path,
            financial_year=_FY,
            itr_form="ITR-2",
            fetch_prices=False,
        )
    if link_target is None:
        assert unreadable.read_bytes() == b"\xff not json"
    else:
        assert unreadable.readlink() == Path(link_target)
    assert not loaded


@pytest.mark.parametrize("link_target", ["schedule-fa.json", "missing.json"])
def test_robinhood_json_preclear_inspects_symlinks(tmp_path: Path, link_target: str) -> None:
    path = tmp_path / "schedule-fa.json"
    path.symlink_to(link_target)
    with pytest.raises(CbdtJsonUnreadableError, match="could not inspect"):
        preclear_own_cbdt_fragment(path)
    assert path.readlink() == Path(link_target)


# --- Form 67 readiness (#601) ---------------------------------------------------


def _zero_credit_assembly() -> SimpleNamespace:
    return form67_assembly(dividend_entry("US", foreign_tax="25", normal_tax="0", credit="0"))


def _overwritten_relief_assembly() -> SimpleNamespace:
    """#900: ₹250 of relief where the portal would fill min(₹300, ₹400) = ₹300."""
    row = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("1000"),
        foreign_tax_inr=Decimal("300"),
        event_count=1,
        tax_payable_inr=Decimal("400"),
        tax_relief_inr=Decimal("250"),
        dtaa_article="Article 25(2)(a)",
    )
    return SimpleNamespace(
        form67_result=Form67ComputeResult(entries=()),
        fsi_result=ScheduleFsiComputeResult(rows=[row]),
    )


# Each FTC refusal that stops the income and all runs before anything is written.
_FTC_REFUSALS = pytest.mark.parametrize(
    ("assembly", "error", "message"),
    [
        (_zero_credit_assembly, ConfigError, r"US/DIVIDEND: the computed credit rounds to ₹0"),
        (_overwritten_relief_assembly, InvariantError, r"read-only relief"),
    ],
    ids=["zero-credit", "overwritten-relief"],
)


def _record_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    results: dict[str, object] = {
        "_write_cg": (),
        "_write_os": (),
        "_write_income": ((), Form67Readiness.READY),
        "_write_fa": ((), True, ()),
        "_write_options": None,
    }
    for name, result in results.items():

        def write(*_args: object, _name: str = name, _result: object = result, **_kw: object):
            calls.append(_name)
            return _result

        monkeypatch.setattr(pipeline, name, write)
    return calls


@_FTC_REFUSALS
def test_income_ftc_refusal_stops_before_schedule_os(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assembly: Callable[[], SimpleNamespace],
    error: type[Exception],
    message: str,
) -> None:
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: _bundle())
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: _options())
    monkeypatch.setattr(pipeline, "_compute_cg", lambda *_args: _cg())
    monkeypatch.setattr(
        pipeline,
        "_compute_income",
        lambda *_args, **_kwargs: (
            SimpleNamespace(blockers=()),
            assembly(),
            object(),
        ),
    )
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    monkeypatch.setattr(pipeline, "_compute_os", lambda *_args: object())
    calls = _record_writes(monkeypatch)
    with pytest.raises(error, match=message):
        run_robinhood_income(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path,
            financial_year=_FY,
            itr_form="ITR-2",
        )
    assert calls == []


@_FTC_REFUSALS
@pytest.mark.parametrize("income_validation_fails", [False, True])
def test_all_ftc_refusal_stops_before_any_leg_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    income_validation_fails: bool,
    assembly: Callable[[], SimpleNamespace],
    error: type[Exception],
    message: str,
) -> None:
    _stub_successful_all(monkeypatch, _complete_fa())
    monkeypatch.setattr(
        pipeline,
        "_compute_income",
        lambda *_args, **_kwargs: (
            SimpleNamespace(blockers=()),
            assembly(),
            object(),
        ),
    )
    monkeypatch.setattr(pipeline, "_compute_os", lambda *_args: object())
    validation_failed = False

    def validate(_bundle: object, computed: core.ComputedRobinhood) -> None:
        nonlocal validation_failed
        if income_validation_fails and computed.income is not None:
            validation_failed = True
            raise RobinhoodPipelineError("income layer does not cover every income/fee event")

    monkeypatch.setattr(pipeline, "_validate_owned_events", validate)
    calls = _record_writes(monkeypatch)
    with pytest.raises(error, match=message):
        run_robinhood_all(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path,
            financial_year=_FY,
            itr_form="ITR-2",
            fetch_prices=False,
        )
    assert calls == []
    assert validation_failed is income_validation_fails


def test_income_withholds_form67_without_calling_every_entry_blank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A READY US entry beside a BLANK DE one makes the aggregate BLANK (#791).
    DE has a vendored DTAA row, so an override can clear its row (#1461)."""
    (zero,) = _zero_credit_assembly().form67_result.entries
    ready = replace(
        zero,
        tax_payable_normal_inr=Decimal("30"),
        section_90_credit_inr=Decimal("25"),
        total_credit_inr=Decimal("25"),
    )
    blank = Form67Entry(
        serial_number=2,
        cbdt_country_code="49",
        country_name_portal="GERMANY",
        iso_alpha2="DE",
        source_of_income=Form67SourceOfIncome.DIVIDEND,
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("10"),
        foreign_tax_rate_percent=Decimal("10"),
        event_count=1,
        tax_paid_rate_percent=Decimal("10"),
        section_90_dtaa_article="Article 10(2)",
        section_90_dtaa_rate_percent=Decimal("10"),
        highest_payment_rate_percent=Decimal("10"),
        treaty_rate_status=Form67TreatyRateStatus.WITHIN,
        eligible_foreign_tax_inr=Decimal("10"),
    )
    assembled = form67_assembly(ready, blank)
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: _bundle())
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: _options())
    monkeypatch.setattr(pipeline, "_compute_cg", lambda *_args: _cg())
    monkeypatch.setattr(
        pipeline,
        "_compute_income",
        lambda *_args, **_kwargs: (
            SimpleNamespace(blockers=()),
            assembled,
            ScheduleTrComputeResult(rows=[]),
        ),
    )
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    report = run_robinhood_income(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path / "out",
        financial_year=_FY,
        itr_form="ITR-2",
    )
    assert report.status is RobinhoodLegStatus.MANUAL
    assert report.reasons == (
        "one or more Form 67 entries lack Indian tax payable and credit, so form-67.csv is "
        "withheld; add an exact [[form67.tax_payable_overrides]] amount for each country "
        "and source of income and rerun, or enter Form 67 on the portal by hand using "
        "form-67-debug.csv as a reference",
    )
    assert [path.name for path in report.outputs if path.name.startswith("form-67")] == [
        "form-67-debug.csv"
    ]


@pytest.mark.parametrize(
    ("tin_country", "status"), [("GB", RobinhoodLegStatus.MANUAL), ("US", RobinhoodLegStatus.OK)]
)
def test_relief_row_without_a_tin_makes_income_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tin_country: str,
    status: RobinhoodLegStatus,
) -> None:
    """#1084: the US relief row needs its own TIN; a GB one doesn't count."""
    _stub_relief_run(tmp_path, monkeypatch, tin_country)
    reasons = (BLANK_RELIEF_TIN_WARNING,) if status is RobinhoodLegStatus.MANUAL else ()

    report = run_robinhood_income(**_run_kwargs(tmp_path))

    assert (report.status, report.reasons) == (status, reasons)
    assert [path.name for path in report.outputs] == [
        "schedule-fsi.csv",
        "schedule-tr.csv",
        "form-67.csv",
    ]
    assert all(path.is_file() for path in report.outputs)
    all_report = run_robinhood_all(**_run_kwargs(tmp_path), fetch_prices=False)
    leg = next(item for item in all_report.legs if item.name == "income-ftc")
    assert (leg.status, leg.reasons) == (status, reasons)
    assert all_report.status is status


# ---- #1461: the BLANK Form 67 action offers an override only where one helps ----

_FORM67_WITHHELD = (
    "one or more Form 67 entries lack Indian tax payable and credit, so form-67.csv is withheld"
)
_SAME_DAY_GAP = same_day_gap("US/DIVIDEND")
_SOURCE_RATE_GAP = source_rate_gap("US/DIVIDEND")
_NO_DTAA_ROW_GAP = unvendored_dtaa_gap("CH")
_BY_HAND = "so enter Form 67 on the portal by hand using form-67-debug.csv as a reference"


def _stub_same_day_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, override: bool) -> None:
    """#1461's example on ``_stub_relief_run``: a regular and a special dividend
    of one security, paid the same day in one account, each with US tax
    withheld, and the US/DIVIDEND [[form67.tax_payable_overrides]] amount only
    when ``override`` is set."""
    payment_date = date(2025, 6, 20)
    bundle = _stub_relief_run(
        tmp_path,
        monkeypatch,
        "US",
        events=(
            *_income_base_events(),
            _income_event(
                RobinhoodEventKind.CASH_DIVIDEND,
                4,
                _PAYMENT_DATE,
                "40.00",
                rate="4",
                source_payment_date=payment_date,
            ),
            _income_event(
                RobinhoodEventKind.NRA_WITHHOLDING,
                5,
                _PAYMENT_DATE,
                "-10.00",
                rate="4",
                source_payment_date=payment_date,
            ),
        ),
        packet=_income_packet(gross="140.00", tax="35.00"),
    )
    if not override:
        bundle.config = replace(bundle.config, form67=Form67Config())


def test_same_day_payment_reason_says_an_override_cannot_clear_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two same-day dividends leave US/DIVIDEND blank, and no override can
    clear it, so the income leg says so and points to form-67-debug.csv."""
    _stub_same_day_run(tmp_path, monkeypatch, override=False)
    reasons = (
        f"{_FORM67_WITHHELD}. {_SAME_DAY_GAP} An exact [[form67.tax_payable_overrides]] "
        f"amount won't clear this entry, {_BY_HAND}",
    )

    report = run_robinhood_income(**_run_kwargs(tmp_path))

    assert (report.status, report.reasons) == (RobinhoodLegStatus.MANUAL, reasons)
    assert [path.name for path in report.outputs] == [
        "schedule-fsi.csv",
        "schedule-tr.csv",
        "form-67-debug.csv",
    ]
    all_report = run_robinhood_all(**_run_kwargs(tmp_path), fetch_prices=False)
    leg = next(item for item in all_report.legs if item.name == "income-ftc")
    assert (leg.status, leg.reasons) == (RobinhoodLegStatus.MANUAL, reasons)


def test_same_day_payment_with_an_override_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: with the override set, the row refuses as before (#890)."""
    _stub_same_day_run(tmp_path, monkeypatch, override=True)
    refusal = refused_override(_SAME_DAY_GAP, "US/DIVIDEND")

    for run in (run_robinhood_income, partial(run_robinhood_all, fetch_prices=False)):
        with pytest.raises(ConfigError) as caught:
            run(**_run_kwargs(tmp_path))
        assert (caught.value.field, caught.value.message) == ("form67", refusal)


def _us_unproven(*, same_day: bool) -> Form67Entry:
    """A BLANK US entry whose credit no override can compute: same-day
    payments (``payment_allocation_unproven``), or no source-currency rate."""
    return Form67Entry(
        serial_number=1,
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        source_of_income=Form67SourceOfIncome.DIVIDEND,
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("25"),
        foreign_tax_rate_percent=Decimal("25"),
        event_count=2,
        tax_paid_rate_percent=Decimal("25") if same_day else None,
        section_90_dtaa_article="Article 10(2)(b)",
        section_90_dtaa_rate_percent=Decimal("25"),
        treaty_rate_status=Form67TreatyRateStatus.UNPROVEN,
        payment_allocation_unproven=same_day,
    )


def _ch_blank(serial: int) -> Form67Entry:
    """A BLANK CH entry: CH has no vendored DTAA row, so no override clears it."""
    return dividend_entry("CH", foreign_tax="10", serial=serial)


def _de_within(serial: int) -> Form67Entry:
    """A BLANK DE entry within Germany's 10% treaty rate: an override clears it."""
    return replace(
        _ch_blank(serial),
        cbdt_country_code="49",
        country_name_portal="GERMANY",
        iso_alpha2="DE",
        tax_paid_rate_percent=Decimal("10"),
        section_90_dtaa_article="Article 10(2)",
        section_90_dtaa_rate_percent=Decimal("10"),
        highest_payment_rate_percent=Decimal("10"),
        treaty_rate_status=Form67TreatyRateStatus.WITHIN,
        eligible_foreign_tax_inr=Decimal("10"),
    )


@pytest.mark.parametrize(
    ("entries", "reason"),
    [
        pytest.param(
            (_us_unproven(same_day=False),),
            f"{_FORM67_WITHHELD}. {_SOURCE_RATE_GAP} An exact [[form67.tax_payable_overrides]] "
            f"amount won't clear this entry, {_BY_HAND}",
            id="no-source-rate",
        ),
        pytest.param(
            (_us_unproven(same_day=True), _de_within(2)),
            f"{_FORM67_WITHHELD}. {_SAME_DAY_GAP} An exact [[form67.tax_payable_overrides]] "
            f"amount won't clear this entry, {_BY_HAND}; for DE/DIVIDEND, you can still add "
            "an exact [[form67.tax_payable_overrides]] amount and rerun first",
            id="mixed",
        ),
        pytest.param(
            (_us_unproven(same_day=False), _ch_blank(2), _de_within(3)),
            f"{_FORM67_WITHHELD}. {_SOURCE_RATE_GAP} {_NO_DTAA_ROW_GAP} An exact "
            f"[[form67.tax_payable_overrides]] amount won't clear these entries, {_BY_HAND}; "
            "for DE/DIVIDEND, you can still add an exact [[form67.tax_payable_overrides]] "
            "amount and rerun first",
            id="two-uncleared",
        ),
    ],
)
def test_blank_reason_offers_an_override_only_where_one_can_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entries: tuple[Form67Entry, ...],
    reason: str,
) -> None:
    """#1461: each row an override can't clear is named with its gap, and the
    override is offered only for the rest."""
    assembled = form67_assembly(*entries)
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: _bundle())
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: _options())
    monkeypatch.setattr(pipeline, "_compute_cg", lambda *_args: _cg())
    monkeypatch.setattr(
        pipeline,
        "_compute_income",
        lambda *_args, **_kwargs: (
            SimpleNamespace(blockers=()),
            assembled,
            ScheduleTrComputeResult(rows=[]),
        ),
    )
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)

    report = run_robinhood_income(**_run_kwargs(tmp_path))

    assert (report.status, report.reasons) == (RobinhoodLegStatus.MANUAL, (reason,))
    assert [path.name for path in report.outputs if path.name.startswith("form-67")] == [
        "form-67-debug.csv"
    ]


# ---- #1446: a reviewed lending fee beside #1084's US dividend ----


def _stub_fee_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *overrides: tuple[Form67SourceOfIncome, str],
) -> None:
    """``_stub_relief_run``'s US dividend plus a reviewed USD 10.00 US
    stock-lending fee, with these US [[form67.tax_payable_overrides]]."""
    bundle = _stub_relief_run(
        tmp_path,
        monkeypatch,
        "US",
        events=(
            *_income_base_events(),
            _income_event(RobinhoodEventKind.STOCK_LENDING_INCOME, 4, _PAYMENT_DATE, "10.00"),
        ),
        robinhood=_income_config(reviewed_fee_source=True, other_account=_OTHER_ACCOUNT),
    )
    bundle.config = replace(
        bundle.config,
        form67=Form67Config(
            tuple(
                Form67TaxPayableOverride("US", source, Decimal(amount))
                for source, amount in overrides
            )
        ),
    )


def test_fee_country_dividend_and_others_overrides_complete_robinhood_income(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exact OTHERS amount for the fee completes the US row's relief, so
    the income leg files Form 67."""
    _stub_fee_run(
        tmp_path,
        monkeypatch,
        (Form67SourceOfIncome.DIVIDEND, "3000"),
        (Form67SourceOfIncome.OTHERS, "300"),
    )

    report = run_robinhood_income(**_run_kwargs(tmp_path))

    assert (report.status, report.reasons) == (RobinhoodLegStatus.OK, ())
    assert [path.name for path in report.outputs] == [
        "schedule-fsi.csv",
        "schedule-tr.csv",
        "form-67.csv",
    ]
    assert all(path.is_file() for path in report.outputs)
    assert not (tmp_path / "form-67-debug.csv").exists()


@pytest.mark.parametrize(
    ("overrides", "refusal"),
    [
        (((Form67SourceOfIncome.DIVIDEND, "3000"),), _RELIEF_REFUSAL),
        (
            ((Form67SourceOfIncome.DIVIDEND, "1000"), (Form67SourceOfIncome.OTHERS, "300")),
            _PORTAL_RELIEF_REFUSAL,
        ),
    ],
    ids=["dividend-alone", "credit-below-portal-relief"],
)
def test_fee_country_refusals_leave_no_robinhood_income_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    overrides: tuple[tuple[Form67SourceOfIncome, str], ...],
    refusal: str,
) -> None:
    """Without an OTHERS amount the relief check refuses. With one, a
    DIVIDEND amount that caps the credit below the portal's relief, min(tax
    paid, the row's summed tax payable), refuses at the portal check (#900)."""
    _stub_fee_run(tmp_path, monkeypatch, *overrides)

    assert _main("robinhood-income", tmp_path) == 4

    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", f"error: {refusal}\n")
    assert list((tmp_path / "out").iterdir()) == []


# ---- #1321: a proven non-US dividend beside a reconciled Form 1042-S ----


@pytest.mark.parametrize(
    ("gross", "exit_code"), [("100.00", 0), ("6283.00", 4)], ids=["files", "form-counts-both"]
)
def test_non_us_dividend_beside_a_reconciled_form_files_robinhood_income(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    gross: str,
    exit_code: int,
) -> None:
    """#1321: the 2025 account holds #1084's US dividend, which its code-06
    form covers, and a Canadian XQCA dividend with DTAX. ``files`` fails on
    aa7ed98, which refused FSI, TR and Form 67; ``form-counts-both`` is a form
    whose Box 2 also counts the XQCA dividend, which still refuses. Neither run
    prints the XQCA amount."""
    bundle = _stub_relief_run(
        tmp_path,
        monkeypatch,
        "US",
        events=(
            *_income_base_events(),
            *_non_us_events(amount=_NON_US_MARKER_AMOUNT, tax="-927.49"),
        ),
        packet=_income_packet(gross=gross),
        robinhood=_non_us_config(other_account=_OTHER_ACCOUNT),
    )
    # The CA relief row needs its own TIN and Indian tax payable, as US's has.
    bundle.config = replace(
        bundle.config,
        account=replace(bundle.config.account, foreign_tins={"US": _TIN_MARKER, "CA": _TIN_MARKER}),
        form67=Form67Config(
            tuple(
                Form67TaxPayableOverride(country, Form67SourceOfIncome.DIVIDEND, Decimal("3000"))
                for country in ("CA", "US")
            )
        ),
    )
    monkeypatch.setattr(pipeline, "_compute_os", core.compute_os)
    monkeypatch.setattr(pipeline, "_write_os", outputs.write_os)
    monkeypatch.setattr(pipeline, "_annual_form_warnings", core.annual_form_warnings)
    monkeypatch.setattr(pipeline, "_income_warnings", core.income_warnings)

    assert _main("robinhood-income", tmp_path) == exit_code

    captured = capsys.readouterr()
    assert_no_markers(captured.out + captured.err, *_NON_US_MARKERS)
    written = sorted(path.name for path in (tmp_path / "out").iterdir())
    if exit_code:
        assert "Robinhood income-ftc: error" in captured.out
        assert written == []
        return
    assert "Robinhood income-ftc: ok" in captured.out
    assert written == ["form-67.csv", "schedule-fsi.csv", "schedule-os.xlsx", "schedule-tr.csv"]


def test_above_rate_non_us_payment_files_the_country_rate_in_form67(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1448: beside #1084's US dividend, Canadian XQCA dividends of USD
    100.00 on 16 and 30 June 2025 with USD 25.01 and 25.00 of DTAX. Canada's
    25.005% is within two half-cents of its 25% treaty rate, but the first
    payment is above its own half-cent, so Form 67 column 6 files 25.01 for
    Canada, and the fractional rate makes the run manual work (#1447); main
    filed 25.00. The US row stays 25.00."""
    second = tuple(
        replace(event, source_record_index=event.source_record_index + 2)
        for event in _non_us_events(day=date(2025, 6, 30), tax="-25.00")[1:]
    )
    bundle = _stub_relief_run(
        tmp_path,
        monkeypatch,
        "US",
        events=(*_income_base_events(), *_non_us_events(tax="-25.01"), *second),
        robinhood=_non_us_config(other_account=_OTHER_ACCOUNT),
    )
    bundle.config = replace(
        bundle.config,
        account=replace(bundle.config.account, foreign_tins={"US": _TIN_MARKER, "CA": _TIN_MARKER}),
        form67=Form67Config(
            tuple(
                Form67TaxPayableOverride(country, Form67SourceOfIncome.DIVIDEND, Decimal("3000"))
                for country in ("CA", "US")
            )
        ),
    )
    monkeypatch.setattr(pipeline, "_compute_os", core.compute_os)
    monkeypatch.setattr(pipeline, "_write_os", outputs.write_os)
    monkeypatch.setattr(pipeline, "_annual_form_warnings", core.annual_form_warnings)
    monkeypatch.setattr(pipeline, "_income_warnings", core.income_warnings)

    exit_code = _main("robinhood-income", tmp_path)

    with (tmp_path / "out" / "form-67.csv").open(encoding="utf-8", newline="") as handle:
        rates = {
            row["Name of the country/ specified territory"]: row["Rate(%)"]
            for row in csv.DictReader(handle)
        }
    assert rates == {"1": "25.01", "2": "25.00"}
    assert exit_code == 3
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Robinhood income-ftc: manual"
    assert f"  Action: {FRACTIONAL_RATE_ACTION}" in lines
    assert any("may blank fractional Rate(%) 25.01 for country code 1;" in line for line in lines)


# ---- #1247: a whole-dollar Form 1042-S $0 that exact dividends prove ----

# FSI "Income from outside India" and "Tax paid outside India" for each
# _ZERO_FILED row: its exact dividend and withholding at ₹85.10 per USD,
# not its printed boxes.
_ZERO_FSI_INR = {
    "box-7a-zero": ("85", "21"),
    "box-7a-zero-two-dollars": ("167", "42"),
    "box-2-zero": ("34", "9"),
    "box-2-zero-zero-rate": ("34", "0"),
}


def _stub_zero_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dividend: str,
    withholding: str | None,
    *copies: tuple[str, str, str],
) -> None:
    _stub_relief_run(
        tmp_path,
        monkeypatch,
        "US",
        events=(),
        other_account=_ZERO_ACCOUNT,
        other_rows=_zero_rows(dividend, withholding),
        packet=_zero_form(*copies),
    )
    monkeypatch.setattr(pipeline, "_compute_os", core.compute_os)
    monkeypatch.setattr(pipeline, "_write_os", outputs.write_os)


def _fsi_amounts(path: Path) -> tuple[str, str]:
    cells = dict(line.split(",", 1) for line in path.read_text(encoding="utf-8-sig").splitlines())
    return (
        cells["Income from outside India (included in PART B-TI)"],
        cells["Tax paid outside India"],
    )


@pytest.mark.parametrize(
    ("row", "dividend", "withholding", "form"),
    [(row, *values) for row, values in _ZERO_FILED.items()],
    ids=_ZERO_FILED,
)
def test_whole_dollar_zero_form_box_files_robinhood_income(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    row: str,
    dividend: str,
    withholding: str | None,
    form: tuple[str, str, str],
) -> None:
    """#1247: ``robinhood-income`` writes OS, FSI, TR and Form 67 from the
    exact events under a code-06 Box 2 or Box 7a that prints $0."""
    _stub_zero_run(tmp_path, monkeypatch, dividend, withholding, form)

    assert _main("robinhood-income", tmp_path) == 0

    assert "Robinhood income-ftc: ok" in capsys.readouterr().out
    out = tmp_path / "out"
    assert sorted(path.name for path in out.iterdir()) == [
        "form-67.csv",
        "schedule-fsi.csv",
        "schedule-os.xlsx",
        "schedule-tr.csv",
    ]
    assert _fsi_amounts(out / "schedule-fsi.csv") == _ZERO_FSI_INR[row]


@pytest.mark.parametrize(
    ("dividend", "withholding", "copies"),
    [
        *(
            (dividend, withholding, (form,))
            for dividend, withholding, form in _ZERO_REFUSED.values()
        ),
        ("100.00", "25.00", (("100.00", "25.00", "25.00"), ("0.00", "0.00", "15.00"))),
    ],
    ids=[*_ZERO_REFUSED, "empty-zero-gross-copy"],
)
def test_whole_dollar_zero_controls_refuse_robinhood_income_without_echo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dividend: str,
    withholding: str | None,
    copies: tuple[tuple[str, str, str], ...],
) -> None:
    """#1247: the refused controls exit 4, write nothing and print neither
    the dividend description nor the account reference."""
    _stub_zero_run(tmp_path, monkeypatch, dividend, withholding, *copies)

    assert _main("robinhood-income", tmp_path) == 4

    captured = capsys.readouterr()
    assert "Robinhood income-ftc: error" in captured.out
    assert_no_markers(captured.out + captured.err, *_ZERO_MARKERS)
    assert list((tmp_path / "out").iterdir()) == []


# ---- #752, #1383: each Robinhood sale's recorded Amount is its consideration ----


def _gross_differs_from_amount() -> Any:
    item = sale("12", "10")
    return cg_result(
        replace(item, closed_lot=replace(item.closed_lot, gross_proceeds_money=Decimal("10.02")))
    )


@pytest.mark.parametrize("command", ["robinhood-cg", "robinhood-income", "robinhood-all"])
def test_proven_gross_that_differs_from_amount_refuses_every_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """No transfer-expense path exists yet for a proven sale charge."""
    ftc_writes = _stub_cg_run(monkeypatch, _gross_differs_from_amount)

    assert _main(command, tmp_path) == 4

    captured = capsys.readouterr()
    output = captured.out + captured.err
    proven_gross = PROVEN_GROSS_REFUSAL.removeprefix("a Robinhood sale's ")
    assert f"{proven_gross} {EVENT_REFERENCES}0:2" in output
    assert_no_markers(output, "XQCA", "99972B101", "2025-06-10")
    assert not (tmp_path / "out" / "schedule-cg.xlsx").exists()
    assert ftc_writes == []


# The issue's example (#1383): one XQAA share bought on 2025-05-01 for $800.00
# and sold on 2025-06-16 for an Amount of $1,000.00. Through the real
# Robinhood CG compute, at its flat ₹85 TTBR, it gains ₹17,000.
_XQAA_SALE = (date(2025, 5, 1), date(2025, 6, 16), "800.00", "1000.00")
# The same synthetic security from a Cayman Islands (KY) issuer, for Schedule
# FSI. Its ISIN carries the XQAA CUSIP, a private-use issuer.
_KY_XQAA = robinhood_security(
    symbol=_XQAA.instrument.symbol,
    cusip=_XQAA.cusip,
    isin=f"KY{_XQAA.cusip}{isin_check_digit(f'KY{_XQAA.cusip}')}",
    issuer_country_code="KY",
)
# The classification advisory that starts a section sheet's tax position
# note when the run has one NASDAQ lot.
_ONE_NASDAQ_LOT_ADVISORY = (
    "Tax position: 1 foreign exchange-listed securities on NASDAQ are reported as assets "
    "other than unquoted shares. Section 50CA prescribed FMV is not computed. No CBDT "
    "guidance or binding ruling directly resolves this foreign-exchange classification."
)


def _basis_by_field(path: Path, sheet_name: str) -> dict[str, str]:
    """Each row's portal field and Basis cell, as :func:`_values_by_field` reads amounts."""
    workbook = load_workbook(path, data_only=True)
    worksheet = workbook[sheet_name]
    basis = {
        str(worksheet.cell(row, 1).value): str(worksheet.cell(row, 5).value)
        for row in range(6, worksheet.max_row + 1)
    }
    workbook.close()
    return basis


@pytest.mark.parametrize("debug", [False, True], ids=["default", "debug"])
@pytest.mark.parametrize(
    ("itr_form", "section", "result"), [("ITR-2", "A5", "A5(e)"), ("ITR-3", "A6", "A6(g)")]
)
def test_a_robinhood_sale_files_its_amount_as_consideration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    itr_form: str,
    section: str,
    result: str,
    debug: bool,
) -> None:
    """#1383: the issue's XQAA sale through the real Robinhood CG compute and
    workbook writer. (a)(ii) is ₹85,000, the HALF_UP of $1,000.00 at the ₹85
    transfer-month TTBR, (b)(iii) is 0, and the balance and result are that
    less the ₹68,000 cost. robinhood-cg exits 0. On a5246a9 the section was
    manual: those cells and the F3 bands were blank, the position note listed
    the sale, and the run exited 3."""
    _stub_cg_run(monkeypatch, lambda: _xqaa_cg(_XQAA_SALE))

    debug_flag = ("--debug",) if debug else ()
    assert _main("robinhood-cg", tmp_path, "--itr-form", itr_form, *debug_flag) == 0

    captured = capsys.readouterr()
    assert "Robinhood equity-cg: ok" in captured.out
    assert "Action:" not in captured.out
    out = tmp_path / "out"
    sheet = f"{itr_form} {section} STCG"
    sheets, scope_note, position_note, _text = _cg_notes(out / "schedule-cg.xlsx")
    assert sheets == [sheet]
    assert position_note == f"{_ONE_NASDAQ_LOT_ADVISORY} {CONSIDERATION_POSITION}"
    basis = _basis_by_field(out / "schedule-cg.xlsx", sheet)
    assert basis[f"{section}(a)(ii)"] == CONSIDERATION_BASIS
    values = _values_by_field(out / "schedule-cg.xlsx", sheet)
    if not debug:
        # Zero editable fields and portal-calculated rows are hidden ...
        assert values == {f"{section}(a)(ii)": 85000, f"{section}(b)(i)": 68000, "F3(ii)": 17000}
        # ... and the scope note names (b)(iii) among the omitted zero fields.
        omitted = scope_note.split("Omitted zero Robinhood fields: ", 1)[1].split(". ", 1)[0]
        assert f"{section}(b)(iii)" in omitted.split(", ")
        return
    assert values == {
        f"{section}(a)(ii)": 85000,
        f"{section}(a)(iii)": 85000,
        f"{section}(b)(i)": 68000,
        f"{section}(b)(iii)": 0,
        f"{section}(b)(iv)": 68000,
        f"{section}(c)": 85000 - 68000,
        f"{section}(d)": 0,
        result: 85000 - 68000,
        "F3(i)": 0,
        "F3(ii)": 17000,
        "F3(iii)": 0,
        "F3(iv)": 0,
        "F3(v)": 0,
    }
    assert basis[f"{section}(b)(iii)"] == TRANSFER_EXPENDITURE_BASIS
    # The debug review CSV's portal-labelled row files the section's total too.
    *_rows, total = _review_rows(out / "schedule-cg-debug.csv")
    assert total[1:-1] == [""] * 14
    assert Decimal(total[-1]) == 17000


def test_robinhood_cg_without_in_fy_sale_stays_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1383 control: an XQAA sale on 2025-03-14, before FY 2025-26, writes
    the same summary workbook as a5246a9, where this passes too."""
    _stub_cg_run(
        monkeypatch,
        lambda: _xqaa_cg((date(2025, 1, 2), date(2025, 3, 14), "800.00", "1000.00")),
    )

    assert _main("robinhood-cg", tmp_path) == 0

    assert _left(tmp_path) == ["schedule-cg.xlsx"]
    sheets, scope_note, position_note, _text = _cg_notes(tmp_path / "out" / "schedule-cg.xlsx")
    assert sheets == ["ITR-2 Schedule CG"]
    assert scope_note == (
        f"{_CG_INSTRUCTION}No Robinhood entry is required in A5 or B8. Preserve any values "
        "from other sources."
    )
    assert position_note == (
        "No Robinhood capital-gain disposals were attributed to this financial year."
    )


def test_robinhood_cg_prints_its_outputs_as_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A relative ``--output-dir`` prints as typed in each ``Wrote:`` line,
    never resolved against the working folder (#1071)."""
    _stub_cg_run(monkeypatch, cg_result)
    monkeypatch.chdir(tmp_path)

    exit_code = cli.main(
        [
            "robinhood-cg",
            "--input",
            "bundle",
            "--config",
            "user.toml",
            "--output-dir",
            "out",
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
            "--debug",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert [line for line in captured.out.splitlines() if "Wrote:" in line] == [
        "  Wrote: out/schedule-cg.xlsx",
        "  Wrote: out/schedule-cg-debug.csv",
        "  Wrote: out/schedule-cg-ltcg-debug.csv",
    ]
    assert str(tmp_path) not in captured.out + captured.err


def test_a_robinhood_sale_leaves_every_robinhood_all_leg_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1383: robinhood-all on :func:`test_robinhood_income_files_fsi_for_a_robinhood_sale_gain`'s
    run exits 0: equity-cg is ok, and income-ftc writes its schedules through
    the real income compute and writer with no error, FSI with the KY sale's
    Capital Gains row. On a5246a9 the equity-cg leg was manual and income-ftc
    withheld Schedule FSI for the sale's KY gain, so the run failed (exit 4)."""
    _stub_ky_sale_income_run(tmp_path, monkeypatch)

    assert _main("robinhood-all", tmp_path) == 0

    stdout = capsys.readouterr().out
    statuses = {name: status for status, name in _LEG_LINE.findall(stdout)}
    assert statuses["equity-cg"] == statuses["income-ftc"] == "ok"
    assert "Action:" not in stdout
    _assert_ky_sale_fsi_row(tmp_path)


# ---- #1322: an option capital gain needs a manual Schedule CG entry ----

_OPTION_CG_REASON = (
    "Robinhood option capital gains need manual Schedule CG entry from "
    f"{outputs.OPTIONS_FILENAMES[0]}; schedule-cg.xlsx names their sections"
)
_CG_INSTRUCTION = (
    "IMPORTANT: ADD Robinhood input amounts to any existing Schedule CG values from other "
    "sources; do not replace existing totals. "
)
# Bought on May 1, 2023 and sold on June 2, 2025: over 24 months, so long-term.
_LONG_TERM_TRADES = (
    _option_trade(_BTO, 2, date(2023, 5, 1), "-615243.00"),
    _option_trade(_STC, 3, date(2025, 6, 2), "1615243.00"),
)


def _cg_notes(path: Path) -> tuple[list[str], str, str, str]:
    """The workbook's sheet names, its first sheet's scope (A1) and tax position
    (A2) notes, and the text of every cell."""
    workbook = load_workbook(path)
    try:
        worksheet = workbook.worksheets[0]
        text = "\n".join(
            str(cell.value)
            for sheet in workbook.worksheets
            for row in sheet.iter_rows()
            for cell in row
            if cell.value is not None
        )
        return workbook.sheetnames, str(worksheet["A1"].value), str(worksheet["A2"].value), text
    finally:
        workbook.close()


# Each case: the ITR form, the trades, the section still needing no entry and
# the section the option gain needs entered in.
_OPTION_CG_CASES = {
    "itr2-stcg": ("ITR-2", _MAY_JUNE_2025_TRADES, "B8", "A5"),
    "itr2-ltcg": ("ITR-2", _LONG_TERM_TRADES, "A5", "B8"),
    "itr3-stcg": ("ITR-3", _MAY_JUNE_2025_TRADES, "B9", "A6"),
}


@pytest.mark.parametrize(
    ("itr_form", "trades", "no_entry", "manual"),
    _OPTION_CG_CASES.values(),
    ids=_OPTION_CG_CASES.keys(),
)
def test_option_capital_gain_makes_robinhood_cg_manual_and_names_its_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    itr_form: str,
    trades: tuple[RobinhoodEvent, ...],
    no_entry: str,
    manual: str,
) -> None:
    """#1322: one capital-treated option gain and no equity sale, under the
    real option compute. On aa7ed98 robinhood-cg exited 0 and its workbook
    said no Robinhood entry was required in either section."""
    _option_cg_run(tmp_path, monkeypatch, RobinhoodOptionTreatment.CAPITAL_GAINS, trades)

    assert _main("robinhood-cg", tmp_path, "--itr-form", itr_form) == 3

    captured = capsys.readouterr()
    assert "Robinhood equity-cg: manual" in captured.out
    assert f"Action: {_OPTION_CG_REASON}" in captured.out
    sheets, scope_note, position_note, text = _cg_notes(tmp_path / "out" / "schedule-cg.xlsx")
    assert sheets == [f"{itr_form} Schedule CG"]
    assert scope_note == (
        f"{_CG_INSTRUCTION}No Robinhood entry is required in {no_entry}. Manual entry in "
        f"{manual}: Robinhood option capital gains are not in this workbook; enter them by "
        f"hand from {outputs.OPTIONS_FILENAMES[0]}, which robinhood-options and robinhood-all "
        "write. Preserve any values from other sources."
    )
    assert position_note == "No Robinhood equity disposals were attributed to this financial year."
    assert_no_markers("\n".join((text, captured.out, captured.err)), *_OPTION_MARKERS)


def test_equity_stcg_sheet_names_an_option_only_ltcg_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1322: an equity STCG loss beside a long-term option gain. On
    aa7ed98 the STCG sheet said no long-term (B8) Robinhood entry was
    required, and robinhood-cg exited 0."""
    _option_cg_run(tmp_path, monkeypatch, RobinhoodOptionTreatment.CAPITAL_GAINS, _LONG_TERM_TRADES)
    monkeypatch.setattr(pipeline, "_compute_cg", lambda *_args: cg_result(sale("12", "10")))

    assert _main("robinhood-cg", tmp_path) == 3

    captured = capsys.readouterr()
    assert f"Action: {_OPTION_CG_REASON}" in captured.out
    sheets, scope_note, _position_note, text = _cg_notes(tmp_path / "out" / "schedule-cg.xlsx")
    assert sheets == ["ITR-2 A5 STCG"]
    assert (
        "Preserve any values from other sources. Manual entry in B8: Robinhood option capital "
        f"gains are not in this workbook; enter them by hand from {outputs.OPTIONS_FILENAMES[0]}, "
        "which robinhood-options and robinhood-all write."
    ) in scope_note
    assert "No long-term (B8)" not in scope_note
    assert_no_markers("\n".join((text, captured.out, captured.err)), *_OPTION_MARKERS)


def test_option_capital_gain_makes_the_all_mode_cg_leg_manual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1322: robinhood-all's equity-cg leg reads the same option lines."""
    _stub_cg_run(monkeypatch, cg_result)
    _option_cg_run(
        tmp_path, monkeypatch, RobinhoodOptionTreatment.CAPITAL_GAINS, _MAY_JUNE_2025_TRADES
    )

    report = run_robinhood_all(**_run_kwargs(tmp_path), fetch_prices=False)

    leg = next(item for item in report.legs if item.name == "equity-cg")
    assert leg.status is RobinhoodLegStatus.MANUAL
    assert leg.reasons == (_OPTION_CG_REASON,)
    _sheets, scope_note, _position_note, _text = _cg_notes(tmp_path / "schedule-cg.xlsx")
    assert "Manual entry in A5: Robinhood option capital gains" in scope_note
    assert "required in A5" not in scope_note


def test_all_mode_cg_leg_fails_without_the_option_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1322: with no option result the workbook can't say which sections
    hold option capital gains, so it isn't written. The run already fails
    on the options leg."""
    _stub_cg_run(monkeypatch, cg_result)
    monkeypatch.setattr(
        pipeline,
        "_compute_options",
        lambda *_args: (_ for _ in ()).throw(RobinhoodPipelineError("missing treatment")),
    )

    report = run_robinhood_all(**_run_kwargs(tmp_path), fetch_prices=False)

    legs = {item.name: item for item in report.legs}
    assert legs["equity-cg"].status is RobinhoodLegStatus.ERROR
    assert legs["equity-cg"].reasons == (
        "the option computation failed, so schedule-cg.xlsx can't say which sections "
        "need option capital gains entered by hand",
    )
    assert legs["options"].status is RobinhoodLegStatus.ERROR
    assert not (tmp_path / "schedule-cg.xlsx").exists()


# Each control: the option treatment and trades, which leave today's workbook.
_OPTION_CG_CONTROLS = {
    "business-options": (_SPECULATIVE, _MAY_JUNE_2025_TRADES),
    "no-option-trades": (RobinhoodOptionTreatment.CAPITAL_GAINS, ()),
}


@pytest.mark.parametrize(
    ("treatment", "trades"), _OPTION_CG_CONTROLS.values(), ids=_OPTION_CG_CONTROLS.keys()
)
def test_robinhood_cg_without_option_capital_gains_keeps_its_workbook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    treatment: RobinhoodOptionTreatment,
    trades: tuple[RobinhoodEvent, ...],
) -> None:
    """#1322 controls: no option capital gain, so the status and both notes
    are today's."""
    _option_cg_run(tmp_path, monkeypatch, treatment, trades)

    assert _main("robinhood-cg", tmp_path, "--itr-form", "ITR-3") == 0

    assert "Robinhood equity-cg: ok" in capsys.readouterr().out
    _sheets, scope_note, position_note, _text = _cg_notes(tmp_path / "out" / "schedule-cg.xlsx")
    assert scope_note == (
        f"{_CG_INSTRUCTION}No Robinhood entry is required in A6 or B9. Preserve any values "
        "from other sources."
    )
    assert position_note == (
        "No Robinhood capital-gain disposals were attributed to this financial year."
    )


def test_option_blockers_without_capital_gains_keep_the_cg_leg_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1322 control: a hard option blocker alone keeps robinhood-cg's one reason."""
    kind = RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT
    options = _blocked_options(kind)
    options.capital_gain_lines = ()
    _stub_blocked_run(monkeypatch, options)

    report = run_robinhood_cg(**_run_kwargs(tmp_path), fetch_prices=False)

    assert report.status is RobinhoodLegStatus.MANUAL
    assert report.reasons == (f"option blockers require separate resolution: {_LOCATED[kind][1]}",)


def _fsi_rows(path: Path) -> list[dict[str, str]]:
    """``schedule-fsi.csv``'s rows, each from its Country Code line to the next."""
    rows: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, value = line.split(",", 1)
        if key == "Country Code":
            rows.append({})
        if key:
            rows[-1][key] = value
    return rows


def _stub_ky_sale_income_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#1084's US dividend run through the real income compute and writers,
    beside the issue's sale from a synthetic Cayman Islands (KY) issuer, a
    ₹17,000 STCG (#1383)."""
    _stub_relief_run(tmp_path, monkeypatch, "US")
    monkeypatch.setattr(
        pipeline, "_compute_cg", lambda *_args: _xqaa_cg(_XQAA_SALE, security=_KY_XQAA)
    )
    monkeypatch.setattr(pipeline, "_compute_os", core.compute_os)
    monkeypatch.setattr(pipeline, "_write_os", outputs.write_os)


def _assert_ky_sale_fsi_row(tmp_path: Path) -> None:
    """``schedule-fsi.csv`` files the KY sale's ₹17,000 gain, with no tax paid
    outside India, as its one Capital Gains row."""
    [gains] = (
        row
        for row in _fsi_rows(tmp_path / "out" / "schedule-fsi.csv")
        if row["Head of Income"] == "Capital Gains"
    )
    assert gains["Country Code"] == "1345-CAYMAN ISLANDS"
    assert gains["Income from outside India (included in PART B-TI)"] == "17000"
    assert gains["Tax paid outside India"] == "0"


def test_robinhood_income_files_fsi_for_a_robinhood_sale_gain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1383: #1084's US dividend run, through the real income compute, beside
    the issue's sale from a synthetic Cayman Islands (KY) issuer, a ₹17,000
    STCG. robinhood-income writes Schedule OS, FSI, TR and Form 67, FSI has a
    KY Capital Gains row of ₹17,000, and the run exits 0. On a5246a9 it
    withheld FSI for the KY gain: exit 4 and no files."""
    _stub_ky_sale_income_run(tmp_path, monkeypatch)

    assert _main("robinhood-income", tmp_path) == 0

    stdout = capsys.readouterr().out
    assert "Robinhood income-ftc: ok" in stdout
    assert "Action:" not in stdout
    assert _left(tmp_path) == [
        "form-67.csv",
        "schedule-fsi.csv",
        "schedule-os.xlsx",
        "schedule-tr.csv",
    ]
    _assert_ky_sale_fsi_row(tmp_path)


def _with_blocker(result: Any) -> Any:
    return replace(
        result,
        blockers=(
            RobinhoodCapitalGainsBlocker(
                kind=RobinhoodCapitalGainsBlockerKind.EVIDENCE_COVERAGE, account_index=0
            ),
        ),
    )


def _with_lot(result: Any, **changes: object) -> Any:
    return replace(
        result,
        attributed_lots=tuple(
            replace(item, closed_lot=replace(item.closed_lot, **changes))
            for item in result.attributed_lots
        ),
    )


# A blocker's reason can name an unreviewed exchange or class, so a blocked
# filing's refusal keeps only its kind's text and context key (#1046, #1252).
# The route line Robinhood adds for each reachable kind (#1555).
_UNREVIEWED_EXCHANGE_ROUTE = FILING_VENUE_ROUTE.format(sales="0:2")
# The ITR's refusal of a rounded consideration that is zero against a cost.
_INVALID_AMOUNTS = (
    "InvariantError: Schedule CG rounded consideration is negative, or zero against a "
    "claimed cost; the ITR refuses that row (context keys: schedule, consideration_inr, "
    "cost_of_acquisition_inr)"
)
# An unreviewed exchange or class the cases below feed in, as a whole token.
_UNREVIEWED_VALUE = re.compile(r"(?i)\b(OTC|PREFERRED)\b")


def _blocked_refusal(*kinds: str) -> str:
    """The exact refusal of a filing blocked on ``kinds``, in blocker order,
    each reachable kind's route line under them."""
    routes = {
        FILING_VENUE_KIND: f"\n{_UNREVIEWED_EXCHANGE_ROUTE}",
        FILING_LTCG_KIND: f"\n{FILING_LTCG_ROUTE}",
    }
    return (
        f"InvariantError: Schedule CG filing is blocked: {'; '.join(kinds)}"
        f"{''.join(routes.get(kind, '') for kind in kinds)} (context keys: reasons)"
    )


_PRECEDENCE_CASES = {
    "blocked": (lambda: _with_blocker(cg_result(sale("12", "10"))), "unresolved blockers"),
    "lot-count": (
        lambda: replace(
            cg_result(sale("12", "10")), attributed_lots=(sale("12", "10"), sale("12", "10"))
        ),
        "every closed lot in STCG or LTCG",
    ),
    "missing-exchange": (
        lambda: _with_lot(cg_result(sale("12", "10")), listing_exchange=None),
        "state its exchange and security class",
    ),
    "unreviewed-exchange": (
        lambda: _with_lot(cg_result(sale("12", "10")), listing_exchange="OTC"),
        _blocked_refusal(FILING_VENUE_KIND),
    ),
    "unreviewed-security-class": (
        lambda: _with_lot(cg_result(sale("12", "10")), sub_category="PREFERRED"),
        _blocked_refusal(FILING_CLASS_KIND),
    ),
    "positive-ltcg": (
        lambda: cg_result(sale("10", "12", long_term=True)),
        _blocked_refusal(FILING_LTCG_KIND),
    ),
    # #1383 control: the issue's sale, held from 2023 and so long-term,
    # through the real compute still refuses for section 54F.
    "real-compute-positive-ltcg": (
        lambda: _xqaa_cg((date(2023, 1, 16), *_XQAA_SALE[1:])),
        _blocked_refusal(FILING_LTCG_KIND),
    ),
    "unreviewed-exchange-with-invalid-amounts": (
        lambda: _with_lot(cg_result(sale("12", "0")), listing_exchange="OTC"),
        _blocked_refusal(FILING_VENUE_KIND),
    ),
    "unreviewed-class-with-invalid-amounts": (
        lambda: _with_lot(cg_result(sale("12", "0")), sub_category="PREFERRED"),
        _blocked_refusal(FILING_CLASS_KIND),
    ),
    "positive-ltcg-with-invalid-stcg-amounts": (
        lambda: cg_result(
            sale("12", "0"), sale("10", "12", long_term=True, lot_ordinal=3, sale_ordinal=4)
        ),
        _blocked_refusal(FILING_LTCG_KIND),
    ),
    # Positive STCG no longer blocks the filing (#1360), so the LTCG amounts'
    # own refusal shows.
    "positive-stcg-with-invalid-ltcg-amounts": (
        lambda: cg_result(
            sale("10", "12"), sale("12", "0", long_term=True, lot_ordinal=3, sale_ordinal=4)
        ),
        _INVALID_AMOUNTS,
    ),
}


@pytest.mark.parametrize(
    ("make_cg", "reason"), _PRECEDENCE_CASES.values(), ids=_PRECEDENCE_CASES.keys()
)
def test_existing_cg_refusals_keep_their_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    make_cg: Callable[[], Any],
    reason: str,
) -> None:
    _stub_cg_run(monkeypatch, make_cg)

    assert _main("robinhood-cg", tmp_path) == 4
    err = capsys.readouterr().err
    assert reason in err
    # No refusal prints an unreviewed exchange or class (#1047).
    assert _UNREVIEWED_VALUE.search(err) is None
    assert not (tmp_path / "out" / "schedule-cg.xlsx").exists()

    report = run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path / "all",
        financial_year=_FY,
        itr_form="ITR-2",
        fetch_prices=False,
    )
    equity_leg = next(leg for leg in report.legs if leg.name == "equity-cg")
    assert equity_leg.status is RobinhoodLegStatus.ERROR
    assert reason in equity_leg.reasons[0]
    assert _UNREVIEWED_VALUE.search(equity_leg.reasons[0]) is None


# ---- #1540: same-day sales whose order decides the short/long-term split ----

_SALE_ORDER_REFUSAL = f"{CG_UNRESOLVED}\n{_SALE_ORDER_REASON}"


def test_same_day_sale_order_over_both_terms_refuses_cg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1540: the issue's example through the real CG compute. robinhood-cg
    exits 4 naming the sales, the cause and the route, and writes nothing;
    robinhood-all's equity-cg leg fails with the same text. On d081de8 each
    lot got a $105 quantity share and robinhood-cg exited 0."""
    _assert_cg_refused(tmp_path, monkeypatch, capsys, _sale_order_case, _SALE_ORDER_REFUSAL)


def test_following_the_sale_order_refusal_files_the_issues_columns_by_hand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1540: follow the refusal word for word. It says schedule-cg.xlsx isn't
    written, and it isn't. Its references are the example's two sale rows and
    two buy rows. Matching those lots, oldest first, to the sales in each
    execution order a trade confirmation can give files the issue's two
    columns: STCG ₹7,650 and LTCG -₹1,700 when the $100 sale ran first, and
    ₹6,800 and -₹850 when the $110 sale did."""
    _stub_cg_run(monkeypatch, _sale_order_case)
    assert _main("robinhood-cg", tmp_path) == 4
    refusal = capsys.readouterr().err

    assert "schedule-cg.xlsx isn't written while this stands" in refusal
    assert not (tmp_path / "out" / "schedule-cg.xlsx").exists()
    [(sales, lots)] = re.findall(r"sales ([\d:, ]+) \(lots ([\d:, ]+)\)", refusal)
    # _sale_order_case's trades are managed (account index 1) activity rows 3 to 6.
    rows = dict(
        zip(
            ("1:3", "1:4", "1:5", "1:6"),
            (_LONG_TERM_BUY, _SHORT_TERM_BUY, _SALE_AT_100, _SALE_AT_110),
            strict=True,
        )
    )
    sale_rows = [rows[reference] for reference in sales.split(", ")]
    lot_rows = sorted(rows[reference] for reference in lots.split(", "))
    assert sale_rows == [_SALE_AT_100, _SALE_AT_110]
    assert lot_rows == [_LONG_TERM_BUY, _SHORT_TERM_BUY]
    filed = set()
    for executed in itertools.permutations(sale_rows):
        # Every lot and sale is one share, so the n-th sale takes the n-th lot.
        by_term = {True: Decimal(), False: Decimal()}
        for (bought, _qty, _price, cost), (sold, _sale_qty, _sale_price, amount) in zip(
            lot_rows, executed, strict=True
        ):
            short_term = is_short_term_holding(bought, sold)
            by_term[short_term] += (Decimal(amount) + Decimal(cost)) * 85
        filed.add((by_term[True], by_term[False]))
    assert filed == {(Decimal(7650), Decimal(-1700)), (Decimal(6800), Decimal(-850))}


@pytest.mark.parametrize(
    ("trades", "values"),
    [
        pytest.param(
            (
                _LONG_TERM_BUY,
                _SHORT_TERM_BUY,
                (_SALE_DAY, "1.0000", "105.00", "105.00"),
                (_SALE_DAY, "1.0000", "105.00", "105.00"),
            ),
            {
                "ITR-2 A5 STCG": {"A5(a)(ii)": 8925, "A5(b)(i)": 1700, "F3(i)": 7225},
                # A long-term loss has no Table F band.
                "ITR-2 B8 LTCG": {"B8(a)(ii)": 8925, "B8(b)(i)": 10200},
            },
            id="equal-cash-per-share",
        ),
        pytest.param(
            (
                (date(2024, 1, 3), "1.0000", "120.00", "-120.00"),
                _SHORT_TERM_BUY,
                _SALE_AT_100,
                _SALE_AT_110,
            ),
            {"ITR-2 A5 STCG": {"A5(a)(ii)": 17850, "A5(b)(i)": 11900, "F3(i)": 5950}},
            id="both-short-term",
        ),
    ],
)
def test_same_day_sales_whose_order_cannot_move_a_term_still_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trades: tuple[tuple[date, str, str, str], ...],
    values: dict[str, dict[str, int]],
) -> None:
    """#1540 controls: the issue's example at equal cash per share, or with
    both lots short-term, writes the workbook and exits 0 with d081de8's
    filed values."""
    _stub_cg_run(monkeypatch, lambda: _sale_order_case(*trades))

    assert _main("robinhood-cg", tmp_path) == 0

    workbook = tmp_path / "out" / "schedule-cg.xlsx"
    assert {sheet: _values_by_field(workbook, sheet) for sheet in values} == values


# ---- #1360: positive STCG writes the workbook; Table F gaps are manual ----

_LOSS_ACROSS_BANDS_REASON = (
    "Schedule CG Table F row 3 needs its date bands entered by hand: a band nets a loss "
    "and two or more bands net gains; schedule-cg.xlsx leaves them blank"
)
_TABLE_F_CASES = {
    # Gains in bands (i) and (iv) beside a loss in band (iii).
    "loss-across-bands": (
        lambda: cg_result(
            sale("10", "13"),
            sale("12", "11", sale_date=date(2025, 10, 1), lot_ordinal=3, sale_ordinal=4),
            sale(
                "10",
                "11",
                sale_date=date(2026, 1, 15),
                lot_ordinal=5,
                sale_ordinal=6,
            ),
        ),
        _LOSS_ACROSS_BANDS_REASON,
    ),
}


@pytest.mark.parametrize(("make_cg", "reason"), _TABLE_F_CASES.values(), ids=_TABLE_F_CASES.keys())
def test_a_positive_stcg_with_blank_table_f_bands_is_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    make_cg: Callable[[], Any],
    reason: str,
) -> None:
    """#1360: robinhood-cg writes the workbook and exits 3, naming why its
    Table F bands are blank; robinhood-all's equity-cg leg is manual, not an
    error."""
    _stub_cg_run(monkeypatch, make_cg)

    assert _main("robinhood-cg", tmp_path) == 3

    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"  Action: {reason}\n" in captured.out
    assert (tmp_path / "out" / "schedule-cg.xlsx").exists()

    report = run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path / "all",
        financial_year=_FY,
        itr_form="ITR-2",
        fetch_prices=False,
    )
    equity_leg = next(leg for leg in report.legs if leg.name == "equity-cg")
    assert equity_leg.status is RobinhoodLegStatus.MANUAL
    assert equity_leg.reasons[0] == reason


@pytest.mark.parametrize(("itr_form", "result"), [("ITR-2", "A5(e)"), ("ITR-3", "A6(g)")])
def test_robinhood_stcg_gains_in_two_bands_split_table_f_row_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    itr_form: str,
    result: str,
) -> None:
    """#1383, #1360 rule (c): two XQAA sales through the real compute net gains
    in bands (ii) and (iii): a $1,000.00 Amount on an $800.00 cost on
    2025-06-16 (₹17,000 at ₹85) and $500.01 on $400.00 on 2025-10-01
    (₹8,500.85). The result, ₹127,501 less ₹102,000, is ₹25,501, so the bands
    split it as ₹17,000 and ₹8,501, and robinhood-cg exits 0. On a5246a9 the
    section was manual and its bands blank (exit 3)."""
    _stub_cg_run(
        monkeypatch,
        lambda: _xqaa_cg(_XQAA_SALE, (date(2025, 5, 1), date(2025, 10, 1), "400.00", "500.01")),
    )

    assert _main("robinhood-cg", tmp_path, "--itr-form", itr_form, "--debug") == 0

    assert "Action:" not in capsys.readouterr().out
    section = result[:2]
    values = _values_by_field(tmp_path / "out" / "schedule-cg.xlsx", f"{itr_form} {section} STCG")
    assert (values[f"{section}(a)(ii)"], values[f"{section}(b)(i)"]) == (127501, 102000)
    bands = [values[f"F3({column})"] for column in ("i", "ii", "iii", "iv", "v")]
    assert bands == [0, 17000, 8501, 0, 0]
    assert sum(bands) == values[result] == 127501 - 102000


def test_robinhood_all_with_a_sale_and_an_option_gain_is_manual_only_for_the_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1383: robinhood-all with the same KY sale and a 2025 option capital
    gain is manual (exit 3) only for the option's existing reasons, and the
    real income writer still files the sale's KY Capital Gains FSI row. On
    a5246a9 the equity-cg leg also named the sale, and income-ftc withheld
    Schedule FSI for its gain, so the run failed (exit 4)."""
    _stub_cg_run(monkeypatch, lambda: _xqaa_cg(_XQAA_SALE, security=_KY_XQAA))
    _option_cg_run(
        tmp_path, monkeypatch, RobinhoodOptionTreatment.CAPITAL_GAINS, _MAY_JUNE_2025_TRADES
    )
    monkeypatch.setattr(
        pipeline, "_compute_cg", lambda *_args: _xqaa_cg(_XQAA_SALE, security=_KY_XQAA)
    )

    # The option bundle holds no income the real income compute can read, so
    # the stub assembles FSI from the capital gains the run hands it, and TR
    # from that FSI; the real writer files them.
    def compute_income(_bundle: object, _fy: object, cg: Any, **_kwargs: object) -> tuple[Any, ...]:
        assembly = _assembly_for(cg)
        tr = compute_schedule_tr_from_fsi(assembly.fsi_result)
        return SimpleNamespace(blockers=()), assembly, tr

    monkeypatch.setattr(pipeline, "_compute_income", compute_income)
    monkeypatch.setattr(pipeline, "_write_income", outputs.write_income)

    assert _main("robinhood-all", tmp_path) == 3

    _assert_ky_sale_fsi_row(tmp_path)
    report = run_robinhood_all(**_run_kwargs(tmp_path), fetch_prices=False)
    manual = {
        leg.name: leg.reasons for leg in report.legs if leg.status is RobinhoodLegStatus.MANUAL
    }
    assert manual == {
        "equity-cg": (_OPTION_CG_REASON,),
        "income-ftc": ("remaining option items require manual Schedule FSI completion",),
        "options": ("manual option schedules are required",),
    }
    assert not any(leg.status is RobinhoodLegStatus.ERROR for leg in report.legs)


# --- A standalone run that fails after writing leaves no output (#745) --------

_STANDALONE = {
    "robinhood-cg": (run_robinhood_cg, ("schedule-cg.xlsx",)),
    "robinhood-income": (
        run_robinhood_income,
        ("form-67.csv", "schedule-fsi.csv", "schedule-os.xlsx", "schedule-tr.csv"),
    ),
    "robinhood-fa": (run_robinhood_fa, ("schedule-fa-a2.csv", "schedule-fa-a3.csv")),
    "robinhood-options": (run_robinhood_options, ("robinhood-options.csv",)),
}
# Every (command, output it writes) pair.
_STANDALONE_WRITTEN = [
    (command, filename) for command, (_run, written) in _STANDALONE.items() for filename in written
]


def _run_standalone(command: str, tmp_path: Path) -> object:
    return _STANDALONE[command][0](
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path / "out",
        financial_year=_FY,
        itr_form="ITR-2",
    )


def _report_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
) -> list[list[str]]:
    """Make the leg report raise ``error``. Returns the outputs present at each
    attempt, which shows the writes ran first."""
    attempts: list[list[str]] = []

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        attempts.append(_left(tmp_path))
        raise error

    monkeypatch.setattr(pipeline, "RobinhoodLegReport", fail)
    return attempts


@pytest.mark.parametrize("layer", ["cli", "entry"])
@pytest.mark.parametrize("command", _STANDALONE)
def test_standalone_refusal_after_the_write_leaves_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    layer: str,
) -> None:
    """The report is built outside the F&O warning block, so its error is not
    wrapped even though these runs have option trades."""
    _stub_writing_all(monkeypatch, tmp_path / "out")
    error = InvariantError(what="report refused after the write", context=())
    attempts = _report_fails(monkeypatch, tmp_path, error)

    if layer == "cli":
        assert _main(command, tmp_path) == 4
        assert error.what in capsys.readouterr().err
    else:
        with pytest.raises(InvariantError) as raised:
            _run_standalone(command, tmp_path)
        assert raised.value is error

    assert attempts == [list(_STANDALONE[command][1])]
    assert _left(tmp_path) == []


@pytest.mark.parametrize("layer", ["cli", "entry"])
@pytest.mark.parametrize(("command", "filename"), _STANDALONE_WRITTEN)
def test_standalone_refusal_after_the_write_whose_cleanup_fails_names_the_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    filename: str,
    layer: str,
) -> None:
    _stub_writing_all(monkeypatch, tmp_path / "out")
    error = InvariantError(what="report refused after the write", context=())
    attempts = _report_fails(monkeypatch, tmp_path, error)
    failing = tmp_path / "out" / filename
    # Only the cleanup after the refusal cannot remove the output.
    _fail_unlink(monkeypatch, failing, attempts)

    if layer == "cli":
        assert _main(command, tmp_path) == 4
        message = capsys.readouterr().err
    else:
        # Catch the base's frozen error too, so pytest cannot mask it with TypeError.
        with pytest.raises(IbkrFaError) as raised:
            _run_standalone(command, tmp_path)
        assert isinstance(raised.value, OutputRemovalError)
        assert_replaced_unchained(raised.value, markers=(error.what,))
        message = str(raised.value)

    assert "may still be there" in message
    assert str(failing) in message
    assert _left(tmp_path) == [filename]


def test_standalone_unexpected_failure_after_the_write_leaves_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bug still fails the run with exit 1, naming its class and source line
    but not its message (#1049), only once the run's output is gone."""
    _stub_writing_all(monkeypatch, tmp_path / "out")
    _report_fails(monkeypatch, tmp_path, RuntimeError("report bug"))

    assert _main("robinhood-cg", tmp_path) == 1
    assert re.fullmatch(internal_error_line(RuntimeError), capsys.readouterr().err)
    assert _left(tmp_path) == []


def test_standalone_income_writer_refusal_removes_schedule_os(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_writing_all(monkeypatch, tmp_path / "out")
    refusal = WriterContractError(violation="nul_byte", row_index=0, cell_index=0, trigger="")

    def refuse(*_args: object, output_dir: Path, **_kwargs: object) -> NoReturn:
        assert (output_dir / "schedule-os.xlsx").exists()
        raise refusal

    monkeypatch.setattr(pipeline, "_write_income", refuse)

    assert _main("robinhood-income", tmp_path) == 4
    assert str(refusal) in capsys.readouterr().err
    assert _left(tmp_path) == []


_BLOCKED_FTC = {
    "income-blockers": (SimpleNamespace(blockers=(_ISSUER_COUNTRY,)), _cg()),
    "cg-blockers": (SimpleNamespace(blockers=()), _cg(blockers=(object(),))),
}


@pytest.mark.parametrize("layer", ["cli", "entry"])
@pytest.mark.parametrize("blocked", _BLOCKED_FTC)
def test_blocked_ftc_schedule_os_that_cannot_be_removed_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    blocked: str,
    layer: str,
) -> None:
    """No report claims the output was removed: the run refuses, naming it."""
    _stub_writing_all(monkeypatch, tmp_path / "out")
    income, cg = _BLOCKED_FTC[blocked]
    monkeypatch.setattr(pipeline, "_compute_cg", lambda *_args: cg)
    monkeypatch.setattr(pipeline, "_compute_income", lambda *_args, **_kwargs: (income, None, None))
    schedule_os = tmp_path / "out" / "schedule-os.xlsx"
    written: list[Path] = []

    def write_os(*_args: object, output_dir: Path, **_kwargs: object) -> tuple[Path, ...]:
        schedule_os.write_text("complete", encoding="utf-8")
        written.append(schedule_os)
        return (schedule_os,)

    monkeypatch.setattr(pipeline, "_write_os", write_os)
    _fail_unlink(monkeypatch, schedule_os, written)

    if layer == "cli":
        assert _main("robinhood-income", tmp_path) == 4
        captured = capsys.readouterr()
        assert "Robinhood income-ftc" not in captured.out
        message = captured.err
    else:
        with pytest.raises(OutputRemovalError) as raised:
            _run_standalone("robinhood-income", tmp_path)
        message = str(raised.value)

    assert "may still be there" in message
    assert str(schedule_os) in message
    assert _left(tmp_path) == ["schedule-os.xlsx"]


def test_standalone_cg_debug_run_removes_its_audits_when_filing_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Owner decision (a) (#745): the refusal carries the reason; no audit of a
    refused filing is left."""
    _stub_cg_run(monkeypatch, lambda: cg_result(sale("10", "11")))

    class RefusingFilingWriter(StubWriter):
        def write(self, _result: object, _path: Path) -> NoReturn:
            assert sorted(path.name for path in (tmp_path / "out").iterdir()) == [
                "schedule-cg-debug.csv",
                "schedule-cg-ltcg-debug.csv",
            ]
            raise RobinhoodPipelineError("filing position unresolved")

    patch_writers(monkeypatch, Subcommand.CG_FILING, RefusingFilingWriter)

    assert _main("robinhood-cg", tmp_path, "--debug") == 4
    assert "filing position unresolved" in capsys.readouterr().err
    assert _left(tmp_path) == []


@pytest.mark.parametrize("layer", ["cli", "entry"])
def test_standalone_fa_json_failure_after_publishing_removes_the_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layer: str,
) -> None:
    """The fragment is published, then building the leg report fails."""
    out = tmp_path / "out"
    written = _stub_writing_all(monkeypatch, out)
    monkeypatch.setattr(pipeline, "_write_fa", outputs.write_fa)
    fragment = out / "schedule-fa.json"
    failure = InvariantError(what="report refused after the write", context=())
    attempts = _report_fails(monkeypatch, tmp_path, failure)

    if layer == "cli":
        assert _main("robinhood-fa", tmp_path, "--format", "cbdt-json-fragment") == 4
    else:
        with pytest.raises(InvariantError) as raised:
            run_robinhood_fa(
                input_dir=tmp_path / "bundle",
                config_path=tmp_path / "user.toml",
                output_dir=out,
                financial_year=_FY,
                itr_form="ITR-2",
                format_id="cbdt-json-fragment",
            )
        assert raised.value is failure

    assert written == [fragment]
    assert attempts == [[fragment.name]]
    assert _left(tmp_path) == []


def test_standalone_fa_late_failure_keeps_a_foreign_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    foreign = out / "schedule-fa.json"
    foreign.write_bytes(b'{"ScheduleFA": {"hand-merged": true}}')
    _stub_writing_all(monkeypatch, out)
    error = InvariantError(what="report refused after the write", context=())
    _report_fails(monkeypatch, tmp_path, error)

    with pytest.raises(InvariantError):
        _run_standalone("robinhood-fa", tmp_path)

    assert _left(tmp_path) == ["schedule-fa.json"]
    assert foreign.read_bytes() == b'{"ScheduleFA": {"hand-merged": true}}'


def _helper_removal_failure(path: Path) -> OutputRemovalError:
    return OutputRemovalError(
        f"the run failed, and its output may still be there: could not remove {path}: denied"
    )


@pytest.mark.parametrize("leg", ["schedule-os", "equity-cg", "schedule-fa-json"])
def test_all_helper_removal_failure_raises_with_the_completed_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leg: str,
) -> None:
    """The helper could not remove its output once; the leg cleanup and the
    run-level rollback then remove everything, but the failure is still raised."""
    out = tmp_path / "out"
    written = _stub_writing_all(monkeypatch, out)
    name = {
        "schedule-os": "schedule-os.xlsx",
        "equity-cg": "schedule-cg.xlsx",
        "schedule-fa-json": "schedule-fa.json",
    }[leg]
    helper_error = _helper_removal_failure(out / name)

    def write_then_fail(*_args: object, **kwargs: object) -> NoReturn:
        (out / name).write_text(leg, encoding="utf-8")
        written.append(out / name)
        if leg == "schedule-fa-json":
            # The JSON is the run's while it is the file the run claimed.
            _claim(cast("ClaimedOutputCleanup", kwargs["json_cleanup"]).claim, out / name)
        raise helper_error

    if leg == "schedule-os":
        monkeypatch.setattr(pipeline, "_write_os", write_then_fail)
    elif leg == "equity-cg":
        monkeypatch.setattr(pipeline, "_write_cg", write_then_fail)
    else:
        # The automatic JSON calls the helper itself, not the pipeline's alias.
        monkeypatch.setattr(outputs, "write_fa", write_then_fail)

    with pytest.raises(pipeline.RobinhoodAllRemovalError) as raised:
        run_robinhood_all(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=out,
            financial_year=_FY,
            itr_form="ITR-2",
            fetch_prices=False,
        )

    assert str(raised.value) == (
        f"leg output cleanup failed: {helper_error}; run outputs were removed"
    )
    assert_replaced_unchained(raised.value)
    report = raised.value.report
    assert tuple(leg.name for leg in report.legs) == _ALL_LEGS
    assert not any(leg.outputs for leg in report.legs)
    assert out / name in written
    assert not any(path.exists() for path in written)


# Each case: the blocked filing and its exact refusal.
_FILING_BLOCKERS = {
    "unreviewed-exchange": (
        lambda: _with_lot(cg_result(sale("12", "10")), listing_exchange="OTC"),
        _blocked_refusal(FILING_VENUE_KIND),
    ),
    "positive-ltcg": (
        lambda: cg_result(sale("10", "12", long_term=True)),
        _blocked_refusal(FILING_LTCG_KIND),
    ),
}


@pytest.mark.parametrize(
    ("make_cg", "refusal"), _FILING_BLOCKERS.values(), ids=_FILING_BLOCKERS.keys()
)
def test_blocked_filing_leaves_no_robinhood_cg_debug_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    make_cg: Callable[[], Any],
    refusal: str,
) -> None:
    """#642: a blocker from the shared builder refuses and, by owner decision (a)
    on #745, leaves neither the workbook nor the debug audits."""
    _stub_cg_run(monkeypatch, make_cg)

    assert _main("robinhood-cg", tmp_path, "--debug") == 4

    err = capsys.readouterr().err
    assert err == f"error: {refusal}\n"
    assert _UNREVIEWED_VALUE.search(err) is None
    assert _left(tmp_path) == []


@pytest.mark.parametrize(
    ("make_cg", "refusal"), _FILING_BLOCKERS.values(), ids=_FILING_BLOCKERS.keys()
)
@pytest.mark.parametrize("debug", [False, True], ids=["default", "debug"])
def test_blocked_filing_fails_only_the_robinhood_all_cg_leg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    make_cg: Callable[[], Any],
    refusal: str,
    debug: bool,
) -> None:
    """The other legs still run; the failed run then removes every output."""
    ftc_writes = _stub_cg_run(monkeypatch, make_cg)

    assert _main("robinhood-all", tmp_path, *(("--debug",) if debug else ())) == 4

    stdout = capsys.readouterr().out
    statuses = {name: status for status, name in _LEG_LINE.findall(stdout)}
    assert tuple(statuses) == _ALL_LEGS
    assert {name for name, status in statuses.items() if status == "error"} == {"equity-cg"}
    assert f"-> {refusal}\n" in stdout
    assert _UNREVIEWED_VALUE.search(stdout) is None
    assert ftc_writes == ["income-ftc"]
    assert list((tmp_path / "out").iterdir()) == []


# ---- #773: structural CBDT JSON problems refuse (exit 4) ---------------------

_A3_PEAK_WORK = (RobinhoodFaManualWork(kind=RobinhoodFaManualKind.A3_PEAK, lot_ids=(_A3_LOT,)),)
# A blank A3 peak and the reason the engine records for it (#962).
_BLANK_A3_PEAK: dict[str, Any] = {
    "peak_value_inr": None,
    "peak_blank_reason": FaA3PeakBlankReason.EMPTY_WINDOW,
}
_FOREIGN_JSON = b'{"ScheduleFA": {"hand-merged": true}}'


@pytest.mark.parametrize(
    ("fa", "extra", "expected_exit", "message"),
    [
        (_json_fa(), (), 0, "Wrote: "),
        (
            _json_fa(a2_changes={"institution_zip": ""}),
            (),
            3,
            "Action: A2 ZipCode: supply the account's [[robinhood.accounts]] zip",
        ),
        (
            _json_fa(manual_work=_A3_PEAK_WORK, eligible=False, **_BLANK_A3_PEAK),
            (),
            3,
            "Action: a3-peak",
        ),
        (
            _json_fa(
                manual_work=_A3_PEAK_WORK,
                eligible=False,
                build=_schema_invalid_json_result,
                **_BLANK_A3_PEAK,
            ),
            (),
            4,
            "ZipCode",
        ),
        (
            _json_fa(manual_work=_A3_PEAK_WORK, eligible=False, **_BLANK_A3_PEAK),
            ("--financial-year", "2026-27"),
            4,
            "this run targets AY 2027-28",
        ),
        (
            _json_fa(second_account_verified=False),
            (),
            4,
            "A2 account 2: the account number was not cross-checked",
        ),
    ],
    ids=[
        "complete",
        "missing-account-zip",
        "not-eligible",
        "not-eligible-invalid-a3-zip",
        "not-eligible-unsupported-ay",
        "account-not-cross-checked",
    ],
)
def test_robinhood_fa_json_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fa: SimpleNamespace,
    extra: tuple[str, ...],
    expected_exit: int,
    message: str,
) -> None:
    _stub_successful_all(monkeypatch, fa)

    assert _main("robinhood-fa", tmp_path, "--format", "cbdt-json-fragment", *extra) == (
        expected_exit
    )

    captured = capsys.readouterr()
    assert message in captured.out + captured.err
    assert (tmp_path / "out" / "schedule-fa.json").exists() is (expected_exit == 0)


@pytest.mark.parametrize(
    ("fa", "extra", "message"),
    [
        (_json_fa(), (), "not generated by ibkr-fa"),
        (_json_fa(), ("--financial-year", "2026-27"), "this run targets AY 2027-28"),
        (_json_fa(build=_schema_invalid_json_result), (), "ZipCode"),
        (
            _json_fa(manual_work=_A3_PEAK_WORK, eligible=False, **_BLANK_A3_PEAK),
            (),
            "not generated by ibkr-fa",
        ),
    ],
    ids=["complete", "unsupported-ay", "invalid-a3-zip", "missing-a3-peak"],
)
def test_robinhood_fa_json_beside_a_foreign_fragment_exits_4_and_keeps_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fa: SimpleNamespace,
    extra: tuple[str, ...],
    message: str,
) -> None:
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    foreign = out / "schedule-fa.json"
    foreign.write_bytes(_FOREIGN_JSON)
    _stub_successful_all(monkeypatch, fa)

    assert _main("robinhood-fa", tmp_path, "--format", "cbdt-json-fragment", *extra) == 4

    assert message in capsys.readouterr().err
    assert _left(tmp_path) == ["schedule-fa.json"]
    assert foreign.read_bytes() == _FOREIGN_JSON


# ---- #783: colliding generated security identities refuse (exit 4) -----------

# Longer than an ISIN, so no CUSIP, symbol or ISIN can contain one.
_SENTINEL_ACCOUNTS = ("8675309000000001", "8675309000000002")
_PRIVATE_SOURCE = f"reviewed with account {_SENTINEL_ACCOUNTS[0]}"


def _identity_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, configured_symbol: str
) -> list[Path]:
    """Stub a run with a buy-only equity CUSIP (99992A109) and an option trade.

    user.toml configures ``configured_symbol`` for another CUSIP, so the real
    loader generates XQAQ from stubbed official sources. Every step after the
    loader is stubbed to succeed, with options drawn from the input events,
    so each command writes unless the loader refuses. Returns every path the
    leg writers write.
    """
    written = _stub_writing_all(monkeypatch, tmp_path / "out")
    monkeypatch.setattr(pipeline, "_load", core.load_bundle)
    _write_bundle(tmp_path / "bundle")
    (tmp_path / "user.toml").write_text(
        robinhood_toml(
            first=robinhood_account_toml(
                number=_SENTINEL_ACCOUNTS[0],
                opening_date="2025-01-01",
                source="synthetic individual statement",
            ),
            second=robinhood_account_toml(
                number=_SENTINEL_ACCOUNTS[1],
                opening_date="2025-10-01",
                source="synthetic managed statement",
            ),
            extra=(
                robinhood_report_toml(file="activity/one.csv")
                + robinhood_report_toml(file="activity/two.csv")
                + robinhood_security_toml(symbol=configured_symbol, source=_PRIVATE_SOURCE)
            ),
        ),
        encoding="utf-8",
    )
    _patch_sources(monkeypatch)
    buy = RobinhoodEvent(
        kind=RobinhoodEventKind.EQUITY_BUY,
        source_record_index=1,
        activity_date=date(2025, 5, 1),
        process_date=date(2025, 5, 1),
        settle_date=date(2025, 5, 2),
        instrument="XQAQ",
        description="Sample Issuer AQ Inc.\nCUSIP: 99992A109",
        trans_code="Buy",
        quantity=Decimal("1"),
        quantity_suffix=None,
        price_usd=Decimal("200"),
        amount_usd=Decimal("-200"),
        issuer_name="Sample Issuer AQ Inc.",
        cusip="99992A109",
    )
    option = _option(RobinhoodEventKind.OPTION_BUY_TO_OPEN, 2, date(2025, 5, 1), date(2025, 5, 2))
    paired = SimpleNamespace(
        accounts=tuple(
            SimpleNamespace(
                activity=SimpleNamespace(
                    primary_bundle=SimpleNamespace(statements=()), cusip_transition_uses=()
                ),
                evidence=SimpleNamespace(events=events),
            )
            for events in ((buy, option), ())
        ),
        cusip_transition_references=(),
        account_labels={0: "Individual", 1: "Managed Individual"},
    )
    monkeypatch.setattr(
        bundle_mod, "reconcile_robinhood_accounts", lambda *_args, **_kwargs: paired
    )

    def compute_options(bundle: Any, *_args: object) -> SimpleNamespace:
        result = _options(treatment=RobinhoodOptionTreatment.SPECULATIVE_BUSINESS)
        result.trades = tuple(
            event
            for account in bundle.paired.accounts
            for event in account.evidence.events
            if event.option_symbol is not None
        )
        return result

    monkeypatch.setattr(pipeline, "_compute_options", compute_options)
    monkeypatch.setattr(pipeline, "_reconstruct_options", compute_options)
    monkeypatch.setattr(
        bundle_mod,
        "reconcile_robinhood_security_quantities",
        lambda *_args: _bundle(has_options=True).quantities,
    )
    _issuer_aq, sources = _identity_sources()
    monkeypatch.setattr(
        bundle_mod,
        "load_identity_sources",
        lambda *_args, **_kwargs: (sources, IdentitySourceAudit("c" * 64, True, None, None)),
    )
    return written


@pytest.mark.parametrize("command", _OWNED)
def test_generated_identity_collision_refuses_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """Before #783 the loader dropped every generated identity and carried on,
    so each command wrote. Even unstubbed, CG and income would have, since a
    buy-only CUSIP blocks neither."""
    written = _identity_run(tmp_path, monkeypatch, configured_symbol="XQAQ")
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    for name in _OWNED[command]:
        (out / name).write_bytes(b"stale")
    kept = {
        out / "schedule-fa.json": _FOREIGN_JSON,
        out / "notes.txt": b"unrelated",
        tmp_path
        / "bundle"
        / ".ibkr-fa-price-cache"
        / "robinhood-identities"
        / ("c" * 64)
        / "sources.json": b"cached",
    }
    for path, data in kept.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    assert _main(command, tmp_path) == 4

    captured = capsys.readouterr()
    assert captured.err.startswith(
        "error: Robinhood security identities collide; every security needs its own "
        "CUSIP, ISIN and symbol:\n"
        "  same symbol: [[robinhood.securities]] entry 1 and generated identity 1 "
        "(from the automatic official identity snapshot "
    )
    output = captured.out + captured.err
    assert_no_markers(output, "XQAQ", "99972B101", "99992A109", "US99972B1017", "US99992A1097")
    assert _PRIVATE_SOURCE not in output
    assert not any(number in output for number in _SENTINEL_ACCOUNTS)
    assert written == []
    assert {path: path.read_bytes() for path in kept} == kept
    assert _left(tmp_path) == ["notes.txt", "schedule-fa.json"]


@pytest.mark.parametrize("command", _OWNED)
def test_generated_identity_without_collision_still_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    written = _identity_run(tmp_path, monkeypatch, configured_symbol="XQCA")
    loaded: list[Any] = []
    for step in ("_compute_options", "_reconstruct_options"):
        compute = getattr(pipeline, step)
        monkeypatch.setattr(
            pipeline,
            step,
            lambda bundle, *args, compute=compute: loaded.append(bundle) or compute(bundle, *args),
        )

    assert _main(command, tmp_path) == 0

    (bundle,) = loaded
    assert [item.instrument.symbol for item in bundle.securities.configured] == ["XQCA"]
    assert [item.cusip for item in bundle.securities.generated] == ["99992A109"]
    assert written
    assert all(path.exists() for path in written)


# ---- #830: filed amounts past the CBDT 14-digit bound refuse (exit 4) --------

_PAST_THE_BOUND = (
    "must round to a whole-rupee amount within ±99,999,999,999,999 (the CBDT 14-digit bound)"
)


def _no_writer(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("a writer ran for a filing past the CBDT bound")


@pytest.mark.parametrize(
    ("bought", "schedule"),
    [(date(2025, 1, 15), "STCG"), (date(2023, 1, 15), "LTCG")],
    ids=["stcg", "ltcg"],
)
def test_robinhood_cg_consideration_past_the_cbdt_bound_refuses_before_any_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bought: date,
    schedule: str,
) -> None:
    """Two sales of USD 600,000,000,000 at ₹85 are each in range; their
    consideration and cost, ₹102,000,000,000,000 each, are not. An LTCG gain
    would block the filing workbook anyway (section 54F), so they have none."""
    price = "600000000000.00"
    events = (
        _trade(RobinhoodEventKind.EQUITY_BUY, 1, bought, "2", price, "-1200000000000.00"),
        _trade(RobinhoodEventKind.EQUITY_SELL, 2, date(2025, 6, 15), "1", price, price),
        _trade(RobinhoodEventKind.EQUITY_SELL, 3, date(2025, 6, 16), "1", price, price),
    )
    slices = tuple(
        _slice(1, (sale,), quantity="1", cost=price, original_date=bought) for sale in (2, 3)
    )
    _stub_cg_run(monkeypatch, lambda: _robinhood_cg_from_lots(events, _lot_result(slices)))
    monkeypatch.setattr(outputs, "write_output_bundle", _no_writer)
    monkeypatch.setattr(outputs, "output_binding", _no_writer)

    assert _main("robinhood-cg", tmp_path) == 4

    err = capsys.readouterr().err
    assert f"InvariantError: Schedule CG {schedule} consideration {_PAST_THE_BOUND}" in err
    assert _left(tmp_path) == []


def test_robinhood_options_bp_adjustments_past_the_cbdt_bound_refuse_before_any_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reviewed BP additions and deductions of ₹100,000,000,000,000 each cancel
    out in BP income, but the options CSV would file both (BP B40 and B41)."""
    paired = _option_paired(_closed_events())
    options = _robinhood_options(paired, RobinhoodOptionTreatment.SPECULATIVE_BUSINESS)
    amount = Decimal("100000000000000")
    reviewed = replace(
        options,
        classifications=(
            replace(options.classifications[0], bp_additions_inr=amount, bp_deductions_inr=amount),
        ),
    )
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: SimpleNamespace(paired=paired))
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: reviewed)
    monkeypatch.setattr(outputs, "write_robinhood_options_csv", _no_writer)

    assert _main("robinhood-options", tmp_path) == 4

    err = capsys.readouterr().err
    assert (
        f"InvariantError: RobinhoodOptionBusinessFiling.bp_additions_inr {_PAST_THE_BOUND}" in err
    )
    assert _left(tmp_path) == []


# ---- #909, #1007, #1009, #1019, #1022, #1023, #1026: a malformed statement table refuses
# every command (exit 4)


_SWEPT_TOTAL = next(
    line for line in _brokerage_statement().splitlines() if line.startswith("Total Swept Funds")
)


@pytest.mark.parametrize(
    ("old", "new", "refusal"),
    [
        pytest.param(
            "Dividends $1.00 $1.00",
            "Dividends $1.00* $1.00*",
            "column 'Dividends': Income and Expense Summary row is malformed",
            id="summary-row-marker",
        ),
        pytest.param(
            "Income and Expense Summary This Period Year to Date",
            "Income and Expense Summary This Period Year to Date\nUnexpected text",
            "row 1: Income and Expense Summary contains an unsupported row",
            id="text-under-the-income-heading",
        ),
        pytest.param(
            "Account Summary Opening Balance Closing Balance",
            "Account Summary Opening Balance Closing Balance   Hidden Row $5.00 $5.00",
            "row 1: table column header holds unsupported text",
            id="row-on-the-account-summary-heading-line",
        ),
        pytest.param(
            _SWEPT_TOTAL,
            f"{_SWEPT_TOTAL}\n{_SWEPT_TOTAL}",
            "column 'Total Swept Funds': Deposit Sweep Activity total is repeated",
            id="repeated-ledger-total",
        ),
        pytest.param(
            "Unlisted Continuation",
            "Internal Security Transfer from 222222222 to 111111111",
            "column 'Description': activity description holds more than one transfer identity",
            id="second-transfer-pair",
        ),
        pytest.param(
            "Unlisted Continuation",
            "Unlisted Continuation\nCUSIP: Hidden Holder Q",
            "row 3, column 'Description': activity row has a malformed CUSIP line",
            id="malformed-activity-cusip-line",
        ),
        pytest.param(
            "OPT 1/17/2025 Call $10.00",
            "OPT 1/17/2025 Call $10.00 XQGD 1/17/2025 Put $20.00",
            "column 'Description': position description holds more than one option contract",
            id="second-option-contract",
        ),
        pytest.param(
            "OPT 1/17/2025 Call $10.00",
            "XQGD 1/17/2025 Call $10.00",
            "column 'Description': position option contract symbol is not the row's Sym/Cusip",
            id="contract-symbol-not-its-cell",
        ),
        pytest.param(
            f"Deposit Sweep Activity\n{_LEDGER_HEADER}",
            f"Deposit Sweep Activity\n{_LEDGER_HEADER}   Balance",
            "column 'Balance': table column label is repeated",
            id="repeated-ledger-label",
        ),
        pytest.param(
            f"Deposit Sweep Activity\n{_LEDGER_HEADER}",
            f"Deposit Sweep Activity\n{_LEDGER_HEADER}   Notes",
            "row 6: table column header holds unsupported text",
            id="ledger-header-text",
        ),
        pytest.param(
            f"Account Activity\n{_activity_header()[0]}",
            f"Account Activity\nUnexpected text\n{_activity_header()[0]}",
            "row 3: Account Activity heading is not followed by its column header",
            id="text-between-heading-and-header",
        ),
        pytest.param(
            _BANK_ROW,
            f"{_BANK_ROW}\n{_NOT_MONEY}",
            "row 5: Program Bank row is incomplete or column-shifted",
            id="bank-row-without-an-amount",
        ),
        pytest.param(
            _BANK_ROW,
            _BANK_ROW.replace("$20.00", " 20.00"),
            "row 5, column 'Balance': Program Bank balance has no dollar sign",
            id="bank-balance-without-a-dollar-sign",
        ),
    ],
)
@pytest.mark.parametrize("command", _OWNED)
def test_malformed_statement_table_refuses_and_leaves_no_stale_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    old: str,
    new: str,
    refusal: str,
) -> None:
    """Every command loads the monthly statements, so a summary row carrying a
    footnote marker, text under the Income heading, a row on the Account
    Summary heading line, a ledger total printed
    twice, a second or misattributed
    identity in a description, a malformed CUSIP line under an activity row,
    a header holding a repeated label or other
    text, text between a table's heading and its header, or a Program Bank
    row without a dollar amount refuses each one, echoing none of the
    statement's text, and its stale outputs are removed."""
    assert _brokerage_statement().count(old) == 1
    text = _brokerage_statement().replace(old, new)
    _with_bundle_texts(monkeypatch, tmp_path, {b"brokerage": text})
    monkeypatch.setattr(bundle_mod, "read_robinhood_statement", read_robinhood_statement)
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    for name in _OWNED[command]:
        (out / name).write_bytes(b"stale")

    assert _main(command, tmp_path) == 4

    err = capsys.readouterr().err
    assert refusal in err
    assert "Unexpected" not in err
    assert "Hidden" not in err
    assert "NOT-MONEY" not in err
    assert "20.00" not in err
    assert "5.00" not in err
    assert _left(tmp_path) == []


# ---- #910: an unknown Stock Lending loan transaction refuses every command (exit 4) --


@pytest.mark.parametrize("command", _OWNED)
def test_unknown_stock_loan_transaction_refuses_and_leaves_no_stale_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """Every command loads the monthly statements, so a Loan Activity row whose
    Transaction code is unknown refuses each one and its stale outputs are removed."""
    text = _brokerage_statement().replace("SLJNL", "XXXXX")
    _with_bundle_texts(monkeypatch, tmp_path, {b"brokerage": text})
    monkeypatch.setattr(bundle_mod, "read_robinhood_statement", read_robinhood_statement)
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    for name in _OWNED[command]:
        (out / name).write_bytes(b"stale")

    assert _main(command, tmp_path) == 4

    assert (
        "column 'Transaction': Stock Lending loan transaction code is unsupported"
        in capsys.readouterr().err
    )
    assert _left(tmp_path) == []


# ---- #1099: a glued Form 1042-S UFI refuses every command that reads tax forms (exit 4)


@pytest.mark.parametrize("command", ["robinhood-income", "robinhood-fa", "robinhood-all"])
def test_glued_1042s_ufi_refuses_and_leaves_no_stale_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """These commands read the Form 1042-S packets, so a packet whose UFI
    digits are glued together refuses each one, echoing none of the UFI, and
    its stale outputs are removed."""
    text = _tax_packet()
    assert text.count("1 2 3 4 5 6 7 8 9 0") == 2
    text = text.replace("1 2 3 4 5 6 7 8 9 0", "1234567890")
    _with_bundle_texts(monkeypatch, tmp_path, {b"brokerage": _brokerage_statement()})
    (tmp_path / "bundle" / "tax-forms").mkdir()
    (tmp_path / "bundle" / "tax-forms" / "form.pdf").write_bytes(b"tax")
    monkeypatch.setattr(tax_form_parser, "pdftotext_layout", lambda _path, **_kwargs: text)
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    for name in _OWNED[command]:
        (out / name).write_bytes(b"stale")

    assert _main(command, tmp_path) == 4

    err = capsys.readouterr().err
    assert (
        "ParseError in section 'Robinhood Form 1042-S packet' at row 1: "
        "Form 1042-S UFI is malformed" in err
    )
    assert "1234567890" not in err
    assert _left(tmp_path) == []


# ---- exact sums (#1166) -------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "cost_usd", "proceeds_usd", "refusal"),
    [
        (
            "robinhood-cg",
            "0",
            "1E-997",
            "Schedule CG STCG, input row 3, column proceeds_inr",
        ),
        (
            "robinhood-income",
            "1E-997",
            "0",
            "Schedule FSI capital gains STCG net, input row 3, column realized_gain_inr",
        ),
    ],
)
@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_sum_past_the_exact_cap_refuses_and_clears_the_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    cost_usd: str,
    proceeds_usd: str,
    refusal: str,
    stale: bool,
) -> None:
    """#1166 (owner decision 2026-11-10: exact, never round): at ₹100/$, a
    ₹1E10 sale beside a ₹1E-995 one needs more digits than an exact sum may
    hold. robinhood-cg refuses at its consideration; robinhood-income at the
    FSI net, which sums the same gains."""
    _stub_cg_run(
        monkeypatch,
        lambda: cg_result(
            sale("0", "1"),
            sale("0", "1E+8", lot_ordinal=3, sale_ordinal=4),
            sale(cost_usd, proceeds_usd, lot_ordinal=5, sale_ordinal=6),
        ),
    )
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    if stale:
        for name in _OWNED[command]:
            (out / name).write_bytes(b"stale")

    assert _main(command, tmp_path) == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: InvariantError: {refusal}: {INEXACT_SUM}\n"
    assert captured.out == ""
    assert _left(tmp_path) == []
