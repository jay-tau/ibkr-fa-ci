"""The stubbed Robinhood bundle the entity-cache CLI tests run (#1702)."""

from __future__ import annotations

from pathlib import Path

import pytest

import ibkr_fa.pipeline_robinhood_bundle as bundle_mod
import ibkr_fa.pipeline_robinhood_entities as entities
from ibkr_fa.models.reference_identity import ObservedIdentityInput
from tests._robinhood_adr import F6_URL as _F6_URL
from tests._robinhood_bundle_stubs import bundle_config_text as _config_text
from tests._robinhood_bundle_stubs import patch_bundle_sources as _patch_sources
from tests._robinhood_bundle_stubs import write_bundle as _write_bundle
from tests._robinhood_toml import robinhood_security_toml

# The ADR the bundle holds unless a test configures another security.
_ADR_CUSIP = "99992F108"
_ADR = robinhood_security_toml(
    symbol="XQAX",
    cusip=_ADR_CUSIP,
    isin="US99992F1084",
    listing_exchange="NYSE",
    is_depositary_receipt="true",
    source=f"ADS programme Form F-6: {_F6_URL}",
)


def cache_folder(tmp_path: Path) -> Path:
    """The input's default cache folder."""
    return tmp_path / "bundle" / ".ibkr-fa-price-cache"


def held_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    securities: str = _ADR,
    cusip: str = _ADR_CUSIP,
    parent_fields: str = "",
) -> Path:
    """A stubbed bundle that holds the security with ``cusip`` in 2025, from
    the ``[[robinhood.securities]]`` entries ``securities`` (default: one ADR)
    and the ``[robinhood]`` fields ``parent_fields``. The real loader and
    presentation caller read its entity caches; returns the input's default
    cache folder, where the caller puts them."""
    _write_bundle(tmp_path / "bundle")
    (tmp_path / "user.toml").write_text(
        _config_text(parent_fields=parent_fields, securities=securities), encoding="utf-8"
    )
    _patch_sources(monkeypatch)
    monkeypatch.setattr(
        bundle_mod,
        "observed_robinhood_identity_inputs",
        lambda _paired: (ObservedIdentityInput(_ADR_CUSIP, "XQAX", "Sample Issuer AX"),),
    )
    monkeypatch.setattr(
        bundle_mod,
        "resolve_robinhood_presentation_evidence",
        entities.resolve_robinhood_presentation_evidence,
    )
    monkeypatch.setattr(entities, "_held_cusips", lambda *_args: frozenset({cusip}))
    (tmp_path / "out").mkdir(mode=0o700)
    return cache_folder(tmp_path)
