"""``netbenefits-cg`` pipeline: a proven-zero Schedule CG (STCG + LTCG) for Fidelity
ESPP lots (#357 / the ``#I13`` wiring gap).

Thin wiring around the already-tested
:func:`ibkr_fa.compute.espp_capital_gains.compute_espp_capital_gains` adapter:
discover the closed-lots CSV + purchase confirmations
(:func:`~ibkr_fa.parser.netbenefits.bundle.load_bundle`) and the statements, check
the statements prove the FY had no sale (below), build the Rule 115
capital-gains FX policy, compute, and write the CG STCG + CG-LTCG review CSVs
via the SAME writers the IBKR ``schedule-cg`` / ``schedule-cg-ltcg`` pipelines use.

Every filed number and every compute guard lives in the compute: the
§49(2AA) historical gross INR FMV from ``[[netbenefits.cg_fmv_values]]``
(tied to the purchase confirmation, not Fidelity's US "Cost basis"), the
24-month STCG/LTCG split from the acquire/sale *dates*, Rule 115(1),
Explanation (2)(f), FX for sale proceeds, and refusals on missing/ambiguous FMV,
identity/cost-basis mismatch, oversell, and any lot the engines would silently
drop. This module adds only discovery, the evidence checks below (run through
:func:`~ibkr_fa.compute.espp_capital_gains.proven_zero_capital_gains`), FX policy
and writers. It guesses nothing.

**Proven-zero only (#666).** The closed-lots CSV carries no download date, so it
can't show it lists every disposal in the FY. The Stock Plan Services statements
can: they print every buy and sell as a dated activity row, and the parser refuses
any dated row it doesn't read. So the run writes Schedule CG only when the statements
cover the capital-gains window with no gap (from the later of the FY start and
the account opening date through ten days past the FY end) and the CSV lists no
disposal in the FY. The result is then a proven zero. Any in-FY closed-lots row
refuses until ibkr-fa can check a
sale against the statements (#1150); the compute's own refusals (such as an in-FY
zero-proceeds row) stay behind that check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.espp_capital_gains import proven_zero_capital_gains
from ibkr_fa.compute.schedule_cg_table_f import ScheduleCgTableFCase
from ibkr_fa.config import load_user_config
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.output import RenderContext, Subcommand
from ibkr_fa.output._cleanup import RunOutputs, remove_previous_outputs
from ibkr_fa.output.bundle import spec_for, write_output_bundle
from ibkr_fa.parser.netbenefits.bundle import load_bundle, load_statements
from ibkr_fa.path_checks import is_dir
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_netbenefits_fa import netbenefits_inputs

_STCG_FILENAME = "schedule-cg.csv"
_LTCG_FILENAME = "schedule-cg-ltcg.csv"


class NetBenefitsCgError(IbkrFaError):
    """A NetBenefits Schedule CG run cannot proceed with certainty.

    Raised for a missing ``[netbenefits]`` config block, a non-directory output
    path, statements that don't prove the FY had no sale (a coverage gap or an
    opening date after the FY end), or a closed-lots row dated in the FY (#666).
    Compute-layer refusals surface as
    :class:`~ibkr_fa.compute.espp_capital_gains.EsppCapitalGainsError`, or as
    :class:`~ibkr_fa.compute.schedule_cg.ScheduleCgDroppedLotError` for a sold
    lot that would get no Schedule CG line. Both are
    :class:`~ibkr_fa.exceptions.IbkrFaError` subclasses, which the CLI maps to
    the same domain-error exit code.
    """


@dataclass(frozen=True, slots=True)
class NetBenefitsCgRunReport:
    """Summary of one successful :func:`run_netbenefits_cg` call.

    Attributes:
        output_dir: Directory the two review CSVs were written to.
        financial_year: The Indian FY reported.
        stcg_rows: Schedule CG (STCG) lines written (>= 0).
        ltcg_rows: Schedule CG (LTCG) lines written (>= 0).
        total_stcg_inr_rounded: The filed STCG total, ``portal_amounts.filed_total_inr``
            (may be negative — a short-term loss).
        total_ltcg_inr_rounded: The filed LTCG total, ``portal_amounts.filed_total_inr``
            (may be negative — a long-term loss).
        out_of_fy_disposals: Closed lots whose ``sold_or_transferred_date`` falls
            OUTSIDE the reported FY (correctly excluded — not this FY's gain).
            Surfaced so a wrong ``--financial-year`` doesn't silently yield empty
            CSVs the user mistakes for "nothing to file".
        fx_policy_appropriate_for_ay: Whether the Rule 115 FX policy is
            verified for this assessment year (see #126).
        run_outputs: Every output the run may have published, which the CLI
            attempts to remove if rendering or printing its report fails (#832, #1227).
        table_f_loss_across_bands: ``(Table F row, file name)`` for each review
            CSV that leaves its Schedule CG Table F date bands blank because a
            band nets a loss and two or more bands net gains, so the filer
            enters them (#1361). Empty until an in-FY sale can pass (#1150).
    """

    output_dir: Path
    financial_year: FinancialYear
    stcg_rows: int
    ltcg_rows: int
    total_stcg_inr_rounded: Decimal
    total_ltcg_inr_rounded: Decimal
    out_of_fy_disposals: int
    fx_policy_appropriate_for_ay: bool
    run_outputs: RunOutputs = field(kw_only=True)
    table_f_loss_across_bands: tuple[tuple[int, str], ...] = field(kw_only=True)


def run_netbenefits_cg(
    *,
    input_dir: Path,
    config_path: Path,
    output_dir: Path,
    financial_year: FinancialYear,
    itr_form: ItrForm = "ITR-2",
    suppress_cross_ay_warning: bool = False,
) -> NetBenefitsCgRunReport:
    """Emit Schedule CG (STCG + LTCG) review CSVs for a proven-zero FY (#666).

    Args:
        input_dir: The NetBenefits bundle DIRECTORY: the closed-lots CSV, the
            purchase-confirmation PDFs and the Stock Plan Services statement PDFs
            covering the capital-gains window (from the later of the FY start and
            the account opening date through ten days past the FY end). Requires
            the same one-closed-lots-CSV bundle shape as ``netbenefits-fa``.
        config_path: Path to user.toml (must contain a ``[netbenefits]`` block).
        output_dir: Destination directory (must exist) for schedule-cg.csv and
            schedule-cg-ltcg.csv.
        financial_year: The Indian FY to report.
        suppress_cross_ay_warning: Suppress the AY 2027-28+ FX-policy
            cross-AY verification warning once independently verified (#126).

    Returns:
        A :class:`NetBenefitsCgRunReport` summarising the run for the CLI.

    Raises:
        NetBenefitsCgError: Missing ``[netbenefits]`` config, a non-directory
            output path, statements that don't cover the capital-gains window,
            an opening date after the FY end, or a closed-lots row dated in the
            FY (#666).
        IbkrFaError: Propagated from config loading, bundle discovery/parsers
            (a statement holding a sale or any other unread dated row refuses
            there), the FX policy, or the ESPP capital-gains compute
            (missing/ambiguous FMV, oversell, silent-drop guard, etc.).
    """
    if not is_dir(output_dir):
        raise NetBenefitsCgError(f"output path is not a directory: {output_dir}")

    # Fail-closed: clear BOTH target CSVs BEFORE any other fallible work (config
    # load, discovery, compute), so ANY refused run — including a missing/invalid
    # config or [netbenefits] block — leaves no STALE prior-run CSV in
    # --output-dir that a user might mistake for current, correct output. Each
    # writer is also atomic (temp-file os.replace), so a mid-write raise likewise
    # leaves the not-yet-written schedule absent. Never clear the config or a
    # bundle file: it would be deleted before it is read.
    targets = (output_dir / _STCG_FILENAME, output_dir / _LTCG_FILENAME)
    remove_previous_outputs(targets, netbenefits_inputs(input_dir, config_path))

    config = load_user_config(config_path)
    if config.netbenefits is None:
        raise NetBenefitsCgError(
            f"no [netbenefits] block in {config_path}; add one (see `ibkr-fa init`)"
        )
    cfg = config.netbenefits

    fx_ctx = TaxFxContext.for_fy(
        financial_year, config.fx, suppress_cross_ay_warning=suppress_cross_ay_warning
    )

    # netbenefits-cg consumes closed lots + confirmations + statements; open-lots
    # CSVs are ignored entirely (a full liquidation may have none, and CG never
    # reads them). The bundle loads first, so its refusals keep firing first.
    bundle = load_bundle(input_dir, require_open_lots=False)
    # #666: the statements must prove the FY had no sale before anything is filed.
    statements = load_statements(input_dir, cfg.participant_no, include_year_end=False)
    result = proven_zero_capital_gains(
        statements, bundle, cfg, financial_year, fx_ctx, error=NetBenefitsCgError
    )

    specs = (
        spec_for(Subcommand.CG, "review-csv", result.stcg, output_dir / _STCG_FILENAME),
        spec_for(Subcommand.CG_LTCG, "review-csv", result.ltcg, output_dir / _LTCG_FILENAME),
    )
    ctx = RenderContext(
        assessment_year_start=financial_year.assessment_year_start, itr_form=itr_form
    )
    # If anything raises from the first write to the report, remove both CSVs:
    # a lone STCG CSV, or an unreported pair, must not be filed.
    run_outputs = RunOutputs(targets, ())
    with run_outputs.removed_on_failure():
        write_output_bundle(specs, ctx)

        fy_start, fy_end = financial_year.start_date, financial_year.end_date
        out_of_fy_disposals = sum(
            1
            for lot in bundle.closed_lots
            if not (fy_start <= lot.sold_or_transferred_date <= fy_end)
        )
        return NetBenefitsCgRunReport(
            output_dir=output_dir,
            financial_year=financial_year,
            stcg_rows=len(result.stcg.stcg_lines),
            ltcg_rows=len(result.ltcg.ltcg_lines),
            total_stcg_inr_rounded=result.stcg.portal_amounts.filed_total_inr,
            total_ltcg_inr_rounded=result.ltcg.portal_amounts.filed_total_inr,
            out_of_fy_disposals=out_of_fy_disposals,
            fx_policy_appropriate_for_ay=fx_ctx.fx_policy_appropriate_for_ay,
            run_outputs=run_outputs,
            table_f_loss_across_bands=tuple(
                (row, path.name)
                for row, section, path in (
                    (3, result.stcg, targets[0]),
                    (5, result.ltcg, targets[1]),
                )
                if section.table_f.case is ScheduleCgTableFCase.LOSS_ACROSS_BANDS
            ),
        )
