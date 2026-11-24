"""Synthetic GLEIF legal-address records for tests to derive theirs from.

:data:`GLEIF_RECORD` is one parsed record (#1682): each test module's own
builder keeps its values by changing it with ``dataclasses.replace``, so a new
record field is added here once. :func:`gleif_record` and :func:`gleif_reply`
build the raw API reply the same way (#1702): an argument whose default is the
value every module's reply shares, and no default where the modules differ, so
each module's values stay at its call.
"""

from __future__ import annotations

import json
from typing import Final

from ibkr_fa.models.reference_identity import GleifLegalAddressRecord
from ibkr_fa.types import Isin

GLEIF_RECORD: Final = GleifLegalAddressRecord(
    isin=Isin("US99992A1097"),
    # A synthetic LEI with valid ISO 17442 check digits.
    lei="999900SAMPLE00000161",
    legal_name="TEST ENTITY",
    address_lines=("99 SAMPLE ST",),
    city="SAMPLETON",
    region="US-DE",
    country_iso2="US",
    postal_code="99991",
    jurisdiction="US-DE",
    category="GENERAL",
    legal_form_id="XTIQ",
    legal_form_other="",
    entity_status="ACTIVE",
    registration_status="ISSUED",
    registration_last_update="2026-01-01T00:00:00Z",
    has_successor=False,
    golden_copy_publish_date="2026-08-23T00:00:00Z",
    source_url="https://api.gleif.org/api/v1/lei-records",
    source_sha256="b" * 64,
)


def gleif_address(
    lines: list[str], *, city: str, region: str, postal_code: str | None, country: str = "US"
) -> dict[str, object]:
    """One raw GLEIF address, legal or headquarters."""
    return {
        "addressLines": lines,
        "city": city,
        "region": region,
        "country": country,
        "postalCode": postal_code,
    }


def gleif_record(
    *,
    lei: str,
    legal_name: str,
    legal_address: dict[str, object],
    jurisdiction: str,
    category: object,
    legal_form_id: str,
    last_update: str,
    headquarters_address: dict[str, object] | None = None,
    legal_form_other: str | None = None,
    entity_status: str = "ACTIVE",
    successor_lei: str | None = None,
    successor_name: str | None = None,
    registration_status: str = "ISSUED",
) -> dict[str, object]:
    """One raw ``lei-records`` entry with an empty ``successorEntities`` list;
    ``headquarters_address`` only when given."""
    entity: dict[str, object] = {"legalName": {"name": legal_name}, "legalAddress": legal_address}
    if headquarters_address is not None:
        entity["headquartersAddress"] = headquarters_address
    entity |= {
        "jurisdiction": jurisdiction,
        "category": category,
        "legalForm": {"id": legal_form_id, "other": legal_form_other},
        "status": entity_status,
        "successorEntity": {"lei": successor_lei, "name": successor_name},
        "successorEntities": [],
    }
    return {
        "type": "lei-records",
        "id": lei,
        "attributes": {
            "lei": lei,
            "entity": entity,
            "registration": {"status": registration_status, "lastUpdateDate": last_update},
        },
    }


def gleif_reply(data: object, *, publish_date: str) -> bytes:
    """The raw reply: ``data`` (one record, or a list of them) under the golden
    copy's publication date."""
    return json.dumps(
        {"meta": {"goldenCopy": {"publishDate": publish_date}}, "data": data}
    ).encode()
