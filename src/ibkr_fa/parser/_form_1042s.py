"""Form 1042-S rules shared by the Robinhood and Fidelity CMA readers (#2046).

Both readers read the same IRS form, so they find its copy designation and
check its Box 10 the same way, with the same refusal texts. Each reader still
chooses the copy letters it accepts, decides when Box 10 is checked, and
raises the refusal through its own ``_fail``.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Final

from ibkr_fa.models.form_1042s import Form1042SCopyType

COPY_DESIGNATION_REFUSAL: Final[str] = "Form 1042-S copy designation is missing or duplicated"
BOX_10_REFUSAL: Final[str] = "Form 1042-S Boxes 7a/8/9 do not reproduce Box 10"


def copy_designation(page: str, letters: str) -> Form1042SCopyType | None:
    """Return the page's one ``COPY <letter>`` designation, or ``None``.

    ``letters`` lists the copy letters the reader accepts, such as ``"BC"``.
    The search covers the whole page, ignores case, and allows spaces or tabs
    between ``COPY`` and the letter. ``None`` means the page prints no
    accepted designation or more than one.
    """
    found = re.findall(rf"\bCOPY[ \t]+([{letters}])\b", page, re.IGNORECASE)
    if len(found) != 1:
        return None
    return Form1042SCopyType(found[0].upper())


def reproduces_box_10[N: (Decimal, int)](box7a: N, box8: N, box9: N, box10: N) -> bool:
    """Whether Boxes 7a + 8 - 9 equal Box 10, in the caller's own number type."""
    return box7a + box8 - box9 == box10
