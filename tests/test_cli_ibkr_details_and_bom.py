"""Details-on statements and garbled byte-order marks refuse through the commands (#1195).

Schedule OS doesn't parse Trades, so only the splitter can refuse a details-on
statement there: a statement whose totals cover every Trades row passed
Schedule OS before #1195 (owner decision 2026-11-11: refuse it everywhere).
``schedule-fa`` refuses the same statements, and garbled byte-order marks in
either IBKR format, with the exact message and no output left behind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ibkr_fa.cli import main
from ibkr_fa.exceptions import ParseError
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_fa import run_fa_a3
from ibkr_fa.pipeline_os import run_schedule_os
from tests._crash_reports import assert_no_markers
from tests._ibkr_details import (
    ACCOUNT,
    ACCOUNT_INFORMATION,
    DETAIL_KINDS,
    DETAILS_OPEN_POSITIONS,
    FLEX_QUERY,
    GARBLED_BOM,
    HOLDER,
    INSTRUMENTS,
    MARKERS,
    STATEMENT,
    activity_statement,
    assert_garbled_bom_refusal,
    assert_open_positions_refusal,
    assert_trades_detail_refusal,
    garbled_bom_text,
    open_positions_text,
    standard_statement,
    trades_detail_text,
)
from tests._ibkr_flex_cli_inputs import earlier_schedule_fa_outputs

_CONFIG = f"""\
[account]
number = "{ACCOUNT}"
holder_name = "{HOLDER}"
opening_date = 2020-01-01
status = "BeneficialOwner"

[fx]
max_carry_forward_days = 14
"""
_FY = FinancialYear(start_year=2025)
_ARRANGEMENTS = pytest.mark.parametrize(
    "open_positions", [False, True], ids=["no-open-positions", "then-open-positions"]
)


def _write(tmp_path: Path, data: str | bytes) -> tuple[Path, Path]:
    statement, config = tmp_path / "statement.csv", tmp_path / "user.toml"
    statement.write_bytes(data.encode() if isinstance(data, str) else data)
    config.write_text(_CONFIG, encoding="utf-8")
    return statement, config


# ---- Schedule OS, which doesn't parse Trades -----------------------------------


def test_schedule_os_accepts_the_standard_statement_without_instruments(tmp_path: Path) -> None:
    """The control: Order rows with their totals and no Financial Instrument
    Information pass Schedule OS, which never parses Trades."""
    statement, config = _write(tmp_path, standard_statement())
    out = tmp_path / "schedule-os.csv"
    report = run_schedule_os(
        input_path=statement, config_path=config, output_path=out, financial_year=_FY
    )
    assert report.ordinary_dividend_rows_written == 0
    assert out.is_file()


@_ARRANGEMENTS
@pytest.mark.parametrize("kind", DETAIL_KINDS)
def test_schedule_os_refuses_a_detail_row_its_totals_cover(
    tmp_path: Path, kind: str, open_positions: bool
) -> None:
    """New refusal: with no Open Positions, this statement passed Schedule OS
    before #1195, since its totals cover the Order and detail rows."""
    text = activity_statement(kind, totals="all", open_positions=open_positions).removesuffix(
        INSTRUMENTS
    )
    statement, config = _write(tmp_path, text)
    out = tmp_path / "schedule-os.csv"
    with pytest.raises(ParseError) as caught:
        run_schedule_os(
            input_path=statement, config_path=config, output_path=out, financial_year=_FY
        )
    assert_trades_detail_refusal(caught.value, 5, source=text)
    assert not out.exists()


@pytest.mark.parametrize("stale", [False, True], ids=["no-earlier-output", "earlier-output"])
@_ARRANGEMENTS
@pytest.mark.parametrize("kind", DETAIL_KINDS)
def test_schedule_os_cli_refuses_a_detail_row(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    open_positions: bool,
    stale: bool,
) -> None:
    text = activity_statement(kind, totals="all", open_positions=open_positions).removesuffix(
        INSTRUMENTS
    )
    statement, config = _write(tmp_path, text)
    out = tmp_path / "schedule-os.csv"
    if stale:
        out.write_text("earlier run", encoding="utf-8")
    argv = ["schedule-os", "--input", str(statement), "--config", str(config)]
    rc = main([*argv, "--output", str(out), "--financial-year", "2025-26"])
    captured = capsys.readouterr()
    assert (rc, captured.out, captured.err) == (4, "", f"error: {trades_detail_text(5)}\n")
    assert_no_markers(
        captured.out + captured.err, *(marker for marker in MARKERS if marker in text)
    )
    assert sorted(path.name for path in tmp_path.iterdir()) == ["statement.csv", "user.toml"]


# ---- Schedule FA ------------------------------------------------------------

# Each input, and the complete refusal it gets: an Open Positions header on
# line 3, a Trades detail row on line 5, or a garbled mark the detector refuses.
_FA_INPUTS: dict[str, tuple[bytes, str]] = {
    "open-positions": (
        (STATEMENT + DETAILS_OPEN_POSITIONS + ACCOUNT_INFORMATION + INSTRUMENTS).encode(),
        open_positions_text(3),
    ),
    **{
        f"trades-{kind}": (activity_statement(kind, totals="all").encode(), trades_detail_text(5))
        for kind in DETAIL_KINDS
    },
    "bom-activity": (GARBLED_BOM + standard_statement().encode(), garbled_bom_text(0)),
    "bom-flex": (GARBLED_BOM + FLEX_QUERY.encode(), garbled_bom_text(0)),
}
_FORMATS = pytest.mark.parametrize(
    ("format_id", "suffix", "extra"),
    [
        ("review-csv", ".csv", ()),
        ("filing-csv", ".csv", ()),
        ("xlsx", ".xlsx", ()),
        ("cbdt-json-fragment", ".json", ("--itr-form", "ITR-2")),
    ],
)


def _schedule_fa(statement: Path, config: Path, out: Path, format_id: str, *extra: str) -> int:
    argv = ["schedule-fa", "--input", str(statement), "--config", str(config)]
    argv += ["--output", str(out), "--calendar-year", "2025", "--format", format_id]
    return main([*argv, *extra])


@pytest.mark.parametrize("stale", [False, True], ids=["no-earlier-output", "earlier-output"])
@_FORMATS
@pytest.mark.parametrize("name", list(_FA_INPUTS))
def test_schedule_fa_cli_refuses_and_leaves_no_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    format_id: str,
    suffix: str,
    extra: tuple[str, ...],
    stale: bool,
) -> None:
    """Exit 4, nothing on stdout, the complete refusal on stderr, and no A3, A2
    or CBDT JSON output left, including one an earlier run wrote."""
    data, text = _FA_INPUTS[name]
    statement, config = _write(tmp_path, data)
    out = tmp_path / f"schedule-fa{suffix}"
    if stale:
        earlier_schedule_fa_outputs(out, format_id)
    rc = _schedule_fa(statement, config, out, format_id, *extra)
    captured = capsys.readouterr()
    assert (rc, captured.out, captured.err) == (4, "", f"error: {text}\n")
    assert_no_markers(
        captured.out + captured.err, *(marker for marker in MARKERS if marker in data.decode())
    )
    assert sorted(path.name for path in tmp_path.iterdir()) == ["statement.csv", "user.toml"]


def test_schedule_fa_keeps_a_foreign_json_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A JSON file ibkr-fa didn't write is kept, and refused before parsing."""
    statement, config = _write(tmp_path, activity_statement("Trade", totals="all"))
    out = tmp_path / "schedule-fa.json"
    out.write_text('{"ScheduleFA": {}}', encoding="utf-8")
    rc = _schedule_fa(statement, config, out, "cbdt-json-fragment", "--itr-form", "ITR-2")
    captured = capsys.readouterr()
    assert (rc, captured.out) == (4, "")
    assert captured.err.startswith(f"error: refusing to overwrite {out} ")
    assert out.read_text(encoding="utf-8") == '{"ScheduleFA": {}}'


@pytest.mark.parametrize("target", ["input", "config"])
def test_schedule_fa_keeps_the_collision_guard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    """An output path naming the input or config refuses before parsing, and
    neither file is touched."""
    statement, config = _write(tmp_path, activity_statement("Trade", totals="all"))
    before = {path: path.read_bytes() for path in (statement, config)}
    out = statement if target == "input" else config
    rc = _schedule_fa(statement, config, out, "review-csv")
    captured = capsys.readouterr()
    assert (rc, captured.out) == (4, "")
    assert captured.err.startswith("error: ")
    assert f"--{target}" in captured.err
    assert "trade details" not in captured.err
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("name", list(_FA_INPUTS))
def test_schedule_fa_pipeline_raises_the_exact_refusal(tmp_path: Path, name: str) -> None:
    data, _ = _FA_INPUTS[name]
    statement, config = _write(tmp_path, data)
    with pytest.raises(ParseError) as caught:
        run_fa_a3(
            input_path=statement,
            config_path=config,
            output_path=tmp_path / "schedule-fa.csv",
            calendar_year=2025,
        )
    if name == "open-positions":
        assert_open_positions_refusal(caught.value, 3, source=data.decode())
    elif name.startswith("trades-"):
        assert_trades_detail_refusal(caught.value, 5, source=data.decode())
    else:
        assert_garbled_bom_refusal(caught.value, 0, source=data.decode())
