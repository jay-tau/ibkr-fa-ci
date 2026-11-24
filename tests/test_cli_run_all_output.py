"""Characterization tests: what a real ``run-all`` prints, and its exit code (#613).

Nothing is monkeypatched. Each snapshot case builds synthetic statements and a
``user.toml`` from ``tests/fixtures``, runs ``main(["run-all", ...])`` offline
(``--no-fetch-prices``), replaces the tmp path with ``<tmp>`` and compares
stdout and stderr with ``tests/fixtures/run_all_output/<case>.std{out,err}.txt``.
Every snapshot case exits 3 because Schedule FA has no prices for its peak values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ibkr_fa import cli
from tests._ibkr_run_all import run_all_from_blocks as _run_all

_FIXTURES = Path(__file__).parent / "fixtures"
_EXPECTED = _FIXTURES / "run_all_output"

# Every case passes --cy-input, so Schedule FA runs too.
_CASES = {
    # Every config block present, no trades: no conditional advisory fires.
    "all-clear": {},
    "masked-account": {"masked": True},
    "foreign-tin-missing": {"tins": False},
    # The XQBA round trip adds a US capital-gains row with no foreign tax.
    # Its Indian tax payable stays blank, which no schedule flags (#1328).
    "tax-payable-missing-tr": {"trades": True},
    # No [form67]: FSI, TR and Form 67 all flag the missing tax payable, and
    # the 15.38% WHT rate (0.40 / 2.60) adds Form 67's fractional-rate warning
    # before it.
    "tax-payable-missing": {"tax_payable": False, "fractional_rate": True},
    "a2-skipped": {"custodian": False},
    "quiet-everything-fires": {
        "masked": True,
        "tins": False,
        "tax_payable": False,
        "fractional_rate": True,
        "custodian": False,
        "quiet": True,
    },
}


@pytest.mark.parametrize("case", list(_CASES))
def test_run_all_output(tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str) -> None:
    rc = _run_all(tmp_path, **_CASES[case])

    captured = capsys.readouterr()
    assert rc == 3
    for stream, text in (("stdout", captured.out), ("stderr", captured.err)):
        expected = (_EXPECTED / f"{case}.{stream}.txt").read_text(encoding="utf-8")
        assert text.replace(str(tmp_path), "<tmp>") == expected, stream


def test_run_all_lists_a_blank_form67_entry_beside_a_complete_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """US has its col 7 override and a credit; GB has neither (#791)."""
    assert _run_all(tmp_path, blank_gb=True) == 3

    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert "POPULATED (cols 7, 9-13)" not in stream
        assert "populated from exact overrides" not in stream
        assert "GB/DIVIDEND: Indian tax payable and credit blank; complete both by hand" in stream
        assert "Indian tax payable and credit are filled for every other entry" in stream


@pytest.mark.parametrize(
    ("tins", "status", "expected_rc"), [(False, "manual_fill", 3), (True, "ok", 0)]
)
def test_run_all_exits_3_on_a_blank_foreign_tin_without_schedule_fa(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tins: bool, status: str, expected_rc: int
) -> None:
    """With Schedule FA skipped and Form 67 complete, the blank US TIN alone
    makes run-all exit 3, with the FSI and TR legs at manual-fill (#924)."""
    assert _run_all(tmp_path, tins=tins, cy=False) == expected_rc

    summary = capsys.readouterr().err.split("RUN-ALL SUMMARY", 1)[1]
    for name in ("schedule-fsi", "schedule-tr"):
        assert f"  {cli._RUN_ALL_STATUS_LABEL[status]}  {name} " in summary


def test_run_all_checklist_does_not_read_the_advisory_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The "Before you file" items come from the reports, so rewording the
    advisories the schedules print still lists both."""
    monkeypatch.setattr(cli, "_FA_A2_SKIPPED_ADVISORY", "Table A2 skipped, reworded")
    monkeypatch.setattr(cli, "_FOREIGN_TIN_MISSING_ADVISORY", "Foreign TIN blank, reworded")

    assert _run_all(tmp_path, tins=False, custodian=False) == 3

    err = capsys.readouterr().err
    assert "Before you file (2 item(s) to address):" in err
    for _action, remedy in cli._PRE_FILING_ACTION_ITEMS:
        assert remedy in err
