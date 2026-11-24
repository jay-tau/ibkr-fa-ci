"""Exact-ISIN GLEIF legal-address cache checks."""

from __future__ import annotations

import errno
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa.compute.robinhood_entity_overrides import GleifLegalAddressAudit
from ibkr_fa.config import FaEntityOverride, RobinhoodSecurityConfig
from ibkr_fa.robinhood_legal_entity_sources import (
    REGISTRANT_SCHEMA_VERSION,
    SCHEMA_VERSION,
    GleifLegalAddressError,
    LegalAddressOpener,
    LegalAddressSourceError,
    _artifact_path,
    _registrant_artifact_path,
    load_gleif_registrant_by_lei,
    load_robinhood_legal_entity_evidence,
)
from ibkr_fa.types import Isin
from tests._crash_reports import assert_replaced_unchained
from tests._deep_json import DEEP_JSON
from tests._entity_cache import dangle as _dangle
from tests._entity_cache import reseal
from tests._entity_cache import reseal_file as _reseal_artifact
from tests._entity_cache_remedies import SET_IDENTITY_TOO
from tests._entity_source_texts import GLEIF_CACHE_INTEGRITY, GLEIF_HTTP_503
from tests._entity_transport import FixedReplyOpener as _RecordingOpener
from tests._entity_transport import blocked_opener as _blocked
from tests._no_echo import CUSIP, FILENAME, MARKERS, STRERROR, assert_no_markers
from tests._robinhood_gleif import CUSIP_ISIN as _CUSIP_ISIN
from tests._robinhood_gleif import CUSIP_SECURITY as _CUSIP_SECURITY
from tests._robinhood_gleif import GLEIF_ISIN as _ISIN
from tests._robinhood_gleif import GLEIF_LEI as _LEI
from tests._robinhood_gleif import cached_registrant as _cached_registrant
from tests._robinhood_gleif import exact_pattern as _exactly
from tests._robinhood_gleif import gleif_security as _security
from tests._robinhood_gleif import issuer_record as _record
from tests._robinhood_gleif import issuer_reply as _response
from tests._robinhood_gleif import legal_address_cache_refusal as _cache_refusal
from tests._robinhood_gleif import legal_address_fetch_refusal as _fetch_refusal
from tests._robinhood_gleif import legal_address_raw_type_refusal as _raw_type_refusal
from tests._robinhood_gleif import load_gleif as _load
from tests._robinhood_gleif import registrant_cache_refusal as _registrant_cache_refusal
from tests._robinhood_gleif import registrant_fetch_refusal as _registrant_fetch_refusal
from tests._robinhood_gleif import registrant_raw_type_refusal as _registrant_raw_type_refusal
from tests._robinhood_gleif import registrant_reply as _registrant_response
from tests._robinhood_gleif import reseal_record as _reseal
from tests._robinhood_gleif import with_raw as _with_raw
from tests._robinhood_security import robinhood_security


def test_exact_isin_retains_legal_form_provenance(tmp_path: Path) -> None:
    records = load_robinhood_legal_entity_evidence(
        (_security(),),
        (),
        cache_dir=tmp_path,
        fetch=True,
        opener=_RecordingOpener(_response()),
    ).records

    assert records[0].legal_form_id == "XTIQ"
    assert records[0].legal_form_other == ""


def test_exact_lei_registrant_uses_distinct_immutable_cache(tmp_path: Path) -> None:
    opener = _RecordingOpener(_registrant_response())

    record, cache_hit = load_gleif_registrant_by_lei(
        _LEI,
        cache_dir=tmp_path,
        fetch=True,
        opener=opener,
    )

    assert record is not None
    assert record.lei == _LEI
    assert record.legal_name == "SAMPLE ISSUER AG INC."
    assert cache_hit is False
    path = _registrant_artifact_path(tmp_path, _LEI)
    assert path.exists()
    assert _LEI not in str(path.parent)

    cached, cache_hit = load_gleif_registrant_by_lei(
        _LEI,
        cache_dir=tmp_path,
        fetch=False,
        opener=_blocked,
    )
    assert cached == record
    assert cache_hit is True


def test_exact_isin_uses_legal_address_and_never_headquarters(tmp_path: Path) -> None:
    overrides, audit, opener = _load(tmp_path)

    assert overrides == (
        FaEntityOverride(
            isin=_ISIN,
            address=(
                "C/O SAMPLE REGISTERED AGENT, SAMPLE AGENT CENTER, "
                "99 SAMPLE ST, SAMPLETON, US-DE, US"
            ),
            zip_code="99991",
        ),
    )
    assert audit == GleifLegalAddressAudit(1, 1, 0)
    assert isinstance(opener, _RecordingOpener)
    assert len(opener.calls) == 1
    assert "US99992E1010" in opener.calls[0]
    artifacts = list(tmp_path.rglob("gleif.json"))
    assert len(artifacts) == 1
    assert str(_ISIN) not in str(artifacts[0].parent)
    artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert artifact["record"]["address_lines"][0] == "C/O SAMPLE REGISTERED AGENT"
    assert "1 SAMPLE LANE" not in artifacts[0].read_text(encoding="utf-8")
    assert artifact["source"]["sha256"] == hashlib.sha256(_response()).hexdigest()


def test_ch_share_uses_one_reviewed_ascii_alias_and_retains_unicode_source(tmp_path: Path) -> None:
    isin = Isin("CH1134540470")
    response = _response(
        [
            _record(
                legal_lines=["Förrlibuckstrasse 190"],
                legal_city="Zürich",
                legal_region="CH-ZH",
                legal_country="CH",
                postal_code="8005",
            )
        ]
    )

    overrides, audit, _opener = _load(
        tmp_path,
        response=response,
        security=_security(isin=isin, country="CH"),
    )

    assert overrides == (
        FaEntityOverride(
            isin=isin,
            address="Forrlibuckstrasse 190, Zurich, CH-ZH, CH",
            zip_code="8005",
        ),
    )
    assert audit.reviewed_ascii_aliases == 1
    assert "reviewed ASCII portal alias" in audit.warnings[0]
    artifact = json.loads(next(tmp_path.rglob("gleif.json")).read_text(encoding="utf-8"))
    assert artifact["record"]["address_lines"] == ["Förrlibuckstrasse 190"]
    assert artifact["record"]["city"] == "Zürich"
    assert "Forrlibuckstrasse" not in json.dumps(artifact)


def test_verified_cache_hit_is_offline_and_byte_stable(tmp_path: Path) -> None:
    first, _audit, _opener = _load(tmp_path)
    path = next(tmp_path.rglob("gleif.json"))
    written = path.read_bytes()

    second, audit, _blocked_opener = _load(tmp_path, opener=_blocked)

    assert second == first
    assert audit.cache_hits == 1
    assert path.read_bytes() == written


def test_reviewed_config_opt_out_skips_lookup(tmp_path: Path) -> None:
    manual = FaEntityOverride(
        isin=_ISIN,
        address="Reviewed legal address",
        zip_code="99991",
        prefer_configured_address=True,
        prefer_configured_nature=True,
        prefer_configured_name=True,
    )

    overrides, audit, opener = _load(tmp_path, configured=(manual,), opener=_blocked)

    assert overrides == ()
    assert audit == GleifLegalAddressAudit(0, 0, 0)
    assert opener is _blocked


def test_depositary_receipt_never_uses_underlying_gleif_entity(tmp_path: Path) -> None:
    overrides, audit, _opener = _load(
        tmp_path,
        security=_security(depositary=True),
        opener=_blocked,
    )

    assert overrides == ()
    assert audit.failure_counts == (("adr-excluded", 1),)


@pytest.mark.parametrize(
    ("response", "security", "reason"),
    [
        pytest.param(_response([]), _security(), "zero-match", id="zero-match"),
        pytest.param(
            _response([_record(), _record()]),
            _security(),
            "multi-match",
            id="multi-match",
        ),
        pytest.param(
            _response([_record(registration_status="LAPSED")]),
            _security(),
            "lifecycle",
            id="lapsed",
        ),
        pytest.param(
            _response([_record(entity_status="INACTIVE")]),
            _security(),
            "lifecycle",
            id="inactive",
        ),
        pytest.param(
            _response([_record(successor="999900SAMPLE00010055")]),
            _security(),
            "lifecycle",
            id="successor",
        ),
        pytest.param(
            _response([_record(legal_country="CA")]),
            _security(),
            "country-mismatch",
            id="country-mismatch",
        ),
        pytest.param(
            _response([_record(postal_code=None)]),
            _security(),
            "incomplete",
            id="missing-postal",
        ),
        pytest.param(
            _response([_record(legal_lines=["SOCIÉTÉ ADDRESS"])]),
            _security(),
            "portal-shape",
            id="non-ascii",
        ),
        pytest.param(
            _response([_record(legal_lines=["=2+2"])]),
            _security(),
            "portal-shape",
            id="formula-prefix",
        ),
        pytest.param(
            _response([_record(postal_code="99991-1234")]),
            _security(),
            "portal-shape",
            id="long-postal",
        ),
    ],
)
def test_unusable_exact_matches_remain_manual(
    tmp_path: Path,
    response: bytes,
    security: RobinhoodSecurityConfig,
    reason: str,
) -> None:
    overrides, audit, _opener = _load(
        tmp_path,
        response=response,
        security=security,
    )

    assert overrides == ()
    assert audit.failure_counts == ((reason, 1),)


def test_cold_cache_only_run_stays_manual_without_network(tmp_path: Path) -> None:
    overrides, audit, _opener = _load(
        tmp_path,
        fetch=False,
        opener=_blocked,
    )

    assert overrides == ()
    assert audit.failure_counts == (("cache-miss", 1),)


def test_corrupt_cache_is_never_refetched_and_refuses(tmp_path: Path) -> None:
    _load(tmp_path)
    path = next(tmp_path.rglob("gleif.json"))
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(
            _cache_refusal(
                tmp_path,
                GLEIF_CACHE_INTEGRITY,
                fetch=True,
            )
        ),
    ):
        _load(tmp_path, opener=_blocked)


def test_resealed_cache_with_inconsistent_provenance_is_refused(tmp_path: Path) -> None:
    _load(tmp_path)
    path = next(tmp_path.rglob("gleif.json"))
    _reseal_artifact(
        path,
        lambda artifact: artifact["source"].update(url="https://api.gleif.org/api/v1/lei-records"),
    )

    with pytest.raises(GleifLegalAddressError, match="cache artifact is malformed"):
        _load(tmp_path, opener=_blocked)


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize(
    ("content", "problem"),
    [
        pytest.param(
            b'{"payload_sha256":"x","unexpected":NaN}', "it is malformed", id="nonfinite-number"
        ),
        pytest.param(DEEP_JSON, "its JSON is nested too deeply", id="deep-json"),
    ],
)
def test_malformed_cache_validation_refuses(
    tmp_path: Path, content: bytes, problem: str, fetch: bool
) -> None:
    _load(tmp_path)
    path = _artifact_path(tmp_path, _ISIN)
    path.write_bytes(content)

    with pytest.raises(
        GleifLegalAddressError, match=_exactly(_cache_refusal(tmp_path, problem, fetch=fetch))
    ):
        _load(tmp_path, fetch=fetch, opener=_blocked)


@pytest.mark.parametrize("fetch", [True, False])
def test_dangling_cache_entry_refuses(tmp_path: Path, fetch: bool) -> None:
    path = _artifact_path(tmp_path, _ISIN)
    path.parent.mkdir(parents=True)
    path.symlink_to(path.parent / "missing.json")

    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(
            _cache_refusal(
                tmp_path,
                "GLEIF legal-address cache artifact could not be read (ENOENT)",
                fetch=fetch,
            )
        ),
    ):
        _load(tmp_path, fetch=fetch, opener=_blocked)


@pytest.mark.parametrize("fetch", [True, False])
def test_cache_inspection_failure_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetch: bool
) -> None:
    path = _artifact_path(tmp_path, _ISIN)
    lstat = Path.lstat

    def inaccessible(self: Path):
        if self == path:
            raise PermissionError(errno.EACCES, STRERROR, FILENAME)
        return lstat(self)

    monkeypatch.setattr(Path, "lstat", inaccessible)

    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(_cache_refusal(tmp_path, "could not inspect it (EACCES)", fetch=fetch)),
    ) as refusal:
        _load(tmp_path, fetch=fetch, opener=_blocked)
    # Before #1044 the refusal printed the OSError's own text.
    assert_no_markers(str(refusal.value))


def test_deeply_nested_response_refuses(tmp_path: Path) -> None:
    with pytest.raises(GleifLegalAddressError, match="could not be fetched and cached"):
        _load(tmp_path, response=DEEP_JSON)


def test_failed_fetch_refuses_and_writes_no_artifact(tmp_path: Path) -> None:
    def unreachable(_url: str) -> bytes:
        raise LegalAddressSourceError(GLEIF_HTTP_503)

    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(_fetch_refusal(tmp_path, GLEIF_HTTP_503)),
    ):
        _load(tmp_path, opener=unreachable)
    assert not list(tmp_path.rglob("gleif.json"))


def test_lookup_keeps_each_isin_outcome(tmp_path: Path) -> None:
    lookup = load_robinhood_legal_entity_evidence(
        (
            _security(),
            robinhood_security(
                symbol="XQAQ",
                cusip="99992A109",
                isin=Isin("US99992A1097"),
                is_depositary_receipt=True,
                source="manual",
            ),
        ),
        (),
        cache_dir=tmp_path,
        fetch=True,
        opener=_RecordingOpener(_response([])),
    )

    assert lookup.records == ()
    assert lookup.failures == {_ISIN: "zero-match", Isin("US99992A1097"): "adr-excluded"}
    assert lookup.requested == 2
    assert lookup.failure_counts == (("adr-excluded", 1), ("zero-match", 1))


def test_fetched_record_round_trips_through_the_cache(tmp_path: Path) -> None:
    def load(fetch: bool, opener: LegalAddressOpener):
        return load_robinhood_legal_entity_evidence(
            (_security(),), (), cache_dir=tmp_path, fetch=fetch, opener=opener
        ).records

    (fetched,) = load(True, _RecordingOpener(_response()))
    (cached,) = load(False, _blocked)

    assert cached == fetched
    artifact = json.loads(_artifact_path(tmp_path, _ISIN).read_text(encoding="utf-8"))
    assert artifact["schema"] == SCHEMA_VERSION
    assert "types_verified" not in artifact["record"]


@pytest.mark.parametrize(
    ("status", "keys", "value", "field"),
    [
        # #821 acceptance; the 59d949e outcome follows each case.
        # Address filed without its region.
        pytest.param("ISSUED", ("entity", "legalAddress", "region"), 123, "", id="issued-region"),
        # incomplete, manual.
        pytest.param(
            "ISSUED", ("entity", "legalAddress", "postalCode"), 12345, "", id="issued-postal"
        ),
        # incomplete, manual.
        pytest.param(
            "ISSUED", ("entity", "legalAddress", "addressLines"), "1 Main St", "", id="issued-lines"
        ),
        # Address filed, no legal form.
        pytest.param("ISSUED", ("entity", "legalForm"), 123, "", id="issued-legal-form"),
        # Loader error, then refusal as a failed fetch.
        pytest.param("ISSUED", ("entity", "successorEntities"), 1, "", id="issued-successors"),
        # lapsed-unverified.
        pytest.param("LAPSED", ("entity", "legalAddress", "region"), 123, "", id="lapsed-region"),
        # lapsed, SEC fallback allowed.
        pytest.param("LAPSED", ("entity", "jurisdiction"), 1, "", id="lapsed-jurisdiction"),
        # Used as if it had no successor.
        pytest.param(
            "ISSUED",
            ("entity", "successorEntity"),
            {"lei": None, "name": 123},
            "entity.successorEntity.name",
            id="issued-successor-name",
        ),
        # Every other field the loader reads.
        pytest.param("ISSUED", ("lei",), 123, "", id="lei"),
        pytest.param(
            "ISSUED", ("entity", "legalName", "name"), ["SAMPLE ISSUER AG"], "", id="legal-name"
        ),
        pytest.param("ISSUED", ("entity", "status"), True, "", id="entity-status"),
        pytest.param("ISSUED", ("registration", "status"), 1, "", id="registration-status"),
        pytest.param(
            "ISSUED", ("registration", "lastUpdateDate"), 20260310, "", id="registration-update"
        ),
        pytest.param(
            "ISSUED",
            ("entity", "legalAddress", "addressLines"),
            ["99", 1],
            "entity.legalAddress.addressLines[1]",
            id="line",
        ),
        pytest.param("ISSUED", ("entity", "legalAddress", "city"), 7, "", id="city"),
        pytest.param(
            "ISSUED", ("entity", "legalAddress", "country"), {"code": "US"}, "", id="country"
        ),
        pytest.param("ISSUED", ("entity", "jurisdiction"), ["US-DE"], "", id="jurisdiction"),
        pytest.param("ISSUED", ("entity", "category"), 1, "", id="category"),
        pytest.param("ISSUED", ("entity", "legalForm", "id"), 7, "", id="legal-form-id"),
        pytest.param("ISSUED", ("entity", "legalForm", "other"), False, "", id="legal-form-other"),
        pytest.param("ISSUED", ("entity", "successorEntity"), "unexpected", "", id="successor"),
        pytest.param(
            "ISSUED",
            ("entity", "successorEntity"),
            {"lei": 123},
            "entity.successorEntity.lei",
            id="successor-lei",
        ),
        pytest.param(
            "ISSUED", ("entity", "successorEntities"), {"lei": "X"}, "", id="successors-object"
        ),
        # The required objects.
        pytest.param("ISSUED", ("entity",), 123, "", id="entity"),
        pytest.param(
            "ISSUED", ("entity", "legalName"), "SAMPLE ISSUER AG", "", id="legal-name-object"
        ),
        pytest.param("ISSUED", ("entity", "legalAddress"), [], "", id="legal-address-object"),
        pytest.param("ISSUED", ("registration",), "ISSUED", "", id="registration-object"),
    ],
)
def test_record_with_an_unexpected_raw_type_refuses_before_caching(
    tmp_path: Path,
    status: str,
    keys: tuple[str, ...],
    value: object,
    field: str,
) -> None:
    record = _with_raw(_record(registration_status=status), *keys, value=value)

    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(
            _raw_type_refusal(tmp_path, f"data[0].attributes.{field or '.'.join(keys)}")
        ),
    ):
        _load(tmp_path, response=_response([record]))
    assert not list(tmp_path.rglob("gleif.json"))


def test_successor_lei_does_not_skip_the_successors_check(tmp_path: Path) -> None:
    record = _record(successor="999900SAMPLE00010055")

    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(_raw_type_refusal(tmp_path, "data[0].attributes.entity.successorEntities")),
    ):
        _load(
            tmp_path,
            response=_response([_with_raw(record, "entity", "successorEntities", value=1)]),
        )
    assert not list(tmp_path.rglob("gleif.json"))


def _payload(change: Callable[[dict[str, Any]], object]) -> bytes:
    payload: dict[str, Any] = json.loads(_response())
    change(payload)
    return json.dumps(payload).encode()


def _region_123(record: dict[str, object]) -> dict[str, object]:
    return _with_raw(record, "entity", "legalAddress", "region", value=123)


@pytest.mark.parametrize(
    ("change", "path"),
    [
        pytest.param(lambda p: p.update(meta=123), "meta", id="meta"),
        pytest.param(lambda p: p["meta"].update(goldenCopy=[]), "meta.goldenCopy", id="golden"),
        pytest.param(
            lambda p: p["meta"]["goldenCopy"].update(publishDate=123),
            "meta.goldenCopy.publishDate",
            id="publish-date",
        ),
        pytest.param(lambda p: p.update(data=_record()), "data", id="data"),
        pytest.param(
            lambda p: (
                p["meta"]["goldenCopy"].update(publishDate=None),
                _region_123(p["data"][0]),
            ),
            "data[0].attributes.entity.legalAddress.region",
            id="null-publish-date-with-bad-region",
        ),
        pytest.param(
            lambda p: p["data"][0].update(attributes=[]), "data[0].attributes", id="attrs"
        ),
        # #821 acceptance: both were multi-match, and cached, on 59d949e.
        pytest.param(lambda p: p.update(data=[123, 456]), "data[0]", id="multi-match-numbers"),
        pytest.param(
            lambda p: p["data"].append(_region_123(_record())),
            "data[1].attributes.entity.legalAddress.region",
            id="multi-match-second-region",
        ),
    ],
)
def test_response_envelope_with_an_unexpected_raw_type_refuses(
    tmp_path: Path,
    change: Callable[[dict[str, Any]], object],
    path: str,
) -> None:
    with pytest.raises(GleifLegalAddressError, match=_exactly(_raw_type_refusal(tmp_path, path))):
        _load(tmp_path, response=_payload(change))
    assert not list(tmp_path.rglob("gleif.json"))


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda p: p.pop("meta"), id="meta"),
        pytest.param(lambda p: p["meta"]["goldenCopy"].update(publishDate=None), id="publish-date"),
        pytest.param(lambda p: p.update(data=None), id="data"),
        pytest.param(lambda p: p.update(data=[None]), id="null-record"),
        pytest.param(lambda p: p["data"][0].pop("attributes"), id="attributes"),
        pytest.param(lambda p: p["data"][0]["attributes"].update(entity=None), id="entity"),
        pytest.param(lambda p: p["data"][0]["attributes"].pop("registration"), id="registration"),
        pytest.param(
            lambda p: p["data"].append({"type": "lei-records", "attributes": {}}),
            id="multi-match-record-without-entity",
        ),
    ],
)
def test_absent_or_null_required_object_refuses_as_a_failed_fetch(
    tmp_path: Path,
    change: Callable[[dict[str, Any]], object],
) -> None:
    with pytest.raises(GleifLegalAddressError, match="could not be fetched and cached"):
        _load(tmp_path, response=_payload(change))
    assert not list(tmp_path.rglob("gleif.json"))


@pytest.mark.parametrize(
    ("keys", "value", "failure_counts"),
    [
        pytest.param(("entity", "successorEntities"), [], (), id="no-successors"),
        pytest.param(("entity", "successorEntities"), None, (), id="null-successors"),
        pytest.param(("entity", "successorEntity"), None, (), id="null-successor"),
        pytest.param(
            ("entity", "successorEntity"), {"lei": "", "name": ""}, (), id="empty-successor"
        ),
        pytest.param(("entity", "legalForm"), None, (), id="null-legal-form"),
        pytest.param(("lei",), None, (("incomplete", 1),), id="null-lei"),
        pytest.param(
            ("entity", "legalAddress", "addressLines"),
            None,
            (("incomplete", 1),),
            id="null-address-lines",
        ),
    ],
)
def test_null_or_empty_raw_values_keep_their_outcome(
    tmp_path: Path,
    keys: tuple[str, ...],
    value: object,
    failure_counts: tuple[tuple[str, int], ...],
) -> None:
    overrides, audit, _opener = _load(
        tmp_path,
        response=_response([_with_raw(_record(), *keys, value=value)]),
    )

    assert audit.failure_counts == failure_counts
    assert len(overrides) == (0 if failure_counts else 1)


@pytest.mark.parametrize(
    ("change", "path"),
    [
        # #821 acceptance; each was source-failure on 59d949e.
        pytest.param(
            lambda p: p["data"]["attributes"].update(lei=123), "data.attributes.lei", id="lei"
        ),
        pytest.param(lambda p: p["data"].update(id=123), "data.id", id="record-id"),
        pytest.param(
            lambda p: p["meta"]["goldenCopy"].update(publishDate=123),
            "meta.goldenCopy.publishDate",
            id="publish-date",
        ),
        # Filed without its region, or counted as a mismatch, on 59d949e.
        pytest.param(
            lambda p: _region_123(p["data"]),
            "data.attributes.entity.legalAddress.region",
            id="region",
        ),
        pytest.param(
            lambda p: p["data"]["attributes"]["entity"].update(category=1),
            "data.attributes.entity.category",
            id="category",
        ),
        pytest.param(
            lambda p: p["data"]["attributes"]["entity"]["successorEntity"].update(name=123),
            "data.attributes.entity.successorEntity.name",
            id="successor-name",
        ),
        pytest.param(lambda p: p.update(data=[]), "data", id="data"),
        pytest.param(lambda p: p["data"].update(attributes="x"), "data.attributes", id="attrs"),
        pytest.param(
            lambda p: (p.update(data=None), p.update(meta=123)),
            "meta",
            id="null-data-with-bad-meta",
        ),
        pytest.param(
            lambda p: (
                p.pop("data"),
                p["meta"]["goldenCopy"].update(publishDate=123),
            ),
            "meta.goldenCopy.publishDate",
            id="missing-data-with-bad-publish-date",
        ),
        pytest.param(
            lambda p: (p["data"].update(id="OTHER-LEI"), _region_123(p["data"])),
            "data.attributes.entity.legalAddress.region",
            id="wrong-lei-with-bad-region",
        ),
    ],
)
def test_registrant_response_with_an_unexpected_raw_type_refuses(
    tmp_path: Path,
    change: Callable[[dict[str, Any]], object],
    path: str,
) -> None:
    payload: dict[str, Any] = json.loads(_registrant_response())
    change(payload)

    with pytest.raises(
        GleifLegalAddressError, match=_exactly(_registrant_raw_type_refusal(tmp_path, path))
    ):
        load_gleif_registrant_by_lei(
            _LEI,
            cache_dir=tmp_path,
            fetch=True,
            opener=_RecordingOpener(json.dumps(payload).encode()),
        )
    assert not list(tmp_path.rglob("gleif-registrant.json"))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("region", 123, id="region"),
        pytest.param("lei", None, id="lei"),
        pytest.param("legal_form_id", 1, id="legal-form"),
        pytest.param("address_lines", "1 SAMPLE ST", id="address-lines"),
        pytest.param("address_lines", ["1 SAMPLE ST", 1], id="address-line"),
        pytest.param("has_successor", 0, id="successor-flag"),
    ],
)
def test_cached_value_with_an_unexpected_type_refuses(
    tmp_path: Path,
    key: str,
    value: object,
) -> None:
    _load(tmp_path)
    path = next(tmp_path.rglob("gleif.json"))
    _reseal(path, lambda record: record.update({key: value}))

    # Before #1044 every such field read as "cache contains an invalid record".
    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(
            _cache_refusal(
                tmp_path,
                f"GLEIF legal-address cache field record.{key} has an unexpected JSON type",
                fetch=True,
            )
        ),
    ):
        _load(tmp_path, opener=_blocked)


def _older_path(cache_dir: Path, domain: str, key: str, value: str, schema: int, name: str) -> Path:
    """An artifact path as older versions computed it, independent of the loader."""
    fingerprint = hashlib.sha256(
        json.dumps(
            {"domain": domain, key: value, "schema": schema},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return cache_dir / "robinhood-legal-entities" / fingerprint / name


def _move_to_older_schema(current: Path, older: Path, schema: int) -> None:
    """Re-label a current artifact as one an older version wrote, at its older path."""
    artifact = json.loads(current.read_text(encoding="utf-8"))
    artifact["schema"] = schema
    record = artifact["record"]
    if current.name == "gleif.json":
        # Schema 3 (#821) has today's fields; schema 2 (#809) carried the type
        # flag; schema 1 also lacked legal form.
        if schema == 2:
            record["types_verified"] = True
        elif schema == 1:
            record.pop("legal_form_id")
            record.pop("legal_form_other")
    reseal(artifact)
    older.parent.mkdir(parents=True)
    older.write_text(json.dumps(artifact), encoding="utf-8")
    current.unlink()
    current.parent.rmdir()


def test_older_path_matches_the_current_fingerprint(tmp_path: Path) -> None:
    current = _older_path(
        tmp_path,
        "ibkr-fa/robinhood-gleif-legal-address",
        "isin",
        str(_ISIN),
        SCHEMA_VERSION,
        "gleif.json",
    )

    assert current == _artifact_path(tmp_path, _ISIN)


@pytest.mark.parametrize("schema", [1, 2, 3])
def test_older_isin_artifact_is_fetched_again_or_a_cache_miss(tmp_path: Path, schema: int) -> None:
    _load(tmp_path)
    older = _older_path(
        tmp_path, "ibkr-fa/robinhood-gleif-legal-address", "isin", str(_ISIN), schema, "gleif.json"
    )
    _move_to_older_schema(_artifact_path(tmp_path, _ISIN), older, schema)
    written = older.read_bytes()

    offline, offline_audit, _opener = _load(tmp_path, fetch=False, opener=_blocked)
    opener = _RecordingOpener(_response())
    fetched, fetched_audit, _opener = _load(tmp_path, opener=opener)

    assert offline == ()
    assert offline_audit == GleifLegalAddressAudit(1, 0, 0, (("cache-miss", 1),))
    assert fetched[0].zip_code == "99991"
    assert fetched_audit == GleifLegalAddressAudit(1, 1, 0)
    assert len(opener.calls) == 1
    assert _artifact_path(tmp_path, _ISIN).exists()
    assert older.read_bytes() == written


@pytest.mark.parametrize("schema", [1, 2])
def test_older_registrant_artifact_is_fetched_again_or_a_cache_miss(
    tmp_path: Path, schema: int
) -> None:
    load_gleif_registrant_by_lei(
        _LEI, cache_dir=tmp_path, fetch=True, opener=_RecordingOpener(_registrant_response())
    )
    older = _older_path(
        tmp_path, "ibkr-fa/robinhood-gleif-registrant", "lei", _LEI, schema, "gleif-registrant.json"
    )
    _move_to_older_schema(_registrant_artifact_path(tmp_path, _LEI), older, schema)

    offline = load_gleif_registrant_by_lei(_LEI, cache_dir=tmp_path, fetch=False, opener=_blocked)
    opener = _RecordingOpener(_registrant_response())
    record, cache_hit = load_gleif_registrant_by_lei(
        _LEI, cache_dir=tmp_path, fetch=True, opener=opener
    )

    assert offline == (None, False)
    assert record is not None
    assert record.lei == _LEI
    assert cache_hit is False
    assert len(opener.calls) == 1
    assert _registrant_artifact_path(tmp_path, _LEI).exists()


# ---- #826: the registrant lookup refuses wherever the exact-ISIN lookup does ----

_OTHER_LEI = "999900SAMPLE00010055"
_REGISTRANT_TEXT_FIELDS = (
    "lei",
    "legal_name",
    "city",
    "region",
    "country_iso2",
    "postal_code",
    "jurisdiction",
    "category",
    "legal_form_id",
    "legal_form_other",
    "entity_status",
    "registration_status",
    "registration_last_update",
    "golden_copy_publish_date",
    "source_url",
    "source_sha256",
)


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize("key", _REGISTRANT_TEXT_FIELDS)
def test_registrant_cache_text_with_an_unexpected_type_refuses(
    tmp_path: Path,
    key: str,
    fetch: bool,
) -> None:
    path = _cached_registrant(tmp_path)
    _reseal(path, lambda record: record.update({key: 123}))

    # On e16b0be the value read as "": the region was left out of the filed
    # address, or the record failed a later check and counted as cache-invalid.
    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(
            _registrant_cache_refusal(
                tmp_path,
                f"GLEIF registrant cache field record.{key} has an unexpected JSON type",
                fetch=fetch,
            )
        ),
    ):
        load_gleif_registrant_by_lei(_LEI, cache_dir=tmp_path, fetch=fetch, opener=_blocked)


def _tamper(path: Path) -> None:
    artifact = json.loads(path.read_text(encoding="utf-8"))
    artifact["record"]["region"] = "US-CA"
    path.write_text(json.dumps(artifact), encoding="utf-8")


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        pytest.param(_tamper, "GLEIF registrant cache artifact is malformed", id="integrity"),
        pytest.param(
            lambda path: path.write_bytes(b"not json"),
            "GLEIF registrant cache artifact is not valid UTF-8 JSON",
            id="not-json",
        ),
        pytest.param(
            lambda path: path.write_bytes(DEEP_JSON),
            "its JSON is nested too deeply",
            id="deep-json",
        ),
        pytest.param(
            lambda path: path.write_bytes(b'{"payload_sha256":"x","unexpected":NaN}'),
            "it is malformed",
            id="nonfinite-number",
        ),
        pytest.param(
            _dangle, "GLEIF registrant cache artifact could not be read (ENOENT)", id="dangling"
        ),
        pytest.param(
            lambda path: _reseal_artifact(path, lambda artifact: artifact.update(schema=True)),
            "GLEIF registrant cache artifact is malformed",
            id="schema-true",
        ),
        pytest.param(
            lambda path: _reseal_artifact(
                path, lambda artifact: artifact.update(schema=float(REGISTRANT_SCHEMA_VERSION))
            ),
            "GLEIF registrant cache artifact is malformed",
            id="schema-float",
        ),
        pytest.param(
            lambda path: _reseal(path, lambda record: record.update(address_lines="1 MAIN ST")),
            "GLEIF registrant cache field record.address_lines has an unexpected JSON type",
            id="address-lines",
        ),
        pytest.param(
            lambda path: _reseal(path, lambda record: record.update(has_successor=0)),
            "GLEIF registrant cache field record.has_successor has an unexpected JSON type",
            id="successor-flag",
        ),
        pytest.param(
            lambda path: _reseal(path, lambda record: record.update(lei=_OTHER_LEI)),
            "GLEIF registrant cache record is inconsistent",
            id="inconsistent",
        ),
    ],
)
def test_unusable_registrant_cache_refuses_without_fetching(
    tmp_path: Path,
    damage: Callable[[Path], object],
    problem: str,
    fetch: bool,
) -> None:
    path = _cached_registrant(tmp_path)
    damage(path)

    # On e16b0be each counted as cache-invalid, was treated as missing, escaped
    # untyped, or (schema-float) was read as a valid cache hit.
    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(_registrant_cache_refusal(tmp_path, problem, fetch=fetch)),
    ):
        load_gleif_registrant_by_lei(_LEI, cache_dir=tmp_path, fetch=fetch, opener=_blocked)


@pytest.mark.parametrize("fetch", [True, False])
def test_registrant_cache_inspection_failure_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch: bool,
) -> None:
    path = _registrant_artifact_path(tmp_path, _LEI)
    lstat = Path.lstat

    def inaccessible(self: Path):
        if self == path:
            raise PermissionError(errno.EACCES, STRERROR, FILENAME)
        return lstat(self)

    monkeypatch.setattr(Path, "lstat", inaccessible)

    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(
            _registrant_cache_refusal(tmp_path, "could not inspect it (EACCES)", fetch=fetch)
        ),
    ) as refusal:
        load_gleif_registrant_by_lei(_LEI, cache_dir=tmp_path, fetch=fetch, opener=_blocked)
    # Before #1044 the refusal printed the OSError's own text.
    assert_no_markers(str(refusal.value))


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize("schema", [True, float(SCHEMA_VERSION)], ids=["true", "float"])
def test_exact_isin_cache_schema_must_be_an_int(
    tmp_path: Path,
    schema: object,
    fetch: bool,
) -> None:
    _load(tmp_path)
    path = _artifact_path(tmp_path, _ISIN)
    _reseal_artifact(path, lambda artifact: artifact.update(schema=schema))

    # On e16b0be the float schema equalled SCHEMA_VERSION and was read.
    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(
            _cache_refusal(
                tmp_path, "GLEIF legal-address cache artifact does not match its ISIN", fetch=fetch
            )
        ),
    ):
        _load(tmp_path, fetch=fetch, opener=_blocked)


def _unreachable(_url: str) -> bytes:
    raise LegalAddressSourceError(GLEIF_HTTP_503)


def _changed_registrant(change: Callable[[dict[str, Any]], object]) -> bytes:
    payload: dict[str, Any] = json.loads(_registrant_response())
    change(payload)
    return json.dumps(payload).encode()


@pytest.mark.parametrize(
    ("opener", "problem"),
    [
        pytest.param(_unreachable, GLEIF_HTTP_503, id="http"),
        pytest.param(
            _RecordingOpener(b"not json"), "GLEIF registrant response is malformed", id="malformed"
        ),
        pytest.param(_RecordingOpener(DEEP_JSON), "its JSON is nested too deeply", id="deep"),
        pytest.param(
            _RecordingOpener(_registrant_response(lei=_OTHER_LEI)),
            "GLEIF registrant response has an invalid record",
            id="another-lei",
        ),
        pytest.param(
            _RecordingOpener(_changed_registrant(lambda payload: payload.pop("data"))),
            "GLEIF registrant response has an invalid record",
            id="missing-data",
        ),
        pytest.param(
            _RecordingOpener(_changed_registrant(lambda payload: payload.update(data=None))),
            "GLEIF registrant response has an invalid record",
            id="null-data",
        ),
    ],
)
def test_failed_registrant_fetch_refuses_and_writes_nothing(
    tmp_path: Path,
    opener: LegalAddressOpener,
    problem: str,
) -> None:
    # On e16b0be the deep response escaped untyped and the others counted as
    # source-failure.
    with pytest.raises(
        GleifLegalAddressError, match=_exactly(_registrant_fetch_refusal(tmp_path, problem))
    ):
        load_gleif_registrant_by_lei(_LEI, cache_dir=tmp_path, fetch=True, opener=opener)
    assert list(tmp_path.iterdir()) == []


def test_failed_registrant_publish_refuses_and_leaves_no_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(_self: Path, _target: Path) -> None:
        raise PermissionError(errno.EACCES, STRERROR, FILENAME)

    monkeypatch.setattr(Path, "hardlink_to", denied)

    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(
            _registrant_fetch_refusal(
                tmp_path, "GLEIF legal-address cache artifact could not be written (EACCES)"
            )
        ),
    ):
        _cached_registrant(tmp_path)
    assert list(_registrant_artifact_path(tmp_path, _LEI).parent.iterdir()) == []


def test_registrant_cleanup_failure_after_publishing_refuses_then_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _registrant_artifact_path(tmp_path, _LEI)
    unlink = Path.unlink

    def stuck(self: Path, missing_ok: bool = False) -> None:
        if self.parent == path.parent and self != path:
            raise PermissionError(errno.EACCES, STRERROR, FILENAME)
        unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", stuck)

    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(
            _registrant_fetch_refusal(tmp_path, "a network or cache-file operation failed (EACCES)")
        ),
    ) as refusal:
        _cached_registrant(tmp_path)
    # Before #1044 the refusal printed the OSError's own text.
    assert_no_markers(str(refusal.value))
    monkeypatch.undo()
    record, cache_hit = load_gleif_registrant_by_lei(
        _LEI, cache_dir=tmp_path, fetch=False, opener=_blocked
    )

    assert record is not None
    assert record.lei == _LEI
    assert cache_hit is True


@pytest.mark.parametrize("fetch", [True, False])
def test_invalid_requested_lei_refuses_without_naming_it(tmp_path: Path, fetch: bool) -> None:
    # On e16b0be this counted as invalid-lei.
    with pytest.raises(
        GleifLegalAddressError,
        match=(
            r"\AGLEIF registrant lookup was asked for an LEI that is not valid\. For each ADR or "
            r"ETF, review its legal entity yourself and set prefer_configured_address, "
            r"prefer_configured_name and prefer_configured_nature in its \[\[fa\.entities\]\] "
            r"entry to skip the GLEIF lookup\. "
            rf"{re.escape(SET_IDENTITY_TOO)}\Z"
        ),
    ):
        load_gleif_registrant_by_lei("bad", cache_dir=tmp_path, fetch=fetch, opener=_blocked)
    assert list(tmp_path.iterdir()) == []


def test_missing_registrant_artifact_is_still_a_cache_miss_offline(tmp_path: Path) -> None:
    result = load_gleif_registrant_by_lei(_LEI, cache_dir=tmp_path, fetch=False, opener=_blocked)

    assert result == (None, False)


# ---- #1044: GLEIF refusals name neither a held security nor an OSError's text ----

# An errno with no symbolic name.
_UNNAMED_ERRNO = max(errno.errorcode) + 1


def _cached_cusip_artifact(tmp_path: Path) -> Path:
    _load(tmp_path, security=_CUSIP_SECURITY)
    return _artifact_path(tmp_path, _CUSIP_ISIN)


def test_cache_refusal_names_neither_the_security_nor_its_artifact(tmp_path: Path) -> None:
    path = _cached_cusip_artifact(tmp_path)
    _reseal(path, lambda record: record.update(region=["US-DE"]))

    with pytest.raises(GleifLegalAddressError) as refusal:
        _load(tmp_path, security=_CUSIP_SECURITY, opener=_blocked)

    message = str(refusal.value)
    assert message == _cache_refusal(
        tmp_path,
        "GLEIF legal-address cache field record.region has an unexpected JSON type",
        fetch=True,
    )
    # Before #1044 it printed the ISIN and the artifact path, whose folder
    # name is the ISIN's fingerprint.
    assert_no_markers(message, CUSIP, _CUSIP_ISIN, path.parent.name)


def test_raw_type_refusal_names_neither_the_security_nor_its_artifact(tmp_path: Path) -> None:
    response = _response([_region_123(_record())])

    with pytest.raises(GleifLegalAddressError) as refusal:
        _load(tmp_path, response=response, security=_CUSIP_SECURITY)

    message = str(refusal.value)
    assert message == _raw_type_refusal(tmp_path, "data[0].attributes.entity.legalAddress.region")
    # Before #1044 it printed the ISIN.
    assert_no_markers(
        message, CUSIP, _CUSIP_ISIN, _artifact_path(tmp_path, _CUSIP_ISIN).parent.name
    )
    assert not list(tmp_path.rglob("gleif.json"))


def test_fetch_refusal_names_neither_the_security_nor_the_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _artifact_path(tmp_path, _CUSIP_ISIN)
    mkdir = Path.mkdir

    def denied(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == path.parent:
            raise PermissionError(errno.EACCES, STRERROR, FILENAME)
        mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", denied)

    with pytest.raises(GleifLegalAddressError) as refusal:
        _load(tmp_path, security=_CUSIP_SECURITY)

    message = str(refusal.value)
    assert message == _fetch_refusal(tmp_path, "a network or cache-file operation failed (EACCES)")
    # Before #1044 it printed the ISIN and the OSError's own text.
    assert_no_markers(message, CUSIP, _CUSIP_ISIN, STRERROR, FILENAME, path.parent.name)
    # Nor does it keep the OSError: no cause or context (#1224).
    assert_replaced_unchained(refusal.value, markers=(STRERROR, FILENAME))


@pytest.mark.parametrize(
    ("error", "name"),
    [
        pytest.param(OSError(errno.EACCES, STRERROR, FILENAME), "EACCES", id="eacces"),
        # One argument leaves errno None.
        pytest.param(OSError(STRERROR), "unknown error", id="no-errno"),
        pytest.param(
            OSError(_UNNAMED_ERRNO, STRERROR, FILENAME), "unknown error", id="unnamed-errno"
        ),
    ],
)
def test_unreadable_cache_refuses_with_only_its_errno_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
    name: str,
) -> None:
    path = _cached_cusip_artifact(tmp_path)
    read_text = Path.read_text

    def unreadable(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == path:
            raise error
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)

    with pytest.raises(GleifLegalAddressError) as refusal:
        _load(tmp_path, security=_CUSIP_SECURITY, opener=_blocked)

    message = str(refusal.value)
    assert message == _cache_refusal(
        tmp_path, f"GLEIF legal-address cache artifact could not be read ({name})", fetch=True
    )
    # Before #1044 it printed the ISIN and the artifact path.
    assert_no_markers(message, *MARKERS, _CUSIP_ISIN, path.parent.name)
    # Nor does it keep the OSError, or the private error that wrapped it: its
    # only cause is the stand-in for that private error (#1224).
    assert_replaced_unchained(refusal.value, LegalAddressSourceError, markers=(STRERROR,))


# ---- #1086: GLEIF registrant refusals name no LEI ----

# Two distinct registrant LEIs, so a refusal can't pass by naming another.
_REGISTRANT_LEIS = ("999900SAMPLE00000355", "999900SAMPLE00000161")
_OS_ERRORS = [
    pytest.param(OSError(errno.EACCES, STRERROR, FILENAME), "EACCES", id="eacces"),
    pytest.param(OSError(None, STRERROR, FILENAME), "unknown error", id="no-errno"),
    pytest.param(OSError(_UNNAMED_ERRNO, STRERROR, FILENAME), "unknown error", id="unnamed-errno"),
]


@pytest.mark.parametrize(("error", "name"), _OS_ERRORS)
@pytest.mark.parametrize("lei", _REGISTRANT_LEIS)
def test_registrant_cache_refusal_names_no_lei(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lei: str, error: OSError, name: str
) -> None:
    load_gleif_registrant_by_lei(
        lei, cache_dir=tmp_path, fetch=True, opener=_RecordingOpener(_registrant_response(lei=lei))
    )
    path = _registrant_artifact_path(tmp_path, lei)
    read_text = Path.read_text

    def unreadable(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == path:
            raise error
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)

    with pytest.raises(GleifLegalAddressError) as refusal:
        load_gleif_registrant_by_lei(lei, cache_dir=tmp_path, fetch=False, opener=_blocked)

    message = str(refusal.value)
    assert message == _registrant_cache_refusal(
        tmp_path, f"GLEIF registrant cache artifact could not be read ({name})", fetch=False
    )
    # Before #1086 it printed the LEI.
    assert_no_markers(message, lei, STRERROR, FILENAME, path.parent.name)


@pytest.mark.parametrize("lei", _REGISTRANT_LEIS)
def test_registrant_raw_type_refusal_names_no_lei(tmp_path: Path, lei: str) -> None:
    payload: dict[str, Any] = json.loads(_registrant_response(lei=lei))
    payload["data"] = 5

    with pytest.raises(GleifLegalAddressError) as refusal:
        load_gleif_registrant_by_lei(
            lei,
            cache_dir=tmp_path,
            fetch=True,
            opener=_RecordingOpener(json.dumps(payload).encode()),
        )

    message = str(refusal.value)
    assert message == _registrant_raw_type_refusal(tmp_path, "data")
    # Before #1086 it printed the LEI.
    assert_no_markers(message, lei)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(("error", "name"), _OS_ERRORS)
@pytest.mark.parametrize("lei", _REGISTRANT_LEIS)
def test_registrant_fetch_refusal_names_no_lei(
    tmp_path: Path, lei: str, error: OSError, name: str
) -> None:
    def failing(_url: str) -> bytes:
        raise error

    with pytest.raises(GleifLegalAddressError) as refusal:
        load_gleif_registrant_by_lei(lei, cache_dir=tmp_path, fetch=True, opener=failing)

    message = str(refusal.value)
    assert message == _registrant_fetch_refusal(
        tmp_path, f"a network or cache-file operation failed ({name})"
    )
    # Before #1086 it printed the LEI.
    assert_no_markers(message, lei, STRERROR, FILENAME)
    assert list(tmp_path.iterdir()) == []
