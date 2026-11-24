"""Tests for the SBI TTBR loader."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.fx import load_ttbr
from ibkr_fa.fx.ttbr_loader import SANE_RATE_LOWER, SANE_RATE_UPPER
from tests._ttbr_rows import SBI_CSV_HEADER as _HEADER
from tests._ttbr_rows import load_ttbr_lines as _load_lines
from tests._ttbr_rows import load_ttbr_text as _load_text
from tests._ttbr_rows import ttbr_row as _row


def test_load_ttbr_usd_returns_non_empty_mapping() -> None:
    rates = load_ttbr("USD")
    assert len(rates) > 1000  # >1000 trading days from 2020 to present
    assert all(isinstance(r, Decimal) for r in rates.values())
    assert all(r > 0 for r in rates.values())


def test_load_ttbr_usd_is_cached_returns_same_mapping() -> None:
    """Subsequent calls return the exact same object (functools.cache)."""
    a = load_ttbr("USD")
    b = load_ttbr("USD")
    assert a is b


def test_load_ttbr_usd_skips_zero_rate_rows() -> None:
    """SBI archive includes 'TT BUY = 0' / '0.00' rows for unpublished days.
    The loader must skip them numerically (not by string match)."""
    rates = load_ttbr("USD")
    assert all(r != Decimal("0") for r in rates.values())
    assert Decimal("0.00") not in rates.values()


def test_load_ttbr_usd_earliest_valid_date_is_2020_01_06() -> None:
    """Per the .source provenance file: 2020-01-04 and -05 are zero-rate
    rows; 2020-01-06 is the first non-zero TT BUY publication."""
    rates = load_ttbr("USD")
    earliest = min(rates.keys())
    assert earliest == date(2020, 1, 6)


def test_load_ttbr_usd_known_recent_rate_round_trips_exactly() -> None:
    """Pinned spot check: 2026-04-30 should be present with the known
    Decimal value 94.6 from the vendored snapshot."""
    rates = load_ttbr("USD")
    assert rates[date(2026, 4, 30)] == Decimal("94.6")


def test_load_ttbr_skips_both_zero_string_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The string forms '0' and '0.00' both round to Decimal('0') and
    must both be skipped."""
    lines = [
        _HEADER,
        _row("2026-01-01 09:00", "0"),
        _row("2026-01-02 09:00", "0.00"),
        _row("2026-01-03 09:00", "83.40"),
    ]
    assert _load_lines(lines, tmp_path, monkeypatch) == {date(2026, 1, 3): Decimal("83.40")}


def test_load_ttbr_preserves_decimal_precision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rates = _load_lines([_HEADER, _row("2026-01-03 09:00", "83.4050")], tmp_path, monkeypatch)
    assert str(rates[date(2026, 1, 3)]) == "83.4050"


def test_load_ttbr_usd_dates_are_unique() -> None:
    """Vendor-time dedup means each calendar date appears at most once."""
    rates = load_ttbr("USD")
    # The mapping itself enforces uniqueness; this asserts no row's date
    # parsed to None / collided.
    assert len(rates) == len(set(rates.keys()))


# ============================================================================
# Load-time integrity invariants (PR #123, issue #117)
# ============================================================================


def test_load_ttbr_usd_archive_rates_are_in_sane_bounds() -> None:
    """Smoke check that every rate in the bundled USD archive falls
    in the (SANE_RATE_LOWER, SANE_RATE_UPPER] window. If a vendor
    refresh ever introduces an out-of-bounds rate, this test fires
    in addition to the load_ttbr() InvariantError."""

    rates = load_ttbr("USD")
    for d, r in rates.items():
        assert SANE_RATE_LOWER < r <= SANE_RATE_UPPER, (
            f"USD/INR rate {r} on {d} outside ({SANE_RATE_LOWER}, {SANE_RATE_UPPER}]"
        )


def test_load_ttbr_usd_dates_are_strictly_monotonic() -> None:
    """Verify the load-time monotonicity check is satisfied by the
    bundled archive. (The check fires inside load_ttbr; this test
    just confirms the happy path on real data.)"""
    rates = load_ttbr("USD")
    dates = list(rates.keys())
    for i in range(1, len(dates)):
        assert dates[i] > dates[i - 1], (
            f"Non-monotonic dates at index {i}: {dates[i - 1]} >= {dates[i]}"
        )


def test_load_ttbr_duplicate_date_raises_invariant_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex P2 on PR #123: the duplicate-date check originally ran
    AFTER converting the iterator to a dict, which silently dropped
    duplicates before they could be observed. The fix materialises
    the pairs as a list first so the monotonic-dates invariant can
    actually see duplicates.

    Verified by constructing a synthetic two-row dataset with the
    same date and asserting load_ttbr raises InvariantError naming
    the duplicate."""
    lines = [
        _HEADER,
        _row("2025-06-15 09:00", "85.50"),
        # Same date again with a different rate -- vendor-time
        # dedup-step failure shape.
        _row("2025-06-15 15:00", "85.75"),
    ]
    with pytest.raises(InvariantError) as exc:
        _load_lines(lines, tmp_path, monkeypatch)
    assert "strictly monotonically increasing" in exc.value.what


# ============================================================================
# Strict parsing and whole-cell checks (issue #941)
# ============================================================================


@pytest.mark.parametrize(
    "bad_line",
    [
        # The PDF FILE cell is missing, so every later cell shifts left
        # and TT BUY would read the TT SELL rate.
        "2025-12-31 09:00,89.47,90.32,89.40,90.50,88,91,88,91",
        _row("2025-12-31 09:00") + ",92",
    ],
)
def test_load_ttbr_refuses_a_row_with_the_wrong_cell_count(
    bad_line: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(InvariantError) as exc:
        _load_lines([_HEADER, _row(), bad_line], tmp_path, monkeypatch)
    assert str(exc.value) == (
        "InvariantError: sbi_ttbr_xxx_inr.csv: row must have exactly 10 cells "
        "(context keys: line=3)"
    )
    assert exc.value.context == (("line", 3),)


@pytest.mark.parametrize(
    "header",
    [
        _HEADER.replace("TT SELL", "TT BUY"),
        _HEADER.replace("TT BUY,TT SELL", "TT SELL,TT BUY"),
    ],
)
def test_load_ttbr_refuses_a_header_that_is_not_the_sbi_header(
    header: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(InvariantError) as exc:
        _load_lines([header, _row()], tmp_path, monkeypatch)
    assert "sbi_ttbr_xxx_inr.csv: header must be exactly" in exc.value.what
    assert "'FOREX TRAVEL CARD BUY'" in exc.value.what


@pytest.mark.parametrize(
    "text",
    [
        "\n".join([_HEADER, _row(), _row(pdf='"Synthetic"Holder Two')]) + "\n",
        # The last cell opens a quote and the file ends before it closes.
        "\n".join([_HEADER, _row(), _row() + ',"Synthetic Holder Two']),
    ],
)
def test_load_ttbr_refuses_malformed_csv(
    text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The message is fixed text, not the ``csv.Error``'s (#1046)."""
    with pytest.raises(InvariantError) as exc:
        _load_text(text, tmp_path, monkeypatch)
    assert str(exc.value) == (
        "InvariantError: sbi_ttbr_xxx_inr.csv: malformed CSV (text after a closing quote, "
        "a quote left open or a cell over the csv field size limit) (context keys: line=3)"
    )
    assert "Synthetic" not in str(exc.value)
    assert exc.value.context == (("line", 3),)


@pytest.mark.parametrize("rate", ["NaN", "", " 89.47", "8_9.47", "U1234567"])
def test_load_ttbr_refuses_a_rate_that_is_not_a_plain_number(
    rate: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(InvariantError) as exc:
        _load_lines([_HEADER, _row("2025-12-31 09:00", rate)], tmp_path, monkeypatch)
    assert str(exc.value) == (
        "InvariantError: sbi_ttbr_xxx_inr.csv: TT BUY is not plain digits with an "
        "optional fraction (context keys: line=2)"
    )
    assert exc.value.context == (("line", 2),)


@pytest.mark.parametrize(
    ("day", "problem"),
    [
        ("Synthetic Holder Two", "is not YYYY-MM-DD HH:MM"),
        ("2025-12-31X", "is not YYYY-MM-DD HH:MM"),
        ("2025-12-31 99:99", "is not a real date and time"),
        ("2025-02-30 09:00", "is not a real date and time"),
    ],
)
@pytest.mark.parametrize("rate", ["89.47", "0"])
def test_load_ttbr_refuses_a_malformed_date_even_on_a_zero_rate_row(
    day: str, problem: str, rate: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(InvariantError) as exc:
        _load_lines([_HEADER, _row(day, rate)], tmp_path, monkeypatch)
    assert str(exc.value) == (
        f"InvariantError: sbi_ttbr_xxx_inr.csv: DATE {problem} (context keys: line=2)"
    )
    assert day not in str(exc.value)
    assert exc.value.context == (("line", 2),)


def test_load_ttbr_refuses_a_quoted_line_break_in_the_pdf_file_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(InvariantError) as exc:
        _load_lines([_HEADER, _row(pdf='"Synthetic\nHolder Two"')], tmp_path, monkeypatch)
    assert str(exc.value) == (
        "InvariantError: sbi_ttbr_xxx_inr.csv: PDF FILE cell has a non-printable character "
        "(context keys: line=3)"
    )
    assert "Synthetic" not in str(exc.value)
    assert exc.value.context == (("line", 3),)
