"""End-to-end test for the Schedule CG LTCG pipeline via the CLI."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from ibkr_fa.cli import main
from ibkr_fa.output import EXCLUDED_STCG_MARKER
from ibkr_fa.output.portal_labels import PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG
from tests._ibkr_user_toml import US_PERSON_USER_TOML

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_LTCG_FIXTURE = _FIXTURE_DIR / "e2e_activity_statement_schedule_cg_ltcg_fy2025_26.csv"


def _write_config(tmp_path: Path) -> Path:
    p = tmp_path / "user.toml"
    p.write_text(US_PERSON_USER_TOML, encoding="utf-8")
    return p


def _read_csv_rows(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


def test_e2e_schedule_cg_ltcg_full_pipeline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end: real fixture + real Rule115CapitalGainsPolicy + real
    parser + real writer.

    The fixture contains:
    - LTCG: 10 sh opened 2023-01-15, closed 2025-12-10 (~1060 days = long-term;
      post-2024-07-23 = new regime). Realized PnL = +$700.
    - STCG: 10 sh opened 2025-05-15, closed 2025-11-20 (~189 days = short-term;
      routed to STCG audit bucket — handled by schedule-cg, not schedule-cg-ltcg).
    - SURV: 5 sh opened 2025-06-01, still open at FY end (excluded — not
      a closure).
    """
    config = _write_config(tmp_path)
    out = tmp_path / "cg-ltcg.csv"
    rc = main(
        [
            "schedule-cg-ltcg",
            "--itr-form",
            "ITR-2",
            "--input",
            str(_LTCG_FIXTURE),
            "--config",
            str(config),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "Schedule CG LTCG" in captured.out
    assert "1 in-FY long-term closure" in captured.out
    # STCG row should produce a stderr advisory pointing at schedule-cg
    assert "short-term closure" in captured.err
    assert "schedule-cg" in captured.err
    # #63 T4-r1: BMA disclosure advisory must reach stderr via main().
    assert "Black Money Act disclosure reminder" in captured.err

    assert out.exists()
    rows = _read_csv_rows(out)
    header = rows[0]
    assert header[0] == "Symbol"
    detail = rows[1]
    assert detail[0] == "LTCG"
    assert detail[2] == "US"
    # Open date 15/01/2023, close date 10/12/2025
    assert detail[3] == "15/01/2023"
    assert detail[4] == "10/12/2025"
    # Holding period > 720 days (long-term)
    assert int(detail[5]) > 720
    assert detail[7] == "1500.00"
    assert detail[8] == "2200.00"
    # FX specified date = close-event preceding-month-end (2025-11-30)
    assert detail[9] == "2025-11-30"
    # 2025-11-30 is a Sunday with no SBI row: the rate carries back to
    # SBI's 2025-11-29 row, shown in the source-date column (#741).
    assert detail[10] == "2025-11-29"
    assert float(detail[11]) > 0
    # Single-conversion identity: basis_inr / basis_usd == proceeds_inr / proceeds_usd
    cost_basis_inr = float(detail[12])
    proceeds_inr = float(detail[13])
    fx = float(detail[11])
    assert abs(cost_basis_inr - 1500 * fx) < 0.01
    assert abs(proceeds_inr - 2200 * fx) < 0.01
    assert "Rule 115(1), Explanation (2)(f)" in detail[14]
    rounded_gain = int(detail[15])
    assert rounded_gain > 0
    assert float(detail[12]) < float(detail[13])

    # Portal-label TOTAL row
    assert rows[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG
    assert rows[-1][-1] == str(rounded_gain)

    # STCG audit block present
    markers = [r[0] for r in rows]
    assert EXCLUDED_STCG_MARKER in markers


def test_e2e_schedule_cg_ltcg_quiet_outputs_only_rounded_total(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    out = tmp_path / "cg-ltcg.csv"
    rc = main(
        [
            "schedule-cg-ltcg",
            "--itr-form",
            "ITR-2",
            "--quiet",
            "--input",
            str(_LTCG_FIXTURE),
            "--config",
            str(config),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.strip().lstrip("-").isdigit()


def test_e2e_schedule_cg_ltcg_atomic_no_partial_file_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Wrong account ID → ConfigError → exit 4 → no output file written."""
    config = tmp_path / "user.toml"
    config.write_text(US_PERSON_USER_TOML.replace("U12345678", "U99999999"), encoding="utf-8")
    out = tmp_path / "cg-ltcg.csv"
    rc = main(
        [
            "schedule-cg-ltcg",
            "--itr-form",
            "ITR-2",
            "--input",
            str(_LTCG_FIXTURE),
            "--config",
            str(config),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    assert rc == 4
    assert not out.exists()
    captured = capsys.readouterr()
    assert "error:" in captured.err
