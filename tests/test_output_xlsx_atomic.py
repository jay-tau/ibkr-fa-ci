"""The openpyxl XLSX writers save atomically and privately (#605).

The portal filing workbooks are reached through the output catalog, which
keeps their keys through the renderer cutovers (#1199).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from ibkr_fa.compute.form67 import Form67ComputeResult
from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from ibkr_fa.compute.schedule_cg_ltcg import ScheduleCgLtcgComputeResult
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.compute.schedule_os import ScheduleOsComputeResult
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult
from ibkr_fa.output import RenderContext, Subcommand, output_binding
from ibkr_fa.output.form67_xlsx import Form67XlsxWriter
from ibkr_fa.output.schedule_cg_ltcg_xlsx import ScheduleCgLtcgXlsxWriter
from ibkr_fa.output.schedule_cg_xlsx import ScheduleCgXlsxWriter
from ibkr_fa.output.schedule_fa_a2_xlsx import ScheduleFaA2XlsxWriter
from ibkr_fa.output.schedule_fsi_xlsx import ScheduleFsiXlsxWriter
from ibkr_fa.output.schedule_os_xlsx import ScheduleOsXlsxWriter
from ibkr_fa.output.schedule_tr_xlsx import ScheduleTrXlsxWriter
from ibkr_fa.output.xlsx import XlsxWriter
from tests._robinhood_cg import ACCOUNT_LABELS, cg_result

_WRITES: dict[str, Callable[[Path], None]] = {
    "form67": lambda path: Form67XlsxWriter().write(Form67ComputeResult(entries=()), path),
    "schedule-cg-ltcg": lambda path: ScheduleCgLtcgXlsxWriter().write(
        ScheduleCgLtcgComputeResult([], [], [], [], []), path
    ),
    "schedule-cg": lambda path: ScheduleCgXlsxWriter().write(
        ScheduleCgStcgComputeResult([], [], [], []), path
    ),
    "schedule-fa-a2": lambda path: ScheduleFaA2XlsxWriter().write([], path),
    "schedule-fsi": lambda path: ScheduleFsiXlsxWriter().write(
        ScheduleFsiComputeResult(rows=[]), path
    ),
    "schedule-os": lambda path: ScheduleOsXlsxWriter().write(
        ScheduleOsComputeResult([], [], interest_total_inr_rounded=Decimal("0")), path
    ),
    "schedule-tr": lambda path: ScheduleTrXlsxWriter().write(
        ScheduleTrComputeResult(rows=[]), path
    ),
    "fa-a3": lambda path: XlsxWriter().write([], path),
    "schedule-os-filing": lambda path: (
        output_binding(Subcommand.OS, "filing-xlsx")
        .writer(RenderContext(2026))
        .write(ScheduleOsComputeResult([], [], interest_total_inr_rounded=Decimal("0")), path)
    ),
    "schedule-cg-filing": lambda path: (
        output_binding(Subcommand.CG_FILING, "filing-xlsx")
        .writer(RenderContext(2026, "ITR-2", "Robinhood", ACCOUNT_LABELS))
        .write(cg_result().filing(()), path)
    ),
}
_EACH_WRITER = pytest.mark.parametrize("write", list(_WRITES.values()), ids=list(_WRITES))


@_EACH_WRITER
def test_failed_save_keeps_destination_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write: Callable[[Path], None]
) -> None:
    out = tmp_path / "out.xlsx"
    out.write_bytes(b"sentinel")

    def fail_mid_save(_workbook: Workbook, filename: Path) -> None:
        Path(filename).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(Workbook, "save", fail_mid_save)

    with pytest.raises(OSError, match="disk full"):
        write(out)

    assert out.read_bytes() == b"sentinel"
    assert sorted(tmp_path.glob(".out.xlsx.*.tmp")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")
@_EACH_WRITER
def test_output_is_owner_only(tmp_path: Path, write: Callable[[Path], None]) -> None:
    out = tmp_path / "out.xlsx"
    previous_umask = os.umask(0o022)
    try:
        write(out)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(out.stat().st_mode) == 0o600
