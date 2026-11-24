"""Fidelity CMA Schedule OS / FSI / TR / Form 67 income pipeline."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compliance import missing_indian_tax_payable_warning
from ibkr_fa.compliance.foreign_tax_credit import BLANK_RELIEF_TAXPAYER_ID_WARNING
from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.fidelity_cma_capital_gains import (
    compute_fidelity_cma_capital_gains,
    in_fy_spaxx_sale_count,
)
from ibkr_fa.compute.form67 import (
    Form67Readiness,
    form67_override_advice,
    require_no_refused_form67_rows,
)
from ibkr_fa.compute.ftc_eligibility import eligible_us_withholding
from ibkr_fa.compute.income_engine import compute_income_schedules
from ibkr_fa.compute.schedule_fsi_relief import relief_row_lacks_taxpayer_id
from ibkr_fa.compute.schedule_tr import compute_schedule_tr_from_fsi
from ibkr_fa.config import FidelityCmaRefundStatus, load_user_config
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.normalize.fidelity_cma.form_1042s import (
    add_statements_from,
    carried_balance_gap,
    reconcile_1042s,
)
from ibkr_fa.output import RenderContext
from ibkr_fa.output._cleanup import RunOutputs, remove_previous_outputs, scan_inputs
from ibkr_fa.output.income_bundle import INCOME_FILENAMES, write_income_outputs
from ibkr_fa.output.schedules.form67 import portal_import_warnings
from ibkr_fa.parser.fidelity_cma import CmaStatementBundle, load_fidelity_cma_statements
from ibkr_fa.parser.fidelity_cma_1042s import Form1042SPacket, read_1042s_packet
from ibkr_fa.path_checks import is_dir
from ibkr_fa.pipeline import FinancialYear

_DECEMBER = 12


class FidelityCmaIncomeError(IbkrFaError):
    """The CMA income run cannot proceed without guessing."""


@dataclass(frozen=True, slots=True, repr=False)
class FidelityCmaIncomeRunReport:
    output_dir: Path
    financial_year: FinancialYear
    dividend_count: int
    interest_count: int
    interest_total_inr_rounded: Decimal
    actual_withholding_count: int
    eligible_withholding_count: int
    blocked_refund_years: tuple[tuple[int, FidelityCmaRefundStatus], ...]
    os_foreign_rows: int
    os_interest_rows: int
    fsi_rows: int
    tr_rows: int
    form67_entries: int
    cg_stcg_rows: int
    cg_ltcg_rows: int
    manual_fill_warnings: tuple[str, ...]
    fx_policy_appropriate_for_ay: bool
    form67_readiness: Form67Readiness
    warnings: tuple[str, ...] = ()
    run_outputs: RunOutputs = field(kw_only=True)

    @property
    def ftc_complete(self) -> bool:
        return not self.blocked_refund_years

    @property
    def filing_complete(self) -> bool:
        return (
            self.ftc_complete
            and not self.manual_fill_warnings
            and self.form67_readiness is Form67Readiness.READY
        )


def fidelity_cma_inputs(
    input_dir: Path, config_path: Path, tax_form_dir: Path | None = None
) -> tuple[tuple[str, Path], ...]:
    """The config and every statement / 1042-S PDF a CMA run may read, for the
    output collision guard."""
    folders = [("--input", input_dir)]
    if tax_form_dir is not None:
        folders.append(("--tax-forms", tax_form_dir))
    return (
        ("--config", config_path),
        *folders,
        *((flag, path) for flag, folder in folders for path in scan_inputs(folder, "*.pdf")),
    )


def _following_month_end(day: date) -> date:
    year = day.year + (day.month == _DECEMBER)
    month = 1 if day.month == _DECEMBER else day.month + 1
    return date(year, month, monthrange(year, month)[1])


def coverage_refusal(
    financial_year: FinancialYear,
    *,
    opened: str,
    end: str,
    gaps: Sequence[str],
    start: str | None = None,
) -> str:
    """A coverage refusal: the statements ``financial_year`` needs, from
    ``start`` (by default its start) or an account opening ``opened`` with no
    carried balance, through ``end``, then each of ``gaps``. It prints only FY
    dates and fixed words (#1510, #1515)."""
    window = (
        f"statement coverage for FY {financial_year} must run from "
        f"{_fy_start(financial_year) if start is None else start}, or from the account's "
        f"first statement if it opened {opened} with no carried balance, through {end}"
    )
    return "; ".join((window, *gaps))


def late_start_gap(financial_year: FinancialYear, *, add_from: str | None = None) -> str:
    """The gap clause for statements that start after the FY start with a
    carried balance: add the months from ``add_from`` (by default the FY
    start)."""
    return carried_balance_gap(_fy_start(financial_year), add_from=add_from)


def early_end_gap(end: str) -> str:
    """The gap clause for statements that end before ``end``."""
    return (
        f"the statements end before {end}, so add every monthly statement after the "
        f"latest one given through {end}"
    )


_HOLDING_HISTORY = (
    "add every monthly statement before it back to the account's first, as SPAXX "
    "capital gains need history from before any carried holding"
)
# The FIFO refuses a first statement that begins with a SPAXX holding
# (compute/fidelity_cma_capital_gains.py). These clauses name that history:
# as a fact when the earliest statement given is the one the FIFO will read,
# else on what the earliest statement shows once the other clauses' months are
# added (#1515).
CARRIED_HOLDING_GAP = f"the earliest statement begins with a SPAXX holding, so {_HOLDING_HISTORY}"
LATER_HOLDING_GAP = (
    f"if the earliest statement then begins with a SPAXX holding, {_HOLDING_HISTORY}"
)


def _fy_start(financial_year: FinancialYear) -> str:
    return f"1 April {financial_year.start_year}"


# reconcile_1042s refuses statements that carry a balance into a start after
# 1 January of the Form 1042-S's tax year, which is read only after this check.
_TAX_YEAR_START = "1 January of your Form 1042-S's tax year"
_TAX_YEAR_START_GAP = (
    f"the statements start with a carried balance, so if they start after {_TAX_YEAR_START}, "
    f"{add_statements_from('that 1 January')}"
)


def _require_fy_coverage(
    bundle: CmaStatementBundle,
    financial_year: FinancialYear,
    *,
    spaxx_sales: int,
) -> None:
    """Refuse missing FY coverage, naming the later 1 January and SPAXX
    history needs (#1515): the Form 1042-S tax year's start, and the history
    needed before a carried holding when the FY has, or a missing FY month
    could add, a SPAXX redemption."""
    statements = bundle.statements
    late = not bundle.covers_from(financial_year.start_date)
    short = statements[-1].period_end < _following_month_end(financial_year.end_date)
    if not (late or short):
        return
    start = f"the earlier of {_fy_start(financial_year)} and {_TAX_YEAR_START}"
    end = f"30 April {financial_year.end_year}"
    carried = statements[0].beginning_account_value_usd is not None
    gaps = []
    if late:
        gaps.append(late_start_gap(financial_year, add_from=start))
    elif carried:
        gaps.append(_TAX_YEAR_START_GAP)
    if short:
        gaps.append(early_end_gap(end))
    # Earlier months can be added only before a carried balance; with every FY
    # month given and no SPAXX redemption, the run computes no gains.
    fy_seen = not late and statements[-1].period_end >= financial_year.end_date
    if carried and spaxx_sales:
        gaps.append(LATER_HOLDING_GAP)
    elif carried and not fy_seen:
        gaps.append(
            f"if FY {financial_year} then has a SPAXX redemption and the earliest statement "
            f"begins with a SPAXX holding, {_HOLDING_HISTORY}"
        )
    raise FidelityCmaIncomeError(
        coverage_refusal(
            financial_year,
            start=start,
            opened="later",
            end=f"{end}, the month after FY end, for late-posted interest",
            gaps=gaps,
        )
    )


def _discover_1042s(tax_form_dir: Path) -> Form1042SPacket:
    if not is_dir(tax_form_dir):
        raise FidelityCmaIncomeError("tax-form input must be a directory")
    paths = sorted(tax_form_dir.glob("*.pdf"))
    if len(paths) != 1:
        raise FidelityCmaIncomeError(
            "expected exactly one observed-version Fidelity Form 1042-S packet"
        )
    return read_1042s_packet(paths[0])


def run_fidelity_cma_income(
    *,
    input_dir: Path,
    tax_form_dir: Path,
    config_path: Path,
    output_dir: Path,
    financial_year: FinancialYear,
    itr_form: ItrForm = "ITR-2",
    suppress_cross_ay_warning: bool = False,
    debug: bool = False,
) -> FidelityCmaIncomeRunReport:
    """Emit CMA filing schedules and optional debug audits; FTC is fail-closed."""
    if not is_dir(output_dir):
        raise FidelityCmaIncomeError("output path must be a directory")
    targets = tuple(output_dir / filename for filename in INCOME_FILENAMES)
    remove_previous_outputs(targets, fidelity_cma_inputs(input_dir, config_path, tax_form_dir))
    config = load_user_config(config_path)
    if config.fidelity_cma is None:
        raise FidelityCmaIncomeError("missing [fidelity_cma] config")
    cfg = config.fidelity_cma
    bundle, year_ends, record = load_fidelity_cma_statements(
        input_dir,
        expected_account_id=cfg.account_id,
        spaxx_isin=cfg.spaxx_isin,
    )
    spaxx_sales = in_fy_spaxx_sale_count(bundle, financial_year)
    _require_fy_coverage(bundle, financial_year, spaxx_sales=spaxx_sales)
    packet = _discover_1042s(tax_form_dir)
    year_end = year_ends.get(packet.tax_year)
    if year_end is None:
        raise FidelityCmaIncomeError("matching CMA year-end report is missing")
    reconcile_1042s(
        bundle,
        year_end,
        packet,
        tax_year=packet.tax_year,
        # FY coverage has passed here, so a SPAXX redemption means gains.
        coverage_clauses=(LATER_HOLDING_GAP,) if spaxx_sales else (),
    )

    dividends, interests, actual_wht = record.income_in_window(
        start=financial_year.start_date,
        end=financial_year.end_date,
    )
    eligibility = eligible_us_withholding(
        record.withholding_tax,
        window_start=financial_year.start_date,
        window_end=financial_year.end_date,
        us_tax_year=cfg.us_tax_year,
        reconciled_tax_years=frozenset({packet.tax_year}),
    )
    eligible_wht = eligibility.eligible
    blocked = eligibility.blocked_tax_years
    fx_ctx = TaxFxContext.for_fy(
        financial_year, config.fx, suppress_cross_ay_warning=suppress_cross_ay_warning
    )
    # An FY with no SPAXX redemption has no gain to prove, so its run needs no
    # acquisition history.
    gains = (
        compute_fidelity_cma_capital_gains(bundle, spaxx_isin=cfg.spaxx_isin, fx_ctx=fx_ctx)
        if spaxx_sales
        else None
    )
    result = compute_income_schedules(
        config,
        financial_year,
        fx_ctx=fx_ctx,
        dividends=dividends,
        withholding_taxes=list(eligible_wht),
        closed_lots=[],
        interest_credits=interests,
        # US: the statement parser refuses statements without the Fidelity
        # Brokerage Services / National Financial Services disclosure, and
        # refuses FDIC/Program Bank sweep positions (parser/fidelity_cma.py).
        interest_payer_country_iso2="US",
        cg_stcg_result=None if gains is None else gains.stcg,
        cg_ltcg_result=None if gains is None else gains.ltcg,
    )
    tr_result = compute_schedule_tr_from_fsi(result.fsi_result)
    # #601: refuse a REFUSED Form 67 row before anything is written.
    require_no_refused_form67_rows(result.form67_result)
    form67_readiness = result.form67_result.readiness
    ctx = RenderContext(
        assessment_year_start=financial_year.assessment_year_start,
        itr_form=itr_form,
        source_label="Fidelity CMA",
    )
    # If anything raises from the first write to the report, remove every target.
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
            # #1453: a blocked FTC leaves Form 67 with no rows though there is
            # US tax to claim, so the header-only official file is withheld.
            withhold_form67=bool(blocked),
        )

        # Only the packet's year is reconciled, so it is blocked only by its
        # refund status; any other year can't be reconciled at all.
        warnings = [
            (
                f"US tax year {year} refund status is {status.value}; FTC is blocked for "
                "the entire FY until every in-FY withholding year is finalized or "
                "no-refund-claim and reconciled. To claim it, give the "
                f"[[fidelity_cma.us_tax_years]] entry with tax_year = {year} "
                'refund_status = "finalized" or "no-refund-claim", a non-empty provenance '
                "string and "
                'eligible_tax = [{ payment_date = YYYY-MM-DD, amount_usd = "0.00" }, ...] '
                f"with one entry for every withholding date in {year} on the statements: the "
                "whole-cent US tax owed on that payment, 0 allowed, never more than the "
                "statement withheld"
                if year == packet.tax_year
                else f"US tax year {year} can't be reconciled in this version: "
                f"fidelity-cma-income reads only a Form 1042-S packet for US tax year "
                f"{packet.tax_year}, so FTC is blocked for the entire FY whatever "
                f"{year}'s refund status; complete Schedule FSI, Schedule TR and Form 67 "
                "by hand"
            )
            for year, status in blocked
        ]
        entries = result.form67_result.entries
        override_advice = form67_override_advice(entries)
        portal_warnings = portal_import_warnings(entries, override_advice=override_advice)
        warnings.extend(portal_warnings)
        if relief_row_lacks_taxpayer_id(result.fsi_result.rows):
            warnings.append(BLANK_RELIEF_TAXPAYER_ID_WARNING)
        if (
            missing_indian_tax_payable_warning(override_advice) not in portal_warnings
            and eligible_wht
            and (
                not result.form67_result.entries
                or form67_readiness is not Form67Readiness.READY
                or any(
                    row.foreign_tax_inr > 0
                    and (
                        row.tax_payable_inr is None
                        or row.tax_relief_inr is None
                        or row.dtaa_article is None
                    )
                    for row in result.fsi_result.rows
                )
            )
        ):
            warnings.append(
                "FSI/Form 67 Indian-tax and relief columns are incomplete; "
                "provide an exact per-source override and, for §91, the statutory "
                "average-rate inputs"
            )
        interest_total = result.os_result.interest_total_inr_rounded
        if interest_total is None:
            raise FidelityCmaIncomeError("filing requires the separately computed interest total")
        return FidelityCmaIncomeRunReport(
            output_dir=output_dir,
            financial_year=financial_year,
            dividend_count=len(dividends),
            interest_count=len(interests),
            interest_total_inr_rounded=interest_total,
            actual_withholding_count=len(actual_wht),
            eligible_withholding_count=len(eligible_wht),
            blocked_refund_years=blocked,
            os_foreign_rows=len(result.os_result.lines),
            os_interest_rows=result.os_interest_rows_consumed,
            fsi_rows=len(result.fsi_result.rows),
            tr_rows=len(tr_result.rows),
            form67_entries=len(result.form67_result.entries),
            cg_stcg_rows=result.cg_stcg_rows_consumed,
            cg_ltcg_rows=result.cg_ltcg_rows_consumed,
            manual_fill_warnings=tuple(warnings),
            fx_policy_appropriate_for_ay=result.fx_policy_appropriate_for_ay,
            form67_readiness=form67_readiness,
            warnings=(),
            run_outputs=run_outputs,
        )
