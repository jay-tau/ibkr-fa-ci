"""IBKR Activity Statement parser.

Sister package to :mod:`ibkr_fa.parser` for the Activity Statement
CSV format. See module-level docstring of
:mod:`ibkr_fa.parser.activity_statement.multi_section` for why this
exists as a separate parser.
"""

from __future__ import annotations

from ibkr_fa.parser.activity_statement.account_info import (
    parse_activity_statement_account_info,
)
from ibkr_fa.parser.activity_statement.dividends import (
    parse_activity_statement_dividends,
)
from ibkr_fa.parser.activity_statement.interest import (
    parse_activity_statement_interest,
)
from ibkr_fa.parser.activity_statement.multi_section import (
    KnownActivitySection,
    Section,
    SectionRows,
    dividend_evidence,
    split_activity_statement_sections,
)
from ibkr_fa.parser.activity_statement.open_positions import (
    build_dec31_close_prices,
    validate_open_positions_values,
    validate_reconciliation,
)
from ibkr_fa.parser.activity_statement.statement_period import (
    StatementPeriod,
    parse_statement_period,
)
from ibkr_fa.parser.activity_statement.trades import (
    FinancialInstrument,
    build_instrument_lookup,
    parse_activity_statement_trades,
)
from ibkr_fa.parser.activity_statement.withholding_tax import (
    parse_activity_statement_withholding_tax,
)

__all__ = [
    "FinancialInstrument",
    "KnownActivitySection",
    "Section",
    "SectionRows",
    "StatementPeriod",
    "build_dec31_close_prices",
    "build_instrument_lookup",
    "dividend_evidence",
    "parse_activity_statement_account_info",
    "parse_activity_statement_dividends",
    "parse_activity_statement_interest",
    "parse_activity_statement_trades",
    "parse_activity_statement_withholding_tax",
    "parse_statement_period",
    "split_activity_statement_sections",
    "validate_open_positions_values",
    "validate_reconciliation",
]
