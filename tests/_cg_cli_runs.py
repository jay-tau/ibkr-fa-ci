"""The Schedule CG command tests' lot markers, exact-line lots and launcher,
shared by the STCG, LTCG, FSI and exact IBKR command tests (#1760)."""

from __future__ import annotations

import csv
import pkgutil
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from openpyxl import load_workbook

from ibkr_fa.cli import main
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.models import BasisProvenance, ClosedLot
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._crash_reports import assert_no_markers, render_without_frames
from tests._exact_refusals import INEXACT_SUM
from tests._fiscal_cli import run_args
from tests._fx_stubs import AuditedFxStub

# #1184's privacy markers: a symbol, a description, a private-use ISIN and free
# text in the exchange and sub-category fields. Every synthetic lot carries all
# of them; no refusal or captured output may print one.
LOT_MARKERS: Final = ("ZQXA", "SYNTHCGDESC", "US99984C1012", "SYNTHCGID", "SYNTHCGREASON")
SHORT_TERM_OPEN: Final = date(2025, 5, 1)
LONG_TERM_OPEN: Final = date(2023, 4, 1)
# 29 significant digits, just below half: ₹1 exactly, ₹2 once rounded to 28.
BELOW_HALF: Final = "1.4999999999999999999999999999"
# Its exact gain over a zero cost (exponent 0) needs 1002 digits.
PAST_THE_CAP: Final = "1E+1001"
GAIN_PAST_THE_CAP: Final = (
    "InvariantError: Schedule CG {schedule}, input lot 1, column realized_gain_inr: " + INEXACT_SUM
)


def run_cg(command: str, statement: Path, config: Path, output: Path, *extra_args: str) -> int:
    """Run ``command --itr-form ITR-2`` through :func:`main` on these paths for
    FY 2025-26, then ``extra_args``."""
    return main(
        run_args((command, "--itr-form", "ITR-2"), (statement, config, output), *extra_args)
    )


def marked_lot(
    proceeds: str,
    *,
    cost: str = "0",
    opened: date = SHORT_TERM_OPEN,
    closed: date = date(2025, 12, 10),
) -> ClosedLot:
    """A one-share USD lot carrying every :data:`LOT_MARKERS`, sold in FY
    2025-26 and short-term unless ``opened`` or ``closed`` say otherwise."""
    symbol, description, isin, exchange, sub_category = LOT_MARKERS
    return ClosedLot(
        currency_primary="USD",
        sub_category=sub_category,
        symbol=Ticker(symbol),
        description=description,
        isin=Isin(isin),
        listing_exchange=exchange,
        issuer_country_code=CountryCode("US"),
        quantity=Decimal("1"),
        cost_basis_money=Decimal(cost),
        proceeds_money=Decimal(proceeds),
        open_date_time=opened,
        close_date_time=closed,
        basis_provenance=BasisProvenance.ENGINE_FIFO,
    )


def compute_on(
    monkeypatch: pytest.MonkeyPatch, lots: Sequence[ClosedLot], *targets: str, rate: str
) -> list[IbkrFaError]:
    """Run each real CG compute at ``targets`` on ``lots`` at a flat ``rate``.

    The pipeline's other arguments pass through, so the refusals come from the
    real compute. Returns the list each refusal is appended to as it leaves.
    """
    raised: list[IbkrFaError] = []

    def on_lots(real: Callable[..., object]) -> Callable[..., object]:
        def compute(_closed_lots: object, **kwargs: Any) -> object:
            try:
                return real(lots, **{**kwargs, "fx_policy": AuditedFxStub(Decimal(rate))})
            except IbkrFaError as error:
                raised.append(error)
                raise

        return compute

    for target in targets:
        monkeypatch.setattr(target, on_lots(pkgutil.resolve_name(target)))
    return raised


def assert_private_refusal(
    capsys: pytest.CaptureFixture[str], raised: list[IbkrFaError], message: str, amount: str
) -> None:
    """Stderr is exactly ``message``, raised with no cause or context, and no
    :data:`LOT_MARKERS` or input ``amount`` is printed or rendered."""
    captured = capsys.readouterr()
    assert captured.err == f"error: {message}\n"
    assert captured.out == ""
    (error,) = raised
    assert str(error) == message
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = render_without_frames(error)
    assert_no_markers(captured.out + captured.err + rendered, *LOT_MARKERS, amount)


def filed_cell(output: Path) -> str:
    """The filed Schedule CG total: the review CSV's or workbook's last cell."""
    if output.suffix == ".xlsx":
        worksheet = load_workbook(output).active
        assert worksheet is not None
        return str(list(worksheet.iter_rows(values_only=True))[-1][-1])
    with output.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))[-1][-1]
