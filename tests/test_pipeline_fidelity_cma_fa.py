"""Fidelity CMA Schedule FA peak-bound config and pipeline tests."""

from __future__ import annotations

import csv
import hashlib
import inspect
import json
import re
import urllib.parse
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import partial
from pathlib import Path
from typing import Any, Literal

import pytest

import ibkr_fa.pipeline_fidelity_cma_fa as fa_mod
from ibkr_fa import cli
from ibkr_fa.cli import main
from ibkr_fa.config import TaxpayerStatus, load_user_config
from ibkr_fa.exceptions import IbkrFaError, InvariantError
from ibkr_fa.models import FaA3PeakBlankReason
from ibkr_fa.models._row_checks import upload_text
from ibkr_fa.models.fa_a2_row import FaA2NatureOfAmount, FaA2Row, FaA2Status
from ibkr_fa.output.cbdt_json_schema import A2_NATURE_LETTERS
from ibkr_fa.output.schedules.fa import A2_UPLOAD_HEADERS, A3_UPLOAD_HEADERS
from ibkr_fa.parser.fidelity_cma import (
    CmaCashActivity,
    CmaCashActivityKind,
    CmaCoreTransaction,
    CmaCoreTransactionKind,
    CmaStatement,
    CmaYearEndIncomeSummary,
    CmaYearEndPosition,
)
from ibkr_fa.prices import load_price_series
from ibkr_fa.prices.cache import write_cache, write_cache_lock
from ibkr_fa.prices.nasdaq import fetch_nasdaq_mutual_fund_daily_closes_usd
from ibkr_fa.prices.series import (
    CONTEMPORANEOUS_BASIS,
    DEFAULT_MAX_PRICE_STALENESS_DAYS,
    PriceSeries,
)
from ibkr_fa.prices.yahoo import DailyCloseFetcher
from ibkr_fa.reference.us_market_calendar import (
    supports_calendar_year,
    us_market_session_on_or_before,
    us_market_sessions,
)
from ibkr_fa.types import Ticker
from tests._crash_reports import assert_unchained_refusal
from tests._exact_refusals import INEXACT_SUM
from tests._fidelity_cma import (
    A2_PRICING_2024_REFUSAL,
    CREDIT_INTEREST,
    DIVIDEND_WITHHOLDING,
    SPAXX_DIVIDEND,
    WITHHOLDING_CENTS_REFUSAL,
    YEAR_END_HOLDINGS_REFUSAL,
    YEAR_END_TOTALS_REFUSAL,
    YEAR_END_WITHHOLDING_SIGN_REFUSAL,
    income_year_end,
)
from tests._fidelity_cma import annual_statement as _statement
from tests._fidelity_cma_bundle import (
    FA_SHA256,
    cma_inputs,  # noqa: F401 - fixture
    folder_digests,
    spaxx_nav,
)
from tests._fidelity_cma_fa_inputs import (
    ACCRUED_INTEREST_REFUSAL,
    NO_SESSION_CLOSE,
    NON_SESSION_CLOSE,
    accrued_interest_statements,
    assert_no_marker,
    inverting_statement,
    marker_statement,
)
from tests._fidelity_cma_fa_inputs import CMA_MARKER_ACCOUNT as ACCOUNT
from tests._fidelity_cma_fa_inputs import CMA_MARKER_HOLDER as HOLDER
from tests._fidelity_cma_fa_inputs import CMA_MARKER_NAV as NAV
from tests._fidelity_cma_fa_run import UPPER_BOUND_CONFIG as _UPPER_BOUND
from tests._fidelity_cma_fa_run import cma_fa_price_series as _series
from tests._fidelity_cma_fa_run import cma_fa_year_end as _year_end
from tests._fidelity_cma_fa_run import prepare_cma_fa as _prepare
from tests._fidelity_cma_fa_run import redemption_statement as _redemption_statement
from tests._fidelity_cma_fa_run import run_cma_fa_cli as _cli

_ACCOUNT = "Z99-999999"


def test_nameless_output_refuses_before_cleanup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        fa_mod, "remove_previous_outputs", lambda *_: pytest.fail("cleanup must not run")
    )
    with pytest.raises(fa_mod.FidelityCmaFaError) as exc:
        fa_mod.run_fidelity_cma_fa(
            input_dir=tmp_path,
            config_path=tmp_path / "user.toml",
            output_path=Path(),
            calendar_year=2025,
        )
    assert str(exc.value) == (
        "fidelity-cma-fa requires --output to name a file for the A2 companion output"
    )
    assert isinstance(exc.value.__context__, ValueError)
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__


def _income_statement() -> CmaStatement:
    """The base statement plus a withheld SPAXX dividend and credit interest,
    all left as free credit balance."""
    return replace(
        _statement(),
        income_credits=(SPAXX_DIVIDEND, CREDIT_INTEREST),
        withholdings=(DIVIDEND_WITHHOLDING,),
        free_credit_balance_usd=Decimal("3.25"),
        ending_account_value_usd=Decimal("13.25"),
        account_value_usd=Decimal("13.25"),
    )


def _income_year_end() -> CmaYearEndIncomeSummary:
    return income_year_end(position=_year_end().position)


def _a2_row(output: Path) -> list[str]:
    path = output.with_name("schedule-fa-a2.csv")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        _header, row = list(csv.reader(handle))
    return row


# sha256 of the A3 and A2 review CSVs, taken before #624 moved CMA income
# normalization and the year-end check out of the pipeline; the A2 digests
# were re-taken when #888 reworded the two peak-bound audit headers. Both end
# lines with CRLF, which .gitattributes and pre-commit would rewrite in
# committed golden files, so the bytes are pinned by digest.
_PINNED_OUTPUT_SHA256 = {
    "no-income": {
        "schedule-fa.csv": "aad7cc2d04da6b7984227e1cc65cffade545bce5c76a3ea8f8f8793aa123f6a2",
        "schedule-fa-a2.csv": "fb1fc3c2fc63f78b94a1ce6e6ab840916134a86b2cf3d8339f3f75d38f019803",
    },
    "income": {
        "schedule-fa.csv": "df7f773188d077db346ef9fca276d80b2ebef9de84d5fc25df4c3275d1c576ed",
        "schedule-fa-a2.csv": "d5164fc1dbe16a76cded887723cf0bf866a150d8f7752a7f44d15309f0b7e640",
    },
}


@pytest.mark.parametrize("fixture", ["no-income", "income"])
def test_review_csv_outputs_match_pinned_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
) -> None:
    income = fixture == "income"
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        statement=_income_statement() if income else None,
        year_end=_income_year_end() if income else None,
    )
    fa_mod.run_fidelity_cma_fa(
        input_dir=tmp_path,
        config_path=config,
        output_path=output,
        calendar_year=2025,
    )
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (output, output.with_name("schedule-fa-a2.csv"))
    } == _PINNED_OUTPUT_SHA256[fixture]


def _a3_rows(output: Path) -> list[dict[str, str]]:
    with output.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


_SPAXX_A3_ENTITY = {
    "Country Name and Code": "2-UNITED STATES OF AMERICA",
    "Name of entity": "Fidelity Government Money Market Fund",
    "Address of entity": "245 Summer Street, Boston, MA",
    "ZIP Code": "02210",
    "Nature of entity": "Money market mutual fund shares",
    "Date of acquiring the interest": "01/01/2025",
    "Total gross amount paid/credited with respect to the holding during the period": "0",
    "_audit: Source ISIN": "US31617H1023",
    "_audit: Source ticker": "SPAXX",
    "_audit: Source country (ISO 3166-1 alpha-2)": "US",
    "_audit: Acquisition date": "2025-01-01",
    "_audit: TTBR on acquisition date (INR per USD)": "85.22",
    "_audit: TTBR on Dec 31 (INR per USD)": "89.47",
}


# Native peaks are on the first day (flat NAV, earliest maximum): quantity x
# 85.22. INR peaks take the highest TTBR in each window: 90.6 on 2025-12-16 for
# the open lot, 87.1 on 2025-02-06 for the lot redeemed on 2025-04-04.
@pytest.mark.parametrize(
    ("peak_currency", "open_peak", "redeemed_peak"),
    [("native", "511", "341"), ("inr", "544", "348")],
)
def test_a3_rows_for_open_and_redeemed_lots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    peak_currency: str,
    open_peak: str,
    redeemed_peak: str,
) -> None:
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        extra_config=f'\n[schedule_fa]\npeak_currency = "{peak_currency}"\n',
        statement=_redemption_statement(),
        year_end=_year_end(quantity="6", market_value="6"),
    )
    with (
        pytest.warns(UserWarning, match="peak override active")
        if peak_currency == "inr"
        else nullcontext()
    ):
        report = fa_mod.run_fidelity_cma_fa(
            input_dir=tmp_path,
            config_path=config,
            output_path=output,
            calendar_year=2025,
        )
    assert report.a3_rows == 2
    assert report.manual_fill_columns == ("A2 Peak Balance During the Period",)
    assert _a3_rows(output) == [
        {
            **_SPAXX_A3_ENTITY,
            "Initial value of the investment": "511",
            "Peak value of investment during the Period": open_peak,
            "Closing balance": "537",
            "Total gross proceeds from sale or redemption of investment during the period": "0",
            "_audit: Cost basis (USD)": "6",
            "_audit: Closing price on Dec 31 (USD)": "1",
            "_audit: Disposal date": "",
        },
        {
            **_SPAXX_A3_ENTITY,
            "Initial value of the investment": "341",
            "Peak value of investment during the Period": redeemed_peak,
            "Closing balance": "0",
            "Total gross proceeds from sale or redemption of investment during the period": "358",
            "_audit: Cost basis (USD)": "4",
            "_audit: Closing price on Dec 31 (USD)": "",
            "_audit: Disposal date": "2025-04-04",
        },
    ]


_A3_PEAK = "Peak value of investment during the Period"
_A3_PEAK_MANUAL = f"A3 {_A3_PEAK}"


# A sweep-fund peak that only the disposal day could set is left blank (owner
# decision 1, #622): on 2025-01-14 the NAV (1.01) and TTBR (86.25) are above
# every earlier day, and a lot bought and redeemed on 2025-03-03 has no other
# day. The open lot keeps its peak.
@pytest.mark.parametrize(
    ("peak_currency", "statement", "navs", "open_peak"),
    [
        (
            "native",
            _redemption_statement(redeemed=date(2025, 1, 14), price="1.01"),
            {date(2025, 1, 14): Decimal("1.01")},
            "523",
        ),
        (
            "inr",
            _redemption_statement(redeemed=date(2025, 1, 14), price="1.01"),
            {date(2025, 1, 14): Decimal("1.01")},
            "544",
        ),
        (
            "native",
            _redemption_statement(bought=date(2025, 3, 3), redeemed=date(2025, 3, 3)),
            None,
            "522",
        ),
        (
            "inr",
            _redemption_statement(bought=date(2025, 3, 3), redeemed=date(2025, 3, 3)),
            None,
            "544",
        ),
    ],
    ids=["strict-max-native", "strict-max-inr", "same-day-native", "same-day-inr"],
)
def test_a3_peak_set_by_the_disposal_day_is_manual_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    peak_currency: str,
    statement: CmaStatement,
    navs: Mapping[date, Decimal] | None,
    open_peak: str,
) -> None:
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        extra_config=f'\n[schedule_fa]\npeak_currency = "{peak_currency}"\n',
        statement=statement,
        year_end=_year_end(quantity="6", market_value="6"),
        navs=navs,
    )
    with (
        pytest.warns(UserWarning, match="peak override active")
        if peak_currency == "inr"
        else nullcontext()
    ):
        report = fa_mod.run_fidelity_cma_fa(
            input_dir=tmp_path,
            config_path=config,
            output_path=output,
            calendar_year=2025,
        )
    assert output.with_name("schedule-fa-a2.csv").exists()
    assert [row[_A3_PEAK] for row in _a3_rows(output)] == [open_peak, ""]
    assert report.manual_fill_columns == ("A2 Peak Balance During the Period", _A3_PEAK_MANUAL)
    assert report.a3_peak_blank_rows == ((FaA3PeakBlankReason.DISPOSAL_DAY_PEAK, (2,)),)
    assert not report.filing_complete


def test_cli_high_peak_basis_refuses_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        extra_config='\n[schedule_fa]\npeak_basis = "high"\n',
    )
    code = main(
        [
            "fidelity-cma-fa",
            "--input",
            str(tmp_path),
            "--config",
            str(config),
            "--output",
            str(output),
            "--calendar-year",
            "2025",
        ]
    )
    assert code == 4
    assert capsys.readouterr().err == (
        'error: fidelity-cma-fa refuses schedule_fa.peak_basis = "high": no source '
        "establishes an intraday high for SPAXX, a money market fund\n"
    )
    assert not output.exists()
    assert not output.with_name("schedule-fa-a2.csv").exists()


def test_default_keeps_peak_blank_and_audits_both_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, output = _prepare(tmp_path, monkeypatch)
    report = fa_mod.run_fidelity_cma_fa(
        input_dir=tmp_path,
        config_path=config,
        output_path=output,
        calendar_year=2025,
    )
    row = _a2_row(output)
    assert report.selected_peak_bound is None
    assert report.manual_fill_columns == ("A2 Peak Balance During the Period",)
    assert row[4] == _ACCOUNT
    assert row[5] == "BENEFICIAL_OWNER"
    assert row[6] == "01/01/2025"
    assert row[7] == ""
    assert Decimal(row[17]) == report.peak_bounds.lower_inr
    assert Decimal(row[19]) == report.peak_bounds.upper_inr
    assert row[21] == ""


@pytest.mark.parametrize(
    "bound",
    ["lower", "upper"],
)
def test_explicit_bound_populates_peak_and_keeps_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound: str,
) -> None:
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        extra_config=(f'\nsuppress_peak_balance_warning = true\npeak_balance_bound = "{bound}"\n'),
    )
    report = fa_mod.run_fidelity_cma_fa(
        input_dir=tmp_path,
        config_path=config,
        output_path=output,
        calendar_year=2025,
    )
    row = _a2_row(output)
    assert report.filing_complete
    selected = report.peak_bounds.lower_inr if bound == "lower" else report.peak_bounds.upper_inr
    assert row[7] == str(selected.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    assert Decimal(row[17]) == report.peak_bounds.lower_inr
    assert Decimal(row[19]) == report.peak_bounds.upper_inr
    assert row[21] == bound


# The SBI TTBR on 2025-12-31 (bundled; the TTBR lookup is not stubbed).
_DEC_31_TTBR = Decimal("89.47")


@pytest.mark.parametrize("bound", [None, "lower", "upper"])
def test_a2_rows_equal_the_rows_built_with_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound: Literal["lower", "upper"] | None,
) -> None:
    # #619 characterization: each row equals the build_fa_a2_row result that
    # replace() then gave the audited bounds and the selected bound, if any.
    # Every other field is worked out here, not taken from the run: the CMA
    # custodian's fixed identity, then from _income_statement() its unmasked
    # Z99-999999 account opened with its 2025-01-01 statement, its $13.25
    # account value and $3.25 free credit balance on Dec 31, and its $1.00
    # dividend and $0.50 interest, one row per nature, each at the Dec 31 TTBR.
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        extra_config=(
            ""
            if bound is None
            else f'\nsuppress_peak_balance_warning = true\npeak_balance_bound = "{bound}"\n'
        ),
        statement=_income_statement(),
        year_end=_income_year_end(),
    )
    report = fa_mod.run_fidelity_cma_fa(
        input_dir=tmp_path,
        config_path=config,
        output_path=output,
        calendar_year=2025,
    )
    bounds = report.peak_bounds
    selected = {None: None, "lower": bounds.lower_inr, "upper": bounds.upper_inr}[bound]
    dividend_inr = Decimal("1.00") * _DEC_31_TTBR
    interest_inr = Decimal("0.50") * _DEC_31_TTBR
    account = FaA2Row(
        country_code="2",
        country_name="UNITED STATES OF AMERICA",
        institution_name="National Financial Services LLC",
        institution_address="245 Summer Street, Boston, MA",
        institution_zip="02210",
        account_number=_ACCOUNT,
        status=FaA2Status.BENEFICIAL_OWNER,
        account_opening_date=date(2025, 1, 1),
        peak_balance_inr=selected,
        closing_balance_inr=Decimal("13.25") * _DEC_31_TTBR,
        nature_of_amount=None,
        gross_amount_inr=None,
        base_currency="USD",
        audit_closing_balance_base_ccy=Decimal("13.25"),
        audit_ttbr_dec31=_DEC_31_TTBR,
        audit_account_number_from_config=False,
        audit_nav_cross_checked=True,
        audit_gross_components={"Dividend": dividend_inr, "Interest": interest_inr},
        audit_period_end_cash_base_ccy=Decimal("3.25"),
        audit_peak_balance_lower_inr=bounds.lower_inr,
        audit_peak_balance_upper_inr=bounds.upper_inr,
        audit_peak_balance_lower_date=bounds.lower_date,
        audit_peak_balance_upper_date=bounds.upper_date,
        audit_peak_balance_bound=bound,
    )

    assert report.a2_rows == (
        replace(
            account,
            nature_of_amount=FaA2NatureOfAmount.INTEREST,
            gross_amount_inr=interest_inr,
        ),
        replace(
            account,
            nature_of_amount=FaA2NatureOfAmount.DIVIDEND,
            gross_amount_inr=dividend_inr,
        ),
    )


def test_year_end_holdings_mismatch_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        year_end=_year_end(quantity="11"),
    )
    with pytest.raises(fa_mod.FidelityCmaFaError, match=YEAR_END_HOLDINGS_REFUSAL):
        fa_mod.run_fidelity_cma_fa(
            input_dir=tmp_path,
            config_path=config,
            output_path=output,
            calendar_year=2025,
        )
    assert not output.exists()
    assert not output.with_name("schedule-fa-a2.csv").exists()


def test_year_end_holder_mismatch_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    year_end = _year_end()
    year_end = CmaYearEndIncomeSummary(
        tax_year=year_end.tax_year,
        account_id=year_end.account_id,
        account_holder="OTHER HOLDER",
        currency=year_end.currency,
        dividends_usd=year_end.dividends_usd,
        interest_usd=year_end.interest_usd,
        withholding_usd=year_end.withholding_usd,
        position=year_end.position,
    )
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        year_end=year_end,
    )
    with pytest.raises(fa_mod.FidelityCmaFaError, match="account identity"):
        fa_mod.run_fidelity_cma_fa(
            input_dir=tmp_path,
            config_path=config,
            output_path=output,
            calendar_year=2025,
        )


@pytest.mark.parametrize(
    ("year_end", "message"),
    [
        (
            replace(_income_year_end(), dividends_usd=Decimal("1.01")),
            YEAR_END_TOTALS_REFUSAL,
        ),
        # Same magnitude as the statement's -0.25, opposite sign.
        (
            replace(_income_year_end(), withholding_usd=Decimal("0.25")),
            YEAR_END_WITHHOLDING_SIGN_REFUSAL,
        ),
    ],
    ids=["dividend-cent-off", "withholding-sign"],
)
def test_year_end_income_mismatch_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    year_end: CmaYearEndIncomeSummary,
    message: str,
) -> None:
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        statement=_income_statement(),
        year_end=year_end,
    )
    with pytest.raises(fa_mod.FidelityCmaFaError, match=message):
        fa_mod.run_fidelity_cma_fa(
            input_dir=tmp_path,
            config_path=config,
            output_path=output,
            calendar_year=2025,
        )
    assert not output.exists()


@pytest.mark.parametrize(
    ("target", "error", "message"),
    [
        # #721: the loader builds the source record, so the WithholdingTax
        # model refuses a NaN statement row before the year-end check runs.
        ("statement", InvariantError, "WithholdingTax.amount must be a non-zero finite"),
        ("year-end", fa_mod.FidelityCmaFaError, WITHHOLDING_CENTS_REFUSAL),
    ],
    ids=["statement", "year-end"],
)
def test_nonfinite_withholding_refuses_with_a_typed_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    error: type[Exception],
    message: str,
) -> None:
    statement = _income_statement()
    year_end = _income_year_end()
    if target == "statement":
        statement = replace(
            statement,
            withholdings=(replace(statement.withholdings[0], amount_usd=Decimal("NaN")),),
        )
    else:
        year_end = replace(year_end, withholding_usd=Decimal("NaN"))
    config, output = _prepare(tmp_path, monkeypatch, statement=statement, year_end=year_end)
    with pytest.raises(error, match=message):
        fa_mod.run_fidelity_cma_fa(
            input_dir=tmp_path,
            config_path=config,
            output_path=output,
            calendar_year=2025,
        )
    assert not output.exists()


def test_missing_cma_status_refuses_with_supported_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, output = _prepare(tmp_path, monkeypatch)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'status = "BeneficialOwner"\n',
            "",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(fa_mod.FidelityCmaFaError, match="INDIVIDUAL TOD"):
        fa_mod.run_fidelity_cma_fa(
            input_dir=tmp_path,
            config_path=config,
            output_path=output,
            calendar_year=2025,
        )


def test_missing_status_remedy_loads_as_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal's assignment, pasted under [fidelity_cma], loads and clears the check."""
    config, output = _prepare(tmp_path, monkeypatch)
    without_status = config.read_text(encoding="utf-8").replace(
        'status = "BeneficialOwner"\n', "", 1
    )
    config.write_text(without_status, encoding="utf-8")
    with pytest.raises(fa_mod.FidelityCmaFaError) as refusal:
        fa_mod.run_fidelity_cma_fa(
            input_dir=tmp_path,
            config_path=config,
            output_path=output,
            calendar_year=2025,
        )
    assert "[fidelity_cma]" in str(refusal.value)
    remedy = re.search(r"status = \S+", str(refusal.value))
    assert remedy is not None
    config.write_text(
        without_status.replace("[fidelity_cma]\n", f"[fidelity_cma]\n{remedy.group()}\n", 1),
        encoding="utf-8",
    )
    cma = load_user_config(config).fidelity_cma
    assert cma is not None
    assert cma.status is TaxpayerStatus.BENEFICIAL_OWNER
    fa_mod.run_fidelity_cma_fa(
        input_dir=tmp_path,
        config_path=config,
        output_path=output,
        calendar_year=2025,
    )
    assert output.exists()


def test_cli_explicit_bound_suppresses_peak_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        extra_config=('\nsuppress_peak_balance_warning = true\npeak_balance_bound = "upper"\n'),
    )
    code = main(
        [
            "fidelity-cma-fa",
            "--input",
            str(tmp_path),
            "--config",
            str(config),
            "--output",
            str(output),
            "--calendar-year",
            "2025",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "same-day credits and debits lack" not in captured.err
    assert "A2 peak selection: upper" in captured.out


_CLI_OUTPUT = Path(__file__).parent / "fixtures" / "fidelity_cma_fa_output"
_SAME_DAY_REDEMPTION = _redemption_statement(bought=date(2025, 3, 3), redeemed=date(2025, 3, 3))
_SIX_UNITS = _year_end(quantity="6", market_value="6")
# Extra config, statement and year-end summary per case; None keeps _prepare's
# default. The same-day redemption leaves its lot's A3 peak blank; that lot is
# A3 row 2, after the open lot (#1552).
_CLI_CASES = {
    "a2-peak": ("", None, None),
    "a2-and-a3-peak": ("", _SAME_DAY_REDEMPTION, _SIX_UNITS),
    "a3-peak": (_UPPER_BOUND, _SAME_DAY_REDEMPTION, _SIX_UNITS),
}


@pytest.mark.parametrize("case", list(_CLI_CASES))
def test_cli_manual_fill_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """The complete CLI text for each mix of manual-fill columns, with the tmp
    path replaced by ``<tmp>`` as in ``tests/test_cli_run_all_output.py`` (#759).
    A manual-fill exit still writes both review CSVs."""
    extra_config, statement, year_end = _CLI_CASES[case]
    config, output = _prepare(
        tmp_path, monkeypatch, extra_config=extra_config, statement=statement, year_end=year_end
    )
    code = main(
        [
            "fidelity-cma-fa",
            "--input",
            str(tmp_path),
            "--config",
            str(config),
            "--output",
            str(output),
            "--calendar-year",
            "2025",
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    for stream, text in (("stdout", captured.out), ("stderr", captured.err)):
        expected = (_CLI_OUTPUT / f"{case}.{stream}.txt").read_text(encoding="utf-8")
        assert text.replace(str(tmp_path), "<tmp>") == expected, stream
    assert output.exists()
    assert output.with_name("schedule-fa-a2.csv").exists()


@pytest.mark.parametrize(
    ("statement", "calendar_year", "stderr"),
    [
        (
            _statement(),
            "2024",
            "error: no CMA monthly statements cover the requested calendar year\n",
        ),
        (
            replace(_statement(), period_end=date(2025, 11, 30)),
            "2025",
            "error: ParseError in section 'Fidelity CMA monthly statement' at row 0: "
            "statement bundle does not end at the requested date\n",
        ),
    ],
    ids=["no-statement-in-year", "ends-before-dec-31"],
)
def test_cli_calendar_year_slice_refusals_keep_their_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    statement: CmaStatement,
    calendar_year: str,
    stderr: str,
) -> None:
    config, output = _prepare(tmp_path, monkeypatch, statement=statement)
    code = main(
        [
            "fidelity-cma-fa",
            "--input",
            str(tmp_path),
            "--config",
            str(config),
            "--output",
            str(output),
            "--calendar-year",
            calendar_year,
        ]
    )
    assert code == 4
    assert capsys.readouterr().err == stderr


_INVERTING_YEAR_END = _year_end(quantity="1", market_value="1")


# The inverting statement's bounds: USD 5900 on 2025-12-10 at TTBR 89.45 is
# INR 527755.00, and USD 6000 on 2025-03-10 at TTBR 86.87 is INR 521220.00.
_INVERTED_BOUND_VALUES = (
    "5900",
    "6000",
    "2025-12-10",
    "2025-03-10",
    "89.45",
    "86.87",
    "527755",
    "521220",
)


def _inverted_refusal(bound: str) -> str:
    """The fixed refusal: the config field, never the bounds, dates or TTBRs (#1047)."""
    return (
        f'fidelity-cma-fa refuses peak_balance_bound = "{bound}": the A2 peak bounds '
        "are ordered in native currency, and converting each at its own date's TTBR "
        "reverses their rupee order, so the lower bound is the larger one in INR. "
        "Remove suppress_peak_balance_warning and peak_balance_bound "
        "from [fidelity_cma] to write output with a blank A2 peak."
    )


def _assert_no_bound_values(text: str) -> None:
    """``text`` has none of the statement-derived bound figures (#1047)."""
    for value in _INVERTED_BOUND_VALUES:
        assert value not in text


@pytest.mark.parametrize("bound", ["lower", "upper"])
def test_attested_bound_refuses_when_the_inr_bounds_invert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound: str,
) -> None:
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        extra_config=f'\nsuppress_peak_balance_warning = true\npeak_balance_bound = "{bound}"\n',
        statement=inverting_statement(debit_first=False),
        year_end=_INVERTING_YEAR_END,
    )
    monkeypatch.setattr(
        fa_mod,
        "write_output_bundle",
        lambda *_args: pytest.fail("inverted attested bounds must refuse before writing output"),
    )
    with pytest.raises(fa_mod.FidelityCmaFaError) as excinfo:
        fa_mod.run_fidelity_cma_fa(
            input_dir=tmp_path,
            config_path=config,
            output_path=output,
            calendar_year=2025,
        )
    assert str(excinfo.value) == _inverted_refusal(bound)
    _assert_no_bound_values(str(excinfo.value))
    assert not output.exists()
    assert not output.with_name("schedule-fa-a2.csv").exists()


_FILING_CSV = ("--format", "filing-csv")


@pytest.mark.parametrize("format_args", [(), _FILING_CSV], ids=["review-csv", "filing-csv"])
@pytest.mark.parametrize("bound", ["lower", "upper"])
def test_cli_inverted_attested_bound_exits_4_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bound: str,
    format_args: tuple[str, ...],
) -> None:
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        extra_config=f'\nsuppress_peak_balance_warning = true\npeak_balance_bound = "{bound}"\n',
        statement=inverting_statement(debit_first=False),
        year_end=_INVERTING_YEAR_END,
    )
    monkeypatch.setattr(
        fa_mod,
        "write_output_bundle",
        lambda *_args: pytest.fail("inverted attested bounds must refuse before writing output"),
    )
    assert _cli(tmp_path, config, output, *format_args) == 4
    err = capsys.readouterr().err
    assert err == f"error: {_inverted_refusal(bound)}\n"
    _assert_no_bound_values(err)
    assert not output.exists()
    assert not output.with_name("schedule-fa-a2.csv").exists()


def test_cli_inverted_bounds_without_attestation_stay_manual_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        statement=inverting_statement(debit_first=False),
        year_end=_INVERTING_YEAR_END,
    )
    assert _cli(tmp_path, config, output) == 3
    row = _a2_row(output)
    assert row[7] == ""
    assert (Decimal(row[17]), Decimal(row[19])) == (Decimal("527755.00"), Decimal("521220.00"))
    assert output.exists()


def test_cli_inverted_bounds_without_attestation_leave_the_upload_peak_blank(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        statement=inverting_statement(debit_first=False),
        year_end=_INVERTING_YEAR_END,
    )
    assert _cli(tmp_path, config, output, *_FILING_CSV) == 3
    _header, row = _upload(output.with_name("schedule-fa-a2.csv"))
    assert row[8] == ""


# ---- --format filing-csv: the Schedule FA upload files (#1385) ---------------


def _upload(path: Path) -> list[list[str]]:
    """The upload's rows, after checking its dialect: CRLF-terminated ASCII
    lines (so no BOM), each of 12 fields, with no quote or audit column."""
    data = path.read_bytes()
    assert data.isascii()
    text = data.decode("ascii")
    assert text.endswith("\r\n")
    lines = text.removesuffix("\r\n").split("\r\n")
    for line in lines:
        assert "\r" not in line
        assert "\n" not in line
        assert line.count(",") == 11
        assert '"' not in line
        assert "_audit" not in line
    return [line.split(",") for line in lines]


def _review_a2(output: Path) -> list[list[str]]:
    """The A2 review CSV's rows after its header."""
    with output.with_name("schedule-fa-a2.csv").open(encoding="utf-8-sig", newline="") as handle:
        _header, *rows = csv.reader(handle)
    return rows


def _iso(review_date: str) -> str:
    """A review file's DD/MM/YYYY date as the upload writes it."""
    return datetime.strptime(review_date, "%d/%m/%Y").date().isoformat()


def _digests(*paths: Path) -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _upload_inputs(
    inputs: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> tuple[Path, Path, dict[str, str]]:
    """The input directory and config for ``inputs``, and the review CSV
    digests a default (review-csv) run on them writes."""
    if inputs == "cma-bundle":
        statements, _tax_forms, config = request.getfixturevalue("cma_inputs")
        monkeypatch.setattr(
            cli,
            "run_fidelity_cma_fa",
            partial(
                fa_mod.run_fidelity_cma_fa,
                price_fetcher=spaxx_nav,
                price_now=datetime(2026, 6, 1, tzinfo=UTC),
            ),
        )
        return statements, config, FA_SHA256
    income = inputs == "income"
    config, _output = _prepare(
        tmp_path,
        monkeypatch,
        statement=_income_statement() if income else None,
        year_end=_income_year_end() if income else None,
    )
    return tmp_path, config, _PINNED_OUTPUT_SHA256[inputs]


# Each upload's A2 nature letters and amounts, in the review file's row order.
_UPLOAD_NATURES = {
    "no-income": [("N", "0")],
    "income": [("I", "45"), ("D", "89")],
    "cma-bundle": [("I", "107"), ("D", "4295"), ("S", "8947")],
}


@pytest.mark.parametrize("inputs", list(_UPLOAD_NATURES))
def test_filing_csv_writes_the_upload_with_the_review_files_cells(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    inputs: str,
) -> None:
    """Both upload files are the shared renderer's dialect, with CMA's fixed
    identities, and every date and amount is the review file's portal cell for
    the same row, as an ISO date or nature letter where the upload needs one."""
    input_dir, config, review_digests = _upload_inputs(inputs, tmp_path, monkeypatch, request)
    review = tmp_path / "review" / "schedule-fa.csv"
    upload = tmp_path / "upload" / "schedule-fa.csv"
    cache = ("--price-cache-dir", str(tmp_path / "price-cache"))
    assert _cli(input_dir, config, review, *cache) == 3
    assert _cli(input_dir, config, upload, *cache, *_FILING_CSV) == 3
    # With no --format the run writes the pinned review CSVs.
    assert _digests(review, review.with_name("schedule-fa-a2.csv")) == review_digests

    institution = fa_mod._A2_INSTITUTION
    a2_header, *a2 = _upload(upload.with_name("schedule-fa-a2.csv"))
    assert tuple(a2_header) == A2_UPLOAD_HEADERS
    review_a2 = _review_a2(review)
    assert len(a2) == len(review_a2)
    for row, portal in zip(a2, review_a2, strict=True):
        assert row[:7] == [
            "UNITED STATES OF AMERICA",
            "2",
            institution.name,
            upload_text(institution.address),
            institution.zip_code,
            "Z99-999999",
            "BENEFICIAL_OWNER",
        ]
        assert "," not in institution.zip_code
        assert row[7:] == [
            _iso(portal[6]),
            portal[7],
            portal[8],
            A2_NATURE_LETTERS[FaA2NatureOfAmount(portal[9])],
            portal[10],
        ]
    assert [(row[10], row[11]) for row in a2] == _UPLOAD_NATURES[inputs]

    entity = fa_mod._SPAXX_ENTITY
    assert entity.address is not None
    a3_header, *a3 = _upload(upload)
    assert tuple(a3_header) == A3_UPLOAD_HEADERS
    review_a3 = _a3_rows(review)
    assert len(a3) == len(review_a3) > 0
    for serial, (row, portal) in enumerate(zip(a3, review_a3, strict=True), start=1):
        assert row[:6] == [
            str(serial),
            "2",
            entity.name,
            upload_text(entity.address),
            entity.zip_code,
            "Money market mutual fund shares",
        ]
        assert row[6:] == [
            _iso(portal["Date of acquiring the interest"]),
            *(portal[header] for header in A3_UPLOAD_HEADERS[7:]),
        ]


def test_library_writes_the_upload_with_filing_csv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, output = _prepare(tmp_path, monkeypatch)
    report = fa_mod.run_fidelity_cma_fa(
        input_dir=tmp_path,
        config_path=config,
        output_path=output,
        calendar_year=2025,
        format_id="filing-csv",
    )
    assert report.format_id == "filing-csv"
    assert report.manual_fill_columns == ("A2 Peak Balance During the Period",)
    assert tuple(_upload(report.a2_output_path)[0]) == A2_UPLOAD_HEADERS
    assert tuple(_upload(output)[0]) == A3_UPLOAD_HEADERS


# Each _CLI_CASES case under --format filing-csv, plus the attested bound of
# test_cli_explicit_bound_suppresses_peak_warning, which leaves nothing manual.
_FILING_CLI_CASES = {**_CLI_CASES, "attested": (_UPPER_BOUND, None, None)}
_OPENING_DATE_WARNING = (
    "warning: the CMA account opening date uses the first supplied statement date"
)


@pytest.mark.parametrize("case", list(_FILING_CLI_CASES))
def test_cli_filing_csv_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """The complete CLI text under --format filing-csv, ``<tmp>`` for the tmp
    path, and the upload's peak cells: each is the review file's portal cell,
    so a peak the run leaves for manual entry is empty."""
    extra_config, statement, year_end = _FILING_CLI_CASES[case]
    config, output = _prepare(
        tmp_path, monkeypatch, extra_config=extra_config, statement=statement, year_end=year_end
    )
    review = tmp_path / "review" / "schedule-fa.csv"
    expected_code = 0 if case == "attested" else 3
    assert _cli(tmp_path, config, review) == expected_code
    capsys.readouterr()

    assert _cli(tmp_path, config, output, *_FILING_CSV) == expected_code
    captured = capsys.readouterr()
    assert captured.err.startswith(_OPENING_DATE_WARNING)
    for stream, text in (("stdout", captured.out), ("stderr", captured.err)):
        expected = (_CLI_OUTPUT / f"{case}.filing-csv.{stream}.txt").read_text(encoding="utf-8")
        assert text.replace(str(tmp_path), "<tmp>") == expected, stream

    _header, *a2 = _upload(output.with_name("schedule-fa-a2.csv"))
    peaks = {row[8] for row in a2}
    assert peaks == {row[7] for row in _review_a2(review)}
    assert (peaks == {""}) is (case in {"a2-peak", "a2-and-a3-peak"})
    _header, *a3 = _upload(output)
    review_a3 = _a3_rows(review)
    assert [row[8] for row in a3] == [row[_A3_PEAK] for row in review_a3]
    # Only the lot redeemed on its purchase day has a blank A3 peak.
    assert [row[8] == "" for row in a3] == [
        row["_audit: Disposal date"] == "2025-03-03" for row in review_a3
    ]


@pytest.mark.parametrize(
    "extra",
    [
        ("--format", "filing-xlsx"),
        ("--format", "cbdt-json-fragment"),
        ("--itr-form", "ITR-2"),
        ("--output-dir", "out"),
    ],
    ids=["filing-xlsx", "cbdt-json-fragment", "itr-form", "output-dir"],
)
def test_cli_refuses_options_fidelity_cma_fa_lacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra: tuple[str, str],
) -> None:
    """argparse refuses each one before the run, exit 2, writing nothing."""
    config, output = _prepare(tmp_path, monkeypatch)
    option, value = extra
    if option == "--output-dir":
        value = str(tmp_path / value)
    with pytest.raises(SystemExit) as exc:
        _cli(tmp_path, config, output, option, value)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    if option == "--format":
        assert err.startswith("usage: ibkr-fa fidelity-cma-fa ")
        assert err.endswith(
            "ibkr-fa fidelity-cma-fa: error: argument --format: invalid value; "
            "run 'ibkr-fa fidelity-cma-fa --help'\n"
        )
    else:
        assert err == (
            "usage: ibkr-fa [-h] COMMAND ...\n"
            "ibkr-fa: error: unrecognized or ambiguous arguments; run 'ibkr-fa --help'\n"
        )
    assert [path.name for path in tmp_path.iterdir()] == ["user.toml"]


def test_cli_filing_csv_output_without_a_file_name_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, _output = _prepare(tmp_path, monkeypatch)
    assert _cli(tmp_path, config, Path("/"), *_FILING_CSV) == 4
    assert capsys.readouterr().err == (
        "error: fidelity-cma-fa requires --output to name a file for the A2 companion output\n"
    )
    assert [path.name for path in tmp_path.iterdir()] == ["user.toml"]


@pytest.mark.parametrize("bound", ["lower", "upper"])
def test_inr_peak_currency_files_the_attested_bound_of_the_inverting_statement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound: str,
) -> None:
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        extra_config=(
            f'\nsuppress_peak_balance_warning = true\npeak_balance_bound = "{bound}"\n'
            '\n[schedule_fa]\npeak_currency = "inr"\n'
        ),
        statement=inverting_statement(debit_first=False),
        year_end=_INVERTING_YEAR_END,
    )
    with pytest.warns(UserWarning, match="peak override active"):
        report = fa_mod.run_fidelity_cma_fa(
            input_dir=tmp_path,
            config_path=config,
            output_path=output,
            calendar_year=2025,
        )
    bounds = report.peak_bounds
    assert (bounds.lower_date, bounds.lower_inr) == (date(2025, 12, 16), Decimal("534540.0"))
    assert (bounds.upper_date, bounds.upper_inr) == (date(2025, 12, 16), Decimal("534540.0"))
    assert report.filing_complete
    row = _a2_row(output)
    assert (row[7], row[21]) == ("534540", bound)


def _marker_year_end(tax_year: int) -> CmaYearEndIncomeSummary:
    """The year-end report matching ``marker_statement(tax_year)``."""
    return replace(
        _year_end(),
        tax_year=tax_year,
        account_id=ACCOUNT,
        account_holder=HOLDER,
        position=CmaYearEndPosition(
            quantity=Decimal(10),
            price_per_unit_usd=Decimal(NAV),
            market_value_usd=Decimal(10) * Decimal(NAV),
            income_earned_usd=Decimal(0),
        ),
    )


def test_a_calendar_year_the_market_calendar_lacks_refuses_before_loading_navs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CY2024 bundle that passes the statement checks refuses before the
    SPAXX NAVs are loaded (#1135)."""
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        statement=marker_statement(2024),
        year_end=_marker_year_end(2024),
    )
    monkeypatch.setattr(
        fa_mod, "load_price_series", lambda *_args, **_kwargs: pytest.fail("NAVs loaded")
    )
    with pytest.raises(IbkrFaError) as excinfo:
        fa_mod.run_fidelity_cma_fa(
            input_dir=tmp_path,
            config_path=config,
            output_path=output,
            calendar_year=2024,
        )
    message = str(excinfo.value)
    assert message == A2_PRICING_2024_REFUSAL
    assert_no_marker(message, year=2024)


def test_cli_a_session_without_a_nav_exits_4_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The fixture series without 2025-07-10 refuses; it never carries the
    2025-07-09 NAV (#1135)."""
    config, output = _prepare(
        tmp_path, monkeypatch, statement=marker_statement(), year_end=_marker_year_end(2025)
    )
    series = _series(datetime(2026, 1, 1, tzinfo=UTC))
    gap = replace(
        series,
        closes={day: Decimal(NAV) for day in series.closes if day != date(2025, 7, 10)},
    )
    monkeypatch.setattr(fa_mod, "load_price_series", lambda *_args, **_kwargs: {gap.ticker: gap})
    a2_output = output.with_name("schedule-fa-a2.csv")
    for stale in (output, a2_output):
        stale.write_bytes(b"STALE")
    assert _cli(tmp_path, config, output) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {NO_SESSION_CLOSE}\n"
    assert_no_marker(captured.err)
    assert not output.exists()
    assert not a2_output.exists()


def test_cli_a_nasdaq_nav_not_in_its_price_format_exits_4_without_output_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real loader and Nasdaq reader, with an empty price cache, refuse a
    NAV of "1,2" on 2025-07-10; the former reader read that close as 12
    (#1138)."""
    config, output = _prepare(
        tmp_path, monkeypatch, statement=marker_statement(), year_end=_marker_year_end(2025)
    )
    monkeypatch.setattr(fa_mod, "load_price_series", load_price_series)
    sessions = us_market_sessions(date(2024, 12, 31), date(2025, 12, 31))
    rows = [
        {"date": f"{day:%m/%d/%Y}", "close": "1,2" if day == date(2025, 7, 10) else f"${NAV}"}
        for day in sessions
    ]
    payload = json.dumps(
        {"data": {"symbol": "SPAXX", "tradesTable": {"rows": rows}}, "status": {"rCode": 200}}
    ).encode()
    monkeypatch.setattr(
        fa_mod,
        "fetch_nasdaq_mutual_fund_daily_closes_usd",
        partial(fetch_nasdaq_mutual_fund_daily_closes_usd, url_opener=lambda *_: payload),
    )
    a2_output = output.with_name("schedule-fa-a2.csv")
    for stale in (output, a2_output):
        stale.write_bytes(b"STALE")
    assert _cli(tmp_path, config, output) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"error: Nasdaq price response, tradesTable row {sessions.index(date(2025, 7, 10)) + 1}, "
        "column close: the value isn't a price in Nasdaq's format ($, digits with comma "
        "thousands, optional decimals, above zero)\n"
    )
    assert not output.exists()
    assert not a2_output.exists()
    assert not any(path.is_file() for path in (tmp_path / ".ibkr-fa-price-cache").rglob("*"))


# ---- exact SPAXX shares (#1244) ---------------------------------------------

_MARKER = "736253"
_A2_FCB_REFUSAL = f"InvariantError: Fidelity CMA A2 peak, free credit balance USD: {INEXACT_SUM}"


def _split_buy_statement(
    *buys: tuple[str, str], redeemed: str = "1.005", proceeds: str = "1.01"
) -> CmaStatement:
    """The base statement with ``buys`` (quantity, amount) made on 2025-01-01
    and ``redeemed`` units sold for USD ``proceeds`` on 2025-04-04; the rest
    of the cash stays as free credit balance."""
    base = _statement()
    spent = sum((Decimal(amount) for _quantity, amount in buys), Decimal(0))
    held = sum((Decimal(quantity) for quantity, _amount in buys), Decimal(0)) - Decimal(redeemed)
    cash = Decimal(12) - spent + Decimal(proceeds)
    core: list[CmaCoreTransaction] = []
    balance = Decimal(0)
    for quantity, amount in buys:
        balance += Decimal(amount)
        core.append(
            CmaCoreTransaction(
                date(2025, 1, 1),
                CmaCoreTransactionKind.BUY,
                Decimal(quantity),
                Decimal(1),
                Decimal(amount),
                balance,
            )
        )
    core.append(
        CmaCoreTransaction(
            date(2025, 4, 4),
            CmaCoreTransactionKind.SELL,
            -Decimal(redeemed),
            Decimal(1),
            -Decimal(proceeds),
            balance - Decimal(proceeds),
        )
    )
    return replace(
        base,
        core_transactions=tuple(core),
        free_credit_balance_usd=cash,
        ending_account_value_usd=cash + held,
        account_value_usd=cash + held,
        position=replace(base.position, quantity=held, ending_market_value_usd=held),
    )


def test_a3_initial_value_of_two_thirds_of_a_purchase_is_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3.015 units bought for USD 3.02 at the 2025-01-01 TTBR of 85.22, 1.005
    of them redeemed: the open 2.010 units cost two thirds of USD 3.02,
    filing ₹172, and the redeemed third files ₹86."""
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        statement=_split_buy_statement(("3.015", "3.02")),
        year_end=_year_end(quantity="2.010", market_value="2.010"),
    )
    fa_mod.run_fidelity_cma_fa(
        input_dir=tmp_path,
        config_path=config,
        output_path=output,
        calendar_year=2025,
    )

    assert [
        (row["Initial value of the investment"], row["_audit: Cost basis (USD)"])
        for row in _a3_rows(output)
    ] == [("172", "2.013333333333333333333333333"), ("86", "1.006666666666666666666666666")]


def test_cli_open_share_past_the_digit_cap_exits_4_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The first purchase is redeemed whole; the second, still open, cost an
    amount past the digit cap. Main refused at its share, naming its row; since
    #1267 the exact A2 free credit balance can't hold the amount within the cap,
    so A2 refuses first with its fixed text and no row. fidelity-cma-cg still
    names the row (test_pipeline_fidelity_cma_cg)."""
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        statement=_split_buy_statement(
            ("1.005", "1.01"), ("2.010", "2.01" + "0" * 1000 + "9" + _MARKER)
        ),
        year_end=_year_end(quantity="2.010", market_value="2.010"),
    )
    output.write_text("stale", encoding="utf-8")
    output.with_name("schedule-fa-a2.csv").write_text("stale", encoding="utf-8")

    with pytest.raises(IbkrFaError) as excinfo:
        fa_mod.run_fidelity_cma_fa(
            input_dir=tmp_path, config_path=config, output_path=output, calendar_year=2025
        )
    expected = InvariantError(what=_A2_FCB_REFUSAL.removeprefix("InvariantError: "), context=())
    assert_unchained_refusal(excinfo.value, expected, _A2_FCB_REFUSAL, markers=(_MARKER, _ACCOUNT))
    assert _cli(tmp_path, config, output) == 4

    err = capsys.readouterr().err
    assert err == f"error: {_A2_FCB_REFUSAL}\n"
    assert _MARKER not in err
    assert _ACCOUNT not in err
    assert not output.exists()
    assert not output.with_name("schedule-fa-a2.csv").exists()


# ---- #1533: a NAV only where SPAXX is held or traded; NAV recovery texts -----

_NAV_NOW = datetime(2026, 6, 1, tzinfo=UTC)
_NAV_FETCH_FAILED = (
    "SPAXX daily NAV could not be fetched or loaded: the Nasdaq price source was unreachable "
    "or returned no usable closes. Re-run to fetch it again. If a re-run still can't get a "
    "usable series, enter the Fidelity CMA Schedule FA by hand."
)


def _spaxx_nav_without(*missing: date) -> DailyCloseFetcher:
    """``spaxx_nav`` without its closes dated ``missing``."""

    def fetch(
        ticker: Ticker,
        *,
        start: date,
        end: date,
        now: datetime,
        fetch_symbol: str | None = None,
    ) -> PriceSeries:
        series = spaxx_nav(ticker, start=start, end=end, now=now, fetch_symbol=fetch_symbol)
        closes = {day: close for day, close in series.closes.items() if day not in missing}
        assert len(closes) == len(series.closes) - len(missing)
        return replace(series, closes=closes)

    return fetch


def _run_bundle(tmp_path: Path, request: pytest.FixtureRequest, fetcher: DailyCloseFetcher) -> Path:
    """Run the CMA bundle's FA review CSVs into ``tmp_path/out`` with the SPAXX
    NAV cache in ``tmp_path/price-cache``; return the output folder."""
    statements, _tax_forms, config = request.getfixturevalue("cma_inputs")
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    fa_mod.run_fidelity_cma_fa(
        input_dir=statements,
        config_path=config,
        output_path=out / "schedule-fa.csv",
        calendar_year=2025,
        price_cache_dir=tmp_path / "price-cache",
        price_fetcher=fetcher,
        price_now=_NAV_NOW,
    )
    return out


def test_a_bundle_without_navs_before_its_first_purchase_writes_the_full_series_bytes(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """The bundle's first SPAXX purchase is on Sunday 2025-01-05, so January 1
    to 4 hold none. Their NAVs, the 2024-12-31 and 2025-01-02 closes, are
    missing; the purchase's, 2025-01-03, isn't. The run writes the full
    series' bytes (#1533); main refused."""
    out = _run_bundle(tmp_path, request, _spaxx_nav_without(date(2024, 12, 31), date(2025, 1, 2)))
    assert folder_digests(out) == FA_SHA256


@pytest.mark.parametrize("locked", [False, True], ids=["cache", "locked-cache"])
def test_moving_a_short_nav_cache_aside_as_the_refusal_says_gets_past_it(
    tmp_path: Path, request: pytest.FixtureRequest, locked: bool
) -> None:
    """#1533: the source first returns no close for 2025-07-10, a session on
    which the bundle holds SPAXX. That run caches the short series, locked or not, and
    refuses. A same-day re-run reuses it and refuses again, though the source
    now returns every close. Moving SPAXX.csv, and its lock, out of the
    nasdaq-mutual-funds folder as the refusal says lets the next run fetch the
    full series and write its bytes."""
    with pytest.raises(IbkrFaError) as short:
        _run_bundle(tmp_path, request, _spaxx_nav_without(date(2025, 7, 10)))
    folder = tmp_path / "price-cache" / "nasdaq-mutual-funds"
    if locked:
        write_cache_lock(folder / "SPAXX.csv")
    with pytest.raises(IbkrFaError) as reused:
        _run_bundle(tmp_path, request, spaxx_nav)
    assert str(short.value) == str(reused.value) == NO_SESSION_CLOSE
    moved = ["SPAXX.csv", "SPAXX.csv.lock"] if locked else ["SPAXX.csv"]
    assert sorted(path.name for path in folder.iterdir()) == moved
    aside = tmp_path / "aside"
    aside.mkdir()
    for name in moved:
        (folder / name).rename(aside / name)
    out = _run_bundle(tmp_path, request, spaxx_nav)
    assert folder_digests(out) == FA_SHA256


def test_a_nav_fetch_failure_clears_once_the_source_is_reachable(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """#1533: with the Nasdaq source unreachable and no cache, the run refuses
    with its recovery; re-run once the source is reachable, it writes the
    full series' bytes."""
    unreachable = partial(fetch_nasdaq_mutual_fund_daily_closes_usd, url_opener=lambda *_: None)
    with pytest.raises(IbkrFaError) as excinfo:
        _run_bundle(tmp_path, request, unreachable)
    assert type(excinfo.value) is fa_mod.FidelityCmaFaError
    assert str(excinfo.value) == _NAV_FETCH_FAILED
    out = _run_bundle(tmp_path, request, spaxx_nav)
    assert folder_digests(out) == FA_SHA256


_NAV_NO_ROWS = json.dumps(
    {"data": {"symbol": "SPAXX", "tradesTable": {"rows": []}}, "status": {"rCode": 200}}
).encode()


def _nav_reply(url: str, *, status: int = 200, symbol: str = "SPAXX") -> bytes:
    """A Nasdaq reply to ``url`` with a $1.0000 close for every US market
    session from its ``fromdate`` to its ``todate``, the closes
    ``spaxx_nav`` gives; ``status`` and ``symbol`` set its status code
    and symbol."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, strict_parsing=True)
    (start,), (end,) = query["fromdate"], query["todate"]
    sessions = us_market_sessions(date.fromisoformat(start), date.fromisoformat(end))
    rows = [{"date": f"{day:%m/%d/%Y}", "close": "$1.0000"} for day in sessions]
    assert rows
    return json.dumps(
        {"data": {"symbol": symbol, "tradesTable": {"rows": rows}}, "status": {"rCode": status}}
    ).encode()


def test_the_nav_reply_the_refusal_cases_edit_writes_the_full_series_bytes(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """``_nav_reply`` with status 200 and the SPAXX symbol, the reply the
    error-status and other-symbol cases below edit, holds a usable close for
    every session the run asks for: the run writes the full series' bytes. So
    each of those cases refuses only for the status or the symbol it changes."""
    fetcher = partial(
        fetch_nasdaq_mutual_fund_daily_closes_usd,
        url_opener=lambda url, _timeout: _nav_reply(url),
    )
    out = _run_bundle(tmp_path, request, fetcher)
    assert folder_digests(out) == FA_SHA256


@pytest.mark.parametrize(
    "reply",
    [
        lambda _url: _NAV_NO_ROWS,
        partial(_nav_reply, status=400),
        partial(_nav_reply, symbol="XQCN"),
        lambda _url: b"<html></html>",
    ],
    ids=["no-rows", "error-status", "other-symbol", "not-json"],
)
def test_a_nav_reply_without_usable_closes_refuses_again_so_the_by_hand_route_applies(
    tmp_path: Path, request: pytest.FixtureRequest, reply: Callable[[str], bytes]
) -> None:
    """#1536: the Nasdaq source answers, but its reply holds no usable SPAXX
    close: a well-formed reply with no rows, an error status or another
    symbol's reply on closes the run would otherwise accept, or a reply that
    isn't JSON. The run refuses with the fetch-failure text and caches
    nothing. A re-run fetches again, gets the same reply and refuses with the
    same text, so entering the Schedule FA by hand is the route that applies."""
    urls: list[str] = []

    def same_reply(url: str, _timeout: float) -> bytes:
        urls.append(url)
        return reply(url)

    fetcher = partial(fetch_nasdaq_mutual_fund_daily_closes_usd, url_opener=same_reply)
    refusals = []
    for _run in range(2):
        with pytest.raises(IbkrFaError) as excinfo:
            _run_bundle(tmp_path, request, fetcher)
        assert type(excinfo.value) is fa_mod.FidelityCmaFaError
        refusals.append(str(excinfo.value))
        assert not any(path.is_file() for path in (tmp_path / "price-cache").rglob("*"))
    assert refusals == [_NAV_FETCH_FAILED, _NAV_FETCH_FAILED]
    assert len(urls) == 2


_LOWER_INR = "_audit: Peak lower bound INR (converted at its own date; full precision)"
_UPPER_INR = "_audit: Peak upper bound INR (converted at its own date; full precision)"


def _review_a2_cells(output: Path) -> dict[str, str]:
    """The A2 review CSV's one row, keyed by its header."""
    with output.with_name("schedule-fa-a2.csv").open(encoding="utf-8-sig", newline="") as handle:
        header, row = csv.reader(handle)
    return dict(zip(header, row, strict=True))


def test_a_no_spaxx_day_without_its_nav_keeps_the_values_but_not_their_trailing_zeros(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1533: the statement's $10 opening deposit buys 10 units on Friday
    2025-01-10, so 2025-01-01 sets both A2 bounds with the deposit as cash.
    With $1.0000 NAVs, its value is $10 + 0 x $1.0000. Without the closes
    before 2025-01-10 it is $10. Every A2 and A3 cell, the filed upper bound
    included, is the same, except the full-precision audit bounds, which keep
    their values and lose their trailing zeros."""
    base = _statement()
    (buy,) = base.core_transactions
    opening, *_mixed_day = base.cash_activity
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        extra_config=_UPPER_BOUND,
        statement=replace(
            base,
            additions_usd=Decimal(10),
            subtractions_usd=Decimal(0),
            ending_account_value_usd=Decimal(10),
            account_value_usd=Decimal(10),
            free_credit_balance_usd=Decimal(0),
            core_transactions=(replace(buy, date=date(2025, 1, 10)),),
            cash_activity=(opening,),
        ),
    )
    series = _series(_NAV_NOW)
    full = replace(series, closes=dict.fromkeys(series.closes, Decimal("1.0000")))
    gap = replace(
        full, closes={day: nav for day, nav in full.closes.items() if day >= date(2025, 1, 10)}
    )
    runs = []
    for nav_series in (full, gap):
        monkeypatch.setattr(
            fa_mod, "load_price_series", lambda *_args, s=nav_series, **_kwargs: {s.ticker: s}
        )
        fa_mod.run_fidelity_cma_fa(
            input_dir=tmp_path, config_path=config, output_path=output, calendar_year=2025
        )
        runs.append((output.read_bytes(), _review_a2_cells(output)))
    (full_a3, full_a2), (gap_a3, gap_a2) = runs
    assert gap_a3 == full_a3
    bounds = (_LOWER_INR, _UPPER_INR)
    assert {k: v for k, v in gap_a2.items() if k not in bounds} == {
        k: v for k, v in full_a2.items() if k not in bounds
    }
    assert full_a2["_audit: Peak lower bound date"] == "2025-01-01"
    assert full_a2["_audit: Peak upper bound date"] == "2025-01-01"
    # $10.0000 and $10 at the 2025-01-01 TTBR: one value, two spellings.
    assert [(full_a2[column], gap_a2[column]) for column in bounds] == [
        ("852.200000", "852.20"),
        ("852.200000", "852.20"),
    ]


def test_the_stale_nav_refusal_cannot_fire_from_fidelity_cma_fa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1533: the run leaves the peak bounds' NAV staleness limit at its
    default, and in every calendar year the run accepts and the market
    calendar supports, no day is more days after its US market session than
    that. So "SPAXX daily NAV coverage is incomplete" can't fire from
    fidelity-cma-fa."""
    config, output = _prepare(tmp_path, monkeypatch)
    real = fa_mod.compute_fidelity_cma_peak_bounds
    calls: list[dict[str, Any]] = []

    def spy(*args: Any, **kwargs: Any):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(fa_mod, "compute_fidelity_cma_peak_bounds", spy)
    fa_mod.run_fidelity_cma_fa(
        input_dir=tmp_path, config_path=config, output_path=output, calendar_year=2025
    )
    (kwargs,) = calls
    assert "max_price_staleness_days" not in kwargs
    default = inspect.signature(real).parameters["max_price_staleness_days"].default
    assert default == DEFAULT_MAX_PRICE_STALENESS_DAYS
    years = [
        year
        for year in range(fa_mod._MIN_CALENDAR_YEAR, fa_mod._MAX_CALENDAR_YEAR + 1)
        if supports_calendar_year(year)
    ]
    assert years
    for year in years:
        day = date(year, 1, 1)
        while day.year == year:
            assert day - us_market_session_on_or_before(day) <= timedelta(days=default)
            day += timedelta(days=1)


# ---- #1552: blank-peak reasons and rows, accrued-interest and non-session NAV routes ----


def test_cli_a_peak_day_without_a_ttbr_names_its_row_cause_and_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1552: with ``[fx] max_carry_forward_days = 0``, the open lot's native
    peak is the $1.01 NAV on Friday 2025-08-15, a US market session with no
    SBI rate, so its A3 peak is blank for NO_TTBR. A $5 deposit on 2025-12-10
    sets both A2 bounds on a day that has a rate, so Table A2 doesn't refuse."""
    base = _statement()
    statement = replace(
        base,
        additions_usd=Decimal("20"),
        ending_account_value_usd=Decimal("17"),
        account_value_usd=Decimal("17"),
        free_credit_balance_usd=Decimal("7"),
        cash_activity=(
            *base.cash_activity,
            CmaCashActivity(
                date(2025, 12, 10), CmaCashActivityKind.DEPOSIT, "late deposit", Decimal("5")
            ),
        ),
    )
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        extra_config="\n[fx]\nmax_carry_forward_days = 0\n",
        statement=statement,
        navs={date(2025, 8, 15): Decimal("1.01")},
    )
    assert _cli(tmp_path, config, output) == 3
    assert (
        f"warning: manual entry required for {_A3_PEAK_MANUAL} in row(s) 1: the peak is blank "
        "because no SBI TT buying rate can be sourced for a day the peak needs. Enter it by "
        "hand; a ticker override or a re-fetch won't help."
    ) in capsys.readouterr().err.splitlines()
    assert [row[_A3_PEAK] for row in _a3_rows(output)] == [""]


def test_cli_a_manual_nav_cache_with_a_split_names_its_row_cause_and_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1552: the Nasdaq reader never records a split, but a manual SPAXX.csv
    can, and a contemporaneous one covering the year is reused before any
    fetch. A split on 2025-06-02 falls inside the open lot's window, so its A3
    peak is blank for SPLIT; Table A2 reads only the split-safe closes."""
    config, output = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(fa_mod, "load_price_series", load_price_series)
    monkeypatch.setattr(
        fa_mod,
        "fetch_nasdaq_mutual_fund_daily_closes_usd",
        lambda *_args, **_kwargs: pytest.fail("the manual cache covers the year"),
    )
    folder = tmp_path / ".ibkr-fa-price-cache" / "nasdaq-mutual-funds"
    folder.mkdir(parents=True)
    write_cache(
        folder / "SPAXX.csv",
        replace(
            _series(datetime(2026, 1, 1, tzinfo=UTC)),
            has_split_in_range=True,
            split_ratios={date(2025, 6, 2): Decimal(2)},
            price_basis=CONTEMPORANEOUS_BASIS,
        ),
        covered_start=date(2024, 12, 1),
        covered_end=date(2025, 12, 31),
        fetch_symbol="SPAXX",
    )
    assert _cli(tmp_path, config, output) == 3
    assert (
        f"warning: manual entry required for {_A3_PEAK_MANUAL} in row(s) 1: the peak is blank "
        "because a stock split falls inside the holding window, or the price series isn't "
        "adjusted to per-date prices, so one lot quantity can't be valued across it. Enter it "
        "by hand; a ticker override or a re-fetch won't help."
    ) in capsys.readouterr().err.splitlines()
    assert [row[_A3_PEAK] for row in _a3_rows(output)] == [""]


def test_cli_accrued_interest_names_the_statement_and_the_by_hand_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1552: June accrues $1 of interest and July's statement credits it as
    cash; December's evidence reconciles. The run refuses, naming statement 1
    and the by-hand route, and leaves no output."""
    config, output = _prepare(
        tmp_path,
        monkeypatch,
        statement=accrued_interest_statements(),
        year_end=replace(_year_end(), interest_usd=Decimal("1")),
    )
    a2_output = output.with_name("schedule-fa-a2.csv")
    for stale in (output, a2_output):
        stale.write_bytes(b"STALE")
    assert _cli(tmp_path, config, output) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {ACCRUED_INTEREST_REFUSAL}\n"
    assert not output.exists()
    assert not a2_output.exists()


def _spaxx_nav_with_saturday(
    ticker: Ticker, *, start: date, end: date, now: datetime, fetch_symbol: str | None = None
) -> PriceSeries:
    """``spaxx_nav`` plus a Saturday 2025-07-12 close at Friday's NAV."""
    series = spaxx_nav(ticker, start=start, end=end, now=now, fetch_symbol=fetch_symbol)
    friday = series.closes[date(2025, 7, 11)]
    return replace(series, closes={**series.closes, date(2025, 7, 12): friday})


@pytest.mark.parametrize("locked", [False, True], ids=["cache", "locked-cache"])
def test_moving_a_nav_cache_with_a_saturday_close_aside_as_the_refusal_says(
    tmp_path: Path, request: pytest.FixtureRequest, locked: bool
) -> None:
    """#1552: the source first returns a Saturday close. That run caches the
    series, locked or not, and refuses with the move-aside route; a same-day
    re-run reuses the cache and refuses again, though the source now returns
    no Saturday close. After moving SPAXX.csv, and its lock, out of the
    nasdaq-mutual-funds folder as the refusal says: a source that still
    returns the Saturday close refuses with the same text, so the by-hand
    route applies; one that doesn't lets the run write the bundle's bytes."""
    folder = tmp_path / "price-cache" / "nasdaq-mutual-funds"
    aside = tmp_path / "aside"
    aside.mkdir()

    def move_aside(attempt: int) -> None:
        moved = ["SPAXX.csv", "SPAXX.csv.lock"] if locked else ["SPAXX.csv"]
        assert sorted(path.name for path in folder.iterdir()) == moved
        (aside / str(attempt)).mkdir()
        for name in moved:
            (folder / name).rename(aside / str(attempt) / name)

    refusals = []
    with pytest.raises(IbkrFaError) as excinfo:
        _run_bundle(tmp_path, request, _spaxx_nav_with_saturday)
    refusals.append(str(excinfo.value))
    if locked:
        write_cache_lock(folder / "SPAXX.csv")
    with pytest.raises(IbkrFaError) as excinfo:
        _run_bundle(tmp_path, request, spaxx_nav)
    refusals.append(str(excinfo.value))
    move_aside(1)
    with pytest.raises(IbkrFaError) as excinfo:
        _run_bundle(tmp_path, request, _spaxx_nav_with_saturday)
    refusals.append(str(excinfo.value))
    assert refusals == [NON_SESSION_CLOSE] * 3
    if locked:
        write_cache_lock(folder / "SPAXX.csv")
    move_aside(2)
    out = _run_bundle(tmp_path, request, spaxx_nav)
    assert folder_digests(out) == FA_SHA256
