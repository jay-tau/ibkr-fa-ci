"""Compute Schedule OS 1b(ix) foreign free-cash-balance interest lines.

Sister module to :mod:`ibkr_fa.compute.schedule_os` (which handles the
dividend 1a(i) reporting). This module takes parsed
:class:`~ibkr_fa.models.InterestCredit` records (cash-balance interest
credits from the IBKR Activity Statement Interest section, #139) and
produces aggregated INR amounts ready for Schedule OS row 1b(ix).

AY 2026-27 row 1b(iv) is pass-through income/loss. IBKR free-cash-
balance interest is residual other interest and belongs in 1b(ix).

Foreign cash-balance interest paid by a foreign broker (IBKR LLC on
idle USD cash) is "income from other sources" under the residuary
clause of Section 56(1) — NOT Section 56(2)(id) ("interest on
securities" as narrowly defined by Section 2(28B) covering Government
/ debenture coupons). Taxed at the user's marginal slab rate.

For India-US DTAA purposes, IBKR LLC's USD-credit-interest paid to
an Indian-resident retail customer is **not** exempt under any
Article 11(3) sub-paragraph (those are Government / central-bank /
EXIM-Bank carve-outs only; a retail Indian resident qualifies for
**none** of them). The practical reason IBKR does not typically
withhold US tax on its USD-credit-interest is **US domestic law**:

* IRC §871(h) "portfolio interest" exemption for NRA recipients of
  most US-source interest income (including bank-deposit interest
  beneficially owned by a foreign person); AND
* IRC §871(i)(2)(A) bank-deposit-interest specific exemption for
  NRAs.

If §871(h) / §871(i) ever fail to apply (e.g., a structured-deposit
product outside the bank-deposit definition, or an NRA-treaty-status
mismatch on the W-8BEN on file), the DTAA fallback is **Article 11(2)(b)
— 15% US withholding**, and the user then claims FTC against the
Indian slab-rate liability via Schedule TR / Form 67. The compute
layer therefore does not currently model per-credit withholding for
interest; if future fixtures surface non-zero WHT on interest, the
:class:`ScheduleOsInterestLine` model can be extended with a
``wht_amount_usd`` field.

FX policy: **Rule 115(1), Explanation (2)(c)** — for "income chargeable
under the head ... 'Income from other sources' (not being income by
way of dividends and 'Interest on securities')" — uses **the SBI
TT-Buying rate of the last day of the previous year of the
assessee**. For an Indian-resident taxpayer, "previous year" per
Section 3 of the Income-tax Act is the financial year being reported,
so the specified date is Mar 31 of the FY-end year. **All interest
credits in the FY are FX-converted at that single fixed date**, not
per-credit — fundamentally different from Rule 115(1), Explanation (2)(e)
for dividends (per-event preceding-calendar-month-end).

Verified against Indian Kanoon's authoritative text of Rule 115
(https://indiankanoon.org/doc/34257484/) — T5 codex P1 finding on
PR #188 round 2 caught the original PR-188 mistake of citing
(2)(f) (capital gains) and reusing the dividend per-event lookup.

Implementation: :class:`~ibkr_fa.fx.policy_rule115.Rule115OtherSourcesPolicy`
(a thin subclass of :class:`~ibkr_fa.fx.policy_rule115.Rule115Policy`)
takes ``previous_year_end`` at construction and overrides
``_preceding_month_end`` to return that fixed date regardless of
event_date — so the inherited carry-back + SBI TTBR archive machinery
applies the right specified date. The pipeline constructs the policy
with ``previous_year_end=financial_year.end_date``; the compute
function below is unchanged from a date-handling perspective (just
passes credit_date to ``fx_policy.rate_for_audited`` which the policy
class then ignores in favor of its fixed specified date).

v1 scope (this module): per-credit INR conversion + in-FY filter +
aggregation. The aggregate total is emitted as a 1b(ix) row in every
Schedule OS output format (filing CSV and xlsx, review CSV and xlsx).
Out of scope (separate follow-up):

* Per-credit 1b(ix) detail block in the review/debug CSV. Every format
  carries the aggregate the portal cell needs; only the per-credit
  audit breakdown is still missing.
* FSI / Form 67 per-source-country interest breakdown (#35 / #37).
* Section 57 deduction for debit interest (margin interest the user
  PAID) — the Interest section parser returns debit rows as
  :class:`~ibkr_fa.models.InterestNonCredit` and nothing files them; if
  margin interest deduction is wanted, that's a separate compute path.
* Modeling per-credit US WHT (only relevant if a future export
  surfaces non-zero withholding on interest — see DTAA / §871(h)
  paragraph above).
* Flex Query interest parsing (#12) — the dispatcher returns an
  empty :class:`InterestCredit` list for Flex Query inputs, but
  :func:`~ibkr_fa.pipeline_os.run_schedule_os` already refuses Flex
  Query inputs outright via a ParseError on the format-detection
  gate (so the silent-1b(ix)=0 failure mode T1 correctness P3-3
  raised cannot actually fire today). When Flex Query becomes
  schedule-os-eligible, the Flex Query Interest parser needs to
  land in the same change so 1b(ix) is emitted from both input
  formats.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from ibkr_fa.compute.exact_inr import exact_inr_conversion, exact_inr_sum
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.policy import AuditedFxPolicy
from ibkr_fa.invariants import require
from ibkr_fa.models import InterestCredit, InterestNonCredit
from ibkr_fa.models.money import require_filed_amount

#: The fixed label of every 1b(ix) interest exactness refusal (#1136).
_INTEREST_LABEL: Final[str] = "Schedule OS 1b(ix) interest"


@dataclass(frozen=True, slots=True)
class ScheduleOsInterestLine:
    """One foreign cash-balance interest credit ready for Schedule OS 1b(ix).

    Sister to :class:`~ibkr_fa.compute.schedule_os.ScheduleOsDividendLine`
    (per-payment FX-converted record). Each instance represents one
    in-FY :class:`~ibkr_fa.models.InterestCredit` with its INR
    conversion pre-computed at full :class:`~decimal.Decimal`
    precision. Rounding (HALF_UP to whole rupees) is applied at the
    aggregate level on :attr:`ScheduleOsInterestComputeResult.total_inr_rounded`,
    not per-row.

    Attributes:
        credit_date: IBKR cash-credit date of the interest payment.
            Mirror of :attr:`InterestCredit.credit_date`.
        fx_specified_date: The Rule 115(1), Explanation (2)(c) "specified
            date" — the last day of the assessee's previous year
            (i.e., the FY-end date being reported, ``Mar 31`` of the
            FY-end year for an Indian individual taxpayer). The same
            date for EVERY credit in the FY, regardless of when in
            the FY the credit was earned. This is the **fundamental
            difference from dividends** (Rule 115(1), Explanation (2)(e)
            uses a per-event preceding-calendar-month-end of the
            payment date). Can be before, equal to, OR after
            :attr:`credit_date` (an Apr 2025 credit in FY 2025-26
            has a Mar 31, 2026 specified date, which is AFTER the
            credit date — that's expected per the rule).
        fx_rate_publication_date: The SBI archive date the rate
            actually came from. Equals :attr:`fx_specified_date` when
            SBI published on the statutory specified date itself;
            strictly earlier when carry-back resolved a weekend /
            holiday / unpublished day to the most recent prior
            published row. Always
            ``<= fx_specified_date`` regardless of
            :attr:`credit_date`.
        fx_rate_inr_per_usd: The INR-per-USD rate actually applied
            for this credit. Strictly positive. Same value for every
            credit in the FY (since :attr:`fx_specified_date` is
            shared) unless an instance is constructed outside the
            production pipeline path.
        amount_gross_usd: Gross interest credit in USD (mirror of
            :attr:`InterestCredit.amount_gross`). Strictly positive
            (the Interest parser returns every other non-footer row as an
            :class:`~ibkr_fa.models.InterestNonCredit`, so it never
            reaches this compute layer).
        amount_gross_inr: ``amount_gross_usd * fx_rate_inr_per_usd``
            at full :class:`~decimal.Decimal` precision. NOT rounded
            here — the aggregate-level
            :attr:`ScheduleOsInterestComputeResult.total_inr_rounded`
            applies CBDT-FAQ HALF_UP rounding once at the end so the
            per-row Decimals can sum to a precise total before
            rounding.
        description: Verbatim audit copy of
            :attr:`InterestCredit.description` (e.g., ``"USD Credit
            Interest for Apr-2025"``).

    Invariants enforced in :meth:`__post_init__`:

    * ``amount_gross_usd > 0``
    * ``fx_rate_inr_per_usd > 0``
    * ``amount_gross_inr > 0``, rounding HALF_UP within the CBDT 14-digit
      bound: the filed 1b(ix) total adds it up (#830)
    * ``fx_rate_publication_date <= fx_specified_date`` (carry-back
      goes strictly backwards in time)

    Note: NO ``fx_specified_date < credit_date`` invariant. Unlike
    the dividend sister (where preceding-month-end is always strictly
    before the payment date by construction), Rule 115(1), Explanation
    (2)(c) uses a fixed FY-end specified date which can be before,
    equal to, or after any given credit_date. T5 codex P1 finding on
    PR #188 round 2 caught the original code's incorrect use of the
    dividend (2)(e) per-event lookup for interest.
    """

    credit_date: date
    fx_specified_date: date
    fx_rate_publication_date: date
    fx_rate_inr_per_usd: Decimal
    amount_gross_usd: Decimal
    amount_gross_inr: Decimal
    description: str

    def __post_init__(self) -> None:
        require(
            self.amount_gross_usd > 0,
            what="ScheduleOsInterestLine.amount_gross_usd must be positive",
            amount_gross_usd=self.amount_gross_usd,
            credit_date=self.credit_date,
        )
        require(
            self.fx_rate_inr_per_usd > 0,
            what="ScheduleOsInterestLine.fx_rate_inr_per_usd must be positive",
            fx_rate_inr_per_usd=self.fx_rate_inr_per_usd,
            credit_date=self.credit_date,
        )
        require(
            self.amount_gross_inr > 0,
            what="ScheduleOsInterestLine.amount_gross_inr must be positive",
            amount_gross_inr=self.amount_gross_inr,
            credit_date=self.credit_date,
        )
        require_filed_amount(
            self.amount_gross_inr,
            what="ScheduleOsInterestLine.amount_gross_inr",
            credit_date=self.credit_date,
        )
        require(
            self.fx_rate_publication_date <= self.fx_specified_date,
            what=(
                "ScheduleOsInterestLine.fx_rate_publication_date must be on "
                "or before fx_specified_date (carry-back goes strictly "
                "backwards in time; equal when no carry-back occurred)"
            ),
            fx_rate_publication_date=self.fx_rate_publication_date,
            fx_specified_date=self.fx_specified_date,
        )
        require(
            self.description != "",
            what="ScheduleOsInterestLine.description must be non-empty (audit trail)",
        )


@dataclass(frozen=True, slots=True)
class ScheduleOsInterestComputeResult:
    """Result of the Schedule OS 1b(ix) interest compute stage.

    Attributes:
        lines: In-FY interest credit lines, sorted by
            ``credit_date``, with Rule 115(1), Explanation (2)(c) FX
            conversion already applied (single FY-end date for every
            credit in the FY). Becomes the audit-block detail of a
            future Schedule OS 1b(ix) review CSV; for v1 it's
            primarily a vehicle for the aggregate
            :attr:`total_inr_rounded` and per-row
            :attr:`ScheduleOsInterestLine.fx_rate_publication_date`
            for auditor traceability.
        total_inr_full: Sum of per-line :attr:`ScheduleOsInterestLine.amount_gross_inr`
            at full :class:`~decimal.Decimal` precision (NO rounding
            applied). Used as the input to the CBDT-FAQ HALF_UP
            rounding in :attr:`total_inr_rounded`.
        total_inr_rounded: ``total_inr_full`` quantized to whole
            rupees via :data:`~decimal.ROUND_HALF_UP`. This is the
            value for ITR-2 Schedule OS cell 1b(ix) ("Others including
            interest from Companies, NBFCs & HFCs"). It is EMITTED as its
            own row in the Schedule OS filing CSV — Schedule FSI reports
            the combined foreign income, so a worksheet carrying only the
            1a(i) dividend would leave the two irreconcilable. The CLI
            advisory repeats it for convenience only.
        excluded_out_of_fy_count: Number of :class:`InterestCredit`
            records whose :attr:`InterestCredit.credit_date` fell
            outside ``[financial_year_start, financial_year_end]``
            and was silently filtered. Surfaced for the CLI advisory
            so the user knows their statement spanned periods other
            than the requested FY (common when their IBKR Activity
            Statement is a calendar-year export, not an FY export).
    """

    lines: list[ScheduleOsInterestLine] = field(default_factory=list)
    total_inr_full: Decimal = Decimal("0")
    total_inr_rounded: Decimal = Decimal("0")
    excluded_out_of_fy_count: int = 0

    def __post_init__(self) -> None:
        require(
            self.excluded_out_of_fy_count >= 0,
            what=("ScheduleOsInterestComputeResult.excluded_out_of_fy_count must be non-negative"),
            excluded_out_of_fy_count=self.excluded_out_of_fy_count,
        )
        require(
            self.total_inr_full >= Decimal("0"),
            what="ScheduleOsInterestComputeResult.total_inr_full must be non-negative",
            total_inr_full=str(self.total_inr_full),
        )
        require(
            self.total_inr_rounded >= Decimal("0"),
            what="ScheduleOsInterestComputeResult.total_inr_rounded must be non-negative",
            total_inr_rounded=str(self.total_inr_rounded),
        )


def compute_schedule_os_interest_lines(
    interest_credits: Iterable[InterestCredit],
    *,
    financial_year_start: date,
    financial_year_end: date,
    fx_policy: AuditedFxPolicy,
) -> ScheduleOsInterestComputeResult:
    """Compute the Schedule OS 1b(ix) interest lines for an FY.

    Args:
        interest_credits: All parsed :class:`InterestCredit` records
            (typically from the dispatcher's tuple). Outside-FY
            credits are silently filtered (a multi-period IBKR
            Activity Statement may emit interest credits from
            before ``financial_year_start`` or after
            ``financial_year_end``); in-FY credits are FX-converted
            via ``fx_policy`` and collected into
            :attr:`ScheduleOsInterestComputeResult.lines`. A generator
            argument is materialised to a list before any precondition
            check so the iterator is never exhausted by the
            precondition pass.
        financial_year_start: First day of the Indian FY being
            reported (e.g., ``date(2025, 4, 1)`` for FY 2025-26).
        financial_year_end: Last day of the Indian FY being reported
            (e.g., ``date(2026, 3, 31)`` for FY 2025-26).
        fx_policy: The :class:`~ibkr_fa.fx.policy.AuditedFxPolicy`
            to use for per-credit FX conversion. v1 production uses
            :class:`~ibkr_fa.fx.policy_rule115.Rule115OtherSourcesPolicy`
            (Rule 115(1), Explanation **(2)(c)** — "income from other
            sources, not being dividends or 'Interest on securities'"),
            which is **fundamentally different** from the
            :class:`~ibkr_fa.fx.policy_rule115.Rule115Policy` used for
            dividends (Explanation (2)(e), preceding-calendar-month-end
            of the payment date). For interest, the specified date is
            **fixed at the last day of the assessee's previous year**
            (FY-end for an Indian individual) and is therefore the
            same for every in-FY credit; whereas for dividends each
            credit gets a different per-event preceding-month-end
            rate. Direct callers / future integration tests MUST wire
            ``Rule115OtherSourcesPolicy(previous_year_end=...)`` for
            interest — wiring the dividend ``Rule115Policy`` here will
            silently produce the wrong INR total whenever SBI TTBR on
            a credit's preceding month-end differs from TTBR on the
            FY-end date (T5 codex P1 finding on PR #188 round 2).
            ``rate_for_audited`` is called once per credit, and the
            line takes its rate, ``fx_specified_date`` and
            :attr:`ScheduleOsInterestLine.fx_rate_publication_date`
            (the actual TTBR publication date, carry-back-aware) from
            that one resolution.

    Returns:
        :class:`ScheduleOsInterestComputeResult` with one line per
        in-FY credit and the aggregate rounded INR total ready for
        ITR-2 Schedule OS cell 1b(ix).

    Raises:
        InvariantError: If any input :class:`InterestCredit` has
            ``currency != "USD"``. v1 supports USD only; multi-
            currency tracking is at #52. Defense-in-depth — the
            parser at ``parser/activity_statement/interest.py``
            already gates this, so reaching this check indicates a
            parser bypass or a future test/direct caller constructing
            a non-USD InterestCredit (T5 codex P2 finding on PR #188:
            without this guard the USD-only Rule115Policy rate would
            be silently applied to e.g. an EUR credit and the
            resulting INR total would silently mis-classify the
            non-USD amount as USD-equivalent INR, corrupting the
            Schedule OS 1b(ix) cell).
        MissingFxRateError: If a credit date lacks a Rule 115 rate
            within the carry-back window (delegated from the
            ``fx_policy.rate_for*`` call).
    """
    require(
        financial_year_start <= financial_year_end,
        what=(
            "compute_schedule_os_interest_lines: financial_year_start must "
            "be on or before financial_year_end"
        ),
        financial_year_start=financial_year_start,
        financial_year_end=financial_year_end,
    )
    materialised = list(interest_credits)

    # Defense-in-depth (T5 codex P2 finding on PR #188): mirror the
    # non-USD guard in compute_schedule_os_dividend_lines. The parser
    # already gates this, so reaching here indicates a parser bypass
    # or schema drift. Multi-currency support is tracked at #52.
    non_usd = [c for c in materialised if c.currency != "USD"]
    require(
        not non_usd,
        what=(
            "compute_schedule_os_interest_lines: v1 supports USD only; "
            "multi-currency support is tracked at #52. The parser at "
            "parser/activity_statement/interest.py already gates this, "
            "so reaching this check indicates a parser bypass or schema "
            "drift. Silently applying a USD-only Rule115Policy rate to "
            "a non-USD credit would mis-classify the amount in the "
            "Schedule OS 1b(ix) INR total."
        ),
        non_usd_count=len(non_usd),
        non_usd_currencies=tuple(sorted({c.currency for c in non_usd})),
    )

    in_fy: list[InterestCredit] = []
    excluded_out_of_fy_count = 0
    for credit in materialised:
        if financial_year_start <= credit.credit_date <= financial_year_end:
            in_fy.append(credit)
        else:
            excluded_out_of_fy_count += 1

    # Sort by credit_date for deterministic output ordering (mirror
    # of ScheduleOsDividendLine sort order). Stable sort preserves
    # original IBKR row order when two credits share a date.
    in_fy.sort(key=lambda c: c.credit_date)

    lines: list[ScheduleOsInterestLine] = []
    for credit in in_fy:
        resolution = fx_policy.rate_for_audited(credit.credit_date)
        rate = resolution.rate
        amount_inr = exact_inr_conversion(
            credit.amount_gross,
            rate,
            label=_INTEREST_LABEL,
            source_row_index=credit.source_row_index,
        )
        lines.append(
            ScheduleOsInterestLine(
                credit_date=credit.credit_date,
                fx_specified_date=resolution.specified_date,
                fx_rate_publication_date=resolution.rate_date,
                fx_rate_inr_per_usd=rate,
                amount_gross_usd=credit.amount_gross,
                amount_gross_inr=amount_inr,
                description=credit.description,
            )
        )

    total_inr_full = exact_inr_sum((ln.amount_gross_inr for ln in lines), label=_INTEREST_LABEL)
    total_inr_rounded = total_inr_full.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    return ScheduleOsInterestComputeResult(
        lines=lines,
        total_inr_full=total_inr_full,
        total_inr_rounded=total_inr_rounded,
        excluded_out_of_fy_count=excluded_out_of_fy_count,
    )


class UnprovenInterestError(IbkrFaError):
    """An Interest row that may change filed interest but proves no amount (#905)."""


def require_no_unproven_interest(non_credits: Sequence[InterestNonCredit]) -> None:
    """Refuse when any of ``non_credits`` is unproven; debit interest passes.

    An accrual-labelled row, a negative correction or a positive row without
    IBKR's credit-interest wording can change the interest the user received,
    but the statement doesn't show by how much, so filing the credits alone
    would guess. The check ignores dates: a correction
    outside the year can still correct a credit inside it.

    Raises:
        UnprovenInterestError: Naming each unproven row's file line and why
            it's unproven.
    """
    unproven = [row for row in non_credits if row.group.unproven]
    if unproven:
        raise UnprovenInterestError(
            "the Activity Statement's Interest section has row(s) that may change "
            "the interest you received but don't prove by how much: "
            + "; ".join(row.describe() for row in unproven)
            + ". The run refuses rather than guess the interest. Only a positive "
            "row whose description reads exactly '<CCY> Credit Interest for "
            "<Mon>-<YYYY>' counts as an interest credit, and only a negative "
            "'Debit Interest' row is left out as margin interest you paid."
        )
