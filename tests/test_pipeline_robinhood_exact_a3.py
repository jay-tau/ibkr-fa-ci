"""Exact Robinhood Schedule FA A3 shares through robinhood-fa and robinhood-all (#1243).

Each run reconciles a synthetic two-account bundle and runs the real option,
price-coverage, source-income, Schedule FA and writer steps. Each account has
a CY 2025 statement and an empty Q1 2026 one, so its evidence covers both
CY 2025 and FY 2025-26. Only the bundle load, the Schedule FA TTBR table
(84.15 every day of CY 2025 by default) and the Yahoo closes ($15 every
session by default) are stubbed.
"""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from ibkr_fa.parser.robinhood import RobinhoodEvent
from ibkr_fa.pipeline_robinhood import RobinhoodPipelineError, run_robinhood_fa
from tests._crash_reports import assert_replaced_unchained
from tests._no_echo import assert_no_markers
from tests._robinhood_a3_exact import (
    DESCRIPTION,
    DIGIT_RUN,
    MARKERS,
    SYMBOL,
    TINY_QUANTITY,
)
from tests._robinhood_a3_load import load_two_accounts as _load
from tests._robinhood_cli import pipeline_run_kwargs_to_out as _run_kwargs
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_cli import stale_fa_outputs as _stale_fa_outputs
from tests._robinhood_fa_blocker_lines import BY_HAND, CREDIT_CAUSE, REFERENCES
from tests._robinhood_fa_blocker_lines import FA_UNRESOLVED as _FA
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_fa_inputs import fa_dividend as _dividend
from tests._robinhood_fa_inputs import fa_sale as _sale
from tests._robinhood_fa_inputs import fa_same_day_sale as _same_day_sale
from tests._robinhood_refusals import ANCHOR_DRIFT_CAUSE


def _later(event: RobinhoodEvent, row: int, day: date) -> RobinhoodEvent:
    """``event`` at activity report row ``row`` on ``day``."""
    return replace(
        event, source_record_index=row, activity_date=day, process_date=day, settle_date=day
    )


def test_robinhood_fa_files_the_exact_initial_value_of_a_partly_sold_lot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1243's repro: the 1 share sold in June of a $10.00 buy of 3 costs
    exactly $10/3, ₹280.50 at 84.15, and files Initial value ₹281 (main ₹280).
    The 2 open shares file ₹561 and close at ₹2,525 as before. The sale
    Amount files as proceeds (#1383), so the run exits 0."""
    _load(
        monkeypatch,
        tmp_path,
        replace(_buy("3"), price_usd=Decimal("3.33"), amount_usd=Decimal("-10.00")),
        replace(_sale(), price_usd=Decimal("5.00"), amount_usd=Decimal("5.00")),
    )

    assert _main("robinhood-fa", tmp_path, "--no-fetch-prices", "--format", "filing-csv") == 0

    assert capsys.readouterr().err == ""
    with (tmp_path / "out" / "schedule-fa-a3.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [(row["Initial value of the investment"], row["Closing balance"]) for row in rows] == [
        ("561", "2525"),
        ("281", "0"),
    ]


def test_robinhood_fa_files_the_exact_closing_of_lots_split_by_a_same_day_sale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1263's repro: three same-day 1-share lots each give up a third of the
    1 share sold in June, and the Dec-31 statement shows the other 2 at $1.70,
    $3.40. At ₹82.50 each lot's exact closing and peak are $3.40/3, ₹93.50, so
    every held lot files Closing balance and Peak ₹94 (main filed ₹93 for the
    lot left with 0.66666666666666666666 of a share). The sale Amount files as
    proceeds (#1383), so the run exits 0."""
    _load(monkeypatch, tmp_path, *_same_day_sale(), price="1.70", rate="82.50")

    assert _main("robinhood-fa", tmp_path, "--no-fetch-prices", "--format", "filing-csv") == 0

    assert capsys.readouterr().err == ""
    with (tmp_path / "out" / "schedule-fa-a3.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    # Each lot's held 2/3, then its sold third.
    assert [
        (row["Closing balance"], row["Peak value of investment during the Period"]) for row in rows
    ] == [("94", "94"), ("0", "47")] * 3


def test_robinhood_fa_files_a_sale_amount_as_a3_and_a2_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1383: robinhood-fa --format filing-csv files a June sale's recorded
    $14.99 Amount as its A3 row's gross proceeds and the account's A2 ``S``
    row: $14.99 x ₹80 = ₹1,199.20, filed ₹1,199. Nothing is left to fill by
    hand, so the run exits 0. Main left both blank and exited 3."""
    _load(monkeypatch, tmp_path, _buy("3"), _sale(), rate="80")

    assert _main("robinhood-fa", tmp_path, "--no-fetch-prices", "--format", "filing-csv") == 0

    assert capsys.readouterr().err == ""
    out = tmp_path / "out"
    with (out / "schedule-fa-a3.csv").open(encoding="utf-8", newline="") as handle:
        a3 = list(csv.DictReader(handle))
    assert [
        row["Total gross proceeds from sale or redemption of investment during the period"]
        for row in a3
    ] == ["0", "1199"]
    with (out / "schedule-fa-a2.csv").open(encoding="utf-8", newline="") as handle:
        a2 = list(csv.DictReader(handle))
    assert [
        (row["Nature of Amount"], row["Amount"])
        for row in a2
        if row["Account Number"] == "111111111"
    ] == [("S", "1199")]


# ---- the digit-cap refusals ----

# A lot of 1E-1001 shares, a place past the digit cap, bought for an Amount
# holding the marked digit run.
_TINY_BUY = replace(
    _buy("1"), quantity=Decimal(TINY_QUANTITY), amount_usd=Decimal(f"-{DIGIT_RUN}.00")
)
# Fragment and statement quantities are 60-digit sums, so only the tiny lot's
# own quantity is past the cap. Since #1267 these runs refused first at A2:
# the exact equity ledger adding the tiny lot to the other lot's shares needs
# more digits than the cap, so A2 refused before A3 read either share. Since
# #1574 the lot engine's anchor drift the tiny lot leaves (the statement's 2
# shares against the replayed 2 and a tiny part), and in the credit case the
# dividend's split, refuse first: they need no price, and A2 reads prices.
# The A3 share refusals are tested with an A2 stand-in in
# test_compute_robinhood_fa.
_BY_HAND = f"{BY_HAND}."
_ANCHOR_DRIFT = (
    f"{ANCHOR_DRIFT_CAUSE.format(moved='sales and transfers')} {REFERENCES}account_index 0 "
    f"statement 1 (page 1 row 1). {_BY_HAND}"
)
_CREDIT = f"{CREDIT_CAUSE} {REFERENCES}Robinhood security 1. {_BY_HAND}"
_DIGIT_CAP = [
    # The tiny lot is the earlier row, so its closing share is a non-last share
    # and reads its quantity.
    pytest.param(
        (_TINY_BUY, _later(_buy("2"), 5, date(2025, 1, 20))),
        Decimal(),
        f"{_FA}\n{_ANCHOR_DRIFT}",
        id="closing",
    ),
    # The tiny lot is the later row: its closing share is the exact rest, 0,
    # which blocks its row, and the $10 dividend both lots hold is then split.
    pytest.param(
        (_buy("2"), _later(_TINY_BUY, 5, date(2025, 2, 20)), _dividend()),
        Decimal(10),
        f"{_FA}\n{_CREDIT}\n{_ANCHOR_DRIFT}",
        id="credit",
    ),
]


def _assert_marked(bundle: SimpleNamespace) -> None:
    """Each marker is in the run's input."""
    events = [
        event
        for account in bundle.paired.accounts
        for event in account.evidence.events
        if event.instrument is not None
    ]
    assert {(event.instrument, event.description) for event in events} == {(SYMBOL, DESCRIPTION)}
    assert any(DIGIT_RUN in str(event.amount_usd) for event in events)


@pytest.mark.parametrize(("events", "dividends", "refusal"), _DIGIT_CAP)
def test_robinhood_fa_refuses_a_share_past_the_digit_cap_and_leaves_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    events: tuple[RobinhoodEvent, ...],
    dividends: Decimal,
    refusal: str,
) -> None:
    """#1243, #1267, #1574: a lot quantity past the cap, which a closing or
    credit share would read, refuses robinhood-fa with exit 4 and the
    blockers it leaves alone. No FA output remains, an earlier run's
    included, and no marker is printed. Main refused both at the A3 share,
    and 57af529 at A2."""
    _assert_marked(_load(monkeypatch, tmp_path, *events, dividends=dividends))
    _stale_fa_outputs(tmp_path / "out")

    assert _main("robinhood-fa", tmp_path, "--no-fetch-prices") == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: {refusal}\n"
    assert captured.out == ""
    assert list((tmp_path / "out").iterdir()) == []
    assert_no_markers(captured.out + captured.err, *MARKERS)


@pytest.mark.parametrize(("events", "dividends", "refusal"), _DIGIT_CAP)
def test_robinhood_all_reports_the_digit_cap_refusal_and_leaves_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    events: tuple[RobinhoodEvent, ...],
    dividends: Decimal,
    refusal: str,
) -> None:
    """#1243, #1267, #1574: in robinhood-all the same blockers fail the
    schedule-fa leg, the run exits 4 and removes every output, an earlier
    run's FA outputs included, and no marker is printed."""
    _assert_marked(_load(monkeypatch, tmp_path, *events, dividends=dividends))
    _stale_fa_outputs(tmp_path / "out")

    assert _main("robinhood-all", tmp_path, "--no-fetch-prices") == 4

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    [leg] = [index for index, line in enumerate(lines) if line.split() == ["error", "schedule-fa"]]
    first, *rest = refusal.splitlines()
    assert [line.strip() for line in lines[leg + 1 : leg + 2 + len(rest)]] == [f"-> {first}", *rest]
    assert captured.err == ""
    assert list((tmp_path / "out").iterdir()) == []
    assert_no_markers(captured.out + captured.err, *MARKERS)


@pytest.mark.parametrize(("events", "dividends", "refusal"), _DIGIT_CAP)
def test_digit_cap_refusal_keeps_the_input_out_of_the_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: tuple[RobinhoodEvent, ...],
    dividends: Decimal,
    refusal: str,
) -> None:
    """#1243, #1267, #1574: run_robinhood_fa raises the blockers' plain
    refusal, with no context: its text, repr, args and chain hold none of the
    run's description, symbol or Amount digits, and it chains no exception,
    suppressed or not."""
    _assert_marked(_load(monkeypatch, tmp_path, *events, dividends=dividends))

    with pytest.raises(RobinhoodPipelineError) as raised:
        run_robinhood_fa(**_run_kwargs(tmp_path), fetch_prices=False)

    assert type(raised.value) is RobinhoodPipelineError
    assert str(raised.value) == refusal
    assert_replaced_unchained(raised.value, markers=MARKERS)
