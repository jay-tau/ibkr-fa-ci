"""Tests for the close-only Massive option provider/cache."""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from http.client import IncompleteRead
from pathlib import Path
from urllib.request import Request
from zoneinfo import ZoneInfo

import pytest

from ibkr_fa.prices import massive_options
from ibkr_fa.prices.massive_options import (
    MassiveOptionPriceError,
    load_or_fetch_massive_option_closes,
    massive_option_ticker,
)
from tests._massive_option_replies import MASSIVE_INVALID_BAR_REFUSAL as _INVALID_BAR
from tests._massive_option_replies import MR_END as _END
from tests._massive_option_replies import MR_EXPIRATION as _EXPIRATION
from tests._massive_option_replies import MR_KEY as _MR_KEY
from tests._massive_option_replies import (
    MR_MARKERS,
    assert_mr_private,
    mr_artifact_bytes,
    mr_bars,
    mr_overview,
)
from tests._massive_option_replies import MR_MISSING as MISSING
from tests._massive_option_replies import MR_PATHS as _PATHS
from tests._massive_option_replies import MR_ROW_ONE_T as _ROW_ONE_T
from tests._massive_option_replies import MR_ROW_TWO_T as _ROW_TWO_T
from tests._massive_option_replies import MR_START as _START
from tests._massive_option_replies import MR_SYMBOL as _MR_SYMBOL
from tests._massive_option_replies import MR_TEXT as _MR_TEXT
from tests._massive_option_replies import MR_TICKER as _MR_TICKER
from tests._massive_option_replies import mr_artifact_path as _artifact
from tests._massive_option_replies import mr_load as _mr_load
from tests._massive_option_replies import no_network_opener as _no_network
from tests._robinhood_refusals import (
    MASSIVE_CLOSE_REFUSAL,
    MASSIVE_CORRECTION_REFUSAL,
    MASSIVE_JSON_REFUSAL,
    MASSIVE_MULTIPLIER_REFUSAL,
    MASSIVE_ROW_REFUSAL,
    MASSIVE_STATUS_REFUSAL,
    MASSIVE_STRIKE_REFUSAL,
    MASSIVE_TIMESTAMP_RANGE_REFUSAL,
    MASSIVE_TIMESTAMP_REFUSAL,
    MASSIVE_VOLUME_REFUSAL,
)
from tests._robinhood_refusals import MASSIVE_COUNT_REFUSAL as _COUNT

_TICKER = "O:XQAQ250117C00250000"


def _overview(**overrides: object) -> bytes:
    result: dict[str, object] = {
        "ticker": _TICKER,
        "underlying_ticker": "XQAQ",
        "expiration_date": "2025-01-17",
        "strike_price": 250,
        "contract_type": "call",
        "shares_per_contract": 100,
        "correction": None,
        "additional_underlyings": [],
    }
    result.update(overrides)
    return json.dumps({"status": "OK", "results": result}).encode()


def _bars(**overrides: object) -> bytes:
    timestamp = int(datetime(2025, 1, 16, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
    payload: dict[str, object] = {
        "status": "OK",
        "ticker": _TICKER,
        "adjusted": False,
        "results": [{"t": timestamp, "c": 1.25, "v": 2, "n": 1}],
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def _load(
    tmp_path: Path,
    responses: list[bytes],
):
    key_path = tmp_path / "env"
    key_path.write_text("MASSIVE_API_KEY=test-secret\n", encoding="utf-8")
    requests: list[Request] = []

    def opener(request: Request, _timeout: float) -> bytes:
        requests.append(request)
        return responses.pop(0)

    series = load_or_fetch_massive_option_closes(
        underlying="XQAQ",
        expiration=_EXPIRATION,
        strike_usd=Decimal("250"),
        right="Call",
        multiplier=Decimal("100"),
        start=_START,
        end=_END,
        cache_dir=tmp_path / "cache",
        fetch=True,
        api_key_file=key_path,
        opener=opener,
        now=datetime(2026, 8, 16, tzinfo=UTC),
        sleep=lambda _seconds: None,
    )
    return series, requests, key_path


def test_encodes_standard_call_only() -> None:
    assert (
        massive_option_ticker(
            underlying="XQAQ",
            expiration=_EXPIRATION,
            strike_usd=Decimal("250"),
            right="Call",
            multiplier=Decimal("100"),
        )
        == _TICKER
    )
    with pytest.raises(MassiveOptionPriceError):
        massive_option_ticker(
            underlying="XQDA.B",
            expiration=_EXPIRATION,
            strike_usd=Decimal("250"),
            right="Call",
            multiplier=Decimal("100"),
        )


def test_fetches_validates_and_reuses_private_cache_without_key(tmp_path: Path) -> None:
    series, requests, key_path = _load(tmp_path, [_overview(), _bars()])
    assert series is not None
    assert series.bars[date(2025, 1, 16)].close_usd == Decimal("1.25")
    assert len(requests) == 2
    assert "as_of=2025-01-17" in requests[0].full_url
    assert "adjusted=false" in requests[1].full_url
    assert requests[0].get_header("Authorization") == "Bearer test-secret"
    artifact = series.artifact_path.read_text(encoding="utf-8")
    assert "test-secret" not in artifact

    key_path.unlink()

    def no_network(_request: Request, _timeout: float) -> bytes:
        raise AssertionError("cache hit must not use network")

    cached = load_or_fetch_massive_option_closes(
        underlying="XQAQ",
        expiration=_EXPIRATION,
        strike_usd=Decimal("250"),
        right="Call",
        multiplier=Decimal("100"),
        start=_START,
        end=_END,
        cache_dir=tmp_path / "cache",
        fetch=True,
        api_key_file=None,
        opener=no_network,
    )
    assert cached is not None
    assert cached.bars == series.bars


def test_refuses_adjusted_contract_and_no_trade_bars(tmp_path: Path) -> None:
    with pytest.raises(MassiveOptionPriceError, match="identity"):
        _load(tmp_path, [_overview(additional_underlyings=[{"symbol": "XQGE"}]), _bars()])
    with pytest.raises(MassiveOptionPriceError, match="no traded days"):
        _load(tmp_path, [_overview(), _bars(results=[])])


def test_refuses_corrupt_cache_instead_of_refetching(tmp_path: Path) -> None:
    series, _requests, _key_path = _load(tmp_path, [_overview(), _bars()])
    assert series is not None
    series.artifact_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(MassiveOptionPriceError, match="cache"):
        load_or_fetch_massive_option_closes(
            underlying="XQAQ",
            expiration=_EXPIRATION,
            strike_usd=Decimal("250"),
            right="Call",
            multiplier=Decimal("100"),
            start=_START,
            end=_END,
            cache_dir=tmp_path / "cache",
            fetch=True,
            api_key_file=None,
        )


_PERMISSIONS_ENFORCED = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)


@_PERMISSIONS_ENFORCED
@pytest.mark.parametrize("fetch", [False, True], ids=["offline", "fetch-enabled"])
def test_cache_that_cannot_be_inspected_is_refused_like_an_unreadable_one(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    fetch: bool,
) -> None:
    """#1238: an artifact in a folder that can't be searched is read, and refused
    as unreadable, instead of reading as missing or being fetched over."""
    _series, _requests, key_path = _load(tmp_path, [_overview(), _bars()])
    cache_dir = tmp_path / "cache"
    path = cache_dir / "XQAQ250117C00250000-2025-01-01-2025-01-17.json"
    cached = {entry.name: entry.read_bytes() for entry in cache_dir.iterdir()}
    assert list(cached) == [path.name]
    requests: list[Request] = []
    responses = [_overview(), _bars()]

    def opener(sent: Request, _timeout: float) -> bytes:
        requests.append(sent)
        return responses.pop(0)

    cache_dir.chmod(0)
    request.addfinalizer(lambda: cache_dir.chmod(0o700))
    with pytest.raises(MassiveOptionPriceError) as raised:
        load_or_fetch_massive_option_closes(
            underlying="XQAQ",
            expiration=_EXPIRATION,
            strike_usd=Decimal("250"),
            right="Call",
            multiplier=Decimal("100"),
            start=_START,
            end=_END,
            cache_dir=cache_dir,
            fetch=fetch,
            api_key_file=key_path,
            opener=opener,
            sleep=lambda _seconds: None,
        )

    assert type(raised.value) is MassiveOptionPriceError
    assert str(raised.value) == "Massive option cache is malformed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert requests == []
    cache_dir.chmod(0o700)
    assert {entry.name: entry.read_bytes() for entry in cache_dir.iterdir()} == cached


def test_normalizes_transport_and_key_decode_failures(tmp_path: Path) -> None:
    bad_key = tmp_path / "bad-env"
    bad_key.write_bytes(b"MASSIVE_API_KEY=bad" + bytes([255]))
    with pytest.raises(MassiveOptionPriceError, match="key file"):
        load_or_fetch_massive_option_closes(
            underlying="XQAQ",
            expiration=_EXPIRATION,
            strike_usd=Decimal("250"),
            right="Call",
            multiplier=Decimal("100"),
            start=_START,
            end=_END,
            cache_dir=tmp_path / "cache",
            fetch=True,
            api_key_file=bad_key,
        )

    key = tmp_path / "env"
    key.write_text("MASSIVE_API_KEY=test\n", encoding="utf-8")

    def truncated(_request: Request, _timeout: float) -> bytes:
        raise IncompleteRead(b"")

    with pytest.raises(MassiveOptionPriceError, match="request failed"):
        load_or_fetch_massive_option_closes(
            underlying="XQAQ",
            expiration=_EXPIRATION,
            strike_usd=Decimal("250"),
            right="Call",
            multiplier=Decimal("100"),
            start=_START,
            end=_END,
            cache_dir=tmp_path / "other-cache",
            fetch=True,
            api_key_file=key,
            opener=truncated,
            sleep=lambda _seconds: None,
        )


# --- #1169: field formats are checked before conversion ----------------------
#
# Each response carries distinct privacy markers: a refusal names fixed fields
# only, never a value, symbol, date, key or artifact name.


def _refuses(
    tmp_path: Path, path: str, overview: bytes | str, bars: bytes | str, message: str
) -> None:
    """The read refuses with exactly ``message`` and no chain. A refused fetch
    writes no artifact; a refused artifact stays byte-identical, and neither
    the key file nor the network is read (either would raise another error)."""
    with pytest.raises(MassiveOptionPriceError) as raised:
        _mr_load(tmp_path, path, overview, bars)
    assert str(raised.value) == message
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert_mr_private(raised.value)
    if path == "fetch":
        assert not (tmp_path / "cache").exists()
    else:
        assert _artifact(tmp_path).read_bytes() == mr_artifact_bytes(overview, bars)


_CLOSE = MASSIVE_CLOSE_REFUSAL
_VOLUME = MASSIVE_VOLUME_REFUSAL
_TIMESTAMP = MASSIVE_TIMESTAMP_REFUSAL
_TIMESTAMP_RANGE = MASSIVE_TIMESTAMP_RANGE_REFUSAL
_ROW_SHAPE = MASSIVE_ROW_REFUSAL
_STATUS = MASSIVE_STATUS_REFUSAL
_STRIKE = MASSIVE_STRIKE_REFUSAL
_MULTIPLIER = MASSIVE_MULTIPLIER_REFUSAL
_CORRECTION = MASSIVE_CORRECTION_REFUSAL
_IDENTITY = "Massive contract identity does not match the request"

# Row-2 field cases: each value is raw JSON text, or MISSING.
AGGREGATE_FIELD_CASES = [
    ("c", '"1_2"', _CLOSE),
    ("c", json.dumps(_MR_TEXT), _CLOSE),
    ("c", '"1.5"', _CLOSE),
    ("c", "true", _CLOSE),
    ("c", "null", _CLOSE),
    ("c", "[1.5]", _CLOSE),
    ("c", "NaN", _CLOSE),
    ("c", "Infinity", _CLOSE),
    ("c", "-Infinity", _CLOSE),
    ("c", MISSING, _CLOSE),
    ("v", '"2_0"', _VOLUME),
    ("v", '"3"', _VOLUME),
    ("v", "false", _VOLUME),
    ("v", "NaN", _VOLUME),
    ("v", "Infinity", _VOLUME),
    ("v", MISSING, _VOLUME),
    ("n", '"2"', _COUNT),
    ("n", json.dumps(_MR_TEXT), _COUNT),
    ("n", "1.99", _COUNT),
    ("n", "2.5", _COUNT),
    ("n", "true", _COUNT),
    ("n", "false", _COUNT),
    ("n", "NaN", _COUNT),
    ("n", "Infinity", _COUNT),
    ("n", "-Infinity", _COUNT),
    ("n", MISSING, _COUNT),
    ("t", json.dumps(str(_ROW_TWO_T)), _TIMESTAMP),
    ("t", f"{_ROW_TWO_T}.5", _TIMESTAMP),
    ("t", f"{_ROW_TWO_T}.001", _TIMESTAMP),
    ("t", "true", _TIMESTAMP),
    ("t", "false", _TIMESTAMP),
    ("t", "NaN", _TIMESTAMP),
    ("t", "Infinity", _TIMESTAMP),
    ("t", "-Infinity", _TIMESTAMP),
    ("t", MISSING, _TIMESTAMP),
    ("t", str(10**20), _TIMESTAMP_RANGE),
    ("t", str(-(10**20)), _TIMESTAMP_RANGE),
    ("t", "9" * 400, _TIMESTAMP_RANGE),
    ("t", "1E+300", _TIMESTAMP_RANGE),
    ("t", "1E+999999", _TIMESTAMP_RANGE),
    # Midnight in New York starting the year 10000.
    ("t", "253402318800000", _TIMESTAMP_RANGE),
]

# Overview cases: ``status`` or a results field, as raw JSON text or MISSING.
OVERVIEW_FIELD_CASES = [
    ("status", MISSING, _STATUS),
    ("status", '"ERROR"', _STATUS),
    ("status", '"ok"', _STATUS),
    ("status", "null", _STATUS),
    ("status", "true", _STATUS),
    ("strike_price", '"2_50"', _STRIKE),
    ("strike_price", '"250"', _STRIKE),
    ("strike_price", json.dumps(_MR_TEXT), _STRIKE),
    ("strike_price", "true", _STRIKE),
    ("strike_price", "null", _STRIKE),
    ("strike_price", "NaN", _STRIKE),
    ("strike_price", "Infinity", _STRIKE),
    ("strike_price", MISSING, _STRIKE),
    ("shares_per_contract", '"1_00"', _MULTIPLIER),
    ("shares_per_contract", '"100"', _MULTIPLIER),
    ("shares_per_contract", "true", _MULTIPLIER),
    ("shares_per_contract", "NaN", _MULTIPLIER),
    ("shares_per_contract", "Infinity", _MULTIPLIER),
    ("shares_per_contract", "-Infinity", _MULTIPLIER),
    ("shares_per_contract", MISSING, _MULTIPLIER),
    ("correction", "false", _CORRECTION),
    ("correction", "true", _CORRECTION),
    ("correction", '"0"', _CORRECTION),
    ("correction", json.dumps(_MR_TEXT), _CORRECTION),
    ("correction", "1", _CORRECTION),
    ("correction", "-1", _CORRECTION),
    ("correction", "0.5", _CORRECTION),
    ("correction", "NaN", _CORRECTION),
    ("correction", "Infinity", _CORRECTION),
    ("correction", "[]", _CORRECTION),
]


@_PATHS
@pytest.mark.parametrize(("field", "literal", "message"), AGGREGATE_FIELD_CASES)
def test_aggregate_field_refuses_before_conversion(
    tmp_path: Path, path: str, field: str, literal: object, message: str
) -> None:
    """Row 2 follows a valid row 1. On main, numeric text such as ``"1_2"``
    became 12, a fractional count was truncated and a large timestamp raised
    a raw ``OverflowError``."""
    _refuses(tmp_path, path, mr_overview(), mr_bars(**{field: literal}), message)


@_PATHS
@pytest.mark.parametrize("row", ["[]", "null", "5", json.dumps(_MR_TEXT)])
def test_aggregate_row_that_is_not_an_object_refuses(tmp_path: Path, path: str, row: str) -> None:
    _refuses(tmp_path, path, mr_overview(), mr_bars(row_two=row), _ROW_SHAPE)


@_PATHS
@pytest.mark.parametrize(("field", "literal", "message"), OVERVIEW_FIELD_CASES)
def test_overview_field_refuses_before_identity(
    tmp_path: Path, path: str, field: str, literal: object, message: str
) -> None:
    """On main, a ``false`` correction passed as zero and numeric text such as
    ``"2_50"`` became 250."""
    _refuses(tmp_path, path, mr_overview(**{field: literal}), mr_bars(), message)


@_PATHS
def test_error_status_cannot_establish_identity(tmp_path: Path, path: str) -> None:
    """Main accepted this envelope: its strike and multiplier matched once
    ``Decimal`` dropped their underscores. Its status now refuses first."""
    overview = mr_overview(status='"ERROR"', strike_price='"2_50"', shares_per_contract='"1_00"')
    _refuses(tmp_path, path, overview, mr_bars(), _STATUS)


@_PATHS
@pytest.mark.parametrize("literal", [MISSING, "null", "0", "-0", "0.0", "0E+2"])
def test_absent_null_or_numeric_zero_correction_passes(
    tmp_path: Path, path: str, literal: object
) -> None:
    series = _mr_load(tmp_path, path, mr_overview(correction=literal), mr_bars())
    assert series is not None
    assert set(series.bars) == {date(2025, 1, 15), date(2025, 1, 16)}


@_PATHS
@pytest.mark.parametrize(
    ("field", "literal", "attribute", "expected"),
    [
        ("c", "2", "close_usd", "2"),
        ("c", "1.375", "close_usd", "1.375"),
        ("c", "1.50", "close_usd", "1.50"),
        ("v", "3", "volume", "3"),
        ("v", "3.25", "volume", "3.25"),
        ("n", "3.0", "transactions", "3"),
        ("n", "3E+1", "transactions", "30"),
        ("t", f"{_ROW_TWO_T}.000", "day", "2025-01-16"),
        ("t", f"{_ROW_TWO_T // 1000}E+3", "day", "2025-01-16"),
    ],
)
def test_native_numbers_keep_their_exact_values(
    tmp_path: Path, path: str, field: str, literal: str, attribute: str, expected: str
) -> None:
    series = _mr_load(tmp_path, path, mr_overview(), mr_bars(**{field: literal}))
    assert series is not None
    (bar,) = (bar for day, bar in series.bars.items() if day != date(2025, 1, 15))
    assert str(getattr(bar, attribute)) == expected


@_PATHS
@pytest.mark.parametrize("strike", ["250", "250.0", "2.5E+2"])
def test_native_strike_numbers_pass(tmp_path: Path, path: str, strike: str) -> None:
    series = _mr_load(tmp_path, path, mr_overview(strike_price=strike), mr_bars())
    assert series is not None


def test_native_fractional_strike_keeps_its_exact_value_on_fetch_and_reuse(tmp_path: Path) -> None:
    ticker = "O:XQMR250117C00250125"
    overview = mr_overview(strike_price="250.125").replace(_MR_TICKER.encode(), ticker.encode())
    bars = mr_bars().replace(_MR_TICKER.encode(), ticker.encode())
    key = tmp_path / "env"
    key.write_text(f"MASSIVE_API_KEY={_MR_KEY}\n", encoding="utf-8")
    responses = [overview, bars]
    load = partial(
        load_or_fetch_massive_option_closes,
        underlying=_MR_SYMBOL,
        expiration=_EXPIRATION,
        strike_usd=Decimal("250.125"),
        right="Call",
        multiplier=Decimal("100"),
        start=_START,
        end=_END,
        cache_dir=tmp_path / "cache",
    )
    series = load(
        fetch=True,
        api_key_file=key,
        opener=lambda _request, _timeout: responses.pop(0),
        sleep=lambda _seconds: None,
    )
    assert series is not None
    assert series.strike_usd == Decimal("250.125")
    assert series.massive_ticker == ticker
    before = series.artifact_path.read_bytes()
    cached = load(fetch=False, api_key_file=None, opener=_no_network)
    assert cached is not None
    assert cached.strike_usd == Decimal("250.125")
    assert cached.massive_ticker == ticker
    assert cached.bars == series.bars
    assert cached.artifact_path.read_bytes() == before


@_PATHS
@pytest.mark.parametrize(
    ("overview", "bars", "message"),
    [
        pytest.param(mr_overview(shares_per_contract="10"), mr_bars(), _IDENTITY, id="multiplier"),
        pytest.param(
            mr_overview(strike_price="250.0000000000000000000000000001"),
            mr_bars(),
            _IDENTITY,
            id="strike-exact",
        ),
        pytest.param(
            mr_overview(additional_underlyings='[{"symbol": "XQGE"}]'),
            mr_bars(),
            _IDENTITY,
            id="adjusted-contract",
        ),
        pytest.param(mr_overview(), mr_bars(c="0"), _INVALID_BAR, id="zero-close"),
        pytest.param(mr_overview(), mr_bars(v="-1"), _INVALID_BAR, id="negative-volume"),
        pytest.param(mr_overview(), mr_bars(n="0"), _INVALID_BAR, id="zero-count"),
        pytest.param(
            mr_overview(), mr_bars(t=str(_ROW_ONE_T + 3_600_000)), _INVALID_BAR, id="same-day"
        ),
        pytest.param(
            mr_overview(),
            mr_bars().replace(b'"adjusted": false', b'"adjusted": false, "next_url": "x"'),
            "Massive aggregate response metadata is invalid",
            id="pagination",
        ),
    ],
)
def test_existing_refusals_remain(
    tmp_path: Path, path: str, overview: bytes, bars: bytes, message: str
) -> None:
    _refuses(tmp_path, path, overview, bars, message)


def test_injected_timestamp_conversion_error_is_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conversion error carrying every marker never rides along in the chain."""

    class Exploding(datetime):
        @classmethod
        def fromtimestamp(cls, *_args: object, **_kwargs: object) -> datetime:
            raise OverflowError(" ".join(MR_MARKERS))

    monkeypatch.setattr(massive_options, "datetime", Exploding)
    _refuses(
        tmp_path,
        "fetch",
        mr_overview(),
        mr_bars(),
        "Massive aggregate response, results row 1, column t: the timestamp must resolve to a "
        "representable New York date",
    )


def test_malformed_overview_date_keeps_no_input_bearing_chain(tmp_path: Path) -> None:
    """``date.fromisoformat``'s error quotes its input; the refusal drops it."""
    expiration = json.dumps("SyntheticDescriptionThree 2093-05-17")
    overview = mr_overview(expiration_date=expiration)
    _refuses(tmp_path, "fetch", overview, mr_bars(), "Massive contract response is malformed")


@_PATHS
def test_number_beyond_decimal_exponent_range_is_malformed_json(tmp_path: Path, path: str) -> None:
    """``Decimal`` raises ``InvalidOperation``, not ``ValueError``, for such an
    exponent: it is refused as malformed JSON, never raised raw."""
    bars = mr_bars(c="1e9999999999999999999")
    _refuses(tmp_path, path, mr_overview(), bars, MASSIVE_JSON_REFUSAL)


@_PATHS
@pytest.mark.parametrize("response", ["overview", "bars"])
def test_malformed_json_keeps_no_input_bearing_chain(
    tmp_path: Path, path: str, response: str
) -> None:
    overview, bars = mr_overview(), mr_bars()
    if response == "overview":
        overview = overview[:-1]
    else:
        bars = bars[:-1]
    _refuses(tmp_path, path, overview, bars, MASSIVE_JSON_REFUSAL)


@pytest.mark.parametrize("response", ["overview", "bars"])
@pytest.mark.parametrize("encoding", ["invalid-utf8", "utf-16", "utf-32"])
def test_undecodable_response_bytes_refuse_before_artifact_creation(
    tmp_path: Path, response: str, encoding: str
) -> None:
    overview, bars = mr_overview(), mr_bars()
    raw = overview if response == "overview" else bars
    bad = raw + b"\xff" if encoding == "invalid-utf8" else raw.decode().encode(encoding)
    _refuses(
        tmp_path,
        "fetch",
        bad if response == "overview" else overview,
        bad if response == "bars" else bars,
        MASSIVE_JSON_REFUSAL,
    )


@pytest.mark.parametrize("response", ["overview", "bars"])
def test_cached_response_text_keeps_no_encoding_error_chain(tmp_path: Path, response: str) -> None:
    overview, bars = mr_overview().decode(), mr_bars().decode()
    if response == "overview":
        overview = overview.replace("SyntheticDescriptionThree", "SyntheticDescriptionThree\ud800")
    else:
        bars = bars.replace("SyntheticDescriptionThree", "SyntheticDescriptionThree\ud800")
    _refuses(tmp_path, "cache", overview, bars, MASSIVE_JSON_REFUSAL)


@_PATHS
def test_utf8_bom_responses_still_pass(tmp_path: Path, path: str) -> None:
    series = _mr_load(tmp_path, path, b"\xef\xbb\xbf" + mr_overview(), b"\xef\xbb\xbf" + mr_bars())
    assert series is not None
    assert series.bars[date(2025, 1, 16)].close_usd == Decimal("1.5")
    assert series.bars[date(2025, 1, 16)].transactions == 2
