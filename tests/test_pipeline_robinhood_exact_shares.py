"""Exact Robinhood lot shares through the robinhood-cg and robinhood-income commands (#1241).

Each run reconciles a synthetic two-account bundle and runs the real lot,
Schedule CG and income-schedule steps. Only the bundle load, the TTBR table
(set to the issue's rates) and robinhood-income's source income (empty) are
stubbed.
"""

from __future__ import annotations

import csv
from dataclasses import fields, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
import ibkr_fa.pipeline_robinhood_core as core
from ibkr_fa.compute.robinhood_income import RobinhoodIncomeResult
from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome
from ibkr_fa.normalize.robinhood import (
    reconcile_robinhood_accounts,
    resolve_robinhood_security_identities,
)
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import RobinhoodStatementActivity
from ibkr_fa.pipeline_robinhood import RobinhoodLegStatus, run_robinhood_cg, run_robinhood_income
from ibkr_fa.pipeline_robinhood_outputs import CG_FILENAMES
from tests._crash_reports import assert_replaced_unchained
from tests._exact_refusals import INEXACT_SUM, TOO_MANY_DIGITS
from tests._fx_stubs import rule115_rates as _rule115_rates
from tests._no_echo import assert_no_markers
from tests._robinhood_account_inputs import accounts_pair as _pair
from tests._robinhood_account_inputs import security_config as _security_config
from tests._robinhood_account_inputs import statement_bundle as _bundle
from tests._robinhood_account_inputs import statement_row as _row
from tests._robinhood_cg import FILING_LTCG_KIND, FILING_LTCG_ROUTE
from tests._robinhood_cg_inputs import CG_CUSIP as _CUSIP
from tests._robinhood_cg_inputs import CG_SYMBOL as _SYMBOL
from tests._robinhood_cg_inputs import cg_trade as _trade
from tests._robinhood_cli import pipeline_run_kwargs_to_out as _run_kwargs
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_income_inputs import shared_user_config as _shared_config
from tests._robinhood_pipeline_stubs import stand_in_bundle as _pipeline_bundle
from tests._robinhood_security import configured_securities
from tests._schedule_cg_filing_workbook import values_by_field as _values_by_field

_BUY = RobinhoodEventKind.MANAGED_EQUITY_BUY
_SELL = RobinhoodEventKind.MANAGED_EQUITY_SELL
# The statements span every trade here, from the earliest buy to the sale after FY 2025-26.
_START, _OPENING, _END = date(2023, 4, 1), date(2023, 4, 15), date(2026, 6, 30)


def _buy(row: int, day: date, quantity: str, amount: str) -> RobinhoodEvent:
    return _trade(_BUY, row, day, quantity, "3.33", f"-{amount}")


def _sell(row: int, day: date, quantity: str, amount: str) -> RobinhoodEvent:
    return _trade(_SELL, row, day, quantity, "3.33", amount)


def _statement_row(trade: RobinhoodEvent) -> RobinhoodStatementActivity:
    """``trade``'s statement row, at its activity report row number."""
    assert trade.amount_usd is not None
    buy = trade.kind is _BUY
    return _row(
        row=trade.source_record_index,
        code=trade.trans_code,
        transaction_date=trade.activity_date,
        symbol=trade.instrument,
        cusip=_CUSIP,
        quantity=trade.quantity,
        price=trade.price_usd,
        # copy_abs, not unary minus, which would round a long Amount to 28 digits.
        debit=trade.amount_usd.copy_abs() if buy else None,
        credit=None if buy else trade.amount_usd,
    )


def _load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *trades: RobinhoodEvent,
    symbol: str = _SYMBOL,
) -> SimpleNamespace:
    """Make every command load ``_pair()``'s two accounts, reconciled, with the
    managed one making ``trades`` in ``symbol``; return the bundle.

    The management fee is dropped: the empty source income couldn't own it.
    """
    bundles, reports, accounts = _pair()
    accounts = tuple(replace(account, opening_date=_OPENING) for account in accounts)
    individual, managed = (item.statements[0] for item in bundles)
    managed = replace(
        managed,
        activities=(
            *(row for row in managed.activities if row.transaction_code != "ADVFEE"),
            *map(_statement_row, trades),
        ),
    )
    fee = RobinhoodEventKind.MANAGEMENT_FEE
    events = (*(event for event in reports[1].events if event.kind is not fee), *trades)
    paired = reconcile_robinhood_accounts(
        tuple(
            _bundle(replace(statement, period_start=_START, period_end=_END))
            for statement in (individual, managed)
        ),
        (
            replace(reports[0], start_date=_START, end_date=_END),
            replace(reports[1], events=events, start_date=_START, end_date=_END),
        ),
        accounts,
    )
    config = RobinhoodConfig(accounts=accounts, securities=(_security_config(symbol=symbol),))
    bundle = _pipeline_bundle()
    bundle.paired = paired
    bundle.packets = ()
    bundle.robinhood = config
    bundle.securities = configured_securities(config)
    bundle.identities = resolve_robinhood_security_identities(paired, config.securities)
    bundle.config = _shared_config(tmp_path)
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    return bundle


# ---- the issue's repros ----
# robinhood-cg can't run the CG repros' FY 2024-25 (no AY 2025-26 FX citations),
# so they run one year later; the FSI repro is the issue's own FY 2025-26.


@pytest.mark.parametrize(
    ("trades", "rates", "filed"),
    [
        pytest.param(
            (
                _buy(3, date(2026, 1, 10), "3.0000", "10.00"),
                _sell(4, date(2026, 2, 15), "1.0000", "3.00"),
                _sell(5, date(2026, 5, 15), "2.0000", "10.00"),
            ),
            {date(2026, 1, 31): Decimal("84.15")},
            (252, 281),
            id="cost-two-fys",
        ),
        pytest.param(
            (
                _buy(3, date(2026, 1, 10), "3.0000", "10.00"),
                _sell(4, date(2026, 2, 15), "1.0000", "3.00"),
                _sell(5, date(2026, 3, 20), "2.0000", "6.00"),
            ),
            {date(2026, 1, 31): Decimal("84.25"), date(2026, 2, 28): Decimal("84.10")},
            (757, 842),
            id="cost-two-rates",
        ),
    ],
)
def test_robinhood_cg_files_the_exact_cost_of_partial_sales(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trades: tuple[RobinhoodEvent, ...],
    rates: dict[date, Decimal],
    filed: tuple[int, int],
) -> None:
    """#1241: the in-FY sales of a $10.00 buy of 3 shares cost exactly $10/3
    at 84.15 (₹280.50), or $10/3 at 84.25 plus $20/3 at 84.10 (₹841.50), and
    file ₹281 and ₹842, not a rounded share's ₹280 and ₹841. The second repro
    sells 2 for $6.00. Each sale's Amount is its consideration (#1383): $3.00
    at 84.15 files ₹252, and $3.00 at 84.25 plus $6.00 at 84.10 files ₹757.
    The section is a loss, so the rest of its fields are 0 or
    portal-calculated, and hidden."""
    _rule115_rates(monkeypatch, overrides=rates)
    _load(monkeypatch, tmp_path, *trades)

    assert _main("robinhood-cg", tmp_path) == 0

    consideration, cost = filed
    assert _values_by_field(tmp_path / "out" / "schedule-cg.xlsx", "ITR-2 A5 STCG") == {
        "A5(a)(ii)": consideration,
        "A5(b)(i)": cost,
    }


def test_robinhood_cg_audit_writes_the_exact_ltcg_proceeds_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1241: a $10.00 sale of 3 shares, 1 long-term, gives the LTCG slice
    exactly $10/3, ₹280.50 at 84.15, which the LTCG debug audit keeps at full
    precision. The 2 short-term shares cost $8.00. Both sections are filed
    from the Amount (#1383), so the run exits 0."""
    _rule115_rates(monkeypatch, overrides={date(2026, 1, 31): Decimal("84.15")})
    _load(
        monkeypatch,
        tmp_path,
        _buy(3, date(2023, 12, 1), "1.0000", "4.00"),
        _buy(4, date(2026, 1, 10), "2.0000", "8.00"),
        _sell(5, date(2026, 2, 15), "3.0000", "10.00"),
    )

    assert _main("robinhood-cg", tmp_path, "--debug") == 0

    audit = tmp_path / "out" / "schedule-cg-ltcg-debug.csv"
    with audit.open(encoding="utf-8-sig", newline="") as handle:
        header, line, *_rest = csv.reader(handle)
    assert dict(zip(header, line, strict=True))["_audit: Proceeds INR (full precision)"] == "280.5"


def test_robinhood_income_fsi_capital_gains_equal_the_exact_cg_balance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1241: in FY 2025-26 the May slice of a $10.00 buy of 3 shares costs
    exactly $20/3, so its $10.00 sale gains ₹280.50 at 84.15. FSI capital-gains
    income is that ₹280.5 and files ₹281, the Schedule CG balance. The check
    reads the rows the command computed; it writes them with Schedule OS, TR
    and Form 67 and exits ok (#1383)."""
    _rule115_rates(monkeypatch, overrides={date(2025, 4, 30): Decimal("84.15")})
    _load(
        monkeypatch,
        tmp_path,
        _buy(3, date(2025, 1, 10), "3.0000", "10.00"),
        _sell(4, date(2025, 2, 15), "1.0000", "3.00"),
        _sell(5, date(2025, 5, 15), "2.0000", "10.00"),
    )
    empty = RobinhoodIncomeResult(**{field.name: () for field in fields(RobinhoodIncomeResult)})
    monkeypatch.setattr(core, "compute_source_income", lambda *_args: empty)
    computed: list[tuple[Any, Any]] = []

    def compute_income(*args: Any, **kwargs: Any) -> Any:
        result = core.compute_income(*args, **kwargs)
        computed.append((args[2], result[1]))
        return result

    monkeypatch.setattr(pipeline, "_compute_income", compute_income)

    report = run_robinhood_income(**_run_kwargs(tmp_path))

    [(cg, assembled)] = computed
    [gains] = (
        row for row in assembled.fsi_result.rows if row.head_of_income is HeadOfIncome.CAPITAL_GAINS
    )
    assert gains.income_inr == Decimal("280.5")
    assert to_whole_rupees(gains.income_inr) == cg.stcg.portal_amounts.balance_inr == 281
    assert (report.status, report.reasons) == (RobinhoodLegStatus.OK, ())
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == [
        "form-67.csv",
        "schedule-fsi.csv",
        "schedule-os.xlsx",
        "schedule-tr.csv",
    ]


# ---- a third partial sale of same-day lots (#1326) ----

# Three same-day buys of 1 share for $10.00, at activity rows 3 to 5.
_SAME_DAY_BUYS = tuple(
    _trade(_BUY, row, date(2026, 1, 10), "1.0000", "10.00", "-10.00") for row in (3, 4, 5)
)


@pytest.mark.parametrize(
    "sales",
    [
        pytest.param(
            (("1.0000", "9.00"), ("0.5000", "4.50"), ("0.5000", "4.50"), ("1.0000", "9.00")),
            id="third-partial-sale",
        ),
        pytest.param(
            (("1.0000", "9.00"), ("0.5000", "4.50"), ("1.5000", "13.50")),
            id="third-sale-takes-the-rest",
        ),
    ],
)
@pytest.mark.parametrize(
    ("itr_form", "sheet", "line"),
    [("ITR-2", "ITR-2 A5 STCG", "A5"), ("ITR-3", "ITR-3 A6 STCG", "A6")],
)
def test_robinhood_cg_files_a_third_partial_sale_of_same_day_lots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sales: tuple[tuple[str, str], ...],
    itr_form: str,
    sheet: str,
    line: str,
) -> None:
    """#1326: the three same-day $10.00 lots are sold at $9.00 a share, 1, 0.5,
    0.5 and 1 share on 2026-02-16 to 19. After the second sale each lot holds
    exactly $5, but the Decimal costs differ in the 20th digit, so main gave
    the third sale a same-day blocker and refused (exit 4). The exact unit
    costs match, so it splits pro rata, and the whole $30 files at 84.15 as
    ₹2,525 beside the $27.00 of Amounts, ₹2,272 (exit 0; #1383). The control
    sells the last 1.5 at once, which the unit-cost test never sees, and files
    the same."""
    _rule115_rates(monkeypatch, overrides={date(2026, 1, 31): Decimal("84.15")})
    _load(
        monkeypatch,
        tmp_path,
        *_SAME_DAY_BUYS,
        *(
            _trade(_SELL, 6 + index, date(2026, 2, 16 + index), quantity, "9.00", amount)
            for index, (quantity, amount) in enumerate(sales)
        ),
    )

    assert _main("robinhood-cg", tmp_path, "--itr-form", itr_form) == 0

    assert _values_by_field(tmp_path / "out" / "schedule-cg.xlsx", sheet) == {
        f"{line}(a)(ii)": 2272,
        f"{line}(b)(i)": 2525,
    }


# ---- #1360: a positive STCG writes the workbook; #1383 files its Table F band ----

# A digit run that a sale's Amount and its lot's cost both carry.
_AMOUNT_MARKER = "87654321"


def test_robinhood_cg_writes_a_positive_stcg_with_its_table_f_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1360: 2 shares bought for $8.00 on 2026-01-10 and sold for $10.00 on
    2026-02-15 gain ₹168.30 at 84.15. Before #1360 that positive STCG refused
    for the item F breakdown (exit 4, no file). The Amount is its consideration
    (#1383), so the section files ₹842 less ₹673, and Table F band (iv), the
    only one with a gain, takes the ₹169 result by rule (c): exit 0. On
    a5246a9 the section was manual and its bands blank (exit 3)."""
    _rule115_rates(monkeypatch, overrides={date(2026, 1, 31): Decimal("84.15")})
    _load(
        monkeypatch,
        tmp_path,
        _buy(3, date(2026, 1, 10), "2.0000", "8.00"),
        _sell(4, date(2026, 2, 15), "2.0000", "10.00"),
    )

    assert _main("robinhood-cg", tmp_path) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Action:" not in captured.out
    assert _values_by_field(tmp_path / "out" / "schedule-cg.xlsx", "ITR-2 A5 STCG") == {
        "A5(a)(ii)": 842,
        "A5(b)(i)": 673,
        "F3(iv)": 169,
    }


@pytest.mark.parametrize(
    ("bought", "code", "stderr"),
    [
        (date(2026, 1, 10), 0, ""),
        (
            date(2023, 6, 1),
            4,
            f"error: InvariantError: Schedule CG filing is blocked: {FILING_LTCG_KIND}\n"
            f"{FILING_LTCG_ROUTE} (context keys: reasons)\n",
        ),
    ],
    ids=["stcg-written", "ltcg-section-54f-refusal"],
)
def test_a_positive_gain_run_prints_no_amount_digits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bought: date,
    code: int,
    stderr: str,
) -> None:
    """#1360: a sale whose Amount and lot cost carry a digit run, for a
    positive short-term gain the workbook now takes, or a long-term one that
    still refuses for section 54F, prints neither on stdout nor stderr."""
    _rule115_rates(monkeypatch, overrides={date(2026, 1, 31): Decimal("84.15")})
    _load(
        monkeypatch,
        tmp_path,
        _buy(3, bought, "2.0000", f"{_AMOUNT_MARKER}.00"),
        _sell(4, date(2026, 2, 15), "2.0000", f"1{_AMOUNT_MARKER}.00"),
    )

    assert _main("robinhood-cg", tmp_path) == code

    captured = capsys.readouterr()
    assert captured.err == stderr
    assert_no_markers(captured.out + captured.err, _AMOUNT_MARKER)


# ---- the digit-cap refusal ----

# Synthetic markers: none may reach the refusal, its chain or the CLI output.
_DESCRIPTION = "SYNTHSHAREDESC"
_TICKER = "ZQXS"
_DIGIT_RUN = "736251"
_MARKERS = (_DESCRIPTION, _TICKER, _DIGIT_RUN)
# 1002 digits before the point, past EXACT_SUM_DIGITS (1000).
_LONG_AMOUNT = _DIGIT_RUN * 167 + ".00"
_SHARE_REFUSAL = (
    "InvariantError: Robinhood lot allocation, activity row {row}, column Amount: the share "
    + TOO_MANY_DIGITS
)
# Each case sells every share it buys: the statements show no closing position.
_LONG_SHARES = [
    # Row 4 sells 1 of the 3 shares row 3 bought for the long Amount; row 5 sells the rest.
    pytest.param(
        (
            _buy(3, date(2025, 6, 10), "3.0000", _LONG_AMOUNT),
            _sell(4, date(2025, 7, 15), "1.0000", "736251.00"),
            _sell(5, date(2025, 8, 15), "2.0000", "6.00"),
        ),
        4,
        id="lot-cost",
    ),
    # Row 5 sells both lots for the long Amount, so its cash is split between them.
    pytest.param(
        (
            _buy(3, date(2025, 6, 10), "1.0000", "3.00"),
            _buy(4, date(2025, 6, 11), "2.0000", "7.00"),
            _sell(5, date(2025, 7, 15), "3.0000", _LONG_AMOUNT),
        ),
        5,
        id="sale-amount",
    ),
]


def _load_marked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, trades: tuple[RobinhoodEvent, ...]
) -> SimpleNamespace:
    """:func:`_load` with every trade in ``_TICKER`` and described ``_DESCRIPTION``."""
    marked = (replace(trade, instrument=_TICKER, description=_DESCRIPTION) for trade in trades)
    return _load(monkeypatch, tmp_path, *marked, symbol=_TICKER)


@pytest.mark.parametrize(("trades", "row"), _LONG_SHARES)
def test_robinhood_cg_refuses_a_share_past_the_digit_cap_and_leaves_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    trades: tuple[RobinhoodEvent, ...],
    row: int,
) -> None:
    """#1241: a partial sale whose lot cost or sale Amount has 1002 digits
    refuses robinhood-cg with exit 4 and the fixed refusal naming the sale's
    row. No CG output remains, an earlier run's included, and no trade marker
    is printed."""
    _load_marked(monkeypatch, tmp_path, trades)
    output_dir = tmp_path / "out"
    output_dir.mkdir(mode=0o700)
    for name in CG_FILENAMES:
        (output_dir / name).write_text("previous run", encoding="utf-8")

    assert _main("robinhood-cg", tmp_path) == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: {_SHARE_REFUSAL.format(row=row)}\n"
    assert captured.out == ""
    assert list(output_dir.iterdir()) == []
    assert_no_markers(captured.out + captured.err, *_MARKERS)


@pytest.mark.parametrize(("trades", "row"), _LONG_SHARES)
def test_digit_cap_refusal_keeps_the_trade_out_of_the_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trades: tuple[RobinhoodEvent, ...],
    row: int,
) -> None:
    """#1241: the refusal's text, repr, args and chain hold none of the
    refusing sale's description, symbol or Amount digits, and it chains no
    exception."""
    bundle = _load_marked(monkeypatch, tmp_path, trades)
    [sale] = (
        event
        for account in bundle.paired.accounts
        for event in account.evidence.events
        if event.kind is _SELL and event.source_record_index == row
    )
    # Each marker is in the run's input.
    assert (sale.description, sale.instrument) == (_DESCRIPTION, _TICKER)
    assert _DIGIT_RUN in str(sale.amount_usd)

    with pytest.raises(InvariantError) as raised:
        run_robinhood_cg(**_run_kwargs(tmp_path))

    assert str(raised.value) == _SHARE_REFUSAL.format(row=row)
    assert_replaced_unchained(raised.value, markers=_MARKERS)


def test_an_unsplit_sale_past_the_digit_cap_keeps_the_line_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1241: a sale that takes one whole lot has no share to split, so its
    long Amount still refuses at the Schedule CG line, as on main (#1184)."""
    _load_marked(
        monkeypatch,
        tmp_path,
        (
            _buy(3, date(2025, 6, 10), "1.0000", "3.00"),
            _sell(4, date(2025, 7, 15), "1.0000", _LONG_AMOUNT),
        ),
    )

    with pytest.raises(InvariantError) as raised:
        run_robinhood_cg(**_run_kwargs(tmp_path))

    assert str(raised.value) == (
        f"InvariantError: Schedule CG STCG, input lot 1, column realized_gain_inr: {INEXACT_SUM}"
    )
