"""One Schedule TR per-country aggregate row.

A :class:`ScheduleTrRow` is the typed shape of one cell-block of
ITR-2/ITR-3 ``Schedule TR — Summary of tax relief claimed for taxes paid
outside India`` (verified against both official AY 2026-27 V1.2 utilities
in #267; FAQ entry also available at
https://wmstatic-prd.incometaxindia.gov.in/web/guest/w/schedule_tr).
The portal form requires resident assessees who have paid taxes
outside India and are claiming tax relief in India under
§90 / §90A / §91 of the Income-tax Act, 1961 to submit a per-
country summary that references the per-(country, head) breakdown
already disclosed on Schedule FSI.

Form structure (per the official AY 2026-27 utilities):

* **Country Code**: 1-4-digit CBDT country code (NOT ISO 3166-1
  alpha-2; CBDT uses its own numeric coding). Identical to
  Schedule FSI's Country Code.
* **Taxpayer Identification Number**: the foreign country's TIN
  assigned to the assessee. Carried on
  :attr:`ScheduleTrRow.taxpayer_id_number`, copied from the matching
  Schedule FSI rows (stamped from ``[account.foreign_tins]`` in
  ``user.toml``); ``None`` when the user supplied none.
* **Total tax paid outside India**: sum of Schedule FSI's
  ``foreign_tax_inr`` for this country (across all heads of
  income — Schedule TR collapses the head dimension that Schedule
  FSI carries separately).
* **Total tax relief available in India**: the sum of each
  contributing Schedule FSI row's relief, rounded to whole rupees
  per row (FSI relief is Form 67's §90 / §91 credit). OPTIONAL on
  this row; ``None`` makes the writer emit an EMPTY cell ("not
  computed; user must fill").
* **Section under which relief claimed**: ``"90"`` / ``"90A"`` /
  ``"91"`` per the Income-tax Act, 1961, set together with the
  relief (the row requires both or neither). OPTIONAL, defaulting
  to ``None``.

Fill status: (a) Country Code and (c) total foreign tax paid INR
(sourced from FSI) are always set. (b) TIN, (d) relief available
and (e) section claimed are OPTIONAL fields defaulting to ``None``,
which the writers render as EMPTY cells per the "not computed;
user must fill" contract from PR #212 (Schedule FSI writers).
:func:`~ibkr_fa.compute.schedule_tr.compute_schedule_tr_from_fsi`
owns when (d) and (e) are filled.

Not modelled (T3 r1 P2 #2): the portal's DTAA-vs-non-DTAA
sub-totals, and the **refund disclosure** rows on Schedule TR. The
portal additionally requires:

* "If any foreign tax (on which relief was allowed in India) is
  REFUNDED by the foreign tax authority during the year ..."
  with refund amount + assessment year in which the relief was
  originally allowed.

This is a per-event sub-table on the portal form, not a
per-country aggregate. This model does NOT model it; the
relevant data isn't typically available from an IBKR Activity
Statement alone (the user must track foreign-tax refunds
separately and add them to the portal form manually). A future
issue would extend this model with a `refunds: list[TaxRefund]`
field once a typed data source is identified.

Aggregation invariant: at most ONE :class:`ScheduleTrRow` per
``cbdt_country_code`` within a
:class:`~ibkr_fa.compute.schedule_tr.ScheduleTrComputeResult`.
Schedule FSI's per-(country, head) rows are collapsed by summing
``foreign_tax_inr`` across heads for each country.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from ibkr_fa.invariants import require
from ibkr_fa.models._identifier_checks import (
    is_cbdt_country_code,
    is_iso_alpha2,
    is_not_india,
    is_portal_country_name,
)
from ibkr_fa.models._row_checks import require_whole_rupee_amounts


class TaxReliefSection(StrEnum):
    """ITR-2 / Income-tax Act, 1961 sections under which foreign tax
    relief is claimed.

    The Indian framework provides three relief pathways:

    * :attr:`SECTION_90` — bilateral relief under a Double Taxation
      Avoidance Agreement (DTAA) that India has notified u/s 90 with
      the source country (most common — US, UK, Canada, Singapore, etc.).
    * :attr:`SECTION_90A` — relief under an Inter-Government
      Agreement (e.g., adopted by Central Government under §90A,
      historically used for the OECD treaty with specified
      territories). Rare in practice for IBKR-account holders.
    * :attr:`SECTION_91` — unilateral relief for income from a country
      with which India has NO DTAA. The relief is the lower of the
      Indian tax rate or the foreign tax rate, applied to the foreign
      income.

    The string values are the EXACT portal labels (e.g., ``"90"``,
    ``"90A"``, ``"91"``); writers render them verbatim into the TR
    CSV / XLSX.
    """

    SECTION_90 = "90"
    SECTION_90A = "90A"
    SECTION_91 = "91"


@dataclass(frozen=True, slots=True)
class ScheduleTrRow:
    """One aggregated per-country row of ITR-2 Schedule TR.

    Schedule TR collapses Schedule FSI's per-(country, head) rows
    into per-country totals — every country whose summed, rounded
    foreign tax is positive surfaces as one TR row, regardless of
    how many heads of income contributed to its foreign-tax pool.

    Construction-time invariants (see :meth:`__post_init__`):

    * ``cbdt_country_code`` is a 1-4 digit numeric string matching
      the format vendored at
      :data:`~ibkr_fa.reference.country_codes._CBDT_CODE_PATTERN`.
    * ``country_name_portal`` is the verbatim ALL-CAPS portal
      display name; non-empty.
    * ``iso_alpha2`` is a 2-letter uppercase ASCII string (ISO
      3166-1 alpha-2); preserved for trace-back to the source
      ISIN's leading two characters.
    * ``cbdt_country_code != "91"`` AND ``iso_alpha2 != "IN"`` —
      Schedule TR is foreign-tax-only; India is structurally
      impossible.
    * ``foreign_tax_inr_total > 0`` — Schedule TR only reports
      countries where the assessee paid POSITIVE foreign tax. A
      country with zero foreign tax has nothing to relieve and
      must not be materialised. This field holds the
      PORTAL-DISPLAYED value (sum of per-FSI-row HALF_UP-rounded
      whole-rupee amounts, matching ITR-2's "total of (c) of
      Schedule FSI in respect of each country" formula).
    * ``foreign_tax_inr_total_full_precision >= 0`` — the full-
      precision sum of FSI's ``foreign_tax_inr`` for this country
      (NOT rounded per row). Preserved for audit reconciliation
      against Schedule FSI. May differ from
      ``foreign_tax_inr_total`` by up to ₹0.50 per contributing
      FSI row in either direction; e.g., two FSI rows of ₹2.50
      give portal=₹6 / full-precision=₹5.00, while two rows of
      ₹1.40 give portal=₹2 / full-precision=₹2.80. The two fields
      have NO ordering invariant between them by construction.
    * ``tax_relief_inr_total`` (if non-None) must be
      ``0 <= tax_relief_inr_total <= foreign_tax_inr_total`` — the
      relief is upper-bounded by the foreign tax actually paid
      under §90/§90A/§91. Zero is a valid value (the engine
      files ``0`` when a country's positive relief rounds to
      zero), distinguished from ``None`` (not computed).
    * ``section_under_which_claimed`` is optional (defaults to
      ``None``) and set together with ``tax_relief_inr_total``.
      When provided, must be a :class:`TaxReliefSection` enum
      value.
    * Both filed amounts (foreign tax and relief) are whole-rupee
      :class:`~decimal.Decimal` values within the CBDT 14-digit bound
      (#830), exactly as :func:`~ibkr_fa.models.money.to_whole_rupees`
      writes them: writers render them as they are (#900).

    Attributes:
        cbdt_country_code: CBDT 1-4-digit numeric country code. NOT the ISO 3166-1
            alpha-2 code — CBDT uses its own coding.
        country_name_portal: ALL-CAPS country display name as it
            appears on the CBDT ITR-2 portal dropdown.
        iso_alpha2: ISO 3166-1 alpha-2 country code. Preserved for
            audit traceability back to the source ISIN's leading
            two characters.
        foreign_tax_inr_total: Sum of per-FSI-row HALF_UP-rounded
            foreign-tax-paid INR amounts for this country across
            all heads of income — the value the ITR-2 portal
            actually displays for column (c) of Schedule TR per
            its "total of (c) of Schedule FSI in respect of each
            country" formula. Always POSITIVE: countries whose summed,
            rounded foreign tax is not positive don't materialise. Per-row
            rounding is applied BEFORE summing (PR #216 T3 r1 P2 #1: two FSI
            rows of ₹2.50 display as ₹3 + ₹3 = ₹6 on the portal,
            NOT round(2.50 + 2.50) = ₹5).
        foreign_tax_inr_total_full_precision: Full-precision sum
            of FSI's ``foreign_tax_inr`` for this country (NOT
            rounded per row). Preserved for FSI ⇄ TR audit
            reconciliation against Schedule FSI's underlying per-
            row breakdown. Always non-negative. NOT used to drive
            any portal-displayed cell.
        tax_relief_inr_total: Tax relief available in India for
            this country, in INR: the sum of each contributing FSI
            row's relief (Form 67's §90 / §91 credit) rounded to
            whole rupees. ``None`` when the compute layer leaves it
            for manual fill; see
            :func:`~ibkr_fa.compute.schedule_tr.compute_schedule_tr_from_fsi`.

            When provided, the value distinguishes two states
            (T3 r1 P2):

            * ``Decimal("0")`` — relief was computed and rounds to
              zero: the engine files ``0`` (with its section) when
              the country's positive relief rounds to zero. A
              country whose relief is all zero before rounding
              (e.g. the assessee's Indian tax on the foreign income
              is ₹0) is left ``None`` instead, because no
              positive-relief row names a section.
            * ``Decimal("> 0")`` — non-zero relief being claimed.

            Both must be ``<= foreign_tax_inr_total`` (relief is
            upper-bounded by the foreign tax actually paid). NOTE
            (T2 r3 P3 followup): the bound is anchored against
            the PORTAL-displayed total (per-row HALF_UP sum),
            NOT against ``foreign_tax_inr_total_full_precision``,
            because the engine sums per-row HALF_UP-rounded relief
            (apples-to-apples with the user's portal entry), NOT
            a rounding of the full-precision FTC computation. A
            switch to a full-precision relief value would need
            this invariant re-anchored to
            ``foreign_tax_inr_total_full_precision`` to avoid
            spurious rejections (e.g., two rows of ₹1.40 give
            portal=₹2 / full=₹2.80 → portal-anchored bound
            would reject a legitimate ₹2.50 relief).
        section_under_which_claimed: ITR-2 section under which
            relief is claimed for this country: ``90`` for a
            treaty (DTAA) credit, ``91`` for unilateral relief.
            Set together with ``tax_relief_inr_total`` and
            ``None`` when it is ``None``. When provided, must be a
            :class:`TaxReliefSection` enum value.
        event_count: Number of underlying Schedule FSI rows
            aggregated into this TR row (one row per
            (cbdt_country_code, head_of_income) bucket in FSI).
            Surfaced for audit reconciliation against Schedule
            FSI's per-row breakdown.
    """

    cbdt_country_code: str
    country_name_portal: str
    iso_alpha2: str
    foreign_tax_inr_total: Decimal
    event_count: int
    tax_relief_inr_total: Decimal | None = None
    section_under_which_claimed: TaxReliefSection | None = None
    # Positioned LAST to honor PR #189 T5 r2 dataclass-ordering
    # convention: new defaulted fields are appended after all
    # existing defaulted fields so legacy positional callers
    # continue to bind correctly. T2 r3 P3 fix to PR #216.
    foreign_tax_inr_total_full_precision: Decimal | None = None
    # The assessee's foreign TIN for this country, copied from the matching
    # Schedule FSI rows (stamped from ``[account.foreign_tins]``). ``None``
    # when the user supplied no TIN for this country (manual-fill).
    taxpayer_id_number: str | None = None

    def __post_init__(self) -> None:
        require_whole_rupee_amounts(self, "tax_relief_inr_total", optional=True)
        require_whole_rupee_amounts(self, "foreign_tax_inr_total")
        require(
            is_cbdt_country_code(self.cbdt_country_code),
            what=(
                "ScheduleTrRow.cbdt_country_code must be a 1-4 character "
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
                "ScheduleTrRow.country_name_portal must be a non-empty "
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
                "ScheduleTrRow.iso_alpha2 must be a 2-letter uppercase "
                "ASCII string (ISO 3166-1 alpha-2). Both isascii() AND "
                "isupper()+isalpha() are required: isupper()+isalpha() "
                "alone would accept non-ASCII uppercase letters (e.g., "
                "'ÄÖ') that aren't valid ISO 3166-1 codes. T4 r1 nit "
                "fix to PR #215."
            ),
            iso_alpha2=self.iso_alpha2,
        )
        require(
            is_not_india(self.iso_alpha2, self.cbdt_country_code),
            what=(
                "ScheduleTrRow must NOT carry India — Schedule TR is "
                "by definition a summary of taxes paid OUTSIDE India "
                "under §90 / §90A / §91 (CBDT portal "
                "https://wmstatic-prd.incometaxindia.gov.in/web/guest"
                "/w/schedule_tr). Indian-source income belongs in "
                "Schedule OS / Schedule CG, not Schedule TR."
            ),
            iso_alpha2=self.iso_alpha2,
            cbdt_country_code=self.cbdt_country_code,
        )
        require(
            self.foreign_tax_inr_total > 0,
            what=(
                "ScheduleTrRow.foreign_tax_inr_total must be positive "
                "(Schedule TR only reports countries where the assessee "
                "paid positive foreign tax; zero-tax countries have "
                "nothing to relieve and must not be materialised)"
            ),
            foreign_tax_inr_total=self.foreign_tax_inr_total,
            cbdt_country_code=self.cbdt_country_code,
        )
        if self.foreign_tax_inr_total_full_precision is not None:
            require(
                self.foreign_tax_inr_total_full_precision >= 0,
                what=(
                    "ScheduleTrRow.foreign_tax_inr_total_full_precision "
                    "must be non-negative when provided. This field is "
                    "the audit-only full-precision sum (NOT rounded "
                    "per FSI row); it does NOT drive any portal-"
                    "displayed cell. ``None`` is legitimate for legacy "
                    "direct constructors that don't track full "
                    "precision separately — the audit cell then falls "
                    "back to foreign_tax_inr_total. T2/T3 r2 fix to "
                    "PR #216."
                ),
                foreign_tax_inr_total_full_precision=(self.foreign_tax_inr_total_full_precision),
                cbdt_country_code=self.cbdt_country_code,
            )
        require(
            self.event_count >= 1,
            what=(
                "ScheduleTrRow.event_count must be >= 1 (every row "
                "aggregates at least one underlying Schedule FSI row)"
            ),
            event_count=self.event_count,
            cbdt_country_code=self.cbdt_country_code,
        )
        if self.tax_relief_inr_total is not None:
            require(
                self.tax_relief_inr_total >= 0,
                what=(
                    "ScheduleTrRow.tax_relief_inr_total must be "
                    "non-negative when provided. Zero is a legitimate "
                    "computed outcome (FTC = min(foreign tax, Indian "
                    "tax on foreign income) = 0 when Indian tax is "
                    "zero — e.g., foreign income exempt domestically "
                    "or absorbed by carry-forward losses); the user "
                    "must still disclose the country on Schedule TR. "
                    "T3 r1 P2 fix to PR #215: distinguished 'computed "
                    "zero' (Decimal(0)) from 'not computed' (None)."
                ),
                tax_relief_inr_total=self.tax_relief_inr_total,
                cbdt_country_code=self.cbdt_country_code,
            )
            require(
                self.tax_relief_inr_total <= self.foreign_tax_inr_total,
                what=(
                    "ScheduleTrRow.tax_relief_inr_total must be "
                    "<= foreign_tax_inr_total — the relief is upper-"
                    "bounded by the foreign tax actually paid (§90/§91 "
                    "relief = min(foreign tax, Indian tax payable on "
                    "foreign income); both upper bounds are <= foreign "
                    "tax paid by construction)"
                ),
                tax_relief_inr_total=self.tax_relief_inr_total,
                foreign_tax_inr_total=self.foreign_tax_inr_total,
                cbdt_country_code=self.cbdt_country_code,
            )
        if self.section_under_which_claimed is not None:
            require(
                isinstance(self.section_under_which_claimed, TaxReliefSection),
                what=(
                    "ScheduleTrRow.section_under_which_claimed must be "
                    "a TaxReliefSection enum member ('90', '90A', or "
                    "'91'). The Income-tax Act, 1961 defines exactly "
                    "these three relief pathways; static typing alone "
                    "is insufficient because a caller could pass a "
                    "raw string like '91A' that StrEnum-equality would "
                    "silently accept without enum membership (T3 r1 "
                    "P3 fix to PR #215)."
                ),
                section_under_which_claimed=self.section_under_which_claimed,
                cbdt_country_code=self.cbdt_country_code,
            )
        # PR #222 fix: symmetric coupling between
        # ``tax_relief_inr_total`` (col c) and
        # ``section_under_which_claimed`` (col d).
        #
        # A populated relief amount without a section reference would
        # render a relief value with a blank "Section under which
        # relief claimed" cell — Schedule TR cannot claim §90 / §90A
        # / §91 relief without naming which statutory provision
        # applies. Symmetric: a section reference without a relief
        # amount is structurally inconsistent (no claim is being
        # made). Forward direction mirrors the Form67Entry §90 /
        # DTAA coupling at form67_entry.py:433-457 (PR #221 r6).
        relief_populated = self.tax_relief_inr_total is not None
        section_populated = self.section_under_which_claimed is not None
        require(
            relief_populated == section_populated,
            what=(
                "ScheduleTrRow.tax_relief_inr_total and "
                "section_under_which_claimed must both be set or "
                "both be None — a populated relief amount without a "
                "statutory section reference (or vice-versa) is "
                "structurally inconsistent on Schedule TR (parallel "
                "to Form67Entry.section_90_credit_inr ↔ "
                "section_90_dtaa_article coupling at "
                "form67_entry.py:433-457)"
            ),
            tax_relief_inr_total=self.tax_relief_inr_total,
            section_under_which_claimed=self.section_under_which_claimed,
            cbdt_country_code=self.cbdt_country_code,
        )
        require(
            self.taxpayer_id_number is None or self.taxpayer_id_number.strip() != "",
            what="ScheduleTrRow.taxpayer_id_number must be a non-empty string when populated",
            taxpayer_id_number=self.taxpayer_id_number,
            cbdt_country_code=self.cbdt_country_code,
        )


__all__ = ["ScheduleTrRow", "TaxReliefSection"]
