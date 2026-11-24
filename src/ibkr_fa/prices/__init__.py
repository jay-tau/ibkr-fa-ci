"""Market price data for FA peak-value computation (issue #14).

This subpackage sources the one input the IBKR Activity Statement does
not contain — per-ticker **daily closing prices** — needed to compute
Schedule FA Table A3's "Peak value of investment during the Period".

Layers:

* :mod:`ibkr_fa.prices.series` — :class:`PriceSeries`, the immutable
  decoupling boundary the compute depends on.
* :mod:`ibkr_fa.prices.yahoo` — fetch raw daily closes from Yahoo's
  public chart API (best-effort; returns ``None`` when Yahoo can't be
  reached or has no data, but raises
  :class:`~ibkr_fa.prices.yahoo.YahooResponseFormatError` for a malformed
  response, #1139, and
  :class:`~ibkr_fa.prices.series.PriceSourceContradictionError` for one
  with two bars on one date on or before ``end``, #942).
* :mod:`ibkr_fa.prices.massive_options` — private, immutable Massive REST
  evidence for historical Robinhood option trade closes.
* :mod:`ibkr_fa.prices.cache` — an on-disk CSV cache that doubles as
  the immutable audit artifact (exact closes used + provenance), so
  re-runs are deterministic and a filed return is reproducible.
* :func:`load_price_series` — the orchestrator the pipeline calls.
"""

from __future__ import annotations

from ibkr_fa.prices.loader import load_price_series, resolve_override_symbols
from ibkr_fa.prices.series import DEFAULT_MAX_PRICE_STALENESS_DAYS, PriceSeries

__all__ = [
    "DEFAULT_MAX_PRICE_STALENESS_DAYS",
    "PriceSeries",
    "load_price_series",
    "resolve_override_symbols",
]
