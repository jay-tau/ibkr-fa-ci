"""End-to-end Table F block of the Schedule CG review files (#1361), through ``main``.

Example 1 is ``tests/fixtures/e2e_activity_statement_schedule_cg_stcg_fy2025_26.csv``:
one short-term sale on 20/11/2025, band (iii), gaining ₹26,460, and one
long-term sale on 10/08/2025, band (ii), gaining ₹69,720.

``e2e_activity_statement_schedule_cg_table_f_loss_across_bands_fy2025_26.csv``
holds three short-term sales, each of 10 shares bought at USD 100: XQTA sold on
20/05/2025 (band (i)) at USD 120, XQTB on 15/10/2025 (band (iii)) at USD 90 and
XQTD on 20/01/2026 (band (iv)) at USD 130. At the April, September and December
month-end TTBRs (84.25, 88.35, 89.47) they gain ₹16,850, lose ₹8,835 and gain
₹26,841, netting ₹34,856: a loss band beside two gain bands, so no band is
computed.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from ibkr_fa.cli import main
from ibkr_fa.compute.schedule_cg_table_f import ScheduleCgTableFCase
from ibkr_fa.output import EXCLUDED_LTCG_DEFERRED_MARKER, EXCLUDED_STCG_MARKER
from ibkr_fa.output.portal_labels import (
    PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG,
    PORTAL_FIELD_LABEL_CG_FOREIGN_STCG,
)
from ibkr_fa.output.schedule_cg_review_csv import TABLE_F_MARKER
from ibkr_fa.output.schedules.cg_filing import TABLE_F_BLANK_BASIS
from tests._ibkr_user_toml import US_PERSON_USER_TOML as _USER_TOML
from tests._trades_totals import with_trades_totals

_FIXTURES = Path(__file__).parent / "fixtures"
_EXAMPLE = _FIXTURES / "e2e_activity_statement_schedule_cg_stcg_fy2025_26.csv"
_LOSS_ACROSS_BANDS = (
    _FIXTURES / "e2e_activity_statement_schedule_cg_table_f_loss_across_bands_fy2025_26.csv"
)
_LOSS_REASON = TABLE_F_BLANK_BASIS[ScheduleCgTableFCase.LOSS_ACROSS_BANDS]
# XQTA's sale row in the loss-across-bands fixture.
_XQTA_SALE = "-10,120.00,120.00,1200.00,0,-1000.00,200.00,0,C"


def _manual_fill_line(row: int, file_name: str) -> str:
    return (
        f"⚠️  MANUAL-FILL: Schedule CG Table F row {row} needs its date bands entered by "
        f"hand: a band nets a loss and two or more bands net gains; {file_name} leaves "
        "them blank."
    )


def _statement(tmp_path: Path, text: str) -> Path:
    """A variant of a fixture, its Trades totals rebuilt from its rows."""
    path = tmp_path / "statement.csv"
    path.write_text(with_trades_totals(text), encoding="utf-8")
    return path


def _schedule_cg(
    tmp_path: Path,
    statement: Path,
    *,
    command: str = "schedule-cg",
    format_id: str = "review-csv",
    quiet: bool = False,
) -> tuple[int, Path]:
    config = tmp_path / "user.toml"
    config.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / f"{command}.{'xlsx' if format_id == 'xlsx' else 'csv'}"
    argv = [
        command,
        "--input",
        str(statement),
        "--config",
        str(config),
        "--output",
        str(out),
        "--financial-year",
        "2025-26",
        "--itr-form",
        "ITR-2",
        "--format",
        format_id,
        *(["--quiet"] if quiet else []),
    ]
    return main(argv), out


def _rows(path: Path) -> list[list[object]]:
    """Every row of a review CSV or XLSX; a blank cell is ``""``."""
    if path.suffix == ".xlsx":
        sheet = load_workbook(path).active
        assert sheet is not None
        return [
            ["" if value is None else value for value in row]
            for row in sheet.iter_rows(values_only=True)
        ]
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [list[object](row) for row in csv.reader(handle)]


def _band_cells(rows: list[list[object]]) -> list[tuple[object, object]]:
    """Each band row's second and last cell: the five rows above the note,
    audit TOTAL and portal TOTAL rows."""
    assert rows[-10:-8] == [[""] * 16, [TABLE_F_MARKER, *[""] * 15]]
    return [(row[1], row[15]) for row in rows[-8:-3]]


@pytest.mark.parametrize("format_id", ["review-csv", "xlsx"])
@pytest.mark.parametrize(
    ("command", "audit_marker", "portal_label", "bands"),
    [
        pytest.param(
            "schedule-cg",
            EXCLUDED_LTCG_DEFERRED_MARKER,
            PORTAL_FIELD_LABEL_CG_FOREIGN_STCG,
            [0, 0, 26460, 0, 0],
            id="stcg",
        ),
        pytest.param(
            "schedule-cg-ltcg",
            EXCLUDED_STCG_MARKER,
            PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG,
            [0, 69720, 0, 0, 0],
            id="ltcg",
        ),
    ],
)
def test_example_bands_add_up_to_the_total(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    audit_marker: str,
    portal_label: str,
    bands: list[int],
    format_id: str,
) -> None:
    """Example 1: the sale's band holds the whole total, after the audit
    block and before the audit TOTAL; the portal TOTAL row stays last."""
    code, out = _schedule_cg(tmp_path, _EXAMPLE, command=command, format_id=format_id)
    rows = _rows(out)

    assert code == 0
    assert "MANUAL-FILL" not in capsys.readouterr().err
    first_cells = [row[0] for row in rows]
    assert first_cells.index(audit_marker) < first_cells.index(TABLE_F_MARKER)
    assert rows[-2][0] == "_audit: TOTAL Realized gain INR (full precision)"
    assert rows[-1][0] == portal_label
    amounts = [int(str(amount)) for _, amount in _band_cells(rows)]
    assert amounts == bands
    assert sum(amounts) == int(str(rows[-1][15]))


@pytest.mark.parametrize("quiet", [False, True], ids=["verbose", "quiet"])
@pytest.mark.parametrize("format_id", ["review-csv", "xlsx"])
def test_loss_across_bands_exits_3_with_blank_bands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], format_id: str, quiet: bool
) -> None:
    code, out = _schedule_cg(tmp_path, _LOSS_ACROSS_BANDS, format_id=format_id, quiet=quiet)
    captured = capsys.readouterr()

    assert code == 3
    assert _band_cells(_rows(out)) == [(_LOSS_REASON, "")] * 5
    assert captured.err.count("MANUAL-FILL") == 1
    assert _manual_fill_line(3, out.name) in captured.err.splitlines()
    assert "MANUAL-FILL" not in captured.out
    if quiet:
        assert captured.out == "34856\n"


def test_ltcg_loss_across_bands_exits_3_with_blank_bands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same three sales, bought two years earlier, are long-term."""
    text = _LOSS_ACROSS_BANDS.read_text(encoding="utf-8")
    for old, new in (
        ("April 1, 2025 - March 31, 2026", "April 1, 2023 - March 31, 2026"),
        ('"2025-04-10', '"2023-04-10'),
        ('"2025-07-01', '"2023-07-01'),
        ('"2025-08-01', '"2023-08-01'),
    ):
        assert text.count(old) == 1
        text = text.replace(old, new)

    code, out = _schedule_cg(tmp_path, _statement(tmp_path, text), command="schedule-cg-ltcg")
    captured = capsys.readouterr()

    assert code == 3
    assert _band_cells(_rows(out)) == [(_LOSS_REASON, "")] * 5
    assert captured.err.count("MANUAL-FILL") == 1
    assert _manual_fill_line(5, out.name) in captured.err.splitlines()


def test_net_loss_bands_are_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """XQTD sold at USD 50 instead loses ₹44,735, so the section nets a loss
    and adds nothing to Table F."""
    text = _LOSS_ACROSS_BANDS.read_text(encoding="utf-8")
    old = "-10,130.00,130.00,1300.00,0,-1000.00,300.00,0,C"
    assert text.count(old) == 1
    text = text.replace(old, "-10,50.00,50.00,500.00,0,-1000.00,-500.00,0,C")

    code, out = _schedule_cg(tmp_path, _statement(tmp_path, text))
    rows = _rows(out)

    assert code == 0
    assert "MANUAL-FILL" not in capsys.readouterr().err
    assert _band_cells(rows) == [("", "0")] * 5
    assert rows[-1][15] == "'-36720"


@pytest.mark.parametrize("quiet", [False, True], ids=["verbose", "quiet"])
def test_loss_across_bands_prints_no_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], quiet: bool
) -> None:
    """A digit-run marker in XQTA's sale price reaches the review file only."""
    marker = "731942"
    text = _LOSS_ACROSS_BANDS.read_text(encoding="utf-8")
    assert text.count(_XQTA_SALE) == 1
    text = text.replace(
        _XQTA_SALE, f"-10,{marker}.00,{marker}.00,{marker}0.00,0,-1000.00,7318420.00,0,C"
    )

    code, out = _schedule_cg(tmp_path, _statement(tmp_path, text), quiet=quiet)
    captured = capsys.readouterr()

    assert code == 3
    assert _band_cells(_rows(out)) == [(_LOSS_REASON, "")] * 5
    assert marker in out.read_text(encoding="utf-8-sig")
    assert marker not in captured.out
    assert marker not in captured.err


@pytest.mark.parametrize("quiet", [False, True], ids=["verbose", "quiet"])
def test_run_all_loss_across_bands_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], quiet: bool
) -> None:
    config = tmp_path / "user.toml"
    config.write_text(_USER_TOML, encoding="utf-8")
    out_dir = tmp_path / "out"
    code = main(
        [
            "run-all",
            "--fy-input",
            str(_LOSS_ACROSS_BANDS),
            "--config",
            str(config),
            "--output-dir",
            str(out_dir),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
            "--format",
            "review-csv",
            *(["--quiet"] if quiet else []),
        ]
    )
    captured = capsys.readouterr()

    assert code == 3
    assert _band_cells(_rows(out_dir / "schedule-cg.csv")) == [(_LOSS_REASON, "")] * 5
    assert _band_cells(_rows(out_dir / "schedule-cg-ltcg.csv")) == [("", "0")] * 5
    assert captured.err.count("MANUAL-FILL") == 1
    assert _manual_fill_line(3, "schedule-cg.csv") in captured.err.splitlines()
    assert f"  manual-fill  schedule-cg       {out_dir / 'schedule-cg.csv'}" in captured.err


# ---- Controls: only the Table F block changes --------------------------------

# Taken at c70b792, main before #1361, with the same runs: each file's SHA-256
# (an XLSX's as the JSON of its cell values) and stdout's and stderr's, with the
# run's tmp path as "<tmp>". The branch's file drops its Table F block, and the
# loss-across-bands run's stderr drops its manual-fill line, before hashing.
_MAIN_SHA256 = {
    ("example", "schedule-cg", "review-csv"): {
        "file": "f7a9614c3d1ed24ad6fbc5e0dd052fd5810fe59b289218b060d1a94461846aa8",
        "stdout": "ce8d1ee1622faa4e8163192e7d72b9a7b72b468b80a1d7e8b4e7167adca58457",
        "stderr": "4c3f08db91acabc3f99a2b9e0c067eb3a546aef8b3535e9ba2c26a74ae154016",
    },
    ("example", "schedule-cg", "xlsx"): {
        "file": "2debe3cf375eedc0839863ca047b2763851ebd34d66fad8c34b9c6516966c5d3",
        "stdout": "962b5acd67e1284207478a12dd40ee0a4045754f4d6f336fdce9a9d678af1635",
        "stderr": "4c3f08db91acabc3f99a2b9e0c067eb3a546aef8b3535e9ba2c26a74ae154016",
    },
    ("example", "schedule-cg-ltcg", "review-csv"): {
        "file": "1fea84d9d21b3786418c180aa4fb655f4a195f1d9249c6cebec0639bc6d33477",
        "stdout": "2dc1e8ee81c8d451888101e7d493c19bfbf0ee720fa0d7154f28b63ea37d1fc5",
        "stderr": "af225139f138b5951a7492d2c5eb67aea1981bbed4c182e2671d0b1de619d707",
    },
    ("example", "schedule-cg-ltcg", "xlsx"): {
        "file": "9c5e148c2214b7e373441ab955cf29813503c2c41d941ca855eb4d3764c9ec2f",
        "stdout": "6a23bdc0c5986a5021fc2271f9328f072f1ffc52fd92a739e5db146cb797a65b",
        "stderr": "af225139f138b5951a7492d2c5eb67aea1981bbed4c182e2671d0b1de619d707",
    },
    ("loss-across-bands", "schedule-cg", "review-csv"): {
        "file": "b668cf027142dc9c0c3de653b3dd3cb626fe9abd141ee70f9a13dda278827f63",
        "stdout": "129893b53ae16696af40218ca2e2e5014dd08d40ea96b7858cbbe2ee4fd6378f",
        "stderr": "88a77ee07b4641562ecc24ca979b8ffd676e6c6ad901953b58d65baf03c0435b",
    },
    ("loss-across-bands", "schedule-cg", "xlsx"): {
        "file": "f9718aa5698b1f1511068d712fa2532e98e9f30ea356c24b2c6a2e86e3e0cfc7",
        "stdout": "0719d049946771c603c87de87f46f460f011b079843cf31d7c03b84c0becfa5d",
        "stderr": "88a77ee07b4641562ecc24ca979b8ffd676e6c6ad901953b58d65baf03c0435b",
    },
    ("loss-across-bands", "schedule-cg-ltcg", "review-csv"): {
        "file": "c58f1e3c023cb2eb88afdd13bba86311b761c728797bf784da8ec64b553e744f",
        "stdout": "a9ff519d0c57e991457fd2b31441cb763e20f271e415fcfbc567e1f5dbf5b056",
        "stderr": "2871f2892c179c0cfc1f6d35822b5820753913c538fec2602760c44f96982cfe",
    },
    ("loss-across-bands", "schedule-cg-ltcg", "xlsx"): {
        "file": "a4259245ea7043ccc850c8694790fd25516a61a3c6c515a9286d3e5f0ac48974",
        "stdout": "2d268355ff9c242308dd3d5bc8ac31d607241c599b8a3b46c42fcbf35c4f924c",
        "stderr": "2871f2892c179c0cfc1f6d35822b5820753913c538fec2602760c44f96982cfe",
    },
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _without_block(out: Path) -> str:
    """The SHA-256 of ``out`` without its Table F block: the blank row, the
    marker, the five bands and the note."""
    if out.suffix == ".xlsx":
        rows = _rows(out)
        assert rows[-9][0] == TABLE_F_MARKER
        return hashlib.sha256(json.dumps(rows[:-10] + rows[-2:], default=str).encode()).hexdigest()
    lines = out.read_bytes().splitlines(keepends=True)
    assert lines[-9].startswith(TABLE_F_MARKER.encode())
    return hashlib.sha256(b"".join(lines[:-10] + lines[-2:])).hexdigest()


@pytest.mark.parametrize("format_id", ["review-csv", "xlsx"])
@pytest.mark.parametrize("command", ["schedule-cg", "schedule-cg-ltcg"])
@pytest.mark.parametrize("case", ["example", "loss-across-bands"])
def test_only_the_table_f_block_changes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
    command: str,
    format_id: str,
) -> None:
    """Detail rows, audit blocks, both TOTAL rows and both streams are main's;
    only the loss-across-bands STCG run's exit code changes, 0 to 3."""
    statement = _EXAMPLE if case == "example" else _LOSS_ACROSS_BANDS
    code, out = _schedule_cg(tmp_path, statement, command=command, format_id=format_id)
    captured = capsys.readouterr()
    row = 3 if command == "schedule-cg" else 5
    stderr = captured.err.replace("\n\n" + _manual_fill_line(row, out.name), "")

    loss_across_bands = (case, command) == ("loss-across-bands", "schedule-cg")
    assert code == (3 if loss_across_bands else 0)
    assert (stderr != captured.err) is loss_across_bands
    assert {
        "file": _without_block(out),
        "stdout": _sha256(captured.out.replace(str(tmp_path), "<tmp>")),
        "stderr": _sha256(stderr.replace(str(tmp_path), "<tmp>")),
    } == _MAIN_SHA256[(case, command, format_id)]
