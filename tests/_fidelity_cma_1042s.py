"""Synthetic Fidelity CMA Form 1042-S packets: parsed, and in the observed
``pdftotext -layout`` shape.

Every value is synthetic. Labels are the IRS form's own wording, placed at
the columns the packet parser reads.
"""

from __future__ import annotations

from collections.abc import Iterable

from ibkr_fa.parser.fidelity_cma_1042s import Form1042SPacket, Form1042SRecord
from tests._fidelity_cma import _ACCOUNT, _HOLDER


def packet(*records: Form1042SRecord) -> Form1042SPacket:
    """The parsed 2025 packet for ``TAX PAYER``'s CMA with ``records``."""
    return Form1042SPacket(
        tax_year=2025,
        account_id=_ACCOUNT,
        account_holder=_HOLDER,
        recipient_country_code="IN",
        withholding_agent_name="NATIONAL FINANCIAL SERVICES LLC",
        withholding_agent_ein="04-3523567",
        records=records,
    )


# Copy B and D print their header on lines 0-6 with the UFI line last; Copy C
# prints it on lines 0-5, its UFI one line above "UNIQUE FORM IDENTIFIER".
_HEADER_TOP = (
    (
        (0, "Form"),
        (8, "1042-S"),
        (42, "Foreign Person\u2019s U.S. Source Income Subject to Withholding"),
    ),
    (
        (43, "Go to www.irs.gov/Form1042S for instructions and the latest information."),
        (173, "2025"),
        (194, "OMB No. 1545-0096"),
    ),
)
_AMENDMENT = ((96, "UNIQUE FORM IDENTIFIER"), (144, "AMENDED"), (166, "AMENDMENT NO."))
_DOTS_7B = " ." * 19
_DOTS_7C = " ." * 44
_DOTS_7D = " ." * 28


def line(*parts: tuple[int, str]) -> str:
    """Place each text at its column, left to right."""
    text = ""
    for column, part in parts:
        assert len(text) <= column, (column, part)
        text = text.ljust(column) + part
    return text


def _right(end: int, text: str) -> tuple[int, str]:
    return (end - len(text), text)


def _header(copy: str, ufi: str) -> list[str]:
    spaced_ufi = (44, " ".join(ufi))
    top = [line(*parts) for parts in _HEADER_TOP]
    if copy == "B":
        return [
            *top,
            "",
            line((200, "COPY B")),
            line((0, "Department of the Treasury"), (198, "for Recipient")),
            "Internal Revenue Service",
            line(spaced_ufi, *_AMENDMENT),
        ]
    if copy == "C":
        return [
            *top,
            line((197, "COPY C for")),
            "Department of the Treasury",
            line(spaced_ufi, (195, "Recipient Attach to any")),
            line(
                (0, "Internal Revenue Service"), *_AMENDMENT, (196, "Federal tax return you file")
            ),
        ]
    assert copy == "D"
    return [
        *top,
        "",
        line((199, "COPY D for")),
        line((0, "Department of the Treasury"), (195, "Recipient Attach to any")),
        "Internal Revenue Service",
        line(spaced_ufi, *_AMENDMENT, (197, "state tax return you file")),
    ]


def form_page(
    copy: str,
    ufi: str,
    *,
    account: str,
    holder: str,
    income_code: str,
    exemption_code: str,
    rate: str,
    gross: int,
    federal_tax: int,
) -> str:
    """One copy page with synthetic header, box values and identity rows."""
    body = [
        "",
        line(
            (0, "1 Income"),
            (15, "2 Gross income 3 Chapter indicator. Enter \u201c3\u201d or \u201c4\u201d"),
            (98, "3"),
            (119, "13d City or town, state or province, country, ZIP or foreign postal code"),
        ),
        line((2, "code"), (121, "SYNTHETIC CITY"), (139, "000000")),
        line(
            (7, income_code),
            _right(28, f"{gross}.00"),
            (40, "3a Exemption code"),
            (73, exemption_code),
            (76, "4a Exemption code"),
            (114, "15"),
            (117, "13e Recipient\u2019s U.S. TIN, if any"),
            (175, "13f Ch. 3 status code"),
        ),
        "",
        # pdftotext prints the 4b label one space after a two-digit 3b rate.
        line(
            (40, "3b Tax rate"),
            (65, rate),
            (79 if len(rate) == 4 else 66 + len(rate), "4b Tax rate"),
            _right(108, "0.00"),
            (175, "13g Ch. 4 status code"),
        ),
        line((0, "5 Withholding allowance"), _right(104, "0.00")),
        line(
            (119, "13h Recipient\u2019s GIIN"),
            (158, "13i Recipient\u2019s foreign tax identification"),
            (203, "13j LOB code"),
        ),
        line((161, "number, if any")),
        line((0, "6 Net income"), _right(104, "0.00")),
        "",
        line((0, "7a Federal tax withheld"), _right(104, f"{federal_tax}.00")),
        "",
        "7b Check if federal tax withheld was not deposited with the IRS because",
        line(
            (3, "escrow procedures were applied (see instructions)" + _DOTS_7B),
            (119, "13k Recipient\u2019s account number"),
        ),
        line((124, account)),
        "7c Check if withholding occurred in subsequent year with respect to a",
        line(
            (3, "partnership interest" + _DOTS_7C),
            (119, "13l Recipient\u2019s date of birth (YYYYMMDD)"),
        ),
        line(
            (0, "7d Check if you are a qualified intermediary,withholding foreign"),
            *zip((127, 133, 140, 147, 154, 162, 169, 176), "19900101", strict=True),
        ),
        "   partnership, or withholding foreign trust revising its reporting on Form",
        "   1042-S to report to a specific recipient" + _DOTS_7D,
        line((119, "14a Primary withholding agent\u2019s name (if applicable)")),
        line((0, "8 Tax withheld by other agents"), _right(104, "0.00")),
        "",
        line(
            (0, "9 Overwithheld tax repaid to recipient pursuant to adjustment"),
            (62, "procedures(see instructions)"),
            (119, "14b Primary withholding agent\u2019s EIN"),
        ),
        line((168, "15 Check if pro-rata basis reporting")),
        "",
        line((3, "("), _right(104, "0.00"), (115, ")")),
        "",
        "10 Total withholding credit (combine boxes 7a, 8, and 9)",
        line(_right(104, f"{federal_tax}.00")),
        "",
        line(
            (0, "11 Tax paid by withholding agent (amounts not withheld) (see instructions)"),
            (119, "15d Intermediary or flow-through entity\u2019s name"),
        ),
        "",
        line(
            (0, "12a Withholding agent\u2019s EIN"),
            (45, "12b Ch. 3 status code"),
            (81, "12c Ch. 4 status code"),
            (119, "15e Intermediary or flow-through entity\u2019s GIIN"),
        ),
        line((12, "04-3523567"), (59, "15"), (97, "02")),
        "12d Withholding agent\u2019s name",
        line((4, "NATIONAL FINANCIAL SERVICES LLC")),
        line((0, "13a Recipient\u2019s name"), (81, "13b Recipient\u2019s country code")),
        line((7, holder), (97, "IN")),
        "",
        line((191, "Form 1042-S (2025)")),
    ]
    return "\n".join([*_header(copy, ufi), *body]) + "\n"


def packet_text(
    records: Iterable[tuple[str, str, str, str, int, int]],
    *,
    account: str,
    holder: str,
) -> str:
    """The 20-page packet: two preface pages, then B, C, D per record with blank backs.

    Each record is ``(ufi, income_code, exemption_code, rate, gross, federal_tax)``.
    """
    pages = ["Fidelity tax packet preface", "Recipient instructions"]
    for ufi, income_code, exemption_code, rate, gross, federal_tax in records:
        for copy in "BCD":
            pages.extend(
                [
                    form_page(
                        copy,
                        ufi,
                        account=account,
                        holder=holder,
                        income_code=income_code,
                        exemption_code=exemption_code,
                        rate=rate,
                        gross=gross,
                        federal_tax=federal_tax,
                    ),
                    "",
                ]
            )
    return "\f".join(pages) + "\f"
