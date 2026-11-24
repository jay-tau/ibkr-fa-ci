"""Tests for :mod:`ibkr_fa.output.fa_manual_fill` (#626)."""

from __future__ import annotations

from dataclasses import replace

from ibkr_fa.compute.fa_a2 import FaA2ManualFillGaps
from ibkr_fa.compute.fa_a3 import FaA3ManualFillGaps
from ibkr_fa.models import FaA3PeakBlankReason
from ibkr_fa.models.fa_a2_row import FaA2ClosingBlankReason
from ibkr_fa.output.fa_manual_fill import (
    FA_A2_CLOSING_BLANK_TEXT,
    FA_A3_PEAK_BLANK_TEXT,
    MANUAL_FILL_PORTAL_COLUMNS,
    fa_a2_manual_fill_columns,
    fa_a3_manual_fill_columns,
    manual_fill_holdings,
    peak_manual_fill_holdings,
)
from ibkr_fa.types import Isin, Ticker
from tests._fa_blank_texts import A2_CLOSING_BLANK_TEXT, A3_PEAK_BLANK_TEXT

_XQAW = (Ticker("XQAW"), Isin("US99990E1038"))
_HOLDING = frozenset({_XQAW})
_SPLIT_PEAK = frozenset({(*_XQAW, FaA3PeakBlankReason.SPLIT)})


def _a3_gaps(*, holdings: frozenset[tuple[Ticker, Isin]], flag: bool) -> FaA3ManualFillGaps:
    return FaA3ManualFillGaps(
        name_of_entity=holdings,
        address_of_entity=holdings,
        zip_code=holdings,
        nature_of_entity=holdings,
        peak=_SPLIT_PEAK if flag else frozenset(),
        no_rows=False,
        dividends=flag,
        gross_proceeds=holdings,
    )


def test_manual_fill_portal_columns_are_the_seven_a3_labels() -> None:
    assert MANUAL_FILL_PORTAL_COLUMNS == (
        "Name of entity",
        "Address of entity",
        "ZIP Code",
        "Nature of entity",
        "Peak value of investment during the Period",
        "Total gross amount paid/credited with respect to the holding during the period",
        "Total gross proceeds from sale or redemption of investment during the period",
    )


def test_a3_columns_follow_portal_order() -> None:
    assert fa_a3_manual_fill_columns(_a3_gaps(holdings=_HOLDING, flag=True)) == (
        MANUAL_FILL_PORTAL_COLUMNS
    )
    assert fa_a3_manual_fill_columns(_a3_gaps(holdings=frozenset(), flag=False)) == ()


def test_a3_columns_name_only_the_flagged_gaps() -> None:
    gaps = FaA3ManualFillGaps(
        name_of_entity=frozenset(),
        address_of_entity=frozenset(),
        zip_code=_HOLDING,
        nature_of_entity=frozenset(),
        peak=_SPLIT_PEAK,
        no_rows=False,
        dividends=False,
        gross_proceeds=_HOLDING,
    )
    assert fa_a3_manual_fill_columns(gaps) == (
        "ZIP Code",
        "Peak value of investment during the Period",
        "Total gross proceeds from sale or redemption of investment during the period",
    )


def test_a3_peak_column_is_named_for_a_run_without_rows() -> None:
    gaps = FaA3ManualFillGaps(
        name_of_entity=frozenset(),
        address_of_entity=frozenset(),
        zip_code=frozenset(),
        nature_of_entity=frozenset(),
        peak=frozenset(),
        no_rows=True,
        dividends=False,
        gross_proceeds=frozenset(),
    )
    assert fa_a3_manual_fill_columns(gaps) == ("Peak value of investment during the Period",)


def test_a3_name_of_entity_column_comes_first() -> None:
    """#1369: a blank Name of entity is named before Address of entity."""
    empty = _a3_gaps(holdings=frozenset(), flag=False)
    name_only = FaA3ManualFillGaps(
        name_of_entity=_HOLDING,
        address_of_entity=frozenset(),
        zip_code=frozenset(),
        nature_of_entity=frozenset(),
        peak=frozenset(),
        no_rows=False,
        dividends=False,
        gross_proceeds=frozenset(),
    )
    assert fa_a3_manual_fill_columns(empty) == ()
    assert fa_a3_manual_fill_columns(name_only) == ("Name of entity",)
    with_address = FaA3ManualFillGaps(
        name_of_entity=_HOLDING,
        address_of_entity=_HOLDING,
        zip_code=frozenset(),
        nature_of_entity=frozenset(),
        peak=frozenset(),
        no_rows=False,
        dividends=False,
        gross_proceeds=frozenset(),
    )
    assert fa_a3_manual_fill_columns(with_address) == ("Name of entity", "Address of entity")


def test_a3_nature_of_entity_column_is_named_on_its_own() -> None:
    gaps = replace(_a3_gaps(holdings=frozenset(), flag=False), nature_of_entity=_HOLDING)
    assert fa_a3_manual_fill_columns(gaps) == ("Nature of entity",)


def test_peak_manual_fill_holdings_group_holdings_by_reason_in_reason_order() -> None:
    xqka = (Ticker("XQKA"), Isin("US99971K1024"))
    peak = [
        (*xqka, FaA3PeakBlankReason.SESSION_WITHOUT_CLOSE),
        (*_XQAW, FaA3PeakBlankReason.SESSION_WITHOUT_CLOSE),
        (*_XQAW, FaA3PeakBlankReason.PRICES_NOT_FETCHED),
    ]
    assert peak_manual_fill_holdings(peak) == (
        (FaA3PeakBlankReason.PRICES_NOT_FETCHED, ("XQAW (US99990E1038)",)),
        (
            FaA3PeakBlankReason.SESSION_WITHOUT_CLOSE,
            ("XQAW (US99990E1038)", "XQKA (US99971K1024)"),
        ),
    )


def test_every_peak_blank_reason_has_a_cause_and_a_remedy() -> None:
    assert set(FA_A3_PEAK_BLANK_TEXT) == set(FaA3PeakBlankReason)
    assert all(cause and remedy.endswith(".") for cause, remedy in FA_A3_PEAK_BLANK_TEXT.values())
    assert dict(FA_A3_PEAK_BLANK_TEXT) == A3_PEAK_BLANK_TEXT


def test_every_a2_closing_blank_reason_has_a_cause_and_a_remedy() -> None:
    """#1013: one fixed wording per reason; none points at a config value,
    since no config value can supply an A2 closing balance."""
    assert set(FA_A2_CLOSING_BLANK_TEXT) == set(FaA2ClosingBlankReason)
    for cause, remedy in FA_A2_CLOSING_BLANK_TEXT.values():
        assert cause
        assert remedy.endswith(".")
        assert "user.toml" not in remedy
        assert "[" not in remedy
    assert dict(FA_A2_CLOSING_BLANK_TEXT) == A2_CLOSING_BLANK_TEXT


def test_manual_fill_holdings_are_sorted_symbol_isin_names() -> None:
    holdings = [
        (Ticker("XQKA"), Isin("US99971K1024")),
        (Ticker("XQAW"), Isin("US99990E1038")),
        (Ticker("XQAW"), Isin("US99990E1038")),
    ]
    assert manual_fill_holdings(holdings) == ("XQAW (US99990E1038)", "XQKA (US99971K1024)")


def test_a2_columns_follow_portal_order() -> None:
    """#1358: the ZIP Code comes before the peak, as in the upload header."""
    assert fa_a2_manual_fill_columns(
        FaA2ManualFillGaps(zip_code=True, peak=True, closing=True, gross=True)
    ) == (
        "ZIP Code",
        "Peak Balance During the Period",
        "Closing balance",
        "Gross amount paid/credited to the account during the period (Nature + Amount)",
    )
    assert fa_a2_manual_fill_columns(
        FaA2ManualFillGaps(zip_code=False, peak=False, closing=True, gross=False)
    ) == ("Closing balance",)
    assert fa_a2_manual_fill_columns(
        FaA2ManualFillGaps(zip_code=True, peak=False, closing=False, gross=False)
    ) == ("ZIP Code",)
    assert (
        fa_a2_manual_fill_columns(
            FaA2ManualFillGaps(zip_code=False, peak=False, closing=False, gross=False)
        )
        == ()
    )
