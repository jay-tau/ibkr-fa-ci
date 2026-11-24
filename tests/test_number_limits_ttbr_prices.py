"""Read-time digit limits on TTBR rates and on fetched and cached prices (#1276).

``parse_ttbr_row``, which ``load_ttbr`` and ``scripts/refresh_ttbr.py`` share,
reads ``TT BUY`` as an FX rate: at most 3 digits before the decimal point and 4
after it, checked right after the ``TT BUY`` shape check. A rate past it
refuses with a ``ValueError`` holding the kind's fixed text, which ``load_ttbr``
turns into its usual row ``InvariantError``; the (1, 200] range runs after, on
in-limit rates only.

Every close and high that the Yahoo, Nasdaq and Massive readers and the price
cache read is a unit price: at most 9 digits before the decimal point and 20
after it. Each reader checks it after its own shape, finite and sign checks and
before any high-below-close check, and refuses in its own error type with the
fixed text and no value or chain. Split ratios and Massive's volume,
transaction count, timestamp, strike and multiplier aren't checked.

New read-limit refusal inputs include numbers one digit or place past each
kind's limit, the issue's examples and larger values. Each carries the digit
run ``8353`` inside the breaching number; no refusal may print it. Each limit
is also pinned from the other side:
a value exactly at it is read, and a value an existing check refuses keeps that
refusal.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest

from ibkr_fa.cli import main
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.fx import ttbr_loader
from ibkr_fa.fx.ttbr_loader import parse_ttbr_row
from ibkr_fa.invariants import invariant_error
from ibkr_fa.prices.cache import PriceCacheError, _cache_path, read_cache, write_cache_lock
from ibkr_fa.prices.massive_options import MassiveOptionPriceError
from ibkr_fa.prices.nasdaq import NasdaqPriceFormatError
from ibkr_fa.prices.yahoo import YahooResponseFormatError
from ibkr_fa.types import Ticker
from tests._activity_statement_e2e import ACTIVITY_STATEMENT_E2E_USER_TOML
from tests._crash_reports import (
    assert_no_markers,
    assert_prints_no_marker,
    assert_replaced_unchained,
    assert_unchained_refusal,
)
from tests._ibkr_run_all import CY_2025_INPUT
from tests._massive_option_replies import MASSIVE_INVALID_BAR_REFUSAL as _INVALID_BAR
from tests._massive_option_replies import MR_MARKERS, mr_artifact_bytes, mr_bars, mr_overview
from tests._massive_option_replies import MR_PATHS as _PATHS
from tests._massive_option_replies import MR_ROW_ONE_T as _ROW_ONE_T
from tests._massive_option_replies import MR_ROW_TWO_T as _ROW_TWO_T
from tests._massive_option_replies import mr_artifact_path as _artifact
from tests._massive_option_replies import mr_load as _mr_load
from tests._nasdaq_prices import NASDAQ_PRICE_FORMAT as _FORMAT
from tests._nasdaq_prices import NASDAQ_VALID_ROW as _VALID_ROW
from tests._nasdaq_prices import fetch_nasdaq_rows as _nasdaq_fetch
from tests._nasdaq_prices import nasdaq_row_message as _nasdaq_message
from tests._number_limits import FX_RATE, PRICE
from tests._price_cache import ALL_CACHE_MARKERS as _MARKERS
from tests._price_cache import CACHE_MARKER_DATE as _MARKER_DATE
from tests._price_cache import CY_2025_FIRST_PRICED_TICKER as _CLI_TICKER
from tests._price_cache import HIGH_CACHE_BODY as _HIGH_BODY
from tests._price_cache import HIGH_CACHE_MARKERS
from tests._price_cache import HIGH_CACHE_ROW as _HIGH_ROW
from tests._price_cache import MARKER_CACHE_BODY as _MARKER_BODY
from tests._price_cache import MARKER_CACHE_ROW as _ROW
from tests._price_cache import assert_cache_refuses as _assert_cache_refuses
from tests._price_cache import edit_once as _edit
from tests._price_cache import price_cache_refusal as _refusal
from tests._price_cache import price_flags as _price_flags
from tests._price_cache import read_marker_cache as _read_marker_cache
from tests._price_cache import split_ratios_edit as _ratios
from tests._price_cache import with_payload_hash as _hashed
from tests._refresh_ttbr_script import make_upstream, refresh_ttbr
from tests._robinhood_refusals import MASSIVE_CLOSE_REFUSAL as _CLOSE
from tests._robinhood_refusals import MASSIVE_ROW_TWO as _ROW_TWO
from tests._schedule_os_e2e import run_schedule_os_on_dividends as _run_schedule_os
from tests._schedule_os_e2e import schedule_os_with_earlier_output as _schedule_os
from tests._ttbr_rows import SBI_CSV_HEADER as _HEADER
from tests._ttbr_rows import USD_TTBR_CSV as _USD_CSV
from tests._ttbr_rows import assert_ttbr_row_refusal as _assert_ttbr_row_refusal
from tests._ttbr_rows import load_ttbr_lines as _load_lines
from tests._ttbr_rows import ttbr_row as _row
from tests._yahoo_prices import bar_epoch as _epoch
from tests._yahoo_prices import chart_payload as _payload
from tests._yahoo_prices import fetch_payload as _fetch

_MARKER = "8353"

# --- TTBR ------------------------------------------------------------------------

# The issue's ``80.`` and 50 more digits, the first refusing whole and decimal
# sizes, and a rate exactly at both limits.
_RATE_50_PLACES = f"80.{_MARKER}{'0' * 46}"
_RATE_4_WHOLE = _MARKER
_RATE_5_PLACES = f"88.0{_MARKER}"
_RATE_AT = "999.9999"
_RATE_SHAPE = "TT BUY is not plain digits with an optional fraction"
_RATES_PAST_THE_LIMIT = pytest.mark.parametrize(
    "rate",
    [
        pytest.param(_RATE_50_PLACES, id="50-places"),
        pytest.param(_RATE_4_WHOLE, id="4-whole-digits"),
        pytest.param(_RATE_5_PLACES, id="5-places"),
        pytest.param(f"{_MARKER}.0000", id="4-whole-digits-4-places"),
    ],
)


def _ttbr_cells(rate: str) -> list[str]:
    return _row("2025-12-31 09:00", rate).split(",")


@_RATES_PAST_THE_LIMIT
def test_ttbr_rate_past_the_fx_rate_limit_refuses_the_row(rate: str) -> None:
    with pytest.raises(ValueError) as raised:
        parse_ttbr_row(_ttbr_cells(rate))
    error = raised.value
    assert type(error) is ValueError
    assert error.args == (FX_RATE,)
    assert str(error) == FX_RATE
    assert_replaced_unchained(error, markers=(_MARKER,))


@pytest.mark.parametrize("rate", [_RATE_AT, "999", "0.0001", "200.0000", "0"])
def test_ttbr_rate_at_the_fx_rate_limit_parses(rate: str) -> None:
    day, parsed = parse_ttbr_row(_ttbr_cells(rate))
    assert day == date(2025, 12, 31)
    assert parsed.as_tuple() == Decimal(rate).as_tuple()


@pytest.mark.parametrize("rate", [f"{_MARKER}e3", f"-88.0{_MARKER}", f"{_MARKER}."])
def test_ttbr_rate_shape_check_comes_before_the_limit(rate: str) -> None:
    with pytest.raises(ValueError) as raised:
        parse_ttbr_row(_ttbr_cells(rate))
    assert raised.value.args == (_RATE_SHAPE,)
    assert_prints_no_marker(raised.value, markers=(_MARKER,))


@_RATES_PAST_THE_LIMIT
def test_load_ttbr_refuses_a_rate_past_the_limit_as_a_row_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rate: str
) -> None:
    """The loader's usual row refusal: the fixed text and the line, no value,
    and only the row error's content-free stand-in. A 4-digit rate, also past
    the (1, 200] range, meets the row check first."""
    with pytest.raises(InvariantError) as raised:
        _load_lines([_HEADER, _row(), _row("2025-12-31 09:00", rate)], tmp_path, monkeypatch)
    _assert_ttbr_row_refusal(raised.value, FX_RATE, line=3, markers=(_MARKER,))


def test_load_ttbr_reads_a_rate_at_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rates = _load_lines(
        [_HEADER, _row(), _row("2025-12-31 09:00", "199.9999")], tmp_path, monkeypatch
    )
    assert rates == {date(2025, 12, 30): Decimal("89.47"), date(2025, 12, 31): Decimal("199.9999")}


@pytest.mark.parametrize(
    ("rate", "marker"),
    [
        pytest.param("200.01", "200.01", id="issue-200.01"),
        pytest.param(f"200.{_MARKER}", _MARKER, id="above-200-at-4-places"),
        pytest.param(f"0.{_MARKER}", _MARKER, id="below-1-at-4-places"),
    ],
)
def test_in_limit_rate_outside_the_sane_range_keeps_the_range_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rate: str, marker: str
) -> None:
    with pytest.raises(InvariantError) as raised:
        _load_lines([_HEADER, _row(), _row("2025-12-31 09:00", rate)], tmp_path, monkeypatch)
    what = "load_ttbr('XXX'): rate must be in (1, 200]"
    assert_unchained_refusal(
        raised.value,
        invariant_error(what=what, date=None, rate=None),
        f"InvariantError: {what} (context keys: date, rate)",
        markers=(marker,),
    )


@_RATES_PAST_THE_LIMIT
def test_refresh_script_refuses_an_upstream_rate_past_the_limit(rate: str) -> None:
    """``scripts/refresh_ttbr.py`` shares the row parser, so it refuses the
    upstream row before writing anything, naming the line and DATE only."""
    upstream = make_upstream([f"2025-01-02 09:00,url,{rate},85.10,84.05,85.20,83.90,85.40,83,85"])
    with pytest.raises(ValueError) as raised:
        refresh_ttbr.transform(upstream)
    error = raised.value
    assert type(error) is ValueError
    assert str(error) == f"upstream CSV line 2 (DATE '2025-01-02 09:00'): {FX_RATE}"
    assert type(error.__cause__) is ValueError
    assert error.__cause__.args == (FX_RATE,)
    assert_prints_no_marker(error, markers=(_MARKER,))


@pytest.fixture
def past_limit_usd_ttbr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The loader reads a synthetic USD file whose second data row has a rate
    with 50 decimal places. Its cache is cleared before and after, so the
    bundled map isn't reused here and no other test sees this file."""
    fake = tmp_path / "reference" / _USD_CSV
    fake.parent.mkdir()
    lines = [_HEADER, _row(), _row("2025-12-31 09:00", _RATE_50_PLACES)]
    fake.write_text("\n".join(lines) + "\n", encoding="utf-8")

    class _Reference:
        def joinpath(self, name: str) -> Path:
            assert name == _USD_CSV
            return fake

    def files(package: str) -> _Reference:
        assert package == "ibkr_fa.reference"
        return _Reference()

    monkeypatch.setattr(
        ttbr_loader, "resources", SimpleNamespace(files=files, as_file=resources.as_file)
    )
    ttbr_loader.load_ttbr.cache_clear()
    yield
    ttbr_loader.load_ttbr.cache_clear()


@pytest.mark.usefixtures("past_limit_usd_ttbr")
def test_schedule_os_refuses_a_ttbr_past_the_limit_and_removes_the_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Converting the statement's USD dividends loads the file, which refuses:
    exit 4 with the fixed message only, no marker, and the earlier output gone."""
    out = _schedule_os(tmp_path)

    assert _run_schedule_os(tmp_path) == 4

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: InvariantError: {_USD_CSV}: {FX_RATE} (context keys: line=3)\n"
    assert_no_markers(captured.out + captured.err, _MARKER)
    assert list(out.iterdir()) == []


# --- Prices -----------------------------------------------------------------------

# The issue's bound-edge close with the marker in its 14 whole digits (16
# places), the first refusing whole and decimal sizes, and a price exactly at
# both limits.
_ISSUE_CLOSE = "83533333333333.1666666666666666"
_PRICE_10 = "8353000000"
_PLACES_21 = "1.000000000000000008353"
_PRICE_AT = "999999999.99999999999999999999"

# --- Yahoo


def _yahoo_refusal(
    bars: dict[date, float | None], highs: dict[date, float | None] | None = None
) -> YahooResponseFormatError:
    with pytest.raises(YahooResponseFormatError) as raised:
        _fetch(_payload(bars=bars, highs=highs), start=date(2025, 3, 1), end=date(2025, 3, 31))
    return raised.value


_MAR_3 = date(2025, 3, 3)
_MAR_4 = date(2025, 3, 4)
_CLOSE_0 = "indicators.quote[0].close[0]"
_HIGH_0 = "indicators.quote[0].high[0]"


@pytest.mark.parametrize(
    ("bars", "highs", "field"),
    [
        pytest.param({_MAR_3: int(_PRICE_10)}, None, _CLOSE_0, id="close-10-digit-integer"),
        pytest.param({_MAR_3: 8353000000.5}, None, _CLOSE_0, id="close-10-whole-digits"),
        pytest.param({_MAR_3: 1.8353e-17}, None, _CLOSE_0, id="close-21-places"),
        pytest.param({_MAR_3: 8353 * 10**37}, None, _CLOSE_0, id="close-41-digit-integer"),
        pytest.param(
            {_MAR_3: 100.0, _MAR_4: int(_PRICE_10)},
            None,
            "indicators.quote[0].close[1]",
            id="second-close",
        ),
        pytest.param(
            {_MAR_3: 100.0, date(2025, 6, 2): int(_PRICE_10)},
            None,
            "indicators.quote[0].close[1]",
            id="close-after-the-window",
        ),
        pytest.param(
            {_MAR_3: 100.0}, {_MAR_3: int(_PRICE_10)}, _HIGH_0, id="high-10-digit-integer"
        ),
        pytest.param(
            {_MAR_3: 100.0}, {_MAR_3: 1.8353e-17}, _HIGH_0, id="high-21-places-below-close"
        ),
        pytest.param(
            {_MAR_3: int(_PRICE_10)}, {_MAR_3: 1.0}, _CLOSE_0, id="close-before-high-checks"
        ),
    ],
)
def test_yahoo_price_past_the_limit_refuses(
    bars: dict[date, float | None], highs: dict[date, float | None] | None, field: str
) -> None:
    """Every bar's close and high is read, a bar after the window's end
    included; the close is checked first, and a high's limit before its
    high-below-close check."""
    assert_unchained_refusal(
        _yahoo_refusal(bars, highs),
        YahooResponseFormatError(field, PRICE),
        f"Yahoo price response, {field}: {PRICE}",
        markers=(_MARKER,),
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(999999999, "999999999", id="9-digit-integer"),
        pytest.param(999999999.5, "999999999.5", id="9-whole-digits"),
        pytest.param(1.8353e-16, "1.8353E-16", id="20-places"),
    ],
)
def test_yahoo_price_at_the_limit_reads(value: float, expected: str) -> None:
    series = _fetch(
        _payload(bars={_MAR_3: value}, highs={_MAR_3: value}),
        start=date(2025, 3, 1),
        end=date(2025, 3, 31),
    )
    assert series is not None
    assert series.closes[_MAR_3].as_tuple() == Decimal(expected).as_tuple()
    assert series.highs[_MAR_3].as_tuple() == Decimal(expected).as_tuple()


_NOT_POSITIVE = "the close isn't null or a number above zero"


@pytest.mark.parametrize(
    ("close", "high", "field", "rule"),
    [
        pytest.param(0, 8353.0, _CLOSE_0, _NOT_POSITIVE, id="zero-close"),
        pytest.param(-int(_PRICE_10), None, _CLOSE_0, _NOT_POSITIVE, id="negative-close"),
        pytest.param(-1.8353e-17, None, _CLOSE_0, _NOT_POSITIVE, id="negative-tiny-close"),
        pytest.param(100.0, 99.8353, _HIGH_0, "the high is below the close", id="high-below-close"),
        pytest.param(
            8353.0,
            float("inf"),
            _HIGH_0,
            "the high isn't null or a finite number",
            id="infinite-high",
        ),
    ],
)
def test_yahoo_existing_refusals_keep_their_text_and_order(
    close: float, high: float | None, field: str, rule: str
) -> None:
    """A close that isn't above zero refuses as before, even past the limit."""
    highs: dict[date, float | None] | None = None if high is None else {_MAR_3: high}
    assert_unchained_refusal(
        _yahoo_refusal({_MAR_3: close}, highs),
        YahooResponseFormatError(field, rule),
        f"Yahoo price response, {field}: {rule}",
        markers=(_MARKER,),
    )


def test_yahoo_split_ratio_is_not_limit_checked() -> None:
    """A split's numerator and denominator aren't prices: a 13-digit numerator
    is applied as before. The close it restores is a unit price, so one within
    the limit is read and one past it refuses at fetch (#1307)."""
    split_day = date(2025, 6, 2)
    numerator = 8353000000000

    def payload(close: float) -> bytes:
        return _payload(
            bars={_MAR_3: close},
            splits={
                str(_epoch(split_day)): {
                    "date": _epoch(split_day),
                    "numerator": numerator,
                    "denominator": 1,
                }
            },
        )

    series = _fetch(payload(1e-12), start=date(2025, 3, 1), end=date(2025, 3, 31))
    assert series is not None
    assert series.split_ratios == {split_day: Decimal(numerator)}
    assert {day: str(close) for day, close in series.closes.items()} == {_MAR_3: "8.353000000000"}
    field = "indicators.quote[0].close[0]"
    rule = f"the close on its own day's split basis has no exact value that fits: {PRICE}"
    with pytest.raises(YahooResponseFormatError) as raised:
        _fetch(payload(100.0), start=date(2025, 3, 1), end=date(2025, 3, 31))
    assert_unchained_refusal(
        raised.value,
        YahooResponseFormatError(field, rule),
        f"Yahoo price response, {field}: {rule}",
        markers=(_MARKER,),
    )


# --- Nasdaq


def _nasdaq_refusal(fields: dict[str, object]) -> NasdaqPriceFormatError:
    """The refusal for a row dated 17 June 2025 with ``fields``, read after
    ``_VALID_ROW``."""
    with pytest.raises(NasdaqPriceFormatError) as raised:
        _nasdaq_fetch(_VALID_ROW, {"date": "06/17/2025", **fields})
    return raised.value


@pytest.mark.parametrize(
    ("fields", "column"),
    [
        pytest.param({"close": _ISSUE_CLOSE}, "close", id="issue-close"),
        pytest.param({"close": f"${_ISSUE_CLOSE}"}, "close", id="issue-close-dollar"),
        pytest.param({"close": _PRICE_10}, "close", id="close-10-whole-digits"),
        pytest.param({"close": "$1,000,000,000.8353"}, "close", id="close-grouped-10-whole"),
        pytest.param({"close": f"${_PLACES_21}"}, "close", id="close-21-places"),
        pytest.param({"close": "$1.00", "high": _PRICE_10}, "high", id="high-10-whole-digits"),
        pytest.param(
            {"close": "$1.00", "high": "0.000000000000000008353"},
            "high",
            id="high-21-places-below-close",
        ),
        pytest.param({"close": _PRICE_10, "high": "$0.50"}, "close", id="close-before-high"),
    ],
)
def test_nasdaq_price_past_the_limit_refuses(fields: dict[str, object], column: str) -> None:
    """A value in Nasdaq's format past the limit refuses at its row and
    column; a high's limit comes before its high-below-close check."""
    assert column in ("close", "high")
    assert_unchained_refusal(
        _nasdaq_refusal(fields),
        NasdaqPriceFormatError(2, "close" if column == "close" else "high", too_many_digits=True),
        _nasdaq_message(2, column, PRICE),
        markers=(_MARKER,),
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("$999,999,999.99999999999999999999", _PRICE_AT, id="grouped"),
        pytest.param(_PRICE_AT, _PRICE_AT, id="plain"),
        pytest.param("$0.00000000000000000001", "1E-20", id="20-places"),
    ],
)
def test_nasdaq_price_at_the_limit_reads(text: str, expected: str) -> None:
    series = _nasdaq_fetch({"date": "06/02/2025", "close": text, "high": text})
    assert series is not None
    assert series.closes[date(2025, 6, 2)].as_tuple() == Decimal(expected).as_tuple()
    assert series.highs[date(2025, 6, 2)].as_tuple() == Decimal(expected).as_tuple()


@pytest.mark.parametrize(
    ("fields", "expected", "problem", "marker"),
    [
        pytest.param(
            {"close": "$0.000000000000000000000"},
            NasdaqPriceFormatError(2, "close"),
            _FORMAT,
            "0.000000000000000000000",
            id="zero-close-past-places",
        ),
        pytest.param(
            {"close": f"-{_PRICE_10}"},
            NasdaqPriceFormatError(2, "close"),
            _FORMAT,
            _MARKER,
            id="negative-close",
        ),
        pytest.param(
            {"close": f"{_PRICE_10},0"},
            NasdaqPriceFormatError(2, "close"),
            _FORMAT,
            _MARKER,
            id="bad-grouping",
        ),
        pytest.param(
            {"close": "$1.00", "high": f"{_PRICE_10}e0"},
            NasdaqPriceFormatError(2, "high"),
            _FORMAT,
            _MARKER,
            id="high",
        ),
        pytest.param(
            {"close": "$1.8353", "high": "$1.00"},
            NasdaqPriceFormatError(2, "high", below_close=True),
            "the high is below the close",
            _MARKER,
            id="high-below-close",
        ),
    ],
)
def test_nasdaq_existing_refusals_keep_their_text_and_order(
    fields: dict[str, object], expected: NasdaqPriceFormatError, problem: str, marker: str
) -> None:
    """A value outside Nasdaq's format, or a zero, keeps the format refusal
    even past the limit; an in-limit high below its close keeps its refusal."""
    assert_unchained_refusal(
        _nasdaq_refusal(fields),
        expected,
        _nasdaq_message(2, expected.column, problem),
        markers=(marker,),
    )


def test_nasdaq_rows_outside_the_window_stay_unread() -> None:
    series = _nasdaq_fetch(
        {"date": "12/31/2024", "close": _ISSUE_CLOSE, "high": _PRICE_10},
        {"date": "01/02/2025", "close": "1.0000"},
    )
    assert series is not None
    assert series.closes == {date(2025, 1, 2): Decimal("1.0000")}


# --- Massive


def _massive_refuses(
    tmp_path: Path, path: str, overview: bytes, bars: bytes, message: str, marker: str
) -> None:
    """The read refuses with exactly ``message`` and no chain or marker. A
    refused fetch writes no artifact; a refused artifact stays byte-identical."""
    with pytest.raises(MassiveOptionPriceError) as raised:
        _mr_load(tmp_path, path, overview, bars)
    error = raised.value
    assert type(error) is MassiveOptionPriceError
    assert error.args == (message,)
    assert str(error) == message
    assert_replaced_unchained(error, markers=(marker, *MR_MARKERS))
    if path == "fetch":
        assert not (tmp_path / "cache").exists()
    else:
        assert _artifact(tmp_path).read_bytes() == mr_artifact_bytes(overview, bars)


_MASSIVE_CLOSE_LIMIT = f"{_ROW_TWO}, column c: {PRICE}"


@_PATHS
@pytest.mark.parametrize(
    "close",
    [
        pytest.param(_ISSUE_CLOSE, id="issue-close"),
        pytest.param(_PRICE_10, id="10-digit-integer"),
        pytest.param(f"{_PRICE_10}.5", id="10-whole-digits"),
        pytest.param(_PLACES_21, id="21-places"),
        pytest.param(f"{_MARKER}E+6", id="10-whole-digits-exponent"),
    ],
)
def test_massive_close_past_the_limit_refuses(tmp_path: Path, path: str, close: str) -> None:
    """Fetched or read from the artifact, a close past the limit refuses after
    the bar's other checks."""
    _massive_refuses(tmp_path, path, mr_overview(), mr_bars(c=close), _MASSIVE_CLOSE_LIMIT, _MARKER)


@_PATHS
@pytest.mark.parametrize("close", [_PRICE_AT, "999999999", "1E-20"])
def test_massive_close_at_the_limit_reads(tmp_path: Path, path: str, close: str) -> None:
    series = _mr_load(tmp_path, path, mr_overview(), mr_bars(c=close))
    assert series is not None
    assert series.bars[date(2025, 1, 16)].close_usd.as_tuple() == Decimal(close).as_tuple()


@_PATHS
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        pytest.param({"c": f"0E-{_MARKER}"}, _INVALID_BAR, id="zero-close"),
        pytest.param({"c": f"-{_PRICE_10}"}, _INVALID_BAR, id="negative-close-past-limit"),
        pytest.param({"c": _PRICE_10, "v": "0"}, _INVALID_BAR, id="zero-volume"),
        pytest.param({"c": _PRICE_10, "n": "0"}, _INVALID_BAR, id="zero-count"),
        pytest.param(
            {"c": _PRICE_10, "t": str(_ROW_ONE_T + 3_600_000)}, _INVALID_BAR, id="same-day"
        ),
        pytest.param({"c": f'"{_PRICE_10}"'}, _CLOSE, id="close-text"),
    ],
)
def test_massive_existing_refusals_keep_their_text_and_order(
    tmp_path: Path, path: str, changes: dict[str, object], message: str
) -> None:
    """The invalid-bar check runs first, so a close past the limit on an
    otherwise invalid bar keeps that refusal; a close that isn't a JSON number
    keeps its own."""
    _massive_refuses(tmp_path, path, mr_overview(), mr_bars(**changes), message, _MARKER)


_LONG_PLACES = "0" * 30


@_PATHS
@pytest.mark.parametrize(
    ("overview", "bars", "attribute", "expected"),
    [
        pytest.param(
            {},
            {"v": f"{_MARKER}000000000000000000000.5"},
            "volume",
            Decimal(f"{_MARKER}000000000000000000000.5"),
            id="volume",
        ),
        pytest.param(
            {},
            {"n": f"{_MARKER}000000000000000000000"},
            "transactions",
            int(f"{_MARKER}000000000000000000000"),
            id="count",
        ),
        pytest.param(
            {}, {"t": f"{_ROW_TWO_T}.{_LONG_PLACES}"}, "day", date(2025, 1, 16), id="timestamp"
        ),
        pytest.param(
            {"strike_price": f"250.{_LONG_PLACES}"},
            {},
            "close_usd",
            Decimal("1.5"),
            id="strike",
        ),
        pytest.param(
            {"shares_per_contract": f"100.{_LONG_PLACES}"},
            {},
            "close_usd",
            Decimal("1.5"),
            id="multiplier",
        ),
    ],
)
def test_massive_fields_other_than_the_close_are_not_limit_checked(
    tmp_path: Path,
    path: str,
    overview: dict[str, object],
    bars: dict[str, object],
    attribute: str,
    expected: object,
) -> None:
    """Volume, count and timestamp keep their own checks only, and the strike
    and multiplier must equal the statement's, as before."""
    series = _mr_load(tmp_path, path, mr_overview(**overview), mr_bars(**bars))
    assert series is not None
    assert getattr(series.bars[date(2025, 1, 16)], attribute) == expected


# --- the price cache


_CACHE_MARKERS = (*_MARKERS, _MARKER)
_TOO_MANY_DIGITS = "has a {name} with too many digits: {price} (line 14, column {name}_usd)"
_LOCKED = pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])


def _cache_row(close: str, high: str) -> str:
    return _edit(_ROW, f"{_MARKER_DATE},{close},{high}\n")(_MARKER_BODY)


@_LOCKED
@pytest.mark.parametrize(
    ("close", "high", "name"),
    [
        pytest.param(_ISSUE_CLOSE, "", "close", id="issue-close"),
        pytest.param(_PRICE_10, "", "close", id="close-10-whole-digits"),
        pytest.param(f"{_MARKER}E+6", "", "close", id="close-10-whole-exponent"),
        pytest.param(_PLACES_21, "6800.5", "close", id="close-21-places"),
        pytest.param(_PRICE_10, "1", "close", id="close-before-high-checks"),
        pytest.param("6731.4289", _PRICE_10, "high", id="high-10-whole-digits"),
        pytest.param("6731.4289", f"6800.{_PLACES_21[2:]}", "high", id="high-21-places"),
        pytest.param(
            "6731.4289", "0.000000000000000008353", "high", id="high-21-places-below-close"
        ),
        pytest.param("6731.4289", f"-{_PRICE_10}", "high", id="negative-high-past-limit"),
    ],
)
def test_cache_price_past_the_limit_refuses(
    tmp_path: Path, close: str, high: str, name: str, locked: bool
) -> None:
    """Locked or not, on read and through every ``load_or_fetch`` mode, without
    fetching or changing a file. A high's limit comes before its
    high-below-close check."""
    problem = _TOO_MANY_DIGITS.format(name=name, price=PRICE)
    text = _hashed(_cache_row(close, high))
    _assert_cache_refuses(tmp_path, text, problem, locked=locked, markers=_CACHE_MARKERS)
    with pytest.raises(PriceCacheError) as raised:
        read_cache(_cache_path(tmp_path, Ticker(_MARKERS[0])), locked=locked)
    assert_replaced_unchained(
        raised.value, markers=tuple(marker for marker in _CACHE_MARKERS if marker in text)
    )


@_LOCKED
@pytest.mark.parametrize(
    ("close", "high"),
    [
        pytest.param(_PRICE_AT, _PRICE_AT, id="9-whole-20-places"),
        pytest.param("1E-20", "999999999", id="20-places-exponent"),
    ],
)
def test_cache_price_at_the_limit_reads(
    tmp_path: Path, close: str, high: str, locked: bool
) -> None:
    path = _cache_path(tmp_path, Ticker(_MARKERS[0]))
    path.write_text(_hashed(_cache_row(close, high)), encoding="utf-8")
    if locked:
        write_cache_lock(path)
    read = read_cache(path, locked=locked)
    assert read is not None
    series = read[0]
    assert series.closes[date(2025, 2, 13)].as_tuple() == Decimal(close).as_tuple()
    assert series.highs[date(2025, 2, 13)].as_tuple() == Decimal(high).as_tuple()


_NOT_POSITIVE_CLOSE = "has a close that isn't a positive price (line 14, column close_usd)"


@_LOCKED
@pytest.mark.parametrize(
    ("close", "high", "problem"),
    [
        pytest.param(f"-{_PRICE_10}", "6800.5", _NOT_POSITIVE_CLOSE, id="negative-close"),
        pytest.param(f"0E-{_MARKER}", "6800.5", _NOT_POSITIVE_CLOSE, id="zero-close-past-places"),
        pytest.param(
            f"{_PRICE_10}_0",
            "6800.5",
            "has close text outside the supported ASCII decimal grammar "
            "(line 14, column close_usd)",
            id="close-grammar",
        ),
        pytest.param(
            "6731.4289",
            f"{_PRICE_10}_0",
            "has high text outside the supported ASCII decimal grammar (line 14, column high_usd)",
            id="high-grammar",
        ),
        pytest.param(
            f"{_MARKER}.5",
            f"{_MARKER}.25",
            "has a high below its close (line 14, column high_usd)",
            id="high-below-close",
        ),
    ],
)
def test_cache_existing_refusals_keep_their_text_and_order(
    tmp_path: Path, close: str, high: str, problem: str, locked: bool
) -> None:
    """The grammar and positive-close checks run before the limit, so a value
    past it that one of them refuses keeps that refusal."""
    _assert_cache_refuses(
        tmp_path, _hashed(_cache_row(close, high)), problem, locked=locked, markers=_CACHE_MARKERS
    )


def test_cache_split_ratio_is_not_limit_checked(tmp_path: Path) -> None:
    """The cache stores the tool's own split quotient, read as before."""
    ratio = f"{_MARKER}000000000000000000000.{'0' * 23}1"
    body = _ratios(f'{{"2026-01-05":"{ratio}"}}')(_MARKER_BODY)
    series = _read_marker_cache(tmp_path, body)[0]
    assert series.split_ratios == {date(2026, 1, 5): Decimal(ratio)}


@pytest.mark.parametrize("online", [False, True], ids=["offline", "online"])
def test_schedule_fa_refuses_a_cached_close_past_the_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    online: bool,
) -> None:
    """The statement's first priced holding has a correctly hashed cache whose
    one close is the issue's: exit 4 with the cache refusal alone, no FA output
    (an earlier run's included), the cache folder unchanged and no marker."""
    config = tmp_path / "user.toml"
    config.write_text(ACTIVITY_STATEMENT_E2E_USER_TOML, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    cache = _cache_path(cache_dir, Ticker(_CLI_TICKER))
    cache.parent.mkdir()
    body = _edit(_HIGH_ROW, f"2025-12-31,{_ISSUE_CLOSE},\n")(_HIGH_BODY)
    cache.write_text(_hashed(body), encoding="utf-8")
    before = {path.name: path.read_bytes() for path in cache_dir.iterdir()}
    out = tmp_path / "out"
    out.mkdir()
    output = out / "schedule-fa.csv"
    for stale in (output, out / "schedule-fa-a2.csv"):
        stale.write_bytes(b"an earlier run")

    rc = main(
        [
            "schedule-fa",
            *("--input", str(CY_2025_INPUT), "--config", str(config)),
            *("--output", str(output), "--calendar-year", "2025"),
            *("--price-cache-dir", str(cache_dir)),
            *_price_flags(monkeypatch, online=online),
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    problem = _TOO_MANY_DIGITS.format(name="close", price=PRICE)
    assert captured.err == f"error: {_refusal(cache_dir / 'x.csv', problem, locked=False)}\n"
    assert captured.out == ""
    assert list(out.iterdir()) == []
    assert {path.name: path.read_bytes() for path in cache_dir.iterdir()} == before
    markers = (_MARKER, *HIGH_CACHE_MARKERS[:3], HIGH_CACHE_MARKERS[6], cache.name)
    assert_no_markers(captured.out + captured.err, *markers)
