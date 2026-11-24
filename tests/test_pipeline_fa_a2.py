"""Integration tests for the Schedule FA Table A2 wiring
(:func:`ibkr_fa.pipeline_fa_a2.build_and_write_fa_a2`).

These build every input synthetically: a NAV summary parsed from a temp CSV
carrying just the ``Net Asset Value`` / ``Change in NAV`` sections, plus
synthetic account / config / lot inputs.
"""

from __future__ import annotations

import csv as csvmod
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.compute.fa_a2 import FaA2AttestedPeak, fa_a2_manual_fill_gaps
from ibkr_fa.compute.lot_overlap import CyOverlap
from ibkr_fa.config import load_user_config
from ibkr_fa.exceptions import ConfigError, InvariantError, MissingFxRateError, ParseError
from ibkr_fa.fx.policy_fa import FaExactDatePolicy
from ibkr_fa.models import BasisProvenance, ClosedLot, Dividend, DividendEvidence
from ibkr_fa.models.account_identity import TaxpayerStatus
from ibkr_fa.models.account_info import AccountInfo
from ibkr_fa.models.fa_a2_row import FaA2ClosingBlankReason, FaA2NatureOfAmount
from ibkr_fa.models.interest_credit import InterestCredit, InterestNonCredit
from ibkr_fa.models.net_asset_value import NavRefusalReason, NetAssetValueSummary
from ibkr_fa.output.fa_manual_fill import fa_a2_manual_fill_columns
from ibkr_fa.parser.activity_statement import (
    KnownActivitySection,
    split_activity_statement_sections,
)
from ibkr_fa.parser.activity_statement.net_asset_value import parse_net_asset_value
from ibkr_fa.parser.format_detect import IbkrCsvFormat
from ibkr_fa.pipeline_fa_a2 import build_and_write_fa_a2, build_fa_a2
from ibkr_fa.types import AccountId, CountryCode, Isin, Ticker
from tests._crash_reports import assert_no_markers
from tests._ibkr_user_toml import CUSTODIAN_TOML

_NAV_CSV = (
    "Net Asset Value,Header,Asset Class,Prior Total,Current Long,"
    "Current Short,Current Total,Change\n"
    "Net Asset Value,Data,Cash ,0,42.518346,0,42.518346,42.518346\n"
    "Net Asset Value,Data,Stock,0,81.20,0,81.20,81.20\n"
    "Net Asset Value,Data,Total,0,123.718346,0,123.718346,123.718346\n"
    "Change in NAV,Header,Field Name,Field Value\n"
    "Change in NAV,Data,Ending Value,123.718346\n"
)

_CONFIG_WITH_CUSTODIAN = (
    """\
[account]
number = "U12345678"
holder_name = "Tax Payer"
opening_date = 2024-01-01
status = "BeneficialOwner"
allow_masked_account = true"""
    + CUSTODIAN_TOML
)

_CONFIG_NO_CUSTODIAN = """\
[account]
number = "U12345678"
holder_name = "Tax Payer"
opening_date = 2024-01-01
status = "BeneficialOwner"
allow_masked_account = true
"""


def _account_info(base_currency: str = "USD") -> AccountInfo:
    return AccountInfo(
        account_id=AccountId("U**********"),  # IBKR's masked form
        account_holder_name="TAX PAYER",
        base_currency=base_currency,
    )


def _config(tmp_path: Path, text: str):
    p = tmp_path / "user.toml"
    p.write_text(text, encoding="utf-8")
    return load_user_config(p)


def _nav(tmp_path: Path, text: str = _NAV_CSV) -> NetAssetValueSummary:
    p = tmp_path / "stmt.csv"
    p.write_text(text, encoding="utf-8")
    sections = split_activity_statement_sections(p)
    return parse_net_asset_value(
        sections.get(KnownActivitySection.NET_ASSET_VALUE),
        sections.get(KnownActivitySection.CHANGE_IN_NAV),
    )


def _call(
    tmp_path: Path,
    *,
    config_text: str,
    base_currency: str,
    input_format=None,
    nav: NetAssetValueSummary | None = None,
    dividend_evidence: DividendEvidence = DividendEvidence.ABSENT,
    calendar_year: int = 2025,
    overlap: CyOverlap | None = None,
    dividends: list[Dividend] | None = None,
    interest_credits: list[InterestCredit] | None = None,
    interest_non_credits: list[InterestNonCredit] | None = None,
    status: TaxpayerStatus | None = None,
):
    config = _config(tmp_path, config_text)
    if status is not None:  # a status config loading would refuse, set in code
        config = replace(config, account=replace(config.account, status=status))
    return build_and_write_fa_a2(
        config=config,
        account_info=_account_info(base_currency),
        nav=nav or _nav(tmp_path),
        dividend_evidence=dividend_evidence,
        input_format=input_format or IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy=FaExactDatePolicy(max_carry_forward_days=14),
        calendar_year=calendar_year,
        overlap=overlap or CyOverlap(open_lots=[], closed_lots=[]),
        dividends=dividends or [],
        interest_credits=interest_credits or [],
        interest_non_credits=interest_non_credits or [],
        output_path=tmp_path / "schedule-fa.csv",
        format_id="review-csv",
    )


def test_happy_path_emits_a2_with_computed_closing(tmp_path: Path) -> None:
    rows, out_path, manual_fill = _call(
        tmp_path, config_text=_CONFIG_WITH_CUSTODIAN, base_currency="USD"
    )
    [row] = rows
    assert out_path == tmp_path / "schedule-fa-a2.csv"
    assert out_path.exists()
    # closing = broker NAV 123.718346 x Dec-31 TTBR; uses the unmasked config number.
    assert row.account_number == "U12345678"
    assert row.audit_account_number_from_config is True  # statement was masked
    assert row.closing_balance_inr is not None
    assert row.closing_balance_inr > 0
    assert row.audit_nav_cross_checked is True
    # no dividends/interest/proceeds → "No Amount paid/credited"; peak manual-fill.
    assert row.nature_of_amount is FaA2NatureOfAmount.NO_AMOUNT
    assert row.peak_balance_inr is None
    assert manual_fill == ("Peak Balance During the Period",)


_CASH_ROW = "Net Asset Value,Data,Cash ,0,42.518346,0,42.518346,42.518346\n"


def test_period_end_cash_reference_is_the_nav_cash_row(tmp_path: Path) -> None:
    rows, _out_path, _manual_fill = _call(
        tmp_path, config_text=_CONFIG_WITH_CUSTODIAN, base_currency="USD"
    )
    assert [row.audit_period_end_cash_base_ccy for row in rows] == [Decimal("42.518346")]


def test_no_cash_row_leaves_the_period_end_cash_reference_unset(tmp_path: Path) -> None:
    """An all-securities account prints no Cash row (#1065: unchanged)."""
    nav = _nav(tmp_path, _NAV_CSV.replace(_CASH_ROW, "").replace("123.718346", "81.20"))
    assert nav.closing_total == Decimal("81.20")
    rows, _out_path, _manual_fill = _call(
        tmp_path, config_text=_CONFIG_WITH_CUSTODIAN, base_currency="USD", nav=nav
    )
    assert [row.audit_period_end_cash_base_ccy for row in rows] == [None]


@pytest.mark.parametrize(
    ("cash", "marker", "message"),
    [
        ("", "Synthetic Holder November", "must not be blank"),
        ("Synthetic Holder Oscar", "Synthetic Holder Oscar", "must be a decimal number"),
    ],
    ids=["blank", "non-numeric"],
)
def test_an_unreadable_cash_row_refuses_rather_than_dropping_the_reference(
    tmp_path: Path, cash: str, marker: str, message: str
) -> None:
    """#1065: the breakdown A2's Cash lookup reads used to skip this row."""
    text = _NAV_CSV.replace(_CASH_ROW, f"Net Asset Value,Data,Cash ,0,{marker},0,{cash},0\n")
    with pytest.raises(ParseError) as exc:
        _nav(tmp_path, text)
    assert (exc.value.section, exc.value.row_index, exc.value.column) == (
        "Net Asset Value",
        0,
        "Current Total",
    )
    assert exc.value.raw_value == ""
    assert exc.value.message == message
    assert_no_markers(str(exc.value), marker)


def test_csv_path_leaves_a_configured_peak_manual_fill(tmp_path: Path) -> None:
    # Only the CBDT JSON files [[schedule_fa.manual_values]]; the CSV is unchanged.
    config_text = _CONFIG_WITH_CUSTODIAN + (
        '[schedule_fa]\nmanual_values_account_number = "U12345678"\n'
        "[[schedule_fa.manual_values]]\ncalendar_year = 2025\na2_peak_balance_inr = 250000\n"
    )
    rows, _out_path, manual_fill = _call(tmp_path, config_text=config_text, base_currency="USD")
    assert [row.peak_balance_inr for row in rows] == [None]
    assert manual_fill == ("Peak Balance During the Period",)


def test_build_fa_a2_files_a_passed_peak(tmp_path: Path) -> None:
    rows = build_fa_a2(
        config=_config(tmp_path, _CONFIG_WITH_CUSTODIAN),
        account_info=_account_info(),
        nav=_nav(tmp_path),
        dividend_evidence=DividendEvidence.ABSENT,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy=FaExactDatePolicy(max_carry_forward_days=14),
        calendar_year=2025,
        overlap=CyOverlap(open_lots=[], closed_lots=[]),
        dividends=[],
        interest_credits=[],
        interest_non_credits=[],
        peak=FaA2AttestedPeak(peak_inr=Decimal("250000"), source="config", reason="supplied"),
    )
    assert [row.peak_balance_inr for row in rows] == [Decimal("250000")]
    assert fa_a2_manual_fill_columns(fa_a2_manual_fill_gaps(rows)) == ()


def test_custodian_without_a_nav_summary_is_refused(tmp_path: Path) -> None:
    # The dispatcher parses NAV only when asked (parse_nav=True); a custodian needs it.
    with pytest.raises(InvariantError):
        build_fa_a2(
            config=_config(tmp_path, _CONFIG_WITH_CUSTODIAN),
            account_info=_account_info(),
            nav=None,
            dividend_evidence=DividendEvidence.ABSENT,
            input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
            fx_policy=_FX,
            calendar_year=2025,
            overlap=_EMPTY_OVERLAP,
            dividends=[],
            interest_credits=[],
            interest_non_credits=[],
        )


def test_no_custodian_is_noop(tmp_path: Path) -> None:
    rows, out_path, manual_fill = _call(
        tmp_path, config_text=_CONFIG_NO_CUSTODIAN, base_currency="USD"
    )
    assert rows == ()
    assert out_path is None
    assert manual_fill == ()
    assert not (tmp_path / "schedule-fa-a2.csv").exists()


def test_signing_authority_with_custodian_raises_config_error(tmp_path: Path) -> None:
    # Config loading refuses SigningAuthority first (#887); A2 refuses it again
    # with the same message.
    with pytest.raises(ConfigError) as excinfo:
        _call(
            tmp_path,
            config_text=_CONFIG_WITH_CUSTODIAN,
            base_currency="USD",
            status=TaxpayerStatus.SIGNING_AUTHORITY,
        )
    assert excinfo.value.field == "account.status"
    assert excinfo.value.message == (
        "signing-authority accounts aren't supported; the supported statuses are "
        "BeneficialOwner and Joint"
    )
    assert not (tmp_path / "schedule-fa-a2.csv").exists()


def test_flex_query_leaves_closing_manual_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_lookup(self: FaExactDatePolicy, event_date: date) -> Decimal:
        raise AssertionError(f"unexpected FX lookup for {event_date}")

    monkeypatch.setattr(FaExactDatePolicy, "rate_for", _no_lookup)
    rows, _out, manual_fill = _call(
        tmp_path,
        config_text=_CONFIG_WITH_CUSTODIAN,
        base_currency="USD",
        input_format=IbkrCsvFormat.FLEX_QUERY,
        nav=NetAssetValueSummary(closing_total=None, refusal_reason=NavRefusalReason.FLEX_QUERY),
        dividend_evidence=DividendEvidence.NOT_DETERMINABLE,
    )
    [row] = rows
    assert row.closing_balance_inr is None  # NAV section not parsed for Flex
    assert row.closing_blank_reason is FaA2ClosingBlankReason.FLEX_QUERY
    assert "Closing balance" in manual_fill
    assert row.gross_amount_inr is None  # gross not determinable for Flex
    assert row.nature_of_amount is None
    assert manual_fill == (
        "Peak Balance During the Period",
        "Closing balance",
        "Gross amount paid/credited to the account during the period (Nature + Amount)",
    )


def test_non_usd_base_leaves_closing_manual_fill(tmp_path: Path) -> None:
    rows, _out, manual_fill = _call(
        tmp_path, config_text=_CONFIG_WITH_CUSTODIAN, base_currency="EUR"
    )
    [row] = rows
    assert row.closing_balance_inr is None  # USD-only auto-fill in v1
    assert row.closing_blank_reason is FaA2ClosingBlankReason.NON_USD_BASE
    assert "Closing balance" in manual_fill


@pytest.mark.parametrize("credit", ["dividend", "interest", "proceeds"])
def test_missing_dec31_rate_for_a_credit_raises(tmp_path: Path, credit: str) -> None:
    # No NAV section, so only the credit needs the Dec-31 rate; 2099 has none.
    nav = _nav(tmp_path, "Statement,Header,Field Name,Field Value\n")
    closed_lots = [_closed_lot(2099, gross_proceeds_money=Decimal("321"))]
    with pytest.raises(MissingFxRateError):
        _call(
            tmp_path,
            config_text=_CONFIG_WITH_CUSTODIAN,
            base_currency="USD",
            nav=nav,
            calendar_year=2099,
            dividends=[_dividend(2099)] if credit == "dividend" else None,
            interest_credits=[_interest(2099)] if credit == "interest" else None,
            overlap=(
                CyOverlap(open_lots=[], closed_lots=closed_lots) if credit == "proceeds" else None
            ),
        )
    assert not (tmp_path / "schedule-fa-a2.csv").exists()


def test_missing_dec31_rate_for_parsed_nav_raises(tmp_path: Path) -> None:
    with pytest.raises(MissingFxRateError):
        _call(
            tmp_path,
            config_text=_CONFIG_WITH_CUSTODIAN,
            base_currency="USD",
            calendar_year=2099,
        )
    assert not (tmp_path / "schedule-fa-a2.csv").exists()


def test_emitted_a2_csv_has_header_and_one_row(tmp_path: Path) -> None:
    _call(tmp_path, config_text=_CONFIG_WITH_CUSTODIAN, base_currency="USD")
    with (tmp_path / "schedule-fa-a2.csv").open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csvmod.reader(fh))
    assert len(rows) == 2  # header + one account row
    assert rows[0][0] == "Country Name and Code"
    assert rows[1][0] == "2-UNITED STATES OF AMERICA"


def test_multiple_credit_natures_emit_separate_rows(tmp_path: Path) -> None:
    rows, out_path, manual_fill = build_and_write_fa_a2(
        config=_config(tmp_path, _CONFIG_WITH_CUSTODIAN),
        account_info=_account_info(),
        nav=_nav(tmp_path),
        dividend_evidence=DividendEvidence.ABSENT,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy=_FX,
        calendar_year=2025,
        overlap=_EMPTY_OVERLAP,
        dividends=[_dividend(2025)],
        interest_credits=[_interest(2025)],
        interest_non_credits=[],
        output_path=tmp_path / "schedule-fa.csv",
        format_id="review-csv",
    )
    assert out_path is not None
    assert [(row.nature_of_amount, row.gross_amount_inr) for row in rows] == [
        (FaA2NatureOfAmount.INTEREST, Decimal("10") * _FX.rate_for(date(2025, 12, 31))),
        (FaA2NatureOfAmount.DIVIDEND, Decimal("5") * _FX.rate_for(date(2025, 12, 31))),
    ]
    assert manual_fill == ("Peak Balance During the Period",)
    with out_path.open(encoding="utf-8-sig", newline="") as handle:
        assert len(list(csvmod.reader(handle))) == 3


def test_known_credit_and_unknown_proceeds_emit_placeholder(tmp_path: Path) -> None:
    rows, _out_path, manual_fill = build_and_write_fa_a2(
        config=_config(tmp_path, _CONFIG_WITH_CUSTODIAN),
        account_info=_account_info(),
        nav=_nav(tmp_path),
        dividend_evidence=DividendEvidence.ABSENT,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy=_FX,
        calendar_year=2025,
        overlap=CyOverlap(open_lots=[], closed_lots=[_closed_lot(2025)]),
        dividends=[_dividend(2025)],
        interest_credits=[],
        interest_non_credits=[],
        output_path=tmp_path / "schedule-fa.csv",
        format_id="review-csv",
    )
    assert [(row.nature_of_amount, row.gross_amount_inr) for row in rows] == [
        (FaA2NatureOfAmount.DIVIDEND, Decimal("5") * _FX.rate_for(date(2025, 12, 31))),
        (None, None),
    ]
    assert "Gross amount paid/credited to the account during the period" in manual_fill[1]


# ---- shared fixtures ---------------------------------------------------------

_EMPTY_OVERLAP = CyOverlap(open_lots=[], closed_lots=[])
_FX = FaExactDatePolicy(max_carry_forward_days=14)


def _dividend(year: int) -> Dividend:
    return Dividend(
        symbol=Ticker("XQAW"),
        isin=Isin("US99990E1038"),
        payment_date=date(year, 6, 2),
        amount_gross=Decimal("5"),
        currency="USD",
        description="XQAW(US99990E1038) Cash Dividend USD 0.50 per Share",
    )


def _interest(year: int) -> InterestCredit:
    return InterestCredit(
        credit_date=date(year, 6, 2),
        amount_gross=Decimal("10"),
        currency="USD",
        description="USD Credit Interest",
    )


def _closed_lot(year: int, *, gross_proceeds_money: Decimal | None = None) -> ClosedLot:
    return ClosedLot(
        currency_primary="USD",
        sub_category="COMMON",
        symbol=Ticker("XQAT"),
        description="SAMPLE ISSUER AT INC",
        isin=Isin("US99990G1085"),
        listing_exchange="NASDAQ",
        issuer_country_code=CountryCode("US"),
        quantity=Decimal("4"),
        cost_basis_money=Decimal("300"),
        proceeds_money=Decimal("320"),
        open_date_time=date(year - 1, 6, 15),
        close_date_time=date(year, 6, 15),
        gross_proceeds_money=gross_proceeds_money,
        basis_provenance=BasisProvenance.ENGINE_FIFO,
    )
