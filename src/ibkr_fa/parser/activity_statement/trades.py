"""Parse an Activity Statement Trades section into execution events.

Activity Statement Trades is the per-execution log: every row is one
trade. The format does not expose IBKR's tax-lot ledger directly —
unlike Flex Query's LOT-detail mode where lots are emitted in a
separate ``Closed Lots`` schema. For Schedule FA Table A3 we need
per-lot acquisition dates so the compute layer can FX-convert each
lot's cost basis at the right TTBR rate.

This parser validates each Trades row and emits one
:class:`~ibkr_fa.models.ExecutionEvent` per row. It does no lot
matching: :func:`ibkr_fa.compute.lot_matching.match_fifo` turns the
events into open and closed lots (#640).

v1 of this parser handles every Trades row whose ``Code`` is ``O``
(or any allow-listed lot-neutral combination), including **opening
short sales** (``Code='O'`` with negative ``Quantity``), which become
:attr:`Side.SHORT` opening events. Short lots survive matching and
reconciliation but are filtered out of Schedule FA Table A3 output by
:func:`ibkr_fa.compute.lot_overlap.overlapping_lots_for_cy` — short
positions are liabilities, not equity HELD, and so don't belong on
A3 (which reports "Foreign Equity and Debt Interest held" per the
ITR-2 utility). The foreign brokerage account that holds the short
proceeds is reportable on FA Table A2 (Foreign Custodial Accounts),
which :mod:`ibkr_fa.pipeline_fa_a2` writes when ``[account.custodian]``
is configured, and the ``schedule-fa`` report states how many short
lots it left out of A3.

Closure rows (``Code`` token ``C``) are also supported as of #104.
Each becomes a closing event carrying its net proceeds ``|Basis| +
Realized P/L`` and its ``T. Price``. The broker's close-side ``Basis``
itself is not carried; see :mod:`ibkr_fa.compute.lot_matching` for why
the matcher costs each slice from the matched open instead. Every event's
quantity, ``|Basis|`` and net proceeds keep every digit of the cells they
come from (#1311, :mod:`ibkr_fa.parser._exact`).

Remaining non-allow-listed codes still cause the parser to refuse
and point the user at the relevant tracker:

* #106 — corporate actions (``Adj``, ``De``, ``Ex``, ``XCH``,
  ``ETF``, ...)
* #107 — transfers (``T``, ``I``, ``HFI``, ...) and tax-basis
  elections (``HC``, ``LI``, ``SL``, ``ML*``)

Wash-sale loss-disallowed adjustments (``LD`` code) are **refused**
(both opening and closing). India has no wash-sale rule, so the Indian
return must use the *actual* acquisition cost — but IBKR's ``LD``
accounting reports a basis we cannot reliably reduce to that: on an
OPENING trade IBKR rewrites ``Basis`` to ADD the disallowed loss (the
reported basis is the US figure, not the price paid), and on a CLOSING
trade the realized P/L is zeroed (breaking the ``|Basis| + Realized
P/L`` proceeds derivation). Rather than emit a US-tax basis on an
Indian return we refuse; actual-cost back-out for ``LD`` is tracked at
#265. This does NOT affect the common case (e.g. the trace data) where
the statement is generated in "wash sales P/L" mode: there the
disallowed loss appears as Realized P/L on the replacement ``O`` row
while ``Basis`` stays the actual acquisition cost, so it flows through
correctly (independent of any execution-venue ``Code`` tokens).
See the ``LD`` handling in :func:`_validate_code` and the IBKR
codes reference at
https://www.ibkrguides.com/reportingreference/reportguide/codes_default.htm
for the per-code semantics.

The Open Positions reader and the checks that tie the matched lots to the
statement's Open Positions section are in
:mod:`ibkr_fa.parser.activity_statement.open_positions` (#2007).
"""

from __future__ import annotations

import datetime as _dt
import operator
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from typing import Final

from ibkr_fa.exceptions import ParseError, ReplacedCrashError, crash_site
from ibkr_fa.invariants import require
from ibkr_fa.models import ExecutionEvent, ExecutionKind, Side, StatementPeriod
from ibkr_fa.number_checks import NumberKind
from ibkr_fa.parser._cells import STOCKS_ONLY, parse_decimal_cell, require_cell, require_columns
from ibkr_fa.parser._exact import exactly
from ibkr_fa.parser._isin import _ISIN_PATTERN, validate_isin_or_raise
from ibkr_fa.parser._scope_guards import require_foreign_mapped_country, require_usd
from ibkr_fa.parser.activity_statement._dates import require_within_period
from ibkr_fa.parser.activity_statement.multi_section import (
    refuse_trades_detail_row,
)
from ibkr_fa.types import CountryCode, Isin, Ticker

_SECTION: Final[str] = "Trades"
_FII_SECTION: Final[str] = "Financial Instrument Information"

# Each Trades-row ``Code`` cell is a ``;``-separated list of tokens
# (e.g. "FP;O" or "O;RP" or "C;FPA;P"). Every row must carry exactly
# one *direction* token (one of :data:`_DIRECTION_TOKENS`) and zero
# or more lot-neutral *modifier* tokens (in :data:`_ALLOWED_CODE_TOKENS`).
# Modifiers affect execution routing or settlement metadata, not the
# lot's quantity, basis, or acquisition semantics. See the module
# docstring for the codes that are deliberately NOT here, each
# tracked in its own issue.
_ALLOWED_CODE_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "FP",  # Fractional portion executed against IB
        "FPA",  # Fractional portion + IB acted as agent on whole-share
        "RP",  # Riskless principal (execution detail)
        "RPA",  # Riskless principal + agent
        "P",  # Partial Execution
        "IA",  # Transaction executed against IB or affiliate
        "IM",  # Mixed: portion against IB, portion as agent
        "PI",  # Price Improvement
        "Cx",  # Crossing executed as dual agent for two IB customers
        "Pr",  # Crossing by IB against an IB affiliate (Principal)
        "M",  # Entered manually by IB
        "IPO",  # IPO; IB was a member of the selling group
        "SI",  # Solicited by Interactive Brokers
        "SO",  # Solicited by introducing broker
        "SF",  # Subject to IBKR Lite Surcharge Fee
    }
)
# Wash-sale loss-disallowed marker (``LD``, #105/#265). Handled OUTSIDE
# the generic allow-list because it carries the disallowed loss into
# IBKR's basis/realized figures (US wash-sale accounting). India has no
# wash-sale rule and we cannot reliably recover the actual acquisition
# cost from the Activity Statement, so ``LD`` rows are REFUSED — see
# :func:`_validate_code` for the dispatch and #265 for the back-out.

# Direction tokens — exactly one must appear in every Trades row's
# Code cell. ``O`` emits an opening event; ``C`` emits a closing event
# that the matcher FIFO-matches against earlier opens (issue #104).
_OPENING_TRADE_TOKEN: Final[str] = "O"
_CLOSING_TRADE_TOKEN: Final[str] = "C"
_DIRECTION_TOKENS: Final[frozenset[str]] = frozenset({_OPENING_TRADE_TOKEN, _CLOSING_TRADE_TOKEN})


@dataclass(frozen=True, slots=True)
class FinancialInstrument:
    """Side-table row from the Financial Instrument Information section.

    Activity Statement's Trades rows don't carry ISIN inline (unlike
    Flex Query), so we build a Symbol -> :class:`FinancialInstrument`
    lookup from the Financial Instrument Information section and
    join at trade-parse time.

    Attributes:
        symbol: Ticker as IBKR emits it (e.g., ``"XMPL"``).
        description: Human-readable security name.
        isin: ISO 6166 ISIN from the ``Security ID`` column. The
            first two characters are the ISO 3166-1 alpha-2 country
            code of the security's primary identifier registry —
            for ADRs of foreign companies this is ``US`` (the
            depositary's ISIN), not the underlying's country.
            That's the correct semantic for Schedule FA, which
            asks about the asset's issuer as held.
        listing_exchange: e.g., ``"NASDAQ"`` or ``"NYSE"``.
        asset_class: IBKR's ``Asset Category`` (e.g., ``"Stocks"``).
    """

    symbol: Ticker
    description: str
    isin: Isin
    listing_exchange: str
    asset_class: str


def _decimal(
    raw: str, *, row_index: int, column: str, kind: NumberKind, section: str = _SECTION
) -> Decimal:
    return parse_decimal_cell(raw, section=section, row_index=row_index, column=column, kind=kind)


def _non_empty(raw: str, *, row_index: int, column: str, section: str = _SECTION) -> str:
    if not raw.strip():
        # Echo no value: it is only whitespace, and Currency refusals never echo a cell (#1027).
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message="must be non-empty",
            raw_value="",
        )
    return raw


# Every Financial Instrument Information, Open Positions and Trades row
# must be a stock (#913). v1 reads every row as a stock lot, so another
# instrument (an option, a bond, a currency pair) would be filed as one.
_STOCKS: Final[str] = "Stocks"
_ONE_TO_ONE: Final[str] = (
    "a multiplier scales every quantity and value, and v1 reads one share as one unit"
)
_NO_CODES: Final[str] = "v1 reads no codes in this section, so a code's meaning would be lost"

_ZERO_QUANTITY: Final[str] = "must not be zero; a row that moved no shares is malformed."


# Exact ASCII-digit shape of a Trades ``Date/Time`` cell. ``[0-9]``, not
# ``\d``, which also matches non-ASCII digits.
_DATE_TIME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}, [0-9]{2}:[0-9]{2}:[0-9]{2}"
)


def _parse_full_date_time(
    raw: str, *, row_index: int, column: str, period: StatementPeriod
) -> _dt.datetime:
    """Parse an Activity Statement ``Date/Time`` cell into a :class:`datetime`.

    Format is exactly ``"YYYY-MM-DD, HH:MM:SS"`` (with the comma); the time
    component is preserved. Used inside :func:`parse_activity_statement_trades`
    so FIFO ordering can distinguish multiple same-day trades (a
    close at 09:30 must not match against an open at 10:30 — Tier-1
    PR #161 quality review).

    The shape is checked before ``fromisoformat``, which on Python 3.12 also
    accepts other ISO forms (``20250615``, ``0930``, ``09:30:00.5``) and a
    timezone suffix. The result is always a plain datetime with no timezone,
    so FIFO matching can compare any two trades (#915). Its date must fall
    inside the statement's ``period`` (:func:`require_within_period`, #1143).
    A calendar or clock refusal replaces the ``ValueError``, which on Python
    3.14 names the date's or time's parts, with a content-free stand-in
    cause (#1226).
    """
    if _DATE_TIME_PATTERN.fullmatch(raw) is None:
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column=column,
            message=("must be in 'YYYY-MM-DD, HH:MM:SS' format (with the comma and space)"),
            raw_value="",
        )
    date_part, time_part = raw.split(", ", 1)
    # Fixed text, after each handler: on Python 3.14 the ValueError names the
    # date's or time's parts.
    crash = None
    try:
        parsed_date = _dt.date.fromisoformat(date_part)
    except ValueError as exc:
        crash = ReplacedCrashError(crash_site(exc))
    if crash is not None:
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column=column,
            message="must be a real calendar date",
            raw_value="",
        ) from crash
    try:
        parsed_time = _dt.time.fromisoformat(time_part)
    except ValueError as exc:
        crash = ReplacedCrashError(crash_site(exc))
    if crash is not None:
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column=column,
            message="must have a real wall-clock time",
            raw_value="",
        ) from crash
    require_within_period(parsed_date, period, section=_SECTION, row_index=row_index, column=column)
    return _dt.datetime.combine(parsed_date, parsed_time)


# Required keys for a Trades data row dict, in IBKR-emitted order.
# Validated per-row at the top of the parser loop so a missing column
# produces a clean :class:`ParseError` instead of a raw ``KeyError``
# escaping past the CLI's documented-error catch (Codex P2 on PR #111).
_REQUIRED_TRADES_ROW_KEYS: Final[tuple[str, ...]] = (
    "DataDiscriminator",
    "Asset Category",
    "Symbol",
    "Currency",
    "Quantity",
    "Basis",
    "Realized P/L",
    "T. Price",
    "C. Price",
    "Date/Time",
    "Code",
)


_WASH_SALE_TOKEN: Final[str] = "LD"


def _validate_code(raw: str, *, row_index: int) -> str:
    """Validate the ``Code`` cell and return the row's direction token.

    Each cell is a ``;``-separated list of tokens. Every token must
    be either:

    * a *direction* token (:data:`_OPENING_TRADE_TOKEN` ``"O"`` for
      an opening trade, or :data:`_CLOSING_TRADE_TOKEN` ``"C"`` for
      a closing trade), of which exactly one must appear; or
    * a *modifier* token in :data:`_ALLOWED_CODE_TOKENS` (lot-neutral
      execution / settlement metadata).

    Any other token (``Adj``/``De``/``Ex``/``XCH``/``ETF`` corporate
    action, ``T``/``I``/``HFI`` transfer, ``HC``/``LI``/``SL``/``ML*``
    tax-basis election, etc.) raises with a pointer at the tracking
    issue that owns that scope.

    Wash-sale loss-disallowed (``LD``) modifier scope (#105/#265):
    **refused** (opening and closing). India has no wash-sale rule, so
    the Indian return needs the actual acquisition cost, but IBKR's
    ``LD`` accounting reports the US wash-sale-adjusted basis (on opens
    the disallowed loss is added to ``Basis``; on closes the realized
    P/L is zeroed, breaking the ``|Basis| + Realized P/L`` proceeds
    derivation). We refuse rather than emit a US-tax basis on an Indian
    return; actual-cost back-out is tracked at #265.

    Returns:
        The single direction token present in ``raw``
        (:data:`_OPENING_TRADE_TOKEN` or :data:`_CLOSING_TRADE_TOKEN`).

    Raises:
        ParseError: On any unrecognised token, on zero direction
            tokens, on more than one direction token, or on any
            ``LD`` (wash-sale loss-disallowed) marker — see #265.
    """
    tokens = [t for t in raw.split(";") if t]
    direction_tokens: list[str] = []
    has_wash_sale_marker = False
    for tok in tokens:
        if tok in _DIRECTION_TOKENS:
            direction_tokens.append(tok)
            continue
        if tok == _WASH_SALE_TOKEN:
            has_wash_sale_marker = True
            continue
        if tok in _ALLOWED_CODE_TOKENS:
            continue
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column="Code",
            message=(
                "unrecognised trade code token. "
                "v1 of the Activity Statement parser only handles "
                "Open ("
                "https://github.com/jay-tau/ibkr-fa/issues/101) and "
                "Close ("
                "https://github.com/jay-tau/ibkr-fa/issues/104) "
                "trades with execution-routing metadata. Tracking "
                "issues for each unhandled "
                "case: https://github.com/jay-tau/ibkr-fa/issues/106 "
                "(corporate actions), "
                "https://github.com/jay-tau/ibkr-fa/issues/107 "
                "(transfers + tax-basis elections), "
                "https://github.com/jay-tau/ibkr-fa/issues/265 "
                "(``LD`` wash-sale). Please file a "
                "new issue with a redacted statement if the token "
                "isn't listed in any of those."
            ),
            raw_value="",
        )
    # Wash-sale loss-disallowed (``LD``) rows are refused. India has no
    # wash-sale rule, so the Indian return must use the ACTUAL
    # acquisition cost — but IBKR's ``LD`` accounting reports a basis we
    # cannot reliably reduce to the actual cost from the Activity
    # Statement alone: on an OPENING trade IBKR rewrites ``Basis`` to ADD
    # the disallowed loss (so the reported basis is the US figure, not
    # the price paid), and on a CLOSING trade the realized P/L is zeroed
    # (breaking the ``|Basis| + Realized P/L`` proceeds derivation).
    # Rather than silently emit a US-tax figure on an Indian return, we
    # refuse. (The common case — the statement generated in "wash sales
    # P/L" mode, where the disallowed loss sits on the replacement O
    # row's Realized P/L while ``Basis`` stays actual cost — carries an
    # actual-cost ``Basis`` and is unaffected.) Actual-cost back-out for
    # ``LD`` is tracked at #265.
    if has_wash_sale_marker:
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column="Code",
            message=(
                "wash-sale loss-disallowed marker ``LD`` is "
                "not supported. India has no wash-sale rule, so the "
                "Indian return must use the ACTUAL acquisition cost, but "
                "IBKR's ``LD`` basis is the US wash-sale-adjusted figure "
                "(on an opening lot the disallowed loss is rolled into "
                "``Basis``; on a closing lot the realized P/L is zeroed) "
                "and cannot be reliably reduced to the actual cost from "
                "the Activity Statement alone. Refusing rather than emit "
                "a US-tax basis on an Indian return. See "
                "https://github.com/jay-tau/ibkr-fa/issues/265."
            ),
            raw_value="",
        )
    if len(direction_tokens) != 1:
        # Zero direction tokens: ambiguous — can't tell whether the
        # row opens or closes a position. More than one: malformed —
        # a single execution can't simultaneously open and close.
        # Either way, refuse rather than guess.
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column="Code",
            message=(
                f"row code must contain exactly one "
                f"direction token (one of "
                f"{sorted(_DIRECTION_TOKENS)!r}); got "
                f"{len(direction_tokens)}. A Trades row that neither "
                f"opens nor closes a position (or that claims to do "
                f"both) is malformed input — refusing to guess."
            ),
            raw_value="",
        )
    return direction_tokens[0]


def build_instrument_lookup(rows: Iterable[dict[str, str]]) -> dict[Ticker, FinancialInstrument]:
    """Build a Symbol -> :class:`FinancialInstrument` map.

    Args:
        rows: Data rows from the
            :class:`KnownActivitySection.FINANCIAL_INSTRUMENT_INFORMATION`
            block.

    Returns:
        Lookup dict. Empty if input is empty.

    Raises:
        ParseError: If a row is missing a required column, isn't a
            plain stock (``Asset Category`` other than ``Stocks``, a
            ``Multiplier`` other than ``1``, a non-blank ``Code``, an
            ``Underlying`` other than its ``Symbol`` or an ``ISIN``
            other than its ``Security ID``, the last three checked when
            the export has the column; #913), has an
            empty ``Symbol``, has a ``Security ID`` that is empty
            or doesn't match the ISO 6166 ISIN shape (12 chars:
            2 uppercase letters + 9 alphanumerics + 1 digit), or
            if the same ``Symbol`` appears more than once (would
            silently overwrite — same class of bug as the
            duplicate-field check on Account Information). ISIN
            shape validation happens at this site via
            :func:`ibkr_fa.parser._isin.validate_isin_or_raise`
            (#150); a downstream defence-in-depth re-check in
            :func:`_resolve_instrument` covers
            :class:`FinancialInstrument`s constructed directly
            (bypassing this lookup builder). No refusal carries a cell
            value, in its message or ``raw_value``, because a shifted
            cell can hold the holder's name or account number.
    """
    out: dict[Ticker, FinancialInstrument] = {}
    materialised = list(rows)
    for row_index, row in enumerate(materialised):
        try:
            symbol = row["Symbol"]
            description = row["Description"]
            security_id = row["Security ID"]
            listing_exch = row["Listing Exch"]
            asset_category = row["Asset Category"]
        except KeyError as exc:
            raise ParseError(
                section=_FII_SECTION,
                row_index=row_index,
                column=str(exc.args[0]),
                message=(
                    f"required column {exc.args[0]!r} missing from the "
                    f"Financial Instrument Information row. IBKR may "
                    f"have changed the section's schema."
                ),
                raw_value="",
            ) from exc
        checks = {"Asset Category": (_STOCKS, STOCKS_ONLY), "Multiplier": ("1", _ONE_TO_ONE)}
        if "Code" in row:
            checks["Code"] = ("", _NO_CODES)
        for column, (expected, why) in checks.items():
            require_cell(row, column, expected, section=_FII_SECTION, row_index=row_index, why=why)
        sym = _non_empty(symbol, row_index=row_index, column="Symbol", section=_FII_SECTION)
        if "Underlying" in row:
            require_cell(
                row,
                "Underlying",
                symbol,
                section=_FII_SECTION,
                row_index=row_index,
                why="a plain stock's Underlying is its own Symbol",
                expected_name="the row's Symbol",
            )
        isin = validate_isin_or_raise(
            security_id, section=_FII_SECTION, row_index=row_index, column="Security ID"
        )
        if "ISIN" in row:
            require_cell(
                row,
                "ISIN",
                security_id,
                section=_FII_SECTION,
                row_index=row_index,
                why="the instrument is identified by its Security ID",
                expected_name="the row's Security ID",
            )
        ticker = Ticker(sym)
        if ticker in out:
            raise ParseError(
                section=_FII_SECTION,
                row_index=row_index,
                column="Symbol",
                message=(
                    "the Symbol appears more than once; refusing "
                    "to silently overwrite the earlier entry. This "
                    "signals either a malformed export or a multi-"
                    "account statement."
                ),
                raw_value="",
            )
        out[ticker] = FinancialInstrument(
            symbol=ticker,
            description=description,
            isin=Isin(isin),
            listing_exchange=listing_exch,
            asset_class=asset_category,
        )
    # Postcondition (PR #124): non-empty output iff there were any
    # input rows. Free given the loop above never breaks early, but
    # the assertion locks the contract -- a future refactor that
    # silently drops "blank" rows would surface immediately.
    require(
        bool(out) == bool(materialised),
        what="build_instrument_lookup post: empty output iff empty input",
        input_count=len(materialised),
        output_count=len(out),
    )
    return out


@dataclass(frozen=True, slots=True)
class _ResolvedInstrument:
    """Validated instrument-side data extracted from a single Trades row.

    Both opening and closing rows share the same Symbol -> ISIN ->
    country lookup + USD-currency check. ``_resolve_instrument``
    runs that block once per row and returns this value object so
    the event construction can carry it forward without
    re-validating.
    """

    ticker: Ticker
    instrument: FinancialInstrument
    isin_upper: str
    country_prefix: str
    currency: str


def _resolve_instrument(
    row: dict[str, str],
    *,
    row_index: int,
    instruments: dict[Ticker, FinancialInstrument],
) -> _ResolvedInstrument:
    """Resolve and validate the instrument-side of a Trades row.

    Shared by both opening and closing rows: Symbol -> ticker ->
    Financial Instrument Information lookup -> ISIN normalisation
    -> ISO-6166 shape check -> ``IN`` reject (#143) -> country-
    code lookup -> USD-only currency check. Returns the validated
    fields so the event construction can use them without
    re-validating.

    Raises:
        ParseError: For any of the per-row validation failures
            documented on :func:`parse_activity_statement_trades`.
    """
    symbol = _non_empty(row["Symbol"], row_index=row_index, column="Symbol")
    ticker = Ticker(symbol)
    if ticker not in instruments:
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column="Symbol",
            message=(
                "the Symbol isn't in the Financial "
                "Instrument Information section; can't look up "
                "ISIN. Re-export the statement with the "
                "Financial Instrument Information section "
                "enabled."
            ),
            raw_value="",
        )
    instrument = instruments[ticker]

    # See parse_activity_statement_trades docstring for the full
    # history of the ISIN normalisation + country-prefix validation
    # block. Extracted into this helper (PR #104) so both opening
    # and closing rows share the same validation surface — pre-
    # extraction the block lived inline in the parser loop and only
    # the O-path exercised it.
    isin_upper = instrument.isin.upper()
    country_prefix = isin_upper[:2]

    if not _ISIN_PATTERN.match(isin_upper):
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column="Symbol",
            message=(
                "the instrument's ISIN doesn't match ISO 6166 "
                "shape (12 chars: 2 uppercase letters + 9 "
                "alphanumerics + 1 check digit). IBKR may have "
                "changed the Financial Instrument Information "
                "schema, or the Security ID column is truncated. "
                "build_instrument_lookup validates this at parse "
                "time via validate_isin_or_raise (#150); reaching "
                "this defence-in-depth check means the "
                "FinancialInstrument was constructed directly "
                "(bypassing the parser)."
            ),
            raw_value="",
        )

    require_foreign_mapped_country(
        country_prefix,
        section=_SECTION,
        row_index=row_index,
        column="Symbol",
    )

    currency = _non_empty(row["Currency"], row_index=row_index, column="Currency")
    require_usd(currency, section=_SECTION, row_index=row_index, column="Currency")

    return _ResolvedInstrument(
        ticker=ticker,
        instrument=instrument,
        isin_upper=isin_upper,
        country_prefix=country_prefix,
        currency=currency,
    )


def parse_activity_statement_trades(
    rows: Iterable[dict[str, str]],
    *,
    instruments: dict[Ticker, FinancialInstrument],
    period: StatementPeriod,
) -> list[ExecutionEvent]:
    """Parse Activity Statement Trades rows into execution events.

    Each row's :func:`_validate_code` returns either
    :data:`_OPENING_TRADE_TOKEN` (``"O"``) or
    :data:`_CLOSING_TRADE_TOKEN` (``"C"``). Both directions share the
    per-row validation in :func:`_resolve_instrument` (Symbol -> ISIN ->
    country -> USD-only currency check). An opening row carries its
    acquisition cost ``|Basis|``; a closing row carries its net proceeds
    ``|Basis| + Realized P/L`` and its ``T. Price``. The quantity, ``|Basis|``
    and net proceeds keep every digit (#1311). Matching the events
    into lots is :func:`ibkr_fa.compute.lot_matching.match_fifo`'s job.

    Args:
        rows: Data rows from the
            :class:`KnownActivitySection.TRADES` block.
        instruments: Symbol -> instrument lookup, typically the
            output of :func:`build_instrument_lookup` applied to the
            Financial Instrument Information section. Trades rows
            join here to get ISIN AND issuer country (derived from
            the ISIN's first two characters).
        period: The statement's parsed Period. Every row's
            ``Date/Time`` date must fall inside it (#1143).

    Returns:
        One :class:`ExecutionEvent` per input row, in input order. An
        opening row with negative ``Quantity`` (a short sale) and a
        closing row with positive ``Quantity`` (a buy-to-cover, which
        the matcher refuses, #109) carry ``side=Side.SHORT``; all
        others carry ``side=Side.LONG``.

    Raises:
        ParseError: For any of the documented per-row validation
            failures (see :func:`_validate_code`,
            :func:`_resolve_instrument`); a ``Trade``, ``ClosedLot`` or
            ``ClosedLot*`` row, which IBKR adds only when a statement is
            exported with details shown (#1195); a ``Currency`` other than
            ``USD``; an
            ``Asset Category`` other than ``Stocks`` (#913); ``Quantity``
            of zero; a
            sign mismatch between ``Quantity`` and ``Basis`` (IBKR
            emits the same sign for both on opening trades); a
            negative closing ``T. Price``; a ``Date/Time`` outside
            ``period`` (#1143); or net proceeds that can't be added up
            exactly (#1311). No refusal carries a cell
            value, in its message or ``raw_value``, because a shifted
            cell can hold the holder's name or account number.

        Non-US-issued holdings (Canadian, Cayman, Irish, Dutch,
        BVI etc.) traded in USD on US exchanges are accepted as
        of #143.
    """
    materialised = list(rows)
    events: list[ExecutionEvent] = []
    for row_index, row in enumerate(materialised):
        # Codex P2 on #111: validate the row dict has every column
        # the parser subscripts before the first direct subscript
        # runs. Without this, a malformed Trades schema (missing
        # ``Basis`` etc.) would raise ``KeyError`` past the CLI's
        # documented-error catch.
        require_columns(
            row,
            _REQUIRED_TRADES_ROW_KEYS,
            section=_SECTION,
            row_index=row_index,
            message=lambda missing: (
                f"required column(s) {missing!r} missing from the "
                f"Trades row. IBKR may have changed the section's "
                f"schema, or the export was generated with a custom "
                f"column subset. Re-export with the standard column "
                f"set."
            ),
        )
        refuse_trades_detail_row(row, row_index=row_index)
        if row.get("DataDiscriminator") != "Order":
            raise ParseError(
                section=_SECTION,
                row_index=row_index,
                column="DataDiscriminator",
                message=(
                    "unexpected DataDiscriminator; v1 only handles "
                    "'Order' rows. The splitter checks the 'SubTotal' and "
                    "'Total' rows and never passes them on."
                ),
                raw_value="",
            )
        require_cell(
            row,
            "Asset Category",
            _STOCKS,
            section=_SECTION,
            row_index=row_index,
            why=STOCKS_ONLY,
        )
        direction = _validate_code(row.get("Code", ""), row_index=row_index)
        resolved = _resolve_instrument(row, row_index=row_index, instruments=instruments)

        quantity_signed = _decimal(
            row["Quantity"], row_index=row_index, column="Quantity", kind=NumberKind.QUANTITY
        )
        if quantity_signed == 0:
            raise ParseError(
                section=_SECTION,
                row_index=row_index,
                column="Quantity",
                message=_ZERO_QUANTITY,
                raw_value="",
            )
        basis_signed = _decimal(
            row["Basis"], row_index=row_index, column="Basis", kind=NumberKind.MONEY
        )
        # T. Price is the raw per-share execution price. For OPENS the
        # cost comes from Basis, so it's unused; for CLOSES it's the
        # gross (pre-commission) sale price per share, used to file
        # FA-A3 gross proceeds (#348) — the
        # CLOSED_LOT Proceeds column is blank, so quantity * T.Price is
        # the only source of the gross sale consideration.
        t_price = _decimal(
            row["T. Price"], row_index=row_index, column="T. Price", kind=NumberKind.PRICE
        )
        # GitHub Codex (PR #136 review): IBKR emits Basis with the
        # SAME sign as Quantity on opening trades (shares *
        # per-share cost). On closing trades the same convention
        # holds: a long-close C row carries negative Quantity and
        # negative Basis (the basis being relieved from the lot).
        if basis_signed != 0 and ((quantity_signed < 0) != (basis_signed < 0)):
            raise ParseError(
                section=_SECTION,
                row_index=row_index,
                column="Basis",
                message=(
                    "has the opposite sign to Quantity — IBKR emits Basis with "
                    "the same sign as Quantity (shares * per-share "
                    "cost for opens; relieved basis for closes). A "
                    "sign mismatch indicates a malformed statement."
                ),
                raw_value="",
            )
        trade_dt = _parse_full_date_time(
            row["Date/Time"], row_index=row_index, column="Date/Time", period=period
        )
        # copy_abs() never rounds; abs() rounds to the context (#1311).
        basis_abs = basis_signed.copy_abs()

        if direction == _OPENING_TRADE_TOKEN:
            kind = ExecutionKind.OPEN
            side = Side.SHORT if quantity_signed < 0 else Side.LONG
            acquisition_cost, net_proceeds, trade_price = basis_abs, None, None
        else:
            realized_pnl = _decimal(
                row["Realized P/L"],
                row_index=row_index,
                column="Realized P/L",
                kind=NumberKind.MONEY,
            )
            if t_price < 0:
                raise ParseError(
                    section=_SECTION,
                    row_index=row_index,
                    column="T. Price",
                    message=(
                        "is negative on a closing trade — a sale price "
                        "cannot be negative (FA-A3 gross proceeds is "
                        "quantity * T.Price, issue #348). A zero price "
                        "(worthless-security removal / delisting) is "
                        "allowed and files gross proceeds 0; a blank "
                        "price is already rejected as a malformed decimal."
                    ),
                    raw_value="",
                )
            kind = ExecutionKind.CLOSE
            # A positive Quantity is a closing BUY (buy-to-cover of a
            # SHORT); match_fifo refuses it (#109).
            side = Side.SHORT if quantity_signed > 0 else Side.LONG
            net_proceeds = exactly(
                operator.add,
                basis_abs,
                realized_pnl,
                refusal=partial(
                    _precision_refusal,
                    section=_SECTION,
                    row_index=row_index,
                    column="Realized P/L",
                    at="the net proceeds arithmetic",
                ),
            )
            acquisition_cost, trade_price = None, t_price
        events.append(
            ExecutionEvent(
                currency_primary=resolved.currency,
                symbol=resolved.ticker,
                description=resolved.instrument.description,
                isin=Isin(resolved.isin_upper),
                listing_exchange=resolved.instrument.listing_exchange,
                issuer_country_code=CountryCode(resolved.country_prefix),
                kind=kind,
                side=side,
                trade_date_time=trade_dt,
                row_index=row_index,
                quantity=quantity_signed.copy_abs(),
                acquisition_cost=acquisition_cost,
                net_proceeds=net_proceeds,
                trade_price=trade_price,
            )
        )

    require(
        len(events) == len(materialised),
        what="parse_activity_statement_trades post: one ExecutionEvent per Trades row",
        row_count=len(materialised),
        event_count=len(events),
    )
    return events


def _precision_refusal(
    _out_of_memory: bool, *, section: str, row_index: int, column: str, at: str
) -> ParseError:
    """The refusal for arithmetic :func:`~ibkr_fa.parser._exact.exactly` can't keep.

    ``at`` is fixed text naming the arithmetic; the refusal prints no cell.
    """
    return ParseError(
        section=section,
        row_index=row_index,
        column=column,
        message=f"{at} exceeds the supported precision",
        raw_value="",
    )
