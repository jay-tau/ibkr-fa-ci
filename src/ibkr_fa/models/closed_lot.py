"""One disposed tax lot: a matched acquisition and disposal.

Every source builds :class:`ClosedLot` records, and
:attr:`ClosedLot.basis_provenance` records who chose the lot's cost
basis (see :class:`BasisProvenance`). A source whose INR cost of
acquisition is set by statute rather than by converting
``cost_basis_money`` passes it as
:attr:`ClosedLot.cost_basis_inr_override` (see
:class:`StatutoryInrCostBasis`). A partial disposal of one
acquisition produces one :class:`ClosedLot` per matched slice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from ibkr_fa.invariants import require
from ibkr_fa.models._identifier_checks import require_lot_identifiers
from ibkr_fa.models._row_checks import require_amounts, require_exact_amount_fields
from ibkr_fa.types import CountryCode, Isin, Ticker


class BasisProvenance(StrEnum):
    """Who chose a :class:`ClosedLot`'s cost basis (#640)."""

    BROKER_MATCHED = "broker_matched"
    """The broker matched the lot and reported its cost (Flex
    ``CLOSED_LOT`` rows). The cost may carry a US wash-sale adjustment, so
    :func:`ibkr_fa.compute.lot_matching.require_indian_basis` checks it
    (#599)."""

    ENGINE_FIFO = "engine_fifo"
    """ibkr-fa matched the lot FIFO from the source's acquisitions and
    costs it at the actual acquisition cost (Activity Statement, Fidelity
    CMA, Robinhood)."""

    S49_2AA = "s49_2aa"
    """The cost is the fair market value taken into account under
    §17(2)(vi), per §49(2AA) (Fidelity ESPP lots). Exactly these lots
    carry :attr:`ClosedLot.cost_basis_inr_override`."""


@dataclass(frozen=True, slots=True)
class StatutoryInrCostBasis:
    """A statutory historical INR cost of acquisition for one lot (#633).

    The Schedule CG engines file it as the lot's INR cost instead of
    converting ``cost_basis_money`` at the transfer-date rate. Proceeds
    are still converted under Rule 115.

    Attributes:
        amount_inr: The INR cost of the disposed quantity, at full
            :class:`~decimal.Decimal` precision. Finite and ``>= 0``.
        source: Audit provenance, written to the CG line's
            ``cost_basis_inr_source``. Non-blank.
    """

    amount_inr: Decimal
    source: str

    def __post_init__(self) -> None:
        """Refuse a non-finite or negative amount and a blank source."""
        require(
            self.amount_inr.is_finite() and self.amount_inr >= 0,
            what="StatutoryInrCostBasis.amount_inr must be finite and non-negative",
            amount_inr=self.amount_inr,
        )
        require(
            bool(self.source.strip()),
            what="StatutoryInrCostBasis.source must be non-blank",
        )


@dataclass(frozen=True, slots=True)
class ClosedLot:
    """One disposed tax lot, from any source.

    Money amounts and quantities are :class:`~decimal.Decimal` (never
    ``float``) so source figures round-trip exactly through INR
    conversions and downstream comparisons.

    Construction-time invariants (see :meth:`__post_init__`) catch
    impossible combinations early — a non-finite amount, close before
    open, malformed ISIN, negative cost — so downstream consumers can
    assume well-formed data.

    Attributes:
        currency_primary: ISO 4217 currency the position was
            denominated in (e.g., ``"USD"``). v1 hard-fails on non-USD
            lots in the parser.
        sub_category: Security class as the source states it (e.g.,
            ``"COMMON"``, ``"ETF"``), or ``None`` when the source does
            not state one.
        symbol: Exchange-traded symbol.
        description: Human-readable security name.
        isin: ISO 6166 ISIN.
        listing_exchange: Exchange the security trades on, as the source
            states it, or ``None`` when the source does not state one.
        issuer_country_code: ISO 3166-1 alpha-2 country code of the
            issuing entity (NOT the listing exchange country).
        quantity: Number of shares disposed in this closure. Always
            positive: this is the closed-lot quantity, not a signed
            trade quantity.
        cost_basis_money: Cost basis in ``currency_primary`` allocated
            to the disposed quantity; :attr:`basis_provenance` says who
            chose it. Source of truth for FA-A3 ``initial_value_inr``
            for closed lots.
        proceeds_money: Net sale proceeds in ``currency_primary``
            allocated to the disposed quantity.
            Used for capital gains, never as FA-A3 gross sale
            consideration.
        open_date_time: Date the lot was originally acquired. Source of
            the TTBR lookup for FA-A3 ``initial_value_inr``.
        close_date_time: Date the lot was disposed of (sold or covered).
            Used to determine CY-overlap and retain disposal-date audit
            provenance for FA-A3 (issue #16).
        gross_proceeds_money: GROSS sale consideration (before
            commission and sale fees) in ``currency_primary``, i.e.
            ``quantity * per-share execution price``. Unlike the
            **net** ``proceeds_money``, this is the raw sale price the
            FA-A3 "gross proceeds" column actually asks for (issue
            #348). Set only where the source proves it: the IBKR
            Activity Statement (``T. Price``) and Fidelity CMA. Robinhood
            sets its recorded ``Amount``, which it files as the sale's full
            value of consideration (owner decision 2026-11-14, #1383; ADR
            0006 section 7). ``None`` for IBKR Flex and NetBenefits ESPP,
            whose proceeds are not proven pre-fee; FA-A3 then remains
            manual-fill. Morgan Stanley StockPlan builds no ``ClosedLot``.
            Always ``>= 0`` when present.
        basis_provenance: Who chose ``cost_basis_money``. Required,
            with no default, so no lot gets a guessed provenance.
        holding_period_date_time: For a ``BROKER_MATCHED`` lot, the
            broker's holding-period start date, which the #599 check
            compares with ``open_date_time``. Required for, and only
            for, ``BROKER_MATCHED`` lots.
        cost_basis_inr_override: The statutory INR cost the Schedule CG
            engines file instead of converting ``cost_basis_money``.
            Required for, and only for, ``S49_2AA`` lots.
        cost_basis_money_exact: The exact cost where ``cost_basis_money``
            can't hold it, such as a Robinhood lot's share of a third of its
            cost (#1241); ``cost_basis_money`` is then
            :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of it. ``None``
            where ``cost_basis_money`` is exact. The Schedule CG engines
            convert and file this rational instead.
        proceeds_money_exact: The same for ``proceeds_money``.
        quantity_exact: The same for ``quantity``, such as a Robinhood lot's
            share of a sale split pro rata across same-day lots (#1263). The
            FA-A3 peak reads this rational instead.
        gross_proceeds_money_exact: The same for ``gross_proceeds_money``,
            such as a Robinhood lot's share of a sale's Amount (#1383); always
            ``None`` where ``gross_proceeds_money`` is. FA-A3 converts this
            rational instead.
    """

    currency_primary: str
    sub_category: str | None
    symbol: Ticker
    description: str
    isin: Isin
    listing_exchange: str | None
    issuer_country_code: CountryCode
    quantity: Decimal
    cost_basis_money: Decimal
    proceeds_money: Decimal
    open_date_time: date
    close_date_time: date
    gross_proceeds_money: Decimal | None = None
    basis_provenance: BasisProvenance = field(kw_only=True)
    holding_period_date_time: date | None = field(default=None, kw_only=True)
    cost_basis_inr_override: StatutoryInrCostBasis | None = field(default=None, kw_only=True)
    cost_basis_money_exact: Fraction | None = field(default=None, kw_only=True)
    proceeds_money_exact: Fraction | None = field(default=None, kw_only=True)
    quantity_exact: Fraction | None = field(default=None, kw_only=True)
    gross_proceeds_money_exact: Fraction | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Fail-fast structural invariants. See class docstring."""
        require_amounts(self, "quantity", "cost_basis_money", "proceeds_money")
        require_amounts(self, "gross_proceeds_money", optional=True)
        require(
            self.symbol != "",
            what="ClosedLot.symbol must be non-empty",
        )
        require_lot_identifiers(
            "ClosedLot",
            isin=self.isin,
            currency_field="currency_primary",
            currency=self.currency_primary,
            issuer_country_code=self.issuer_country_code,
        )
        # ClosedLot.quantity is "always positive" per docstring — the
        # close direction lives in Buy/Sell on the source row, not in
        # the sign of this quantity field.
        require(
            self.quantity > 0,
            what="ClosedLot.quantity must be positive (the closed-lot quantity is unsigned)",
            quantity=self.quantity,
        )
        require(
            self.cost_basis_money >= 0,
            what="ClosedLot.cost_basis_money must be non-negative",
            cost_basis_money=self.cost_basis_money,
        )
        require(
            self.close_date_time >= self.open_date_time,
            what="ClosedLot.close_date_time must be on or after open_date_time",
            open_date_time=self.open_date_time,
            close_date_time=self.close_date_time,
        )
        # gross_proceeds_money (issue #348) is the raw pre-fee sale
        # consideration (quantity * execution price). When supplied it is
        # non-negative by construction (positive quantity * a
        # non-negative price — zero for a worthless-security removal);
        # a negative value signals a buggy parser.
        require(
            self.gross_proceeds_money is None or self.gross_proceeds_money >= 0,
            what="ClosedLot.gross_proceeds_money must be non-negative when present",
            gross_proceeds_money=self.gross_proceeds_money,
        )
        require(
            isinstance(self.basis_provenance, BasisProvenance),
            what="ClosedLot.basis_provenance must be a BasisProvenance enum member",
            basis_provenance=self.basis_provenance,
        )
        require(
            (self.holding_period_date_time is not None)
            == (self.basis_provenance is BasisProvenance.BROKER_MATCHED),
            what=(
                "ClosedLot.holding_period_date_time is required for, and only "
                "for, BROKER_MATCHED lots"
            ),
            basis_provenance=self.basis_provenance,
            holding_period_date_time=self.holding_period_date_time,
        )
        require(
            (self.cost_basis_inr_override is not None)
            == (self.basis_provenance is BasisProvenance.S49_2AA),
            what="ClosedLot.cost_basis_inr_override is required for, and only for, S49_2AA lots",
            basis_provenance=self.basis_provenance,
        )
        require_exact_amount_fields(
            self,
            "ClosedLot",
            "cost_basis_money",
            "proceeds_money",
            "quantity",
            "gross_proceeds_money",
        )
