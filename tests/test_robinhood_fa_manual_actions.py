"""#1483: Robinhood Schedule FA's identity and Table D tasks name a cause and remedy.

On 331b472 ``robinhood-fa`` and ``robinhood-all`` printed an ``a3-identity``
task whose Name of entity isn't blank, and an ``option-table-d`` task, as the
bare kind. Each identity cause now names the Table A3 cells needing work and
the ``[[fa.entities]]`` keys that fill them, or entering them by hand, then
lists its holdings by symbol and ISIN. A rerun setting exactly the keys the
lines name proves they clear the identity task. Table D's line names its cause
and manual entry, with no holding. Tasks, their kinds and the exit code (3)
are unchanged.

#1490: a task whose Name of entity is blank printed only the #1369 name line
on 886182d. It now prints that line, then the same cause lines for its other
gaps, and following every line clears it. A blank address without a blank ZIP
has no input and is an invariant error.

The issue's other three kinds never reach these actions: each comes with a
blocker that refuses Schedule FA first (``option-other-table-review`` with
``OPTION_CLASSIFICATION``; a blank ``a3-gross-credits`` and an unresolved
``a2-gross-credits`` nature with ``A3_CREDIT_ATTRIBUTION``), so they keep the
bare kind.
"""

from __future__ import annotations

from dataclasses import fields, replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, cast

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
from ibkr_fa.compute.robinhood_entity_nature import RobinhoodEntityNatureEvidence
from ibkr_fa.compute.robinhood_fa import (
    RobinhoodA3IdentityGap,
    RobinhoodA3IdentityGapHolding,
    RobinhoodFaBlockerKind,
    RobinhoodFaManualKind,
    RobinhoodFaManualWork,
    _dedupe_manual,
    _option_manual_work,
    compute_robinhood_fa,
)
from ibkr_fa.compute.robinhood_income import (
    RobinhoodIncomeBlocker,
    RobinhoodIncomeBlockerKind,
    RobinhoodIncomeResult,
)
from ibkr_fa.compute.robinhood_lots import RobinhoodSecurityEventReference
from ibkr_fa.config import (
    FaEntityOverride,
    RobinhoodOptionTreatment,
)
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.pipeline_robinhood_reporting import fa_manual_reasons
from ibkr_fa.types import Isin, Ticker
from tests._fa_a3_name_runs import NAME_ACTION as _NAME_ACTION
from tests._fa_a3_name_runs import robinhood_all_legs as _legs
from tests._fa_cli_runs import CliRun as _Run
from tests._robinhood_a3_exact import SYMBOL
from tests._robinhood_a3_load import load_two_accounts
from tests._robinhood_cg import cg_result
from tests._robinhood_cli import robinhood_main as _robinhood_main
from tests._robinhood_fa_actions import (
    ADDRESS_AND_ZIP,
    ETF_NAME,
    NATURE,
    OPTION_TABLE_D_ACTION,
    ZIP,
    listed,
)
from tests._robinhood_fa_blocker_lines import FA_UNRESOLVED
from tests._robinhood_fa_inputs import FA_ISIN as _ISIN
from tests._robinhood_fa_inputs import FlatFx as _FlatFx
from tests._robinhood_fa_inputs import fa_anchor_only_options as _anchor_only_options
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_fa_inputs import fa_config as _config
from tests._robinhood_fa_inputs import fa_entity as _entity
from tests._robinhood_fa_inputs import fa_income as _income
from tests._robinhood_fa_inputs import fa_options as _options
from tests._robinhood_fa_inputs import fa_paired as _paired
from tests._robinhood_fa_inputs import fa_price_series as _prices
from tests._robinhood_fa_remedy_runs import named_entity_keys as _named_keys
from tests._robinhood_fa_remedy_runs import run_with_entity_block as _run
from tests._robinhood_option_runs import MAY_JUNE_2025_TRADES as _MAY_JUNE_2025_TRADES
from tests._robinhood_option_runs import option_cg_run as _option_cg_run
from tests._robinhood_pipeline_stubs import assembly_for as _assembly_for
from tests._robinhood_pipeline_stubs import complete_fa as _complete_fa
from tests._robinhood_pipeline_stubs import stub_cg_run as _stub_cg_run
from tests._robinhood_security import configured_securities

# The automatic identity of the harness's security: its address, ZIP and name.
_AUTOMATIC = _entity()[_ISIN]
_REVIEW_ADDRESS = cast("str", _AUTOMATIC.address)
_NATURE = "Listed company shares"
# What a filer verifies for each key a line names.
_VERIFIED = {
    "address": '"1 Example Way Example City"',
    "zip": '"99999"',
    "nature_of_entity": f'"{_NATURE}"',
    "name": '"Synthetic Trust - Synthetic Series"',
    "prefer_configured_address": "true",
}
# The harness's one holding, as each line lists it.
_HOLDING = f"{SYMBOL} ({_ISIN})"


def _entry(**values: str) -> str:
    """One ``[[fa.entities]]`` block for the harness's ISIN, each value as TOML."""
    return "\n[[fa.entities]]\n" + "".join(
        f"{key} = {value}\n" for key, value in {"isin": f'"{_ISIN}"', **values}.items()
    )


def _evidence(nature: str) -> RobinhoodEntityNatureEvidence:
    """Automatic nature evidence for the harness's ISIN."""
    return RobinhoodEntityNatureEvidence(
        isin=_ISIN, value=nature, source="synthetic", transformation="none", raw_value=nature
    )


class _Cause(NamedTuple):
    """A run's lines, its ``[[fa.entities]]`` entry's TOML values (``None``
    for no entry), its automatic identity and nature evidence, whether the
    security is an ETF, the review file's Address of entity and ZIP Code, and
    the activity report's issuer name (``None`` for the harness's)."""

    actions: tuple[str, ...]
    entry: dict[str, str] | None = None
    automatic: tuple[FaEntityOverride, ...] = ()
    natures: tuple[RobinhoodEntityNatureEvidence, ...] = ()
    etf: bool = False
    review: tuple[str, str] = ("", "")
    issuer: str | None = None


def _followed(cause: _Cause) -> str:
    """``cause``'s entry (a new one if it has none) with every key its lines
    name set: to the review file's address and ZIP where it has them, as the
    lines say to copy them, and to a verified value otherwise."""
    review = {"address": cause.review[0], "zip": cause.review[1]}
    named = {
        key: f'"{review[key]}"' if review.get(key, "").strip() else _VERIFIED[key]
        for action in cause.actions
        for key in _named_keys(action)
    }
    return _entry(**{**(cause.entry or {}), **named})


_NO_NATURE = replace(_AUTOMATIC, nature_of_entity=None)
_ENTRY_ADDRESS = _VERIFIED["address"].strip('"')
_BLANK_NAME = "   "
_CAUSES = {
    # No automatic address and no entry: Address of entity, ZIP Code and
    # Nature of entity are blank.
    "no-identity": _Cause((listed(ADDRESS_AND_ZIP, _HOLDING), listed(NATURE, _HOLDING))),
    # An entry with no zip: its address files without a postal code.
    "entry-without-zip": _Cause(
        (listed(ZIP, _HOLDING),),
        entry={"address": _VERIFIED["address"], "nature_of_entity": _VERIFIED["nature_of_entity"]},
        review=(_ENTRY_ADDRESS, ""),
    ),
    # A whitespace-only automatic postal code (no automatic route builds one,
    # but the ZIP line holds for it): it wins over an entry's zip unless the
    # entry prefers its own address.
    "automatic-blank-zip": _Cause(
        (listed(ZIP, _HOLDING),),
        automatic=(replace(_NO_NATURE, zip_code=" "),),
        natures=(_evidence(_NATURE),),
        review=(_REVIEW_ADDRESS, " "),
    ),
    # An automatic address and ZIP, but no legal form and no entry.
    "no-nature": _Cause(
        (listed(NATURE, _HOLDING),),
        automatic=(_NO_NATURE,),
        review=(_REVIEW_ADDRESS, _AUTOMATIC.zip_code),
    ),
    # An ETF with no entry and no registrant evidence: the broker's
    # description as its name, and its other cells blank.
    "etf-unidentified": _Cause(
        (
            listed(ETF_NAME, _HOLDING),
            listed(ADDRESS_AND_ZIP, _HOLDING),
            listed(NATURE, _HOLDING),
        ),
        etf=True,
    ),
    # An ETF whose entry gives every cell but its name.
    "etf-entry-without-name": _Cause(
        (listed(ETF_NAME, _HOLDING),),
        entry={
            "address": _VERIFIED["address"],
            "zip": _VERIFIED["zip"],
            "nature_of_entity": _VERIFIED["nature_of_entity"],
        },
        etf=True,
        review=(_ENTRY_ADDRESS, _VERIFIED["zip"].strip('"')),
    ),
    # #1490: a whitespace-only issuer name that no entry or automatic identity
    # replaces prints the name line, then a line for each other gap.
    "blank-name-no-identity": _Cause(
        (_NAME_ACTION, listed(ADDRESS_AND_ZIP, _HOLDING), listed(NATURE, _HOLDING)),
        issuer=_BLANK_NAME,
    ),
    # An automatic address and ZIP with no name, and no legal form.
    "blank-name-no-nature": _Cause(
        (_NAME_ACTION, listed(NATURE, _HOLDING)),
        automatic=(replace(_NO_NATURE, name=None),),
        review=(_REVIEW_ADDRESS, _AUTOMATIC.zip_code),
        issuer=_BLANK_NAME,
    ),
    "blank-name-entry-without-zip": _Cause(
        (_NAME_ACTION, listed(ZIP, _HOLDING)),
        entry={"address": _VERIFIED["address"], "nature_of_entity": _VERIFIED["nature_of_entity"]},
        review=(_ENTRY_ADDRESS, ""),
        issuer=_BLANK_NAME,
    ),
    "blank-name-etf-unidentified": _Cause(
        (
            _NAME_ACTION,
            listed(ETF_NAME, _HOLDING),
            listed(ADDRESS_AND_ZIP, _HOLDING),
            listed(NATURE, _HOLDING),
        ),
        etf=True,
        issuer=_BLANK_NAME,
    ),
}
_FA_FORMATS = {"default": (), "filing-csv": ("--format", "filing-csv")}


def _manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cause: _Cause,
    label: str,
    command: str,
    *extra: str,
    block: str | None = None,
) -> _Run:
    """``command`` on ``cause``'s run, its entry replaced by ``block`` if given."""
    return _run(
        tmp_path,
        monkeypatch,
        capsys,
        label,
        command,
        *extra,
        block=(
            block if block is not None else "" if cause.entry is None else _entry(**cause.entry)
        ),
        automatic=cause.automatic,
        natures=cause.natures,
        etf=cause.etf,
        issuer=cause.issuer,
    )


@pytest.mark.parametrize("fmt", list(_FA_FORMATS))
@pytest.mark.parametrize("label", list(_CAUSES))
def test_robinhood_fa_prints_each_identity_cause_and_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    label: str,
    fmt: str,
) -> None:
    """The leg is ``manual`` and exits 3, with one ``Action:`` per cause, each
    naming the identity cells needing work and what fills them, then its holding. On 331b472
    it printed ``Action: a3-identity``."""
    cause = _CAUSES[label]
    run = _manual(tmp_path, monkeypatch, capsys, cause, "run", "robinhood-fa", *_FA_FORMATS[fmt])

    assert run.rc == 3
    assert run.out.startswith("Robinhood schedule-fa: manual\n")
    assert run.out.count("Action:") == len(cause.actions)
    assert "".join(f"  Action: {action}\n" for action in cause.actions) in run.out


@pytest.mark.parametrize("label", list(_CAUSES))
def test_robinhood_all_prints_each_identity_cause_and_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    label: str,
) -> None:
    """The schedule-fa leg is ``manual`` with one line per cause and its
    holding, no other leg is manual or failed, and the run exits 3. On 331b472
    the leg's line was ``-> a3-identity``."""
    cause = _CAUSES[label]
    run = _manual(tmp_path, monkeypatch, capsys, cause, "run", "robinhood-all")

    assert run.rc == 3
    legs = _legs(run.out)
    assert legs.pop("schedule-fa") == "manual"
    assert not {"manual", "error"} & set(legs.values())
    assert run.out.count("-> a3-identity") == len(cause.actions)
    assert "".join(f"{' ' * 17}-> {action}\n" for action in cause.actions) in run.out


@pytest.mark.parametrize("command", ["robinhood-fa", "robinhood-all"])
@pytest.mark.parametrize("label", list(_CAUSES))
def test_following_each_identity_remedy_clears_its_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    label: str,
    command: str,
) -> None:
    """A rerun whose entry adds or sets exactly the keys the lines name, each
    to a verified value (the review file's address and ZIP where a line says
    to copy them), loads in both commands and exits 0 with no action. Its
    stderr is the manual run's. For ``robinhood-fa``, stdout is the manual
    run's less the actions and with the leg ``ok``; for ``robinhood-all``,
    the leg statuses are unchanged except for ``schedule-fa`` becoming ``ok``."""
    cause = _CAUSES[label]
    manual = _manual(tmp_path, monkeypatch, capsys, cause, "manual", command)
    followed = _followed(cause)
    assert _named_keys(followed) == sorted(
        {
            "isin",
            *(cause.entry or {}),
            *(key for action in cause.actions for key in _named_keys(action)),
        }
    )

    rerun = _manual(tmp_path, monkeypatch, capsys, cause, "rerun", command, block=followed)

    assert rerun.rc == 0
    assert "a3-identity" not in rerun.out
    assert rerun.err == manual.err
    if command == "robinhood-all":
        assert _legs(rerun.out) == {**_legs(manual.out), "schedule-fa": "ok"}
    else:
        expected = manual.out.replace(
            "Robinhood schedule-fa: manual\n", "Robinhood schedule-fa: ok\n", 1
        )
        for action in cause.actions:
            expected = expected.replace(f"  Action: {action}\n", "")
        assert rerun.out == expected


def test_without_preferring_its_address_an_entry_zip_leaves_a_blank_automatic_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Control for the ZIP line's ``prefer_configured_address = true``: the
    same entry without it leaves the automatic blank ZIP filed."""
    block = _entry(address=f'"{_REVIEW_ADDRESS}"', zip=_VERIFIED["zip"])
    cause = _CAUSES["automatic-blank-zip"]
    run = _manual(tmp_path, monkeypatch, capsys, cause, "run", "robinhood-fa", block=block)

    assert run.rc == 3
    assert f"  Action: {listed(ZIP, _HOLDING)}\n" in run.out


def test_following_the_name_line_alone_leaves_the_nature_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Control for #1490's nature line: an entry setting only the keys the
    name line names (on 886182d the only line printed) fills the name, address
    and ZIP, and the rerun raises an identity task for the nature."""
    cause = _CAUSES["blank-name-no-identity"]
    block = _followed(cause._replace(actions=(_NAME_ACTION,)))
    run = _manual(tmp_path, monkeypatch, capsys, cause, "run", "robinhood-fa", block=block)

    assert run.rc == 3
    assert run.out.count("Action:") == 1
    assert f"  Action: {listed(NATURE, _HOLDING)}\n" in run.out


# ---- Compute carries each cause and holding; the renderer only words them ---------


def _compute(
    *,
    overrides: dict[Isin, FaEntityOverride],
    natures: dict[Isin, str] | None = None,
    sub_category: str = "COMMON",
    income: RobinhoodIncomeResult | None = None,
) -> Any:
    """The compute test bundle's Schedule FA: one security held at Dec 31."""
    paired, accounts = _paired(sold=False, dividend=False)
    config = _config(accounts)
    config = replace(config, securities=(replace(config.securities[0], sub_category=sub_category),))
    return compute_robinhood_fa(
        paired,
        config=config,
        securities=configured_securities(config),
        income=income or _income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=overrides,
        nature_by_isin=natures or {},
    )


_GAP = RobinhoodA3IdentityGap


@pytest.mark.parametrize(
    ("overrides", "natures", "sub_category", "gaps"),
    [
        pytest.param(
            {}, {}, "COMMON", {_GAP.ADDRESS, _GAP.ZIP_CODE, _GAP.NATURE}, id="no-identity"
        ),
        pytest.param(
            {_ISIN: replace(_AUTOMATIC, zip_code="")}, {}, "COMMON", {_GAP.ZIP_CODE}, id="blank-zip"
        ),
        pytest.param({_ISIN: _NO_NATURE}, {}, "COMMON", {_GAP.NATURE}, id="nature"),
        pytest.param(
            {_ISIN: replace(_AUTOMATIC, name=None)}, {}, "ETF", {_GAP.ETF_NAME}, id="etf-name"
        ),
        pytest.param({_ISIN: _AUTOMATIC}, {}, "COMMON", set(), id="complete"),
        pytest.param({_ISIN: _NO_NATURE}, {_ISIN: _NATURE}, "COMMON", set(), id="automatic-nature"),
    ],
)
def test_compute_carries_each_identity_gap_with_its_holding(
    overrides: dict[Isin, FaEntityOverride],
    natures: dict[Isin, str],
    sub_category: str,
    gaps: set[RobinhoodA3IdentityGap],
) -> None:
    """The ``a3-identity`` task carries each identity cell that needs work,
    with its row's symbol and ISIN; a complete identity raises no task, as on
    331b472."""
    result = _compute(overrides=overrides, natures=natures, sub_category=sub_category)

    (row,) = result.a3_rows
    tasks = [item for item in result.manual_work if item.kind is RobinhoodFaManualKind.A3_IDENTITY]
    expected = frozenset(
        RobinhoodA3IdentityGapHolding(gap, row.source_ticker, row.source_isin) for gap in gaps
    )
    assert [item.identity_gaps for item in tasks] == ([expected] if gaps else [])


def test_grouped_identity_task_carries_every_lots_gaps() -> None:
    first = RobinhoodSecurityEventReference(0, 2)
    second = RobinhoodSecurityEventReference(1, 7)
    gaps = (
        RobinhoodA3IdentityGapHolding(_GAP.NATURE, Ticker(SYMBOL), _ISIN),
        RobinhoodA3IdentityGapHolding(_GAP.ZIP_CODE, Ticker(SYMBOL), _ISIN),
    )
    result = _dedupe_manual(
        tuple(
            RobinhoodFaManualWork(
                kind=RobinhoodFaManualKind.A3_IDENTITY,
                security_config_indices=(3,),
                lot_ids=(lot,),
                identity_gaps=frozenset({gap}),
            )
            for lot, gap in zip((second, first), gaps, strict=True)
        )
    )

    assert result == (
        RobinhoodFaManualWork(
            kind=RobinhoodFaManualKind.A3_IDENTITY,
            security_config_indices=(3,),
            lot_ids=(first, second),
            identity_gaps=frozenset(gaps),
        ),
    )


_A = (Ticker("XQAA"), Isin("US99995B1044"))
_B = (Ticker("XQBB"), Isin("US99995A1052"))
_C = (Ticker(SYMBOL), _ISIN)


def _identity(
    index: int, holding: tuple[Ticker, Isin], *gaps: RobinhoodA3IdentityGap
) -> RobinhoodFaManualWork:
    return RobinhoodFaManualWork(
        kind=RobinhoodFaManualKind.A3_IDENTITY,
        security_config_indices=(index,),
        lot_ids=(RobinhoodSecurityEventReference(0, index),),
        identity_gaps=frozenset(RobinhoodA3IdentityGapHolding(gap, *holding) for gap in gaps),
    )


def _reasons(*items: RobinhoodFaManualWork) -> tuple[str, ...]:
    """The actions for ``items``, each lot named by a filled Table A3 row."""
    a3 = tuple(
        SimpleNamespace(lot_id=lot, row=SimpleNamespace(name_of_entity="Synthetic Corporation"))
        for item in items
        for lot in item.lot_ids
    )
    return fa_manual_reasons(cast("Any", SimpleNamespace(manual_work=items, a3=a3)))


def test_each_cause_prints_once_with_every_holding_it_covers() -> None:
    """One line per kind and cause, listing each holding once however many
    tasks name it: the series-ETF name, a blank address with its ZIP, a blank
    ZIP alone and a blank nature. Two Table D accounts print one line, with no
    holding."""
    table_d = [
        RobinhoodFaManualWork(kind=RobinhoodFaManualKind.OPTION_TABLE_D, account_indices=(index,))
        for index in (0, 1)
    ]
    a, b = (f"{ticker} ({isin})" for ticker, isin in (_A, _B))

    assert _reasons(
        _identity(0, _A, _GAP.ADDRESS, _GAP.ZIP_CODE, _GAP.NATURE),
        _identity(1, _B, _GAP.ZIP_CODE),
        _identity(2, _A, _GAP.NATURE),
        _identity(3, _B, _GAP.ETF_NAME, _GAP.NATURE),
        *table_d,
    ) == (
        listed(ETF_NAME, b),
        listed(ADDRESS_AND_ZIP, a),
        listed(ZIP, b),
        listed(NATURE, a, b),
        OPTION_TABLE_D_ACTION,
    )


def test_a_blank_address_without_a_blank_zip_is_an_invariant_error() -> None:
    """No input blanks a Robinhood Table A3 Address of entity alone (#1490):
    a row without an entry or an automatic identity has both cells blank, the
    loader refuses a blank configured address, an entry without one stops the
    run before any row (#1449), and every automatic route needs an address.
    On 886182d this printed an address-only line."""
    with pytest.raises(InvariantError) as raised:
        _reasons(_identity(0, _A, _GAP.ZIP_CODE), _identity(1, _C, _GAP.ADDRESS, _GAP.NATURE))

    assert str(raised.value) == (
        "InvariantError: a Robinhood Table A3 Address of entity is blank and its ZIP Code is not"
    )


def test_an_identity_task_without_a_gap_is_an_invariant_error() -> None:
    """Compute raises an identity task only for a row with a blank name or a
    gap it carries, so one with neither is a bug, not a bare kind."""
    with pytest.raises(InvariantError) as raised:
        _reasons(_identity(0, _A))

    assert str(raised.value) == "InvariantError: a Robinhood a3-identity task has no identity gap"


# ---- The kinds a blocker always refuses first -----------------------------------------


def _blocked_income(kind: RobinhoodIncomeBlockerKind) -> RobinhoodIncomeResult:
    """Source income with one ``kind`` blocker for the first account in 2025."""
    blocker = RobinhoodIncomeBlocker(kind=kind, account_indices=(0,), tax_years=(2025,))
    return replace(_income(dividend=False), blockers=(blocker,))


# Each income blocker an unresolved A2 nature comes from (compute/robinhood_fa.py
# _a2_gross_evidence): an incomplete Schedule OS is any blocker it doesn't
# ignore; interest without program-bank evidence is an unresolved partition; a
# calendar-year Interest Earned mismatch is a statement reconciliation blocker.
_SCHEDULE_OS_BLOCKERS = (
    RobinhoodIncomeBlockerKind.PROGRAM_BANK_PARTITION,
    RobinhoodIncomeBlockerKind.STATEMENT_RECONCILIATION,
)


def _gross_credit_fa(kind: RobinhoodIncomeBlockerKind) -> Any:
    """Schedule FA under a ``kind`` income blocker, with both gross-credit tasks."""
    income = _blocked_income(kind)
    assert not income.schedule_os_complete
    result = _compute(overrides={_ISIN: _AUTOMATIC}, income=income)
    assert {
        RobinhoodFaManualKind.A2_GROSS_CREDITS,
        RobinhoodFaManualKind.A3_GROSS_CREDITS,
    } <= {item.kind for item in result.manual_work}
    assert RobinhoodFaBlockerKind.A3_CREDIT_ATTRIBUTION in {item.kind for item in result.blockers}
    return result


def _option_review_fa() -> Any:
    """Schedule FA's option step for an unclassified account with options."""
    manual, blockers = _option_manual_work(_anchor_only_options(classified=False), 2025)
    assert [item.kind for item in manual] == [RobinhoodFaManualKind.OPTION_OTHER_TABLE_REVIEW]
    assert [item.kind for item in blockers] == [RobinhoodFaBlockerKind.OPTION_CLASSIFICATION]
    return _complete_fa(manual_work=manual, blockers=blockers, cbdt_json_eligible=False)


_UNREACHED = {
    **{
        f"gross-credits-{kind.value}": partial(_gross_credit_fa, kind)
        for kind in _SCHEDULE_OS_BLOCKERS
    },
    "option-other-table-review": _option_review_fa,
}
_UNREACHED_KINDS = (
    RobinhoodFaManualKind.A2_GROSS_CREDITS,
    RobinhoodFaManualKind.A3_GROSS_CREDITS,
    RobinhoodFaManualKind.OPTION_OTHER_TABLE_REVIEW,
)


@pytest.mark.parametrize("command", ["robinhood-fa", "robinhood-all"])
@pytest.mark.parametrize("label", list(_UNREACHED))
def test_each_unreached_kind_comes_with_a_blocker_that_refuses_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    label: str,
    command: str,
) -> None:
    """Compute raises ``a2-gross-credits`` and ``a3-gross-credits`` only with
    an ``A3_CREDIT_ATTRIBUTION`` blocker, and ``option-other-table-review``
    only with an ``OPTION_CLASSIFICATION`` one. Given that result, both
    commands refuse Schedule FA (exit 4) and print none of these kinds."""
    fa = _UNREACHED[label]()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    load_two_accounts(monkeypatch, run_dir, _buy("2"))
    monkeypatch.setattr(pipeline, "_compute_fa", lambda *_args, **_kwargs: fa)
    capsys.readouterr()

    assert _robinhood_main(command, run_dir, "--no-fetch-prices") == 4

    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert FA_UNRESOLVED in text
    assert not [kind.value for kind in _UNREACHED_KINDS if kind.value in text]


@pytest.mark.parametrize("kind", _UNREACHED_KINDS)
def test_kinds_a_blocker_always_refuses_first_keep_the_bare_kind(
    kind: RobinhoodFaManualKind,
) -> None:
    assert _reasons(RobinhoodFaManualWork(kind=kind, account_indices=(0,))) == (kind.value,)


# ---- option-table-d ----------------------------------------------------------------


def _table_d_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    treatment: RobinhoodOptionTreatment = RobinhoodOptionTreatment.CAPITAL_GAINS,
) -> None:
    """Options traded in CY 2025 under ``treatment`` and the real option
    compute, with Schedule FA's tasks from its real option step."""
    _stub_cg_run(monkeypatch, cg_result)
    _option_cg_run(tmp_path, monkeypatch, treatment, _MAY_JUNE_2025_TRADES)
    monkeypatch.setattr(
        pipeline,
        "_compute_fa",
        lambda _bundle, financial_year, _income, options, **_kwargs: _complete_fa(
            manual_work=_option_manual_work(options, financial_year.start_year)[0],
            cbdt_json_eligible=False,
        ),
    )
    # robinhood-all's report reads the income result's and assembly's warnings too.
    monkeypatch.setattr(
        pipeline,
        "_compute_income",
        lambda _bundle, _fy, cg, **_kwargs: (
            RobinhoodIncomeResult(**{field.name: () for field in fields(RobinhoodIncomeResult)}),
            SimpleNamespace(
                **vars(_assembly_for(cg)),
                fx_policy_appropriate_for_ay=True,
                wht_fx_policy_appropriate_for_ay=True,
            ),
            object(),
        ),
    )


@pytest.mark.parametrize(
    ("command", "line"),
    [
        ("robinhood-fa", f"Action: {OPTION_TABLE_D_ACTION}"),
        ("robinhood-all", f"-> {OPTION_TABLE_D_ACTION}"),
    ],
)
def test_option_table_d_prints_its_cause_and_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    line: str,
) -> None:
    """The schedule-fa leg is ``manual`` with one Table D line, and the run
    exits 3. On 331b472 the line was the bare ``option-table-d``."""
    _table_d_run(tmp_path, monkeypatch)

    assert _robinhood_main(command, tmp_path, "--no-fetch-prices") == 3

    out = capsys.readouterr().out
    assert [text.strip() for text in out.splitlines() if "option-table-d" in text] == [line]


def test_a_business_election_prints_no_option_table_d_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1621: the same trades under a speculative-business election are A2-only,
    so robinhood-fa, with the ITR-3 that election needs, prints no Table D line
    and exits 0."""
    _table_d_run(tmp_path, monkeypatch, RobinhoodOptionTreatment.SPECULATIVE_BUSINESS)

    rc = _robinhood_main("robinhood-fa", tmp_path, "--no-fetch-prices", "--itr-form", "ITR-3")

    captured = capsys.readouterr()
    assert "option-table-d" not in captured.out + captured.err
    assert rc == 0
