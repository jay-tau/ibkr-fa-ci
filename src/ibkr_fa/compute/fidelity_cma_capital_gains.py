"""FIFO-match Fidelity CMA SPAXX core activity into Indian tax lots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import NamedTuple

from ibkr_fa.compute.exact_inr import (
    checked_addition,
    checked_product,
    checked_sum,
    exact_share,
    review_amount,
)
from ibkr_fa.compute.lot_matching import FifoPool
from ibkr_fa.compute.schedule_cg import (
    ScheduleCgStcgComputeResult,
    compute_schedule_cg_stcg_lines,
)
from ibkr_fa.compute.schedule_cg_ltcg import (
    ScheduleCgLtcgComputeResult,
    compute_schedule_cg_ltcg_lines,
)
from ibkr_fa.compute.schedule_cg_portal_amounts import require_schedule_cg_filed_amounts
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.models import BasisProvenance, ClosedLot, OpenLot
from ibkr_fa.models.open_lot import Side
from ibkr_fa.models.schedule_cg_row import is_short_term_holding
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.parser.fidelity_cma import (
    CmaCoreTransaction,
    CmaCoreTransactionKind,
    CmaStatement,
    CmaStatementBundle,
)
from ibkr_fa.types import CountryCode, Isin, Ticker

_DESCRIPTION = "FIDELITY GOVERNMENT MONEY MARKET FUND"


class FidelityCmaCapitalGainsError(IbkrFaError):
    """The CMA SPAXX lot history cannot produce a certain Indian tax result."""


class FidelityCmaCgError(IbkrFaError):
    """The CMA capital-gains run cannot proceed without guessing."""


class _Row(NamedTuple):
    """A Core Fund activity row, with its 1-based statement and row numbers."""

    transaction: CmaCoreTransaction
    statement: int
    row: int

    def at(self, column: str) -> str:
        """The fixed refusal label for ``column`` of this row (#1273)."""
        return (
            f"SPAXX lot allocation, statement {self.statement} activity row {self.row}, "
            f"column {column}"
        )


def _share(
    amount: Decimal, part: Decimal, whole: Decimal, *, at: _Row
) -> tuple[Decimal, Fraction | None]:
    """The share of ``amount`` that ``part`` of ``whole`` carries, exactly (#1244).

    Returns ``amount * part / whole`` at the caller's precision where that is
    the exact share, else :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of
    it, plus the exact rational where that Decimal can't hold it. Refuses,
    naming ``at``, a share past the exact-arithmetic digit cap.
    """
    exact = exact_share(amount, part, whole)
    if exact is None:
        raise FidelityCmaCapitalGainsError(
            f"SPAXX lot allocation, statement {at.statement} activity row {at.row}: "
            "the share needs more digits than exact arithmetic allows"
        )
    shown = amount * part / whole
    return review_amount(shown, None if Fraction(shown) == exact else exact)


def _closed_lot(
    opened: CmaCoreTransaction,
    sale: _Row,
    quantity: Decimal,
    *,
    spaxx_isin: Isin,
) -> ClosedLot:
    sold = sale.transaction
    basis, basis_exact = _share(opened.amount_usd, quantity, opened.quantity, at=sale)
    proceeds, proceeds_exact = _share(
        sold.amount_usd.copy_abs(), quantity, sold.quantity.copy_abs(), at=sale
    )
    return ClosedLot(
        currency_primary="USD",
        sub_category=None,
        symbol=Ticker("SPAXX"),
        description=_DESCRIPTION,
        isin=spaxx_isin,
        listing_exchange=None,
        issuer_country_code=CountryCode("US"),
        quantity=quantity,
        cost_basis_money=basis,
        proceeds_money=proceeds,
        open_date_time=opened.date,
        close_date_time=sold.date,
        gross_proceeds_money=checked_product(
            quantity, sold.price_per_unit_usd, at=sale.at("gross_proceeds_money")
        ),
        basis_provenance=BasisProvenance.ENGINE_FIFO,
        cost_basis_money_exact=basis_exact,
        proceeds_money_exact=proceeds_exact,
    )


def _fifo(
    statements: CmaStatementBundle,
    *,
    spaxx_isin: Isin,
) -> tuple[tuple[CmaStatement, ...], FifoPool[_Row], tuple[ClosedLot, ...]]:
    bundle = statements.statements
    if bundle[0].position.beginning_market_value_usd is not None:
        raise FidelityCmaCapitalGainsError(
            "SPAXX capital gains require statements starting before any carried holding"
        )

    opens: FifoPool[_Row] = FifoPool()
    running_quantity = Decimal(0)
    closed: list[ClosedLot] = []
    for statement_number, statement in enumerate(bundle, start=1):
        # Printed order is Fidelity's only same-day ordering evidence; the parser
        # independently proves it through the Core Fund running-balance chain.
        for row_number, transaction in enumerate(statement.core_transactions, start=1):
            row = _Row(transaction, statement_number, row_number)
            if transaction.kind is not CmaCoreTransactionKind.SELL:
                opens.add(row, transaction.quantity)
                running_quantity = checked_addition(
                    running_quantity, transaction.quantity, at=row.at("quantity")
                )
                continue

            consumption = opens.consume(
                transaction.quantity.copy_abs(), at=row.at("quantity"), prefer_open_on_tie=True
            )
            if consumption.shortfall > 0:
                raise FidelityCmaCapitalGainsError(
                    "SPAXX sale cannot be matched to complete prior acquisition history"
                )
            closed.extend(
                _closed_lot(opened.transaction, row, quantity, spaxx_isin=spaxx_isin)
                for opened, quantity in consumption.allocations
            )
            running_quantity = checked_addition(
                running_quantity, transaction.quantity, at=row.at("quantity")
            )

        if running_quantity != statement.position.quantity:
            raise FidelityCmaCapitalGainsError(
                "SPAXX transaction quantities do not match the monthly holding"
            )
    return bundle, opens, tuple(closed)


def build_fidelity_cma_closed_lots(
    statements: CmaStatementBundle,
    *,
    spaxx_isin: Isin,
) -> tuple[ClosedLot, ...]:
    """Return FIFO slices for every SPAXX sale in a complete statement bundle."""
    _bundle, _opens, closed = _fifo(statements, spaxx_isin=spaxx_isin)
    return closed


def in_fy_spaxx_sale_count(
    statements: CmaStatementBundle,
    financial_year: FinancialYear,
) -> int:
    """Count the SPAXX redemptions dated in the FY."""
    return sum(
        transaction.kind is CmaCoreTransactionKind.SELL
        and financial_year.start_date <= transaction.date <= financial_year.end_date
        for statement in statements.statements
        for transaction in statement.core_transactions
    )


@dataclass(frozen=True, slots=True, repr=False)
class FidelityCmaCapitalGains:
    """Every FIFO slice of the bundle and the FY's Schedule CG results."""

    closed_lots: tuple[ClosedLot, ...]
    stcg: ScheduleCgStcgComputeResult
    ltcg: ScheduleCgLtcgComputeResult


def compute_fidelity_cma_capital_gains(
    statements: CmaStatementBundle,
    *,
    spaxx_isin: Isin,
    fx_ctx: TaxFxContext,
) -> FidelityCmaCapitalGains:
    """FIFO-match the bundle and compute SPAXX STCG and LTCG for ``fx_ctx``'s FY.

    Shared by ``fidelity-cma-cg`` and ``fidelity-cma-income`` so Schedule CG
    and Schedule FSI use the same STCG and LTCG compute results.
    """
    financial_year = fx_ctx.financial_year
    closed_lots = build_fidelity_cma_closed_lots(statements, spaxx_isin=spaxx_isin)
    if any(
        financial_year.start_date <= lot.close_date_time <= financial_year.end_date
        and not is_short_term_holding(lot.open_date_time, lot.close_date_time)
        for lot in closed_lots
    ):
        # ponytail: current in-FY evidence is short-term under either possible
        # section 50AA interpretation; refuse longer holdings until authoritative.
        raise FidelityCmaCgError(
            "in-FY SPAXX holdings over 24 months are unsupported pending "
            "authoritative section 50AA treatment"
        )
    stcg = compute_schedule_cg_stcg_lines(
        closed_lots,
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
        fx_policy=fx_ctx.capital_gains,
    )
    ltcg = compute_schedule_cg_ltcg_lines(
        closed_lots,
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
        fx_policy=fx_ctx.capital_gains,
    )
    if ltcg.ltcg_lines:
        raise FidelityCmaCgError("unsupported long-term SPAXX lots reached output")
    require_schedule_cg_filed_amounts(stcg.stcg_lines, schedule="STCG")
    return FidelityCmaCapitalGains(closed_lots=closed_lots, stcg=stcg, ltcg=ltcg)


@dataclass(frozen=True, slots=True, repr=False)
class FidelityCmaLotInventory:
    """FIFO-resolved SPAXX lots as of one report date."""

    open_lots: tuple[OpenLot, ...]
    closed_lots: tuple[ClosedLot, ...]


def _open_lot(
    purchase: _Row,
    remaining: Decimal,
    *,
    spaxx_isin: Isin,
    report_date: date,
    mark_price: Decimal,
) -> OpenLot:
    opened = purchase.transaction
    cost, cost_exact = _share(opened.amount_usd, remaining, opened.quantity, at=purchase)
    return OpenLot(
        currency_primary="USD",
        symbol=Ticker("SPAXX"),
        description=_DESCRIPTION,
        isin=spaxx_isin,
        issuer_country_code=CountryCode("US"),
        report_date=report_date,
        quantity=remaining,
        mark_price=mark_price,
        cost_basis_money=cost,
        side=Side.LONG,
        open_date_time=opened.date,
        cost_basis_money_exact=cost_exact,
    )


def build_fidelity_cma_lot_inventory(
    statements: CmaStatementBundle,
    *,
    spaxx_isin: Isin,
    report_date: date,
    mark_price: Decimal,
) -> FidelityCmaLotInventory:
    """Return remaining open lots plus every prior FIFO closure."""
    if not mark_price.is_finite() or mark_price <= 0:
        raise FidelityCmaCapitalGainsError("SPAXX report-date NAV must be positive")
    bundle, opens, closed = _fifo(statements, spaxx_isin=spaxx_isin)
    if bundle[0].beginning_account_value_usd is not None:
        raise FidelityCmaCapitalGainsError(
            "Schedule FA inventory requires statements from before any carried account balance"
        )
    if bundle[-1].period_end != report_date:
        raise FidelityCmaCapitalGainsError(
            "SPAXX lot inventory requires a statement ending on the report date"
        )
    open_lots = tuple(
        _open_lot(
            opened, remaining, spaxx_isin=spaxx_isin, report_date=report_date, mark_price=mark_price
        )
        for opened, remaining in opens.remaining()
    )
    held = checked_sum(
        enumerate((lot.quantity for lot in open_lots), start=1),
        at=lambda position: f"SPAXX report-date holding, open lot {position}, column quantity",
    )
    if held != bundle[-1].position.quantity:
        raise FidelityCmaCapitalGainsError(
            "remaining SPAXX FIFO quantity does not match the report-date holding"
        )
    return FidelityCmaLotInventory(open_lots=open_lots, closed_lots=closed)
