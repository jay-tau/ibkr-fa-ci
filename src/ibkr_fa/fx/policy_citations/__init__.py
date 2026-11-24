"""FX policy citation strings — current-AY surface + per-AY selector.

Single entry point for every short, human-readable FX-policy citation
(e.g., ``"Rule 115(1), Explanation (2)(e)"`` under AY 2026-27 vs.
``"Rule 206(2)(b)(i) Sl. No. 5"`` under AY 2027-28+). Each
:class:`~ibkr_fa.fx.policy.FxPolicy` subclass surfaces these strings
via :class:`~ibkr_fa.exceptions.MissingFxRateError` and audit-trail
:meth:`~ibkr_fa.fx.policy_rule115.Rule115Policy._policy_kind_for`
rendering.

This package mirrors :mod:`ibkr_fa.output.portal_labels`: one
**per-AY snapshot** file (``_ay_YYYY_YY.py``) per supported Assessment
Year + a thin selector here, which applies the selection rule in
:mod:`ibkr_fa._ay_snapshots` (shared with the portal-labels package) to
this package's registry.

Why per-AY snapshots (issue #207, mirroring issue #65's portal_labels
pattern):

* The 1961 Act's Rule 115 / Rule 128 citations are obsolete under
  AY 2027-28+ which operates under the Income-tax Rules, 2026
  framework (Rule 206 / 207 / 76 absorptions). Pinning the citations
  per-AY in dedicated snapshot files keeps the diff for a citation
  change concentrated in ONE file, preserving the prior AY's audit
  trail unchanged.
* :class:`~ibkr_fa.fx.policy.FxPolicy` subclasses accept an
  ``assessment_year_start`` constructor kwarg and resolve their
  per-AY citation via :func:`for_ay` at audit-trail render time.
  Legacy callers that omit the kwarg fall through to the
  :attr:`POLICY_KIND` class-attribute default (AY 2026-27).

Public surface:

* :func:`for_ay` — explicit per-AY accessor: given an AY start year,
  returns the snapshot module to read citation constants from.
* :func:`is_forward_compat_fallback` — silent predicate that returns
  ``True`` iff :func:`for_ay` would fall back to the latest snapshot.
* :class:`UnknownAssessmentYearError` — raised by :func:`for_ay` for
  out-of-range AYs.
* :data:`CURRENT_AY_START` — the AY this package considers current
  for back-compat re-exports.
* Module-level re-exports of every citation constant from the CURRENT
  AY's snapshot, for callers that don't (yet) thread the AY explicitly.
"""

from __future__ import annotations

from types import ModuleType
from typing import Final

from ibkr_fa import _ay_snapshots
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.policy_citations import _ay_2026_27, _ay_2027_28

# --- Current-AY default ---------------------------------------------------
# Bump this constant + add the new snapshot module when CBDT publishes
# a new Rules amendment that touches FX conventions. The :func:`for_ay`
# selector picks the right snapshot based on the requested AY; the
# module-level re-exports below default to whichever AY is current.
#
# NOTE: this is deliberately AY 2026-27 (NOT AY 2027-28), even though
# both snapshots are registered. The current-AY default governs legacy
# callers that read the module-level constants directly (no AY kwarg
# threaded). Switching the default would silently re-cite Rule 206 for
# anyone NOT yet passing an explicit AY — a regression for the
# AY 2026-27 filing season that is the active in-flight workload as of
# this package's introduction.
CURRENT_AY_START: Final[int] = 2026

# Map of supported AY start years (int) to snapshot modules. Each new
# snapshot file added to this package must be registered here.
_SNAPSHOTS_BY_AY_START: Final[dict[int, ModuleType]] = {
    2026: _ay_2026_27,
    2027: _ay_2027_28,
}


def for_ay(assessment_year_start: int) -> ModuleType:
    """Return the FX policy citation snapshot module for the given AY.

    Args:
        assessment_year_start: 4-digit starting year of the AY
            (e.g., ``2026`` for AY 2026-27, ``2027`` for AY 2027-28).

    Returns:
        The :class:`~types.ModuleType` whose attributes are the
        citation constants verified for that AY. Read constants off
        the returned module (e.g.,
        ``for_ay(2027).POLICY_KIND_RULE_115_2E``).

    Raises:
        UnknownAssessmentYearError: If no snapshot is registered
            for ``assessment_year_start`` (and no fallback rule
            covers it). Forward-compatibility rule: when the
            requested AY is the FIRST AY AFTER the latest
            registered snapshot, the latest registered snapshot is
            returned (no error) — this lets a calendar-year jump-
            ahead pipeline continue to work against the most recent
            registered AY's citations until CBDT publishes a new
            Rules amendment and someone adds the next snapshot.
            Older AYs (before the earliest registered snapshot)
            always raise. The rule is
            :func:`ibkr_fa._ay_snapshots.snapshot_for`.

    See also:
        :func:`is_forward_compat_fallback` — silent predicate that
        returns ``True`` iff calling :func:`for_ay` with the same
        AY would return the latest snapshot via the +1 forward-
        compat fallback (rather than via an exact registered
        match). Callers that want to surface a "FX citations not
        yet verified for this AY" warning should consult this
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
    AY has no verified FX-citation snapshot; falling back to the
    most recent verified AY" warning to the user. The predicate is
    silent (no logging side effects) and never raises:
    out-of-range AYs that would raise :class:`UnknownAssessmentYearError`
    in :func:`for_ay` simply return ``False`` here — they are
    "unsupported", not "fallback".

    Args:
        assessment_year_start: 4-digit AY start year to inspect.

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


def latest_registered_ay() -> int:
    """Return the latest AY start year with a registered snapshot.

    Helper for CLI advisory rendering — used to populate the
    dynamic "Using AY ``<latest>`` FX citations as forward-compat
    fallback for AY ``<requested>``" message text without callers
    needing to know about the internal ``_SNAPSHOTS_BY_AY_START``
    dict.
    """
    return max(_SNAPSHOTS_BY_AY_START)


class UnknownAssessmentYearError(IbkrFaError, LookupError):
    """No FX-citation snapshot is registered for the requested AY.

    Raised by :func:`for_ay` when ``assessment_year_start`` falls
    outside the supported range (older than the earliest registered
    snapshot, OR more than one AY after the latest). The actionable
    fix is to add the corresponding ``_ay_YYYY_YY.py`` snapshot file
    in this package and register it in :data:`_SNAPSHOTS_BY_AY_START`.

    Inherits from both :class:`~ibkr_fa.exceptions.IbkrFaError` (so
    the CLI's domain-error handler catches it cleanly instead of
    escaping with a traceback) and :class:`LookupError` (so legacy
    ``except LookupError`` call sites keep working).

    Distinct from
    :class:`ibkr_fa.output.portal_labels.UnknownAssessmentYearError`
    — the two packages cover different concerns (portal-cell labels
    vs. FX policy citations) and should not be cross-imported.
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
            f"No FX-citation snapshot registered for AY "
            f"{assessment_year_start}-{(assessment_year_start + 1) % 100:02d}. "
            f"Supported AYs: {earliest_registered}-{(earliest_registered + 1) % 100:02d} "
            f"through {latest_registered}-{(latest_registered + 1) % 100:02d} "
            f"(plus one-year forward fallback to the latest). "
            f"To add a new AY, create "
            f"src/ibkr_fa/fx/policy_citations/_ay_"
            f"{assessment_year_start}_{(assessment_year_start + 1) % 100:02d}.py "
            f"and register it in _SNAPSHOTS_BY_AY_START."
        )


# --- Current-AY re-exports ------------------------------------------------
# Re-export every citation constant from the current AY's snapshot so
# legacy call sites that don't (yet) thread ``assessment_year_start``
# keep working unchanged. New per-AY-aware callers should use
# :func:`for_ay` instead.
_CURRENT = _SNAPSHOTS_BY_AY_START[CURRENT_AY_START]

POLICY_KIND_RULE_115_2E = _CURRENT.POLICY_KIND_RULE_115_2E
POLICY_KIND_RULE_115_2F = _CURRENT.POLICY_KIND_RULE_115_2F
POLICY_KIND_RULE_115_EXPLANATION_2C = _CURRENT.POLICY_KIND_RULE_115_EXPLANATION_2C
POLICY_KIND_RULE_128_5II = _CURRENT.POLICY_KIND_RULE_128_5II


__all__ = [
    "CURRENT_AY_START",
    "POLICY_KIND_RULE_115_2E",
    "POLICY_KIND_RULE_115_2F",
    "POLICY_KIND_RULE_115_EXPLANATION_2C",
    "POLICY_KIND_RULE_128_5II",
    "UnknownAssessmentYearError",
    "for_ay",
    "is_forward_compat_fallback",
    "latest_registered_ay",
]
