"""Hand-built Robinhood Schedule CG results and the Schedule CG filing workbook
writer and reader, shared by the filing workbook tests, the filed total tests
and the Robinhood pipeline tests.

``robinhood_stcg_result`` is a Robinhood capital gains result with one
hand-built short-term line and its attributed lot, which ``tests._robinhood_cg``'s
``sale`` builds as sold by event 1, and ``robinhood_ltcg_result`` swaps them
for one long-term line and lot with the given USD cost and proceeds.
``write_filing_workbook`` writes a Schedule CG filing result through the output
catalog's ``filing-xlsx`` writer, as the pipelines do, and
``write_result_workbook`` builds a result's filing beside the given option
lines and writes it the same way. ``values_by_field`` reads a written sheet
back, from row 6 down, as each row's field (column A) mapped to its value
(column C), and fails when a field repeats.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.robinhood_capital_gains import (
    RobinhoodCapitalGainLot,
    RobinhoodCapitalGainsResult,
)
from ibkr_fa.compute.robinhood_options import RobinhoodOptionCapitalGainLine
from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from ibkr_fa.compute.schedule_cg_filing import ScheduleCgFilingResult
from ibkr_fa.compute.schedule_cg_ltcg import ScheduleCgLtcgComputeResult
from ibkr_fa.models.schedule_cg_ltcg_row import ScheduleCgLtcgLine
from ibkr_fa.models.schedule_cg_row import ScheduleCgStcgLine
from ibkr_fa.output import RenderContext, Subcommand, output_binding
from ibkr_fa.types import Isin, Ticker
from tests._robinhood_cg import ACCOUNT_LABELS, sale

_OPEN = date(2025, 5, 1)
_CLOSE = date(2025, 6, 10)


def robinhood_stcg_result() -> RobinhoodCapitalGainsResult:
    line = ScheduleCgStcgLine(
        symbol=Ticker("XQCA"),
        isin=Isin("US99972B1017"),
        issuer_country_iso2="US",
        description="SAMPLE ISSUER CA",
        open_date=_OPEN,
        close_date=_CLOSE,
        holding_period_days=(_CLOSE - _OPEN).days,
        quantity=Decimal("1"),
        cost_basis_usd=Decimal("12"),
        proceeds_usd=Decimal("10"),
        fx_specified_date=date(2025, 5, 31),
        fx_rate_publication_date=date(2025, 5, 31),
        fx_rate_inr_per_usd=Decimal("100"),
        cost_basis_inr=Decimal("1200"),
        cost_basis_inr_source="Rule 115 transfer TTBR",
        proceeds_inr=Decimal("1000"),
        realized_gain_inr=Decimal("-200"),
    )
    return RobinhoodCapitalGainsResult(
        stcg=ScheduleCgStcgComputeResult([line], [], [], []),
        ltcg=ScheduleCgLtcgComputeResult([], [], [], [], []),
        attributed_lots=(sale("12", "10", sale_ordinal=1),),
        blockers=(),
        excluded_out_of_fy_disposal_references=(),
        sale_price_checks=(),
    )


def robinhood_ltcg_result(*, cost: Decimal, proceeds: Decimal) -> RobinhoodCapitalGainsResult:
    result = robinhood_stcg_result()
    open_date = date(2023, 1, 1)
    line = ScheduleCgLtcgLine(
        symbol=Ticker("XQCA"),
        isin=Isin("US99972B1017"),
        issuer_country_iso2="US",
        description="SAMPLE ISSUER CA",
        open_date=open_date,
        close_date=_CLOSE,
        holding_period_days=(_CLOSE - open_date).days,
        quantity=Decimal("1"),
        cost_basis_usd=cost,
        proceeds_usd=proceeds,
        fx_specified_date=date(2025, 5, 31),
        fx_rate_publication_date=date(2025, 5, 31),
        fx_rate_inr_per_usd=Decimal("100"),
        cost_basis_inr=cost * 100,
        cost_basis_inr_source="Rule 115 transfer TTBR",
        proceeds_inr=proceeds * 100,
        realized_gain_inr=(proceeds - cost) * 100,
    )
    item = result.attributed_lots[0]
    closed_lot = replace(
        item.closed_lot,
        cost_basis_money=cost,
        proceeds_money=proceeds,
        gross_proceeds_money=proceeds,
        open_date_time=open_date,
    )
    source_slice = replace(
        item.source_slice,
        cost_usd=cost,
        original_date=open_date,
        entry_date=open_date,
    )
    return replace(
        result,
        stcg=replace(result.stcg, stcg_lines=[]),
        ltcg=replace(result.ltcg, ltcg_lines=[line]),
        attributed_lots=(RobinhoodCapitalGainLot(source_slice, closed_lot),),
    )


def write_filing_workbook(
    filing: ScheduleCgFilingResult,
    path: Path,
    *,
    assessment_year_start: int = 2026,
    itr_form: ItrForm,
    source_label: str = "Robinhood",
    account_labels: dict[int, str] = ACCOUNT_LABELS,
    include_checks: bool = False,
) -> None:
    """Write ``filing`` through the catalog, as the pipelines do."""
    ctx = RenderContext(
        assessment_year_start, itr_form, source_label, account_labels, debug=include_checks
    )
    output_binding(Subcommand.CG_FILING, "filing-xlsx").writer(ctx).write(filing, path)


def write_result_workbook(
    result: RobinhoodCapitalGainsResult,
    path: Path,
    *,
    assessment_year_start: int,
    itr_form: ItrForm,
    account_labels: dict[int, str],
    include_checks: bool = False,
    options: tuple[RobinhoodOptionCapitalGainLine, ...] = (),
) -> None:
    """Write ``result``'s filing workbook as the Robinhood pipeline does, beside
    the run's option capital-gain lines ``options``."""
    filing = result.filing(options)
    assert filing is not None
    write_filing_workbook(
        filing,
        path,
        assessment_year_start=assessment_year_start,
        itr_form=itr_form,
        account_labels=account_labels,
        include_checks=include_checks,
    )


def values_by_field(path: Path, sheet_name: str) -> dict[str, int | None]:
    workbook = load_workbook(path, data_only=True)
    worksheet = workbook[sheet_name]
    rows = [
        (str(worksheet.cell(row, 1).value), worksheet.cell(row, 3).value)
        for row in range(6, worksheet.max_row + 1)
    ]
    workbook.close()
    values = dict(rows)
    assert len(values) == len(rows), f"{sheet_name} repeats a field"
    return values
