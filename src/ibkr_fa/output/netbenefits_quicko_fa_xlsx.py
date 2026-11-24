"""Quicko "Other Broker" workbook for NetBenefits Schedule FA holdings."""

from __future__ import annotations

import lzma
import posixpath
import re
import unicodedata
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import ClassVar, Final, Self
from xml.sax.saxutils import escape
from zipfile import BadZipFile, ZipFile, ZipInfo

from openpyxl import load_workbook
from openpyxl.reader.strings import read_string_table
from openpyxl.utils.cell import column_index_from_string, coordinate_from_string
from openpyxl.utils.exceptions import InvalidFileException

from ibkr_fa.exceptions import IbkrFaError, errno_name
from ibkr_fa.models import FaA3Row
from ibkr_fa.output import register
from ibkr_fa.output._atomic import atomic_output_path
from ibkr_fa.output._xlsx_contract import (
    _assert_no_nul_in_xlsx_row,
    _assert_no_xlsx_formula_injection,
    _whole_rupee_cell,
)
from ibkr_fa.output.base import Artifact, Destination, OutputKey, RenderContext
from ibkr_fa.reference.country_codes import lookup_country
from ibkr_fa.xlsx_checks import RefuseOnWarning, require_cells_in_order

SHEET_NAME: Final[str] = "Shares & Securities"
QUICKO_TEMPLATE_FILENAME: Final[str] = "Personal+Assets+Foreign+Shares+And+Securities+Template.xlsx"
HEADERS: Final[tuple[str, ...]] = (
    "Description*",
    "Account Number*",
    "Country*",
    "Street",
    "Area",
    "City",
    "State",
    "Zip Code*",
    "Purchase Date*",
    "Purchase Value*",
    "Peak Value*",
    "Fair Market Value*",
    "Sale Value",
    "Dividend/Interest Income",
)
_WORKBOOK_XML: Final[str] = "xl/workbook.xml"
_WORKBOOK_RELS_XML: Final[str] = "xl/_rels/workbook.xml.rels"
_SHARED_STRINGS_XML: Final[str] = "xl/sharedStrings.xml"
_XML_NS: Final[dict[str, str]] = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_RELATIONSHIP_ID: Final[str] = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
)
_OPTIONAL_AMOUNT_START_INDEX: Final[int] = 12
_ILLEGAL_XML_CHAR: Final[re.Pattern[str]] = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_QUICKO_COUNTRY_FIRST_ROW: Final[int] = 32
_QUICKO_COUNTRY_LAST_ROW: Final[int] = 281
_ROW_TAG: Final[str] = f"{{{_XML_NS['x']}}}row"
_V_TAG: Final[str] = f"{{{_XML_NS['x']}}}v"
# The only forms the filled-sheet check accepts for a row's and a cell's ``r``.
_ROW_NUMBER: Final[re.Pattern[str]] = re.compile("[1-9][0-9]{0,6}")
_CELL_REFERENCE: Final[re.Pattern[str]] = re.compile("([A-Z]{1,3})([1-9][0-9]{0,6})")


class QuickoFaXlsxError(IbkrFaError):
    """A Quicko holding row cannot be emitted in an uploadable shape."""


@dataclass(slots=True)
class _SharedStrings:
    xml: str
    values: list[str]
    original_count: int = 0
    original_unique: int = 0
    references: int = 0
    added_unique: int = 0

    def index(self, value: str) -> int:
        self.references += 1
        try:
            return self.values.index(value)
        except ValueError:
            closing_tag = "</sst>"
            if closing_tag not in self.xml:
                raise QuickoFaXlsxError(
                    "Quicko template sharedStrings.xml has an unsupported root tag"
                ) from None
            index = len(self.values)
            self.values.append(value)
            self.xml = self.xml.replace(
                closing_tag,
                f"<si><t>{escape(value)}</t></si>{closing_tag}",
                1,
            )
            self.added_unique += 1
            return index

    def payload(self) -> bytes:
        updated = _set_root_attribute(
            self.xml,
            "count",
            self.original_count + self.references,
        )
        updated = _set_root_attribute(
            updated,
            "uniqueCount",
            self.original_unique + self.added_unique,
        )
        return updated.encode("utf-8")


def _address_cells(address: str) -> tuple[str, str, str, str]:
    """Split ``street, city, state`` while preserving longer street addresses."""

    parts = [part.strip() for part in address.split(",") if part.strip()]
    match parts:
        case []:
            return "", "", "", ""
        case [street]:
            return street, "", "", ""
        case [street, city]:
            return street, "", city, ""
    return ", ".join(parts[:-2]), "", parts[-2], parts[-1]


def _country_key(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value).casefold()
        if character.isalnum()
    )


def _row_cells(
    row: FaA3Row,
    *,
    row_index: int,
    account_number: str,
    quicko_countries: Mapping[str, str],
) -> list[object]:
    street, area, city, state = _address_cells(row.address_of_entity)
    portal_country = lookup_country(row.source_country_iso2).country_name_portal
    try:
        quicko_country = quicko_countries[_country_key(portal_country)]
    except KeyError:
        raise QuickoFaXlsxError(
            f"Quicko row {row_index}: the template's country list has no entry for the "
            "holding's country"
        ) from None
    filed = row.filed
    return [
        row.name_of_entity,
        account_number,
        quicko_country,
        street,
        area,
        city,
        state,
        row.zip_code,
        row.date_of_acquiring.strftime("%d/%m/%Y"),
        _whole_rupee_cell(filed.initial_value_inr, field="Quicko Purchase Value"),
        _whole_rupee_cell(filed.peak_value_inr, field="Quicko Peak Value"),
        _whole_rupee_cell(filed.closing_value_inr, field="Quicko Fair Market Value"),
        _whole_rupee_cell(filed.gross_proceeds_from_sale_inr, field="Quicko Sale Value"),
        _whole_rupee_cell(
            filed.gross_amount_paid_credited_inr, field="Quicko Dividend/Interest Income"
        ),
    ]


def _assert_xml_safe(cells: Sequence[object], *, row_index: int) -> None:
    for cell_index, cell in enumerate(cells):
        if not isinstance(cell, str):
            continue
        match = _ILLEGAL_XML_CHAR.search(cell)
        if match is not None:
            raise QuickoFaXlsxError(
                f"Quicko row {row_index} cell {cell_index} contains an illegal XML control "
                "character"
            )


def _validate_template(path: Path) -> dict[str, str]:
    """Check the template's sheets, headers and blank rows; return its country labels.

    Every warning raised from opening the workbook to the last read is recorded,
    never printed. Any warning except openpyxl's fixed
    :data:`~ibkr_fa.xlsx_checks.ALLOWED_WARNING` refuses the template as
    unreadable, in place of the labels or any other refusal (#1103).
    """
    with RefuseOnWarning(_unreadable_template(path)):
        return _read_template(path)


def _unreadable_template(path: Path) -> QuickoFaXlsxError:
    return QuickoFaXlsxError(
        f"cannot read Quicko template {path}: it is not a readable XLSX workbook"
    )


def _read_template(path: Path) -> dict[str, str]:
    """:func:`_validate_template` without its warning check."""
    workbook = None
    try:
        workbook = load_workbook(path, read_only=True, data_only=False)
        if workbook.sheetnames != ["Instructions", SHEET_NAME]:
            raise QuickoFaXlsxError(
                f"Quicko template must contain exactly the Instructions and {SHEET_NAME} sheets, "
                "in that order"
            )
        worksheet = workbook[SHEET_NAME]
        # Read every cell, not only the range the sheet's <dimension> tag declares,
        # and refuse a sheet whose stored order would hide cells from the read.
        worksheet.reset_dimensions()
        require_cells_in_order(worksheet)
        headers = tuple(next(worksheet.iter_rows(max_row=1, values_only=True), ()))
        if headers[: len(HEADERS)] != HEADERS or any(
            value is not None for value in headers[len(HEADERS) :]
        ):
            raise QuickoFaXlsxError("Quicko template Shares & Securities headers do not match")
        if any(
            value is not None
            for row in worksheet.iter_rows(min_row=2, values_only=True)
            for value in row
        ):
            raise QuickoFaXlsxError("Quicko template Shares & Securities sheet is not blank")
        return {
            _country_key(value): value
            for row in workbook["Instructions"].iter_rows(
                min_row=_QUICKO_COUNTRY_FIRST_ROW,
                max_row=_QUICKO_COUNTRY_LAST_ROW,
                min_col=2,
                max_col=2,
                values_only=True,
            )
            if isinstance((value := row[0]), str) and value
        }
    except OSError as exc:
        raise QuickoFaXlsxError(f"cannot read Quicko template {path} ({errno_name(exc)})") from None
    except (
        ValueError,
        BadZipFile,
        IndexError,
        KeyError,
        ET.ParseError,
        InvalidFileException,
        zlib.error,
        lzma.LZMAError,
    ):
        raise _unreadable_template(path) from None
    finally:
        if workbook is not None:
            workbook.close()


def _worksheet_xml_path(payloads: Mapping[str, bytes]) -> str:
    """Resolve the named sheet through workbook relationships."""

    try:
        workbook = ET.fromstring(payloads[_WORKBOOK_XML])
        relationships = ET.fromstring(payloads[_WORKBOOK_RELS_XML])
    except (KeyError, ET.ParseError):
        raise QuickoFaXlsxError("Quicko template workbook relationships are invalid") from None
    sheet = next(
        (
            node
            for node in workbook.findall(".//x:sheet", _XML_NS)
            if node.get("name") == SHEET_NAME
        ),
        None,
    )
    relationship_id = None if sheet is None else sheet.get(_RELATIONSHIP_ID)
    target = next(
        (node.get("Target") for node in relationships if node.get("Id") == relationship_id),
        None,
    )
    if target is None:
        raise QuickoFaXlsxError(f"Quicko template cannot resolve the {SHEET_NAME} worksheet")
    path = posixpath.normpath(posixpath.join("xl", target)).lstrip("/")
    if not path.startswith("xl/worksheets/") or path not in payloads:
        raise QuickoFaXlsxError(f"Quicko template has an invalid {SHEET_NAME} worksheet target")
    return path


def _tag_start(name: str, reference: str) -> str:
    """Pattern for the start of a ``<name>`` tag whose ``r`` attribute is ``reference``,
    wherever ``r`` sits among the tag's attributes (#1076)."""
    return rf'<{name}(?=\s)[^>]*\sr="{reference}"'


def _cell_style(sheet_xml: str, reference: str) -> str | None:
    tag = re.search(_tag_start("c", reference) + "[^>]*", sheet_xml)
    style = None if tag is None else re.search(r'\ss="(\d+)"', tag.group())
    return None if style is None else style.group(1)


def _replace_cell(sheet_xml: str, reference: str, replacement: str) -> str:
    pattern = _tag_start("c", reference) + "[^>]*?(?:/>|>.*?</c>)"
    cell_match = re.search(pattern, sheet_xml, flags=re.DOTALL)
    if cell_match is not None:
        return sheet_xml[: cell_match.start()] + replacement + sheet_xml[cell_match.end() :]
    column, row = coordinate_from_string(reference)
    row_number = str(row)
    empty_row = re.search(f"({_tag_start('row', row_number)}[^>]*)/>", sheet_xml)
    if empty_row is not None:
        expanded = f"{empty_row.group(1)}>{replacement}</row>"
        return sheet_xml[: empty_row.start()] + expanded + sheet_xml[empty_row.end() :]
    row_pattern = f"({_tag_start('row', row_number)}[^>]*?(?<!/)>)(.*?)(</row>)"
    match = re.search(row_pattern, sheet_xml, flags=re.DOTALL)
    if match is None:
        raise QuickoFaXlsxError(f"Quicko template is missing data row {row_number}")
    cells = match.group(2)
    target_column = column_index_from_string(column)
    for existing in re.finditer(_tag_start("c", f"([A-Z]+){row_number}"), cells):
        if column_index_from_string(existing.group(1)) > target_column:
            cells = cells[: existing.start()] + replacement + cells[existing.start() :]
            break
    else:
        cells += replacement
    return (
        sheet_xml[: match.start()]
        + match.group(1)
        + cells
        + match.group(3)
        + sheet_xml[match.end() :]
    )


def _set_root_attribute(xml: str, name: str, value: int) -> str:
    root_start = xml.find("<sst")
    root_end = xml.find(">", root_start)
    if root_start < 0 or root_end < 0:
        raise QuickoFaXlsxError("Quicko template sharedStrings.xml has no root element")
    opening = xml[root_start:root_end]
    pattern = rf'(\b{re.escape(name)}=")\d+(")'
    match = re.search(pattern, opening)
    if match is not None:
        opening = (
            opening[: match.start()]
            + match.group(1)
            + str(value)
            + match.group(2)
            + opening[match.end() :]
        )
    else:
        opening += f' {name}="{value}"'
    return xml[:root_start] + opening + xml[root_end:]


def _shared_strings(xml: str) -> _SharedStrings:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise QuickoFaXlsxError("Quicko template sharedStrings.xml is invalid") from None
    if root.tag != f"{{{_XML_NS['x']}}}sst" or "</sst>" not in xml:
        raise QuickoFaXlsxError("Quicko template sharedStrings.xml has an unsupported root tag")
    try:
        original_count = int(root.attrib.get("count", "0"))
        original_unique = int(root.attrib.get("uniqueCount", "0"))
    except ValueError:
        raise QuickoFaXlsxError("Quicko template sharedStrings.xml has invalid counts") from None
    return _SharedStrings(
        xml=xml,
        values=["".join(item.itertext()) for item in root.findall("x:si", _XML_NS)],
        original_count=original_count,
        original_unique=original_unique,
    )


def _render_cell(
    reference: str,
    *,
    style: str | None,
    value: str | int,
    shared: _SharedStrings,
) -> str:
    style_attr = "" if style is None else f' s="{style}"'
    if isinstance(value, str):
        return f'<c r="{reference}"{style_attr} t="s"><v>{shared.index(value)}</v></c>'
    return f'<c r="{reference}"{style_attr} t="n"><v>{int(value)}</v></c>'


def _populate_sheet(
    sheet_xml: str,
    rows: Sequence[FaA3Row],
    *,
    account_number: str,
    quicko_countries: Mapping[str, str],
    shared: _SharedStrings,
) -> tuple[str, dict[str, tuple[str | int, str | None]]]:
    """Fill ``rows`` into ``sheet_xml``; also return each written cell's value and style."""
    written: dict[str, tuple[str | int, str | None]] = {}
    for row_number, row in enumerate(rows, start=2):
        cells = _row_cells(
            row,
            row_index=row_number - 1,
            account_number=account_number,
            quicko_countries=quicko_countries,
        )
        _assert_no_nul_in_xlsx_row(cells, row_index=row_number - 1)
        _assert_no_xlsx_formula_injection(cells, row_index=row_number - 1)
        _assert_xml_safe(cells, row_index=row_number - 1)
        text_style = _cell_style(sheet_xml, f"B{row_number}")
        for offset, raw_value in enumerate(cells):
            value = None if offset >= _OPTIONAL_AMOUNT_START_INDEX and raw_value == 0 else raw_value
            if value in (None, ""):
                continue
            if not isinstance(value, (str, int)):
                raise QuickoFaXlsxError(
                    f"Quicko row {row_number - 1} contains an unsupported cell value"
                )
            column = chr(ord("A") + offset)
            reference = f"{column}{row_number}"
            style = text_style if column == "I" else _cell_style(sheet_xml, reference)
            replacement = _render_cell(reference, style=style, value=value, shared=shared)
            sheet_xml = _replace_cell(sheet_xml, reference, replacement)
            written[reference] = (value, style)
    return sheet_xml, written


def _holds(
    cell: ET.Element, value: str | int, style: str | None, shared_strings: Sequence[str]
) -> bool:
    """Whether ``cell`` is exactly what :func:`_render_cell` writes for ``value`` and ``style``."""
    attributes = {"r": cell.get("r"), "t": "s" if isinstance(value, str) else "n"}
    if style is not None:
        attributes["s"] = style
    if cell.attrib != attributes or [child.tag for child in cell] != [_V_TAG]:
        return False
    if cell.text or cell[0].attrib or len(cell[0]) or cell[0].tail:
        return False
    text = cell[0].text or ""
    if isinstance(value, int):
        return text == str(value)
    return text in {str(index) for index, string in enumerate(shared_strings) if string == value}


def _check_filled_sheet(
    sheet_payload: bytes,
    written: Mapping[str, tuple[str | int, str | None]],
    shared_strings: Sequence[str],
) -> None:
    """Refuse a filled sheet whose rows or cells are out of order, or that lost a written cell.

    Row and cell entries count ``<row>`` and ``<c>`` elements from 1 in file
    order, so a refusal can point at the header and unused rows too, and
    names nothing the sheet holds (#1076).
    """
    failed = "Quicko workbook check failed:"
    sheet = f"of the filled {SHEET_NAME} sheet"
    try:
        root = ET.fromstring(sheet_payload)
    except ET.ParseError:
        raise QuickoFaXlsxError(
            f"{failed} the filled {SHEET_NAME} sheet is not well-formed XML"
        ) from None
    cells: dict[str, ET.Element] = {}
    previous_row = 0
    for row_entry, row in enumerate(root.iter(_ROW_TAG), start=1):
        row_number = row.get("r", "")
        if _ROW_NUMBER.fullmatch(row_number) is None or int(row_number) <= previous_row:
            raise QuickoFaXlsxError(
                f"{failed} row entry {row_entry} {sheet} repeats an earlier row or is out of order"
            ) from None
        previous_row = int(row_number)
        previous_column = 0
        for cell_entry, cell in enumerate(row.findall("x:c", _XML_NS), start=1):
            reference = cell.get("r", "")
            match = _CELL_REFERENCE.fullmatch(reference)
            column = 0 if match is None else column_index_from_string(match.group(1))
            if match is None or match.group(2) != row_number or column <= previous_column:
                raise QuickoFaXlsxError(
                    f"{failed} cell entry {cell_entry} of row entry {row_entry} {sheet} repeats "
                    "a cell, belongs to another row, or is out of column order"
                ) from None
            previous_column = column
            cells[reference] = cell
    for reference, (value, style) in written.items():
        cell = cells.get(reference)
        if cell is None or not _holds(cell, value, style, shared_strings):
            raise QuickoFaXlsxError(
                f"{failed} cell {reference} {sheet} is missing, or holds a value or style "
                "other than the one written"
            ) from None


def _write_package(path: Path, infos: Sequence[ZipInfo], payloads: dict[str, bytes]) -> None:
    with atomic_output_path(path) as tmp_path, ZipFile(tmp_path, "w") as output:
        for info in infos:
            output.writestr(info, payloads[info.filename])


@dataclass(frozen=True)
class QuickoFaWorkbookInput:
    """What the Quicko Schedule FA workbook needs.

    Attributes:
        rows: The Table A3 rows, one per holding.
        account_number: The NetBenefits account the rows belong to.
        template_path: Quicko's downloaded template, which is filled in place.
    """

    rows: tuple[FaA3Row, ...]
    account_number: str
    template_path: Path


@register(OutputKey(Destination.QUICKO_IMPORT, Artifact.FA_A3_XLSX))
class QuickoFaXlsxWriter:
    """Fill Quicko's Schedule FA import template. Registered by key only: it is
    not a ``--format`` choice, and :meth:`from_context` reads nothing from ``ctx``."""

    format_id: ClassVar[str] = "quicko-fa-xlsx"

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:  # noqa: ARG003 — OutputWriter protocol
        """Build the writer; ``ctx`` is unused (see the class docstring)."""
        return cls()

    def write(self, value: QuickoFaWorkbookInput, path: Path) -> None:
        """Fill Quicko's downloaded template without rewriting its XLSX package."""

        if not value.rows:
            raise QuickoFaXlsxError("Quicko Schedule FA workbook requires at least one holding")
        quicko_countries = _validate_template(value.template_path)

        try:
            with ZipFile(value.template_path) as source:
                infos = source.infolist()
                payloads = {info.filename: source.read(info.filename) for info in infos}
            sheet_path = _worksheet_xml_path(payloads)
            sheet_xml = payloads[sheet_path].decode("utf-8")
            shared = _shared_strings(payloads[_SHARED_STRINGS_XML].decode("utf-8"))
        except OSError as exc:
            raise QuickoFaXlsxError(
                f"cannot read Quicko template {value.template_path} ({errno_name(exc)})"
            ) from None
        except (BadZipFile, KeyError, UnicodeError, zlib.error, lzma.LZMAError):
            raise QuickoFaXlsxError(
                f"Quicko template {value.template_path} package is invalid"
            ) from None

        sheet_xml, written = _populate_sheet(
            sheet_xml,
            value.rows,
            account_number=value.account_number,
            quicko_countries=quicko_countries,
            shared=shared,
        )
        payloads[sheet_path] = sheet_xml.encode("utf-8")
        try:
            payloads[_SHARED_STRINGS_XML] = shared.payload()
            strings = read_string_table(BytesIO(payloads[_SHARED_STRINGS_XML]))
        except (ET.ParseError, UnicodeError):
            # An unreadable table cannot resolve any written text cell.
            strings = []
        _check_filled_sheet(payloads[sheet_path], written, strings)
        _write_package(path, infos, payloads)


__all__ = [
    "HEADERS",
    "QUICKO_TEMPLATE_FILENAME",
    "SHEET_NAME",
    "QuickoFaWorkbookInput",
    "QuickoFaXlsxError",
    "QuickoFaXlsxWriter",
]
