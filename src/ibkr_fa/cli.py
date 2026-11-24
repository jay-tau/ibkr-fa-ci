"""``ibkr-fa`` CLI entry point.

Thin wrapper around the ``ibkr_fa.pipeline*`` modules — argparse, human-readable
error rendering, and exit-code policy. No business logic lives here;
everything routes through the pipeline.

Subcommand layout (extensible):

* ``ibkr-fa schedule-fa``       — Schedule FA Table A3 review CSV
  (calendar-year capital-balances pipeline; routes to
  :func:`ibkr_fa.pipeline_fa.run_fa_a3`).
* ``ibkr-fa schedule-os``       — Schedule OS row 1a(i) ordinary
  dividends + row 1b(ix) other interest review output.
* ``ibkr-fa schedule-cg``       — Schedule CG Section A foreign-equity
  STCG review CSV (FY-based; Rule 115 capital-gains FX policy via
  :class:`~ibkr_fa.fx.policy_rule115_capital_gains.Rule115CapitalGainsPolicy`;
  routes to :func:`ibkr_fa.pipeline_cg.run_schedule_cg`).
* ``ibkr-fa schedule-cg-ltcg``  — Schedule CG Section B foreign-equity
  LTCG review CSV (FY-based; new regime — 12.5% without indexation,
  post-2024-07-23 disposals only; routes to
  :func:`ibkr_fa.pipeline_cg_ltcg.run_schedule_cg_ltcg`).
* ``ibkr-fa schedule-fsi``      — Schedule FSI per-(country, head-of-
  income) review CSV / XLSX (FY-based foreign-source income
  aggregator over OS / CG / interest / WHT; routes to
  :func:`ibkr_fa.pipeline_fsi.run_schedule_fsi`).
* ``ibkr-fa schedule-tr``       — Schedule TR per-country tax-relief
  review CSV / XLSX (FY-based aggregator over Schedule FSI's per-
  country foreign-tax-paid totals; routes to
  :func:`ibkr_fa.pipeline_tr.run_schedule_tr`).
* ``ibkr-fa form-67``           — CBDT Form 67 per-(country, source-
  of-income) review CSV (FY-based; reuses the Form 67 entries the
  shared Schedule FSI parse + compute seam already builds; routes
  to :func:`ibkr_fa.pipeline_form67.run_form67`).
* ``ibkr-fa run-all``           — one-shot orchestrator: produces
  every schedule above into ``--output-dir`` from one invocation
  (the six FY schedules from ``--fy-input``; Schedule FA from
  ``--cy-input``, skipped when omitted). Reuses each subcommand's
  handler so per-schedule output + advisories are identical; prints a
  final status-first summary and returns the worst per-schedule exit
  code (see below).
* ``ibkr-fa robinhood-*``       — two-account Robinhood bundle commands
  for CG, income/FTC, Schedule FA, options, and complete all-mode
  orchestration.

Exit codes:

* ``0``  — success AND no manual-fill warnings. ``schedule-os`` /
  ``schedule-cg`` / ``schedule-cg-ltcg`` always emit this on success
  (writer fills every TOTAL cell deterministically). ``schedule-fa``
  emits this when every portal column was populated — which, since
  issue #14, includes the peak-value column once prices are fetched
  (the default) and every holding's price resolved. It still exits 3
  when any column is left blank (e.g. ``--no-fetch-prices``, an
  unpriceable holding, or no Dividends section for the gross-amount
  column).
* ``1``  — generic / unexpected error. An unexpected exception prints its
  class and innermost ``ibkr_fa`` source line, never its message;
  ``IBKR_FA_TRACEBACK=1`` prints Python's full traceback instead, which may
  contain input values.
* ``2``  — **argparse usage error** (missing required flag, unknown
  subcommand, invalid ``--format`` value), argparse's own exit code. The
  error never prints a typed value (#1071): argparse's text prints as it
  stands only when it is a reviewed message that names options and rules
  alone, and any other error prints fixed text naming the option, if there
  is one (see :class:`_NoEchoArgumentParser`). Domain errors
  use code 4 instead so wrappers can distinguish "wrong command
  line" from "wrong data".
* ``3``  — ``schedule-fa``, ``netbenefits-fa``, ``schedule-fsi`` and
  ``schedule-tr`` — success BUT the run reported manual-fill columns.
  ``netbenefits-fa`` always leaves the A2 account peak for manual fill
  because its statements do not provide a complete daily account-value
  series. Every command that writes Schedule FSI or TR emits 3 when a
  relief row's foreign Taxpayer Identification Number is blank (no
  ``[account.foreign_tins]`` entry for its country): ``schedule-fsi`` and
  ``schedule-tr`` (in every ``--format``), ``run-all``,
  ``robinhood-income``, ``robinhood-all``, ``netbenefits-income``,
  ``netbenefits-all``, ``stockplan-income``, ``stockplan-all`` and
  ``fidelity-cma-income``. The files are still written, with that cell
  blank. Every command that writes the Form 67 upload ``form-67.csv`` emits 3
  when a row files a fractional ``Rate(%)``, which the filer enters by hand
  after upload (#1447): ``form-67`` (``--format portal-csv``), ``run-all``
  (default ``--format``), ``robinhood-income``, ``robinhood-all``,
  ``netbenefits-income``, ``netbenefits-all``, ``stockplan-income``,
  ``stockplan-all`` and ``fidelity-cma-income``. The file is still written,
  with the exact rate.
  ``schedule-fsi`` and ``schedule-tr`` (in every ``--format``) also
  emit 3 when a Schedule FSI row with foreign tax has a blank Indian tax
  payable (``schedule-tr``: in a country it writes), the case their
  "INDIAN TAX CALCULATION REQUIRED" warning names; the files are still
  written. ``netbenefits-income`` and ``netbenefits-all`` also emit 3 when
  the Quicko block leaves "Name of the deductor" blank (no
  ``[netbenefits.account].withholding_agent_name``). Distinct from 0 so CI / shell
  scripts can detect "review CSV needs manual editing before paste"
  without parsing stderr. ``schedule-cg``, ``schedule-cg-ltcg``,
  ``fidelity-cma-cg`` and ``netbenefits-cg`` (and ``run-all`` and
  ``netbenefits-all`` through them) emit 3 when a review file leaves its
  Schedule CG Table F date bands blank because a band nets a loss and two
  or more bands net gains (#1361); the files are still written.
  ``schedule-os`` never emits 3.
* ``4``  — pipeline raised a documented error
  (:class:`ConfigError`, :class:`ParseError`,
  :class:`MissingFxRateError`, :class:`CountryCodeNotFoundError`,
  :class:`UnknownOutputFormatError`, an :class:`OSError` such as
  :class:`FileNotFoundError`), or a value was too large or too precise to
  compute (a :class:`decimal.DecimalException`), also while rendering the
  report after the outputs were written: the outputs are then removed.

``ibkr-fa run-all`` returns the worst per-schedule outcome: ``1`` if any
schedule hit an unexpected internal error, else ``4`` if any raised a
documented domain error, else ``3`` if any reported manual-fill columns,
else ``0``. An intentionally skipped Schedule FA (no ``--cy-input``)
does not affect the code, but is flagged loudly in the summary.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import functools
import io
import os
import re
import stat
import sys
from collections.abc import Callable, Iterable, Sequence
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, DecimalException, InvalidOperation
from enum import Enum, auto
from pathlib import Path
from typing import Any, Final, NamedTuple, NoReturn, Protocol, TextIO

from ibkr_fa.compliance import (
    BMA_DISCLOSURE_ADVISORY,
    ITR_FORM_SELECTION_ADVISORY,
    missing_indian_tax_payable_warning,
)
from ibkr_fa.compliance.itr_form_selection import ITR_FORMS, ItrForm
from ibkr_fa.compute import (
    LTCG_SECTION_112_VS_112A_ADVISORY,
    STCG_SLAB_VS_111A_ADVISORY,
)
from ibkr_fa.compute.dec31_close import CloseSource
from ibkr_fa.compute.espp_perquisite import EsppPerquisiteAdvisory
from ibkr_fa.compute.form67 import Form67Readiness, Form67RefusalReason
from ibkr_fa.compute.schedule_cg_table_f import table_f_loss_across_bands_reason
from ibkr_fa.compute.schedule_fsi import require_fsi_write_ready
from ibkr_fa.compute.wht_reversal_pairing import WhtCrossFyReversalReport
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.config_template import write_user_toml_template
from ibkr_fa.exceptions import (
    DOMAIN_ERRORS,
    PathRefusalError,
    crash_site,
    errno_name,
    first_crash,
)
from ibkr_fa.fx import policy_citations as fx_policy_citations
from ibkr_fa.fx.context import CROSS_AY_FX_WARNING_STEP
from ibkr_fa.models.fa_a2_row import FaA2ClosingBlankReason, FaA2Row
from ibkr_fa.models.fa_a3_row import FaA3PeakBlankReason
from ibkr_fa.models.form67_entry import Form67TreatyRateStatus
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.output import (
    EXCLUDED_CG_INDIAN_SOURCE_MARKER,
    EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER,
    EXCLUDED_LTCG_DEFERRED_MARKER,
    EXCLUDED_LTCG_OUT_OF_FY_MARKER,
    EXCLUDED_OUT_OF_FY_MARKER,
    EXCLUDED_PRE_CUTOVER_MARKER,
    EXCLUDED_STCG_MARKER,
    OutputCatalogError,
    Subcommand,
    command_formats,
)
from ibkr_fa.output._cleanup import (
    OutputCollisionError,
    OutputRemovalError,
    RunOutputs,
    refuse_input_collisions,
    remove_failed_run_outputs,
    remove_outputs_on_failure,
    remove_previous_outputs,
)
from ibkr_fa.output.cbdt_json_fragment import (
    CbdtJsonIncompleteError,
    CbdtJsonUnreadableError,
    CbdtScheduleFaJsonWriter,
    preclear_own_cbdt_fragment,
)
from ibkr_fa.output.cbdt_json_merge import (
    CbdtMergeReport,
    merge_cbdt_fragment,
)
from ibkr_fa.output.cbdt_json_schema import schema_version_for
from ibkr_fa.output.fa_manual_fill import (
    FA_A2_CLOSING_BLANK_TEXT,
    FA_A2_CLOSING_LABEL,
    FA_A2_ZIP_CODE_LABEL,
    FA_A3_PEAK_BLANK_TEXT,
    FA_A3_PEAK_VALUE_COLUMN,
    FA_A3_ZIP_CODE_COLUMN,
)
from ibkr_fa.output.income_bundle import (
    INCOME_FILENAMES,
    INCOME_REVIEW_FILENAMES,
    income_output_names,
)
from ibkr_fa.output.netbenefits_quicko_fa_xlsx import QUICKO_TEMPLATE_FILENAME
from ibkr_fa.output.portal_labels import for_ay as portal_labels_for_ay
from ibkr_fa.output.schedules.form67 import fractional_rate_warnings
from ibkr_fa.pii import display_account_id
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_cg import ScheduleCgRunReport, run_schedule_cg
from ibkr_fa.pipeline_cg_ltcg import ScheduleCgLtcgRunReport, run_schedule_cg_ltcg
from ibkr_fa.pipeline_fa import RunReport, run_fa_a3
from ibkr_fa.pipeline_fa_a2 import fa_a2_path
from ibkr_fa.pipeline_fidelity_cma_cg import (
    FidelityCmaCgRunReport,
    run_fidelity_cma_cg,
)
from ibkr_fa.pipeline_fidelity_cma_fa import (
    A2_PEAK_MANUAL_FILL,
    A3_PEAK_MANUAL_FILL,
    FidelityCmaFaRunReport,
    run_fidelity_cma_fa,
)
from ibkr_fa.pipeline_fidelity_cma_income import (
    FidelityCmaIncomeRunReport,
    run_fidelity_cma_income,
)
from ibkr_fa.pipeline_form67 import Form67RunReport, run_form67
from ibkr_fa.pipeline_form67_models import Form67PerEntryRow
from ibkr_fa.pipeline_fsi import ScheduleFsiRunReport, run_schedule_fsi
from ibkr_fa.pipeline_fsi_compute import _compute_schedule_fsi_for_pipeline, _FsiReuseScope
from ibkr_fa.pipeline_netbenefits_cg import NetBenefitsCgRunReport, run_netbenefits_cg
from ibkr_fa.pipeline_netbenefits_fa import (
    NetBenefitsFaRunReport,
    netbenefits_fa_sibling_paths,
    netbenefits_inputs,
    run_netbenefits_fa,
)
from ibkr_fa.pipeline_netbenefits_income import (
    NetBenefitsIncomeRunReport,
    netbenefits_form67_readiness,
    run_netbenefits_income,
)
from ibkr_fa.pipeline_os import ScheduleOsRunReport, run_schedule_os
from ibkr_fa.pipeline_robinhood import (
    RobinhoodAllRemovalError,
    RobinhoodAllReport,
    RobinhoodLegReport,
    RobinhoodLegStatus,
    run_robinhood_all,
    run_robinhood_cg,
    run_robinhood_fa,
    run_robinhood_income,
    run_robinhood_options,
)
from ibkr_fa.pipeline_stockplan_fa import (
    StockplanFaRunReport,
    inspect_stockplan_cg_evidence,
    run_stockplan_fa,
    stockplan_inputs,
)
from ibkr_fa.pipeline_stockplan_income import (
    StockplanIncomeRunReport,
    run_stockplan_income,
    stockplan_form67_readiness,
)
from ibkr_fa.pipeline_tr import ScheduleTrRunReport, run_schedule_tr
from ibkr_fa.pipeline_tr_models import TrPerCountryRow
from ibkr_fa.prices.cache import DEFAULT_PRICE_CACHE_DIR_NAME

_PROG_NAME: Final[str] = "ibkr-fa"
_EXIT_OK: Final[int] = 0
_EXIT_GENERIC: Final[int] = 1
# 2 is argparse's usage-error exit (stdlib convention), which
# _NoEchoArgumentParser keeps while printing only reviewed or fixed text,
# never a typed value (#1071). Commands that find a usage error themselves
# return this constant.
_EXIT_ARGPARSE_USAGE: Final[int] = 2
_EXIT_MANUAL_FILL_REQUIRED: Final[int] = 3
_EXIT_DOMAIN_ERROR: Final[int] = 4
_DOMAIN_ERRORS: Final = DOMAIN_ERRORS
#: A filed USD figure is supplied as whole cents — at most 2 decimal places.
_MAX_USD_DECIMAL_PLACES: Final[int] = 2

# Shared text for the "account ID cross-check was skipped" warning. Emitted
# (unchanged) by every per-schedule renderer when a masked account is paired
# with masked-account acceptance (the --allow-masked-account flag or the
# [account].allow_masked_account opt-in), unless ``run-all`` prints it once
# itself (see _run_all_global_advisories).
_MASKED_ACCOUNT_SKIP_WARNING: Final[str] = (
    "⚠️  Account ID cross-check was SKIPPED because the input statement "
    "masked the account number AND masked-account acceptance is enabled "
    "(--allow-masked-account or [account].allow_masked_account in user.toml). "
    "You are responsible for confirming the statement belongs to the same "
    "account as user.toml before filing."
)

_FINANCIAL_YEAR_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<start>\d{4})-(?P<end>\d{2}|\d{4})$")

# Shared nudge for a blank foreign Taxpayer Identification Number on a
# relief-claiming Schedule FSI / TR row. The foreign TIN is mandatory when
# claiming §90/90A/91 foreign-tax relief, but it is NOT in the IBKR
# statement, so it must come from [account.foreign_tins] in user.toml. FSI
# and TR both emit it, unless ``run-all`` prints it once itself (see
# _run_all_global_advisories); it is intentionally country-independent (the
# blank cells are visible in the output).
_FOREIGN_TIN_MISSING_ADVISORY: Final[str] = (
    "ℹ️  Foreign Taxpayer Identification Number is BLANK for one or more "  # noqa: RUF001
    "countries where foreign tax was paid. On Schedule FSI / TR this number "
    "is generally MANDATORY when claiming foreign-tax relief under section "
    "90 / 90A / 91 — leaving it blank can delay processing or cause the "
    "relief to be denied. The IBKR statement does not carry your foreign "
    "TIN, so add it to user.toml under [account.foreign_tins], keyed by the "
    'country\'s ISO 3166-1 alpha-2 code (e.g. US = "123456789"; run '
    "`ibkr-fa init` for an annotated template), then re-run — one entry "
    "fills BOTH Schedule FSI and Schedule TR. If that country did not allot "
    "you a TIN, enter your passport number instead."
)

# The warning a run prints when the FX policy isn't verified for the
# assessment year: the NetBenefits and StockPlan wording, then the Fidelity
# CMA wording.
_CROSS_AY_FX_WARNING: Final[str] = (
    "warning: the FX policy has not been verified for this assessment year; "
    f"{CROSS_AY_FX_WARNING_STEP}"
)
_CMA_CROSS_AY_FX_WARNING: Final[str] = (
    "warning: the FX policy has not been verified for this assessment year; "
    "pass --suppress-cross-ay-warning after independent verification."
)

# Schedule FA Table A2 was NOT generated because [account.custodian] is
# unconfigured. Emitted by `_render_run_report` (schedule-fa); run-all lists
# its own "Before you file" item when the FA report has no A2 rows.
_FA_A2_SKIPPED_ADVISORY: Final[str] = (
    "ℹ️  Schedule FA Table A2 (the foreign custodial account itself) was "  # noqa: RUF001
    "NOT generated because [account.custodian] is not configured in "
    "user.toml. ITR-2 requires disclosing the IBKR account in Table A2 "
    "(foreign custodial account) IN ADDITION TO the holdings inside it "
    "in Table A3 (written above) — Table A3 on its own is an incomplete "
    "Schedule FA disclosure. The custodian's name / address / country "
    "are not carried in the IBKR statement, so they must be supplied: "
    "add an [account.custodian] block to user.toml (run `ibkr-fa init` "
    "for an annotated template) and re-run to emit Table A2."
)


def _traceback_requested() -> bool:
    """Whether ``IBKR_FA_TRACEBACK=1`` asks for Python's full traceback instead
    of the fixed error line (#1049). Off by default: a traceback can print
    input values."""
    return os.environ.get("IBKR_FA_TRACEBACK") == "1"


def _holds_crash(exc: BaseException) -> bool:
    """Whether ``exc``'s cause or context chain holds an exception other than
    one of :data:`_DOMAIN_ERRORS`, such as the stand-in for a crash its cleanup
    replaced (:func:`~ibkr_fa.exceptions.raise_replacing`)."""
    return first_crash(exc.__cause__, exc.__context__) is not None


def _raise_for_traceback(exc: Exception) -> None:
    """Re-raise domain error ``exc`` under ``IBKR_FA_TRACEBACK=1`` when its chain
    holds a crash, so Python prints the whole chain after cleanup has run."""
    if _traceback_requested() and _holds_crash(exc):
        raise exc


def _filesystem_problem(operation: str, exc: OSError, path: Path | None = None) -> str:
    """``operation`` (fixed text), ``path`` and ``exc``'s errno name, such as
    ``could not write user.toml (EACCES)``.

    ``path`` is the one the caller used: a path the user typed, or a fixed name
    joined to one. Never ``exc``'s text or file names, which may hold any text
    or paths the user never typed (#1049).
    """
    where = "" if path is None else f" {path}"
    return f"{operation}{where} ({errno_name(exc)})"


def _domain_error(command: str, exc: Exception, *, prefix: str = "") -> int:
    """Print ``command``'s refusal for ``exc``, one of :data:`_DOMAIN_ERRORS`,
    and return exit 4. A ``decimal`` exception is named by its class only and
    an ``OSError`` by its errno name, after fixed text, so no value is printed;
    any other error by its own message, after ``prefix``.

    Under ``IBKR_FA_TRACEBACK=1``, re-raises ``exc`` instead when its chain
    holds a crash (see :func:`_raise_for_traceback`).
    """
    _raise_for_traceback(exc)
    if isinstance(exc, DecimalException):
        message = (
            f"{command}: a value in the input is too large or too precise to compute "
            f"({type(exc).__name__})"
        )
    elif isinstance(exc, OSError):
        message = f"{command}: {_filesystem_problem('a file operation failed', exc)}"
    else:
        message = f"{prefix}{exc}"
    print(f"error: {message}", file=sys.stderr)
    return _EXIT_DOMAIN_ERROR


class _NullConsole(io.TextIOBase):
    """A console stream that discards everything written to it."""

    def write(self, text: str) -> int:
        return len(text)


def _console_settled(stream: TextIO) -> bool:
    """Whether ``stream`` flushes, after pointing it at ``os.devnull`` if it didn't."""
    try:
        stream.flush()
    except OSError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, stream.fileno())
            finally:
                os.close(devnull)
            stream.flush()
        except OSError:
            return False
    return True


def _settle_broken_console() -> None:
    """Point each standard stream that can no longer flush at ``os.devnull``.

    A failed write or flush, such as ``BrokenPipeError`` once a pipe's reader
    has exited, leaves its text buffered, and Python flushes the standard
    streams again at exit: that flush would fail too and turn the exit code
    into 120. This is the ``signal`` docs' remedy for ``BrokenPipeError``.
    Restoring the default ``SIGPIPE`` action instead would kill the process at
    the failed write, before it removed its outputs. A stream that still can't
    flush, such as when no file descriptor is left to open ``os.devnull``, is
    replaced by one that discards its text (#1049).
    """
    if not _console_settled(sys.stdout):
        sys.stdout = _NullConsole()
    if not _console_settled(sys.stderr):
        sys.stderr = _NullConsole()


def _all_command_refusal(command: str, exc: Exception) -> int:
    """Refuse an all-command once its cleanup scope has attempted every removal.

    Reports ``exc`` through :func:`_domain_error` while stderr still works, and
    drops the message if it doesn't. Then settles the console (see
    :func:`_settle_broken_console`), so a failed console write exits 4 (#925).
    The ``IBKR_FA_TRACEBACK=1`` re-raise happens first, outside that
    suppression, even for an ``OSError`` (#1049).
    """
    _raise_for_traceback(exc)
    with contextlib.suppress(OSError):
        _domain_error(command, exc)
    _settle_broken_console()
    return _EXIT_DOMAIN_ERROR


def _table_f_manual_fill_lines(loss_across_bands: Iterable[tuple[int, str]]) -> list[str]:
    """The stderr line for each Schedule CG Table F row a review file leaves
    blank because a band nets a loss and two or more bands net gains (#1361).

    Each names the row and the file, never a figure, date or symbol.
    """
    return [
        f"⚠️  MANUAL-FILL: {table_f_loss_across_bands_reason(row, file_name)}."
        for row, file_name in loss_across_bands
    ]


def _table_f_exit_code(loss_across_bands: tuple[tuple[int, str], ...]) -> int:
    """3 when a review file leaves Table F date bands for the filer (#1361), else 0."""
    return _EXIT_MANUAL_FILL_REQUIRED if loss_across_bands else _EXIT_OK


def _print_report(run_outputs: RunOutputs, stderr_lines: Iterable[str], report: str) -> None:
    """Print a rendered report: its stderr lines, then the stdout report, flushed.

    Prints and flushes in the scope of ``run_outputs``, the run's outputs, as
    the command rendered it
    (:meth:`~ibkr_fa.output._cleanup.RunOutputs.removed_on_failure`). A failed
    write or flush, such as ``BrokenPipeError`` once a pipe's reader has
    exited, attempts every removal before it propagates; if a removal fails
    too, the scope's :class:`OutputRemovalError` propagates in its place
    (#1227). The flush makes a block-buffered stdout, a pipe or a file, write
    the report here rather than at exit, outside every cleanup scope (#1236).
    """
    with run_outputs.removed_on_failure():
        for line in stderr_lines:
            print(line, file=sys.stderr)
        print(report, flush=True)


class _CommandReport(Protocol):
    """A command's run report: the outputs its run published."""

    @property
    def run_outputs(self) -> RunOutputs: ...


def _run_report_command[R: _CommandReport](
    command: str,
    run: Callable[[], R],
    render: Callable[[R], tuple[Iterable[str], str]],
    exit_code: Callable[[R], int],
) -> int:
    """Run ``command``'s pipeline, print its report and return its exit code.

    ``run()`` runs the pipeline. ``render(report)`` returns the stderr lines and
    the stdout report, rendered in the scope of the run's outputs, so a renderer
    failure attempts every removal first. Either step's domain error refuses
    through :func:`_domain_error`. :func:`_print_report` prints outside that
    refusal handling, and ``exit_code(report)`` then picks the exit code.
    """
    try:
        report = run()
        with report.run_outputs.removed_on_failure():
            warnings, rendered = render(report)
    except _DOMAIN_ERRORS as exc:
        return _domain_error(command, exc)
    _print_report(report.run_outputs, warnings, rendered)
    return exit_code(report)


#: The fixed texts the ``type=`` parsers below raise. They state a rule, never
#: the value, and are the only ``type=`` texts an argparse error prints as they
#: stand (see :class:`_NoEchoArgumentParser`).
_FY_SHAPE_ERROR: Final = (
    "--financial-year must be in YYYY-YY or YYYY-YYYY form "
    "(e.g., '2025-26' or '2025-2026' for the April 2025 - March 2026 period)."
)
_FY_SHORT_END_ERROR: Final = (
    "--financial-year's two-digit end year must be the last two digits "
    "of its start year + 1 (e.g., '2025-26')."
)
_FY_LONG_END_ERROR: Final = (
    "--financial-year's end year must equal its start year + 1 "
    "(e.g., '2025-2026'; an Indian FY spans exactly one year, April 1 - March 31)."
)
_FY_START_ERROR: Final = "--financial-year must start in a year from 1900 to 9998."
_USD_SHAPE_ERROR: Final = "must be a USD amount, such as 100.25"
_USD_SIGN_ERROR: Final = "must be a non-negative, finite USD amount"
_USD_CENTS_ERROR: Final = "must have at most 2 decimal places (whole cents)"
_USD_DIGITS_ERROR: Final = NumberKind.MONEY.refusal
_REVIEWED_TYPE_ERRORS: Final = frozenset(
    {
        _FY_SHAPE_ERROR,
        _FY_SHORT_END_ERROR,
        _FY_LONG_END_ERROR,
        _FY_START_ERROR,
        _USD_SHAPE_ERROR,
        _USD_SIGN_ERROR,
        _USD_CENTS_ERROR,
        _USD_DIGITS_ERROR,
    }
)


def _parse_financial_year(spec: str) -> FinancialYear:
    """Parse ``"YYYY-YY"`` or ``"YYYY-YYYY"`` into a :class:`FinancialYear`.

    Used as an argparse ``type=`` callable so the CLI surface accepts the
    canonical ITR-portal label (e.g., ``"2025-26"``) and the long form
    (e.g., ``"2025-2026"``) interchangeably, normalising both to the
    typed dataclass before the pipeline sees them.

    Raises:
        argparse.ArgumentTypeError: If ``spec`` doesn't match the
            expected pattern, or if the end year doesn't equal
            start year + 1 (e.g., ``"2025-27"`` is rejected — Indian
            FYs always span exactly one year).
    """
    match = _FINANCIAL_YEAR_RE.fullmatch(spec)
    if match is None:
        raise argparse.ArgumentTypeError(_FY_SHAPE_ERROR)
    start_year = int(match.group("start"))
    end_token = match.group("end")
    expected_end_short = (start_year + 1) % 100
    if len(end_token) == 2:  # noqa: PLR2004 — short-form FY tag width
        end_value = int(end_token)
        if end_value != expected_end_short:
            raise argparse.ArgumentTypeError(_FY_SHORT_END_ERROR)
    else:
        end_value = int(end_token)
        if end_value != start_year + 1:
            raise argparse.ArgumentTypeError(_FY_LONG_END_ERROR)
    try:
        return FinancialYear(start_year=start_year)
    except Exception as exc:  # InvariantError for a start year outside 1900-9998
        raise argparse.ArgumentTypeError(_FY_START_ERROR) from exc


def _parse_usd_2dp(spec: str) -> Decimal:
    """Parse a non-negative USD amount with at most 2 decimals (a filed cent figure)."""
    try:
        value = Decimal(spec)
    except (InvalidOperation, ValueError) as exc:
        raise argparse.ArgumentTypeError(_USD_SHAPE_ERROR) from exc
    if not value.is_finite() or value < 0:
        raise argparse.ArgumentTypeError(_USD_SIGN_ERROR)
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)  # finite (checked above) -> integer exponent
    # Reject more than 2 LITERAL decimal places (e.g. "1.2300") — Decimal `==`
    # ignores the exponent, so a numeric-equality check would let those through.
    # Checking the exponent also avoids a quantize() that raises on huge inputs.
    if exponent < -_MAX_USD_DECIMAL_PLACES:
        raise argparse.ArgumentTypeError(_USD_CENTS_ERROR)
    if number_limit_breach(value, NumberKind.MONEY) is not None:
        raise argparse.ArgumentTypeError(_USD_DIGITS_ERROR)
    return value


def _add_accept_anonymised_statement_argument(sub: argparse.ArgumentParser) -> None:
    """Attach the shared ``--accept-anonymised-statement`` flag (#141).

    IBKR's Activity Statement exporter allows hiding the ``Name`` and
    ``Account`` rows from the Account Information section for privacy
    (seen in a real private statement). Without this flag the
    parser refuses such anonymised exports as a safety default.
    Passing the flag opts into a fallback path where the dispatcher
    backfills the absent ``Name`` / ``Account`` values from
    ``[account].holder_name`` and ``[account].number`` in
    ``user.toml``; the CLI then surfaces a loud warning to stderr
    that the run is built on user-attested identity rather than
    statement-verified identity.

    Per-subparser duplication mirrors the existing pattern for
    ``--allow-masked-account`` and ``--suppress-cross-ay-warning``
    (each subcommand has its own copy of the flag rather than a
    parent-parser inheritance chain — this keeps argparse's per-
    subcommand --help output complete and avoids the parent-parser
    `set_defaults` pitfall when a future subcommand legitimately
    needs different behaviour). The flag is orthogonal to
    ``--allow-masked-account`` (which handles a present-but-masked
    Account row); both can coexist on the same invocation.
    """
    sub.add_argument(
        "--accept-anonymised-statement",
        dest="accept_anonymised_statement",
        action="store_true",
        help=(
            "Accept Activity Statement exports that hide the 'Name' "
            "and/or 'Account' rows from the Account Information section "
            "(IBKR's privacy export option). Without this flag the "
            "parser refuses such exports — re-export from IBKR with all "
            "account-identity fields enabled, or pass this flag to "
            "fall back to [account].holder_name and [account].number "
            "from user.toml. When fallback is used the run is built on "
            "user-attested identity rather than statement-verified "
            "identity — a loud warning is surfaced via the run output "
            "(stderr for schedule-os / schedule-cg / schedule-cg-ltcg "
            "via the safety-warning block; stdout for schedule-fa as "
            "part of the run summary) and you are responsible for "
            "confirming the statement belongs to the same account as "
            "user.toml before filing. Orthogonal to "
            "--allow-masked-account (which handles a present-but-"
            "masked Account row, e.g. 'U**********'). No effect on "
            "Flex Query inputs — Flex's Account Information has no "
            "hide-at-export option, and the Flex dispatcher always "
            "returns an empty fallback-used set."
        ),
    )


def _add_itr_form_argument(
    sub: argparse.ArgumentParser, *, required: bool, help_text: str | None = None
) -> None:
    sub.add_argument(
        "--itr-form",
        choices=ITR_FORMS,
        required=required,
        default=None,
        help=help_text
        or (
            "Target return form. Required because this command emits form-specific "
            "Schedule CG/TR labels. Use ITR-2 when you have no business/profession "
            "income; use ITR-3 when you do."
            if required
            else "Target return form for filing-csv or cbdt-json-fragment. Required only "
            "for the standalone JSON artifact; filing-csv defaults to ITR-2."
        ),
    )


def _add_debug_form67_argument(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--debug-form67",
        action="store_true",
        help=(
            "Also emit form-67-debug.csv with audit columns. This debug file is "
            "NOT compatible with the CBDT upload template; upload form-67.csv only."
        ),
    )


def _add_debug_argument(sub: argparse.ArgumentParser, *, help_text: str | None = None) -> None:
    sub.add_argument(
        "--debug",
        "--debug-form67",
        dest="debug",
        action="store_true",
        help=help_text
        or (
            "Also emit non-filing audit CSVs for Schedule OS, FSI, TR, and Form 67. "
            "Use the files without '-debug' for filing."
        ),
    )


_NETBENEFITS_FY_HELP = (
    "FY in YYYY-YY or YYYY-YYYY form (e.g., 2025-26 for the April "
    "2025 - March 2026 period; AY 2026-27)."
)
_FIDELITY_CMA_FY_HELP = "Indian FY in YYYY-YY or YYYY-YYYY form (for example, 2025-26)."
_FIDELITY_CMA_SUPPRESS_CROSS_AY_HELP = "Suppress the unverified cross-AY FX-policy warning."


def _add_financial_year_argument(sub: argparse.ArgumentParser, *, help_text: str) -> None:
    sub.add_argument(
        "--financial-year",
        dest="financial_year",
        type=_parse_financial_year,
        required=True,
        metavar="FY",
        help=help_text,
    )


def _add_suppress_cross_ay_warning_argument(
    sub: argparse.ArgumentParser, *, help_text: str | None = None
) -> None:
    sub.add_argument(
        "--suppress-cross-ay-warning",
        dest="suppress_cross_ay_warning",
        action="store_true",
        help=help_text,
    )


def _add_netbenefits_config_argument(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to user.toml (must contain a [netbenefits] block).",
    )


def _add_fidelity_cma_input_argument(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="DIR",
        help="Directory containing Fidelity CMA monthly and year-end statement PDFs.",
    )


def _add_fidelity_cma_config_argument(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to user.toml (must contain a [fidelity_cma] block).",
    )


def _add_robinhood_common_arguments(
    parser: argparse.ArgumentParser, *, itr_form_help: str | None = None
) -> None:
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="DIR",
        help=(
            "Robinhood bundle directory containing activity/, statements/, and optional tax-forms/."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to user.toml with the complete [robinhood] bundle manifest.",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        type=Path,
        required=True,
        metavar="DIR",
        help=(
            "Dedicated Robinhood output directory; do not share it with another source. "
            "If it doesn't exist, the command creates it, readable only by you; an "
            "existing one must be yours and private (chmod 700), or the run refuses."
        ),
    )
    parser.add_argument(
        "--financial-year",
        dest="financial_year",
        type=_parse_financial_year,
        required=True,
        metavar="FY",
        help=(
            "Indian FY in YYYY-YY form. Schedule FA uses the FY start year "
            "as its calendar year and the bundle must cover both periods."
        ),
    )
    _add_itr_form_argument(parser, required=True, help_text=itr_form_help)


def _add_robinhood_price_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-fetch-prices",
        dest="no_fetch_prices",
        action="store_true",
        help=(
            "Use validated equity and option caches only; never access the network. "
            "Schedule FA refuses if required cached evidence is incomplete."
        ),
    )
    parser.add_argument(
        "--price-cache-dir",
        dest="price_cache_dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Integrity-checked equity and option cache directory; defaults to "
            "<input>/.ibkr-fa-price-cache."
        ),
    )


def _add_format_argument(
    parser: argparse.ArgumentParser, command: str, help_text: str | None = None
) -> None:
    """Add ``command``'s ``--format``: its declared choices and default (#1206)."""
    formats = command_formats(command)
    parser.add_argument(
        "--format",
        dest="format_id",
        choices=formats.choices,
        default=formats.default,
        help=help_text,
    )


def _add_robinhood_parsers(subparsers: argparse._SubParsersAction) -> None:
    cg = subparsers.add_parser(
        "robinhood-cg",
        help="Produce reconciled Robinhood equity Schedule CG outputs.",
    )
    _add_robinhood_common_arguments(cg)
    _add_robinhood_price_arguments(cg)
    _add_debug_argument(
        cg,
        help_text=(
            "Also write the Schedule CG review sidecars schedule-cg-debug.<ext> and "
            "schedule-cg-ltcg-debug.<ext> (<ext> from --format), and add the zero-valued "
            "fields and portal-calculated rows to schedule-cg.xlsx. File from "
            "schedule-cg.xlsx; the -debug files are not filing files."
        ),
    )
    _add_format_argument(
        cg,
        "robinhood-cg",
        help_text=(
            "Debug sidecar format; the non-debug filing output is always "
            "the portal-shaped schedule-cg.xlsx workbook."
        ),
    )

    income = subparsers.add_parser(
        "robinhood-income",
        help="Produce Robinhood Schedule OS/FSI/TR/Form 67 outputs.",
    )
    _add_robinhood_common_arguments(income)
    _add_robinhood_price_arguments(income)
    _add_debug_argument(income)
    _add_suppress_cross_ay_warning_argument(income)

    fa = subparsers.add_parser(
        "robinhood-fa",
        help="Produce two-account Robinhood Schedule FA A2/A3 outputs.",
    )
    _add_robinhood_common_arguments(
        fa,
        itr_form_help=(
            "Target return form. Required because it selects the form of the CBDT JSON "
            "fragment (--format cbdt-json-fragment), because filing-csv checks each "
            "Schedule FA country against that form's dropdown, and because reviewed "
            "business treatment of Robinhood options needs ITR-3. Use ITR-2 when you "
            "have no business/profession income; use ITR-3 when you do."
        ),
    )
    _add_robinhood_price_arguments(fa)
    _add_format_argument(fa, "robinhood-fa")

    options = subparsers.add_parser(
        "robinhood-options",
        help="Produce the Robinhood option filing/review CSV.",
    )
    _add_robinhood_common_arguments(options)

    all_parser = subparsers.add_parser(
        "robinhood-all",
        help="Produce every supported Robinhood schedule and completeness status.",
        description=(
            "Runs every Robinhood leg and prints each one's status, exiting with "
            "the worst. If any leg fails, it removes every file this run wrote "
            "(a schedule-fa.json it did not write is kept), so a failed run "
            "leaves nothing to file."
        ),
    )
    _add_robinhood_common_arguments(all_parser)
    _add_robinhood_price_arguments(all_parser)
    _add_debug_argument(
        all_parser,
        help_text=(
            "Also write the non-filing review files schedule-cg-debug.<ext> and "
            "schedule-cg-ltcg-debug.<ext> (<ext> from --format), schedule-os-debug.csv, "
            "schedule-fsi-debug.csv, schedule-tr-debug.csv and form-67-debug.csv, and add "
            "the zero-valued fields and portal-calculated rows to schedule-cg.xlsx. Use "
            "the files without '-debug' for filing."
        ),
    )
    _add_suppress_cross_ay_warning_argument(all_parser)
    _add_format_argument(
        all_parser,
        "robinhood-all",
        help_text="Shared Schedule FA/CG review format for all-mode.",
    )


def _income_debug_outputs_line(output_dir: Path) -> str:
    paths = ", ".join(str(output_dir / filename) for filename in INCOME_REVIEW_FILENAMES)
    return f"Wrote non-filing debug audits: {paths}."


def _render_income_warnings(
    report: NetBenefitsIncomeRunReport | StockplanIncomeRunReport, *, debug: bool, output_dir: Path
) -> list[str]:
    """The stderr lines a NetBenefits or StockPlan income run prints before its report."""
    lines = []
    if not report.fx_policy_appropriate_for_ay:
        lines.append(_CROSS_AY_FX_WARNING)
    lines.extend(f"warning: {warning}" for warning in report.manual_fill_warnings)
    if debug:
        lines.append(_income_debug_outputs_line(output_dir))
    return lines


def _build_run_all_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``run-all`` one-shot orchestrator subcommand.

    Produces every schedule into ``--output-dir`` from a single
    invocation. The six financial-year schedules read ``--fy-input``;
    Schedule FA reads ``--cy-input`` (a calendar-year statement) and is
    skipped when it is omitted. Mirrors the per-subcommand flag-
    duplication convention (see
    :func:`_add_accept_anonymised_statement_argument`).
    """
    run_all = subparsers.add_parser(
        "run-all",
        help="Produce every ITR-2/ITR-3 foreign-income schedule in one run.",
        description=(
            "One-shot orchestrator: produces all seven ibkr-fa schedules "
            "into --output-dir from a single invocation, reusing each "
            "subcommand's handler so the per-schedule output and safety "
            "advisories are identical to running them individually. The six "
            "financial-year schedules (schedule-os / -cg / -cg-ltcg / -fsi / "
            "-tr / form-67) read --fy-input; Schedule FA reads --cy-input (a "
            "calendar-year statement). Schedule FA's calendar year is derived "
            "from --financial-year (FY YYYY-YY maps to Schedule FA calendar "
            "year YYYY); when --cy-input is omitted, FA is SKIPPED and the "
            "remaining six run. run-all runs every schedule even after one "
            "fails, and prints a final status-first summary to stderr; the "
            "exit code reflects the worst per-schedule outcome. If any "
            "schedule fails, run-all removes every file this run wrote, so a "
            "failed run leaves nothing to file. A failed write to the console "
            "(for example, to a closed pipe) instead stops the run at once: "
            "run-all attempts to remove every schedule's output files and exits 4. Each "
            "schedule's target files are "
            "deleted before its run, and Schedule FA's even when it is skipped, so "
            "no stale file is left behind."
        ),
    )
    run_all.add_argument(
        "--fy-input",
        dest="fy_input",
        type=Path,
        required=True,
        help=(
            "IBKR Activity Statement covering the financial year (Apr-Mar). "
            "Used for schedule-os / -cg / -cg-ltcg / -fsi / -tr / form-67."
        ),
    )
    run_all.add_argument(
        "--cy-input",
        dest="cy_input",
        type=Path,
        default=None,
        help=(
            "IBKR Activity Statement ending on Dec 31 of the calendar year "
            "whose year equals the --financial-year start year, and starting "
            "on or before Jan 1 of it. Used for "
            "Schedule FA. When omitted, Schedule FA is skipped (the other "
            "six schedules still run), and any schedule-fa.csv/.xlsx or "
            "schedule-fa-a2.csv/.xlsx an earlier run left in --output-dir is "
            "deleted."
        ),
    )
    run_all.add_argument(
        "--config", dest="config", type=Path, required=True, help="Path to user.toml."
    )
    run_all.add_argument(
        "--output-dir",
        dest="output_dir",
        type=Path,
        required=True,
        help=(
            "Directory for the generated schedule files (created if absent). "
            "Each schedule writes '<name>.<ext>' (ext: csv for review-csv, "
            "xlsx for xlsx)."
        ),
    )
    run_all.add_argument(
        "--financial-year",
        dest="financial_year",
        type=_parse_financial_year,
        required=True,
        help=(
            "FY in YYYY-YY or YYYY-YYYY form (e.g., 2025-26). Schedule FA "
            "uses calendar year = the FY start year."
        ),
    )
    _add_format_argument(
        run_all,
        "run-all",
        help_text=(
            "Output format applied to schedules (default: review-csv). Under "
            "that default, Form 67 alone uses the official portal-csv; explicit "
            "xlsx keeps Form 67 as xlsx."
        ),
    )
    _add_debug_form67_argument(run_all)
    run_all.add_argument(
        "--allow-masked-account",
        dest="allow_masked_account",
        action="store_true",
        help="Forwarded to every schedule. See `schedule-fa --allow-masked-account`.",
    )
    run_all.add_argument(
        "--suppress-cross-ay-warning",
        dest="suppress_cross_ay_warning",
        action="store_true",
        help=(
            "Forwarded to every schedule. Suppresses the AY 2027-28+ cross-AY-verification warning."
        ),
    )
    run_all.add_argument(
        "--quiet",
        dest="quiet",
        action="store_true",
        help=(
            "Forwarded to the FY schedules to suppress their verbose stdout "
            "report (schedule-fa ignores it). The final run-all summary and "
            "all safety advisories still print."
        ),
    )
    run_all.add_argument(
        "--no-fetch-prices",
        dest="no_fetch_prices",
        action="store_true",
        help="Forwarded to Schedule FA. Skip fetching prices for the peak-value column (#14).",
    )
    run_all.add_argument(
        "--price-cache-dir",
        dest="price_cache_dir",
        type=Path,
        default=None,
        help=(
            "Forwarded to Schedule FA. Defaults to "
            "'<output-dir>/.ibkr-fa-price-cache' for run-all (keeps the price "
            "audit artifacts alongside the generated schedules)."
        ),
    )
    _add_itr_form_argument(run_all, required=True)
    _add_accept_anonymised_statement_argument(run_all)


#: argparse's own messages that name nothing typed, the same on Python 3.12 to
#: 3.14: its fixed argument-count texts, and the templates whose ``%s`` lists
#: argument names joined by the separator beside them.
_ARGPARSE_COUNT_ERRORS: Final = frozenset(
    {"expected one argument", "expected at most one argument", "expected at least one argument"}
)
_ARGPARSE_NAME_LIST_ERRORS: Final = (
    ("the following arguments are required: %s", ", "),
    ("one of the arguments %s is required", " "),
    ("not allowed with argument %s", ", "),
)


def _lists_only(message: str, template: str, separator: str, names: set[str | None]) -> bool:
    """Whether ``message`` is ``template`` with its ``%s`` listing only ``names``."""
    prefix, _, suffix = template.partition("%s")
    if not (message.startswith(prefix) and message.endswith(suffix)):
        return False
    listed = message[len(prefix) : len(message) - len(suffix)]
    return all(name in names for name in listed.split(separator))


class _NoEchoArgumentParser(argparse.ArgumentParser):
    """An argument parser whose errors never print a typed value (#1071).

    An error prints as argparse wrote it only when its message is reviewed: one
    of :data:`_REVIEWED_TYPE_ERRORS`, an argument-count text, or a template from
    :data:`_ARGPARSE_NAME_LIST_ERRORS` that lists only this parser's own
    argument names. Anything else prints fixed text that names the argument
    when the error has one. Either way, it prints the usage line first and
    exits 2. ``add_subparsers`` builds every sub-parser from this class too,
    and the class passes ``exit_on_error=False`` itself because sub-parsers
    don't inherit it. :meth:`error` applies the same check, because older
    Python 3.12 releases report a missing required argument or group through
    it rather than raising ``ArgumentError``.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs, exit_on_error=False)

    def parse_known_args(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().parse_known_args(*args, **kwargs)
        except argparse.ArgumentError as exc:
            super().error(self._error_text(exc))

    def parse_args(self, *args: Any, **kwargs: Any) -> Any:
        parsed, unrecognized = self.parse_known_args(*args, **kwargs)
        if unrecognized:
            super().error(self._fallback(None))
        return parsed

    def error(self, message: str) -> NoReturn:
        """Exit 2 with ``message`` only if it is reviewed, otherwise fixed text."""
        super().error(message if self._reviewed(message) else self._fallback(None))

    def _fallback(self, argument_name: str | None) -> str:
        if argument_name is None:
            return f"unrecognized or ambiguous arguments; run '{self.prog} --help'"
        return f"argument {argument_name}: invalid value; run '{self.prog} --help'"

    def _error_text(self, exc: argparse.ArgumentError) -> str:
        return str(exc) if self._reviewed(exc.message) else self._fallback(exc.argument_name)

    def _reviewed(self, message: str) -> bool:
        names = {argparse._get_action_name(action) for action in self._actions}
        counts = {action.nargs for action in self._actions if isinstance(action.nargs, int)}
        return (
            message in _REVIEWED_TYPE_ERRORS
            or message in _ARGPARSE_COUNT_ERRORS
            or message in {f"expected {n} argument{'' if n == 1 else 's'}" for n in counts}
            or any(
                _lists_only(message, template, separator, names)
                for template, separator in _ARGPARSE_NAME_LIST_ERRORS
            )
        )


def _build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915
    parser = _NoEchoArgumentParser(
        prog=_PROG_NAME,
        description=(
            "Produce Indian-tax artefacts from IBKR, Robinhood, Fidelity, "
            "and StockPlan source exports with evidence-backed reconciliation."
        ),
        epilog=(
            "Compliance advisories: the IBKR subcommands (schedule-fa, "
            "schedule-os, schedule-cg, schedule-cg-ltcg, schedule-fsi, "
            "schedule-tr, form-67 and run-all) and the robinhood-* "
            "subcommands print two loud tax-safety advisories that "
            "cannot be silenced via --quiet: "
            "(1) a Black Money Act, 2015 disclosure reminder covering "
            "the §43 ₹10 lakh penalty for non-disclosure of foreign "
            "assets / income on the return (subject to the §43 ₹20 "
            "lakh aggregate-foreign-asset, other-than-immovable-"
            "property, peak-balance carve-out introduced by Finance "
            "(No.2) Act, 2024 with effect from 1 October 2024), plus "
            "the §50 6-mo-to-7-yr imprisonment exposure for wilful "
            "non-disclosure (with a matching §50 ₹20 lakh prosecution "
            "carve-out enacted by Finance Act, 2026 (Act 4 of 2026, "
            "Presidential assent 30 March 2026), deemed inserted "
            "with effect from 1 October 2024); and (2) an ITR-form "
            "selection guide (ITR-2 default for ibkr-fa users; ITR-3 "
            "if business / profession income also present; ITR-1 / "
            "ITR-4 NEVER applicable to foreign-asset holders). merge "
            "prints the first only; the NetBenefits, Fidelity CMA, "
            "StockPlan and init subcommands print neither. The "
            "schedule-os, schedule-cg, schedule-cg-ltcg, schedule-fsi, "
            "schedule-tr, form-67 and run-all advisories route to "
            "stderr; schedule-fa's, merge's and robinhood-*'s go to "
            "stdout as part of the run report. "
            "See README.md for the full statutory context."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="COMMAND",
    )
    _add_robinhood_parsers(subparsers)

    sched = subparsers.add_parser(
        "schedule-fa",
        help="Produce Schedule FA Table A3, and Table A2 when [account.custodian] is set.",
        description=(
            "Produce Schedule FA Table A3, and Table A2 when "
            "[account.custodian] is set, from an IBKR Flex Query or "
            "Activity Statement export (auto-detected), in the chosen "
            "--format. Peak "
            "value (#14) is auto-computed from fetched raw daily closing "
            "prices by default (select the native-currency peak first, then "
            "apply that date's SBI TTBR). The "
            'schedule_fa.peak_currency = "inr" override instead maximizes '
            "daily converted values and prints a warning. With "
            "--no-fetch-prices, only a complete price cache already on "
            "disk is used. Peak is left blank for any holding whose "
            "price can't be sourced with certainty. Gross amount "
            "paid/credited is auto-filled when the input carries a Dividends "
            "section with in-CY payments (Activity Statement) and "
            "manual-fill otherwise (Flex Query, or Activity Statement "
            "without dividends). Gross proceeds from sale or redemption "
            "uses proven pre-fee consideration and the 31 December SBI "
            "TT-Buying rate; open lots emit 0, while closed lots with "
            "only commission-reduced net cash remain manual-fill. "
            "The CLI prints a warning enumerating any unfilled columns "
            "and exits with status 3."
        ),
    )
    sched.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "IBKR CSV (Flex Query or Activity Statement; auto-detected). "
            "Flex Query must include Open Positions LOT, Account Information and "
            "Trades with Closed Lots, and its header and trailer records ('Include "
            "header and trailer records' in its delivery settings). "
            "Activity Statement must include Statement, Account Information, "
            "Financial Instrument Information, and Trades. Open Positions "
            "is optional (#162): closure-only exports where every position "
            "opened in the period was also fully closed are accepted."
        ),
    )
    sched.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to user.toml.",
    )
    sched.add_argument(
        "--output",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "Destination file for Table A3 in the chosen --format, or for "
            "the combined A2+A3 fragment with cbdt-json-fragment. Parent "
            "directory must exist. Every other format writes Table A2 to "
            "the <name>-a2.<ext> sibling (review.csv → review-a2.csv), and "
            "first deletes any file already there, even when the run "
            "writes no A2."
        ),
    )
    sched.add_argument(
        "--calendar-year",
        type=int,
        required=True,
        metavar="CY",
        help=(
            "Calendar year being reported, 1990-2099 (e.g., 2025 for AY 2026-27). "
            "An Activity Statement must end on Dec 31 of this year and start on or "
            "before Jan 1 (from Jan 1, or from your account's first trade if that is "
            "earlier, so it holds every holding's opening trade); a Flex Query's "
            "header record must show a period ending on Dec 31 of this year and "
            "starting on or before Jan 1, and its ReportDate must equal Dec 31 of "
            "this year."
        ),
    )
    _add_format_argument(
        sched,
        "schedule-fa",
        help_text=(
            "Output format (default: review-csv). 'cbdt-json-fragment' emits one "
            "combined Schedule FA A2+A3 JSON fragment for ITR-2 or ITR-3 "
            "(all-or-nothing: it refuses unless every required field is present)."
        ),
    )
    _add_itr_form_argument(sched, required=False)
    sched.add_argument(
        "--allow-masked-account",
        dest="allow_masked_account",
        action="store_true",
        help=(
            "Opt out of the IBKR-account-ID cross-check when the input "
            "statement masks the account number (e.g., 'U**********'). "
            "IBKR Activity Statement exports mask the account ID unless "
            "'Show full account number' is enabled at export time; pass "
            "this flag to accept the masked statement at the cost of "
            "skipping the safety net that prevents accidentally filing "
            "someone else's holdings on your return. Has no effect on "
            "unmasked account IDs — those are always cross-checked."
        ),
    )
    sched.add_argument(
        "--suppress-cross-ay-warning",
        dest="suppress_cross_ay_warning",
        action="store_true",
        help=(
            "Suppress the AY 2027-28+ cross-AY-verification warning. "
            "By default, runs with --calendar-year >= 2026 (which map to "
            "AY 2027-28 onwards under the Income-tax Act, 2025 + "
            "Income-tax Rules, 2026) print a warning that the "
            "FA-specific date-selection semantics under the new framework "
            "haven't been verified by ibkr-fa. The TTBR rate source "
            "is preserved per Rule 206(1) (resident FX rule) and "
            "Rule 207(3)(b) (TTBR definition); carry-forward fallback "
            "across non-publication days and the FA-specific date "
            "selection remain CBDT FAQ / practice (no Rule 206 "
            "carry-forward fallback text). "
            "Pass this flag if you have independently verified the FX "
            "policy is correct for the target AY. See "
            "https://github.com/jay-tau/ibkr-fa/issues/126 for "
            "verification status."
        ),
    )
    _add_accept_anonymised_statement_argument(sched)
    sched.add_argument(
        "--no-fetch-prices",
        dest="no_fetch_prices",
        action="store_true",
        help=(
            "Do NOT fetch market prices to compute the Schedule FA "
            "'Peak value of investment during the Period' column (#14). "
            "By default schedule-fa fetches each holding's RAW (never "
            "split/dividend-adjusted) daily closing prices for the "
            "calendar year, selects the native-currency peak, and converts "
            "it at that date's SBI TTBR. With "
            'schedule_fa.peak_basis = "high", it uses the higher of that '
            "closing peak and the weekday intraday highs. The fetched "
            "prices are cached next to the input "
            "statement as an immutable audit artifact so re-runs are "
            "deterministic. Pass this flag to never access the network: "
            "peaks then come only from a complete, integrity-checked "
            "cache already on disk (e.g. from an earlier run) and are "
            "otherwise left blank for manual entry (the pre-#14 "
            "behaviour). Under the default close basis, an open lot "
            "acquired on December 31 takes its closing value as its peak "
            "either way. Peak is also left blank for any "
            "holding whose price cannot be sourced with certainty "
            "(for example, an unknown/non-USD ticker, a stock split after "
            "the lot's first in-year day and on or before the earlier of "
            "its disposal date and December 31, a split in a price series "
            "not normalized to contemporaneous per-date prices, or a price "
            "gap too large to carry forward) — it is never guessed."
        ),
    )
    sched.add_argument(
        "--price-cache-dir",
        dest="price_cache_dir",
        type=Path,
        default=None,
        help=(
            "Directory for the FA peak-value price cache / audit "
            "artifacts. Defaults to a '.ibkr-fa-price-cache' folder "
            "next to the input statement. To refetch a holding's prices, move its "
            "cache file, and any .lock beside it, out of this directory and re-run "
            "with price fetching (without --no-fetch-prices)."
        ),
    )

    _add_netbenefits_fa_income_parsers(subparsers)

    _add_fidelity_cma_parsers(subparsers)

    sp_income = subparsers.add_parser(
        "stockplan-income",
        help="Produce filing schedules plus official Form 67 CSV for a StockPlan dividend.",
        description=(
            "Produce Schedule OS/FSI/TR filing CSVs and the official Form 67 "
            "upload CSV for a Morgan Stanley "
            "StockPlan (Shareworks) holding's dividend, e.g. Google/Alphabet "
            "GSUs, from a directory containing the 'IN - Dividend Report' .xlsx "
            "plus the [stockplan] config block. The report is the sole source of "
            "the gross dividend and US withholding; its title-stated coverage "
            "must span the whole financial year, else the run REFUSES rather "
            "than risk missing a dividend. The dividend routes to Schedule OS "
            "1a(i) (gross INR via Rule 115), and the US withholding to Schedule "
            "FSI/TR/Form 67 as a foreign tax credit. The files are written "
            "into --output-dir; when a Form 67 row has no Indian tax payable "
            "and no credit, form-67-debug.csv replaces form-67.csv and the run "
            "exits 3. It also exits 3 when form-67.csv files a fractional Rate(%) "
            "to enter by hand after upload. It never reads the Capital Gain/Loss "
            "workbook, so it can't "
            "prove zero sales: every successful run warns and exits 3. Run "
            "stockplan-all to prove zero sales. Accepted exports, refusals and "
            "exit codes: docs/stockplan-contract.md."
        ),
    )
    sp_income.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="DIR",
        help="Directory containing the 'IN - Dividend Report' .xlsx. Other reports are ignored.",
    )
    sp_income.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to user.toml (must contain a [stockplan] block).",
    )
    sp_income.add_argument(
        "--output-dir",
        dest="output_dir",
        type=Path,
        required=True,
        metavar="DIR",
        help=(
            "Destination DIRECTORY for schedule-os.csv, schedule-fsi.csv, "
            "schedule-tr.csv, and the official form-67.csv, or form-67-debug.csv "
            "when a Form 67 row is BLANK. Must exist."
        ),
    )
    sp_income.add_argument(
        "--financial-year",
        dest="financial_year",
        type=_parse_financial_year,
        required=True,
        metavar="FY",
        help=(
            "FY in YYYY-YY or YYYY-YYYY form (e.g., 2025-26 for the April "
            "2025 - March 2026 period; AY 2026-27)."
        ),
    )
    sp_income.add_argument(
        "--suppress-cross-ay-warning",
        dest="suppress_cross_ay_warning",
        action="store_true",
        help="Suppress the AY 2027-28+ FX-policy cross-AY verification warning (see schedule-fsi).",
    )
    _add_debug_argument(sp_income)
    _add_itr_form_argument(sp_income, required=True)

    sp_fa = subparsers.add_parser(
        "stockplan-fa",
        help="Produce a Schedule FA Table A3 review CSV for a Morgan Stanley StockPlan holding.",
        description=(
            "Produce a Schedule FA Table A3 review CSV for a Morgan Stanley "
            "StockPlan (Shareworks) holding, e.g. Google/Alphabet GSUs, from a "
            "directory of the StockPlan .xlsx exports (the 'IN - Capital "
            "Gain/Loss Report', 'Vested Share Holdings', and 'IN - Dividend "
            "Report') plus the [stockplan] config block. The Dec-31 holdings are "
            "reconstructed from the per-vest Releases Report (vests on/before "
            "Dec-31); each lot's initial value is the FMV at vest (the §49(2AA) "
            "basis), the closing value is the Dec-31 Yahoo close, and the peak is "
            "the CY high/close. The Vested Share Holdings report is a snapshot "
            "as of its Selected Date (its Quote History sheet's Quote Date), which "
            "must be after Dec-31; every vest before that date must add up exactly "
            "to its aggregate, no vest may fall on it, and the Releases Report must "
            "be run on or after it. The run REFUSES on a reconciliation gap or any "
            "sale (intra-year disposals are a future stockplan-cg). Table A2 (the "
            "account itself) is a separate follow-up (the Dec-31 account cash "
            "isn't in these reports), so a successful run exits 3 (manual fill). "
            "Accepted exports, refusals and exit codes: docs/stockplan-contract.md."
        ),
    )
    sp_fa.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="DIR",
        help="Directory of the StockPlan .xlsx exports (CG/Loss, Vested Share Holdings, Dividend).",
    )
    sp_fa.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to user.toml (must contain a [stockplan] block with the Schedule FA fields).",
    )
    sp_fa.add_argument(
        "--output",
        type=Path,
        required=True,
        metavar="PATH",
        help="Destination file for the review CSV. Parent directory must exist.",
    )
    sp_fa.add_argument(
        "--calendar-year",
        type=int,
        required=True,
        metavar="CY",
        help="Calendar year being reported (e.g., 2025 for AY 2026-27).",
    )
    sp_fa.add_argument(
        "--no-fetch-prices",
        dest="no_fetch_prices",
        action="store_true",
        help=(
            "Use only complete, matching, integrity-checked cached market prices. "
            "stockplan-fa needs daily prices for the year-end close and peak; "
            "without a usable closing price, the run REFUSES."
        ),
    )
    sp_fa.add_argument(
        "--price-cache-dir",
        dest="price_cache_dir",
        type=Path,
        default=None,
        help=(
            "Directory for the price cache / audit artifacts. Defaults to a "
            "'.ibkr-fa-price-cache' folder inside the --input directory."
        ),
    )

    sp_all = subparsers.add_parser(
        "stockplan-all",
        help="Produce supported StockPlan schedules and report every known filing gap.",
        description=(
            "One-shot orchestrator for a Morgan Stanley StockPlan (e.g. Google "
            "GSU) export directory: runs stockplan-income (Schedule OS/FSI/TR/"
            "Form 67) and stockplan-fa (Schedule FA Table A3) against the SAME "
            "--input directory, writing all files into --output-dir. Reuses each "
            "subcommand's handler, so each schedule's files match a standalone "
            "run. It also proves whether Schedule CG is unnecessary or blocked by "
            "sales, and reports Table A2 as manual-fill because daily account "
            "cash/NAV is unavailable. That Schedule CG check covers the zero-sales "
            "gap a standalone stockplan-income run warns about, so the income leg "
            "omits that warning: it is manual-fill when a Form 67 row is BLANK, "
            "form-67.csv files a fractional Rate(%) or a relief row's foreign "
            "Taxpayer Identification Number is blank, and ok otherwise. "
            "Schedule FA's "
            "calendar year is derived from --financial-year (FY YYYY-YY maps to "
            "CY YYYY). Checks Form 67 readiness before any leg runs; a refusal "
            "stops the run there. Otherwise runs every leg even after one fails, "
            "prints a status-first summary to stderr, and exits with the worst "
            "outcome. A clean run always exits 3 because Table A2 is not generated; "
            "sales or unproven CG evidence exit 4. If any leg fails, it attempts to "
            "remove every output; while stderr still works, cleanup errors name "
            "files that may remain. A failed write to the console (for example, "
            "to a closed pipe) instead stops the run at once: it attempts to "
            "remove every output and exits 4. "
            "Do NOT file any output "
            "until every manual-fill or error line is resolved. Accepted exports, "
            "refusals and exit codes: docs/stockplan-contract.md."
        ),
    )
    sp_all.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="DIR",
        help="Directory of the StockPlan .xlsx exports (CG/Loss, Vested Share Holdings, Dividend).",
    )
    sp_all.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to user.toml (must contain a [stockplan] block with the Schedule FA fields).",
    )
    sp_all.add_argument(
        "--output-dir",
        dest="output_dir",
        type=Path,
        required=True,
        metavar="DIR",
        help="Destination DIRECTORY for all generated schedule CSVs (created if absent).",
    )
    sp_all.add_argument(
        "--financial-year",
        dest="financial_year",
        type=_parse_financial_year,
        required=True,
        metavar="FY",
        help="FY in YYYY-YY or YYYY-YYYY form (e.g., 2025-26 for AY 2026-27; FA uses CY 2025).",
    )
    sp_all.add_argument(
        "--no-fetch-prices",
        dest="no_fetch_prices",
        action="store_true",
        help="Do NOT fetch market prices for the Schedule FA close/peak (see stockplan-fa).",
    )
    sp_all.add_argument(
        "--price-cache-dir",
        dest="price_cache_dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Price-cache directory for Schedule FA; defaults to a "
            "'.ibkr-fa-price-cache' folder inside --input."
        ),
    )
    sp_all.add_argument(
        "--suppress-cross-ay-warning",
        dest="suppress_cross_ay_warning",
        action="store_true",
        help="Suppress the AY 2027-28+ FX-policy cross-AY verification warning.",
    )
    _add_debug_argument(sp_all)
    _add_itr_form_argument(sp_all, required=True)

    _add_netbenefits_cg_all_parsers(subparsers)

    os_sched = subparsers.add_parser(
        "schedule-os",
        help="Produce Schedule OS ordinary-dividend (1a(i)) review output.",
        description=(
            "Produce a Schedule OS review file for AY 2026-27 row 1a(i), "
            "'Dividend income [other than (ii) and (iii)]'. Every supported "
            "in-FY ordinary portfolio dividend is converted with Rule 115(1), Explanation (2)(e) "
            "and included in one portal total regardless of source country. "
            "Country remains on each detail row for FSI/TR/Form 67. The "
            "[os].schedule_os_indian_adr_routing setting affects that source "
            "attribution only; it does not change the 1a(i) total. "
            "An in-FY Return Of Capital, Long-Term Capital Gain or "
            "Cash in Lieu refuses with exit 4, whatever the issuer's "
            "country: ibkr-fa can't yet file one. An in-FY Payment In "
            "Lieu Of Dividend "
            "refuses with exit 4: ibkr-fa can't yet file it as other "
            "income with its withholding. The total is summed at full "
            "Decimal precision and rounded HALF_UP once. In-FY IBKR free-cash-"
            "balance interest uses Rule 115(1), Explanation (2)(c) and is surfaced "
            "as Schedule OS row 1b(ix) other interest. Flex Query "
            "inputs are rejected — Flex CashTransactions dividend "
            "parsing is tracked at "
            "https://github.com/jay-tau/ibkr-fa/issues/12."
        ),
    )
    os_sched.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "IBKR Activity Statement CSV. Must include Statement and "
            "Account Information sections; Dividends and Withholding Tax "
            "sections are parsed if present. Its period must contain the "
            "whole FY and may start earlier or end later; only in-FY rows "
            "are filed. Flex Query inputs are rejected (see #12)."
        ),
    )
    os_sched.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to user.toml.",
    )
    os_sched.add_argument(
        "--output",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "Destination file. Parent directory "
            "must exist. The on-disk format is controlled by --format "
            "(default review-csv); pick a path suffix that matches "
            "(e.g., ``.csv`` for review-csv, ``.xlsx`` for xlsx)."
        ),
    )
    _add_format_argument(
        os_sched,
        "schedule-os",
        help_text=(
            "Output format (default: review-csv). 'filing-csv' produces the "
            "vertical Quicko / ITR manual-entry worksheet without audit columns. 'xlsx' produces a "
            "single-sheet Excel workbook with the same section structure "
            "(globally sorted 1a(i) details, 1a(i) audit-TOTAL + portal-TOTAL, "
            "1b(ix) interest TOTAL, optional 1e block) "
            "and number/date typing so the portal-TOTAL "
            "cells are summable in Excel."
        ),
    )
    os_sched.add_argument(
        "--financial-year",
        dest="financial_year",
        type=_parse_financial_year,
        required=True,
        metavar="FY",
        help=(
            "FY in YYYY-YY or YYYY-YYYY form (e.g., 2025-26 or 2025-2026 "
            "for the April 2025 - March 2026 period; AY 2026-27)."
        ),
    )
    os_sched.add_argument(
        "--allow-masked-account",
        dest="allow_masked_account",
        action="store_true",
        help=(
            "Opt out of the IBKR-account-ID cross-check when the input "
            "statement masks the account number. Same semantics as "
            "schedule-fa --allow-masked-account."
        ),
    )
    os_sched.add_argument(
        "--suppress-cross-ay-warning",
        dest="suppress_cross_ay_warning",
        action="store_true",
        help=(
            "Suppress the AY 2027-28+ cross-AY-verification warning. By "
            "default, runs targeting AY 2027-28 onwards (FY 2026-27 and "
            "later) print a warning that Rule 206(2)(b)(i) Sl. No. 5 of "
            "the Income-tax Rules, 2026 preserves the Rule 115(1), Explanation (2)(e) "
            "dividend preceding-month-end semantic verbatim but the "
            "carry-back fallback across non-publication days remains "
            "CBDT-FAQ-based pending primary-source verification. Pass "
            "this flag if you have independently verified the FX policy "
            "is correct for the target AY. See "
            "https://github.com/jay-tau/ibkr-fa/issues/126 for "
            "verification status."
        ),
    )
    _add_accept_anonymised_statement_argument(os_sched)
    os_sched.add_argument(
        "--quiet",
        dest="quiet",
        action="store_true",
        help=(
            "Suppress the verbose multi-line render on STDOUT — print "
            "the two rounded-INR portal totals as a space-separated line: "
            "'<1a(i) ordinary dividends> <1b(ix) other interest>'. This "
            "intentionally replaces the incorrect pre-#415 three-field format. "
            "Useful for piping (e.g., "
            "DIVIDENDS=$(ibkr-fa schedule-os --quiet --financial-year "
            "2025-26 --input ... --output ... --config ... | cut -d' ' "
            "-f1); INTEREST=$(... | cut -d' ' -f2)). Safety warnings (cross-"
            "AY mismatch, masked-account usage, zero-qualifying-"
            "dividends, out-of-FY "
            "interest credits) ALWAYS route to stderr regardless of "
            "--quiet — suppressing them would mask under-reporting risk."
        ),
    )

    cg_sched = subparsers.add_parser(
        "schedule-cg",
        help="Produce a Schedule CG STCG (foreign short-term capital gains) review CSV.",
        description=(
            "Produce a form-aware Schedule CG STCG review CSV for the ITR-2 A5e "
            "or ITR-3 A6g foreign-equity line (holding period "
            "<= 24 months per Section 2(42A)) from an IBKR Activity "
            "Statement export. Each in-FY non-Indian-source closed lot "
            "has its realized capital gain converted to INR via Rule "
            "115(1), Explanation (2)(f) — a SINGLE conversion at the transfer-event "
            "preceding-month-end SBI TTBR; both the cost basis and "
            "the proceeds use the same transfer-date rate. (The "
            "first-proviso-to-section-48 + Rule 115A pathway "
            "applies only to non-residents transferring shares or "
            "debentures of an Indian company; foreign equities held "
            "by residents fall under the general Rule 115 "
            "specified-date rule.) Long-term closures (> 24 months) "
            "and out-of-FY closures are partitioned into audit blocks "
            "but excluded from the STCG aggregate. The filed TOTAL is "
            "consideration less cost, each summed and rounded HALF_UP "
            "to whole rupees, as the ITR form computes Balance; the "
            "audit TOTAL keeps the full-precision gain. Net losses "
            "(TOTAL < 0) are valid and carry forward 8 AYs per "
            "Section 74. Flex Query inputs are rejected — CG depends "
            "on the Trades-section FIFO-matched closures (see #104) "
            "that the Flex parser doesn't currently produce."
        ),
    )
    cg_sched.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "IBKR Activity Statement CSV. Must include Statement, "
            "Account Information, Financial Instrument Information, "
            "and Trades sections (CG depends on FIFO-matched closures). "
            "Open Positions is optional (#162): closure-only exports "
            "are accepted. Flex Query inputs are rejected."
        ),
    )
    cg_sched.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to user.toml.",
    )
    cg_sched.add_argument(
        "--output",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "Destination file. Parent directory "
            "must exist. The on-disk format is controlled by --format "
            "(default review-csv); pick a path suffix that matches "
            "(e.g., ``.csv`` for review-csv, ``.xlsx`` for xlsx)."
        ),
    )
    _add_format_argument(
        cg_sched,
        "schedule-cg",
        help_text=(
            "Output format (default: review-csv). 'xlsx' produces a "
            "single-sheet Excel workbook with the same column layout "
            "and section structure as the CSV; signed amounts (e.g., "
            "net STCG losses) ride as native numeric cells so "
            "spreadsheet SUM works directly on the gain column."
        ),
    )
    cg_sched.add_argument(
        "--financial-year",
        dest="financial_year",
        type=_parse_financial_year,
        required=True,
        metavar="FY",
        help=(
            "FY in YYYY-YY or YYYY-YYYY form (e.g., 2025-26 or 2025-2026 "
            "for the April 2025 - March 2026 period; AY 2026-27)."
        ),
    )
    _add_itr_form_argument(cg_sched, required=True)
    cg_sched.add_argument(
        "--allow-masked-account",
        dest="allow_masked_account",
        action="store_true",
        help=(
            "Opt out of the IBKR-account-ID cross-check when the input "
            "statement masks the account number. Same semantics as "
            "schedule-fa --allow-masked-account."
        ),
    )
    cg_sched.add_argument(
        "--suppress-cross-ay-warning",
        dest="suppress_cross_ay_warning",
        action="store_true",
        help=(
            "Suppress the AY 2027-28+ cross-AY-verification warning. By "
            "default, runs targeting AY 2027-28 onwards print a warning "
            "that the Rule 115 capital-gains preceding-month-end "
            "semantic is preserved by the Income-tax Rules, 2026 but "
            "the carry-back fallback across non-publication days remains "
            "CBDT-FAQ-based pending primary-source verification. Pass "
            "this flag if you have independently verified the FX policy "
            "is correct for the target AY. See "
            "https://github.com/jay-tau/ibkr-fa/issues/126 for "
            "verification status."
        ),
    )
    _add_accept_anonymised_statement_argument(cg_sched)
    cg_sched.add_argument(
        "--quiet",
        dest="quiet",
        action="store_true",
        help=(
            "Suppress the verbose multi-line render on STDOUT — print "
            "only the rounded-INR TOTAL (may be negative for a net loss) "
            "as a single scalar number on one line. Useful for piping. "
            "Safety warnings (cross-AY mismatch, masked-account usage, "
            "zero-STCG-rows, LTCG-deferred / IN-source / out-of-FY "
            "exclusion counts) and the manual-fill line for Table F "
            "date bands left blank (exit 3) ALWAYS route to stderr "
            "regardless of --quiet."
        ),
    )

    cg_ltcg_sched = subparsers.add_parser(
        "schedule-cg-ltcg",
        help="Produce a Schedule CG LTCG (foreign long-term capital gains) review CSV.",
        description=(
            "Produce a form-aware Schedule CG LTCG review CSV for the ITR-2 B8e "
            "or ITR-3 B9e foreign-equity line (holding period "
            "> 24 calendar months per Section 2(42A)) from an IBKR "
            "Activity Statement export. Each in-FY non-Indian-source "
            "long-term closure has its realized capital gain converted "
            "to INR via Rule 115(1), Explanation (2)(f) — a SINGLE conversion at the "
            "transfer-event preceding-month-end SBI TTBR (same as "
            "schedule-cg STCG semantics). v1 supports the NEW REGIME "
            "ONLY (12.5% without indexation, disposals on or after "
            "2024-07-23 per Finance (No. 2) Act, 2024). Pre-cutover "
            "disposals require indexation via CBDT Cost Inflation "
            "Index data and are routed to an audit block (out of v1 "
            "scope). Short-term closures (handled by schedule-cg) and "
            "out-of-FY closures are also partitioned into audit blocks. "
            "The filed TOTAL is consideration less cost, each summed "
            "and rounded HALF_UP to whole rupees, as the ITR form "
            "computes Balance; the audit TOTAL keeps the full-precision "
            "gain. Net losses (TOTAL < 0) are valid — Section 74's "
            "8-AY long-term-loss carry-forward applies (offsets "
            "long-term gains only). Flex Query inputs are rejected — "
            "CG depends on the Trades-section FIFO-matched closures "
            "(see #104) that the Flex parser doesn't currently produce."
        ),
    )
    cg_ltcg_sched.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "IBKR Activity Statement CSV. Must include Statement, "
            "Account Information, Financial Instrument Information, "
            "and Trades sections. Open Positions is optional (#162): "
            "closure-only exports are accepted. Should typically be a "
            "MULTI-YEAR export from your account's first trade, or from "
            "Apr 1 of the FY if that is earlier (FIFO matching needs the "
            "opening trade of every lot held during the FY, including "
            "lots sold in it). Flex Query inputs are rejected."
        ),
    )
    cg_ltcg_sched.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to user.toml.",
    )
    cg_ltcg_sched.add_argument(
        "--output",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "Destination file for the review output. On-disk format "
            "matches --format (review-csv → .csv-shaped; xlsx → "
            ".xlsx workbook). Parent directory must exist."
        ),
    )
    _add_format_argument(
        cg_ltcg_sched,
        "schedule-cg-ltcg",
        help_text=(
            "Output format for the Schedule CG LTCG review. "
            "'review-csv' (default) emits a UTF-8 BOM CSV file. "
            "'xlsx' emits a single-sheet Excel workbook with "
            "native numeric / date cells (formulas like =SUM() "
            "work directly on the audit columns)."
        ),
    )
    cg_ltcg_sched.add_argument(
        "--financial-year",
        dest="financial_year",
        type=_parse_financial_year,
        required=True,
        metavar="FY",
        help=(
            "FY in YYYY-YY or YYYY-YYYY form (e.g., 2025-26 or 2025-2026 "
            "for the April 2025 - March 2026 period; AY 2026-27)."
        ),
    )
    _add_itr_form_argument(cg_ltcg_sched, required=True)
    cg_ltcg_sched.add_argument(
        "--allow-masked-account",
        dest="allow_masked_account",
        action="store_true",
        help=(
            "Opt out of the IBKR-account-ID cross-check when the input "
            "statement masks the account number. Same semantics as "
            "schedule-cg --allow-masked-account."
        ),
    )
    cg_ltcg_sched.add_argument(
        "--suppress-cross-ay-warning",
        dest="suppress_cross_ay_warning",
        action="store_true",
        help=(
            "Suppress the AY 2027-28+ cross-AY-verification warning. By "
            "default, runs targeting AY 2027-28 onwards print a warning "
            "that the Rule 115 capital-gains preceding-month-end "
            "semantic is preserved by the Income-tax Rules, 2026 but "
            "the carry-back fallback across non-publication days remains "
            "CBDT-FAQ-based pending primary-source verification. Pass "
            "this flag if you have independently verified the FX policy "
            "is correct for the target AY. See "
            "https://github.com/jay-tau/ibkr-fa/issues/126 for "
            "verification status."
        ),
    )
    _add_accept_anonymised_statement_argument(cg_ltcg_sched)
    cg_ltcg_sched.add_argument(
        "--quiet",
        dest="quiet",
        action="store_true",
        help=(
            "Suppress the verbose multi-line render on STDOUT — print "
            "only the rounded-INR TOTAL (may be negative for a net "
            "long-term loss) as a single scalar number on one line. "
            "Safety warnings (cross-AY mismatch, masked-account, "
            "zero-LTCG-rows, audit-bucket counts, pre-cutover "
            "disposals deferred, net-loss advisory) and the manual-fill "
            "line for Table F date bands left blank (exit 3) ALWAYS "
            "route to stderr regardless of --quiet."
        ),
    )

    fsi_sched = subparsers.add_parser(
        "schedule-fsi",
        help="Produce a Schedule FSI per-(country, head-of-income) review file.",
        description=(
            "Produce a Schedule FSI review CSV (or XLSX) for ITR-2/ITR-3 "
            "Schedule FSI — Details of Income from outside India and "
            "tax relief — from an IBKR Activity Statement export. "
            "Aggregates four FSI-eligible sources (foreign-source ordinary "
            "dividends under OTHER_SOURCES per Schedule OS row 1a(i); broker "
            "credit-interest under OTHER_SOURCES per Schedule OS row "
            "1b(ix); short-term capital gains under CAPITAL_GAINS per "
            "Schedule CG Section A; long-term capital gains under "
            "CAPITAL_GAINS per Schedule CG Section B new regime) plus "
            "withholding tax (Dividends-section WHT layered onto "
            "matching foreign-income rows per Rule 128(5)(ii) FX) "
            "into per-(country, head) rows. Output file has 8 portal "
            "columns + 5 audit columns per ITR-2 form column letters "
            "(a)-(h); see docs/portal/schedule-fsi.md. v1 requires an "
            "Activity Statement with all four sections (STATEMENT + "
            "ACCOUNT_INFORMATION + FINANCIAL_INSTRUMENT_INFORMATION + "
            "TRADES); a dividend-only export (no Trades) is not "
            "supported in v1 — workaround: run `schedule-os` for the "
            "dividend totals and manually transcribe. ADR / GDR routing: "
            "when `[os].schedule_os_indian_adr_routing = 'underlying-"
            "issuer'`, Indian-ADR dividends are treated as Indian-source and "
            "the matching depositary-country WHT "
            "is symmetrically excluded from Schedule FSI (issue #225). "
            "Flex Query inputs are rejected (#12)."
        ),
    )
    fsi_sched.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "IBKR Activity Statement CSV. Must include Statement, "
            "Account Information, Financial Instrument Information, "
            "and Trades sections; Dividends, Withholding Tax, and "
            "Interest sections are parsed if present."
        ),
    )
    fsi_sched.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to user.toml.",
    )
    fsi_sched.add_argument(
        "--output",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "Destination file. Parent directory "
            "must exist. The on-disk format is controlled by --format "
            "(default review-csv); pick a path suffix that matches "
            "(e.g., ``.csv`` for review-csv, ``.xlsx`` for xlsx)."
        ),
    )
    _add_format_argument(
        fsi_sched,
        "schedule-fsi",
        help_text=(
            "Output format (default: review-csv). 'filing-csv' produces a vertical "
            "two-column worksheet for the user-entered portal fields, excluding "
            "the read-only relief field. 'xlsx' produces a "
            "single-sheet Excel workbook with the same 13-column "
            "layout (8 portal columns + 5 audit columns) and number "
            "typing so the audit-INR columns are summable in Excel."
        ),
    )
    fsi_sched.add_argument(
        "--financial-year",
        dest="financial_year",
        type=_parse_financial_year,
        required=True,
        metavar="FY",
        help=(
            "FY in YYYY-YY or YYYY-YYYY form (e.g., 2025-26 or "
            "2025-2026 for the April 2025 - March 2026 period; AY "
            "2026-27)."
        ),
    )
    fsi_sched.add_argument(
        "--allow-masked-account",
        dest="allow_masked_account",
        action="store_true",
        help=(
            "Opt out of the IBKR-account-ID cross-check when the input "
            "statement masks the account number. Same semantics as "
            "schedule-os --allow-masked-account."
        ),
    )
    fsi_sched.add_argument(
        "--suppress-cross-ay-warning",
        dest="suppress_cross_ay_warning",
        action="store_true",
        help=(
            "Suppress the AY 2027-28+ cross-AY-verification warning. "
            "By default, runs targeting AY 2027-28 onwards print a "
            "warning that both the income FX policy (Rule 115 → Rule "
            "206 per Rules 2026) and the WHT FX policy (Rule 128 → "
            "Rule 76 per Rules 2026) require independent verification. "
            "Pass this flag once verified."
        ),
    )
    _add_accept_anonymised_statement_argument(fsi_sched)
    fsi_sched.add_argument(
        "--quiet",
        dest="quiet",
        action="store_true",
        help=(
            "Suppress the verbose multi-line render on STDOUT — print "
            "a CSV-with-header per-row machine-parseable output: "
            "'cbdt_country_code,head_of_income,income_inr_rounded,"
            "foreign_tax_inr_rounded' plus one detail row per "
            "(country, head). Zero-row runs emit just the header. "
            "Safety warnings (cross-AY mismatch, masked-account, "
            "ADR-routing config, audit-bucket counts) ALWAYS route to "
            "stderr regardless of --quiet."
        ),
    )

    tr_sched = subparsers.add_parser(
        "schedule-tr",
        help="Produce a Schedule TR per-country tax-relief review file.",
        description=(
            "Produce a form-aware Schedule TR review CSV (or XLSX) for ITR-2/ITR-3 "
            "Schedule TR — Summary of tax relief claimed for taxes "
            "paid outside India. Aggregates Schedule FSI per-(country, "
            "head) rows into one per-country foreign-tax-paid total. "
            "Tax relief and the section under which it is claimed (90 / "
            "90A / 91) auto-populate per country from the [form67] config "
            "+ DTAA / §91 reference data (#36 / #37, PR #277); a country is "
            "left blank when the [form67] config is absent or a contributing "
            "Schedule FSI row's relief can't be computed. All Schedule FSI "
            "guarantees flow through transitively (Activity Statement "
            "input only and the per-ISIN orphan-WHT guard; Indian-ADR "
            "WHT is symmetrically excluded under UNDERLYING_ISSUER "
            "routing per #225). The output is 9 cells per row (5 portal "
            "columns + 4 audit columns) per docs/portal/schedule-tr.md. "
            "Uses the PR #216 dual-anchor pattern: portal cells are "
            "HALF_UP rounded while audit cells preserve full precision."
        ),
    )
    tr_sched.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "IBKR Activity Statement CSV. Must include Statement, "
            "Account Information, Financial Instrument Information, "
            "and Trades sections; Dividends, Withholding Tax, and "
            "Interest sections are parsed if present. Schedule TR "
            "consumes the Schedule FSI aggregate derived from these "
            "sections."
        ),
    )
    tr_sched.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to user.toml.",
    )
    tr_sched.add_argument(
        "--output",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "Destination file for the review output. Parent directory "
            "must exist. The on-disk format is controlled by --format "
            "(default review-csv); pick a path suffix that matches "
            "(e.g., ``.csv`` for review-csv, ``.xlsx`` for xlsx)."
        ),
    )
    _add_format_argument(
        tr_sched,
        "schedule-tr",
        help_text=(
            "Output format (default: review-csv). 'xlsx' produces a "
            "single-sheet Excel workbook with the same 9-column layout "
            "(5 portal columns + 4 audit columns) and number typing "
            "so the audit-INR column is summable in Excel."
        ),
    )
    tr_sched.add_argument(
        "--financial-year",
        dest="financial_year",
        type=_parse_financial_year,
        required=True,
        metavar="FY",
        help=(
            "FY in YYYY-YY or YYYY-YYYY form (e.g., 2025-26 or "
            "2025-2026 for the April 2025 - March 2026 period; AY "
            "2026-27)."
        ),
    )
    _add_itr_form_argument(tr_sched, required=True)
    tr_sched.add_argument(
        "--allow-masked-account",
        dest="allow_masked_account",
        action="store_true",
        help=(
            "Opt out of the IBKR-account-ID cross-check when the input "
            "statement masks the account number. Same semantics as "
            "schedule-os --allow-masked-account."
        ),
    )
    tr_sched.add_argument(
        "--suppress-cross-ay-warning",
        dest="suppress_cross_ay_warning",
        action="store_true",
        help=(
            "Suppress the AY 2027-28+ cross-AY-verification warning. "
            "By default, runs targeting AY 2027-28 onwards print a "
            "warning that both the income FX policy (Rule 115 → Rule "
            "206 per Rules 2026) and the WHT FX policy (Rule 128 → "
            "Rule 76 per Rules 2026) require independent verification. "
            "Pass this flag once verified."
        ),
    )
    _add_accept_anonymised_statement_argument(tr_sched)
    tr_sched.add_argument(
        "--quiet",
        dest="quiet",
        action="store_true",
        help=(
            "Suppress the verbose multi-line render on STDOUT — print "
            "a CSV-with-header per-row machine-parseable output: "
            "'cbdt_country_code,iso_alpha2,country_name_portal,"
            "foreign_tax_inr_total_portal_displayed,"
            "foreign_tax_inr_total_full_precision,event_count' plus "
            "one detail row per country. Zero-row runs emit just the "
            "header. Safety warnings (cross-AY mismatch, masked-account, "
            "ADR-routing config, FTC-relief advisory, audit-bucket "
            "counts) ALWAYS route to stderr regardless of --quiet."
        ),
    )

    form67_sched = subparsers.add_parser(
        "form-67",
        help="Produce a Form 67 per-(country, source-of-income) review file.",
        description=(
            "Produce a Form 67 review CSV for CBDT Form 67 — Statement "
            "of income from a country or specified territory outside "
            "India and Foreign Tax Credit, filed via the e-filing "
            "portal's Form 67 service per Rule 128(9) of the Income-tax "
            "Rules, 1962. Form 67 is per-(country, source-of-income) "
            "aggregate (NOT per-country alone like Schedule TR; NOT "
            "per-event like a transaction log) — one row per (country, "
            "DIVIDEND) in v1; future source-kind tagging on the WHT "
            "model will let v2 also emit (country, INTEREST) etc. "
            "Form 67 reuses the entries the shared Schedule FSI "
            "parse+compute already builds, so all FSI "
            "guarantees flow through transitively (Activity Statement "
            "input only and the per-ISIN orphan-WHT guard; Indian-ADR "
            "WHT is symmetrically excluded under UNDERLYING_ISSUER "
            "routing per #225). The default output is CBDT's official "
            "15-column IncomeDetails.csv upload format. Use `--format "
            "review-csv` only for the non-uploadable 17-column audit view. "
            "Cols 7, 9-13 (tax "
            "payable, DTAA article / rate, §90 / §91 credits, total) "
            "auto-populate from the [form67] config + DTAA reference "
            "data (#37 step 4/N, PRs #230-#234); col 8 (§115JB / JC "
            "special-rate tax payable) is still manual-fill (#235)."
        ),
    )
    form67_sched.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "IBKR Activity Statement CSV. Must include Statement, "
            "Account Information, Financial Instrument Information, "
            "and Trades sections; Dividends, Withholding Tax, and "
            "Interest sections are parsed if present. Form 67 "
            "reuses the entries the shared Schedule FSI compute "
            "builds from these sections."
        ),
    )
    form67_sched.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to user.toml.",
    )
    form67_sched.add_argument(
        "--output",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "Destination file. Parent directory must exist. The default "
            "portal-csv is the official 15-column upload format."
        ),
    )
    _add_format_argument(
        form67_sched,
        "form-67",
        help_text=(
            "Output format (default: portal-csv, the official upload file). "
            "review-csv and xlsx are audit/debug formats and must not be uploaded."
        ),
    )
    form67_sched.add_argument(
        "--financial-year",
        dest="financial_year",
        type=_parse_financial_year,
        required=True,
        metavar="FY",
        help=(
            "FY in YYYY-YY or YYYY-YYYY form (e.g., 2025-26 or "
            "2025-2026 for the April 2025 - March 2026 period; AY "
            "2026-27)."
        ),
    )
    form67_sched.add_argument(
        "--allow-masked-account",
        dest="allow_masked_account",
        action="store_true",
        help=(
            "Opt out of the IBKR-account-ID cross-check when the input "
            "statement masks the account number. Same semantics as "
            "schedule-os --allow-masked-account."
        ),
    )
    form67_sched.add_argument(
        "--suppress-cross-ay-warning",
        dest="suppress_cross_ay_warning",
        action="store_true",
        help=(
            "Suppress the AY 2027-28+ cross-AY-verification warning. "
            "By default, runs targeting AY 2027-28 onwards print a "
            "warning that both the income FX policy (Rule 115 → Rule "
            "206 per Rules 2026) and the WHT FX policy (Rule 128 → "
            "Rule 76 per Rules 2026) require independent verification, "
            "AND that Form 67 itself is renumbered to Form 44 under "
            "Rule 76 per the AY 2027-28+ ITR utility (CBDT Form 44/45 "
            "FAQ + Navigator rule-wise comparison). The v1 writer "
            "emits Form 67-labelled output regardless of AY; the "
            "Form 44 migration is tracked in epic #126. Pass this "
            "flag once verified."
        ),
    )
    _add_accept_anonymised_statement_argument(form67_sched)
    form67_sched.add_argument(
        "--quiet",
        dest="quiet",
        action="store_true",
        help=(
            "Suppress the verbose multi-line render on STDOUT — print "
            "a CSV-with-header per-row machine-parseable output: "
            "'serial_number,cbdt_country_code,iso_alpha2,country_name_portal,"
            "source_of_income,income_inr,foreign_tax_inr,"
            "foreign_tax_rate_percent,event_count' plus one detail row "
            "per entry. Zero-row runs emit just the header. Safety "
            "warnings (cross-AY mismatch, masked-account, ADR-routing "
            "config, FTC-relief advisory, audit-bucket counts) "
            "ALWAYS route to stderr regardless of --quiet."
        ),
    )

    _build_run_all_parser(subparsers)

    init_sub = subparsers.add_parser(
        "init",
        help="Scaffold a starter user.toml config file.",
        description=(
            "Write a fully-annotated starter user.toml to PATH "
            "(default: ./user.toml). Every setting in the generated "
            "file is COMMENTED OUT on purpose: a freshly-scaffolded "
            "config never files example data, so ibkr-fa reports "
            "'account: missing required table' until you uncomment and "
            "fill the [account] table. Only holder_name is required there: "
            "number and opening_date are read only by IBKR commands, and "
            "status only by IBKR and StockPlan commands. Only "
            "[account].number is cross-checked (against the IBKR "
            "statement), so the template makes you set holder_name, and "
            "opening_date / status where you keep them, by hand rather "
            "than shipping live example values for those filed fields. "
            "Uncomment any optional sections (foreign TINs, custodian, "
            "exact Form 67 inputs, price overrides, per-issuer FA addresses) you "
            "need. Refuses to overwrite an existing file unless --force, "
            "and refuses to create missing parent directories."
        ),
    )
    init_sub.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path("user.toml"),
        metavar="PATH",
        help="Destination config file path. Defaults to ./user.toml.",
    )
    init_sub.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help=(
            "Overwrite PATH if it already exists. Without this flag the "
            "scaffold refuses to clobber an existing config to protect "
            "your edits."
        ),
    )

    merge_sub = subparsers.add_parser(
        "merge",
        help="Merge a Schedule FA JSON fragment into a saved ITR-2 or ITR-3 return.",
        description=(
            "Splice the Table A2 + A3 rows from a `schedule-fa --format "
            "cbdt-json-fragment` file (--fragment) into a saved ITR-2 or ITR-3 return "
            "exported from the offline utility (--itr), writing the result to a "
            "NEW file (--output). Strict merge-once: it REFUSES rather than risk "
            "a wrong filing — the target's Table A3 must be empty and its Table "
            "A2 must not already disclose this account (re-export a CLEAN return "
            "and merge once). Every value outside Table A2 and A3 is kept, each "
            "number at its exact decimal value rather than a float approximation "
            "(a plain decimal such as -1500.50 keeps its text). The output is rewritten as "
            "indented UTF-8 JSON, so its layout and bytes differ from your saved "
            "return. The inputs are never modified; --output must be a new path. "
            "The fragment is treated as untrusted and fully re-validated. Re-"
            "import the merged file into the offline utility."
        ),
    )
    merge_sub.add_argument(
        "--fragment",
        dest="fragment",
        type=Path,
        required=True,
        metavar="FRAGMENT.json",
        help="The ibkr-fa Schedule FA JSON fragment (cbdt-json-fragment output).",
    )
    merge_sub.add_argument(
        "--itr",
        dest="itr",
        type=Path,
        required=True,
        metavar="SAVED_ITR.json",
        help="Your saved ITR-2 or ITR-3 return JSON, exported from the offline utility.",
    )
    merge_sub.add_argument(
        "--output",
        "-o",
        dest="output",
        type=Path,
        required=True,
        metavar="MERGED.json",
        help="Destination for the merged return (must NOT already exist).",
    )

    return parser


def _add_netbenefits_fa_income_parsers(subparsers: argparse._SubParsersAction) -> None:
    nb_fa = subparsers.add_parser(
        "netbenefits-fa",
        help="Produce Schedule FA Tables A3 and A2 for a Fidelity ESPP holding.",
        description=(
            "Produce Schedule FA Tables A3 and A2 for a Fidelity "
            "NetBenefits (e.g. Microsoft ESPP) holding from a bundle "
            "directory of NetBenefits exports (open/closed-lots CSVs, "
            "trade-confirmation PDFs and statement PDFs) plus the [netbenefits] "
            "config block. The statements are read and must cover, with no gap, "
            "from the later of Jan-1 and netbenefits.account.account_opening_date "
            "through ten days after Dec-31: with every confirmation in the bundle, "
            "they prove that the Dec-31 purchase is the only lot held in the "
            "calendar year, and the run refuses otherwise. "
            "The lot's initial value is the confirmation FMV converted at the "
            "acquisition-date TTBR, while the closing value is the Dec-31 close "
            "(config price override and/or a Yahoo close, fetched or read from "
            "the price cache, cross-checked), and the peak value under the default "
            'schedule_fa.peak_basis = "close" equals the closing value (the '
            "lot is purchased on Dec-31, so its in-CY holding window is a "
            'single day); with peak_basis = "high" it needs a fetched or cached '
            "price series with a valid intraday high for every US market session "
            "in that window (outside 2025, every weekday; a window with no such "
            "day uses the carried close), else it is left for manual fill. The "
            "gross dividend "
            "is a proven 0 (empty in-CY held window). "
            "Every figure is cross-checked against the trade confirmation "
            "and the config; the run REFUSES on any mismatch or "
            "unprovable Dec-31 holding. It emits Table A2 alongside Table A3; "
            "A2's peak balance remains manual-fill because the statements do not "
            "provide a complete daily account-value series."
        ),
    )
    nb_fa.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="DIR",
        help=(
            "The NetBenefits bundle DIRECTORY: exactly one open-lots CSV, "
            "exactly one closed-lots CSV (may be header-only), one "
            "trade-confirmation PDF per ESPP purchase, and the statement PDFs. "
            "The statements are read: the Stock Plan Services reports must cover "
            "through ten days after Dec-31."
        ),
    )
    _add_netbenefits_config_argument(nb_fa)
    nb_fa.add_argument(
        "--output",
        type=Path,
        required=True,
        metavar="PATH",
        help="Destination file for Table A3. Parent directory must exist.",
    )
    _add_format_argument(
        nb_fa,
        "netbenefits-fa",
        help_text=(
            "Output format (default: filing-csv). Use review-csv for audit/provenance "
            "columns. 'cbdt-json-fragment' emits one "
            "combined Schedule FA A2+A3 JSON fragment for ITR-2 or ITR-3 "
            "(covers ONLY the NetBenefits account). It requires the A2 peak via a "
            "[[netbenefits.a2_peak_overrides]] config entry (all-or-nothing: it "
            "refuses unless every required field is present)."
        ),
    )
    nb_fa.add_argument(
        "--quicko-workbook",
        dest="quicko_workbook",
        action="store_true",
        help=(
            "Also write the Quicko Foreign Shares & Securities (Other Broker) upload "
            "next to Table A3 (--output schedule-fa-a3.csv writes "
            "schedule-fa-quicko.xlsx; filing-csv only). Put Quicko's "
            f"downloaded {QUICKO_TEMPLATE_FILENAME} in the --input directory; the run "
            "fills that exact workbook. If the workbook cannot be written, including "
            "when the template is missing, the run refuses (exit 4) and removes "
            "Tables A2 and A3 too."
        ),
    )
    _add_itr_form_argument(nb_fa, required=False)
    nb_fa.add_argument(
        "--calendar-year",
        type=int,
        required=True,
        metavar="CY",
        help="Calendar year being reported (e.g., 2025 for AY 2026-27).",
    )
    nb_fa.add_argument(
        "--no-fetch-prices",
        dest="no_fetch_prices",
        action="store_true",
        help=(
            "Do NOT fetch market prices. By default netbenefits-fa fetches "
            "the security's raw daily closes for the calendar year to "
            "cross-check (and fall back to for) the Dec-31 closing value. "
            "With this flag, the Dec-31 close comes from a complete, "
            "integrity-checked price cache already on disk or from a "
            "[[netbenefits.price_overrides]] config entry; with neither, the "
            "run refuses rather than guess. Under the default "
            "close basis the peak value equals the closing value (single-day "
            'Dec-31 holding). Under schedule_fa.peak_basis = "high" the peak '
            "needs a fetched or cached price series with a valid intraday high "
            "for every US market session in the holding window (outside 2025, "
            "every weekday); a window with no such day can use the carried close "
            "instead. When the peak can't "
            "be established, the CSV outputs and the Quicko workbook leave it "
            "blank for manual fill, and no CBDT JSON fragment is written."
        ),
    )
    nb_fa.add_argument(
        "--price-cache-dir",
        dest="price_cache_dir",
        type=Path,
        default=None,
        help=(
            "Directory for the price cache / audit artifacts. Defaults to "
            "a '.ibkr-fa-price-cache' folder inside the --input directory."
        ),
    )

    nb_income = subparsers.add_parser(
        "netbenefits-income",
        help="Produce filing schedules plus official Form 67 CSV for a Fidelity ESPP dividend.",
        description=(
            "Produce Schedule OS/FSI/TR filing CSVs and the official Form 67 "
            "upload CSV for a Fidelity "
            "NetBenefits (e.g. Microsoft ESPP) holding's dividend, from a "
            "bundle directory of NetBenefits statement PDFs plus the "
            "[netbenefits] config block. The statement is the sole source of "
            "the gross dividend and US withholding; the bundle's statements "
            "must contiguously cover from the account opening date (or "
            "FY start) through FY end, else the run REFUSES rather than risk "
            "missing a dividend. The dividend routes to Schedule OS 1a(i) "
            "(gross INR via Rule 115), and the US withholding to Schedule "
            "FSI/TR/Form 67 as a foreign tax credit (Rule 128; DTAA Art "
            "10(2)(b) 25% cap; relief Art 25(2)(a)). The files are written "
            "into --output-dir; when a Form 67 row has no Indian tax payable "
            "and no credit, form-67-debug.csv replaces form-67.csv and the run "
            "exits 3. It also exits 3 when form-67.csv files a fractional "
            "Rate(%) to enter by hand after upload, when a relief row's foreign "
            "Taxpayer Identification Number is blank (no [account.foreign_tins] "
            "entry for its country), or when the Quicko block in schedule-tr.csv "
            'leaves "Name of the deductor" blank (no '
            "[netbenefits.account].withholding_agent_name)."
        ),
    )
    nb_income.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="DIR",
        help="The NetBenefits bundle DIRECTORY (statement PDFs). Confirmations/CSVs are ignored.",
    )
    _add_netbenefits_config_argument(nb_income)
    nb_income.add_argument(
        "--output-dir",
        dest="output_dir",
        type=Path,
        required=True,
        metavar="DIR",
        help=(
            "Destination DIRECTORY for schedule-os.csv, schedule-fsi.csv, "
            "schedule-tr.csv, and the official form-67.csv, or form-67-debug.csv "
            "when a Form 67 row is BLANK. Must exist."
        ),
    )
    _add_financial_year_argument(nb_income, help_text=_NETBENEFITS_FY_HELP)
    _add_suppress_cross_ay_warning_argument(
        nb_income,
        help_text=(
            "Suppress the AY 2027-28+ FX-policy cross-AY verification warning (see schedule-fsi)."
        ),
    )
    nb_income.add_argument(
        "--reconcile-1042s-fy-withholding-usd",
        dest="reconcile_1042s_usd",
        type=_parse_usd_2dp,
        default=None,
        metavar="USD",
        help=(
            "Cross-check the FTC withholding against the authoritative 1042-S. Pass the "
            "FY-ALLOCATED USD withholding total (the portion of your 1042-S box total that "
            "falls in this Indian financial year — NOT the raw calendar-year box total). "
            "A total that differs from the statements refuses (exit 4) and nothing is written."
        ),
    )
    _add_debug_argument(nb_income)
    _add_itr_form_argument(nb_income, required=True)


def _add_fidelity_cma_parsers(subparsers: argparse._SubParsersAction) -> None:
    cma_income = subparsers.add_parser(
        "fidelity-cma-income",
        help="Produce Fidelity CMA filing schedules plus official Form 67 CSV.",
        description=(
            "Parse the observed Fidelity CMA monthly statements and 2025 Form "
            "1042-S packet, then emit Schedule OS/FSI/TR filing CSVs and the "
            "official Form 67 upload CSV. "
            "SPAXX distributions are foreign dividends; Credit Interest is "
            "foreign interest. Schedule FSI also includes SPAXX capital gains, "
            "so an FY with a SPAXX redemption needs the complete acquisition "
            "history or the run refuses. US-tax refund status is selected per "
            "calendar year in [fidelity_cma.us_tax_years]. FTC is "
            "all-or-nothing for the FY and remains blocked until every in-FY "
            "withholding year is finalized or no-refund-claim with "
            "event-level eligible-tax provenance and reconciled, and only "
            "2025, the packet's year, can be reconciled; a blocked FTC still emits "
            "the income schedules, writes form-67-debug.csv in place of "
            "form-67.csv and exits 3. When a Form 67 row has "
            "no Indian tax payable and no credit, form-67-debug.csv replaces "
            "form-67.csv and the run exits 3. It also exits 3 when form-67.csv "
            "files a fractional Rate(%) to enter by hand after upload."
        ),
    )
    _add_fidelity_cma_input_argument(cma_income)
    cma_income.add_argument(
        "--tax-forms",
        dest="tax_forms",
        type=Path,
        required=True,
        metavar="DIR",
        help="Directory containing the observed Fidelity Form 1042-S packet PDF.",
    )
    _add_fidelity_cma_config_argument(cma_income)
    cma_income.add_argument(
        "--output-dir",
        dest="output_dir",
        type=Path,
        required=True,
        metavar="DIR",
        help=(
            "Destination for schedule-os.csv, schedule-fsi.csv, schedule-tr.csv, "
            "and the official form-67.csv, or form-67-debug.csv when a Form 67 "
            "row is BLANK or FTC is blocked. Must exist."
        ),
    )
    _add_financial_year_argument(cma_income, help_text=_FIDELITY_CMA_FY_HELP)
    _add_suppress_cross_ay_warning_argument(
        cma_income, help_text=_FIDELITY_CMA_SUPPRESS_CROSS_AY_HELP
    )
    _add_debug_argument(cma_income)
    _add_itr_form_argument(cma_income, required=True)

    cma_cg = subparsers.add_parser(
        "fidelity-cma-cg",
        help="Produce Fidelity CMA SPAXX Schedule CG review CSVs.",
        description=(
            "FIFO-match observed SPAXX buys, dividend reinvestments, and sales "
            "from Fidelity CMA monthly statements, then emit Schedule CG STCG "
            "and LTCG review CSVs. A lot held 24 months or less is short-term "
            "under both possible section 50AA interpretations; any in-FY holding "
            "over 24 months refuses until authoritative foreign-fund treatment exists."
        ),
    )
    _add_fidelity_cma_input_argument(cma_cg)
    _add_fidelity_cma_config_argument(cma_cg)
    cma_cg.add_argument(
        "--output-dir",
        dest="output_dir",
        type=Path,
        required=True,
        metavar="DIR",
        help="Destination for schedule-cg.csv and schedule-cg-ltcg.csv. Must exist.",
    )
    _add_financial_year_argument(cma_cg, help_text=_FIDELITY_CMA_FY_HELP)
    _add_suppress_cross_ay_warning_argument(cma_cg, help_text=_FIDELITY_CMA_SUPPRESS_CROSS_AY_HELP)
    _add_itr_form_argument(cma_cg, required=True)

    cma_fa = subparsers.add_parser(
        "fidelity-cma-fa",
        help="Produce Fidelity CMA Schedule FA Tables A2 and A3 review or upload output.",
        description=(
            "Reconstruct SPAXX lots, daily closing values, and audited A2 peak "
            "bounds from the complete Fidelity CMA statement bundle. The A2 peak "
            "stays blank with a warning by default. To file a computed bound, "
            "user.toml must explicitly suppress that warning and choose lower or "
            "upper; no bound is selected implicitly."
        ),
    )
    _add_fidelity_cma_input_argument(cma_fa)
    cma_fa.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "Path to user.toml (must contain a [fidelity_cma] block with "
            'status = "BeneficialOwner").'
        ),
    )
    cma_fa.add_argument(
        "--output",
        type=Path,
        required=True,
        metavar="PATH",
        help="Table A3 output path; Table A2 is written to a sibling -a2 file.",
    )
    cma_fa.add_argument(
        "--calendar-year",
        type=int,
        required=True,
        metavar="CY",
        help="Schedule FA calendar year (for example, 2025 for AY 2026-27).",
    )
    _add_format_argument(
        cma_fa,
        "fidelity-cma-fa",
        help_text=(
            "Output format (default: review-csv). 'filing-csv' writes the 12-column "
            "Schedule FA A2/A3 upload files the ITR portal imports, without audit columns."
        ),
    )
    cma_fa.add_argument(
        "--price-cache-dir",
        dest="price_cache_dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "SPAXX daily-NAV cache directory; defaults to a "
            "'.ibkr-fa-price-cache' folder inside the --input directory."
        ),
    )


def _add_netbenefits_cg_all_parsers(subparsers: argparse._SubParsersAction) -> None:
    """Register netbenefits-cg and netbenefits-all.

    They follow the StockPlan commands in the subcommand order, so they register
    apart from ``_add_netbenefits_fa_income_parsers``.
    """
    nb_cg = subparsers.add_parser(
        "netbenefits-cg",
        help=(
            "Produce Schedule CG (STCG + LTCG) review CSVs for Fidelity ESPP lots, "
            "for an FY the statements prove had no sale."
        ),
        description=(
            "Produce the Schedule CG short-term (schedule-cg.csv) and "
            "long-term (schedule-cg-ltcg.csv) review CSVs for Fidelity "
            "NetBenefits (e.g. Microsoft ESPP) lots, from a bundle directory "
            "of the closed-lots CSV, the purchase-confirmation PDFs and the "
            "Stock Plan Services statement PDFs, plus the [netbenefits] config "
            "block. For now the CSVs are written only for an FY the statements "
            "prove had no sale: they must cover, with no gap, from the later of "
            "the FY start and netbenefits.account.account_opening_date through "
            "ten days after the FY end, and the closed-lots CSV must list no sale "
            "or transfer in the FY. An FY with a sale or transfer refuses until ibkr-fa can "
            "check sales against the statements. The cost of acquisition is the "
            "historical gross INR FMV configured in "
            "[[netbenefits.cg_fmv_values]] (§49(2AA) — the value taken into "
            "account under §17(2)(vi)), NOT Fidelity's US 'Cost basis'; the "
            "STCG/LTCG split is the 24-month foreign-equity rule from the "
            "acquire/sale dates (never Fidelity's US 'Term'); sale proceeds use "
            "Rule 115(1), Explanation (2)(f). Missing/ambiguous FMV evidence, an "
            "oversell, or any lot the engines would silently drop REFUSES "
            "rather than filing a guessed figure. The two CSVs are written "
            "into --output-dir."
        ),
    )
    nb_cg.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="DIR",
        help=(
            "The NetBenefits bundle DIRECTORY (closed-lots CSV + confirmation PDFs + "
            "the Stock Plan Services statement PDFs covering the capital-gains "
            "window described above)."
        ),
    )
    _add_netbenefits_config_argument(nb_cg)
    nb_cg.add_argument(
        "--output-dir",
        dest="output_dir",
        type=Path,
        required=True,
        metavar="DIR",
        help=(
            "Destination DIRECTORY for the two review CSVs (schedule-cg.csv, "
            "schedule-cg-ltcg.csv). Must exist."
        ),
    )
    _add_financial_year_argument(nb_cg, help_text=_NETBENEFITS_FY_HELP)
    _add_suppress_cross_ay_warning_argument(
        nb_cg,
        help_text=(
            "Suppress the AY 2027-28+ FX-policy cross-AY verification warning (see schedule-cg)."
        ),
    )
    _add_itr_form_argument(nb_cg, required=True)

    nb_all = subparsers.add_parser(
        "netbenefits-all",
        help="Produce every NetBenefits ESPP schedule in one run (FA + income + CG).",
        description=(
            "One-shot orchestrator for a Fidelity NetBenefits (e.g. Microsoft "
            "ESPP) bundle: runs netbenefits-fa (Schedule FA Table A3/A2), "
            "netbenefits-income (Schedule OS/FSI/TR/Form 67), and netbenefits-cg "
            "(Schedule CG STCG + LTCG) against the SAME --input bundle, writing "
            "all files into --output-dir. In all-mode, the income leg also "
            "includes the NetBenefits capital gains in Schedule FSI, so FSI and "
            "Schedule CG reconcile. For now both are filed only for an FY the "
            "statements prove had no sale: they must cover, with no gap, from "
            "the later of the FY start and netbenefits.account.account_opening_date "
            "through ten days after the FY end, and an FY with a sale or transfer refuses until "
            "ibkr-fa can check sales against the statements. Schedule FA's "
            "calendar year is derived "
            "from --financial-year (FY YYYY-YY maps to CY YYYY). Runs every "
            "command even after one fails, prints a status-first summary to "
            "stderr, and exits with the worst per-command outcome. If any command "
            "fails, it removes every file this run wrote, so a failed run leaves "
            "nothing to file. A failed write to the console (for example, to a "
            "closed pipe) instead stops the run at once: it attempts to remove every output "
            "and exits 4."
        ),
    )
    nb_all.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="DIR",
        help="The NetBenefits bundle DIRECTORY (lots CSVs + confirmation/statement PDFs).",
    )
    _add_netbenefits_config_argument(nb_all)
    nb_all.add_argument(
        "--output-dir",
        dest="output_dir",
        type=Path,
        required=True,
        metavar="DIR",
        help="Destination DIRECTORY for all generated schedule CSVs (created if absent).",
    )
    _add_financial_year_argument(
        nb_all,
        help_text="FY in YYYY-YY or YYYY-YYYY form (e.g., 2025-26 for AY 2026-27).",
    )
    nb_all.add_argument(
        "--no-fetch-prices",
        dest="no_fetch_prices",
        action="store_true",
        help="Do NOT fetch market prices for the Schedule FA peak value (see netbenefits-fa).",
    )
    nb_all.add_argument(
        "--price-cache-dir",
        dest="price_cache_dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Price-cache directory for Schedule FA; defaults to a "
            "'.ibkr-fa-price-cache' folder inside the --input directory."
        ),
    )
    nb_all.add_argument(
        "--quicko-workbook",
        dest="quicko_workbook",
        action="store_true",
        help=(
            "Also write schedule-fa-quicko.xlsx, the Quicko Schedule FA upload (see "
            "netbenefits-fa). If it cannot be written, the FA command fails."
        ),
    )
    _add_suppress_cross_ay_warning_argument(
        nb_all,
        help_text="Suppress the AY 2027-28+ FX-policy cross-AY verification warning.",
    )
    _add_debug_argument(nb_all)
    _add_itr_form_argument(nb_all, required=True)
    nb_all.add_argument(
        "--reconcile-1042s-fy-withholding-usd",
        dest="reconcile_1042s_usd",
        type=_parse_usd_2dp,
        default=None,
        metavar="USD",
        help=(
            "Cross-check the FTC withholding against the 1042-S (see netbenefits-income). "
            "A mismatch refuses (exit 4) and leaves no output."
        ),
    )


def _identity_fallback_warning_lines(identity_supplied_from_fallback: frozenset[str]) -> list[str]:
    """Return the safety-warning block for the #141 identity-fallback path.

    Empty when no fallback was used (the common case — no behavior
    change for users not passing ``--accept-anonymised-statement``).

    The cross-check-trivially-true sentence is gated on whether
    ``"Account"`` specifically was substituted: a Name-only fallback
    (Account row genuinely present in the statement) still gets a
    statement-verified cross-check, just with a user-attested holder
    name. T1 review of PR #166 (claude-opus-4.7-xhigh, P3) — the
    earlier unconditional wording would be inaccurate for the
    Name-only path (rare in practice today since IBKR's privacy
    exporter hides both together, but the parser correctly supports
    independent Name-only / Account-only paths and a future export
    config could exercise it).
    """
    if not identity_supplied_from_fallback:
        return []
    fields_descr = " and ".join(f"{name!r}" for name in sorted(identity_supplied_from_fallback))
    account_in_fallback = "Account" in identity_supplied_from_fallback
    cross_check_clause = (
        " The account-ID cross-check therefore passed trivially against a "
        "user-attested value rather than a statement-verified one."
        if account_in_fallback
        else " (The Account row WAS present in the statement, so the "
        "account-ID cross-check remained statement-verified — only the "
        "holder-name was user-attested.)"
    )
    return [
        f"⚠️  Identity field(s) {fields_descr} supplied from user.toml, "
        f"NOT from the input statement (--accept-anonymised-statement was "
        f"passed and IBKR's export hides these fields).{cross_check_clause} "
        f"You are responsible for confirming the statement belongs to the "
        f"same account as user.toml before filing — re-export from IBKR "
        f"with all account-identity fields enabled to restore the "
        f"statement-verified safety net. See https://github.com/jay-tau/"
        f"ibkr-fa/issues/141."
    ]


def _portal_labels_fallback_warning_lines(
    *,
    portal_labels_used_forward_compat_fallback: bool,
    portal_labels_assessment_year_start: int,
) -> list[str]:
    """Combined fallback advisories: portal labels (issue #65) +
    FX-policy citations (issue #207).

    Both share the same AY input (every pipeline's FX policy and
    writer use the same ``financial_year.assessment_year_start``), so
    one helper returns the concatenated list — callers can iterate
    in a single ``for`` block. Empty list when neither subsystem
    needs to warn.

    Args:
        portal_labels_used_forward_compat_fallback: ``True`` iff the
            writer's snapshot lookup hit the +1 forward-compat
            fallback in
            :func:`ibkr_fa.output.portal_labels.for_ay`.
        portal_labels_assessment_year_start: 4-digit AY start year
            the writer / policy was asked for (e.g., ``2027`` for
            AY 2027-28). Surfaced verbatim so the user can correlate
            with their ``--financial-year`` argument.
    """
    lines: list[str] = []
    lines.extend(
        _portal_labels_only_fallback_warning_lines(
            portal_labels_used_forward_compat_fallback=portal_labels_used_forward_compat_fallback,
            portal_labels_assessment_year_start=portal_labels_assessment_year_start,
        )
    )
    lines.extend(
        _fx_citations_fallback_warning_lines(
            assessment_year_start=portal_labels_assessment_year_start,
        )
    )
    return lines


def _portal_labels_only_fallback_warning_lines(
    *,
    portal_labels_used_forward_compat_fallback: bool,
    portal_labels_assessment_year_start: int,
) -> list[str]:
    """Issue #65: stderr advisory when the writer fell back to the
    most-recent verified portal-labels snapshot for an AY whose own
    snapshot has not yet been published.

    Empty list (no advisory) when the writer used a registered
    snapshot — the labels are AY-verified, nothing to warn about.

    Most callers should use :func:`_portal_labels_fallback_warning_lines`
    which also chains the FX-policy citation forward-compat advisory
    (issue #207). This helper exists for tests that want to assert
    the portal-labels branch in isolation.

    Args:
        portal_labels_used_forward_compat_fallback: ``True`` iff the
            writer's snapshot lookup hit the +1 forward-compat
            fallback in
            :func:`ibkr_fa.output.portal_labels.for_ay`.
        portal_labels_assessment_year_start: 4-digit AY start year
            the writer was asked for (e.g., ``2027`` for AY 2027-28).
    """
    if not portal_labels_used_forward_compat_fallback:
        return []
    ay_label = (
        f"{portal_labels_assessment_year_start}-"
        f"{(portal_labels_assessment_year_start + 1) % 100:02d}"
    )
    return [
        f"⚠️  Portal labels for AY {ay_label} have not yet been verified "
        f"against the corresponding ITR-2 utility — the writer fell back "
        f"to the most-recent verified snapshot. Cross-check each portal "
        f"cell label against the AY {ay_label} ITR-2 utility before "
        f"filing; see https://github.com/jay-tau/ibkr-fa/issues/65 for "
        f"the snapshot-bump workflow."
    ]


def _fx_citations_fallback_warning_lines(
    *,
    assessment_year_start: int,
) -> list[str]:
    """Issue #207: stderr advisory when the FX-policy citation
    snapshot for the requested AY isn't registered and the policy
    fell back to the most-recent verified snapshot via the +1
    forward-compat rule of
    :func:`ibkr_fa.fx.policy_citations.for_ay`.

    Empty list (no advisory) when:

    * The requested AY has its own registered snapshot
      (citations are AY-verified — nothing to warn about); OR
    * The requested AY is too far in the past or future for the
      +1-only fallback rule. Those cases would raise
      :class:`~ibkr_fa.fx.policy_citations.UnknownAssessmentYearError`
      at policy construction time and never reach the CLI renderer.

    The advisory text is dynamic so it correctly names the
    actually-used fallback snapshot AY (which moves whenever a new
    per-AY snapshot is registered in ``_SNAPSHOTS_BY_AY_START``).

    Args:
        assessment_year_start: 4-digit AY start year the policy was
            asked for. Same source as
            ``RunReport.portal_labels_assessment_year_start``.
    """
    if not fx_policy_citations.is_forward_compat_fallback(assessment_year_start):
        return []
    requested_label = f"{assessment_year_start}-{(assessment_year_start + 1) % 100:02d}"
    latest_ay = fx_policy_citations.latest_registered_ay()
    fallback_label = f"{latest_ay}-{(latest_ay + 1) % 100:02d}"
    return [
        f"⚠️  FX-policy citations for AY {requested_label} have not yet "
        f"been verified against the corresponding Income-tax Rules text — "
        f"the policy fell back to the most-recent verified snapshot "
        f"(AY {fallback_label}). The Rule citations rendered into audit "
        f"trails and any raised MissingFxRateError will name AY "
        f"{fallback_label} rules, NOT AY {requested_label} rules. "
        f"Verify the FX-rule citations against the AY {requested_label} "
        f"Income-tax Rules before filing; see "
        f"https://github.com/jay-tau/ibkr-fa/issues/207 for the "
        f"snapshot-bump workflow."
    ]


def _cross_fy_reversal_warning_lines(
    cross_fy_reversal_report: WhtCrossFyReversalReport,
) -> list[str]:
    """Issue #224: the stderr advisory for current-FY reversals whose
    paired original was in a prior FY. They were EXCLUDED from
    per-country totals, and the prior FY's return may need amendment.

    Orphan reversals and reversals ambiguous across FYs refuse in the
    compute layer (#893), so they have no advisory. Returns an empty
    list when nothing was excluded.
    """
    lines: list[str] = []
    rep = cross_fy_reversal_report
    if rep.cross_fy_excluded_count > 0:
        amounts_fragment = ", ".join(
            f"{amount} {currency}"
            for currency, amount in sorted(rep.cross_fy_excluded_amount_foreign.items())
        )
        lines.append(
            f"ℹ️  Excluded {rep.cross_fy_excluded_count} cross-FY WHT "  # noqa: RUF001
            f"reversal(s) totalling {amounts_fragment} from per-country "
            f"foreign-tax totals — these reverse WHT claimed in a prior "
            f"FY (heuristic ISIN + equal-and-opposite + LIFO pairing). "
            f"The prior FY's return may need amendment to subtract the "
            f"refund; see https://github.com/jay-tau/ibkr-fa/issues/224."
        )
    return lines


def _a2_period_end_reference_lines(row: FaA2Row) -> list[str]:
    """A labelled period-end (Dec 31) cash/non-cash split for the A2 peak
    manual-fill guidance — a REFERENCE to help the user establish the peak,
    never the filed figure. Empty unless both the NAV total and its cash
    component were parsed (an all-securities account has no Cash row).
    """
    total = row.audit_closing_balance_base_ccy
    cash = row.audit_period_end_cash_base_ccy
    if total is None or cash is None:
        return []
    ccy = row.base_currency
    q = Decimal("0.01")
    # Quantize the operands FIRST, then derive the non-cash remainder from the
    # rounded figures, so the printed identity (total = cash + non-cash) always
    # adds up on screen (independent rounding could otherwise be off by 0.01).
    # "+ 0" normalises a possible "-0.00" to "0.00".
    total_q = total.quantize(q)
    cash_q = cash.quantize(q)
    non_cash_q = (total_q - cash_q) + Decimal("0")
    return [
        f"     For reference, your PERIOD-END (Dec 31) account was {ccy} "
        f"{total_q} = {cash_q} cash + {non_cash_q} non-cash assets (securities "
        "etc.). These are year-end, NOT peak-day, figures (the peak may fall on "
        "another day, and each holding peaks on its own day) — do NOT simply "
        "add them; use IBKR's daily NAV report to find the true peak-day total.",
    ]


def _a2_closing_blank_lines(rows: Sequence[FaA2Row]) -> list[str]:
    """Each blank A2 closing balance's cause and remedy, read from its row (#1013)."""
    lines: list[str] = []
    for reason in FaA2ClosingBlankReason:
        if any(row.closing_blank_reason is reason for row in rows):
            cause, remedy = FA_A2_CLOSING_BLANK_TEXT[reason]
            lines.append(f"      The closing balance is blank because {cause}.")
            lines.append(f"      {remedy}")
    return lines


def _render_cbdt_json_report(report: RunReport, *, include_global_advisories: bool = True) -> str:
    """Render the success summary for a ``cbdt-json-fragment`` run.

    A written JSON fragment is complete by construction (the writer refuses
    otherwise), so this surfaces the path, the schema it validated against, the
    user-supplied A2 peak provenance (never silent), the merge instruction, and
    the standing BMA / ITR compliance advisories (unless
    ``include_global_advisories`` is False).
    """
    calendar_year = report.portal_labels_assessment_year_start - 1
    itr_form = report.cbdt_json_itr_form
    assert itr_form in ITR_FORMS
    return_key = itr_form.replace("-", "")
    lines = [
        f"Wrote the Schedule FA CBDT JSON fragment to {report.output_path}",
        f"  Target form: {itr_form}",
        f"  Schema-validated against {schema_version_for(itr_form)}",
        f"  Tables: A2 (foreign custodial account) + {report.rows_written} A3 holding row(s)",
        f"  Input format: {report.input_format.value}",
        f"  Account: {display_account_id(report.account_id)}",
    ]
    if report.cbdt_json_a2_peak_balance_inr is not None:
        lines.append(
            "  Using your supplied filed value: A2 PeakBalanceDuringPeriod="
            f"{report.cbdt_json_a2_peak_balance_inr} for CY {calendar_year} "
            "(from [[schedule_fa.manual_values]])"
        )
    lines.append("")
    lines.append(
        "  ℹ️  This is a FRAGMENT, not a full return: merge "  # noqa: RUF001
        "ScheduleFA.DtlsForeignCustodialAcc and ScheduleFA.DtlsForeignEquityDebtInterest "
        f"into ITR.{return_key}.ScheduleFA of your {itr_form} JSON, preserving any other tables / "
        "rows already there. The top-level _ibkrFa marker is provenance only — do "
        "not merge it. Run `ibkr-fa merge` to splice it into your saved return."
    )
    if include_global_advisories:
        lines.append("")
        lines.append(BMA_DISCLOSURE_ADVISORY)
        lines.append("")
        lines.append(ITR_FORM_SELECTION_ADVISORY)
    return "\n".join(lines)


def _render_a3_peak_blank_reasons(report: RunReport) -> list[str]:
    """Each blank A3 peak's cause and remedy, over its holdings (#962)."""
    if not report.peak_manual_fill_holdings:
        return [
            "   Peak value: this run wrote no Table A3 row, so enter each holding you "
            "held during the year by hand, peak included."
        ]
    lines: list[str] = []
    for reason, holdings in report.peak_manual_fill_holdings:
        cause, remedy = FA_A3_PEAK_BLANK_TEXT[reason]
        lines.append(f"   Peak value is blank for these holdings because {cause}:")
        lines.extend(f"     - {holding}" for holding in holdings)
        lines.append(f"       {remedy}")
        if reason is FaA3PeakBlankReason.NO_USD_SERIES:
            lines.append("       [[prices.ticker_overrides]]")
            lines.append('       ibkr_ticker = "XNEW"        # your broker symbol')
            lines.append('       isin = "CA99999B1004"       # the holding ISIN (must match)')
            lines.append('       yahoo_symbol = "XOLD"       # the USD price-source symbol')
    return lines


def _render_run_report(  # noqa: PLR0912, PLR0915
    report: RunReport, *, include_global_advisories: bool = True
) -> str:
    """Render the success summary written to stdout.

    ``include_global_advisories=False`` leaves out the advisories ``run-all``
    prints once (see :func:`_run_all_global_advisories`).
    """
    if report.emitted_cbdt_json:
        return _render_cbdt_json_report(report, include_global_advisories=include_global_advisories)
    lines = [
        f"Wrote {report.rows_written} Schedule FA Table A3 row(s) to {report.output_path}",
        f"  Input format: {report.input_format.value}",
        f"  Account: {display_account_id(report.account_id)}",
    ]
    if include_global_advisories and not report.account_id_cross_checked:
        lines.append("")
        lines.append(_MASKED_ACCOUNT_SKIP_WARNING)
    for warning_line in _identity_fallback_warning_lines(report.identity_supplied_from_fallback):
        lines.append("")
        lines.append(warning_line)
    if not report.fx_policy_appropriate_for_ay:
        lines.append("")
        lines.append(
            "⚠️  This run targets AY 2027-28 or later, which is governed "
            "by the Income-tax Act, 2025 + Income-tax Rules, 2026."
        )
        lines.append(
            "   The SBI TTBR rate source is preserved under the new "
            "framework per Rule 206(1) (resident FX rule) and "
            "Rule 207(3)(b) (TTBR definition)."
        )
        lines.append(
            "   However, the Schedule-FA-specific date-selection "
            "semantics — i.e., which dates the TTBR rate is looked up "
            "for when computing initial and closing values — and the "
            "carry-forward fallback across non-publication days have "
            "not yet been verified by ibkr-fa under the 2025-Act "
            "framework (no Rule 206 carry-forward fallback text; both rest on "
            "CBDT FAQ / practice)."
        )
        lines.append(
            "   See https://github.com/jay-tau/ibkr-fa/issues/126 for "
            "verification status. Pass --suppress-cross-ay-warning to "
            "silence this once you've independently verified."
        )
    for warning_line in _portal_labels_fallback_warning_lines(
        portal_labels_used_forward_compat_fallback=(
            report.portal_labels_used_forward_compat_fallback
        ),
        portal_labels_assessment_year_start=report.portal_labels_assessment_year_start,
    ):
        lines.append("")
        lines.append(warning_line)
    if report.short_lots_excluded > 0:
        lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.short_lots_excluded} short lot(s) "  # noqa: RUF001
            f"from Schedule FA Table A3 — short positions are "
            f"liabilities, not equity HELD, so they don't belong on "
            f"A3 (which reports 'Foreign Equity and Debt Interest "
            f"held'). The foreign brokerage account holding the "
            f"short proceeds may itself be reportable on FA Table A2 "
            f"(Foreign Custodial Accounts). "
            f"See https://github.com/jay-tau/ibkr-fa/issues/109."
        )
    if report.orphan_dividends:
        # #142: classification-specific advisory replaces the pre-#142
        # one-size-fits-all "never acquired" message. Two distinct
        # paths route to two different ITR schedules:
        # * previously_held: dividend on Schedule OS, sale on Schedule CG
        # * never_held: CIL/spinoff (Schedule CG) or true orphan (Schedule OS)
        previously_held = [
            o for o in report.orphan_dividends if o.classification == "previously_held"
        ]
        never_held = [o for o in report.orphan_dividends if o.classification == "never_held"]
        if previously_held:
            lines.append("")
            entries = ", ".join(
                f"{o.symbol} (₹{o.total_amount_inr.quantize(Decimal('1'), rounding=ROUND_HALF_UP)} "
                f"paid {o.latest_payment_date.isoformat()}"
                + (f", last sold {o.last_sold_date.isoformat()}" if o.last_sold_date else "")
                + ")"
                for o in previously_held
            )
            lines.append(
                f"ℹ️  Dropped dividends for {len(previously_held)} symbol(s) "  # noqa: RUF001
                f"not held at Dec 31 but previously owned and sold during "
                f"the period: {entries}. The dividend was earned on the "
                f"holding and belongs on Schedule OS (Income from Other "
                f"Sources); the sale itself belongs on Schedule CG "
                f"(Capital Gains). See "
                f"https://github.com/jay-tau/ibkr-fa/issues/142."
            )
        if never_held:
            lines.append("")
            entries = ", ".join(
                f"{o.symbol} (₹{o.total_amount_inr.quantize(Decimal('1'), rounding=ROUND_HALF_UP)} "
                f"paid {o.latest_payment_date.isoformat()})"
                for o in never_held
            )
            lines.append(
                f"ℹ️  Dropped dividends for {len(never_held)} symbol(s) "  # noqa: RUF001
                f"never held as a regular lot during the period: {entries}. "
                f"Typical cause is spinoff payments on tickers never "
                f"acquired as a regular lot. Orphan cash dividends on a "
                f"never-held ticker are typically Schedule OS. See "
                f"https://github.com/jay-tau/ibkr-fa/issues/137."
            )
    # Issue #145 — US-source-dividend withholding advisory. Only fires
    # when the input had at least one US-source dividend; otherwise
    # nothing to advise about. The three branches are mutually
    # exclusive (W-8BEN missing, FTC-via-1040, unexpected backup
    # withholding) and the [account].us_person config field
    # disambiguates them. Non-blocking informational signal — does
    # not affect exit code.
    if report.us_source_dividend_count > 0:
        has_us_wht = report.us_source_withholding_tax_net_paid > Decimal("0")
        if not report.us_person and not has_us_wht:
            lines.append("")
            lines.append(
                f"⚠️  Parsed {report.us_source_dividend_count} US-source "
                f"dividend(s) but found zero withholding tax on US-source "
                f"holdings. For a non-US-person account this typically "
                f"means the W-8BEN at IBKR is missing, expired, or "
                f"incorrect — IBKR should be withholding the India treaty "
                f"rate (~25%) on US-source dividends, and zero withholding "
                f"breaks your foreign tax credit pathway. Action: log into "
                f"IBKR and renew / re-file Form W-8BEN. If you actually "
                f"are a US person (W-9 on file), set "
                f"[account].us_person = true in user.toml to silence this "
                f"and surface the correct FTC-via-1040 advisory instead. "
                f"See https://github.com/jay-tau/ibkr-fa/issues/145."
            )
        elif report.us_person and not has_us_wht:
            lines.append("")
            lines.append(
                f"ℹ️  Parsed {report.us_source_dividend_count} US-source "  # noqa: RUF001
                f"dividend(s) with zero withholding tax. This is the "
                f"expected pattern for a US person (W-9 on file at IBKR): "
                f"IBKR doesn't apply NRA withholding to W-9 accounts, so "
                f"the foreign-tax-credit pathway is via US Form 1040, NOT "
                f"via Indian Form 67 / Schedule TR. Indian Schedule OS "
                f"still reports the gross dividend at slab rate; the "
                f"India-US treaty resident-sourcing rules then govern how "
                f"the US-paid tax is credited back on the Indian return. "
                f"Consult a CA familiar with US-citizen-resident-of-India "
                f"filings. "
                f"See https://github.com/jay-tau/ibkr-fa/issues/145."
            )
        elif report.us_person and has_us_wht:
            lines.append("")
            lines.append(
                f"⚠️  Parsed {report.us_source_dividend_count} US-source "
                f"dividend(s) AND ${report.us_source_withholding_tax_net_paid} "
                f"net US-source withholding tax on an account "
                f"declared as US person ([account].us_person = true). "
                f"IBKR does NOT normally apply NRA withholding to W-9 "
                f"accounts, so non-zero US-source withholding here is "
                f"unusual — possible causes: backup withholding (TIN "
                f"mismatch with the IRS), the [account].us_person flag "
                f"is set in error, or IBKR has incorrectly classified "
                f"the account. Verify your IBKR account-status page and "
                f"compare to your IRS Form 1099-DIV before claiming FTC. "
                f"See https://github.com/jay-tau/ibkr-fa/issues/145."
            )
    if report.manual_fill_columns:
        lines.append("")
        lines.append(
            "⚠️  The following portal columns contain blank values. "
            "Verify each missing fact and edit the review file before pasting into the portal:"
        )
        for col in report.manual_fill_columns:
            lines.append(f"  - {col}")
        if report.name_of_entity_manual_fill_holdings:
            lines.append(
                "   Name of entity is blank for these holdings because the statement's "
                "security description is blank; verify the issuer's legal name and set "
                "name in that ISIN's [[fa.entities]] block, or enter it by hand before filing:"
            )
            for holding in report.name_of_entity_manual_fill_holdings:
                lines.append(f"     - {holding}")
        if report.address_of_entity_manual_fill_holdings:
            lines.append(
                "   Address of entity is missing for these holdings; verify the issuer's "
                "address from authoritative issuer material and set address in one "
                "[[fa.entities]] block per ISIN:"
            )
            for holding in report.address_of_entity_manual_fill_holdings:
                lines.append(f"     - {holding}")
        if report.zip_code_manual_fill_holdings:
            lines.append(
                "   ZIP Code is missing for these holdings; verify the issuer's postal code "
                "from authoritative issuer material and set zip in that ISIN's "
                "[[fa.entities]] block:"
            )
            for holding in report.zip_code_manual_fill_holdings:
                lines.append(f"     - {holding}")
        if report.nature_of_entity_manual_fill_holdings:
            lines.append(
                "   Nature of entity is missing for these holdings; verify the legal "
                "entity type from authoritative issuer material and set nature_of_entity "
                "in that ISIN's [[fa.entities]] block:"
            )
            for holding in report.nature_of_entity_manual_fill_holdings:
                lines.append(f"     - {holding}")
            lines.append('       nature_of_entity = "Company"  # example only; verify yours')
        if FA_A3_PEAK_VALUE_COLUMN in report.manual_fill_columns:
            lines.extend(_render_a3_peak_blank_reasons(report))
        if report.gross_proceeds_manual_fill_holdings:
            lines.append(
                "   Gross proceeds are missing for these disposed holdings because "
                "the source proves only commission-reduced net cash, not the portal's "
                "requested pre-fee sale consideration:"
            )
            for holding in report.gross_proceeds_manual_fill_holdings:
                lines.append(f"     - {holding}")
            lines.append("       Verify and enter gross quantity x execution price.")
        lines.append("")
        lines.append("Exit status: 3 (manual-fill columns present — see above).")
    if report.fa_gross_amount_zeroed_no_cy_dividends:
        lines.append("")
        lines.append(
            "ℹ️  No dividend activity found for the calendar year: this "  # noqa: RUF001
            "Activity Statement has neither a 'Dividends' nor a 'Change in "
            "Dividend Accruals' section (IBKR omits both when there were "
            "none), so the 'Total gross amount paid/credited...' column is "
            "set to 0 for every holding. This zero is inferred only from the "
            "statement provided — verify it reflects no dividend, payment-in-"
            "lieu, or other distribution activity on these holdings before "
            "filing, and edit the value if you received any not shown here."
        )
    if report.interest_credits_entries > 0:
        lines.append("")
        lines.append(
            f"ℹ️  Parsed {report.interest_credits_entries} realized "  # noqa: RUF001
            f"USD credit-interest row(s) from the Activity Statement "
            f"Interest section. Table A2 adds the calendar year's credits "
            f"to the account's gross amount credited (Interest); Table A3 "
            f"doesn't use them. Debit interest (margin interest you paid) "
            f"isn't income and is left out."
        )
    if report.interest_unproven_rows:
        lines.append("")
        lines.append(
            "⚠️  These Interest rows are unproven: an accrual-labelled row, a "
            "positive row without IBKR's credit-interest wording, or a negative "
            "or zero row that isn't debit interest, may change the "
            "interest you received without showing by how much. Table A2 leaves "
            "its Interest amount for manual fill, and schedule-os, schedule-fsi, "
            "schedule-tr and form-67 refuse this statement:"
        )
        lines.extend(f"     - {row.describe()}" for row in report.interest_unproven_rows)
    if report.a2_rows:
        lines.append("")
        lines.append(
            "Wrote Schedule FA Table A2 (foreign custodial account) "
            f"{len(report.a2_rows)} row(s) to {report.a2_output_path}"
        )
        if report.a2_manual_fill_columns:
            lines.append(
                "  ⚠️  Fill these Table A2 portal cell(s) before filing — left "
                "blank because they could not be computed with certainty:"
            )
            for col in report.a2_manual_fill_columns:
                lines.append(f"    - {col}")
                if col == FA_A2_ZIP_CODE_LABEL:
                    lines.append(
                        "      Set [account.custodian].zip to the institution's postal code "
                        "and re-run, or fill it in by hand before filing."
                    )
                elif col == FA_A2_CLOSING_LABEL:
                    lines.extend(_a2_closing_blank_lines(report.a2_rows))
            lines.append(
                "     Find your account's HIGHEST total native-currency value "
                "(securities + cash) on any single day of the calendar year, then "
                "convert it to INR at that peak date's SBI TTBR. Per Rule 114F a custodial "
                "account's value INCLUDES the cash held in it, not just the "
                "securities — and the statement carries no daily account NAV, so "
                "the peak day cannot be derived."
            )
            lines.extend(_a2_period_end_reference_lines(report.a2_rows[0]))
        lines.append(
            "  ℹ️  Table A2 (the account) and Table A3 (the holdings inside "  # noqa: RUF001
            "it) deliberately overlap — do NOT add A2's balance to the A3 "
            "holding values when testing the Black Money Act ₹20-lakh "
            "threshold; that double-counts the same assets. Consult your CA."
        )
    else:
        # a2_rows is empty IFF [account.custodian] is unconfigured (the only
        # no-op path in build_and_write_fa_a2). Silently emitting only A3
        # would leave an incomplete Schedule FA — ITR-2 requires the
        # account-level A2 disclosure too — so warn rather than omit
        # quietly. Non-quietable, like the BMA / ITR advisories below.
        lines.append("")
        lines.append(_FA_A2_SKIPPED_ADVISORY)
    # #63: BMA disclosure advisory — fires on every schedule-fa run
    # because Schedule FA IS the foreign-asset disclosure surface
    # the BMA penalty hooks into. Placed at the bottom of the report
    # so the operational summary remains scannable above and the
    # compliance reminder is the last thing the user sees.
    if include_global_advisories:
        lines.append("")
        lines.append(BMA_DISCLOSURE_ADVISORY)
        # #64: ITR-form selection guidance, after the BMA advisory.
        lines.append("")
        lines.append(ITR_FORM_SELECTION_ADVISORY)
    return "\n".join(lines)


def _preclear_cbdt_json_target(output_path: Path) -> int | None:
    """Before a ``cbdt-json-fragment`` run, ensure no STALE importable JSON is
    left at ``output_path`` on any later failure — without destroying a file we
    did not author.

    A prior ibkr-fa fragment (recognised by its provenance marker) is removed so
    a re-run that succeeds rewrites it and a re-run that fails leaves nothing
    importable. Any other existing file (a hand-merged ScheduleFA, another tool's
    output, an unrelated file the user mis-pointed ``--output`` at), or one that cannot
    be inspected, is preserved and the run is refused. Returns an exit code to
    return immediately, or ``None`` to proceed.
    """
    try:
        if preclear_own_cbdt_fragment(output_path):
            return None
    except (CbdtJsonUnreadableError, OutputRemovalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_DOMAIN_ERROR
    print(
        f"error: refusing to overwrite {output_path} — it was not generated by "
        "ibkr-fa (no provenance marker), so it may be a hand-merged or unrelated "
        "file. Choose a different --output path, or remove it yourself.",
        file=sys.stderr,
    )
    return _EXIT_DOMAIN_ERROR


def _schedule_fa_step(  # noqa: PLR0911
    args: argparse.Namespace, *, include_global_advisories: bool
) -> tuple[int, RunReport | None]:
    """Run ``schedule-fa``: its exit code, and its report unless it failed."""
    emit_json = args.format_id == CbdtScheduleFaJsonWriter.format_id
    if emit_json and args.itr_form is None:
        print(
            "error: --itr-form {ITR-2,ITR-3} is required with --format cbdt-json-fragment",
            file=sys.stderr,
        )
        return _EXIT_ARGPARSE_USAGE, None
    # The JSON preclear, or a CSV/XLSX run's A2 clear and failure cleanup, deletes
    # these files: refuse before any of that if one is an input.
    try:
        refuse_input_collisions(
            (args.output,) if emit_json else (args.output, fa_a2_path(args.output)),
            (("--input", args.input), ("--config", args.config)),
        )
    except OutputCollisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_DOMAIN_ERROR, None
    if emit_json:
        # Pre-clear BEFORE the run so any failure path (refusal, parse, FX, …)
        # leaves no stale importable JSON; never clobber a non-ibkr-fa file.
        preclear_exit = _preclear_cbdt_json_target(args.output)
        if preclear_exit is not None:
            return preclear_exit, None
    try:
        report = run_fa_a3(
            input_path=args.input,
            config_path=args.config,
            output_path=args.output,
            calendar_year=args.calendar_year,
            format_id=args.format_id,
            itr_form=args.itr_form,
            allow_masked_account=args.allow_masked_account,
            accept_anonymised_statement=args.accept_anonymised_statement,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
            fetch_prices=not args.no_fetch_prices,
            price_cache_dir=args.price_cache_dir,
        )
        with report.run_outputs.removed_on_failure():
            rendered = _render_run_report(
                report, include_global_advisories=include_global_advisories
            )
    except CbdtJsonIncompleteError as exc:
        # All-or-nothing JSON: a missing filed value. Same "supply X, re-run"
        # family as a CSV manual-fill column → exit 3 (ordered before the generic
        # IbkrFaError handler). A structural refusal (CbdtJsonStructuralError,
        # e.g. an unverified account binding) or a CbdtSchemaError is not manual
        # fill: it falls through to exit 4.
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_MANUAL_FILL_REQUIRED, None
    except _DOMAIN_ERRORS as exc:
        # Domain errors carry structured context; their __str__ already
        # produces a helpful one-line message (see exceptions.py).
        return _domain_error("schedule-fa", exc), None
    _print_report(report.run_outputs, (), rendered)
    if report.emitted_cbdt_json:
        # A written JSON fragment is complete by construction (the writer refuses
        # otherwise), so there are no manual-fill cells to resolve → success.
        return _EXIT_OK, report
    if report.manual_fill_columns or report.a2_manual_fill_columns or not report.a2_rows:
        return _EXIT_MANUAL_FILL_REQUIRED, report
    return _EXIT_OK, report


def _run_schedule_fa(args: argparse.Namespace) -> int:
    """Dispatch the ``schedule-fa`` subcommand."""
    return _schedule_fa_step(args, include_global_advisories=True)[0]


def _render_espp_perquisite_advisories(
    advisories: tuple[EsppPerquisiteAdvisory, ...],
) -> list[str]:
    """Render the ESPP perquisite Form-16 advisories (advisory only, never a filed
    figure). One bullet per purchase; INR shown when the Rule 115(1), Explanation (2)(a)
    preceding-month-end TTBR resolved, else a USD-only reminder."""
    if not advisories:
        return []
    lines = [
        "",
        "ESPP salary perquisite — advisory only (not a Schedule FA filed figure):",
    ]
    for adv in advisories:
        usd = f"USD {adv.perquisite_usd:.2f}"
        if adv.perquisite_inr is not None:
            head = (
                f"  - {adv.purchase_date.isoformat()} (FY {adv.financial_year}): verify "
                f"₹{adv.perquisite_inr:,} ({usd} = FMV - contribution) is included as salary in "
                "your Form 16. INR at the Rule 115(1), Explanation (2)(a) "
                "preceding-month-end SBI TTBR — "
                f"indicative; your Form 16 figure governs."
            )
        else:
            head = (
                f"  - {adv.purchase_date.isoformat()} (FY {adv.financial_year}): verify the ESPP "
                f"discount perquisite {usd} (= FMV - contribution) is included as salary in your "
                "Form 16 (Rule 115(1), Explanation (2)(a) preceding-month-end "
                "TTBR unavailable — verify the INR "
                f"via Form 16)."
            )
        lines.append(head)
        lines.append(
            f"    For future capital gains the §49(2AA) cost of acquisition is the FMV at purchase "
            f"(USD {adv.fmv_total_usd:.2f}), NOT the USD {adv.contribution_usd:.2f} you paid."
        )
    return lines


def _render_netbenefits_a2_institution_warning(report: NetBenefitsFaRunReport) -> list[str]:
    """The ADR 0021 warning when the A2 institution is Fidelity Stock Plan Services."""
    if not report.a2_institution_is_fidelity_sps:
        return []
    return [
        "  warning: [netbenefits.account] institution_name names Fidelity Stock Plan "
        "Services. Its participant agreement says it does not maintain an account or "
        "hold assets for the participant (ADR 0021). This run still wrote the configured "
        "name into Table A2. Check your NetBenefits documents for the firm that "
        "holds your account; if they name a different firm, change institution_name, "
        "address and zip under [netbenefits.account]."
    ]


def _render_netbenefits_peak_blank_rows(report: NetBenefitsFaRunReport) -> list[str]:
    """Each blank A3 peak's cause and remedy, over its rows (#1549)."""
    peak_label = "A3 / Quicko" if report.quicko_output_path is not None else "A3"
    lines: list[str] = []
    for reason, rows in report.a3_peak_blank_rows:
        cause, remedy = FA_A3_PEAK_BLANK_TEXT[reason]
        lines.append(
            f"  {peak_label} Peak value(s) for row(s) {', '.join(map(str, rows))}: "
            f"MANUAL-FILL — the peak is blank because {cause}. {remedy}"
        )
    return lines


def _render_netbenefits_fa_report(report: NetBenefitsFaRunReport) -> str:
    """Render the netbenefits-fa success summary (Table A3 + Table A2)."""
    if report.emitted_cbdt_json:
        itr_form = report.cbdt_json_itr_form
        assert itr_form in ITR_FORMS
        return "\n".join(
            [
                f"Wrote the CBDT Schedule FA JSON fragment ({report.rows_written} Table A3 "
                f"holding(s) + the Table A2 account) to {report.output_path}.\n"
                "  A2 Peak Balance During the Period: "
                f"\u20b9{report.cbdt_json_a2_peak_balance_inr} "
                "(from [[netbenefits.a2_peak_overrides]]).\n"
                f"  Target form: {itr_form}. Import it with `ibkr-fa merge`. This fragment "
                "covers ONLY the NetBenefits account — splice any IBKR Schedule FA separately. "
                "Verify every figure with your CA before filing.",
                *_render_netbenefits_a2_institution_warning(report),
            ]
        )
    lines = [
        f"Wrote {report.rows_written} Schedule FA Table A3 row(s) for "
        f"{report.security_name} to {report.output_path}.",
    ]
    for index, (source, provenance) in enumerate(report.close_sources, start=1):
        flag = (
            "  [market fallback — not broker-stated]"
            if source is CloseSource.YAHOO_FALLBACK
            else ""
        )
        lines.append(f"  Row {index} Dec-31 close: {provenance}{flag}")
    lines.extend(_render_espp_perquisite_advisories(report.perquisite_advisories))
    for column in report.a3_manual_fill_columns:
        if column == FA_A3_PEAK_VALUE_COLUMN:
            lines.extend(_render_netbenefits_peak_blank_rows(report))
        elif column == FA_A3_ZIP_CODE_COLUMN:
            lines.append(
                f"  A3 {column}: MANUAL-FILL — set [netbenefits.security].entity_zip to the "
                "issuer's postal code, verified from authoritative issuer material, and "
                "re-run, or fill it in by hand before filing."
            )
        else:
            lines.append(f"  A3 {column}: MANUAL-FILL — fill it in by hand before filing.")
    if report.a2_output_path is not None:
        lines.append("")
        lines.append(f"Wrote Schedule FA Table A2 (the account itself) to {report.a2_output_path}.")
        lines.append(
            "  Closing balance = the held securities (Σ A3) + the proven-zero Dec-31 cash."
        )
        for col in report.a2_manual_fill_columns:
            if col == FA_A2_ZIP_CODE_LABEL:
                lines.append(
                    f"  {col}: MANUAL-FILL — set [netbenefits.account].zip to the "
                    "institution's postal code and re-run, or fill it in by hand before filing."
                )
            else:
                lines.append(
                    f"  {col}: MANUAL-FILL — select your account's highest native-currency "
                    "single-day total (securities + cash), then convert it at that peak date's "
                    "SBI TTBR; the statements "
                    "do not provide a complete daily account-value series."
                )
        lines.append(
            "  \u2139\ufe0f  Table A2 (the account) and Table A3 (the holdings inside it) "
            "deliberately overlap — do NOT add A2's balance to the A3 holding values when "
            "testing the Black Money Act \u20b920-lakh threshold; that double-counts the same "
            "assets. Consult your CA."
        )
    lines.extend(_render_netbenefits_a2_institution_warning(report))
    if report.quicko_output_path is not None:
        lines.append("")
        lines.append(
            "Wrote Quicko Foreign Shares & Securities (Other Broker) upload to "
            f"{report.quicko_output_path}."
        )
        if report.quicko_zip_manual_fill_rows:
            lines.append(
                f"  {report.quicko_zip_manual_fill_rows} Quicko ZIP Code value(s): "
                "MANUAL-FILL — add the issuer postal code before upload."
            )
    lines.append("")
    lines.append("!! NOT filing-complete: fill the A2 peak balance above, then emit the")
    lines.append("   CBDT JSON fragment with a verified [[netbenefits.a2_peak_overrides]] value.")
    return "\n".join(lines)


def _run_netbenefits_fa(args: argparse.Namespace) -> int:
    """Dispatch the ``netbenefits-fa`` subcommand."""
    if args.format_id == CbdtScheduleFaJsonWriter.format_id and args.itr_form is None:
        print(
            "error: --itr-form {ITR-2,ITR-3} is required with --format cbdt-json-fragment",
            file=sys.stderr,
        )
        return _EXIT_ARGPARSE_USAGE
    if args.quicko_workbook and args.format_id != "filing-csv":
        print(
            "error: --quicko-workbook needs --format filing-csv; "
            f"{args.format_id} never writes the Quicko workbook",
            file=sys.stderr,
        )
        return _EXIT_ARGPARSE_USAGE
    try:
        report = run_netbenefits_fa(
            input_dir=args.input,
            config_path=args.config,
            output_path=args.output,
            calendar_year=args.calendar_year,
            fetch_prices=not args.no_fetch_prices,
            price_cache_dir=args.price_cache_dir,
            format_id=args.format_id,
            itr_form=args.itr_form,
            quicko_workbook=args.quicko_workbook,
        )
        with report.run_outputs.removed_on_failure():
            rendered = _render_netbenefits_fa_report(report)
    except CbdtJsonIncompleteError as exc:
        # All-or-nothing JSON: a missing filed value (the A2 peak / a blank ZIP).
        # Same "supply X, re-run" family as a manual-fill column → exit 3 (ordered
        # before the generic IbkrFaError handler). A structural refusal (e.g. an
        # A2 identifier that isn't participant_no) or a CbdtSchemaError falls
        # through to exit 4.
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_MANUAL_FILL_REQUIRED
    except _DOMAIN_ERRORS as exc:
        return _domain_error("netbenefits-fa", exc)
    _print_report(report.run_outputs, (), rendered)
    if report.emitted_cbdt_json:
        # A written JSON fragment is complete by construction (the writer refuses
        # otherwise) → success.
        return _EXIT_OK
    return _EXIT_MANUAL_FILL_REQUIRED


def _form67_output_name(readiness: Form67Readiness) -> str:
    """Which Form 67 file a source-income run wrote (#601)."""
    if readiness is Form67Readiness.BLANK:
        return (
            "Form 67 debug CSV (official Form 67 CSV withheld: one or more entries lack "
            "Indian tax payable and credit)"
        )
    return "official Form 67 CSV"


def _income_outputs_line(report: NetBenefitsIncomeRunReport | StockplanIncomeRunReport) -> str:
    """The NetBenefits and StockPlan income summaries' first line: what was written where."""
    fy = report.financial_year
    return (
        f"Wrote Schedule OS / FSI / TR filing CSVs and "
        f"{_form67_output_name(report.form67_readiness)} for FY "
        f"{fy.start_date.year}-{fy.end_year_2digit:02d} to {report.output_dir}."
    )


def _income_row_counts_line(report: NetBenefitsIncomeRunReport | StockplanIncomeRunReport) -> str:
    """The NetBenefits and StockPlan income summaries' row-count line."""
    return (
        f"  Schedule OS foreign-source 1a(i) detail rows: {report.os_foreign_rows}; "
        f"Schedule FSI rows: {report.fsi_rows}; Schedule TR rows: {report.tr_rows}; "
        f"Form 67 entries: {report.form67_entries}."
    )


def _render_netbenefits_income_report(report: NetBenefitsIncomeRunReport) -> str:
    """Render the netbenefits-income success summary."""
    lines = [
        _income_outputs_line(report),
        f"  Dividends: {report.dividend_count}; withholdings: {report.withholding_count}.",
        _income_row_counts_line(report),
    ]
    if report.cg_stcg_rows or report.cg_ltcg_rows:
        lines.append(
            f"  Schedule FSI includes NetBenefits capital gains: "
            f"{report.cg_stcg_rows} STCG, {report.cg_ltcg_rows} LTCG."
        )
    lines.extend(_render_1042s_reconciliation(report))
    return "\n".join(lines)


def _render_1042s_reconciliation(report: NetBenefitsIncomeRunReport) -> list[str]:
    """The 1042-S withholding reconciliation line (#I16); a mismatch refuses before this (#918)."""
    total = f"USD {report.statement_withholding_usd}"
    if report.reconcile_1042s_usd is not None:
        return [
            f"  \u2713 1042-S reconciliation: the FY-allocated 1042-S total ({total}) matches "
            "the statement-derived FTC withholding."
        ]
    # Nothing supplied and no withholding: nothing to reconcile later (#361).
    if report.withholding_count == 0:
        return []
    return [
        f"  \u2139\ufe0f  The FTC withholding ({total}) is statement-derived. Reconcile it against "
        "the authoritative 1042-S / SPS Grantor Letter once issued (typically the following "
        "year, so it covers a US CALENDAR year - allocate its withholding to this Indian "
        "FY) via --reconcile-1042s-fy-withholding-usd. Do NOT delay filing for the 1042-S.",
    ]


def _run_netbenefits_income(args: argparse.Namespace) -> int:
    """Dispatch the ``netbenefits-income`` subcommand."""
    return _run_report_command(
        "netbenefits-income",
        lambda: run_netbenefits_income(
            input_dir=args.input,
            config_path=args.config,
            output_dir=args.output_dir,
            financial_year=args.financial_year,
            itr_form=args.itr_form,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
            reconcile_1042s_usd=args.reconcile_1042s_usd,
            include_capital_gains=getattr(args, "include_capital_gains", False),
            debug=getattr(args, "debug", False),
        ),
        lambda report: (
            _render_income_warnings(
                report, debug=getattr(args, "debug", False), output_dir=args.output_dir
            ),
            _render_netbenefits_income_report(report),
        ),
        lambda report: _EXIT_MANUAL_FILL_REQUIRED if report.manual_fill_required else _EXIT_OK,
    )


def _render_fidelity_cma_income_report(report: FidelityCmaIncomeRunReport) -> str:
    fy = report.financial_year
    status = (
        "complete"
        if report.filing_complete
        else "BLOCKED"
        if not report.ftc_complete
        else "ACTION NEEDED"
    )
    # #1453: a blocked FTC withholds the official Form 67 CSV (see the pipeline).
    form67_output = (
        "Form 67 debug CSV (official Form 67 CSV withheld: FTC is blocked; see the warning above)"
        if not report.ftc_complete
        else _form67_output_name(report.form67_readiness)
    )
    return "\n".join(
        [
            (
                "Wrote Fidelity CMA Schedule OS / FSI / TR filing CSVs and "
                f"{form67_output} "
                f"for FY {fy.start_date.year}-{fy.end_year_2digit:02d}."
            ),
            (
                f"  Dividends: {report.dividend_count}; Credit Interest: "
                f"{report.interest_count} (Schedule OS 1b(ix) total: "
                f"₹{report.interest_total_inr_rounded}); actual withholdings: "
                f"{report.actual_withholding_count}; eligible withholdings: "
                f"{report.eligible_withholding_count}."
            ),
            (
                f"  Schedule OS dividend rows: {report.os_foreign_rows}; interest rows: "
                f"{report.os_interest_rows}; FSI: {report.fsi_rows}; TR: "
                f"{report.tr_rows}; Form 67: {report.form67_entries}."
            ),
            *(
                [
                    "  Schedule FSI includes Fidelity CMA SPAXX capital gains: "
                    f"{report.cg_stcg_rows} STCG, {report.cg_ltcg_rows} LTCG."
                ]
                if report.cg_stcg_rows or report.cg_ltcg_rows
                else []
            ),
            f"  FTC status: {status}.",
        ]
    )


def _render_fidelity_cma_income_warnings(
    report: FidelityCmaIncomeRunReport, *, debug: bool, output_dir: Path
) -> list[str]:
    """The stderr lines a Fidelity CMA income run prints before its report."""
    lines = []
    if not report.fx_policy_appropriate_for_ay:
        lines.append(_CMA_CROSS_AY_FX_WARNING)
    lines.extend(f"warning: {warning}" for warning in report.warnings)
    lines.extend(f"warning: {warning}" for warning in report.manual_fill_warnings)
    if debug:
        lines.append(_income_debug_outputs_line(output_dir))
    return lines


def _run_fidelity_cma_income(args: argparse.Namespace) -> int:
    return _run_report_command(
        "fidelity-cma-income",
        lambda: run_fidelity_cma_income(
            input_dir=args.input,
            tax_form_dir=args.tax_forms,
            config_path=args.config,
            output_dir=args.output_dir,
            financial_year=args.financial_year,
            itr_form=args.itr_form,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
            debug=getattr(args, "debug", False),
        ),
        lambda report: (
            _render_fidelity_cma_income_warnings(
                report, debug=getattr(args, "debug", False), output_dir=args.output_dir
            ),
            _render_fidelity_cma_income_report(report),
        ),
        lambda report: _EXIT_OK if report.filing_complete else _EXIT_MANUAL_FILL_REQUIRED,
    )


def _render_fidelity_cma_cg_report(report: FidelityCmaCgRunReport) -> str:
    fy = report.financial_year
    return "\n".join(
        [
            (
                "Wrote Fidelity CMA Schedule CG review CSVs for FY "
                f"{fy.start_date.year}-{fy.end_year_2digit:02d} to {report.output_dir}."
            ),
            (
                f"  Statements: {report.statement_count}; in-FY SPAXX sales: "
                f"{report.sale_count}; FIFO slices: "
                f"{report.closed_lot_count}; STCG rows: {report.stcg_rows}; "
                f"LTCG rows: {report.ltcg_rows}; out-of-FY slices: "
                f"{report.out_of_fy_disposals}."
            ),
            f"  Total STCG INR (rounded): {report.total_stcg_inr_rounded}.",
        ]
    )


def _render_fidelity_cma_cg_warnings(report: FidelityCmaCgRunReport, *, itr_form: str) -> list[str]:
    """The stderr lines a Fidelity CMA CG run prints before its report."""
    lines = []
    if not report.fx_policy_appropriate_for_ay:
        lines.append(_CMA_CROSS_AY_FX_WARNING)
    if report.stcg_rows:
        destination = "A/5(e)" if itr_form == "ITR-2" else "A/6(g)"
        lines.append(
            "SPAXX redemption gains are reported at slab rate under Schedule CG "
            f"{destination}; they are NOT Section 111A. Every supported "
            "closure is within 24 months, so the unsettled section 50AA foreign-fund "
            "question cannot change this run's in-FY STCG classification."
        )
    if report.out_of_fy_disposals and not report.stcg_rows:
        lines.append(
            "warning: CMA disposals exist outside the requested FY; "
            "re-check --financial-year if you expected CG rows"
        )
    lines.extend(_table_f_manual_fill_lines(report.table_f_loss_across_bands))
    return lines


def _run_fidelity_cma_cg(args: argparse.Namespace) -> int:
    return _run_report_command(
        "fidelity-cma-cg",
        lambda: run_fidelity_cma_cg(
            input_dir=args.input,
            config_path=args.config,
            output_dir=args.output_dir,
            financial_year=args.financial_year,
            itr_form=args.itr_form,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
        ),
        lambda report: (
            _render_fidelity_cma_cg_warnings(report, itr_form=args.itr_form),
            _render_fidelity_cma_cg_report(report),
        ),
        lambda report: _table_f_exit_code(report.table_f_loss_across_bands),
    )


def _render_fidelity_cma_fa_report(report: FidelityCmaFaRunReport) -> str:
    selected = (
        "manual-fill" if report.selected_peak_bound is None else report.selected_peak_bound.value
    )
    bounds = (
        "both bounds are in the review-csv and xlsx audit columns, not in the upload"
        if report.format_id == "filing-csv"
        else "both bounds remain in audit columns"
    )
    return "\n".join(
        [
            (
                f"Wrote {report.a3_rows} Fidelity CMA Schedule FA A3 row(s) "
                f"for CY{report.calendar_year} to {report.output_path}."
            ),
            f"  Table A2: {report.a2_output_path}.",
            f"  A2 peak selection: {selected}; {bounds}.",
        ]
    )


def _render_fidelity_cma_fa_warnings(report: FidelityCmaFaRunReport) -> list[str]:
    """The stderr lines a Fidelity CMA FA run prints before its report. The
    upload has no audit columns, so it names the blank A2 peak for manual entry.
    Each blank A3 peak's cause and remedy are named over its rows (#1552)."""
    upload = report.format_id == "filing-csv"
    lines = [
        "warning: the CMA account opening date uses the first supplied statement "
        "date because that statement proves no carried account or SPAXX balance; "
        "verify the bundle starts at legal account opening."
    ]
    if report.selected_peak_bound is None:
        bounds = (
            "The upload has no audit columns; --format review-csv or xlsx writes the "
            "proven lower bound and conservative upper bound"
            if upload
            else "Audit columns contain the proven lower bound and conservative upper bound"
        )
        lines.append(
            "warning: A2 peak is blank because same-day credits and debits lack "
            f"a shared timestamp/order. {bounds}, each converted at its own date's "
            "TTBR; under the default native-currency peak selection their rupee "
            "order can differ from the native order. To file one, set "
            "[fidelity_cma].suppress_peak_balance_warning = true and choose "
            'peak_balance_bound = "lower" or "upper"; upper is more conservative '
            "in native currency and may overstate, while lower may understate "
            "the actual intraday peak. An attested bound is refused when the "
            "lower bound converts to more rupees than the upper."
        )
    for column in report.manual_fill_columns:
        if column == A3_PEAK_MANUAL_FILL:
            for reason, rows in report.a3_peak_blank_rows:
                cause, remedy = FA_A3_PEAK_BLANK_TEXT[reason]
                lines.append(
                    f"warning: manual entry required for {column} in row(s) "
                    f"{', '.join(map(str, rows))}: the peak is blank because {cause}. {remedy}"
                )
        elif upload or column != A2_PEAK_MANUAL_FILL:
            lines.append(f"warning: manual entry required for {column}.")
    return lines


def _run_fidelity_cma_fa(args: argparse.Namespace) -> int:
    return _run_report_command(
        "fidelity-cma-fa",
        lambda: run_fidelity_cma_fa(
            input_dir=args.input,
            config_path=args.config,
            output_path=args.output,
            calendar_year=args.calendar_year,
            format_id=args.format_id,
            price_cache_dir=args.price_cache_dir,
        ),
        lambda report: (
            _render_fidelity_cma_fa_warnings(report),
            _render_fidelity_cma_fa_report(report),
        ),
        lambda report: _EXIT_OK if report.filing_complete else _EXIT_MANUAL_FILL_REQUIRED,
    )


def _render_stockplan_income_report(report: StockplanIncomeRunReport) -> str:
    """Render the stockplan-income success summary."""
    return "\n".join(
        [
            _income_outputs_line(report),
            f"  Dividends: {report.dividend_count}; withholdings: {report.withholding_count} "
            f"(in-FY FTC withholding USD {report.withholding_total_usd}).",
            _income_row_counts_line(report),
        ]
    )


def _run_stockplan_income(args: argparse.Namespace) -> int:
    """Dispatch the ``stockplan-income`` subcommand."""
    try:
        report = run_stockplan_income(
            input_dir=args.input,
            config_path=args.config,
            output_dir=args.output_dir,
            financial_year=args.financial_year,
            itr_form=args.itr_form,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
            debug=getattr(args, "debug", False),
            cg_step_checks_sales=getattr(args, "cg_step_checks_sales", False),
        )
        with report.run_outputs.removed_on_failure():
            warnings = _render_income_warnings(
                report, debug=getattr(args, "debug", False), output_dir=args.output_dir
            )
            rendered = _render_stockplan_income_report(report)
    except _DOMAIN_ERRORS as exc:
        return _domain_error("stockplan-income", exc)
    _print_report(report.run_outputs, warnings, rendered)
    return _EXIT_MANUAL_FILL_REQUIRED if report.manual_fill_required else _EXIT_OK


def _render_stockplan_fa_report(report: StockplanFaRunReport) -> str:
    """Render the stockplan-fa success summary (Schedule FA Table A3)."""
    lines = [
        f"Wrote {report.rows_written} Schedule FA Table A3 row(s) (CY{report.calendar_year}, "
        f"{report.held_shares} shares held on Dec 31) to {report.output_path}.",
        f"  Completeness proven: cumulative vests reconcile exactly to the Vested Share "
        f"Holdings aggregate (reconciling boundary {report.reconciled_as_of.isoformat()}).",
        f"  Gross amount credited during the period (dividends): INR {report.dividend_inr}.",
    ]
    for reason in FaA3PeakBlankReason:
        count = report.peak_blank_reasons.count(reason)
        if count:
            cause, remedy = FA_A3_PEAK_BLANK_TEXT[reason]
            lines.append(
                f"  {count} A3 Peak value(s): MANUAL-FILL — the peak is blank because "
                f"{cause}. {remedy}"
            )
    lines.append(
        "  Table A2: MANUAL-FILL — these reports do not prove the Dec-31 account cash "
        "balance or daily NAV."
    )
    return "\n".join(lines)


def _run_stockplan_fa(args: argparse.Namespace) -> int:
    """Dispatch the ``stockplan-fa`` subcommand."""
    try:
        report = run_stockplan_fa(
            input_dir=args.input,
            config_path=args.config,
            output_path=args.output,
            calendar_year=args.calendar_year,
            fetch_prices=not args.no_fetch_prices,
            price_cache_dir=args.price_cache_dir,
        )
        with report.run_outputs.removed_on_failure():
            warnings = [f"warning: {warning}" for warning in report.warnings]
            rendered = _render_stockplan_fa_report(report)
    except _DOMAIN_ERRORS as exc:
        return _domain_error("stockplan-fa", exc)
    _print_report(report.run_outputs, warnings, rendered)
    # Table A2 is always manual until #375 supplies complete account NAV/cash evidence.
    return _EXIT_MANUAL_FILL_REQUIRED


_SOURCE_NOT_REQUIRED: Final[int] = -1


def _run_stockplan_cg_status(args: argparse.Namespace) -> int:
    """Prove whether Schedule CG is required, without generating it."""
    try:
        report = inspect_stockplan_cg_evidence(
            input_dir=args.input,
            config_path=args.config,
            financial_year=args.financial_year,
        )
        # Rendered here so a failure is refused like the inspection's; the leg
        # writes nothing, and stockplan-all removes every output when it fails.
        refusal = (
            f"error: Schedule CG was not generated: {report.report_path.name} contains "
            f"{report.sales_count} sale(s), but stockplan-cg is not implemented. "
            "Do not file until those sales are reported (tracked by #381)."
            if report.sales_count
            else None
        )
    except _DOMAIN_ERRORS as exc:
        return _domain_error(
            "stockplan-all", exc, prefix="could not prove StockPlan Schedule CG status: "
        )
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return _EXIT_DOMAIN_ERROR
    return _SOURCE_NOT_REQUIRED


# The files each stockplan-all leg can write. Used to pre-clear stale prior
# output up front and to remove every leg's output when any leg fails.
_STOCKPLAN_ALL_TARGETS: Final[dict[str, tuple[str, ...]]] = {
    "stockplan-income": INCOME_FILENAMES,
    "stockplan-fa": ("schedule-fa.csv",),
    "Schedule CG": ("schedule-cg.csv", "schedule-cg-ltcg.csv"),
}


def _unproduced_outputs(paths: Iterable[Path]) -> str:
    """Describe each promised output that is not a regular file or cannot be
    checked ("" if every one is there)."""
    problems = []
    for path in paths:
        try:
            if not stat.S_ISREG(path.stat().st_mode):
                problems.append(f"{path}: not a regular file")
        except OSError as exc:
            problems.append(_filesystem_problem("could not check", exc, path))
    return "; ".join(problems)


def _render_source_all_summary(
    results: list[tuple[str, int]],
    *,
    financial_year: FinancialYear,
    orchestrator: str,
    notes: dict[str, str],
) -> str:
    """Status-first summary of a source ``*-all`` orchestrator run (netbenefits/stockplan).

    ``notes`` adds a parenthesised note to a leg's line, such as why its output was removed.
    """
    label = {
        _EXIT_OK: _RUN_ALL_STATUS_LABEL["ok"],
        _EXIT_MANUAL_FILL_REQUIRED: _RUN_ALL_STATUS_LABEL["manual_fill"],
        _SOURCE_NOT_REQUIRED: "not required (zero sales proven)",
    }
    fy = financial_year
    lines = [
        _RUN_ALL_RULE,
        f"  {orchestrator} summary (FY {fy.start_date.year}-{fy.end_year_2digit:02d})",
        _RUN_ALL_RULE,
    ]
    lines.extend(
        f"  {label.get(code, _RUN_ALL_STATUS_LABEL['error'])}  {name}"
        + (f"  ({notes[name]})" if name in notes else "")
        for name, code in results
    )
    lines.append(
        "  'ERROR' = that schedule was NOT produced; 'manual-fill' = user work remains; "
        "'not required' = the source proved zero activity. Do NOT file until every "
        "manual-fill or ERROR line is resolved."
    )
    return "\n".join(lines)


def _run_source_all(
    *,
    output_dir: Path,
    financial_year: FinancialYear,
    targets: dict[str, tuple[str, ...]],
    inputs: Callable[[], tuple[tuple[str, Path], ...]],
    steps: tuple[tuple[str, Callable[[argparse.Namespace], int], argparse.Namespace], ...],
    promised: Callable[[Form67Readiness], dict[str, tuple[str, ...]]],
    form67_readiness: Callable[[], Form67Readiness],
    orchestrator: str,
    summary_results: tuple[tuple[str, int], ...] = (),
) -> int:
    """Run a source's subcommands with fail-closed output handling.

    Refuses before any leg runs if a target is an input. Otherwise attempts to
    remove every stale target, and refuses before any leg runs if any removal
    failed, naming each target it could not remove. Then runs
    :func:`_run_source_legs` in one cleanup scope over every ``targets`` file.
    A console write or flush that fails there, such as with ``BrokenPipeError``
    once a pipe's reader has exited, stops the run at once: every target's
    removal is attempted, and the run exits 4 (#925).
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        problem = _filesystem_problem("could not create --output-dir", exc, output_dir)
        print(f"error: {problem}", file=sys.stderr)
        return _EXIT_DOMAIN_ERROR

    target_paths = [output_dir / name for names in targets.values() for name in names]
    try:
        remove_previous_outputs(target_paths, inputs())
    except (OutputCollisionError, OutputRemovalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_DOMAIN_ERROR
    try:
        with remove_outputs_on_failure(target_paths):
            return _run_source_legs(
                output_dir=output_dir,
                financial_year=financial_year,
                target_paths=target_paths,
                steps=steps,
                promised=promised,
                form67_readiness=form67_readiness,
                orchestrator=orchestrator,
                summary_results=summary_results,
            )
    except _DOMAIN_ERRORS as exc:
        return _all_command_refusal(orchestrator, exc)


def _run_source_legs(
    *,
    output_dir: Path,
    financial_year: FinancialYear,
    target_paths: list[Path],
    steps: tuple[tuple[str, Callable[[argparse.Namespace], int], argparse.Namespace], ...],
    promised: Callable[[Form67Readiness], dict[str, tuple[str, ...]]],
    form67_readiness: Callable[[], Form67Readiness],
    orchestrator: str,
    summary_results: tuple[tuple[str, int], ...],
) -> int:
    """Run every step of :func:`_run_source_all`, even after one fails, so the
    summary shows every error. A step fails when it exits with anything but
    0, 3 (manual fill) or :data:`_SOURCE_NOT_REQUIRED`, or exits 0 or 3
    without every file ``promised`` names for it. If any step failed, every
    ``target_paths`` file is removed, so the run leaves no output to file; a
    file that cannot be removed is named in an error. Returns the worst outcome.

    Before the first step, ``form67_readiness`` computes the income leg's
    Form 67 readiness without writing (#601): a REFUSED row, or any domain
    error that leaves readiness unknown, stops the run before any leg writes;
    otherwise the readiness picks the promised files.

    An ``OSError`` that escapes a step is a failed console write, not a crash:
    each handler turns its file errors into exit 4 inside its own ``try``. It
    propagates, like a failed write here, so the run stops (#925). So does an
    :class:`OutputRemovalError`, which escapes a step only when the handler's
    report fails to print and a removal fails too (#1227).
    """
    try:
        promised_files = promised(form67_readiness())
    except _DOMAIN_ERRORS as exc:
        return _domain_error(orchestrator, exc, prefix=f"{orchestrator}: ")
    except Exception as exc:
        if _traceback_requested():
            raise
        print(
            f"error: {orchestrator}: the Form 67 readiness check crashed unexpectedly "
            f"({crash_site(exc)})",
            file=sys.stderr,
        )
        return _EXIT_GENERIC

    results: list[tuple[str, int]] = []
    for name, handler, sub_args in steps:
        print(f"\n{_RUN_ALL_RULE}\n  {orchestrator}: {name}\n{_RUN_ALL_RULE}", flush=True)
        try:
            code = handler(sub_args)
        except (OSError, OutputRemovalError):
            raise
        except Exception as exc:
            if _traceback_requested():
                raise
            print(
                f"error: {orchestrator}: {name} crashed unexpectedly ({crash_site(exc)})",
                file=sys.stderr,
            )
            code = _EXIT_GENERIC
        if code in (_EXIT_OK, _EXIT_MANUAL_FILL_REQUIRED) and (
            problems := _unproduced_outputs(
                output_dir / filename for filename in promised_files[name]
            )
        ):
            print(
                f"error: {orchestrator}: {name} exited {code} without its promised output: "
                f"{problems}",
                file=sys.stderr,
            )
            code = _EXIT_DOMAIN_ERROR
        results.append((name, code))

    sys.stdout.flush()
    notes: dict[str, str] = {}
    if any(
        code not in (_EXIT_OK, _EXIT_MANUAL_FILL_REQUIRED, _SOURCE_NOT_REQUIRED)
        for _, code in results
    ):
        note = "output removed because the run failed"
        try:
            remove_failed_run_outputs(target_paths)
        except OutputRemovalError as exc:
            print(f"error: {exc}", file=sys.stderr)
            note = "the run failed: do NOT file this output"
        notes = {
            name: note
            for name, code in results
            if code in (_EXIT_OK, _EXIT_MANUAL_FILL_REQUIRED) and promised_files[name]
        }

    results.extend(summary_results)
    summary = _render_source_all_summary(
        results, financial_year=financial_year, orchestrator=orchestrator, notes=notes
    )
    print(summary, file=sys.stderr)
    return _aggregate_run_all_exit_code(
        [_EXIT_OK if code == _SOURCE_NOT_REQUIRED else code for _, code in results]
    )


def _run_stockplan_all(args: argparse.Namespace) -> int:
    """Dispatch ``stockplan-all``: run income + FA and report A2/CG status.

    Reuses the two output handlers and independently probes CG evidence, all
    through :func:`_run_source_all`: every leg runs and reports, but if any
    fails (sales or unproven CG evidence included), the run removes every
    output. A failed console write instead stops the run at once and attempts
    to remove every output (exit 4). The A2 summary line is manual fill, not a failure.
    """
    output_dir: Path = args.output_dir
    fy = args.financial_year
    # Schedule FA is calendar-year based (Dec-31 position); income is FY based.
    # CY = FY start year (FY 2025-26 -> CY 2025). Both read the same --input dir.
    income_args = argparse.Namespace(
        input=args.input,
        config=args.config,
        output_dir=output_dir,
        financial_year=fy,
        itr_form=args.itr_form,
        suppress_cross_ay_warning=args.suppress_cross_ay_warning,
        debug=args.debug,
        # The Schedule CG step below proves zero sales or fails the run (#882).
        cg_step_checks_sales=True,
    )
    fa_args = argparse.Namespace(
        input=args.input,
        config=args.config,
        output=output_dir / "schedule-fa.csv",
        calendar_year=fy.start_year,
        no_fetch_prices=args.no_fetch_prices,
        price_cache_dir=args.price_cache_dir,
    )
    cg_args = argparse.Namespace(input=args.input, config=args.config, financial_year=fy)
    steps: tuple[tuple[str, Callable[[argparse.Namespace], int], argparse.Namespace], ...] = (
        ("stockplan-income", _run_stockplan_income, income_args),
        ("stockplan-fa", _run_stockplan_fa, fa_args),
        ("Schedule CG", _run_stockplan_cg_status, cg_args),
    )
    return _run_source_all(
        output_dir=output_dir,
        financial_year=fy,
        targets=_STOCKPLAN_ALL_TARGETS,
        inputs=functools.partial(stockplan_inputs, args.input, args.config),
        steps=steps,
        promised=lambda readiness: {
            "stockplan-income": income_output_names(form67_readiness=readiness, debug=args.debug),
            "stockplan-fa": ("schedule-fa.csv",),
            "Schedule CG": (),
        },
        form67_readiness=functools.partial(
            stockplan_form67_readiness,
            input_dir=args.input,
            config_path=args.config,
            financial_year=fy,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
        ),
        orchestrator="stockplan-all",
        summary_results=(
            (
                "Schedule FA Table A2 (complete daily cash/NAV evidence unavailable)",
                _EXIT_MANUAL_FILL_REQUIRED,
            ),
        ),
    )


_STOCKPLAN_CONTRACT_POINTER: Final[str] = (
    "See docs/stockplan-contract.md for the StockPlan exports ibkr-fa accepts, "
    "what each refusal means, and how to report an export it has not seen."
)


def _stockplan_exit(code: int) -> int:
    """Return ``code``, first pointing a refused StockPlan run at its contract
    while stderr still works. A failed print settles the console and keeps the
    exit code (#925).
    """
    if code == _EXIT_DOMAIN_ERROR:
        try:
            print(_STOCKPLAN_CONTRACT_POINTER, file=sys.stderr)
        except OSError:
            _settle_broken_console()
    return code


def _render_netbenefits_cg_report(report: NetBenefitsCgRunReport) -> str:
    """Render the netbenefits-cg success summary (Schedule CG STCG + LTCG)."""
    fy = report.financial_year
    lines = [
        f"Wrote Schedule CG (STCG + LTCG) review CSVs for FY "
        f"{fy.start_date.year}-{fy.end_year_2digit:02d} to {report.output_dir}.",
        f"  STCG lines: {report.stcg_rows} (total realized gain INR "
        f"{report.total_stcg_inr_rounded}); LTCG lines: {report.ltcg_rows} "
        f"(total realized gain INR {report.total_ltcg_inr_rounded}).",
        "  Note: the statements prove this FY had no sale, so these CSVs file no "
        "capital gains. Run `netbenefits-fa` separately for the year-end Schedule "
        "FA holdings disclosure.",
    ]
    return "\n".join(lines)


def _render_netbenefits_cg_warnings(report: NetBenefitsCgRunReport) -> list[str]:
    """The stderr lines a netbenefits-cg run prints before its report."""
    lines = []
    if not report.fx_policy_appropriate_for_ay:
        lines.append(_CROSS_AY_FX_WARNING)
    if report.out_of_fy_disposals and not report.stcg_rows and not report.ltcg_rows:
        fy = report.financial_year
        lines.append(
            f"warning: no in-FY Schedule CG rows were produced, but "
            f"{report.out_of_fy_disposals} disposal(s) fell OUTSIDE FY "
            f"{fy.start_date.year}-{fy.end_year_2digit:02d}. If you expected capital gains "
            f"this year, re-run with the correct --financial-year."
        )
    lines.extend(_table_f_manual_fill_lines(report.table_f_loss_across_bands))
    return lines


def _run_netbenefits_cg(args: argparse.Namespace) -> int:
    """Dispatch the ``netbenefits-cg`` subcommand."""
    return _run_report_command(
        "netbenefits-cg",
        lambda: run_netbenefits_cg(
            input_dir=args.input,
            config_path=args.config,
            output_dir=args.output_dir,
            financial_year=args.financial_year,
            itr_form=args.itr_form,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
        ),
        lambda report: (
            _render_netbenefits_cg_warnings(report),
            _render_netbenefits_cg_report(report),
        ),
        lambda report: _table_f_exit_code(report.table_f_loss_across_bands),
    )


# netbenefits-all's Table A3 file, and the Table A2, Quicko workbook and legacy
# Table A2 names the FA pipeline puts beside it.
_NETBENEFITS_FA_A3: Final[str] = "schedule-fa-a3.csv"
_NETBENEFITS_FA_A2, _NETBENEFITS_FA_QUICKO, _NETBENEFITS_FA_LEGACY_A2 = (
    path.name for path in netbenefits_fa_sibling_paths(Path(_NETBENEFITS_FA_A3))
)

# The files each netbenefits leg can write. Used to (a) pre-clear stale prior
# output up front and (b) remove every leg's output when any leg fails.
_NETBENEFITS_ALL_TARGETS: Final[dict[str, tuple[str, ...]]] = {
    # schedule-fa.csv is the pre-A2 legacy name: clear it on every run so an
    # old ambiguous file cannot sit beside the explicit A2/A3 outputs.
    "netbenefits-fa": (
        _NETBENEFITS_FA_A3,
        _NETBENEFITS_FA_A2,
        _NETBENEFITS_FA_QUICKO,
        "schedule-fa.csv",
        _NETBENEFITS_FA_LEGACY_A2,
    ),
    "netbenefits-income": INCOME_FILENAMES,
    "netbenefits-cg": ("schedule-cg.csv", "schedule-cg-ltcg.csv"),
}


def _run_netbenefits_all(args: argparse.Namespace) -> int:
    """Dispatch ``netbenefits-all``: run FA + income + CG against one bundle.

    Reuses the three per-command handlers (so their output/warnings/exit codes
    are identical to running each alone) through :func:`_run_source_all`:
    every leg runs and reports, but if any fails, the run removes every output.
    A failed console write instead stops the run at once and attempts to remove
    every output (exit 4).
    The FA leg promises Table A3 and A2, plus the Quicko workbook when requested.
    """
    output_dir: Path = args.output_dir
    fy = args.financial_year
    # Schedule FA is calendar-year based (Dec-31 position); the other schedules
    # are FY based. CY = FY start year (FY 2025-26 -> CY 2025). All three read the
    # same bundle dir, each picking the files it needs.
    fa_args = argparse.Namespace(
        input=args.input,
        config=args.config,
        output=output_dir / _NETBENEFITS_FA_A3,
        calendar_year=fy.start_year,
        no_fetch_prices=args.no_fetch_prices,
        price_cache_dir=args.price_cache_dir,
        format_id=command_formats("netbenefits-all").default,
        itr_form=args.itr_form,
        quicko_workbook=args.quicko_workbook,
    )
    income_args = argparse.Namespace(
        input=args.input,
        config=args.config,
        output_dir=output_dir,
        financial_year=fy,
        itr_form=args.itr_form,
        suppress_cross_ay_warning=args.suppress_cross_ay_warning,
        reconcile_1042s_usd=args.reconcile_1042s_usd,
        include_capital_gains=True,
        debug=args.debug,
    )
    cg_args = argparse.Namespace(
        input=args.input,
        config=args.config,
        output_dir=output_dir,
        financial_year=fy,
        itr_form=args.itr_form,
        suppress_cross_ay_warning=args.suppress_cross_ay_warning,
    )
    steps: tuple[tuple[str, Callable[[argparse.Namespace], int], argparse.Namespace], ...] = (
        ("netbenefits-fa", _run_netbenefits_fa, fa_args),
        ("netbenefits-income", _run_netbenefits_income, income_args),
        ("netbenefits-cg", _run_netbenefits_cg, cg_args),
    )
    return _run_source_all(
        output_dir=output_dir,
        financial_year=fy,
        targets=_NETBENEFITS_ALL_TARGETS,
        inputs=functools.partial(netbenefits_inputs, args.input, args.config),
        steps=steps,
        promised=lambda readiness: {
            "netbenefits-fa": (
                _NETBENEFITS_FA_A3,
                _NETBENEFITS_FA_A2,
                *((_NETBENEFITS_FA_QUICKO,) if args.quicko_workbook else ()),
            ),
            "netbenefits-income": income_output_names(form67_readiness=readiness, debug=args.debug),
            "netbenefits-cg": _NETBENEFITS_ALL_TARGETS["netbenefits-cg"],
        },
        form67_readiness=functools.partial(
            netbenefits_form67_readiness,
            input_dir=args.input,
            config_path=args.config,
            financial_year=fy,
            include_capital_gains=income_args.include_capital_gains,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
        ),
        orchestrator="netbenefits-all",
    )


def _render_schedule_os_report(report: ScheduleOsRunReport) -> str:
    """Render the verbose Schedule OS success summary for stdout.

    Surfaces the one 1a(i) portal total plus source-country counts.
    """
    foreign_source_count = (
        report.ordinary_dividend_rows_written - report.indian_source_dividend_rows_written
    )
    lines = [
        f"Wrote Schedule OS review to {report.output_path} with "
        f"{report.ordinary_dividend_rows_written} qualifying ordinary dividend payment(s) "
        f"({foreign_source_count} foreign-source, "
        f"{report.indian_source_dividend_rows_written} Indian-source)",
        f"  Input format: {report.input_format.value}",
        f"  Account: {display_account_id(report.account_id)}",
        f"  Financial year: {report.financial_year_start.isoformat()} "
        f"to {report.financial_year_end.isoformat()}",
        f"  TOTAL 1a(i) ordinary dividends (rounded ₹): "
        f"{report.ordinary_dividend_total_inr_rounded}",
    ]
    if report.interest_lines_count > 0:
        interest_label = portal_labels_for_ay(
            report.portal_labels_assessment_year_start
        ).PORTAL_FIELD_LABEL_OS_OTHER_INTEREST
        lines.append(
            f"  {interest_label}: ₹{report.interest_total_inr_rounded} "
            f"(from {report.interest_lines_count} in-FY InterestCredit row(s))"
        )
    return "\n".join(lines)


def _render_schedule_os_report_quiet(report: ScheduleOsRunReport) -> str:
    """Render the --quiet stdout: rounded-INR TOTALs on one line.

    Format: ``"<1a(i) ordinary dividends> <1b(ix) other interest>"``.
    This intentionally breaks the old three-field format because its
    first two fields represented portal destinations that do not exist
    in AY 2026-27.
    """
    return f"{report.ordinary_dividend_total_inr_rounded} {report.interest_total_inr_rounded}"


def _render_schedule_os_safety_warnings(  # noqa: PLR0912
    report: ScheduleOsRunReport,
    *,
    include_global_advisories: bool = True,
) -> str:
    """Render the stderr safety-warning block for Schedule OS.

    Always emitted regardless of --quiet (per plan §5 rev-5 quality P2:
    silently suppressing tax-safety diagnostics with --quiet would mask
    under-reporting risk).

    ``include_global_advisories=False`` leaves out the advisories ``run-all``
    prints once (see :func:`_run_all_global_advisories`).
    """
    lines: list[str] = []
    if include_global_advisories and not report.account_id_cross_checked:
        lines.append(_MASKED_ACCOUNT_SKIP_WARNING)
    for warning_line in _identity_fallback_warning_lines(report.identity_supplied_from_fallback):
        if lines:
            lines.append("")
        lines.append(warning_line)
    if not report.fx_policy_appropriate_for_ay:
        if lines:
            lines.append("")
        lines.append(
            "⚠️  This run targets AY 2027-28 or later, which is governed "
            "by the Income-tax Act, 2025 + Income-tax Rules, 2026."
        )
        lines.append(
            "   Rule 206(2)(b)(i) Sl. No. 5 preserves the Rule 115(1), Explanation (2)(e) "
            "dividend preceding-month-end semantic verbatim, but the "
            "carry-back fallback across non-publication days remains "
            "CBDT-FAQ-based pending primary-source verification."
        )
        lines.append(
            "   See https://github.com/jay-tau/ibkr-fa/issues/126 for "
            "verification status. Pass --suppress-cross-ay-warning to "
            "silence this once you've independently verified."
        )
    for warning_line in _portal_labels_fallback_warning_lines(
        portal_labels_used_forward_compat_fallback=(
            report.portal_labels_used_forward_compat_fallback
        ),
        portal_labels_assessment_year_start=report.portal_labels_assessment_year_start,
    ):
        if lines:
            lines.append("")
        lines.append(warning_line)
    if report.ordinary_dividend_rows_written == 0:
        if lines:
            lines.append("")
        fy_label = f"{report.financial_year_start.year}-{report.financial_year_end.year % 100:02d}"
        lines.append(
            f"ℹ️  No qualifying ordinary dividends found in FY {fy_label} for "  # noqa: RUF001
            f"Schedule OS 1a(i); wrote a zero-total review file."
        )
    # ADR/GDR mode changes foreign-source attribution for FSI/TR/Form 67,
    # never the Schedule OS 1a(i) ordinary-dividend total.
    if lines:
        lines.append("")
    if report.adr_routing_mode == ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER:
        lines.append(
            f"ℹ️  ADR / GDR source attribution: {report.adr_reroutes_count} "  # noqa: RUF001
            f"dividend row(s) treated as Indian-source per user.toml "
            f"[os].schedule_os_indian_adr_routing = 'underlying-issuer' "
            f"(issue #183). These rows are excluded from foreign-source "
            f"FSI/TR/Form 67 aggregation. This setting does not change the "
            f"Schedule OS 1a(i) total."
        )
    else:
        lines.append(
            "ℹ️  ADR / GDR source attribution: Indian-company ADRs / GDRs "  # noqa: RUF001
            "(INFY, IBN, HDB, WIT, RDY, ...) carry US- / Jersey- / Cayman-"
            "prefixed ISINs and remain foreign-source for FSI/TR/Form 67 "
            "under the depositary convention. If your CA prefers the "
            "underlying-issuer view, set "
            "[os].schedule_os_indian_adr_routing = 'underlying-issuer'. "
            "Either mode leaves the Schedule OS 1a(i) total unchanged."
        )
    if report.interest_credits_out_of_fy_count > 0:
        # T3 codex-emulation P2 finding on PR #188 round 2:
        # interest_credits_out_of_fy_count was documented as
        # CLI-surfaced but no renderer actually emitted it.
        # Without this advisory, a user whose multi-period IBKR
        # Activity Statement (e.g., a calendar-year export filed
        # for an FY-bounded run) carries Interest rows outside the
        # requested FY would silently drop those rows from the
        # 1b(ix) aggregate. The advisory points the user at the
        # mismatch so they can verify the FY scope or re-export.
        if lines:
            lines.append("")
        fy_label = f"{report.financial_year_start.year}-{report.financial_year_end.year % 100:02d}"
        lines.append(
            f"ℹ️  Silently filtered {report.interest_credits_out_of_fy_count} "  # noqa: RUF001
            f"InterestCredit row(s) outside FY {fy_label} from the 1b(ix) "
            f"aggregate. This is normal if your IBKR Activity Statement "
            f"is a calendar-year export spanning more than one FY; verify "
            f"by re-exporting an FY-bounded statement or by inspecting "
            f"the Interest section dates against the requested "
            f"--financial-year."
        )
    # #63: BMA disclosure advisory — foreign-source income (the
    # foreign-source dividend detail this subcommand emits) is squarely in BMA
    # scope. Same advisory text as schedule-fa for consistency;
    # fires unconditionally so a user cannot miss the disclosure
    # obligation regardless of which subcommand they run first.
    if include_global_advisories:
        if lines:
            lines.append("")
        lines.append(BMA_DISCLOSURE_ADVISORY)
        # #64: ITR-form selection guidance, after the BMA advisory.
        lines.append("")
        lines.append(ITR_FORM_SELECTION_ADVISORY)
    return "\n".join(lines)


def _schedule_os_step(
    args: argparse.Namespace, *, include_global_advisories: bool
) -> tuple[int, ScheduleOsRunReport | None]:
    """Run ``schedule-os``: its exit code, and its report unless it failed."""
    try:
        report = run_schedule_os(
            input_path=args.input,
            config_path=args.config,
            output_path=args.output,
            financial_year=args.financial_year,
            format_id=args.format_id,
            allow_masked_account=args.allow_masked_account,
            accept_anonymised_statement=args.accept_anonymised_statement,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
        )
        with report.run_outputs.removed_on_failure():
            # never empty: the ADR / GDR note always prints
            warnings = _render_schedule_os_safety_warnings(
                report, include_global_advisories=include_global_advisories
            )
            rendered = (
                _render_schedule_os_report_quiet(report)
                if args.quiet
                else _render_schedule_os_report(report)
            )
    except _DOMAIN_ERRORS as exc:
        return _domain_error("schedule-os", exc), None

    _print_report(report.run_outputs, (warnings,), rendered)

    return _EXIT_OK, report


def _run_schedule_os(args: argparse.Namespace) -> int:
    """Dispatch the ``schedule-os`` subcommand."""
    return _schedule_os_step(args, include_global_advisories=True)[0]


def _render_schedule_cg_report(report: ScheduleCgRunReport) -> str:
    """Render the verbose Schedule CG STCG success summary for stdout."""
    lines = [
        f"Wrote Schedule CG STCG review CSV with {report.stcg_rows_written} "
        f"in-FY short-term closure(s) to {report.output_path}",
        f"  Input format: {report.input_format.value}",
        f"  Account: {display_account_id(report.account_id)}",
        f"  Financial year: {report.financial_year_start.isoformat()} "
        f"to {report.financial_year_end.isoformat()}",
        f"  TOTAL realized gain (rounded ₹): {report.total_realized_gain_inr_rounded}",
    ]
    if report.excluded_ltcg_deferred_count > 0:
        lines.append(
            f"  Excluded (LTCG): {report.excluded_ltcg_deferred_count} "
            f"— run `ibkr-fa schedule-cg-ltcg` for the Section B review "
            f"CSV; see {EXCLUDED_LTCG_DEFERRED_MARKER} audit block in "
            f"the CSV"
        )
    if report.excluded_indian_source_count > 0:
        lines.append(
            f"  Excluded (Indian-source, domestic CG): "
            f"{report.excluded_indian_source_count} — see "
            f"{EXCLUDED_CG_INDIAN_SOURCE_MARKER} audit block in the CSV"
        )
    if report.excluded_out_of_fy_count > 0:
        lines.append(
            f"  Excluded (out-of-FY close date): "
            f"{report.excluded_out_of_fy_count} — see "
            f"{EXCLUDED_OUT_OF_FY_MARKER} audit block in the CSV"
        )
    return "\n".join(lines)


def _render_schedule_cg_report_quiet(report: ScheduleCgRunReport) -> str:
    """Render the --quiet stdout: just the rounded-INR TOTAL on one line.

    May be negative for a net STCG loss.
    """
    return str(report.total_realized_gain_inr_rounded)


def _render_schedule_cg_safety_warnings(  # noqa: PLR0912
    report: ScheduleCgRunReport, *, include_global_advisories: bool = True
) -> str:
    """Render the stderr safety-warning block for Schedule CG STCG.

    Always emitted regardless of --quiet — suppressing tax-safety
    diagnostics would mask under-reporting / mis-reporting risk.

    ``include_global_advisories=False`` leaves out the advisories ``run-all``
    prints once (see :func:`_run_all_global_advisories`).
    """
    lines: list[str] = []
    if include_global_advisories and not report.account_id_cross_checked:
        lines.append(_MASKED_ACCOUNT_SKIP_WARNING)
    for warning_line in _identity_fallback_warning_lines(report.identity_supplied_from_fallback):
        if lines:
            lines.append("")
        lines.append(warning_line)
    if not report.fx_policy_appropriate_for_ay:
        if lines:
            lines.append("")
        lines.append(
            "⚠️  This run targets AY 2027-28 or later, which is governed "
            "by the Income-tax Act, 2025 + Income-tax Rules, 2026."
        )
        lines.append(
            "   The Rule 115 capital-gains preceding-month-end "
            "semantic is preserved verbatim in the Rules 2026 Table, "
            "but the carry-back fallback across non-publication days "
            "remains CBDT-FAQ-based pending primary-source verification."
        )
        lines.append(
            "   See https://github.com/jay-tau/ibkr-fa/issues/126 for "
            "verification status. Pass --suppress-cross-ay-warning to "
            "silence this once you've independently verified."
        )
    for warning_line in _portal_labels_fallback_warning_lines(
        portal_labels_used_forward_compat_fallback=(
            report.portal_labels_used_forward_compat_fallback
        ),
        portal_labels_assessment_year_start=report.portal_labels_assessment_year_start,
    ):
        if lines:
            lines.append("")
        lines.append(warning_line)
    if report.stcg_rows_written > 0:
        if lines:
            lines.append("")
        lines.append(STCG_SLAB_VS_111A_ADVISORY)
    else:
        if lines:
            lines.append("")
        fy_label = f"{report.financial_year_start.year}-{report.financial_year_end.year % 100:02d}"
        lines.append(
            f"ℹ️  No in-FY foreign-source short-term capital gains found "  # noqa: RUF001
            f"in FY {fy_label}; wrote zero-row review CSV with TOTAL ₹0."
        )
    if report.excluded_ltcg_deferred_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.excluded_ltcg_deferred_count} long-term "  # noqa: RUF001
            f"closure(s) (holding > 24 months) — these belong on Schedule "
            f"CG / Section B (LTCG). Run `ibkr-fa schedule-cg-ltcg` against "
            f"the same Activity Statement to produce the LTCG review CSV. "
            f"See the {EXCLUDED_LTCG_DEFERRED_MARKER} audit block in the CSV."
        )
    if report.excluded_indian_source_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.excluded_indian_source_count} "  # noqa: RUF001
            f"Indian-source closure(s) (ISIN prefix 'IN') from the foreign "
            f"STCG aggregate — these are domestic CG and belong on a "
            f"different Schedule CG section (out of v1 scope). See the "
            f"{EXCLUDED_CG_INDIAN_SOURCE_MARKER} audit block."
        )
    if report.excluded_out_of_fy_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.excluded_out_of_fy_count} closure(s) "  # noqa: RUF001
            f"whose close date is outside the requested FY window — see "
            f"the {EXCLUDED_OUT_OF_FY_MARKER} audit block. A multi-period "
            f"Activity Statement export may legitimately contain such rows."
        )
    if report.total_realized_gain_inr_rounded < 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Net STCG TOTAL is negative "  # noqa: RUF001
            f"({report.total_realized_gain_inr_rounded}). This is a "
            f"short-term capital LOSS — per Section 74 it can be "
            f"set off against other short-term gains in the same AY "
            f"and the remainder carried forward for 8 AYs. The review "
            f"CSV escapes the leading '-' with a single quote so "
            f"spreadsheet formula-injection guards do not trigger; "
            f"Excel strips the apostrophe on display."
        )
    for manual_line in _table_f_manual_fill_lines(report.table_f_loss_across_bands):
        if lines:
            lines.append("")
        lines.append(manual_line)
    # #63: BMA disclosure advisory — any foreign-equity STCG
    # transaction implies an underlying foreign asset, which is in
    # BMA disclosure scope.
    if include_global_advisories:
        if lines:
            lines.append("")
        lines.append(BMA_DISCLOSURE_ADVISORY)
        # #64: ITR-form selection guidance, after the BMA advisory.
        lines.append("")
        lines.append(ITR_FORM_SELECTION_ADVISORY)
    return "\n".join(lines)


def _schedule_cg_step(
    args: argparse.Namespace, *, include_global_advisories: bool
) -> tuple[int, ScheduleCgRunReport | None]:
    """Run ``schedule-cg``: its exit code, and its report unless it failed."""
    try:
        report = run_schedule_cg(
            input_path=args.input,
            config_path=args.config,
            output_path=args.output,
            financial_year=args.financial_year,
            format_id=args.format_id,
            itr_form=args.itr_form,
            allow_masked_account=args.allow_masked_account,
            accept_anonymised_statement=args.accept_anonymised_statement,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
        )
        with report.run_outputs.removed_on_failure():
            # never empty: the STCG advisory or the zero-gains note always prints
            warnings = _render_schedule_cg_safety_warnings(
                report, include_global_advisories=include_global_advisories
            )
            rendered = (
                _render_schedule_cg_report_quiet(report)
                if args.quiet
                else _render_schedule_cg_report(report)
            )
    except _DOMAIN_ERRORS as exc:
        return _domain_error("schedule-cg", exc), None

    _print_report(report.run_outputs, (warnings,), rendered)

    return _table_f_exit_code(report.table_f_loss_across_bands), report


def _run_schedule_cg(args: argparse.Namespace) -> int:
    """Dispatch the ``schedule-cg`` subcommand."""
    return _schedule_cg_step(args, include_global_advisories=True)[0]


def _render_schedule_cg_ltcg_report(report: ScheduleCgLtcgRunReport) -> str:
    """Render the verbose Schedule CG LTCG success summary for stdout."""
    lines = [
        f"Wrote Schedule CG LTCG review CSV with {report.ltcg_rows_written} "
        f"in-FY long-term closure(s) to {report.output_path}",
        f"  Input format: {report.input_format.value}",
        f"  Account: {display_account_id(report.account_id)}",
        f"  Financial year: {report.financial_year_start.isoformat()} "
        f"to {report.financial_year_end.isoformat()}",
        f"  TOTAL realized gain (rounded ₹): {report.total_realized_gain_inr_rounded}",
    ]
    if report.excluded_stcg_count > 0:
        lines.append(
            f"  Excluded (STCG, handled by schedule-cg): "
            f"{report.excluded_stcg_count} — see "
            f"{EXCLUDED_STCG_MARKER} audit block in the CSV"
        )
    if report.excluded_pre_2024_07_23_count > 0:
        lines.append(
            f"  Excluded (pre-2024-07-23 disposals, old regime needs "
            f"indexation): {report.excluded_pre_2024_07_23_count} — see "
            f"{EXCLUDED_PRE_CUTOVER_MARKER} audit block in the CSV"
        )
    if report.excluded_indian_source_count > 0:
        lines.append(
            f"  Excluded (Indian-source, domestic CG): "
            f"{report.excluded_indian_source_count} — see "
            f"{EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER} audit block in the CSV"
        )
    if report.excluded_out_of_fy_count > 0:
        lines.append(
            f"  Excluded (out-of-FY close date): "
            f"{report.excluded_out_of_fy_count} — see "
            f"{EXCLUDED_LTCG_OUT_OF_FY_MARKER} audit block in the CSV"
        )
    return "\n".join(lines)


def _render_schedule_cg_ltcg_report_quiet(report: ScheduleCgLtcgRunReport) -> str:
    """Render the --quiet stdout for LTCG: rounded-INR TOTAL on one line.

    May be negative for a net long-term loss.
    """
    return str(report.total_realized_gain_inr_rounded)


def _render_schedule_cg_ltcg_safety_warnings(  # noqa: PLR0912, PLR0915
    report: ScheduleCgLtcgRunReport, *, include_global_advisories: bool = True
) -> str:
    """Render the stderr safety-warning block for Schedule CG LTCG.

    ``include_global_advisories=False`` leaves out the advisories ``run-all``
    prints once (see :func:`_run_all_global_advisories`).
    """
    lines: list[str] = []
    if include_global_advisories and not report.account_id_cross_checked:
        lines.append(_MASKED_ACCOUNT_SKIP_WARNING)
    for warning_line in _identity_fallback_warning_lines(report.identity_supplied_from_fallback):
        if lines:
            lines.append("")
        lines.append(warning_line)
    if not report.fx_policy_appropriate_for_ay:
        if lines:
            lines.append("")
        lines.append(
            "⚠️  This run targets AY 2027-28 or later, which is governed "
            "by the Income-tax Act, 2025 + Income-tax Rules, 2026."
        )
        lines.append(
            "   The Rule 115 capital-gains preceding-month-end "
            "semantic is preserved verbatim in the Rules 2026 Table, "
            "but the carry-back fallback across non-publication days "
            "remains CBDT-FAQ-based pending primary-source verification."
        )
        lines.append(
            "   See https://github.com/jay-tau/ibkr-fa/issues/126 for "
            "verification status. Pass --suppress-cross-ay-warning to "
            "silence this once you've independently verified."
        )
    for warning_line in _portal_labels_fallback_warning_lines(
        portal_labels_used_forward_compat_fallback=(
            report.portal_labels_used_forward_compat_fallback
        ),
        portal_labels_assessment_year_start=report.portal_labels_assessment_year_start,
    ):
        if lines:
            lines.append("")
        lines.append(warning_line)
    if report.ltcg_rows_written > 0:
        if lines:
            lines.append("")
        lines.append(LTCG_SECTION_112_VS_112A_ADVISORY)
    else:
        if lines:
            lines.append("")
        fy_label = f"{report.financial_year_start.year}-{report.financial_year_end.year % 100:02d}"
        lines.append(
            f"ℹ️  No in-FY foreign-source long-term capital gains found "  # noqa: RUF001
            f"in FY {fy_label}; wrote zero-row review CSV with TOTAL ₹0."
        )
    if report.excluded_stcg_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.excluded_stcg_count} short-term "  # noqa: RUF001
            f"closure(s) (holding <= 24 months) — these belong on "
            f"Schedule CG / Section A (STCG) via the schedule-cg "
            f"subcommand. See the {EXCLUDED_STCG_MARKER} audit block."
        )
    if report.excluded_pre_2024_07_23_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"⚠️  Excluded {report.excluded_pre_2024_07_23_count} "
            f"long-term closure(s) disposed BEFORE 2024-07-23 — the "
            f"old regime (20% with indexation via CBDT Cost Inflation "
            f"Index) is out of v1 scope. These rows are visible in the "
            f"{EXCLUDED_PRE_CUTOVER_MARKER} audit block but NOT in the "
            f"LTCG TOTAL. You will need to manually add them to your "
            f"Schedule CG filing using the old-regime computation."
        )
    if report.excluded_indian_source_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.excluded_indian_source_count} "  # noqa: RUF001
            f"Indian-source closure(s) (ISIN prefix 'IN') from the foreign "
            f"LTCG aggregate. See the "
            f"{EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER} audit block."
        )
    if report.excluded_out_of_fy_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.excluded_out_of_fy_count} closure(s) "  # noqa: RUF001
            f"whose close date is outside the requested FY window — see "
            f"the {EXCLUDED_LTCG_OUT_OF_FY_MARKER} audit block."
        )
    if report.total_realized_gain_inr_rounded < 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Net LTCG TOTAL is negative "  # noqa: RUF001
            f"({report.total_realized_gain_inr_rounded}). This is a "
            f"long-term capital LOSS — per Section 74 it can be set "
            f"off against other LONG-TERM gains in the same AY (NOT "
            f"against short-term gains) and the remainder carried "
            f"forward for 8 AYs (long-term losses → long-term gains "
            f"only). The review CSV escapes the leading '-' with a "
            f"single quote so spreadsheet formula-injection guards "
            f"do not trigger."
        )
    for manual_line in _table_f_manual_fill_lines(report.table_f_loss_across_bands):
        if lines:
            lines.append("")
        lines.append(manual_line)
    # #63: BMA disclosure advisory — any foreign-equity LTCG
    # transaction implies an underlying foreign asset, which is in
    # BMA disclosure scope.
    if include_global_advisories:
        if lines:
            lines.append("")
        lines.append(BMA_DISCLOSURE_ADVISORY)
        # #64: ITR-form selection guidance, after the BMA advisory.
        lines.append("")
        lines.append(ITR_FORM_SELECTION_ADVISORY)
    return "\n".join(lines)


def _schedule_cg_ltcg_step(
    args: argparse.Namespace, *, include_global_advisories: bool
) -> tuple[int, ScheduleCgLtcgRunReport | None]:
    """Run ``schedule-cg-ltcg``: its exit code, and its report unless it failed."""
    try:
        report = run_schedule_cg_ltcg(
            input_path=args.input,
            config_path=args.config,
            output_path=args.output,
            financial_year=args.financial_year,
            allow_masked_account=args.allow_masked_account,
            accept_anonymised_statement=args.accept_anonymised_statement,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
            format_id=args.format_id,
            itr_form=args.itr_form,
        )
        with report.run_outputs.removed_on_failure():
            # never empty: the LTCG advisory or the zero-gains note always prints
            warnings = _render_schedule_cg_ltcg_safety_warnings(
                report, include_global_advisories=include_global_advisories
            )
            rendered = (
                _render_schedule_cg_ltcg_report_quiet(report)
                if args.quiet
                else _render_schedule_cg_ltcg_report(report)
            )
    except _DOMAIN_ERRORS as exc:
        return _domain_error("schedule-cg-ltcg", exc), None

    _print_report(report.run_outputs, (warnings,), rendered)

    return _table_f_exit_code(report.table_f_loss_across_bands), report


def _run_schedule_cg_ltcg(args: argparse.Namespace) -> int:
    """Dispatch the ``schedule-cg-ltcg`` subcommand."""
    return _schedule_cg_ltcg_step(args, include_global_advisories=True)[0]


def _render_schedule_fsi_report(report: ScheduleFsiRunReport) -> str:
    """Render the verbose Schedule FSI success summary for stdout."""
    lines = [
        f"Wrote Schedule FSI review file with {report.rows_written} "
        f"per-(country, head-of-income) row(s) to {report.output_path}",
        f"  Input format: {report.input_format.value}",
        f"  Account: {display_account_id(report.account_id)}",
        f"  Financial year: {report.financial_year_start.isoformat()} "
        f"to {report.financial_year_end.isoformat()}",
        f"  TOTAL income (rounded ₹): {report.total_income_inr_rounded}",
        f"  TOTAL foreign tax (rounded ₹): {report.total_foreign_tax_inr_rounded}",
    ]
    # Per-source consumption breakdown lets the user reconcile against
    # the sister `schedule-os` / `schedule-cg` / `schedule-cg-ltcg`
    # runs by row counts.
    lines.append(
        f"  Aggregator consumption: "
        f"{report.os_dividend_rows_consumed} dividend(s), "
        f"{report.os_interest_rows_consumed} interest event(s), "
        f"{report.cg_stcg_rows_consumed} STCG row(s), "
        f"{report.cg_ltcg_rows_consumed} LTCG row(s), "
        f"{report.wht_rows_in_fy} in-FY WHT row(s)"
    )
    if report.per_country_rows:
        lines.append("")
        lines.append("  Per-(country, head) breakdown:")
        for per_country in report.per_country_rows:
            lines.append(
                f"    {per_country.country_name_portal} "
                f"(CBDT {per_country.cbdt_country_code}) / "
                f"{per_country.head_of_income.value}: "
                f"income ₹{per_country.income_inr_rounded}, "
                f"foreign tax ₹{per_country.foreign_tax_inr_rounded}, "
                f"{per_country.event_count} event(s)"
            )
    return "\n".join(lines)


def _render_schedule_fsi_report_quiet(report: ScheduleFsiRunReport) -> str:
    """Render the --quiet stdout: CSV header + per-row CSV.

    Format:

        cbdt_country_code,head_of_income,income_inr_rounded,foreign_tax_inr_rounded
        2,Capital Gains,12345,0
        2,Other Sources,6789,1234

    Zero-row runs emit just the header line. The output uses
    :data:`csv.QUOTE_MINIMAL` (csv.writer default) — cells with
    embedded commas / quotes / newlines are quoted; the
    ``head_of_income`` value (``"Capital Gains"`` / ``"Other
    Sources"``) is NOT quoted (no special chars). Numeric cells
    are integer rupees (HALF_UP per CBDT FAQ).
    """
    buf = io.StringIO()
    # Use lineterminator='\n' (default is CRLF '\r\n') so the output
    # is shell-friendly — print() adds the trailing newline; csv.writer's
    # default CRLF would otherwise leave a stray '\r' before each '\n'
    # that shell pipelines / parsers must strip. (T4 r1 #4.)
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        [
            "cbdt_country_code",
            "head_of_income",
            "income_inr_rounded",
            "foreign_tax_inr_rounded",
        ]
    )
    for row in report.per_country_rows:
        writer.writerow(
            [
                row.cbdt_country_code,
                row.head_of_income.value,
                str(row.income_inr_rounded),
                str(row.foreign_tax_inr_rounded),
            ]
        )
    # Strip the trailing newline csv.writer appends; the print() call
    # in the caller adds its own.
    return buf.getvalue().rstrip("\n")


def _render_schedule_fsi_safety_warnings(  # noqa: PLR0912, PLR0915
    report: ScheduleFsiRunReport, *, include_global_advisories: bool = True
) -> str:
    """Render the stderr safety-warning block for Schedule FSI.

    Always emitted regardless of --quiet — suppressing tax-safety
    diagnostics would mask under-reporting / mis-reporting risk.

    ``include_global_advisories=False`` leaves out the advisories ``run-all``
    prints once (see :func:`_run_all_global_advisories`).
    """
    lines: list[str] = []
    if include_global_advisories and report.has_unresolved_indian_tax_payable:
        lines.append(missing_indian_tax_payable_warning(report.override_advice))
    if include_global_advisories and not report.account_id_cross_checked:
        lines.append(_MASKED_ACCOUNT_SKIP_WARNING)
    for warning_line in _identity_fallback_warning_lines(report.identity_supplied_from_fallback):
        if lines:
            lines.append("")
        lines.append(warning_line)
    if not report.fx_policy_appropriate_for_ay or not report.wht_fx_policy_appropriate_for_ay:
        if lines:
            lines.append("")
        lines.append(
            "⚠️  This run targets AY 2027-28 or later, which is governed "
            "by the Income-tax Act, 2025 + Income-tax Rules, 2026."
        )
        lines.append(
            "   Rule 206(2)(b)(i) preserves the Rule 115 preceding-"
            "month-end semantic for income FX, and Rule 76(7)(b) "
            "preserves the Rule 128(5)(ii) preceding-month-end semantic "
            "for foreign-tax-credit FX. Both require independent "
            "primary-source verification of the carry-back fallback "
            "across non-publication days."
        )
        lines.append(
            "   See https://github.com/jay-tau/ibkr-fa/issues/207 for "
            "verification status. Pass --suppress-cross-ay-warning to "
            "silence this once you've independently verified."
        )
    for warning_line in _portal_labels_fallback_warning_lines(
        portal_labels_used_forward_compat_fallback=(
            report.portal_labels_used_forward_compat_fallback
        ),
        portal_labels_assessment_year_start=report.portal_labels_assessment_year_start,
    ):
        if lines:
            lines.append("")
        lines.append(warning_line)
    for warning_line in _cross_fy_reversal_warning_lines(report.cross_fy_reversal_report):
        if lines:
            lines.append("")
        lines.append(warning_line)
    if report.rows_written == 0:
        if lines:
            lines.append("")
        fy_label = f"{report.financial_year_start.year}-{report.financial_year_end.year % 100:02d}"
        lines.append(
            f"ℹ️  No qualifying FSI income found in FY {fy_label}; "  # noqa: RUF001
            f"wrote empty Schedule FSI file (header only). If you have "
            f"foreign dividends / capital gains / broker interest in "
            f"this FY, verify the input statement covers the right "
            f"period and the source sections are present."
        )
    # Aggregator-exclusion summaries — surface upstream warnings about
    # omitted source data so the user can verify they're not missing
    # anything inadvertently.
    if report.excluded_cg_indian_source_count > 0 or report.excluded_ltcg_indian_source_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded Indian-source capital gains from FSI: "  # noqa: RUF001
            f"{report.excluded_cg_indian_source_count} STCG + "
            f"{report.excluded_ltcg_indian_source_count} LTCG. Indian-"
            f"source income is by definition not foreign-source; see "
            f"sister schedule-cg / schedule-cg-ltcg audit blocks "
            f"({EXCLUDED_CG_INDIAN_SOURCE_MARKER} / "
            f"{EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER})."
        )
    if report.excluded_cg_out_of_fy_count > 0 or report.excluded_ltcg_out_of_fy_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.excluded_cg_out_of_fy_count} STCG + "  # noqa: RUF001
            f"{report.excluded_ltcg_out_of_fy_count} LTCG closure(s) "
            f"with close_date outside the reporting FY. See "
            f"{EXCLUDED_OUT_OF_FY_MARKER} / "
            f"{EXCLUDED_LTCG_OUT_OF_FY_MARKER} audit blocks in the "
            f"sister CG / CG-LTCG outputs."
        )
    if report.excluded_ltcg_pre_cutover_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.excluded_ltcg_pre_cutover_count} "  # noqa: RUF001
            f"LTCG closure(s) with disposal date before the 2024-07-23 "
            f"new-regime cutover (old-regime indexation pathway, out "
            f"of v1 scope). See {EXCLUDED_PRE_CUTOVER_MARKER} block in "
            f"the sister schedule-cg-ltcg output."
        )
    if report.interest_credits_out_of_fy_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.interest_credits_out_of_fy_count} "  # noqa: RUF001
            f"broker credit-interest event(s) with date outside the "
            f"reporting FY. See sister `schedule-os` 1b(ix) summary."
        )
    # ADR / GDR routing-mode advisory: FSI's run is impacted by the
    # OS routing mode because OS provides the foreign-dividend stream.
    if lines:
        lines.append("")
    if report.adr_routing_mode == ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER:
        # Indian-ADR dividends were attributed to India and the
        # matching depositary-country WHT was symmetrically skipped
        # at the compute layer (issue #225 fix), so the FSI row set
        # below reflects only non-Indian-ADR foreign income + WHT.
        lines.append(
            "ℹ️  ADR / GDR routing: UNDERLYING_ISSUER mode (issue #183). "  # noqa: RUF001
            "Indian-company ADRs / GDRs mapped in "
            "src/ibkr_fa/reference/indian_adr_mapping.py have been "
            "treated as Indian-source and are therefore "
            "EXCLUDED from FSI (Indian-source income is not foreign-"
            "source); the matching depositary-country WHT is "
            "symmetrically excluded from the FSI foreign-tax-paid "
            "column (issue #225). Verify the rerouting matches your "
            "CA's expectation."
        )
    else:
        lines.append(
            "ℹ️  ADR / GDR routing note: Indian-company ADRs / GDRs "  # noqa: RUF001
            "are treated as foreign-source per the asset-as-held "
            "convention (default 'depositary' mode). Their dividends "
            "appear in the FSI under their depositary's source country "
            "(typically US / Jersey / Cayman), and any depositary-"
            "country WHT is layered onto the matching same-country row. "
            "If your CA prefers the underlying-issuer view, set "
            "[os].schedule_os_indian_adr_routing = 'underlying-issuer' "
            "in user.toml — the FSI compute layer symmetrically "
            "excludes the mapped Indian-ADR WHT to prevent silent "
            "absorption (issue #225 fix in PR #251)."
        )
    # FSI foreign-TIN completeness nudge (mandatory for §90/90A/91 relief).
    if include_global_advisories and report.has_foreign_tax_without_taxpayer_id:
        if lines:
            lines.append("")
        lines.append(_FOREIGN_TIN_MISSING_ADVISORY)
    # BMA + ITR-form advisories (always-on, per #178 + #179).
    if include_global_advisories:
        lines.append("")
        lines.append(BMA_DISCLOSURE_ADVISORY)
        lines.append("")
        lines.append(ITR_FORM_SELECTION_ADVISORY)
    return "\n".join(lines)


def _schedule_fsi_step(
    args: argparse.Namespace, *, include_global_advisories: bool
) -> tuple[int, ScheduleFsiRunReport | None]:
    """Run ``schedule-fsi``: its exit code, and its report unless it failed."""
    try:
        report = run_schedule_fsi(
            input_path=args.input,
            config_path=args.config,
            output_path=args.output,
            financial_year=args.financial_year,
            format_id=args.format_id,
            allow_masked_account=args.allow_masked_account,
            accept_anonymised_statement=args.accept_anonymised_statement,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
        )
        with report.run_outputs.removed_on_failure():
            # never empty: the ADR / GDR note always prints
            warnings = _render_schedule_fsi_safety_warnings(
                report, include_global_advisories=include_global_advisories
            )
            rendered = (
                _render_schedule_fsi_report_quiet(report)
                if args.quiet
                else _render_schedule_fsi_report(report)
            )
    except _DOMAIN_ERRORS as exc:
        return _domain_error("schedule-fsi", exc), None

    _print_report(report.run_outputs, (warnings,), rendered)

    return (_EXIT_MANUAL_FILL_REQUIRED if report.manual_fill_required else _EXIT_OK), report


def _run_schedule_fsi(args: argparse.Namespace) -> int:
    """Dispatch the ``schedule-fsi`` subcommand."""
    return _schedule_fsi_step(args, include_global_advisories=True)[0]


def _format_tr_full_precision_inr(value: Decimal) -> str:
    """Render a TR audit INR cell at TRUE full precision (no quantize).

    The verbose label says "full precision" — the quiet CSV uses
    ``str(Decimal)`` which preserves every digit; the verbose render
    must match so the label and the rendered value agree.
    T4 r4 observation follow-up on PR #218 (precision label drift).
    """
    return str(value)


def _classify_tr_relief_population(
    per_country_rows: tuple[TrPerCountryRow, ...],
) -> str:
    """Classify Schedule TR relief population across the per-country rows.

    Mirrors :func:`_classify_form67_relief_population` (PR #262 / issue #261)
    so the schedule-tr console summary + safety warnings report the relief
    that #36/#37 (PR #277) computes. Returns one of:

    * ``"fully_populated"`` — every country row has BOTH
      ``tax_relief_inr_total`` and ``section_under_which_claimed`` set.
    * ``"partially_populated"`` — some (but not all) country rows are
      populated; the rest were left blank because some contributing FSI
      row has no computed relief (e.g. missing ``[form67]`` config).
    * ``"all_blank"`` — no country row has relief populated.

    A row counts as populated only when BOTH coupled fields are set;
    ``TrPerCountryRow`` does not itself enforce the coupling, so a one-sided
    value is treated conservatively as not-populated. Callers handle the
    empty-rows (no foreign tax paid) case separately.
    """
    populated = sum(
        1
        for row in per_country_rows
        if row.tax_relief_inr_total is not None and row.section_under_which_claimed is not None
    )
    if populated == 0:
        return "all_blank"
    if populated == len(per_country_rows):
        return "fully_populated"
    return "partially_populated"


def _tr_blank_relief_iso_codes(per_country_rows: tuple[TrPerCountryRow, ...]) -> str:
    """Return a sorted, comma-joined list of ISO codes whose relief is blank."""
    return ", ".join(
        sorted(
            row.iso_alpha2
            for row in per_country_rows
            if row.tax_relief_inr_total is None or row.section_under_which_claimed is None
        )
    )


def _schedule_tr_relief_summary_line(report: ScheduleTrRunReport) -> str:
    """The stdout summary line describing Schedule TR relief population."""
    rows = report.per_country_rows
    if not rows:
        return "  Tax relief and section under which claimed: none (no foreign tax paid)"
    state = _classify_tr_relief_population(rows)
    if state == "fully_populated":
        return (
            "  Tax relief and section under which claimed: POPULATED from exact "
            "overrides plus DTAA / §91 reference data — verify before filing"
        )
    if state == "partially_populated":
        populated = sum(
            1
            for row in rows
            if row.tax_relief_inr_total is not None and row.section_under_which_claimed is not None
        )
        return (
            "  Tax relief and section under which claimed: PARTIALLY POPULATED "
            f"({populated} of {len(rows)} countries; blank: "
            f"{_tr_blank_relief_iso_codes(rows)}) — supply exact Form 67 evidence "
            "where applicable and fill the rest manually before filing"
        )
    return (
        "  Tax relief and section under which claimed: LEFT BLANK — not "
        "auto-populated (no verified DTAA path or incomplete §91 evidence); "
        "provide exact Form 67 evidence where applicable and fill manually "
        "before filing"
    )


def _schedule_tr_relief_advisory_line(report: ScheduleTrRunReport) -> str | None:
    """The stderr relief advisory for schedule-tr, or ``None`` when there is no
    foreign tax to relieve. Mirrors the Form 67 relief stderr note (PR #262)."""
    rows = report.per_country_rows
    if not rows:
        return None
    state = _classify_tr_relief_population(rows)
    if state == "fully_populated":
        return (
            "ℹ️  Schedule TR tax relief + section are POPULATED from exact "  # noqa: RUF001
            "overrides plus DTAA / §91 reference data. Verify the §90 / §91 / "
            "total-relief values against your CA's expectations before filing."
        )
    if state == "partially_populated":
        return (
            "⚠️  Schedule TR tax relief is PARTIALLY POPULATED (blank: "
            f"{_tr_blank_relief_iso_codes(rows)}). Supply exact Form 67 evidence "
            "where applicable and fill the remaining countries manually "
            "before filing."
        )
    return (
        "⚠️  Schedule TR tax relief and section are LEFT BLANK — not "
        "auto-populated (no verified DTAA path or incomplete §91 evidence). "
        "Supply exact Form 67 evidence where applicable and fill manually "
        "before filing."
    )


def _render_schedule_tr_report(report: ScheduleTrRunReport) -> str:
    """Render the verbose Schedule TR success summary for stdout."""
    lines = [
        "Schedule TR — Summary of Tax Relief Claimed",
        f"Wrote {report.rows_written} TR rows to {report.output_path}",
        f"  Input format: {report.input_format.value}",
        f"  Account: {display_account_id(report.account_id)}",
        f"  Financial year: {report.financial_year_start.isoformat()} "
        f"to {report.financial_year_end.isoformat()}",
    ]
    if report.per_country_rows:
        lines.append("")
        lines.append("  Per-country breakdown:")
        lines.append(
            "    cbdt_country_code | iso_alpha2 | country_name_portal | "
            "foreign_tax_inr_total_portal_displayed | event_count"
        )
        for row in report.per_country_rows:
            lines.append(
                f"    {row.cbdt_country_code} | {row.iso_alpha2} | "
                f"{row.country_name_portal} | "
                f"INR {row.foreign_tax_inr_total_portal_displayed} | "
                f"{row.event_count}"
            )
    else:
        lines.append("")
        lines.append("  Per-country breakdown: no TR rows (no foreign tax paid).")
    lines.extend(
        [
            "",
            "  Total foreign tax paid (portal-displayed): "
            f"INR {report.total_foreign_tax_inr_portal_displayed}",
            "  Total foreign tax paid (full precision): "
            f"INR {_format_tr_full_precision_inr(report.total_foreign_tax_inr_full_precision)}",
            _schedule_tr_relief_summary_line(report),
            "",
            "  Upstream consumption:",
            f"    FSI rows consumed: {report.fsi_rows_consumed}",
            f"    OS dividends: {report.os_dividend_rows_consumed}",
            f"    OS interest: {report.os_interest_rows_consumed}",
            f"    CG STCG: {report.cg_stcg_rows_consumed}",
            f"    CG LTCG: {report.cg_ltcg_rows_consumed}",
            f"    WHT in FY: {report.wht_rows_in_fy}",
            "",
            "  Exclusions:",
            f"    CG indian-source: {report.excluded_cg_indian_source_count}",
            f"    CG out-of-fy: {report.excluded_cg_out_of_fy_count}",
            f"    LTCG pre-cutover: {report.excluded_ltcg_pre_cutover_count}",
            f"    LTCG indian-source: {report.excluded_ltcg_indian_source_count}",
            f"    LTCG out-of-fy: {report.excluded_ltcg_out_of_fy_count}",
            f"    interest out-of-fy: {report.interest_credits_out_of_fy_count}",
            "",
            "  AY policy:",
            "    portal_labels_assessment_year_start: "
            f"{report.portal_labels_assessment_year_start}",
            f"    fx_policy_appropriate_for_ay: {report.fx_policy_appropriate_for_ay}",
            f"    wht_fx_policy_appropriate_for_ay: {report.wht_fx_policy_appropriate_for_ay}",
        ]
    )
    return "\n".join(lines)


def _render_schedule_tr_report_quiet(report: ScheduleTrRunReport) -> str:
    """Render the --quiet stdout for TR: CSV header + per-country CSV."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        [
            "cbdt_country_code",
            "iso_alpha2",
            "country_name_portal",
            "foreign_tax_inr_total_portal_displayed",
            "foreign_tax_inr_total_full_precision",
            "event_count",
        ]
    )
    for row in report.per_country_rows:
        writer.writerow(
            [
                row.cbdt_country_code,
                row.iso_alpha2,
                row.country_name_portal,
                str(row.foreign_tax_inr_total_portal_displayed),
                ""
                if row.foreign_tax_inr_total_full_precision is None
                else str(row.foreign_tax_inr_total_full_precision),
                row.event_count,
            ]
        )
    return buf.getvalue().rstrip("\n")


def _render_schedule_tr_safety_warnings(  # noqa: PLR0912, PLR0915
    report: ScheduleTrRunReport, *, include_global_advisories: bool = True
) -> str:
    """Render the stderr safety-warning block for Schedule TR.

    ``include_global_advisories=False`` leaves out the advisories ``run-all``
    prints once (see :func:`_run_all_global_advisories`).
    """
    lines: list[str] = []
    if include_global_advisories and report.has_unresolved_indian_tax_payable:
        lines.append(missing_indian_tax_payable_warning(report.override_advice))
    if include_global_advisories and not report.account_id_cross_checked:
        lines.append(_MASKED_ACCOUNT_SKIP_WARNING)
    for warning_line in _identity_fallback_warning_lines(report.identity_supplied_from_fallback):
        if lines:
            lines.append("")
        lines.append(warning_line)
    if not report.fx_policy_appropriate_for_ay or not report.wht_fx_policy_appropriate_for_ay:
        if lines:
            lines.append("")
        lines.append(
            "⚠️  This run targets AY 2027-28 or later, which is governed "
            "by the Income-tax Act, 2025 + Income-tax Rules, 2026."
        )
        lines.append(
            "   Rule 206(2)(b)(i) preserves the Rule 115 preceding-"
            "month-end semantic for income FX, and Rule 76(7)(b) "
            "preserves the Rule 128(5)(ii) preceding-month-end semantic "
            "for foreign-tax-credit FX. Both require independent "
            "primary-source verification of the carry-back fallback "
            "across non-publication days."
        )
        lines.append(
            "   See https://github.com/jay-tau/ibkr-fa/issues/207 for "
            "verification status. Pass --suppress-cross-ay-warning to "
            "silence this once you've independently verified."
        )
    for warning_line in _portal_labels_fallback_warning_lines(
        portal_labels_used_forward_compat_fallback=(
            report.portal_labels_used_forward_compat_fallback
        ),
        portal_labels_assessment_year_start=report.portal_labels_assessment_year_start,
    ):
        if lines:
            lines.append("")
        lines.append(warning_line)
    for warning_line in _cross_fy_reversal_warning_lines(report.cross_fy_reversal_report):
        if lines:
            lines.append("")
        lines.append(warning_line)
    if report.adr_routing_mode == ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER:
        if lines:
            lines.append("")
        lines.append(
            "ℹ️  ADR / GDR routing: UNDERLYING_ISSUER mode (issue #183) "  # noqa: RUF001
            "is configured. Schedule TR inherits Schedule FSI's source-"
            "country routing — Indian-ADR dividends are reported as "
            "domestic income on Schedule OS, and matching "
            "depositary-country withholding tax is symmetrically "
            "excluded from Schedule TR (issue #225 fix). Non-ADR "
            "foreign WHT and unrelated same-country foreign income "
            "continue to flow into Schedule TR normally."
        )
    # Zero-rows + upstream-exclusion advisories: TR carries the same
    # counters as Schedule FSI (they flow through via the shared
    # _ScheduleFsiComputeContext seam shipped in PR #217), and the
    # --quiet help promises "audit-bucket counts ALWAYS route to stderr
    # regardless of --quiet". Mirror the FSI text but reference TR's
    # upstream relationship (FSI → TR) so the user knows which sister
    # subcommand to consult for the audit detail. T3 r1 P2 fix to
    # PR #218.
    if report.rows_written == 0:
        if lines:
            lines.append("")
        fy_label = f"{report.financial_year_start.year}-{report.financial_year_end.year % 100:02d}"
        lines.append(
            f"ℹ️  No qualifying TR rows produced for FY {fy_label}; "  # noqa: RUF001
            f"wrote empty Schedule TR file (header only). Schedule TR "
            f"only surfaces countries with positive foreign tax paid, "
            f"so either (a) no foreign tax was withheld in this FY, or "
            f"(b) the input statement is missing the Withholding Tax "
            f"section. Run `ibkr-fa schedule-fsi` to inspect the "
            f"upstream per-(country, head) breakdown."
        )
    if report.excluded_cg_indian_source_count > 0 or report.excluded_ltcg_indian_source_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded Indian-source capital gains upstream of TR: "  # noqa: RUF001
            f"{report.excluded_cg_indian_source_count} STCG + "
            f"{report.excluded_ltcg_indian_source_count} LTCG. Indian-"
            f"source income is by definition not foreign-source; see "
            f"sister schedule-cg / schedule-cg-ltcg audit blocks "
            f"({EXCLUDED_CG_INDIAN_SOURCE_MARKER} / "
            f"{EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER})."
        )
    if report.excluded_cg_out_of_fy_count > 0 or report.excluded_ltcg_out_of_fy_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.excluded_cg_out_of_fy_count} STCG + "  # noqa: RUF001
            f"{report.excluded_ltcg_out_of_fy_count} LTCG closure(s) "
            f"with close_date outside the reporting FY (upstream of TR). "
            f"See {EXCLUDED_OUT_OF_FY_MARKER} / "
            f"{EXCLUDED_LTCG_OUT_OF_FY_MARKER} audit blocks in the "
            f"sister CG / CG-LTCG outputs."
        )
    if report.excluded_ltcg_pre_cutover_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.excluded_ltcg_pre_cutover_count} "  # noqa: RUF001
            f"LTCG closure(s) with disposal date before the 2024-07-23 "
            f"new-regime cutover (old-regime indexation pathway, out "
            f"of v1 scope; upstream of TR). See "
            f"{EXCLUDED_PRE_CUTOVER_MARKER} block in the sister "
            f"schedule-cg-ltcg output."
        )
    if report.interest_credits_out_of_fy_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.interest_credits_out_of_fy_count} "  # noqa: RUF001
            f"broker credit-interest event(s) with date outside the "
            f"reporting FY (upstream of TR). See sister `schedule-os` "
            f"1b(ix) summary."
        )
    relief_advisory = _schedule_tr_relief_advisory_line(report)
    if relief_advisory is not None:
        if lines:
            lines.append("")
        lines.append(relief_advisory)
    # TR foreign-TIN completeness nudge (mandatory for §90/90A/91 relief).
    if include_global_advisories and report.has_foreign_tax_without_taxpayer_id:
        if lines:
            lines.append("")
        lines.append(_FOREIGN_TIN_MISSING_ADVISORY)
    if include_global_advisories:
        if lines:
            lines.append("")
        lines.append(BMA_DISCLOSURE_ADVISORY)
        lines.append("")
        lines.append(ITR_FORM_SELECTION_ADVISORY)
    return "\n".join(lines)


def _schedule_tr_step(
    args: argparse.Namespace, *, include_global_advisories: bool
) -> tuple[int, ScheduleTrRunReport | None]:
    """Run ``schedule-tr``: its exit code, and its report unless it failed."""
    try:
        report = run_schedule_tr(
            input_path=args.input,
            config_path=args.config,
            output_path=args.output,
            financial_year=args.financial_year,
            format_id=args.format_id,
            itr_form=args.itr_form,
            allow_masked_account=args.allow_masked_account,
            accept_anonymised_statement=args.accept_anonymised_statement,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
        )
        with report.run_outputs.removed_on_failure():
            # never empty: the zero-row note or the relief note always prints
            warnings = _render_schedule_tr_safety_warnings(
                report, include_global_advisories=include_global_advisories
            )
            rendered = (
                _render_schedule_tr_report_quiet(report)
                if args.quiet
                else _render_schedule_tr_report(report)
            )
    except _DOMAIN_ERRORS as exc:
        return _domain_error("schedule-tr", exc), None

    _print_report(report.run_outputs, (warnings,), rendered)

    return (_EXIT_MANUAL_FILL_REQUIRED if report.manual_fill_required else _EXIT_OK), report


def _run_schedule_tr(args: argparse.Namespace) -> int:
    """Dispatch the ``schedule-tr`` subcommand."""
    return _schedule_tr_step(args, include_global_advisories=True)[0]


def _format_form67_full_precision_inr(value: Decimal) -> str:
    """Render a Form 67 audit INR cell at TRUE full precision (no quantize).

    Same convention as :func:`_format_tr_full_precision_inr`: the
    verbose "full precision" label must agree with the quiet CSV
    ``str(Decimal)`` rendering.
    """
    return str(value)


def _format_form67_rate_percent(value: Decimal) -> str:
    """Render an unfiled Form 67 audit rate as a two-decimal HALF_UP string.

    Filed rates come already rounded from the row's ``filed`` values (#898).
    This shows the audit rates the same way (PR #226 r1 T3 fix): Python's
    default `f"{value:.2f}"` would use ROUND_HALF_EVEN, which silently rounds
    ties in the opposite direction from the CBDT portal's HALF_UP convention.
    """
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{quantized:.2f}"


def _format_form67_rate_above_cap(rate: Decimal, cap: Decimal) -> str:
    """Render a rate above ``cap`` so that it reads above ``cap`` (#1467).

    The two-place HALF_UP text of :func:`_format_form67_rate_percent` when that
    reads above ``cap`` or ``rate`` isn't above it; otherwise the fewest further
    places, rounded down so the text never overstates ``rate``, that read above
    ``cap``.
    """
    text = _format_form67_rate_percent(rate)
    places = 2
    while Decimal(text) <= cap < rate:
        places += 1
        text = f"{rate.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN):f}"
    return text


# The short reason, per readiness refusal, why an entry with col 7 has no
# credit (#791). ZERO_CREDIT is absent: that entry has a credit, of 0.
_FORM67_UNCREDITED_REASONS: Final[dict[Form67RefusalReason, str]] = {
    Form67RefusalReason.SECTION_91_INPUTS_MISSING: "no [form67.section_91] inputs",
    Form67RefusalReason.NO_VENDORED_DTAA: "not in the vendored DTAA table",
    Form67RefusalReason.SOURCE_RATE_UNPROVEN: "source-currency rate not established",
    Form67RefusalReason.PAYMENT_ALLOCATION_UNPROVEN: "withholding not attributable per payment",
}
_FORM67_PARTIAL_PORTAL_NOTE: Final = (
    "The official portal-csv output refuses an entry with Indian tax payable but no "
    "credit, and is withheld while any entry's Indian tax payable and credit are blank."
)


def _form67_row_filled(row: Form67PerEntryRow) -> bool:
    """Whether col 7 and the credit are both set: READY, or a zero credit."""
    return (
        row.readiness.readiness is Form67Readiness.READY
        or row.readiness.reason is Form67RefusalReason.ZERO_CREDIT
    )


def _classify_form67_relief_population(
    per_entry_rows: tuple[Form67PerEntryRow, ...],
) -> str:
    """Classify the population status of the Form 67 relief columns
    (7, 9-13) across all per-entry rows, from each row's readiness.

    Returns one of four states (T4 r1 MEDIUM finding of PR #262; #791):

    * ``"fully_populated"`` — some row has col 7, and every row has col 7
      and its credit (READY, or the zero credit #601 refuses). See
      :mod:`ibkr_fa.compute.form67` for per-column fill rules.
    * ``"partially_populated"`` — some row has col 7, but some row has no
      credit: REFUSED with col 7 alone, or BLANK.
    * ``"dtaa_only"`` — every row is BLANK, and at least one has cols 9-10
      (``section_90_dtaa_article`` / ``section_90_dtaa_rate_percent``)
      populated from DTAA-vendoring alone. This is the DEFAULT no-config
      state for users holding DTAA-vendored securities (US/UK/FR/DE/JP/etc.)
      — cols 9-10 auto-populate from the vendored reference data while cols
      7, 11-13 remain blank until ``[form67]`` config is supplied.
    * ``"all_blank"`` — no per-entry row has any relief column
      populated (rare: no DTAA-vendored country AND no config).
    """
    if any(row.readiness.readiness is not Form67Readiness.BLANK for row in per_entry_rows):
        if all(_form67_row_filled(row) for row in per_entry_rows):
            return "fully_populated"
        return "partially_populated"
    if any(row.section_90_dtaa_article is not None for row in per_entry_rows):
        return "dtaa_only"
    return "all_blank"


def _form67_unfilled_relief_lines(per_entry_rows: tuple[Form67PerEntryRow, ...]) -> list[str]:
    """One line per entry without a credit: those with col 7 alone, then BLANK ones."""
    uncredited: list[str] = []
    blank: list[str] = []
    for row in per_entry_rows:
        entry = f"{row.iso_alpha2}/{row.source_of_income.name}"
        reason = row.readiness.reason
        if row.readiness.readiness is Form67Readiness.BLANK:
            blank.append(f"{entry}: Indian tax payable and credit blank; complete both by hand")
        elif reason is not None and reason is not Form67RefusalReason.ZERO_CREDIT:
            uncredited.append(
                f"{entry}: Indian tax payable supplied, but no credit computed "
                f"({_FORM67_UNCREDITED_REASONS[reason]})"
            )
    return [*uncredited, *blank]


def _form67_over_treaty_excess_lines(
    per_entry_rows: tuple[Form67PerEntryRow, ...],
) -> list[str]:
    """Advisory for entries with a payment whose source-currency tax rate
    exceeded the DTAA cap.

    Reads the ``ABOVE`` decision compute recorded on each row (#767), and
    names its highest per-payment rate, not the country average (#890).
    """
    flagged: list[tuple[Form67PerEntryRow, Decimal]] = []
    for row in per_entry_rows:
        if row.treaty_rate_status is not Form67TreatyRateStatus.ABOVE:
            continue
        paid_percent = row.highest_payment_rate_percent
        assert paid_percent is not None  # pinned by the row's ABOVE invariant
        flagged.append((row, paid_percent))
    if not flagged:
        return []
    lines = [
        "⚠️  Foreign withholding tax appears to exceed the DTAA treaty "
        "cap for the source(s) below. India's foreign tax credit "
        "(§90 / Rule 128(5)(i)) recognises foreign tax only up to the "
        "treaty-permitted source-country rate on the gross foreign "
        "income — and the credit is then further limited by the Indian "
        "tax payable on that same income — so the portion withheld above "
        "the treaty cap is NOT creditable through Form 67:"
    ]
    for row, paid_percent in flagged:
        cap_percent = row.filed.section_90_dtaa_rate_percent
        assert cap_percent is not None
        lines.append(
            f"     - {row.country_name_portal}: highest per-payment source-currency "
            f"withholding {_format_form67_rate_above_cap(paid_percent, cap_percent)}% "
            f"exceeds the DTAA cap "
            f"{cap_percent}%; automatic credit applies "
            "the conservative legacy INR cap to each payment above it, pending "
            "per-event eligible-tax conversion."
        )
    lines.append(
        "   Recovery of the excess, if available, is generally through a "
        "refund / treaty-relief claim with the SOURCE-country tax "
        "authority (for US over-withholding on portfolio dividends, e.g. "
        "an eligible non-resident US refund claim under the India-US "
        "treaty), not through Form 67. Any separate Indian deduction "
        "treatment is fact-specific. Consult your CA."
    )
    return lines


def _format_form67_inr_2dp(value: Decimal) -> str:
    """Render an INR amount to two decimals for advisory prose (₹0.00 for zero)."""
    return f"₹{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def _form67_indian_cap_shortfall_lines(
    per_entry_rows: tuple[Form67PerEntryRow, ...],
) -> list[str]:
    """Advisory for entries whose §90 credit was capped by Indian tax.

    The §90 credit is ``min(tax_payable_normal, eligible_foreign_tax_inr)``,
    both computed by :mod:`ibkr_fa.compute.form67`.
    When an exact per-source override is the binding constraint, part of
    the treaty-eligible foreign tax is left uncredited. This is the
    SECOND distinct cause of "credit <
    foreign tax paid"; the first — over-treaty withholding — is surfaced
    by :func:`_form67_over_treaty_excess_lines`. Unlike that one, this
    shortfall is not source-country over-withholding and is generally
    not recoverable by a source-country refund.

    Fires per entry on the §90/DTAA path (``eligible_foreign_tax_inr``
    and ``section_90_credit_inr`` both present — i.e. the user supplied
    the config needed to compute cols 7/11) where ``shortfall =
    eligible_foreign_tax_inr - section_90_credit_inr`` rounds to at least one
    paisa. ``shortfall > 0`` exactly means ``tax_payable_normal <
    eligible_foreign_tax_inr`` (the Indian-tax cap was the binding
    constraint). Returns ``[]`` when no entry qualifies. The §91
    (non-DTAA) path caps by the Indian average rate instead and is out
    of scope here.
    """
    flagged: list[tuple[Form67PerEntryRow, Decimal, Decimal, Decimal]] = []
    for row in per_entry_rows:
        eligible = row.eligible_foreign_tax_inr
        credit = row.section_90_credit_inr
        if eligible is None or credit is None:
            continue
        shortfall = eligible - credit
        if shortfall <= 0 or shortfall.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) < Decimal(
            "0.01"
        ):
            continue
        flagged.append((row, eligible, credit, shortfall))
    if not flagged:
        return []
    lines = [
        "ℹ️  India's §90 foreign tax credit is also capped at the Indian "  # noqa: RUF001
        "tax payable on the same income (Rule 128(5)). For the source(s) "
        "below, after applying the DTAA treaty cap, this Indian-tax cap is "
        "the binding limit, so part of the treaty-eligible foreign tax is "
        "left uncredited:"
    ]
    for row, eligible, credit, shortfall in flagged:
        lines.append(
            f"     - {row.country_name_portal}: of ~"
            f"{_format_form67_inr_2dp(eligible)} treaty-eligible foreign tax, "
            f"only ~{_format_form67_inr_2dp(credit)} is creditable (the Indian "
            f"tax computed for this income), leaving ~"
            f"{_format_form67_inr_2dp(shortfall)} unrelieved as Form 67 FTC "
            f"for this income/year."
        )
    lines.append(
        "   This is distinct from over-treaty withholding: it is generally "
        "NOT recoverable from the source country merely because India caps "
        "the credit (any source-country refund depends on source-country "
        "law). Check that the exact Indian tax payable supplied in "
        "[[form67.tax_payable_overrides]] reflects the actual tax on this "
        "income. Consult your CA."
    )
    return lines


def _render_form67_report(report: Form67RunReport) -> str:
    """Render the verbose Form 67 success summary for stdout."""
    lines = [
        "Form 67 — Statement of foreign income and Foreign Tax Credit",
        f"Wrote {report.rows_written} Form 67 entries to {report.output_path}",
        *(
            (
                "  Official Form 67 CSV withheld: one or more entries lack Indian tax payable "
                "and credit. Complete them from this review CSV before filing.",
            )
            if report.official_output_withheld
            else ()
        ),
        f"  Input format: {report.input_format.value}",
        f"  Account: {display_account_id(report.account_id)}",
        f"  Financial year: {report.financial_year_start.isoformat()} "
        f"to {report.financial_year_end.isoformat()}",
    ]
    if report.per_entry_rows:
        lines.append("")
        lines.append("  Per-entry breakdown:")
        lines.append(
            "    sl_no | cbdt_country_code | iso_alpha2 | country_name_portal | "
            "source_of_income | income_inr | foreign_tax_inr | foreign_tax_rate_percent | "
            "event_count"
        )
        for row in report.per_entry_rows:
            filed = row.filed
            lines.append(
                f"    {row.serial_number} | {row.cbdt_country_code} | "
                f"{row.iso_alpha2} | {row.country_name_portal} | "
                f"{row.source_of_income.value} | "
                f"INR {filed.income_inr} | "
                f"INR {filed.foreign_tax_inr} | "
                f"{_format_form67_rate_percent(row.foreign_tax_rate_percent)}% | "
                f"{row.event_count}"
            )
    else:
        lines.append("")
        lines.append("  Per-entry breakdown: no Form 67 entries (no foreign tax paid).")
    # Issue #261 audit (PR #262): this summary used to say the relief
    # columns were not computed, which was wrong once the relief PRs
    # (#230 through #233) shipped. Mirror the same 3-variant
    # classification from the safety-warnings advisory so the
    # verbose-render summary stays consistent. T3 r0 P0 of PR #262
    # surfaced the mirroring requirement; T4 r1 MEDIUM surfaced the
    # 3-variant split (DTAA-only is a real intermediate state for
    # users holding DTAA-vendored securities without [form67]
    # config — claiming "cols 7, 9-13 populated" for them was
    # wrong because cols 7, 11-13 require config). #791 added the
    # partially-populated state: col 7 alone isn't a filled credit.
    relief_state = _classify_form67_relief_population(report.per_entry_rows)
    if relief_state == "fully_populated":
        relief_summary_line = (
            "  Tax payable + DTAA article/rate + FTC credit columns: POPULATED "
            "(cols 7, 9-13) from exact overrides plus DTAA reference data "
            "(col 8 §115JB/JC "
            "special-rate still unimplemented per #235)"
        )
    elif relief_state == "partially_populated":
        rows = report.per_entry_rows
        partial = [
            "  Tax payable + DTAA article/rate + FTC credit columns: PARTIALLY POPULATED",
            *(f"    {line}" for line in _form67_unfilled_relief_lines(rows)),
        ]
        if any(_form67_row_filled(row) for row in rows):
            partial.append(
                "    Indian tax payable and credit are filled for every other entry "
                "(col 8 §115JB/JC special-rate still unimplemented per #235)."
            )
        partial.append(f"    {_FORM67_PARTIAL_PORTAL_NOTE}")
        relief_summary_line = "\n".join(partial)
    elif relief_state == "dtaa_only":
        relief_summary_line = (
            "  DTAA article + rate columns: POPULATED (cols 9-10) from DTAA "
            "reference data; cols 7, 11-13 BLANK until exact Indian tax "
            "attributable to the source is supplied"
        )
    else:  # "all_blank"
        relief_summary_line = (
            "  Tax payable + DTAA article/rate + FTC credit columns: BLANK — "
            "the source is not on the verified-DTAA path. A §91 claim requires "
            "an exact [[form67.tax_payable_overrides]] amount plus "
            "[form67.section_91] statutory average-rate inputs"
        )
    lines.extend(
        [
            "",
            "  Total income from outside India (portal-displayed): "
            f"INR {report.total_income_inr_portal_displayed}",
            "  Total foreign tax paid (portal-displayed): "
            f"INR {report.total_foreign_tax_inr_portal_displayed}",
            "  Total foreign tax paid (full precision): "
            f"INR {_format_form67_full_precision_inr(report.total_foreign_tax_inr_full_precision)}",
            relief_summary_line,
            "",
            "  Upstream consumption:",
            f"    FSI rows consumed: {report.fsi_rows_consumed}",
            f"    OS dividends: {report.os_dividend_rows_consumed}",
            f"    OS interest: {report.os_interest_rows_consumed}",
            f"    CG STCG: {report.cg_stcg_rows_consumed}",
            f"    CG LTCG: {report.cg_ltcg_rows_consumed}",
            f"    WHT in FY: {report.wht_rows_in_fy}",
            "",
            "  Exclusions:",
            f"    CG indian-source: {report.excluded_cg_indian_source_count}",
            f"    CG out-of-fy: {report.excluded_cg_out_of_fy_count}",
            f"    LTCG pre-cutover: {report.excluded_ltcg_pre_cutover_count}",
            f"    LTCG indian-source: {report.excluded_ltcg_indian_source_count}",
            f"    LTCG out-of-fy: {report.excluded_ltcg_out_of_fy_count}",
            f"    interest out-of-fy: {report.interest_credits_out_of_fy_count}",
            "",
            "  AY policy:",
            "    portal_labels_assessment_year_start: "
            f"{report.portal_labels_assessment_year_start}",
            f"    fx_policy_appropriate_for_ay: {report.fx_policy_appropriate_for_ay}",
            f"    wht_fx_policy_appropriate_for_ay: {report.wht_fx_policy_appropriate_for_ay}",
        ]
    )
    return "\n".join(lines)


def _render_form67_report_quiet(report: Form67RunReport) -> str:
    """Render the --quiet stdout for Form 67: CSV header + per-entry CSV."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        [
            "serial_number",
            "cbdt_country_code",
            "iso_alpha2",
            "country_name_portal",
            "source_of_income",
            "income_inr",
            "foreign_tax_inr",
            "foreign_tax_rate_percent",
            "event_count",
        ]
    )
    for row in report.per_entry_rows:
        writer.writerow(
            [
                row.serial_number,
                row.cbdt_country_code,
                row.iso_alpha2,
                row.country_name_portal,
                row.source_of_income.value,
                str(row.income_inr),
                str(row.foreign_tax_inr),
                str(row.foreign_tax_rate_percent),
                row.event_count,
            ]
        )
    return buf.getvalue().rstrip("\n")


def _render_form67_safety_warnings(  # noqa: PLR0912, PLR0915
    report: Form67RunReport, *, include_global_advisories: bool = True
) -> str:
    """Render the stderr safety-warning block for Form 67.

    Mirrors :func:`_render_schedule_tr_safety_warnings`. Form 67 has
    one ADDITIONAL warning vs Schedule TR: the AY 2027-28+ cross-AY
    block also flags the Form 67 → Form 44 / Rule 128 → Rule 76
    renumbering per the CBDT Form 44/45 FAQ (Form 67 writer continues
    to emit Form 67-labelled output regardless of AY in v1; the Form
    44 migration is tracked in epic #126).

    ``include_global_advisories=False`` leaves out the advisories ``run-all``
    prints once (see :func:`_run_all_global_advisories`).
    """
    lines = list(fractional_rate_warnings(report.per_entry_rows))
    if include_global_advisories and report.has_unresolved_indian_tax_payable:
        lines.append(missing_indian_tax_payable_warning(report.override_advice))
    if include_global_advisories and not report.account_id_cross_checked:
        lines.append(_MASKED_ACCOUNT_SKIP_WARNING)
    for warning_line in _identity_fallback_warning_lines(report.identity_supplied_from_fallback):
        if lines:
            lines.append("")
        lines.append(warning_line)
    if not report.fx_policy_appropriate_for_ay or not report.wht_fx_policy_appropriate_for_ay:
        if lines:
            lines.append("")
        lines.append(
            "⚠️  This run targets AY 2027-28 or later, which is governed "
            "by the Income-tax Act, 2025 + Income-tax Rules, 2026."
        )
        lines.append(
            "   Rule 206(2)(b)(i) preserves the Rule 115 preceding-"
            "month-end semantic for income FX, and Rule 76(7)(b) "
            "preserves the Rule 128(5)(ii) preceding-month-end semantic "
            "for foreign-tax-credit FX. Both require independent "
            "primary-source verification of the carry-back fallback "
            "across non-publication days."
        )
        lines.append(
            "   See https://github.com/jay-tau/ibkr-fa/issues/207 for "
            "FX-policy verification status. Pass --suppress-cross-ay-warning "
            "to silence this once you've independently verified."
        )
    # Form 44 / Rule 76 form-name renumbering: gated INDEPENDENTLY of
    # the FX-policy warning. T3 r2 of PR #228 surfaced that coupling
    # Form 44 to the FX-policy gate means the form-name advisory
    # disappears when FX policy is verified (or when the user passes
    # --suppress-cross-ay-warning) — even though the v1 writer still
    # emits Form 67-labelled output regardless of AY. The form-name
    # renumbering is a separate concern from FX-policy verification;
    # it fires whenever the targeted AY is 2027-28+ AND Form 67-
    # labelled output is still being emitted (i.e., epic #126 hasn't
    # shipped Form 44 portal labels yet).
    min_form_44_ay_start = 2027
    if report.portal_labels_assessment_year_start >= min_form_44_ay_start:
        if lines:
            lines.append("")
        lines.append(
            "ℹ️  Form 67 → Form 44 renumbering: under the AY 2027-28+ "  # noqa: RUF001
            "ITR utility, CBDT Form 67 is renumbered to Form 44 (Rule "
            "128 → Rule 76 per the CBDT Form 44/45 FAQ + Navigator "
            "rule-wise comparison). The v1 writer emits Form 67-labelled "
            "output regardless of AY; the Form 44 migration is tracked "
            "in epic #126. This advisory is INDEPENDENT of the cross-AY "
            "FX-policy warning above and is NOT silenced by "
            "--suppress-cross-ay-warning."
        )
    for warning_line in _portal_labels_fallback_warning_lines(
        portal_labels_used_forward_compat_fallback=(
            report.portal_labels_used_forward_compat_fallback
        ),
        portal_labels_assessment_year_start=report.portal_labels_assessment_year_start,
    ):
        if lines:
            lines.append("")
        lines.append(warning_line)
    for warning_line in _cross_fy_reversal_warning_lines(report.cross_fy_reversal_report):
        if lines:
            lines.append("")
        lines.append(warning_line)
    if report.adr_routing_mode == ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER:
        if lines:
            lines.append("")
        lines.append(
            "ℹ️  ADR / GDR routing: UNDERLYING_ISSUER mode (issue #183) "  # noqa: RUF001
            "is configured. Indian-ADR dividends are reported as "
            "domestic income on Schedule OS, and matching "
            "depositary-country withholding tax is symmetrically "
            "excluded from Form 67 (issue #225 fix). No FTC claim is "
            "produced for those Indian-ADR WHT amounts via this "
            "schedule — recover them domestically via TDS / §90 "
            "relief on your Indian-source dividend declaration if "
            "applicable. Non-ADR foreign WHT and unrelated "
            "same-country foreign dividends continue to flow into "
            "Form 67 normally."
        )
    # Zero-rows + upstream-exclusion advisories: Form 67 carries the same
    # counters as Schedule FSI / TR via the shared FSI compute seam.
    if report.rows_written == 0:
        if lines:
            lines.append("")
        fy_label = f"{report.financial_year_start.year}-{report.financial_year_end.year % 100:02d}"
        lines.append(
            f"ℹ️  No qualifying Form 67 entries produced for FY {fy_label}; "  # noqa: RUF001
            f"wrote empty Form 67 file (header only). Form 67 only "
            f"surfaces (country, source-of-income) aggregates with "
            f"positive netted foreign tax paid AND positive matching "
            f"foreign income, so either (a) no foreign tax was withheld "
            f"in this FY, or (b) the input statement is missing the "
            f"Withholding Tax / Dividends sections. Run "
            f"`ibkr-fa schedule-fsi` to inspect the upstream "
            f"per-(country, head) breakdown."
        )
    if report.excluded_cg_indian_source_count > 0 or report.excluded_ltcg_indian_source_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded Indian-source capital gains upstream of Form 67: "  # noqa: RUF001
            f"{report.excluded_cg_indian_source_count} STCG + "
            f"{report.excluded_ltcg_indian_source_count} LTCG. Indian-"
            f"source income is by definition not foreign-source; see "
            f"sister schedule-cg / schedule-cg-ltcg audit blocks "
            f"({EXCLUDED_CG_INDIAN_SOURCE_MARKER} / "
            f"{EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER})."
        )
    if report.excluded_cg_out_of_fy_count > 0 or report.excluded_ltcg_out_of_fy_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.excluded_cg_out_of_fy_count} STCG + "  # noqa: RUF001
            f"{report.excluded_ltcg_out_of_fy_count} LTCG closure(s) "
            f"with close_date outside the reporting FY (upstream of Form 67). "
            f"See {EXCLUDED_OUT_OF_FY_MARKER} / "
            f"{EXCLUDED_LTCG_OUT_OF_FY_MARKER} audit blocks in the "
            f"sister CG / CG-LTCG outputs."
        )
    if report.excluded_ltcg_pre_cutover_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.excluded_ltcg_pre_cutover_count} "  # noqa: RUF001
            f"LTCG closure(s) with disposal date before the 2024-07-23 "
            f"new-regime cutover (old-regime indexation pathway, out "
            f"of v1 scope; upstream of Form 67). See "
            f"{EXCLUDED_PRE_CUTOVER_MARKER} block in the sister "
            f"schedule-cg-ltcg output."
        )
    if report.interest_credits_out_of_fy_count > 0:
        if lines:
            lines.append("")
        lines.append(
            f"ℹ️  Excluded {report.interest_credits_out_of_fy_count} "  # noqa: RUF001
            f"broker credit-interest event(s) with date outside the "
            f"reporting FY (upstream of Form 67). See sister "
            f"`schedule-os` 1b(ix) summary."
        )
    if lines:
        lines.append("")
    # Issue #261 audit (PR #262): check whether the relief cols
    # (7, 11-13) are populated for any emitted Form 67 entry. The
    # §90 / §91 / DTAA relief computation was shipped across PRs
    # #230 through #233 (#37 step 4/N a-d) and populates these
    # cols conditional on the user supplying `[form67]` + per-
    # (country, source) DTAA-vendored applicability. Col 8
    # (§115JB/JC special-rate tax payable) is the only sub-step
    # deferred — tracked at #235.
    #
    # 3-variant classification per T4 r1 MEDIUM finding:
    # cols 9 + 10 (DTAA article + rate) populate INDEPENDENTLY of
    # `[form67]` config via DTAA-vendoring; cols 7, 11-13 require
    # config (col 7 directly; credits through their col 7 gates in
    # compute/form67.py).
    # ``dtaa_only`` is the DEFAULT no-config state for any user
    # holding DTAA-vendored securities (US/UK/FR/DE/JP/etc.); we
    # must NOT tell those users that cols 7, 11-13 are populated
    # when they're actually blank. Nor, since #791, any user one of
    # whose entries has col 7 but no credit, or no col 7 at all.
    relief_state = _classify_form67_relief_population(report.per_entry_rows)
    if relief_state == "fully_populated":
        lines.append(
            "ℹ️  Form 67 relief columns (7, 9-13) populated from exact "  # noqa: RUF001
            "overrides plus DTAA-vendored reference data. Verify the rendered "
            "tax payable / DTAA "
            "article / §90 / §91 / total credit values against your "
            "CA's expectations before filing. Col 8 (§115JB/JC "
            "special-rate tax payable, for assessees subject to MAT / "
            "AMT) is the only sub-step still unimplemented (#235); "
            "fill that one manually if applicable."
        )
    elif relief_state == "partially_populated":
        rows = report.per_entry_rows
        lines.append("⚠️  Form 67 relief columns are PARTIALLY POPULATED:")
        lines.extend(f"     - {line}" for line in _form67_unfilled_relief_lines(rows))
        if any(_form67_row_filled(row) for row in rows):
            lines.append(
                "   Indian tax payable and credit are filled for every other entry. "
                "Verify them against your CA's expectations before filing. Col 8 "
                "(§115JB/JC special-rate tax payable) is still unimplemented (#235); "
                "fill it manually if applicable."
            )
        lines.append(f"   {_FORM67_PARTIAL_PORTAL_NOTE}")
    elif relief_state == "dtaa_only":
        lines.append(
            "ℹ️  Form 67 DTAA article + rate (cols 9-10) populated, but "  # noqa: RUF001
            "cols 7 and 11-13 remain blank until exact Indian tax attributable "
            "to the source is supplied."
        )
    else:  # "all_blank"
        lines.append(
            "ℹ️  Form 67 relief columns (7-13) left blank because no "  # noqa: RUF001
            "verified DTAA relief path was available. A §91 claim requires "
            "an exact [[form67.tax_payable_overrides]] amount plus both "
            "[form67.section_91] statutory average-rate inputs."
        )
    for excess_line in _form67_over_treaty_excess_lines(report.per_entry_rows):
        if excess_line.startswith("⚠️") and lines:
            lines.append("")
        lines.append(excess_line)
    for shortfall_line in _form67_indian_cap_shortfall_lines(report.per_entry_rows):
        if shortfall_line.startswith("ℹ️") and lines:  # noqa: RUF001
            lines.append("")
        lines.append(shortfall_line)
    if include_global_advisories:
        lines.append("")
        lines.append(BMA_DISCLOSURE_ADVISORY)
        lines.append("")
        lines.append(ITR_FORM_SELECTION_ADVISORY)
    return "\n".join(lines)


def _form67_step(
    args: argparse.Namespace, *, include_global_advisories: bool
) -> tuple[int, Form67RunReport | None]:
    """Run ``form-67``: its exit code, and its report unless it failed."""
    try:
        report = run_form67(
            input_path=args.input,
            config_path=args.config,
            output_path=args.output,
            financial_year=args.financial_year,
            format_id=args.format_id,
            allow_masked_account=args.allow_masked_account,
            accept_anonymised_statement=args.accept_anonymised_statement,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
            debug_output_path=(
                args.output.with_name("form-67-debug.csv")
                if getattr(args, "debug_form67", False)
                else None
            ),
            all_mode_readiness=getattr(args, "form67_all_mode_readiness", False),
        )
        with report.run_outputs.removed_on_failure():
            # never empty: the relief-columns note always prints
            warnings = _render_form67_safety_warnings(
                report, include_global_advisories=include_global_advisories
            )
            rendered = (
                _render_form67_report_quiet(report) if args.quiet else _render_form67_report(report)
            )
    except _DOMAIN_ERRORS as exc:
        return _domain_error("form-67", exc), None

    _print_report(report.run_outputs, (warnings,), rendered)

    return (_EXIT_MANUAL_FILL_REQUIRED if report.manual_fill_required else _EXIT_OK), report


def _run_form67(args: argparse.Namespace) -> int:
    """Dispatch the ``form-67`` subcommand."""
    return _form67_step(args, include_global_advisories=True)[0]


def _run_init(args: argparse.Namespace) -> int:
    """Dispatch the ``init`` subcommand."""
    path: Path = args.path
    try:
        write_user_toml_template(path, force=args.force)
    except FileExistsError:
        print(
            f"error: {path} already exists. Pass --force to overwrite it.",
            file=sys.stderr,
        )
        return _EXIT_DOMAIN_ERROR
    except PathRefusalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_DOMAIN_ERROR
    except OSError as exc:
        print(f"error: {_filesystem_problem('could not write', exc, path)}", file=sys.stderr)
        return _EXIT_DOMAIN_ERROR
    print(f"Wrote starter config to {path}.")
    print(
        "Next: uncomment and fill the [account] table (required), then "
        "uncomment any optional sections you need, before running ibkr-fa."
    )
    return _EXIT_OK


def _render_merge_report(report: CbdtMergeReport) -> str:
    """Loud, non-quietable summary of what the merge changed."""
    lines = [
        f"Merged the Schedule FA fragment into {report.output_path} ({report.itr_form})",
        f"  Table A2 (foreign custodial account): +{report.a2_rows_added} row(s) "
        f"for {len(report.a2_account_numbers)} account(s) "
        f"({', '.join(display_account_id(item) for item in report.a2_account_numbers)})",
        f"  Table A3 (foreign equity & debt): +{report.a3_rows_added} row(s)",
    ]
    if report.preserved_a2_accounts:
        others = ", ".join(display_account_id(a) for a in report.preserved_a2_accounts)
        lines.append(
            f"  Preserved {report.preserved_a2_rows} existing Table A2 row(s) "
            f"for {len(report.preserved_a2_accounts)} other account(s): "
            f"{others} — verify they are correct"
        )
    if report.created_schedule_fa:
        lines.append(
            "  Created a new Schedule FA (the target previously declared no "
            "foreign assets — confirm that flip is intended)"
        )
    lines.append(
        "  Stripped the _ibkrFa provenance marker; your original --fragment and "
        "--itr files are unchanged."
    )
    lines.append(f"  Import {report.output_path} into the offline {report.itr_form} utility.")
    lines.append("")
    lines.append(BMA_DISCLOSURE_ADVISORY)
    return "\n".join(lines)


def _run_merge(args: argparse.Namespace) -> int:
    """Dispatch the ``merge`` subcommand."""
    try:
        report = merge_cbdt_fragment(
            fragment_path=args.fragment,
            itr_path=args.itr,
            output_path=args.output,
        )
        with report.run_outputs.removed_on_failure():
            rendered = _render_merge_report(report)
    except _DOMAIN_ERRORS as exc:
        # A refused merge (CbdtJsonMergeError), any other domain error (e.g. a
        # schema check) or a filesystem failure during the atomic write surfaces
        # as a clean exit, never an uncaught traceback.
        return _domain_error("merge", exc)
    _print_report(report.run_outputs, (), rendered)
    return _EXIT_OK


def _robinhood_exit(status: RobinhoodLegStatus) -> int:
    if status in {RobinhoodLegStatus.OK, RobinhoodLegStatus.NOT_REQUIRED}:
        return _EXIT_OK
    if status is RobinhoodLegStatus.MANUAL:
        return _EXIT_MANUAL_FILL_REQUIRED
    return _EXIT_DOMAIN_ERROR


def _render_robinhood_leg(report: RobinhoodLegReport) -> str:
    lines = [f"Robinhood {report.name}: {report.status.value}"]
    lines.extend(f"  Wrote: {path}" for path in report.outputs)
    lines.extend(f"  Action: {reason}" for reason in report.reasons)
    lines.extend(f"  Warning: {warning}" for warning in report.warnings)
    lines.extend(("", BMA_DISCLOSURE_ADVISORY, "", ITR_FORM_SELECTION_ADVISORY))
    return "\n".join(lines)


def _run_robinhood_cg(args: argparse.Namespace) -> int:
    return _run_report_command(
        "robinhood-cg",
        lambda: run_robinhood_cg(
            input_dir=args.input,
            config_path=args.config,
            output_dir=args.output_dir,
            financial_year=args.financial_year,
            itr_form=args.itr_form,
            format_id=args.format_id,
            debug=args.debug,
            fetch_prices=not args.no_fetch_prices,
            price_cache_dir=args.price_cache_dir,
        ),
        lambda report: ((), _render_robinhood_leg(report)),
        lambda report: _robinhood_exit(report.status),
    )


def _run_robinhood_income(args: argparse.Namespace) -> int:
    return _run_report_command(
        "robinhood-income",
        lambda: run_robinhood_income(
            input_dir=args.input,
            config_path=args.config,
            output_dir=args.output_dir,
            financial_year=args.financial_year,
            itr_form=args.itr_form,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
            debug=args.debug,
            fetch_prices=not args.no_fetch_prices,
            price_cache_dir=args.price_cache_dir,
        ),
        lambda report: ((), _render_robinhood_leg(report)),
        lambda report: _robinhood_exit(report.status),
    )


def _run_robinhood_fa(args: argparse.Namespace) -> int:
    return _run_report_command(
        "robinhood-fa",
        lambda: run_robinhood_fa(
            input_dir=args.input,
            config_path=args.config,
            output_dir=args.output_dir,
            financial_year=args.financial_year,
            itr_form=args.itr_form,
            format_id=args.format_id,
            fetch_prices=not args.no_fetch_prices,
            price_cache_dir=args.price_cache_dir,
        ),
        lambda report: ((), _render_robinhood_leg(report)),
        lambda report: _robinhood_exit(report.status),
    )


def _run_robinhood_options(args: argparse.Namespace) -> int:
    return _run_report_command(
        "robinhood-options",
        lambda: run_robinhood_options(
            input_dir=args.input,
            config_path=args.config,
            output_dir=args.output_dir,
            financial_year=args.financial_year,
            itr_form=args.itr_form,
        ),
        lambda report: ((), _render_robinhood_leg(report)),
        lambda report: _robinhood_exit(report.status),
    )


def _render_robinhood_all(report: RobinhoodAllReport) -> str:
    lines = [
        (
            "ROBINHOOD SUMMARY — "
            f"FY {report.financial_year.start_year}-"
            f"{report.financial_year.end_year_2digit:02d} / CY {report.calendar_year}"
        ),
        f"STATUS: {report.status.value}",
    ]
    for leg in report.legs:
        lines.append(f"  {leg.status.value:12} {leg.name}")
        lines.extend(f"                 -> {reason}" for reason in leg.reasons)
        lines.extend(f"                 -> warning: {warning}" for warning in leg.warnings)
        lines.extend(f"                 -> {path}" for path in leg.outputs)
    lines.extend(("", BMA_DISCLOSURE_ADVISORY, "", ITR_FORM_SELECTION_ADVISORY))
    return "\n".join(lines)


def _run_robinhood_all(args: argparse.Namespace) -> int:
    removal_error: RobinhoodAllRemovalError | None = None
    try:
        try:
            report = run_robinhood_all(
                input_dir=args.input,
                config_path=args.config,
                output_dir=args.output_dir,
                financial_year=args.financial_year,
                itr_form=args.itr_form,
                format_id=args.format_id,
                fetch_prices=not args.no_fetch_prices,
                price_cache_dir=args.price_cache_dir,
                suppress_cross_ay_warning=args.suppress_cross_ay_warning,
                debug=args.debug,
            )
        except RobinhoodAllRemovalError as exc:
            # The run failed and could not remove its outputs: its report still
            # prints, then the error.
            removal_error, report = exc, exc.report
        with report.run_outputs.removed_on_failure():
            rendered = _render_robinhood_all(report)
    except _DOMAIN_ERRORS as exc:
        if removal_error is not None:
            print(f"error: {removal_error}", file=sys.stderr)
        return _domain_error("robinhood-all", exc)
    if removal_error is not None:
        # Retries the removals if the report or the error cannot be printed.
        with report.run_outputs.removed_on_failure():
            print(rendered, flush=True)
            print(f"error: {removal_error}", file=sys.stderr)
        return _EXIT_DOMAIN_ERROR
    _print_report(report.run_outputs, (), rendered)
    return _robinhood_exit(report.status)


class _RunAllOutcome(NamedTuple):
    """One schedule's result within a ``run-all`` invocation."""

    name: str
    status: str  # "ok" | "manual_fill" | "error" | "skipped"
    output_path: Path | None
    detail: str


def _build_run_all_sub_args(
    *,
    input_path: Path,
    output_path: Path,
    config_path: Path,
    format_id: str,
    itr_form: ItrForm,
    financial_year: FinancialYear,
    calendar_year: int,
    quiet: bool,
    allow_masked_account: bool,
    accept_anonymised_statement: bool,
    suppress_cross_ay_warning: bool,
    no_fetch_prices: bool,
    price_cache_dir: Path | None,
    debug_form67: bool = False,
) -> argparse.Namespace:
    """Build a per-schedule ``Namespace`` covering the superset of every
    attribute any ``_run_schedule_*`` / ``_run_form67`` handler reads.

    Setting attributes a given handler ignores is harmless; this single
    builder is the one place to update if a subcommand grows a new flag,
    so ``run-all`` cannot silently diverge into an ``AttributeError``.
    """
    return argparse.Namespace(
        input=input_path,
        output=output_path,
        config=config_path,
        format_id=format_id,
        itr_form=itr_form,
        financial_year=financial_year,
        calendar_year=calendar_year,
        quiet=quiet,
        allow_masked_account=allow_masked_account,
        accept_anonymised_statement=accept_anonymised_statement,
        suppress_cross_ay_warning=suppress_cross_ay_warning,
        no_fetch_prices=no_fetch_prices,
        price_cache_dir=price_cache_dir,
        debug_form67=debug_form67,
        form67_all_mode_readiness=True,
    )


type _RunAllReport = (
    RunReport
    | ScheduleOsRunReport
    | ScheduleCgRunReport
    | ScheduleCgLtcgRunReport
    | ScheduleFsiRunReport
    | ScheduleTrRunReport
    | Form67RunReport
)


class _RunAllStep(Protocol):
    """A schedule step ``run-all`` runs: its exit code, and its report unless it failed."""

    def __call__(
        self, args: argparse.Namespace, *, include_global_advisories: bool
    ) -> tuple[int, _RunAllReport | None]: ...


class _PreFilingAction(Enum):
    """A "Before you file" checklist item (see :data:`_PRE_FILING_ACTION_ITEMS`)."""

    FA_A2_SKIPPED = auto()
    FOREIGN_TIN_MISSING = auto()


# "Before you file" checklist — completeness action-items that are easy to
# miss in the per-schedule scrollback because the schedule still generated a
# file. Each entry pairs an item with a short imperative remedy line
# for the run-all summary. An item is listed ONLY when a run report calls for
# it (see :func:`_pre_filing_actions`); the full advisory still prints in the
# schedule's own output. Ordered by filing priority.
_PRE_FILING_ACTION_ITEMS: Final[tuple[tuple[_PreFilingAction, str], ...]] = (
    (
        _PreFilingAction.FA_A2_SKIPPED,
        "Schedule FA Table A2 (the foreign custodial account itself) was NOT "
        "generated — add an [account.custodian] block to user.toml (run "
        "`ibkr-fa init` for a template) and re-run. ITR-2 needs Table A2 in "
        "addition to the Table A3 holdings.",
    ),
    (
        _PreFilingAction.FOREIGN_TIN_MISSING,
        "Foreign Taxpayer Identification Number is BLANK on a relief-claiming "
        "Schedule FSI / TR row — add the TIN to [account.foreign_tins] in "
        "user.toml, or use your passport number if that country did not allot "
        "you a TIN (mandatory when claiming §90/90A/91 relief).",
    ),
)


def _pre_filing_actions(reports: Sequence[_RunAllReport]) -> set[_PreFilingAction]:
    """The checklist items the run reports call for: Table A2 skipped on the
    Schedule FA report, or a blank foreign TIN on a Schedule FSI or TR report."""
    actions: set[_PreFilingAction] = set()
    for report in reports:
        if isinstance(report, RunReport) and not report.a2_rows:
            actions.add(_PreFilingAction.FA_A2_SKIPPED)
        if (
            isinstance(report, ScheduleFsiRunReport | ScheduleTrRunReport)
            and report.has_foreign_tax_without_taxpayer_id
        ):
            actions.add(_PreFilingAction.FOREIGN_TIN_MISSING)
    return actions


def _run_all_global_advisories(reports: Sequence[_RunAllReport]) -> list[str]:
    """The global advisories ``run-all`` prints once, that the run reports
    call for: each conditional one when any report sets its field, and the
    BMA + ITR-form walls when any schedule returned a report.

    These have the same text for every schedule, so each step's renderer
    leaves them out under ``run-all`` (``include_global_advisories=False``)
    instead of printing them up to 7x. Schedule-specific advisories (TR
    relief, DTAA-excess / ADR notes, the Table A2 skip) still print with
    their schedule. Order = emission order: the conditional warnings first,
    then the two ~500-word compliance walls last (matching the per-schedule
    order). #178/#179 non-quietable contract preserved: each still fires once
    and ignores --quiet.

    The missing Indian tax warning gives the per-row advice of the reports
    that set its field, each sentence once, in report order (#1474). Those
    reports read one Form 67 result, so that is its advice.
    """
    blank_tax_reports = [
        report
        for report in reports
        if isinstance(report, ScheduleFsiRunReport | ScheduleTrRunReport | Form67RunReport)
        and report.has_unresolved_indian_tax_payable
    ]
    fired = (
        (
            _MASKED_ACCOUNT_SKIP_WARNING,
            any(not report.account_id_cross_checked for report in reports),
        ),
        (
            _FOREIGN_TIN_MISSING_ADVISORY,
            any(
                isinstance(report, ScheduleFsiRunReport | ScheduleTrRunReport)
                and report.has_foreign_tax_without_taxpayer_id
                for report in reports
            ),
        ),
        (
            missing_indian_tax_payable_warning(
                dict.fromkeys(
                    sentence for report in blank_tax_reports for sentence in report.override_advice
                )
            ),
            bool(blank_tax_reports),
        ),
        (BMA_DISCLOSURE_ADVISORY, bool(reports)),
        (ITR_FORM_SELECTION_ADVISORY, bool(reports)),
    )
    return [advisory for advisory, fires in fired if fires]


def _invoke_run_all_handler(  # noqa: PLR0911
    name: str,
    step: _RunAllStep,
    sub_args: argparse.Namespace,
    output_path: Path,
    reports: list[_RunAllReport],
) -> tuple[_RunAllOutcome, int]:
    """Clear stale targets and run a step, classifying its result for the summary.

    The step runs with ``include_global_advisories=False``, and the report it
    returns is appended to ``reports``: ``run-all`` prints the global
    advisories once and builds its "Before you file" checklist from those
    reports. The step's stdout + stderr are captured and replayed unchanged
    (never inspected). Capture/replay is local to ``run-all`` —
    single-subcommand runs are untouched, so the #178/#179 non-quietable
    contract stays byte-identical there. Output is replayed to the streams
    captured BEFORE the redirect (so pytest ``capsys`` / shell redirection
    still see it) even if the step raises, with stderr (the warnings block)
    before stdout (the report) to match the live per-schedule order. Note:
    this preserves "stderr block then stdout block", not arbitrary
    stdout/stderr interleaving — fine for today's steps.

    Wraps the call so an UNEXPECTED exception (a bug, not a handled domain
    error — those already return code 4) is recorded as an internal error
    and ``run-all`` continues with the next schedule. A success or manual
    fill without a report is an internal error too, and one without every
    file :func:`_run_all_promised_outputs` names is an error. ``run-all``
    removes every output once any schedule errors.
    """
    try:
        _clear_run_all_targets(output_path.parent, name)
    except OutputRemovalError as exc:
        return _run_all_clearing_error(name, exc)
    real_out, real_err = sys.stdout, sys.stderr
    out_buf, err_buf = io.StringIO(), io.StringIO()
    code: int | None = None
    report: _RunAllReport | None = None
    error_outcome: _RunAllOutcome | None = None
    try:
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code, report = step(sub_args, include_global_advisories=False)
    except Exception as exc:
        # Orchestrator must survive a per-schedule crash (a bug, not a
        # handled domain error — those already return code 4) so the
        # remaining schedules still run. IBKR_FA_TRACEBACK=1 re-raises it
        # instead, after the replay below.
        if _traceback_requested():
            raise
        error_outcome = _RunAllOutcome(
            name,
            "error",
            None,
            f"internal error: crashed unexpectedly ({crash_site(exc)}); no output written",
        )
    finally:
        # Replay captured output, even on a raise (don't lose partial
        # diagnostics). stderr before stdout matches the live warnings-then-
        # report order; flush so the per-schedule blocks stay correctly
        # ordered when stdout is block-buffered (e.g. redirected to a file
        # alongside 2>&1 stderr). strip("\n"): print adds the newline back,
        # and the per-schedule header already provides separation.
        for buf, stream in ((err_buf, real_err), (out_buf, real_out)):
            text = buf.getvalue()
            if text.strip():
                print(text.strip("\n"), file=stream, flush=True)
    if error_outcome is not None:
        return error_outcome, _EXIT_GENERIC
    assert code is not None  # set whenever no exception was raised
    if report is not None:
        reports.append(report)
    elif code in (_EXIT_OK, _EXIT_MANUAL_FILL_REQUIRED):
        return (
            _RunAllOutcome(
                name, "error", None, f"internal error: exit {code} without a run report"
            ),
            _EXIT_GENERIC,
        )
    if code in (_EXIT_OK, _EXIT_MANUAL_FILL_REQUIRED) and (
        problems := _unproduced_outputs(_run_all_promised_outputs(name, sub_args, report))
    ):
        return (
            _RunAllOutcome(
                name,
                "error",
                None,
                f"exit {code} without its promised output: {problems}; no current output "
                "— ignore any existing files for this schedule",
            ),
            _EXIT_DOMAIN_ERROR,
        )
    if code == _EXIT_OK:
        return (_RunAllOutcome(name, "ok", output_path, ""), code)
    if code == _EXIT_MANUAL_FILL_REQUIRED:
        withheld = _withheld_form67_path(report)
        return (
            _RunAllOutcome(
                name,
                "manual_fill",
                output_path if withheld is None else withheld,
                _run_all_manual_fill_detail(report),
            ),
            code,
        )
    return (
        _RunAllOutcome(
            name,
            "error",
            None,
            f"failed (exit {code}); no current output — ignore any existing {output_path.name}",
        ),
        code,
    )


def _aggregate_run_all_exit_code(codes: list[int]) -> int:
    """Worst-of aggregate: internal error > domain error > manual-fill > clean."""
    if any(code == _EXIT_GENERIC for code in codes):
        return _EXIT_GENERIC
    if any(code not in (_EXIT_OK, _EXIT_MANUAL_FILL_REQUIRED) for code in codes):
        return _EXIT_DOMAIN_ERROR
    if any(code == _EXIT_MANUAL_FILL_REQUIRED for code in codes):
        return _EXIT_MANUAL_FILL_REQUIRED
    return _EXIT_OK


_RUN_ALL_RULE: Final[str] = "=" * 72
_RUN_ALL_STATUS_LABEL: Final[dict[str, str]] = {
    "ok": "ok         ",
    "manual_fill": "manual-fill",
    "error": "ERROR      ",
    "skipped": "SKIPPED    ",
}


def _action_needed_remedy_clause(*, has_manual: bool, has_actions: bool) -> str:
    """The 'ACTION NEEDED' remedy clause, naming only the part(s) shown below
    (per-schedule manual-fill flags and/or the 'Before you file' checklist),
    so the status line never references a checklist that did not render."""
    if has_manual and has_actions:
        return (
            "complete the manual-fill column(s) flagged below and the 'Before you file' checklist"
        )
    if has_actions:
        return "complete the 'Before you file' checklist below"
    return "complete the manual-fill column(s) flagged below"


def _render_run_all_summary(
    outcomes: list[_RunAllOutcome],
    *,
    financial_year: FinancialYear,
    calendar_year: int,
    fa_price_cache_dir: Path | None,
    pre_filing_actions: set[_PreFilingAction] | None = None,
    removal_failed: bool = False,
) -> str:
    """Render the final status-first summary block (-> stderr).

    ``removal_failed``: a failed run could not remove all of its output.
    """
    has_error = any(o.status == "error" for o in outcomes)
    has_manual = any(o.status == "manual_fill" for o in outcomes)
    has_skip = any(o.status == "skipped" for o in outcomes)
    # Action-items in priority order, including only those that fired.
    actions = pre_filing_actions or set()
    ordered_actions = [remedy for action, remedy in _PRE_FILING_ACTION_ITEMS if action in actions]
    fy_label = f"{financial_year.start_year}-{(financial_year.start_year + 1) % 100:02d}"
    lines = [
        "",
        _RUN_ALL_RULE,
        f"RUN-ALL SUMMARY — FY {fy_label} / Schedule FA calendar year {calendar_year}",
    ]
    if has_error:
        lines.append(
            "STATUS: FAILED — resolve every error below and re-run. "
            + (
                "This run's output could not all be removed (see the error above): "
                "do NOT file any of it."
                if removal_failed
                else "Every output of this run was removed."
            )
        )
    elif has_manual or ordered_actions:
        # Both manual-fill columns and the completeness action-items below are
        # "address before filing" items — never report a bare OK while either
        # is outstanding (a filer who reads only the status line must not
        # think the output is final). The remedy clause names only the
        # part(s) actually shown below.
        remedy_clause = _action_needed_remedy_clause(
            has_manual=has_manual, has_actions=bool(ordered_actions)
        )
        lines.append(
            f"STATUS: ACTION NEEDED — every schedule ran, but {remedy_clause} before filing."
        )
    elif has_skip:
        lines.append(
            "STATUS: OK — the six financial-year schedules were generated "
            "(Schedule FA skipped — see note)."
        )
    else:
        lines.append("STATUS: OK — every requested schedule was generated.")
    if ordered_actions and not has_error:
        lines.append("")
        lines.append(f"Before you file ({len(ordered_actions)} item(s) to address):")
        for i, remedy in enumerate(ordered_actions, start=1):
            lines.append(f"  {i}. {remedy}")
    if has_skip:
        lines.append(
            "NOTE: Schedule FA was NOT generated (no --cy-input). Provide a "
            "calendar-year statement via --cy-input to include it."
        )
    lines.append(_RUN_ALL_RULE)
    for outcome in outcomes:
        location = str(outcome.output_path) if outcome.output_path is not None else "(no file)"
        lines.append(f"  {_RUN_ALL_STATUS_LABEL[outcome.status]}  {outcome.name:16}  {location}")
        if outcome.detail:
            lines.append(f"                 -> {outcome.detail}")
    if fa_price_cache_dir is not None:
        lines.append(f"  Schedule FA price cache / audit artifacts: {fa_price_cache_dir}")
    lines.append(_RUN_ALL_RULE)
    return "\n".join(lines)


def _run_all_targets(output_dir: Path, name: str) -> tuple[Path, ...]:
    """Every file ``run-all`` deletes before running (or skipping) schedule ``name``.

    BOTH format variants (.csv and .xlsx), so a failed schedule leaves no
    misleading file behind AND a prior run with a different --format can't
    leave a stale off-extension file masquerading as current output for the
    same schedule (rubber-duck P1.2 + code-review stale-output finding).
    Schedule FA also writes Table A2 to a ``-a2`` sibling; form-67 may write
    a debug CSV.
    """
    stems = (name, f"{name}-a2") if name == "schedule-fa" else (name,)
    targets = [output_dir / f"{stem}.{ext}" for stem in stems for ext in ("csv", "xlsx")]
    if name == "form-67":
        targets.append(output_dir / "form-67-debug.csv")
    return tuple(targets)


def _clear_run_all_targets(output_dir: Path, name: str) -> bool:
    """Delete every target of schedule ``name``; whether any was there.

    Only a missing target counts as absent: any other ``OSError`` raises
    :class:`OutputRemovalError` naming the target and its errno name (see
    :func:`_run_all_clearing_error`).
    """
    removed = False
    for target in _run_all_targets(output_dir, name):
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise OutputRemovalError(
                _filesystem_problem("could not clear the stale output target", exc, target)
            ) from exc
        removed = True
    return removed


def _run_all_clearing_error(name: str, exc: OutputRemovalError) -> tuple[_RunAllOutcome, int]:
    """Schedule ``name``'s error outcome when its targets could not be cleared."""
    return (
        _RunAllOutcome(
            name,
            "error",
            None,
            f"{exc}; no current output — ignore any existing files for this schedule",
        ),
        _EXIT_DOMAIN_ERROR,
    )


def _skip_schedule_fa(output_dir: Path) -> tuple[_RunAllOutcome, int]:
    """Schedule FA's outcome without ``--cy-input``: skipped, but its targets
    are still cleared, so an earlier run's Schedule FA can't sit beside this
    run's files (#738). A target that can't be cleared fails the leg, as it
    does for every other schedule.
    """
    try:
        removed = _clear_run_all_targets(output_dir, "schedule-fa")
    except OutputRemovalError as exc:
        return _run_all_clearing_error("schedule-fa", exc)
    detail = "not generated: --cy-input not provided (Schedule FA needs a calendar-year statement)"
    if removed:
        detail += "; an earlier Schedule FA output was removed"
    return _RunAllOutcome("schedule-fa", "skipped", None, detail), _EXIT_OK


def _withheld_form67_path(report: _RunAllReport | None) -> Path | None:
    """The review CSV a Form 67 step wrote in place of the withheld official
    file (#601), or ``None``."""
    if isinstance(report, Form67RunReport) and report.official_output_withheld:
        return report.output_path
    return None


def _run_all_manual_fill_detail(report: _RunAllReport | None) -> str:
    """What a step that exited 3 leaves to do: complete the withheld Form 67
    from its review CSV (#601), enter a fractional Rate(%) by hand after
    upload (#1447), or fill a review CSV's manual columns."""
    if _withheld_form67_path(report) is not None:
        return (
            "official Form 67 CSV withheld: complete the blank Indian tax "
            "payable and credit from this review CSV"
        )
    if isinstance(report, Form67RunReport) and report.upload_has_fractional_rate:
        return "after upload, enter each fractional Rate(%) by hand; see the Form 67 warning above"
    return "review CSV needs manual columns before filing"


def _run_all_promised_outputs(
    name: str, sub_args: argparse.Namespace, report: _RunAllReport | None
) -> tuple[Path, ...]:
    """The files schedule ``name`` must have written when it exits 0 or 3.

    Form 67 adds its debug CSV under ``--debug-form67``. Schedule FA adds
    Table A2 unless its ``report`` has no A2 rows ([account.custodian] unset:
    A2 skipped by design).
    """
    output: Path = sub_args.output
    if _withheld_form67_path(report) is not None:
        return (output.with_name("form-67-debug.csv"),)
    if name == "form-67" and sub_args.debug_form67:
        return (output, output.with_name("form-67-debug.csv"))
    a2_skipped = isinstance(report, RunReport) and not report.a2_rows
    if name == "schedule-fa" and not a2_skipped:
        return (output, fa_a2_path(output))
    return (output,)


def _remove_failed_run_all_outputs(
    outcomes: list[_RunAllOutcome], targets: Sequence[Path]
) -> tuple[list[_RunAllOutcome], str | None]:
    """All-or-nothing: once any schedule errors, remove every target, so no
    output of the run is left to file, and say so on each schedule that had
    one. Returns the outcomes, and the error naming any file it could not
    remove (``None`` if none), for the caller to print.
    """
    if not any(outcome.status == "error" for outcome in outcomes):
        return outcomes, None
    removal_error = None
    detail = "output removed because the run failed"
    try:
        remove_failed_run_outputs(targets)
    except OutputRemovalError as exc:
        removal_error = f"error: {exc}"
        detail = "the run failed: do NOT file this output (see the removal error above)"
    return [
        outcome._replace(output_path=None, detail=detail)
        if outcome.status in ("ok", "manual_fill")
        else outcome
        for outcome in outcomes
    ], removal_error


def _run_all_form67_preflight(args: argparse.Namespace, targets: Sequence[Path]) -> int | None:
    """Build the shared FSI context and refuse a REFUSED Form 67 row (#601),
    or a Schedule FSI relief the portal would overwrite (#900), before any
    schedule writes.

    Returns ``None`` to run the schedules, which inside
    :class:`_FsiReuseScope` reuse the context built here. On a refusal, or a
    domain error that leaves Form 67 readiness unknown, removes every target
    so no earlier run's output survives, and returns the exit code to stop with.
    """
    try:
        context = _compute_schedule_fsi_for_pipeline(
            input_path=args.fy_input,
            config_path=args.config,
            financial_year=args.financial_year,
            allow_masked_account=args.allow_masked_account,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
            accept_anonymised_statement=args.accept_anonymised_statement,
            report_name="Form 67 foreign-tax-credit reporting",
        )
        require_fsi_write_ready(context.fsi_result.rows, context.form67_result)
    except _DOMAIN_ERRORS as exc:
        code = _domain_error("run-all", exc, prefix="run-all: ")
    except Exception as exc:
        if _traceback_requested():
            raise
        print(
            f"error: run-all: the Form 67 readiness check crashed unexpectedly ({crash_site(exc)})",
            file=sys.stderr,
        )
        code = _EXIT_GENERIC
    else:
        return None
    try:
        remove_failed_run_outputs(targets)
    except OutputRemovalError as exc:
        print(f"error: {exc}", file=sys.stderr)
    return code


def _run_all(args: argparse.Namespace) -> int:
    """Dispatch the ``run-all`` subcommand (orchestrates all schedules).

    Refuses before touching anything if a target is an input. Then runs
    :func:`_run_all_schedules` in one cleanup scope over every target of
    every schedule. A console write or flush that fails there, such as with
    ``BrokenPipeError`` once a pipe's reader has exited, stops the run at
    once: every target's removal is attempted, and the run exits 4 (#925).
    """
    output_dir: Path = args.output_dir
    fy_schedules: tuple[tuple[str, _RunAllStep], ...] = (
        ("schedule-os", _schedule_os_step),
        ("schedule-cg", _schedule_cg_step),
        ("schedule-cg-ltcg", _schedule_cg_ltcg_step),
        ("schedule-fsi", _schedule_fsi_step),
        ("schedule-tr", _schedule_tr_step),
        ("form-67", _form67_step),
    )
    # Each schedule's targets are deleted before it runs, Schedule FA's even
    # when it is skipped, so an input that IS a target would be gone before it
    # is read. Refuse before touching anything.
    inputs = [("--fy-input", args.fy_input), ("--config", args.config)]
    if args.cy_input is not None:
        inputs.append(("--cy-input", args.cy_input))
    targets = [
        *(target for name, _ in fy_schedules for target in _run_all_targets(output_dir, name)),
        *_run_all_targets(output_dir, "schedule-fa"),
    ]
    try:
        refuse_input_collisions(targets, inputs)
    except OutputCollisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_DOMAIN_ERROR
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        problem = _filesystem_problem("could not create --output-dir", exc, output_dir)
        print(f"error: {problem}", file=sys.stderr)
        return _EXIT_DOMAIN_ERROR
    try:
        with remove_outputs_on_failure(targets):
            return _run_all_schedules(args, fy_schedules, targets)
    except _DOMAIN_ERRORS as exc:
        return _all_command_refusal("run-all", exc)


def _run_all_schedules(
    args: argparse.Namespace,
    fy_schedules: tuple[tuple[str, _RunAllStep], ...],
    targets: Sequence[Path],
) -> int:
    """Run every schedule of :func:`_run_all`, even after one fails, so the
    summary shows every error. If any schedule fails (see
    :func:`_invoke_run_all_handler`), removes every target of every schedule
    it ran, so a failed run leaves no output to file. Schedule FA skipped
    without ``--cy-input`` is no failure, but its targets are still cleared
    (see :func:`_skip_schedule_fa`).
    """
    output_dir: Path = args.output_dir
    extension = "xlsx" if args.format_id == "xlsx" else "csv"
    substitutes = command_formats("run-all").substitutes
    calendar_year = args.financial_year.start_year
    fa_price_cache_dir = args.price_cache_dir or (output_dir / DEFAULT_PRICE_CACHE_DIR_NAME)

    outcomes: list[_RunAllOutcome] = []
    codes: list[int] = []
    # Every schedule's report, for the global advisories printed once before
    # the summary and for its "Before you file" checklist.
    reports: list[_RunAllReport] = []

    # schedule-fsi, schedule-tr and form-67 share one FSI build (#609).
    with _FsiReuseScope():
        if (code := _run_all_form67_preflight(args, targets)) is not None:
            return code
        for name, step in fy_schedules:
            schedule_format = substitutes.get((Subcommand(name), args.format_id), args.format_id)
            schedule_extension = "xlsx" if schedule_format == "xlsx" else "csv"
            output_path = output_dir / f"{name}.{schedule_extension}"
            print(f"\n{_RUN_ALL_RULE}\n  run-all: {name}\n{_RUN_ALL_RULE}", flush=True)
            sub_args = _build_run_all_sub_args(
                input_path=args.fy_input,
                output_path=output_path,
                config_path=args.config,
                format_id=schedule_format,
                itr_form=args.itr_form,
                financial_year=args.financial_year,
                calendar_year=calendar_year,
                quiet=args.quiet,
                allow_masked_account=args.allow_masked_account,
                accept_anonymised_statement=args.accept_anonymised_statement,
                suppress_cross_ay_warning=args.suppress_cross_ay_warning,
                no_fetch_prices=args.no_fetch_prices,
                price_cache_dir=fa_price_cache_dir,
                debug_form67=args.debug_form67,
            )
            outcome, code = _invoke_run_all_handler(name, step, sub_args, output_path, reports)
            outcomes.append(outcome)
            codes.append(code)

    if args.cy_input is not None:
        output_path = output_dir / f"schedule-fa.{extension}"
        print(f"\n{_RUN_ALL_RULE}\n  run-all: schedule-fa\n{_RUN_ALL_RULE}", flush=True)
        sub_args = _build_run_all_sub_args(
            input_path=args.cy_input,
            output_path=output_path,
            config_path=args.config,
            format_id=args.format_id,
            itr_form=args.itr_form,
            financial_year=args.financial_year,
            calendar_year=calendar_year,
            quiet=args.quiet,
            allow_masked_account=args.allow_masked_account,
            accept_anonymised_statement=args.accept_anonymised_statement,
            suppress_cross_ay_warning=args.suppress_cross_ay_warning,
            no_fetch_prices=args.no_fetch_prices,
            price_cache_dir=fa_price_cache_dir,
            debug_form67=args.debug_form67,
        )
        outcome, code = _invoke_run_all_handler(
            "schedule-fa", _schedule_fa_step, sub_args, output_path, reports
        )
    else:
        outcome, code = _skip_schedule_fa(output_dir)
    outcomes.append(outcome)
    codes.append(code)

    # #178/#179: emit each global advisory the reports call for ONCE for the
    # whole run-all invocation (every step left them out of its own output, to
    # avoid repeating the same walls up to 7x). Emitted in
    # _run_all_global_advisories order (the conditional warnings, then the two
    # compliance walls) and placed BEFORE the summary so the actionable status
    # stays the last thing the user sees; still non-quietable and still fires
    # once regardless of --quiet.
    # Flush all per-schedule stdout before the final stderr block so a merged
    # (2>&1) view keeps the deduped advisories + summary genuinely last.
    sys.stdout.flush()
    # The block is rendered in full before any of it prints, and a failure to
    # render it removes every output of the run (#832).
    deduped = _run_all_global_advisories(reports)
    outcomes, removal_error = _remove_failed_run_all_outputs(outcomes, targets)
    summary = _render_run_all_summary(
        outcomes,
        financial_year=args.financial_year,
        calendar_year=calendar_year,
        fa_price_cache_dir=fa_price_cache_dir if args.cy_input is not None else None,
        pre_filing_actions=_pre_filing_actions(reports),
        removal_failed=removal_error is not None,
    )
    if deduped:
        print("\n" + "\n\n".join(deduped), file=sys.stderr)
    if removal_error is not None:
        print(removal_error, file=sys.stderr)
    print(summary, file=sys.stderr)
    return _aggregate_run_all_exit_code(codes)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the desired process exit code.

    A domain error that escapes a command goes through :func:`_domain_error`
    and exits 4 (#1049). Any other exception prints only its class and
    innermost ``ibkr_fa`` source line and exits 1.
    A console that fails while that line prints is settled (see
    :func:`_settle_broken_console`), keeping the exit code. With
    ``IBKR_FA_TRACEBACK=1``, the exception propagates instead, so Python prints
    its full traceback, which may contain input values.

    Args:
        argv: Argument list, EXCLUDING the program name. When
            :data:`None` (the default), reads from :data:`sys.argv`.
            Tests pass an explicit list to avoid touching
            ``sys.argv``.

    Returns:
        Process exit code per the module-level codes table.
    """
    try:
        return _main(argv)
    except Exception as exc:
        if _traceback_requested():
            raise
        if isinstance(exc, _DOMAIN_ERRORS):
            code = _EXIT_DOMAIN_ERROR
            with contextlib.suppress(OSError):
                _domain_error(_PROG_NAME, exc)
        else:
            code = _EXIT_GENERIC
            with contextlib.suppress(OSError):
                print(f"error: internal error ({crash_site(exc)})", file=sys.stderr)
        _settle_broken_console()
        return code


def _main(argv: Sequence[str] | None) -> int:  # noqa: PLR0911, PLR0912
    """:func:`main` without its handling of escaped exceptions."""
    try:
        parser = _build_parser()
    except OutputCatalogError as exc:
        # The --format choices come from the output catalog (#790).
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_DOMAIN_ERROR
    args = parser.parse_args(argv)
    if args.command == "schedule-fa":
        return _run_schedule_fa(args)
    if args.command == "netbenefits-fa":
        return _run_netbenefits_fa(args)
    if args.command == "netbenefits-income":
        return _run_netbenefits_income(args)
    if args.command == "fidelity-cma-income":
        return _run_fidelity_cma_income(args)
    if args.command == "fidelity-cma-cg":
        return _run_fidelity_cma_cg(args)
    if args.command == "fidelity-cma-fa":
        return _run_fidelity_cma_fa(args)
    if args.command == "netbenefits-cg":
        return _run_netbenefits_cg(args)
    if args.command == "netbenefits-all":
        return _run_netbenefits_all(args)
    if args.command == "stockplan-income":
        return _stockplan_exit(_run_stockplan_income(args))
    if args.command == "stockplan-fa":
        return _stockplan_exit(_run_stockplan_fa(args))
    if args.command == "stockplan-all":
        return _stockplan_exit(_run_stockplan_all(args))
    if args.command == "robinhood-cg":
        return _run_robinhood_cg(args)
    if args.command == "robinhood-income":
        return _run_robinhood_income(args)
    if args.command == "robinhood-fa":
        return _run_robinhood_fa(args)
    if args.command == "robinhood-options":
        return _run_robinhood_options(args)
    if args.command == "robinhood-all":
        return _run_robinhood_all(args)
    if args.command == "schedule-os":
        return _run_schedule_os(args)
    if args.command == "schedule-cg":
        return _run_schedule_cg(args)
    if args.command == "schedule-cg-ltcg":
        return _run_schedule_cg_ltcg(args)
    if args.command == "schedule-fsi":
        return _run_schedule_fsi(args)
    if args.command == "schedule-tr":
        return _run_schedule_tr(args)
    if args.command == "form-67":
        return _run_form67(args)
    if args.command == "run-all":
        return _run_all(args)
    if args.command == "init":
        return _run_init(args)
    if args.command == "merge":
        return _run_merge(args)
    # argparse with required=True on the subparser makes the unknown
    # branch unreachable; parser.error() never returns (raises SystemExit).
    parser.error(f"unknown command: {args.command!r}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
