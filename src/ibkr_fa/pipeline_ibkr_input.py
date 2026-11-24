"""Shared IBKR Activity Statement preamble for the FY pipelines (#609).

Schedule OS, Schedule CG, Schedule CG LTCG and the Schedule FSI compute
seam each start the same way. This module runs those steps once, in this
order:

1. ``load_user_config`` and :func:`_require_ibkr_account`, the account
   checks that Schedule FA also runs.
2. :func:`~ibkr_fa.config.effective_anonymisation_flags`.
3. ``detect_ibkr_csv_format``: anything but an Activity Statement raises
   :class:`~ibkr_fa.exceptions.ParseError` with the caller's message.
4. ``_dispatch_activity_statement`` with the anonymised fallbacks and,
   for the income commands, the ``Change in NAV`` income check (#1279).
5. ``_cross_check_account_id``.

Each caller passes its own period validator, broker validator, required
sections and error text. The pure FX-AY check stays with each caller.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ibkr_fa.config import UserConfig, effective_anonymisation_flags, load_user_config
from ibkr_fa.exceptions import ParseError
from ibkr_fa.models import SourceBundle
from ibkr_fa.parser.activity_statement import KnownActivitySection
from ibkr_fa.parser.activity_statement.statement_period import StatementPeriod
from ibkr_fa.parser.format_detect import IbkrCsvFormat, detect_ibkr_csv_format
from ibkr_fa.pipeline_dispatch import (
    _cross_check_account_id,
    _dispatch_activity_statement,
)
from ibkr_fa.types import AccountId

__all__ = ["_IbkrActivityInput", "_load_ibkr_activity_input", "_require_ibkr_account"]


@dataclass(frozen=True, slots=True)
class _IbkrActivityInput:
    """Everything the FY pipelines read from the shared preamble."""

    config: UserConfig
    input_format: IbkrCsvFormat
    extract: SourceBundle
    account_id_cross_checked: bool


def _require_ibkr_account(config: UserConfig) -> AccountId:
    """Return ``config``'s IBKR account number once its opening date and
    status are set.

    The checks run in this order, so the first one missing refuses with its
    :class:`~ibkr_fa.exceptions.ConfigError`: ``require_opening_date``,
    ``require_status``, ``require_number``. Schedule FA and
    :func:`_load_ibkr_activity_input` call it right after loading the config.
    """
    config.account.require_opening_date()
    config.account.require_status()
    return config.account.require_number()


def _load_ibkr_activity_input(
    *,
    input_path: Path,
    config_path: Path,
    period_validator: Callable[[StatementPeriod], None],
    broker_validator: Callable[[tuple[str, ...]], None] | None,
    required_sections: frozenset[KnownActivitySection],
    unsupported_format_message: Callable[[IbkrCsvFormat], str],
    allow_masked_account: bool,
    accept_anonymised_statement: bool,
    check_income_against_nav: bool,
) -> _IbkrActivityInput:
    """Load the config and the Activity Statement at ``input_path``.

    ``unsupported_format_message`` builds the :class:`ParseError` message
    for a detected format other than Activity Statement.
    ``check_income_against_nav`` is passed to the dispatcher: the commands
    that file income set it (#1279).
    """
    config = load_user_config(config_path)
    account_number = _require_ibkr_account(config)
    allow_masked_account, accept_anonymised_statement = effective_anonymisation_flags(
        config,
        allow_masked_account=allow_masked_account,
        accept_anonymised_statement=accept_anonymised_statement,
    )

    input_format = detect_ibkr_csv_format(input_path)
    if input_format is not IbkrCsvFormat.ACTIVITY_STATEMENT:
        raise ParseError(
            section="<input format>",
            row_index=0,
            column="<format detection>",
            message=unsupported_format_message(input_format),
            raw_value="",
        )

    extract = _dispatch_activity_statement(
        input_path,
        period_validator=period_validator,
        broker_validator=broker_validator,
        required_sections=required_sections,
        fallback_account_holder_name=(
            config.account.holder_name if accept_anonymised_statement else None
        ),
        fallback_account_id=(account_number if accept_anonymised_statement else None),
        check_income_against_nav=check_income_against_nav,
    )

    return _IbkrActivityInput(
        config=config,
        input_format=input_format,
        extract=extract,
        account_id_cross_checked=_cross_check_account_id(
            extract.account_info,
            config_account_number=account_number,
            allow_masked_account=allow_masked_account,
        ),
    )
