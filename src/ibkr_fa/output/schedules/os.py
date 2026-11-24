"""Schedule OS filing rows and workbook sheets (#1202).

The filing CSV and the filing workbook share the ITR rows: section 1's fields
and section 10's dividend periods, as text. The CSV adds Quicko's two dividend
screens as vertical blocks, one per security. The workbook converts the ITR
rows to cells and adds Quicko sheets by account, when there are dividend lines
and every one names its account, and by security. Only the selected artifact
is built, so the CSV never asks for the account split. Every amount is read from the
compute result; nothing here rounds, allocates or adds money.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import Final

from ibkr_fa.compute.schedule_os import (
    ScheduleOsAccountSplit,
    ScheduleOsComputeResult,
    ScheduleOsSecuritySplit,
)
from ibkr_fa.invariants import require
from ibkr_fa.output.base import RenderContext
from ibkr_fa.output.filing_xlsx import FormSheet, TableSheet
from ibkr_fa.output.portal_labels import for_ay
from ibkr_fa.reference.country_codes import lookup_country

_COLUMN_COUNT: Final[int] = 7
_QUICKO_ALTERNATIVE_NOTE: Final[str] = (
    "Alternative views of the same dividend total: enter either this sheet "
    "or the other Quicko sheet, never both."
)
_QUICKO_SINGLE_VIEW_NOTE: Final[str] = (
    "Quicko dividend entries grouped by security; amounts reconcile to the ITR Schedule OS sheet."
)


def _quicko_period_labels(financial_year_start: int) -> tuple[str, ...]:
    financial_year_end = financial_year_start + 1
    return (
        f"Period 1: 01/04/{financial_year_start} to 15/06/{financial_year_start}",
        f"Period 2: 16/06/{financial_year_start} to 15/09/{financial_year_start}",
        f"Period 3: 16/09/{financial_year_start} to 15/12/{financial_year_start}",
        f"Period 4: 16/12/{financial_year_start} to 15/03/{financial_year_end}",
        f"Period 5: 16/03/{financial_year_end} to 31/03/{financial_year_end}",
    )


def _require_interest_total(result: ScheduleOsComputeResult) -> None:
    require(
        result.interest_total_inr_rounded is not None,
        what="Schedule OS filing requires the separately computed interest total",
    )


def _itr_rows(
    result: ScheduleOsComputeResult,
    *,
    labels: ModuleType,
) -> Iterator[list[str]]:
    """Yield the compact ITR Schedule OS filing rows."""
    yield _field_row(
        labels.PORTAL_SECTION_LABEL_OS_SECTION_1,
        "IMPORTANT: ADD every ITR amount in this file to existing Schedule OS values "
        "from Indian income, other accounts, or other sources; do not replace totals.",
    )
    yield _field_row("Field", "Amount to ADD")
    yield _field_row(
        labels.PORTAL_FIELD_LABEL_OS_SECTION_1_ORDINARY_DIVIDEND,
        str(result.ordinary_dividend_total_inr_rounded),
    )
    # 1b(ix) is a SEPARATE Schedule OS cell from 1a(i), so it is emitted as its
    # own field row rather than folded into the dividend total. Emitting it here
    # is what keeps this file self-sufficient: Schedule FSI reports the combined
    # foreign income, so a worksheet carrying only the dividend would leave the
    # two CSVs irreconcilable and the interest silently unfiled.
    yield _field_row(
        labels.PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST,
        str(result.interest_total_inr_rounded),
    )
    for nature, _exact, other_whole_rupees in result.other_income_totals:
        yield _field_row(
            f"{labels.PORTAL_FIELD_LABEL_OS_SECTION_1_ANY_OTHER_INCOME} / Nature: {nature}",
            str(other_whole_rupees),
        )
    yield [""] * _COLUMN_COUNT
    yield _field_row(labels.PORTAL_SECTION_LABEL_OS_SECTION_10, "")
    yield _field_row(
        "IMPORTANT: ADD these gross period amounts to any existing/prefilled "
        "Schedule OS values, then subtract any row 2f DTAA dividend and section "
        "57(1) interest expenditure; neither subtraction is computed here.",
        "",
    )
    yield list(labels.PORTAL_COLUMN_HEADERS_OS_SECTION_10)
    yield [
        "3a.",
        labels.PORTAL_ROW_LABEL_OS_SECTION_10_ORDINARY_DIVIDEND,
        *(str(period.whole_rupees) for period in result.dividend_periods),
    ]


def _field_row(label: str, value: str) -> list[str]:
    """A ``label, value`` row padded to the file's column count."""
    return [label, value, *([""] * (_COLUMN_COUNT - 2))]


def _quicko_description(
    split: ScheduleOsSecuritySplit,
    *,
    source_label: str | None,
    include_isin: bool,
) -> str:
    """Describe one Quicko foreign-dividend entry by ticker and where it came from.

    Quicko's Description is free text, so it names the holding and the broker
    account the rows were parsed from — that is what makes an entry
    recognisable months later, rather than the ticker alone.
    """
    holding = f"{split.symbol} ({split.isin})" if include_isin else str(split.symbol)
    return f"{holding} - {source_label}" if source_label else holding


def _quicko_rows(
    result: ScheduleOsComputeResult,
    *,
    financial_year_start: int,
    source_label: str | None,
) -> Iterator[list[str]]:
    """Yield Quicko's two dividend screens, kept apart to avoid double entry.

    Quicko splits "Add Dividend Income" from "Add Foreign Dividend Income";
    only the latter carries a country and therefore feeds Schedule FSI. A
    foreign dividend entered on the domestic screen would land in Schedule OS
    with no FSI/TR row and no foreign tax credit, so each line appears under
    exactly one heading.

    Both blocks are laid out vertically, one field per row, in the order the
    Quicko form asks for them.
    """
    entries = _quicko_entries(result, source_label=source_label)
    previous_heading: str | None = None
    for heading, country, description, split in entries:
        if previous_heading is not None:
            yield [""] * _COLUMN_COUNT
        if heading != previous_heading:
            yield _field_row(heading, "")
        if country:
            yield _field_row("Country", country)
            yield _field_row("Description", description)
        for label, amount in zip(
            _quicko_period_labels(financial_year_start),
            split.period_whole_rupees,
            strict=True,
        ):
            yield _field_row(label, str(amount))
        if not country:
            yield _field_row("Total", str(split.total_whole_rupees))
            yield _field_row("Expense (per period)", "not computed")
        previous_heading = heading


def _quicko_entries(
    result: ScheduleOsComputeResult,
    *,
    source_label: str | None,
) -> tuple[tuple[str, str, str, ScheduleOsSecuritySplit], ...]:
    """Return one reconciled Quicko row per security."""
    splits = result.dividend_splits_by_security
    symbol_counts = Counter((split.issuer_country_iso2, split.symbol) for split in splits)
    return tuple(
        (
            "Quicko / Add Foreign Dividend Income",
            lookup_country(split.issuer_country_iso2).country_name,
            _quicko_description(
                split,
                source_label=source_label,
                include_isin=symbol_counts[(split.issuer_country_iso2, split.symbol)] > 1,
            ),
            split,
        )
        if split.issuer_country_iso2 != "IN"
        else ("Quicko / Add Dividend Income (Indian-source only)", "", "", split)
        for split in splits
    )


def _quicko_account_entries(
    result: ScheduleOsComputeResult,
    *,
    source_label: str | None,
    account_labels: dict[int, str],
) -> tuple[tuple[str, str, str, str, ScheduleOsAccountSplit], ...]:
    """Return reconciled Quicko rows grouped by account and country."""
    splits = result.dividend_splits_by_account
    label_counts = Counter(
        account_labels.get(account_index)
        for account_index in {split.source_account_index for split in splits}
    )
    entries = []
    for split in splits:
        account_index = split.source_account_index
        iso = split.issuer_country_iso2
        foreign = iso != "IN"
        account_label = account_labels.get(account_index) or None
        duplicate_label = account_label is not None and label_counts[account_label] > 1
        label_suffix = f" (index {account_index})" if duplicate_label else ""
        display_label = (
            f"{account_label}{label_suffix}"
            if account_label is not None
            else f"Account index {account_index}"
        )
        entries.append(
            (
                display_label,
                (
                    "Quicko / Add Foreign Dividend Income"
                    if foreign
                    else "Quicko / Add Dividend Income (Indian-source only)"
                ),
                lookup_country(iso).country_name if foreign else "",
                (
                    f"{source_label + ' ' if source_label else ''}{account_label} account"
                    f"{label_suffix}"
                    if account_label is not None
                    else f"{source_label or 'Source'} account index {account_index}"
                ),
                split,
            )
        )
    return tuple(entries)


def _quicko_cells(
    screen: str,
    country: str,
    description: str,
    split: ScheduleOsSecuritySplit | ScheduleOsAccountSplit,
) -> list[object]:
    """One Quicko entry's workbook cells, from the screen to the expense instruction."""
    return [
        screen.removeprefix("Quicko / Add "),
        country or None,
        description or None,
        *split.period_whole_rupees,
        split.total_whole_rupees,
        None if country else "not computed",
    ]


def _csv_rows(
    result: ScheduleOsComputeResult,
    *,
    labels: ModuleType,
    financial_year_start: int,
    source_label: str | None,
) -> Iterator[list[str]]:
    yield from _itr_rows(result, labels=labels)
    yield [""] * _COLUMN_COUNT
    yield from _quicko_rows(
        result,
        financial_year_start=financial_year_start,
        source_label=source_label,
    )


def _workbook_sheets(
    result: ScheduleOsComputeResult,
    *,
    labels: ModuleType,
    financial_year_start: int,
    source_label: str | None,
    account_labels: dict[int, str],
) -> Iterator[FormSheet | TableSheet]:
    yield FormSheet(
        "ITR Schedule OS",
        _itr_rows(result, labels=labels),
        widths=(62, 28, 18, 18, 18, 18, 18),
        merge="B1:G1",
        heading_prefix="Schedule OS /",
        column_headers=frozenset({"Field", "Sl No."}),
        note_prefix="IMPORTANT:",
    )
    header = [
        "Quicko screen",
        "Country",
        "Description",
        *_quicko_period_labels(financial_year_start),
        "Total",
        "Expense (per period)",
    ]
    security_rows = [
        _quicko_cells(*entry) for entry in _quicko_entries(result, source_label=source_label)
    ]
    account_entries = _quicko_account_entries(
        result,
        source_label=source_label,
        account_labels=account_labels,
    )
    if account_entries:
        yield TableSheet(
            "Quicko by Account",
            _QUICKO_ALTERNATIVE_NOTE,
            header=["Account", *header],
            rows=[
                [account, *_quicko_cells(screen, country, description, split)]
                for account, screen, country, description, split in account_entries
            ],
            widths=(20, 24, 28, 36, 20, 20, 20, 20, 20, 14, 20),
        )
    yield TableSheet(
        "Quicko by Security",
        _QUICKO_ALTERNATIVE_NOTE if account_entries else _QUICKO_SINGLE_VIEW_NOTE,
        header=header,
        rows=security_rows,
        widths=(24, 28, 42, 20, 20, 20, 20, 20, 14, 20),
    )


def filing_csv_rows(
    ctx: RenderContext,
) -> Callable[[ScheduleOsComputeResult], Iterator[list[str]]]:
    """The run's filing CSV rows; resolves ``ctx``'s labels now.

    The returned function checks the interest total when called, before the
    file is staged, and returns the rows lazily: the ITR rows, a blank row,
    then the Quicko blocks.
    """
    labels = for_ay(ctx.assessment_year_start)

    def rows(result: ScheduleOsComputeResult) -> Iterator[list[str]]:
        _require_interest_total(result)
        return _csv_rows(
            result,
            labels=labels,
            financial_year_start=ctx.assessment_year_start - 1,
            source_label=ctx.source_label,
        )

    return rows


def filing_workbook_sheets(
    ctx: RenderContext,
) -> Callable[[ScheduleOsComputeResult], Iterator[FormSheet | TableSheet]]:
    """The run's filing workbook sheets; resolves ``ctx``'s labels now.

    The returned function checks the interest total when called, before the
    workbook is created, and returns the sheets lazily: the ITR sheet, whose
    rows are read as it is built, then the Quicko sheets.
    """
    labels = for_ay(ctx.assessment_year_start)
    account_labels = dict(ctx.account_labels or {})

    def sheets(result: ScheduleOsComputeResult) -> Iterator[FormSheet | TableSheet]:
        _require_interest_total(result)
        return _workbook_sheets(
            result,
            labels=labels,
            financial_year_start=ctx.assessment_year_start - 1,
            source_label=ctx.source_label,
            account_labels=account_labels,
        )

    return sheets


__all__ = ["filing_csv_rows", "filing_workbook_sheets"]
