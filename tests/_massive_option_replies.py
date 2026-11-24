"""One synthetic Massive option contract's raw replies for the Massive price tests.

The contract is a call at $250 expiring 17 Jan 2025. Its builders write the raw
contract overview and daily bars carrying privacy markers, and a correctly hashed
cache artifact. ``mr_load`` reads the series through the fetch or the cache path,
and ``assert_mr_private`` checks an error and every link of its chain for the
markers through ``assert_prints_no_marker``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.request import Request
from zoneinfo import ZoneInfo

import pytest

from ibkr_fa.prices.massive_options import load_or_fetch_massive_option_closes
from tests._crash_reports import assert_prints_no_marker

MR_EXPIRATION = date(2025, 1, 17)
MR_START = date(2025, 1, 1)
MR_END = date(2025, 1, 17)

MR_SYMBOL = "XQMR"
MR_TICKER = "O:XQMR250117C00250000"
MR_KEY = "SyntheticApiKeyThree"
MR_TEXT = "4_0739"
MR_MARKERS = (
    MR_SYMBOL,
    "SyntheticDescriptionThree",
    "SyntheticSourceThree",
    "SyntheticReasonThree",
    "2093-05-17",
    MR_TEXT,
    MR_KEY,
)
MR_MISSING = object()


def _ny_millis(day: date) -> int:
    ny = ZoneInfo("America/New_York")
    return int(datetime(day.year, day.month, day.day, tzinfo=ny).timestamp() * 1000)


MR_ROW_ONE_T = _ny_millis(date(2025, 1, 15))
MR_ROW_TWO_T = _ny_millis(date(2025, 1, 16))


class _Raw:
    """A value written into the JSON as exactly this text."""

    def __init__(self, text: str) -> None:
        self.text = text


def _dumps(payload: object) -> bytes:
    raws: list[str] = []

    def slot(value: object) -> str:
        assert isinstance(value, _Raw)
        raws.append(value.text)
        return f"<raw {len(raws) - 1}>"

    text = json.dumps(payload, default=slot)
    for index, literal in enumerate(raws):
        text = text.replace(f'"<raw {index}>"', literal)
    return text.encode()


def _set(target: dict[str, object], changes: dict[str, object]) -> None:
    """Set each field to its raw JSON text, or remove it for ``MR_MISSING``."""
    for field, literal in changes.items():
        target.pop(field, None)
        if literal is not MR_MISSING:
            target[field] = _Raw(str(literal))


def mr_overview(**changes: object) -> bytes:
    """A valid XQMR contract overview carrying the markers. Each change sets
    ``status`` or a results field to raw JSON text, or removes it."""
    result: dict[str, object] = {
        "ticker": MR_TICKER,
        "underlying_ticker": MR_SYMBOL,
        "expiration_date": "2025-01-17",
        "strike_price": 250,
        "contract_type": "call",
        "shares_per_contract": 100,
        "correction": None,
        "additional_underlyings": [],
        "description": "SyntheticDescriptionThree",
        "listed_on": "2093-05-17",
    }
    payload: dict[str, object] = {
        "status": "OK",
        "request_id": "SyntheticSourceThree",
        "reason": "SyntheticReasonThree",
        "numeric_text": MR_TEXT,
        "key_marker": MR_KEY,
        "results": result,
    }
    _set(payload, {key: value for key, value in changes.items() if key == "status"})
    _set(result, {key: value for key, value in changes.items() if key != "status"})
    return _dumps(payload)


def mr_bars(*, row_two: object = None, **changes: object) -> bytes:
    """Valid XQMR bars for Jan 15 and 16 carrying the markers. Each change sets
    a field of results row 2 to raw JSON text, or removes it; ``row_two``
    replaces that whole row with raw JSON text."""
    row_one = {"t": MR_ROW_ONE_T, "c": _Raw("1.25"), "v": 2, "n": 1, "x": "SyntheticSourceThree"}
    second: dict[str, object] = {"t": MR_ROW_TWO_T, "c": _Raw("1.5"), "v": 3, "n": 2}
    second["x"] = "SyntheticReasonThree"
    _set(second, changes)
    return _dumps(
        {
            "status": "OK",
            "ticker": MR_TICKER,
            "adjusted": False,
            "request_id": "SyntheticSourceThree",
            "description": "SyntheticDescriptionThree",
            "as_of": "2093-05-17",
            "numeric_text": MR_TEXT,
            "key_marker": MR_KEY,
            "results": [row_one, second if row_two is None else _Raw(str(row_two))],
        }
    )


def mr_artifact_bytes(
    overview: bytes | str, bars: bytes | str, *, start: date = MR_START, end: date = MR_END
) -> bytes:
    """A correctly hashed XQMR cache artifact holding these responses."""
    metadata = {
        "ticker": MR_TICKER,
        "underlying": MR_SYMBOL,
        "expiration": "2025-01-17",
        "strike_usd": "250",
        "multiplier": "100",
        "covered_start": start.isoformat(),
        "covered_end": end.isoformat(),
        "adjusted": False,
        "source": "massive-rest",
        "fetched_at": "2026-08-16T00:00:00+00:00",
    }
    body = {
        "metadata": metadata,
        "overview_raw": overview.decode() if isinstance(overview, bytes) else overview,
        "bars_raw": bars.decode() if isinstance(bars, bytes) else bars,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    body["payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    return json.dumps({"schema": 1, **body}).encode()


def mr_artifact_path(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "XQMR250117C00250000-2025-01-01-2025-01-17.json"


def no_network_opener(_request: Request, _timeout: float) -> bytes:
    raise AssertionError("cache reuse must not use the network")


def mr_load(tmp_path: Path, path: str, overview: bytes | str, bars: bytes | str):
    """Read the XQMR series through the fetch path, or from a correctly hashed
    artifact with a missing key file and no network."""
    if path == "fetch":
        assert isinstance(overview, bytes) and isinstance(bars, bytes)
        key = tmp_path / "env"
        key.write_text(f"MASSIVE_API_KEY={MR_KEY}\n", encoding="utf-8")
        responses = [overview, bars]
        opener = lambda _request, _timeout: responses.pop(0)  # noqa: E731
    else:
        key = tmp_path / "missing-env"
        opener = no_network_opener
        mr_artifact_path(tmp_path).parent.mkdir()
        mr_artifact_path(tmp_path).write_bytes(mr_artifact_bytes(overview, bars))
    return load_or_fetch_massive_option_closes(
        underlying=MR_SYMBOL,
        expiration=MR_EXPIRATION,
        strike_usd=Decimal("250"),
        right="Call",
        multiplier=Decimal("100"),
        start=MR_START,
        end=MR_END,
        cache_dir=tmp_path / "cache",
        fetch=True,
        api_key_file=key,
        opener=opener,
        now=datetime(2026, 8, 16, tzinfo=UTC),
        sleep=lambda _seconds: None,
    )


def assert_mr_private(error: BaseException) -> None:
    """No :data:`MR_MARKERS` marker anywhere in ``error`` or any link of its
    chain, suppressed or not (:func:`assert_prints_no_marker`)."""
    assert_prints_no_marker(error, markers=MR_MARKERS)


# The reader's refusal for a bar on a day already read, or with a close, volume
# or count that isn't positive.
MASSIVE_INVALID_BAR_REFUSAL = "Massive aggregate bar is invalid"
# Runs a test once through each read path of ``mr_load``.
MR_PATHS = pytest.mark.parametrize("path", ["fetch", "cache"])
