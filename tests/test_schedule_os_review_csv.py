"""Tests for :mod:`ibkr_fa.output.schedule_os_review_csv`."""

from __future__ import annotations

import csv
import errno
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from ibkr_fa.compute.schedule_os import (
    ScheduleOsComputeResult,
    ScheduleOsDividendLine,
    ScheduleOsDomesticDividendLine,
)
from ibkr_fa.compute.schedule_os_other_income import ScheduleOsOtherIncomeLine
from ibkr_fa.models import DistributionKind
from ibkr_fa.output import (
    Artifact,
    Destination,
    OutputBinding,
    OutputKey,
    Subcommand,
    output_binding,
)
from ibkr_fa.output._cleanup import OutputRemovalError
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.portal_labels import (
    PORTAL_FIELD_LABEL_OS_OTHER_INTEREST,
)
from ibkr_fa.output.schedule_os_review_csv import ScheduleOsReviewCsvWriter
from ibkr_fa.types import Isin, Ticker
from tests._crash_reports import assert_replaced_unchained
from tests._schedule_os_totals import total_rows


def make_line(
    *,
    symbol: str = "XQCA",
    isin: str = "US99972B1017",
    issuer_country_iso2: str = "US",
    payment_date: date = date(2025, 6, 12),
    fx_specified_date: date = date(2025, 5, 31),
    fx_rate_publication_date: date | None = None,
    fx_rate: Decimal = Decimal("83.50"),
    amount_gross_usd: Decimal = Decimal("120.4567"),
    amount_gross_inr: Decimal = Decimal("10058.134450"),  # 120.4567 * 83.50
    description: str | None = None,
    kind: DistributionKind = DistributionKind.CASH_DIVIDEND,
) -> ScheduleOsDividendLine:
    if description is None:
        description = f"{symbol}({isin}) Cash Dividend USD 0.8300 per Share (Ordinary Dividend)"
    if fx_rate_publication_date is None:
        fx_rate_publication_date = fx_specified_date
    return ScheduleOsDividendLine(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        issuer_country_iso2=issuer_country_iso2,
        payment_date=payment_date,
        fx_specified_date=fx_specified_date,
        fx_rate_publication_date=fx_rate_publication_date,
        fx_rate_inr_per_usd=fx_rate,
        amount_gross_usd=amount_gross_usd,
        amount_gross_inr=amount_gross_inr,
        description=description,
        kind=kind,
    )


def make_domestic_line(
    *,
    symbol: str = "XQCD",
    isin: str = "IN99970W1077",
    payment_date: date = date(2025, 7, 1),
    fx_specified_date: date = date(2025, 6, 30),
    fx_rate_publication_date: date | None = None,
    fx_rate: Decimal = Decimal("83.50"),
    amount_gross_usd: Decimal = Decimal("12.34"),
    amount_gross_inr: Decimal = Decimal("1030.39"),
    description: str | None = None,
) -> ScheduleOsDomesticDividendLine:
    if description is None:
        description = f"{symbol}({isin}) Cash Dividend USD 0.3500 per Share (Ordinary Dividend)"
    if fx_rate_publication_date is None:
        fx_rate_publication_date = fx_specified_date
    return ScheduleOsDomesticDividendLine(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        issuer_country_iso2="IN",
        payment_date=payment_date,
        fx_specified_date=fx_specified_date,
        fx_rate_publication_date=fx_rate_publication_date,
        fx_rate_inr_per_usd=fx_rate,
        amount_gross_usd=amount_gross_usd,
        amount_gross_inr=amount_gross_inr,
        description=description,
        kind=DistributionKind.CASH_DIVIDEND,
    )


def make_result(
    *,
    lines: list[ScheduleOsDividendLine] | None = None,
    domestic_lines: list[ScheduleOsDomesticDividendLine] | None = None,
    other_income_lines: tuple[ScheduleOsOtherIncomeLine, ...] = (),
    interest_total_inr_rounded: Decimal = Decimal("0"),
) -> ScheduleOsComputeResult:
    return ScheduleOsComputeResult(
        lines=[] if lines is None else lines,
        domestic_lines=[] if domestic_lines is None else domestic_lines,
        interest_total_inr_rounded=interest_total_inr_rounded,
        other_income_lines=other_income_lines,
    )


_LENDING_FEE = ScheduleOsOtherIncomeLine(
    nature="Securities lending fee",
    payment_date=date(2025, 6, 1),
    fx_specified_date=date(2026, 3, 31),
    fx_rate_publication_date=date(2026, 3, 31),
    fx_rate_inr_per_usd=Decimal("80"),
    amount_gross_usd=Decimal("0.11"),
    amount_gross_inr=Decimal("8.80"),
    description="Stock Lending",
    source_row_index=1,
)


def test_review_csv_emits_other_income_audit_rows(tmp_path: Path) -> None:
    result = make_result(other_income_lines=(_LENDING_FEE,))
    output = tmp_path / "schedule-os.csv"

    ScheduleOsReviewCsvWriter().write(result, output)

    with output.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))
    assert any(row[0] == "_other_income:" for row in rows)
    assert any(row[0] == "Securities lending fee" and row[5] == "0.11" for row in rows)


def read_csv_rows(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


def write_and_read(
    tmp_path: Path,
    result: ScheduleOsComputeResult,
    *,
    filename: str = "schedule-os.csv",
) -> list[list[str]]:
    out = tmp_path / filename
    ScheduleOsReviewCsvWriter().write(result, out)
    return read_csv_rows(out)


def blank_row() -> list[str]:
    return [""] * 11


def temp_glob(path: Path) -> list[Path]:
    return list(path.parent.glob(f".{path.name}.*"))


_ZERO_TOTAL_ROW_COUNT = 12


def test_empty_result_emits_ordinary_portal_total_at_zero(tmp_path: Path) -> None:
    rows = write_and_read(tmp_path, make_result())

    assert len(rows) == _ZERO_TOTAL_ROW_COUNT
    assert all(len(row) == 11 for row in rows)
    _, portal, _ = total_rows(rows)
    assert rows[portal][-1] == "0"


def test_single_foreign_dividend_line_written(tmp_path: Path) -> None:
    line = make_line()
    rows = write_and_read(tmp_path, make_result(lines=[line]))

    foreign_detail_idx = next(i for i, r in enumerate(rows) if len(r) > 0 and r[0] == "XQCA")
    assert rows[foreign_detail_idx] == [
        "XQCA",
        "US99972B1017",
        "US",
        "12/06/2025",
        "Cash Dividend",
        "120.4567",
        "2025-05-31",
        "",
        "83.50",
        "10058.134450",
        "10058",
    ]


def test_audit_cells_in_correct_columns(tmp_path: Path) -> None:
    rows = write_and_read(tmp_path, make_result(lines=[make_line()]))
    header = rows[0]
    detail = next(r for r in rows if len(r) > 0 and r[0] == "XQCA")

    assert header[5:10] == [
        "_audit: Gross USD",
        "_audit: FX specified date (YYYY-MM-DD)",
        "_audit: SBI TTBR source date (YYYY-MM-DD)",
        "_audit: SBI TTBR (INR/USD)",
        "_audit: Gross INR (full precision)",
    ]
    assert detail[5:10] == ["120.4567", "2025-05-31", "", "83.50", "10058.134450"]


def test_ordinary_full_precision_sum_then_round(tmp_path: Path) -> None:
    lines = [
        make_line(symbol="AAA", isin="US99971J1050", amount_gross_inr=Decimal("0.4")),
        make_line(symbol="BBB", isin="US99971J1051", amount_gross_inr=Decimal("0.4")),
        make_line(symbol="CCC", isin="US99971J1052", amount_gross_inr=Decimal("0.4")),
    ]
    rows = write_and_read(tmp_path, make_result(lines=lines))

    # Each detail row's rounded ₹ cell shows 0 (HALF_UP of 0.4 is 0).
    detail_rows = [r for r in rows if r and r[0] in {"AAA", "BBB", "CCC"}]
    assert [row[10] for row in detail_rows] == ["0", "0", "0"]
    audit, portal, _ = total_rows(rows)
    assert rows[audit][9] == "1.2"
    assert rows[portal][-1] == "1"


def test_dividend_periods_match_quicko_boundaries_and_reconcile_rounding(
    tmp_path: Path,
) -> None:
    dated_amounts = [
        (date(2025, 4, 1), "1.1"),
        (date(2025, 6, 15), "2.1"),
        (date(2025, 6, 16), "4.1"),
        (date(2025, 9, 15), "8.1"),
        (date(2025, 9, 16), "16.1"),
        (date(2025, 12, 15), "32.1"),
        (date(2025, 12, 16), "64.1"),
        (date(2026, 3, 15), "128.1"),
        (date(2026, 3, 16), "256.1"),
        (date(2026, 3, 31), "512.1"),
    ]
    rows = write_and_read(
        tmp_path,
        make_result(
            lines=[
                make_line(
                    payment_date=payment_date,
                    fx_specified_date=date(2025, 3, 31),
                    amount_gross_inr=Decimal(amount),
                )
                for payment_date, amount in dated_amounts
            ]
        ),
    )

    header_index = next(
        i
        for i, row in enumerate(rows)
        if row[0]
        == "Parsed-source contribution to Schedule OS section 10 / Quicko dividend periods"
    )
    assert rows[header_index][1:3] == [
        "Quicko Income to add / ITR section 10 row 3a",
        "Quicko Expense to add / ITR section 57(i) aggregate (not computed)",
    ]
    period_rows = rows[header_index + 1 : header_index + 6]
    assert [row[0] for row in period_rows] == [
        "Quicko Period 1 / ITR: Up to 15/06",
        "Quicko Period 2 / ITR: 16/06 to 15/09",
        "Quicko Period 3 / ITR: 16/09 to 15/12",
        "Quicko Period 4 / ITR: 16/12 to 15/03",
        "Quicko Period 5 / ITR: 16/03 to 31/03",
    ]
    assert [row[9] for row in period_rows] == ["3.2", "12.2", "48.2", "192.2", "768.2"]
    assert [row[1] for row in period_rows] == ["4", "12", "48", "192", "768"]
    assert [row[2] for row in period_rows] == [""] * 5
    _, portal, _ = total_rows(rows)
    assert sum(int(row[1]) for row in period_rows) == int(rows[portal][-1]) == 1024


def test_total_row_first_cell_matches_portal_label_constant(tmp_path: Path) -> None:
    rows = write_and_read(tmp_path, make_result(lines=[make_line()]))

    _, portal, _ = total_rows(rows)
    assert rows[portal][-1] == "10058"


def test_csv_quoting(tmp_path: Path) -> None:
    out = tmp_path / "quote.csv"
    description = "Stock Lending, special event"
    line = replace(_LENDING_FEE, description=description)

    ScheduleOsReviewCsvWriter().write(make_result(other_income_lines=(line,)), out)

    text = out.read_text(encoding="utf-8-sig")
    assert f'"{description}"' in text
    rows = read_csv_rows(out)
    assert [row[4] for row in rows if row[0] == line.nature] == [description]


def test_overwrite_existing_file(tmp_path: Path) -> None:
    out = tmp_path / "out.csv"
    out.write_text("hello\n", encoding="utf-8")

    ScheduleOsReviewCsvWriter().write(make_result(), out)

    rows = read_csv_rows(out)
    assert "hello" not in out.read_text(encoding="utf-8-sig")
    assert len(rows) == _ZERO_TOTAL_ROW_COUNT
    _, portal, _ = total_rows(rows)
    assert rows[portal][-1] == "0"


def test_parent_dir_must_exist_loud_fail(tmp_path: Path) -> None:
    out = tmp_path / "does-not-exist" / "review.csv"

    with pytest.raises(FileNotFoundError):
        ScheduleOsReviewCsvWriter().write(make_result(), out)

    assert not list(tmp_path.glob(".review.csv.*"))


def test_format_id_register_os_decorator() -> None:
    assert ScheduleOsReviewCsvWriter.format_id == "review-csv"
    assert output_binding(Subcommand.OS, "review-csv") == OutputBinding(
        OutputKey(Destination.REVIEW, Artifact.OS_CSV), ScheduleOsReviewCsvWriter
    )


def test_blank_delimiter_row_always_present_at_zero(tmp_path: Path) -> None:
    """One blank delimiter separates details/audit rows from totals."""
    rows = write_and_read(tmp_path, make_result())
    blank_count = sum(1 for r in rows if r == blank_row())
    assert blank_count >= 1


def test_blank_delimiter_row_has_exactly_11_empty_fields(tmp_path: Path) -> None:
    rows = write_and_read(tmp_path, make_result())
    blank_rows = [r for r in rows if r == [""] * 11]

    assert blank_rows  # at least one blank row
    assert blank_rows[0] == ["", "", "", "", "", "", "", "", "", "", ""]


def test_uniform_11_column_width_across_every_row(tmp_path: Path) -> None:
    result = make_result(
        lines=[make_line()],
        domestic_lines=[make_domestic_line()],
    )
    rows = write_and_read(tmp_path, result)

    assert all(len(row) == 11 for row in rows)


def test_ordinary_portal_total_combines_both_sources(tmp_path: Path) -> None:
    result = make_result(
        lines=[make_line()],
        domestic_lines=[make_domestic_line()],
    )
    rows = write_and_read(tmp_path, result)

    _, portal, _ = total_rows(rows)
    # 10058.134450 + 1030.39 = 11088.524450, rounded once HALF_UP.
    assert rows[portal][-1] == "11089"


def test_zero_total_csv_shape(tmp_path: Path) -> None:
    rows = write_and_read(tmp_path, make_result())

    assert len(rows) == _ZERO_TOTAL_ROW_COUNT
    assert [len(row) for row in rows] == [11] * _ZERO_TOTAL_ROW_COUNT
    _, portal, interest = total_rows(rows)
    assert rows[portal][-1] == "0"
    assert rows[interest] == [PORTAL_FIELD_LABEL_OS_OTHER_INTEREST, *[""] * 9, "0"]


def test_interest_total_row_follows_the_ordinary_portal_total(tmp_path: Path) -> None:
    rows = write_and_read(tmp_path, make_result(interest_total_inr_rounded=Decimal("4321")))

    _, _, interest = total_rows(rows)
    assert interest == len(rows) - 1
    assert rows[interest] == [
        "Schedule OS / 1b(ix) Others including interest from Companies, NBFCs & HFCs",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "4321",
    ]


def test_totals_come_before_the_other_income_block(tmp_path: Path) -> None:
    result = make_result(
        lines=[make_line()],
        other_income_lines=(_LENDING_FEE,),
        interest_total_inr_rounded=Decimal("4321"),
    )
    rows = write_and_read(tmp_path, result)

    audit, portal, interest = total_rows(rows)
    assert [rows[audit][9], rows[portal][10], rows[interest][10]] == [
        "10058.134450",
        "10058",
        "4321",
    ]
    assert rows[interest + 2][0] == "_other_income:"
    assert rows[interest + 3][0] == "Securities lending fee"


# ---- source attribution inside the one 1a(i) block ------------------------


def test_ordinary_total_emits_at_zero(tmp_path: Path) -> None:
    rows = write_and_read(tmp_path, make_result())
    _, portal, _ = total_rows(rows)
    assert rows[portal][-1] == "0"


def test_domestic_block_detail_row_written(tmp_path: Path) -> None:
    line = make_domestic_line()
    rows = write_and_read(tmp_path, make_result(domestic_lines=[line]))

    detail_idx = next(i for i, r in enumerate(rows) if len(r) > 0 and r[0] == "XQCD")
    assert rows[detail_idx] == [
        "XQCD",
        "IN99970W1077",
        "IN",
        "01/07/2025",
        "Cash Dividend",
        "12.34",
        "2025-06-30",
        "",
        "83.50",
        "1030.39",
        "1030",
    ]


def test_one_combined_audit_total_label(tmp_path: Path) -> None:
    rows = write_and_read(tmp_path, make_result(domestic_lines=[make_domestic_line()]))

    total_rows(rows)
    audit_labels = [r[0] for r in rows if r[0].startswith("_audit:")]
    assert audit_labels == ["_audit: 1a(i) TOTAL Gross INR (full precision)"]


def test_detail_rows_are_globally_sorted_not_grouped_by_country(tmp_path: Path) -> None:
    result = make_result(
        lines=[make_line()],
        domestic_lines=[
            make_domestic_line(payment_date=date(2025, 6, 1), fx_specified_date=date(2025, 5, 31))
        ],
    )
    rows = write_and_read(tmp_path, result)
    domestic_idx = next(i for i, r in enumerate(rows) if r and r[0] == "XQCD")
    foreign_idx = next(i for i, r in enumerate(rows) if r and r[0] == "XQCA")
    assert domestic_idx < foreign_idx


def test_combined_total_sums_full_precision_before_rounding(tmp_path: Path) -> None:
    domestic_line = make_domestic_line(amount_gross_inr=Decimal("0.6"))
    foreign_line = make_line(amount_gross_inr=Decimal("0.6"))
    rows = write_and_read(
        tmp_path,
        make_result(lines=[foreign_line], domestic_lines=[domestic_line]),
    )

    _, portal, _ = total_rows(rows)
    assert rows[portal][-1] == "1"  # round(0.6 + 0.6), not round(0.6) + round(0.6)


# ---- Asymmetric-bucket portal-TOTAL emission (T1 correctness P3) -----------


def test_only_foreign_lines_feed_ordinary_total(tmp_path: Path) -> None:
    rows = write_and_read(tmp_path, make_result(lines=[make_line()]))

    _, portal, _ = total_rows(rows)
    assert rows[portal][-1] == "10058"


def test_only_domestic_lines_feed_ordinary_total(tmp_path: Path) -> None:
    rows = write_and_read(tmp_path, make_result(domestic_lines=[make_domestic_line()]))

    _, portal, _ = total_rows(rows)
    assert rows[portal][-1] == "1030"


def test_utf8_bom_first_three_bytes(tmp_path: Path) -> None:
    out = tmp_path / "bom.csv"

    ScheduleOsReviewCsvWriter().write(make_result(), out)

    assert out.read_bytes()[:3] == b"\xef\xbb\xbf"


def test_atomic_write_happy_path_no_temp_file_left_behind(tmp_path: Path) -> None:
    out = tmp_path / "atomic.csv"

    ScheduleOsReviewCsvWriter().write(make_result(lines=[make_line()]), out)

    assert out.exists()
    assert temp_glob(out) == []


def test_atomic_write_contract_violation_leaves_no_temp_file(tmp_path: Path) -> None:
    out = tmp_path / "contract.csv"
    out.write_text("sentinel\n", encoding="utf-8")
    line = make_line(symbol="XQCA\x00")

    with pytest.raises(WriterContractError):
        ScheduleOsReviewCsvWriter().write(make_result(lines=[line]), out)

    assert out.read_text(encoding="utf-8") == "sentinel\n"
    assert temp_glob(out) == []


def test_atomic_write_keyboard_interrupt_leaves_no_temp_file(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "interrupt.csv"
    out.write_text("sentinel\n", encoding="utf-8")
    real_writer = csv.writer

    class InterruptingWriter:
        def __init__(self, *args, **kwargs) -> None:
            self._writer = real_writer(*args, **kwargs)
            self._calls = 0

        def writerow(self, row) -> object:
            self._calls += 1
            if self._calls == 2:
                raise KeyboardInterrupt
            return self._writer.writerow(row)

    def writer_factory(*args, **kwargs) -> InterruptingWriter:
        return InterruptingWriter(*args, **kwargs)

    monkeypatch.setattr(csv, "writer", writer_factory)

    with pytest.raises(KeyboardInterrupt):
        ScheduleOsReviewCsvWriter().write(make_result(lines=[make_line()]), out)

    assert out.read_text(encoding="utf-8") == "sentinel\n"
    assert temp_glob(out) == []


def test_atomic_write_concurrent_writers_produce_one_artefact(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "concurrent.csv"
    result = make_result(lines=[make_line()])
    barrier = threading.Barrier(2)
    real_mkstemp = tempfile.mkstemp

    def barrier_mkstemp(*args, **kwargs) -> tuple[int, str]:
        barrier.wait(timeout=10)
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", barrier_mkstemp)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(ScheduleOsReviewCsvWriter().write, result, out),
            executor.submit(ScheduleOsReviewCsvWriter().write, result, out),
        ]
        for future in futures:
            future.result(timeout=10)

    assert out.exists()
    rows = read_csv_rows(out)
    # Locate by content rather than a fixed row index.
    assert any(r and r[0] == "XQCA" for r in rows)
    assert temp_glob(out) == []


def test_mkstemp_raises_propagates_unchanged(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "mkstemp.csv"

    def raise_permission(*args, **kwargs) -> tuple[int, str]:
        raise PermissionError("Permission denied: '.'")

    monkeypatch.setattr(tempfile, "mkstemp", raise_permission)

    with pytest.raises(PermissionError, match="Permission denied"):
        ScheduleOsReviewCsvWriter().write(make_result(), out)

    assert not out.exists()
    assert temp_glob(out) == []


def test_unlink_raises_during_cleanup_refuses_naming_the_staging_file(tmp_path: Path) -> None:
    """The staging file is named by its pattern beside the output, not by its own
    random name, and the unlink failure by its errno name only: never its text
    or file name, markers here (#1048)."""
    out = tmp_path / "unlink.csv"
    line = make_line(symbol="XQCA\x00")

    with (
        patch(
            "pathlib.Path.unlink",
            autospec=False,
            side_effect=PermissionError(
                errno.EACCES, "Synthetic Strerror Marker", "Synthetic Holder Two/x"
            ),
        ) as m_unlink,
        pytest.raises(OutputRemovalError) as raised,
    ):
        ScheduleOsReviewCsvWriter().write(make_result(lines=[line]), out)

    # Keeps none of the writer's NUL refusal it replaced, nor the unlink failure (#1211).
    assert_replaced_unchained(
        raised.value,
        markers=("contains a NUL byte", "Synthetic Strerror Marker", "Synthetic Holder Two"),
    )
    m_unlink.assert_called_once_with(missing_ok=True)
    (staged,) = temp_glob(out)
    message = str(raised.value)
    assert message == (
        "the write failed, and its output may still be there: could not remove the staging "
        f"file {tmp_path}/.unlink.csv.*.tmp (EACCES)"
    )
    assert staged.name not in message
    for marker in ("Synthetic Strerror Marker", "Synthetic Holder Two"):
        assert marker.lower() not in message.lower()


def test_os_fdopen_raises_closes_raw_fd(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "fdopen.csv"
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.exists():
        pytest.skip("/proc/self/fd is Linux-specific")
    before_fd_count = len(os.listdir(fd_dir))  # noqa: PTH208 - /proc fd-count contract

    def raise_fdopen(*args, **kwargs) -> object:
        raise OSError("EMFILE")

    monkeypatch.setattr(os, "fdopen", raise_fdopen)

    with pytest.raises(OSError, match="EMFILE"):
        ScheduleOsReviewCsvWriter().write(make_result(), out)

    assert len(os.listdir(fd_dir)) <= before_fd_count  # noqa: PTH208 - /proc fd-count contract
    assert temp_glob(out) == []


def _foreign_detail_row(rows: list[list[str]], symbol: str = "XQCA") -> list[str]:
    """Locate a foreign-source detail row by symbol."""
    return next(r for r in rows if r and r[0] == symbol)


def test_payment_date_formatted_ddmmyyyy(tmp_path: Path) -> None:
    line = make_line(
        payment_date=date(2025, 1, 5),
        fx_specified_date=date(2024, 12, 31),
    )
    rows = write_and_read(tmp_path, make_result(lines=[line]))
    detail = _foreign_detail_row(rows)

    assert detail[3] == "05/01/2025"
    assert detail[6] == "2024-12-31"


def test_register_os_lookup_returns_writer_class() -> None:
    assert output_binding(Subcommand.OS, "review-csv").target is ScheduleOsReviewCsvWriter


def test_distribution_type_renders_line_kind(tmp_path: Path) -> None:
    """The ``Distribution type`` column renders ``line.kind`` and never
    parses the description (#606): a description with no
    ``SYMBOL(ISIN)`` prefix still shows the kind's label."""
    line = make_line(description="Special payout", kind=DistributionKind.SPECIAL_DIVIDEND)
    rows = write_and_read(tmp_path, make_result(lines=[line]))
    detail = _foreign_detail_row(rows)

    assert detail[4] == "Special Dividend"


# ---- #160: fx_rate_publication_date column rendering ---------------------


def test_publication_date_cell_blank_when_no_carry_back(tmp_path: Path) -> None:
    """When fx_rate_publication_date == fx_specified_date (no carry-
    back), the publication-date cell renders blank — populated cells
    signal that carry-back occurred, which is the visual cue an
    auditor scans for.
    """
    line = make_line(
        fx_specified_date=date(2025, 5, 31),
        fx_rate_publication_date=date(2025, 5, 31),
    )
    rows = write_and_read(tmp_path, make_result(lines=[line]))
    detail = _foreign_detail_row(rows)
    # col 6: Rule 115 specified date (always present)
    # col 7: Rule 115 TTBR source date (blank when no carry-back)
    assert detail[6] == "2025-05-31"
    assert detail[7] == ""


def test_publication_date_cell_full_date_when_carry_back(tmp_path: Path) -> None:
    """The Sep 15 / Aug 31 issue #160 example: when the policy
    carried back from Aug 31 (Sun) to Aug 30 (Sat), the
    publication-date cell shows the Aug 30 archive date so the
    auditor can verify the rate against the SBI row directly.
    """
    line = make_line(
        payment_date=date(2025, 9, 15),
        fx_specified_date=date(2025, 8, 31),
        fx_rate_publication_date=date(2025, 8, 30),
    )
    rows = write_and_read(tmp_path, make_result(lines=[line]))
    detail = _foreign_detail_row(rows)
    # col 6: Rule 115 specified date; col 7: TTBR source date (populated
    # because Aug 31 was a Sunday absent from the SBI archive).
    assert detail[6] == "2025-08-31"
    assert detail[7] == "2025-08-30"


# ---- per-AY portal labels threading (issue #65) ---------------------------


def test_writer_with_registered_ay_uses_snapshot_labels(tmp_path: Path) -> None:
    """Threading an explicit registered AY sources portal labels from
    that AY's snapshot (issue #65)."""
    from ibkr_fa.output.portal_labels import for_ay  # noqa: PLC0415

    snap = for_ay(2026)
    out = tmp_path / "ay2026.csv"
    ScheduleOsReviewCsvWriter(assessment_year_start=2026).write(make_result(), out)
    rows = read_csv_rows(out)
    _, portal, interest = total_rows(rows)
    assert rows[portal][0] == snap.PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND
    assert rows[interest][0] == snap.PORTAL_FIELD_LABEL_OS_OTHER_INTEREST


def test_writer_with_forward_compat_ay_falls_back_silently(tmp_path: Path) -> None:
    """+1 forward-compat AY silently falls back to the latest
    snapshot — writer does NOT raise (CLI surfaces the advisory)."""
    from ibkr_fa.output.portal_labels import (  # noqa: PLC0415
        _SNAPSHOTS_BY_AY_START,
        for_ay,
    )

    latest = max(_SNAPSHOTS_BY_AY_START)
    snap = for_ay(latest)
    out = tmp_path / "forward.csv"
    ScheduleOsReviewCsvWriter(assessment_year_start=latest + 1).write(make_result(), out)
    rows = read_csv_rows(out)
    _, portal, interest = total_rows(rows)
    assert rows[portal][0] == snap.PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND
    assert rows[interest][0] == snap.PORTAL_FIELD_LABEL_OS_OTHER_INTEREST


def test_writer_with_unknown_ay_raises_at_construction() -> None:
    """AY two beyond latest registered raises at construction."""
    from ibkr_fa.output.portal_labels import (  # noqa: PLC0415
        _SNAPSHOTS_BY_AY_START,
        UnknownAssessmentYearError,
    )

    latest = max(_SNAPSHOTS_BY_AY_START)
    with pytest.raises(UnknownAssessmentYearError):
        ScheduleOsReviewCsvWriter(assessment_year_start=latest + 2)
