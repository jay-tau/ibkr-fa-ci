"""A Robinhood security configured with issuer country India, through
robinhood-fa and robinhood-all (#1325).

Table A3's country dropdown leaves India out, so no format may file the row.
The bundle is the exact-A3 tests' synthetic two-account bundle, holding 2
shares bought in CY 2025; only the bundle load, the TTBR table and the Yahoo
closes are stubbed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ibkr_fa import cli
from tests._no_echo import assert_no_markers
from tests._robinhood_a3_exact import DESCRIPTION, SYMBOL
from tests._robinhood_a3_load import load_two_accounts as _load
from tests._robinhood_cli import robinhood_argv
from tests._robinhood_fa_inputs import FA_CUSIP as _CUSIP
from tests._robinhood_fa_inputs import FA_ISIN as _ISIN
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_refusals import INDIA_REFUSAL

# Security symbol, description, CUSIP, ISIN, and entity name and address.
_MARKERS = (SYMBOL, DESCRIPTION, _CUSIP, _ISIN, "Synthetic Corporation", "One Synthetic Way")


def _argv(tmp_path: Path, command: str, itr_form: str, *extra: str) -> list[str]:
    return [*robinhood_argv(command, tmp_path, itr_form=itr_form, no_fetch_prices=True), *extra]


@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
@pytest.mark.parametrize(
    ("format_id", "written"),
    [
        ("review-csv", ["schedule-fa-a2.csv", "schedule-fa-a3.csv"]),
        ("filing-csv", ["schedule-fa-a2.csv", "schedule-fa-a3.csv"]),
        ("xlsx", ["schedule-fa-a2.xlsx", "schedule-fa-a3.xlsx"]),
        ("cbdt-json-fragment", ["schedule-fa.json"]),
    ],
)
def test_robinhood_fa_refuses_an_india_issuer_and_removes_its_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    format_id: str,
    written: list[str],
    itr_form: str,
) -> None:
    """With a US issuer the run files its outputs. Configured as India, the
    same run exits 4 with the fixed refusal alone, prints nothing else, names
    none of the security's identifiers, and leaves none of the earlier run's
    outputs. Main wrote India (code 91) to the review and filing CSVs and the
    XLSX, and the JSON fragment refused only at schema validation."""
    argv = _argv(tmp_path, "robinhood-fa", itr_form, "--format", format_id)
    out = tmp_path / "out"
    _load(monkeypatch, tmp_path, _buy("2"))
    assert cli.main(argv) == 0
    assert sorted(path.name for path in out.iterdir()) == written
    capsys.readouterr()
    _load(monkeypatch, tmp_path, _buy("2"), country="IN")

    assert cli.main(argv) == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: {INDIA_REFUSAL}\n"
    assert captured.out == ""
    assert list(out.iterdir()) == []
    assert_no_markers(captured.err, *_MARKERS)


def test_robinhood_all_fails_the_schedule_fa_leg_on_an_india_issuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """In robinhood-all the refusal fails the schedule-fa leg with its fixed
    text; the run exits 4, removes every output, an earlier run's included,
    and names none of the security's identifiers."""
    argv = _argv(tmp_path, "robinhood-all", "ITR-2")
    out = tmp_path / "out"
    _load(monkeypatch, tmp_path, _buy("2"))
    assert cli.main(argv) == 0
    assert "schedule-fa-a3.csv" in [path.name for path in out.iterdir()]
    capsys.readouterr()
    _load(monkeypatch, tmp_path, _buy("2"), country="IN")

    assert cli.main(argv) == 4

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    [leg] = [index for index, line in enumerate(lines) if line.split() == ["error", "schedule-fa"]]
    assert lines[leg + 1].strip() == f"-> {INDIA_REFUSAL}"
    assert captured.err == ""
    assert list(out.iterdir()) == []
    assert_no_markers(captured.out, *_MARKERS)
