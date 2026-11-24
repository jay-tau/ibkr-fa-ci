"""IBKR's ``schedule-fsi`` and ``schedule-tr`` refuse a REFUSED Form 67 row as
``form-67`` does (#1480).

The #1474 fixtures: the FY 2025-26 end-to-end statement with its one security
moved to Cayman (no DTAA with India, so a section 91 credit) or Switzerland (no
vendored DTAA row), each with an exact [[form67.tax_payable_overrides]] amount
for its dividend. Form 67 can't compute either credit, so the row is REFUSED
and ``form-67`` stops with exit 4, writing nothing. ``schedule-fsi`` and
``schedule-tr`` now do the same in every format, with ``form-67``'s error,
after removing an earlier output; following the error clears it on a rerun.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ibkr_fa.cli import main
from tests._ftc_warnings import CH_GAP as _CH_GAP
from tests._ftc_warnings import blank_row_warning as _warning
from tests._ftc_warnings import wont_clear
from tests._ibkr_form67_runs import CH_ISIN as _CH
from tests._ibkr_form67_runs import CH_OVERRIDE as _CH_OVERRIDE
from tests._ibkr_form67_runs import KY_ISIN as _KY
from tests._ibkr_form67_runs import KY_OVERRIDE as _KY_OVERRIDE
from tests._ibkr_form67_runs import SECTION_91_INPUTS as _SECTION_91
from tests._ibkr_form67_runs import config_with as _config
from tests._ibkr_form67_runs import ibkr_output_dir as _output_dir
from tests._ibkr_form67_runs import missing_tax_warnings as _warnings
from tests._ibkr_form67_runs import statement_with_isin as _statement

# Per case: the security's ISIN, its override, the start of form-67's error,
# the config that follows that error, and the rerun's exit code and missing
# Indian tax warnings. The Cayman rerun exits 3 for the blank foreign TIN
# advisory alone: the config has no KY entry in [account.foreign_tins].
_CASES = {
    "no-section-91": (
        _KY,
        _KY_OVERRIDE,
        "KY/DIVIDEND: KY has no DTAA with India, and a section 91 credit needs",
        _KY_OVERRIDE + _SECTION_91,
        3,
        [],
    ),
    "unvendored": (
        _CH,
        _CH_OVERRIDE,
        _CH_GAP,
        "",
        3,
        [_warning(wont_clear(_CH_GAP, "CH/DIVIDEND"))],
    ),
}
_COMMANDS = pytest.mark.parametrize("command", ["schedule-fsi", "schedule-tr"])
# None runs the default format, review-csv.
_FORMATS = pytest.mark.parametrize("format_id", [None, "filing-csv", "xlsx"])


def _argv(
    command: str, statement: Path, config: Path, output: Path, format_id: str | None
) -> list[str]:
    return [
        command,
        *("--input", str(statement), "--config", str(config)),
        *("--output", str(output), "--financial-year", "2025-26"),
        *(("--itr-form", "ITR-2") if command == "schedule-tr" else ()),
        *(("--format", format_id) if format_id is not None else ()),
    ]


def _output(out: Path, command: str, format_id: str | None) -> Path:
    return out / f"{command}.{'xlsx' if format_id == 'xlsx' else 'csv'}"


@_FORMATS
@_COMMANDS
@pytest.mark.parametrize("case", list(_CASES))
def test_a_refused_form67_row_stops_the_run_as_in_form_67(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
    command: str,
    format_id: str | None,
) -> None:
    """The run exits 4, prints exactly what ``form-67`` prints for the same
    input, and leaves the output directory empty, its earlier output removed.
    Fails on 6f81822, which exited 3 with the generic missing Indian tax
    warning and wrote the file with the row's Indian tax payable blank."""
    isin, override, refusal, *_ = _CASES[case]
    statement = _statement(tmp_path, isin)
    config = _config(tmp_path, override)
    form67_out = tmp_path / "form-67"
    form67_out.mkdir()
    form67_rc = main(_argv("form-67", statement, config, form67_out / "form-67.csv", None))
    form67 = capsys.readouterr()
    assert form67_rc == 4
    assert f"Form 67 cannot be produced: {refusal}" in form67.err
    out = _output_dir(tmp_path)
    output = _output(out, command, format_id)
    output.write_text("earlier", encoding="utf-8")

    rc = main(_argv(command, statement, config, output, format_id))

    captured = capsys.readouterr()
    assert rc == 4
    assert (captured.out, captured.err) == (form67.out, form67.err)
    assert list(out.iterdir()) == []


@_FORMATS
@_COMMANDS
@pytest.mark.parametrize("case", list(_CASES))
def test_following_the_refusal_clears_it_on_a_rerun(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
    command: str,
    format_id: str | None,
) -> None:
    """Cayman: adding both [form67.section_91] inputs the error names files
    the section 91 credit, with no missing Indian tax warning. Switzerland:
    removing the override, as the error says, writes the file with the row
    blank, #1474's Swiss advice and exit 3."""
    isin, override, _refusal, followed, exit_code, warnings = _CASES[case]
    statement = _statement(tmp_path, isin)
    out = _output_dir(tmp_path)
    output = _output(out, command, format_id)
    assert main(_argv(command, statement, _config(tmp_path, override), output, format_id)) == 4
    capsys.readouterr()

    rc = main(_argv(command, statement, _config(tmp_path, followed), output, format_id))

    assert rc == exit_code
    assert _warnings(capsys) == warnings
    assert list(out.iterdir()) == [output]
