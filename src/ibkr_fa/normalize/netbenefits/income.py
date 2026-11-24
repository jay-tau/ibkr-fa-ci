"""Turn NetBenefits Stock Plan Services statements into dividend and withholding models.

:func:`normalize_income` collects the in-FY income events across the
(possibly overlapping) itemizing statements, dedups them, and returns the
:class:`~ibkr_fa.models.Dividend` and :class:`~ibkr_fa.models.WithholdingTax`
records in a :class:`~ibkr_fa.models.NetBenefitsSourceRecord`. Overlapping
statements must list the same in-FY events for the days they share. Every in-FY
dividend must be for the configured ESPP security or Fidelity's core fund,
and every withholding must match exactly one same-date, same-name dividend
(``StatementWithholding`` carries no CUSIP).

:func:`require_year_end_income_listed` checks a year-end report's in-FY income
rows against the Stock Plan Services reports (#1249).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from itertools import combinations
from typing import Final

from ibkr_fa.models import (
    Dividend,
    NetBenefitsSourceRecord,
    WithholdingTax,
)
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.normalize.netbenefits.statement_label import statement_among_all_reports
from ibkr_fa.parser.netbenefits.bundle import NetBenefitsBundleError
from ibkr_fa.parser.netbenefits.statement_pdf import (
    DIV_TABLE,
    WHT_TABLE,
    ReportType,
    StatementData,
    StatementDividend,
    StatementWithholding,
)
from ibkr_fa.reference.fidelity_core_funds import FYIXX_CUSIP, FYIXX_ISIN, FYIXX_SYMBOL
from ibkr_fa.types import Isin, Ticker

# The statement table each kind of income event is listed in.
_TABLES: Final[dict[str, str]] = {"dividend": DIV_TABLE, "withholding": WHT_TABLE}
# What an opening-date refusal's fix also needs: the income step requires
# coverage from the later of the FY start and the opening date (#1471).
_THEN_COVER: Final = "and the statements (year-end reports excluded) must then cover"
_COVER_FROM_CORRECTED_DATE: Final = (
    f"{_THEN_COVER} from the corrected date, or the FY start if later, through the FY end"
)
# When the withholding opening-date refusal is reached, no statement lists a same-date
# dividend (see _collect_events), so a corrected date leaves it matching none (#1486).
_NO_DIVIDEND_ON_ITS_DATE: Final = (
    "The statements also list no dividend on that withholding's date, so even then it "
    "matches no dividend, and no config change can fix that: check the statement that lists it"
)


def normalize_income(
    statements: list[StatementData],
    *,
    security_cusip: str,
    security_symbol: Ticker,
    security_isin: Isin,
    account_opening_date: date,
    fy_start: date,
    fy_end: date,
    coverage_window: str | None = None,
) -> NetBenefitsSourceRecord:
    """Build the Dividend + WithholdingTax models from the in-FY statement events.

    ``statements`` are the itemizing SPS reports, sorted by period start. The
    record's coverage is their periods; the caller checks those cover the
    income window. A statement lists every event in its period, so statements
    whose periods overlap must list the same in-FY events for the days they
    share, or the run refuses. Refusals name a statement by its 1-based place
    in ``statements`` and an event by its 1-based row in its statement table,
    never a value. An opening-date refusal also names what the statements must
    then cover: ``coverage_window``, the caller's wider window (#1478), or by
    default from the corrected date, or the FY start if later, through the FY end.
    """
    stmt_divs, stmt_whts = _collect_events(
        statements,
        security_cusip=security_cusip,
        opening=account_opening_date,
        fy_start=fy_start,
        fy_end=fy_end,
        cover=(
            _COVER_FROM_CORRECTED_DATE
            if coverage_window is None
            else f"{_THEN_COVER} {coverage_window}"
        ),
    )
    identity = {
        security_cusip: (security_symbol, security_isin),
        FYIXX_CUSIP: (FYIXX_SYMBOL, FYIXX_ISIN),
    }
    # No in-FY dividend is a valid empty state (coverage is proven upstream, so
    # this is a real zero, not a data gap) — emit empty schedules rather than
    # refuse, mirroring netbenefits-cg on no-sales. An orphan withholding (WHT
    # with no matching dividend) still refuses below. (#361)
    dividends = [
        Dividend(
            symbol=identity[d.cusip][0],
            isin=identity[d.cusip][1],
            payment_date=d.date,
            amount_gross=d.gross_amount_usd,
            currency="USD",
            description=f"{identity[d.cusip][0]}({identity[d.cusip][1]}) Cash Dividend",
            kind=DistributionKind.CASH_DIVIDEND,
        )
        for d in stmt_divs
    ]
    # Match each in-FY withholding to exactly one same-date same-name dividend
    # (StatementWithholding has no CUSIP; identity comes from the matched dividend).
    divs_by_date_name: dict[tuple[date, str], list[StatementDividend]] = {}
    for d in stmt_divs:
        divs_by_date_name.setdefault((d.date, d.security_name), []).append(d)
    withholdings: list[WithholdingTax] = []
    for w, statement, row in stmt_whts:
        candidates = divs_by_date_name.get((w.date, w.security_name), [])
        if len(candidates) != 1:
            raise NetBenefitsBundleError(
                f"the withholding at {_row('withholding', statement, row)} matches "
                f"{len(candidates)} in-FY dividend(s); expected exactly one (ambiguous or orphan)"
            )
        matched_symbol, matched_isin = identity[candidates[0].cusip]
        withholdings.append(
            WithholdingTax(
                symbol=matched_symbol,
                isin=matched_isin,
                payment_date=w.date,
                amount=w.amount_usd,  # signed: negative == tax paid (#I9)
                currency="USD",
                description=f"{matched_symbol}({matched_isin}) Withholding Tax",
            )
        )
    return NetBenefitsSourceRecord(
        dividends=tuple(dividends),
        withholding_tax=tuple(withholdings),
    )


def require_year_end_income_listed(
    statements: list[StatementData], *, fy_start: date, fy_end: date
) -> None:
    """Refuse an in-FY income row on a year-end report that the Stock Plan Services
    reports don't list (#1249).

    ``statements`` are every report, year-end reports included, sorted by period
    start. A year-end report's silence proves nothing, but an income row it prints
    is evidence, as #1151 settled for purchase rows. So each in-FY dividend and
    withholding a year-end report prints must be listed by a Stock Plan Services
    report whose period covers its date, with the same identity (a dividend's date
    and CUSIP, a withholding's date and security name) and amount, as many times as
    the year-end report prints it. Rows outside the FY aren't compared. The refusal
    names the row by its report's 1-based place in ``statements`` and its 1-based
    row in its table, never a value.
    """
    listed = [
        (s, Counter(key for _, key in _income_keys(s)))
        for s in statements
        if s.report_type is ReportType.STOCK_PLAN_SERVICES
    ]
    for number, stmt in enumerate(statements, start=1):
        if stmt.report_type is not ReportType.YEAR_END:
            continue
        printed: Counter[tuple[str, date, str, Decimal]] = Counter()
        for row, key in _income_keys(stmt):
            kind, day = key[0], key[1]
            if not fy_start <= day <= fy_end:
                continue
            printed[key] += 1
            if not any(
                s.period_start <= day <= s.period_end and counts[key] >= printed[key]
                for s, counts in listed
            ):
                raise NetBenefitsBundleError(
                    f"{statement_among_all_reports(number)} lists a "
                    f"{kind} at row {row} of its {_TABLES[kind]} table that no Stock Plan "
                    "Services report covering that day lists with the same amount; a year-end "
                    "report's income must match the Stock Plan Services reports, so one of them "
                    "is wrong — refusing rather than guess which"
                )


def _income_keys(stmt: StatementData) -> Iterator[tuple[int, tuple[str, date, str, Decimal]]]:
    """Each dividend's and withholding's 1-based table row, with its kind, date,
    identity and amount."""
    for row, d in enumerate(stmt.dividends, start=1):
        yield row, ("dividend", d.date, d.cusip, d.gross_amount_usd)
    for row, w in enumerate(stmt.withholdings, start=1):
        yield row, ("withholding", w.date, w.security_name, w.amount_usd)


def _collect_events(
    statements: list[StatementData],
    *,
    security_cusip: str,
    opening: date,
    fy_start: date,
    fy_end: date,
    cover: str,
) -> tuple[list[StatementDividend], list[tuple[StatementWithholding, int, int]]]:
    """Collect in-FY dividends/withholdings across (possibly overlapping)
    statements. Event identity is (date, CUSIP) for dividends and
    (date, security_name) for withholdings — WITHOUT the amount. Two
    overlapping statements must first list the same identities for their
    shared in-FY days: an event one lists and the other leaves out REFUSES,
    since one of them is wrong. Then the SAME event reported by two
    overlapping statements dedups, but if those reports
    DISAGREE on the amount (or, for a dividend, the printed security name) it
    REFUSES rather than filing both. Keying dividends on the CUSIP (not the
    printed name) means a "CORP" vs "CORPORATION" name drift for the same
    security+date can't slip a conflicting figure through as a second event.
    Also refuses a third-security dividend and a duplicate event WITHIN one
    statement (ambiguous), and an in-FY event dated BEFORE the account opening
    (income can't precede the account, and coverage before the opening date is
    not required — so such an event is a contradiction that would otherwise let
    other pre-opening income go unproven); that refusal names ``cover``, what
    the statements must then cover, and for a withholding adds that it matches
    no dividend (#1486). Each withholding comes back with
    the 1-based statement and table row that first list it."""
    if security_cusip == FYIXX_CUSIP:
        raise NetBenefitsBundleError(
            "netbenefits.security.cusip is the account's core money-market fund; configure "
            "the ESPP stock instead"
        )
    _require_overlaps_agree(statements, fy_start=fy_start, fy_end=fy_end)
    divs: dict[tuple[date, str], tuple[StatementDividend, Decimal, str, int, int]] = {}
    whts: dict[tuple[date, str], tuple[StatementWithholding, Decimal, str, int, int]] = {}
    for statement, stmt in enumerate(statements, start=1):
        seen_divs: dict[tuple[date, str], int] = {}
        seen_whts: dict[tuple[date, str], int] = {}
        for row, d in enumerate(stmt.dividends, start=1):
            if not fy_start <= d.date <= fy_end:
                continue
            if d.date < opening:
                raise NetBenefitsBundleError(
                    f"the dividend at {_row('dividend', statement, row)} predates "
                    f"netbenefits.account.account_opening_date; income can't precede the "
                    f"account, and coverage before it isn't required — fix "
                    f"netbenefits.account.account_opening_date, {cover}"
                )
            if d.cusip not in (security_cusip, FYIXX_CUSIP):
                raise NetBenefitsBundleError(
                    f"the in-FY dividend at {_row('dividend', statement, row)} is for a CUSIP "
                    f"that is neither netbenefits.security.cusip nor the core money-market fund"
                )
            _add_event(
                divs,
                seen_divs,
                (d.date, d.cusip),
                d,
                d.gross_amount_usd,
                d.security_name,
                "dividend",
                statement,
                row,
            )
        for row, w in enumerate(stmt.withholdings, start=1):
            if not fy_start <= w.date <= fy_end:
                continue
            if w.date < opening:
                # No statement lists a dividend dated w.date: in this statement or an
                # earlier one it would have refused above, and a later one listing it
                # would cover that day (the parser keeps events inside their period), so
                # _require_overlaps_agree would have refused unless this one listed it too.
                raise NetBenefitsBundleError(
                    f"the withholding at {_row('withholding', statement, row)} predates "
                    f"netbenefits.account.account_opening_date; fix "
                    f"netbenefits.account.account_opening_date, {cover}. "
                    f"{_NO_DIVIDEND_ON_ITS_DATE}"
                )
            _add_event(
                whts,
                seen_whts,
                (w.date, w.security_name),
                w,
                w.amount_usd,
                w.security_name,
                "withholding",
                statement,
                row,
            )
    return [d for d, *_ in divs.values()], [(w, s, r) for w, _, _, s, r in whts.values()]


def _require_overlaps_agree(
    statements: list[StatementData], *, fy_start: date, fy_end: date
) -> None:
    """Refuse two statements that list different events for the days they share.

    Compares every pair of statements on their shared in-FY days (none for a
    pair that doesn't overlap). Each must list the same dividend identities
    and the same withholding identities there, or one of them is wrong. The
    earliest difference (by day, kind, identity and the two periods) is named,
    by the statement that lists it, its row, and the statement that leaves it
    out. The same event is selected regardless of statement order.
    """
    missing: list[tuple[date, str, str, str, str, int, int, int]] = []
    for (i, a), (j, b) in combinations(enumerate(statements, start=1), 2):
        start = max(a.period_start, b.period_start, fy_start)
        end = min(a.period_end, b.period_end, fy_end)
        a_events, b_events = _events_between(a, start, end), _events_between(b, start, end)
        missing += [
            (*event, _period(a), _period(b), i, row, j)
            for event, row in a_events.items()
            if event not in b_events
        ]
        missing += [
            (*event, _period(b), _period(a), j, row, i)
            for event, row in b_events.items()
            if event not in a_events
        ]
    if missing:
        _day, kind, _identity, _lister, _other, lister, row, other = min(missing)
        raise NetBenefitsBundleError(
            f"{_statement(lister)} lists a {kind} at row {row} of its {_TABLES[kind]} table "
            f"that the {_TABLES[kind]} table of the overlapping {_statement(other)} leaves out; "
            f"overlapping statements must list the same events, so one of them is wrong — "
            f"refusing rather than guess which"
        )


def _events_between(
    stmt: StatementData, start: date, end: date
) -> dict[tuple[date, str, str], int]:
    """The statement's dividend and withholding identities dated ``start``..``end``,
    each with the 1-based table row that first lists it."""
    events: dict[tuple[date, str, str], int] = {}
    for row, d in enumerate(stmt.dividends, start=1):
        if start <= d.date <= end:
            events.setdefault((d.date, "dividend", f"CUSIP {d.cusip!r}"), row)
    for row, w in enumerate(stmt.withholdings, start=1):
        if start <= w.date <= end:
            events.setdefault((w.date, "withholding", repr(w.security_name)), row)
    return events


def _period(stmt: StatementData) -> str:
    """The period as an internal sort key; never printed."""
    return f"{stmt.period_start.isoformat()}..{stmt.period_end.isoformat()}"


def _statement(number: int) -> str:
    return f"statement {number} (by period start, year-end reports excluded)"


def _row(kind: str, statement: int, row: int) -> str:
    """Where an income event of ``kind`` is listed, such as ``statement 2 (…), row 1
    of its Taxes Withheld table``."""
    return f"{_statement(statement)}, row {row} of its {_TABLES[kind]} table"


def _add_event(
    store: dict,  # type: ignore[type-arg]
    seen_this_statement: dict,  # type: ignore[type-arg]
    identity: tuple,  # type: ignore[type-arg]
    event: object,
    amount: Decimal,
    name: str,
    kind: str,
    statement: int,
    row: int,
) -> None:
    """Record one in-FY income event listed at ``row`` of ``statement``, refusing a
    within-statement duplicate and a cross-statement amount/name conflict for the
    same identity. (For withholdings the name IS part of the identity, so the
    name check there never fires.)"""
    first_row = seen_this_statement.get(identity)
    if first_row is not None:
        raise NetBenefitsBundleError(
            f"{_statement(statement)} lists the same {kind} at rows {first_row} and {row} of "
            f"its {_TABLES[kind]} table — ambiguous, refusing"
        )
    seen_this_statement[identity] = row
    existing = store.get(identity)
    if existing is not None:
        first, here = _row(kind, *existing[3:]), _row(kind, statement, row)
        if existing[1] != amount:
            raise NetBenefitsBundleError(
                f"the {kind} at {first} and the same {kind} at {here} report different "
                f"amounts; refusing rather than file a guessed figure"
            )
        if existing[2] != name:
            raise NetBenefitsBundleError(
                f"the {kind} at {first} and the same {kind} at {here} report different "
                f"security names; refusing rather than guess which is correct"
            )
        return
    store[identity] = (event, amount, name, statement, row)
