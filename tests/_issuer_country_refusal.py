"""The parsers' refusal text for an issuer country that isn't in the bundled ITR
country-code table, shared by the Flex no-echo and IBKR refusal chain parser tests.

Tests pin this literal rather than reading it from :mod:`ibkr_fa`, so a changed
wording fails them.
"""

from __future__ import annotations

UNMAPPED_COUNTRY_RULE = (
    "the issuer country isn't in the bundled ITR country-code table. Add a row to "
    "src/ibkr_fa/reference/itr_country_codes.csv with the CBDT code from the ITR-2 "
    "utility's country dropdown."
)
