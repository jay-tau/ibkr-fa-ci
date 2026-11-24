"""Tests for :mod:`ibkr_fa.reference.country_codes`."""

from __future__ import annotations

import csv as csvmod
from importlib import resources

import pytest

import ibkr_fa.reference.country_codes as cc
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.reference.country_codes import (
    CountryCodeEntry,
    CountryCodeNotFoundError,
    load_country_codes,
    lookup_country,
)
from tests._crash_reports import assert_prints_no_marker


def test_load_country_codes_returns_dict_keyed_by_iso() -> None:
    table = load_country_codes()
    assert isinstance(table, dict)
    assert "US" in table
    entry = table["US"]
    assert isinstance(entry, CountryCodeEntry)


def test_us_row_matches_portal_authoritative_source() -> None:
    """Code 2 = United States of America. Verified against the
    official ITR-2 utility v1.0, sheet DropDownValues."""
    entry = lookup_country("US")
    assert entry.iso_alpha2 == "US"
    assert entry.cbdt_code == "2"
    assert entry.country_name == "United States of America"
    assert entry.country_name_portal == "UNITED STATES OF AMERICA"


def test_regression_uk_is_44_not_1() -> None:
    """Codex / portal-research catch: an earlier version of this CSV
    listed UK as code 1, but the portal dropdown shows '1-CANADA'.
    UK is '44-UNITED KINGDOM...'. Regression-guard the right
    mapping."""
    entry = lookup_country("GB")
    assert entry.cbdt_code == "44"
    assert "KINGDOM" in entry.country_name_portal


def test_regression_canada_is_1() -> None:
    """The portal's code 1 maps to Canada (not UK, as a stray
    third-party blog had claimed)."""
    entry = lookup_country("CA")
    assert entry.cbdt_code == "1"
    assert entry.country_name_portal == "CANADA"


def test_regression_netherlands_is_31_not_10() -> None:
    """An earlier version of this CSV had Netherlands = 10, but the
    portal dropdown has no code 10 at all. Netherlands is 31."""
    entry = lookup_country("NL")
    assert entry.cbdt_code == "31"
    assert entry.country_name_portal == "NETHERLANDS"


def test_portal_display_combines_code_and_name() -> None:
    entry = lookup_country("US")
    assert entry.portal_display == "2-UNITED STATES OF AMERICA"


def test_portal_display_for_uk() -> None:
    entry = lookup_country("GB")
    assert entry.portal_display == "44-UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND"


@pytest.mark.parametrize(
    "looked_up", ["ZZ", "Synthetic Holder Two", "QZ731942"], ids=["code", "holder-name", "marker"]
)
def test_lookup_unknown_iso_raises_with_actionable_message(looked_up: str) -> None:
    """A missing lookup is a tax-filing error in the making, so the
    parser raises rather than returning None. The error names the CSV
    to edit, but neither prints nor keeps the missing code: the code can
    come from a statement cell (#1047, #1368). Nor does it chain a
    ``KeyError`` holding the code, even as suppressed context (#1188)."""
    with pytest.raises(CountryCodeNotFoundError) as exc:
        lookup_country(looked_up)
    assert str(exc.value) == (
        "no CBDT country code mapped for a looked-up ISO alpha-2 code; add a row to "
        "src/ibkr_fa/reference/itr_country_codes.csv after verifying the code against "
        "the official ITR-2 utility (see the .source file there for the upstream URL)."
    )
    assert_prints_no_marker(exc.value, markers=(looked_up,))
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_load_country_codes_is_cached() -> None:
    """Cached: repeated calls return the same dict object."""
    a = load_country_codes()
    b = load_country_codes()
    assert a is b


def test_csv_has_expected_minimum_country_set() -> None:
    """Regression guard: every IBKR-common ISO alpha-2 we want to
    support must be present. If someone trims the CSV, this test
    will name the missing entry."""
    table = load_country_codes()
    expected = {
        "AU",
        "BM",
        "CA",
        "CH",
        "DE",
        "FR",
        "GB",
        "HK",
        "IE",
        "JP",
        "NL",
        "SG",
        "US",
    }
    missing = expected - table.keys()
    assert not missing, f"missing countries in CSV: {missing}"


def test_country_name_portal_is_uppercase() -> None:
    """Every row's portal display name should be UPPERCASE — that's
    how the portal dropdown stores them, and case mismatch would
    cause the portal to reject the value."""
    table = load_country_codes()
    for entry in table.values():
        assert entry.country_name_portal == entry.country_name_portal.upper(), (
            f"{entry.iso_alpha2}: '{entry.country_name_portal}' is not uppercase"
        )


def test_country_name_is_title_or_mixed_case() -> None:
    """Audit column should be in human-readable case, not SHOUTY
    uppercase like the portal column."""
    table = load_country_codes()
    for entry in table.values():
        # Allow rows where country_name == country_name_portal IF the
        # name is short enough that the display difference doesn't matter
        # (no rows in v1 hit this), but generally enforce mixed case.
        assert entry.country_name != entry.country_name.upper() or len(entry.country_name) <= 4, (
            f"{entry.iso_alpha2}: country_name '{entry.country_name}' should be mixed case"
        )


def test_load_rejects_unexpected_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: if someone changes the CSV schema without updating
    the loader, the loader must fail loudly with a pointer to fix
    both sides together — NOT silently parse the wrong columns."""

    # Bypass the cache so the test sees the patched file content.
    load_country_codes.cache_clear()
    bad_csv = "wrong,header,format,here\nUS,2,United States,UNITED STATES\n"

    class _FakeFile:
        def read_text(self, encoding: str) -> str:
            return bad_csv

    class _FakeFiles:
        def joinpath(self, _name: str) -> _FakeFile:
            return _FakeFile()

    def fake_files(_pkg: str) -> _FakeFiles:
        return _FakeFiles()

    monkeypatch.setattr(resources, "files", fake_files)
    try:
        with pytest.raises(InvariantError, match="unexpected header"):
            cc.load_country_codes()
    finally:
        load_country_codes.cache_clear()


def test_load_skips_blank_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank lines mid-CSV must not blow up the loader."""

    load_country_codes.cache_clear()
    csv_with_blanks = (
        "iso_alpha2,cbdt_code,country_name,country_name_portal\n"
        "US,2,United States of America,UNITED STATES OF AMERICA\n"
        "\n"
        "GB,44,United Kingdom,UNITED KINGDOM\n"
    )

    class _FakeFile:
        def read_text(self, encoding: str) -> str:
            return csv_with_blanks

    class _FakeFiles:
        def joinpath(self, _name: str) -> _FakeFile:
            return _FakeFile()

    monkeypatch.setattr(resources, "files", lambda _pkg: _FakeFiles())
    try:
        table = cc.load_country_codes()
        assert set(table) == {"US", "GB"}
    finally:
        load_country_codes.cache_clear()


_HEADER = "iso_alpha2,cbdt_code,country_name,country_name_portal\n"


def _load_refusal(monkeypatch: pytest.MonkeyPatch, row: str) -> InvariantError:
    """Load a one-row table through a patched ``resources.files``."""

    class _FakeFile:
        def read_text(self, encoding: str) -> str:
            return _HEADER + row + "\n"

    class _FakeFiles:
        def joinpath(self, _name: str) -> _FakeFile:
            return _FakeFile()

    load_country_codes.cache_clear()
    monkeypatch.setattr(resources, "files", lambda _pkg: _FakeFiles())
    try:
        with pytest.raises(InvariantError) as excinfo:
            cc.load_country_codes()
    finally:
        load_country_codes.cache_clear()
    return excinfo.value


@pytest.mark.parametrize(
    ("row", "what"),
    [
        ('"U\nS",2,United States,UNITED STATES', "iso_alpha2 must be printable"),
        ('"U"S,2,United States,UNITED STATES', "malformed CSV"),
        ('US,"2\n",United States,UNITED STATES', "cbdt_code must be printable"),
        (
            "US,02,United States,UNITED STATES",
            "cbdt_code must be ASCII digits with no leading zero",
        ),
        ("US,2,United States,UNITED STATES,extra", "exactly 4 columns"),
        ("US,2,United States", "exactly 4 columns"),
    ],
)
def test_load_refuses_a_row_that_is_not_four_whole_cells(
    monkeypatch: pytest.MonkeyPatch, row: str, what: str
) -> None:
    """Strict parsing and whole-cell checks (#945): the old loader joined
    a quoted multi-line cell, read ``"U"S`` as ``US``, accepted ``02``
    and crashed on a short or long row with a plain ``ValueError``."""
    err = _load_refusal(monkeypatch, row)
    assert what in err.what
    assert "itr_country_codes.csv" in err.what


# ============================================================================
# Load-time integrity invariants (PR #125, issue #119)
# ============================================================================


def test_country_codes_table_passes_load_time_invariants() -> None:
    """Smoke check that the bundled itr_country_codes.csv satisfies
    all the load-time invariants (well-formed ISO alpha-2 keys,
    numeric CBDT codes, non-empty names, no duplicates). The
    invariants fire inside load_country_codes(); this test just
    confirms the happy path on real data."""
    table = load_country_codes()
    assert len(table) > 0
    for iso, entry in table.items():
        assert len(iso) == 2 and iso.isupper() and iso.isalpha()
        assert entry.cbdt_code.isdigit()
        assert entry.country_name != ""
        assert entry.country_name_portal != ""


# ---- #74: full CBDT annexure ---------------------------------------------


def test_full_annexure_loaded() -> None:
    """The bundled CSV ships the full CBDT ITR-2 country annexure.

    The portal workbook ships three country named ranges with
    slightly different coverage: ``Country`` (BU, 247 entries),
    ``Country_NoIndia`` (BW, used in some forms; adds Zambia and
    Western Sahara that BU misses), and ``Country_Juridiction``
    (BY, used in jurisdiction selectors). The vendored CSV is the
    **union** of all three (249 distinct countries) so any IBKR
    holding from any CBDT-recognised country resolves at lookup
    time without raising :class:`CountryCodeNotFoundError`.
    Closes #74.
    """
    table = load_country_codes()
    assert len(table) == 249


@pytest.mark.parametrize(
    ("iso", "cbdt_code", "name"),
    [
        ("DE", "49", "Germany"),
        ("JP", "81", "Japan"),
        ("AU", "61", "Australia"),
        ("BR", "55", "Brazil"),
        ("SG", "65", "Singapore"),
        ("CH", "41", "Switzerland"),
        ("AE", "971", "United Arab Emirates"),
        ("SA", "966", "Saudi Arabia"),
        ("ZA", "28", "South Africa"),
        ("MX", "52", "Mexico"),
        ("ID", "62", "Indonesia"),
        ("TH", "66", "Thailand"),
        ("MY", "60", "Malaysia"),
        ("KR", "82", "South Korea"),
        ("KP", "850", "North Korea"),
        ("VG", "1284", "British Virgin Islands"),
        ("VI", "1340", "U.S. Virgin Islands"),
        ("SZ", "268", "Eswatini"),
        ("MK", "389", "North Macedonia"),
        ("LA", "856", "Laos"),
        ("TL", "670", "Timor-Leste"),
        ("VA", "6", "Vatican City"),
        ("AX", "1001", "Åland Islands"),
    ],
)
def test_full_annexure_spot_checks(iso: str, cbdt_code: str, name: str) -> None:
    """Spot-check that representative additions from the full
    annexure resolve correctly: at least one row from each major
    region (Europe, Asia-Pacific, Middle East, Africa, Latin
    America, IBKR-relevant tax-haven jurisdictions), plus several
    where the CBDT portal form differs from the ISO 3166-1 current
    friendly name (Eswatini vs SWAZILAND, North Macedonia vs the
    Former Yugoslav formulation, Laos vs LAO PEOPLES DEMOCRATIC
    REPUBLIC, etc.).
    """
    entry = lookup_country(iso)
    assert entry.cbdt_code == cbdt_code
    assert entry.country_name == name


def test_full_annexure_no_iso_or_cbdt_collisions() -> None:
    """Both the ISO alpha-2 keys and the CBDT numeric codes must be
    1-to-1 with their country rows. A duplicate CBDT code would
    indicate either a CBDT data error or a bug in the
    portal-dropdown extraction script.
    """

    with (
        resources.files("ibkr_fa.reference")
        .joinpath("itr_country_codes.csv")
        .open(encoding="utf-8") as f
    ):
        reader = csvmod.DictReader(f)
        rows = list(reader)
    iso_keys = [r["iso_alpha2"] for r in rows]
    cbdt_keys = [r["cbdt_code"] for r in rows]
    assert len(iso_keys) == len(set(iso_keys)), "duplicate ISO alpha-2 keys"
    assert len(cbdt_keys) == len(set(cbdt_keys)), "duplicate CBDT codes"


@pytest.mark.parametrize(
    ("iso", "portal_name"),
    [
        # Politically-outdated names — ISO renamed but CBDT still uses legacy form.
        ("SZ", "SWAZILAND"),  # ISO 2018: Eswatini
        ("TR", "TURKEY"),  # ISO 2022: Türkiye
        ("MK", "MACEDONIA (THE FORMER YUGOSLAV REPUBLIC OF)"),  # ISO: North Macedonia
        # Parenthesised long-form (CBDT's UN-style).
        ("KR", "KOREA (REPUBLIC OF)"),
        ("KP", "KOREA (DEMOCRATIC PEOPLES REPUBLIC OF)"),
        ("VG", "VIRGIN ISLANDS (BRITISH)"),
        ("VI", "VIRGIN ISLANDS (U.S.)"),
        ("BO", "BOLIVIA (PLURINATIONAL STATE OF)"),
        ("VE", "VENEZUELA (BOLIVARIAN REPUBLIC OF)"),
        ("IR", "IRAN (ISLAMIC REPUBLIC OF)"),
        ("CD", "CONGO (DEMOCRATIC REPUBLIC OF THE)"),
        ("FM", "MICRONESIA (FEDERATED STATES OF)"),
        # Short-form synonym.
        ("VA", "HOLY SEE"),
        # Accent-stripped portal names.
        ("CI", "COTE DIVOIRE"),
        ("RE", "REUNION"),
        ("BL", "SAINT BARTHELEMY"),
        ("CW", "CURACAO"),
        ("AX", "ALAND ISLANDS"),
        # Punctuation quirk: CBDT writes "TIMOR-LESTE(EAST TIMOR)" with
        # no space before the opening parenthesis.
        ("TL", "TIMOR-LESTE(EAST TIMOR)"),
        # Politically-outdated single word (LAO PEOPLES DEMOCRATIC REPUBLIC).
        ("LA", "LAO PEOPLES DEMOCRATIC REPUBLIC"),
    ],
)
def test_override_map_portal_names_exact(iso: str, portal_name: str) -> None:
    """The ``country_name_portal`` for every entry in the manual
    ISO-override map MUST match the verbatim CBDT portal string
    byte-for-byte. The portal-facing country cell is built as
    ``f'{cbdt_code}-{country_name_portal}'``; any drift here
    silently files a value the portal dropdown won't accept and
    the e-filing submission gets rejected with no actionable
    error trail back to ibkr-fa.

    These rows are the highest-risk for accidental drift: the
    friendly :attr:`country_name` deliberately differs from
    :attr:`country_name_portal` (Eswatini vs SWAZILAND, North
    Macedonia vs the Former Yugoslav formulation, etc.), so a
    future maintainer who edits only the friendly name would
    not notice the portal-side drift until a real user files.
    (T4-r1 finding #3.)
    """
    entry = lookup_country(iso)
    assert entry.country_name_portal == portal_name


def test_full_annexure_baseline_entries_preserved() -> None:
    """Backwards-compat: the 26 entries that were vendored before
    the #74 expansion must be byte-for-byte preserved (same
    cbdt_code, same country_name, same country_name_portal).
    """
    baseline = {
        "AU": ("61", "Australia", "AUSTRALIA"),
        "BM": ("1441", "Bermuda", "BERMUDA"),
        "CA": ("1", "Canada", "CANADA"),
        "CH": ("41", "Switzerland", "SWITZERLAND"),
        "CN": ("86", "China", "CHINA"),
        "DE": ("49", "Germany", "GERMANY"),
        "DK": ("45", "Denmark", "DENMARK"),
        "ES": ("35", "Spain", "SPAIN"),
        "FI": ("358", "Finland", "FINLAND"),
        "FR": ("33", "France", "FRANCE"),
        "GB": ("44", "United Kingdom", "UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND"),
        "HK": ("852", "Hong Kong", "HONG KONG"),
        "IE": ("353", "Ireland", "IRELAND"),
        "IL": ("972", "Israel", "ISRAEL"),
        "IT": ("5", "Italy", "ITALY"),
        "JP": ("81", "Japan", "JAPAN"),
        "KR": ("82", "South Korea", "KOREA (REPUBLIC OF)"),
        "KY": ("1345", "Cayman Islands", "CAYMAN ISLANDS"),
        "LU": ("352", "Luxembourg", "LUXEMBOURG"),
        "NL": ("31", "Netherlands", "NETHERLANDS"),
        "NO": ("47", "Norway", "NORWAY"),
        "SE": ("46", "Sweden", "SWEDEN"),
        "SG": ("65", "Singapore", "SINGAPORE"),
        "TW": ("886", "Taiwan", "TAIWAN"),
        "US": ("2", "United States of America", "UNITED STATES OF AMERICA"),
        "VG": ("1284", "British Virgin Islands", "VIRGIN ISLANDS (BRITISH)"),
    }
    for iso, (cbdt_code, country_name, country_name_portal) in baseline.items():
        entry = lookup_country(iso)
        assert entry.cbdt_code == cbdt_code, f"{iso}.cbdt_code drifted"
        assert entry.country_name == country_name, f"{iso}.country_name drifted"
        assert entry.country_name_portal == country_name_portal, (
            f"{iso}.country_name_portal drifted"
        )
