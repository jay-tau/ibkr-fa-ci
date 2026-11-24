# ruff: noqa: RUF002
# Full-width Unicode homoglyphs (U+FF1D / U+FF0B / U+FF0D / U+FF20) appear
# deliberately in this module's docstrings AND in the trigger set — they
# ARE the OWASP-extended triggers we reject. RUF002 flags them as
# "ambiguous"; for a security-contract module that explicitly enumerates
# the homoglyphs, that warning fires on exactly the lines we need to be
# precise about. Per-file ignore.
"""Writer-level safety contract helpers (NUL + CSV-formula-injection).

The writer is the **last gate** before bytes hit disk, so per-row
defensive assertions here cover all upstream row sources (parser,
compute, registry consumer) including any not yet imagined. Violations
raise :class:`WriterContractError`, the CLI's
:class:`~ibkr_fa.exceptions.IbkrFaError` mapper renders the message,
and the user gets a deterministic failure rather than a silent
dangerous CSV.

CSV writers write every row through :func:`write_checked_rows`, which
runs two contract assertions on it before ``writer.writerow(row)``:

1. :func:`_assert_no_nul_in_row` — rejects any cell containing
   ``"\\x00"``. ``csv.writer`` (and downstream ``csv.reader``) accept
   NUL as data silently, so a NUL-tainted cell would pass width checks
   without warning — a privileged-process / supply-chain attack vector
   if the upstream parser is ever bypassed.

2. :func:`_assert_no_csv_formula_injection` — rejects any cell whose
   first character (raw OR after :py:meth:`str.lstrip`) is a known
   CSV-formula trigger. Trigger set is the OWASP-canonical list at
   https://owasp.org/www-community/attacks/CSV_Injection: the ASCII
   spreadsheet-execution set ``= + - @``, plus the C0 whitespace
   triggers ``\\t \\v \\r \\n`` (vertical-tab included as a
   defense-in-depth extension beyond OWASP's literal text — some
   CSV parsers normalise ``\\v`` to a field separator), plus the
   Unicode full-width homoglyphs ``＝ ＋ － ＠`` (the full-width
   characters may be interpreted as formulas in some locales, e.g.
   Japanese-locale Excel / LibreOffice installs per OWASP). Triggers
   fire Excel formula execution on file open (e.g.,
   ``=cmd|'/c calc'!A0`` classic CVE-2014-3524-style payload).

Helpers thread ``row_index`` (zero-based; header = 0) through so the
resulting :class:`WriterContractError` deterministically locates the
offending row across detail / IN-source / excluded / audit / TOTAL
rows.

The double-check (raw ``cell[0]`` AND ``cell.lstrip()[0]``) is a
**defense-in-depth extension beyond OWASP's literal text** — OWASP's
page warns about field-separator / quote tricks to start new cells
but does not itself prescribe a whitespace strip. The post-strip
check catches payloads padded with leading spaces; the raw check
catches standalone ``\\t``/``\\v``/``\\r``/``\\n`` cells whose
``cell.lstrip()`` is empty (where the lstrip-path would otherwise
short-circuit silently).

**Empty-string guards** are essential to avoid :class:`IndexError`:
if ``cell == ""`` the entire formula-trigger check is skipped (no
first char to inspect); if ``cell.lstrip() == ""`` only the lstrip-
path is skipped while the raw-``cell[0]`` path still runs — which
catches the leading-``\\t``/``\\v``/``\\r``/``\\n`` case above. Cells
that are entirely ASCII spaces (0x20 only) pass — no execution risk
in that shape.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal, Protocol

from ibkr_fa.exceptions import IbkrFaError

_ASCII_FORMULA_TRIGGERS: Final[frozenset[str]] = frozenset({"=", "+", "-", "@"})
_C0_WHITESPACE_TRIGGERS: Final[frozenset[str]] = frozenset({"\t", "\v", "\r", "\n"})
_FULLWIDTH_FORMULA_TRIGGERS: Final[frozenset[str]] = frozenset(
    {"\uff1d", "\uff0b", "\uff0d", "\uff20"}
)
_FORMULA_TRIGGERS: Final[frozenset[str]] = (
    _ASCII_FORMULA_TRIGGERS | _C0_WHITESPACE_TRIGGERS | _FULLWIDTH_FORMULA_TRIGGERS
)


def _formula_trigger(cell: str) -> str | None:
    """The :data:`_FORMULA_TRIGGERS` character that makes ``cell`` unsafe, or ``None``.

    ``cell`` must not be empty. Its raw first character is checked first, then
    the first character after :py:meth:`str.lstrip`, so a cell of spaces alone
    passes. The CSV and XLSX formula checks both decide each cell here.
    """
    if cell[0] in _FORMULA_TRIGGERS:
        return cell[0]
    stripped = cell.lstrip()
    if stripped == "":
        return None
    if stripped[0] in _FORMULA_TRIGGERS:
        return stripped[0]
    return None


_NUL: Final[str] = "\x00"

# Excel's cell limit: 32,767 UTF-16 code units ("Excel specifications and limits",
# Microsoft support; [MS-XLSB] XLWideString). openpyxl silently cuts strings
# over 32,767 Python code points (#1323).
_XLSX_CELL_TEXT_LIMIT: Final[int] = 32_767


@dataclass(frozen=True, slots=True)
class WriterContractError(IbkrFaError):
    """Raised when a row about to be written violates the writer's safety contract.

    Subclasses :class:`~ibkr_fa.exceptions.IbkrFaError` and follows the
    same ``@dataclass(frozen=True, slots=True)`` shape as the rest of
    the exception hierarchy (see :class:`~ibkr_fa.exceptions.ParseError`
    / :class:`~ibkr_fa.exceptions.MissingFxRateError` /
    :class:`~ibkr_fa.exceptions.InvariantError`).

    Args:
        violation: Discriminator naming which contract was violated.
            Typed as ``Literal[...]`` so ``ty`` statically rejects typos
            like ``"nullbyte"`` at construction call sites; the
            :meth:`__str__` defensive branch handles bypassed-type-system
            constructions.
        row_index: Zero-based row index within the output CSV (header =
            0, first detail row = 1, etc.) so the user can
            deterministically locate the failing row across detail /
            IN-source / excluded / audit / TOTAL rows. Matches the
            :class:`~ibkr_fa.exceptions.ParseError` ``row_index``
            precedent.
        cell_index: Zero-based index of the offending cell within the
            row (so the user can locate the column).
        trigger: For ``"formula_trigger"``, the literal trigger
            character (e.g., ``"="``, ``"\\t"``, ``"\\uff1d"``). Empty
            string for every other violation.

    The violations: ``"nul_byte"`` (a cell holds a NUL byte),
    ``"formula_trigger"`` (a cell starts with a formula trigger),
    ``"unrepresentable_number"`` (an XLSX number cell would read back as
    infinite, empty or zero, #832), ``"text_too_long"`` (an XLSX text cell
    is longer than :data:`_XLSX_CELL_TEXT_LIMIT`, #1323) and
    ``"illegal_character"`` (an XLSX text cell holds a control character a
    workbook cell can't store, #1357).

    The message names the row, the cell index and the violation, never the
    cell's value: a cell can hold a name, an account number or an amount
    (#1048).
    """

    violation: Literal[
        "nul_byte",
        "formula_trigger",
        "unrepresentable_number",
        "text_too_long",
        "illegal_character",
    ]
    row_index: int
    cell_index: int
    trigger: str

    def __str__(self) -> str:
        where = f"WriterContractError: row {self.row_index} cell at index {self.cell_index}"
        if self.violation == "nul_byte":
            return f"{where} contains a NUL byte"
        if self.violation == "formula_trigger":
            return f"{where} starts with formula-trigger character {self.trigger!r}"
        if self.violation == "unrepresentable_number":
            return (
                f"{where} holds a number the workbook cannot store: it would read back as"
                " infinite, empty or zero"
            )
        if self.violation == "text_too_long":
            return (
                f"{where} is longer than a workbook cell can hold"
                f" ({_XLSX_CELL_TEXT_LIMIT:,} characters)"
            )
        if self.violation == "illegal_character":
            return f"{where} holds a control character a workbook cell can't store"
        return (
            "WriterContractError: unknown violation kind at row"
            f" {self.row_index} cell {self.cell_index}"
        )


def _assert_no_nul_in_row(row: Sequence[str], *, row_index: int) -> None:
    """Assert that no cell in ``row`` contains a NUL byte.

    Args:
        row: The CSV row about to be written. Each cell is examined in
            order; the FIRST cell containing ``"\\x00"`` triggers a
            :class:`WriterContractError`.
        row_index: Zero-based row index threaded onto the exception so
            the user can deterministically locate the failing row.

    Raises:
        WriterContractError: With ``violation="nul_byte"``,
            ``cell_index`` pointing to the offending cell and empty
            ``trigger``.
    """
    for cell_index, cell in enumerate(row):
        if _NUL in cell:
            raise WriterContractError(
                violation="nul_byte",
                row_index=row_index,
                cell_index=cell_index,
                trigger="",
            )


def _assert_no_csv_formula_injection(row: Sequence[str], *, row_index: int) -> None:
    """Assert that no cell in ``row`` starts with a CSV-formula trigger.

    Each cell is checked **twice** — once on the raw first char and
    once on the post-:py:meth:`str.lstrip` first char — against the
    trigger set ``{= + - @ \\t \\v \\r \\n ＝ ＋ － ＠}`` (OWASP-canonical
    list with full-width homoglyph + C0-whitespace extensions; the
    full ASCII spaces are tolerated). Empty-string guards skip the
    relevant path safely.

    Args:
        row: The CSV row about to be written. Each cell is examined in
            order; the FIRST cell with a trigger in either position
            raises.
        row_index: Zero-based row index threaded onto the exception so
            the user can deterministically locate the failing row.

    Raises:
        WriterContractError: With ``violation="formula_trigger"``,
            ``cell_index`` pointing to the offending cell and ``trigger``
            set to the literal one-char trigger that fired.
    """
    for cell_index, cell in enumerate(row):
        if cell == "":
            continue
        trigger = _formula_trigger(cell)
        if trigger is not None:
            raise WriterContractError(
                violation="formula_trigger",
                row_index=row_index,
                cell_index=cell_index,
                trigger=trigger,
            )


def csv_safe_signed(value: Decimal) -> str:
    """Render a signed Decimal safely for CSV output.

    For non-negative values, returns ``str(value)`` unchanged.

    For negative values, prepends the OWASP-canonical single-quote
    escape character so Excel / LibreOffice / Numbers treat the cell as
    a literal string rather than a formula whose leading ``-`` would
    trigger :func:`_assert_no_csv_formula_injection`.

    The apostrophe prefix is invisible when the CSV is opened in Excel
    (it strips the leading ``'`` while preserving the rendered text);
    raw CSV readers see the literal apostrophe. Tests should assert
    against the literal string ``"'-1500"`` for negative cells.
    """
    rendered = str(value)
    if rendered.startswith("-"):
        return "'" + rendered
    return rendered


class _RowWriter(Protocol):
    def writerow(self, row: Iterable[str], /) -> object: ...


def write_checked_rows(
    writer: _RowWriter,
    rows: Iterable[Sequence[object]],
    *,
    start_index: int,
    formula_columns: int | None = None,
) -> None:
    """Check each of ``rows`` against the writer contract, then write it.

    Cells are rendered the way :func:`csv.writer` renders them (``None`` →
    ``""``, anything else → :class:`str`), both assertions run on the
    rendered row, and ``writer.writerow`` is called once per row.

    Args:
        writer: The :func:`csv.writer` to write to.
        rows: The rows to write, in order.
        start_index: ``row_index`` of the first row in ``rows``.
        formula_columns: If set, limit the formula-trigger check to the
            first ``formula_columns`` cells. The NUL check always covers
            every cell.

    Raises:
        WriterContractError: On the first row that violates either
            assertion; nothing from that row is written.
    """
    for row_index, row in enumerate(rows, start=start_index):
        cells = ["" if cell is None else str(cell) for cell in row]
        _assert_no_nul_in_row(cells, row_index=row_index)
        _assert_no_csv_formula_injection(cells[:formula_columns], row_index=row_index)
        writer.writerow(cells)
