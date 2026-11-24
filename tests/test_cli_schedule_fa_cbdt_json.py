"""PR4 wiring of the CBDT Schedule FA JSON fragment into `schedule-fa` (#39).

Pipeline-level tests drive ``run_fa_a3(format_id="cbdt-json-fragment", ...)``
against a synthetic full-CY Activity Statement (positions + NAV) with an injected
flat price fetcher (so A3 peak values populate offline). CLI-level tests drive
``main([...])`` for the argparse surface, pre-clear, and exit codes, monkeypatching
``run_fa_a3`` where a real statement is unnecessary.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, cast

import pytest

import ibkr_fa.pipeline_fa_cbdt_json as cbdt_mod
from ibkr_fa.cli import _preclear_cbdt_json_target, main
from ibkr_fa.compute.robinhood_entity_nature import merge_robinhood_entity_natures
from ibkr_fa.compute.robinhood_identity import (
    RobinhoodEntityRoute,
    RobinhoodFoundEntityAddress,
    robinhood_entity_override_supersession_warnings,
    robinhood_unverified_entity_addresses,
)
from ibkr_fa.config import FaEntityOverride, load_user_config
from ibkr_fa.models.fa_a2_row import FaA2FiledValues, FaA2Row
from ibkr_fa.models.fa_a3_row import FaA3FiledValues, FaA3Row
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.output.cbdt_json_fragment import (
    CbdtJsonIncompleteError,
    CbdtJsonStructuralError,
    is_ibkr_fa_cbdt_fragment,
)
from ibkr_fa.output.cbdt_json_schema import CbdtItrForm
from ibkr_fa.parser.format_detect import IbkrCsvFormat
from ibkr_fa.pipeline_fa import run_fa_a3
from ibkr_fa.pipeline_fa_models import RunReport
from ibkr_fa.types import Isin
from tests._ibkr_flex_cli_inputs import earlier_schedule_fa_outputs
from tests._ibkr_schedule_fa_inputs import CY_2025_NAV_INPUT as _FIXTURE
from tests._ibkr_schedule_fa_inputs import SCHEDULE_FA_CONFIG as _FULL_CONFIG
from tests._ibkr_schedule_fa_inputs import SCHEDULE_FA_CONFIG_NO_PEAK as _CONFIG_NO_PEAK
from tests._ibkr_schedule_fa_inputs import flat_close_fetcher as _flat_close_fetcher
from tests._ibkr_user_toml import CUSTODIAN_TOML
from tests._robinhood_security import robinhood_security


def _config(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "user.toml"
    p.write_text(text, encoding="utf-8")
    return p


def _statement(tmp_path: Path, *, transform=None) -> Path:
    """Copy the NAV fixture into tmp_path, optionally transforming its lines."""
    text = _FIXTURE.read_text(encoding="utf-8")
    if transform is not None:
        edited = transform(text)
        assert edited != text, "the transform did not change the fixture"
        text = edited
    p = tmp_path / "stmt.csv"
    p.write_text(text, encoding="utf-8")
    return p


def _run_json(
    tmp_path: Path,
    *,
    config_text: str = _FULL_CONFIG,
    transform=None,
    itr_form: CbdtItrForm = "ITR-2",
    **overrides,
):
    return run_fa_a3(
        input_path=_statement(tmp_path, transform=transform),
        config_path=_config(tmp_path, config_text),
        output_path=tmp_path / "schedule-fa.json",
        calendar_year=2025,
        format_id="cbdt-json-fragment",
        itr_form=itr_form,
        fetch_prices=True,
        price_cache_dir=tmp_path / ".price-cache",
        price_fetcher=_flat_close_fetcher("100"),
        price_now=datetime(2026, 6, 1, tzinfo=UTC),
        **overrides,
    )


# ---- pipeline happy path --------------------------------------------------


def test_emits_valid_json_fragment(tmp_path: Path) -> None:
    report = _run_json(tmp_path)
    out = tmp_path / "schedule-fa.json"
    assert report.emitted_cbdt_json is True
    assert report.manual_fill_columns == ()
    assert report.a2_manual_fill_columns == ()
    assert is_ibkr_fa_cbdt_fragment(out)
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["_ibkrFa"]["format"] == "cbdt-json-fragment"
    sched = doc["ScheduleFA"]
    assert len(sched["DtlsForeignCustodialAcc"]) == 1
    assert len(sched["DtlsForeignEquityDebtInterest"]) == 3  # 2 XQBB lots + 1 XQBE lot
    a2 = sched["DtlsForeignCustodialAcc"][0]
    assert a2["PeakBalanceDuringPeriod"] == 250000  # from config
    assert a2["AccountNumber"] == "U12345678"
    assert a2["ZipCode"] == "06830"


def test_json_emits_one_a2_row_per_credit_nature(tmp_path: Path) -> None:
    def add_credits(text: str) -> str:
        rows = (
            "Dividends,Header,Currency,Date,Description,Amount,,,,,,,,,,,\n"
            "Dividends,Data,USD,16-03-2025,"
            "XQBB(US99995B1044) Cash Dividend USD 0.50 per Share "
            "(Ordinary Dividend),5.00,,,,,,,,,,,\n"
            "Dividends,Data,Total,,,5.00,,,,,,,,,,,\n"
            "Interest,Header,Currency,Date,Description,Amount,,,,,,,,,,,\n"
            "Interest,Data,USD,30-04-2025,USD Credit Interest for Apr-2025,"
            "10.00,,,,,,,,,,,\n"
            "Interest,Data,Total,,,10.00,,,,,,,,,,,\n"
        )
        # Change in NAV prints each section's total, as schedule-fa checks (#1324).
        ending_value = "Change in NAV,Data,Ending Value,"
        assert text.count(ending_value) == 1
        return text.replace("Open Positions,Header", rows + "Open Positions,Header").replace(
            ending_value,
            f"Change in NAV,Data,Dividends,5.00\nChange in NAV,Data,Interest,10.00\n{ending_value}",
        )

    report = _run_json(tmp_path, transform=add_credits)
    doc = json.loads((tmp_path / "schedule-fa.json").read_text(encoding="utf-8"))
    rows = doc["ScheduleFA"]["DtlsForeignCustodialAcc"]

    assert len(report.a2_rows) == len(rows) == 2
    assert [row["NatureOfAmount"] for row in rows] == ["I", "D"]
    assert all(row["AccountNumber"] == "U12345678" for row in rows)


def test_emits_itr3_bound_fragment(tmp_path: Path) -> None:
    _run_json(tmp_path, itr_form="ITR-3")
    doc = json.loads((tmp_path / "schedule-fa.json").read_text(encoding="utf-8"))
    assert doc["_ibkrFa"]["itrForm"] == "ITR-3"


def test_a2_sibling_csv_not_written_in_json_mode(tmp_path: Path) -> None:
    _run_json(tmp_path)
    assert not (tmp_path / "schedule-fa-a2.json").exists()
    assert not (tmp_path / "schedule-fa-a2.csv").exists()


def test_report_carries_peak_provenance(tmp_path: Path) -> None:
    report = _run_json(tmp_path)
    assert report.cbdt_json_a2_peak_balance_inr == 250000
    (row,) = report.a2_rows
    assert row.peak_balance_inr == Decimal("250000")
    assert row.audit_peak_override_source == "[[schedule_fa.manual_values]] calendar_year = 2025"
    assert row.audit_peak_override_reason == (
        "user-supplied: the statement has no daily account value to compute the peak from"
    )


def test_report_peak_matches_the_filed_row_instead_of_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cbdt_mod.build_fa_a2

    def distinct_row_peak(**kwargs):
        return tuple(
            replace(row, peak_balance_inr=Decimal("12344.50")) for row in original(**kwargs)
        )

    monkeypatch.setattr(cbdt_mod, "build_fa_a2", distinct_row_peak)
    report = _run_json(tmp_path)
    doc = json.loads((tmp_path / "schedule-fa.json").read_text(encoding="utf-8"))
    assert report.a2_rows[0].peak_balance_inr == Decimal("12344.50")
    assert (
        report.cbdt_json_a2_peak_balance_inr
        == (doc["ScheduleFA"]["DtlsForeignCustodialAcc"][0]["PeakBalanceDuringPeriod"])
        == 12345
    )


def test_report_peak_is_the_a2_rows_filed_peak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1077: the report reads the row's filed peak, as the fragment does, and
    never rounds it."""
    seven = Decimal("7")
    monkeypatch.setattr(FaA3Row, "filed", property(lambda _row: FaA3FiledValues(*(seven,) * 5)))
    monkeypatch.setattr(FaA2Row, "filed", property(lambda _row: FaA2FiledValues(*(seven,) * 3)))
    report = _run_json(tmp_path)
    doc = json.loads((tmp_path / "schedule-fa.json").read_text(encoding="utf-8"))
    assert report.a2_rows[0].peak_balance_inr == Decimal("250000")
    assert report.cbdt_json_a2_peak_balance_inr == 7
    assert doc["ScheduleFA"]["DtlsForeignCustodialAcc"][0]["PeakBalanceDuringPeriod"] == 7


def _a3_item(
    name: str, address: str, zip_code: str, acquired: str, *amounts: int
) -> dict[str, object]:
    initial, peak, closing = amounts
    return {
        "CountryName": "United States of America",
        "CountryCodeExcludingIndia": "2",
        "NameOfEntity": name,
        "AddressOfEntity": address,
        "ZipCode": zip_code,
        "NatureOfEntity": "Company",
        "InterestAcquiringDate": acquired,
        "InitialValOfInvstmnt": initial,
        "PeakBalanceDuringPeriod": peak,
        "ClosingBalance": closing,
        "TotGrossAmtPaidCredited": 0,
        "TotGrossProceeds": 0,
    }


_XQBB = ("SAMPLE ISSUER BB INC", "1 Sample Issuer Street, Sample City, XX", "00001")
_XQBE = ("SAMPLE ISSUER BE CORP", "2 Sample Issuer Street, Sample City, XX", "00002")

# The whole fragment _FULL_CONFIG files, byte for byte (#619 characterization).
# A3 initial = USD cost x the acquisition-date TTBR; closing = shares x the Dec-31
# close (95.40, 263.40) x 89.47. The flat 100 fetcher gives each lot a native peak
# of shares x 100 on its first session: 400 x 85.95 = 34380 and 600 x 85.80 = 51480
# for the two XQBB lots (above their closings), while XQBE's 400 is below its
# 1053.60 closing, so its peak is floored to the closing value.
# XQBB 2025-01-15: 320.50 x 85.95 = 27546.975 -> 27547; 4 x 95.40 x 89.47 = 34141.752 -> 34142.
# XQBB 2025-03-20: 540.50 x 85.80 = 46374.90 -> 46375; 6 x 95.40 x 89.47 = 51212.628 -> 51213.
# XQBE 2025-01-15: 1005.40 x 85.95 = 86414.13 -> 86414; 4 x 263.40 x 89.47 = 94265.592 -> 94266.
# A2 closing: NAV Total 2107.60 x 89.47 = 188566.972 -> 188567.
_EXPECTED_FRAGMENT = {
    "_ibkrFa": {
        "format": "cbdt-json-fragment",
        "itrForm": "ITR-2",
        "schemaVersion": "ITR-2 AY2026-27 Main V1.2 (2026-08-13)",
    },
    "ScheduleFA": {
        "DtlsForeignCustodialAcc": [
            {
                "CountryName": "UNITED STATES OF AMERICA",
                "CountryCodeExcludingIndia": "2",
                "FinancialInstName": "Interactive Brokers LLC",
                "FinancialInstAddress": "One Pickwick Plaza, Greenwich, CT",
                "ZipCode": "06830",
                "AccountNumber": "U12345678",
                "Status": "BENEFICIAL_OWNER",
                "AccOpenDate": "2020-01-01",
                "PeakBalanceDuringPeriod": 250000,
                "ClosingBalance": 188567,
                "GrossAmtPaidCredited": 0,
                "NatureOfAmount": "N",
            }
        ],
        "DtlsForeignEquityDebtInterest": [
            _a3_item(*_XQBB, "2025-01-15", 27547, 34380, 34142),
            _a3_item(*_XQBB, "2025-03-20", 46375, 51480, 51213),
            _a3_item(*_XQBE, "2025-01-15", 86414, 94266, 94266),
        ],
    },
}


def test_fragment_document_is_pinned(tmp_path: Path) -> None:
    _run_json(tmp_path)
    assert (tmp_path / "schedule-fa.json").read_text(encoding="utf-8") == (
        json.dumps(_EXPECTED_FRAGMENT, ensure_ascii=False, indent=2) + "\n"
    )


# ---- pipeline refusals ----------------------------------------------------


def test_refuse_when_peak_unsupplied(tmp_path: Path) -> None:
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        _run_json(tmp_path, config_text=_CONFIG_NO_PEAK)
    assert "A2 PeakBalanceDuringPeriod" in str(exc.value)
    assert not (tmp_path / "schedule-fa.json").exists()


def test_cli_refuses_a3_peak_with_a_missing_session_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#939: XQBE's series lacks Wednesday 2025-06-04, a session, so its A3 peak
    is blank, and a fragment cannot hold a blank: no file, exit 3. The gap names
    the row, not its ISIN or acquisition date (#1048)."""
    flat = _flat_close_fetcher("100")

    def _fetch(ticker, *, start, end, now, fetch_symbol=None):
        series = flat(ticker, start=start, end=end, now=now, fetch_symbol=fetch_symbol)
        if ticker != "XQBE":
            return series
        closes = {day: close for day, close in series.closes.items() if day != date(2025, 6, 4)}
        return replace(series, closes=closes)

    monkeypatch.setattr(
        "ibkr_fa.cli.run_fa_a3",
        partial(run_fa_a3, price_fetcher=_fetch, price_now=datetime(2026, 6, 1, tzinfo=UTC)),
    )
    out = tmp_path / "schedule-fa.json"
    args = _cli_args(tmp_path, out)
    args[args.index("--input") + 1] = str(_statement(tmp_path))
    assert main(args) == 3
    err = capsys.readouterr().err
    # #962: the gap says a session has no close (without its date, #1048) and
    # where to enter the peak by hand, not a re-fetch.
    assert err == (
        "error: Refusing to emit the CBDT Schedule FA JSON fragment — it would file an "
        "incomplete disclosure. Supply the following and re-run:\n"
        "  - A3 row 3 PeakBalanceDuringPeriod: the peak is blank because a US market "
        "session in the holding window has no close: a dropped bar, a trading halt or "
        "delisting, or, in a year whose US market closures the tool doesn't list yet, a "
        "market holiday. The peak is never guessed: enter it by hand. If the session is a "
        "market holiday the tool doesn't list, adding that year's US market closures lets "
        "a later run compute it. A ticker override or a re-fetch won't help. No config "
        "value can supply an A3 peak itself, so to enter it by hand use the review CSV or "
        "xlsx output instead\n"
    )
    assert "price fetching enabled" not in err
    # #1048: neither XQBE's ISIN nor its acquisition date.
    for value in ("US99995D1000", "2025-01-15"):
        assert value.lower() not in err.lower()
    assert not out.exists()


def test_cli_refuses_an_unconfirmed_nav_as_a_missing_a2_closing_balance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#906: no Total row, a blank Stock amount and no Change in NAV. The
    parser used to file Cash alone (100); now the A2 closing balance is
    missing, so no fragment is written and the run exits 3."""

    def partial_nav(text: str) -> str:
        edited = text.replace(
            "Net Asset Value,Data,Stock,0,2007.60,0,2007.60,2007.60\n"
            "Net Asset Value,Data,Total,0,2107.60,0,2107.60,2107.60\n"
            "Change in NAV,Header,Field Name,Field Value\n"
            "Change in NAV,Data,Ending Value,2107.60\n",
            "Net Asset Value,Data,Stock,0,2007.60,0,,2007.60\n",
        )
        assert edited != text
        return edited

    monkeypatch.setattr(
        "ibkr_fa.cli.run_fa_a3",
        partial(
            run_fa_a3,
            price_fetcher=_flat_close_fetcher("100"),
            price_now=datetime(2026, 6, 1, tzinfo=UTC),
        ),
    )
    out = tmp_path / "schedule-fa.json"
    assert _main_json(tmp_path, out, transform=partial_nav) == 3
    assert capsys.readouterr().err == (
        "error: Refusing to emit the CBDT Schedule FA JSON fragment — it would file an "
        "incomplete disclosure. Supply the following and re-run:\n"
        "  - A2 ClosingBalance: the closing balance is blank because the Net Asset Value "
        "table has no 'Total' row, and an asset-class row has a blank 'Current Total', so "
        "the rows can't confirm the 'Change in NAV' 'Ending Value'. If the file was edited "
        "or cut short, re-export the Activity Statement from IBKR and re-run. Otherwise, "
        "enter it by hand in the review CSV or XLSX, or in the portal: the account's Net "
        "Asset Value on 31 December of the requested calendar year, in its base currency, "
        "converted to INR at that date's SBI TTBR. No config value can supply an A2 closing "
        "balance, so to enter it by hand use the review CSV or xlsx output instead\n"
    )
    assert not out.exists()


_NAV_CASH_ROW = "Net Asset Value,Data,Cash,0,100.00,0,100.00,100.00\n"
_NAV_STOCK_ROW = "Net Asset Value,Data,Stock,0,2007.60,0,2007.60,2007.60\n"
_NAV_TOTAL_ROW = "Net Asset Value,Data,Total,0,2107.60,0,2107.60,2107.60\n"
_ENDING_VALUE_ROW = "Change in NAV,Data,Ending Value,2107.60\n"


def _edit(*replacements: tuple[str, str]) -> Callable[[str], str]:
    """A transform making each ``(old, new)`` replacement of one fixture line."""

    def transform(text: str) -> str:
        for old, new in replacements:
            assert text.count(old) == 1, old
            text = text.replace(old, new)
        return text

    return transform


_CONTRADICTION_REMEDY = (
    "The statement contradicts itself, so Schedule FA Table A2's closing balance can't be "
    "filed. Re-download the Activity Statement from IBKR or fix the file, then re-run."
)
_TOTAL_BREAKDOWN_ERROR = (
    "error: ParseError in section 'Net Asset Value' at row 2, column 'Current Total': the "
    "asset-class rows' Current Total amounts don't add up to this Total row's Current Total "
    "within 0.01, or there are no asset-class rows and the Total isn't zero. "
    f"{_CONTRADICTION_REMEDY}\n"
)
_ENDING_VALUE_ERROR = (
    "error: ParseError in section 'Change in NAV' at row 0, column 'Field Value': this "
    "Ending Value differs by more than 0.01 from the Net Asset Value figure (its Total "
    "row's Current Total or, with no Total row, the sum of its asset-class rows). "
    f"{_CONTRADICTION_REMEDY}\n"
)
# #1309: the fixture's NAV edited to contradict itself. Each case gives the
# error the run stops at and the figures it must not print: the cells it
# edits and the Total it leaves.
_NAV_CONTRADICTIONS = {
    # Example 1: Cash 50000 + Stock 2007.60 add up to 52007.60, while the
    # Total and the Ending Value stay 2107.60.
    "rows-disagree-with-total": (
        _edit((_NAV_CASH_ROW, _NAV_CASH_ROW.replace("100.00", "50000.00"))),
        _TOTAL_BREAKDOWN_ERROR,
        ("50000", "2107"),
    ),
    # Example 2: the rows and the Total agree at 2107.60; the Ending Value is 2207.60.
    "ending-value-disagrees-with-total": (
        _edit((_ENDING_VALUE_ROW, _ENDING_VALUE_ROW.replace("2107.60", "2207.60"))),
        _ENDING_VALUE_ERROR,
        ("2207", "2107"),
    ),
    # Example 2 without its Total row: the rows' sum is checked instead.
    "ending-value-disagrees-with-rows": (
        _edit(
            (_NAV_TOTAL_ROW, ""),
            (_ENDING_VALUE_ROW, _ENDING_VALUE_ROW.replace("2107.60", "2207.60")),
        ),
        _ENDING_VALUE_ERROR,
        ("2207", "2107"),
    ),
    # Examples 1 and 2 with digit-run markers in the cells they edit.
    "rows-disagree-with-total-markers": (
        _edit(
            (_NAV_CASH_ROW, _NAV_CASH_ROW.replace("100.00", "735180246.00")),
            (_NAV_TOTAL_ROW, _NAV_TOTAL_ROW.replace("2107.60", "735180146.00")),
            (_ENDING_VALUE_ROW, _ENDING_VALUE_ROW.replace("2107.60", "735180146.00")),
        ),
        _TOTAL_BREAKDOWN_ERROR,
        ("735180246", "735180146"),
    ),
    "ending-value-disagrees-with-total-markers": (
        _edit((_ENDING_VALUE_ROW, _ENDING_VALUE_ROW.replace("2107.60", "735180346.00"))),
        _ENDING_VALUE_ERROR,
        ("735180346", "2107"),
    ),
}


@pytest.mark.parametrize("case", sorted(_NAV_CONTRADICTIONS))
@pytest.mark.parametrize(
    ("format_id", "suffix", "extra_args"),
    [
        ("filing-csv", ".csv", ()),
        ("review-csv", ".csv", ()),
        ("xlsx", ".xlsx", ()),
        ("cbdt-json-fragment", ".json", ("--itr-form", "ITR-2")),
    ],
)
def test_schedule_fa_stops_at_nav_figures_that_disagree(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
    format_id: str,
    suffix: str,
    extra_args: tuple[str, ...],
) -> None:
    """#1309: these used to leave A2's closing balance blank (exit 3).
    CSV/XLSX wrote files; JSON refused the fragment. The run now stops at
    dispatch, writes nothing and removes an earlier run's outputs."""
    transform, error, figures = _NAV_CONTRADICTIONS[case]
    out = tmp_path / f"schedule-fa{suffix}"
    earlier_schedule_fa_outputs(out, format_id)
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(_statement(tmp_path, transform=transform)),
            "--config",
            str(_config(tmp_path, _FULL_CONFIG)),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--format",
            format_id,
            "--no-fetch-prices",
            *extra_args,
        ]
    )
    assert rc == 4
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", error)
    for figure in figures:
        assert figure not in captured.out + captured.err, figure
    assert sorted(path.name for path in tmp_path.iterdir()) == ["stmt.csv", "user.toml"]


def _filing_csv_run(
    run_dir: Path, capsys: pytest.CaptureFixture[str], config_text: str, transform
) -> tuple[int, str, str, dict[str, bytes]]:
    """Run ``schedule-fa --format filing-csv`` in ``run_dir``; return its exit,
    its streams with ``run_dir`` masked, and its output files."""
    run_dir.mkdir()
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(_statement(run_dir, transform=transform)),
            "--config",
            str(_config(run_dir, config_text)),
            "--output",
            str(run_dir / "schedule-fa.csv"),
            "--calendar-year",
            "2025",
            "--format",
            "filing-csv",
            "--no-fetch-prices",
        ]
    )
    captured = capsys.readouterr()
    files = {path.name: path.read_bytes() for path in sorted(run_dir.glob("schedule-fa*"))}
    return (
        rc,
        captured.out.replace(str(run_dir), "<run>"),
        captured.err.replace(str(run_dir), "<run>"),
        files,
    )


@pytest.mark.parametrize(
    "transform",
    [
        pytest.param(
            _edit((_NAV_STOCK_ROW, _NAV_STOCK_ROW.replace("2007.60", "2007.61"))),
            id="rows-0.01-over-the-total",
        ),
        pytest.param(
            _edit((_NAV_STOCK_ROW, _NAV_STOCK_ROW.replace("2007.60", "2007.59"))),
            id="rows-0.01-under-the-total",
        ),
        pytest.param(
            _edit((_ENDING_VALUE_ROW, _ENDING_VALUE_ROW.replace("2107.60", "2107.61"))),
            id="ending-value-0.01-over",
        ),
        pytest.param(
            _edit((_ENDING_VALUE_ROW, _ENDING_VALUE_ROW.replace("2107.60", "2107.59"))),
            id="ending-value-0.01-under",
        ),
    ],
)
def test_nav_figures_within_a_cent_file_the_printed_total(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], transform
) -> None:
    """#1287, #1309: figures 0.01 apart confirm each other, so the A2 file is
    the one the statement as printed gives, with the printed Total (188567)."""
    printed = _filing_csv_run(tmp_path / "as-printed", capsys, _CONFIG_NO_PEAK, None)
    edited = _filing_csv_run(tmp_path / "edited", capsys, _CONFIG_NO_PEAK, transform)
    assert printed[0] == edited[0] == 3
    a2 = edited[3]["schedule-fa-a2.csv"]
    assert a2 == printed[3]["schedule-fa-a2.csv"]
    [row] = list(csv.DictReader(a2.decode("utf-8-sig").splitlines()))
    assert row["Closing balance"] == "188567"


_CONFIG_NO_CUSTODIAN = _CONFIG_NO_PEAK.replace(CUSTODIAN_TOML, "")


@pytest.mark.parametrize("case", ["rows-disagree-with-total", "ending-value-disagrees-with-total"])
def test_schedule_fa_without_a_custodian_keeps_running_on_nav_figures_that_disagree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    """#1309: without ``[account.custodian]`` there's no Table A2, so the NAV
    isn't parsed and the run gives what the statement as printed gives."""
    assert "[account.custodian]" not in _CONFIG_NO_CUSTODIAN
    transform, _error, _figures = _NAV_CONTRADICTIONS[case]
    printed = _filing_csv_run(tmp_path / "as-printed", capsys, _CONFIG_NO_CUSTODIAN, None)
    edited = _filing_csv_run(tmp_path / "edited", capsys, _CONFIG_NO_CUSTODIAN, transform)
    assert printed[0] == 3
    assert printed[3]
    assert edited == printed


def test_refusal_reasons_name_ibkr_config_keys(tmp_path: Path) -> None:
    # No peak, no custodian ZIP, XQBB's [[fa.entities]] without zip/nature and
    # no XQBE entry: the exact remedies the IBKR user sees (#619 characterization).
    # Entity gaps name the holding by its first A3 row, never its ISIN (#1048):
    # XQBB's two lots are rows 1 and 2, XQBE's lot is row 3.
    config_text = _CONFIG_NO_PEAK
    for line in ('zip = "06830"\n', 'zip = "00001"\n', 'nature_of_entity = "Company"\n'):
        assert line in config_text
        config_text = config_text.replace(line, "", 1)
    config_text, xqbe_entry, _ = config_text.partition('[[fa.entities]]\nisin = "US99995D1000"')
    assert xqbe_entry
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        _run_json(tmp_path, config_text=config_text)
    zip_why = "(the CBDT JSON requires a non-blank postal code even though the CSV allows it blank)"
    zip_address = (
        "copy the row's own Address of entity if it shows one; an entry with zip needs one"
    )
    nature_address = (
        "copy the row's own Address of entity and ZIP Code if it shows them; an IBKR "
        "row's entry needs an address, and a Robinhood entry without one stops the run "
        "until you verify the address it finds"
    )
    assert exc.value.reasons == (
        "A2 PeakBalanceDuringPeriod: supply [[schedule_fa.manual_values]] with "
        "calendar_year = 2025 and a2_peak_balance_inr (select the highest "
        "native-currency securities + cash value first, then apply that date's SBI "
        "TTBR; it cannot be derived from the statement)",
        "A2 ZipCode: supply [account.custodian].zip (the institution's postal code; "
        "the CBDT JSON requires it even though the CSV allows it blank)",
        "A3 holding in row 1 ZipCode: supply [[fa.entities]] with isin = that row's ISIN, "
        f"an address ({zip_address}) and its zip {zip_why}",
        "A3 holding in row 1 NatureOfEntity: supply [[fa.entities]] with isin = that "
        f"row's ISIN, an address and zip ({nature_address}) and its verified "
        "nature_of_entity (1-34 characters)",
        # #1289: without an entry the address is blank too, not the country name.
        "A3 holding in row 3 AddressOfEntity: supply [[fa.entities]] with isin = that "
        "row's ISIN and its address",
        "A3 holding in row 3 ZipCode: supply [[fa.entities]] with isin = that row's ISIN, "
        f"an address ({zip_address}) and its zip {zip_why}",
        "A3 holding in row 3 NatureOfEntity: supply [[fa.entities]] with isin = that "
        f"row's ISIN, an address and zip ({nature_address}) and its verified "
        "nature_of_entity (1-34 characters)",
    )
    for isin in ("US99995B1044", "US99995D1000"):
        assert isin.lower() not in str(exc.value).lower()


def test_fa_entities_remedies_never_print_the_isin() -> None:
    """#1048: each [[fa.entities]] remedy gets the holding's ISIN but names
    "that row's ISIN" instead; the gap line already names the row."""
    remedies = cbdt_mod.FA_ENTITIES_REMEDIES
    isin = "US99971J1050"
    texts = (remedies.address(isin), remedies.zip_code(isin), remedies.nature(isin))
    assert texts == (
        "supply [[fa.entities]] with isin = that row's ISIN and its address",
        "supply [[fa.entities]] with isin = that row's ISIN, an address (copy the row's "
        "own Address of entity if it shows one; an entry with zip needs one) and its zip "
        "(the CBDT JSON requires a non-blank postal code even though the CSV allows it "
        "blank)",
        "supply [[fa.entities]] with isin = that row's ISIN, an address and zip (copy the "
        "row's own Address of entity and ZIP Code if it shows them; an IBKR row's entry "
        "needs an address, and a Robinhood entry without one stops the run until you "
        "verify the address it finds) and its verified nature_of_entity (1-34 characters)",
    )
    for text in texts:
        assert isin.lower() not in text.lower()


def test_nature_remedy_entry_loads(tmp_path: Path) -> None:
    """#1430: the entry the nature remedy asks for, ``isin``, the row's own
    address and ZIP copied in and ``nature_of_entity``, loads. On a Robinhood
    row whose address and ZIP came from GLEIF, the entry files its nature with
    no supersession warning. Without the address it loads too, and the
    Robinhood run stops for the filer to verify the GLEIF address (#1449)."""
    isin = Isin("US99995B1044")
    address = "1 Example Plaza, Anytown, DE"
    entry = f'[account]\nholder_name = "Tax Payer"\n\n[[fa.entities]]\nisin = "{isin}"\n'
    nature = 'nature_of_entity = "Company"\n'
    config_path = tmp_path / "user.toml"
    config_path.write_text(
        f'{entry}address = "{address}"\nzip = "99999"\n{nature}', encoding="utf-8"
    )
    configured = load_user_config(config_path).fa.entities
    automatic = (FaEntityOverride(isin=isin, address=address, zip_code="99999"),)
    assert robinhood_entity_override_supersession_warnings(automatic, configured) == ()
    assert merge_robinhood_entity_natures((), configured) == {isin: "Company"}

    config_path.write_text(entry + nature, encoding="utf-8")
    addressless = load_user_config(config_path).fa.entities
    held = (robinhood_security(cusip="99995B104", isin=isin),)
    (unverified,) = robinhood_unverified_entity_addresses(
        addressless, held, automatic, {isin: RobinhoodEntityRoute.LEGAL}, ()
    )
    assert unverified.entry == FaEntityOverride(isin=isin, nature_of_entity="Company")
    assert unverified.found == RobinhoodFoundEntityAddress(
        RobinhoodEntityRoute.LEGAL, address, "99999"
    )


def test_refuse_when_nature_of_entity_unsupplied(tmp_path: Path) -> None:
    """XQBB's nature is unsupplied: one gap, naming XQBB's first A3 row and the
    config key, not its ISIN (#1048)."""
    missing_one_nature = _FULL_CONFIG.replace('nature_of_entity = "Company"\n', "", 1)
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        _run_json(tmp_path, config_text=missing_one_nature)
    assert exc.value.reasons == (
        "A3 holding in row 1 NatureOfEntity: supply [[fa.entities]] with isin = that "
        "row's ISIN, an address and zip (copy the row's own Address of entity and ZIP "
        "Code if it shows them; an IBKR row's entry needs an address, and a Robinhood "
        "entry without one stops the run until you verify the address it finds) and its "
        "verified nature_of_entity (1-34 characters)",
    )
    assert "us99995b1044" not in str(exc.value).lower()
    assert not (tmp_path / "schedule-fa.json").exists()


def test_refuse_masked_account(tmp_path: Path) -> None:
    # A masked statement + --allow-masked-account skips the cross-check, so the
    # account binding is unverified → the JSON writer refuses.
    masked = lambda t: t.replace("Account,U12345678", "Account,U****")  # noqa: E731
    with pytest.raises(CbdtJsonStructuralError) as exc:
        _run_json(tmp_path, transform=masked, allow_masked_account=True)
    assert "cross-checked" in str(exc.value)
    assert not (tmp_path / "schedule-fa.json").exists()


def test_refuse_anonymised_statement_with_hidden_account(tmp_path: Path) -> None:
    # The KEY round-1 panel case: --accept-anonymised-statement backfills the
    # account number from config when the Account row is hidden, which makes the
    # raw cross-check pass — but the binding was never VERIFIED, so emission must
    # be refused.
    hide = lambda t: t.replace(  # noqa: E731
        "Account Information,Data,Account,U12345678,,,,,,,,,,,,,\n", ""
    )
    with pytest.raises(CbdtJsonStructuralError) as exc:
        _run_json(tmp_path, transform=hide, accept_anonymised_statement=True)
    assert "cross-checked" in str(exc.value)
    assert not (tmp_path / "schedule-fa.json").exists()


def test_refuse_json_run_without_itr_form_as_structural(tmp_path: Path) -> None:
    # The CLI refuses a missing --itr-form first (exit 2); a direct pipeline call
    # reaches this structural refusal instead.
    with pytest.raises(CbdtJsonStructuralError, match="--itr-form"):
        _run_json(tmp_path, itr_form=cast("Any", None))
    assert not (tmp_path / "schedule-fa.json").exists()


@pytest.mark.parametrize(
    ("transform", "flag"),
    [
        (
            lambda t: t.replace("Account,U12345678", "Account,U****"),
            "--allow-masked-account",
        ),
        (
            lambda t: t.replace("Account Information,Data,Account,U12345678,,,,,,,,,,,,,\n", ""),
            "--accept-anonymised-statement",
        ),
        (
            lambda t: t.replace("Account Information,Data,Name,Sample Holder,,,,,,,,,,,,,\n", ""),
            "--accept-anonymised-statement",
        ),
    ],
    ids=["masked-account", "hidden-account", "name-from-config"],
)
def test_cli_unverified_account_binding_exits_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], transform: Any, flag: str
) -> None:
    # #773: an unverified binding is a structural refusal (exit 4), not manual
    # fill (exit 3), and it writes no fragment.
    out = tmp_path / "schedule-fa.json"
    rc = _main_json(tmp_path, out, "--no-fetch-prices", flag, transform=transform)
    assert rc == 4
    assert "the account number was not cross-checked against its statement" in (
        capsys.readouterr().err
    )
    assert not out.exists()


def _main_json(
    tmp_path: Path, out: Path, *flags: str, config_text: str = _FULL_CONFIG, transform=None
) -> int:
    """Run ``schedule-fa --format cbdt-json-fragment`` on the NAV fixture."""
    return main(
        [
            "schedule-fa",
            "--input",
            str(_statement(tmp_path, transform=transform)),
            "--config",
            str(_config(tmp_path, config_text)),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--format",
            "cbdt-json-fragment",
            "--itr-form",
            "ITR-2",
            *flags,
        ]
    )


def test_cli_control_character_in_custodian_name_exits_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # #818: user.toml refuses a filed text value with a control character when
    # it loads (exit 4), before any output; #798's builder check stays for text
    # from other sources.
    config_text = _FULL_CONFIG.replace('"Interactive Brokers LLC"', '"Interactive Brokers\\nLLC"')
    out = tmp_path / "schedule-fa.json"
    rc = _main_json(tmp_path, out, "--no-fetch-prices", config_text=config_text)
    err = capsys.readouterr().err
    assert rc == 4
    assert (
        "ConfigError at field 'account.custodian.institution_name': has a control "
        "character at position 20; filed text may not contain control characters"
    ) in err
    assert "Interactive" not in err
    assert not out.exists()


def test_cli_control_character_refusal_withholds_the_account_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    account = "U1234\n5678"
    original = cbdt_mod.build_fa_a2

    def control_character_account(**kwargs):
        return tuple(replace(row, account_number=account) for row in original(**kwargs))

    monkeypatch.setattr(cbdt_mod, "build_fa_a2", control_character_account)
    out = tmp_path / "schedule-fa.json"
    rc = _main_json(tmp_path, out, "--no-fetch-prices")
    err = capsys.readouterr().err
    assert rc == 4
    # FaA2Row refuses it before the writer's own check can (#854).
    assert (
        "InvariantError: FaA2Row.account_number must not contain a control character "
        "(context keys: character, position)"
    ) in err
    for shown in (account, repr(account), "U1234"):
        assert shown not in err
    assert not out.exists()


# ---- CLI: argparse surface ------------------------------------------------


def _fake_emit_report(out: Path, itr_form: CbdtItrForm = "ITR-2") -> RunReport:
    return RunReport(
        rows_written=2,
        output_path=out,
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        emitted_cbdt_json=True,
        cbdt_json_a2_peak_balance_inr=250000,
        cbdt_json_itr_form=itr_form,
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )


def _cli_args(tmp_path: Path, out: Path, itr_form: CbdtItrForm | None = "ITR-2") -> list[str]:
    inp = tmp_path / "in.csv"
    inp.write_text("x", encoding="utf-8")
    cfg = tmp_path / "user.toml"
    cfg.write_text(_FULL_CONFIG, encoding="utf-8")
    args = [
        "schedule-fa",
        "--input",
        str(inp),
        "--config",
        str(cfg),
        "--output",
        str(out),
        "--calendar-year",
        "2025",
        "--format",
        "cbdt-json-fragment",
    ]
    if itr_form is not None:
        args.extend(["--itr-form", itr_form])
    return args


def test_cli_accepts_format_and_exits_0_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "schedule-fa.json"
    seen: dict[str, object] = {}

    def _fake(**kwargs: object) -> RunReport:
        seen.update(kwargs)
        return _fake_emit_report(out, "ITR-3")

    monkeypatch.setattr("ibkr_fa.cli.run_fa_a3", _fake)
    rc = main(_cli_args(tmp_path, out, "ITR-3"))
    assert rc == 0
    assert seen["itr_form"] == "ITR-3"
    summary = capsys.readouterr().out
    assert "CBDT JSON fragment" in summary
    assert "Target form: ITR-3" in summary
    assert "PeakBalanceDuringPeriod=250000" in summary


def test_cli_requires_itr_form_before_preclear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "schedule-fa.json"
    out.write_text(
        json.dumps({"_ibkrFa": {"format": "cbdt-json-fragment"}}),
        encoding="utf-8",
    )

    def _must_not_run(**kwargs: object) -> RunReport:
        raise AssertionError(kwargs)

    monkeypatch.setattr("ibkr_fa.cli.run_fa_a3", _must_not_run)
    assert main(_cli_args(tmp_path, out, None)) == 2
    assert "--itr-form" in capsys.readouterr().err
    assert out.exists()


def test_cli_exit_3_on_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(**kw: object) -> RunReport:
        raise CbdtJsonIncompleteError(["A2 PeakBalanceDuringPeriod: supply it"])

    monkeypatch.setattr("ibkr_fa.cli.run_fa_a3", _raise)
    out = tmp_path / "schedule-fa.json"
    rc = main(_cli_args(tmp_path, out))
    assert rc == 3
    assert "A2 PeakBalanceDuringPeriod" in capsys.readouterr().err


def test_run_all_rejects_cbdt_json_format(tmp_path: Path) -> None:
    inp = tmp_path / "in.csv"
    inp.write_text("x", encoding="utf-8")
    cfg = tmp_path / "user.toml"
    cfg.write_text(_FULL_CONFIG, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "run-all",
                "--input",
                str(inp),
                "--config",
                str(cfg),
                "--output-dir",
                str(tmp_path / "out"),
                "--financial-year",
                "2025-26",
                "--format",
                "cbdt-json-fragment",
            ]
        )
    assert exc.value.code == 2  # argparse usage error


# ---- CLI: stale / foreign file pre-clear ----------------------------------


def test_cli_preclears_own_prior_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A prior ibkr-fa fragment at --output is removed before the run; on a refusal the
    # run leaves nothing importable (here the run "succeeds" via the fake).
    out = tmp_path / "schedule-fa.json"
    out.write_text(
        json.dumps({"_ibkrFa": {"format": "cbdt-json-fragment"}, "ScheduleFA": {}}),
        encoding="utf-8",
    )
    seen: dict[str, bool] = {}

    def _fake(**kw: object) -> RunReport:
        seen["existed_during_run"] = out.exists()
        return _fake_emit_report(out)

    monkeypatch.setattr("ibkr_fa.cli.run_fa_a3", _fake)
    rc = main(_cli_args(tmp_path, out))
    assert rc == 0
    assert seen["existed_during_run"] is False  # pre-cleared before the run


def test_cli_refuses_to_overwrite_foreign_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A file at --output WITHOUT our marker (e.g. a hand-merged ScheduleFA) is never
    # touched; the run is refused with exit 4 before run_fa_a3 is even called.
    out = tmp_path / "schedule-fa.json"
    foreign = json.dumps({"ScheduleFA": {"DetailsForiegnBank": [{"x": 1}]}})
    out.write_text(foreign, encoding="utf-8")

    def _must_not_run(**kw: object) -> RunReport:
        raise AssertionError("run_fa_a3 must not be reached when the --output target is foreign")

    monkeypatch.setattr("ibkr_fa.cli.run_fa_a3", _must_not_run)
    rc = main(_cli_args(tmp_path, out))
    assert rc == 4
    assert "not generated by ibkr-fa" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == foreign  # untouched


@pytest.mark.parametrize("link_target", [None, "schedule-fa.json", "missing.json"])
def test_cli_refuses_a_file_it_cannot_inspect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    link_target: str | None,
) -> None:
    # A file at --output that cannot be parsed is neither ours nor proven foreign: refuse
    # with exit 4 before run_fa_a3, and leave its bytes alone.
    out = tmp_path / "schedule-fa.json"
    if link_target is None:
        out.write_bytes(b"\xff not json")
    else:
        out.symlink_to(link_target)

    def _must_not_run(**kw: object) -> RunReport:
        raise AssertionError(
            "run_fa_a3 must not be reached when the --output target cannot be inspected"
        )

    monkeypatch.setattr("ibkr_fa.cli.run_fa_a3", _must_not_run)
    rc = main(_cli_args(tmp_path, out))
    assert rc == 4
    action = "check output target" if link_target == out.name else "inspect"
    assert f"could not {action} {out}" in capsys.readouterr().err
    if link_target is None:
        assert out.read_bytes() == b"\xff not json"
    else:
        assert out.readlink() == Path(link_target)


@pytest.mark.parametrize("link_target", ["schedule-fa.json", "missing.json"])
def test_cli_json_preclear_inspects_symlinks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], link_target: str
) -> None:
    path = tmp_path / "schedule-fa.json"
    path.symlink_to(link_target)
    assert _preclear_cbdt_json_target(path) == 4
    assert f"could not inspect {path}" in capsys.readouterr().err
    assert path.readlink() == Path(link_target)


def test_cli_json_preclear_names_an_own_fragment_it_cannot_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shared preclear (#644) keeps the CLI's exit 4 and names the path and
    errno name, never the OS text (#1049)."""
    path = tmp_path / "schedule-fa.json"
    path.write_text(json.dumps({"_ibkrFa": {"format": "cbdt-json-fragment"}}), encoding="utf-8")
    unlink = Path.unlink

    def cannot_remove(target: Path, missing_ok: bool = False) -> None:
        if target == path:
            raise PermissionError(13, "Permission denied", str(target))
        unlink(target, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", cannot_remove)
    assert _preclear_cbdt_json_target(path) == 4
    assert capsys.readouterr().err == (
        f"error: could not remove the previous output {path} (EACCES)\n"
    )
    assert path.exists()
