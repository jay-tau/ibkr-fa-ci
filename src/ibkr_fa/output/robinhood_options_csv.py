"""Robinhood option workpaper CSV (``robinhood-options.csv``).

Every row, header included, goes through
:func:`~ibkr_fa.output._writer_contract.write_checked_rows`. Amount cells
render through :func:`~ibkr_fa.output._writer_contract.csv_safe_signed`, so a
loss reads ``'-8499``. The pipeline decides the ITR-3 business filing and
passes it in.

After the ``capital-gain`` rows, each bucket with option capital gains gets
five ``table-f`` rows (#1362): Schedule CG Table F row 3 for
``stcg-other-assets`` or row 5 for ``ltcg-other-assets``, one per date band,
from its :attr:`~ibkr_fa.compute.robinhood_options.RobinhoodOptionsResult.table_f`
row. ``amount_inr`` is the band's whole rupees, or blank when compute leaves
the bucket's bands blank, and ``manual_target`` says why. The bands are the
option gains alone, before any set-off.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Final

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionBusinessFiling,
    RobinhoodOptionCapitalGainBucket,
    RobinhoodOptionsResult,
    option_classification_audit_notes,
)
from ibkr_fa.compute.schedule_cg_table_f import TABLE_F_BAND_COUNT, ScheduleCgTableFCase
from ibkr_fa.output._atomic import atomic_text_output
from ibkr_fa.output._writer_contract import csv_safe_signed, write_checked_rows
from ibkr_fa.output.portal_labels import for_ay
from ibkr_fa.output.schedules.cg_filing import (
    TABLE_F_COLUMNS,
    TABLE_F_LOSS_ACROSS_BANDS_BASIS,
)

OPTIONS_FILENAMES = ("robinhood-options.csv",)
# Why a bucket's Table F bands are blank (#1362).
_TABLE_F_BLANK_NOTE: Final[dict[ScheduleCgTableFCase, str]] = {
    ScheduleCgTableFCase.MANUAL_SECTION: (
        "Blank: these option gains round past the CBDT 14-digit bound, so no band is computed."
    ),
    ScheduleCgTableFCase.LOSS_ACROSS_BANDS: TABLE_F_LOSS_ACROSS_BANDS_BASIS,
}
_TABLE_F_COMPUTED_NOTE: Final[str] = (
    "Robinhood option gains closed in this band, net, before any set-off; the five "
    "bands add up to these option gains' exact total rounded half up, or are 0 when "
    "it isn't positive."
)


def _table_f_rows(
    result: RobinhoodOptionsResult,
    *,
    headings: tuple[str, ...],
    texts: Mapping[RobinhoodOptionCapitalGainBucket, tuple[str, str]],
) -> Iterator[list[object]]:
    """Five ``table-f`` rows per bucket with capital gains, (i) to (v) (#1362).

    ``texts`` gives each bucket's Table F row label and Schedule CG section,
    such as A5, for the run's ITR form.
    """
    for bucket, table_f in result.table_f.items():
        row_label, section_ref = texts[bucket]
        note = _TABLE_F_BLANK_NOTE.get(table_f.case, _TABLE_F_COMPUTED_NOTE)
        guidance = (
            "Add the same band's amounts from schedule-cg.xlsx's equity bands and every "
            "other capital-gains source, then lower the bands for any capital loss set "
            "off against them, so the row totals that Schedule BFLA item. If the "
            f"{section_ref} figures you enter for these options round to a different "
            "total, the row must still total Schedule BFLA."
        )
        bands = table_f.bands or (None,) * TABLE_F_BAND_COUNT
        for column, heading, band in zip(TABLE_F_COLUMNS, headings, bands, strict=True):
            yield [
                "table-f",
                "",
                bucket.value,
                heading,
                "",
                "",
                "",
                "" if band is None else csv_safe_signed(band),
                (
                    f"Schedule CG Table F row {bucket.table_f_row}({column}) {heading}: "
                    f"{row_label} {note} {guidance}"
                ),
            ]


def _iter_rows(
    result: RobinhoodOptionsResult,
    *,
    capital_targets: Mapping[RobinhoodOptionCapitalGainBucket, str],
    table_f_headings: tuple[str, ...],
    table_f_texts: Mapping[RobinhoodOptionCapitalGainBucket, tuple[str, str]],
    business_filing: RobinhoodOptionBusinessFiling | None,
    business_target: str,
) -> Iterator[list[object]]:
    yield [
        "record_type",
        "account_index",
        "treatment_or_kind",
        "activity_or_close_date",
        "quantity",
        "cost_or_cash_usd",
        "proceeds_or_pnl_usd",
        "amount_inr",
        "manual_target",
    ]
    for record_type, account, treatment, note in option_classification_audit_notes(result):
        yield [record_type, account, treatment, "", "", "", "", "", note]
    for item in result.capital_gain_lines:
        yield [
            "capital-gain",
            item.source_slice.contract.account_index,
            item.bucket.value,
            item.source_slice.close_date.isoformat(),
            csv_safe_signed(item.source_slice.quantity),
            csv_safe_signed(item.source_slice.cost_usd),
            csv_safe_signed(item.source_slice.net_proceeds_usd),
            csv_safe_signed(item.net_gain_inr),
            (
                f"{capital_targets[item.bucket]}; Schedule CFL (if a loss), "
                "Schedule FSI (CG), and Schedule FA Table D"
            ),
        ]
    yield from _table_f_rows(result, headings=table_f_headings, texts=table_f_texts)
    for item in result.business_summaries:
        yield [
            "business-summary",
            item.account_index,
            item.treatment.value,
            "",
            "",
            csv_safe_signed(item.matched_premium_paid_usd),
            csv_safe_signed(item.net_pnl_usd),
            csv_safe_signed(item.net_pnl_inr),
            business_target,
        ]
    if result.business_summaries:
        yield [
            "itr-3-note",
            "",
            "Sections 44AA/44AB",
            "",
            "",
            "",
            "",
            csv_safe_signed(business_filing.turnover_inr) if business_filing is not None else "",
            (
                "Robinhood only; aggregate with all other businesses. Turnover "
                "shown is ICAI 5.11(a) differences basis. Books under 44AA(2): "
                "for an individual/HUF, business income above Rs 2,50,000 or "
                "turnover above Rs 25,00,000 in any of the three preceding years, "
                "or the likely current-year figures for a newly set-up business. "
                "Tax audit under 44AB(a): current-year turnover above "
                "Rs 1,00,00,000, raised to Rs 10,00,00,000 only if cash receipts "
                "and cash payments are each 5% or less. This workpaper determines "
                "neither."
            ),
        ]
    if business_filing is not None:
        yield [
            "itr-3-audit",
            "",
            business_filing.turnover_basis.value,
            "",
            "",
            "",
            "",
            "",
            "ICAI Guidance Note Revised 2026 para 5.11(a)/(b)(i)",
        ]
        audit_fields = (
            ("turnover-full-precision", business_filing.turnover_inr_full_precision),
            ("gross-profit-full-precision", business_filing.gross_profit_inr_full_precision),
            ("expenditure-full-precision", business_filing.expenditure_inr_full_precision),
            ("net-income-full-precision", business_filing.net_income_inr_full_precision),
        )
        for field, amount in audit_fields:
            yield [
                "itr-3-audit",
                "",
                field,
                "",
                "",
                "",
                "",
                csv_safe_signed(amount),
                "pre-rounding INR reconciliation",
            ]
        yield [
            "itr-3-field",
            "",
            f"Nature of business {business_filing.nature_of_business_code}",
            "",
            "",
            "",
            "",
            "",
            "Futures and Options trading",
        ]
        yield [
            "itr-3-note",
            "",
            "BP A1",
            "",
            "",
            "",
            "",
            "",
            (
                "AY utility auto-populates profit before tax from P&L, including "
                "44ADA row 62ii and option row 65iv; do not add 65iv again"
            ),
        ]
        fields = (
            ("P&L 65i", business_filing.turnover_inr, "Turnover from speculative activity"),
            ("P&L 65ii", business_filing.gross_profit_inr, "Gross profit"),
            ("P&L 65iii", business_filing.expenditure_inr, "Expenditure"),
            ("P&L 65iv", business_filing.net_income_inr, "Net income"),
            ("BP 2a", business_filing.net_income_inr, "Speculative profit/loss in P&L"),
            ("BP B39", business_filing.net_income_inr, "Speculative profit/loss"),
            ("BP B40", business_filing.bp_additions_inr, "Additions under sections 28-44DA"),
            ("BP B41", business_filing.bp_deductions_inr, "Deductions under sections 28-44DA"),
            ("BP B42", business_filing.bp_income_inr, "Income from speculative business"),
            (
                "CFL current AY / column 6",
                business_filing.cfl_loss_inr,
                "Loss from speculative business",
            ),
        )
        for field, amount, label in fields:
            yield ["itr-3-field", "", field, "", "", "", "", csv_safe_signed(amount), label]
    for item in result.blockers:
        yield [
            "blocker",
            item.account_index,
            item.kind.value,
            "",
            "",
            "",
            "",
            "",
            "resolve source evidence",
        ]


def write_robinhood_options_csv(
    result: RobinhoodOptionsResult,
    output_dir: Path,
    *,
    assessment_year_start: int,
    itr_form: ItrForm,
    business_filing: RobinhoodOptionBusinessFiling | None,
    business_target: str,
) -> Path:
    """Write the option workpaper atomically and return the path it published:
    ``output_dir`` as given, joined with ``OPTIONS_FILENAMES[0]``, not resolved.

    ``business_filing`` is the computed ITR-3 P&L/BP/CFL filing, or ``None``
    when none is emitted. ``business_target`` names where the business
    summary rows go.
    """
    labels = for_ay(assessment_year_start)
    capital_targets = {
        RobinhoodOptionCapitalGainBucket.STCG_OTHER_ASSETS: (
            labels.PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM[itr_form]
        ),
        RobinhoodOptionCapitalGainBucket.LTCG_OTHER_ASSETS: (
            labels.PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG_BY_ITR_FORM[itr_form]
        ),
    }
    table_f_texts = {
        RobinhoodOptionCapitalGainBucket.STCG_OTHER_ASSETS: (
            labels.PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM[itr_form],
            labels.PORTAL_SECTION_REF_CG_FOREIGN_STCG_BY_ITR_FORM[itr_form],
        ),
        RobinhoodOptionCapitalGainBucket.LTCG_OTHER_ASSETS: (
            labels.PORTAL_ROW_LABEL_CG_TABLE_F_LTCG_BY_ITR_FORM[itr_form],
            labels.PORTAL_SECTION_REF_CG_FOREIGN_LTCG_BY_ITR_FORM[itr_form],
        ),
    }
    path = output_dir / OPTIONS_FILENAMES[0]
    with atomic_text_output(path, encoding="utf-8-sig", newline="") as handle:
        write_checked_rows(
            csv.writer(handle),
            _iter_rows(
                result,
                capital_targets=capital_targets,
                table_f_headings=labels.PORTAL_COLUMN_HEADERS_CG_TABLE_F,
                table_f_texts=table_f_texts,
                business_filing=business_filing,
                business_target=business_target,
            ),
            start_index=0,
        )
    return path


__all__ = ["OPTIONS_FILENAMES", "write_robinhood_options_csv"]
