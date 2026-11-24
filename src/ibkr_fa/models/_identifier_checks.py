"""Identifier checks shared by the models (#627, #628, #854, #1654, #1658)."""

from __future__ import annotations

import re
from typing import Final

from ibkr_fa.invariants import require
from ibkr_fa.text_checks import first_control_character
from ibkr_fa.types import ISIN_PATTERN as _ISIN_PATTERN

# ISO 4217 currency codes are exactly 3 uppercase ASCII letters.
_CURRENCY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{3}$")
_ISO_4217_LENGTH: Final[int] = 3

# ISO 3166-1 alpha-2 country codes are exactly 2 uppercase ASCII letters.
_COUNTRY_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{2}$")
_ISO_3166_ALPHA2_LENGTH: Final[int] = 2

# CBDT country codes are 1-4 digit numeric strings, vendored as strings to
# keep any leading zero CBDT might introduce (e.g. "002") without an int → str
# step at every render site. See
# :data:`~ibkr_fa.reference.country_codes._CBDT_CODE_PATTERN`.
_MIN_CBDT_CODE_LENGTH: Final[int] = 1
_MAX_CBDT_CODE_LENGTH: Final[int] = 4

# India's ISO 3166-1 alpha-2 and CBDT ITR-2 country codes. The foreign
# rows (Form 67, Schedule FSI / TR, Schedule FA A2 / A3) refuse India.
INDIA_ISO_ALPHA2: Final[str] = "IN"
INDIA_CBDT_CODE: Final[str] = "91"


def is_isin_shape(isin: str) -> bool:
    """Whether ``isin`` has the ISO 6166 shape (:data:`_ISIN_PATTERN`)."""
    return _ISIN_PATTERN.fullmatch(isin) is not None


def is_cbdt_country_code(code: str) -> bool:
    """Whether ``code`` is a 1-4 character ASCII-decimal CBDT country code.

    Both ``isascii()`` and ``isdecimal()`` are needed: ``isdecimal()`` alone
    accepts non-ASCII decimals, such as fullwidth or Arabic-Indic digits, that
    would get past the ASCII India code :data:`INDIA_CBDT_CODE`.
    """
    return (
        code.isascii()
        and code.isdecimal()
        and _MIN_CBDT_CODE_LENGTH <= len(code) <= _MAX_CBDT_CODE_LENGTH
    )


def is_portal_country_name(name: str) -> bool:
    """Whether ``name`` is a non-blank, upper-case portal country display name."""
    return name.strip() != "" and name.isupper()


def is_iso_alpha2(code: str) -> bool:
    """Whether ``code`` is 2 upper-case ASCII letters (ISO 3166-1 alpha-2).

    The string-method form: ``isascii()`` keeps out non-ASCII upper-case
    letters that ``isupper()`` and ``isalpha()`` accept. It agrees with
    :data:`_COUNTRY_CODE_PATTERN` on every ``str``, not on other types such as
    ``bytes``, so the two stay separate.
    """
    return (
        len(code) == _ISO_3166_ALPHA2_LENGTH
        and code.isascii()
        and code.isupper()
        and code.isalpha()
    )


def is_iso_currency_code(code: str) -> bool:
    """Whether ``code`` is 3 upper-case ASCII letters (ISO 4217), the
    income-event models' currency rule.

    The string-method form, like :func:`is_iso_alpha2`. It agrees with
    :data:`_CURRENCY_PATTERN` on every ``str``, not on other types such as
    ``bytes``, so the two stay separate.
    """
    return len(code) == _ISO_4217_LENGTH and code.isascii() and code.isalpha() and code.isupper()


def is_not_india(iso_alpha2: str, cbdt_country_code: str) -> bool:
    """Whether neither the ISO code nor the CBDT code is India's; the two
    fields are independent, so both are checked."""
    return iso_alpha2 != INDIA_ISO_ALPHA2 and cbdt_country_code != INDIA_CBDT_CODE


def require_isin_shape(model: str, isin: str) -> None:
    """Require an ISO 6166 ISIN shape; ``model`` names the class in the error."""
    require(
        is_isin_shape(isin),
        what=(
            f"{model}.isin must match ISO 6166 (12 chars: 2 uppercase "
            "letters + 9 alphanumerics + 1 digit)"
        ),
        isin=isin,
    )


def require_no_control_character(model: str, field: str, value: str) -> None:
    """Refuse a control character in ``value`` (#818's rule for filed text).
    The error keeps only the keys of the character and its position, never
    the value, which may be an account number (see :mod:`ibkr_fa.pii`)."""
    found = first_control_character(value)
    if found is not None:
        index, char = found
        require(
            False,
            what=f"{model}.{field} must not contain a control character",
            character=repr(char),
            position=index + 1,
        )


def require_issuer_country_code(model: str, issuer_country_code: str) -> None:
    """Require an ISO 3166-1 alpha-2 issuer country; ``model`` names the class."""
    require(
        _COUNTRY_CODE_PATTERN.fullmatch(issuer_country_code) is not None,
        what=(
            f"{model}.issuer_country_code must be 2 uppercase ASCII letters (ISO 3166-1 alpha-2)"
        ),
        issuer_country_code=issuer_country_code,
    )


def require_lot_identifiers(
    model: str, *, isin: str, currency_field: str, currency: str, issuer_country_code: str
) -> None:
    """Require an ISO 6166 ISIN, ISO 4217 currency and ISO 3166-1 alpha-2 country.

    ``model`` and ``currency_field`` name the class and its currency
    attribute in the :class:`~ibkr_fa.exceptions.InvariantError`.
    """
    require_isin_shape(model, isin)
    require(
        _CURRENCY_PATTERN.fullmatch(currency) is not None,
        what=f"{model}.{currency_field} must be 3 uppercase ASCII letters (ISO 4217)",
        **{currency_field: currency},
    )
    require_issuer_country_code(model, issuer_country_code)
