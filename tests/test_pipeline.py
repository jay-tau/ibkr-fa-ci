"""Tests for :mod:`ibkr_fa.pipeline`.

The pipeline pulls together every previously-tested layer, so these
tests focus on the wiring contract rather than re-testing each
layer's internals. End-to-end testing against the e2e fixture is
the responsibility of a separate PR (e2e-test).
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa import pipeline_dispatch
from ibkr_fa.cli import main
from ibkr_fa.compute.lot_matching import BrokerBasisError
from ibkr_fa.exceptions import (
    ConfigError,
    InvariantError,
    LotMatchingError,
    MissingFxRateError,
    ParseError,
    PathRefusalError,
)
from ibkr_fa.fx import policy_fa, ttbr_loader
from ibkr_fa.models import (
    AccountInfo,
    Dividend,
    OpenLot,
    Side,
    SourceBundle,
    StatementPeriod,
)
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.output.base import Subcommand, UnknownOutputFormatError
from ibkr_fa.parser.activity_statement import (
    KnownActivitySection,
    split_activity_statement_sections,
)
from ibkr_fa.pii import display_account_id, is_masked_account_id
from ibkr_fa.pipeline import (
    IbkrCsvFormat,
    _check_fx_policy_for_ay,
)
from ibkr_fa.pipeline_dispatch import (
    _OS_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
    _cross_check_account_id,
    _dispatch_activity_statement,
)
from ibkr_fa.pipeline_fa import (
    MANUAL_FILL_PORTAL_COLUMNS,
    UnsupportedCalendarYearError,
    run_fa_a3,
)
from ibkr_fa.pipeline_fa_models import OrphanDividendInfo, RunReport
from ibkr_fa.pipeline_fa_prices import TickerIsinCollisionError
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.reference.us_market_calendar import us_market_sessions
from ibkr_fa.types import AccountId, CountryCode, Isin, Ticker
from tests._crash_reports import assert_no_markers, assert_unchained_refusal
from tests._flex_section_rows import OPEN_POSITIONS_HEADER, TRADES_LOT_DETAIL_HEADER
from tests._ibkr_account_refusals import ACCOUNT_MISMATCH_REFUSAL, MASKED_ACCOUNT_REFUSAL
from tests._ibkr_fa_sums_inputs import run_schedule_fa_cy2025 as _run_schedule_fa
from tests._ibkr_pipeline_inputs import PIPELINE_ACCOUNT_INFO_SECTION as _ACCOUNT_INFO_SECTION
from tests._ibkr_pipeline_inputs import (
    PIPELINE_AS_ACCOUNT_INFORMATION_SECTION as _AS_ACCOUNT_INFORMATION_SECTION,
)
from tests._ibkr_pipeline_inputs import (
    PIPELINE_AS_FINANCIAL_INSTRUMENT_INFO_SECTION as _AS_FINANCIAL_INSTRUMENT_INFO_SECTION,
)
from tests._ibkr_pipeline_inputs import (
    PIPELINE_AS_OPEN_POSITIONS_SECTION as _AS_OPEN_POSITIONS_SECTION,
)
from tests._ibkr_pipeline_inputs import PIPELINE_AS_STATEMENT_SECTION as _AS_STATEMENT_SECTION
from tests._ibkr_pipeline_inputs import PIPELINE_AS_TRADES_SECTION as _AS_TRADES_SECTION
from tests._ibkr_pipeline_inputs import PIPELINE_OPEN_POSITIONS_SECTION as _OPEN_POSITIONS_SECTION
from tests._ibkr_pipeline_inputs import (
    PIPELINE_TRADES_WITH_CLOSED_LOT as _TRADES_SECTION_WITH_CLOSED_LOT,
)
from tests._ibkr_pipeline_inputs import write_activity_statement_csv
from tests._ibkr_pipeline_inputs import write_pipeline_flex_csv as write_flex_csv
from tests._ibkr_pipeline_inputs import write_pipeline_user_toml as write_user_toml
from tests._ibkr_user_toml import CUSTODIAN_TOML
from tests._lot_matching_inputs import BROKER_BASIS_MESSAGE
from tests._number_limits import MONEY
from tests._trades_totals import with_trades_totals
from tests._writer_stubs import StubWriter, patch_writers

# Same closing execution and CLOSED_LOT row, the lot with a wash-sale-shifted
# HoldingPeriodDateTime (before its OpenDateTime), which #599 refuses.
_TRADES_SECTION_WITH_SHIFTED_CLOSED_LOT = (
    TRADES_LOT_DETAIL_HEADER
    + """
"USD","1","STK","COMMON","XQBD","SAMPLE ISSUER BD INC","99995A106","US99995A1060","NASDAQ","US","20250428","20250428","20250429","ExchTrade","-4","61.15","-244.6","244.6","0","0","USD","244.6","61.15","C","-260","-15.4","SELL","EXECUTION","",""
"USD","1","STK","COMMON","XQBD","SAMPLE ISSUER BD INC","99995A106","US99995A1060","NASDAQ","US","20250428","20250428","","","4","65","","","","","","","","C","260","-15.4","SELL","CLOSED_LOT","20250428;101500","20250310;100000"
"""  # noqa: E501
)

# A Trades section with a header and no rows: nothing was traded (#1286).
_NO_TRADES_SECTION = _TRADES_SECTION_WITH_CLOSED_LOT.splitlines(keepends=True)[0]


# ---- happy path ------------------------------------------------------------


def test_run_with_open_only_produces_review_csv(tmp_path: Path) -> None:
    flex = write_flex_csv(
        tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION, _NO_TRADES_SECTION
    )
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    report = run_fa_a3(
        input_path=flex,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    assert isinstance(report, RunReport)
    assert report.rows_written == 1
    assert report.output_path == out
    assert report.account_id == "U12345678"
    assert report.account_id_cross_checked is True
    assert report.input_format.value == "flex-query"
    assert out.exists()
    # Verify a known-good cell in the written CSV: the XQBB open lot's
    # portal-facing country.
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[1][0] == "2-UNITED STATES OF AMERICA"


def test_run_with_open_and_closed_lots(tmp_path: Path) -> None:
    """When the Trades section is present and contains CLOSED_LOT
    rows, those become additional FaA3Rows after the open lots."""
    flex = write_flex_csv(
        tmp_path,
        _ACCOUNT_INFO_SECTION,
        _OPEN_POSITIONS_SECTION,
        _TRADES_SECTION_WITH_CLOSED_LOT,
    )
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    report = run_fa_a3(
        input_path=flex,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    # 1 open lot + 1 closed lot.
    assert report.rows_written == 2
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    # Header + 2 data rows.
    assert len(rows) == 3


def _assert_absent(message: str, *values: str) -> None:
    """Assert no ``values`` appear in ``message``, compared case-insensitively."""
    for value in values:
        assert value.lower() not in message.lower(), value


def test_run_refuses_closed_lot_with_shifted_holding_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#599: a Flex CLOSED_LOT whose HoldingPeriodDateTime differs from
    its OpenDateTime may carry a US wash-sale basis, so the run refuses
    and writes nothing. Since #640 the engine refuses it, and the CLI
    still exits 4. The refusal prints none of the lot's symbol, ISIN or
    dates (#1047)."""
    flex = write_flex_csv(
        tmp_path,
        _ACCOUNT_INFO_SECTION,
        _OPEN_POSITIONS_SECTION,
        _TRADES_SECTION_WITH_SHIFTED_CLOSED_LOT,
    )
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    with pytest.raises(BrokerBasisError) as raised:
        run_fa_a3(
            input_path=flex,
            config_path=cfg,
            output_path=out,
            calendar_year=2025,
        )
    assert str(raised.value) == BROKER_BASIS_MESSAGE
    assert not out.exists()
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(flex),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )
    assert rc == 4
    err = capsys.readouterr().err
    assert err == f"error: {BROKER_BASIS_MESSAGE}\n"
    _assert_absent(err, "XQBD", "US99995A1060", "2025-04-28", "2025-03-10")
    assert not out.exists()


def _missing_trades_refusal(flex: Path) -> str:
    """#1286's refusal of a Flex Query with no Trades section, for ``flex``."""
    return (
        "ParseError in section 'Trades' at row 0: required section 'Trades' not "
        f"found in {flex}. Schedule FA must list every holding held during the "
        "calendar year, and a Flex Query shows the ones sold before December 31 "
        "only in its Trades section. Re-run the Flex Query with the Trades section "
        "enabled and its Closed Lots checkbox turned on."
    )


def test_run_when_trades_section_absent_refuses(tmp_path: Path) -> None:
    """#1286: a Flex Query with no Trades section can't show a holding sold
    during the year, so Schedule FA refuses it and writes nothing. A Trades
    section with a header and no rows is the evidence that nothing was
    traded (``_NO_TRADES_SECTION``)."""
    flex = write_flex_csv(tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION)
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    with pytest.raises(ParseError) as raised:
        run_fa_a3(
            input_path=flex,
            config_path=cfg,
            output_path=out,
            calendar_year=2025,
        )
    assert str(raised.value) == _missing_trades_refusal(flex)
    assert not out.exists()


# ---- Trades evidence for the holdings sold during the year (#1286) ---------

# Neutral markers for the symbol, description and ISIN of a holding still held
# on Dec 31 (10 shares bought 2025-04-29) and of one bought 2025-03-03 and
# fully sold 2025-06-16.
_HELD = ("QZ86HELD", "QZ86 HELD CORP", "US99986R1005")
_SOLD = ("QZ86SOLD", "QZ86 SOLD CORP", "US99986Q1022")
_HELD_CELLS = '"USD","1","STK","COMMON","QZ86HELD","QZ86 HELD CORP","99986R100","US99986R1005"'
_SOLD_CELLS = '"USD","1","STK","COMMON","QZ86SOLD","QZ86 SOLD CORP","99986Q102","US99986Q1022"'
_HELD_OPEN_POSITIONS = (
    _OPEN_POSITIONS_SECTION.splitlines(keepends=True)[0]
    + f'{_HELD_CELLS},"NASDAQ","US","20251231","10","95.4","954","85.2","852","Long","LOT",'
    '"20250429","20250429"\n'
)
# The held holding's opening buy: 10 x 85.2 = 852, no commission.
_HELD_OPENING_EXECUTION = (
    f'{_HELD_CELLS},"NASDAQ","US","20250429","20250429","20250430","ExchTrade","10","85.2",'
    '"852","-852","0","0","USD","-852","85.2","O","852","0","BUY","EXECUTION","",""\n'
)
# The sold holding's sale: 4 x 65 = 260 less 1 commission is 259 net, and its
# closed lot's cost 240 + realized 19 = 259.
_SOLD_CLOSING_EXECUTION = (
    f'{_SOLD_CELLS},"NASDAQ","US","20250616","20250616","20250617","ExchTrade","-4","65",'
    '"-260","260","0","-1","USD","259","65","C","-240","19","SELL","EXECUTION","",""\n'
)
_SOLD_CLOSED_LOT = (
    f'{_SOLD_CELLS},"NASDAQ","US","20250616","20250616","","","4","65","","","","","","",'
    '"","C","240","19","SELL","CLOSED_LOT","20250303;101500","20250303;101500"\n'
)

_UNMATCHED_CLOSING_REFUSAL = (
    "ParseError in section 'Trades' at row 0, column 'Quantity': the CLOSED_LOT "
    "quantities for this row's security don't add up to its closing EXECUTION "
    "quantities, so a holding sold during the year can't be checked against its "
    "closed lots. Re-run the Flex Query over the whole calendar year with the "
    "Closed Lots checkbox in the Trades section turned on."
)


def _sold_holding_flex(tmp_path: Path, trades_rows: str | None) -> Path:
    """Flex Query holding ``_HELD``, with a Trades section of ``trades_rows``,
    or with no Trades section for ``None``."""
    sections = [_ACCOUNT_INFO_SECTION, _HELD_OPEN_POSITIONS]
    if trades_rows is not None:
        sections.append(_NO_TRADES_SECTION + trades_rows)
    return write_flex_csv(tmp_path, *sections)


@pytest.mark.parametrize(
    ("trades_rows", "markers"),
    [
        pytest.param(None, _HELD, id="no-trades-section"),
        pytest.param(
            _SOLD_CLOSING_EXECUTION, _HELD + _SOLD, id="closing-execution-without-closed-lot"
        ),
        pytest.param(_SOLD_CLOSED_LOT, _HELD + _SOLD, id="closed-lot-without-closing-execution"),
    ],
)
def test_flex_without_the_sold_holdings_evidence_refuses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    trades_rows: str | None,
    markers: tuple[str, ...],
) -> None:
    """#1286: Table A3 lists a holding sold during the year only from the Flex
    Trades section's CLOSED_LOT rows. With no Trades section, or a closing
    execution and its closed lots that don't match, the sold holding's
    evidence can't be checked, so ``run_fa_a3`` and ``schedule-fa`` refuse
    with fixed text, write nothing and print none of the input's markers."""
    flex = _sold_holding_flex(tmp_path, trades_rows)
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "schedule-fa-a3.csv"
    inputs = sorted(tmp_path.iterdir())
    if trades_rows is None:
        expected = ParseError(
            section="Trades",
            row_index=0,
            column=None,
            message=_missing_trades_refusal(flex).split(": ", 1)[1],
            raw_value="",
        )
        text = _missing_trades_refusal(flex)
    else:
        expected = ParseError(
            section="Trades",
            row_index=0,
            column="Quantity",
            message=_UNMATCHED_CLOSING_REFUSAL.split(": ", 1)[1],
            raw_value="",
        )
        text = _UNMATCHED_CLOSING_REFUSAL

    with pytest.raises(ParseError) as raised:
        run_fa_a3(input_path=flex, config_path=cfg, output_path=out, calendar_year=2025)
    assert_unchained_refusal(raised.value, expected, text, markers=markers)
    assert sorted(tmp_path.iterdir()) == inputs

    rc = _run_schedule_fa(flex, cfg, out, "--format", "filing-csv")

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {text}\n"
    assert captured.out == ""
    assert_no_markers(captured.err, *markers)
    assert sorted(tmp_path.iterdir()) == inputs


# SHA-256 of schedule-fa-a3.csv and of stdout with the tmp path replaced by
# "<tmp>", as main wrote them at d2a3a00, before #1286, except that #1289
# blanked each row's Address of entity and added the address and ZIP warnings,
# and #1430 reworded the nature warning to set nature_of_entity in that ISIN's
# [[fa.entities]] block instead of adding one.
_ONLY_HELD_SHA256 = (
    "b3ab4572ec0c63f788e7a77cee25ea47fc7c4559bdfe0e365738e2f8d95f1332",
    "04d35cef579f8bef3479ec24c09a03462893853d4ea9321e429fd3ff2c406896",
)
_CONTROL_SHA256 = {
    "closed-lot": (
        "f21679659d858438c2a0895a44314de7f56444684feab2bafbb3e350ac4feb14",
        "00e1bf67eedc780d50cb6e0f9cc8b7e7be4dbd394863aaaa379da39502151ae4",
    ),
    "opening-only": _ONLY_HELD_SHA256,
    "header-only": _ONLY_HELD_SHA256,
}


@pytest.mark.parametrize(
    ("case", "trades_rows"),
    [
        pytest.param("closed-lot", _SOLD_CLOSING_EXECUTION + _SOLD_CLOSED_LOT, id="closed-lot"),
        pytest.param("opening-only", _HELD_OPENING_EXECUTION, id="opening-only"),
        pytest.param("header-only", "", id="header-only"),
    ],
)
def test_flex_with_the_sold_holdings_evidence_files_as_before(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str, trades_rows: str
) -> None:
    """#1286 controls: a closing execution with its closed lot (the sold
    holding is filed with closing balance 0), opening-only Trades and a
    header-only Trades section give main's exit code, and main's bytes and
    stdout apart from #1289's blank addresses and their warnings and #1430's
    nature warning wording."""
    flex = _sold_holding_flex(tmp_path, trades_rows)
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "schedule-fa-a3.csv"

    rc = _run_schedule_fa(flex, cfg, out, "--format", "filing-csv")

    assert rc == 3
    stdout = capsys.readouterr().out.replace(str(tmp_path), "<tmp>")
    digests = (
        hashlib.sha256(out.read_bytes()).hexdigest(),
        hashlib.sha256(stdout.encode()).hexdigest(),
    )
    assert digests == _CONTROL_SHA256[case]


# ---- manual-fill warnings --------------------------------------------------


def test_manual_fill_columns_always_present_in_v1(tmp_path: Path) -> None:
    """The report names every blank field that applies to its rows."""
    flex = write_flex_csv(
        tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION, _NO_TRADES_SECTION
    )
    cfg = write_user_toml(tmp_path)
    report = run_fa_a3(
        input_path=flex,
        config_path=cfg,
        output_path=tmp_path / "review.csv",
        calendar_year=2025,
    )
    # Every A3 manual-fill column but Name of entity (#1369) and gross proceeds.
    assert report.manual_fill_columns == MANUAL_FILL_PORTAL_COLUMNS[1:-1]
    assert len(report.manual_fill_columns) == 5
    # #1289: no [[fa.entities]] entry, so address and ZIP are named too.
    assert report.address_of_entity_manual_fill_holdings == ("XQBB (US99995B1044)",)
    assert report.zip_code_manual_fill_holdings == ("XQBB (US99995B1044)",)
    assert report.nature_of_entity_manual_fill_holdings == ("XQBB (US99995B1044)",)
    # Spot-check that each label is one of the portal-verbatim ones.
    assert any("Peak value" in c for c in report.manual_fill_columns)


def test_manual_fill_columns_present_even_when_zero_rows(tmp_path: Path) -> None:
    """Even an empty result still requires the warning, because the
    user's expectation is "you must manually fill these on every
    row you do enter"."""
    # Open Position with a 2025 ReportDate but we request calendar_year=2024
    # so the pipeline rejects it. To get an empty result, write a year-end
    # snapshot for 2026 but request CY=2026 too, with no closed lots and
    # a synthetic open lot whose open_date is AFTER Dec 31 of CY (impossible
    # in practice but exercises the empty path).
    # Simpler: just file with no Open Positions data rows (header only).
    open_header_only = OPEN_POSITIONS_HEADER + "\n"
    flex = write_flex_csv(tmp_path, _ACCOUNT_INFO_SECTION, open_header_only, _NO_TRADES_SECTION)
    cfg = write_user_toml(tmp_path)
    report = run_fa_a3(
        input_path=flex,
        config_path=cfg,
        output_path=tmp_path / "review.csv",
        calendar_year=2025,
    )
    assert report.rows_written == 0
    assert "Nature of entity" not in report.manual_fill_columns
    assert len(report.manual_fill_columns) == 2


# ---- account-number cross-check -------------------------------------------


def test_account_number_mismatch_raises_config_error(tmp_path: Path) -> None:
    """A statement account of U7654321 against user.toml's U1234567 refuses
    with fixed text that prints neither number, masked or not (#1047) —
    defense against accidentally filing someone else's holdings on your
    return."""
    account_info = _ACCOUNT_INFO_SECTION.replace('"U12345678"', '"U7654321"')
    flex = write_flex_csv(tmp_path, account_info, _OPEN_POSITIONS_SECTION, _NO_TRADES_SECTION)
    cfg = write_user_toml(tmp_path, account_number="U1234567")
    with pytest.raises(ConfigError) as exc:
        run_fa_a3(
            input_path=flex,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )
    assert exc.value.field == "account.number"
    assert exc.value.message == ACCOUNT_MISMATCH_REFUSAL
    assert str(exc.value) == f"ConfigError at field 'account.number': {ACCOUNT_MISMATCH_REFUSAL}"
    _assert_absent(str(exc.value), "U7654321", "U1234567", "U***4321", "U***4567")


def test_missing_ibkr_account_number_refuses_before_output(tmp_path: Path) -> None:
    flex = write_flex_csv(tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION)
    cfg = write_user_toml(tmp_path)
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace('number = "U12345678"\n', ""),
        encoding="utf-8",
    )
    output = tmp_path / "review.csv"

    with pytest.raises(ConfigError) as exc:
        run_fa_a3(
            input_path=flex,
            config_path=cfg,
            output_path=output,
            calendar_year=2025,
        )

    assert exc.value.field == "account.number"
    assert "required for IBKR statement commands" in exc.value.message
    assert not output.exists()


def test_account_number_match_succeeds(tmp_path: Path) -> None:
    """Sanity inverse of the mismatch test — same numbers proceed."""
    flex = write_flex_csv(
        tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION, _NO_TRADES_SECTION
    )
    cfg = write_user_toml(tmp_path, account_number="U12345678")
    report = run_fa_a3(
        input_path=flex,
        config_path=cfg,
        output_path=tmp_path / "review.csv",
        calendar_year=2025,
    )
    assert report.account_id == "U12345678"


# ---- required-section enforcement -----------------------------------------


def test_missing_account_information_section_raises(tmp_path: Path) -> None:
    """Account Information is required so the account-number
    cross-check can run. Without it the pipeline can't enforce its
    defence and must refuse."""
    flex = write_flex_csv(tmp_path, _OPEN_POSITIONS_SECTION)
    cfg = write_user_toml(tmp_path)
    with pytest.raises(ParseError) as exc:
        run_fa_a3(
            input_path=flex,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )
    assert exc.value.section == "Account Information"
    assert "Re-run the Flex Query" in exc.value.message


def test_missing_open_positions_section_raises(tmp_path: Path) -> None:
    """Open Positions is the primary input to FA-A3 — if it's not
    in the Flex Query, the user has no Schedule FA data to file.
    Refuse loudly."""
    flex = write_flex_csv(tmp_path, _ACCOUNT_INFO_SECTION)
    cfg = write_user_toml(tmp_path)
    with pytest.raises(ParseError) as exc:
        run_fa_a3(
            input_path=flex,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )
    assert exc.value.section == "Open Positions (LOT)"


@pytest.mark.parametrize(
    ("blocks", "section", "message"),
    [
        (
            (_OPEN_POSITIONS_SECTION,),
            "Account Information",
            "required section 'Account Information' not found in {path}. "
            "Re-run the Flex Query with this section enabled.",
        ),
        (
            (_ACCOUNT_INFO_SECTION,),
            "Open Positions (LOT)",
            "required section 'Open Positions (LOT)' not found in {path}. "
            "Re-run the Flex Query with this section enabled.",
        ),
        (
            (_TRADES_SECTION_WITH_CLOSED_LOT,),
            "Account Information, Open Positions (LOT)",
            "required sections 'Account Information', 'Open Positions (LOT)' not found in "
            "{path}. Re-run the Flex Query with these sections enabled.",
        ),
    ],
)
def test_flex_missing_required_sections_named_in_one_error(
    tmp_path: Path, blocks: tuple[str, ...], section: str, message: str
) -> None:
    """#784: a Flex Query missing both required sections is refused once,
    naming Account Information then Open Positions (LOT); one missing
    section keeps its single-section error."""
    flex = write_flex_csv(tmp_path, *blocks)
    cfg = write_user_toml(tmp_path)
    with pytest.raises(ParseError) as exc:
        run_fa_a3(
            input_path=flex,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )
    error = exc.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (section, 0, None, "")
    assert error.message == message.format(path=flex)


# ---- output format selection ----------------------------------------------


def test_default_format_is_review_csv(tmp_path: Path) -> None:
    """v1's default --format is review-csv; the call signature must
    not require the user to pass it."""
    flex = write_flex_csv(
        tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION, _NO_TRADES_SECTION
    )
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    # No format_id kwarg -> default.
    run_fa_a3(
        input_path=flex,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    assert out.exists()


def test_unknown_format_id_raises(tmp_path: Path) -> None:
    """Bad --format value propagates as UnknownOutputFormatError so
    the CLI can render the list of available formats."""
    flex = write_flex_csv(
        tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION, _NO_TRADES_SECTION
    )
    cfg = write_user_toml(tmp_path)
    with pytest.raises(UnknownOutputFormatError) as exc:
        run_fa_a3(
            input_path=flex,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
            format_id="not-a-real-format",
        )
    assert exc.value.format_id == "not-a-real-format"


def test_out_of_range_calendar_year_refuses_after_preclear(tmp_path: Path) -> None:
    """#928: a year date() cannot build refuses with a typed error, not ValueError,
    once the earlier outputs are removed and before the config or input is read."""
    out = tmp_path / "review.csv"
    a2 = tmp_path / "review-a2.csv"
    out.write_text("stale A3", encoding="utf-8")
    a2.write_text("stale A2", encoding="utf-8")
    with pytest.raises(
        UnsupportedCalendarYearError,
        match=r"^calendar year 0 is out of the supported range \[1990, 2099\]$",
    ):
        run_fa_a3(
            input_path=tmp_path / "missing.csv",
            config_path=tmp_path / "missing.toml",
            output_path=out,
            calendar_year=0,
        )
    assert not out.exists()
    assert not a2.exists()


def test_a2_failure_removes_the_whole_filing_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flex = write_flex_csv(
        tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION, _NO_TRADES_SECTION
    )
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    a2 = tmp_path / "review-a2.csv"
    out.write_text("stale A3", encoding="utf-8")
    a2.write_text("stale A2", encoding="utf-8")

    def fail_a2(**_kwargs: object) -> None:
        raise RuntimeError("A2 writer failed")

    monkeypatch.setattr("ibkr_fa.pipeline_fa.build_and_write_fa_a2", fail_a2)
    with pytest.raises(RuntimeError, match="A2 writer failed"):
        run_fa_a3(
            input_path=flex,
            config_path=cfg,
            output_path=out,
            calendar_year=2025,
        )

    assert not out.exists()
    assert not a2.exists()


def test_a3_failure_rolls_back_the_written_a2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flex = write_flex_csv(
        tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION, _NO_TRADES_SECTION
    )
    cfg = write_user_toml(tmp_path)
    with cfg.open("a", encoding="utf-8") as handle:
        handle.write(CUSTODIAN_TOML)
    out = tmp_path / "review.csv"
    a2 = tmp_path / "review-a2.csv"
    out.write_text("stale", encoding="utf-8")

    class FailingA3Writer(StubWriter):
        def write(self, _rows: object, _path: Path) -> None:
            raise RuntimeError("A3 writer failed")

    patch_writers(monkeypatch, Subcommand.FA, FailingA3Writer)
    with pytest.raises(RuntimeError, match="A3 writer failed"):
        run_fa_a3(
            input_path=flex,
            config_path=cfg,
            output_path=out,
            calendar_year=2025,
        )

    assert not out.exists()
    assert not a2.exists()


def test_stale_a2_is_removed_when_no_custodian_is_configured(tmp_path: Path) -> None:
    flex = write_flex_csv(
        tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION, _NO_TRADES_SECTION
    )
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    a2 = tmp_path / "review-a2.csv"
    a2.write_text("stale", encoding="utf-8")

    report = run_fa_a3(input_path=flex, config_path=cfg, output_path=out, calendar_year=2025)

    assert report.a2_rows == ()
    assert out.exists()
    assert not a2.exists()


# ---- error propagation ----------------------------------------------------


def test_missing_input_file_raises_file_not_found(tmp_path: Path) -> None:
    cfg = write_user_toml(tmp_path)
    with pytest.raises(FileNotFoundError):
        run_fa_a3(
            input_path=tmp_path / "does-not-exist.csv",
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )


def test_missing_config_file_raises_path_refusal(tmp_path: Path) -> None:
    flex = write_flex_csv(tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION)
    with pytest.raises(PathRefusalError):
        run_fa_a3(
            input_path=flex,
            config_path=tmp_path / "no-toml-here.toml",
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )


def test_open_positions_wrong_report_date_raises_parse_error(tmp_path: Path) -> None:
    """The pipeline enforces ReportDate == Dec 31 of calendar_year.
    A user passing the wrong CY gets a clear ParseError (not a
    silently-wrong output)."""
    flex = write_flex_csv(tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION)
    cfg = write_user_toml(tmp_path)
    with pytest.raises(ParseError) as exc:
        run_fa_a3(
            input_path=flex,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2024,  # CSV ReportDate is 20251231
        )
    assert exc.value.column == "ReportDate"


# ---- oversized filed amounts (#788) -----------------------------------------

_NAV_STATEMENT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_with_nav_2025.csv"
)


@pytest.mark.parametrize(
    ("format_id", "suffix"), [("review-csv", ".csv"), ("filing-csv", ".csv"), ("xlsx", ".xlsx")]
)
def test_oversized_flex_cost_basis_refuses_before_any_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], format_id: str, suffix: str
) -> None:
    """A Flex Query lot cost of 1E14 USD (within the money limit, #1276) is
    past the CBDT bound in rupees, so it refuses when its A3 row is built,
    before its filed value is rounded to whole rupees."""
    flex = write_flex_csv(
        tmp_path,
        _ACCOUNT_INFO_SECTION,
        _OPEN_POSITIONS_SECTION.replace('"852"', '"1E14"'),
        _NO_TRADES_SECTION,
    )
    assert '"1E14"' in flex.read_text(encoding="utf-8")
    cfg = write_user_toml(tmp_path)
    inputs = sorted(tmp_path.iterdir())

    rc = _run_schedule_fa(flex, cfg, tmp_path / f"schedule-fa{suffix}", "--format", format_id)

    assert rc == 4
    assert "InvariantError: FaA3Row.initial_value_inr must round" in capsys.readouterr().err
    assert sorted(tmp_path.iterdir()) == inputs


def test_oversized_activity_statement_cost_basis_refuses_the_json_fragment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The JSON fragment needs a verified Table A2 account, which a Flex Query
    can't provide: an Activity Statement lot cost of 1E14 USD (within the
    money limit, #1276) refuses the same way."""
    text = _NAV_STATEMENT_FIXTURE.read_text(encoding="utf-8")
    lot_basis = ",-0.40,1005.40,0,48.60,O\n"
    assert text.count(lot_basis) == 1
    statement = tmp_path / "statement.csv"
    statement.write_text(
        with_trades_totals(text.replace(lot_basis, ",-0.40,1E14,0,48.60,O\n")), encoding="utf-8"
    )
    cfg = write_user_toml(tmp_path)
    cfg.write_text(cfg.read_text(encoding="utf-8") + CUSTODIAN_TOML, encoding="utf-8")
    inputs = sorted(tmp_path.iterdir())

    rc = _run_schedule_fa(
        statement,
        cfg,
        tmp_path / "schedule-fa.json",
        "--format",
        "cbdt-json-fragment",
        "--itr-form",
        "ITR-2",
    )

    assert rc == 4
    assert "InvariantError: FaA3Row.initial_value_inr must round" in capsys.readouterr().err
    assert sorted(tmp_path.iterdir()) == inputs


def test_flex_cost_basis_past_its_digit_limit_refuses_when_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1276: the 1E100-shaped Flex Query lot cost the oversized test above
    once read refuses when the Open Positions row is read, with the money
    limit and no value: exit 4, no output, the stale output removed."""
    flex = write_flex_csv(
        tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION.replace('"852"', '"7242E100"')
    )
    assert '"7242E100"' in flex.read_text(encoding="utf-8")
    cfg = write_user_toml(tmp_path)
    inputs = sorted(tmp_path.iterdir())
    out = tmp_path / "schedule-fa.csv"
    out.write_text("stale\n", encoding="utf-8")

    rc = _run_schedule_fa(flex, cfg, out)

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == (
        "error: ParseError in section 'Open Positions (LOT)' at row 0, column "
        f"'CostBasisMoney': {MONEY}\n"
    )
    assert captured.out == ""
    assert "7242" not in captured.err
    assert sorted(tmp_path.iterdir()) == inputs


def test_activity_statement_cost_basis_past_its_digit_limit_refuses_when_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1276: the 1E100-shaped Activity Statement lot cost the JSON-fragment
    test above once read refuses when the splitter checks the Trades totals,
    on its file line, with the money limit and no value: exit 4, no output,
    the stale output removed."""
    text = _NAV_STATEMENT_FIXTURE.read_text(encoding="utf-8")
    lot_basis = ",-0.40,1005.40,0,48.60,O\n"
    assert text.count(lot_basis) == 1
    statement = tmp_path / "statement.csv"
    statement.write_text(
        with_trades_totals(text.replace(lot_basis, ",-0.40,7242E100,0,48.60,O\n")),
        encoding="utf-8",
    )
    cfg = write_user_toml(tmp_path)
    cfg.write_text(cfg.read_text(encoding="utf-8") + CUSTODIAN_TOML, encoding="utf-8")
    inputs = sorted(tmp_path.iterdir())
    out = tmp_path / "schedule-fa.json"
    # A fragment an earlier run wrote, which ibkr-fa recognises as its own.
    out.write_text('{"_ibkrFa": {"format": "cbdt-json-fragment"}, "ScheduleFA": {}}', "utf-8")

    rc = _run_schedule_fa(
        statement, cfg, out, "--format", "cbdt-json-fragment", "--itr-form", "ITR-2"
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert (
        captured.err
        == f"error: ParseError in section 'Trades' at row 21, column 'Basis': {MONEY}\n"
    )
    assert captured.out == ""
    assert "7242" not in captured.err
    assert sorted(tmp_path.iterdir()) == inputs


# ============================================================================
# Activity Statement format support (issue #110)
# ============================================================================
#
# Synthetic Activity Statement fixtures. The CSV is section-tagged:
# each row begins with the section name and a Header/Data
# discriminator, trimmed to the sections the pipeline requires.

# Closure-only Trades section (#162): opens XQBK and closes
# it before Dec 31, so the AS has no Dec-31 holdings and IBKR
# legitimately omits the Open Positions section from the export. The
# C trade's Open/CloseIndicator is "C" so the FIFO matcher emits one
# ClosedLot and zero surviving OpenLots.
_AS_TRADES_SECTION_CLOSURE_ONLY = """\
Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code
Trades,Data,Order,Stocks,USD,XQBK,"2025-03-15, 09:45:00",1,212,212.60,-212,-0.35,212.35,0,0.60,O
Trades,Data,Order,Stocks,USD,XQBK,"2025-09-20, 14:12:00",-1,220,220,220,-0.35,-212.35,7.30,0,C
"""  # noqa: E501


# ---- Activity Statement happy path ---------------------------------------


def test_activity_statement_full_pipeline(tmp_path: Path) -> None:
    """End-to-end: an Activity Statement CSV reaches the writer and
    produces a single FA-A3 row from the reconstructed open lot."""
    as_csv = write_activity_statement_csv(tmp_path)
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    report = run_fa_a3(
        input_path=as_csv,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    assert report.rows_written == 1
    assert report.account_id == "U12345678"
    assert report.account_id_cross_checked is True
    assert report.input_format.value == "activity-statement"
    assert out.exists()
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    # Header + 1 data row for the XQBK open lot.
    assert len(rows) == 2
    assert rows[1][0] == "2-UNITED STATES OF AMERICA"


# ---- Activity Statement period validation -------------------------------


def test_activity_statement_wrong_period_raises(tmp_path: Path) -> None:
    """Statement period mismatching --calendar-year must fail immediately
    after period parsing, before account and Trades parsing."""
    fy_statement = """\
Statement,Header,Field Name,Field Value,,,,,,,,,,,,,
Statement,Data,Period,"April 1, 2025 - March 31, 2026",,,,,,,,,,,,,
"""
    as_csv = write_activity_statement_csv(tmp_path, statement_section=fy_statement)
    cfg = write_user_toml(tmp_path)
    with pytest.raises(ParseError) as exc:
        run_fa_a3(
            input_path=as_csv,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )
    assert "must end on Dec 31, 2025 and start on or before Jan 1, 2025" in exc.value.message
    assert "re-export" in exc.value.message.lower()


# ---- Activity Statement section requirements ----------------------------


@pytest.mark.parametrize(
    "missing",
    [
        "Account Information",
        "Financial Instrument Information",
        "Trades",
    ],
)
def test_activity_statement_missing_section_raises(tmp_path: Path, missing: str) -> None:
    """Each required Activity Statement section must be present;
    drop any one and the pipeline refuses with a clear error
    naming the missing section.

    The ``Statement`` section is the format-detection anchor — its
    absence is tested separately in
    :func:`test_activity_statement_missing_statement_section_fails_detection`,
    because without it the file's first line doesn't match either
    format sentinel and detection fails before per-section
    enforcement runs. ``Open Positions`` is **not** in this list:
    per #162 it is optional, and the closure-only positive path is
    covered by
    :func:`test_activity_statement_closure_only_no_open_positions_section_succeeds`.
    The negative case (surviving open lot + missing Open Positions
    section) is covered by
    :func:`test_activity_statement_surviving_open_without_open_positions_section_raises`.
    """
    as_csv = write_activity_statement_csv(tmp_path, omit_section=missing)
    cfg = write_user_toml(tmp_path)
    with pytest.raises(ParseError) as exc:
        run_fa_a3(
            input_path=as_csv,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )
    assert exc.value.section == missing
    assert "Re-export the Activity Statement" in exc.value.message


@pytest.mark.parametrize(
    ("omitted", "section", "quoted"),
    [
        (
            ("Account Information", "Trades"),
            "Account Information, Trades",
            "'Account Information', 'Trades'",
        ),
        (
            ("Account Information", "Financial Instrument Information", "Trades"),
            "Account Information, Trades, Financial Instrument Information",
            "'Account Information', 'Trades', 'Financial Instrument Information'",
        ),
    ],
)
def test_activity_statement_missing_sections_all_named_in_declaration_order(
    tmp_path: Path, omitted: tuple[str, ...], section: str, quoted: str
) -> None:
    """#784: one refusal names every missing required section, in
    :class:`KnownActivitySection` declaration order rather than set
    order, so the user can fix them all before re-running."""
    as_csv = write_activity_statement_csv(tmp_path, omit_section=omitted)
    cfg = write_user_toml(tmp_path)
    with pytest.raises(ParseError) as exc:
        run_fa_a3(
            input_path=as_csv,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )
    error = exc.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (section, 0, None, "")
    assert error.message == (
        f"required Activity Statement sections {quoted} not found in {as_csv}. "
        "Re-export the Activity Statement with these sections enabled."
    )


def _period_never_checked(_period: StatementPeriod) -> None:
    pytest.fail("required sections are checked before the statement period")


def _stub_split_sections(
    monkeypatch: pytest.MonkeyPatch,
    as_csv: Path,
    *,
    empty: tuple[KnownActivitySection, ...] = (),
    absent: tuple[KnownActivitySection, ...] = (),
) -> None:
    """Serve ``as_csv``'s real section map with ``empty`` sections holding no
    blocks and ``absent`` sections removed."""
    sections = split_activity_statement_sections(as_csv)
    for section in empty:
        sections[section] = []
    for section in absent:
        del sections[section]
    monkeypatch.setattr(
        pipeline_dispatch, "split_activity_statement_sections", lambda _path: sections
    )


def test_activity_statement_empty_and_absent_sections_named_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#784: a required section present with no blocks counts as missing,
    as before, and is named alongside an absent one."""
    as_csv = write_activity_statement_csv(tmp_path)
    _stub_split_sections(
        monkeypatch,
        as_csv,
        empty=(KnownActivitySection.ACCOUNT_INFORMATION,),
        absent=(KnownActivitySection.TRADES,),
    )
    with pytest.raises(ParseError) as exc:
        _dispatch_activity_statement(as_csv, period_validator=_period_never_checked)
    assert exc.value.section == "Account Information, Trades"


def test_os_required_sections_missing_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#784: Schedule OS requires only Statement and Account Information;
    with neither present, one refusal names both."""
    as_csv = write_activity_statement_csv(tmp_path)
    _stub_split_sections(
        monkeypatch,
        as_csv,
        absent=(KnownActivitySection.STATEMENT, KnownActivitySection.ACCOUNT_INFORMATION),
    )
    with pytest.raises(ParseError) as exc:
        _dispatch_activity_statement(
            as_csv,
            period_validator=_period_never_checked,
            required_sections=_OS_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
        )
    assert exc.value.section == "Statement, Account Information"


def test_os_missing_account_information_keeps_single_section_error(tmp_path: Path) -> None:
    """#784: with one required section missing, the refusal is unchanged."""
    as_csv = write_activity_statement_csv(tmp_path, omit_section="Account Information")
    with pytest.raises(ParseError) as exc:
        _dispatch_activity_statement(
            as_csv,
            period_validator=_period_never_checked,
            required_sections=_OS_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
        )
    error = exc.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "Account Information",
        0,
        None,
        "",
    )
    assert error.message == (
        f"required Activity Statement section 'Account Information' not found in {as_csv}. "
        "Re-export the Activity Statement with this section enabled."
    )


_DISPATCH_AND_PRINT_REFUSAL = """\
import sys
from pathlib import Path

from ibkr_fa.exceptions import ParseError
from ibkr_fa.pipeline_dispatch import _dispatch_activity_statement

try:
    _dispatch_activity_statement(Path(sys.argv[1]), period_validator=lambda _period: None)
except ParseError as error:
    print(error)
else:
    sys.exit("no ParseError")
"""


def test_activity_statement_missing_sections_error_ignores_hash_seed(tmp_path: Path) -> None:
    """#784: the required sections are a frozenset of ``StrEnum`` members,
    which iterates in ``PYTHONHASHSEED`` order. The refusal must not."""
    as_csv = write_activity_statement_csv(
        tmp_path,
        omit_section=("Account Information", "Financial Instrument Information", "Trades"),
    )
    runs = [
        subprocess.run(
            [sys.executable, "-c", _DISPATCH_AND_PRINT_REFUSAL, str(as_csv)],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            text=True,
            check=False,
        )
        for seed in range(8)
    ]
    assert [run.returncode for run in runs] == [0] * 8, [run.stderr for run in runs]
    errors = {run.stdout for run in runs}
    assert len(errors) == 1, errors
    assert "'Account Information, Trades, Financial Instrument Information'" in errors.pop()


def test_activity_statement_closure_only_no_open_positions_section_succeeds(
    tmp_path: Path,
) -> None:
    """#162: an Activity Statement where every position opened in
    the calendar year was also fully closed in the year may
    legitimately omit the Open Positions section (IBKR doesn't
    emit empty sections). The FA pipeline must accept this case
    and produce a review CSV — Schedule FA Table A3 reports any
    foreign holding *held at any time during* the period, so the
    closed lot still appears as one A3 row (peak value over its
    open-to-close window), but the Dec-31 close-price lookup is
    not consulted for any surviving lot and the matcher's #133
    refusal is not triggered.

    The closure-only Trades section here has one O trade matched
    by one C trade on the same symbol; the FIFO matcher emits one
    ClosedLot and zero surviving OpenLots, so
    ``build_dec31_close_prices({})`` is the empty dict and
    ``match_fifo`` emits no open lot, so it looks up no ticker.
    """
    as_csv = write_activity_statement_csv(
        tmp_path,
        trades_section=_AS_TRADES_SECTION_CLOSURE_ONLY,
        omit_section="Open Positions",
    )
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    report = run_fa_a3(
        input_path=as_csv,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    assert report.rows_written == 1
    assert out.exists()
    # Header + 1 data row for the closed lot held during the CY.
    with out.open(encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 2, f"expected header + 1 data row, got {len(rows)} rows"


def test_activity_statement_surviving_open_without_open_positions_section_raises(
    tmp_path: Path,
) -> None:
    """#162: when the Open Positions section is missing AND the
    Trades section leaves a residual open lot at period end, the
    matcher's #133 refusal raises — we still can't derive
    a Dec-31 close price for that surviving lot, so refusing
    output is the safe behaviour.

    Uses the default Trades section, which has one O trade on
    XQBK with no matching C — exactly the surviving-open scenario.
    """
    as_csv = write_activity_statement_csv(tmp_path, omit_section="Open Positions")
    cfg = write_user_toml(tmp_path)
    with pytest.raises(LotMatchingError) as exc:
        run_fa_a3(
            input_path=as_csv,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )
    assert exc.value.row_index == 0
    # Fixed text: the row index locates the lot, not its symbol (#1047).
    assert str(exc.value) == (
        "LotMatchingError in section 'Trades' at row 0: the opening trade leaves a "
        "surviving open lot after FIFO matching, but its symbol does not appear in "
        "the Open Positions section, so the Dec 31 close price can't be determined. "
        "See https://github.com/jay-tau/ibkr-fa/issues/133."
    )
    _assert_absent(str(exc.value), "XQBK")


def test_activity_statement_missing_statement_section_fails_detection(tmp_path: Path) -> None:
    """When the ``Statement`` section is missing, the file's first
    line is the next section's header (e.g., ``Account
    Information,Header,...``), which matches neither the
    Activity-Statement nor the Flex-Query sentinel. Detection
    raises before section-requirement enforcement gets a chance."""
    as_csv = write_activity_statement_csv(tmp_path, omit_section="Statement")
    cfg = write_user_toml(tmp_path)
    with pytest.raises(ParseError) as exc:
        run_fa_a3(
            input_path=as_csv,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )
    assert exc.value.section == "<file>"
    assert "doesn't match a known" in exc.value.message


def test_activity_statement_statement_header_without_columns_exits_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#812: a ``Statement`` header that names no columns still passes
    format detection, which reads only its first two cells. The splitter
    refuses it at its line, so ``schedule-fa`` reports the malformed
    header rather than a missing ``Statement`` section."""
    as_csv = write_activity_statement_csv(tmp_path, statement_section="Statement,Header,,,,\n")
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(as_csv),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )
    assert rc == 4
    err = capsys.readouterr().err
    assert (
        "ParseError in section 'Statement' at row 1: section header declares no column names"
    ) in err
    assert "not found in" not in err
    assert not out.exists()


def test_activity_statement_trades_header_with_blank_column_name_exits_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#819: an empty name in the ``Trades`` header leaves every column
    the Trades parser reads in place, so the value under it used to be
    dropped without an error and ``schedule-fa`` wrote the review CSV.
    The splitter refuses the header at its line."""
    blank_name_trades = """\
Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,,Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code
Trades,Data,Order,Stocks,USD,XQBK,LOST,"2025-12-29, 09:45:00",1,212,212.60,-212,-0.35,212.35,0,0.60,O
"""  # noqa: E501
    as_csv = write_activity_statement_csv(tmp_path, trades_section=blank_name_trades)
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(as_csv),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )
    assert rc == 4
    assert (
        "ParseError in section 'Trades' at row 12: section header's column name 5 is "
        "empty or only whitespace"
    ) in capsys.readouterr().err
    assert not out.exists()


# ---- #144 multi-block single-section guard ------------------------------


def test_activity_statement_multi_block_statement_section_raises_invariant(
    tmp_path: Path,
) -> None:
    """#144: when IBKR re-emits the Statement section header mid-file
    (creating a second Statement block), :func:`_single_section`
    refuses to silently take ``[0]`` and raises an InvariantError so
    the unexpected shape is reported, not absorbed."""
    cfg = write_user_toml(tmp_path)
    doubled_statement = _AS_STATEMENT_SECTION + _AS_STATEMENT_SECTION
    as_csv = write_activity_statement_csv(tmp_path, statement_section=doubled_statement)
    with pytest.raises(InvariantError) as exc:
        run_fa_a3(
            input_path=as_csv,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )
    assert "Statement" in str(exc.value)
    assert ("block_count", None) in exc.value.context
    assert "issues/144" in str(exc.value)


def test_activity_statement_multi_block_account_information_section_raises_invariant(
    tmp_path: Path,
) -> None:
    """#144: same loud-fail guard applies to the Account Information
    section — a duplicated block would, under the old ``[0]`` access,
    silently drop the second block's identity rows. The new guard
    refuses and reports.
    """
    cfg = write_user_toml(tmp_path)
    doubled_account_info = _AS_ACCOUNT_INFORMATION_SECTION.format(account="U12345678") * 2
    p = tmp_path / "activity-statement.csv"
    p.write_text(
        with_trades_totals(
            _AS_STATEMENT_SECTION
            + doubled_account_info
            + _AS_FINANCIAL_INSTRUMENT_INFO_SECTION
            + _AS_TRADES_SECTION
            + _AS_OPEN_POSITIONS_SECTION
        ),
        encoding="utf-8",
    )
    with pytest.raises(InvariantError) as exc:
        run_fa_a3(
            input_path=p,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )
    assert "Account Information" in str(exc.value)
    assert ("block_count", None) in exc.value.context
    assert "issues/144" in str(exc.value)


# ---- Masked-account handling --------------------------------------------


def test_masked_account_without_flag_raises(tmp_path: Path) -> None:
    """IBKR masks the account ID by default on Activity Statement
    exports. The pipeline cannot cross-check a masked ID against
    user.toml; without --allow-masked-account it must refuse, in fixed
    text that prints neither the mask nor user.toml's number (#1047)."""
    as_csv = write_activity_statement_csv(tmp_path, account="U**********")
    cfg = write_user_toml(tmp_path, account_number="U1234567")
    with pytest.raises(ConfigError) as exc:
        run_fa_a3(
            input_path=as_csv,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )
    assert exc.value.field == "account.number"
    assert exc.value.message == MASKED_ACCOUNT_REFUSAL
    assert str(exc.value) == f"ConfigError at field 'account.number': {MASKED_ACCOUNT_REFUSAL}"
    _assert_absent(str(exc.value), "U1234567", "U***4567", "U***")


def test_masked_account_with_flag_skips_cross_check(tmp_path: Path) -> None:
    """With --allow-masked-account, a masked ID skips the cross-check
    and the run completes; the report flags that the safety net
    didn't run so the CLI can warn loudly."""
    as_csv = write_activity_statement_csv(tmp_path, account="U**********")
    cfg = write_user_toml(tmp_path, account_number="U99999999")
    report = run_fa_a3(
        input_path=as_csv,
        config_path=cfg,
        output_path=tmp_path / "review.csv",
        calendar_year=2025,
        allow_masked_account=True,
    )
    assert report.account_id == "U**********"
    assert report.account_id_cross_checked is False
    assert report.input_format.value == "activity-statement"


def test_masked_account_via_config_opt_in_skips_cross_check(tmp_path: Path) -> None:
    """The [account].allow_masked_account opt-in enables the masked-account
    skip WITHOUT the --allow-masked-account CLI flag (config OR flag), so a
    repeat user with a masked statement need not pass it every run."""
    as_csv = write_activity_statement_csv(tmp_path, account="U**********")
    cfg = tmp_path / "user.toml"
    cfg.write_text(
        '[account]\nnumber = "U99999999"\nholder_name = "Test Holder"\n'
        'opening_date = 2020-01-01\nstatus = "BeneficialOwner"\n'
        "allow_masked_account = true\n\n[fx]\nmax_carry_forward_days = 14\n",
        encoding="utf-8",
    )
    report = run_fa_a3(
        input_path=as_csv,
        config_path=cfg,
        output_path=tmp_path / "review.csv",
        calendar_year=2025,
        # No allow_masked_account=True here — the config opt-in drives it.
    )
    assert report.account_id == "U**********"
    assert report.account_id_cross_checked is False


def test_unmasked_account_ignores_allow_masked_flag(tmp_path: Path) -> None:
    """The --allow-masked-account flag is a no-op for unmasked IDs:
    the cross-check still runs. A mismatch must still raise so the
    flag can't be used to bypass the safety net on real IDs."""
    as_csv = write_activity_statement_csv(tmp_path, account="U12345678")
    cfg = write_user_toml(tmp_path, account_number="U99999999")
    with pytest.raises(ConfigError) as exc:
        run_fa_a3(
            input_path=as_csv,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
            allow_masked_account=True,
        )
    # Importantly, this is the mismatch error, not the masked-account error.
    assert exc.value.message == ACCOUNT_MISMATCH_REFUSAL


def test_flex_query_default_does_not_check_masked(tmp_path: Path) -> None:
    """allow_masked_account defaults to False but is irrelevant for
    Flex Query: those exports include the full account ID
    inline."""
    flex = write_flex_csv(
        tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION, _NO_TRADES_SECTION
    )
    cfg = write_user_toml(tmp_path)
    report = run_fa_a3(
        input_path=flex,
        config_path=cfg,
        output_path=tmp_path / "review.csv",
        calendar_year=2025,
    )
    assert report.account_id_cross_checked is True
    assert report.input_format.value == "flex-query"


# ---- Format detection failure -------------------------------------------


def test_unknown_input_format_raises(tmp_path: Path) -> None:
    """Random non-IBKR CSV passed to the pipeline must fail at
    format detection, not later in parsing."""
    weird = tmp_path / "weird.csv"
    weird.write_text("Date,Ticker,Quantity,Price\n2025-01-15,XQBA,10,150.00\n")
    cfg = write_user_toml(tmp_path)
    with pytest.raises(ParseError) as exc:
        run_fa_a3(
            input_path=weird,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )
    assert "Activity Statement" in exc.value.message
    assert "Flex Query" in exc.value.message


# ============================================================================
# Pipeline + RunReport invariants (PR #125, issue #119)
# ============================================================================


def test_run_report_invariant_rows_written_must_be_non_negative() -> None:
    """RunReport.__post_init__ rejects negative rows_written."""
    with pytest.raises(InvariantError) as exc:
        RunReport(
            rows_written=-1,
            output_path=Path("/tmp/x"),
            manual_fill_columns=(),
            account_id="U1",
            account_id_cross_checked=True,
            input_format=IbkrCsvFormat.FLEX_QUERY,
            fx_policy_appropriate_for_ay=True,
            short_lots_excluded=0,
            orphan_dividends=(),
            portal_labels_assessment_year_start=2026,
            run_outputs=RunOutputs((), ()),
        )
    assert "rows_written must be non-negative" in exc.value.what


def test_run_report_invariant_empty_account_id_rejected() -> None:
    """RunReport.__post_init__ rejects empty account_id."""
    with pytest.raises(InvariantError) as exc:
        RunReport(
            rows_written=0,
            output_path=Path("/tmp/x"),
            manual_fill_columns=(),
            account_id="",
            account_id_cross_checked=True,
            input_format=IbkrCsvFormat.FLEX_QUERY,
            fx_policy_appropriate_for_ay=True,
            short_lots_excluded=0,
            orphan_dividends=(),
            portal_labels_assessment_year_start=2026,
            run_outputs=RunOutputs((), ()),
        )
    assert "account_id must be non-empty" in exc.value.what


def test_run_report_invariant_skipped_check_with_unmasked_account_rejected() -> None:
    """RunReport with cross_checked=False AND a real (unmasked)
    account_id means the safety net was bypassed without
    justification. The __post_init__ refuses to construct such a
    state."""
    with pytest.raises(InvariantError) as exc:
        RunReport(
            rows_written=0,
            output_path=Path("/tmp/x"),
            manual_fill_columns=(),
            account_id="U12345678",  # NOT masked
            account_id_cross_checked=False,
            input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
            fx_policy_appropriate_for_ay=True,
            short_lots_excluded=0,
            orphan_dividends=(),
            portal_labels_assessment_year_start=2026,
            run_outputs=RunOutputs((), ()),
        )
    assert "safety net was bypassed without justification" in exc.value.what
    # The InvariantError context must not embed the raw account ID
    # and must keep the `account_id` key (parallel to the
    # ScheduleOsRunReport equivalent in test_pipeline_schedule_os.py).
    # The message prints neither (#1046).
    rendered = str(exc.value)
    assert "U12345678" not in rendered
    assert ("account_id", None) in exc.value.context
    assert "U***5678" not in rendered


def test_run_report_invariant_skipped_check_with_line_break_after_mask_rejected() -> None:
    """#854: ``"U***\\n"`` is not IBKR's mask, so it can't justify a
    skipped cross-check."""
    with pytest.raises(InvariantError) as exc:
        RunReport(
            rows_written=0,
            output_path=Path("/tmp/x"),
            manual_fill_columns=(),
            account_id="U***\n",
            account_id_cross_checked=False,
            input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
            fx_policy_appropriate_for_ay=True,
            short_lots_excluded=0,
            orphan_dividends=(),
            portal_labels_assessment_year_start=2026,
            run_outputs=RunOutputs((), ()),
        )
    assert "safety net was bypassed without justification" in exc.value.what


def test_run_report_invariant_skipped_check_with_masked_account_accepted() -> None:
    """The inverse: a masked account ID legitimately pairs with
    cross_checked=False (the user passed --allow-masked-account)."""
    report = RunReport(
        rows_written=0,
        output_path=Path("/tmp/x"),
        manual_fill_columns=(),
        account_id="U**********",
        account_id_cross_checked=False,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    assert report.account_id_cross_checked is False


def test_run_report_invariant_withholding_tax_entries_must_be_non_negative() -> None:
    """RunReport.__post_init__ rejects negative withholding_tax_entries."""
    with pytest.raises(InvariantError) as exc:
        RunReport(
            rows_written=0,
            output_path=Path("/tmp/x"),
            manual_fill_columns=(),
            account_id="U1",
            account_id_cross_checked=True,
            input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
            fx_policy_appropriate_for_ay=True,
            short_lots_excluded=0,
            orphan_dividends=(),
            withholding_tax_entries=-1,
            portal_labels_assessment_year_start=2026,
            run_outputs=RunOutputs((), ()),
        )
    assert "withholding_tax_entries must be non-negative" in exc.value.what


def test_run_report_invariant_us_source_dividend_count_must_be_non_negative() -> None:
    """RunReport.__post_init__ rejects negative us_source_dividend_count."""
    with pytest.raises(InvariantError) as exc:
        RunReport(
            rows_written=0,
            output_path=Path("/tmp/x"),
            manual_fill_columns=(),
            account_id="U1",
            account_id_cross_checked=True,
            input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
            fx_policy_appropriate_for_ay=True,
            short_lots_excluded=0,
            orphan_dividends=(),
            us_source_dividend_count=-1,
            portal_labels_assessment_year_start=2026,
            run_outputs=RunOutputs((), ()),
        )
    assert "us_source_dividend_count must be non-negative" in exc.value.what


def test_run_report_invariant_us_source_withholding_tax_net_paid_must_be_non_negative() -> None:
    """RunReport.__post_init__ rejects negative us_source_withholding_tax_net_paid."""
    with pytest.raises(InvariantError) as exc:
        RunReport(
            rows_written=0,
            output_path=Path("/tmp/x"),
            manual_fill_columns=(),
            account_id="U1",
            account_id_cross_checked=True,
            input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
            fx_policy_appropriate_for_ay=True,
            short_lots_excluded=0,
            orphan_dividends=(),
            us_source_withholding_tax_net_paid=Decimal("-0.01"),
            portal_labels_assessment_year_start=2026,
            run_outputs=RunOutputs((), ()),
        )
    assert "us_source_withholding_tax_net_paid must be non-negative" in exc.value.what


def test_run_report_us_person_defaults_to_false() -> None:
    """us_person defaults to False (W-8BEN / non-US-person case)."""
    report = RunReport(
        rows_written=0,
        output_path=Path("/tmp/x"),
        manual_fill_columns=(),
        account_id="U1",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    assert report.us_person is False
    assert report.us_source_dividend_count == 0
    assert report.us_source_withholding_tax_net_paid == Decimal("0")


# ============================================================================
# AY cross-AY-verification warning (PR #132 / issue #130 / tracker #126)
# ============================================================================
# Behaviour summary after Codex's review of PR #132 (which surfaced
# that Rule 206(1) + Rule 207(3)(b) of the Income-tax Rules, 2026
# preserve the SBI TTBR rate source for resident FX conversion;
# carry-forward fallback and FA-specific date selection rest on CBDT FAQ /
# practice):
#   * CY <= 2025 -> fx_policy_appropriate_for_ay=True; no warning.
#   * CY >= 2026 -> proceeds; fx_policy_appropriate_for_ay=False;
#     CLI prints an informational warning that the FA-specific
#     date-selection semantics and the carry-forward fallback under
#     the new framework aren't yet verified by ibkr-fa.
#   * CY >= 2026 + suppress_cross_ay_warning=True ->
#     fx_policy_appropriate_for_ay=True; user has independently
#     verified and explicitly suppressed the warning.
# Critically, the pipeline NO LONGER BLOCKS on CY >= 2026 — Codex's
# finding showed the framework is largely preserved, so blocking by
# default was over-aggressive.


def test_cross_ay_warn_cy_2025_no_warning(tmp_path: Path) -> None:
    """CY 2025 maps to AY 2026-27, which is governed by the 1961 Act
    via Section 536 — exactly the framework ibkr-fa implements. No
    warning fires; fx_policy_appropriate_for_ay is True."""
    flex = write_flex_csv(
        tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION, _NO_TRADES_SECTION
    )
    cfg = write_user_toml(tmp_path)
    report = run_fa_a3(
        input_path=flex,
        config_path=cfg,
        output_path=tmp_path / "review.csv",
        calendar_year=2025,
    )
    assert report.fx_policy_appropriate_for_ay is True


def test_cross_ay_warn_cy_2026_proceeds_with_warning(tmp_path: Path) -> None:
    """CY 2026 -> AY 2027-28 -> 2025-Act framework. Per Tier-1
    review's verification of the gazette text (Rule 206(1) +
    Rule 207(3)(b) preserve the rate source for resident FX
    conversion), we DON'T block — proceeds with the cross-AY
    warning surface.

    The downstream pipeline still fails with MissingFxRateError
    because the bundled SBI TTBR archive doesn't extend to Dec 31
    2026 — but that's an archive-staleness issue, separate from
    the cross-AY warning. The point of THIS integration test is
    that the AY guard itself no longer raises ConfigError;
    end-to-end emission of the warning (with
    fx_policy_appropriate_for_ay=False propagating into a rendered
    RunReport) is covered by the unit-level helper tests
    (``test_cross_ay_warn_helper_*``) and the CLI render-path
    test in ``test_cli.py``."""
    open_positions_cy_2026 = _OPEN_POSITIONS_SECTION.replace(
        '"USD","1","STK","COMMON","XQBB","SAMPLE ISSUER BB INC","99995B104","US99995B1044","NASDAQ","US","20251231","10","95.4","954","85.2","852","Long","LOT","20250429","20250429"',  # noqa: E501
        '"USD","1","STK","COMMON","XQBB","SAMPLE ISSUER BB INC","99995B104","US99995B1044","NASDAQ","US","20261231","10","95.4","954","85.2","852","Long","LOT","20260430","20260430"',  # noqa: E501
    )
    assert '"20261231"' in open_positions_cy_2026
    flex = write_flex_csv(
        tmp_path,
        _ACCOUNT_INFO_SECTION,
        open_positions_cy_2026,
        _NO_TRADES_SECTION,
        calendar_year=2026,
    )
    cfg = write_user_toml(tmp_path)
    # The AY guard does NOT raise. The downstream MissingFxRateError
    # for Dec 31 2026 is the expected failure point, not the guard.
    with pytest.raises(MissingFxRateError):
        run_fa_a3(
            input_path=flex,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2026,
        )


def test_cross_ay_warn_helper_returns_true_for_pre_2026_cys() -> None:
    """Unit-level: CY <= 2025 always returns True (1961-Act
    framework that ibkr-fa implements is verified appropriate)."""
    assert _check_fx_policy_for_ay(calendar_year=2025, suppress_cross_ay_warning=False) is True
    assert _check_fx_policy_for_ay(calendar_year=2024, suppress_cross_ay_warning=False) is True
    # The flag is documented as a no-op for legitimately-supported CYs.
    assert _check_fx_policy_for_ay(calendar_year=2025, suppress_cross_ay_warning=True) is True


def test_cross_ay_warn_helper_returns_false_for_unverified_cys() -> None:
    """Unit-level: CY >= 2026 returns False by default — signalling
    the CLI to print the verification warning."""
    assert _check_fx_policy_for_ay(calendar_year=2026, suppress_cross_ay_warning=False) is False
    assert _check_fx_policy_for_ay(calendar_year=2030, suppress_cross_ay_warning=False) is False


def test_cross_ay_warn_helper_returns_true_when_user_suppresses() -> None:
    """Unit-level: suppress_cross_ay_warning=True flips the result
    to True — the user has independently verified and the warning
    is silenced."""
    assert _check_fx_policy_for_ay(calendar_year=2026, suppress_cross_ay_warning=True) is True
    assert _check_fx_policy_for_ay(calendar_year=2030, suppress_cross_ay_warning=True) is True


def test_cross_ay_warn_helper_does_not_raise() -> None:
    """Regression: prior to PR #132's Codex review, this helper
    raised ConfigError by default for CY >= 2026 — over-aggressive
    given the gazette text. Verify it now returns rather than
    raises."""
    # Should NOT raise.
    result = _check_fx_policy_for_ay(calendar_year=2026, suppress_cross_ay_warning=False)
    assert result is False


def test_cross_ay_warn_cy_2026_full_pipeline_with_synthetic_fx_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end exercise of the warn-not-block path for CY 2026:
    monkeypatch the TTBR archive to include synthetic CY 2026 rates
    so the pipeline completes successfully and we can assert the
    propagated RunReport.fx_policy_appropriate_for_ay=False.

    This is the test that proves the warning is actually emitted
    end-to-end (helper unit tests prove the boolean; this proves
    the boolean reaches RunReport on a successful run). The bundled
    archive can't satisfy CY 2026 today, hence monkeypatching."""

    # Build a synthetic archive covering both the synthetic
    # acquisition date and Dec 31 2026 — needs to satisfy load-time
    # invariants from PR #123 (rates in (1, 200], strictly monotonic
    # dates).
    synthetic_archive = {
        date(2026, 4, 30): Decimal("84.50"),
        date(2026, 12, 31): Decimal("85.20"),
    }
    fake_load = lambda _ccy: synthetic_archive  # noqa: E731
    # Patch both the loader module AND the binding imported into
    # policy_fa (which is what FaExactDatePolicy.__init__ calls).
    monkeypatch.setattr(ttbr_loader, "load_ttbr", fake_load)
    monkeypatch.setattr(policy_fa, "load_ttbr", fake_load)

    open_positions_cy_2026 = _OPEN_POSITIONS_SECTION.replace(
        '"USD","1","STK","COMMON","XQBB","SAMPLE ISSUER BB INC","99995B104","US99995B1044","NASDAQ","US","20251231","10","95.4","954","85.2","852","Long","LOT","20250429","20250429"',  # noqa: E501
        '"USD","1","STK","COMMON","XQBB","SAMPLE ISSUER BB INC","99995B104","US99995B1044","NASDAQ","US","20261231","10","95.4","954","85.2","852","Long","LOT","20260430","20260430"',  # noqa: E501
    )
    assert '"20261231"' in open_positions_cy_2026
    flex = write_flex_csv(
        tmp_path,
        _ACCOUNT_INFO_SECTION,
        open_positions_cy_2026,
        _NO_TRADES_SECTION,
        calendar_year=2026,
    )
    cfg = write_user_toml(tmp_path)
    report = run_fa_a3(
        input_path=flex,
        config_path=cfg,
        output_path=tmp_path / "review.csv",
        calendar_year=2026,
    )
    # The pipeline succeeded; fx_policy_appropriate_for_ay propagated.
    assert report.rows_written == 1
    assert report.fx_policy_appropriate_for_ay is False


def test_cross_ay_warn_cy_2026_full_pipeline_with_suppress_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inverse of the above: with --suppress-cross-ay-warning the
    field flips to True. End-to-end coverage of the four-way
    matrix CY x flag, via monkeypatched archive (since the bundled
    one doesn't cover Dec 31 2026)."""

    synthetic_archive = {
        date(2026, 4, 30): Decimal("84.50"),
        date(2026, 12, 31): Decimal("85.20"),
    }
    fake_load = lambda _ccy: synthetic_archive  # noqa: E731
    monkeypatch.setattr(ttbr_loader, "load_ttbr", fake_load)
    monkeypatch.setattr(policy_fa, "load_ttbr", fake_load)

    open_positions_cy_2026 = _OPEN_POSITIONS_SECTION.replace(
        '"USD","1","STK","COMMON","XQBB","SAMPLE ISSUER BB INC","99995B104","US99995B1044","NASDAQ","US","20251231","10","95.4","954","85.2","852","Long","LOT","20250429","20250429"',  # noqa: E501
        '"USD","1","STK","COMMON","XQBB","SAMPLE ISSUER BB INC","99995B104","US99995B1044","NASDAQ","US","20261231","10","95.4","954","85.2","852","Long","LOT","20260430","20260430"',  # noqa: E501
    )
    assert '"20261231"' in open_positions_cy_2026
    flex = write_flex_csv(
        tmp_path,
        _ACCOUNT_INFO_SECTION,
        open_positions_cy_2026,
        _NO_TRADES_SECTION,
        calendar_year=2026,
    )
    cfg = write_user_toml(tmp_path)
    report = run_fa_a3(
        input_path=flex,
        config_path=cfg,
        output_path=tmp_path / "review.csv",
        calendar_year=2026,
        suppress_cross_ay_warning=True,
    )
    assert report.rows_written == 1
    assert report.fx_policy_appropriate_for_ay is True


# ---------------------------------------------------------------------------
# display_account_id — PII-masking helper for report output
#
# Tier-3 Codex CLI review (PR #159) flagged that the CLI surfaced raw
# IBKR account IDs in stdout (`Account: U12345678`). Report output
# (CLI report headers, run-report fields) goes through
# `display_account_id`, which keeps the leading prefix letter(s) and the
# last 4 chars (`U***5678`, `DU***4567`) so the account owner can
# disambiguate while screenshots / pasted-into-issues snippets / shared
# session recordings do not leak the full identifier. Errors never print
# an account ID, masked or not, and a value not shaped like an account
# number renders as `<not an account number>` (#1047).
# ---------------------------------------------------------------------------


def test_display_account_id_masks_standard_eight_digit_id() -> None:
    """U + 8 digits is the most common IBKR account format; mask the
    middle 4 to keep last 4 visible."""
    assert display_account_id("U12345678") == "U***5678"


def test_display_account_id_masks_seven_digit_id() -> None:
    """Older IBKR accounts use 7 digits after the U prefix; still mask
    to last 4 (so the first 3 digits are hidden)."""
    assert display_account_id("U1234567") == "U***4567"


def test_display_account_id_masks_ten_digit_id() -> None:
    """Some institutional accounts use up to 10 digits after U."""
    assert display_account_id("U1234567890") == "U***7890"


def test_display_account_id_returns_already_masked_unchanged() -> None:
    """IBKR's default Activity Statement export already masks the ID
    to `U**********` — re-running through `display_account_id` must
    not produce `U***********` (double-mask) or `U***` (over-mask)."""
    assert display_account_id("U**********") == "U**********"


def test_display_account_id_returns_short_masked_unchanged() -> None:
    """A short mask like `U***` (4 chars) is already a placeholder;
    return unchanged."""
    assert display_account_id("U***") == "U***"


@pytest.mark.parametrize(
    ("account_id", "masked"),
    [("U*", True), ("U***", True), ("U**********", True), ("U***\n", False), ("U", False)],
)
def test_is_masked_account_id_matches_the_whole_value(account_id: str, masked: bool) -> None:
    """#854: ``$`` also matches just before a final line break, so
    ``"U***\\n"`` used to count as IBKR's mask."""
    assert is_masked_account_id(account_id) is masked


def test_display_account_id_does_not_mask_a_mask_with_a_line_break() -> None:
    """#854: a malformed mask isn't returned unchanged; it isn't shaped
    like an account number either, so it renders as the placeholder
    (#1047)."""
    assert display_account_id("U**********\n") == "<not an account number>"


@pytest.mark.parametrize(
    "account_id",
    [
        "ANN",
        "Lee",
        "U12",
        "Synthetic Holder Two",
        # Too few digits to be an account number.
        "U1",
        "U1234",
        "DU1234",
        "",
        # Not the whole value, more than two letters, or non-ASCII digits.
        "U1234567\n",
        " U1234567",
        "ABC1234567",
        "U\u0661\u0662\u0663\u0664\u0665\u0666\u0667",
    ],
)
def test_display_account_id_renders_a_non_account_value_as_a_placeholder(
    account_id: str,
) -> None:
    """#1047: only a value shaped like an account number (up to two ASCII
    letters, then at least five ASCII digits) is masked; anything else,
    such as a name in a column-shifted account cell, renders as a fixed
    placeholder, so no part of it reaches a report."""
    assert display_account_id(account_id) == "<not an account number>"


def test_display_account_id_masks_six_char_input_hides_single_middle() -> None:
    """6 chars (``U`` + 5) is the shortest input the helper will
    actively mask for a single-letter prefix: the single middle char
    between ``U`` and the last-4 suffix is hidden behind the asterisk
    block. Output is longer than input (6 → 8) because the asterisk
    block is a fixed 3-asterisk marker, not a 1-for-1 substitution."""
    assert display_account_id("U12345") == "U***2345"


def test_display_account_id_preserves_two_letter_prefix() -> None:
    """IBKR account IDs can have 2-letter prefixes (``DU`` for
    advisor sub-accounts / paper-trading, ``DF`` for master Financial
    Advisor accounts per the IBKR TWS API docs). The full prefix is
    preserved in the masked rendering so the user can disambiguate
    not just which account but which account *family* a report
    refers to. The prefix detection looks at the leading 1-2 ASCII
    letters and stops at the first digit."""
    assert display_account_id("DU1234567") == "DU***4567"
    assert display_account_id("DF12345678") == "DF***5678"


def test_display_account_id_masks_seven_char_two_letter_prefix_id() -> None:
    """7 chars (``DF`` + 5) is the shortest input the helper will
    actively mask for a 2-letter prefix; this is the minimum length
    of a documented IBKR account ID (``DF12345``, per IBKR TWS API
    "Model Allocations" docs). Output is longer than input (7 → 9)
    due to the fixed 3-asterisk block."""
    assert display_account_id("DF12345") == "DF***2345"


def test_display_account_id_preserves_last_four_for_disambiguation() -> None:
    """The contract is 'mask middle, keep last 4' — verify last 4
    chars survive masking so the user can identify which account a
    given report refers to."""
    masked = display_account_id("U99999999")
    assert masked.endswith("9999")
    assert masked.startswith("U***")
    # And ensure no raw digit other than the last 4 leaks.
    assert "1" not in masked and "9999" in masked


def test_cross_check_config_error_message_does_not_leak_raw_account_ids(
    tmp_path: Path,
) -> None:
    """Defense-in-depth regression: the mismatch ConfigError message
    must contain neither account ID, raw or masked (#1047). If a future
    change prints either account again, this test fails fast."""
    flex = write_flex_csv(
        tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION, _NO_TRADES_SECTION
    )
    cfg = write_user_toml(tmp_path, account_number="U99999999")
    with pytest.raises(ConfigError) as exc:
        run_fa_a3(
            input_path=flex,
            config_path=cfg,
            output_path=tmp_path / "review.csv",
            calendar_year=2025,
        )
    msg = str(exc.value)
    assert "U12345678" not in msg, "raw parsed account leaked into ConfigError message"
    assert "U99999999" not in msg, "raw config account leaked into ConfigError message"
    assert "U***5678" not in msg, "masked parsed account leaked into ConfigError message"
    assert "U***9999" not in msg, "masked config account leaked into ConfigError message"


@pytest.mark.parametrize("allow_masked_account", [False, True])
def test_cross_check_compares_a_mask_with_a_line_break(allow_masked_account: bool) -> None:
    """#854: ``"U***\\n"`` is not IBKR's mask, so ``--allow-masked-account``
    can't skip the cross-check, and the comparison refuses it with the
    mismatch refusal, which prints neither account (#1047)."""
    account_info = AccountInfo(
        account_id=AccountId("U***\n"),
        account_holder_name="Test Holder",
        base_currency="USD",
    )
    with pytest.raises(ConfigError) as exc:
        _cross_check_account_id(
            account_info,
            config_account_number="U12345678",
            allow_masked_account=allow_masked_account,
        )
    assert exc.value.field == "account.number"
    assert exc.value.message == ACCOUNT_MISMATCH_REFUSAL
    _assert_absent(str(exc.value), "U***", "U12345678", "5678")


# ---- #142 OrphanDividendInfo invariants ---------------------------------


def test_orphan_dividend_info_rejects_unknown_classification() -> None:
    """#142 / T1-correctness PR #170 P2: the classification field is
    pinned to a closed set ({'previously_held', 'never_held'}) via
    __post_init__. Any other string is rejected as InvariantError —
    drift detector for a future refactor that drops the guard.
    """
    with pytest.raises(InvariantError) as exc:
        OrphanDividendInfo(
            symbol=Ticker("BAD"),
            classification="held",  # ty: ignore[invalid-argument-type]
            total_amount_inr=Decimal("100"),
            latest_payment_date=date(2025, 1, 1),
            last_sold_date=None,
        )
    assert "classification" in exc.value.what
    assert "previously_held" in exc.value.what
    assert "never_held" in exc.value.what


def test_orphan_dividend_info_rejects_never_held_with_last_sold_date() -> None:
    """#142 / T1-correctness PR #170 P2: the cross-field invariant
    'never_held implies last_sold_date is None' is enforced. A
    never_held record with a populated last_sold_date is structurally
    contradictory (the symbol never appeared in Trades → no sale date
    to reference).
    """
    with pytest.raises(InvariantError) as exc:
        OrphanDividendInfo(
            symbol=Ticker("BAD"),
            classification="never_held",
            total_amount_inr=Decimal("100"),
            latest_payment_date=date(2025, 1, 1),
            last_sold_date=date(2025, 1, 2),
        )
    assert "never_held" in exc.value.what
    assert "last_sold_date" in exc.value.what


def test_orphan_dividend_info_accepts_previously_held_with_none_last_sold_date() -> None:
    """#142: previously_held legitimately allows last_sold_date=None
    when the symbol only has OPEN-side trades (never closed in-period).
    Lock that the cross-field invariant doesn't over-fire."""
    info = OrphanDividendInfo(
        symbol=Ticker("OK"),
        classification="previously_held",
        total_amount_inr=Decimal("100"),
        latest_payment_date=date(2025, 1, 1),
        last_sold_date=None,
    )
    assert info.classification == "previously_held"
    assert info.last_sold_date is None


def test_orphan_dividend_classifier_excludes_short_lots_from_previously_held(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#142 / T3 PR #170 P2: SHORT-only orphan dividends must NOT
    classify as 'previously_held'. A short open is a liability, not
    an 'owned and sold' holding — classifying it as previously_held
    would emit the misleading 'sale belongs on Schedule CG' advisory.
    The classifier filters SHORT opens from traded_symbols so SHORT-
    only orphans route to 'never_held' (whose advisory is less
    specific but not actively wrong).

    Monkeypatches _dispatch_activity_statement to return a synthetic
    SourceBundle — easier than crafting a full SHORT-with-PIL fixture.
    """
    short_lot = OpenLot(
        currency_primary="USD",
        symbol=Ticker("SHRTSYM"),
        description="hypothetical short",
        isin=Isin("US99971K1024"),
        issuer_country_code=CountryCode("US"),
        report_date=date(2025, 12, 31),
        quantity=Decimal("10"),
        mark_price=Decimal("100"),
        cost_basis_money=Decimal("950"),
        side=Side.SHORT,
        open_date_time=date(2025, 6, 1),
    )
    pil_dividend = Dividend(
        symbol=Ticker("SHRTSYM"),
        isin=Isin("US99971K1024"),
        payment_date=date(2025, 9, 30),
        amount_gross=Decimal("5"),
        currency="USD",
        description="SHRTSYM(US99971K1024) Payment In Lieu Of Dividend",
    )

    def fake_dispatch(*_args: object, **_kwargs: object) -> SourceBundle:
        return SourceBundle(
            account_info=AccountInfo(
                account_id=AccountId("U12345678"),
                account_holder_name="Test Holder",
                base_currency="USD",
            ),
            open_lots=[short_lot],
            closed_lots=[],
            dividends=[pil_dividend],
            withholding_tax=[],
            interest_credits=[],
            interest_non_credits=[],
            identity_fallback_used=frozenset(),
        )

    monkeypatch.setattr("ibkr_fa.pipeline_fa._dispatch_activity_statement", fake_dispatch)
    monkeypatch.setattr(
        "ibkr_fa.pipeline_fa.detect_ibkr_csv_format", lambda _: IbkrCsvFormat.ACTIVITY_STATEMENT
    )

    tmp = tmp_path / "test_orphan_short.csv"
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "test_orphan_short_out.csv"
    report = run_fa_a3(
        input_path=tmp,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
    )
    short_orphans = [o for o in report.orphan_dividends if str(o.symbol) == "SHRTSYM"]
    assert len(short_orphans) == 1
    [short_orphan] = short_orphans
    # CRUCIAL: classified as never_held, NOT previously_held.
    # If this regresses, the user will see the misleading
    # "previously owned and sold; sale belongs on Schedule CG"
    # advisory for a position they never owned (only borrowed).
    assert short_orphan.classification == "never_held"
    assert short_orphan.last_sold_date is None


# ---- Writers are built from the run's RenderContext (#644) -----------------


def test_from_context_threads_the_assessment_year() -> None:
    """A writer built from the run's context uses that AY's portal-labels
    snapshot. (A writer without ``from_context`` is refused at registration:
    see ``tests/test_output_registry.py``.)"""

    from ibkr_fa.output.base import RenderContext  # noqa: PLC0415
    from ibkr_fa.output.review_csv import ReviewCsvWriter  # noqa: PLC0415

    writer = ReviewCsvWriter.from_context(RenderContext(assessment_year_start=2026))
    assert isinstance(writer, ReviewCsvWriter)
    # _labels module attribute should be the AY-2026 snapshot module.
    assert writer._labels.__name__.endswith("_ay_2026_27")  # type: ignore[attr-defined]


# ---- One ISIN per priced ticker (#916) -------------------------------------

_X_ISIN_A = "US99971K1025"
_X_ISIN_B = "US99971L1008"
_OPEN_POSITIONS_HEADER = _OPEN_POSITIONS_SECTION.splitlines()[0]
_TRADES_HEADER = _TRADES_SECTION_WITH_CLOSED_LOT.splitlines()[0]
_X_OVERRIDE = f"""
[[prices.ticker_overrides]]
ibkr_ticker = "X"
isin = "{_X_ISIN_A}"
yahoo_symbol = "AAA"
"""


def _x_open_positions(*rows: tuple[str, str]) -> str:
    """Open Positions holding one share of ``X`` per ``(isin, side)``."""
    return "\n".join(
        [
            _OPEN_POSITIONS_HEADER,
            *(
                f'"USD","1","STK","COMMON","X","X CORP","99971J105","{isin}","NYSE","US",'
                f'"20251231","1","10","10","8","8","{side}","LOT","20250102","20250102"'
                for isin, side in rows
            ),
        ]
    )


def _x_closed_lot(isin: str, *, opened: str, sold: str) -> str:
    """Trades section with one share of ``X`` bought on ``opened`` and sold on ``sold``."""
    return (
        f"{_TRADES_HEADER}\n"
        f'"USD","1","STK","COMMON","X","X CORP","99971J105","{isin}","NYSE","US",'
        f'"{sold}","{sold}","","ExchTrade","-1","10","-10","10","0","0","USD","10","10","C",'
        f'"-9","1","SELL","EXECUTION","",""\n'
        f'"USD","1","STK","COMMON","X","X CORP","99971J105","{isin}","NYSE","US",'
        f'"{sold}","{sold}","","","1","9","","","","","","","","C","9","1","SELL",'
        f'"CLOSED_LOT","{opened};100000","{opened};100000"'
    )


class _RecordingCloses:
    """Price-fetcher double: ``AAA`` closes at $100 and every other symbol at
    $10 on every US market session. Records each ``(ticker, fetch_symbol)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, ticker, *, start, end, now, fetch_symbol=None):
        self.calls.append((str(ticker), fetch_symbol))
        close = Decimal(100 if fetch_symbol == "AAA" else 10)
        return PriceSeries(
            ticker=ticker,
            currency="USD",
            closes=dict.fromkeys(us_market_sessions(start, end), close),
            has_split_in_range=False,
            source="test-double",
            fetched_at=datetime(2026, 6, 1, tzinfo=UTC),
        )


def _run_x(
    tmp_path: Path, fetcher: _RecordingCloses, *sections: str, config_extra: str = ""
) -> RunReport:
    # From 2024: one case's lot was sold in 2024 (#1313).
    flex = write_flex_csv(tmp_path, _ACCOUNT_INFO_SECTION, *sections, start="20240101")
    cfg = write_user_toml(tmp_path)
    cfg.write_text(cfg.read_text(encoding="utf-8") + config_extra, encoding="utf-8")
    return run_fa_a3(
        input_path=flex,
        config_path=cfg,
        output_path=tmp_path / "review.csv",
        calendar_year=2025,
        fetch_prices=True,
        price_cache_dir=tmp_path / "price-cache",
        price_fetcher=fetcher,
        price_now=datetime(2026, 6, 1, tzinfo=UTC),
    )


# The ticker/ISIN collision refusal counts the colliding tickers and names the
# CY; it never prints a symbol or an ISIN (#1047).
_TICKER_ISIN_COLLISION_REFUSAL = (
    "Schedule FA prices every lot of a ticker from one daily-close series, but 1 "
    "ticker(s) name more than one ISIN among the lots priced for CY 2025; check "
    "your lots for a symbol shared by two securities."
)


def _assert_no_collision_values(message: str) -> None:
    """Neither ISIN nor the ticker ``X`` (as a whole token) is in ``message``."""
    _assert_absent(message, _X_ISIN_A, _X_ISIN_B)
    assert re.search(r"(?i)\bX\b", message) is None


@pytest.mark.parametrize("config_extra", [_X_OVERRIDE, ""], ids=["override", "no-override"])
def test_ticker_with_two_isins_refuses_before_pricing(tmp_path: Path, config_extra: str) -> None:
    """Open ``X`` / ISIN A and ``X`` / ISIN B sold in the CY would read one
    series. With the A override, B's peak would come from ``AAA``'s $100
    instead of B's own $10; without one, both read Yahoo's ``X``. The
    refusal counts the ticker but prints neither it nor the ISINs (#1047)."""
    fetcher = _RecordingCloses()
    with pytest.raises(TickerIsinCollisionError) as excinfo:
        _run_x(
            tmp_path,
            fetcher,
            _x_open_positions((_X_ISIN_A, "Long")),
            _x_closed_lot(_X_ISIN_B, opened="20250303", sold="20250616"),
            config_extra=config_extra,
        )
    assert str(excinfo.value) == _TICKER_ISIN_COLLISION_REFUSAL
    _assert_no_collision_values(str(excinfo.value))
    assert fetcher.calls == []
    assert not (tmp_path / "review.csv").exists()
    assert not (tmp_path / "price-cache").exists()


@pytest.mark.parametrize(
    "sections",
    [
        pytest.param(
            (
                _x_open_positions((_X_ISIN_A, "Long")),
                _x_closed_lot(_X_ISIN_B, opened="20240301", sold="20240603"),
            ),
            id="sold-before-cy",
        ),
        pytest.param(
            (_x_open_positions((_X_ISIN_A, "Long"), (_X_ISIN_B, "Short")), _NO_TRADES_SECTION),
            id="short-position",
        ),
    ],
)
def test_ticker_isin_check_skips_lots_the_cy_never_prices(
    tmp_path: Path, sections: tuple[str, ...]
) -> None:
    """An ``X`` / ISIN B lot that this year's pricing never sees leaves the
    long ``X`` / ISIN A lot priced and written."""
    fetcher = _RecordingCloses()
    report = _run_x(tmp_path, fetcher, *sections)
    assert report.rows_written == 1
    assert fetcher.calls == [("X", None)]
    with (tmp_path / "review.csv").open(encoding="utf-8", newline="") as fh:
        [row] = list(csv.DictReader(fh))
    assert _X_ISIN_A in row.values()


def test_one_isin_across_several_lots_of_a_ticker_is_priced(tmp_path: Path) -> None:
    fetcher = _RecordingCloses()
    report = _run_x(
        tmp_path,
        fetcher,
        _x_open_positions((_X_ISIN_A, "Long")),
        _x_closed_lot(_X_ISIN_A, opened="20250303", sold="20250616"),
        config_extra=_X_OVERRIDE,
    )
    assert report.rows_written == 2
    assert fetcher.calls == [("X", "AAA")]


def test_cli_ticker_with_two_isins_exits_4_and_clears_stale_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    flex = write_flex_csv(
        tmp_path,
        _ACCOUNT_INFO_SECTION,
        _x_open_positions((_X_ISIN_A, "Long")),
        _x_closed_lot(_X_ISIN_B, opened="20250303", sold="20250616"),
    )
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "schedule-fa.csv"
    out.write_text("stale\n", encoding="utf-8")

    rc = _run_schedule_fa(flex, cfg, out)

    assert rc == 4
    err = capsys.readouterr().err
    assert err == f"error: {_TICKER_ISIN_COLLISION_REFUSAL}\n"
    _assert_no_collision_values(err)
    assert not out.exists()
