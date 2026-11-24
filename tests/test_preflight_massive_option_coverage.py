"""The Massive preflight script reads its default config from the private root (#838)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "preflight_massive_option_coverage.py"
)
_spec = importlib.util.spec_from_file_location("preflight_massive_option_coverage", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
preflight = importlib.util.module_from_spec(_spec)
sys.modules["preflight_massive_option_coverage"] = preflight
_spec.loader.exec_module(preflight)

_CONFIG = """\
[account]
number = "U1234567"
holder_name = "Tax Payer"
opening_date = 2024-02-01
status = "BeneficialOwner"

[prices]
massive_api_key_file = "keys/massive.env"
"""


def test_default_config_and_relative_key_file_resolve_under_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    (root / "keys").mkdir(parents=True)
    (root / "user.toml").write_text(_CONFIG, encoding="utf-8")
    (root / "keys" / "massive.env").write_text("MASSIVE_API_KEY=synthetic\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("IBKR_FA_PRIVATE_DATA", str(root))
    monkeypatch.chdir(elsewhere)
    assert preflight._api_key() == "synthetic"
