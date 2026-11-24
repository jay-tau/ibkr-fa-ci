"""Unit tests for ScheduleCgStcgLine model invariants."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from decimal import (
    Clamped,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    getcontext,
    localcontext,
)
from fractions import Fraction

import pytest

from ibkr_fa.compute.exact_inr import exact_product, is_exact_decimal
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.schedule_cg_ltcg_row import ScheduleCgLtcgLine
from ibkr_fa.models.schedule_cg_row import (
    STCG_HOLDING_THRESHOLD_MONTHS,
    ScheduleCgStcgLine,
    _add_calendar_months,
    is_short_term_holding,
)
from ibkr_fa.types import Isin, Ticker
from tests._amount_checks import NOT_A_FINITE_DECIMAL
from tests._cg_exact_lines import exact_ltcg_line, exact_stcg_line
from tests._crash_reports import render_without_frames


def make_line(**overrides: object) -> ScheduleCgStcgLine:
    """Factory with sensible defaults; overrides supplied via kwargs."""
    defaults: dict[str, object] = {
        "symbol": Ticker("XQAQ"),
        "isin": Isin("US99992A1097"),
        "issuer_country_iso2": "US",
        "description": "SAMPLE ISSUER AQ INC",
        "open_date": date(2025, 1, 15),
        "close_date": date(2025, 6, 20),
        "holding_period_days": (date(2025, 6, 20) - date(2025, 1, 15)).days,
        "quantity": Decimal("10"),
        "cost_basis_usd": Decimal("1500"),
        "proceeds_usd": Decimal("1800"),
        "fx_specified_date": date(2025, 5, 31),
        "fx_rate_inr_per_usd": Decimal("86.20"),
        "cost_basis_inr": Decimal("1500") * Decimal("86.20"),
        "cost_basis_inr_source": "test basis",
        "proceeds_inr": Decimal("1800") * Decimal("86.20"),
        "realized_gain_inr": (Decimal("1800") - Decimal("1500")) * Decimal("86.20"),
    }
    defaults.update(overrides)
    defaults.setdefault("fx_rate_publication_date", defaults["fx_specified_date"])
    return ScheduleCgStcgLine(**defaults)  # ty: ignore[invalid-argument-type]


def test_constructs_with_valid_fields() -> None:
    line = make_line()
    assert line.symbol == Ticker("XQAQ")
    assert line.holding_period_days == 156
    assert line.realized_gain_inr > 0


def test_rejects_non_positive_quantity() -> None:
    with pytest.raises(InvariantError):
        make_line(quantity=Decimal("0"))


def test_rejects_negative_cost_basis_usd() -> None:
    with pytest.raises(InvariantError):
        make_line(cost_basis_usd=Decimal("-1"))


def test_rejects_empty_cost_basis_source() -> None:
    with pytest.raises(InvariantError):
        make_line(cost_basis_inr_source=" ")


def test_accepts_negative_proceeds_usd() -> None:
    """T5 r5 (Codex bot) PR #163 P2 fix: net proceeds may be NEGATIVE
    when IBKR commissions on a tiny / deep-loss sale exceed the gross
    proceeds (the upstream ClosedLot permits a negative
    proceeds_money). The model accepts it;
    the writer escapes via csv_safe_signed.
    """
    line = make_line(
        proceeds_usd=Decimal("-50"),
        # Keep INR amounts consistent with negative proceeds at FX=86.20.
        proceeds_inr=Decimal("-50") * Decimal("86.20"),
        realized_gain_inr=(Decimal("-50") - Decimal("1500")) * Decimal("86.20"),
    )
    assert line.proceeds_usd < 0
    assert line.proceeds_inr < 0


@pytest.mark.parametrize("rate", ["0", "-86.20"])
def test_rejects_non_positive_fx_rate(rate: str) -> None:
    with pytest.raises(InvariantError) as exc:
        make_line(fx_rate_inr_per_usd=Decimal(rate))
    assert str(exc.value) == (
        "InvariantError: ScheduleCgStcgLine.fx_rate_inr_per_usd must be positive "
        "(context keys: fx_rate_inr_per_usd, symbol, isin)"
    )


def test_rejects_close_before_open() -> None:
    with pytest.raises(InvariantError):
        make_line(
            open_date=date(2025, 6, 20),
            close_date=date(2025, 1, 15),
            holding_period_days=-156,
        )


def test_rejects_holding_period_inconsistent_with_dates() -> None:
    with pytest.raises(InvariantError):
        make_line(holding_period_days=999)


def test_rejects_holding_period_above_stcg_threshold() -> None:
    """A lot held > 24 calendar months must not be constructable as STCG."""
    open_d = date(2022, 1, 1)
    close_d = date(2025, 1, 1)
    days = (close_d - open_d).days
    with pytest.raises(InvariantError):
        make_line(
            open_date=open_d,
            close_date=close_d,
            holding_period_days=days,
            fx_specified_date=date(2024, 12, 31),
        )


def test_accepts_holding_period_at_24_calendar_month_boundary() -> None:
    """T3 review: a lot held EXACTLY 24 calendar months is STCG, not LTCG.

    Acquired 2024-04-01, disposed 2026-04-01 → cutoff hits the close
    date exactly. `is_short_term_holding` uses `<=` so this is STCG.
    """
    open_d = date(2024, 4, 1)
    close_d = date(2026, 4, 1)
    line = make_line(
        open_date=open_d,
        close_date=close_d,
        holding_period_days=(close_d - open_d).days,
        fx_specified_date=date(2026, 3, 31),
    )
    assert line.holding_period_days > 720  # exceeds the OLD day-count threshold
    assert is_short_term_holding(open_d, close_d) is True


def test_rejects_one_day_past_24_calendar_months() -> None:
    """A lot disposed one day after the 24-month cutoff is LTCG."""
    open_d = date(2024, 4, 1)
    close_d = date(2026, 4, 2)
    with pytest.raises(InvariantError):
        make_line(
            open_date=open_d,
            close_date=close_d,
            holding_period_days=(close_d - open_d).days,
            fx_specified_date=date(2026, 3, 31),
        )


def test_add_calendar_months_clamps_last_day() -> None:
    """A Jan 31 + 1 calendar month must clamp to Feb 28/29, not roll to March."""
    assert _add_calendar_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert _add_calendar_months(date(2023, 1, 31), 1) == date(2023, 2, 28)
    # Year roll
    assert _add_calendar_months(date(2024, 12, 31), 1) == date(2025, 1, 31)
    # 24 months
    assert _add_calendar_months(date(2024, 4, 1), 24) == date(2026, 4, 1)
    assert STCG_HOLDING_THRESHOLD_MONTHS == 24


def test_rejects_specified_date_not_before_close_date() -> None:
    with pytest.raises(InvariantError):
        make_line(fx_specified_date=date(2025, 6, 20))


def test_accepts_carried_back_publication_date() -> None:
    line = make_line(fx_rate_publication_date=date(2025, 5, 30))
    assert line.fx_rate_publication_date == date(2025, 5, 30)


def test_rejects_publication_date_after_specified_date() -> None:
    with pytest.raises(InvariantError, match="fx_rate_publication_date must be on or before"):
        make_line(fx_rate_publication_date=date(2025, 6, 1))


@pytest.mark.parametrize("value", [None, "2025-05-31", datetime(2025, 5, 31)])
def test_rejects_publication_date_that_is_not_a_date(value: object) -> None:
    with pytest.raises(InvariantError, match="fx_rate_publication_date must be a date"):
        make_line(fx_rate_publication_date=value)


def test_rejects_indian_issuer_country() -> None:
    with pytest.raises(InvariantError):
        make_line(issuer_country_iso2="IN")


def test_rejects_lowercase_country_code() -> None:
    with pytest.raises(InvariantError):
        make_line(issuer_country_iso2="us")


def test_rejects_three_letter_country_code() -> None:
    with pytest.raises(InvariantError):
        make_line(issuer_country_iso2="USA")


def test_rejects_non_ascii_capital_in_country_code() -> None:
    """#854: ``isupper`` and ``isalpha`` accept ``"Ü"``; ISO 3166-1 is ASCII."""
    with pytest.raises(InvariantError) as exc:
        make_line(issuer_country_iso2="ÜS")
    assert exc.value.what == (
        "ScheduleCgStcgLine.issuer_country_iso2 must be a 2-letter "
        "uppercase ISO 3166-1 alpha-2 code"
    )
    assert exc.value.context == (
        ("issuer_country_iso2", None),
        ("symbol", None),
        ("isin", None),
    )


# ``$`` also matches just before a final line break (#835).
@pytest.mark.parametrize("isin", ["BADISIN", "US99992A1097\n"])
def test_rejects_malformed_isin(isin: str) -> None:
    with pytest.raises(InvariantError) as exc:
        make_line(isin=Isin(isin))
    assert exc.value.what == (
        "ScheduleCgStcgLine.isin must match ISO 6166 (12 chars: 2 "
        "uppercase letters + 9 alphanumerics + 1 digit)"
    )
    assert exc.value.context == (("isin", None), ("symbol", None))


def test_rejects_inconsistent_realized_gain_inr() -> None:
    with pytest.raises(InvariantError):
        make_line(realized_gain_inr=Decimal("0"))


_PROCEEDS_RULE = (
    "ScheduleCgStcgLine.proceeds_inr must equal "
    "proceeds_usd * fx_rate_inr_per_usd exactly (derived field)"
)


@pytest.mark.parametrize(
    "build",
    [make_line, lambda **fields: replace(make_line(), **fields)],
    ids=["direct", "replace"],
)
def test_rejects_proceeds_inr_off_the_line_rate(build: Callable[..., object]) -> None:
    """#937: $1,800 at ₹86.20/$ is ₹155,160. ₹18,000 with the matching
    ₹111,300 loss passes the gain check, so only the proceeds check refuses it."""
    with pytest.raises(InvariantError) as exc:
        build(proceeds_inr=Decimal("18000"), realized_gain_inr=Decimal("-111300"))
    assert exc.value.what == _PROCEEDS_RULE
    assert exc.value.context == (
        ("proceeds_inr", None),
        ("proceeds_usd", None),
        ("fx_rate_inr_per_usd", None),
        ("symbol", None),
        ("isin", None),
    )


def test_overflowing_proceeds_product_refused() -> None:
    """#937: a product of finite amounts can overflow; that refuses too."""
    with pytest.raises(InvariantError) as exc:
        make_line(proceeds_usd=Decimal("1e999999"))
    assert exc.value.what == _PROCEEDS_RULE
    assert exc.value.__context__ is None


def test_exact_long_proceeds_pass_at_any_caller_precision() -> None:
    """#1184 replaces #937's caller-context rule: the exact 32-digit product
    passes whether the line is built at 60 digits or at the default 28, and
    the default context's rounded product refuses."""
    proceeds_usd = Decimal("1234.5678901234567890123456789")
    with localcontext(Context(prec=60)):
        proceeds_inr = proceeds_usd * Decimal("86.20")
        line = make_line(
            proceeds_usd=proceeds_usd,
            proceeds_inr=proceeds_inr,
            realized_gain_inr=proceeds_inr - Decimal("1500") * Decimal("86.20"),
        )
    assert len(proceeds_inr.as_tuple().digits) > 28
    assert replace(line) == line
    rounded = proceeds_usd * Decimal("86.20")
    assert rounded != proceeds_inr
    with pytest.raises(InvariantError) as exc:
        replace(line, proceeds_inr=rounded)
    assert exc.value.what == _PROCEEDS_RULE


def test_accepts_negative_realized_gain_loss_case() -> None:
    """STCG losses are valid Schedule CG rows (carry-forward 8 AYs).

    With single-conversion at transfer date: cost_basis_inr and
    proceeds_inr use the same FX, and the negative realized gain is
    simply ``(proceeds_usd - cost_basis_usd) * fx_rate``.
    """
    line = make_line(
        proceeds_usd=Decimal("1200"),
        proceeds_inr=Decimal("1200") * Decimal("86.20"),
        realized_gain_inr=(Decimal("1200") - Decimal("1500")) * Decimal("86.20"),
    )
    assert line.realized_gain_inr < 0


def test_accepts_zero_cost_basis_zero_proceeds() -> None:
    """Degenerate edge case (a worthless-security write-off): allowed."""
    line = make_line(
        cost_basis_usd=Decimal("0"),
        proceeds_usd=Decimal("0"),
        cost_basis_inr=Decimal("0"),
        proceeds_inr=Decimal("0"),
        realized_gain_inr=Decimal("0"),
    )
    assert line.realized_gain_inr == 0


@pytest.mark.parametrize(
    "field",
    [
        "quantity",
        "cost_basis_usd",
        "proceeds_usd",
        "fx_rate_inr_per_usd",
        "cost_basis_inr",
        "proceeds_inr",
        "realized_gain_inr",
    ],
)
@pytest.mark.parametrize("value", [*NOT_A_FINITE_DECIMAL, None], ids=repr)
def test_amount_must_be_a_finite_decimal(field: str, value: object) -> None:
    with pytest.raises(InvariantError) as exc:
        replace(make_line(), **{field: value})
    assert exc.value.what == f"ScheduleCgStcgLine.{field} must be a finite Decimal"


def test_infinite_proceeds_and_gain_refused() -> None:
    """#936, #937: Infinity * rate and Infinity - cost are both Infinity, so
    the proceeds and gain checks alone pass it."""
    infinity = Decimal("Infinity")
    with pytest.raises(InvariantError) as exc:
        replace(
            make_line(), proceeds_usd=infinity, proceeds_inr=infinity, realized_gain_inr=infinity
        )
    assert exc.value.what == "ScheduleCgStcgLine.proceeds_usd must be a finite Decimal"


# ---- exact derived fields (#1184) -------------------------------------------

_GAIN_RULE = (
    "ScheduleCgStcgLine.realized_gain_inr must equal "
    "proceeds_inr - cost_basis_inr exactly (derived field)"
)
_PROCEEDS_MESSAGE = (
    "InvariantError: ScheduleCgStcgLine.proceeds_inr must equal "
    "proceeds_usd * fx_rate_inr_per_usd exactly (derived field) "
    "(context keys: proceeds_inr, proceeds_usd, fx_rate_inr_per_usd, symbol, isin)"
)
_GAIN_MESSAGE = (
    "InvariantError: ScheduleCgStcgLine.realized_gain_inr must equal "
    "proceeds_inr - cost_basis_inr exactly (derived field) "
    "(context keys: realized_gain_inr, expected_gain, proceeds_inr, cost_basis_inr, "
    "symbol, isin)"
)
_PROCEEDS_KEYS = ("proceeds_inr", "proceeds_usd", "fx_rate_inr_per_usd", "symbol", "isin")
_GAIN_KEYS = (
    "realized_gain_inr",
    "expected_gain",
    "proceeds_inr",
    "cost_basis_inr",
    "symbol",
    "isin",
)
# The default 28-digit context's value of both long derived fields below.
_DEFAULT_ROUNDED = "1.500000000000000000000000000"


def _exact_fields(proceeds_usd: str, fx_rate: str, cost_basis_inr: str) -> dict[str, Decimal]:
    """A line's amounts with ``proceeds_inr`` and ``realized_gain_inr`` exact.

    Computed at 2000 digits with ``Rounded`` trapped, so the test side never
    rounds; the default 28-digit context would.
    """
    usd, rate, cost = Decimal(proceeds_usd), Decimal(fx_rate), Decimal(cost_basis_inr)
    with localcontext(Context(prec=2000, traps=[Rounded])):
        proceeds_inr = usd * rate
        gain = proceeds_inr - cost
    return {
        "proceeds_usd": usd,
        "fx_rate_inr_per_usd": rate,
        "proceeds_inr": proceeds_inr,
        "cost_basis_inr": cost,
        "realized_gain_inr": gain,
    }


def _digits(count: int) -> str:
    """A positive amount with ``count`` significant digits."""
    return "1." + "7" * (count - 1)


# A 29-digit product at FX 1 and a 30-digit difference; 28 digits round both.
_LONG_PROCEEDS = _exact_fields("1.4999999999999999999999999999", "1", "0")
_LONG_GAIN = _exact_fields("1.5", "1", "1E-29")
# -9E+999999 - 9E+999999 overflows the default Emax of 999999.
_OVERFLOWING_GAIN = {
    "proceeds_usd": Decimal("-9E+999999"),
    "fx_rate_inr_per_usd": Decimal("1"),
    "proceeds_inr": Decimal("-9E+999999"),
    "cost_basis_inr": Decimal("9E+999999"),
    "realized_gain_inr": Decimal("-1.8E+1000000"),
}


@pytest.mark.parametrize(
    ("fields", "field", "default_value", "rule", "other_wrong"),
    [
        pytest.param(
            _LONG_PROCEEDS,
            "proceeds_inr",
            lambda fields: fields["proceeds_usd"] * fields["fx_rate_inr_per_usd"],
            _PROCEEDS_RULE,
            ("1.499999999999999999999999999", "1.4999999999999999999999999998"),
            id="proceeds",
        ),
        pytest.param(
            _LONG_GAIN,
            "realized_gain_inr",
            lambda fields: fields["proceeds_inr"] - fields["cost_basis_inr"],
            _GAIN_RULE,
            ("1.499999999999999999999999999", "1.49999999999999999999999999998"),
            id="gain",
        ),
    ],
)
def test_derived_field_must_be_exact_not_rounded(
    fields: dict[str, Decimal],
    field: str,
    default_value: Callable[[dict[str, Decimal]], Decimal],
    rule: str,
    other_wrong: tuple[str, ...],
) -> None:
    """#1184: the exact long derived field is accepted at the default
    precision; the default context's rounded value of it refuses, and so do a
    truncation and a last-digit slip."""
    line = make_line(**fields)
    assert len(getattr(line, field).as_tuple().digits) > 28
    with localcontext(Context()):
        rounded = default_value(fields)
    assert rounded == Decimal(_DEFAULT_ROUNDED)
    for wrong in (rounded, *map(Decimal, other_wrong)):
        with pytest.raises(InvariantError) as exc:
            replace(line, **{field: wrong})
        assert exc.value.what == rule


@pytest.mark.parametrize(
    ("fields", "field", "digits"),
    [
        pytest.param(_exact_fields(_digits(500), "86.20", "0"), "proceeds_inr", 504, id="product"),
        pytest.param(
            _exact_fields("1E+1000", "1", "1"), "realized_gain_inr", 1000, id="difference"
        ),
    ],
)
def test_derived_fields_within_the_shared_digit_limit_accepted(
    fields: dict[str, Decimal], field: str, digits: int
) -> None:
    """#1184: products and differences up to ``EXACT_SUM_DIGITS`` (1000)
    digits are checked exactly at the default precision."""
    line = make_line(**fields)
    assert len(getattr(line, field).as_tuple().digits) == digits


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        pytest.param(_exact_fields(_digits(999), "86.20", "0"), _PROCEEDS_MESSAGE, id="product"),
        pytest.param(_exact_fields("1E+1001", "1", "0"), _GAIN_MESSAGE, id="difference"),
    ],
)
def test_derived_field_past_the_shared_digit_limit_needs_a_wider_context(
    fields: dict[str, Decimal], message: str
) -> None:
    """#1184: a 1003-digit product or a 1002-digit difference can't be
    checked within 1000 digits and refuses; a caller at 1200 digits accepts
    the same line."""
    with pytest.raises(InvariantError) as exc:
        make_line(**fields)
    assert str(exc.value) == message
    with localcontext(Context(prec=1200)):
        line = make_line(**fields)
    assert line.realized_gain_inr == fields["realized_gain_inr"]


def test_overflowing_gain_difference_refused() -> None:
    """#1184: a difference past the inherited ``Emax`` refuses with the model's
    message instead of raising ``Overflow``; a caller with a wider ``Emax``
    accepts it."""
    with pytest.raises(InvariantError) as exc:
        make_line(**_OVERFLOWING_GAIN)
    assert str(exc.value) == _GAIN_MESSAGE
    assert exc.value.__context__ is None
    with localcontext(Context(Emax=2_000_000)):
        line = make_line(**_OVERFLOWING_GAIN)
    assert line.realized_gain_inr == _OVERFLOWING_GAIN["realized_gain_inr"]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        pytest.param(_exact_fields("1E+10", "86.20", "0"), _PROCEEDS_MESSAGE, id="product"),
        pytest.param(_exact_fields("-6E+10", "1", "6E+10"), _GAIN_MESSAGE, id="difference"),
    ],
)
def test_small_caller_emax_refuses_an_overflowing_derived_field(
    fields: dict[str, Decimal], message: str
) -> None:
    """#1184: the checks inherit the caller's exponent limit; a line valid at
    the default ``Emax`` refuses under ``Emax=10``."""
    line = make_line(**fields)
    with localcontext(Context(Emax=10)), pytest.raises(InvariantError) as exc:
        replace(line)
    assert str(exc.value) == message
    assert exc.value.__context__ is None


_MARKERS: dict[str, object] = {
    "symbol": Ticker("ZQXA"),
    "description": "SYNTHCGDESC",
    "cost_basis_inr_source": "SYNTHCGSOURCE",
}


@pytest.mark.parametrize(
    ("fields", "message", "keys"),
    [
        pytest.param(
            {"proceeds_usd": Decimal("1E+999999")},
            _PROCEEDS_MESSAGE,
            _PROCEEDS_KEYS,
            id="proceeds-overflow",
        ),
        pytest.param(
            _exact_fields(_digits(999), "86.20", "0"),
            _PROCEEDS_MESSAGE,
            _PROCEEDS_KEYS,
            id="proceeds-digit-limit",
        ),
        pytest.param(
            {**_LONG_PROCEEDS, "proceeds_inr": Decimal(_DEFAULT_ROUNDED)},
            _PROCEEDS_MESSAGE,
            _PROCEEDS_KEYS,
            id="proceeds-mismatch",
        ),
        pytest.param(_OVERFLOWING_GAIN, _GAIN_MESSAGE, _GAIN_KEYS, id="gain-overflow"),
        pytest.param(
            _exact_fields("1E+1001", "1", "0"),
            _GAIN_MESSAGE,
            _GAIN_KEYS,
            id="gain-digit-limit",
        ),
        pytest.param(
            {**_LONG_GAIN, "realized_gain_inr": Decimal(_DEFAULT_ROUNDED)},
            _GAIN_MESSAGE,
            _GAIN_KEYS,
            id="gain-mismatch",
        ),
    ],
)
def test_derived_field_refusal_is_the_complete_typed_error(
    fields: dict[str, Decimal],
    message: str,
    keys: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1184: arithmetic failures and mismatches give the same complete
    message and context-key order (each key kept without its value), chain
    no helper exception and render no marker."""
    with pytest.raises(InvariantError) as exc:
        make_line(**_MARKERS, **fields)
    error = exc.value
    assert str(error) == message
    assert error.context == tuple((key, None) for key in keys)
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = render_without_frames(error)
    amounts = [str(value) for value in fields.values()]
    for text in ("ZQXA", "SYNTHCGDESC", "SYNTHCGSOURCE", *amounts):
        assert text not in rendered, text
    assert capsys.readouterr() == ("", "")


def test_statutory_inr_cost_is_not_tied_to_the_rate() -> None:
    """#1184: a long statutory INR cost unrelated to ``cost_basis_usd *
    fx_rate_inr_per_usd`` is accepted with its exact gain; the default
    context's rounded gain refuses."""
    fields = _exact_fields("1800", "86.20", "123456.789012345678901234567890123")
    line = make_line(**fields)
    assert line.cost_basis_inr != line.cost_basis_usd * line.fx_rate_inr_per_usd
    assert len(line.realized_gain_inr.as_tuple().digits) > 28
    with localcontext(Context()):
        rounded_gain = fields["proceeds_inr"] - fields["cost_basis_inr"]
    with pytest.raises(InvariantError) as exc:
        replace(line, realized_gain_inr=rounded_gain)
    assert exc.value.what == _GAIN_RULE


_HOSTILE = Context(prec=3, traps=[DivisionByZero, InvalidOperation, Overflow, Inexact, Rounded])


@pytest.mark.parametrize("caller", [Context(), _HOSTILE], ids=["default", "hostile"])
def test_construction_leaves_the_callers_context_unchanged(caller: Context) -> None:
    """#1184: the checks run in a widened copy of the caller's context, so
    accepted and refused lines leave its precision, traps and flags as they
    were, and a 3-digit caller trapping ``Inexact`` and ``Rounded`` changes no
    outcome. The lines are built before entering that context."""
    short, line = make_line(), make_line(**_LONG_PROCEEDS)
    refusals = [
        ({"proceeds_inr": Decimal(_DEFAULT_ROUNDED)}, _PROCEEDS_RULE),
        ({"proceeds_usd": Decimal(_digits(1001))}, _PROCEEDS_RULE),
        ({"realized_gain_inr": Decimal(_DEFAULT_ROUNDED)}, _GAIN_RULE),
        (_OVERFLOWING_GAIN, _GAIN_RULE),
    ]
    with localcontext(caller):
        getcontext().flags[Clamped] = True
        before = repr(getcontext())
        assert replace(short) == short
        assert replace(line) == line
        for change, rule in refusals:
            with pytest.raises(InvariantError) as exc:
                replace(line, **change)
            assert exc.value.what == rule
            assert exc.value.__context__ is None
        after = repr(getcontext())
    assert after == before


_IMPORT_ORDER_CHILD = """\
import sys
from dataclasses import replace
from datetime import date
from decimal import Decimal

assert not any(name.startswith("ibkr_fa") for name in sys.modules)
import {first}
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.schedule_cg_ltcg_row import ScheduleCgLtcgLine
from ibkr_fa.models.schedule_cg_row import ScheduleCgStcgLine

exact = Decimal("1.4999999999999999999999999999")
for model, opened, closed in (
    (ScheduleCgStcgLine, date(2025, 1, 15), date(2025, 6, 20)),
    (ScheduleCgLtcgLine, date(2023, 4, 1), date(2025, 5, 1)),
):
    line = model(
        symbol="XQAQ", isin="US99992A1097", issuer_country_iso2="US",
        description="SAMPLE ISSUER AQ INC", open_date=opened, close_date=closed,
        holding_period_days=(closed - opened).days, quantity=Decimal(1),
        cost_basis_usd=Decimal(0), proceeds_usd=exact,
        fx_specified_date=date(2025, 3, 31), fx_rate_publication_date=date(2025, 3, 31),
        fx_rate_inr_per_usd=Decimal(1), cost_basis_inr=Decimal(0),
        cost_basis_inr_source="test basis", proceeds_inr=exact, realized_gain_inr=exact,
    )
    print("accepted")
    try:
        replace(line, proceeds_inr=Decimal("{rounded}"))
    except InvariantError as error:
        print(error)
"""


@pytest.mark.parametrize(
    "first",
    [
        "ibkr_fa.models.schedule_cg_row",
        "ibkr_fa.models.schedule_cg_ltcg_row",
        "ibkr_fa.compute.exact_inr",
        "ibkr_fa.models",
    ],
)
def test_models_check_exactly_in_any_import_order(first: str) -> None:
    """#1184: the models import the exact helpers lazily; whichever module a
    fresh interpreter imports first, both models accept the exact long
    proceeds and refuse the rounded ones with their own message."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _IMPORT_ORDER_CHILD.format(first=first, rounded=_DEFAULT_ROUNDED),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    ltcg_message = (
        "InvariantError: ScheduleCgLtcgLine.proceeds_inr must equal "
        "proceeds_usd * fx_rate_inr_per_usd exactly (derived field) "
        "(context keys: proceeds_inr, proceeds_usd, fx_rate_inr_per_usd, symbol, isin)"
    )
    assert result.stdout == f"accepted\n{_PROCEEDS_MESSAGE}\naccepted\n{ltcg_message}\n"


# ---- exact amounts (#1241), both line models ---------------------------------

_MODELS = ["ScheduleCgStcgLine", "ScheduleCgLtcgLine"]
_EXACT_FIELDS = (
    "cost_basis_usd",
    "proceeds_usd",
    "cost_basis_inr",
    "proceeds_inr",
    "realized_gain_inr",
)
_EXACT_RULE = (
    "InvariantError: {model}.exact_amounts must hold the exact amounts its Decimal fields "
    "round, with proceeds_inr = proceeds_usd * fx_rate_inr_per_usd and "
    "realized_gain_inr = proceeds_inr - cost_basis_inr"
)


def _proceeds_repro(model: str) -> ScheduleCgStcgLine | ScheduleCgLtcgLine:
    """#1241's proceeds repro at ₹84.15: 1 share bought on 2022-12-01 for $4.00 and 2 on
    2025-01-10 for $6.00, all 3 sold on 2025-02-15 for $10.00. The LTCG slice gets
    $10/3 of the proceeds and the STCG slice $20/3."""
    if model == "ScheduleCgStcgLine":
        return exact_stcg_line(
            cost_usd=6,
            proceeds_usd=Fraction(20, 3),
            rate="84.15",
            sold=date(2025, 2, 15),
            quantity="2",
        )
    return exact_ltcg_line(
        cost_usd=4, proceeds_usd=Fraction(10, 3), rate="84.15", sold=date(2025, 2, 15)
    )


def _one_unit_further(value: Decimal) -> Decimal:
    """``value`` one unit further from zero at its last digit: past the amount it cuts."""
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    return value + Decimal(1).scaleb(exponent).copy_sign(value)


@pytest.mark.parametrize("model", _MODELS)
def test_line_with_exact_amounts_accepts_proceeds_off_the_decimal_product(model: str) -> None:
    """#1241: proceeds_inr is the exact share times ₹84.15 (₹561 or ₹280.50), not the
    Decimal cut times the rate; the line holds it with its exact amounts, and #1184 refuses
    it without them."""
    line = _proceeds_repro(model)
    assert line.proceeds_inr != exact_product(line.proceeds_usd, line.fx_rate_inr_per_usd)
    with pytest.raises(InvariantError) as exc:
        replace(line, exact_amounts=None)
    assert str(exc.value) == (
        f"InvariantError: {model}.proceeds_inr must equal proceeds_usd * fx_rate_inr_per_usd "
        "exactly (derived field) "
        "(context keys: proceeds_inr, proceeds_usd, fx_rate_inr_per_usd, symbol, isin)"
    )


@pytest.mark.parametrize("model", _MODELS)
@pytest.mark.parametrize("field", _EXACT_FIELDS)
def test_decimal_field_that_is_not_a_cut_of_its_exact_amount_refuses(
    model: str, field: str
) -> None:
    """#1241: each Decimal field must be its exact amount cut toward zero; one unit further
    from zero at its last digit, as a HALF_UP rounding can be, refuses."""
    line = _proceeds_repro(model)
    with pytest.raises(InvariantError) as exc:
        replace(line, **{field: _one_unit_further(getattr(line, field))})
    assert str(exc.value) == _EXACT_RULE.format(model=model)


@pytest.mark.parametrize("model", _MODELS)
@pytest.mark.parametrize(
    "field",
    [
        pytest.param("proceeds_usd", id="proceeds-off-the-rate"),
        pytest.param("cost_basis_inr", id="gain-off-the-difference"),
    ],
)
def test_exact_amounts_must_hold_the_line_arithmetic(model: str, field: str) -> None:
    """#1241: the exact proceeds_inr must be proceeds_usd x rate and the exact gain proceeds
    less cost. Moving one operand 1E-40 from zero, which every Decimal field still cuts to,
    refuses."""
    line = _proceeds_repro(model)
    exact = line.exact_amounts
    assert exact is not None
    moved = replace(exact, **{field: getattr(exact, field) + Fraction(1, 10**40)})
    with pytest.raises(InvariantError) as exc:
        replace(line, exact_amounts=moved)
    assert str(exc.value) == _EXACT_RULE.format(model=model)
    assert all(
        is_exact_decimal(getattr(line, name), getattr(moved, name)) for name in _EXACT_FIELDS
    )


@pytest.mark.parametrize("model", _MODELS)
def test_exact_amounts_of_another_type_refuse(model: str) -> None:
    """#1241: the exact amounts must be a ``ScheduleCgExactAmounts``."""
    line = _proceeds_repro(model)
    exact = line.exact_amounts
    assert exact is not None
    with pytest.raises(InvariantError) as exc:
        replace(line, exact_amounts=tuple(getattr(exact, name) for name in _EXACT_FIELDS))
    assert str(exc.value) == _EXACT_RULE.format(model=model)


@pytest.mark.parametrize("model", _MODELS)
@pytest.mark.parametrize("name", _EXACT_FIELDS)
def test_exact_amount_of_another_type_refuses(model: str, name: str) -> None:
    """#1241: each exact amount must be a ``Fraction``; a Decimal one refuses, not a TypeError."""
    line = _proceeds_repro(model)
    exact = line.exact_amounts
    assert exact is not None
    as_decimal = replace(exact, **{name: Decimal(getattr(line, name))})
    with pytest.raises(InvariantError) as exc:
        replace(line, exact_amounts=as_decimal)
    assert str(exc.value) == _EXACT_RULE.format(model=model)
