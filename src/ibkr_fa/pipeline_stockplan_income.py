"""``stockplan-income`` pipeline: Schedule OS / FSI / TR / Form 67 for a Morgan
Stanley StockPlan (Shareworks) foreign dividend, e.g. Google/Alphabet GSUs (#365).

Parses the "IN - Dividend Report" .xlsx into shared
:class:`~ibkr_fa.models.Dividend` + :class:`~ibkr_fa.models.WithholdingTax`
records and routes them through the SAME broker-agnostic compute the IBKR and
NetBenefits pipelines use
(:func:`ibkr_fa.compute.income_engine.compute_income_schedules` +
:func:`ibkr_fa.compute.schedule_tr.compute_schedule_tr_from_fsi`), then writes
the filing CSVs plus optional debug audits. A BLANK Form 67 row withholds
``form-67.csv`` and writes ``form-67-debug.csv`` instead.

The dividend report is the sole authoritative income source. Fail-closed
("it's taxes — never be wrong"): its title-stated coverage must span the whole
financial year, else an in-FY dividend could be silently missing — refuse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compliance.foreign_tax_credit import BLANK_RELIEF_TAXPAYER_ID_WARNING
from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.form67 import (
    Form67Readiness,
    form67_override_advice,
    require_no_refused_form67_rows,
)
from ibkr_fa.compute.income_engine import (
    IncomeComputeResult,
    compute_income_schedules,
)
from ibkr_fa.compute.schedule_fsi import require_fsi_write_ready
from ibkr_fa.compute.schedule_fsi_relief import relief_row_lacks_taxpayer_id
from ibkr_fa.compute.schedule_tr import compute_schedule_tr_from_fsi
from ibkr_fa.config import UserConfig, load_user_config
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.models import Dividend, WithholdingTax
from ibkr_fa.output import RenderContext
from ibkr_fa.output._cleanup import RunOutputs, remove_previous_outputs
from ibkr_fa.output.income_bundle import INCOME_FILENAMES, write_income_outputs
from ibkr_fa.output.schedules.form67 import portal_import_warnings
from ibkr_fa.parser.stockplan import require_verified_issuer
from ibkr_fa.parser.stockplan.dividend_report import parse_stockplan_dividend_report
from ibkr_fa.path_checks import is_dir
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_stockplan_fa import stockplan_inputs
from ibkr_fa.types import Isin, Ticker

_DIVIDEND_REPORT_PREFIX = "IN - Dividend Report"

# #882: this run never reads the Capital Gain/Loss workbook, so on its own it
# can't prove zero sales.
_ZERO_SALES_UNPROVEN = (
    "zero StockPlan sales are not proven: stockplan-income does not read the Capital "
    "Gain/Loss workbook, so a sale would be missing from Schedule CG and Schedule FSI. "
    "Run stockplan-all to prove zero sales, or check the Capital Gains Report for sales "
    "before filing."
)


class StockplanIncomeError(IbkrFaError):
    """A StockPlan income run cannot proceed with certainty.

    Raised for missing config, a missing/ambiguous dividend report, or a report
    whose coverage does not span the financial year. The CLI maps it (like every
    :class:`IbkrFaError`) to the domain-error exit code. Its text never prints a
    date (#1043).
    """


@dataclass(frozen=True, slots=True)
class StockplanIncomeRunReport:
    """Summary of one successful :func:`run_stockplan_income` call."""

    output_dir: Path
    financial_year: FinancialYear
    os_foreign_rows: int
    fsi_rows: int
    tr_rows: int
    form67_entries: int
    dividend_count: int
    withholding_count: int
    manual_fill_warnings: tuple[str, ...]
    fx_policy_appropriate_for_ay: bool
    withholding_total_usd: Decimal
    form67_readiness: Form67Readiness
    manual_fill_required: bool
    run_outputs: RunOutputs = field(kw_only=True)


def _discover_dividend_report(input_dir: Path) -> Path:
    """The single ``IN - Dividend Report*.xlsx`` in ``input_dir``, or refuse."""
    matches = sorted(
        p for p in input_dir.glob("*.xlsx") if p.stem.startswith(_DIVIDEND_REPORT_PREFIX)
    )
    if not matches:
        raise StockplanIncomeError(f"no {_DIVIDEND_REPORT_PREFIX!r} .xlsx found in {input_dir}")
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        raise StockplanIncomeError(
            f"multiple {_DIVIDEND_REPORT_PREFIX!r} .xlsx in {input_dir} (ambiguous): {names}"
        )
    return matches[0]


def _load_dividends(
    input_dir: Path, config_path: Path, financial_year: FinancialYear
) -> tuple[UserConfig, list[Dividend], list[WithholdingTax]]:
    """Load the config and the dividend report's records, refusing a report
    whose coverage does not span the FY."""
    config = load_user_config(config_path)
    config.account.require_status()
    if config.stockplan is None:
        raise StockplanIncomeError(
            f"no [stockplan] block in {config_path}; add one (see docs/stockplan-contract.md)"
        )

    sec = config.stockplan.security
    require_verified_issuer(sec.symbol, str(sec.isin))
    report_path = _discover_dividend_report(input_dir)
    dividends, withholdings, coverage = parse_stockplan_dividend_report(
        report_path, symbol=Ticker(sec.symbol), isin=Isin(sec.isin)
    )
    # Fail-closed coverage: the report must span the whole FY, else an in-FY
    # dividend could be missing and we'd under-report income.
    cov_start, cov_end = coverage
    if cov_start > financial_year.start_date or cov_end < financial_year.end_date:
        raise StockplanIncomeError(
            f"{report_path.name}'s reporting period does not span the financial year; "
            "a dividend could be missing"
        )
    return config, dividends, withholdings


def _compute(
    config: UserConfig,
    financial_year: FinancialYear,
    dividends: list[Dividend],
    withholdings: list[WithholdingTax],
    suppress_cross_ay_warning: bool,
) -> IncomeComputeResult:
    return compute_income_schedules(
        config,
        financial_year,
        fx_ctx=TaxFxContext.for_fy(
            financial_year, config.fx, suppress_cross_ay_warning=suppress_cross_ay_warning
        ),
        dividends=dividends,
        withholding_taxes=withholdings,
        closed_lots=[],
        interest_credits=[],
        interest_payer_country_iso2=None,
    )


def stockplan_form67_readiness(
    *,
    input_dir: Path,
    config_path: Path,
    financial_year: FinancialYear,
    suppress_cross_ay_warning: bool = False,
) -> Form67Readiness:
    """The income run's Form 67 readiness, computed without writing anything,
    so ``stockplan-all`` can refuse a REFUSED row (#601), or a Schedule FSI
    relief the portal would overwrite (#900), before its first write."""
    config, dividends, withholdings = _load_dividends(input_dir, config_path, financial_year)
    result = _compute(config, financial_year, dividends, withholdings, suppress_cross_ay_warning)
    require_fsi_write_ready(result.fsi_result.rows, result.form67_result)
    return result.form67_result.readiness


def run_stockplan_income(
    *,
    input_dir: Path,
    config_path: Path,
    output_dir: Path,
    financial_year: FinancialYear,
    itr_form: ItrForm = "ITR-2",
    suppress_cross_ay_warning: bool = False,
    debug: bool = False,
    cg_step_checks_sales: bool = False,
) -> StockplanIncomeRunReport:
    """Emit filing schedules and optional debug audits for the StockPlan dividend.

    Args:
        cg_step_checks_sales: True only when the caller runs a Capital Gains
            step that proves zero sales or fails the run (``stockplan-all``).
            Otherwise zero sales are unproven: the report warns and requires
            manual fill (#882).

    Raises:
        StockplanIncomeError: Missing [stockplan] config, a missing/ambiguous
            dividend report, or coverage that does not span the FY.
        ConfigError: ``[stockplan.security]`` is not a verified issuer.
        IbkrFaError: Propagated from config loading / the parser / FX / the shared
            FSI orphan-WHT guard.
    """
    if not is_dir(output_dir):
        raise StockplanIncomeError(f"output path is not a directory: {output_dir}")

    # Fail-closed: clear ALL target CSVs BEFORE any other fallible work (config
    # load, discovery, parse, coverage, compute, write) so ANY refused run —
    # including a missing/invalid config or [stockplan] block — leaves no STALE
    # prior-run CSV that a user might mistake for current output (#358 pattern),
    # unless a target is the config or an export this run reads.
    targets = tuple(output_dir / filename for filename in INCOME_FILENAMES)
    remove_previous_outputs(targets, stockplan_inputs(input_dir, config_path))

    config, dividends, withholdings = _load_dividends(input_dir, config_path, financial_year)
    result = _compute(config, financial_year, dividends, withholdings, suppress_cross_ay_warning)
    tr_result = compute_schedule_tr_from_fsi(result.fsi_result)
    # #601: refuse a REFUSED Form 67 row before anything is written.
    require_no_refused_form67_rows(result.form67_result)
    ctx = RenderContext(
        assessment_year_start=financial_year.assessment_year_start,
        itr_form=itr_form,
        source_label="Morgan Stanley StockPlan (Shareworks)",
    )
    # Targets were already cleared up front. Everything from the first write to
    # the report is all-or-nothing: if anything raises, remove every target so a
    # failed run can't leave fresh CSVs that a user might file as complete.
    run_outputs = RunOutputs(targets, ())
    with run_outputs.removed_on_failure():
        write_income_outputs(
            schedule_os=result.os_result,
            fsi=result.fsi_result,
            tr=tr_result,
            form67=result.form67_result,
            output_dir=output_dir,
            context=ctx,
            debug=debug,
        )

        in_fy_dividends = [
            d
            for d in dividends
            if financial_year.start_date <= d.payment_date <= financial_year.end_date
        ]
        in_fy_withholdings = [
            w
            for w in withholdings
            if financial_year.start_date <= w.payment_date <= financial_year.end_date
        ]
        warnings: list[str] = []
        entries = result.form67_result.entries
        portal_warnings = portal_import_warnings(
            entries, override_advice=form67_override_advice(entries)
        )
        warnings.extend(portal_warnings)
        # Only a row with foreign tax claims relief, so a withholding-free run
        # isn't asked for a TIN it won't file (mirrors #361).
        relief_taxpayer_id_blank = relief_row_lacks_taxpayer_id(result.fsi_result.rows)
        if relief_taxpayer_id_blank:
            warnings.append(BLANK_RELIEF_TAXPAYER_ID_WARNING)
        if not cg_step_checks_sales:
            warnings.append(_ZERO_SALES_UNPROVEN)

        withholding_total_usd = (
            -sum((w.amount for w in in_fy_withholdings), Decimal("0"))
        ).quantize(Decimal("0.01"))

        return StockplanIncomeRunReport(
            output_dir=output_dir,
            financial_year=financial_year,
            os_foreign_rows=len(result.os_result.lines),
            fsi_rows=len(result.fsi_result.rows),
            tr_rows=len(tr_result.rows),
            form67_entries=len(result.form67_result.entries),
            dividend_count=len(in_fy_dividends),
            withholding_count=len(in_fy_withholdings),
            manual_fill_warnings=tuple(warnings),
            fx_policy_appropriate_for_ay=result.fx_policy_appropriate_for_ay,
            withholding_total_usd=withholding_total_usd,
            form67_readiness=result.form67_result.readiness,
            manual_fill_required=(
                not cg_step_checks_sales
                or result.form67_result.readiness is Form67Readiness.BLANK
                or result.form67_result.ready_with_fractional_rate
                or relief_taxpayer_id_blank
            ),
            run_outputs=run_outputs,
        )
