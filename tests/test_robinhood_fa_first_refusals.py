"""#1567: ``robinhood-fa``'s and ``robinhood-all``'s Schedule FA refusal names,
on the first run, each stop the run already knows before pricing.

Each case follows the first text literally: an identity guidance file gets its
placeholders filled and goes into user.toml, and an incomplete activity report
gets its missing trade. The rerun then reaches exit 0 or exactly the stop the
first text named. A stop whose route is completing Schedule FA by hand has
nothing to follow; serving the option closes 789c17f asked for first changes
nothing there, and no close is read.

On 789c17f:

- a held security with no configured identity refused at the price stage
  with "Robinhood equity inventory has no configured identity" alone, and
  wrote no guidance file;
- an account that traded options in the calendar year but none in the
  financial year refused first for the Massive key or a price cache, and only
  with the closes for its missing option classification;
- an open business-treated position at the financial year's end refused
  first for the closes, and only then at the A2 peak;
- ``robinhood-fa``'s inventory refusal under a hard option blocker dropped
  the option summary ``robinhood-all``'s FA leg prints.

The runs use the real reconciliation, option, inventory and Schedule FA steps
of the harnesses in ``tests/_robinhood_fa_blockers.py`` and
``tests/_robinhood_option_texts.py``; only the inventory case stubs its
options, as the hard-option cases in
``tests/test_robinhood_fa_blocker_reasons.py`` do.

Since #1574 the unconfigured security stops before any price, on its
a3-identity blocker, and its case follows the guidance through the address
stop to exit 0 with no entity stub.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
import ibkr_fa.pipeline_robinhood_core as core
import ibkr_fa.pipeline_robinhood_equity_prices as equity_prices
import ibkr_fa.pipeline_robinhood_option_prices as option_prices
from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionBlockerKind,
    RobinhoodOptionTreatment,
)
from ibkr_fa.config import load_user_config
from ibkr_fa.normalize.robinhood import resolve_robinhood_security_identities
from ibkr_fa.pipeline_robinhood_models import RobinhoodLegStatus
from ibkr_fa.prices.series import MassiveOptionCloseSeries, OptionCloseBar
from tests._fa_a3_name_runs import output_table as _table
from tests._robinhood_cli import run_robinhood_command as _run
from tests._robinhood_fa_address import address_stop as _address_stop
from tests._robinhood_fa_address_runs import ADDRESS_GUIDANCE_HEADER as _ADDRESS_HEADER
from tests._robinhood_fa_blocker_lines import (
    ANCHOR,
    FA_UNRESOLVED,
    INVENTORY_INCOMPLETE,
    REFERENCES,
)
from tests._robinhood_fa_blockers import (
    OPTION_CLASSIFICATION_LINE,
    POSITION_REFERENCES,
    REVIEWED_PLACEHOLDERS,
    buy_at_row,
    empty_price_cache,
    load_blocker_accounts,
    resolved_bundle,
    schedule_fa_leg,
)
from tests._robinhood_fa_inputs import FA_CUSIP as _CUSIP
from tests._robinhood_fa_inputs import FA_ISIN as _ISIN
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_fa_inputs import fa_sale as _sale
from tests._robinhood_option_runs import LOCATED_BLOCKERS as _LOCATED
from tests._robinhood_option_runs import blocked_options as _blocked_options
from tests._robinhood_option_series import option_series
from tests._robinhood_option_texts import BUY_TO_OPEN as _BTO
from tests._robinhood_option_texts import SELL_TO_CLOSE as _STC
from tests._robinhood_option_texts import held_call as _held
from tests._robinhood_option_texts import load_option_accounts as _load
from tests._robinhood_option_texts import option_at_row as _trade
from tests._robinhood_refusals import FNO_FEMA_WARNING
from tests._robinhood_remedies import filled_guidance
from tests._robinhood_security import (
    IDENTITY_GUIDANCE_FILE,
    configured_securities,
    identity_guidance_line,
)
from tests._robinhood_toml import robinhood_account_toml, robinhood_toml, write

_OPTION_STOP = "option blockers prevent complete Schedule FA: "


def _closes(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Serve $1.00 Massive closes, and return the reads."""
    reads: list[object] = []

    def read(**kwargs: Any) -> MassiveOptionCloseSeries:
        reads.append(kwargs)
        days = (kwargs["end"] - kwargs["start"]).days + 1
        return option_series(
            "O:SYNTH",
            kwargs["underlying"],
            SimpleNamespace(**kwargs),
            {
                day: OptionCloseBar(day, Decimal("1.00"), Decimal(1), 1)
                for day in (kwargs["start"] + timedelta(days=offset) for offset in range(days))
            },
            artifact_path=Path("synthetic.json"),
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    monkeypatch.setattr(option_prices, "load_or_fetch_massive_option_closes", read)
    return reads


# ---- A held security with no configured identity ------------------------------------


# The held security's [[fa.entities]] entry with a name alone: no address,
# ZIP or nature (#1449).
_ENTITY_NAME = "Tax Payer Holdings Inc."
_ENTRY = f'\n[[fa.entities]]\nisin = "{_ISIN}"\nname = "{_ENTITY_NAME}"\n'
# What the GLEIF record resolved_bundle serves gives the entry.
_GLEIF_ADDRESS = (
    "C/O SAMPLE REGISTERED AGENT, SAMPLE AGENT CENTER, 99 SAMPLE ST, SAMPLETON, US-DE, US"
)


@pytest.mark.parametrize("command", ["robinhood-fa", "robinhood-all"])
def test_following_the_identity_then_the_address_guidance_writes_schedule_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """Two shares bought and held in a security with no configured identity,
    an empty price cache, and a user.toml whose [[fa.entities]] entry names
    the security but gives no address, ZIP or nature. Each rerun resolves
    that entry from the run's user.toml through the pipeline's own resolver,
    with one GLEIF record for the security (#1449); no entity override stub
    supplies a Table A3 cell (#1574).

    The first run names a3-identity before any price and writes the
    identity guidance. Its filled-in block is parsed separately into the
    synthetic bundle's securities; the rerun stops for the entry with no
    address and writes the address GLEIF found.
    With that block's address and zip uncommented in place of the entry,
    and the prices fixed, the third run writes Schedule FA with them. On
    57af529 the first run named the price stage's identity refusal, and the
    test reached exit 0 on the second run only because a stub filled the
    cells the user.toml lacked."""
    stub = load_blocker_accounts(monkeypatch, tmp_path, buy_at_row(2, "2", "10"), securities=False)
    served = equity_prices.load_price_series
    reads = empty_price_cache(monkeypatch)
    config = tmp_path / "user.toml"
    config.write_text(config.read_text(encoding="utf-8") + _ENTRY, encoding="utf-8")
    resolved_bundle(monkeypatch, tmp_path, stub)
    path = tmp_path / ("out" if command == "robinhood-fa" else "all") / IDENTITY_GUIDANCE_FILE

    def refusal() -> str:
        if command == "robinhood-fa":
            code, out, err = _run(command, tmp_path, capsys)
            assert (code, out) == (4, "")
            return err.removeprefix("error: ").removesuffix("\n")
        (reason,) = schedule_fa_leg(tmp_path).reasons
        return reason

    assert refusal() == f"{FA_UNRESOLVED}\n{identity_guidance_line(path)}"
    assert reads == []

    account = robinhood_account_toml(
        number="999999999", opening_date="2020-01-01", source="synthetic statement"
    )
    (tmp_path / "followed").mkdir()
    robinhood = load_user_config(
        write(
            tmp_path / "followed",
            robinhood_toml(
                first=account, extra=f"\n{filled_guidance(path, REVIEWED_PLACEHOLDERS)}"
            ),
        )
    ).robinhood
    assert robinhood is not None
    stub.robinhood = replace(stub.robinhood, securities=robinhood.securities)
    stub.securities = configured_securities(stub.robinhood)
    stub.identities = resolve_robinhood_security_identities(stub.paired, robinhood.securities)
    resolved_bundle(monkeypatch, tmp_path, stub)

    assert refusal() == _address_stop(path)
    block = path.read_text(encoding="utf-8").partition(_ADDRESS_HEADER)[2]
    assert block.count("# address = ") == block.count("# zip = ") == 1
    verified = block.replace("# address = ", "address = ").replace("# zip = ", "zip = ")
    config.write_text(
        config.read_text(encoding="utf-8").replace(_ENTRY, verified), encoding="utf-8"
    )
    resolved_bundle(monkeypatch, tmp_path, stub)
    monkeypatch.setattr(equity_prices, "load_price_series", served)

    if command == "robinhood-fa":
        assert _run(command, tmp_path, capsys)[::2] == (0, "")
    else:
        leg = schedule_fa_leg(tmp_path)
        assert (leg.status, leg.reasons) == (RobinhoodLegStatus.OK, ())
    rows = _table(path.parent / "schedule-fa-a3.csv")
    filed = [dict(zip(rows[0], row, strict=True)) for row in rows[1:]]
    assert [
        (row["Name of entity"], row["Address of entity"], row["ZIP Code"]) for row in filed
    ] == [(_ENTITY_NAME, _GLEIF_ADDRESS, "99999")]
    assert all(row["Nature of entity"] for row in filed)


# ---- Option stops that need no prices -------------------------------------------------

# A call bought on 3 February 2025 and sold on 3 March 2025: calendar year
# 2025, before FY 2025-26, so the account has no FY classification.
_Q1_ROUND_TRIP = (
    _trade(_BTO, 2, date(2025, 2, 3)),
    _trade(_STC, 3, date(2025, 3, 3)),
)


def _unclassified_q1(monkeypatch: pytest.MonkeyPatch, run_dir: Path) -> str:
    _load(monkeypatch, run_dir, _Q1_ROUND_TRIP, classified=())
    return f"{FA_UNRESOLVED}\n{OPTION_CLASSIFICATION_LINE}"


def _open_business(monkeypatch: pytest.MonkeyPatch, run_dir: Path) -> str:
    """A speculative-business call bought on 1 December 2025 and still held
    on 31 December and 31 March, as both statements show."""
    _load(
        monkeypatch,
        run_dir,
        (_trade(_BTO, 2, date(2025, 12, 1)),),
        positions={0: (_held("1"),), 1: (_held("1"),)},
        treatment=RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )
    return f"{_OPTION_STOP}{_LOCATED[RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION][1]}"


def _open_beside_unclassified(monkeypatch: pytest.MonkeyPatch, run_dir: Path) -> str:
    """:func:`_open_business`'s call in account 1, and :data:`_Q1_ROUND_TRIP`
    in account 2, which nothing classifies: each account's stop, the option
    blocker first, as the A2 peak refuses it first."""
    _load(
        monkeypatch,
        run_dir,
        (_trade(_BTO, 2, date(2025, 12, 1)),),
        _Q1_ROUND_TRIP,
        positions={0: (_held("1"),), 1: (_held("1"),)},
        treatment=RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )
    classification = OPTION_CLASSIFICATION_LINE.replace("account_index 0.", "account_index 1.")
    return (
        f"{_OPTION_STOP}{_LOCATED[RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION][1]}; "
        f"{FA_UNRESOLVED}\n{classification}"
    )


_BY_HAND_CASES = {
    "option-classification": (_unclassified_q1, "ITR-2"),
    "open-business-position": (_open_business, "ITR-3"),
    "open-position-beside-an-unclassified-account": (_open_beside_unclassified, "ITR-3"),
}


@pytest.mark.parametrize("command", ["robinhood-fa", "robinhood-all"])
@pytest.mark.parametrize(("load", "itr_form"), _BY_HAND_CASES.values(), ids=_BY_HAND_CASES)
def test_an_option_stop_is_named_before_any_option_close_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    load: Any,
    itr_form: ItrForm,
    command: str,
) -> None:
    """Each stop sends Schedule FA by hand, and no close can clear one. With
    every close served, the run names every stop and reads no close."""
    stop = load(monkeypatch, tmp_path)
    reads = _closes(monkeypatch)

    if command == "robinhood-fa":
        assert _run(command, tmp_path, capsys, itr_form=itr_form) == (
            4,
            "",
            f"error: {FNO_FEMA_WARNING} {stop}\n",
        )
    else:
        leg = schedule_fa_leg(tmp_path, itr_form)
        assert (leg.status, leg.reasons, leg.warnings) == (
            RobinhoodLegStatus.ERROR,
            (stop,),
            (),
        )
    assert reads == []


# Account 1's unconfigured security, bought and sold on 10 February 2025, at
# activity report rows 4 and 5: no held day needs a close.
_SAME_DAY_ROUND_TRIP = tuple(
    replace(
        event,
        source_record_index=row,
        activity_date=date(2025, 2, 10),
        process_date=date(2025, 2, 10),
        settle_date=date(2025, 2, 10),
        quantity=Decimal(1),
        amount_usd=amount,
    )
    for row, event, amount in ((4, _buy("1"), Decimal(-10)), (5, _sale(), Decimal("14.99")))
)


@pytest.mark.parametrize("command", ["robinhood-fa", "robinhood-all"])
def test_a_classification_stop_keeps_the_identity_guidance_the_fa_compute_wrote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """:data:`_Q1_ROUND_TRIP` with nothing classified, beside a security with no
    configured identity that no price step reads. With every close cached,
    789c17f reached the FA compute, whose a3-identity refusal named the private
    guidance file beside the classification line and wrote it. The stop before
    the prices does the same, reading no close."""
    _load(monkeypatch, tmp_path, (*_Q1_ROUND_TRIP, *_SAME_DAY_ROUND_TRIP), classified=())
    reads = _closes(monkeypatch)
    stop = f"{FA_UNRESOLVED}\n{OPTION_CLASSIFICATION_LINE}"

    if command == "robinhood-fa":
        path = tmp_path / "out" / IDENTITY_GUIDANCE_FILE
        assert _run(command, tmp_path, capsys) == (
            4,
            "",
            f"error: {FNO_FEMA_WARNING} {stop}\n{identity_guidance_line(path)}\n",
        )
    else:
        path = tmp_path / "all" / IDENTITY_GUIDANCE_FILE
        assert schedule_fa_leg(tmp_path).reasons == (f"{stop}\n{identity_guidance_line(path)}",)
    assert f'cusip = "{_CUSIP}"' in path.read_text(encoding="utf-8").splitlines()
    assert reads == []


# ---- An inventory refusal beside a hard option blocker --------------------------------


@pytest.mark.parametrize("command", ["robinhood-fa", "robinhood-all"])
def test_following_the_inventory_route_reaches_the_option_stop_it_named(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """The activity report buys one share and the statements show two, beside
    an oversold call. The inventory refusal names the option stop too, in
    both commands; supplying the complete report reaches exactly that stop.
    On 789c17f robinhood-fa's refusal ended after the inventory line."""
    options = _blocked_options(RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY)
    monkeypatch.setattr(pipeline, "_reconstruct_options", lambda *_args: options)
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: options)
    # The stubbed options carry no activity rows or trades of their own, so
    # nothing for Schedule FA's steps before the prices either (#1574).
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    monkeypatch.setattr(core, "option_classification_blockers", lambda *_args: ())
    monkeypatch.setattr(core, "fa_pre_price_blockers", lambda *_args: ())
    inventory = "\n".join(
        (
            INVENTORY_INCOMPLETE,
            ANCHOR.format(references=REFERENCES + POSITION_REFERENCES),
        )
    )
    stop = f"{_OPTION_STOP}{_LOCATED[RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY][1]}"

    def refusal() -> str:
        if command == "robinhood-fa":
            code, out, err = _run(command, tmp_path, capsys)
            assert (code, out) == (4, "")
            return err.removeprefix(f"error: {FNO_FEMA_WARNING} ").removesuffix("\n")
        (reason,) = schedule_fa_leg(tmp_path).reasons
        return reason

    load_blocker_accounts(monkeypatch, tmp_path, buy_at_row(2, "1", "10"))
    assert refusal() == f"{inventory}; {stop}"

    load_blocker_accounts(monkeypatch, tmp_path, buy_at_row(2, "1", "10"), buy_at_row(3, "1", "10"))
    assert refusal() == stop
