"""The exact Form 67 section 91 credit through the real CLI (#1177).

``form-67``, ``schedule-fsi``, ``schedule-tr`` and ``run-all`` run on a fixture
statement whose dividend and withholding rows are replaced, at the income
engine's input, by the synthetic Bermuda rows of ``tests/_section_91_scenarios.py``
(``tests/test_pipeline_section_91_exact.py`` describes each scenario).
Every new refusal exits 4 with exactly one ``error:`` line and leaves no new or
earlier output; the corrected credits file where nothing refuses.
"""

from __future__ import annotations

from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from unittest.mock import Mock

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from tests._run_all_fakes import RUN_ALL_SCHEDULE_NAMES
from tests._section_91_inputs import (
    _EXACT_PORTAL,
    _EXACT_ZERO,
    _MARKERS,
    _PREFIX,
    _PREFLIGHT_OTHER,
    _PREFLIGHT_PORTAL,
    _PREFLIGHT_ZERO,
    _PROOF,
    _Country,
)
from tests._section_91_scenarios import (
    _FIXTURE,
    _FORM67,
    _PORTAL,
    _SCENARIOS,
    _arrange,
    _form67_filed_credits,
    _Scenario,
)


def _error(detail: str, prefix: str = "") -> str:
    return f"error: {prefix}{_PREFIX} 1, {detail}\n"


def _assert_no_markers(*streams: str) -> None:
    for stream in streams:
        for marker in _MARKERS:
            assert marker not in stream, marker


def _command(command: str, config: Path, output: Path, format_id: str) -> int:
    return main(
        [
            command,
            *(("--itr-form", "ITR-2") if command == "schedule-tr" else ()),
            "--input",
            str(_FIXTURE),
            "--config",
            str(config),
            "--output",
            str(output),
            "--format",
            format_id,
            "--financial-year",
            "2025-26",
        ]
    )


_CLI_SCENARIOS = {
    **_SCENARIOS,
    # The exact credit can't convert the 1E-1001 foreign tax within
    # EXACT_SUM_DIGITS places. A total tax of 1E-1001, as before, now refuses
    # when user.toml is read (#1275).
    "proof": _Scenario((_Country(gross="10", wht="-1E-1001", payable="5"),), "1", "1", "1"),
    "preflight-other": _Scenario((_Country(gross="1", wht="-1", payable="1"),), "1", "3", "1"),
}
_REFUSALS = [
    ("form-67", "portal-csv", "proof", _PROOF),
    ("form-67", "portal-csv", "preflight-other", _PREFLIGHT_OTHER),
    # The corrected credit files ₹0 through the real readiness check.
    ("form-67", "portal-csv", "readiness", _EXACT_ZERO),
    # The former credit filed ₹0 (control), or the credit is zero either way.
    ("form-67", "portal-csv", "control", _PREFLIGHT_ZERO),
    ("form-67", "portal-csv", "zero-tax", _PREFLIGHT_ZERO),
    *(
        (command, format_id, name, detail)
        for command in ("schedule-fsi", "schedule-tr")
        for format_id in ("filing-csv", "review-csv", "xlsx")
        for name, detail in (
            # The corrected ₹1 relief newly breaks the portal rule.
            ("product", _EXACT_PORTAL),
            # The former ₹1 broke it, though the corrected ₹2 doesn't.
            ("ordinary", _PREFLIGHT_PORTAL),
        )
    ),
]


@pytest.mark.parametrize(
    ("command", "format_id", "name", "detail"),
    _REFUSALS,
    ids=["-".join(case[:3]) for case in _REFUSALS],
)
@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_each_new_refusal_exits_4_with_one_error_line_and_no_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    format_id: str,
    name: str,
    detail: str,
    stale: bool,
) -> None:
    """Fails on 1f885b5, which wrote the readiness and product scenarios,
    refused control and zero tax with a ConfigError and the ordinary scenario
    with the generic portal-relief message. The proof case formerly refused
    zero credit, and the trapped rate formerly raised a Decimal exception."""
    config = _arrange(monkeypatch, tmp_path, _CLI_SCENARIOS[name])
    output = tmp_path / f"{command}.{'xlsx' if format_id == 'xlsx' else 'csv'}"
    if stale:
        output.write_text("stale", encoding="utf-8")

    with localcontext() as context:
        context.traps[Inexact] = name == "preflight-other"
        rc = _command(command, config, output, format_id)

    captured = capsys.readouterr()
    assert (rc, captured.err, captured.out) == (4, _error(detail), "")
    assert sorted(tmp_path.iterdir()) == [config]
    _assert_no_markers(captured.err, captured.out)


# Each scenario's run-all refusal: the Form 67 readiness check's, else the
# Schedule FSI portal check's (the ordinary and product Form 67 rows are READY).
_RUN_ALL = {name: _FORM67[name][3] or _PORTAL[name] for name in _PORTAL}


@pytest.mark.parametrize("format_id", ["review-csv", "xlsx"])
@pytest.mark.parametrize("name", list(_RUN_ALL))
def test_run_all_refuses_before_any_schedule_and_clears_every_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    format_id: str,
) -> None:
    """run-all's preflight enforces Form 67 readiness in every format, then the
    Schedule FSI portal rule: control and zero tax keep the former ₹0 refusal,
    readiness refuses its corrected ₹0, and the ordinary and product READY rows
    refuse at the portal rule. No schedule runs and every earlier output goes.

    Fails on 1f885b5 (a ConfigError, the generic portal message, or no refusal
    for the readiness and product scenarios)."""
    config = _arrange(monkeypatch, tmp_path, _SCENARIOS[name])
    out = tmp_path / "out"
    out.mkdir()
    for schedule in RUN_ALL_SCHEDULE_NAMES:
        for target in cli._run_all_targets(out, schedule):
            target.write_text("stale", encoding="utf-8")
    invoke = Mock(wraps=cli._invoke_run_all_handler)
    monkeypatch.setattr(cli, "_invoke_run_all_handler", invoke)

    rc = main(
        [
            "run-all",
            "--fy-input",
            str(_FIXTURE),
            "--financial-year",
            "2025-26",
            "--format",
            format_id,
            "--itr-form",
            "ITR-2",
            "--config",
            str(config),
            "--output-dir",
            str(out),
        ]
    )

    captured = capsys.readouterr()
    assert (rc, captured.err, captured.out) == (4, _error(_RUN_ALL[name], "run-all: "), "")
    invoke.assert_not_called()
    assert list(out.iterdir()) == []
    _assert_no_markers(captured.err, captured.out)


@pytest.mark.parametrize(
    ("command", "format_id", "name"),
    [
        ("form-67", "portal-csv", "ordinary"),
        ("form-67", "portal-csv", "product"),
        ("form-67", "review-csv", "readiness"),
        ("form-67", "xlsx", "control"),
        ("form-67", "portal-csv", "repeating"),
        ("schedule-fsi", "filing-csv", "repeating"),
        ("schedule-tr", "filing-csv", "repeating"),
    ],
)
def test_corrected_and_repeating_rate_credits_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    format_id: str,
    name: str,
) -> None:
    """Form 67 files the exact credit: ₹2 for the ordinary cent amounts (the
    public regression), ₹1 for the product correction, ₹3 for a repeating 1/3
    rate. The standalone review formats write the readiness scenario's
    corrected REFUSED ₹0 row and the control's corrected ₹1. Schedule FSI and
    TR write the repeating-rate relief. Each exits 0.

    Fails on 1f885b5 for the ordinary (₹1), product (₹2), readiness (₹1) and
    control (₹0) Form 67 credits."""
    config = _arrange(monkeypatch, tmp_path, _SCENARIOS[name])
    output = tmp_path / f"{command}.{'xlsx' if format_id == 'xlsx' else 'csv'}"

    rc = _command(command, config, output, format_id)

    captured = capsys.readouterr()
    assert rc == 0
    assert "error:" not in captured.err
    if command == "form-67":
        filed = Decimal(_FORM67[name][1])
        assert _form67_filed_credits(output, format_id) == (filed, filed)
    else:
        assert output.is_file()
    _assert_no_markers(captured.err, captured.out)
