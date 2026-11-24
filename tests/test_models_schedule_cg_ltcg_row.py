"""Unit tests for ScheduleCgLtcgLine model invariants."""

from __future__ import annotations

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

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.schedule_cg_ltcg_row import (
    NEW_REGIME_CUTOVER_DATE,
    ScheduleCgLtcgLine,
)
from ibkr_fa.types import Isin, Ticker
from tests._amount_checks import NOT_A_FINITE_DECIMAL
from tests._crash_reports import render_without_frames


def make_line(**overrides: object) -> ScheduleCgLtcgLine:
    """Factory with sensible LTCG defaults; overrides supplied via kwargs.

    The default lot is held just over 24 calendar months (acquired
    2023-04-01, disposed 2025-05-01) and disposed comfortably after
    the Finance (No. 2) Act, 2024 cutover date (2024-07-23) so the
    base construction succeeds.
    """
    defaults: dict[str, object] = {
        "symbol": Ticker("XQAQ"),
        "isin": Isin("US99992A1097"),
        "issuer_country_iso2": "US",
        "description": "SAMPLE ISSUER AQ INC",
        "open_date": date(2023, 4, 1),
        "close_date": date(2025, 5, 1),
        "holding_period_days": (date(2025, 5, 1) - date(2023, 4, 1)).days,
        "quantity": Decimal("10"),
        "cost_basis_usd": Decimal("1500"),
        "proceeds_usd": Decimal("2200"),
        "fx_specified_date": date(2025, 4, 30),
        "fx_rate_inr_per_usd": Decimal("86.20"),
        "cost_basis_inr": Decimal("1500") * Decimal("86.20"),
        "cost_basis_inr_source": "test basis",
        "proceeds_inr": Decimal("2200") * Decimal("86.20"),
        "realized_gain_inr": (Decimal("2200") - Decimal("1500")) * Decimal("86.20"),
    }
    defaults.update(overrides)
    defaults.setdefault("fx_rate_publication_date", defaults["fx_specified_date"])
    return ScheduleCgLtcgLine(**defaults)  # ty: ignore[invalid-argument-type]


def test_constructs_with_valid_long_term_fields() -> None:
    line = make_line()
    assert line.symbol == Ticker("XQAQ")
    assert line.realized_gain_inr > 0
    assert line.holding_period_days > 720  # well past the 24-month threshold


def test_rejects_empty_cost_basis_source() -> None:
    with pytest.raises(InvariantError):
        make_line(cost_basis_inr_source=" ")


def test_rejects_short_term_holding_period() -> None:
    """A lot held <= 24 calendar months must NOT be constructable as LTCG —
    it belongs in the STCG pipeline."""
    with pytest.raises(InvariantError) as exc:
        make_line(
            open_date=date(2025, 1, 15),
            close_date=date(2025, 6, 20),
            holding_period_days=(date(2025, 6, 20) - date(2025, 1, 15)).days,
            fx_specified_date=date(2025, 5, 31),
        )
    assert "short-term" in exc.value.what.lower() or "stcg" in exc.value.what.lower()


def test_rejects_exactly_24_calendar_months_as_stcg_not_ltcg() -> None:
    """Boundary: a lot held EXACTLY 24 calendar months is STCG; LTCG
    requires STRICTLY more."""
    with pytest.raises(InvariantError):
        make_line(
            open_date=date(2024, 4, 1),
            close_date=date(2026, 4, 1),
            holding_period_days=(date(2026, 4, 1) - date(2024, 4, 1)).days,
            fx_specified_date=date(2026, 3, 31),
        )


def test_accepts_one_day_past_24_calendar_months_as_ltcg() -> None:
    """Boundary: one day past 24 calendar months is LTCG."""
    line = make_line(
        open_date=date(2024, 4, 1),
        close_date=date(2026, 4, 2),
        holding_period_days=(date(2026, 4, 2) - date(2024, 4, 1)).days,
        fx_specified_date=date(2026, 3, 31),
    )
    assert line.holding_period_days == 731


def test_rejects_pre_cutover_disposal() -> None:
    """v1 scope: disposals strictly before 2024-07-23 require indexation
    (CBDT Cost Inflation Index) and are out of v1 scope."""
    with pytest.raises(InvariantError) as exc:
        make_line(
            open_date=date(2020, 1, 1),
            close_date=date(2024, 7, 22),  # one day before cutover
            holding_period_days=(date(2024, 7, 22) - date(2020, 1, 1)).days,
            fx_specified_date=date(2024, 6, 30),
        )
    assert "2024-07-23" in exc.value.what or "cut-in" in exc.value.what.lower()


def test_accepts_disposal_on_cutover_date() -> None:
    """Boundary: a disposal on 2024-07-23 itself is the FIRST day of the
    new regime (12.5% without indexation) and is in scope."""
    line = make_line(
        open_date=date(2020, 1, 1),
        close_date=date(2024, 7, 23),
        holding_period_days=(date(2024, 7, 23) - date(2020, 1, 1)).days,
        fx_specified_date=date(2024, 6, 30),
    )
    assert line.close_date == NEW_REGIME_CUTOVER_DATE


def test_rejects_close_before_open() -> None:
    with pytest.raises(InvariantError):
        make_line(
            open_date=date(2025, 5, 1),
            close_date=date(2023, 4, 1),
            holding_period_days=-761,
        )


def test_rejects_holding_period_inconsistent_with_dates() -> None:
    with pytest.raises(InvariantError):
        make_line(holding_period_days=999)


def test_rejects_negative_cost_basis_usd() -> None:
    with pytest.raises(InvariantError):
        make_line(cost_basis_usd=Decimal("-1"))


def test_accepts_negative_proceeds_usd() -> None:
    """LTCG losses are valid (Section 74 carry-forward for long-term)."""
    line = make_line(
        proceeds_usd=Decimal("-50"),
        proceeds_inr=Decimal("-50") * Decimal("86.20"),
        realized_gain_inr=(Decimal("-50") - Decimal("1500")) * Decimal("86.20"),
    )
    assert line.proceeds_usd < 0
    assert line.realized_gain_inr < 0


@pytest.mark.parametrize("rate", ["0", "-86.20"])
def test_rejects_non_positive_fx_rate(rate: str) -> None:
    with pytest.raises(InvariantError) as exc:
        make_line(fx_rate_inr_per_usd=Decimal(rate))
    assert str(exc.value) == (
        "InvariantError: ScheduleCgLtcgLine.fx_rate_inr_per_usd must be positive "
        "(context keys: fx_rate_inr_per_usd, symbol, isin)"
    )


def test_rejects_specified_date_not_before_close_date() -> None:
    with pytest.raises(InvariantError):
        make_line(fx_specified_date=date(2025, 5, 1))


def test_accepts_carried_back_publication_date() -> None:
    line = make_line(fx_rate_publication_date=date(2025, 4, 29))
    assert line.fx_rate_publication_date == date(2025, 4, 29)


def test_rejects_publication_date_after_specified_date() -> None:
    with pytest.raises(InvariantError, match="fx_rate_publication_date must be on or before"):
        make_line(fx_rate_publication_date=date(2025, 5, 1))


@pytest.mark.parametrize("value", [None, "2025-04-30", datetime(2025, 4, 30)])
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
        "ScheduleCgLtcgLine.issuer_country_iso2 must be a 2-letter "
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
        "ScheduleCgLtcgLine.isin must match ISO 6166 (12 chars: 2 "
        "uppercase letters + 9 alphanumerics + 1 digit)"
    )
    assert exc.value.context == (("isin", None), ("symbol", None))


def test_rejects_inconsistent_realized_gain_inr() -> None:
    with pytest.raises(InvariantError):
        make_line(realized_gain_inr=Decimal("0"))


_PROCEEDS_RULE = (
    "ScheduleCgLtcgLine.proceeds_inr must equal "
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
        build(
            proceeds_usd=Decimal("1800"),
            proceeds_inr=Decimal("18000"),
            realized_gain_inr=Decimal("-111300"),
        )
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


def test_accepts_zero_cost_basis_zero_proceeds() -> None:
    """Degenerate edge case (a worthless-security write-off)."""
    line = make_line(
        cost_basis_usd=Decimal("0"),
        proceeds_usd=Decimal("0"),
        cost_basis_inr=Decimal("0"),
        proceeds_inr=Decimal("0"),
        realized_gain_inr=Decimal("0"),
    )
    assert line.realized_gain_inr == 0


def test_new_regime_cutover_date_constant() -> None:
    """Locks the Finance (No. 2) Act, 2024 cutover date."""
    assert date(2024, 7, 23) == NEW_REGIME_CUTOVER_DATE


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
    assert exc.value.what == f"ScheduleCgLtcgLine.{field} must be a finite Decimal"


def test_infinite_proceeds_and_gain_refused() -> None:
    """#936, #937: Infinity * rate and Infinity - cost are both Infinity, so
    the proceeds and gain checks alone pass it."""
    infinity = Decimal("Infinity")
    with pytest.raises(InvariantError) as exc:
        replace(
            make_line(), proceeds_usd=infinity, proceeds_inr=infinity, realized_gain_inr=infinity
        )
    assert exc.value.what == "ScheduleCgLtcgLine.proceeds_usd must be a finite Decimal"


# ---- exact derived fields (#1184) -------------------------------------------

_GAIN_RULE = (
    "ScheduleCgLtcgLine.realized_gain_inr must equal "
    "proceeds_inr - cost_basis_inr exactly (derived field)"
)
_PROCEEDS_MESSAGE = (
    "InvariantError: ScheduleCgLtcgLine.proceeds_inr must equal "
    "proceeds_usd * fx_rate_inr_per_usd exactly (derived field) "
    "(context keys: proceeds_inr, proceeds_usd, fx_rate_inr_per_usd, symbol, isin)"
)
_GAIN_MESSAGE = (
    "InvariantError: ScheduleCgLtcgLine.realized_gain_inr must equal "
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
    fields = _exact_fields("2200", "86.20", "123456.789012345678901234567890123")
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
