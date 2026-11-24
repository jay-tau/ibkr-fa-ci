"""#1476: Robinhood Schedule FA says why a Table A3 peak is blank and what clears it.

``robinhood-fa`` and ``robinhood-all`` printed the bare action ``a3-peak``.
Each blank-peak reason a Robinhood run reaches now prints its cause and
remedy, once however many rows it blanks, and names those Table A3 rows by
their 1-based position in the A3 file (#1560; #1554 named lots).

The bundle is synthetic (Robinhood statements are PDFs), as in
``test_cli_price_cache_layout_robinhood_cma``: ``_load`` returns a reconciled
synthetic account holding one configured equity. Price loading, Schedule FA
computation and reporting run for real; ``robinhood-all`` keeps its writers
stubbed. The price source is a stub; nothing touches the network.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest
from openpyxl import load_workbook

import ibkr_fa.pipeline_robinhood as robinhood_pipeline
import ibkr_fa.pipeline_robinhood_equity_prices as robinhood_equity_prices
import ibkr_fa.pipeline_robinhood_outputs as robinhood_outputs
import tests._robinhood_fa_inputs as fa_inputs
from ibkr_fa.cli import _build_parser, main
from ibkr_fa.compute.robinhood_fa import RobinhoodFaManualKind, RobinhoodFaManualWork
from ibkr_fa.config import load_user_config
from ibkr_fa.models import FaA3PeakBlankReason
from ibkr_fa.normalize.robinhood import (
    RobinhoodSecurityEventReference,
)
from ibkr_fa.output.fa_manual_fill import FA_A3_PEAK_VALUE_COLUMN
from ibkr_fa.parser.robinhood import RobinhoodEvent
from ibkr_fa.pipeline_robinhood_bundle import RobinhoodBundle
from ibkr_fa.pipeline_robinhood_reporting import fa_manual_reasons
from ibkr_fa.prices.cache import write_cache_lock
from ibkr_fa.prices.series import CONTEMPORANEOUS_BASIS, PriceSeries
from ibkr_fa.reference.us_market_calendar import us_market_sessions
from ibkr_fa.types import Ticker
from tests._cli_subcommands import subcommands as _commands
from tests._price_cache import price_source_called as _price_source_called
from tests._robinhood_fa_actions import peak_action
from tests._robinhood_fa_inputs import (
    FA_ISIN,
    FA_SYMBOL,
    fa_buy,
    fa_cash,
    fa_income,
    fa_paired_events,
    fa_sale,
    fa_statement,
)
from tests._robinhood_income_stubs import form67_assembly
from tests._robinhood_synthetic_fa import stub_all_but_fa, synthetic_bundle

_SYMBOL = FA_SYMBOL
_ISIN = FA_ISIN
_CONFIG = (
    '[account]\nholder_name = "Tax Payer"\n'
    # A complete identity, so the only manual Table A3 task is the peak.
    f'\n[[fa.entities]]\nisin = "{_ISIN}"\nname = "Synthetic Corporation"\n'
    'address = "One Synthetic Way"\nzip = "99999"\n'
    'nature_of_entity = "Listed company shares"\n'
)
_HIGH_BASIS = '\n[schedule_fa]\npeak_basis = "high"\n'
# Every session the price source returns: the loader asks from ten days
# before 1 January 2025 to 31 December.
_SESSIONS = us_market_sessions(date(2024, 12, 22), date(2025, 12, 31))
_PRICE = Decimal("15")
# The filed peak once it is valued: two shares at the flat $15 close, at the
# 85.48 TTBR of 10 January 2025, the day they were bought.
_FILLED_PEAK = "2564"
# A US session with no TTBR published within two days before it: the last
# rate before 1 April 2025 is 28 March's.
_NO_TTBR_DAY = date(2025, 4, 1)


def _series(
    *, highs: bool, closes: Mapping[date, Decimal] | None = None
) -> Callable[..., PriceSeries]:
    """A stub price source: every session closes at ``_PRICE`` unless
    ``closes`` says otherwise, with a high equal to each close or no highs."""

    def fetch(
        ticker: Ticker, *, start: date, end: date, now: datetime, fetch_symbol: str | None = None
    ) -> PriceSeries:
        prices = {day: (closes or {}).get(day, _PRICE) for day in _SESSIONS}
        return PriceSeries(
            ticker=ticker,
            currency="USD",
            closes=prices,
            highs=dict(prices) if highs else {},
            has_split_in_range=False,
            source="synthetic",
            fetched_at=datetime(2026, 1, 2),
            price_basis=CONTEMPORANEOUS_BASIS,
        )

    return fetch


def _deposit(day: date, amount: str) -> RobinhoodEvent:
    return replace(
        fa_cash(),
        source_record_index=5,
        activity_date=day,
        process_date=day,
        settle_date=day,
        amount_usd=Decimal(amount),
    )


def _same_day_sale() -> RobinhoodEvent:
    buy = fa_buy()
    return replace(
        fa_sale(),
        activity_date=buy.activity_date,
        process_date=buy.activity_date,
        settle_date=buy.activity_date,
        quantity=Decimal("2"),
        amount_usd=Decimal("29.98"),
    )


class _Case(NamedTuple):
    """One blank-peak reason a Robinhood run reaches, and how."""

    config: str
    fetch: Callable[..., PriceSeries]
    events: tuple[RobinhoodEvent, ...] = (fa_buy(),)
    # Shares held on 31 December, and the statement's price for them.
    held: Decimal = Decimal("2")
    statement_price: Decimal = _PRICE


_CASES = {
    # A price cache with no intraday highs, under the high basis.
    FaA3PeakBlankReason.SESSION_WITHOUT_HIGH: _Case(_HIGH_BASIS, _series(highs=False)),
    # Every high equals its close, and the statement's closing value is
    # four tenths of a cent a share above the 31 December close, within the
    # half-cent cross-check.
    FaA3PeakBlankReason.HIGH_BELOW_CLOSING: _Case(
        _HIGH_BASIS, _series(highs=True), statement_price=Decimal("15.004")
    ),
    # Bought and sold the same day: held at the end of no day.
    FaA3PeakBlankReason.EMPTY_WINDOW: _Case(
        "", _series(highs=True), events=(fa_buy(), _same_day_sale()), held=Decimal()
    ),
    # The close peaks on a day with no TTBR within the carry limit; a later
    # deposit moves the account's A2 peak to a day with one.
    FaA3PeakBlankReason.NO_TTBR: _Case(
        "\n[fx]\nmax_carry_forward_days = 2\n",
        _series(highs=True, closes={_NO_TTBR_DAY: Decimal("20")}),
        events=(fa_buy(), _deposit(date(2025, 9, 2), "100")),
    ),
}
_REACHED = pytest.mark.parametrize("reason", list(_CASES), ids=lambda reason: reason.value)


def _bundle(config_path: Path, case: _Case) -> RobinhoodBundle:
    with pytest.MonkeyPatch.context() as patch:
        # Every statement prices the equity at the case's statement price.
        patch.setattr(fa_inputs, "fa_statement", partial(fa_statement, price=case.statement_price))
        paired = fa_paired_events(case.events, position_quantity=case.held)
    return synthetic_bundle(config_path, paired)


class _Run(NamedTuple):
    argv: list[str]
    config: Path
    cache: Path
    out: Path


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, case: _Case) -> _Run:
    """The argv of a ``command`` run over ``case``'s synthetic bundle. The
    bundle is rebuilt from the config file on every run."""
    # No dividend or interest, as in test_compute_robinhood_fa.
    income = fa_income(dividend=False)
    monkeypatch.setattr(robinhood_pipeline, "_compute_source_income", lambda *_args: income)
    if command == "robinhood-all":
        # The other legs and writers are stubbed, as in
        # test_cli_price_cache_layout_robinhood_cma; FA computation and reporting are real.
        stub_all_but_fa(monkeypatch, tmp_path / "out")
        monkeypatch.setattr(
            robinhood_pipeline,
            "_compute_income",
            lambda *_args, **_kwargs: (income, form67_assembly(), object()),
        )
    config = tmp_path / "user.toml"
    config.write_text(_CONFIG + case.config, encoding="utf-8")
    monkeypatch.setattr(
        robinhood_pipeline, "_load", lambda **kwargs: _bundle(kwargs["config_path"], case)
    )
    # The pipeline's own default fetcher wraps this one.
    monkeypatch.setattr(robinhood_equity_prices, "fetch_daily_closes_usd_retrying", case.fetch)
    cache = tmp_path / "cache"
    out = tmp_path / "out"
    out.mkdir(mode=0o700, exist_ok=True)  # Robinhood refuses an output folder others can read
    argv = [
        command,
        *("--input", str(tmp_path / "bundle"), "--config", str(config)),
        *("--output-dir", str(out), "--financial-year", "2025-26", "--itr-form", "ITR-2"),
        *("--price-cache-dir", str(cache)),
    ]
    return _Run(argv, config, cache, out)


def _peak_actions(command: str, stdout: str) -> list[str]:
    """The ``a3-peak`` actions ``command`` prints."""
    prefix = "  Action: " if command == "robinhood-fa" else " " * 17 + "-> "
    return re.findall(rf"^{prefix}(a3-peak.*)$", stdout, re.MULTILINE)


def _peaks(out: Path) -> list[str]:
    with (out / "schedule-fa-a3.csv").open(encoding="utf-8", newline="") as handle:
        return [row[FA_A3_PEAK_VALUE_COLUMN] for row in csv.DictReader(handle)]


_COMMANDS = pytest.mark.parametrize("command", ["robinhood-fa", "robinhood-all"])


@_REACHED
@_COMMANDS
def test_a_blank_peak_prints_its_cause_and_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    reason: FaA3PeakBlankReason,
) -> None:
    """On 9b6ff2f each run printed the bare action ``a3-peak``."""
    run = _setup(tmp_path, monkeypatch, command, _CASES[reason])

    assert main(run.argv) == 3

    stdout = capsys.readouterr().out
    # Each case's one holding is the A3 file's one row.
    assert _peak_actions(command, stdout) == [peak_action(reason, 1)]
    if command == "robinhood-fa":
        assert _peaks(run.out) == [""]


# Two holdings: one bought and sold on 10 January (activity report rows 2 and
# 4), held at the end of no day, and one bought on 21 January (row 6) and held
# at 31 December, when the statement values it above every high. They are A3
# rows 1 and 2.
_TWO_HOLDINGS = _Case(
    _HIGH_BASIS,
    _series(highs=True),
    events=(
        fa_buy(),
        _same_day_sale(),
        replace(
            fa_buy(),
            source_record_index=6,
            activity_date=date(2025, 1, 21),
            process_date=date(2025, 1, 21),
            settle_date=date(2025, 1, 21),
        ),
    ),
    statement_price=Decimal("15.004"),
)


@_COMMANDS
def test_each_reason_names_its_own_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """Two holdings whose peaks are blank for different reasons: each action
    names its own row. On 14b9fc5 neither named any; on fc442a8 each named
    its lot."""
    run = _setup(tmp_path, monkeypatch, command, _TWO_HOLDINGS)

    assert main(run.argv) == 3

    assert _peak_actions(command, capsys.readouterr().out) == [
        peak_action(FaA3PeakBlankReason.EMPTY_WINDOW, 1),
        peak_action(FaA3PeakBlankReason.HIGH_BELOW_CLOSING, 2),
    ]
    if command == "robinhood-fa":
        assert _peaks(run.out) == ["", ""]


# One buy of two shares on 10 January (activity report row 2): one share sold
# the same day (row 4), held at the end of no day, and one held at 31 December,
# when the statement values it above every high. The buy becomes two A3 rows,
# blank for different reasons; on fc442a8 both actions named lot 0:2.
_SPLIT = _Case(
    _HIGH_BASIS,
    _series(highs=True),
    events=(
        fa_buy(),
        replace(_same_day_sale(), quantity=Decimal("1"), amount_usd=Decimal("14.99")),
    ),
    held=Decimal("1"),
    statement_price=Decimal("15.004"),
)
_CLOSING = "Closing balance"
_PROCEEDS = "Total gross proceeds from sale or redemption of investment during the period"


def _a3_file(out: Path, format_id: str) -> list[dict[str, str]]:
    """The data rows of the A3 file a ``format_id`` run writes, in file order."""
    if format_id == "xlsx":
        workbook = load_workbook(out / "schedule-fa-a3.xlsx")
        try:
            header, *rows = workbook.worksheets[0].iter_rows(values_only=True)
        finally:
            workbook.close()
        return [
            {
                str(name): "" if cell is None else str(cell)
                for name, cell in zip(header, row, strict=True)
            }
            for row in rows
        ]
    with (out / "schedule-fa-a3.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.mark.parametrize(
    ("command", "format_id"),
    [
        ("robinhood-fa", "review-csv"),
        ("robinhood-fa", "filing-csv"),
        ("robinhood-fa", "xlsx"),
        ("robinhood-fa", "cbdt-json-fragment"),
        ("robinhood-all", "review-csv"),
        ("robinhood-all", "xlsx"),
    ],
)
def test_a_split_acquisition_names_each_part_s_own_row_in_every_a3_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    format_id: str,
) -> None:
    """Each action names the row the A3 file the run writes gives that part:
    the sold part's row has the sale's proceeds and no closing balance, the
    kept part's a closing balance and no proceeds, and both peaks are blank.
    A blank peak leaves no CBDT JSON fragment to write, so that format writes
    no file."""
    run = _setup(tmp_path, monkeypatch, command, _SPLIT)
    if command == "robinhood-all":
        # The real Schedule FA writer; the other legs' writers stay stubbed.
        monkeypatch.setattr(robinhood_pipeline, "_write_fa", robinhood_outputs.write_fa)

    assert main([*run.argv, "--format", format_id]) == 3

    assert _peak_actions(command, capsys.readouterr().out) == [
        peak_action(FaA3PeakBlankReason.EMPTY_WINDOW, 2),
        peak_action(FaA3PeakBlankReason.HIGH_BELOW_CLOSING, 1),
    ]
    if format_id == "cbdt-json-fragment":
        assert list(run.out.iterdir()) == []
        return
    rows = _a3_file(run.out, format_id)
    if format_id == "filing-csv":
        # The upload numbers its rows in their first cell, as the actions do.
        assert [next(iter(row.values())) for row in rows] == ["1", "2"]
    kept, sold = rows
    assert [row[FA_A3_PEAK_VALUE_COLUMN] for row in rows] == ["", ""]
    assert (kept[_PROCEEDS], sold[_CLOSING]) == ("0", "0")
    assert kept[_CLOSING] != "0"
    assert sold[_PROCEEDS] != "0"


def _moved_aside(cache_file: Path, aside: Path, *, locked: bool) -> None:
    """Follow the remedy's first route: move the holding's cache file, and the
    ``.lock`` beside it when there is one, out of the price cache directory."""
    aside.mkdir()
    lock = cache_file.with_name(f"{cache_file.name}.lock")
    for path in (cache_file, lock) if locked else (cache_file,):
        path.rename(aside / path.name)


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
@_COMMANDS
def test_moving_the_cache_file_aside_lets_a_fetch_fill_the_high_peak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    locked: bool,
) -> None:
    """The run caches a series without highs. Once highs are available a rerun
    still reuses that cache and leaves the peak blank; moved aside as the
    remedy says, the next online run fetches the highs and fills it."""
    reason = FaA3PeakBlankReason.SESSION_WITHOUT_HIGH
    run = _setup(tmp_path, monkeypatch, command, _CASES[reason])
    assert main(run.argv) == 3
    (cache_file,) = run.cache.glob("*.csv")
    if locked:
        write_cache_lock(cache_file)
    monkeypatch.setattr(
        robinhood_equity_prices, "fetch_daily_closes_usd_retrying", _series(highs=True)
    )
    capsys.readouterr()

    assert main(run.argv) == 3
    assert _peak_actions(command, capsys.readouterr().out) == [peak_action(reason, 1)]

    _moved_aside(cache_file, tmp_path / "aside", locked=locked)
    status = main(run.argv)

    assert status == 0
    assert _peak_actions(command, capsys.readouterr().out) == []
    if command == "robinhood-fa":
        assert _peaks(run.out) == [_FILLED_PEAK]


@_COMMANDS
def test_the_close_basis_the_remedy_names_fills_the_peak_from_the_same_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """The remedy's second route: the setting it names, written as printed,
    values the peak from the cached closes; nothing is fetched."""
    reason = FaA3PeakBlankReason.SESSION_WITHOUT_HIGH
    run = _setup(tmp_path, monkeypatch, command, _CASES[reason])
    assert main(run.argv) == 3
    (action,) = _peak_actions(command, capsys.readouterr().out)
    followed = re.search(r'set \[(\w+)\] (\w+ = "\w+")', action)
    assert followed is not None
    run.config.write_text(f"{_CONFIG}\n[{followed[1]}]\n{followed[2]}\n", encoding="utf-8")
    monkeypatch.setattr(
        robinhood_equity_prices, "fetch_daily_closes_usd_retrying", _price_source_called
    )

    status = main(run.argv)

    assert status == 0
    assert _peak_actions(command, capsys.readouterr().out) == []
    if command == "robinhood-fa":
        assert _peaks(run.out) == [_FILLED_PEAK]


def _blank_peaks(*reasons: FaA3PeakBlankReason) -> Any:
    """A Schedule FA result with one ``a3-peak`` task per lot, each lot's
    one A3 row's peak blank for the matching reason."""
    lots = [RobinhoodSecurityEventReference(0, ordinal) for ordinal in range(len(reasons))]
    a3 = tuple(
        SimpleNamespace(lot_id=lot, row=SimpleNamespace(peak_blank_reason=reason))
        for lot, reason in zip(lots, reasons, strict=True)
    )
    return SimpleNamespace(
        manual_work=tuple(
            RobinhoodFaManualWork(kind=RobinhoodFaManualKind.A3_PEAK, lot_ids=(lot,))
            for lot in lots
        ),
        a3=a3,
        a3_rows=tuple(review.row for review in a3),
    )


def test_each_reason_prints_once_however_many_rows_it_blanks() -> None:
    """Two rows blank for each reason; each reason's action prints once, in
    reason order, naming both rows. That includes the reasons a Robinhood run
    can't reach (listed in #1476's PR), so one never prints a bare kind."""
    reasons = tuple(FaA3PeakBlankReason)
    last = 2 * len(reasons)

    assert fa_manual_reasons(_blank_peaks(*reasons, *reversed(reasons))) == tuple(
        peak_action(reason, index, last + 1 - index)
        for index, reason in enumerate(reasons, start=1)
    )


def test_every_flag_and_setting_an_action_names_is_accepted_by_both_commands(
    tmp_path: Path,
) -> None:
    text = "\n".join(fa_manual_reasons(_blank_peaks(*FaA3PeakBlankReason)))
    flags = set(re.findall(r"--[a-z][a-z-]*", text))
    assert flags == {"--no-fetch-prices", "--price-cache-dir"}
    commands = _commands(_build_parser())
    for command in ("robinhood-fa", "robinhood-all"):
        assert flags <= commands[command]._option_string_actions.keys()

    assert set(re.findall(r"\[\[?[\w.]+\]\]?(?: \w+ = \"\w+\")?", text)) == {
        '[schedule_fa] peak_basis = "close"',
        "[[prices.ticker_overrides]]",
    }
    config = tmp_path / "user.toml"
    config.write_text(
        f'{_CONFIG}\n[schedule_fa]\npeak_basis = "close"\n'
        f'\n[[prices.ticker_overrides]]\nibkr_ticker = "{_SYMBOL}"\nisin = "{_ISIN}"\n'
        'yahoo_symbol = "XQAB"\n',
        encoding="utf-8",
    )
    loaded = load_user_config(config)
    assert loaded.schedule_fa.peak_basis == "close"
    assert [(item.ibkr_ticker, item.yahoo_symbol) for item in loaded.prices.ticker_overrides] == [
        (_SYMBOL, "XQAB")
    ]
