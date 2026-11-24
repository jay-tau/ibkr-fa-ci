"""Parse the observed Robinhood Form 1042-S packet losslessly."""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Final, NoReturn

from ibkr_fa.exceptions import ParseError
from ibkr_fa.models.form_1042s import (
    Form1042SAmount,
    Form1042SAmountKind,
    Form1042SCopy,
    Form1042SCopyType,
)
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.parser._form_1042s import (
    BOX_10_REFUSAL,
    COPY_DESIGNATION_REFUSAL,
    copy_designation,
    reproduces_box_10,
)
from ibkr_fa.parser._pdf import pdftotext_layout
from ibkr_fa.parser._robinhood_money import ROBINHOOD_WHOLE_DOLLARS

_SECTION: Final[str] = "Robinhood Form 1042-S packet"
_FORM_YEAR: Final[int] = 2025
_FORM_LINE_COUNT: Final[int] = 47
_UFI_DIGITS: Final[int] = 10
_LEFT_COLUMN_END: Final[int] = 101
_CODE_TABLE_COLUMN: Final[int] = 90
_UFI_LINE_INDEX: Final[int] = 4
_AGENT_VALUE_COUNT: Final[int] = 3
_DOB_DIGITS: Final[int] = 8
_SUMMARY_MIN_LINES: Final[int] = 10
_PACKET_TRAILING_PAGES: Final[int] = 4
_PAGES_PER_ECONOMIC_FORM: Final[int] = 4
_MIN_PACKET_PAGES: Final[int] = 8
_MONEY_BODY: Final[str] = rf"{ROBINHOOD_WHOLE_DOLLARS}\.[0-9]{{2}}"
_MONEY_RE: Final = re.compile(rf"{_MONEY_BODY}")
_DASH_ZERO: Final[str] = "-0-"
_UFI_ROW_HEADER: Final[str] = "Internal Revenue Service "
_SPACED_UFI: Final = re.compile(rf"(?:[0-9] ){{{_UFI_DIGITS - 1}}}[0-9]")
_SPACED_DOB: Final = re.compile(rf"(?:[0-9] ){{{_DOB_DIGITS - 1}}}[0-9]")
_BOX9_LABEL: Final[str] = (
    "9 Overwithheld tax repaid to recipient pursuant to adjustment procedures (see instructions)"
)
_UNCHECKED_OPTION_CHROME: Final[tuple[str, ...]] = (
    "7b Check if federal tax withheld was not deposited with the IRS because",
    "   escrow procedures were applied (see instructions) . . . . . .",
    "7c Check if withholding occurred in subsequent year with respect to a",
    "   partnership interest . . . . . . . . . . . . .                                     .",
    "7d Check if you are a qualified intermediary, withholding foreign",
    "   partnership, or withholding foreign trust revising its reporting on Form",
    "   1042-S to report to a specific recipient . . . . . . . .                           .",
)
_PRO_RATA_CHROME: Final[str] = (
    "14b Primary withholding agent's EIN           15 Check if pro-rata basis reporting"
)


class Robinhood1042SPageKind(StrEnum):
    """Physical page roles in the observed packet."""

    FORM_B = "form-b"
    FORM_C = "form-c"
    FORM_VERSO = "form-verso"
    BROKER_SUMMARY = "broker-summary"
    IRS_RECIPIENT_INSTRUCTIONS = "irs-recipient-instructions"
    IRS_CODES_CH3 = "irs-codes-ch3"
    IRS_CODES_CH4 = "irs-codes-ch4"


class Robinhood1042SBlankField(StrEnum):
    """Regions proved blank in every supplied form."""

    WITHHOLDING_AGENT_GIIN = "12e"
    WITHHOLDING_AGENT_FOREIGN_TIN = "12g"
    RECIPIENT_GIIN = "13h"
    RECIPIENT_LOB_CODE = "13j"
    PRIMARY_AGENT_NAME = "14a"
    PRIMARY_AGENT_EIN = "14b"
    INTERMEDIARY_EIN = "15a"
    INTERMEDIARY_CH3_STATUS = "15b"
    INTERMEDIARY_CH4_STATUS = "15c"
    INTERMEDIARY_NAME = "15d"
    INTERMEDIARY_GIIN = "15e"
    INTERMEDIARY_COUNTRY = "15f"
    INTERMEDIARY_FOREIGN_TIN = "15g"
    INTERMEDIARY_ADDRESS = "15h"
    INTERMEDIARY_CITY = "15i"
    PAYER_NAME = "16a"
    PAYER_TIN = "16b"
    PAYER_GIIN = "16c"
    PAYER_CH3_STATUS = "16d"
    PAYER_CH4_STATUS = "16e"
    STATE_TAX_WITHHELD = "17a"
    PAYER_STATE_TAX_NUMBER = "17b"
    STATE_NAME = "17c"


_PROVED_BLANK_FIELDS: Final[frozenset[Robinhood1042SBlankField]] = frozenset(
    Robinhood1042SBlankField
)


@dataclass(frozen=True, slots=True, repr=False)
class Robinhood1042SPage:
    """One classified physical PDF page."""

    ordinal: int
    kind: Robinhood1042SPageKind
    unique_form_identifier: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class Robinhood1042SAgent:
    """Populated withholding-agent Boxes 12a-d/f/h-i."""

    ein: str
    chapter3_status_code: str
    chapter4_status_code: str
    name: str
    country_code: str
    phone: str
    address: str
    city_state_postal: str


@dataclass(frozen=True, slots=True, repr=False)
class Robinhood1042SRecipient:
    """Populated recipient Boxes 13a-g/i/k-l."""

    name: str
    country_code: str
    address: str
    city_state_postal: str
    us_tin: str
    chapter3_status_code: str
    chapter4_status_code: str
    foreign_tin: str
    account_reference: str
    date_of_birth: date


@dataclass(frozen=True, slots=True, repr=False)
class Robinhood1042SCopy(Form1042SCopy):
    """Every parsed field from one physical Copy B or Copy C."""

    page_ordinal: int
    agent: Robinhood1042SAgent
    recipient: Robinhood1042SRecipient
    proved_blank_fields: frozenset[Robinhood1042SBlankField]

    def economic_fingerprint(self) -> tuple[object, ...]:
        """All fields that must agree between physical B/C copies."""

        return tuple(
            getattr(self, item.name)
            for item in fields(self)
            if item.name not in {"page_ordinal", "copy_type"}
        )


@dataclass(frozen=True, slots=True, repr=False)
class Robinhood1042SSummaryRow:
    """One broker-generated tax-summary income group."""

    income_code: str
    chapter3_exemption_code: str
    chapter3_tax_rate_percent: Decimal
    chapter4_exemption_code: str
    chapter4_tax_rate_percent: Decimal
    gross_income: Form1042SAmount
    federal_tax_withheld: Form1042SAmount
    tax_withheld_other_agents: Form1042SAmount
    tax_paid_by_agent: Form1042SAmount
    total_withholding_credit: Form1042SAmount
    amount_repaid_to_recipient: Form1042SAmount


@dataclass(frozen=True, slots=True, repr=False)
class Robinhood1042SSummary:
    """Broker-generated packet summary, preserved without reconciling it."""

    page_ordinal: int
    account_reference: str
    rows: tuple[Robinhood1042SSummaryRow, ...]


@dataclass(frozen=True, slots=True, repr=False)
class Robinhood1042SPacket:
    """Lossless source evidence from one complete packet."""

    tax_year: int
    pages: tuple[Robinhood1042SPage, ...]
    copies: tuple[Robinhood1042SCopy, ...]
    summary: Robinhood1042SSummary


@dataclass(frozen=True, slots=True)
class _InstructionCodeTables:
    income: str
    chapter3_exemption: str
    chapter4_exemption: str
    chapter3_status: str
    chapter4_status: str


def _fail(message: str, *, page: int = 0, column: str | None = None) -> NoReturn:
    raise ParseError(
        section=_SECTION,
        row_index=page,
        column=column,
        message=message,
        raw_value="",
    )


def _decimal(raw: str, *, page: int, column: str, kind: NumberKind) -> Decimal:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        _fail("Form 1042-S contains a malformed decimal", page=page, column=column)
    if value < 0 or not value.is_finite():
        _fail("Form 1042-S decimal must be finite and non-negative", page=page, column=column)
    breach = number_limit_breach(value, kind)
    if breach is not None:
        _fail(breach, page=page, column=column)
    return value


def _amount(
    raw: str,
    *,
    page: int,
    column: str,
    parenthesized: bool = False,
) -> Form1042SAmount:
    value = raw.strip()
    compact = re.sub(r"[ \t]+", "", value)
    if compact in {"", "()"}:
        return Form1042SAmount(Form1042SAmountKind.BLANK, None)
    has_parentheses = compact.startswith("(") and compact.endswith(")")
    if has_parentheses and not parenthesized:
        _fail(
            "Form 1042-S amount has unexpected parentheses",
            page=page,
            column=column,
        )
    if compact in {_DASH_ZERO, f"({_DASH_ZERO})"}:
        return Form1042SAmount(
            Form1042SAmountKind.DASH_ZERO,
            Decimal("0"),
        )
    if compact.startswith("(") and compact.endswith(")"):
        compact = compact[1:-1]
    match = _MONEY_RE.fullmatch(compact)
    if match is None:
        _fail("Form 1042-S amount has an unsupported format", page=page, column=column)
    return Form1042SAmount(
        Form1042SAmountKind.AMOUNT,
        _decimal(compact, page=page, column=column, kind=NumberKind.MONEY),
    )


def _rate(raw: str, *, page: int, column: str) -> Decimal:
    match = re.fullmatch(r"[ \t]*([0-9]{1,3})[ \t]*\.[ \t]*([0-9]{2})[ \t]*", raw)
    if match is None:
        _fail("Form 1042-S tax rate is malformed", page=page, column=column)
    return _decimal(
        f"{match.group(1)}.{match.group(2)}",
        page=page,
        column=column,
        kind=NumberKind.RATE,
    )


def _label_tail(
    raw: str,
    label: str,
    *,
    page: int,
    column: str,
) -> str:
    if not raw.startswith(label):
        _fail("Form 1042-S box label is missing or shifted", page=page, column=column)
    return raw[len(label) :].strip()


def _exact_nonempty(raw: str, *, page: int, column: str) -> str:
    value = raw.strip()
    if not value:
        _fail("Form 1042-S required source field is blank", page=page, column=column)
    return value


def _require_blank(raw: str, *, page: int, column: str) -> None:
    if raw.strip():
        _fail("Form 1042-S previously blank field is now populated", page=page, column=column)


def _normalize_space(raw: str) -> str:
    return re.sub(r"[ \t]+", " ", raw).strip()


def _form_pages(text: str) -> tuple[str, ...]:
    segments = text.split("\f")
    if not segments or segments[-1].strip():
        _fail("Form 1042-S packet is missing its trailing page delimiter")
    segments.pop()
    if not segments or any(not segment.strip() for segment in segments):
        _fail("Form 1042-S packet contains an empty physical page")
    return tuple(segments)


def _is_form_page(page: str) -> bool:
    return "OMB No." in page and bool(re.search(r"Form 1042-S[ \t]*\([0-9]{4}\)", page))


def _form_year(page: str, *, page_ordinal: int) -> int:
    top = re.findall(r"\b([0-9]{4})[ \t]+OMB No\.", page)
    bottom = re.findall(r"Form 1042-S[ \t]*\(([0-9]{4})\)", page)
    if top != [str(_FORM_YEAR)] or bottom != [str(_FORM_YEAR)]:
        _fail(
            "Form 1042-S version or tax year is unsupported",
            page=page_ordinal,
        )
    return _FORM_YEAR


def _copy_header(
    page: str,
    lines: list[str],
    *,
    page_ordinal: int,
) -> tuple[Form1042SCopyType, str]:
    """Read the copy letter and the unique form identifier (UFI).

    With runs of spaces and tabs collapsed to one space, the text before
    ``UNIQUE FORM IDENTIFIER`` must be exactly ``Internal Revenue Service``
    followed by ten single digits separated by single spaces."""
    copy_type = copy_designation(page, "BC")
    if copy_type is None:
        _fail(COPY_DESIGNATION_REFUSAL, page=page_ordinal)
    if len(lines) <= _UFI_LINE_INDEX or "UNIQUE FORM IDENTIFIER" not in lines[_UFI_LINE_INDEX]:
        _fail("Form 1042-S UFI row is missing or shifted", page=page_ordinal)
    ufi_line = lines[_UFI_LINE_INDEX]
    ufi_prefix, tail = ufi_line.split("UNIQUE FORM IDENTIFIER", 1)
    ufi_words = _normalize_space(ufi_prefix)
    if not ufi_words.startswith(_UFI_ROW_HEADER):
        _fail("Form 1042-S UFI row is missing or shifted", page=page_ordinal)
    spaced_ufi = ufi_words.removeprefix(_UFI_ROW_HEADER)
    if _SPACED_UFI.fullmatch(spaced_ufi) is None:
        _fail("Form 1042-S UFI is malformed", page=page_ordinal)
    unique_form_identifier = spaced_ufi.replace(" ", "")
    if "AMENDED" not in tail or "AMENDMENT NO." not in tail:
        _fail("Form 1042-S amendment fields are missing", page=page_ordinal)
    amended_cell, amendment_tail = tail.split("AMENDMENT NO.", 1)
    amended_cell = amended_cell.strip()
    if not amended_cell.startswith("AMENDED"):
        _fail("Form 1042-S amendment field is malformed", page=page_ordinal)
    if amended_cell.removeprefix("AMENDED").strip():
        _fail("amended Form 1042-S layout is not yet evidenced", page=page_ordinal)
    amendment_value = amendment_tail.replace("for Recipient", "").strip()
    if amendment_value:
        _fail("amended Form 1042-S layout is not yet evidenced", page=page_ordinal)
    return copy_type, unique_form_identifier


def _parse_core_boxes(
    lines: list[str],
    *,
    page: int,
) -> tuple[
    str,
    Form1042SAmount,
    str,
    str,
    Decimal,
    str,
    Decimal,
    Form1042SAmount,
    Form1042SAmount,
    Form1042SAmount,
    Form1042SAmount,
    Form1042SAmount,
    Form1042SAmount,
    Form1042SAmount,
]:
    if len(lines) < _FORM_LINE_COUNT:
        _fail("Form 1042-S page is truncated", page=page)
    income_match = re.fullmatch(r"[ \t]*([0-9]{2})[ \t]*", lines[7][0:13])
    if income_match is None:
        _fail("Form 1042-S income code is missing or malformed", page=page)
    income_code = income_match.group(1)
    gross_income = _amount(lines[7][13:28], page=page, column="Box 2")

    chapter_match = re.search(
        r"Enter “3” or “4”[ \t]+([34])[ \t]*$",
        lines[5][34:_LEFT_COLUMN_END],
    )
    if chapter_match is None:
        _fail("Form 1042-S chapter indicator is malformed", page=page)
    chapter_indicator = chapter_match.group(1)
    chapter3_exemption = _label_tail(
        lines[6][34:67],
        "3a Exemption code",
        page=page,
        column="Box 3a",
    )
    chapter4_exemption = _label_tail(
        lines[6][67:_LEFT_COLUMN_END],
        "4a Exemption code",
        page=page,
        column="Box 4a",
    )
    if (
        re.fullmatch(r"[0-9]{2}", chapter3_exemption) is None
        or re.fullmatch(r"[0-9]{2}", chapter4_exemption) is None
    ):
        _fail("Form 1042-S exemption code is malformed", page=page)
    chapter3_rate = _rate(
        _label_tail(
            lines[7][28:67],
            "3b Tax rate",
            page=page,
            column="Box 3b",
        ),
        page=page,
        column="Box 3b",
    )
    chapter4_rate = _rate(
        _label_tail(
            lines[7][67:_LEFT_COLUMN_END],
            "4b Tax rate",
            page=page,
            column="Box 4b",
        ),
        page=page,
        column="Box 4b",
    )

    box5 = _amount(
        _label_tail(
            lines[9][:_LEFT_COLUMN_END],
            "5 Withholding allowance",
            page=page,
            column="Box 5",
        ),
        page=page,
        column="Box 5",
    )
    box6 = _amount(
        _label_tail(
            lines[10][:_LEFT_COLUMN_END],
            "6 Net income",
            page=page,
            column="Box 6",
        ),
        page=page,
        column="Box 6",
    )
    box7a = _amount(
        _label_tail(
            lines[11][:99],
            "7a Federal tax withheld",
            page=page,
            column="Box 7a",
        ),
        page=page,
        column="Box 7a",
    )
    box8 = _amount(
        _label_tail(
            lines[20][:_LEFT_COLUMN_END],
            "8 Tax withheld by other agents",
            page=page,
            column="Box 8",
        ),
        page=page,
        column="Box 8",
    )
    box9_right_label = "15a Intermediary or flow-through entity's EIN, if any"
    box9_right_start = lines[21].find(box9_right_label)
    if box9_right_start < 0:
        _fail("Form 1042-S Box 9 boundary label is missing", page=page)
    _require_blank(
        _label_tail(
            lines[21][:box9_right_start],
            _BOX9_LABEL,
            page=page,
            column="Box 9",
        ),
        page=page,
        column="Box 9",
    )
    box9_cell = lines[22][:_LEFT_COLUMN_END].strip()
    if not (box9_cell.startswith("(") and box9_cell.endswith(")")):
        _fail("Form 1042-S Box 9 cell is malformed", page=page)
    box9 = _amount(
        box9_cell,
        page=page,
        column="Box 9",
        parenthesized=True,
    )
    _require_blank(
        _label_tail(
            lines[23][:_LEFT_COLUMN_END],
            "10 Total withholding credit (combine boxes 7a, 8, and 9)",
            page=page,
            column="Box 10",
        ),
        page=page,
        column="Box 10",
    )
    box10 = _amount(lines[24][:_LEFT_COLUMN_END], page=page, column="Box 10")
    box11 = _amount(
        _label_tail(
            lines[25][:_LEFT_COLUMN_END],
            "11 Tax paid by withholding agent (amounts not withheld) (see instructions)",
            page=page,
            column="Box 11",
        ),
        page=page,
        column="Box 11",
    )
    return (
        income_code,
        gross_income,
        chapter_indicator,
        chapter3_exemption,
        chapter3_rate,
        chapter4_exemption,
        chapter4_rate,
        box5,
        box6,
        box7a,
        box8,
        box9,
        box10,
        box11,
    )


def _parse_agent(lines: list[str], *, page: int) -> Robinhood1042SAgent:
    values = lines[29][:_LEFT_COLUMN_END].split()
    if len(values) != _AGENT_VALUE_COUNT:
        _fail("Form 1042-S withholding-agent status row is malformed", page=page)
    ein, chapter3, chapter4 = values
    if re.fullmatch(r"[0-9]{2}", chapter3) is None or re.fullmatch(r"[0-9]{2}", chapter4) is None:
        _fail("Form 1042-S withholding-agent status code is malformed", page=page)
    name = _exact_nonempty(lines[31][:_LEFT_COLUMN_END], page=page, column="Box 12d")
    _require_blank(lines[33][:_LEFT_COLUMN_END], page=page, column="Box 12e")
    country = _exact_nonempty(lines[35][:30], page=page, column="Box 12f")
    _require_blank(lines[35][30:_LEFT_COLUMN_END], page=page, column="Box 12g")
    phone = _exact_nonempty(
        _label_tail(
            lines[36][:_LEFT_COLUMN_END],
            "12h Address (number and street)",
            page=page,
            column="Box 12h",
        ),
        page=page,
        column="Box 12h phone",
    )
    return Robinhood1042SAgent(
        ein=_exact_nonempty(ein, page=page, column="Box 12a"),
        chapter3_status_code=chapter3,
        chapter4_status_code=chapter4,
        name=name,
        country_code=country,
        phone=phone,
        address=_exact_nonempty(
            lines[37][:_LEFT_COLUMN_END],
            page=page,
            column="Box 12h",
        ),
        city_state_postal=_exact_nonempty(
            lines[39][:_LEFT_COLUMN_END],
            page=page,
            column="Box 12i",
        ),
    )


def _status_tail(raw: str, label: str, *, page: int, column: str) -> str:
    value = _label_tail(raw, label, page=page, column=column)
    if re.fullmatch(r"[0-9]{2}", value) is None:
        _fail("Form 1042-S status code is malformed", page=page, column=column)
    return value


def _parse_recipient(lines: list[str], *, page: int) -> Robinhood1042SRecipient:
    """Read the recipient Boxes 13a-g/i/k-l.

    With runs of spaces and tabs collapsed to one space, the Box 13l birth-date
    cell must be exactly eight single digits separated by single spaces
    (YYYYMMDD) that name a real date."""
    if (
        not lines[40][:53].startswith("13a Recipient's name")
        or not lines[40][53:_LEFT_COLUMN_END].startswith("13b Recipient's country code")
        or not lines[42][:_LEFT_COLUMN_END].startswith("13c Address (number and street)")
        or not lines[44][:_LEFT_COLUMN_END].startswith(
            "13d City or town, state or province, country, ZIP or foreign postal code"
        )
    ):
        _fail("Form 1042-S recipient label block is missing or shifted", page=page)
    us_tin = _exact_nonempty(lines[6][_LEFT_COLUMN_END:150], page=page, column="Box 13e")
    chapter3 = _status_tail(
        lines[5][150:],
        "13f Ch. 3 status code",
        page=page,
        column="Box 13f",
    )
    chapter4 = _status_tail(
        lines[6][150:],
        "13g Ch. 4 status code",
        page=page,
        column="Box 13g",
    )
    _require_blank(lines[8][_LEFT_COLUMN_END:133], page=page, column="Box 13h")
    foreign_tin = _exact_nonempty(lines[8][133:178], page=page, column="Box 13i")
    _require_blank(lines[8][178:], page=page, column="Box 13j")
    account_reference = _exact_nonempty(
        lines[12][99:],
        page=page,
        column="Box 13k",
    )
    birth_cell = _normalize_space(lines[16][_LEFT_COLUMN_END:])
    if _SPACED_DOB.fullmatch(birth_cell) is None:
        _fail(
            "Form 1042-S recipient date of birth is malformed",
            page=page,
            column="Box 13l",
        )
    birth_digits = birth_cell.replace(" ", "")
    date_of_birth = None
    with contextlib.suppress(ValueError):
        date_of_birth = date(
            int(birth_digits[:4]),
            int(birth_digits[4:6]),
            int(birth_digits[6:]),
        )
    # Refused outside the handler, so no chained error names the date's parts.
    if date_of_birth is None:
        _fail(
            "Form 1042-S recipient date of birth is invalid",
            page=page,
            column="Box 13l",
        )
    country_code = _exact_nonempty(
        lines[41][53:_LEFT_COLUMN_END],
        page=page,
        column="Box 13b",
    )
    if re.fullmatch(r"[A-Z]{2}", country_code) is None:
        _fail("Form 1042-S recipient country code is malformed", page=page)
    return Robinhood1042SRecipient(
        name=_exact_nonempty(lines[41][:53], page=page, column="Box 13a"),
        country_code=country_code,
        address=_exact_nonempty(lines[43][:_LEFT_COLUMN_END], page=page, column="Box 13c"),
        city_state_postal=_exact_nonempty(
            lines[45][:_LEFT_COLUMN_END],
            page=page,
            column="Box 13d",
        ),
        us_tin=us_tin,
        chapter3_status_code=chapter3,
        chapter4_status_code=chapter4,
        foreign_tin=foreign_tin,
        account_reference=account_reference,
        date_of_birth=date_of_birth,
    )


def _checkboxes_and_blank_regions(
    lines: list[str],
    *,
    page: int,
) -> tuple[bool, bool, bool, bool]:
    observed_chrome = tuple(
        lines[index][:_LEFT_COLUMN_END].rstrip() for index in (12, 13, 15, 16, 17, 18, 19)
    )
    if observed_chrome != _UNCHECKED_OPTION_CHROME:
        _fail("checked or changed Form 1042-S option is not yet evidenced", page=page)
    if _normalize_space(lines[19][_LEFT_COLUMN_END:]) != _normalize_space(_PRO_RATA_CHROME):
        _fail("changed Form 1042-S pro-rata option is not yet evidenced", page=page)

    _require_blank(lines[18][_LEFT_COLUMN_END:], page=page, column="Box 14a")
    _require_blank(lines[20][_LEFT_COLUMN_END:], page=page, column="Box 14b")
    _require_blank(lines[22][_LEFT_COLUMN_END:], page=page, column="Boxes 15a-c")
    _require_blank(lines[24][_LEFT_COLUMN_END:], page=page, column="Box 15d")
    _require_blank(
        _label_tail(
            lines[25][_LEFT_COLUMN_END:],
            "15e Intermediary or flow-through entity's GIIN",
            page=page,
            column="Box 15e",
        ),
        page=page,
        column="Box 15e",
    )
    _require_blank(lines[26][:_LEFT_COLUMN_END], page=page, column="Box 15e")
    _require_blank(lines[27][_LEFT_COLUMN_END:], page=page, column="Boxes 15f-g")
    _require_blank(lines[30][_LEFT_COLUMN_END:], page=page, column="Box 15h")
    _require_blank(lines[32][_LEFT_COLUMN_END:], page=page, column="Box 15i")
    _require_blank(lines[34][_LEFT_COLUMN_END:], page=page, column="Boxes 16a-b")
    _require_blank(lines[36][_LEFT_COLUMN_END:], page=page, column="Boxes 16c-e")
    _require_blank(lines[38][_LEFT_COLUMN_END:], page=page, column="Boxes 17a-c")
    return False, False, False, False


def _conditional_arithmetic(copy: Robinhood1042SCopy) -> None:
    gross = copy.gross_income.value
    allowance = copy.withholding_allowance.value
    net = copy.net_income.value
    if gross is not None and allowance is not None and net is not None and gross - allowance != net:
        _fail("Form 1042-S Boxes 2/5 do not reproduce Box 6", page=copy.page_ordinal)
    box7a = copy.federal_tax_withheld.value
    box8 = copy.tax_withheld_other_agents.value
    box9 = copy.overwithheld_tax_repaid.value
    box10 = copy.total_withholding_credit.value
    if all(value is not None for value in (box7a, box8, box9, box10)):
        assert box7a is not None and box8 is not None and box9 is not None and box10 is not None
        if not reproduces_box_10(box7a, box8, box9, box10):
            _fail(BOX_10_REFUSAL, page=copy.page_ordinal)


def _parse_copy(page_text: str, *, page_ordinal: int) -> Robinhood1042SCopy:
    tax_year = _form_year(page_text, page_ordinal=page_ordinal)
    lines = page_text.splitlines()
    if len(lines) != _FORM_LINE_COUNT:
        _fail("Form 1042-S page line structure is shifted", page=page_ordinal)
    if f"Form 1042-S ({_FORM_YEAR})" not in lines[46]:
        _fail("Form 1042-S footer is missing or shifted", page=page_ordinal)
    copy_type, ufi = _copy_header(page_text, lines, page_ordinal=page_ordinal)
    (
        income_code,
        gross_income,
        chapter_indicator,
        chapter3_exemption,
        chapter3_rate,
        chapter4_exemption,
        chapter4_rate,
        box5,
        box6,
        box7a,
        box8,
        box9,
        box10,
        box11,
    ) = _parse_core_boxes(lines, page=page_ordinal)
    box7b, box7c, box7d, pro_rata = _checkboxes_and_blank_regions(
        lines,
        page=page_ordinal,
    )
    copy = Robinhood1042SCopy(
        page_ordinal=page_ordinal,
        copy_type=copy_type,
        unique_form_identifier=ufi,
        tax_year=tax_year,
        amended=False,
        amendment_number=None,
        income_code=income_code,
        gross_income=gross_income,
        chapter_indicator=chapter_indicator,
        chapter3_exemption_code=chapter3_exemption,
        chapter3_tax_rate_percent=chapter3_rate,
        chapter4_exemption_code=chapter4_exemption,
        chapter4_tax_rate_percent=chapter4_rate,
        withholding_allowance=box5,
        net_income=box6,
        federal_tax_withheld=box7a,
        federal_tax_not_deposited=box7b,
        subsequent_year_withholding=box7c,
        qualified_intermediary=box7d,
        tax_withheld_other_agents=box8,
        overwithheld_tax_repaid=box9,
        total_withholding_credit=box10,
        tax_paid_by_agent=box11,
        agent=_parse_agent(lines, page=page_ordinal),
        recipient=_parse_recipient(lines, page=page_ordinal),
        pro_rata_reporting=pro_rata,
        proved_blank_fields=_PROVED_BLANK_FIELDS,
    )
    _conditional_arithmetic(copy)
    return copy


def _instruction_page_kind(page: str, ordinal: int) -> Robinhood1042SPageKind:
    if "U.S. Income Tax Filing Requirements" in page and "Explanation of Codes" in page:
        return Robinhood1042SPageKind.IRS_RECIPIENT_INSTRUCTIONS
    if "Explanation of Codes (continued)" in page and "Boxes 3a and 4a" in page:
        return Robinhood1042SPageKind.IRS_CODES_CH3
    if "Explanation of Codes (continued)" in page and "Chapter 4 Status Codes" in page:
        return Robinhood1042SPageKind.IRS_CODES_CH4
    _fail("Form 1042-S instruction page is unknown or reordered", page=ordinal)


def _code_is_listed(code: str, instruction_text: str) -> bool:
    if code == "00":
        return True  # IRS no-exemption/no-status token.
    return bool(re.search(rf"(?m)(?:^|[ \t]){re.escape(code)}[ \t]+[A-Z]", instruction_text))


def _instruction_code_tables(
    recipient_page: str,
    chapter3_page: str,
    chapter4_page: str,
) -> _InstructionCodeTables:
    recipient_lines = recipient_page.splitlines()
    chapter3_lines = chapter3_page.splitlines()
    chapter4_lines = chapter4_page.splitlines()
    exemption_index = next(
        (
            index
            for index, line in enumerate(chapter3_lines)
            if "Boxes 3a and 4a. Exemption Code" in line
        ),
        None,
    )
    status_index = next(
        (index for index, line in enumerate(chapter3_lines) if "Boxes 12b, 12c, 13f, 13g" in line),
        None,
    )
    chapter4_exemption_index = next(
        (
            index
            for index, line in enumerate(chapter3_lines)
            if "Chapter 4" in line[_CODE_TABLE_COLUMN:]
        ),
        None,
    )
    chapter4_status_index = next(
        (index for index, line in enumerate(chapter4_lines) if "Chapter 4 Status Codes" in line),
        None,
    )
    if (
        exemption_index is None
        or status_index is None
        or chapter4_exemption_index is None
        or chapter4_status_index is None
        or not (chapter4_exemption_index < status_index < exemption_index)
    ):
        _fail("Form 1042-S instruction code-table boundaries are missing")
    shared_status_lines = [
        line[:_CODE_TABLE_COLUMN]
        for line in chapter4_lines[chapter4_status_index + 1 :]
        if re.match(r"[ \t]*0[1-4][ \t]+", line[:_CODE_TABLE_COLUMN])
    ]
    return _InstructionCodeTables(
        income="\n".join(
            [
                *recipient_lines,
                *(line[:_CODE_TABLE_COLUMN] for line in chapter3_lines[:exemption_index]),
            ]
        ),
        chapter3_exemption="\n".join(
            [
                *(line[:_CODE_TABLE_COLUMN] for line in chapter3_lines[exemption_index:]),
                *(line[_CODE_TABLE_COLUMN:] for line in chapter3_lines[:chapter4_exemption_index]),
            ]
        ),
        chapter4_exemption="\n".join(
            line[_CODE_TABLE_COLUMN:]
            for line in chapter3_lines[chapter4_exemption_index:status_index]
        ),
        chapter3_status="\n".join(
            [
                *(line[_CODE_TABLE_COLUMN:] for line in chapter3_lines[status_index:]),
                *(line[:_CODE_TABLE_COLUMN] for line in chapter4_lines[:chapter4_status_index]),
                *shared_status_lines,
            ]
        ),
        chapter4_status="\n".join(
            [
                *(line[:_CODE_TABLE_COLUMN] for line in chapter4_lines[chapter4_status_index:]),
                *(line[_CODE_TABLE_COLUMN:] for line in chapter4_lines),
            ]
        ),
    )


def _validate_copy_codes(
    copy: Robinhood1042SCopy,
    tables: _InstructionCodeTables,
) -> None:
    checks = (
        (copy.income_code, tables.income),
        (copy.chapter3_exemption_code, tables.chapter3_exemption),
        (copy.chapter4_exemption_code, tables.chapter4_exemption),
        (copy.agent.chapter3_status_code, tables.chapter3_status),
        (copy.recipient.chapter3_status_code, tables.chapter3_status),
        (copy.agent.chapter4_status_code, tables.chapter4_status),
        (copy.recipient.chapter4_status_code, tables.chapter4_status),
    )
    for code, table in checks:
        if not _code_is_listed(code, table):
            _fail(
                "Form 1042-S code is absent from the packet's code tables",
                page=copy.page_ordinal,
            )


def _parse_summary(page: str, *, page_ordinal: int) -> Robinhood1042SSummary:
    lines = page.splitlines()
    if (
        len(lines) < _SUMMARY_MIN_LINES
        or "Foreign Person's U.S. Source Income Subject to Withholding" not in page
    ):
        _fail("Robinhood tax summary page is missing or malformed", page=page_ordinal)
    account_match = re.fullmatch(
        r"[ \t]*Robinhood Markets Inc as agent for[ \t]+"
        r"Account:[ \t]*(\S+)[ \t]*",
        lines[0],
    )
    if account_match is None:
        _fail("Robinhood tax summary account reference is missing", page=page_ordinal)
    if _normalize_space(lines[1]) != "Robinhood Securities LLC Summary":
        _fail("Robinhood tax summary subheading is missing or shifted", page=page_ordinal)
    expected_title = "Foreign Person's U.S. Source Income Subject to Withholding"
    if lines[2].strip() != expected_title:
        _fail("Robinhood tax summary title is missing or shifted", page=page_ordinal)
    rows: list[Robinhood1042SSummaryRow] = []
    consumed_lines = {0, 1, 2}
    for index, line in enumerate(lines):
        code_match = re.fullmatch(r"[ \t]*INCOME CODE[ \t]+([0-9]{2})[ \t]*", line)
        if "INCOME CODE" in line and code_match is None:
            _fail(
                "Robinhood tax summary income heading is malformed",
                page=page_ordinal,
            )
        if code_match is None:
            continue
        if index + 4 >= len(lines):
            _fail("Robinhood tax summary row is truncated", page=page_ordinal)
        chapter_match = re.fullmatch(
            r"[ \t]*Chapter 3, exemption code ([0-9]{2}) - "
            r"([0-9]+\.[0-9]{2})%[ \t]+Chapter 4, exemption code "
            r"([0-9]{2}) - ([0-9]+\.[0-9]{2})%[ \t]*",
            lines[index + 1],
        )
        if chapter_match is None:
            _fail("Robinhood tax summary chapter row is malformed", page=page_ordinal)
        if "Federal Tax" not in lines[index + 2] or "Gross Income" not in lines[index + 3]:
            _fail("Robinhood tax summary column headings are malformed", page=page_ordinal)
        consumed_lines.update(range(index, index + 5))
        starts = (0, 29, 48, 66, 86, 107)
        data = lines[index + 4]
        ends: tuple[int | None, ...] = (*starts[1:], None)
        values = tuple(
            _amount(
                data[start:end],
                page=page_ordinal,
                column=column,
            )
            for start, end, column in zip(
                starts,
                ends,
                (
                    "Gross Income",
                    "Federal Tax Withheld",
                    "Tax Withheld Other Agents",
                    "Tax Paid By Agent",
                    "Total Credit",
                    "Amount Repaid",
                ),
                strict=True,
            )
        )
        rows.append(
            Robinhood1042SSummaryRow(
                income_code=code_match.group(1),
                chapter3_exemption_code=chapter_match.group(1),
                chapter3_tax_rate_percent=_decimal(
                    chapter_match.group(2),
                    page=page_ordinal,
                    column="Chapter 3 rate",
                    kind=NumberKind.RATE,
                ),
                chapter4_exemption_code=chapter_match.group(3),
                chapter4_tax_rate_percent=_decimal(
                    chapter_match.group(4),
                    page=page_ordinal,
                    column="Chapter 4 rate",
                    kind=NumberKind.RATE,
                ),
                gross_income=values[0],
                federal_tax_withheld=values[1],
                tax_withheld_other_agents=values[2],
                tax_paid_by_agent=values[3],
                total_withholding_credit=values[4],
                amount_repaid_to_recipient=values[5],
            )
        )
    if any(line.strip() for index, line in enumerate(lines) if index not in consumed_lines):
        _fail("Robinhood tax summary contains an unsupported line", page=page_ordinal)
    if not rows:
        _fail("Robinhood tax summary contains no income rows", page=page_ordinal)
    return Robinhood1042SSummary(
        page_ordinal=page_ordinal,
        account_reference=account_match.group(1),
        rows=tuple(rows),
    )


def parse_robinhood_1042s_packet(text: str) -> Robinhood1042SPacket:
    """Parse one complete supplied Robinhood Form 1042-S packet."""

    physical_pages = _form_pages(text)
    if (
        len(physical_pages) < _MIN_PACKET_PAGES
        or (len(physical_pages) - _PACKET_TRAILING_PAGES) % _PAGES_PER_ECONOMIC_FORM
    ):
        _fail("Form 1042-S packet page count does not match the observed sequence")
    economic_count = (len(physical_pages) - _PACKET_TRAILING_PAGES) // _PAGES_PER_ECONOMIC_FORM
    copies: list[Robinhood1042SCopy] = []
    page_records: list[Robinhood1042SPage] = []
    for economic_index in range(economic_count):
        offset = economic_index * _PAGES_PER_ECONOMIC_FORM
        copy_b_page = physical_pages[offset]
        verso_b = physical_pages[offset + 1]
        copy_c_page = physical_pages[offset + 2]
        verso_c = physical_pages[offset + 3]
        if not _is_form_page(copy_b_page) or not _is_form_page(copy_c_page):
            _fail("Form 1042-S form page is missing or reordered", page=offset + 1)
        copy_b = _parse_copy(copy_b_page, page_ordinal=offset + 1)
        copy_c = _parse_copy(copy_c_page, page_ordinal=offset + 3)
        if (
            copy_b.copy_type is not Form1042SCopyType.B
            or copy_c.copy_type is not Form1042SCopyType.C
        ):
            _fail("Form 1042-S B/C sequence is unsupported", page=offset + 1)
        if copy_b.economic_fingerprint() != copy_c.economic_fingerprint():
            _fail("Form 1042-S B/C copies disagree", page=offset + 1)
        for verso, ordinal in ((verso_b, offset + 2), (verso_c, offset + 4)):
            if tuple(line.strip() for line in verso.splitlines() if line.strip()) != (
                "This page intentionally left blank.",
            ):
                _fail("Form 1042-S verso page is malformed", page=ordinal)
            page_records.append(Robinhood1042SPage(ordinal, Robinhood1042SPageKind.FORM_VERSO))
        copies.extend((copy_b, copy_c))
        page_records.extend(
            (
                Robinhood1042SPage(
                    copy_b.page_ordinal,
                    Robinhood1042SPageKind.FORM_B,
                    copy_b.unique_form_identifier,
                ),
                Robinhood1042SPage(
                    copy_c.page_ordinal,
                    Robinhood1042SPageKind.FORM_C,
                    copy_c.unique_form_identifier,
                ),
            )
        )
    if len({copy.unique_form_identifier for copy in copies}) != economic_count:
        _fail("Form 1042-S UFI appears in more than one economic form")

    summary_ordinal = economic_count * _PAGES_PER_ECONOMIC_FORM + 1
    summary = _parse_summary(
        physical_pages[summary_ordinal - 1],
        page_ordinal=summary_ordinal,
    )
    page_records.append(
        Robinhood1042SPage(
            summary_ordinal,
            Robinhood1042SPageKind.BROKER_SUMMARY,
        )
    )
    instruction_pages = physical_pages[-3:]
    instruction_kinds: list[Robinhood1042SPageKind] = []
    for index, instruction_page in enumerate(instruction_pages, start=summary_ordinal + 1):
        kind = _instruction_page_kind(instruction_page, index)
        instruction_kinds.append(kind)
        page_records.append(
            Robinhood1042SPage(
                index,
                kind,
            )
        )
    expected_instruction_kinds = [
        Robinhood1042SPageKind.IRS_RECIPIENT_INSTRUCTIONS,
        Robinhood1042SPageKind.IRS_CODES_CH3,
        Robinhood1042SPageKind.IRS_CODES_CH4,
    ]
    if instruction_kinds != expected_instruction_kinds:
        _fail("Form 1042-S instruction pages are missing, duplicated, or reordered")
    tables = _instruction_code_tables(*instruction_pages)
    for copy in copies:
        _validate_copy_codes(copy, tables)
    return Robinhood1042SPacket(
        tax_year=_FORM_YEAR,
        pages=tuple(sorted(page_records, key=lambda page: page.ordinal)),
        copies=tuple(copies),
        summary=summary,
    )


def read_robinhood_1042s_packet(path: Path) -> Robinhood1042SPacket:
    """Extract and parse one private packet without exposing its path."""

    text = pdftotext_layout(
        path,
        doc_kind="Robinhood Form 1042-S PDFs",
        source=_SECTION,
    )
    try:
        return parse_robinhood_1042s_packet(text)
    except ParseError as exc:
        raise exc from None
