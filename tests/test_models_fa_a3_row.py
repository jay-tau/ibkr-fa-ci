"""Tests for FaA3Row domain model."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import FaA3PeakBlankReason, FaA3Row
from ibkr_fa.reference.country_codes import (
    CountryCodeEntry,
    CountryCodeNotFoundError,
    load_country_codes,
)
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._crash_reports import assert_no_markers, assert_unchained_refusal
from tests._fa_a3_rows import make_fa_a3_row as make_row
from tests._robinhood_refusals import INDIA_REFUSAL


def test_open_at_year_end_row_populated_fields() -> None:
    row = make_row()
    assert row.country_code == "2"
    assert row.name_of_entity == "SAMPLE ISSUER AW INC"
    assert row.initial_value_inr == Decimal("32578")
    assert row.closing_value_inr == Decimal("41742")
    assert row.audit_disposal_date is None


def test_unresolved_portal_fields_accept_none() -> None:
    """Model-level: peak / gross-paid / gross-proceeds are all
    declared ``Decimal | None`` and the model accepts ``None`` for
    each (a blank peak also needs its reason). The pipeline populates
    gross-proceeds (issue #16) and
    gross-paid (issue #31, when input has dividends), so this test
    pins the MODEL contract, not the pipeline default."""
    row = make_row()
    assert row.peak_value_inr is None
    assert row.gross_amount_paid_credited_inr is None
    assert row.gross_proceeds_from_sale_inr is None


def test_blank_nature_is_valid_manual_fill_state() -> None:
    assert make_row(nature_of_entity="").nature_of_entity == ""


@pytest.mark.parametrize("nature", [" ", "x" * 35])
def test_invalid_nature_rejected(nature: str) -> None:
    with pytest.raises(InvariantError):
        make_row(nature_of_entity=nature)


def test_disposed_in_cy_lot_without_gross_credits_has_no_dec31_audit() -> None:
    """A disposed lot without gross credits has no Dec-31 audit field."""
    row = make_row(
        closing_value_inr=Decimal("0"),
        audit_close_price_usd=None,
        audit_ttbr_dec31=None,
        audit_disposal_date=date(2026, 6, 15),
    )
    assert row.closing_value_inr == Decimal("0")
    assert row.audit_close_price_usd is None
    assert row.audit_ttbr_dec31 is None
    assert row.audit_disposal_date == date(2026, 6, 15)


def test_fa_a3_row_is_frozen() -> None:
    row = make_row()
    with pytest.raises(AttributeError):
        row.initial_value_inr = Decimal("0")  # ty: ignore[invalid-assignment]


def test_fa_a3_row_uses_slots() -> None:
    row = make_row()
    assert not hasattr(row, "__dict__")


def test_dutch_issuer_listed_in_us_uses_nl_country_code() -> None:
    """Synthetic NL issuer: country_code reflects issuer (NL=31), not listing exchange (US=2)."""
    row = make_row(
        country_name="Netherlands",
        country_code="31",
        name_of_entity="SAMPLE ISSUER AD NV",
        source_isin=Isin("NL99971G1013"),
        source_ticker=Ticker("XQAD"),
        source_country_iso2=CountryCode("NL"),
    )
    assert row.country_code == "31"
    assert row.source_country_iso2 == "NL"
    assert row.name_of_entity == "SAMPLE ISSUER AD NV"


def test_initial_value_inr_holds_full_precision_decimal() -> None:
    """The field keeps full precision; :attr:`FaA3Row.filed` rounds it."""
    row = make_row(initial_value_inr=Decimal("32577.735"))
    assert row.initial_value_inr == Decimal("32577.735")
    assert row.filed.initial_value_inr == Decimal("32578")


# ============================================================================
# Construction-time invariants (PR #122, issue #116)
# ============================================================================


def test_invariant_empty_country_name_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(country_name="")
    assert "country_name must be non-empty" in exc.value.what


@pytest.mark.parametrize("bad_cbdt", ["", "US", "2a"])
def test_invariant_malformed_country_code_rejected(bad_cbdt: str) -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(country_code=bad_cbdt)
    assert "country_code must be a numeric CBDT" in exc.value.what


def test_invariant_empty_name_of_entity_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(name_of_entity="")
    assert "name_of_entity must be non-empty" in exc.value.what


def test_invariant_empty_source_ticker_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(source_ticker=Ticker(""))
    assert "source_ticker must be non-empty" in exc.value.what


def test_invariant_malformed_source_isin_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(source_isin=Isin("not-an-isin"))
    assert "source_isin must match ISO 6166" in exc.value.what


def test_invariant_malformed_source_country_iso2_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(source_country_iso2=CountryCode("USA"))
    assert "source_country_iso2" in exc.value.what


@pytest.mark.parametrize(
    ("field", "value", "what"),
    [
        ("source_isin", "BADISIN", "FaA3Row.source_isin must match ISO 6166"),
        # ``$`` also matches just before a final line break (#835).
        ("source_isin", "US99990E1038\n", "FaA3Row.source_isin must match ISO 6166"),
        ("source_country_iso2", "US\n", "FaA3Row.source_country_iso2 must be ISO 3166-1 alpha-2"),
        ("country_code", "2\n", "FaA3Row.country_code must be a numeric CBDT ITR-2 code"),
    ],
)
def test_invariant_malformed_identifier_error(field: str, value: str, what: str) -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(**{field: value})
    assert exc.value.what == what
    assert exc.value.context == ((field, None),)


# ---- One country identity (#1188) -------------------------------------------

_COUNTRY_RULE = (
    "InvariantError: FaA3Row.country_code must match the CBDT code for source_country_iso2"
)
# Distinct synthetic markers; no refusal below may print or chain them.
_MARKED = {
    "name_of_entity": "SYNTHROWNAME",
    "address_of_entity": "SYNTHROWADDRESS",
    "source_ticker": "SYNTHROWSOURCE",
}


def _assert_quiet(error: BaseException, capfd: pytest.CaptureFixture[str], *markers: str) -> None:
    """``error`` has no cause or context, and neither it nor the captured
    output holds any of ``markers``."""
    assert error.__cause__ is None
    assert error.__context__ is None
    captured = capfd.readouterr()
    for text in (str(error), repr(error), captured.out, captured.err):
        assert_no_markers(text, *markers)


@pytest.mark.parametrize(
    "overrides",
    [{}, {"initial_value_inr": Decimal("-100")}],
    ids=["mismatch", "mismatch-and-negative-initial-value"],
)
def test_country_code_must_match_the_source_country(
    overrides: dict[str, object], capfd: pytest.CaptureFixture[str]
) -> None:
    """A Netherlands source with the UK code (44) would review as
    ``31-NETHERLANDS`` but file ``44``. The country check fires before the
    amount-sign checks."""
    with pytest.raises(InvariantError) as exc:
        make_row(
            country_name="Netherlands",
            country_code="44",
            source_isin=Isin("NL99971G1013"),
            source_country_iso2=CountryCode("NL"),
            **_MARKED,
            **overrides,
        )
    assert type(exc.value) is InvariantError
    assert str(exc.value) == _COUNTRY_RULE
    assert exc.value.context == ()
    _assert_quiet(exc.value, capfd, *_MARKED.values())


@pytest.mark.parametrize(
    ("iso", "code", "name"),
    [
        ("NL", "31", "Netherlands"),
        ("NL", "31", "Holland"),
        ("NL", "31", " the netherlands "),
        ("US", "2", "United States"),
        ("US", "2", "USA"),
    ],
)
def test_matching_country_code_accepted_with_any_country_name(
    iso: str, code: str, name: str
) -> None:
    """Only the two codes are compared; the supplied name is kept as given."""
    row = make_row(country_name=name, country_code=code, source_country_iso2=CountryCode(iso))
    assert (row.country_name, row.country_code, row.source_country_iso2) == (name, code, iso)


@pytest.mark.parametrize("iso", ["nl", "N1", "NLD", "N", ""])
def test_malformed_source_country_keeps_its_shape_refusal(iso: str) -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(country_code="31", source_country_iso2=CountryCode(iso))
    assert exc.value.what == "FaA3Row.source_country_iso2 must be ISO 3166-1 alpha-2"
    assert exc.value.context == (("source_country_iso2", None),)


def test_unmapped_source_country_refused_without_echo(capfd: pytest.CaptureFixture[str]) -> None:
    """An unmapped ISO code raises the lookup's own error, with no chained
    ``KeyError`` holding the code."""
    with pytest.raises(CountryCodeNotFoundError) as exc:
        make_row(source_country_iso2=CountryCode("QQ"), **_MARKED)
    assert type(exc.value) is CountryCodeNotFoundError
    assert str(exc.value) == (
        "no CBDT country code mapped for a looked-up ISO alpha-2 code; add a row to "
        "src/ibkr_fa/reference/itr_country_codes.csv after verifying the code against "
        "the official ITR-2 utility (see the .source file there for the upstream URL)."
    )
    _assert_quiet(exc.value, capfd, "QQ", *_MARKED.values())


# ---- Not India (#1325) -------------------------------------------------------

_ISIN_MARKER = "US99971Q1013"


@pytest.mark.parametrize(
    "overrides",
    [{}, {"initial_value_inr": Decimal("-100")}],
    ids=["india", "india-and-negative-initial-value"],
)
def test_india_row_refused_without_echo(
    overrides: dict[str, object], capfd: pytest.CaptureFixture[str]
) -> None:
    """Table A3's country dropdown leaves India out, so a row whose code
    matches its ``IN`` source (91) refuses. It fires before the amount-sign
    checks, with a fixed message, no context and no chain."""
    with pytest.raises(InvariantError) as exc:
        make_row(
            country_name="India",
            country_code="91",
            source_country_iso2=CountryCode("IN"),
            source_isin=Isin(_ISIN_MARKER),
            **_MARKED,
            **overrides,
        )
    expected = InvariantError(what=INDIA_REFUSAL.removeprefix("InvariantError: "), context=())
    assert_unchained_refusal(
        exc.value, expected, INDIA_REFUSAL, markers=(*_MARKED.values(), _ISIN_MARKER)
    )
    _assert_quiet(exc.value, capfd, *_MARKED.values(), _ISIN_MARKER)


@pytest.mark.parametrize(("iso", "code"), [("US", "91"), ("IN", "2")])
def test_india_on_one_side_keeps_the_country_match_refusal(iso: str, code: str) -> None:
    """The match check runs first, so India's code with another source (or
    India's source with another code) keeps the #1188 refusal."""
    with pytest.raises(InvariantError) as exc:
        make_row(country_code=code, source_country_iso2=CountryCode(iso))
    assert str(exc.value) == _COUNTRY_RULE


@pytest.mark.parametrize(
    "entry",
    [entry for iso, entry in sorted(load_country_codes().items()) if iso != "IN"],
    ids=lambda entry: entry.iso_alpha2,
)
def test_every_other_vendored_country_builds_a_row(entry: CountryCodeEntry) -> None:
    """Only India refuses: every other country in the vendored table keeps its row."""
    row = make_row(
        country_name=entry.country_name,
        country_code=entry.cbdt_code,
        source_country_iso2=CountryCode(entry.iso_alpha2),
    )
    assert (row.country_code, row.source_country_iso2) == (entry.cbdt_code, entry.iso_alpha2)


# ---- Raw text within the CBDT schemas' maximum lengths (#1189) ---------------

# Each limited field, its limit, a distinct synthetic marker and its refusal.
_TEXT_LIMITS = [
    pytest.param(
        "country_name",
        55,
        "SYNTHLENCOUNTRY",
        "InvariantError: FaA3Row.country_name must be at most 55 characters",
        id="country_name",
    ),
    pytest.param(
        "name_of_entity",
        125,
        "SYNTHLENNAME",
        "InvariantError: FaA3Row.name_of_entity must be at most 125 characters",
        id="name_of_entity",
    ),
    pytest.param(
        "address_of_entity",
        200,
        "SYNTHLENADDRESS",
        "InvariantError: FaA3Row.address_of_entity must be at most 200 characters",
        id="address_of_entity",
    ),
    pytest.param(
        "zip_code",
        8,
        "SYNTHZIP",
        "InvariantError: FaA3Row.zip_code must be at most 8 characters",
        id="zip_code",
    ),
]


@pytest.mark.parametrize(("field", "limit", "marker", "message"), _TEXT_LIMITS)
def test_text_at_its_maximum_length_accepted(
    field: str, limit: int, marker: str, message: str
) -> None:
    value = marker.ljust(limit, "X")
    assert getattr(make_row(**{field: value}), field) == value


@pytest.mark.parametrize(("field", "limit", "marker", "message"), _TEXT_LIMITS)
def test_text_over_its_maximum_length_refused(
    field: str, limit: int, marker: str, message: str, capfd: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(**{field: marker.ljust(limit + 1, "X")})
    assert type(exc.value) is InvariantError
    assert str(exc.value) == message
    assert exc.value.context == ()
    _assert_quiet(exc.value, capfd, marker)


def test_zip_plus_four_refused() -> None:
    """A US ZIP+4 with its dash is 10 characters."""
    with pytest.raises(InvariantError) as exc:
        make_row(zip_code="12345-6789")
    assert str(exc.value) == "InvariantError: FaA3Row.zip_code must be at most 8 characters"


def test_raw_length_counts_whitespace_the_filing_csv_collapses() -> None:
    """Intentional tightening: the filing CSV rendered this 127-character name
    as ``B C`` while the CBDT JSON refused it; the row now refuses it."""
    with pytest.raises(InvariantError) as exc:
        make_row(name_of_entity="B" + " " * 125 + "C")
    assert str(exc.value) == (
        "InvariantError: FaA3Row.name_of_entity must be at most 125 characters"
    )


@pytest.mark.parametrize("field", ["address_of_entity", "zip_code", "nature_of_entity"])
def test_blank_manual_fill_text_accepted(field: str) -> None:
    assert getattr(make_row(**{field: ""}), field) == ""


# ---- Text the upload file would turn blank (#1329) --------------------------

# Each text field the bulk-upload file renders, with its #1189 length limit.
_UPLOAD_TEXT = [
    pytest.param("name_of_entity", 125, id="name_of_entity"),
    pytest.param("address_of_entity", 200, id="address_of_entity"),
    pytest.param("zip_code", 8, id="zip_code"),
    pytest.param("nature_of_entity", 34, id="nature_of_entity"),
]
_COMMA_ONLY = [",", ", ,", " ,", ",\t,\n"]


@pytest.mark.parametrize("value", _COMMA_ONLY)
@pytest.mark.parametrize(("field", "limit"), _UPLOAD_TEXT)
def test_comma_only_text_refused(
    field: str, limit: int, value: str, capfd: pytest.CaptureFixture[str]
) -> None:
    """The upload file would write a blank cell for it, so the row refuses it,
    naming only the field."""
    marked = {name: v for name, v in _MARKED.items() if name != field}
    with pytest.raises(InvariantError) as exc:
        make_row(**{field: value}, **marked)
    assert type(exc.value) is InvariantError
    assert str(exc.value) == (
        f"InvariantError: FaA3Row.{field} must contain a character other than a comma or a space"
    )
    assert exc.value.context == ()
    assert exc.value.args == ()
    _assert_quiet(exc.value, capfd, *marked.values())


@pytest.mark.parametrize("value", ["a,", ", a ,", "QZCOMMA,"])
@pytest.mark.parametrize(("field", "limit"), _UPLOAD_TEXT)
def test_text_with_another_character_accepted(field: str, limit: int, value: str) -> None:
    assert getattr(make_row(**{field: value}), field) == value


@pytest.mark.parametrize(("field", "limit"), _UPLOAD_TEXT[:3])
def test_whitespace_only_text_keeps_its_handling(field: str, limit: int) -> None:
    """The check leaves an empty or whitespace-only value to the row's older rules.
    Spaces only: a tab is a control character, which the row refuses (#1367)."""
    assert getattr(make_row(**{field: "  "}), field) == "  "


def test_whitespace_only_nature_keeps_its_refusal() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(nature_of_entity=" \t")
    assert exc.value.what == (
        "FaA3Row.nature_of_entity must be empty or contain non-whitespace text"
    )


@pytest.mark.parametrize(("field", "limit"), _UPLOAD_TEXT)
def test_comma_only_text_over_its_limit_keeps_the_length_refusal(field: str, limit: int) -> None:
    """#1189's length refusal fires first, with its own message."""
    with pytest.raises(InvariantError) as exc:
        make_row(**{field: "," * (limit + 1)})
    assert type(exc.value) is InvariantError
    assert exc.value.what == f"FaA3Row.{field} must be at most {limit} characters"


def test_comma_only_text_keeps_every_earlier_refusal() -> None:
    """The check runs last: a row that already refused keeps its message."""
    with pytest.raises(InvariantError) as exc:
        make_row(address_of_entity=",", initial_value_inr=Decimal("-100"))
    assert exc.value.what == "FaA3Row.initial_value_inr must be non-negative"


# ---- Control characters (#1367) ---------------------------------------------

# NUL, the characters the upload file turned into a space (tab, LF, CR, U+001F)
# and ones it copied as they were (U+0001, U+001B, DEL).
_CONTROL = [
    pytest.param(char, id=f"U+{ord(char):04X}")
    for char in ("\x00", "\x01", "\t", "\n", "\r", "\x1b", "\x1f", "\x7f")
]
_FIELD_ORDER = ("name_of_entity", "address_of_entity", "zip_code", "nature_of_entity")
# A digit-run privacy marker; no refusal below may print or chain it.
_DIGITS = "473812"


def _control_refusal(field: str, position: int) -> str:
    return (
        f"FaA3Row.{field} has a control character at position {position}; "
        "filed text may not contain control characters"
    )


@pytest.mark.parametrize(
    ("template", "position"),
    [("{c}Q{m}", 1), ("Q{c}{m}", 2), ("Q{m}{c}", 8)],
    ids=["first", "middle", "last"],
)
@pytest.mark.parametrize("char", _CONTROL)
@pytest.mark.parametrize(("field", "limit"), _UPLOAD_TEXT)
def test_control_character_refused(
    field: str,
    limit: int,
    char: str,
    template: str,
    position: int,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Named by the field and the 1-based position, never by the value or the
    character, with no context, args or chain."""
    with pytest.raises(InvariantError) as exc:
        make_row(**{field: template.format(c=char, m=_DIGITS)})
    what = _control_refusal(field, position)
    assert_unchained_refusal(
        exc.value,
        InvariantError(what=what, context=()),
        f"InvariantError: {what}",
        markers=(_DIGITS,),
    )
    assert exc.value.args == ()
    _assert_quiet(exc.value, capfd, _DIGITS)


@pytest.mark.parametrize(("field", "limit"), _UPLOAD_TEXT)
def test_control_character_over_its_limit_keeps_the_length_refusal(field: str, limit: int) -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(**{field: "\x01" + "X" * limit})
    assert exc.value.what == f"FaA3Row.{field} must be at most {limit} characters"


@pytest.mark.parametrize(("field", "limit"), _UPLOAD_TEXT)
def test_commas_around_a_tab_keep_the_upload_text_refusal(field: str, limit: int) -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(**{field: ",\t,"})
    assert exc.value.what == (
        f"FaA3Row.{field} must contain a character other than a comma or a space"
    )


def test_control_character_keeps_every_earlier_refusal() -> None:
    """The check runs after every other: a row that already refused keeps its
    message."""
    with pytest.raises(InvariantError) as exc:
        make_row(name_of_entity="SAMPLE\x01ISSUER", initial_value_inr=Decimal("-100"))
    assert exc.value.what == "FaA3Row.initial_value_inr must be non-negative"


@pytest.mark.parametrize(
    ("first", "second"),
    list(itertools.combinations(_FIELD_ORDER, 2)),
    ids=lambda field: field.removesuffix("_of_entity"),
)
def test_first_field_in_order_refuses(first: str, second: str) -> None:
    """Name, address, ZIP, nature: the first with a control character refuses."""
    with pytest.raises(InvariantError) as exc:
        make_row(**{first: "QQ\x01Q", second: "Q\x01QQ"})
    assert exc.value.what == _control_refusal(first, 3)


def test_first_control_character_in_a_field_is_named() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(name_of_entity="SAMPLE\x1fISSUER\x01INC")
    assert exc.value.what == _control_refusal("name_of_entity", 7)


def test_whitespace_only_name_with_a_tab_refused() -> None:
    """A whitespace-only name is a manual-fill blank (#1369), but a tab is a
    control character (#1367): the name refuses rather than become a blank
    upload cell."""
    with pytest.raises(InvariantError) as exc:
        make_row(name_of_entity=" \t")
    assert exc.value.what == _control_refusal("name_of_entity", 2)


@pytest.mark.parametrize(
    "value", ["Q\x85Q", "Q, Q", "Q\u00c9Q"], ids=["U+0085", "comma", "non-ascii-letter"]
)
@pytest.mark.parametrize(("field", "limit"), _UPLOAD_TEXT)
def test_text_without_a_control_character_accepted(field: str, limit: int, value: str) -> None:
    """A C1 character such as U+0085 isn't one: the upload file's ASCII check
    refuses it instead."""
    assert getattr(make_row(**{field: value}), field) == value


def test_invariant_negative_initial_value_inr_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(initial_value_inr=Decimal("-100"))
    assert "initial_value_inr must be non-negative" in exc.value.what


def test_invariant_negative_closing_value_inr_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(closing_value_inr=Decimal("-100"))
    assert "closing_value_inr must be non-negative" in exc.value.what


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("peak_value_inr", Decimal("-1")),
        ("gross_amount_paid_credited_inr", Decimal("-1")),
        ("gross_proceeds_from_sale_inr", Decimal("-1")),
    ],
)
def test_invariant_negative_optional_inr_fields_rejected(field: str, bad_value: Decimal) -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(**{field: bad_value})  # type: ignore[arg-type]
    assert field in exc.value.what
    assert "non-negative" in exc.value.what


def test_invariant_negative_cost_basis_usd_rejected() -> None:
    """Negative audit_cost_basis_usd is rejected, but zero is
    allowed (corporate-action lots propagate zero cost basis from
    OpenLot/ClosedLot through to FA-A3 — Codex P2 on PR #122)."""
    with pytest.raises(InvariantError) as exc:
        make_row(audit_cost_basis_usd=Decimal("-1"))
    assert "audit_cost_basis_usd must be non-negative" in exc.value.what


def test_invariant_zero_cost_basis_usd_accepted() -> None:
    """Zero-cost lots reach FA-A3 unchanged from the lot models;
    requiring strict positivity here would block corporate-action
    cases the upstream models legitimately allow."""
    row = make_row(audit_cost_basis_usd=Decimal("0"), initial_value_inr=Decimal("0"))
    assert row.audit_cost_basis_usd == Decimal("0")


def test_invariant_non_positive_ttbr_acq_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(audit_ttbr_acquisition=Decimal("0"))
    assert "audit_ttbr_acquisition must be positive" in exc.value.what


def test_invariant_negative_close_price_usd_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(audit_close_price_usd=Decimal("-1"))
    assert "audit_close_price_usd" in exc.value.what


def test_invariant_non_positive_ttbr_dec31_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(audit_ttbr_dec31=Decimal("0"))
    assert "audit_ttbr_dec31" in exc.value.what


def test_invariant_disposed_lot_requires_zero_closing_value() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(
            audit_disposal_date=date(2025, 6, 15),
            audit_close_price_usd=None,
            audit_ttbr_dec31=None,
            # closing_value_inr default is non-zero -- expected to fail
        )
    assert "closing_value_inr == 0" in exc.value.what


def test_invariant_disposed_lot_requires_no_close_price() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(
            audit_disposal_date=date(2025, 6, 15),
            closing_value_inr=Decimal("0"),
            audit_ttbr_dec31=None,
            # audit_close_price_usd default Decimal('32.70') -- expected to fail
        )
    assert "audit_close_price_usd is None" in exc.value.what


def test_invariant_disposed_lot_without_gross_credits_requires_no_dec31_ttbr() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(
            audit_disposal_date=date(2025, 6, 15),
            closing_value_inr=Decimal("0"),
            audit_close_price_usd=None,
            # audit_ttbr_dec31 default Decimal('85.10') -- expected to fail
        )
    assert "audit_ttbr_dec31" in exc.value.what


def test_disposed_lot_income_records_dec31_ttbr() -> None:
    row = make_row(
        audit_disposal_date=date(2025, 6, 15),
        closing_value_inr=Decimal("0"),
        gross_amount_paid_credited_inr=Decimal("100"),
        audit_close_price_usd=None,
        audit_ttbr_dec31=Decimal("85.10"),
    )
    assert row.audit_ttbr_dec31 == Decimal("85.10")


def test_disposed_lot_proceeds_record_dec31_ttbr() -> None:
    row = make_row(
        audit_disposal_date=date(2025, 6, 15),
        closing_value_inr=Decimal("0"),
        gross_proceeds_from_sale_inr=Decimal("100"),
        audit_close_price_usd=None,
        audit_ttbr_dec31=Decimal("85.10"),
    )
    assert row.audit_ttbr_dec31 == Decimal("85.10")


def test_invariant_open_lot_requires_close_price() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(audit_close_price_usd=None)
    assert "must have audit_close_price_usd populated" in exc.value.what


def test_invariant_open_lot_requires_dec31_ttbr() -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(audit_ttbr_dec31=None)
    assert "must have audit_ttbr_dec31 populated" in exc.value.what


@pytest.mark.parametrize(
    ("peak", "reason"),
    [
        (None, None),
        (Decimal("80000"), FaA3PeakBlankReason.SPLIT),
        (None, "split"),
    ],
    ids=["blank-without-reason", "filled-with-reason", "raw-string-reason"],
)
def test_invariant_peak_blank_reason_pairs_with_a_blank_peak(
    peak: Decimal | None, reason: object
) -> None:
    """#962: a blank peak needs a FaA3PeakBlankReason member, and only a blank one."""
    with pytest.raises(InvariantError) as exc:
        make_row(peak_value_inr=peak, peak_blank_reason=reason)
    assert "FaA3Row.peak_blank_reason" in str(exc.value)


def test_invariant_acquisition_date_mismatch_rejected() -> None:
    """The audit_acquisition_date exists to preserve the same value
    distinctly; constructing with a different value is a parser bug
    and must reject."""
    # The factory ties date_acq to both fields; to break it we have
    # to construct FaA3Row directly.
    with pytest.raises(InvariantError) as exc:
        FaA3Row(
            country_name="United States",
            country_code="2",
            name_of_entity="XQAW",
            address_of_entity="USA",
            zip_code="",
            nature_of_entity="Listed Company",
            date_of_acquiring=date(2025, 1, 15),
            initial_value_inr=Decimal("1000"),
            peak_value_inr=None,
            closing_value_inr=Decimal("1100"),
            gross_amount_paid_credited_inr=None,
            gross_proceeds_from_sale_inr=None,
            source_isin=Isin("US99990E1038"),
            source_ticker=Ticker("XQAW"),
            source_country_iso2=CountryCode("US"),
            audit_cost_basis_usd=Decimal("100"),
            audit_acquisition_date=date(2025, 6, 30),  # mismatched!
            audit_close_price_usd=Decimal("11"),
            audit_ttbr_acquisition=Decimal("85"),
            audit_ttbr_dec31=Decimal("85"),
            audit_disposal_date=None,
            peak_blank_reason=FaA3PeakBlankReason.PRICES_NOT_FETCHED,
        )
    assert "date_of_acquiring and audit_acquisition_date must match" in exc.value.what


# ---- field types and the filed-amount bound (#788) --------------------------

_FILED_AMOUNTS = [
    "initial_value_inr",
    "peak_value_inr",
    "closing_value_inr",
    "gross_amount_paid_credited_inr",
    "gross_proceeds_from_sale_inr",
]


@pytest.mark.parametrize("field", _FILED_AMOUNTS)
@pytest.mark.parametrize("value", ["99999999999999", "99999999999999.4"])
def test_filed_amount_rounding_within_the_cbdt_bound_accepted(field: str, value: str) -> None:
    assert getattr(make_row(**{field: Decimal(value)}), field) == Decimal(value)


@pytest.mark.parametrize("field", _FILED_AMOUNTS)
@pytest.mark.parametrize("value", ["99999999999999.5", "100000000000000", "1E100"])
def test_filed_amount_rounding_beyond_the_cbdt_bound_rejected(field: str, value: str) -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(**{field: Decimal(value)})
    assert exc.value.what.startswith(f"FaA3Row.{field} must round to a whole-rupee amount")


@pytest.mark.parametrize("field", _FILED_AMOUNTS)
@pytest.mark.parametrize(
    ("value", "filed"),
    [("64217.50", "64218"), ("64217.49", "64217"), ("-0.00", "0"), ("0.4", "0"), ("5E+2", "500")],
)
def test_filed_value_is_the_field_rounded_half_up_to_whole_rupees(
    field: str, value: str, filed: str
) -> None:
    """``filed`` rounds HALF_UP to whole rupees, never giving ``-0`` or an
    exponent form, and keeps the field's full precision on the row."""
    row = make_row(**{field: Decimal(value)})
    assert getattr(row, field).compare_total(Decimal(value)) == 0
    assert getattr(row.filed, field).compare_total(Decimal(filed)) == 0


@pytest.mark.parametrize(
    "field", ["peak_value_inr", "gross_amount_paid_credited_inr", "gross_proceeds_from_sale_inr"]
)
def test_filed_value_of_a_blank_field_is_none(field: str) -> None:
    assert getattr(make_row(**{field: None}).filed, field) is None


def test_filed_values_are_frozen() -> None:
    filed = make_row().filed
    with pytest.raises(AttributeError):
        filed.initial_value_inr = Decimal("1")  # ty: ignore[invalid-assignment]


def test_audit_amount_beyond_the_cbdt_bound_accepted() -> None:
    row = make_row(audit_cost_basis_usd=Decimal("1E100"), audit_close_price_usd=Decimal("1E100"))
    assert row.audit_cost_basis_usd == Decimal("1E100")


def _acquired(value: object) -> dict[str, object]:
    """Both acquisition-date fields set to ``value``, so they still agree."""
    return {"date_of_acquiring": value, "audit_acquisition_date": value}


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"zip_code": 99991}, "zip_code"),
        ({"initial_value_inr": 32578}, "initial_value_inr"),
        ({"initial_value_inr": Decimal("NaN")}, "initial_value_inr"),
        ({"initial_value_inr": Decimal("sNaN")}, "initial_value_inr"),
        ({"initial_value_inr": Decimal("Infinity")}, "initial_value_inr"),
        ({"closing_value_inr": None}, "closing_value_inr"),
        ({"audit_ttbr_acquisition": 84}, "audit_ttbr_acquisition"),
        ({"audit_cost_basis_usd": Decimal("NaN")}, "audit_cost_basis_usd"),
        (_acquired("2026-03-18"), "date_of_acquiring"),
        (_acquired(datetime(2026, 3, 18)), "date_of_acquiring"),
        (_acquired(datetime(2026, 3, 18, 12, 34, 56)), "date_of_acquiring"),
        (_acquired(datetime(2026, 3, 18, tzinfo=UTC)), "date_of_acquiring"),
    ],
    ids=[
        "int-zip",
        "int-filed-amount",
        "nan-filed-amount",
        "snan-filed-amount",
        "infinite-filed-amount",
        "none-closing-value",
        "int-audit-amount",
        "nan-audit-amount",
        "str-date",
        "midnight-datetime",
        "datetime",
        "aware-datetime",
    ],
)
def test_wrong_field_type_rejected_naming_the_field(
    overrides: dict[str, object], field: str
) -> None:
    with pytest.raises(InvariantError) as exc:
        make_row(**overrides)
    assert exc.value.what.startswith(f"FaA3Row.{field} must be")
