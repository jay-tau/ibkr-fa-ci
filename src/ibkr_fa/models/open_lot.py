"""One tax lot held on a report date, valued at that date's close.

Each :class:`OpenLot` is a single tax lot: the same security may appear
in several lots when it was acquired in tranches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Final

from ibkr_fa.invariants import require
from ibkr_fa.models._identifier_checks import require_lot_identifiers
from ibkr_fa.models._row_checks import require_amounts, require_exact_amount_fields
from ibkr_fa.types import CountryCode, Isin, Ticker

# Sanity bounds for ``report_date``. 1990 predates online retail
# brokerage; 2099 is far enough out that no legitimate Schedule FA
# filing today would reference such a date.
REPORT_DATE_MIN_YEAR: Final[int] = 1990
REPORT_DATE_MAX_YEAR: Final[int] = 2099


class Side(StrEnum):
    """Position side. IBKR encodes this as ``"Long"`` or ``"Short"``.

    For Schedule FA Table A3 v1 we expect only ``LONG`` lots; ``SHORT``
    lots will eventually be filtered out (a short position isn't a
    foreign asset to disclose). The enum exists so that filtering
    happens against a typed value rather than a magic string.
    """

    LONG = "Long"
    SHORT = "Short"


@dataclass(frozen=True, slots=True)
class OpenLot:
    """One tax lot held on :attr:`report_date`.

    Money amounts and quantities are :class:`~decimal.Decimal` (never
    ``float``) so source figures round-trip exactly through INR
    conversions and downstream comparisons.

    Construction-time invariants (see :meth:`__post_init__`) catch
    impossible combinations early — a non-finite amount, non-positive
    quantity, negative cost, malformed ISIN, a ``side`` that isn't a
    :class:`Side` member (its raw string included), etc. — so downstream
    consumers can assume well-formed data.

    Attributes:
        currency_primary: ISO 4217 currency the position is denominated
            in (e.g., ``"USD"``). v1 hard-fails on non-USD lots in the
            parser, but the field is preserved for future multi-currency
            support.
        symbol: Exchange-traded symbol.
        description: Human-readable security name (e.g., ``"EXAMPLE
            HOLDINGS INC"``). Used in the FA-A3 entity-name column.
        isin: ISO 6166 ISIN. Required by v1; rows missing an ISIN raise
            :class:`~ibkr_fa.exceptions.ParseError`.
        issuer_country_code: ISO 3166-1 alpha-2 country code of the
            issuing entity (NOT the listing exchange country). For
            example, a Dutch N.V. listed on NASDAQ has
            ``issuer_country_code`` ``"NL"``, not ``"US"``. This is the
            field FA-A3 cares about for the country column.
        report_date: The "as of" date of the holding. v1 requires this
            to equal ``date(calendar_year, 12, 31)`` and rejects
            otherwise.
        quantity: Number of shares in this lot. May be fractional.
        mark_price: Closing price per share on ``report_date`` in
            ``currency_primary``. ``quantity * mark_price`` is the
            FA-A3 closing value unless ``closing_total_usd`` is set.
        cost_basis_money: Acquisition value of the lot in
            ``currency_primary``: the cash paid for a purchase, and the
            FMV at acquisition for an ESPP purchase (ADR-0003
            decision 2) or an RSU vest. Source of truth for the FA-A3
            ``initial_value_inr`` column. A purchase's value must be
            the actual cost: India has no wash-sale rule, so a US
            wash-sale-adjusted basis is wrong here. An IBKR Activity
            Statement in "wash sales P/L" mode keeps ``Basis`` at the
            actual cost and tracks the disallowed loss on the
            replacement row's Realized P/L, which is correct for India.
            IBKR's ``LD`` representation instead rewrites ``Basis`` to
            the adjusted figure, so ``LD`` rows are refused by the
            Activity Statement parser until the actual cost can be
            backed out (issue #265).
        side: :class:`Side` — ``LONG`` or ``SHORT``.
        open_date_time: Date the lot was opened (acquisition trade
            date). Source of the TTBR lookup for ``initial_value_inr``.
        closing_total_usd: The broker statement's Dec-31 holding total in
            USD when this lot holds all of it, or this lot's share of it by
            quantity (#1243), else ``None``. When set, it is the FA-A3
            closing value and ``mark_price`` is review detail, one division
            of it by ``quantity`` (#1242).
        cost_basis_money_exact: The exact cost where ``cost_basis_money``
            can't hold it, such as a Robinhood lot's share of a third of its
            cost (#1241, #1243); ``cost_basis_money`` is then
            :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of it. ``None``
            where ``cost_basis_money`` is exact. The FA-A3 initial value
            converts this rational instead.
        closing_total_usd_exact: A Robinhood lot's exact share of a statement
            total split by quantity, including a share its Decimal can hold
            (#1243). This keeps split shares on the rational conversion path.
            It needs ``closing_total_usd``.
        quantity_exact: The exact quantity where ``quantity`` can't hold it,
            such as a Robinhood lot's share of a sale split pro rata across
            same-day lots (#1263); ``quantity`` is then
            :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of it. ``None``
            where ``quantity`` is exact. The FA-A3 peak and its closing floor
            read this rational instead.
    """

    currency_primary: str
    symbol: Ticker
    description: str
    isin: Isin
    issuer_country_code: CountryCode
    report_date: date
    quantity: Decimal
    mark_price: Decimal
    cost_basis_money: Decimal
    side: Side
    open_date_time: date
    closing_total_usd: Decimal | None = field(default=None, kw_only=True)
    cost_basis_money_exact: Fraction | None = field(default=None, kw_only=True)
    closing_total_usd_exact: Fraction | None = field(default=None, kw_only=True)
    quantity_exact: Fraction | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Fail-fast structural invariants. See class docstring."""
        require_amounts(self, "quantity", "mark_price", "cost_basis_money")
        require(
            self.closing_total_usd is None
            or (isinstance(self.closing_total_usd, Decimal) and self.closing_total_usd.is_finite()),
            what="OpenLot.closing_total_usd must be a finite Decimal or None",
        )
        require(
            isinstance(self.side, Side),
            what="OpenLot.side must be a Side enum member",
            side=self.side,
        )
        require(
            self.symbol != "",
            what="OpenLot.symbol must be non-empty",
        )
        require_lot_identifiers(
            "OpenLot",
            isin=self.isin,
            currency_field="currency_primary",
            currency=self.currency_primary,
            issuer_country_code=self.issuer_country_code,
        )
        require(
            self.quantity > 0,
            what=(
                "OpenLot.quantity must be positive (Open Positions snapshot "
                "uses positive shares-held regardless of Side; SHORT direction "
                "is tracked separately via the Side enum)"
            ),
            quantity=self.quantity,
            side=self.side,
        )
        require(
            self.mark_price >= 0,
            what="OpenLot.mark_price must be non-negative",
            mark_price=self.mark_price,
        )
        require(
            self.closing_total_usd is None or self.closing_total_usd >= 0,
            what="OpenLot.closing_total_usd must be None or non-negative",
        )
        require(
            self.cost_basis_money >= 0,
            what=(
                "OpenLot.cost_basis_money must be non-negative (cost basis is "
                "absolute money paid for the lot; Open Positions snapshot uses "
                "unsigned cost regardless of Side)"
            ),
            cost_basis_money=self.cost_basis_money,
        )
        require(
            REPORT_DATE_MIN_YEAR <= self.report_date.year <= REPORT_DATE_MAX_YEAR,
            what=(
                f"OpenLot.report_date.year must be in "
                f"[{REPORT_DATE_MIN_YEAR}, {REPORT_DATE_MAX_YEAR}]"
            ),
            report_date=self.report_date,
        )
        require(
            self.open_date_time <= self.report_date,
            what="OpenLot.open_date_time must be on or before report_date",
            open_date_time=self.open_date_time,
            report_date=self.report_date,
        )
        require_exact_amount_fields(
            self, "OpenLot", "cost_basis_money", "closing_total_usd", "quantity"
        )
