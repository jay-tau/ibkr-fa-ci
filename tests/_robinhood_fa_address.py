"""Robinhood Schedule FA address texts that more than one test module pins (#1718).

Each is written once here, so a wording change is re-pinned in one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

# The warning when automatic legal-address evidence replaces one configured
# [[fa.entities]] address.
ADDRESS_SUPERSEDED_ONE: Final = (
    "automatic legal-address evidence superseded 1 configured Robinhood entity address; "
    "set prefer_configured_address = true to retain reviewed values"
)


def address_stop(guidance: Path | str, *, nature: bool = False) -> str:
    """The two-line stop when an [[fa.entities]] entry for a held security has
    no address (#1449), naming the private guidance file ``guidance``. With
    ``nature`` it also asks for the entry's missing nature (#1568)."""
    nature_clause = (
        "set nature_of_entity to the issuer's legal entity type if the block has none, "
        if nature
        else ""
    )
    return (
        "Robinhood Schedule FA was not written: one or more [[fa.entities]] entries for "
        "securities held in the calendar year have no address\n"
        "the address found for each, or a note that none was found, is in the private "
        f"file {guidance}: verify each address against the issuer's own filings, uncomment "
        f"its address and zip lines, {nature_clause}replace that entry in user.toml with the "
        "block, and re-run"
    )
