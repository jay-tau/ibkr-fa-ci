"""One Massive option-close reply for the Robinhood option price tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from ibkr_fa.prices.series import MassiveOptionCloseSeries, OptionCloseBar


class OptionTerms(Protocol):
    """The contract terms a Massive series repeats."""

    @property
    def expiration(self) -> date: ...

    @property
    def strike_usd(self) -> Decimal: ...

    @property
    def multiplier(self) -> Decimal: ...


def option_series(
    massive_ticker: str,
    underlying: str,
    contract: OptionTerms,
    bars: Mapping[date, OptionCloseBar],
    *,
    artifact_path: Path,
    fetched_at: datetime,
) -> MassiveOptionCloseSeries:
    """Massive's ``bars`` for ``contract``'s expiration, strike and
    multiplier, as ``massive_ticker`` on ``underlying``, cached at
    ``artifact_path`` and fetched at ``fetched_at``."""
    return MassiveOptionCloseSeries(
        massive_ticker=massive_ticker,
        underlying=underlying,
        expiration=contract.expiration,
        strike_usd=contract.strike_usd,
        multiplier=contract.multiplier,
        bars=bars,
        artifact_path=artifact_path,
        fetched_at=fetched_at,
    )
