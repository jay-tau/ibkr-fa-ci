"""#1458, #1465, #1521: each lookup-skip remedy, followed word for word, loads,
skips the lookup that refused, and files every Table A3 identity cell; and with
fetching off, following a move-aside remedy recovers the identity the cache held.

Since #1449 a Robinhood ``[[fa.entities]]`` entry may leave out ``address``,
but ``prefer_configured_address = true`` needs one, and a held ETF's entry
without one stops Schedule FA. On 0105ee4 the remedies named the flags,
address and zip but not name or nature_of_entity, so an entry with exactly
those keys skipped the lookup and then left Schedule FA a manual task for
Nature of entity (and, for an ETF, its name). With fetching off, moving the
cache folder aside only turned it into a cache miss.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from functools import partial
from pathlib import Path
from typing import NamedTuple, cast

import pytest

import ibkr_fa.pipeline_robinhood_entities as entities
from ibkr_fa.compute.robinhood_entity_overrides import RobinhoodFundEntityAudit
from ibkr_fa.config import FaEntityOverride, RobinhoodConfig, load_user_config
from ibkr_fa.models.reference_identity import GleifLegalAddressLookup, RegistrantLookup
from ibkr_fa.normalize.robinhood import RobinhoodSecurityQuantityResult
from ibkr_fa.robinhood_adr_entity_sources import load_robinhood_adr_entity_evidence
from ibkr_fa.robinhood_fund_entity_sources import load_robinhood_fund_entity_evidence
from ibkr_fa.robinhood_identity_sources import IdentitySourceAudit, SecEntityCacheError
from ibkr_fa.robinhood_legal_entity_sources import (
    GleifLegalAddressError,
    load_gleif_registrant_by_lei,
    load_robinhood_legal_entity_evidence,
)
from ibkr_fa.types import Isin
from tests._entity_cache import break_only_artifact as _broken
from tests._entity_cache_remedies import MOVE_ASIDE, MOVE_ASIDE_SET_CONTACT
from tests._entity_transport import FixedReplyOpener as _RecordingOpener
from tests._entity_transport import RecordingOpener as _FundOpener
from tests._entity_transport import blocked_opener as _blocked
from tests._fa_a3_name_runs import output_table as _table
from tests._fa_a3_name_runs import robinhood_all_legs as _legs
from tests._robinhood_adr import ADR_ISIN as _ADR_ISIN
from tests._robinhood_adr import AdrOpener as _Opener
from tests._robinhood_adr import adr_observed as _observed
from tests._robinhood_adr import adr_security as _security
from tests._robinhood_adr import cached_f6 as _cached_f6
from tests._robinhood_adr import resolve_adr as _resolve
from tests._robinhood_entity_stubs import stub_zero_match_sources
from tests._robinhood_fa_address_runs import ADDRESS_GUIDANCE_HEADER as _HEADER
from tests._robinhood_fa_address_runs import address_verification_run as _stopped_run
from tests._robinhood_fa_inputs import FA_ISIN as _HELD_ISIN
from tests._robinhood_fa_remedy_runs import named_entity_keys as _named_keys
from tests._robinhood_fa_remedy_runs import run_with_entity_block as _filed_run
from tests._robinhood_fund import FUND_CIK as _FUND_CIK
from tests._robinhood_fund import FUND_LEI as _FUND_LEI
from tests._robinhood_fund import cached_ncen as _cached_fund
from tests._robinhood_fund import fund_identity_sources as _fund_sources
from tests._robinhood_fund import fund_index_responses as _index_responses
from tests._robinhood_fund import fund_registrant_reply as _gleif_response
from tests._robinhood_fund import fund_security as _fund_security
from tests._robinhood_fund import ncen_document_url as _document_url
from tests._robinhood_fund import ncen_filing as _filing
from tests._robinhood_fund import ncen_xml as _ncen_xml
from tests._robinhood_fund import resolve_cached_fund as _resolve_cached
from tests._robinhood_fund import resolve_fund as _resolve_fund
from tests._robinhood_gleif import GLEIF_LEI as _GLEIF_LEI
from tests._robinhood_gleif import cached_registrant as _cached_registrant
from tests._robinhood_gleif import gleif_security as _gleif_security
from tests._robinhood_gleif import issuer_reply as _response
from tests._robinhood_gleif import load_gleif as _load_gleif
from tests._robinhood_gleif import registrant_reply as _registrant_response
from tests._robinhood_identity_inputs import ISSUER_AQ_ISIN as _STOCK_ISIN
from tests._robinhood_identity_sources import load_snapshot as _load_snapshot
from tests._robinhood_presentation import ISSUER_AQ_SECURITY as _STOCK
from tests._robinhood_presentation import resolve_presentation as _presentation
from tests._robinhood_security import IDENTITY_GUIDANCE_FILE as _GUIDANCE

_ISIN = Isin("US99995B1044")
_ETF = _fund_security(cusip="99995B104", isin=_ISIN)
_CUTOFF = date(2025, 12, 31)
_ADDRESS = "1 Example Plaza, Anytown, DE"
# What a filer adds for each key a remedy names.
_FOLLOWED = {
    "name": '"Example Holdings Inc."',
    "address": f'"{_ADDRESS}"',
    "zip": '"99999"',
    "nature_of_entity": '"Company"',
    "prefer_configured_address": "true",
    "prefer_configured_name": "true",
    "prefer_configured_nature": "true",
}
# The A3 cells those keys file: name, address, ZIP and nature.
_CELLS = ("Example Holdings Inc.", _ADDRESS, "99999", "Company")
_ACCOUNT = '[account]\nholder_name = "Tax Payer"\n'

type _Refuse = Callable[[Path, pytest.MonkeyPatch], object]


# --- Each refusal, with fetching off as under --no-fetch-prices ------------------


def _gleif(cache_dir: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    load_gleif_registrant_by_lei("bad", cache_dir=cache_dir, fetch=False, opener=_blocked)


def _gleif_isin(cache_dir: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    _load_gleif(cache_dir)
    _broken(cache_dir / "robinhood-legal-entities")
    _load_gleif(cache_dir, fetch=False, opener=_blocked)


def _gleif_registrant(cache_dir: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    _cached_registrant(cache_dir).write_bytes(b"not json")
    load_gleif_registrant_by_lei(_GLEIF_LEI, cache_dir=cache_dir, fetch=False, opener=_blocked)


def _f6(cache_dir: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    _cached_f6(cache_dir).write_bytes(b"not json")
    _resolve(_security(), cache_dir, fetch=False, sec_opener=_blocked, gleif_opener=_blocked)


def _snapshot(cache_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the production caller, which knows whether the run fetches."""
    stub_zero_match_sources(monkeypatch, _STOCK.cusip, _STOCK_ISIN)
    _load_snapshot(cache_dir)
    _broken(cache_dir / "robinhood-identities")
    _presentation(cache_dir, fetch=False)


def _ncen(cache_dir: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    _cached_fund(cache_dir).write_bytes(b"not json")
    load_robinhood_fund_entity_evidence(
        (_ETF,),
        _fund_sources(),
        cutoff=_CUTOFF,
        cache_dir=cache_dir,
        fetch=False,
        sec_user_agent=None,
        sec_opener=_blocked,
        gleif_opener=_blocked,
    )


def _remedy(tmp_path: Path, refuse: _Refuse, monkeypatch: pytest.MonkeyPatch) -> str:
    """Raise the real refusal over ``tmp_path / "cache"``; return its text
    after the cache folder it names (all of it if it names none)."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    with pytest.raises((GleifLegalAddressError, SecEntityCacheError)) as refusal:
        refuse(cache_dir, monkeypatch)
    return str(refusal.value).rpartition(str(cache_dir))[2]


def _added(remedy: str) -> str:
    """Each key ``remedy`` names, set as a filer following it would."""
    return "".join(
        f"{key} = {value}\n" for key, value in _FOLLOWED.items() if re.search(rf"\b{key}\b", remedy)
    )


def _followed(
    tmp_path: Path, refuse: _Refuse, monkeypatch: pytest.MonkeyPatch, isin: Isin = _ISIN
) -> FaEntityOverride:
    """Load an entry holding ``isin`` and only the keys the refusal's remedy
    names; before #1521 the entry also held a reviewed name and nature."""
    path = tmp_path / "user.toml"
    added = _added(_remedy(tmp_path, refuse, monkeypatch))
    path.write_text(f'{_ACCOUNT}\n[[fa.entities]]\nisin = "{isin}"\n{added}', encoding="utf-8")
    (entry,) = load_user_config(path).fa.entities
    return entry


@pytest.mark.parametrize(
    ("refuse", "flags"),
    [
        pytest.param(_gleif, True, id="gleif-lei"),
        pytest.param(_gleif_isin, True, id="gleif-isin"),
        pytest.param(_gleif_registrant, True, id="gleif-registrant"),
        pytest.param(_f6, True, id="f6"),
        pytest.param(_snapshot, True, id="snapshot"),
        pytest.param(_ncen, False, id="ncen"),
    ],
)
def test_remedy_followed_word_for_word_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refuse: _Refuse, flags: bool
) -> None:
    """On be2d3f1 each flag remedy named only the three flags, and
    prefer_configured_address = true then refused. On 9d8258e the N-CEN
    remedy named no key, so its entry kept no address. On 0105ee4 none named
    name or nature_of_entity."""
    assert _followed(tmp_path, refuse, monkeypatch) == FaEntityOverride(
        isin=_ISIN,
        address=_ADDRESS,
        zip_code="99999",
        name="Example Holdings Inc.",
        nature_of_entity="Company",
        prefer_configured_address=flags,
        prefer_configured_nature=flags,
        prefer_configured_name=flags,
    )


# --- The followed entry skips the lookup ------------------------------------------


def test_followed_gleif_remedy_skips_the_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fed back over the same corrupt cache, with fetching on and the opener
    blocked, the followed entry neither refuses nor requests anything."""
    entry = _followed(tmp_path, _gleif_isin, monkeypatch, _gleif_security().instrument.isin)

    lookup = load_robinhood_legal_entity_evidence(
        (_gleif_security(),), (entry,), cache_dir=tmp_path / "cache", fetch=True, opener=_blocked
    )

    assert lookup == GleifLegalAddressLookup((), {}, 0)


def test_followed_f6_remedy_skips_the_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fed back over the same corrupt cache, with fetching on and every opener
    blocked, the followed entry neither refuses nor requests anything. Before
    #1465 no test passed this loader a configured entry."""
    entry = _followed(tmp_path, _f6, monkeypatch, _ADR_ISIN)

    lookup = load_robinhood_adr_entity_evidence(
        (_security(),),
        (_observed(),),
        (entry,),
        cutoff=_CUTOFF,
        cache_dir=tmp_path / "cache",
        fetch=True,
        sec_user_agent="test@example.com",
        sec_opener=_blocked,
        gleif_opener=_blocked,
    )

    assert lookup == RegistrantLookup((), 0, 0, 0)


def test_followed_snapshot_remedy_skips_the_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fed back over the same corrupt snapshot, with fetching on, the common
    stock's followed entry leaves no SEC candidate, so the snapshot isn't
    read and no automatic cell is filed."""
    entry = _followed(tmp_path, _snapshot, monkeypatch, _STOCK_ISIN)
    monkeypatch.setattr(
        entities,
        "load_identity_sources",
        lambda *_args, **_kwargs: pytest.fail("the followed entry must not fetch"),
    )

    presentation = _presentation(tmp_path / "cache", configured=(entry,), fetch=True)

    assert presentation.entity_overrides == ()
    assert presentation.nature_evidence == ()
    assert presentation.identity_source_audit is None


def test_followed_ncen_remedy_skips_the_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline, not the N-CEN loader, drops a configured ETF. Fed back
    over the same corrupt cache, with fetching on and the loader's openers
    blocked, the followed entry neither refuses nor requests anything, and it
    leaves Schedule FA no entry without an address to stop on (#1449)."""
    entry = _followed(tmp_path, _ncen, monkeypatch)
    monkeypatch.setattr(entities, "_held_cusips", lambda *_args: frozenset({_ETF.cusip}))
    # The fund's SEC class, as a post-FY snapshot gives it, so that a lookup
    # would reach the corrupt cache.
    monkeypatch.setattr(
        entities,
        "load_post_fy_identity_sources",
        lambda *_args, **_kwargs: (
            _fund_sources(),
            IdentitySourceAudit("f" * 64, True, None, tmp_path / "sources.json"),
        ),
    )
    monkeypatch.setattr(
        entities,
        "load_robinhood_fund_entity_evidence",
        partial(load_robinhood_fund_entity_evidence, sec_opener=_blocked, gleif_opener=_blocked),
    )

    presentation = entities.resolve_robinhood_presentation_evidence(
        robinhood=RobinhoodConfig(accounts=()),
        securities=(_ETF,),
        configured=(entry,),
        observed=(),
        manual_fields={},
        identity_sources=None,
        quantities=cast("RobinhoodSecurityQuantityResult", object()),
        calendar_year=_CUTOFF.year,
        cache_dir=tmp_path / "cache",
        fetch=True,
    )

    assert presentation.fund_audit == RobinhoodFundEntityAudit(0, 0, 0, 0)
    assert presentation.unverified_addresses == ()


# --- The followed entry files Schedule FA ------------------------------------------


def _a3_cells(out_dir: Path) -> list[tuple[object, ...]]:
    """Each Table A3 row's Name, Address, ZIP and Nature of entity."""
    (path,) = out_dir.glob("schedule-fa-a3.*")
    header, *rows = _table(path)
    columns = [
        header.index(name)
        for name in ("Name of entity", "Address of entity", "ZIP Code", "Nature of entity")
    ]
    return [tuple(row[column] for column in columns) for row in rows]


@pytest.mark.parametrize("command", ["robinhood-fa", "robinhood-all"])
@pytest.mark.parametrize(
    ("refuse", "kind"),
    [
        # The invalid-LEI and registrant remedies are for each held ADR and ETF.
        pytest.param(_gleif, "adr", id="gleif-lei-adr"),
        pytest.param(_gleif, "etf", id="gleif-lei-etf"),
        pytest.param(_gleif_isin, "stock", id="gleif-isin"),
        pytest.param(_gleif_registrant, "adr", id="gleif-registrant-adr"),
        pytest.param(_gleif_registrant, "etf", id="gleif-registrant-etf"),
        pytest.param(_f6, "adr", id="f6"),
        # For each held US common stock GLEIF doesn't resolve, and each ETF.
        pytest.param(_snapshot, "stock", id="snapshot-stock"),
        pytest.param(_snapshot, "etf", id="snapshot-etf"),
        pytest.param(_ncen, "etf", id="ncen"),
    ],
)
def test_followed_remedy_files_every_identity_cell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    refuse: _Refuse,
    kind: str,
    command: str,
) -> None:
    """A held security of the remedy's kind, configured with only the keys
    the remedy names and no automatic evidence (the skipped lookups find
    none), reruns offline to exit 0: Schedule FA is ``ok``, with no
    ``a3-identity`` task, and its A3 row files the entry's four cells. On
    0105ee4 the entry had no nature_of_entity, nor an ETF's name, and the
    run exited 3 on an ``a3-identity`` task."""
    added = _added(_remedy(tmp_path, refuse, monkeypatch))
    block = f'\n[[fa.entities]]\nisin = "{_HELD_ISIN}"\n{added}'

    run = _filed_run(
        tmp_path, monkeypatch, capsys, "rerun", command, block=block,
        etf=kind == "etf", adr=kind == "adr",
    )  # fmt: skip

    assert run.rc == 0
    assert "a3-identity" not in run.out
    if command == "robinhood-all":
        assert _legs(run.out)["schedule-fa"] == "ok"
    else:
        assert run.out.startswith("Robinhood schedule-fa: ok\n")
    assert _a3_cells(run.out_dir) == [_CELLS]


def test_followed_etf_guidance_note_files_every_identity_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An ETF entry with no address stops Schedule FA and writes a guidance
    block whose note says nothing was looked up (#1449). Its address and zip
    lines uncommented and set, plus each key the note names that the block
    lacks, both commands rerun offline to exit 0 with every cell filed. On
    0105ee4 the note named only the address, and the rerun stopped on the
    ETF's name and nature."""
    entry = f'\n[[fa.entities]]\nisin = "{_HELD_ISIN}"\n'
    stopped = _stopped_run(
        tmp_path, monkeypatch, capsys, "stopped", entry, "robinhood-fa",
        automatic=(), held_etf=True,
    )  # fmt: skip
    assert stopped.rc == 4
    block = (
        (stopped.out_dir / _GUIDANCE).read_text(encoding="utf-8").removeprefix(_HEADER).lstrip("\n")
    )
    block = block.replace('# address = ""', f"address = {_FOLLOWED['address']}")
    block = block.replace('# zip = ""', f"zip = {_FOLLOWED['zip']}")
    note, *lines = block.splitlines()
    present = {line.partition(" = ")[0] for line in lines if " = " in line}
    followed = block + "".join(
        f"{key} = {_FOLLOWED[key]}\n" for key in _named_keys(note) if key not in present
    )

    for command in ("robinhood-fa", "robinhood-all"):
        run = _filed_run(
            tmp_path, monkeypatch, capsys, command, command, block="\n" + followed, etf=True
        )

        assert run.rc == 0, command
        assert "a3-identity" not in run.out
        assert _a3_cells(run.out_dir) == [_CELLS]


# --- #1521: the move-aside remedy, followed with fetching off --------------------


class _Cache(NamedTuple):
    """One lookup cache: its folder, and the identity a run finds for the
    held security with fetching on (every source served) or off."""

    folder: str
    identity: Callable[[Path, bool], object]


def _gleif_isin_identity(cache_dir: Path, fetch: bool) -> object:
    opener = _RecordingOpener(_response()) if fetch else _blocked
    return _load_gleif(cache_dir, fetch=fetch, opener=opener)[0]


def _gleif_registrant_identity(cache_dir: Path, fetch: bool) -> object:
    opener = _RecordingOpener(_registrant_response()) if fetch else _blocked
    return load_gleif_registrant_by_lei(
        _GLEIF_LEI, cache_dir=cache_dir, fetch=fetch, opener=opener
    )[0]


def _f6_identity(cache_dir: Path, fetch: bool) -> object:
    opener = _Opener() if fetch else _blocked
    return _resolve(_security(), cache_dir, fetch=fetch, sec_opener=opener, gleif_opener=opener)[:2]


def _ncen_identity(cache_dir: Path, fetch: bool) -> object:
    if not fetch:
        return _resolve_cached(cache_dir)[:2]
    selected = _filing("0009991009-25-000024", "2025-03-31")
    return _resolve_fund(
        cache_dir,
        fetch=True,
        sec_user_agent="test@example.com",
        sec_opener=_FundOpener(
            _index_responses(
                [selected], {_document_url(_FUND_CIK, selected["accessionNumber"]): _ncen_xml()}
            )
        ),
        gleif_opener=_FundOpener(
            {f"https://api.gleif.org/api/v1/lei-records/{_FUND_LEI}": _gleif_response()}
        ),
    )[:2]


def _snapshot_identity(cache_dir: Path, fetch: bool) -> object:
    """Through the production caller; a fetch writes the covering snapshot."""
    presentation = _presentation(cache_dir, fetch=fetch)
    return presentation.entity_overrides, presentation.nature_evidence


_CACHES = [
    pytest.param(_Cache("robinhood-legal-entities", _gleif_isin_identity), id="gleif-isin"),
    pytest.param(
        _Cache("robinhood-legal-entities", _gleif_registrant_identity), id="gleif-registrant"
    ),
    pytest.param(_Cache("robinhood-adr-entities", _f6_identity), id="f6"),
    pytest.param(_Cache("robinhood-fund-entities", _ncen_identity), id="ncen"),
    pytest.param(_Cache("robinhood-identities", _snapshot_identity), id="snapshot"),
]


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize("cache", _CACHES)
def test_followed_move_aside_remedy_recovers_the_cached_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache: _Cache, fetch: bool
) -> None:
    """A cache filled by a fetching run is damaged, and a run with ``fetch``
    refuses. Following the remedy's first route, the folder moved aside and
    the run repeated, dropping --no-fetch-prices only if the remedy says to,
    finds the same identity the cache held. On 0105ee4 the remedy said only
    to re-run, and with fetching off the rerun found nothing."""
    stub_zero_match_sources(monkeypatch, _STOCK.cusip, _STOCK_ISIN)
    # The production caller fetches the snapshot as load_identity_sources
    # does, with every source served.
    monkeypatch.setattr(
        entities,
        "load_identity_sources",
        lambda *_args, cache_dir, **_kwargs: _load_snapshot(cache_dir)[:2],
    )
    intact = cache.identity(tmp_path, True)
    assert intact not in (None, (), ((), ()))
    _broken(tmp_path / cache.folder)

    with pytest.raises((GleifLegalAddressError, SecEntityCacheError)) as refusal:
        cache.identity(tmp_path, fetch)
    message = str(refusal.value)
    # #1526: an SEC cache's remedy also names the contact when the run has none
    # (the snapshot's caller here, and the N-CEN cache-only run); the openers
    # this rerun injects skip the contact check.
    opening = MOVE_ASIDE_SET_CONTACT if "robinhood.sec_user_agent" in message else MOVE_ASIDE
    assert f"{tmp_path / cache.folder}. {opening[fetch]}, or" in message
    (tmp_path / cache.folder).rename(tmp_path / "aside")
    rerun = cache.identity(tmp_path, fetch or "without --no-fetch-prices" in message)

    assert rerun == intact
