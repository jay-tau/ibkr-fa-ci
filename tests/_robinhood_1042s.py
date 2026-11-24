"""Synthetic Robinhood Form 1042-S packet text shared by the Robinhood tests (#1797).

One printed line with each piece at its column (:func:`form_line`), a whole
packet's text (:func:`robinhood_1042s_text`) and that packet with a string
replaced on Copy B and Copy C (:func:`replace_on_both_copies`), such as the
spaced birth date (:data:`SPACED_DOB`).
"""

from __future__ import annotations

_WIDTH = 200


def form_line(*pieces: tuple[int, str]) -> str:
    chars = [" "] * _WIDTH
    for start, value in pieces:
        chars[start : start + len(value)] = value
    return "".join(chars).rstrip()


def _form(
    *,
    copy: str,
    ufi: str,
    income_code: str = "06",
    gross: str = "100.00",
    box5: str = "",
    box6: str = "",
    box7a: str = "25.00",
    box8: str = "-0-",
    box9: str = "0.00",
    box10: str = "25.00",
    box11: str = "",
    amended: str = "",
    amendment_number: str = "",
    chapter3_exemption: str = "00",
    chapter4_exemption: str = "00",
    agent_chapter3_status: str = "03",
    rate: str = "25",
    account: str = "ACCOUNT-TEST",
) -> str:
    lines = [""] * 47
    lines[0] = "Robinhood synthetic Form 1042-S"
    lines[1] = form_line((33, "2025 OMB No. 1545-0096"), (145, f"COPY {copy}"))
    lines[3] = "Department of the Treasury"
    spaced_ufi = " ".join(ufi)
    lines[4] = (
        f"Internal Revenue Service {spaced_ufi} UNIQUE FORM IDENTIFIER "
        f"AMENDED {amended} AMENDMENT NO. {amendment_number}"
        + (" for Recipient" if copy == "C" else "")
    )
    lines[5] = form_line(
        (0, "1 Income"),
        (13, "2 Gross income"),
        (34, "3 Chapter indicator. Enter “3” or “4”"),
        (94, "3"),
        (101, "13e Recipient's U.S. TIN, if any"),
        (150, "13f Ch. 3 status code"),
        (186, "15"),
    )
    lines[6] = form_line(
        (2, "code"),
        (34, "3a Exemption code"),
        (61, chapter3_exemption),
        (67, "4a Exemption code"),
        (85, chapter4_exemption),
        (114, "XXX-XX-9999"),
        (150, "13g Ch. 4 status code"),
        (184, "16"),
    )
    lines[7] = form_line(
        (3, income_code),
        (13, gross),
        (28, "3b Tax rate"),
        (53, f"{rate} .00"),
        (67, "4b Tax rate"),
        (86, "00 .00"),
        (101, "13h Recipient's GIIN"),
        (133, "13i Recipient's foreign tax identification"),
        (178, "13j LOB code"),
    )
    lines[8] = form_line((133, "TIN-TEST-001"))
    lines[9] = form_line((0, "5 Withholding allowance"), (90, box5))
    lines[10] = form_line((0, "6 Net income"), (90, box6))
    lines[11] = form_line(
        (0, "7a Federal tax withheld"),
        (90, box7a),
        (99, "13k Recipient's account number"),
    )
    lines[12] = form_line(
        (0, "7b Check if federal tax withheld was not deposited with the IRS because"),
        (137, account),
    )
    lines[13] = "   escrow procedures were applied (see instructions) . . . . . ."
    lines[14] = form_line((101, "13l Recipient's date of birth (YYYYMMDD)"))
    lines[15] = "7c Check if withholding occurred in subsequent year with respect to a"
    lines[16] = form_line(
        (0, "   partnership interest . . . . . . . . . . . . ."),
        (86, "."),
        (107, "1 9 9 0 0 1 0 1"),
    )
    lines[17] = form_line(
        (0, "7d Check if you are a qualified intermediary, withholding foreign"),
        (101, "14a Primary withholding agent's name (if applicable)"),
    )
    lines[18] = "   partnership, or withholding foreign trust revising its reporting on Form"
    lines[19] = form_line(
        (0, "   1042-S to report to a specific recipient . . . . . . . ."),
        (86, "."),
        (101, "14b Primary withholding agent's EIN"),
        (147, "15 Check if pro-rata basis reporting"),
    )
    lines[20] = form_line((0, f"8 Tax withheld by other agents {box8}"))
    lines[21] = form_line(
        (
            0,
            "9 Overwithheld tax repaid to recipient pursuant to adjustment procedures "
            "(see instructions)",
        ),
        (92, "15a Intermediary or flow-through entity's EIN, if any"),
    )
    lines[22] = form_line((4, f"( {box9} )"), (90, ""))
    lines[23] = form_line(
        (0, "10 Total withholding credit (combine boxes 7a, 8, and 9)"),
        (101, "15d Intermediary or flow-through entity's name"),
    )
    lines[24] = form_line((90, box10))
    lines[25] = form_line(
        (0, f"11 Tax paid by withholding agent (amounts not withheld) (see instructions) {box11}"),
        (101, "15e Intermediary or flow-through entity's GIIN"),
    )
    lines[26] = form_line(
        (101, "15f Country code"),
        (128, "15g Foreign tax identification number, if any"),
    )
    lines[27] = form_line(
        (0, "12a Withholding agent EIN"),
        (47, "12b Ch. 3 status code"),
        (72, "12c Ch. 4 status code"),
    )
    lines[29] = form_line(
        (15, "12-3456789"),
        (56, agent_chapter3_status),
        (81, "04"),
        (101, "15h Address (number and street)"),
    )
    lines[30] = "12d Withholding agent's name"
    lines[31] = form_line(
        (0, "ROBINHOOD SECURITIES LLC"),
        (101, "15i City or town, state or province, country, ZIP or foreign postal code"),
    )
    lines[32] = "12e Withholding agent's global intermediary identification number (GIIN)"
    lines[33] = form_line((101, "16a Payer's name"), (164, "16b Payer's TIN"))
    lines[34] = form_line(
        (0, "12f Country code"),
        (30, "12g Foreign tax identification number, if any"),
    )
    lines[35] = form_line(
        (12, "US"),
        (101, "16c Payer's GIIN"),
        (142, "16d Ch. 3 status code"),
        (171, "16e Ch. 4 status code"),
    )
    lines[36] = form_line(
        (0, "12h Address (number and street)"),
        (62, "Phone: (000) 000-0000"),
    )
    lines[37] = form_line(
        (0, "500 TEST ROAD"),
        (101, "17a State income tax withheld"),
        (139, "17b Payer's state tax no."),
        (164, "17c Name of state"),
    )
    lines[38] = "12i City or town, state or province, country, ZIP or foreign postal code"
    lines[39] = "TEST CITY FL 00000"
    lines[40] = form_line((0, "13a Recipient's name"), (53, "13b Recipient's country code"))
    lines[41] = form_line((0, "TAX PAYER"), (72, "IN"))
    lines[42] = "13c Address (number and street)"
    lines[43] = "1 TEST ROAD"
    lines[44] = "13d City or town, state or province, country, ZIP or foreign postal code"
    lines[45] = "TEST CITY INDIA 000000"
    lines[46] = form_line((126, "Form 1042-S (2025)"))
    return "\n".join(lines)


def _summary(
    rows: tuple[tuple[str, str, str], ...], *, rate: str = "25", account: str = "ACCOUNT-TEST"
) -> str:
    lines = [
        form_line((0, "Robinhood Markets Inc as agent for"), (70, f"Account: {account}")),
        "Robinhood Securities LLC Summary",
        "Foreign Person's U.S. Source Income Subject to Withholding",
        "",
        "",
    ]
    starts = (0, 29, 48, 66, 86, 107)
    for code, gross, withheld in rows:
        lines.extend(
            (
                f"INCOME CODE {code}",
                f"Chapter 3, exemption code 00 - {rate}.00% Chapter 4, exemption code 00 - 00.00%",
                form_line(
                    (29, "Federal Tax"),
                    (48, "Withholding by"),
                    (72, "Tax paid by"),
                    (86, "Tax withholding"),
                    (107, "Amount repaid to"),
                ),
                form_line(
                    (4, "Gross Income"),
                    (31, "Withheld"),
                    (50, "other agents"),
                    (66, "withholding agent"),
                    (95, "credit"),
                    (114, "recipient"),
                ),
                form_line(
                    *tuple(
                        zip(
                            starts,
                            (gross, withheld, "", "", withheld, ""),
                            strict=True,
                        )
                    ),
                ),
                "",
                "",
            )
        )
    return "\n".join(lines)


def robinhood_1042s_text(
    *,
    ufis: tuple[str, ...] = ("1234567890",),
    mismatch_copy: bool = False,
    amended: bool = False,
    chapter3_exemption: str = "00",
    chapter4_exemption: str = "00",
    agent_chapter3_status: str = "03",
    income_code: str = "06",
    gross: str = "100.00",
    tax: str = "25.00",
    rate: str = "25",
    account: str = "ACCOUNT-TEST",
) -> str:
    pages: list[str] = []
    summary_rows: list[tuple[str, str, str]] = []
    codes: list[str] = []
    for index, ufi in enumerate(ufis):
        code = income_code if index == 0 else "34"
        amount = gross if index == 0 else "50.00"
        withheld = tax if index == 0 else "25.00"
        b = _form(
            copy="B",
            ufi=ufi,
            income_code=code,
            gross=amount,
            box7a=withheld,
            box10=withheld,
            amended="X" if amended else "",
            chapter3_exemption=chapter3_exemption,
            chapter4_exemption=chapter4_exemption,
            agent_chapter3_status=agent_chapter3_status,
            rate=rate,
            account=account,
        )
        c = _form(
            copy="C",
            ufi=ufi,
            income_code=code,
            gross="51.00" if mismatch_copy and index == 0 else amount,
            box7a=withheld,
            box10=withheld,
            amended="X" if amended else "",
            chapter3_exemption=chapter3_exemption,
            chapter4_exemption=chapter4_exemption,
            agent_chapter3_status=agent_chapter3_status,
            rate=rate,
            account=account,
        )
        pages.extend(
            (
                b,
                "This page intentionally left blank.",
                c,
                "This page intentionally left blank.",
            )
        )
        summary_rows.append((code, amount, withheld))
        codes.append(code)
    pages.append(_summary(tuple(summary_rows), rate=rate, account=account))
    listed = "\n".join(f"{code} Listed income code" for code in codes)
    pages.extend(
        (
            "U.S. Income Tax Filing Requirements\nExplanation of Codes\n" + listed,
            "\n".join(
                (
                    form_line(
                        (0, "Explanation of Codes (continued)"),
                        (100, "11 Chapter 3 exemption  12 Chapter 3 exemption"),
                    ),
                    form_line((100, "Chapter 4")),
                    form_line((100, "00 No exemption")),
                    form_line((100, "15 Payee not subject to withholding")),
                    "Boxes 12b, 12c, 13f, 13g status codes",
                    form_line((100, "03 Agent status")),
                    form_line((100, "15 Recipient status")),
                    "Boxes 3a and 4a. Exemption Code",
                    "00 No exemption",
                )
            ),
            "\n".join(
                (
                    "Explanation of Codes (continued)",
                    "Chapter 4 Status Codes",
                    "01 Shared status",
                    "04 Agent status",
                    "16 Recipient status",
                )
            ),
        )
    )
    return "\f".join(pages) + "\f"


SPACED_DOB = "1 9 9 0 0 1 0 1"


def replace_on_both_copies(old: str, new: str) -> str:
    """The test packet with ``old`` replaced on Copy B and Copy C."""
    text = robinhood_1042s_text()
    assert text.count(old) == 2
    return text.replace(old, new)
