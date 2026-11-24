#!/usr/bin/env python3
"""Assessment-year rollover scaffolding for ``output/portal_labels``.

Usage::

    uv run python scripts/rollover_portal_labels.py --from-ay 2026-27 --to-ay 2027-28
    uv run python scripts/rollover_portal_labels.py --from-ay 2026-27 --to-ay 2027-28 --force

Issue #66. Provides a single library entry point,
:func:`scaffold_next_ay_snapshot`, that copies an existing per-AY
portal-labels snapshot (e.g., ``_ay_2026_27.py``) to a new AY
(``_ay_2027_28.py``) and prepends a TODO checklist header so the
maintainer remembers to:

1. Verify every label against the new AY's published ITR utility
   (CBDT typically releases a v1.0 Excel utility within a few months
   of the AY start).
2. Register the new snapshot in
   :data:`~ibkr_fa.output.portal_labels._SNAPSHOTS_BY_AY_START`.
3. Bump :data:`~ibkr_fa.output.portal_labels.CURRENT_AY_START` once
   the new AY's snapshot is the production default.

The function is deliberately a thin file-level helper (no AST / Python
parsing): it copies bytes, prepends a marker block, and rewrites the
title line + introductory sentences that name the source AY. Anything
more clever (e.g., parsing labels and rewriting them per a diff
manifest) belongs in a follow-up issue once CBDT publishes the first
real schema change.

It writes only into this checkout's ``src/ibkr_fa/output/portal_labels/``
(:data:`PACKAGE_DIR`), never into an installed package: the new
snapshot has to be registered in the source anyway (issue #686).

See also:

* :func:`ibkr_fa.output.portal_labels.for_ay` — runtime selector that
  the scaffolded snapshot plugs into once registered.
* Issue #65 (closed) — per-AY snapshot package that this rollover
  helper extends.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ibkr_fa.exceptions import IbkrFaError

_MIN_PLAUSIBLE_AY_START: Final[int] = 1900
_ASSESSMENT_YEAR_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<start>\d{4})-(?P<end>\d{2}|\d{4})$")

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
PACKAGE_DIR: Final[Path] = REPO_ROOT / "src/ibkr_fa/output/portal_labels"


def _module_filename_for_ay(assessment_year_start: int) -> str:
    """Return the canonical snapshot filename for ``assessment_year_start``.

    E.g., ``2026`` -> ``"_ay_2026_27.py"``.
    """
    end_short = (assessment_year_start + 1) % 100
    return f"_ay_{assessment_year_start}_{end_short:02d}.py"


def _ay_label(assessment_year_start: int) -> str:
    """Return the human-readable AY label for ``assessment_year_start``.

    E.g., ``2026`` -> ``"2026-27"``.
    """
    end_short = (assessment_year_start + 1) % 100
    return f"{assessment_year_start}-{end_short:02d}"


@dataclass(frozen=True, slots=True)
class SnapshotSourceNotFoundError(IbkrFaError):
    """The requested source snapshot file does not exist.

    Raised by :func:`scaffold_next_ay_snapshot` when
    ``package_dir / _ay_<from>_<short>.py`` is absent — typically
    because the maintainer passed the wrong ``--from-ay`` or the
    snapshot was deleted.
    """

    source_path: Path
    from_ay_start: int

    def __str__(self) -> str:
        return (
            f"Source portal-labels snapshot for AY "
            f"{_ay_label(self.from_ay_start)} not found at "
            f"{self.source_path}. Pass --from-ay matching an existing "
            f"snapshot file."
        )


@dataclass(frozen=True, slots=True)
class SnapshotTargetExistsError(IbkrFaError):
    """The target snapshot file already exists.

    Raised by :func:`scaffold_next_ay_snapshot` when
    ``package_dir / _ay_<to>_<short>.py`` already exists and
    ``force=False``. The rollover refuses to overwrite by default
    so a half-edited next-AY snapshot is not silently clobbered.
    """

    target_path: Path
    to_ay_start: int

    def __str__(self) -> str:
        return (
            f"Target portal-labels snapshot for AY "
            f"{_ay_label(self.to_ay_start)} already exists at "
            f"{self.target_path}. Pass --force to overwrite (this "
            f"discards any in-progress edits)."
        )


@dataclass(frozen=True, slots=True)
class InvalidRolloverDirectionError(IbkrFaError):
    """``--to-ay`` is not strictly later than ``--from-ay``.

    The rollover helper is forward-only: scaffolding a PRIOR AY's
    snapshot from a LATER one would lose audit-trail provenance.
    Equal AYs are also rejected (no-op).
    """

    from_ay_start: int
    to_ay_start: int

    def __str__(self) -> str:
        return (
            f"Rollover direction must be forward: --to-ay "
            f"{_ay_label(self.to_ay_start)} must be later than "
            f"--from-ay {_ay_label(self.from_ay_start)}. "
            f"To revisit an older AY, edit its snapshot directly."
        )


@dataclass(frozen=True, slots=True)
class ImplausibleAssessmentYearError(IbkrFaError):
    """An assessment-year start value is implausibly small.

    Mirrors the plausible-AY invariant used elsewhere in the
    codebase (``_PLAUSIBLE_MIN_AY_START`` in pipeline.py).
    Catches typos like ``--from-ay 226-27`` that would otherwise
    silently create ``_ay_226_27.py``.
    """

    assessment_year_start: int
    field: str

    def __str__(self) -> str:
        return (
            f"{self.field}={self.assessment_year_start} is implausibly "
            f"small (must be >= {_MIN_PLAUSIBLE_AY_START}). Pass a "
            f"4-digit AY start year, e.g., 2026 for AY 2026-27."
        )


@dataclass(frozen=True, slots=True)
class ScaffoldResult:
    """Outcome of a :func:`scaffold_next_ay_snapshot` call.

    Returned so :func:`main` (and tests) can render a
    structured summary and decide on next steps. ``bytes_written``
    is the size of the new file on disk, useful as a sanity-check
    in tests.
    """

    source_path: Path
    target_path: Path
    from_ay_start: int
    to_ay_start: int
    bytes_written: int
    overwrote_existing: bool


def _build_todo_header(*, from_ay_start: int, to_ay_start: int, source_filename: str) -> str:
    """Build the TODO-checklist block prepended to a scaffolded snapshot.

    The block is rendered as a Python comment so the file remains
    importable in its scaffolded state (no syntax error). The
    docstring inside the body still applies — the comment block sits
    ABOVE the ``from __future__ import annotations`` line.
    """
    from_label = _ay_label(from_ay_start)
    to_label = _ay_label(to_ay_start)
    return (
        f"# =============================================================\n"
        f"# TODO (issue #66 rollover): scaffolded from {source_filename}\n"
        f"#                            for AY {to_label}\n"
        f"# =============================================================\n"
        f"#\n"
        f"# This file was generated by `scripts/rollover_portal_labels.py\n"
        f"# --from-ay {from_label} --to-ay {to_label}`. Every label below is a\n"
        f"# VERBATIM COPY of the AY {from_label} snapshot and has NOT\n"
        f"# been verified against the AY {to_label} ITR utility.\n"
        f"#\n"
        f"# Before merging this file:\n"
        f"#\n"
        f"#  [ ] Download the CBDT-published ITR-2 utility for\n"
        f"#      AY {to_label} (the Excel/Java offline utility, not the\n"
        f"#      online portal — the offline utility's TR_FA sheet is\n"
        f"#      the source-of-truth for column headers and data\n"
        f"#      validation constraints).\n"
        f"#  [ ] Diff every PORTAL_COLUMN_HEADERS_FA_A3 entry against\n"
        f"#      the new utility's TR_FA sheet, rows 50-51, columns\n"
        f"#      E-O. Update verbatim and capture screenshot evidence\n"
        f"#      in docs/portal/schedule-fa-a3.md.\n"
        f"#  [ ] Diff PORTAL_FIELD_LABEL_OS_* against Schedule OS row\n"
        f"#      1a(i), 1a(ii), 1a(iii), and interest cells. Update + capture screenshot\n"
        f"#      evidence in docs/portal/schedule-os.md.\n"
        f"#  [ ] Diff PORTAL_FIELD_LABEL_CG_FOREIGN_* against Schedule\n"
        f"#      CG Section A item 5 (STCG) and Section B item 7\n"
        f"#      (LTCG). Update + capture screenshot evidence in\n"
        f"#      docs/portal/schedule-cg.md and\n"
        f"#      docs/portal/schedule-cg-ltcg.md.\n"
        f"#  [ ] Re-verify PORTAL_DATE_FORMAT (still DD/MM/YYYY? Check\n"
        f"#      cell J52:J57 textLength + number format in the new\n"
        f"#      utility's TR_FA sheet).\n"
        f"#  [ ] Re-verify format_amount_for_portal's whole-rupee cells\n"
        f"#      (whole-rupee integer vs. fractional cells; the models\n"
        f"#      round, the helper only checks).\n"
        f"#  [ ] Update the module docstring below to reference the\n"
        f"#      new AY {to_label} verification evidence (date + URL).\n"
        f"#  [ ] Register this snapshot in _SNAPSHOTS_BY_AY_START in\n"
        f"#      src/ibkr_fa/output/portal_labels/__init__.py.\n"
        f"#  [ ] Once verified + registered, decide whether to bump\n"
        f"#      CURRENT_AY_START to {to_ay_start} (this routes the\n"
        f"#      module-level re-exports to the new snapshot).\n"
        f"#  [ ] DELETE THIS HEADER once every checkbox above is\n"
        f"#      ticked — the presence of this block in main is a\n"
        f"#      regression signal.\n"
        f"# =============================================================\n"
        f"\n"
    )


def _rewrite_module_title_line(source_text: str, *, from_ay_start: int, to_ay_start: int) -> str:
    """Rewrite the first docstring line + any inline AY references.

    Replaces the AY label in the module title line so the scaffolded
    file's docstring at least claims the correct AY. The TODO header
    still tells the maintainer to fully re-verify every label.

    Only the AY-label substring is touched; the rest of the
    docstring (which explains per-AY snapshot design rationale) is
    left intact since it is AY-independent.
    """
    from_label = _ay_label(from_ay_start)
    to_label = _ay_label(to_ay_start)
    return source_text.replace(from_label, to_label)


def scaffold_next_ay_snapshot(
    *,
    from_ay_start: int,
    to_ay_start: int,
    package_dir: Path,
    force: bool = False,
) -> ScaffoldResult:
    """Scaffold the per-AY portal-labels snapshot for ``to_ay_start``.

    Copies the existing ``_ay_<from>_<short>.py`` snapshot to
    ``_ay_<to>_<short>.py``, prepends a TODO-checklist comment
    block, and rewrites the AY label in the docstring to the new
    AY. The file is created in ``package_dir`` (:data:`PACKAGE_DIR`
    when run as a script; tests pass a temporary directory).

    Args:
        from_ay_start: 4-digit AY start year to copy FROM
            (e.g., ``2026`` for AY 2026-27).
        to_ay_start: 4-digit AY start year to scaffold TO
            (e.g., ``2027`` for AY 2027-28). Must be strictly
            greater than ``from_ay_start``.
        package_dir: Directory containing the existing
            ``_ay_<from>_<short>.py`` snapshot. The new file is
            written here.
        force: If ``True``, overwrite the target file when it
            already exists. Defaults to ``False`` — the default
            refuses to overwrite to protect in-progress edits.

    Returns:
        A :class:`ScaffoldResult` summarising the rollover.

    Raises:
        ImplausibleAssessmentYearError: Either AY start is below
            :data:`_MIN_PLAUSIBLE_AY_START`.
        InvalidRolloverDirectionError: ``to_ay_start <= from_ay_start``.
        SnapshotSourceNotFoundError: No file at
            ``package_dir / _ay_<from>_<short>.py``.
        SnapshotTargetExistsError: Target file exists and ``force``
            is ``False``.
    """
    if from_ay_start < _MIN_PLAUSIBLE_AY_START:
        raise ImplausibleAssessmentYearError(
            assessment_year_start=from_ay_start, field="from_ay_start"
        )
    if to_ay_start < _MIN_PLAUSIBLE_AY_START:
        raise ImplausibleAssessmentYearError(assessment_year_start=to_ay_start, field="to_ay_start")
    if to_ay_start <= from_ay_start:
        raise InvalidRolloverDirectionError(from_ay_start=from_ay_start, to_ay_start=to_ay_start)

    source_filename = _module_filename_for_ay(from_ay_start)
    target_filename = _module_filename_for_ay(to_ay_start)
    source_path = package_dir / source_filename
    target_path = package_dir / target_filename

    if not source_path.is_file():
        raise SnapshotSourceNotFoundError(source_path=source_path, from_ay_start=from_ay_start)

    target_existed = target_path.exists()
    if target_existed and not force:
        raise SnapshotTargetExistsError(target_path=target_path, to_ay_start=to_ay_start)

    source_text = source_path.read_text(encoding="utf-8")
    rewritten = _rewrite_module_title_line(
        source_text, from_ay_start=from_ay_start, to_ay_start=to_ay_start
    )
    todo_header = _build_todo_header(
        from_ay_start=from_ay_start,
        to_ay_start=to_ay_start,
        source_filename=source_filename,
    )
    scaffolded = todo_header + rewritten

    target_path.write_text(scaffolded, encoding="utf-8")

    return ScaffoldResult(
        source_path=source_path,
        target_path=target_path,
        from_ay_start=from_ay_start,
        to_ay_start=to_ay_start,
        bytes_written=len(scaffolded.encode("utf-8")),
        overwrote_existing=target_existed,
    )


def _parse_assessment_year(spec: str) -> int:
    """Parse an AY label (``"YYYY-YY"`` or ``"YYYY-YYYY"``) to its 4-digit start.

    The argparse ``type=`` for ``--from-ay`` / ``--to-ay``: accepts
    the canonical portal AY labels (e.g., ``"2026-27"`` for AY
    2026-27) and normalises to the integer the portal-labels package
    consumes (e.g., ``2026``).

    Raises:
        argparse.ArgumentTypeError: If ``spec`` doesn't match the
            expected pattern, or if the end year doesn't equal
            start year + 1 (e.g., ``"2026-28"`` is rejected — Indian
            AYs always span exactly one year).
    """
    match = _ASSESSMENT_YEAR_RE.fullmatch(spec)
    if match is None:
        raise argparse.ArgumentTypeError(
            f"assessment year must be in YYYY-YY or YYYY-YYYY form "
            f"(e.g., '2026-27' or '2026-2027'); got {spec!r}."
        )
    start_year = int(match.group("start"))
    end_token = match.group("end")
    expected_end_short = (start_year + 1) % 100
    if len(end_token) == 2:  # noqa: PLR2004 — short-form AY tag width
        end_value = int(end_token)
        if end_value != expected_end_short:
            raise argparse.ArgumentTypeError(
                f"assessment year end portion {end_token!r} does not "
                f"match start year {start_year} + 1 = {start_year + 1} "
                f"(expected '-{expected_end_short:02d}' for AY "
                f"{start_year}-{expected_end_short:02d})."
            )
    else:
        end_value = int(end_token)
        if end_value != start_year + 1:
            raise argparse.ArgumentTypeError(
                f"assessment year end year {end_token!r} does not equal "
                f"start year + 1 = {start_year + 1} (Indian AY spans "
                f"exactly one year)."
            )
    return start_year


def _render_summary(result: ScaffoldResult) -> str:
    """Render a stdout summary for a successful rollover scaffold."""
    from_label = _ay_label(result.from_ay_start)
    to_label = _ay_label(result.to_ay_start)
    action = "overwrote" if result.overwrote_existing else "scaffolded"
    lines = [
        f"Portal-labels snapshot {action} for AY {to_label}.",
        f"  source: {result.source_path}",
        f"  target: {result.target_path}",
        f"  bytes:  {result.bytes_written}",
        "",
        "Next steps (also encoded as a TODO checklist at the top of",
        "the new file; the placeholder block MUST be deleted before",
        f"the AY {to_label} snapshot is fit for production use):",
        "",
        "  1. Verify every label in the scaffolded file against the",
        f"     CBDT-published ITR-2 offline utility for AY {to_label}.",
        "     (See docs/portal/schedule-*.md for the per-schedule",
        f"     evidence-capture protocol established for AY {from_label}.)",
        "  2. Register the new snapshot in _SNAPSHOTS_BY_AY_START in",
        "     src/ibkr_fa/output/portal_labels/__init__.py.",
        "  3. Decide whether to bump CURRENT_AY_START to",
        f"     {result.to_ay_start} (routes module-level re-exports",
        "     to the new snapshot).",
        "  4. Add per-AY tests in tests/test_output_portal_labels_*.",
        "  5. Delete the TODO header block in the scaffolded file",
        "     once every checkbox is ticked.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Scaffold the next Assessment Year's portal-labels snapshot "
            "file by copying an existing per-AY snapshot (e.g., "
            "_ay_2026_27.py) to a new AY's snapshot (_ay_2027_28.py) under "
            "src/ibkr_fa/output/portal_labels/ in this checkout. The "
            "scaffolded file carries a TODO checklist comment block at the "
            "top that the maintainer must walk through. This script only "
            "writes the scaffolded file; it does NOT register the new AY in "
            "_SNAPSHOTS_BY_AY_START (that requires human review of label "
            "changes from the new CBDT utility)."
        ),
    )
    parser.add_argument(
        "--from-ay",
        dest="from_ay_start",
        type=_parse_assessment_year,
        required=True,
        metavar="AY",
        help=(
            "Source AY whose snapshot is being rolled forward "
            "(e.g., '2026-27' or '2026-2027' for AY 2026-27). The "
            "corresponding _ay_YYYY_YY.py file must already exist."
        ),
    )
    parser.add_argument(
        "--to-ay",
        dest="to_ay_start",
        type=_parse_assessment_year,
        required=True,
        metavar="AY",
        help=(
            "Target AY whose snapshot is being scaffolded (e.g., "
            "'2027-28'). Must be strictly later than --from-ay. The "
            "target file _ay_YYYY_YY.py must NOT already exist "
            "unless --force is passed."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite the target snapshot file if it already exists. "
            "Without this flag the rollover refuses to clobber an "
            "existing file to protect any in-progress edits."
        ),
    )
    args = parser.parse_args(argv)

    try:
        result = scaffold_next_ay_snapshot(
            from_ay_start=args.from_ay_start,
            to_ay_start=args.to_ay_start,
            package_dir=PACKAGE_DIR,
            force=args.force,
        )
    except IbkrFaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(_render_summary(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
