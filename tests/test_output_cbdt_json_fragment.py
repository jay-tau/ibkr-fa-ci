"""Tests for the CBDT Schedule FA JSON fragment writer (#39, PR3).

The writer is all-or-nothing: it emits a COMPLETE, schema-valid fragment or
REFUSES with an exact list of what to supply. These tests pin both the golden
emission (validated against the vendored schema) and every refusal path, since
the JSON import bypasses the human review step and thus files data directly.
"""

from __future__ import annotations

import errno
import itertools
import json
import os
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.models.fa_a2_row import (
    FaA2ClosingBlankReason,
    FaA2NatureOfAmount,
    FaA2Row,
)
from ibkr_fa.models.fa_a3_row import FaA3PeakBlankReason, FaA3Row
from ibkr_fa.output._cleanup import (
    ClaimedOutputCleanup,
    FileIdentity,
    OutputRemovalError,
    OutputWriteError,
)
from ibkr_fa.output.cbdt_json_fragment import (
    CBDT_JSON_FRAGMENT_FORMAT_ID,
    CbdtA2FragmentInput,
    CbdtA3Remedies,
    CbdtJsonIncompleteError,
    CbdtJsonStructuralError,
    CbdtJsonUnreadableError,
    CbdtScheduleFaFragmentInput,
    CbdtScheduleFaJsonWriter,
    build_cbdt_schedule_fa_fragment,
    check_cbdt_schedule_fa_fragment,
    is_ibkr_fa_cbdt_fragment,
    preclear_own_cbdt_fragment,
    refuse_failed_fragment_write,
)
from ibkr_fa.output.cbdt_json_schema import (
    A2_DEFINITION,
    A3_DEFINITION,
    CBDT_SCHEMA_VERSION,
    CbdtSchemaError,
    schema_version_for,
)
from tests._cbdt_fragment_inputs import FRAGMENT_A2_REMEDIES as _A2_REMEDIES
from tests._cbdt_fragment_inputs import FRAGMENT_A3_REMEDIES as _A3_REMEDIES
from tests._cbdt_fragment_inputs import fail_staging_fstat
from tests._cbdt_fragment_inputs import fragment_a2_row as _a2
from tests._cbdt_fragment_inputs import fragment_a3_row as _a3
from tests._cbdt_fragment_inputs import fragment_input as _input
from tests._cbdt_fragment_inputs import unchecked_row as _unchecked
from tests._cbdt_fragment_inputs import with_attested_peak as _with_peak
from tests._crash_reports import assert_replaced_unchained
from tests._deep_json import DEEP_JSON
from tests._fa_blank_texts import A2_CLOSING_BLANK_TEXT, A3_PEAK_BLANK_TEXT
from tests._no_echo import HOLDER as _HOLDER
from tests._no_echo import STRERROR as _STRERROR

# Synthetic privacy markers (#1048): no refusal may print them.
_ISIN = "US99971J1050"
_ACQUIRED = date(2025, 3, 1)
_AMOUNT = "1234567"


def _marker_os_error() -> PermissionError:
    """An ``EACCES`` error whose text and file name are markers (#1048)."""
    return PermissionError(errno.EACCES, _STRERROR, f"{_HOLDER}/x")


def _assert_absent(message: str, *values: str) -> None:
    """None of ``values`` is in ``message``, compared case-insensitively (#1048).

    Unsupplied markers guard future echoes; their absence alone does not
    demonstrate a change from base.
    """
    for value in values:
        assert value.lower() not in message.lower(), value


def _synthetic_lot(**overrides: Any) -> FaA3Row:
    """A lot of a synthetic holding whose ISIN, acquisition date, entity name
    and amounts are privacy markers (#1048); override any field via kwargs."""
    fields: dict[str, Any] = {
        "name_of_entity": _HOLDER,
        "source_isin": _ISIN,
        "source_ticker": "SYN",
        "date_of_acquiring": _ACQUIRED,
        "audit_acquisition_date": _ACQUIRED,
        "initial_value_inr": Decimal(_AMOUNT),
        "closing_value_inr": Decimal(_AMOUNT),
    }
    fields.update(overrides)
    return _a3(**fields)


_BOTH_ENTRY_POINTS = pytest.mark.parametrize(
    "run",
    [build_cbdt_schedule_fa_fragment, check_cbdt_schedule_fa_fragment],
    ids=["build", "check"],
)


# ---- golden emission ------------------------------------------------------


def test_format_id_constant() -> None:
    assert CBDT_JSON_FRAGMENT_FORMAT_ID == "cbdt-json-fragment"


def test_build_emits_both_tables_schema_valid() -> None:
    frag = build_cbdt_schedule_fa_fragment(_input())
    assert set(frag) == {"_ibkrFa", "ScheduleFA"}
    sched = frag["ScheduleFA"]
    assert set(sched) == {A2_DEFINITION, A3_DEFINITION}
    assert len(sched[A2_DEFINITION]) == 1
    assert len(sched[A3_DEFINITION]) == 1


def test_build_emits_multiple_independently_bound_a2_rows() -> None:
    second = _a2(
        account_number="U7654321",
        account_opening_date=date(2025, 1, 1),
        closing_balance_inr=Decimal("4000"),
        audit_closing_balance_base_ccy=Decimal("50"),
        institution_name="Second Foreign Custodian",
    )
    value = _input(
        a2=(
            CbdtA2FragmentInput(row=_a2(), account_id_cross_checked=True),
            CbdtA2FragmentInput(row=second, account_id_cross_checked=True),
        )
    )
    rows = build_cbdt_schedule_fa_fragment(value)["ScheduleFA"][A2_DEFINITION]
    assert [row["AccountNumber"] for row in rows] == ["U1234567", "U7654321"]


def test_build_emits_same_a2_account_once_per_nature() -> None:
    dividend = _a2()
    interest = _a2(
        nature_of_amount=FaA2NatureOfAmount.INTEREST,
        gross_amount_inr=Decimal("500"),
    )
    value = _input(
        a2=tuple(
            CbdtA2FragmentInput(row=row, account_id_cross_checked=True)
            for row in (dividend, interest)
        )
    )

    rows = build_cbdt_schedule_fa_fragment(value)["ScheduleFA"][A2_DEFINITION]

    assert [row["AccountNumber"] for row in rows] == ["U1234567", "U1234567"]
    assert [row["NatureOfAmount"] for row in rows] == ["D", "I"]


def test_build_refuses_inconsistent_repeated_a2_account() -> None:
    dividend = _a2()
    interest = _a2(
        nature_of_amount=FaA2NatureOfAmount.INTEREST,
        gross_amount_inr=Decimal("500"),
        closing_balance_inr=Decimal("1"),
    )
    value = _input(
        a2=tuple(
            CbdtA2FragmentInput(row=row, account_id_cross_checked=True)
            for row in (dividend, interest)
        )
    )

    with pytest.raises(CbdtJsonStructuralError, match="ClosingBalance"):
        build_cbdt_schedule_fa_fragment(value)


def test_provenance_marker_stamped() -> None:
    frag = build_cbdt_schedule_fa_fragment(_input())
    assert frag["_ibkrFa"]["format"] == CBDT_JSON_FRAGMENT_FORMAT_ID
    assert frag["_ibkrFa"]["itrForm"] == "ITR-2"
    assert frag["_ibkrFa"]["schemaVersion"] == CBDT_SCHEMA_VERSION


def test_itr3_fragment_is_form_bound_and_schema_valid() -> None:
    frag = build_cbdt_schedule_fa_fragment(_input(itr_form="ITR-3"))
    assert frag["_ibkrFa"] == {
        "format": CBDT_JSON_FRAGMENT_FORMAT_ID,
        "itrForm": "ITR-3",
        "schemaVersion": schema_version_for("ITR-3"),
    }


def test_is_ibkr_fa_cbdt_fragment_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "schedule-fa.json"
    CbdtScheduleFaJsonWriter().write(_input(), path)
    assert is_ibkr_fa_cbdt_fragment(path) is True


def test_is_ibkr_fa_cbdt_fragment_rejects_foreign(tmp_path: Path) -> None:
    # A bare ScheduleFA shape WITHOUT our marker is not "ours".
    bare = tmp_path / "bare.json"
    bare.write_text('{"ScheduleFA": {"DtlsForeignCustodialAcc": []}}', encoding="utf-8")
    assert is_ibkr_fa_cbdt_fragment(bare) is False
    # A JSON non-object and a missing file are not-ours.
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2, 3]", encoding="utf-8")
    assert is_ibkr_fa_cbdt_fragment(arr) is False
    assert is_ibkr_fa_cbdt_fragment(tmp_path / "nope.json") is False


def _unreadable(path: Path, kind: str) -> str:
    """:class:`CbdtJsonUnreadableError`'s text for ``path`` and the failure
    ``kind``: an ``OSError``'s class and errno name, else the class name alone
    (#1048)."""
    return (
        f"could not inspect {path} to tell whether ibkr-fa wrote it ({kind}); it was left "
        "untouched. Fix, move or remove it, then re-run."
    )


@pytest.mark.parametrize(
    ("make", "cause", "kind"),
    [
        (
            lambda path: path.write_text(f'{{"{_HOLDER}": not json}}', encoding="utf-8"),
            ValueError,
            "JSONDecodeError",
        ),
        (
            lambda path: path.write_bytes(b'{"_ibkrFa": "\xff"}'),
            UnicodeDecodeError,
            "UnicodeDecodeError",
        ),
        (
            lambda path: path.write_text(DEEP_JSON.decode(), encoding="utf-8"),
            RecursionError,
            "RecursionError",
        ),
        (lambda path: path.mkdir(), IsADirectoryError, "IsADirectoryError: EISDIR"),
        (lambda path: path.symlink_to(path.name), OSError, "OSError: ELOOP"),
        (
            lambda path: path.symlink_to("missing.json"),
            FileNotFoundError,
            "FileNotFoundError: ENOENT",
        ),
    ],
    ids=["not-json", "not-utf8", "too-deep", "directory", "looping-link", "dangling-link"],
)
def test_is_ibkr_fa_cbdt_fragment_refuses_a_file_it_cannot_inspect(
    tmp_path: Path, make: Any, cause: type[BaseException], kind: str
) -> None:
    # Unreadable is neither ours nor proven foreign, so the caller must refuse
    # rather than keep or remove it. The refusal names the failure by its class
    # (and errno name), never its text. The invalid-JSON marker was already absent
    # on base; that check guards future excerpts, while the exact message detects the change.
    path = tmp_path / "schedule-fa.json"
    make(path)
    with pytest.raises(CbdtJsonUnreadableError) as raised:
        is_ibkr_fa_cbdt_fragment(path)
    assert isinstance(raised.value.__cause__, cause)
    assert str(raised.value) == _unreadable(path, kind)
    _assert_absent(str(raised.value), _HOLDER)


@pytest.mark.parametrize("method", ["lstat", "open"])
@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (_marker_os_error, "PermissionError: EACCES"),
        (lambda: OSError(_STRERROR), "OSError: unknown error"),
        (lambda: OSError(99999, _STRERROR), "OSError: unknown error"),
    ],
    ids=["eacces", "no-errno", "unmapped-errno"],
)
def test_is_ibkr_fa_cbdt_fragment_names_an_os_error_by_its_errno_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    failure: Callable[[], OSError],
    kind: str,
) -> None:
    """#1048: never the error's text or file name."""
    path = tmp_path / "schedule-fa.json"
    path.write_text("{}", encoding="utf-8")
    error = failure()
    original = getattr(Path, method)

    def fail(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == path:
            raise error
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, fail)
    with pytest.raises(CbdtJsonUnreadableError) as raised:
        is_ibkr_fa_cbdt_fragment(path)
    assert raised.value.__cause__ is error
    assert str(raised.value) == _unreadable(path, kind)
    _assert_absent(str(raised.value), _STRERROR, _HOLDER)


def test_a2_item_mapping() -> None:
    a2 = build_cbdt_schedule_fa_fragment(_input())["ScheduleFA"][A2_DEFINITION][0]
    assert a2 == {
        "CountryName": "United States",
        "CountryCodeExcludingIndia": "2",
        "FinancialInstName": "Interactive Brokers LLC",
        "FinancialInstAddress": "One Pickwick Plaza, Greenwich, CT",
        "ZipCode": "06830",
        "AccountNumber": "U1234567",
        "Status": "BENEFICIAL_OWNER",
        "AccOpenDate": "2024-02-01",
        "PeakBalanceDuringPeriod": 8950,  # from config
        "ClosingBalance": 8950,  # 8950.40 -> 8950 HALF_UP
        "GrossAmtPaidCredited": 1235,  # 1234.50 -> 1235 HALF_UP
        "NatureOfAmount": "D",
    }


def test_a3_item_mapping() -> None:
    a3 = build_cbdt_schedule_fa_fragment(_input())["ScheduleFA"][A3_DEFINITION][0]
    assert a3 == {
        "CountryName": "United States",
        "CountryCodeExcludingIndia": "2",
        "NameOfEntity": "SAMPLE ISSUER AA INC",
        "AddressOfEntity": "100 Example Plaza, Sampleville, CA",
        "ZipCode": "99990",
        "NatureOfEntity": "Listed Company",
        "InterestAcquiringDate": "2024-03-15",
        "InitialValOfInvstmnt": 73372,  # 73372.30 -> 73372
        "PeakBalanceDuringPeriod": 85001,  # 85000.70 -> 85001
        "ClosingBalance": 83000,  # 83000.20 -> 83000
        "TotGrossAmtPaidCredited": 501,  # 500.50 -> 501 HALF_UP
        "TotGrossProceeds": 0,
    }


@pytest.mark.parametrize(
    ("nature", "letter"),
    [
        (FaA2NatureOfAmount.INTEREST, "I"),
        (FaA2NatureOfAmount.DIVIDEND, "D"),
        (FaA2NatureOfAmount.PROCEEDS, "S"),
        (FaA2NatureOfAmount.OTHER_INCOME, "O"),
        (FaA2NatureOfAmount.NO_AMOUNT, "N"),
    ],
)
def test_a2_nature_of_amount_maps_to_single_letter(nature: FaA2NatureOfAmount, letter: str) -> None:
    row = _a2(nature_of_amount=nature, gross_amount_inr=Decimal("0"))
    a2 = build_cbdt_schedule_fa_fragment(_input(a2_row=row))["ScheduleFA"][A2_DEFINITION][0]
    assert a2["NatureOfAmount"] == letter


@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
@pytest.mark.parametrize("zero", ["0", "-0", "0.00", "-0E+3"])
def test_no_credit_a2_row_files_n_and_zero(itr_form: str, zero: str) -> None:
    """A complete no-credit row files ``N`` with a zero gross amount (#1188)."""
    row = _a2(nature_of_amount=FaA2NatureOfAmount.NO_AMOUNT, gross_amount_inr=Decimal(zero))
    fragment = build_cbdt_schedule_fa_fragment(_input(a2_row=row, itr_form=itr_form))
    [a2] = fragment["ScheduleFA"][A2_DEFINITION]
    assert (a2["NatureOfAmount"], a2["GrossAmtPaidCredited"]) == ("N", 0)
    assert type(a2["GrossAmtPaidCredited"]) is int


def test_negative_closing_balance_rounds_away_from_zero() -> None:
    # A margined account can carry a negative closing NAV; HALF_UP rounds a tie
    # away from zero (-0.5 -> -1) and the schema permits a negative integer.
    row = _a2(
        closing_balance_inr=Decimal("-0.50"),
        audit_closing_balance_base_ccy=Decimal("-0.01"),
        audit_ttbr_dec31=Decimal("89.50"),
    )
    a2 = build_cbdt_schedule_fa_fragment(_input(a2_row=row))["ScheduleFA"][A2_DEFINITION][0]
    assert a2["ClosingBalance"] == -1


def test_negative_a2_peak_balance_emitted() -> None:
    # A margined account can run negative; the peak (-1500) must still be >= the
    # closing (-2000), since the peak is the highest single-day balance.
    a2 = build_cbdt_schedule_fa_fragment(
        _input(
            a2_peak_balance_inr=Decimal("-1500"), a2_row=_a2(closing_balance_inr=Decimal("-2000"))
        )
    )["ScheduleFA"][A2_DEFINITION][0]
    assert a2["PeakBalanceDuringPeriod"] == -1500


def test_json_does_not_compare_peak_and_closing_inr() -> None:
    # A2 selects the native-currency peak first, then applies that day's TTBR.
    # Different peak/closing TTBRs mean the resulting INR figures need not order.
    a2 = build_cbdt_schedule_fa_fragment(_input(a2_peak_balance_inr=Decimal("8949")))["ScheduleFA"][
        A2_DEFINITION
    ][0]
    assert a2["PeakBalanceDuringPeriod"] == 8949


def test_peak_equal_to_rounded_closing_accepted() -> None:
    # closing 8950.40 files as 8950; a peak of exactly 8950 (== filed closing) is
    # valid and must NOT false-refuse on the sub-rupee fraction.
    a2 = build_cbdt_schedule_fa_fragment(_input(a2_peak_balance_inr=Decimal("8950")))["ScheduleFA"][
        A2_DEFINITION
    ][0]
    assert (a2["PeakBalanceDuringPeriod"], a2["ClosingBalance"]) == (8950, 8950)


def test_a2_peak_is_the_row_peak_rounded_half_up() -> None:
    # HALF_UP files 8948.5 as 8949; HALF_EVEN would file 8948.
    a2 = build_cbdt_schedule_fa_fragment(_input(a2_peak_balance_inr=Decimal("8948.5")))[
        "ScheduleFA"
    ][A2_DEFINITION][0]
    assert a2["PeakBalanceDuringPeriod"] == 8949


def test_empty_a3_is_valid() -> None:
    # A cash-only custodial account has an A2 row but no equity holdings.
    frag = build_cbdt_schedule_fa_fragment(_input(a3_rows=()))
    assert frag["ScheduleFA"][A3_DEFINITION] == []


def test_multiple_lots_same_isin_emit_separate_items() -> None:
    rows = (
        _a3(date_of_acquiring=date(2024, 3, 15), audit_acquisition_date=date(2024, 3, 15)),
        _a3(date_of_acquiring=date(2024, 6, 1), audit_acquisition_date=date(2024, 6, 1)),
    )
    items = build_cbdt_schedule_fa_fragment(_input(a3_rows=rows))["ScheduleFA"][A3_DEFINITION]
    assert len(items) == 2
    assert {i["InterestAcquiringDate"] for i in items} == {"2024-03-15", "2024-06-01"}


# ---- atomic write ---------------------------------------------------------


def test_write_produces_parseable_json(tmp_path: Path) -> None:
    path = tmp_path / "schedule-fa.json"
    CbdtScheduleFaJsonWriter().write(_input(), path)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == build_cbdt_schedule_fa_fragment(_input())
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_write_overwrites_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "schedule-fa.json"
    path.write_text("STALE", encoding="utf-8")
    CbdtScheduleFaJsonWriter().write(_input(), path)
    assert json.loads(path.read_text(encoding="utf-8"))["ScheduleFA"]


def test_refused_write_does_not_create_file(tmp_path: Path) -> None:
    path = tmp_path / "schedule-fa.json"
    with pytest.raises(CbdtJsonIncompleteError):
        CbdtScheduleFaJsonWriter().write(_input(a2_peak_balance_inr=None), path)
    assert not path.exists()


def test_refused_write_leaves_no_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "schedule-fa.json"
    with pytest.raises(CbdtJsonStructuralError):
        CbdtScheduleFaJsonWriter().write(_input(account_id_cross_checked=False), path)
    assert list(tmp_path.iterdir()) == []


def test_write_cleans_up_temp_file_on_replace_failure(tmp_path: Path) -> None:
    # Target an existing directory: os.replace(temp_file, dir) raises OSError
    # after the temp file is created — the writer must unlink the temp file.
    target = tmp_path / "a-directory"
    target.mkdir()
    with pytest.raises(OSError):
        CbdtScheduleFaJsonWriter().write(_input(), target)
    assert list(tmp_path.iterdir()) == [target]


def test_write_reports_the_staged_file_before_publishing_it(tmp_path: Path) -> None:
    path = tmp_path / "schedule-fa.json"
    staged: list[tuple[FileIdentity, bool]] = []

    CbdtScheduleFaJsonWriter().write(
        _input(), path, on_staged=lambda identity: staged.append((identity, path.exists()))
    )

    info = os.lstat(path)
    assert staged == [((info.st_dev, info.st_ino), False)]


def _staging_files(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f".{path.name}.*.tmp"))


def _refuse_staged(
    path: Path, staging: list[Path], error: Exception
) -> Callable[[FileIdentity], None]:
    """An ``on_staged`` that records the staging file, then raises ``error``."""

    def refuse(_identity: FileIdentity) -> None:
        staging.extend(_staging_files(path))
        raise error

    return refuse


def test_write_publishes_nothing_when_on_staged_refuses(tmp_path: Path) -> None:
    path = tmp_path / "schedule-fa.json"
    path.write_bytes(b"EXISTING")
    error = RuntimeError("on_staged refused")
    staging: list[Path] = []

    with pytest.raises(RuntimeError) as raised:
        CbdtScheduleFaJsonWriter().write(
            _input(), path, on_staged=_refuse_staged(path, staging, error)
        )

    assert raised.value is error
    assert len(staging) == 1
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_bytes() == b"EXISTING"


def test_write_names_the_staging_pattern_it_cannot_remove_after_on_staged_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1048: the refusal names the staging file by its pattern beside the
    output path, never by its random name, and the error by its errno name."""
    path = tmp_path / "schedule-fa.json"
    path.write_bytes(b"EXISTING")
    error = RuntimeError("on_staged refused")
    staging: list[Path] = []
    unlink = Path.unlink

    def cannot_remove(target: Path, missing_ok: bool = False) -> None:
        if target in staging:
            raise _marker_os_error()
        unlink(target, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", cannot_remove)

    with pytest.raises(OutputRemovalError) as raised:
        CbdtScheduleFaJsonWriter().write(
            _input(), path, on_staged=_refuse_staged(path, staging, error)
        )

    assert len(staging) == 1
    assert str(raised.value) == (
        "the write failed, and its output may still be there: could not remove the "
        f"staging file {tmp_path / '.schedule-fa.json.*.tmp'} (EACCES)"
    )
    _assert_absent(str(raised.value), staging[0].name, _STRERROR, _HOLDER)
    assert_replaced_unchained(raised.value, RuntimeError, markers=(str(error), _STRERROR, _HOLDER))
    assert path.read_bytes() == b"EXISTING"


def test_write_refuses_when_the_staging_file_cannot_be_inspected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1048: the staging file by its pattern, the error by its errno name."""
    path = tmp_path / "schedule-fa.json"
    path.write_bytes(b"EXISTING")
    failure, staging = fail_staging_fstat(monkeypatch, path)
    claimed: list[FileIdentity] = []

    with pytest.raises(OutputWriteError) as raised:
        CbdtScheduleFaJsonWriter().write(_input(), path, on_staged=claimed.append)

    assert len(staging) == 1
    assert str(raised.value) == (
        f"could not write {path}: could not inspect its staging file "
        f"{tmp_path / '.schedule-fa.json.*.tmp'} (EIO)"
    )
    _assert_absent(str(raised.value), staging[0].name, _STRERROR, _HOLDER)
    assert raised.value.__cause__ is failure
    assert claimed == []
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_bytes() == b"EXISTING"


# ---- structural refusals (fail fast) --------------------------------------


def test_refuse_wrong_assessment_year() -> None:
    with pytest.raises(CbdtJsonStructuralError) as exc:
        build_cbdt_schedule_fa_fragment(_input(assessment_year_start=2027, calendar_year=2026))
    assert "schema is for AY 2026-27" in str(exc.value)


def test_refuse_assessment_year_not_calendar_year_plus_one() -> None:
    with pytest.raises(CbdtJsonStructuralError) as exc:
        build_cbdt_schedule_fa_fragment(_input(calendar_year=2024, assessment_year_start=2026))
    assert "calendar year" in str(exc.value)


def test_refuse_account_not_cross_checked() -> None:
    with pytest.raises(CbdtJsonStructuralError) as exc:
        build_cbdt_schedule_fa_fragment(_input(account_id_cross_checked=False))
    assert "cross-checked" in str(exc.value)


def test_refuse_missing_a2_row() -> None:
    with pytest.raises(CbdtJsonStructuralError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a2_row=None))
    assert "Table A2" in str(exc.value)


# ---- per-item completeness refusals ---------------------------------------


def test_refuse_missing_a2_peak() -> None:
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a2_peak_balance_inr=None))
    assert exc.value.reasons == ("A2 PeakBalanceDuringPeriod: supply the peak",)


def test_config_backed_gaps_use_the_caller_remedies() -> None:
    """Each A3 remedy gets the holding's ISIN; the gap names the holding by its
    first row, never by the ISIN (#1048)."""
    seen: list[tuple[str, str]] = []

    def remedy(name: str) -> Callable[[str], str]:
        def text(isin: str) -> str:
            seen.append((name, isin))
            return f"supply the entity {name}"

        return text

    remedies = CbdtA3Remedies(
        address=remedy("address"), zip_code=remedy("zip"), nature=remedy("nature")
    )
    row_a2 = _a2(institution_address=" ", institution_zip="")
    row_a3 = _a3(address_of_entity="", zip_code="", nature_of_entity="")
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(
            _input(a2_row=row_a2, a3_rows=(row_a3,), a3_remedies=remedies)
        )
    assert exc.value.reasons == (
        "A2 FinancialInstAddress: supply the address",
        "A2 ZipCode: supply the zip",
        "A3 holding in row 1 AddressOfEntity: supply the entity address",
        "A3 holding in row 1 ZipCode: supply the entity zip",
        "A3 holding in row 1 NatureOfEntity: supply the entity nature",
    )
    assert seen == [(name, "US99990A3095") for name in ("address", "zip", "nature")]
    _assert_absent(str(exc.value), "US99990A3095")


def test_refuse_blank_a2_zip() -> None:
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a2_row=_a2(institution_zip="")))
    assert "A2 ZipCode" in str(exc.value)


def test_refuse_whitespace_a2_zip() -> None:
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a2_row=_a2(institution_zip="   ")))
    assert "A2 ZipCode" in str(exc.value)


@pytest.mark.parametrize("reason", list(FaA2ClosingBlankReason))
def test_refuse_missing_a2_closing(reason: FaA2ClosingBlankReason) -> None:
    """#1013: the gap names the row's reason, from its fixed wording."""
    row = _a2(
        closing_balance_inr=None,
        audit_closing_balance_base_ccy=None,
        audit_ttbr_dec31=None,
        closing_blank_reason=reason,
    )
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a2_row=row))
    cause, remedy = A2_CLOSING_BLANK_TEXT[reason]
    assert (
        f"A2 ClosingBalance: the closing balance is blank because {cause}. {remedy} No "
        "config value can supply an A2 closing balance, so to enter it by hand use the "
        "review CSV or xlsx output instead"
    ) in exc.value.reasons


def test_refuse_undetermined_a2_nature() -> None:
    row = _a2(nature_of_amount=None, gross_amount_inr=None)
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a2_row=row))
    assert "NatureOfAmount" in str(exc.value)


def test_refuse_blank_a3_zip_names_the_holding_row() -> None:
    """#1048: the holding by its first row, not its ISIN."""
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a3_rows=(_a3(zip_code=""),)))
    assert exc.value.reasons == ("A3 holding in row 1 ZipCode: supply the entity zip",)
    _assert_absent(str(exc.value), "US99990A3095")


def test_refuse_missing_a3_peak() -> None:
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a3_rows=(_a3(peak_value_inr=None),)))
    assert "PeakBalanceDuringPeriod" in str(exc.value)


def test_refuse_missing_a3_gross_amount() -> None:
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a3_rows=(_a3(gross_amount_paid_credited_inr=None),)))
    assert "TotGrossAmtPaidCredited" in str(exc.value)


def test_refuse_missing_a3_proceeds() -> None:
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a3_rows=(_a3(gross_proceeds_from_sale_inr=None),)))
    assert "TotGrossProceeds" in str(exc.value)


def test_refusal_collects_all_gaps() -> None:
    row_a2 = _a2(institution_zip="")
    row_a3 = _a3(zip_code="")
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(
            _input(a2_row=row_a2, a2_peak_balance_inr=None, a3_rows=(row_a3,))
        )
    reasons = exc.value.reasons
    # A2 peak + A2 ZIP + A3 ZIP — all three surfaced in one refusal.
    assert reasons == (
        "A2 PeakBalanceDuringPeriod: supply the peak",
        "A2 ZipCode: supply the zip",
        "A3 holding in row 1 ZipCode: supply the entity zip",
    )


def test_repeated_isin_zip_gap_deduplicated() -> None:
    # Two lots of the same ISIN, both with a blank ZIP: the entity-level ZIP
    # gap must appear once, not once per lot.
    rows = (
        _a3(
            zip_code="",
            date_of_acquiring=date(2024, 3, 15),
            audit_acquisition_date=date(2024, 3, 15),
        ),
        _a3(
            zip_code="", date_of_acquiring=date(2024, 6, 1), audit_acquisition_date=date(2024, 6, 1)
        ),
    )
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a3_rows=rows))
    zip_reasons = [r for r in exc.value.reasons if "ZipCode" in r]
    assert zip_reasons == ["A3 holding in row 1 ZipCode: supply the entity zip"]


def test_gap_labels_name_rows_not_values() -> None:
    """#1048: a per-lot gap names its A3 row; an entity gap names the holding
    by its first row, once for all its lots. Neither shows the ISIN, the
    acquisition date, the entity name or an amount."""
    rows = (
        _synthetic_lot(zip_code="", peak_value_inr=None),
        _synthetic_lot(zip_code="", peak_value_inr=None),
        _a3(zip_code=""),
    )
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a3_rows=rows))
    cause, remedy = A3_PEAK_BLANK_TEXT[FaA3PeakBlankReason.PRICES_NOT_FETCHED]
    peak = (
        f"PeakBalanceDuringPeriod: the peak is blank because {cause}. {remedy} No config "
        "value can supply an A3 peak itself, so to enter it by hand use the review CSV or "
        "xlsx output instead"
    )
    assert exc.value.reasons == (
        f"A3 row 1 {peak}",
        "A3 holding in row 1 ZipCode: supply the entity zip",
        f"A3 row 2 {peak}",
        "A3 holding in row 3 ZipCode: supply the entity zip",
    )
    # ISIN/date checks detect the old echo; name/amount were already absent and guard future echoes.
    _assert_absent(str(exc.value), _ISIN, _ACQUIRED.isoformat(), _HOLDER, _AMOUNT, "US99990A3095")


# ---- date / type safety ---------------------------------------------------


def test_refuse_datetime_for_a2_open_date() -> None:
    # datetime is-a date; its isoformat carries a T-suffix the unanchored CBDT
    # pattern would match as a substring — the writer must refuse it.
    row = _unchecked(_a2(), account_opening_date=datetime(2024, 2, 1, 0, 0))
    with pytest.raises(CbdtJsonStructuralError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a2_row=row))
    assert "AccOpenDate" in str(exc.value)


@pytest.mark.parametrize(
    "day", [date(3000, 3, 1), date(999, 3, 1), date(1, 3, 1)], ids=["3000", "999", "1"]
)
@pytest.mark.parametrize(
    ("table", "label"),
    [("A2", "A2 AccOpenDate"), ("A3", "A3 row 1 InterestAcquiringDate")],
    ids=["a2", "a3"],
)
def test_refuse_date_year_outside_cbdt_pattern(day: date, table: str, label: str) -> None:
    # The CBDT date pattern only accepts years 1000-2999 (leading [12]); a real
    # date outside that range isoformats fine but must be refused, not emitted,
    # and the refusal never shows the date (#1048).
    if table == "A2":
        value = _input(a2_row=_a2(account_opening_date=day))
    else:
        value = _input(a3_rows=(_a3(date_of_acquiring=day, audit_acquisition_date=day),))
    with pytest.raises(CbdtJsonStructuralError) as exc:
        build_cbdt_schedule_fa_fragment(value)
    assert exc.value.reasons == (f"{label}: is not a CBDT-acceptable YYYY-MM-DD date",)
    _assert_absent(str(exc.value), day.isoformat())


# ---- authoritative schema gate (non-blank but invalid) --------------------


@_BOTH_ENTRY_POINTS
def test_schema_refusal_names_the_row_field_and_rule_not_the_value(
    run: Callable[[CbdtScheduleFaFragmentInput], object],
) -> None:
    """#1048: a too-long entity name in the SECOND A3 row names its 0-based
    index, the field and the schema keyword, never the name or the ISIN. The
    row skips its own length check (#1189), which would refuse it first."""
    name = " ".join([_HOLDER] * 7)  # 146 characters; the schema's maxLength is 125
    rows = (_a3(), _unchecked(_synthetic_lot(), name_of_entity=name))
    with pytest.raises(CbdtSchemaError) as exc:
        run(_input(a3_rows=rows))
    assert str(exc.value) == (
        "ITR-2 Schedule FA Table A3 (DtlsForeignEquityDebtInterest) item at index 1 failed "
        "CBDT schema validation: field 'NameOfEntity' breaks the schema's 'maxLength' rule"
    )
    assert (exc.value.row_index, exc.value.field, exc.value.keyword) == (
        1,
        "NameOfEntity",
        "maxLength",
    )
    # Base echoed the name; ISIN/date/amount were already absent and guard future echoes.
    _assert_absent(str(exc.value), _HOLDER, _ISIN, _ACQUIRED.isoformat(), _AMOUNT)


@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
def test_text_at_the_rows_maximum_lengths_is_filed_as_given(itr_form: str) -> None:
    """The rows' text limits are the schemas' own (#1189): text at them passes
    the schema gate and is filed raw, commas and whitespace runs included."""
    a2_text = {
        "CountryName": "C" * 55,
        "FinancialInstName": "N" * 125,
        "FinancialInstAddress": "A,  " * 50,
        "ZipCode": "Z" * 8,
        "AccountNumber": "U" * 34,
    }
    a3_text = {
        "CountryName": "C" * 55,
        "NameOfEntity": "N" * 125,
        "AddressOfEntity": "A,  " * 50,
        "ZipCode": "Z" * 8,
        "NatureOfEntity": "E" * 34,
    }
    a2_row = _a2(
        country_name=a2_text["CountryName"],
        institution_name=a2_text["FinancialInstName"],
        institution_address=a2_text["FinancialInstAddress"],
        institution_zip=a2_text["ZipCode"],
        account_number=a2_text["AccountNumber"],
    )
    a3_row = _a3(
        country_name=a3_text["CountryName"],
        name_of_entity=a3_text["NameOfEntity"],
        address_of_entity=a3_text["AddressOfEntity"],
        zip_code=a3_text["ZipCode"],
        nature_of_entity=a3_text["NatureOfEntity"],
    )
    fragment = build_cbdt_schedule_fa_fragment(
        _input(a2_row=a2_row, a3_rows=(a3_row,), itr_form=itr_form)
    )["ScheduleFA"]
    a2_item, a3_item = fragment[A2_DEFINITION][0], fragment[A3_DEFINITION][0]
    assert {field: a2_item[field] for field in a2_text} == a2_text
    assert {field: a3_item[field] for field in a3_text} == a3_text


def test_a3_allows_converted_peak_below_converted_closing() -> None:
    """Native peak and closing use different dates, so their INR values need not order."""
    a3 = build_cbdt_schedule_fa_fragment(
        _input(
            a3_rows=(_a3(peak_value_inr=Decimal("100.00"), closing_value_inr=Decimal("200.00")),)
        )
    )["ScheduleFA"][A3_DEFINITION][0]
    assert (a3["PeakBalanceDuringPeriod"], a3["ClosingBalance"]) == (100, 200)


def test_a3_peak_equal_to_closing_accepted() -> None:
    a3 = build_cbdt_schedule_fa_fragment(
        _input(
            a3_rows=(_a3(peak_value_inr=Decimal("500.00"), closing_value_inr=Decimal("500.00")),)
        )
    )["ScheduleFA"][A3_DEFINITION][0]
    assert a3["PeakBalanceDuringPeriod"] == a3["ClosingBalance"] == 500


# ---- structural, then schema, then missing cells (#773) --------------------


def _bound(*rows: FaA2Row) -> tuple[CbdtA2FragmentInput, ...]:
    return tuple(CbdtA2FragmentInput(row=row, account_id_cross_checked=True) for row in rows)


def _interest(**overrides: Any) -> FaA2Row:
    """The ``_a2()`` account's interest row."""
    return _a2(
        nature_of_amount=FaA2NatureOfAmount.INTEREST, gross_amount_inr=Decimal("500"), **overrides
    )


def _other_income(**overrides: Any) -> FaA2Row:
    """The ``_a2()`` account's other-income row."""
    return _a2(
        nature_of_amount=FaA2NatureOfAmount.OTHER_INCOME, gross_amount_inr=Decimal("0"), **overrides
    )


def _without_peaks(value: CbdtScheduleFaFragmentInput) -> CbdtScheduleFaFragmentInput:
    """``value`` with every A2 and A3 peak missing. The peaks are removed past
    the row checks, so a row built past them keeps its malformed field."""
    return replace(
        value,
        a2=tuple(
            replace(
                item,
                row=None
                if item.row is None
                else _unchecked(
                    item.row,
                    peak_balance_inr=None,
                    audit_peak_override_source=None,
                    audit_peak_override_reason=None,
                ),
            )
            for item in value.a2
        ),
        a3_rows=tuple(
            _unchecked(
                row,
                peak_value_inr=None,
                peak_blank_reason=FaA3PeakBlankReason.PRICES_NOT_FETCHED,
            )
            for row in value.a3_rows
        ),
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"itr_form": "ITR-1"}, "unsupported ITR form; expected ITR-2 or ITR-3"),
        ({"a2": ()}, "Table A2 (the foreign custodial account itself) is missing"),
        ({"a2": _bound(_a2(), _a2())}, "duplicate AccountNumber/NatureOfAmount pair"),
        ({"a2_row": _a2(account_number="   ")}, "blank AccountNumber"),
        (
            {"a2_row": _unchecked(_a2(), account_opening_date=datetime(2024, 2, 1))},
            "A2 AccOpenDate: must be a plain calendar date",
        ),
        (
            {"a3_rows": (_unchecked(_a3(), date_of_acquiring=datetime(2024, 3, 15)),)},
            "InterestAcquiringDate: must be a plain calendar date",
        ),
        (
            {"a3_rows": (_unchecked(_a3(), date_of_acquiring="2024-03-15"),)},
            "InterestAcquiringDate: must be a plain calendar date",
        ),
        (
            {"a2_row": _a2(account_opening_date=date(3000, 1, 1))},
            "A2 AccOpenDate: is not a CBDT-acceptable YYYY-MM-DD date",
        ),
        (
            {"a2_row": _a2(institution_name="Interactive Brokers\nLLC")},
            "A2 account 1 FinancialInstName: the value (not shown) has the control character "
            "'\\n' at position 20; ibkr-fa merge would refuse it",
        ),
        (
            {"a3_rows": (_unchecked(_a3(), zip_code="\t"),)},
            "A3 row 1 ZipCode: the value (not shown) has the control character '\\t' at "
            "position 1; ibkr-fa merge would refuse it",
        ),
    ],
    ids=[
        "unsupported-itr-form",
        "no-a2-account",
        "duplicate-account-nature",
        "whitespace-account-number",
        "a2-datetime",
        "a3-datetime",
        "a3-str-date",
        "date-outside-pattern",
        "a2-control-character",
        "a3-control-character-only-zip",
    ],
)
@pytest.mark.parametrize("missing_peaks", [False, True], ids=["alone", "with-missing-peaks"])
def test_structural_problem_refuses_as_structural(
    overrides: dict[str, Any], reason: str, missing_peaks: bool
) -> None:
    value = _input(**overrides)
    if missing_peaks:
        value = _without_peaks(value)

    with pytest.raises(CbdtJsonStructuralError) as exc:
        build_cbdt_schedule_fa_fragment(value)

    assert any(reason in item for item in exc.value.reasons), exc.value.reasons
    assert "PeakBalanceDuringPeriod" not in str(exc.value)


@pytest.mark.parametrize(
    ("second", "reason"),
    [
        (_interest(institution_zip=""), "A2 ZipCode: supply the zip"),
        (_with_peak(_interest(), None), "A2 PeakBalanceDuringPeriod: supply the peak"),
    ],
    ids=["missing-zip", "missing-peak"],
)
@pytest.mark.parametrize("reverse", [False, True], ids=["supplied-first", "missing-first"])
def test_missing_a2_cell_beside_a_supplied_one_is_not_a_disagreement(
    second: FaA2Row, reason: str, reverse: bool
) -> None:
    rows = (second, _a2()) if reverse else (_a2(), second)

    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a2=_bound(*rows)))

    # The missing cell is reported, never filled from the other row.
    assert exc.value.reasons == (reason,)


def test_two_unresolved_a2_natures_are_missing_cells_not_a_duplicate_pair() -> None:
    unresolved = _a2(nature_of_amount=None, gross_amount_inr=None)

    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a2=_bound(unresolved, unresolved)))

    assert exc.value.reasons == (
        "A2 GrossAmtPaidCredited / NatureOfAmount: source evidence could not resolve "
        "every account credit; verify the unresolved nature and amount",
    )


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_supplied_a2_values_that_disagree_beside_a_missing_one_refuse_as_structural(
    order: tuple[int, ...],
) -> None:
    rows = (
        _a2(institution_zip=""),
        _interest(institution_zip="10001"),
        _other_income(institution_zip="10002"),
    )

    with pytest.raises(CbdtJsonStructuralError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a2=_bound(*(rows[i] for i in order))))

    assert exc.value.reasons == (
        "Table A2 rows for one account disagree on account-level field(s): ZipCode",
    )


@pytest.mark.parametrize(
    "value",
    [
        _input(a2_row=_unchecked(_with_peak(_a2(), None), institution_zip="12345-6789")),
        _input(a3_rows=(_unchecked(_a3(peak_value_inr=None), zip_code="123456789"),)),
    ],
    ids=["a2-zip", "a3-zip"],
)
def test_invalid_supplied_value_beside_a_missing_one_fails_the_schema_gate(
    value: CbdtScheduleFaFragmentInput,
) -> None:
    """The ZIP skips the row's own length check (#1189), which would refuse it first."""
    with pytest.raises(CbdtSchemaError, match="ZipCode"):
        build_cbdt_schedule_fa_fragment(value)


def test_check_refuses_structural_and_schema_problems_but_not_missing_cells() -> None:
    check_cbdt_schedule_fa_fragment(_without_peaks(_input()))
    with pytest.raises(CbdtJsonStructuralError, match="cross-checked"):
        check_cbdt_schedule_fa_fragment(_input(account_id_cross_checked=False))
    with pytest.raises(CbdtSchemaError, match="ZipCode"):
        check_cbdt_schedule_fa_fragment(
            _input(a2_row=_unchecked(_a2(), institution_zip="12345-6789"))
        )


# ---- malformed input (#788) --------------------------------------------------


def _remedy_that_raises(isin: str) -> str:
    raise KeyError(isin)


class _UnprintableError(Exception):
    def __str__(self) -> str:
        raise ValueError("exception text unavailable")


def _remedy_with_unprintable_error(isin: str) -> str:
    raise _UnprintableError(isin)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, "the input must be a CbdtScheduleFaFragmentInput (got NoneType)"),
        ({}, "the input must be a CbdtScheduleFaFragmentInput (got dict)"),
        (_input(a2=(None,)), "a2 must be a tuple of CbdtA2FragmentInput"),
        (_input(a3_rows=None), "a3_rows must be a tuple of FaA3Row"),
        (_input(a3_rows=(None,)), "a3_rows must be a tuple of FaA3Row"),
        (
            _input(account_id_cross_checked="false"),
            "A2 account 1: account_id_cross_checked must be a bool (got str)",
        ),
        (_input(assessment_year_start="2026"), "assessment_year_start must be an int (got str)"),
        (_input(assessment_year_start=2026.0), "assessment_year_start must be an int (got float)"),
        (_input(calendar_year=True), "calendar_year must be an int (got bool)"),
        (
            _input(a2_remedies=replace(_A2_REMEDIES, peak=None)),
            "a2_remedies must be a CbdtA2Remedies of text",
        ),
        (
            _input(a3_remedies=replace(_A3_REMEDIES, address="supply the address")),
            "a3_remedies must be a CbdtA3Remedies of callables",
        ),
        (
            _input(a3_remedies=replace(_A3_REMEDIES, address=lambda: "supply the address")),
            "the A3 address remedy for the A3 holding in row 1 raised TypeError",
        ),
        (
            _input(a3_remedies=replace(_A3_REMEDIES, zip_code=_remedy_that_raises)),
            "the A3 zip_code remedy for the A3 holding in row 1 raised KeyError",
        ),
        (
            _input(a3_remedies=replace(_A3_REMEDIES, address=_remedy_with_unprintable_error)),
            "the A3 address remedy for the A3 holding in row 1 raised _UnprintableError",
        ),
        (
            _input(a3_remedies=replace(_A3_REMEDIES, nature=lambda isin: None)),
            "the A3 nature remedy for the A3 holding in row 1 returned NoneType, not text",
        ),
    ],
    ids=[
        "none",
        "dict",
        "none-a2-item",
        "none-a3-rows",
        "none-a3-row",
        "str-cross-checked",
        "str-ay",
        "float-ay",
        "bool-calendar-year",
        "none-a2-remedy",
        "non-callable-a3-remedy",
        "a3-remedy-without-argument",
        "a3-remedy-that-raises",
        "a3-remedy-with-unprintable-error",
        "a3-remedy-returning-none",
    ],
)
@pytest.mark.parametrize(
    "run",
    [build_cbdt_schedule_fa_fragment, check_cbdt_schedule_fa_fragment],
    ids=["build", "check"],
)
def test_malformed_input_refuses_as_structural(
    value: Any, reason: str, run: Callable[[CbdtScheduleFaFragmentInput], object]
) -> None:
    with pytest.raises(CbdtJsonStructuralError) as exc:
        run(value)
    assert any(reason in item for item in exc.value.reasons), exc.value.reasons
    # The remedy failures used to name the holding's ISIN (#1048).
    _assert_absent(str(exc.value), "US99990A3095")


def _zip_remedy_that_raises(isin: str) -> str:
    if isin == _ISIN:
        raise ValueError(_HOLDER)
    return "supply the entity zip"


def _nature_remedy_returning_an_amount(isin: str) -> Any:
    return int(_AMOUNT) if isin == _ISIN else "supply the entity nature"


@_BOTH_ENTRY_POINTS
@pytest.mark.parametrize(
    ("remedies", "reason"),
    [
        (
            replace(_A3_REMEDIES, zip_code=_zip_remedy_that_raises),
            "the A3 zip_code remedy for the A3 holding in row 2 raised ValueError",
        ),
        (
            replace(_A3_REMEDIES, nature=_nature_remedy_returning_an_amount),
            "the A3 nature remedy for the A3 holding in row 2 returned int, not text",
        ),
    ],
    ids=["raises", "returns-int"],
)
def test_a3_remedy_refusal_names_the_holding_row_not_the_isin(
    remedies: CbdtA3Remedies, reason: str, run: Callable[[CbdtScheduleFaFragmentInput], object]
) -> None:
    """#1048: a remedy that fails for the holding whose lots are rows 2 and 3
    names it by its first row, never by its ISIN or the remedy's error text."""
    rows = (_a3(), _synthetic_lot(), _synthetic_lot())
    with pytest.raises(CbdtJsonStructuralError) as exc:
        run(_input(a3_rows=rows, a3_remedies=remedies))
    assert str(exc.value) == (
        "Refusing to emit the CBDT Schedule FA JSON fragment — its inputs are structurally "
        f"invalid. Resolve the following and re-run:\n  - {reason}"
    )
    # Base echoed the ISIN; error text/amount/date were already absent and guard future echoes.
    _assert_absent(str(exc.value), _ISIN, _HOLDER, _AMOUNT, _ACQUIRED.isoformat())


# ---- control characters in filed text (#798) ---------------------------------

# Each filed text field `ibkr-fa merge` refuses a control character in: its
# table, its CBDT name and the row attribute it is filed from.
_TEXT_FIELDS = [
    ("A2", "CountryName", "country_name"),
    ("A2", "FinancialInstName", "institution_name"),
    ("A2", "FinancialInstAddress", "institution_address"),
    ("A2", "ZipCode", "institution_zip"),
    ("A2", "AccountNumber", "account_number"),
    ("A3", "CountryName", "country_name"),
    ("A3", "NameOfEntity", "name_of_entity"),
    ("A3", "AddressOfEntity", "address_of_entity"),
    ("A3", "ZipCode", "zip_code"),
    ("A3", "NatureOfEntity", "nature_of_entity"),
]

# The fields whose value a refusal used to show; the account number never was.
_SHOWN_TEXT_FIELDS = [entry for entry in _TEXT_FIELDS if entry[1] != "AccountNumber"]

_A3_ROW_1 = "A3 row 1"


def _control_character_reason(item: str, field: str, char: str, position: int) -> str:
    """The structural reason for a control character, which never shows the
    value (#1048)."""
    return (
        f"{item} {field}: the value (not shown) has the control character {char!r} at "
        f"position {position}; ibkr-fa merge would refuse it"
    )


def test_refuse_a2_institution_name_with_a_line_break() -> None:
    row = _a2(institution_name="Interactive Brokers\nLLC")
    with pytest.raises(CbdtJsonStructuralError) as exc:
        build_cbdt_schedule_fa_fragment(_input(a2_row=row))
    assert exc.value.reasons == (
        "A2 account 1 FinancialInstName: the value (not shown) has the control "
        "character '\\n' at position 20; ibkr-fa merge would refuse it",
    )
    _assert_absent(str(exc.value), "Interactive Brokers")


@_BOTH_ENTRY_POINTS
@pytest.mark.parametrize("char", ["\n", "\t", "\x00", "\x7f"], ids=["lf", "tab", "nul", "del"])
@pytest.mark.parametrize(
    ("table", "field", "attribute"), _TEXT_FIELDS, ids=[f"{t}-{f}" for t, f, _ in _TEXT_FIELDS]
)
def test_refuse_control_character_in_filed_text(
    table: str,
    field: str,
    attribute: str,
    char: str,
    run: Callable[[CbdtScheduleFaFragmentInput], object],
) -> None:
    clean = getattr(_a2() if table == "A2" else _a3(), attribute)
    value = f"{clean[:2]}{char}{clean[2:]}"
    if table == "A2":
        # FaA2Row refuses a control character in its account number itself (#854).
        bundle, item = _input(a2_row=_unchecked(_a2(), **{attribute: value})), "A2 account 1"
    else:
        # FaA3Row refuses one in its name, address, ZIP and nature itself (#1367).
        bundle, item = _input(a3_rows=(_unchecked(_a3(), **{attribute: value}),)), _A3_ROW_1

    with pytest.raises(CbdtJsonStructuralError) as exc:
        run(bundle)

    assert exc.value.reasons == (_control_character_reason(item, field, char, 3),)
    # AccountNumber was already hidden on base; that case pins unchanged behavior,
    # while the other fields detect the old echo (#1048).
    _assert_absent(str(exc.value), clean[2:])


@_BOTH_ENTRY_POINTS
@pytest.mark.parametrize(
    ("table", "field", "attribute"),
    _SHOWN_TEXT_FIELDS,
    ids=[f"{t}-{f}" for t, f, _ in _SHOWN_TEXT_FIELDS],
)
def test_control_character_refusal_never_shows_the_value(
    table: str,
    field: str,
    attribute: str,
    run: Callable[[CbdtScheduleFaFragmentInput], object],
) -> None:
    """#1048: the refusal names the row, the field, the character and its
    position, never the value. The rows skip their own checks, which refuse a
    ZIP this long first (#1189)."""
    value = f"{_HOLDER}\x07"
    if table == "A2":
        bundle, item = _input(a2_row=_unchecked(_a2(), **{attribute: value})), "A2 account 1"
    else:
        bundle, item = _input(a3_rows=(_unchecked(_a3(), **{attribute: value}),)), _A3_ROW_1

    with pytest.raises(CbdtJsonStructuralError) as exc:
        run(bundle)

    assert exc.value.reasons == (
        f"{item} {field}: the value (not shown) has the control character '\\x07' at "
        "position 21; ibkr-fa merge would refuse it",
    )
    _assert_absent(str(exc.value), _HOLDER)


@_BOTH_ENTRY_POINTS
@pytest.mark.parametrize(
    ("overrides", "item"),
    [
        ({"a2_row": _a2(institution_zip="\t")}, "A2 account 1"),
        ({"a3_rows": (_unchecked(_a3(), zip_code="\t"),)}, _A3_ROW_1),
    ],
    ids=["a2", "a3"],
)
def test_refuse_zip_of_only_a_control_character(
    overrides: dict[str, Any], item: str, run: Callable[[CbdtScheduleFaFragmentInput], object]
) -> None:
    # The check reads the supplied text, before a blank one becomes a missing cell.
    with pytest.raises(CbdtJsonStructuralError) as exc:
        run(_input(**overrides))
    assert exc.value.reasons == (_control_character_reason(item, "ZipCode", "\t", 1),)


@_BOTH_ENTRY_POINTS
def test_control_character_refusal_lists_every_reason_without_any_value(
    run: Callable[[CbdtScheduleFaFragmentInput], object],
) -> None:
    account = "U1234\x00567"
    a2 = _bound(
        _unchecked(_a2(country_name="United\tStates"), account_number=account),
        _a2(
            account_number="U7654321",
            institution_address="One Pickwick Plaza\nGreenwich, CT",
            institution_zip="06830\x7f",
        ),
    )
    a3_rows = (
        _unchecked(_a3(), name_of_entity="SAMPLE ISSUER AA\nINC"),
        _unchecked(
            _a3(
                source_isin="US99992A1097",
                source_ticker="XQAQ",
                name_of_entity="SAMPLE ISSUER AQ INC",
            ),
            address_of_entity="1 Issuer Way,\tSampletown, CA",
            nature_of_entity="Listed\x00Company",
        ),
    )

    with pytest.raises(CbdtJsonStructuralError) as exc:
        run(_input(a2=a2, a3_rows=a3_rows))

    assert exc.value.reasons == (
        _control_character_reason("A2 account 1", "CountryName", "\t", 7),
        _control_character_reason("A2 account 1", "AccountNumber", "\x00", 6),
        _control_character_reason("A2 account 2", "FinancialInstAddress", "\n", 19),
        _control_character_reason("A2 account 2", "ZipCode", "\x7f", 6),
        _control_character_reason("A3 row 1", "NameOfEntity", "\n", 17),
        _control_character_reason("A3 row 2", "AddressOfEntity", "\t", 14),
        _control_character_reason("A3 row 2", "NatureOfEntity", "\x00", 7),
    )
    for shown in (account, repr(account), "U1234"):
        assert shown not in str(exc.value)
    # #1048: nor any other value, nor the A3 rows' ISINs or acquisition date.
    _assert_absent(
        str(exc.value),
        "States",
        "Pickwick",
        "06830",
        "SAMPLE ISSUER",
        "Sampletown",
        "Company",
        "US99990A3095",
        "US99992A1097",
        "2024-03-15",
    )


# --- preclear_own_cbdt_fragment (#644) -----------------------------------------


@pytest.mark.parametrize(
    ("content", "vacant", "left"),
    [
        (None, True, False),
        (json.dumps({"_ibkrFa": {"format": CBDT_JSON_FRAGMENT_FORMAT_ID}}), True, False),
        (json.dumps({"ScheduleFA": {}}), False, True),
    ],
    ids=["nothing-there", "ours-removed", "foreign-kept"],
)
def test_preclear_own_cbdt_fragment(
    tmp_path: Path, content: str | None, vacant: bool, left: bool
) -> None:
    path = tmp_path / "schedule-fa.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert preclear_own_cbdt_fragment(path) is vacant
    assert path.exists() is left


def test_preclear_own_cbdt_fragment_refuses_an_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "schedule-fa.json"
    path.write_bytes(b"\xff not json")
    with pytest.raises(CbdtJsonUnreadableError, match="could not inspect"):
        preclear_own_cbdt_fragment(path)
    assert path.read_bytes() == b"\xff not json"


def test_preclear_own_cbdt_fragment_names_one_it_cannot_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1048: the path and the errno name, never the error's text or file name."""
    path = tmp_path / "schedule-fa.json"
    path.write_text(json.dumps({"_ibkrFa": {"format": CBDT_JSON_FRAGMENT_FORMAT_ID}}), "utf-8")
    error = _marker_os_error()

    def cannot_remove(_target: Path, missing_ok: bool = False) -> None:
        raise error

    monkeypatch.setattr(Path, "unlink", cannot_remove)
    with pytest.raises(OutputRemovalError) as exc:
        preclear_own_cbdt_fragment(path)
    assert str(exc.value) == f"could not remove the previous output {path} (EACCES)"
    _assert_absent(str(exc.value), _STRERROR, _HOLDER)
    assert exc.value.__cause__ is error
    assert path.exists()


# --- refuse_failed_fragment_write (#1048) --------------------------------------


class _RefusedError(IbkrFaError):
    """The caller's refusal for :func:`refuse_failed_fragment_write`."""


@pytest.mark.parametrize(
    ("failure", "detail"),
    [
        (_marker_os_error, "PermissionError: EACCES"),
        (lambda: OSError(_STRERROR), "OSError: unknown error"),
        (lambda: OSError(99999, _STRERROR), "OSError: unknown error"),
        (lambda: RuntimeError(_HOLDER), "RuntimeError"),
        (lambda: CbdtJsonIncompleteError([f"A3 {_ISIN} {_HOLDER}"]), "CbdtJsonIncompleteError"),
        # An output-layer error's text is fixed text and allowed paths, so it is
        # relayed whole, as before #1048: this case pins that branch, not a change.
        (
            lambda: OutputWriteError("could not write the output (EIO)"),
            "OutputWriteError: could not write the output (EIO)",
        ),
    ],
    ids=["eacces", "no-errno", "unmapped-errno", "runtime-error", "domain-error", "write-error"],
)
def test_refuse_failed_fragment_write_after_staging_names_the_failure_kind_only(
    tmp_path: Path, failure: Callable[[], Exception], detail: str
) -> None:
    """Once staged, any failure refuses. It is named by its class (an
    ``OSError`` with its errno name; an output-layer error, whose text is fixed
    and allowed paths, in full); other failures expose no message text."""
    path = tmp_path / "schedule-fa.json"
    cleanup = ClaimedOutputCleanup(path)
    cleanup.claim((1, 2))
    error = failure()

    with pytest.raises(_RefusedError) as raised:
        refuse_failed_fragment_write(path, cleanup, error, _RefusedError)

    assert str(raised.value) == (
        f"the Schedule FA JSON fragment {path} failed after it was staged or written: {detail}"
    )
    assert raised.value.__cause__ is error
    _assert_absent(str(raised.value), _STRERROR, _HOLDER, _ISIN)


def test_refuse_failed_fragment_write_before_staging_names_a_non_domain_failure_kind_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schedule-fa.json"
    error = RuntimeError(_HOLDER)

    with pytest.raises(_RefusedError) as raised:
        refuse_failed_fragment_write(path, ClaimedOutputCleanup(path), error, _RefusedError)

    assert (
        str(raised.value) == f"the Schedule FA JSON fragment {path} was not written: RuntimeError"
    )
    assert raised.value.__cause__ is error
    assert not path.exists()
    _assert_absent(str(raised.value), _HOLDER)


def test_refuse_failed_fragment_write_names_a_file_that_appeared_without_the_failure_text(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schedule-fa.json"
    path.write_bytes(b"NOT OURS")
    error = RuntimeError(_HOLDER)

    with pytest.raises(_RefusedError) as raised:
        refuse_failed_fragment_write(path, ClaimedOutputCleanup(path), error, _RefusedError)

    assert str(raised.value) == (
        f"{path} appeared while the Schedule FA JSON fragment was being written; this run "
        "did not write it, and it was left untouched (RuntimeError)"
    )
    assert raised.value.__cause__ is error
    assert path.read_bytes() == b"NOT OURS"
    _assert_absent(str(raised.value), _HOLDER)


@pytest.mark.parametrize(
    ("inspection", "name"),
    [
        (_marker_os_error, "EACCES"),
        (lambda: OSError(_STRERROR), "unknown error"),
        (lambda: OSError(99999, _STRERROR), "unknown error"),
    ],
    ids=["eacces", "no-errno", "unmapped-errno"],
)
def test_refuse_failed_fragment_write_names_a_path_it_cannot_inspect_by_errno_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inspection: Callable[[], OSError],
    name: str,
) -> None:
    path = tmp_path / "schedule-fa.json"
    error = RuntimeError(_HOLDER)
    lstat = os.lstat

    def cannot_inspect(target: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if target == path:
            raise inspection()
        return lstat(target, *args, **kwargs)

    monkeypatch.setattr("ibkr_fa.output.cbdt_json_fragment.os.lstat", cannot_inspect)

    with pytest.raises(OutputRemovalError) as raised:
        refuse_failed_fragment_write(path, ClaimedOutputCleanup(path), error, _RefusedError)

    assert str(raised.value) == (
        "the Schedule FA JSON fragment was not written, and could not inspect "
        f"{path} ({name}); it was left untouched (RuntimeError)"
    )
    assert raised.value.__cause__ is error
    _assert_absent(str(raised.value), _STRERROR, _HOLDER)
