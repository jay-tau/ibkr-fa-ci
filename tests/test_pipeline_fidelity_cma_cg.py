"""Fidelity CMA sweep-fund FIFO and Schedule CG pipeline tests."""

from __future__ import annotations

import csv
import errno
import os
from dataclasses import replace
from datetime import date
from decimal import Decimal
from fractions import Fraction
from functools import partial
from pathlib import Path

import pytest

import ibkr_fa.compute.fidelity_cma_capital_gains as cg_compute
import ibkr_fa.pipeline_fidelity_cma_cg as cg_mod
from ibkr_fa.cli import main
from ibkr_fa.compute.exact_inr import exact_decimal
from ibkr_fa.compute.fidelity_cma_capital_gains import (
    FidelityCmaCapitalGains,
    FidelityCmaCapitalGainsError,
    build_fidelity_cma_closed_lots,
    build_fidelity_cma_lot_inventory,
)
from ibkr_fa.compute.schedule_cg import compute_schedule_cg_stcg_lines
from ibkr_fa.compute.schedule_cg_portal_amounts import schedule_cg_portal_amounts
from ibkr_fa.compute.schedule_fsi import compute_schedule_fsi_from_capital_gains
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.output.base import Subcommand
from ibkr_fa.parser.fidelity_cma import (
    CmaCoreTransaction,
    CmaCoreTransactionKind,
    CmaPosition,
    CmaStatement,
    CmaStatementBundle,
    CmaValuationVariant,
    to_source_record,
    validate_statement_bundle,
)
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_fidelity_cma_cg import (
    FidelityCmaCgError,
    FidelityCmaCgRunReport,
    run_fidelity_cma_cg,
)
from ibkr_fa.types import Isin
from tests._cg_lines import loss_across_bands_stcg_result, stcg_result
from tests._crash_reports import assert_unchained_refusal
from tests._exact_refusals import INEXACT_PRODUCT, INEXACT_SUM
from tests._fidelity_cma import (
    HELD_OVER_24_MONTHS_REFUSAL,
    UNMATCHED_SALE_REFUSAL,
    cma_config,
    monthly_history,
)
from tests._fidelity_cma import core_transaction as _tx
from tests._fidelity_cma_cg import (
    _MARKER,
    CG_EARLY_END,
    CG_WINDOW,
    LATE_START,
    LATER_HOLDING,
    PAST_CAP,
    SHARE_REFUSAL,
    thirds,
)
from tests._fx_stubs import AuditedFxStub
from tests._writer_stubs import StubWriter, patch_writers

_ACCOUNT = "Z99-999999"
_HOLDER = "TAX PAYER"
_ISIN = Isin("US99971W1062")
_FY = FinancialYear(2025)
_CONFIG = cma_config(spaxx_isin="US31617H1023")
_PERMISSIONS_ENFORCED = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)


def _statement(
    transactions: tuple[CmaCoreTransaction, ...],
    *,
    quantity: str,
    period_start: date = date(2025, 3, 1),
    period_end: date = date(2026, 3, 31),
    carried_fund: bool = False,
) -> CmaStatement:
    value = Decimal(quantity)
    return CmaStatement(
        period_start=period_start,
        period_end=period_end,
        account_id=_ACCOUNT,
        account_holder=_HOLDER,
        currency="USD",
        valuation_variant=CmaValuationVariant.ACCOUNT_VALUE,
        beginning_account_value_usd=None,
        additions_usd=Decimal(0),
        subtractions_usd=Decimal(0),
        change_in_investment_value_usd=Decimal(0),
        ending_account_value_usd=value,
        accrued_interest_usd=Decimal(0),
        account_value_usd=value,
        free_credit_balance_usd=None,
        position=CmaPosition(
            beginning_market_value_usd=(Decimal("1") if carried_fund else None),
            quantity=value,
            price_per_unit_usd=Decimal(1),
            ending_market_value_usd=value,
            estimated_annual_income_usd=Decimal(0),
            estimated_yield_percent=Decimal(0),
        ),
        core_transactions=transactions,
        income_credits=(),
        withholdings=(),
        cash_activity=(),
        debit_card_transactions=(),
    )


def test_fifo_uses_source_order_and_preserves_split_basis() -> None:
    same_day = date(2025, 4, 15)
    statement = _statement(
        (
            _tx(CmaCoreTransactionKind.BUY, same_day, "10", "2", "20"),
            _tx(CmaCoreTransactionKind.SELL, same_day, "-4", "3", "-12"),
            _tx(CmaCoreTransactionKind.SELL, same_day, "-6", "1", "-6"),
        ),
        quantity="0",
    )
    lots = build_fidelity_cma_closed_lots(validate_statement_bundle((statement,)), spaxx_isin=_ISIN)
    assert all(lot.sub_category is None for lot in lots)
    assert all(lot.listing_exchange is None for lot in lots)
    assert [lot.cost_basis_money for lot in lots] == [Decimal("8"), Decimal("12")]
    assert [lot.proceeds_money for lot in lots] == [Decimal("12"), Decimal("6")]
    assert sum((lot.cost_basis_money for lot in lots), Decimal(0)) == Decimal("20")
    assert [lot.proceeds_money - lot.cost_basis_money for lot in lots] == [
        Decimal("4"),
        Decimal("-6"),
    ]


def test_one_sale_splits_across_multiple_fifo_opens() -> None:
    statement = _statement(
        (
            _tx(CmaCoreTransactionKind.BUY, date(2025, 3, 1), "2", "1", "2"),
            _tx(CmaCoreTransactionKind.REINVESTMENT, date(2025, 3, 2), "3", "2", "6"),
            _tx(CmaCoreTransactionKind.SELL, date(2025, 4, 1), "-4", "3", "-12"),
        ),
        quantity="1",
    )
    lots = build_fidelity_cma_closed_lots(validate_statement_bundle((statement,)), spaxx_isin=_ISIN)
    assert [lot.quantity for lot in lots] == [Decimal("2"), Decimal("2")]
    assert [lot.cost_basis_money for lot in lots] == [Decimal("2"), Decimal("4")]
    assert [lot.proceeds_money for lot in lots] == [Decimal("6"), Decimal("6")]


def test_carried_fcb_does_not_block_complete_sweep_fund_fifo() -> None:
    statement = replace(
        _statement(
            (
                _tx(CmaCoreTransactionKind.BUY, date(2025, 3, 1), "1", "1", "1"),
                _tx(CmaCoreTransactionKind.SELL, date(2025, 4, 1), "-1", "1", "-1"),
            ),
            quantity="0",
        ),
        beginning_account_value_usd=Decimal("5"),
    )
    lots = build_fidelity_cma_closed_lots(validate_statement_bundle((statement,)), spaxx_isin=_ISIN)
    assert len(lots) == 1


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            _statement((), quantity="1", carried_fund=True),
            "carried holding",
        ),
        (
            _statement(
                (_tx(CmaCoreTransactionKind.BUY, date(2025, 3, 1), "1", "1", "1"),),
                quantity="2",
            ),
            "quantities do not match",
        ),
        (
            _statement(
                (_tx(CmaCoreTransactionKind.SELL, date(2025, 4, 1), "-1", "1", "-1"),),
                quantity="0",
            ),
            "complete prior acquisition history",
        ),
    ],
)
def test_fifo_refuses_unprovable_tax_lots(
    statement: CmaStatement,
    message: str,
) -> None:
    with pytest.raises(FidelityCmaCapitalGainsError, match=message):
        build_fidelity_cma_closed_lots(validate_statement_bundle((statement,)), spaxx_isin=_ISIN)


def test_fifo_refuses_sale_larger_than_prior_acquisitions() -> None:
    statement = _statement(
        (
            _tx(CmaCoreTransactionKind.BUY, date(2025, 3, 1), "1", "1", "1"),
            _tx(CmaCoreTransactionKind.SELL, date(2025, 4, 1), "-2", "1", "-2"),
        ),
        quantity="0",
    )
    with pytest.raises(FidelityCmaCapitalGainsError, match="complete prior acquisition history"):
        build_fidelity_cma_closed_lots(validate_statement_bundle((statement,)), spaxx_isin=_ISIN)


def test_fifo_refuses_a_full_sale_after_a_partial_take_of_10_to_the_30_units() -> None:
    """Fails on 0d3547b: 10^30 - 1 left rounded back to 10^30, so the second
    sale matched in full and one unit more was sold than was bought (#1273)."""
    quantity = "1000000000000000000000000000000.000"
    statement = _statement(
        (
            _tx(CmaCoreTransactionKind.BUY, date(2025, 3, 1), quantity, "1", quantity),
            _tx(CmaCoreTransactionKind.SELL, date(2025, 4, 1), "-1.000", "1", "-1.00"),
            _tx(CmaCoreTransactionKind.SELL, date(2025, 4, 2), f"-{quantity}", "1", f"-{quantity}"),
        ),
        quantity="0.000",
    )

    with pytest.raises(FidelityCmaCapitalGainsError) as excinfo:
        build_fidelity_cma_closed_lots(validate_statement_bundle((statement,)), spaxx_isin=_ISIN)

    assert str(excinfo.value) == UNMATCHED_SALE_REFUSAL


def test_fifo_keeps_open_exponent_on_an_exact_remainder_tie() -> None:
    """Fails on 0d3547b: 10^30 - 1 left rounded back to 10^30, so the second
    sale took less than the lot held and the holding check refused (#1273).
    Now the second sale ties the exact remainder and keeps its exponent."""
    quantity = "1000000000000000000000000000000.000"
    rest = "999999999999999999999999999999"
    statement = _statement(
        (
            _tx(CmaCoreTransactionKind.BUY, date(2025, 3, 1), quantity, "1", quantity),
            _tx(CmaCoreTransactionKind.SELL, date(2025, 4, 1), "-1.000", "1", "-1.00"),
            _tx(CmaCoreTransactionKind.SELL, date(2025, 4, 2), f"-{rest}", "1", f"-{rest}.00"),
        ),
        quantity="0.000",
    )

    lots = build_fidelity_cma_closed_lots(validate_statement_bundle((statement,)), spaxx_isin=_ISIN)

    assert [str(lot.quantity) for lot in lots] == ["1.000", f"{rest}.000"]


def _stub_sources(monkeypatch: pytest.MonkeyPatch, *statements: CmaStatement) -> None:
    def load_statements(*_args: object, spaxx_isin: str, **_kwargs: object) -> object:
        bundle = validate_statement_bundle(statements)
        return bundle, {}, to_source_record(bundle, spaxx_isin=spaxx_isin)

    monkeypatch.setattr(cg_mod, "load_fidelity_cma_statements", load_statements)
    monkeypatch.setattr("ibkr_fa.fx.context.Rule115CapitalGainsPolicy", AuditedFxStub)


def _cli_args(input_dir: Path, config_path: Path, output_dir: Path) -> list[str]:
    return [
        "fidelity-cma-cg",
        "--itr-form",
        "ITR-2",
        "--input",
        str(input_dir),
        "--config",
        str(config_path),
        "--output-dir",
        str(output_dir),
        "--financial-year",
        "2025-26",
    ]


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: str = _CONFIG,
    seed_stale: bool = False,
    statements: tuple[CmaStatement, ...] | None = None,
) -> tuple[FidelityCmaCgRunReport, Path]:
    if statements is None:
        statements = (
            _statement(
                (
                    _tx(CmaCoreTransactionKind.BUY, date(2025, 3, 15), "2", "2", "4"),
                    _tx(CmaCoreTransactionKind.SELL, date(2025, 4, 15), "-2", "3", "-6"),
                ),
                quantity="0",
            ),
        )
    _stub_sources(monkeypatch, *statements)
    config_path = tmp_path / "user.toml"
    config_path.write_text(config, encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    if seed_stale:
        for filename in cg_mod._OUTPUT_FILENAMES:
            (output_dir / filename).write_text("stale", encoding="utf-8")
    report = run_fidelity_cma_cg(
        input_dir=tmp_path,
        config_path=config_path,
        output_dir=output_dir,
        financial_year=_FY,
    )
    return report, output_dir


def test_pipeline_writes_stcg_and_empty_ltcg_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, output_dir = _run(tmp_path, monkeypatch)
    assert report.sale_count == 1
    assert report.closed_lot_count == 1
    assert report.stcg_rows == 1
    assert report.ltcg_rows == 0
    assert report.total_stcg_inr_rounded == Decimal("170")
    assert {path.name for path in output_dir.glob("*.csv")} == set(cg_mod._OUTPUT_FILENAMES)
    with (output_dir / "schedule-cg.csv").open(
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        rows = list(csv.reader(handle))
    assert rows[-1][0] == (
        "Schedule CG / A / 5(e) STCG on assets other than at A1 or A2 or A3 or A4 (A5e)"
    )


def test_in_fy_long_held_lot_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statement = _statement(
        (
            _tx(CmaCoreTransactionKind.BUY, date(2022, 1, 1), "1", "1", "1"),
            _tx(CmaCoreTransactionKind.SELL, date(2025, 4, 1), "-1", "1", "-1"),
        ),
        quantity="0",
        period_start=date(2022, 1, 1),
    )
    with pytest.raises(FidelityCmaCgError, match=HELD_OVER_24_MONTHS_REFUSAL):
        _run(tmp_path, monkeypatch, statements=(statement,))


def test_out_of_fy_long_held_lot_does_not_block_requested_fy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statement = _statement(
        (
            _tx(CmaCoreTransactionKind.BUY, date(2021, 1, 1), "1", "1", "1"),
            _tx(CmaCoreTransactionKind.SELL, date(2024, 3, 31), "-1", "1", "-1"),
            _tx(CmaCoreTransactionKind.BUY, date(2025, 3, 15), "1", "1", "1"),
            _tx(CmaCoreTransactionKind.SELL, date(2025, 4, 15), "-1", "1", "-1"),
        ),
        quantity="0",
        period_start=date(2021, 1, 1),
    )
    report, _ = _run(tmp_path, monkeypatch, statements=(statement,))
    assert report.stcg_rows == 1
    assert report.out_of_fy_disposals == 1
    assert report.closed_lot_count == 2


# Gap-free monthly statements holding the sweep-fund units at $1, with no
# beginning market value.
_monthly = partial(monthly_history, price=Decimal(1))


def _opening_history(
    start: date, *, end: date = date(2026, 4, 30), carried_cash: Decimal | None = None
) -> tuple[CmaStatement, ...]:
    """#921's example: units bought on 15 May 2025 and part sold on 16 June."""
    return _monthly(
        _tx(CmaCoreTransactionKind.BUY, date(2025, 5, 15), "10", "1", "10"),
        _tx(CmaCoreTransactionKind.SELL, date(2025, 6, 16), "-4", "1.5", "-6"),
        start=start,
        end=end,
        carried_cash=carried_cash,
    )


def test_account_opened_during_the_fy_writes_its_stcg_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#921: a partial first month with a dash beginning value is the account's
    opening, so no earlier FY sale can be missing."""
    report, output_dir = _run(tmp_path, monkeypatch, statements=_opening_history(date(2025, 5, 12)))
    assert report.statement_count == 12
    assert (report.sale_count, report.closed_lot_count, report.stcg_rows) == (1, 1, 1)
    assert report.total_stcg_inr_rounded == Decimal("170")
    assert {path.name for path in output_dir.glob("*.csv")} == set(cg_mod._OUTPUT_FILENAMES)


@pytest.mark.parametrize(
    ("statements", "gaps"),
    [
        pytest.param(
            _opening_history(date(2025, 5, 1), carried_cash=Decimal("5.00")),
            (LATE_START, LATER_HOLDING),
            id="carried-cash-without-fund",
        ),
        pytest.param(
            _opening_history(date(2025, 5, 12), end=date(2026, 2, 28)),
            (CG_EARLY_END,),
            id="ends-before-fy-end",
        ),
        pytest.param(
            _opening_history(date(2025, 4, 1), end=date(2026, 2, 28), carried_cash=Decimal("5.00")),
            (CG_EARLY_END,),
            id="carried-cash-without-fund-ends-before-fy-end",
        ),
        pytest.param(
            _opening_history(date(2025, 5, 1), end=date(2026, 2, 28), carried_cash=Decimal("5.00")),
            (LATE_START, CG_EARLY_END, LATER_HOLDING),
            id="both-edges",
        ),
        pytest.param(
            _monthly(start=date(2026, 5, 12), end=date(2026, 6, 30)),
            ("the account opened after 31 March 2026, so it has no FY 2025-26 history",),
            id="opened-after-fy-end",
        ),
    ],
)
def test_statement_coverage_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    statements: tuple[CmaStatement, ...],
    gaps: tuple[str, ...],
) -> None:
    """#921: a late start with a carried account value could miss an earlier
    FY sale even with no carried units; an opening after the FY has no FY
    history at all. #1510: the refusal names the window and only the edges
    the statements miss. #1515: an added earlier month may begin with SPAXX
    units, whose history the FIFO then needs, so a late start also names it;
    carried cash from 1 April with no units needs no earlier month."""
    with pytest.raises(FidelityCmaCgError) as refusal:
        _run(tmp_path, monkeypatch, seed_stale=True, statements=statements)
    assert str(refusal.value) == "; ".join((CG_WINDOW, *gaps))
    assert not any((tmp_path / "out").glob("*.csv"))


def test_early_refusal_clears_stale_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_cma = (
        '[account]\nnumber = "U1234567"\nholder_name = "Tax Payer"\n'
        'opening_date = 2020-01-01\nstatus = "BeneficialOwner"\n'
    )
    with pytest.raises(FidelityCmaCgError, match=r"missing \[fidelity_cma\]"):
        _run(tmp_path, monkeypatch, config=no_cma, seed_stale=True)
    assert not any((tmp_path / "out").glob("*.csv"))


@_PERMISSIONS_ENFORCED
def test_unsearchable_output_folder_raises_eacces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """#1238: the output-folder check raises EACCES instead of calling the folder missing."""
    loads: list[object] = []
    monkeypatch.setattr(
        cg_mod, "load_fidelity_cma_statements", lambda *args, **_: loads.append(args)
    )
    config_path = tmp_path / "user.toml"
    config_path.write_text(_CONFIG, encoding="utf-8")
    blocked = tmp_path / "blocked"
    output_dir = blocked / "out"
    output_dir.mkdir(parents=True)
    blocked.chmod(0)
    request.addfinalizer(lambda: blocked.chmod(0o700))
    with pytest.raises(PermissionError) as raised:
        run_fidelity_cma_cg(
            input_dir=tmp_path,
            config_path=config_path,
            output_dir=output_dir,
            financial_year=_FY,
        )
    assert type(raised.value) is PermissionError
    assert str(raised.value) == f"[Errno {errno.EACCES}] Permission denied: {str(output_dir)!r}"
    blocked.chmod(0o700)
    assert list(output_dir.iterdir()) == []
    assert loads == []


def test_writer_failure_rolls_back_both_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingWriter(StubWriter):
        def write(self, _result: object, _path: Path) -> None:
            raise RuntimeError("writer failed")

    patch_writers(monkeypatch, Subcommand.CG_LTCG, FailingWriter)
    with pytest.raises(RuntimeError, match="writer failed"):
        _run(tmp_path, monkeypatch)
    assert not any((tmp_path / "out").glob("*.csv"))


def test_cli_surfaces_stcg_treatment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = FidelityCmaCgRunReport(
        output_dir=tmp_path,
        financial_year=_FY,
        statement_count=16,
        sale_count=1,
        closed_lot_count=2,
        stcg_rows=2,
        ltcg_rows=0,
        out_of_fy_disposals=0,
        total_stcg_inr_rounded=Decimal("170"),
        fx_policy_appropriate_for_ay=True,
        run_outputs=RunOutputs((), ()),
        table_f_loss_across_bands=(),
    )
    monkeypatch.setattr("ibkr_fa.cli.run_fidelity_cma_cg", lambda **_kwargs: report)
    code = main(
        [
            "fidelity-cma-cg",
            "--itr-form",
            "ITR-2",
            "--input",
            "statements",
            "--config",
            "user.toml",
            "--output-dir",
            str(tmp_path),
            "--financial-year",
            "2025-26",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "STCG rows: 2" in captured.out
    assert "NOT Section 111A" in captured.err


def test_run_reports_the_component_total(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#615: the RunReport total carries the Policy A component balance."""
    monkeypatch.setattr(
        cg_compute,
        "compute_schedule_cg_stcg_lines",
        lambda *_args, **_kwargs: stcg_result("1000.4", "1200.6"),
    )

    report, output_dir = _run(tmp_path, monkeypatch)

    assert report.total_stcg_inr_rounded == Decimal("-201")
    with (output_dir / "schedule-cg.csv").open(encoding="utf-8-sig", newline="") as handle:
        assert list(csv.reader(handle))[-1][-1] == "'-201"


def test_loss_across_bands_names_the_table_f_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1361: STCG whose bands are blank for a loss across bands names its
    Table F row and file; the command exits 3 with the manual-fill line."""
    monkeypatch.setattr(
        cg_compute,
        "compute_schedule_cg_stcg_lines",
        lambda *_args, **_kwargs: loss_across_bands_stcg_result(),
    )

    report, output_dir = _run(tmp_path, monkeypatch)

    assert report.table_f_loss_across_bands == ((3, "schedule-cg.csv"),)
    with (output_dir / "schedule-cg.csv").open(encoding="utf-8-sig", newline="") as handle:
        assert [row[-1] for row in list(csv.reader(handle))[-8:-3]] == [""] * 5
    monkeypatch.setattr("ibkr_fa.cli.run_fidelity_cma_cg", lambda **_kwargs: report)
    code = main(
        [
            "fidelity-cma-cg",
            "--itr-form",
            "ITR-2",
            "--input",
            "statements",
            "--config",
            "user.toml",
            "--output-dir",
            str(output_dir),
            "--financial-year",
            "2025-26",
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert (
        "⚠️  MANUAL-FILL: Schedule CG Table F row 3 needs its date bands entered by hand: "
        "a band nets a loss and two or more bands net gains; schedule-cg.csv leaves them blank."
    ) in captured.err.splitlines()
    assert "MANUAL-FILL" not in captured.out


def test_refused_stcg_writes_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#615 owner decision 2: zero consideration with a cost is refused."""
    monkeypatch.setattr(
        cg_compute,
        "compute_schedule_cg_stcg_lines",
        lambda *_args, **_kwargs: stcg_result("0.40", "0.60"),
    )

    with pytest.raises(InvariantError) as excinfo:
        _run(tmp_path, monkeypatch)

    assert dict(excinfo.value.context) == {
        "schedule": None,
        "consideration_inr": None,
        "cost_of_acquisition_inr": None,
    }
    assert list((tmp_path / "out").iterdir()) == []


# ---- filed amounts past the CBDT bound (#830) -------------------------------


def test_cli_refuses_a_consideration_total_past_the_bound_before_any_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two USD 0.7 trillion sweep-fund sales are about ₹6e13 each at the stub's ₹85,
    in range, but the totals they file aren't; the consideration refuses first."""
    lot = "1400000000000"
    sale = "700000000000"
    _stub_sources(
        monkeypatch,
        _statement(
            (
                _tx(CmaCoreTransactionKind.BUY, date(2025, 3, 15), lot, "1", lot),
                _tx(CmaCoreTransactionKind.SELL, date(2025, 4, 15), f"-{sale}", "1", f"-{sale}"),
                _tx(CmaCoreTransactionKind.SELL, date(2025, 5, 15), f"-{sale}", "1", f"-{sale}"),
            ),
            quantity="0",
        ),
    )
    monkeypatch.setattr(
        cg_mod,
        "write_output_bundle",
        lambda *_args: pytest.fail("compute must refuse before any writer is built"),
    )
    config_path = tmp_path / "user.toml"
    config_path.write_text(_CONFIG, encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    code = main(
        [
            "fidelity-cma-cg",
            "--itr-form",
            "ITR-2",
            "--input",
            str(tmp_path),
            "--config",
            str(config_path),
            "--output-dir",
            str(output_dir),
            "--financial-year",
            "2025-26",
        ]
    )

    assert code == 4
    assert "InvariantError: Schedule CG STCG consideration must round" in capsys.readouterr().err
    assert list(output_dir.iterdir()) == []


# ---- exact SPAXX shares (#1244) ---------------------------------------------

_BUY = CmaCoreTransactionKind.BUY
_SELL = CmaCoreTransactionKind.SELL


def assert_share_refusal(error: BaseException, *, statement: int, row: int) -> None:
    """``error`` is the digit-cap refusal for ``statement`` and ``row``, and
    nothing in it or its chain names the account or the marker amount."""
    message = SHARE_REFUSAL.format(statement, row)
    assert type(error) is FidelityCmaCapitalGainsError
    assert (str(error), error.args) == (message, (message,))
    assert (error.__cause__, error.__context__) == (None, None)
    for text in (str(error), repr(error)):
        assert _MARKER not in text
        assert _ACCOUNT not in text


def _exact(amount: Decimal, rational: Fraction | None) -> Fraction:
    return Fraction(amount) if rational is None else rational


def test_thirds_of_a_cost_file_the_exact_gain_in_schedule_cg_and_fsi() -> None:
    """At ₹50 Schedule CG files ₹152 - ₹151 = ₹1, and FSI the exact
    (3.03 - 3.02) x 50 = ₹0.50, which files ₹1 too. A third of USD 3.02 used to
    round up at 28 digits, so FSI got a hair under 50 paise and filed ₹0."""
    lots = build_fidelity_cma_closed_lots(
        validate_statement_bundle((_statement(thirds(), quantity="0.000"),)), spaxx_isin=_ISIN
    )

    third = Fraction(Decimal("3.02")) / 3
    assert [(lot.cost_basis_money, lot.cost_basis_money_exact) for lot in lots] == [
        (exact_decimal(third), third)
    ] * 3
    assert [(lot.proceeds_money, lot.proceeds_money_exact) for lot in lots] == [
        (Decimal("1.01"), None)
    ] * 3
    stcg = compute_schedule_cg_stcg_lines(
        lots,
        financial_year_start=_FY.start_date,
        financial_year_end=_FY.end_date,
        fx_policy=AuditedFxStub(Decimal(50)),
    )
    amounts = schedule_cg_portal_amounts(stcg.stcg_lines, schedule="STCG")
    assert (amounts.consideration_inr, amounts.cost_of_acquisition_inr, amounts.balance_inr) == (
        Decimal(152),
        Decimal(151),
        Decimal(1),
    )
    (row,) = compute_schedule_fsi_from_capital_gains(stcg).rows
    assert (row.income_inr, row.filed.income_inr) == (Decimal("0.5"), Decimal(1))


def test_cli_files_the_thirds_repro_schedule_cg_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fidelity-cma-cg`` files the components ₹152 and ₹151 and the ₹1 balance."""
    _stub_sources(monkeypatch, _statement(thirds(), quantity="0.000"))
    monkeypatch.setattr(
        "ibkr_fa.fx.context.Rule115CapitalGainsPolicy", partial(AuditedFxStub, Decimal(50))
    )
    computed: list[FidelityCmaCapitalGains] = []

    def compute(
        statements: CmaStatementBundle, *, spaxx_isin: Isin, fx_ctx: TaxFxContext
    ) -> FidelityCmaCapitalGains:
        computed.append(
            cg_compute.compute_fidelity_cma_capital_gains(
                statements, spaxx_isin=spaxx_isin, fx_ctx=fx_ctx
            )
        )
        return computed[-1]

    monkeypatch.setattr(cg_mod, "compute_fidelity_cma_capital_gains", compute)
    config_path = tmp_path / "user.toml"
    config_path.write_text(_CONFIG, encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    assert main(_cli_args(tmp_path, config_path, output_dir)) == 0

    amounts = computed[0].stcg.portal_amounts
    assert (amounts.consideration_inr, amounts.cost_of_acquisition_inr, amounts.balance_inr) == (
        Decimal(152),
        Decimal(151),
        Decimal(1),
    )
    with (output_dir / "schedule-cg.csv").open(encoding="utf-8-sig", newline="") as handle:
        assert list(csv.reader(handle))[-1][-1] == "1"


def test_whole_cent_shares_keep_their_decimals_and_carry_no_rational() -> None:
    """Amounts equal to their quantities divide exactly, so every lot keeps the
    Decimal it had and needs no rational."""
    statement = _statement(
        (
            _tx(_BUY, date(2025, 4, 1), "3.000", "1.0000", "3.00"),
            _tx(_SELL, date(2025, 5, 1), "-1.000", "1.0000", "-1.00"),
        ),
        quantity="2.000",
    )
    inventory = build_fidelity_cma_lot_inventory(
        validate_statement_bundle((statement,)),
        spaxx_isin=_ISIN,
        report_date=date(2026, 3, 31),
        mark_price=Decimal(1),
    )

    (closed,) = inventory.closed_lots
    (still_open,) = inventory.open_lots
    assert [str(closed.cost_basis_money), str(closed.proceeds_money)] == ["1.00", "1.00"]
    assert str(still_open.cost_basis_money) == "2.00"
    assert (
        closed.cost_basis_money_exact,
        closed.proceeds_money_exact,
        still_open.cost_basis_money_exact,
    ) == (None, None, None)


def test_shares_of_one_source_add_back_to_it_exactly() -> None:
    """One sale takes all of a first purchase and part of a second. Neither
    the second purchase's cost nor the sale's proceeds divides exactly by
    quantity, yet each source's shares add back to its amount."""
    statement = _statement(
        (
            _tx(_BUY, date(2025, 4, 1), "3.015", "1.0000", "3.02"),
            _tx(_BUY, date(2025, 4, 2), "3.015", "1.0000", "3.01"),
            _tx(_SELL, date(2025, 5, 1), "-4.025", "1.0000", "-4.02"),
        ),
        quantity="2.005",
    )
    inventory = build_fidelity_cma_lot_inventory(
        validate_statement_bundle((statement,)),
        spaxx_isin=_ISIN,
        report_date=date(2026, 3, 31),
        mark_price=Decimal(1),
    )

    first, second = inventory.closed_lots
    (still_open,) = inventory.open_lots
    assert (first.cost_basis_money, first.cost_basis_money_exact) == (Decimal("3.02"), None)
    rationals = (
        second.cost_basis_money_exact,
        still_open.cost_basis_money_exact,
        first.proceeds_money_exact,
        second.proceeds_money_exact,
    )
    assert None not in rationals
    assert _exact(second.cost_basis_money, second.cost_basis_money_exact) + _exact(
        still_open.cost_basis_money, still_open.cost_basis_money_exact
    ) == Fraction(Decimal("3.01"))
    assert sum(
        (_exact(lot.proceeds_money, lot.proceeds_money_exact) for lot in (first, second)),
        Fraction(),
    ) == Fraction(Decimal("4.02"))


@pytest.mark.parametrize(
    ("bought", "sold"),
    [(PAST_CAP, "-1.00"), ("1.00", f"-{PAST_CAP}")],
    ids=["cost", "proceeds"],
)
def test_a_closed_share_past_the_digit_cap_names_the_sale(bought: str, sold: str) -> None:
    """The sale is the third statement's second Core Fund row."""
    statements = _monthly(
        _tx(_BUY, date(2025, 4, 1), "1.000", "1.0000", bought),
        _tx(_BUY, date(2025, 6, 2), "1.000", "1.0000", "1.00"),
        _tx(_SELL, date(2025, 6, 3), "-1.000", "1.0000", sold),
        start=date(2025, 4, 1),
    )

    with pytest.raises(FidelityCmaCapitalGainsError) as excinfo:
        build_fidelity_cma_closed_lots(validate_statement_bundle(statements), spaxx_isin=_ISIN)

    assert_share_refusal(excinfo.value, statement=3, row=2)


def test_an_open_share_past_the_digit_cap_names_the_purchase() -> None:
    """The open lot is the second statement's second Core Fund row."""
    statements = _monthly(
        _tx(_BUY, date(2025, 4, 1), "1.000", "1.0000", "1.00"),
        _tx(_BUY, date(2025, 5, 2), "1.000", "1.0000", "1.00"),
        _tx(_BUY, date(2025, 5, 3), "1.000", "1.0000", PAST_CAP),
        _tx(_SELL, date(2025, 6, 3), "-1.000", "1.0000", "-1.00"),
        start=date(2025, 4, 1),
    )

    with pytest.raises(FidelityCmaCapitalGainsError) as excinfo:
        build_fidelity_cma_lot_inventory(
            validate_statement_bundle(statements),
            spaxx_isin=_ISIN,
            report_date=statements[-1].period_end,
            mark_price=Decimal(1),
        )

    assert_share_refusal(excinfo.value, statement=2, row=2)


def test_cli_share_past_the_digit_cap_exits_4_and_removes_stale_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_sources(
        monkeypatch,
        *_monthly(
            _tx(_BUY, date(2025, 4, 1), "1.000", "1.0000", "1.00"),
            _tx(_SELL, date(2025, 6, 3), "-1.000", "1.0000", f"-{PAST_CAP}"),
            start=date(2025, 4, 1),
        ),
    )
    config_path = tmp_path / "user.toml"
    config_path.write_text(_CONFIG, encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    for filename in cg_mod._OUTPUT_FILENAMES:
        (output_dir / filename).write_text("stale", encoding="utf-8")

    assert main(_cli_args(tmp_path, config_path, output_dir)) == 4

    err = capsys.readouterr().err
    assert err == f"error: {SHARE_REFUSAL.format(3, 1)}\n"
    assert _MARKER not in err
    assert _ACCOUNT not in err
    assert list(output_dir.iterdir()) == []


# ---- exact SPAXX quantities and gross proceeds (#1273) ----------------------

_TEN_TO_THE_30 = "1000000000000000000000000000000"
_ONE_UNIT_LESS = "999999999999999999999999999999"
# 999 digits holding the privacy marker: within the cap, but not with cents.
_WIDE = _MARKER + "0" * 993
_QUANTITY_REFUSAL = (
    "InvariantError: SPAXX lot allocation, statement 1 activity row {}, column quantity: "
    + INEXACT_SUM
)


def _one_unit_sold_of_10_to_the_30() -> CmaStatement:
    """10^30 units bought, 1 sold, and 10^30 - 1 held at the month end."""
    return _statement(
        (
            _tx(_BUY, date(2025, 4, 1), _TEN_TO_THE_30, "1", f"{_TEN_TO_THE_30}.00"),
            _tx(_SELL, date(2025, 5, 1), "-1", "1", "-1.00"),
        ),
        quantity=_ONE_UNIT_LESS,
    )


def _assert_invariant_refusal(error: BaseException, text: str) -> None:
    """``error`` is the fixed-text ``InvariantError`` that prints ``text``, with
    no context, chaining nothing and naming neither the marker nor the account."""
    expected = InvariantError(what=text.removeprefix("InvariantError: "), context=())
    assert_unchained_refusal(error, expected, text, markers=(_MARKER, _ACCOUNT))


def test_fifo_holds_10_to_the_30_less_the_unit_sold() -> None:
    """Fails on 0d3547b: the running quantity 10^30 - 1 rounded back to 10^30,
    so the month-end check refused with "SPAXX transaction quantities do not
    match the monthly holding"."""
    lots = build_fidelity_cma_closed_lots(
        validate_statement_bundle((_one_unit_sold_of_10_to_the_30(),)), spaxx_isin=_ISIN
    )

    assert [(str(lot.quantity), lot.cost_basis_money) for lot in lots] == [("1", Decimal("1.00"))]


def test_inventory_keeps_an_open_lot_of_10_to_the_30_less_one_unit() -> None:
    """Fails on 0d3547b: FIFO's running quantity refused first, as in
    :func:`test_fifo_holds_10_to_the_30_less_the_unit_sold`; the open lot
    itself had rounded back to 10^30 units. Now it holds exactly 10^30 - 1,
    which matches the report-date holding."""
    inventory = build_fidelity_cma_lot_inventory(
        validate_statement_bundle((_one_unit_sold_of_10_to_the_30(),)),
        spaxx_isin=_ISIN,
        report_date=date(2026, 3, 31),
        mark_price=Decimal(1),
    )

    (still_open,) = inventory.open_lots
    assert str(still_open.quantity) == _ONE_UNIT_LESS
    assert (still_open.cost_basis_money, still_open.cost_basis_money_exact) == (
        Decimal(f"{_ONE_UNIT_LESS}.00"),
        None,
    )
    assert [str(lot.quantity) for lot in inventory.closed_lots] == ["1"]


@pytest.mark.parametrize(
    ("transactions", "held", "row"),
    [
        pytest.param(
            (
                _tx(_BUY, date(2025, 4, 1), "0.01", "1", "0.01"),
                _tx(_SELL, date(2025, 5, 1), f"-{_WIDE}", "1", "-1.00"),
            ),
            "0",
            2,
            id="unmatched-rest",
        ),
        pytest.param(
            (
                _tx(_BUY, date(2025, 4, 1), _WIDE, "1", "1.00"),
                _tx(_SELL, date(2025, 5, 1), "-0.01", "1", "-0.01"),
            ),
            f"{int(_WIDE) - 1}.99",
            2,
            id="lot-remainder",
        ),
    ],
)
def test_a_fifo_take_past_the_digit_cap_names_the_sale(
    transactions: tuple[CmaCoreTransaction, ...], held: str, row: int
) -> None:
    """A 999-digit quantity less a cent needs 1,001 digits. Fails on 0d3547b:
    the rest of the sale rounded and refused as a shortfall, and the lot's
    remainder rounded and refused at the month-end holding check."""
    statement = _statement(transactions, quantity=held)

    with pytest.raises(InvariantError) as excinfo:
        build_fidelity_cma_closed_lots(validate_statement_bundle((statement,)), spaxx_isin=_ISIN)

    _assert_invariant_refusal(excinfo.value, _QUANTITY_REFUSAL.format(row))


@pytest.mark.parametrize(
    ("transactions", "held", "row"),
    [
        pytest.param(
            (
                _tx(_BUY, date(2025, 4, 1), "0.01", "1", "0.01"),
                _tx(_BUY, date(2025, 4, 2), _WIDE, "1", "1.00"),
            ),
            f"{_WIDE}.01",
            2,
            id="purchase",
        ),
        pytest.param(
            (
                _tx(_BUY, date(2025, 4, 1), "1", "1", "1.00"),
                _tx(_BUY, date(2025, 4, 2), _WIDE, "1", "1.00"),
                _tx(_SELL, date(2025, 5, 1), "-0.05", "1", "-0.05"),
            ),
            f"{_WIDE}.95",
            3,
            id="sale",
        ),
    ],
)
def test_a_running_quantity_past_the_digit_cap_names_the_added_row(
    transactions: tuple[CmaCoreTransaction, ...], held: str, row: int
) -> None:
    """The 999-digit holding plus or less cents needs 1,001 digits. Fails on
    0d3547b: the running quantity rounded and refused at the month-end
    holding check."""
    statement = _statement(transactions, quantity=held)

    with pytest.raises(InvariantError) as excinfo:
        build_fidelity_cma_closed_lots(validate_statement_bundle((statement,)), spaxx_isin=_ISIN)

    _assert_invariant_refusal(excinfo.value, _QUANTITY_REFUSAL.format(row))


def test_gross_proceeds_past_the_digit_cap_name_the_sale() -> None:
    """1.11 units at a 999-digit price come to 1,001 digits. Fails on 0d3547b:
    the product rounded to 28 digits and the lot was built."""
    price = "1." + "0" * 992 + _MARKER
    statement = _statement(
        (
            _tx(_BUY, date(2025, 4, 1), "1.11", "1", "1.11"),
            _tx(_SELL, date(2025, 5, 1), "-1.11", price, "-1.11"),
        ),
        quantity="0",
    )

    with pytest.raises(InvariantError) as excinfo:
        build_fidelity_cma_closed_lots(validate_statement_bundle((statement,)), spaxx_isin=_ISIN)

    _assert_invariant_refusal(
        excinfo.value,
        "InvariantError: SPAXX lot allocation, statement 1 activity row 2, column "
        f"gross_proceeds_money: {INEXACT_PRODUCT}",
    )


def test_gross_proceeds_past_28_digits_are_exact() -> None:
    """Fails on 0d3547b: 3 units at USD 1.000000000000000000000000000001 came
    to USD 3.000000000000000000000000000, the product cut to 28 digits."""
    price = "1.000000000000000000000000000001"
    statement = _statement(
        (
            _tx(_BUY, date(2025, 4, 1), "3", "1", "3.00"),
            _tx(_SELL, date(2025, 5, 1), "-3", price, "-3.00"),
        ),
        quantity="0",
    )

    (lot,) = build_fidelity_cma_closed_lots(
        validate_statement_bundle((statement,)), spaxx_isin=_ISIN
    )

    assert str(lot.gross_proceeds_money) == "3.000000000000000000000000000003"
