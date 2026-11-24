"""#1574: ``robinhood-fa`` and ``robinhood-all``'s FA leg name each Schedule FA
blocker their compute decides without prices before they read any price.

On 57af529 a run whose price step failed named only that step: here a held
security with no cached closes under ``--no-fetch-prices``. Once the filer had
fixed the prices, the rerun refused for a blocker the first run could already
have named, and most of those send the whole Robinhood Schedule FA to be
completed by hand. Each case gives one such blocker and an empty price cache,
using the inputs of ``tests/_robinhood_fa_blockers.py``. The first
run names the blocker and reads no price. Following its text, then fixing the
prices, reaches exit 0 or exactly the stop it named.

The a3-identity case is followed in ``tests/test_robinhood_fa_first_refusals.py``.
Option-classification stops came before the prices already (#1567).
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
import ibkr_fa.pipeline_robinhood_core as core
import ibkr_fa.pipeline_robinhood_equity_prices as equity_prices
from ibkr_fa.compute.robinhood_fa import RobinhoodFaBlocker, RobinhoodFaBlockerKind
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionBlockerKind,
    option_blocker_summary,
)
from ibkr_fa.normalize.robinhood.security import RobinhoodSecurityEventReference
from ibkr_fa.pipeline_robinhood_models import RobinhoodLegStatus
from tests._robinhood_cli import FINANCIAL_YEAR as _FY
from tests._robinhood_cli import run_robinhood_command
from tests._robinhood_fa_blocker_lines import FA_UNRESOLVED
from tests._robinhood_fa_blockers import (
    CREDIT_LINE,
    FA_BLOCKER_CASES,
    buy_at_row,
    december_statements,
    empty_price_cache,
    load_blocker_accounts,
    load_dec31_unresolved,
    resolved_bundle,
    schedule_fa_leg,
)
from tests._robinhood_income_stubs import (  # noqa: F401 - autouse fixture
    silenced_default_income_warnings as _silence_default_income_warnings,
)
from tests._robinhood_option_runs import blocked_options as _blocked_options
from tests._robinhood_pending import CLOSING_BLOCKED, DEC31_REPRO
from tests._robinhood_refusals import EQUITY_COVERAGE_REFUSAL
from tests._robinhood_security import identity_guidance_line

_COMMANDS = ("robinhood-fa", "robinhood-all")
# Kinds whose route is completing the whole Robinhood Schedule FA by hand.
_BY_HAND = ("a3-credit-attribution", "a3-lots-same-day", "a3-closing")


def _refusal(command: str, run_dir: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """robinhood-fa's exit-4 refusal, which prints nothing on stdout and
    leaves no file, or the reason robinhood-all's FA leg fails with."""
    if command == "robinhood-fa":
        code, out, err = run_robinhood_command(command, run_dir, capsys)
        assert (code, out) == (4, "")
        assert list((run_dir / "out").iterdir()) == []
        return err.removeprefix("error: ").removesuffix("\n")
    leg = schedule_fa_leg(run_dir)
    assert leg.status is RobinhoodLegStatus.ERROR
    (reason,) = leg.reasons
    return reason


@pytest.mark.parametrize("command", _COMMANDS)
@pytest.mark.parametrize("case", _BY_HAND)
def test_a_blocker_routed_by_hand_stops_the_first_run_before_any_price(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    command: str,
) -> None:
    """The first run names the blocker and reads no price. Its route has
    nothing to follow, and with the prices fixed the rerun names exactly the
    same stop."""
    load, refusal = FA_BLOCKER_CASES[case]
    load(monkeypatch, tmp_path)
    served = equity_prices.load_price_series
    reads = empty_price_cache(monkeypatch)

    assert _refusal(command, tmp_path, capsys) == refusal
    assert reads == []

    monkeypatch.setattr(equity_prices, "load_price_series", served)
    assert _refusal(command, tmp_path, capsys) == refusal


@pytest.mark.parametrize("command", _COMMANDS)
def test_following_the_statement_route_then_fixing_the_prices_writes_schedule_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """A statement spanning 31 December: the first run names a2-closing,
    evidence-coverage and the credits its Schedule OS blocker holds back,
    and reads no price. Supplying the statements that end on 31 December and
    start on 1 January, as the route says, and fixing the prices writes
    Schedule FA, its Table A3 identity resolved from GLEIF."""
    load, refusal = FA_BLOCKER_CASES["a2-closing-and-evidence-coverage"]
    load(monkeypatch, tmp_path)
    reads = empty_price_cache(monkeypatch)

    assert _refusal(command, tmp_path, capsys) == refusal
    assert reads == []

    supplied = load_blocker_accounts(
        monkeypatch, tmp_path, buy_at_row(2, "2", "10"), managed=december_statements
    )
    resolved_bundle(monkeypatch, tmp_path, supplied)
    if command == "robinhood-fa":
        assert run_robinhood_command(command, tmp_path, capsys)[::2] == (0, "")
        assert (tmp_path / "out" / "schedule-fa-a3.csv").is_file()
    else:
        leg = schedule_fa_leg(tmp_path)
        assert (leg.status, leg.reasons) == (RobinhoodLegStatus.OK, ())
        assert (tmp_path / "all" / "schedule-fa-a3.csv").is_file()


@pytest.mark.parametrize("command", _COMMANDS)
def test_a_closing_price_off_the_statement_still_stops_after_the_prices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """The a3-closing cause a price decides stays after the price step: a
    $20 close for 31 December values the 3 shares a third above the
    statement's total. With an empty price cache the first run names the
    cache, as on 57af529; with the closes served it names the closing line."""
    load_dec31_unresolved(monkeypatch, tmp_path, DEC31_REPRO, dec31_close=Decimal(20))
    served = equity_prices.load_price_series
    reads = empty_price_cache(monkeypatch)

    first = _refusal(command, tmp_path, capsys)
    assert first.startswith(f"{EQUITY_COVERAGE_REFUSAL} (0/1 securities")
    assert reads

    monkeypatch.setattr(equity_prices, "load_price_series", served)
    assert _refusal(command, tmp_path, capsys) == CLOSING_BLOCKED.format(lots="0:2, 0:5")


@pytest.mark.parametrize(
    "case",
    [
        *_BY_HAND,
        "a2-closing-and-evidence-coverage",
        "cash-only-income",
        "unbound-substitute-form-income",
    ],
)
def test_the_blockers_before_the_prices_are_the_fa_compute_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """With the prices served, Schedule FA's compute finds exactly the
    blockers the pipelines name before them, for every case above."""
    load, _refusal_text = FA_BLOCKER_CASES[case]
    load(monkeypatch, tmp_path)
    bundle = cast("Callable[..., Any]", pipeline._load)()
    options = core.reconstruct_options(bundle, _FY, "ITR-2")

    def unused(_text: str) -> Path:
        raise AssertionError("no guidance is written")

    gated = core.fa_inventory(bundle, write_guidance=unused)
    income = core.compute_source_income(bundle, _FY)
    prices, _equity, option_audit, quantities = core.fa_prices(
        bundle,
        _FY,
        options,
        tmp_path / "bundle",
        tmp_path / "user.toml",
        fetch_prices=False,
        price_cache_dir=None,
        price_fetcher=None,
        write_guidance=unused,
    )
    fa = core.compute_fa(
        bundle,
        _FY,
        income,
        options,
        prices=prices,
        option_prices=dict(option_audit.series_by_contract),
        quantities=quantities,
    )

    assert fa.blockers
    assert core.fa_pre_price_blockers(bundle, _FY, options, gated, income) == fa.blockers


def test_an_option_stop_names_the_fa_blockers_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An option blocker's stop names the Schedule FA blockers decided
    without prices after it, with the identity guidance an a3-identity
    blocker brings. On 57af529 an option blocker stopped the run alone, and
    these blockers came only once it was cleared."""
    options: Any = _blocked_options(RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT)
    bundle: Any = load_blocker_accounts(
        monkeypatch, tmp_path, buy_at_row(2, "2", "10"), securities=False
    )
    lot = RobinhoodSecurityEventReference(0, 2)
    credit = RobinhoodFaBlocker(
        kind=RobinhoodFaBlockerKind.A3_CREDIT_ATTRIBUTION,
        security_config_indices=(0,),
        lot_ids=(lot,),
    )
    path = tmp_path / "guidance.toml"
    written: list[str] = []

    def write(text: str) -> Path:
        written.append(text)
        return path

    stop = (
        f"option blockers prevent complete Schedule FA: {option_blocker_summary(options.blockers)}"
        f"; {FA_UNRESOLVED}\n{CREDIT_LINE}"
    )
    assert core.fa_pre_price_refusal(options, (credit,), bundle, write) == stop
    assert written == []

    identity = RobinhoodFaBlocker(kind=RobinhoodFaBlockerKind.A3_IDENTITY)
    assert core.fa_pre_price_refusal(options, (credit, identity), bundle, write) == (
        f"{stop}\n{identity_guidance_line(path)}"
    )
    assert len(written) == 1
