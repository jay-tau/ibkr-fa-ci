"""The whole-dollar grammar the three Robinhood readers share.

The account-activity CSV reader (:mod:`ibkr_fa.parser.robinhood`), the
brokerage statement reader (:mod:`ibkr_fa.parser.robinhood_statement.parser`)
and the Form 1042-S reader (:mod:`ibkr_fa.parser.robinhood_1042s`) each build
their money patterns from :data:`ROBINHOOD_WHOLE_DOLLARS`. The ``$`` marker, the
decimals and the sign, parentheses and blank rules around it stay with each reader.
"""

from __future__ import annotations

from typing import Final

# A whole-dollar part: 0, or digits grouped by commas in threes with no leading zero.
ROBINHOOD_WHOLE_DOLLARS: Final[str] = r"(?:0|[1-9][0-9]{0,2}(?:,[0-9]{3})*)"
