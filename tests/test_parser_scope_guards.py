"""Tests for :mod:`ibkr_fa.parser._scope_guards` (#641).

The refusals themselves are covered per parser in
``tests/test_parser_activity_statement_trades.py``,
``tests/test_parser_open_positions.py`` and
``tests/test_parser_closed_lots.py``, and the USD check at every site in
``tests/test_parser_currency_refusals.py``.
"""

from __future__ import annotations

from ibkr_fa.fx.ttbr_loader import load_ttbr
from ibkr_fa.parser._scope_guards import _EARLIEST_TTBR_DATE


def test_earliest_ttbr_date_is_the_bundled_archive_start() -> None:
    """The parsers keep their own copy of the archive start, so a change
    to the bundled archive must fail here."""
    assert min(load_ttbr("USD")) == _EARLIEST_TTBR_DATE
