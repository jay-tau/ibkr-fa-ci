"""The Schedule TR and Form 67 reports' ``Exclusions:`` list has no non-ordinary line (#1363).

Since #1310 an in-FY return of capital, long-term capital gain or cash in lieu
refuses, so the count the ``    non-ordinary distributions:`` line printed could
only be 0. The line is gone: ``  Exclusions:`` is followed directly by the
capital-gains counts. The fixture is synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ibkr_fa.cli import main
from tests._ibkr_user_toml import US_PERSON_USER_TOML

_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["schedule-tr", "--itr-form", "ITR-2"], id="schedule-tr"),
        pytest.param(["form-67"], id="form-67"),
    ],
)
def test_exclusions_list_has_no_non_ordinary_line(
    command: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "user.toml"
    config.write_text(US_PERSON_USER_TOML, encoding="utf-8")

    rc = main(
        [
            *command,
            "--input",
            str(_FIXTURE),
            "--config",
            str(config),
            "--output",
            str(tmp_path / "out.csv"),
            "--financial-year",
            "2025-26",
        ]
    )

    assert rc == 3  # no [account.foreign_tins]: the US relief row's TIN is blank (#924)
    captured = capsys.readouterr()
    assert "non-ordinary" not in captured.out.lower()
    assert "non-ordinary" not in captured.err.lower()
    lines = captured.out.splitlines()
    assert lines.count("  Exclusions:") == 1
    exclusions = lines.index("  Exclusions:")
    assert lines[exclusions + 1] == "    CG indian-source: 0"
