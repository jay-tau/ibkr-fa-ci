"""Shared core that the per-schedule pipeline modules import from.

#155 split the old single-module pipeline into a module per command. Three
kinds of thing stay here:

1. **Compatibility re-exports.** Names that moved out, or that this module
   once imported for its own use, are imported back so
   ``from ibkr_fa.pipeline import *`` and ``ibkr_fa.pipeline.<name>`` keep
   working. Tests pin that surface: the wildcard-import checks in
   ``tests/test_pipeline_fa_extraction.py`` and
   ``tests/test_pipeline_os_extraction.py``, and the legacy-import checks in
   ``tests/test_pipeline_dispatch_extraction.py`` and
   ``tests/test_models_tax_year.py``. Several pipeline modules still import
   shared names through here, such as the dispatchers,
   :class:`~ibkr_fa.models.tax_year.FinancialYear` and
   ``_starting_ay_for_calendar_year``.
2. **The cross-AY FX check**, :func:`_check_fx_policy_for_ay` (below), which
   :func:`~ibkr_fa.pipeline_fa.run_fa_a3` calls.
3. **The shared run-report validators**, the ``_require_*`` functions and
   their constants. The run reports in :mod:`ibkr_fa.pipeline_fa_models`,
   :mod:`ibkr_fa.pipeline_os_models`, :mod:`ibkr_fa.pipeline_fsi_models`,
   :mod:`ibkr_fa.pipeline_tr_models`, :mod:`ibkr_fa.pipeline_form67_models`,
   :mod:`ibkr_fa.pipeline_cg` and :mod:`ibkr_fa.pipeline_cg_ltcg` call them
   from ``__post_init__``.

The rest of the policy this module once owned lives elsewhere:

- Schedule FA Table A3's entry point, :func:`~ibkr_fa.pipeline_fa.run_fa_a3`,
  is in :mod:`ibkr_fa.pipeline_fa`.
- Input-format detection (Flex Query or Activity Statement) is in
  :mod:`ibkr_fa.parser.format_detect`. The per-format parser dispatch, the
  account-number cross-check with its masked-account opt-out, and the
  required-section sets are in :mod:`ibkr_fa.pipeline_dispatch`.
- The run reports and their manual-fill flags sit with their pipelines: the
  ones item 3 lists in those modules (Schedule FA's is
  :class:`~ibkr_fa.pipeline_fa_models.RunReport`), and the other brokers'
  reports in their own pipeline modules.

**Cross-AY FX-policy verification status.** :func:`_check_fx_policy_for_ay`
maps the requested ``calendar_year`` to its assessment year and decides
whether the 1961-Act FX framework ibkr-fa implements has been
verified appropriate for that AY. CY <= 2025 (AY 2026-27 and
earlier) is verified via Section 536's transitional saving
clause. CY >= 2026 (AY 2027-28+) is governed by the Income-tax
Act, 2025 + Income-tax Rules, 2026 — under which the rate source
is preserved (Rule 206(1) + Rule 207(3)(b)), but the
Schedule-FA-specific date-selection semantics and the
carry-forward fallback across non-publication days have not yet
been verified (they rest on CBDT FAQ / practice rather than
rule text). The :class:`~ibkr_fa.pipeline_fa_models.RunReport` exposes this as
:attr:`~ibkr_fa.pipeline_fa_models.RunReport.fx_policy_appropriate_for_ay` so the CLI can surface
a verification-needed warning. See #126 for the cross-AY work.

PII helpers (``display_account_id`` + ``is_masked_account_id``) live
in :mod:`ibkr_fa.pii` — a dependency-light module any layer can
import without re-entering the pipeline. New user-visible surfaces
that handle account IDs (CLI renderers, exception ``raw_value``) MUST
route through ``display_account_id`` to avoid leaking the raw
identifier.
"""

from __future__ import annotations

import functools  # noqa: F401 — functools re-exported for wildcard-import compat
from collections.abc import (  # noqa: F401 — Callable / Iterable re-exported for wildcard
    Callable,
    Iterable,
)
from dataclasses import dataclass  # noqa: F401 — re-exported for wildcard-import compat
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal  # noqa: F401 — ROUND_HALF_UP re-exported
from pathlib import Path  # noqa: F401 — Path re-exported for wildcard-import compat
from typing import (  # noqa: F401 — Literal re-exported for wildcard-import compat
    Final,
    Literal,
)

from ibkr_fa.compliance.itr_form_selection import ItrForm  # noqa: F401 — re-exported for compat

# Compat re-imports (T5 codex P2 on PR #193 round 2): preserve the
# pre-extraction wildcard-import surface for names that were
# previously imported here (and so showed up in
# ``from ibkr_fa.pipeline import *``) but are now only used by the
# per-schedule modules. Without these, any notebook / script
# previously relying on the wildcard form to obtain
# :class:`Rule115Policy`, :class:`FaExactDatePolicy`, etc. via pipeline
# would silently break post-extraction. Eager re-imports here are
# safe (no circular dependency since these are leaf modules), and
# the wildcard import exports every public global of this module.
from ibkr_fa.compute import (  # noqa: F401 — FA helpers re-exported for wildcard
    aggregate_dividends_by_symbol_inr,
    build_fa_a3_rows,
    overlapping_lots_for_cy,
)
from ibkr_fa.compute.schedule_cg import (  # noqa: F401 — CG helpers re-exported
    ScheduleCgStcgComputeResult,
    compute_schedule_cg_stcg_lines,
)
from ibkr_fa.compute.schedule_cg_ltcg import (  # noqa: F401 — LTCG helpers re-exported
    ScheduleCgLtcgComputeResult,
    compute_schedule_cg_ltcg_lines,
)
from ibkr_fa.compute.schedule_os import compute_schedule_os_dividend_lines  # noqa: F401
from ibkr_fa.compute.schedule_os_interest import (  # noqa: F401
    compute_schedule_os_interest_lines,
)
from ibkr_fa.compute.source_coverage import (  # noqa: F401 — validators re-exported for compat
    require_calendar_year_period,
    require_financial_year_contained_in_period,
)
from ibkr_fa.config import (
    ScheduleOsIndianAdrRoutingMode,  # noqa: F401
    load_user_config,  # noqa: F401 — re-exported for wildcard-import compat
)
from ibkr_fa.exceptions import ConfigError, ParseError  # noqa: F401 — compat re-exports
from ibkr_fa.fx import Rule115OtherSourcesPolicy, Rule115Policy  # noqa: F401
from ibkr_fa.fx.policy_fa import FaExactDatePolicy  # noqa: F401 — FA helper re-exported
from ibkr_fa.fx.policy_rule115_capital_gains import (  # noqa: F401 — CG helper re-exported
    Rule115CapitalGainsPolicy,
)
from ibkr_fa.invariants import require
from ibkr_fa.models import (
    AccountInfo,  # noqa: F401 — model re-exported for wildcard-import compat
    ClosedLot,  # noqa: F401
    Dividend,  # noqa: F401
    InterestCredit,  # noqa: F401
    OpenLot,  # noqa: F401
    Side,  # noqa: F401 — Side re-exported for wildcard-import compat
    WithholdingTax,  # noqa: F401
)
from ibkr_fa.models.tax_year import (
    FIRST_AY_REQUIRING_2025_ACT_FRAMEWORK as _FIRST_AY_REQUIRING_2025_ACT_FRAMEWORK,
)
from ibkr_fa.models.tax_year import FinancialYear  # noqa: F401 — moved to models in #603
from ibkr_fa.models.tax_year import (
    starting_ay_for_calendar_year as _starting_ay_for_calendar_year,
)
from ibkr_fa.parser import (  # noqa: F401 — parser entry-points re-exported for wildcard compat
    KnownSection,
    parse_account_info,
    parse_closed_lots,
    parse_open_positions,
    split_sections,
)
from ibkr_fa.parser.activity_statement import (  # noqa: F401 — AS parser entry-points re-exported
    KnownActivitySection,
    Section,
    build_dec31_close_prices,
    build_instrument_lookup,
    parse_activity_statement_account_info,
    parse_activity_statement_dividends,
    parse_activity_statement_interest,
    parse_activity_statement_trades,
    parse_activity_statement_withholding_tax,
    split_activity_statement_sections,
    validate_reconciliation,
)
from ibkr_fa.parser.activity_statement.statement_period import (  # noqa: F401
    StatementPeriod,
    parse_statement_period,
)
from ibkr_fa.parser.format_detect import (  # noqa: F401 — IbkrCsvFormat re-exported for wildcard / direct imports
    IbkrCsvFormat,
    detect_ibkr_csv_format,
)
from ibkr_fa.pii import display_account_id, is_masked_account_id

# Dispatcher re-exports (issue #155): the two dispatchers, the account
# cross-check and the CG, FSI and OS required-section sets live in
# :mod:`ibkr_fa.pipeline_dispatch`. Per-schedule modules and tests still
# import them from here; pipeline.py doesn't use them itself (hence F401).
from ibkr_fa.pipeline_dispatch import (  # noqa: F401
    _CG_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
    _FSI_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
    _OS_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
    _cross_check_account_id,
    _dispatch_activity_statement,
    _dispatch_flex_query,
)
from ibkr_fa.types import Ticker  # noqa: F401 — Ticker re-exported for wildcard-import compat


def _check_fx_policy_for_ay(
    *,
    calendar_year: int,
    suppress_cross_ay_warning: bool,
) -> bool:
    """Decide whether the current FX policy is known-correct for the
    requested CY's AY.

    Returns:
        ``True`` if the FX policy is verified appropriate for the AY:
          * CY <= 2025: the 1961-Act framework that ibkr-fa
            implements is the verified-applicable framework for
            AY 2026-27 via Section 536(2)(c)'s transitional saving
            clause.
          * CY >= 2026 AND ``suppress_cross_ay_warning=True``: the
            user has explicitly attested they have independently
            verified the 2025-Act framework's FA-specific
            date-selection semantics and carry-forward fallback
            match ibkr-fa's behaviour (the rate source is preserved
            per Rule 206(1) + Rule 207(3)(b); see module-level
            commentary on
            :data:`_FIRST_AY_REQUIRING_2025_ACT_FRAMEWORK`).
        ``False`` if CY >= 2026 AND ``suppress_cross_ay_warning=False``
        — the underlying rate source is identical to ibkr-fa's
        existing implementation, so the remaining unverified
        components are the FA-specific date-selection semantics and
        the carry-forward fallback across non-publication days
        (neither has Rule 206 carry-forward fallback text; both rest on CBDT FAQ /
        practice). The CLI surfaces the resulting
        :attr:`~ibkr_fa.pipeline_fa_models.RunReport.fx_policy_appropriate_for_ay` as a warning
        so the user can verify before filing. The pipeline still
        proceeds.

    Does NOT raise: the earlier (PR #132 initial) behaviour of
    raising ConfigError was too aggressive given the GitHub Codex
    review's finding that the 2025-Act framework preserves the rate
    source. The signal is now informational, not blocking.
    """
    starting_ay = _starting_ay_for_calendar_year(calendar_year)
    if starting_ay < _FIRST_AY_REQUIRING_2025_ACT_FRAMEWORK:
        # No flag effect: pre-2027 AYs are governed by the 1961 Act
        # which ibkr-fa implements correctly. The flag is documented
        # as a no-op here.
        return True
    return suppress_cross_ay_warning


# #141: only Name + Account are user-hideable in IBKR's Activity
# Statement export configurator. Account Type / Base Currency are
# always emitted, so they are NOT in the fallback contract. This
# constant pins the closed set so every RunReport's invariant check
# refuses any other field name in identity_supplied_from_fallback.
_VALID_IDENTITY_FALLBACK_FIELD_NAMES: Final[frozenset[str]] = frozenset({"Name", "Account"})


def _require_identity_fallback_subset(
    *, run_report_class_name: str, identity_supplied_from_fallback: frozenset[str]
) -> None:
    """Shared invariant for every RunReport's ``__post_init__``.

    The ``identity_supplied_from_fallback`` field is a subset of
    :data:`_VALID_IDENTITY_FALLBACK_FIELD_NAMES` ({Name, Account}).
    Any other value would indicate a parser-pipeline contract drift
    where the dispatcher's fallback set leaked an unexpected field
    name through to the report layer.
    """
    extras = identity_supplied_from_fallback - _VALID_IDENTITY_FALLBACK_FIELD_NAMES
    require(
        not extras,
        what=(
            f"{run_report_class_name}.identity_supplied_from_fallback must be "
            f"a subset of {{'Name', 'Account'}} (only those two Activity "
            f"Statement fields are user-hideable per #141); got unexpected "
            f"field name(s)."
        ),
        unexpected_field_names=sorted(extras),
    )


def _require_masked_account_id_if_unchecked(
    *, run_report_class_name: str, account_id: str, account_id_cross_checked: bool
) -> None:
    """Shared invariant for every RunReport's ``__post_init__``.

    If the account cross-check was SKIPPED, the only legitimate
    reason is a masked account ID. Constructing the report with
    ``account_id_cross_checked=False`` AND an unmasked ID would mean
    the safety net was silently bypassed.
    """
    if not account_id_cross_checked:
        require(
            is_masked_account_id(account_id),
            what=(
                f"{run_report_class_name}: account_id_cross_checked=False is "
                "only legitimate when account_id matches the IBKR mask "
                "pattern (^U\\*+$). Otherwise the safety net was "
                "bypassed without justification."
            ),
            account_id=display_account_id(account_id),
        )


def _require_financial_year_end(
    *, run_report_class_name: str, financial_year_start: date, financial_year_end: date
) -> None:
    """Shared invariant for every FY RunReport's ``__post_init__``.

    ``financial_year_end`` is one year after ``financial_year_start``,
    less one day.
    """
    expected_end = date(
        financial_year_start.year + 1,
        financial_year_start.month,
        financial_year_start.day,
    ) - timedelta(days=1)
    require(
        financial_year_end == expected_end,
        what=(
            f"{run_report_class_name}: financial_year_end must equal "
            "financial_year_start + 1 year - 1 day (Apr 1 → Mar 31 invariant)"
        ),
        financial_year_start=str(financial_year_start),
        financial_year_end=str(financial_year_end),
        expected_end=str(expected_end),
    )


# Issue #65: lower bound for the portal-labels-AY invariant, the same
# 1900 as FinancialYear's start-year bound.
_PLAUSIBLE_MIN_AY_START: Final[int] = 1900


def _require_plausible_assessment_year_start(
    *, run_report_class_name: str, portal_labels_assessment_year_start: int
) -> None:
    """Shared invariant for every RunReport's ``__post_init__``.

    Issue #65: ``portal_labels_assessment_year_start`` is at least
    1900, matching FinancialYear's start-year lower bound.
    """
    require(
        portal_labels_assessment_year_start >= _PLAUSIBLE_MIN_AY_START,
        what=(
            f"{run_report_class_name}: portal_labels_assessment_year_start must be "
            f"a plausible 4-digit AY start year (>= {_PLAUSIBLE_MIN_AY_START})"
        ),
        portal_labels_assessment_year_start=portal_labels_assessment_year_start,
    )


def _require_manual_fill_flags_need_rows(
    *,
    run_report_class_name: str,
    rows_label: str,
    rows_written: int,
    has_foreign_tax_without_taxpayer_id: bool,
    has_unresolved_indian_tax_payable: bool,
) -> None:
    """Shared invariant for the Schedule FSI and TR RunReports' ``__post_init__``.

    Both manual-fill flags describe a written row, so neither can be
    ``True`` when the run wrote no ``rows_label`` rows.
    """
    require(
        rows_written > 0 or not has_foreign_tax_without_taxpayer_id,
        what=(
            f"{run_report_class_name}: has_foreign_tax_without_taxpayer_id "
            f"cannot be True when no {rows_label} rows were written"
        ),
        rows_written=rows_written,
        has_foreign_tax_without_taxpayer_id=has_foreign_tax_without_taxpayer_id,
    )
    require(
        rows_written > 0 or not has_unresolved_indian_tax_payable,
        what=(
            f"{run_report_class_name}: has_unresolved_indian_tax_payable "
            f"cannot be True when no {rows_label} rows were written"
        ),
        rows_written=rows_written,
        has_unresolved_indian_tax_payable=has_unresolved_indian_tax_payable,
    )


# The counts the shared FSI compute step returns, in the order the
# Schedule FSI, TR and Form 67 RunReports check them.
_FSI_COMPUTE_COUNT_NAMES: Final[tuple[str, ...]] = (
    "os_dividend_rows_consumed",
    "os_interest_rows_consumed",
    "cg_stcg_rows_consumed",
    "cg_ltcg_rows_consumed",
    "wht_rows_in_fy",
    "excluded_cg_indian_source_count",
    "excluded_cg_out_of_fy_count",
    "excluded_ltcg_pre_cutover_count",
    "excluded_ltcg_indian_source_count",
    "excluded_ltcg_out_of_fy_count",
    "interest_credits_out_of_fy_count",
)


def _require_fsi_compute_counts_non_negative(
    *, run_report_class_name: str, run_report: object, leading_count_names: tuple[str, ...] = ()
) -> None:
    """Shared invariant for the Schedule FSI, TR and Form 67 RunReports' ``__post_init__``.

    Each of ``run_report``'s ``leading_count_names`` fields, then each
    of its :data:`_FSI_COMPUTE_COUNT_NAMES` fields, in that order, is
    at least 0.
    """
    for count_name in (*leading_count_names, *_FSI_COMPUTE_COUNT_NAMES):
        count_value = getattr(run_report, count_name)
        require(
            count_value >= 0,
            what=f"{run_report_class_name}: {count_name} must be >= 0",
            **{count_name: count_value},
        )
