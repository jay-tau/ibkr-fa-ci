"""Schedule CG filing workbook sheets for one source (#642, #1205).

The workbook holds one manual-entry sheet per section with disposals, STCG
before LTCG, or one summary sheet when neither has any. Compute decides every
amount and blocker in :class:`ScheduleCgFilingResult`; each amount cell reads
its section's :class:`ScheduleCgFilingSection` field (#1078), and each
section's Table F date bands, five rows after its result, read its
:class:`~ibkr_fa.compute.schedule_cg_table_f.ScheduleCgTableFRow` (#1360).
Nothing here calculates or rounds money. The source's name comes from the
run's ``source_label``, and its evidence wording and filing position from
``result.basis``. A section holding disposals the workbook doesn't cover is
never said to need no entry; its notes say where to enter them instead
(#1322).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from decimal import Decimal
from types import ModuleType
from typing import Final

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.schedule_cg_filing import (
    ScheduleCgFilingBasis,
    ScheduleCgFilingResult,
    ScheduleCgFilingSection,
    require_unblocked_schedule_cg_filing,
)
from ibkr_fa.compute.schedule_cg_table_f import (
    TABLE_F_BAND_COUNT,
    ScheduleCgTableFCase,
    ScheduleCgTableFRow,
)
from ibkr_fa.invariants import invariant_error
from ibkr_fa.output.base import RenderContext
from ibkr_fa.output.filing_xlsx import EntrySheet
from ibkr_fa.output.portal_labels import for_ay

_FOOTNOTE: Final[str] = (
    "Manual-entry workpaper only. The Income Tax portal imports Schedule CG "
    "only as part of a complete ITR JSON, not as a standalone XLSX or CSV."
)
_WIDTHS: Final[tuple[int, ...]] = (18, 62, 20, 22, 62)
#: The Table F band columns, (i) to (v).
TABLE_F_COLUMNS: Final[tuple[str, ...]] = ("i", "ii", "iii", "iv", "v")
#: Why a Table F row is left blank by rule (e), a loss across bands (#1360).
TABLE_F_LOSS_ACROSS_BANDS_BASIS: Final[str] = (
    "Blank: a date band nets a loss and two or more bands net gains. Choose how "
    "the loss lowers them: no band below 0, and the five add up to this "
    "section's result."
)
#: The reason that starts a blank Table F row's Basis, by why it is blank
#: (#1360, #1379); the review CSV and XLSX files show the same text (#1361).
#: No Schedule CG section is entered by hand any more (#1383), so a
#: ``MANUAL_SECTION`` row has no entry and refuses (:func:`table_f_blank_basis`).
TABLE_F_BLANK_BASIS: Final[dict[ScheduleCgTableFCase, str]] = {
    ScheduleCgTableFCase.LOSS_ACROSS_BANDS: TABLE_F_LOSS_ACROSS_BANDS_BASIS,
}


def table_f_blank_basis(case: ScheduleCgTableFCase) -> str | None:
    """The reason that starts a Table F row's Basis when ``case`` leaves it
    blank, or ``None`` for a computed row.

    Raises:
        InvariantError: For a ``MANUAL_SECTION`` row, which no Schedule CG
            section builds any more (#1383).
    """
    if case is ScheduleCgTableFCase.MANUAL_SECTION:
        raise invariant_error(
            what="a Schedule CG Table F row is MANUAL_SECTION, which no Schedule CG section builds"
        )
    return TABLE_F_BLANK_BASIS.get(case)


def _cell(amount: Decimal | None) -> int | None:
    """``amount`` as an integer cell; ``None`` stays a blank cell."""
    return None if amount is None else int(amount)


def _section_48_rows(
    section: str,
    amounts: ScheduleCgFilingSection,
    *,
    source_label: str,
    basis: ScheduleCgFilingBasis,
) -> list[list[object]]:
    """Consideration and section 48 deduction rows."""
    return [
        [
            f"{section}(a)(ii)",
            "Full value of consideration for assets other than unquoted shares",
            _cell(amounts.consideration_inr),
            "Enter/add",
            basis.consideration,
        ],
        [
            f"{section}(a)(iii)",
            "Total full value of consideration",
            _cell(amounts.total_consideration_inr),
            "Portal calculates/check",
            (
                f"{source_label} contribution equals {section}(a)(ii); its "
                "unquoted-share contribution is zero."
            ),
        ],
        [
            f"{section}(b)(i)",
            "Cost of acquisition without indexation",
            _cell(amounts.cost_of_acquisition_inr),
            "Enter/add",
            basis.cost,
        ],
        [
            f"{section}(b)(iii)",
            "Expenditure wholly and exclusively in connection with transfer",
            _cell(amounts.transfer_expenditure_inr),
            "Enter/add",
            basis.transfer_expenditure,
        ],
        [
            f"{section}(b)(iv)",
            "Total deductions under section 48",
            _cell(amounts.total_deductions_inr),
            "Portal calculates/check",
            (
                f"{source_label} contribution equals {section}(b)(i) + "
                f"{section}(b)(iii); its cost-of-improvement contribution is zero."
            ),
        ],
    ]


def _stcg_rows(
    section: str,
    amounts: ScheduleCgFilingSection,
    *,
    source_label: str,
    basis: ScheduleCgFilingBasis,
) -> list[list[object]]:
    result = _cell(amounts.result_inr)
    rows: list[list[object]] = [
        *_section_48_rows(section, amounts, source_label=source_label, basis=basis),
        [
            f"{section}(c)",
            "Balance: total consideration less section 48 deductions",
            _cell(amounts.balance_inr),
            "Portal calculates/check",
            f"{section}(a)(iii) - {section}(b)(iv).",
        ],
        [
            f"{section}(d)",
            "Loss disallowed under section 94(7) or 94(8)",
            _cell(amounts.section_94_7_8_disallowed_loss_inr),
            "Enter/add",
            basis.section_94_7_8,
        ],
    ]
    if section == "A5":
        rows.append(
            [
                "A5(e)",
                "STCG on assets other than at A1 or A2 or A3 or A4",
                result,
                "Portal calculates/check",
                (
                    f"{source_label} contribution is A5(c) + A5(d). A non-positive "
                    f"{source_label} result contributes zero to Table F."
                ),
            ]
        )
    else:
        rows.append(
            [
                "A6(g)",
                "STCG on assets other than at A1 or A2 or A3 or A4 or A5",
                result,
                "Portal calculates/check",
                (
                    f"A6(c) + A6(d); {basis.schedule_dcg} A non-positive "
                    f"{source_label} result contributes zero to Table F."
                ),
            ]
        )
    return rows


def _ltcg_rows(
    section: str,
    amounts: ScheduleCgFilingSection,
    *,
    source_label: str,
    basis: ScheduleCgFilingBasis,
) -> list[list[object]]:
    rows: list[list[object]] = [
        *_section_48_rows(section, amounts, source_label=source_label, basis=basis),
        [
            f"{section}(c)",
            "Balance: total consideration less section 48 deductions",
            _cell(amounts.balance_inr),
            "Portal calculates/check",
            "",
        ],
        [
            f"{section}(e)",
            "LTCG on residual assets",
            _cell(amounts.result_inr),
            "Portal calculates/check",
            (
                f"{source_label} contribution equals {section}(c); its section 54F "
                "deduction is zero because positive LTCG is blocked pending review."
            ),
        ],
    ]
    return rows


def _table_f_rows(
    row: int,
    description: str,
    table_f: ScheduleCgTableFRow,
    *,
    headings: tuple[str, ...],
    source_label: str,
    basis: ScheduleCgFilingBasis,
) -> list[list[object]]:
    """Table F row ``row``'s five date bands, (i) to (v); blank cells where
    ``table_f`` has no bands (#1360). A blank row's note gives its reason, then
    the same completion guidance as a computed row's (#1379)."""
    own_bands = table_f_blank_basis(table_f.case) or (
        f"{source_label} sales dated in this band, net, before any set-off; the five "
        "bands add up to this section's result, or are 0 when it isn't positive."
    )
    note = (
        f"{own_bands} Add the same band's amounts from every other capital-gains file: "
        f"{basis.table_f_other_files}. Then lower the bands for any capital loss set "
        "off against them, so the row totals the Schedule BFLA item its description "
        "names. The portal checks only the total."
    )
    bands = table_f.bands or (None,) * TABLE_F_BAND_COUNT
    return [
        [f"F{row}({column})", f"{heading}: {description}", _cell(band), "Enter/add", note]
        for column, heading, band in zip(TABLE_F_COLUMNS, headings, bands, strict=True)
    ]


def _visible_rows(
    rows: list[list[object]],
    *,
    include_checks: bool,
) -> tuple[list[list[object]], tuple[str, ...], bool]:
    if include_checks:
        return rows, (), False
    editable = [row for row in rows if row[3] == "Enter/add"]
    # A blank row shows even when the portal calculates it.
    visible = [row for row in rows if row[2] is None or (row[3] == "Enter/add" and row[2] != 0)]
    omitted_zero = tuple(str(row[0]) for row in editable if row[2] == 0)
    return visible, omitted_zero, bool(editable) and not visible


def _uncovered_note(refs: tuple[str, ...], basis: ScheduleCgFilingBasis) -> str:
    """The sentence naming ``refs``, the sections holding disposals the
    workbook doesn't cover; empty when there are none (#1322)."""
    if not refs:
        return ""
    return f"Manual entry in {' and '.join(refs)}: {basis.uncovered_disposals}."


def _scope_note(
    section: str,
    *,
    source_label: str,
    basis: ScheduleCgFilingBasis,
    is_stcg: bool,
    empty_counterpart: str | None,
    uncovered_refs: tuple[str, ...],
    include_checks: bool,
    omitted_editable: tuple[str, ...],
    all_editable_zero: bool,
) -> str:
    # ponytail: omitted under the adopted filing position/current safety gates;
    # restore these rows if either changes.
    own_editable = [ref for ref in omitted_editable if ref.startswith(section)]
    omitted = [f"{section}(a)(i)(a-c)", f"{section}(b)(ii)", *own_editable]
    if is_stcg and section == "A6":
        omitted.extend(("A6(e)", "A6(f)"))
    if not is_stcg:
        omitted.append(f"{section}(d)")
    # Zero Table F bands follow the section's own fields (#1360).
    omitted.extend(ref for ref in omitted_editable if ref not in own_editable)
    note = (
        f"Omitted zero {source_label} fields: {', '.join(omitted)}. "
        "Preserve any values from other sources."
    )
    if empty_counterpart is not None:
        note += (
            f" No {empty_counterpart} {source_label} disposals; no {source_label} "
            "entry is required."
        )
    if uncovered_refs:
        note += f" {_uncovered_note(uncovered_refs, basis)}"
    if include_checks:
        note += (
            " Debug view: modeled zero inputs and portal-calculated checks are "
            "included; use only Enter/add rows for filing."
        )
    else:
        note += (
            " Zero-valued editable fields and portal-calculated totals and results "
            "are available only with --debug."
        )
    if all_editable_zero:
        note += f" {source_label} disposals exist, but every editable amount rounds to 0; " + (
            "no numeric entry from this sheet is required."
            if section in uncovered_refs
            else f"no numeric {source_label} entry is required."
        )
    return note


def _sheet(
    *,
    title: str,
    rows: list[list[object]],
    position_note: str,
    scope_note: str,
    source_label: str,
) -> EntrySheet:
    return EntrySheet(
        title,
        instruction=(
            f"IMPORTANT: ADD {source_label} input amounts to any existing Schedule CG "
            f"values from other sources; do not replace existing totals. {scope_note}"
        ),
        warning=position_note,
        footnote=_FOOTNOTE,
        header=[
            "Portal field",
            "Official field description",
            f"{source_label} amount",
            "Portal action",
            "Basis",
        ],
        rows=rows,
        widths=_WIDTHS,
    )


def _workbook_sheets(
    result: ScheduleCgFilingResult,
    *,
    labels: ModuleType,
    itr_form: ItrForm,
    source_label: str,
    include_checks: bool,
) -> Iterator[EntrySheet]:
    """Project both sections now, and return the sheets lazily."""
    label = source_label
    stcg_ref = labels.PORTAL_SECTION_REF_CG_FOREIGN_STCG_BY_ITR_FORM[itr_form]
    ltcg_ref = labels.PORTAL_SECTION_REF_CG_FOREIGN_LTCG_BY_ITR_FORM[itr_form]
    stcg = result.stcg_amounts
    ltcg = result.ltcg_amounts
    roundings = {
        "STCG": stcg.component_rounding_difference_inr,
        "LTCG": ltcg.component_rounding_difference_inr,
    }
    rounding_note = (
        " Whole-rupee portal component arithmetic differs from separately "
        "rounding the full-precision gain by "
        + " and ".join(f"{name} {difference}" for name, difference in roundings.items())
        + "; the workbook uses the portal component result."
        if any(roundings.values())
        else ""
    )
    uncovered = {
        stcg_ref: result.stcg_uncovered_disposals,
        ltcg_ref: result.ltcg_uncovered_disposals,
    }
    position_note = result.classification_advisory or (
        f"No {label} "
        + (result.basis.covered_disposals if any(uncovered.values()) else "capital-gain disposals")
        + " were attributed to this financial year."
    )
    position_note += rounding_note
    # Each section sheet states the source's filing position (#1383).
    section_note = f"{position_note} {result.basis.consideration_position}"
    has_stcg = bool(result.stcg.stcg_lines)
    has_ltcg = bool(result.ltcg.ltcg_lines)

    def uncovered_refs(*refs: str) -> tuple[str, ...]:
        """Those of ``refs`` holding disposals the workbook doesn't cover, STCG first.

        A section's sheet speaks for it, and for the other section too when
        that one has no sheet; the summary speaks for both."""
        return tuple(ref for ref in (stcg_ref, ltcg_ref) if ref in refs and uncovered[ref])

    def empty_counterpart(term: str, ref: str, *, has_lines: bool) -> str | None:
        """The counterpart a sheet's note says needs no entry: empty and fully covered."""
        return None if has_lines or uncovered[ref] else f"{term} ({ref})"

    headings = labels.PORTAL_COLUMN_HEADERS_CG_TABLE_F
    stcg_rows, stcg_omitted, stcg_all_zero = _visible_rows(
        [
            *_stcg_rows(stcg_ref, result.stcg_section, source_label=label, basis=result.basis),
            *_table_f_rows(
                3,
                labels.PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM[itr_form],
                result.stcg_table_f,
                headings=headings,
                source_label=label,
                basis=result.basis,
            ),
        ],
        include_checks=include_checks,
    )
    ltcg_rows, ltcg_omitted, ltcg_all_zero = _visible_rows(
        [
            *_ltcg_rows(ltcg_ref, result.ltcg_section, source_label=label, basis=result.basis),
            *_table_f_rows(
                5,
                labels.PORTAL_ROW_LABEL_CG_TABLE_F_LTCG_BY_ITR_FORM[itr_form],
                result.ltcg_table_f,
                headings=headings,
                source_label=label,
                basis=result.basis,
            ),
        ],
        include_checks=include_checks,
    )

    def sheets() -> Iterator[EntrySheet]:
        if has_stcg:
            yield _sheet(
                title=f"{itr_form} {stcg_ref} STCG",
                rows=stcg_rows,
                position_note=section_note,
                scope_note=_scope_note(
                    stcg_ref,
                    source_label=label,
                    basis=result.basis,
                    is_stcg=True,
                    empty_counterpart=empty_counterpart("long-term", ltcg_ref, has_lines=has_ltcg),
                    uncovered_refs=(
                        uncovered_refs(stcg_ref) if has_ltcg else uncovered_refs(stcg_ref, ltcg_ref)
                    ),
                    include_checks=include_checks,
                    omitted_editable=stcg_omitted,
                    all_editable_zero=stcg_all_zero,
                ),
                source_label=label,
            )
        if has_ltcg:
            yield _sheet(
                title=f"{itr_form} {ltcg_ref} LTCG",
                rows=ltcg_rows,
                position_note=section_note,
                scope_note=_scope_note(
                    ltcg_ref,
                    source_label=label,
                    basis=result.basis,
                    is_stcg=False,
                    empty_counterpart=empty_counterpart("short-term", stcg_ref, has_lines=has_stcg),
                    uncovered_refs=(
                        uncovered_refs(ltcg_ref) if has_stcg else uncovered_refs(stcg_ref, ltcg_ref)
                    ),
                    include_checks=include_checks,
                    omitted_editable=ltcg_omitted,
                    all_editable_zero=ltcg_all_zero,
                ),
                source_label=label,
            )
        if not has_stcg and not has_ltcg:
            not_required = " or ".join(ref for ref in (stcg_ref, ltcg_ref) if not uncovered[ref])
            yield _sheet(
                title=f"{itr_form} Schedule CG",
                rows=[],
                position_note=position_note,
                scope_note=" ".join(
                    part
                    for part in (
                        not_required and f"No {label} entry is required in {not_required}.",
                        _uncovered_note(uncovered_refs(stcg_ref, ltcg_ref), result.basis),
                        "Preserve any values from other sources.",
                    )
                    if part
                ),
                source_label=label,
            )

    return sheets()


def cg_filing_sheets(
    ctx: RenderContext,
) -> Callable[[ScheduleCgFilingResult], Iterator[EntrySheet]]:
    """The run's filing workbook sheets; checks ``ctx``, then resolves its labels.

    ``ctx`` must name the ITR form and source the workbook prints; a
    ``--debug`` run adds the zero-valued editable fields and the
    portal-calculated checks. The returned function refuses a blocked result,
    then projects both sections before the workbook is created. It returns the
    sheets lazily: STCG, then LTCG, or the summary when neither section has a
    disposal. A section whose Table F bands are blank because a loss band
    sits beside two or more gain bands shows them blank (#1360).
    """
    itr_form, source_label = ctx.itr_form, ctx.source_label
    if itr_form is None or source_label is None:
        raise invariant_error(
            what="the Schedule CG filing workbook needs the run's ITR form and source label",
            itr_form=itr_form,
            source_label=source_label,
        )
    labels = for_ay(ctx.assessment_year_start)

    def sheets(result: ScheduleCgFilingResult) -> Iterator[EntrySheet]:
        require_unblocked_schedule_cg_filing(result.blockers)
        return _workbook_sheets(
            result,
            labels=labels,
            itr_form=itr_form,
            source_label=source_label,
            include_checks=ctx.debug,
        )

    return sheets


__all__ = [
    "TABLE_F_BLANK_BASIS",
    "TABLE_F_COLUMNS",
    "TABLE_F_LOSS_ACROSS_BANDS_BASIS",
    "cg_filing_sheets",
    "table_f_blank_basis",
]
