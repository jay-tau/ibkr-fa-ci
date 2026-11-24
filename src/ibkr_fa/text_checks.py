"""Checks on filed text, shared by the config loader and the output writers.

A filed text value may not contain a control character. ``user.toml`` refuses
one when it loads (#818); the CBDT JSON builder and ``ibkr-fa merge`` refuse
one in the fragment (#798), which also covers text from statements. A Schedule
FA Table A3 row refuses one in its name, address, ZIP or nature when it is
built (#1367), so every Table A3 output format does.

This module imports nothing from :mod:`ibkr_fa`, so every layer can use it.
"""

from __future__ import annotations


def first_control_character(value: str) -> tuple[int, str] | None:
    """The index and character of the first control character in ``value`` (a
    character below a space, or DEL), or ``None``."""
    return next(((i, ch) for i, ch in enumerate(value) if ch < " " or ch == "\x7f"), None)
