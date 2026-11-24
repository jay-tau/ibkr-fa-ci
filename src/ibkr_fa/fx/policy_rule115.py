"""Rule 115(1), Explanation (2)(e) FX policy: preceding-month-end TTBR for foreign dividends.

Income-tax Rules, 1962, Rule 115(1), Explanation (2)(e) prescribes that "income by way
of dividends" from a foreign source must be converted to INR using the
SBI TT-Buying rate of the **last day of the calendar month immediately
preceding the month in which the dividend is declared, distributed or
paid by the company**.

The Rule 115(1), Explanation (2)(e) dividend-preceding-month-end semantic is preserved
under the Income-tax Rules, 2026 framework — specifically by
**Rule 206(2)(b)(i) Table Sl. No. 5**, which preserves the verbatim
Rule 115(1), Explanation (2)(e) clause for dividend income ("last day of the calendar
month immediately preceding the month in which the dividend is
declared, distributed or paid by the company"). The TT-Buying rate
itself is defined in **Rule 207(3)(b)** (cross-referenced from
Rule 206(2)(a)); Rule 207(1)'s "last published rate may be taken"
fallback is TDS-side (non-residents / IFSC units) and not part of
the Schedule OS pathway. Carry-back to the most recent published
prior rate is therefore **not** in the new Rules text; ibkr-fa
retains it as CBDT FAQ / practice carried over from the 1961-Act
framework, pending primary-source verification (tracked at #126).
Press summaries claiming the 2025-Act framework switches to RBI
reference rates are inaccurate (see PR #132 Codex P2). The CLI
emits a soft cross-AY warning for runs targeting AY 2027-28 or
later so the user can independently confirm against the current
Income-tax Rules text.

IBKR exposes payment date (not declaration / record date), so this
policy uses payment date as the trigger event. For each ``event_date``
the *specified date* under the rule is::

    if event_date.month == 1:
        specified = date(event_date.year - 1, 12, 31)
    else:
        specified = date(event_date.year, event_date.month, 1) - 1 day

When SBI did not publish on the specified date (Sundays, public
holidays, archive gaps), the policy steps **backwards day by day** to
the most recent published prior date, capped at
``max_carry_forward_days``. Carry-back is always *backwards in time*:
tax rules require using a rate that was knowable on or before the
specified date. Forward extrapolation would be anachronistic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import ClassVar

from ibkr_fa.fx import policy_citations
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.fx.policy_citations import _ay_2026_27
from ibkr_fa.fx.policy_fa import DEFAULT_MAX_CARRY_FORWARD_DAYS, carry_back_rate
from ibkr_fa.fx.ttbr_loader import SANE_RATE_LOWER, SANE_RATE_UPPER, load_ttbr
from ibkr_fa.invariants import require


@dataclass(frozen=True, slots=True)
class Rule115Resolution:
    """Audit-grade resolution result for a single
    :meth:`Rule115Policy.rate_for_audited` call.

    Captures all three values an auditor needs to reproduce the lookup:
    the statutory specified date, the actual SBI publication date the
    rate was read from (== specified date when no carry-back was needed;
    an earlier published date when SBI did not publish on the specified
    date itself), and the resolved rate.

    Attributes:
        rate: The INR-per-unit-currency rate read from the SBI archive.
            Strictly positive; matches the value
            :meth:`Rule115Policy.rate_for` would return for the same
            ``event_date``.
        specified_date: The statutory specified date for the event, as
            the policy defines it: the last day of the month immediately
            preceding the event date for dividends (Rule 115(1),
            Explanation (2)(e)), capital gains ((2)(f)) and foreign tax
            (Rule 128(5)(ii)); the last day of the previous year for
            clause (c) income (:class:`Rule115OtherSourcesPolicy`).
        rate_date: The actual SBI archive date the rate was read from.
            Equals :attr:`specified_date` when SBI published on the
            specified date itself. Earlier than :attr:`specified_date`
            (by at most ``Rule115Policy.max_carry_forward_days``)
            when carry-back occurred (specified date fell on a
            Sunday / holiday / unpublished day).

    Invariants enforced in :meth:`__post_init__`:

    * ``rate > 0``
    * ``rate_date <= specified_date``
    """

    rate: Decimal
    specified_date: date
    rate_date: date

    def __post_init__(self) -> None:
        require(
            self.rate > 0,
            what="Rule115Resolution.rate must be positive",
            rate=self.rate,
            specified_date=self.specified_date,
            rate_date=self.rate_date,
        )
        require(
            self.rate_date <= self.specified_date,
            what=(
                "Rule115Resolution.rate_date must be on or before "
                "specified_date (carry-back goes strictly backwards in time)"
            ),
            specified_date=self.specified_date,
            rate_date=self.rate_date,
        )


def preceding_month_end(event_date: date) -> date:
    """Return the last day of the calendar month immediately preceding
    ``event_date``'s month. January rolls back to December 31 of the
    prior year.
    """
    return date(event_date.year, event_date.month, 1) - timedelta(days=1)


class Rule115Policy(FxPolicy):
    """Schedule OS / CG / FSI FX policy under Rule 115(1), Explanation (2)(e).

    Resolution algorithm for ``rate_for(event_date)``:

    1. Compute ``specified_date`` = last day of the calendar month
       immediately preceding ``event_date``'s month. ``January`` rolls
       to ``December`` of the prior year.
    2. If the bundled archive has a published rate for
       ``specified_date``, return it.
    3. Otherwise, find the most recent published date strictly before
       ``specified_date`` (binary search over the sorted archive).

       a. If that prior date is more than ``max_carry_forward_days``
          before ``specified_date``, raise :class:`MissingFxRateError`
          with a ``policy_kind`` discriminator naming the rule.
       b. Otherwise, return the prior rate.

    4. If ``specified_date`` predates the earliest archive row,
       raise :class:`MissingFxRateError` with ``last_available=None``.

    Args:
        max_carry_forward_days: Maximum number of days to carry back
            from the preceding-month-end. Default
            :data:`DEFAULT_MAX_CARRY_FORWARD_DAYS` (14) — covers
            typical weekend / holiday clusters around month-end.
        assessment_year_start: Optional 4-digit AY start year. When
            provided, :meth:`policy_kind_str` resolves the citation
            from the per-AY snapshot in
            :mod:`ibkr_fa.fx.policy_citations` (e.g., AY 2027 →
            ``"Rule 206(2)(b)(i) Sl. No. 5"``). When ``None``
            (default, legacy behaviour), :meth:`policy_kind_str`
            falls back to the class-level :attr:`POLICY_KIND`
            (AY 2026-27). The kwarg is plumbed through by the
            pipeline layer (issue #207); compute-layer tests and
            external callers MAY omit it and get unchanged AY
            2026-27 citations.

    Attributes:
        currency: Always ``"USD"`` for v1.
        POLICY_KIND: Short tag rendered into raised
            :class:`MissingFxRateError` instances so users can
            distinguish this policy from :class:`FaExactDatePolicy`.
            Pinned to the AY 2026-27 citation as the legacy
            default: the constant :attr:`_CITATION_ATTR` names,
            read from the AY 2026-27 snapshot
            (:mod:`ibkr_fa.fx.policy_citations._ay_2026_27`), not
            the current-AY re-exports. Per-AY resolution goes via
            :meth:`policy_kind_str` instead. Subclasses override
            this AND :attr:`_CITATION_ATTR` together.
        _CITATION_ATTR: Name of the constant on the per-AY snapshot
            module that :meth:`policy_kind_str` reads from when
            :attr:`_assessment_year_start` is non-None. Subclasses
            override this to point at their own per-AY citation
            (e.g., :class:`Rule128Policy` overrides to
            ``"POLICY_KIND_RULE_128_5II"``).
    """

    currency: ClassVar[str] = "USD"
    POLICY_KIND: ClassVar[str] = _ay_2026_27.POLICY_KIND_RULE_115_2E
    _CITATION_ATTR: ClassVar[str] = "POLICY_KIND_RULE_115_2E"

    def __init__(
        self,
        *,
        max_carry_forward_days: int = DEFAULT_MAX_CARRY_FORWARD_DAYS,
        assessment_year_start: int | None = None,
    ) -> None:
        if max_carry_forward_days < 0:
            msg = "max_carry_forward_days must be non-negative"
            raise ValueError(msg)
        self._max_days = max_carry_forward_days
        self._assessment_year_start = assessment_year_start
        rates = load_ttbr(self.currency)
        self._rates: dict[date, Decimal] = dict(rates)
        self._sorted_dates: list[date] = sorted(self._rates)
        # Lazily-populated cache: (year, month) -> preceding-month-end.
        # All same-month events resolve to the same specified date, so
        # this is at most one entry per distinct event month seen.
        self._pme_cache: dict[tuple[int, int], date] = {}

    @property
    def max_carry_forward_days(self) -> int:
        """The configured staleness ceiling (read-only)."""
        return self._max_days

    def rate_for(self, event_date: date) -> Decimal:
        return self.rate_for_audited(event_date).rate

    def rate_for_audited(self, event_date: date) -> Rule115Resolution:
        """Return both the Rule 115 rate and the SBI publication date
        the rate was read from.

        Audit-grade companion to :meth:`rate_for`. Same lookup
        algorithm (preceding-calendar-month-end → carry-back to most
        recent prior published row within
        :attr:`max_carry_forward_days`), but surfaces the actual
        publication date so callers (notably the Schedule OS review
        CSV writer) can show the auditor a date-rate pair that
        matches an actual SBI archive row, rather than the synthetic
        ``(specified_date, carried-back_rate)`` pair that
        :meth:`rate_for` alone produces.

        Args:
            event_date: The taxable-event date (e.g., dividend payment
                date). Same semantics as :meth:`rate_for`.

        Returns:
            A :class:`Rule115Resolution` carrying the resolved rate,
            the statutory specified date
            (preceding-calendar-month-end), and the actual SBI
            publication date the rate was read from.

        Raises:
            MissingFxRateError: Same conditions as :meth:`rate_for`
                (no rate within the carry-back window, or specified
                date predates the archive).
        """
        specified = self._preceding_month_end(event_date)
        rate, rate_date = carry_back_rate(
            specified,
            rates=self._rates,
            sorted_dates=self._sorted_dates,
            max_carry_forward_days=self._max_days,
            currency=self.currency,
            policy_kind=self._policy_kind_for,
        )
        self._require_sane_rate(rate, event_date=event_date)
        return Rule115Resolution(rate=rate, specified_date=specified, rate_date=rate_date)

    def _preceding_month_end(self, event_date: date) -> date:
        """Return the last calendar day of the month immediately
        preceding ``event_date``'s month.

        Memoised on ``(year, month)`` since every event in the same
        calendar month resolves to the same specified date.
        """
        key = (event_date.year, event_date.month)
        cached = self._pme_cache.get(key)
        if cached is not None:
            return cached
        pme = preceding_month_end(event_date)
        self._pme_cache[key] = pme
        return pme

    def _policy_kind_for(self) -> str:
        """The policy and event kind a :class:`MissingFxRateError` names.

        Never the event date, which is input (#1047).
        """
        return f"{self.policy_kind_str()} (preceding-month-end for a dividend)"

    def policy_kind_str(self) -> str:
        """Return the short, human-readable policy citation for this
        policy's effective Assessment Year.

        When :attr:`_assessment_year_start` is ``None`` (the
        construction-time default), returns the class-level
        :attr:`POLICY_KIND` — preserves legacy AY 2026-27 wording
        for callers that haven't yet threaded the per-AY kwarg.

        When :attr:`_assessment_year_start` is set, resolves the
        citation from
        :mod:`ibkr_fa.fx.policy_citations`' per-AY snapshot via
        :attr:`_CITATION_ATTR`. The snapshot for the requested AY
        may be the latest registered snapshot under the +1 forward-
        compat fallback rule of
        :func:`~ibkr_fa.fx.policy_citations.for_ay` — callers that
        want to detect that fallback should consult
        :func:`~ibkr_fa.fx.policy_citations.is_forward_compat_fallback`.

        Subclasses MUST NOT override this method — override the
        :attr:`POLICY_KIND` and :attr:`_CITATION_ATTR` ClassVars
        instead (parametric design intentional to prevent the
        subclass footgun where a class forgets to wire the per-AY
        lookup).
        """
        if self._assessment_year_start is None:
            return self.POLICY_KIND
        snapshot = policy_citations.for_ay(self._assessment_year_start)
        return getattr(snapshot, self._CITATION_ATTR)

    def _require_sane_rate(self, rate: Decimal, *, event_date: date) -> None:
        """Postcondition: the returned rate must fall within sane bounds.

        Mirrors :meth:`FaExactDatePolicy._require_sane_rate`; locks
        the contract against future refactors that might bypass the
        loader-level invariant (e.g., a monkeypatched cached value).

        Instance-method dispatch (NOT ``@classmethod`` / ``@staticmethod``)
        so the sanity-failure error message can render the AY-aware
        :meth:`policy_kind_str` for this instance — under
        ``assessment_year_start=2027`` an AMT sanity error
        correctly cites Rule 206 / Rule 76, not the AY 2026-27
        legacy. The :class:`Rule115OtherSourcesPolicy` subclass
        likewise inherits the AY-aware citation automatically (no
        per-subclass override needed beyond
        :attr:`_CITATION_ATTR`).
        """
        require(
            SANE_RATE_LOWER < rate <= SANE_RATE_UPPER,
            what=(
                f"{type(self).__name__} ({self.policy_kind_str()}) rate_for "
                f"postcondition: rate must be in "
                f"({SANE_RATE_LOWER}, {SANE_RATE_UPPER}]"
            ),
            rate=rate,
            event_date=event_date,
        )


class Rule115OtherSourcesPolicy(Rule115Policy):
    """FY-end FX policy under Rule 115(1), Explanation (2)(c).

    Sister class to :class:`Rule115Policy` (which is dividends-specific
    per Rule 115(1), Explanation (2)(e)). The two sub-clauses are
    **fundamentally different**, NOT just-different-citation:

    * **Rule 115(1), Explanation (2)(e)** — dividends: TTBR of the **last
      day of the calendar month immediately preceding** the month in
      which the dividend is declared / distributed / paid. Per-event
      lookup (different rate per payment date).
    * **Rule 115(1), Explanation (2)(c)** — ordinary income from house
      property, profits and gains of business or profession, and
      residual other sources: TTBR of **the last day of the previous
      year of the assessee**. A single FX rate applies throughout the
      FY; an individual credit or disposal date does not change it.

    For Indian-resident taxpayers (the only scope ibkr-fa targets),
    "previous year" per Section 3 of the Income-tax Act is the
    financial year being reported. So for FY 2025-26 (AY 2026-27),
    the specified date is **March 31, 2026**, and every interest
    credit in FY 2025-26 — whether earned in April 2025 or March
    2026 — is converted at the March 31, 2026 SBI TTBR.

    Verified against Indian Kanoon's authoritative text of
    Rule 115 (https://indiankanoon.org/doc/34257484/) — T5 codex P1
    finding on PR #188 round 2 caught the original (incorrect)
    citation that this class previously cited (2)(f), which is
    actually capital gains, and that this class previously inherited
    the per-event preceding-month-end logic from
    :class:`Rule115Policy` which is dividend-specific.

    Implementation: the class overrides :meth:`_preceding_month_end`
    to return the fixed FY-end date (the ``previous_year_end``
    constructor arg) regardless of ``event_date``, so the inherited
    :meth:`~Rule115Policy.rate_for_audited` / :meth:`rate_for`
    machinery — including the carry-back algorithm + SBI TTBR
    archive + sanity bounds — applies the right specified date.
    The misleadingly-named ``_preceding_month_end`` is kept (not
    renamed) so the override stays unambiguous to a future reader
    diffing the two policy classes.

    Used by
    :func:`~ibkr_fa.compute.schedule_os_interest.compute_schedule_os_interest_lines`
    for Schedule OS row 1b(ix), and by source adapters that expose
    manual business-income amounts.
    """

    POLICY_KIND: ClassVar[str] = _ay_2026_27.POLICY_KIND_RULE_115_EXPLANATION_2C
    _CITATION_ATTR: ClassVar[str] = "POLICY_KIND_RULE_115_EXPLANATION_2C"

    def __init__(
        self,
        *,
        previous_year_end: date,
        max_carry_forward_days: int = DEFAULT_MAX_CARRY_FORWARD_DAYS,
        assessment_year_start: int | None = None,
    ) -> None:
        """Construct a Rule 115(1), Explanation (2)(c) FX policy for an FY.

        Args:
            previous_year_end: The last day of the assessee's
                previous year — for an Indian individual taxpayer
                this is the last day of the financial year being
                reported (e.g., ``date(2026, 3, 31)`` for FY
                2025-26 / AY 2026-27). All interest credits in
                that FY are FX-converted at the SBI TTBR for this
                specified date (with the standard carry-back to
                the most recent prior published rate if SBI didn't
                publish on the date itself).
            max_carry_forward_days: Maximum carry-back window (same
                semantics as :class:`Rule115Policy`).
            assessment_year_start: Optional 4-digit AY start year
                threaded through to the parent for per-AY citation
                resolution via :meth:`policy_kind_str`. Default
                ``None`` preserves the legacy
                ``"Rule 115(1), Explanation (2)(c)"`` citation. Under
                AY 2027-28+ the citation resolves to
                ``"Rule 206(2)(b)(i) Sl. No. 3"``, which Table-
                verbatim preserves the FY-end ("last day of the tax
                year of the assessee") convention. The
                "Terminology equivalence" subsection of
                :doc:`/architecture` confirms that the
                2026-Rules "tax year" and 1961-Act "previous year"
                bind to the same calendar date (Mar 31 of the
                Indian FY) for resident-individual assessees, so
                this class's existing FY-end logic is correct
                under both regimes — no behavioural change is
                needed, only the citation refresh. See
                :mod:`ibkr_fa.fx.policy_citations._ay_2027_28` for
                the issue #252 / PR #255 correction trail (PR #253
                initially mis-cited Sl. No. 5 with a behavioural-
                change marker; #252 corrected to Sl. No. 3).
        """
        super().__init__(
            max_carry_forward_days=max_carry_forward_days,
            assessment_year_start=assessment_year_start,
        )
        self._previous_year_end = previous_year_end

    @property
    def previous_year_end(self) -> date:
        """The fixed Rule 115(1), Explanation (2)(c) specified date (read-only)."""
        return self._previous_year_end

    def _preceding_month_end(self, event_date: date) -> date:  # noqa: ARG002 — Rule115Policy hook
        """Rule 115(1), Explanation (2)(c) specified date: the last day of
        the assessee's previous year. ``event_date`` is ignored —
        all interest credits in the FY share a single FX-conversion
        date, regardless of which month they posted.

        Despite the inherited method name (kept to preserve the
        override-vs-parent diff signal for a future reader), this is
        NOT a preceding-month-end computation for this subclass.
        """
        return self._previous_year_end

    def _policy_kind_for(self) -> str:
        return (
            f"{self.policy_kind_str()} (last day of previous year "
            f"{self._previous_year_end.isoformat()} for interest received)"
        )
