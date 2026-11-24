"""Parse the exact observed Fidelity CMA 2025 Form 1042-S packet."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, fields, replace
from decimal import Decimal
from pathlib import Path
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.models.form_1042s import (
    Form1042SAmount,
    Form1042SAmountKind,
    Form1042SCopy,
    Form1042SCopyType,
)
from ibkr_fa.parser._form_1042s import (
    BOX_10_REFUSAL,
    COPY_DESIGNATION_REFUSAL,
    copy_designation,
    reproduces_box_10,
)
from ibkr_fa.parser._pdf import pdftotext_layout

_SECTION: Final[str] = "Fidelity CMA Form 1042-S packet"
_FORM_YEAR: Final[int] = 2025
_FORM_PAGE_NUMBERS: Final[tuple[int, ...]] = tuple(range(3, 20, 2))
_PACKET_PAGE_COUNT: Final[int] = 20
_PREFACE_PAGE_COUNT: Final[int] = 2
_RECORD_COUNT: Final[int] = 3
_WHOLE_DOLLARS: Final = re.compile(r"(?:0|[1-9][0-9]{0,2}(?:,[0-9]{3})+|[1-9][0-9]*)\.00")
_RATE: Final = re.compile(r"(?:0|[1-9][0-9]?)\.[0-9]{2}")
_CODE: Final = re.compile(r"[0-9]{2}")
_UFI_DIGITS: Final[int] = 10
_MAX_DOLLARS: Final[int] = 99_999_999_999_999
_BLANK: Final[Form1042SAmount] = Form1042SAmount(Form1042SAmountKind.BLANK, None)

# In the 34 lines after the UFI line, Boxes 1-11 end before this column;
# identity and condition-box fields sit to its right.
_LEFT_END: Final[int] = 116
# Lines are counted from the UNIQUE FORM IDENTIFIER line. The amendment
# area is that line after the label, plus these columns on the lines either
# side of it; copies C and D print their recipient instructions after it.
_AMENDMENT_COLUMNS: Final[slice] = slice(_LEFT_END, 195)
_SPACED_UFI: Final[str] = rf"(?:[0-9] ){{{_UFI_DIGITS - 1}}}[0-9]"
_HEADER_TOP: Final[tuple[str, ...]] = (
    re.escape("Form 1042-S Foreign Person\u2019s U.S. Source Income Subject to Withholding"),
    re.escape(
        "Go to www.irs.gov/Form1042S for instructions and the latest information."
        f" {_FORM_YEAR} OMB No. 1545-0096"
    ),
)
# Whitespace-normalised header lines above the UFI line, as patterns. Copy C
# prints its UFI one line higher than B and D; that copy of it isn't read.
_HEADER: Final[dict[Form1042SCopyType, tuple[str, ...]]] = {
    Form1042SCopyType.B: (
        *_HEADER_TOP,
        "",
        "COPY B",
        "Department of the Treasury for Recipient",
        "Internal Revenue Service",
    ),
    Form1042SCopyType.C: (
        *_HEADER_TOP,
        "COPY C for",
        "Department of the Treasury",
        f"{_SPACED_UFI} Recipient Attach to any",
    ),
    Form1042SCopyType.D: (
        *_HEADER_TOP,
        "",
        "COPY D for",
        "Department of the Treasury Recipient Attach to any",
        "Internal Revenue Service",
    ),
}
_AMENDMENT_TAIL: Final[dict[Form1042SCopyType, str]] = {
    Form1042SCopyType.B: "AMENDED AMENDMENT NO.",
    Form1042SCopyType.C: "AMENDED AMENDMENT NO. Federal tax return you file",
    Form1042SCopyType.D: "AMENDED AMENDMENT NO. state tax return you file",
}
_BOX3_LABELS: Final[str] = (
    "1 Income       2 Gross income 3 Chapter indicator. Enter \u201c3\u201d or \u201c4\u201d"
)
_BOX2_COLUMN: Final[int] = _BOX3_LABELS.index("2 Gross income")
_BOX11_LABEL: Final[str] = (
    "11 Tax paid by withholding agent (amounts not withheld) (see instructions)"
)
# Left-column text of every line from the UFI line + 1 to Box 11's next
# line, as (owning box, exact text); ``None`` marks a line whose values are
# read by ``_parse_left_column``. Boxes 7b-7d print unchecked here.
_LEFT_COLUMN: Final[tuple[tuple[str, str | None], ...]] = (
    ("Box 1", ""),
    ("Box 3", None),
    ("Box 1", "  code"),
    ("Box 2", None),
    ("Box 3b", ""),
    ("Box 3b", None),
    ("Box 5", None),
    ("Box 5", ""),
    ("Box 5", ""),
    ("Box 6", None),
    ("Box 6", ""),
    ("Box 7a", None),
    ("Box 7b", ""),
    ("Box 7b", "7b Check if federal tax withheld was not deposited with the IRS because"),
    ("Box 7b", "   escrow procedures were applied (see instructions)" + " ." * 19),
    ("Box 7b", ""),
    ("Box 7c", "7c Check if withholding occurred in subsequent year with respect to a"),
    ("Box 7c", "   partnership interest" + " ." * 44),
    ("Box 7d", "7d Check if you are a qualified intermediary,withholding foreign"),
    ("Box 7d", "   partnership, or withholding foreign trust revising its reporting on Form"),
    ("Box 7d", "   1042-S to report to a specific recipient" + " ." * 28),
    ("Box 7d", ""),
    ("Box 8", None),
    ("Box 8", ""),
    (
        "Box 9",
        "9 Overwithheld tax repaid to recipient pursuant to adjustment"
        " procedures(see instructions)",
    ),
    ("Box 9", ""),
    ("Box 9", ""),
    ("Box 9", None),
    ("Box 9", ""),
    ("Box 10", "10 Total withholding credit (combine boxes 7a, 8, and 9)"),
    ("Box 10", None),
    ("Box 10", ""),
    ("Box 11", None),
    ("Box 11", None),
)
# Right-column text, whitespace-normalised, of the lines around the Box 7b-7d
# check boxes and the Box 15 pro-rata check box, keyed by line offset.
_RIGHT_COLUMN: Final[dict[int, tuple[str, str]]] = {
    13: ("Box 7b", ""),
    14: ("Box 7b", ""),
    15: ("Box 7b", "13k Recipient\u2019s account number"),
    17: ("Box 7c", ""),
    18: ("Box 7c", "13l Recipient\u2019s date of birth (YYYYMMDD)"),
    20: ("Box 7d", ""),
    21: ("Box 7d", ""),
    22: ("Box 7d", "14a Primary withholding agent\u2019s name (if applicable)"),
    25: ("Box 15", "14b Primary withholding agent\u2019s EIN"),
    26: ("Box 15", "15 Check if pro-rata basis reporting"),
    27: ("Box 15", ""),
}
_BOX_LABELS: Final[tuple[tuple[str, str], ...]] = (
    ("Box 1", "1 Income"),
    ("Box 2", "Gross income"),
    ("Box 3", "3 Chapter indicator"),
    ("Box 3a", "3a Exemption code"),
    ("Box 3b", "3b Tax rate"),
    ("Box 4a", "4a Exemption code"),
    ("Box 4b", "4b Tax rate"),
    ("Box 5", "Withholding allowance"),
    ("Box 6", "Net income"),
    ("Box 7a", "Federal tax withheld"),
    ("Box 7b", "7b Check if"),
    ("Box 7c", "7c Check if"),
    ("Box 7d", "7d Check if"),
    ("Box 8", "Tax withheld by other agents"),
    ("Box 9", "Overwithheld tax repaid"),
    ("Box 10", "Total withholding credit"),
    ("Box 11", "Tax paid by withholding agent"),
    _RIGHT_COLUMN[26],
)


@dataclass(frozen=True, slots=True, repr=False)
class Form1042SRecord:
    record_key: str
    form: Form1042SCopy


@dataclass(frozen=True, slots=True, repr=False)
class Form1042SPacket:
    tax_year: int
    account_id: str
    account_holder: str
    recipient_country_code: str
    withholding_agent_name: str
    withholding_agent_ein: str
    records: tuple[Form1042SRecord, ...]


@dataclass(frozen=True, slots=True, repr=False)
class _IdentityFields:
    account_id: str
    account_holder: str
    recipient_country_code: str
    withholding_agent_name: str
    withholding_agent_ein: str


@dataclass(frozen=True, slots=True, repr=False)
class _FormCopy:
    form: Form1042SCopy
    identity: _IdentityFields

    def fingerprint(self) -> tuple[object, ...]:
        form = self.form
        if form.tax_paid_by_agent.kind is Form1042SAmountKind.BLANK:
            form = replace(form, tax_paid_by_agent=_dollars(0))
        return (
            self.identity,
            *(
                getattr(form, field.name)
                for field in fields(form)
                if field.name not in {"copy_type", "unique_form_identifier"}
            ),
        )


@dataclass(frozen=True, slots=True, repr=False)
class _LeftColumn:
    """Boxes 1-11 of one copy, each read whole from its fixed place."""

    income_code: str
    gross_income: int
    chapter3_exemption_code: str
    chapter4_exemption_code: str
    chapter3_tax_rate_percent: Decimal
    chapter4_tax_rate_percent: Decimal
    withholding_allowance: int
    net_income: int
    federal_tax_withheld: int
    tax_withheld_other_agents: int
    overwithheld_tax_repaid: int
    total_withholding_credit: int
    tax_paid_by_agent: Form1042SAmount


def _fail(message: str) -> ParseError:
    return ParseError(
        section=_SECTION,
        row_index=0,
        column=None,
        message=message,
        raw_value="",
    )


def _changed(box: str) -> ParseError:
    return _fail(f"Form 1042-S {box} area is marked or changed")


def _dollars(value: int) -> Form1042SAmount:
    return Form1042SAmount(Form1042SAmountKind.AMOUNT, Decimal(value))


def _whole_dollars(area: str, box: str) -> int:
    """Read ``area`` as exactly one printed whole-dollar amount."""
    text = area.strip()
    if (
        _WHOLE_DOLLARS.fullmatch(text) is None
        or (value := int(text.removesuffix(".00").replace(",", ""))) > _MAX_DOLLARS
    ):
        raise _fail(f"Form 1042-S {box} must contain one bounded whole-dollar amount")
    return value


def _rate(area: str, box: str) -> Decimal:
    text = area.strip()
    if _RATE.fullmatch(text) is None:
        raise _fail(f"Form 1042-S {box} must contain one tax rate")
    return Decimal(text)


def _code(area: str, box: str) -> str:
    text = area.strip()
    if _CODE.fullmatch(text) is None:
        raise _fail(f"Form 1042-S {box} must contain one two-digit code")
    return text


def _after_label(text: str, label: str, box: str) -> str:
    if not text.startswith(label):
        raise _changed(box)
    return text[len(label) :]


def _split_at_label(text: str, label: str, box: str) -> tuple[str, str]:
    before, found, after = text.partition(label)
    if not found or label in after:
        raise _changed(box)
    return before, after


def _next_nonblank(lines: list[str], label: str) -> str:
    indexes = [index for index, line in enumerate(lines) if label in line]
    if len(indexes) != 1:
        raise _fail(f"Form 1042-S {label} field is missing or duplicated")
    for line in lines[indexes[0] + 1 :]:
        if line.strip():
            return line
    raise _fail(f"Form 1042-S {label} value is missing")


def _parse_copy_header(page: str, lines: list[str]) -> tuple[Form1042SCopyType, str | None, int]:
    copy_type = copy_designation(page, "BCD")
    if copy_type is None:
        raise _fail(COPY_DESIGNATION_REFUSAL)
    year_bottom = re.findall(r"Form 1042-S[ \t]*\(([0-9]{4})\)", page)
    if year_bottom != [str(_FORM_YEAR)]:
        raise _fail("unexpected Form 1042-S version or tax year")

    ufi_indexes = [index for index, line in enumerate(lines) if "UNIQUE FORM IDENTIFIER" in line]
    if len(ufi_indexes) != 1:
        raise _fail("Form 1042-S UFI header is missing or duplicated")
    ufi_index = ufi_indexes[0]
    if len(lines) <= ufi_index + len(_LEFT_COLUMN):
        raise _fail("Form 1042-S page is truncated")
    header = _HEADER[copy_type]
    if ufi_index != len(header) or any(
        re.fullmatch(pattern, " ".join(line.split())) is None
        for pattern, line in zip(header, lines, strict=False)
    ):
        raise _fail("Form 1042-S header is marked or changed")

    prefix, _label, tail = (
        " ".join(part.split()) for part in lines[ufi_index].partition("UNIQUE FORM IDENTIFIER")
    )
    if tail != _AMENDMENT_TAIL[copy_type] or any(
        line[_AMENDMENT_COLUMNS].strip() for line in (lines[ufi_index - 1], lines[ufi_index + 1])
    ):
        raise _fail("amended Form 1042-S copies are unsupported")
    if copy_type is Form1042SCopyType.C:
        if prefix != "Internal Revenue Service":
            raise _fail("Form 1042-S header is marked or changed")
        return copy_type, None, ufi_index
    if re.fullmatch(_SPACED_UFI, prefix) is None:
        raise _fail("Copy B/D UFI is missing or malformed")
    return copy_type, prefix.replace(" ", ""), ufi_index


def _left_column_text(lines: list[str], ufi_index: int) -> dict[int, str]:
    """Return each line's left-column text by offset, checking chrome and check boxes."""
    texts: dict[int, str] = {}
    for offset, (box, chrome) in enumerate(_LEFT_COLUMN, start=1):
        line = lines[ufi_index + offset]
        if len(line) > _LEFT_END and " " not in line[_LEFT_END - 1 : _LEFT_END + 1]:
            raise _changed(box)
        texts[offset] = line[:_LEFT_END].rstrip()
        if chrome is not None and texts[offset] != chrome:
            raise _changed(box)
        if offset in _RIGHT_COLUMN:
            right_box, right_chrome = _RIGHT_COLUMN[offset]
            if " ".join(line[_LEFT_END:].split()) != right_chrome:
                raise _changed(right_box)
    return texts


def _parse_left_column(lines: list[str], ufi_index: int) -> _LeftColumn:
    text = _left_column_text(lines, ufi_index)
    if _after_label(text[2], _BOX3_LABELS, "Box 3").strip() != "3":
        raise _fail("Form 1042-S Box 3 chapter indicator must be 3")

    # Boxes 1, 2, 3a and 4a share a line, as do Boxes 3b and 4b. pdftotext
    # moves a label that follows a wider value, so each value area runs
    # between the labels wherever they print. Boxes 1 and 2 split under the
    # Box 2 label on the line above.
    boxes_1_2, codes = _split_at_label(text[4], "3a Exemption code", "Box 3a")
    box3a, box4a = _split_at_label(codes, "4a Exemption code", "Box 4a")
    before_rates, rates = _split_at_label(text[6], "3b Tax rate", "Box 3b")
    if before_rates.strip():
        raise _changed("Box 3b")
    box3b, box4b = _split_at_label(rates, "4b Tax rate", "Box 4b")

    box9 = re.fullmatch(r" *\((?P<value>[^()]*)\)", text[28])
    if box9 is None:
        raise _fail("Form 1042-S Box 9 must contain one bounded whole-dollar amount")
    box11_areas = [
        area for area in (_after_label(text[33], _BOX11_LABEL, "Box 11"), text[34]) if area.strip()
    ]
    if len(box11_areas) > 1:
        raise _fail("Form 1042-S Box 11 must contain one bounded whole-dollar amount")

    left = _LeftColumn(
        income_code=_code(boxes_1_2[:_BOX2_COLUMN], "Box 1"),
        gross_income=_whole_dollars(boxes_1_2[_BOX2_COLUMN:], "Box 2"),
        chapter3_exemption_code=_code(box3a, "Box 3a"),
        chapter4_exemption_code=_code(box4a, "Box 4a"),
        chapter3_tax_rate_percent=_rate(box3b, "Box 3b"),
        chapter4_tax_rate_percent=_rate(box4b, "Box 4b"),
        withholding_allowance=_whole_dollars(
            _after_label(text[7], "5 Withholding allowance", "Box 5"), "Box 5"
        ),
        net_income=_whole_dollars(_after_label(text[10], "6 Net income", "Box 6"), "Box 6"),
        federal_tax_withheld=_whole_dollars(
            _after_label(text[12], "7a Federal tax withheld", "Box 7a"), "Box 7a"
        ),
        tax_withheld_other_agents=_whole_dollars(
            _after_label(text[23], "8 Tax withheld by other agents", "Box 8"), "Box 8"
        ),
        overwithheld_tax_repaid=_whole_dollars(box9.group("value"), "Box 9"),
        total_withholding_credit=_whole_dollars(text[31], "Box 10"),
        tax_paid_by_agent=(
            _dollars(_whole_dollars(box11_areas[0], "Box 11")) if box11_areas else _BLANK
        ),
    )
    # Validate value areas first so stray labels still name the affected box.
    for box, label in _BOX_LABELS:
        pattern = rf"(?:{box.removeprefix('Box ')}\s+)?{re.escape(label)}"
        if (
            sum(
                box == "Box 15" or match.start() < _LEFT_END
                for line in lines
                for match in re.finditer(pattern, line)
            )
            > 1
        ):
            raise _fail(f"Form 1042-S {box} label is duplicated")
    # Both rates share one row; an unnumbered duplicate must still refuse.
    if (
        sum(
            any(match.start() < _LEFT_END for match in re.finditer(r"(?:[34]b\s+)?Tax rate", line))
            for line in lines
        )
        > 1
    ):
        raise _fail("Form 1042-S Boxes 3b/4b tax-rate row is duplicated")
    if not reproduces_box_10(
        left.federal_tax_withheld,
        left.tax_withheld_other_agents,
        left.overwithheld_tax_repaid,
        left.total_withholding_credit,
    ):
        raise _fail(BOX_10_REFUSAL)
    return left


def _parse_identity_fields(lines: list[str]) -> _IdentityFields:
    account = _next_nonblank(lines, "13k Recipient").strip()
    if re.fullmatch(r"[A-Z][0-9]{2}-[0-9]{6}", account) is None:
        raise _fail("Form 1042-S recipient account is missing or malformed")
    recipient_line = _next_nonblank(lines, "13a Recipient")
    recipient_match = re.match(
        r"^[ \t]*(?P<name>.+?)[ \t]{2,}(?P<country>[A-Z]{2})[ \t]*$",
        recipient_line,
    )
    if recipient_match is None:
        raise _fail("Form 1042-S recipient identity row is malformed")

    agent_line = _next_nonblank(lines, "12a Withholding agent")
    agent_match = re.match(
        r"^[ \t]*(?P<ein>[0-9]{2}-[0-9]{7})[ \t]+"
        r"(?P<chapter3>[0-9]{2})[ \t]+(?P<chapter4>[0-9]{2})[ \t]*$",
        agent_line,
    )
    if agent_match is None:
        raise _fail("Form 1042-S withholding-agent status row is malformed")
    agent_name = _next_nonblank(lines, "12d Withholding agent").strip()
    if agent_name != "NATIONAL FINANCIAL SERVICES LLC":
        raise _fail("unexpected Form 1042-S withholding agent")
    return _IdentityFields(
        account_id=account,
        account_holder=recipient_match.group("name").strip(),
        recipient_country_code=recipient_match.group("country"),
        withholding_agent_name=agent_name,
        withholding_agent_ein=agent_match.group("ein"),
    )


def _parse_copy(page: str) -> _FormCopy:
    lines = page.splitlines()
    copy_type, unique_form_identifier, ufi_index = _parse_copy_header(page, lines)
    left = _parse_left_column(lines, ufi_index)
    identity = _parse_identity_fields(lines)

    # The observed packet prints Box 3 as "3" and leaves Boxes 7b-7d and 15
    # unchecked; _parse_left_column refuses any other content there.
    return _FormCopy(
        form=Form1042SCopy(
            copy_type=copy_type,
            unique_form_identifier=unique_form_identifier,
            tax_year=_FORM_YEAR,
            amended=False,
            amendment_number=None,
            income_code=left.income_code,
            gross_income=_dollars(left.gross_income),
            chapter_indicator="3",
            chapter3_exemption_code=left.chapter3_exemption_code,
            chapter3_tax_rate_percent=left.chapter3_tax_rate_percent,
            chapter4_exemption_code=left.chapter4_exemption_code,
            chapter4_tax_rate_percent=left.chapter4_tax_rate_percent,
            withholding_allowance=_dollars(left.withholding_allowance),
            net_income=_dollars(left.net_income),
            federal_tax_withheld=_dollars(left.federal_tax_withheld),
            federal_tax_not_deposited=False,
            subsequent_year_withholding=False,
            qualified_intermediary=False,
            tax_withheld_other_agents=_dollars(left.tax_withheld_other_agents),
            overwithheld_tax_repaid=_dollars(left.overwithheld_tax_repaid),
            total_withholding_credit=_dollars(left.total_withholding_credit),
            tax_paid_by_agent=left.tax_paid_by_agent,
            pro_rata_reporting=False,
        ),
        identity=identity,
    )


def _record_from_anchor(anchor: _FormCopy) -> Form1042SRecord:
    rate = f"{anchor.form.chapter3_tax_rate_percent:.2f}"
    return Form1042SRecord(record_key=f"{anchor.form.income_code}:{rate}", form=anchor.form)


def _packet_form_pages(text: str) -> tuple[str, ...]:
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    if len(pages) != _PACKET_PAGE_COUNT:
        raise _fail("unexpected Form 1042-S packet page count")
    if not pages[0].strip() or not pages[1].strip():
        raise _fail("Form 1042-S packet preface pages are missing")
    expected_indexes = {page - 1 for page in _FORM_PAGE_NUMBERS}
    for index, page in enumerate(pages):
        if index in expected_indexes:
            if "Form 1042-S" not in page:
                raise _fail("Form 1042-S packet page sequence is malformed")
        elif index >= _PREFACE_PAGE_COUNT and page.strip():
            raise _fail("unexpected text-bearing page in Form 1042-S packet")
    return tuple(pages[index] for index in sorted(expected_indexes))


def _canonical_copies(copies: tuple[_FormCopy, ...]) -> tuple[_FormCopy, ...]:
    if (
        tuple(copy.form.copy_type for copy in copies)
        != (
            Form1042SCopyType.B,
            Form1042SCopyType.C,
            Form1042SCopyType.D,
        )
        * _RECORD_COUNT
    ):
        raise _fail("unexpected Form 1042-S B/C/D copy sequence")

    anchors: dict[str, list[_FormCopy]] = defaultdict(list)
    copy_cs: list[_FormCopy] = []
    for copy in copies:
        if copy.form.copy_type is Form1042SCopyType.C:
            copy_cs.append(copy)
        else:
            assert copy.form.unique_form_identifier is not None
            anchors[copy.form.unique_form_identifier].append(copy)
    if len(anchors) != _RECORD_COUNT or len(copy_cs) != _RECORD_COUNT:
        raise _fail("unexpected Form 1042-S economic-record count")

    canonical: list[_FormCopy] = []
    used_copy_c: set[int] = set()
    for anchor_copies in anchors.values():
        if {copy.form.copy_type for copy in anchor_copies} != {
            Form1042SCopyType.B,
            Form1042SCopyType.D,
        }:
            raise _fail("Form 1042-S B/D copies are missing or duplicated")
        if len({copy.fingerprint() for copy in anchor_copies}) != 1:
            raise _fail("Form 1042-S B/D copies disagree")
        matches = [
            index
            for index, copy_c in enumerate(copy_cs)
            if copy_c.fingerprint() == anchor_copies[0].fingerprint()
        ]
        if len(matches) != 1 or matches[0] in used_copy_c:
            raise _fail("Form 1042-S Copy C cannot be matched unambiguously")
        used_copy_c.add(matches[0])
        canonical.append(anchor_copies[0])
    if len(used_copy_c) != len(copy_cs):
        raise _fail("Form 1042-S packet contains an unmatched Copy C")
    return tuple(canonical)


def _packet_identity(canonical: tuple[_FormCopy, ...]) -> _IdentityFields:
    identities = {copy.identity for copy in canonical}
    if len(identities) != 1:
        raise _fail("Form 1042-S records contain inconsistent identities")
    return identities.pop()


def parse_1042s_packet(text: str) -> Form1042SPacket:
    """Parse and deduplicate the exact observed 20-page 2025 packet."""
    copies = tuple(_parse_copy(page) for page in _packet_form_pages(text))
    canonical = _canonical_copies(copies)
    identity = _packet_identity(canonical)
    records = tuple(
        sorted(
            (_record_from_anchor(copy) for copy in canonical),
            key=lambda record: (
                record.form.income_code,
                record.form.chapter3_tax_rate_percent,
            ),
        )
    )
    if len({record.record_key for record in records}) != len(records):
        raise _fail("Form 1042-S record keys are duplicated")
    return Form1042SPacket(
        _FORM_YEAR,
        identity.account_id,
        identity.account_holder,
        identity.recipient_country_code,
        identity.withholding_agent_name,
        identity.withholding_agent_ein,
        records,
    )


def read_1042s_packet(path: Path) -> Form1042SPacket:
    """Render and parse a private packet without exposing its path in errors."""
    text = pdftotext_layout(
        path,
        doc_kind="Fidelity CMA Form 1042-S PDFs",
        source=_SECTION,
    )
    try:
        return parse_1042s_packet(text)
    except ParseError as exc:
        raise exc from None
