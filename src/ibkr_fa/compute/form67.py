"""Aggregate Form 67 entries per (country, source-of-income) from WHT + OS.

PR (post-#220 closure) step 1/N of #37 introduces the Form 67
compute layer. Form 67 is filed via the e-filing portal's Form 67
service per Rule 128(9) of the Income-tax Rules, 1962 (post-2022-
amendment filing windows: for original / belated returns under
§139(1) / §139(4), on or before the end of the relevant assessment
year provided the ITR has been filed within that time limit; for
updated returns under §139(8A), on or before the date on which the
updated return is itself furnished; pre-2022, Form 67 had to be
filed on or before the §139(1) due date), and carries the Foreign
Tax Credit claim under §90 / §90A / §91 of the Income-tax Act,
1961. Schedule TR's "section under which relief claimed" cross-
references Form 67's per-(country, source) credit allocations.

Granularity (CRITICAL — see
:mod:`ibkr_fa.models.form67_entry` for the verbatim PDF column
list):

Form 67 is **per-(country, source-of-income)** — NOT per-event.
Ten dividend payments from the United States in one financial year
aggregate to ONE Form 67 row with summed income + summed tax.
PR #220's per-event design was structurally wrong and was closed
unmerged after T5 codex verification against the live PDF.

v1 scope (this aggregator):

* **Source coverage**: Dividends-section WHT only
  (source_of_income = :attr:`~ibkr_fa.models.form67_entry.Form67SourceOfIncome.DIVIDEND`).
  CG / interest WHT requires source-kind tagging on the
  :class:`~ibkr_fa.models.WithholdingTax` model (tracked as a
  follow-up).
* **DTAA article + rate (cols 9 + 10)**: populated from the
  vendored DTAA reference table at
  :mod:`ibkr_fa.reference.dtaa_dividend` per the tri-state
  lookup contract — DTAA-vendored countries populate cols 9 + 10
  (withholding article + portfolio dividend cap rate), no_dtaa-
  vendored countries leave both None (the §91 unilateral pathway
  has no DTAA article to cite), and unvendored countries also
  leave both None (the explicit "unknown" branch — the compute
  layer MUST NOT default to either §90 or §91 because silently
  choosing the wrong branch would mis-attribute relief). Added
  in step 4b/N of #37.
* **Indian tax payable normal (col 7)**: populated only from an exact
  per-source override. Broker data cannot establish the taxpayer's
  Indian tax attributable to foreign income, so every source without
  an override remains blank.
* **§90 / §90A credit + total credit (cols 11 + 13)**: computed
  via Rule 128(5)(i) when BOTH the DTAA tri-state lookup yields
  ``applicability=DTAA`` (so cols 9 + 10 are populated) AND
  col 7 is resolved (so the Indian-tax-cap input is known).
  Source-currency income and tax determine whether the tax is within
  the treaty rate; this avoids comparing Rule-128-converted tax with
  Rule-115-converted income on different FX dates. The test runs per
  dividend payment, before refunds (#890, see
  :func:`_decide_treaty_rate`), and is recorded once on
  :attr:`Form67Entry.treaty_rate_status` (#767). Within-rate tax is
  converted under Rule 128 and then capped by Indian tax payable. An
  above-rate payment retains the legacy INR treaty-cap fallback, for
  that payment alone, until per-event eligible-tax conversion is
  modelled. With no source-currency rate, or withholding that can't be
  tested payment by payment (``UNPROVEN``), eligible tax and the credit
  stay blank, and readiness refuses the row if col 7 is set. The
  eligible amount is stored on
  :attr:`Form67Entry.eligible_foreign_tax_inr` for every ``WITHIN`` or
  ``ABOVE`` entry, even when col 7 is blank. Added in step 4c/N of #37.
* **§91 unilateral credit + total credit (cols 12 + 13)**: computed
  per the §91 Explanation of the Income-tax Act, 1961 when ALL
  THREE gates are met: (a) DTAA tri-state lookup yields
  ``applicability=NO_DTAA`` (so the §91 pathway is structurally
  applicable — §90 mutex would reject a §91 credit on a DTAA
  country, and unvendored "unknown" countries are conservatively
  excluded); (b) ``Form67Config.section_91`` is supplied (i.e.,
  the user has filled the ``[form67.section_91]`` table with
  ``total_indian_tax_before_91_relief_inr`` +
  ``total_income_inr``); (c) col 7 is resolved (audit-consistency
  with the §90 pathway). The credit is the exact rational
  ``min(foreign_tax_inr, total_indian_tax * income_inr / total_income)``,
  proven to file as its exact whole-rupee HALF_UP value (#1177, see
  :mod:`ibkr_fa.compute.section_91`).
  When any gate is not met, cols 12 + 13 stay ``None``. §90 and
  §91 are mutually exclusive by construction (the DTAA-
  applicability gate routes each country to at most one branch);
  ``total_credit_inr`` picks the populated branch. Added in step
  4d/N of #37.
* **Tax payable special §115JB / JC (col 8)**: not computed; the
  engine always sets it to ``None`` (typically not relevant to
  individual investors). The writers still render a supplied value.
* **Currency**: single-currency v1 limitation matches the FSI
  WHT aggregator (USD only); multi-currency tracked at #52.

WHT ⇄ OS-dividend matching (per-country, NOT per-event):

For each in-FY paid WHT row (negative amount), shared with Schedule
FSI through :func:`~ibkr_fa.compute.foreign_tax.net_foreign_tax_by_country`:

1. Aggregate by source country (ISIN[:2]) → per-country tax in
   foreign currency.
2. Apply Rule 128(5)(ii) FX per-event (preceding-month-end SBI
   TTBR, carry-back-aware in
   :class:`~ibkr_fa.fx.policy_rule128.Rule128Policy`)
   to derive the per-event INR contribution before summing — so
   the per-country total carries event-correct FX conversion.
3. NET against in-FY refund / reversal WHT rows for the same
   country. Drop the country if the netted total is non-positive.

For each country with positive netted WHT:

* Aggregate the matching dividend income from
  :func:`~ibkr_fa.compute.schedule_os.compute_schedule_os_dividend_lines`
  results — sum :attr:`ScheduleOsDividendLine.amount_gross_inr`
  for all lines whose ISIN prefix matches the country. The OS
  layer has already applied Rule 115(1), Explanation (2)(e) FX, so the result is
  audit-consistent with Schedule OS 1a(i) / Schedule FSI.
* Derive the INR audit rate ``(foreign_tax_inr / income_inr) * 100``.
  Column 6 uses the source-currency withholding rate when proven,
  otherwise the INR audit rate. The source rate snaps to the DTAA rate
  only when the treaty-rate test finds no payment above that rate by more
  than the payment's own tolerance (#1448).
* HARD-FAIL via :class:`~ibkr_fa.invariants.InvariantError` if a
  country has positive WHT but ZERO matching OS dividend
  income — silently emitting a zero-income entry would
  under-report income and force a manual portal edit. Per T5
  codex r4 P1 of the closed PR #220: orphan WHT must hard-fail,
  not be a soft diagnostic.

Exact sums (#1167):

The per-country income and each payment's gross dividend add in USD and
INR; eligible foreign tax adds only in INR. Each uses
:func:`~ibkr_fa.compute.exact_inr.checked_addition`, so they never round.
The country's foreign tax is the netting's signed total negated with
``copy_negate()``, which never rounds either. The treaty-rate test's
products, excess and treaty cap, and column 6's treaty snap, are exact too
(:func:`~ibkr_fa.compute.exact_inr.checked_product`,
:func:`~ibkr_fa.compute.exact_inr.checked_subtraction`, ``copy_abs()``,
#1312). Each unsnapped rate, column 6's and each payment's, rounds HALF_UP to the exact
``100 * tax / income``'s hundredths when the context keeps a third decimal
place (:func:`_rate_percent`). A refusal names
the 1-based position of the dividend line, payment or country and the field,
never a value.

Deterministic sort + serial-number assignment:

The compute layer sorts on
``(cbdt_country_code, source_of_income.value)`` (same key as
Schedule FSI's rows) then assigns 1-indexed
:attr:`Form67Entry.serial_number` values. Byte-identical output
across re-runs of the same input.

The registered Form 67 writers and
:func:`ibkr_fa.pipeline_form67.run_form67` consume the
:class:`Form67ComputeResult` this module builds.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_DOWN, Decimal, Inexact, Rounded, localcontext
from enum import StrEnum
from fractions import Fraction
from typing import Final

from ibkr_fa.compute.exact_inr import checked_addition, checked_product, checked_subtraction
from ibkr_fa.compute.foreign_tax import (
    ForeignTaxNetting,
    WithholdingPaymentKey,
    net_foreign_tax_by_country,
    payment_key,
)
from ibkr_fa.compute.schedule_os import ScheduleOsDividendLine
from ibkr_fa.compute.section_91 import (
    EXACT_ZERO,
    PREFLIGHT_ZERO,
    Section91Check,
    exact_section_91_credit,
    section_91_error,
)
from ibkr_fa.compute.wht_reversal_pairing import WhtCrossFyReversalReport
from ibkr_fa.config import (
    Form67Config,
    Form67Section91Config,
    ScheduleOsIndianAdrRoutingMode,
)
from ibkr_fa.exceptions import ConfigError
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.invariants import require
from ibkr_fa.models.form67_entry import (
    Form67Entry,
    Form67SourceOfIncome,
    Form67TreatyRateStatus,
)
from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.reference.dtaa_dividend import DtaaApplicability, lookup_dtaa_dividend

__all__ = [
    "Form67ComputeResult",
    "Form67Readiness",
    "Form67RefusalReason",
    "Form67RowReadiness",
    "compute_form67_entries_from_wht",
    "form67_override_advice",
    "form67_override_gap",
    "form67_row_readiness",
    "form67_section_91_need",
    "require_no_refused_form67_rows",
    "tax_payable_override_map",
]


class Form67Readiness(StrEnum):
    """Whether Form 67 rows can go into the official ``form-67.csv`` (#601).

    READY rows are complete. BLANK rows have no Indian tax payable and no
    credit: the run withholds the official file and leaves them for manual
    work. REFUSED rows have a credit that files as ₹0, or Indian tax payable
    and a credit a named gap keeps uncomputed, and stop the run.
    """

    READY = "ready"
    BLANK = "blank"
    REFUSED = "refused"


class Form67RefusalReason(StrEnum):
    """The gap that makes a Form 67 row REFUSED."""

    NO_VENDORED_DTAA = "no_vendored_dtaa"
    SECTION_91_INPUTS_MISSING = "section_91_inputs_missing"
    ZERO_CREDIT = "zero_credit"
    SOURCE_RATE_UNPROVEN = "source_rate_unproven"
    PAYMENT_ALLOCATION_UNPROVEN = "payment_allocation_unproven"


@dataclass(frozen=True, slots=True)
class Form67RowReadiness:
    """One Form 67 row's readiness; ``reason`` is set exactly when REFUSED."""

    readiness: Form67Readiness
    reason: Form67RefusalReason | None = None

    def __post_init__(self) -> None:
        require(
            (self.reason is not None) == (self.readiness is Form67Readiness.REFUSED),
            what="Form67RowReadiness.reason must be set exactly when the row is REFUSED",
            readiness=self.readiness,
            reason=self.reason,
        )


_READINESS_RANK: Final[dict[Form67Readiness, int]] = {
    Form67Readiness.READY: 0,
    Form67Readiness.BLANK: 1,
    Form67Readiness.REFUSED: 2,
}


def form67_row_readiness(entry: Form67Entry) -> Form67RowReadiness:
    """Classify one row: READY when col 7, the claimed credit (col 11 or 12)
    and col 13 are set and the credit files as at least ₹1; BLANK when all
    three are uncomputed; REFUSED as a zero credit when all three are set and
    the credit files as ₹0, or with :func:`_credit_gap` when col 7 alone is
    set. No producer builds any other mix of the three, or col 7 alone with
    no named gap, so that fails an invariant (#1491).

    The decision reads the filed, whole-rupee values (#898), so a positive
    credit that rounds to ₹0 is a zero credit.
    """
    filed = entry.filed
    claimed = filed.claimed_credit_inr
    tax_payable = filed.tax_payable_normal_inr
    total = filed.total_credit_inr
    if tax_payable is None and claimed is None and total is None:
        return Form67RowReadiness(Form67Readiness.BLANK)
    if tax_payable is not None and claimed is not None and total is not None:
        # Form67Entry pins total == claimed >= 0, so a filed credit that isn't
        # positive is exactly zero.
        if claimed > 0:
            return Form67RowReadiness(Form67Readiness.READY)
        return Form67RowReadiness(Form67Readiness.REFUSED, Form67RefusalReason.ZERO_CREDIT)
    reason = _credit_gap(entry) if claimed is None and total is None else None
    require(
        reason is not None,
        what=(
            "Form 67 row: col 7, the claimed credit and col 13 are neither all computed "
            "nor all blank, and no named gap keeps the credit uncomputed"
        ),
        country=entry.iso_alpha2,
        source=entry.source_of_income.name,
    )
    return Form67RowReadiness(Form67Readiness.REFUSED, reason)


def _credit_gap(entry: Form67Entry) -> Form67RefusalReason | None:
    """The gap that keeps a dividend row's credit uncomputed whatever col 7
    says, or ``None`` when no named gap does: a country with no vendored DTAA
    row, one with no DTAA with India (a section 91 credit also needs the
    [form67.section_91] inputs), withholding not attributable per payment, or
    no source-currency rate."""
    if entry.source_of_income is not Form67SourceOfIncome.DIVIDEND:
        return None
    dtaa = lookup_dtaa_dividend(entry.iso_alpha2)
    if dtaa is None:
        return Form67RefusalReason.NO_VENDORED_DTAA
    if dtaa.applicability is DtaaApplicability.NO_DTAA:
        return Form67RefusalReason.SECTION_91_INPUTS_MISSING
    if entry.payment_allocation_unproven:
        return Form67RefusalReason.PAYMENT_ALLOCATION_UNPROVEN
    if entry.treaty_rate_status is Form67TreatyRateStatus.UNPROVEN:
        return Form67RefusalReason.SOURCE_RATE_UNPROVEN
    return None


# The gaps that keep a row's credit uncomputed whatever col 7 says (#1461).
_OVERRIDE_CANNOT_CLEAR: Final = frozenset(
    {
        Form67RefusalReason.NO_VENDORED_DTAA,
        Form67RefusalReason.PAYMENT_ALLOCATION_UNPROVEN,
        Form67RefusalReason.SOURCE_RATE_UNPROVEN,
    }
)
# The inputs a section 91 credit needs besides col 7.
_SECTION_91_INPUTS: Final = (
    "[form67.section_91] total_indian_tax_before_91_relief_inr and total_income_inr"
)


def _override_probe(entry: Form67Entry, *, caller: str) -> Form67RefusalReason | None:
    """The gap a BLANK row meets once an override sets col 7 and its credit
    stays uncomputed, which is what an override leaves when the credit can't
    be computed (#1461), or ``None`` when no named gap keeps it uncomputed.
    Refuses a row that isn't BLANK."""
    require(
        form67_row_readiness(entry).readiness is Form67Readiness.BLANK,
        what=f"{caller} needs a BLANK Form 67 row",
    )
    return _credit_gap(entry)


def form67_override_gap(entry: Form67Entry) -> str | None:
    """Why no exact [[form67.tax_payable_overrides]] amount can clear this
    BLANK row, or ``None`` when one could (#1461).

    The row is tried with col 7 set and its credit still uncomputed, which is
    what an override leaves when the credit can't be computed, so the answer
    follows :func:`form67_row_readiness`. A row whose country has no vendored
    DTAA row, whose withholding can't be tested payment by payment, or that has
    no source-currency rate gets its refusal's wording, naming the row. Refuses
    a row that isn't BLANK.
    """
    reason = _override_probe(entry, caller="form67_override_gap")
    if reason in _OVERRIDE_CANNOT_CLEAR:
        return _credit_gap_text(entry, reason)
    return None


def form67_section_91_need(entry: Form67Entry) -> str | None:
    """What else this BLANK row needs for an exact [[form67.tax_payable_overrides]]
    amount to clear it, or ``None`` when the override needs nothing else or
    can't clear it (#1469).

    Like :func:`form67_override_gap`, it reads the gap the row meets with col 7
    set: a country vendored as having no DTAA with India gets a section 91
    credit, which also needs the [form67.section_91] inputs. Refuses a row that
    isn't BLANK.
    """
    reason = _override_probe(entry, caller="form67_section_91_need")
    if reason is not Form67RefusalReason.SECTION_91_INPUTS_MISSING:
        return None
    return (
        f"{entry.iso_alpha2}/{entry.source_of_income.name}: {entry.iso_alpha2} has no DTAA "
        f"with India, so its section 91 credit also needs {_SECTION_91_INPUTS}."
    )


def form67_override_advice(entries: Iterable[Form67Entry]) -> tuple[str, ...]:
    """What the blank Indian tax payable warning says about each BLANK row of
    ``entries`` in place of its generic override sentence (#1469).

    Empty when an exact [[form67.tax_payable_overrides]] amount alone clears
    every BLANK row, so the warning keeps that sentence. Otherwise, per BLANK
    row in order: for a row an override clears, the override sentence, then
    :func:`form67_section_91_need` for a no-DTAA country; for a row it can't
    clear, :func:`form67_override_gap` and that an override won't clear it.
    """
    advice: list[str] = []
    override_alone = True
    for entry in entries:
        if form67_row_readiness(entry).readiness is not Form67Readiness.BLANK:
            continue
        row = f"{entry.iso_alpha2}/{entry.source_of_income.name}"
        gap = form67_override_gap(entry)
        if gap is not None:
            advice.append(
                f"{gap} An exact [[form67.tax_payable_overrides]] amount won't clear {row}."
            )
            override_alone = False
            continue
        advice.append(
            f"To have the tool fill {row}, add an exact [[form67.tax_payable_overrides]] "
            f"amount for it and, when the {entry.iso_alpha2} Schedule FSI Other Sources row "
            "also holds interest or other income, one for INTEREST_INCOME or OTHERS too."
        )
        need = form67_section_91_need(entry)
        if need is not None:
            advice.append(need)
            override_alone = False
    return () if override_alone else tuple(advice)


def _credit_gap_text(entry: Form67Entry, reason: Form67RefusalReason) -> str:
    """The sentence naming the row and the gap no override clears."""
    row = f"{entry.iso_alpha2}/{entry.source_of_income.name}"
    if reason is Form67RefusalReason.NO_VENDORED_DTAA:
        return (
            f"{row}: {entry.iso_alpha2} has no row in the vendored DTAA dividend table, "
            "so neither a section 90 nor a section 91 credit can be computed (#249)."
        )
    if reason is Form67RefusalReason.SOURCE_RATE_UNPROVEN:
        return (
            f"{row}: the source-currency tax rate can't be established (for example, "
            "a same-year refund netted the source-currency tax to zero, #593), so "
            "neither the treaty-eligible foreign tax nor the section 90 credit can be "
            "computed."
        )
    if entry.highest_payment_rate_percent is not None:
        cause = (
            "a dividend payment was withheld above the DTAA rate, and a refund or "
            f"reversal of {entry.iso_alpha2} tax in the year can't be attributed to it"
        )
    else:
        cause = (
            "the withholding can't be matched to a single dividend payment (same-day "
            "dividends of one security in one account can't be told apart, or a "
            "payment's withholding has no dividend or exceeds it)"
        )
    return (
        f"{row}: {cause}, so the treaty-eligible foreign tax, which is capped per "
        "payment (#890), and the section 90 credit can't be computed."
    )


def _refusal_text(entry: Form67Entry, reason: Form67RefusalReason) -> str:
    row = f"{entry.iso_alpha2}/{entry.source_of_income.name}"
    without_override = (
        f"To produce the other outputs, remove the [[form67.tax_payable_overrides]] "
        f"entry for {row}; the row is then left blank for manual Form 67 work"
    )
    if reason in _OVERRIDE_CANNOT_CLEAR:
        return f"{_credit_gap_text(entry, reason)} {without_override}"
    if reason is Form67RefusalReason.SECTION_91_INPUTS_MISSING:
        return (
            f"{row}: {entry.iso_alpha2} has no DTAA with India, and a section 91 credit "
            f"needs {_SECTION_91_INPUTS}"
        )
    require(
        reason is Form67RefusalReason.ZERO_CREDIT,
        what="Form 67 refusal text: no text for this refusal reason",
        reason=reason,
    )
    return (
        f"{row}: the computed credit rounds to ₹0 in whole rupees, the amount Form 67 "
        "files; zero-credit rows are refused under the owner's #601 policy. "
        + without_override
        + ", and Schedule FSI tax and relief are completed by hand, entering 0 "
        "only where the whole FSI row's tax really is 0"
    )


def require_no_refused_form67_rows(result: Form67ComputeResult) -> None:
    """Raise :class:`ConfigError` naming every REFUSED row and its gap.

    A computed section 91 credit is checked first with the former formula's
    credit, which refuses with the fixed preflight message when it would file
    ₹0, and last with the exact credit, which refuses with the fixed
    corrected-value message (#1177). Within either section 91 phase, several
    refusals name the first country input row. The other rows keep the
    :class:`ConfigError` between those phases.
    """
    checks = result._section_91_checks
    section_91 = sorted(
        (
            (checks[entry.iso_alpha2], entry)
            for entry in result.entries
            if entry.iso_alpha2 in checks
        ),
        key=lambda pair: pair[0].position,
    )
    for check, entry in section_91:
        legacy = replace(
            entry,
            section_91_credit_inr=check.legacy_credit_inr,
            total_credit_inr=check.legacy_credit_inr,
        )
        if form67_row_readiness(legacy).reason is Form67RefusalReason.ZERO_CREDIT:
            raise section_91_error(check.position, PREFLIGHT_ZERO)
    refusals = [
        _refusal_text(entry, row.reason)
        for entry, row in zip(result.entries, result.row_readiness, strict=True)
        if row.reason is not None
        and not (entry.iso_alpha2 in checks and row.reason is Form67RefusalReason.ZERO_CREDIT)
    ]
    if refusals:
        raise ConfigError(
            field="form67",
            message="Form 67 cannot be produced: " + "; ".join(refusals) + ".",
        )
    for check, entry in section_91:
        if form67_row_readiness(entry).reason is Form67RefusalReason.ZERO_CREDIT:
            raise section_91_error(check.position, EXACT_ZERO)


@dataclass(frozen=True, slots=True)
class Form67ComputeResult:
    """Result of the Form 67 per-(country, source) compute stage.

    Attributes:
        entries: All :class:`Form67Entry` rows for the FY, sorted by
            ``(cbdt_country_code, source_of_income.value)`` with
            1-indexed :attr:`Form67Entry.serial_number` values
            preserved in that order. Byte-identical across re-runs.
        cross_fy_reversal_report: Diagnostic counts for cross-FY
            WHT-reversal pairing decisions applied during the WHT
            aggregation pass (issue #224). Captures the number of
            reversals excluded from current-FY totals because their
            paired original was in a prior FY (per the LIFO
            heuristic at :func:`~ibkr_fa.compute.wht_reversal_pairing.pair_wht_reversals`),
            which the CLI reports as a prior-FY amendment advisory.
            A current-FY orphan reversal, or one ambiguous across
            FYs, refuses the compute instead
            (:class:`~ibkr_fa.compute.wht_reversal_pairing.UnprovenWhtReversalError`,
            #893). Defaults to an empty (all-zero) report for
            back-compat when no WHT was processed.
        _section_91_checks: Validation-only record of each computed section
            91 credit, keyed by the entry's ``iso_alpha2``: its country input
            row and the former formula's credit, which
            :func:`require_no_refused_form67_rows` and
            :func:`~ibkr_fa.compute.schedule_fsi.require_fsi_relief_matches_portal`
            check where they already ran (#1177). Never filed.
    """

    entries: tuple[Form67Entry, ...]
    cross_fy_reversal_report: WhtCrossFyReversalReport = field(
        default_factory=WhtCrossFyReversalReport
    )
    _section_91_checks: Mapping[str, Section91Check] = field(
        default_factory=dict, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        # Pin determinism: serial_number values are 1..N in tuple
        # order. A direct constructor that supplies un-numbered or
        # out-of-order entries would silently break byte-determinism;
        # this check surfaces the bug at construction time.
        for expected_serial, entry in enumerate(self.entries, start=1):
            require(
                entry.serial_number == expected_serial,
                what=(
                    "Form67ComputeResult.entries must carry 1-indexed "
                    "serial_number values in tuple order"
                ),
                expected_serial=expected_serial,
                actual_serial=entry.serial_number,
            )
        keys = [(e.cbdt_country_code, e.source_of_income.value) for e in self.entries]
        require(
            keys == sorted(keys),
            what=(
                "Form67ComputeResult.entries must be sorted by "
                "(cbdt_country_code, source_of_income.value) for "
                "deterministic output across runs"
            ),
        )
        # T3 r1 P3 of PR #221: pin (cbdt_country_code,
        # source_of_income) uniqueness so a directly-constructed
        # result cannot contain two rows for the same aggregate
        # bucket. The compute layer's aggregation guarantees this
        # by construction; this check protects against future
        # direct-constructor consumers that might violate the
        # per-(country, source) contract.
        seen: set[tuple[str, str]] = set()
        for entry in self.entries:
            key = (entry.cbdt_country_code, entry.source_of_income.value)
            require(
                key not in seen,
                what=(
                    "Form67ComputeResult.entries must be unique on "
                    "(cbdt_country_code, source_of_income) — Form 67 "
                    "is per-aggregate; duplicate rows for the same "
                    "(country, source) violate the contract"
                ),
                cbdt_country_code=entry.cbdt_country_code,
                source_of_income=entry.source_of_income.value,
            )
            seen.add(key)
        section_91_isos = {
            entry.iso_alpha2 for entry in self.entries if entry.section_91_credit_inr is not None
        }
        require(
            set(self._section_91_checks) <= section_91_isos,
            what=(
                "Form67ComputeResult section 91 checks must belong to entries "
                "with a section 91 credit"
            ),
        )

    @property
    def row_readiness(self) -> tuple[Form67RowReadiness, ...]:
        """Each entry's readiness, in ``entries`` order."""
        return tuple(form67_row_readiness(entry) for entry in self.entries)

    @property
    def readiness(self) -> Form67Readiness:
        """REFUSED over BLANK over READY across the rows; an empty result is READY."""
        return max(
            (row.readiness for row in self.row_readiness),
            key=_READINESS_RANK.__getitem__,
            default=Form67Readiness.READY,
        )

    @property
    def ready_with_fractional_rate(self) -> bool:
        """READY, and a row files a fractional Rate(%). When the selected format
        writes the upload ``form-67.csv``, the filer enters that rate by hand
        after upload, so the run reports manual work (#1447)."""
        return self.readiness is Form67Readiness.READY and any(
            entry.filed.tax_paid_rate_is_fractional for entry in self.entries
        )


def tax_payable_override_map(
    form67_config: Form67Config | None,
) -> dict[tuple[str, Form67SourceOfIncome], Decimal]:
    """Each exact configured Form 67 col 7 amount by (ISO alpha-2, source),
    in config order, a later entry for the same key replacing an earlier one.
    Empty without a config."""
    if form67_config is None:
        return {}
    return {
        (override.iso_alpha2, override.source_of_income): override.tax_payable_normal_inr
        for override in form67_config.tax_payable_overrides
    }


_HUNDRED: Final = Decimal("100")
_ZERO: Final = Decimal("0")
# Source amounts are cent-rounded per withholding row, so each paid row may
# sit up to half a cent from the treaty rate's exact amount.
_ROUNDING_TOLERANCE_PER_ROW: Final = Decimal("0.005")


def _rate_percent(tax: Decimal, income: Decimal, *, at: str) -> Decimal:
    """``100 * tax / income`` at the active context's precision, filing the exact
    rate's hundredths (#1312).

    The rate is ``(tax / income) * 100`` in the active context, as before, so
    it keeps the value and digits every output printed. Where that rounding
    carried it up onto a half hundredth, such as 12.345, that the exact rate is
    below, it is instead the exact rate divided once at that precision,
    rounding toward zero. The exact numerator is
    :func:`~ibkr_fa.compute.exact_inr.checked_product`, which refuses as it does
    at ``at``, a fixed label. Either way its two-place HALF_UP is the exact
    rate's whenever the context keeps a third decimal place: at the default 28
    digits, any rate below 10^25 %. ``income`` must be positive.
    """
    rate = (tax / income) * _HUNDRED
    numerator = checked_product(tax, _HUNDRED, at=at)
    with localcontext() as context:
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = context.traps[Rounded] = False
        cut = numerator / income
    # At a half-hundredth boundary, rate times 200 is an odd integer.
    half_hundredths = Fraction(rate) * 200
    on_half_hundredth = half_hundredths.denominator == 1 and half_hundredths.numerator % 2 == 1
    return cut if rate != cut and on_half_hundredth else rate


@dataclass(frozen=True, slots=True)
class _PaymentGross:
    """One payment's dividend lines: gross in USD and Rule-115 INR, and their count."""

    usd: Decimal
    inr: Decimal
    line_count: int


@dataclass(frozen=True, slots=True)
class _TreatyRateDecision:
    """The treaty-rate fields :func:`_decide_treaty_rate` sets on an entry."""

    status: Form67TreatyRateStatus
    eligible_foreign_tax_inr: Decimal | None = None
    highest_payment_rate_percent: Decimal | None = None
    payment_allocation_unproven: bool = False


def _gross_by_payment(
    os_dividend_lines: Iterable[ScheduleOsDividendLine],
) -> dict[WithholdingPaymentKey, _PaymentGross]:
    gross: dict[WithholdingPaymentKey, _PaymentGross] = {}
    for position, line in enumerate(os_dividend_lines, start=1):
        key = payment_key(line)
        so_far = gross.get(key, _PaymentGross(_ZERO, _ZERO, 0))
        gross[key] = _PaymentGross(
            usd=checked_addition(
                so_far.usd,
                line.amount_gross_usd,
                at=f"Form 67 payment gross, input row {position}, column amount_gross_usd",
            ),
            inr=checked_addition(
                so_far.inr,
                line.amount_gross_inr,
                at=f"Form 67 payment gross, input row {position}, column amount_gross_inr",
            ),
            line_count=so_far.line_count + 1,
        )
    return gross


def _decide_treaty_rate(
    *,
    iso: str,
    country_rate_percent: Decimal | None,
    treaty_rate_percent: Decimal,
    foreign_tax_inr: Decimal,
    netting: ForeignTaxNetting,
    gross_by_payment: Mapping[WithholdingPaymentKey, _PaymentGross],
) -> _TreatyRateDecision:
    """Decide whether a DTAA country's withholding is within the treaty rate.

    Rule 128(5)(i)'s proviso ignores foreign tax above the DTAA rate but
    doesn't say whether that excess is measured per payment or on the
    year's total. Until that is settled, the owner's interim policy caps
    each payment at the treaty rate (#890). A payment is the in-FY dividend
    lines and paid withholding rows sharing a
    :class:`~ibkr_fa.compute.foreign_tax.WithholdingPaymentKey`.

    * No country source rate: ``UNPROVEN`` (#767).
    * A payment that can't be tested on its own: ``UNPROVEN`` with payment
      allocation unproven. That is a key holding several dividend lines,
      whatever its account index, or paid withholding with no dividend
      line on its key or above its gross.
    * A payment is above the rate when its paid tax, before refunds,
      exceeds the treaty rate times its gross by more than half a cent per
      paid row. Its rate is snapped to the treaty rate within that margin.
      The treaty tax, the excess and its magnitude are exact, and an unsnapped
      rate is :func:`_rate_percent` of the paid tax and gross (#1312).
    * No payment above: ``WITHIN``, with all net Rule-128 tax eligible.
    * Some payment above and a refund or reversal in the country, which
      can't be attributed to a payment: ``UNPROVEN`` with payment
      allocation unproven.
    * Otherwise ``ABOVE``: eligible tax is each payment's Rule-128 tax,
      capped for an above-rate payment at the exact treaty rate times its
      Rule-115 gross.
    """
    if country_rate_percent is None:
        return _TreatyRateDecision(Form67TreatyRateStatus.UNPROVEN)
    treaty_rate = treaty_rate_percent / _HUNDRED
    highest: Decimal | None = None
    any_above = False
    eligible = _ZERO
    for position, (key, paid) in enumerate(netting.paid_by_payment.items(), start=1):
        if key.isin[:2] != iso:
            continue
        gross = gross_by_payment.get(key)
        if gross is None or paid.native > gross.usd or gross.line_count > 1:
            return _TreatyRateDecision(
                Form67TreatyRateStatus.UNPROVEN, payment_allocation_unproven=True
            )
        tolerance = _ROUNDING_TOLERANCE_PER_ROW * paid.row_count
        at = f"Form 67 treaty-rate test, payment {position}"
        excess = checked_subtraction(
            paid.native,
            checked_product(treaty_rate, gross.usd, at=f"{at}, field treaty_tax_usd"),
            at=f"{at}, field excess_usd",
        )
        above = excess > tolerance
        rate = (
            treaty_rate_percent
            if excess.copy_abs() <= tolerance
            else _rate_percent(paid.native, gross.usd, at=f"{at}, field rate_percent")
        )
        highest = rate if highest is None else max(highest, rate)
        any_above = any_above or above
        cap = f"Form 67 eligible foreign tax, payment {position}"
        eligible = checked_addition(
            eligible,
            (
                min(paid.inr, checked_product(treaty_rate, gross.inr, at=f"{cap}, field cap_inr"))
                if above
                else paid.inr
            ),
            at=f"{cap}, field eligible_foreign_tax_inr",
        )
    # A positive source-currency country rate needs a paid row in the country.
    require(
        highest is not None,
        what="Form 67 compute: a country with a source rate must have a paid payment",
        iso=iso,
    )
    if not any_above:
        return _TreatyRateDecision(
            Form67TreatyRateStatus.WITHIN,
            eligible_foreign_tax_inr=foreign_tax_inr,
            highest_payment_rate_percent=highest,
        )
    if any(row.amount > 0 and row.isin[:2] == iso for row in netting.included_rows):
        return _TreatyRateDecision(
            Form67TreatyRateStatus.UNPROVEN,
            highest_payment_rate_percent=highest,
            payment_allocation_unproven=True,
        )
    return _TreatyRateDecision(
        Form67TreatyRateStatus.ABOVE,
        eligible_foreign_tax_inr=eligible,
        highest_payment_rate_percent=highest,
    )


def _resolve_section_90_relief(
    *,
    eligible_foreign_tax_inr: Decimal | None,
    tax_payable_normal_inr: Decimal | None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Compute Form 67 cols 11 (§90 credit) + 13 (total credit) via
    Rule 128(5)(i).

    The credit cannot exceed the Indian tax payable on the same foreign
    income (so that the FTC cannot create a refund on income for which
    India has collected no tax), nor the eligible foreign tax from
    :func:`_decide_treaty_rate`::

        section_90_credit_inr = min(tax_payable_normal_inr, eligible_foreign_tax_inr)

    When exact Indian tax payable is absent, cols 7, 11, and 13 remain
    blank. Foreign tax withheld is not evidence of Indian tax
    attributable to the same income.

    Zero exact tax_payable_normal_inr is a legitimate computed outcome —
    credit = min(0, eligible) = 0; Form67Entry's
    section_90_credit_inr >= 0 invariant accepts this. The
    total_credit_inr equals the §90 credit per the Form67Entry
    cross-field invariant (total == populated branch's credit).

    Returns:
        Effective normal tax, section 90 credit, and total credit.
    """
    if eligible_foreign_tax_inr is None or tax_payable_normal_inr is None:
        return tax_payable_normal_inr, None, None
    section_90_credit_inr = min(tax_payable_normal_inr, eligible_foreign_tax_inr)
    return tax_payable_normal_inr, section_90_credit_inr, section_90_credit_inr


def _resolve_section_91_relief(
    *,
    dtaa_applicability: DtaaApplicability | None,
    section_91_config: Form67Section91Config | None,
    tax_payable_normal_inr: Decimal | None,
    foreign_tax_inr: Decimal,
    income_inr: Decimal,
    position: int,
) -> tuple[Decimal | None, Decimal | None, Section91Check | None]:
    """Compute Form 67 cols 12 (§91 unilateral credit) + 13 (total
    credit) per the §91 Explanation of the Income-tax Act, 1961.

    §91 grants relief for income from a country with which India has
    NO DTAA under §90. The credit is the exact rational::

        section_91_credit_inr = min(foreign_tax_inr, total_indian_tax * income_inr / total_income)

    that is, the doubly taxed income at the lower of the Indian average
    rate and the foreign rate. :func:`~ibkr_fa.compute.section_91.exact_section_91_credit`
    computes it (#1177). When foreign tax wins or ties, the credit is
    ``foreign_tax_inr`` itself, never round-tripped through a rate
    (PR #233 T3 r0 P1: ``income * (foreign_tax / income)`` can grow by an
    ULP and break ``section_91_credit_inr <= foreign_tax_inr``). Otherwise
    the exact cap is divided once, rounding down, at the caller's
    precision; that is never above the cap, so it stays below foreign tax.
    Either value is returned only once its whole-rupee HALF_UP rounding is
    proven to equal the exact credit's. A repeating average rate is
    ordinary and computes. The fields stay unrounded; Form 67 and Schedule FSI
    round them where they always have. Schedule TR adds those filed whole-rupee cells.

    Gated on ALL THREE of:

      (a) DTAA applicability is exactly NO_DTAA. For DTAA-applicable
          countries, the §90 pathway is the only available route
          (Form67Entry's mutex rejects §91 credit paired with DTAA
          fields). For unvendored ("unknown") countries, the
          compute layer MUST NOT default to §91 — silently
          attributing relief under the wrong section for a DTAA
          country we haven't yet vendored would mis-file the
          return. The blocking design point from the step-4/N
          planning rubber-duck pass applies here too.

      (b) section_91_config is supplied (i.e., the user has filled
          [form67.section_91] in user.toml). The Indian average
          rate cannot be derived from the IBKR statement alone.

      (c) tax_payable_normal_inr is resolved. This isn't a direct
          input to the §91 formula, but is required for audit
          consistency with the §90 pathway: col 7 must accompany
          a populated col 12 so the auditor can reconcile the
          per-source Indian tax payable against the §91 average-
          rate-based relief computation.

    When any gate is not met, all three returned values are None and
    nothing is computed.

    Zero total Indian tax is legitimate (e.g., total Indian income below
    the basic exemption limit) and computes a zero credit, which
    Form67Entry's section_91_credit_inr >= 0 invariant accepts; the
    zero-credit filing policy then applies as before. The
    total_credit_inr equals the §91 credit per the Form67Entry
    cross-field invariant.

    ``position`` is the country's 1-based input row, which the fixed
    refusals name.

    Returns:
        ``(section_91_credit_inr, total_credit_inr, check)``: all None
        (gate not met), or the same proven Decimal twice and the
        :class:`~ibkr_fa.compute.section_91.Section91Check` the readiness
        and Schedule FSI portal checks read.
    """
    if (
        dtaa_applicability is not DtaaApplicability.NO_DTAA
        or section_91_config is None
        or tax_payable_normal_inr is None
    ):
        return None, None, None
    credit, check = exact_section_91_credit(
        total_tax=section_91_config.total_indian_tax_before_91_relief_inr,
        total_income=section_91_config.total_income_inr,
        income_inr=income_inr,
        foreign_tax_inr=foreign_tax_inr,
        position=position,
    )
    return credit, credit, check


def _build_form67_entry_for_country(
    *,
    iso: str,
    cbdt_code: str,
    country_name: str,
    income_inr: Decimal,
    foreign_tax_inr: Decimal,
    income_currency: Decimal,
    foreign_tax_currency: Decimal,
    event_count: int,
    override_map: dict[tuple[str, Form67SourceOfIncome], Decimal],
    section_91_config: Form67Section91Config | None,
    netting: ForeignTaxNetting,
    gross_by_payment: Mapping[WithholdingPaymentKey, _PaymentGross],
    position: int,
) -> tuple[Form67Entry, Section91Check | None]:
    """Build one :class:`Form67Entry` for a country aggregate, and its
    section 91 check when it has a section 91 credit.

    Resolves cols 7 (tax payable), 9 + 10 (DTAA article + rate via
    the tri-state lookup), the treaty-rate status, the eligible foreign
    tax, 11 (§90 credit), 12 (§91 credit), and 13 (total credit) per the
    gates documented in the relief helpers. Caller supplies the post-net-positive per-country WHT
    + matching OS-dividend income (already aggregated and
    validated), the CBDT country reference fields, the
    Form67Config sub-resolved bits (override_map and section_91_config),
    and the netting and per-payment gross the treaty-rate test reads.
    ``position`` is the country's 1-based input row.

    Column 6 files the source-currency rate when proven, else the INR rate,
    each rounded HALF_UP to two places by
    :attr:`~ibkr_fa.models.form67_entry.Form67Entry.filed`. Before snapping, both are
    :func:`_rate_percent` of the tax and income, so each files the exact
    rate's hundredths when the context keeps a third decimal place (#1312).
    The source rate is proven when the tax and
    income are positive and the rate is at most 100 %. It snaps to the DTAA
    rate when the tax is within half a cent per withholding row of that rate
    times the income, and :func:`_decide_treaty_rate` found no payment above
    that rate by more than the payment's own tolerance (#1448); the treaty
    tax and the difference are exact.
    """
    at = f"Form 67 country {position}"
    foreign_tax_rate_percent = _rate_percent(
        foreign_tax_inr, income_inr, at=f"{at}, field foreign_tax_rate_percent"
    )

    dtaa_entry = lookup_dtaa_dividend(iso)
    if dtaa_entry is not None and dtaa_entry.applicability is DtaaApplicability.DTAA:
        # The WITHHOLDING (distributive) article, not the relief article. Col 9 sits
        # immediately beside col 10 "Rate of tax as per DTAA (%)", and only a
        # distributive article states a rate — India-US Article 10(2)(b) caps
        # portfolio dividends at 25%, while Article 25 (Relief from Double Taxation)
        # fixes no percentage at all. The form's own structure confirms it: the
        # parallel "Relief claimed under section 91" block has a rate column but NO
        # article column, because there is no treaty to cite.
        #
        # Beware the India-US coincidence: the relief article number (25) equals the
        # portfolio dividend rate (25%), so citing the wrong one still "looks right".
        section_90_dtaa_article = dtaa_entry.withholding_article
        section_90_dtaa_rate_percent = dtaa_entry.portfolio_dividend_rate_percent
    else:
        section_90_dtaa_article = None
        section_90_dtaa_rate_percent = None
    tax_paid_rate_percent: Decimal | None = None
    if income_currency > 0 and foreign_tax_currency > 0:
        source_effective_rate = _rate_percent(
            foreign_tax_currency, income_currency, at=f"{at}, field tax_paid_rate_percent"
        )
        if source_effective_rate <= _HUNDRED:
            tax_paid_rate_percent = source_effective_rate
    within_rounding = False
    if tax_paid_rate_percent is not None and section_90_dtaa_rate_percent is not None:
        expected_tax = checked_product(
            section_90_dtaa_rate_percent / _HUNDRED,
            income_currency,
            at=f"{at}, field expected_tax_currency",
        )
        source_rounding_tolerance = _ROUNDING_TOLERANCE_PER_ROW * event_count
        difference = checked_subtraction(
            foreign_tax_currency, expected_tax, at=f"{at}, field expected_tax_difference"
        )
        within_rounding = difference.copy_abs() <= source_rounding_tolerance

    source = Form67SourceOfIncome.DIVIDEND
    tax_payable_normal_inr = override_map.get((iso, source))
    decision: _TreatyRateDecision | None = None
    if section_90_dtaa_rate_percent is not None:
        decision = _decide_treaty_rate(
            iso=iso,
            country_rate_percent=tax_paid_rate_percent,
            treaty_rate_percent=section_90_dtaa_rate_percent,
            foreign_tax_inr=foreign_tax_inr,
            netting=netting,
            gross_by_payment=gross_by_payment,
        )
        highest = decision.highest_payment_rate_percent
        # Snap only when no tested payment is above the treaty rate: a payment above
        # it isn't cent rounding, however small the country's difference (#1448).
        if within_rounding and (highest is None or highest <= section_90_dtaa_rate_percent):
            tax_paid_rate_percent = section_90_dtaa_rate_percent
    eligible_foreign_tax_inr = None if decision is None else decision.eligible_foreign_tax_inr
    (
        tax_payable_normal_inr,
        section_90_credit_inr,
        section_90_total_inr,
    ) = _resolve_section_90_relief(
        eligible_foreign_tax_inr=eligible_foreign_tax_inr,
        tax_payable_normal_inr=tax_payable_normal_inr,
    )

    dtaa_applicability = dtaa_entry.applicability if dtaa_entry is not None else None
    section_91_credit_inr, section_91_total_inr, section_91_check = _resolve_section_91_relief(
        dtaa_applicability=dtaa_applicability,
        section_91_config=section_91_config,
        tax_payable_normal_inr=tax_payable_normal_inr,
        foreign_tax_inr=foreign_tax_inr,
        income_inr=income_inr,
        position=position,
    )

    # §90 and §91 are mutually exclusive by construction (the DTAA
    # applicability gate routes each country to at most one branch);
    # pick whichever resolved a non-None total.
    if section_90_total_inr is not None:
        total_credit_inr: Decimal | None = section_90_total_inr
    elif section_91_total_inr is not None:
        total_credit_inr = section_91_total_inr
    else:
        total_credit_inr = None

    entry = Form67Entry(
        serial_number=1,  # placeholder; reassigned after sort
        cbdt_country_code=cbdt_code,
        country_name_portal=country_name,
        iso_alpha2=iso,
        source_of_income=source,
        income_inr=income_inr,
        foreign_tax_inr=foreign_tax_inr,
        foreign_tax_rate_percent=foreign_tax_rate_percent,
        event_count=event_count,
        tax_paid_rate_percent=tax_paid_rate_percent,
        tax_payable_normal_inr=tax_payable_normal_inr,
        tax_payable_special_inr=None,
        section_90_dtaa_article=section_90_dtaa_article,
        section_90_dtaa_rate_percent=section_90_dtaa_rate_percent,
        eligible_foreign_tax_inr=eligible_foreign_tax_inr,
        treaty_rate_status=None if decision is None else decision.status,
        section_90_credit_inr=section_90_credit_inr,
        section_91_credit_inr=section_91_credit_inr,
        total_credit_inr=total_credit_inr,
        highest_payment_rate_percent=(
            None if decision is None else decision.highest_payment_rate_percent
        ),
        payment_allocation_unproven=(decision is not None and decision.payment_allocation_unproven),
    )
    return entry, section_91_check


def compute_form67_entries_from_wht(
    withholding_tax: Iterable[WithholdingTax],
    *,
    os_dividend_lines: Iterable[ScheduleOsDividendLine],
    wht_fx_policy: FxPolicy,
    financial_year_start: date,
    financial_year_end: date,
    form67_config: Form67Config | None = None,
    adr_routing_mode: ScheduleOsIndianAdrRoutingMode = ScheduleOsIndianAdrRoutingMode.DEPOSITARY,
) -> Form67ComputeResult:
    """Build per-(country, DIVIDEND) Form 67 entries from in-FY WHT.

    Aggregates Dividends-section WHT by source country, nets in-FY
    refunds against tax-paid amounts, applies Rule 128(5)(ii) FX
    per-event before summing, and emits ONE :class:`Form67Entry`
    per country with positive netted WHT.

    Args:
        withholding_tax: Iterable of parsed
            :class:`~ibkr_fa.models.WithholdingTax` instances from
            the Activity Statement's Withholding Tax section. v1
            assumes Dividends-section WHT only (parser already
            filters by section).
        os_dividend_lines: Iterable of in-FY foreign-source
            :class:`~ibkr_fa.compute.schedule_os.ScheduleOsDividendLine`
            entries from a prior
            :func:`~ibkr_fa.compute.schedule_os.compute_schedule_os_dividend_lines`
            call. Used to source the per-country gross income
            (Rule 115(1), Explanation (2)(e) FX-converted upstream).
        wht_fx_policy: Rule 128(5)(ii) FX policy (typically
            :class:`~ibkr_fa.fx.policy_rule128.Rule128Policy`)
            covering :attr:`WithholdingTax.currency`.
        financial_year_start: Inclusive lower bound for
            ``payment_date`` filtering (April 1 of the reporting FY).
        financial_year_end: Inclusive upper bound (March 31).
        form67_config: Optional exact source-level tax-payable overrides
            and section 91 statutory inputs. Entries without an override
            leave Indian-tax and FTC fields blank.
        adr_routing_mode: Schedule OS's Indian-ADR routing mode
            (default :attr:`~ibkr_fa.config.ScheduleOsIndianAdrRoutingMode.DEPOSITARY`).
            When
            :attr:`~ibkr_fa.config.ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER`,
            Indian-underlying ADRs (ISINs in
            :data:`~ibkr_fa.reference.indian_adr_mapping.INDIAN_ADR_MAPPING`)
            have their dividends rerouted by Schedule OS to
            :class:`~ibkr_fa.compute.schedule_os.ScheduleOsDomesticDividendLine`
            (excluded from ``os_dividend_lines``); to preserve
            scope symmetry with the income side, this aggregator
            silently SKIPS WHT rows whose ISIN is in the mapping
            so they do not orphan the depositary country's bucket
            or get silently absorbed by unrelated same-country
            foreign income (issue #225). Default DEPOSITARY mode
            preserves the legacy depositary-key behavior with no
            skipping. Added in #225.

    Returns:
        :class:`Form67ComputeResult` with sorted, serial-numbered
        entries.

    Raises:
        InvariantError: If any in-FY WHT row's currency does not
            match ``wht_fx_policy.currency`` (multi-currency v1
            limitation per #52); if ``wht_fx_policy.currency`` is
            not "USD" (the only currency v1 supports); if any
            country has positive netted WHT but zero matching OS
            dividend income (orphan WHT — silent omission would
            under-report claim per T5 codex r4 P1 of the closed
            PR #220).
        :class:`~ibkr_fa.reference.country_codes.CountryCodeNotFoundError`:
            If a WHT row's ISIN prefix doesn't map to a known
            CBDT country code.
    """
    # Mirror the FSI WHT aggregator's single-currency invariant:
    # require the policy itself to be USD-denominated. Without
    # this, an EUR caller supplying an EUR policy + EUR WHT
    # would bypass the per-row currency match check and produce
    # entries through an unsupported multi-currency path. T5
    # codex r4 P2 of the closed PR #220.
    require(
        wht_fx_policy.currency == "USD",
        what=(
            "Form 67 compute v1 requires a USD-denominated FX policy "
            "(matches the FSI WHT aggregator's single-currency "
            "invariant; multi-currency support tracked at #52)"
        ),
        fx_policy_currency=wht_fx_policy.currency,
    )

    # PR-4c/N of #37: pre-build the (iso, source) → tax_payable
    # override lookup dict for O(1) per-entry resolution. When
    # form67_config is None or has no overrides, this is an empty
    # dict and every lookup returns None.
    override_map = tax_payable_override_map(form67_config)
    # PR-4d/N of #37: expose the §91 sub-config for the
    # _resolve_section_91_relief gate. None when no Form67Config or
    # no [form67.section_91] sub-table → cols 12 + 13 stay None for
    # all NO_DTAA countries.
    section_91_config: Form67Section91Config | None = (
        None if form67_config is None else form67_config.section_91
    )

    # Aggregate the per-country INR income from the OS dividend
    # lines stream first (one pass; the data is already FX-converted
    # under Rule 115(1), Explanation (2)(e)). Used to populate Form 67 col (4).
    #
    # Income key: ScheduleOsDividendLine.issuer_country_iso2 — the
    # underlying issuer's jurisdiction. This matches Schedule FSI's
    # income aggregator (schedule_fsi.py:265) so Form 67 ⇄ FSI
    # reconciliation by country is byte-consistent for the income
    # side. WHT (below) uses isin[:2] (the depositary jurisdiction
    # where withholding occurred); the difference matters for ADR
    # securities where the depositary differs from the underlying
    # issuer (e.g., a UK-issuer ADR on a US exchange has
    # isin[:2]='US' but issuer_country_iso2='GB'). The orphan-WHT
    # guard catches the mismatch — T4 r1 P2 of PR #221.
    os_lines = tuple(os_dividend_lines)
    per_country_income_inr: dict[str, Decimal] = {}
    per_country_income_currency: dict[str, Decimal] = {}
    for position, os_line in enumerate(os_lines, start=1):
        iso = os_line.issuer_country_iso2
        per_country_income_inr[iso] = checked_addition(
            per_country_income_inr.get(iso, _ZERO),
            os_line.amount_gross_inr,
            at=f"Form 67 country income, input row {position}, column amount_gross_inr",
        )
        per_country_income_currency[iso] = checked_addition(
            per_country_income_currency.get(iso, _ZERO),
            os_line.amount_gross_usd,
            at=f"Form 67 country income, input row {position}, column amount_gross_usd",
        )

    # Per-country signed WHT, netted exactly as Schedule FSI col 5 nets
    # it: see compute/foreign_tax.py for the #225 ADR skip, the #224
    # cross-FY reversal pairing and the per-event Rule 128(5)(ii) FX.
    netting = net_foreign_tax_by_country(
        withholding_tax,
        fx_policy=wht_fx_policy,
        financial_year_start=financial_year_start,
        financial_year_end=financial_year_end,
        adr_routing_mode=adr_routing_mode,
    )

    # Build entries for countries with positive netted WHT. HARD-FAIL
    # on orphan WHT (positive WHT, zero matching OS income).
    # The treaty-rate test pairs each payment's WHT with the dividend lines
    # on its own key, whatever their issuer country (#890).
    gross_by_payment = _gross_by_payment(os_lines)
    entries_unsorted: list[Form67Entry] = []
    section_91_checks: dict[str, Section91Check] = {}
    # A section 91 refusal names the country's 1-based position here, counted
    # before any country is skipped (#1177).
    for position, country in enumerate(netting.countries.values(), start=1):
        iso = country.iso_alpha2
        # Negate: negative signed totals → positive tax paid. copy_negate,
        # unlike unary minus, never rounds an exact total (#1167).
        foreign_tax_inr = country.signed_inr.copy_negate()
        foreign_tax_currency = country.signed_native.copy_negate()
        if foreign_tax_inr <= 0:
            # Net refund or net zero — no Form 67 entry. The Schedule TR
            # / FSI layers handle refunds at the per-country level.
            continue
        income_inr = per_country_income_inr.get(iso, Decimal("0"))
        income_currency = per_country_income_currency.get(iso, Decimal("0"))
        require(
            income_inr > 0,
            what=(
                "Form 67 compute: country has positive netted WHT but "
                "ZERO matching foreign-source dividend income in the "
                "OS compute output. Indian FTC (Rule 128(5)(i)) "
                "requires the foreign income to be declared on "
                "Schedule FSI before a credit can be claimed against "
                "it (Form 67 is the FTC-claim vehicle), so silently "
                "emitting a zero-income entry would under-report the "
                "claim and force a manual portal edit. Common causes: "
                "(a) IBKR mis-classification of the underlying "
                "instrument; (b) downstream OS-row filtering removed "
                "the matching dividend; (c) the source statement is missing the "
                "Dividends section. (Issue #225 Indian-ADR mis-routing "
                "is now handled structurally: when "
                "adr_routing_mode=UNDERLYING_ISSUER is active, mapped "
                "Indian-ADR WHT rows are skipped before WHT netting so "
                "they cannot orphan the depositary country here.)"
            ),
            iso=iso,
            foreign_tax_inr=foreign_tax_inr,
            income_currency=income_currency,
            foreign_tax_currency=foreign_tax_currency,
        )
        # PR-4b/N: cols 9 + 10 from DTAA tri-state lookup (withholding
        # article + treaty rate for DTAA, None otherwise).
        # PR-4c/N: cols 7, 11, 13 from Form67Config + Rule 128(5)(i)
        # §90 formula.
        # PR-4d/N: cols 12 + 13 from Form67Config.section_91 + §91
        # Explanation formula. §90 and §91 are mutex by the DTAA
        # tri-state gate; total_credit_inr picks the populated
        # branch. See _build_form67_entry_for_country for the
        # per-col gating contract.
        entry, section_91_check = _build_form67_entry_for_country(
            iso=iso,
            cbdt_code=country.cbdt_country_code,
            country_name=country.country_name_portal,
            income_inr=income_inr,
            foreign_tax_inr=foreign_tax_inr,
            income_currency=income_currency,
            foreign_tax_currency=foreign_tax_currency,
            event_count=country.event_count,
            override_map=override_map,
            section_91_config=section_91_config,
            netting=netting,
            gross_by_payment=gross_by_payment,
            position=position,
        )
        entries_unsorted.append(entry)
        if section_91_check is not None:
            section_91_checks[iso] = section_91_check

    # Sort deterministically + reassign 1-indexed serial numbers.
    entries_unsorted.sort(key=lambda e: (e.cbdt_country_code, e.source_of_income.value))
    sorted_entries: list[Form67Entry] = []
    for serial, e in enumerate(entries_unsorted, start=1):
        sorted_entries.append(replace(e, serial_number=serial))

    return Form67ComputeResult(
        entries=tuple(sorted_entries),
        cross_fy_reversal_report=netting.cross_fy_reversal_report,
        _section_91_checks=section_91_checks,
    )
