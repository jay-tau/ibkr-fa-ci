"""The synthetic held ETF the Robinhood fund entity tests share (#1812): its
security config and SEC fund record, the N-CEN filings, documents and index
replies an opener serves for it, its registrant's GLEIF reply, the fund lookup
and its cached N-CEN, and the whole refusal an unusable N-CEN cache gives.

It also builds the parsed N-CEN identity and GLEIF registrant records that the
entity override compute and pipeline entity tests share (#1822).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from ibkr_fa.compute.robinhood_entity_overrides import (
    RobinhoodFundEntityAudit,
    RobinhoodFundEntityEvidence,
    resolve_robinhood_fund_entity_overrides,
)
from ibkr_fa.config import FaEntityOverride, RobinhoodSecurityConfig
from ibkr_fa.models.reference_identity import (
    SEC_SUBMISSION_URL_TEMPLATE,
    GleifRegistrantRecord,
    IdentitySourceBundle,
    NcenFundIdentity,
    SecFundRecord,
)
from ibkr_fa.robinhood_fund_entity_sources import (
    _artifact_path,
    load_robinhood_fund_entity_evidence,
)
from ibkr_fa.robinhood_identity_sources import IdentitySourceOpener
from ibkr_fa.robinhood_legal_entity_sources import LegalAddressOpener
from ibkr_fa.types import Isin
from tests._entity_cache import reseal_file
from tests._entity_cache_remedies import MOVE_ASIDE, MOVE_ASIDE_SET_CONTACT, SET_IDENTITY_TOO
from tests._entity_transport import RecordingOpener
from tests._gleif_records import gleif_address, gleif_record, gleif_reply
from tests._no_echo import assert_no_markers
from tests._robinhood_security import robinhood_security

FUND_CIK = 9991060
FUND_SERIES_ID = "S000999920"
FUND_CLASS_ID = "C000999921"
_ISIN = Isin("US99992D2027")
FUND_LEI = "999900SAMPLE00000355"
FUND_CUTOFF = date(2025, 12, 31)
# An index row; tests may supply non-string cells.
FilingRow = dict[str, Any]


def fund_security(
    *,
    symbol: str = "XQAU",
    cusip: str = "99992D202",
    isin: Isin = _ISIN,
) -> RobinhoodSecurityConfig:
    return robinhood_security(
        symbol=symbol,
        cusip=cusip,
        isin=isin,
        listing_exchange="ARCA",
        sub_category="ETF",
    )


def fund_identity_sources(
    *,
    symbol: str = "XQAU",
    cik: int = FUND_CIK,
    series_id: str = FUND_SERIES_ID,
    class_id: str = FUND_CLASS_ID,
) -> IdentitySourceBundle:
    return IdentitySourceBundle(
        {},
        {},
        {symbol: (SecFundRecord(cik, series_id, class_id, symbol),)},
        {},
        {"source": "a" * 64},
    )


def _filing_block(rows: list[FilingRow]) -> dict[str, list[object]]:
    keys = (
        "accessionNumber",
        "filingDate",
        "reportDate",
        "acceptanceDateTime",
        "form",
        "primaryDocument",
    )
    return {key: [row[key] for row in rows] for key in keys}


def ncen_filing(
    accession: str,
    report_date: str,
    *,
    form: object = "N-CEN",
    filing_date: str = "2025-06-10",
    acceptance: object = "2025-06-10T15:30:00.000Z",
) -> FilingRow:
    return {
        "accessionNumber": accession,
        "filingDate": filing_date,
        "reportDate": report_date,
        "acceptanceDateTime": acceptance,
        "form": form,
        "primaryDocument": "xslFormN-CEN_X01/primary_doc.xml",
    }


def ncen_document_url(cik: int, accession: str) -> str:
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{cik}/{accession.replace('-', '')}/primary_doc.xml"
    )


def ncen_xml(
    *,
    cik: int = FUND_CIK,
    report_date: str = "2025-03-31",
    registrant_name: str = "Sample Fund Trust",
    registrant_lei: str = FUND_LEI,
    series_id: str = FUND_SERIES_ID,
    series_name: str = "Sample Fund AU ETF",
    series_lei: str = "999900SAMPLE00000452",
    class_id: str = FUND_CLASS_ID,
    ticker: str = "XQAU",
    class_name: str = "Sample Fund AU ETF",
    series_id_xml: str | None = None,
) -> bytes:
    """One filing's XML; ``series_id_xml`` replaces the series-ID element as raw XML."""
    if series_id_xml is None:
        series_id_xml = f"<mgmtInvSeriesId>{series_id}</mgmtInvSeriesId>"
    registrant_name_text = escape(registrant_name)
    series_name_text = escape(series_name)
    class_name_attr = quoteattr(class_name)
    class_id_attr = quoteattr(class_id)
    ticker_attr = quoteattr(ticker)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/ncen">
  <formData>
    <generalInfo reportEndingPeriod="{report_date}"/>
    <registrantInfo>
      <registrantFullName>{registrant_name_text}</registrantFullName>
      <registrantCik>{cik:010d}</registrantCik>
      <registrantLei>{registrant_lei}</registrantLei>
    </registrantInfo>
    <managementInvestmentQuestionSeriesInfo>
      <managementInvestmentQuestion>
        <mgmtInvFundName>{series_name_text}</mgmtInvFundName>
        {series_id_xml}
        <mgmtInvLei>{series_lei}</mgmtInvLei>
        <sharesOutstandings>
          <sharesOutstanding sharesOutstandingClassName={class_name_attr}
            sharesOutstandingClassId={class_id_attr}
            sharesOutstandingTickerSymbol={ticker_attr}/>
        </sharesOutstandings>
      </managementInvestmentQuestion>
    </managementInvestmentQuestionSeriesInfo>
  </formData>
</edgarSubmission>
""".encode()


def fund_registrant_reply(
    *,
    lei: str = FUND_LEI,
    legal_name: str = "SAMPLE FUND TRUST",
    category: object = "FUND",
    successor_name: str | None = None,
) -> bytes:
    return gleif_reply(
        gleif_record(
            lei=lei,
            legal_name=legal_name,
            legal_address=gleif_address(
                ["c/o SAMPLE REGISTERED AGENT", "SAMPLE AGENT CENTER", "99 SAMPLE ST"],
                city="SAMPLETON",
                region="US-DE",
                postal_code="99991",
            ),
            jurisdiction="US-DE",
            category=category,
            legal_form_id="4FSX",
            successor_name=successor_name,
            last_update="2025-11-03T00:00:00Z",
        ),
        publish_date="2026-08-24T00:00:00Z",
    )


# The LEI the parsed records below carry; the N-CEN XML and GLEIF reply above
# carry FUND_LEI instead.
REGISTRANT_LEI = "999900SAMPLE00000161"


def registrant_record(**changes: object) -> GleifRegistrantRecord:
    """A GLEIF registrant record: a sample depositary bank unless ``changes`` say otherwise."""
    record = GleifRegistrantRecord(
        lei=REGISTRANT_LEI,
        legal_name="Sample Depositary Bank, National Association",
        address_lines=("1 Sample Plaza",),
        city="Sampleville",
        region="US-SD",
        country_iso2="US",
        postal_code="99992",
        jurisdiction="US",
        category="GENERAL",
        legal_form_id="62VJ",
        legal_form_other="",
        entity_status="ACTIVE",
        registration_status="ISSUED",
        registration_last_update="2026-05-01T00:00:00Z",
        has_successor=False,
        golden_copy_publish_date="2026-08-25T00:00:00Z",
        source_url=f"https://api.gleif.org/api/v1/lei-records/{REGISTRANT_LEI}",
        source_sha256="a" * 64,
    )
    return replace(record, **changes)


def ncen_identity(registrant_name: str = "Sample Fund Trust") -> NcenFundIdentity:
    return NcenFundIdentity(
        cik=9991060,
        series_id="S000999920",
        class_id="C000999921",
        ticker="XQAU",
        registrant_name=registrant_name,
        registrant_lei=REGISTRANT_LEI,
        series_name="Sample Fund AU ETF",
        series_lei="999900SAMPLE00000452",
        class_name="Sample Fund AU ETF",
        accession="0009991009-25-000024",
        form="N-CEN",
        report_date=date(2025, 3, 31),
        filing_date=date(2025, 6, 10),
        acceptance_datetime="2025-06-10T15:30:00.000Z",
        document_url="https://www.sec.gov/Archives/edgar/data/9991060/000999100925000024/primary_doc.xml",
        document_sha256="d" * 64,
    )


def fund_registrant_record(**changes: object) -> GleifRegistrantRecord:
    return registrant_record(
        **{
            "legal_name": "SAMPLE FUND TRUST",
            "address_lines": ("99 SAMPLE ST",),
            "city": "SAMPLETON",
            "region": "US-DE",
            "postal_code": "99991",
            "category": "FUND",
            **changes,
        }
    )


def resolve_fund(
    tmp_path: Path,
    *,
    fetch: bool,
    sec_user_agent: str | None,
    sec_opener: IdentitySourceOpener,
    gleif_opener: LegalAddressOpener,
) -> tuple[
    tuple[FaEntityOverride, ...],
    tuple[RobinhoodFundEntityEvidence, ...],
    RobinhoodFundEntityAudit,
]:
    lookup = load_robinhood_fund_entity_evidence(
        (fund_security(),),
        fund_identity_sources(),
        cutoff=FUND_CUTOFF,
        cache_dir=tmp_path,
        fetch=fetch,
        sec_user_agent=sec_user_agent,
        sec_opener=sec_opener,
        gleif_opener=gleif_opener,
    )
    return resolve_robinhood_fund_entity_overrides((fund_security(),), lookup)


def fund_index_responses(
    history_rows: list[FilingRow],
    documents: dict[str, bytes],
    *,
    recent_rows: list[FilingRow] | None = None,
) -> dict[str, bytes]:
    root_url = SEC_SUBMISSION_URL_TEMPLATE.format(cik=FUND_CIK)
    history_name = "CIK0009991060-submissions-001.json"
    responses = {
        root_url: json.dumps(
            {
                "filings": {
                    "recent": _filing_block(recent_rows or []),
                    "files": [{"name": history_name}],
                }
            }
        ).encode(),
        f"https://data.sec.gov/submissions/{history_name}": json.dumps(
            _filing_block(history_rows)
        ).encode(),
    }
    responses.update(documents)
    return responses


# #1083: a newer and an older filing; malformed SEC evidence in the newer one
# must never move the walk to the older one.
NEWER_NCEN_FILING = ncen_filing("0009991009-25-000052", "2025-06-30")
OLDER_NCEN_FILING = ncen_filing("0009991009-25-000024", "2025-03-31")


def cached_ncen(tmp_path: Path) -> Path:
    selected = ncen_filing("0009991009-25-000024", "2025-03-31")
    responses = fund_index_responses(
        [selected],
        {ncen_document_url(FUND_CIK, selected["accessionNumber"]): ncen_xml()},
    )
    resolve_fund(
        tmp_path,
        fetch=True,
        sec_user_agent="test@example.com",
        sec_opener=RecordingOpener(responses),
        gleif_opener=RecordingOpener(
            {f"https://api.gleif.org/api/v1/lei-records/{FUND_LEI}": fund_registrant_reply()}
        ),
    )
    return _artifact_path(
        tmp_path, SecFundRecord(FUND_CIK, FUND_SERIES_ID, FUND_CLASS_ID, "XQAU"), FUND_CUTOFF
    )


def resolve_cached_fund(tmp_path: Path, *, fetch: bool = False):
    return resolve_fund(
        tmp_path,
        fetch=fetch,
        sec_user_agent=None,
        sec_opener=RecordingOpener({}),
        gleif_opener=RecordingOpener({}),
    )


def assert_ncen_refusal(
    message: str,
    cache_dir: Path,
    path: Path,
    problem: str,
    *,
    fetch: bool,
    set_contact: bool = False,
) -> None:
    """The whole N-CEN cache refusal, so it can't also carry a cached value.

    It names the cache folder, never the ISIN, which may be built from a
    statement CUSIP, nor the artifact's path, whose folder name is a
    fingerprint (#1044). ``set_contact``: the run had no usable SEC contact.
    """
    move_aside = (MOVE_ASIDE_SET_CONTACT if set_contact else MOVE_ASIDE)[fetch]
    assert message == (
        f"SEC N-CEN selection cache for a held ETF is unusable ({problem}): "
        f"{cache_dir / 'robinhood-fund-entities'}. {move_aside}, or review each held "
        "ETF's legal entity yourself and add an [[fa.entities]] entry for it to skip the "
        f"lookup. {SET_IDENTITY_TOO}"
    )
    assert_no_markers(message, _ISIN, str(path), path.parent.name)


def reseal_ncen_identity(**changes: object) -> Callable[[Path], None]:
    return lambda path: reseal_file(path, lambda artifact: artifact["identity"].update(changes))
