"""Saved ITR returns and CBDT JSON fragments for ``ibkr-fa merge``, shared by
the merge CLI tests and the report render cleanup tests (#1941).

- ``merge_a2_item`` and ``merge_a3_item``: a valid Table A2 and Table A3 item
  of a fragment, updated with ``over``;
- ``merge_fragment``: a valid fragment with those items (``_a3_item_list``: its
  A3 items) and the ``_ibkrFa`` marker for ``itr_form`` (``legacy``: without
  ``itrForm``);
- ``itr_form_header``: the form header of a saved return; ``saved_itr_text``: a
  saved ITR as raw text, with ``itr2_extra`` after the header and an optional
  ``ScheduleFA``;
- ``write_fragment_file``: writes a fragment to ``fragment.json`` (``_write``:
  as indented JSON); ``write_itr_file``: writes a saved ITR's text to the
  form's file, e.g. ``itr-2.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ibkr_fa.output.cbdt_json_schema import CbdtItrForm, schema_version_for


def merge_a2_item(**over: Any) -> dict[str, Any]:
    item = {
        "CountryName": "United States",
        "CountryCodeExcludingIndia": "2",
        "FinancialInstName": "Interactive Brokers LLC",
        "FinancialInstAddress": "One Pickwick Plaza, Greenwich, CT",
        "ZipCode": "06830",
        "AccountNumber": "U1234567",
        "Status": "BENEFICIAL_OWNER",
        "AccOpenDate": "2024-02-01",
        "PeakBalanceDuringPeriod": 250000,
        "ClosingBalance": 8950,
        "GrossAmtPaidCredited": 1235,
        "NatureOfAmount": "D",
    }
    item.update(over)
    return item


def merge_a3_item(**over: Any) -> dict[str, Any]:
    item = {
        "CountryName": "United States",
        "CountryCodeExcludingIndia": "2",
        "NameOfEntity": "EXAMPLE HOLDINGS INC",
        "AddressOfEntity": "1 Example Plaza, Anytown, NY",
        "ZipCode": "99999",
        "NatureOfEntity": "Listed Company",
        "InterestAcquiringDate": "2024-03-15",
        "InitialValOfInvstmnt": 64218,
        "PeakBalanceDuringPeriod": 85001,
        "ClosingBalance": 83000,
        "TotGrossAmtPaidCredited": 501,
        "TotGrossProceeds": 0,
    }
    item.update(over)
    return item


def merge_fragment(
    a2: dict[str, Any] | None = None,
    a3: list[dict[str, Any]] | None = None,
    *,
    itr_form: CbdtItrForm = "ITR-2",
    legacy: bool = False,
) -> dict[str, Any]:
    marker = {
        "format": "cbdt-json-fragment",
        "schemaVersion": schema_version_for(itr_form),
    }
    if not legacy:
        marker["itrForm"] = itr_form
    return {
        "_ibkrFa": marker,
        "ScheduleFA": {
            "DtlsForeignCustodialAcc": [a2 if a2 is not None else merge_a2_item()],
            "DtlsForeignEquityDebtInterest": _a3_item_list(a3),
        },
    }


def _a3_item_list(a3: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if a3 is not None:
        return a3
    return [merge_a3_item()]


def itr_form_header(itr_form: CbdtItrForm = "ITR-2") -> dict[str, Any]:
    return {
        "FormName": itr_form,
        "Description": "For Individuals and HUFs",
        "AssessmentYear": "2026",
        "SchemaVer": "Ver1.0",
        "FormVer": "Ver1.0",
    }


def saved_itr_text(
    itr2_extra: str = "",
    schedule_fa: str | None = None,
    *,
    itr_form: CbdtItrForm = "ITR-2",
) -> str:
    """A saved ITR as RAW TEXT (so exact number formatting is testable)."""
    return_key = itr_form.replace("-", "")
    form_key = f"Form_{return_key}"
    sfa = f',\n      "ScheduleFA": {schedule_fa}' if schedule_fa is not None else ""
    return (
        f'{{\n  "ITR": {{\n    "{return_key}": {{\n'
        f'      "{form_key}": {json.dumps(itr_form_header(itr_form))}'
        f"{itr2_extra}{sfa}\n"
        "    }\n  }\n}\n"
    )


def _write(path: Path, obj: Any) -> Path:
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return path


def write_fragment_file(
    tmp_path: Path,
    frag: dict[str, Any] | None = None,
    *,
    itr_form: CbdtItrForm = "ITR-2",
) -> Path:
    return _write(
        tmp_path / "fragment.json",
        frag if frag is not None else merge_fragment(itr_form=itr_form),
    )


def write_itr_file(
    tmp_path: Path, text: str | None = None, *, itr_form: CbdtItrForm = "ITR-2"
) -> Path:
    p = tmp_path / f"{itr_form.lower()}.json"
    p.write_text(text if text is not None else saved_itr_text(itr_form=itr_form), encoding="utf-8")
    return p
