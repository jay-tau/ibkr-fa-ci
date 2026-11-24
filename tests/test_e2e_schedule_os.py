"""End-to-end test for the Schedule OS pipeline via the CLI.

FY-based Activity Statement e2e for the ``schedule-os`` subcommand,
mirroring :mod:`tests.test_e2e_activity_statement` (which covers the
CY-based ``schedule-fa`` pipeline). Each test wires the real CLI
``main(argv)`` against a synthetic FY 2025-26 Activity Statement
fixture and asserts on stdout/stderr, exit code, and the resulting
review CSV.

Fixtures live in ``tests/fixtures/e2e_activity_statement_schedule_os_*``
and cover four payload shapes:

* zero-dividends happy path (buy-and-hold; no Dividends section);
* 3 US-source ordinary dividends across the FY chosen to exercise
  Rule 115(1), Explanation (2)(e) carry-back edges (Sun→Sat weekend gaps + FY-first-day
  exact-archive hit);
* mixed foreign-source + Indian-source (ISIN prefix ``IN``);
* mixed foreign-source + non-ordinary distribution (``Return Of
  Capital``), which refuses since #1310.

Dates are pinned to FY 2025-26 because the vendored SBI TTBR archive
covers Apr 1 2025 → May 2 2026.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

from ibkr_fa.output.portal_labels import (
    PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND,
    PORTAL_FIELD_LABEL_OS_OTHER_INTEREST,
)
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_os import run_schedule_os
from tests._activity_statement_e2e import OUTSIDE_PERIOD
from tests._change_in_nav_checks import RE_EXPORT_HINT
from tests._income_blocks import with_totals
from tests._schedule_os_e2e import SCHEDULE_OS_DIVIDENDS_STATEMENT as _DIVIDENDS_FIXTURE
from tests._schedule_os_e2e import SCHEDULE_OS_USER_TOML as _USER_TOML
from tests._schedule_os_e2e import run_schedule_os_cli as _run_cli
from tests._schedule_os_totals import total_rows

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_ZERO_FIXTURE = _FIXTURES_DIR / "e2e_activity_statement_schedule_os_zero_dividends_fy2025_26.csv"
_INDIAN_SOURCE_FIXTURE = (
    _FIXTURES_DIR / "e2e_activity_statement_schedule_os_with_indian_source_fy2025_26.csv"
)
_NON_ORDINARY_FIXTURE = (
    _FIXTURES_DIR / "e2e_activity_statement_schedule_os_with_non_ordinary_fy2025_26.csv"
)
_INTEREST_FIXTURE = _FIXTURES_DIR / "e2e_activity_statement_schedule_os_with_interest_fy2025_26.csv"
_ISO_DATES_FIXTURE = _FIXTURES_DIR / "e2e_activity_statement_schedule_os_iso_dates_fy2025_26.csv"
_CY_DIVIDENDS_FIXTURE = _FIXTURES_DIR / "e2e_activity_statement_with_dividends_2025.csv"
_FLEX_FIXTURE = _FIXTURES_DIR / "e2e_year_end_2025.csv"

_ALL_OS_FIXTURES = (
    _ZERO_FIXTURE,
    _DIVIDENDS_FIXTURE,
    _INDIAN_SOURCE_FIXTURE,
    _NON_ORDINARY_FIXTURE,
)


@pytest.fixture
def setup_e2e(tmp_path: Path) -> tuple[Path, Path]:
    """Write user.toml in tmp_path and return (config_path, output_path)."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "schedule_os_review.csv"
    return cfg, out


def _read_csv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


def test_e2e_zero_dividends_happy_path_emits_zero_total_csv(
    setup_e2e: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Buy-and-hold zero-dividends fixture: CLI exits 0, writer emits
    a zero-total review CSV, stderr surfaces the no-qualifying info
    banner, and the 1a(i) portal total row and the 1b(ix) interest
    total row after it both end in ``"0"``."""
    cfg, out = setup_e2e

    rc = _run_cli(_ZERO_FIXTURE, cfg, out)

    assert rc == 0
    assert out.exists()

    captured = capsys.readouterr()
    assert ("Wrote Schedule OS review to ") in captured.out
    assert (
        "with 0 qualifying ordinary dividend payment(s) (0 foreign-source, 0 Indian-source)"
    ) in captured.out
    assert "TOTAL 1a(i) ordinary dividends (rounded ₹): 0" in captured.out
    # T1-r9 quality lane PR #159 P2: end-to-end real-CLI exercise of
    # the schedule-os account-ID masking on stdout. Fixture account
    # is U12345678; masked form is U***5678.
    assert "Account: U***5678" in captured.out
    assert "U12345678" not in captured.out
    assert "No qualifying ordinary dividends found in FY 2025-26" in captured.err

    rows = _read_csv(out)
    assert rows, "review CSV must not be empty"
    for i, row in enumerate(rows, start=1):
        assert len(row) == 11, f"row {i} has {len(row)} cols, want 11"

    _, portal, interest = total_rows(rows)
    assert rows[portal][-1] == "0"
    assert rows[interest][-1] == "0"


def test_e2e_relative_output_prints_as_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A relative ``--output`` prints as typed, never resolved against the
    working folder (#1071)."""
    (tmp_path / "user.toml").write_text(_USER_TOML, encoding="utf-8")
    (tmp_path / "out").mkdir()
    monkeypatch.chdir(tmp_path)

    rc = _run_cli(_ZERO_FIXTURE, Path("user.toml"), Path("out/os.csv"))

    assert rc == 0
    assert (tmp_path / "out" / "os.csv").exists()
    captured = capsys.readouterr()
    assert "Wrote Schedule OS review to out/os.csv with " in captured.out
    assert str(tmp_path) not in captured.out + captured.err


def test_e2e_with_dividends_three_us_source_payments(
    setup_e2e: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Three US-source ordinary dividends: stdout reports "3
    qualifying", stderr is empty (no warnings for FY 2025-26 + unmasked
    account + non-zero dividends), and the review CSV's 1a(i) portal
    TOTAL row is > 0."""
    cfg, out = setup_e2e

    rc = _run_cli(_DIVIDENDS_FIXTURE, cfg, out)

    assert rc == 0
    captured = capsys.readouterr()
    assert ("Wrote Schedule OS review to ") in captured.out
    assert (
        "with 3 qualifying ordinary dividend payment(s) (3 foreign-source, 0 Indian-source)"
    ) in captured.out
    assert "TOTAL 1a(i) ordinary dividends (rounded ₹):" in captured.out
    # T1-r9 quality lane PR #159 P2: parallel masking assertion for
    # the dividends path; same fixture account U12345678 → U***5678.
    assert "Account: U***5678" in captured.out
    assert "U12345678" not in captured.out
    # #63: BMA disclosure advisory always fires on stderr now; other
    # safety warnings should still be absent for a happy-path run.
    assert "Black Money Act disclosure reminder" in captured.err
    assert "Account ID cross-check was SKIPPED" not in captured.err
    assert "AY 2027-28" not in captured.err

    rows = _read_csv(out)
    # header (1) + 3 detail rows + audit-total row + portal-label TOTAL row
    # = at least 6 rows.
    assert len(rows) >= 6
    for i, row in enumerate(rows, start=1):
        assert len(row) == 11, f"row {i} has {len(row)} cols, want 11"

    _, portal, _ = total_rows(rows)
    assert int(rows[portal][-1]) > 0

    # Spot-check a detail row: XQBB Sep 15 2025 payment, USD 2.10.
    xqbb_rows = [r for r in rows[1:] if r[0] == "XQBB"]
    assert len(xqbb_rows) == 1
    detail_row = xqbb_rows[0]
    assert detail_row[1] == "US99995B1044"
    assert detail_row[2] == "US"
    assert detail_row[3] == "15/09/2025"
    assert detail_row[5] == "2.10"


def test_e2e_iso_yyyy_mm_dd_dates_parse_through_full_pipeline(
    setup_e2e: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: an IBKR-LLC / US-flavor Activity Statement whose
    Dividends, Withholding Tax, AND Interest sections carry ISO
    ``YYYY-MM-DD`` dates (as real statements do) must parse
    end-to-end. Before the shared ``_dates`` helper, the Dividends and
    Withholding Tax parsers accepted only DD-MM-YYYY and raised
    ParseError mid-pipeline, blocking the whole run (the splitter
    parses Dividends before Interest, so dual-format Interest alone did
    not unblock it). All three ISO-dated sections now flow through
    splitter -> pipeline -> writer. A non-zero exit here would mean an
    ISO date in any consumed section still fails to parse."""
    cfg, out = setup_e2e

    rc = _run_cli(_ISO_DATES_FIXTURE, cfg, out)

    assert rc == 0
    captured = capsys.readouterr()
    assert (
        "with 3 qualifying ordinary dividend payment(s) (3 foreign-source, 0 Indian-source)"
    ) in captured.out
    # 1b(ix) interest income parsed from the ISO-dated Interest section
    # and converted to a positive INR total (not the empty-section 0).
    assert PORTAL_FIELD_LABEL_OS_OTHER_INTEREST in captured.out
    assert f"{PORTAL_FIELD_LABEL_OS_OTHER_INTEREST}: ₹0" not in captured.out

    # The ISO input "2025-09-15" is parsed and re-rendered in the
    # portal's DD/MM/YYYY display form, proving the date round-tripped.
    rows = _read_csv(out)
    xqbb_rows = [r for r in rows[1:] if r[0] == "XQBB"]
    assert len(xqbb_rows) == 1
    assert xqbb_rows[0][3] == "15/09/2025"


def test_e2e_dividends_fixture_fy_boundary_carry_backs_resolve(
    setup_e2e: tuple[Path, Path],
) -> None:
    """Direct pipeline call (not CLI): Apr 1 FY-first-day + Sep 15 +
    Dec 15 specified-date lookups all resolve through Rule115Policy
    carry-back without raising MissingFxRateError.

    Locks Rule 115(1), Explanation (2)(e) end-to-end: the preceding-month-end "specified
    date" lands on Mar 31 (Mon, India FY-end bank holiday — Mar 30 Sun
    also absent, so the bundled SBI TTBR archive carries back to Sat
    Mar 29), Aug 31 (Sun → Sat Aug 30) and Nov 30 (Sun → Sat Nov 29) —
    all three must walk back to an earlier available SBI rate."""
    cfg, out = setup_e2e

    report = run_schedule_os(
        input_path=_DIVIDENDS_FIXTURE,
        config_path=cfg,
        output_path=out,
        financial_year=FinancialYear(start_year=2025),
    )

    assert report.ordinary_dividend_rows_written == 3
    assert report.indian_source_dividend_rows_written == 0
    assert report.ordinary_dividend_total_inr_rounded > 0


def test_e2e_mixed_source_dividends_share_one_1a_i_total(
    setup_e2e: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Indian and foreign details retain country but share one total."""
    cfg, out = setup_e2e

    rc = _run_cli(_INDIAN_SOURCE_FIXTURE, cfg, out)

    assert rc == 0
    captured = capsys.readouterr()
    assert "2 qualifying ordinary dividend payment(s) (1 foreign-source, 1 Indian-source)" in (
        captured.out
    )

    rows = _read_csv(out)
    flat_first_cells = [r[0] for r in rows if r]
    assert flat_first_cells.count(PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND) == 1
    detail_countries = {r[0]: r[2] for r in rows if r and r[0] in {"XQBF", "XQBB"}}
    assert detail_countries == {"XQBF": "IN", "XQBB": "US"}
    _, portal, _ = total_rows(rows)
    assert int(rows[portal][-1]) > 0


def test_e2e_review_csv_interest_total_matches_stdout(
    setup_e2e: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The review CSV's 1b(ix) amount is the one the stdout 1b(ix) line prints."""
    cfg, out = setup_e2e

    rc = _run_cli(_INTEREST_FIXTURE, cfg, out, "2025-26", "--format", "review-csv")

    assert rc == 0
    printed = re.findall(
        rf"^  {re.escape(PORTAL_FIELD_LABEL_OS_OTHER_INTEREST)}: ₹(\d+) \(from ",
        capsys.readouterr().out,
        flags=re.MULTILINE,
    )
    assert printed != ["0"]
    rows = _read_csv(out)
    _, _, interest = total_rows(rows)
    assert [rows[interest][-1]] == printed


def test_e2e_non_ordinary_distribution_refuses(
    setup_e2e: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """XQBA's in-FY ``Return Of Capital`` (Dividends-section row index 1)
    refuses with exit 4 and fixed text (#1310): no stdout, and an earlier
    run's output is removed. It used to route to the
    ``_excluded_distributions:`` audit block."""
    cfg, out = setup_e2e
    out.write_bytes(b"STALE")

    rc = _run_cli(_NON_ORDINARY_FIXTURE, cfg, out)

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "error: Return Of Capital in the Dividends section (Dividends-section row"
        " index 1; 0-based, so data row 2): ibkr-fa can't yet file an IBKR return"
        " of capital, long-term capital gain distribution or cash in lieu."
        " Skipping it, or counting it as a dividend, could misstate your income,"
        " capital gains, foreign tax credit or Schedule FA, so this run stops."
        " Locate the row in your IBKR Activity Statement CSV by its"
        " Dividends-section row index.\n"
    )
    for marker in ("XQBA", "US99995C1027", "0.50", "4.00", "30-09-2025"):
        assert marker in _NON_ORDINARY_FIXTURE.read_text(encoding="utf-8")
        assert marker not in captured.err
    assert not out.exists()


def test_e2e_partial_fy_period_rejected(
    setup_e2e: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CY-aligned (Jan 1 - Dec 31 2025) fixture cannot satisfy
    ``--financial-year 2025-26``: its period doesn't contain the FY, so
    the validator rejects with exit code 4 (domain error), a stderr
    message naming Schedule OS, and no output."""
    cfg, out = setup_e2e

    rc = _run_cli(_CY_DIVIDENDS_FIXTURE, cfg, out)

    assert rc == 4
    captured = capsys.readouterr()
    assert "does not fully contain the Indian financial year 2025-26" in captured.err
    assert "Schedule OS dividend reporting" in captured.err
    assert not out.exists()


# ---- a statement whose period contains the FY (#1284) ----------------------------

_EXACT_PERIOD = "April 1, 2025 - March 31, 2026"
_CASH = "XQBB(US99995B1044) Cash Dividend USD 1.00 per Share (Ordinary Dividend)"
# The issue's repro: one USD 100 dividend withheld USD 25 on 2025-09-12. At the
# 2025-08-31 TTBR of 87.70 it files 1a(i) = 8770.
_DIVIDEND = f"Dividends,Data,USD,12-09-2025,{_CASH},100.00\n"
_WITHHOLDING = f"Withholding Tax,Data,USD,12-09-2025,{_CASH} - US Tax,-25.00,\n"
_INTEREST = "Interest,Data,USD,30-04-2025,USD Credit Interest for Apr-2025,1.23\n"


def _statement(
    period: str,
    dividends: tuple[str, ...] = (_DIVIDEND,),
    interest: tuple[str, ...] = (),
) -> str:
    """The zero-dividends fixture's Statement and Account Information under
    ``period``, then ``dividends``, :data:`_WITHHOLDING`, ``interest`` and
    IBKR's ``Total`` rows."""
    text = _ZERO_FIXTURE.read_text(encoding="utf-8")
    old = f'Statement,Data,Period,"{_EXACT_PERIOD}"'
    assert text.count(old) == 1
    text = text.replace(old, f'Statement,Data,Period,"{period}"')
    text += "Dividends,Header,Currency,Date,Description,Amount\n" + "".join(dividends)
    text += "Withholding Tax,Header,Currency,Date,Description,Amount,Code\n" + _WITHHOLDING
    if interest:
        text += "Interest,Header,Currency,Date,Description,Amount\n" + "".join(interest)
    return with_totals(text)


def _run_statement(
    run_dir: Path, capsys: pytest.CaptureFixture[str], text: str
) -> tuple[int, str, str, bytes | None]:
    """``schedule-os`` on ``text``: its exit code, stdout and stderr with
    ``run_dir`` masked, and the output file's bytes if it was written."""
    run_dir.mkdir()
    statement = run_dir / "statement.csv"
    statement.write_text(text, encoding="utf-8")
    cfg = run_dir / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = run_dir / "schedule-os.csv"
    capsys.readouterr()
    rc = _run_cli(statement, cfg, out)
    captured = capsys.readouterr()
    return (
        rc,
        captured.out.replace(str(run_dir), "<run>"),
        captured.err.replace(str(run_dir), "<run>"),
        out.read_bytes() if out.exists() else None,
    )


@pytest.mark.parametrize(
    "period",
    [
        pytest.param("April 1, 2024 - March 31, 2026", id="starts-earlier"),
        pytest.param("April 1, 2025 - June 30, 2026", id="ends-later"),
        pytest.param("January 1, 2020 - June 30, 2026", id="both"),
    ],
)
def test_e2e_statement_containing_the_fy_files_as_the_exact_year_statement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], period: str
) -> None:
    """#1284: Schedule OS accepts a statement whose period contains the FY,
    as ``schedule-fsi`` does, and gives exactly the exact-year statement's
    exit code, streams and file. Before, it refused the period with exit 4."""
    exact = _run_statement(tmp_path / "exact", capsys, _statement(_EXACT_PERIOD))
    assert exact[0] == 0
    assert exact[3] is not None
    rows = list(csv.reader(exact[3].decode("utf-8-sig").splitlines()))
    _, portal, _ = total_rows(rows)
    assert rows[portal][-1] == "8770"

    assert _run_statement(tmp_path / "wider", capsys, _statement(period)) == exact


_OUT_OF_FY_NOTE = "Silently filtered 1 InterestCredit row(s) outside FY 2025-26"


@pytest.mark.parametrize(
    ("period", "dividend_day", "interest_day", "interest_month"),
    [
        pytest.param(
            "April 1, 2024 - March 31, 2026", "12-09-2024", "30-09-2024", "Sep", id="before-the-fy"
        ),
        pytest.param(
            "April 1, 2025 - June 30, 2026", "12-05-2026", "30-04-2026", "Apr", id="after-the-fy"
        ),
    ],
)
def test_e2e_out_of_fy_rows_in_a_wider_statement_are_not_filed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    period: str,
    dividend_day: str,
    interest_day: str,
    interest_month: str,
) -> None:
    """#1284: a wider statement's out-of-FY dividend and interest credit are
    left out. The file and stdout are the exact-year statement's, and stderr
    only adds the out-of-year interest note."""
    exact = _run_statement(
        tmp_path / "exact", capsys, _statement(_EXACT_PERIOD, interest=(_INTEREST,))
    )
    assert exact[0] == 0
    year = interest_day[-4:]
    dividend = f"Dividends,Data,USD,{dividend_day},{_CASH},50.00\n"
    interest = (
        f"Interest,Data,USD,{interest_day},USD Credit Interest for {interest_month}-{year},2.00\n"
    )
    before = dividend_day.endswith("2024")
    text = _statement(
        period,
        dividends=(dividend, _DIVIDEND) if before else (_DIVIDEND, dividend),
        interest=(interest, _INTEREST) if before else (_INTEREST, interest),
    )

    wider = _run_statement(tmp_path / "wider", capsys, text)

    assert wider[0] == 0
    assert wider[1] == exact[1]
    assert wider[3] == exact[3]
    assert _OUT_OF_FY_NOTE not in exact[2]
    # The note is one paragraph; without it, stderr is the exact-year run's.
    note_start = wider[2].rindex("\n", 0, wider[2].index(_OUT_OF_FY_NOTE)) + 1
    note_end = wider[2].index("\n\n", note_start) + 2
    assert wider[2][:note_start] + wider[2][note_end:] == exact[2]


def test_e2e_dividends_dated_before_the_period_refuse(
    setup_e2e: tuple[Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1143: the dividends fixture with its three payment dates moved into
    2024, under its unchanged FY 2025-26 Period and Total, refuses at the
    first moved row and writes nothing. Before, the run exited 0 with a
    1a(i) total of 0."""
    cfg, out = setup_e2e
    text = _DIVIDENDS_FIXTURE.read_text(encoding="utf-8")
    for day in ("01-04-2025", "15-09-2025", "15-12-2025"):
        assert text.count(f",{day},") == 1
        text = text.replace(f",{day},", f",{day[:6]}2024,")
    statement = tmp_path / "statement.csv"
    statement.write_text(text, encoding="utf-8")

    rc = _run_cli(statement, cfg, out)

    assert rc == 4
    assert capsys.readouterr().err == (
        f"error: ParseError in section 'Dividends' at row 0, column 'Date': {OUTSIDE_PERIOD}\n"
    )
    assert not out.exists()


@pytest.mark.parametrize(
    ("printed", "refuses"),
    [pytest.param("150.00", False, id="whole-period"), pytest.param("100.00", True, id="fy-only")],
)
def test_e2e_change_in_nav_on_a_wider_statement_covers_its_whole_period(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], printed: str, refuses: bool
) -> None:
    """#1279's check compares the statement's own ``Change in NAV``, which
    covers its whole period, with every row, so on a wider statement the
    out-of-FY dividend counts: a ``Dividends`` line holding only the FY's
    dividend refuses, and one holding both passes (#1284)."""
    earlier = f"Dividends,Data,USD,12-09-2024,{_CASH},50.00\n"
    change_in_nav = (
        "Change in NAV,Header,Field Name,Field Value\n"
        "Change in NAV,Data,Starting Value,0\n"
        f"Change in NAV,Data,Dividends,{printed}\n"
        "Change in NAV,Data,Withholding Tax,-25.00\n"
        "Change in NAV,Data,Ending Value,125.00\n"
    )
    text = _statement("April 1, 2024 - March 31, 2026", dividends=(earlier, _DIVIDEND))

    result = _run_statement(tmp_path / "run", capsys, text + change_in_nav)

    if refuses:
        assert result[:2] == (4, "")
        assert result[2] == (
            "error: the Activity Statement's Change in NAV 'Dividends' amount (zero when "
            "the line isn't printed) differs by more than 0.01 from the total of the "
            f"'Dividends' section. A row may be missing or changed. {RE_EXPORT_HINT}\n"
        )
        assert result[3] is None
    else:
        assert result[0] == 0
        assert result[3] is not None
        rows = list(csv.reader(result[3].decode("utf-8-sig").splitlines()))
        _, portal, _ = total_rows(rows)
        assert rows[portal][-1] == "8770"


def test_e2e_flex_query_input_rejected(
    setup_e2e: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Flex Query inputs are unsupported for Schedule OS (sister gap
    to #138). The dispatcher rejects with exit 4 and a stderr message
    mentioning Activity Statement / Flex Query."""
    cfg, out = setup_e2e

    rc = _run_cli(_FLEX_FIXTURE, cfg, out)

    assert rc == 4
    captured = capsys.readouterr()
    assert "Activity Statement" in captured.err or "Flex Query" in captured.err


def test_e2e_fixtures_have_required_sections() -> None:
    """Defence-in-depth: every OS fixture must carry the Statement
    + Account Information headers the OS dispatcher's
    ``_OS_DEFAULT_REQUIRED_ACTIVITY_SECTIONS`` mandates."""
    for fixture in _ALL_OS_FIXTURES:
        text = fixture.read_text(encoding="utf-8")
        assert "Statement,Header,Field Name,Field Value" in text, (
            f"{fixture.name} missing Statement header row"
        )
        assert "Account Information,Header,Field Name,Field Value" in text, (
            f"{fixture.name} missing Account Information header row"
        )
