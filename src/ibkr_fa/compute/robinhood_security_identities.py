"""The Robinhood security identities one run uses, kept apart from user config."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from ibkr_fa.config import RobinhoodSecurityConfig
from ibkr_fa.exceptions import IbkrFaError


class RobinhoodIdentityCollisionError(IbkrFaError):
    """Two Robinhood security identities share a CUSIP, ISIN or symbol."""


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodSecurityIdentities:
    """Configured identities from user.toml, then identities generated from official sources.

    An index into ``securities`` is the ``config_index`` /
    ``security_config_index`` every Robinhood leg uses.
    """

    configured: tuple[RobinhoodSecurityConfig, ...]
    generated: tuple[RobinhoodSecurityConfig, ...] = ()

    def __post_init__(self) -> None:
        # Each entry is named by position, never by its CUSIP, ISIN or symbol,
        # which may come from a statement (#1044). A configured entry's
        # free-text source can hold anything; a generated entry's source is
        # built by code.
        origins = (
            *(f"[[robinhood.securities]] entry {n}" for n in range(1, len(self.configured) + 1)),
            *(
                f"generated identity {n} (from the {item.source})"
                for n, item in enumerate(self.generated, start=1)
            ),
        )
        entries: dict[tuple[str, str], list[str]] = {}
        for item, origin in zip(self.securities, origins, strict=True):
            symbol, isin = item.instrument.symbol, item.instrument.isin
            for key in (("CUSIP", item.cusip), ("ISIN", isin), ("symbol", symbol)):
                entries.setdefault(key, []).append(origin)
        collisions = [
            f"  same {field}: {first} and {second}"
            for (field, _value), shared in entries.items()
            for first, second in combinations(shared, 2)
        ]
        if collisions:
            raise RobinhoodIdentityCollisionError(
                "Robinhood security identities collide; every security needs its own "
                "CUSIP, ISIN and symbol:\n" + "\n".join(collisions)
            )

    @property
    def securities(self) -> tuple[RobinhoodSecurityConfig, ...]:
        """Configured entries in config order, then generated entries."""
        return (*self.configured, *self.generated)
