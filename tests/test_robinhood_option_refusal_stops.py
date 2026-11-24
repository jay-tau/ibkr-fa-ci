"""#1569: Robinhood's ``--itr-form`` and missing-classification refusals name
every option stop the run already has, so following either one reaches
exactly the stop it named.

The follow-through cases run the real reconciliation and option steps of the harness in
``tests/_robinhood_option_texts.py``, follow the first text literally
(``--itr-form ITR-3``, or the ``[[robinhood.option_classifications]]`` entry it
describes, filled in) and rerun the same command.

On b8182e1:

- the ITR-form refusal named only the flag, in every command, though the
  reconstruction it checks already held its option blockers;
- the classification refusal left out the open-business-position and
  pre-cutover blockers of accounts that already had an entry: the run found
  them only after it refused.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa.compute.robinhood_options import RobinhoodOptionBlockerKind
from ibkr_fa.config import RobinhoodOptionTreatment
from tests._robinhood_cli import run_robinhood_command as _run
from tests._robinhood_option_texts import BUY_TO_OPEN as _BTO
from tests._robinhood_option_texts import CALL_ROUND_TRIP as _ROUND_TRIP
from tests._robinhood_option_texts import CLASSIFICATION_RECIPE_ERROR as _RECIPE
from tests._robinhood_option_texts import OPEN_BUSINESS_BLOCKERS as _OPEN_BUSINESS
from tests._robinhood_option_texts import SELL_TO_CLOSE as _STC
from tests._robinhood_option_texts import held_call as _held
from tests._robinhood_option_texts import load_option_accounts as _load
from tests._robinhood_option_texts import option_at_row as _trade
from tests._robinhood_refusals import FNO_FEMA_WARNING, OPTION_BLOCKER_MESSAGES
from tests._robinhood_remedies import FOLLOWED_REMEDY_ACCOUNT as _ACCOUNT
from tests._robinhood_remedies import followed_remedy_entry as _followed

_ERROR = f"error: {FNO_FEMA_WARNING} "
_ITR_FORM = "reviewed Robinhood option treatment requires --itr-form ITR-3"
_ALSO_FOUND = " won't clear the unresolved option blockers the run also found: "
_FA_STOP = "option blockers prevent complete Schedule FA: "
_SPECULATIVE = RobinhoodOptionTreatment.SPECULATIVE_BUSINESS


def _named(prefix: str, out: str, err: str) -> str:
    """What follows ``prefix`` on the one output line holding it. A refusal,
    whose ``prefix`` starts with ``error: ``, is that line alone on stderr,
    with nothing on stdout; any other stop line is in the run report on
    stdout, with nothing on stderr."""
    if prefix.startswith("error: "):
        assert out == ""
        (line,) = err.splitlines()
    else:
        assert err == ""
        (line,) = (line for line in out.splitlines() if prefix in line)
    return line[line.index(prefix) + len(prefix) :]


# ---- The ITR-form refusal ---------------------------------------------------------------

# One call bought and sold on 1 May 2025: no row says which came first.
_SAME_DAY = (
    _trade(_BTO, 2, date(2025, 5, 1)),
    _trade(_STC, 3, date(2025, 5, 1)),
)
# The blocker both runs name: its kind's message, count, account and location.
_SAME_DAY_BLOCKER = (
    f"{OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY]} (1 blocker; "
    "first at account 1, activity report row 2; columns Activity Date and Trans Code)"
)
# Each command's exit code with ITR-3, and the words before the blockers it
# stops at.
_ITR3_STOPS = {
    "robinhood-options": (4, f"{_ERROR}Robinhood options contain unresolved blockers: "),
    "robinhood-fa": (4, f"{_ERROR}{_FA_STOP}"),
    "robinhood-all": (4, f"-> {_FA_STOP}"),
    "robinhood-cg": (3, "  Action: option blockers require separate resolution: "),
    "robinhood-income": (
        3,
        "  Action: remaining option items require manual Schedule FSI completion; "
        "option blockers: ",
    ),
}


@pytest.mark.parametrize("command", list(_ITR3_STOPS))
def test_following_the_itr_form_refusal_reaches_the_stop_it_named(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """Account 1's options are speculative business, and it bought and sold
    one call on 1 May 2025. Run 1, with ITR-2, refuses for the flag and names
    the same-day blocker; run 2, with ITR-3, stops at exactly that blocker."""
    _load(monkeypatch, tmp_path, _SAME_DAY, treatment=_SPECULATIVE)

    code, first_out, first = _run(command, tmp_path, capsys)
    rerun_code, out, err = _run(command, tmp_path, capsys, itr_form="ITR-3")

    stop_code, prefix = _ITR3_STOPS[command]
    assert rerun_code == stop_code
    assert _named(prefix, out, err) == _SAME_DAY_BLOCKER
    assert (code, first_out, first) == (
        4,
        "",
        f"{_ERROR}{_ITR_FORM}; the flag{_ALSO_FOUND}{_named(prefix, out, err)}\n",
    )


@pytest.mark.parametrize("command", list(_ITR3_STOPS))
def test_the_itr_form_refusal_without_an_option_blocker_names_only_the_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """A speculative-business call bought in May and sold in June 2025: no
    option blocker, so the text is b8182e1's."""
    _load(monkeypatch, tmp_path, _ROUND_TRIP, treatment=_SPECULATIVE)

    assert _run(command, tmp_path, capsys) == (4, "", f"{_ERROR}{_ITR_FORM}\n")


# ---- The classification refusal --------------------------------------------------------


def _recipe(year: int) -> str:
    """The refusal for account 2's entry in FY ``year``, with no blocker."""
    return (
        _RECIPE.format(accounts="account 2")
        .replace("financial_year = 2025", f"financial_year = {year}")
        .removesuffix("\n")
    )


def _follow(run_dir: Path, bundle: Any, refusal: str) -> None:
    """Add the entry ``refusal`` describes, filled in, beside account 1's."""
    (run_dir / "followed").mkdir()
    entry, _keys = _followed(run_dir / "followed", refusal, "option_classifications")
    assert entry.account_number == _ACCOUNT
    bundle.robinhood = replace(
        bundle.robinhood,
        option_classifications=(*bundle.robinhood.option_classifications, entry),
    )


# Account 1's open position, as the run names it after the words before it.
_OPEN_POSITION = _OPEN_BUSINESS.removeprefix("option blockers: ")
# Each command's exit code once account 2 has its entry, and the words
# before the blockers it stops at.
_ENTRY_STOPS = {
    "robinhood-options": (3, "  Action: manual option schedules are required; option blockers: "),
    "robinhood-fa": (4, f"{_ERROR}{_FA_STOP}"),
    "robinhood-income": _ITR3_STOPS["robinhood-income"],
    "robinhood-all": (4, f"-> {_FA_STOP}"),
}


@pytest.mark.parametrize("command", list(_ENTRY_STOPS))
def test_following_the_classification_refusal_reaches_another_accounts_open_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """FY 2025-26. Account 1's options are speculative business, and a call it
    bought on 1 December 2025 is still open on 31 March 2026; account 2 bought
    and sold a call in the year and has no entry. Run 1 asks for account 2's
    entry and names account 1's open position; with the entry, run 2 stops at
    exactly that position."""
    bundle = _load(
        monkeypatch,
        tmp_path,
        (_trade(_BTO, 2, date(2025, 12, 1)),),
        _ROUND_TRIP,
        positions={0: (_held("1"),), 1: (_held("1"),)},
        treatment=_SPECULATIVE,
    )

    code, first_out, first = _run(command, tmp_path, capsys, itr_form="ITR-3")
    _follow(tmp_path, bundle, first)
    rerun_code, out, err = _run(command, tmp_path, capsys, itr_form="ITR-3")

    stop_code, prefix = _ENTRY_STOPS[command]
    assert rerun_code == stop_code
    assert _named(prefix, out, err) == _OPEN_POSITION
    assert (code, first_out, first) == (
        4,
        "",
        f"{_recipe(2025)}; the entry{_ALSO_FOUND}{_named(prefix, out, err)}\n",
    )


def test_following_the_classification_refusal_reaches_another_accounts_pre_cutover_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """FY 2024-25, the one year a close can fall before the 23 July 2024
    cut-over. Account 1's options are capital gains, and it bought a call on
    1 May 2024 and sold it on 3 June 2024; account 2 traded a call later in
    the year and has no entry. ``robinhood-fa`` alone reaches the blocker:
    the commands that convert option amounts to INR stop first at FY
    2024-25's rates."""
    bundle = _load(
        monkeypatch,
        tmp_path,
        (
            _trade(_BTO, 2, date(2024, 5, 1)),
            _trade(_STC, 3, date(2024, 6, 3)),
        ),
        (
            _trade(_BTO, 2, date(2024, 8, 1)),
            _trade(_STC, 3, date(2024, 9, 2)),
        ),
        year=2024,
    )

    code, first_out, first = _run("robinhood-fa", tmp_path, capsys, year=2024)
    _follow(tmp_path, bundle, first)
    rerun_code, out, err = _run("robinhood-fa", tmp_path, capsys, year=2024)

    assert rerun_code == 4
    assert _named(f"{_ERROR}{_FA_STOP}", out, err) == (
        f"{OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE]} "
        "(1 blocker; first at account 1, activity report row 3; column Activity Date)"
    )
    assert (code, first_out, first) == (
        4,
        "",
        f"{_recipe(2024)}; the entry{_ALSO_FOUND}{_named(f'{_ERROR}{_FA_STOP}', out, err)}\n",
    )
