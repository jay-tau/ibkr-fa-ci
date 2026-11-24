"""IBKR's Flex Query header and trailer records for synthetic test files (#1313).

Schedule FA reads a Flex Query only with its ``BOF`` and ``EOF`` records
(IBKR's Reporting Reference, "Delivery Configuration and General
Configuration"), so a test that files a Flex Query wraps it in them.

The marked records carry a digit-run marker in every ``BOF`` field the tool
doesn't read and in the ``EOF`` account ID, so a test can check that no
refusal or output contains one of those markers. The two refusal builders give the
``ParseError`` for records that don't match IBKR's documented layout and for
a file that isn't UTF-8 text.
"""

from __future__ import annotations

from ibkr_fa.exceptions import ParseError


def bof_record(
    start: str = "20250101",
    end: str = "20251231",
    *,
    account: str = "U12345678",
    sections: str = "3",
    generated: str = "20260105;101500",
    builds: tuple[str, str] = ("100", "100"),
) -> str:
    """A ``BOF`` record line: account ID, section count, from and to dates,
    generation date and time, and the two build numbers."""
    cells = ("BOF", account, sections, start, end, generated, *builds)
    return ",".join(f'"{cell}"' for cell in cells) + "\n"


def eof_record(account: str = "U12345678") -> str:
    """An ``EOF`` record line."""
    return f'"EOF","{account}"\n'


def with_flex_records(text: str, *, calendar_year: int = 2025, start: str | None = None) -> str:
    """``text`` between a ``BOF`` record covering ``calendar_year`` and an ``EOF``
    record; the period starts on ``start`` (``YYYYMMDD``), by default Jan 1."""
    body = text if text.endswith("\n") else text + "\n"
    first = start or f"{calendar_year}0101"
    return bof_record(first, f"{calendar_year}1231") + body + eof_record()


_LAYOUT = (
    "the Flex Query's header and trailer records don't match IBKR's documented layout: "
    "{rule}. Re-export the Flex Query."
)


def flex_layout_refusal(line: int, rule: str) -> ParseError:
    return ParseError(
        section="<file>",
        row_index=line,
        column=None,
        message=_LAYOUT.format(rule=rule),
        raw_value="",
    )


BOF_DATE_RULE = "a BOF date isn't YYYYMMDD"


def _row(*cells: str) -> str:
    return ",".join(f'"{cell}"' for cell in cells) + "\n"


# Digit runs in every BOF field the tool doesn't read, in the EOF account ID,
# and in a malformed BOF date.
_BOF_ACCOUNT = "81726354"
_SECTION_COUNT = "92837465"
_GENERATED = "918273"
_BUILDS = ("64738291", "57382910")
_EOF_ACCOUNT = "46372819"
_BAD_DATE = "47182936"
FLEX_RECORD_MARKERS = (_BOF_ACCOUNT, _SECTION_COUNT, _GENERATED, *_BUILDS, _EOF_ACCOUNT, _BAD_DATE)


def marked_bof(start: str = "20250101", end: str = "20251231") -> str:
    """A ``BOF`` record carrying a marker in every field the tool doesn't read."""
    return bof_record(
        start,
        end,
        account=f"U{_BOF_ACCOUNT}",
        sections=_SECTION_COUNT,
        generated=f"20260105;{_GENERATED}",
        builds=_BUILDS,
    )


def marked_eof() -> str:
    return eof_record(f"U{_EOF_ACCOUNT}")


def late_non_utf8_records(text: str) -> bytes:
    """An invalid byte in EOF's account cell, beyond the first decoder buffer."""
    padding = _row("CurrencyPrimary", "SampleField") + _row("USD", "unused") * 2048
    footer = marked_eof().encode().replace(_EOF_ACCOUNT.encode(), _EOF_ACCOUNT.encode() + b"\xff")
    return (marked_bof() + text + padding).encode() + footer


def utf8_refusal() -> ParseError:
    return ParseError(
        section="<file>",
        row_index=0,
        column=None,
        message="the file is not UTF-8 text",
        raw_value="",
    )
