"""Shared helpers for the Form 67, Schedule FSI, TR and OS command tests (#1749)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ibkr_fa import cli


def install_fake(
    monkeypatch: pytest.MonkeyPatch,
    report: object | None = None,
    *,
    pipeline: str,
    raises: BaseException | None = None,
    recorder: dict[str, Any] | None = None,
) -> None:
    """Replace ``cli.<pipeline>`` with a fake that copies its keyword arguments
    into ``recorder``, then raises ``raises`` or returns ``report``."""

    def fake(**kwargs: Any) -> object:
        if recorder is not None:
            recorder.update(kwargs)
        if raises is not None:
            raise raises
        assert report is not None
        return report

    monkeypatch.setattr(cli, pipeline, fake)


def write_cli_paths(tmp_path: Path, config_text: str) -> tuple[Path, Path, Path]:
    """Write a placeholder ``in.csv`` and ``config_text`` as ``user.toml`` in
    ``tmp_path``; return them and the unwritten ``out.csv``."""
    inp = tmp_path / "in.csv"
    inp.write_text("placeholder", encoding="utf-8")
    cfg = tmp_path / "user.toml"
    cfg.write_text(config_text, encoding="utf-8")
    out = tmp_path / "out.csv"
    return inp, cfg, out


def run_args(command: tuple[str, ...], paths: tuple[Path, Path, Path], *extra: str) -> list[str]:
    """``command`` (the words before ``--input``), then the input, config and
    output ``paths``, FY 2025-26 and ``extra``."""
    inp, cfg, out = paths
    return [
        *command,
        "--input",
        str(inp),
        "--config",
        str(cfg),
        "--output",
        str(out),
        "--financial-year",
        "2025-26",
        *extra,
    ]
