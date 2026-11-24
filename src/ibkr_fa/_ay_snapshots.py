"""Per-AY snapshot selection shared by the FX-citation and portal-labels packages.

:mod:`ibkr_fa.fx.policy_citations` and :mod:`ibkr_fa.output.portal_labels`
each keep a registry of per-AY snapshot modules keyed by AY start year
(``_SNAPSHOTS_BY_AY_START``). Both select a snapshot by the same rule,
written here once:

* an AY with its own registered snapshot gets that snapshot;
* the first AY after the latest registered one gets the latest
  snapshot (the forward-compat fallback);
* any other AY has no snapshot.

The +1 cap is deliberate: a 5-year-old gap is not a silently-OK case,
it's a "you forgot to add the snapshot" bug.

Each package passes its own registry at call time and raises its own
``UnknownAssessmentYearError`` when :func:`snapshot_for` returns
``None``. This module imports neither package, so the two stay
independent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType


def snapshot_for(
    registry: Mapping[int, ModuleType], assessment_year_start: int
) -> ModuleType | None:
    """Return the snapshot for ``assessment_year_start``, or ``None`` if none applies.

    The exact registered match, else the latest registered snapshot when
    :func:`is_forward_compat_fallback` holds, else ``None``.
    """
    snapshot = registry.get(assessment_year_start)
    if snapshot is not None:
        return snapshot
    if is_forward_compat_fallback(registry, assessment_year_start):
        return registry[max(registry)]
    return None


def is_forward_compat_fallback(
    registry: Mapping[int, ModuleType], assessment_year_start: int
) -> bool:
    """Return ``True`` iff ``assessment_year_start`` falls back to the latest snapshot.

    That is, it has no registered snapshot of its own and is exactly one
    year after the latest registered AY.
    """
    if assessment_year_start in registry:
        return False
    return assessment_year_start == max(registry) + 1
