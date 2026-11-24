"""Exact SEC N-CEN and GLEIF registrant evidence for Robinhood ETF A3 rows.

``SCHEMA_VERSION`` is 2 since #1083. Version 1's reader could skip malformed
series IDs or form cells and ranked unchecked acceptance times as strings.
It could therefore select an older filing. Such a selection looks like a
good one, so no version 1 artifact is reused.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final, NoReturn, cast

from ibkr_fa.config import RobinhoodSecurityConfig
from ibkr_fa.evidence_cache import (
    HASH_KEY,
    SHA256_PATTERN,
    as_object,
    as_text,
    canonical_sha256,
    collapsed,
    is_schema,
    open_sealed_artifact,
    publish_once,
)
from ibkr_fa.exceptions import IbkrFaError, raise_replacing
from ibkr_fa.models.reference_identity import (
    LEI_PATTERN,
    NCEN_MISSING_CONTACT,
    SEC_SUBMISSION_URL_TEMPLATE,
    GleifRegistrantRecord,
    IdentitySourceBundle,
    NcenFundIdentity,
    RegistrantLookup,
    SecFundRecord,
)
from ibkr_fa.robinhood_identity_sources import (
    SET_IDENTITY_TOO,
    IdentitySourceError,
    IdentitySourceOpener,
    SecEntityCacheError,
    sec_move_aside_remedy,
    sec_source_opener,
)
from ibkr_fa.robinhood_legal_entity_sources import (
    LegalAddressOpener,
    load_gleif_registrant_by_lei,
)
from ibkr_fa.types import Isin

CACHE_SUBDIRECTORY: Final[str] = "robinhood-fund-entities"
SCHEMA_VERSION: Final[int] = 2

_ARTIFACT_NAME: Final[str] = "ncen-selection.json"
_FINGERPRINT_DOMAIN: Final[str] = "ibkr-fa/robinhood-ncen-selection"
_SEC_SUBMISSIONS_BASE: Final[str] = "https://data.sec.gov/submissions"
_SEC_ARCHIVES_BASE: Final[str] = "https://www.sec.gov/Archives/edgar/data"
_MAX_INDEX_BYTES: Final[int] = 32 * 1024 * 1024
_MAX_DOCUMENT_BYTES: Final[int] = 8 * 1024 * 1024
_ACCESSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[0-9]{10}-[0-9]{2}-[0-9]{6}\Z")
_INDEX_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\ACIK[0-9]{10}-submissions-[0-9]{3}\.json\Z"
)
_DOCUMENT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9._-]+\.xml\Z")
_SERIES_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"\AS[0-9]{9}\Z")
_ACCEPTANCE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z\Z"
)
_CACHE_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"selected", "no-precutoff-series", "class-mismatch", "ticker-mismatch"}
)


class FundEntitySourceError(ValueError):
    """An SEC N-CEN source or immutable cache artifact is unusable."""


@dataclass(frozen=True, slots=True)
class NcenCandidate:
    """One N-CEN submission row from current or historical SEC indexes."""

    cik: int
    accession: str
    form: str
    report_date: date
    filing_date: date
    acceptance_datetime: str
    accepted_at: datetime
    document_name: str

    @property
    def document_url(self) -> str:
        accession = self.accession.replace("-", "")
        return f"{_SEC_ARCHIVES_BASE}/{self.cik}/{accession}/{self.document_name}"


@dataclass(frozen=True, slots=True)
class _SourceEntry:
    url: str
    sha256: str
    bytes: int


@dataclass(frozen=True, slots=True)
class _CheckedCandidate:
    accession: str
    report_date: str
    form: str
    reason: str


def _fund_request(fund: SecFundRecord) -> dict[str, object]:
    """The fund class an N-CEN selection is for, as its fingerprint, artifact and reader key it."""
    return {
        "cik": fund.cik,
        "series_id": fund.series_id,
        "class_id": fund.class_id,
        "ticker": fund.symbol,
    }


def _fingerprint(fund: SecFundRecord, cutoff: date) -> str:
    return canonical_sha256(
        {
            "domain": _FINGERPRINT_DOMAIN,
            "schema": SCHEMA_VERSION,
            **_fund_request(fund),
            "cutoff": cutoff.isoformat(),
        }
    )


def _artifact_path(cache_dir: Path, fund: SecFundRecord, cutoff: date) -> Path:
    return cache_dir / CACHE_SUBDIRECTORY / _fingerprint(fund, cutoff) / _ARTIFACT_NAME


def _parse_date(value: object, *, field: str) -> date:
    if not isinstance(value, str):
        raise FundEntitySourceError(f"SEC N-CEN {field} is malformed")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FundEntitySourceError(f"SEC N-CEN {field} is malformed") from exc


def _accepted_at(value: str) -> datetime | None:
    """The UTC instant of an SEC ``acceptanceDateTime``, exactly ``YYYY-MM-DDTHH:MM:SS.mmmZ``."""
    if _ACCEPTANCE_PATTERN.fullmatch(value) is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return None


def _cached_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise FundEntitySourceError("N-CEN selection cache is malformed")
    return collapsed(value)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(parent: ET.Element, name: str, *, scope: str) -> str:
    """The text of ``parent``'s one ``name`` child, or ``""`` when it has none."""
    matches = [child for child in parent if _local(child.tag) == name]
    if len(matches) > 1 or any(len(child) > 0 for child in matches):
        raise FundEntitySourceError(f"SEC N-CEN XML {scope} has a repeated or nested {name}")
    return collapsed(matches[0].text or "") if matches else ""


def _series_id(question: ET.Element, position: int) -> str:
    """A question's one ``mgmtInvSeriesId``: no child elements, ``S`` and nine digits."""
    matches = [child for child in question if _local(child.tag) == "mgmtInvSeriesId"]
    text = matches[0].text if len(matches) == 1 and len(matches[0]) == 0 else None
    if text is None or _SERIES_ID_PATTERN.fullmatch(text) is None:
        raise FundEntitySourceError(
            f"SEC N-CEN XML question {position} has a malformed mgmtInvSeriesId"
        )
    return text


class _SourceCache:
    def __init__(self, opener: IdentitySourceOpener) -> None:
        self._opener = opener
        self._raw: dict[str, bytes] = {}

    def get(self, url: str, *, maximum: int) -> bytes:
        if url not in self._raw:
            raw = self._opener(url)
            if not raw or len(raw) > maximum:
                raise FundEntitySourceError("SEC N-CEN source has an invalid size")
            self._raw[url] = raw
        return self._raw[url]

    def entry(self, url: str) -> _SourceEntry:
        raw = self._raw[url]
        return _SourceEntry(url, hashlib.sha256(raw).hexdigest(), len(raw))


def _json_object(raw: bytes, *, source: str) -> Mapping[str, object]:
    try:
        payload = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise FundEntitySourceError(f"{source} is malformed") from exc
    result = as_object(payload)
    if result is None:
        raise FundEntitySourceError(f"{source} is malformed")
    return result


def _candidate_rows(
    block: Mapping[str, object], *, cik: int, part: str
) -> tuple[NcenCandidate, ...]:
    """N-CEN rows of one index ``part``: ``recent filings`` or ``historical page <k>``."""
    keys = (
        "accessionNumber",
        "filingDate",
        "reportDate",
        "acceptanceDateTime",
        "form",
        "primaryDocument",
    )
    columns = {key: block.get(key) for key in keys}
    if not all(isinstance(value, list) for value in columns.values()):
        raise FundEntitySourceError("SEC submissions index has malformed filing columns")
    lists = cast("dict[str, list[object]]", columns)
    lengths = {len(value) for value in lists.values()}
    if len(lengths) != 1:
        raise FundEntitySourceError("SEC submissions index filing columns disagree")
    result = []
    for index in range(next(iter(lengths), 0)):
        row = f"SEC submissions index {part} row {index + 1}"
        form = as_text(lists["form"][index])
        if not form:
            raise FundEntitySourceError(f"{row} has a malformed form")
        if form not in {"N-CEN", "N-CEN/A"}:
            continue
        accession = as_text(lists["accessionNumber"][index])
        primary_raw = as_text(lists["primaryDocument"][index])
        primary = Path(primary_raw).name
        if (
            _ACCESSION_PATTERN.fullmatch(accession) is None
            or _DOCUMENT_NAME_PATTERN.fullmatch(primary) is None
            or ".." in Path(primary_raw).parts
            or primary_raw.startswith(("/", "\\"))
        ):
            raise FundEntitySourceError("SEC N-CEN filing identity is malformed")
        acceptance = lists["acceptanceDateTime"][index]
        if not isinstance(acceptance, str) or (accepted_at := _accepted_at(acceptance)) is None:
            raise FundEntitySourceError(f"{row} has a malformed acceptanceDateTime")
        result.append(
            NcenCandidate(
                cik=cik,
                accession=accession,
                form=form,
                report_date=_parse_date(lists["reportDate"][index], field="report date"),
                filing_date=_parse_date(lists["filingDate"][index], field="filing date"),
                acceptance_datetime=acceptance,
                accepted_at=accepted_at,
                document_name=primary,
            )
        )
    return tuple(result)


def _submission_candidates(
    cik: int,
    cache: _SourceCache,
) -> tuple[tuple[NcenCandidate, ...], tuple[_SourceEntry, ...]]:
    root_url = SEC_SUBMISSION_URL_TEMPLATE.format(cik=cik)
    root = _json_object(cache.get(root_url, maximum=_MAX_INDEX_BYTES), source="SEC submissions")
    filings = as_object(root.get("filings"))
    recent = as_object(None if filings is None else filings.get("recent"))
    files = None if filings is None else filings.get("files")
    if recent is None or not isinstance(files, list):
        raise FundEntitySourceError("SEC submissions index is missing filing blocks")
    candidates = list(_candidate_rows(recent, cik=cik, part="recent filings"))
    urls = [root_url]
    for page, entry in enumerate(files, start=1):
        item = as_object(entry)
        name = "" if item is None else as_text(item.get("name"))
        if _INDEX_NAME_PATTERN.fullmatch(name) is None:
            raise FundEntitySourceError("SEC submissions historical index name is malformed")
        url = f"{_SEC_SUBMISSIONS_BASE}/{name}"
        block = _json_object(
            cache.get(url, maximum=_MAX_INDEX_BYTES),
            source="SEC submissions historical index",
        )
        candidates.extend(_candidate_rows(block, cik=cik, part=f"historical page {page}"))
        urls.append(url)
    by_accession: dict[str, NcenCandidate] = {}
    for candidate in candidates:
        existing = by_accession.get(candidate.accession)
        if existing is not None and existing != candidate:
            raise FundEntitySourceError("SEC submissions indexes disagree on one accession")
        by_accession[candidate.accession] = candidate
    return tuple(by_accession.values()), tuple(cache.entry(url) for url in urls)


def _parse_ncen_document(
    raw: bytes,
    candidate: NcenCandidate,
    fund: SecFundRecord,
) -> tuple[NcenFundIdentity | None, str]:
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise FundEntitySourceError("SEC N-CEN XML contains a forbidden declaration")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise FundEntitySourceError("SEC N-CEN XML is malformed") from exc
    general = [item for item in root.iter() if _local(item.tag) == "generalInfo"]
    registrants = [item for item in root.iter() if _local(item.tag) == "registrantInfo"]
    if len(general) != 1 or len(registrants) != 1:
        raise FundEntitySourceError("SEC N-CEN XML has invalid top-level identity")
    report_date = _parse_date(
        general[0].attrib.get("reportEndingPeriod"),
        field="XML report period",
    )
    if report_date != candidate.report_date:
        raise FundEntitySourceError("SEC N-CEN XML report period disagrees with its index")
    registrant = registrants[0]
    registrant_cik = _child_text(registrant, "registrantCik", scope="registrantInfo").lstrip("0")
    if registrant_cik != str(candidate.cik):
        raise FundEntitySourceError("SEC N-CEN XML is for another registrant")
    registrant_name = _child_text(registrant, "registrantFullName", scope="registrantInfo")
    registrant_lei = _child_text(registrant, "registrantLei", scope="registrantInfo")
    # Every question's series ID is checked: a malformed one could be the
    # requested series, so the filing can't be called series-absent.
    all_questions = (
        item for item in root.iter() if _local(item.tag) == "managementInvestmentQuestion"
    )
    questions = [
        (position, item)
        for position, item in enumerate(all_questions, start=1)
        if _series_id(item, position) == fund.series_id
    ]
    if not questions:
        return None, "series-absent"
    if len(questions) != 1:
        raise FundEntitySourceError("SEC N-CEN XML duplicates the requested series")
    position, question = questions[0]
    series_name = _child_text(question, "mgmtInvFundName", scope=f"question {position}")
    series_lei = _child_text(question, "mgmtInvLei", scope=f"question {position}")
    classes = [
        item
        for item in question.iter()
        if _local(item.tag) == "sharesOutstanding"
        and collapsed(item.attrib.get("sharesOutstandingClassId", "")) == fund.class_id
    ]
    if len(classes) != 1:
        return None, "class-mismatch"
    ticker = collapsed(classes[0].attrib.get("sharesOutstandingTickerSymbol", ""))
    if ticker.upper() != fund.symbol.upper():
        return None, "ticker-mismatch"
    class_name = collapsed(classes[0].attrib.get("sharesOutstandingClassName", ""))
    if (
        not registrant_name
        or LEI_PATTERN.fullmatch(registrant_lei) is None
        or not series_name
        or not class_name
    ):
        raise FundEntitySourceError("SEC N-CEN XML is missing selected identity fields")
    return (
        NcenFundIdentity(
            cik=fund.cik,
            series_id=fund.series_id,
            class_id=fund.class_id,
            ticker=fund.symbol,
            registrant_name=registrant_name,
            registrant_lei=registrant_lei,
            series_name=series_name,
            series_lei=series_lei,
            class_name=class_name,
            accession=candidate.accession,
            form=candidate.form,
            report_date=candidate.report_date,
            filing_date=candidate.filing_date,
            acceptance_datetime=candidate.acceptance_datetime,
            document_url=candidate.document_url,
            document_sha256=hashlib.sha256(raw).hexdigest(),
        ),
        "selected",
    )


def _candidate_key(candidate: NcenCandidate) -> tuple[date, bool, datetime, str]:
    return (
        candidate.report_date,
        candidate.form == "N-CEN/A",
        candidate.accepted_at,
        candidate.accession,
    )


def _select_identity(
    fund: SecFundRecord,
    cutoff: date,
    cache: _SourceCache,
) -> tuple[
    NcenFundIdentity | None,
    str,
    tuple[_CheckedCandidate, ...],
    tuple[_SourceEntry, ...],
]:
    candidates, index_sources = _submission_candidates(fund.cik, cache)
    eligible = sorted(
        (candidate for candidate in candidates if candidate.report_date <= cutoff),
        key=_candidate_key,
        reverse=True,
    )
    checked: list[_CheckedCandidate] = []
    sources = list(index_sources)
    for candidate in eligible:
        raw = cache.get(candidate.document_url, maximum=_MAX_DOCUMENT_BYTES)
        sources.append(cache.entry(candidate.document_url))
        identity, reason = _parse_ncen_document(raw, candidate, fund)
        checked.append(
            _CheckedCandidate(
                candidate.accession,
                candidate.report_date.isoformat(),
                candidate.form,
                reason,
            )
        )
        if reason == "series-absent":
            continue
        if identity is None:
            return None, reason, tuple(checked), tuple(sources)
        return identity, "selected", tuple(checked), tuple(sources)
    return None, "no-precutoff-series", tuple(checked), tuple(sources)


def _identity_payload(identity: NcenFundIdentity) -> dict[str, object]:
    return {
        "cik": identity.cik,
        "series_id": identity.series_id,
        "class_id": identity.class_id,
        "ticker": identity.ticker,
        "registrant_name": identity.registrant_name,
        "registrant_lei": identity.registrant_lei,
        "series_name": identity.series_name,
        "series_lei": identity.series_lei,
        "class_name": identity.class_name,
        "accession": identity.accession,
        "form": identity.form,
        "report_date": identity.report_date.isoformat(),
        "filing_date": identity.filing_date.isoformat(),
        "acceptance_datetime": identity.acceptance_datetime,
        "document_url": identity.document_url,
        "document_sha256": identity.document_sha256,
    }


def _identity_from_payload(payload: Mapping[str, object]) -> NcenFundIdentity:
    cik = payload.get("cik")
    if isinstance(cik, bool) or not isinstance(cik, int) or cik <= 0:
        raise FundEntitySourceError("N-CEN cache contains an invalid CIK")
    values = {
        key: _cached_text(payload, key)
        for key in (
            "series_id",
            "class_id",
            "ticker",
            "registrant_name",
            "registrant_lei",
            "series_name",
            "series_lei",
            "class_name",
            "accession",
            "form",
            "acceptance_datetime",
            "document_url",
            "document_sha256",
        )
    }
    acceptance = payload.get("acceptance_datetime")
    if (
        not all(
            values[key]
            for key in (
                "series_id",
                "class_id",
                "ticker",
                "registrant_name",
                "registrant_lei",
                "series_name",
                "class_name",
                "accession",
                "form",
                "document_url",
                "document_sha256",
            )
        )
        or LEI_PATTERN.fullmatch(values["registrant_lei"]) is None
        or values["form"] not in {"N-CEN", "N-CEN/A"}
        or _ACCESSION_PATTERN.fullmatch(values["accession"]) is None
        or not isinstance(acceptance, str)
        or _accepted_at(acceptance) is None
        or SHA256_PATTERN.fullmatch(values["document_sha256"]) is None
    ):
        raise FundEntitySourceError("N-CEN cache contains an invalid identity")
    return NcenFundIdentity(
        cik=cik,
        series_id=values["series_id"],
        class_id=values["class_id"],
        ticker=values["ticker"],
        registrant_name=values["registrant_name"],
        registrant_lei=values["registrant_lei"],
        series_name=values["series_name"],
        series_lei=values["series_lei"],
        class_name=values["class_name"],
        accession=values["accession"],
        form=values["form"],
        report_date=_parse_date(payload.get("report_date"), field="cached report date"),
        filing_date=_parse_date(payload.get("filing_date"), field="cached filing date"),
        acceptance_datetime=values["acceptance_datetime"],
        document_url=values["document_url"],
        document_sha256=values["document_sha256"],
    )


def _write_artifact(
    path: Path,
    *,
    fund: SecFundRecord,
    cutoff: date,
    fetched_at: datetime,
    outcome: str,
    identity: NcenFundIdentity | None,
    checked: tuple[_CheckedCandidate, ...],
    sources: tuple[_SourceEntry, ...],
) -> None:
    payload: dict[str, object] = {
        "schema": SCHEMA_VERSION,
        "fingerprint": _fingerprint(fund, cutoff),
        "fetched_at": fetched_at.isoformat(),
        "cutoff": cutoff.isoformat(),
        "request": _fund_request(fund),
        "outcome": outcome,
        "identity": None if identity is None else _identity_payload(identity),
        "checked_candidates": [
            {
                "accession": item.accession,
                "report_date": item.report_date,
                "form": item.form,
                "reason": item.reason,
            }
            for item in checked
        ],
        "sources": [
            {"url": item.url, "sha256": item.sha256, "bytes": item.bytes}
            for item in sorted(sources, key=lambda item: item.url)
        ],
    }
    publish_once(path, payload, refuse=_refuse_publication)


def _refuse_publication(exc: OSError) -> NoReturn:
    if isinstance(exc, FileExistsError):
        raise FundEntitySourceError("N-CEN selection cache already exists") from exc
    raise FundEntitySourceError("N-CEN selection cache could not be written") from exc


def _read_artifact(
    path: Path,
    *,
    fund: SecFundRecord,
    cutoff: date,
) -> tuple[NcenFundIdentity | None, str] | None:
    """Read one cached N-CEN selection, or ``None`` when no artifact exists."""

    opened = open_sealed_artifact(path, label="N-CEN selection cache", error=FundEntitySourceError)
    if opened is None:
        return None
    artifact, actual = opened
    expected = _cached_text(artifact, HASH_KEY)
    request = as_object(artifact.get("request"))
    request_cik = None if request is None else request.get("cik")
    sources = artifact.get("sources")
    checked = artifact.get("checked_candidates")
    if (
        not expected
        or actual != expected
        or not is_schema(artifact.get("schema"), SCHEMA_VERSION)
        or artifact.get("fingerprint") != _fingerprint(fund, cutoff)
        or not _cached_text(artifact, "fetched_at")
        or artifact.get("cutoff") != cutoff.isoformat()
        or isinstance(request_cik, bool)
        or not isinstance(request_cik, int)
        or request != _fund_request(fund)
        or not isinstance(sources, list)
        or not sources
        or not isinstance(checked, list)
    ):
        raise FundEntitySourceError("N-CEN selection cache is malformed")
    source_digests: dict[str, str] = {}
    for source in sources:
        entry = as_object(source)
        size = None if entry is None else entry.get("bytes")
        if (
            entry is None
            or not _cached_text(entry, "url")
            or SHA256_PATTERN.fullmatch(_cached_text(entry, "sha256")) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
        ):
            raise FundEntitySourceError("N-CEN selection cache has invalid provenance")
        url = _cached_text(entry, "url")
        if url in source_digests:
            raise FundEntitySourceError("N-CEN selection cache duplicates a source")
        source_digests[url] = _cached_text(entry, "sha256")
    for item in checked:
        entry = as_object(item)
        if (
            entry is None
            or _ACCESSION_PATTERN.fullmatch(_cached_text(entry, "accession")) is None
            or not _cached_text(entry, "report_date")
            or _cached_text(entry, "form") not in {"N-CEN", "N-CEN/A"}
            or not _cached_text(entry, "reason")
        ):
            raise FundEntitySourceError("N-CEN selection cache has invalid candidate evidence")
    outcome = _cached_text(artifact, "outcome")
    identity_raw = artifact.get("identity")
    if outcome not in _CACHE_OUTCOMES:
        raise FundEntitySourceError("N-CEN selection cache has an invalid outcome")
    if identity_raw is None:
        if outcome == "selected":
            raise FundEntitySourceError("N-CEN selection cache has an invalid outcome")
        return None, outcome
    identity_payload = as_object(identity_raw)
    if outcome != "selected" or identity_payload is None:
        raise FundEntitySourceError("N-CEN selection cache has an invalid identity")
    identity = _identity_from_payload(identity_payload)
    if (
        identity.cik != fund.cik
        or identity.series_id != fund.series_id
        or identity.class_id != fund.class_id
        or identity.ticker != fund.symbol
        or identity.report_date > cutoff
        or not identity.document_url.startswith(
            f"{_SEC_ARCHIVES_BASE}/{fund.cik}/{identity.accession.replace('-', '')}/"
        )
        or source_digests.get(identity.document_url) != identity.document_sha256
    ):
        raise FundEntitySourceError("N-CEN selection cache is for another fund class")
    return identity, outcome


def load_robinhood_fund_entity_evidence(  # noqa: PLR0912, PLR0915
    securities: Iterable[RobinhoodSecurityConfig],
    sources: IdentitySourceBundle | None,
    *,
    cutoff: date,
    cache_dir: Path,
    fetch: bool,
    sec_user_agent: str | None,
    sec_opener: IdentitySourceOpener | None = None,
    gleif_opener: LegalAddressOpener | None = None,
    now: datetime | None = None,
) -> RegistrantLookup[NcenFundIdentity]:
    """Load exact SEC-series ETF registrants and their GLEIF legal-entity records.

    Raises:
        SecEntityCacheError: an ETF's N-CEN selection cache artifact is unusable.
            It names the cache folder and a fixed problem, never a cached
            value, an ISIN or the artifact's path, and chains nothing that
            does: no context, and as its cause only the content-free stand-in
            :func:`~ibkr_fa.exceptions.raise_replacing` leaves for the error
            it replaced.
    """

    source_cache: _SourceCache | None = _SourceCache(sec_opener) if sec_opener is not None else None
    evidence: list[tuple[Isin, NcenFundIdentity, GleifRegistrantRecord]] = []
    failures: Counter[str] = Counter()
    requested = selection_hits = gleif_hits = 0
    for security in sorted(securities, key=lambda item: item.instrument.isin):
        if security.sub_category != "ETF" or security.is_depositary_receipt:
            continue
        requested += 1
        if sources is None:
            failures["sec-sources-unavailable"] += 1
            continue
        fund_rows = sources.sec_funds_by_symbol.get(security.instrument.symbol, ())
        if len(fund_rows) != 1:
            failures["sec-fund-not-unique"] += 1
            continue
        fund = fund_rows[0]
        path = _artifact_path(cache_dir, fund, cutoff)
        try:
            cached = _read_artifact(path, fund=fund, cutoff=cutoff)
        except (FundEntitySourceError, MemoryError) as exc:
            problem = (
                "N-CEN selection cache is unreadable" if isinstance(exc, MemoryError) else str(exc)
            )
            # Neither the ISIN, which may be built from a statement CUSIP, nor
            # the artifact's path, whose folder name is a fingerprint (#1044).
            remedy = sec_move_aside_remedy(fetch=fetch, sec_user_agent=sec_user_agent)
            refusal = SecEntityCacheError(
                f"SEC N-CEN selection cache for a held ETF is unusable ({problem}): "
                f"{cache_dir / CACHE_SUBDIRECTORY}. {remedy}, or "
                "review each held ETF's legal entity yourself and add an [[fa.entities]] entry "
                f"for it to skip the lookup. {SET_IDENTITY_TOO}"
            )
            raise_replacing(refusal, exc)
        if cached is not None:
            identity, outcome = cached
            selection_hits += 1
        elif not fetch:
            failures["ncen-cache-miss"] += 1
            continue
        else:
            try:
                try:
                    if source_cache is None:
                        source_cache = _SourceCache(sec_source_opener(sec_user_agent))
                except IdentitySourceError:
                    # Only the contact check refuses here (#1526).
                    failures[NCEN_MISSING_CONTACT] += 1
                    continue
                identity, outcome, checked, source_entries = _select_identity(
                    fund,
                    cutoff,
                    source_cache,
                )
                _write_artifact(
                    path,
                    fund=fund,
                    cutoff=cutoff,
                    fetched_at=now if now is not None else datetime.now(UTC),
                    outcome=outcome,
                    identity=identity,
                    checked=checked,
                    sources=source_entries,
                )
            except (FundEntitySourceError, IbkrFaError, MemoryError, OSError):
                failures["ncen-source-failure"] += 1
                continue
        if identity is None:
            failures[outcome or "ncen-unresolved"] += 1
            continue
        record, gleif_hit = load_gleif_registrant_by_lei(
            identity.registrant_lei,
            cache_dir=cache_dir,
            fetch=fetch,
            opener=gleif_opener,
            now=now,
        )
        gleif_hits += int(gleif_hit)
        if record is None:
            failures["gleif-cache-miss"] += 1
            continue
        evidence.append((security.instrument.isin, identity, record))
    return RegistrantLookup(
        evidence=tuple(evidence),
        requested=requested,
        selection_cache_hits=selection_hits,
        gleif_cache_hits=gleif_hits,
        failure_counts=tuple(sorted(failures.items())),
    )
