"""One Form 67 (country, source-of-income) aggregate row for the FTC claim.

A :class:`Form67Entry` is the typed shape of one row of CBDT Form 67
("Statement of income from a country or specified territory outside
India and Foreign Tax Credit"), filed via the e-filing portal's
Form 67 service per Rule 128(9) of the Income-tax Rules, 1962. Per
the 2022 amendment to Rule 128(9), for original / belated returns
under §139(1) / §139(4), Form 67 may be furnished on or before the
end of the relevant assessment year, provided the ITR has been
filed within that time limit (Form 67 no longer needs to precede
the ITR — pre-2022 it had to be filed on or before the §139(1) due
date). For updated returns under §139(8A) the proviso to Rule
128(9) applies: Form 67 (covering the additional income in the
updated return) must be furnished on or before the date on which
the updated return is itself furnished.

Form 67 row granularity (CRITICAL — verified against the official
CBDT PDF retrieved 2026-06-04 from the archive.org Wayback Machine
snapshot 20230629195202 of
``incometaxindia.gov.in/forms/income-tax%20rules/itr62form67.pdf``):

The Form 67 main table (Part A, item 5) is **per-(country, source-of-
income)** AGGREGATE — NOT per-event. A taxpayer with ten foreign
dividend payments from the United States in one financial year files
ONE row with the SUMMED income and SUMMED tax paid, NOT ten separate
rows. This matches Schedule FSI's per-(country, head_of_income)
granularity with 7 additional tax / relief columns layered on top.

The previous PR #220 design ("one row per foreign-tax-payment event")
was structurally wrong and was closed unmerged per T5 codex P1
review after verification against the live PDF. The codebase memory
under subject "tax-law correctness" captures the verified column
list and source enumeration.

Form 67 columns (verbatim from the official PDF, with v1 field
mapping):

* **Column (1) Sl. No.** — 1-indexed serial number assigned after
  the compute layer's deterministic sort.
* **Column (2) Name of the country / specified territory** — the
  CBDT 1-4 digit numeric code AND the portal display name. v1
  stores both plus the ISO 3166-1 alpha-2 for FSI reconciliation.
* **Column (3) Source of income** — one of the ten heads
  enumerated on the form: Salary / House property / Business or
  professional income / Long term capital gain / Short term capital
  gain / Interest income / **Dividend** / Royalty (not being part
  of business income) / Fees for technical services (not being
  part of business income) / Others. v1 emits ``DIVIDEND`` only
  (WHT on dividends; CG / interest WHT requires source-kind
  tagging on the WHT model — follow-up).
* **Column (4) Income from outside India** — gross foreign-source
  income for the (country, source) aggregate, in INR (Rule 115(1), Explanation (2)(e)
  preceding-month-end FX, same value Schedule OS / FSI use).
* **Columns (5)-(6) Tax paid outside India** — sub-divided into:

    - (5) **Amount**: foreign tax paid for the (country, source)
      aggregate, in INR (Rule 128(5)(ii) preceding-month-end FX).
      Always positive — net of within-FY reversals per the FSI
      WHT aggregator's netting convention.
    - (6) **Rate**: source-currency withholding rate when proven;
      otherwise ``(5) / (4) * 100``. The source rate snaps to the DTAA
      rate only when the treaty-rate test finds no payment above that
      rate by more than the payment's own tolerance. Rendered as a
      percentage; see :mod:`ibkr_fa.compute.form67`.

* **Column (7) Tax payable on such income under normal provisions
  in India** — exact source-level Indian tax when supplied; otherwise
  blank because broker data cannot establish the taxpayer's Indian
  tax attributable to that income.
* **Column (8) Tax payable on such income under section
  115JB/115JC** — Indian tax under MAT / AMT special provisions.
  Not computed: the compute layer always sets it to ``None``. The
  writers still render a supplied value.
* **Columns (9)-(11) Credit claimed under section 90 / 90A** —
  sub-divided into:

    - (9) **Article No. of Double Taxation Avoidance Agreement**
      — verbatim citation of the DISTRIBUTIVE (withholding)
      article, i.e. the one that supplies column (10)'s rate
      (e.g., ``"Article 10(2)(b)"`` of the India-US DTAA, the
      25% portfolio-dividend cap). NOT the relief article
      (``"Article 25(2)(a)"``), which states no rate at all —
      see :mod:`ibkr_fa.compute.form67` for the full reasoning.
      Filled from the vendored DTAA table for a DTAA country;
      ``None`` otherwise.
    - (10) **Rate of tax as per Double Taxation Avoidance
      Agreement** — DTAA-prescribed rate for this source-country
      pair (often equals the foreign WHT rate at column 6 when
      WHT was withheld at treaty rate). Filled with column (9);
      ``None`` otherwise.
    - (11) **Amount** — relief INR claimed under §90 / §90A per
      Rule 128(5)(i): the lower of (a) the per-source Indian
      tax payable and (b) the eligible foreign tax, where
      eligible foreign tax is determined from the source-currency
      tax rate of each dividend payment. Compute records that
      decision once on :attr:`Form67Entry.treaty_rate_status`
      (#767, #890). Tax of a payment within the DTAA rate remains
      actual Rule-128-converted tax; a payment above it retains the
      conservative INR treaty-cap fallback for that payment alone;
      with no source-currency rate, or withholding that can't be
      tested payment by payment, eligible tax and the credit stay
      ``None``. The compute layer stores that amount on
      :attr:`Form67Entry.eligible_foreign_tax_inr` (not a portal
      column) for every ``WITHIN`` or ``ABOVE`` entry, even when
      column (7) is blank. Then
      ``section_90_credit_inr = min(tax_payable_normal_inr, eligible_foreign_tax_inr)``.
      Note that
      ``section_90_dtaa_rate_percent`` is expressed in
      percentage units (e.g. ``15`` for 15%, ``12.5`` for
      12.5%), so the ``/100`` division is required to get a
      decimal rate. Filled only for a ``WITHIN`` or ``ABOVE``
      entry with column (7) set; ``None`` otherwise.

* **Column (12) Credit claimed under section 91 — Amount** —
  relief INR claimed under §91 (for countries with NO DTAA with
  India): the net foreign tax capped at the configured Indian
  average rate times income. Filled only when the vendored DTAA lookup
  explicitly returns ``NO_DTAA``, ``[form67.section_91]`` is
  supplied and column (7) is set. Otherwise ``None`` (blank, not
  zero), including for every DTAA country.
* **Column (13) Total foreign tax credit claimed** — the actual
  FTC claim that flows through to Schedule TR. Equals
  ``section_90_credit_inr OR section_91_credit_inr`` since a
  given (country, source) is covered by ONE of §90/90A/91, not
  both. ``None`` when neither credit is filled.

Fill status: columns (1), (2), (3) (always DIVIDEND), (4), (5),
(6) are always populated. Columns (7)-(13) are OPTIONAL fields
defaulting to ``None``, which the writers render as an EMPTY cell
("not computed; user must fill"); an INR ``Decimal(0)`` amount renders ``0``.
A zero rate renders ``0.00``. :mod:`ibkr_fa.compute.form67` owns the fill rules.

Filed values (#898): the portal files amounts in whole rupees and rates to
two places, both rounded half-up. :attr:`Form67Entry.filed` derives them through
:func:`form67_filed_values` on each access, for readiness and every writer;
the fields above keep the unrounded values for audit. ``filed`` also carries the
column (9) citation and gives the bare article number the portal files (#1079).

Audit fields (NOT on the portal form; preserved for FSI ⇄ Form 67
reconciliation): :attr:`event_count` (total number of underlying
in-FY WHT events aggregated into this row, INCLUDING refund /
reversal rows that net against paid rows — matches Schedule FSI's
WHT aggregator semantic at
:func:`~ibkr_fa.compute.schedule_fsi.compute_schedule_fsi_from_withholding_tax`).

Treaty-rate evidence (#890): :attr:`highest_payment_rate_percent` is
the highest source-currency rate of any payment, snapped to the treaty
rate within that payment's rounding tolerance. It is set for ``WITHIN``
and ``ABOVE``, and for an ``UNPROVEN`` entry whose above-rate payment
sits beside a refund it can't be told apart from.
:attr:`payment_allocation_unproven` marks that ``UNPROVEN`` entry, and
one whose withholding can't be matched to a single payment.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, Protocol

from ibkr_fa.invariants import require
from ibkr_fa.models._dtaa_article import bare_article_number, require_citation_type
from ibkr_fa.models._identifier_checks import (
    is_cbdt_country_code,
    is_iso_alpha2,
    is_not_india,
    is_portal_country_name,
)
from ibkr_fa.models._row_checks import require_amounts, require_filed_amounts
from ibkr_fa.models.money import optional_whole_rupees, to_whole_rupees

# Hundred — used for the percentage derivation invariant.
_HUNDRED: Final[Decimal] = Decimal("100")

# Form 67 files rates to two decimal places.
_RATE_QUANTUM: Final[Decimal] = Decimal("0.01")
_ZERO_RATE: Final[Decimal] = Decimal("0.00")

_DTAA_ARTICLE_FIELD: Final[str] = "Form67Entry.section_90_dtaa_article (col 9)"


class Form67SourceOfIncome(StrEnum):
    """The ten "Source of income" rows enumerated on Form 67 (Part A,
    item 5, column 3).

    Verbatim from the official PDF retrieved 2026-06-04 from the
    archive.org Wayback Machine snapshot 20230629195202 of
    ``incometaxindia.gov.in/forms/income-tax%20rules/itr62form67.pdf``.

    NOTE: this enum is DISTINCT from
    :class:`~ibkr_fa.models.schedule_fsi_row.HeadOfIncome` (the
    ITR-2 head taxonomy). ITR-2 uses broader buckets ("Other
    Sources" covers Dividend + Interest + Royalty + FTS + ...);
    Form 67 splits them out separately. The compute layer translates
    when materialising rows.

    v1 scope: only :attr:`DIVIDEND` is emitted (matches the
    Dividends-section WHT-only limitation tracked at v1).
    """

    SALARY = "Salary"
    HOUSE_PROPERTY = "House property"
    BUSINESS_INCOME = "Business/professional income"
    LONG_TERM_CAPITAL_GAIN = "Long term capital gain"
    SHORT_TERM_CAPITAL_GAIN = "Short term capital gain"
    INTEREST_INCOME = "Interest income"
    DIVIDEND = "Dividend"
    ROYALTY = "Royalty not being part of business income"
    FEES_FOR_TECHNICAL_SERVICES = "Fees for technical services not being part of business income"
    OTHERS = "Others (specify)"


class Form67TreatyRateStatus(StrEnum):
    """Whether a DTAA entry's withholding is within the treaty rate (#767).

    Compute decides it once and every reader takes it from the entry. The
    test is per dividend payment, before refunds (#890): a payment is the
    in-FY dividend lines and paid withholding rows sharing (source account,
    ISIN, payment date).

    * :attr:`WITHIN`: no payment's (snapped) source rate is above the treaty
      rate, so all foreign tax is eligible.
    * :attr:`ABOVE`: some payment is above it and the country has no refund
      or reversal; eligible tax caps each above-rate payment at the legacy
      INR treaty cap.
    * :attr:`UNPROVEN`: either no country source rate can be established,
      for example because a same-year refund netted the source-currency tax
      to zero (#593), or the entry's ``payment_allocation_unproven`` is set
      because the withholding can't be tested payment by payment. Eligible
      tax and the credit stay ``None``.
    """

    WITHIN = "within"
    ABOVE = "above"
    UNPROVEN = "unproven"


def _rate_above(rate: Decimal | None, treaty_rate: Decimal | None) -> bool | None:
    """``rate > treaty_rate``, or ``None`` when either is missing or unordered."""
    if rate is None or treaty_rate is None:
        return None
    try:
        if rate > treaty_rate:
            return True
        if rate <= treaty_rate:
            return False
    except (InvalidOperation, TypeError):
        return None
    return None


def require_treaty_rate_status(
    owner: str,
    *,
    treaty_rate_status: Form67TreatyRateStatus | None,
    section_90_dtaa_rate_percent: Decimal | None,
    tax_paid_rate_percent: Decimal | None,
    highest_payment_rate_percent: Decimal | None,
    payment_allocation_unproven: bool,
    eligible_foreign_tax_inr: Decimal | None,
    foreign_tax_inr: Decimal,
    section_90_credit_inr: Decimal | None,
    total_credit_inr: Decimal | None,
) -> None:
    """Pin ``owner.treaty_rate_status`` to the fields it decides (#767, #890).

    Shared by :class:`Form67Entry` and the run report's per-entry row, so a
    reader can trust the status without comparing rates itself. ``WITHIN``
    and ``ABOVE`` are checked against the highest per-payment rate; the
    country rate in ``tax_paid_rate_percent`` is only required to be set.
    """
    require(
        treaty_rate_status is None or isinstance(treaty_rate_status, Form67TreatyRateStatus),
        what=f"{owner}.treaty_rate_status must be a Form67TreatyRateStatus member or None",
        treaty_rate_status=treaty_rate_status,
    )
    require(
        (treaty_rate_status is None) == (section_90_dtaa_rate_percent is None),
        what=(
            f"{owner}.treaty_rate_status must be set exactly when "
            "section_90_dtaa_rate_percent is set"
        ),
        treaty_rate_status=treaty_rate_status,
        section_90_dtaa_rate_percent=section_90_dtaa_rate_percent,
    )
    require(
        payment_allocation_unproven is False
        or (
            payment_allocation_unproven is True
            and treaty_rate_status is Form67TreatyRateStatus.UNPROVEN
        ),
        what=(
            f"{owner}.treaty_rate_status must be UNPROVEN when payment_allocation_unproven "
            "is set, and payment_allocation_unproven must be a bool"
        ),
        treaty_rate_status=treaty_rate_status,
        payment_allocation_unproven=payment_allocation_unproven,
    )
    if highest_payment_rate_percent is not None:
        require(
            isinstance(highest_payment_rate_percent, Decimal)
            and highest_payment_rate_percent.is_finite()
            and Decimal("0") < highest_payment_rate_percent <= _HUNDRED,
            what=(
                f"{owner}.highest_payment_rate_percent must be a finite Decimal "
                "in (0, 100] when provided"
            ),
            highest_payment_rate_percent=highest_payment_rate_percent,
        )
    highest_above = _rate_above(highest_payment_rate_percent, section_90_dtaa_rate_percent)
    if treaty_rate_status is None:
        require(
            highest_payment_rate_percent is None,
            what=(
                f"{owner}.treaty_rate_status None (no treaty rate) needs "
                "highest_payment_rate_percent to be None"
            ),
            highest_payment_rate_percent=highest_payment_rate_percent,
        )
    elif treaty_rate_status is Form67TreatyRateStatus.UNPROVEN:
        require(
            (tax_paid_rate_percent is not None) == payment_allocation_unproven
            and (
                highest_payment_rate_percent is None
                or (payment_allocation_unproven and highest_above is True)
            )
            and eligible_foreign_tax_inr is None
            and section_90_credit_inr is None
            and total_credit_inr is None,
            what=(
                f"{owner}.treaty_rate_status UNPROVEN leaves eligible_foreign_tax_inr, "
                "section_90_credit_inr and total_credit_inr None. Without "
                "payment_allocation_unproven it means no source-currency rate, so "
                "tax_paid_rate_percent and highest_payment_rate_percent are None too; with it, "
                "tax_paid_rate_percent is set and highest_payment_rate_percent is None or "
                "above section_90_dtaa_rate_percent"
            ),
            payment_allocation_unproven=payment_allocation_unproven,
            tax_paid_rate_percent=tax_paid_rate_percent,
            highest_payment_rate_percent=highest_payment_rate_percent,
            section_90_dtaa_rate_percent=section_90_dtaa_rate_percent,
            eligible_foreign_tax_inr=eligible_foreign_tax_inr,
            section_90_credit_inr=section_90_credit_inr,
            total_credit_inr=total_credit_inr,
        )
    else:
        within = treaty_rate_status is Form67TreatyRateStatus.WITHIN
        require(
            tax_paid_rate_percent is not None and highest_above is (not within),
            what=(
                f"{owner}.treaty_rate_status {treaty_rate_status.name} needs "
                "tax_paid_rate_percent and highest_payment_rate_percent, the latter at most "
                "section_90_dtaa_rate_percent for WITHIN and above it for ABOVE"
            ),
            tax_paid_rate_percent=tax_paid_rate_percent,
            highest_payment_rate_percent=highest_payment_rate_percent,
            section_90_dtaa_rate_percent=section_90_dtaa_rate_percent,
        )
        try:
            eligible_matches = eligible_foreign_tax_inr is not None and (
                not within or eligible_foreign_tax_inr == foreign_tax_inr
            )
        except (InvalidOperation, TypeError):
            eligible_matches = False
        require(
            eligible_matches,
            what=(
                f"{owner}.treaty_rate_status {treaty_rate_status.name} needs "
                "eligible_foreign_tax_inr, equal to foreign_tax_inr for WITHIN"
            ),
            eligible_foreign_tax_inr=eligible_foreign_tax_inr,
            foreign_tax_inr=foreign_tax_inr,
        )


@dataclass(frozen=True, slots=True)
class Form67FiledValues:
    """What Form 67 files for one row (#898): amounts in whole rupees and rates
    to two places, both rounded half-up, and the column (9) DTAA citation.

    Every writer, report and readiness check reads these, and none of them
    rounds. ``tax_paid_rate_percent`` is column (6): the source-currency rate
    when proven, otherwise the INR ratio. A column the row leaves blank is
    ``None``. Column (9) files :attr:`section_90_dtaa_article_number` (#1079).
    """

    income_inr: Decimal
    foreign_tax_inr: Decimal
    tax_paid_rate_percent: Decimal
    tax_payable_normal_inr: Decimal | None
    tax_payable_special_inr: Decimal | None
    section_90_dtaa_article: str | None
    section_90_dtaa_rate_percent: Decimal | None
    section_90_credit_inr: Decimal | None
    section_91_credit_inr: Decimal | None
    total_credit_inr: Decimal | None

    @property
    def section_90_dtaa_article_number(self) -> str | None:
        """Column (9): the citation's bare article number, ``None`` without one.

        Refuses, when read, a citation not shaped like ``Article 10(2)(b)``.
        """
        return bare_article_number(
            self.section_90_dtaa_article,
            field="Form67FiledValues.section_90_dtaa_article (col 9)",
        )

    @property
    def tax_paid_rate_is_fractional(self) -> bool:
        """Column (6) has a fractional part, which the portal's CSV importer may
        blank, so the filer enters it by hand after upload (#417, #1447)."""
        return self.tax_paid_rate_percent != self.tax_paid_rate_percent.to_integral_value()

    @property
    def claimed_credit_inr(self) -> Decimal | None:
        """The filed section 90 credit, else the filed section 91 credit."""
        if self.section_90_credit_inr is not None:
            return self.section_90_credit_inr
        return self.section_91_credit_inr


class Form67Values(Protocol):
    """The unrounded Form 67 values :func:`form67_filed_values` rounds."""

    income_inr: Decimal
    foreign_tax_inr: Decimal
    foreign_tax_rate_percent: Decimal
    tax_paid_rate_percent: Decimal | None
    tax_payable_normal_inr: Decimal | None
    tax_payable_special_inr: Decimal | None
    section_90_dtaa_article: str | None
    section_90_dtaa_rate_percent: Decimal | None
    section_90_credit_inr: Decimal | None
    section_91_credit_inr: Decimal | None
    total_credit_inr: Decimal | None


def _two_place_rate(value: Decimal) -> Decimal:
    """``value`` rounded half-up to two places, never ``-0.00``.

    A rate too large to round raises :class:`decimal.InvalidOperation`, which
    the CLI refuses as too large to compute (#832).
    """
    rounded = value.quantize(_RATE_QUANTUM, rounding=ROUND_HALF_UP)
    return rounded if rounded else _ZERO_RATE


def form67_filed_values(row: Form67Values) -> Form67FiledValues:
    """Round ``row``'s values to what Form 67 files, and carry its DTAA citation.

    Refuses, with an :class:`~ibkr_fa.exceptions.InvariantError` naming the
    field, an amount that isn't a finite :class:`Decimal` within the CBDT
    whole-rupee bound or a rate that isn't a finite :class:`Decimal`.
    """
    require_filed_amounts(row, "income_inr", "foreign_tax_inr")
    require_filed_amounts(
        row,
        "tax_payable_normal_inr",
        "tax_payable_special_inr",
        "section_90_credit_inr",
        "section_91_credit_inr",
        "total_credit_inr",
        optional=True,
    )
    require_amounts(row, "foreign_tax_rate_percent")
    require_amounts(row, "tax_paid_rate_percent", "section_90_dtaa_rate_percent", optional=True)
    paid_rate = (
        row.tax_paid_rate_percent
        if row.tax_paid_rate_percent is not None
        else row.foreign_tax_rate_percent
    )
    dtaa_rate = row.section_90_dtaa_rate_percent
    return Form67FiledValues(
        income_inr=to_whole_rupees(row.income_inr),
        foreign_tax_inr=to_whole_rupees(row.foreign_tax_inr),
        tax_paid_rate_percent=_two_place_rate(paid_rate),
        tax_payable_normal_inr=optional_whole_rupees(row.tax_payable_normal_inr),
        tax_payable_special_inr=optional_whole_rupees(row.tax_payable_special_inr),
        section_90_dtaa_article=row.section_90_dtaa_article,
        section_90_dtaa_rate_percent=None if dtaa_rate is None else _two_place_rate(dtaa_rate),
        section_90_credit_inr=optional_whole_rupees(row.section_90_credit_inr),
        section_91_credit_inr=optional_whole_rupees(row.section_91_credit_inr),
        total_credit_inr=optional_whole_rupees(row.total_credit_inr),
    )


@dataclass(frozen=True, slots=True)
class Form67Entry:
    """One per-(country, source-of-income) Form 67 row claiming FTC.

    See module docstring for column ⇄ field mapping and fill
    status.

    Construction-time invariants (see :meth:`__post_init__`):

    * ``serial_number >= 1`` — 1-indexed per the portal form.
    * ``cbdt_country_code`` is a 1-4 digit ASCII-decimal numeric
      string.
    * ``country_name_portal`` is the verbatim ALL-CAPS portal
      display name; non-empty.
    * ``iso_alpha2`` is a 2-letter uppercase ASCII string (ISO
      3166-1 alpha-2).
    * ``cbdt_country_code != "91"`` AND ``iso_alpha2 != "IN"`` —
      Form 67 is foreign-tax-only.
    * ``source_of_income`` is a :class:`Form67SourceOfIncome`
      enum member.
    * ``income_inr > 0`` — Form 67 cannot claim relief on a
      zero-income source.
    * ``foreign_tax_inr > 0`` — Form 67 entries are NET-positive
      per source; refund-only rows are dropped upstream.
    * ``foreign_tax_rate_percent > 0`` and equals
      ``foreign_tax_inr / income_inr * 100`` to a tolerance.
    * ``event_count >= 1`` — at least one underlying WHT event.
    * Optional fields default ``None`` and, when
      populated, must satisfy:
      - ``tax_payable_normal_inr >= 0``
      - ``tax_payable_special_inr >= 0``
      - ``section_90_dtaa_article`` is non-empty when populated, and
        shaped like ``Article 10(2)(b)`` (checked last)
      - ``section_90_dtaa_rate_percent >= 0`` when populated
      - ``0 <= eligible_foreign_tax_inr <= foreign_tax_inr`` when
        populated
      - ``treaty_rate_status`` is a :class:`Form67TreatyRateStatus`,
        set exactly when ``section_90_dtaa_rate_percent`` is, and
        agrees with the fields it decides, including
        ``highest_payment_rate_percent`` and
        ``payment_allocation_unproven`` (see
        :func:`require_treaty_rate_status`)
      - ``section_90_credit_inr >= 0`` and ``<= foreign_tax_inr``
      - ``section_90_credit_inr`` requires ``eligible_foreign_tax_inr``
        and is ``<=`` it
      - ``section_90_credit_inr <= tax_payable_normal_inr`` when both
        are populated
      - ``section_91_credit_inr >= 0`` and ``<= foreign_tax_inr``
      - At most ONE of ``section_90_credit_inr`` /
        ``section_91_credit_inr`` is non-None (§90/§90A and §91
        are mutually exclusive per source-country)
      - ``total_credit_inr`` equals whichever of §90/§90A or §91
        credit is non-None (or zero when both are None)
      - ``total_credit_inr <= foreign_tax_inr``
    * Every filed amount (income, foreign tax, tax payable and the
      credits) is a finite :class:`~decimal.Decimal` that rounds
      HALF_UP within the CBDT 14-digit bound (#830).

    :attr:`filed` gives the values Form 67 files, rounded from these
    fields each time it is read (#898).
    """

    serial_number: int
    cbdt_country_code: str
    country_name_portal: str
    iso_alpha2: str
    source_of_income: Form67SourceOfIncome
    income_inr: Decimal
    foreign_tax_inr: Decimal
    foreign_tax_rate_percent: Decimal
    event_count: int
    tax_paid_rate_percent: Decimal | None = None
    tax_payable_normal_inr: Decimal | None = None
    tax_payable_special_inr: Decimal | None = None
    section_90_dtaa_article: str | None = None
    section_90_dtaa_rate_percent: Decimal | None = None
    eligible_foreign_tax_inr: Decimal | None = None
    treaty_rate_status: Form67TreatyRateStatus | None = None
    section_90_credit_inr: Decimal | None = None
    section_91_credit_inr: Decimal | None = None
    total_credit_inr: Decimal | None = None
    highest_payment_rate_percent: Decimal | None = None
    payment_allocation_unproven: bool = False

    @property
    def filed(self) -> Form67FiledValues:
        """The whole-rupee amounts, two-place rates and DTAA citation Form 67 files
        for this row."""
        return form67_filed_values(self)

    def __post_init__(self) -> None:  # noqa: PLR0915
        require_filed_amounts(
            self,
            "tax_payable_normal_inr",
            "tax_payable_special_inr",
            "section_90_credit_inr",
            "section_91_credit_inr",
            "total_credit_inr",
            optional=True,
        )
        require_filed_amounts(self, "income_inr", "foreign_tax_inr")
        require(
            self.serial_number >= 1,
            what="Form67Entry.serial_number must be >= 1 (1-indexed per the portal form)",
            serial_number=self.serial_number,
        )
        require(
            is_cbdt_country_code(self.cbdt_country_code),
            what=(
                "Form67Entry.cbdt_country_code must be a 1-4 character "
                "ASCII-decimal numeric string (matches "
                "ScheduleFsiRow / ScheduleTrRow invariant)"
            ),
            cbdt_country_code=self.cbdt_country_code,
        )
        require(
            is_portal_country_name(self.country_name_portal),
            what=(
                "Form67Entry.country_name_portal must be a non-empty ALL-CAPS portal display name"
            ),
            country_name_portal=self.country_name_portal,
            cbdt_country_code=self.cbdt_country_code,
        )
        require(
            is_iso_alpha2(self.iso_alpha2),
            what=(
                "Form67Entry.iso_alpha2 must be a 2-letter uppercase "
                "ASCII string (ISO 3166-1 alpha-2; all four checks "
                "required including isalpha() per PR #217 T4 r4)"
            ),
            iso_alpha2=self.iso_alpha2,
        )
        require(
            is_not_india(self.iso_alpha2, self.cbdt_country_code),
            what=(
                "Form67Entry must NOT carry India — Form 67 is by "
                "definition a statement of foreign tax payments under "
                "§90 / §90A / §91 (taxes paid OUTSIDE India)."
            ),
            iso_alpha2=self.iso_alpha2,
            cbdt_country_code=self.cbdt_country_code,
        )
        require(
            isinstance(self.source_of_income, Form67SourceOfIncome),
            what=(
                "Form67Entry.source_of_income must be a "
                "Form67SourceOfIncome enum member. v1 only emits "
                "DIVIDEND (Dividends-section WHT); other source "
                "types require source-kind tagging on the WHT model "
                "(follow-up)."
            ),
            source_of_income=self.source_of_income,
        )
        require(
            self.income_inr > 0,
            what=(
                "Form67Entry.income_inr must be > 0 (Form 67 cannot "
                "claim relief on a zero-income source)"
            ),
            income_inr=self.income_inr,
        )
        require(
            self.foreign_tax_inr > 0,
            what=(
                "Form67Entry.foreign_tax_inr must be > 0 (Form 67 "
                "entries are NET-positive per (country, source); "
                "refund-only rows are dropped upstream by the WHT "
                "netting layer)"
            ),
            foreign_tax_inr=self.foreign_tax_inr,
        )
        require(
            self.foreign_tax_rate_percent > 0,
            what="Form67Entry.foreign_tax_rate_percent must be > 0",
            foreign_tax_rate_percent=self.foreign_tax_rate_percent,
        )
        # Derived-field invariant: percent = tax / income * 100.
        # Tolerance is generous (1e-4) to absorb floating-Decimal
        # rounding from per-event aggregation; filed derives the
        # two-place rate for the writers.
        expected_percent = (self.foreign_tax_inr / self.income_inr) * _HUNDRED
        require(
            abs(self.foreign_tax_rate_percent - expected_percent) <= Decimal("0.0001"),
            what=(
                "Form67Entry.foreign_tax_rate_percent must equal "
                "(foreign_tax_inr / income_inr) * 100 to a tolerance "
                "of 0.0001 — this is the effective foreign withholding "
                "rate per Form 67's column (6) derivation"
            ),
            foreign_tax_rate_percent=self.foreign_tax_rate_percent,
            expected_percent=expected_percent,
        )
        require(
            self.event_count >= 1,
            what=(
                "Form67Entry.event_count must be >= 1 (at least one "
                "underlying WHT event aggregates into this row)"
            ),
            event_count=self.event_count,
        )
        if self.tax_payable_normal_inr is not None:
            require(
                self.tax_payable_normal_inr >= 0,
                what="Form67Entry.tax_payable_normal_inr must be >= 0 when provided",
                tax_payable_normal_inr=self.tax_payable_normal_inr,
            )
        if self.tax_paid_rate_percent is not None:
            require(
                Decimal("0") < self.tax_paid_rate_percent <= _HUNDRED,
                what="Form67Entry.tax_paid_rate_percent must be in (0, 100] when provided",
                tax_paid_rate_percent=self.tax_paid_rate_percent,
            )
        if self.tax_payable_special_inr is not None:
            require(
                self.tax_payable_special_inr >= 0,
                what="Form67Entry.tax_payable_special_inr must be >= 0 when provided",
                tax_payable_special_inr=self.tax_payable_special_inr,
            )
        require_citation_type(self.section_90_dtaa_article, field=_DTAA_ARTICLE_FIELD)
        if self.section_90_dtaa_article is not None:
            require(
                self.section_90_dtaa_article.strip() != "",
                what=(
                    "Form67Entry.section_90_dtaa_article must be a "
                    "non-empty string when provided (None for v1 "
                    "skeleton)"
                ),
                section_90_dtaa_article=self.section_90_dtaa_article,
            )
        if self.section_90_dtaa_rate_percent is not None:
            require(
                self.section_90_dtaa_rate_percent >= 0,
                what=("Form67Entry.section_90_dtaa_rate_percent must be >= 0 when provided"),
                section_90_dtaa_rate_percent=self.section_90_dtaa_rate_percent,
            )
        require_treaty_rate_status(
            "Form67Entry",
            treaty_rate_status=self.treaty_rate_status,
            section_90_dtaa_rate_percent=self.section_90_dtaa_rate_percent,
            tax_paid_rate_percent=self.tax_paid_rate_percent,
            highest_payment_rate_percent=self.highest_payment_rate_percent,
            payment_allocation_unproven=self.payment_allocation_unproven,
            eligible_foreign_tax_inr=self.eligible_foreign_tax_inr,
            foreign_tax_inr=self.foreign_tax_inr,
            section_90_credit_inr=self.section_90_credit_inr,
            total_credit_inr=self.total_credit_inr,
        )
        require(
            self.eligible_foreign_tax_inr is None
            or Decimal("0") <= self.eligible_foreign_tax_inr <= self.foreign_tax_inr,
            what=(
                "Form67Entry.eligible_foreign_tax_inr must be in [0, foreign_tax_inr] when provided"
            ),
            eligible_foreign_tax_inr=self.eligible_foreign_tax_inr,
            foreign_tax_inr=self.foreign_tax_inr,
        )
        if self.section_90_credit_inr is not None:
            require(
                self.section_90_credit_inr >= 0,
                what=(
                    "Form67Entry.section_90_credit_inr must be >= 0 "
                    "when provided (zero is a legitimate computed "
                    "outcome — same semantic as Schedule TR per PR "
                    "#215 T3 r1 P2)"
                ),
                section_90_credit_inr=self.section_90_credit_inr,
            )
            require(
                self.section_90_credit_inr <= self.foreign_tax_inr,
                what=(
                    "Form67Entry.section_90_credit_inr must be <= "
                    "foreign_tax_inr (the §90/§90A relief is "
                    "upper-bounded by the actual foreign tax paid)"
                ),
                section_90_credit_inr=self.section_90_credit_inr,
                foreign_tax_inr=self.foreign_tax_inr,
            )
            eligible = self.eligible_foreign_tax_inr
            require(
                eligible is not None and self.section_90_credit_inr <= eligible,
                what=(
                    "Form67Entry.section_90_credit_inr (col 11) requires "
                    "eligible_foreign_tax_inr and must be <= it "
                    "(Rule 128(5)(i) caps the credit at eligible foreign tax)"
                ),
                section_90_credit_inr=self.section_90_credit_inr,
                eligible_foreign_tax_inr=eligible,
            )
            tax_payable = self.tax_payable_normal_inr
            require(
                tax_payable is None or self.section_90_credit_inr <= tax_payable,
                what=(
                    "Form67Entry.section_90_credit_inr (col 11) must be <= "
                    "tax_payable_normal_inr (col 7) when col 7 is set "
                    "(Rule 128(5)(i) caps the credit at the Indian tax payable)"
                ),
                section_90_credit_inr=self.section_90_credit_inr,
                tax_payable_normal_inr=tax_payable,
            )
        if self.section_91_credit_inr is not None:
            require(
                self.section_91_credit_inr >= 0,
                what="Form67Entry.section_91_credit_inr must be >= 0 when provided",
                section_91_credit_inr=self.section_91_credit_inr,
            )
            require(
                self.section_91_credit_inr <= self.foreign_tax_inr,
                what=(
                    "Form67Entry.section_91_credit_inr must be <= "
                    "foreign_tax_inr (the §91 relief is upper-bounded "
                    "by the actual foreign tax paid)"
                ),
                section_91_credit_inr=self.section_91_credit_inr,
                foreign_tax_inr=self.foreign_tax_inr,
            )
        require(
            self.section_90_credit_inr is None or self.section_91_credit_inr is None,
            what=(
                "Form67Entry: at most one of section_90_credit_inr / "
                "section_91_credit_inr may be non-None. A (country, "
                "source) pair is covered by §90/§90A (DTAA countries) "
                "OR §91 (non-DTAA countries), never both — the "
                "applicability is mutually exclusive per the Income-"
                "tax Act."
            ),
            section_90_credit_inr=self.section_90_credit_inr,
            section_91_credit_inr=self.section_91_credit_inr,
        )
        # T3 r5 P3 #1 of PR #221: portal columns (9)/(10)/(11) form
        # one coupled §90/§90A block. A §90 credit (col 11) MUST cite
        # the DTAA article (col 9) AND the DTAA-prescribed rate
        # (col 10) — claiming relief without naming the treaty
        # provision would leave columns 9/10 blank on the portal
        # submission. Step-4/N relief computation must populate all
        # three together.
        if self.section_90_credit_inr is not None:
            require(
                self.section_90_dtaa_article is not None,
                what=(
                    "Form67Entry.section_90_dtaa_article (col 9) "
                    "must be provided whenever section_90_credit_inr "
                    "(col 11) is populated — §90/§90A relief "
                    "requires citing the DTAA article number on the "
                    "portal form (T3 r5 P3 #1 of PR #221)"
                ),
                section_90_credit_inr=self.section_90_credit_inr,
                section_90_dtaa_article=self.section_90_dtaa_article,
            )
            require(
                self.section_90_dtaa_rate_percent is not None,
                what=(
                    "Form67Entry.section_90_dtaa_rate_percent "
                    "(col 10) must be provided whenever "
                    "section_90_credit_inr (col 11) is populated — "
                    "§90/§90A relief requires the DTAA-prescribed "
                    "rate on the portal form (T3 r5 P3 #1 of PR #221)"
                ),
                section_90_credit_inr=self.section_90_credit_inr,
                section_90_dtaa_rate_percent=self.section_90_dtaa_rate_percent,
            )
        # Converse: §91 unilateral relief applies ONLY to non-DTAA
        # countries (Income-tax Act §91). The DTAA columns (9)/(10)
        # have no meaning in that case and the portal expects them
        # blank — reject objects that mix §91 credit with §90 DTAA
        # metadata.
        if self.section_91_credit_inr is not None:
            require(
                self.section_90_dtaa_article is None,
                what=(
                    "Form67Entry.section_90_dtaa_article (col 9) "
                    "must be None when section_91_credit_inr "
                    "(col 12) is populated — §91 unilateral relief "
                    "applies only to NON-DTAA countries, so the "
                    "DTAA article column is not applicable (T3 r5 "
                    "P3 #1 of PR #221)"
                ),
                section_91_credit_inr=self.section_91_credit_inr,
                section_90_dtaa_article=self.section_90_dtaa_article,
            )
            require(
                self.section_90_dtaa_rate_percent is None,
                what=(
                    "Form67Entry.section_90_dtaa_rate_percent "
                    "(col 10) must be None when "
                    "section_91_credit_inr (col 12) is populated — "
                    "§91 unilateral relief applies only to NON-DTAA "
                    "countries, so the DTAA rate column is not "
                    "applicable (T3 r5 P3 #1 of PR #221)"
                ),
                section_91_credit_inr=self.section_91_credit_inr,
                section_90_dtaa_rate_percent=self.section_90_dtaa_rate_percent,
            )
        # T4 r3 P2 of PR #221: symmetric companion to the
        # (total_credit_inr != 0, both branches None) guard below.
        # A caller that populates section_90_credit_inr or
        # section_91_credit_inr but leaves total_credit_inr=None
        # would otherwise pass __post_init__ silently because the
        # entire cross-field block below is wrapped in
        # ``if self.total_credit_inr is not None``. That asymmetry
        # is a latent step-4/N relief-computation bug: the portal
        # serialiser reads total_credit_inr (col 13) directly and
        # would emit None for a populated credit-branch row,
        # silently dropping the FTC claim.
        require(
            not (
                self.total_credit_inr is None
                and (
                    self.section_90_credit_inr is not None or self.section_91_credit_inr is not None
                )
            ),
            what=(
                "Form67Entry.total_credit_inr must be provided "
                "(non-None) whenever section_90_credit_inr or "
                "section_91_credit_inr is populated — the portal "
                "writes col 13 (Total FTC) directly from "
                "total_credit_inr, so a populated source column "
                "without a total would silently drop the FTC claim"
            ),
            section_90_credit_inr=self.section_90_credit_inr,
            section_91_credit_inr=self.section_91_credit_inr,
            total_credit_inr=self.total_credit_inr,
        )
        if self.total_credit_inr is not None:
            require(
                self.total_credit_inr >= 0,
                what="Form67Entry.total_credit_inr must be >= 0 when provided",
                total_credit_inr=self.total_credit_inr,
            )
            require(
                self.total_credit_inr <= self.foreign_tax_inr,
                what=(
                    "Form67Entry.total_credit_inr must be <= "
                    "foreign_tax_inr (the total FTC claimed is "
                    "upper-bounded by the actual foreign tax paid)"
                ),
                total_credit_inr=self.total_credit_inr,
                foreign_tax_inr=self.foreign_tax_inr,
            )
            # Cross-field consistency: total = §90/§90A OR §91
            # (whichever is non-None). When both are None the
            # total must be 0 (no relief computed yet means no
            # credit can be claimed).
            populated_credit = (
                self.section_90_credit_inr
                if self.section_90_credit_inr is not None
                else self.section_91_credit_inr
            )
            if populated_credit is not None:
                require(
                    self.total_credit_inr == populated_credit,
                    what=(
                        "Form67Entry.total_credit_inr must equal the "
                        "non-None of section_90_credit_inr / "
                        "section_91_credit_inr (a (country, source) "
                        "is covered by one branch, so the total IS "
                        "that branch's credit)"
                    ),
                    total_credit_inr=self.total_credit_inr,
                    populated_credit=populated_credit,
                )
            else:
                # T3 r1 P2 of PR #221: total_credit_inr must NOT be
                # populated with a non-zero value when neither §90/§90A
                # nor §91 credit is set. A non-zero total without a
                # legal source column would claim FTC with no
                # statutory basis. Permit zero (e.g., as a placeholder
                # for "computed total = 0" cases).
                require(
                    self.total_credit_inr == 0,
                    what=(
                        "Form67Entry.total_credit_inr must be 0 when "
                        "both section_90_credit_inr and "
                        "section_91_credit_inr are None — a non-zero "
                        "total without a populated source column "
                        "(11) or (12) would claim FTC with no "
                        "statutory basis"
                    ),
                    total_credit_inr=self.total_credit_inr,
                )
        bare_article_number(self.section_90_dtaa_article, field=_DTAA_ARTICLE_FIELD)


__all__ = [
    "Form67Entry",
    "Form67FiledValues",
    "Form67SourceOfIncome",
    "Form67TreatyRateStatus",
    "Form67Values",
    "form67_filed_values",
    "require_treaty_rate_status",
]
