"""The Schedule OS end-to-end config, dividends statement and CLI runners that
several test modules share.

- ``SCHEDULE_OS_DIVIDENDS_STATEMENT``: the path of the synthetic FY 2025-26
  Activity Statement with dividends, under ``tests/fixtures/``.
- ``SCHEDULE_OS_USER_TOML``: the ``user.toml`` text the Schedule OS end-to-end
  runs use. It is the shared ``ACCOUNT_AND_FX_USER_TOML`` from
  ``tests._ibkr_user_toml``.
- ``run_schedule_os_cli``: runs ``schedule-os`` on a statement through
  ``ibkr_fa.cli.main`` and returns its exit code.
- ``schedule_os_with_earlier_output``: writes the config and an earlier run's
  Schedule OS output, and returns the output folder.
- ``run_schedule_os_on_dividends``: runs ``schedule-os`` on the dividends
  statement with the config and output folder that
  ``schedule_os_with_earlier_output`` wrote.
"""

from __future__ import annotations

from pathlib import Path

from ibkr_fa.cli import main
from tests._ibkr_user_toml import ACCOUNT_AND_FX_USER_TOML

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
SCHEDULE_OS_DIVIDENDS_STATEMENT = (
    _FIXTURES_DIR / "e2e_activity_statement_schedule_os_with_dividends_fy2025_26.csv"
)

SCHEDULE_OS_USER_TOML = ACCOUNT_AND_FX_USER_TOML


def run_schedule_os_cli(
    fixture: Path, cfg: Path, out: Path, fy: str = "2025-26", *extra: str
) -> int:
    return main(
        [
            "schedule-os",
            "--input",
            str(fixture),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--financial-year",
            fy,
            *extra,
        ]
    )


def schedule_os_with_earlier_output(tmp_path: Path) -> Path:
    """The FY 2025-26 dividends statement's config and an earlier run's
    Schedule OS output; returns the output folder."""
    (tmp_path / "user.toml").write_text(SCHEDULE_OS_USER_TOML, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "schedule-os.csv").write_bytes(b"an earlier run")
    return out


def run_schedule_os_on_dividends(tmp_path: Path) -> int:
    return run_schedule_os_cli(
        SCHEDULE_OS_DIVIDENDS_STATEMENT, tmp_path / "user.toml", tmp_path / "out/schedule-os.csv"
    )
