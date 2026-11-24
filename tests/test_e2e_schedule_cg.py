"""End-to-end test for the Schedule CG STCG pipeline via the CLI.

FY-based Activity Statement e2e for ``schedule-cg``, mirroring
:mod:`tests.test_e2e_schedule_os` (which exercises the same pattern
for the dividend pathway). Each test wires the real CLI
``main(argv)`` against a synthetic FY 2025-26 Activity Statement
fixture and asserts on stdout/stderr, exit code, and the resulting
review CSV.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from ibkr_fa.cli import main
from ibkr_fa.output import EXCLUDED_LTCG_DEFERRED_MARKER
from ibkr_fa.output.portal_labels import PORTAL_FIELD_LABEL_CG_FOREIGN_STCG
from tests._ibkr_user_toml import US_PERSON_USER_TOML as _USER_TOML

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_STCG_FIXTURE = _FIXTURE_DIR / "e2e_activity_statement_schedule_cg_stcg_fy2025_26.csv"


def _write_config(tmp_path: Path) -> Path:
    p = tmp_path / "user.toml"
    p.write_text(_USER_TOML, encoding="utf-8")
    return p


def _read_csv_rows(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


def test_e2e_schedule_cg_stcg_full_pipeline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end: real fixture + real Rule115CapitalGainsPolicy + real
    Activity Statement parser + real writer.

    The fixture contains:
    - STCG: 10 sh opened 2025-05-15 @ $150, closed 2025-11-20 @ $180
      (basis 1500 USD, proceeds 1800 USD, realized PnL +300 USD,
      holding ~189 days = STCG).
    - LTCG: 20 sh opened 2023-01-10 @ $50, closed 2025-08-10 @ $90
      (holding > 24 calendar months → LTCG, see schedule-cg-ltcg).
    - SURV: 5 sh opened 2025-06-01, still open at FY end.
    """
    config = _write_config(tmp_path)
    out = tmp_path / "cg.csv"
    rc = main(
        [
            "schedule-cg",
            "--itr-form",
            "ITR-2",
            "--input",
            str(_STCG_FIXTURE),
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
    assert "Schedule CG STCG" in captured.out
    assert "1 in-FY short-term closure" in captured.out
    # LTCG bucket should produce a stderr advisory directing users
    # at the new schedule-cg-ltcg subcommand (post-#30).
    assert "long-term closure" in captured.err
    assert "schedule-cg-ltcg" in captured.err
    # #63 T4-r1: BMA disclosure advisory must reach stderr via main().
    assert "Black Money Act disclosure reminder" in captured.err

    assert out.exists()
    rows = _read_csv_rows(out)
    header = rows[0]
    assert header[0] == "Symbol"
    # The first detail row is the only STCG (LTCG is in the audit block)
    detail = rows[1]
    assert detail[0] == "STCG"
    assert detail[2] == "US"
    # Open date 15/05/2025
    assert detail[3] == "15/05/2025"
    # Close date 20/11/2025
    assert detail[4] == "20/11/2025"
    # Holding period
    assert detail[5] == "189"
    # Cost basis USD = 1500
    assert detail[7] == "1500.00"
    # Proceeds USD = 1800
    assert detail[8] == "1800.00"
    # T5 Codex P1 fix: single Rule 115 specified-date + TTBR pair
    # (transfer-event only). Column layout: 9=specified date,
    # 10=SBI TTBR source date (#741), 11=TTBR, 12=basis INR,
    # 13=proceeds INR, 14=basis source, 15=rounded gain.
    # Close=2025-11-20 → preceding-month-end specified-date 2025-10-31.
    assert detail[9] == "2025-10-31"
    # SBI published on 2025-10-31 itself: no carry-back, blank cell.
    assert detail[10] == ""
    assert float(detail[11]) > 0
    # Single-conversion identity: cost_basis_inr / cost_basis_usd
    # equals proceeds_inr / proceeds_usd (same FX applied to both).
    cost_basis_inr = float(detail[12])
    proceeds_inr = float(detail[13])
    cost_basis_usd = float(detail[7])
    proceeds_usd = float(detail[8])
    fx = float(detail[11])
    assert abs(cost_basis_inr - cost_basis_usd * fx) < 0.01
    assert abs(proceeds_inr - proceeds_usd * fx) < 0.01
    # Realized gain INR rounded — must be positive
    assert "Rule 115(1), Explanation (2)(f)" in detail[14]
    rounded_gain = int(detail[15])
    assert rounded_gain > 0
    # Sanity-check the proceeds-vs-basis ordering for the gain case.
    assert float(detail[12]) < float(detail[13])

    # Last row = portal label + rounded TOTAL
    assert rows[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_STCG
    assert rows[-1][-1] == str(rounded_gain)

    # LTCG audit block present
    markers = [r[0] for r in rows]
    assert EXCLUDED_LTCG_DEFERRED_MARKER in markers


def test_e2e_schedule_cg_quiet_outputs_only_rounded_total(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    out = tmp_path / "cg.csv"
    rc = main(
        [
            "schedule-cg",
            "--itr-form",
            "ITR-2",
            "--quiet",
            "--input",
            str(_STCG_FIXTURE),
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
    # --quiet stdout: single integer line (rounded INR TOTAL)
    assert captured.out.strip().lstrip("-").isdigit()


def test_e2e_schedule_cg_atomic_no_partial_file_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Wrong account ID → ConfigError → exit 4 → no output file written."""
    config = tmp_path / "user.toml"
    config.write_text(
        _USER_TOML.replace("U12345678", "U99999999"),
        encoding="utf-8",
    )
    out = tmp_path / "cg.csv"
    rc = main(
        [
            "schedule-cg",
            "--itr-form",
            "ITR-2",
            "--input",
            str(_STCG_FIXTURE),
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
