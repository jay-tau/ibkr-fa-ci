"""Direct coverage for Robinhood output-group completeness and rollback."""

from __future__ import annotations

import csv
import json
import os
import stat
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, Self, cast

import pytest

from ibkr_fa import pipeline_robinhood_outputs as outputs
from ibkr_fa.compute.form67 import Form67Readiness
from ibkr_fa.compute.schedule_os import (
    ScheduleOsComputeResult,
    compute_schedule_os_dividend_lines,
)
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult
from ibkr_fa.exceptions import ConfigError, IbkrFaError, PathRefusalError
from ibkr_fa.models import DistributionKind, Dividend
from ibkr_fa.models.fa_a3_row import FaA3PeakBlankReason
from ibkr_fa.models.form67_entry import Form67Entry
from ibkr_fa.output import output_binding
from ibkr_fa.output._cleanup import FileIdentity, OutputRemovalError
from ibkr_fa.output.base import OutputBinding, RenderContext, Subcommand
from ibkr_fa.output.cbdt_json_fragment import (
    CbdtJsonIncompleteError,
    CbdtJsonStructuralError,
    CbdtScheduleFaFragmentInput,
    CbdtScheduleFaJsonWriter,
)
from ibkr_fa.output.cbdt_json_schema import CbdtSchemaError
from ibkr_fa.output.portal_labels import for_ay
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_robinhood import RobinhoodPipelineError, run_robinhood_all
from ibkr_fa.types import Isin, Ticker
from tests._crash_reports import assert_replaced_unchained
from tests._fx_stubs import AuditedFxStub
from tests._output_removal_failures import fail_unlink as _fail_unlink
from tests._robinhood_cg import ACCOUNT_LABELS, NO_OPTIONS
from tests._robinhood_cg_review import one_share_sales_cg as _xqaa_cg
from tests._robinhood_cg_review import review_rows as _review_rows
from tests._robinhood_cli import FINANCIAL_YEAR as _FY
from tests._robinhood_fa_json import ITR_FORM as _ITR_FORM
from tests._robinhood_fa_json import fa_json_claim as _json_cleanup
from tests._robinhood_fa_json import robinhood_json_result as _robinhood_json_result
from tests._robinhood_fa_json import schema_invalid_json_result as _schema_invalid_json_result
from tests._robinhood_fa_json import write_fa_json as _write_json
from tests._robinhood_income_stubs import dividend_entry, form67_assembly
from tests._robinhood_refusals import PRIVATE_OUTPUT_DIR_REFUSAL
from tests._schedule_cg_filing_workbook import robinhood_ltcg_result, robinhood_stcg_result
from tests._writer_stubs import StubWriter, patch_writers

# A blank A3 peak and the reason the engine records for it (#962).
_BLANK_A3_PEAK: dict[str, Any] = {
    "peak_value_inr": None,
    "peak_blank_reason": FaA3PeakBlankReason.EMPTY_WINDOW,
}


def _assembled(*, complete: bool) -> Any:
    """One US dividend row: READY when ``complete``, otherwise BLANK (no exact
    Indian tax payable, so no credit)."""
    amount = "15" if complete else None
    return form67_assembly(dividend_entry("US", foreign_tax="15", normal_tax=amount, credit=amount))


def _tr_result() -> ScheduleTrComputeResult:
    return ScheduleTrComputeResult(rows=[])


def _fa_result() -> Any:
    return cast(
        Any,
        SimpleNamespace(a2_rows=(), a3_rows=(), cbdt_json_eligible=True),
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")
def test_ensure_output_dir_creates_owner_only_leaf_with_normal_umask(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    previous_umask = os.umask(0o022)
    try:
        outputs.ensure_output_dir(output_dir)
    finally:
        os.umask(previous_umask)

    info = output_dir.stat()
    assert info.st_uid == os.getuid()
    assert stat.S_IMODE(info.st_mode) == 0o700


def test_ensure_output_dir_rejects_a_path_that_is_no_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The folder is replaced by a file between its creation and the check."""
    output_dir = tmp_path / "output"
    output_dir.write_bytes(b"")
    monkeypatch.setattr(Path, "mkdir", lambda *_args, **_kwargs: None)

    with pytest.raises(PathRefusalError) as raised:
        outputs.ensure_output_dir(output_dir)
    assert raised.value == PathRefusalError("Robinhood output path must be a directory", output_dir)


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership only")
def test_ensure_output_dir_rejects_directory_owned_by_another_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir(mode=0o700)
    owner = output_dir.stat().st_uid
    monkeypatch.setattr(outputs.os, "getuid", lambda: owner + 1)

    with pytest.raises(PathRefusalError) as raised:
        outputs.ensure_output_dir(output_dir)
    assert raised.value == PathRefusalError(PRIVATE_OUTPUT_DIR_REFUSAL, output_dir)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")
def test_permissive_output_dir_fails_before_existing_outputs_are_cleared(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    output_dir.chmod(0o755)
    sentinel = output_dir / "schedule-cg.csv"
    sentinel.write_bytes(b"existing private output")

    with pytest.raises(PathRefusalError) as raised:
        run_robinhood_all(
            input_dir=tmp_path / "missing-input",
            config_path=tmp_path / "missing-config.toml",
            output_dir=output_dir,
            financial_year=_FY,
            itr_form=_ITR_FORM,
            fetch_prices=False,
        )

    assert raised.value == PathRefusalError(PRIVATE_OUTPUT_DIR_REFUSAL, output_dir)
    assert sentinel.read_bytes() == b"existing private output"


def test_complete_form67_writes_only_filing_group(tmp_path: Path) -> None:
    written, readiness = outputs.write_income(
        _assembled(complete=True),
        _tr_result(),
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form=_ITR_FORM,
        debug=False,
    )

    expected = {"schedule-fsi.csv", "schedule-tr.csv", "form-67.csv"}
    assert readiness is Form67Readiness.READY
    assert {path.name for path in written} == expected
    assert {path.name for path in tmp_path.iterdir()} == expected
    with (tmp_path / "schedule-tr.csv").open(newline="", encoding="utf-8-sig") as handle:
        assert not any(row[0].startswith("Quicko") for row in csv.reader(handle))


def test_schedule_os_writes_readable_workbook_instead_of_csv(tmp_path: Path) -> None:
    written = outputs.write_os(
        ScheduleOsComputeResult(
            lines=[],
            domestic_lines=[],
            interest_total_inr_rounded=Decimal("0"),
        ),
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form=_ITR_FORM,
        debug=False,
        account_labels={},
    )

    assert {path.name for path in written} == {"schedule-os.xlsx"}
    assert {path.name for path in tmp_path.iterdir()} == {"schedule-os.xlsx"}


def test_schedule_os_debug_review_shows_dividend_kind_not_raw_text(tmp_path: Path) -> None:
    """The ``Distribution type`` column shows the dividend's kind, not
    Robinhood's raw ``Cash Div: ...`` text (#606)."""
    dividend = Dividend(
        symbol=Ticker("XQAQ"),
        isin=Isin("US99992A1097"),
        payment_date=date(2025, 5, 15),
        amount_gross=Decimal("2.50"),
        currency="USD",
        description="Cash Div: R/D 2025-05-12 P/D 2025-05-15 - 10 shares at 0.25",
        source_row_index=0,
        kind=DistributionKind.CASH_DIVIDEND,
        type_slice="Cash Dividend USD",
        source_account_index=0,
    )
    result = compute_schedule_os_dividend_lines(
        [dividend],
        financial_year_start=_FY.start_date,
        financial_year_end=_FY.end_date,
        fx_policy=AuditedFxStub(),
    )

    outputs.write_os(
        replace(result, interest_total_inr_rounded=Decimal("0")),
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form=_ITR_FORM,
        debug=True,
        account_labels={0: "Robinhood account 1"},
    )

    with (tmp_path / "schedule-os-debug.csv").open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["Distribution type"] for row in rows if row["Symbol"] == "XQAQ"] == [
        "Cash Dividend"
    ]


@pytest.mark.parametrize(
    ("debug", "expected"),
    [
        (False, {"schedule-cg.xlsx"}),
        (
            True,
            {
                "schedule-cg.xlsx",
                "schedule-cg-debug.csv",
                "schedule-cg-ltcg-debug.csv",
            },
        ),
    ],
)
def test_schedule_cg_keeps_lot_audits_behind_debug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    debug: bool,
    expected: set[str],
) -> None:
    filing = object()
    result = cast(Any, SimpleNamespace(stcg=object(), ltcg=object(), filing=lambda _lines: filing))
    filing_contexts: list[RenderContext] = []
    filing_slots: list[tuple[Subcommand, str]] = []

    class FilingWriter:
        format_id = "filing-xlsx"

        @classmethod
        def from_context(cls, ctx: RenderContext) -> Self:
            filing_contexts.append(ctx)
            return cls()

        def write(self, result: object, path: Path) -> None:
            assert result is filing
            path.write_bytes(b"filing")

    class DebugWriter(StubWriter):
        def write(self, _result: object, path: Path) -> None:
            path.write_bytes(b"debug")

    def filing_binding(subcommand: Subcommand, format_id: str) -> OutputBinding:
        filing_slots.append((subcommand, format_id))
        return OutputBinding(output_binding(subcommand, format_id).key, FilingWriter)

    monkeypatch.setattr(outputs, "output_binding", filing_binding)
    patch_writers(monkeypatch, Subcommand.CG, DebugWriter)
    patch_writers(monkeypatch, Subcommand.CG_LTCG, DebugWriter)

    written = outputs.write_cg(
        result,
        options=NO_OPTIONS,
        output_dir=tmp_path,
        format_id="review-csv",
        financial_year=_FY,
        itr_form=_ITR_FORM,
        account_labels=ACCOUNT_LABELS,
        debug=debug,
    )

    assert {path.name for path in written} == expected
    assert {path.name for path in tmp_path.iterdir()} == expected
    assert filing_slots == [(Subcommand.CG_FILING, "filing-xlsx")]
    assert filing_contexts == [
        RenderContext(2026, "ITR-3", "Robinhood", ACCOUNT_LABELS, debug=debug)
    ]


def test_schedule_cg_removes_debug_audits_when_filing_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal carries the reason; no audit of a refused filing is left (#745)."""
    result = cast(
        Any, SimpleNamespace(stcg=object(), ltcg=object(), filing=lambda _lines: object())
    )

    class RefusingFilingWriter(StubWriter):
        def write(self, _result: object, _path: Path) -> None:
            raise IbkrFaError("filing position unresolved")

    class DebugWriter(StubWriter):
        def write(self, _result: object, path: Path) -> None:
            path.write_bytes(b"debug")

    patch_writers(monkeypatch, Subcommand.CG_FILING, RefusingFilingWriter)
    patch_writers(monkeypatch, Subcommand.CG, DebugWriter)
    patch_writers(monkeypatch, Subcommand.CG_LTCG, DebugWriter)

    with pytest.raises(IbkrFaError, match="filing position unresolved"):
        outputs.write_cg(
            result,
            options=NO_OPTIONS,
            output_dir=tmp_path,
            format_id="review-csv",
            financial_year=_FY,
            itr_form=_ITR_FORM,
            account_labels=ACCOUNT_LABELS,
            debug=True,
        )

    assert list(tmp_path.iterdir()) == []


def test_incomplete_form67_writes_its_review_file_without_filing_form67(
    tmp_path: Path,
) -> None:
    written, readiness = outputs.write_income(
        _assembled(complete=False),
        _tr_result(),
        output_dir=tmp_path,
        financial_year=_FY,
        itr_form=_ITR_FORM,
        debug=False,
    )

    # #601 / #646: a BLANK row adds only form-67-debug.csv; the other review
    # files stay behind --debug, as on every income source.
    expected = {"schedule-fsi.csv", "schedule-tr.csv", "form-67-debug.csv"}
    assert readiness is Form67Readiness.BLANK
    assert {path.name for path in written} == expected
    assert {path.name for path in tmp_path.iterdir()} == expected
    assert not (tmp_path / "form-67.csv").exists()


def test_income_group_rolls_back_after_fsi_was_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, bool] = {}

    class FailingTrWriter(StubWriter):
        def write(self, _payload: object, path: Path) -> None:
            seen["fsi_existed"] = (path.parent / "schedule-fsi.csv").is_file()
            raise IbkrFaError("boom")

    patch_writers(monkeypatch, Subcommand.TR, FailingTrWriter)

    with pytest.raises(IbkrFaError, match="boom"):
        outputs.write_income(
            _assembled(complete=True),
            _tr_result(),
            output_dir=tmp_path,
            financial_year=_FY,
            itr_form=_ITR_FORM,
            debug=False,
        )

    assert seen == {"fsi_existed": True}
    assert list(tmp_path.iterdir()) == []


def test_fa_real_writers_succeed_then_group_rolls_back_after_a2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _fa_result()
    written, json_eligible, _ = outputs.write_fa(
        result,
        output_dir=tmp_path,
        format_id="review-csv",
        financial_year=_FY,
        itr_form=_ITR_FORM,
        json_available=True,
        json_cleanup=_json_cleanup(tmp_path),
    )
    expected = {"schedule-fa-a2.csv", "schedule-fa-a3.csv"}
    assert json_eligible is True
    assert {path.name for path in written} == expected
    assert {path.name for path in tmp_path.iterdir()} == expected
    for path in written:
        path.unlink()

    seen: dict[str, bool] = {}

    class FailingA3Writer(StubWriter):
        def write(self, _payload: object, path: Path) -> None:
            seen["a2_existed"] = (path.parent / "schedule-fa-a2.csv").is_file()
            raise IbkrFaError("boom")

    patch_writers(monkeypatch, Subcommand.FA, FailingA3Writer)

    with pytest.raises(IbkrFaError, match="boom"):
        outputs.write_fa(
            result,
            output_dir=tmp_path,
            format_id="review-csv",
            financial_year=_FY,
            itr_form=_ITR_FORM,
            json_available=True,
            json_cleanup=_json_cleanup(tmp_path),
        )

    assert seen == {"a2_existed": True}
    assert list(tmp_path.iterdir()) == []


def test_fa_cbdt_json_files_half_up_rounded_account_peaks(tmp_path: Path) -> None:
    # Both peaks end in .5, where HALF_UP (12345, 401) and HALF_EVEN (12344, 400)
    # differ (#619 characterization: the whole document, byte for byte).
    written, json_complete, missing = outputs.write_fa(
        _robinhood_json_result(),
        output_dir=tmp_path,
        format_id="cbdt-json-fragment",
        financial_year=_FY,
        itr_form=_ITR_FORM,
        json_available=True,
        json_cleanup=_json_cleanup(tmp_path),
    )
    assert (json_complete, missing) == (True, ())
    assert written == (tmp_path / outputs.JSON_FILENAME,)
    assert written[0].read_text(encoding="utf-8") == (
        json.dumps(
            {
                "_ibkrFa": {
                    "format": "cbdt-json-fragment",
                    "itrForm": "ITR-3",
                    "schemaVersion": "ITR-3 AY2026-27 Main V1.1 Schedule FA",
                },
                "ScheduleFA": {
                    "DtlsForeignCustodialAcc": [
                        _robinhood_a2_item("111111111", 12345),
                        _robinhood_a2_item("222222222", 401),
                    ],
                    "DtlsForeignEquityDebtInterest": [
                        {
                            "CountryName": "United States of America",
                            "CountryCodeExcludingIndia": "2",
                            "NameOfEntity": "SAMPLE ISSUER CA CORP",
                            "AddressOfEntity": "1 Sample Way, Sampletown, WA",
                            "ZipCode": "99990",
                            "NatureOfEntity": "Listed company shares",
                            "InterestAcquiringDate": "2025-03-14",
                            "InitialValOfInvstmnt": 700,
                            "PeakBalanceDuringPeriod": 821,
                            "ClosingBalance": 800,
                            "TotGrossAmtPaidCredited": 0,
                            "TotGrossProceeds": 0,
                        }
                    ],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _robinhood_a2_item(account: str, peak: int) -> dict[str, object]:
    return {
        "CountryName": "UNITED STATES OF AMERICA",
        "CountryCodeExcludingIndia": "2",
        "FinancialInstName": "Robinhood Securities, LLC",
        "FinancialInstAddress": "500 Colonial Center Parkway, Lake Mary, FL",
        "ZipCode": "32746",
        "AccountNumber": account,
        "Status": "BENEFICIAL_OWNER",
        "AccOpenDate": "2025-01-05",
        "PeakBalanceDuringPeriod": peak,
        "ClosingBalance": 800,
        "GrossAmtPaidCredited": 0,
        "NatureOfAmount": "N",
    }


def test_fa_cbdt_json_refuses_an_account_not_cross_checked(tmp_path: Path) -> None:
    with pytest.raises(CbdtJsonStructuralError, match="A2 account 2: the account number was not"):
        outputs.write_fa(
            _robinhood_json_result(second_account_verified=False),
            output_dir=tmp_path,
            format_id="cbdt-json-fragment",
            financial_year=_FY,
            itr_form=_ITR_FORM,
            json_available=True,
            json_cleanup=_json_cleanup(tmp_path),
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("schedule", ["STCG", "LTCG"])
def test_schedule_cg_refused_schedule_leaves_no_cg_output(
    tmp_path: Path, debug: bool, schedule: str
) -> None:
    """#615 owner decision 2: zero consideration with a cost is refused."""
    if schedule == "STCG":
        result = robinhood_stcg_result()
        line = replace(
            result.stcg.stcg_lines[0],
            proceeds_usd=Decimal("0.0040"),
            proceeds_inr=Decimal("0.40"),
            cost_basis_inr=Decimal("0.60"),
            realized_gain_inr=Decimal("-0.20"),
        )
        result = replace(result, stcg=replace(result.stcg, stcg_lines=[line]))
    else:
        result = robinhood_ltcg_result(cost=Decimal("0.006"), proceeds=Decimal("0.004"))

    with pytest.raises(IbkrFaError) as excinfo:
        outputs.write_cg(
            result,
            options=NO_OPTIONS,
            output_dir=tmp_path,
            format_id="review-csv",
            financial_year=_FY,
            itr_form=_ITR_FORM,
            account_labels=ACCOUNT_LABELS,
            debug=debug,
        )

    assert dict(cast(Any, excinfo.value).context) == {
        "schedule": None,
        "consideration_inr": None,
        "cost_of_acquisition_inr": None,
    }
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("format_id", ["review-csv", "xlsx"])
@pytest.mark.parametrize("two_sale_schedule", ["STCG", "LTCG"])
def test_debug_review_files_file_each_robinhood_section_total(
    tmp_path: Path, format_id: str, two_sale_schedule: str
) -> None:
    """#1383: each Robinhood sale's Amount is its consideration, so every
    section's portal-labelled review row files its total. On a5246a9 a
    section with a sale filed none (#901).

    The real Robinhood CG compute, at ₹85/$, puts a $12.00 cost / $10.00
    Amount sale (₹-170) and a $5.00 / $4.00 sale (₹-85) in one section, and a
    $3.00 / $2.00 sale (₹-85) in the other. A lot bought in 2023 is long-term.
    """
    short_term, long_term = date(2025, 5, 1), date(2023, 1, 16)
    two_sale, other = (
        (long_term, short_term) if two_sale_schedule == "LTCG" else (short_term, long_term)
    )
    result = _xqaa_cg(
        (two_sale, date(2025, 6, 16), "12.00", "10.00"),
        (two_sale, date(2025, 6, 17), "5.00", "4.00"),
        (other, date(2025, 6, 18), "3.00", "2.00"),
    )

    outputs.write_cg(
        result,
        options=NO_OPTIONS,
        output_dir=tmp_path,
        format_id=format_id,
        financial_year=_FY,
        itr_form=_ITR_FORM,
        account_labels=ACCOUNT_LABELS,
        debug=True,
    )

    extension = "xlsx" if format_id == "xlsx" else "csv"
    labels = for_ay(_FY.assessment_year_start)
    review = {
        "STCG": (
            _review_rows(tmp_path / f"schedule-cg-debug.{extension}"),
            labels.PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM[_ITR_FORM],
        ),
        "LTCG": (
            _review_rows(tmp_path / f"schedule-cg-ltcg-debug.{extension}"),
            labels.PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG_BY_ITR_FORM[_ITR_FORM],
        ),
    }
    two_sale_rows, two_sale_label = review.pop(two_sale_schedule)
    [(other_rows, other_label)] = review.values()
    # The two-sale section keeps its audit lines and audit TOTAL ...
    assert [Decimal(row[-1]) for row in two_sale_rows[1:3]] == [Decimal(-170), Decimal(-85)]
    assert two_sale_rows[-2][0] == "_audit: TOTAL Realized gain INR (full precision)"
    assert Decimal(two_sale_rows[-2][-1]) == Decimal(-255)
    # ... and its portal-labelled row files the total, as the other section's does.
    assert two_sale_rows[-1][:-1] == [two_sale_label, *[""] * 14]
    assert Decimal(two_sale_rows[-1][-1]) == Decimal(-255)
    assert other_rows[-1][:-1] == [other_label, *[""] * 14]
    assert Decimal(other_rows[-1][-1]) == Decimal(-85)


# --- Form 67 readiness (#601) ---------------------------------------------------


_US_ZERO = dividend_entry("US", foreign_tax="25", normal_tax="0", credit="0")
_CH_PARTIAL = dividend_entry("CH", foreign_tax="25", normal_tax="30")
_CH_BLANK = dividend_entry("CH", foreign_tax="25", serial=2)


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ((_US_ZERO,), r"US/DIVIDEND: the computed credit rounds to ₹0"),
        ((_CH_PARTIAL,), r"CH/DIVIDEND: CH has no row in the vendored DTAA dividend table"),
        # REFUSED outranks BLANK in a mixed result.
        ((_US_ZERO, _CH_BLANK), r"US/DIVIDEND: the computed credit rounds to ₹0"),
    ],
    ids=["zero-credit", "unvendored", "zero-with-blank"],
)
def test_refused_form67_rows_refuse_before_any_income_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entries: tuple[Form67Entry, ...],
    message: str,
) -> None:
    def refuse_write(*_args: object) -> None:
        pytest.fail("REFUSED Form 67 must be checked before any income write")

    monkeypatch.setattr(OutputBinding, "writer", refuse_write)
    with pytest.raises(ConfigError, match=message):
        outputs.write_income(
            form67_assembly(*entries),
            _tr_result(),
            output_dir=tmp_path,
            financial_year=_FY,
            itr_form=_ITR_FORM,
            debug=False,
        )
    assert list(tmp_path.iterdir()) == []


# --- Each helper call is all-or-nothing, through its last publish (#745) ------


class _Writes(StubWriter):
    """A writer that publishes its destination, then raises ``failure`` if set."""

    failure: BaseException | None = None

    def write(self, _payload: object, path: Path) -> None:
        path.write_bytes(b"published")
        if self.failure is not None:
            raise self.failure


def _publishes_then_raises(failure: BaseException) -> type[_Writes]:
    return type("_PublishesThenRaises", (_Writes,), {"failure": failure})


def _stub_writers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every writer the helpers call publish its destination."""
    for subcommand in Subcommand:
        patch_writers(monkeypatch, subcommand, _Writes)

    def write_options(_result: object, output_dir: Path, **_kwargs: object) -> Path:
        # The real writer's contract: publish, then return the path it published,
        # the folder as given joined with the file name, not resolved (#1071).
        path = output_dir / "robinhood-options.csv"
        path.write_bytes(b"published")
        return path

    monkeypatch.setattr(outputs, "write_robinhood_options_csv", write_options)
    monkeypatch.setattr(outputs, "_option_business_output", lambda _result: (None, "manual"))


_HELPER_OUTPUTS = {
    "cg": ("schedule-cg-debug.csv", "schedule-cg-ltcg-debug.csv", "schedule-cg.xlsx"),
    "os": ("schedule-os-debug.csv", "schedule-os.xlsx"),
    "income": ("form-67.csv", "schedule-fsi.csv", "schedule-tr.csv"),
    "fa": ("schedule-fa-a2.csv", "schedule-fa-a3.csv"),
    "options": ("robinhood-options.csv",),
}


def _call_helper(helper: str, tmp_path: Path) -> object:
    payload = cast(
        Any,
        SimpleNamespace(
            stcg=object(), ltcg=object(), filing=lambda _lines: object(), trades=(object(),)
        ),
    )
    year: dict[str, Any] = {"financial_year": _FY, "itr_form": _ITR_FORM}
    if helper == "cg":
        return outputs.write_cg(
            payload,
            options=NO_OPTIONS,
            output_dir=tmp_path,
            format_id="review-csv",
            account_labels=ACCOUNT_LABELS,
            debug=True,
            **year,
        )
    if helper == "os":
        return outputs.write_os(
            payload, output_dir=tmp_path, account_labels=ACCOUNT_LABELS, debug=True, **year
        )
    if helper == "income":
        return outputs.write_income(
            _assembled(complete=True), _tr_result(), output_dir=tmp_path, debug=False, **year
        )
    if helper == "fa":
        return outputs.write_fa(
            _fa_result(),
            output_dir=tmp_path,
            format_id="review-csv",
            json_available=True,
            json_cleanup=_json_cleanup(tmp_path),
            **year,
        )
    return outputs.write_options(payload, tmp_path, _FY, _ITR_FORM)


@pytest.mark.parametrize("helper", _HELPER_OUTPUTS)
def test_helper_writes_every_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper: str
) -> None:
    _stub_writers(monkeypatch)
    _call_helper(helper, tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == list(_HELPER_OUTPUTS[helper])


@pytest.mark.parametrize("helper", _HELPER_OUTPUTS)
def test_failure_after_the_last_publish_removes_every_file_the_call_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper: str
) -> None:
    """The call's last write publishes its file, then raises: every file the
    call writes is there when it fails."""
    _stub_writers(monkeypatch)
    failure = RuntimeError("after the last publish")

    def raise_once_all_published(path: Path) -> None:
        if sorted(item.name for item in path.parent.iterdir()) == list(_HELPER_OUTPUTS[helper]):
            raise failure

    class RaisesAfterTheLastPublish(_Writes):
        def write(self, _payload: object, path: Path) -> None:
            super().write(_payload, path)
            raise_once_all_published(path)

    def write_options(_result: object, output_dir: Path, **_kwargs: object) -> Path:
        path = output_dir / "robinhood-options.csv"
        path.write_bytes(b"published")
        raise_once_all_published(path)
        return path

    for subcommand in Subcommand:
        patch_writers(monkeypatch, subcommand, RaisesAfterTheLastPublish)
    monkeypatch.setattr(outputs, "write_robinhood_options_csv", write_options)

    with pytest.raises(RuntimeError) as raised:
        _call_helper(helper, tmp_path)

    assert raised.value is failure
    assert list(tmp_path.iterdir()) == []


def test_writer_that_publishes_then_raises_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The income helper's first write, Schedule FSI, publishes its file, then
    raises before Schedule TR and Form 67 are written: no file is left. Each
    helper's failure after its last publish is tested above."""
    _stub_writers(monkeypatch)
    failure = RuntimeError("after publishing")
    patch_writers(monkeypatch, Subcommand.FSI, _publishes_then_raises(failure))

    with pytest.raises(RuntimeError) as raised:
        _call_helper("income", tmp_path)

    assert raised.value is failure
    assert list(tmp_path.iterdir()) == []


def test_helper_removal_failure_names_the_file_and_removes_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_writers(monkeypatch)
    failure = IbkrFaError("form 67 writer refused")
    patch_writers(monkeypatch, Subcommand.FORM67, _publishes_then_raises(failure))
    failing = tmp_path / "schedule-fsi.csv"
    _fail_unlink(monkeypatch, failing, [True])

    with pytest.raises(OutputRemovalError) as raised:
        _call_helper("income", tmp_path)

    assert "may still be there" in str(raised.value)
    assert str(failing) in str(raised.value)
    assert_replaced_unchained(raised.value, markers=(str(failure),))
    assert [path.name for path in tmp_path.iterdir()] == [failing.name]


def test_fa_json_refuses_an_occupied_destination_untouched(tmp_path: Path) -> None:
    """A direct call has no preclear: it never arms removal of a file it didn't write."""
    fragment = tmp_path / "schedule-fa.json"
    fragment.write_bytes(b"someone else's")

    with pytest.raises(RobinhoodPipelineError, match="already exists"):
        _write_json(tmp_path)

    assert fragment.read_bytes() == b"someone else's"


def _fragment_fails_after_publishing(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> list[Path]:
    published: list[Path] = []
    write = CbdtScheduleFaJsonWriter.write

    def publish_then_raise(
        self: CbdtScheduleFaJsonWriter,
        value: CbdtScheduleFaFragmentInput,
        path: Path,
        *,
        on_staged: Callable[[FileIdentity], None],
    ) -> NoReturn:
        write(self, value, path, on_staged=on_staged)
        published.append(path)
        raise failure

    monkeypatch.setattr(CbdtScheduleFaJsonWriter, "write", publish_then_raise)
    return published


@pytest.mark.parametrize(
    "failure",
    [CbdtJsonIncompleteError(["after publishing"]), RuntimeError("after publishing")],
    ids=["incomplete", "unexpected"],
)
def test_fa_json_failure_after_publishing_removes_the_fragment_and_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """Only a missing filed value, refused before publishing, is "not written"."""
    published = _fragment_fails_after_publishing(monkeypatch, failure)

    with pytest.raises(
        RobinhoodPipelineError, match="failed after it was staged or written"
    ) as raised:
        _write_json(tmp_path)

    assert raised.value.__cause__ is failure
    assert published == [tmp_path / "schedule-fa.json"]
    assert list(tmp_path.iterdir()) == []


def test_fa_json_failure_after_publishing_whose_removal_fails_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = _fragment_fails_after_publishing(
        monkeypatch, CbdtJsonIncompleteError(["after publishing"])
    )
    fragment = tmp_path / "schedule-fa.json"
    _fail_unlink(monkeypatch, fragment, published)

    with pytest.raises(OutputRemovalError, match="may still be there") as raised:
        _write_json(tmp_path)

    assert str(fragment) in str(raised.value)
    assert_replaced_unchained(
        raised.value, markers=("failed after it was staged or written", "after publishing")
    )
    assert fragment.exists()


def test_fa_json_missing_zip_is_not_written(tmp_path: Path) -> None:
    """A missing filed value still refuses before publishing, as "not written"."""
    result = _robinhood_json_result()
    item = result.a2[0]
    unzipped = SimpleNamespace(
        row=replace(item.row, institution_zip=""), account_id_cross_checked=True
    )
    result = SimpleNamespace(**{**vars(result), "a2": (unzipped, *result.a2[1:])})

    assert _write_json(tmp_path, result) == (
        (),
        False,
        (f"A2 ZipCode: {outputs._A2_REMEDIES.zip_code}",),
    )
    assert list(tmp_path.iterdir()) == []


# AY 2027-28, which the vendored CBDT schema doesn't cover.
_UNSUPPORTED_JSON_FY = FinancialYear(2026)


def test_fa_json_not_eligible_is_not_written(tmp_path: Path) -> None:
    result = _robinhood_json_result(eligible=False, **_BLANK_A3_PEAK)

    assert _write_json(tmp_path, result) == ((), False, ())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("result", "financial_year", "error", "match"),
    [
        (
            _schema_invalid_json_result(eligible=False, **_BLANK_A3_PEAK),
            _FY,
            CbdtSchemaError,
            "ZipCode",
        ),
        (
            _robinhood_json_result(eligible=False, **_BLANK_A3_PEAK),
            _UNSUPPORTED_JSON_FY,
            CbdtJsonStructuralError,
            "AY 2027-28",
        ),
        (
            _robinhood_json_result(
                eligible=False, **_BLANK_A3_PEAK, second_account_verified=cast(Any, "false")
            ),
            _FY,
            CbdtJsonStructuralError,
            "A2 account 2: account_id_cross_checked must be a bool",
        ),
    ],
    ids=["invalid-a3-zip", "unsupported-ay", "non-bool-proof-flag"],
)
def test_fa_json_not_eligible_still_refuses_structural_and_schema_problems(
    tmp_path: Path,
    result: Any,
    financial_year: FinancialYear,
    error: type[Exception],
    match: str,
) -> None:
    with pytest.raises(error, match=match):
        _write_json(tmp_path, result, financial_year=financial_year)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("result", "financial_year", "error", "match"),
    [
        (_robinhood_json_result(), _FY, RobinhoodPipelineError, "not generated by ibkr-fa"),
        (_robinhood_json_result(), _UNSUPPORTED_JSON_FY, CbdtJsonStructuralError, "AY 2027-28"),
        (_schema_invalid_json_result(), _FY, CbdtSchemaError, "ZipCode"),
        (
            _robinhood_json_result(eligible=False, **_BLANK_A3_PEAK),
            _FY,
            RobinhoodPipelineError,
            "not generated by ibkr-fa",
        ),
    ],
    ids=["complete", "unsupported-ay", "invalid-a3-zip", "missing-a3-peak"],
)
def test_fa_json_beside_a_foreign_fragment_refuses_and_keeps_it(
    tmp_path: Path,
    result: Any,
    financial_year: FinancialYear,
    error: type[Exception],
    match: str,
) -> None:
    foreign = tmp_path / outputs.JSON_FILENAME
    foreign.write_bytes(b'{"ScheduleFA": {"hand-merged": true}}')

    with pytest.raises(error, match=match):
        _write_json(tmp_path, result, financial_year=financial_year, json_available=False)

    assert list(tmp_path.iterdir()) == [foreign]
    assert foreign.read_bytes() == b'{"ScheduleFA": {"hand-merged": true}}'
