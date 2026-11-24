"""CLI refusals for exact foreign-tax sums (#1167).

``form-67``, ``schedule-fsi`` and ``schedule-tr`` run the real pipeline on a
fixture statement whose withholding rows are replaced, at the income engine's
input, by synthetic rows: the compute tests' marked 29-digit row, then $1E10 and
$1E-995 of US tax. The country's INR net would need more than ``EXACT_SUM_DIGITS``
digits, so every command refuses at the third row before writing anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ibkr_fa import pipeline_fsi_compute
from ibkr_fa.cli import main
from tests._exact_refusals import INEXACT_SUM
from tests._foreign_tax_exact_sums import MARKERS as _MARKERS
from tests._foreign_tax_exact_sums import marked_wht as _marked_wht
from tests._foreign_tax_exact_sums import wht as _wht
from tests._ibkr_user_toml import US_PERSON_USER_TOML

_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
)
_ROWS = (_marked_wht(), _wht("-1E+10"), _wht("-1E-995"))


@pytest.mark.parametrize(
    ("command", "format_id", "suffix"),
    [
        ("form-67", "portal-csv", ".csv"),
        ("form-67", "review-csv", ".csv"),
        ("form-67", "xlsx", ".xlsx"),
        ("schedule-fsi", "filing-csv", ".csv"),
        ("schedule-fsi", "review-csv", ".csv"),
        ("schedule-fsi", "xlsx", ".xlsx"),
        ("schedule-tr", "filing-csv", ".csv"),
        ("schedule-tr", "review-csv", ".csv"),
        ("schedule-tr", "xlsx", ".xlsx"),
    ],
)
@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_foreign_tax_net_past_the_exact_cap_refuses_and_clears_the_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    format_id: str,
    suffix: str,
    stale: bool,
) -> None:
    real = pipeline_fsi_compute.compute_income_schedules

    def with_synthetic_withholding(*args: Any, **kwargs: Any) -> Any:
        kwargs["withholding_taxes"] = list(_ROWS)
        return real(*args, **kwargs)

    monkeypatch.setattr(
        pipeline_fsi_compute, "compute_income_schedules", with_synthetic_withholding
    )
    config = tmp_path / "user.toml"
    config.write_text(US_PERSON_USER_TOML, encoding="utf-8")
    out = tmp_path / f"{command}{suffix}"
    if stale:
        out.write_text("stale", encoding="utf-8")
    itr = ["--itr-form", "ITR-2"] if command == "schedule-tr" else []

    rc = main(
        [
            command,
            *itr,
            "--input",
            str(_FIXTURE),
            "--config",
            str(config),
            "--output",
            str(out),
            "--format",
            format_id,
            "--financial-year",
            "2025-26",
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == (
        "error: InvariantError: Foreign tax country net, included row 3, column amount_inr: "
        f"{INEXACT_SUM}\n"
    )
    assert captured.out == ""
    for marker in _MARKERS:
        assert marker not in captured.err, marker
    assert not out.exists()
