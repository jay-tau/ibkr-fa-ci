"""Output cleanup never deletes a run's own inputs (#673).

Every command that clears output before (or while) it runs must refuse — exit
4, every file left byte-for-byte, no leg run — when a file it may delete is the
same file as its ``--config``, its ``--input`` statement, a bundle file it
reads, or a file its config names. Missing files never clash.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import ibkr_fa.pipeline_robinhood as robinhood_pipeline
from ibkr_fa import cli
from ibkr_fa.cli import main
from ibkr_fa.output.netbenefits_quicko_fa_xlsx import QUICKO_TEMPLATE_FILENAME
from ibkr_fa.pipeline_robinhood import RobinhoodPipelineError
from tests._config_toml import NON_UTF8_TOML
from tests._ibkr_user_toml import CUSTODIAN_TOML
from tests._malformed_csv import UNCLOSED, oversized_cell
from tests._netbenefits import CLOSED_EMPTY_CSV, CLOSED_RENAMED_PROCEEDS_CSV, OPEN_CSV
from tests._oserror_markers import assert_no_oserror_markers as _assert_no_markers
from tests._oserror_markers import marked_oserror as _marked_oserror
from tests._robinhood_toml import (
    robinhood_corrected_statement_toml,
    robinhood_report_toml,
    robinhood_security_toml,
    robinhood_toml,
)
from tests._source_all import CG_OUTPUTS, INCOME_OUTPUTS_WITH_DEBUG
from tests._source_commands import SOURCE_FY_ARGS

_FIXTURES = Path(__file__).parent / "fixtures"
_FA_STATEMENT = _FIXTURES / "e2e_activity_statement_with_nav_2025.csv"
_MALFORMED_CONFIG = b"not = [valid toml"
_PROVENANCE_JSON = {"_ibkrFa": {"format": "cbdt-json-fragment"}}

_NETBENEFITS_FA = (
    "schedule-fa-a3.csv",
    "schedule-fa-a2.csv",
    "schedule-fa-quicko.xlsx",
    "schedule-fa.csv",
    "schedule-fa-a3-a2.csv",
)
_ROBINHOOD_CG = (
    "schedule-cg.csv",
    "schedule-cg.xlsx",
    "schedule-cg-ltcg.csv",
    "schedule-cg-ltcg.xlsx",
    "schedule-cg-debug.csv",
    "schedule-cg-debug.xlsx",
    "schedule-cg-ltcg-debug.csv",
    "schedule-cg-ltcg-debug.xlsx",
)
_ROBINHOOD_INCOME = (
    "schedule-os.csv",
    "schedule-os.xlsx",
    "schedule-os-debug.csv",
    "schedule-fsi.csv",
    "schedule-tr.csv",
    "form-67.csv",
    "schedule-fsi-debug.csv",
    "schedule-tr-debug.csv",
    "form-67-debug.csv",
)
_ROBINHOOD_FA_TABLES = (
    "schedule-fa-a2.csv",
    "schedule-fa-a2.xlsx",
    "schedule-fa-a3.csv",
    "schedule-fa-a3.xlsx",
    "schedule-fa.csv",
    "schedule-fa.xlsx",
    "schedule-fa-a3-a2.csv",
    "schedule-fa-quicko.xlsx",
)
_ROBINHOOD_OPTIONS = ("robinhood-options.csv",)

# Commands writing into --output-dir: every file each one may delete. A JSON
# target is listed last so the "last target" case is the cross-set one.
_DIR_COMMANDS: dict[str, tuple[str, ...]] = {
    "stockplan-income": INCOME_OUTPUTS_WITH_DEBUG,
    "stockplan-all": (*INCOME_OUTPUTS_WITH_DEBUG, "schedule-fa.csv", *CG_OUTPUTS),
    "netbenefits-cg": CG_OUTPUTS,
    "netbenefits-income": INCOME_OUTPUTS_WITH_DEBUG,
    "netbenefits-all": (*_NETBENEFITS_FA, *INCOME_OUTPUTS_WITH_DEBUG, *CG_OUTPUTS),
    "fidelity-cma-cg": CG_OUTPUTS,
    "fidelity-cma-income": INCOME_OUTPUTS_WITH_DEBUG,
    "robinhood-cg": _ROBINHOOD_CG,
    "robinhood-options": _ROBINHOOD_OPTIONS,
    "robinhood-income": _ROBINHOOD_INCOME,
    "robinhood-fa": (*_ROBINHOOD_FA_TABLES, "schedule-fa.json"),
    "robinhood-all": (
        *_ROBINHOOD_CG,
        *_ROBINHOOD_INCOME,
        *_ROBINHOOD_FA_TABLES,
        *_ROBINHOOD_OPTIONS,
        "schedule-fa.json",
    ),
}
# Commands writing to --output (the first target): extra argv, every target.
_FILE_COMMANDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "stockplan-fa": (("--calendar-year", "2025"), ("schedule-fa.csv",)),
    "fidelity-cma-fa": (("--calendar-year", "2025"), ("schedule-fa.csv", "schedule-fa-a2.csv")),
    "form-67": (("--financial-year", "2025-26"), ("form-67.csv",)),
}
# The orchestrator legs that must not run once the collision check refuses.
_LEGS: dict[str, tuple[str, ...]] = {
    "stockplan-all": ("_run_stockplan_income", "_run_stockplan_fa", "_run_stockplan_cg_status"),
    "netbenefits-all": ("_run_netbenefits_fa", "_run_netbenefits_income", "_run_netbenefits_cg"),
}


def _stale(path: Path) -> bytes:
    if path.suffix == ".json":
        return json.dumps({**_PROVENANCE_JSON, "stale": path.name}).encode()
    return f"stale {path.name}".encode()


def _snapshot(paths: list[Path]) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in paths}


def _forbid_legs(monkeypatch: pytest.MonkeyPatch, command: str) -> list[str]:
    ran: list[str] = []
    for name in _LEGS.get(command, ()):
        monkeypatch.setattr(cli, name, lambda _args, name=name: ran.append(name) or 0)
    if command.startswith("robinhood-"):

        def load(**_kwargs: object) -> None:
            ran.append("load_robinhood_bundle")
            raise RobinhoodPipelineError("the bundle must not load after a collision refusal")

        monkeypatch.setattr(robinhood_pipeline, "_load", load)
    return ran


def _dir_argv(command: str, tmp_path: Path, out: Path, config: Path) -> list[str]:
    bundle = tmp_path / "bundle"
    bundle.mkdir(exist_ok=True)
    argv = [command, "--input", str(bundle), "--config", str(config), "--output-dir", str(out)]
    if command == "fidelity-cma-income":
        tax_forms = tmp_path / "tax-forms"
        tax_forms.mkdir(exist_ok=True)
        argv += ["--tax-forms", str(tax_forms)]
    return [*argv, *SOURCE_FY_ARGS]


def _file_argv(command: str, tmp_path: Path, out: Path, config: Path) -> list[str]:
    extra, targets = _FILE_COMMANDS[command]
    if command == "form-67":
        source = tmp_path / "statement.csv"
        source.write_bytes(b"the user's statement")
    else:
        source = tmp_path / "bundle"
        source.mkdir(exist_ok=True)
    return [
        command,
        "--input",
        str(source),
        "--config",
        str(config),
        "--output",
        str(out / targets[0]),
        *extra,
    ]


def _out(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir(mode=0o700)  # Robinhood refuses a group/world-accessible output dir
    return out


def _config_alias_cases() -> list[tuple[str, str]]:
    cases = []
    for command, targets in (
        *_DIR_COMMANDS.items(),
        *((command, spec[1]) for command, spec in _FILE_COMMANDS.items()),
    ):
        cases.append((command, targets[0]))
        if len(targets) > 1:
            cases.append((command, targets[-1]))
    return cases


@pytest.mark.parametrize(("command", "alias"), _config_alias_cases())
def test_config_saved_as_an_output_target_is_refused_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    alias: str,
) -> None:
    """A malformed ``--config`` at a target proves the check runs before any
    config parse; every other stale target (including a provenance-marked
    JSON fragment) must survive too."""
    out = _out(tmp_path)
    targets = _DIR_COMMANDS.get(command) or _FILE_COMMANDS[command][1]
    for name in targets:
        (out / name).write_bytes(_stale(out / name))
    config = out / alias
    config.write_bytes(_MALFORMED_CONFIG)
    before = _snapshot([out / name for name in targets])
    ran = _forbid_legs(monkeypatch, command)
    build = _dir_argv if command in _DIR_COMMANDS else _file_argv

    assert main(build(command, tmp_path, out, config)) == 4

    assert _snapshot([out / name for name in targets]) == before
    assert ran == []
    err = capsys.readouterr().err
    assert "--config" in err
    assert alias in err


def test_form67_input_that_is_its_output_is_refused_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    statement = tmp_path / "form-67.csv"
    statement.write_bytes(b"the user's statement")
    rc = main(
        [
            "form-67",
            "--input",
            str(statement),
            "--config",
            str(tmp_path / "user.toml"),
            "--output",
            str(statement),
            "--financial-year",
            "2025-26",
        ]
    )
    assert rc == 4
    assert statement.read_bytes() == b"the user's statement"
    assert "--input" in capsys.readouterr().err


_FA_CONFIG = (
    """\
[account]
number = "U12345678"
holder_name = "Sample Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"
"""
    + CUSTODIAN_TOML
)


@pytest.mark.parametrize("flag", ["--input", "--config"])
def test_schedule_fa_a2_sibling_that_is_an_input_is_refused_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    """A valid run reaches the A2 sibling's removal only after parsing, so a
    statement or config saved under that name would be deleted mid-run."""
    out = tmp_path / "out"
    out.mkdir()
    a2 = out / "schedule-fa-a2.csv"
    paths = {"--input": tmp_path / "statement.csv", "--config": tmp_path / "user.toml"}
    paths[flag] = a2
    paths["--input"].write_bytes(_FA_STATEMENT.read_bytes())
    paths["--config"].write_text(_FA_CONFIG, encoding="utf-8")
    expected = a2.read_bytes()
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(paths["--input"]),
            "--config",
            str(paths["--config"]),
            "--output",
            str(out / "schedule-fa.csv"),
            "--calendar-year",
            "2025",
            "--itr-form",
            "ITR-2",
            "--no-fetch-prices",
        ]
    )
    assert rc == 4
    assert a2.read_bytes() == expected
    assert not (out / "schedule-fa.csv").exists()
    err = capsys.readouterr().err
    assert flag in err
    assert "schedule-fa-a2.csv" in err


@pytest.mark.parametrize("flag", ["--input", "--config"])
def test_schedule_fa_json_output_that_is_an_input_is_refused_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    """Provenance proves the file was once ibkr-fa's fragment, not that this
    run does not read it: the preclear must not delete a supplied input."""
    fragment = tmp_path / "schedule-fa.json"
    fragment.write_text(json.dumps(_PROVENANCE_JSON), encoding="utf-8")
    expected = fragment.read_bytes()
    paths = {"--input": tmp_path / "statement.csv", "--config": tmp_path / "user.toml"}
    paths[flag] = fragment
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(paths["--input"]),
            "--config",
            str(paths["--config"]),
            "--output",
            str(fragment),
            "--calendar-year",
            "2025",
            "--format",
            "cbdt-json-fragment",
            "--itr-form",
            "ITR-2",
        ]
    )
    assert rc == 4
    assert fragment.read_bytes() == expected
    assert flag in capsys.readouterr().err


def _link_bundle_file(bundle_file: Path, target: Path) -> None:
    """Make ``bundle_file`` (a file the run reads) a symlink to ``target``, so
    deleting the target would delete the bundle file's data."""
    target.write_bytes(f"the user's {bundle_file.name}".encode())
    bundle_file.parent.mkdir(parents=True, exist_ok=True)
    bundle_file.symlink_to(target)


# command → (bundle file relative to --input, the target it is linked to).
_BUNDLE_SYMLINK_CASES = {
    "stockplan-income": ("IN - Dividend Report 2025.xlsx", "schedule-os.csv"),
    "stockplan-all": ("Vested Share Holdings.xlsx", "schedule-cg-ltcg.csv"),
    "netbenefits-cg": ("confirmations/2025-06-30.pdf", "schedule-cg.csv"),
    "netbenefits-income": ("statements/2025-12.pdf", "form-67-debug.csv"),
    "netbenefits-all": ("2025-06-30.pdf", "schedule-fa-quicko.xlsx"),
    "fidelity-cma-cg": ("2025-12.pdf", "schedule-cg-ltcg.csv"),
    "fidelity-cma-income": ("2025-12.pdf", "schedule-os.csv"),
    "robinhood-cg": ("statements/2025/2025-12.pdf", "schedule-cg.xlsx"),
    "robinhood-income": ("tax-forms/1042-s.pdf", "schedule-os.xlsx"),
    "robinhood-fa": ("activity/individual.csv", "schedule-fa-a3.csv"),
    "robinhood-all": ("activity/managed-individual.csv", "robinhood-options.csv"),
}


@pytest.mark.parametrize("command", sorted(_BUNDLE_SYMLINK_CASES))
def test_bundle_file_reached_through_a_target_is_refused_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    out = _out(tmp_path)
    relative, target_name = _BUNDLE_SYMLINK_CASES[command]
    argv = _dir_argv(command, tmp_path, out, tmp_path / "user.toml")
    _link_bundle_file(tmp_path / "bundle" / relative, out / target_name)
    if command == "fidelity-cma-income":
        # The 1042-S directory is an input too.
        _link_bundle_file(tmp_path / "tax-forms" / "1042-s.pdf", out / "schedule-tr.csv")
    before = _snapshot(sorted(out.iterdir()))
    ran = _forbid_legs(monkeypatch, command)

    assert main(argv) == 4

    assert _snapshot(sorted(out.iterdir())) == before
    assert ran == []
    err = capsys.readouterr().err
    assert "--input" in err
    assert target_name in err


@pytest.mark.parametrize("command", sorted(_BUNDLE_SYMLINK_CASES))
def test_bundle_file_reached_through_a_dangling_target_is_refused_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """The bundle scan lists a symlink without following it, so a bundle file
    whose chain goes through a target that is a dangling symlink is still an
    input: clearing the target would break the user's chain."""
    out = _out(tmp_path)
    relative, target_name = _BUNDLE_SYMLINK_CASES[command]
    argv = _dir_argv(command, tmp_path, out, tmp_path / "user.toml")
    target = out / target_name
    referent = tmp_path / "unmounted" / relative
    target.symlink_to(referent)
    bundle_file = tmp_path / "bundle" / relative
    bundle_file.parent.mkdir(parents=True, exist_ok=True)
    bundle_file.symlink_to(target)
    ran = _forbid_legs(monkeypatch, command)

    assert main(argv) == 4

    assert target.readlink() == referent
    assert ran == []
    err = capsys.readouterr().err
    assert f"--input {bundle_file} is or goes through {target}" in err


def test_schedule_os_config_at_a_dangling_output_is_refused_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``--config`` on an unmounted volume, symlinked at the output path, needs
    only the volume mounted: the run must not delete the user's symlink."""
    out = tmp_path / "out"
    out.mkdir()
    output = out / "schedule-os.csv"
    referent = tmp_path / "unmounted" / "user.toml"
    output.symlink_to(referent)
    rc = main(
        [
            "schedule-os",
            "--input",
            str(_FIXTURES / "e2e_activity_statement_schedule_os_with_dividends_fy2025_26.csv"),
            "--config",
            str(output),
            "--output",
            str(output),
            "--financial-year",
            "2025-26",
        ]
    )
    assert rc == 4
    assert os.path.lexists(output)
    assert output.readlink() == referent
    assert f"--config {output} is or goes through {output}" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("command", "bundle_name", "content"),
    [
        ("stockplan-fa", "IN - Capital Gain_Loss Report 2025.xlsx", b"the user's report"),
        ("netbenefits-fa", "View closed lots.csv", CLOSED_EMPTY_CSV.encode()),
        ("netbenefits-fa", "View open lots.csv", OPEN_CSV.encode()),
        ("netbenefits-fa", "View closed lots (1).csv", CLOSED_RENAMED_PROCEEDS_CSV.encode()),
        ("netbenefits-fa", QUICKO_TEMPLATE_FILENAME, b"the user's Quicko template"),
        ("fidelity-cma-fa", "2025-12.pdf", b"the user's statement"),
    ],
)
def test_output_that_is_a_bundle_file_is_refused_untouched(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    bundle_name: str,
    content: bytes,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    bundle_file = bundle / bundle_name
    bundle_file.write_bytes(content)
    rc = main(
        [
            command,
            "--input",
            str(bundle),
            "--config",
            str(tmp_path / "user.toml"),
            "--output",
            str(bundle_file),
            "--calendar-year",
            "2025",
        ]
    )
    assert rc == 4
    assert bundle_file.read_bytes() == content
    err = capsys.readouterr().err
    assert "--input" in err
    assert bundle_name in err


def test_robinhood_cusip_transitions_file_saved_as_a_target_is_refused_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A file named by the config is an input too, even though ``--input``
    itself is a directory."""
    out = _out(tmp_path)
    transitions = out / "schedule-cg.csv"
    transitions.write_bytes(b"the user's CUSIP transition evidence")
    stale = out / "schedule-cg-debug.csv"
    stale.write_bytes(b"stale")
    config = tmp_path / "user.toml"
    config.write_text(
        robinhood_toml(parent_fields=f'cusip_transitions_file = "{transitions}"\n'),
        encoding="utf-8",
    )
    ran = _forbid_legs(monkeypatch, "robinhood-cg")

    assert main(_dir_argv("robinhood-cg", tmp_path, out, config)) == 4

    assert transitions.read_bytes() == b"the user's CUSIP transition evidence"
    assert stale.read_bytes() == b"stale"
    assert ran == []
    err = capsys.readouterr().err
    assert "robinhood.cusip_transitions_file" in err
    assert "schedule-cg.csv" in err


def test_netbenefits_stale_output_inside_the_bundle_is_still_cleared(tmp_path: Path) -> None:
    """Compatibility (passes before #673 too): only real bundle sources are
    protected. A prior run's output written inside the bundle is not a lots
    export, so it is cleared as usual and the run reaches its normal error."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    stale = bundle / "schedule-cg.csv"
    stale.write_bytes(b"Schedule CG,stale\n")
    rc = main(
        [
            "netbenefits-cg",
            "--input",
            str(bundle),
            "--config",
            str(tmp_path / "user.toml"),
            "--output-dir",
            str(bundle),
            *SOURCE_FY_ARGS,
        ]
    )
    assert rc == 4
    assert not stale.exists()


@pytest.mark.parametrize(
    ("command", "target_name"),
    [
        ("netbenefits-cg", "schedule-cg.csv"),
        ("netbenefits-income", "schedule-os.csv"),
        ("netbenefits-all", "schedule-fa-a3.csv"),
    ],
)
def test_netbenefits_lots_near_miss_named_like_a_target_is_refused_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    target_name: str,
) -> None:
    """#911: a CSV that looks like a lots export is an input, even for
    netbenefits-income, which never reads lots, so clearing never removes it."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    near_miss = bundle / target_name
    near_miss.write_text(CLOSED_RENAMED_PROCEEDS_CSV, encoding="utf-8")
    ran = _forbid_legs(monkeypatch, command)
    rc = main(
        [
            command,
            "--input",
            str(bundle),
            "--config",
            str(tmp_path / "user.toml"),
            "--output-dir",
            str(bundle),
            *SOURCE_FY_ARGS,
        ]
    )
    assert rc == 4
    assert near_miss.read_text(encoding="utf-8") == CLOSED_RENAMED_PROCEEDS_CSV
    assert ran == []
    err = capsys.readouterr().err
    assert f"--input {near_miss}" in err


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(f"{UNCLOSED}\n", id="quote-left-open"),
        pytest.param(f"{oversized_cell()}\n", id="oversized-cell"),
    ],
)
def test_netbenefits_unreadable_csv_named_like_a_target_is_refused_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    text: str,
) -> None:
    """#1098: a CSV whose first record can't be read can't be ruled out as a
    lots export, so it is an input and clearing never removes it."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    unreadable = bundle / "schedule-os.csv"
    unreadable.write_text(text, encoding="utf-8")
    ran = _forbid_legs(monkeypatch, "netbenefits-income")
    rc = main(
        [
            "netbenefits-income",
            *("--input", str(bundle), "--config", str(tmp_path / "user.toml")),
            *("--output-dir", str(bundle), *SOURCE_FY_ARGS),
        ]
    )
    assert rc == 4
    assert unreadable.read_text(encoding="utf-8") == text
    assert ran == []
    assert f"--input {unreadable}" in capsys.readouterr().err


_ROBINHOOD_COMMANDS = sorted(
    command for command in _DIR_COMMANDS if command.startswith("robinhood-")
)


@pytest.mark.parametrize(
    ("config_bytes", "refusal"),
    [(_MALFORMED_CONFIG, "invalid TOML"), (NON_UTF8_TOML, "must be UTF-8 text")],
    ids=["malformed-toml", "not-utf8"],
)
@pytest.mark.parametrize("command", _ROBINHOOD_COMMANDS)
def test_robinhood_config_that_cannot_load_refuses_before_clearing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    config_bytes: bytes,
    refusal: str,
) -> None:
    """Robinhood reads its config to learn which other files it names, so a
    config that exists but cannot load refuses before anything is deleted."""
    out = _out(tmp_path)
    targets = [out / name for name in _DIR_COMMANDS[command]]
    for target in targets:
        target.write_bytes(_stale(target))
    config = tmp_path / "user.toml"
    config.write_bytes(config_bytes)
    before = _snapshot(targets)
    ran = _forbid_legs(monkeypatch, command)

    assert main(_dir_argv(command, tmp_path, out, config)) == 4

    assert _snapshot(targets) == before
    assert ran == []
    assert f"ConfigError at field '<file>': {refusal}" in capsys.readouterr().err


def test_robinhood_unknown_security_class_refuses_before_clearing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A sub_category the entity routes don't know refuses at load, before
    anything is deleted, instead of taking the direct GLEIF route (#930)."""
    out = _out(tmp_path)
    targets = [out / name for name in _DIR_COMMANDS["robinhood-fa"]]
    for target in targets:
        target.write_bytes(_stale(target))
    config = tmp_path / "user.toml"
    config.write_text(
        robinhood_toml(extra=robinhood_security_toml(sub_category="ETFF")), encoding="utf-8"
    )
    before = _snapshot(targets)
    ran = _forbid_legs(monkeypatch, "robinhood-fa")

    assert main(_dir_argv("robinhood-fa", tmp_path, out, config)) == 4

    assert _snapshot(targets) == before
    assert ran == []
    assert "robinhood.securities[0].sub_category" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("key", "extra"),
    [
        (
            "robinhood.corrected_statements",
            robinhood_corrected_statement_toml(selected_file="out/schedule-cg.csv"),
        ),
        (
            "robinhood.corrected_statements",
            robinhood_corrected_statement_toml(supersedes='["out/schedule-cg.csv"]'),
        ),
        ("robinhood.reports", robinhood_report_toml(file="out/schedule-cg.csv")),
    ],
)
def test_robinhood_config_named_file_outside_the_bundle_folders_is_refused_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    key: str,
    extra: str,
) -> None:
    """Corrected statements are read before discovery checks they are bundle
    PDFs, so a config-named file anywhere under --input is protected."""
    out = tmp_path / "bundle" / "out"
    out.mkdir(parents=True, mode=0o700)
    named = out / "schedule-cg.csv"
    named.write_bytes(b"the user's file")
    config = tmp_path / "user.toml"
    config.write_text(robinhood_toml(extra=extra), encoding="utf-8")
    ran = _forbid_legs(monkeypatch, "robinhood-cg")

    assert main(_dir_argv("robinhood-cg", tmp_path, out, config)) == 4

    assert named.read_bytes() == b"the user's file"
    assert ran == []
    err = capsys.readouterr().err
    assert key in err
    assert "schedule-cg.csv" in err


def _check_failure_cases() -> list[tuple[str, tuple[str, ...]]]:
    return [
        *((command, targets) for command, targets in _DIR_COMMANDS.items()),
        *((command, spec[1]) for command, spec in _FILE_COMMANDS.items()),
    ]


@pytest.mark.parametrize(("command", "targets"), _check_failure_cases())
def test_collision_check_that_cannot_inspect_a_target_refuses_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    targets: tuple[str, ...],
) -> None:
    out = _out(tmp_path)
    paths = [out / name for name in targets]
    for path in paths:
        path.write_bytes(_stale(path))
    before = _snapshot(paths)
    ran = _forbid_legs(monkeypatch, command)
    build = _dir_argv if command in _DIR_COMMANDS else _file_argv
    argv = build(command, tmp_path, out, tmp_path / "user.toml")
    original = Path.samefile

    def samefile(path: Path, other: Path) -> bool:
        if path == paths[0]:
            raise _marked_oserror()
        return original(path, other)

    monkeypatch.setattr(Path, "samefile", samefile)

    assert main(argv) == 4

    monkeypatch.undo()
    assert _snapshot(paths) == before
    assert ran == []
    err = capsys.readouterr().err
    # The first input checked: form-67's statement, every other command's config.
    checked = (
        f"--input {tmp_path / 'statement.csv'}"
        if command == "form-67"
        else f"--config {tmp_path / 'user.toml'}"
    )
    assert err.splitlines()[0] == (
        f"error: could not check output target {paths[0]} against {checked} (EACCES)"
    )
    _assert_no_markers(err)


@pytest.mark.parametrize(
    ("output_name", "extra"),
    [("schedule-fa.csv", ()), ("schedule-fa.json", ("--format", "cbdt-json-fragment"))],
)
def test_schedule_fa_collision_check_failure_refuses_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_name: str,
    extra: tuple[str, ...],
) -> None:
    output = tmp_path / output_name
    output.write_bytes(_stale(output))
    before = output.read_bytes()
    original = Path.samefile

    def samefile(path: Path, other: Path) -> bool:
        if path == output:
            raise _marked_oserror()
        return original(path, other)

    monkeypatch.setattr(Path, "samefile", samefile)
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(tmp_path / "statement.csv"),
            "--config",
            str(tmp_path / "user.toml"),
            "--output",
            str(output),
            "--calendar-year",
            "2025",
            "--itr-form",
            "ITR-2",
            *extra,
        ]
    )
    monkeypatch.undo()
    assert rc == 4
    assert output.read_bytes() == before
    err = capsys.readouterr().err
    assert err == (
        f"error: could not check output target {output} against --input "
        f"{tmp_path / 'statement.csv'} (EACCES)\n"
    )
    _assert_no_markers(err)


# command → the bundle folder (relative to --input) whose listing fails.
_UNLISTABLE = {
    "stockplan-income": ".",
    "stockplan-all": ".",
    "netbenefits-income": "statements",
    "netbenefits-all": ".",
    "fidelity-cma-cg": ".",
    "robinhood-cg": "statements",
    "robinhood-all": "tax-forms",
}


@pytest.mark.parametrize("command", sorted(_UNLISTABLE))
def test_bundle_folder_that_cannot_be_listed_refuses_before_clearing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """A folder that cannot be listed may hide a file aliased by a target, so
    the check refuses instead of reading it as empty (as ``glob`` would)."""
    out = _out(tmp_path)
    paths = [out / name for name in _DIR_COMMANDS[command]]
    for path in paths:
        path.write_bytes(_stale(path))
    before = _snapshot(paths)
    argv = _dir_argv(command, tmp_path, out, tmp_path / "user.toml")
    unlistable = (tmp_path / "bundle" / _UNLISTABLE[command]).resolve()
    unlistable.mkdir(exist_ok=True)
    ran = _forbid_legs(monkeypatch, command)
    original = os.scandir

    def scandir(path: str | Path = ".") -> object:
        if Path(path).resolve() == unlistable:
            raise _marked_oserror()
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)

    assert main(argv) == 4

    monkeypatch.undo()
    assert _snapshot(paths) == before
    assert ran == []
    # The folder as typed or listed, and the errno name only (#1048).
    err = capsys.readouterr().err
    listed = tmp_path / "bundle" / _UNLISTABLE[command]
    assert (
        f"error: could not list {listed} to check it against the output targets (EACCES)"
        in err.splitlines()
    )
    _assert_no_markers(err)


@pytest.mark.parametrize(
    ("command", "target_name"),
    [
        ("stockplan-fa", None),
        ("netbenefits-fa", None),
        ("fidelity-cma-fa", None),
        ("stockplan-income", "schedule-os.csv"),
        ("netbenefits-cg", "schedule-cg.csv"),
        ("fidelity-cma-income", "form-67.csv"),
        ("robinhood-options", "robinhood-options.csv"),
    ],
)
def test_input_file_given_for_a_bundle_folder_is_refused_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    target_name: str | None,
) -> None:
    """``--input`` names a folder, but a file passed there is still the user's
    input: scanning it finds nothing, so the path itself must be checked."""
    out = _out(tmp_path)
    config = tmp_path / "user.toml"
    if target_name is None:
        source = out / "export.xlsx"
        argv = [command, "--input", "", "--config", str(config), "--output", str(source)]
        argv += ["--calendar-year", "2025"]
    else:
        source = out / target_name
        argv = _dir_argv(command, tmp_path, out, config)
    argv[argv.index("--input") + 1] = str(source)
    source.write_bytes(b"the user's export")
    ran = _forbid_legs(monkeypatch, command)

    assert main(argv) == 4

    assert source.read_bytes() == b"the user's export"
    assert ran == []
    assert "--input" in capsys.readouterr().err


def test_run_all_input_beneath_a_regular_file_refuses_before_clearing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Compatibility: the existing run-all guard also refuses this OSError."""
    out = _out(tmp_path)
    stale = out / "schedule-os.csv"
    stale.write_bytes(b"stale")
    blocker = tmp_path / "regular-file"
    blocker.write_bytes(b"the user's file")
    ran: list[str] = []
    for name in (
        "_run_schedule_os",
        "_run_schedule_cg",
        "_run_schedule_cg_ltcg",
        "_run_schedule_fsi",
        "_run_schedule_tr",
        "_run_form67",
    ):
        monkeypatch.setattr(cli, name, lambda _args, name=name: ran.append(name) or 0)

    assert (
        main(
            [
                "run-all",
                "--fy-input",
                str(blocker / "statement.csv"),
                "--config",
                str(tmp_path / "user.toml"),
                "--output-dir",
                str(out),
                *SOURCE_FY_ARGS,
            ]
        )
        == 4
    )

    assert stale.read_bytes() == b"stale"
    assert blocker.read_bytes() == b"the user's file"
    assert ran == []
    # The errno name, not its strerror (#1048).
    err = capsys.readouterr().err
    assert err == (
        f"error: could not check output target {stale} against --fy-input "
        f"{blocker / 'statement.csv'} (ENOTDIR)\n"
    )
    assert "not a directory" not in err.lower()


def test_bundle_entry_that_cannot_be_inspected_refuses_before_clearing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _out(tmp_path)
    paths = [out / name for name in _DIR_COMMANDS["netbenefits-all"]]
    for path in paths:
        path.write_bytes(_stale(path))
    before = _snapshot(paths)
    argv = _dir_argv("netbenefits-all", tmp_path, out, tmp_path / "user.toml")
    bundle = tmp_path / "bundle"
    nested = bundle / "nested"
    nested.mkdir()
    (nested / "statement.pdf").symlink_to(out / "schedule-cg.csv")
    ran = _forbid_legs(monkeypatch, "netbenefits-all")
    original = os.scandir

    def cannot_inspect(*_args: object, **_kwargs: object) -> bool:
        raise _marked_oserror()

    class Scan:
        def __init__(self, path):
            self.iterator = original(path)

        def __enter__(self):
            self.iterator.__enter__()
            return self

        def __exit__(self, *args):
            return self.iterator.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.iterator)
            if entry.name == "nested":
                return SimpleNamespace(name=entry.name, path=entry.path, is_dir=cannot_inspect)
            return entry

    monkeypatch.setattr(
        os, "scandir", lambda path: Scan(path) if Path(path) == bundle else original(path)
    )

    assert main(argv) == 4

    assert _snapshot(paths) == before
    assert ran == []
    # Names the folder being listed, not the entry or the OSError's file name (#1048).
    err = capsys.readouterr().err
    assert err == (
        f"error: could not list {bundle} to check it against the output targets (EACCES)\n"
    )
    _assert_no_markers(err)
