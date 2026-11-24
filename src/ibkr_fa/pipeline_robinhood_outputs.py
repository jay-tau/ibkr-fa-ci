"""Atomic output helpers for Robinhood schedule legs."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.form67 import Form67Readiness, require_no_refused_form67_rows
from ibkr_fa.compute.income_engine import IncomeComputeResult
from ibkr_fa.compute.robinhood_capital_gains import RobinhoodCapitalGainsResult
from ibkr_fa.compute.robinhood_fa import RobinhoodFaResult
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionBusinessFiling,
    RobinhoodOptionsResult,
    compute_robinhood_option_business_filing,
    compute_robinhood_option_business_fsi,
)
from ibkr_fa.compute.schedule_os import ScheduleOsComputeResult
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult
from ibkr_fa.exceptions import PathRefusalError, errno_name, relayed_failure
from ibkr_fa.invariants import require
from ibkr_fa.output import (
    CG_FILING_WORKBOOK,
    OS_FILING_WORKBOOK,
    RenderContext,
    Subcommand,
    output_binding,
)
from ibkr_fa.output._atomic import atomic_text_output
from ibkr_fa.output._cleanup import (
    ClaimedOutputCleanup,
    OutputRemovalError,
    remove_output_paths,
    remove_outputs_on_failure,
)
from ibkr_fa.output.bundle import spec_for, write_output_bundle
from ibkr_fa.output.cbdt_json_fragment import (
    CbdtA2FragmentInput,
    CbdtA2Remedies,
    CbdtJsonIncompleteError,
    CbdtScheduleFaFragmentInput,
    CbdtScheduleFaJsonWriter,
    check_cbdt_schedule_fa_fragment,
    preclear_own_cbdt_fragment,
    refuse_failed_fragment_write,
)
from ibkr_fa.output.cbdt_json_schema import CBDT_SCHEMA_ASSESSMENT_YEAR_START
from ibkr_fa.output.income_bundle import FTC_FILENAMES, write_income_outputs
from ibkr_fa.output.robinhood_options_csv import (
    OPTIONS_FILENAMES,
    write_robinhood_options_csv,
)
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_fa_cbdt_json import FA_ENTITIES_REMEDIES
from ibkr_fa.pipeline_robinhood_core import RobinhoodPipelineError

CG_FILING_FILENAMES = (
    "schedule-cg.csv",
    "schedule-cg.xlsx",
    "schedule-cg-ltcg.csv",
    "schedule-cg-ltcg.xlsx",
)
CG_DEBUG_FILENAMES = (
    "schedule-cg-debug.csv",
    "schedule-cg-debug.xlsx",
    "schedule-cg-ltcg-debug.csv",
    "schedule-cg-ltcg-debug.xlsx",
)
CG_FILENAMES = (*CG_FILING_FILENAMES, *CG_DEBUG_FILENAMES)
OS_FILENAMES = (
    "schedule-os.csv",
    "schedule-os.xlsx",
    "schedule-os-debug.csv",
)
INCOME_FILENAMES = FTC_FILENAMES
FA_FILENAMES = (
    "schedule-fa-a2.csv",
    "schedule-fa-a2.xlsx",
    "schedule-fa-a3.csv",
    "schedule-fa-a3.xlsx",
    "schedule-fa.csv",
    "schedule-fa.xlsx",
    "schedule-fa-a3-a2.csv",
    "schedule-fa-quicko.xlsx",
)
JSON_FILENAME = "schedule-fa.json"
#: The private file a refusal for unconfigured securities writes its suggested
#: ``user.toml`` blocks to (#1044), and Schedule FA's stop for an
#: ``[[fa.entities]]`` entry without address the addresses it found (#1449).
#: Every command's collision check and preclear covers it, but no refusal's
#: failure cleanup does, so the refusal that writes it keeps it.
IDENTITY_GUIDANCE_FILENAME = "robinhood-identity-guidance.toml"
_A2_REMEDIES: Final[CbdtA2Remedies] = CbdtA2Remedies(
    peak="the account's computed daily peak is missing; no config value can supply it",
    address="supply the account's [[robinhood.accounts]] address",
    zip_code=(
        "supply the account's [[robinhood.accounts]] zip (the CBDT JSON requires it "
        "even though the CSV allows it blank)"
    ),
)
ALL_FILENAMES = (*CG_FILENAMES, *OS_FILENAMES, *INCOME_FILENAMES, *FA_FILENAMES, *OPTIONS_FILENAMES)


def ensure_output_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise PathRefusalError("Robinhood output path must be a directory", path)
    if os.name != "posix":
        return
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PathRefusalError(
            "Robinhood output directory must be owned by the current user and private "
            "(use an owner-owned directory and run `chmod 700` on it, or use a filesystem "
            "that preserves POSIX modes)",
            path,
        )


def paths(output_dir: Path, names: Iterable[str]) -> tuple[Path, ...]:
    return tuple(output_dir / name for name in names)


class IdentityGuidanceFile:
    """One run's private identity guidance file, written at most once (#1044).

    Called with the guidance's TOML text, it writes the file, mode ``0o600``
    through :func:`atomic_text_output`, which replaces anything at the path
    rather than writing through it. It returns the path: the output folder as
    the user typed it, joined with :data:`IDENTITY_GUIDANCE_FILENAME`. A later
    call returns the path without writing again.
    """

    def __init__(self, output_dir: Path, cleanup_errors: list[Exception] | None = None) -> None:
        self._path = output_dir / IDENTITY_GUIDANCE_FILENAME
        self._written = False
        self._cleanup_errors = cleanup_errors

    def __call__(self, guidance: str) -> Path:
        """Write ``guidance`` unless this run already has, and return the path.

        Raises:
            RobinhoodPipelineError: if the file can't be written; its message
                names the path and the errno name only.
            OutputRemovalError: if a failed write's staging file can't be
                removed. It is added to ``cleanup_errors`` too, when given.
        """
        if not self._written:
            try:
                with atomic_text_output(self._path, encoding="utf-8", newline="\n") as handle:
                    handle.write(guidance)
            except OutputRemovalError as exc:
                if self._cleanup_errors is not None:
                    self._cleanup_errors.append(exc)
                raise
            except OSError as exc:
                raise RobinhoodPipelineError(
                    f"could not write the identity guidance file {self._path} ({errno_name(exc)})"
                ) from exc
            self._written = True
        return self._path


def _occupied(path: Path) -> bool:
    """Whether anything is at ``path``. Only a missing path reads as vacant; any
    other error while checking propagates."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def clear_leg(output_dir: Path, filenames: tuple[str, ...]) -> None:
    remove_output_paths(paths(output_dir, filenames))


def clear_all(output_dir: Path) -> bool:
    json_available = preclear_own_cbdt_fragment(output_dir / JSON_FILENAME)
    clear_leg(output_dir, (*ALL_FILENAMES, IDENTITY_GUIDANCE_FILENAME))
    return json_available


def write_cg(
    result: RobinhoodCapitalGainsResult,
    *,
    options: RobinhoodOptionsResult,
    output_dir: Path,
    format_id: str,
    financial_year: FinancialYear,
    itr_form: ItrForm,
    debug: bool,
    account_labels: dict[int, str],
) -> tuple[Path, ...]:
    """Write ``result``'s CG outputs; the filing workbook names the sections
    ``options``' capital-gain lines need entered by hand (#1322)."""
    filing_path = output_dir / "schedule-cg.xlsx"
    extension = "xlsx" if format_id == "xlsx" else "csv"
    debug_paths = (
        output_dir / f"schedule-cg-debug.{extension}",
        output_dir / f"schedule-cg-ltcg-debug.{extension}",
    )
    output_paths = (filing_path, *debug_paths) if debug else (filing_path,)
    debug_specs = (
        [
            spec_for(Subcommand.CG, format_id, result.stcg, debug_paths[0]),
            spec_for(Subcommand.CG_LTCG, format_id, result.ltcg, debug_paths[1]),
        ]
        if debug
        else []
    )
    ctx = RenderContext(
        assessment_year_start=financial_year.assessment_year_start,
        itr_form=itr_form,
        source_label="Robinhood",
        account_labels=account_labels,
        debug=debug,
    )
    # All or nothing: a refused filing workbook removes the debug audits too;
    # the refusal carries the reason.
    with remove_outputs_on_failure(output_paths):
        write_output_bundle(debug_specs, ctx)
        filing = result.filing(options.capital_gain_lines)
        require(filing is not None, what="Schedule CG filing requires a blocker-free result")
        assert filing is not None  # narrowed for the type checker; require() raised above
        output_binding(*CG_FILING_WORKBOOK).writer(ctx).write(filing, filing_path)
        return output_paths


def _option_business_output(
    result: RobinhoodOptionsResult,
) -> tuple[RobinhoodOptionBusinessFiling | None, str]:
    filing = compute_robinhood_option_business_filing(result)
    if filing is not None:
        fsi_status = (
            "Schedule FSI emitted"
            if filing.bp_income_inr > 0
            else "non-positive BP B42; no Schedule FSI row"
        )
        return filing, f"ITR-3 P&L/BP/CFL fields emitted ({fsi_status})"
    if compute_robinhood_option_business_fsi(result).rows:
        return None, "ITR-3 P&L/BP/CFL (Schedule FSI emitted)"
    if result.business_fsi_manual_work_required:
        return None, "ITR-3 P&L/BP/CFL/Schedule FSI (manual)"
    return None, "ITR-3 P&L/BP/CFL (no positive aggregate Schedule FSI amount)"


def write_options(
    result: RobinhoodOptionsResult,
    output_dir: Path,
    financial_year: FinancialYear,
    itr_form: ItrForm,
) -> Path | None:
    if not result.trades:
        return None
    business_filing, business_target = _option_business_output(result)
    # All or nothing: if the writer raises after publishing, remove the file.
    with remove_outputs_on_failure(paths(output_dir, OPTIONS_FILENAMES)):
        return write_robinhood_options_csv(
            result,
            output_dir,
            assessment_year_start=financial_year.assessment_year_start,
            itr_form=itr_form,
            business_filing=business_filing,
            business_target=business_target,
        )


def write_income(
    assembled: IncomeComputeResult,
    tr: ScheduleTrComputeResult,
    *,
    output_dir: Path,
    financial_year: FinancialYear,
    itr_form: ItrForm,
    debug: bool,
) -> tuple[tuple[Path, ...], Form67Readiness]:
    # #601: a REFUSED row stops the leg before anything is written.
    require_no_refused_form67_rows(assembled.form67_result)
    ctx = RenderContext(
        assessment_year_start=financial_year.assessment_year_start,
        itr_form=itr_form,
        source_label="Robinhood",
    )
    # Every caller clears these names first, so removing them all on failure
    # removes only what this call wrote.
    with remove_outputs_on_failure(paths(output_dir, INCOME_FILENAMES)):
        written = write_income_outputs(
            schedule_os=None,
            fsi=assembled.fsi_result,
            tr=tr,
            form67=assembled.form67_result,
            output_dir=output_dir,
            context=ctx,
            debug=debug,
        )
        return written, assembled.form67_result.readiness


def write_os(
    result: ScheduleOsComputeResult,
    *,
    output_dir: Path,
    financial_year: FinancialYear,
    itr_form: ItrForm,
    debug: bool,
    account_labels: dict[int, str],
) -> tuple[Path, ...]:
    """Write filing Schedule OS and its optional review sidecar."""

    specs = [spec_for(*OS_FILING_WORKBOOK, result, output_dir / "schedule-os.xlsx")]
    if debug:
        specs.append(
            spec_for(Subcommand.OS, "review-csv", result, output_dir / "schedule-os-debug.csv")
        )
    ctx = RenderContext(
        assessment_year_start=financial_year.assessment_year_start,
        itr_form=itr_form,
        source_label="Robinhood",
        account_labels=account_labels,
    )
    output_paths = tuple(spec.path for spec in specs)
    with remove_outputs_on_failure(output_paths):
        write_output_bundle(specs, ctx)
        return output_paths


def write_fa(
    result: RobinhoodFaResult,
    *,
    output_dir: Path,
    format_id: str,
    financial_year: FinancialYear,
    itr_form: ItrForm,
    json_available: bool,
    json_cleanup: ClaimedOutputCleanup,
) -> tuple[tuple[Path, ...], bool, tuple[str, ...]]:
    """Write the requested FA output. Returns its paths, whether the requested
    JSON is complete (for another format, whether the result is eligible for
    one), and the fragment's missing filed values when it isn't written for
    them.

    The fragment's file is claimed through ``json_cleanup``, which the caller
    arms for the JSON path around its run: a failure removes the fragment only
    while it is still the file this call staged."""
    ctx = RenderContext(
        assessment_year_start=financial_year.assessment_year_start, itr_form=itr_form
    )
    if format_id == CbdtScheduleFaJsonWriter.format_id:
        path = output_dir / JSON_FILENAME
        value = CbdtScheduleFaFragmentInput(
            a2=tuple(
                CbdtA2FragmentInput(
                    row=item.row,
                    account_id_cross_checked=item.account_id_cross_checked,
                )
                for item in result.a2
            ),
            a3_rows=result.a3_rows,
            calendar_year=financial_year.start_year,
            assessment_year_start=financial_year.assessment_year_start,
            itr_form=itr_form,
            a2_remedies=_A2_REMEDIES,
            a3_remedies=FA_ENTITIES_REMEDIES,
        )
        if not result.cbdt_json_eligible or not json_available:
            # A structural or schema problem refuses even when no fragment
            # is written; a missing value is the result's manual work.
            check_cbdt_schedule_fa_fragment(value)
            if not json_available:
                raise RobinhoodPipelineError(
                    f"refusing to write {path}: the file there was not generated by "
                    "ibkr-fa (no provenance marker), so it may be a hand-merged or "
                    "unrelated file; it was left untouched. Move or remove it, then re-run."
                )
            return (), False, ()
        # A successful write would replace a file already here, which this run
        # didn't write.
        if _occupied(path):
            raise RobinhoodPipelineError(
                f"{path} already exists and this run did not write it; it was left untouched"
            )
        try:
            CbdtScheduleFaJsonWriter.from_context(ctx).write(
                value, path, on_staged=json_cleanup.claim
            )
        except Exception as exc:
            refuse_failed_fragment_write(path, json_cleanup, exc, RobinhoodPipelineError)
            # Refused before staging: a missing filed value is "not written".
            if isinstance(exc, CbdtJsonIncompleteError):
                return (), False, exc.reasons
            raise
        return (path,), True, ()

    extension = "xlsx" if format_id == "xlsx" else "csv"
    output_paths = (
        output_dir / f"schedule-fa-a2.{extension}",
        output_dir / f"schedule-fa-a3.{extension}",
    )
    with remove_outputs_on_failure(output_paths):
        write_output_bundle(
            [
                spec_for(Subcommand.FA_A2, format_id, result.a2_rows, output_paths[0]),
                spec_for(Subcommand.FA, format_id, result.a3_rows, output_paths[1]),
            ],
            ctx,
        )
        return output_paths, result.cbdt_json_eligible, ()


def write_automatic_fa_json(
    result: RobinhoodFaResult,
    *,
    output_dir: Path,
    financial_year: FinancialYear,
    itr_form: ItrForm,
    json_available: bool,
    json_cleanup: ClaimedOutputCleanup,
    cleanup_errors: list[Exception],
) -> tuple[tuple[Path, ...], str | None, str | None]:
    """Write all-mode's automatic JSON, claiming its file through
    ``json_cleanup`` (see :func:`write_fa`). Returns its outputs, a warning when
    it is skipped, and an error when an attempt to write it fails. A failed
    attempt that may have left a file it could not remove is added to
    ``cleanup_errors``."""
    if not result.cbdt_json_eligible:
        return (), None, None
    if not json_available:
        return (
            (),
            "automatic Schedule FA JSON was not written because the output "
            "directory contains a file not created by ibkr-fa",
            None,
        )
    if financial_year.assessment_year_start != CBDT_SCHEMA_ASSESSMENT_YEAR_START:
        schema_ay = CBDT_SCHEMA_ASSESSMENT_YEAR_START
        target_ay = financial_year.assessment_year_start
        return (
            (),
            "automatic Schedule FA JSON was not written because the vendored "
            f"schema covers AY {schema_ay}-{str(schema_ay + 1)[-2:]}, not "
            f"AY {target_ay}-{str(target_ay + 1)[-2:]}",
            None,
        )
    try:
        json_outputs, complete, missing = write_fa(
            result,
            output_dir=output_dir,
            format_id=CbdtScheduleFaJsonWriter.format_id,
            financial_year=financial_year,
            itr_form=itr_form,
            json_available=True,
            json_cleanup=json_cleanup,
        )
    except Exception as exc:
        # A staging file the writer could not remove arrives wrapped, as a
        # failure after staging.
        if isinstance(exc, OutputRemovalError) or isinstance(exc.__cause__, OutputRemovalError):
            cleanup_errors.append(exc)
        failure = relayed_failure(exc, "could not write", output_dir / JSON_FILENAME)
        return (), None, f"automatic Schedule FA JSON failed: {failure}"
    if not complete:
        return (
            (),
            None,
            "automatic Schedule FA JSON was not written because required "
            "filing values are incomplete: " + "; ".join(missing),
        )
    return json_outputs, None, None
