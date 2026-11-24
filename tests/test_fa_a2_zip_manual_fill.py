"""A blank Schedule FA Table A2 ZIP Code is named as a cell to fill by hand (#1358).

Both vendored CBDT schemas require the A2 ZIP, but every A2 config block lets
it be blank. On main, IBKR and NetBenefits runs wrote the blank cell and listed
the other cells to fill by hand without it, and an otherwise complete Robinhood
CSV or XLSX run exited 0. Now each run names the ZIP Code and the setting that
supplies it as manual work (exit 3 unless something else fails), and each A2
file it writes keeps the ZIP-set run's cells with the ZIP blank. Robinhood
writes no JSON fragment for a blank ZIP. No new line prints the configured
address, which carries a digit-run marker.
"""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

import openpyxl
import pytest

import ibkr_fa.pipeline_robinhood as robinhood_pipeline
from ibkr_fa.cli import main
from ibkr_fa.compute.form67 import Form67Readiness
from ibkr_fa.output.netbenefits_quicko_fa_xlsx import QUICKO_TEMPLATE_FILENAME
from tests import _robinhood_dividend_year_bundle as dividend_year
from tests._crash_reports import assert_no_markers
from tests._ibkr_schedule_fa_inputs import SCHEDULE_FA_CONFIG_NO_PEAK as _CONFIG_NO_PEAK
from tests._netbenefits_e2e import NB_E2E_TOML, nb_e2e_bundle
from tests._netbenefits_fa import write_quicko_template
from tests._portal_parity import fix_xlsx_clock
from tests._robinhood_cli import LEG_LINE
from tests._robinhood_cli import robinhood_main as _robinhood_main
from tests._robinhood_fa_inputs import fa_account as _account
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_fa_inputs import fa_entity as _entity
from tests._robinhood_income_stubs import form67_assembly

_FIXTURES = Path(__file__).parent / "fixtures"
_CY_INPUT = _FIXTURES / "e2e_activity_statement_with_nav_2025.csv"
_FY_INPUT = _FIXTURES / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
# A digit run in each configured A2 address; no stream may print it.
_MARKER = "736251"
_ZIP_COLUMN = "ZIP Code"


class _Run(NamedTuple):
    rc: int
    out: str
    err: str
    out_dir: Path


def _capture(run_dir: Path, capsys: pytest.CaptureFixture[str], argv: list[str]) -> _Run:
    """Run ``argv``; return its exit, its streams with ``run_dir`` masked, and
    its output folder."""
    capsys.readouterr()
    rc = main(argv)
    captured = capsys.readouterr()
    return _Run(
        rc,
        captured.out.replace(str(run_dir), "<run>"),
        captured.err.replace(str(run_dir), "<run>"),
        run_dir / "out",
    )


def _table(path: Path) -> list[list[object]]:
    """Every CSV row, or every non-empty row of each workbook sheet."""
    if path.suffix == ".xlsx":
        workbook = openpyxl.load_workbook(path)
        rows = [list(row) for sheet in workbook for row in sheet.iter_rows(values_only=True)]
        return [row for row in rows if any(cell is not None for cell in row)]
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [list(row) for row in csv.reader(handle)]


def _blank_zip(path: Path) -> list[list[object]]:
    """``path``'s table with every A2 ZIP Code cell read as ``""``: the
    writers keep an empty cell empty and may keep whitespace."""
    table = _table(path)
    column = table[0].index(_ZIP_COLUMN)
    for row in table[1:]:
        assert str(row[column] or "").strip() == "", row[column]
        row[column] = ""
    return table


def _assert_only_the_a2_zip_differs(
    blank: Path, with_zip: Path, *, without: tuple[str, ...] = ()
) -> None:
    """``blank`` holds ``with_zip``'s files except ``without``: each A2 file
    with its ZIP Code cells blank, every other file byte for byte."""
    names = sorted(path.name for path in with_zip.iterdir() if path.name not in without)
    assert sorted(path.name for path in blank.iterdir()) == names
    a2_files = [name for name in names if "-a2." in name]
    assert a2_files
    for name in names:
        if name in a2_files:
            expected = _table(with_zip / name)
            column = expected[0].index(_ZIP_COLUMN)
            assert all(row[column] not in (None, "") for row in expected[1:])
            for row in expected[1:]:
                row[column] = ""
            assert _blank_zip(blank / name) == expected, name
        else:
            assert (blank / name).read_bytes() == (with_zip / name).read_bytes(), name


# ---- IBKR: schedule-fa and run-all's Schedule FA leg --------------------------

_IBKR_ZIP = 'zip = "06830"\n'
_IBKR_CONFIG = _CONFIG_NO_PEAK.replace("One Pickwick Plaza,", f"One Pickwick Plaza {_MARKER},")
_IBKR_A2_LIST = (
    "  ⚠️  Fill these Table A2 portal cell(s) before filing — left "
    "blank because they could not be computed with certainty:\n"
)
_IBKR_ZIP_LINES = (
    "    - ZIP Code\n"
    "      Set [account.custodian].zip to the institution's postal code and re-run, "
    "or fill it in by hand before filing.\n"
)
_IBKR_COMMANDS = {
    "filing-csv": ("schedule-fa", "filing-csv", ".csv"),
    "review-csv": ("schedule-fa", "review-csv", ".csv"),
    "xlsx": ("schedule-fa", "xlsx", ".xlsx"),
    "run-all": ("run-all", "review-csv", ""),
}


def _ibkr_argv(run_dir: Path, command: str) -> list[str]:
    name, format_id, suffix = _IBKR_COMMANDS[command]
    config = ("--config", str(run_dir / "user.toml"))
    if name == "run-all":
        return [
            "run-all",
            *("--fy-input", str(_FY_INPUT), "--cy-input", str(_CY_INPUT), *config),
            *("--output-dir", str(run_dir / "out"), "--financial-year", "2025-26"),
            *("--format", format_id, "--itr-form", "ITR-2", "--no-fetch-prices"),
        ]
    return [
        "schedule-fa",
        *("--input", str(_CY_INPUT), *config),
        *("--output", str(run_dir / "out" / f"schedule-fa{suffix}")),
        *("--calendar-year", "2025", "--format", format_id, "--itr-form", "ITR-3"),
        "--no-fetch-prices",
    ]


def _ibkr_run(run_dir: Path, capsys: pytest.CaptureFixture[str], config: str, command: str) -> _Run:
    (run_dir / "out").mkdir(parents=True)
    (run_dir / "user.toml").write_text(config, encoding="utf-8")
    return _capture(run_dir, capsys, _ibkr_argv(run_dir, command))


@pytest.mark.parametrize(
    "zip_line",
    [
        pytest.param("", id="absent"),
        pytest.param('zip = ""\n', id="empty"),
        pytest.param('zip = "   "\n', id="whitespace-only"),
    ],
)
@pytest.mark.parametrize("command", sorted(_IBKR_COMMANDS))
def test_ibkr_names_a_blank_a2_zip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    zip_line: str,
) -> None:
    """The A2 list gains the ZIP Code and the setting, before the peak; exit 3
    and stderr stay as with the ZIP set. Files differ only in the A2 ZIP
    cells. Main printed the same output with and without the ZIP."""
    fix_xlsx_clock(monkeypatch)
    with_zip = _ibkr_run(tmp_path / "with-zip", capsys, _IBKR_CONFIG, command)
    assert with_zip.rc == 3
    assert with_zip.out.count(_IBKR_A2_LIST) == 1
    assert _ZIP_COLUMN not in with_zip.out

    assert _IBKR_CONFIG.count(_IBKR_ZIP) == 1
    blank = _ibkr_run(
        tmp_path / "blank", capsys, _IBKR_CONFIG.replace(_IBKR_ZIP, zip_line), command
    )

    assert blank.rc == 3
    assert blank.out == with_zip.out.replace(_IBKR_A2_LIST, _IBKR_A2_LIST + _IBKR_ZIP_LINES)
    assert blank.err == with_zip.err
    _assert_only_the_a2_zip_differs(blank.out_dir, with_zip.out_dir)
    assert _MARKER in _IBKR_CONFIG
    assert_no_markers(blank.out + blank.err, _MARKER)


# ---- NetBenefits: netbenefits-fa and netbenefits-all --------------------------

_NETBENEFITS_ZIP = 'zip = "02210"\n'
_NETBENEFITS_CONFIG = NB_E2E_TOML.replace("245 Summer Street,", f"245 Summer Street {_MARKER},")
_NETBENEFITS_PEAK = "  Peak Balance During the Period: MANUAL-FILL — "
_NETBENEFITS_ZIP_LINE = (
    "  ZIP Code: MANUAL-FILL — set [netbenefits.account].zip to the institution's "
    "postal code and re-run, or fill it in by hand before filing.\n"
)
_NETBENEFITS_FA = ("--output", "out/schedule-fa-a3.csv", "--calendar-year", "2025")


@pytest.mark.parametrize(
    ("argv", "quicko"),
    [
        pytest.param(("netbenefits-fa", *_NETBENEFITS_FA), False, id="filing-csv"),
        pytest.param(
            ("netbenefits-fa", *_NETBENEFITS_FA, "--format", "review-csv"), False, id="review-csv"
        ),
        pytest.param(("netbenefits-fa", *_NETBENEFITS_FA, "--quicko-workbook"), True, id="quicko"),
        pytest.param(
            (
                "netbenefits-all",
                *("--output-dir", "out", "--financial-year", "2025-26", "--itr-form", "ITR-2"),
            ),
            False,
            id="all",
        ),
    ],
)
def test_netbenefits_names_a_blank_a2_zip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...],
    *,
    quicko: bool,
) -> None:
    """Without ``[netbenefits.account].zip`` the A2 block gains the ZIP line,
    which gives no peak advice, before the peak line. Exit 3 and stderr stay
    as with the ZIP set; files differ only in the A2 ZIP cells. Main printed
    the same output either way."""
    fix_xlsx_clock(monkeypatch)

    def run(name: str, config: str) -> _Run:
        run_dir = tmp_path / name
        run_dir.mkdir()
        bundle, config_path = nb_e2e_bundle(run_dir, monkeypatch)
        config_path.write_text(config, encoding="utf-8")
        if quicko:
            write_quicko_template(bundle / QUICKO_TEMPLATE_FILENAME)
        (run_dir / "out").mkdir()
        args = [str(run_dir / arg) if arg.startswith("out") else arg for arg in argv]
        return _capture(
            run_dir,
            capsys,
            [
                *args[:1],
                *("--input", str(bundle), "--config", str(config_path)),
                *args[1:],
                "--no-fetch-prices",
            ],
        )

    with_zip = run("with-zip", _NETBENEFITS_CONFIG)
    assert with_zip.rc == 3
    assert with_zip.out.count(_NETBENEFITS_PEAK) == 1
    assert "ZIP Code: MANUAL-FILL" not in with_zip.out

    assert _NETBENEFITS_CONFIG.count(_NETBENEFITS_ZIP) == 1
    blank = run("blank", _NETBENEFITS_CONFIG.replace(_NETBENEFITS_ZIP, ""))

    assert blank.rc == 3
    assert "peak" not in _NETBENEFITS_ZIP_LINE.lower()
    assert blank.out == with_zip.out.replace(
        _NETBENEFITS_PEAK, _NETBENEFITS_ZIP_LINE + _NETBENEFITS_PEAK
    )
    assert blank.err == with_zip.err
    _assert_only_the_a2_zip_differs(blank.out_dir, with_zip.out_dir)
    assert _MARKER in _NETBENEFITS_CONFIG
    assert_no_markers(blank.out + blank.err, _MARKER)


# ---- Robinhood: robinhood-fa and robinhood-all --------------------------------

_ROBINHOOD_ADDRESS = f"500 Colonial Center Parkway {_MARKER}, Lake Mary, FL"
_A2_ZIP_REASON = (
    "a2-zip: set [[robinhood.accounts]] zip to the institution's postal code "
    "and re-run, or fill the Table A2 ZIP Code by hand before filing\n"
)
_A2_ZIP_ACTION = f"  Action: {_A2_ZIP_REASON}"
_SUFFIX = {"filing-csv": "csv", "review-csv": "csv", "xlsx": "xlsx"}
_ROBINHOOD_FA_FORMATS = pytest.mark.parametrize("format_id", sorted(_SUFFIX))


def _robinhood_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    zip_code: str,
    command: str,
    *extra: str,
) -> _Run:
    """Run ``command`` on the #1320 two-account bundle (dividend posted
    December 31), with both accounts' ``zip_code`` and the marked address."""
    run_dir = tmp_path / name
    run_dir.mkdir()
    monkeypatch.setattr(
        dividend_year,
        "_account",
        lambda number: replace(_account(number), address=_ROBINHOOD_ADDRESS, zip_code=zip_code),
    )
    dividend_year._load(monkeypatch, run_dir, date(2025, 12, 31), date(2026, 1, 2))
    capsys.readouterr()
    rc = _robinhood_main(command, run_dir, "--no-fetch-prices", *extra)
    captured = capsys.readouterr()
    return _Run(
        rc,
        captured.out.replace(str(run_dir), "<run>"),
        captured.err.replace(str(run_dir), "<run>"),
        run_dir / "out",
    )


def _income_ftc_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bundle has no annual tax forms, so its income-ftc leg fails. Stub
    that leg to succeed, as ``tests/test_pipeline_robinhood.py`` stubs it;
    Schedule FA still reads the real source income."""
    compute_income = robinhood_pipeline._compute_income

    def income_without_annual_forms(*args: Any, **kwargs: Any) -> tuple[object, ...]:
        income, _assembled, _tr = compute_income(*args, **kwargs)
        return replace(income, blockers=()), form67_assembly(), object()

    monkeypatch.setattr(robinhood_pipeline, "_compute_income", income_without_annual_forms)
    monkeypatch.setattr(robinhood_pipeline, "_income_warnings", lambda _assembled: ())
    monkeypatch.setattr(
        robinhood_pipeline, "_write_income", lambda *_args, **_kwargs: ((), Form67Readiness.READY)
    )


@_ROBINHOOD_FA_FORMATS
@pytest.mark.parametrize("zip_code", ["", "   "], ids=["empty", "whitespace-only"])
def test_robinhood_fa_names_a_blank_a2_zip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    format_id: str,
    zip_code: str,
) -> None:
    """The leg is ``manual`` with the ``a2-zip`` action and exits 3; main
    reported ``ok`` and exited 0. Files differ from the ZIP-set run only in
    the A2 ZIP cells."""
    fix_xlsx_clock(monkeypatch)
    args = ("robinhood-fa", "--format", format_id)
    with_zip = _robinhood_run(tmp_path, monkeypatch, capsys, "with-zip", "32746", *args)
    assert with_zip.rc == 0
    assert with_zip.out.startswith("Robinhood schedule-fa: ok\n")
    assert "Action:" not in with_zip.out

    blank = _robinhood_run(tmp_path, monkeypatch, capsys, "blank", zip_code, *args)

    assert blank.rc == 3
    lines = with_zip.out.splitlines(keepends=True)
    assert lines[0] == "Robinhood schedule-fa: ok\n"
    assert lines[1:3] == [
        f"  Wrote: <run>/out/schedule-fa-a{table}.{_SUFFIX[format_id]}\n" for table in (2, 3)
    ]
    expected = ["Robinhood schedule-fa: manual\n", *lines[1:3], _A2_ZIP_ACTION, *lines[3:]]
    assert blank.out == "".join(expected)
    assert blank.err == with_zip.err
    _assert_only_the_a2_zip_differs(blank.out_dir, with_zip.out_dir)
    assert_no_markers(blank.out + blank.err, _MARKER)


def test_robinhood_fa_json_names_a_blank_a2_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``cbdt-json-fragment`` still exits 3 and writes no fragment; its action
    is the ``a2-zip`` line, where main printed the JSON gap."""
    args = ("robinhood-fa", "--format", "cbdt-json-fragment")
    with_zip = _robinhood_run(tmp_path, monkeypatch, capsys, "with-zip", "32746", *args)
    assert with_zip.rc == 0
    assert (with_zip.out_dir / "schedule-fa.json").is_file()

    blank = _robinhood_run(tmp_path, monkeypatch, capsys, "blank", "", *args)

    assert blank.rc == 3
    assert blank.out.startswith(f"Robinhood schedule-fa: manual\n{_A2_ZIP_ACTION}")
    assert blank.out.count("Action:") == 1
    assert "ZipCode" not in blank.out + blank.err
    assert list(blank.out_dir.iterdir()) == []
    assert blank.err == with_zip.err
    assert_no_markers(blank.out + blank.err, _MARKER)


def _legs(stdout: str) -> dict[str, str]:
    return {name: status for status, name in LEG_LINE.findall(stdout)}


def test_robinhood_all_reports_a_blank_a2_zip_as_manual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The schedule-fa leg is ``manual`` with one ``a2-zip`` action for both
    accounts, where main tried the automatic JSON and failed the leg. This
    bundle's income-ftc leg fails for its own reason, so both runs exit 4."""
    with_zip = _robinhood_run(tmp_path, monkeypatch, capsys, "with-zip", "32746", "robinhood-all")
    blank = _robinhood_run(tmp_path, monkeypatch, capsys, "blank", "", "robinhood-all")

    assert with_zip.rc == blank.rc == 4
    assert _legs(with_zip.out)["schedule-fa"] == "ok"
    assert _legs(blank.out) == {**_legs(with_zip.out), "schedule-fa": "manual"}
    assert blank.out.count(_A2_ZIP_REASON) == 1
    assert "ZipCode" not in blank.out + blank.err
    assert blank.err == with_zip.err
    assert_no_markers(blank.out + blank.err, _MARKER)


def test_robinhood_all_exits_3_for_a_blank_a2_zip_when_every_other_leg_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one loosening #1358 sanctions: main failed the schedule-fa leg on
    the automatic JSON, exited 4 and removed every output. Now the run exits
    3 and keeps the ZIP-set run's files with the A2 ZIP cells blank, less
    the automatic JSON."""
    fix_xlsx_clock(monkeypatch)
    _income_ftc_succeeds(monkeypatch)
    with_zip = _robinhood_run(tmp_path, monkeypatch, capsys, "with-zip", "32746", "robinhood-all")
    assert with_zip.rc == 0
    assert (with_zip.out_dir / "schedule-fa.json").is_file()

    blank = _robinhood_run(tmp_path, monkeypatch, capsys, "blank", "", "robinhood-all")

    assert blank.rc == 3
    assert _legs(blank.out) == {**_legs(with_zip.out), "schedule-fa": "manual"}
    assert "error" not in _legs(blank.out).values()
    assert blank.out.count(_A2_ZIP_REASON) == 1
    assert "ZipCode" not in blank.out + blank.err
    assert blank.err == with_zip.err
    _assert_only_the_a2_zip_differs(blank.out_dir, with_zip.out_dir, without=("schedule-fa.json",))
    assert_no_markers(blank.out + blank.err, _MARKER)


_ROW_REFUSAL = (
    "  error        schedule-fa\n"
    "                 -> InvariantError: FaA3Row.name_of_entity has a control character at "
    "position 10; filed text may not contain control characters\n"
)


@pytest.mark.parametrize("a3_zip", ["99990", ""], ids=["a3-zip-set", "a3-zip-blank"])
def test_robinhood_all_attempts_no_automatic_json_for_a_blank_a2_zip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    a3_zip: str,
) -> None:
    """A synthetic tab in the issuer name. #1358 pinned that a blank A2 ZIP, as
    manual work, kept the run away from the automatic JSON's own refusal of it,
    so the run exited 3. Since #1367 the Table A3 row refuses it before any
    manual-fill work or JSON: whatever the A2 and A3 ZIPs, the schedule-fa leg
    fails, the run exits 4 and its outputs are removed."""
    issuer = "Synthetic\tCorporation"
    monkeypatch.setattr(dividend_year, "_buy", lambda: replace(_buy(), issuer_name=issuer))
    monkeypatch.setattr(
        dividend_year,
        "_entity",
        lambda: {
            key: replace(value, name=None, zip_code=a3_zip) for key, value in _entity().items()
        },
    )
    _income_ftc_succeeds(monkeypatch)
    with_zip = _robinhood_run(tmp_path, monkeypatch, capsys, "with-zip", "32746", "robinhood-all")
    blank = _robinhood_run(tmp_path, monkeypatch, capsys, "blank", "", "robinhood-all")

    assert blank.err == with_zip.err
    for run in (with_zip, blank):
        assert run.rc == 4
        assert _legs(run.out)["schedule-fa"] == "error"
        assert run.out.count(_ROW_REFUSAL) == 1
        assert "automatic Schedule FA JSON" not in run.out
        assert list(run.out_dir.iterdir()) == []
        assert issuer not in run.out + run.err
        assert_no_markers(run.out + run.err, _MARKER)
