"""Tests for :mod:`ibkr_fa.output.portal_labels` per-AY package surface.

Issue #65: portal labels are now organised into per-AY snapshot files
(``_ay_YYYY_YY.py``) under :mod:`ibkr_fa.output.portal_labels`, with a
selector :func:`for_ay` for explicit per-AY access. This test module
pins the package contract:

* The current-AY default re-exports work for every legacy name.
* :func:`for_ay` returns the right snapshot for registered AYs.
* :func:`for_ay` falls back to the latest snapshot for ONE AY beyond
  (forward-compat for upcoming AYs without a published ITR-2 utility
  schema change).
* :func:`for_ay` raises a clear, actionable error for unsupported AYs.
"""

from __future__ import annotations

import pytest

from ibkr_fa.output import portal_labels
from ibkr_fa.output.portal_labels import (
    CURRENT_AY_START,
    PORTAL_COLUMN_HEADERS_CG_TABLE_F,
    PORTAL_COLUMN_HEADERS_FA_A3,
    PORTAL_DATE_FORMAT,
    PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG,
    PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG_BY_ITR_FORM,
    PORTAL_FIELD_LABEL_CG_FOREIGN_STCG,
    PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM,
    PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND,
    PORTAL_FIELD_LABEL_OS_OTHER_INTEREST,
    PORTAL_ROW_LABEL_CG_TABLE_F_LTCG_BY_ITR_FORM,
    PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM,
    PORTAL_SECTION_REF_CG_FOREIGN_LTCG_BY_ITR_FORM,
    PORTAL_SECTION_REF_CG_FOREIGN_STCG_BY_ITR_FORM,
    UnknownAssessmentYearError,
    _ay_2026_27,
    for_ay,
)

# ---- current-AY default ----------------------------------------------------


def test_current_ay_start_is_2026() -> None:
    """The default AY for v1 is 2026-27 (FY 2025-26 income; CY 2025
    events). Bumping this constant requires bumping the corresponding
    snapshot file too — the test in
    test_package_re_exports_match_current_snapshot pins that
    invariant."""
    assert CURRENT_AY_START == 2026


def test_ay_2026_27_foreign_ltcg_targets_b8e_residual() -> None:
    assert PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG == (
        "Schedule CG / B / 8(e) LTCG from sale of assets where B1 to B7 are not applicable (B8e)"
    )


def test_ay_2026_27_itr3_cg_destinations_are_full_official_labels() -> None:
    assert PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM["ITR-3"] == (
        "Schedule CG / A / 6(g) STCG on assets other than at A1 or A2 or A3 or A4 or A5 (A6g)"
    )
    assert PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG_BY_ITR_FORM["ITR-3"] == (
        "Schedule CG / B / 9(e) LTCG from sale of assets where B1 to B8 are not applicable (B9e)"
    )
    assert PORTAL_SECTION_REF_CG_FOREIGN_STCG_BY_ITR_FORM == {
        "ITR-2": "A5",
        "ITR-3": "A6",
    }
    assert PORTAL_SECTION_REF_CG_FOREIGN_LTCG_BY_ITR_FORM == {
        "ITR-2": "B8",
        "ITR-3": "B9",
    }


def test_ay_2026_27_table_f_labels_are_the_notified_forms_rows_3_and_5() -> None:
    """#1360: the five date bands and the BFLA item each row names, by form."""
    assert PORTAL_COLUMN_HEADERS_CG_TABLE_F == (
        "Upto 15/6",
        "16/6 to 15/9",
        "16/9 to 15/12",
        "16/12 to 15/3",
        "16/3 to 31/3",
    )
    assert PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM == {
        "ITR-2": (
            "Short-term capital gains taxable at applicable rates. Enter value from item 3v "
            "of schedule BFLA, if any."
        ),
        "ITR-3": (
            "Short-term capital gains taxable at applicable rates. Enter value from item "
            "5viii of schedule BFLA, if any."
        ),
    }
    assert PORTAL_ROW_LABEL_CG_TABLE_F_LTCG_BY_ITR_FORM == {
        "ITR-2": (
            "Long- term capital gains taxable at the rate of 12.5%. Enter value from item "
            "3vii of schedule BFLA, if any."
        ),
        "ITR-3": (
            "Long- term capital gains taxable at the rate of 12.5%. Enter value from item 5x "
            "of schedule BFLA, if any."
        ),
    }


def test_package_re_exports_match_current_snapshot() -> None:
    """Each top-level re-export from the package MUST be the SAME
    object as the current snapshot's attribute — not a copy. This
    guarantees a bump of :data:`CURRENT_AY_START` propagates
    everywhere without silent divergence between the package
    surface and the snapshot."""
    current = for_ay(CURRENT_AY_START)
    assert PORTAL_COLUMN_HEADERS_FA_A3 is current.PORTAL_COLUMN_HEADERS_FA_A3
    assert PORTAL_DATE_FORMAT is current.PORTAL_DATE_FORMAT
    assert (
        PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND is current.PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND
    )
    assert PORTAL_FIELD_LABEL_OS_OTHER_INTEREST is current.PORTAL_FIELD_LABEL_OS_OTHER_INTEREST
    assert PORTAL_FIELD_LABEL_CG_FOREIGN_STCG is current.PORTAL_FIELD_LABEL_CG_FOREIGN_STCG
    assert PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG is current.PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG
    assert (
        PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM
        is current.PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM
    )
    assert (
        PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG_BY_ITR_FORM
        is current.PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG_BY_ITR_FORM
    )
    assert (
        PORTAL_SECTION_REF_CG_FOREIGN_STCG_BY_ITR_FORM
        is current.PORTAL_SECTION_REF_CG_FOREIGN_STCG_BY_ITR_FORM
    )
    assert (
        PORTAL_SECTION_REF_CG_FOREIGN_LTCG_BY_ITR_FORM
        is current.PORTAL_SECTION_REF_CG_FOREIGN_LTCG_BY_ITR_FORM
    )
    assert PORTAL_COLUMN_HEADERS_CG_TABLE_F is current.PORTAL_COLUMN_HEADERS_CG_TABLE_F
    assert (
        PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM
        is current.PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM
    )
    assert (
        PORTAL_ROW_LABEL_CG_TABLE_F_LTCG_BY_ITR_FORM
        is current.PORTAL_ROW_LABEL_CG_TABLE_F_LTCG_BY_ITR_FORM
    )
    assert portal_labels.format_country_for_portal is current.format_country_for_portal
    assert portal_labels.format_date_for_portal is current.format_date_for_portal
    assert portal_labels.format_amount_for_portal is current.format_amount_for_portal


# ---- for_ay selector -------------------------------------------------------


def test_for_ay_returns_2026_27_snapshot_for_2026() -> None:
    """The AY 2026-27 snapshot is the only one registered in v1; for
    AY start year 2026 (i.e., AY 2026-27), :func:`for_ay` returns
    the :mod:`_ay_2026_27` module."""
    assert for_ay(2026) is _ay_2026_27


def test_for_ay_forward_compat_falls_back_to_latest_for_one_ay_ahead() -> None:
    """When CBDT has not yet published the next AY's ITR-2 utility
    schema, the labels typically carry forward unchanged. To unblock
    callers running against the next-year statement BEFORE someone
    has added the new snapshot, :func:`for_ay` falls back to the
    latest registered snapshot for AYs ONE year beyond. This is a
    forward-compat ergonomic — older AYs (before the earliest
    registered) always raise to surface the missing-snapshot bug."""
    assert for_ay(2027) is _ay_2026_27


def test_for_ay_raises_for_two_ays_beyond_latest() -> None:
    """A 2-year-or-more gap between the latest registered snapshot
    and the requested AY is NOT silently OK — it's a "you forgot to
    add the snapshot" bug. :func:`for_ay` raises with an actionable
    message naming the snapshot file to create."""
    with pytest.raises(UnknownAssessmentYearError) as exc:
        for_ay(2028)
    msg = str(exc.value)
    assert "AY 2028-29" in msg
    assert "_ay_2028_29.py" in msg
    assert "_SNAPSHOTS_BY_AY_START" in msg
    # The exception object exposes the start-year fields so
    # callers can format their own message.
    assert exc.value.assessment_year_start == 2028
    assert exc.value.latest_registered == 2026


def test_for_ay_raises_for_earlier_than_earliest() -> None:
    """Older AYs (before the earliest registered snapshot) raise.
    No silent fallback to a snapshot from a later AY — using
    labels from a FUTURE AY for a PAST AY would silently corrupt
    historical filings if CBDT ever renames a portal cell."""
    with pytest.raises(UnknownAssessmentYearError) as exc:
        for_ay(2020)
    assert exc.value.assessment_year_start == 2020
    assert exc.value.earliest_registered == 2026


def test_for_ay_returns_module_type() -> None:
    """The return type is a Python module; constants are read as
    attributes (e.g., ``for_ay(2026).PORTAL_DATE_FORMAT``). This
    test pins the shape so a future refactor (e.g., to a dataclass)
    has to update the test deliberately."""
    import types  # noqa: PLC0415

    snapshot = for_ay(2026)
    assert isinstance(snapshot, types.ModuleType), (
        f"for_ay must return a Python module (per #65 snapshot contract); "
        f"got {type(snapshot).__name__}"
    )
    # Sanity-check a handful of attributes the writer relies on so a
    # silent module-shape regression (e.g., empty stub) also trips.
    assert hasattr(snapshot, "PORTAL_DATE_FORMAT")
    assert hasattr(snapshot, "format_amount_for_portal")
    assert callable(snapshot.format_amount_for_portal)


# ---- package layout regression --------------------------------------------


def test_snapshots_registry_keys_match_filename_convention() -> None:
    """Every registered AY in ``_SNAPSHOTS_BY_AY_START`` must have a
    snapshot module whose ``__name__`` ends with
    ``_ay_<YYYY>_<YY>`` matching the registry key. Catches a future
    typo where someone bumps the registry but forgets to rename the
    file."""
    # pyright: ignore[reportPrivateUsage]
    for ay_start, module in portal_labels._SNAPSHOTS_BY_AY_START.items():
        expected_suffix = f"_ay_{ay_start}_{(ay_start + 1) % 100:02d}"
        assert module.__name__.endswith(expected_suffix), (
            f"Snapshot for AY start {ay_start} should live in a module "
            f"named *{expected_suffix}, got {module.__name__!r}"
        )


def test_legacy_top_level_imports_still_work() -> None:
    """Issue #65 must not break the legacy
    ``from ibkr_fa.output.portal_labels import X`` import path. This
    test pins the backward-compat surface by importing each public
    name and asserting it's the right shape."""
    # Re-import locally to verify the import statement itself
    # works (the module-level imports above already proved it
    # once, but a fresh import is a stronger regression pin).
    import importlib  # noqa: PLC0415

    pl = importlib.import_module("ibkr_fa.output.portal_labels")
    assert isinstance(pl.PORTAL_COLUMN_HEADERS_FA_A3, tuple)
    assert callable(pl.format_amount_for_portal)


# ---- is_forward_compat_fallback predicate ---------------------------------


def test_is_forward_compat_fallback_registered_ay_returns_false() -> None:
    """An AY that has its own registered snapshot is NOT a fallback —
    the writer used a verified label set, no advisory needed."""
    from ibkr_fa.output.portal_labels import is_forward_compat_fallback  # noqa: PLC0415

    assert is_forward_compat_fallback(2026) is False


def test_is_forward_compat_fallback_one_year_beyond_returns_true() -> None:
    """An AY exactly one beyond the latest registered snapshot hits
    the +1 forward-compat fallback path in :func:`for_ay`; the CLI
    needs this signal to surface a "labels not yet verified for this
    AY" warning."""
    from ibkr_fa.output.portal_labels import (  # noqa: PLC0415
        _SNAPSHOTS_BY_AY_START,
        is_forward_compat_fallback,
    )

    latest = max(_SNAPSHOTS_BY_AY_START)
    assert is_forward_compat_fallback(latest + 1) is True


def test_is_forward_compat_fallback_two_years_beyond_returns_false() -> None:
    """An AY two beyond the latest registered snapshot is OUT of
    forward-compat range — :func:`for_ay` would raise
    :class:`UnknownAssessmentYearError`, not fall back. The
    predicate stays silent (returns False) so the caller can decide
    whether to surface a different error."""
    from ibkr_fa.output.portal_labels import (  # noqa: PLC0415
        _SNAPSHOTS_BY_AY_START,
        is_forward_compat_fallback,
    )

    latest = max(_SNAPSHOTS_BY_AY_START)
    assert is_forward_compat_fallback(latest + 2) is False


def test_is_forward_compat_fallback_past_returns_false() -> None:
    """An AY earlier than the earliest registered snapshot is
    out-of-range historical — :func:`for_ay` would raise. Predicate
    returns False (no fallback)."""
    from ibkr_fa.output.portal_labels import is_forward_compat_fallback  # noqa: PLC0415

    assert is_forward_compat_fallback(2020) is False


# ---- UnknownAssessmentYearError is a domain error (T5 PR #200 P2) ----------


def test_unknown_ay_error_is_ibkr_fa_domain_error() -> None:
    """T5 codex P2 on PR #200: the CLI catches IbkrFaError to surface
    domain errors as one-line user-facing messages (no traceback).
    UnknownAssessmentYearError must inherit from IbkrFaError so an
    AY+2 request from the CLI doesn't escape with a Python
    traceback."""
    from ibkr_fa.exceptions import IbkrFaError  # noqa: PLC0415

    assert issubclass(UnknownAssessmentYearError, IbkrFaError)
    # And remains a LookupError so legacy ``except LookupError`` call
    # sites keep working.
    assert issubclass(UnknownAssessmentYearError, LookupError)
