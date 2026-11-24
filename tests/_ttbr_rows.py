"""Synthetic SBI TTBR CSV rows, the uncached loader run on them and the row
refusal check, shared by the TTBR loader, TTBR refusal chain and TTBR number
limit tests.

- ``SBI_CSV_HEADER``: an SBI TTBR file's header line, written out in full.
- ``ttbr_row``: one raw ten-cell data line; keyword arguments set its date,
  ``TT BUY`` rate and PDF file cells.
- ``load_ttbr_text``: the uncached ``load_ttbr`` on a synthetic ``XXX`` file
  holding a text.
- ``load_ttbr_lines``: ``load_ttbr_text`` on lines, each ended by a newline.
- ``USD_TTBR_CSV``: the file name ``load_ttbr`` reads for USD.
- ``assert_ttbr_row_refusal``: an error is exactly ``load_ttbr``'s row refusal
  for a problem at a line, with no marker anywhere in its exception graph.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.fx import ttbr_loader
from ibkr_fa.invariants import invariant_error
from tests._crash_reports import assert_replaced_unchained

SBI_CSV_HEADER = (
    "DATE,PDF FILE,TT BUY,TT SELL,BILL BUY,BILL SELL,"
    "FOREX TRAVEL CARD BUY,FOREX TRAVEL CARD SELL,CN BUY,CN SELL"
)


def ttbr_row(day: str = "2025-12-30 09:00", rate: str = "89.47", pdf: str = "p.pdf") -> str:
    """One raw ten-cell data line; the cells are pasted in verbatim."""
    return f"{day},{pdf},{rate},90.32,89.40,90.50,88,91,88,91"


def load_ttbr_text(
    text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Mapping[date, Decimal]:
    """Run the uncached ``load_ttbr`` on a synthetic file holding ``text``."""
    fake_csv = tmp_path / "sbi_ttbr_xxx_inr.csv"
    fake_csv.write_bytes(text.encode("utf-8"))

    class _FakeTraversable:
        def joinpath(self, _name: str) -> Path:
            return fake_csv

    monkeypatch.setattr(ttbr_loader.resources, "files", lambda _pkg: _FakeTraversable())
    return ttbr_loader.load_ttbr.__wrapped__("XXX")


def load_ttbr_lines(
    lines: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Mapping[date, Decimal]:
    return load_ttbr_text("\n".join(lines) + "\n", tmp_path, monkeypatch)


USD_TTBR_CSV = "sbi_ttbr_usd_inr.csv"


def assert_ttbr_row_refusal(
    error: BaseException,
    problem: str,
    *,
    line: int,
    markers: tuple[str, ...],
    csv_name: str = "sbi_ttbr_xxx_inr.csv",
) -> None:
    """``error`` is exactly the row refusal for ``problem`` at ``line``. No
    marker is anywhere in its exception graph; it has no context, and its only
    cause is the stand-in naming the row error's class and a line."""
    assert type(error) is InvariantError
    assert error == invariant_error(what=f"{csv_name}: {problem}", line=line)
    assert str(error) == f"InvariantError: {csv_name}: {problem} (context keys: line={line})"
    assert_replaced_unchained(error, ValueError, markers=markers)
