"""``user.toml`` refusals name the field and the rule, never a configured value (#1045).

Each case pins the exact field path and fixed message, and checks that the
distinct privacy markers it configured appear nowhere in the error. Public
constants and short values that could collide with fixed text (the verified
StockPlan symbol, ``ZZ``, small years) are covered by the exact message alone.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

import ibkr_fa.config as config_module
from ibkr_fa.config import load_user_config
from ibkr_fa.exceptions import ConfigError
from ibkr_fa.parser.stockplan import require_verified_issuer
from tests._config_toml import (
    _A2_PEAK,
    UNKNOWN_COUNTRY_CODE,
    UNVERIFIED_STOCKPLAN_ISSUER,
    VALID_NETBENEFITS,
    VALID_TOML,
    _fidelity_cma,
    _form67_override,
    _nb_with_binding,
    _netbenefits,
    unknown_key,
)
from tests._crash_reports import assert_prints_no_marker, assert_unchained_refusal
from tests._ibkr_user_toml import CUSTODIAN_TOML
from tests._robinhood_toml import (
    _OPTION_BUSINESS_FILING,
    robinhood_account_toml,
    robinhood_option_classification_toml,
    robinhood_security_receipt_resolution_toml,
    robinhood_security_toml,
    robinhood_toml,
    write,
)
from tests._text_edits import replace_once

_NAME = "Synthetic Holder Two"
_ACCOUNT = "U1234567"
_TIN = "123-45-6789"
_NUMBER = "98765432"
_TEXT_MARKERS = (_NAME, _ACCOUNT, _TIN)


def _section_91(total_income: str) -> str:
    return (
        VALID_TOML
        + "\n[form67.section_91]\n"
        + 'total_indian_tax_before_91_relief_inr = "500000"\n'
        + f'total_income_inr = "{total_income}"\n'
    )


def _manual_value(calendar_year: int | str) -> str:
    return (
        f"\n[[schedule_fa.manual_values]]\ncalendar_year = {calendar_year}\n"
        "a2_peak_balance_inr = 1000\n"
    )


def _schedule_fa(*years: int | str) -> str:
    return (
        VALID_TOML
        + f'\n[schedule_fa]\nmanual_values_account_number = "{_ACCOUNT}"\n'
        + "".join(_manual_value(year) for year in years)
    )


def _a2_peak(calendar_year: int | str) -> str:
    return replace_once(_A2_PEAK, "calendar_year = 2025", f"calendar_year = {calendar_year}")


def _ticker_override(ticker: str) -> str:
    return (
        "\n[[prices.ticker_overrides]]\n"
        f'ibkr_ticker = "{ticker}"\nisin = "CA99991A1003"\nyahoo_symbol = "XQAM"\n'
    )


def _fa_entity(isin: str) -> str:
    return f'\n[[fa.entities]]\nisin = "{isin}"\naddress = "Sampletown, CA"\n'


_DECIMAL = "must be a decimal number"
_ISO_DATE = "must be an ISO date (YYYY-MM-DD)"
_CALENDAR_YEAR_RANGE = "must be between 2000 and 2100"
_DUPLICATE_YEAR = "duplicates another configured year"
_DUPLICATE_TICKER = (
    "duplicates another override's ibkr_ticker; each ibkr_ticker may appear at most once "
    "(the price fetch is ticker-keyed, so two overrides for one ticker cannot both apply)"
)
_BEFORE_OPENING = "a Schedule FA peak for a year the account did not exist is impossible"

# (toml, field, message, markers that must not appear in the error)
_CASES: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    # Marker cases: a field that takes the marker as raw text.
    **{
        f"decimal-raw-{marker}": (
            _section_91(marker),
            "form67.section_91.total_income_inr",
            _DECIMAL,
            (marker,),
        )
        for marker in _TEXT_MARKERS
    },
    **{
        f"iso-date-raw-{marker}": (
            replace_once(VALID_TOML, "opening_date = 2024-02-01", f'opening_date = "{marker}"'),
            "account.opening_date",
            _ISO_DATE,
            (marker,),
        )
        for marker in _TEXT_MARKERS
    },
    "schedule-fa-calendar-year-range": (
        _schedule_fa(_NUMBER),
        "schedule_fa.manual_values[0].calendar_year",
        _CALENDAR_YEAR_RANGE,
        (_NUMBER,),
    ),
    "netbenefits-a2-calendar-year-range": (
        VALID_TOML + _nb_with_binding() + _a2_peak(_NUMBER),
        "netbenefits.a2_peak_overrides[0].calendar_year",
        _CALENDAR_YEAR_RANGE,
        (_NUMBER,),
    ),
    **{
        f"robinhood-sub-category-{marker}": (
            robinhood_toml(extra=robinhood_security_toml(sub_category=marker)),
            "robinhood.securities[0].sub_category",
            "must be one of: COMMON, ETF",
            (marker,),
        )
        for marker in _TEXT_MARKERS
    },
    **{
        f"duplicate-ticker-{marker}": (
            VALID_TOML + _ticker_override(marker) + _ticker_override(marker),
            "prices.ticker_overrides[1]",
            _DUPLICATE_TICKER,
            (marker,),
        )
        for marker in _TEXT_MARKERS
    },
    # Rule cases.
    "decimal-nan-payload": (
        _section_91("NaN1234567"),
        "form67.section_91.total_income_inr",
        "must be a finite decimal number, not NaN or Infinity",
        ("NaN1234567", "1234567"),
    ),
    "decimal-negative": (
        _section_91("-1234567.89"),
        "form67.section_91.total_income_inr",
        "must be a decimal number of at least 0",
        ("1234567.89",),
    ),
    "percent-over-100": (
        _netbenefits('espp_discount_pct = "10"', 'espp_discount_pct = "123.45"'),
        "netbenefits.espp_discount_pct",
        "must be a decimal number of at most 100",
        ("123.45",),
    ),
    "fmv-total-over-max": (
        _netbenefits(
            'section17_gross_fmv_total_inr = "41850.00"',
            'section17_gross_fmv_total_inr = "100000000000000.89"',
        ),
        "netbenefits.cg_fmv_values[0].section17_gross_fmv_total_inr",
        "must be > 0 and <= 99999999999999",
        ("100000000000000.89",),
    ),
    "duplicate-price-date": (
        VALID_TOML
        + VALID_NETBENEFITS
        + "\n[[netbenefits.price_overrides]]\nprice_date = 2025-12-31\n"
        + 'price_per_share_usd = "1.00"\nsource = "s"\nreason = "r"\n',
        "netbenefits.price_overrides[1]",
        "duplicates another override's price_date; each date may appear at most once",
        ("2025-12-31",),
    ),
    "duplicate-cg-fmv-date": (
        VALID_TOML
        + VALID_NETBENEFITS
        + "\n[[netbenefits.cg_fmv_values]]\npurchase_date = 2025-12-31\n"
        + 'section17_gross_fmv_total_inr = "1.00"\nsource = "s"\n',
        "netbenefits.cg_fmv_values[1]",
        "duplicates another value's purchase_date; each purchase date may appear at most once",
        ("2025-12-31",),
    ),
    "duplicate-schedule-fa-year": (
        _schedule_fa(2025, 2025),
        "schedule_fa.manual_values[1]",
        _DUPLICATE_YEAR,
        (),
    ),
    "duplicate-netbenefits-a2-year": (
        VALID_TOML + _nb_with_binding() + _A2_PEAK + _A2_PEAK,
        "netbenefits.a2_peak_overrides[1]",
        _DUPLICATE_YEAR,
        (),
    ),
    "custodian-unknown-country": (
        VALID_TOML + replace_once(CUSTODIAN_TOML, 'country_iso2 = "US"', 'country_iso2 = "ZZ"'),
        "account.custodian.country_iso2",
        UNKNOWN_COUNTRY_CODE,
        (),
    ),
    "unknown-country": (
        _netbenefits('country_iso2 = "US"', 'country_iso2 = "ZZ"'),
        "netbenefits.account.country_iso2",
        UNKNOWN_COUNTRY_CODE,
        (),
    ),
    "duplicate-form67-override": (
        VALID_TOML + "\n[form67]\n" + _form67_override() + _form67_override(),
        "form67.tax_payable_overrides[1]",
        "duplicates another override for the same country and source",
        (),
    ),
    "isin-check-digit": (
        VALID_TOML + _fa_entity("US99992A1096"),
        "fa.entities[0].isin",
        "invalid check digit",
        (),
    ),
    "cusip-check-digit": (
        _netbenefits('cusip = "99999E104"', 'cusip = "99999E105"'),
        "netbenefits.security.cusip",
        "invalid check digit",
        (),
    ),
    "duplicate-isin": (
        VALID_TOML + _fa_entity("US99992A1097") + _fa_entity("US99992A1097"),
        "fa.entities[1]",
        "duplicates another override's ISIN; each ISIN may appear at most once",
        ("US99992A1097",),
    ),
    "isin-not-the-cusip": (
        _netbenefits('isin = "US99999E1047"', 'isin = "US99992A1097"'),
        "netbenefits.security.isin",
        "must be the US ISIN for the configured CUSIP ('US', the CUSIP, then the ISIN check digit)",
        ("US99992A1097", "US99999E1047", "99999E104"),
    ),
    "netbenefits-a2-year-before-opening": (
        VALID_TOML + _nb_with_binding() + _a2_peak(2024),
        "netbenefits.a2_peak_overrides[0].calendar_year",
        f"is before the year of netbenefits.account.account_opening_date; {_BEFORE_OPENING}",
        (),
    ),
    "schedule-fa-year-before-opening": (
        _schedule_fa(2023),
        "schedule_fa.manual_values[0].calendar_year",
        f"is before the year of account.opening_date; {_BEFORE_OPENING}",
        ("2024-02-01",),
    ),
    "a2-participant-binding": (
        VALID_TOML
        + replace_once(
            VALID_NETBENEFITS,
            'participant_no = "OQ1234567"\n',
            f'participant_no = "{_TIN}"\na2_peak_overrides_participant_no = "{_NUMBER}"\n',
        )
        + _A2_PEAK,
        "netbenefits.a2_peak_overrides_participant_no",
        "must equal [netbenefits].participant_no; the manual A2 peak is bound to that participant",
        (_TIN, _NUMBER),
    ),
    "manual-values-account-binding": (
        VALID_TOML + '\n[schedule_fa]\nmanual_values_account_number = "U7654321"\n',
        "schedule_fa.manual_values_account_number",
        "must equal [account].number; the manual Schedule FA figures are bound to that account",
        ("U7654321", _ACCOUNT),
    ),
    "participant-is-the-ibkr-account": (
        _netbenefits('participant_no = "OQ1234567"', f'participant_no = "{_ACCOUNT}"'),
        "netbenefits.participant_no",
        "must not equal [account].number; the IBKR account ID is a different identifier "
        "from the Fidelity Participant Number",
        (_ACCOUNT,),
    ),
    # Sites #1045 doesn't name.
    "cma-payment-date-outside-tax-year": (
        _fidelity_cma(
            "finalized",
            'provenance = "p"\n'
            'eligible_tax = [{ payment_date = 2024-06-17, amount_usd = "1.00" }]\n',
        ),
        "fidelity_cma.us_tax_years[0].eligible_tax[0].payment_date",
        "must fall in the US tax year given by fidelity_cma.us_tax_years[0].tax_year",
        (),
    ),
    "cma-provenance-required": (
        _fidelity_cma("no-refund-claim", ""),
        "fidelity_cma.us_tax_years[0].provenance",
        "is required when refund_status is finalized or no-refund-claim",
        (),
    ),
    "price-not-positive": (
        _netbenefits('price_per_share_usd = "213.47"', 'price_per_share_usd = "0.000"'),
        "netbenefits.price_overrides[0].price_per_share_usd",
        "must be > 0",
        ("0.000",),
    ),
    "discount-not-positive": (
        _netbenefits('espp_discount_pct = "10"', 'espp_discount_pct = "0.000"'),
        "netbenefits.espp_discount_pct",
        "must be a percent > 0 and <= 100",
        ("0.000",),
    ),
}


@pytest.mark.parametrize("case", _CASES)
def test_refusal_names_the_field_and_rule_without_the_value(tmp_path: Path, case: str) -> None:
    toml, field, message, markers = _CASES[case]
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == field
    assert exc.value.message == message
    for marker in markers:
        assert marker not in str(exc.value)


def test_foreign_tins_entry_is_named_by_position_not_key(tmp_path: Path) -> None:
    toml = VALID_TOML + f'\n[account.foreign_tins]\nGB = "AB123456C"\n"{_TIN}" = "US"\n'
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "account.foreign_tins[1]"
    assert exc.value.message == "must be a 2-letter uppercase ISO 3166-1 alpha-2 code"
    assert _TIN not in str(exc.value)


def test_unknown_key_is_refused_without_the_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + f'"{_NAME}" = "x"\n'))
    assert exc.value.field == "account"
    assert exc.value.message == unknown_key(
        "accept_anonymised_statement, allow_masked_account, custodian, foreign_tins, "
        "holder_name, number, opening_date, status, us_person"
    )
    assert _NAME not in str(exc.value)


def _decode_error(document: str) -> tomllib.TOMLDecodeError:
    try:
        tomllib.loads(document)
    except tomllib.TOMLDecodeError as exc:
        return exc
    raise AssertionError("the document parsed")


def test_toml_error_takes_the_position_from_lineno_and_colno(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Python 3.14 sets lineno and colno; they win over the message suffix.
    error = _decode_error(f"x = {{{_ACCOUNT} = 1, {_ACCOUNT} = 2}}\n")
    vars(error).update(lineno=3, colno=7)

    def refuse(_file: object) -> object:
        raise error

    monkeypatch.setattr(tomllib, "load", refuse)
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML))
    assert exc.value.field == "<file>"
    assert exc.value.message == "invalid TOML at line 3, column 7"
    assert _ACCOUNT not in str(exc.value)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (f"x = {{{_ACCOUNT} = 1, {_ACCOUNT} = 2}}\n", "invalid TOML at line 6, column 32"),
        # A quoted key that mimics the suffix: only the real, final one counts.
        (
            'x = {"(at line 5, column 6)" = 1, "(at line 5, column 6)" = 2}\n',
            "invalid TOML at line 6, column 62",
        ),
    ],
    ids=["duplicate-inline-key", "key-mimics-the-suffix"],
)
def test_toml_error_takes_the_position_from_the_message_suffix(
    tmp_path: Path, line: str, message: str
) -> None:
    # Python 3.12 and 3.13 give the position only as "(at line N, column M)".
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + line))
    assert exc.value.field == "<file>"
    assert exc.value.message == message
    if _ACCOUNT in line:
        assert _ACCOUNT not in str(exc.value)


@pytest.mark.skipif(
    sys.version_info >= (3, 14), reason="3.14 sets lineno and colno on every TOML error"
)
def test_toml_error_without_a_position_is_plain(tmp_path: Path) -> None:
    # A repeated top-level key reports only "(at end of document)".
    error = _decode_error("a = 1\na = 2")
    assert str(error).endswith("(at end of document)")
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, "a = 1\na = 2"))
    assert exc.value.field == "<file>"
    assert exc.value.message == "invalid TOML"


@pytest.mark.parametrize(
    ("symbol", "isin", "markers"),
    [
        (_ACCOUNT, "US99999E1047", (_ACCOUNT, "US99999E1047")),
        ("GOOG", "US99999E1047", ("US99999E1047",)),
    ],
)
def test_stockplan_unverified_issuer_refusal_is_fixed_text(
    symbol: str, isin: str, markers: tuple[str, ...]
) -> None:
    with pytest.raises(ConfigError) as exc:
        require_verified_issuer(symbol, isin)
    assert exc.value.field == "stockplan.security"
    assert exc.value.message == UNVERIFIED_STOCKPLAN_ISSUER
    for marker in markers:
        assert marker not in str(exc.value)


# ---- Exception graph (#1210) ------------------------------------------------
#
# Each refusal below used to chain an exception holding its input: an enum
# lookup's ValueError or KeyError, a country lookup's code, a date parse's
# ValueError, a decode error's bytes, a TOML error's text and, on Python 3.14,
# its whole document. Each input holds distinct markers for its own values and
# a later failure (``_LATER``) that must not refuse first.

_LATER = '\n[schedule_fa]\nmanual_values_account_number = "U7654321"\n'
_LATER_THAN_ACCOUNT = ConfigError(
    field="schedule_fa.manual_values_account_number",
    message="must equal [account].number; the manual Schedule FA figures are bound to that account",
)
_LATER_WITHOUT_ACCOUNT = ConfigError(
    field="schedule_fa.manual_values_account_number",
    message="manual IBKR Schedule FA values require a real [account].number",
)
_RH_CLASSIFICATION = "robinhood.option_classifications[0]"
_RH_ACCOUNT = "robinhood.accounts[0]"
_BYTES_MARKER = "Synthetic Bytes Marker"
_TABLE_MARKER = "synthetic_table_marker"
_DOCUMENT_MARKER = "Synthetic Document Marker"
_DUPLICATE_TABLE = f"\n[{_TABLE_MARKER}]\n[{_TABLE_MARKER}]\n"
_NOT_UTF8 = (
    (VALID_TOML + f"# {_BYTES_MARKER}\n").encode()
    + b"# \xff\n"
    + _DUPLICATE_TABLE.encode()
    + _LATER.encode()
)


def _ibkr(body: str) -> bytes:
    return (VALID_TOML + body + _LATER).encode()


def _robinhood_account(number: str, **fields: str) -> str:
    defaults = {
        "number": number,
        "opening_date": "2025-01-01",
        "source": f"synthetic statement {number}",
        "institution_name": "Synthetic Broker LLC",
        "address": "1 Sample Way, Sampletown",
        "zip_code": "00001",
    }
    return robinhood_account_toml(**(defaults | fields))


def _robinhood(extra: str = "", **first: str) -> bytes:
    """Robinhood's two filed accounts; ``first`` replaces fields of the first."""
    return robinhood_toml(
        first=_robinhood_account("100000001", **first),
        second=_robinhood_account("200000002"),
        extra=extra + _LATER,
    ).encode()


def _cma(fields: str = "", years: str = "") -> bytes:
    return (
        VALID_TOML
        + '\n[fidelity_cma]\naccount_id = "Z99-999999"\nspaxx_isin = "US99999E1047"\n'
        + fields
        + years
        + _LATER
    ).encode()


def _skip_the_spaxx_isin_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """TEST-ONLY STUB: ``[fidelity_cma]`` accepts only the real SPAXX ISIN, which
    a synthetic fixture can't carry, so this skips that one equality check.
    Every other check still runs, including ``spaxx_isin``'s string checks."""
    ensure = config_module._ensure

    def stub(condition: bool, field: str, message: str) -> None:
        if field == "fidelity_cma.spaxx_isin" and message.startswith(
            "must be the verified SPAXX ISIN"
        ):
            return
        ensure(condition, field, message)

    monkeypatch.setattr(config_module, "_ensure", stub)


# (document, expected refusal, markers it must not carry anywhere)
_GRAPH_CASES: dict[str, tuple[bytes, ConfigError, tuple[str, ...]]] = {
    "robinhood-option-treatment": (
        _robinhood(robinhood_option_classification_toml(treatment="Synthetic Treatment Marker")),
        ConfigError(
            field=f"{_RH_CLASSIFICATION}.treatment",
            message="must be one of: capital-gains, speculative-business, non-speculative-business",
        ),
        ("Synthetic Treatment Marker",),
    ),
    "robinhood-turnover-basis": (
        _robinhood(
            robinhood_option_classification_toml(
                business_filing=replace_once(
                    _OPTION_BUSINESS_FILING,
                    '"icai-5.11a-differences"',
                    '"Synthetic Turnover Marker"',
                )
            )
        ),
        ConfigError(
            field=f"{_RH_CLASSIFICATION}.turnover_basis",
            message="must be: icai-5.11a-differences",
        ),
        ("Synthetic Turnover Marker",),
    ),
    "robinhood-receipt-treatment": (
        _robinhood(
            robinhood_security_receipt_resolution_toml(treatment="Synthetic Receipt Marker")
        ),
        ConfigError(
            field="robinhood.security_receipt_resolutions[0].treatment",
            message="must be promotional-reward",
        ),
        ("Synthetic Receipt Marker",),
    ),
    "robinhood-iso-date": (
        _robinhood(opening_date='"Synthetic Date Marker"'),
        ConfigError(field=f"{_RH_ACCOUNT}.opening_date", message=_ISO_DATE),
        ("Synthetic Date Marker",),
    ),
    "robinhood-taxpayer-status": (
        _robinhood(status="Synthetic Status Marker"),
        ConfigError(
            field=f"{_RH_ACCOUNT}.status", message="must be one of: BeneficialOwner, Joint"
        ),
        ("Synthetic Status Marker",),
    ),
    "robinhood-country": (
        _robinhood(country_iso2="QX"),
        ConfigError(field=f"{_RH_ACCOUNT}.country_iso2", message=UNKNOWN_COUNTRY_CODE),
        ("QX",),
    ),
    "cma-refund-status": (
        _cma(
            years=(
                "\n[[fidelity_cma.us_tax_years]]\ntax_year = 2025\n"
                'refund_status = "Synthetic Refund Marker"\n'
            )
        ),
        ConfigError(
            field="fidelity_cma.us_tax_years[0].refund_status",
            message=(
                "must be one of: unknown, not-filed-planned, filed-pending, finalized, "
                "no-refund-claim"
            ),
        ),
        ("Synthetic Refund Marker",),
    ),
    "cma-peak-balance-bound": (
        _cma(
            'suppress_peak_balance_warning = true\npeak_balance_bound = "Synthetic Bound Marker"\n'
        ),
        ConfigError(
            field="fidelity_cma.peak_balance_bound",
            message=(
                'must be "lower" (minimum proven native balance; may understate the '
                'intraday peak) or "upper" (more conservative in native currency; '
                "may overstate it); each converts at its own date's TTBR, so the "
                "rupee order can differ, and fidelity-cma-fa refuses the bound when it does"
            ),
        ),
        ("Synthetic Bound Marker",),
    ),
    "cma-taxpayer-status": (
        _cma('status = "Synthetic Layout Marker"\n'),
        ConfigError(
            field="fidelity_cma.status",
            message="must be BeneficialOwner for the observed INDIVIDUAL TOD layout",
        ),
        ("Synthetic Layout Marker",),
    ),
    "custodian-country": (
        _ibkr(
            "\n[account.custodian]\n"
            'institution_name = "Synthetic Custodian LLC"\n'
            'address = "2 Sample Way, Sampletown"\n'
            'zip = "00002"\ncountry_iso2 = "XQ"\n'
        ),
        ConfigError(field="account.custodian.country_iso2", message=UNKNOWN_COUNTRY_CODE),
        ("XQ",),
    ),
    "os-routing": (
        _ibkr('\n[os]\nschedule_os_indian_adr_routing = "Synthetic Routing Marker"\n'),
        ConfigError(
            field="os.schedule_os_indian_adr_routing",
            message="must be one of: depositary, underlying-issuer",
        ),
        ("Synthetic Routing Marker",),
    ),
    "form67-source-name": (
        _ibkr(
            "\n[form67]\n"
            + replace_once(_form67_override(), '"DIVIDEND"', '"Synthetic Source Marker"')
        ),
        ConfigError(
            field="form67.tax_payable_overrides[0].source",
            message=(
                "must be a Form67SourceOfIncome enum NAME (uppercase). Valid names: SALARY, "
                "HOUSE_PROPERTY, BUSINESS_INCOME, LONG_TERM_CAPITAL_GAIN, "
                "SHORT_TERM_CAPITAL_GAIN, INTEREST_INCOME, DIVIDEND, ROYALTY, "
                "FEES_FOR_TECHNICAL_SERVICES, OTHERS"
            ),
        ),
        ("Synthetic Source Marker",),
    ),
    # The decode error comes first, before the duplicate table after the bad byte.
    "not-utf8": (
        _NOT_UTF8,
        ConfigError(
            field="<file>",
            message=(
                f"must be UTF-8 text, but the byte at offset {_NOT_UTF8.index(b'\xff')} "
                "(counting from 0) is not valid UTF-8"
            ),
        ),
        (_BYTES_MARKER, _TABLE_MARKER),
    ),
    # The document marker sits apart from the duplicate table: only Python
    # 3.14's ``doc`` holds it.
    "duplicate-table": (
        (f"# {_DOCUMENT_MARKER}\n" + VALID_TOML + _DUPLICATE_TABLE + _LATER).encode(),
        ConfigError(field="<file>", message="invalid TOML at line 9, column 24"),
        (_TABLE_MARKER, _DOCUMENT_MARKER),
    ),
    # The parser's message quotes nothing here, so on Python 3.14 the document
    # marker is the one its ``doc`` gives away.
    "toml-error-quoting-nothing": (
        (f"# {_DOCUMENT_MARKER}\n" + VALID_TOML + "broken =\n" + _LATER).encode(),
        ConfigError(field="<file>", message="invalid TOML at line 7, column 9"),
        (_DOCUMENT_MARKER,),
    ),
}


def _refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: bytes, *, field: str
) -> ConfigError:
    if field.startswith("fidelity_cma."):
        _skip_the_spaxx_isin_check(monkeypatch)
    path = tmp_path / "user.toml"
    path.write_bytes(document)
    with pytest.raises(ConfigError) as exc:
        load_user_config(path)
    return exc.value


@pytest.mark.parametrize("case", _GRAPH_CASES)
def test_refusal_graph_holds_no_configured_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    document, expected, markers = _GRAPH_CASES[case]
    error = _refusal(tmp_path, monkeypatch, document, field=expected.field)
    assert_unchained_refusal(error, expected, str(expected), markers=markers)


@pytest.mark.parametrize("case", _GRAPH_CASES)
def test_refusal_in_a_caller_handler_links_only_the_caller_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    document, expected, markers = _GRAPH_CASES[case]
    caller = LookupError("caller")
    try:
        raise caller
    except LookupError:
        error = _refusal(tmp_path, monkeypatch, document, field=expected.field)
    assert error == expected
    assert error.__cause__ is None
    assert error.__context__ is caller
    assert_prints_no_marker(error, markers=markers)


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (_ibkr(""), _LATER_THAN_ACCOUNT),
        (_robinhood(), _LATER_WITHOUT_ACCOUNT),
        (_cma(), _LATER_THAN_ACCOUNT),
    ],
    ids=["ibkr", "robinhood", "cma"],
)
def test_the_later_failure_refuses_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: bytes, expected: ConfigError
) -> None:
    # The cases' later failure is live in each base, and the CMA stub lets
    # the load reach it.
    _skip_the_spaxx_isin_check(monkeypatch)
    assert _refusal(tmp_path, monkeypatch, document, field=expected.field) == expected
