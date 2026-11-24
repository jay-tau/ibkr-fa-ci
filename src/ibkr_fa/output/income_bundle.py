"""Write a source's income outputs: Schedule OS, FSI, TR and Form 67 (#646).

NetBenefits, StockPlan, Fidelity CMA and Robinhood write the same income
files, chosen by one rule. The filing files come first, then the review
(``*-debug.csv``) files, each in Schedule OS, FSI, TR, Form 67 order. The
output declaration's ``INCOME_FILING`` and ``INCOME_REVIEW`` selections name
their slots in that order (#1206); the filenames, which callers clear and
remove, are this module's own:

* A BLANK Form 67 (#601) withholds ``form-67.csv`` and writes
  ``form-67-debug.csv`` even without ``--debug``. So does a caller that
  passes ``withhold_form67``: Fidelity CMA while its FTC is blocked (#1453).
* ``--debug`` writes every review file.
* Without a Schedule OS result (Robinhood writes OS as its own leg),
  neither OS file is written.

A Schedule FSI relief the portal's read-only cell would overwrite (#900)
refuses before any file is written.

Like :func:`~ibkr_fa.output.bundle.write_output_bundle`, this does no
cleanup: the caller's window removes every target if anything raises.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final, NamedTuple

from ibkr_fa.compute.form67 import Form67ComputeResult, Form67Readiness
from ibkr_fa.compute.schedule_fsi import (
    ScheduleFsiComputeResult,
    require_fsi_relief_matches_portal,
)
from ibkr_fa.compute.schedule_os import ScheduleOsComputeResult
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult
from ibkr_fa.invariants import require
from ibkr_fa.output import INCOME_FILING, INCOME_REVIEW
from ibkr_fa.output.base import RenderContext, Subcommand
from ibkr_fa.output.bundle import spec_for, write_output_bundle


class _Output(NamedTuple):
    filename: str
    subcommand: Subcommand
    format_id: str


# Each schedule's filing and review filenames: this bundle's own inventory,
# which callers clear and remove. The declaration's selections only order the
# slots (#1206), so a selection can't drop a file from cleanup.
_FILENAMES: Final[Mapping[Subcommand, tuple[str, str]]] = {
    Subcommand.OS: ("schedule-os.csv", "schedule-os-debug.csv"),
    Subcommand.FSI: ("schedule-fsi.csv", "schedule-fsi-debug.csv"),
    Subcommand.TR: ("schedule-tr.csv", "schedule-tr-debug.csv"),
    Subcommand.FORM67: ("form-67.csv", "form-67-debug.csv"),
}
_FILING: Final = tuple(_Output(_FILENAMES[sc][0], sc, f) for sc, f in INCOME_FILING)
_REVIEW: Final = tuple(_Output(_FILENAMES[sc][1], sc, f) for sc, f in INCOME_REVIEW)
INCOME_FILING_FILENAMES: Final[tuple[str, ...]] = tuple(n[0] for n in _FILENAMES.values())
INCOME_REVIEW_FILENAMES: Final[tuple[str, ...]] = tuple(n[1] for n in _FILENAMES.values())
INCOME_FILENAMES: Final[tuple[str, ...]] = (*INCOME_FILING_FILENAMES, *INCOME_REVIEW_FILENAMES)
# The FSI, TR and Form 67 names, for a source that writes Schedule OS separately.
FTC_FILENAMES: Final[tuple[str, ...]] = tuple(
    name for name in INCOME_FILENAMES if name not in _FILENAMES[Subcommand.OS]
)


def _selected(
    *, form67_readiness: Form67Readiness, withhold_form67: bool, debug: bool, schedule_os: bool
) -> tuple[_Output, ...]:
    require(
        form67_readiness is not Form67Readiness.REFUSED,
        what="a REFUSED Form 67 must be refused before any income output is chosen",
    )
    withheld = withhold_form67 or form67_readiness is Form67Readiness.BLANK
    filing = (o for o in _FILING if not (withheld and o.subcommand is Subcommand.FORM67))
    review = (o for o in _REVIEW if debug or (withheld and o.subcommand is Subcommand.FORM67))
    return tuple(o for o in (*filing, *review) if schedule_os or o.subcommand is not Subcommand.OS)


def income_output_names(*, form67_readiness: Form67Readiness, debug: bool) -> tuple[str, ...]:
    """The files a run with Schedule OS writes, in order (see the module docstring).

    Raises:
        InvariantError: If ``form67_readiness`` is REFUSED; the run refuses it first.
    """
    selected = _selected(
        form67_readiness=form67_readiness, withhold_form67=False, debug=debug, schedule_os=True
    )
    return tuple(o.filename for o in selected)


def write_income_outputs(
    *,
    schedule_os: ScheduleOsComputeResult | None,
    fsi: ScheduleFsiComputeResult,
    tr: ScheduleTrComputeResult,
    form67: Form67ComputeResult,
    output_dir: Path,
    context: RenderContext,
    debug: bool,
    withhold_form67: bool = False,
) -> tuple[Path, ...]:
    """Write the income files under ``output_dir`` with the registered writers.

    For a READY or BLANK ``form67``, ``withhold_form67=True`` writes
    ``form-67-debug.csv`` in place of ``form-67.csv``, as a BLANK Form 67 does.

    Returns:
        The written paths, in order (see the module docstring).

    Raises:
        InvariantError: If ``form67`` is REFUSED (the run refuses it first),
            or if ``fsi`` or ``tr`` carries a relief the portal would overwrite.
        Whatever a writer raises; the later files are not written.
    """
    require_fsi_relief_matches_portal(fsi.rows, form67)
    require_fsi_relief_matches_portal(tr.fsi_rows_with_overwritten_relief, form67)
    payloads = {
        Subcommand.OS: schedule_os,
        Subcommand.FSI: fsi,
        Subcommand.TR: tr,
        Subcommand.FORM67: form67,
    }
    selected = _selected(
        form67_readiness=form67.readiness,
        withhold_form67=withhold_form67,
        debug=debug,
        schedule_os=schedule_os is not None,
    )
    specs = [
        spec_for(o.subcommand, o.format_id, payloads[o.subcommand], output_dir / o.filename)
        for o in selected
    ]
    return write_output_bundle(specs, context)
