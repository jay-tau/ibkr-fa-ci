"""Read-time digit limits on Fidelity CMA statement cells and ``user.toml``
numbers (#1275).

Each new digit-limit refusal input is one digit or place past its kind's limit
and carries the digit run ``9464`` inside the breaching number; no refusal may print it.
Each limit is also pinned from the other side: a value exactly at it is read
(or reaches the check that follows), and a value past an existing, tighter
bound keeps that bound's refusal.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from ibkr_fa.cli import main
from ibkr_fa.config import load_user_config
from ibkr_fa.exceptions import ConfigError, ParseError
from ibkr_fa.output.income_bundle import INCOME_FILING_FILENAMES
from ibkr_fa.parser.fidelity_cma import parse_year_end_income_summary
from tests._config_toml import VALID_TOML, _fidelity_cma, _form67_override, _netbenefits
from tests._crash_reports import assert_unchained_refusal
from tests._fidelity_cma import _ACCOUNT
from tests._fidelity_cma import YEAR_END_TEXT as _YEAR_END
from tests._fidelity_cma import monthly_statement_text as _statement
from tests._fidelity_cma import parse_monthly_text as _parse
from tests._fidelity_cma_bundle import bundle_inputs as _inputs
from tests._number_limits import MONEY, PRICE, QUANTITY, RATE
from tests._robinhood_toml import (
    _OPTION_BUSINESS_FILING,
    robinhood_option_classification_toml,
    robinhood_security_receipt_resolution_toml,
    robinhood_security_toml,
    robinhood_toml,
    write,
)
from tests._text_edits import replace_once as _edit

_MARKER = "9464"
_SECTION = "Fidelity CMA monthly statement"


def _cma_refusal(message: str, column: str | None = None) -> ParseError:
    return ParseError(section=_SECTION, row_index=0, column=column, message=message, raw_value="")


def _cma_text(message: str, column: str | None = None) -> str:
    where = "row 0" if column is None else f"row 0, column {column!r}"
    return f"ParseError in section {_SECTION!r} at {where}: {message}"


def _parse_year_end(text: str) -> object:
    return parse_year_end_income_summary(
        text, expected_currency="USD", expected_account_id=_ACCOUNT
    )


# The monthly statement's SPAXX holding (beginning value, quantity, price,
# ending value), yield and first Core Fund Activity row (quantity, price,
# amount), and the year-end report's SPAXX holding.
_HOLDING = "MONEY $100.00 105.000 $1.0000 $105.00"
_YIELD = "MARKET (SPAXX) 4.000%"
_CORE_ROW = "MARKET 4.250 $1.0000 $4.25"
_YEAR_END_HOLDING = "MONEY 10.000 $1.0000 $10.00"
_POSITION_MISMATCH = "SPAXX quantity and price do not reproduce its market value"
_CORE_MISMATCH = "SPAXX activity amount, price, quantity, or balance is inconsistent"
_YEAR_END_MISMATCH = "year-end SPAXX quantity, price, value, or income is inconsistent"

# (year-end report?, printed text, the same text with one cell one digit past
# its kind's limit, the kind's refusal). Each cell is read before any check
# that compares it, so the limit is the first refusal.
_CMA_BREACHES: dict[str, tuple[bool, str, str, str]] = {
    "spaxx-quantity-13-whole-digits": (
        False,
        _HOLDING,
        "MONEY $100.00 9464000000000.000 $1.0000 $105.00",
        QUANTITY,
    ),
    "spaxx-price-10-whole-digits": (
        False,
        _HOLDING,
        "MONEY $100.00 105.000 $9464000000.0000 $105.00",
        PRICE,
    ),
    "spaxx-yield-4-whole-digits": (False, _YIELD, "MARKET (SPAXX) 9464.000%", RATE),
    "spaxx-beginning-value-16-whole-digits": (
        False,
        _HOLDING,
        "MONEY $9464000000000000.00 105.000 $1.0000 $105.00",
        MONEY,
    ),
    "core-quantity-13-whole-digits": (
        False,
        _CORE_ROW,
        "MARKET 9464000000000.000 $1.0000 $4.25",
        QUANTITY,
    ),
    "core-price-10-whole-digits": (
        False,
        _CORE_ROW,
        "MARKET 4.250 $9464000000.0000 $4.25",
        PRICE,
    ),
    "year-end-quantity-13-whole-digits": (
        True,
        _YEAR_END_HOLDING,
        "MONEY 9464000000000.000 $1.0000 $10.00",
        QUANTITY,
    ),
    "year-end-price-10-whole-digits": (
        True,
        _YEAR_END_HOLDING,
        "MONEY 10.000 $9464000000.0000 $10.00",
        PRICE,
    ),
}


@pytest.mark.parametrize(
    ("year_end", "old", "new", "message"), _CMA_BREACHES.values(), ids=list(_CMA_BREACHES)
)
def test_cma_cell_past_its_limit_refuses_when_read(
    year_end: bool, old: str, new: str, message: str
) -> None:
    """Each SPAXX quantity, price and yield, and a money cell, one digit past
    its kind's limit. Before #1275 each read, then a quantity or price failed
    its value cross-check, the beginning value failed the Total Core Account
    match and the yield parsed."""
    text = _edit(_YEAR_END if year_end else _statement(), old, new)
    assert _MARKER in new
    with pytest.raises(ParseError) as caught:
        if year_end:
            _parse_year_end(text)
        else:
            _parse(text)
    assert_unchained_refusal(
        caught.value, _cma_refusal(message), _cma_text(message), markers=(_MARKER,)
    )


# (year-end report?, printed text, the same text with one cell exactly at its
# kind's limit, the refusal the statement's next check then raises, its column).
_CMA_AT_LIMIT: dict[str, tuple[bool, str, str, str, str | None]] = {
    "spaxx-quantity-12-whole-digits": (
        False,
        _HOLDING,
        "MONEY $100.00 946400000000.000 $1.0000 $105.00",
        _POSITION_MISMATCH,
        None,
    ),
    "spaxx-price-9-whole-digits": (
        False,
        _HOLDING,
        "MONEY $100.00 105.000 $946400000.0000 $105.00",
        _POSITION_MISMATCH,
        None,
    ),
    "spaxx-beginning-value-15-whole-digits": (
        False,
        _HOLDING,
        "MONEY $946400000000000.00 105.000 $1.0000 $105.00",
        "Total Core Account beginning value does not match the core fund holding",
        "Total Core Account",
    ),
    "core-quantity-12-whole-digits": (
        False,
        _CORE_ROW,
        "MARKET 946400000000.000 $1.0000 $4.25",
        _CORE_MISMATCH,
        None,
    ),
    "core-price-9-whole-digits": (
        False,
        _CORE_ROW,
        "MARKET 4.250 $946400000.0000 $4.25",
        _CORE_MISMATCH,
        None,
    ),
    "year-end-quantity-12-whole-digits": (
        True,
        _YEAR_END_HOLDING,
        "MONEY 946400000000.000 $1.0000 $10.00",
        _YEAR_END_MISMATCH,
        None,
    ),
    "year-end-price-9-whole-digits": (
        True,
        _YEAR_END_HOLDING,
        "MONEY 10.000 $946400000.0000 $10.00",
        _YEAR_END_MISMATCH,
        None,
    ),
}


@pytest.mark.parametrize(
    ("year_end", "old", "new", "message", "column"),
    _CMA_AT_LIMIT.values(),
    ids=list(_CMA_AT_LIMIT),
)
def test_cma_cell_at_its_limit_reaches_the_next_check(
    year_end: bool, old: str, new: str, message: str, column: str | None
) -> None:
    """A cell exactly at its kind's limit is read; the statement's existing
    cross-check then refuses it, as before #1275."""
    text = _edit(_YEAR_END if year_end else _statement(), old, new)
    with pytest.raises(ParseError) as caught:
        if year_end:
            _parse_year_end(text)
        else:
            _parse(text)
    assert_unchained_refusal(
        caught.value,
        _cma_refusal(message, column),
        _cma_text(message, column),
        markers=(_MARKER,),
    )


def test_cma_yield_at_its_limit_parses() -> None:
    """The yield has no cross-check: 3 whole digits, the rate limit, parse."""
    statement = _parse(_edit(_statement(), _YIELD, "MARKET (SPAXX) 946.400%"))
    plain = _parse(_statement())
    yield_percent = Decimal("946.400")
    assert statement == replace(
        plain, position=replace(plain.position, estimated_yield_percent=yield_percent)
    )
    assert str(statement.position.estimated_yield_percent) == "946.400"


# Recognized cells whose values are not stored still get the same read-time limits.
# (layout, old text, replacement template, kind, value at limit, first refusal)
_UNUSED_CMA_CELLS = [
    *[
        (layout, "Total Core Account (100%", "Total Core Account ({value}%", RATE, "946", "9464")
        for layout in ("monthly", "annual")
    ],
    *[
        (layout, "yield: 3.95%", "yield: {value}%", RATE, "946.40", "9464.00")
        for layout in ("monthly", "annual")
    ],
    (
        "monthly",
        "Change in Account Value    $5.00",
        "Change in Account Value    ${value}",
        MONEY,
        "946400000000000.00",
        "9464000000000000.00",
    ),
    *[
        (
            "monthly",
            old,
            template,
            MONEY,
            "946400000000000.00",
            "9464000000000000.00",
        )
        for old, template in (
            ("Beginning Account Value $100.00 $100.00", "Beginning Account Value $100.00 ${value}"),
            ("Additions 10.00 10.00", "Additions 10.00 {value}"),
            ("Subtractions -5.00 -5.00", "Subtractions -5.00 -{value}"),
            ("Change in Investment Value * 0.00 0.00", "Change in Investment Value * 0.00 {value}"),
            ("Ending Account Value $105.00 $105.00", "Ending Account Value $105.00 ${value}"),
            ("Deposits    10.00    10.00", "Deposits    {value}    10.00"),
            ("Deposits    10.00    10.00", "Deposits    10.00    {value}"),
            ("Taxable    $1.10    $1.10", "Taxable    ${value}    $1.10"),
            ("Taxable    $1.10    $1.10", "Taxable    $1.10    ${value}"),
        )
    ],
    ("monthly", "100% Core Account", "{value}% Core Account", RATE, "946", "9464"),
    (
        "monthly",
        "Core Account ($105)",
        "Core Account (${value})",
        MONEY,
        "946400000000000",
        "9464000000000000",
    ),
    *[
        ("monthly", old, template, kind, at_limit, past_limit)
        for old, template, kind, at_limit, past_limit in (
            (
                "Market    $105    100%",
                "Market    ${value}    100%",
                MONEY,
                "946400000000000",
                "9464000000000000",
            ),
            ("Market    $105    100%", "Market    $105    {value}%", RATE, "946", "9464"),
            (
                "Total    $105    100%",
                "Total    ${value}    100%",
                MONEY,
                "946400000000000",
                "9464000000000000",
            ),
            ("Total    $105    100%", "Total    $105    {value}%", RATE, "946", "9464"),
        )
    ],
    *[
        ("fcb", old, template, MONEY, "946400000000000.00", "9464000000000000.00")
        for old, template in (
            ("Credit Balance    2.00", "Credit Balance    {value}"),
            ("Ending Net Account Value    $107.00", "Ending Net Account Value    ${value}"),
        )
    ],
    *[
        ("monthly", old, template, PRICE, at_limit, past_limit)
        for old, template, at_limit, past_limit in (
            ("MORNING TRADE @ 1", "MORNING TRADE @ {value}", "946400000", "9464000000"),
            ("MORNING TRADE @ 1", "@{value}", "946400000", "9464000000"),
            ("REINVEST @ $1.000", "REINVEST @ ${value}", "946400000.000", "9464000000.000"),
        )
    ],
    *[
        ("monthly", old, template, MONEY, "946400000000000.00", "9464000000000000.00")
        for old, template in (
            ("Beginning Balance    $0.00", "Beginning Balance    ${value}"),
            ("Total Payments    0.00", "Total Payments    {value}"),
            ("Total Purchases    -3.00", "Total Purchases    -{value}"),
            ("Ending Balance    $0.00", "Ending Balance    ${value}"),
        )
    ],
    (
        "annual",
        "Interest 0.50",
        "Ending Account Value as of Dec 31, 2025 ${value} Interest 0.50",
        MONEY,
        "946400000000000.00",
        "9464000000000000.00",
    ),
]


def _unused_cma_input(layout: str, old: str, template: str, value: str) -> str:
    text = _YEAR_END if layout == "annual" else _statement(fcb=layout == "fcb")
    return _edit(text, old, template.format(value=value))


@pytest.mark.parametrize(
    ("layout", "old", "template", "message", "at_limit", "past_limit"), _UNUSED_CMA_CELLS
)
def test_cma_unused_numeric_cell_past_its_limit_refuses(
    layout: str, old: str, template: str, message: str, at_limit: str, past_limit: str
) -> None:
    """Shape-checked amounts, prices and rates cannot bypass their numeric limit."""
    assert _MARKER in past_limit
    text = _unused_cma_input(layout, old, template, past_limit)
    with pytest.raises(ParseError) as caught:
        (_parse_year_end if layout == "annual" else _parse)(text)
    assert_unchained_refusal(
        caught.value, _cma_refusal(message), _cma_text(message), markers=(_MARKER,)
    )


@pytest.mark.parametrize(
    ("layout", "old", "template", "message", "at_limit", "past_limit"), _UNUSED_CMA_CELLS
)
def test_cma_unused_numeric_cell_at_its_limit_keeps_the_result(
    layout: str, old: str, template: str, message: str, at_limit: str, past_limit: str
) -> None:
    """The limit does not give a previously unused cell any new calculation meaning."""
    text = _unused_cma_input(layout, old, template, at_limit)
    baseline = _YEAR_END if layout == "annual" else _statement(fcb=layout == "fcb")
    read = _parse_year_end if layout == "annual" else _parse
    assert read(text) == read(baseline)


_CLI_QUANTITY = "9464000000000.000"


def test_cli_refuses_a_spaxx_quantity_past_its_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``fidelity-cma-income`` on the golden tests' synthetic bundle, with one
    statement's SPAXX quantity at 13 whole digits: exit 4, the fixed refusal,
    and the stale outputs removed with nothing written. Before #1275 the
    statement refused later, on its quantity and price cross-check."""
    statements, tax_forms, config = _inputs(tmp_path, monkeypatch, {})
    # The fixture's pdftotext stub.
    render = cast("Callable[..., subprocess.CompletedProcess[str]]", subprocess.run)
    holding = re.compile(r"^(FIDELITY GOVERNMENT MONEY \$\S+ )[0-9]+\.[0-9]{3}( \$1\.0000 )", re.M)

    def edited(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        result = render(args, **kwargs)
        if args[4] == str(statements / "2025-06.pdf"):
            result.stdout, count = holding.subn(rf"\g<1>{_CLI_QUANTITY}\g<2>", result.stdout)
            assert count == 1
        return result

    monkeypatch.setattr(subprocess, "run", edited)
    out = tmp_path / "out"
    out.mkdir()
    for filename in INCOME_FILING_FILENAMES:
        (out / filename).write_text("stale", encoding="utf-8")
    code = main(
        [
            "fidelity-cma-income",
            "--itr-form",
            "ITR-2",
            "--input",
            str(statements),
            "--tax-forms",
            str(tax_forms),
            "--config",
            str(config),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    assert code == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {_cma_text(QUANTITY)}\n"
    assert captured.out == ""
    assert _MARKER not in captured.out + captured.err
    assert not any(out.iterdir())


# --- user.toml ---------------------------------------------------------------


def _receipt(*, exact_quantity: str = "0.052876", reward: str = "5.00") -> str:
    return robinhood_toml(
        extra=robinhood_security_toml()
        + robinhood_security_receipt_resolution_toml(
            exact_quantity=f'"{exact_quantity}"', reward_fair_market_value_usd=f'"{reward}"'
        )
    )


def _option_amount(name: str) -> Callable[[str], str]:
    def document(value: str) -> str:
        filing = _edit(_OPTION_BUSINESS_FILING, f'{name} = "0"', f'{name} = "{value}"')
        return robinhood_toml(extra=robinhood_option_classification_toml(business_filing=filing))

    return document


def _form67_tax(value: str) -> str:
    override = _edit(
        _form67_override(), 'tax_payable_normal_inr = "100"', f'tax_payable_normal_inr = "{value}"'
    )
    return VALID_TOML + "\n[form67]\n" + override


def _section_91(*, total_tax: str = "500000", total_income: str = "1000000") -> str:
    return (
        VALID_TOML
        + "\n[form67.section_91]\n"
        + f'total_indian_tax_before_91_relief_inr = "{total_tax}"\n'
        + f'total_income_inr = "{total_income}"\n'
    )


def _cma_amount(value: str) -> str:
    return _fidelity_cma(
        "finalized",
        'provenance = "p"\n'
        f'eligible_tax = [{{ payment_date = 2025-05-01, amount_usd = "{value}" }}]\n',
    )


def _nb(line: str) -> Callable[[str], str]:
    name, _, _old = line.partition(" = ")
    return lambda value: _netbenefits(line, f'{name} = "{value}"')


_RECEIPT = "robinhood.security_receipt_resolutions[0]"
_OPTION = "robinhood.option_classifications[0]"

# Each Decimal field in user.toml: (field path, kind refusal, document holding
# a value, where the loaded config keeps it).
_FIELDS: dict[str, tuple[str, str, Callable[[str], str], str]] = {
    "exact_quantity": (
        f"{_RECEIPT}.exact_quantity",
        QUANTITY,
        lambda value: _receipt(exact_quantity=value),
        "robinhood.security_receipt_resolutions.0.exact_quantity",
    ),
    "reward_fair_market_value_usd": (
        f"{_RECEIPT}.reward_fair_market_value_usd",
        MONEY,
        lambda value: _receipt(reward=value),
        "robinhood.security_receipt_resolutions.0.reward_fair_market_value_usd",
    ),
    **{
        name: (
            f"{_OPTION}.{name}",
            MONEY,
            _option_amount(name),
            f"robinhood.option_classifications.0.{name}",
        )
        for name in ("external_expenses_inr", "bp_additions_inr", "bp_deductions_inr")
    },
    "tax_payable_normal_inr": (
        "form67.tax_payable_overrides[0].tax_payable_normal_inr",
        MONEY,
        _form67_tax,
        "form67.tax_payable_overrides.0.tax_payable_normal_inr",
    ),
    "total_indian_tax_before_91_relief_inr": (
        "form67.section_91.total_indian_tax_before_91_relief_inr",
        MONEY,
        lambda value: _section_91(total_tax=value),
        "form67.section_91.total_indian_tax_before_91_relief_inr",
    ),
    "total_income_inr": (
        "form67.section_91.total_income_inr",
        MONEY,
        lambda value: _section_91(total_income=value),
        "form67.section_91.total_income_inr",
    ),
    "amount_usd": (
        "fidelity_cma.us_tax_years[0].eligible_tax[0].amount_usd",
        MONEY,
        _cma_amount,
        "fidelity_cma.us_tax_years.0.eligible_tax.0.amount_usd",
    ),
    "section17_gross_fmv_total_inr": (
        "netbenefits.cg_fmv_values[0].section17_gross_fmv_total_inr",
        MONEY,
        _nb('section17_gross_fmv_total_inr = "41850.00"'),
        "netbenefits.cg_fmv_values.0.section17_gross_fmv_total_inr",
    ),
    "price_per_share_usd": (
        "netbenefits.price_overrides[0].price_per_share_usd",
        PRICE,
        _nb('price_per_share_usd = "213.47"'),
        "netbenefits.price_overrides.0.price_per_share_usd",
    ),
    "espp_discount_pct": (
        "netbenefits.espp_discount_pct",
        RATE,
        _nb('espp_discount_pct = "10"'),
        "netbenefits.espp_discount_pct",
    ),
}
_PLAIN_MONEY = tuple(
    name
    for name, (_path, kind, _document, _where) in _FIELDS.items()
    if kind == MONEY and name not in {"amount_usd", "section17_gross_fmv_total_inr"}
)

# 15 whole digits and 20 places, then one digit or place more.
_MONEY_AT_LIMIT = "946400000000000.94640000000000000001"
_MONEY_WHOLE = "9464000000000000.00"
_MONEY_PLACES = "5.946400000000000000001"

# (field, value exactly at its limits)
_CONFIG_AT_LIMIT: dict[str, tuple[str, str]] = {
    "exact_quantity-12-whole-20-places": ("exact_quantity", "946400000000.94640000000000000001"),
    **{f"{name}-15-whole-20-places": (name, _MONEY_AT_LIMIT) for name in _PLAIN_MONEY},
    # Its own rule keeps 2 places.
    "amount_usd-15-whole-digits": ("amount_usd", "946400000000000.00"),
    # The CBDT bound keeps 14 whole digits.
    "section17_gross_fmv_total_inr-14-whole-20-places": (
        "section17_gross_fmv_total_inr",
        "94640000000000.94640000000000000001",
    ),
    "price_per_share_usd-9-whole-20-places": (
        "price_per_share_usd",
        "946400000.94640000000000000001",
    ),
    "espp_discount_pct-6-places": ("espp_discount_pct", "99.946400"),
    "espp_discount_pct-3-whole-6-places": ("espp_discount_pct", "100.000000"),
}


def _read(config: object, where: str) -> object:
    value = config
    for part in where.split("."):
        value = (
            cast("tuple[object, ...]", value)[int(part)] if part.isdigit() else getattr(value, part)
        )
    return value


@pytest.mark.parametrize(("name", "value"), _CONFIG_AT_LIMIT.values(), ids=list(_CONFIG_AT_LIMIT))
def test_config_number_at_its_limits_loads_exactly(tmp_path: Path, name: str, value: str) -> None:
    """Each field at its kind's limits (or its own tighter bound) loads with
    every digit and place it was given."""
    _path, _kind, document, where = _FIELDS[name]
    loaded = _read(load_user_config(write(tmp_path, document(value))), where)
    assert isinstance(loaded, Decimal)
    assert str(loaded) == value


# (field, value one digit or place past its kind's limit)
_CONFIG_PAST_LIMIT: dict[str, tuple[str, str]] = {
    "exact_quantity-13-whole-digits": ("exact_quantity", "9464000000000"),
    "exact_quantity-21-places": ("exact_quantity", "0.946400000000000000001"),
    **{f"{name}-16-whole-digits": (name, _MONEY_WHOLE) for name in _PLAIN_MONEY},
    **{f"{name}-21-places": (name, _MONEY_PLACES) for name in _PLAIN_MONEY},
    "amount_usd-16-whole-digits": ("amount_usd", _MONEY_WHOLE),
    # Before #1275 its 2-places rule refused this.
    "amount_usd-21-places": ("amount_usd", "0.946400000000000000001"),
    # Before #1275 its CBDT bound refused this.
    "section17_gross_fmv_total_inr-16-whole-digits": (
        "section17_gross_fmv_total_inr",
        _MONEY_WHOLE,
    ),
    "section17_gross_fmv_total_inr-21-places": (
        "section17_gross_fmv_total_inr",
        "41850.946400000000000000001",
    ),
    "price_per_share_usd-10-whole-digits": ("price_per_share_usd", "9464000000.00"),
    "price_per_share_usd-21-places": ("price_per_share_usd", "213.946400000000000000001"),
    # 4 whole digits are past 100, which keeps its own refusal (below).
    "espp_discount_pct-7-places": ("espp_discount_pct", "15.9464001"),
}


@pytest.mark.parametrize(
    ("name", "value"), _CONFIG_PAST_LIMIT.values(), ids=list(_CONFIG_PAST_LIMIT)
)
def test_config_number_past_its_limit_refuses(tmp_path: Path, name: str, value: str) -> None:
    """One field per path and kind, a digit or a place past the kind's limit:
    the kind's fixed refusal at the field, after the string, finite and sign
    checks and before the field's own bounds."""
    path, kind, document, _where = _FIELDS[name]
    config = write(tmp_path, document(value))
    assert _MARKER in value
    with pytest.raises(ConfigError) as caught:
        load_user_config(config)
    assert_unchained_refusal(
        caught.value,
        ConfigError(field=path, message=kind),
        f"ConfigError at field {path!r}: {kind}",
        markers=(_MARKER,),
    )


_CBDT_BOUND = "must be > 0 and <= 99999999999999"
_AT_MOST_100 = "must be a decimal number of at most 100"

# (field, value, the refusal it keeps): each value is past an existing bound
# that is tighter than the kind's limit or runs before it.
_CONFIG_KEPT: dict[str, tuple[str, str, str]] = {
    # tests/test_config_no_echo.py's case: 15 whole digits, one past the bound.
    "section17-cbdt-bound": ("section17_gross_fmv_total_inr", "100000000000000.89", _CBDT_BOUND),
    "section17-15-whole-digits": (
        "section17_gross_fmv_total_inr",
        "946400000000000.00",
        _CBDT_BOUND,
    ),
    "espp-just-past-100": ("espp_discount_pct", "100.01", _AT_MOST_100),
    "espp-4-whole-digits": ("espp_discount_pct", "9464.000", _AT_MOST_100),
    "espp-past-100-with-7-places": ("espp_discount_pct", "100.9464001", _AT_MOST_100),
    "amount_usd-3-places": (
        "amount_usd",
        "0.946",
        "must be a non-negative whole-cent USD amount (at most 2 decimal places)",
    ),
    "exact_quantity-negative-13-whole-digits": (
        "exact_quantity",
        "-9464000000000",
        "must be a decimal number of at least 0",
    ),
}


@pytest.mark.parametrize(
    ("name", "value", "message"), _CONFIG_KEPT.values(), ids=list(_CONFIG_KEPT)
)
def test_config_value_past_an_existing_bound_keeps_its_refusal(
    tmp_path: Path, name: str, value: str, message: str
) -> None:
    """A value just past an existing bound keeps that bound's refusal: the CBDT
    bound, at most 100, whole cents and the sign check."""
    path, _kind, document, _where = _FIELDS[name]
    config = write(tmp_path, document(value))
    with pytest.raises(ConfigError) as caught:
        load_user_config(config)
    assert_unchained_refusal(
        caught.value,
        ConfigError(field=path, message=message),
        f"ConfigError at field {path!r}: {message}",
        markers=(value, _MARKER) if _MARKER in value else (value,),
    )
