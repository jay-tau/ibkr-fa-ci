"""Shared input for the JSON recursion tests (#2005).

Python 3.14's recursion limit depends on stack size. In the issue's 3.14.10 probe,
100,000 levels raised ``RecursionError`` at 8 MiB and ``JSONDecodeError`` at 16/64 MiB;
1,000,000 raised ``RecursionError`` at 8/16/64 MiB and ``JSONDecodeError`` at
256 MiB/unlimited.
"""

from __future__ import annotations

from typing import Final

DEEP_JSON: Final[bytes] = b"[" * 1_000_000
