"""IBKR Flex Query CSV section parsers.

The Flex Query CSV is a concatenation of multiple sections — each
section has its own header row followed by data rows for that
section's schema. Sections are identified by the full set of column
names in the header row (IBKR documents these schemas in its Flex
Query Reference).

This package's :mod:`~ibkr_fa.parser.multi_section` module splits
a CSV file into named sections. Per-section parsers (e.g., for
``Open Positions``, ``Closed Lots``, ``Account Information``) live in
their own modules and consume the section's row dicts.
"""

from __future__ import annotations

from ibkr_fa.parser.account_info import parse_account_info
from ibkr_fa.parser.closed_lots import parse_closed_lots
from ibkr_fa.parser.multi_section import (
    FlexQuery,
    KnownSection,
    Section,
    SectionRows,
    split_flex_query,
    split_sections,
)
from ibkr_fa.parser.open_positions import parse_open_positions

__all__ = [
    "FlexQuery",
    "KnownSection",
    "Section",
    "SectionRows",
    "parse_account_info",
    "parse_closed_lots",
    "parse_open_positions",
    "split_flex_query",
    "split_sections",
]
