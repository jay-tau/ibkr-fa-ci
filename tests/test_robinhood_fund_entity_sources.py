from __future__ import annotations

import errno
import json
from collections.abc import Callable
from pathlib import Path

import pytest

import ibkr_fa.robinhood_fund_entity_sources as fund_sources
from ibkr_fa.compute.robinhood_entity_overrides import (
    RobinhoodFundEntityAudit,
    RobinhoodFundEntityEvidence,
)
from ibkr_fa.models.reference_identity import (
    SecFundRecord,
)
from ibkr_fa.robinhood_fund_entity_sources import (
    SCHEMA_VERSION,
    FundEntitySourceError,
    _artifact_path,
    _parse_ncen_document,
    _SourceCache,
    _submission_candidates,
    load_robinhood_fund_entity_evidence,
)
from ibkr_fa.robinhood_identity_sources import SecEntityCacheError
from ibkr_fa.robinhood_legal_entity_sources import (
    GleifLegalAddressError,
    _registrant_artifact_path,
)
from ibkr_fa.types import Isin, isin_check_digit
from tests._crash_reports import assert_replaced_unchained
from tests._deep_json import DEEP_JSON
from tests._entity_cache import dangle as _dangle
from tests._entity_cache import files as _files
from tests._entity_cache import reseal_file as _reseal_artifact
from tests._entity_transport import RecordingOpener as _Opener
from tests._no_echo import CUSIP, FILENAME, STRERROR, assert_no_markers
from tests._robinhood_fund import FUND_CIK as _CIK
from tests._robinhood_fund import FUND_CLASS_ID as _CLASS
from tests._robinhood_fund import FUND_CUTOFF as _CUTOFF
from tests._robinhood_fund import FUND_LEI as _LEI
from tests._robinhood_fund import FUND_SERIES_ID as _SERIES
from tests._robinhood_fund import NEWER_NCEN_FILING as _NEWER
from tests._robinhood_fund import OLDER_NCEN_FILING as _OLDER
from tests._robinhood_fund import FilingRow as _Row
from tests._robinhood_fund import assert_ncen_refusal as _assert_ncen_refusal
from tests._robinhood_fund import cached_ncen as _cached_fund
from tests._robinhood_fund import fund_identity_sources as _sources
from tests._robinhood_fund import fund_index_responses as _index_responses
from tests._robinhood_fund import fund_registrant_reply as _gleif_response
from tests._robinhood_fund import fund_security as _security
from tests._robinhood_fund import ncen_document_url as _document_url
from tests._robinhood_fund import ncen_filing as _filing
from tests._robinhood_fund import ncen_xml as _ncen_xml
from tests._robinhood_fund import reseal_ncen_identity as _reseal_identity
from tests._robinhood_fund import resolve_cached_fund as _resolve_cached
from tests._robinhood_fund import resolve_fund as _resolve
from tests._robinhood_gleif import exact_pattern as _exactly
from tests._robinhood_gleif import registrant_cache_refusal as _registrant_cache_refusal
from tests._robinhood_gleif import registrant_raw_type_refusal as _registrant_raw_type_refusal
from tests._robinhood_gleif import reseal_record as _reseal

_FUND = SecFundRecord(_CIK, _SERIES, _CLASS, "XQAU")


def test_historical_indexes_select_latest_precutoff_series_filing(tmp_path: Path) -> None:
    absent = _filing("0009991009-25-000031", "2025-04-30")
    selected = _filing("0009991009-25-000024", "2025-03-31")
    post_cutoff = _filing("0009991008-26-000046", "2026-03-31", filing_date="2026-06-15")
    responses = _index_responses(
        [absent, selected],
        {
            _document_url(_CIK, absent["accessionNumber"]): _ncen_xml(
                report_date="2025-04-30",
                series_id="S000999109",
                class_id="C000999109",
                ticker="OTHER",
            ),
            _document_url(_CIK, selected["accessionNumber"]): _ncen_xml(),
        },
        recent_rows=[post_cutoff],
    )
    sec_opener = _Opener(responses)
    gleif_opener = _Opener({f"https://api.gleif.org/api/v1/lei-records/{_LEI}": _gleif_response()})

    overrides, evidence, audit = _resolve(
        tmp_path,
        fetch=True,
        sec_user_agent="test@example.com",
        sec_opener=sec_opener,
        gleif_opener=gleif_opener,
    )

    assert overrides[0].name == "SAMPLE FUND TRUST - Sample Fund AU ETF"
    assert overrides[0].zip_code == "99991"
    assert evidence[0].ncen_accession == selected["accessionNumber"]
    assert audit.resolved == 1
    assert _document_url(_CIK, post_cutoff["accessionNumber"]) not in sec_opener.calls
    artifact = _artifact_path(
        tmp_path,
        SecFundRecord(_CIK, _SERIES, _CLASS, "XQAU"),
        _CUTOFF,
    )
    assert artifact.exists()
    assert json.loads(artifact.read_text())["checked_candidates"][0]["reason"] == "series-absent"

    cached, cached_evidence, cached_audit = _resolve(
        tmp_path,
        fetch=False,
        sec_user_agent=None,
        sec_opener=_Opener({}),
        gleif_opener=_Opener({}),
    )
    assert cached == overrides
    assert cached_evidence == evidence
    assert cached_audit.selection_cache_hits == 1
    assert cached_audit.gleif_cache_hits == 1


def test_amendment_is_preferred_within_same_report_period(tmp_path: Path) -> None:
    # The original has the later acceptance time and the higher accession, so
    # only amendment status can rank the amendment first.
    original = _filing(
        "0009991009-25-000012",
        "2025-03-31",
        acceptance="2025-08-01T00:00:00.000Z",
    )
    amendment = _filing("0009991009-25-000011", "2025-03-31", form="N-CEN/A")
    responses = _index_responses(
        [original, amendment],
        {
            _document_url(_CIK, original["accessionNumber"]): _ncen_xml(),
            _document_url(_CIK, amendment["accessionNumber"]): _ncen_xml(),
        },
    )

    _overrides, evidence, _audit = _resolve(
        tmp_path,
        fetch=True,
        sec_user_agent="test@example.com",
        sec_opener=_Opener(responses),
        gleif_opener=_Opener(
            {f"https://api.gleif.org/api/v1/lei-records/{_LEI}": _gleif_response()}
        ),
    )

    assert evidence[0].ncen_accession == amendment["accessionNumber"]


def test_stale_amendment_does_not_beat_newer_matching_period(tmp_path: Path) -> None:
    stale = _filing(
        "0009991008-26-000080",
        "2023-06-30",
        form="N-CEN/A",
        filing_date="2026-04-21",
        acceptance="2026-04-21T16:30:00.000Z",
    )
    current = _filing("0009991009-25-000052", "2025-06-30")
    responses = _index_responses(
        [stale, current],
        {_document_url(_CIK, current["accessionNumber"]): _ncen_xml(report_date="2025-06-30")},
    )

    _overrides, evidence, _audit = _resolve(
        tmp_path,
        fetch=True,
        sec_user_agent="test@example.com",
        sec_opener=_Opener(responses),
        gleif_opener=_Opener(
            {f"https://api.gleif.org/api/v1/lei-records/{_LEI}": _gleif_response()}
        ),
    )

    assert evidence[0].ncen_accession == current["accessionNumber"]


def test_postcutoff_only_filing_stays_manual(tmp_path: Path) -> None:
    post_cutoff = _filing("0009991008-26-000046", "2026-03-31", filing_date="2026-06-15")
    responses = _index_responses([], {}, recent_rows=[post_cutoff])

    overrides, evidence, audit = _resolve(
        tmp_path,
        fetch=True,
        sec_user_agent="test@example.com",
        sec_opener=_Opener(responses),
        gleif_opener=_Opener({}),
    )

    assert overrides == ()
    assert evidence == ()
    assert audit.failure_counts == (("no-precutoff-series", 1),)


@pytest.mark.parametrize(
    ("class_id", "ticker", "reason"),
    [
        pytest.param("C000999100", "XQAU", "class-mismatch", id="wrong-class-id"),
        pytest.param(_CLASS, "WRONG", "ticker-mismatch", id="wrong-ticker"),
    ],
)
def test_class_or_ticker_mismatch_stays_manual(
    tmp_path: Path, class_id: str, ticker: str, reason: str
) -> None:
    selected = _filing("0009991009-25-000024", "2025-03-31")
    responses = _index_responses(
        [selected],
        {
            _document_url(_CIK, selected["accessionNumber"]): _ncen_xml(
                class_id=class_id,
                ticker=ticker,
            )
        },
    )

    # The fund's GLEIF record is served, so a missed mismatch resolves the
    # fund and fails the checks below, not the opener.
    overrides, evidence, audit = _resolve(
        tmp_path,
        fetch=True,
        sec_user_agent="test@example.com",
        sec_opener=_Opener(responses),
        gleif_opener=_Opener(
            {f"https://api.gleif.org/api/v1/lei-records/{_LEI}": _gleif_response()}
        ),
    )

    assert overrides == ()
    assert evidence == ()
    assert audit.failure_counts == ((reason, 1),)


@pytest.mark.parametrize(
    ("legal_name", "category"),
    [
        pytest.param("OTHER FUND ADVISORS, LLC", "FUND", id="other-legal-name"),
        pytest.param("SAMPLE FUND TRUST", "GENERAL", id="non-fund-category"),
    ],
)
def test_ncen_registrant_must_match_fund_gleif_entity(
    tmp_path: Path, legal_name: str, category: str
) -> None:
    selected = _filing("0009991009-25-000024", "2025-03-31")
    responses = _index_responses(
        [selected],
        {_document_url(_CIK, selected["accessionNumber"]): _ncen_xml()},
    )

    overrides, evidence, audit = _resolve(
        tmp_path,
        fetch=True,
        sec_user_agent="test@example.com",
        sec_opener=_Opener(responses),
        gleif_opener=_Opener(
            {
                f"https://api.gleif.org/api/v1/lei-records/{_LEI}": _gleif_response(
                    legal_name=legal_name,
                    category=category,
                )
            }
        ),
    )

    assert overrides == ()
    assert evidence == ()
    assert audit.failure_counts == (("registrant-identity-mismatch", 1),)


def test_registrant_naming_its_successor_only_by_name_stays_manual(tmp_path: Path) -> None:
    selected = _filing("0009991009-25-000024", "2025-03-31")
    responses = _index_responses(
        [selected],
        {_document_url(_CIK, selected["accessionNumber"]): _ncen_xml()},
    )

    # #829: on 97194f8 the registrant's name and address were filed.
    overrides, evidence, audit = _resolve(
        tmp_path,
        fetch=True,
        sec_user_agent="test@example.com",
        sec_opener=_Opener(responses),
        gleif_opener=_Opener(
            {
                f"https://api.gleif.org/api/v1/lei-records/{_LEI}": _gleif_response(
                    successor_name="Successor Corp"
                )
            }
        ),
    )

    assert overrides == evidence == ()
    assert audit.failure_counts == (("gleif-lifecycle", 1),)


def test_fund_registrant_with_an_unexpected_raw_type_refuses(tmp_path: Path) -> None:
    selected = _filing("0009991009-25-000024", "2025-03-31")
    responses = _index_responses(
        [selected],
        {_document_url(_CIK, selected["accessionNumber"]): _ncen_xml()},
    )

    # #821: on 59d949e the registrant was counted as a mismatch.
    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(_registrant_raw_type_refusal(tmp_path, "data.attributes.entity.category")),
    ):
        _resolve(
            tmp_path,
            fetch=True,
            sec_user_agent="test@example.com",
            sec_opener=_Opener(responses),
            gleif_opener=_Opener(
                {f"https://api.gleif.org/api/v1/lei-records/{_LEI}": _gleif_response(category=1)}
            ),
        )
    assert not list(tmp_path.rglob("gleif-registrant.json"))


# ---- #1083: malformed SEC evidence never moves the walk to an older filing ----

_NEWER_XML = _ncen_xml(report_date="2025-06-30")
_GLEIF_URL = f"https://api.gleif.org/api/v1/lei-records/{_LEI}"
_FORM_MARKER = "Synthetic Form Marker"
_TIME_MARKER = "Synthetic Time Marker"


def _edited(xml: bytes, old: str, new: str) -> bytes:
    """``xml`` with its one ``old`` replaced by the raw ``new``."""
    assert xml.count(old.encode()) == 1
    return xml.replace(old.encode(), new.encode())


def _with_question(xml: bytes, series_id_xml: str, *, first: bool) -> bytes:
    """``xml`` with a question for another series before or after the held one."""
    question = (
        "<managementInvestmentQuestion><mgmtInvFundName>Sample Other ETF</mgmtInvFundName>"
        f"{series_id_xml}</managementInvestmentQuestion>"
    )
    if first:
        tag = "<managementInvestmentQuestionSeriesInfo>"
        return _edited(xml, tag, tag + question)
    tag = "</managementInvestmentQuestionSeriesInfo>"
    return _edited(xml, tag, question + tag)


def _resolve_newer_over_older(
    tmp_path: Path, newer_xml: bytes = _NEWER_XML, *, newer: _Row = _NEWER
) -> tuple[tuple[RobinhoodFundEntityEvidence, ...], RobinhoodFundEntityAudit, _Opener]:
    """Resolve the held ETF from a newer filing over a valid older one."""
    sec_opener = _Opener(
        _index_responses(
            [newer, _OLDER],
            {
                _document_url(_CIK, newer["accessionNumber"]): newer_xml,
                _document_url(_CIK, _OLDER["accessionNumber"]): _ncen_xml(),
            },
        )
    )
    _overrides, evidence, audit = _resolve(
        tmp_path,
        fetch=True,
        sec_user_agent="test@example.com",
        sec_opener=sec_opener,
        gleif_opener=_Opener({_GLEIF_URL: _gleif_response()}),
    )
    return evidence, audit, sec_opener


def _assert_source_failure(
    tmp_path: Path,
    evidence: tuple[RobinhoodFundEntityEvidence, ...],
    audit: RobinhoodFundEntityAudit,
    sec_opener: _Opener,
) -> None:
    """An SEC source failure: nothing selected or cached, and no older filing read."""
    assert audit.failure_counts == (("ncen-source-failure", 1),)
    assert evidence == ()
    assert not _artifact_path(tmp_path, _FUND, _CUTOFF).exists()
    assert _document_url(_CIK, _OLDER["accessionNumber"]) not in sec_opener.calls


def _assert_no_values(message: str) -> None:
    assert_no_markers(
        message, _SERIES, _NEWER["accessionNumber"], str(_CIK), _FORM_MARKER, _TIME_MARKER
    )


def _assert_xml_refusal(xml: bytes, problem: str) -> None:
    """Parsed directly, ``xml`` refuses with exactly ``problem`` and no values."""
    (candidate,), _sources = _submission_candidates(
        _CIK, _SourceCache(_Opener(_index_responses([_NEWER], {})))
    )
    with pytest.raises(FundEntitySourceError) as refusal:
        _parse_ncen_document(xml, candidate, _FUND)
    message = str(refusal.value)
    assert message == f"SEC N-CEN XML {problem}"
    _assert_no_values(message)


def _assert_index_refusal(newer: _Row, field: str) -> None:
    """Parsed directly, row 2 of either index part refuses with only its position."""
    for part, history, recent in (
        ("recent filings", [], [_OLDER, newer]),
        ("historical page 1", [_OLDER, newer], []),
    ):
        responses = _index_responses(history, {}, recent_rows=recent)
        with pytest.raises(FundEntitySourceError) as refusal:
            _submission_candidates(_CIK, _SourceCache(_Opener(responses)))
        message = str(refusal.value)
        assert message == f"SEC submissions index {part} row 2 has a malformed {field}"
        _assert_no_values(message)


@pytest.mark.parametrize(
    "series_id_xml",
    [
        pytest.param(f"<mgmtInvSeriesId>{_SERIES}</mgmtInvSeriesId>" * 2, id="duplicate-target"),
        pytest.param(
            f"<mgmtInvSeriesId>{_SERIES}</mgmtInvSeriesId><mgmtInvSeriesId>S000999109</mgmtInvSeriesId>",
            id="target-and-another",
        ),
        pytest.param("", id="missing"),
        pytest.param("<mgmtInvSeriesId> </mgmtInvSeriesId>", id="blank"),
        pytest.param("<mgmtInvSeriesId>S0009</mgmtInvSeriesId>", id="malformed"),
        pytest.param(f"<mgmtInvSeriesId>{_SERIES}<x/></mgmtInvSeriesId>", id="target-then-nested"),
    ],
)
def test_malformed_series_id_is_a_source_failure(tmp_path: Path, series_id_xml: str) -> None:
    xml = _ncen_xml(report_date="2025-06-30", series_id_xml=series_id_xml)

    # #1083: on 18146d8 the older filing was selected, or the nested ID read as the target.
    _assert_source_failure(tmp_path, *_resolve_newer_over_older(tmp_path, xml))
    _assert_xml_refusal(xml, "question 1 has a malformed mgmtInvSeriesId")


def test_malformed_series_id_of_another_series_is_a_source_failure(tmp_path: Path) -> None:
    xml = _with_question(
        _NEWER_XML, "<mgmtInvSeriesId>S000999109<x/></mgmtInvSeriesId>", first=False
    )

    # #1083: on 18146d8 this filing was selected.
    _assert_source_failure(tmp_path, *_resolve_newer_over_older(tmp_path, xml))
    _assert_xml_refusal(xml, "question 2 has a malformed mgmtInvSeriesId")


@pytest.mark.parametrize(
    ("xml", "problem"),
    [
        pytest.param(
            _edited(_NEWER_XML, "</registrantCik>", "<x/></registrantCik>"),
            "registrantInfo has a repeated or nested registrantCik",
            id="nested-registrant-cik",
        ),
        pytest.param(
            _edited(_NEWER_XML, "</registrantFullName>", "<x/></registrantFullName>"),
            "registrantInfo has a repeated or nested registrantFullName",
            id="nested-registrant-name",
        ),
        pytest.param(
            _edited(
                _NEWER_XML,
                f"<registrantLei>{_LEI}</registrantLei>",
                f"<registrantLei>{_LEI}</registrantLei>" * 2,
            ),
            "registrantInfo has a repeated or nested registrantLei",
            id="repeated-registrant-lei",
        ),
        pytest.param(
            _edited(
                _NEWER_XML,
                "<mgmtInvFundName>Sample Fund AU ETF</mgmtInvFundName>",
                "<mgmtInvFundName>Sample Fund AU ETF<x/></mgmtInvFundName>",
            ),
            "question 1 has a repeated or nested mgmtInvFundName",
            id="nested-series-name",
        ),
        pytest.param(
            _with_question(
                _edited(
                    _NEWER_XML,
                    "<mgmtInvFundName>Sample Fund AU ETF</mgmtInvFundName>",
                    "<mgmtInvFundName>Sample Fund AU ETF<x/></mgmtInvFundName>",
                ),
                "<mgmtInvSeriesId>S000999109</mgmtInvSeriesId>",
                first=True,
            ),
            "question 2 has a repeated or nested mgmtInvFundName",
            id="nested-series-name-second-question",
        ),
    ],
)
def test_repeated_or_nested_identity_field_is_a_source_failure(
    tmp_path: Path, xml: bytes, problem: str
) -> None:
    # #1083: on 18146d8 a nested field's leading text was read and the filing
    # selected; a repeated registrantLei refused as a missing identity field.
    _assert_source_failure(tmp_path, *_resolve_newer_over_older(tmp_path, xml))
    _assert_xml_refusal(xml, problem)


def test_question_without_a_series_lei_is_still_selected(tmp_path: Path) -> None:
    xml = _edited(_NEWER_XML, "<mgmtInvLei>999900SAMPLE00000452</mgmtInvLei>", "")

    evidence, audit, _sec_opener = _resolve_newer_over_older(tmp_path, xml)

    assert evidence[0].ncen_accession == _NEWER["accessionNumber"]
    assert audit.failure_counts == ()


@pytest.mark.parametrize(
    "form", [None, 123, "", [_FORM_MARKER]], ids=["null", "number", "empty", "list"]
)
def test_malformed_form_cell_is_a_source_failure(tmp_path: Path, form: object) -> None:
    newer = {**_NEWER, "form": form}

    # #1083: on 18146d8 the row was skipped and the older filing selected.
    evidence, audit, sec_opener = _resolve_newer_over_older(tmp_path, newer=newer)
    _assert_source_failure(tmp_path, evidence, audit, sec_opener)
    assert _document_url(_CIK, _NEWER["accessionNumber"]) not in sec_opener.calls
    _assert_index_refusal(newer, "form")


def test_row_for_another_form_needs_only_its_form(tmp_path: Path) -> None:
    other = {**dict.fromkeys(_OLDER), "form": "10-K"}
    responses = _index_responses(
        [other, _OLDER],
        {_document_url(_CIK, _OLDER["accessionNumber"]): _ncen_xml()},
    )

    _overrides, evidence, audit = _resolve(
        tmp_path,
        fetch=True,
        sec_user_agent="test@example.com",
        sec_opener=_Opener(responses),
        gleif_opener=_Opener({_GLEIF_URL: _gleif_response()}),
    )

    assert audit.failure_counts == ()
    assert evidence[0].ncen_accession == _OLDER["accessionNumber"]


@pytest.mark.parametrize(
    "acceptance",
    [
        pytest.param(None, id="null"),
        pytest.param(_TIME_MARKER, id="marker"),
        pytest.param("2025-02-30T10:00:00.000Z", id="impossible-date"),
        pytest.param("2025-06-10T15:30:00Z", id="no-milliseconds"),
        pytest.param("2025-06-10T15:30:00.000000Z", id="six-digits"),
        pytest.param("2025-06-10T15:30:00.000+00:00", id="offset"),
        pytest.param("2025-06-10 15:30:00.000Z", id="space"),
    ],
)
def test_malformed_acceptance_time_is_a_source_failure(tmp_path: Path, acceptance: object) -> None:
    newer = {**_NEWER, "acceptanceDateTime": acceptance}

    # #1083: on 18146d8 acceptance times were sorted without validation.
    _assert_source_failure(tmp_path, *_resolve_newer_over_older(tmp_path, newer=newer))
    _assert_index_refusal(newer, "acceptanceDateTime")


def test_later_acceptance_time_wins_within_one_period(tmp_path: Path) -> None:
    # The later one has the lower accession, so only the time can rank it first.
    later = _filing(
        "0009991009-25-000011",
        "2025-03-31",
        form="N-CEN/A",
        acceptance="2025-06-10T15:30:00.900Z",
    )
    earlier = _filing(
        "0009991009-25-000012",
        "2025-03-31",
        form="N-CEN/A",
        acceptance="2025-06-10T15:30:00.000Z",
    )
    responses = _index_responses(
        [earlier, later],
        {_document_url(_CIK, later["accessionNumber"]): _ncen_xml()},
    )

    _overrides, evidence, _audit = _resolve(
        tmp_path,
        fetch=True,
        sec_user_agent="test@example.com",
        sec_opener=_Opener(responses),
        gleif_opener=_Opener({_GLEIF_URL: _gleif_response()}),
    )

    assert evidence[0].ncen_accession == later["accessionNumber"]


@pytest.mark.parametrize("fetch", [True, False])
def test_resealed_fund_registrant_cache_refuses(tmp_path: Path, fetch: bool) -> None:
    _cached_fund(tmp_path)
    path = _registrant_artifact_path(tmp_path, _LEI)
    _reseal(path, lambda record: record.update(region=123))
    written = _files(tmp_path)

    # #826: on e16b0be the registrant's address was filed without its region.
    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(
            _registrant_cache_refusal(
                tmp_path,
                "GLEIF registrant cache field record.region has an unexpected JSON type",
                fetch=fetch,
            )
        ),
    ) as refusal:
        _resolve_cached(tmp_path, fetch=fetch)
    # #1044: not the artifact's path, whose folder name is a fingerprint.
    assert_no_markers(str(refusal.value), path.parent.name)
    assert _files(tmp_path) == written


@pytest.mark.parametrize("fetch", [True, False])
def test_invalid_registrant_lei_in_a_resealed_ncen_cache_refuses(
    tmp_path: Path, fetch: bool
) -> None:
    path = _cached_fund(tmp_path)
    _reseal_artifact(path, lambda artifact: artifact["identity"].update(registrant_lei="bad"))
    written = _files(tmp_path)

    # #826: on e16b0be this counted as gleif-invalid-lei and the row stayed manual.
    # #846: on 24a8caf the GLEIF lookup refused it, without naming the N-CEN artifact.
    with pytest.raises(SecEntityCacheError) as refusal:
        _resolve_cached(tmp_path, fetch=fetch)
    _assert_ncen_refusal(
        str(refusal.value),
        tmp_path,
        path,
        "N-CEN cache contains an invalid identity",
        fetch=fetch,
        set_contact=True,
    )
    assert "bad" not in str(refusal.value)
    assert _files(tmp_path) == written


def test_cache_only_run_without_the_registrant_artifact_counts_a_gleif_miss(
    tmp_path: Path,
) -> None:
    _cached_fund(tmp_path)
    _registrant_artifact_path(tmp_path, _LEI).unlink()

    overrides, evidence, audit = _resolve_cached(tmp_path)

    assert overrides == evidence == ()
    assert audit.failure_counts == (("gleif-cache-miss", 1),)


def _tamper(path: Path) -> None:
    artifact = json.loads(path.read_text(encoding="utf-8"))
    artifact["identity"]["registrant_name"] = "Another Trust"
    path.write_text(json.dumps(artifact), encoding="utf-8")


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        pytest.param(_tamper, "N-CEN selection cache is malformed", id="integrity"),
        pytest.param(
            lambda path: path.write_bytes(b"not json"),
            "N-CEN selection cache is unreadable",
            id="not-json",
        ),
        pytest.param(
            lambda path: path.write_bytes(DEEP_JSON),
            "N-CEN selection cache is nested too deeply",
            id="deep-json",
        ),
        pytest.param(
            lambda path: path.write_bytes(b'{"payload_sha256":"x","unexpected":NaN}'),
            "N-CEN selection cache contains a non-finite number",
            id="nonfinite-number",
        ),
        pytest.param(_dangle, "N-CEN selection cache could not be read (ENOENT)", id="dangling"),
        pytest.param(
            lambda path: _reseal_artifact(path, lambda artifact: artifact.update(schema=True)),
            "N-CEN selection cache is malformed",
            id="schema-true",
        ),
        pytest.param(
            lambda path: _reseal_artifact(
                path, lambda artifact: artifact.update(schema=float(SCHEMA_VERSION))
            ),
            "N-CEN selection cache is malformed",
            id="schema-float",
        ),
        pytest.param(
            lambda path: _reseal_artifact(
                path, lambda artifact: artifact.update(fingerprint="0" * 64)
            ),
            "N-CEN selection cache is malformed",
            id="fingerprint",
        ),
        pytest.param(
            lambda path: _reseal_artifact(
                path, lambda artifact: artifact["request"].update(cik=float(_CIK))
            ),
            "N-CEN selection cache is malformed",
            id="request-cik-float",
        ),
        pytest.param(
            lambda path: _reseal_artifact(path, lambda artifact: artifact.pop("fetched_at")),
            "N-CEN selection cache is malformed",
            id="fetched-at-missing",
        ),
        pytest.param(
            lambda path: _reseal_artifact(path, lambda artifact: artifact.update(fetched_at=123)),
            "N-CEN selection cache is malformed",
            id="fetched-at-number",
        ),
        pytest.param(
            lambda path: _reseal_artifact(path, lambda artifact: artifact.update(fetched_at="")),
            "N-CEN selection cache is malformed",
            id="fetched-at-empty",
        ),
        pytest.param(
            _reseal_identity(series_lei=123),
            "N-CEN selection cache is malformed",
            id="series-lei-number",
        ),
        pytest.param(
            _reseal_identity(acceptance_datetime=123),
            "N-CEN selection cache is malformed",
            id="acceptance-number",
        ),
        *(
            pytest.param(
                _reseal_identity(acceptance_datetime=acceptance),
                "N-CEN cache contains an invalid identity",
                id=f"acceptance-{name}",
            )
            for name, acceptance in (
                ("empty", ""),
                ("marker", _TIME_MARKER),
                ("padded", " 2025-06-10T15:30:00.000Z"),
                ("no-milliseconds", "2025-06-10T15:30:00Z"),
                ("offset", "2025-06-10T15:30:00.000+00:00"),
            )
        ),
        pytest.param(
            _reseal_identity(form="10-K"),
            "N-CEN cache contains an invalid identity",
            id="form",
        ),
        pytest.param(
            _reseal_identity(accession="000999100925000024"),
            "N-CEN cache contains an invalid identity",
            id="accession",
        ),
    ],
)
def test_unusable_ncen_cache_refuses_without_fetching(
    tmp_path: Path,
    damage: Callable[[Path], object],
    problem: str,
    fetch: bool,
) -> None:
    path = _cached_fund(tmp_path)
    damage(path)
    written = _files(tmp_path)

    # #846: on 24a8caf each counted as ncen-cache-invalid, was treated as
    # missing, escaped untyped, or was read as a valid cache hit.
    with pytest.raises(SecEntityCacheError) as refusal:
        _resolve_cached(tmp_path, fetch=fetch)
    _assert_ncen_refusal(str(refusal.value), tmp_path, path, problem, fetch=fetch, set_contact=True)
    assert _files(tmp_path) == written


@pytest.mark.parametrize("fetch", [True, False])
def test_ncen_cache_inspection_failure_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch: bool,
) -> None:
    path = _cached_fund(tmp_path)
    written = _files(tmp_path)
    lstat = Path.lstat

    def inaccessible(self: Path):
        if self == path:
            raise PermissionError("cache inspection denied")
        return lstat(self)

    monkeypatch.setattr(Path, "lstat", inaccessible)

    # #846: on 24a8caf the cache was never inspected with lstat and was read.
    with pytest.raises(SecEntityCacheError) as refusal:
        _resolve_cached(tmp_path, fetch=fetch)
    _assert_ncen_refusal(
        str(refusal.value),
        tmp_path,
        path,
        "N-CEN selection cache could not be inspected (unknown error)",
        fetch=fetch,
        set_contact=True,
    )
    assert _files(tmp_path) == written


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize(
    ("method", "failure"),
    [
        pytest.param("lstat", "could not be inspected", id="lstat"),
        pytest.param("read_text", "could not be read", id="read"),
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
def test_ncen_cache_os_error_prints_only_its_errno_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    failure: str,
    code: int | None,
    name: str,
    fetch: bool,
) -> None:
    path = _cached_fund(tmp_path)
    written = _files(tmp_path)
    original = getattr(Path, method)

    def failing(self: Path, *args: object, **kwargs: object):
        if self == path:
            raise OSError(code, STRERROR, FILENAME)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, failing)

    # #1044: on 53fc2f29 the refusal printed the ISIN and the artifact's path,
    # and chained the OSError.
    with pytest.raises(SecEntityCacheError) as refusal:
        _resolve_cached(tmp_path, fetch=fetch)
    message = str(refusal.value)
    _assert_ncen_refusal(
        message,
        tmp_path,
        path,
        f"N-CEN selection cache {failure} ({name})",
        fetch=fetch,
        set_contact=True,
    )
    # #1223: the refusal keeps only a stand-in for the private error it
    # replaced, not that error or the OSError it wrapped.
    assert_replaced_unchained(refusal.value, FundEntitySourceError, markers=(STRERROR, FILENAME))
    assert _files(tmp_path) == written


def test_ncen_refusal_prints_no_cusip_built_isin(tmp_path: Path) -> None:
    path = _cached_fund(tmp_path)
    path.write_bytes(b"not json")
    isin = Isin(f"US{CUSIP}{isin_check_digit(f'US{CUSIP}')}")

    # #1044: on 53fc2f29 the refusal printed this ISIN, and so the CUSIP in it.
    with pytest.raises(SecEntityCacheError) as refusal:
        load_robinhood_fund_entity_evidence(
            (_security(cusip=CUSIP, isin=isin),),
            _sources(),
            cutoff=_CUTOFF,
            cache_dir=tmp_path,
            fetch=False,
            sec_user_agent=None,
            sec_opener=_Opener({}),
            gleif_opener=_Opener({}),
        )
    message = str(refusal.value)
    _assert_ncen_refusal(
        message,
        tmp_path,
        path,
        "N-CEN selection cache is unreadable",
        fetch=False,
        set_contact=True,
    )
    assert_no_markers(message, isin, CUSIP)


def test_cache_only_run_without_the_ncen_artifact_counts_an_ncen_miss(tmp_path: Path) -> None:
    overrides, evidence, audit = _resolve_cached(tmp_path)

    assert overrides == evidence == ()
    assert audit.failure_counts == (("ncen-cache-miss", 1),)


def test_version_1_selection_is_never_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(fund_sources, "SCHEMA_VERSION", 1)
        old = _cached_fund(tmp_path)
    old_bytes = old.read_bytes()
    assert json.loads(old_bytes)["schema"] == 1

    # #1083: a version 1 selection may have skipped a malformed newer filing.
    overrides, evidence, audit = _resolve_cached(tmp_path)
    assert overrides == evidence == ()
    assert audit.failure_counts == (("ncen-cache-miss", 1),)

    new = _cached_fund(tmp_path)
    assert new != old
    assert json.loads(new.read_text(encoding="utf-8"))["schema"] == SCHEMA_VERSION == 2
    assert old.read_bytes() == old_bytes

    _overrides, cached_evidence, cached_audit = _resolve_cached(tmp_path)
    assert cached_evidence[0].ncen_accession == _OLDER["accessionNumber"]
    assert cached_audit.selection_cache_hits == 1
    assert cached_audit.failure_counts == ()


@pytest.mark.parametrize("fetch", [True, False])
def test_ncen_cache_hash_memory_failure_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetch: bool
) -> None:
    path = _cached_fund(tmp_path)
    written = _files(tmp_path)
    dumps = json.dumps

    def exhausted(payload, **kwargs):
        if "fetched_at" in payload:
            raise MemoryError("private cached value")
        return dumps(payload, **kwargs)

    monkeypatch.setattr(json, "dumps", exhausted)

    with pytest.raises(SecEntityCacheError) as refusal:
        _resolve_cached(tmp_path, fetch=fetch)
    _assert_ncen_refusal(
        str(refusal.value),
        tmp_path,
        path,
        "N-CEN selection cache is unreadable",
        fetch=fetch,
        set_contact=True,
    )
    assert_replaced_unchained(refusal.value, MemoryError, markers=("private cached value",))
    assert _files(tmp_path) == written
