"""The Robinhood Schedule FA blocker harness (#1550), shared by the Schedule FA
blocker-reason, first-refusal and pre-price-stop tests (#1815).

:func:`load_blocker_accounts` makes every command load two accounts reconciled
from synthetic statements and activity reports, and
:func:`load_dec31_unresolved` loads #1246's Dec-31 pending-trade bundle with no
identity resolution. :func:`resolved_bundle` resolves a held security's Table
A3 identity from one synthetic GLEIF record, and :func:`empty_price_cache`
makes the FA price step read an empty price cache. :data:`FA_BLOCKER_CASES`
holds each reachable kind's loader and refusal text, and
:func:`schedule_fa_leg` runs ``robinhood-all``'s Schedule FA leg. Synthetic
inputs only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
import ibkr_fa.pipeline_robinhood_entities as entities
from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.config import RobinhoodConfig, load_user_config
from ibkr_fa.models.reference_identity import RegistrantLookup
from ibkr_fa.normalize.robinhood import (
    RobinhoodActivityReportEvidence,
    reconcile_robinhood_accounts,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.parser.robinhood import RobinhoodEvent
from ibkr_fa.parser.robinhood_statement import RobinhoodStatement, RobinhoodStatementFamily
from ibkr_fa.pipeline_robinhood import run_robinhood_all
from ibkr_fa.pipeline_robinhood_bundle import RobinhoodBundle
from ibkr_fa.prices.loader import load_price_series
from ibkr_fa.robinhood_legal_entity_sources import load_robinhood_legal_entity_evidence
from ibkr_fa.types import Ticker
from tests._entity_transport import FixedReplyOpener as _RecordingOpener
from tests._robinhood_a3_exact import DESCRIPTION, SYMBOL
from tests._robinhood_a3_load import (
    a3_bundle,
    flat_ttbr,
    in_symbol,
    serve_closes,
    year_and_quarter_statements,
)
from tests._robinhood_cg import SAME_DAY_EVIDENCE, SAME_DAY_LOT_CAUSE
from tests._robinhood_cli import FINANCIAL_YEAR as _FY
from tests._robinhood_fa_blocker_lines import ANCHOR as _ANCHOR
from tests._robinhood_fa_blocker_lines import BY_HAND as _BY_HAND
from tests._robinhood_fa_blocker_lines import CLOSING_CAUSE, CREDIT_CAUSE
from tests._robinhood_fa_blocker_lines import FA_UNRESOLVED as _FA
from tests._robinhood_fa_blocker_lines import INVENTORY_INCOMPLETE as _INVENTORY
from tests._robinhood_fa_blocker_lines import NOT_WRITTEN as _NOT_WRITTEN
from tests._robinhood_fa_blocker_lines import REFERENCES as _REFERENCES
from tests._robinhood_fa_blocker_lines import REPORT_ROUTE as _REPORT_ROUTE
from tests._robinhood_fa_inputs import FA_CUSIP as _CUSIP
from tests._robinhood_fa_inputs import FA_ISIN as _ISIN
from tests._robinhood_fa_inputs import FA_OPENING as _OPENING
from tests._robinhood_fa_inputs import fa_account as _account
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_fa_inputs import fa_cash as _cash
from tests._robinhood_fa_inputs import fa_dividend as _dividend
from tests._robinhood_fa_inputs import fa_sale as _sale
from tests._robinhood_fa_inputs import fa_statement as _statement
from tests._robinhood_gleif import issuer_record as _record
from tests._robinhood_gleif import issuer_reply as _response
from tests._robinhood_income_inputs import income_packet as _packet
from tests._robinhood_pending import DEC31_REPRO, dec31_trade, load_dec31_accounts
from tests._robinhood_security import (
    robinhood_security,
)

_FY_END = date(2026, 3, 31)
_JANUARY = date(2025, 1, 10)
_STATEMENT_ROUTE = (
    f"{_NOT_WRITTEN}: supply Robinhood's statements for the periods ending on 31 December "
    "and starting on 1 January in place of one that spans both and re-run, or complete the "
    "whole Robinhood Schedule FA by hand."
)


def buy_at_row(row: int, quantity: str, price: str, day: date = _JANUARY) -> RobinhoodEvent:
    """A buy at activity report row ``row``, settling the same day."""
    return replace(
        _buy(quantity),
        source_record_index=row,
        activity_date=day,
        process_date=day,
        settle_date=day,
        price_usd=Decimal(price),
        amount_usd=-(Decimal(quantity) * Decimal(price)),
    )


def sale_at_row(
    row: int, quantity: str, price: str, day: date = date(2025, 6, 13)
) -> RobinhoodEvent:
    """A sale at activity report row ``row``, settling the same day."""
    return replace(
        _sale(),
        source_record_index=row,
        activity_date=day,
        process_date=day,
        settle_date=day,
        quantity=Decimal(quantity),
        price_usd=Decimal(price),
        amount_usd=Decimal(quantity) * Decimal(price),
    )


def managed_statements(deposit: tuple[RobinhoodEvent, ...]) -> tuple[RobinhoodStatement, ...]:
    """The managed account's usual statements: CY 2025, then Q1 2026."""
    return year_and_quarter_statements(
        "222222222",
        RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
        deposit,
        cash=Decimal(5),
        quantity=Decimal(),
    )


def spanning_statements(deposit: tuple[RobinhoodEvent, ...]) -> tuple[RobinhoodStatement, ...]:
    """The managed account's cash-only statements to 30 November 2025, from 1
    December 2025 to 31 January 2026, and to the FY's end: none ends on 31
    December."""
    family = RobinhoodStatementFamily.MANAGED_INDIVIDUAL
    first = _statement(
        "222222222",
        family,
        deposit,
        cash=Decimal(5),
        position_quantity=Decimal(),
        end=date(2025, 11, 30),
    )
    later = (
        (date(2025, 12, 1), date(2026, 1, 31)),
        (date(2026, 2, 1), _FY_END),
    )
    return (
        first,
        *(
            _statement(
                "222222222",
                family,
                (),
                cash=Decimal(5),
                position_quantity=Decimal(),
                start=start,
                end=end,
                opening_cash=Decimal(5),
                opening_securities=Decimal(),
            )
            for start, end in later
        ),
    )


def load_blocker_accounts(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    *events: RobinhoodEvent,
    held: Decimal = Decimal(2),
    dividends: Decimal = Decimal(),
    managed: Callable[
        [tuple[RobinhoodEvent, ...]], tuple[RobinhoodStatement, ...]
    ] = managed_statements,
    position_symbol: str = SYMBOL,
    securities: bool = True,
) -> SimpleNamespace:
    """Make every command load two reconciled accounts, as
    ``load_two_accounts`` does: the first makes ``events`` in ``SYMBOL`` and its
    statements show ``held`` shares under ``position_symbol``; the second holds
    $5 of cash in ``managed``'s statements. The security is configured unless
    ``securities`` is false. Returns the bundle."""
    marked = tuple(
        replace(event, instrument=SYMBOL, description=DESCRIPTION)
        if event.instrument is not None
        else event
        for event in events
    )
    deposit = (_cash(),)
    cash = sum((event.amount_usd or Decimal() for event in marked), Decimal())
    year, quarter = year_and_quarter_statements(
        "111111111",
        RobinhoodStatementFamily.INDIVIDUAL,
        marked,
        cash=cash,
        quantity=held,
        dividends=dividends,
    )
    statements = tuple(in_symbol(statement, position_symbol) for statement in (year, quarter))
    accounts = (_account("111111111"), _account("222222222"))
    paired = reconcile_robinhood_accounts(
        validate_robinhood_statement_bundles((*statements, *managed(deposit))),
        tuple(
            RobinhoodActivityReportEvidence(events=items, start_date=_OPENING, end_date=_FY_END)
            for items in (marked, deposit)
        ),
        accounts,
    )
    security = robinhood_security(
        symbol=SYMBOL, cusip=_CUSIP, isin=_ISIN, source="synthetic issuer evidence"
    )
    config = RobinhoodConfig(accounts=accounts, securities=(security,) if securities else ())
    bundle = a3_bundle(paired, config, run_dir)
    bundle.identity_resolution = None
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    flat_ttbr(monkeypatch, Decimal("84.15"))
    serve_closes(monkeypatch, SYMBOL, requested_only=True)
    return bundle


def load_dec31_unresolved(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    trades: tuple[tuple[RobinhoodEvent, ...], RobinhoodEvent],
    **changes: Any,
) -> None:
    """#1246's Dec-31 pending-trade bundle, with no identity resolution, as
    the loader leaves it when every security is configured, and ``changes``
    to its load."""
    load_dec31_accounts(monkeypatch, run_dir, trades, **changes)
    load = cast("Callable[..., Any]", pipeline._load)

    def loaded(**kwargs: object) -> Any:
        bundle = load(**kwargs)
        bundle.identity_resolution = None
        return bundle

    monkeypatch.setattr(pipeline, "_load", loaded)


def empty_price_cache(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Ticker, ...]]:
    """Make the FA price step read the run's empty price cache, as a
    ``--no-fetch-prices`` run with no cached closes does, and return the
    tickers each read asks for. The caller fixes the prices by restoring the
    closes the harness serves (#1574)."""
    reads: list[tuple[Ticker, ...]] = []

    def read(windows: Mapping[Ticker, tuple[date, date]], **kwargs: Any) -> Any:
        reads.append(tuple(sorted(windows)))
        return load_price_series(windows, **kwargs)

    monkeypatch.setattr("ibkr_fa.pipeline_robinhood_equity_prices.load_price_series", read)
    return reads


def resolved_bundle(
    monkeypatch: pytest.MonkeyPatch, run_dir: Path, stub: SimpleNamespace
) -> RobinhoodBundle:
    """Make every command load ``stub``'s statements, reports and securities
    as a real :class:`RobinhoodBundle` with ``run_dir``'s user.toml, and
    return it. The pipeline's own resolver finds each held security's Table
    A3 identity from one active GLEIF record (an address, ZIP 99999 and a
    legal form that gives a nature), as the address-verification tests do:
    no stub supplies a cell that the user.toml and that record don't (#1574)."""
    config = load_user_config(run_dir / "user.toml")
    held = frozenset(item.cusip for item in stub.robinhood.securities)
    monkeypatch.setattr(entities, "_held_cusips", lambda *_args: held)
    for loader in ("load_robinhood_adr_entity_evidence", "load_robinhood_fund_entity_evidence"):
        monkeypatch.setattr(
            entities, loader, lambda *_args, **_kwargs: RegistrantLookup((), 0, 0, 0)
        )
    monkeypatch.setattr(
        entities,
        "load_robinhood_legal_entity_evidence",
        partial(
            load_robinhood_legal_entity_evidence,
            opener=_RecordingOpener(_response([_record(postal_code="99999")])),
        ),
    )
    presentation = entities.resolve_robinhood_presentation_evidence(
        robinhood=stub.robinhood,
        securities=stub.robinhood.securities,
        configured=config.fa.entities,
        observed=(),
        manual_fields={},
        identity_sources=None,
        quantities=stub.quantities,
        calendar_year=_FY.start_year,
        cache_dir=run_dir / "identity-cache",
        fetch=True,
    )
    bundle = RobinhoodBundle(
        config=config,
        robinhood=stub.robinhood,
        securities=stub.securities,
        paired=stub.paired,
        identities=stub.identities,
        packets=stub.packets,
        quantities=stub.quantities,
        scope=stub.scope,
        automatic_entity_overrides=presentation.entity_overrides,
        unverified_entity_addresses=presentation.unverified_addresses,
        legal_address_audit=presentation.legal_address_audit,
        automatic_nature_evidence=presentation.nature_evidence,
        nature_audit=presentation.nature_audit,
    )
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    return bundle


def schedule_fa_leg(run_dir: Path, itr_form: ItrForm = "ITR-2") -> Any:
    """``robinhood-all``'s Schedule FA leg for FY 2025-26, written to ``all``."""
    report = run_robinhood_all(
        input_dir=run_dir / "bundle",
        config_path=run_dir / "user.toml",
        output_dir=run_dir / "all",
        financial_year=_FY,
        itr_form=itr_form,
        fetch_prices=False,
    )
    return next(leg for leg in report.legs if leg.name == "schedule-fa")


_SAME_DAY = (
    f"{SAME_DAY_LOT_CAUSE.format(moved='sales and transfers')} {_REFERENCES}activity rows 0:4 "
    f"(lots 0:2, 0:3). {SAME_DAY_EVIDENCE} {_BY_HAND}, matching the lots first in, first "
    "out, in the order the trades and transfers ran."
)
# The issue's example: two same-day buys at $10 and $11 (rows 0:2 and 0:3),
# then a sale of half a share (row 0:4).
SAME_DAY_TRADES = (buy_at_row(2, "1", "10"), buy_at_row(3, "1", "11"), sale_at_row(4, "0.5", "12"))
_A2_CLOSING = (
    "Table A2's closing balance comes from each account's statement ending on 31 December, "
    f"and no statement of these accounts ends on that day. {_REFERENCES}account_index 1. "
    f"{_STATEMENT_ROUTE}"
)
_INCOME_CREDITS = (
    "Table A3's dividend and Stock Lending credits come from Robinhood's source income for "
    "the financial year, which has unresolved Schedule OS blockers; robinhood-income names "
    f"each one. {_REFERENCES}account_index 1, Robinhood security 1. "
    f"{_NOT_WRITTEN}: clear them and re-run, "
    "or complete the whole Robinhood Schedule FA by hand."
)
_COVERAGE = (
    "Schedule FA needs each account's activity report and statements to cover the calendar "
    "year from the account's opening date, with one statement ending on 31 December, and "
    f"these accounts' evidence doesn't. {_REFERENCES}account_index 1. {_STATEMENT_ROUTE}"
)
CREDIT_LINE = f"{CREDIT_CAUSE} {_REFERENCES}Robinhood security 1 (lots 0:2). {_BY_HAND}."
_CLOSING = f"{CLOSING_CAUSE} {_REFERENCES}Robinhood security 1 (lots 0:5). {_BY_HAND}."
POSITION_REFERENCES = (
    "account_index 0 statement 1 (page 1 row 1); account_index 0 statement 2 (page 1 row 1)"
)
_NEGATIVE = (
    "The activity report takes more shares of a security out of an account than its earlier "
    "trades and transfers put in, so the shares held can't be proved. "
    f"{_REFERENCES}activity rows 0:4. {_REPORT_ROUTE}"
)
_POSITION_IDENTITY = (
    "A monthly statement shows a position whose symbol or CUSIP matches no security in the "
    f"activity report, so its shares can't be tied to one security. {_REFERENCES}"
    f"{POSITION_REFERENCES}. {_REPORT_ROUTE}"
)
SNAPSHOT_LINE = (
    "The trade-date or settlement-date basis of a monthly statement's share count can't "
    "be proved from the activity report and its Pending Trades, so the shares held can't "
    "be proved. "
    f"{_REFERENCES}account_index 0 statement 1 (page 1 row 1). {_BY_HAND}."
)
# A Dec-31 buy of 0.00001 share for $0.01: the 2.0000 shares the statement
# shows are within display tolerance of both the trade-date and settled counts.
_TINY_PENDING_BUY = dec31_trade(
    replace(_buy("0.00001"), price_usd=Decimal(1000), amount_usd=Decimal("-0.01")), 5
)


def _unbound_income(monkeypatch: pytest.MonkeyPatch, run_dir: Path) -> SimpleNamespace:
    """Cash-only accounts and a substitute-payment form no account can claim."""
    bundle = load_blocker_accounts(
        monkeypatch, run_dir, replace(_cash(), amount_usd=Decimal(7)), held=Decimal()
    )
    bundle.packets = (_packet(income_code="34", account_reference="999999999"),)
    return bundle


FaBlockerLoader = Callable[[pytest.MonkeyPatch, Path], object]
FA_BLOCKER_CASES: dict[str, tuple[FaBlockerLoader, str]] = {
    "a3-lots-same-day": (
        lambda monkeypatch, run_dir: load_blocker_accounts(
            monkeypatch, run_dir, *SAME_DAY_TRADES, held=Decimal("1.5")
        ),
        f"{_FA}\n{_SAME_DAY}",
    ),
    # The dividend says 3.0000 shares; the account's lot holds 2.0000.
    "a3-credit-attribution": (
        lambda monkeypatch, run_dir: load_blocker_accounts(
            monkeypatch,
            run_dir,
            buy_at_row(2, "2.0000", "10"),
            replace(
                _dividend(),
                amount_usd=Decimal("1.00"),
                description_shares=Decimal("3.0000"),
                description_rate_usd=Decimal("0.3333"),
            ),
            dividends=Decimal("1.00"),
        ),
        f"{_FA}\n{CREDIT_LINE}",
    ),
    # The managed account's statement from 1 December spans 31 December, so
    # its Income summary can't be checked against CY 2025 either.
    "a2-closing-and-evidence-coverage": (
        lambda monkeypatch, run_dir: load_blocker_accounts(
            monkeypatch, run_dir, buy_at_row(2, "2", "10"), managed=spanning_statements
        ),
        f"{_FA}\n{_A2_CLOSING}\n{_INCOME_CREDITS}\n{_COVERAGE}",
    ),
    "cash-only-income": (
        lambda monkeypatch, run_dir: load_blocker_accounts(
            monkeypatch,
            run_dir,
            replace(_cash(), amount_usd=Decimal(7)),
            held=Decimal(),
            managed=spanning_statements,
        ),
        f"{_FA}\n{_A2_CLOSING}\n"
        + _INCOME_CREDITS.replace("account_index 1, Robinhood security 1", "account_index 1")
        + f"\n{_COVERAGE}",
    ),
    "unbound-substitute-form-income": (
        _unbound_income,
        f"{_FA}\n"
        + _INCOME_CREDITS.replace("account_index 1, Robinhood security 1", "account_index 0, 1"),
    ),
    # Every share is bought on 31 December and settles in January.
    "a3-closing": (
        lambda monkeypatch, run_dir: load_dec31_unresolved(
            monkeypatch, run_dir, ((), DEC31_REPRO[1])
        ),
        f"{_FA}\n{_CLOSING}",
    ),
    # The activity report buys 1 share; the statements show 2.
    "anchor-mismatch": (
        lambda monkeypatch, run_dir: load_blocker_accounts(
            monkeypatch, run_dir, buy_at_row(2, "1", "10")
        ),
        f"{_INVENTORY}\n{_ANCHOR.format(references=_REFERENCES + POSITION_REFERENCES)}",
    ),
    # The activity report buys 1 share and sells 2.
    "negative-inventory": (
        lambda monkeypatch, run_dir: load_blocker_accounts(
            monkeypatch,
            run_dir,
            buy_at_row(2, "1", "10"),
            sale_at_row(4, "2", "12"),
            held=Decimal(),
        ),
        f"{_INVENTORY}\n{_NEGATIVE}",
    ),
    # The statements show the 2 shares bought under a symbol the report never
    # trades, so the bought shares are missing from them too.
    "position-identity": (
        lambda monkeypatch, run_dir: load_blocker_accounts(
            monkeypatch, run_dir, buy_at_row(2, "2", "10"), position_symbol="XQZZ"
        ),
        f"{_INVENTORY}\n"
        + _ANCHOR.format(
            references=f"{_REFERENCES}account_index 0 statement 1; account_index 0 statement 2"
        )
        + f"\n{_POSITION_IDENTITY}",
    ),
    "snapshot-basis": (
        lambda monkeypatch, run_dir: load_dec31_unresolved(
            monkeypatch, run_dir, ((_buy("2.0000"),), _TINY_PENDING_BUY)
        ),
        f"{_INVENTORY}\n{SNAPSHOT_LINE}",
    ),
}


def december_statements(deposit: tuple[RobinhoodEvent, ...]) -> tuple[RobinhoodStatement, ...]:
    """:func:`spanning_statements` with its 1 December - 31 January statement replaced
    by Robinhood's statements ending on 31 December and starting on 1 January."""
    first, spanning, last = spanning_statements(deposit)
    december = replace(spanning, period_end=date(2025, 12, 31))
    january = replace(spanning, period_start=date(2026, 1, 1))
    return first, december, january, last


OPTION_CLASSIFICATION_LINE = (
    "These accounts traded or held options in the calendar year but none in the financial "
    "year, so the run has no [[robinhood.option_classifications]] treatment saying whether "
    "they belong in Table D or only in Table A2, and no entry for this financial year gives "
    f"one. {_REFERENCES}account_index 0. {_BY_HAND}, reporting those options under the "
    "treatment of the financial year they were traded in."
)


# What a filer who reviewed the synthetic security's evidence writes for
# each placeholder of its suggested block.
REVIEWED_PLACEHOLDERS = {
    "isin": f'"{_ISIN}"',
    "listing_exchange": '"NASDAQ"',
    "sub_category": '"COMMON"',
    "asset_class": '"STK"',
    "issuer_country_code": '"US"',
    "is_depositary_receipt": "false",
    "source": '"synthetic issuer evidence"',
}
