"""One Schedule FSI per-(country, head-of-income) aggregate row.

A :class:`ScheduleFsiRow` is the typed shape of one cell-block of
ITR-2/ITR-3 ``Schedule FSI — Details of Income from outside India and tax
relief`` (verified against both official AY 2026-27 V1.2 utilities in #267;
FAQ entry also available at
https://wmstatic-prd.incometaxindia.gov.in/web/guest/w/schedule_fsi).
The portal form requires resident assessees to disclose foreign
income broken down per country and per head, with the foreign tax
paid in each (country, head) bucket. Schedule TR (relief summary)
+ Form 67 (FTC claim) consume the same per-bucket totals downstream.

Form structure (per the official AY 2026-27 utilities):

* **Country Code**: 1-4-digit CBDT country code. NOT ISO 3166-1
  alpha-2; CBDT uses its own numeric coding from the ITR-2 form's
  Annexure (vendored at
  :data:`~ibkr_fa.reference.country_codes._COUNTRY_CSV`).
* **Taxpayer Identification Number**: the foreign country's TIN
  assigned to the assessee (e.g., US Social Security Number).
  Carried on :attr:`ScheduleFsiRow.taxpayer_id_number`, stamped by
  the pipeline from ``[account.foreign_tins]`` in ``user.toml``
  (keyed by ISO alpha-2); ``None`` when the user supplied none.
* **Head of Income**: the ITR income-head taxonomy. Three heads are
  reachable from supported broker data: ``Other Sources`` (foreign
  dividends and interest), ``Capital Gains`` (foreign stock
  STCG/LTCG) and ``Business or Profession`` (reviewed Robinhood
  option business income). ``Salary`` and ``House Property`` are out
  of scope.
* **Income from outside India**: gross income in INR for this
  (country, head) bucket. Reconciles row-by-row against the same
  source rows in Schedule OS / Schedule CG (single source of
  truth; no double accounting).
* **Tax paid outside India**: foreign withholding tax (for
  dividends + interest) OR foreign capital-gains tax paid abroad
  (for CG) in INR. Net of reversals per
  :class:`~ibkr_fa.models.withholding_tax.WithholdingTax`'s sign
  convention.
* **Tax payable on such income under normal provisions in India**:
  copied from Form 67 for the ``Other Sources`` head when an exact
  source-level amount is supplied; otherwise ``None`` for manual
  calculation.
* **Tax relief available in India**: the §90 / §90A / §91 FTC
  (``min(foreign_tax, indian_tax_on_foreign_income)``, DTAA-capped),
  computed (#36/#37) by reusing Form 67's credit as the single source of
  truth; ``None`` when not provably derivable (left manual-fill).
* **Relevant article of DTAA**: the treaty article behind a §90 credit,
  read from the vendored relief-article reference (#36/#37); ``None`` for
  §91 unilateral relief or when not computed. Form 67 separately carries the
  withholding article beside its treaty-rate field.

Country Code and TIN are outer grouping fields. This module covers the
nested columns (a) Head of Income, (b) Income from outside India, and
(c) Tax paid outside India for every row, plus (d) tax payable,
(e) tax relief, and (f) DTAA article where they are provably
derivable from Form 67's §90/§91 credit (#36/#37 —
:func:`~ibkr_fa.compute.schedule_fsi_relief.enrich_fsi_with_form67_relief`).
TIN is carried on :attr:`ScheduleFsiRow.taxpayer_id_number`, stamped from
``[account.foreign_tins]`` in user.toml.

Aggregation invariant: at most ONE :class:`ScheduleFsiRow` per
``(cbdt_country_code, head_of_income)`` pair within a
:class:`~ibkr_fa.compute.schedule_fsi.ScheduleFsiComputeResult`.
Multiple underlying events (multiple dividends from different US
issuers, multiple CG closes against the same country, etc.) are
summed into a single row per bucket; the writer renders one
portal-form row per :class:`ScheduleFsiRow`.

Issue #35 (foundation: columns a/b/c). Issues #36 (Schedule TR) / #37
(Form 67) added the ``tax_payable_inr`` / ``tax_relief_inr`` /
``dtaa_article`` FTC columns, populated from Form 67's credit and the vendored
relief article.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from ibkr_fa.invariants import require
from ibkr_fa.models._dtaa_article import bare_article_number, require_citation_type
from ibkr_fa.models._identifier_checks import (
    is_cbdt_country_code,
    is_iso_alpha2,
    is_not_india,
    is_portal_country_name,
)
from ibkr_fa.models._row_checks import require_filed_amounts, require_whole_rupee_amounts
from ibkr_fa.models.money import optional_whole_rupees, to_whole_rupees

# ITR-2 e-filing portal labels the Head of Income field with these
# exact strings. The writer renders these verbatim into the FSI CSV
# / XLSX (single source of truth — the portal value is the enum
# value, not a separate label lookup).
_OTHER_SOURCES_LABEL: Final[str] = "Other Sources"
_CAPITAL_GAINS_LABEL: Final[str] = "Capital Gains"
_BUSINESS_PROFESSION_LABEL: Final[str] = "Business or Profession"

_DTAA_ARTICLE_FIELD: Final[str] = "ScheduleFsiRow.dtaa_article"


class HeadOfIncome(StrEnum):
    """ITR-2/ITR-3 head-of-income taxonomy reachable from broker data.

    The forms' full taxonomy is ``{Salary, House Property, Business or
    Profession, Capital Gains, Other Sources}``. Three members are
    reachable from supported broker evidence:

    * :attr:`OTHER_SOURCES` — foreign dividends and foreign-broker
      credit interest. Both head into FSI under "Other Sources" because
      the IT Act §56 residual head catches both.
    * :attr:`CAPITAL_GAINS` — foreign-stock STCG (Schedule CG main
      block, §111A out-of-scope for foreign equity — slab-rated)
      and foreign-stock LTCG (Schedule CG-LTCG block, §112(1)(c)
      proviso — 12.5% post-July-2024 cutover). Both head into FSI
      under "Capital Gains".
    * :attr:`BUSINESS_PROFESSION` — reviewed foreign option business
      income whose exact positive net P&L is available after
      reconstruction.

    ``Salary`` and ``House Property`` remain out of scope.

    The string values are the EXACT portal labels: writers render
    them verbatim into the FSI CSV / XLSX (no separate label
    lookup; the enum value IS the portal label).
    """

    OTHER_SOURCES = _OTHER_SOURCES_LABEL
    CAPITAL_GAINS = _CAPITAL_GAINS_LABEL
    BUSINESS_PROFESSION = _BUSINESS_PROFESSION_LABEL


@dataclass(frozen=True, slots=True)
class ScheduleFsiFiledValues:
    """What Schedule FSI files for one row (#900): whole rupees, rounded half-up,
    and the DTAA citation. An Other Sources row's income is instead its
    Schedule OS rupees (:attr:`ScheduleFsiRow.filed_income_inr`, #1319).

    Every writer, report and Schedule TR read these, and none of them rounds.
    A column the row leaves blank (manual fill) is ``None``. The filing CSV
    files :attr:`dtaa_article_number` (#1079).
    """

    income_inr: Decimal
    foreign_tax_inr: Decimal
    tax_payable_inr: Decimal | None
    tax_relief_inr: Decimal | None
    dtaa_article: str | None

    @property
    def dtaa_article_number(self) -> str | None:
        """The citation's bare article number, ``None`` without one.

        Refuses, when read, a citation not shaped like ``Article 10(2)(b)``.
        """
        return bare_article_number(self.dtaa_article, field="ScheduleFsiFiledValues.dtaa_article")

    @property
    def portal_relief_inr(self) -> Decimal | None:
        """The relief the portal fills itself, ``min(tax paid, tax payable)``.

        ``None`` when the relief or the tax payable is blank: the user fills
        those, so there is nothing to compare.
        """
        if self.tax_relief_inr is None or self.tax_payable_inr is None:
            return None
        return min(self.foreign_tax_inr, self.tax_payable_inr)

    @property
    def portal_overwrites_relief(self) -> bool:
        """Whether the portal's read-only relief cell would replace this relief."""
        portal_relief = self.portal_relief_inr
        return portal_relief is not None and self.tax_relief_inr != portal_relief


@dataclass(frozen=True, slots=True)
class ScheduleFsiRow:
    """One aggregated (country, head) row of ITR-2 Schedule FSI.

    Surfaces the portal-form's columns (a) Head of Income,
    (b) Income from outside India, (c) Tax paid outside India for
    a single ``(country_code, head_of_income)`` bucket. The writer
    renders one portal-form row per instance.

    Construction-time invariants (see :meth:`__post_init__`)
    enforce:

    * ``cbdt_country_code`` is a 1-4 digit numeric string matching
      the format vendored at
      :data:`~ibkr_fa.reference.country_codes._CBDT_CODE_PATTERN`.
    * ``country_name_portal`` is the verbatim ALL-CAPS portal
      display name (e.g., ``"UNITED STATES OF AMERICA"``); non-
      empty.
    * ``iso_alpha2`` is a 2-letter uppercase ASCII string (ISO
      3166-1 alpha-2); preserved for trace-back to the source
      ISIN's leading two characters.
    * ``head_of_income`` is a :class:`HeadOfIncome` member; its raw
      portal label (e.g. ``"Other Sources"``) is refused.
    * ``income_inr > 0`` — Schedule FSI only reports POSITIVE
      foreign income. A zero-income (country, head) bucket has
      nothing to report and must not be materialised. Loss-making
      capital gains aggregate to a NET POSITIVE figure across
      offsetting wins/losses before reaching FSI; per-row losses
      are tracked in Schedule CG but never independently surface
      to FSI as a negative row.
    * ``foreign_tax_inr >= 0`` — foreign withholding tax / capital-
      gains tax paid abroad is always non-negative in aggregate
      (the WHT model carries per-row signed amounts so reversals
      can be netted at compute time, but the aggregate written
      into FSI is the NET non-negative figure). Zero is valid
      (e.g., a Capital Gains row where the user paid no foreign
      tax — common for US accounts with no foreign-source
      withholding on CG events).
    * ``event_count >= 1`` — every materialised row aggregates at
      least one underlying source event.
    * Every filed amount (income, foreign tax, tax payable and relief)
      is a finite :class:`~decimal.Decimal` that rounds HALF_UP within
      the CBDT 14-digit bound (#830).
    * ``dtaa_article``, when set, is shaped like ``Article 10(2)(b)``
      (checked last, #1079).

    Attributes:
        cbdt_country_code: CBDT 1-4-digit numeric country code per the ITR form's
            Annexure (e.g., ``"2"`` for United States, ``"91"``
            for India, ``"44"`` for United Kingdom). Vendored
            mapping lives at
            :mod:`~ibkr_fa.reference.country_codes`. NOT the
            ISO 3166-1 alpha-2 code — CBDT uses its own coding.
            Although every cell here is materialised by looking
            up the ISO alpha-2 → CBDT mapping, the resolved CBDT
            code is the field surfaced to the portal.
        country_name_portal: ALL-CAPS country display name as it
            appears on the CBDT ITR-2 portal dropdown (e.g.,
            ``"UNITED STATES OF AMERICA"``). Carried verbatim
            from
            :attr:`~ibkr_fa.reference.country_codes.CountryCodeEntry.country_name_portal`.
        iso_alpha2: ISO 3166-1 alpha-2 country code (e.g.,
            ``"US"``). Preserved for audit traceability back to
            the source ISIN's leading two characters.
        head_of_income: ITR-2 head taxonomy bucket — see
            :class:`HeadOfIncome`. Within a single
            :class:`~ibkr_fa.compute.schedule_fsi.ScheduleFsiComputeResult`
            at most one row exists per
            ``(cbdt_country_code, head_of_income)`` pair.
        income_inr: Sum of gross income from this (country, head)
            bucket in INR at full :class:`~decimal.Decimal`
            precision. Aggregated from the per-event INR amounts
            produced by Schedule OS / Schedule CG (single source
            of truth — FSI doesn't re-FX-convert; it consumes the
            already-converted amounts). :attr:`filed` gives the
            CBDT-FAQ HALF_UP whole-rupee amount the writers render,
            or :attr:`filed_income_inr` when that is set.
        foreign_tax_inr: Sum of foreign tax paid (withholding or
            capital-gains tax) on this bucket's income in INR at
            full :class:`~decimal.Decimal` precision. Always non-
            negative (reversals are netted before reaching this
            field). Zero is valid (e.g., a CG row with no
            foreign-source tax). :attr:`filed` gives the CBDT-FAQ
            HALF_UP whole-rupee amount the writers render. Per the v1
            architecture, the FX conversion for this column
            follows Rule 128(5)(ii) (preceding-month-end TTBR per
            :class:`~ibkr_fa.fx.policy_rule128.Rule128Policy`) —
            different from the Rule 115 dividend / Rule 115(1),
            Explanation (2)(f), capital-gains FX policies used
            for the income column above. The two columns can
            therefore use different FX-publication dates for the
            same event, by design; the auditor can reconcile via
            the per-row audit blocks in the upstream Schedule
            OS / CG writer outputs.
        event_count: Number of underlying source events
            aggregated into this row (count of dividends + WHT
            rows + CG closes etc. that contributed). Surfaced for
            audit reconciliation against the upstream Schedule
            OS / CG row counts; the writer renders it in the
            audit-block detail, NOT in the portal-form output.
    """

    cbdt_country_code: str
    country_name_portal: str
    iso_alpha2: str
    head_of_income: HeadOfIncome
    income_inr: Decimal
    foreign_tax_inr: Decimal
    event_count: int
    # FTC columns (f)/(g)/(h). Populated (issue #36/#37) by joining the
    # Form 67 §90/§91 credit for this bucket; ``None`` when not computable
    # (unsupported relief path or a head/source whose FTC v1 doesn't compute
    # — e.g. foreign interest or Capital-Gains special-rate tax) so
    # the column stays manual-fill rather than being fabricated.
    tax_payable_inr: Decimal | None = None
    tax_relief_inr: Decimal | None = None
    dtaa_article: str | None = None
    # The foreign country's Taxpayer Identification Number assigned to the
    # assessee (e.g. a US SSN/ITIN), stamped by the pipeline from
    # ``[account.foreign_tins]`` in user.toml keyed by :attr:`iso_alpha2`.
    # ``None`` when the user supplied no TIN for this country (manual-fill).
    taxpayer_id_number: str | None = None
    # The whole rupees an Other Sources row files as income: its country's
    # shares of the filed Schedule OS fields it comes from, set by
    # compute_schedule_fsi (#1319). ``None`` files the HALF_UP of income_inr.
    filed_income_inr: Decimal | None = None
    # What the manual fill guidance says about this Other Sources row's blank
    # Form 67 DIVIDEND row in place of its generic override sentence, set by
    # enrich_fsi_with_form67_relief (#1469). Empty keeps that sentence.
    override_advice: tuple[str, ...] = ()

    @property
    def filed(self) -> ScheduleFsiFiledValues:
        """The whole-rupee amounts and DTAA citation Schedule FSI files for this row."""
        return ScheduleFsiFiledValues(
            income_inr=(
                to_whole_rupees(self.income_inr)
                if self.filed_income_inr is None
                else self.filed_income_inr
            ),
            foreign_tax_inr=to_whole_rupees(self.foreign_tax_inr),
            tax_payable_inr=optional_whole_rupees(self.tax_payable_inr),
            tax_relief_inr=optional_whole_rupees(self.tax_relief_inr),
            dtaa_article=self.dtaa_article,
        )

    @property
    def needs_indian_tax_payable(self) -> bool:
        """Whether the filer must type this row's Indian tax payable by hand: the
        row has foreign tax (``foreign_tax_inr > 0``) but no ``tax_payable_inr``.

        Compared at full precision, not on :attr:`filed`. It sets the Schedule
        FSI and TR run reports' ``has_unresolved_indian_tax_payable`` (TR's only
        for the countries it emits) and adds the FSI worksheet's manual fill
        guidance row under the tax payable cell.
        """
        return self.foreign_tax_inr > 0 and self.tax_payable_inr is None

    def __post_init__(self) -> None:
        require_filed_amounts(self, "tax_payable_inr", "tax_relief_inr", optional=True)
        require_filed_amounts(self, "income_inr", "foreign_tax_inr")
        require(
            isinstance(self.head_of_income, HeadOfIncome),
            what="ScheduleFsiRow.head_of_income must be a HeadOfIncome enum member",
            head_of_income=self.head_of_income,
        )
        require(
            is_cbdt_country_code(self.cbdt_country_code),
            what=(
                "ScheduleFsiRow.cbdt_country_code must be a 1-4 character "
                "ASCII-decimal numeric string (CBDT ITR-2 country-code "
                "format). Both isascii() AND isdecimal() are required: "
                "isdecimal() alone would accept non-ASCII decimals like "
                "fullwidth or Arabic-Indic digits that bypass the India "
                "sentinel ASCII '91' below; isdigit() would additionally "
                "accept Unicode superscript / subscript digits."
            ),
            cbdt_country_code=self.cbdt_country_code,
        )
        require(
            is_portal_country_name(self.country_name_portal),
            what=(
                "ScheduleFsiRow.country_name_portal must be a non-empty "
                "ALL-CAPS portal display name (the writer renders it "
                "verbatim — mixed-case or whitespace-only names indicate "
                "a corrupted upstream country-codes vendor row)"
            ),
            country_name_portal=self.country_name_portal,
            cbdt_country_code=self.cbdt_country_code,
        )
        require(
            is_iso_alpha2(self.iso_alpha2),
            what=(
                "ScheduleFsiRow.iso_alpha2 must be a 2-letter uppercase "
                "ASCII string (ISO 3166-1 alpha-2)"
            ),
            iso_alpha2=self.iso_alpha2,
        )
        require(
            is_not_india(self.iso_alpha2, self.cbdt_country_code),
            what=(
                "ScheduleFsiRow must NOT carry India — Schedule FSI is "
                "by definition 'income from outside India' (CBDT portal "
                "https://wmstatic-prd.incometaxindia.gov.in/web/guest/w"
                "/schedule_fsi). Indian-source income belongs in "
                "Schedule OS row 1a(i) / Schedule CG (Indian shares), "
                "not Schedule FSI. The compute layer already filters "
                "Indian-source dividends via ScheduleOsDividendLine; "
                "this guard prevents future CG / interest aggregators "
                "or direct construction from bypassing that filter."
            ),
            iso_alpha2=self.iso_alpha2,
            cbdt_country_code=self.cbdt_country_code,
        )
        require(
            self.income_inr > 0,
            what=(
                "ScheduleFsiRow.income_inr must be positive (Schedule FSI "
                "only reports positive foreign income; zero-income (country, "
                "head) buckets must not be materialised)"
            ),
            income_inr=self.income_inr,
            cbdt_country_code=self.cbdt_country_code,
            head_of_income=self.head_of_income,
        )
        require(
            self.foreign_tax_inr >= 0,
            what=(
                "ScheduleFsiRow.foreign_tax_inr must be non-negative "
                "(reversals are netted before reaching FSI aggregation)"
            ),
            foreign_tax_inr=self.foreign_tax_inr,
            cbdt_country_code=self.cbdt_country_code,
            head_of_income=self.head_of_income,
        )
        require(
            self.event_count >= 1,
            what=(
                "ScheduleFsiRow.event_count must be at least 1 "
                "(every materialised row aggregates at least one event)"
            ),
            event_count=self.event_count,
            cbdt_country_code=self.cbdt_country_code,
            head_of_income=self.head_of_income,
        )
        require(
            self.tax_payable_inr is None or self.tax_payable_inr >= 0,
            what="ScheduleFsiRow.tax_payable_inr must be non-negative when populated",
            tax_payable_inr=self.tax_payable_inr,
            cbdt_country_code=self.cbdt_country_code,
        )
        # The §90/§91 foreign-tax credit can never exceed the foreign tax
        # actually paid on the bucket — the critical guard against an
        # OVERSTATED relief figure (the dangerous direction for a tax tool).
        require(
            self.tax_relief_inr is None or (0 <= self.tax_relief_inr <= self.foreign_tax_inr),
            what=(
                "ScheduleFsiRow.tax_relief_inr must be in [0, foreign_tax_inr] "
                "when populated — the FTC cannot exceed the foreign tax paid"
            ),
            tax_relief_inr=self.tax_relief_inr,
            foreign_tax_inr=self.foreign_tax_inr,
            cbdt_country_code=self.cbdt_country_code,
        )
        require_whole_rupee_amounts(self, "filed_income_inr", optional=True)
        require(
            self.filed_income_inr is None
            or (self.head_of_income is HeadOfIncome.OTHER_SOURCES and self.filed_income_inr >= 0),
            what="ScheduleFsiRow.filed_income_inr must be non-negative on an Other Sources row",
            filed_income_inr=self.filed_income_inr,
            head_of_income=self.head_of_income,
        )
        require_citation_type(self.dtaa_article, field=_DTAA_ARTICLE_FIELD)
        require(
            self.dtaa_article is None or self.dtaa_article.strip() != "",
            what="ScheduleFsiRow.dtaa_article must be a non-empty string when populated",
            dtaa_article=self.dtaa_article,
            cbdt_country_code=self.cbdt_country_code,
        )
        require(
            self.taxpayer_id_number is None or self.taxpayer_id_number.strip() != "",
            what="ScheduleFsiRow.taxpayer_id_number must be a non-empty string when populated",
            taxpayer_id_number=self.taxpayer_id_number,
            cbdt_country_code=self.cbdt_country_code,
        )
        bare_article_number(self.dtaa_article, field=_DTAA_ARTICLE_FIELD)
