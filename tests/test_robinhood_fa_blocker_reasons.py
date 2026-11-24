"""#1550: Robinhood Schedule FA refusals name the records, cause and route for
each Schedule FA and equity inventory blocker an input can reach.

On 105bad5 ``robinhood-fa`` and ``robinhood-all``'s FA leg printed only
"Robinhood Schedule FA contains unresolved evidence blockers", or "Robinhood
equity inventory evidence is incomplete", for these kinds. The cases in
``test_each_reachable_kind_names_its_records_cause_and_route`` run the real
reconciliation, option, inventory, price-coverage, source-income and Schedule
FA steps over synthetic statements and activity reports. Only the bundle load,
the Schedule FA TTBR table and the Yahoo closes are stubbed in those cases, as
in ``tests/test_pipeline_robinhood_exact_a3.py``. The option tests below use
narrower harnesses.

``robinhood-fa`` now also checks the hard option blockers before the option-price
replay, so an oversold contract prints the option explanation
``robinhood-options`` prints. And a CG blocker with an unresolved identity
prints the lot-engine causes it carries (audit 44, a gap in #1546).

Kinds no input reaches through the CLI keep the fixed line alone:

- ``a3-inventory``: the FA compute gets ``fa_prices``' quantities in both
  commands, and ``fa_prices`` refuses any inventory blocker first.
- ``a3-disposal``: the lot engine makes a disposal slice only from one
  account's same-day DISPOSAL movements of one security, combined
  (``combined_disposal``), and each DISPOSAL movement comes from one sale row.
  ``_disposal_movement_for_references`` rebuilds that same combined movement
  from the sales' own movements, so it never finds none, a non-disposal or a
  non-sale row.
- the inventory gate's ``security-receipt``: an unresolved REC receipt refuses
  in ``resolve_robinhood_security_quantities`` (#1522), and a resolved one
  leaves no blocker.
- the lot engine's ``inherited-quantity`` and ``inherited-basis`` in Schedule
  FA: they come only from inventory blockers, which the gate refuses first.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import fields, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import ibkr_fa.compute.robinhood_fa as robinhood_fa_mod
import ibkr_fa.pipeline_robinhood as pipeline
from ibkr_fa.compute.robinhood_a2_peak import (
    RobinhoodA2PeakError,
    robinhood_required_option_sessions,
)
from ibkr_fa.compute.robinhood_fa import (
    RobinhoodFaBlocker,
    RobinhoodFaBlockerKind,
    _option_manual_work,
)
from ibkr_fa.compute.robinhood_income import RobinhoodIncomeResult
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionBlockerKind,
    RobinhoodOptionTreatment,
    option_blocker_summary,
)
from ibkr_fa.config import load_user_config
from ibkr_fa.normalize.robinhood import inventory as inventory_mod
from ibkr_fa.normalize.robinhood import resolve_robinhood_security_identities
from ibkr_fa.pipeline_robinhood import RobinhoodPipelineError
from ibkr_fa.pipeline_robinhood_models import RobinhoodLegStatus
from tests._robinhood_cg import CG_UNRESOLVED as _CG
from tests._robinhood_cg import SAME_DAY_CAUSE, SAME_DAY_ROUTE
from tests._robinhood_cli import robinhood_main as _robinhood_main
from tests._robinhood_cli import run_robinhood_command as _run
from tests._robinhood_fa_blocker_lines import ANCHOR as _ANCHOR
from tests._robinhood_fa_blocker_lines import FA_UNRESOLVED as _FA
from tests._robinhood_fa_blocker_lines import REFERENCES as _REFERENCES
from tests._robinhood_fa_blockers import FA_BLOCKER_CASES as _CASES
from tests._robinhood_fa_blockers import OPTION_CLASSIFICATION_LINE as _OPTION_CLASSIFICATION
from tests._robinhood_fa_blockers import REVIEWED_PLACEHOLDERS as _REVIEWED
from tests._robinhood_fa_blockers import SAME_DAY_TRADES as _SAME_DAY_TRADES
from tests._robinhood_fa_blockers import SNAPSHOT_LINE as _SNAPSHOT
from tests._robinhood_fa_blockers import FaBlockerLoader as _Loader
from tests._robinhood_fa_blockers import buy_at_row as _bought
from tests._robinhood_fa_blockers import load_blocker_accounts as _load
from tests._robinhood_fa_blockers import resolved_bundle as _resolved
from tests._robinhood_fa_blockers import schedule_fa_leg as _fa_leg
from tests._robinhood_identity_guidance import assert_guidance_file as _assert_guidance_file
from tests._robinhood_identity_guidance import inventory_refusal as _inventory_refusal
from tests._robinhood_identity_guidance import stub_identity_refusals as _stub_identity_refusals
from tests._robinhood_income_stubs import (  # noqa: F401 - autouse fixture
    silenced_default_income_warnings as _silence_default_income_warnings,
)
from tests._robinhood_option_inputs import (
    option_event,
    option_paired,
    reconstruct_synthetic_options,
)
from tests._robinhood_option_runs import BTO as _BTO
from tests._robinhood_option_runs import STC as _STC
from tests._robinhood_option_runs import blocked_options as _blocked_options
from tests._robinhood_option_runs import option_cg_run as _option_cg_run
from tests._robinhood_option_runs import option_trade as _option_trade
from tests._robinhood_pipeline_stubs import complete_fa as _complete_fa
from tests._robinhood_refusals import EVENT_REFERENCES, FNO_FEMA_WARNING
from tests._robinhood_remedies import filled_guidance
from tests._robinhood_security import (
    IDENTITY_GUIDANCE_FILE,
    configured_securities,
    identity_guidance_line,
)
from tests._robinhood_toml import robinhood_account_toml, robinhood_toml, write

# ---- Each reachable kind ---------------------------------------------------------


@pytest.mark.parametrize(("load", "refusal"), _CASES.values(), ids=_CASES.keys())
def test_each_reachable_kind_names_its_records_cause_and_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    load: _Loader,
    refusal: str,
) -> None:
    """robinhood-fa exits 4 as on 105bad5, writes no Schedule FA file, and
    prints each kind's line after the fixed one; robinhood-all's FA leg fails
    with the same text. On 105bad5 both printed the fixed line alone."""
    load(monkeypatch, tmp_path)

    assert _run("robinhood-fa", tmp_path, capsys) == (4, "", f"error: {refusal}\n")

    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == []
    leg = _fa_leg(tmp_path)
    assert leg.status is RobinhoodLegStatus.ERROR
    assert leg.reasons == (refusal,)


# ---- Following the routes ------------------------------------------------------------


def test_snapshot_basis_can_lack_pending_proof_with_one_matching_count() -> None:
    """The date basis still needs Pending Trades proof when exactly one count
    matches and the statement shows no position, as the normalizer's test does."""
    kind = inventory_mod._resolve_position_snapshot(
        trade_quantity=Decimal(1),
        settlement_quantity=Decimal(),
        statement_quantity=Decimal(),
        tolerance=Decimal(),
        pending_delta=None,
    )
    assert kind is inventory_mod.RobinhoodInventoryBlockerKind.SNAPSHOT_BASIS
    blocker = inventory_mod.RobinhoodInventoryBlocker(
        account_index=0,
        cusip=None,
        unproved_fact=inventory_mod.RobinhoodInventoryUnprovedFact.QUANTITY,
        kind=kind,
        statement_index=0,
    )
    assert robinhood_fa_mod.inventory_blocker_reasons((blocker,)) == (
        _SNAPSHOT.replace(" (page 1 row 1)", ""),
    )


def test_following_the_report_route_writes_schedule_fa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The anchor-mismatch line: the activity report misses the second share's
    buy, so supplying the complete report, with that buy at the row the
    statements imply, writes Schedule FA, its Table A3 identity resolved from
    GLEIF, not a stub (#1574)."""
    _load(monkeypatch, tmp_path, _bought(2, "1", "10"))
    code, out, err = _run("robinhood-fa", tmp_path, capsys)
    assert (code, out) == (4, "")
    assert "supply the complete report and re-run" in err

    _resolved(
        monkeypatch,
        tmp_path,
        _load(monkeypatch, tmp_path, _bought(2, "1", "10"), _bought(3, "1", "10")),
    )

    assert _run("robinhood-fa", tmp_path, capsys)[::2] == (0, "")
    assert (tmp_path / "out" / "schedule-fa-a3.csv").is_file()


# ---- option-classification -----------------------------------------------------------


def _q1_option_fa() -> Any:
    """Schedule FA's real option step over the real reconstruction of a call
    bought and sold in February and March 2025: CY 2025, before FY 2025-26, so
    the account has no FY exposure and no option classification."""
    trades = (
        option_event(_BTO, 2, date(2025, 2, 3), date(2025, 2, 4)),
        option_event(_STC, 3, date(2025, 3, 3), date(2025, 3, 4), price="1.50", amount="300.00"),
    )
    paired = option_paired(trades, statements=(), report_start=date(2025, 1, 1))
    reconstruction = reconstruct_synthetic_options(paired, None)
    assert (reconstruction.classifications, reconstruction.blockers) == ((), ())
    manual, blockers = _option_manual_work(reconstruction, 2025)
    assert [item.kind for item in blockers] == [RobinhoodFaBlockerKind.OPTION_CLASSIFICATION]
    return _complete_fa(blockers=blockers, manual_work=manual, cbdt_json_eligible=False)


@pytest.mark.parametrize("command", ["robinhood-fa", "robinhood-all"])
def test_option_classification_names_its_account_cause_and_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """Through the CLI the held sessions need Massive closes, which a run with
    a key file fetches; here the prices are stubbed, and Schedule FA is its
    real option step's result for those trades."""
    fa = _q1_option_fa()
    _load(monkeypatch, tmp_path, _bought(2, "2", "10"))
    monkeypatch.setattr(pipeline, "_compute_fa", lambda *_args, **_kwargs: fa)

    if command == "robinhood-fa":
        assert _run(command, tmp_path, capsys) == (
            4,
            "",
            f"error: {_FA}\n{_OPTION_CLASSIFICATION}\n",
        )
    else:
        assert _fa_leg(tmp_path).reasons == (f"{_FA}\n{_OPTION_CLASSIFICATION}",)


# ---- Line order and grouping -----------------------------------------------------------


def test_lines_follow_kind_order_and_group_each_security_s_lots() -> None:
    """One line per explained kind, in kind order; a3-credit-attribution's two
    causes each get one; blockers of one kind and security share one
    reference with every lot. a3-identity, a3-inventory and a3-disposal add
    none."""
    ref = robinhood_fa_mod.RobinhoodSecurityEventReference
    kind = RobinhoodFaBlockerKind
    reasons = robinhood_fa_mod.fa_blocker_reasons(
        (
            RobinhoodFaBlocker(kind=kind.OPTION_CLASSIFICATION, account_indices=(1,)),
            RobinhoodFaBlocker(kind=kind.A3_CLOSING, security_config_indices=(0,)),
            RobinhoodFaBlocker(
                kind=kind.A3_CLOSING, security_config_indices=(0,), lot_ids=(ref(0, 5),)
            ),
            RobinhoodFaBlocker(
                kind=kind.A3_CLOSING, security_config_indices=(0,), lot_ids=(ref(0, 2),)
            ),
            RobinhoodFaBlocker(
                kind=kind.A3_CREDIT_ATTRIBUTION, security_config_indices=(1,), lot_ids=(ref(1, 3),)
            ),
            RobinhoodFaBlocker(
                kind=kind.A3_CREDIT_ATTRIBUTION, security_config_indices=(0,), income_blocked=True
            ),
            RobinhoodFaBlocker(kind=kind.A3_IDENTITY),
            RobinhoodFaBlocker(kind=kind.A3_INVENTORY),
            RobinhoodFaBlocker(kind=kind.A3_DISPOSAL, lot_ids=(ref(0, 9),)),
        )
    )

    assert [reason.split(_REFERENCES)[1].split(". ")[0] for reason in reasons] == [
        "Robinhood security 1 (lots 0:2, 0:5)",
        "Robinhood security 1",
        "Robinhood security 2 (lots 1:3)",
        "account_index 1",
    ]


# ---- Option blockers before the option-price replay -------------------------------------

# One call bought (row 2), then two sold to close (row 3).
_OVERSALE = (
    _option_trade(_BTO, 2, date(2025, 5, 1), "-615243.00"),
    replace(
        _option_trade(_STC, 3, date(2025, 6, 2), "1615242.00"),
        quantity=Decimal(2),
        price_usd=Decimal("8076.21"),
    ),
)


def _oversale_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """The real option step over :data:`_OVERSALE`, with an FA price step
    that replays the options as ``fa_prices``' option-price audit does,
    refusing as it does. Returns the price step's calls."""
    _option_cg_run(tmp_path, monkeypatch, RobinhoodOptionTreatment.CAPITAL_GAINS, _OVERSALE)
    monkeypatch.setattr(pipeline, "_fa_inventory", lambda *_args, **_kwargs: None, raising=False)
    calls: list[object] = []

    def replay(_bundle: object, financial_year: Any, options: Any, *_args: object, **_kw: object):
        calls.append(options)
        try:
            robinhood_required_option_sessions(options, calendar_year=financial_year.start_year)
        except RobinhoodA2PeakError as exc:
            raise RobinhoodPipelineError(str(exc)) from None
        raise AssertionError("the oversold contract replays")

    monkeypatch.setattr(pipeline, "_fa_prices", replay)
    return calls


def test_an_option_oversale_prints_the_option_blocker_in_robinhood_fa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """robinhood-fa checks the hard option blockers before pricing, so it
    prints the explanation robinhood-options prints for the same input, not
    "Robinhood daily option inventory became negative" (105bad5). Exit 4 as
    before; the price step never runs."""
    calls = _oversale_run(tmp_path, monkeypatch)
    code, out, options_err = _run("robinhood-options", tmp_path, capsys)
    assert (code, out) == (4, "")
    summary = options_err.partition("Robinhood options contain unresolved blockers: ")[2]
    assert "an option contract is sold to close more than" in summary

    assert _run("robinhood-fa", tmp_path, capsys) == (
        4,
        "",
        f"error: {FNO_FEMA_WARNING} option blockers prevent complete Schedule FA: {summary}",
    )
    assert calls == []


def test_robinhood_all_checks_the_option_blockers_before_pricing_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """robinhood-all's FA leg checks them in robinhood-fa's order (#1560): it
    prints the option summary once, without the replay's "Robinhood daily
    option inventory became negative" (fc442a8), and the price step never
    runs."""
    calls = _oversale_run(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pipeline,
        "_compute_income",
        lambda _bundle, _fy, _cg, **_kwargs: (
            RobinhoodIncomeResult(**{field.name: () for field in fields(RobinhoodIncomeResult)}),
            None,
            None,
        ),
    )
    _, out, options_err = _run("robinhood-options", tmp_path, capsys)
    assert out == ""
    summary = options_err.partition("Robinhood options contain unresolved blockers: ")[2].rstrip(
        "\n"
    )

    (reason,) = _fa_leg(tmp_path).reasons

    assert reason == f"option blockers prevent complete Schedule FA: {summary}"
    assert calls == []


def test_hard_options_preserve_inventory_refusal_and_identity_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep the base's inventory refusal and private guidance with hard options;
    its refusal also gains the inventory cause (#1550), absent on the base."""
    _stub_identity_refusals(monkeypatch)
    options = _blocked_options(RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT)
    monkeypatch.setattr(pipeline, "_reconstruct_options", lambda *_args: options)

    code, out, err = _run("robinhood-fa", tmp_path, capsys)

    assert (code, out) == (4, "")
    path = tmp_path / "out" / IDENTITY_GUIDANCE_FILE
    _assert_guidance_file(path)
    assert [item.name for item in path.parent.iterdir()] == [path.name]
    assert identity_guidance_line(path) in err
    assert _ANCHOR.format(references=_REFERENCES + "account_index 0 statement 1") in err


def test_robinhood_all_keeps_the_inventory_refusal_ahead_of_hard_options_without_pricing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """robinhood-all's FA leg runs the inventory gate before it reports the
    hard option blockers (#1560), as robinhood-fa does: the refusal and its
    guidance line come first, the run keeps the private guidance file, and
    the price step never runs. On fc442a8 the same text came from the price
    step's own inventory gate."""
    _stub_identity_refusals(monkeypatch)
    options = _blocked_options(RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT)
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: options)
    priced: list[object] = []
    price = pipeline._fa_prices

    def recorded(*args: Any, **kwargs: Any) -> Any:
        priced.append(args)
        return price(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_fa_prices", recorded)

    (reason,) = _fa_leg(tmp_path).reasons

    path = tmp_path / "all" / IDENTITY_GUIDANCE_FILE
    _assert_guidance_file(path)
    assert [item.name for item in path.parent.iterdir()] == [path.name]
    assert reason == (
        f"{_inventory_refusal()}\n{identity_guidance_line(path)}; option blockers "
        f"prevent complete Schedule FA: {option_blocker_summary(options.blockers)}"
    )
    assert priced == []


# ---- A CG identity blocker's lot causes (audit 44) ---------------------------------------

_CG_SAME_DAY = f"{SAME_DAY_CAUSE}{EVENT_REFERENCES}sales 0:4 (lots 0:2, 0:3){SAME_DAY_ROUTE}"


def _guidance(run_dir: Path) -> str:
    return identity_guidance_line(run_dir / "out" / IDENTITY_GUIDANCE_FILE)


def _cg_text(command: str, run_dir: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """``command``'s exit 4 and the lines it prints from the CG refusal on,
    without robinhood-income's ``Action:`` and robinhood-all's ``-> `` prefixes."""
    capsys.readouterr()
    assert _robinhood_main(command, run_dir, "--no-fetch-prices") == 4
    captured = capsys.readouterr()
    if command == "robinhood-cg":
        return captured.err.removeprefix("error: ")
    lines = [re.sub(r"^\s*(Action: |-> )", "", line) for line in captured.out.splitlines()]
    start = lines.index(_CG)
    return "\n".join(lines[start : start + 3]) + "\n"


@pytest.mark.parametrize("command", ["robinhood-all", "robinhood-income"])
def test_a_cg_identity_blocker_names_its_lot_cause_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """The comment's input: the issue's same-day trades in a security with no
    configured identity. Each command names the same-day cause and its route
    on the first run, then the identity guidance. On 105bad5 they printed
    the guidance alone after the fixed line. robinhood-cg's first run is the
    first check of :func:`test_following_the_identity_guidance_leaves_only_the_lot_cause`."""
    _load(monkeypatch, tmp_path, *_SAME_DAY_TRADES, held=Decimal("1.5"), securities=False)

    assert _cg_text(command, tmp_path, capsys) == f"{_CG}\n{_CG_SAME_DAY}\n{_guidance(tmp_path)}\n"


def test_following_the_identity_guidance_leaves_only_the_lot_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Follow the guidance line word for word: replace every placeholder in
    the private file's block and add it to user.toml. The rerun refuses with
    only the same-day line the first run already named; on 105bad5 the first
    run named none."""
    bundle = _load(monkeypatch, tmp_path, *_SAME_DAY_TRADES, held=Decimal("1.5"), securities=False)
    first = _cg_text("robinhood-cg", tmp_path, capsys)
    assert first == f"{_CG}\n{_CG_SAME_DAY}\n{_guidance(tmp_path)}\n"
    block = filled_guidance(tmp_path / "out" / IDENTITY_GUIDANCE_FILE, _REVIEWED)
    account = robinhood_account_toml(
        number="999999999", opening_date="2020-01-01", source="synthetic statement"
    )
    followed = tmp_path / "followed"
    followed.mkdir()
    robinhood = load_user_config(
        write(followed, robinhood_toml(first=account, extra=f"\n{block}"))
    ).robinhood
    assert robinhood is not None
    (security,) = robinhood.securities
    assert tomllib.loads(block)["robinhood"]["securities"][0]["cusip"] == security.cusip
    bundle.robinhood = replace(bundle.robinhood, securities=robinhood.securities)
    bundle.securities = configured_securities(bundle.robinhood)
    bundle.identities = resolve_robinhood_security_identities(bundle.paired, robinhood.securities)

    capsys.readouterr()
    assert _robinhood_main("robinhood-cg", tmp_path, "--no-fetch-prices") == 4
    assert capsys.readouterr().err == f"error: {_CG}\n{_CG_SAME_DAY}\n"
