"""Morgan Stanley StockPlan (Shareworks) .xlsx report parsers (#365)."""

from __future__ import annotations

from ibkr_fa.exceptions import ConfigError


def require_verified_issuer(symbol: str, isin: str) -> None:
    """Refuse unless the configured issuer's real exports have been verified."""
    if (symbol, isin) == ("GOOG", "US02079K1079"):
        return
    raise ConfigError(
        field="stockplan.security",
        message=(
            "Morgan Stanley StockPlan exports are verified only for Alphabet GOOG "
            "(US02079K1079); refusing the configured issuer until its real exports "
            "are reviewed (#382)."
        ),
    )
