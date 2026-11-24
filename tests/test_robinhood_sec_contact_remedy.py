"""#1526: the SEC lookups name robinhood.sec_user_agent when the run's SEC
fair-access contact is missing or would be refused.

The SEC identity snapshot, Form F-6 and N-CEN caches are rebuilt from SEC, and
an SEC fetch needs that contact. On 338b855 their cache refusals said only to
move the folder aside and re-run. A filer without a contact who did exactly
that got a fetch that failed the contact check, counted as
``f6-source-failure`` or ``ncen-source-failure`` (or a snapshot reason naming
no key), and Schedule FA stopped at new Table A3 identity tasks.

The recovery and cold-cache lookups under test go through the real contact
check (``sec_source_opener``, or ``load_identity_sources`` with no injected
opener). The urllib transport below that check is replaced; cache setup
reuses the existing seed helpers, which inject openers.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import date
from functools import partial
from pathlib import Path
from typing import NamedTuple, NoReturn

import pytest

import ibkr_fa.pipeline_robinhood_entities as entities
import ibkr_fa.robinhood_adr_entity_sources as adr_sources
import ibkr_fa.robinhood_fund_entity_sources as fund_sources
import ibkr_fa.robinhood_identity_sources as identity
import tests._robinhood_adr as adr_helpers
from ibkr_fa.compute.robinhood_entity_overrides import (
    resolve_robinhood_adr_entity_overrides,
    resolve_robinhood_fund_entity_overrides,
)
from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.robinhood_adr_entity_sources import load_robinhood_adr_entity_evidence
from ibkr_fa.robinhood_fund_entity_sources import load_robinhood_fund_entity_evidence
from ibkr_fa.robinhood_identity_sources import SecEntityCacheError
from tests._entity_cache import break_only_artifact as _broken
from tests._entity_cache_remedies import SET_SEC_CONTACT
from tests._entity_source_texts import IDENTITY_CACHE_MALFORMED, NCEN_SOURCE_FAILURE
from tests._entity_transport import blocked_opener as _no_gleif_request
from tests._no_echo import assert_no_markers
from tests._robinhood_adr import F6_URL as _F6_URL
from tests._robinhood_adr import adr_observed as _observed
from tests._robinhood_adr import adr_security as _adr
from tests._robinhood_adr import assert_f6_refusal as _assert_f6_refusal
from tests._robinhood_adr import cached_f6 as _cached_f6
from tests._robinhood_adr import f6_document as _f6_document
from tests._robinhood_entity_stubs import stub_zero_match_sources
from tests._robinhood_fund import FUND_CIK as _FUND_CIK
from tests._robinhood_fund import assert_ncen_refusal as _assert_ncen_refusal
from tests._robinhood_fund import cached_ncen as _cached_fund
from tests._robinhood_fund import fund_identity_sources as _fund_sources
from tests._robinhood_fund import fund_index_responses as _index_responses
from tests._robinhood_fund import fund_registrant_reply as _gleif_response
from tests._robinhood_fund import fund_security as _etf
from tests._robinhood_fund import ncen_document_url as _document_url
from tests._robinhood_fund import ncen_filing as _filing
from tests._robinhood_fund import ncen_xml as _ncen_xml
from tests._robinhood_identity_inputs import ISSUER_AQ_ISIN as _STOCK_ISIN
from tests._robinhood_identity_sources import snapshot_refusal as _snapshot_refusal
from tests._robinhood_identity_sources import source_responses as _snapshot_sources
from tests._robinhood_presentation import ISSUER_AQ_OBSERVED as _STOCK_OBSERVED
from tests._robinhood_presentation import ISSUER_AQ_SECURITY as _STOCK
from tests._robinhood_presentation import PRESENTATION_QUANTITIES as _QUANTITIES

_CUTOFF = date(2025, 12, 31)
# What a filer following the refusal sets.
_CONTACT = "Tax Payer taxpayer@example.com"
_DEPOSITARY = "SAMPLE DEPOSITARY BANK"
_DEPOSITARY_LEI = "999900SAMPLE00000455"
# robinhood.sec_user_agent in a run without a usable contact: unset, or a value
# the config loader accepts but the contact check refuses (no email address).
_UNUSABLE = [pytest.param(None, id="unset"), pytest.param("Tax Payer", id="no-email")]
_NCEN_FILING = _filing("0009991009-25-000024", "2025-03-31")

# What one run of a lookup finds: the cells it resolves, and its warnings (the
# snapshot's audit reason, if any).
type _Found = tuple[tuple[object, ...], tuple[str, ...]]


class _Transport:
    """The real fetch's urllib call: answers each URL from ``responses`` and
    records the User-Agent each request declared."""

    def __init__(self, responses: Mapping[str, bytes]) -> None:
        self.responses = dict(responses)
        self.agents: dict[str, str | None] = {}

    def __call__(self, request: urllib.request.Request, _timeout: float) -> tuple[str, bytes]:
        url = request.full_url
        self.agents[url] = request.get_header("User-agent")
        if url not in self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return url, self.responses[url]


def _no_request(request: urllib.request.Request, _timeout: float) -> NoReturn:
    raise AssertionError(f"no request is allowed here: {request.full_url}")


def _f6(cache_dir: Path, *, fetch: bool, contact: str | None) -> _Found:
    """The held ADR's lookup, its GLEIF registrant only from the cache."""
    lookup = load_robinhood_adr_entity_evidence(
        (_adr(),),
        (_observed(),),
        (),
        cutoff=_CUTOFF,
        cache_dir=cache_dir,
        fetch=fetch,
        sec_user_agent=contact,
        gleif_opener=_no_gleif_request,
    )
    overrides, records, audit = resolve_robinhood_adr_entity_overrides((_adr(),), lookup)
    return (overrides, records), audit.warnings


def _ncen(cache_dir: Path, *, fetch: bool, contact: str | None) -> _Found:
    """The held ETF's lookup, its GLEIF registrant only from the cache."""
    lookup = load_robinhood_fund_entity_evidence(
        (_etf(),),
        _fund_sources(),
        cutoff=_CUTOFF,
        cache_dir=cache_dir,
        fetch=fetch,
        sec_user_agent=contact,
        gleif_opener=_no_gleif_request,
    )
    overrides, evidence, audit = resolve_robinhood_fund_entity_overrides((_etf(),), lookup)
    return (overrides, evidence), audit.warnings


def _snapshot(cache_dir: Path, *, fetch: bool, contact: str | None) -> _Found:
    """Through the production caller, for a held US common stock that GLEIF
    doesn't resolve: the snapshot search, then a fetch if it finds none."""
    presentation = entities.resolve_robinhood_presentation_evidence(
        robinhood=RobinhoodConfig(accounts=(), sec_user_agent=contact),
        securities=(_STOCK,),
        configured=(),
        observed=_STOCK_OBSERVED,
        manual_fields={},
        identity_sources=None,
        quantities=_QUANTITIES,
        calendar_year=2025,
        cache_dir=cache_dir,
        fetch=fetch,
    )
    audit = presentation.identity_source_audit
    failure = () if audit is None or audit.failure is None else (audit.failure,)
    return (presentation.entity_overrides, presentation.nature_evidence), failure


def _seed_snapshot(cache_dir: Path) -> Path:
    """A fetching run with a contact writes the covering snapshot."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(identity, "_url_open", _Transport(_snapshot_sources()))
        _snapshot(cache_dir, fetch=True, contact=_CONTACT)
    (path,) = (item for item in (cache_dir / "robinhood-identities").rglob("*") if item.is_file())
    return path


def _f6_refusal(message: str, cache_dir: Path, path: Path, **kwargs: bool) -> None:
    _assert_f6_refusal(message, cache_dir, path, "ADR depositary cache is unreadable", **kwargs)


def _ncen_refusal(message: str, cache_dir: Path, path: Path, **kwargs: bool) -> None:
    _assert_ncen_refusal(message, cache_dir, path, "N-CEN selection cache is unreadable", **kwargs)


def _snapshot_message(message: str, cache_dir: Path, _path: Path, **kwargs: bool) -> None:
    assert message == _snapshot_refusal(cache_dir, IDENTITY_CACHE_MALFORMED, **kwargs)


class _Cache(NamedTuple):
    """One SEC lookup cache: its folder, how a fetching run fills it (returns
    the artifact), the run, the whole refusal, and what its fetch requests."""

    folder: str
    seed: Callable[[Path], Path]
    run: Callable[..., _Found]
    assert_refusal: Callable[..., None]
    responses: Callable[[], Mapping[str, bytes]]


_CACHES = [
    pytest.param(
        _Cache(
            "robinhood-adr-entities",
            _cached_f6,
            _f6,
            _f6_refusal,
            lambda: {_F6_URL: _f6_document(depositary=_DEPOSITARY)},
        ),
        id="f6",
    ),
    pytest.param(
        _Cache(
            "robinhood-fund-entities",
            _cached_fund,
            _ncen,
            _ncen_refusal,
            lambda: _index_responses(
                [_NCEN_FILING],
                {_document_url(_FUND_CIK, _NCEN_FILING["accessionNumber"]): _ncen_xml()},
            ),
        ),
        id="ncen",
    ),
    pytest.param(
        _Cache(
            "robinhood-identities",
            _seed_snapshot,
            _snapshot,
            _snapshot_message,
            _snapshot_sources,
        ),
        id="snapshot",
    ),
]


@pytest.fixture(autouse=True)
def _only_the_lookup_under_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """For the production caller, the ADR and ETF lookups find nothing and
    GLEIF finds no record for the stock; no request leaves unless a test
    answers it."""
    stub_zero_match_sources(monkeypatch, _STOCK.cusip, _STOCK_ISIN)
    monkeypatch.setattr(identity, "_url_open", _no_request)
    # Keep the reused ADR seed and the tested fetch synthetic, including the depositary.
    monkeypatch.setattr(
        adr_sources, "_DEPOSITARY_LEIS", {("SAMPLE", "DEPOSITARY", "BANK"): _DEPOSITARY_LEI}
    )
    monkeypatch.setattr(adr_helpers, "ADR_DEPOSITARY_LEI", _DEPOSITARY_LEI)
    monkeypatch.setattr(adr_helpers, "f6_document", partial(_f6_document, depositary=_DEPOSITARY))
    monkeypatch.setattr(
        adr_helpers,
        "depositary_gleif_reply",
        partial(_gleif_response, lei=_DEPOSITARY_LEI, legal_name=_DEPOSITARY, category="GENERAL"),
    )


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize(
    ("contact", "set_contact"),
    [
        pytest.param(None, True, id="unset"),
        pytest.param("Tax Payer", True, id="no-email"),
        pytest.param(_CONTACT, False, id="valid"),
    ],
)
@pytest.mark.parametrize("cache", _CACHES)
def test_refusal_names_the_contact_only_without_a_usable_one(
    tmp_path: Path, cache: _Cache, contact: str | None, set_contact: bool, fetch: bool
) -> None:
    """With a usable contact the refusal is the one 338b855 printed; without
    one its recovery step also says to set the key. It never prints the
    configured value."""
    path = cache.seed(tmp_path)
    _broken(tmp_path / cache.folder)

    with pytest.raises(SecEntityCacheError) as refusal:
        cache.run(tmp_path, fetch=fetch, contact=contact)

    message = str(refusal.value)
    cache.assert_refusal(message, tmp_path, path, fetch=fetch, set_contact=set_contact)
    if contact is not None:
        assert_no_markers(message, contact)


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize("contact", _UNUSABLE)
@pytest.mark.parametrize("cache", _CACHES)
def test_following_the_refusal_recovers_the_cached_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cache: _Cache,
    contact: str | None,
    fetch: bool,
) -> None:
    """A run without a usable contact refuses over the damaged cache. Doing
    what the refusal says (set the contact if it names robinhood.sec_user_agent,
    move the folder aside, re-run, dropping --no-fetch-prices if it says to)
    fetches through the real contact check and finds the identity the cache
    held, with no warning. On 338b855 the rerun's fetch failed the contact
    check."""
    cache.seed(tmp_path)
    cells, warnings = cache.run(tmp_path, fetch=False, contact=None)
    assert cells != ((), ())
    assert warnings == ()
    _broken(tmp_path / cache.folder)

    with pytest.raises(SecEntityCacheError) as refusal:
        cache.run(tmp_path, fetch=fetch, contact=contact)
    message = str(refusal.value)
    if "robinhood.sec_user_agent" in message:
        contact = _CONTACT
    (tmp_path / cache.folder).rename(tmp_path / "aside")
    transport = _Transport(cache.responses())
    monkeypatch.setattr(identity, "_url_open", transport)
    rerun = cache.run(
        tmp_path, fetch=fetch or "without --no-fetch-prices" in message, contact=contact
    )

    assert rerun == (cells, ())
    sec_agents = {agent for url, agent in transport.agents.items() if ".sec.gov/" in url}
    assert sec_agents == {_CONTACT}


# --- A fetch with no cache --------------------------------------------------


@pytest.mark.parametrize("contact", _UNUSABLE)
def test_fetch_without_a_usable_contact_names_the_key(tmp_path: Path, contact: str | None) -> None:
    """With no cache and fetching on, each SEC lookup fails the contact check
    before any request, and its warning or audit reason names the key. On
    338b855 the warnings counted f6-source-failure and ncen-source-failure,
    and the snapshot reason named no key."""
    f6 = _f6(tmp_path, fetch=True, contact=contact)
    ncen = _ncen(tmp_path, fetch=True, contact=contact)
    snapshot = _snapshot(tmp_path, fetch=True, contact=contact)

    assert f6 == (
        ((), ()),
        (
            "ADR depositary identity remains manual for 1 securities (f6-missing-contact:1); "
            f"to fetch from SEC, {SET_SEC_CONTACT}",
        ),
    )
    assert ncen == (
        ((), ()),
        (
            "ETF registrant identity remains manual for 1 securities (ncen-missing-contact:1); "
            f"to fetch from SEC, {SET_SEC_CONTACT}",
        ),
    )
    assert snapshot == (
        ((), ()),
        (
            "a declared SEC fair-access contact (User-Agent) is required to fetch identity "
            f"sources; {SET_SEC_CONTACT}",
        ),
    )
    if contact is not None:
        assert_no_markers(" ".join((*f6[1], *ncen[1], *snapshot[1])), contact)


def test_ncen_cache_construction_failure_keeps_its_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-contact failure while constructing the source cache keeps the
    original source-failure warning, including with a usable contact."""

    def allocation_failed(_self: object, _opener: identity.IdentitySourceOpener) -> NoReturn:
        raise MemoryError("synthetic allocation failure")

    monkeypatch.setattr(fund_sources._SourceCache, "__init__", allocation_failed)

    assert _ncen(tmp_path, fetch=True, contact=_CONTACT) == (
        ((), ()),
        (NCEN_SOURCE_FAILURE,),
    )


def test_network_failure_with_a_usable_contact_keeps_its_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a usable contact, an SEC request that fails keeps 338b855's
    warnings and snapshot reason, which name no key."""
    nasdaq = _snapshot_sources()[identity.NASDAQ_TRADED_URL]

    def sec_unreachable(request: urllib.request.Request, _timeout: float) -> tuple[str, bytes]:
        if ".sec.gov/" in request.full_url:
            raise urllib.error.URLError("synthetic outage")
        assert request.full_url == identity.NASDAQ_TRADED_URL
        return request.full_url, nasdaq

    monkeypatch.setattr(identity, "_url_open", sec_unreachable)
    # No pause between the retries.
    monkeypatch.setattr(
        identity, "_fetch_bytes", partial(identity._fetch_bytes, sleep=lambda _seconds: None)
    )

    assert _f6(tmp_path, fetch=True, contact=_CONTACT) == (
        ((), ()),
        ("ADR depositary identity remains manual for 1 securities (f6-source-failure:1)",),
    )
    assert _ncen(tmp_path, fetch=True, contact=_CONTACT) == (
        ((), ()),
        (NCEN_SOURCE_FAILURE,),
    )
    assert _snapshot(tmp_path, fetch=True, contact=_CONTACT) == (
        ((), ()),
        ("the SEC ticker/exchange dataset could not be retrieved",),
    )
