"""Form 67 readiness: one rule for every source (#601)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ibkr_fa.compute.form67 import (
    Form67ComputeResult,
    Form67Entry,
    Form67Readiness,
    Form67RefusalReason,
    Form67RowReadiness,
    Form67SourceOfIncome,
    Form67TreatyRateStatus,
    compute_form67_entries_from_wht,
    form67_override_advice,
    form67_override_gap,
    form67_row_readiness,
    form67_section_91_need,
    require_no_refused_form67_rows,
)
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.compute.schedule_os import ScheduleOsDividendLine
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult
from ibkr_fa.config import Form67Config, Form67Section91Config, Form67TaxPayableOverride
from ibkr_fa.exceptions import ConfigError, InvariantError
from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.output import RenderContext, Subcommand, output_binding
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_robinhood_outputs import write_income
from ibkr_fa.types import Isin, Ticker
from tests._form67_compute_inputs import DEFAULT_ISIN_US as _DEFAULT_ISIN_US
from tests._form67_compute_inputs import FY_END as _FY_END
from tests._form67_compute_inputs import FY_START as _FY_START
from tests._form67_compute_inputs import (
    REFUND_BESIDE_ABOVE_RATE_PAYMENTS as _REFUND_BESIDE_ABOVE_RATE_PAYMENTS,
)
from tests._form67_compute_inputs import UNPROVEN_SOURCE_RATE_CASES as _UNPROVEN_SOURCE_RATE_CASES
from tests._form67_compute_inputs import os_dividend_line as _os_line
from tests._form67_compute_inputs import withholding_row as _wht
from tests._ftc_warnings import (
    KY_SECTION_91_NEED,
    KY_SECTION_91_REFUSAL,
    override_sentence,
    refused_override,
    same_day_gap,
    source_rate_gap,
    unvendored_dtaa_gap,
    wont_clear,
)
from tests._ftc_warnings import UNPRODUCED_WHAT as _UNPRODUCED_WHAT
from tests._fx_stubs import DateAwareRatePolicy, FixedRatePolicy

# (ISO alpha-2, CBDT code, portal name)
_US = ("US", "2", "UNITED STATES OF AMERICA")
_CH = ("CH", "41", "SWITZERLAND")  # no vendored DTAA row (#249)
_KY = ("KY", "1345", "CAYMAN ISLANDS")  # vendored as no_dtaa
_US_ARTICLE = "Article 10(2)(b)"
_US_RATE = Decimal("25")


def _entry(
    country: tuple[str, str, str],
    *,
    serial: int = 1,
    normal_tax: Decimal | None = None,
    article: str | None = None,
    dtaa_rate: Decimal | None = None,
    section_90: Decimal | None = None,
    section_91: Decimal | None = None,
    total: Decimal | None = None,
    unproven: bool = False,
    payment_allocation_unproven: bool = False,
) -> Form67Entry:
    """A 25% entry; with a treaty rate it is WITHIN, or UNPROVEN on request.

    ``payment_allocation_unproven`` makes it the #890 refund case: a 30%
    payment beside a refund that can't be attributed to it.
    """
    iso, cbdt, name = country
    highest = None
    if dtaa_rate is None:
        status = None
    elif unproven or payment_allocation_unproven:
        status = Form67TreatyRateStatus.UNPROVEN
        if payment_allocation_unproven:
            highest = Decimal("30")
    else:
        status = Form67TreatyRateStatus.WITHIN
        highest = Decimal("25")
    return Form67Entry(
        serial_number=serial,
        cbdt_country_code=cbdt,
        country_name_portal=name,
        iso_alpha2=iso,
        source_of_income=Form67SourceOfIncome.DIVIDEND,
        income_inr=Decimal("1000"),
        foreign_tax_inr=Decimal("250"),
        foreign_tax_rate_percent=Decimal("25"),
        event_count=1,
        tax_paid_rate_percent=None if unproven else Decimal("25"),
        tax_payable_normal_inr=normal_tax,
        section_90_dtaa_article=article,
        section_90_dtaa_rate_percent=dtaa_rate,
        eligible_foreign_tax_inr=(
            None if status is not Form67TreatyRateStatus.WITHIN else Decimal("250")
        ),
        treaty_rate_status=status,
        section_90_credit_inr=section_90,
        section_91_credit_inr=section_91,
        total_credit_inr=total,
        highest_payment_rate_percent=highest,
        payment_allocation_unproven=payment_allocation_unproven,
    )


def _upload() -> Any:
    return output_binding(Subcommand.FORM67, "portal-csv").writer(RenderContext(2026))


def _refused(reason: Form67RefusalReason) -> Form67RowReadiness:
    return Form67RowReadiness(Form67Readiness.REFUSED, reason)


# Each row shape a compute can produce, with the readiness it must get.
SHAPES: dict[str, tuple[Form67Entry, Form67RowReadiness]] = {
    "ready-section-90": (
        _entry(
            _US,
            normal_tax=Decimal("300"),
            article=_US_ARTICLE,
            dtaa_rate=_US_RATE,
            section_90=Decimal("250"),
            total=Decimal("250"),
        ),
        Form67RowReadiness(Form67Readiness.READY),
    ),
    "ready-section-91": (
        _entry(_KY, normal_tax=Decimal("300"), section_91=Decimal("100"), total=Decimal("100")),
        Form67RowReadiness(Form67Readiness.READY),
    ),
    "blank": (
        _entry(_US, article=_US_ARTICLE, dtaa_rate=_US_RATE),
        Form67RowReadiness(Form67Readiness.BLANK),
    ),
    "partial-unvendored": (
        _entry(_CH, normal_tax=Decimal("300")),
        _refused(Form67RefusalReason.NO_VENDORED_DTAA),
    ),
    "partial-no-section-91-inputs": (
        _entry(_KY, normal_tax=Decimal("300")),
        _refused(Form67RefusalReason.SECTION_91_INPUTS_MISSING),
    ),
    "zero-credit-section-90": (
        _entry(
            _US,
            normal_tax=Decimal("0"),
            article=_US_ARTICLE,
            dtaa_rate=_US_RATE,
            section_90=Decimal("0"),
            total=Decimal("0"),
        ),
        _refused(Form67RefusalReason.ZERO_CREDIT),
    ),
    "zero-credit-section-91": (
        _entry(_KY, normal_tax=Decimal("300"), section_91=Decimal("0"), total=Decimal("0")),
        _refused(Form67RefusalReason.ZERO_CREDIT),
    ),
    # #898: readiness reads the whole-rupee credit the portal CSV files.
    "credit-rounding-to-zero-section-90": (
        _entry(
            _US,
            normal_tax=Decimal("0.49"),
            article=_US_ARTICLE,
            dtaa_rate=_US_RATE,
            section_90=Decimal("0.49"),
            total=Decimal("0.49"),
        ),
        _refused(Form67RefusalReason.ZERO_CREDIT),
    ),
    "credit-rounding-to-one-section-90": (
        _entry(
            _US,
            normal_tax=Decimal("0.50"),
            article=_US_ARTICLE,
            dtaa_rate=_US_RATE,
            section_90=Decimal("0.50"),
            total=Decimal("0.50"),
        ),
        Form67RowReadiness(Form67Readiness.READY),
    ),
    "credit-rounding-to-zero-section-91": (
        _entry(_KY, normal_tax=Decimal("300"), section_91=Decimal("0.49"), total=Decimal("0.49")),
        _refused(Form67RefusalReason.ZERO_CREDIT),
    ),
    "credit-rounding-to-one-section-91": (
        _entry(_KY, normal_tax=Decimal("300"), section_91=Decimal("0.50"), total=Decimal("0.50")),
        Form67RowReadiness(Form67Readiness.READY),
    ),
    "unproven-source-rate-blank": (
        _entry(_US, article=_US_ARTICLE, dtaa_rate=_US_RATE, unproven=True),
        Form67RowReadiness(Form67Readiness.BLANK),
    ),
    "unproven-source-rate-with-col-7": (
        _entry(
            _US, normal_tax=Decimal("300"), article=_US_ARTICLE, dtaa_rate=_US_RATE, unproven=True
        ),
        _refused(Form67RefusalReason.SOURCE_RATE_UNPROVEN),
    ),
    "payment-allocation-unproven-blank": (
        _entry(_US, article=_US_ARTICLE, dtaa_rate=_US_RATE, payment_allocation_unproven=True),
        Form67RowReadiness(Form67Readiness.BLANK),
    ),
    "payment-allocation-unproven-with-col-7": (
        _entry(
            _US,
            normal_tax=Decimal("300"),
            article=_US_ARTICLE,
            dtaa_rate=_US_RATE,
            payment_allocation_unproven=True,
        ),
        _refused(Form67RefusalReason.PAYMENT_ALLOCATION_UNPROVEN),
    ),
}
# #1491: no producer builds these mixes of col 7, the claimed credit and col
# 13, or col 7 alone without a named gap, so readiness fails an invariant
# rather than name a refusal. Until #1491 each was REFUSED as INCOMPLETE.
UNPRODUCED: dict[str, Form67Entry] = {
    "zero-total-without-a-credit": _entry(
        _US, article=_US_ARTICLE, dtaa_rate=_US_RATE, total=Decimal("0")
    ),
    "credit-without-col-7": _entry(
        _US,
        article=_US_ARTICLE,
        dtaa_rate=_US_RATE,
        section_90=Decimal("250"),
        total=Decimal("250"),
    ),
    "col-7-within-the-treaty-rate": _entry(
        _US, normal_tax=Decimal("300"), article=_US_ARTICLE, dtaa_rate=_US_RATE
    ),
    "col-7-on-interest": replace(
        _entry(_CH, normal_tax=Decimal("300")),
        source_of_income=Form67SourceOfIncome.INTEREST_INCOME,
    ),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_row_readiness(shape: str) -> None:
    entry, expected = SHAPES[shape]
    assert form67_row_readiness(entry) == expected


@pytest.mark.parametrize("shape", sorted(UNPRODUCED))
def test_unproduced_row_fails_the_readiness_invariant(shape: str) -> None:
    """Readiness, and so the #601 check that reads it, refuses with a typed
    invariant whose context holds keys only."""
    entry = UNPRODUCED[shape]
    with pytest.raises(InvariantError) as caught:
        form67_row_readiness(entry)
    assert type(caught.value) is InvariantError
    assert caught.value.what == _UNPRODUCED_WHAT
    assert caught.value.context == (("country", None), ("source", None))
    with pytest.raises(InvariantError, match="no named gap keeps the credit uncomputed"):
        require_no_refused_form67_rows(Form67ComputeResult(entries=(entry,)))


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_portal_writer_accepts_only_ready_rows(shape: str, tmp_path: Path) -> None:
    entry, expected = SHAPES[shape]
    path = tmp_path / "form-67.csv"
    result = Form67ComputeResult(entries=(entry,))
    if expected.readiness is Form67Readiness.READY:
        _upload().write(result, path)
        assert path.is_file()
    else:
        with pytest.raises(ConfigError):
            _upload().write(result, path)
        assert not path.exists()


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_robinhood_write_income_follows_row_readiness(shape: str, tmp_path: Path) -> None:
    entry, expected = SHAPES[shape]
    assembled = cast(
        Any,
        SimpleNamespace(
            fsi_result=ScheduleFsiComputeResult(rows=[]),
            form67_result=Form67ComputeResult(entries=(entry,)),
        ),
    )

    def write() -> tuple[tuple[Path, ...], Form67Readiness]:
        return write_income(
            assembled,
            ScheduleTrComputeResult(rows=[]),
            output_dir=tmp_path,
            financial_year=FinancialYear(2025),
            itr_form="ITR-2",
            debug=False,
        )

    if expected.readiness is Form67Readiness.REFUSED:
        with pytest.raises(ConfigError):
            write()
        assert list(tmp_path.iterdir()) == []
        return
    written, readiness = write()
    assert readiness is expected.readiness
    form67_files = sorted(path.name for path in written if path.name.startswith("form-67"))
    assert form67_files == (
        ["form-67.csv"] if readiness is Form67Readiness.READY else ["form-67-debug.csv"]
    )


@pytest.mark.parametrize("section", ["90", "91"])
def test_half_rupee_credit_files_one_rupee(section: str, tmp_path: Path) -> None:
    """#898: ₹0.50 is READY and the portal CSV files it as 1, under either section."""
    entry, _ = SHAPES[f"credit-rounding-to-one-section-{section}"]
    assert entry.filed.claimed_credit_inr == entry.filed.total_credit_inr == Decimal("1")
    path = tmp_path / "form-67.csv"
    _upload().write(Form67ComputeResult(entries=(entry,)), path)
    detail = path.read_text(encoding="utf-8").splitlines()[1].split(",")
    section_90_cell, section_91_cell, total_cell = detail[12:15]
    assert (section_90_cell, section_91_cell) == (("1", "") if section == "90" else ("", "1"))
    assert total_cell == "1"


@pytest.mark.parametrize("section", ["90", "91"])
def test_credit_rounding_to_zero_refusal_says_so(section: str) -> None:
    entry, _ = SHAPES[f"credit-rounding-to-zero-section-{section}"]
    assert entry.filed.claimed_credit_inr == Decimal("0")
    with pytest.raises(ConfigError) as caught:
        require_no_refused_form67_rows(Form67ComputeResult(entries=(entry,)))
    assert (
        f"{entry.iso_alpha2}/DIVIDEND: the computed credit rounds to ₹0 in whole rupees, "
        "the amount Form 67 files"
    ) in str(caught.value)


def test_blank_row_files_blank_credit_columns() -> None:
    filed = SHAPES["blank"][0].filed
    assert filed.tax_payable_normal_inr is None
    assert filed.claimed_credit_inr is None
    assert filed.total_credit_inr is None


def test_result_readiness_ranks_refused_over_blank_over_ready() -> None:
    ready = SHAPES["ready-section-90"][0]
    blank = SHAPES["blank"][0]
    ch_blank = _entry(_CH, serial=2)
    ch_partial = _entry(_CH, serial=2, normal_tax=Decimal("300"))
    assert Form67ComputeResult(entries=()).readiness is Form67Readiness.READY
    assert Form67ComputeResult(entries=(ready, ch_blank)).readiness is Form67Readiness.BLANK
    assert Form67ComputeResult(entries=(blank, ch_partial)).readiness is Form67Readiness.REFUSED
    assert Form67ComputeResult(entries=(blank, ch_partial)).row_readiness == (
        Form67RowReadiness(Form67Readiness.BLANK),
        _refused(Form67RefusalReason.NO_VENDORED_DTAA),
    )


def test_refusal_names_every_refused_row_and_its_remedy() -> None:
    # KY's CBDT code 1345 sorts before the US's 2.
    ky = _entry(_KY, normal_tax=Decimal("300"))
    zero = replace(SHAPES["zero-credit-section-90"][0], serial_number=2)
    with pytest.raises(ConfigError) as caught:
        require_no_refused_form67_rows(Form67ComputeResult(entries=(ky, zero)))
    message = str(caught.value)
    assert "US/DIVIDEND: the computed credit rounds to ₹0" in message
    assert "remove the [[form67.tax_payable_overrides]] entry for US/DIVIDEND" in message
    assert "KY/DIVIDEND: KY has no DTAA with India" in message
    assert "[form67.section_91]" in message


@pytest.mark.parametrize(
    ("highest_payment_rate_percent", "cause"),
    [
        pytest.param(
            Decimal("30"),
            "a dividend payment was withheld above the DTAA rate, and a refund or reversal "
            "of US tax in the year can't be attributed to it",
            id="refund-beside-above-rate-payment",
        ),
        pytest.param(
            None,
            "the withholding can't be matched to a single dividend payment (same-day "
            "dividends of one security in one account can't be told apart",
            id="same-day-payments",
        ),
    ],
)
def test_payment_allocation_unproven_with_col_7_is_refused(
    highest_payment_rate_percent: Decimal | None, cause: str
) -> None:
    """#890: the refusal names its cause and #767's remedy."""
    entry = replace(
        SHAPES["payment-allocation-unproven-with-col-7"][0],
        highest_payment_rate_percent=highest_payment_rate_percent,
    )
    assert form67_row_readiness(entry) == _refused(Form67RefusalReason.PAYMENT_ALLOCATION_UNPROVEN)
    with pytest.raises(ConfigError) as caught:
        require_no_refused_form67_rows(Form67ComputeResult(entries=(entry,)))
    message = str(caught.value)
    assert f"US/DIVIDEND: {cause}" in message
    assert "the section 90 credit can't be computed" in message
    assert (
        "remove the [[form67.tax_payable_overrides]] entry for US/DIVIDEND; the row is then "
        "left blank for manual Form 67 work"
    ) in message


def test_refused_row_readiness_requires_a_reason() -> None:
    with pytest.raises(InvariantError, match="REFUSED"):
        Form67RowReadiness(Form67Readiness.REFUSED)
    with pytest.raises(InvariantError, match="REFUSED"):
        Form67RowReadiness(Form67Readiness.READY, Form67RefusalReason.ZERO_CREDIT)


_SAME_DAY = replace(
    SHAPES["payment-allocation-unproven-blank"][0], highest_payment_rate_percent=None
)
# #1461: each BLANK row, and the gap no override clears, if any.
OVERRIDE_GAPS: dict[str, tuple[Form67Entry, str | None]] = {
    "dtaa-within-rate": (SHAPES["blank"][0], None),
    # An override and the [form67.section_91] inputs compute its credit.
    "no-dtaa": (_entry(_KY), None),
    "unvendored": (_entry(_CH), unvendored_dtaa_gap("CH")),
    "unproven-source-rate": (
        SHAPES["unproven-source-rate-blank"][0],
        source_rate_gap("US/DIVIDEND"),
    ),
    "refund-beside-above-rate-payment": (
        SHAPES["payment-allocation-unproven-blank"][0],
        "US/DIVIDEND: a dividend payment was withheld above the DTAA rate, and a refund or "
        "reversal of US tax in the year can't be attributed to it, so the treaty-eligible "
        "foreign tax, which is capped per payment (#890), and the section 90 credit can't be "
        "computed.",
    ),
    "same-day-payments": (_SAME_DAY, same_day_gap("US/DIVIDEND")),
}


@pytest.mark.parametrize("case", sorted(OVERRIDE_GAPS))
def test_override_gap_names_only_a_row_no_override_clears(case: str) -> None:
    entry, gap = OVERRIDE_GAPS[case]
    assert form67_row_readiness(entry) == Form67RowReadiness(Form67Readiness.BLANK)
    assert form67_override_gap(entry) == gap


@pytest.mark.parametrize(
    "case", sorted(case for case, (_, gap) in OVERRIDE_GAPS.items() if gap is not None)
)
def test_override_gap_is_the_refusal_an_override_meets(case: str) -> None:
    """With col 7 set, the row refuses with the same gap, then the remedy of
    removing the override again, so the two can't drift apart."""
    entry, gap = OVERRIDE_GAPS[case]
    assert gap is not None
    row = f"{entry.iso_alpha2}/DIVIDEND"
    with pytest.raises(ConfigError) as caught:
        require_no_refused_form67_rows(
            Form67ComputeResult(entries=(replace(entry, tax_payable_normal_inr=Decimal("300")),))
        )
    assert caught.value.message == refused_override(gap, row)


@pytest.mark.parametrize(
    "shape",
    sorted(
        shape for shape, (_, row) in SHAPES.items() if row.readiness is not Form67Readiness.BLANK
    ),
)
def test_override_gap_refuses_a_row_that_is_not_blank(shape: str) -> None:
    with pytest.raises(InvariantError, match="BLANK"):
        form67_override_gap(SHAPES[shape][0])


# ---- #1469: what each BLANK row needs, and the advice that says so ----


@pytest.mark.parametrize("case", sorted(OVERRIDE_GAPS))
def test_section_91_need_names_the_inputs_only_for_a_no_dtaa_row(case: str) -> None:
    entry, _gap = OVERRIDE_GAPS[case]
    assert form67_section_91_need(entry) == (KY_SECTION_91_NEED if case == "no-dtaa" else None)


@pytest.mark.parametrize(
    "shape",
    sorted(
        shape for shape, (_, row) in SHAPES.items() if row.readiness is not Form67Readiness.BLANK
    ),
)
def test_section_91_need_refuses_a_row_that_is_not_blank(shape: str) -> None:
    with pytest.raises(InvariantError, match="BLANK"):
        form67_section_91_need(SHAPES[shape][0])


@pytest.mark.parametrize("case", sorted(OVERRIDE_GAPS))
def test_override_advice_offers_an_override_only_where_one_clears(case: str) -> None:
    """A DTAA row an override alone clears keeps the generic sentence (no
    advice); a no-DTAA row gets the override and the section 91 inputs; any
    other row gets its gap and no override."""
    entry, gap = OVERRIDE_GAPS[case]
    row = f"{entry.iso_alpha2}/DIVIDEND"
    if case == "dtaa-within-rate":
        expected: tuple[str, ...] = ()
    elif gap is None:
        expected = (override_sentence(entry.iso_alpha2), KY_SECTION_91_NEED)
    else:
        expected = (wont_clear(gap, row),)
    assert form67_override_advice((entry,)) == expected


def test_override_advice_gives_each_row_its_own_advice_in_a_mixed_run() -> None:
    """Beside a row no override clears, the clearable row is named with its own
    override sentence, in entry order; READY rows get none."""
    ready = SHAPES["ready-section-90"][0]
    blank = SHAPES["blank"][0]
    ch, _gap = OVERRIDE_GAPS["unvendored"]
    assert form67_override_advice((ready, ch, blank)) == (
        wont_clear(unvendored_dtaa_gap("CH"), "CH/DIVIDEND"),
        override_sentence("US"),
    )
    assert form67_override_advice((ready, blank, blank)) == ()


# Each OVERRIDE_GAPS case from real inputs: (withholding rows, dividend lines,
# Rule 128 FX policy) that compute_form67_entries_from_wht turns into that
# BLANK row.
_KY_ISIN = Isin("KY99995C1000")
_CH_ISIN = Isin("CH99995C1002")
_AT_85 = FixedRatePolicy(rate=Decimal("85"))


def _country_payment(
    isin: Isin, iso: str, tax: str
) -> tuple[WithholdingTax, ScheduleOsDividendLine]:
    """A USD 100.00 dividend of a synthetic ``iso`` security and ``tax`` withheld."""
    symbol = Ticker(f"X{iso}")
    return (
        _wht(
            isin=isin, symbol=symbol, amount=Decimal(tax).copy_negate(), description=f"X{iso} Tax"
        ),
        _os_line(
            isin=isin,
            symbol=symbol,
            description=f"X{iso}",
            issuer_country_iso2=iso,
            amount_gross_usd=Decimal("100.00"),
            amount_gross_inr=Decimal("8500"),
        ),
    )


_US_WHT, _US_LINE = _country_payment(_DEFAULT_ISIN_US, "US", "25.00")
_KY_WHT, _KY_LINE = _country_payment(_KY_ISIN, "KY", "10.00")
_CH_WHT, _CH_LINE = _country_payment(_CH_ISIN, "CH", "15.00")
_SAME_YEAR_REFUND = _UNPROVEN_SOURCE_RATE_CASES["same-year-refund"]
_REFUND_BESIDE_ABOVE = _REFUND_BESIDE_ABOVE_RATE_PAYMENTS["refund-of-one-row-of-above-rate-payment"]
OVERRIDE_GAP_INPUTS: dict[
    str, tuple[list[WithholdingTax], list[ScheduleOsDividendLine], object]
] = {
    "dtaa-within-rate": ([_US_WHT], [_US_LINE], _AT_85),
    "no-dtaa": ([_KY_WHT], [_KY_LINE], _AT_85),
    "unvendored": ([_CH_WHT], [_CH_LINE], _AT_85),
    "unproven-source-rate": (
        _SAME_YEAR_REFUND[0],
        [_SAME_YEAR_REFUND[1]],
        DateAwareRatePolicy(_SAME_YEAR_REFUND[2]),
    ),
    "refund-beside-above-rate-payment": (
        _REFUND_BESIDE_ABOVE[0],
        _REFUND_BESIDE_ABOVE[1],
        DateAwareRatePolicy(_REFUND_BESIDE_ABOVE[2]),
    ),
    # #1469's example: same-day dividends of USD 100.00 and USD 40.00 of one
    # security, with USD 25.00 and USD 10.00 withheld.
    "same-day-payments": (
        [_US_WHT, replace(_US_WHT, amount=Decimal("-10.00"))],
        [
            _US_LINE,
            replace(_US_LINE, amount_gross_usd=Decimal("40.00"), amount_gross_inr=Decimal("3400")),
        ],
        _AT_85,
    ),
}
# What an exact override and the section 91 inputs say when the advice asks.
_OVERRIDE_INR = Decimal("3000")
_SECTION_91 = Form67Section91Config(
    total_indian_tax_before_91_relief_inr=Decimal("30000"), total_income_inr=Decimal("100000")
)


def _compute_case(case: str, config: Form67Config | None = None) -> Form67ComputeResult:
    wht, lines, fx = OVERRIDE_GAP_INPUTS[case]
    return compute_form67_entries_from_wht(
        wht,
        os_dividend_lines=lines,
        wht_fx_policy=cast(Any, fx),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=config,
    )


def test_every_override_gap_case_has_real_inputs() -> None:
    assert sorted(OVERRIDE_GAP_INPUTS) == sorted(OVERRIDE_GAPS)


@pytest.mark.parametrize("case", sorted(OVERRIDE_GAPS))
def test_override_gap_case_reruns_with_its_advice_applied(case: str) -> None:
    """#1469: each case computed for real is the BLANK row with that gap. Where
    the advice offers an override, adding exactly what it names (the override,
    and the [form67.section_91] inputs when it names them) computes a credit.
    Where it offers none, adding an override anyway refuses with the gap."""
    (entry,) = _compute_case(case).entries
    gap = OVERRIDE_GAPS[case][1]
    assert form67_row_readiness(entry) == Form67RowReadiness(Form67Readiness.BLANK)
    assert form67_override_gap(entry) == gap
    row = f"{entry.iso_alpha2}/DIVIDEND"
    override = Form67TaxPayableOverride(entry.iso_alpha2, entry.source_of_income, _OVERRIDE_INR)
    advice = " ".join(form67_override_advice((entry,)))
    need = form67_section_91_need(entry)
    if gap is not None:
        assert f"To have the tool fill {row}" not in advice
        assert f"won't clear {row}." in advice
        with pytest.raises(ConfigError) as caught:
            require_no_refused_form67_rows(_compute_case(case, Form67Config((override,))))
        assert caught.value.message.startswith(f"Form 67 cannot be produced: {gap} ")
        return
    section_91 = None
    if need is not None:
        assert need in advice
        assert "total_indian_tax_before_91_relief_inr and total_income_inr" in need
        section_91 = _SECTION_91
    result = _compute_case(case, Form67Config((override,), section_91))
    require_no_refused_form67_rows(result)
    (rerun,) = result.entries
    assert form67_row_readiness(rerun) == Form67RowReadiness(Form67Readiness.READY)
    assert rerun.filed.total_credit_inr is not None
    assert rerun.filed.total_credit_inr > 0


def test_no_dtaa_override_alone_still_refuses_for_the_section_91_inputs() -> None:
    """As before #1469: the override without [form67.section_91] refuses."""
    override = Form67TaxPayableOverride("KY", Form67SourceOfIncome.DIVIDEND, _OVERRIDE_INR)
    with pytest.raises(ConfigError) as caught:
        require_no_refused_form67_rows(_compute_case("no-dtaa", Form67Config((override,))))
    assert caught.value.message == KY_SECTION_91_REFUSAL


# #1491: the readiness each real input's row gets with each [form67] config.
# Every compute branch lands on BLANK, READY or a named refusal, never on the
# invariant: a row the compute leaves with col 7 and no credit always has a
# named gap.
_GAP_WITH_COL_7: dict[str, Form67RowReadiness] = {
    "dtaa-within-rate": Form67RowReadiness(Form67Readiness.READY),
    "no-dtaa": _refused(Form67RefusalReason.SECTION_91_INPUTS_MISSING),
    "unvendored": _refused(Form67RefusalReason.NO_VENDORED_DTAA),
    "unproven-source-rate": _refused(Form67RefusalReason.SOURCE_RATE_UNPROVEN),
    "refund-beside-above-rate-payment": _refused(Form67RefusalReason.PAYMENT_ALLOCATION_UNPROVEN),
    "same-day-payments": _refused(Form67RefusalReason.PAYMENT_ALLOCATION_UNPROVEN),
}


@pytest.mark.parametrize("config", ["none", "override", "override-and-section-91"])
@pytest.mark.parametrize("case", sorted(OVERRIDE_GAP_INPUTS))
def test_every_computed_row_gets_a_named_readiness(case: str, config: str) -> None:
    (blank,) = _compute_case(case).entries
    override = Form67TaxPayableOverride(blank.iso_alpha2, blank.source_of_income, _OVERRIDE_INR)
    expected = _GAP_WITH_COL_7[case]
    if config == "none":
        form67_config = None
        expected = Form67RowReadiness(Form67Readiness.BLANK)
    elif config == "override":
        form67_config = Form67Config((override,))
    else:
        form67_config = Form67Config((override,), _SECTION_91)
        if case == "no-dtaa":
            expected = Form67RowReadiness(Form67Readiness.READY)
    (entry,) = _compute_case(case, form67_config).entries
    assert form67_row_readiness(entry) == expected
