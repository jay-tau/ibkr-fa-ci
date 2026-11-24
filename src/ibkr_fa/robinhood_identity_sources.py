"""Official identity reference sources for Robinhood securities (#492).

Robinhood statements bind ``ticker -> CUSIP``; they never state the
issuer facts Schedule FA needs. This module owns the *evidence* half of
that gap: it retrieves, strictly validates, hashes and caches Nasdaq's directory,
two SEC reference files and the required SEC filer submission records.

Design constraints that the code below encodes literally:

* **Primary sources only.** Nasdaq Trader's traded-symbol directory, SEC
  ticker/fund reference files and per-CIK SEC submissions JSON. No aggregator
  (OpenFIGI, GLEIF, Yahoo, Massive) is identity authority, so none is contacted.
  GLEIF legal-address evidence is isolated in
  :mod:`ibkr_fa.robinhood_legal_entity_sources` and never determines identity.
* **Fingerprinted, immutable cache.** Source artifacts are keyed by
  :func:`identity_input_fingerprint` — the sorted, normalized
  ``(CUSIP, symbol, description)`` identity evidence plus the relevant
  manual ``[[robinhood.securities]]`` filing fields plus the resolver
  schema version. Unrelated cash/statement churn cannot invalidate a
  snapshot, and any identity-relevant change selects a *new* snapshot
  directory. A prior artifact is never rewritten. A filing may reuse a
  verified schema-v2 snapshot across fingerprints only when it was fetched
  after the target March 31 and covers each security that consumes it.
* **Verify before trust.** One JSON artifact per fingerprint carries the
  normalized records, the source URLs, the retrieval time and the
  SHA-256 of every raw source. Its own SHA-256 covers the entire
  payload except that hash field, and it is recomputed on *every* read.
  A missing/short/tampered hash is a hard failure, never a silent
  refetch (a refetch would mask evidence tampering).
* **Fail closed, never crash.** :func:`load_identity_sources` returns
  ``(None, audit)`` with a typed failure string for every transport or
  schema problem, so the identity layer can raise a per-security blocker
  without aborting the independent options leg.
* **No private values in failure strings.** Holdings are PII. Failure
  messages name only the public source, never a symbol, CUSIP,
  description or the declared SEC contact. Refusals replacing caught
  input-bearing errors keep none of that error, not even as a suppressed
  context: it can hold a CIK-bearing URL, a transport reason, response or cache bytes, a JSON
  document, a flag, a timestamp or a path through a fingerprint folder,
  whose name is derived from the holdings. These refusals leave through
  :func:`~ibkr_fa.exceptions.raise_replacing`, so their only link is the
  content-free stand-in kept when the replaced error was or chained a crash.
* **SEC fair access.** https://www.sec.gov/os/webmaster-faq#developers
  requires a declared User-Agent carrying a contact address on every
  request. That is identification, not authentication: no account and no
  API key exists. A cold fetch without that setting fails *before* any
  request is made; a warm cache never needs it.

Stdlib only (``urllib``/``json``/``hashlib``): identity
evidence must not add a runtime dependency to a tax tool.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final, NoReturn, cast

from ibkr_fa.evidence_cache import (
    HASH_KEY,
    SHA256_PATTERN,
    UrlOpen,
    canonical_sha256,
    collapsed,
    fetch_with_retries,
    publish_once,
)
from ibkr_fa.exceptions import IbkrFaError, errno_name, raise_replacing
from ibkr_fa.models.reference_identity import (
    SEC_SUBMISSION_URL_TEMPLATE,
    SET_SEC_CONTACT,
    IdentitySourceBundle,
    NasdaqFlag,
    NasdaqTradedRecord,
    ObservedIdentityInput,
    SecFundRecord,
    SecSubmissionRecord,
    SecTickerRecord,
)
from ibkr_fa.path_checks import may_exist

__all__ = [
    "CACHE_SUBDIRECTORY",
    "NASDAQ_TRADED_URL",
    "SCHEMA_VERSION",
    "SEC_COMPANY_TICKERS_EXCHANGE_URL",
    "SEC_COMPANY_TICKERS_MF_URL",
    "SET_IDENTITY_TOO",
    "IdentitySourceAudit",
    "IdentitySourceError",
    "IdentitySourceOpener",
    "identity_input_fingerprint",
    "load_identity_sources",
    "load_post_fy_identity_sources",
    "move_aside_remedy",
    "sec_move_aside_remedy",
    "sec_source_opener",
]

NASDAQ_TRADED_URL: Final[str] = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"
SEC_COMPANY_TICKERS_EXCHANGE_URL: Final[str] = (
    "https://www.sec.gov/files/company_tickers_exchange.json"
)
SEC_COMPANY_TICKERS_MF_URL: Final[str] = "https://www.sec.gov/files/company_tickers_mf.json"

SCHEMA_VERSION: Final[int] = 2
_LEGACY_SCHEMA_VERSION: Final[int] = 1
CACHE_SUBDIRECTORY: Final[str] = "robinhood-identities"

_ARTIFACT_NAME: Final[str] = "sources.json"
_FINGERPRINT_DOMAIN: Final[str] = "ibkr-fa/robinhood-identity-inputs"

_NASDAQ_HEADER: Final[str] = (
    "Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|"
    "Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares"
)
_NASDAQ_TRAILER_PREFIX: Final[str] = "File Creation Time:"
_NASDAQ_FIELD_COUNT: Final[int] = 12
# A usable directory is at least a header plus the creation-time trailer.
_NASDAQ_MIN_LINES: Final[int] = 2

_SEC_TICKER_FIELDS: Final[tuple[str, ...]] = ("cik", "name", "ticker", "exchange")
_SEC_FUND_FIELDS: Final[tuple[str, ...]] = ("cik", "seriesId", "classId", "symbol")

_NASDAQ_LABEL: Final[str] = "the Nasdaq Trader traded-symbol directory"
_SEC_TICKERS_LABEL: Final[str] = "the SEC ticker/exchange dataset"
_SEC_FUNDS_LABEL: Final[str] = "the SEC fund class dataset"
_SEC_SUBMISSIONS_LABEL: Final[str] = "the SEC submissions bulk archive"
_SOURCE_LABELS: Final[Mapping[str, str]] = {
    NASDAQ_TRADED_URL: _NASDAQ_LABEL,
    SEC_COMPANY_TICKERS_EXCHANGE_URL: _SEC_TICKERS_LABEL,
    SEC_COMPANY_TICKERS_MF_URL: _SEC_FUNDS_LABEL,
}

_ARTIFACT_INVALID: Final[str] = "the identity source cache artifact contains an invalid record"
_ARTIFACT_UNREADABLE: Final[str] = "the identity source cache artifact is unreadable"
_ARTIFACT_TOO_DEEP: Final[str] = "the identity source cache artifact is nested too deeply"
_ARTIFACT_UNSUPPORTED_SCHEMA: Final[str] = (
    "the identity source cache artifact has an unsupported schema"
)
# Every remedy that skips a lookup through an [[fa.entities]] entry ends with
# this (#1458, #1465, #1521). An entry may leave out address (#1449), name and
# nature_of_entity, but with the lookup skipped Schedule FA has only the
# entry's cells: one without address stops it, one without nature_of_entity
# leaves Nature of entity blank, and one without name files the broker's
# description, a manual task for a held ETF (#793).
SET_IDENTITY_TOO: Final[str] = (
    "Set name, address (and zip, if the issuer has one) and nature_of_entity too in any such "
    "entry that leaves one out."
)
_SNAPSHOT_SKIP: Final[str] = (
    "review the legal entities yourself and, to skip the lookup, set prefer_configured_address, "
    "prefer_configured_name and prefer_configured_nature in the [[fa.entities]] entry of each "
    "held US common stock that GLEIF doesn't resolve, and add an [[fa.entities]] entry for each "
    f"held ETF. {SET_IDENTITY_TOO}"
)


_COLD_CACHE_FAILURE: Final[str] = (
    "identity reference sources are not cached for these inputs and fetching is disabled"
)
_MISSING_CONTACT_FAILURE: Final[str] = (
    "a declared SEC fair-access contact (User-Agent) is required to fetch identity sources; "
    f"{SET_SEC_CONTACT}"
)

_SEC_HOSTS: Final[frozenset[str]] = frozenset({"data.sec.gov", "www.sec.gov"})
_PROJECT_USER_AGENT: Final[str] = "ibkr-fa Schedule-FA identity reference fetch"
_RETRY_BACKOFF_SECONDS: Final[float] = 5.0
_MAX_USER_AGENT_LENGTH: Final[int] = 200
_MAX_CIK: Final[int] = 9_999_999_999
_MAX_SUBMISSION_BYTES: Final[int] = 128 * 1024 * 1024

# Injectable transport: URL -> raw response bytes. Implementations raise on
# any retrieval failure; ``load_identity_sources`` converts that into an
# audit failure. Tests inject a stub, production uses the urllib transport
# built by ``_default_opener``.
type IdentitySourceOpener = Callable[[str], bytes]


class IdentitySourceError(IbkrFaError):
    """An official identity reference source is unavailable or invalid.

    Raised (and caught) inside this module only: every public entry point
    converts it into :attr:`IdentitySourceAudit.failure` so a transport or
    schema problem degrades into a typed identity blocker instead of
    aborting the run.
    """


class SecEntityCacheError(IbkrFaError):
    """An SEC cache that A3 entity evidence relies on is unusable.

    Raised by the ADR depositary and ETF registrant loaders for a held ADR's
    Form F-6 cache or ETF's N-CEN selection cache, and by the post-FY identity
    snapshot search for any entry it can't use. The message names the cache
    folder and a fixed phrase for the failure, never a cached value, an ISIN
    or the name of an entry in that folder.
    """


@dataclass(frozen=True, slots=True)
class IdentitySourceAudit:
    """Provenance for one :func:`load_identity_sources` call.

    ``failure`` is ``None`` on success and otherwise a user-facing string
    that names only public sources — never a holding, CUSIP, description
    or the declared SEC contact. ``artifact_path`` points at the
    fingerprinted artifact whenever one exists on disk (including the
    invalid artifact a failure refers to, so the user can inspect it).
    """

    fingerprint: str
    cache_hit: bool
    failure: str | None
    artifact_path: Path | None


def _label(url: str) -> str:
    if url.startswith("https://data.sec.gov/submissions/CIK"):
        return _SEC_SUBMISSIONS_LABEL
    return _SOURCE_LABELS.get(url, "an identity reference source")


def _upper(value: str) -> str:
    return " ".join(value.split()).upper()


def identity_input_fingerprint(
    inputs: Iterable[ObservedIdentityInput],
    manual_filing_fields: Mapping[str, Mapping[str, str]],
    schema_version: int = SCHEMA_VERSION,
) -> str:
    """Return the SHA-256 fingerprint of one identity-input dataset.

    The digest covers the sorted, de-duplicated, normalized
    ``(CUSIP, symbol, description)`` triples, the relevant manual
    ``[[robinhood.securities]]`` filing fields (callers stringify
    non-string values), and the resolver schema version. Input order,
    case and internal whitespace are normalized away, so a repeat run
    over unchanged identity evidence selects the same snapshot while any
    identity-relevant change selects a new one.
    """
    observed = sorted(
        {(_upper(item.cusip), _upper(item.symbol), _upper(item.description)) for item in inputs}
    )
    manual = {
        _upper(symbol): {
            collapsed(field): collapsed(value) for field, value in sorted(fields.items())
        }
        for symbol, fields in sorted(manual_filing_fields.items())
    }
    return canonical_sha256(
        {
            "domain": _FINGERPRINT_DOMAIN,
            "schema_version": int(schema_version),
            "inputs": [list(entry) for entry in observed],
            "manual_filing_fields": manual,
        }
    )


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


# The low-level seam under the default transport, so hostname pinning, retry
# and HTTP-status mapping stay testable without a network.
def _url_open(request: urllib.request.Request, timeout: float) -> tuple[str, bytes]:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.geturl(), response.read()


def _fetch_bytes(
    url: str,
    *,
    sec_user_agent: str,
    url_open: UrlOpen | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Fetch ``url`` with bounded retries, pinning the host after redirects.

    SEC hosts receive the declared fair-access contact; Nasdaq Trader
    receives a plain project agent. A redirect that lands on any other
    host (or leaves TLS) is refused rather than followed silently: the
    whole point of a primary source is that the bytes came from it.
    """
    expected_host = urllib.parse.urlsplit(url).hostname
    agent = sec_user_agent if expected_host in _SEC_HOSTS else _PROJECT_USER_AGENT
    request = urllib.request.Request(url, headers={"User-Agent": agent, "Accept": "*/*"})

    def refuse_status(exc: urllib.error.HTTPError) -> NoReturn:
        raise_replacing(
            IdentitySourceError(f"{_label(url)} request failed with HTTP {exc.code}"), exc
        )

    def refuse_unreachable(exc: Exception) -> NoReturn:
        raise_replacing(IdentitySourceError(f"{_label(url)} could not be retrieved"), exc)

    final_url, body = fetch_with_retries(
        request,
        transport=url_open if url_open is not None else _url_open,
        backoff_seconds=_RETRY_BACKOFF_SECONDS,
        sleep=sleep,
        refuse_status=refuse_status,
        refuse_unreachable=refuse_unreachable,
    )
    split = urllib.parse.urlsplit(final_url)
    if split.scheme != "https" or split.hostname != expected_host:
        raise IdentitySourceError(f"{_label(url)} redirected to an unexpected host")
    return body


def _default_opener(sec_user_agent: str) -> IdentitySourceOpener:
    last_sec_request = 0.0

    def _open(url: str) -> bytes:
        nonlocal last_sec_request
        if urllib.parse.urlsplit(url).hostname in _SEC_HOSTS:
            delay = 0.12 - (time.monotonic() - last_sec_request)
            if delay > 0:
                time.sleep(delay)
            last_sec_request = time.monotonic()
        return _fetch_bytes(url, sec_user_agent=sec_user_agent)

    return _open


def sec_source_opener(sec_user_agent: str | None) -> IdentitySourceOpener:
    """Build the repository's fair-access, host-pinned SEC transport."""

    return _default_opener(_validated_sec_user_agent(sec_user_agent))


def _usable_sec_contact(value: str | None) -> str | None:
    """The configured SEC fair-access contact, stripped, or ``None`` when it
    is missing or would be refused."""
    candidate = (value or "").strip()
    if (
        not candidate
        or "@" not in candidate
        or not candidate.isprintable()
        or len(candidate) > _MAX_USER_AGENT_LENGTH
    ):
        return None
    return candidate


def _validated_sec_user_agent(value: str | None) -> str:
    contact = _usable_sec_contact(value)
    if contact is None:
        raise IdentitySourceError(_MISSING_CONTACT_FAILURE)
    return contact


def _read_source(opener: IdentitySourceOpener, url: str) -> bytes:
    try:
        raw = opener(url)
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:
        raise_replacing(IdentitySourceError(f"{_label(url)} could not be retrieved"), exc)
    if not raw:
        raise IdentitySourceError(f"{_label(url)} returned an empty response")
    if url.startswith("https://data.sec.gov/submissions/CIK") and len(raw) > _MAX_SUBMISSION_BYTES:
        raise IdentitySourceError(f"{_SEC_SUBMISSIONS_LABEL} filer record is implausibly large")
    return raw


# ---------------------------------------------------------------------------
# Source parsing
# ---------------------------------------------------------------------------


def _decode(raw: bytes, *, source: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise_replacing(IdentitySourceError(f"{source} is not valid UTF-8"), exc)


def _nasdaq_flag(value: str, *, allow_unavailable: bool = False) -> NasdaqFlag:
    text = value.strip().upper()
    if text == NasdaqFlag.UNAVAILABLE and not allow_unavailable:
        raise IdentitySourceError(f"{_NASDAQ_LABEL} contains a blank required flag")
    try:
        return NasdaqFlag(text)
    except ValueError as exc:
        raise_replacing(
            IdentitySourceError(f"{_NASDAQ_LABEL} contains an unexpected flag value"), exc
        )


def _nasdaq_creation_marker(line: str) -> str:
    marker = collapsed(line.split("|", maxsplit=1)[0].removeprefix(_NASDAQ_TRAILER_PREFIX))
    if not marker:
        raise IdentitySourceError(f"{_NASDAQ_LABEL} has an empty File Creation Time trailer")
    return marker


def _parse_nasdaq_traded(raw: bytes) -> tuple[dict[str, NasdaqTradedRecord], str]:
    """Parse ``nasdaqtraded.txt`` into records plus its creation marker.

    Enforces the exact pinned header, the mandatory ``File Creation
    Time`` trailer (its absence means a truncated download, which would
    otherwise look like a valid but short directory) and symbol
    uniqueness across the whole file.
    """
    lines = [line for line in _decode(raw, source=_NASDAQ_LABEL).splitlines() if line.strip()]
    if len(lines) < _NASDAQ_MIN_LINES:
        raise IdentitySourceError(f"{_NASDAQ_LABEL} is empty")
    if lines[0] != _NASDAQ_HEADER:
        raise IdentitySourceError(f"{_NASDAQ_LABEL} header does not match the pinned schema")
    if not lines[-1].startswith(_NASDAQ_TRAILER_PREFIX):
        raise IdentitySourceError(f"{_NASDAQ_LABEL} is missing its File Creation Time trailer")
    marker = _nasdaq_creation_marker(lines[-1])

    records: dict[str, NasdaqTradedRecord] = {}
    for line in lines[1:-1]:
        if line.startswith(_NASDAQ_TRAILER_PREFIX):
            raise IdentitySourceError(f"{_NASDAQ_LABEL} contains a misplaced trailer row")
        fields = line.split("|")
        if len(fields) != _NASDAQ_FIELD_COUNT:
            raise IdentitySourceError(f"{_NASDAQ_LABEL} contains a row with unexpected columns")
        (
            traded,
            symbol_raw,
            name_raw,
            exchange_raw,
            _category,
            etf_raw,
            _lot_size,
            test_raw,
            _financial_status,
            _cqs_symbol,
            _nasdaq_symbol,
            next_shares_raw,
        ) = fields
        _nasdaq_flag(traded)
        symbol = _upper(symbol_raw)
        name = collapsed(name_raw)
        exchange = _upper(exchange_raw)
        if not symbol or not name or not exchange:
            raise IdentitySourceError(f"{_NASDAQ_LABEL} contains a row with missing identity")
        if symbol in records:
            raise IdentitySourceError(f"{_NASDAQ_LABEL} contains a duplicate symbol")
        records[symbol] = NasdaqTradedRecord(
            symbol=symbol,
            name=name,
            listing_exchange=exchange,
            etf=_nasdaq_flag(etf_raw, allow_unavailable=True),
            test_issue=_nasdaq_flag(test_raw),
            next_shares=_nasdaq_flag(next_shares_raw, allow_unavailable=True),
        )
    if not records:
        raise IdentitySourceError(f"{_NASDAQ_LABEL} contains no data rows")
    return records, marker


def _json_object(raw: bytes, *, source: str) -> Mapping[str, object]:
    try:
        payload = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise_replacing(IdentitySourceError(f"{source} is not valid JSON"), exc)
    if not isinstance(payload, dict):
        raise IdentitySourceError(f"{source} is not a JSON object")
    return payload


def _sec_rows(
    payload: Mapping[str, object],
    *,
    expected_fields: Sequence[str],
    source: str,
) -> list[list[object]]:
    if payload.get("fields") != list(expected_fields):
        raise IdentitySourceError(f"{source} field list does not match the pinned schema")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise IdentitySourceError(f"{source} contains no data rows")
    rows: list[list[object]] = []
    for entry in data:
        if not isinstance(entry, list) or len(entry) != len(expected_fields):
            raise IdentitySourceError(f"{source} contains a row that does not match the schema")
        rows.append(cast("list[object]", entry))
    return rows


def _source_cik(value: object, *, source: str) -> int:
    if isinstance(value, bool):
        raise IdentitySourceError(f"{source} contains a malformed CIK")
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, str) and value.strip().isdigit():
        candidate = int(value.strip())
    else:
        raise IdentitySourceError(f"{source} contains a malformed CIK")
    if not 0 < candidate <= _MAX_CIK:
        raise IdentitySourceError(f"{source} contains a malformed CIK")
    return candidate


def _source_text(value: object, *, source: str) -> str:
    if not isinstance(value, str):
        raise IdentitySourceError(f"{source} contains a malformed text field")
    return value


def _parse_sec_tickers(
    raw: bytes,
    *,
    wanted: frozenset[str],
) -> dict[str, tuple[SecTickerRecord, ...]]:
    payload = _json_object(raw, source=_SEC_TICKERS_LABEL)
    grouped: dict[str, set[SecTickerRecord]] = {}
    for row in _sec_rows(payload, expected_fields=_SEC_TICKER_FIELDS, source=_SEC_TICKERS_LABEL):
        cik = _source_cik(row[0], source=_SEC_TICKERS_LABEL)
        name = collapsed(_source_text(row[1], source=_SEC_TICKERS_LABEL))
        symbol = _upper(_source_text(row[2], source=_SEC_TICKERS_LABEL))
        exchange_raw = row[3]
        exchange = (
            ""
            if exchange_raw is None
            else collapsed(_source_text(exchange_raw, source=_SEC_TICKERS_LABEL))
        )
        if not name or not symbol:
            raise IdentitySourceError(f"{_SEC_TICKERS_LABEL} contains a row with missing identity")
        if symbol not in wanted:
            continue
        grouped.setdefault(symbol, set()).add(
            SecTickerRecord(cik=cik, name=name, symbol=symbol, exchange=exchange)
        )
    return {
        symbol: tuple(
            sorted(records, key=lambda record: (record.cik, record.exchange, record.name))
        )
        for symbol, records in sorted(grouped.items())
    }


def _parse_sec_funds(
    raw: bytes,
    *,
    wanted: frozenset[str],
) -> dict[str, tuple[SecFundRecord, ...]]:
    payload = _json_object(raw, source=_SEC_FUNDS_LABEL)
    grouped: dict[str, set[SecFundRecord]] = {}
    for row in _sec_rows(payload, expected_fields=_SEC_FUND_FIELDS, source=_SEC_FUNDS_LABEL):
        cik = _source_cik(row[0], source=_SEC_FUNDS_LABEL)
        series_id = _upper(_source_text(row[1], source=_SEC_FUNDS_LABEL))
        class_id = _upper(_source_text(row[2], source=_SEC_FUNDS_LABEL))
        symbol = _upper(_source_text(row[3], source=_SEC_FUNDS_LABEL))
        # Most class rows carry no ticker at all; they can never match an
        # observed symbol, so they are skipped rather than treated as drift.
        if not symbol or symbol not in wanted:
            continue
        if not series_id or not class_id:
            raise IdentitySourceError(f"{_SEC_FUNDS_LABEL} contains a row with missing identity")
        grouped.setdefault(symbol, set()).add(
            SecFundRecord(cik=cik, series_id=series_id, class_id=class_id, symbol=symbol)
        )
    return {
        symbol: tuple(
            sorted(records, key=lambda record: (record.cik, record.series_id, record.class_id))
        )
        for symbol, records in sorted(grouped.items())
    }


def _submission_forms(filings: object) -> tuple[str, ...]:
    """Collect every ``form`` value present in one submissions JSON.

    The per-filer entry carries the current filings under
    ``filings.recent`` and may carry further blocks alongside it; the
    ``filings.files`` index (which has no ``form`` array) is ignored.
    """
    if not isinstance(filings, dict):
        raise IdentitySourceError(f"{_SEC_SUBMISSIONS_LABEL} filer record has no filings")
    blocks_by_key = cast("Mapping[str, object]", filings)
    recent = blocks_by_key.get("recent")
    if not isinstance(recent, dict) or not isinstance(
        cast("Mapping[str, object]", recent).get("form"), list
    ):
        raise IdentitySourceError(f"{_SEC_SUBMISSIONS_LABEL} filer record has no current filings")
    forms: set[str] = set()
    for _key, value in sorted(blocks_by_key.items()):
        blocks: list[object] = cast("list[object]", value) if isinstance(value, list) else [value]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            raw_forms = cast("Mapping[str, object]", block).get("form")
            if raw_forms is None:
                continue
            if not isinstance(raw_forms, list):
                raise IdentitySourceError(
                    f"{_SEC_SUBMISSIONS_LABEL} filer record has a malformed form list"
                )
            for entry in raw_forms:
                if not isinstance(entry, str):
                    raise IdentitySourceError(
                        f"{_SEC_SUBMISSIONS_LABEL} filer record has a malformed form value"
                    )
                text = _upper(entry)
                if text:
                    forms.add(text)
    return tuple(sorted(forms))


def _parse_submission(raw: bytes, *, cik: int) -> SecSubmissionRecord:
    payload = _json_object(raw, source=_SEC_SUBMISSIONS_LABEL)
    if "name" not in payload or "stateOfIncorporation" not in payload or "filings" not in payload:
        raise IdentitySourceError(f"{_SEC_SUBMISSIONS_LABEL} filer record is missing fields")
    if _source_cik(payload.get("cik"), source=_SEC_SUBMISSIONS_LABEL) != cik:
        raise IdentitySourceError(f"{_SEC_SUBMISSIONS_LABEL} filer record is for another filer")
    name = collapsed(_source_text(payload["name"], source=_SEC_SUBMISSIONS_LABEL))
    if not name:
        raise IdentitySourceError(f"{_SEC_SUBMISSIONS_LABEL} filer record has no name")
    state_raw = payload["stateOfIncorporation"]
    state = (
        "" if state_raw is None else _upper(_source_text(state_raw, source=_SEC_SUBMISSIONS_LABEL))
    )
    addresses = payload.get("addresses")
    business_raw = (
        cast("Mapping[str, object]", addresses).get("business")
        if isinstance(addresses, dict)
        else None
    )
    business = cast("Mapping[str, object]", business_raw) if isinstance(business_raw, dict) else {}

    def optional_text(key: str, *, upper: bool = False) -> str:
        value = business.get(key)
        if not isinstance(value, str):
            return ""
        text = collapsed(value)
        return text.upper() if upper else text

    return SecSubmissionRecord(
        cik=cik,
        name=name,
        state_of_incorporation=state,
        forms=_submission_forms(payload["filings"]),
        business_street1=optional_text("street1"),
        business_street2=optional_text("street2"),
        business_city=optional_text("city"),
        business_state_or_country=optional_text("stateOrCountry", upper=True),
        business_zip_code=optional_text("zipCode"),
    )


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------


def _artifact_path(cache_dir: Path, fingerprint: str) -> Path:
    return cache_dir / CACHE_SUBDIRECTORY / fingerprint / _ARTIFACT_NAME


def _artifact_payload(
    *,
    fingerprint: str,
    bundle: IdentitySourceBundle,
    sources: Sequence[Mapping[str, object]],
    fetched_at: datetime,
) -> dict[str, object]:
    return {
        "schema": SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "fetched_at": fetched_at.isoformat(),
        "sources": [dict(entry) for entry in sources],
        "nasdaq": [
            {
                "symbol": record.symbol,
                "name": record.name,
                "listing_exchange": record.listing_exchange,
                "etf": str(record.etf),
                "test_issue": str(record.test_issue),
                "next_shares": str(record.next_shares),
            }
            for _symbol, record in sorted(bundle.nasdaq_by_symbol.items())
        ],
        "sec_tickers": [
            {
                "cik": record.cik,
                "name": record.name,
                "symbol": record.symbol,
                "exchange": record.exchange,
            }
            for _symbol, group in sorted(bundle.sec_tickers_by_symbol.items())
            for record in group
        ],
        "sec_funds": [
            {
                "cik": record.cik,
                "series_id": record.series_id,
                "class_id": record.class_id,
                "symbol": record.symbol,
            }
            for _symbol, group in sorted(bundle.sec_funds_by_symbol.items())
            for record in group
        ],
        "sec_submissions": [
            {
                "cik": record.cik,
                "name": record.name,
                "state_of_incorporation": record.state_of_incorporation,
                "forms": list(record.forms),
                "business_address": {
                    "street1": record.business_street1,
                    "street2": record.business_street2,
                    "city": record.business_city,
                    "state_or_country": record.business_state_or_country,
                    "zip_code": record.business_zip_code,
                },
            }
            for _cik, record in sorted(bundle.sec_submissions_by_cik.items())
        ],
    }


def _write_artifact(path: Path, payload: Mapping[str, object]) -> None:
    """Write the artifact exactly once, never over an existing file."""
    publish_once(path, payload, refuse=_refuse_publication)


def _refuse_publication(exc: OSError) -> NoReturn:
    problem = "already exists" if isinstance(exc, FileExistsError) else "could not be written"
    raise_replacing(IdentitySourceError(f"the identity source cache artifact {problem}"), exc)


def _rows(artifact: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    value = artifact.get(key)
    if not isinstance(value, list):
        raise IdentitySourceError(_ARTIFACT_INVALID)
    rows: list[Mapping[str, object]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise IdentitySourceError(_ARTIFACT_INVALID)
        rows.append(cast("Mapping[str, object]", entry))
    return rows


def _text_field(row: Mapping[str, object], key: str, *, allow_empty: bool = False) -> str:
    value = row.get(key)
    if not isinstance(value, str) or (not value and not allow_empty):
        raise IdentitySourceError(_ARTIFACT_INVALID)
    return value


def _flag_field(
    row: Mapping[str, object],
    key: str,
    *,
    allow_unavailable: bool = False,
) -> NasdaqFlag:
    value = row.get(key)
    if not isinstance(value, str) or (value == NasdaqFlag.UNAVAILABLE and not allow_unavailable):
        raise IdentitySourceError(_ARTIFACT_INVALID)
    try:
        return NasdaqFlag(value)
    except ValueError as exc:
        raise_replacing(IdentitySourceError(_ARTIFACT_INVALID), exc)


def _int_field(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= _MAX_CIK:
        raise IdentitySourceError(_ARTIFACT_INVALID)
    return value


def _forms_field(row: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = row.get(key)
    if not isinstance(value, list):
        raise IdentitySourceError(_ARTIFACT_INVALID)
    forms: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry:
            raise IdentitySourceError(_ARTIFACT_INVALID)
        forms.append(entry)
    if forms != sorted(set(forms)):
        raise IdentitySourceError(_ARTIFACT_INVALID)
    return tuple(forms)


def _bundle_from_artifact(
    artifact: Mapping[str, object],
    *,
    schema_version: int = SCHEMA_VERSION,
) -> IdentitySourceBundle:
    nasdaq: dict[str, NasdaqTradedRecord] = {}
    for row in _rows(artifact, "nasdaq"):
        record = NasdaqTradedRecord(
            symbol=_text_field(row, "symbol"),
            name=_text_field(row, "name"),
            listing_exchange=_text_field(row, "listing_exchange"),
            etf=_flag_field(row, "etf", allow_unavailable=True),
            test_issue=_flag_field(row, "test_issue"),
            next_shares=_flag_field(row, "next_shares", allow_unavailable=True),
        )
        if record.symbol in nasdaq:
            raise IdentitySourceError(_ARTIFACT_INVALID)
        nasdaq[record.symbol] = record

    tickers: dict[str, list[SecTickerRecord]] = {}
    for row in _rows(artifact, "sec_tickers"):
        ticker = SecTickerRecord(
            cik=_int_field(row, "cik"),
            name=_text_field(row, "name"),
            symbol=_text_field(row, "symbol"),
            exchange=_text_field(row, "exchange", allow_empty=True),
        )
        tickers.setdefault(ticker.symbol, []).append(ticker)

    funds: dict[str, list[SecFundRecord]] = {}
    for row in _rows(artifact, "sec_funds"):
        fund = SecFundRecord(
            cik=_int_field(row, "cik"),
            series_id=_text_field(row, "series_id"),
            class_id=_text_field(row, "class_id"),
            symbol=_text_field(row, "symbol"),
        )
        funds.setdefault(fund.symbol, []).append(fund)

    submissions: dict[int, SecSubmissionRecord] = {}
    for row in _rows(artifact, "sec_submissions"):
        has_address_schema = schema_version != _LEGACY_SCHEMA_VERSION
        raw_address = {} if not has_address_schema else row.get("business_address")
        if not isinstance(raw_address, dict):
            raise IdentitySourceError(_ARTIFACT_INVALID)
        address = cast("Mapping[str, object]", raw_address)
        submission = SecSubmissionRecord(
            cik=_int_field(row, "cik"),
            name=_text_field(row, "name"),
            state_of_incorporation=_text_field(row, "state_of_incorporation", allow_empty=True),
            forms=_forms_field(row, "forms"),
            business_street1=_text_field(address, "street1", allow_empty=True)
            if has_address_schema
            else "",
            business_street2=_text_field(address, "street2", allow_empty=True)
            if has_address_schema
            else "",
            business_city=_text_field(address, "city", allow_empty=True)
            if has_address_schema
            else "",
            business_state_or_country=(
                _text_field(address, "state_or_country", allow_empty=True)
                if has_address_schema
                else ""
            ),
            business_zip_code=_text_field(address, "zip_code", allow_empty=True)
            if has_address_schema
            else "",
        )
        if submission.cik in submissions:
            raise IdentitySourceError(_ARTIFACT_INVALID)
        submissions[submission.cik] = submission

    source_sha256: dict[str, str] = {}
    for row in _rows(artifact, "sources"):
        url = _text_field(row, "url")
        digest = _text_field(row, "sha256")
        if url in source_sha256 or not SHA256_PATTERN.match(digest):
            raise IdentitySourceError(_ARTIFACT_INVALID)
        source_sha256[url] = digest
    if not source_sha256:
        raise IdentitySourceError(_ARTIFACT_INVALID)

    return IdentitySourceBundle(
        nasdaq_by_symbol=nasdaq,
        sec_tickers_by_symbol={symbol: tuple(group) for symbol, group in tickers.items()},
        sec_funds_by_symbol={symbol: tuple(group) for symbol, group in funds.items()},
        sec_submissions_by_cik=submissions,
        source_sha256=source_sha256,
    )


def _sealed_artifact(path: Path) -> Mapping[str, object]:
    """Read one artifact and verify its payload hash.

    A ``MemoryError`` is left to the caller, which owns the whole read.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise_replacing(
            IdentitySourceError(
                f"the identity source cache artifact could not be read ({errno_name(exc)})"
            ),
            exc,
        )
    except UnicodeError as exc:
        raise_replacing(IdentitySourceError(_ARTIFACT_UNREADABLE), exc)
    try:
        artifact = json.loads(text)
    except RecursionError as exc:
        raise IdentitySourceError(_ARTIFACT_TOO_DEEP) from exc
    except ValueError as exc:
        raise_replacing(IdentitySourceError("the identity source cache artifact is malformed"), exc)
    if not isinstance(artifact, dict):
        raise IdentitySourceError("the identity source cache artifact is malformed")
    expected = artifact.get(HASH_KEY)
    if not isinstance(expected, str) or not SHA256_PATTERN.match(expected):
        raise IdentitySourceError("the identity source cache artifact has no payload hash")
    payload = {key: value for key, value in artifact.items() if key != HASH_KEY}
    try:
        actual = canonical_sha256(payload)
    except RecursionError as exc:
        raise_replacing(IdentitySourceError(_ARTIFACT_TOO_DEEP), exc)
    except ValueError as exc:
        raise_replacing(
            IdentitySourceError("the identity source cache artifact contains a non-finite number"),
            exc,
        )
    if actual != expected:
        raise IdentitySourceError("the identity source cache artifact failed its integrity check")
    return cast("Mapping[str, object]", artifact)


def _read_artifact(
    path: Path,
    *,
    fingerprint: str,
    schema_version: int = SCHEMA_VERSION,
) -> IdentitySourceBundle:
    """Return the cached bundle, verifying integrity unconditionally."""
    return _checked_bundle(
        _sealed_artifact(path),
        fingerprint=fingerprint,
        schema_version=schema_version,
    )


def _checked_bundle(
    artifact: Mapping[str, object],
    *,
    fingerprint: str,
    schema_version: int,
) -> IdentitySourceBundle:
    if artifact.get("schema") != schema_version:
        raise IdentitySourceError(_ARTIFACT_UNSUPPORTED_SCHEMA)
    if artifact.get("fingerprint") != fingerprint:
        raise IdentitySourceError(
            "the identity source cache artifact does not match the requested identity inputs"
        )
    if not isinstance(artifact.get("fetched_at"), str):
        raise IdentitySourceError(_ARTIFACT_INVALID)
    return _bundle_from_artifact(artifact, schema_version=schema_version)


def _load_cached_sources(
    path: Path,
    *,
    fingerprint: str,
    schema_version: int = SCHEMA_VERSION,
) -> tuple[IdentitySourceBundle | None, IdentitySourceAudit]:
    try:
        bundle = _read_artifact(
            path,
            fingerprint=fingerprint,
            schema_version=schema_version,
        )
    except (IdentitySourceError, MemoryError) as exc:
        failure = str(exc) if isinstance(exc, IdentitySourceError) else _ARTIFACT_UNREADABLE
        return None, IdentitySourceAudit(fingerprint, False, failure, path)
    return bundle, IdentitySourceAudit(fingerprint, True, None, path)


def _snapshot_folder(path: Path) -> list[str] | None:
    """List one folder of the snapshot search, following a link; ``None`` if absent."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise_replacing(
            IdentitySourceError(f"the cache entry could not be inspected ({errno_name(exc)})"),
            exc,
        )
    if stat.S_ISLNK(mode):
        try:
            mode = path.stat().st_mode
        except OSError as exc:
            raise_replacing(
                IdentitySourceError(
                    f"the cache entry is a link that can't be followed ({errno_name(exc)})"
                ),
                exc,
            )
    if not stat.S_ISDIR(mode):
        raise IdentitySourceError("the cache entry is not a folder")
    try:
        return sorted(item.name for item in path.iterdir())
    except OSError as exc:
        raise_replacing(
            IdentitySourceError(f"the cache folder could not be listed ({errno_name(exc)})"),
            exc,
        )
    except MemoryError as exc:
        raise IdentitySourceError("the cache folder could not be listed") from exc


def _read_snapshot(path: Path) -> tuple[int, datetime, IdentitySourceBundle]:
    """Fully check one snapshot under its own schema before anyone may skip it."""
    artifact = _sealed_artifact(path)
    schema = artifact.get("schema")
    if (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or schema not in {_LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}
    ):
        raise IdentitySourceError(_ARTIFACT_UNSUPPORTED_SCHEMA)
    if artifact.get("fingerprint") != path.parent.name:
        raise IdentitySourceError(_ARTIFACT_INVALID)
    bundle = _checked_bundle(artifact, fingerprint=path.parent.name, schema_version=schema)
    try:
        fetched = datetime.fromisoformat(cast("str", artifact["fetched_at"]))
    except ValueError as exc:
        raise_replacing(IdentitySourceError(_ARTIFACT_INVALID), exc)
    if fetched.tzinfo is None:
        raise IdentitySourceError(_ARTIFACT_INVALID)
    return schema, fetched, bundle


def move_aside_remedy(*, fetch: bool, set_sec_contact: bool = False) -> str:
    """How a refusal over an unusable lookup cache says to rebuild it.

    With fetching off, a folder moved aside is only a cache miss: the lookup
    finds nothing, and Schedule FA stops for each cell it would have filled
    (#1521). So the rerun must fetch; the refusal's entry remedy is the
    offline route. ``set_sec_contact`` adds the SEC fair-access contact that
    the rerun's fetch needs (#1526).
    """
    aside = "Move that folder aside"
    if set_sec_contact:
        aside = f"{aside}, {SET_SEC_CONTACT},"
    if fetch:
        return f"{aside} and re-run"
    return (
        f"{aside} and re-run without --no-fetch-prices (with fetching off, the lookup then finds "
        "nothing)"
    )


def sec_move_aside_remedy(*, fetch: bool, sec_user_agent: str | None) -> str:
    """:func:`move_aside_remedy` for a cache rebuilt from SEC, which also names
    the contact when the run's ``sec_user_agent`` is missing or would be
    refused (#1526)."""
    return move_aside_remedy(
        fetch=fetch, set_sec_contact=_usable_sec_contact(sec_user_agent) is None
    )


def _snapshot_refusal(
    root: Path, exc: BaseException, *, fetch: bool, sec_user_agent: str | None
) -> SecEntityCacheError:
    # The cache folder only, never an entry found in it: its name can be
    # anything (#1071).
    problem = str(exc) if isinstance(exc, IdentitySourceError) else _ARTIFACT_UNREADABLE
    remedy = sec_move_aside_remedy(fetch=fetch, sec_user_agent=sec_user_agent)
    return SecEntityCacheError(
        f"SEC identity snapshot cache is unusable ({problem}): {root}. {remedy}, or "
        f"{_SNAPSHOT_SKIP}"
    )


def load_post_fy_identity_sources(
    required_common_symbols: Iterable[str],
    *,
    required_fund_symbols: Iterable[str] = (),
    cache_dir: Path,
    financial_year_end: date,
    fetch: bool,
    sec_user_agent: str | None,
    current_identity_fingerprint: str | None = None,
) -> tuple[IdentitySourceBundle | None, IdentitySourceAudit]:
    """Reuse the best verified post-FY snapshot without all-or-nothing common coverage.

    Every ``sources.json`` in the cache is fully checked before it is skipped
    for its schema or coverage; only a missing cache folder or a fingerprint
    folder without an artifact counts as absent. Nothing is fetched here:
    ``fetch``, whether the run fetches, and ``sec_user_agent``, the run's SEC
    contact, only word the refusal's remedy.

    Raises:
        SecEntityCacheError: an entry in the snapshot search is unusable. It
            names the cache folder and a fixed problem. Fingerprint-folder
            and artifact refusals leave through
            :func:`~ibkr_fa.exceptions.raise_replacing`: no context, and as
            their cause only a content-free stand-in when the replaced error
            was or chained a crash. These chains keep no cached value or path
            through a fingerprint folder.
    """

    required_common = frozenset(_upper(symbol) for symbol in required_common_symbols)
    required_funds = frozenset(_upper(symbol) for symbol in required_fund_symbols)
    request_fingerprint = canonical_sha256(
        {
            "domain": "ibkr-fa/robinhood-post-fy-identity-snapshot",
            "financial_year_end": financial_year_end.isoformat(),
            "required_common_symbols": sorted(required_common),
            "required_fund_symbols": sorted(required_funds),
        }
    )
    candidates: list[tuple[int, int, datetime, str, Path, IdentitySourceBundle]] = []
    pre_cutoff: list[tuple[int, int, datetime, str, Path, IdentitySourceBundle]] = []
    root = cache_dir / CACHE_SUBDIRECTORY
    try:
        fingerprints = _snapshot_folder(root) or []
    except IdentitySourceError as exc:
        raise _snapshot_refusal(root, exc, fetch=fetch, sec_user_agent=sec_user_agent) from exc
    for fingerprint in fingerprints:
        folder = root / fingerprint
        try:
            names = _snapshot_folder(folder) or []
        except IdentitySourceError as exc:
            raise_replacing(
                _snapshot_refusal(root, exc, fetch=fetch, sec_user_agent=sec_user_agent), exc
            )
        if _ARTIFACT_NAME not in names:
            continue
        path = folder / _ARTIFACT_NAME
        try:
            schema, fetched, bundle = _read_snapshot(path)
        except (IdentitySourceError, MemoryError) as exc:
            raise_replacing(
                _snapshot_refusal(root, exc, fetch=fetch, sec_user_agent=sec_user_agent), exc
            )
        common_coverage = len(
            required_common & bundle.nasdaq_by_symbol.keys() & bundle.sec_tickers_by_symbol.keys()
        )
        fund_coverage = len(required_funds & bundle.sec_funds_by_symbol.keys())
        if schema != SCHEMA_VERSION:
            continue
        if (required_common or required_funds) and common_coverage + fund_coverage == 0:
            continue
        target = candidates if fetched.date() > financial_year_end else pre_cutoff
        target.append(
            (
                common_coverage + fund_coverage,
                fund_coverage,
                fetched,
                fingerprint,
                path,
                bundle,
            )
        )
    if not candidates:
        if pre_cutoff:
            _coverage, _fund_coverage, _fetched, fingerprint, path, _bundle = max(
                pre_cutoff,
                key=lambda item: (item[0], item[1], item[2], item[3]),
            )
            if fingerprint == current_identity_fingerprint:
                return None, IdentitySourceAudit(
                    request_fingerprint,
                    False,
                    (
                        "the current identity snapshot predates the target FY end "
                        "and cannot be refreshed in place; use a new cache directory "
                        "for a post-FY fetch"
                    ),
                    path,
                )
        return None, IdentitySourceAudit(
            request_fingerprint,
            False,
            "no verified covering identity snapshot was fetched after the target financial year",
            None,
        )
    _coverage, _fund_coverage, _fetched, fingerprint, path, bundle = max(
        candidates,
        key=lambda item: (item[0], item[1], item[2], item[3]),
    )
    return bundle, IdentitySourceAudit(fingerprint, True, None, path)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _source_entry(url: str, raw: bytes, *, marker: str | None = None) -> dict[str, object]:
    return {
        "url": url,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "marker": marker,
    }


def _fetch_bundle(
    *,
    wanted: frozenset[str],
    opener: IdentitySourceOpener,
) -> tuple[IdentitySourceBundle, list[dict[str, object]]]:
    raw_nasdaq = _read_source(opener, NASDAQ_TRADED_URL)
    all_nasdaq, marker = _parse_nasdaq_traded(raw_nasdaq)
    nasdaq = {symbol: record for symbol, record in all_nasdaq.items() if symbol in wanted}

    raw_tickers = _read_source(opener, SEC_COMPANY_TICKERS_EXCHANGE_URL)
    sec_tickers = _parse_sec_tickers(raw_tickers, wanted=wanted)

    raw_funds = _read_source(opener, SEC_COMPANY_TICKERS_MF_URL)
    sec_funds = _parse_sec_funds(raw_funds, wanted=wanted)

    sources = [
        _source_entry(NASDAQ_TRADED_URL, raw_nasdaq, marker=marker),
        _source_entry(SEC_COMPANY_TICKERS_EXCHANGE_URL, raw_tickers),
        _source_entry(SEC_COMPANY_TICKERS_MF_URL, raw_funds),
    ]

    ciks = sorted(
        {record.cik for group in sec_tickers.values() for record in group}
        | {record.cik for group in sec_funds.values() for record in group}
    )
    submissions: dict[int, SecSubmissionRecord] = {}
    for cik in ciks:
        url = SEC_SUBMISSION_URL_TEMPLATE.format(cik=cik)
        raw_submission = _read_source(opener, url)
        submissions[cik] = _parse_submission(raw_submission, cik=cik)
        sources.append(_source_entry(url, raw_submission))

    bundle = IdentitySourceBundle(
        nasdaq_by_symbol=nasdaq,
        sec_tickers_by_symbol=sec_tickers,
        sec_funds_by_symbol=sec_funds,
        sec_submissions_by_cik=submissions,
        source_sha256={str(entry["url"]): str(entry["sha256"]) for entry in sources},
    )
    return bundle, sources


def load_identity_sources(
    inputs: Iterable[ObservedIdentityInput],
    manual_filing_fields: Mapping[str, Mapping[str, str]],
    *,
    cache_dir: Path,
    fetch: bool,
    sec_user_agent: str | None,
    opener: IdentitySourceOpener | None = None,
    now: datetime | None = None,
) -> tuple[IdentitySourceBundle | None, IdentitySourceAudit]:
    """Return the official source records for one identity-input dataset.

    The snapshot lives in ``cache_dir/robinhood-identities/<fingerprint>/``.
    A verified artifact is reused with **zero** transport calls; a cold
    cache is fetched only when ``fetch`` is true and a declared SEC
    fair-access contact is configured. An existing artifact is never
    overwritten: if it is present but invalid, the call fails so the
    tampering/corruption is visible instead of being papered over by a
    refetch.

    Never raises for a source problem — every transport, schema or cache
    failure comes back as ``(None, audit)`` with
    :attr:`IdentitySourceAudit.failure` set.
    """
    observed = tuple(inputs)
    fingerprint = identity_input_fingerprint(observed, manual_filing_fields)
    path = _artifact_path(cache_dir, fingerprint)

    if may_exist(path):
        return _load_cached_sources(path, fingerprint=fingerprint)

    if not fetch:
        legacy_fingerprint = identity_input_fingerprint(
            observed,
            manual_filing_fields,
            _LEGACY_SCHEMA_VERSION,
        )
        legacy_path = _artifact_path(cache_dir, legacy_fingerprint)
        if may_exist(legacy_path):
            return _load_cached_sources(
                legacy_path,
                fingerprint=legacy_fingerprint,
                schema_version=_LEGACY_SCHEMA_VERSION,
            )
        return None, IdentitySourceAudit(fingerprint, False, _COLD_CACHE_FAILURE, None)

    try:
        contact = _validated_sec_user_agent(sec_user_agent)
        transport = opener if opener is not None else _default_opener(contact)
        wanted = frozenset(_upper(item.symbol) for item in observed if item.symbol.strip())
        bundle, sources = _fetch_bundle(wanted=wanted, opener=transport)
        _write_artifact(
            path,
            _artifact_payload(
                fingerprint=fingerprint,
                bundle=bundle,
                sources=sources,
                fetched_at=now if now is not None else datetime.now(UTC),
            ),
        )
    except (IdentitySourceError, MemoryError, OSError) as exc:
        return None, IdentitySourceAudit(
            fingerprint,
            False,
            str(exc) if isinstance(exc, IdentitySourceError) else "identity cache I/O failed",
            path if path.exists() else None,
        )
    return bundle, IdentitySourceAudit(fingerprint, False, None, path)
