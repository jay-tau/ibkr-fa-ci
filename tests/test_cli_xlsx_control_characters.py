"""CLI regressions: a statement description holding a control character a
workbook cell can't store refuses the workbook with exit 4 instead of crashing
with the text (#1357).

Each input is a synthetic fixture with one description changed to hold U+0001
and the privacy marker; no stream may print the marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from tests._ibkr_user_toml import US_PERSON_USER_TOML
from tests._xlsx_control_characters import MARKER, refusal_text

_FIXTURES = Path(__file__).parent / "fixtures"


def _inputs(tmp_path: Path, fixture: str, old: str, new: str) -> tuple[Path, Path]:
    """The fixture with its one ``old`` description replaced by ``new``, and a config."""
    text = (_FIXTURES / fixture).read_text(encoding="utf-8")
    assert text.count(old) == 1
    statement = tmp_path / "statement.csv"
    statement.write_text(text.replace(old, new), encoding="utf-8")
    config = tmp_path / "user.toml"
    config.write_text(US_PERSON_USER_TOML, encoding="utf-8")
    return statement, config


@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_schedule_cg_xlsx_refuses_a_description_with_a_control_character(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], stale: bool
) -> None:
    """The long-term lot's description reaches the excluded LTCG block (row 4,
    cell 11). The run refuses, writes no workbook and removes a stale one."""
    statement, config = _inputs(
        tmp_path,
        "e2e_activity_statement_schedule_cg_stcg_fy2025_26.csv",
        "LTCG DEFERRED HOLD GT 24MO",
        f"LTCG DEFERRED\x01HOLD {MARKER} GT 24MO",
    )
    out = tmp_path / "cg.xlsx"
    if stale:
        out.write_text("stale", encoding="utf-8")

    rc = main(
        [
            "schedule-cg",
            "--itr-form",
            "ITR-2",
            "--input",
            str(statement),
            "--config",
            str(config),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
            "--format",
            "xlsx",
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", f"error: {refusal_text(4, 11)}\n")
    assert not out.exists()


def test_run_all_xlsx_names_the_cg_ltcg_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The run-all golden fixture's description reaches the CG-LTCG workbook's
    excluded STCG block. That leg refuses, and the run removes every output."""
    statement, config = _inputs(
        tmp_path,
        "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv",
        "SAMPLE ISSUER BA INC",
        f"SAMPLE\x01ISSUER {MARKER} INC",
    )
    out = tmp_path / "out"

    rc = main(
        [
            "run-all",
            "--fy-input",
            str(statement),
            "--financial-year",
            "2025-26",
            "--format",
            "xlsx",
            "--itr-form",
            "ITR-2",
            "--config",
            str(config),
            "--output-dir",
            str(out),
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err.count(f"\nerror: {refusal_text(3, 11)}\n") == 1
    error_label = cli._RUN_ALL_STATUS_LABEL["error"]
    errors = [
        line.split()[1]
        for line in captured.err.splitlines()
        if line.lstrip().startswith(error_label)
    ]
    assert errors == ["schedule-cg-ltcg"]
    assert "internal error" not in captured.out + captured.err
    assert MARKER not in captured.out + captured.err
    assert list(out.iterdir()) == []


def test_schedule_fa_xlsx_refuses_a_description_with_a_control_character(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no entity override, the description is Table A3's Name of entity.
    The Table A3 row refuses the control character itself (#1367), before the
    workbook writer sees it. Prices are offline, as ``tests/conftest.py`` sets."""
    statement, config = _inputs(
        tmp_path,
        "e2e_activity_statement_2025.csv",
        "SAMPLE ISSUER BB INC",
        f"SAMPLE\x01ISSUER {MARKER} INC",
    )
    out = tmp_path / "fa.xlsx"

    rc = main(
        [
            "schedule-fa",
            "--input",
            str(statement),
            "--config",
            str(config),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--format",
            "xlsx",
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (
        "",
        "error: InvariantError: FaA3Row.name_of_entity has a control character at position 7; "
        "filed text may not contain control characters\n",
    )
    assert not out.exists()
