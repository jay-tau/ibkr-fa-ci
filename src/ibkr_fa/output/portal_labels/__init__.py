"""ITR-2/ITR-3 portal labels — current-AY surface + per-AY selector.

Single entry point for every portal-facing label, constraint, and
format helper the writers consume. This package replaces the v1
flat-file layout (``output/portal_labels.py`` +
``output/schedule_os_portal_labels.py`` +
``output/schedule_cg_portal_labels.py``) with one
**per-AY snapshot** file (``_ay_YYYY_YY.py``) per supported
Assessment Year + a thin selector here, which applies the selection
rule in :mod:`ibkr_fa._ay_snapshots` (shared with
:mod:`ibkr_fa.fx.policy_citations`) to this package's registry.

Why per-AY snapshots (issue #65):

* CBDT MAY rename or renumber portal cells from one AY to the next.
  Pinning the labels per-AY in dedicated snapshot files keeps the
  diff for a label change concentrated in ONE file, preserving the
  prior AY's audit trail unchanged.
* The current-AY default at module load (e.g.,
  ``from ibkr_fa.output.portal_labels import
  PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND``) keeps existing call
  sites working without per-call AY threading. Tests and callers
  that need a specific AY use :func:`for_ay`.
* The maintainer script ``scripts/rollover_portal_labels.py``
  (issues #66, #686) scaffolds the next AY's snapshot from the
  previous one with a TODO checklist header.

Public surface:

* Module-level re-exports of every label / format helper from the
  CURRENT AY's snapshot (currently :mod:`_ay_2026_27`). New
  callers should import from this package, not from the per-AY
  module directly.
* :data:`CURRENT_AY_START` — the AY this package defaults to
  (e.g., ``2026`` for AY 2026-27).
* :func:`for_ay` — explicit per-AY accessor: given an AY start
  year, returns the snapshot module to read constants / call
  helpers from. Used by tests that pin behaviour across AYs and
  by the future per-CY pipeline-driven AY selection.
"""

from __future__ import annotations

from types import ModuleType
from typing import Final

from ibkr_fa import _ay_snapshots
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.output.portal_labels import _ay_2026_27

# --- Current-AY default ---------------------------------------------------
# Bump this constant + add the new snapshot module when CBDT publishes
# new ITR utilities. The :func:`for_ay` selector picks the
# right snapshot based on the requested AY; the module-level re-
# exports below default to whichever AY is current.
CURRENT_AY_START: Final[int] = 2026

# Map of supported AY start years (int) to snapshot modules. Each new
# snapshot file added to this package must be registered here.
_SNAPSHOTS_BY_AY_START: Final[dict[int, ModuleType]] = {
    2026: _ay_2026_27,
}


def for_ay(assessment_year_start: int) -> ModuleType:
    """Return the portal-labels snapshot module for the given AY.

    Args:
        assessment_year_start: 4-digit starting year of the AY
            (e.g., ``2026`` for AY 2026-27).

    Returns:
        The :class:`~types.ModuleType` whose attributes are the
        portal labels + format helpers verified for that AY. Read
        constants and call format helpers off the returned module
        (e.g., ``for_ay(2026).PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND``).

    Raises:
        UnknownAssessmentYearError: If no snapshot is registered
            for ``assessment_year_start`` (and no fallback rule
            covers it). Forward-compatibility rule: when the
            requested AY is the FIRST AY AFTER the latest
            registered snapshot, the latest registered snapshot is
            returned (no error) — this lets calendar-year 2026
            pipelines (AY 2027-28) continue to work against the
            AY 2026-27 labels until CBDT publishes a schema change
            and someone adds the next snapshot. Older AYs (before
            the earliest registered snapshot) always raise. The
            rule is :func:`ibkr_fa._ay_snapshots.snapshot_for`.

    See also:
        :func:`is_forward_compat_fallback` — silent predicate that
        returns ``True`` iff calling :func:`for_ay` with the same
        AY would return the latest snapshot via the +1 forward-
        compat fallback (rather than via an exact registered
        match). Callers that want to surface a "labels not yet
        verified for this AY" warning should consult this
        predicate, not catch a swallowed exception.
    """
    snapshot = _ay_snapshots.snapshot_for(_SNAPSHOTS_BY_AY_START, assessment_year_start)
    if snapshot is not None:
        return snapshot
    raise UnknownAssessmentYearError(
        assessment_year_start=assessment_year_start,
        earliest_registered=min(_SNAPSHOTS_BY_AY_START),
        latest_registered=max(_SNAPSHOTS_BY_AY_START),
    )


def is_forward_compat_fallback(assessment_year_start: int) -> bool:
    """Return ``True`` iff :func:`for_ay` would fall back to the latest snapshot.

    Use this from CLI / RunReport code to surface a "the requested
    AY has no verified portal-labels snapshot; falling back to the
    most recent verified AY" warning to the user. The predicate is
    silent (no logging side effects) and never raises:
    out-of-range AYs that would raise :class:`UnknownAssessmentYearError`
    in :func:`for_ay` simply return ``False`` here — they are
    "unsupported", not "fallback".

    Args:
        assessment_year_start: 4-digit AY start year to inspect
            (e.g., ``2027`` for AY 2027-28).

    Returns:
        ``True`` iff ``assessment_year_start`` is exactly one year
        after the latest registered snapshot (so :func:`for_ay`
        would return the latest snapshot as a forward-compat
        fallback rather than via an exact registered match).
        ``False`` for any AY that has its own registered snapshot,
        AND for any AY too far in the past or future for the
        +1-only fallback rule. The rule is
        :func:`ibkr_fa._ay_snapshots.is_forward_compat_fallback`.
    """
    return _ay_snapshots.is_forward_compat_fallback(_SNAPSHOTS_BY_AY_START, assessment_year_start)


class UnknownAssessmentYearError(IbkrFaError, LookupError):
    """No portal-labels snapshot is registered for the requested AY.

    Raised by :func:`for_ay` when ``assessment_year_start`` falls
    outside the supported range (older than the earliest registered
    snapshot, OR more than one AY after the latest). The actionable
    fix is to add the corresponding ``_ay_YYYY_YY.py`` snapshot file
    and register it in :data:`_SNAPSHOTS_BY_AY_START` — the
    ``scripts/rollover_portal_labels.py`` maintainer script
    (issue #66) scaffolds the file.

    Inherits from both :class:`~ibkr_fa.exceptions.IbkrFaError`
    (so the CLI's domain-error handler catches it cleanly instead
    of escaping with a traceback) and :class:`LookupError` (so
    legacy ``except LookupError`` call sites — primarily tests —
    keep working).
    """

    def __init__(
        self,
        *,
        assessment_year_start: int,
        earliest_registered: int,
        latest_registered: int,
    ) -> None:
        self.assessment_year_start = assessment_year_start
        self.earliest_registered = earliest_registered
        self.latest_registered = latest_registered
        super().__init__(
            f"No portal-labels snapshot registered for AY "
            f"{assessment_year_start}-{(assessment_year_start + 1) % 100:02d}. "
            f"Supported AYs: {earliest_registered}-{(earliest_registered + 1) % 100:02d} "
            f"through {latest_registered}-{(latest_registered + 1) % 100:02d} "
            f"(plus one-year forward fallback to the latest). "
            f"To add a new AY, create "
            f"src/ibkr_fa/output/portal_labels/_ay_"
            f"{assessment_year_start}_{(assessment_year_start + 1) % 100:02d}.py "
            f"and register it in _SNAPSHOTS_BY_AY_START."
        )


# --- Current-AY re-exports ------------------------------------------------
# Re-export every public name from the current AY's snapshot so legacy
# call sites like ``from ibkr_fa.output.portal_labels import
# PORTAL_COLUMN_HEADERS_FA_A3`` keep working unchanged. New per-AY-
# aware callers should use :func:`for_ay` instead.
_CURRENT = _SNAPSHOTS_BY_AY_START[CURRENT_AY_START]

PORTAL_COLUMN_HEADERS_FA_A3 = _CURRENT.PORTAL_COLUMN_HEADERS_FA_A3
PORTAL_COLUMN_HEADERS_FA_A2 = _CURRENT.PORTAL_COLUMN_HEADERS_FA_A2
PORTAL_DATE_FORMAT = _CURRENT.PORTAL_DATE_FORMAT
PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND = _CURRENT.PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND
PORTAL_FIELD_LABEL_OS_SECTION_1_ORDINARY_DIVIDEND = (
    _CURRENT.PORTAL_FIELD_LABEL_OS_SECTION_1_ORDINARY_DIVIDEND
)
PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST = (
    _CURRENT.PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST
)
PORTAL_FIELD_LABEL_OS_SECTION_1_ANY_OTHER_INCOME = (
    _CURRENT.PORTAL_FIELD_LABEL_OS_SECTION_1_ANY_OTHER_INCOME
)
PORTAL_COLUMN_HEADERS_OS_SECTION_10 = _CURRENT.PORTAL_COLUMN_HEADERS_OS_SECTION_10
PORTAL_SECTION_LABEL_OS_SECTION_1 = _CURRENT.PORTAL_SECTION_LABEL_OS_SECTION_1
PORTAL_SECTION_LABEL_OS_SECTION_10 = _CURRENT.PORTAL_SECTION_LABEL_OS_SECTION_10
PORTAL_ROW_LABEL_OS_SECTION_10_ORDINARY_DIVIDEND = (
    _CURRENT.PORTAL_ROW_LABEL_OS_SECTION_10_ORDINARY_DIVIDEND
)
PORTAL_FIELD_LABEL_OS_OTHER_INTEREST = _CURRENT.PORTAL_FIELD_LABEL_OS_OTHER_INTEREST
PORTAL_FIELD_LABEL_CG_FOREIGN_STCG = _CURRENT.PORTAL_FIELD_LABEL_CG_FOREIGN_STCG
PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG = _CURRENT.PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG
PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM = (
    _CURRENT.PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM
)
PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG_BY_ITR_FORM = (
    _CURRENT.PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG_BY_ITR_FORM
)
PORTAL_SECTION_REF_CG_FOREIGN_STCG_BY_ITR_FORM = (
    _CURRENT.PORTAL_SECTION_REF_CG_FOREIGN_STCG_BY_ITR_FORM
)
PORTAL_SECTION_REF_CG_FOREIGN_LTCG_BY_ITR_FORM = (
    _CURRENT.PORTAL_SECTION_REF_CG_FOREIGN_LTCG_BY_ITR_FORM
)
PORTAL_COLUMN_HEADERS_CG_TABLE_F = _CURRENT.PORTAL_COLUMN_HEADERS_CG_TABLE_F
PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM = _CURRENT.PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM
PORTAL_ROW_LABEL_CG_TABLE_F_LTCG_BY_ITR_FORM = _CURRENT.PORTAL_ROW_LABEL_CG_TABLE_F_LTCG_BY_ITR_FORM
PORTAL_COLUMN_HEADERS_FSI = _CURRENT.PORTAL_COLUMN_HEADERS_FSI
PORTAL_COLUMN_HEADERS_TR = _CURRENT.PORTAL_COLUMN_HEADERS_TR
PORTAL_COLUMN_HEADERS_TR_BY_ITR_FORM = _CURRENT.PORTAL_COLUMN_HEADERS_TR_BY_ITR_FORM
PORTAL_COLUMN_HEADERS_FORM67 = _CURRENT.PORTAL_COLUMN_HEADERS_FORM67
format_country_for_portal = _CURRENT.format_country_for_portal
format_date_for_portal = _CURRENT.format_date_for_portal
format_amount_for_portal = _CURRENT.format_amount_for_portal
require_fa_country_code_supported = _CURRENT.require_fa_country_code_supported


__all__ = [
    "CURRENT_AY_START",
    "PORTAL_COLUMN_HEADERS_CG_TABLE_F",
    "PORTAL_COLUMN_HEADERS_FA_A2",
    "PORTAL_COLUMN_HEADERS_FA_A3",
    "PORTAL_COLUMN_HEADERS_FORM67",
    "PORTAL_COLUMN_HEADERS_FSI",
    "PORTAL_COLUMN_HEADERS_OS_SECTION_10",
    "PORTAL_COLUMN_HEADERS_TR",
    "PORTAL_COLUMN_HEADERS_TR_BY_ITR_FORM",
    "PORTAL_DATE_FORMAT",
    "PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG",
    "PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG_BY_ITR_FORM",
    "PORTAL_FIELD_LABEL_CG_FOREIGN_STCG",
    "PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM",
    "PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND",
    "PORTAL_FIELD_LABEL_OS_OTHER_INTEREST",
    "PORTAL_FIELD_LABEL_OS_SECTION_1_ANY_OTHER_INCOME",
    "PORTAL_FIELD_LABEL_OS_SECTION_1_ORDINARY_DIVIDEND",
    "PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST",
    "PORTAL_ROW_LABEL_CG_TABLE_F_LTCG_BY_ITR_FORM",
    "PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM",
    "PORTAL_ROW_LABEL_OS_SECTION_10_ORDINARY_DIVIDEND",
    "PORTAL_SECTION_LABEL_OS_SECTION_1",
    "PORTAL_SECTION_LABEL_OS_SECTION_10",
    "PORTAL_SECTION_REF_CG_FOREIGN_LTCG_BY_ITR_FORM",
    "PORTAL_SECTION_REF_CG_FOREIGN_STCG_BY_ITR_FORM",
    "UnknownAssessmentYearError",
    "for_ay",
    "format_amount_for_portal",
    "format_country_for_portal",
    "format_date_for_portal",
    "is_forward_compat_fallback",
    "require_fa_country_code_supported",
]
