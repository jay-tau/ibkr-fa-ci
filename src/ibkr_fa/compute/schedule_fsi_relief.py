"""Join Form 67's §90/§91 FTC into Schedule FSI rows (issues #36 / #37).

Schedule FSI, Schedule TR, and Form 67 all describe the SAME foreign-tax
credit at different granularities, so the relief numbers MUST agree. Rather
than re-deriving the §90/§91 formula (which would risk divergence), this
module reuses Form 67's already-computed, already-tested credit as the single
source of truth and copies it onto the matching FSI rows.

Safety stance (never fabricate a relief figure):

* A row whose ``foreign_tax_inr == 0`` has a provable relief of ₹0 (nothing to
  credit) regardless of head; its ``tax_payable_inr`` / ``dtaa_article`` stay
  ``None`` (the slab- / special-rate "tax payable" for a no-credit row is left
  to manual fill — notably for the special-rate Capital-Gains head).
* An ``Other Sources`` row that exactly matches its country's Form 67 DIVIDEND
  entry inherits its tax payable and total credit, then cites the vendored DTAA
  relief article required by Schedule FSI.
* A mixed Other Sources row can inherit the same dividend credit when the
  caller supplies exact interest and residual-other-source totals. Its tax
  payable is the sum of each independently resolved source slice. The
  reconciliation's income sums and the tax-payable sum are exact and never
  round (:func:`~ibkr_fa.compute.exact_inr.checked_addition`, #1167); one
  that can't be added exactly refuses rather than leaving the row
  unenriched.
* Every other row (unproven mixed income, Capital Gains with foreign tax, or a
  run where Form 67 produced no credit) stays manual-fill. An Other Sources
  row whose country's Form 67 DIVIDEND row is BLANK carries that row's
  :func:`~ibkr_fa.compute.form67.form67_override_advice` for its manual fill
  guidance (#1469).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Final

from ibkr_fa.compute.exact_inr import checked_addition
from ibkr_fa.compute.form67 import (
    Form67ComputeResult,
    form67_override_advice,
    tax_payable_override_map,
)
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.config import Form67Config
from ibkr_fa.invariants import require
from ibkr_fa.models.form67_entry import Form67Entry, Form67SourceOfIncome
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.reference.dtaa_dividend import lookup_dtaa_dividend

_ZERO: Final[Decimal] = Decimal("0")


def stamp_taxpayer_ids(
    fsi_result: ScheduleFsiComputeResult,
    foreign_tins: Mapping[str, str],
) -> ScheduleFsiComputeResult:
    """Return ``fsi_result`` with each row's ``taxpayer_id_number`` set from
    ``foreign_tins`` (keyed by the row's ISO alpha-2 country).

    A country absent from the map leaves the row's TIN ``None`` (manual-fill,
    today's behaviour). Schedule TR copies the TIN from these rows, so a
    single stamp here populates both schedules. No-op for an empty map.
    """
    if not foreign_tins:
        return fsi_result
    # dataclasses.replace (not a fresh construction) so sibling fields such as
    # cross_fy_reversal_report survive onto the result the FSI/TR pipelines read.
    return dataclasses.replace(
        fsi_result,
        rows=[
            dataclasses.replace(row, taxpayer_id_number=foreign_tins.get(row.iso_alpha2))
            for row in fsi_result.rows
        ],
    )


def relief_row_lacks_taxpayer_id(rows: Iterable[ScheduleFsiRow]) -> bool:
    """Whether any row with foreign tax (a relief claim) has no foreign TIN.

    Schedule FSI and TR need the TIN on every relief-claiming row, whatever its
    country. A row without foreign tax claims no relief, so its blank TIN is
    complete (#361).
    """
    return any(row.foreign_tax_inr > _ZERO and row.taxpayer_id_number is None for row in rows)


def require_form67_credit_reconciles(
    fsi_result: ScheduleFsiComputeResult,
    form67_result: Form67ComputeResult,
) -> None:
    """Require every computed Form 67 credit to equal its FSI relief."""
    for entry in form67_result.entries:
        if entry.total_credit_inr is None:
            continue
        matches = [
            row
            for row in fsi_result.rows
            if row.cbdt_country_code == entry.cbdt_country_code
            and row.iso_alpha2 == entry.iso_alpha2
            and row.head_of_income is HeadOfIncome.OTHER_SOURCES
        ]
        require(
            len(matches) == 1 and matches[0].tax_relief_inr == entry.total_credit_inr,
            what=(
                "Form 67 credit must equal the matching Schedule FSI relief, and that "
                "country's Schedule FSI Other Sources row also holds other income: add "
                "an exact [[form67.tax_payable_overrides]] amount for each other source "
                "in it (INTEREST_INCOME for interest; OTHERS for substitute payments and "
                "reviewed Robinhood stock-lending fees together)"
            ),
            country=entry.iso_alpha2,
            form67_credit=entry.total_credit_inr,
            fsi_relief=None if len(matches) != 1 else matches[0].tax_relief_inr,
        )


def enrich_fsi_with_form67_relief(
    fsi_result: ScheduleFsiComputeResult,
    form67_result: Form67ComputeResult,
    *,
    interest_income_inr_by_iso: Mapping[str, Decimal] | None = None,
    other_income_inr_by_iso: Mapping[str, Decimal] | None = None,
    form67_config: Form67Config | None = None,
) -> ScheduleFsiComputeResult:
    """Return ``fsi_result`` with FTC columns populated where provably correct.

    See the module docstring for the per-row rules. The (country, head) keys,
    ordering, income, and foreign-tax of every row are preserved unchanged;
    only the three previously-``None`` FTC columns may be filled.
    """
    dividend_entries: dict[tuple[str, str], Form67Entry] = {}
    for entry in form67_result.entries:
        if entry.source_of_income is not Form67SourceOfIncome.DIVIDEND:
            continue
        key = (entry.cbdt_country_code, entry.iso_alpha2)
        # Form67ComputeResult enforces (cbdt_country_code, source_of_income)
        # uniqueness, so a country has at most one DIVIDEND entry; assert it.
        require(
            key not in dividend_entries,
            what="duplicate Form 67 DIVIDEND entry for one country while enriching FSI",
            country=key,
        )
        dividend_entries[key] = entry
    interest_income = interest_income_inr_by_iso or {}
    other_income = other_income_inr_by_iso or {}
    override_map = tax_payable_override_map(form67_config)
    # dataclasses.replace (not a fresh construction) so sibling fields such as
    # cross_fy_reversal_report survive onto the result the FSI/TR pipelines read.
    return dataclasses.replace(
        fsi_result,
        rows=[
            _enrich_row(row, dividend_entries, interest_income, other_income, override_map)
            for row in fsi_result.rows
        ],
    )


def _enrich_row(
    row: ScheduleFsiRow,
    dividend_entries: dict[tuple[str, str], Form67Entry],
    interest_income_inr_by_iso: Mapping[str, Decimal],
    other_income_inr_by_iso: Mapping[str, Decimal],
    override_map: dict[tuple[str, Form67SourceOfIncome], Decimal],
) -> ScheduleFsiRow:
    if row.foreign_tax_inr == _ZERO:
        # No foreign tax paid → the credit is provably ₹0, any head.
        return dataclasses.replace(row, tax_relief_inr=_ZERO)
    entry = dividend_entries.get((row.cbdt_country_code, row.iso_alpha2))
    if (
        row.head_of_income is not HeadOfIncome.OTHER_SOURCES
        or entry is None
        or entry.tax_payable_normal_inr is None
        or entry.total_credit_inr is None
        or row.foreign_tax_inr != entry.foreign_tax_inr
    ):
        # Filing relief needs both the credit and its mandatory Indian-tax cap. An
        # Other Sources row left manual carries its country's blank dividend row's
        # advice for its manual fill guidance (#1469).
        advice = (
            form67_override_advice((entry,))
            if row.head_of_income is HeadOfIncome.OTHER_SOURCES and entry is not None
            else ()
        )
        return dataclasses.replace(row, override_advice=advice)
    dtaa_article = None
    if entry.section_90_credit_inr is not None:
        dtaa = lookup_dtaa_dividend(row.iso_alpha2)
        if dtaa is not None and dtaa.relief_article is not None:
            dtaa_article = dtaa.relief_article
        else:
            require(
                False,
                what="missing DTAA relief article for a computed section 90 credit",
                country=row.iso_alpha2,
            )
    if row.income_inr != entry.income_inr:
        source_slices = (
            (
                Form67SourceOfIncome.INTEREST_INCOME,
                interest_income_inr_by_iso.get(row.iso_alpha2, _ZERO),
            ),
            (
                Form67SourceOfIncome.OTHERS,
                other_income_inr_by_iso.get(row.iso_alpha2, _ZERO),
            ),
        )
        source_slice_total = checked_addition(
            source_slices[0][1],
            source_slices[1][1],
            at=(
                "Schedule FSI relief reconciliation, fields interest_income_inr "
                "and other_income_inr"
            ),
        )
        reconciled_income = checked_addition(
            entry.income_inr,
            source_slice_total,
            at=(
                "Schedule FSI relief reconciliation, fields entry.income_inr "
                "and source_slice_total_inr"
            ),
        )
        if row.income_inr != reconciled_income:
            return row
        tax_payable = entry.tax_payable_normal_inr
        for source, income in source_slices:
            if income == _ZERO:
                continue
            if income < _ZERO:
                return row
            source_tax_payable = override_map.get((row.iso_alpha2, source))
            if source_tax_payable is None:
                return row
            tax_payable = checked_addition(
                tax_payable,
                source_tax_payable,
                at=(
                    f"Schedule FSI relief tax payable, key {source.name}, "
                    "field tax_payable_normal_inr"
                ),
            )
    else:
        tax_payable = entry.tax_payable_normal_inr
    # A §91 credit has no DTAA relief article, so the FSI field stays blank.
    return dataclasses.replace(
        row,
        tax_payable_inr=tax_payable,
        tax_relief_inr=entry.total_credit_inr,
        dtaa_article=dtaa_article,
    )
