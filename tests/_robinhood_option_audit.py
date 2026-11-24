"""Stand-ins for the Robinhood option close-audit tests: an options result,
a config, the option close refusal texts, served Massive replies and seeded
cache records.

``held_call_options`` is a stand-in options result for one call contract held
15-17 Jan 2025, with the contract; ``massive_key_config`` is a stand-in config
whose ``prices`` names the given Massive API key file, or none.
``serve_massive`` serves synthetic Massive replies to the real reader as
fetched bytes or as a hashed cache artifact, and ``seed_records`` writes a
synthetic record into each identity and entity cache folder.
``tests/test_pipeline_robinhood_option_prices.py``,
``tests/test_cli_robinhood.py``, ``tests/test_pipeline_robinhood_equity_prices.py``
and ``tests/test_prices_massive_refusal_chains.py`` use them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from ibkr_fa import (
    robinhood_adr_entity_sources,
    robinhood_fund_entity_sources,
    robinhood_identity_sources,
    robinhood_legal_entity_sources,
)
from ibkr_fa.compute.robinhood_options import RobinhoodOptionContract
from ibkr_fa.config import PricesConfig
from ibkr_fa.parser.robinhood import RobinhoodEventKind
from ibkr_fa.prices import massive_options
from tests._massive_option_replies import mr_artifact_bytes


def held_call_options(symbol: str = "XQAQ", strike: str = "250"):
    contract = RobinhoodOptionContract(
        account_index=0,
        symbol=symbol,
        expiration=date(2025, 1, 17),
        right="Call",
        strike_usd=Decimal(strike),
    )
    lot = SimpleNamespace(
        contract=contract,
        open_date=date(2025, 1, 15),
        close_date=date(2025, 1, 17),
    )
    trades = (
        SimpleNamespace(
            contract=contract,
            activity_date=date(2025, 1, 15),
            quantity=Decimal("1"),
            kind=RobinhoodEventKind.OPTION_BUY_TO_OPEN,
        ),
        SimpleNamespace(
            contract=contract,
            activity_date=date(2025, 1, 17),
            quantity=Decimal("1"),
            kind=RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
        ),
    )
    return SimpleNamespace(
        trades=trades,
        lot_slices=(lot,),
        valuation_anchors=(),
        validated_valuation_anchors=(),
    ), contract


def massive_key_config(key_file: str | None = "env"):
    return SimpleNamespace(prices=PricesConfig(massive_api_key_file=key_file))


# #1523: what each option close refusal says to do.
OPTION_NOT_CACHED = (
    "complete cached Massive option closes are required: price fetching was off "
    "(--no-fetch-prices) and no complete price cache was found. Re-run without "
    "--no-fetch-prices (a Massive fetch needs [prices] massive_api_key_file in user.toml), or "
    "point --price-cache-dir at a cache directory that holds those closes."
)
OPTION_NO_KEY = (
    "Massive API key file is not configured and no complete price cache was found: complete "
    "cached Massive option closes are required, and a Massive fetch needs [prices] "
    "massive_api_key_file in user.toml. Set it to a file containing MASSIVE_API_KEY=... and "
    "re-run, or point --price-cache-dir at a cache directory that holds those closes."
)
OPTION_SHORT = (
    "A held contract's Massive series has no close for a US market session it was held "
    "(Massive has no daily bar for a day the contract didn't trade), and a cached Massive "
    "series is reused before any fetch, so a re-run alone returns the same closes. Move each "
    "such contract's cached file out of the massive-options folder of the price cache "
    "directory (--price-cache-dir) and re-run with price fetching (a Massive fetch needs "
    "[prices] massive_api_key_file in user.toml): a fresh fetch can return closes a series "
    "cached earlier lacks. If you can't tell which contract is short, move every .json file "
    "in that folder out of it instead and re-run the same way. If the fresh series is still "
    "short, enter the Robinhood Schedule FA by hand: closes are never guessed."
)


def serve_massive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path: str,
    overview: bytes,
    bars: bytes,
    *more: bytes,
) -> Path | None:
    """Serve synthetic XQMR responses to the real reader: as fetched bytes,
    with the key file beside ``user.toml`` and ``more`` served after them,
    or as a correctly hashed CY 2025 artifact under ``tmp_path / "prices"``,
    returned for a byte check. Neither path waits between requests; the
    cache path opens no network."""
    monkeypatch.setattr(massive_options, "_REQUEST_INTERVAL_SECONDS", 0)
    if path == "fetch":
        (tmp_path / "env").write_text("MASSIVE_API_KEY=SyntheticApiKeyThree\n", encoding="utf-8")
        responses = [overview, bars, *more]
        monkeypatch.setattr(
            massive_options, "_default_url_opener", lambda _request, _timeout: responses.pop(0)
        )
        return None
    artifact = (
        tmp_path / "prices" / "massive-options" / "XQMR250117C00250000-2025-01-01-2025-12-31.json"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(mr_artifact_bytes(overview, bars, end=date(2025, 12, 31)))
    return artifact


# The subfolders of a Robinhood price cache directory that hold its identity
# and entity records; neither short-series fallback moves them (#1530).
RECORD_FOLDERS = (
    robinhood_identity_sources.CACHE_SUBDIRECTORY,
    robinhood_legal_entity_sources.CACHE_SUBDIRECTORY,
    robinhood_adr_entity_sources.CACHE_SUBDIRECTORY,
    robinhood_fund_entity_sources.CACHE_SUBDIRECTORY,
)


def seed_records(cache: Path, *names: str) -> dict[Path, bytes]:
    """Write a synthetic record into each of ``RECORD_FOLDERS`` and at each
    of ``names`` (relative to ``cache``), and return each one's bytes."""
    records = {}
    for name in (*(f"{folder}/record.json" for folder in RECORD_FOLDERS), *names):
        path = cache / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"synthetic {name}\n", encoding="utf-8")
        records[path] = path.read_bytes()
    return records
