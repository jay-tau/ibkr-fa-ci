"""A Statement Period for parser tests whose rows' dates don't matter (#1143).

:func:`statement_field_row` builds a ``Field Name`` / ``Field Value`` row, as the
Statement section lists its fields, for the Statement Period parser and IBKR
refusal chain tests.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from ibkr_fa.models import StatementPeriod

ALL_DATES: Final[StatementPeriod] = StatementPeriod(start_date=date.min, end_date=date.max)
"""A Period that contains every date, so no valid date is outside it."""


def statement_field_row(name: str, value: str) -> dict[str, str]:
    return {"Field Name": name, "Field Value": value}
