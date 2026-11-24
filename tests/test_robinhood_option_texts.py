"""#1554: the Robinhood option texts each name the cause, account or rows the
filer needs, through the CLI.

Each case runs real account reconciliation and option reconstruction over
synthetic statements and activity reports; only the bundle load and the FY TTBR
table are stubbed, as in ``tests/test_robinhood_fa_blocker_reasons.py``. The
hard-kind ``robinhood-fa`` cases also run the real inventory gate before refusing.

- An open business-treated position at the FY's end is manual work, not a
  refusal: ``robinhood-options`` and ``robinhood-income`` now name it and its
  account. On 14b9fc5 they printed only "manual option schedules are required"
  and "remaining option items require manual Schedule FSI completion".
- The missing-classification recipe names the account and the financial year;
  on 14b9fc5 it printed placeholders for both.
- Five hard kinds print their explanation in ``robinhood-options`` and
  ``robinhood-fa``. These pass on 14b9fc5 too: they pin that path, the one
  #1550's oversale fix went through.

The option ``evidence-coverage`` kind is gone: no CLI input reached it. Each
command's loader refuses first (``normalize/robinhood/accounts.py``
``_validate_bundle_account`` and ``_bounded_bundle``, then
``validate_robinhood_report_coverage``), for the same four conditions:

- an activity report starting after the account's opening date;
- an activity report ending before the FY's end;
- a first statement starting after the opening date, which must fall in it;
- statements ending before the FY's end.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionBlockerKind,
    RobinhoodOptionTreatment,
)
from ibkr_fa.parser.robinhood import RobinhoodEvent
from ibkr_fa.parser.robinhood_statement import RobinhoodStatementPosition
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

_OPTIONS_REFUSAL = f"{FNO_FEMA_WARNING} Robinhood options contain unresolved blockers: "
_FA_REFUSAL = f"{FNO_FEMA_WARNING} option blockers prevent complete Schedule FA: "


def _written(run_dir: Path) -> list[str]:
    return sorted(path.name for path in (run_dir / "out").iterdir())


# ---- An open business position -------------------------------------------------


@pytest.mark.parametrize(
    ("command", "reason", "files"),
    [
        (
            "robinhood-options",
            f"manual option schedules are required; {_OPEN_BUSINESS}",
            ["robinhood-options.csv"],
        ),
        (
            "robinhood-income",
            f"remaining option items require manual Schedule FSI completion; {_OPEN_BUSINESS}",
            ["form-67.csv", "schedule-fsi.csv", "schedule-os.xlsx", "schedule-tr.csv"],
        ),
    ],
    ids=["robinhood-options", "robinhood-income"],
)
def test_an_open_business_position_names_its_unvalued_stock_and_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    reason: str,
    files: list[str],
) -> None:
    """A speculative-business call bought on 2 February 2026 and still held
    on 31 March, as the Q1 statement shows. The run stays manual (exit 3)
    and writes the same files; its reason now says why and where."""
    _load(
        monkeypatch,
        tmp_path,
        (_trade(_BTO, 2, date(2026, 2, 2)),),
        positions={1: (_held("1"),)},
        treatment=RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )

    code, out, _err = _run(command, tmp_path, capsys, itr_form="ITR-3")

    assert code == 3
    assert f"  Action: {reason}\n" in out
    assert _written(tmp_path) == files


# ---- The missing-classification recipe --------------------------------------------


def test_the_classification_recipe_names_the_account_and_year_it_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only account 2 traded options, and nothing classifies them. Run 1
    refuses with the recipe; with exactly the entry it shows, for account 2's
    number, run 2 passes the stop and writes the workpaper."""
    bundle = _load(monkeypatch, tmp_path, second=_ROUND_TRIP, classified=())

    code, out, first = _run("robinhood-options", tmp_path, capsys)

    assert (code, out, first) == (4, "", _RECIPE.format(accounts="account 2"))
    (tmp_path / "followed").mkdir()
    entry, _keys = _followed(tmp_path / "followed", first, "option_classifications")
    assert (entry.account_number, entry.financial_year) == (_ACCOUNT, 2025)
    bundle.robinhood = replace(bundle.robinhood, option_classifications=(entry,))

    code, out, _err = _run("robinhood-options", tmp_path, capsys)

    assert code == 3
    assert out.startswith("Robinhood options: manual\n")
    assert _written(tmp_path) == ["robinhood-options.csv"]


def test_the_classification_recipe_names_every_account_it_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both accounts traded options and neither is classified: one refusal
    names both."""
    later = (_trade(_BTO, 2, date(2025, 7, 1)), _trade(_STC, 3, date(2025, 8, 1)))
    _load(monkeypatch, tmp_path, _ROUND_TRIP, later, classified=())

    assert _run("robinhood-options", tmp_path, capsys) == (
        4,
        "",
        _RECIPE.format(accounts="each of accounts 1 and 2"),
    )


# ---- Each hard kind through the CLI ---------------------------------------------------

# Each kind's trades, account 1's statement positions, FY and first location.
_KIND_CASES: dict[
    RobinhoodOptionBlockerKind,
    tuple[tuple[RobinhoodEvent, ...], dict[int, tuple[RobinhoodStatementPosition, ...]], int, str],
] = {
    # The activity gives two contracts; the 31 December statement shows three.
    RobinhoodOptionBlockerKind.POSITION_RECONCILIATION: (
        (_trade(_BTO, 2, date(2025, 5, 1), quantity="2"),),
        {0: (_held("3"),), 1: (_held("2"),)},
        2025,
        "1 blocker; first at account 1, Securities Held in Account: statement 1 page 1 row 1; "
        "column Qty",
    ),
    # Capital gains closed in FY 2024-25 before the 23 July 2024 cut-over.
    RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE: (
        (_trade(_BTO, 2, date(2024, 5, 1)), _trade(_STC, 3, date(2024, 6, 3))),
        {},
        2024,
        "1 blocker; first at account 1, activity report row 3; column Activity Date",
    ),
    # One contract at $1.00 for an Amount of $101.00.
    RobinhoodOptionBlockerKind.PREMIUM_RECONCILIATION: (
        (_trade(_BTO, 2, date(2025, 5, 1), amount="-101.00"), _trade(_STC, 3, date(2025, 6, 2))),
        {},
        2025,
        "1 blocker; first at account 1, activity report row 2; columns Quantity, Price and Amount",
    ),
    # A put, bought and sold.
    RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT: (
        (
            _trade(_BTO, 2, date(2025, 5, 1), right="Put"),
            _trade(_STC, 3, date(2025, 6, 2), right="Put"),
        ),
        {},
        2025,
        "2 blockers; first at account 1, activity report row 2; column Description",
    ),
    # Sold on 23 June 2025, after its 20 June expiry.
    RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE: (
        (
            _trade(_BTO, 2, date(2025, 5, 1), expiration=date(2025, 6, 20)),
            _trade(_STC, 3, date(2025, 6, 23), expiration=date(2025, 6, 20)),
        ),
        {},
        2025,
        "1 blocker; first at account 1, activity report row 3; columns Activity Date and "
        "Description",
    ),
}


_REFUSALS = {"robinhood-options": _OPTIONS_REFUSAL, "robinhood-fa": _FA_REFUSAL}


@pytest.mark.parametrize("command", list(_REFUSALS))
@pytest.mark.parametrize("kind", list(_KIND_CASES), ids=lambda kind: kind.value)
def test_each_hard_kind_prints_its_explanation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: RobinhoodOptionBlockerKind,
    command: str,
) -> None:
    """Each command refuses (exit 4) with the kind's message and where its
    first blocker is, and leaves no output."""
    trades, positions, year, located = _KIND_CASES[kind]
    _load(monkeypatch, tmp_path, trades, positions=positions, year=year)

    assert _run(command, tmp_path, capsys, year=year) == (
        4,
        "",
        f"error: {_REFUSALS[command]}{OPTION_BLOCKER_MESSAGES[kind]} ({located})\n",
    )
    assert _written(tmp_path) == []
