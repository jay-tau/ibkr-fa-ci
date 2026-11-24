"""Characterize every portal-entry writer before the renderer cutovers (#1199).

The byte cases compare each portal CSV with the baseline captured at the commit
``tests/fixtures/portal_parity_baseline.json`` names (#1427 and again #1446
re-captured the ``fsi/manual-fill`` record; see docs/architecture.md); the portal workbooks'
bytes are pinned by ``test_schedule_os_output_pins.py`` and
``test_schedule_cg_filing_golden.py``. The remaining tests pin what every
writer does around those bytes: replacing an existing target or symlink, a
failed replace, the safety refusals it keeps, and the order in which it
projects rows, checks them and publishes the file.

Writers are reached only through the output catalog, and order is observed
only where the cutovers keep it: the rows a writer pulls, the staging file
``tempfile.mkstemp`` creates, each write to the staging handle ``os.fdopen``
returns, and the ``os.replace`` that publishes it.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from openpyxl import load_workbook

from ibkr_fa.compute.form67 import (
    Form67ComputeResult,
    Form67Entry,
    Form67Readiness,
    Form67SourceOfIncome,
    Form67TreatyRateStatus,
)
from ibkr_fa.compute.schedule_cg_filing import (
    ScheduleCgFilingBlocker,
    ScheduleCgFilingBlockerKind,
    ScheduleCgFilingResult,
)
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.compute.schedule_os import ScheduleOsComputeResult
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult
from ibkr_fa.exceptions import ConfigError, IbkrFaError, InvariantError
from ibkr_fa.models import form67_entry as form67_entry_model
from ibkr_fa.models.fa_a2_row import FaA2NatureOfAmount, FaA2Row, FaA2Status
from ibkr_fa.models.fa_a3_row import FaA3PeakBlankReason, FaA3Row
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.models.schedule_tr_row import ScheduleTrRow, TaxReliefSection
from ibkr_fa.output import RenderContext, Subcommand, output_binding
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.income_bundle import write_income_outputs
from ibkr_fa.output.portal_labels import (
    _SNAPSHOTS_BY_AY_START,
    PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST,
    UnknownAssessmentYearError,
)
from ibkr_fa.output.quicko_foreign_tax import QuickoForeignTaxInput
from ibkr_fa.output.schedules import form67 as form67_upload
from ibkr_fa.output.schedules.form67 import FORM67_UPLOAD_HEADERS
from tests._cbdt_fragment_inputs import unchecked_row as _unchecked
from tests._exact_refusals import INEXACT_SUM
from tests._fa_a3_name_runs import A3_ASCII_ERROR
from tests._ftc_warnings import PORTAL_RELIEF_REFUSAL, US_ZERO_CREDIT_REFUSAL
from tests._portal_parity import (
    assert_file_matches,
    assert_workbook_matches,
    baseline_cases,
    expected,
    fix_xlsx_clock,
)
from tests._robinhood_cg import ACCOUNT_LABELS, FILING_LTCG_KIND, cg_result, sale
from tests._schedule_os_hand_built import HAND_BUILT_OS_PINS as _HAND_BUILT_PINS
from tests._schedule_os_hand_built import foreign_dividend_line as _foreign
from tests._schedule_os_hand_built import hand_built_os_result as _hand_built_result

_AY = 2026
_GB = "UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND"
_GROUP = "portal-writers"


# ---- synthetic payloads ------------------------------------------------------


def _a2(**overrides: Any) -> FaA2Row:
    values: dict[str, Any] = {
        "country_code": "2",
        "country_name": "UNITED STATES OF AMERICA",
        "institution_name": "Sample Custodian, LLC",
        "institution_address": "1 Example Plaza,  Sampleville, CA",
        "institution_zip": "99990",
        "account_number": "ZQ0000001",
        "status": FaA2Status.BENEFICIAL_OWNER,
        "account_opening_date": date(2024, 1, 15),
        "peak_balance_inr": Decimal("120450.50"),
        "nature_of_amount": FaA2NatureOfAmount.INTEREST,
        "gross_amount_inr": Decimal("1234.50"),
        "base_currency": "USD",
        "audit_closing_balance_base_ccy": Decimal("1100.0055"),
        "audit_ttbr_dec31": Decimal("89.09"),
        "audit_peak_override_source": "synthetic config",
        "audit_peak_override_reason": "synthetic peak",
    }
    values.update(overrides)
    values.setdefault(
        "closing_balance_inr", values["audit_closing_balance_base_ccy"] * values["audit_ttbr_dec31"]
    )
    if values["peak_balance_inr"] is None:
        values["audit_peak_override_source"] = None
        values["audit_peak_override_reason"] = None
    return FaA2Row(**values)


def _a3(**overrides: Any) -> FaA3Row:
    values: dict[str, Any] = {
        "country_name": "United States of America",
        "country_code": "2",
        "name_of_entity": "SAMPLE ISSUER ZQ, INC",
        "address_of_entity": "100 Example Plaza, Sampleville, CA",
        "zip_code": "99990",
        "nature_of_entity": "Listed Company",
        "date_of_acquiring": date(2025, 3, 14),
        "initial_value_inr": Decimal("64217.50"),
        "peak_value_inr": Decimal("85000.49"),
        "closing_value_inr": Decimal("83000.50"),
        "gross_amount_paid_credited_inr": Decimal("500.50"),
        "gross_proceeds_from_sale_inr": Decimal("0"),
        "source_isin": "US99990Z3095",
        "source_ticker": "ZQAA",
        "source_country_iso2": "US",
        "audit_cost_basis_usd": Decimal("884.123"),
        "audit_acquisition_date": date(2025, 3, 14),
        "audit_close_price_usd": Decimal("140.4567"),
        "audit_ttbr_acquisition": Decimal("83.07"),
        "audit_ttbr_dec31": Decimal("89.53"),
        "audit_disposal_date": None,
    }
    values.update(overrides)
    values.setdefault(
        "peak_blank_reason",
        FaA3PeakBlankReason.PRICES_NOT_FETCHED if values["peak_value_inr"] is None else None,
    )
    return FaA3Row(**values)


def _fsi(**overrides: Any) -> ScheduleFsiRow:
    values: dict[str, Any] = {
        "cbdt_country_code": "2",
        "country_name_portal": "UNITED STATES OF AMERICA",
        "iso_alpha2": "US",
        "head_of_income": HeadOfIncome.OTHER_SOURCES,
        "income_inr": Decimal("12345.6789"),
        "foreign_tax_inr": Decimal("1851.234"),
        "event_count": 3,
        "tax_payable_inr": Decimal("3703.70"),
        "tax_relief_inr": Decimal("1851"),
        "dtaa_article": "Article 10(2)(b)",
        "taxpayer_id_number": "ZQ-TIN-0001",
    }
    values.update(overrides)
    return ScheduleFsiRow(**values)


def _tr(**overrides: Any) -> ScheduleTrRow:
    values: dict[str, Any] = {
        "cbdt_country_code": "2",
        "country_name_portal": "UNITED STATES OF AMERICA",
        "iso_alpha2": "US",
        "foreign_tax_inr_total": Decimal("1851"),
        "event_count": 3,
        "tax_relief_inr_total": Decimal("1851"),
        "section_under_which_claimed": TaxReliefSection.SECTION_90,
        "taxpayer_id_number": "ZQ-TIN-0001",
    }
    values.update(overrides)
    return ScheduleTrRow(**values)


def _form67_entry(serial: int = 1, **overrides: Any) -> Form67Entry:
    income = overrides.pop("income_inr", Decimal("10000.50"))
    tax = overrides.pop("foreign_tax_inr", Decimal("1500.75"))
    rate = tax / income * 100
    values: dict[str, Any] = {
        "serial_number": serial,
        "cbdt_country_code": "2",
        "country_name_portal": "UNITED STATES OF AMERICA",
        "iso_alpha2": "US",
        "source_of_income": Form67SourceOfIncome.DIVIDEND,
        "income_inr": income,
        "foreign_tax_inr": tax,
        "foreign_tax_rate_percent": rate,
        "event_count": 1,
        "tax_payable_normal_inr": Decimal("3000"),
        "tax_payable_special_inr": None,
        "section_90_dtaa_article": "Article 10(2)(b)",
        "section_90_dtaa_rate_percent": Decimal("25"),
        "tax_paid_rate_percent": rate,
        "highest_payment_rate_percent": rate,
        "treaty_rate_status": Form67TreatyRateStatus.WITHIN,
        "eligible_foreign_tax_inr": tax,
        "section_90_credit_inr": Decimal("1500"),
        "section_91_credit_inr": None,
        "total_credit_inr": Decimal("1500"),
    }
    values.update(overrides)
    return Form67Entry(**values)


_OS_ITR_FORMULA = (
    "WriterContractError: row 3 cell at index 1 starts with formula-trigger character '-'"
)
_OS_INEXACT_1E = f"InvariantError: Schedule OS 1e other income INR {INEXACT_SUM}"
_OS_UNLISTED_COUNTRY = (
    "no CBDT country code mapped for a looked-up ISO alpha-2 code; add a row to "
    "src/ibkr_fa/reference/itr_country_codes.csv after verifying the code against the "
    "official ITR-2 utility (see the .source file there for the upstream URL)."
)
_OS_PARTIAL_ACCOUNTS = (
    "InvariantError: Quicko account view requires account metadata on every dividend line"
)


def _os(
    *,
    interest: str = "280",
    inexact_1e: bool = False,
    unlisted_country: bool = False,
    partial_accounts: bool = False,
) -> ScheduleOsComputeResult:
    """The hand-built OS result with ``interest`` as its 1b(ix) total, and if
    asked: one 1e nature whose exact total needs 1,001 digits, past the
    1,000-digit cap (#1312), a last security whose country has no CBDT code, or
    a first dividend line with no account."""
    result = _hand_built_result()
    lines = list(result.lines)
    other = result.other_income_lines
    if inexact_1e:
        other = (
            replace(other[1], amount_gross_inr=Decimal("1")),
            replace(other[2], amount_gross_inr=Decimal("1E-1000")),
        )
    if unlisted_country:
        lines.append(_foreign("XQZZ", "ZZ99990Q1004", "ZZ", date(2025, 10, 1), "0.50", "84.00", 1))
    if partial_accounts:
        lines[0] = replace(lines[0], source_account_index=None)
    return replace(
        result,
        lines=lines,
        interest_total_inr_rounded=Decimal(interest),
        other_income_lines=other,
    )


def _cg_filing() -> ScheduleCgFilingResult:
    filing = cg_result(
        sale("12", "10"),
        sale("12", "10", long_term=True, lot_ordinal=3, sale_ordinal=4),
    ).filing(())
    assert filing is not None
    return filing


# ---- byte cases ---------------------------------------------------------------


@dataclass(frozen=True)
class _Artifact:
    subcommand: Subcommand
    format_id: str
    filename: str
    context: RenderContext


_ARTIFACTS = {
    "fa-a2": _Artifact(
        Subcommand.FA_A2, "filing-csv", "schedule-fa-a2.csv", RenderContext(_AY, "ITR-2")
    ),
    "fa-a3": _Artifact(Subcommand.FA, "filing-csv", "schedule-fa.csv", RenderContext(_AY, "ITR-3")),
    "os-csv": _Artifact(
        Subcommand.OS,
        "filing-csv",
        "schedule-os.csv",
        RenderContext(_AY, source_label="Robinhood", account_labels={0: "Individual", 1: "Joint"}),
    ),
    "os-xlsx": _Artifact(
        Subcommand.OS,
        "filing-xlsx",
        "schedule-os.xlsx",
        RenderContext(_AY, source_label="Robinhood", account_labels={0: "Individual", 1: "Joint"}),
    ),
    "fsi": _Artifact(Subcommand.FSI, "filing-csv", "schedule-fsi.csv", RenderContext(_AY)),
    "tr": _Artifact(Subcommand.TR, "filing-csv", "schedule-tr.csv", RenderContext(_AY, "ITR-2")),
    "form67": _Artifact(Subcommand.FORM67, "portal-csv", "form-67.csv", RenderContext(_AY)),
    "cg-xlsx": _Artifact(
        Subcommand.CG_FILING,
        "filing-xlsx",
        "schedule-cg.xlsx",
        RenderContext(_AY, "ITR-2", "Robinhood", ACCOUNT_LABELS),
    ),
}


def _quicko_block_context() -> RenderContext:
    return RenderContext(
        _AY,
        "ITR-2",
        quicko_foreign_tax=QuickoForeignTaxInput(
            form67=Form67ComputeResult(entries=(_form67_entry(),)),
            deductor_name="Sample Withholding Agent, LLC",
            foreign_tins={"US": "ZQ-TIN-0001"},
        ),
    )


# case id: (artifact, payload builder, context builder or None for the artifact's)
_CASES: dict[str, tuple[str, Callable[[], object], Callable[[], RenderContext] | None]] = {
    "fa-a2/empty": ("fa-a2", list, None),
    "fa-a2/populated": (
        "fa-a2",
        lambda: [
            _a2(),
            _a2(
                account_number="ZQ0000002",
                nature_of_amount=FaA2NatureOfAmount.NO_AMOUNT,
                gross_amount_inr=Decimal("0"),
                audit_closing_balance_base_ccy=Decimal("-0.0625"),
                audit_ttbr_dec31=Decimal("88"),
            ),
        ],
        None,
    ),
    "fa-a2/manual-fill": (
        "fa-a2",
        lambda: [_a2(peak_balance_inr=None, nature_of_amount=None, gross_amount_inr=None)],
        None,
    ),
    "fa-a3/empty": ("fa-a3", list, None),
    "fa-a3/populated": (
        "fa-a3",
        lambda: [
            _a3(),
            _a3(
                name_of_entity="SAMPLE ISSUER ZR PLC",
                country_code="44",
                country_name="United Kingdom",
                source_country_iso2="GB",
                gross_proceeds_from_sale_inr=Decimal("1200.50"),
                closing_value_inr=Decimal("0"),
            ),
        ],
        None,
    ),
    "fa-a3/manual-fill": ("fa-a3", lambda: [_a3(peak_value_inr=None)], None),
    "fsi/empty": ("fsi", lambda: ScheduleFsiComputeResult(rows=[]), None),
    "fsi/populated": (
        "fsi",
        lambda: ScheduleFsiComputeResult(
            rows=[
                _fsi(
                    head_of_income=HeadOfIncome.CAPITAL_GAINS,
                    foreign_tax_inr=Decimal("0"),
                    tax_relief_inr=Decimal("0"),
                    dtaa_article=None,
                ),
                _fsi(),
                _fsi(cbdt_country_code="44", country_name_portal=_GB, iso_alpha2="GB"),
            ]
        ),
        None,
    ),
    "fsi/manual-fill": (
        "fsi",
        lambda: ScheduleFsiComputeResult(
            rows=[_fsi(tax_payable_inr=None, tax_relief_inr=None, taxpayer_id_number=None)]
        ),
        None,
    ),
    "tr/empty": ("tr", lambda: ScheduleTrComputeResult(rows=[]), None),
    "tr/populated": (
        "tr",
        lambda: ScheduleTrComputeResult(
            rows=[
                _tr(),
                _tr(cbdt_country_code="44", country_name_portal=_GB, iso_alpha2="GB"),
            ]
        ),
        None,
    ),
    "tr/manual-fill": (
        "tr",
        lambda: ScheduleTrComputeResult(
            rows=[
                _tr(
                    tax_relief_inr_total=None,
                    section_under_which_claimed=None,
                    taxpayer_id_number=None,
                )
            ]
        ),
        None,
    ),
    "tr/quicko-block": (
        "tr",
        lambda: ScheduleTrComputeResult(rows=[_tr()]),
        _quicko_block_context,
    ),
    "form67/empty": ("form67", lambda: Form67ComputeResult(entries=()), None),
    "form67/populated": (
        "form67",
        lambda: Form67ComputeResult(
            entries=(
                _form67_entry(1),
                _form67_entry(
                    2,
                    source_of_income=Form67SourceOfIncome.INTEREST_INCOME,
                    income_inr=Decimal("2000.25"),
                    foreign_tax_inr=Decimal("307.50"),
                    section_90_credit_inr=Decimal("307"),
                    total_credit_inr=Decimal("307"),
                ),
            )
        ),
        None,
    ),
}


def _names(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir())


def _write(
    artifact: str, payload: object, path: Path, context: RenderContext | None = None
) -> None:
    spec = _ARTIFACTS[artifact]
    output_binding(spec.subcommand, spec.format_id).writer(context or spec.context).write(
        payload, path
    )


@pytest.mark.parametrize("case", _CASES)
def test_portal_csv_bytes_match_the_baseline(tmp_path: Path, case: str) -> None:
    artifact, make_payload, make_context = _CASES[case]
    path = tmp_path / _ARTIFACTS[artifact].filename

    _write(artifact, make_payload(), path, None if make_context is None else make_context())

    assert_file_matches(path.read_bytes(), expected(_GROUP, case))
    assert _names(tmp_path) == [path.name]


def test_every_writer_case_has_a_baseline_record() -> None:
    assert set(_CASES) == baseline_cases(_GROUP)


# ---- around the bytes: existing targets, failed replace, refusals ----------------

_SENTINEL = b"SENTINEL"


def _populated(artifact: str) -> object:
    if artifact in {"os-csv", "os-xlsx"}:
        return _hand_built_result()
    if artifact == "cg-xlsx":
        return _cg_filing()
    return _CASES[f"{artifact}/populated"][1]()


def _assert_baseline_bytes(artifact: str, data: bytes) -> None:
    """``data`` is the baseline of ``artifact``'s populated payload."""
    if artifact == "os-csv":
        assert hashlib.sha256(data).hexdigest() == _HAND_BUILT_PINS["filing-csv"]
    elif artifact == "os-xlsx":
        assert_workbook_matches(data, expected("schedule-os-filing-xlsx", "hand-built"))
    elif artifact == "cg-xlsx":
        assert_workbook_matches(
            data, expected("schedule-cg-filing-xlsx", "both-sections/ITR-2/default")
        )
    else:
        assert_file_matches(data, expected(_GROUP, f"{artifact}/populated"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits and symlinks")
@pytest.mark.parametrize("existing", ["file", "symlink"])
@pytest.mark.parametrize("artifact", _ARTIFACTS)
def test_writing_over_an_existing_target_publishes_the_baseline_privately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact: str, existing: str
) -> None:
    fix_xlsx_clock(monkeypatch)
    out = tmp_path / "out"
    out.mkdir()
    path = out / _ARTIFACTS[artifact].filename
    linked = tmp_path / "linked"
    linked.write_bytes(_SENTINEL)
    if existing == "file":
        path.write_bytes(_SENTINEL)
    else:
        path.symlink_to(linked)
    previous_umask = os.umask(0o022)
    try:
        _write(artifact, _populated(artifact), path)
    finally:
        os.umask(previous_umask)

    assert not path.is_symlink()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    _assert_baseline_bytes(artifact, path.read_bytes())
    assert _names(out) == [path.name]
    assert linked.read_bytes() == _SENTINEL


@pytest.mark.parametrize("artifact", _ARTIFACTS)
def test_a_failed_replace_keeps_the_existing_target_and_removes_the_staging_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact: str
) -> None:
    path = tmp_path / _ARTIFACTS[artifact].filename
    path.write_bytes(_SENTINEL)
    failure = OSError(errno.ENOSPC, "No space left on device")

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError) as raised:
        _write(artifact, _populated(artifact), path)

    assert raised.value is failure
    assert path.read_bytes() == _SENTINEL
    assert _names(tmp_path) == [path.name]


def _blocked(filing: ScheduleCgFilingResult) -> ScheduleCgFilingResult:
    blocker = ScheduleCgFilingBlocker(
        ScheduleCgFilingBlockerKind.SECTION_54_DECISION_PENDING, "Synthetic Holder Two"
    )
    return replace(filing, blockers=(blocker,))


def _blocked_cg_filing() -> ScheduleCgFilingResult:
    return _blocked(_cg_filing())


_GB_RELIEF_REFUSAL = PORTAL_RELIEF_REFUSAL.replace("on the US Other", "on the GB Other")


@dataclass(frozen=True)
class _Refusal:
    artifact: str
    payload: Callable[[], object]
    error: type[IbkrFaError]
    message: str
    staged: bool  # whether the refusal comes after a staging file was created
    context: Callable[[], RenderContext] | None = None


_REFUSALS = {
    "fa-a2/formula-trigger": _Refusal(
        "fa-a2",
        lambda: [_a2(), _a2(institution_name="=ZQ")],
        WriterContractError,
        "WriterContractError: row 2 cell at index 2 starts with formula-trigger character '='",
        staged=True,
    ),
    "fa-a2/itr-3-country": _Refusal(
        "fa-a2",
        lambda: [_a2(country_code="967", country_name="YEMEN")],
        InvariantError,
        "InvariantError: official ITR-3 Schedule FA country dropdown does not accept this code "
        "(context keys: country_code)",
        staged=True,
        context=lambda: RenderContext(_AY, "ITR-3"),
    ),
    "fa-a3/non-ascii": _Refusal(
        "fa-a3",
        lambda: [_a3(name_of_entity="SAMPLE ISSUER ZÉ")],
        InvariantError,
        A3_ASCII_ERROR,
        staged=True,
    ),
    "fa-a3/formula-trigger": _Refusal(
        "fa-a3",
        lambda: [_a3(address_of_entity="@ZQ")],
        WriterContractError,
        "WriterContractError: row 1 cell at index 3 starts with formula-trigger character '@'",
        staged=True,
    ),
    "os-csv/no-interest-total": _Refusal(
        "os-csv",
        lambda: replace(_hand_built_result(), interest_total_inr_rounded=None),
        InvariantError,
        "InvariantError: Schedule OS filing requires the separately computed interest total",
        staged=False,
    ),
    "os-xlsx/no-interest-total": _Refusal(
        "os-xlsx",
        lambda: replace(_hand_built_result(), interest_total_inr_rounded=None),
        InvariantError,
        "InvariantError: Schedule OS filing requires the separately computed interest total",
        staged=False,
    ),
    "os-csv/negative-itr-text": _Refusal(
        "os-csv",
        lambda: _os(interest="-1"),
        WriterContractError,
        _OS_ITR_FORMULA,
        staged=True,
    ),
    "os-xlsx/negative-itr-text": _Refusal(
        "os-xlsx",
        lambda: _os(interest="-1"),
        WriterContractError,
        _OS_ITR_FORMULA,
        staged=False,
    ),
    "os-xlsx/formula-trigger": _Refusal(
        "os-xlsx",
        _hand_built_result,
        WriterContractError,
        "WriterContractError: row 2 cell at index 0 starts with formula-trigger character '='",
        staged=False,
        context=lambda: RenderContext(_AY, source_label="Robinhood", account_labels={0: "=ZQ"}),
    ),
    "fsi/relief-overwritten": _Refusal(
        "fsi",
        lambda: ScheduleFsiComputeResult(rows=[_fsi(tax_relief_inr=Decimal("1000"))]),
        InvariantError,
        PORTAL_RELIEF_REFUSAL,
        staged=False,
    ),
    "fsi/nul-byte": _Refusal(
        "fsi",
        lambda: ScheduleFsiComputeResult(rows=[_fsi(taxpayer_id_number="ZQ\x00")]),
        WriterContractError,
        "WriterContractError: row 1 cell at index 1 contains a NUL byte",
        staged=True,
    ),
    "tr/relief-overwritten": _Refusal(
        "tr",
        lambda: ScheduleTrComputeResult(
            rows=[_tr()], fsi_rows_with_overwritten_relief=(_fsi(tax_relief_inr=Decimal("1000")),)
        ),
        InvariantError,
        PORTAL_RELIEF_REFUSAL,
        staged=False,
    ),
    "tr/formula-trigger": _Refusal(
        "tr",
        lambda: ScheduleTrComputeResult(rows=[_tr(taxpayer_id_number="+ZQ")]),
        WriterContractError,
        "WriterContractError: row 1 cell at index 1 starts with formula-trigger character '+'",
        staged=True,
    ),
    "form67/blank": _Refusal(
        "form67",
        lambda: Form67ComputeResult(
            entries=(
                _form67_entry(
                    tax_payable_normal_inr=None,
                    section_90_credit_inr=None,
                    total_credit_inr=None,
                ),
            )
        ),
        ConfigError,
        "ConfigError at field 'form67': the official Form 67 CSV needs every row's Indian tax "
        "payable and credit; rows left blank for manual work belong in form-67-debug.csv",
        staged=True,
    ),
    "form67/refused": _Refusal(
        "form67",
        lambda: Form67ComputeResult(
            entries=(
                _form67_entry(
                    section_90_credit_inr=Decimal("0.4"), total_credit_inr=Decimal("0.4")
                ),
            )
        ),
        ConfigError,
        f"ConfigError at field 'form67': {US_ZERO_CREDIT_REFUSAL}",
        staged=True,
    ),
    "form67/others-source": _Refusal(
        "form67",
        lambda: Form67ComputeResult(
            entries=(_form67_entry(source_of_income=Form67SourceOfIncome.OTHERS),)
        ),
        InvariantError,
        "InvariantError: official Form 67 source code 10 (Others) requires a description that "
        "Form67Entry does not carry (context keys: source)",
        staged=True,
    ),
    "cg-xlsx/blocked": _Refusal(
        "cg-xlsx",
        _blocked_cg_filing,
        InvariantError,
        f"InvariantError: Schedule CG filing is blocked: {FILING_LTCG_KIND} (context keys: "
        "reasons)",
        staged=False,
    ),
    "cg-xlsx/formula-trigger": _Refusal(
        "cg-xlsx",
        _cg_filing,
        WriterContractError,
        "WriterContractError: row 4 cell at index 2 starts with formula-trigger character '='",
        staged=False,
        context=lambda: RenderContext(_AY, "ITR-2", "=ZQ", ACCOUNT_LABELS),
    ),
    "cg-xlsx/no-source-label": _Refusal(
        "cg-xlsx",
        _cg_filing,
        InvariantError,
        "InvariantError: the Schedule CG filing workbook needs the run's ITR form and source "
        "label (context keys: itr_form, source_label)",
        staged=False,
        context=lambda: RenderContext(_AY, "ITR-2", None, ACCOUNT_LABELS),
    ),
}


@pytest.fixture
def stagings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The staging files ``tempfile.mkstemp`` creates, by name."""
    created: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        created.append(Path(name).name)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
    return created


@pytest.mark.parametrize("refusal", _REFUSALS)
def test_a_safety_refusal_keeps_the_existing_target_and_leaves_no_staging_file(
    tmp_path: Path, stagings: list[str], refusal: str
) -> None:
    case = _REFUSALS[refusal]
    path = tmp_path / _ARTIFACTS[case.artifact].filename
    path.write_bytes(_SENTINEL)
    payload, context = case.payload(), None if case.context is None else case.context()

    with pytest.raises(case.error) as raised:
        _write(case.artifact, payload, path, context)

    assert type(raised.value) is case.error
    assert str(raised.value) == case.message
    assert len(stagings) == case.staged
    assert path.read_bytes() == _SENTINEL
    assert _names(tmp_path) == [path.name]


# ---- projection, checking and publication order ------------------------------


class _LoggedHandle:
    """The staging handle, logging each write."""

    def __init__(self, handle: Any, events: list[str]) -> None:
        self._handle = handle
        self._events = events

    def write(self, text: str) -> int:
        self._events.append("write")
        return self._handle.write(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """``stage``, ``write`` and ``publish`` events, in order, beside the rows
    a test's payload logs as the writer pulls them."""
    log: list[str] = []
    real_mkstemp, real_fdopen, real_replace = tempfile.mkstemp, os.fdopen, os.replace

    def mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        log.append("stage")
        return real_mkstemp(*args, **kwargs)

    def fdopen(*args: Any, **kwargs: Any) -> _LoggedHandle:
        return _LoggedHandle(real_fdopen(*args, **kwargs), log)

    def replace_(*args: Any, **kwargs: Any) -> None:
        log.append("publish")
        real_replace(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(os, "fdopen", fdopen)
    monkeypatch.setattr(os, "replace", replace_)
    return log


def _pulled(rows: list[Any], log: list[str]) -> Iterator[Any]:
    """A generator of ``rows`` that logs ``row <i>`` as each is pulled."""
    for index, row in enumerate(rows):
        log.append(f"row {index}")
        yield row


class _Rows(list[Any]):
    """A result's row list that logs ``row <i>`` each time it is iterated."""

    log: list[str]

    def __iter__(self) -> Iterator[Any]:
        for index, row in enumerate(list.__iter__(self)):
            self.log.append(f"row {index}")
            yield row


class _Entries(tuple[Any, ...]):
    """A result's entry tuple that logs ``<label> <i>`` each time it is iterated."""

    log: list[str]
    label = "row"

    def __iter__(self) -> Iterator[Any]:
        for index, row in enumerate(tuple.__iter__(self)):
            self.log.append(f"{self.label} {index}")
            yield row


def _logged(artifact: str, payload: Any, log: list[str]) -> Any:
    """``payload`` with its rows logging each pull into ``log``."""
    if artifact in {"fa-a2", "fa-a3"}:
        return _pulled(payload, log)
    if artifact in {"fsi", "tr"}:
        rows = _Rows(payload.rows)
        rows.log = log
        return replace(payload, rows=rows)
    if artifact == "form67":
        entries = _Entries(payload.entries)
        entries.log = log
        return replace(payload, entries=entries)
    return payload


def _run_logged(
    tmp_path: Path,
    events: list[str],
    artifact: str,
    payload: Any,
    context: RenderContext | None = None,
) -> tuple[list[str], BaseException | None]:
    path = tmp_path / _ARTIFACTS[artifact].filename
    logged = _logged(artifact, payload, events)
    events.clear()  # a result's own checks iterate its rows when it is built
    try:
        _write(artifact, logged, path, context)
    except Exception as exc:
        return list(events), exc
    return list(events), None


def _runs(log: list[str]) -> list[str]:
    """``log`` with each run of repeated events written once, as ``"<event> x<n>"``."""
    runs: list[str] = []
    for event in log:
        if runs and runs[-1].split(" x")[0] == event:
            count = int(runs[-1].split(" x")[1]) + 1 if " x" in runs[-1] else 2
            runs[-1] = f"{event} x{count}"
        else:
            runs.append(event)
    return runs


# The events a writer's populated payload logs: FA and TR stream their rows,
# FSI pulls every row for its relief check and again for its grouping pass
# before writing, and Form 67 checks its entries before the header.
# Workbook construction refusals are checked above, before staging.
_ORDER = {
    "fa-a2": ["stage", "write", "row 0", "write", "row 1", "write", "publish"],
    "fa-a3": ["stage", "write", "row 0", "write", "row 1", "write", "publish"],
    "os-csv": ["stage", "write x45", "publish"],
    "os-xlsx": ["stage", "publish"],
    "fsi": ["row 0", "row 1", "row 2", "stage", "row 0", "row 1", "row 2", "write x23", "publish"],
    "tr": ["stage", "write", "row 0", "write", "row 1", "write", "publish"],
    "form67": [
        "stage",
        *("row 0", "row 1") * 4,
        "write",
        "row 0",
        "write",
        "row 1",
        "write",
        "publish",
    ],
    "cg-xlsx": ["stage", "publish"],
}


@pytest.mark.parametrize("artifact", _ARTIFACTS)
def test_projection_checking_and_publication_order(
    tmp_path: Path, events: list[str], artifact: str
) -> None:
    log, failure = _run_logged(tmp_path, events, artifact, _populated(artifact))

    assert failure is None
    assert _runs(log) == _ORDER[artifact]


# Mixed failures and row-iteration controls: the refusal that wins, and the events
# before it, show a writer streams its rows without reading ahead, projects a
# row before checking it, checks every cell for a NUL byte before any for a
# formula trigger, that FSI groups every row before checking one, and that FSI
# and TR check relief before staging, ahead of any row's cell checks (#1204).
# Both OS artifacts check each ITR row before projecting the next (#1202), and
# project every Quicko entry after the ITR rows and before the first Quicko
# row, sheet or account split.
_MIXED = {
    "os-csv/itr-check-before-a-later-itr-projection": (
        "os-csv",
        lambda: _os(interest="-1", inexact_1e=True),
        _OS_ITR_FORMULA,
        ["stage", "write x3"],
    ),
    "os-csv/itr-projection-alone": (
        "os-csv",
        lambda: _os(inexact_1e=True),
        _OS_INEXACT_1E,
        ["stage", "write x4"],
    ),
    "os-csv/quicko-entries-after-the-itr-rows": (
        "os-csv",
        lambda: _os(unlisted_country=True),
        _OS_UNLISTED_COUNTRY,
        ["stage", "write x12"],
    ),
    "os-xlsx/itr-check-before-a-later-itr-projection": (
        "os-xlsx",
        lambda: _os(interest="-1", inexact_1e=True),
        _OS_ITR_FORMULA,
        [],
    ),
    "os-xlsx/itr-projection-alone": ("os-xlsx", lambda: _os(inexact_1e=True), _OS_INEXACT_1E, []),
    "os-xlsx/itr-sheet-before-quicko-entries": (
        "os-xlsx",
        lambda: _os(interest="-1", unlisted_country=True),
        _OS_ITR_FORMULA,
        [],
    ),
    "os-xlsx/quicko-entries-before-the-account-split": (
        "os-xlsx",
        lambda: _os(unlisted_country=True, partial_accounts=True),
        _OS_UNLISTED_COUNTRY,
        [],
    ),
    "os-xlsx/account-split-alone": (
        "os-xlsx",
        lambda: _os(partial_accounts=True),
        _OS_PARTIAL_ACCOUNTS,
        [],
    ),
    "fa-a2/no-read-ahead": (
        "fa-a2",
        lambda: [_a2(), _a2(institution_name="=ZQ"), _a2()],
        "WriterContractError: row 2 cell at index 2 starts with formula-trigger character '='",
        ["stage", "write", "row 0", "write", "row 1"],
    ),
    "fa-a3/check-before-a-later-projection": (
        "fa-a3",
        lambda: [_a3(), _a3(address_of_entity="@ZQ"), _a3(name_of_entity="SAMPLE ISSUER ZÉ")],
        "WriterContractError: row 2 cell at index 3 starts with formula-trigger character '@'",
        ["stage", "write", "row 0", "write", "row 1"],
    ),
    "fa-a3/projection-before-check-in-a-row": (
        "fa-a3",
        lambda: [_a3(name_of_entity="@ZQ", address_of_entity="100 Example Plaza, Sampleville É")],
        A3_ASCII_ERROR,
        ["stage", "write", "row 0"],
    ),
    "fsi/grouping-before-checks": (
        "fsi",
        lambda: ScheduleFsiComputeResult(
            rows=[
                _fsi(taxpayer_id_number="=ZQ"),
                _fsi(cbdt_country_code="44", country_name_portal=_GB, iso_alpha2="GB"),
            ]
        ),
        "WriterContractError: row 1 cell at index 1 starts with formula-trigger character '='",
        ["row 0", "row 1", "stage", "row 0", "row 1", "write"],
    ),
    "fsi/nul-before-formula": (
        "fsi",
        lambda: ScheduleFsiComputeResult(rows=[_fsi(taxpayer_id_number="=ZQ\x00")]),
        "WriterContractError: row 1 cell at index 1 contains a NUL byte",
        ["row 0", "stage", "row 0", "write"],
    ),
    "fsi/relief-before-staging-and-an-earlier-rows-checks": (
        "fsi",
        lambda: ScheduleFsiComputeResult(
            rows=[
                _fsi(taxpayer_id_number="ZQ\x00"),
                _fsi(
                    cbdt_country_code="44",
                    country_name_portal=_GB,
                    iso_alpha2="GB",
                    tax_relief_inr=Decimal("1000"),
                ),
            ]
        ),
        _GB_RELIEF_REFUSAL,
        ["row 0", "row 1"],
    ),
    "tr/relief-before-staging-and-row-checks": (
        "tr",
        lambda: ScheduleTrComputeResult(
            rows=[_tr(taxpayer_id_number="+ZQ")],
            fsi_rows_with_overwritten_relief=(_fsi(tax_relief_inr=Decimal("1000")),),
        ),
        PORTAL_RELIEF_REFUSAL,
        [],
    ),
    "tr/no-read-ahead": (
        "tr",
        lambda: ScheduleTrComputeResult(
            rows=[
                _tr(taxpayer_id_number="+ZQ"),
                _tr(cbdt_country_code="44", country_name_portal=_GB, iso_alpha2="GB"),
            ]
        ),
        "WriterContractError: row 1 cell at index 1 starts with formula-trigger character '+'",
        ["stage", "write", "row 0"],
    ),
    "form67/projects-each-entry-after-the-header": (
        "form67",
        lambda: Form67ComputeResult(
            entries=(
                _form67_entry(1),
                _form67_entry(2, source_of_income=Form67SourceOfIncome.OTHERS),
            )
        ),
        "InvariantError: official Form 67 source code 10 (Others) requires a description that "
        "Form67Entry does not carry (context keys: source)",
        ["stage", *("row 0", "row 1") * 4, "write", "row 0", "write", "row 1"],
    ),
}


@pytest.mark.parametrize("case", _MIXED)
def test_mixed_failures_show_the_projection_and_checking_order(
    tmp_path: Path, events: list[str], case: str
) -> None:
    artifact, make_payload, message, order = _MIXED[case]

    log, failure = _run_logged(tmp_path, events, artifact, make_payload())

    assert str(failure) == message
    assert _runs(log) == order
    assert _names(tmp_path) == []


# ---- FA upload rows through the shared renderer (#1201) -------------------------


@pytest.mark.parametrize("case", [case for case in _CASES if case.startswith("fa-")])
def test_fa_upload_bytes_from_a_generator_match_the_baseline(tmp_path: Path, case: str) -> None:
    artifact, make_payload, _context = _CASES[case]
    path = tmp_path / _ARTIFACTS[artifact].filename
    rows: Any = make_payload()

    _write(artifact, (row for row in rows), path)

    assert_file_matches(path.read_bytes(), expected(_GROUP, case))
    assert _names(tmp_path) == [path.name]


_FILED_REFUSAL = "InvariantError: synthetic filed amounts refused"
_ITR3_COUNTRY_REFUSAL = (
    "InvariantError: official ITR-3 Schedule FA country dropdown does not accept this code "
    "(context keys: country_code)"
)


def _refuse_filed(_row: object) -> object:
    raise InvariantError(what="synthetic filed amounts refused", context=())


def _over_long_institution_name() -> FaA2Row:
    """An A2 row whose name, longer than the row's own check allows, reaches the
    writer's length check: the row's check refuses it first otherwise."""
    row = _a2()
    object.__setattr__(row, "institution_name", "Z" * 126)
    return row


@dataclass(frozen=True)
class _FaOrder:
    artifact: str
    itr_form: Literal["ITR-2", "ITR-3"]
    rows: Callable[[], list[Any]]
    refuse_filed: bool  # whether every row's .filed refuses
    error: type[IbkrFaError]
    message: str
    order: list[str]
    cell: tuple[int, int] | None = None  # a WriterContractError's (row, cell) index


# Each refusal the projection order puts first, against the one it would put
# after it, and the same refusals alone. Rows are pulled from a generator one at
# a time: "row 2" never appearing after a row-1 refusal means no read-ahead.
_FA_ORDER = {
    "fa-a2/country-before-filed": _FaOrder(
        "fa-a2",
        "ITR-3",
        lambda: [_a2(country_code="967", country_name="YEMEN")],
        True,
        InvariantError,
        _ITR3_COUNTRY_REFUSAL,
        ["stage", "write", "row 0"],
    ),
    "fa-a3/country-before-filed": _FaOrder(
        "fa-a3",
        "ITR-3",
        lambda: [_a3(country_code="263", country_name="Zimbabwe", source_country_iso2="ZW")],
        True,
        InvariantError,
        _ITR3_COUNTRY_REFUSAL,
        ["stage", "write", "row 0"],
    ),
    "fa-a2/filed-before-text": _FaOrder(
        "fa-a2",
        "ITR-2",
        lambda: [_over_long_institution_name()],
        True,
        InvariantError,
        _FILED_REFUSAL,
        ["stage", "write", "row 0"],
    ),
    "fa-a2/text-alone": _FaOrder(
        "fa-a2",
        "ITR-2",
        lambda: [_over_long_institution_name()],
        False,
        InvariantError,
        "InvariantError: official Schedule FA A2 institution name must be at most 125 characters "
        "(context keys: length)",
        ["stage", "write", "row 0"],
    ),
    "fa-a3/filed-before-text": _FaOrder(
        "fa-a3",
        "ITR-3",
        lambda: [_a3(name_of_entity="SAMPLE ISSUER ZÉ")],
        True,
        InvariantError,
        _FILED_REFUSAL,
        ["stage", "write", "row 0"],
    ),
    "fa-a3/text-alone": _FaOrder(
        "fa-a3",
        "ITR-3",
        lambda: [_a3(name_of_entity="SAMPLE ISSUER ZÉ")],
        False,
        InvariantError,
        A3_ASCII_ERROR,
        ["stage", "write", "row 0"],
    ),
    "fa-a2/nul-in-a-later-cell-before-formula": _FaOrder(
        "fa-a2",
        "ITR-2",
        lambda: [_a2(), _a2(institution_name="=ZQ", institution_zip="ZQ\x00")],
        False,
        WriterContractError,
        "WriterContractError: row 2 cell at index 4 contains a NUL byte",
        ["stage", "write", "row 0", "write", "row 1"],
        (2, 4),
    ),
    "fa-a3/nul-in-a-later-cell-before-formula": _FaOrder(
        "fa-a3",
        "ITR-3",
        # The row refuses a control character itself (#1367); this pins the writer.
        lambda: [_unchecked(_a3(name_of_entity="=ZQ"), nature_of_entity="Listed\x00")],
        False,
        WriterContractError,
        "WriterContractError: row 1 cell at index 5 contains a NUL byte",
        ["stage", "write", "row 0"],
        (1, 5),
    ),
    "fa-a2/safety-refusal-before-a-later-projection-refusal": _FaOrder(
        "fa-a2",
        "ITR-3",
        lambda: [
            _a2(),
            _a2(institution_name="=ZQ"),
            _a2(country_code="967", country_name="YEMEN"),
        ],
        False,
        WriterContractError,
        "WriterContractError: row 2 cell at index 2 starts with formula-trigger character '='",
        ["stage", "write", "row 0", "write", "row 1"],
        (2, 2),
    ),
    "fa-a3/safety-refusal-before-a-later-projection-refusal": _FaOrder(
        "fa-a3",
        "ITR-3",
        lambda: [
            _a3(),
            _a3(address_of_entity="@ZQ"),
            _a3(country_code="263", country_name="Zimbabwe", source_country_iso2="ZW"),
        ],
        False,
        WriterContractError,
        "WriterContractError: row 2 cell at index 3 starts with formula-trigger character '@'",
        ["stage", "write", "row 0", "write", "row 1"],
        (2, 3),
    ),
}


@pytest.mark.parametrize("case", _FA_ORDER)
def test_fa_upload_refusal_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: list[str], case: str
) -> None:
    spec = _FA_ORDER[case]
    rows = spec.rows()
    if spec.refuse_filed:
        row_type = FaA2Row if spec.artifact == "fa-a2" else FaA3Row
        monkeypatch.setattr(row_type, "filed", property(_refuse_filed))
    path = tmp_path / _ARTIFACTS[spec.artifact].filename
    events.clear()

    with pytest.raises(IbkrFaError) as raised:
        _write(spec.artifact, _pulled(rows, events), path, RenderContext(_AY, spec.itr_form))

    assert type(raised.value) is spec.error
    assert str(raised.value) == spec.message
    if spec.cell is not None:
        assert isinstance(raised.value, WriterContractError)
        assert (raised.value.row_index, raised.value.cell_index) == spec.cell
    assert _runs(events) == spec.order
    assert _names(tmp_path) == []


# ---- Schedule OS filing rows through the shared renderers (#1202) -----------------


@pytest.mark.parametrize(
    ("interest", "value", "data_type", "number_format"),
    [("1", 1, "n", "#,##0"), ("1.0", "1.0", "s", "General"), ("1E+3", "1E+3", "s", "General")],
)
def test_os_workbook_converts_itr_text_after_checking_it(
    tmp_path: Path, interest: str, value: object, data_type: str, number_format: str
) -> None:
    """The workbook checks each ITR row's text, then turns whole-number text
    into a number and keeps any other text; ``-1`` is refused as a formula
    trigger before it could become a number (``_REFUSALS``)."""
    path = tmp_path / "schedule-os.xlsx"

    _write("os-xlsx", _os(interest=interest), path)

    workbook = load_workbook(path)
    try:
        sheet = workbook["ITR Schedule OS"]
        assert sheet["A4"].value == PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST
        cell = sheet["B4"]
        assert (cell.value, cell.data_type, cell.number_format) == (value, data_type, number_format)
    finally:
        workbook.close()


def test_os_partial_account_metadata_refuses_only_the_workbook(
    tmp_path: Path, stagings: list[str]
) -> None:
    """The CSV never asks for the account split, so with one dividend line's
    account unknown it still writes the hand-built bytes, while the workbook
    keeps its refusal before staging."""
    result = _os(partial_accounts=True)
    csv_path, xlsx_path = tmp_path / "schedule-os.csv", tmp_path / "schedule-os.xlsx"

    _write("os-csv", result, csv_path)
    with pytest.raises(InvariantError) as raised:
        _write("os-xlsx", result, xlsx_path)

    assert hashlib.sha256(csv_path.read_bytes()).hexdigest() == _HAND_BUILT_PINS["filing-csv"]
    assert str(raised.value) == _OS_PARTIAL_ACCOUNTS
    assert len(stagings) == 1
    assert _names(tmp_path) == [csv_path.name]


# ---- Form 67 upload rows through the shared renderer (#1203) --------------------

_FORM67_FILED_REFUSAL = _FILED_REFUSAL
_FORM67_SOURCE_OTHERS = (
    "InvariantError: official Form 67 source code 10 (Others) requires a description that "
    "Form67Entry does not carry (context keys: source)"
)
_FORM67_COUNTRY_MISMATCH = (
    "InvariantError: official Form 67 country code must match the vendored country reference "
    "(context keys: iso_alpha2, expected, actual)"
)
_FORM67_RATE = (
    "InvariantError: official Form 67 rate must be between 0 and 100 percent "
    "(context keys: field, value)"
)
_FORM67_BLANK = _REFUSALS["form67/blank"].message
_FORM67_REFUSED = _REFUSALS["form67/refused"].message
# The events before the header: the readiness checks read every entry four times,
# and the REFUSED check raises after its third read.
_FORM67_READINESS = ["stage", *("row 0",) * 4]
_FORM67_READINESS_2 = ["stage", *("row 0", "row 1") * 4]
_FORM67_REFUSED_READINESS = ["stage", *("row 0",) * 3]
_FORM67_REFUSED_READINESS_2 = ["stage", *("row 0", "row 1") * 3]


def _blank_form67_entry(serial: int = 1, **overrides: Any) -> Form67Entry:
    return _form67_entry(
        serial,
        tax_payable_normal_inr=None,
        section_90_credit_inr=None,
        total_credit_inr=None,
        **overrides,
    )


def _refused_form67_entry(serial: int = 1, **overrides: Any) -> Form67Entry:
    """A row whose credit files as ₹0: REFUSED."""
    return _form67_entry(
        serial, section_90_credit_inr=Decimal("0.4"), total_credit_inr=Decimal("0.4"), **overrides
    )


def _stub_form67_entry(**filed: Any) -> Form67Entry:
    """A ready entry whose filed values carry ``filed``, which ``Form67Entry``
    itself refuses."""
    entry = _form67_entry()
    stub = SimpleNamespace(**{name: getattr(entry, name) for name in entry.__slots__})
    stub.filed = replace(entry.filed, **filed)
    return cast("Form67Entry", stub)


@dataclass(frozen=True)
class _Form67Order:
    entries: Callable[[], tuple[Any, ...]]
    error: type[IbkrFaError]
    message: str
    order: list[str]
    header_trigger: bool = False  # the header's first cell starts with '='
    refuse_filed: bool = False  # every entry's .filed refuses once the header is written
    article: str | None = None  # every row's filed column (9) article number
    cell: tuple[int, int] | None = None  # a WriterContractError's (row, cell) index


# Readiness runs once the file is staged, before the header and every row. In a
# row, the country checks come before the source check, which comes before
# .filed; the filed cells are then rendered in column order. An earlier row's
# safety refusal comes before a later row's projection, which never runs.
_FORM67_ORDER = {
    "form67/refused-before-the-header-check": _Form67Order(
        lambda: (_refused_form67_entry(),),
        ConfigError,
        _FORM67_REFUSED,
        _FORM67_REFUSED_READINESS,
        header_trigger=True,
    ),
    "form67/blank-before-the-header-check": _Form67Order(
        lambda: (_blank_form67_entry(),),
        ConfigError,
        _FORM67_BLANK,
        _FORM67_READINESS,
        header_trigger=True,
    ),
    "form67/header-check-alone": _Form67Order(
        lambda: (_form67_entry(),),
        WriterContractError,
        "WriterContractError: row 0 cell at index 0 starts with formula-trigger character '='",
        _FORM67_READINESS,
        header_trigger=True,
        cell=(0, 0),
    ),
    "form67/refused-before-a-projection-refusal": _Form67Order(
        lambda: (
            _refused_form67_entry(),
            _form67_entry(2, source_of_income=Form67SourceOfIncome.OTHERS),
        ),
        ConfigError,
        _FORM67_REFUSED,
        _FORM67_REFUSED_READINESS_2,
    ),
    "form67/blank-before-a-projection-refusal": _Form67Order(
        lambda: (
            _blank_form67_entry(),
            _form67_entry(2, source_of_income=Form67SourceOfIncome.OTHERS),
        ),
        ConfigError,
        _FORM67_BLANK,
        _FORM67_READINESS_2,
    ),
    "form67/others-country-before-the-country-match": _Form67Order(
        lambda: (_form67_entry(cbdt_country_code="9999"),),
        InvariantError,
        "InvariantError: official Form 67 country code 9999 (Others) requires a description "
        "that Form67Entry does not carry (context keys: iso_alpha2)",
        [*_FORM67_READINESS, "write", "row 0"],
    ),
    "form67/country-match-before-the-others-source": _Form67Order(
        lambda: (
            _form67_entry(cbdt_country_code="44", source_of_income=Form67SourceOfIncome.OTHERS),
        ),
        InvariantError,
        _FORM67_COUNTRY_MISMATCH,
        [*_FORM67_READINESS, "write", "row 0"],
    ),
    "form67/others-source-before-filed": _Form67Order(
        lambda: (_form67_entry(source_of_income=Form67SourceOfIncome.OTHERS),),
        InvariantError,
        _FORM67_SOURCE_OTHERS,
        [*_FORM67_READINESS, "write", "row 0"],
        refuse_filed=True,
    ),
    "form67/filed-alone": _Form67Order(
        lambda: (_form67_entry(),),
        InvariantError,
        _FORM67_FILED_REFUSAL,
        [*_FORM67_READINESS, "write", "row 0"],
        refuse_filed=True,
    ),
    "form67/amount-before-a-later-rate": _Form67Order(
        lambda: (
            _stub_form67_entry(
                income_inr=Decimal("999999999999999"), tax_paid_rate_percent=Decimal("101")
            ),
        ),
        InvariantError,
        "InvariantError: official Form 67 amount must be a non-negative integer of at most 14 "
        "digits (context keys: field, value)",
        [*_FORM67_READINESS, "write", "row 0"],
    ),
    "form67/rate-alone": _Form67Order(
        lambda: (_stub_form67_entry(tax_paid_rate_percent=Decimal("101")),),
        InvariantError,
        _FORM67_RATE,
        [*_FORM67_READINESS, "write", "row 0"],
    ),
    "form67/rate-before-a-later-article": _Form67Order(
        lambda: (
            _stub_form67_entry(
                tax_paid_rate_percent=Decimal("101"), section_90_dtaa_article="Section 25"
            ),
        ),
        InvariantError,
        _FORM67_RATE,
        [*_FORM67_READINESS, "write", "row 0"],
    ),
    "form67/article-alone": _Form67Order(
        lambda: (_stub_form67_entry(section_90_dtaa_article="Section 25"),),
        InvariantError,
        "InvariantError: Form67FiledValues.section_90_dtaa_article (col 9) must be a DTAA "
        "citation shaped like 'Article 10(2)(b)'",
        [*_FORM67_READINESS, "write", "row 0"],
    ),
    "form67/nul-before-formula-in-a-cell": _Form67Order(
        lambda: (_form67_entry(),),
        WriterContractError,
        "WriterContractError: row 1 cell at index 10 contains a NUL byte",
        [*_FORM67_READINESS, "write", "row 0"],
        article="=ZQ\x00",
        cell=(1, 10),
    ),
    "form67/safety-refusal-before-a-later-projection-refusal": _Form67Order(
        lambda: (
            _form67_entry(1),
            _form67_entry(2, source_of_income=Form67SourceOfIncome.OTHERS),
        ),
        WriterContractError,
        "WriterContractError: row 1 cell at index 10 starts with formula-trigger character '='",
        [*_FORM67_READINESS_2, "write", "row 0"],
        article="=ZQ",
        cell=(1, 10),
    ),
}


@pytest.mark.parametrize("case", _FORM67_ORDER)
def test_form67_upload_refusal_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: list[str], case: str
) -> None:
    spec = _FORM67_ORDER[case]
    if spec.header_trigger:
        monkeypatch.setattr(
            form67_upload, "FORM67_UPLOAD_HEADERS", ("=ZQ", *FORM67_UPLOAD_HEADERS[1:])
        )
    if spec.refuse_filed:
        filed = Form67Entry.filed.fget
        assert filed is not None
        monkeypatch.setattr(
            Form67Entry,
            "filed",
            property(lambda entry: _refuse_filed(entry) if "write" in events else filed(entry)),
        )
    if spec.article is not None:
        article = spec.article
        monkeypatch.setattr(
            form67_entry_model, "bare_article_number", lambda _citation, *, field: article
        )
    payload = Form67ComputeResult(entries=spec.entries())

    log, failure = _run_logged(tmp_path, events, "form67", payload)

    assert type(failure) is spec.error
    assert str(failure) == spec.message
    if spec.cell is not None:
        assert isinstance(failure, WriterContractError)
        assert (failure.row_index, failure.cell_index) == spec.cell
    assert _runs(log) == _runs(spec.order)
    assert _names(tmp_path) == []


# ---- FSI and TR filing worksheets through the shared renderer (#1204) ------------


def _logging_quicko_context(log: list[str]) -> RenderContext:
    """The Quicko block context, its Form 67 entries logging ``entry <i>`` into
    ``log`` as they are read."""
    context = _quicko_block_context()
    quicko = context.quicko_foreign_tax
    assert quicko is not None
    entries = _Entries(quicko.form67.entries)
    entries.log, entries.label = log, "entry"
    form67 = replace(quicko.form67, entries=entries)
    return replace(context, quicko_foreign_tax=replace(quicko, form67=form67))


# FSI never reads the run's Form 67 entries; TR reads them for its Quicko block
# only after its last row, so an earlier row's refusal stops it before the block.
_QUICKO_ORDER: dict[str, tuple[str, Callable[[], object], str | None, list[str]]] = {
    "fsi": ("fsi", lambda: _populated("fsi"), None, _ORDER["fsi"]),
    "tr": (
        "tr",
        lambda: ScheduleTrComputeResult(rows=[_tr()]),
        None,
        ["stage", "write", "row 0", "write", "entry 0", "write x17", "publish"],
    ),
    "tr/row-refusal-before-the-block": (
        "tr",
        lambda: ScheduleTrComputeResult(rows=[_tr(taxpayer_id_number="+ZQ")]),
        "WriterContractError: row 1 cell at index 1 starts with formula-trigger character '+'",
        ["stage", "write", "row 0"],
    ),
}


@pytest.mark.parametrize("case", _QUICKO_ORDER)
def test_only_tr_reads_the_quicko_block_and_only_after_its_last_row(
    tmp_path: Path, events: list[str], case: str
) -> None:
    artifact, make_payload, message, order = _QUICKO_ORDER[case]

    log, failure = _run_logged(
        tmp_path, events, artifact, make_payload(), _logging_quicko_context(events)
    )

    assert (None if failure is None else str(failure)) == message
    assert _runs(log) == order


_REFUSED_FORM67 = (
    "InvariantError: a REFUSED Form 67 must be refused before any income output is chosen"
)


def _bundle_form67(readiness: Form67Readiness) -> Form67ComputeResult:
    if readiness is Form67Readiness.READY:
        return Form67ComputeResult(entries=(_form67_entry(),))
    # Indian tax payable of ₹0, so a zero credit (#601). Until #1491 this was
    # Indian tax payable without a credit, an INCOMPLETE row no compute builds.
    refused = _form67_entry(
        tax_payable_normal_inr=Decimal("0"),
        section_90_credit_inr=Decimal("0"),
        total_credit_inr=Decimal("0"),
    )
    return Form67ComputeResult(entries=(refused,))


_GB_OVERWRITTEN = _fsi(
    cbdt_country_code="44", country_name_portal=_GB, iso_alpha2="GB", tax_relief_inr=Decimal("1000")
)

# The income bundle checks FSI's relief, then TR's, then chooses its files, all
# before staging the first one, Schedule OS's. Each case's refusal would come
# later, or not at all, if its check moved (#1204).
_BUNDLE_MIXED: dict[
    str, tuple[list[ScheduleFsiRow], ScheduleTrComputeResult, Form67Readiness, str]
] = {
    "fsi-relief-before-tr-relief-and-the-selection": (
        [_fsi(tax_relief_inr=Decimal("1000"))],
        ScheduleTrComputeResult(rows=[_tr()], fsi_rows_with_overwritten_relief=(_GB_OVERWRITTEN,)),
        Form67Readiness.REFUSED,
        PORTAL_RELIEF_REFUSAL,
    ),
    "tr-relief-before-the-selection": (
        [_fsi()],
        ScheduleTrComputeResult(rows=[_tr()], fsi_rows_with_overwritten_relief=(_GB_OVERWRITTEN,)),
        Form67Readiness.REFUSED,
        _GB_RELIEF_REFUSAL,
    ),
    "tr-relief-before-an-fsi-cell-refusal": (
        [_fsi(taxpayer_id_number="ZQ\x00")],
        ScheduleTrComputeResult(rows=[_tr()], fsi_rows_with_overwritten_relief=(_GB_OVERWRITTEN,)),
        Form67Readiness.READY,
        _GB_RELIEF_REFUSAL,
    ),
    "selection-before-any-staging": (
        [_fsi()],
        ScheduleTrComputeResult(rows=[_tr()]),
        Form67Readiness.REFUSED,
        _REFUSED_FORM67,
    ),
}


@pytest.mark.parametrize("case", _BUNDLE_MIXED)
def test_the_income_bundle_checks_fsi_then_tr_relief_before_choosing_or_staging(
    tmp_path: Path, events: list[str], case: str
) -> None:
    fsi_rows, tr, readiness, message = _BUNDLE_MIXED[case]
    form67 = _bundle_form67(readiness)
    assert form67.readiness is readiness
    fsi = _logged("fsi", ScheduleFsiComputeResult(rows=fsi_rows), events)
    events.clear()

    with pytest.raises(InvariantError) as raised:
        write_income_outputs(
            schedule_os=_hand_built_result(),
            fsi=fsi,
            tr=tr,
            form67=form67,
            output_dir=tmp_path,
            context=_ARTIFACTS["os-csv"].context,
            debug=True,
        )

    assert str(raised.value) == message
    assert events == ["row 0"]  # FSI's relief check, and nothing staged
    assert _names(tmp_path) == []


# ---- Schedule CG filing workbook through the shared renderer (#1205) --------------

_UNKNOWN_AY = max(_SNAPSHOTS_BY_AY_START) + 2
_CG_BLOCKED = (
    f"InvariantError: Schedule CG filing is blocked: {FILING_LTCG_KIND} (context keys: reasons)"
)
_CG_CONTEXT = (
    "InvariantError: the Schedule CG filing workbook needs the run's ITR form and source "
    "label (context keys: itr_form, source_label)"
)
_CG_UNKNOWN_AY = str(
    UnknownAssessmentYearError(
        assessment_year_start=_UNKNOWN_AY,
        earliest_registered=min(_SNAPSHOTS_BY_AY_START),
        latest_registered=max(_SNAPSHOTS_BY_AY_START),
    )
)


def _cg_context(
    *,
    assessment_year_start: int = _AY,
    source_label: str | None = "Robinhood",
    account_labels: dict[int, str] | None = ACCOUNT_LABELS,
) -> RenderContext:
    return RenderContext(assessment_year_start, "ITR-2", source_label, account_labels)


# The refusal that wins at each boundary, with the later one alone as a control
# where _REFUSALS has none: building the writer checks the run's context, then
# resolves the AY's labels; writing checks the blockers, then projects the
# sections.
_CG_ORDER = {
    "context-before-the-ay": _Refusal(
        "cg-xlsx",
        _cg_filing,
        InvariantError,
        _CG_CONTEXT,
        staged=False,
        context=lambda: _cg_context(assessment_year_start=_UNKNOWN_AY, source_label=None),
    ),
    "ay-alone": _Refusal(
        "cg-xlsx",
        _cg_filing,
        UnknownAssessmentYearError,
        _CG_UNKNOWN_AY,
        staged=False,
        context=lambda: _cg_context(assessment_year_start=_UNKNOWN_AY),
    ),
    "ay-before-the-blockers": _Refusal(
        "cg-xlsx",
        _blocked_cg_filing,
        UnknownAssessmentYearError,
        _CG_UNKNOWN_AY,
        staged=False,
        context=lambda: _cg_context(assessment_year_start=_UNKNOWN_AY),
    ),
    "blockers-before-a-sheet-check": _Refusal(
        "cg-xlsx",
        _blocked_cg_filing,
        InvariantError,
        _CG_BLOCKED,
        staged=False,
        context=lambda: _cg_context(source_label="=ZQ"),
    ),
}


@pytest.mark.parametrize("case", _CG_ORDER)
def test_cg_workbook_refusal_order(tmp_path: Path, stagings: list[str], case: str) -> None:
    refusal = _CG_ORDER[case]
    path = tmp_path / _ARTIFACTS["cg-xlsx"].filename
    path.write_bytes(_SENTINEL)
    payload, context = refusal.payload(), None if refusal.context is None else refusal.context()

    with pytest.raises(refusal.error) as raised:
        _write("cg-xlsx", payload, path, context)

    assert type(raised.value) is refusal.error
    assert str(raised.value) == refusal.message
    assert len(stagings) == refusal.staged
    assert path.read_bytes() == _SENTINEL
    assert _names(tmp_path) == [path.name]


def test_cg_workbook_reads_no_account_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workbook names no account (#1383), so a run with no account labels
    writes the baseline bytes."""
    fix_xlsx_clock(monkeypatch)
    path = tmp_path / _ARTIFACTS["cg-xlsx"].filename

    _write("cg-xlsx", _cg_filing(), path, _cg_context(account_labels=None))

    assert_workbook_matches(
        path.read_bytes(), expected("schedule-cg-filing-xlsx", "both-sections/ITR-2/default")
    )
