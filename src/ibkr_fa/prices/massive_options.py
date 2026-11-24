"""Fetch and cache Massive daily closes for standard U.S. option contracts."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from ibkr_fa.exceptions import IbkrFaError, raise_replacing
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.path_checks import may_exist
from ibkr_fa.prices.series import MassiveOptionCloseSeries, OptionCloseBar
from ibkr_fa.types import STANDARD_OPTION_MULTIPLIER

_API_ROOT: Final[str] = "https://api.massive.com"
_SCHEMA: Final[int] = 1
_NY: Final[ZoneInfo] = ZoneInfo("America/New_York")
_TIMEOUT_SECONDS: Final[float] = 30
_REQUEST_INTERVAL_SECONDS: Final[float] = 12.1
_HTTP_TOO_MANY_REQUESTS: Final[int] = 429
_MAX_OSI_ROOT_LENGTH: Final[int] = 6
_MAX_OSI_STRIKE_MILLIS: Final[int] = 99_999_999

UrlOpener = Callable[[urllib.request.Request, float], bytes]


class MassiveOptionPriceError(IbkrFaError):
    """Massive option-price evidence is unavailable or invalid.

    The request, artifact and key-file refusal handlers covered by #1225 keep
    none of the error they replace: not the contract-bearing request URL, a
    reason, headers or partial bytes, the
    contract- and date-derived artifact or staging path, the key file's or
    artifact's bytes, a JSON document or a cached timestamp. These refusals are
    raised through :func:`~ibkr_fa.exceptions.raise_replacing`, so each has no
    context, suppressed or not, and as cause at most that function's content-free
    stand-in (#1225).
    """


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _default_url_opener(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
        if urllib.parse.urlsplit(response.geturl()).hostname != "api.massive.com":
            raise MassiveOptionPriceError("Massive response redirected to an unexpected host")
        return response.read()


def massive_option_ticker(
    *,
    underlying: str,
    expiration: date,
    strike_usd: Decimal,
    right: str,
    multiplier: Decimal,
) -> str:
    if (
        not underlying.isalpha()
        or not underlying.isascii()
        or underlying != underlying.upper()
        or len(underlying) > _MAX_OSI_ROOT_LENGTH
        or right != "Call"
        or multiplier != STANDARD_OPTION_MULTIPLIER
    ):
        raise MassiveOptionPriceError("option contract is outside the standard Call scope")
    strike_millis = strike_usd * 1000
    if (
        strike_millis != strike_millis.to_integral_value()
        or not 0 <= strike_millis <= _MAX_OSI_STRIKE_MILLIS
    ):
        raise MassiveOptionPriceError("option strike cannot be encoded as an OSI strike")
    return f"O:{underlying}{expiration:%y%m%d}C{int(strike_millis):08d}"


def _request_bytes(
    *,
    path: str,
    query: dict[str, str],
    api_key: str,
    opener: UrlOpener,
) -> bytes:
    url = f"{_API_ROOT}{path}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "ibkr-fa-private-option-close",
        },
    )
    try:
        return opener(request, _TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        if exc.code == _HTTP_TOO_MANY_REQUESTS:
            raise_replacing(MassiveOptionPriceError("Massive rate limit reached; rerun later"), exc)
        raise_replacing(
            MassiveOptionPriceError(f"Massive request failed with HTTP {exc.code}"), exc
        )
    except (http.client.HTTPException, OSError, TimeoutError) as exc:
        raise_replacing(MassiveOptionPriceError("Massive request failed"), exc)


def _json(raw: bytes | str) -> dict[str, Any]:
    try:
        if isinstance(raw, str):
            raw = raw.encode()
        # The artifact writer must be able to preserve these response bytes.
        raw.decode()
        payload = json.loads(raw, parse_float=Decimal)
    # InvalidOperation: a number whose exponent Decimal can't hold.
    except (InvalidOperation, UnicodeError, ValueError):
        pass
    else:
        if not isinstance(payload, dict):
            raise MassiveOptionPriceError("Massive response is not a JSON object")
        return payload
    raise MassiveOptionPriceError("Massive returned malformed JSON")


def _finite_number(value: object) -> Decimal | None:
    """``value`` exactly, when it is a finite JSON number as :func:`_json` reads
    one (an ``int`` or ``Decimal``, never a ``bool``); otherwise ``None``.

    ``NaN`` and ``Infinity`` read as floats, and numeric strings aren't
    numbers (#1169)."""
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        return None
    number = Decimal(value)
    return number if number.is_finite() else None


def _integral_number(value: object) -> Decimal | None:
    """``value`` when it is a finite, exactly integral JSON number; never truncated."""
    number = _finite_number(value)
    return number if number is not None and number == number.to_integral_value() else None


def _validate_identity(
    payload: dict[str, Any],
    *,
    ticker: str,
    underlying: str,
    expiration: date,
    strike_usd: Decimal,
    multiplier: Decimal,
) -> None:
    # Each refusal is raised outside any handler, so no input-bearing
    # conversion error rides along as its context (#1169).
    if payload.get("status") != "OK":
        raise MassiveOptionPriceError(
            "Massive contract response, field status: the status must be OK"
        )
    result = payload.get("results")
    if not isinstance(result, dict):
        raise MassiveOptionPriceError("Massive contract response has no result")
    strike = _finite_number(result.get("strike_price"))
    if strike is None:
        raise MassiveOptionPriceError(
            "Massive contract response, results field strike_price: "
            "the strike must be a finite JSON number"
        )
    shares = _finite_number(result.get("shares_per_contract"))
    if shares is None:
        raise MassiveOptionPriceError(
            "Massive contract response, results field shares_per_contract: "
            "the multiplier must be a finite JSON number"
        )
    correction = result.get("correction")
    if correction is not None and _finite_number(correction) != 0:
        raise MassiveOptionPriceError(
            "Massive contract response, results field correction: the correction must be "
            "absent, null or finite numeric zero, excluding booleans"
        )
    try:
        valid = (
            result["ticker"] == ticker
            and result["underlying_ticker"] == underlying
            and date.fromisoformat(result["expiration_date"]) == expiration
            and strike == strike_usd
            and result["contract_type"] == "call"
            and shares == multiplier
            and not result.get("additional_underlyings")
        )
    except (KeyError, TypeError, ValueError):
        valid = None
    if valid is None:
        raise MassiveOptionPriceError("Massive contract response is malformed")
    if not valid:
        raise MassiveOptionPriceError("Massive contract identity does not match the request")


def _new_york_date(milliseconds: Decimal) -> date | None:
    """The New York date of an integral millisecond timestamp, or ``None`` when
    it has none. Through ``float``, as before: its rounding keeps a
    representable timestamp inside its millisecond, so the date is exact."""
    try:
        return datetime.fromtimestamp(float(milliseconds) / 1000, _NY).date()
    except (OverflowError, OSError, ValueError):
        return None


def _parse_bars(payload: dict[str, Any], *, ticker: str) -> dict[date, OptionCloseBar]:
    if (
        payload.get("status") != "OK"
        or payload.get("ticker") != ticker
        or payload.get("adjusted") is not False
        or payload.get("next_url") is not None
    ):
        raise MassiveOptionPriceError("Massive aggregate response metadata is invalid")
    rows: list[Any] | None = payload.get("results")
    if not isinstance(rows, list):
        raise MassiveOptionPriceError("Massive aggregate response has no bars")
    bars: dict[date, OptionCloseBar] = {}
    for index, row in enumerate(rows, start=1):
        where = f"Massive aggregate response, results row {index}"
        if not isinstance(row, dict):
            raise MassiveOptionPriceError(f"{where}: the row must be an object")
        close = _finite_number(row.get("c"))
        if close is None:
            raise MassiveOptionPriceError(
                f"{where}, column c: the close must be a finite JSON number"
            )
        volume = _finite_number(row.get("v"))
        if volume is None:
            raise MassiveOptionPriceError(
                f"{where}, column v: the volume must be a finite JSON number"
            )
        transactions = _integral_number(row.get("n"))
        if transactions is None:
            raise MassiveOptionPriceError(
                f"{where}, column n: the transaction count must be a finite, exactly "
                "integral JSON number"
            )
        milliseconds = _integral_number(row.get("t"))
        if milliseconds is None:
            raise MassiveOptionPriceError(
                f"{where}, column t: the timestamp must be a finite, exactly integral JSON "
                "number of milliseconds"
            )
        day = _new_york_date(milliseconds)
        if day is None:
            raise MassiveOptionPriceError(
                f"{where}, column t: the timestamp must resolve to a representable New York date"
            )
        if day in bars or close <= 0 or volume <= 0 or transactions <= 0:
            raise MassiveOptionPriceError("Massive aggregate bar is invalid")
        breach = number_limit_breach(close, NumberKind.PRICE)
        if breach is not None:
            raise MassiveOptionPriceError(f"{where}, column c: {breach}")
        bars[day] = OptionCloseBar(day, close, volume, int(transactions))
    if not bars:
        raise MassiveOptionPriceError("Massive aggregate response contains no traded days")
    return bars


def _artifact_path(cache_dir: Path, ticker: str, start: date, end: date) -> Path:
    safe_ticker = ticker.removeprefix("O:")
    return cache_dir / f"{safe_ticker}-{start.isoformat()}-{end.isoformat()}.json"


def _identity_metadata(
    *,
    ticker: str,
    underlying: str,
    expiration: date,
    strike_usd: Decimal,
    multiplier: Decimal,
    start: date,
    end: date,
) -> dict[str, Any]:
    """Return the artifact metadata that names its contract and window, without ``fetched_at``."""
    return {
        "ticker": ticker,
        "underlying": underlying,
        "expiration": expiration.isoformat(),
        "strike_usd": str(strike_usd),
        "multiplier": str(multiplier),
        "covered_start": start.isoformat(),
        "covered_end": end.isoformat(),
        "adjusted": False,
        "source": "massive-rest",
    }


def _payload_hash(metadata: dict[str, Any], overview_raw: str, bars_raw: str) -> str:
    canonical = json.dumps(
        {"metadata": metadata, "overview_raw": overview_raw, "bars_raw": bars_raw},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _write_artifact(
    path: Path,
    *,
    metadata: dict[str, Any],
    overview_raw: bytes,
    bars_raw: bytes,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    overview_text = overview_raw.decode()
    bars_text = bars_raw.decode()
    artifact = {
        "schema": _SCHEMA,
        "metadata": metadata,
        "overview_raw": overview_text,
        "bars_raw": bars_text,
        "payload_sha256": _payload_hash(metadata, overview_text, bars_text),
    }
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(artifact, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        try:
            path.hardlink_to(tmp_path)
        except FileExistsError as exc:
            raise_replacing(MassiveOptionPriceError("Massive option cache already exists"), exc)
    finally:
        tmp_path.unlink(missing_ok=True)


def _read_artifact(
    path: Path,
    *,
    ticker: str,
    underlying: str,
    expiration: date,
    strike_usd: Decimal,
    multiplier: Decimal,
    start: date,
    end: date,
) -> MassiveOptionCloseSeries:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
        metadata = artifact["metadata"]
        overview_text = artifact["overview_raw"]
        bars_text = artifact["bars_raw"]
        expected_hash = artifact["payload_sha256"]
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise_replacing(MassiveOptionPriceError("Massive option cache is malformed"), exc)
    if (
        artifact.get("schema") != _SCHEMA
        or not isinstance(metadata, dict)
        or not isinstance(overview_text, str)
        or not isinstance(bars_text, str)
        or expected_hash != _payload_hash(metadata, overview_text, bars_text)
        or metadata
        != {
            **_identity_metadata(
                ticker=ticker,
                underlying=underlying,
                expiration=expiration,
                strike_usd=strike_usd,
                multiplier=multiplier,
                start=start,
                end=end,
            ),
            "fetched_at": metadata.get("fetched_at"),
        }
    ):
        raise MassiveOptionPriceError("Massive option cache identity or hash is invalid")
    try:
        fetched_at = datetime.fromisoformat(str(metadata["fetched_at"]))
        overview = _json(overview_text)
        bars_payload = _json(bars_text)
    except (UnicodeError, ValueError) as exc:
        raise_replacing(MassiveOptionPriceError("Massive option cache metadata is invalid"), exc)
    _validate_identity(
        overview,
        ticker=ticker,
        underlying=underlying,
        expiration=expiration,
        strike_usd=strike_usd,
        multiplier=multiplier,
    )
    return MassiveOptionCloseSeries(
        massive_ticker=ticker,
        underlying=underlying,
        expiration=expiration,
        strike_usd=strike_usd,
        multiplier=multiplier,
        bars=_parse_bars(bars_payload, ticker=ticker),
        artifact_path=path.resolve(),
        fetched_at=fetched_at,
    )


def read_api_key(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "MASSIVE_API_KEY":
                key = value.strip().strip("'\"")
                if key:
                    return key
    except (OSError, UnicodeError) as exc:
        raise_replacing(MassiveOptionPriceError("Massive API key file is unavailable"), exc)
    raise MassiveOptionPriceError("Massive API key file contains no key")


def load_or_fetch_massive_option_closes(
    *,
    underlying: str,
    expiration: date,
    strike_usd: Decimal,
    right: str,
    multiplier: Decimal,
    start: date,
    end: date,
    cache_dir: Path,
    fetch: bool,
    api_key_file: Path | None,
    opener: UrlOpener | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> MassiveOptionCloseSeries | None:
    ticker = massive_option_ticker(
        underlying=underlying,
        expiration=expiration,
        strike_usd=strike_usd,
        right=right,
        multiplier=multiplier,
    )
    path = _artifact_path(cache_dir, ticker, start, end)
    if may_exist(path):
        return _read_artifact(
            path,
            ticker=ticker,
            underlying=underlying,
            expiration=expiration,
            strike_usd=strike_usd,
            multiplier=multiplier,
            start=start,
            end=end,
        )
    if not fetch:
        return None
    if api_key_file is None:
        raise MassiveOptionPriceError("Massive API key file is not configured")

    key = read_api_key(api_key_file)
    actual_opener = opener or _default_url_opener
    quoted_ticker = urllib.parse.quote(ticker, safe=":")
    sleep(_REQUEST_INTERVAL_SECONDS)
    overview_raw = _request_bytes(
        path=f"/v3/reference/options/contracts/{quoted_ticker}",
        query={"as_of": expiration.isoformat()},
        api_key=key,
        opener=actual_opener,
    )
    _validate_identity(
        _json(overview_raw),
        ticker=ticker,
        underlying=underlying,
        expiration=expiration,
        strike_usd=strike_usd,
        multiplier=multiplier,
    )
    sleep(_REQUEST_INTERVAL_SECONDS)
    bars_raw = _request_bytes(
        path=(f"/v2/aggs/ticker/{quoted_ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}"),
        query={"adjusted": "false", "sort": "asc", "limit": "5000"},
        api_key=key,
        opener=actual_opener,
    )
    bars = _parse_bars(_json(bars_raw), ticker=ticker)
    fetched_at = now or datetime.now(UTC)
    metadata = {
        **_identity_metadata(
            ticker=ticker,
            underlying=underlying,
            expiration=expiration,
            strike_usd=strike_usd,
            multiplier=multiplier,
            start=start,
            end=end,
        ),
        "fetched_at": fetched_at.isoformat(),
    }
    _write_artifact(path, metadata=metadata, overview_raw=overview_raw, bars_raw=bars_raw)
    return MassiveOptionCloseSeries(
        massive_ticker=ticker,
        underlying=underlying,
        expiration=expiration,
        strike_usd=strike_usd,
        multiplier=multiplier,
        bars=bars,
        artifact_path=path.resolve(),
        fetched_at=fetched_at,
    )
