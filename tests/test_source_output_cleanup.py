"""StockPlan, NetBenefits and Fidelity CMA commands refuse when they cannot
remove an earlier output (#736).

Each one removes every output it may write before it reads config or bundle.
A target that cannot be removed refuses the run with exit 4 and is named, once
every other target has been removed, as for the IBKR schedules
(``tests/test_standalone_output_cleanup.py``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from ibkr_fa.cli import main
from ibkr_fa.output._cleanup import OutputRemovalError
from ibkr_fa.output.cbdt_json_fragment import CBDT_JSON_FRAGMENT_FORMAT_ID, CbdtJsonUnreadableError
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_fidelity_cma_cg import run_fidelity_cma_cg
from ibkr_fa.pipeline_fidelity_cma_fa import FidelityCmaFaError, run_fidelity_cma_fa
from ibkr_fa.pipeline_fidelity_cma_income import run_fidelity_cma_income
from ibkr_fa.pipeline_netbenefits_cg import run_netbenefits_cg
from ibkr_fa.pipeline_netbenefits_fa import NetBenefitsFaError, run_netbenefits_fa
from ibkr_fa.pipeline_netbenefits_income import run_netbenefits_income
from ibkr_fa.pipeline_stockplan_fa import run_stockplan_fa
from ibkr_fa.pipeline_stockplan_income import run_stockplan_income
from tests._output_removal_failures import CLEANUP_LAYERS as _LAYERS
from tests._output_removal_failures import STALE_BYTES as _STALE
from tests._output_removal_failures import existing_names as _existing
from tests._output_removal_failures import fail_unlink as _fail_unlink
from tests._output_removal_failures import make_unremovable as _make_unremovable
from tests._source_all import CG_OUTPUTS, INCOME_OUTPUTS_WITH_DEBUG
from tests._source_commands import SOURCE_FY_ARGS

_MALFORMED_CONFIG = b"not = [valid toml"
_CY_ARGS = ("--calendar-year", "2025")
_FY_KWARGS: dict[str, object] = {"financial_year": FinancialYear(2025)}
_CY_KWARGS: dict[str, object] = {"calendar_year": 2025}
_NO_FETCH_KWARGS: dict[str, object] = {**_CY_KWARGS, "fetch_prices": False}


@dataclass(frozen=True)
class _Command:
    run: Callable[..., object]
    output_dir: bool  # writes into --output-dir, not to --output
    targets: tuple[str, ...]  # file names; for --output, the first is --output
    cli_args: tuple[str, ...]
    run_kwargs: dict[str, object]


_COMMANDS = {
    "stockplan-income": _Command(
        run_stockplan_income, True, INCOME_OUTPUTS_WITH_DEBUG, SOURCE_FY_ARGS, _FY_KWARGS
    ),
    "stockplan-fa": _Command(
        run_stockplan_fa, False, ("schedule-fa.csv",), _CY_ARGS, _NO_FETCH_KWARGS
    ),
    "netbenefits-income": _Command(
        run_netbenefits_income, True, INCOME_OUTPUTS_WITH_DEBUG, SOURCE_FY_ARGS, _FY_KWARGS
    ),
    "netbenefits-cg": _Command(run_netbenefits_cg, True, CG_OUTPUTS, SOURCE_FY_ARGS, _FY_KWARGS),
    "netbenefits-fa": _Command(
        run_netbenefits_fa,
        False,
        (
            "schedule-fa-a3.csv",
            "schedule-fa-a2.csv",
            "schedule-fa-a3-a2.csv",
            "schedule-fa-quicko.xlsx",
        ),
        _CY_ARGS,
        _NO_FETCH_KWARGS,
    ),
    "fidelity-cma-income": _Command(
        run_fidelity_cma_income, True, INCOME_OUTPUTS_WITH_DEBUG, SOURCE_FY_ARGS, _FY_KWARGS
    ),
    "fidelity-cma-cg": _Command(run_fidelity_cma_cg, True, CG_OUTPUTS, SOURCE_FY_ARGS, _FY_KWARGS),
    "fidelity-cma-fa": _Command(
        run_fidelity_cma_fa,
        False,
        ("schedule-fa.csv", "schedule-fa-a2.csv"),
        _CY_ARGS,
        _CY_KWARGS,
    ),
}
# Every (command, index of a target) pair.
_TARGETS = [(name, index) for name, cmd in _COMMANDS.items() for index in range(len(cmd.targets))]


# Missing inputs: a run that got as far as reading them would refuse.
_BUNDLE = "missing-bundle"
_TAX_FORMS = "missing-tax-forms"  # fidelity-cma-income's 1042-S folder


def _cli(name: str, tmp_path: Path, config: Path, targets: list[Path], *extra: str) -> int:
    command = _COMMANDS[name]
    output = ("--output-dir", targets[0].parent) if command.output_dir else ("--output", targets[0])
    tax_forms = ("--tax-forms", str(tmp_path / _TAX_FORMS)) if name == "fidelity-cma-income" else ()
    return main(
        [
            name,
            "--input",
            str(tmp_path / _BUNDLE),
            "--config",
            str(config),
            output[0],
            str(output[1]),
            *tax_forms,
            *command.cli_args,
            *extra,
        ]
    )


def _entry(name: str, tmp_path: Path, config: Path, targets: list[Path], **extra: object) -> object:
    command = _COMMANDS[name]
    output = (
        {"output_dir": targets[0].parent} if command.output_dir else {"output_path": targets[0]}
    )
    tax_forms = {"tax_form_dir": tmp_path / _TAX_FORMS} if name == "fidelity-cma-income" else {}
    return command.run(
        input_dir=tmp_path / _BUNDLE,
        config_path=config,
        **output,
        **tax_forms,
        **command.run_kwargs,
        **extra,
    )


def _malformed_config(tmp_path: Path) -> Path:
    config = tmp_path / "user.toml"
    config.write_bytes(_MALFORMED_CONFIG)
    return config


def _stale_targets(out: Path, name: str) -> list[Path]:
    out.mkdir()
    targets = [out / filename for filename in _COMMANDS[name].targets]
    for target in targets:
        target.write_bytes(_STALE)
    return targets


def _named(message: str, targets: list[Path]) -> list[Path]:
    return [target for target in targets if str(target) in message]


@pytest.mark.parametrize("layer", _LAYERS)
@pytest.mark.parametrize("how", ["directory", "permission"])
@pytest.mark.parametrize(("name", "index"), _TARGETS)
def test_previous_output_that_cannot_be_removed_refuses_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    index: int,
    how: str,
    layer: str,
) -> None:
    """A missing bundle and a malformed config prove nothing was read: either
    would refuse with its own error had the run got that far. Every other
    target is removed, and only the one left is named."""
    config = _malformed_config(tmp_path)
    targets = _stale_targets(tmp_path / "out", name)
    failing = targets[index]
    _make_unremovable(monkeypatch, failing, how)

    if layer == "cli":
        assert _cli(name, tmp_path, config, targets) == 4
        message = capsys.readouterr().err
    else:
        with pytest.raises(OutputRemovalError) as exc:
            _entry(name, tmp_path, config, targets)
        message = str(exc.value)

    assert "could not remove the previous output" in message
    assert _named(message, targets) == [failing]
    assert _existing(targets) == [failing.name]
    assert config.read_bytes() == _MALFORMED_CONFIG


@pytest.mark.parametrize("layer", _LAYERS)
def test_every_output_that_cannot_be_removed_is_named(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    layer: str,
) -> None:
    config = _malformed_config(tmp_path)
    targets = _stale_targets(tmp_path / "out", "netbenefits-income")
    failing = [targets[0], targets[-1]]
    _make_unremovable(monkeypatch, failing[0], "directory")
    _make_unremovable(monkeypatch, failing[1], "permission")

    if layer == "cli":
        assert _cli("netbenefits-income", tmp_path, config, targets) == 4
        message = capsys.readouterr().err
    else:
        with pytest.raises(OutputRemovalError) as exc:
            _entry("netbenefits-income", tmp_path, config, targets)
        message = str(exc.value)

    assert "could not remove the previous output" in message
    assert _named(message, targets) == failing
    assert _existing(targets) == [path.name for path in failing]


@pytest.mark.parametrize("calendar_year", ["2100", "1989"])
def test_fidelity_cma_fa_calendar_year_refusal_removes_previous_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], calendar_year: str
) -> None:
    """The year is checked after the earlier outputs are removed (#919)."""
    config = _malformed_config(tmp_path)
    targets = _stale_targets(tmp_path / "out", "fidelity-cma-fa")

    exit_code = main(
        [
            "fidelity-cma-fa",
            "--input",
            str(tmp_path / _BUNDLE),
            "--config",
            str(config),
            "--output",
            str(targets[0]),
            "--calendar-year",
            calendar_year,
        ]
    )

    assert exit_code == 4
    assert capsys.readouterr().err == "error: calendar year must be between 1990 and 2099\n"
    assert _existing(targets) == []
    assert config.read_bytes() == _MALFORMED_CONFIG


@pytest.mark.parametrize("output", ["/", "."])
def test_fidelity_cma_fa_nameless_output_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], output: str
) -> None:
    """Moving the sibling-path calculation must preserve a CLI refusal."""
    config = _malformed_config(tmp_path)

    exit_code = main(
        [
            "fidelity-cma-fa",
            "--input",
            str(tmp_path / _BUNDLE),
            "--config",
            str(config),
            "--output",
            output,
            "--calendar-year",
            "2100",
        ]
    )

    assert exit_code == 4
    assert capsys.readouterr().err.startswith("error: ")


def test_fidelity_cma_fa_invalid_library_path_refuses(tmp_path: Path) -> None:
    """A path error must not turn a library format refusal into a raw ValueError."""
    config = _malformed_config(tmp_path)

    with pytest.raises(FidelityCmaFaError) as exc:
        _entry(
            "fidelity-cma-fa", tmp_path, config, [tmp_path / "bad\0.csv"], format_id="filing-csv"
        )
    assert str(exc.value) == "fidelity-cma-fa requires valid paths for previous-output cleanup"
    assert isinstance(exc.value.__context__, ValueError)
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__


def test_fidelity_cma_fa_format_refusal_removes_previous_outputs(tmp_path: Path) -> None:
    """``--format`` choices stop this at the CLI; the library checks the format
    after the earlier outputs are removed (#919)."""
    config = _malformed_config(tmp_path)
    targets = _stale_targets(tmp_path / "out", "fidelity-cma-fa")

    with pytest.raises(
        FidelityCmaFaError,
        match=r"^fidelity-cma-fa supports filing-csv, review-csv and xlsx only$",
    ):
        _entry("fidelity-cma-fa", tmp_path, config, targets, format_id="filing-xlsx")

    assert _existing(targets) == []
    assert config.read_bytes() == _MALFORMED_CONFIG


_FRAGMENT = json.dumps({"_ibkrFa": {"format": CBDT_JSON_FRAGMENT_FORMAT_ID}})


def _json(layer: str, tmp_path: Path, config: Path, fragment: Path) -> object:
    if layer == "cli":
        return _cli(
            "netbenefits-fa",
            tmp_path,
            config,
            [fragment],
            "--format",
            CBDT_JSON_FRAGMENT_FORMAT_ID,
            "--itr-form",
            "ITR-2",
        )
    return _entry(
        "netbenefits-fa",
        tmp_path,
        config,
        [fragment],
        format_id=CBDT_JSON_FRAGMENT_FORMAT_ID,
        itr_form="ITR-2",
    )


@pytest.mark.parametrize("layer", _LAYERS)
def test_netbenefits_fa_json_fragment_that_cannot_be_removed_refuses_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    layer: str,
) -> None:
    config = _malformed_config(tmp_path)
    fragment = tmp_path / "schedule-fa.json"
    fragment.write_text(_FRAGMENT, encoding="utf-8")
    _fail_unlink(monkeypatch, fragment, [True])

    if layer == "cli":
        assert _json(layer, tmp_path, config, fragment) == 4
        message = capsys.readouterr().err
    else:
        with pytest.raises(OutputRemovalError) as exc:
            _json(layer, tmp_path, config, fragment)
        message = str(exc.value)

    assert "could not remove the previous output" in message
    assert str(fragment) in message
    assert fragment.read_text(encoding="utf-8") == _FRAGMENT
    assert config.read_bytes() == _MALFORMED_CONFIG


@pytest.mark.parametrize("layer", _LAYERS)
@pytest.mark.parametrize(
    ("existing", "error", "message"),
    [
        ('{"ScheduleFA": {"hand-merged": true}}', NetBenefitsFaError, "refusing to overwrite"),
        ("not an ibkr-fa fragment", CbdtJsonUnreadableError, "could not inspect"),
        (None, CbdtJsonUnreadableError, "could not inspect"),
    ],
    ids=["foreign", "malformed", "directory"],
)
def test_netbenefits_fa_json_target_not_proven_its_own_is_kept(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    existing: str | None,
    error: type[Exception],
    message: str,
    layer: str,
) -> None:
    config = _malformed_config(tmp_path)
    fragment = tmp_path / "schedule-fa.json"
    if existing is None:
        fragment.mkdir()
    else:
        fragment.write_text(existing, encoding="utf-8")

    if layer == "cli":
        assert _json(layer, tmp_path, config, fragment) == 4
        assert message in capsys.readouterr().err
    else:
        with pytest.raises(error, match=re.escape(message)):
            _json(layer, tmp_path, config, fragment)

    if existing is None:
        assert fragment.is_dir()
    else:
        assert fragment.read_text(encoding="utf-8") == existing
