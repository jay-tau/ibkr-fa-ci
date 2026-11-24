"""The Robinhood normalizer keeps every digit (#1308).

The statement parser accepts in-limit cells of up to 35 digits whose totals
match their rows exactly (#1275, #1295). The normalizer's cash identities,
statement position checks, quantity bands, movement quantities and display
round trips now add, subtract, negate and compare those values with every
digit too, so a statement can neither fail an identity it meets nor pass one
it breaks only past Decimal's default 28 digits. Integration cases run the real
readers and the normalizer's public functions the way ``load_robinhood_bundle``
does. Incorrect values refuse or block with the existing messages.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Iterator
from decimal import Context, Decimal, localcontext
from typing import Final

import pytest

import ibkr_fa.normalize.robinhood.activity as activity_mod
from ibkr_fa.compute.robinhood_lots import reconcile_robinhood_security_lots
from ibkr_fa.config import RobinhoodSecurityReceiptResolution, RobinhoodSecurityReceiptTreatment
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountsReconciliation,
    RobinhoodCashReconciliation,
    RobinhoodInventoryBlockerKind,
    RobinhoodQuantityMovementKind,
    RobinhoodReconciliationError,
    RobinhoodSecurityQuantityResult,
    reconcile_robinhood_activity,
    reconcile_robinhood_cash,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.parser.robinhood import RobinhoodEventKind, parse_robinhood_activity
from tests._robinhood_exact_bundle import (
    CUSIP,
    INDIVIDUAL,
    MANAGED,
    MANAGED_ACCOUNT,
    SYMBOL,
    Loan,
    Position,
    Row,
    Sweep,
    activity_csv,
    exactly,
    one_account,
    pair,
    parse,
    quantities,
    statement,
    usd,
)
from tests._robinhood_refusals import CASH_IDENTITY_REFUSAL as _CASH_IDENTITY
from tests._robinhood_refusals import REC_CONTRADICTION, TRANSFER_CONTRADICTION

_CUSIP_LINE: Final = f"CUSIP: {CUSIP}"
_DEPOSIT_DAY: Final = "1/10/2025"


def _refuses(message: str, build: Callable[[], object]) -> RobinhoodReconciliationError:
    """``build()`` refuses with the fixed ``message``, chaining nothing."""
    with pytest.raises(RobinhoodReconciliationError) as raised:
        build()
    assert raised.value.message == message
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    return raised.value


def _deposit_csv(amount: str) -> tuple[str, ...]:
    return (_DEPOSIT_DAY, _DEPOSIT_DAY, _DEPOSIT_DAY, "", "ACH Deposit", "ACH", "", "", amount)


def _deposit_row(amount: str) -> Row:
    return Row("ACH", _DEPOSIT_DAY, credit=amount, description="ACH Deposit")


def _coin(amount: str) -> Row:
    """A statement-only Crypto cash credit, absent from the activity CSV."""
    return Row("COIN", "1/12/2025", credit=amount, description="Crypto Money Movement")


# ---- cash identities ----


def _cash(
    *,
    opening_cash: str,
    deposit: str,
    coins: tuple[str, ...],
    closing_cash: str,
) -> RobinhoodCashReconciliation:
    """One legacy statement opening with ``opening_cash`` (no securities),
    crediting ``deposit`` (in the CSV too) and the Crypto ``coins``, and
    closing with ``closing_cash``."""
    text = statement(
        opening=(opening_cash, "0", opening_cash),
        closing_cash=closing_cash,
        rows=(_deposit_row(usd(deposit)), *(_coin(usd(coin)) for coin in coins)),
    )
    return one_account(text, (_deposit_csv(usd(deposit)),))


def test_example_a_statement_only_credits_reconcile_past_28_digits() -> None:
    """Each $0.000000000000005 credit added to the $10 trillion deposit needs
    29 digits; main rounded both away and refused the exact closing cash."""
    cash = _cash(
        opening_cash="0",
        deposit="10000000000000.00",
        coins=("0.000000000000005", "0.000000000000005"),
        closing_cash="10000000000000.00000000000001",
    )

    assert len(cash.activity_reconciliation.crypto_rows) == 2


def test_example_b_closing_cash_missing_a_credit_refuses() -> None:
    """The Account Activity total says cash rose by the $0.000000000000004
    credit too, but the closing cash leaves it out; main's 28-digit net
    dropped it and passed."""
    _refuses(
        _CASH_IDENTITY,
        lambda: _cash(
            opening_cash="0",
            deposit="10000000000000.00",
            coins=("0.000000000000004",),
            closing_cash="10000000000000.00",
        ),
    )


# X + 0.49 and X + 0.51 units of the 28th digit: main rounded the opening cash
# down before adding the credit, and the closing cash up.
_OPENING_JUST_BELOW: Final = "100000000000000.000000000000049"
_CLOSING_JUST_ABOVE: Final = "100000000000001.000000000000051"


def test_the_opening_side_of_the_cash_identity_adds_with_every_digit() -> None:
    _cash(
        opening_cash=_OPENING_JUST_BELOW,
        deposit="1.00",
        coins=("0.000000000000002",),
        closing_cash=_CLOSING_JUST_ABOVE,
    )


@pytest.mark.parametrize(
    ("coins", "closing_cash"),
    [
        pytest.param(
            ("0.000000000000005", "0.000000000000005"),
            "10000000000000.00000000000002",
            id="a-unit-too-much",
        ),
        pytest.param(("0.000000000000005", "0.000000000000005"), "10000000000000.00", id="a"),
        pytest.param(("0.000000000000002",), "10000000000000.000000000000001", id="b-unit-short"),
    ],
)
def test_closing_cash_that_misses_exact_activity_refuses(
    coins: tuple[str, ...], closing_cash: str
) -> None:
    _refuses(
        _CASH_IDENTITY,
        lambda: _cash(
            opening_cash="0", deposit="10000000000000.00", coins=coins, closing_cash=closing_cash
        ),
    )


@pytest.mark.parametrize(
    ("coins", "closing_cash", "reconciles"),
    [
        pytest.param(("0.01", "0.02"), "10000000000000.03", True, id="matches"),
        pytest.param(("0.01", "0.02"), "10000000000000.04", False, id="a-cent-off"),
    ],
)
def test_cash_that_fits_28_digits_reconciles_as_before(
    coins: tuple[str, ...], closing_cash: str, reconciles: bool
) -> None:
    def build() -> object:
        return _cash(
            opening_cash="0", deposit="10000000000000.00", coins=coins, closing_cash=closing_cash
        )

    if reconciles:
        build()
    else:
        _refuses(_CASH_IDENTITY, build)


# A Deposit Sweep movement of 30 digits from $2 billion of brokerage cash.
_SWEPT: Final = "1000000000.00000000000000000001"


def _sweep(swept: str = _SWEPT) -> RobinhoodCashReconciliation:
    text = statement(
        opening=("2000000000.00", "0", "2000000010.00"),
        closing_cash=exactly("2000000001.00", f"-{swept}"),
        rows=(_deposit_row("$1.00"),),
        sweep=Sweep("10.00", (("", swept),)),
    )
    return one_account(text, (_deposit_csv("$1.00"),))


def test_a_long_deposit_sweep_movement_reconciles() -> None:
    """Main's 28-digit sweep net lost the movement's last digit and refused
    with ``Deposit Sweep net does not reconcile``."""
    cash = _sweep()

    assert cash.periods[0].program_bank_present


def test_a_deposit_sweep_movement_that_fits_28_digits_reconciles_as_before() -> None:
    _sweep("1000000000.000000000000000001")


# ---- the in-kind and managed-trade bands ----

_LONG_QUANTITY: Final = "100000000000.00000000000000000001"
_TWO_UNITS_ABOVE: Final = "100000000000.00000000000000000003"


def _transfer_csv(quantity: str) -> tuple[str, ...]:
    return (
        "1/12/2025",
        "1/12/2025",
        "1/12/2025",
        SYMBOL,
        f"Synthetic Equity\n{_CUSIP_LINE}",
        "ITRF",
        quantity,
        "",
        "",
    )


def _in_kind(csv_quantity: str, statement_quantity: str) -> RobinhoodCashReconciliation:
    """An account receives ``statement_quantity`` shares in kind from another
    broker, which its activity CSV reports as ``csv_quantity``."""
    row = Row(
        "ITRF",
        "1/12/2025",
        quantity=statement_quantity,
        symbol=SYMBOL,
        description="Synthetic Equity",
        lines=(_CUSIP_LINE,),
    )
    text = statement(closing_cash="10.00", rows=(_deposit_row("$10.00"), row))
    return one_account(text, (_deposit_csv("$10.00"), _transfer_csv(csv_quantity)))


def test_example_e_in_kind_quantities_two_units_apart_past_28_digits_refuse() -> None:
    """Main rounded both 32-digit sums to 100000000000 and passed them."""
    _refuses(
        "in-kind aggregate quantity evidence disagrees",
        lambda: _in_kind(_LONG_QUANTITY, _TWO_UNITS_ABOVE),
    )


def _in_kind_rows(
    csv_quantities: tuple[str, ...], statement_quantities: tuple[str, ...]
) -> RobinhoodCashReconciliation:
    """:func:`_in_kind` with several rows on each side."""
    rows = tuple(
        Row(
            "ITRF",
            "1/12/2025",
            quantity=quantity,
            symbol=SYMBOL,
            description="Synthetic Equity",
            lines=(_CUSIP_LINE,),
        )
        for quantity in statement_quantities
    )
    text = statement(closing_cash="10.00", rows=(_deposit_row("$10.00"), *rows))
    return one_account(
        text,
        (_deposit_csv("$10.00"), *(_transfer_csv(quantity) for quantity in csv_quantities)),
    )


def test_in_kind_rows_that_agree_exactly_reconcile_where_rounding_set_them_apart() -> None:
    """Both sides total 100000000000.00000000000000008 shares, printed to 17
    places (half unit 5E-18). Main rounded the report's first row down before
    adding the second, and the statement's total up, and refused the 1E-16 gap."""
    cash = _in_kind_rows(
        ("100000000000.00000000000000004", "0.00000000000000004"),
        ("100000000000.00000000000000000", "0.00000000000000008"),
    )

    assert len(cash.activity_reconciliation.in_kind_transfer_groups) == 1


def test_in_kind_quantities_two_units_apart_within_28_digits_still_refuse() -> None:
    _refuses(
        "in-kind aggregate quantity evidence disagrees",
        lambda: _in_kind("10000000000.0000000000000001", "10000000000.0000000000000003"),
    )


def _managed_buy(
    quantity: str, statement_price: str, amount: str = "$100,000,000,000.00"
) -> RobinhoodCashReconciliation:
    """A Managed Individual account buys ``quantity`` shares for ``amount``
    at a CSV price of $10.00, which the statement prints as
    ``statement_price``."""
    buy = Row(
        "Buy",
        "1/13/2025",
        quantity=quantity,
        price=statement_price,
        debit=amount,
        symbol=SYMBOL,
        description="Synthetic Equity",
        lines=(_CUSIP_LINE,),
    )
    text = statement(managed=True, rows=(_deposit_row(amount), buy))
    csv_buy = (
        "1/13/2025",
        "1/13/2025",
        "1/14/2025",
        SYMBOL,
        f"Synthetic Equity\n{_CUSIP_LINE}\nManaged Trade",
        "Buy",
        quantity,
        "$10.00",
        f"({amount})",
    )
    return one_account(text, (_deposit_csv(amount), csv_buy))


_MANAGED_QUANTITY: Final = "10000000000.00000000000000000001"


def test_a_managed_price_past_its_exact_band_refuses() -> None:
    """The band adds the amount's half cent spread over the 31-digit quantity:
    $0.005 / 10000000000.00000000000000000001 is just under $5E-13, so the
    statement's $10.005000000001 is about 5E-43 past it. Main's division rounded the
    spread up to $5E-13 and passed."""
    _refuses(
        "activity quantity or price evidence disagrees",
        lambda: _managed_buy(_MANAGED_QUANTITY, "$10.005000000001"),
    )


def test_a_managed_price_difference_past_28_digits_refuses() -> None:
    _refuses(
        "activity quantity or price evidence disagrees",
        lambda: _managed_buy("0.00000000005", "$100,000,010.00500000000000000001", amount="$0.01"),
    )


@pytest.mark.parametrize(
    ("quantity", "statement_price"),
    [
        pytest.param(_MANAGED_QUANTITY, "$10.005000000000", id="inside-the-band"),
        pytest.param("10000000000", "$10.005000000001", id="on-the-band-edge"),
    ],
)
def test_a_managed_price_within_its_exact_band_reconciles(
    quantity: str, statement_price: str
) -> None:
    _managed_buy(quantity, statement_price)


# Bought for $0.01, so each band's edge is exactly
# |$10.00 - price| x quantity = ($0.005 + half the price's unit) x quantity + $0.005.
# Rounded to 28 digits, the first edge's right side and the second's
# tolerance product each lose digits downward.
_EDGES: Final = pytest.mark.parametrize(
    ("quantity", "price", "past"),
    [
        pytest.param(
            "0.001073741824",
            "$14.66161287307739257813",
            "$14.66161287307739257814",
            id="sum-rounds-down",
        ),
        pytest.param(
            "0.00000034359738368",
            "$14,561.92022836685180664063",
            "$14,561.92022836685180664064",
            id="product-rounds-down",
        ),
    ],
)


@_EDGES
def test_a_managed_price_on_its_exact_band_edge_reconciles(
    quantity: str, price: str, past: str
) -> None:
    del past
    _managed_buy(quantity, price, amount="$0.01")


@_EDGES
def test_a_managed_price_a_unit_past_its_exact_band_edge_refuses(
    quantity: str, price: str, past: str
) -> None:
    del price
    _refuses(
        "activity quantity or price evidence disagrees",
        lambda: _managed_buy(quantity, past, amount="$0.01"),
    )


def test_a_managed_price_a_unit_past_a_short_band_still_refuses() -> None:
    _refuses(
        "activity quantity or price evidence disagrees",
        lambda: _managed_buy("10000000000", "$10.005000000002"),
    )


# ---- statement positions, movements and the replay ----


def _trade_csv(code: str, day: str, quantity: str, amount: str) -> tuple[str, ...]:
    return (
        day,
        day,
        day,
        SYMBOL,
        f"Synthetic Equity\n{_CUSIP_LINE}",
        code,
        quantity,
        "$1.00",
        f"({amount})" if code == "Buy" else amount,
    )


def _trade_row(code: str, day: str, quantity: str, amount: str) -> Row:
    return Row(
        code,
        day,
        quantity=quantity,
        price="$1.00",
        debit=amount if code == "Buy" else "",
        credit=amount if code == "Sell" else "",
        symbol=SYMBOL,
        description="Synthetic Equity",
        lines=(_CUSIP_LINE,),
    )


def _held(
    trades: tuple[tuple[str, str, str, str], ...],
    *,
    deposit: str,
    closing_cash: str,
    positions: tuple[Position, ...] = (),
    loans: tuple[Loan, ...] = (),
) -> RobinhoodAccountsReconciliation:
    """The individual account deposits ``deposit`` and makes ``trades``,
    each ``(code, day, quantity, amount)`` at $1.00, then holds
    ``positions``; the managed account only deposits."""
    text = statement(
        closing_cash=closing_cash,
        positions=positions,
        loans=loans,
        rows=(_deposit_row(deposit), *(_trade_row(*trade) for trade in trades)),
    )
    rows = (_deposit_csv(deposit), *(_trade_csv(*trade) for trade in trades))
    return pair((text, rows), MANAGED_ACCOUNT)


_HOLDING_PRICE: Final = "$123,456,789.12345678901234567891"


def _one_share(price: str) -> RobinhoodAccountsReconciliation:
    cost = "$123,456,789.12"
    return _held(
        (("Buy", "1/13/2025", "1", cost),),
        deposit=cost,
        closing_cash="0",
        positions=(Position("1", price, price=price),),
    )


def test_example_c_a_29_digit_holding_reconciles_to_total_securities() -> None:
    """Main's 28-digit sum of the one holding missed the printed Total
    Securities and refused."""
    result = quantities(_one_share(_HOLDING_PRICE))

    assert (result.blockers, len(result.anchors)) == ((), 1)


def test_a_holding_that_fits_28_digits_reconciles_as_before() -> None:
    result = quantities(_one_share("$123,456,789.1234567890123456789"))

    assert (result.blockers, len(result.anchors)) == ((), 1)


_ROUND_LOT: Final = "100000000000"
_SPENT: Final = "$100,000,000,000.00"
_SPENT_CASH: Final = "100000000000.00"


def test_example_d_a_sale_of_every_share_bought_leaves_no_lot() -> None:
    """Main negated the sale with unary minus, which rounded it to 28 digits
    and left the lots 1E-20 shares the statement doesn't hold."""
    paired = _held(
        (
            ("Buy", "1/13/2025", _LONG_QUANTITY, _SPENT),
            ("Sell", "1/20/2025", _LONG_QUANTITY, _SPENT),
        ),
        deposit=_SPENT,
        closing_cash=_SPENT_CASH,
    )

    result = quantities(paired)
    lots = reconcile_robinhood_security_lots(result)

    assert [
        movement.signed_quantity
        for movement in result.movements
        if movement.kind is RobinhoodQuantityMovementKind.DISPOSAL
    ] == [Decimal(f"-{_LONG_QUANTITY}")]
    assert (result.blockers, lots.blockers, lots.open_fragments) == ((), (), ())


@pytest.mark.parametrize(
    "positions",
    [
        pytest.param((Position(_LONG_QUANTITY, _SPENT),), id="one-row"),
        pytest.param(
            (
                Position(_ROUND_LOT, _SPENT),
                Position("0.00000000000000000001", "$0.00", loaned=True),
            ),
            id="held-and-loaned-rows",
        ),
    ],
)
def test_a_held_32_digit_quantity_anchors_exactly(positions: tuple[Position, ...]) -> None:
    """The replay's ledger and the statement's per-CUSIP quantity both keep
    every digit, so the anchor and the lots agree with the statement. Main
    rounded both to 100000000000 and the lots, which hold the exact
    acquisition, reported a quantity shortfall."""
    loans = tuple(
        Loan(position.quantity, position.value) for position in positions if position.loaned
    )
    paired = _held(
        (("Buy", "1/13/2025", _LONG_QUANTITY, _SPENT),),
        deposit=_SPENT,
        closing_cash="0",
        positions=positions,
        loans=loans,
    )

    result = quantities(paired)
    lots = reconcile_robinhood_security_lots(result)

    (anchor,) = result.anchors
    assert (anchor.ledger_quantity, anchor.statement_quantity) == (
        Decimal(_LONG_QUANTITY),
        Decimal(_LONG_QUANTITY),
    )
    assert (result.blockers, lots.blockers) == ((), ())
    assert [fragment.quantity for fragment in lots.open_fragments] == [Decimal(_LONG_QUANTITY)]


def test_a_statement_two_units_off_a_32_digit_holding_gets_the_anchor_mismatch_blocker() -> None:
    """The movement's tolerance is its own half unit, 5E-21. Main took ``abs``
    of the 32-digit quantity first, which rounded it to 28 digits and widened
    the tolerance to 5E-17, so the anchor passed."""
    paired = _held(
        (("Buy", "1/13/2025", _LONG_QUANTITY, _SPENT),),
        deposit=_SPENT,
        closing_cash="0",
        positions=(Position(_TWO_UNITS_ABOVE, _SPENT),),
    )

    result = quantities(paired)

    assert [blocker.kind for blocker in result.blockers] == [
        RobinhoodInventoryBlockerKind.ANCHOR_MISMATCH
    ]
    assert result.anchors == ()


def test_a_statement_within_a_32_digit_holdings_half_unit_anchors_with_that_tolerance() -> None:
    paired = _held(
        (("Buy", "1/13/2025", _LONG_QUANTITY, _SPENT),),
        deposit=_SPENT,
        closing_cash="0",
        positions=(Position(_LONG_QUANTITY, _SPENT),),
    )

    (anchor,) = quantities(paired).anchors

    assert anchor.tolerance == Decimal("5E-21")


# Two loaned rows, of 29 digits and of 17 places, whose exact sum
# 100000000000.00000000000000008 is the shares lent; main rounded the first
# down before adding the second.
_LOANED_ROWS: Final = ("100000000000.00000000000000004", "0.00000000000000004")
_LENT: Final = "100000000000.00000000000000008"


@pytest.mark.parametrize(
    ("lent", "reconciles"),
    [
        pytest.param(_LENT, True, id="exact-sum"),
        pytest.param("100000000000.00000000000000009", False, id="a-unit-more"),
    ],
)
def test_the_loaned_partition_adds_its_rows_with_every_digit(lent: str, reconciles: bool) -> None:
    paired = _held(
        (("Buy", "1/13/2025", _LENT, _SPENT),),
        deposit=_SPENT,
        closing_cash="0",
        positions=(
            Position(_LOANED_ROWS[0], _SPENT, loaned=True),
            Position(_LOANED_ROWS[1], "$0.00", loaned=True),
        ),
        loans=(Loan(lent, _SPENT),),
    )

    if reconciles:
        assert quantities(paired).blockers == ()
    else:
        _refuses("loaned security quantities or values disagree", lambda: quantities(paired))


def _pending_buy(
    *, shown: str, pending: str = _LONG_QUANTITY, held: str | None = None
) -> RobinhoodAccountsReconciliation:
    """The individual account buys ``pending`` shares for $100,000,000,000.00
    on Jan 31, settling on Feb 3, after a settled buy of ``held`` shares for
    $1.00, and the statement shows ``shown`` shares."""
    trade = Row(
        "Buy",
        "1/31/2025",
        quantity=pending,
        price="$1.00",
        debit=_SPENT,
        description="Synthetic Equity",
        lines=(_CUSIP_LINE,),
        settles="2/3/2025",
    )
    settled = () if held is None else (("Buy", "1/13/2025", held, "$1.00"),)
    text = statement(
        closing_cash=_SPENT_CASH,
        positions=(Position(shown, usd(Decimal(shown).quantize(Decimal("0.01")))),)
        if shown != "0"
        else (),
        rows=(_deposit_row("$100,000,000,001.00"), *(_trade_row(*trade) for trade in settled)),
        pending=(trade,),
    )
    csv_buy = (
        "1/31/2025",
        "1/31/2025",
        "2/3/2025",
        SYMBOL,
        f"Synthetic Equity\n{_CUSIP_LINE}",
        "Buy",
        pending,
        "$1.00",
        f"({_SPENT})",
    )
    rows = (
        _deposit_csv("$100,000,000,001.00"),
        *(_trade_csv(*trade) for trade in settled),
        csv_buy,
    )
    return pair((text, rows), MANAGED_ACCOUNT)


def test_a_pending_32_digit_buy_anchors_the_settled_statement_exactly() -> None:
    """Main's pending delta and trade-date quantity rounded to 28 digits, so the
    lots' exact acquisition disagreed with the anchored quantity."""
    result = quantities(_pending_buy(shown="0"))
    lots = reconcile_robinhood_security_lots(result)

    (anchor,) = result.anchors
    assert (anchor.trade_date_quantity, anchor.settlement_date_quantity) == (
        Decimal(_LONG_QUANTITY),
        Decimal(),
    )
    assert (result.blockers, lots.blockers) == ((), ())


def test_a_trade_date_statement_anchors_the_32_digit_settled_quantity_exactly() -> None:
    """The statement shows the settled 32-digit holding plus the share bought
    on Jan 31; the settled quantity is the statement's less the pending one,
    which main's 28-digit subtraction rounded."""
    result = quantities(
        _pending_buy(shown="100000000001.00000000000000000001", pending="1", held=_LONG_QUANTITY)
    )

    (anchor,) = result.anchors
    assert (anchor.trade_date_quantity, anchor.settlement_date_quantity) == (
        Decimal("100000000001.00000000000000000001"),
        Decimal(_LONG_QUANTITY),
    )
    assert result.blockers == ()


# ---- transfers between the two accounts ----


def _internal_transfer_row(quantity: str) -> Row:
    return Row(
        "ITRF",
        "1/12/2025",
        quantity=quantity,
        symbol=SYMBOL,
        description="Synthetic Equity",
        lines=(_CUSIP_LINE, f"Internal Security Transfer from {INDIVIDUAL} to {MANAGED}"),
    )


def _managed_sale_csv(quantity: str) -> tuple[str, ...]:
    return (
        "1/13/2025",
        "1/13/2025",
        "1/14/2025",
        SYMBOL,
        f"Synthetic Equity\n{_CUSIP_LINE}\nManaged Trade",
        "Sell",
        quantity,
        "$1.00",
        _SPENT,
    )


def _transfer(
    *,
    bought: str,
    sent: tuple[str, str],
    received: tuple[str, str],
    later_buy: str | None = None,
    sold: str | None = None,
) -> RobinhoodAccountsReconciliation:
    """The individual account buys ``bought`` shares and moves them to the
    managed account, whose report and statement show ``received`` = (CSV,
    statement) quantities against the individual's ``sent``. The individual
    then buys ``later_buy`` shares for $1.00 and holds them, and the managed
    account sells ``sold`` shares or holds what it received."""
    trades = [("Buy", "1/6/2025", bought, _SPENT)]
    if later_buy is not None:
        trades.append(("Buy", "1/12/2025", later_buy, "$1.00"))
    individual = statement(
        closing_cash="0",
        positions=(Position(later_buy, "$1.00"),) if later_buy is not None else (),
        rows=(
            _deposit_row("$100,000,000,001.00"),
            *(_trade_row(*trade) for trade in trades),
            _internal_transfer_row(sent[1]),
        ),
    )
    individual_rows = (
        _deposit_csv("$100,000,000,001.00"),
        *(_trade_csv(*trade) for trade in trades),
        _transfer_csv(sent[0]),
    )
    managed_rows: list[Row] = [_deposit_row("$10.00"), _internal_transfer_row(received[1])]
    managed_csv = [_deposit_csv("$10.00"), _transfer_csv(received[0])]
    if sold is not None:
        managed_rows.append(
            Row(
                "Sell",
                "1/13/2025",
                quantity=sold,
                price="$1.00",
                credit=_SPENT,
                symbol=SYMBOL,
                description="Synthetic Equity",
                lines=(_CUSIP_LINE,),
            )
        )
        managed_csv.append(_managed_sale_csv(sold))
    managed = statement(
        account=MANAGED,
        managed=True,
        closing_cash=exactly("10.00", _SPENT_CASH if sold is not None else "0"),
        positions=() if sold is not None else (Position(received[1], _SPENT),),
        rows=tuple(managed_rows),
    )
    return pair((individual, individual_rows), (managed, managed_csv))


def test_security_transfer_statement_quantities_two_units_apart_past_28_digits_refuse() -> None:
    """Each account's report matches its own statement, but the two statements
    print the transfer two units apart. Main rounded both sums to 100000000000
    and passed them."""
    _refuses(
        "security transfer aggregate quantities disagree",
        lambda: _transfer(
            bought=_LONG_QUANTITY,
            sent=(_LONG_QUANTITY, _LONG_QUANTITY),
            received=(_TWO_UNITS_ABOVE, _TWO_UNITS_ABOVE),
        ),
    )


def test_security_transfer_statement_quantities_that_agree_past_28_digits_reconcile() -> None:
    result = quantities(
        _transfer(
            bought=_LONG_QUANTITY,
            sent=(_LONG_QUANTITY, _LONG_QUANTITY),
            received=(_LONG_QUANTITY, _LONG_QUANTITY),
        )
    )

    assert (result.blockers, len(result.transfers)) == ((), 1)


# A 28-digit statement display that both reports' 32-digit quantities round to.
_SHOWN_28: Final = "100000000000.0000000000000000"


def test_reciprocal_report_quantities_two_units_apart_past_28_digits_refuse() -> None:
    """Both statements print the transfer as 100000000000.0000000000000000,
    which each report's own quantity is within half a unit of, but the two
    reports are two units apart in their 20th place. Main's 28-digit sums of
    the reports' quantities were equal."""
    paired = _transfer(
        bought=_LONG_QUANTITY,
        sent=(_LONG_QUANTITY, _SHOWN_28),
        received=(_TWO_UNITS_ABOVE, _SHOWN_28),
    )

    _refuses("reciprocal in-kind report quantities disagree", lambda: quantities(paired))


def test_reciprocal_report_quantities_that_agree_past_28_digits_reconcile() -> None:
    paired = _transfer(
        bought=_LONG_QUANTITY,
        sent=(_LONG_QUANTITY, _SHOWN_28),
        received=(_LONG_QUANTITY, _SHOWN_28),
    )

    result = quantities(paired)

    assert [
        movement.signed_quantity
        for movement in result.movements
        if movement.transfer_index is not None
    ] == [Decimal(f"-{_LONG_QUANTITY}"), Decimal(_LONG_QUANTITY)]


# A 30-digit display and the exact 32-digit quantity it rounds from (#1308):
# quantizing the exact quantity to the display's 18 places needs 30 digits.
_DISPLAY_30: Final = "100000000000.000000000000000000"
_EXACT_32: Final = "100000000000.00000000000000000030"


def _shown_transfer() -> RobinhoodAccountsReconciliation:
    return _transfer(
        bought=_EXACT_32,
        sent=(_DISPLAY_30, _DISPLAY_30),
        received=(_DISPLAY_30, _DISPLAY_30),
    )


def test_a_resolved_transfer_quantity_that_rounds_to_30_digit_displays_is_used() -> None:
    """Main's 28-digit quantize of the exact quantity to the displays'
    places raised, and the quantity was refused as contradicting them."""
    result = quantities(_shown_transfer(), transfer_quantities={0: Decimal(_EXACT_32)})

    (transfer,) = result.transfers
    assert transfer.resolved_quantity == Decimal(_EXACT_32)


@pytest.mark.parametrize(
    "exact",
    [
        pytest.param("100000000000.00000000000000000060", id="rounds-up"),
        pytest.param("100000000000.00000000000000000050", id="half-way"),
    ],
)
def test_a_resolved_transfer_quantity_that_misses_a_30_digit_display_still_refuses(
    exact: str,
) -> None:
    _refuses(
        TRANSFER_CONTRADICTION,
        lambda: quantities(_shown_transfer(), transfer_quantities={0: Decimal(exact)}),
    )


def _undershown_transfer() -> RobinhoodAccountsReconciliation:
    """The individual account buys the exact 32-digit quantity and sends it;
    every report and statement shows the transfer's 30-digit display, so the
    managed account then sells 3E-19 shares more than it received. The
    individual's later buy fixes its statement precision at 20 places."""
    return _transfer(
        bought=_EXACT_32,
        sent=(_DISPLAY_30, _DISPLAY_30),
        received=(_DISPLAY_30, _DISPLAY_30),
        later_buy="1.00000000000000000000",
        sold=_EXACT_32,
    )


def test_an_undershown_32_digit_transfer_is_corrected_to_the_exact_quantity() -> None:
    """The source anchor's 3E-19 drift and the destination's 3E-19 residual
    prove the exact quantity moved. Main negated the 32-digit sale with unary
    minus, which rounded it to 28 digits, so no residual and no correction
    followed, and the lots blocked the drift."""
    result = quantities(_undershown_transfer(), resolutions=())
    lots = reconcile_robinhood_security_lots(result)

    (transfer,) = result.transfers
    assert transfer.resolved_quantity == Decimal(_EXACT_32)
    assert (result.residuals, result.blockers, lots.blockers) == ((), (), ())


def test_a_sale_a_unit_past_the_exact_transfer_leaves_it_uncorrected() -> None:
    """The managed account's residual is then a unit more than the source's
    drift, so nothing proves the exact quantity: the transfer keeps its
    display and the lots block the drift, as before."""
    paired = _transfer(
        bought=_EXACT_32,
        sent=(_DISPLAY_30, _DISPLAY_30),
        received=(_DISPLAY_30, _DISPLAY_30),
        later_buy="1.00000000000000000000",
        sold="100000000000.00000000000000000031",
    )

    result = quantities(paired, resolutions=())

    (transfer,) = result.transfers
    assert transfer.resolved_quantity is None
    assert reconcile_robinhood_security_lots(result).blockers != ()


# ---- REC resolutions ----


def _receipt(exact: str) -> RobinhoodSecurityQuantityResult:
    """The individual account receives a REC row displaying the 30-digit
    quantity and holds ``exact`` shares, resolved as ``exact``."""
    receipt = Row(
        "REC",
        "1/13/2025",
        quantity=_DISPLAY_30,
        symbol=SYMBOL,
        description="Synthetic Equity",
        lines=(_CUSIP_LINE,),
    )
    text = statement(
        closing_cash="10.00",
        positions=(Position(exact, _SPENT),),
        rows=(_deposit_row("$10.00"), receipt),
    )
    rows = (
        _deposit_csv("$10.00"),
        (
            "1/13/2025",
            "1/13/2025",
            "1/13/2025",
            SYMBOL,
            f"Synthetic Equity\n{_CUSIP_LINE}",
            "REC",
            _DISPLAY_30,
            "",
            "",
        ),
    )
    paired = pair((text, rows), MANAGED_ACCOUNT)
    (event,) = (
        event
        for event in paired.accounts[0].evidence.events
        if event.kind is RobinhoodEventKind.SECURITY_RECEIPT
    )
    resolution = RobinhoodSecurityReceiptResolution(
        account_number=INDIVIDUAL,
        source_record_index=event.source_record_index,
        cusip=CUSIP,
        treatment=RobinhoodSecurityReceiptTreatment.PROMOTIONAL_REWARD,
        exact_quantity=Decimal(exact),
        reward_fair_market_value_usd=Decimal("5"),
        income_reported_financial_year=2024,
        income_reported=True,
        source="Synthetic reward confirmation",
        reason="Synthetic promotional reward",
    )
    return quantities(paired, resolutions=(resolution,))


def test_a_rec_resolution_that_rounds_to_its_30_digit_display_is_accepted() -> None:
    """Main's 28-digit quantize to the display's 18 places raised, so it
    refused the resolution as contradicting the REC row."""
    result = _receipt(_EXACT_32)

    (acquisition,) = result.acquisitions
    assert (acquisition.quantity, acquisition.cost_usd) == (Decimal(_EXACT_32), Decimal("5"))
    assert result.blockers == ()


def test_a_rec_resolution_a_unit_past_its_30_digit_display_still_refuses() -> None:
    _refuses(
        REC_CONTRADICTION,
        lambda: _receipt("100000000000.00000000000000000060"),
    )


# ---- precision refusals ----

# A digit run in a long statement cell; no refusal may print it.
_MARKER: Final = "736251"


def test_an_activity_net_past_the_exponent_limit_refuses_without_echo() -> None:
    """Unreachable at the default context: in-limit cells give sums of about
    35 digits. Under an exponent limit of 13, the two $90 trillion deposits'
    sum overflows, and the refusal names only the fixed label."""
    deposit = "$90,000,000,000,000.00"
    text = statement(
        closing_cash=exactly("180000000000000.00", f"0.000000000{_MARKER}"),
        rows=(
            _deposit_row(deposit),
            Row("ACH", "1/11/2025", credit=deposit, description="ACH Deposit"),
            _coin(f"$0.000000000{_MARKER}"),
        ),
    )
    assert _MARKER in text
    rows = (
        _deposit_csv(deposit),
        ("1/11/2025", "1/11/2025", "1/11/2025", "", "ACH Deposit", "ACH", "", "", deposit),
    )

    events = parse_robinhood_activity(activity_csv(rows))
    (bundle,) = validate_robinhood_statement_bundles((parse(text),))

    with localcontext(Context(Emax=13)):
        error = _refuses(
            "Account Activity net exceeds the supported precision",
            lambda: reconcile_robinhood_cash(
                reconcile_robinhood_activity(bundle, (), events), events
            ),
        )

    for shown in (str(error), repr(error), *(str(arg) for arg in error.args)):
        assert _MARKER not in shown


class _UnaddableDecimal(Decimal):
    def __radd__(self, other: object) -> Decimal:
        raise MemoryError("synthetic allocation failure")


@pytest.mark.parametrize(
    ("operation", "operands"),
    [
        pytest.param(operator.add, (Decimal("9E+999999"), Decimal("9E+999999")), id="overflow"),
        pytest.param(operator.add, (Decimal(1), _UnaddableDecimal(1)), id="out-of-memory"),
    ],
)
def test_arithmetic_that_cannot_be_exact_refuses_with_its_fixed_label(
    operation: Callable[..., Decimal], operands: tuple[Decimal, ...]
) -> None:
    _refuses(
        "Example check exceeds the supported precision",
        lambda: activity_mod.robinhood_exact(operation, *operands, label="Example check"),
    )


def test_exact_sum_iteration_failure_refuses_with_its_fixed_label() -> None:
    def values() -> Iterator[Decimal]:
        yield Decimal(1)
        raise MemoryError("synthetic allocation failure")

    _refuses(
        "Example check exceeds the supported precision",
        lambda: activity_mod.robinhood_exact_sum(values(), label="Example check"),
    )
