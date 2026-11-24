"""End-to-end snapshot test for the Activity Statement pipeline.

Mirror of :mod:`tests.test_e2e` but exercising the Activity Statement
input format instead of Flex Query. Runs the CLI's ``schedule-fa``
subcommand against a synthetic fixture
(``tests/fixtures/e2e_activity_statement_2025.csv``) that includes
every section the Activity Statement pipeline path requires:

* ``Statement`` (with the CY-matching ``Period`` field)
* ``Account Information`` (with an unmasked account ID so the
  cross-check actually runs)
* ``Financial Instrument Information`` (for the ISIN side-table)
* ``Trades`` (with multiple ``O`` rows including same-symbol
  multiple lots, exercising lot reconstruction)
* ``Open Positions`` (summary used for reconciliation)

Additional fixtures used by later tests in this module:

* ``e2e_activity_statement_with_dividends_2025.csv`` — adds a
  ``Dividends`` section so column 10 is auto-populated (PR #137).
* ``e2e_activity_statement_with_orphan_dividend_2025.csv`` — adds
  a cash dividend on a ticker never held, exercising the
  orphan-dividend signal (PR #137 Tier-2 P3; a cash in lieu until
  #1310 made Schedule FA refuse one).
* ``e2e_activity_statement_multi_block_dividends_2025.csv`` — has
  two ``Dividends,Header`` blocks for the same symbol, locking
  the multi-block iteration fix (PR #137 Tier-3 Codex P2).

Every fixture this module reads is synthetic: made-up securities,
values and holder. The primary fixture is deliberately small and
easily scannable, with dates pinned to 2025 so the TTBR archive covers
them. If the archive's published rates for these specific dates
ever change, the snapshot assertions catch it.

Closes the Activity Statement parser tracker #101 along with #112.
"""

from __future__ import annotations

import csv
import dataclasses
import functools
import types
import typing
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa import pipeline_dispatch
from ibkr_fa import pipeline_fa as pipeline_fa_module
from ibkr_fa.cli import main
from ibkr_fa.compute import fa_a3_dividends
from ibkr_fa.compute.source_coverage import require_calendar_year_period
from ibkr_fa.exceptions import MissingFxRateError, ParseError
from ibkr_fa.fx.policy_fa import FaExactDatePolicy
from ibkr_fa.models import (
    DistributionKind,
    Dividend,
    DividendEvidence,
    FinancialYear,
    SourceBundle,
)
from ibkr_fa.parser.activity_statement import dividend_evidence, split_activity_statement_sections
from ibkr_fa.parser.activity_statement.dividends import classify_distribution
from ibkr_fa.pipeline import _dispatch_activity_statement
from ibkr_fa.pipeline_fa import MANUAL_FILL_PORTAL_COLUMNS, run_fa_a3
from ibkr_fa.pipeline_os import run_schedule_os
from ibkr_fa.prices.cache import PriceCacheError
from ibkr_fa.prices.series import PriceSourceContradictionError
from ibkr_fa.prices.yahoo import YahooResponseFormatError
from ibkr_fa.types import Isin, Ticker
from tests._activity_statement_e2e import ACTIVITY_STATEMENT_E2E_USER_TOML as _USER_TOML
from tests._activity_statement_e2e import OUTSIDE_PERIOD
from tests._change_in_nav_checks import RE_EXPORT_HINT
from tests._crash_reports import assert_prints_no_marker
from tests._ibkr_schedule_fa_inputs import flat_close_fetcher
from tests._ibkr_user_toml import CUSTODIAN_TOML
from tests._income_blocks import with_totals
from tests._malformed_csv import MARKER, UNCLOSED, malformed_message, oversized_cell
from tests._no_echo import HOLDER, assert_no_markers
from tests._price_cache import write_example_cache
from tests._trades_totals import with_trades_totals
from tests._yahoo_prices import NO_ZONE_REFUSAL, repeated_date_fetcher, serve_no_zone_response

_FIXTURE = Path(__file__).parent / "fixtures" / "e2e_activity_statement_2025.csv"
_MULTI_BLOCK_DIVIDENDS_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_multi_block_dividends_2025.csv"
)
_MULTI_COUNTRY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_multi_country_2025.csv"
)
_NAV_FIXTURE = Path(__file__).parent / "fixtures" / "e2e_activity_statement_with_nav_2025.csv"


@pytest.fixture
def setup_e2e(tmp_path: Path) -> tuple[Path, Path]:
    """Write user.toml in tmp_path and return (config_path, output_path)."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    return cfg, out


def test_e2e_activity_statement_cli_against_fixture(
    setup_e2e: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: CLI consumes the Activity Statement fixture,
    reconstructs lots from Trades, reconciles against Open
    Positions Summary, produces a review CSV whose every cell
    matches a snapshot, exits 3, and prints the manual-fill
    warning block."""
    cfg, out = setup_e2e

    rc = main(
        [
            "schedule-fa",
            "--input",
            str(_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )

    # Exit 3: Nature of entity and peak (blank under conftest.py's offline price
    # fetcher) stay manual-fill, and without [account.custodian] no Table A2 is written.
    assert rc == 3
    assert out.exists()

    captured = capsys.readouterr()
    # Stdout success summary — must announce the detected format so
    # users have an audit trail of which parser ran.
    assert "Wrote 3 Schedule FA Table A3 row(s)" in captured.out
    assert "Input format: activity-statement" in captured.out
    assert "Account: U***5678" in captured.out
    assert "U12345678" not in captured.out
    # Manual-fill warning block — only v1-incomplete columns must be named.
    # Peak is blank (conftest disables the network), so it remains manual.
    assert "Peak value of investment during the Period" in captured.out
    # This Activity Statement contains only open lots, whose gross proceeds are known to be 0.
    assert (
        "Total gross proceeds from sale or redemption of investment during the period"
        not in captured.out
    )
    # PR #137 zero-when-sure: this fixture is a full-CY Activity Statement
    # with NO Dividends section, so the gross-amount column is set to 0 for
    # every holding (NOT manual-fill) and an info note explains the inferred
    # zero. The column must NOT appear in the manual-fill block.
    assert (
        "Total gross amount paid/credited with respect to the holding during the period"
        not in captured.out
    )
    assert "No dividend activity found for the calendar year" in captured.out
    # The peak manual-fill block surfaces the [prices].ticker_overrides
    # escape hatch: fetching was on, but the conftest's fetcher returns no
    # series, so the price source had no USD series for either ticker (#962).
    assert (
        "Peak value is blank for these holdings because the price source returned no USD "
        "daily series for the ticker"
    ) in captured.out
    assert "[[prices.ticker_overrides]]" in captured.out
    assert "--no-fetch-prices" not in captured.out
    assert "Nature of entity is missing for these holdings" in captured.out
    assert "XQBB (US99995B1044)" in captured.out
    assert "XQBE (US99995D1000)" in captured.out
    assert "Exit status: 3" in captured.out
    # Account cross-check ran (unmasked ID); no SKIPPED warning.
    assert "cross-check was SKIPPED" not in captured.out
    # Stderr must be empty on the success-with-warnings path.
    assert captured.err == ""

    # --- Snapshot the CSV row-by-row -------------------------------------
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))

    # Header: 11 portal columns + 9 audit columns = 20.
    assert len(rows[0]) == 20

    # 3 data rows from the 3 reconstructed open lots — input order:
    # XQBB Jan 15, XQBB Mar 20, XQBE Jan 15.
    assert len(rows) == 4

    # Row 1: XQBB lot opened 2025-01-15.
    # basis = 320.50 USD, TTBR(2025-01-15) = 85.95
    # initial = 320.50 * 85.95 = 27546.975 -> 27547 rupees
    # closing = 4 * 95.40 * 89.47 = 34141.752 -> 34142 rupees
    # The TTBR values come from the vendored SBI archive; if those
    # rates change for these dates, this assertion catches it.
    assert rows[1] == [
        "2-UNITED STATES OF AMERICA",
        "SAMPLE ISSUER BB INC",
        "",  # address: no [[fa.entities]] entry (#1289)
        "",
        "",
        "15/01/2025",
        "27547",
        "",  # peak_value (v1 incomplete)
        "34142",
        "0",  # gross_amount_paid: no CY dividends -> 0 (PR #137)
        "0",  # gross_proceeds: open lot -> Decimal("0") -> "0" (#16)
        "US99995B1044",
        "XQBB",
        "US",
        "320.50",
        "2025-01-15",
        "95.40",
        "85.95",
        "89.47",
        "",  # no disposal date (open lot)
    ]

    # Row 2: XQBB lot opened 2025-03-20 (second lot, same symbol).
    # basis = 540.50 USD, TTBR(2025-03-20) = 85.80
    # initial = 540.50 * 85.80 = 46374.9 -> 46375 rupees
    # closing = 6 * 95.40 * 89.47 = 51212.628 -> 51213 rupees
    assert rows[2] == [
        "2-UNITED STATES OF AMERICA",
        "SAMPLE ISSUER BB INC",
        "",  # address: no [[fa.entities]] entry (#1289)
        "",
        "",
        "20/03/2025",
        "46375",
        "",
        "51213",
        "0",
        "0",
        "US99995B1044",
        "XQBB",
        "US",
        "540.50",
        "2025-03-20",
        "95.40",
        "85.8",
        "89.47",
        "",
    ]

    # Row 3: XQBE lot opened 2025-01-15.
    # basis = 1005.40 USD, TTBR(2025-01-15) = 85.95
    # initial = 1005.40 * 85.95 = 86414.13 -> 86414 rupees
    # closing = 4 * 263.40 * 89.47 = 94265.592 -> 94266 rupees
    assert rows[3] == [
        "2-UNITED STATES OF AMERICA",
        "SAMPLE ISSUER BE CORP",
        "",  # address: no [[fa.entities]] entry (#1289)
        "",
        "",
        "15/01/2025",
        "86414",
        "",
        "94266",
        "0",
        "0",
        "US99995D1000",
        "XQBE",
        "US",
        "1005.40",
        "2025-01-15",
        "263.40",
        "85.95",
        "89.47",
        "",
    ]


def test_e2e_activity_statement_fixture_has_all_required_sections() -> None:
    """Defence-in-depth: if someone accidentally trims the e2e
    fixture, this is the first test that screams.

    Verifies the five sections the Activity Statement pipeline path
    requires are all present in the committed fixture file.
    """
    text = _FIXTURE.read_text(encoding="utf-8")
    assert "Statement,Header" in text
    assert "Account Information,Header" in text
    assert "Financial Instrument Information,Header" in text
    assert "Trades,Header" in text
    assert "Open Positions,Header" in text
    # Period must be a full-CY range for the pipeline to accept it.
    assert "January 1, 2025 - December 31, 2025" in text
    # Account ID is unmasked so the cross-check exercises the
    # match path (not the masked-account path).
    assert "Account Information,Data,Account,U12345678" in text


def test_e2e_activity_statement_writer_csv_is_well_formed(
    setup_e2e: tuple[Path, Path],
) -> None:
    """Sanity: the written file is parsable as a CSV without errors --
    no quoting bugs, no encoding bugs."""
    cfg, out = setup_e2e
    main(
        [
            "schedule-fa",
            "--input",
            str(_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    # Round-trip through DictReader — would raise on malformed quoting.
    with out.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert len(rows) == 3
    # Every data row has a value for every header.
    for row in rows:
        assert set(row.keys()) == set(reader.fieldnames or [])


# ============================================================================
# Short-position end-to-end (issue #109)
# ============================================================================
#
# Issue #109: the Activity Statement parser now accepts opening short
# sales (Code=O with negative Quantity) as Side.SHORT lots. The
# overlapping_lots_for_cy compute filter drops Side.SHORT before
# Schedule FA-A3 row construction — short positions are liabilities,
# not foreign assets, and aren't reported on Schedule FA at all.
#
# The end-to-end test below builds a synthetic Activity Statement
# containing ONE opening long (XQBB) and ONE opening short (XQBE),
# runs the full pipeline, and asserts:
#   1. The CLI exits successfully (the short doesn't crash parsing).
#   2. Reconciliation passes (the short's signed quantity matches
#      Open Positions Summary's negative Quantity for the same symbol).
#   3. The output CSV contains exactly ONE row — for the long lot.
#      The short is silently filtered out.


_SHORTS_FIXTURE = Path(__file__).parent / "fixtures" / "e2e_activity_statement_short_2025.csv"


def test_e2e_activity_statement_short_lots_excluded_from_fa_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #109 end-to-end: an Activity Statement containing one
    long (XQBB) and one short (XQBE) must produce exactly ONE
    Schedule FA-A3 row — the long. The short is constructed as
    Side.SHORT by the parser, reconciles cleanly against Open
    Positions Summary's negative Quantity, and is then dropped by
    overlapping_lots_for_cy before any FA row is written.

    Pre-#109 behaviour: parser would have raised ParseError on the
    negative-Quantity O trade, blocking the user from filing AT
    ALL — even though their long position should have been
    reportable independently of the short."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(_SHORTS_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    # Exit 3: Nature of entity and peak (blank under conftest.py's offline price
    # fetcher) stay manual-fill, and without [account.custodian] no Table A2 is written.
    assert rc == 3
    assert out.exists()
    captured = capsys.readouterr()
    # The output announces ONE FA row, not two — the short was
    # filtered out post-reconciliation.
    assert "Wrote 1 Schedule FA Table A3 row(s)" in captured.out
    # Tier-1 r2 review (Opus 4.7 xhigh + GPT-5.5): the full integration
    # chain (pipeline computes `short_lots_excluded=1` -> RunReport stores
    # it -> CLI renders the info message) is pinned end-to-end. Render-
    # only tests use a synthetic RunReport and would miss a regression
    # that mis-wires the pipeline's count to 0.
    assert "Excluded 1 short lot(s)" in captured.out
    assert "issues/109" in captured.out
    # No reconciliation failure in stderr — the signed-quantity
    # check passed for both XQBB (+10) and XQBE (-4).
    assert "reconciliation failed" not in captured.err
    assert "negative quantity" not in captured.err

    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    # Header + 1 data row.
    assert len(rows) == 2
    # The one row is XQBB (the long). XQBE (the short) is absent.
    assert "SAMPLE ISSUER BB INC" in rows[1]
    assert "SAMPLE ISSUER BE CORP" not in {cell for row in rows for cell in row}


# ============================================================================
# Dividends end-to-end (issue #31)
# ============================================================================
#
# Issue #31 (this PR): the Activity Statement parser now parses the
# Dividends section and the compute layer aggregates per-symbol INR
# totals (calendar-year USD total x Dec-31 TTBR) for Schedule FA Table A3 column
# 10 ("Total gross amount paid/credited with respect to the holding
# during the period"). Pre-#31 this column was always left blank
# with a manual-fill warning; post-#31 it's auto-filled when the
# input has Dividends data.
#
# The end-to-end test below builds a synthetic Activity Statement
# with one open lot (XQBB) and two dividend payments to that holding
# during CY 2025, runs the full pipeline, and asserts:
#   1. The output CSV has gross_amount_paid_credited_inr populated
#      with a positive integer for XQBB.
#   2. The CLI's manual-fill warning OMITS the dividend column.
#   3. The remaining unfilled column (peak) is still listed in
#      the manual-fill warning (the fixture's open lot makes
#      gross proceeds known to be zero).


_DIVIDENDS_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_with_dividends_2025.csv"
)


def test_e2e_activity_statement_dividends_populate_fa_column_10(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #31 end-to-end: an Activity Statement with a Dividends
    section produces a FaA3Row whose
    gross_amount_paid_credited_inr is the CY USD total converted at Dec-31 TTBR.
    The CLI's manual-fill warning omits the dividend column.

    Pre-#31 behaviour: gross_amount_paid_credited_inr was always
    None, manual-fill warning always listed 3 columns including
    'Total gross amount paid/credited...'.
    """
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(_DIVIDENDS_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    # Manual-fill warnings still fire (peak remains), so exit code
    # is still 3. This fixture's open lot makes gross proceeds zero,
    # while dividends are populated from input.
    assert rc == 3
    assert out.exists()
    captured = capsys.readouterr()

    # The dividend column MUST be absent from the manual-fill list.
    # Pre-#31 it would have been listed alongside peak + gross-proceeds.
    assert (
        "Total gross amount paid/credited with respect to the holding "
        "during the period" not in captured.out
    )
    # Peak remains manual-fill; the open lot's zero gross proceeds must not appear.
    assert "Peak value of investment during the Period" in captured.out
    assert (
        "Total gross proceeds from sale or redemption of investment during the period"
        not in captured.out
    )

    # The output CSV row's column-10 value (1-indexed col 10 in the
    # portal; field name "Total gross amount paid/credited...") is
    # populated. Exact INR figure depends on the SBI TTBR archive
    # rates on the two payment dates (2025-03-16 and 2025-09-15)
    # so we don't pin a specific integer; instead we assert it's
    # a positive integer (was None / blank pre-#31).
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    header = rows[0]
    col10_idx = header.index(
        "Total gross amount paid/credited with respect to the holding during the period"
    )
    assert len(rows) == 2
    data_row = rows[1]
    col10_value = data_row[col10_idx]
    assert col10_value != ""
    # Two dividends of $2.10 each = $4.20 gross. At ~85-89 INR/USD
    # in 2025, expect roughly ₹350-380. Lock a generous range so
    # SBI TTBR archive updates don't break the test, but tight
    # enough to catch a 10x bug.
    inr_value = int(col10_value)
    assert 300 <= inr_value <= 400, f"Unexpected INR value: {inr_value}"


def test_e2e_schedule_fa_sums_a_dividend_row_outside_the_kind_grammar(tmp_path: Path) -> None:
    """#1096: Schedule FA A3 ignores the distribution kind, so a dividend
    row the classifier marks MALFORMED still runs and adds its gross
    amount to column 10, the same as the well-formed row did."""
    malformed = "Cash Dividend USD 0.21 per Share - US Tax"
    assert classify_distribution(malformed) is DistributionKind.MALFORMED
    text = _DIVIDENDS_FIXTURE.read_text(encoding="utf-8")
    well_formed = "Cash Dividend USD 0.21 per Share (Ordinary Dividend),2.10,"
    assert text.count(well_formed) == 2
    statement = tmp_path / "statement.csv"
    statement.write_text(text.replace(well_formed, f"{malformed},2.10,", 1), encoding="utf-8")
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")

    outputs = []
    for source in (_DIVIDENDS_FIXTURE, statement):
        out = tmp_path / f"review-{len(outputs)}.csv"
        report = run_fa_a3(input_path=source, config_path=cfg, output_path=out, calendar_year=2025)
        assert report.rows_written == 1
        outputs.append(out.read_bytes())

    assert outputs[0] == outputs[1]


# ============================================================================
# Orphan dividend symbols end-to-end (PR #137 Tier-2 P3)
# ============================================================================
#
# PR #137 Tier-2 P3 finding (Claude Code Opus 4.7 max): dividend
# symbols that parsed from the Dividends section but for which no
# matching lot exists in the FA-A3 output (typical cause: Cash-In-
# Lieu / spinoff payments on tickers never held) were silently
# dropped from FA A3 with no user signal. The manual-fill warning
# also suppressed column 10 in that case, implying the column was
# fully covered when an orphan payment had been dropped.
#
# Fix (commit 524d798): RunReport.orphan_dividends field +
# CLI informational message. End-to-end test below pins both:
#   1. The pipeline correctly identifies the orphan symbol via
#      report.orphan_dividends (asserted via direct
#      run_fa_a3 call to bypass the CLI string-render).
#   2. The CLI surfaces the orphan message to stdout for the same
#      scenario (asserted via main() + captured stdout).


_ORPHAN_DIVIDENDS_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_with_orphan_dividend_2025.csv"
)


def test_e2e_activity_statement_orphan_dividend_reported_via_run_report(
    tmp_path: Path,
) -> None:
    """PR #137 Tier-2 P3 — pipeline integration test (not just
    render). Calls run_fa_a3 directly and asserts
    report.orphan_dividends == ('SPINOFF',). The fixture
    has one held XQBB lot + one XQBB cash dividend + one SPINOFF
    cash dividend for a never-held ticker. XQBB is matched
    by the per-row attribution; SPINOFF is the orphan."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    report = run_fa_a3(
        input_path=_ORPHAN_DIVIDENDS_FIXTURE,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    # The held XQBB row is the only FA-A3 row written. The SPINOFF
    # cash dividend is silently excluded from A3 (correctly:
    # A3 reports HELD assets) but surfaces via the new orphan field
    # so the user knows to consider Schedule OS.
    assert report.rows_written == 1
    assert len(report.orphan_dividends) == 1
    [orphan] = report.orphan_dividends
    assert orphan.symbol == "SPINOFF"
    assert orphan.classification == "never_held"
    assert orphan.last_sold_date is None

    # Defense-in-depth (PR #137 Tier-1 r6 correctness P3.2): assert
    # the matched XQBB dividend ($2.10 on 2025-03-16) lands in
    # column 10 of the XQBB row. Without this assertion, an orphan-
    # detection regression that accidentally cleared
    # dividends_by_symbol_inr when orphans were present would still
    # pass the rows_written + orphan tuple checks above.
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    header = rows[0]
    col10_idx = header.index(
        "Total gross amount paid/credited with respect to the holding during the period"
    )
    assert len(rows) == 2
    held_col10 = rows[1][col10_idx]
    assert held_col10 != "", "XQBB col 10 must be auto-filled, not blank"
    # $2.10 * the 31 December 2025 TTBR (~89.5 INR/USD) ≈ ₹188. Generous range
    # so SBI archive updates don't break the test, tight enough to
    # catch a wrong-symbol attribution or a 10x bug.
    assert 150 <= int(held_col10) <= 220, f"Unexpected XQBB col 10: {held_col10}"


def test_e2e_activity_statement_orphan_dividend_message_in_cli_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PR #137 Tier-2 P3 — CLI message integration test. Confirms
    the orphan_dividends list reaches stdout via the
    informational message. Complements the unit render tests
    in test_cli.py (which use synthetic RunReports) with a real
    pipeline run."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(_ORPHAN_DIVIDENDS_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    assert rc == 3  # manual-fill warning still fires for peak value
    captured = capsys.readouterr()
    # The orphan informational message must mention the symbol.
    assert "Dropped dividends for 1 symbol(s)" in captured.out
    assert "SPINOFF" in captured.out
    assert "Schedule OS" in captured.out
    # #1310: Schedule FA refuses a cash in lieu dated in the calendar
    # year, so the advisory no longer names one as a cause.
    assert "Cash-In-Lieu" not in captured.out
    assert "deemed sale" not in captured.out
    assert "issues/137" in captured.out


def test_e2e_activity_statement_no_orphan_dividends_when_all_matched(
    tmp_path: Path,
) -> None:
    """Symmetric: the existing dividends fixture has XQBB matching
    a held XQBB lot. report.orphan_dividends must be empty
    (no orphan message in stdout). Locks the contract that
    matched-only dividend sections produce no false-positive
    orphan signal."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    report = run_fa_a3(
        input_path=_DIVIDENDS_FIXTURE,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    assert report.orphan_dividends == ()


# ============================================================================
# Pipeline empty-aggregate defense-in-depth (PR #137 Tier-1 r6 P3.3)
# ============================================================================
#
# r6 correctness reviewer (Opus 4.7 xhigh) flagged that if the
# Dividends section parses successfully but every payment_date falls
# outside the target CY, ``aggregate_dividends_by_symbol_inr`` returns
# ``{}``. Pre-fix, the pipeline passed that empty dict through to
# ``build_fa_a3_rows``, which then wrote Decimal('0') in every col-10
# cell while the manual-fill warning ALSO got suppressed (the
# ``is not None`` check at the manual-fill branch was True for {}).
# Net effect: silently-zeroed col 10 with no user signal.
#
# In practice ``require_calendar_year_period`` forbids partial-year
# Activity Statements, so the all-payments-out-of-CY scenario is
# unreachable through any valid IBKR input. The fix is defense-in-
# depth: an empty aggregate is coerced to ``None`` at the pipeline
# boundary so the manual-fill warning fires and col 10 stays blank.
#
# r7 code reviewer (GPT-5.5) P3: this fix had no focused regression
# test — only the compute-layer behaviour was covered. The test below
# locks the pipeline boundary behaviour by monkeypatching the
# aggregator to return ``{}`` and asserting the manual-fill warning
# still includes column 10 + the output CSV col 10 is blank.


def test_run_fa_a3_empty_aggregate_coerced_to_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth: when the aggregator returns ``{}`` (the
    unreachable-in-practice case where every parsed dividend's
    payment_date falls outside the target CY),
    ``pipeline_fa.run_fa_a3`` coerces that to ``None`` so the manual-
    fill warning fires AND col 10 stays blank — rather than the
    pre-fix silent-zero failure mode. Locks PR #137 Tier-1 r6 P3.3
    via the pipeline boundary, complementing the compute-layer
    ``test_dividends_empty_dict_kwarg_all_rows_zero`` which only
    covers the low-level API behaviour."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"

    # Force the aggregator to return {} regardless of input. The
    # _DIVIDENDS_FIXTURE has in-CY dividends, but we're
    # simulating the pathological "all payments outside CY" case
    # at the pipeline boundary.
    def _empty_aggregate(*_args: object, **_kwargs: object) -> dict[str, Decimal]:
        return {}

    monkeypatch.setattr(fa_a3_dividends, "aggregate_dividends_by_symbol_inr", _empty_aggregate)

    report = run_fa_a3(
        input_path=_DIVIDENDS_FIXTURE,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )

    # The manual-fill list must name column 10 (gross amount
    # paid/credited): it is every A3 manual-fill column except the
    # first, Name of entity (#1369), and the last, gross proceeds.
    assert report.manual_fill_columns == MANUAL_FILL_PORTAL_COLUMNS[1:-1]
    # Orphan list must also be empty (no orphan signal on the
    # unreachable-in-practice path).
    assert report.orphan_dividends == ()

    # Output CSV's col 10 cells must be blank, NOT "0". The pre-
    # fix bug rendered "0" (the writer's format of Decimal("0"))
    # which the portal accepted as a valid value while the user
    # had no warning to look up the real number.
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    header = rows[0]
    col10_idx = header.index(
        "Total gross amount paid/credited with respect to the holding during the period"
    )
    assert len(rows) >= 2
    for data_row in rows[1:]:
        assert data_row[col10_idx] == "", (
            f"col 10 must be blank when aggregator returns {{}}, got {data_row[col10_idx]!r}"
        )


# ============================================================================
# PR #137 zero-when-sure: section-presence guard (rubber-duck findings)
# ============================================================================
#
# The FA pipeline infers ₹0 for the gross-amount column when an Activity
# Statement has no dividends — but an empty PARSED dividend list is too weak
# a signal on its own (a present-but-unparsed Dividends section, or accrual-
# only activity in "Change in Dividend Accruals", would be a false zero that
# UNDERSTATES income — the highest-cost error for a tax tool). So zeroing is
# gated on ``dividend_evidence`` returning ``ABSENT``, which requires
# POSITIVE evidence that neither dividend-related section is present.


def _write_min_activity_statement(path: Path, *, extra_sections: str = "") -> None:
    """Write a minimal splitter-valid Activity Statement, plus extra rows."""
    path.write_text(
        "Statement,Header,Field Name,Field Value\n"
        "Statement,Data,Title,Activity Statement\n"
        'Statement,Data,Period,"January 1, 2025 - December 31, 2025"\n'
        "Account Information,Header,Field Name,Field Value\n"
        "Account Information,Data,Account,U12345678\n" + extra_sections,
        encoding="utf-8",
    )


def test_dividend_evidence_section_presence(tmp_path: Path) -> None:
    """``dividend_evidence`` is PRESENT iff a Dividends OR Change in Dividend
    Accruals section header is present — independent of whether those
    sections have any parsable data rows."""
    none_present = tmp_path / "none.csv"
    _write_min_activity_statement(none_present)
    assert (
        dividend_evidence(split_activity_statement_sections(none_present))
        is DividendEvidence.ABSENT
    )

    dividends_present = tmp_path / "dividends.csv"
    _write_min_activity_statement(
        dividends_present,
        extra_sections=(
            "Dividends,Header,Currency,Date,Description,Amount\n"
            "Dividends,Data,USD,16-03-2025,XQBB(US99995B1044) Cash Dividend,2.10\n"
        ),
    )
    assert (
        dividend_evidence(split_activity_statement_sections(dividends_present))
        is DividendEvidence.PRESENT
    )

    accruals_present = tmp_path / "accruals.csv"
    _write_min_activity_statement(
        accruals_present,
        extra_sections=(
            "Change in Dividend Accruals,Header,Currency,Symbol,Date,Description,Amount\n"
            "Change in Dividend Accruals,Data,USD,XQBB,2025-12-15,accrual reversal,-1.50\n"
        ),
    )
    assert (
        dividend_evidence(split_activity_statement_sections(accruals_present))
        is DividendEvidence.PRESENT
    )


def test_run_fa_a3_accruals_section_blocks_zero_inference(tmp_path: Path) -> None:
    """End-to-end safety guard: an Activity Statement with a Change in
    Dividend Accruals section but NO Dividends section must NOT infer ₹0 —
    accrual activity means we cannot assert "no dividends". The gross-amount
    column stays manual-fill (the conservative, never-understate choice)."""
    base = _FIXTURE.read_text(encoding="utf-8").rstrip("\n")
    accruals = (
        "\nChange in Dividend Accruals,Header,Currency,Symbol,Date,Description,Amount\n"
        "Change in Dividend Accruals,Data,USD,XQBB,2025-12-15,XQBB accrual reversal,-1.50\n"
    )
    variant = tmp_path / "with_accruals.csv"
    variant.write_text(base + accruals, encoding="utf-8")
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"

    report = run_fa_a3(
        input_path=variant,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )

    assert report.fa_gross_amount_zeroed_no_cy_dividends is False
    assert (
        "Total gross amount paid/credited with respect to the holding during the period"
        in report.manual_fill_columns
    )
    # The output CSV's gross-amount cells stay blank (manual-fill), NOT "0".
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    col10_idx = rows[0].index(
        "Total gross amount paid/credited with respect to the holding during the period"
    )
    assert len(rows) >= 2
    for data_row in rows[1:]:
        assert data_row[col10_idx] == ""


# ============================================================================
# #699: a missing December 31 TTBR refuses the A3 dividend column
# ============================================================================


def test_aggregate_cy_dividends_for_fa_refuses_missing_dec31_rate() -> None:
    dividend = Dividend(
        symbol=Ticker("XQBB"),
        isin=Isin("US99995B1044"),
        payment_date=date(2099, 3, 16),
        amount_gross=Decimal("5"),
        currency="USD",
        description="XQBB(US99995B1044) Cash Dividend USD 0.50 per Share",
    )
    with pytest.raises(MissingFxRateError):
        fa_a3_dividends.resolve_fa_a3_dividends(
            [dividend],
            calendar_year=2099,  # no December 31 rate
            fx_policy=FaExactDatePolicy(max_carry_forward_days=14),
            dividend_evidence=DividendEvidence.PRESENT,
        )


def test_run_fa_a3_refuses_missing_dec31_rate_for_cy_dividend(tmp_path: Path) -> None:
    """No lots and no ``[account.custodian]``, so only A3's dividend column
    needs the December 31 rate, which 2099 lacks."""
    statement = tmp_path / "statement.csv"
    statement.write_text(
        "Statement,Header,Field Name,Field Value\n"
        "Statement,Data,BrokerName,Interactive Brokers LLC\n"
        "Statement,Data,Title,Activity Statement\n"
        'Statement,Data,Period,"January 1, 2099 - December 31, 2099"\n'
        "Account Information,Header,Field Name,Field Value\n"
        "Account Information,Data,Name,Sample Holder\n"
        "Account Information,Data,Account,U12345678\n"
        "Account Information,Data,Account Type,Individual\n"
        "Account Information,Data,Customer Type,Individual\n"
        "Account Information,Data,Account Capabilities,Cash\n"
        "Account Information,Data,Base Currency,USD\n"
        "Financial Instrument Information,Header,Asset Category,Symbol,Description,"
        "Conid,Security ID,Listing Exch,Multiplier,Type,Code\n"
        "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,"
        "Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code\n"
        "Dividends,Header,Currency,Date,Description,Amount\n"
        "Dividends,Data,USD,16-03-2099,"
        "XQBB(US99995B1044) Cash Dividend USD 0.50 per Share (Ordinary Dividend),5.00\n"
        "Dividends,Data,Total,,,5.00\n",
        encoding="utf-8",
    )
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"

    with pytest.raises(MissingFxRateError):
        run_fa_a3(
            input_path=statement,
            config_path=cfg,
            output_path=out,
            calendar_year=2099,
        )

    assert sorted(path.name for path in tmp_path.iterdir()) == ["statement.csv", "user.toml"]


# ============================================================================
# #626: the FA pipeline reads dividend evidence and NAV from the bundle
# ============================================================================

_CUSTODIAN_TOML = _USER_TOML + CUSTODIAN_TOML
_NAV_TOTAL_ROW = "Net Asset Value,Data,Total,0,2107.60,0,2107.60,2107.60"
_MALFORMED_NAV_TOTAL_ROW = "Net Asset Value,Data,Total,0,2107.60,0,abc,2107.60"


def _write_config(tmp_path: Path, text: str) -> Path:
    cfg = tmp_path / "user.toml"
    cfg.write_text(text, encoding="utf-8")
    return cfg


def _malformed_nav_statement(tmp_path: Path) -> Path:
    text = _NAV_FIXTURE.read_text(encoding="utf-8")
    assert text.count(_NAV_TOTAL_ROW) == 1
    statement = tmp_path / "malformed-nav.csv"
    statement.write_text(text.replace(_NAV_TOTAL_ROW, _MALFORMED_NAV_TOTAL_ROW), encoding="utf-8")
    return statement


def test_run_fa_a3_splits_the_statement_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """USD base, a custodian and no parsed dividends: before #626 the FA
    pipeline re-split this file for dividend evidence (twice) and for NAV."""
    split = pipeline_dispatch.split_activity_statement_sections
    calls: list[Path] = []

    def spy(path: Path) -> object:
        calls.append(path)
        return split(path)

    monkeypatch.setattr(pipeline_dispatch, "split_activity_statement_sections", spy)
    report = run_fa_a3(
        input_path=_NAV_FIXTURE,
        config_path=_write_config(tmp_path, _CUSTODIAN_TOML),
        output_path=tmp_path / "review.csv",
        calendar_year=2025,
    )

    assert calls == [_NAV_FIXTURE]
    assert report.fa_gross_amount_zeroed_no_cy_dividends is True
    [a2_row] = report.a2_rows
    assert a2_row.closing_balance_inr is not None


def test_run_fa_a3_with_custodian_refuses_malformed_nav_before_prices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_prices(*_args: object, **_kwargs: object) -> None:
        pytest.fail("prices must not load once the NAV refused at dispatch")

    monkeypatch.setattr(pipeline_fa_module, "load_fa_price_series", no_prices)
    with pytest.raises(ParseError) as exc:
        run_fa_a3(
            input_path=_malformed_nav_statement(tmp_path),
            config_path=_write_config(tmp_path, _CUSTODIAN_TOML),
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )
    error = exc.value
    # The Total row is the block's third row; the refusal echoes no cell (#1040).
    assert (error.section, error.row_index, error.column) == ("Net Asset Value", 2, "Current Total")
    assert (error.message, error.raw_value) == ("must be a decimal number", "")


def test_run_fa_a3_without_custodian_ignores_malformed_nav(tmp_path: Path) -> None:
    report = run_fa_a3(
        input_path=_malformed_nav_statement(tmp_path),
        config_path=_write_config(tmp_path, _USER_TOML),
        output_path=tmp_path / "review.csv",
        calendar_year=2025,
    )
    assert report.a2_rows == ()


def test_run_schedule_os_ignores_malformed_nav(tmp_path: Path) -> None:
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "e2e_activity_statement_schedule_os_zero_dividends_fy2025_26.csv"
    )
    statement = tmp_path / "malformed-nav.csv"
    statement.write_text(
        fixture.read_text(encoding="utf-8")
        + "Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,"
        "Current Total,Change\n" + _MALFORMED_NAV_TOTAL_ROW + "\n",
        encoding="utf-8",
    )
    report = run_schedule_os(
        input_path=statement,
        config_path=_write_config(tmp_path, _USER_TOML),
        output_path=tmp_path / "schedule-os.csv",
        financial_year=FinancialYear(start_year=2025),
    )
    assert report.ordinary_dividend_rows_written == 0


# ============================================================================
# Multi-block Dividends section (PR #137 Tier-3 Codex GPT-5.5 high P2)
# ============================================================================
#
# The Activity Statement splitter (`split_activity_statement_sections`)
# returns a `list[Section]` per `KnownActivitySection` because IBKR can
# re-emit a section header mid-file (the splitter's docstring's
# explicit example is Net Asset Value). Pre-fix, the pipeline indexed
# `sections[KnownActivitySection.DIVIDENDS][0]` only — silently dropping
# any payments in a second+ Dividends,Header block. The fixture used here
# has two Dividends,Header blocks: XQBB $2.10 in block 1, XQBB $5.00 in
# block 2. The test asserts BOTH are parsed and summed in column 10.


def test_e2e_activity_statement_multi_block_dividends_all_parsed(
    tmp_path: Path,
) -> None:
    """PR #137 Tier-3 Codex P2: iterate over all Dividends,Header
    blocks. The fixture has two such blocks for the same symbol
    (XQBB $2.10 + XQBB $5.00 = $7.10 total). Pre-fix only $2.10
    would have been counted; the second $5.00 would be silently
    dropped. The col-10 value must reflect both."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    report = run_fa_a3(
        input_path=_MULTI_BLOCK_DIVIDENDS_FIXTURE,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    assert report.rows_written == 1
    assert report.orphan_dividends == ()

    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    header = rows[0]
    col10_idx = header.index(
        "Total gross amount paid/credited with respect to the holding during the period"
    )
    held_col10 = rows[1][col10_idx]
    assert held_col10 != "", "XQBB col 10 must be populated from both blocks"
    # $2.10 (block 1, 2025-03-16) + $5.00 (block 2, 2025-09-15) = $7.10
    # gross. At ~85-89 INR/USD in 2025 → roughly ₹600. If only block
    # 1 had been parsed (pre-fix), the value would be ~₹178 — caught
    # by the lower-bound assertion.
    inr_value = int(held_col10)
    assert 500 <= inr_value <= 700, (
        f"Expected ~₹600 for two-block XQBB dividends; got {inr_value} — "
        f"suggests only one block was parsed (Codex P2 regression)"
    )


# ============================================================================
# Multi-country ISIN support (#143)
# ============================================================================
#
# Issue #143 (tracked under #52 epic — multi-country axis split from the
# broader multi-currency work): the Activity Statement trades parser
# previously hard-rejected any non-US ISIN, so a statement holding a
# foreign-incorporated security (a common pattern for ADRs, dual-listed CA
# stocks, Cayman tech IPOs and Irish UCITS USD ETFs) couldn't run end-to-end.
#
# This synthetic fixture has 3 holdings traded in USD on US exchanges, two of
# them foreign-incorporated:
#   - XQBB (US-issued, control row)
#   - XQBC (Canadian — CA99995H1010)
#   - XQBJ (Irish — IE99995J1074)
#
# Pre-#143: would have rejected XQBC with "country prefix is 'CA', not 'US'".
# Post-#143: all 3 holdings emit correct country codes in Schedule FA col 1.


def test_e2e_activity_statement_multi_country_isins_accepted(
    tmp_path: Path,
) -> None:
    """Issue #143: foreign-incorporated USD-traded holdings (Canadian
    XQBC, Irish XQBJ) parse alongside US XQBB. Each emits its ITR
    country code and name (e.g. ``1-CANADA``) in Schedule FA col 1.
    Pre-#143 the parser would have raised ParseError on the first
    non-US ISIN, aborting the run."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    report = run_fa_a3(
        input_path=_MULTI_COUNTRY_FIXTURE,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    assert report.rows_written == 3

    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    header = rows[0]
    country_idx = header.index("Country Name and Code")
    ticker_idx = header.index("_audit: Source ticker")
    # Build a {ticker: country} map for clear per-ticker assertions.
    by_ticker = {r[ticker_idx]: r[country_idx] for r in rows[1:]}
    assert by_ticker["XQBB"].startswith("2-")  # US ITR code is 2
    assert "UNITED STATES OF AMERICA" in by_ticker["XQBB"]
    assert by_ticker["XQBC"].startswith("1-")  # CA ITR code is 1
    assert "CANADA" in by_ticker["XQBC"]
    assert by_ticker["XQBJ"].startswith("353-")  # IE ITR code is 353
    assert "IRELAND" in by_ticker["XQBJ"]


# ============================================================================
# Withholding Tax section end-to-end (#138)
# ============================================================================
#
# Issue #138 added the Withholding Tax parser as a sister to the
# Dividends parser. The end-to-end contract on the pipeline layer
# is: an Activity Statement whose Withholding Tax section is present
# parses to a non-empty list and is surfaced via the new
# ``RunReport.withholding_tax_entries`` count. Schedule OS / FSI /
# Form 67 consumption is compute-layer work tracked under #33 — this
# test does NOT verify any downstream INR conversion or pairing
# with the originating dividend row.

_WITHHOLDING_TAX_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_with_withholding_tax_2025.csv"
)


def test_e2e_activity_statement_withholding_tax_threads_through_pipeline(
    tmp_path: Path,
) -> None:
    """Issue #138 end-to-end: pipeline parses the Withholding Tax
    section into WithholdingTax records and surfaces the count via
    ``RunReport.withholding_tax_entries``. The fixture has two WHT
    rows (the negative-amount, US-Tax-suffixed siblings of the
    Dividends fixture's two dividend rows) totalling -$0.64.

    The compute layer doesn't yet consume these records — Schedule
    OS net dividends, FSI per-country foreign tax, TR, and Form 67
    are all tracked under #33. This test pins the pipeline contract
    (5-tuple dispatch, threading to ``RunReport``) so the next PR
    in the chain can build on a stable surface.
    """
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    report = run_fa_a3(
        input_path=_WITHHOLDING_TAX_FIXTURE,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    # Two WHT rows in the fixture (sibling to the two Dividends
    # rows on the same payment dates).
    assert report.withholding_tax_entries == 2
    # Dividends are still parsed and threaded — Withholding Tax
    # support doesn't regress the #31 dividend-on-column-10
    # behaviour from the dividends fixture above.
    assert report.rows_written == 1


def test_e2e_activity_statement_no_withholding_tax_section_is_zero(
    setup_e2e: tuple[Path, Path],
) -> None:
    """The primary Activity Statement fixture has no Withholding
    Tax section; the pipeline must produce
    ``withholding_tax_entries == 0`` (not raise) — common for
    accounts with a W-9 on file at IBKR (US persons get 0%
    withholding) and for buy-and-hold accounts with no
    foreign-dividend-paying holdings."""
    cfg, out = setup_e2e
    report = run_fa_a3(
        input_path=_FIXTURE,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    assert report.withholding_tax_entries == 0


def test_e2e_flex_query_input_returns_zero_withholding_tax(
    tmp_path: Path,
) -> None:
    """Flex Query inputs have no Withholding Tax parser yet (see
    the Flex dispatcher in pipeline.py); the 5-tuple contract is
    uniform across both input formats so downstream consumers
    don't need to branch on ``input_format``. A Flex Query run
    must produce ``withholding_tax_entries == 0`` without raising.
    """
    flex_fixture = Path(__file__).parent / "fixtures" / "e2e_year_end_2025.csv"
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    report = run_fa_a3(
        input_path=flex_fixture,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    assert report.withholding_tax_entries == 0


# ============================================================================
# Issue #139 — Interest section parser end-to-end. Sister test block to the
# Withholding Tax suite above. The pipeline contract: an Activity Statement
# whose Interest section is present parses to a non-empty list and is
# surfaced via the new ``RunReport.interest_credits_entries`` count. Schedule
# OS interest income (#32) and FSI per-source-country breakdown (#35) are
# compute-layer concerns tracked separately; this test does NOT verify any
# downstream INR conversion.
# ============================================================================

_INTEREST_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_with_interest_2025.csv"
)


def test_e2e_activity_statement_interest_threads_through_pipeline(
    tmp_path: Path,
) -> None:
    """Issue #139 end-to-end: pipeline parses the Interest section
    into InterestCredit records and surfaces the count via
    ``RunReport.interest_credits_entries``. The fixture has three
    monthly USD-credit-interest rows totalling $4.35.

    The compute layer doesn't yet consume these records — Schedule
    OS interest income (#32) and FSI per-source-country breakdown
    (#35) are tracked separately. This test pins the pipeline
    contract (SourceBundle dispatch, threading to ``RunReport``) so the
    next PR in the chain can build on a stable surface.
    """
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    report = run_fa_a3(
        input_path=_INTEREST_FIXTURE,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    # Three Interest rows in the fixture (one per April / May / June).
    assert report.interest_credits_entries == 3
    # Withholding Tax section absent in this fixture — pipeline
    # produces zero without raising.
    assert report.withholding_tax_entries == 0


def test_e2e_activity_statement_no_interest_section_is_zero(
    setup_e2e: tuple[Path, Path],
) -> None:
    """The primary Activity Statement fixture has no Interest
    section; the pipeline must produce
    ``interest_credits_entries == 0`` (not raise) — common for
    accounts whose cash balance never exceeded IBKR's minimum-
    balance threshold for credit interest, or for periods where
    rates were below the threshold."""
    cfg, out = setup_e2e
    report = run_fa_a3(
        input_path=_FIXTURE,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    assert report.interest_credits_entries == 0


def test_e2e_flex_query_input_returns_zero_interest_credits(
    tmp_path: Path,
) -> None:
    """Flex Query inputs have no Interest parser yet (see the Flex
    dispatcher in pipeline_dispatch.py); the SourceBundle contract is uniform
    across both input formats so downstream consumers don't need to
    branch on ``input_format``. A Flex Query run must produce
    ``interest_credits_entries == 0`` without raising.
    """
    flex_fixture = Path(__file__).parent / "fixtures" / "e2e_year_end_2025.csv"
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    report = run_fa_a3(
        input_path=flex_fixture,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    assert report.interest_credits_entries == 0


# ============================================================================
# Issue #145 — US-person withholding-tax advisory end-to-end. Validates the
# 3 mutually-exclusive CLI advisory branches against synthetic fixtures.
# ============================================================================
#
# Fixtures used:
#   * ``e2e_activity_statement_with_dividends_2025.csv`` — US-source
#     dividends (XQBB US ISIN) but no Withholding Tax section. The
#     "missing W-8BEN" base shape — branch outcome depends
#     on us_person.
#   * ``e2e_activity_statement_with_withholding_tax_2025.csv`` — US-source
#     dividends + matching Withholding Tax records. The "correctly
#     withheld" base shape — branch outcome depends on us_person.
#   * ``e2e_activity_statement_multi_country_2025.csv`` — has non-US
#     issuers (CA, KY, IE). With no US-source dividends, NO advisory
#     should ever fire regardless of us_person.

_US_PERSON_USER_TOML = """\
[account]
number = "U12345678"
holder_name = "Sample Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"
us_person = true

[fx]
max_carry_forward_days = 14
"""


def test_e2e_us_source_dividends_zero_wht_default_user_emits_w8ben_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-US-person account (default us_person=false) parsing a
    statement with US-source dividends but no Withholding Tax
    section: the CLI must surface the W-8BEN-missing warning,
    exit cleanly (advisory is non-blocking), and write the FA-A3
    CSV. This is the production shape from the bug
    that motivated #145, except with us_person=false."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    exit_code = main(
        [
            "schedule-fa",
            "--input",
            str(_DIVIDENDS_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    # Exit 3: Nature of entity and peak (blank under conftest.py's offline price
    # fetcher) stay manual-fill, and without [account.custodian] no Table A2 is written.
    assert exit_code == 3
    assert out.exists()
    captured = capsys.readouterr()
    assert "W-8BEN" in captured.out
    assert "US-source dividend" in captured.out
    assert "issues/145" in captured.out
    assert "US Form 1040" not in captured.out
    assert "backup withholding" not in captured.out


def test_e2e_us_source_dividends_zero_wht_us_person_emits_ftc_1040_advisory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """US-person account (us_person=true) parsing the same shape:
    the CLI must surface the FTC-via-1040 informational note
    instead of the W-8BEN warning. This is a production
    shape: the user is a US citizen ordinarily resident in
    India, W-9 on file at IBKR, zero NRA withholding is correct."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_US_PERSON_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    exit_code = main(
        [
            "schedule-fa",
            "--input",
            str(_DIVIDENDS_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    # Exit 3: Nature of entity and peak (blank under conftest.py's offline price
    # fetcher) stay manual-fill, and without [account.custodian] no Table A2 is written.
    assert exit_code == 3
    captured = capsys.readouterr()
    assert "US Form 1040" in captured.out
    assert "W-9 on file" in captured.out
    assert "issues/145" in captured.out
    assert "W-8BEN" not in captured.out
    assert "backup withholding" not in captured.out


def test_e2e_us_person_with_us_withholding_emits_backup_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """US-person account with US-source dividends AND US-source
    withholding tax: unusual — emit the backup-withholding
    warning."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_US_PERSON_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    exit_code = main(
        [
            "schedule-fa",
            "--input",
            str(_WITHHOLDING_TAX_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    # Exit 3: Nature of entity and peak (blank under conftest.py's offline price
    # fetcher) stay manual-fill, and without [account.custodian] no Table A2 is written.
    assert exit_code == 3
    captured = capsys.readouterr()
    assert "backup withholding" in captured.out
    assert "1099-DIV" in captured.out
    assert "issues/145" in captured.out
    assert "W-8BEN" not in captured.out
    assert "US Form 1040" not in captured.out


def test_e2e_non_us_person_with_correct_us_withholding_emits_no_advisory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default non-US-person account with US-source dividends AND
    matching US-source withholding tax: the system is working as
    intended (W-8BEN on file, IBKR withholding India treaty rate).
    No advisory should fire."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    exit_code = main(
        [
            "schedule-fa",
            "--input",
            str(_WITHHOLDING_TAX_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    # Exit 3: Nature of entity and peak (blank under conftest.py's offline price
    # fetcher) stay manual-fill, and without [account.custodian] no Table A2 is written.
    assert exit_code == 3
    captured = capsys.readouterr()
    assert "issues/145" not in captured.out
    assert "W-8BEN" not in captured.out
    assert "US Form 1040" not in captured.out
    assert "backup withholding" not in captured.out


def test_e2e_no_us_source_dividends_emits_no_advisory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A multi-country fixture with no US-incorporated issuers
    (XQBC/CA, etc.) — even though it includes non-US dividends,
    the us_source_dividend_count is zero so no #145 advisory
    fires, regardless of us_person. Uses us_person=true as the
    stricter test (would fire backup-withholding if the gate were
    purely WHT-count based instead of US-source-dividend based)."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_US_PERSON_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    exit_code = main(
        [
            "schedule-fa",
            "--input",
            str(_MULTI_COUNTRY_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    # Exit 3: Nature of entity and peak (blank under conftest.py's offline price
    # fetcher) stay manual-fill, and without [account.custodian] no Table A2 is written.
    assert exit_code == 3
    captured = capsys.readouterr()
    assert "issues/145" not in captured.out
    assert "W-8BEN" not in captured.out
    assert "US Form 1040" not in captured.out
    assert "backup withholding" not in captured.out


# ---------------------------------------------------------------------------
# PR #152 Codex Tier-5 P1 — reversal/refund WHT rows must NOT be counted as
# "paid US-source withholding" for advisory purposes. The fixture has US-
# source dividends plus two POSITIVE-amount WHT rows (reversals of an
# earlier-period withholding). For a non-US-person, this MUST still surface
# the W-8BEN-missing warning. For a US-person, it must NOT surface the
# backup-withholding warning.
# ---------------------------------------------------------------------------

_REVERSAL_ONLY_WHT_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "e2e_activity_statement_with_reversal_only_withholding_tax_2025.csv"
)


def test_e2e_reversal_only_us_wht_treated_as_no_paid_wht_non_us_person(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-US-person account whose only US-source WHT rows are
    POSITIVE-amount reversals/refunds (no negative-amount paid
    rows): the W-8BEN-missing warning MUST still fire — zero
    tax was actually withheld in the period."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    exit_code = main(
        [
            "schedule-fa",
            "--input",
            str(_REVERSAL_ONLY_WHT_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    assert exit_code == 3
    captured = capsys.readouterr()
    assert "W-8BEN" in captured.out
    assert "issues/145" in captured.out
    assert "US Form 1040" not in captured.out
    assert "backup withholding" not in captured.out


def test_e2e_reversal_only_us_wht_does_not_trigger_backup_warning_us_person(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """US-person account whose only US-source WHT rows are
    POSITIVE-amount reversals/refunds: the backup-withholding
    warning MUST NOT fire (no tax was actually withheld). The
    FTC-via-1040 informational note fires instead."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_US_PERSON_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    exit_code = main(
        [
            "schedule-fa",
            "--input",
            str(_REVERSAL_ONLY_WHT_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    assert exit_code == 3
    captured = capsys.readouterr()
    assert "backup withholding" not in captured.out
    assert "US Form 1040" in captured.out
    assert "issues/145" in captured.out
    assert "W-8BEN" not in captured.out


def test_e2e_reversal_only_us_wht_report_counts_zero_paid_withholding(
    tmp_path: Path,
) -> None:
    """Direct assertion: ``us_source_withholding_tax_net_paid`` MUST
    floor at zero when the period contains only reversal/refund rows.
    The fixture has two positive-amount WHT rows (signed net = +0.64,
    which represents a refund), so net paid MUST be zero — even
    though the parser consumed two WHT rows total."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    report = run_fa_a3(
        input_path=_REVERSAL_ONLY_WHT_FIXTURE,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    assert report.withholding_tax_entries == 2  # parser saw 2 rows
    assert report.us_source_withholding_tax_net_paid == Decimal("0")  # but zero paid
    assert report.us_source_dividend_count == 2


# ---------------------------------------------------------------------------
# PR #152 Codex Tier-3 P2 regression — paired paid+reversal canceling
# within the same period. The fixture pairs each negative-amount paid row
# with a same-magnitude positive-amount reversal row (signed net = $0),
# representing the case where IBKR withheld at the dividend then refunded
# it in the same period (e.g., W-8BEN got processed mid-period). The
# advisory MUST treat this as "no net withholding" — neither the original
# raw-row-count predicate (which would count the paid row and say "WHT
# present") nor a `< 0` predicate handles this case. Only net-summation
# with a zero floor (`max(-net, 0)`) does.
# ---------------------------------------------------------------------------

_PAIRED_CANCELING_WHT_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "e2e_activity_statement_with_paired_canceling_withholding_tax_2025.csv"
)


def test_e2e_paired_canceling_us_wht_treated_as_no_paid_wht_non_us_person(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-US-person account whose US-source WHT rows are paired
    paid+reversal that cancel to a signed net of zero: the
    W-8BEN-missing warning MUST fire — the user has no net tax
    credit to claim, so semantically no withholding occurred."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    exit_code = main(
        [
            "schedule-fa",
            "--input",
            str(_PAIRED_CANCELING_WHT_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    assert exit_code == 3
    captured = capsys.readouterr()
    assert "W-8BEN" in captured.out
    assert "issues/145" in captured.out
    assert "US Form 1040" not in captured.out
    assert "backup withholding" not in captured.out


def test_e2e_paired_canceling_us_wht_does_not_trigger_backup_warning_us_person(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """US-person account whose US-source WHT rows are paired
    paid+reversal that cancel to a signed net of zero: the
    backup-withholding warning MUST NOT fire (no net tax was
    actually withheld). FTC-via-1040 fires instead."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_US_PERSON_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    exit_code = main(
        [
            "schedule-fa",
            "--input",
            str(_PAIRED_CANCELING_WHT_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    assert exit_code == 3
    captured = capsys.readouterr()
    assert "backup withholding" not in captured.out
    assert "US Form 1040" in captured.out
    assert "issues/145" in captured.out
    assert "W-8BEN" not in captured.out


def test_e2e_paired_canceling_us_wht_report_net_paid_is_zero(
    tmp_path: Path,
) -> None:
    """Direct assertion: ``us_source_withholding_tax_net_paid`` MUST
    floor at zero when the period's paid and reversal rows cancel
    out. The fixture has two -$0.32 paid rows and two +$0.32
    reversal rows (signed net = $0), so net paid MUST be zero —
    even though the parser consumed four WHT rows total."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    report = run_fa_a3(
        input_path=_PAIRED_CANCELING_WHT_FIXTURE,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    assert report.withholding_tax_entries == 4  # parser saw 4 rows
    assert report.us_source_withholding_tax_net_paid == Decimal("0")
    assert report.us_source_dividend_count == 2


# ============================================================================
# End-to-end FIFO closures (issue #104)
# ============================================================================

_CLOSURES_FIXTURE = Path(__file__).parent / "fixtures" / "e2e_activity_statement_closures_2025.csv"

_CLOSURE_ONLY_NO_OP_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "e2e_activity_statement_closure_only_no_open_positions_2025.csv"
)


def test_e2e_activity_statement_closures_fixture_emits_open_and_closed_lots(
    setup_e2e: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: the closures fixture exercises FIFO matching at
    the pipeline level. Asserts the CLI completes successfully, the
    review CSV holds the two surviving open lots (PART residual,
    SURV), and the dispatcher's closed_lots return carries one
    ClosedLot per FIFO slice (SOLO 1, DUAL 2, PART 1 = 4 slices).

    Verifies the post-#104 contract end-to-end: that mid-year-closed
    symbols (SOLO, DUAL) don't appear in the FA-A3 review CSV (they
    have no surviving open), and that survivors with residual
    quantity (PART = 4 shares from a 10-share open) carry the
    proportionally-allocated residual basis."""
    cfg, out = setup_e2e

    rc = main(
        [
            "schedule-fa",
            "--input",
            str(_CLOSURES_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    assert rc == 3
    assert out.exists()
    captured = capsys.readouterr()
    # 6 rows: PART residual + SURV (open lots) + SOLO + DUAL slice 1
    # + DUAL slice 2 + PART closed slice (closed lots that overlap CY).
    assert "Wrote 6 Schedule FA Table A3 row(s)" in captured.out

    # CSV: header + 6 data rows. Closed-lot rows carry the disposal
    # date in the last audit column; open-lot rows leave it blank.
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 7
    symbols_seen = [row[12] for row in rows[1:]]
    assert sorted(symbols_seen) == ["DUAL", "DUAL", "PART", "PART", "SOLO", "SURV"]
    # Open-lot rows have blank disposal date; closed-lot rows are dated.
    blank_disposal = [row for row in rows[1:] if row[-1] == ""]
    dated_disposal = [row for row in rows[1:] if row[-1] != ""]
    assert len(blank_disposal) == 2  # PART residual + SURV
    assert len(dated_disposal) == 4  # SOLO + DUAL x2 + PART closed

    # Cross-check the dispatcher directly to verify ClosedLot counts
    # — the FA-A3 CSV doesn't surface closed lots in v1, so this is
    # the only place we can assert them end-to-end.
    bundle = _dispatch_activity_statement(
        _CLOSURES_FIXTURE,
        period_validator=functools.partial(require_calendar_year_period, calendar_year=2025),
    )
    open_lots, closed_lots = bundle.open_lots, bundle.closed_lots
    assert len(open_lots) == 2
    assert {str(lot.symbol) for lot in open_lots} == {"PART", "SURV"}
    # PART residual: 4 shares of the 10-share open at basis 1000 = 400.
    part = next(lot for lot in open_lots if str(lot.symbol) == "PART")
    assert part.quantity == Decimal("4")
    assert part.cost_basis_money == Decimal("400")
    assert part.mark_price == Decimal("130")

    # 4 ClosedLot slices: SOLO 1 + DUAL 2 + PART 1.
    assert len(closed_lots) == 4
    by_symbol: dict[str, list] = {}
    for cl in closed_lots:
        by_symbol.setdefault(str(cl.symbol), []).append(cl)
    assert len(by_symbol["SOLO"]) == 1
    assert len(by_symbol["DUAL"]) == 2
    assert len(by_symbol["PART"]) == 1
    # SOLO: full 10-share drain, basis 1000, realized 200.
    [solo] = by_symbol["SOLO"]
    assert solo.quantity == Decimal("10")
    assert solo.cost_basis_money == Decimal("1000")
    assert solo.proceeds_money - solo.cost_basis_money == Decimal("200")
    assert solo.proceeds_money == Decimal("1200")
    # DUAL: FIFO order 6 then 4; per-slice proceeds = slice_qty *
    # uniform close-per-share ($124 = $1240 total proceeds / 10 sh);
    # realized = proceeds - basis. Sum of realized = $200 (matches
    # IBKR's reported Realized P/L on the C row).
    dual_slices = sorted(by_symbol["DUAL"], key=lambda c: c.open_date_time)
    assert [s.quantity for s in dual_slices] == [Decimal("6"), Decimal("4")]
    assert dual_slices[0].cost_basis_money == Decimal("600")
    assert dual_slices[0].proceeds_money == Decimal("744")
    assert dual_slices[0].proceeds_money - dual_slices[0].cost_basis_money == Decimal("144")
    assert dual_slices[1].cost_basis_money == Decimal("440")
    assert dual_slices[1].proceeds_money == Decimal("496")
    assert dual_slices[1].proceeds_money - dual_slices[1].cost_basis_money == Decimal("56")
    # PART partial drain: 6 shares closed of 10.
    [part_cl] = by_symbol["PART"]
    assert part_cl.quantity == Decimal("6")
    assert part_cl.cost_basis_money == Decimal("600")


def test_activity_statement_dispatch_uses_all_trades_blocks_for_fifo_closures(
    tmp_path: Path,
) -> None:
    """The dispatcher must flatten every Trades block, not just [0].

    IBKR can repeat a section header to start a new block. This
    synthetic statement puts the O row in the first Trades block and
    the C row in the second; using only the first block would leave a
    phantom surviving open and fail the Dec-31 lookup.
    """
    statement = tmp_path / "multi_trades_blocks.csv"
    fii_header = (
        "Financial Instrument Information,Header,Asset Category,Symbol,Description,"
        "Conid,Security ID,Listing Exch,Multiplier,Type,Code"
    )
    fii_row = (
        "Financial Instrument Information,Data,Stocks,XQBB,SAMPLE ISSUER BB INC,"
        "12345,US99995B1044,NASDAQ,1,COMMON,"
    )
    trades_header = (
        "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,"
        "Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code"
    )
    open_row = (
        'Trades,Data,Order,Stocks,USD,XQBB,"2025-02-01, 10:30:00",10,100.00,'
        "100.00,-1000.00,0,1000.00,0,0,O"
    )
    close_row = (
        'Trades,Data,Order,Stocks,USD,XQBB,"2025-08-01, 10:30:00",-10,120.00,'
        "120.00,1200.00,0,-1000.00,200.00,0,C"
    )
    open_positions_header = (
        "Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,"
        "Mult,Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code"
    )
    statement.write_text(
        with_trades_totals(
            "\n".join(
                [
                    "Statement,Header,Field Name,Field Value",
                    'Statement,Data,Period,"January 1, 2025 - December 31, 2025"',
                    "Account Information,Header,Field Name,Field Value",
                    "Account Information,Data,Name,Sample Holder",
                    "Account Information,Data,Account,U12345678",
                    "Account Information,Data,Account Type,Individual",
                    "Account Information,Data,Customer Type,Individual",
                    "Account Information,Data,Account Capabilities,Cash",
                    "Account Information,Data,Base Currency,USD",
                    fii_header,
                    fii_row,
                    trades_header,
                    open_row,
                    trades_header,
                    close_row,
                    open_positions_header,
                ]
            )
            + "\n"
        ),
        encoding="utf-8",
    )

    bundle = _dispatch_activity_statement(
        statement,
        period_validator=functools.partial(require_calendar_year_period, calendar_year=2025),
    )
    open_lots, closed_lots = bundle.open_lots, bundle.closed_lots

    assert open_lots == []
    assert len(closed_lots) == 1
    [closed] = closed_lots
    assert closed.symbol == "XQBB"
    assert closed.quantity == Decimal("10")
    assert closed.cost_basis_money == Decimal("1000")
    assert closed.proceeds_money == Decimal("1200")


# ============================================================================
# End-to-end closure-only Activity Statement (issue #162)
# ============================================================================


def test_e2e_activity_statement_closure_only_no_open_positions_section(
    setup_e2e: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#162: an Activity Statement where every position opened in the
    calendar year was also fully closed in the year may legitimately
    omit the Open Positions section entirely (IBKR doesn't emit empty
    sections). The dispatcher must accept the file, the FA-A3 review
    CSV must list the closed lots as held-during-period rows, and the
    CLI must exit 3 (the standard manual-fill-warning exit code).

    The fixture has SOLO (single full-drain) + DUAL (FIFO two-slice
    full-drain) and **no Open Positions section at all**. Verifies
    that ``build_dec31_close_prices({})`` falling through to the
    empty dict doesn't trip the #133 ParseError path when no lot
    survives.
    """
    cfg, out = setup_e2e

    rc = main(
        [
            "schedule-fa",
            "--input",
            str(_CLOSURE_ONLY_NO_OP_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    assert rc == 3
    assert out.exists()
    captured = capsys.readouterr()
    # SOLO closed slice + DUAL closed slice 1 + DUAL closed slice 2 = 3 rows.
    assert "Wrote 3 Schedule FA Table A3 row(s)" in captured.out

    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 4  # header + 3 data rows
    symbols_seen = sorted(row[12] for row in rows[1:])
    assert symbols_seen == ["DUAL", "DUAL", "SOLO"]
    # Every row is a closed-lot row — disposal date populated.
    assert all(row[-1] != "" for row in rows[1:])

    # Cross-check the dispatcher directly: no surviving opens.
    bundle = _dispatch_activity_statement(
        _CLOSURE_ONLY_NO_OP_FIXTURE,
        period_validator=functools.partial(require_calendar_year_period, calendar_year=2025),
    )
    open_lots, closed_lots = bundle.open_lots, bundle.closed_lots
    assert open_lots == []
    assert len(closed_lots) == 3
    by_symbol = {str(cl.symbol): cl for cl in closed_lots if str(cl.symbol) == "SOLO"}
    solo = by_symbol["SOLO"]
    assert solo.quantity == Decimal("10")
    assert solo.cost_basis_money == Decimal("1000")
    assert solo.proceeds_money == Decimal("1200")


@pytest.mark.parametrize("discriminator", ["NewSummary", ""])
def test_closure_only_statement_refuses_a_held_row_of_an_unknown_kind(
    tmp_path: Path, discriminator: str
) -> None:
    """#1093: one 99-share EUR holding added to the closure-only statement.
    As a Summary row it refuses as not USD; as a row of an unknown kind both
    Open Positions readers skipped it, so the statement passed with no open
    lots and the currency and quantity checks never saw it."""
    marker = "Synthetic Marker Q"
    statement = tmp_path / "statement.csv"
    statement.write_text(
        _CLOSURE_ONLY_NO_OP_FIXTURE.read_text(encoding="utf-8")
        + "Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,"
        "Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code,,,\n"
        f"Open Positions,Data,{discriminator},Stocks,EUR,{HOLDER},99,1,{marker},990.00,11.00,"
        "1089.00,99.00,,,,\n",
        encoding="utf-8",
    )

    with pytest.raises(ParseError) as exc:
        _dispatch_activity_statement(
            statement,
            period_validator=functools.partial(require_calendar_year_period, calendar_year=2025),
        )
    error = exc.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "Open Positions",
        0,
        "DataDiscriminator",
        "",
    )
    assert error.message.startswith("must be 'Summary' or 'Lot', ")
    assert_no_markers(str(error), HOLDER, marker, *filter(None, [discriminator]))


def test_closure_only_statement_refuses_a_lot_row_without_a_summary_row(
    setup_e2e: tuple[Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1124: one Lot row, with no Summary row, added to the closure-only
    statement. Both Open Positions readers skipped Lot rows, so schedule-fa
    exited 3 and wrote output that left the holding out."""
    cfg, out = setup_e2e
    statement = tmp_path / "statement.csv"
    statement.write_text(
        _CLOSURE_ONLY_NO_OP_FIXTURE.read_text(encoding="utf-8")
        + "Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,"
        "Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code,,,\n"
        f"Open Positions,Data,Lot,Stocks,USD,{HOLDER},7.123457,1,10.00,71.23457,11.00,"
        "78.358027,7.123457,,,,\n",
        encoding="utf-8",
    )

    rc = main(
        [
            "schedule-fa",
            *("--input", str(statement), "--config", str(cfg)),
            *("--output", str(out), "--calendar-year", "2025"),
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "ParseError in section 'Open Positions' at row 0, column 'Symbol': this Lot row's "
        "Symbol has no Open Positions Summary row; refusing rather than dropping a holding "
        "that only the lot detail shows.\n"
    ) in captured.err
    assert not out.exists()
    assert_no_markers(captured.err, HOLDER, "7.123457")


def test_e2e_activity_statement_refuses_base_currency_with_trailing_line_break(
    setup_e2e: tuple[Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#835: a ``"USD\\n"`` Base Currency field is not a currency code; the
    parser refuses it (#1021)."""
    cfg, out = setup_e2e
    text = _FIXTURE.read_text(encoding="utf-8")
    cell = "Account Information,Data,Base Currency,USD,"
    assert text.count(cell) == 1
    statement = tmp_path / "statement.csv"
    statement.write_text(
        text.replace(cell, 'Account Information,Data,Base Currency,"USD\n",'), encoding="utf-8"
    )

    rc = main(
        [
            "schedule-fa",
            "--input",
            str(statement),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )

    assert rc == 4
    assert "ParseError in section 'Account Information' at row 0, column 'Base Currency'" in (
        capsys.readouterr().err
    )


def test_e2e_activity_statement_refuses_a_second_period_row(
    setup_e2e: tuple[Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1092: a March-only Period row after the full-year one is refused,
    not ignored, and the earlier output is removed. The CLI exits 4."""
    cfg, out = setup_e2e
    text = _FIXTURE.read_text(encoding="utf-8")
    row = 'Statement,Data,Period,"January 1, 2025 - December 31, 2025",,,,,,,,,,,,,\n'
    assert text.count(row) == 1
    statement = tmp_path / "statement.csv"
    statement.write_text(
        text.replace(row, f'{row}Statement,Data,Period,"March 1, 2025 - March 31, 2025"\n'),
        encoding="utf-8",
    )
    out.write_bytes(b"STALE")

    rc = main(
        [
            "schedule-fa",
            *("--input", str(statement), "--config", str(cfg)),
            *("--output", str(out), "--calendar-year", "2025"),
        ]
    )

    assert rc == 4
    assert (
        "ParseError in section 'Statement' at row 4, column 'Period': the Statement section "
        "has more than one Period row; refusing to choose one, because the period decides "
        "which events the report covers. Re-export the Activity Statement as one statement "
        "for one period.\n"
    ) in capsys.readouterr().err
    assert not out.exists()


# ---- rows dated outside the statement's own Period (#1143) ------------------

# The CY 2025 Period, in the forms a refusal could print it.
_PERIOD_TEXTS = ("January 1, 2025", "December 31, 2025", "2025-01-01", "2025-12-31")
# A CY 2025 statement with two dated rows in each dated section, each on the
# Period's first or last day. Every income row's Description but the interest
# credit's carries the holder marker; the Trades rows trade the synthetic
# symbol XQPD.
_PERIOD_EDGES = with_totals(
    "Statement,Header,Field Name,Field Value\n"
    "Statement,Data,BrokerName,Interactive Brokers LLC\n"
    "Statement,Data,Title,Activity Statement\n"
    'Statement,Data,Period,"January 1, 2025 - December 31, 2025"\n'
    "Account Information,Header,Field Name,Field Value\n"
    "Account Information,Data,Name,Sample Holder\n"
    "Account Information,Data,Account,U12345678\n"
    "Account Information,Data,Account Type,Individual\n"
    "Account Information,Data,Base Currency,USD\n"
    "Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,"
    "Security ID,Listing Exch,Multiplier,Type,Code\n"
    "Financial Instrument Information,Data,Stocks,XQPD,SAMPLE ISSUER PD INC,11111,"
    "US99995M1009,NASDAQ,1,COMMON,\n"
    "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,"
    "T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code\n"
    'Trades,Data,Order,Stocks,USD,XQPD,"2025-01-01, 09:30:00",10,100.00,100.00,'
    "-1000.00,0,1000.00,0,0,O\n"
    'Trades,Data,Order,Stocks,USD,XQPD,"2025-12-31, 15:30:00",-10,120.00,120.00,'
    "1200.00,0,-1000.00,200.00,0,C\n"
    "Dividends,Header,Currency,Date,Description,Amount\n"
    f"Dividends,Data,USD,01-01-2025,XQPD(US99995M1009) Cash Dividend USD 0.10 per Share "
    f"{HOLDER},1.00\n"
    f"Dividends,Data,USD,31-12-2025,XQPD(US99995M1009) Cash Dividend USD 0.20 per Share "
    f"{HOLDER},2.00\n"
    "Withholding Tax,Header,Currency,Date,Description,Amount,Code\n"
    f"Withholding Tax,Data,USD,01-01-2025,XQPD(US99995M1009) Cash Dividend USD 0.10 per "
    f"Share - US Tax {HOLDER},-0.30,\n"
    f"Withholding Tax,Data,USD,31-12-2025,XQPD(US99995M1009) Cash Dividend USD 0.20 per "
    f"Share - US Tax {HOLDER},-0.60,\n"
    "Interest,Header,Currency,Date,Description,Amount\n"
    # A credit needs IBKR's exact wording (#1280), so it carries no marker.
    "Interest,Data,USD,01-01-2025,USD Credit Interest for Dec-2024,1.23\n"
    f"Interest,Data,USD,31-12-2025,USD Debit Interest for Dec-2025 {HOLDER},-0.45\n"
)


def _dispatch_period_edges(path: Path) -> SourceBundle:
    return _dispatch_activity_statement(
        path,
        period_validator=functools.partial(require_calendar_year_period, calendar_year=2025),
    )


def test_dispatch_accepts_rows_on_the_periods_first_and_last_days(tmp_path: Path) -> None:
    """#1143: the Period is inclusive at both ends, in every dated section."""
    statement = tmp_path / "statement.csv"
    statement.write_text(_PERIOD_EDGES, encoding="utf-8")

    bundle = _dispatch_period_edges(statement)

    edges = [date(2025, 1, 1), date(2025, 12, 31)]
    [solo] = bundle.closed_lots
    assert [solo.open_date_time, solo.close_date_time] == edges
    assert [d.payment_date for d in bundle.dividends] == edges
    assert [w.payment_date for w in bundle.withholding_tax] == edges
    assert [c.credit_date for c in bundle.interest_credits] == edges[:1]
    assert [n.row_date for n in bundle.interest_non_credits] == edges[1:]


_TRADE = "Trades,Data,Order,Stocks,USD,XQPD,"


@pytest.mark.parametrize(
    ("row", "day", "moved", "section", "row_index", "column"),
    [
        (_TRADE, "2025-01-01", "2024-12-31", "Trades", 0, "Date/Time"),
        (_TRADE, "2025-12-31", "2026-01-01", "Trades", 1, "Date/Time"),
        ("Dividends,Data,USD,", "01-01-2025", "31-12-2024", "Dividends", 0, "Date"),
        ("Dividends,Data,USD,", "31-12-2025", "01-01-2026", "Dividends", 1, "Date"),
        ("Withholding Tax,Data,USD,", "31-12-2025", "01-01-2026", "Withholding Tax", 1, "Date"),
        ("Interest,Data,USD,", "01-01-2025", "31-12-2024", "Interest", 0, "Date"),
        ("Interest,Data,USD,", "31-12-2025", "01-01-2026", "Interest", 1, "Date"),
    ],
    ids=[
        "trades-open-before-start",
        "trades-close-after-end",
        "dividends-day-before-start",
        "dividends-day-after-end",
        "withholding-tax-after-end",
        "interest-credit-before-start",
        "interest-non-credit-after-end",
    ],
)
def test_dispatch_refuses_a_row_dated_outside_the_period(
    tmp_path: Path,
    row: str,
    day: str,
    moved: str,
    section: str,
    row_index: int,
    column: str,
) -> None:
    """#1143: a row dated a day outside the statement's Period refuses in
    the parser that read it, at the row and column its malformed-date
    refusal names, and echoes no cell, row date or Period date."""
    statement = tmp_path / "statement.csv"

    def dispatch_with(date_cell: str) -> ParseError:
        prefix = row + ('"' if section == "Trades" else "")
        assert _PERIOD_EDGES.count(prefix + day) == 1
        statement.write_text(_PERIOD_EDGES.replace(prefix + day, prefix + date_cell), "utf-8")
        with pytest.raises(ParseError) as exc:
            _dispatch_period_edges(statement)
        return exc.value

    error = dispatch_with(moved)
    malformed = dispatch_with("2025/06/30")

    assert (error.section, error.row_index, error.column) == (section, row_index, column)
    assert (malformed.section, malformed.row_index, malformed.column) == (
        section,
        row_index,
        column,
    )
    assert error.message == OUTSIDE_PERIOD
    assert error.raw_value == ""
    assert_no_markers(str(error), HOLDER, "XQPD", day, moved, *_PERIOD_TEXTS)


def test_schedule_fa_refuses_a_round_trip_dated_before_the_period(
    setup_e2e: tuple[Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1143: the closures fixture with its SOLO round trip moved to 2024
    under its CY 2025 Period refuses at the opening trade, before FIFO
    matching, and writes nothing. Before, A3 silently dropped SOLO."""
    cfg, out = setup_e2e
    text = _CLOSURES_FIXTURE.read_text(encoding="utf-8")
    for day in ("2025-02-01, 10:30:00", "2025-08-01, 10:30:00"):
        assert text.count(day) == 1
        text = text.replace(day, day.replace("2025", "2024"))
    statement = tmp_path / "statement.csv"
    statement.write_text(text, encoding="utf-8")

    rc = main(
        [
            "schedule-fa",
            *("--input", str(statement), "--config", str(cfg)),
            *("--output", str(out), "--calendar-year", "2025"),
        ]
    )

    assert rc == 4
    assert capsys.readouterr().err == (
        f"error: ParseError in section 'Trades' at row 0, column 'Date/Time': {OUTSIDE_PERIOD}\n"
    )
    assert not out.exists()


def _dated_fields(tp: object, path: str) -> Iterator[str]:
    """The path of every ``date`` or ``datetime`` field reachable from ``tp``."""
    if dataclasses.is_dataclass(tp):
        hints = typing.get_type_hints(tp)
        for field in dataclasses.fields(tp):
            yield from _dated_fields(hints[field.name], f"{path}.{field.name}")
    elif typing.get_origin(tp) in (types.UnionType, typing.Union):
        for arg in typing.get_args(tp):
            yield from _dated_fields(arg, path)
    elif typing.get_origin(tp) is not None:
        for arg in typing.get_args(tp):
            yield from _dated_fields(arg, f"{path}[]")
    elif isinstance(tp, type) and issubclass(tp, date):
        yield path


# Each date an Activity Statement's SourceBundle can carry, and what keeps it
# inside the statement's Period (#1143).
_BUNDLE_DATES = {
    "open_lots[].open_date_time": "Trades Date/Time, trades._parse_full_date_time",
    "open_lots[].report_date": "the Period's end, passed to match_fifo",
    "closed_lots[].open_date_time": "Trades Date/Time, trades._parse_full_date_time",
    "closed_lots[].close_date_time": "Trades Date/Time, trades._parse_full_date_time",
    "closed_lots[].holding_period_date_time": "Flex Query only; None here",
    "dividends[].payment_date": "Dividends Date, _dates.parse_statement_date",
    "dividends[].holding_date": "Robinhood only; None here",
    "withholding_tax[].payment_date": "Withholding Tax Date, _dates.parse_statement_date",
    "interest_credits[].credit_date": "Interest Date, _dates.parse_statement_date",
    "interest_non_credits[].row_date": "Interest Date, _dates.parse_statement_date",
}


def test_every_dated_bundle_field_has_a_period_check(tmp_path: Path) -> None:
    """#1143: a new dated field on a dispatcher output fails here until it
    is listed above with its period check. Fields that only other
    sources set stay empty on this path."""
    assert sorted(_dated_fields(SourceBundle, "")) == sorted(f".{p}" for p in _BUNDLE_DATES)

    statement = tmp_path / "statement.csv"
    statement.write_text(_PERIOD_EDGES, encoding="utf-8")
    bundle = _dispatch_period_edges(statement)
    assert [lot.holding_period_date_time for lot in bundle.closed_lots] == [None]
    assert [d.holding_date for d in bundle.dividends] == [None, None]


_OS_DIVIDENDS_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "e2e_activity_statement_schedule_os_with_dividends_fy2025_26.csv"
)
_DIVIDENDS_HEADER = "Dividends,Header,Currency,Date,Description,Amount,,,,,,,,,,,\n"
# A quote left open in an ignored section's row swallows every later section.
_OPEN_QUOTE_DEPOSIT = (
    "Deposits & Withdrawals,Header,Currency,Settle Date,Description,Amount\n"
    f"Deposits & Withdrawals,Data,USD,2025-02-03,{UNCLOSED},100.00\n"
)
_OVERSIZED_DEPOSIT = (
    "Deposits & Withdrawals,Header,Currency,Settle Date,Description,Amount\n"
    f"Deposits & Withdrawals,Data,USD,2025-02-03,{oversized_cell()},100.00\n"
)


@pytest.mark.parametrize(
    "deposit",
    [
        pytest.param(_OPEN_QUOTE_DEPOSIT, id="quote-left-open"),
        pytest.param(_OVERSIZED_DEPOSIT, id="oversized-cell"),
    ],
)
@pytest.mark.parametrize(
    ("fixture", "period", "line"),
    [
        pytest.param(
            _DIVIDENDS_FIXTURE, ("schedule-fa", "--calendar-year", "2025"), 19, id="schedule-fa"
        ),
        pytest.param(
            _OS_DIVIDENDS_FIXTURE,
            ("schedule-os", "--financial-year", "2025-26"),
            13,
            id="schedule-os",
        ),
    ],
)
def test_e2e_activity_statement_refuses_a_malformed_record(
    setup_e2e: tuple[Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    deposit: str,
    fixture: Path,
    period: tuple[str, str, str],
    line: int,
) -> None:
    """#1098: a quote left open in a Deposits & Withdrawals row used to
    swallow the Dividends section, so the statement loaded with no dividends
    and the run passed; a cell over the CSV field-size limit was an internal
    error (exit 1). Both now refuse with exit 4, and the earlier output is
    removed."""
    cfg, out = setup_e2e
    command, *period_option = period
    statement = tmp_path / "statement.csv"
    text = fixture.read_text(encoding="utf-8")
    assert text.count(_DIVIDENDS_HEADER) == 1
    statement.write_text(
        text.replace(_DIVIDENDS_HEADER, deposit + _DIVIDENDS_HEADER), encoding="utf-8"
    )
    out.write_bytes(b"STALE")

    rc = main(
        [
            command,
            *("--input", str(statement), "--config", str(cfg)),
            *("--output", str(out), *period_option),
        ]
    )

    assert rc == 4
    err = capsys.readouterr().err
    expected = malformed_message(line, "re-export the Activity Statement.")
    assert f"ParseError in section '<file>' at row {line}: {expected}\n" in err
    assert_no_markers(err, HOLDER, MARKER)
    assert not out.exists()


_CLOSURES_FIXTURE = Path(__file__).parent / "fixtures" / "e2e_activity_statement_closures_2025.csv"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(("schedule-os", "--financial-year", "2025-26"), id="schedule-os"),
        pytest.param(
            ("schedule-cg", "--itr-form", "ITR-2", "--financial-year", "2025-26"),
            id="schedule-cg",
        ),
    ],
)
def test_e2e_a_removed_round_trip_refuses_at_its_orphan_subtotal(
    setup_e2e: tuple[Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: tuple[str, ...],
) -> None:
    """#1090: the issue's example, one symbol's buy and sell removed with its
    SubTotal and the Total kept, so the ending holdings still reconcile. The
    splitter used to drop the totals and let each command go on to its own
    checks; every Activity Statement command, schedule-os included, now
    refuses at the orphan SubTotal first, and the earlier output is removed."""
    cfg, out = setup_e2e
    lines = _CLOSURES_FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)
    # Lines 20-21 are SOLO's buy and sell; its SubTotal is on line 22.
    assert [line.split(",")[:6] for line in lines[19:22]] == [
        ["Trades", "Data", "Order", "Stocks", "USD", "SOLO"],
        ["Trades", "Data", "Order", "Stocks", "USD", "SOLO"],
        ["Trades", "SubTotal", "", "Stocks", "USD", "SOLO"],
    ]
    statement = tmp_path / "statement.csv"
    statement.write_text("".join(lines[:19] + lines[21:]), encoding="utf-8")
    out.write_bytes(b"STALE")
    name, *options = command

    rc = main(
        [name, "--input", str(statement), "--config", str(cfg), "--output", str(out), *options]
    )

    assert rc == 4
    assert capsys.readouterr().err == (
        "error: ParseError in section 'Trades' at row 20: the 'SubTotal' row on line 20 has "
        "no rows above it since the previous 'SubTotal' row or the block's header. "
        f"{RE_EXPORT_HINT}\n"
    )
    assert not out.exists()


# ============================================================================
# End-to-end anonymised-statement fallback (issue #141)
# ============================================================================

_ANONYMISED_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_anonymised_2025.csv"
)


def test_e2e_activity_statement_anonymised_fails_without_flag(
    setup_e2e: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#141: an Activity Statement export with Name + Account rows
    hidden (IBKR's privacy option) is refused by default. The pre-#141
    safety net is preserved unless the user explicitly opts in via
    --accept-anonymised-statement.
    """
    cfg, out = setup_e2e

    rc = main(
        [
            "schedule-fa",
            "--input",
            str(_ANONYMISED_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    assert rc == 4
    captured = capsys.readouterr()
    assert "Account Information" in captured.err
    assert "--accept-anonymised-statement" in captured.err


def test_e2e_activity_statement_anonymised_succeeds_with_flag(
    setup_e2e: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#141: with --accept-anonymised-statement, the pipeline backfills
    the hidden Name + Account fields from user.toml, emits the FA-A3
    row, and surfaces a loud stderr warning that identity was
    user-attested rather than statement-verified.
    """
    cfg, out = setup_e2e

    rc = main(
        [
            "schedule-fa",
            "--input",
            str(_ANONYMISED_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--accept-anonymised-statement",
        ]
    )
    assert rc == 3
    assert out.exists()
    captured = capsys.readouterr()
    # SOLO single full-drain → 1 closed-lot row in FA-A3 output.
    assert "Wrote 1 Schedule FA Table A3 row(s)" in captured.out
    # Loud warning about fallback identity goes to stdout via _render_run_report.
    assert "Identity field(s)" in captured.out
    assert "'Account'" in captured.out
    assert "'Name'" in captured.out
    assert "user-attested" in captured.out
    assert "issues/141" in captured.out

    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 2  # header + 1 data row


_PREVIOUSLY_HELD_ORPHAN_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "e2e_activity_statement_previously_held_orphan_dividend_2025.csv"
)


def test_e2e_activity_statement_held_then_sold_in_period_is_not_orphan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#142: a synthetic XQBH round trip (bought 11/10, sold 12/05, dividend
    paid 12/29, all within the same CY) currently does NOT produce
    an orphan. The closed lot becomes a FA-A3 row (per PR #104 +
    PR #161), and the per-symbol dividend attribution matches the
    dividend to that row's column 10. So the issue's stated
    misleading-message bug isn't reachable in the current pipeline
    via this scenario.

    This test pins that behaviour: XQBH appears as a FA-A3 closed-
    lot row, the $1.50 dividend lands in its column 10, and no
    orphan-dividend advisory fires. The #142 classification logic
    remains forward-compatible: if a future change ever excludes
    closed lots from FA-A3 output (or makes partial-closure
    semantics produce dividend-bearing lots outside the overlap
    set), the previously_held branch will fire correctly with row
    context.
    """
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    report = run_fa_a3(
        input_path=_PREVIOUSLY_HELD_ORPHAN_FIXTURE,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    # 2 rows: XQBB open (still held Dec 31) + XQBH closed lot.
    assert report.rows_written == 2
    # No orphan: XQBH closed lot → XQBH FA-A3 row → XQBH dividend
    # attributed to that row's col 10.
    assert report.orphan_dividends == ()
    # XQBH's dividend lands in col 10 of the XQBH row (not blank,
    # not the XQBB row's value).
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    header = rows[0]
    symbol_idx = header.index("Name of entity")
    col10_idx = header.index(
        "Total gross amount paid/credited with respect to the holding during the period"
    )
    closed_row = next(r for r in rows[1:] if r[symbol_idx] == "SAMPLE ISSUER BH INC")
    assert closed_row[col10_idx] != "", "XQBH col 10 must be auto-filled with the $1.50 dividend"
    assert int(closed_row[col10_idx]) > 0
    # No orphan-advisory in CLI stdout (the render is in
    # _render_run_report which we don't invoke here directly, but
    # the empty orphan tuple is the canonical contract).
    capsys.readouterr()  # drain


# ============================================================================
# End-to-end Interest section parsing (issue #139)
# ============================================================================

_WITH_INTEREST_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_with_interest_2025.csv"
)


def test_e2e_activity_statement_interest_section_parsed_via_dispatcher(
    tmp_path: Path,
) -> None:
    """End-to-end: the with-interest fixture exercises the Interest
    section parser at the dispatcher level. Asserts that the
    SourceBundle's ``interest_credits`` attribute carries one
    :class:`~ibkr_fa.models.InterestCredit` per data row, that the
    section's per-section ``Total`` footer is filtered, and that
    chronological order + per-row source_row_index threading is
    preserved.

    Issue #139 scope: the parser layer only. Schedule OS interest-
    income aggregation (#32) and Schedule FSI per-source-country
    breakdown (#35) are compute-layer concerns tracked separately;
    this test verifies the dispatcher contract that downstream
    compute can rely on.
    """
    interest_credits = _dispatch_activity_statement(
        _WITH_INTEREST_FIXTURE,
        period_validator=functools.partial(require_calendar_year_period, calendar_year=2025),
    ).interest_credits

    # Three credit rows in the fixture (Apr / May / Jun 2025); the
    # per-section ``Total`` footer with Currency='Total' must NOT
    # produce a fourth InterestCredit.
    assert len(interest_credits) == 3

    # Chronological order preserved (matches the fixture's input
    # order, which is IBKR's chronological emission order).
    assert [c.credit_date for c in interest_credits] == [
        date(2025, 4, 30),
        date(2025, 5, 31),
        date(2025, 6, 30),
    ]

    # Per-credit fields round-trip from the fixture.
    apr, may, jun = interest_credits
    assert apr.amount_gross == Decimal("1.23")
    assert may.amount_gross == Decimal("1.45")
    assert jun.amount_gross == Decimal("1.67")
    for credit in interest_credits:
        assert credit.currency == "USD"
        assert credit.description.startswith("USD Credit Interest for ")

    # source_row_index is 0-based and matches the row position
    # within the Interest section's data rows (after splitter has
    # dropped the header row).
    assert [c.source_row_index for c in interest_credits] == [0, 1, 2]


# --- Issue #14: FA peak value populated from a fetched price series ---------


def test_e2e_fa_peak_value_populated_when_prices_fetched(
    setup_e2e: tuple[Path, Path],
) -> None:
    """With fetch_prices=True and a resolving fetcher, every FA row gets a
    peak value, so 'Peak value' drops off the manual-fill list. The fixture
    is a full-CY Activity Statement with no Dividends section, so the
    gross-amount column is set to 0 (PR #137 zero-when-sure), NOT manual-
    fill. Nature of entity remains manual-fill without per-ISIN evidence."""
    cfg, out = setup_e2e

    report = run_fa_a3(
        input_path=_FIXTURE,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
        fetch_prices=True,
        price_cache_dir=out.parent / ".price-cache",
        price_fetcher=flat_close_fetcher("100"),
        price_now=datetime(2026, 6, 1, tzinfo=UTC),
    )

    assert "Peak value of investment during the Period" not in report.manual_fill_columns
    # No Dividends section -> gross-amount inferred as 0 (not manual-fill).
    assert (
        "Total gross amount paid/credited with respect to the holding during the period"
        not in report.manual_fill_columns
    )
    assert report.fa_gross_amount_zeroed_no_cy_dividends is True
    # Peak computed + gross-amount zeroed; only entity identity remains (#1289).
    assert report.manual_fill_columns == ("Address of entity", "ZIP Code", "Nature of entity")

    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    # Peak value is portal column index 7 (see the snapshot test above).
    for data_row in rows[1:]:
        peak_cell = data_row[7]
        assert peak_cell != ""
        assert int(peak_cell) > 0
        # Gross-amount (portal column index 9) is the inferred zero.
        assert data_row[9] == "0"


def test_e2e_fa_peak_value_blank_when_a_session_close_is_missing(
    setup_e2e: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#939: XQBE's series lacks Wednesday 2025-06-04, a session. Its peak is
    left blank instead of carrying Tuesday's close (that peak sat below the
    closing value and floored to it); the XQBB lots keep theirs. The CLI exits 3."""
    cfg, out = setup_e2e
    flat = flat_close_fetcher("100")

    def _fetch(ticker, *, start, end, now, fetch_symbol=None):
        series = flat(ticker, start=start, end=end, now=now, fetch_symbol=fetch_symbol)
        if ticker != "XQBE":
            return series
        closes = {day: close for day, close in series.closes.items() if day != date(2025, 6, 4)}
        return replace(series, closes=closes)

    run = functools.partial(
        run_fa_a3, price_fetcher=_fetch, price_now=datetime(2026, 6, 1, tzinfo=UTC)
    )
    report = run(
        input_path=_FIXTURE,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
        fetch_prices=True,
        price_cache_dir=out.parent / ".price-cache",
    )

    assert "Peak value of investment during the Period" in report.manual_fill_columns
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    name = rows[0].index("Name of entity")
    peaks = [(row[name], row[7]) for row in rows[1:]]
    assert [entity for entity, peak in peaks if peak == ""] == ["SAMPLE ISSUER BE CORP"]
    assert all(int(peak) > 0 for entity, peak in peaks if entity != "SAMPLE ISSUER BE CORP")

    monkeypatch.setattr("ibkr_fa.cli.run_fa_a3", run)
    rc = main(
        [
            "schedule-fa",
            *("--input", str(_FIXTURE), "--config", str(cfg)),
            *("--output", str(out.parent / "cli.csv"), "--calendar-year", "2025"),
            *("--price-cache-dir", str(out.parent / ".cli-price-cache")),
        ]
    )
    assert rc == 3
    printed = capsys.readouterr().out
    assert "  - Peak value of investment during the Period" in printed
    # #962: the hint lists XQBE alone and says a session has no close (without
    # its date, #1048), never an override.
    assert (
        "   Peak value is blank for these holdings because a US market session in the "
        "holding window has no close: a dropped bar, a trading halt or delisting, or, in a "
        "year whose US market closures the tool doesn't list yet, a market holiday:\n"
        "     - XQBE (US99995D1000)\n"
        "       The peak is never guessed: enter it by hand."
    ) in printed
    assert "[[prices.ticker_overrides]]" not in printed
    assert "no USD daily series" not in printed


def test_e2e_fa_no_fetch_prices_hint_says_to_rerun_with_fetching(
    setup_e2e: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """#962: with --no-fetch-prices and no cache, each blank peak says to re-run
    without the flag, and no ticker override is suggested."""
    cfg, out = setup_e2e
    rc = main(
        [
            "schedule-fa",
            *("--input", str(_FIXTURE), "--config", str(cfg)),
            *("--output", str(out), "--calendar-year", "2025"),
            *("--price-cache-dir", str(out.parent / ".price-cache"), "--no-fetch-prices"),
        ]
    )

    assert rc == 3
    printed = capsys.readouterr().out
    assert (
        "   Peak value is blank for these holdings because price fetching was off "
        "(--no-fetch-prices) and no complete price cache was found:\n"
        "     - XQBB (US99995B1044)\n"
        "     - XQBE (US99995D1000)\n"
        "       Re-run without --no-fetch-prices to compute it.\n"
    ) in printed
    assert "[[prices.ticker_overrides]]" not in printed


def test_e2e_fa_refuses_a_price_cache_without_a_valid_hash(
    setup_e2e: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#940: each held ticker's cache is the example cache, with no
    payload_sha256 line and a 3 June close edited to $999. The run refuses
    instead of using it: no fetch, the earlier output is removed and the
    caches are unchanged. The CLI exits 4. The refusal names the cache
    folder, not the cache file, whose name is built from the Symbol cell
    (#1047)."""
    cfg, out = setup_e2e
    cache_dir = out.parent / ".price-cache"
    caches = [write_example_cache(cache_dir, ticker) for ticker in ("XQBB", "XQBE")]
    before = [cache.read_bytes() for cache in caches]
    fetched: list[Ticker] = []
    flat = flat_close_fetcher("100")

    def _fetch(ticker, *, start, end, now, fetch_symbol=None):
        fetched.append(ticker)
        return flat(ticker, start=start, end=end, now=now, fetch_symbol=fetch_symbol)

    run = functools.partial(
        run_fa_a3, price_fetcher=_fetch, price_now=datetime(2026, 6, 1, tzinfo=UTC)
    )
    out.write_bytes(b"STALE")
    with pytest.raises(PriceCacheError) as exc:
        run(
            input_path=_FIXTURE,
            config_path=cfg,
            output_path=out,
            calendar_year=2025,
            fetch_prices=True,
            price_cache_dir=cache_dir,
        )
    message = str(exc.value)
    assert message == (
        f"Price cache folder {cache_dir}: the cache file with one holding's prices has no "
        "payload_sha256 line. Move it aside so the next online run fetches a fresh one, "
        "or restore the original."
    )
    for symbol in ("XQBB", "XQBE"):
        assert symbol.lower() not in message.lower()
    assert not out.exists()

    monkeypatch.setattr("ibkr_fa.cli.run_fa_a3", run)
    cli_out = out.parent / "cli.csv"
    cli_out.write_bytes(b"STALE")
    rc = main(
        [
            "schedule-fa",
            *("--input", str(_FIXTURE), "--config", str(cfg)),
            *("--output", str(cli_out), "--calendar-year", "2025"),
            *("--price-cache-dir", str(cache_dir)),
        ]
    )
    assert rc == 4
    assert f"error: {exc.value}" in capsys.readouterr().err
    assert not cli_out.exists()
    assert fetched == []
    assert [cache.read_bytes() for cache in caches] == before


def test_e2e_fa_refuses_two_price_bars_on_one_date(
    setup_e2e: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#942: the fetcher runs the Yahoo parser on a response with two bars on
    2 June 2025, $100 then $10. The run refuses instead of keeping the $10 bar:
    the earlier output is removed and no price cache is written. The CLI exits 4.
    The error keeps neither the holding's symbol nor the date (#1368)."""
    cfg, out = setup_e2e
    cache_dir = out.parent / ".price-cache"
    run = functools.partial(
        run_fa_a3, price_fetcher=repeated_date_fetcher, price_now=datetime(2026, 6, 1, tzinfo=UTC)
    )
    out.write_bytes(b"STALE")
    with pytest.raises(PriceSourceContradictionError) as exc:
        run(
            input_path=_FIXTURE,
            config_path=cfg,
            output_path=out,
            calendar_year=2025,
            fetch_prices=True,
            price_cache_dir=cache_dir,
        )
    assert str(exc.value) == (
        "Yahoo returned two price bars for one date for a holding; the price source "
        "contradicts itself, so its prices are refused"
    )
    assert_prints_no_marker(exc.value, markers=("XQBB", "2025-06-02", "2025, 6, 2"))
    assert exc.value == PriceSourceContradictionError("Yahoo")
    assert not out.exists()

    monkeypatch.setattr("ibkr_fa.cli.run_fa_a3", run)
    cli_out = out.parent / "cli.csv"
    cli_out.write_bytes(b"STALE")
    rc = main(
        [
            "schedule-fa",
            *("--input", str(_FIXTURE), "--config", str(cfg)),
            *("--output", str(cli_out), "--calendar-year", "2025"),
            *("--price-cache-dir", str(cache_dir)),
        ]
    )
    assert rc == 4
    assert f"error: {exc.value}" in capsys.readouterr().err
    assert not cli_out.exists()
    assert not cache_dir.exists()


def test_e2e_fa_refuses_a_price_response_with_no_exchange_zone(
    setup_e2e: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1139: the default fetcher gets a Yahoo response with no exchange zone.
    The run refuses after one request, writing no price cache and no FA
    output, and the CLI exits 4 with the fixed message."""
    cfg, out = setup_e2e
    cache_dir = out.parent / ".price-cache"
    urls = serve_no_zone_response(monkeypatch)
    with pytest.raises(YahooResponseFormatError) as exc:
        run_fa_a3(
            input_path=_FIXTURE,
            config_path=cfg,
            output_path=out,
            calendar_year=2025,
            fetch_prices=True,
            price_cache_dir=cache_dir,
        )
    assert str(exc.value) == NO_ZONE_REFUSAL
    assert len(urls) == 1
    assert not out.exists()
    assert not cache_dir.exists()

    cli_out = out.parent / "cli.csv"
    rc = main(
        [
            "schedule-fa",
            *("--input", str(_FIXTURE), "--config", str(cfg)),
            *("--output", str(cli_out), "--calendar-year", "2025"),
            *("--price-cache-dir", str(cache_dir)),
        ]
    )
    assert rc == 4
    assert capsys.readouterr().err == f"error: {NO_ZONE_REFUSAL}\n"
    assert len(urls) == 2
    assert not cli_out.exists()
    assert not cache_dir.exists()


def test_e2e_fa_peak_value_blank_when_fetch_disabled(
    setup_e2e: tuple[Path, Path],
) -> None:
    """fetch_prices=False keeps peak manual-fill (pre-#14 behaviour)."""
    cfg, out = setup_e2e
    report = run_fa_a3(
        input_path=_FIXTURE,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
        fetch_prices=False,
    )
    assert "Peak value of investment during the Period" in report.manual_fill_columns
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    for data_row in rows[1:]:
        assert data_row[7] == ""
