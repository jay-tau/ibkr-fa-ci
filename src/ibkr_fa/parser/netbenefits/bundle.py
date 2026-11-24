"""Discover and parse the files in a Fidelity NetBenefits bundle directory.

A bundle (``--input <dir>``) holds the open- and closed-lots CSVs, the
trade-confirmation PDFs and the Stock Plan Services statement PDFs.
:func:`load_bundle` reads the lots CSVs and the confirmations, and
:func:`load_statements` reads the statements. Every PDF is classified by its
title (:func:`classify_pdf`) before it is parsed.

Refusals about discovered files label them by their path relative to the
``--input`` folder and print no value from their contents. An invalid input
directory is named by its typed path. Errors listing folders or inspecting
file entries under it refuse without naming the entry.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final, NoReturn

from ibkr_fa.exceptions import IbkrFaError, errno_name
from ibkr_fa.parser._pdf import pdftotext_layout
from ibkr_fa.parser.netbenefits.confirmation_pdf import EspPurchase, parse_confirmation
from ibkr_fa.parser.netbenefits.lots_csv import (
    EspClosedLot,
    EspLot,
    FidelityLotsKind,
    MarkupNearMiss,
    NearMissLotsHeader,
    detect_fidelity_lots_csv,
    read_closed_lots,
    read_open_lots,
)
from ibkr_fa.parser.netbenefits.statement_pdf import ReportType, StatementData, parse_statement
from ibkr_fa.path_checks import is_dir, is_file

# Document-title markers used to tell a trade confirmation from a statement when
# classifying the bundle's PDFs. Mirror the titles in confirmation_pdf /
# statement_pdf; a PDF that is neither (or both) is refused.
_CONFIRMATION_MARKER: Final[str] = "YOU PURCHASED"
_STATEMENT_MARKERS: Final[tuple[str, ...]] = (
    "STOCK PLAN SERVICES REPORT",
    "YEAR-END INVESTMENT REPORT",
)
_NEAR_MISS_ADVICE: Final[str] = (
    "If Fidelity changed the export, ibkr-fa must be updated to read it; otherwise "
    "move the file out of the input directory"
)


class NetBenefitsBundleError(IbkrFaError):
    """A NetBenefits bundle's files are missing, duplicated or ambiguous.

    The CLI maps it (like every :class:`IbkrFaError`) to the domain-error exit
    code.
    """


@dataclass(frozen=True, slots=True)
class NetBenefitsBundle:
    """The classified, parsed lots CSVs and confirmations in the ``--input`` directory."""

    open_lots: tuple[EspLot, ...]
    closed_lots: tuple[EspClosedLot, ...]
    confirmations_by_date: Mapping[date, EspPurchase]


def classify_pdf(text: str, label: str) -> str:
    """Return ``"confirmation"`` or ``"statement"`` for a bundle PDF, or refuse.

    A PDF that matches neither marker (or both) is ambiguous and refused rather
    than guessed (a misclassified document would attach the wrong evidence).
    ``label`` names the PDF in the refusal.
    """
    is_conf = _CONFIRMATION_MARKER in text
    is_stmt = any(marker in text for marker in _STATEMENT_MARKERS)
    if is_conf and not is_stmt:
        return "confirmation"
    if is_stmt and not is_conf:
        return "statement"
    raise NetBenefitsBundleError(
        f"cannot classify PDF {label}: it matches "
        f"{'both confirmation and statement' if is_conf else 'neither confirmation nor statement'} "
        f"markers; remove non-NetBenefits PDFs from the input directory"
    )


def load_bundle(input_dir: Path, *, require_open_lots: bool = True) -> NetBenefitsBundle:
    """Discover and parse the lots CSVs + confirmation PDFs under ``input_dir``.

    Requires exactly one closed-lots CSV (it may list no lots, but still needs
    its USD footer; its absence leaves the Dec-31 quantity unprovable, so it is
    refused). By default also requires exactly one open-lots CSV (Table A3 needs
    the Dec-31 holdings); ``require_open_lots=False`` (used by the capital-gains
    runs, which read no open lots) ignores recognized open-lots CSVs — their lot
    data are not parsed, and their count is not checked — so a full liquidation,
    duplicate exports, or malformed lot data under an exact open-lots header never
    blocks a CG run. Statement PDFs are classified but not parsed here;
    :func:`load_statements` parses them, and the capital-gains runs call it too
    (#666), so a statement the parser refuses blocks a CG run there.

    A CSV that looks like a lots export but matches neither exact header (see
    :class:`NearMissLotsHeader`) is refused, since ignoring it could drop a
    changed export's lots (#911). So is any CSV holding markup (``<`` or ``>``)
    other than the closed-lots header's documented decoration (see
    :class:`MarkupNearMiss`, #1097): markup could hide a lots export, so such a
    file is never ignored as unrelated. The exception is a near miss proven to be
    open-lots only when ``require_open_lots=False``: it is ignored like any
    open-lots export. Every other CSV, such as notes or an earlier run's output,
    is ignored.

    Raises:
        NetBenefitsBundleError: The bundle's files are missing, duplicated or
            ambiguous, a folder can't be listed or a file entry can't be inspected.
        OSError: ``input_dir`` itself can't be inspected.
        ParseError: A file can't be read or parsed. Neither error prints a
            value from a file; see :mod:`.lots_csv` and :mod:`.confirmation_pdf`.
    """
    if not is_dir(input_dir):
        raise NetBenefitsBundleError(f"input path is not a directory: {input_dir}")

    open_csvs: list[Path] = []
    closed_csvs: list[Path] = []
    for csv_path in _files_with_suffix(input_dir, ".csv"):
        kind = detect_fidelity_lots_csv(csv_path, source=_label(csv_path, input_dir))
        if kind is FidelityLotsKind.OPEN:
            open_csvs.append(csv_path)
        elif kind is FidelityLotsKind.CLOSED:
            closed_csvs.append(csv_path)
        elif isinstance(kind, MarkupNearMiss) and (require_open_lots or not kind.open_lots_only):
            raise NetBenefitsBundleError(
                f"{_label(csv_path, input_dir)} holds markup ('<' or '>') other than the "
                "closed-lots header's documented <span> decoration, so it could hide a "
                f"Fidelity lots export and can't be ignored. {_NEAR_MISS_ADVICE}"
            )
        elif isinstance(kind, NearMissLotsHeader) and (
            require_open_lots or not kind.open_lots_only
        ):
            raise NetBenefitsBundleError(
                f"{_label(csv_path, input_dir)} looks like a Fidelity lots export but "
                f"matches neither lots header: {kind.difference()}. {_NEAR_MISS_ADVICE}"
            )
    # Require exactly one open-lots CSV for FA. For CG (require_open_lots=False)
    # open-lots CSVs are ignored ENTIRELY — CG never reads them, so an absent,
    # duplicate, or malformed open-lots export must not block a valid CG run.
    if require_open_lots:
        _require_one(open_csvs, "open-lots CSV", input_dir)
    _require_one(
        closed_csvs,
        "closed-lots CSV (required; its absence leaves Dec-31 quantity unprovable)",
        input_dir,
    )

    confirmations: dict[date, EspPurchase] = {}
    confirmation_labels: dict[date, str] = {}
    for pdf_path in _files_with_suffix(input_dir, ".pdf"):
        label = _label(pdf_path, input_dir)
        text = pdftotext_layout(pdf_path, doc_kind="NetBenefits PDFs", source=label)
        if classify_pdf(text, label) != "confirmation":
            continue
        purchase = parse_confirmation(text, source=f"confirmation {label}")
        if purchase.purchase_date in confirmations:
            raise NetBenefitsBundleError(
                f"confirmations {confirmation_labels[purchase.purchase_date]} and {label} "
                "share a purchase date; the bundle is ambiguous"
            )
        confirmations[purchase.purchase_date] = purchase
        confirmation_labels[purchase.purchase_date] = label

    return NetBenefitsBundle(
        open_lots=(
            read_open_lots(open_csvs[0], source=f"open lots {_label(open_csvs[0], input_dir)}")
            if require_open_lots
            else ()
        ),
        closed_lots=read_closed_lots(
            closed_csvs[0], source=f"closed lots {_label(closed_csvs[0], input_dir)}"
        ),
        confirmations_by_date=confirmations,
    )


def load_statements(
    input_dir: Path, participant_no: str, *, include_year_end: bool
) -> list[StatementData]:
    """Parse the bundle's statement PDFs (with the participant cross-check),
    sorted by period start; confirmations are ignored.

    The Table A2 cash-zero proof (#I11) and income pass ``include_year_end=True``
    and get every statement. Capital gains passes ``False`` and gets only the
    itemizing SPS reports. A year-end report is a holdings/NAV summary, not an
    itemized income ledger, so its silence on a date can't prove no dividend
    occurred there, and it never counts towards coverage. But an income row it
    prints is evidence and must match the SPS reports (#1249).

    Raises:
        NetBenefitsBundleError: ``input_dir`` is not a directory, a folder can't
            be listed, a file entry can't be inspected, or a PDF can't be
            classified unambiguously as a confirmation or a statement.
        OSError: ``input_dir`` itself can't be inspected.
        ParseError: A PDF can't be rendered or a statement can't be parsed. No
            refusal prints a value from a file; see :mod:`.statement_pdf`.
    """
    if not is_dir(input_dir):
        raise NetBenefitsBundleError(f"input path is not a directory: {input_dir}")
    statements: list[StatementData] = []
    for pdf_path in _files_with_suffix(input_dir, ".pdf"):
        label = _label(pdf_path, input_dir)
        text = pdftotext_layout(pdf_path, doc_kind="NetBenefits PDFs", source=label)
        if classify_pdf(text, label) != "statement":
            continue
        stmt = parse_statement(
            text, expected_participant_no=participant_no, source=f"statement {label}"
        )
        if not include_year_end and stmt.report_type is ReportType.YEAR_END:
            continue
        statements.append(stmt)
    statements.sort(key=lambda s: s.period_start)
    return statements


def _files_with_suffix(input_dir: Path, suffix: str) -> list[Path]:
    """Sorted files under ``input_dir`` whose extension matches ``suffix``,
    case-insensitively (a browser may save ``View open lots.CSV`` on some OSes).

    Every folder is listed and every entry checked, so a folder that can't be
    read or searched refuses instead of hiding the files in it (#1238). Like
    ``rglob``, this doesn't follow a linked folder.
    """
    try:
        found = [
            path
            for folder, _folders, names in os.walk(input_dir, onerror=_reraise)
            for path in (Path(folder, name) for name in names)
            if is_file(path) and path.suffix.lower() == suffix
        ]
    except OSError as exc:
        message = f"could not read every folder and file in the input directory ({errno_name(exc)})"
    else:
        return sorted(found)
    # Refused outside the handler: the error names the folder or file.
    raise NetBenefitsBundleError(message)


def _reraise(error: OSError) -> NoReturn:
    raise error


def _label(path: Path, input_dir: Path) -> str:
    """``path`` relative to the ``--input`` folder: the file's label in refusals."""
    return path.relative_to(input_dir).as_posix()


def _require_one(paths: list[Path], what: str, input_dir: Path) -> None:
    if len(paths) != 1:
        names = ", ".join(_label(p, input_dir) for p in paths) or "none"
        raise NetBenefitsBundleError(
            f"expected exactly one {what} in the input directory; found {len(paths)} ({names})"
        )
