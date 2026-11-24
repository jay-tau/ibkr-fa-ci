"""ITR-2/ITR-3 portal labels — pinned snapshot for AY 2026-27.

This module is the **source of truth** for every portal-facing label,
constraint, and format helper that v1 uses to render review CSVs for
filings whose Assessment Year is **2026-27** (i.e., Financial Year
2025-26 income; Indian calendar year 2025 events).

Introduced in issue #65 to give each AY its own immutable snapshot
file so that a future portal label change concentrates the diff in a
new `_ay_YYYY_YY.py` rather than scattering edits across the
codebase.

Primary provenance (#267): the official AY 2026-27 **ITR-2 V1.2** and
**ITR-3 V1.2** Excel utilities plus their V1.1 JSON schemas. The exact
downloaded artifacts were pinned by SHA-256:

* ITR-2 utility zip: ``7a6e10def93b73cd3afa88b25e424a096f0eed9b34d86c65bf84037841418e3b``
* ITR-2 workbook: ``e5ce02f3ffc848d7fb708d4157c6c41af12013e68ee84686715b788c1f9fb785``
* ITR-2 schema: ``77be18189ecac94c7ed22baaa5e3bb46e238f781f55b9e2a8f6547f1e8e00ec8``
* ITR-3 utility zip: ``fa2badbe9b1908a5faf5779b885c99cc1d9414303932d39e9bb98c9496299861``
* ITR-3 workbook: ``863281c4f66aa49e374a39ad1d0fc726e321c2ce3d506fc28d7e2d81c8f80c4c``
* ITR-3 schema: ``66f4bd705e0e0788fdacb37eb297ace56749d791b84f2f170f4da84236c25e8b``

The audit did NOT produce a blanket "all current behavior is correct"
verdict:

* FA A2/A3 labels are identical between forms. Both country lists use
  CBDT annexure values, including ``2-UNITED STATES OF AMERICA`` (not
  literal ISD ``1``), but ITR-3 omits Yemen and Zimbabwe.
* FSI's visible columns are identical; ITR-3 additionally supports the
  Business/Profession head in its schema.
* TR's final header differs by form, and CG uses A5e/B8e in ITR-2 but
  A6g/B9e in ITR-3. Form-keyed mappings below preserve both.
* Schedule OS uses one 1a(i) ordinary-dividend total, 1b(ix) for parsed
  free-cash-balance interest, and 1e for supported residual income.
* Form 67 is separate from either ITR and remains pinned to the
  Rule 128 form verified in PR #226.

Why one file per AY (and why the underscore prefix):

* Each year, CBDT MAY rename or renumber portal cells. By pinning
  labels per-AY in a dedicated snapshot file, a future AY's label
  change does NOT silently mutate a current AY's audit trail — the
  diff is concentrated in one new file (``_ay_YYYY_YY.py``) instead
  of scattered edits across the codebase.
* The underscore prefix marks every snapshot as a private
  implementation module. External callers should import from the
  package's top-level :mod:`ibkr_fa.output.portal_labels` surface,
  which re-exports the **current** AY's labels and exposes
  :func:`~ibkr_fa.output.portal_labels.for_ay` for explicit
  per-AY access.
* Until CBDT publishes a schema change for AY 2027-28 (Income-tax
  Rules, 2026), the same labels also apply to AY 2027-28; the
  package's :func:`for_ay` falls back to this snapshot for AYs whose
  own snapshot has not yet been created.

Format helpers (:func:`format_country_for_portal`,
:func:`format_date_for_portal`, :func:`format_amount_for_portal`) are
co-located with the labels because their behaviour is also a
per-AY contract (the portal's date format, whole-rupee amount cells,
and country-code combination conventions could in principle change).
The models round filed amounts to whole rupees; the amount helper only
checks and renders them. v1
applies the same helpers across all AYs the package supports; if a
future AY's portal changes (say) the date format from DD/MM/YYYY to
ISO 8601, the per-AY snapshot can override the helper without
disturbing prior AYs' audit trail.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Final

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.invariants import require
from ibkr_fa.models.money import require_whole_rupees
from ibkr_fa.reference.country_codes import lookup_country

# =========================================================================
# Schedule FA Table A3 — portal column headers
# =========================================================================
# Verbatim, in left-to-right order, from the ITR-2 utility's TR_FA
# sheet (rows 50-51, columns E-O). See docs/portal/schedule-fa-a3.md
# for the source-of-truth screenshots.
PORTAL_COLUMN_HEADERS_FA_A3: Final[tuple[str, ...]] = (
    "Country Name and Code",
    "Name of entity",
    "Address of entity",
    "ZIP Code",
    "Nature of entity",
    "Date of acquiring the interest",
    "Initial value of the investment",
    "Peak value of investment during the Period",
    "Closing balance",
    "Total gross amount paid/credited with respect to the holding during the period",
    "Total gross proceeds from sale or redemption of investment during the period",
)

# Schedule FA Table A2 — Foreign Custodial Accounts (the IBKR account itself,
# issue #18). One row per account/credit nature. There is NO opening/initial-value column
# and NO account-closure-date column (unlike A3).
PORTAL_COLUMN_HEADERS_FA_A2: Final[tuple[str, ...]] = (
    "Country Name and Code",
    "Name of financial institution in which the account is held",
    "Address of financial institution",
    "ZIP Code",
    "Account Number",
    "Status",
    "Account opening date",
    "Peak Balance During the Period",
    "Closing balance",
    "Gross amount paid/credited to the account during the period - Nature of amount",
    "Gross amount paid/credited to the account during the period - Amount",
)

# Date format the portal expects in the "Date of acquiring" cell.
# Verified: cell J52:J57 in TR_FA sheet has textLength=10 + number
# format "@" (text). Indian convention is DD/MM/YYYY.
PORTAL_DATE_FORMAT: Final[str] = "%d/%m/%Y"


# =========================================================================
# Schedule OS — per-row portal labels
# =========================================================================
# Verified in both official AY 2026-27 V1.2 workbooks.
PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND: Final[str] = (
    "Schedule OS / 1a(i) Dividend income [other than (ii) and (iii)]"
)
PORTAL_SECTION_LABEL_OS_SECTION_1: Final[str] = (
    "Schedule OS / 1. Gross income chargeable to tax at normal applicable rates "
    "(1a+ 1b+ 1c+ 1d + 1e)"
)
PORTAL_FIELD_LABEL_OS_SECTION_1_ORDINARY_DIVIDEND: Final[str] = (
    "1ai. Dividend income [other than (ii) and (iii)]"
)
PORTAL_SECTION_LABEL_OS_SECTION_10: Final[str] = (
    "Schedule OS / 10. Information about accrual/receipt of income from Other Sources"
)
PORTAL_COLUMN_HEADERS_OS_SECTION_10: Final[tuple[str, ...]] = (
    "Sl No.",
    "Other Source Income",
    "Upto 15/6",
    "From 16/6 to 15/9",
    "From 16/9 to 15/12",
    "From 16/12 to 15/3",
    "From 16/3 to 31/3",
)
PORTAL_ROW_LABEL_OS_SECTION_10_ORDINARY_DIVIDEND: Final[str] = (
    "Dividend Income referred in Sl.no. 1a(i)"
)
PORTAL_FIELD_LABEL_OS_OTHER_INTEREST: Final[str] = (
    "Schedule OS / 1b(ix) Others including interest from Companies, NBFCs & HFCs"
)
# The same portal cell as above, labelled for the Section 1 field block (which
# prefixes rows with the bare cell number, e.g. "1ai."), so the filing worksheet
# reads like the portal form rather than repeating the "Schedule OS /" heading.
PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST: Final[str] = (
    "1b(ix). Others including interest from Companies, NBFCs & HFCs"
)
PORTAL_FIELD_LABEL_OS_SECTION_1_ANY_OTHER_INCOME: Final[str] = (
    "1e. Any other income (please specify nature)"
)


# =========================================================================
# Schedule CG — per-row portal labels
# =========================================================================
# Foreign STCG rows for resident assessees land in Schedule CG /
# Section A / Item 5. The official AY 2026-27 schema describes A5e as
# "STCG on assets other than at A1 or A2 or A3 or A4", covering both
# foreign shares and foreign fund units that do not enter the earlier
# special buckets. This income is taxed at the slab rate under the
# regular Chapter IV-D provisions; it is not Section 111A.
PORTAL_FIELD_LABEL_CG_FOREIGN_STCG: Final[str] = (
    "Schedule CG / A / 5(e) STCG on assets other than at A1 or A2 or A3 or A4 (A5e)"
)
PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM: Final[dict[ItrForm, str]] = {
    "ITR-2": PORTAL_FIELD_LABEL_CG_FOREIGN_STCG,
    "ITR-3": (
        "Schedule CG / A / 6(g) STCG on assets other than at A1 or A2 or A3 or A4 or A5 (A6g)"
    ),
}
PORTAL_SECTION_REF_CG_FOREIGN_STCG_BY_ITR_FORM: Final[dict[ItrForm, str]] = {
    "ITR-2": "A5",
    "ITR-3": "A6",
}

# For a resident, foreign-share and foreign-fund LTCG lands in Schedule
# CG / Section B / Item 8(e), the residual row for assets where B1-B7
# do not apply. Notification 46/2026 expressly reserves B4 for
# non-residents selling Indian-company shares/debentures and B7 for an
# NRI's Chapter XII-A foreign-exchange asset. The underlying tax section
# remains Section 112, not Section 112A.
PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG: Final[str] = (
    "Schedule CG / B / 8(e) LTCG from sale of assets where B1 to B7 are not applicable (B8e)"
)
PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG_BY_ITR_FORM: Final[dict[ItrForm, str]] = {
    "ITR-2": PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG,
    "ITR-3": (
        "Schedule CG / B / 9(e) LTCG from sale of assets where B1 to B8 are not applicable (B9e)"
    ),
}
PORTAL_SECTION_REF_CG_FOREIGN_LTCG_BY_ITR_FORM: Final[dict[ItrForm, str]] = {
    "ITR-2": "B8",
    "ITR-3": "B9",
}

# Schedule CG part F, "Information about accrual/receipt of capital gain"
# (Table F, #1360), as Notification 46/2026 prints it in ITR-2 (G.S.R. 227(E),
# Gazette p.50) and ITR-3 (G.S.R. 228(E), Gazette pp.107-108): the five
# date-band headings, then the row for the foreign STCG above (row 3) and the
# section 112 LTCG above (row 5), each naming the Schedule BFLA item it must add
# up to. The form prints each row's two lines without the period joining them
# here, and prints "Long- term" as written.
PORTAL_COLUMN_HEADERS_CG_TABLE_F: Final[tuple[str, ...]] = (
    "Upto 15/6",
    "16/6 to 15/9",
    "16/9 to 15/12",
    "16/12 to 15/3",
    "16/3 to 31/3",
)
PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM: Final[dict[ItrForm, str]] = {
    "ITR-2": (
        "Short-term capital gains taxable at applicable rates. "
        "Enter value from item 3v of schedule BFLA, if any."
    ),
    "ITR-3": (
        "Short-term capital gains taxable at applicable rates. "
        "Enter value from item 5viii of schedule BFLA, if any."
    ),
}
PORTAL_ROW_LABEL_CG_TABLE_F_LTCG_BY_ITR_FORM: Final[dict[ItrForm, str]] = {
    "ITR-2": (
        "Long- term capital gains taxable at the rate of 12.5%. "
        "Enter value from item 3vii of schedule BFLA, if any."
    ),
    "ITR-3": (
        "Long- term capital gains taxable at the rate of 12.5%. "
        "Enter value from item 5x of schedule BFLA, if any."
    ),
}


# =========================================================================
# Schedule FSI — per-row portal column headers
# =========================================================================
# Schedule FSI (Foreign Source Income) is a per-(country, head-of-income)
# disclosure: each row carries one source country, one head bucket
# (Other Sources / Capital Gains / Business or Profession), the gross foreign income, the foreign
# tax paid, and the Indian-tax + relief figures. Unlike Schedule OS / CG
# the form has NO single "TOTAL" cell — the portal accepts one row per
# (country, head), and downstream sums are computed server-side. Hence
# this snapshot exposes ONLY the column headers (not a TOTAL field label).
#
# Column count: 8 (NOT 9). Country Code and TIN are outer grouping
# fields; each group then contains income-head rows with columns (a)
# through (f). The flattened review-writer order is:
#       Country Code
#       Taxpayer Identification number
#   (a) Head of Income
#   (b) Income from outside India (included in PART B-TI)
#   (c) Tax paid outside India
#   (d) Tax payable on such income under normal provisions in India
#   (e) Tax relief available in India
#   (f) Relevant article of DTAA if relief claimed u/s 90 or 90A
#
# Verified in both V1.2 workbooks at FSI!E5:M5. The visible relief
# header includes the formula "(e)= (c) or (d) whichever is lower".
#
# v1 fill status (all fields are emitted; unsupported values stay empty):
#       Country Code                                  — filled
#       Taxpayer Identification number                — config or empty
#   (a) Head of Income                                — filled
#   (b) Income from outside India                     — filled
#   (c) Tax paid outside India                        — filled
#   (d) Tax payable under normal provisions           — computed or empty
#   (e) Tax relief available                          — computed or empty
#   (f) Relevant DTAA article                         — computed or empty
PORTAL_COLUMN_HEADERS_FSI: Final[tuple[str, ...]] = (
    "Country Code",
    "Taxpayer Identification number",
    "Head of Income",
    "Income from outside India (included in PART B-TI)",
    "Tax paid outside India",
    "Tax payable on such income under normal provisions in India",
    "Tax relief available in India (e)= (c) or (d) whichever is lower",
    "Relevant article of DTAA if relief claimed u/s 90 or 90A",
)


# =========================================================================
# Schedule TR — per-row portal column headers
# =========================================================================
# Schedule TR (Summary of Tax Relief Claimed) aggregates Schedule FSI's
# per-(country, head) rows into per-country relief-summary rows for
# resident assessees claiming foreign-tax credit under §90 / §90A / §91
# of the Income-tax Act, 1961. The portal accepts ONE row per source
# country (Schedule FSI's head dimension is collapsed).
#
# Verified in ITR-2 V1.2 at TR_FA!E6:I6. ITR-3 has the same first four
# columns but calls (e) "Section under which relief claimed ..."; #414
# owns that form-specific output label.
#
# The 5 ITR-2 portal columns in order are:
#   (a) Country Code                              — filled (CBDT numeric)
#   (b) Taxpayer Identification Number            — the per-country TIN
#       carried from Schedule FSI (stamped from [account.foreign_tins]);
#       EMPTY when the country has none
#   (c) Total taxes paid outside India ...        — filled (sum of FSI's
#       HALF_UP-rounded foreign_tax_inr per row across heads; T3 r1 P2 #1
#       fix on PR #216 — portal expects "total of (c) of Schedule FSI"
#       which is per-FSI-row rounded then summed, NOT
#       round-of-full-precision-sum)
#   (d) Total tax relief available ...            — sum of each FSI row's
#       rounded relief; filled only when every contributing row has
#       computed relief and the positive-relief rows resolve to one
#       section, otherwise EMPTY (rules: compute/schedule_tr.py)
#   (e) Tax Relief Claimed under section ...      — '90' / '90A' / '91',
#       filled together with (d)
#
# The portal also has DTAA-vs-non-DTAA sub-totals and a per-event
# refund-disclosure sub-table; neither is modelled (the sub-tables
# aren't per-country aggregates and require additional inputs — see
# docs/portal/schedule-tr.md).
PORTAL_COLUMN_HEADERS_TR: Final[tuple[str, ...]] = (
    "Country Code",
    "Taxpayer Identification Number",
    "Total taxes paid outside India (total of (c) of Schedule FSI in respect of each country)",
    "Total tax relief available (total of (e) of Schedule FSI in respect of each country)",
    "Tax Relief Claimed under section (specify 90, 90A or 91)",
)
PORTAL_COLUMN_HEADERS_TR_BY_ITR_FORM: Final[dict[ItrForm, tuple[str, ...]]] = {
    "ITR-2": PORTAL_COLUMN_HEADERS_TR,
    "ITR-3": (
        *PORTAL_COLUMN_HEADERS_TR[:4],
        "Section under which relief claimed (specify 90, 90A or 91)",
    ),
}


# =========================================================================
# Form 67 — per-row portal column headers
# =========================================================================
# Form 67 (Statement of income from a country or specified territory
# outside India and Foreign Tax Credit) is a separate CBDT filing (NOT
# part of ITR-2) furnished via the e-filing portal's Form 67 service per
# Rule 128(9) of the Income-tax Rules, 1962. The main table is Part A
# item 5: per-(country, source-of-income) aggregate rows (NOT per-event).
# A taxpayer with ten foreign dividend payments from the United States
# in one financial year files ONE row with the SUMMED income and SUMMED
# tax paid; matches Schedule FSI's per-(country, head_of_income)
# granularity plus 7 additional tax / relief columns layered on top.
#
# Form 67 column structure (verbatim from the official CBDT PDF,
# `incometaxindia.gov.in/forms/income-tax%20rules/itr62form67.pdf`,
# verified PR #226 r1 via wget + pdftotext -layout on the
# archive.org snapshot 20230629195202 — see
# :mod:`ibkr_fa.models.form67_entry` module docstring for the per-column
# v1 field mapping):
#   (1)  Sl. No.                                          — 1-indexed serial
#                                                            from compute
#                                                            layer's sort
#   (2)  Name of the country/specified territory          — country display
#                                                            ("Code-NAME" via
#                                                            entry's stored
#                                                            cbdt_country_code
#                                                            + country_name_portal
#                                                            fields, matching
#                                                            Schedule FA
#                                                            dropdown shape;
#                                                            r1 changed from
#                                                            format_country_for_portal
#                                                            lookup to stored
#                                                            fields per T3 r0 P2)
#   (3)  Source of income                                 — DIVIDEND in v1; one
#                                                            of the 10 enum
#                                                            values per CBDT PDF
#   (4)  Income from outside India                        — INR aggregate
#                                                            (Rule 115(1), Explanation (2)(e)
#                                                            preceding-month-end FX)
#   (5)  Tax paid outside India - Amount                  — INR aggregate
#                                                            (Rule 128(5)(ii)
#                                                            preceding-month-end FX,
#                                                            net of in-FY refunds)
#   (6)  Tax paid outside India - Rate                    — source-currency rate
#                                                            when proven; else
#                                                            col 5 / col 4 * 100;
#                                                            two-decimal
#                                                            HALF_UP per CBDT
#                                                            convention
#   (7)  Tax payable on such income under normal          — exact override or
#        provisions in India                                 EMPTY for manual
#                                                            calculation
#   (8)  Tax payable on such income under section         — not computed: EMPTY
#        115JB/JC                                            from the engine; a
#                                                            supplied value still
#                                                            renders
#   (9)  Credit claimed under section 90/90A -            — withholding article
#        Article No. of Double Taxation Avoidance            from the vendored
#        Agreements                                          DTAA table; EMPTY
#                                                            for a non-DTAA
#                                                            country
#   (10) Credit claimed under section 90/90A -            — treaty rate, filled
#        Rate of tax as per Double Taxation Avoidance        with col 9; two-
#        Agreements                                          decimal HALF_UP
#   (11) Credit claimed under section 90/90A - Amount     — min(col 7, eligible
#                                                            foreign tax) under
#                                                            Rule 128(5)(i) for a
#                                                            DTAA country with
#                                                            col 7 set and proven
#                                                            source rate; else EMPTY
#   (12) Credit claimed under section 91 - Amount         — only when the DTAA
#                                                            lookup returns
#                                                            NO_DTAA, with a
#                                                            [form67.section_91] table
#                                                            and col 7 set; else
#                                                            EMPTY rather than 0
#   (13) Total foreign tax credit claimed                 — equals the col 11 or
#                                                            col 12 credit; EMPTY
#                                                            when neither is set
#
# Column 6 rate choice and fill rules for cols 7-13: compute/form67.py.
#
# Column (3) "Source of income" enum values per the CBDT PDF (10 total):
# Salary / House property / Business or professional income / Long term
# capital gain / Short term capital gain / Interest income / Dividend /
# Royalty (not being part of business income) / Fees for technical
# services (not being part of business income) / Others (specify).
# v1 emits DIVIDEND only; CG / interest WHT requires source-kind
# tagging on the WHT model (follow-up).
PORTAL_COLUMN_HEADERS_FORM67: Final[tuple[str, ...]] = (
    "Sl. No.",
    "Name of the country/specified territory",
    "Source of income",
    "Income from outside India",
    "Tax paid outside India - Amount",
    "Tax paid outside India - Rate",
    "Tax payable on such income under normal provisions in India",
    "Tax payable on such income under section 115JB/JC",
    "Credit claimed under section 90/90A - Article No. of Double Taxation Avoidance Agreements",
    "Credit claimed under section 90/90A - Rate of tax as per Double Taxation Avoidance Agreements",
    "Credit claimed under section 90/90A - Amount",
    "Credit claimed under section 91 - Amount",
    "Total foreign tax credit claimed",
)


# =========================================================================
# Format helpers (FA-A3 specific, but available to all schedules)
# =========================================================================

_ITR3_UNSUPPORTED_COUNTRY_CODES: Final[frozenset[str]] = frozenset({"967", "263"})


def require_fa_country_code_supported(country_code: str, *, itr_form: ItrForm) -> None:
    """Reject the codes ITR-3's Schedule FA country dropdown leaves out (Yemen, Zimbabwe).

    India, which both forms' dropdowns leave out, never reaches this check:
    :class:`~ibkr_fa.models.fa_a2_row.FaA2Row` and
    :class:`~ibkr_fa.models.fa_a3_row.FaA3Row` refuse it when built (#1325).
    """

    require(
        itr_form != "ITR-3" or country_code not in _ITR3_UNSUPPORTED_COUNTRY_CODES,
        what=f"official {itr_form} Schedule FA country dropdown does not accept this code",
        country_code=country_code,
    )


def format_country_for_portal(iso_alpha2: str) -> str:
    """Build the portal's combined country cell from an ISO alpha-2 code.

    Args:
        iso_alpha2: e.g., ``"US"``.

    Returns:
        The dropdown value the portal accepts, e.g.,
        ``"2-UNITED STATES OF AMERICA"``.

    Raises:
        CountryCodeNotFoundError: If ``iso_alpha2`` isn't in
            :mod:`ibkr_fa.reference.country_codes`.
    """
    return lookup_country(iso_alpha2).portal_display


def format_date_for_portal(d: date) -> str:
    """Render ``d`` as the ``DD/MM/YYYY`` string the portal expects."""
    return d.strftime(PORTAL_DATE_FORMAT)


def format_amount_for_portal(value: Decimal | None, *, field: str) -> str:
    """Render a filed whole-rupee INR amount as a CSV cell value.

    The portal's Schedule FA monetary cells are constrained to integer
    values (``type=whole`` in the Excel utility's data validation). The
    models round each filed amount half-up (``FaA3Row.filed``,
    ``FaA2Row.filed``); this helper never rounds.

    Args:
        value: A filed whole-rupee amount, or :data:`None` when the
            column is left for the user to fill.
        field: The writer's fixed column name, used in the refusal.

    Returns:
        ``""`` (empty cell) for :data:`None`, else the amount's digits
        (e.g., ``"52480"``).

    Raises:
        InvariantError: ``value`` is not a whole-rupee amount (a
            fraction, an exponent form or ``-0``).
    """
    if value is None:
        return ""
    require_whole_rupees(value, what=field)
    return str(value)
