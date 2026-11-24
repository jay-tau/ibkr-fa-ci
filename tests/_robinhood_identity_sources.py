"""Synthetic official identity sources (the Nasdaq Trader traded-symbol
directory, the SEC ticker and fund datasets and SEC submission records), the
identity-source loader and the snapshot cache helpers, shared by the identity
source tests and the Robinhood bundle, entity, refusal-chain and remedy tests
(#1804).

:func:`source_responses` serves every source by URL; :func:`load_snapshot`
loads them with caller-controlled transport and cache settings.
:func:`snapshot_refusal` writes the whole snapshot-search refusal for one
problem text.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa.models.reference_identity import SEC_SUBMISSION_URL_TEMPLATE, ObservedIdentityInput
from ibkr_fa.robinhood_identity_sources import (
    NASDAQ_TRADED_URL,
    SEC_COMPANY_TICKERS_EXCHANGE_URL,
    SEC_COMPANY_TICKERS_MF_URL,
    IdentitySourceOpener,
    identity_input_fingerprint,
    load_identity_sources,
    load_post_fy_identity_sources,
)
from tests._entity_cache import reseal as _reseal
from tests._entity_cache_remedies import MOVE_ASIDE, MOVE_ASIDE_SET_CONTACT, SET_IDENTITY_TOO
from tests._entity_transport import RecordingOpener as _RecordingOpener

# The official Nasdaq Trader field contract, pinned here independently of
# the module constant so a drifting header fails a test, not a user run.
NASDAQ_HEADER = (
    "Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|"
    "Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares"
)
NASDAQ_TRAILER = "File Creation Time: 0816202516:59|||||||||||"
NASDAQ_ROWS = (
    "Y|XQAQ|Sample Issuer AQ Inc. - Common Stock|Q|Q|N|100|N|N|XQAQ|XQAQ|N",
    "Y|XQAJ|Sample Fund AJ ETF|P| |Y|100|N| |XQAJ|XQAJ|N",
    "Y|ZZZZ|Test Issue Corp - Common Stock|Q|Q|N|100|Y|N|ZZZZ|ZZZZ| ",
)

TICKER_ROWS: tuple[list[object], ...] = (
    [9991029, "Sample Issuer AQ Inc.", "XQAQ", "Nasdaq"],
    [9991050, "Other Corp", "ZZZZ", None],
    [9990001, "Sample Issuer AA Inc.", "XQAA", "Nasdaq"],
)
_FUND_ROWS: tuple[list[object], ...] = (
    [9991004, "S000999904", "C000999941", "XQAJ"],
    [9991004, "S000999904", "C000999942", ""],
    [9991004, "S000999904", "C000999943", "XQAIX"],
)

ISSUER_AQ_SUBMISSION = {
    "cik": "9991029",
    "name": "Sample Issuer AQ Inc.",
    "stateOfIncorporation": "CA",
    "addresses": {
        "business": {
            "street1": "1 Issuer Way",
            "street2": None,
            "city": "Sampletown",
            "stateOrCountry": "CA",
            "zipCode": "99990",
        }
    },
    "filings": {
        "recent": {"form": ["10-K", "8-K", "10-Q", "8-K"]},
        "files": [{"name": "CIK0009991029-submissions-001.json", "filingCount": 1000}],
    },
}
# A synthetic fund filer. Its CIK sorts before XQAQ's: submissions are
# requested and stored in CIK order, so the identity source tests expect its
# request first and find XQAQ at ``sec_submissions[1]``.
_SAMPLE_FUNDS = {
    "cik": 9991004,
    "name": "SAMPLE FUNDS AJ",
    "stateOfIncorporation": "DE",
    "addresses": {
        "business": {
            "street1": "100 Sample Boulevard",
            "street2": None,
            "city": "Sampleville",
            "stateOrCountry": "PA",
            "zipCode": "19990",
        }
    },
    "filings": {
        "recent": {"form": ["N-CSR", "497"]},
        # Older filings live alongside ``recent``; scalar members (and the
        # ``files`` index) carry no forms and must be ignored, not fatal.
        "older": {"form": ["485BPOS"]},
        "note": "form index",
    },
}

SEC_CONTACT = "ibkr-fa filer taxpayer@example.com"
SNAPSHOT_NOW = datetime(2026, 8, 17, tzinfo=UTC)
ISSUER_AQ_CUSIP = "99992A109"
IDENTITY_INPUTS = (
    ObservedIdentityInput(
        cusip=ISSUER_AQ_CUSIP, symbol="XQAQ", description="SAMPLE ISSUER AQ INC COMMON STOCK"
    ),
    ObservedIdentityInput(cusip="99990D105", symbol="XQAJ", description="SAMPLE FUND AJ ETF"),
)
MANUAL_IDENTITIES: Mapping[str, Mapping[str, str]] = {
    "XQAQ": {"cusip": ISSUER_AQ_CUSIP, "isin": "US99992A1097", "listing_exchange": "NASDAQ"}
}


def nasdaq_bytes(
    rows: Sequence[str] = NASDAQ_ROWS,
    *,
    header: str = NASDAQ_HEADER,
    trailer: str | None = NASDAQ_TRAILER,
) -> bytes:
    lines = [header, *rows]
    if trailer is not None:
        lines.append(trailer)
    return ("\r\n".join(lines) + "\r\n").encode()


def _sec_dataset(fields: Sequence[str], rows: Sequence[object]) -> bytes:
    return json.dumps({"fields": list(fields), "data": list(rows)}).encode()


def tickers_bytes(
    rows: Sequence[object] = TICKER_ROWS,
    fields: Sequence[str] = ("cik", "name", "ticker", "exchange"),
) -> bytes:
    return _sec_dataset(fields, rows)


def funds_bytes(
    rows: Sequence[object] = _FUND_ROWS,
    fields: Sequence[str] = ("cik", "seriesId", "classId", "symbol"),
) -> bytes:
    return _sec_dataset(fields, rows)


def source_responses(overrides: Mapping[str, bytes] | None = None) -> dict[str, bytes]:
    responses = {
        NASDAQ_TRADED_URL: nasdaq_bytes(),
        SEC_COMPANY_TICKERS_EXCHANGE_URL: tickers_bytes(),
        SEC_COMPANY_TICKERS_MF_URL: funds_bytes(),
        SEC_SUBMISSION_URL_TEMPLATE.format(cik=9991029): json.dumps(ISSUER_AQ_SUBMISSION).encode(),
        SEC_SUBMISSION_URL_TEMPLATE.format(cik=9991004): json.dumps(_SAMPLE_FUNDS).encode(),
        SEC_SUBMISSION_URL_TEMPLATE.format(cik=9991050): json.dumps(
            {
                "cik": 9991050,
                "name": "Other Corp",
                "stateOfIncorporation": "DE",
                "filings": {"recent": {"form": ["10-K"]}},
            }
        ).encode(),
        SEC_SUBMISSION_URL_TEMPLATE.format(cik=9990001): json.dumps(
            {
                "cik": 9990001,
                "name": "Sample Issuer AA Inc.",
                "stateOfIncorporation": "DE",
                "filings": {"recent": {"form": ["10-K"]}},
            }
        ).encode(),
    }
    responses.update(overrides or {})
    return responses


def no_network(url: str) -> bytes:
    raise AssertionError(f"no transport call is allowed here: {url}")


def load_snapshot(
    cache_dir: Path,
    *,
    responses: Mapping[str, bytes] | None = None,
    inputs: Sequence[ObservedIdentityInput] = IDENTITY_INPUTS,
    manual: Mapping[str, Mapping[str, str]] = MANUAL_IDENTITIES,
    fetch: bool = True,
    sec_user_agent: str | None = SEC_CONTACT,
    opener: IdentitySourceOpener | None = None,
    now: datetime = SNAPSHOT_NOW,
):
    transport: IdentitySourceOpener = (
        opener if opener is not None else _RecordingOpener(source_responses(responses))
    )
    bundle, audit = load_identity_sources(
        inputs,
        manual,
        cache_dir=cache_dir,
        fetch=fetch,
        sec_user_agent=sec_user_agent,
        opener=transport,
        now=now,
    )
    return bundle, audit, transport


def read_artifact(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def rewrite_artifact(path: Path, artifact) -> None:
    path.write_text(json.dumps(artifact, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def legacy_snapshot(cache_dir: Path) -> Path:
    """Replace the current snapshot with a verified schema-v1 one for the same inputs."""
    _bundle, current_audit, _opener = load_snapshot(cache_dir)
    assert current_audit.artifact_path is not None
    artifact = read_artifact(current_audit.artifact_path)
    for row in artifact["sec_submissions"]:
        row.pop("business_address")
    legacy_fingerprint = identity_input_fingerprint(IDENTITY_INPUTS, MANUAL_IDENTITIES, 1)
    artifact.update({"schema": 1, "fingerprint": legacy_fingerprint})
    _reseal(artifact)
    legacy_path = cache_dir / "robinhood-identities" / legacy_fingerprint / "sources.json"
    legacy_path.parent.mkdir(parents=True)
    rewrite_artifact(legacy_path, artifact)
    current_audit.artifact_path.unlink()
    current_audit.artifact_path.parent.rmdir()
    return legacy_path


# Problems the post-FY snapshot search names (#853).
IDENTITY_CACHE_INTEGRITY = "the identity source cache artifact failed its integrity check"
IDENTITY_CACHE_UNREADABLE = "the identity source cache artifact is unreadable"
IDENTITY_CACHE_NON_FINITE = "the identity source cache artifact contains a non-finite number"
IDENTITY_CACHE_INVALID_RECORD = "the identity source cache artifact contains an invalid record"

_XQAA = ObservedIdentityInput(cusip="99990A309", symbol="XQAA", description="SAMPLE ISSUER AA INC")


def snapshot_refusal(
    cache_dir: Path, problem: str, *, fetch: bool, set_contact: bool = False
) -> str:
    """The whole snapshot-search refusal, so it can't also carry a cached value.

    It names the cache folder, never an entry found in it (#1071).
    ``set_contact``: the run had no usable SEC contact.
    """
    move_aside = (MOVE_ASIDE_SET_CONTACT if set_contact else MOVE_ASIDE)[fetch]
    return (
        f"SEC identity snapshot cache is unusable ({problem}): "
        f"{cache_dir / 'robinhood-identities'}. {move_aside}, or review the legal "
        "entities yourself and, to skip the lookup, set prefer_configured_address, "
        "prefer_configured_name and prefer_configured_nature in the [[fa.entities]] entry of "
        "each held US common stock that GLEIF doesn't resolve, and add an [[fa.entities]] entry "
        f"for each held ETF. {SET_IDENTITY_TOO}"
    )


def load_post_fy_snapshot(cache_dir: Path, *, fetch: bool = False):
    return load_post_fy_identity_sources(
        {"XQAQ"},
        cache_dir=cache_dir,
        financial_year_end=date(2026, 3, 31),
        fetch=fetch,
        sec_user_agent=SEC_CONTACT,
    )


def cache_state(cache_dir: Path) -> dict[str, bytes | str | None]:
    """Every cache entry: a link's target, a file's bytes, or ``None`` for a folder."""
    state: dict[str, bytes | str | None] = {}
    for folder, dirs, files in os.walk(cache_dir):
        for name in (*dirs, *files):
            path = Path(folder, name)
            key = str(path.relative_to(cache_dir))
            if path.is_symlink():
                state[key] = str(path.readlink())
            else:
                state[key] = path.read_bytes() if path.is_file() else None
    return state


def covering_snapshot(cache_dir: Path) -> Path:
    _bundle, audit, _opener = load_snapshot(cache_dir)
    assert audit.artifact_path is not None
    return audit.artifact_path


def non_covering_snapshot(cache_dir: Path) -> Path:
    _bundle, audit, _opener = load_snapshot(cache_dir, inputs=(_XQAA,), manual={})
    assert audit.artifact_path is not None
    return audit.artifact_path


def _beside_covering(cache_dir: Path) -> Path:
    covering_snapshot(cache_dir)
    return non_covering_snapshot(cache_dir)


SNAPSHOTS = [
    pytest.param(covering_snapshot, id="covering"),
    pytest.param(non_covering_snapshot, id="non-covering"),
    pytest.param(_beside_covering, id="non-covering-beside-covering"),
    pytest.param(legacy_snapshot, id="schema-1"),
]


def tamper_snapshot(path: Path) -> None:
    artifact = read_artifact(path)
    artifact["fetched_at"] = "2026-08-18T00:00:00+00:00"
    rewrite_artifact(path, artifact)


def resealed(mutate: Callable[[dict[str, Any]], object]) -> Callable[[Path], None]:
    def damage(path: Path) -> None:
        artifact = read_artifact(path)
        mutate(artifact)
        _reseal(artifact)
        rewrite_artifact(path, artifact)

    return damage
