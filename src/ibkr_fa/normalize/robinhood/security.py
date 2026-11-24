"""Resolve Robinhood source securities to strict configured identities."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Set
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ibkr_fa.config import RobinhoodSecurityConfig
from ibkr_fa.normalize.robinhood.accounts import (
    RobinhoodAccountsReconciliation,
)
from ibkr_fa.normalize.robinhood.activity import RobinhoodStatementReference
from ibkr_fa.normalize.robinhood.bundle import (
    RobinhoodReconciliationError,
)
from ibkr_fa.normalize.robinhood.identity_exceptions import (
    canonical_statement_cusip,
)


@dataclass(frozen=True, slots=True, repr=False, order=True)
class RobinhoodSecurityEventReference:
    """Resolvable CUSIP-bearing event identity inside the paired result."""

    account_index: int
    event_ordinal: int


class RobinhoodSecurityIdentityBlockerKind(StrEnum):
    """Evidence-authorized identity blockers."""

    MISSING_CONFIG = "missing-config"


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodSecurityIdentityBlocker:
    """One unresolved security represented only by safe source handles."""

    kind: RobinhoodSecurityIdentityBlockerKind
    event_references: tuple[RobinhoodSecurityEventReference, ...]
    observed_symbols: tuple[str, ...]
    """The report and statement symbols a configured entry for this CUSIP
    would resolve (#1497)."""


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodResolvedSecurity:
    """One configured identity and all source symbol provenance."""

    config_index: int
    event_references: tuple[RobinhoodSecurityEventReference, ...]
    observed_symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodSecurityIdentityResult:
    """Resolved identities and explicit missing-config blockers."""

    resolved: tuple[RobinhoodResolvedSecurity, ...]
    blockers: tuple[RobinhoodSecurityIdentityBlocker, ...]


def _fail(message: str) -> RobinhoodReconciliationError:
    return RobinhoodReconciliationError(message)


def canonical_statement_symbols(
    paired: RobinhoodAccountsReconciliation,
) -> dict[str, set[str]]:
    """The symbols statement activity rows print, by canonical CUSIP.

    A row's CUSIP is its first printed one, or the activity CUSIP when an
    accepted CUSIP transition covers the row (#1187).
    """

    statement_symbols: dict[str, set[str]] = defaultdict(set)
    for account in paired.accounts:
        for statement_index, statement in enumerate(account.activity.primary_bundle.statements):
            for row in statement.activities:
                if row.symbol is None or not row.cusips:
                    continue
                # #470 proves the first CUSIP belongs to this row; later values
                # can be page-boundary spillover from the following row.
                reference = RobinhoodStatementReference(statement_index, row.location)
                cusip = canonical_statement_cusip(
                    account.activity,
                    reference,
                    row.cusips[0],
                )
                statement_symbols[cusip].add(row.symbol)
    return statement_symbols


def replaced_statement_cusips(paired: RobinhoodAccountsReconciliation) -> frozenset[str]:
    """The printed CUSIPs that accepted CUSIP transitions replace with an
    activity CUSIP on statement rows."""

    return frozenset(
        use.statement_cusip
        for account in paired.accounts
        for use in account.activity.cusip_transition_uses
    )


def observed_symbol(
    cusip: str,
    statement_symbols: Mapping[str, Set[str]],
    report_symbols: Mapping[str, Set[str]],
    replaced: Set[str],
) -> str | None:
    """The one symbol ``cusip``'s source evidence names, or ``None``.

    A CUSIP in ``replaced`` names none: ADR 0006 leaves history that holds
    both CUSIPs of a transition unsupported. Otherwise one statement symbol
    wins. With no statement symbol, one report symbol is used. Two or more
    statement symbols stay ambiguous: report evidence never settles their
    conflict (#1187).
    """

    if cusip in replaced:
        return None
    statement = statement_symbols.get(cusip, frozenset())
    report = report_symbols.get(cusip, frozenset())
    if len(statement) == 1:
        return next(iter(statement))
    if not statement and len(report) == 1:
        return next(iter(report))
    return None


def _observed_security_evidence(
    paired: RobinhoodAccountsReconciliation,
) -> tuple[
    dict[str, list[RobinhoodSecurityEventReference]],
    dict[str, set[str]],
    dict[str, set[str]],
]:
    references: dict[str, list[RobinhoodSecurityEventReference]] = defaultdict(list)
    report_symbols: dict[str, set[str]] = defaultdict(set)
    symbol_cusips: dict[str, set[str]] = defaultdict(set)

    for account_index, account in enumerate(paired.accounts):
        for event in account.evidence.events:
            if event.cusip is None:
                continue
            references[event.cusip].append(
                RobinhoodSecurityEventReference(account_index, event.source_record_index)
            )
            if event.instrument is not None:
                report_symbols[event.cusip].add(event.instrument)
                symbol_cusips[event.instrument].add(event.cusip)
    statement_symbols = canonical_statement_symbols(paired)

    if any(len(cusips) > 1 for cusips in symbol_cusips.values()):
        raise _fail("one source symbol resolves to multiple CUSIPs")
    return references, report_symbols, statement_symbols


def resolve_robinhood_security_identities(
    paired: RobinhoodAccountsReconciliation,
    securities: tuple[RobinhoodSecurityConfig, ...],
) -> RobinhoodSecurityIdentityResult:
    """Resolve every observed CUSIP or retain a typed missing-config blocker."""

    references, report_symbols, statement_symbols = _observed_security_evidence(paired)
    configured = {security.cusip: (index, security) for index, security in enumerate(securities)}
    resolved: list[RobinhoodResolvedSecurity] = []
    blockers: list[RobinhoodSecurityIdentityBlocker] = []

    for cusip, source_references in references.items():
        ordered_references = tuple(sorted(source_references))
        observed_symbols = report_symbols[cusip] | statement_symbols[cusip]
        configured_entry = configured.get(cusip)
        if configured_entry is None:
            blockers.append(
                RobinhoodSecurityIdentityBlocker(
                    kind=RobinhoodSecurityIdentityBlockerKind.MISSING_CONFIG,
                    event_references=ordered_references,
                    observed_symbols=tuple(sorted(observed_symbols)),
                )
            )
            continue
        config_index, security = configured_entry
        if security.instrument.symbol not in observed_symbols:
            raise _fail("configured canonical symbol contradicts source evidence")
        resolved.append(
            RobinhoodResolvedSecurity(
                config_index=config_index,
                event_references=ordered_references,
                observed_symbols=tuple(sorted(observed_symbols)),
            )
        )

    return RobinhoodSecurityIdentityResult(
        resolved=tuple(sorted(resolved, key=lambda item: item.config_index)),
        blockers=tuple(sorted(blockers, key=lambda blocker: blocker.event_references[0])),
    )


_TOML_ESCAPES: Final[Mapping[str, str]] = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}
_SURROGATES: Final[range] = range(0xD800, 0xE000)


def toml_string(value: str) -> str:
    """``value`` as a TOML basic string that reads back unchanged.

    Raises:
        RobinhoodReconciliationError: if ``value`` holds a lone surrogate,
            which no TOML document can hold. The message never prints the value.
    """
    parts = []
    for character in value:
        code = ord(character)
        if code in _SURROGATES:
            raise _fail("a suggested security identity value cannot be written as TOML")
        if character in _TOML_ESCAPES:
            parts.append(_TOML_ESCAPES[character])
        elif code < 0x20 or code == 0x7F:  # noqa: PLR2004 - TOML's control characters
            parts.append(f"\\u{code:04X}")
        else:
            parts.append(character)
    return '"' + "".join(parts) + '"'


def render_robinhood_security_toml(
    paired: RobinhoodAccountsReconciliation,
    *,
    securities: tuple[RobinhoodSecurityConfig, ...] = (),
    failure_checks: Mapping[str, tuple[str, ...]] | None = None,
) -> str:
    """Return a deterministic private registry skeleton; never write it.

    Omit the identities already in ``securities``. Each statement value is
    written as an escaped TOML basic string, so it reads back unchanged.

    Raises:
        RobinhoodReconciliationError: if a value cannot be written as TOML;
            the message never prints it.
    """

    references, report_symbols, statement_symbols = _observed_security_evidence(paired)
    replaced = replaced_statement_cusips(paired)
    configured = {security.cusip for security in securities}
    blocks = []
    for cusip in sorted(set(references) - configured):
        checks = () if failure_checks is None else failure_checks.get(cusip, ())
        symbol = observed_symbol(cusip, statement_symbols, report_symbols, replaced)
        blocks.append(
            "\n".join(
                (
                    *(("# identity_checks_failed = " + ", ".join(checks),) if checks else ()),
                    "[[robinhood.securities]]",
                    "symbol = " + toml_string("REPLACE" if symbol is None else symbol),
                    f"cusip = {toml_string(cusip)}",
                    'isin = "REPLACE"',
                    'listing_exchange = "REPLACE"',
                    'sub_category = "REPLACE"',
                    'asset_class = "REPLACE"',
                    'issuer_country_code = "REPLACE"',
                    (
                        'is_depositary_receipt = "REPLACE"'
                        "  # replace with a bare true or false, without quotes"
                    ),
                    'source = "REPLACE"',
                )
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")
