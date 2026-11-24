"""Pin the content of every Robinhood ``schedule-cg.xlsx`` the filing tests write (#642).

Each scenario goes through :func:`pipeline_robinhood_outputs.write_cg`, the
production entry point, in both ITR forms, with and without ``--debug``. The
workbook is compared by content, not zip bytes, because openpyxl stamps
creation times: sheet titles, cell values, merged ranges, fonts, fills,
borders, alignment, protection, number formats, column and row dimensions,
freeze panes, auto-filter, print titles and page setup. The digests were
captured from the Robinhood-only writer at 22cd3c4, before the shared
filing writer replaced it. The digests whose notes name the sold security
were recaptured when that security became synthetic (#1070); only its
ticker and CUSIP changed. Every digest of a workbook with a section sheet was
recaptured when each section gained its Schedule CG Table F rows (#1360),
and every digest of a workbook with a blank Table F row when that row's
Basis gained the completion guidance (#1379). Every digest of a workbook with
a section sheet was recaptured again when Robinhood's recorded sale Amount
became each sale's consideration (#1383): each section is filled and its
note states that position. Then four scenarios built exactly another's lots
and were deleted (``unproven-stcg`` was ``component-rounding-note``,
``unproven-ltcg`` was ``ltcg-loss``, ``unproven-both`` was ``both-sections``,
and ``accounts-sharing-a-label`` was ``stcg-loss`` sold from another account,
which the workbook doesn't name), and four were renamed for their lots.
The ``--debug`` digests of the three workbooks with an LTCG section were
recaptured when its (e) note named section 54F instead of section 54 (#1555).
Each workbook's complete raw bytes are also compared,
under a fixed clock, with the #1199 baseline in
``tests/fixtures/portal_parity_baseline.json``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import tostring

import pytest
from openpyxl import load_workbook

from ibkr_fa import pipeline_robinhood_outputs as outputs
from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.robinhood_capital_gains import (
    RobinhoodCapitalGainLot,
    RobinhoodCapitalGainsResult,
)
from ibkr_fa.pipeline import FinancialYear
from tests._portal_parity import assert_workbook_matches, baseline_cases, expected, fix_xlsx_clock
from tests._robinhood_cg import ACCOUNT_LABELS, NO_OPTIONS, cg_result, sale

_FY = FinancialYear(2025)
_ITR_FORMS = ("ITR-2", "ITR-3")
_DEBUG_MODES = (False, True)


def _nyse_sale() -> RobinhoodCapitalGainLot:
    lot = sale("12", "10", country="GB", lot_ordinal=3, sale_ordinal=4)
    return replace(lot, closed_lot=replace(lot.closed_lot, listing_exchange="NYSE"))


_SCENARIOS: dict[str, Callable[[], RobinhoodCapitalGainsResult]] = {
    "stcg-loss": lambda: cg_result(sale("12", "10")),
    "ltcg-loss": lambda: cg_result(sale("12", "10", long_term=True)),
    "both-sections": lambda: cg_result(
        sale("12", "10"),
        sale("12", "10", long_term=True, lot_ordinal=3, sale_ordinal=4),
    ),
    "two-securities": lambda: cg_result(sale("12", "10"), _nyse_sale()),
    "empty": cg_result,
    "all-editable-round-to-zero": lambda: cg_result(sale("0.004", "0.004")),
    "component-rounding-note": lambda: cg_result(sale("12.006", "10.004")),
    "stcg-losses-in-two-bands": lambda: cg_result(
        sale("12", "10"),
        sale("5", "4", sale_date=date(2025, 7, 1), lot_ordinal=3, sale_ordinal=4),
    ),
    "both-sections-rounding-note": lambda: cg_result(
        sale("12.006", "10.004"),
        sale("12.006", "10.004", long_term=True, lot_ordinal=3, sale_ordinal=4),
    ),
    "split-sale-and-another-sale": lambda: cg_result(
        sale("6", "5", lot_ordinal=1, sale_ordinal=10),
        sale("7", "6", lot_ordinal=2, sale_ordinal=10),
        sale("8", "7", sale_date=date(2025, 7, 15), lot_ordinal=3, sale_ordinal=11),
    ),
    "rounded-zero-stcg-balance": lambda: cg_result(sale("10.003", "10.004")),
}


_GOLDEN_SHA256: dict[str, dict[str, str]] = {
    "stcg-loss": {
        "ITR-2/default": "c00edeb8a3251e899850bd09b8a2f8d0220edc9b5404feced758125de4384f2d",
        "ITR-2/debug": "2f68c7f743cd07841f9b9b2389971b888b28147bea45aa234a3a5c5d1d1d350e",
        "ITR-3/default": "13bb3094ba2440648f89935ca527d0f294d40f15e2e95477072c5ac6c1519399",
        "ITR-3/debug": "d4c9721e2438b0f3dbd80290e1a47f37445fc2a28c928e8c522385bc08b05626",
    },
    "ltcg-loss": {
        "ITR-2/default": "45e123124ecced33b3a533c5cd17e0611048106e161e3edd57e4f27416227742",
        "ITR-2/debug": "0612fb368ece93e0a3c64ef34eb7f687ce6e50ad451c32cb80dd855e5c4ec8ee",
        "ITR-3/default": "9d13abe2cbe1e57e34e901e5838d648217529e97c48f83cf61e5575afa060b00",
        "ITR-3/debug": "bb9c4550328d4cfcdfc3b3f8397576abf8ca469112c4abb203420002fd1dae03",
    },
    "both-sections": {
        "ITR-2/default": "3395fa7a7026559201d9daa096c9300edd64eaa48a2ae477e4ffbf2bc3af4bad",
        "ITR-2/debug": "833aa8fb398f215fbe8a72629f7722558a52357272e639a2b7fc2b74adc556f2",
        "ITR-3/default": "173bbb7d75ec2847b21d9c15c511aaa152d526cd108a7a2cb24ecac43a7b20f9",
        "ITR-3/debug": "2f206d45d72bb54812aa716b48fbd825dd2e86375e87076d45f0b5d2d43eaef3",
    },
    "two-securities": {
        "ITR-2/default": "8813d8320b7b56401a52e40ce24f9116c16a703cfc0f8f8d3cef9fb9767b90d5",
        "ITR-2/debug": "5a66522d2b4a4e6100f0a4e76268eedb090f69b7f7107e21ea26bd6dd156b497",
        "ITR-3/default": "c25c9a7a91774d466dec8f3cb48c112c066a473ba6bbb9a0047d10177f6d85f1",
        "ITR-3/debug": "742b518da54d2489735412a4a9df44a4d9e7779064006d46b60c4f873340bc0c",
    },
    "empty": {
        "ITR-2/default": "c4bd5c96fe859cecd580148fa0afa139feadee72a4a2a190bf6e0be55ff49013",
        "ITR-2/debug": "c4bd5c96fe859cecd580148fa0afa139feadee72a4a2a190bf6e0be55ff49013",
        "ITR-3/default": "795bca5d0dadcd93c614f30e6a1a00353737e4e0af83de0066fb589fe835da54",
        "ITR-3/debug": "795bca5d0dadcd93c614f30e6a1a00353737e4e0af83de0066fb589fe835da54",
    },
    "all-editable-round-to-zero": {
        "ITR-2/default": "77020570510281e1a9509c0c8bb889b1e453a778b75bc3d7e4a5ff9cd95b6b7d",
        "ITR-2/debug": "0f8fa31c89a02d6e919ca29e369786c302e2346e8a7e1bcb5a86e08899de07ac",
        "ITR-3/default": "2ee7afdd0b61fc57261321eb9197615b9cd0657bac9b1e91c1586b3e7f4b1c57",
        "ITR-3/debug": "13cf06bb525a5c890d9311e0e7bb48c2c8a8a627e63ed5180d45dcb6e0835bec",
    },
    "component-rounding-note": {
        "ITR-2/default": "762f4ee0301481ad3a072cd62d1b980521e355634fa9f39ff5f52f8f1055dda7",
        "ITR-2/debug": "f74d6a6bd78145e78d9f4b8a608442f7e0ee22fb2cd196693f42af246d52a592",
        "ITR-3/default": "9bddb6154665b12092e0c7d97f9e85e4139c8eeebd51e229ec471271c3d71551",
        "ITR-3/debug": "3a40b388f7d0a4eee12f2dce310581441e5d453f201b29d94fd60d98f4e71170",
    },
    "stcg-losses-in-two-bands": {
        "ITR-2/default": "20edd128ef4b0e4545030aa046b3832bb3b7a585670dbf0cefaf8b2b4e8d0f8a",
        "ITR-2/debug": "71466fd399a169824596ce47eb3a3cb0bbd0ded4ebd522d0c5ad0e13dd538163",
        "ITR-3/default": "f8ed00aca79bdc6588e1b84758e3b1669400f4adf6e204093362d70d008efb5b",
        "ITR-3/debug": "3bfa16d64fe1c1e99978c0e5d894f3bbc9ce97e5fa4b868379da528ad00ad854",
    },
    "both-sections-rounding-note": {
        "ITR-2/default": "01f7a2ee4fa87a978903cd898a493a58f8e6c4b313742c8d6751e98d7b0a2703",
        "ITR-2/debug": "e8d4b1a596198bbcfd6a288d115b3d368e54640bb7f05dce8c8fd3e6bb703f60",
        "ITR-3/default": "842dd489bb8b8d154042e8df6c66b7b09028dc7a732ecf1a842964904b671496",
        "ITR-3/debug": "9c7475ef9d5ccbbd25e27ce40266fafa1f15b905ea61aa8768a020b547bc74d9",
    },
    "split-sale-and-another-sale": {
        "ITR-2/default": "062800d8287a46deac06e60787f99114239447b48ac882ca0f7f55116deae79b",
        "ITR-2/debug": "0c9102fc9b97b911a2da015480bd2dc3d29204dbf8761a3b1c563f64152c941f",
        "ITR-3/default": "b288f7f7d25915e64a1ec4f92c7218494a01bd96133dcb74bebe2f9f4534d08b",
        "ITR-3/debug": "f663fedecd1a66816263dd44bfca3172ee8293765722b6a81b26e163f8a9c3ac",
    },
    "rounded-zero-stcg-balance": {
        "ITR-2/default": "4f3a22fc9f29dd78d7a8a737902284e43087454efcb19c093daae24a32f83324",
        "ITR-2/debug": "2d9afdb2c718bc7b42b7e2aa6748ca1cc588cc10ca79f04fee383ca88edb52fc",
        "ITR-3/default": "b06940d580d66cf47e90f62593c990a24f7329a14fe35c1337445e47407128e4",
        "ITR-3/debug": "dbafaf7608549331221d5d58448d61a741c90bd83046720f9f26c2a0ffa3965d",
    },
}


def _xml(style: Any) -> str:
    return tostring(style.to_tree(), encoding="unicode")


def _sheet(worksheet: Any) -> dict[str, object]:
    return {
        "title": worksheet.title,
        "cells": [
            [
                [
                    cell.value,
                    cell.number_format,
                    *(
                        _xml(style)
                        for style in (
                            cell.font,
                            cell.fill,
                            cell.border,
                            cell.alignment,
                            cell.protection,
                        )
                    ),
                ]
                for cell in row
            ]
            for row in worksheet.iter_rows()
        ],
        "merged": sorted(str(merged) for merged in worksheet.merged_cells.ranges),
        "columns": {key: dict(dimension) for key, dimension in worksheet.column_dimensions.items()},
        "rows": {str(key): dict(dimension) for key, dimension in worksheet.row_dimensions.items()},
        "freeze_panes": worksheet.freeze_panes,
        "auto_filter": worksheet.auto_filter.ref,
        "print_title_rows": worksheet.print_title_rows,
        "grid_lines": worksheet.sheet_view.showGridLines,
        "page_setup": [
            worksheet.page_setup.orientation,
            worksheet.page_setup.fitToWidth,
            worksheet.page_setup.fitToHeight,
            worksheet.sheet_properties.pageSetUpPr.fitToPage,
        ],
    }


def _digest(path: Path) -> str:
    workbook = load_workbook(path)
    try:
        snapshot = [_sheet(worksheet) for worksheet in workbook.worksheets]
    finally:
        workbook.close()
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def _write_cg(tmp_path: Path, scenario: str, itr_form: ItrForm, *, debug: bool) -> Path:
    outputs.write_cg(
        _SCENARIOS[scenario](),
        options=NO_OPTIONS,
        output_dir=tmp_path,
        format_id="review-csv",
        financial_year=_FY,
        itr_form=itr_form,
        debug=debug,
        account_labels=ACCOUNT_LABELS,
    )
    return tmp_path / "schedule-cg.xlsx"


@pytest.mark.parametrize("debug", _DEBUG_MODES, ids=["default", "debug"])
@pytest.mark.parametrize("itr_form", _ITR_FORMS)
@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_robinhood_filing_workbook_content_is_pinned(
    tmp_path: Path, scenario: str, itr_form: ItrForm, debug: bool
) -> None:
    path = _write_cg(tmp_path, scenario, itr_form, debug=debug)

    mode = f"{itr_form}/{'debug' if debug else 'default'}"
    assert _digest(path) == _GOLDEN_SHA256[scenario][mode]


@pytest.mark.parametrize("debug", _DEBUG_MODES, ids=["default", "debug"])
@pytest.mark.parametrize("itr_form", _ITR_FORMS)
@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_robinhood_filing_workbook_bytes_match_the_portal_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, itr_form: ItrForm, debug: bool
) -> None:
    """Every byte of the workbook, docProps/core.xml included (#1199)."""
    fix_xlsx_clock(monkeypatch)
    path = _write_cg(tmp_path, scenario, itr_form, debug=debug)

    mode = f"{itr_form}/{'debug' if debug else 'default'}"
    assert_workbook_matches(
        path.read_bytes(), expected("schedule-cg-filing-xlsx", f"{scenario}/{mode}")
    )


def test_every_golden_digest_has_a_scenario() -> None:
    modes = {
        f"{itr_form}/{'debug' if debug else 'default'}"
        for itr_form in _ITR_FORMS
        for debug in _DEBUG_MODES
    }
    digests = {scenario: set(by_mode) for scenario, by_mode in _GOLDEN_SHA256.items()}
    assert digests == dict.fromkeys(_SCENARIOS, modes)
    assert {f"{scenario}/{mode}" for scenario in _SCENARIOS for mode in modes} == baseline_cases(
        "schedule-cg-filing-xlsx"
    )
