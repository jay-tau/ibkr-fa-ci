"""Source stubs for the Robinhood presentation-entity tests.

They make ``resolve_robinhood_presentation_evidence`` hold one CUSIP and find
no ADR or fund registrant, so a test reaches only the source it names.
"""

from __future__ import annotations

import pytest

import ibkr_fa.pipeline_robinhood_entities as entities
from ibkr_fa.models.reference_identity import GleifLegalAddressLookup, RegistrantLookup
from ibkr_fa.types import Isin


def stub_registrant_sources(monkeypatch: pytest.MonkeyPatch, cusip: str) -> None:
    """Make the presentation step hold only ``cusip`` and find no ADR or
    fund registrant."""
    monkeypatch.setattr(
        entities, "_held_cusips", lambda _quantities, _calendar_year: frozenset({cusip})
    )
    for loader in ("load_robinhood_adr_entity_evidence", "load_robinhood_fund_entity_evidence"):
        monkeypatch.setattr(
            entities, loader, lambda *_args, **_kwargs: RegistrantLookup((), 0, 0, 0)
        )


def stub_zero_match_sources(monkeypatch: pytest.MonkeyPatch, cusip: str, isin: Isin) -> None:
    """:func:`stub_registrant_sources`, with GLEIF finding no record for ``isin``."""
    stub_registrant_sources(monkeypatch, cusip)
    monkeypatch.setattr(
        entities,
        "load_robinhood_legal_entity_evidence",
        lambda *_args, **_kwargs: GleifLegalAddressLookup((), {isin: "zero-match"}, 1),
    )
