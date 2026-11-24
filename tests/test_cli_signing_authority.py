"""A ``SigningAuthority`` account status is refused when the config loads (#887).

Each command treats it like any other ``ConfigError`` on load: it clears its
stale output as before, exits 4 and parses no statement.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from ibkr_fa import pipeline_fa, pipeline_ibkr_input, pipeline_stockplan_income
from ibkr_fa.cli import main

_CONFIG = """\
[account]
number = "U1234567"
holder_name = "Tax Payer"
opening_date = 2024-01-01
status = "SigningAuthority"

[stockplan.security]
symbol = "XQCB"
isin = "US99971P1012"
"""

_DIAGNOSTIC = (
    "ConfigError at field 'account.status': signing-authority accounts aren't "
    "supported; the supported statuses are BeneficialOwner and Joint"
)

_FY = ("--financial-year", "2025-26")
_ITR = ("--itr-form", "ITR-2")


@pytest.mark.parametrize(
    ("command", "args", "stale_name"),
    [
        (
            "schedule-fa",
            ("--input", "{fy}", "--output", "{out}/schedule-fa.csv", "--calendar-year", "2025"),
            "schedule-fa.csv",
        ),
        (
            "schedule-os",
            ("--input", "{fy}", "--output", "{out}/schedule-os.csv", *_FY),
            "schedule-os.csv",
        ),
        (
            "stockplan-income",
            ("--input", "{exports}", "--output-dir", "{out}", *_FY, *_ITR),
            "schedule-os.csv",
        ),
        (
            "run-all",
            ("--fy-input", "{fy}", "--cy-input", "{cy}", "--output-dir", "{out}", *_FY, *_ITR),
            "schedule-os.csv",
        ),
    ],
)
def test_signing_authority_status_exits_4_before_any_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    args: tuple[str, ...],
    stale_name: str,
) -> None:
    stubs = []
    for module, name in (
        (pipeline_fa, "detect_ibkr_csv_format"),
        (pipeline_ibkr_input, "detect_ibkr_csv_format"),
        (pipeline_stockplan_income, "parse_stockplan_dividend_report"),
    ):
        stub = Mock(side_effect=AssertionError(f"{name} must not run"))
        monkeypatch.setattr(module, name, stub)
        stubs.append(stub)
    config = tmp_path / "user.toml"
    config.write_text(_CONFIG, encoding="utf-8")
    paths = {name: tmp_path / name for name in ("fy", "cy", "exports", "out")}
    paths["fy"].write_text("statement", encoding="utf-8")
    paths["cy"].write_text("statement", encoding="utf-8")
    paths["exports"].mkdir()
    # A report the dividend parser would read if the config loaded.
    (paths["exports"] / "IN - Dividend Report.xlsx").write_bytes(b"")
    paths["out"].mkdir()
    stale = paths["out"] / stale_name
    stale.write_text("stale", encoding="utf-8")

    argv = [command, "--config", str(config), *(arg.format(**paths) for arg in args)]
    assert main(argv) == 4

    assert _DIAGNOSTIC in capsys.readouterr().err
    for stub in stubs:
        stub.assert_not_called()
    assert not stale.exists()
