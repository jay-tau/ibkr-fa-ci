"""An ``[[fa.entities]]`` entry may leave out ``address`` (#1449).

Owner decision, 15 Nov 2026: when an entry has no address, find the
appropriate one and make the filer verify it before Schedule FA is generated.
A Robinhood run writes the address it found for each held security's entry
to the private ``robinhood-identity-guidance.toml`` and stops Schedule FA
(exit 4); the block refuses at load until the filer uncomments a verified
address. ``robinhood-cg`` and ``robinhood-income`` run as without the entry.
IBKR looks no address up, so a held ISIN's entry still needs one. On main
every such entry refused at load, for every command.
"""

from __future__ import annotations

import os
import stat
from functools import partial
from pathlib import Path
from typing import cast

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
import ibkr_fa.pipeline_robinhood_entities as entities
from ibkr_fa.compute.robinhood_identity import (
    RobinhoodEntityRoute,
    RobinhoodFoundEntityAddress,
    RobinhoodUnverifiedEntityAddress,
)
from ibkr_fa.config import FaEntityOverride, load_user_config
from ibkr_fa.exceptions import ConfigError
from ibkr_fa.models.reference_identity import RegistrantLookup
from ibkr_fa.normalize.robinhood import RobinhoodSecurityIdentityResult
from ibkr_fa.pipeline_robinhood_bundle import (
    RobinhoodBundle,
    address_verification_guidance,
    identity_failure_guidance,
)
from ibkr_fa.robinhood_legal_entity_sources import load_robinhood_legal_entity_evidence
from tests._crash_reports import assert_no_markers
from tests._entity_transport import FixedReplyOpener as _RecordingOpener
from tests._fa_a3_name_runs import IBKR_NAV_STATEMENT as _NAV
from tests._fa_a3_name_runs import capture_main as _capture
from tests._fa_a3_name_runs import output_table as _table
from tests._fa_a3_name_runs import robinhood_all_legs as _legs
from tests._fa_a3_name_runs import schedule_fa_with_name as _schedule_fa
from tests._fa_cli_runs import CliRun as _Run
from tests._fa_cli_runs import robinhood_run as _invoke
from tests._ibkr_run_all import FY_2025_26_INPUT as _FY_2025_26_INPUT
from tests._ibkr_run_all import run_all_argv as _run_all_argv
from tests._ibkr_schedule_fa_inputs import SCHEDULE_FA_CONFIG as _FULL_CONFIG
from tests._ibkr_schedule_fa_inputs import flat_close_fetcher as _flat_close_fetcher
from tests._portal_parity import fix_xlsx_clock
from tests._robinhood_a3_load import load_two_accounts
from tests._robinhood_fa_address import ADDRESS_SUPERSEDED_ONE, address_stop
from tests._robinhood_fa_address_runs import ADDRESS_GUIDANCE_HEADER as _HEADER
from tests._robinhood_fa_address_runs import address_verification_run as _run
from tests._robinhood_fa_address_runs import stub_address_bundle as _bundle
from tests._robinhood_fa_inputs import FA_ISIN as _ISIN
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_gleif import issuer_record as _record
from tests._robinhood_gleif import issuer_reply as _response
from tests._robinhood_security import IDENTITY_GUIDANCE_FILE as _GUIDANCE
from tests._robinhood_security import IDENTITY_GUIDANCE_HEADER

_NAME = "Tax Payer Holdings Inc."
_FOUND_ADDRESS = "One Synthetic Way"
_FOUND_ZIP = "99990"
_VERIFIED_ADDRESS = "1 Sample Road, Suite 2"
_NOT_HELD_ISIN = "US99995A1052"
_ENTRY = f'\n[[fa.entities]]\nisin = "{_ISIN}"\nname = "{_NAME}"\n'
_GLEIF_BLOCK = (
    "# fa.entities[0]: found by GLEIF (the ISIN's legal address)\n"
    "[[fa.entities]]\n"
    f'isin = "{_ISIN}"\n'
    f'name = "{_NAME}"\n'
    f'# address = "{_FOUND_ADDRESS}"\n'
    f'# zip = "{_FOUND_ZIP}"\n'
    "prefer_configured_address = true\n"
)


# The stop's two lines, with the run folder masked.
_STOP = address_stop(f"<run>/out/{_GUIDANCE}")
# The stop when an entry's nature is missing too (#1568).
_NATURE_STOP = address_stop(f"<run>/out/{_GUIDANCE}", nature=True)
_NATURE_PLACEHOLDER = '# nature_of_entity = ""\n'


def _assert_stopped(run: _Run, guidance: str, *values: str, stop: str = _STOP) -> None:
    """Exit 4, the two-line ``stop`` on stderr, nothing on stdout, and the
    output folder holding only the private guidance file: ``guidance``, mode
    0o600. No stream holds ``values``."""
    assert run.rc == 4
    assert run.out == ""
    assert run.err == f"error: {stop}\n"
    assert [path.name for path in run.out_dir.iterdir()] == [_GUIDANCE]
    path = run.out_dir / _GUIDANCE
    assert path.read_bytes() == guidance.encode("utf-8")
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert_no_markers(run.out + run.err, _ISIN, _NAME, *values)


def _files(folder: Path) -> list[str]:
    """The names of the files in ``folder``, sorted; not the price cache folder."""
    return sorted(path.name for path in folder.iterdir() if path.is_file())


def _assert_same_run(run: _Run, control: _Run) -> None:
    """The same exit, streams and output bytes."""
    assert (run.rc, run.out, run.err) == (control.rc, control.out, control.err)
    names = _files(control.out_dir)
    assert names
    assert _files(run.out_dir) == names
    for name in names:
        assert (run.out_dir / name).read_bytes() == (control.out_dir / name).read_bytes(), name


# ---- robinhood-fa: find, write out, stop ---------------------------------------------


@pytest.mark.parametrize("fmt", ["filing-csv", "review-csv"])
def test_robinhood_fa_stops_with_the_gleif_address_to_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fmt: str
) -> None:
    """An entry with ``isin`` and ``name`` for the held security: no Schedule
    FA file, exit 4, and one block with the GLEIF address commented out."""
    run = _run(tmp_path, monkeypatch, capsys, "run", _ENTRY, "robinhood-fa", "--format", fmt)

    _assert_stopped(run, f"{_HEADER}\n{_GLEIF_BLOCK}", _FOUND_ADDRESS)


def test_robinhood_fa_stops_with_no_address_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No automatic address: the block says none was found and leaves both
    commented values empty. The entry's nature is repeated."""
    entry = f'\n[[fa.entities]]\nisin = "{_ISIN}"\nnature_of_entity = "Company"\n'
    run = _run(tmp_path, monkeypatch, capsys, "run", entry, "robinhood-fa", automatic=())

    _assert_stopped(
        run,
        f"{_HEADER}\n"
        "# fa.entities[0]: no address found; add the issuer's legal address\n"
        "[[fa.entities]]\n"
        f'isin = "{_ISIN}"\n'
        'nature_of_entity = "Company"\n'
        '# address = ""\n'
        '# zip = ""\n'
        "prefer_configured_address = true\n",
    )


def test_robinhood_fa_stops_for_an_etf_entry_without_a_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A configured ETF takes no automatic cell (ADR 0011), so nothing was
    looked up: the block says so and keeps the entry's preferences. The entry
    sets no nature, so the block and the stop name it too (#1568)."""
    entry = (
        f'\n[[fa.entities]]\nisin = "{_ISIN}"\nname = "{_NAME}"\n'
        "prefer_configured_name = true\nprefer_configured_nature = true\n"
    )
    run = _run(
        tmp_path, monkeypatch, capsys, "run", entry, "robinhood-fa", automatic=(), held_etf=True
    )

    _assert_stopped(
        run,
        f"{_HEADER}\n"
        "# fa.entities[0]: no address looked up for an ETF with an entry (ADR 0011): "
        "remove the entry to let the N-CEN route fill all four cells, or add the "
        "registrant's legal address, and name and nature_of_entity if this block has none\n"
        "[[fa.entities]]\n"
        f'isin = "{_ISIN}"\n'
        f'name = "{_NAME}"\n'
        f"{_NATURE_PLACEHOLDER}"
        '# address = ""\n'
        '# zip = ""\n'
        "prefer_configured_address = true\n"
        "prefer_configured_name = true\n"
        "prefer_configured_nature = true\n",
        stop=_NATURE_STOP,
    )


def test_the_block_refuses_unchanged_and_files_the_verified_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pasted unchanged the block refuses at load. With its address and zip
    uncommented, the address corrected, the run completes like the run with
    no entry, files the verified address and ZIP, and the entry's preferred
    name, and prints no supersession warning; without
    ``prefer_configured_address`` the same entry warns."""
    fix_xlsx_clock(monkeypatch)
    args = ("robinhood-fa", "--format", "review-csv")
    entry = f"{_ENTRY}prefer_configured_name = true\n"
    stopped = _run(tmp_path, monkeypatch, capsys, "stopped", entry, *args)
    block = (stopped.out_dir / _GUIDANCE).read_text(encoding="utf-8").removeprefix(_HEADER)

    config_path = tmp_path / "pasted.toml"
    config_path.write_text('[account]\nholder_name = "Tax Payer"\n' + block, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_user_config(config_path)
    assert exc.value.field == "fa.entities[0].prefer_configured_address"

    verified = block.replace(f'# address = "{_FOUND_ADDRESS}"', f'address = "{_VERIFIED_ADDRESS}"')
    verified = verified.replace(f'# zip = "{_FOUND_ZIP}"', 'zip = "99999"')
    no_entry = _run(tmp_path, monkeypatch, capsys, "no-entry", "", *args)
    run = _run(tmp_path, monkeypatch, capsys, "verified", "\n" + verified, *args)
    unpreferred = verified.replace("prefer_configured_address = true\n", "")
    warned = _run(tmp_path, monkeypatch, capsys, "unpreferred", "\n" + unpreferred, *args)

    assert no_entry.rc == run.rc == 0
    assert run.out == no_entry.out
    assert run.err == no_entry.err
    assert "superseded" not in run.out + run.err
    assert ADDRESS_SUPERSEDED_ONE in warned.out + warned.err
    rows = _table(run.out_dir / "schedule-fa-a3.csv")
    filed = [dict(zip(rows[0], row, strict=True)) for row in rows[1:]]
    assert [(row["Address of entity"], row["ZIP Code"]) for row in filed] == [
        (_VERIFIED_ADDRESS, "99999")
    ]
    assert [row["Name of entity"] for row in filed] == [_NAME]


def test_robinhood_fa_ignores_an_addressless_entry_for_a_security_not_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An entry without address for an ISIN the bundle doesn't hold is unused,
    as any unused entry is."""
    fix_xlsx_clock(monkeypatch)
    entry = f'\n[[fa.entities]]\nisin = "{_NOT_HELD_ISIN}"\nname = "{_NAME}"\n'
    control = _run(tmp_path, monkeypatch, capsys, "run", "", "robinhood-fa")
    run = _run(tmp_path, monkeypatch, capsys, "not-held", entry, "robinhood-fa")

    assert control.rc == 0
    _assert_same_run(run, control)


# ---- robinhood-all, robinhood-cg and robinhood-income -------------------------------


def test_robinhood_all_removes_every_output_and_keeps_the_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Schedule FA leg errors with the stop, the run exits 4, and of the
    complete run's files only the private guidance file is left."""
    complete = _run(tmp_path, monkeypatch, capsys, "complete", "", "robinhood-all")
    assert complete.rc == 0
    assert len(list(complete.out_dir.iterdir())) > 1

    run = _run(tmp_path, monkeypatch, capsys, "run", _ENTRY, "robinhood-all")

    legs = _legs(run.out)
    assert run.rc == 4
    assert (legs.pop("schedule-fa"), legs.pop("fa-a1")) == ("error", "error")
    assert legs == {
        leg: status
        for leg, status in _legs(complete.out).items()
        if leg not in {"schedule-fa", "fa-a1"}
    }
    assert all(line in run.out for line in _STOP.splitlines())
    assert [path.name for path in run.out_dir.iterdir()] == [_GUIDANCE]
    path = run.out_dir / _GUIDANCE
    assert path.read_bytes() == f"{_HEADER}\n{_GLEIF_BLOCK}".encode()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert_no_markers(run.out + run.err, _ISIN, _NAME, _FOUND_ADDRESS)


def test_one_guidance_file_holds_the_identity_and_address_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With the security also missing from the identity table, the one
    guidance file holds the identity section, then the address section."""
    run_dir = tmp_path / "run"
    bundle = _bundle(monkeypatch, run_dir, _ENTRY, unconfigured=True)
    identity = identity_failure_guidance(bundle)
    assert identity.startswith(IDENTITY_GUIDANCE_HEADER)

    run = _invoke(run_dir, capsys, "robinhood-all")

    assert run.rc == 4
    assert [path.name for path in run.out_dir.iterdir()] == [_GUIDANCE]
    path = run.out_dir / _GUIDANCE
    assert path.read_bytes() == f"{identity}\n{_HEADER}\n{_GLEIF_BLOCK}".encode()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert_no_markers(run.out + run.err, _ISIN, _NAME, _FOUND_ADDRESS)


@pytest.mark.parametrize("command", ["robinhood-cg", "robinhood-income"])
def test_cg_and_income_run_as_without_the_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """Neither files Table A3, so the entry changes nothing: the same exit,
    streams and file bytes, and no guidance file."""
    fix_xlsx_clock(monkeypatch)
    control = _run(tmp_path, monkeypatch, capsys, "run", "", command)
    run = _run(tmp_path, monkeypatch, capsys, "entry", _ENTRY, command)

    _assert_same_run(run, control)
    assert not (run.out_dir / _GUIDANCE).exists()


# ---- #1568: no nature found for the entry either -------------------------------------


def _resolved_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    label: str,
    entries: str,
    command: str,
    legal_form: str,
) -> _Run:
    """``command`` on the #1243 bundle with ``entries`` appended to its
    user.toml, its A3 presentation evidence resolved by the pipeline from one
    active GLEIF record for the held ISIN: an address, ZIP 99999 and
    ``legal_form`` (``9999`` gives no nature). No stub supplies a nature."""
    run_dir = tmp_path / label
    run_dir.mkdir()
    stub = load_two_accounts(monkeypatch, run_dir, _buy("2"))
    config_path = run_dir / "user.toml"
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write(entries)
    config = load_user_config(config_path)
    (security,) = stub.robinhood.securities
    monkeypatch.setattr(entities, "_held_cusips", lambda *_args: frozenset({security.cusip}))
    for loader in ("load_robinhood_adr_entity_evidence", "load_robinhood_fund_entity_evidence"):
        monkeypatch.setattr(
            entities, loader, lambda *_args, **_kwargs: RegistrantLookup((), 0, 0, 0)
        )
    record = _record(legal_form_id=legal_form, postal_code="99999")
    monkeypatch.setattr(
        entities,
        "load_robinhood_legal_entity_evidence",
        partial(load_robinhood_legal_entity_evidence, opener=_RecordingOpener(_response([record]))),
    )
    presentation = entities.resolve_robinhood_presentation_evidence(
        robinhood=stub.robinhood,
        securities=stub.robinhood.securities,
        configured=config.fa.entities,
        observed=(),
        manual_fields={},
        identity_sources=None,
        quantities=stub.quantities,
        calendar_year=2025,
        cache_dir=run_dir / "cache",
        fetch=True,
    )
    bundle = RobinhoodBundle(
        config=config,
        robinhood=stub.robinhood,
        securities=stub.securities,
        paired=stub.paired,
        identities=cast("RobinhoodSecurityIdentityResult", stub.identities),
        packets=stub.packets,
        quantities=stub.quantities,
        scope=stub.scope,
        automatic_entity_overrides=presentation.entity_overrides,
        unverified_entity_addresses=presentation.unverified_addresses,
        legal_address_audit=presentation.legal_address_audit,
        automatic_nature_evidence=presentation.nature_evidence,
        nature_audit=presentation.nature_audit,
    )
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    return _invoke(run_dir, capsys, command)


# The block for _ENTRY when _resolved_run's GLEIF record gives a nature.
_RESOLVED_BLOCK = _GLEIF_BLOCK.replace(
    _FOUND_ADDRESS,
    "C/O SAMPLE REGISTERED AGENT, SAMPLE AGENT CENTER, 99 SAMPLE ST, SAMPLETON, US-DE, US",
).replace(_FOUND_ZIP, "99999")


@pytest.mark.parametrize("command", ["robinhood-fa", "robinhood-all"])
def test_following_the_block_files_the_nature_it_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """GLEIF finds the address but legal form 9999 gives no nature. The block,
    followed as the stop says (address and zip uncommented, nature_of_entity
    set), clears the holding on one rerun. On 51a30f8 neither named the
    nature, so the rerun ended MANUAL for a blank Nature of entity."""
    stopped = _resolved_run(tmp_path, monkeypatch, capsys, "stopped", _ENTRY, command, "9999")
    assert stopped.rc == 4
    block = (stopped.out_dir / _GUIDANCE).read_text(encoding="utf-8").partition(_HEADER)[2]
    followed = (
        block.replace("# address = ", "address = ")
        .replace("# zip = ", "zip = ")
        .replace(_NATURE_PLACEHOLDER, 'nature_of_entity = "Company"\n')
    )

    run = _resolved_run(tmp_path, monkeypatch, capsys, "followed", followed, command, "9999")

    assert run.rc == 0
    assert "a3-identity" not in run.out + run.err
    rows = _table(run.out_dir / "schedule-fa-a3.csv")
    assert [row[rows[0].index("Nature of entity")] for row in rows[1:]] == ["Company"]
    named = f'name = "{_NAME}"\n'
    assert block == "\n" + _RESOLVED_BLOCK.replace(named, named + _NATURE_PLACEHOLDER)
    stop = stopped.out + stopped.err
    assert all(line in stop for line in _NATURE_STOP.splitlines())
    assert_no_markers(stop, _ISIN, _NAME)


@pytest.mark.parametrize("command", ["robinhood-fa", "robinhood-all"])
def test_a_resolved_nature_keeps_the_stop_and_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """GLEIF's legal form gives a nature: the stop and block are 51a30f8's,
    with no nature_of_entity line."""
    run = _resolved_run(tmp_path, monkeypatch, capsys, "run", _ENTRY, command, "XTIQ")

    assert run.rc == 4
    assert all(line in run.out + run.err for line in _STOP.splitlines())
    assert "nature_of_entity" not in run.out + run.err
    path = run.out_dir / _GUIDANCE
    assert path.read_text(encoding="utf-8") == f"{_HEADER}\n{_RESOLVED_BLOCK}"


# ---- The guidance text -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("route", "note"),
    [
        (RobinhoodEntityRoute.LEGAL, "found by GLEIF (the ISIN's legal address)"),
        (
            RobinhoodEntityRoute.SEC,
            "found by SEC (the filer's business address, after GLEIF found no usable record)",
        ),
        (
            RobinhoodEntityRoute.ADR,
            "found by GLEIF (the legal address of the depositary its Form F-6 names)",
        ),
        (
            RobinhoodEntityRoute.FUND,
            "found by GLEIF (the legal address of the registrant its Form N-CEN names)",
        ),
    ],
)
def test_each_route_names_its_source(route: RobinhoodEntityRoute, note: str) -> None:
    """Each block names the route its address came from, by entry index, and
    writes each value as a TOML string that reads back unchanged."""
    entry = FaEntityOverride(isin=_ISIN, name='Tax "Payer"\tHoldings')
    item = RobinhoodUnverifiedEntityAddress(
        entry_index=2,
        entry=entry,
        found=RobinhoodFoundEntityAddress(route, "1 Sample Road\\Suite", "99999"),
        etf=False,
        nature_missing=False,
    )
    bundle = cast("RobinhoodBundle", type("Bundle", (), {"unverified_entity_addresses": (item,)}))

    assert address_verification_guidance(bundle) == (
        f"{_HEADER}\n"
        f"# fa.entities[2]: {note}\n"
        "[[fa.entities]]\n"
        f'isin = "{_ISIN}"\n'
        'name = "Tax \\"Payer\\"\\tHoldings"\n'
        '# address = "1 Sample Road\\\\Suite"\n'
        '# zip = "99999"\n'
        "prefer_configured_address = true\n"
    )


def test_no_unverified_address_writes_no_section() -> None:
    bundle = cast("RobinhoodBundle", type("Bundle", (), {"unverified_entity_addresses": ()}))

    assert address_verification_guidance(bundle) == ""


# ---- IBKR: an entry for a held ISIN still needs address --------------------------------


@pytest.fixture
def _flat_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", _flat_close_fetcher())
    fix_xlsx_clock(monkeypatch)


_IBKR_REFUSAL = (
    "ConfigError at field 'fa.entities[{index}].address': missing required field: IBKR "
    "Schedule FA looks up no issuer address, so an entry for an IBKR holding needs one"
)
_XQBE_ENTRY = (
    '[[fa.entities]]\nisin = "US99995D1000"\n'
    'address = "2 Sample Issuer Street, Sample City, XX"\nzip = "00002"\n'
)


@pytest.mark.usefixtures("_flat_closes")
@pytest.mark.parametrize("fmt", ["filing-csv", "cbdt-json-fragment"])
def test_schedule_fa_refuses_an_addressless_entry_for_a_held_isin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fmt: str
) -> None:
    """XQBE's entry (the second) without address and zip: exit 4, the field
    and reason on stderr, no Schedule FA file and no ISIN on any stream."""
    assert _FULL_CONFIG.count(_XQBE_ENTRY) == 1
    config = _FULL_CONFIG.replace(_XQBE_ENTRY, '[[fa.entities]]\nisin = "US99995D1000"\n')
    run = _schedule_fa(tmp_path, capsys, "run", "SAMPLE ISSUER BB INC", fmt, config=config)

    assert run.rc == 4
    assert run.out == ""
    assert run.err == f"error: {_IBKR_REFUSAL.format(index=1)}\n"
    assert list(run.out_dir.iterdir()) == []
    assert_no_markers(run.out + run.err, "US99995D1000")


@pytest.mark.usefixtures("_flat_closes")
@pytest.mark.parametrize("fmt", ["filing-csv", "cbdt-json-fragment"])
def test_schedule_fa_ignores_an_addressless_entry_for_an_isin_not_held(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fmt: str
) -> None:
    """One user.toml can serve both brokers: an entry IBKR doesn't hold
    changes no exit, stream or byte."""
    control = _schedule_fa(tmp_path, capsys, "control", "SAMPLE ISSUER BB INC", fmt)
    config = f'{_FULL_CONFIG}[[fa.entities]]\nisin = "{_NOT_HELD_ISIN}"\n'
    run = _schedule_fa(tmp_path, capsys, "run", "SAMPLE ISSUER BB INC", fmt, config=config)

    assert control.rc in {0, 3}
    _assert_same_run(run, control)


def _run_all(tmp_path: Path, capsys: pytest.CaptureFixture[str], label: str, entries: str) -> _Run:
    """``run-all --format review-csv`` with the NAV statement as ``--cy-input``
    and ``entries`` appended to its user.toml."""
    run_dir = tmp_path / label
    (run_dir / "out").mkdir(parents=True)
    argv = [*_run_all_argv(run_dir, _FY_2025_26_INPUT), "--cy-input", str(_NAV)]
    with (run_dir / "user.toml").open("a", encoding="utf-8") as handle:
        handle.write(entries)
    return _capture(run_dir, capsys, argv)


@pytest.mark.usefixtures("_flat_closes")
def test_run_all_refuses_its_schedule_fa_step_for_a_held_isin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """XQBB's entry without address: the Schedule FA step fails with the same
    refusal, the run exits 4 and removes every output. An entry for an ISIN
    IBKR doesn't hold changes no exit, stream or byte."""
    control = _run_all(tmp_path, capsys, "control", "")
    not_held = _run_all(
        tmp_path, capsys, "not-held", f'\n[[fa.entities]]\nisin = "{_NOT_HELD_ISIN}"\n'
    )
    run = _run_all(tmp_path, capsys, "run", '\n[[fa.entities]]\nisin = "US99995B1044"\n')

    assert control.rc in {0, 3}
    _assert_same_run(not_held, control)
    assert run.rc == 4
    streams = run.out + run.err
    assert streams.count(f"error: {_IBKR_REFUSAL.format(index=0)}\n") == 1
    assert "  ERROR        schedule-fa       (no file)\n                 -> failed (exit 4)" in (
        streams
    )
    assert _files(run.out_dir) == []
    assert_no_markers(streams, "US99995B1044")
