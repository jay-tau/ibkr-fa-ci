"""The read-time digit limits' refusal texts (#1275), shared by the reader tests.

Each is the one fixed message a reader raises, in its layer's error type, for
a number of that kind past its limits. Tests pin these literals rather than
reading :mod:`ibkr_fa.number_checks`, so a changed limit fails them.
"""

from __future__ import annotations

QUANTITY = "a quantity may have at most 12 digits before the decimal point and 20 after it"
MONEY = "a money amount may have at most 15 digits before the decimal point and 20 after it"
PRICE = "a unit price may have at most 9 digits before the decimal point and 20 after it"
RATE = "a rate may have at most 3 digits before the decimal point and 6 after it"
FX_RATE = "an FX rate may have at most 3 digits before the decimal point and 4 after it"
