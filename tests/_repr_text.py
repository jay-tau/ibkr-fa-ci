"""The ``repr`` text a privacy check scans, without memory addresses (#1803).

A ``repr=False`` dataclass or plain class shows Python's default
``<module.Type object at 0x7f...>``. Its address is a run of hex digits
that can vary between allocations, so a marker made only of hex digits,
such as ``"0001"``, can turn up in it by chance and fail a ``not in`` check
that is otherwise right.
"""

from __future__ import annotations

import re

_ADDRESS = re.compile(r" at 0x[0-9A-Fa-f]+>")


def repr_without_addresses(value: object) -> str:
    """``repr(value)`` with every default repr's `` at 0x...`` address
    dropped, ``<module.Type object>`` instead; all other text is kept."""
    return _ADDRESS.sub(">", repr(value))
