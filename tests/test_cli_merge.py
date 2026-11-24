"""Tests for `ibkr-fa merge` — splicing a Schedule FA fragment into a saved
ITR-2 return (#39, PR5).

The merge writes the user's FILED return, so the bar is "never be wrong": these
pin the happy path (exact preservation of everything outside the two FA tables),
every strict refusal (the fragment is UNTRUSTED — a hand-edited file must not
splice schema-valid-but-actually-invalid data), and the file-safety guarantees
(no clobber, no partial, inputs never mutated).
"""

from __future__ import annotations

import errno
import json
import os
import re
from collections.abc import Callable
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest

import ibkr_fa.output.cbdt_json_merge as merge_mod
from ibkr_fa.cli import _render_merge_report, main
from ibkr_fa.output._cleanup import OutputRemovalError
from ibkr_fa.output.cbdt_json_fragment import (
    CbdtJsonUnreadableError,
    build_cbdt_schedule_fa_fragment,
)
from ibkr_fa.output.cbdt_json_merge import (
    _EXPECTED_ASSESSMENT_YEAR,
    _EXPECTED_SCHEMA_VER,
    CbdtJsonMergeError,
    CbdtMergeReport,
    merge_cbdt_fragment,
)
from ibkr_fa.output.cbdt_json_schema import CbdtItrForm
from tests._cbdt_fragment_inputs import fragment_input as _input
from tests._cbdt_merge_inputs import itr_form_header as _form
from tests._cbdt_merge_inputs import merge_a2_item as _a2_item
from tests._cbdt_merge_inputs import merge_a3_item as _a3_item
from tests._cbdt_merge_inputs import merge_fragment as _fragment
from tests._cbdt_merge_inputs import saved_itr_text as _itr_text
from tests._cbdt_merge_inputs import write_fragment_file as _frag_file
from tests._cbdt_merge_inputs import write_itr_file as _itr_file
from tests._crash_reports import assert_replaced_unchained

# Synthetic values no error may print (#1048).
_MARKER = "Synthetic Holder Two"
_STRERROR_MARKER = "Synthetic Strerror Marker"
# ``errno`` codes for :func:`_os_error` and the name an error prints for each.
_ERRNO_TEXT = {errno.EACCES: "EACCES", None: "unknown error", 99999: "unknown error"}

_PERMISSIONS_ENFORCED = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)


def _os_error(code: int | None = errno.EACCES) -> OSError:
    """An ``OSError`` with ``code`` (``None``: no errno), marker text and an
    optional marker filename no error may print (#1048)."""
    if code is None:
        return OSError(_STRERROR_MARKER)
    return OSError(code, _STRERROR_MARKER, f"{_MARKER}/x")


def _assert_not_echoed(message: str, *values: str) -> None:
    """None of ``values`` appears in ``message``, compared case-insensitively.

    Unsupplied markers guard future echoes; their absence alone does not
    demonstrate a change from base.
    """
    for value in values:
        assert value.lower() not in message.lower(), value


# --- fixtures: a valid fragment + a valid saved ITR-2 ----------------------


def _merge(
    tmp_path: Path,
    *,
    frag: dict[str, Any] | None = None,
    itr: str | None = None,
    itr_form: CbdtItrForm = "ITR-2",
):
    return merge_cbdt_fragment(
        fragment_path=_frag_file(tmp_path, frag, itr_form=itr_form),
        itr_path=_itr_file(tmp_path, itr, itr_form=itr_form),
        output_path=tmp_path / "merged.json",
    )


# --- happy path ------------------------------------------------------------


def test_merge_into_absent_schedule_fa(tmp_path: Path) -> None:
    report = _merge(tmp_path)
    out = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"))
    sfa = out["ITR"]["ITR2"]["ScheduleFA"]
    assert len(sfa["DtlsForeignCustodialAcc"]) == 1
    assert len(sfa["DtlsForeignEquityDebtInterest"]) == 1
    assert sfa["DtlsForeignCustodialAcc"][0]["AccountNumber"] == "U1234567"
    assert report.created_schedule_fa is True
    assert report.a3_rows_added == 1
    assert "_ibkrFa" not in (tmp_path / "merged.json").read_text(encoding="utf-8")


def test_merges_the_fragment_the_builder_emits(tmp_path: Path) -> None:
    # #798: the builder refuses whatever the merge's untrusted-fragment checks
    # refuse, so the merge accepts the builder's own output.
    fragment = build_cbdt_schedule_fa_fragment(_input())
    _merge(tmp_path, frag=fragment)
    out = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"))
    assert out["ITR"]["ITR2"]["ScheduleFA"] == fragment["ScheduleFA"]


def test_merge_into_itr3_auto_detects_target(tmp_path: Path) -> None:
    report = _merge(tmp_path, itr_form="ITR-3")
    out = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"))
    assert (
        out["ITR"]["ITR3"]["ScheduleFA"]["DtlsForeignCustodialAcc"][0]["AccountNumber"]
        == "U1234567"
    )
    assert report.itr_form == "ITR-3"


def test_legacy_fragment_remains_itr2_only(tmp_path: Path) -> None:
    _merge(tmp_path, frag=_fragment(legacy=True))
    assert (tmp_path / "merged.json").exists()

    other = tmp_path / "itr3"
    other.mkdir()
    with pytest.raises(CbdtJsonMergeError, match="fragment targets ITR-2"):
        _merge(other, frag=_fragment(legacy=True), itr_form="ITR-3")


def test_refuse_fragment_target_form_mismatch(tmp_path: Path) -> None:
    with pytest.raises(CbdtJsonMergeError, match="fragment targets ITR-2"):
        _merge(tmp_path, frag=_fragment(itr_form="ITR-2"), itr_form="ITR-3")


def test_merge_into_empty_schedule_fa(tmp_path: Path) -> None:
    itr = _itr_text(schedule_fa='{"DtlsForeignCustodialAcc": [], "DetailsForiegnBank": [{"x": 1}]}')
    _merge(tmp_path, itr=itr)
    out = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"))
    sfa = out["ITR"]["ITR2"]["ScheduleFA"]
    assert len(sfa["DtlsForeignCustodialAcc"]) == 1
    # A1 (other table inside ScheduleFA) preserved.
    assert sfa["DetailsForiegnBank"] == [{"x": 1}]


def test_preserves_siblings_and_exact_numbers(tmp_path: Path) -> None:
    # Sibling schedules + a treaty-rate-style decimal with a trailing zero and a
    # high-precision value must keep their exact number text on the re-indented
    # output.
    itr = _itr_text(
        itr2_extra=',\n      "ScheduleSI": '
        '{"rate": -1500.50, "big": 90071992547409.1234, "u": 100.0001}'
    )
    _merge(tmp_path, itr=itr)
    raw = (tmp_path / "merged.json").read_text(encoding="utf-8")
    assert '"rate": -1500.50' in raw
    assert '"big": 90071992547409.1234' in raw
    assert '"u": 100.0001' in raw


def test_minified_return_keeps_every_value_outside_the_two_fa_lists(tmp_path: Path) -> None:
    # #960: the merge rewrites a minified return as indented JSON, so escapes,
    # exponent numbers and -0 change their bytes. Their values must not: loaded
    # with parse_float=Decimal, input and output are equal outside the two lists.
    form = json.dumps(_form(), separators=(",", ":"))
    itr = (
        '{"ITR":{"ITR2":{"Form_ITR2":' + form + ',"ScheduleSI":{"url":"a\\/b",'
        '"name":"caf\\u00e9","exp":1e5,"exp2":1.50E2,"negzero":-0,"dec":-1500.50,'
        '"big":12345678901234567890.123},"ScheduleFA":{"DetailsForiegnBank":[{"x":1}]}}}}'
    )
    _merge(tmp_path, itr=itr)
    merged = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"), parse_float=Decimal)
    sfa = merged["ITR"]["ITR2"]["ScheduleFA"]
    del sfa["DtlsForeignCustodialAcc"], sfa["DtlsForeignEquityDebtInterest"]
    assert merged == json.loads(itr, parse_float=Decimal)


def test_a2_other_account_preserved(tmp_path: Path) -> None:
    other = _a2_item(AccountNumber="U9999999")
    itr = _itr_text(schedule_fa=json.dumps({"DtlsForeignCustodialAcc": [other]}))
    report = _merge(tmp_path, itr=itr)
    out = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"))
    accounts = [
        r["AccountNumber"] for r in out["ITR"]["ITR2"]["ScheduleFA"]["DtlsForeignCustodialAcc"]
    ]
    assert accounts == ["U9999999", "U1234567"]
    assert report.preserved_a2_accounts == ("U9999999",)


def test_preserves_schema_invalid_other_account_a2(tmp_path: Path) -> None:
    # A pre-existing OTHER-account A2 row that our vendored schema would reject
    # (e.g. a country code outside this snapshot's enum) is the user's filed data
    # — preserve it verbatim, never re-validate/reject it. The merge succeeds.
    other = _a2_item(AccountNumber="U9999999", CountryCodeExcludingIndia="9998")
    itr = _itr_text(schedule_fa=json.dumps({"DtlsForeignCustodialAcc": [other]}))
    _merge(tmp_path, itr=itr)
    out = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"))
    rows = out["ITR"]["ITR2"]["ScheduleFA"]["DtlsForeignCustodialAcc"]
    assert rows[0]["CountryCodeExcludingIndia"] == "9998"  # preserved verbatim
    assert rows[1]["AccountNumber"] == "U1234567"


def test_form_ver_may_differ_from_schema_ver(tmp_path: Path) -> None:
    # A real filed ITR-2 JSON had SchemaVer="Ver1.1" with FormVer="Ver1.0": the
    # two version fields are INDEPENDENT. The merge pins only SchemaVer (the
    # JSON-structure version), so a target with the expected SchemaVer but a
    # different FormVer must still be ACCEPTED (not over-refused).
    form = _form()
    form["FormVer"] = "Ver2.0"
    itr = (
        '{\n  "ITR": {\n    "ITR2": {\n'
        f'      "Form_ITR2": {json.dumps(form)}\n'
        "    }\n  }\n}\n"
    )
    _merge(tmp_path, itr=itr)
    assert (tmp_path / "merged.json").exists()


@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
def test_merge_fragment_with_empty_a3(tmp_path: Path, itr_form: CbdtItrForm) -> None:
    # #897: an explicitly empty Table A3 means no holdings, so it merges.
    report = _merge(tmp_path, frag=_fragment(a3=[], itr_form=itr_form), itr_form=itr_form)
    out = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"))
    sfa = out["ITR"][itr_form.replace("-", "")]["ScheduleFA"]
    assert sfa["DtlsForeignEquityDebtInterest"] == []
    assert report.a3_rows_added == 0


@pytest.mark.parametrize(
    ("peak", "closing"),
    [(-1500, -2000), (-99_999_999_999_999, -99_999_999_999_999)],
    ids=["margined", "at-the-bound"],
)
@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
def test_merge_keeps_negative_a2_balances(
    tmp_path: Path, itr_form: CbdtItrForm, peak: int, closing: int
) -> None:
    # #897: a margined account's balances can be negative (FaA2Row allows it),
    # so the A3 sign check must not reach Table A2. #1075: down to the CBDT bound.
    a2 = _a2_item(PeakBalanceDuringPeriod=peak, ClosingBalance=closing)
    _merge(tmp_path, frag=_fragment(a2=a2, itr_form=itr_form), itr_form=itr_form)
    out = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"))
    (row,) = out["ITR"][itr_form.replace("-", "")]["ScheduleFA"]["DtlsForeignCustodialAcc"]
    assert (row["PeakBalanceDuringPeriod"], row["ClosingBalance"]) == (peak, closing)


def test_identical_lots_both_kept(tmp_path: Path) -> None:
    frag = _fragment(a3=[_a3_item(), _a3_item()])  # two byte-identical A3 rows
    _merge(tmp_path, frag=frag)
    out = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"))
    assert len(out["ITR"]["ITR2"]["ScheduleFA"]["DtlsForeignEquityDebtInterest"]) == 2


# --- UNTRUSTED-fragment refusals (these pass validate_item but are invalid) -


@pytest.mark.parametrize(
    "bad_date",
    ["2026-02-30", "2026-11-31", "2020-01-15T00:00:00", "junk 2020-01-15 junk", " 2020-01-15 "],
)
def test_refuse_invalid_a2_date(tmp_path: Path, bad_date: str) -> None:
    frag = _fragment(a2=_a2_item(AccOpenDate=bad_date))
    with pytest.raises(CbdtJsonMergeError, match="calendar date"):
        _merge(tmp_path, frag=frag)
    assert not (tmp_path / "merged.json").exists()


@pytest.mark.parametrize("bad", ["   ", "IBKR\nEVIL", "\t", "ANN\n"])
def test_refuse_blank_or_control_string(tmp_path: Path, bad: str) -> None:
    frag = _fragment(a2=_a2_item(FinancialInstName=bad))
    with pytest.raises(CbdtJsonMergeError) as raised:
        _merge(tmp_path, frag=frag)
    assert str(raised.value) == (
        "fragment Table A2 row 1 field 'FinancialInstName' is blank/whitespace or has control "
        "characters — the fragment looks hand-edited or corrupt"
    )
    # Only the ANN case supplies this marker; match a whole token, not letters in fixed text.
    assert re.search(r"\bANN\b", str(raised.value), re.IGNORECASE) is None
    assert not (tmp_path / "merged.json").exists()


def _target_with(**form: str) -> str:
    """A saved ITR-2 whose ``Form_ITR2`` block has ``form``'s values."""
    return '{\n"ITR":{"ITR2":{"Form_ITR2":' + json.dumps({**_form(), **form}) + "}}}"


def _fragment_marked(**marker: str) -> dict[str, Any]:
    """A fragment whose ``_ibkrFa`` marker has ``marker``'s values."""
    frag = _fragment()
    frag["_ibkrFa"].update(marker)
    return frag


_HAND_EDITED = "— the fragment looks hand-edited or corrupt"


def _fragment_a2_rows(*rows: dict[str, Any], itr_form: CbdtItrForm = "ITR-2") -> dict[str, Any]:
    """A fragment whose Table A2 holds ``rows``."""
    frag = _fragment(itr_form=itr_form)
    frag["ScheduleFA"]["DtlsForeignCustodialAcc"] = list(rows)
    return frag


_BELOW_BOUND = -100_000_000_000_000
_FULL_WIDTH_ACCOUNT = "U\uff11\uff12\uff13\uff14\uff15\uff16\uff17"  # U, full-width 1234567
# FaA2Row rules the schema gate lets through (#1075): (case id, the forms each
# runs for, the fragment's Table A2 rows, the refusal, values no output may print).
# The ITR-2 schema refuses a balance below the bound itself.
_A2_ROW_REFUSALS: list[
    tuple[str, tuple[CbdtItrForm, ...], list[dict[str, Any]], str, tuple[str, ...]]
] = [
    (
        "masked",
        ("ITR-2", "ITR-3"),
        [_a2_item(AccountNumber="U**********")],
        f"fragment Table A2 row 1 field 'AccountNumber' is a masked account number {_HAND_EDITED}",
        ("U**********",),
    ),
    (
        "masked-row-2",
        ("ITR-2", "ITR-3"),
        [_a2_item(), _a2_item(AccountNumber="U**********")],
        f"fragment Table A2 row 2 field 'AccountNumber' is a masked account number {_HAND_EDITED}",
        ("U**********",),
    ),
    (
        "full-width-digits",
        ("ITR-2", "ITR-3"),
        [_a2_item(AccountNumber=_FULL_WIDTH_ACCOUNT)],
        f"fragment Table A2 row 1 field 'AccountNumber' has a non-ASCII character {_HAND_EDITED}",
        (_FULL_WIDTH_ACCOUNT, "1234567"),
    ),
    (
        "name-shaped",
        ("ITR-2", "ITR-3"),
        [_a2_item(AccountNumber="Synthetic Holder Twö")],
        f"fragment Table A2 row 1 field 'AccountNumber' has a non-ASCII character {_HAND_EDITED}",
        ("Synthetic Holder Tw",),
    ),
    (
        "peak-below-bound",
        ("ITR-3",),
        [_a2_item(PeakBalanceDuringPeriod=_BELOW_BOUND)],
        f"fragment Table A2 row 1 field 'PeakBalanceDuringPeriod' is below the CBDT 14-digit "
        f"bound {_HAND_EDITED}",
        ("100000000000000",),
    ),
    (
        "closing-below-bound",
        ("ITR-3",),
        [_a2_item(ClosingBalance=_BELOW_BOUND)],
        f"fragment Table A2 row 1 field 'ClosingBalance' is below the CBDT 14-digit bound "
        f"{_HAND_EDITED}",
        ("100000000000000",),
    ),
]


@pytest.mark.parametrize(
    ("frag", "itr", "message", "value"),
    [
        pytest.param(
            None,
            _target_with(FormName=_MARKER),
            "the target is not an ITR-2 (Form_ITR2.FormName is not 'ITR-2')",
            _MARKER,
            id="form-name",
        ),
        pytest.param(
            None,
            _target_with(AssessmentYear="2025-03-01"),
            "the target's assessment year (Form_ITR2.AssessmentYear) is not AY 2026-27 "
            "(expected '2026') — this fragment is for AY 2026-27 only",
            "2025-03-01",
            id="assessment-year",
        ),
        pytest.param(
            None,
            _target_with(SchemaVer=_MARKER),
            "the target was saved against a different JSON schema revision "
            "(Form_ITR2.SchemaVer is not 'Ver1.0') — ibkr-fa's Schedule FA mapping is "
            "pinned to that revision. Open an issue with your SchemaVer so it can be "
            "supported.",
            _MARKER,
            id="schema-ver",
        ),
        pytest.param(
            _fragment_marked(schemaVersion=_MARKER),
            None,
            "fragment was built for a different CBDT schema (its _ibkrFa.schemaVersion is "
            "not 'ITR-2 AY2026-27 Main V1.2 (2026-08-13)'); regenerate it with this "
            "version of ibkr-fa",
            _MARKER,
            id="fragment-schema-version",
        ),
        pytest.param(
            _fragment(a2=_a2_item(AccOpenDate="2025-02-30")),
            None,
            f"fragment Table A2 row 1 field 'AccOpenDate' is not a valid YYYY-MM-DD "
            f"calendar date {_HAND_EDITED}",
            "2025-02-30",
            id="a2-date",
        ),
        pytest.param(
            _fragment(a3=[_a3_item(), _a3_item(InterestAcquiringDate="2025-02-30")]),
            None,
            f"fragment Table A3 row 2 field 'InterestAcquiringDate' is not a valid "
            f"YYYY-MM-DD calendar date {_HAND_EDITED}",
            "2025-02-30",
            id="a3-date",
        ),
        pytest.param(
            _fragment(a2=_a2_item(AccountNumber="U1234567\t")),
            None,
            f"fragment Table A2 row 1 field 'AccountNumber' is blank/whitespace or has "
            f"control characters {_HAND_EDITED}",
            "U1234567",
            id="a2-account-control-character",
        ),
        pytest.param(
            # #1075: the control-character refusal comes before the ASCII check.
            _fragment(a2=_a2_item(AccountNumber="Ü1234567\t")),
            None,
            f"fragment Table A2 row 1 field 'AccountNumber' is blank/whitespace or has "
            f"control characters {_HAND_EDITED}",
            ("Ü1234567", "1234567"),
            id="a2-account-control-character-and-non-ascii",
        ),
        *(
            pytest.param(
                _fragment_a2_rows(*rows, itr_form=forms[0]),
                _itr_text(itr_form=forms[0]),
                message,
                values,
                id=f"a2-{case_id}",
            )
            for case_id, forms, rows, message, values in _A2_ROW_REFUSALS
        ),
        pytest.param(
            _fragment(a3=[_a3_item(NameOfEntity=f" {_MARKER}\n")]),
            None,
            f"fragment Table A3 row 1 field 'NameOfEntity' is blank/whitespace or has "
            f"control characters {_HAND_EDITED}",
            _MARKER,
            id="a3-name-control-character",
        ),
        pytest.param(
            _fragment(a3=[_a3_item(ClosingBalance=-1234567)]),
            None,
            f"fragment Table A3 row 1 field 'ClosingBalance' is negative {_HAND_EDITED}",
            "1234567",
            id="a3-negative-amount",
        ),
        pytest.param(
            _fragment(a3=[_a3_item(), _a3_item(NameOfEntity=_MARKER * 7)]),
            None,
            "fragment Table A3 row 2 failed CBDT schema validation: field 'NameOfEntity' "
            "breaks the schema's 'maxLength' rule",
            _MARKER,
            id="a3-name-too-long",
        ),
        pytest.param(
            _fragment(a2=_a2_item(ClosingBalance="1234567")),
            None,
            "fragment Table A2 row 1 failed CBDT schema validation: field 'ClosingBalance' "
            "breaks the schema's 'type' rule",
            "1234567",
            id="a2-amount-as-text",
        ),
    ],
)
def test_refusal_names_the_field_not_the_value(
    tmp_path: Path,
    frag: dict[str, Any] | None,
    itr: str | None,
    message: str,
    value: str | tuple[str, ...],
) -> None:
    # #1048: the fragment and the target are untrusted; a refusal names where
    # the problem is and which rule it breaks, never the value.
    with pytest.raises(CbdtJsonMergeError) as raised:
        _merge(tmp_path, frag=frag, itr=itr)
    assert str(raised.value) == message
    _assert_not_echoed(str(raised.value), *((value,) if isinstance(value, str) else value))
    assert not (tmp_path / "merged.json").exists()


def _assert_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    frag: dict[str, Any],
    itr_form: CbdtItrForm,
    reason: str,
) -> None:
    """The merge and the CLI (exit 4) both refuse for ``reason``, writing nothing."""
    fragment_path = _frag_file(tmp_path, frag)
    itr_path = _itr_file(tmp_path, itr_form=itr_form)
    out = tmp_path / "merged.json"
    with pytest.raises(CbdtJsonMergeError, match=re.escape(reason)):
        merge_cbdt_fragment(fragment_path=fragment_path, itr_path=itr_path, output_path=out)
    assert not out.exists()
    argv = ["merge", "--fragment", str(fragment_path), "--itr", str(itr_path), "--output", str(out)]
    assert main(argv) == 4
    assert reason in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
@pytest.mark.parametrize(
    ("table", "reason"),
    [
        ("DtlsForeignCustodialAcc", "DtlsForeignCustodialAcc (Table A2)"),
        ("DtlsForeignEquityDebtInterest", "DtlsForeignEquityDebtInterest (Table A3)"),
    ],
)
def test_refuse_fragment_missing_table(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    itr_form: CbdtItrForm,
    table: str,
    reason: str,
) -> None:
    # #897: the writer always emits both tables, so a missing one is not "no rows".
    frag = _fragment(itr_form=itr_form)
    del frag["ScheduleFA"][table]
    _assert_refused(tmp_path, capsys, frag, itr_form, reason)


@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
@pytest.mark.parametrize(
    "field",
    [
        "InitialValOfInvstmnt",
        "PeakBalanceDuringPeriod",
        "ClosingBalance",
        "TotGrossAmtPaidCredited",
        "TotGrossProceeds",
    ],
)
def test_refuse_negative_a3_amount(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], itr_form: CbdtItrForm, field: str
) -> None:
    # #897: both vendored schemas let these go negative; FaA3Row never does.
    frag = _fragment(a3=[_a3_item(**{field: -1})], itr_form=itr_form)
    _assert_refused(tmp_path, capsys, frag, itr_form, f"field {field!r} is negative")


_ABSENT = object()  # A ``_rule_case`` value that deletes the field.
# The merge's own refusals by rule; any other rule is a schema keyword.
_MERGE_RULES = {
    "date": "is not a valid YYYY-MM-DD calendar date",
    "blank-or-control": "is blank/whitespace or has control characters",
    "non-ascii": "has a non-ASCII character",
    "masked": "is a masked account number",
}
_A2_TEXT = ("CountryName", "FinancialInstName", "FinancialInstAddress", "ZipCode", "AccountNumber")
_A3_TEXT = ("CountryName", "NameOfEntity", "AddressOfEntity", "ZipCode", "NatureOfEntity")
_A2_AMOUNTS = ("PeakBalanceDuringPeriod", "ClosingBalance", "GrossAmtPaidCredited")
_A3_AMOUNTS = (
    "InitialValOfInvstmnt",
    "PeakBalanceDuringPeriod",
    "ClosingBalance",
    "TotGrossAmtPaidCredited",
    "TotGrossProceeds",
)


def _rule_case(table: str, field: str, value: object, rule: str, label: str = "") -> Any:
    """Row 1 of Table ``table`` with ``field`` set to ``value``, refused by
    ``rule``; ``label`` tells apart cases with the same field and rule."""
    if rule in _MERGE_RULES:
        refusal = f"field {field!r} {_MERGE_RULES[rule]} {_HAND_EDITED}"
    else:
        refusal = (
            f"failed CBDT schema validation: field {field!r} breaks the schema's {rule!r} rule"
        )
    return pytest.param(table, field, value, refusal, id=f"{table}-{field}-{label or rule}")


@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
@pytest.mark.parametrize(
    ("table", "field", "value", "refusal"),
    [
        # Text fields are strings (FaA2Row and FaA3Row require_text).
        *(_rule_case("A2", field, 2, "type") for field in _A2_TEXT),
        *(_rule_case("A3", field, 2, "type") for field in _A3_TEXT),
        # The country code is a numeric CBDT code; Table A2's is never India.
        _rule_case("A2", "CountryCodeExcludingIndia", 2, "enum", "not-text"),
        _rule_case("A3", "CountryCodeExcludingIndia", 2, "enum", "not-text"),
        _rule_case("A2", "CountryCodeExcludingIndia", "US", "enum", "not-numeric"),
        _rule_case("A3", "CountryCodeExcludingIndia", "US", "enum", "not-numeric"),
        _rule_case("A2", "CountryCodeExcludingIndia", "91", "enum", "india"),
        # Country and institution or entity names are non-empty.
        *(
            _rule_case("A2", field, "", "minLength")
            for field in ("CountryName", "FinancialInstName", "FinancialInstAddress")
        ),
        *(_rule_case("A3", field, "", "minLength") for field in ("CountryName", "NameOfEntity")),
        # FaA3Row's nature of entity is empty or has text, and at most 34 characters.
        _rule_case("A3", "NatureOfEntity", "   ", "blank-or-control"),
        _rule_case("A3", "NatureOfEntity", "N" * 35, "maxLength"),
        # Their other text is at most the schemas' maximum length (#1189).
        *(
            _rule_case(table, field, "L" * (limit + 1), "maxLength")
            for table, field, limit in (
                ("A2", "CountryName", 55),
                ("A2", "FinancialInstName", 125),
                ("A2", "FinancialInstAddress", 200),
                ("A2", "ZipCode", 8),
                ("A2", "AccountNumber", 34),
                ("A3", "CountryName", 55),
                ("A3", "NameOfEntity", 125),
                ("A3", "AddressOfEntity", 200),
                ("A3", "ZipCode", 8),
            )
        ),
        # FaA2Row's account number: non-empty, no control character, ASCII, not masked.
        _rule_case("A2", "AccountNumber", "", "minLength"),
        _rule_case("A2", "AccountNumber", "U1234567\t", "blank-or-control"),
        _rule_case("A2", "AccountNumber", _FULL_WIDTH_ACCOUNT, "non-ascii"),
        _rule_case("A2", "AccountNumber", "U**********", "masked"),
        # Status and nature are their enums, and the nature comes with its amount.
        _rule_case("A2", "Status", "SIGNING_AUTHORITY", "enum"),
        _rule_case("A2", "NatureOfAmount", "X", "enum"),
        _rule_case("A2", "NatureOfAmount", _ABSENT, "required"),
        _rule_case("A2", "GrossAmtPaidCredited", _ABSENT, "required"),
        # Dates are calendar dates, not datetimes (require_dates).
        *(
            _rule_case(table, field, value, "date", label)
            for table, field in (("A2", "AccOpenDate"), ("A3", "InterestAcquiringDate"))
            for value, label in (("2025-02-30", "no-such-day"), ("2025-02-01T00:00:00", "datetime"))
        ),
        # Amounts are numbers within the CBDT bound (require_filed_amounts), and
        # Table A2's gross amount is non-negative. The A2 balances' lower bound:
        # test_itr2_schema_refuses_an_a2_balance_below_the_bound and the
        # _A2_ROW_REFUSALS cases; the A3 signs: test_refuse_negative_a3_amount.
        *(_rule_case("A2", field, "1", "type") for field in _A2_AMOUNTS),
        *(_rule_case("A3", field, "1", "type") for field in _A3_AMOUNTS),
        *(_rule_case("A2", field, 10**14, "maximum") for field in _A2_AMOUNTS),
        *(_rule_case("A3", field, 10**14, "maximum") for field in _A3_AMOUNTS),
        _rule_case("A2", "GrossAmtPaidCredited", -1, "minimum"),
    ],
)
def test_refuses_each_row_model_rule_a_fragment_field_can_break(
    tmp_path: Path, itr_form: CbdtItrForm, table: str, field: str, value: object, refusal: str
) -> None:
    # #1075: the schema gate or the merge's own check refuses every FaA2Row and
    # FaA3Row rule a fragment's own fields can break, for both forms.
    item = _a2_item() if table == "A2" else _a3_item()
    if value is _ABSENT:
        del item[field]
    else:
        item[field] = value
    frag = (
        _fragment(a2=item, itr_form=itr_form)
        if table == "A2"
        else _fragment(a3=[item], itr_form=itr_form)
    )
    with pytest.raises(CbdtJsonMergeError) as raised:
        _merge(tmp_path, frag=frag, itr=_itr_text(itr_form=itr_form))
    assert str(raised.value) == f"fragment Table {table} row 1 {refusal}"
    assert not (tmp_path / "merged.json").exists()


@pytest.mark.parametrize("field", ["PeakBalanceDuringPeriod", "ClosingBalance"])
def test_itr2_schema_refuses_an_a2_balance_below_the_bound(tmp_path: Path, field: str) -> None:
    # #1075: the merge's own bound check is for ITR-3, whose schema sets no minimum.
    with pytest.raises(CbdtJsonMergeError) as raised:
        _merge(tmp_path, frag=_fragment(a2=_a2_item(**{field: _BELOW_BOUND})))
    assert str(raised.value) == (
        f"fragment Table A2 row 1 failed CBDT schema validation: field {field!r} "
        "breaks the schema's 'minimum' rule"
    )
    assert not (tmp_path / "merged.json").exists()


def test_refuse_fragment_without_marker(tmp_path: Path) -> None:
    frag = _fragment()
    del frag["_ibkrFa"]
    with pytest.raises(CbdtJsonMergeError, match="provenance marker"):
        _merge(tmp_path, frag=frag)


@pytest.mark.parametrize("link_target", [None, "fragment.json", "missing.json"])
def test_refuse_fragment_that_cannot_be_inspected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], link_target: str | None
) -> None:
    frag = tmp_path / "fragment.json"
    if link_target is None:
        frag.write_text("not json", encoding="utf-8")
    else:
        frag.symlink_to(link_target)
    itr = _itr_file(tmp_path)
    original_itr = itr.read_bytes()
    out = tmp_path / "merged.json"
    with pytest.raises(CbdtJsonUnreadableError, match="could not inspect"):
        merge_cbdt_fragment(
            fragment_path=frag,
            itr_path=itr,
            output_path=out,
        )
    assert main(["merge", "--fragment", str(frag), "--itr", str(itr), "--output", str(out)]) == 4
    assert "could not inspect" in capsys.readouterr().err
    assert itr.read_bytes() == original_itr
    if link_target is None:
        assert frag.read_text(encoding="utf-8") == "not json"
    else:
        assert frag.readlink() == Path(link_target)
    assert not out.exists()


def test_refuse_wrong_format_marker(tmp_path: Path) -> None:
    frag = _fragment()
    frag["_ibkrFa"]["format"] = "something-else"
    with pytest.raises(CbdtJsonMergeError, match="not an ibkr-fa"):
        _merge(tmp_path, frag=frag)


def test_refuse_schema_version_mismatch(tmp_path: Path) -> None:
    frag = _fragment()
    frag["_ibkrFa"]["schemaVersion"] = "ITR-2 AY2099-00 vX"
    with pytest.raises(CbdtJsonMergeError, match="different CBDT schema"):
        _merge(tmp_path, frag=frag)


def test_merge_fragment_two_a2_rows(tmp_path: Path) -> None:
    frag = _fragment()
    frag["ScheduleFA"]["DtlsForeignCustodialAcc"] = [
        _a2_item(),
        _a2_item(AccountNumber="U7654321"),
    ]
    report = _merge(tmp_path, frag=frag)
    merged = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"))
    accounts = [
        row["AccountNumber"]
        for row in merged["ITR"]["ITR2"]["ScheduleFA"]["DtlsForeignCustodialAcc"]
    ]
    assert accounts == ["U1234567", "U7654321"]
    assert report.a2_account_numbers == ("U1234567", "U7654321")


def test_merge_fragment_repeated_a2_account_natures(tmp_path: Path) -> None:
    frag = _fragment()
    frag["ScheduleFA"]["DtlsForeignCustodialAcc"] = [
        _a2_item(NatureOfAmount="D", GrossAmtPaidCredited=10),
        _a2_item(NatureOfAmount="I", GrossAmtPaidCredited=20),
    ]

    report = _merge(tmp_path, frag=frag)
    merged = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"))
    rows = merged["ITR"]["ITR2"]["ScheduleFA"]["DtlsForeignCustodialAcc"]

    assert [row["AccountNumber"] for row in rows] == ["U1234567", "U1234567"]
    assert [row["NatureOfAmount"] for row in rows] == ["D", "I"]
    assert report.a2_account_numbers == ("U1234567",)
    assert report.a2_rows_added == 2


def test_refuse_fragment_duplicate_a2_account_natures(tmp_path: Path) -> None:
    frag = _fragment()
    frag["ScheduleFA"]["DtlsForeignCustodialAcc"] = [_a2_item(), _a2_item()]
    with pytest.raises(CbdtJsonMergeError, match="duplicate"):
        _merge(tmp_path, frag=frag)


def test_refuse_fragment_inconsistent_repeated_a2_account(tmp_path: Path) -> None:
    frag = _fragment()
    frag["ScheduleFA"]["DtlsForeignCustodialAcc"] = [
        _a2_item(NatureOfAmount="D"),
        _a2_item(NatureOfAmount="I", ClosingBalance=1),
    ]
    with pytest.raises(CbdtJsonMergeError, match="ClosingBalance"):
        _merge(tmp_path, frag=frag)


def test_merge_report_distinguishes_a2_rows_from_accounts(tmp_path: Path) -> None:
    other = [
        _a2_item(AccountNumber="U9999999", NatureOfAmount="D"),
        _a2_item(AccountNumber="U9999999", NatureOfAmount="I"),
    ]
    itr = _itr_text(schedule_fa=json.dumps({"DtlsForeignCustodialAcc": other}))
    frag = _fragment()
    frag["ScheduleFA"]["DtlsForeignCustodialAcc"] = [
        _a2_item(NatureOfAmount="D"),
        _a2_item(NatureOfAmount="I"),
        _a2_item(NatureOfAmount="S"),
    ]

    report = _merge(tmp_path, itr=itr, frag=frag)
    (run_outputs,) = (field for field in fields(report) if field.name == "run_outputs")
    assert run_outputs.kw_only is True
    rendered = _render_merge_report(report)

    assert "Table A2 (foreign custodial account): +3 row(s) for 1 account(s)" in rendered
    assert "Preserved 2 existing Table A2 row(s) for 1 other account(s)" in rendered


@pytest.mark.parametrize("table", ["DetailsForiegnBank", _MARKER])
def test_refuse_fragment_extra_schedulefa_key(tmp_path: Path, table: str) -> None:
    """The refusal names the two allowed tables, not the extra one (#1048)."""
    frag = _fragment()
    frag["ScheduleFA"][table] = [{"x": 1}]
    with pytest.raises(CbdtJsonMergeError) as raised:
        _merge(tmp_path, frag=frag)
    assert str(raised.value) == (
        "fragment ScheduleFA has a table other than DtlsForeignCustodialAcc and "
        "DtlsForeignEquityDebtInterest"
    )
    _assert_not_echoed(str(raised.value), table)


# --- A2 conflict / A3 non-empty refusals -----------------------------------


_ACCOUNT_CONFLICT = (
    "fragment Table A2 row {row} field 'AccountNumber' is already disclosed in the "
    "target's Table A2 row {existing} — merge into a CLEAN ITR-2 export, once "
    "(re-export from the utility)"
)


@pytest.mark.parametrize("existing_account", ["U1234567", " U1234567", "u1234567", "U1234567 "])
def test_refuse_a2_same_account_conflict(tmp_path: Path, existing_account: str) -> None:
    """The refusal names the rows, not the account number (#1048)."""
    existing = _a2_item(AccountNumber=existing_account)
    itr = _itr_text(schedule_fa=json.dumps({"DtlsForeignCustodialAcc": [existing]}))
    with pytest.raises(CbdtJsonMergeError) as raised:
        _merge(tmp_path, itr=itr)
    assert str(raised.value) == _ACCOUNT_CONFLICT.format(row=1, existing=1)
    _assert_not_echoed(str(raised.value), "U1234567")
    assert not (tmp_path / "merged.json").exists()


def test_account_conflict_names_the_fragment_and_target_rows(tmp_path: Path) -> None:
    # #1048: fragment row 2 holds the account the target's row 3 already lists.
    existing = [
        _a2_item(AccountNumber="U9999999"),
        _a2_item(AccountNumber="U8888888"),
        _a2_item(AccountNumber="U1234567"),
    ]
    itr = _itr_text(schedule_fa=json.dumps({"DtlsForeignCustodialAcc": existing}))
    frag = _fragment()
    frag["ScheduleFA"]["DtlsForeignCustodialAcc"] = [
        _a2_item(AccountNumber="U7654321"),
        _a2_item(AccountNumber="U1234567"),
    ]
    with pytest.raises(CbdtJsonMergeError) as raised:
        _merge(tmp_path, frag=frag, itr=itr)
    assert str(raised.value) == _ACCOUNT_CONFLICT.format(row=2, existing=3)
    _assert_not_echoed(str(raised.value), "U1234567")
    assert not (tmp_path / "merged.json").exists()


@pytest.mark.parametrize("row_number", [1, 2])
@pytest.mark.parametrize("missing", [False, True], ids=["blank", "missing"])
def test_refuse_existing_a2_blank_account(tmp_path: Path, row_number: int, missing: bool) -> None:
    existing = _a2_item(AccountNumber="   ")
    if missing:
        del existing["AccountNumber"]
    rows = [_a2_item(AccountNumber="U7654321")] * (row_number - 1) + [existing]
    itr = _itr_text(schedule_fa=json.dumps({"DtlsForeignCustodialAcc": rows}))
    with pytest.raises(CbdtJsonMergeError) as raised:
        _merge(tmp_path, itr=itr)
    assert str(raised.value) == (
        f"target Table A2 row {row_number} field 'AccountNumber' is missing/blank; refusing "
        "to merge into a target with a corrupt A2 row"
    )
    assert raised.value.__cause__ is None
    assert not (tmp_path / "merged.json").exists()


def test_refuse_nonempty_a3(tmp_path: Path) -> None:
    itr = _itr_text(schedule_fa=json.dumps({"DtlsForeignEquityDebtInterest": [_a3_item()]}))
    with pytest.raises(CbdtJsonMergeError, match="already has Table A3"):
        _merge(tmp_path, itr=itr)


# --- target identity refusals ----------------------------------------------


def test_refuse_not_itr2(tmp_path: Path) -> None:
    itr = '{"ITR": {"ITR1": {"Form_ITR1": {}}}}'
    with pytest.raises(CbdtJsonMergeError, match="ITR2"):
        _merge(tmp_path, itr=itr)


def test_refuse_ambiguous_supported_wrappers(tmp_path: Path) -> None:
    itr = json.dumps(
        {
            "ITR": {
                "ITR2": {"Form_ITR2": _form("ITR-2")},
                "ITR3": {"Form_ITR3": _form("ITR-3")},
            }
        }
    )
    with pytest.raises(CbdtJsonMergeError, match="exactly one supported"):
        _merge(tmp_path, itr=itr)


def test_refuse_wrong_form_name(tmp_path: Path) -> None:
    form = _form()
    form["FormName"] = "ITR-3"
    itr = '{\n"ITR":{"ITR2":{"Form_ITR2":' + json.dumps(form) + "}}}"
    with pytest.raises(CbdtJsonMergeError, match="not an ITR-2"):
        _merge(tmp_path, itr=itr)


# `Form_ITR2.AssessmentYear` is the bare start-year "2026" for AY 2026-27 (the
# "2026-27" label format is used only by other ITR fields), so the label format
# and adjacent years are all refused.
@pytest.mark.parametrize("bad_ay", ["2026-27", "2025-26", "2025-2026", "2027-28", "2027"])
def test_refuse_wrong_assessment_year(tmp_path: Path, bad_ay: str) -> None:
    form = _form()
    form["AssessmentYear"] = bad_ay
    itr = '{\n"ITR":{"ITR2":{"Form_ITR2":' + json.dumps(form) + "}}}"
    with pytest.raises(CbdtJsonMergeError, match="assessment year"):
        _merge(tmp_path, itr=itr)


def test_refuse_wrong_schema_ver(tmp_path: Path) -> None:
    form = _form()
    form["SchemaVer"] = "Ver1.1"
    itr = '{\n"ITR":{"ITR2":{"Form_ITR2":' + json.dumps(form) + "}}}"
    with pytest.raises(CbdtJsonMergeError, match="schema revision"):
        _merge(tmp_path, itr=itr)


def test_refuse_missing_form_ver(tmp_path: Path) -> None:
    # FormVer's value isn't pinned, but it is a required field — its absence
    # means the target isn't a genuine offline-utility export.
    form = _form()
    del form["FormVer"]
    itr = '{\n"ITR":{"ITR2":{"Form_ITR2":' + json.dumps(form) + "}}}"
    with pytest.raises(CbdtJsonMergeError, match="no FormVer"):
        _merge(tmp_path, itr=itr)


def test_expected_literals_match_vendored_schema() -> None:
    # Drift guard: the pinned target-identity literals must equal the vendored
    # schema's own Form_ITR2 patterns, so a re-vendoring can't silently leave the
    # merge refusing every valid return (the unanchored patterns ARE the exact
    # expected literals — that weakness is exactly why we pin equality).
    import json  # noqa: PLC0415
    from importlib import resources  # noqa: PLC0415

    schema = json.loads(
        (resources.files("ibkr_fa.reference") / "cbdt_itr2_schema_ay2026_27.json").read_text()
    )
    form_props = schema["definitions"]["Form_ITR2"]["properties"]
    assert form_props["AssessmentYear"]["pattern"] == _EXPECTED_ASSESSMENT_YEAR
    assert form_props["SchemaVer"]["pattern"] == _EXPECTED_SCHEMA_VER

    itr3_schema = json.loads(
        (
            resources.files("ibkr_fa.reference") / "cbdt_itr3_schedule_fa_schema_ay2026_27.json"
        ).read_text()
    )
    itr3_form_props = itr3_schema["definitions"]["Form_ITR3"]["properties"]
    assert itr3_form_props["FormName"]["enum"] == ["ITR-3"]
    assert itr3_form_props["AssessmentYear"]["enum"] == [_EXPECTED_ASSESSMENT_YEAR]
    assert itr3_form_props["SchemaVer"]["pattern"] == _EXPECTED_SCHEMA_VER


def test_refuse_schedulefa_not_dict(tmp_path: Path) -> None:
    itr = _itr_text(schedule_fa="[1, 2, 3]")
    with pytest.raises(CbdtJsonMergeError, match="ScheduleFA"):
        _merge(tmp_path, itr=itr)


# --- file safety -----------------------------------------------------------


def test_refuse_duplicate_keys_anywhere(tmp_path: Path) -> None:
    # A duplicate key OUTSIDE ScheduleFA must still abort (load would drop one).
    itr = (
        '{\n  "ITR": {\n    "ITR2": {\n'
        f'      "Form_ITR2": {json.dumps(_form())},\n'
        '      "ScheduleSI": {"a": 1, "a": 2}\n'
        "    }\n  }\n}\n"
    )
    with pytest.raises(CbdtJsonMergeError, match="duplicate key"):
        _merge(tmp_path, itr=itr)


def _merge_files(tmp_path: Path) -> CbdtMergeReport:
    """Merge the fragment and the saved return already in ``tmp_path``."""
    return merge_cbdt_fragment(
        fragment_path=tmp_path / "fragment.json",
        itr_path=tmp_path / "itr-2.json",
        output_path=tmp_path / "merged.json",
    )


def _input_holding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, data: bytes
) -> Path:
    """Write a valid fragment and saved return to ``tmp_path``, then give the
    ``label`` one (``fragment`` or ``ITR``) ``data``; return its path.

    The fragment gets ``data`` only once the merge's provenance check has read
    it, as if it changed between that read and the merge's own load: the check
    refuses any file it cannot parse, so only then does the load refuse.
    """
    fragment = _frag_file(tmp_path)
    itr = _itr_file(tmp_path)
    if label == "ITR":
        itr.write_bytes(data)
        return itr
    inspect = merge_mod.is_ibkr_fa_cbdt_fragment

    def inspect_then_replace(path: Path) -> bool:
        ours = inspect(path)
        path.write_bytes(data)
        return ours

    monkeypatch.setattr(merge_mod, "is_ibkr_fa_cbdt_fragment", inspect_then_replace)
    return fragment


@pytest.mark.parametrize("label", ["fragment", "ITR"])
def test_invalid_json_names_its_line_and_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    # #1048: the bad token is on line 2, column 27, after the 20-character key.
    path = _input_holding(tmp_path, monkeypatch, label, b'{\n  "Synthetic Holder Two": nope\n}\n')
    with pytest.raises(CbdtJsonMergeError) as raised:
        _merge_files(tmp_path)
    assert str(raised.value) == f"the {label} file {path} is not valid JSON (at line 2, column 27)"
    assert isinstance(raised.value.__cause__, json.JSONDecodeError)
    # The decoder's own message quotes no document text either, so this guards
    # the fixed text against a future excerpt of the file.
    _assert_not_echoed(str(raised.value), _MARKER, "nope")
    assert not (tmp_path / "merged.json").exists()


@pytest.mark.parametrize("label", ["fragment", "ITR"])
def test_invalid_utf8_names_its_byte_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    # #1048: the undecodable byte is at offset 29, after the 20-character key;
    # its value (0xff) is file content and is not printed.
    data = b'{\n  "Synthetic Holder Two": "\xff"\n}\n'
    path = _input_holding(tmp_path, monkeypatch, label, data)
    with pytest.raises(CbdtJsonMergeError) as raised:
        _merge_files(tmp_path)
    assert str(raised.value) == f"the {label} file {path} is not valid UTF-8 (at byte offset 29)"
    assert isinstance(raised.value.__cause__, UnicodeDecodeError)
    # Base echoed the byte but already hid the holder; its absence guards future excerpts.
    _assert_not_echoed(str(raised.value), "0xff", _MARKER)
    assert not (tmp_path / "merged.json").exists()


@pytest.mark.parametrize(
    "code", [errno.EACCES, None, 99999], ids=["EACCES", "no-errno", "unmapped"]
)
@pytest.mark.parametrize("label", ["fragment", "ITR"])
def test_unreadable_input_names_its_file_and_errno(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, code: int | None
) -> None:
    # #1048: the errno's name, never the error's text or file name.
    fragment = _frag_file(tmp_path)
    itr = _itr_file(tmp_path)
    path = fragment if label == "fragment" else itr
    error = _os_error(code)
    original = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        if self == path:
            raise error
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(CbdtJsonMergeError) as raised:
        _merge_files(tmp_path)
    assert str(raised.value) == f"could not read the {label} file {path} ({_ERRNO_TEXT[code]})"
    assert raised.value.__cause__ is error
    _assert_not_echoed(str(raised.value), _STRERROR_MARKER, _MARKER)


@pytest.mark.parametrize("label", ["fragment", "ITR"])
def test_duplicate_key_names_its_file_not_the_key(tmp_path: Path, label: str) -> None:
    # #1048: the fragment and the saved return each name their own label and
    # path; the duplicated key is not printed.
    duplicate = f'"{_MARKER}": 1, "{_MARKER}": 2, '
    fragment = _frag_file(tmp_path)
    itr = _itr_file(tmp_path)
    if label == "fragment":
        # Inside the A3 row: the provenance check's plain load accepts it.
        text = fragment.read_text(encoding="utf-8")
        fragment.write_text(
            text.replace('"NameOfEntity": ', duplicate + '"NameOfEntity": ', 1), encoding="utf-8"
        )
        path, other = fragment, itr
    else:
        itr.write_text(
            _itr_text(itr2_extra=f',\n      "ScheduleSI": {{{duplicate}"x": 3}}'),
            encoding="utf-8",
        )
        path, other = itr, fragment
    with pytest.raises(CbdtJsonMergeError) as raised:
        _merge_files(tmp_path)
    assert str(raised.value) == (
        f"refusing to merge: the {label} file {path} has a duplicate key in a JSON object; "
        "loading it would silently drop data. Re-export a clean file."
    )
    # Base named neither file; this guards wrong-source binding, while the exact text pins our path.
    assert str(other) not in str(raised.value)
    _assert_not_echoed(str(raised.value), _MARKER)
    assert not (tmp_path / "merged.json").exists()


def test_refuse_nan(tmp_path: Path) -> None:
    itr = _itr_text(itr2_extra=',\n      "ScheduleSI": {"x": NaN}')
    with pytest.raises(CbdtJsonMergeError, match="non-finite"):
        _merge(tmp_path, itr=itr)


def test_refuse_output_exists(tmp_path: Path) -> None:
    out = tmp_path / "merged.json"
    out.write_text("PRECIOUS", encoding="utf-8")
    with pytest.raises(CbdtJsonMergeError, match="already exists"):
        merge_cbdt_fragment(
            fragment_path=_frag_file(tmp_path),
            itr_path=_itr_file(tmp_path),
            output_path=out,
        )
    assert out.read_text(encoding="utf-8") == "PRECIOUS"  # untouched


@_PERMISSIONS_ENFORCED
def test_output_in_an_unsearchable_folder_raises_eacces(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """#1238: the output existence check raises EACCES instead of calling the output absent."""
    fragment, itr = _frag_file(tmp_path), _itr_file(tmp_path)
    before = fragment.read_bytes(), itr.read_bytes()
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    out = blocked / "merged.json"
    blocked.chmod(0)
    request.addfinalizer(lambda: blocked.chmod(0o700))
    with pytest.raises(PermissionError) as raised:
        merge_cbdt_fragment(fragment_path=fragment, itr_path=itr, output_path=out)
    assert type(raised.value) is PermissionError
    assert str(raised.value) == f"[Errno {errno.EACCES}] Permission denied: {str(out)!r}"
    assert (fragment.read_bytes(), itr.read_bytes()) == before
    blocked.chmod(0o700)
    assert list(blocked.iterdir()) == []


def test_refuse_output_same_as_itr(tmp_path: Path) -> None:
    itr = _itr_file(tmp_path)
    with pytest.raises(CbdtJsonMergeError, match="different path"):
        merge_cbdt_fragment(
            fragment_path=_frag_file(tmp_path),
            itr_path=itr,
            output_path=itr,
        )


def test_inputs_never_mutated_on_refusal(tmp_path: Path) -> None:
    frag_path = _frag_file(tmp_path)
    itr = _itr_text(schedule_fa=json.dumps({"DtlsForeignEquityDebtInterest": [_a3_item()]}))
    itr_path = _itr_file(tmp_path, itr)
    before_frag = frag_path.read_bytes()
    before_itr = itr_path.read_bytes()
    with pytest.raises(CbdtJsonMergeError):
        merge_cbdt_fragment(
            fragment_path=frag_path, itr_path=itr_path, output_path=tmp_path / "out.json"
        )
    assert frag_path.read_bytes() == before_frag
    assert itr_path.read_bytes() == before_itr


def test_mid_write_failure_leaves_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Inject a failure during the atomic publish (after the temp is created) and
    # assert NO file is left at the output and no stray temp remains.
    def _boom(_fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(merge_mod.os, "fsync", _boom)
    with pytest.raises(OSError, match="disk full"):
        merge_cbdt_fragment(
            fragment_path=_frag_file(tmp_path),
            itr_path=_itr_file(tmp_path),
            output_path=tmp_path / "merged.json",
        )
    assert not (tmp_path / "merged.json").exists()
    assert not list(tmp_path.glob(".merged.json.*"))  # temp cleaned up


# --- a staging file that cannot be removed (#740) ---------------------------

_HANDLE_STEPS = frozenset({"write", "flush", "close"})


def _fail_steps(monkeypatch: pytest.MonkeyPatch, **errors: BaseException) -> None:
    """Make each named step of the publish raise its error: a method of the
    staging handle (``close`` after really closing), or else ``os.<step>``."""
    for step, error in errors.items():
        if step not in _HANDLE_STEPS:
            monkeypatch.setattr(os, step, Mock(side_effect=error))
    real_fdopen = os.fdopen

    def fdopen(*args: Any, **kwargs: Any) -> MagicMock:
        handle = real_fdopen(*args, **kwargs)
        wrapped = MagicMock(wraps=handle)
        # A context manager whose exit closes it, like the file object.
        wrapped.__enter__.return_value = wrapped
        wrapped.__exit__.side_effect = lambda *_exc: wrapped.close()
        wrapped.write.side_effect = errors.get("write")
        wrapped.flush.side_effect = errors.get("flush")

        def close() -> None:
            handle.close()
            if "close" in errors:
                raise errors["close"]

        wrapped.close.side_effect = close
        return wrapped

    if "fdopen" not in errors:
        monkeypatch.setattr(os, "fdopen", fdopen)


def _fail_staging_unlink(
    monkeypatch: pytest.MonkeyPatch,
    out: Path,
    *,
    before: Callable[[Path], None] | None = None,
    also_fail: Path | None = None,
    code: int | None = errno.EACCES,
    also_fail_code: int | None = errno.EACCES,
) -> list[Path]:
    """Make removing ``out``'s staging file raise :func:`_os_error` with
    ``code``, after calling ``before`` with it, and removing ``also_fail``
    raise one with ``also_fail_code``. Returns every path an unlink was
    attempted on."""
    original = Path.unlink
    attempted: list[Path] = []

    def unlink(path: Path, missing_ok: bool = False) -> None:
        attempted.append(path)
        staging = path.parent == out.parent and path.match(f".{out.name}.*.tmp")
        if staging and before is not None:
            before(path)
        if staging:
            raise _os_error(code)
        if path == also_fail:
            raise _os_error(also_fail_code)
        original(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    return attempted


def _discard_failure(out: Path) -> str:
    """The refusal when the staging file of an unpublished ``out`` stays (#1048)."""
    return (
        "the write failed, and its output may still be there: could not remove the "
        f"staging file {out.parent}/.{out.name}.*.tmp (EACCES)"
    )


def _rollback_report(out: Path, unpublish: str, code: int | None = errno.EACCES) -> str:
    """The refusal when the staging file of a published ``out`` stays, followed
    by ``unpublish``, what became of ``out`` (#1048)."""
    return (
        f"the merge could not remove its staging file {out.parent}/.{out.name}.*.tmp, "
        f"a second link to the merged return ({_ERRNO_TEXT[code]}); {unpublish}"
    )


def _assert_names_no_staging_file(message: str, staged: Path) -> None:
    """``message`` names the staging file by its pattern only: not by its
    random ``mkstemp`` name, nor the injected error's text or file name."""
    assert staged.name not in message
    _assert_not_echoed(message, _STRERROR_MARKER, _MARKER)


def _cli_merge(tmp_path: Path) -> int:
    return main(
        [
            "merge",
            "--fragment",
            str(_frag_file(tmp_path)),
            "--itr",
            str(_itr_file(tmp_path)),
            "--output",
            str(tmp_path / "merged.json"),
        ]
    )


@pytest.mark.parametrize("layer", ["api", "cli"])
@pytest.mark.parametrize("step", ["fdopen", "write", "flush", "fsync", "close", "link"])
def test_failed_publish_whose_staging_removal_fails_names_the_staging_file_pattern(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    step: str,
    layer: str,
) -> None:
    out = tmp_path / "merged.json"
    first_failure = OSError(f"{step} failed")
    _fail_steps(monkeypatch, **{step: first_failure})
    attempted = _fail_staging_unlink(monkeypatch, out)

    if layer == "cli":
        assert _cli_merge(tmp_path) == 4
        message = capsys.readouterr().err
        assert message == f"error: {_discard_failure(out)}\n"
    else:
        with pytest.raises(OutputRemovalError) as raised:
            _merge(tmp_path)
        assert_replaced_unchained(raised.value, markers=(_STRERROR_MARKER, _MARKER))
        message = str(raised.value)
        assert message == _discard_failure(out)

    (staged,) = attempted
    _assert_names_no_staging_file(message, staged)
    assert not out.exists()


@pytest.mark.parametrize(
    ("step", "close_error"),
    [
        ("write", OSError("close failed")),
        ("fsync", OSError("close failed")),
        ("write", KeyboardInterrupt()),
        ("flush", KeyboardInterrupt()),
        ("fsync", KeyboardInterrupt()),
    ],
    ids=[
        "write",
        "fsync",
        "write-close-interrupt",
        "flush-close-interrupt",
        "fsync-close-interrupt",
    ],
)
def test_close_failure_after_a_failed_write_keeps_none_of_that_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str, close_error: BaseException
) -> None:
    first_failure = OSError(f"{step} failed")
    _fail_steps(monkeypatch, **{step: first_failure, "close": close_error})
    _fail_staging_unlink(monkeypatch, tmp_path / "merged.json")

    with pytest.raises(OutputRemovalError) as raised:
        _merge(tmp_path)

    # No crash stand-in: an interrupted close did not replace the write's OSError.
    assert_replaced_unchained(raised.value, markers=(_STRERROR_MARKER, _MARKER))


def test_output_created_by_another_process_is_untouched_when_staging_removal_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "merged.json"
    real_link = os.link

    def link_after_another_process(src: str, dst: Path) -> None:
        out.write_text("THEIRS", encoding="utf-8")
        real_link(src, dst)

    monkeypatch.setattr(os, "link", link_after_another_process)
    attempted = _fail_staging_unlink(monkeypatch, out)

    assert _cli_merge(tmp_path) == 4
    (staged,) = attempted
    err = capsys.readouterr().err
    assert err == f"error: {_discard_failure(out)}\n"
    _assert_names_no_staging_file(err, staged)
    assert out.read_text(encoding="utf-8") == "THEIRS"


@pytest.mark.parametrize(
    "code", [errno.EACCES, None, 99999], ids=["EACCES", "no-errno", "unmapped"]
)
def test_published_merge_whose_staging_removal_fails_removes_its_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    code: int | None,
) -> None:
    out = tmp_path / "merged.json"
    attempted = _fail_staging_unlink(monkeypatch, out, code=code)

    assert _cli_merge(tmp_path) == 4
    staged = attempted[0]
    err = capsys.readouterr().err
    unpublish = f"removed --output {out}, so the merge published nothing"
    assert err == f"error: {_rollback_report(out, unpublish, code)}\n"
    _assert_names_no_staging_file(err, staged)
    assert staged.exists()
    assert not out.exists()


def test_published_merge_with_a_relative_output_names_the_relative_staging_pattern(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # #1048: mkstemp returns an absolute name even for a relative --output; the
    # refusal names the staging file beside the --output as typed.
    monkeypatch.chdir(tmp_path)
    fragment = _frag_file(tmp_path).name
    itr = _itr_file(tmp_path).name
    attempted = _fail_staging_unlink(monkeypatch, tmp_path / "merged.json")

    argv = ["merge", "--fragment", fragment, "--itr", itr, "--output", "merged.json"]
    assert main(argv) == 4
    err = capsys.readouterr().err
    assert err == (
        "error: the merge could not remove its staging file .merged.json.*.tmp, a second "
        "link to the merged return (EACCES); removed --output merged.json, so the merge "
        "published nothing\n"
    )
    _assert_names_no_staging_file(err, attempted[0])
    assert str(tmp_path) not in err
    assert not (tmp_path / "merged.json").exists()


@pytest.mark.parametrize("replacement", ["file", "symlink"])
def test_published_merge_keeps_an_output_replaced_before_its_rollback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    out = tmp_path / "merged.json"

    def replace(staged: Path) -> None:
        theirs = tmp_path / "theirs.json"
        if replacement == "file":
            theirs.write_text("THEIRS", encoding="utf-8")
        else:
            theirs.symlink_to(staged)
        theirs.replace(out)

    attempted = _fail_staging_unlink(monkeypatch, out, before=replace)

    assert _cli_merge(tmp_path) == 4
    err = capsys.readouterr().err
    staged = attempted[0]
    assert attempted == [staged]
    unpublish = f"did not remove --output {out}, which may not be this run's"
    assert err == f"error: {_rollback_report(out, unpublish)}\n"
    _assert_names_no_staging_file(err, staged)
    if replacement == "file":
        assert out.read_text(encoding="utf-8") == "THEIRS"
    else:
        assert out.readlink() == staged


@pytest.mark.parametrize("rollback_code", [errno.EACCES, None], ids=["EACCES", "no-errno"])
def test_published_merge_whose_rollback_fails_names_both_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    rollback_code: int | None,
) -> None:
    out = tmp_path / "merged.json"
    attempted = _fail_staging_unlink(monkeypatch, out, also_fail=out, also_fail_code=rollback_code)

    assert _cli_merge(tmp_path) == 4
    err = capsys.readouterr().err
    staged = attempted[0]
    assert attempted == [staged, out]
    unpublish = (
        f"could not remove --output {out} either, which still holds the merged return "
        f"({_ERRNO_TEXT[rollback_code]})"
    )
    assert err == f"error: {_rollback_report(out, unpublish)}\n"
    _assert_names_no_staging_file(err, staged)
    assert out.samefile(staged)


@pytest.mark.parametrize("lstat_code", [errno.EACCES, 99999], ids=["EACCES", "unmapped"])
def test_published_merge_whose_output_cannot_be_checked_keeps_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    lstat_code: int,
) -> None:
    out = tmp_path / "merged.json"
    original_lstat = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        if path == out:
            raise _os_error(lstat_code)
        return original_lstat(path)

    attempted = _fail_staging_unlink(
        monkeypatch, out, before=lambda _staged: monkeypatch.setattr(Path, "lstat", lstat)
    )

    assert _cli_merge(tmp_path) == 4
    err = capsys.readouterr().err
    staged = attempted[0]
    assert attempted == [staged]
    unpublish = (
        f"did not remove --output {out}, which may not be this run's ({_ERRNO_TEXT[lstat_code]})"
    )
    assert err == f"error: {_rollback_report(out, unpublish)}\n"
    _assert_names_no_staging_file(err, staged)
    assert out.samefile(staged)


# --- CLI wiring ------------------------------------------------------------


def test_cli_merge_exit_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "merge",
            "--fragment",
            str(_frag_file(tmp_path)),
            "--itr",
            str(_itr_file(tmp_path)),
            "--output",
            str(tmp_path / "merged.json"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Merged the Schedule FA fragment" in out
    assert (tmp_path / "merged.json").exists()


def test_cli_merge_refusal_exit_4(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    itr = _itr_text(schedule_fa=json.dumps({"DtlsForeignEquityDebtInterest": [_a3_item()]}))
    rc = main(
        [
            "merge",
            "--fragment",
            str(_frag_file(tmp_path)),
            "--itr",
            str(_itr_file(tmp_path, itr)),
            "--output",
            str(tmp_path / "merged.json"),
        ]
    )
    assert rc == 4
    assert "already has Table A3" in capsys.readouterr().err


def test_cli_merge_account_conflict_does_not_print_the_account(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # #1048: a second merge into a return that already lists the account.
    existing = _a2_item(AccountNumber="U1234567")
    itr = _itr_text(schedule_fa=json.dumps({"DtlsForeignCustodialAcc": [existing]}))
    rc = main(
        [
            "merge",
            "--fragment",
            str(_frag_file(tmp_path)),
            "--itr",
            str(_itr_file(tmp_path, itr)),
            "--output",
            str(tmp_path / "merged.json"),
        ]
    )
    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {_ACCOUNT_CONFLICT.format(row=1, existing=1)}\n"
    _assert_not_echoed(captured.err + captured.out, "U1234567")
    assert not (tmp_path / "merged.json").exists()


@pytest.mark.parametrize(
    ("itr_form", "rows", "message", "values"),
    [
        pytest.param(itr_form, rows, message, values, id=f"{case_id}-{itr_form}")
        for case_id, forms, rows, message, values in _A2_ROW_REFUSALS
        for itr_form in forms
    ],
)
def test_cli_merge_refuses_an_a2_row_the_writer_cannot_build(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    itr_form: CbdtItrForm,
    rows: list[dict[str, Any]],
    message: str,
    values: tuple[str, ...],
) -> None:
    # #1075: a masked or non-ASCII account number, or a balance below the CBDT bound.
    out = tmp_path / "merged.json"
    rc = main(
        [
            "merge",
            "--fragment",
            str(_frag_file(tmp_path, _fragment_a2_rows(*rows, itr_form=itr_form))),
            "--itr",
            str(_itr_file(tmp_path, itr_form=itr_form)),
            "--output",
            str(out),
        ]
    )
    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {message}\n"
    _assert_not_echoed(captured.err + captured.out, *values)
    assert not out.exists()


def test_cli_merge_requires_all_args(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["merge", "--fragment", str(tmp_path / "f.json")])
    assert exc.value.code == 2  # argparse usage error
