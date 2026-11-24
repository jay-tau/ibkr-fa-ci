"""Parse Fidelity NetBenefits ESPP trade-confirmation PDFs into raw purchases.

The confirmation PDF is the **only** document that states the FMV at purchase
("Market Value at Purchase") — a *total* (not per-share) — which ADR-0003 fixes
as both the Schedule FA A3 "Initial value" and the §49(2AA) capital-gains cost
basis. It is also the only full evidence of the perquisite (= FMV minus
contribution, the document's "Gain"). So it must be parsed before an A3 lot is
assembled, and never fabricated.

The model below captures only what the document evidences. There is **no ISIN**
on the confirmation (it carries SYMBOL + CUSIP); ISIN comes from
``[netbenefits.security]`` config and is joined downstream, never invented here.
The Yahoo-close FMV cross-check and the discount-identity check need data the PDF
does not carry (a price feed, the plan discount %), so they belong to the
consumer (#310), not this parser. Everything verified here is **document
-internal** — figures cross-checked against each other, refusing on any mismatch.

Parsing runs against the text of ``pdftotext -layout`` (already on PATH — no new
dependency). Tests run against committed text fixtures, never binary PDFs.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.number_checks import NumberKind, number_limit_breach

_SECTION: Final[str] = "Fidelity ESPP confirmation"
# "Accumulated Contributions" is printed twice (PURCHASE INFORMATION + EXPLANATION
# OF PROCEEDS); both must be present and equal.
_CONTRIB_OCCURRENCES: Final[int] = 2

# Locale-proof month parsing (``%b`` is locale-sensitive; Fidelity emits English
# upper-case abbreviations like ``DEC/31/2025``).
_MONTHS: Final[dict[str, int]] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}  # fmt: skip

# A money figure: either grouped thousands (``1,234.56``) or plain (``743.30``),
# always two decimals. A malformed filed amount (e.g. ``1,23.45``) matches
# nothing and so refuses, rather than being silently re-grouped.
# The trailing ``(?!\S)`` makes the amount end at whitespace or the end of the
# text, so a corrupt figure like ``$743.300``, ``$743.30x``, ``$743.30,00`` or
# ``$74.34-`` refuses rather than being silently truncated to its valid prefix
# (a trailing sign would otherwise flip the filed value's meaning).
_MONEY: Final[str] = r"(?:\d{1,3}(?:,\d{3})*|\d+)\.\d{2}(?!\S)"
# A plain quantity / per-share price (Fidelity prints up to 4 decimals), with the
# same trailing guard against silent truncation (``3.482x`` and ``3.482-`` refuse).
_NUM: Final[str] = r"\d+(?:\.\d+)?(?!\S)"
# An upper-case ``MMM/DD/YYYY`` date; the trailing guard refuses ``DEC/31/20250``.
# A trailing ``.`` is allowed on purpose: the purchase date legitimately ends a
# sentence ("...PLAN on DEC/31/2025."), and a date is fixed-width so a trailing
# ``.`` cannot truncate any significant digit.
_MDY: Final[str] = r"[A-Z]{3}/\d{2}/\d{4}(?![\w/])"

_PARTICIPANT_RE: Final = re.compile(r"PARTICIPANT NO\.\s+([A-Z0-9]+)(?![\w.])")
# The single confirmation data row: participant, type/reg.rep, trade date,
# settlement date, trans no, CUSIP. Anchored on the two ``MM-DD-YY`` dates plus
# the trailing 9-char CUSIP, which only this row carries. The ``(?![\w.])`` guards
# reject a corrupt participant/CUSIP (extra letter/digit/underscore/dot) instead
# of truncating it.
_ROW_RE: Final = re.compile(
    r"([A-Z0-9]+)\s+\d+\s+\d+\s+(\d{2}-\d{2}-\d{2})\s+(\d{2}-\d{2}-\d{2})\s+\w+\s+([0-9A-Z]{9})(?![\w.])"
)
_SYMBOL_RE: Final = re.compile(r"SYMBOL:\s*(\S+)")
# The issuer name printed on the line directly under the "SECURITY DESCRIPTION"
# header, in the left column (e.g. "EXAMPLE CORP"). Captured up to a 2+ space
# column gap (the pdftotext -layout column delimiter) so a right-column value
# can't bleed in. Starts with [A-Z0-9] because a company name may begin with a
# digit ("3M CO"). A small leading run of spaces/tabs/form-feeds is allowed (a
# page break lands a `\f` before a header) — but BOUNDED ({0,8}) so a blank left
# column can't skip ~40 spaces to capture the far right-column label instead of
# refusing; mirrors the statement parser's `[ \t\f]*` leading class.
_SECURITY_DESC_RE: Final = re.compile(
    r"^[ \t\f]*SECURITY DESCRIPTION\b.*\n[ \t\f]{0,8}(?P<name>[A-Z0-9][^\n]*?)(?:[ \t]{2,}.*)?$",
    re.MULTILINE,
)
_PURCHASE_RE: Final = re.compile(rf"YOU PURCHASED\s+({_NUM})\s+AT\s+\$({_NUM})\s+PURCHASE")
_FMV_RE: Final = re.compile(rf"Market Value at Purchase\S*\s+\$({_MONEY})")
_GAIN_RE: Final = re.compile(rf"Gain\S*\s+\$({_MONEY})")
# Excludes the footnote "Accumulated Contributions are the amount..." (no ``$``).
_CONTRIB_RE: Final = re.compile(rf"Accumulated Contributions\*?\s+\$({_MONEY})")
_OFFERING_RE: Final = re.compile(rf"Offering period:\s+({_MDY})\s*-\s*({_MDY})")
_PURCHASE_DATE_RE: Final = re.compile(rf"PLAN on\s+({_MDY})")
_SHARE_PROCEEDS_RE: Final = re.compile(rf"Share Proceeds\*+\s+({_NUM})")


@dataclass(frozen=True, slots=True)
class EspPurchase:
    """One ESPP purchase from a Fidelity trade-confirmation PDF.

    Every field is parsed verbatim from the document. ISIN is **not** present on
    the confirmation (joined from config downstream).

    Attributes:
        purchase_date: The purchase date ("...PLAN on <date>").
        settlement_date: The settlement date on the confirmation data row (on or
            after the trade date, which is the purchase date).
        offering_period_start / offering_period_end: The "Offering period".
        symbol: Ticker ("SYMBOL: XESP").
        security_description: The issuer name printed under "SECURITY
            DESCRIPTION" (e.g. "EXAMPLE CORP") — the A3 "Name of entity".
        cusip: 9-character CUSIP from the confirmation data row.
        participant_no: Fidelity participant number.
        shares: Shares purchased ("YOU PURCHASED <n> AT ...").
        purchase_price_per_share_usd: The discounted per-share price paid.
        fmv_total_usd: "Market Value at Purchase" — the FMV **total** (shares x
            per-share FMV). The A3 "Initial value" / §49(2AA) cost basis.
        contribution_usd: "Accumulated Contributions" — the discounted total
            actually paid (the US basis).
        perquisite_usd: "Gain" — the §17(2)(vi) perquisite (FMV minus contribution).
    """

    purchase_date: _dt.date
    settlement_date: _dt.date
    offering_period_start: _dt.date
    offering_period_end: _dt.date
    symbol: str
    security_description: str
    cusip: str
    participant_no: str
    shares: Decimal
    purchase_price_per_share_usd: Decimal
    fmv_total_usd: Decimal
    contribution_usd: Decimal
    perquisite_usd: Decimal

    @property
    def fmv_per_share_usd(self) -> Decimal:
        """FMV per share, derived (``fmv_total_usd / shares``). Not stored — the
        document evidences only the total; consumers quantize as needed."""
        return self.fmv_total_usd / self.shares


def _fail(message: str, *, source: str, field: str | None = None) -> ParseError:
    return ParseError(section=source, row_index=0, column=field, message=message, raw_value="")


def _only(pattern: re.Pattern[str], text: str, *, field: str, source: str) -> re.Match[str]:
    """Return the sole match, refusing if a field is missing or duplicated (a
    duplicate signals a concatenated/spoofed document)."""
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise _fail(
            f"expected exactly one {field}; found {len(matches)}", source=source, field=field
        )
    return matches[0]


def _money(raw: str, *, field: str, source: str, kind: NumberKind = NumberKind.MONEY) -> Decimal:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - regex pre-validates format
        raise _fail(f"{field} is not a number", source=source, field=field) from exc
    if not value.is_finite() or value <= 0:
        raise _fail(f"{field} must be a positive amount", source=source, field=field)
    breach = number_limit_breach(value, kind)
    if breach is not None:
        raise _fail(breach, source=source, field=field)
    return value


def _mdy(raw: str, *, field: str, source: str) -> _dt.date:
    month, day, year = raw.split("/")
    with contextlib.suppress(KeyError, ValueError):
        return _dt.date(int(year), _MONTHS[month], int(day))
    # Refused outside the handler, so no chained error names the month or the date's parts.
    raise _fail(f"{field} is not a valid date", source=source, field=field)


def _short_date(raw: str, *, field: str, source: str) -> _dt.date:
    # ``MM-DD-YY`` from the data row; the two-digit year is a 20xx confirmation.
    month, day, year = raw.split("-")
    with contextlib.suppress(ValueError):
        return _dt.date(2000 + int(year), int(month), int(day))
    # Refused outside the handler, so no chained error names the date's parts.
    raise _fail(f"{field} is not a valid date", source=source, field=field)


def _require_close(
    got: Decimal, expected: Decimal, tol: Decimal, *, what: str, field: str, source: str
) -> None:
    if abs(got - expected) > tol:
        raise _fail(
            f"{what}, but they differ by more than the rounding tolerance",
            source=source,
            field=field,
        )


def parse_confirmation(text: str, *, source: str = _SECTION) -> EspPurchase:
    """Parse ``pdftotext -layout`` output of one confirmation into an
    :class:`EspPurchase`, refusing on any missing field or internal mismatch.

    Pure (text in, model out) for fixture-based testing. ``source`` labels the
    document in error messages.

    Raises:
        ParseError: A field is missing, duplicated or malformed, or the document's
            figures disagree. The refusal names ``source`` and the field, never a
            value from the document.
    """
    row = _only(_ROW_RE, text, field="confirmation data row", source=source)
    row_participant, trade_raw, settle_raw, cusip = row.groups()
    header_participant = _only(_PARTICIPANT_RE, text, field="PARTICIPANT NO.", source=source).group(
        1
    )
    if header_participant != row_participant:
        raise _fail(
            "the PARTICIPANT NO. header differs from the participant number on the "
            "confirmation data row",
            source=source,
            field="PARTICIPANT NO.",
        )

    symbol = _only(_SYMBOL_RE, text, field="SYMBOL", source=source).group(1)
    security_description = (
        _only(_SECURITY_DESC_RE, text, field="SECURITY DESCRIPTION", source=source)
        .group("name")
        .strip()
    )
    purchase = _only(_PURCHASE_RE, text, field="YOU PURCHASED line", source=source)
    shares = _money(purchase.group(1), field="shares", source=source, kind=NumberKind.QUANTITY)
    price = _money(purchase.group(2), field="purchase price", source=source, kind=NumberKind.PRICE)

    fmv_total = _money(
        _only(_FMV_RE, text, field="Market Value at Purchase", source=source).group(1),
        field="Market Value at Purchase",
        source=source,
    )
    perquisite = _money(
        _only(_GAIN_RE, text, field="Gain", source=source).group(1), field="Gain", source=source
    )

    contribs = _CONTRIB_RE.findall(text)
    if len(contribs) != _CONTRIB_OCCURRENCES:
        raise _fail(
            f"expected exactly two Accumulated Contributions; found {len(contribs)}", source=source
        )
    contribution = _money(contribs[0], field="Accumulated Contributions", source=source)
    if _money(contribs[1], field="Accumulated Contributions", source=source) != contribution:
        raise _fail("the two Accumulated Contributions figures differ", source=source)

    share_proceeds = _money(
        _only(_SHARE_PROCEEDS_RE, text, field="Share Proceeds", source=source).group(1),
        field="Share Proceeds",
        source=source,
        kind=NumberKind.QUANTITY,
    )
    if share_proceeds != shares:
        raise _fail(
            "Share Proceeds does not equal the shares purchased",
            source=source,
            field="Share Proceeds",
        )

    offering = _only(_OFFERING_RE, text, field="Offering period", source=source)
    offering_start = _mdy(offering.group(1), field="offering start", source=source)
    offering_end = _mdy(offering.group(2), field="offering end", source=source)
    purchase_date = _mdy(
        _only(_PURCHASE_DATE_RE, text, field="purchase date", source=source).group(1),
        field="purchase date",
        source=source,
    )
    trade_date = _short_date(trade_raw, field="trade date", source=source)
    settle_date = _short_date(settle_raw, field="settlement date", source=source)

    # Document-internal cross-checks — refuse on any mismatch.
    if trade_date != purchase_date:
        raise _fail(
            "the trade date does not equal the purchase date", source=source, field="trade date"
        )
    if settle_date < trade_date:
        raise _fail(
            "the settlement date precedes the trade date", source=source, field="settlement date"
        )
    if not offering_start <= purchase_date <= offering_end:
        raise _fail(
            "the purchase date is outside the offering period",
            source=source,
            field="purchase date",
        )
    # All three are exact 2-dp figures the document prints, so the perquisite
    # identity must hold exactly — a 1-cent drift is a wrong filed figure.
    if fmv_total - contribution != perquisite:
        raise _fail(
            "Market Value at Purchase minus Accumulated Contributions does not equal Gain",
            source=source,
            field="Gain",
        )
    # Fidelity prints shares to 3-4 dp (e.g. "3.482"), so the printed
    # share count carries up to a 3-dp half-ULP of rounding; |shares*price -
    # contribution| <= price * 0.0005 (+ a half-cent for the contribution's own
    # rounding). This still catches any dollar-level / column-shift corruption, and
    # contribution itself is exactly guarded by the perquisite identity above.
    _require_close(
        shares * price,
        contribution,
        price * Decimal("0.0005") + Decimal("0.005"),
        what="shares x purchase price should equal Contributions",
        field="Accumulated Contributions",
        source=source,
    )

    return EspPurchase(
        purchase_date=purchase_date,
        settlement_date=settle_date,
        offering_period_start=offering_start,
        offering_period_end=offering_end,
        symbol=symbol,
        security_description=security_description,
        cusip=cusip,
        participant_no=header_participant,
        shares=shares,
        purchase_price_per_share_usd=price,
        fmv_total_usd=fmv_total,
        contribution_usd=contribution,
        perquisite_usd=perquisite,
    )
