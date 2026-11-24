"""A whitespace-only Schedule FA Table A3 Name of entity is named as manual fill (#1369).

For names that pass the existing row and output-format checks, the run now
names the cell and the ``[[fa.entities]]`` ``name`` setting as manual work
(exit 3). On main only the CBDT JSON fragment named it: IBKR's CSV and workbook
runs printed nothing about it, an otherwise complete Robinhood CSV or workbook
run exited 0, and ``robinhood-all`` failed its Schedule FA leg on the automatic
JSON and removed every output. CSV and workbook contents stay as main writes
them; ``robinhood-all`` now retains its review files without automatic JSON.
Empty or control-character names and non-ASCII upload names keep refusing.
No new line prints statement text: a digit-run marker in another holding's
description and in the Robinhood buy never reaches a stream.
"""

from __future__ import annotations

import csv
import io
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import tests._robinhood_a3_load as a3_load
from ibkr_fa.compute.robinhood_fa import (
    RobinhoodA3IdentityGap,
    RobinhoodA3IdentityGapHolding,
    RobinhoodFaManualKind,
    RobinhoodFaManualWork,
)
from ibkr_fa.exceptions import InvariantError, ParseError
from ibkr_fa.models import FaA3PeakBlankReason
from ibkr_fa.normalize.robinhood import RobinhoodSecurityEventReference
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.parser.format_detect import IbkrCsvFormat
from ibkr_fa.parser.robinhood import parse_robinhood_activity
from ibkr_fa.pipeline_fa_models import RunReport
from ibkr_fa.pipeline_robinhood_reporting import fa_manual_reasons
from ibkr_fa.types import Isin, Ticker
from tests._crash_reports import assert_no_markers
from tests._fa_a3_name_runs import A3_ASCII_REFUSAL as _ASCII
from tests._fa_a3_name_runs import IBKR_FLEX_STATEMENT as _FLEX
from tests._fa_a3_name_runs import IBKR_NAV_STATEMENT as _NAV
from tests._fa_a3_name_runs import NAME_ACTION as _NAME_ACTION
from tests._fa_a3_name_runs import SCHEDULE_FA_SUFFIXES as _FORMATS
from tests._fa_a3_name_runs import XQBB_NAME as _XQBB_NAME
from tests._fa_a3_name_runs import XQBE_NAME as _XQBE_NAME
from tests._fa_a3_name_runs import capture_main as _capture
from tests._fa_a3_name_runs import edit_statement_names as _edit
from tests._fa_a3_name_runs import output_table as _table
from tests._fa_a3_name_runs import robinhood_all_legs as _legs
from tests._fa_a3_name_runs import schedule_fa_with_name as _schedule_fa
from tests._fa_cli_runs import CliRun as _Run
from tests._fa_cli_runs import robinhood_run
from tests._ibkr_run_all import FY_2025_26_INPUT as _FY_2025_26_INPUT
from tests._ibkr_run_all import run_all_argv as _run_all_argv
from tests._ibkr_schedule_fa_inputs import SCHEDULE_FA_CONFIG as _FULL_CONFIG
from tests._ibkr_schedule_fa_inputs import flat_close_fetcher as _flat_close_fetcher
from tests._portal_parity import fix_xlsx_clock
from tests._robinhood_fa_actions import NATURE, listed, peak_action
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_fa_inputs import fa_entity as _entity
from tests._robinhood_statements import ACTIVITY_FOOTER as _ACTIVITY_FOOTER
from tests._robinhood_statements import ACTIVITY_HEADER as _ACTIVITY_HEADER

# A digit run in XQBE's description and in the Robinhood buy; no stream may print it.
_MARKER = "473812"
_NAME_COLUMN = "Name of entity"
_A3_WARNING = (
    "⚠️  The following portal columns contain blank values. "
    "Verify each missing fact and edit the review file before pasting into the portal:\n"
)
_NAME_BLOCK = (
    "   Name of entity is blank for these holdings because the statement's security "
    "description is blank; verify the issuer's legal name and set name in that ISIN's "
    "[[fa.entities]] block, or enter it by hand before filing:\n"
    "     - XQBB (US99995B1044)\n"
)
_A3_EXIT = "\nExit status: 3 (manual-fill columns present — see above).\n"
_WHITESPACE = {"spaces": "   ", "no-break-space": "\u00a0"}


def _assert_only_names_differ(blank: Path, named: Path, name: str, *, old: str) -> None:
    """``blank`` holds ``named``'s files: in each A3 file every Name of entity
    cell that held ``old`` holds ``name`` (empty in the upload CSV, which
    collapses whitespace), and every other file is byte for byte the same."""
    names = sorted(path.name for path in named.iterdir())
    assert sorted(path.name for path in blank.iterdir()) == names
    a3_files = [
        name
        for name in names
        if name.startswith("schedule-fa") and "-a2." not in name and not name.endswith(".json")
    ]
    assert a3_files
    for file_name in names:
        if file_name not in a3_files:
            assert (blank / file_name).read_bytes() == (named / file_name).read_bytes(), file_name
            continue
        expected = _table(named / file_name)
        column = expected[0].index(_NAME_COLUMN)
        filed = "" if expected[0][0] == "Country/Region name" else name
        changed = 0
        for row in expected[1:]:
            if row[column] == old:
                row[column] = filed
                changed += 1
        assert changed
        assert _table(blank / file_name) == expected, file_name


# ---- IBKR: schedule-fa and run-all's Schedule FA leg ---------------------------


@pytest.fixture
def _flat_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", _flat_close_fetcher())
    fix_xlsx_clock(monkeypatch)


_IBKR_CASES = [
    pytest.param(fmt, label, id=f"{fmt}-{label}")
    for fmt in _FORMATS
    for label in _WHITESPACE
    if (fmt, label) != ("filing-csv", "no-break-space")
]


@pytest.mark.usefixtures("_flat_closes")
@pytest.mark.parametrize(("fmt", "label"), _IBKR_CASES)
def test_schedule_fa_names_a_whitespace_only_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fmt: str, label: str
) -> None:
    """The unedited statement prints no Table A3 warning. With XQBB's
    description whitespace only, the run adds one naming Name of entity and
    XQBB, still exits 3 and keeps stderr. The files are the unedited run's with
    XQBB's name cells as main writes them: blank in the upload, the
    whitespace itself in the review CSV and workbook."""
    named = _schedule_fa(tmp_path, capsys, "named", _XQBB_NAME, fmt)
    assert named.rc == 3
    assert _A3_WARNING not in named.out

    name = _WHITESPACE[label]
    blank = _schedule_fa(tmp_path, capsys, "blank", name, fmt)

    anchor = "  Account: U***5678\n"
    assert named.out.count(anchor) == 1
    added = f"\n{_A3_WARNING}  - {_NAME_COLUMN}\n{_NAME_BLOCK}{_A3_EXIT}"
    assert blank.rc == 3
    assert blank.out == named.out.replace(anchor, anchor + added)
    assert blank.err == named.err
    _assert_only_names_differ(blank.out_dir, named.out_dir, name, old=_XQBB_NAME)


@pytest.mark.usefixtures("_flat_closes")
def test_flex_schedule_fa_names_a_whitespace_only_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Flex statement's Table A3 warning already lists other columns; Name
    of entity now comes first, and its block comes before the others."""
    named = _schedule_fa(tmp_path, capsys, "named", _XQBB_NAME, "filing-csv", statement=_FLEX)
    assert named.rc == 3
    assert named.out.count(_A3_WARNING) == 1

    blank = _schedule_fa(tmp_path, capsys, "blank", "   ", "filing-csv", statement=_FLEX)

    columns_end = named.out.index("\n   ", named.out.index(_A3_WARNING)) + 1
    expected = named.out[:columns_end] + _NAME_BLOCK + named.out[columns_end:]
    expected = expected.replace(_A3_WARNING, f"{_A3_WARNING}  - {_NAME_COLUMN}\n")
    assert blank.rc == 3
    assert blank.out == expected
    assert blank.err == named.err
    _assert_only_names_differ(blank.out_dir, named.out_dir, "   ", old=_XQBB_NAME)


def _run_all(tmp_path: Path, capsys: pytest.CaptureFixture[str], label: str, xqbb: str) -> _Run:
    """``run-all --format review-csv`` with the NAV statement, XQBB's
    description ``xqbb`` and XQBE's marked, as ``--cy-input``."""
    run_dir = tmp_path / label
    (run_dir / "out").mkdir(parents=True)
    cy_input = _edit(_NAV, run_dir, xqbb, xqbe=f"{_XQBE_NAME} {_MARKER}")
    argv = [*_run_all_argv(run_dir, _FY_2025_26_INPUT), "--cy-input", str(cy_input)]
    return _capture(run_dir, capsys, argv)


def test_run_all_names_a_whitespace_only_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no ``[[fa.entities]]``, the Schedule FA warning names address, ZIP,
    nature and peak; it now names Name of entity first, with XQBB's block.
    Exit 3, stderr and every file but the A3 review CSV's name cells are the
    unedited run's. Neither stream prints XQBE's marker."""
    named = _run_all(tmp_path, capsys, "named", _XQBB_NAME)
    assert named.rc == 3
    assert named.out.count(_A3_WARNING) == 1
    assert f"  - {_NAME_COLUMN}\n" not in named.out

    blank = _run_all(tmp_path, capsys, "blank", "   ")

    address = "   Address of entity is missing for these holdings;"
    assert named.out.count(address) == 1
    expected = named.out.replace(_A3_WARNING, f"{_A3_WARNING}  - {_NAME_COLUMN}\n").replace(
        address, _NAME_BLOCK + address
    )
    assert blank.rc == 3
    assert blank.out == expected
    assert blank.err == named.err
    names = sorted(path.name for path in named.out_dir.iterdir())
    assert sorted(path.name for path in blank.out_dir.iterdir()) == names
    for file_name in names:
        if file_name != "schedule-fa.csv":
            assert (blank.out_dir / file_name).read_bytes() == (
                named.out_dir / file_name
            ).read_bytes(), file_name
    expected_a3 = _table(named.out_dir / "schedule-fa.csv")
    column = expected_a3[0].index(_NAME_COLUMN)
    for row in expected_a3[1:]:
        if row[column] == _XQBB_NAME:
            row[column] = "   "
    assert _table(blank.out_dir / "schedule-fa.csv") == expected_a3
    assert _MARKER in (blank.out_dir.parent / "statement.csv").read_text(encoding="utf-8")
    assert_no_markers(blank.out + blank.err, _MARKER)


# ---- IBKR controls ---------------------------------------------------------------

_EMPTY_NAME = "error: InvariantError: FaA3Row.name_of_entity must be non-empty\n"
_COMMA_NAME = (
    "error: InvariantError: FaA3Row.name_of_entity must contain a character other than "
    "a comma or a space\n"
)


@pytest.mark.usefixtures("_flat_closes")
@pytest.mark.parametrize("statement", [_NAV, _FLEX], ids=["activity-statement", "flex"])
@pytest.mark.parametrize("fmt", [*_FORMATS, "cbdt-json-fragment"])
@pytest.mark.parametrize(
    ("name", "error"),
    [pytest.param("", _EMPTY_NAME, id="empty"), pytest.param(" , ", _COMMA_NAME, id="comma")],
)
def test_an_empty_or_comma_only_name_still_refuses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    statement: Path,
    fmt: str,
    name: str,
    error: str,
) -> None:
    """Main's row refusals stay: exit 4, the same one-line stderr, no output."""
    run = _schedule_fa(tmp_path, capsys, "run", name, fmt, statement=statement)

    assert run.rc == 4
    assert run.err == error
    assert run.out == ""
    assert list(run.out_dir.iterdir()) == []


@pytest.mark.usefixtures("_flat_closes")
def test_a_no_break_space_name_still_refuses_the_upload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The upload CSV's ASCII check runs before any manual-fill report."""
    run = _schedule_fa(tmp_path, capsys, "run", "\u00a0", "filing-csv")

    assert run.rc == 4
    assert run.err == _ASCII
    assert list(run.out_dir.iterdir()) == []


@pytest.mark.usefixtures("_flat_closes")
def test_schedule_fa_json_keeps_naming_a_whitespace_only_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fragment already refused to file the blank cell: exit 3 and the
    same stderr, no file."""
    run = _schedule_fa(tmp_path, capsys, "run", "   ", "cbdt-json-fragment")

    assert run.rc == 3
    assert run.err == (
        "error: Refusing to emit the CBDT Schedule FA JSON fragment — it would file an "
        "incomplete disclosure. Supply the following and re-run:\n"
        "  - A3 holding in row 1 NameOfEntity: the entity name is blank\n"
    )
    assert run.out == ""
    assert list(run.out_dir.iterdir()) == []


_XQBB_ENTITY = '[[fa.entities]]\nisin = "US99995B1044"\n'


@pytest.mark.usefixtures("_flat_closes")
@pytest.mark.parametrize(("fmt", "rc"), [("filing-csv", 3), ("cbdt-json-fragment", 0)])
def test_a_configured_name_fills_the_cell(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fmt: str, rc: int
) -> None:
    """The remedy: ``name`` in XQBB's ``[[fa.entities]]`` block is filed and
    nothing names the cell. The upload still exits 3 for the A2 peak."""
    assert _FULL_CONFIG.count(_XQBB_ENTITY) == 1
    config = _FULL_CONFIG.replace(_XQBB_ENTITY, f'{_XQBB_ENTITY}name = "XQAA Holdings"\n')
    run = _schedule_fa(tmp_path, capsys, "run", "   ", fmt, config=config)

    assert run.rc == rc
    assert _NAME_COLUMN not in run.out + run.err
    if fmt == "filing-csv":
        rows = _table(run.out_dir / "schedule-fa.csv")
        column = rows[0].index(_NAME_COLUMN)
        assert [row[column] for row in rows[1:]] == ["XQAA Holdings"] * 2 + [_XQBE_NAME]


# ---- Robinhood: robinhood-fa and robinhood-all ----------------------------------

_ROBINHOOD_ISSUER = "Synthetic Corporation"
_ROBINHOOD_SUFFIX = {"filing-csv": "csv", "review-csv": "csv", "xlsx": "xlsx"}


def _robinhood(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    label: str,
    issuer: str,
    command: str,
    *extra: str,
    name: str | None = None,
) -> _Run:
    """``command`` on the #1243 two-account bundle: one buy of 2 shares, with
    issuer name ``issuer`` and the marker in its description and Amount, and
    ``name`` as the security's configured name."""
    run_dir = tmp_path / label
    run_dir.mkdir()
    monkeypatch.setattr(a3_load, "DESCRIPTION", f"{issuer} {_MARKER}")
    buy = replace(_buy("2"), issuer_name=issuer, amount_usd=Decimal(f"-{_MARKER}.00"))
    bundle = a3_load.load_two_accounts(monkeypatch, run_dir, buy)
    bundle.entity_overrides = {
        isin: replace(entity, name=name) for isin, entity in _entity().items()
    }
    events = [event for account in bundle.paired.accounts for event in account.evidence.events]
    assert any(_MARKER in str(event.amount_usd) for event in events)
    assert any(_MARKER in event.description for event in events)
    return robinhood_run(run_dir, capsys, command, *extra)


_ROBINHOOD_CASES = [
    pytest.param(fmt, label, id=f"{fmt}-{label}")
    for fmt in _ROBINHOOD_SUFFIX
    for label in _WHITESPACE
    if (fmt, label) != ("filing-csv", "no-break-space")
]


@pytest.mark.parametrize(("fmt", "label"), _ROBINHOOD_CASES)
def test_robinhood_fa_names_a_whitespace_only_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fmt: str,
    label: str,
) -> None:
    """The named run is ``ok`` and exits 0. With a whitespace-only issuer name
    the leg is ``manual`` with one ``a3-identity`` action naming the cell and
    the setting, and exits 3; main reported ``ok``. Files are the named run's
    with the name cells as main writes them."""
    fix_xlsx_clock(monkeypatch)
    args = ("robinhood-fa", "--format", fmt)
    named = _robinhood(tmp_path, monkeypatch, capsys, "named", _ROBINHOOD_ISSUER, *args)
    assert named.rc == 0
    assert named.out.startswith("Robinhood schedule-fa: ok\n")
    assert "Action:" not in named.out

    name = _WHITESPACE[label]
    blank = _robinhood(tmp_path, monkeypatch, capsys, "blank", name, *args)

    lines = named.out.splitlines(keepends=True)
    assert lines[1:3] == [
        f"  Wrote: <run>/out/schedule-fa-a{table}.{_ROBINHOOD_SUFFIX[fmt]}\n" for table in (2, 3)
    ]
    action = f"  Action: {_NAME_ACTION}\n"
    assert blank.rc == 3
    assert blank.out == "".join(
        ["Robinhood schedule-fa: manual\n", *lines[1:3], action, *lines[3:]]
    )
    assert blank.err == named.err
    _assert_only_names_differ(blank.out_dir, named.out_dir, name, old=_ROBINHOOD_ISSUER)
    assert_no_markers(blank.out + blank.err, _MARKER)


def test_robinhood_fa_json_names_a_whitespace_only_name_as_identity_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``cbdt-json-fragment`` still exits 3 and writes no fragment; its one
    action is now the ``a3-identity`` line, where main printed the JSON gap."""
    args = ("robinhood-fa", "--format", "cbdt-json-fragment")
    named = _robinhood(tmp_path, monkeypatch, capsys, "named", _ROBINHOOD_ISSUER, *args)
    assert named.rc == 0
    assert (named.out_dir / "schedule-fa.json").is_file()

    blank = _robinhood(tmp_path, monkeypatch, capsys, "blank", "   ", *args)

    assert blank.rc == 3
    assert blank.out.startswith(f"Robinhood schedule-fa: manual\n  Action: {_NAME_ACTION}\n")
    assert blank.out.count("Action:") == 1
    assert "NameOfEntity" not in blank.out + blank.err
    assert list(blank.out_dir.iterdir()) == []
    assert blank.err == named.err
    assert_no_markers(blank.out + blank.err, _MARKER)


@pytest.mark.parametrize("fmt", ["review-csv", "xlsx"])
@pytest.mark.parametrize("label", sorted(_WHITESPACE))
def test_robinhood_all_reports_a_whitespace_only_name_as_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fmt: str,
    label: str,
) -> None:
    """The one loosening #1369 sanctions: main failed the schedule-fa leg on
    the automatic JSON's blank-name gap, exited 4 and removed every output.
    Now the leg is ``manual`` with the ``a3-identity`` action, the run exits 3,
    and it keeps the named run's files, less the automatic JSON it no longer
    attempts, with the name cells as main writes them."""
    fix_xlsx_clock(monkeypatch)
    args = ("robinhood-all", "--format", fmt)
    named = _robinhood(tmp_path, monkeypatch, capsys, "named", _ROBINHOOD_ISSUER, *args)
    assert named.rc == 0
    assert (named.out_dir / "schedule-fa.json").is_file()

    name = _WHITESPACE[label]
    blank = _robinhood(tmp_path, monkeypatch, capsys, "blank", name, *args)

    assert blank.rc == 3
    assert _legs(blank.out) == {**_legs(named.out), "schedule-fa": "manual"}
    assert blank.out.count(f"-> {_NAME_ACTION}\n") == 1
    assert "automatic Schedule FA JSON" not in blank.out + blank.err
    assert "NameOfEntity" not in blank.out + blank.err
    assert not (blank.out_dir / "schedule-fa.json").exists()
    assert blank.err == named.err
    (named.out_dir / "schedule-fa.json").unlink()
    _assert_only_names_differ(blank.out_dir, named.out_dir, name, old=_ROBINHOOD_ISSUER)
    assert_no_markers(blank.out + blank.err, _MARKER)


# ---- Robinhood controls -------------------------------------------------------------


@pytest.mark.parametrize(
    ("issuer", "fmt", "error"),
    [
        pytest.param(" , ", "review-csv", _COMMA_NAME, id="comma"),
        pytest.param("\u00a0", "filing-csv", _ASCII, id="no-break-space-upload"),
    ],
)
def test_robinhood_fa_keeps_its_name_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    issuer: str,
    fmt: str,
    error: str,
) -> None:
    """#1329's comma-only refusal and the upload's ASCII check fire as on main."""
    run = _robinhood(tmp_path, monkeypatch, capsys, "run", issuer, "robinhood-fa", "--format", fmt)

    assert run.rc == 4
    assert run.err == error
    assert list(run.out_dir.iterdir()) == []
    assert_no_markers(run.out + run.err, _MARKER)


@pytest.mark.parametrize("fmt", ["review-csv", "xlsx"])
def test_robinhood_all_keeps_the_comma_only_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fmt: str,
) -> None:
    """A comma-only name still fails the schedule-fa leg at the row (exit 4)."""
    run = _robinhood(tmp_path, monkeypatch, capsys, "run", " , ", "robinhood-all", "--format", fmt)

    assert run.rc == 4
    assert _legs(run.out)["schedule-fa"] == "error"
    assert run.out.count(f"-> {_COMMA_NAME.removeprefix('error: ')}") == 1
    assert list(run.out_dir.iterdir()) == []
    assert_no_markers(run.out + run.err, _MARKER)


@pytest.mark.parametrize("fmt", ["filing-csv", "cbdt-json-fragment"])
def test_robinhood_fa_configured_name_fills_the_cell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fmt: str,
) -> None:
    """The remedy: a configured name is filed and the run is ``ok`` (exit 0)."""
    args = ("robinhood-fa", "--format", fmt)
    run = _robinhood(tmp_path, monkeypatch, capsys, "run", "   ", *args, name="XQAA Holdings")

    assert run.rc == 0
    assert run.out.startswith("Robinhood schedule-fa: ok\n")
    assert "Action:" not in run.out
    if fmt == "filing-csv":
        rows = _table(run.out_dir / "schedule-fa-a3.csv")
        column = rows[0].index(_NAME_COLUMN)
        assert [row[column] for row in rows[1:]] == ["XQAA Holdings"]


# ---- Where the name comes from ----------------------------------------------------


def _activity(issuer: str) -> str:
    """A Robinhood activity report with one buy whose issuer line is ``issuer``."""
    out = io.StringIO(newline="")
    writer = csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(_ACTIVITY_HEADER)
    writer.writerow(
        (
            *("1/2/2025", "1/2/2025", "1/3/2025", "XQAA", f"{issuer}\nCUSIP: 99961C102"),
            *("Buy", "1", "$10.00", "($10.00)"),
        )
    )
    writer.writerow(("",))
    writer.writerow(_ACTIVITY_FOOTER)
    return out.getvalue()


@pytest.mark.parametrize("issuer", ["   ", "\u00a0"], ids=["spaces", "no-break-space"])
def test_robinhood_activity_keeps_a_whitespace_only_issuer_name(issuer: str) -> None:
    """The Robinhood regressions above start from what the parser keeps."""
    (event,) = parse_robinhood_activity(_activity(issuer))

    assert event.issuer_name == issuer


def test_robinhood_activity_still_refuses_an_empty_issuer_line() -> None:
    with pytest.raises(ParseError) as raised:
        parse_robinhood_activity(_activity(""))

    assert str(raised.value) == (
        "ParseError in section 'Robinhood account activity' at row 1, column "
        "'Description': security description has an unsupported grammar"
    )


def test_only_identity_tasks_over_a_blank_name_get_the_name_action() -> None:
    """The ``a3-identity`` action names the cell and setting once, for the tasks
    whose lots have a whitespace-only name. Each identity gap's cause then gets
    one line listing every holding with it, a blank-named one's included
    (#1490); on 886182d the line listed only the named holding (#1483)."""
    lots = [RobinhoodSecurityEventReference(0, row) for row in (2, 3, 4)]
    holdings = (
        (Ticker("XQAA"), Isin("US99995B1044")),
        (Ticker("XQBB"), Isin("US99995A1052")),
        (Ticker("ABCD"), Isin("US99971T1034")),
    )
    identity = [
        RobinhoodFaManualWork(
            kind=RobinhoodFaManualKind.A3_IDENTITY,
            security_config_indices=(index,),
            lot_ids=(lot,),
            identity_gaps=frozenset(
                {RobinhoodA3IdentityGapHolding(RobinhoodA3IdentityGap.NATURE, *holding)}
            ),
        )
        for index, (lot, holding) in enumerate(zip(lots, holdings, strict=True))
    ]
    peak = RobinhoodFaManualWork(kind=RobinhoodFaManualKind.A3_PEAK, lot_ids=(lots[2],))
    names = ("   ", "\u3000", _ROBINHOOD_ISSUER)
    peaks = (None, None, FaA3PeakBlankReason.EMPTY_WINDOW)
    a3 = tuple(
        SimpleNamespace(
            lot_id=lot, row=SimpleNamespace(name_of_entity=name, peak_blank_reason=reason)
        )
        for lot, name, reason in zip(lots, names, peaks, strict=True)
    )
    fa = SimpleNamespace(
        manual_work=(identity[0], peak, identity[1], identity[2]),
        a3=a3,
        a3_rows=tuple(review.row for review in a3),
    )

    assert fa_manual_reasons(cast("Any", fa)) == (
        _NAME_ACTION,
        listed(NATURE, "ABCD (US99971T1034)", "XQAA (US99995B1044)", "XQBB (US99995A1052)"),
        peak_action(FaA3PeakBlankReason.EMPTY_WINDOW, 3),
    )


def _report(holdings: tuple[str, ...]) -> RunReport:
    return RunReport(
        rows_written=2,
        output_path=Path("/tmp/schedule-fa.csv"),
        manual_fill_columns=(_NAME_COLUMN,),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        name_of_entity_manual_fill_holdings=holdings,
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )


def test_run_report_names_each_blank_named_holding_once() -> None:
    """Like the address, ZIP and nature lists, the report refuses a repeat."""
    assert _report(("XQBB (US99995B1044)",)).name_of_entity_manual_fill_holdings == (
        "XQBB (US99995B1044)",
    )
    with pytest.raises(InvariantError) as raised:
        _report(("XQBB (US99995B1044)",) * 2)
    assert raised.value.what == "RunReport.name_of_entity_manual_fill_holdings must be unique"
