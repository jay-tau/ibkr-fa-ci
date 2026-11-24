"""A2/A3 output pair plus the separate Quicko workbook."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.espp_positions import NetBenefitsFaError
from ibkr_fa.exceptions import errno_name
from ibkr_fa.models import FaA3Row
from ibkr_fa.models.fa_a2_row import FaA2Row
from ibkr_fa.output import RenderContext, Subcommand
from ibkr_fa.output.bundle import spec_for, write_output_bundle
from ibkr_fa.output.netbenefits_quicko_fa_xlsx import (
    QUICKO_TEMPLATE_FILENAME,
    QuickoFaWorkbookInput,
    QuickoFaXlsxWriter,
)


def write_netbenefits_fa_outputs(
    *,
    rows: Sequence[FaA3Row],
    a2_row: FaA2Row,
    output_path: Path,
    a2_path: Path,
    quicko_path: Path,
    input_dir: Path,
    format_id: str,
    assessment_year_start: int,
    itr_form: ItrForm | None,
    quicko_workbook: bool,
) -> Path | None:
    """Write A2, A3 and, when requested, the Quicko workbook. The caller's
    cleanup window removes all three if anything fails.

    An ``OSError`` from the Quicko write refuses with ``quicko_path`` and its
    errno name; any other unexpected exception propagates, so the CLI reports
    it as an internal error."""

    ctx = RenderContext(assessment_year_start=assessment_year_start, itr_form=itr_form)
    write_output_bundle(
        [
            spec_for(Subcommand.FA_A2, format_id, [a2_row], a2_path),
            spec_for(Subcommand.FA, format_id, rows, output_path),
        ],
        ctx,
    )
    if not quicko_workbook:
        return None
    try:
        QuickoFaXlsxWriter.from_context(ctx).write(
            QuickoFaWorkbookInput(
                rows=tuple(rows),
                account_number=a2_row.account_number,
                template_path=input_dir / QUICKO_TEMPLATE_FILENAME,
            ),
            quicko_path,
        )
    except OSError as exc:
        raise NetBenefitsFaError(
            f"could not write the requested Quicko workbook {quicko_path} ({errno_name(exc)})"
        ) from None
    return quicko_path


__all__ = ["write_netbenefits_fa_outputs"]
