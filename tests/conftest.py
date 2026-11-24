"""Shared pytest fixtures for the ibkr-fa test suite.

Network isolation (issue #14): the FA peak-value feature fetches daily
closing prices, and the CLI enables fetching by default. To keep the
suite deterministic and offline, an autouse fixture neutralises the
default price fetcher so any code path that does not inject its own
fetcher behaves as "no prices available" (peak -> manual-fill). Tests
that exercise the populated-peak path inject an explicit fetcher (via
``run_fa_a3(price_fetcher=...)`` or ``load_price_series(fetcher=...)``)
and are therefore unaffected; tests that probe the real transport call
``ibkr_fa.prices.yahoo`` helpers directly, which this fixture leaves
untouched.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import fields, replace
from datetime import date, datetime
from decimal import Context, Decimal

import pytest

from ibkr_fa import output
from ibkr_fa.prices.series import PriceSeries
from tests._private_data import PrivateDataRootError, private_data_root


def pytest_configure() -> None:
    """Fail the session, before any test runs, when the private-data
    root is set but unusable (#838)."""
    try:
        private_data_root()
    except PrivateDataRootError as exc:
        raise pytest.UsageError(str(exc)) from exc


@pytest.fixture(autouse=True)
def _no_traceback_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test with the CLI's default error lines, even when the
    developer's shell sets ``IBKR_FA_TRACEBACK`` (#1049)."""
    monkeypatch.delenv("IBKR_FA_TRACEBACK", raising=False)


@pytest.fixture(autouse=True)
def _offline_price_fetcher(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Neutralise the default network price fetcher for every test."""

    def _no_network(
        ticker: object,
        *,
        start: date,
        end: date,
        now: datetime,
        fetch_symbol: str | None = None,
    ) -> PriceSeries | None:
        return None

    def _no_typed_transport(url: str, timeout: float):
        del url, timeout
        from ibkr_fa.prices.yahoo import (  # noqa: PLC0415
            YahooPriceFailureKind,
            _YahooTransportResult,
        )

        return _YahooTransportResult(None, YahooPriceFailureKind.TRANSPORT)

    def _no_massive_transport(_request: object, _timeout: float) -> bytes:
        raise OSError("network disabled in tests")

    def _no_identity_transport(_request: object, _timeout: float) -> tuple[str, bytes]:
        raise OSError("network disabled in tests")

    # Belt: the loader resolves this name at call time, so patching it
    # neutralises the pipeline/CLI default-fetch path.
    monkeypatch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", _no_network)
    monkeypatch.setattr(
        "ibkr_fa.pipeline_robinhood_equity_prices.fetch_daily_closes_usd_retrying",
        _no_network,
    )
    # Braces: the real urllib transport, also resolved at call time by
    # fetch_daily_closes_usd_result, so any direct fetch without an
    # injected url_opener fails closed instead of hitting the network.
    # A test that exercises the real transport keeps a reference to
    # _default_url_opener_result taken at import, which this patch
    # doesn't replace, and patches urllib.request.urlopen instead.
    monkeypatch.setattr(
        "ibkr_fa.prices.yahoo._default_url_opener_result",
        _no_typed_transport,
    )
    monkeypatch.setattr(
        "ibkr_fa.prices.massive_options._default_url_opener",
        _no_massive_transport,
    )
    monkeypatch.setattr(
        "ibkr_fa.robinhood_identity_sources._url_open",
        _no_identity_transport,
    )
    yield


@pytest.fixture
def isolated_output_registry() -> Iterator[None]:
    """Restore the writer store and the output catalog after a test that
    registers writers or edits catalog entries (#790)."""
    registry, catalog = dict(output._REGISTRY), dict(output._CATALOG)
    yield
    output._REGISTRY.clear()
    output._REGISTRY.update(registry)
    output._CATALOG.clear()
    output._CATALOG.update(catalog)


@pytest.fixture
def whole_rupees_without_rounding():
    """Whole-rupee values that refuse rounding or precision formatting."""

    class FiledDecimal(Decimal):
        def quantize(self, *_args, **_kwargs):
            raise AssertionError("a consumer rounded a filed value")

        def __format__(self, specifier: str, _context: Context | None = None, /) -> str:
            if specifier:
                raise AssertionError("a consumer formatted a filed value with precision")
            return super().__format__(specifier)

        def __add__(self, other):
            return type(self)(super().__add__(other))

        def __radd__(self, other):
            return type(self)(super().__radd__(other))

    return FiledDecimal


@pytest.fixture
def filed_without_rounding(monkeypatch: pytest.MonkeyPatch, whole_rupees_without_rounding):
    """Give consumers of a row's ``filed`` values (Form 67, Schedule FSI)
    distinct filed values that refuse another rounding pass."""

    def install(row, **overrides):
        filed = replace(row.filed, **overrides)
        filed = replace(
            filed,
            **{
                field.name: whole_rupees_without_rounding(value)
                for field in fields(filed)
                if isinstance(value := getattr(filed, field.name), Decimal)
            },
        )
        monkeypatch.setattr(type(row), "filed", property(lambda _row: filed))
        return filed

    return install
