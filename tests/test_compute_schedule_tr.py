"""Tests for :mod:`ibkr_fa.compute.schedule_tr`."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.compute.schedule_tr import (
    ScheduleTrComputeResult,
    compute_schedule_tr_from_fsi,
)
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.models.schedule_tr_row import ScheduleTrRow, TaxReliefSection
from ibkr_fa.output import RenderContext, Subcommand, output_binding

# ---------------------------------------------------------------------------
# ---- Helpers
# ---------------------------------------------------------------------------


def _fsi_row(
    *,
    cbdt: str,
    iso: str,
    country: str,
    head: HeadOfIncome,
    income: Decimal,
    tax: Decimal,
    event_count: int = 1,
    tax_payable: Decimal | None = None,
    tax_relief: Decimal | None = None,
    dtaa_article: str | None = None,
) -> ScheduleFsiRow:
    return ScheduleFsiRow(
        cbdt_country_code=cbdt,
        country_name_portal=country,
        iso_alpha2=iso,
        head_of_income=head,
        income_inr=income,
        foreign_tax_inr=tax,
        event_count=event_count,
        tax_payable_inr=tax_payable,
        tax_relief_inr=tax_relief,
        dtaa_article=dtaa_article,
    )


def _tr_row(
    *,
    cbdt: str = "2",
    iso: str = "US",
    country: str = "UNITED STATES OF AMERICA",
    foreign_tax_total: Decimal = Decimal("100"),
    event_count: int = 1,
    foreign_tax_total_full_precision: Decimal | None = None,
    tax_relief_total: Decimal | None = None,
    section: TaxReliefSection | None = None,
) -> ScheduleTrRow:
    return ScheduleTrRow(
        cbdt_country_code=cbdt,
        country_name_portal=country,
        iso_alpha2=iso,
        foreign_tax_inr_total=foreign_tax_total,
        event_count=event_count,
        foreign_tax_inr_total_full_precision=foreign_tax_total_full_precision,
        tax_relief_inr_total=tax_relief_total,
        section_under_which_claimed=section,
    )


# ---------------------------------------------------------------------------
# ---- ScheduleTrComputeResult invariants
# ---------------------------------------------------------------------------


def test_compute_result_empty_rows_is_valid() -> None:
    """An FSI with zero foreign tax across all heads produces an
    empty TR. Empty is the v1 contract for 'no relief to claim'."""
    result = ScheduleTrComputeResult(rows=[])
    assert result.rows == []


def test_compute_result_rejects_duplicate_cbdt_codes() -> None:
    """Each TR row is per-country; a duplicate cbdt indicates an
    aggregation bug in compute."""
    with pytest.raises(InvariantError, match="at most one row per cbdt_country_code"):
        ScheduleTrComputeResult(rows=[_tr_row(cbdt="2"), _tr_row(cbdt="2")])


def test_compute_result_rejects_unsorted_rows() -> None:
    """Lexicographic sort on cbdt_country_code (matches FSI convention)."""
    us_row = _tr_row(cbdt="2", iso="US")
    gb_row = _tr_row(cbdt="111", iso="GB", country="UNITED KINGDOM")
    # Lexicographically "111" < "2" (string compare); US-first
    # (cbdt='2') violates the lexicographic sort.
    with pytest.raises(InvariantError, match="sorted by cbdt_country_code"):
        ScheduleTrComputeResult(rows=[us_row, gb_row])


def test_compute_result_accepts_lexicographically_sorted_rows() -> None:
    """Mirror the FSI rationale: lexicographic, not numeric — '1268'
    sorts BEFORE '2'."""
    rows = [
        _tr_row(cbdt="111", iso="GB", country="UNITED KINGDOM"),
        _tr_row(cbdt="1268", iso="AG", country="ANTIGUA AND BARBUDA"),
        _tr_row(cbdt="2", iso="US"),
    ]
    result = ScheduleTrComputeResult(rows=rows)
    assert [r.cbdt_country_code for r in result.rows] == ["111", "1268", "2"]


# ---------------------------------------------------------------------------
# ---- compute_schedule_tr_from_fsi — empty input
# ---------------------------------------------------------------------------


def test_compute_from_empty_fsi_returns_empty_tr() -> None:
    fsi = ScheduleFsiComputeResult(rows=[])
    result = compute_schedule_tr_from_fsi(fsi)
    assert result.rows == []


# ---------------------------------------------------------------------------
# ---- Head-of-income collapse (the core TR semantic)
# ---------------------------------------------------------------------------


def test_compute_collapses_two_heads_to_single_country_row() -> None:
    """The defining TR semantic: per-(country, head) FSI rows are
    collapsed to per-country TR rows by summing foreign_tax_inr
    across heads. Two US heads → one US TR row."""
    fsi = ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.CAPITAL_GAINS,
                income=Decimal("10000"),
                tax=Decimal("0"),
            ),
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("500"),
                tax=Decimal("100"),
            ),
        ]
    )
    result = compute_schedule_tr_from_fsi(fsi)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.cbdt_country_code == "2"
    assert row.iso_alpha2 == "US"
    assert row.country_name_portal == "UNITED STATES OF AMERICA"
    # 0 (CG) + 100 (OS) = 100
    assert row.foreign_tax_inr_total == Decimal("100")
    # event_count counts FSI rows that fed into this TR row.
    assert row.event_count == 2


def test_compute_drops_country_with_zero_foreign_tax_across_all_heads() -> None:
    """A country with foreign_tax_inr == 0 across all heads (e.g.,
    capital-gains-only country with no WHT) has nothing to relieve
    → silently dropped from TR even though it appears in FSI."""
    fsi = ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.CAPITAL_GAINS,
                income=Decimal("1000"),
                tax=Decimal("0"),
            ),
        ]
    )
    result = compute_schedule_tr_from_fsi(fsi)
    assert result.rows == []


def test_compute_multi_country_emits_one_row_per_country_sorted() -> None:
    """Multiple countries → one TR row each, sorted lexicographically
    by cbdt_country_code."""
    fsi = ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="111",
                iso="GB",
                country="UNITED KINGDOM",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("100"),
                tax=Decimal("15"),
            ),
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("500"),
                tax=Decimal("100"),
            ),
        ]
    )
    result = compute_schedule_tr_from_fsi(fsi)
    assert len(result.rows) == 2
    # Lexicographic sort: "111" < "2"
    assert result.rows[0].cbdt_country_code == "111"
    assert result.rows[0].iso_alpha2 == "GB"
    assert result.rows[0].foreign_tax_inr_total == Decimal("15")
    assert result.rows[1].cbdt_country_code == "2"
    assert result.rows[1].iso_alpha2 == "US"
    assert result.rows[1].foreign_tax_inr_total == Decimal("100")


def test_compute_mixed_zero_and_nonzero_countries_only_keeps_nonzero() -> None:
    """A multi-country FSI where one country has zero foreign tax and
    another has positive → only the positive country surfaces in TR."""
    fsi = ScheduleFsiComputeResult(
        rows=[
            # GB: zero foreign tax → dropped.
            _fsi_row(
                cbdt="111",
                iso="GB",
                country="UNITED KINGDOM",
                head=HeadOfIncome.CAPITAL_GAINS,
                income=Decimal("100"),
                tax=Decimal("0"),
            ),
            # US: positive foreign tax → kept.
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("500"),
                tax=Decimal("100"),
            ),
        ]
    )
    result = compute_schedule_tr_from_fsi(fsi)
    assert len(result.rows) == 1
    assert result.rows[0].cbdt_country_code == "2"


# ---------------------------------------------------------------------------
# ---- uncomputed FSI relief leaves relief and section None
# ---------------------------------------------------------------------------


def test_compute_leaves_relief_and_section_none_when_fsi_relief_uncomputed() -> None:
    """The fixture's FSI row has no computed relief (``tax_relief``
    is None, as when no Form 67 credit reached it), so the country's
    tax_relief_inr_total and section_under_which_claimed stay None
    (the PR #212 'not computed; user must fill' contract) rather than
    showing a partial total."""
    fsi = ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("500"),
                tax=Decimal("100"),
            ),
        ]
    )
    result = compute_schedule_tr_from_fsi(fsi)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.tax_relief_inr_total is None
    assert row.section_under_which_claimed is None


# ---------------------------------------------------------------------------
# ---- event_count semantics
# ---------------------------------------------------------------------------


def test_compute_event_count_counts_fsi_rows_not_upstream_events() -> None:
    """event_count on the TR row is the number of FSI rows that fed
    into it (not the underlying source events — those are already
    aggregated at the FSI layer)."""
    fsi = ScheduleFsiComputeResult(
        rows=[
            # Two US rows (CG + OS), each carrying multiple upstream
            # events. TR's event_count should be 2, NOT 5 or anything
            # else.
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.CAPITAL_GAINS,
                income=Decimal("10000"),
                tax=Decimal("100"),
                event_count=3,
            ),
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("500"),
                tax=Decimal("50"),
                event_count=2,
            ),
        ]
    )
    result = compute_schedule_tr_from_fsi(fsi)
    assert result.rows[0].event_count == 2  # two FSI rows → 2


def test_compute_single_fsi_row_yields_event_count_one() -> None:
    fsi = ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("500"),
                tax=Decimal("100"),
                event_count=5,  # upstream had 5 events
            ),
        ]
    )
    result = compute_schedule_tr_from_fsi(fsi)
    assert result.rows[0].event_count == 1  # one FSI row → 1


# ---------------------------------------------------------------------------
# ---- T3 r1 P2 #1 — portal-reconciliation rounding semantics
# ---------------------------------------------------------------------------


def test_compute_rounds_each_fsi_row_tax_before_summing_for_portal() -> None:
    """The ITR-2 portal labels TR's column (c) as 'total of (c) of
    Schedule FSI in respect of each country' — i.e., the sum of
    FSI's already-HALF_UP-rounded portal-displayed cells, NOT the
    HALF_UP-round of the full-precision sum.

    Two FSI rows of ₹1.50 each display as ₹2 + ₹2 = ₹4 on the
    portal. ``round(1.50 + 1.50) = 3`` would be off by ₹1.
    Regression for T3 r1 P2 #1 on PR #216.
    """
    fsi = ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.CAPITAL_GAINS,
                income=Decimal("100"),
                tax=Decimal("1.50"),
            ),
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("100"),
                tax=Decimal("1.50"),
            ),
        ]
    )
    result = compute_schedule_tr_from_fsi(fsi)
    assert len(result.rows) == 1
    # Each FSI cell rounds to ₹2; sum is ₹4. NOT round(3.00)=₹3.
    assert result.rows[0].foreign_tax_inr_total == Decimal("4")
    # T2/T3 r2 follow-up: full-precision audit sum preserved
    # separately for FSI⇄TR reconciliation. ₹1.50 + ₹1.50 = ₹3.00.
    assert result.rows[0].foreign_tax_inr_total_full_precision == Decimal("3.00")


def test_compute_per_row_rounding_handles_mixed_round_directions() -> None:
    """HALF_UP per-row rounding must NOT be replaced by a single
    rounding of the sum: e.g., 1.50 + 0.50 → portal shows ₹2 + ₹1 =
    ₹3, but ``round(2.00) = ₹2`` would be off by ₹1.
    """
    fsi = ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.CAPITAL_GAINS,
                income=Decimal("100"),
                tax=Decimal("1.50"),
            ),
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("100"),
                tax=Decimal("0.50"),
            ),
        ]
    )
    result = compute_schedule_tr_from_fsi(fsi)
    assert len(result.rows) == 1
    # ₹2 + ₹1 = ₹3. NOT round(2.00) = ₹2.
    assert result.rows[0].foreign_tax_inr_total == Decimal("3")
    # Full-precision audit sum: ₹1.50 + ₹0.50 = ₹2.00.
    assert result.rows[0].foreign_tax_inr_total_full_precision == Decimal("2.00")


def test_compute_per_row_rounding_independent_per_country() -> None:
    """Per-row HALF_UP rounding applies independently per country
    (no cross-country sum smuggling)."""
    fsi = ScheduleFsiComputeResult(
        rows=[
            # Lexicographic sort by cbdt_country_code: "1126" < "2".
            _fsi_row(
                cbdt="1126",
                iso="GB",
                country="UNITED KINGDOM",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("100"),
                tax=Decimal("1.50"),
            ),
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("100"),
                tax=Decimal("1.50"),
            ),
        ]
    )
    result = compute_schedule_tr_from_fsi(fsi)
    assert len(result.rows) == 2
    # Each country gets its own per-row-rounded total of ₹2.
    by_cbdt = {row.cbdt_country_code: row for row in result.rows}
    assert by_cbdt["2"].foreign_tax_inr_total == Decimal("2")
    assert by_cbdt["1126"].foreign_tax_inr_total == Decimal("2")
    # Full-precision audit sums per country: ₹1.50 each.
    assert by_cbdt["2"].foreign_tax_inr_total_full_precision == Decimal("1.50")
    assert by_cbdt["1126"].foreign_tax_inr_total_full_precision == Decimal("1.50")


# ---- #36/#37: FTC relief total + section, joined from enriched FSI rows -----


def test_relief_and_section_90_for_treaty_dividend_country() -> None:
    """A country whose only FSI row is a §90-credited dividend bucket gets its
    total relief (per-row HALF_UP then summed) and section '90'."""
    fsi = ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("68.4372"),
                tax=Decimal("17.3833"),
                tax_relief=Decimal("17.1093"),
                dtaa_article="Article 25(2)(a)",
            )
        ]
    )
    [row] = compute_schedule_tr_from_fsi(fsi).rows
    assert row.tax_relief_inr_total == Decimal("17")  # 17.1093 HALF_UP
    assert row.section_under_which_claimed == TaxReliefSection.SECTION_90


def test_section_91_when_relief_present_without_dtaa_article() -> None:
    fsi = ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="999",
                iso="HX",
                country="HYPOTHETICAL NON-DTAA TERRITORY",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("1000"),
                tax=Decimal("250"),
                tax_relief=Decimal("200"),
                dtaa_article=None,  # unilateral §91 relief: no treaty article
            )
        ]
    )
    [row] = compute_schedule_tr_from_fsi(fsi).rows
    assert row.tax_relief_inr_total == Decimal("200")
    assert row.section_under_which_claimed == TaxReliefSection.SECTION_91


def test_relief_left_none_when_any_country_row_is_uncomputed() -> None:
    """Dividend row credited, but a Capital-Gains row with foreign tax is
    uncomputed (None) — the per-country total stays manual-fill rather than
    showing a partial (understated) relief."""
    fsi = ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.CAPITAL_GAINS,
                income=Decimal("500"),
                tax=Decimal("40"),  # foreign CG tax → relief uncomputed (None)
                tax_relief=None,
            ),
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("75"),
                tax=Decimal("19"),
                tax_relief=Decimal("19"),
                dtaa_article="Article 25(2)(a)",
            ),
        ]
    )
    [row] = compute_schedule_tr_from_fsi(fsi).rows
    assert row.tax_relief_inr_total is None
    assert row.section_under_which_claimed is None


def test_zero_foreign_tax_cg_row_does_not_block_relief() -> None:
    """A dividend row (credited) plus a Capital-Gains row with ZERO foreign
    tax (relief provably ₹0) still yields a complete per-country total."""
    fsi = ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.CAPITAL_GAINS,
                income=Decimal("500"),
                tax=Decimal("0"),
                tax_relief=Decimal("0"),  # no foreign tax → provable ₹0 relief
            ),
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("75"),
                tax=Decimal("19"),
                tax_relief=Decimal("19"),
                dtaa_article="Article 25(2)(a)",
            ),
        ]
    )
    [row] = compute_schedule_tr_from_fsi(fsi).rows
    assert row.tax_relief_inr_total == Decimal("19")
    assert row.section_under_which_claimed == TaxReliefSection.SECTION_90


def test_relief_total_never_exceeds_foreign_tax_total() -> None:
    [row] = compute_schedule_tr_from_fsi(
        ScheduleFsiComputeResult(
            rows=[
                _fsi_row(
                    cbdt="2",
                    iso="US",
                    country="UNITED STATES OF AMERICA",
                    head=HeadOfIncome.OTHER_SOURCES,
                    income=Decimal("68.4372"),
                    tax=Decimal("17.3833"),
                    tax_relief=Decimal("17.1093"),
                    dtaa_article="Article 25(2)(a)",
                )
            ]
        )
    ).rows
    assert row.tax_relief_inr_total is not None
    assert row.tax_relief_inr_total <= row.foreign_tax_inr_total


def test_relief_and_section_stay_none_without_enriched_relief() -> None:
    """No enriched relief (e.g. no exact Indian tax) → relief + section stay
    None, left for manual fill."""
    fsi = ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("75"),
                tax=Decimal("19"),
                tax_relief=None,
            )
        ]
    )
    [row] = compute_schedule_tr_from_fsi(fsi).rows
    assert row.tax_relief_inr_total is None
    assert row.section_under_which_claimed is None


@pytest.mark.parametrize(
    "dtaa_article", ["Article 25(2)(a)", None], ids=["section-90", "section-91"]
)
def test_zero_credit_with_positive_foreign_tax_leaves_relief_none(
    dtaa_article: str | None,
) -> None:
    """A wholly-zero FTC (the assessee's Indian tax on the income is itself ₹0)
    on a row that DID pay foreign tax names no section, §90 or §91, so TR
    relief and section stay ``None``. No command writes this country: its
    Form 67 credit files as ₹0, a row #601 refuses first. From #1359 until
    #1491 the compute filed ``0`` under the row's section."""
    fsi = ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("1000"),
                tax=Decimal("150"),
                tax_payable=Decimal("0"),
                tax_relief=Decimal("0"),  # FTC capped at ₹0 Indian tax
                dtaa_article=dtaa_article,
            )
        ]
    )
    [row] = compute_schedule_tr_from_fsi(fsi).rows
    assert row.foreign_tax_inr_total == Decimal("150")  # country still reported
    assert row.tax_relief_inr_total is None
    assert row.section_under_which_claimed is None


def _zero_relief_us_row(
    head: HeadOfIncome, *, tax: Decimal, dtaa_article: str | None
) -> ScheduleFsiRow:
    """A US row whose computed relief is exactly ₹0."""
    return _fsi_row(
        cbdt="2",
        iso="US",
        country="UNITED STATES OF AMERICA",
        head=head,
        income=Decimal("500"),
        tax=tax,
        tax_relief=Decimal("0"),
        dtaa_article=dtaa_article,
    )


# ---- #787: positive relief on both §90 and §91 refuses ----------------------


def _mixed_section_us_rows(*, amount: Decimal) -> list[ScheduleFsiRow]:
    """A §91 Capital-Gains row and a §90 Other-Sources row, both positive."""
    return [
        _fsi_row(
            cbdt="2",
            iso="US",
            country="UNITED STATES OF AMERICA",
            head=HeadOfIncome.CAPITAL_GAINS,
            income=Decimal("500"),
            tax=amount,
            tax_relief=amount,
            dtaa_article=None,
        ),
        _fsi_row(
            cbdt="2",
            iso="US",
            country="UNITED STATES OF AMERICA",
            head=HeadOfIncome.OTHER_SOURCES,
            income=Decimal("75"),
            tax=amount,
            tax_relief=amount,
            dtaa_article="Article 25(2)(a)",
        ),
    ]


_UNCOMPUTED_BUSINESS_ROW = _fsi_row(
    cbdt="2",
    iso="US",
    country="UNITED STATES OF AMERICA",
    head=HeadOfIncome.BUSINESS_PROFESSION,
    income=Decimal("300"),
    tax=Decimal("30"),
    tax_relief=None,
)


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(_mixed_section_us_rows(amount=Decimal("19")), id="two-heads"),
        pytest.param(
            [_UNCOMPUTED_BUSINESS_ROW, *_mixed_section_us_rows(amount=Decimal("19"))],
            id="plus-uncomputed-head",
        ),
        pytest.param(_mixed_section_us_rows(amount=Decimal("0.10")), id="tax-rounds-to-zero"),
    ],
)
def test_positive_relief_on_both_sections_refuses(rows: list[ScheduleFsiRow]) -> None:
    """A country whose positive-relief rows resolve to both §90 and §91 is a
    broken invariant, so it refuses ahead of every blank or omit outcome."""
    with pytest.raises(InvariantError, match="more than one section") as exc_info:
        compute_schedule_tr_from_fsi(ScheduleFsiComputeResult(rows=rows))
    assert dict(exc_info.value.context) == {
        "cbdt_country_code": None,
        "iso_alpha2": None,
        "sections": None,
        "positive_relief_rows": None,
    }


def test_positive_relief_rows_sharing_section_90_fill_one_row() -> None:
    fsi = ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.CAPITAL_GAINS,
                income=Decimal("500"),
                tax=Decimal("40"),
                tax_relief=Decimal("30.4"),
                dtaa_article="Article 13",
            ),
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("75"),
                tax=Decimal("19"),
                tax_relief=Decimal("18.6"),
                dtaa_article="Article 25(2)(a)",
            ),
        ]
    )
    [row] = compute_schedule_tr_from_fsi(fsi).rows
    assert row.foreign_tax_inr_total == Decimal("59")
    assert row.tax_relief_inr_total == Decimal("49")  # 30 + 19, each HALF_UP
    assert row.section_under_which_claimed == TaxReliefSection.SECTION_90


def test_single_section_row_with_tax_rounding_to_zero_is_omitted() -> None:
    fsi = ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("75"),
                tax=Decimal("0.10"),
                tax_relief=Decimal("0.10"),
                dtaa_article="Article 25(2)(a)",
            )
        ]
    )
    assert compute_schedule_tr_from_fsi(fsi).rows == []


# ---- Zero relief on both §90 and §91 names no section ----------------------


def test_zero_relief_on_both_sections_leaves_relief_none() -> None:
    """Zero-relief rows that paid foreign tax on both §90 and §91 name no
    section either, so the country's relief and section stay ``None`` like
    any other zero relief. From #1359 until #1491 this refused, though no
    producer builds it: only a country's Other Sources row gets a computed
    relief while it paid foreign tax."""
    fsi = ScheduleFsiComputeResult(
        rows=[
            _zero_relief_us_row(HeadOfIncome.CAPITAL_GAINS, tax=Decimal("19"), dtaa_article=None),
            _zero_relief_us_row(
                HeadOfIncome.OTHER_SOURCES, tax=Decimal("34"), dtaa_article="Article 25(2)(a)"
            ),
        ]
    )
    [row] = compute_schedule_tr_from_fsi(fsi).rows
    assert row.foreign_tax_inr_total == Decimal("53")
    assert row.tax_relief_inr_total is None
    assert row.section_under_which_claimed is None


def test_positive_relief_names_the_section_beside_zero_relief_rows() -> None:
    """A §90 positive-relief row decides the section; a §91 zero-relief row
    that paid foreign tax beside it neither refuses nor changes it."""
    fsi = ScheduleFsiComputeResult(
        rows=[
            _zero_relief_us_row(HeadOfIncome.CAPITAL_GAINS, tax=Decimal("19"), dtaa_article=None),
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("75"),
                tax=Decimal("12"),
                tax_relief=Decimal("12"),
                dtaa_article="Article 25(2)(a)",
            ),
        ]
    )
    [row] = compute_schedule_tr_from_fsi(fsi).rows
    assert row.tax_relief_inr_total == Decimal("12")
    assert row.section_under_which_claimed is TaxReliefSection.SECTION_90


# ---------------------------------------------------------------------------
# ---- Relief the portal's read-only FSI cell would overwrite (#900)
# ---------------------------------------------------------------------------


def _overwritten_relief_fsi() -> ScheduleFsiComputeResult:
    """The issue's example (US: ₹250 of treaty-capped relief against the
    portal's min(₹300, ₹400) = ₹300) beside a GB row the portal keeps."""
    return ScheduleFsiComputeResult(
        rows=[
            _fsi_row(
                cbdt="2",
                iso="US",
                country="UNITED STATES OF AMERICA",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("1000"),
                tax=Decimal("300"),
                tax_payable=Decimal("400"),
                tax_relief=Decimal("250"),
                dtaa_article="Article 25(2)(a)",
            ),
            _fsi_row(
                cbdt="44",
                iso="GB",
                country="UNITED KINGDOM",
                head=HeadOfIncome.OTHER_SOURCES,
                income=Decimal("1000"),
                tax=Decimal("100.40"),
                tax_payable=Decimal("400"),
                tax_relief=Decimal("100.40"),
                dtaa_article="Article 24(2)(a)",
            ),
        ]
    )


@pytest.mark.parametrize("format_id", ["filing-csv", "review-csv", "xlsx"])
def test_aggregation_carries_overwritten_relief_and_every_tr_writer_refuses(
    tmp_path: Path, format_id: str
) -> None:
    fsi = _overwritten_relief_fsi()

    tr = compute_schedule_tr_from_fsi(fsi)

    assert tr.fsi_rows_with_overwritten_relief == (fsi.rows[0],)
    # TR still sums the filed whole rupees: ₹100.40 files as ₹100.
    assert [(row.foreign_tax_inr_total, row.tax_relief_inr_total) for row in tr.rows] == [
        (Decimal("300"), Decimal("250")),
        (Decimal("100"), Decimal("100")),
    ]
    path = tmp_path / "schedule-tr"
    with pytest.raises(InvariantError, match="read-only relief") as excinfo:
        output_binding(Subcommand.TR, format_id).writer(RenderContext(2026)).write(tr, path)
    assert dict(excinfo.value.context) == {
        "country": None,
        "head": None,
        "computed_relief": None,
        "portal_relief": None,
    }
    assert not path.exists()


def test_result_refuses_to_carry_a_row_the_portal_keeps() -> None:
    kept = _overwritten_relief_fsi().rows[1]

    with pytest.raises(InvariantError, match="only carry FSI rows whose relief"):
        ScheduleTrComputeResult(rows=[], fsi_rows_with_overwritten_relief=(kept,))


def test_aggregation_sums_filed_values_and_keeps_raw_tax_for_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _fsi_row(
            cbdt="2",
            iso="US",
            country="UNITED STATES OF AMERICA",
            head=head,
            income=Decimal("1000"),
            tax=Decimal("100.40"),
            tax_relief=Decimal("90.20"),
            dtaa_article="Article 25(2)(a)",
        )
        for head in (HeadOfIncome.CAPITAL_GAINS, HeadOfIncome.OTHER_SOURCES)
    ]
    filed = replace(rows[0].filed, foreign_tax_inr=Decimal("120"), tax_relief_inr=Decimal("60"))
    monkeypatch.setattr(ScheduleFsiRow, "filed", property(lambda _row: filed))

    result = compute_schedule_tr_from_fsi(ScheduleFsiComputeResult(rows=rows))

    (row,) = result.rows
    assert row.foreign_tax_inr_total == Decimal("240")
    assert row.tax_relief_inr_total == Decimal("120")
    assert row.foreign_tax_inr_total_full_precision == Decimal("200.80")
