"""``netbenefits-income`` pipeline: Schedule OS / FSI / TR / Form 67 for a Fidelity
NetBenefits foreign dividend (#I10).

Has :func:`~ibkr_fa.normalize.netbenefits.income.normalize_income` build
:class:`~ibkr_fa.models.Dividend` + :class:`~ibkr_fa.models.WithholdingTax`
records from the bundle's SPS statements (#I9 income parsing) and routes them
through the SAME broker-agnostic compute the IBKR pipelines use
(:func:`ibkr_fa.compute.income_engine.compute_income_schedules` +
:func:`ibkr_fa.compute.schedule_os.compute_schedule_os_dividend_lines`), then
writes the filing CSVs plus optional debug audits. A BLANK Form 67 row
withholds ``form-67.csv`` and writes ``form-67-debug.csv`` instead.

The STATEMENT is the sole authoritative source (the price feed carries no
per-share dividend events, so there is no Yahoo gross-verifier); the #I9 parser's
``gross + withholding == MMKT sweep`` coupling is the document-internal proof.
To avoid silently under-reporting, the bundle's statements must CONTIGUOUSLY
cover the holding-capable window ``[max(FY-start, account opening date), FY-end]``
— a gap refuses. Every in-FY dividend must be for the configured ESPP security
or Fidelity's core fund, and every withholding must match exactly one same-date,
same-name dividend (``StatementWithholding`` carries no CUSIP). Year-end reports
don't count towards coverage, since their silence proves nothing, but each in-FY
dividend and withholding one prints must match a covering Stock Plan Services
report (#1249).

With ``include_capital_gains`` (``netbenefits-all``), the FY's capital gains are
filed only when the statements prove it had no sale (#666): they must cover the
capital-gains window, which runs ten days past the FY end, and the closed-lots
CSV must list no disposal in the FY (see
:func:`~ibkr_fa.compute.espp_capital_gains.proven_zero_capital_gains`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Final

from ibkr_fa.compliance.foreign_tax_credit import BLANK_RELIEF_TAXPAYER_ID_WARNING
from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.espp_capital_gains import (
    CAPITAL_GAINS_WINDOW,
    EsppCapitalGainsResult,
    proven_zero_capital_gains,
)
from ibkr_fa.compute.form67 import (
    Form67Readiness,
    form67_override_advice,
    require_no_refused_form67_rows,
)
from ibkr_fa.compute.income_engine import IncomeComputeResult, compute_income_schedules
from ibkr_fa.compute.schedule_fsi import require_fsi_write_ready
from ibkr_fa.compute.schedule_fsi_relief import relief_row_lacks_taxpayer_id
from ibkr_fa.compute.schedule_tr import compute_schedule_tr_from_fsi
from ibkr_fa.compute.source_coverage import first_coverage_gap
from ibkr_fa.config import NetBenefitsConfig, UserConfig, load_user_config
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.models import Dividend, WithholdingTax
from ibkr_fa.normalize.netbenefits.income import normalize_income, require_year_end_income_listed
from ibkr_fa.output import RenderContext
from ibkr_fa.output._cleanup import RunOutputs, remove_previous_outputs
from ibkr_fa.output.income_bundle import INCOME_FILENAMES, write_income_outputs
from ibkr_fa.output.quicko_foreign_tax import (
    QuickoForeignTaxInput,
    has_quicko_foreign_tax_entries,
)
from ibkr_fa.output.schedules.form67 import portal_import_warnings
from ibkr_fa.parser.netbenefits.bundle import load_bundle, load_statements
from ibkr_fa.parser.netbenefits.statement_pdf import ReportType, StatementData
from ibkr_fa.path_checks import is_dir
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_netbenefits_fa import netbenefits_inputs


class NetBenefitsIncomeError(IbkrFaError):
    """A NetBenefits income run cannot proceed with certainty.

    Raised for config, output-path, statement-coverage and 1042-S withholding
    mismatch refusals. The CLI maps it (like every :class:`IbkrFaError`) to the
    domain-error exit code.
    """


_CENT = Decimal("0.01")
# The window the statements must cover, as refusals name it.
_INCOME_WINDOW = (
    "the income window from the later of the FY start and "
    "netbenefits.account.account_opening_date through the FY end"
)
# ADR-0019 (#1327): a missing withholding agent blanks Quicko's deductor value.
BLANK_QUICKO_DEDUCTOR_WARNING: Final = (
    'The Quicko block in schedule-tr.csv leaves "Name of the deductor" blank; add '
    "[netbenefits.account].withholding_agent_name, copied from your Form 1042-S: box 12d "
    "for tax in box 7a, or the agent box 14a names for tax in box 8"
)


@dataclass(frozen=True, slots=True)
class NetBenefitsIncomeRunReport:
    """Summary of one successful :func:`run_netbenefits_income` call.

    Attributes (#I16 1042-S reconciliation):
        statement_withholding_usd: Sum of the in-FY statement Non-Resident Tax
            (the USD total the FTC is built from), always >= 0.
        reconcile_1042s_usd: The FY-allocated authoritative 1042-S withholding
            total, which matched ``statement_withholding_usd`` (a mismatch
            refuses), or ``None`` if not supplied (advise reconciling later).
    """

    output_dir: Path
    financial_year: FinancialYear
    os_foreign_rows: int
    fsi_rows: int
    tr_rows: int
    form67_entries: int
    dividend_count: int
    withholding_count: int
    cg_stcg_rows: int
    cg_ltcg_rows: int
    manual_fill_warnings: tuple[str, ...]
    fx_policy_appropriate_for_ay: bool
    form67_readiness: Form67Readiness
    form67_ready_with_fractional_rate: bool
    relief_taxpayer_id_blank: bool
    quicko_deductor_blank: bool
    statement_withholding_usd: Decimal = Decimal("0")
    reconcile_1042s_usd: Decimal | None = None
    run_outputs: RunOutputs = field(kw_only=True)

    @property
    def manual_fill_required(self) -> bool:
        """Form 67 is withheld, ``form-67.csv`` files a fractional Rate(%) the
        filer enters by hand after upload (#1447), a relief row's foreign TIN is
        blank (#1084), or a Quicko form's "Name of the deductor" is blank (#1327).
        """
        return (
            self.form67_readiness is Form67Readiness.BLANK
            or self.form67_ready_with_fractional_rate
            or self.relief_taxpayer_id_blank
            or self.quicko_deductor_blank
        )


def _load_income_records(
    input_dir: Path,
    config_path: Path,
    financial_year: FinancialYear,
    *,
    include_capital_gains: bool,
) -> tuple[
    UserConfig, NetBenefitsConfig, list[StatementData], list[Dividend], list[WithholdingTax]
]:
    """Load the config, the Stock Plan Services statements (year-end reports
    excluded) and the in-FY dividend + withholding records. A year-end report's
    in-FY income rows must then match the Stock Plan Services reports (#1249).
    With ``include_capital_gains``, the coverage and opening-date refusals name
    the capital-gains window, which the corrected run must also cover (#1478,
    #1486)."""
    config = load_user_config(config_path)
    if config.netbenefits is None:
        raise NetBenefitsIncomeError(
            f"no [netbenefits] block in {config_path}; add one (see `ibkr-fa init`)"
        )
    cfg = config.netbenefits

    every_report = load_statements(input_dir, cfg.participant_no, include_year_end=True)
    statements = [s for s in every_report if s.report_type is ReportType.STOCK_PLAN_SERVICES]
    # Income can't predate the account opening; require coverage from there (or
    # FY-start, whichever later) through FY-end so no in-window dividend is missed.
    window_start = max(financial_year.start_date, cfg.account.account_opening_date)
    gap = first_coverage_gap(
        ((s.period_start, s.period_end) for s in statements),
        window_start=window_start,
        window_end=financial_year.end_date,
    )
    # With capital gains, name its wider window: statements covering it also pass
    # require_capital_gains_coverage on the rerun (#1486).
    window = CAPITAL_GAINS_WINDOW if include_capital_gains else _INCOME_WINDOW
    if gap is not None and gap.short_end:
        raise NetBenefitsIncomeError(
            f"the statements (year-end reports excluded) end before the FY end, so they don't "
            f"cover {window}; supply the statement(s) covering the gap"
        )
    if gap is not None:
        raise NetBenefitsIncomeError(
            f"the statements (year-end reports excluded) leave a gap in {window} "
            f"(coverage is needed to prove no dividend was missed there)"
        )

    record = normalize_income(
        statements,
        security_cusip=cfg.security.cusip,
        security_symbol=cfg.security.instrument.symbol,
        security_isin=cfg.security.instrument.isin,
        account_opening_date=cfg.account.account_opening_date,
        fy_start=financial_year.start_date,
        fy_end=financial_year.end_date,
        coverage_window=CAPITAL_GAINS_WINDOW if include_capital_gains else None,
    )
    require_year_end_income_listed(
        every_report, fy_start=financial_year.start_date, fy_end=financial_year.end_date
    )
    return config, cfg, statements, list(record.dividends), list(record.withholding_tax)


def _income_schedules(
    config: UserConfig,
    financial_year: FinancialYear,
    fx_ctx: TaxFxContext,
    dividends: list[Dividend],
    withholdings: list[WithholdingTax],
    cg_result: EsppCapitalGainsResult | None = None,
) -> IncomeComputeResult:
    """The shared income compute over NetBenefits' records: the source has no
    closed lots for the engine and no interest, and ``cg_result`` brings the
    precomputed capital gains, if any."""
    return compute_income_schedules(
        config,
        financial_year,
        fx_ctx=fx_ctx,
        dividends=dividends,
        withholding_taxes=withholdings,
        closed_lots=[],
        interest_credits=[],
        interest_payer_country_iso2=None,
        cg_stcg_result=None if cg_result is None else cg_result.stcg,
        cg_ltcg_result=None if cg_result is None else cg_result.ltcg,
    )


def netbenefits_form67_readiness(
    *,
    input_dir: Path,
    config_path: Path,
    financial_year: FinancialYear,
    include_capital_gains: bool,
    suppress_cross_ay_warning: bool = False,
) -> Form67Readiness:
    """The income run's Form 67 readiness, computed without writing anything,
    so ``netbenefits-all`` can refuse a REFUSED row (#601) or a Schedule FSI
    relief the portal would overwrite (#900) before its first write. A missing
    ``[netbenefits.account].withholding_agent_name`` doesn't refuse: when the
    income leg writes a Quicko form, it leaves the deductor value blank and
    needs manual fill (#1327). Other refusals still apply.
    Form 67 comes from dividends and withholding alone, so capital gains are
    not computed; a Capital Gains FSI row is never compared (its relief or tax
    payable is blank). ``include_capital_gains`` is the income leg's: it picks
    the window the coverage and opening-date refusals name (#1478, #1486)."""
    config, _cfg, _statements, dividends, withholdings = _load_income_records(
        input_dir, config_path, financial_year, include_capital_gains=include_capital_gains
    )
    result = _income_schedules(
        config,
        financial_year,
        TaxFxContext.for_fy(
            financial_year, config.fx, suppress_cross_ay_warning=suppress_cross_ay_warning
        ),
        dividends,
        withholdings,
    )
    require_fsi_write_ready(result.fsi_result.rows, result.form67_result)
    return result.form67_result.readiness


def run_netbenefits_income(
    *,
    input_dir: Path,
    config_path: Path,
    output_dir: Path,
    financial_year: FinancialYear,
    itr_form: ItrForm = "ITR-2",
    suppress_cross_ay_warning: bool = False,
    reconcile_1042s_usd: Decimal | None = None,
    include_capital_gains: bool = False,
    debug: bool = False,
) -> NetBenefitsIncomeRunReport:
    """Emit filing schedules and optional debug audits for the Fidelity dividend.

    An otherwise successful run that writes a Quicko form without
    ``[netbenefits.account].withholding_agent_name`` still writes every file:
    each form's "Name of the deductor" is blank, and the report asks for manual
    fill (ADR-0019, #1327). Other refusals still apply.

    Raises:
        NetBenefitsIncomeError: Missing [netbenefits] config, a non-directory
            output path, a statement coverage gap, or a supplied
            ``reconcile_1042s_usd`` that differs from the in-FY statement
            withholding. With
            ``include_capital_gains``, also statements that don't cover the
            capital-gains window, an account opening date after the FY end, or a
            closed-lots row dated in the FY (#666), before any output is written.
        NetBenefitsBundleError: An unclassifiable PDF, a non-directory input,
            multi-security income, an unmatched withholding, or a year-end
            report's in-FY income row that no covering Stock Plan Services
            report lists with the same amount.
        IbkrFaError: Propagated from config loading / parsers / FX / the shared
            FSI orphan-WHT guard, and with ``include_capital_gains`` from the
            ESPP capital-gains compute, before any output is written.
    """
    if not is_dir(output_dir):
        raise NetBenefitsIncomeError(f"output path is not a directory: {output_dir}")
    targets = tuple(output_dir / filename for filename in INCOME_FILENAMES)
    remove_previous_outputs(targets, netbenefits_inputs(input_dir, config_path))

    config, cfg, statements, dividends, withholdings = _load_income_records(
        input_dir, config_path, financial_year, include_capital_gains=include_capital_gains
    )
    # #I16: every withholding amount is parser-guaranteed strictly negative (a
    # non-negative "Non-Resident Tax" is refused upstream, #I9), so the filed FTC
    # total is just -Σ amount. (A real reversal/refund would fail loudly there, not net here.)
    signed_sum = sum((w.amount for w in withholdings), Decimal("0"))
    statement_withholding_usd = (-signed_sum).quantize(_CENT)
    # #918: a supplied 1042-S total must equal it, checked before any compute or write.
    # Both are whole-cent (the CLI rejects >2 dp); Decimal != is value-inequality, so
    # no quantize on the supplied value (which would raise on a huge input).
    if reconcile_1042s_usd is not None and reconcile_1042s_usd != statement_withholding_usd:
        raise NetBenefitsIncomeError(
            f"FY {financial_year}: the Form 1042-S withholding total given with "
            "--reconcile-1042s-fy-withholding-usd differs from the statements' in-FY "
            "withholding total. Correct the FY-allocated 1042-S total, or find the dividend or "
            "withholding the statements miss"
        )

    fx_ctx = TaxFxContext.for_fy(
        financial_year, config.fx, suppress_cross_ay_warning=suppress_cross_ay_warning
    )
    cg_result = None
    if include_capital_gains:
        # After the existing income checks, bundle refusals precede the new
        # capital-gains evidence checks (#666).
        bundle = load_bundle(input_dir, require_open_lots=False)
        cg_result = proven_zero_capital_gains(
            statements, bundle, cfg, financial_year, fx_ctx, error=NetBenefitsIncomeError
        )

    result = _income_schedules(config, financial_year, fx_ctx, dividends, withholdings, cg_result)
    tr_result = compute_schedule_tr_from_fsi(result.fsi_result)

    # #601: refuse a REFUSED Form 67 row before anything is written.
    require_no_refused_form67_rows(result.form67_result)
    withholding_agent_name = cfg.account.withholding_agent_name
    # ADR-0019 (#1327): without the name, each Quicko form's deductor value is blank.
    quicko_deductor_blank = withholding_agent_name is None and has_quicko_foreign_tax_entries(
        result.form67_result
    )
    ctx = RenderContext(
        assessment_year_start=financial_year.assessment_year_start,
        itr_form=itr_form,
        source_label="Fidelity NetBenefits stock plan",
        quicko_foreign_tax=QuickoForeignTaxInput(
            form67=result.form67_result,
            deductor_name=withholding_agent_name,
            foreign_tins=config.account.foreign_tins,
        ),
    )
    # Each writer is atomic; a failure from the first write to the report also
    # removes every target so no partial or unreported set can be filed.
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

        warnings: list[str] = []
        entries = result.form67_result.entries
        warnings.extend(
            portal_import_warnings(entries, override_advice=form67_override_advice(entries))
        )
        # Only a row with foreign tax claims relief, so a withholding-free run
        # isn't asked for a TIN it won't file (#361).
        relief_taxpayer_id_blank = relief_row_lacks_taxpayer_id(result.fsi_result.rows)
        if relief_taxpayer_id_blank:
            warnings.append(BLANK_RELIEF_TAXPAYER_ID_WARNING)
        if quicko_deductor_blank:
            warnings.append(BLANK_QUICKO_DEDUCTOR_WARNING)

        return NetBenefitsIncomeRunReport(
            output_dir=output_dir,
            financial_year=financial_year,
            os_foreign_rows=len(result.os_result.lines),
            fsi_rows=len(result.fsi_result.rows),
            tr_rows=len(tr_result.rows),
            form67_entries=len(result.form67_result.entries),
            dividend_count=len(dividends),
            withholding_count=len(withholdings),
            cg_stcg_rows=result.cg_stcg_rows_consumed,
            cg_ltcg_rows=result.cg_ltcg_rows_consumed,
            manual_fill_warnings=tuple(warnings),
            fx_policy_appropriate_for_ay=result.fx_policy_appropriate_for_ay,
            form67_readiness=result.form67_result.readiness,
            form67_ready_with_fractional_rate=result.form67_result.ready_with_fractional_rate,
            relief_taxpayer_id_blank=relief_taxpayer_id_blank,
            quicko_deductor_blank=quicko_deductor_blank,
            statement_withholding_usd=statement_withholding_usd,
            reconcile_1042s_usd=reconcile_1042s_usd,
            run_outputs=run_outputs,
        )
