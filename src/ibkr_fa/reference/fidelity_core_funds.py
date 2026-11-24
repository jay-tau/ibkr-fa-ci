"""Fidelity core (cash sweep) money-market fund identities.

A Fidelity NetBenefits Stock Plan Services account sweeps cash into a core
money-market fund that pays its own dividends. Its statements print the fund
as ``FID TREASURY ONLY MMKT FUND CL OUS``: the Holdings ``Core Account`` row
shows the symbol ``(FYIXX)``, and the income rows show the CUSIP
``31617H821``. The statements print no ISIN, so :data:`FYIXX_ISIN` is the ISO
6166 ISIN for that CUSIP: ``"US"`` + CUSIP + the check digit from
:func:`ibkr_fa.types.isin_check_digit`.
"""

from __future__ import annotations

from typing import Final

from ibkr_fa.types import Isin, Ticker

FYIXX_CUSIP: Final[str] = "31617H821"
FYIXX_SYMBOL: Final[Ticker] = Ticker("FYIXX")
FYIXX_ISIN: Final[Isin] = Isin("US31617H8218")
