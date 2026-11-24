"""Tests for the fingerprinted official identity source cache (#492).

Every test injects its own transport: the autouse fixture below fails the
real urllib seam closed, so a regression that forgets to plumb the opener
crashes here instead of silently reaching Nasdaq/SEC from CI.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from ibkr_fa.models.reference_identity import (
    SEC_SUBMISSION_URL_TEMPLATE,
    NasdaqFlag,
    NasdaqTradedRecord,
    ObservedIdentityInput,
    SecFundRecord,
    SecSubmissionRecord,
    SecTickerRecord,
)
from ibkr_fa.robinhood_identity_sources import (
    NASDAQ_TRADED_URL,
    SCHEMA_VERSION,
    SEC_COMPANY_TICKERS_EXCHANGE_URL,
    SEC_COMPANY_TICKERS_MF_URL,
    IdentitySourceError,
    SecEntityCacheError,
    _parse_submission,
    identity_input_fingerprint,
    load_identity_sources,
    load_post_fy_identity_sources,
)
from ibkr_fa.robinhood_identity_sources import _fetch_bytes as fetch_bytes
from tests._crash_reports import assert_replaced_unchained
from tests._deep_json import DEEP_JSON
from tests._entity_cache import dangle as _dangle
from tests._entity_cache import reseal as _reseal
from tests._entity_cache_remedies import SET_SEC_CONTACT
from tests._entity_source_texts import IDENTITY_CACHE_MALFORMED, NASDAQ_UNAVAILABLE
from tests._entity_transport import RecordingOpener as _RecordingOpener
from tests._entity_transport import (  # noqa: F401 - autouse fixture
    close_http_errors as _close_http_errors,
)
from tests._entity_transport import http_error as _http_error
from tests._no_echo import FILENAME, HOLDER, STRERROR, assert_no_markers
from tests._robinhood_identity_sources import IDENTITY_CACHE_INTEGRITY as _INTEGRITY
from tests._robinhood_identity_sources import IDENTITY_CACHE_INVALID_RECORD as _INVALID_RECORD
from tests._robinhood_identity_sources import IDENTITY_CACHE_NON_FINITE as _NON_FINITE
from tests._robinhood_identity_sources import IDENTITY_CACHE_UNREADABLE as _UNREADABLE
from tests._robinhood_identity_sources import IDENTITY_INPUTS as _INPUTS
from tests._robinhood_identity_sources import ISSUER_AQ_CUSIP as _CUSIP
from tests._robinhood_identity_sources import ISSUER_AQ_SUBMISSION as _ISSUER_AQ
from tests._robinhood_identity_sources import MANUAL_IDENTITIES as _MANUAL
from tests._robinhood_identity_sources import NASDAQ_ROWS as _ROWS
from tests._robinhood_identity_sources import NASDAQ_TRAILER as _TRAILER
from tests._robinhood_identity_sources import SEC_CONTACT as _SEC_CONTACT
from tests._robinhood_identity_sources import SNAPSHOT_NOW as _NOW
from tests._robinhood_identity_sources import SNAPSHOTS as _SNAPSHOTS
from tests._robinhood_identity_sources import TICKER_ROWS as _TICKER_ROWS
from tests._robinhood_identity_sources import cache_state as _cache_state
from tests._robinhood_identity_sources import covering_snapshot as _covering
from tests._robinhood_identity_sources import funds_bytes as _funds_bytes
from tests._robinhood_identity_sources import legacy_snapshot as _legacy_snapshot
from tests._robinhood_identity_sources import load_post_fy_snapshot as _post_fy
from tests._robinhood_identity_sources import load_snapshot as _load
from tests._robinhood_identity_sources import nasdaq_bytes as _nasdaq_bytes
from tests._robinhood_identity_sources import no_network as _no_network
from tests._robinhood_identity_sources import non_covering_snapshot as _non_covering
from tests._robinhood_identity_sources import read_artifact as _artifact
from tests._robinhood_identity_sources import resealed as _resealed
from tests._robinhood_identity_sources import rewrite_artifact as _rewrite
from tests._robinhood_identity_sources import snapshot_refusal as _snapshot_refusal
from tests._robinhood_identity_sources import source_responses as _sources
from tests._robinhood_identity_sources import tamper_snapshot as _tamper
from tests._robinhood_identity_sources import tickers_bytes as _tickers_bytes

_PRIVATE_VALUES = (_CUSIP, "XQAQ", "XQAJ", "SAMPLE ISSUER AQ INC COMMON STOCK", _SEC_CONTACT)


@pytest.fixture(autouse=True)
def _block_identity_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the real identity transport closed for every test."""

    def _blocked(_request: urllib.request.Request, _timeout: float) -> tuple[str, bytes]:
        raise AssertionError("identity source transport must not touch the network")

    monkeypatch.setattr("ibkr_fa.robinhood_identity_sources._url_open", _blocked)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parses_official_sources_into_exact_normalized_records(tmp_path: Path) -> None:
    responses = _sources()
    bundle, audit, opener = _load(tmp_path, responses=responses)

    assert audit.failure is None
    assert audit.cache_hit is False
    assert audit.fingerprint == identity_input_fingerprint(_INPUTS, _MANUAL)
    assert bundle is not None
    # Only the observed symbols survive: the unrelated ZZZZ/XQAA/XQAIX rows
    # never enter the snapshot.
    # Flags keep the upstream Y/N vocabulary, so a consumer may compare
    # against either the enum member or the raw official value.
    assert bundle.nasdaq_by_symbol == {
        "XQAQ": NasdaqTradedRecord(
            "XQAQ",
            "Sample Issuer AQ Inc. - Common Stock",
            "Q",
            NasdaqFlag.NO,
            NasdaqFlag.NO,
            NasdaqFlag.NO,
        ),
        "XQAJ": NasdaqTradedRecord(
            "XQAJ",
            "Sample Fund AJ ETF",
            "P",
            NasdaqFlag.YES,
            NasdaqFlag.NO,
            NasdaqFlag.NO,
        ),
    }
    assert bundle.nasdaq_by_symbol["XQAJ"].etf is NasdaqFlag.YES
    assert bundle.nasdaq_by_symbol["XQAJ"].etf == "Y"
    assert bundle.nasdaq_by_symbol["XQAJ"].test_issue != "Y"
    assert bundle.sec_tickers_by_symbol == {
        "XQAQ": (SecTickerRecord(9991029, "Sample Issuer AQ Inc.", "XQAQ", "Nasdaq"),)
    }
    assert bundle.sec_funds_by_symbol == {
        "XQAJ": (SecFundRecord(9991004, "S000999904", "C000999941", "XQAJ"),)
    }
    assert bundle.sec_submissions_by_cik == {
        9991029: SecSubmissionRecord(
            9991029,
            "Sample Issuer AQ Inc.",
            "CA",
            ("10-K", "10-Q", "8-K"),
            "1 Issuer Way",
            "",
            "Sampletown",
            "CA",
            "99990",
        ),
        9991004: SecSubmissionRecord(
            9991004,
            "SAMPLE FUNDS AJ",
            "DE",
            ("485BPOS", "497", "N-CSR"),
            "100 Sample Boulevard",
            "",
            "Sampleville",
            "PA",
            "19990",
        ),
    }
    assert bundle.source_sha256 == {
        url: hashlib.sha256(responses[url]).hexdigest() for url in opener.calls
    }
    assert opener.calls == [
        NASDAQ_TRADED_URL,
        SEC_COMPANY_TICKERS_EXCHANGE_URL,
        SEC_COMPANY_TICKERS_MF_URL,
        SEC_SUBMISSION_URL_TEMPLATE.format(cik=9991004),
        SEC_SUBMISSION_URL_TEMPLATE.format(cik=9991029),
    ]


def test_artifact_pins_schema_urls_hashes_and_creation_marker(tmp_path: Path) -> None:
    responses = _sources()
    _bundle, audit, _opener = _load(tmp_path, responses=responses)

    assert audit.artifact_path is not None
    assert audit.artifact_path.parent.name == audit.fingerprint
    assert audit.artifact_path.parent.parent.name == "robinhood-identities"
    raw_text = audit.artifact_path.read_text(encoding="utf-8")
    artifact = _artifact(audit.artifact_path)
    assert artifact["schema"] == 2
    assert artifact["fingerprint"] == audit.fingerprint
    assert artifact["fetched_at"] == "2026-08-17T00:00:00+00:00"
    sources = {entry["url"]: entry for entry in artifact["sources"]}
    assert set(sources) == set(_opener.calls)
    assert sources[NASDAQ_TRADED_URL]["marker"] == "0816202516:59"
    assert sources[SEC_COMPANY_TICKERS_MF_URL]["marker"] is None
    assert artifact["sec_submissions"][1]["business_address"] == {
        "street1": "1 Issuer Way",
        "street2": "",
        "city": "Sampletown",
        "state_or_country": "CA",
        "zip_code": "99990",
    }
    for url in _opener.calls:
        raw = responses[url]
        assert sources[url]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert sources[url]["bytes"] == len(raw)
    payload = {key: value for key, value in artifact.items() if key != "payload_sha256"}
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert artifact["payload_sha256"] == expected
    # The declared SEC contact is a private setting; it never lands on disk.
    assert _SEC_CONTACT not in raw_text


def test_unmatched_symbols_skip_submission_requests(tmp_path: Path) -> None:
    inputs = (ObservedIdentityInput(cusip="99971J105", symbol="NONE", description="Nothing Inc"),)
    bundle, audit, opener = _load(tmp_path, inputs=inputs, manual={})

    assert audit.failure is None
    assert bundle is not None
    assert bundle.sec_submissions_by_cik == {}
    assert not any("/submissions/CIK" in url for url in opener.calls)
    assert not any("/submissions/CIK" in url for url in bundle.source_sha256)


def test_duplicate_sec_ticker_rows_keep_every_candidate(tmp_path: Path) -> None:
    rows = [*_TICKER_ROWS, [9991040, "Sample Issuer AQ Holdings Ltd", "XQAQ", "NYSE"]]
    bundle, audit, _opener = _load(
        tmp_path,
        responses={
            SEC_COMPANY_TICKERS_EXCHANGE_URL: _tickers_bytes(rows),
            SEC_SUBMISSION_URL_TEMPLATE.format(cik=9991040): json.dumps(
                {
                    "cik": 9991040,
                    "name": "Sample Issuer AQ Holdings Ltd",
                    "stateOfIncorporation": "DE",
                    "filings": {"recent": {"form": ["10-K"]}},
                }
            ).encode(),
        },
    )

    assert audit.failure is None
    assert bundle is not None
    assert bundle.sec_tickers_by_symbol["XQAQ"] == (
        SecTickerRecord(9991029, "Sample Issuer AQ Inc.", "XQAQ", "Nasdaq"),
        SecTickerRecord(9991040, "Sample Issuer AQ Holdings Ltd", "XQAQ", "NYSE"),
    )
    assert set(bundle.sec_submissions_by_cik) == {9991004, 9991040, 9991029}


def test_null_sec_exchange_becomes_empty_string(tmp_path: Path) -> None:
    inputs = (ObservedIdentityInput(cusip="99971J105", symbol="ZZZZ", description="Other Corp"),)
    bundle, audit, _opener = _load(
        tmp_path,
        inputs=inputs,
        manual={},
        responses={},
    )

    assert audit.failure is None
    assert bundle is not None
    assert bundle.sec_tickers_by_symbol == {
        "ZZZZ": (SecTickerRecord(9991050, "Other Corp", "ZZZZ", ""),)
    }
    assert bundle.sec_submissions_by_cik == {
        9991050: SecSubmissionRecord(9991050, "Other Corp", "DE", ("10-K",))
    }
    assert bundle.nasdaq_by_symbol["ZZZZ"] == NasdaqTradedRecord(
        "ZZZZ",
        "Test Issue Corp - Common Stock",
        "Q",
        NasdaqFlag.NO,
        NasdaqFlag.YES,
        NasdaqFlag.UNAVAILABLE,
    )

    cached, cached_audit, _blocked = _load(tmp_path, inputs=inputs, manual={}, opener=_no_network)

    assert cached_audit.cache_hit is True
    assert cached == bundle


def test_optional_business_address_fields_never_fail_the_snapshot(tmp_path: Path) -> None:
    submission = {
        **_ISSUER_AQ,
        "addresses": {
            "business": {
                "street1": None,
                "street2": 7,
                "city": "",
                "stateOrCountry": None,
                "zipCode": None,
            }
        },
    }
    bundle, audit, _opener = _load(
        tmp_path,
        responses={
            SEC_SUBMISSION_URL_TEMPLATE.format(cik=9991029): json.dumps(submission).encode()
        },
    )

    assert audit.failure is None
    assert bundle is not None
    record = bundle.sec_submissions_by_cik[9991029]
    assert (
        record.business_street1,
        record.business_street2,
        record.business_city,
        record.business_state_or_country,
        record.business_zip_code,
    ) == ("", "", "", "", "")


@pytest.mark.parametrize(
    "responses",
    [
        pytest.param({NASDAQ_TRADED_URL: _nasdaq_bytes(header="Symbol|Name")}, id="header-drift"),
        pytest.param({NASDAQ_TRADED_URL: _nasdaq_bytes(trailer=None)}, id="missing-trailer"),
        pytest.param(
            {NASDAQ_TRADED_URL: _nasdaq_bytes(trailer="File Creation Time: |||||||||||")},
            id="empty-trailer",
        ),
        pytest.param(
            {NASDAQ_TRADED_URL: _nasdaq_bytes(rows=(*_ROWS, _ROWS[0]))},
            id="duplicate-symbol",
        ),
        pytest.param(
            {NASDAQ_TRADED_URL: _nasdaq_bytes(rows=("Y|XQAQ|Sample Issuer AQ Inc.|Q|Q|N|100|N",))},
            id="short-row",
        ),
        pytest.param(
            {NASDAQ_TRADED_URL: _nasdaq_bytes(rows=(_ROWS[0].replace("|100|N|N|", "|100|X|N|"),))},
            id="unknown-test-issue-flag",
        ),
        pytest.param(
            {NASDAQ_TRADED_URL: _nasdaq_bytes(rows=(_ROWS[0].replace("|100|N|N|", "|100| |N|"),))},
            id="blank-test-issue-flag",
        ),
        pytest.param(
            {
                NASDAQ_TRADED_URL: _nasdaq_bytes(
                    rows=("Y||Sample Issuer AQ Inc.|Q|Q|N|100|N|N|XQAQ|XQAQ|N",)
                )
            },
            id="missing-symbol",
        ),
        pytest.param(
            {NASDAQ_TRADED_URL: _nasdaq_bytes(rows=(_TRAILER,))},
            id="misplaced-trailer",
        ),
        pytest.param({NASDAQ_TRADED_URL: _nasdaq_bytes(rows=())}, id="no-rows"),
        pytest.param({NASDAQ_TRADED_URL: b"\xff\xfe not utf-8"}, id="not-utf-8"),
        pytest.param({NASDAQ_TRADED_URL: b""}, id="empty-response"),
    ],
)
def test_nasdaq_schema_drift_fails_explicitly(
    tmp_path: Path, responses: Mapping[str, bytes]
) -> None:
    bundle, audit, _opener = _load(tmp_path, responses=responses)

    assert bundle is None
    assert audit.failure is not None
    assert "Nasdaq Trader" in audit.failure
    assert audit.artifact_path is None
    assert not (tmp_path / "robinhood-identities").exists()


@pytest.mark.parametrize(
    ("responses", "expected"),
    [
        pytest.param(
            {
                SEC_COMPANY_TICKERS_EXCHANGE_URL: _tickers_bytes(
                    fields=("cik", "name", "symbol", "exchange")
                )
            },
            "ticker/exchange",
            id="ticker-field-rename",
        ),
        pytest.param(
            {
                SEC_COMPANY_TICKERS_EXCHANGE_URL: _tickers_bytes(
                    rows=[[9991029, "Sample Issuer AQ", "XQAQ"]]
                )
            },
            "ticker/exchange",
            id="ticker-row-shape",
        ),
        pytest.param(
            {SEC_COMPANY_TICKERS_EXCHANGE_URL: _tickers_bytes(rows=[])},
            "ticker/exchange",
            id="ticker-no-rows",
        ),
        pytest.param(
            {SEC_COMPANY_TICKERS_EXCHANGE_URL: b"{not json"},
            "ticker/exchange",
            id="ticker-not-json",
        ),
        pytest.param(
            {SEC_COMPANY_TICKERS_EXCHANGE_URL: b"[]"},
            "ticker/exchange",
            id="ticker-not-object",
        ),
        pytest.param(
            {
                SEC_COMPANY_TICKERS_EXCHANGE_URL: _tickers_bytes(
                    rows=[["not-a-cik", "Sample Issuer AQ Inc.", "XQAQ", "Nasdaq"]]
                )
            },
            "ticker/exchange",
            id="ticker-bad-cik",
        ),
        pytest.param(
            {
                SEC_COMPANY_TICKERS_EXCHANGE_URL: _tickers_bytes(
                    rows=[[9991029, "", "XQAQ", "Nasdaq"]]
                )
            },
            "ticker/exchange",
            id="ticker-missing-name",
        ),
        pytest.param(
            {
                SEC_COMPANY_TICKERS_EXCHANGE_URL: _tickers_bytes(
                    rows=[[0, "Sample Issuer AQ Inc.", "XQAQ", "Nasdaq"]]
                )
            },
            "ticker/exchange",
            id="ticker-non-positive-cik",
        ),
        pytest.param(
            {
                SEC_COMPANY_TICKERS_EXCHANGE_URL: _tickers_bytes(
                    rows=[[True, "Sample Issuer AQ Inc.", "XQAQ", "Nasdaq"]]
                )
            },
            "ticker/exchange",
            id="ticker-boolean-cik",
        ),
        pytest.param(
            {
                SEC_COMPANY_TICKERS_EXCHANGE_URL: _tickers_bytes(
                    rows=[[9991029, 42, "XQAQ", "Nasdaq"]]
                )
            },
            "ticker/exchange",
            id="ticker-non-text-name",
        ),
        pytest.param(
            {
                SEC_COMPANY_TICKERS_MF_URL: _funds_bytes(
                    fields=("cik", "seriesId", "classId", "ticker")
                )
            },
            "fund class",
            id="fund-field-rename",
        ),
        pytest.param(
            {SEC_COMPANY_TICKERS_MF_URL: _funds_bytes(rows=[[9991004, "", "C000999941", "XQAJ"]])},
            "fund class",
            id="fund-missing-series",
        ),
    ],
)
def test_sec_schema_drift_fails_explicitly(
    tmp_path: Path, responses: Mapping[str, bytes], expected: str
) -> None:
    bundle, audit, _opener = _load(tmp_path, responses=responses)

    assert bundle is None
    assert audit.failure is not None
    assert expected in audit.failure


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"{not json", id="not-json"),
        pytest.param(json.dumps({**_ISSUER_AQ, "cik": 9991001}).encode(), id="other-filer"),
        pytest.param(
            json.dumps({k: v for k, v in _ISSUER_AQ.items() if k != "name"}).encode(),
            id="missing-field",
        ),
        pytest.param(
            json.dumps({**_ISSUER_AQ, "filings": {"files": []}}).encode(),
            id="no-current-filings",
        ),
        pytest.param(
            json.dumps({**_ISSUER_AQ, "filings": {"recent": {"form": ["10-K", 7]}}}).encode(),
            id="non-text-form",
        ),
        pytest.param(
            json.dumps(
                {**_ISSUER_AQ, "filings": {"recent": {"form": []}, "older": {"form": "10-K"}}}
            ).encode(),
            id="malformed-form-list",
        ),
        pytest.param(json.dumps({**_ISSUER_AQ, "name": "  "}).encode(), id="empty-name"),
        pytest.param(
            json.dumps({**_ISSUER_AQ, "filings": []}).encode(), id="filings-not-an-object"
        ),
    ],
)
def test_submission_record_problems_fail_explicitly(raw: bytes) -> None:
    with pytest.raises(IdentitySourceError, match="submissions"):
        _parse_submission(raw, cik=9991029)


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic_and_order_case_insensitive() -> None:
    shuffled = (
        ObservedIdentityInput(
            cusip=" 99990d105 ", symbol="xqaj", description="sample  fund aj  etf"
        ),
        ObservedIdentityInput(
            cusip="99992A109", symbol="xqaq", description="sample issuer aq inc common\tstock"
        ),
        ObservedIdentityInput(
            cusip="99992A109", symbol="XQAQ", description="SAMPLE ISSUER AQ INC COMMON STOCK"
        ),
    )
    manual_reordered = {"xqaq": dict(reversed(list(_MANUAL["XQAQ"].items())))}

    assert identity_input_fingerprint(shuffled, manual_reordered) == identity_input_fingerprint(
        _INPUTS, _MANUAL
    )
    assert len(identity_input_fingerprint(_INPUTS, _MANUAL)) == 64


@pytest.mark.parametrize(
    ("inputs", "manual", "schema_version"),
    [
        pytest.param(_INPUTS[:1], _MANUAL, SCHEMA_VERSION, id="fewer-securities"),
        pytest.param(
            (replace(_INPUTS[0], cusip="99992A100"), _INPUTS[1]),
            _MANUAL,
            SCHEMA_VERSION,
            id="changed-cusip",
        ),
        pytest.param(
            (replace(_INPUTS[0], symbol="XQFC"), _INPUTS[1]),
            _MANUAL,
            SCHEMA_VERSION,
            id="changed-symbol",
        ),
        pytest.param(
            _INPUTS,
            {"XQAQ": {**_MANUAL["XQAQ"], "listing_exchange": "NYSE"}},
            SCHEMA_VERSION,
            id="changed-manual-fields",
        ),
        pytest.param(_INPUTS, _MANUAL, SCHEMA_VERSION + 1, id="changed-schema-version"),
    ],
)
def test_identity_relevant_changes_move_the_fingerprint(
    inputs: Sequence[ObservedIdentityInput],
    manual: Mapping[str, Mapping[str, str]],
    schema_version: int,
) -> None:
    """Each case differs from the baseline in exactly the one thing its id names."""
    assert identity_input_fingerprint(inputs, manual, schema_version) != identity_input_fingerprint(
        _INPUTS, _MANUAL, SCHEMA_VERSION
    )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_cache_hit_reuses_byte_identical_content_without_transport(tmp_path: Path) -> None:
    first, first_audit, _opener = _load(tmp_path)
    assert first_audit.artifact_path is not None
    written = first_audit.artifact_path.read_bytes()

    second, second_audit, _blocked = _load(tmp_path, opener=_no_network)

    assert second_audit.cache_hit is True
    assert second_audit.failure is None
    assert second_audit.fingerprint == first_audit.fingerprint
    assert second == first
    assert first_audit.artifact_path.read_bytes() == written


def test_changed_inputs_build_a_new_snapshot_and_keep_the_old_one(tmp_path: Path) -> None:
    _first, first_audit, _opener = _load(tmp_path)
    assert first_audit.artifact_path is not None
    original = first_audit.artifact_path.read_bytes()

    changed = (*_INPUTS, ObservedIdentityInput(cusip="99990A309", symbol="XQAA", description="A"))
    second, second_audit, _other = _load(tmp_path, inputs=changed)

    assert second is not None
    assert second_audit.failure is None
    assert second_audit.cache_hit is False
    assert second_audit.fingerprint != first_audit.fingerprint
    assert second_audit.artifact_path != first_audit.artifact_path
    assert first_audit.artifact_path.read_bytes() == original
    assert "XQAA" in second.sec_tickers_by_symbol
    snapshots = sorted((tmp_path / "robinhood-identities").iterdir())
    assert len(snapshots) == 2


def test_cold_cache_without_fetch_returns_a_failure_and_no_transport(tmp_path: Path) -> None:
    bundle, audit, _blocked = _load(tmp_path, fetch=False, opener=_no_network)

    assert bundle is None
    assert audit.cache_hit is False
    assert audit.artifact_path is None
    assert audit.failure == (
        "identity reference sources are not cached for these inputs and fetching is disabled"
    )
    assert not (tmp_path / "robinhood-identities").exists()


def test_warm_cache_is_served_with_fetch_disabled(tmp_path: Path) -> None:
    expected, _audit, _opener = _load(tmp_path)

    bundle, audit, _blocked = _load(tmp_path, fetch=False, sec_user_agent=None, opener=_no_network)

    assert audit.cache_hit is True
    assert bundle == expected


def test_post_fy_snapshot_is_reused_across_input_fingerprints(tmp_path: Path) -> None:
    expected, first_audit, _opener = _load(tmp_path)

    bundle, audit = load_post_fy_identity_sources(
        {"XQAQ"},
        required_fund_symbols={"XQAJ"},
        cache_dir=tmp_path,
        financial_year_end=date(2026, 3, 31),
        fetch=False,
        sec_user_agent=_SEC_CONTACT,
    )

    assert bundle == expected
    assert audit.cache_hit
    assert audit.artifact_path == first_audit.artifact_path


def test_post_fy_snapshot_allows_per_security_common_stock_coverage(
    tmp_path: Path,
) -> None:
    expected, _first_audit, _opener = _load(tmp_path)

    bundle, audit = load_post_fy_identity_sources(
        {"XQAQ", "MISSING"},
        cache_dir=tmp_path,
        financial_year_end=date(2026, 3, 31),
        fetch=False,
        sec_user_agent=_SEC_CONTACT,
    )

    assert bundle == expected
    assert audit.cache_hit


def test_post_fy_snapshot_allows_per_security_fund_coverage(tmp_path: Path) -> None:
    expected, _first_audit, _opener = _load(tmp_path)

    bundle, audit = load_post_fy_identity_sources(
        {"XQAQ"},
        required_fund_symbols={"XQAJ", "XQFB"},
        cache_dir=tmp_path,
        financial_year_end=date(2026, 3, 31),
        fetch=False,
        sec_user_agent=_SEC_CONTACT,
    )

    assert bundle == expected
    assert audit.cache_hit


def test_pre_fy_end_snapshot_is_not_final_for_that_filing(tmp_path: Path) -> None:
    _load(tmp_path, now=datetime(2026, 3, 31, 23, 59, tzinfo=UTC))

    bundle, audit = load_post_fy_identity_sources(
        {"XQAQ"},
        cache_dir=tmp_path,
        financial_year_end=date(2026, 3, 31),
        fetch=False,
        sec_user_agent=_SEC_CONTACT,
        current_identity_fingerprint=identity_input_fingerprint(_INPUTS, _MANUAL),
    )

    assert bundle is None
    assert audit.failure == (
        "the current identity snapshot predates the target FY end and cannot be "
        "refreshed in place; use a new cache directory for a post-FY fetch"
    )
    assert audit.artifact_path is not None


def test_pre_fy_snapshot_for_another_fingerprint_does_not_block_fetch(
    tmp_path: Path,
) -> None:
    _load(tmp_path, now=datetime(2026, 3, 31, 23, 59, tzinfo=UTC))

    bundle, audit = load_post_fy_identity_sources(
        {"XQAQ"},
        cache_dir=tmp_path,
        financial_year_end=date(2026, 3, 31),
        fetch=False,
        sec_user_agent=_SEC_CONTACT,
        current_identity_fingerprint="0" * 64,
    )

    assert bundle is None
    assert audit.artifact_path is None
    assert audit.failure == (
        "no verified covering identity snapshot was fetched after the target financial year"
    )


def test_post_fy_snapshot_must_cover_symbols_and_pass_integrity(tmp_path: Path) -> None:
    _bundle, first_audit, _opener = _load(tmp_path)
    assert first_audit.artifact_path is not None

    uncovered, uncovered_audit = load_post_fy_identity_sources(
        {"MISSING"},
        cache_dir=tmp_path,
        financial_year_end=date(2026, 3, 31),
        fetch=False,
        sec_user_agent=_SEC_CONTACT,
    )
    assert uncovered is None
    assert uncovered_audit.artifact_path is None

    artifact = _artifact(first_audit.artifact_path)
    artifact["fetched_at"] = "2026-08-18T00:00:00+00:00"
    first_audit.artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(SecEntityCacheError) as refusal:
        load_post_fy_identity_sources(
            {"XQAQ"},
            cache_dir=tmp_path,
            financial_year_end=date(2026, 3, 31),
            fetch=False,
            sec_user_agent=_SEC_CONTACT,
        )
    assert str(refusal.value) == _snapshot_refusal(tmp_path, _INTEGRITY, fetch=False)


def test_fetch_disabled_can_reuse_a_verified_v1_snapshot(tmp_path: Path) -> None:
    legacy_fingerprint = identity_input_fingerprint(_INPUTS, _MANUAL, 1)
    legacy_path = _legacy_snapshot(tmp_path)

    bundle, audit, _blocked = _load(
        tmp_path,
        fetch=False,
        sec_user_agent=None,
        opener=_no_network,
    )

    assert audit.cache_hit is True
    assert audit.fingerprint == legacy_fingerprint
    assert audit.artifact_path == legacy_path
    assert bundle is not None
    # A v1 snapshot keeps no business address, so those fields stay empty.
    assert bundle.sec_submissions_by_cik == {
        9991029: SecSubmissionRecord(
            9991029, "Sample Issuer AQ Inc.", "CA", ("10-K", "10-Q", "8-K"), "", "", "", "", ""
        ),
        9991004: SecSubmissionRecord(
            9991004, "SAMPLE FUNDS AJ", "DE", ("485BPOS", "497", "N-CSR"), "", "", "", "", ""
        ),
    }


# ---------------------------------------------------------------------------
# Post-FY snapshot search refusals (#853)
# ---------------------------------------------------------------------------

_TOO_DEEP = "the identity source cache artifact is nested too deeply"
_UNSUPPORTED_SCHEMA = "the identity source cache artifact has an unsupported schema"
_NO_SNAPSHOT = "no verified covering identity snapshot was fetched after the target financial year"
_ENFORCES_PERMISSIONS = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)


def _non_finite(path: Path) -> None:
    artifact = _artifact(path)
    artifact["sources"][0]["bytes"] = float("nan")
    path.write_text(json.dumps(artifact), encoding="utf-8")


def _schema(value: object) -> Callable[[Path], None]:
    return _resealed(lambda artifact: artifact.update(schema=value))


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize("snapshot", _SNAPSHOTS)
@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        pytest.param(_tamper, _INTEGRITY, id="integrity"),
        pytest.param(
            lambda path: path.write_bytes(b"not json"),
            IDENTITY_CACHE_MALFORMED,
            id="not-json",
        ),
        pytest.param(lambda path: path.write_bytes(DEEP_JSON), _TOO_DEEP, id="deep-json"),
        pytest.param(_non_finite, _NON_FINITE, id="non-finite-number"),
        pytest.param(
            _dangle,
            "the identity source cache artifact could not be read (ENOENT)",
            id="dangling-link",
        ),
        pytest.param(_schema(True), _UNSUPPORTED_SCHEMA, id="schema-true"),
        pytest.param(_schema(2.0), _UNSUPPORTED_SCHEMA, id="schema-float"),
        pytest.param(_schema(0), _UNSUPPORTED_SCHEMA, id="schema-0"),
        pytest.param(_schema(3), _UNSUPPORTED_SCHEMA, id="schema-3"),
        pytest.param(
            _resealed(lambda artifact: artifact.update(fingerprint="0" * 64)),
            _INVALID_RECORD,
            id="fingerprint-not-its-folder",
        ),
        pytest.param(
            _resealed(lambda artifact: artifact["sec_tickers"][0].update(cik="9991029")),
            _INVALID_RECORD,
            id="invalid-record",
        ),
    ],
)
def test_post_fy_search_refuses_an_unusable_artifact(
    tmp_path: Path,
    snapshot: Callable[[Path], Path],
    damage: Callable[[Path], object],
    problem: str,
    fetch: bool,
) -> None:
    """With fetching off, the remedy says the rerun must fetch (#1521)."""
    path = snapshot(tmp_path)
    damage(path)
    written = _cache_state(tmp_path)

    # #853: on b41cf75 each returned a failure audit, escaped untyped, or was
    # skipped as if absent.
    with pytest.raises(SecEntityCacheError) as refusal:
        _post_fy(tmp_path, fetch=fetch)
    message = str(refusal.value)
    assert message == _snapshot_refusal(tmp_path, problem, fetch=fetch)
    # #1071: on a43eebd it printed the artifact's path.
    assert_no_markers(message, path.parent.name, "sources.json")
    assert _cache_state(tmp_path) == written


def _exhaust(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    stage: str,
    error: type[BaseException],
) -> None:
    """Fail reading or re-hashing ``path`` only, as running out of memory would."""
    if stage == "read":
        read_text = Path.read_text

        def failing_read(self: Path, *args, **kwargs) -> str:
            if self == path:
                raise error("private cached value")
            return read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", failing_read)
    else:
        dumps = json.dumps

        def failing_dumps(value, *args, **kwargs) -> str:
            if isinstance(value, dict) and value.get("fingerprint") == path.parent.name:
                raise error("private cached value")
            return dumps(value, *args, **kwargs)

        monkeypatch.setattr(json, "dumps", failing_dumps)


_EXHAUSTION = [
    pytest.param("read", MemoryError, _UNREADABLE, id="read-memory"),
    pytest.param("re-hash", MemoryError, _UNREADABLE, id="re-hash-memory"),
    pytest.param("re-hash", RecursionError, _TOO_DEEP, id="re-hash-recursion"),
]


@pytest.mark.parametrize("snapshot", _SNAPSHOTS)
@pytest.mark.parametrize(("stage", "error", "problem"), _EXHAUSTION)
def test_post_fy_search_refuses_on_a_read_or_re_hash_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: Callable[[Path], Path],
    stage: str,
    error: type[BaseException],
    problem: str,
) -> None:
    path = snapshot(tmp_path)
    written = _cache_state(tmp_path)
    _exhaust(monkeypatch, path, stage, error)

    with pytest.raises(SecEntityCacheError) as refusal:
        _post_fy(tmp_path)
    message = _snapshot_refusal(tmp_path, problem, fetch=False)
    assert type(refusal.value) is SecEntityCacheError
    assert refusal.value.args == (message,)
    assert str(refusal.value) == message
    assert vars(refusal.value) == {}
    # #1222: on 2c018e3 it chained the failure, with its text.
    assert_replaced_unchained(refusal.value, error, markers=("private cached value",))
    assert _cache_state(tmp_path) == written


@_ENFORCES_PERMISSIONS
@pytest.mark.parametrize("snapshot", _SNAPSHOTS)
def test_post_fy_search_refuses_an_unreadable_artifact(
    tmp_path: Path,
    snapshot: Callable[[Path], Path],
) -> None:
    path = snapshot(tmp_path)
    written = _cache_state(tmp_path)
    path.chmod(0)
    try:
        with pytest.raises(SecEntityCacheError) as refusal:
            _post_fy(tmp_path)
    finally:
        path.chmod(0o600)

    assert str(refusal.value) == _snapshot_refusal(
        tmp_path, "the identity source cache artifact could not be read (EACCES)", fetch=False
    )
    assert _cache_state(tmp_path) == written


def _regular_file(path: Path) -> None:
    path.write_bytes(b"")


def _broken_link(path: Path) -> None:
    path.symlink_to(path.with_name("missing"))


def _looping_link(path: Path) -> None:
    path.symlink_to(path.name)


_UNUSABLE_ENTRIES = [
    pytest.param(_regular_file, "the cache entry is not a folder", id="regular-file"),
    pytest.param(
        _broken_link,
        "the cache entry is a link that can't be followed (ENOENT)",
        id="broken-link",
    ),
    pytest.param(
        _looping_link,
        "the cache entry is a link that can't be followed (ELOOP)",
        id="looping-link",
    ),
]


@pytest.mark.parametrize(("place", "problem"), _UNUSABLE_ENTRIES)
def test_post_fy_search_refuses_an_unusable_identities_folder(
    tmp_path: Path,
    place: Callable[[Path], None],
    problem: str,
) -> None:
    root = tmp_path / "robinhood-identities"
    place(root)
    written = _cache_state(tmp_path)

    with pytest.raises(SecEntityCacheError) as refusal:
        _post_fy(tmp_path)
    assert str(refusal.value) == _snapshot_refusal(tmp_path, problem, fetch=False)
    assert _cache_state(tmp_path) == written


@pytest.mark.parametrize(("place", "problem"), _UNUSABLE_ENTRIES)
def test_post_fy_search_refuses_an_unusable_fingerprint_entry(
    tmp_path: Path,
    place: Callable[[Path], None],
    problem: str,
) -> None:
    _covering(tmp_path)
    entry = tmp_path / "robinhood-identities" / HOLDER
    place(entry)
    written = _cache_state(tmp_path)

    with pytest.raises(SecEntityCacheError) as refusal:
        _post_fy(tmp_path)
    message = str(refusal.value)
    assert type(refusal.value) is SecEntityCacheError
    assert refusal.value.args == (_snapshot_refusal(tmp_path, problem, fetch=False),)
    assert message == _snapshot_refusal(tmp_path, problem, fetch=False)
    assert vars(refusal.value) == {}
    # #1071: on a43eebd it printed the entry's path, and so its name.
    assert_no_markers(message, HOLDER)
    # #1222: on 2c018e3 a link's OSError, naming that path, stayed attached.
    assert_replaced_unchained(refusal.value, markers=(HOLDER,))
    assert _cache_state(tmp_path) == written


@_ENFORCES_PERMISSIONS
@pytest.mark.parametrize("fingerprint_folder", [False, True], ids=["identities", "fingerprint"])
def test_post_fy_search_refuses_an_unlistable_folder(
    tmp_path: Path,
    fingerprint_folder: bool,
) -> None:
    path = _covering(tmp_path)
    locked = path.parent if fingerprint_folder else tmp_path / "robinhood-identities"
    written = _cache_state(tmp_path)
    locked.chmod(0)
    try:
        with pytest.raises(SecEntityCacheError) as refusal:
            _post_fy(tmp_path)
    finally:
        locked.chmod(0o700)

    message = str(refusal.value)
    assert message == _snapshot_refusal(
        tmp_path, "the cache folder could not be listed (EACCES)", fetch=False
    )
    # #1071: on a43eebd a locked fingerprint folder printed its path.
    assert_no_markers(message, path.parent.name)
    assert _cache_state(tmp_path) == written


def test_post_fy_search_refuses_an_identities_folder_it_cannot_inspect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _covering(tmp_path)
    root = tmp_path / "robinhood-identities"
    lstat = Path.lstat

    def inaccessible(self: Path):
        if self == root:
            raise PermissionError("cache inspection denied")
        return lstat(self)

    monkeypatch.setattr(Path, "lstat", inaccessible)

    with pytest.raises(SecEntityCacheError) as refusal:
        _post_fy(tmp_path)
    assert str(refusal.value) == _snapshot_refusal(
        tmp_path, "the cache entry could not be inspected (unknown error)", fetch=False
    )


@pytest.mark.parametrize(
    ("method", "problem"),
    [
        pytest.param("lstat", "the cache entry could not be inspected", id="inspect"),
        pytest.param("stat", "the cache entry is a link that can't be followed", id="follow"),
        pytest.param("iterdir", "the cache folder could not be listed", id="list"),
        pytest.param(
            "read_text", "the identity source cache artifact could not be read", id="read"
        ),
    ],
)
@pytest.mark.parametrize(
    ("code", "name"),
    [
        pytest.param(errno.EACCES, "EACCES", id="eacces"),
        pytest.param(None, "unknown error", id="no-errno"),
        pytest.param(max(errno.errorcode) + 1, "unknown error", id="unnamed-errno"),
    ],
)
def test_post_fy_search_os_error_prints_only_its_errno_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    problem: str,
    code: int | None,
    name: str,
) -> None:
    root = tmp_path / "robinhood-identities"
    root.mkdir()
    # A folder in the cache root; to be followed, a link to one outside it.
    entry = root / HOLDER
    folder = tmp_path / HOLDER if method == "stat" else entry
    folder.mkdir()
    (folder / "sources.json").write_text("{}", encoding="utf-8")
    if folder != entry:
        entry.symlink_to(folder)
    failed = entry / "sources.json" if method == "read_text" else entry
    written = _cache_state(tmp_path)
    original = getattr(Path, method)

    def failing(self: Path, *args: object, **kwargs: object):
        # ``lstat`` may call ``stat(follow_symlinks=False)``; only the call
        # under test fails.
        if self == failed and kwargs.get("follow_symlinks", True):
            raise OSError(code, STRERROR, FILENAME)
        return original(self, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(Path, method, failing)
        with pytest.raises(SecEntityCacheError) as refusal:
            _post_fy(tmp_path)
    message = str(refusal.value)
    assert type(refusal.value) is SecEntityCacheError
    assert refusal.value.args == (_snapshot_refusal(tmp_path, f"{problem} ({name})", fetch=False),)
    assert message == _snapshot_refusal(tmp_path, f"{problem} ({name})", fetch=False)
    assert vars(refusal.value) == {}
    # #1071: on a43eebd it printed a path holding the entry's name, dropped
    # the errno and chained the OSError. #1222: on 2c018e3 it still kept that
    # OSError as a suppressed context, with its text and file name.
    assert_no_markers(message, HOLDER, STRERROR, FILENAME)
    assert_replaced_unchained(refusal.value, markers=(HOLDER, STRERROR, FILENAME))
    assert _cache_state(tmp_path) == written


def _interrupted_write(cache_dir: Path) -> None:
    folder = cache_dir / "robinhood-identities" / ("0" * 64)
    folder.mkdir(parents=True)
    (folder / ".sources.json.abc.tmp").write_text("partial", encoding="utf-8")


@pytest.mark.parametrize(
    "prepare",
    [
        pytest.param(lambda _cache_dir: None, id="no-cache"),
        pytest.param(
            lambda cache_dir: (cache_dir / "robinhood-identities").mkdir(), id="empty-cache"
        ),
        pytest.param(_interrupted_write, id="folder-without-artifact"),
        pytest.param(_legacy_snapshot, id="valid-schema-1"),
        pytest.param(_non_covering, id="valid-non-covering"),
    ],
)
def test_post_fy_search_still_skips_what_it_cannot_use(
    tmp_path: Path,
    prepare: Callable[[Path], object],
) -> None:
    prepare(tmp_path)

    bundle, audit = _post_fy(tmp_path)

    assert bundle is None
    assert audit.cache_hit is False
    assert audit.failure == _NO_SNAPSHOT
    assert audit.artifact_path is None


@pytest.mark.parametrize(
    ("damage", "failure"),
    [
        pytest.param(lambda path: path.write_bytes(DEEP_JSON), _TOO_DEEP, id="deep-json"),
        pytest.param(_non_finite, _NON_FINITE, id="non-finite-number"),
    ],
)
def test_unusable_own_artifact_becomes_an_audit_failure(
    tmp_path: Path,
    damage: Callable[[Path], object],
    failure: str,
) -> None:
    path = _covering(tmp_path)
    damage(path)

    # #853: on b41cf75 these escaped load_identity_sources untyped.
    bundle, audit, _blocked = _load(tmp_path, opener=_no_network)

    assert bundle is None
    assert audit.failure == failure
    assert audit.artifact_path == path


@pytest.mark.parametrize(("stage", "error", "failure"), _EXHAUSTION)
def test_own_artifact_read_or_re_hash_failure_becomes_an_audit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error: type[BaseException],
    failure: str,
) -> None:
    path = _covering(tmp_path)
    _exhaust(monkeypatch, path, stage, error)

    bundle, audit, _blocked = _load(tmp_path, opener=_no_network)

    assert bundle is None
    assert audit.failure == failure
    assert audit.artifact_path == path


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            # Rows are stored by symbol, so [0] is XQAJ's ETF row; flip its flag.
            lambda artifact: artifact["nasdaq"][0].update({"etf": "N"}),
            id="record-tampered",
        ),
        pytest.param(
            lambda artifact: artifact["sources"][0].update({"sha256": "0" * 64}),
            id="source-hash-tampered",
        ),
        pytest.param(
            lambda artifact: artifact["sec_submissions"][1].update(
                {"state_of_incorporation": "DE"}
            ),
            id="incorporation-tampered",
        ),
    ],
)
def test_tampered_artifact_is_refused_and_never_refetched(tmp_path: Path, mutate) -> None:
    _bundle, audit, _opener = _load(tmp_path)
    assert audit.artifact_path is not None
    artifact = _artifact(audit.artifact_path)
    mutate(artifact)
    _rewrite(audit.artifact_path, artifact)
    tampered = audit.artifact_path.read_bytes()

    bundle, second_audit, _blocked = _load(tmp_path, opener=_no_network)

    assert bundle is None
    assert second_audit.cache_hit is False
    assert second_audit.failure == ("the identity source cache artifact failed its integrity check")
    assert second_audit.artifact_path == audit.artifact_path
    assert audit.artifact_path.read_bytes() == tampered


@pytest.mark.parametrize(
    ("mutate", "reseal", "expected"),
    [
        pytest.param(
            lambda artifact: artifact.pop("payload_sha256"),
            False,
            "the identity source cache artifact has no payload hash",
            id="hashless",
        ),
        pytest.param(
            lambda artifact: artifact.update({"payload_sha256": "short"}),
            False,
            "the identity source cache artifact has no payload hash",
            id="malformed-hash",
        ),
        pytest.param(
            lambda artifact: artifact.update({"schema": 3}),
            True,
            "the identity source cache artifact has an unsupported schema",
            id="schema-drift",
        ),
        pytest.param(
            lambda artifact: artifact.update({"fingerprint": "0" * 64}),
            True,
            "the identity source cache artifact does not match the requested identity inputs",
            id="foreign-fingerprint",
        ),
    ],
)
def test_invalid_cache_artifacts_are_refused(
    tmp_path: Path, mutate, reseal: bool, expected: str
) -> None:
    _bundle, audit, _opener = _load(tmp_path)
    assert audit.artifact_path is not None
    artifact = _artifact(audit.artifact_path)
    mutate(artifact)
    if reseal:
        _reseal(artifact)
    _rewrite(audit.artifact_path, artifact)

    bundle, second_audit, _blocked = _load(tmp_path, opener=_no_network)

    assert bundle is None
    assert second_audit.failure == expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("not json at all", id="not-json"),
        pytest.param("[]", id="not-an-object"),
    ],
)
def test_unparseable_cache_artifact_is_refused(tmp_path: Path, text: str) -> None:
    _bundle, audit, _opener = _load(tmp_path)
    assert audit.artifact_path is not None
    audit.artifact_path.write_text(text, encoding="utf-8")

    bundle, second_audit, _blocked = _load(tmp_path, opener=_no_network)

    assert bundle is None
    assert second_audit.failure == IDENTITY_CACHE_MALFORMED


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda artifact: artifact.update({"nasdaq": "rows"}), id="not-a-list"),
        pytest.param(lambda artifact: artifact.update({"nasdaq": ["row"]}), id="not-a-mapping"),
        pytest.param(
            lambda artifact: artifact["nasdaq"][0].update({"etf": "MAYBE"}), id="unknown-flag"
        ),
        pytest.param(
            lambda artifact: artifact["nasdaq"][0].update({"test_issue": None}), id="null-flag"
        ),
        pytest.param(
            lambda artifact: artifact["nasdaq"][0].update({"test_issue": ""}), id="blank-flag"
        ),
        pytest.param(
            lambda artifact: artifact["nasdaq"][0].update({"symbol": ""}), id="empty-text"
        ),
        pytest.param(
            lambda artifact: artifact["nasdaq"].append(dict(artifact["nasdaq"][0])),
            id="duplicate-symbol",
        ),
        pytest.param(
            lambda artifact: artifact["sec_tickers"][0].update({"cik": "9991029"}), id="text-cik"
        ),
        pytest.param(
            lambda artifact: artifact["sec_submissions"][0].update({"forms": ["8-K", "10-K"]}),
            id="unsorted-forms",
        ),
        pytest.param(
            lambda artifact: artifact["sec_submissions"][0].update({"forms": [1]}),
            id="non-text-forms",
        ),
        pytest.param(
            lambda artifact: artifact["sec_submissions"][0].update({"forms": "10-K"}),
            id="forms-not-a-list",
        ),
        pytest.param(
            lambda artifact: artifact["sec_submissions"].append(
                dict(artifact["sec_submissions"][0])
            ),
            id="duplicate-cik",
        ),
        pytest.param(lambda artifact: artifact.update({"sources": []}), id="no-sources"),
        pytest.param(
            lambda artifact: artifact["sources"][0].update({"sha256": "not-a-hash"}),
            id="bad-source-hash",
        ),
        pytest.param(lambda artifact: artifact.update({"fetched_at": 0}), id="bad-timestamp"),
    ],
)
def test_structurally_invalid_cache_artifacts_are_refused(tmp_path: Path, mutate) -> None:
    _bundle, audit, _opener = _load(tmp_path)
    assert audit.artifact_path is not None
    artifact = _artifact(audit.artifact_path)
    mutate(artifact)
    _reseal(artifact)
    _rewrite(audit.artifact_path, artifact)

    bundle, second_audit, _blocked = _load(tmp_path, opener=_no_network)

    assert bundle is None
    assert second_audit.failure is not None
    assert "cache artifact" in second_audit.failure


def test_existing_artifact_is_never_overwritten_by_a_fetch(tmp_path: Path) -> None:
    _bundle, audit, _opener = _load(tmp_path)
    assert audit.artifact_path is not None
    audit.artifact_path.write_text("not json at all", encoding="utf-8")

    bundle, second_audit, opener = _load(tmp_path)

    assert bundle is None
    assert second_audit.failure is not None
    assert opener.calls == []
    assert audit.artifact_path.read_text(encoding="utf-8") == "not json at all"


def test_oversized_submission_member_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ibkr_fa.robinhood_identity_sources._MAX_SUBMISSION_BYTES", 8)

    bundle, audit, _opener = _load(tmp_path)

    assert bundle is None
    assert audit.failure is not None
    assert "implausibly large" in audit.failure


def test_default_transport_is_used_when_no_opener_is_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = _sources()
    seen: list[str] = []

    def _open(request: urllib.request.Request, _timeout: float) -> tuple[str, bytes]:
        seen.append(request.full_url)
        return request.full_url, responses[request.full_url]

    monkeypatch.setattr("ibkr_fa.robinhood_identity_sources._url_open", _open)

    bundle, audit = load_identity_sources(
        _INPUTS,
        _MANUAL,
        cache_dir=tmp_path,
        fetch=True,
        sec_user_agent=_SEC_CONTACT,
        now=_NOW,
    )

    assert audit.failure is None
    assert bundle is not None
    assert seen == [
        NASDAQ_TRADED_URL,
        SEC_COMPANY_TICKERS_EXCHANGE_URL,
        SEC_COMPANY_TICKERS_MF_URL,
        SEC_SUBMISSION_URL_TEMPLATE.format(cik=9991004),
        SEC_SUBMISSION_URL_TEMPLATE.format(cik=9991029),
    ]


def test_a_snapshot_written_mid_fetch_is_never_overwritten(tmp_path: Path) -> None:
    fingerprint = identity_input_fingerprint(_INPUTS, _MANUAL)
    path = tmp_path / "robinhood-identities" / fingerprint / "sources.json"
    responses = _sources()

    def _racing(url: str) -> bytes:
        if url == SEC_SUBMISSION_URL_TEMPLATE.format(cik=9991029):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("written by a concurrent run", encoding="utf-8")
        return responses[url]

    bundle, audit, _opener = _load(tmp_path, opener=_racing)

    assert bundle is None
    assert audit.failure == "the identity source cache artifact already exists"
    assert audit.artifact_path == path
    assert path.read_text(encoding="utf-8") == "written by a concurrent run"


def test_unreadable_artifact_is_refused(tmp_path: Path) -> None:
    fingerprint = identity_input_fingerprint(_INPUTS, _MANUAL)
    (tmp_path / "robinhood-identities" / fingerprint / "sources.json").mkdir(parents=True)

    bundle, audit, _blocked = _load(tmp_path, opener=_no_network)

    assert bundle is None
    assert audit.failure == "the identity source cache artifact could not be read (EISDIR)"


@_ENFORCES_PERMISSIONS
@pytest.mark.parametrize(
    ("snapshot", "fetch"),
    [
        pytest.param(_covering, False, id="current-offline"),
        pytest.param(_covering, True, id="current-fetch-enabled"),
        pytest.param(_legacy_snapshot, False, id="schema-1-offline"),
    ],
)
def test_snapshot_that_cannot_be_inspected_is_refused_like_an_unreadable_one(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    snapshot: Callable[[Path], Path],
    fetch: bool,
) -> None:
    """#1238: a current or schema-1 snapshot in a folder that can't be searched is
    read, and refused as unreadable, instead of reading as a cold cache or being
    fetched over."""
    path = snapshot(tmp_path)
    written = _cache_state(tmp_path)
    opener = _RecordingOpener(_sources())
    path.parent.chmod(0)
    request.addfinalizer(lambda: path.parent.chmod(0o700))

    bundle, audit, _opener = _load(tmp_path, fetch=fetch, opener=opener)

    assert bundle is None
    assert audit.failure == "the identity source cache artifact could not be read (EACCES)"
    assert audit.artifact_path == path
    assert opener.calls == []
    path.parent.chmod(0o700)
    assert _cache_state(tmp_path) == written


# ---------------------------------------------------------------------------
# SEC fair access and transport
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "contact",
    [
        pytest.param(None, id="unset"),
        pytest.param("   ", id="blank"),
        pytest.param("ibkr-fa user", id="no-contact-address"),
        pytest.param("ibkr-fa\nX-Injected: 1 user@example.com", id="header-injection"),
        pytest.param("a" * 200 + "@example.com", id="implausibly-long"),
    ],
)
def test_cold_fetch_without_a_declared_sec_contact_fails_before_any_request(
    tmp_path: Path, contact: str | None
) -> None:
    bundle, audit, opener = _load(tmp_path, sec_user_agent=contact)

    assert bundle is None
    assert opener.calls == []
    # #1526: the reason names the key to set.
    assert audit.failure == (
        "a declared SEC fair-access contact (User-Agent) is required to fetch identity sources; "
        f"{SET_SEC_CONTACT}"
    )
    assert not (tmp_path / "robinhood-identities").exists()


def test_transport_declares_the_sec_contact_only_to_sec_hosts() -> None:
    seen: list[urllib.request.Request] = []

    def _open(request: urllib.request.Request, timeout: float) -> tuple[str, bytes]:
        seen.append(request)
        assert timeout == 30.0
        return request.full_url, b"payload"

    assert fetch_bytes(NASDAQ_TRADED_URL, sec_user_agent=_SEC_CONTACT, url_open=_open) == b"payload"
    assert fetch_bytes(SEC_COMPANY_TICKERS_MF_URL, sec_user_agent=_SEC_CONTACT, url_open=_open) == (
        b"payload"
    )

    assert seen[0].get_header("User-agent") == "ibkr-fa Schedule-FA identity reference fetch"
    assert _SEC_CONTACT not in str(seen[0].header_items())
    assert seen[1].get_header("User-agent") == _SEC_CONTACT


def test_transport_refuses_a_redirect_to_another_host() -> None:
    def _redirect(request: urllib.request.Request, _timeout: float) -> tuple[str, bytes]:
        return "https://mirror.example.com/company_tickers_exchange.json", b"payload"

    with pytest.raises(IdentitySourceError, match="redirected to an unexpected host"):
        fetch_bytes(
            SEC_COMPANY_TICKERS_EXCHANGE_URL, sec_user_agent=_SEC_CONTACT, url_open=_redirect
        )


def test_transport_refuses_a_downgrade_to_plain_http() -> None:
    def _downgrade(request: urllib.request.Request, _timeout: float) -> tuple[str, bytes]:
        return request.full_url.replace("https://", "http://"), b"payload"

    with pytest.raises(IdentitySourceError, match="redirected to an unexpected host"):
        fetch_bytes(NASDAQ_TRADED_URL, sec_user_agent=_SEC_CONTACT, url_open=_downgrade)


def test_transport_retries_rate_limits_then_succeeds() -> None:
    attempts: list[float] = []
    slept: list[float] = []

    def _flaky(request: urllib.request.Request, timeout: float) -> tuple[str, bytes]:
        attempts.append(timeout)
        if len(attempts) < 3:
            raise _http_error(request.full_url, 429, "Too Many Requests")
        return request.full_url, b"payload"

    body = fetch_bytes(
        SEC_COMPANY_TICKERS_MF_URL,
        sec_user_agent=_SEC_CONTACT,
        url_open=_flaky,
        sleep=slept.append,
    )

    assert body == b"payload"
    assert len(attempts) == 3
    assert slept == [5.0, 10.0]


@pytest.mark.parametrize(
    ("failure", "expected", "calls"),
    [
        pytest.param(
            lambda: _http_error("https://www.sec.gov/x", 403, "Forbidden"),
            "request failed with HTTP 403",
            1,
            id="forbidden",
        ),
        pytest.param(
            lambda: _http_error("https://www.sec.gov/x", 429, "Too Many Requests"),
            "request failed with HTTP 429",
            3,
            id="rate-limited",
        ),
        pytest.param(lambda: TimeoutError("timed out"), "could not be retrieved", 3, id="timeout"),
        pytest.param(
            lambda: urllib.error.URLError("unreachable"),
            "could not be retrieved",
            3,
            id="unreachable",
        ),
    ],
)
def test_transport_failures_become_typed_source_errors(
    failure: Callable[[], Exception], expected: str, calls: int
) -> None:
    attempts: list[int] = []

    def _failing(_request: urllib.request.Request, _timeout: float) -> tuple[str, bytes]:
        attempts.append(1)
        raise failure()

    with pytest.raises(IdentitySourceError, match=expected):
        fetch_bytes(
            SEC_COMPANY_TICKERS_EXCHANGE_URL,
            sec_user_agent=_SEC_CONTACT,
            url_open=_failing,
            sleep=lambda _seconds: None,
        )
    assert len(attempts) == calls


def test_opener_failures_become_audit_failures(tmp_path: Path) -> None:
    def _broken(url: str) -> bytes:
        raise OSError("connection reset")

    bundle, audit, _opener = _load(tmp_path, opener=_broken)

    assert bundle is None
    assert audit.failure == NASDAQ_UNAVAILABLE


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


def test_identity_inputs_and_bundles_never_render_private_values(tmp_path: Path) -> None:
    bundle, audit, _opener = _load(tmp_path)

    assert repr(_INPUTS[0]) == "ObservedIdentityInput(<redacted>)"
    assert str(_INPUTS) == "(ObservedIdentityInput(<redacted>), ObservedIdentityInput(<redacted>))"
    assert repr(bundle) == (
        "IdentitySourceBundle(nasdaq=2, sec_tickers=1, sec_funds=1, sec_submissions=2, sources=5)"
    )
    for value in _PRIVATE_VALUES:
        assert value not in repr(bundle)
        assert value not in repr(audit)


@pytest.mark.parametrize(
    ("responses", "fetch", "contact"),
    [
        pytest.param(None, False, _SEC_CONTACT, id="cold-cache"),
        pytest.param(None, True, None, id="missing-contact"),
        pytest.param({NASDAQ_TRADED_URL: b"garbage"}, True, _SEC_CONTACT, id="nasdaq-drift"),
        pytest.param({SEC_COMPANY_TICKERS_EXCHANGE_URL: b"[]"}, True, _SEC_CONTACT, id="sec-drift"),
        pytest.param(
            {SEC_SUBMISSION_URL_TEMPLATE.format(cik=9991029): b"nope"},
            True,
            _SEC_CONTACT,
            id="submission-drift",
        ),
    ],
)
def test_failure_strings_never_leak_private_values(
    tmp_path: Path,
    responses: Mapping[str, bytes] | None,
    fetch: bool,
    contact: str | None,
) -> None:
    bundle, audit, _opener = _load(
        tmp_path, responses=responses, fetch=fetch, sec_user_agent=contact
    )

    assert bundle is None
    assert audit.failure is not None
    for value in _PRIVATE_VALUES:
        assert value not in audit.failure
        assert value not in repr(audit)
