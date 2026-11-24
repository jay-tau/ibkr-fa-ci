"""Form 67 column 6 through the real CLI when a payment is above the treaty rate (#1448).

Column 6 snaps the country's source-currency rate to the DTAA rate when its
tax is within half a cent per withholding row of that rate times its income.
Since #1448 it doesn't when the per-payment treaty-rate test (#890) found a
payment above the rate: that payment's excess isn't cent rounding. The
over-treaty advisory names that payment's rate, and since #1467 it reads above
the cap even when the rate is less than a hundredth of a percent above it.

Every statement is the synthetic FY 2025-26 withholding fixture with its one
XQBA dividend replaced by each case's payments, and the config is the
fixture's plus a US TIN and a US ``DIVIDEND`` tax payable override. Every
payment converts at the 2025-08-31 TTBR of ₹87.70, under Rule 115 and Rule
128 alike.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from tests._ftc_warnings import PORTAL_RELIEF_REFUSAL as _PORTAL_RELIEF_REFUSAL
from tests._ftc_warnings import fractional_rate_warning
from tests._ibkr_form67_runs import F67_RATE as _F67_RATE
from tests._ibkr_form67_runs import FSI_TAX as _FSI_TAX
from tests._ibkr_form67_runs import TR_RELIEF as _TR_RELIEF
from tests._ibkr_form67_runs import fsi_other_sources as _fsi_other_sources
from tests._ibkr_form67_runs import one_row as _one_row
from tests._ibkr_form67_runs import run_command as _run
from tests._ibkr_run_all import FY_2025_26_INPUT as _FY_2025_26_INPUT
from tests._ibkr_user_toml import US_FOREIGN_TIN_TOML as _TIN
from tests._ibkr_user_toml import US_PERSON_USER_TOML as _USER_TOML
from tests._text_edits import replace_once as _replace_once

_SECURITY: Final = "XQBA(US99995C1027) Cash Dividend USD"
_OVERRIDE: Final = """
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "{}"
"""
# (date, dividend, tax) for each payment on the fixture's 10 shares.
_EXAMPLE: Final = (("12-09-2025", "100.00", "-25.01"), ("26-09-2025", "100.00", "-25.00"))
_FRACTIONAL_WARNING: Final = fractional_rate_warning("25.01", "2")
_OVER_TREATY: Final = (
    "     - UNITED STATES OF AMERICA: highest per-payment source-currency withholding "
    "25.01% exceeds the DTAA cap 25.00%;"
)
_OVER_TREATY_BELOW_A_HUNDREDTH: Final = (
    "     - UNITED STATES OF AMERICA: highest per-payment source-currency withholding "
    "25.004% exceeds the DTAA cap 25.00%;"
)


def _inputs(
    tmp_path: Path, payments: tuple[tuple[str, str, str], ...], payable: str
) -> tuple[Path, Path]:
    """The fixture with its dividend and tax replaced by ``payments``, and both
    Total rows their sums; the config with a US TIN and ``payable``."""
    text = _FY_2025_26_INPUT.read_text(encoding="utf-8")
    dividends = "".join(
        f"Dividends,Data,USD,{day},{_SECURITY} {Decimal(dividend) / 10} per Share "
        f"(Ordinary Dividend),{dividend},,,,,,,,,,,\n"
        for day, dividend, _ in payments
    )
    taxes = "".join(
        f"Withholding Tax,Data,USD,{day},{_SECURITY} {Decimal(dividend) / 10} per Share "
        f"- US Tax,{tax},,,,,,,,,,,\n"
        for day, dividend, tax in payments
    )
    dividend_total = sum(Decimal(dividend) for _, dividend, _ in payments)
    tax_total = sum(Decimal(tax) for _, _, tax in payments)
    text = _replace_once(
        text,
        f"Dividends,Data,USD,12-09-2025,{_SECURITY} 0.26 per Share (Ordinary Dividend),2.60,"
        ",,,,,,,,,,\nDividends,Data,Total,,,2.60,",
        f"{dividends}Dividends,Data,Total,,,{dividend_total},",
    )
    text = _replace_once(
        text,
        f"Withholding Tax,Data,USD,12-09-2025,{_SECURITY} 0.26 per Share - US Tax,-0.39,"
        ",,,,,,,,,,\nWithholding Tax,Data,Total,,,-0.39,",
        f"{taxes}Withholding Tax,Data,Total,,,{tax_total},",
    )
    statement = tmp_path / "statement.csv"
    statement.write_text(text, encoding="utf-8")
    config = tmp_path / "user.toml"
    config.write_text(_USER_TOML + _TIN + _OVERRIDE.format(payable), encoding="utf-8")
    return statement, config


def _portal_row(path: Path) -> str:
    """The upload CSV's one data row."""
    _header, row = path.read_text(encoding="utf-8").splitlines()
    return row


def test_above_rate_payment_files_the_country_rate_in_column_6(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1448's example: USD 100.00 dividends on 12 and 26 September 2025 with
    USD 25.01 and 25.00 withheld, and a ₹4,000 override. The country is
    25.005%, within two half-cents of 25%, but the first payment is 25.01%,
    above its own half-cent. Column 6 files 25.01, stderr asks for it by hand,
    and the upload exits 3 as manual work (#1447); main filed 25.00 and exited
    0. Every other column, Schedule FSI and TR are as on main."""
    statement, config = _inputs(tmp_path, _EXAMPLE, "4000")
    upload = tmp_path / "form-67.csv"

    exit_code = _run("form-67", statement, config, upload, "portal-csv")

    err = capsys.readouterr().err.splitlines()
    assert _portal_row(upload) == "1,2,,7,,17540,4386,25.01,4000,,10,25.00,4000,,4000"
    assert exit_code == 3
    assert _FRACTIONAL_WARNING in err
    assert any(line.startswith(_OVER_TREATY) for line in err)

    review = tmp_path / "form-67-review.csv"
    assert _run("form-67", statement, config, review, "review-csv") == 0
    assert _one_row(review)[_F67_RATE] == "25.01"
    fsi, tr = tmp_path / "schedule-fsi.csv", tmp_path / "schedule-tr.csv"
    assert _run("schedule-fsi", statement, config, fsi) == 0
    assert _run("schedule-tr", statement, config, tr) == 0
    assert _fsi_other_sources(fsi)[_FSI_TAX] == "4386"
    assert _one_row(tr)[_TR_RELIEF] == "4000"


def test_above_rate_payment_keeps_the_portal_relief_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1448 with a ₹99,999,999 override: column 6 files 25.01, and Schedule
    FSI and TR still refuse at the portal-relief check, because the capped
    credit ₹4,385 is below the ₹4,386 tax paid (#900)."""
    statement, config = _inputs(tmp_path, _EXAMPLE, "99999999")
    review = tmp_path / "form-67-review.csv"

    assert _run("form-67", statement, config, review, "review-csv") == 0
    assert _one_row(review)[_F67_RATE] == "25.01"
    capsys.readouterr()
    for command in ("schedule-fsi", "schedule-tr"):
        out = tmp_path / f"{command}.csv"
        assert _run(command, statement, config, out) == 4, command
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", f"error: {_PORTAL_RELIEF_REFUSAL}\n"), command
        assert not out.exists()


def test_over_treaty_advisory_reads_above_the_cap_below_a_hundredth(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1467's example: one USD 1,000.00 dividend with USD 250.04 withheld is
    25.004%, above its half cent. Main's over-treaty line printed 25.00%, which
    doesn't exceed the 25.00% cap it names; it now prints 25.004%. Column 6
    still files 25.00."""
    statement, config = _inputs(tmp_path, (("12-09-2025", "1000.00", "-250.04"),), "4000")
    review = tmp_path / "form-67-review.csv"

    assert _run("form-67", statement, config, review, "review-csv") == 0

    err = capsys.readouterr().err.splitlines()
    assert any(line.startswith(_OVER_TREATY_BELOW_A_HUNDREDTH) for line in err)
    assert _one_row(review)[_F67_RATE] == "25.00"


@pytest.mark.parametrize(
    ("payments", "row", "exit_code"),
    [
        pytest.param(
            _EXAMPLE[:1],
            "1,2,,7,,8770,2193,25.01,4000,,10,25.00,2193,,2193",
            3,
            id="one-payment",
        ),
        pytest.param(
            (("12-09-2025", "100.00", "-25.00"), ("26-09-2025", "100.00", "-25.00")),
            "1,2,,7,,17540,4385,25.00,4000,,10,25.00,4000,,4000",
            0,
            id="both-at-the-rate",
        ),
        # 25.0005% rounds to the treaty rate rather than snapping to it.
        pytest.param(
            (("12-09-2025", "1000.00", "-250.01"), ("26-09-2025", "1000.00", "-250.00")),
            "1,2,,7,,175400,43851,25.00,4000,,10,25.00,4000,,4000",
            0,
            id="rounds-to-the-rate",
        ),
    ],
)
def test_column_6_controls_file_as_on_main(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payments: tuple[tuple[str, str, str], ...],
    row: str,
    exit_code: int,
) -> None:
    """#1448 controls, each filed and exiting as on main: one payment 1 cent
    above 25% (no snap with one row's tolerance; its fractional rate is
    manual work, #1447), both payments at exactly 25%, and a USD 1,000.00
    pair at 25.0005% whose unsnapped rate still files 25.00."""
    statement, config = _inputs(tmp_path, payments, "4000")
    upload = tmp_path / "form-67.csv"

    assert _run("form-67", statement, config, upload, "portal-csv") == exit_code

    capsys.readouterr()
    assert _portal_row(upload) == row
