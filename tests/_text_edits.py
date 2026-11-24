"""The exact-once text edit the tests share."""

from __future__ import annotations


def replace_once(text: str, old: str, new: str) -> str:
    """``text`` with ``old`` replaced by ``new``; ``old`` must occur exactly once."""
    assert text.count(old) == 1, old
    return text.replace(old, new)
