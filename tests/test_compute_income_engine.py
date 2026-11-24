"""The shared income engine stays below the pipeline and names no broker (#607)."""

from __future__ import annotations

import ast
import dataclasses
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.compute import income_engine
from ibkr_fa.compute.form67 import (
    Form67Readiness,
    Form67RefusalReason,
    Form67RowReadiness,
    form67_row_readiness,
)
from ibkr_fa.compute.income_engine import compute_income_schedules
from ibkr_fa.compute.schedule_fsi import InterestPayerCountryError
from ibkr_fa.compute.wht_reversal_pairing import UnprovenWhtReversalError
from ibkr_fa.config import (
    Form67Config,
    Form67TaxPayableOverride,
    FxConfig,
    load_user_config,
)
from ibkr_fa.exceptions import IbkrFaError, InvariantError
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.models import Dividend, HeadOfIncome, OtherSourceIncome
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.models.form67_entry import Form67SourceOfIncome, Form67TreatyRateStatus
from ibkr_fa.models.interest_credit import InterestCredit
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._foreign_tax_refusals import (
    NO_ORIGINAL,
    assert_no_wht_echo,
    orphan_wht_message,
    unproven_reversal_message,
)
from tests._ftc_warnings import FORM67_RELIEF_REFUSAL as _RELIEF_REFUSAL


def _forbidden_imports(source: str) -> list[str]:
    """Return each import of ``ibkr_fa.pipeline*``, ``ibkr_fa.parser`` or ``ibkr_fa.output``."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            parts = ["ibkr_fa", "compute"][: 3 - node.level] if node.level else []
            if node.module:
                parts.append(node.module)
            module = ".".join(parts)
            targets = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        else:
            continue
        for target in targets:
            parts = target.split(".")
            if (
                len(parts) > 1
                and parts[0] == "ibkr_fa"
                and (parts[1] in {"parser", "output"} or parts[1].startswith("pipeline"))
            ):
                found.append(f"{node.lineno}: {ast.unparse(node)}")
                break
    return found


def test_income_engine_imports_no_pipeline_parser_or_output() -> None:
    source = Path(income_engine.__file__).read_text(encoding="utf-8")
    assert _forbidden_imports(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "from ibkr_fa.pipeline import FinancialYear",
        "import ibkr_fa.pipeline_fsi_compute",
        "from ibkr_fa import pipeline_os",
        "from ibkr_fa.parser.activity_statement import parse_dividends",
        "from ibkr_fa import output",
        "from ..output import get_writer",
        "from .. import parser",
        "def f():\n    from ibkr_fa.pipeline import FinancialYear",
    ],
)
def test_scan_flags_forbidden_import(source: str) -> None:
    assert len(_forbidden_imports(source)) == 1


@pytest.mark.parametrize(
    ("compute", "extra_kwargs"),
    [
        (
            income_engine.compute_income_schedules,
            {"withholding_taxes": [], "closed_lots": [], "interest_payer_country_iso2": None},
        ),
        (income_engine.compute_schedule_os_result, {}),
    ],
)
def test_wrong_fy_context_refuses_before_loading_ttbr(compute, extra_kwargs, tmp_path, monkeypatch):
    config_path = tmp_path / "user.toml"
    config_path.write_text('[account]\nholder_name = "Tax Payer"\n', encoding="utf-8")
    config = load_user_config(config_path)

    def unexpected_ttbr(_currency):
        pytest.fail("wrong-FY context must refuse before loading TTBR")

    monkeypatch.setattr("ibkr_fa.fx.policy_rule115.load_ttbr", unexpected_ttbr)
    with pytest.raises(InvariantError, match="fx_ctx must be built for the financial year"):
        compute(
            config,
            FinancialYear(2025),
            fx_ctx=TaxFxContext.for_fy(FinancialYear(2026), config.fx),
            dividends=[],
            interest_credits=[],
            **extra_kwargs,
        )


def test_orphan_paid_wht_error_names_no_broker(tmp_path: Path) -> None:
    """The engine's orphan refusal is broker-neutral, and counts the event
    without its ISIN, date or amount (#1047)."""
    config_path = tmp_path / "user.toml"
    config_path.write_text(
        """\
[account]
number = "U1234567"
holder_name = "Tax Payer"
opening_date = 2020-01-01
status = "BeneficialOwner"
""",
        encoding="utf-8",
    )
    orphan = WithholdingTax(
        symbol=Ticker("XQAQ"),
        isin=Isin("US99992A1097"),
        payment_date=date(2025, 9, 12),
        amount=Decimal("-3.60"),
        currency="USD",
        description="XQAQ(US99992A1097) Cash Dividend USD 0.24 per Share - US Tax",
    )

    with pytest.raises(IbkrFaError) as exc:
        compute_income_schedules(
            load_user_config(config_path),
            FinancialYear(2025),
            fx_ctx=TaxFxContext.for_fy(FinancialYear(2025), FxConfig()),
            dividends=[],
            withholding_taxes=[orphan],
            closed_lots=[],
            interest_credits=[],
            interest_payer_country_iso2=None,
        )

    msg = str(exc.value)
    assert msg == orphan_wht_message(1)
    assert_no_wht_echo(msg, [orphan])
    assert "§90" in msg
    assert "Rule 128" in msg
    assert "IBKR" not in msg
    assert "Activity Statement" not in msg


# ---- interest payer country (#698) ----------------------------------------

_FY = FinancialYear(2025)


def _credit(day: date) -> InterestCredit:
    return InterestCredit(
        credit_date=day,
        amount_gross=Decimal("10.00"),
        currency="USD",
        description="USD Credit Interest",
    )


_IN_FY_CREDIT = _credit(date(2025, 6, 30))
_OUT_OF_FY_CREDIT = _credit(date(2024, 6, 30))
_CREDITS = pytest.mark.parametrize(
    "interest",
    [[_IN_FY_CREDIT], [_OUT_OF_FY_CREDIT], []],
    ids=["in-fy", "out-of-fy", "none"],
)


def _engine_config(tmp_path: Path):
    config_path = tmp_path / "user.toml"
    config_path.write_text('[account]\nholder_name = "Tax Payer"\n', encoding="utf-8")
    return load_user_config(config_path)


def _run_engine(tmp_path: Path, interest: list[InterestCredit], payer: object):
    config = _engine_config(tmp_path)
    return compute_income_schedules(
        config,
        _FY,
        fx_ctx=TaxFxContext.for_fy(_FY, config.fx),
        dividends=[],
        withholding_taxes=[],
        closed_lots=[],
        interest_credits=interest,
        interest_payer_country_iso2=payer,  # ty: ignore[invalid-argument-type]
    )


@pytest.fixture
def no_fsi_compute(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(**_kwargs: object) -> None:
        pytest.fail("a refused payer country must stop before compute_schedule_fsi")

    monkeypatch.setattr(income_engine, "compute_schedule_fsi", unexpected)


@_CREDITS
@pytest.mark.parametrize("payer", ["IE", "us", "", "ZZ", False, []], ids=repr)
@pytest.mark.usefixtures("no_fsi_compute")
def test_engine_refuses_every_payer_but_us(
    tmp_path: Path, interest: list[InterestCredit], payer: object
) -> None:
    with pytest.raises(InterestPayerCountryError) as exc:
        _run_engine(tmp_path, interest, payer)
    assert str(exc.value) == (
        f"interest payer country {payer!r} is not supported; only US interest payers are supported"
    )


@pytest.mark.parametrize(
    "interest", [[_IN_FY_CREDIT], [_OUT_OF_FY_CREDIT]], ids=["in-fy", "out-of-fy"]
)
@pytest.mark.usefixtures("no_fsi_compute")
def test_engine_refuses_interest_without_a_stated_payer(
    tmp_path: Path, interest: list[InterestCredit]
) -> None:
    with pytest.raises(InterestPayerCountryError) as exc:
        _run_engine(tmp_path, interest, None)
    assert str(exc.value) == (
        "interest credits were supplied, but the source did not state their payer country"
    )


@pytest.fixture
def fsi_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Record the payer arguments the engine passes on."""
    calls: list[dict[str, object]] = []
    compute_fsi = income_engine.compute_schedule_fsi
    enrich = income_engine.enrich_fsi_with_form67_relief

    def spy_fsi(**kwargs: object):
        calls.append({"broker_domicile_iso2": kwargs["broker_domicile_iso2"]})
        return compute_fsi(**kwargs)  # ty: ignore[invalid-argument-type]

    def spy_enrich(*args: object, **kwargs: object):
        calls.append({"interest_income_inr_by_iso": kwargs["interest_income_inr_by_iso"]})
        return enrich(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(income_engine, "compute_schedule_fsi", spy_fsi)
    monkeypatch.setattr(income_engine, "enrich_fsi_with_form67_relief", spy_enrich)
    return calls


def test_engine_files_us_interest_under_us(
    tmp_path: Path, fsi_calls: list[dict[str, object]]
) -> None:
    result = _run_engine(tmp_path, [_IN_FY_CREDIT, _OUT_OF_FY_CREDIT], "US")

    [row] = result.fsi_result.rows
    assert (row.iso_alpha2, row.head_of_income, row.event_count) == (
        "US",
        HeadOfIncome.OTHER_SOURCES,
        1,
    )
    assert fsi_calls == [
        {"broker_domicile_iso2": "US"},
        {"interest_income_inr_by_iso": {"US": row.income_inr}},
    ]


@pytest.mark.parametrize("payer", ["US", None])
def test_engine_accepts_no_interest_with_us_or_none(
    tmp_path: Path, fsi_calls: list[dict[str, object]], payer: str | None
) -> None:
    result = _run_engine(tmp_path, [], payer)

    assert result.fsi_result.rows == []
    assert fsi_calls == [{"broker_domicile_iso2": payer}, {"interest_income_inr_by_iso": {}}]


def test_engine_payer_argument_is_required(tmp_path: Path) -> None:
    config = _engine_config(tmp_path)
    with pytest.raises(TypeError, match="interest_payer_country_iso2"):
        compute_income_schedules(  # ty: ignore[missing-argument]
            config,
            _FY,
            fx_ctx=TaxFxContext.for_fy(_FY, config.fx),
            dividends=[],
            withholding_taxes=[],
            closed_lots=[],
            interest_credits=[],
        )


def test_engine_refuses_a_reversal_whose_original_year_is_unproven(tmp_path: Path) -> None:
    """#893: the engine's shared WHT netting refuses an orphan refund,
    without printing its ISIN, amount, currency or date (#1047)."""
    refund = WithholdingTax(
        symbol=Ticker("XQAQ"),
        isin=Isin("US99992A1097"),
        payment_date=date(2025, 9, 12),
        amount=Decimal("3.60"),
        currency="USD",
        description="XQAQ(US99992A1097) Cash Dividend USD 0.24 per Share - US Tax",
    )
    config = _engine_config(tmp_path)

    with pytest.raises(UnprovenWhtReversalError) as exc:
        compute_income_schedules(
            config,
            _FY,
            fx_ctx=TaxFxContext.for_fy(_FY, config.fx),
            dividends=[],
            withholding_taxes=[refund],
            closed_lots=[],
            interest_credits=[],
            interest_payer_country_iso2=None,
        )
    assert str(exc.value) == unproven_reversal_message(NO_ORIGINAL)
    assert_no_wht_echo(str(exc.value), [refund])


# ---- same-day dividend lines (#1283) --------------------------------------

_SAME_DAY = date(2025, 9, 12)
_XQBA_ISIN = Isin("US99995C1027")


def _same_day_dividend(kind: DistributionKind, account_index: int | None) -> Dividend:
    return Dividend(
        symbol=Ticker("XQBA"),
        isin=_XQBA_ISIN,
        payment_date=_SAME_DAY,
        amount_gross=Decimal("100.00"),
        currency="USD",
        description=f"XQBA({_XQBA_ISIN}) {kind.value} USD 10.00 per Share",
        kind=kind,
        source_account_index=account_index,
    )


@pytest.mark.parametrize("account_index", [None, 0], ids=["no-account-index", "account-0"])
def test_engine_leaves_same_day_dividend_lines_unproven(
    tmp_path: Path, account_index: int | None
) -> None:
    """#1283: the shared engine treats several dividend lines on one payment key
    alike for every source. Without an account index (IBKR, NetBenefits,
    StockPlan, Fidelity CMA) the USD 30 isn't pooled with the unwithheld line
    into 15% any more than with Robinhood's index: the credit is blank, and
    with col 7 set the row is refused."""
    config_path = tmp_path / "user.toml"
    config_path.write_text(
        '[account]\nholder_name = "Tax Payer"\n\n[form67]\n'
        "[[form67.tax_payable_overrides]]\n"
        'country = "US"\nsource = "DIVIDEND"\ntax_payable_normal_inr = "10000"\n',
        encoding="utf-8",
    )
    config = load_user_config(config_path)
    withholding = WithholdingTax(
        symbol=Ticker("XQBA"),
        isin=_XQBA_ISIN,
        payment_date=_SAME_DAY,
        amount=Decimal("-30.00"),
        currency="USD",
        description=f"XQBA({_XQBA_ISIN}) Cash Dividend USD 10.00 per Share - US Tax",
        source_account_index=account_index,
    )

    result = compute_income_schedules(
        config,
        _FY,
        fx_ctx=TaxFxContext.for_fy(_FY, config.fx),
        dividends=[
            _same_day_dividend(DistributionKind.CASH_DIVIDEND, account_index),
            _same_day_dividend(DistributionKind.SPECIAL_DIVIDEND, account_index),
        ],
        withholding_taxes=[withholding],
        closed_lots=[],
        interest_credits=[],
        interest_payer_country_iso2=None,
    )

    assert result.os_dividend_rows_consumed == 2
    (entry,) = result.form67_result.entries
    assert entry.treaty_rate_status is Form67TreatyRateStatus.UNPROVEN
    assert entry.payment_allocation_unproven
    assert entry.highest_payment_rate_percent is None
    assert entry.tax_payable_normal_inr == Decimal("10000")
    assert entry.total_credit_inr is None
    assert form67_row_readiness(entry) == Form67RowReadiness(
        Form67Readiness.REFUSED, Form67RefusalReason.PAYMENT_ALLOCATION_UNPROVEN
    )
    (fsi_row,) = (
        row for row in result.fsi_result.rows if row.head_of_income is HeadOfIncome.OTHER_SOURCES
    )
    assert fsi_row.tax_relief_inr is None


# ---- Form 67 credit vs FSI relief (#1416) ---------------------------------

_XQAA_DAY = date(2025, 6, 16)
_XQAA_ISIN = Isin("US99995B1044")


def test_engine_refuses_a_dividend_credit_its_mixed_fsi_row_cannot_carry(
    tmp_path: Path,
) -> None:
    """USD 100 dividend with USD 20 US withholding, plus USD 10 US interest. Only
    the dividend has a tax-payable override, so Form 67 computes the dividend's
    credit while the FSI Other Sources row, which also holds the interest, leaves
    its relief blank. The engine's own cross-form check refuses."""
    config_path = tmp_path / "user.toml"
    config_path.write_text(
        '[account]\nholder_name = "Tax Payer"\n\n'
        '[account.foreign_tins]\nUS = "123456789"\n\n'
        "[form67]\n[[form67.tax_payable_overrides]]\n"
        'country = "US"\nsource = "DIVIDEND"\ntax_payable_normal_inr = "2000"\n',
        encoding="utf-8",
    )
    config = load_user_config(config_path)
    dividend = Dividend(
        symbol=Ticker("XQAA"),
        isin=_XQAA_ISIN,
        payment_date=_XQAA_DAY,
        amount_gross=Decimal("100.00"),
        currency="USD",
        description=f"XQAA({_XQAA_ISIN}) Cash Dividend USD 1.00 per Share",
        kind=DistributionKind.CASH_DIVIDEND,
    )
    withholding = WithholdingTax(
        symbol=Ticker("XQAA"),
        isin=_XQAA_ISIN,
        payment_date=_XQAA_DAY,
        amount=Decimal("-20.00"),
        currency="USD",
        description=f"XQAA({_XQAA_ISIN}) Cash Dividend USD 1.00 per Share - US Tax",
    )

    with pytest.raises(InvariantError) as caught:
        compute_income_schedules(
            config,
            _FY,
            fx_ctx=TaxFxContext.for_fy(_FY, config.fx),
            dividends=[dividend],
            withholding_taxes=[withholding],
            closed_lots=[],
            interest_credits=[_IN_FY_CREDIT],
            interest_payer_country_iso2="US",
        )
    assert str(caught.value) == _RELIEF_REFUSAL


# ---- remedies that clear the FSI relief refusal (#1427, #1446) ------------

_FEE_DAY = date(2025, 9, 1)


def _override(source: Form67SourceOfIncome, amount: str) -> Form67TaxPayableOverride:
    return Form67TaxPayableOverride(
        iso_alpha2="US", source_of_income=source, tax_payable_normal_inr=Decimal(amount)
    )


def _fee_engine(
    tmp_path: Path,
    overrides: tuple[Form67TaxPayableOverride, ...],
    *,
    with_fee: bool = True,
    with_interest: bool = False,
):
    """USD 100 XQAA dividend with USD 20 US withholding and, unless
    ``with_fee`` is false, a reviewed USD 10 US stock-lending fee, as
    Robinhood's income leg hands them to the shared engine. ``with_interest``
    adds USD 5 of US interest."""
    config = dataclasses.replace(
        _engine_config(tmp_path), form67=Form67Config(tax_payable_overrides=overrides)
    )
    return compute_income_schedules(
        config,
        _FY,
        fx_ctx=TaxFxContext.for_fy(_FY, config.fx),
        dividends=[
            Dividend(
                symbol=Ticker("XQAA"),
                isin=_XQAA_ISIN,
                payment_date=_FEE_DAY,
                amount_gross=Decimal("100"),
                currency="USD",
                description=f"XQAA({_XQAA_ISIN}) Cash Dividend USD 1.00 per Share",
                kind=DistributionKind.CASH_DIVIDEND,
            )
        ],
        withholding_taxes=[
            WithholdingTax(
                symbol=Ticker("XQAA"),
                isin=_XQAA_ISIN,
                payment_date=_FEE_DAY,
                amount=Decimal("-20"),
                currency="USD",
                description=f"XQAA({_XQAA_ISIN}) Cash Dividend USD 1.00 per Share - US Tax",
            )
        ],
        closed_lots=[],
        interest_credits=(
            [
                InterestCredit(
                    credit_date=_FEE_DAY,
                    amount_gross=Decimal("5"),
                    currency="USD",
                    description="USD Credit Interest",
                )
            ]
            if with_interest
            else []
        ),
        interest_payer_country_iso2="US" if with_interest else None,
        other_source_incomes=(
            [
                OtherSourceIncome(
                    nature="Stock lending fee",
                    payment_date=_FEE_DAY,
                    amount_gross=Decimal("10"),
                    currency="USD",
                    description="Synthetic loan fee",
                    source_country_iso2=CountryCode("US"),
                )
            ]
            if with_fee
            else []
        ),
    )


def _us_other_sources_row(result):
    (row,) = (
        row
        for row in result.fsi_result.rows
        if row.iso_alpha2 == "US" and row.head_of_income is HeadOfIncome.OTHER_SOURCES
    )
    return row


def test_fee_country_dividend_override_alone_refuses(tmp_path: Path) -> None:
    """A DIVIDEND override alone leaves the fee's OTHERS slice without Indian
    tax, so the row's relief stays blank and the refusal names the OTHERS
    amount that completes it."""
    with pytest.raises(InvariantError) as caught:
        _fee_engine(tmp_path, (_override(Form67SourceOfIncome.DIVIDEND, "30"),))
    assert str(caught.value) == _RELIEF_REFUSAL


def test_fee_country_others_override_completes_relief(tmp_path: Path) -> None:
    """#1446: the engine honours an exact OTHERS amount for a country with
    reviewed lending fees, so the row's tax payable is the two overrides' sum
    and its relief is the Form 67 credit."""
    result = _fee_engine(
        tmp_path,
        (
            _override(Form67SourceOfIncome.DIVIDEND, "30"),
            _override(Form67SourceOfIncome.OTHERS, "3"),
        ),
    )
    assert result.form67_result.readiness is Form67Readiness.READY
    (entry,) = result.form67_result.entries
    row = _us_other_sources_row(result)
    assert row.tax_payable_inr == Decimal("33")
    assert entry.total_credit_inr is not None
    assert row.tax_relief_inr == entry.total_credit_inr


def test_fee_country_with_interest_completes_with_three_overrides(tmp_path: Path) -> None:
    """With US interest in the row too, an override for each of its three
    sources completes the relief."""
    result = _fee_engine(
        tmp_path,
        (
            _override(Form67SourceOfIncome.DIVIDEND, "30"),
            _override(Form67SourceOfIncome.INTEREST_INCOME, "1.5"),
            _override(Form67SourceOfIncome.OTHERS, "3"),
        ),
        with_interest=True,
    )
    assert result.form67_result.readiness is Form67Readiness.READY
    (entry,) = result.form67_result.entries
    row = _us_other_sources_row(result)
    assert row.tax_payable_inr == Decimal("34.5")
    assert entry.total_credit_inr is not None
    assert row.tax_relief_inr == entry.total_credit_inr


@pytest.mark.parametrize(
    "missing",
    [Form67SourceOfIncome.INTEREST_INCOME, Form67SourceOfIncome.OTHERS],
    ids=["no-interest-override", "no-others-override"],
)
def test_fee_country_with_interest_refuses_without_each_override(
    tmp_path: Path, missing: Form67SourceOfIncome
) -> None:
    """The same income with only two of the three overrides refuses."""
    overrides = tuple(
        _override(source, amount)
        for source, amount in (
            (Form67SourceOfIncome.DIVIDEND, "30"),
            (Form67SourceOfIncome.INTEREST_INCOME, "1.5"),
            (Form67SourceOfIncome.OTHERS, "3"),
        )
        if source is not missing
    )
    with pytest.raises(InvariantError) as caught:
        _fee_engine(tmp_path, overrides, with_interest=True)
    assert str(caught.value) == _RELIEF_REFUSAL


def test_fee_country_without_dividend_override_is_manual_fill(tmp_path: Path) -> None:
    """Without the DIVIDEND override, a fee country returns the manual-fill
    result: Form 67 blank, the FSI row's tax and relief blank."""
    result = _fee_engine(tmp_path, ())
    assert result.form67_result.readiness is Form67Readiness.BLANK
    row = _us_other_sources_row(result)
    assert row.tax_payable_inr is None
    assert row.tax_relief_inr is None


def test_dividend_only_country_override_completes_form67(tmp_path: Path) -> None:
    """The override route the Robinhood reason names: without the fee, a
    DIVIDEND override completes Form 67 and the FSI row's relief."""
    result = _fee_engine(
        tmp_path, (_override(Form67SourceOfIncome.DIVIDEND, "30"),), with_fee=False
    )
    assert result.form67_result.readiness is Form67Readiness.READY
    (entry,) = result.form67_result.entries
    row = _us_other_sources_row(result)
    assert row.tax_payable_inr == Decimal("30")
    assert entry.total_credit_inr is not None
    assert row.tax_relief_inr == entry.total_credit_inr


# ---- late withholding remedy (#1424) --------------------------------------

_XQAQ_ISIN = Isin("US99992A1097")
_MARCH_DIVIDEND = Dividend(
    symbol=Ticker("XQAQ"),
    isin=_XQAQ_ISIN,
    payment_date=date(2026, 3, 20),
    amount_gross=Decimal("100.00"),
    currency="USD",
    description=f"XQAQ({_XQAQ_ISIN}) Cash Dividend USD 1.00 per Share",
    kind=DistributionKind.CASH_DIVIDEND,
)
_APRIL_WITHHOLDING = WithholdingTax(
    symbol=Ticker("XQAQ"),
    isin=_XQAQ_ISIN,
    payment_date=date(2026, 4, 2),
    amount=Decimal("-20.00"),
    currency="USD",
    description=f"XQAQ({_XQAQ_ISIN}) Cash Dividend USD 1.00 per Share - US Tax",
)


def test_orphan_refusal_no_longer_offers_an_earlier_fy_rerun(tmp_path: Path) -> None:
    """A dividend paid on 20 March whose US tax is booked on 2 April. The
    booking's FY refuses. The dividend's FY reads only withholding booked
    inside it, so it completes without that tax, and the refusal mustn't
    send the filer there."""
    config = _engine_config(tmp_path)

    def run(fy: FinancialYear):
        return compute_income_schedules(
            config,
            fy,
            fx_ctx=TaxFxContext.for_fy(fy, config.fx),
            dividends=[_MARCH_DIVIDEND],
            withholding_taxes=[_APRIL_WITHHOLDING],
            closed_lots=[],
            interest_credits=[],
            interest_payer_country_iso2=None,
        )

    with pytest.raises(IbkrFaError) as exc:
        run(FinancialYear(2026))
    message = str(exc.value)
    assert message == orphan_wht_message(1, fy_start=date(2026, 4, 1), fy_end=date(2027, 3, 31))
    assert "Claim its credit by hand" in message
    assert "re-run with the FY" not in message

    earlier = run(FinancialYear(2025))
    assert len(earlier.os_result.lines) == 1
    assert len(earlier.form67_result.entries) == 0
    assert earlier.wht_rows_in_fy == 0
