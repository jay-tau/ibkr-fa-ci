"""Strict, account-scoped exceptions for stale statement CUSIPs."""

from __future__ import annotations

import re
import tomllib
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import cast

from ibkr_fa.config import toml_error_message
from ibkr_fa.exceptions import ConfigError, errno_name
from ibkr_fa.normalize.robinhood.activity import (
    ROBINHOOD_CUSIP_CONTRADICTION,
    RobinhoodActivityReconciliation,
    RobinhoodCusipTransitionUse,
    RobinhoodStatementReference,
)
from ibkr_fa.normalize.robinhood.bundle import RobinhoodReconciliationError
from ibkr_fa.parser.robinhood import RobinhoodEvent
from ibkr_fa.parser.robinhood_statement import RobinhoodStatementActivity
from ibkr_fa.types import CUSIP_PATTERN, cusip_check_digit_matches

_FIELD = "robinhood.cusip_transitions_file"
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]*\Z")
_KEYS = frozenset(
    {
        "account_number",
        "symbol",
        "old_cusip",
        "new_cusip",
        "new_cusip_from",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodCusipTransition:
    account_number: str
    symbol: str
    old_cusip: str
    new_cusip: str
    new_cusip_from: date


def _cusip_valid(value: object) -> bool:
    if not isinstance(value, str) or CUSIP_PATTERN.fullmatch(value) is None:
        return False
    return cusip_check_digit_matches(value)


def _read_document(path: Path, shown: str | Path) -> dict[str, object]:
    """The TOML document at ``path``.

    A read failure names the file as ``shown`` and the errno name; an invalid
    file gives fixed text and, for TOML, the line and column when known. Never the file's
    text or the reader's message, which can quote a key (#1044), and no
    chained exception.
    """
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        message = f"could not read {shown} ({errno_name(exc)})"
    except UnicodeDecodeError as exc:
        message = (
            f"must be UTF-8 text, but the byte at offset {exc.start} (counting from 0) "
            "is not valid UTF-8"
        )
    except tomllib.TOMLDecodeError as exc:
        message = toml_error_message(exc)
    except (RecursionError, ValueError):
        message = "TOML exceeds supported parsing limits"
    # Refused outside the handler: a decode error holds the file's bytes, and a
    # TOML error its text (a duplicate key, or the whole document on 3.14).
    raise ConfigError(_FIELD, message)


def load_cusip_transitions(
    path: Path | None,
    *,
    account_numbers: frozenset[str],
    configured_as: str | None = None,
) -> tuple[RobinhoodCusipTransition, ...]:
    """Load the transitions file at ``path``.

    ``configured_as`` is the setting as the user wrote it, which a read
    failure names (``path`` when not given).

    Raises:
        ConfigError: if the file can't be read or isn't valid.
    """
    if path is None:
        return ()
    document = _read_document(path, path if configured_as is None else configured_as)
    if set(document) != {"transitions"} or not isinstance(document["transitions"], list):
        raise ConfigError(_FIELD, "must contain only a [[transitions]] array of tables")

    transitions: list[RobinhoodCusipTransition] = []
    seen: set[tuple[str, str]] = set()
    for index, raw_value in enumerate(document["transitions"]):
        field = f"cusip_transitions.transitions[{index}]"
        if not isinstance(raw_value, dict) or set(raw_value) != _KEYS:
            raise ConfigError(field, f"must contain exactly {', '.join(sorted(_KEYS))}")
        raw = cast("dict[str, object]", raw_value)
        account_number = raw["account_number"]
        symbol = raw["symbol"]
        old_cusip = raw["old_cusip"]
        new_cusip = raw["new_cusip"]
        new_from = raw["new_cusip_from"]
        if not isinstance(account_number, str) or account_number not in account_numbers:
            raise ConfigError(f"{field}.account_number", "must match a configured account")
        if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
            raise ConfigError(f"{field}.symbol", "must be an uppercase ASCII ticker")
        if (
            not isinstance(old_cusip, str)
            or not isinstance(new_cusip, str)
            or not _cusip_valid(old_cusip)
            or not _cusip_valid(new_cusip)
        ):
            raise ConfigError(f"{field}.old_cusip/new_cusip", "must be valid CUSIPs")
        if old_cusip == new_cusip:
            raise ConfigError(f"{field}.new_cusip", "must differ from old_cusip")
        if not isinstance(new_from, date) or isinstance(new_from, datetime):
            raise ConfigError(f"{field}.new_cusip_from", "must be a TOML local date")
        key = (account_number, symbol)
        if key in seen:
            raise ConfigError(field, "duplicates another account/ticker transition")
        seen.add(key)
        transitions.append(
            RobinhoodCusipTransition(
                account_number,
                symbol,
                old_cusip,
                new_cusip,
                new_from,
            )
        )
    return tuple(transitions)


def configured_cusip_transitions_path(config_path: Path, configured_path: str) -> Path:
    """``robinhood.cusip_transitions_file``, relative to the config's directory."""
    path = Path(configured_path)
    return path if path.is_absolute() else config_path.parent / path


def load_configured_cusip_transitions(
    config_path: Path,
    configured_path: str | None,
    *,
    account_numbers: frozenset[str],
) -> tuple[RobinhoodCusipTransition, ...]:
    if configured_path is None:
        return ()
    return load_cusip_transitions(
        configured_cusip_transitions_path(config_path, configured_path),
        account_numbers=account_numbers,
        configured_as=configured_path,
    )


def cusip_transition_warnings(references: tuple[str, ...]) -> tuple[str, ...]:
    if not references:
        return ()
    joined = ", ".join(references)
    return (
        f"{len(references)} statement CUSIP contradiction"
        f"{'s were' if len(references) != 1 else ' was'} accepted by configured "
        "account/ticker/date transitions; activity CUSIP remained authoritative "
        "and the statement corroborated symbol only. Internal "
        "account_index:event_ordinal references (not account numbers): "
        f"{joined}.",
    )


def _statement_rows(
    reconciliation: RobinhoodActivityReconciliation,
) -> dict[RobinhoodStatementReference, RobinhoodStatementActivity]:
    return {
        RobinhoodStatementReference(statement_index, row.location): row
        for statement_index, statement in enumerate(reconciliation.primary_bundle.statements)
        for row in statement.activities
    }


def apply_cusip_transitions(
    reconciliation: RobinhoodActivityReconciliation,
    events: tuple[RobinhoodEvent, ...],
    *,
    account_number: str,
    transitions: tuple[RobinhoodCusipTransition, ...],
) -> RobinhoodActivityReconciliation:
    configured = {
        item.symbol: item for item in transitions if item.account_number == account_number
    }
    event_by_ordinal = {event.source_record_index: event for event in events}
    row_by_reference = _statement_rows(reconciliation)
    uses: list[RobinhoodCusipTransitionUse] = []
    groups = (
        *reconciliation.activity_groups,
        *reconciliation.in_kind_transfer_groups,
        *reconciliation.pending_trade_groups,
    )
    for group in groups:
        grouped_events = [event_by_ordinal[ordinal] for ordinal in group.event_ordinals]
        grouped_rows = [row_by_reference[reference] for reference in group.statement_references]
        symbols = {event.instrument for event in grouped_events if event.instrument is not None} & {
            row.symbol for row in grouped_rows if row.symbol is not None
        }
        for symbol in sorted(symbols):
            symbol_events = [event for event in grouped_events if event.instrument == symbol]
            symbol_rows = [row for row in grouped_rows if row.symbol == symbol]
            if any(event.cusip is None for event in symbol_events) or any(
                not getattr(row, "cusips", ()) for row in symbol_rows
            ):
                continue
            event_cusips = Counter(cast("str", event.cusip) for event in symbol_events)
            statement_cusips = Counter(row.cusips[0] for row in symbol_rows)
            if not statement_cusips - event_cusips:
                continue
            transition = configured.get(symbol)
            if (
                transition is None
                or len(event_cusips) != 1
                or len(statement_cusips) != 1
                or sum(event_cusips.values()) != sum(statement_cusips.values())
            ):
                raise RobinhoodReconciliationError(ROBINHOOD_CUSIP_CONTRADICTION)
            event_cusip = next(iter(event_cusips))
            statement_cusip = next(iter(statement_cusips))
            event_dates = {event.activity_date for event in symbol_events}
            statement_dates = {row.transaction_date for row in symbol_rows}
            if len(event_dates) != 1 or event_dates != statement_dates:
                raise RobinhoodReconciliationError(
                    "statement CUSIP contradiction lacks one exact transaction date"
                )
            event_date = next(iter(event_dates))
            expected = (
                transition.old_cusip
                if event_date < transition.new_cusip_from
                else transition.new_cusip
            )
            if event_cusip != expected or {event_cusip, statement_cusip} != {
                transition.old_cusip,
                transition.new_cusip,
            }:
                raise RobinhoodReconciliationError(
                    "statement CUSIP contradiction is not covered by the configured transition"
                )
            uses.append(
                RobinhoodCusipTransitionUse(
                    symbol=symbol,
                    activity_cusip=event_cusip,
                    statement_cusip=statement_cusip,
                    event_ordinals=tuple(
                        sorted(event.source_record_index for event in symbol_events)
                    ),
                    statement_references=tuple(
                        sorted(
                            (
                                group.statement_references[index]
                                for index, row in enumerate(grouped_rows)
                                if row.symbol == symbol
                            ),
                            key=lambda reference: (
                                reference.statement_index,
                                reference.location.page,
                                reference.location.row,
                            ),
                        )
                    ),
                )
            )
    return replace(reconciliation, cusip_transition_uses=tuple(uses))


def canonical_statement_cusip(
    reconciliation: RobinhoodActivityReconciliation,
    reference: RobinhoodStatementReference,
    printed_cusip: str,
) -> str:
    for use in reconciliation.cusip_transition_uses:
        if reference in use.statement_references:
            if printed_cusip != use.statement_cusip:
                raise RobinhoodReconciliationError(
                    "CUSIP transition use contradicts its statement row"
                )
            return use.activity_cusip
    return printed_cusip
