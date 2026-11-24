from __future__ import annotations

from contextlib import suppress
from functools import partial
from pathlib import Path

import ibkr_fa.pipeline_robinhood_core as _core
import ibkr_fa.pipeline_robinhood_outputs as _outputs
import ibkr_fa.pipeline_robinhood_reporting as _reporting
from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.robinhood_capital_gains import robinhood_sale_price_checks
from ibkr_fa.compute.robinhood_options import (
    ROBINHOOD_FNO_FEMA_WARNING,
    option_blocker_summary,
)
from ibkr_fa.compute.schedule_fsi import require_fsi_write_ready
from ibkr_fa.exceptions import IbkrFaError, raise_replacing, relayed_failure
from ibkr_fa.output._cleanup import (
    ClaimedOutputCleanup,
    OutputRemovalError,
    RunOutputs,
    remove_failed_run_outputs,
    remove_outputs_on_failure,
)
from ibkr_fa.output.cbdt_json_fragment import (
    CbdtScheduleFaJsonWriter,
    preclear_own_cbdt_fragment,
)
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_robinhood_bundle import refuse_robinhood_input_collisions
from ibkr_fa.pipeline_robinhood_models import (
    NO_RUN_OUTPUTS,
    RobinhoodAllRemovalError,
    RobinhoodAllReport,
    RobinhoodLegReport,
    RobinhoodLegStatus,
)
from ibkr_fa.pipeline_robinhood_models import (
    error_leg as _error_leg,
)
from ibkr_fa.pipeline_robinhood_models import (
    scope_warnings as _scope_warnings,
)
from ibkr_fa.prices.yahoo import DailyCloseFetcher

_Computed = _core.ComputedRobinhood
RobinhoodPipelineError = _core.RobinhoodPipelineError
_compute_cg = _core.compute_cg
_compute_fa = _core.compute_fa
_compute_income = _core.compute_income
_compute_options = _core.compute_options
_compute_os = _core.compute_os
_compute_source_income = _core.compute_source_income
_reconstruct_options = _core.reconstruct_options
_annual_form_warnings = _core.annual_form_warnings
_fa_prices = _core.fa_prices
_fa_inventory = _core.fa_inventory
_fno_warning_on_error = _core.fno_warning_on_error
_hard_option_blockers, _income_warnings = _core.hard_option_blockers, _core.income_warnings
_load = _core.load_bundle
_sale_price_checks = robinhood_sale_price_checks
_validate_owned_events = _core.validate_owned_events
_with_receipt_identity_guidance = _core.with_receipt_identity_guidance

_clear_all = _outputs.clear_all
_clear_leg = _outputs.clear_leg
_write_cg = _outputs.write_cg
_write_fa = _outputs.write_fa
_write_income = _outputs.write_income
_write_options = _outputs.write_options
_write_os = _outputs.write_os
_GUIDANCE = _outputs.IDENTITY_GUIDANCE_FILENAME


def _refuse_input_collisions(
    output_dir: Path, filenames: tuple[str, ...], input_dir: Path, config_path: Path
) -> None:
    """Refuse before clearing ``filenames`` if one is a file this run reads."""
    refuse_robinhood_input_collisions(
        _outputs.paths(output_dir, filenames), input_dir=input_dir, config_path=config_path
    )


def run_robinhood_cg(
    *,
    input_dir: Path,
    config_path: Path,
    output_dir: Path,
    financial_year: FinancialYear,
    itr_form: ItrForm,
    format_id: str = "review-csv",
    debug: bool = False,
    fetch_prices: bool = True,
    price_cache_dir: Path | None = None,
) -> RobinhoodLegReport:
    _outputs.ensure_output_dir(output_dir)
    _refuse_input_collisions(
        output_dir, (*_outputs.CG_FILENAMES, _GUIDANCE), input_dir, config_path
    )
    _clear_leg(output_dir, (*_outputs.CG_FILENAMES, _GUIDANCE))
    bundle = _load(
        input_dir=input_dir,
        config_path=config_path,
        financial_year=financial_year,
        include_tax_forms=False,
        schedule_fa=False,
        fetch_identities=fetch_prices,
        identity_cache_dir=price_cache_dir,
    )
    options = _compute_options(bundle, financial_year, itr_form)
    targets = _outputs.paths(output_dir, _outputs.CG_FILENAMES)
    guidance = _outputs.IdentityGuidanceFile(output_dir)
    # If anything raises from the first write to the report, remove every CG
    # output. A refusal keeps the identity guidance file it wrote.
    with remove_outputs_on_failure(targets):
        with _fno_warning_on_error(bool(options.trades)):
            cg = _with_receipt_identity_guidance(
                lambda: _compute_cg(bundle, financial_year), bundle, guidance
            )
            _validate_owned_events(bundle, _Computed(options=options, cg=cg))
            if cg.blockers:
                raise RobinhoodPipelineError(
                    _reporting.reason_with_warnings(
                        _core.cg_blocker_reason(bundle, cg, guidance),
                        _reporting.capital_gains_warnings(cg, filing=True),
                    )
                )
            outputs = _write_cg(
                cg,
                options=options,
                output_dir=output_dir,
                format_id=format_id,
                financial_year=financial_year,
                itr_form=itr_form,
                debug=debug,
                account_labels=bundle.paired.account_labels,
            )
        hard_options = _hard_option_blockers(options)
        reasons = (
            *(
                (
                    "option blockers require separate resolution: "
                    f"{option_blocker_summary(hard_options)}",
                )
                if hard_options
                else ()
            ),
            *_reporting.cg_manual_reasons(cg, options, writes_options_csv=False),
        )
        return RobinhoodLegReport(
            "equity-cg",
            RobinhoodLegStatus.MANUAL if reasons else RobinhoodLegStatus.OK,
            outputs,
            reasons,
            _scope_warnings(
                bundle.scope,
                _reporting.option_warnings(options),
                _reporting.capital_gains_warnings(cg, filing=True),
            ),
            run_outputs=RunOutputs(targets, ()),
        )


def run_robinhood_options(
    *,
    input_dir: Path,
    config_path: Path,
    output_dir: Path,
    financial_year: FinancialYear,
    itr_form: ItrForm,
) -> RobinhoodLegReport:
    _outputs.ensure_output_dir(output_dir)
    _refuse_input_collisions(
        output_dir, (*_outputs.OPTIONS_FILENAMES, _GUIDANCE), input_dir, config_path
    )
    _clear_leg(output_dir, (*_outputs.OPTIONS_FILENAMES, _GUIDANCE))
    bundle = _load(
        input_dir=input_dir,
        config_path=config_path,
        financial_year=financial_year,
        include_tax_forms=False,
        schedule_fa=False,
        fetch_identities=False,
    )
    options = _compute_options(bundle, financial_year, itr_form)
    targets = _outputs.paths(output_dir, _outputs.OPTIONS_FILENAMES)
    # If anything raises from the write to the report, remove the workpaper.
    with remove_outputs_on_failure(targets):
        with _fno_warning_on_error(bool(options.trades)):
            _validate_owned_events(bundle, _Computed(options=options))
            if hard_options := _hard_option_blockers(options):
                raise RobinhoodPipelineError(
                    "Robinhood options contain unresolved blockers: "
                    f"{option_blocker_summary(hard_options)}"
                )
            path = _write_options(options, output_dir, financial_year, itr_form)
        if path is None:
            return RobinhoodLegReport(
                "options",
                RobinhoodLegStatus.NOT_REQUIRED,
                warnings=_scope_warnings(bundle.scope),
                run_outputs=RunOutputs(targets, ()),
            )
        manual = bool(options.trades) and options.manual_work_required
        return RobinhoodLegReport(
            "options",
            RobinhoodLegStatus.MANUAL if manual else RobinhoodLegStatus.OK,
            (path,),
            _reporting.option_manual_reasons(options) if manual else (),
            _scope_warnings(bundle.scope, _reporting.option_warnings(options)),
            run_outputs=RunOutputs(targets, ()),
        )


def run_robinhood_income(
    *,
    input_dir: Path,
    config_path: Path,
    output_dir: Path,
    financial_year: FinancialYear,
    itr_form: ItrForm,
    suppress_cross_ay_warning: bool = False,
    debug: bool = False,
    fetch_prices: bool = True,
    price_cache_dir: Path | None = None,
) -> RobinhoodLegReport:
    _outputs.ensure_output_dir(output_dir)
    filenames = (*_outputs.OS_FILENAMES, *_outputs.INCOME_FILENAMES)
    targets = _outputs.paths(output_dir, filenames)
    _refuse_input_collisions(output_dir, (*filenames, _GUIDANCE), input_dir, config_path)
    _clear_leg(output_dir, (*filenames, _GUIDANCE))
    bundle = _load(
        input_dir=input_dir,
        config_path=config_path,
        financial_year=financial_year,
        include_tax_forms=True,
        schedule_fa=False,
        fetch_identities=fetch_prices,
        identity_cache_dir=price_cache_dir,
    )
    options = _compute_options(bundle, financial_year, itr_form)
    guidance = _outputs.IdentityGuidanceFile(output_dir)
    # If anything raises from the first write to the report, remove every output.
    # A refusal keeps the identity guidance file it wrote.
    with remove_outputs_on_failure(targets):
        with _fno_warning_on_error(bool(options.trades)):
            cg = _with_receipt_identity_guidance(
                lambda: _compute_cg(bundle, financial_year), bundle, guidance
            )
            income, assembled, tr = _compute_income(
                bundle,
                financial_year,
                cg,
                options=options,
                suppress_cross_ay_warning=suppress_cross_ay_warning,
            )
            annual_form_warnings = _annual_form_warnings(income)
            _validate_owned_events(bundle, _Computed(options=options, cg=cg, income=income))
            ftc_blocked = income.blockers or cg.blockers or assembled is None or tr is None
            if not ftc_blocked:
                # The helper's refusals stop the run before Schedule OS too.
                require_fsi_write_ready(assembled.fsi_result.rows, assembled.form67_result)
            ftc_reasons = partial(_core.income_ftc_reasons, bundle, income, cg, guidance)
            os_outputs = _write_os(
                _compute_os(bundle, financial_year, income, ftc_reasons=ftc_reasons),
                output_dir=output_dir,
                financial_year=financial_year,
                itr_form=itr_form,
                debug=debug,
                account_labels=bundle.paired.account_labels,
            )
            if ftc_blocked:
                report = RobinhoodLegReport(
                    "income-ftc",
                    RobinhoodLegStatus.ERROR,
                    (),
                    (
                        "FTC schedules remain blocked; Schedule OS is complete but was "
                        "removed, so this failed run leaves no output",
                        *ftc_reasons(),
                    ),
                    _scope_warnings(
                        bundle.scope,
                        annual_form_warnings,
                        _reporting.option_warnings(options),
                        _reporting.capital_gains_warnings(cg),
                    ),
                    run_outputs=NO_RUN_OUTPUTS,
                )
            else:
                outputs, form67_readiness = _write_income(
                    assembled,
                    tr,
                    output_dir=output_dir,
                    financial_year=financial_year,
                    itr_form=itr_form,
                    debug=debug,
                )
                outputs = (*os_outputs, *outputs)
        if not ftc_blocked:
            reasons = _reporting.income_manual_reasons(
                options,
                form67_readiness,
                assembled.form67_result.entries,
                assembled.fsi_result.rows,
                form67_ready_with_fractional_rate=(
                    assembled.form67_result.ready_with_fractional_rate
                ),
            )
            report = RobinhoodLegReport(
                "income-ftc",
                RobinhoodLegStatus.MANUAL if reasons else RobinhoodLegStatus.OK,
                outputs,
                reasons,
                _scope_warnings(
                    bundle.scope,
                    _income_warnings(assembled),
                    annual_form_warnings,
                    _reporting.option_warnings(options),
                    _reporting.capital_gains_warnings(cg),
                ),
                run_outputs=RunOutputs(targets, ()),
            )
    if report.status is RobinhoodLegStatus.ERROR:
        # Like robinhood-all (#719), an exit-4 run leaves no output.
        remove_failed_run_outputs(targets)
    return report


def run_robinhood_fa(
    *,
    input_dir: Path,
    config_path: Path,
    output_dir: Path,
    financial_year: FinancialYear,
    itr_form: ItrForm,
    format_id: str = "review-csv",
    fetch_prices: bool = True,
    price_cache_dir: Path | None = None,
    price_fetcher: DailyCloseFetcher | None = None,
) -> RobinhoodLegReport:
    _outputs.ensure_output_dir(output_dir)
    _refuse_input_collisions(
        output_dir,
        (*_outputs.FA_FILENAMES, _outputs.JSON_FILENAME, _GUIDANCE),
        input_dir,
        config_path,
    )
    json_available = preclear_own_cbdt_fragment(output_dir / _outputs.JSON_FILENAME)
    _clear_leg(output_dir, (*_outputs.FA_FILENAMES, _GUIDANCE))
    # The preclear left nothing at these paths.
    targets = _outputs.paths(output_dir, _outputs.FA_FILENAMES)
    bundle = _load(
        input_dir=input_dir,
        config_path=config_path,
        financial_year=financial_year,
        include_tax_forms=True,
        schedule_fa=True,
        fetch_identities=fetch_prices,
        identity_cache_dir=price_cache_dir,
    )
    guidance = _outputs.IdentityGuidanceFile(output_dir)
    # #1449: no price is fetched and no Schedule FA file is written until the filer
    # verifies the address found for an entry that has none.
    if unverified := _core.unverified_address_reason(bundle, guidance):
        raise RobinhoodPipelineError(unverified)
    # The reconstruction only: an FY option conversion can't fail Schedule FA (#1145).
    options = _reconstruct_options(bundle, financial_year, itr_form)
    # If anything raises from the first write to the report, remove every output,
    # and the JSON fragment only while it is the file this run staged. A refusal
    # keeps the identity guidance file it wrote.
    with (
        remove_outputs_on_failure(targets),
        ClaimedOutputCleanup(output_dir / _outputs.JSON_FILENAME) as json_cleanup,
    ):
        with _fno_warning_on_error(bool(options.trades)):
            # Before the prices: they can't clear a stop decided without them,
            # and their option replay can't explain an option blocker, such as
            # an oversold contract (#1550, #1567, #1574). Inventory refusals
            # and their guidance file come first, naming any option stop after
            # them, as robinhood-all does.
            try:
                gated = _fa_inventory(bundle, write_guidance=guidance)
            except IbkrFaError as exc:
                if (option_stop := _core.fa_option_stop(options, financial_year)) is None:
                    raise
                earlier = f"{exc}; {option_stop}"
            else:
                earlier = None
            if earlier is not None:
                # Raised after the handler, so it chains to nothing.
                raise RobinhoodPipelineError(earlier)
            # A refusal here comes again below, after the prices, as before.
            early_income = None
            with suppress(IbkrFaError):
                early_income = _compute_source_income(bundle, financial_year)
            blockers = _core.fa_pre_price_blockers(
                bundle, financial_year, options, gated, early_income
            )
            if stop := _core.fa_pre_price_refusal(options, blockers, bundle, guidance):
                # With its blockers, the sale-price warnings the FA compute's refusal carries.
                warnings = (
                    _reporting.sale_price_warnings(_sale_price_checks(bundle.paired))
                    if blockers
                    else ()
                )
                raise RobinhoodPipelineError(_reporting.reason_with_warnings(stop, warnings))
            prices, equity_price_audit, option_price_audit, quantities = _fa_prices(
                bundle,
                financial_year,
                options,
                input_dir,
                config_path=config_path,
                fetch_prices=fetch_prices,
                price_cache_dir=price_cache_dir,
                price_fetcher=price_fetcher,
                write_guidance=guidance,
            )
            # Not the FY capital gains: a refusal there can't fail Schedule FA (#1085).
            price_checks = _sale_price_checks(bundle.paired)
            # Source income only: an FY schedule refusal can't fail Schedule FA (#920).
            income = (
                _compute_source_income(bundle, financial_year)
                if early_income is None
                else early_income
            )
            fa = _compute_fa(
                bundle,
                financial_year,
                income,
                options,
                prices=prices,
                option_prices=dict(option_price_audit.series_by_contract),
                quantities=quantities,
            )
            _validate_owned_events(bundle, _Computed(options=options, income=income))
            if fa.blockers:
                raise RobinhoodPipelineError(
                    _reporting.reason_with_warnings(
                        _core.fa_blocker_reason(bundle, fa, guidance),
                        _reporting.sale_price_warnings(price_checks),
                    )
                )
            outputs, json_complete, json_missing = _write_fa(
                fa,
                output_dir=output_dir,
                format_id=format_id,
                financial_year=financial_year,
                itr_form=itr_form,
                json_available=json_available,
                json_cleanup=json_cleanup,
            )
        manual = bool(fa.manual_work) or (
            format_id == CbdtScheduleFaJsonWriter.format_id and not json_complete
        )
        reasons = (*_reporting.fa_manual_reasons(fa), *json_missing)
        return RobinhoodLegReport(
            "schedule-fa",
            RobinhoodLegStatus.MANUAL if manual else RobinhoodLegStatus.OK,
            outputs,
            tuple(dict.fromkeys(reasons)),
            _scope_warnings(
                bundle.scope,
                (
                    *_reporting.option_warnings(options),
                    *_reporting.sale_price_warnings(price_checks),
                    *equity_price_audit.warnings,
                    *option_price_audit.warnings,
                    *getattr(bundle, "entity_address_warnings", ()),
                ),
            ),
            run_outputs=RunOutputs(targets, (json_cleanup,)),
        )


def run_robinhood_all(
    *,
    input_dir: Path,
    config_path: Path,
    output_dir: Path,
    financial_year: FinancialYear,
    itr_form: ItrForm,
    format_id: str = "review-csv",
    fetch_prices: bool = True,
    price_cache_dir: Path | None = None,
    price_fetcher: DailyCloseFetcher | None = None,
    suppress_cross_ay_warning: bool = False,
    debug: bool = False,
) -> RobinhoodAllReport:
    """Run every Robinhood leg and report each one's status.

    All or nothing: if any leg ends ``ERROR``, or an exception escapes after the
    preclear, the run attempts to remove its ``ALL_FILENAMES`` outputs before it
    returns or raises, and its JSON fragment while it is the file the run
    staged. The identity guidance file a refusing leg wrote is kept. A leg's
    cleanup failure or a rollback failure after all legs finish raises
    :class:`RobinhoodAllRemovalError`, carrying the completed report.
    """
    _outputs.ensure_output_dir(output_dir)
    _refuse_input_collisions(
        output_dir,
        (*_outputs.ALL_FILENAMES, _outputs.JSON_FILENAME, _GUIDANCE),
        input_dir,
        config_path,
    )
    json_available = _clear_all(output_dir)
    # The preclear left nothing at these paths, so whatever is there later is this
    # run's output.
    targets = _outputs.paths(output_dir, _outputs.ALL_FILENAMES)
    json_cleanup = ClaimedOutputCleanup(output_dir / _outputs.JSON_FILENAME)
    cleanup_errors: list[Exception] = []
    with remove_outputs_on_failure(targets), json_cleanup:
        report = _run_all_legs(
            input_dir=input_dir,
            config_path=config_path,
            output_dir=output_dir,
            financial_year=financial_year,
            itr_form=itr_form,
            format_id=format_id,
            fetch_prices=fetch_prices,
            price_cache_dir=price_cache_dir,
            price_fetcher=price_fetcher,
            suppress_cross_ay_warning=suppress_cross_ay_warning,
            debug=debug,
            json_available=json_available,
            json_cleanup=json_cleanup,
            cleanup_errors=cleanup_errors,
            guidance=_outputs.IdentityGuidanceFile(output_dir, cleanup_errors),
            run_outputs=RunOutputs(targets, (json_cleanup,)),
        )
    if report.status is not RobinhoodLegStatus.ERROR:
        return report
    try:
        remove_failed_run_outputs(targets, (json_cleanup,))
    except OutputRemovalError as exc:
        raise RobinhoodAllRemovalError(
            str(exc),
            _reporting.without_outputs(
                report, "the run failed: do NOT file this output (see the error below)"
            ),
        ) from exc
    report = _reporting.without_outputs(report, "output removed because the run failed")
    if cleanup_errors:
        details = "; ".join(
            relayed_failure(exc, "could not remove outputs in", output_dir)
            for exc in cleanup_errors
        )
        raise_replacing(
            RobinhoodAllRemovalError(
                f"leg output cleanup failed: {details}; run outputs were removed", report
            ),
            cleanup_errors[0],
        )
    return report


def _failed_writing_leg(
    name: str,
    exc: Exception,
    output_dir: Path,
    filenames: tuple[str, ...],
    cleanup_errors: list[Exception],
    *,
    warnings: tuple[str, ...] = (),
) -> RobinhoodLegReport:
    reason = relayed_failure(exc, f"could not write the {name} outputs in", output_dir)
    if isinstance(exc, OutputRemovalError):
        # The writer could not remove its own output.
        cleanup_errors.append(exc)
    try:
        _clear_leg(output_dir, filenames)
    except Exception as cleanup_error:
        cleanup_errors.append(cleanup_error)
        failure = relayed_failure(
            cleanup_error, f"could not remove the {name} outputs in", output_dir
        )
        reason += f"; cleanup failed: {failure}"
    return _error_leg(name, reason, warnings=warnings)


def _run_all_legs(  # noqa: PLR0912, PLR0915
    *,
    input_dir: Path,
    config_path: Path,
    output_dir: Path,
    financial_year: FinancialYear,
    itr_form: ItrForm,
    format_id: str,
    fetch_prices: bool,
    price_cache_dir: Path | None,
    price_fetcher: DailyCloseFetcher | None,
    suppress_cross_ay_warning: bool,
    debug: bool,
    json_available: bool,
    json_cleanup: ClaimedOutputCleanup,
    cleanup_errors: list[Exception],
    guidance: _outputs.IdentityGuidanceFile,
    run_outputs: RunOutputs,
) -> RobinhoodAllReport:
    bundle = _load(
        input_dir=input_dir,
        config_path=config_path,
        financial_year=financial_year,
        include_tax_forms=True,
        schedule_fa=True,
        fetch_identities=fetch_prices,
        identity_cache_dir=price_cache_dir,
    )
    # #1449: written first, so the guidance file holds both of its sections.
    fa_refusal = _core.unverified_address_reason(bundle, guidance)
    (
        options,
        cg,
        income,
        assembled,
        tr,
        fa,
        equity_price_audit,
        option_price_audit,
        errors,
    ) = _core.attempt_compute(
        bundle,
        financial_year,
        itr_form,
        suppress_cross_ay_warning=suppress_cross_ay_warning,
        input_dir=input_dir,
        config_path=config_path,
        fetch_prices=fetch_prices,
        price_cache_dir=price_cache_dir,
        price_fetcher=price_fetcher,
        write_guidance=guidance,
        hooks=_core.RobinhoodComputeHooks(
            _compute_options,
            _compute_cg,
            _compute_income,
            _fa_prices,
            _compute_fa,
            _validate_owned_events,
            _fa_inventory,
        ),
        fa_refusal=fa_refusal,
    )
    if assembled is not None:
        # The helper's refusals stop the run before any leg writes.
        require_fsi_write_ready(assembled.fsi_result.rows, assembled.form67_result)
    equity_warnings = () if equity_price_audit is None else equity_price_audit.warnings
    option_warnings = () if option_price_audit is None else option_price_audit.warnings
    price_warnings = (
        equity_warnings + option_warnings + getattr(bundle, "entity_address_warnings", ())
    )
    fno_warnings = (ROBINHOOD_FNO_FEMA_WARNING,) if _core.has_options(bundle) else ()
    option_tax_warnings = () if options is None else _reporting.treatment_warnings(options)
    cg_warnings = () if cg is None else _reporting.capital_gains_warnings(cg, filing=True)
    legs: list[RobinhoodLegReport] = []
    cg_error = errors.get("cg")
    if cg_error is None and cg is not None and cg.blockers:
        cg_error = _core.cg_blocker_reason(bundle, cg, guidance)
    try:
        cg_leg = _reporting.write_cg_leg(
            cg,
            options=options,
            failure=cg_error,
            warnings=cg_warnings,
            output_dir=output_dir,
            format_id=format_id,
            financial_year=financial_year,
            itr_form=itr_form,
            debug=debug,
            account_labels=bundle.paired.account_labels,
            writer=_write_cg,
        )
    except Exception as exc:
        # write_cg_leg raises only when the CG outputs couldn't be removed. When
        # its own removal failed, the write failure is the context; a writer's
        # removal error keeps none of the failure it replaced (#1211).
        cleanup_errors.append(exc)
        reason = "cleanup failed: " + relayed_failure(
            exc, "could not remove the equity-cg outputs in", output_dir
        )
        if exc.__context__ is not None:
            failure = relayed_failure(
                exc.__context__, "could not write the equity-cg outputs in", output_dir
            )
            reason = f"{failure}; {reason}"
        cg_leg = _error_leg("equity-cg", reason, warnings=cg_warnings)
    legs.append(cg_leg)
    if income is None or "income" in errors:
        legs.append(_error_leg("schedule-os", errors.get("income", "income compute failed")))
    else:
        try:
            os_outputs = _write_os(
                _compute_os(bundle, financial_year, income),
                output_dir=output_dir,
                financial_year=financial_year,
                itr_form=itr_form,
                debug=debug,
                account_labels=bundle.paired.account_labels,
            )
        except Exception as exc:
            legs.append(
                _failed_writing_leg(
                    "schedule-os", exc, output_dir, _outputs.OS_FILENAMES, cleanup_errors
                )
            )
        else:
            legs.append(
                RobinhoodLegReport(
                    "schedule-os",
                    RobinhoodLegStatus.OK,
                    os_outputs,
                    run_outputs=NO_RUN_OUTPUTS,
                )
            )

    annual_form_warnings = () if income is None else _annual_form_warnings(income)
    if income is None or assembled is None or tr is None or income.blockers or "income" in errors:
        legs.append(
            _error_leg(
                "income-ftc",
                errors["income"]
                if "income" in errors
                else "\n".join(
                    (
                        "unresolved income blockers",
                        *_core.ftc_blocker_reasons(income, bundle.paired.account_labels),
                    )
                ),
                warnings=annual_form_warnings,
            )
        )
    else:
        try:
            outputs, form67_readiness = _write_income(
                assembled,
                tr,
                output_dir=output_dir,
                financial_year=financial_year,
                itr_form=itr_form,
                debug=debug,
            )
        except Exception as exc:
            legs.append(
                _failed_writing_leg(
                    "income-ftc", exc, output_dir, _outputs.INCOME_FILENAMES, cleanup_errors
                )
            )
        else:
            reasons = _reporting.income_manual_reasons(
                options,
                form67_readiness,
                assembled.form67_result.entries,
                assembled.fsi_result.rows,
                form67_ready_with_fractional_rate=(
                    assembled.form67_result.ready_with_fractional_rate
                ),
            )
            legs.append(
                RobinhoodLegReport(
                    "income-ftc",
                    RobinhoodLegStatus.MANUAL if reasons else RobinhoodLegStatus.OK,
                    outputs,
                    reasons,
                    (*_income_warnings(assembled), *annual_form_warnings),
                    run_outputs=NO_RUN_OUTPUTS,
                )
            )

    hard_options = () if options is None else _hard_option_blockers(options)
    if fa is None or fa.blockers or "fa" in errors or "options" in errors:
        fa_error = errors.get("fa") or errors.get("options")
        if fa_error is None and fa is not None and fa.blockers:
            fa_error = _core.fa_blocker_reason(bundle, fa, guidance)
        legs.append(
            _error_leg(
                "schedule-fa",
                fa_error or "unresolved FA blockers",
                warnings=price_warnings,
            )
        )
    else:
        try:
            outputs, _, _ = _write_fa(
                fa,
                output_dir=output_dir,
                format_id=format_id,
                financial_year=financial_year,
                itr_form=itr_form,
                json_available=json_available,
                json_cleanup=json_cleanup,
            )
        except Exception as exc:
            legs.append(
                _failed_writing_leg(
                    "schedule-fa",
                    exc,
                    output_dir,
                    _outputs.FA_FILENAMES,
                    cleanup_errors,
                    warnings=price_warnings,
                )
            )
        else:
            reasons = _reporting.fa_manual_reasons(fa)
            warnings = price_warnings
            status = RobinhoodLegStatus.MANUAL if fa.manual_work else RobinhoodLegStatus.OK
            json_outputs, json_warning, json_error = _outputs.write_automatic_fa_json(
                fa,
                output_dir=output_dir,
                financial_year=financial_year,
                itr_form=itr_form,
                json_available=json_available,
                json_cleanup=json_cleanup,
                cleanup_errors=cleanup_errors,
            )
            outputs = (*outputs, *json_outputs)
            if json_warning is not None:
                warnings = (*warnings, json_warning)
            if json_error is not None:
                status = RobinhoodLegStatus.ERROR
                reasons = (*reasons, json_error)
            legs.append(
                RobinhoodLegReport(
                    "schedule-fa",
                    status,
                    outputs,
                    reasons,
                    warnings,
                    run_outputs=NO_RUN_OUTPUTS,
                )
            )

    if fa is None:
        legs.append(_error_leg("fa-a1", "Schedule FA failed; A1 status is unknown"))
    else:
        legs.append(
            RobinhoodLegReport(
                "fa-a1",
                RobinhoodLegStatus.NOT_REQUIRED,
                warnings=(
                    ("Program Bank cash and interest are disclosed in Schedule FA A2",)
                    if fa.program_bank
                    else ()
                ),
                run_outputs=NO_RUN_OUTPUTS,
            )
        )

    if options is None or "options" in errors:
        legs.append(
            _error_leg(
                "options",
                errors.get("options", "option computation failed"),
                warnings=fno_warnings,
            )
        )
    elif not options.trades:
        legs.append(
            RobinhoodLegReport(
                "options",
                RobinhoodLegStatus.NOT_REQUIRED,
                warnings=fno_warnings,
                run_outputs=NO_RUN_OUTPUTS,
            )
        )
    elif hard_options:
        legs.append(
            _error_leg(
                "options",
                f"unresolved option blockers: {option_blocker_summary(hard_options)}",
                warnings=(*fno_warnings, *option_tax_warnings),
            )
        )
    else:
        try:
            path = _write_options(options, output_dir, financial_year, itr_form)
        except Exception as exc:
            legs.append(
                _failed_writing_leg(
                    "options",
                    exc,
                    output_dir,
                    _outputs.OPTIONS_FILENAMES,
                    cleanup_errors,
                    warnings=(*fno_warnings, *option_tax_warnings),
                )
            )
        else:
            manual = bool(options.trades) and options.manual_work_required
            legs.append(
                RobinhoodLegReport(
                    "options",
                    RobinhoodLegStatus.MANUAL if manual else RobinhoodLegStatus.OK,
                    (path,) if path is not None else (),
                    _reporting.option_manual_reasons(options) if manual else (),
                    (*fno_warnings, *option_tax_warnings),
                    run_outputs=NO_RUN_OUTPUTS,
                )
            )

    legs.append(
        RobinhoodLegReport(
            "product-scope",
            RobinhoodLegStatus.NOT_REQUIRED,
            warnings=_scope_warnings(bundle.scope),
            run_outputs=NO_RUN_OUTPUTS,
        )
    )
    return RobinhoodAllReport(
        output_dir=output_dir,
        financial_year=financial_year,
        calendar_year=financial_year.start_year,
        legs=tuple(legs),
        run_outputs=run_outputs,
    )


__all__ = [
    "RobinhoodAllRemovalError",
    "RobinhoodAllReport",
    "RobinhoodLegReport",
    "RobinhoodLegStatus",
    "RobinhoodPipelineError",
    "run_robinhood_all",
    "run_robinhood_cg",
    "run_robinhood_fa",
    "run_robinhood_income",
    "run_robinhood_options",
]
