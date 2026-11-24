"""The Robinhood Schedule FA actions #1483 words, as the CLI prints them.

Each ``a3-identity`` cause line ends in a colon and lists its holdings, one
``SYMBOL (ISIN)`` per line: :func:`listed` builds the whole action.
:func:`peak_action` builds #1476's ``a3-peak`` action for a blank Table A3 peak.
"""

from __future__ import annotations

from ibkr_fa.models import FaA3PeakBlankReason
from ibkr_fa.output.fa_manual_fill import FA_A3_PEAK_VALUE_COLUMN
from tests._fa_blank_texts import A3_PEAK_BLANK_TEXT
from tests._robinhood_refusals import PRICE_CACHE_MOVE

_VERIFIED = "verified against its own filings"
_NO_ADDRESS = "no issuer address was found and no [[fa.entities]] entry gives one"
_ADDRESS_REMEDY = (
    f"Set address and zip to the issuer's legal address and postal code, {_VERIFIED}, in an "
    "[[fa.entities]] entry for each ISIN and re-run, or fill them by hand before filing:"
)
ADDRESS_AND_ZIP = (
    "a3-identity: Table A3 Address of entity and ZIP Code are blank for these holdings because "
    f"{_NO_ADDRESS}. {_ADDRESS_REMEDY}"
)
ZIP = (
    "a3-identity: Table A3 ZIP Code is blank for these holdings because the issuer address "
    f"filed has no postal code. Set zip to the issuer's postal code, {_VERIFIED}, with the "
    "review file's Address of entity as address and prefer_configured_address = true, in the "
    "[[fa.entities]] entry for each ISIN and re-run, or fill it by hand before filing:"
)
NATURE = (
    "a3-identity: Table A3 Nature of entity is blank for these holdings because no legal form "
    "was found for the issuer and no [[fa.entities]] entry sets nature_of_entity. Set "
    f"nature_of_entity to the issuer's legal entity type, {_VERIFIED}, in the [[fa.entities]] "
    "entry for each ISIN, with the review file's Address of entity and ZIP Code copied into "
    "address and zip (the issuer's own if they are blank; an entry without address stops the "
    "next run until you verify the address it finds), and re-run, or fill it by hand before "
    "filing:"
)
ETF_NAME = (
    "a3-identity: Table A3 Name of entity is the broker's description, not the legal name, for "
    "these series ETFs. An ETF with an [[fa.entities]] entry takes only that entry's cells "
    "(ADR 0011), so set name (the registrant trust's legal name and the fund's series name), "
    "address and zip (the trust's legal address) and nature_of_entity in the entry for each "
    "ISIN, verified against the fund's own filings, and re-run, or fill them by hand before "
    "filing:"
)
OPTION_TABLE_D_ACTION = (
    "option-table-d: options in an account whose [[robinhood.option_classifications]] "
    'treatment is "capital-gains" are other capital assets for Schedule FA Table D, which '
    "ibkr-fa doesn't write. Enter each option contract the account held at any time in the "
    "calendar year, including any closed before 31 December, in Table D by hand before filing"
)


def listed(header: str, *holdings: str) -> str:
    """``header`` followed by each ``SYMBOL (ISIN)`` holding on its own line."""
    return header + "".join(f"\n    - {holding}" for holding in holdings)


# The Robinhood remedy for a session without a high, which replaces the
# generic one in A3_PEAK_BLANK_TEXT.
_ROBINHOOD_SESSION_WITHOUT_HIGH_REMEDY = (
    f"{PRICE_CACHE_MOVE}: a cached series is reused even without highs. Or set "
    '[schedule_fa] peak_basis = "close", or enter it by hand. A ticker override won\'t help.'
)


def peak_action(reason: FaA3PeakBlankReason, *rows: int) -> str:
    """The ``a3-peak`` action the Robinhood Schedule FA commands print for
    ``reason``'s A3 ``rows``."""
    cause, remedy = A3_PEAK_BLANK_TEXT[reason]
    if reason is FaA3PeakBlankReason.SESSION_WITHOUT_HIGH:
        remedy = _ROBINHOOD_SESSION_WITHOUT_HIGH_REMEDY
    return (
        f"a3-peak: the Table A3 {FA_A3_PEAK_VALUE_COLUMN} in row(s) "
        f"{', '.join(map(str, rows))} is blank because {cause}. {remedy}"
    )
