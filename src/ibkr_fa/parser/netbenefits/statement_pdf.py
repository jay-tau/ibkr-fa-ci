"""Parse the identity, period, holdings and Dec-31 NAV of a Fidelity SPS statement.

The dividend / Non-Resident Tax withholding / money-market sweep, the per-month
valuations and the Dec-31 account NAV live **only** in the Fidelity Stock Plan
Services (SPS) PDF statements. This reader parses the statement **identity +
period** (#311) plus the **Holdings table** and the **Dec-31 account NAV** (#312).
The dividend / WHT / MMKT section parser (#I9) extends it further.

The top-of-page header — the report-type
line, the very next ``Month D, YYYY - Month D, YYYY`` period line, then the
account holder and ``Participant Number:`` — REPEATS once per content page. Every
header block found is required to be identical; any disagreement (e.g. a stapled
multi-statement PDF) refuses. The custodian ("Fidelity Stock Plan Services LLC")
appears only in the page-footer disclosure and is asserted present as a
document-type guard. An optional ``expected_participant_no`` (the config value,
passed as a primitive so the parser stays config-free) is cross-checked and
refuses on mismatch.

A statement is either **priced** (with a ``Holdings`` table and an
``Ending Account Value Incl. AI $X`` line) or **unpriced** (with unavailable
valuation fields). The two states are
mutually exclusive and exactly one must hold (else refuse); the unpriced state is
represented as ``holdings = ()`` and ``account_nav_usd = None``.

The income detail sections (``Dividends, Interest & Other Income``, ``Taxes
Withheld``, ``Core Fund Activity``) are optional: a period without income prints
none of them. On a Stock Plan Services report, the dividend section's ``Total`` must
equal the ``Income Summary`` block's This Period ``Total``, and the withholding
section's ``Total`` must equal the account summary's ``Taxes Withheld`` line. Both
summaries are printed only once the year has income, so an absent summary means
zero, but a nonzero summary without its section refuses. On a report that starts on
January 1 and ends in the same year, This Period and Year-to-Date cover the same
days, so each summary's two columns must be equal. A statement missing both
a summary and its section still parses as zero; catching that would need the
Year-to-Date columns chained across statements. A year-end report usually prints no
detail sections, so its summaries are checked for shape and not read. Any detail
rows it does print are read and returned like a Stock Plan Services report's. Its
silence proves nothing, but an income row it prints must match the Stock Plan
Services reports (#1249).

An ESPP purchase is read too. Its cash: the ``SPP PURCHASE CREDIT`` rows must sum to
``Total Other Activity In``, which ``Total Securities Bought`` must spend exactly. Its
rows sit in the statement's one ``Securities Bought & Sold`` table: the title, the
two-line column header (page-margin lines, which hold no lower-case letter, may sit
between the two), the purchase rows, then ``Total Securities Bought`` and ``Net
Securities Bought & Sold``. A purchase row is ``<marker> MM/DD <NAME> ESPP### <9-char
id> You Bought <3-dp quantity> $<5-dp price> - -$<amount>``; its marker, one
lower-case letter, needs exactly one legend line, ``<marker> Purchase of stock through
your company stock plan.``, which may run on. Each row's date must fall in the period,
its quantity and price must be positive, and its amount must be an outflow equal to
quantity x price within the Holdings tolerance. The rows must sum to ``Total
Securities Bought``, which ``Net Securities Bought & Sold`` must equal. Every line
naming ``Securities Bought`` or ``Net Securities`` must be that table's title, ``Total``
or ``Net`` line. Each row is returned with its settlement date, identifier, quantity,
price and amount, in table order.

The whole statement is then checked for dated lines and lines starting with the word
``Total``. A dated line holds an ``MM/DD`` token anywhere, whatever precedes it: two
digits, ``/`` and two digits, with the line start or whitespace before it and
whitespace or the line end after it. Dated or ``Total`` text after a page break
(``\\f``) is first put on a line of its own. Each such line must be one the parser
read: a section's row or ``Total``, an ESPP purchase's cash line or row, a holdings
row or total, the ``Income Summary`` ``Total`` row, or an identity line. The only
kinds accepted without being read are a year-end report's summaries (shape-checked)
and five ``Total`` lines of exact shapes that aren't cash activity: ``Total Common
Stock``, ``Total Including Other Holdings``, the asset allocation's ``Total``,
``Total for all Offering Periods`` and a cost-basis footnote. Any other dated
or ``Total`` line refuses, so cash activity this parser can't read never parses as
absent.

Parsing is pure over ``pdftotext -layout`` text; tests run against committed
synthetic text fixtures, never binary PDFs.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.parser._fidelity_text import MONEY_DOLLAR_OPTIONAL as _MONEY_BARE
from ibkr_fa.parser._fidelity_text import MONEY_DOLLAR_REQUIRED as _MONEY
from ibkr_fa.parser._fidelity_text import PRICE_DOLLAR_OPTIONAL as _PRICE_BARE
from ibkr_fa.parser._fidelity_text import WHOLE_NUMBER as _UNSIGNED
from ibkr_fa.parser._fidelity_text import YEARLESS_DATE as _TXN_DATE
from ibkr_fa.parser._fidelity_text import FullDateFailure, full_date, yearless_dates

_SECTION: Final[str] = "Fidelity SPS statement"
_CENT: Final[Decimal] = Decimal("0.01")
# ``_UNSIGNED`` (the whole-number part of every amount, price and quantity below) and
# the ``_MONEY``, ``_MONEY_BARE`` and ``_PRICE_BARE`` tokens come from
# :mod:`ibkr_fa.parser._fidelity_text`. Fidelity prints the ``$`` only on the first
# row of a money or price column, so continuation rows may be bare (``0.01``,
# ``1.0000``): ``_MONEY_BARE`` and ``_PRICE_BARE`` allow that. Its Federal withholding
# subtotal is the detail-section total exception; other section and holdings totals
# use the strict ``_MONEY`` token.
# A per-unit price prints 4 decimals.
_PRICE: Final[str] = rf"\${_UNSIGNED}\.[0-9]{{4}}"
# A share quantity (no ``$``); the holdings table prints exactly 3 decimals.
# Fixing the precision is what makes the ``ending ~= qty x price`` tolerance safe:
# a truncated quantity (``3.4`` for ``3.482``) must refuse, not parse with a wider
# derived tolerance.
_QTY: Final[str] = rf"{_UNSIGNED}\.[0-9]{{3}}"
# The displayed quantity is rounded to 3 dp while the ending value uses the true
# quantity, so the cross-check tolerance is the 3-dp half-ULP x price (plus a cent).
_QTY_HALF_ULP: Final[str] = "0.0005"
# The custodian disclosure that identifies a genuine Fidelity SPS statement. Both
# the comma-less form (the statements) and the comma form (e.g. "Fidelity Stock
# Plan Services, LLC", which Fidelity also uses) are accepted so a legitimate
# statement is never falsely refused; the matched text is stored verbatim.
_CUSTODIAN_RE: Final[re.Pattern[str]] = re.compile(r"Fidelity Stock Plan Services,? LLC")

# Report-type phrases as they appear in the PDF (the year-end one is prefixed with
# the year, e.g. "2025 YEAR-END INVESTMENT REPORT", matched by the optional year).
_SPS_PHRASE: Final[str] = "STOCK PLAN SERVICES REPORT"
_YEAR_END_PHRASE: Final[str] = "YEAR-END INVESTMENT REPORT"

_DATE: Final[str] = r"[A-Z][a-z]+ [0-9]{1,2}, [0-9]{4}"

# The statement identity is cross-checked across the per-page repetition, since
# the layout repeats different parts of the header on each page:
#
# * the report-type + period line pair repeats on EVERY page (``pdftotext``
#   prefixes page-2+ headers with a form-feed ``\f`` before the indentation, hence
#   ``\f`` in the leading class);
# * the ``Participant Number:`` line repeats on every page;
# * the account-holder name appears ONLY in the page-1 header, on the line
#   immediately above its ``Participant Number:`` (continuation pages omit it).
#
# Each repeated signal is collected and required to be internally consistent (a
# disagreement signals a stapled / merged / corrupted multi-statement PDF, which
# refuses). Every *occurrence* of a signal's label (the report-type phrase / the
# ``Participant Number:`` label) must STRICT-match its pattern: a malformed line on
# one page (e.g. a trailing token) must refuse, not be silently skipped while the
# clean pages agree. The year-end title carries a leading year (e.g. "2025
# YEAR-END INVESTMENT REPORT"), captured and required to match the period year;
# the monthly title never carries a year. The participant capture is anchored to
# end-of-line so a trailing token (corrupt / extra field) refuses, not truncates.
_REPORT_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*({_SPS_PHRASE}|([0-9]{{4}}) {_YEAR_END_PHRASE})[ \t]*\n"
    rf"[ \t]*({_DATE})[ \t]*-[ \t]*({_DATE})[ \t]*$",
    re.MULTILINE,
)
# Loose label occurrences, counted to ensure every one strict-matches above.
_REPORT_LABEL_RE: Final[re.Pattern[str]] = re.compile(f"{_SPS_PHRASE}|{_YEAR_END_PHRASE}")
_PARTICIPANT_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"Participant Number:")
_PARTICIPANT_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t\f]*Participant Number:[ \t]*([A-Z0-9]+)[ \t]*$", re.MULTILINE
)
# The holder is the non-blank line that sits between a BLANK line and a
# ``Participant Number:`` line — the page-1 header is
# ``report / period / blank / holder / participant``. Requiring the preceding
# blank line is what distinguishes the holder from the period line (a malformed
# header with the period directly above the participant must not record the period
# string as the holder). Continuation pages have a blank line directly above the
# participant (no holder) and so yield no match; the same name in the address
# block / holdings rows is never directly above a ``Participant Number:`` label.
_HOLDER_RE: Final[re.Pattern[str]] = re.compile(
    r"\n[ \t]*\n[ \t\f]*(\S[^\n]*?)[ \t]*\n[ \t\f]*Participant Number:[ \t]*[A-Z0-9]+[ \t]*$",
    re.MULTILINE,
)

# --- holdings + NAV (#312) ------------------------------------------------------
# The "Your Stock Plan Account Value:" line is the priced/unpriced discriminator:
# its value is a dash (unpriced) or a money amount (priced). It is preceded on the
# page by "Envelope # ...", so it is not anchored to line start. Each label
# occurrence must strict-match (loose-count guard below) so a malformed line on one
# page refuses rather than being silently skipped.
_ACCOUNT_VALUE_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"Your Stock Plan Account Value:")
_ACCOUNT_VALUE_RE: Final[re.Pattern[str]] = re.compile(
    rf"Your Stock Plan Account Value:[ \t]+(-|{_MONEY})[ \t]*$", re.MULTILINE
)
# The Dec-31 account NAV used for the A2 closing balance (priced statements only).
# The amount must be a complete token (followed by whitespace or end-of-line, so a
# trailing digit like ``$653.360`` refuses) and must not be followed anywhere on the
# line by a second money token (``$653.36, $999.00`` or ``$653.36 $999.00`` refuse),
# which would make the NAV ambiguous; non-money trailing layout text (a 2-column
# page, e.g. ``Percent of``) is allowed.
_NAV_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"Ending Account Value Incl\. AI")
_NAV_RE: Final[re.Pattern[str]] = re.compile(
    rf"Ending Account Value Incl\. AI[ \t]+({_MONEY})(?=[ \t]|$)(?![^\n]*\$[0-9])", re.MULTILINE
)
# One holdings data row: ``<DESCRIPTION> (<SYMBOL>) <beginning> <qty> <price>
# <ending> <cost> <gain/loss> <eai>``. Line-anchored (``[ \t]`` not ``\s``) so a
# trailing token refuses; beginning MV is ``unavailable`` or a money amount. Only
# matches priced STOCK rows (the ``(SYMBOL)``); the money-market / core row has no
# ``(SYMBOL)`` and ``not applicable`` cost/gain — it is the #I14 concern. The
# description may start with a digit (e.g. ``3M CO``). The symbol charset matches the
# repo's ticker convention (``parser/activity_statement/_description.py``): an upper
# first char then upper-alnum / dot / hyphen, so hyphenated tickers (``BRK-B``,
# ``BF-B``, ``RDS-A``) are accepted; lowercase is rejected by design.
_SYMBOL: Final[str] = r"[A-Z][A-Z0-9.\-]*"
_HOLDING_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*([A-Z0-9][^\n(]*?)[ \t]+\(({_SYMBOL})\)[ \t]+"
    rf"(unavailable|{_MONEY})[ \t]+"
    rf"({_QTY})[ \t]+({_PRICE})[ \t]+({_MONEY})[ \t]+({_MONEY})[ \t]+"
    rf"({_MONEY})[ \t]+({_MONEY})[ \t]*$",
    re.MULTILINE,
)
# A loose stock-row *candidate*: the row prefix only, counted so every candidate must
# STRICT-match above — a malformed / extra stock row (a trailing token, a mangled
# later column, a truncated quantity like ``3.4``, a malformed beginning column like
# ``$1,23.00``, or a row truncated right after the beginning column like ``(XESP)
# unavailable``) refuses rather than being silently skipped. After the ``(SYMBOL)``
# the beginning column is either the ``unavailable`` keyword (sufficient on its own,
# so a page-split row truncated there still counts) or a value-ish token (optional
# ``-``/``$`` then a digit, catching malformed money) followed by one more token.
# Prose with a parenthesised abbreviation (``(EAI) & Estimated Yield (EY) - ...``), a
# customer-service phone number (``(800) 544-0275 ...`` — excluded by the uppercase
# ``_SYMBOL``), and the money-market fund's symbol continuation (``OUS (FYIXX)
# 3.725%``, a numeric token with nothing after) are all excluded.
#
# ponytail: the numeric beginning column deliberately requires one *following* token,
# which means a stock row truncated right after a numeric beginning value (``(XESP)
# $100.00`` at end-of-line) is NOT counted and is silently dropped. That requirement
# is load-bearing: dropping it (to count such a truncation) also matches
# one-value summary lines that carry a parenthesised abbreviation, e.g. ``Accrued
# Interest (AI)   $0.00`` and the ``(FYIXX) 3.725%`` continuation, which would
# false-refuse a valid statement. The gap is harmless: a non-spurious truncated
# row fails the exact ``Total Stocks`` foot or the later stocks + core = NAV check,
# so it still refuses; only a spurious truncated fragment is dropped, which cannot
# change a filed figure. Upgrade path: only if a statement truncates a
# priced row this way.
_HOLDING_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*[A-Z0-9][^\n(]*?[ \t]+\({_SYMBOL}\)[ \t]+(?:unavailable|-?\$?[0-9]\S*[ \t]+\S)",
    re.MULTILINE,
)
# The ``Core Account`` money-market (core position) holding — the cash sweep the net
# dividend is paid into. It is a SECURITY (units of a US registered investment
# company), not cash, so it is captured for Schedule FA rather than dropped; the
# grand ``Total Holdings`` already includes it while ``Total Stocks`` does not.
#
# Its row differs from a stock row in three ways, all of which this pattern encodes:
# cost basis and unrealized gain/loss print the literal ``not applicable`` (a stable
# $1.00 NAV sweep has no tracked basis); the name WRAPS with the ``(SYMBOL)`` landing
# on the continuation line; and an optional ``-- 7-day yield: N%`` line may follow.
# The beginning column is ``unavailable`` on the first statement that prices it.
_CORE_HOLDING_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*([A-Z0-9][^\n(]*?)[ \t]+(unavailable|{_MONEY})[ \t]+"
    rf"({_QTY})[ \t]+({_PRICE})[ \t]+({_MONEY})[ \t]+"
    rf"not applicable[ \t]+not applicable[ \t]+({_MONEY})[ \t]*$"
    rf"\n[ \t\f]*([A-Z0-9][A-Z0-9.,&'/-]*(?:[ ][A-Z0-9.,&'/-]+)*)?[ \t]*\(({_SYMBOL})\)"
    r"(?:[ \t]+(-?[0-9]+(?:\.[0-9]+)?)%[ \t]*$)?",
    re.MULTILINE,
)
_HOLDING_YIELD_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t\f]*(?:--[ \t]*7-day yield:[ \t]*)?(-?[0-9]+(?:\.[0-9]+)?)%[ \t]*$",
    re.MULTILINE,
)
# Counted against the strict match so a malformed core row refuses rather than being
# silently dropped (the holdings-guard pattern). ``not applicable`` twice over is the
# distinguishing shape — no stock row prints it.
_CORE_HOLDING_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t\f]*[A-Z0-9][^\n(]*?[ \t]+not applicable[ \t]+not applicable[ \t]", re.MULTILINE
)
# The ``Total Core Account (N% of account holdings)`` foot. The label WRAPS: the
# numbers sit on the first physical line and ``holdings)`` lands on the next, so the
# pattern must not require the closing label on the same line. Columns: beginning,
# ending, eai (no quantity/price/cost/gain).
_TOTAL_CORE_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"Total Core Account \(")
_TOTAL_CORE_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*Total Core Account \(([0-9]+(?:\.[0-9]+)?)% of account[ \t]+"
    rf"(unavailable|{_MONEY})[ \t]+({_MONEY})[ \t]+({_MONEY})[ \t]*$",
    re.MULTILINE,
)
# The ``Total Stocks (N% of account holdings)`` row: the stocks-only subtotal
# (excludes the money-market / core fund, which the grand ``Total Holdings``
# includes). Columns: beginning, ending, cost, gain/loss, eai (no qty/price). The
# percentage may be fractional (``99.5%``); a stray page marker may trail the line.
# The loose label (counted) catches a malformed/duplicate subtotal line that fails
# to strict-match, so it refuses rather than being silently skipped.
_TOTAL_STOCKS_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"Total Stocks \(")
_TOTAL_STOCKS_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*Total Stocks \(([0-9]+(?:\.[0-9]+)?)% of account holdings\)[ \t]+"
    rf"(unavailable|{_MONEY})[ \t]+"
    rf"({_MONEY})[ \t]+({_MONEY})[ \t]+({_MONEY})[ \t]+({_MONEY})(?:[ \t]+[A-Z])?[ \t]*$",
    re.MULTILINE,
)
# The grand ``Total Holdings`` row (stocks + money-market/core + other holdings). Its
# ending value (the first money column) is the account total and MUST equal the
# account NAV / value being filed; a divergence is an internal contradiction and
# refuses. Fully line-anchored (ending, cost, gain/loss, eai, optional page marker)
# so a malformed / extra-column row refuses; the loose label (counted) refuses a
# missing/duplicate row.
_TOTAL_HOLDINGS_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t\f]*Total Holdings\b", re.MULTILINE
)
_TOTAL_HOLDINGS_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*Total Holdings[ \t]+({_MONEY})[ \t]+({_MONEY})[ \t]+({_MONEY})[ \t]+({_MONEY})"
    rf"(?:[ \t]+[A-Z])?[ \t]*$",
    re.MULTILINE,
)

# --- income activity: dividends, withholding, MMKT sweep (#I9) -------------------
# Transaction rows print the date as MM/DD with NO year (resolved from the period).
# A dated line holds an ``MM/DD`` token anywhere, whatever precedes it (#1149): the line
# start or whitespace before it, whitespace or the line end after it. Counting dated
# lines in a section slice (loose) and requiring the count to equal the supported
# strict-row count refuses an unsupported or malformed row rather than silently
# skipping it (the holdings-guard pattern).
_TXN_DATE_TOKEN: Final[str] = rf"(?<!\S){_TXN_DATE}(?!\S)"
_TXN_DATE_LINE_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[^\n]*?{_TXN_DATE_TOKEN}[^\n]*", re.MULTILINE
)
# A wrapped security-name tail is all-caps, but so is the repeated page title.
# Exclude the two report titles before capturing the tail so a row at the bottom
# of a page cannot absorb the next page's header into its security identity.
_WRAPPED_NAME_TAIL: Final[str] = (
    rf"(?!(?:{_SPS_PHRASE}|[0-9]{{4}} {_YEAR_END_PHRASE})[ \t]*$)"
    r"([A-Z0-9][A-Z0-9.,&'/-]*(?:[ ][A-Z0-9.,&'/-]+)*)"
)

# Each section is delimited by an on-its-own-line header and a "Total ..." line. The
# strict header/total are EOL-anchored so a header does not also match its own "Total
# <header>" line; the loose label is line-start-anchored and (counted vs the strict
# match) refuses a header/total carrying trailing text rather than treating a present
# section as absent and silently dropping its income (a false ACCEPT of dropped tax).
_DIV_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t\f]*Dividends, Interest & Other Income[ \t]*$", re.MULTILINE
)
# The account summary carries no dividend line (the period's income is summarised in
# the ``Income Summary`` block instead), so ANY line starting with this label counts
# as a header, and one with trailing text, an amount included, refuses as malformed.
_DIV_HEADER_LOOSE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t\f]*Dividends, Interest & Other Income", re.MULTILINE
)
_DIV_TOTAL_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*Total Dividends, Interest & Other Income[ \t]+({_MONEY})[ \t]*$", re.MULTILINE
)
_DIV_TOTAL_LOOSE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t\f]*Total Dividends, Interest & Other Income", re.MULTILINE
)
# A dividend row: date, security name (may start with a digit, e.g. ``3M CO``), 9-char
# Symbol/CUSIP, "Dividend Received", the two dash columns (quantity, price), gross.
# A long security name WRAPS onto the next line, which carries only the name's tail
# indented under the name column (``FID TREASURY ONLY MMKT FUND CL`` / ``OUS``). That
# tail is captured and rejoined so the name is not silently truncated — a truncated
# name would drift against the same event printed unwrapped on an overlapping
# statement (refusing as a false "conflicting security names") and would break the
# withholding pairing, which matches on (date, name). The continuation is constrained
# to ALL-CAPS single-spaced text so it cannot swallow the Title-Case ``Total
# Dividends, Interest & Other Income`` foot or a following section header.
_DIV_ROW_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*({_TXN_DATE})[ \t]+([A-Z0-9][^\n]*?)[ \t]+([0-9A-Z]{{9}})[ \t]+Dividend Received"
    rf"[ \t]+-[ \t]+-[ \t]+({_MONEY_BARE})[ \t]*$"
    rf"(?:\n[ \t\f]*{_WRAPPED_NAME_TAIL}[ \t]*$)?",
    re.MULTILINE,
)

_WHT_HEADER_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t\f]*Taxes Withheld[ \t]*$", re.MULTILINE)
# The loose "Taxes Withheld" header must NOT match the "Subtractions" summary line
# (``  Taxes Withheld  -0.37 ...`` or, when nothing was withheld that period, ``  Taxes
# Withheld  -  -``), which carries trailing columns — so the lookahead rejects a
# following dash, ``$`` or digit. The section header stands alone on its line.
_WHT_HEADER_LOOSE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t\f]*Taxes Withheld(?![ \t]+(?:-|\$|[0-9]))", re.MULTILINE
)
# "Total Taxes Withheld" (the section total, with a $) — NOT "Total Federal Taxes
# Withheld" (a sub-total printed without a $), which has "Federal" between the words.
_WHT_TOTAL_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*Total Taxes Withheld[ \t]+({_MONEY})[ \t]*$", re.MULTILINE
)
_WHT_TOTAL_LOOSE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t\f]*Total Taxes Withheld", re.MULTILINE
)
# The "Total Federal Taxes Withheld" sub-total is printed WITHOUT a ``$`` (e.g.
# ``-0.37``). For a non-resident only the federal (Non-Resident) tax is withheld, so
# this sub-total must equal the section total and the row sum — a divergence is an
# internal contradiction that refuses. Its amount token therefore allows a missing $.
_WHT_FEDERAL_TOTAL_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*Total Federal Taxes Withheld[ \t]+({_MONEY_BARE})[ \t]*$", re.MULTILINE
)
_WHT_FEDERAL_TOTAL_LOOSE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t\f]*Total Federal Taxes Withheld", re.MULTILINE
)
# A withholding row: date, security, "Non-Resident Tax", signed amount (e.g. -$0.37).
# The security name WRAPS exactly as it does in the dividend table (the core fund's
# name is long enough to). Capturing and rejoining the tail is load-bearing here: the
# withholding is matched to its dividend on (date, security_name), so a name truncated
# on one side and not the other silently fails to pair and refuses at filing time.
# Same ALL-CAPS single-spaced constraint, so it cannot swallow the Title-Case
# "Total Federal Taxes Withheld" foot that follows the last row.
_WHT_ROW_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*({_TXN_DATE})[ \t]+([A-Z0-9][^\n]*?)[ \t]+Non-Resident Tax[ \t]+"
    rf"({_MONEY_BARE})[ \t]*$"
    rf"(?:\n[ \t\f]*{_WRAPPED_NAME_TAIL}[ \t]*$)?",
    re.MULTILINE,
)

_MMKT_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t\f]*Core Fund Activity[ \t]*$", re.MULTILINE
)
# Like the Taxes Withheld loose header, a following amount column is not counted.
_MMKT_HEADER_LOOSE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t\f]*Core Fund Activity(?![ \t]+(?:-|\$|[0-9]))", re.MULTILINE
)
_MMKT_TOTAL_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*Total Core Fund Activity[ \t]+({_MONEY})[ \t]*$", re.MULTILINE
)
_MMKT_TOTAL_LOOSE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t\f]*Total Core Fund Activity", re.MULTILINE
)
# A core-fund sweep row: date, "CASH", the transaction verb, a "FID TREASURY ...
# MMKT FUND" description, quantity, price, amount, balance. Requiring CASH + the
# verb + the FID-TREASURY description distinguishes it from the ESPP stock "You
# Bought" row (which is "<SECURITY> ESPP### <CUSIP> You Bought ...") in a different
# section. "Reinvestment" is the fund's own distribution being reinvested; "You
# Bought" is the swept dividend. Both credit the fund and both count toward the
# "Total Core Fund Activity" foot, so both must parse or the section refuses.
_MMKT_ROW_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*({_TXN_DATE})[ \t]+CASH[ \t]+(?:You Bought|Reinvestment)[ \t]+"
    rf"(FID TREASURY[^\n]*?MMKT FUND[^\n]*?)[ \t]+"
    rf"({_QTY})[ \t]+({_PRICE_BARE})[ \t]+({_MONEY_BARE})[ \t]+({_MONEY_BARE})[ \t]*$",
    re.MULTILINE,
)

# --- period summaries vs the detail sections (#903) ------------------------------
# A Stock Plan Services report also summarises the period's income, and its dividend
# and withholding section totals must equal their summaries' ``This Period`` columns.
# Every summary
# row prints a ``This Period`` and a ``Year-to-Date`` column, each a dash (zero) or an
# amount; Fidelity prints neither summary when there has been no income in the year
# so far. Withholding is the account summary's ``Taxes Withheld`` line (indented
# under ``Subtractions``). The account summary has no dividend line, so dividends are
# checked against the ``Income Summary`` block, whose rows are exactly ``Taxable``,
# an indented ``Dividends`` and ``Total``.
_SUMMARY_COLUMNS: Final[str] = rf"[ \t]+(-|{_MONEY_BARE})[ \t]+(-|{_MONEY_BARE})[ \t]*$"
_WHT_SUMMARY_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*Taxes Withheld{_SUMMARY_COLUMNS}", re.MULTILINE
)
# Count the label even when its columns are unrecognizable; the reader excludes the
# section's strict header and totals so displaced or malformed lines still refuse.
_WHT_SUMMARY_LOOSE_RE: Final[re.Pattern[str]] = re.compile(r"Taxes Withheld")
# The title shares its line with left-column text, so only its end of line is anchored.
_INCOME_SUMMARY_TITLE_RE: Final[re.Pattern[str]] = re.compile(
    r"Income Summary[ \t]*$", re.MULTILINE
)
_INCOME_SUMMARY_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"Income Summary")
# The column header may share its line with left-column text, like the title.
_INCOME_SUMMARY_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"This Period[ \t]+Year-to-Date[ \t]*$"
)
_INCOME_SUMMARY_ROW_RES: Final[dict[str, re.Pattern[str]]] = {
    label: re.compile(rf"[ \t\f]*{label}{_SUMMARY_COLUMNS}")
    for label in ("Taxable", "Dividends", "Total")
}
# The block's rows are the first lines after its title that carry an amount-like token.
# Loose detection includes malformed numeric tokens so strict row parsing can refuse them.
_SUMMARY_AMOUNT_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\S)(?:-|[^\w\s]*\d[\d,]*\.\d{2}[^\w\s]*)(?!\S)"
)
# A row label and an amount-like token on the same line, excluding the detail label;
# counted so malformed rows outside the block (or without its title) still refuse.
_INCOME_SUMMARY_ROW_LOOSE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:Taxable|Dividends)(?!, Interest & Other Income)[^\n]*?(?:-|\$|\d)"
)
# A line with any text on it, for walking the block's lines with their positions.
_NON_BLANK_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t\f]*[^ \t\f\n][^\n]*", re.MULTILINE)
# A year-end report's account summary prints its ``Taxes Withheld`` line with one
# column, a dash or an amount. It is checked for shape and not read.
_YEAR_END_WHT_SUMMARY_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*Taxes Withheld[ \t]+(-|{_MONEY_BARE})[ \t]*$", re.MULTILINE
)

# --- dated and ``Total`` lines across the whole statement (#1088, #1149) ----------
# Cash can reach the account only through activity the statement lists, so every
# dated line and every line that starts with the word ``Total`` must be one the parser
# read or shape-checked; any other refuses. The group is set on a ``Total`` line; the
# match runs to the end of the line.
_DATED_OR_TOTAL_LINE_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?:[ \t\f]*(Total)\b|(?=[^\n]*?{_TXN_DATE_TOKEN}))[^\n]*", re.MULTILINE
)
# pdftotext starts each page's first line with its form feed, so text before a form
# feed on its line is a page-break remnant, including unsupported indentation. Dated
# text after it makes the line a dated line that no reader reads.
_PAGE_BREAK_REMNANT_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[^\n]*[^ \t\f\n][^\S\n]*\f[^\n]*?{_TXN_DATE_TOKEN}[^\n]*", re.MULTILINE
)
# Fidelity's ESPP purchase row in the ``Securities Bought & Sold`` table: a one-letter
# footnote marker, the settlement date, the upper-case security name, ``ESPP###``,
# the 9-character identifier, ``You Bought``, a 3-decimal quantity, a ``$`` price
# with 5 decimals, the ``Transaction Cost`` dash and the amount, which must carry its
# minus sign. Matched against a whole line; the groups are the marker, date, identifier,
# quantity, price and amount.
_ESPP_ROW_RE: Final[re.Pattern[str]] = re.compile(
    rf"[ \t\f]*([a-z])[ \t]+({_TXN_DATE})[ \t]+[A-Z]+(?: [A-Z]+)*[ \t]+ESPP###[ \t]+"
    rf"([A-Z0-9]{{9}})[ \t]+You Bought[ \t]+({_QTY})[ \t]+"
    rf"(\${_UNSIGNED}\.[0-9]{{5}})[ \t]+-[ \t]+((?=-){_MONEY})[ \t]*"
)
# A line that names either part of the purchase is an ESPP row candidate, so a
# malformed one gets the purchase-row refusal.
_ESPP_ROW_LOOSE_RE: Final[re.Pattern[str]] = re.compile(r"ESPP|You Bought")
# The purchase rows' table (owner decision, #1149): its title, its two-line column
# header and its two summary lines, each matched against a whole line. Any line naming
# the table must be one of its title, ``Total Securities Bought`` or ``Net`` lines.
_BOUGHT_TITLE_RE: Final[re.Pattern[str]] = re.compile(r"[ \t\f]*Securities Bought & Sold[ \t]*")
_BOUGHT_HEADER_RES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"[ \t\f]*Settlement[ \t]+Symbol/[ \t]+Transaction[ \t]*"),
    re.compile(
        r"[ \t\f]*Date[ \t]+Security Name[ \t]+CUSIP[ \t]+Description[ \t]+Quantity[ \t]+"
        r"Price[ \t]+Cost[ \t]+Amount[ \t]*"
    ),
)
_NET_BOUGHT_RE: Final[re.Pattern[str]] = re.compile(
    rf"[ \t\f]*Net Securities Bought & Sold[ \t]+-[ \t]+({_MONEY})[ \t]*"
)
_BOUGHT_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    r"Securities[^\S\n]+Bought|Net[^\S\n]+Securities"
)
# Each purchase row's footnote marker is explained by one legend line, which may run
# on. Group 1 is the marker; group 2 is set when the whole sentence follows it.
_LEGEND_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t\f]*([a-z])[ \t]+Purchase of stock( through your company stock plan\.)?", re.MULTILINE
)
# The purchase's cash (owner decision, #1088): the plan journals it in as an ``SPP
# PURCHASE CREDIT`` row (three dash columns, then the amount), footed by ``Total Other
# Activity In``, and the purchase spends it in ``Total Securities Bought``. Each total
# prints a dash column, then its amount.
_SPP_CREDIT_ROW_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*({_TXN_DATE})[ \t]+SPP PURCHASE CREDIT[ \t]+Journaled[ \t]+-[ \t]+-[ \t]+-"
    rf"[ \t]+({_MONEY_BARE})[ \t]*$",
    re.MULTILINE,
)
_OTHER_ACTIVITY_IN_TOTAL_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*Total Other Activity In[ \t]+-[ \t]+({_MONEY})[ \t]*$", re.MULTILINE
)
_SECURITIES_BOUGHT_TOTAL_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t\f]*Total Securities Bought[ \t]+-[ \t]+({_MONEY})[ \t]*$", re.MULTILINE
)
# ``Total`` lines accepted by exact shape without being read (owner decision, #1088):
# none of them is cash activity in the account.
# The asset allocation's total: whole dollars and ``100%``.
_ALLOCATION_TOTAL: Final[str] = rf"Total[ \t]+(\${_UNSIGNED})[ \t]+(100)%"
_UNREAD_TOTAL_LINE_RES: Final[tuple[tuple[re.Pattern[str], tuple[NumberKind, ...]], ...]] = tuple(
    (re.compile(rf"[ \t\f]*{shape}[ \t]*"), kinds)
    for shape, kinds in (
        # The holdings table's common-stock sub-total; its label wraps after "account".
        (
            r"Total Common Stock \(([0-9]+(?:\.[0-9]+)?)% of account[ \t]+"
            rf"(unavailable|{_MONEY})[ \t]+({_MONEY})[ \t]+({_MONEY})"
            rf"[ \t]+({_MONEY})[ \t]+({_MONEY})",
            (NumberKind.RATE, *(NumberKind.MONEY,) * 5),
        ),
        # One or two columns, each a dash or an amount; the asset allocation's total may
        # share its line.
        (
            rf"Total Including Other Holdings[ \t]+(-|{_MONEY})(?:[ \t]+(-|{_MONEY}))?"
            rf"(?:[ \t]+{_ALLOCATION_TOTAL})?",
            (NumberKind.MONEY, NumberKind.MONEY, NumberKind.MONEY, NumberKind.RATE),
        ),
        # The asset allocation's total on its own line; a page marker letter may trail.
        (
            rf"{_ALLOCATION_TOTAL}(?:[ \t]+[A-Z])?",
            (NumberKind.MONEY, NumberKind.RATE),
        ),
        # The ESPP offering-period summary: two amounts, each followed by a one-digit
        # number, then a 4-decimal price; or a quantity and an amount.
        (
            rf"Total for all Offering Periods[ \t]+(?:({_MONEY})[ \t]+[0-9][ \t]+({_MONEY})"
            rf"[ \t]+[0-9][ \t]+({_PRICE})|({_QTY})[ \t]+({_MONEY}))",
            (
                NumberKind.MONEY,
                NumberKind.MONEY,
                NumberKind.PRICE,
                NumberKind.QUANTITY,
                NumberKind.MONEY,
            ),
        ),
        # A holdings footnote.
        (
            r"Total Cost Basis does not include the cost basis on core, money market or other "
            r"positions where cost basis is unknown or not applicable\.",
            (),
        ),
    )
)


class ReportType(StrEnum):
    """Which Fidelity SPS statement this is."""

    STOCK_PLAN_SERVICES = "Stock Plan Services Report"
    YEAR_END = "Year-End Investment Report"


@dataclass(frozen=True, slots=True)
class StatementHolding:
    """One priced holding from a Fidelity SPS statement ``Holdings`` table.

    Carries only what the holdings table states. There is **no CUSIP** in this
    table (the symbol is in the description, ``EXAMPLE CORP (XESP)``); the CUSIP
    / ISIN come from ``[netbenefits.security]`` config and are joined downstream,
    exactly as for the lots-CSV parser. The "Beginning Market Value" column is
    not captured.

    Attributes:
        description: The security description (e.g. ``"EXAMPLE CORP"``).
        symbol: The ticker from the description's ``(SYMBOL)`` (e.g. ``"XESP"``).
        quantity: Shares held (> 0).
        price_per_unit_usd: Per-unit price (4 dp in the source).
        ending_market_value_usd: Ending market value (= quantity x price).
        total_cost_basis_usd: Total cost basis.
        unrealized_gain_loss_usd: Unrealized gain/loss (may be negative).
        eai_usd: Estimated Annual Income (signed; may be negative per Fidelity).
    """

    description: str
    symbol: str
    quantity: Decimal
    price_per_unit_usd: Decimal
    ending_market_value_usd: Decimal
    total_cost_basis_usd: Decimal
    unrealized_gain_loss_usd: Decimal
    eai_usd: Decimal


@dataclass(frozen=True, slots=True)
class StatementCoreHolding:
    """The ``Core Account`` money-market holding — the account's cash sweep.

    A SEPARATE type from :class:`StatementHolding` because the core row genuinely
    carries less: cost basis and unrealized gain/loss print ``not applicable``, so
    they are absent here rather than faked as zero. Modelling them as ``Decimal("0")``
    would silently assert a zero basis that the statement never states.

    This is a security (units of a US registered investment company), not cash, and
    must be disclosed in Schedule FA Table A3 in its own right while its value also
    sits inside the Table A2 account balances. The parser surfaces it for that proof;
    the NetBenefits FA pipeline currently refuses rather than auto-disclosing it.

    Attributes:
        description: The fund as printed, name rejoined across the line wrap
            (e.g. ``"FID TREASURY ONLY MMKT FUND CL OUS"``).
        symbol: The ticker from the continuation line's ``(SYMBOL)`` (e.g. ``"FYIXX"``).
        quantity: Units held (> 0).
        price_per_unit_usd: Per-unit NAV (``$1.0000`` for a stable-NAV fund).
        ending_market_value_usd: Ending market value (= quantity x price).
        eai_usd: Estimated Annual Income (signed; may be negative per Fidelity).
    """

    description: str
    symbol: str
    quantity: Decimal
    price_per_unit_usd: Decimal
    ending_market_value_usd: Decimal
    eai_usd: Decimal


@dataclass(frozen=True, slots=True)
class StatementDividend:
    """One ``Dividend Received`` row from the statement's income activity (#I9).

    Attributes:
        date: The settlement / credit date (MM/DD on the page, year from the period).
        security_name: The payer as printed (e.g. ``"EXAMPLE CORP"``).
        cusip: The 9-character Symbol/CUSIP from the row (captured, not validated here
            — the config cross-check is joined downstream, as for the lots CSV).
        gross_amount_usd: The gross dividend amount (> 0; before withholding).
    """

    date: _dt.date
    security_name: str
    cusip: str
    gross_amount_usd: Decimal


@dataclass(frozen=True, slots=True)
class StatementWithholding:
    """One ``Non-Resident Tax`` row from the ``Taxes Withheld`` section (#I9).

    Attributes:
        date: The withholding date (MM/DD on the page, year from the period).
        security_name: The security as printed (e.g. ``"EXAMPLE CORP"``).
        amount_usd: The SIGNED statement value (negative — a deduction, e.g.
            ``-0.37``), stored as printed rather than normalised to a magnitude.
    """

    date: _dt.date
    security_name: str
    amount_usd: Decimal


@dataclass(frozen=True, slots=True)
class StatementMmktBuy:
    """One ``Core Fund Activity`` money-market sweep buy (#I9).

    The net dividend is swept into the core fund; this is the ``CASH / You Bought /
    FID TREASURY ... MMKT FUND`` reinvestment. The fund itself becomes an FA holding
    in #I14; here it is parsed for the dividend reconciliation.

    Attributes:
        date: The settlement date (MM/DD on the page, year from the period).
        fund_name: The core fund as printed (e.g. ``"FID TREASURY ONLY MMKT FUND CL OUS"``).
        quantity: Units bought (> 0; 3 dp in the source).
        price_usd: Per-unit NAV (> 0; 4 dp in the source).
        amount_usd: The buy amount (> 0; ~= quantity x price).
    """

    date: _dt.date
    fund_name: str
    quantity: Decimal
    price_usd: Decimal
    amount_usd: Decimal


@dataclass(frozen=True, slots=True)
class StatementPurchase:
    """One ESPP purchase row from the ``Securities Bought & Sold`` table (#1149).

    Attributes:
        settlement_date: The settlement date (MM/DD on the page, year from the period).
        identifier: The 9-character Symbol/CUSIP from the row (captured, not
            validated here; the config cross-check is joined downstream).
        quantity: Shares bought (> 0; 3 dp in the source).
        price_usd: The per-share price (> 0; 5 dp in the source).
        amount_usd: The SIGNED amount as printed (negative: an outflow).
    """

    settlement_date: _dt.date
    identifier: str
    quantity: Decimal
    price_usd: Decimal
    amount_usd: Decimal


@dataclass(frozen=True, slots=True)
class StatementData:
    """Identity + period of a Fidelity SPS statement.

    The skeleton (#311). The section parsers extend the pipeline with holdings /
    NAV (#I7) and dividend / WHT / MMKT (#I9); this container carries only the
    identity every statement states.

    Attributes:
        report_type: :class:`ReportType` (monthly/multi-month vs year-end).
        period_start / period_end: The coverage period (inclusive).
        participant_no: The Fidelity Participant Number.
        account_holder: The account holder name as printed in the header.
        custodian: The custodian, ``"Fidelity Stock Plan Services LLC"`` (asserted
            present in the document as a type guard, not a variably-parsed figure).
        holdings: The priced ``Holdings`` rows, or ``()`` when the statement is
            unpriced (the lot shows ``-``).
        account_nav_usd: The Dec-31 account NAV ("Ending Account Value Incl. AI"),
            or ``None`` when the statement is unpriced. ``None`` is the explicit
            unpriced marker that drives the #310 Yahoo fallback.
        dividends: ``Dividend Received`` rows (#I9), or ``()`` if none.
        withholdings: ``Non-Resident Tax`` rows (#I9), or ``()`` if none.
        mmkt_buys: Core-fund money-market sweep buys (#I9), or ``()`` if none.
        purchases: The ESPP purchase rows of the ``Securities Bought & Sold`` table
            (#1149), in table order, or ``()`` if none.
        core_holding: The ``Core Account`` money-market holding, or ``None`` when
            the statement has no Core Account section (before the first dividend
            sweep the account holds no core position at all).
    """

    report_type: ReportType
    period_start: _dt.date
    period_end: _dt.date
    participant_no: str
    account_holder: str
    custodian: str
    holdings: tuple[StatementHolding, ...]
    account_nav_usd: Decimal | None
    dividends: tuple[StatementDividend, ...] = ()
    withholdings: tuple[StatementWithholding, ...] = ()
    mmkt_buys: tuple[StatementMmktBuy, ...] = ()
    purchases: tuple[StatementPurchase, ...] = ()
    core_holding: StatementCoreHolding | None = None


# A whole-document or whole-table check reports ``row_index=0``; its message says so,
# so the location is never read as the table's first row.
_WHOLE_DOCUMENT: Final[str] = "the whole document was checked"
_WHOLE_TABLE: Final[str] = "the whole table was checked"

DIV_TABLE: Final[str] = "Dividends, Interest & Other Income"
WHT_TABLE: Final[str] = "Taxes Withheld"
MMKT_TABLE: Final[str] = "Core Fund Activity"
_SPP_TABLE: Final[str] = "Other Activity In"
_BOUGHT_TABLE: Final[str] = "Securities Bought"
_NAV_COLUMN: Final[str] = "Ending Account Value Incl. AI"
_ACCOUNT_VALUE_COLUMN: Final[str] = "Your Stock Plan Account Value"
_WHT_SUMMARY_COLUMN: Final[str] = "account summary: Taxes Withheld"
_INCOME_SUMMARY_COLUMN: Final[str] = "Income Summary: Total"
# The money columns of a ``_HOLDING_RE`` / ``_MMKT_ROW_RE`` row, in capture order.
_HOLDING_FIELDS: Final[tuple[str, ...]] = (
    "quantity",
    "price",
    "ending market value",
    "cost basis",
    "unrealized gain/loss",
    "EAI",
)
_MMKT_FIELDS: Final[tuple[str, ...]] = ("quantity", "price", "amount", "balance")
# The row fields read as a quantity or a unit price; every other one is money.
_FIELD_KINDS: Final[dict[str, NumberKind]] = {
    "quantity": NumberKind.QUANTITY,
    "price": NumberKind.PRICE,
}

# Resolves a transaction row's ``MM/DD`` given its ``column`` and ``row_index``.
type _Resolve = Callable[[str, str, int], _dt.date]


def _fail(
    message: str, *, source: str, column: str | None = None, row_index: int = 0
) -> ParseError:
    """Build a refusal with the caller's location and no statement value."""
    return ParseError(
        section=source, row_index=row_index, column=column, message=message, raw_value=""
    )


def _consistent_repeated[T](
    matches: list[T],
    label_count: int,
    *,
    empty: str,
    malformed: str,
    what: str,
    source: str,
) -> T:
    """Return the single value a per-page-repeated signal must agree on.

    ``matches`` are the strict pattern hits; ``label_count`` is the number of loose
    label occurrences. Refuses if the signal is absent (``empty``), if some
    occurrence failed to strict-match (``malformed`` — a corrupt line on one page
    must not be silently skipped), or if the strict hits disagree (``what``). The
    disagreement is located at the 0-based occurrence of the first differing value.
    """
    if not matches:
        raise _fail(empty, source=source)
    if len(matches) != label_count:
        raise _fail(malformed, source=source)
    differs = next((i for i, match in enumerate(matches) if match != matches[0]), None)
    if differs is not None:
        raise _fail(
            f"inconsistent {what} across pages: this occurrence differs from the first",
            source=source,
            column=what,
            row_index=differs,
        )
    return matches[0]


def _date(raw: str, *, source: str, column: str) -> _dt.date:
    parsed = full_date(raw)
    if parsed is FullDateFailure.MALFORMED:
        raise _fail(
            "not a Fidelity statement date (expected 'Month D, YYYY')",
            source=source,
            column=column,
        )
    if parsed is FullDateFailure.NOT_A_CALENDAR_DATE:
        raise _fail("not a valid calendar date", source=source, column=column)
    return parsed


def _money(
    raw: str,
    *,
    source: str,
    column: str,
    row_index: int = 0,
    kind: NumberKind = NumberKind.MONEY,
) -> Decimal:
    """Parse a ``[-]$N[,NNN].DD`` (or 4-dp) money/price token, or a quantity,
    to a Decimal of ``kind``."""
    try:
        value = Decimal(raw.replace("$", "").replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - regex pre-validates format
        raise _fail(
            "not a valid money amount", source=source, column=column, row_index=row_index
        ) from exc
    breach = number_limit_breach(value, kind)
    if breach is not None:
        raise _fail(breach, source=source, column=column, row_index=row_index)
    return value


def _require_positive(
    value: Decimal, *, source: str, column: str, row_index: int | None = None
) -> None:
    """Refuse a value that isn't positive. ``row_index`` is its row in its table;
    without one it is a whole-document figure, located at row 0, and the message
    says so."""
    if value <= 0:
        message = f"{column} must be > 0"
        if row_index is None:
            message = f"{message}; {_WHOLE_DOCUMENT}"
        raise _fail(message, source=source, column=column, row_index=row_index or 0)


def _require_close(
    got: Decimal,
    expected: Decimal,
    tol: Decimal,
    *,
    what: str,
    source: str,
    column: str,
    row_index: int,
) -> None:
    if abs(got - expected) > tol:
        raise _fail(
            f"{what} differ by more than the rounding tolerance",
            source=source,
            column=column,
            row_index=row_index,
        )


def _rounding_tolerance(price: Decimal) -> Decimal:
    """How far an amount may sit from quantity x price: the 3-dp half-ULP x price,
    plus a cent, because the quantity prints rounded to 3 dp."""
    return Decimal(_QTY_HALF_ULP) * price + _CENT


def _rejoined_name(head: str, tail: str | None) -> str:
    """A security name whose ``tail`` wrapped onto the next line, rejoined with one
    space; the Core Account, dividend and withholding readers share this join so
    a fund keeps one name across them."""
    return f"{head.strip()} {tail.strip()}".strip() if tail else head.strip()


def _parse_holdings(text: str, *, source: str) -> tuple[StatementHolding, ...]:
    """Parse + cross-check the priced ``Holdings`` table stock rows.

    Each row's ending value is checked against ``quantity x price`` and exactly
    against ``cost + gain/loss``; the per-row sums are cross-checked against the
    stocks-only ``Total Stocks`` subtotal, so a missed or extra row refuses. The
    stock data rows do not repeat across pages, so every match is a distinct
    holding (two genuinely identical positions are both kept). A core-only account
    legitimately returns no stock holdings; the caller reconciles that core position
    to the account NAV.
    """
    holdings: list[StatementHolding] = []
    for i, (desc, sym, begin_raw, *raws) in enumerate(_HOLDING_RE.findall(text)):
        quantity, price, ending, cost, gain_loss, eai = (
            _money(
                raw,
                source=source,
                column=f"Holdings: {field}",
                row_index=i,
                kind=_FIELD_KINDS.get(field, NumberKind.MONEY),
            )
            for raw, field in zip(raws, _HOLDING_FIELDS, strict=True)
        )
        if begin_raw != "unavailable":
            _money(begin_raw, source=source, column="Holdings: beginning market value", row_index=i)
        for value, field in zip((quantity, price, ending, cost), _HOLDING_FIELDS[:4], strict=True):
            _require_positive(value, source=source, column=f"Holdings: {field}", row_index=i)
        # EAI (Estimated Annual Income) is a signed informational figure: Fidelity's
        # own footer states "EAI may be negative on short & EY positions", so it is
        # not constrained here; it is still cross-checked against the Total Stocks EAI.
        # The quantity is displayed to 3 dp while the ending value uses the true
        # quantity, so the tolerance is the 3-dp half-ULP x price (plus a cent).
        # Still catches a column shift / wrong price (dollars).
        _require_close(
            ending,
            quantity * price,
            _rounding_tolerance(price),
            what="the ending market value and quantity x price",
            source=source,
            column="Holdings: ending market value",
            row_index=i,
        )
        # ``gain/loss`` is, by definition, the difference of the two displayed
        # dollar columns, so this must hold exactly (no rounding slack).
        if ending - cost != gain_loss:
            raise _fail(
                "the ending market value minus the cost basis must equal the unrealized gain/loss",
                source=source,
                column="Holdings: unrealized gain/loss",
                row_index=i,
            )
        holdings.append(
            StatementHolding(
                description=desc.strip(),
                symbol=sym,
                quantity=quantity,
                price_per_unit_usd=price,
                ending_market_value_usd=ending,
                total_cost_basis_usd=cost,
                unrealized_gain_loss_usd=gain_loss,
                eai_usd=eai,
            )
        )
    # Every stock-row candidate must have strict-matched; a malformed or extra row
    # (trailing token, mangled column) must refuse, not be silently dropped.
    if len(holdings) != len(_HOLDING_LABEL_RE.findall(text)):
        raise _fail("a Holdings stock row is malformed", source=source)
    for raw in _HOLDING_YIELD_RE.findall(text):
        _money(raw, source=source, column="Holdings: yield", kind=NumberKind.RATE)
    if not holdings:
        if _TOTAL_STOCKS_LABEL_RE.search(text):
            raise _fail(
                "priced statement has no parseable Holdings rows",
                source=source,
            )
        return ()
    result = tuple(holdings)
    _cross_check_total(result, text, source=source)
    return result


def _parse_core_holding(text: str, *, source: str) -> StatementCoreHolding | None:
    """Parse + cross-check the ``Core Account`` money-market holding.

    Returns ``None`` when the statement has no Core Account section — genuinely
    absent before the first dividend sweep creates the position, not a silent drop:
    a malformed row still refuses via the loose-label count, and a present section
    whose foot disagrees with the row refuses via the subtotal cross-check.
    """
    rows = _CORE_HOLDING_RE.findall(text)
    labels = len(_CORE_HOLDING_LABEL_RE.findall(text))
    feet = _TOTAL_CORE_RE.findall(text)
    foot_labels = len(_TOTAL_CORE_LABEL_RE.findall(text))
    if not rows and not labels and not foot_labels:
        return None
    if len(rows) != 1 or labels != 1:
        raise _fail(
            f"expected exactly one Core Account holding row; found "
            f"{len(rows)} strict / {labels} loose",
            source=source,
        )
    if len(feet) != 1 or foot_labels != 1:
        raise _fail(
            f"expected exactly one 'Total Core Account' subtotal row; found "
            f"{len(feet)} strict / {foot_labels} loose",
            source=source,
        )
    desc, begin_raw, qty_raw, price_raw, end_raw, eai_raw, name_tail, symbol, yield_raw = rows[0]
    quantity, price, ending, eai = (
        _money(
            raw,
            source=source,
            column=f"Core Account: {field}",
            kind=_FIELD_KINDS.get(field, NumberKind.MONEY),
        )
        for raw, field in (
            (qty_raw, "quantity"),
            (price_raw, "price"),
            (end_raw, "ending market value"),
            (eai_raw, "EAI"),
        )
    )
    if begin_raw != "unavailable":
        _money(begin_raw, source=source, column="Core Account: beginning market value")
    if yield_raw:
        _money(yield_raw, source=source, column="Core Account: yield", kind=NumberKind.RATE)
    for value, field in ((quantity, "quantity"), (price, "price"), (ending, "ending market value")):
        _require_positive(value, source=source, column=f"Core Account: {field}", row_index=0)
    _require_close(
        ending,
        quantity * price,
        _rounding_tolerance(price),
        what="the ending market value and quantity x price",
        source=source,
        column="Core Account: ending market value",
        row_index=0,
    )
    # The foot restates the single row's ending value and EAI; a divergence means a
    # second core position exists that the row pattern missed, so refuse.
    foot_end, foot_eai = (
        _money(v, source=source, column="Core Account: Total Core Account") for v in feet[0][2:]
    )
    foot_percent, foot_begin = feet[0][:2]
    _money(
        foot_percent, source=source, column="Core Account: Total Core Account", kind=NumberKind.RATE
    )
    if foot_begin != "unavailable":
        _money(foot_begin, source=source, column="Core Account: Total Core Account")
    if (ending, eai) != (foot_end, foot_eai):
        raise _fail(
            "the Core Account holding's ending market value and EAI differ from the "
            f"'Total Core Account' subtotal; {_WHOLE_TABLE}",
            source=source,
            column="Core Account: Total Core Account",
        )
    # The name wraps; rejoin so it matches the unwrapped spelling the Core Fund
    # Activity rows print, which is what downstream identity comparisons use.
    name = _rejoined_name(desc, name_tail)
    return StatementCoreHolding(
        description=name,
        symbol=symbol,
        quantity=quantity,
        price_per_unit_usd=price,
        ending_market_value_usd=ending,
        eai_usd=eai,
    )


def _cross_check_total(holdings: tuple[StatementHolding, ...], text: str, *, source: str) -> None:
    """Assert the ``Total Stocks`` subtotal equals the per-row sums.

    Cross-checks against the stocks-only ``Total Stocks (N% of account holdings)``
    row (not the grand ``Total Holdings``, which also includes the money-market /
    core fund — #I14), so a missed or extra stock row refuses.
    """
    totals = _TOTAL_STOCKS_RE.findall(text)
    label_count = len(_TOTAL_STOCKS_LABEL_RE.findall(text))
    if len(totals) != 1 or label_count != 1:
        raise _fail(
            f"expected exactly one 'Total Stocks' subtotal row; found "
            f"{len(totals)} strict / {label_count} loose",
            source=source,
        )
    percent_raw, begin_raw, end_raw, cost_raw, gl_raw, eai_raw = totals[0]
    for label, got, raw in (
        ("ending value", sum((h.ending_market_value_usd for h in holdings), Decimal(0)), end_raw),
        ("cost", sum((h.total_cost_basis_usd for h in holdings), Decimal(0)), cost_raw),
        ("gain/loss", sum((h.unrealized_gain_loss_usd for h in holdings), Decimal(0)), gl_raw),
        ("EAI", sum((h.eai_usd for h in holdings), Decimal(0)), eai_raw),
    ):
        # Fidelity's subtotal is the sum of the displayed (already cent-rounded)
        # line items, so it must match exactly; a one-cent gap signals a dropped /
        # extra / misread row and must refuse.
        expected = _money(raw, source=source, column="Holdings: Total Stocks")
        if got != expected:
            raise _fail(
                f"the sum of the holdings' {label} does not equal 'Total Stocks'; {_WHOLE_TABLE}",
                source=source,
                column="Holdings: Total Stocks",
            )
    _money(percent_raw, source=source, column="Holdings: Total Stocks", kind=NumberKind.RATE)
    if begin_raw != "unavailable":
        _money(begin_raw, source=source, column="Holdings: Total Stocks")


def _parse_holdings_and_nav(
    text: str, *, source: str
) -> tuple[tuple[StatementHolding, ...], Decimal | None]:
    """Return ``(holdings, account_nav_usd)``, refusing on an ambiguous price state.

    The "Your Stock Plan Account Value:" line discriminates the two states: a dash
    is unpriced (``(), None``); a money amount is priced. The value must be
    consistent across the pages it repeats on. The priced branch requires the
    ``Ending Account Value Incl. AI`` NAV (the A2 figure) and parses + cross-checks
    the holdings table; the unpriced branch asserts there is no priced content.
    """
    account_value_hits = _ACCOUNT_VALUE_RE.findall(text)
    if not account_value_hits:
        raise _fail("no 'Your Stock Plan Account Value:' line found", source=source)
    # Every account-value label occurrence must strict-match (a malformed line on
    # one page must not be silently skipped while a clean one agrees).
    if len(account_value_hits) != len(_ACCOUNT_VALUE_LABEL_RE.findall(text)):
        raise _fail("a 'Your Stock Plan Account Value:' line is malformed", source=source)
    account_values = set(account_value_hits)
    if len(account_values) != 1:
        raise _fail(
            f"inconsistent account-value state across pages; {_WHOLE_DOCUMENT}",
            source=source,
            column=_ACCOUNT_VALUE_COLUMN,
        )
    (account_value,) = account_values

    if account_value == "-":
        # Defend against a stray priced fragment in an otherwise-unpriced statement.
        # Use the loose labels so a *malformed* stray holding / subtotal line also
        # refuses, not only a strict-matching one.
        if (
            _HOLDING_LABEL_RE.search(text)
            or _CORE_HOLDING_LABEL_RE.search(text)
            or _TOTAL_STOCKS_LABEL_RE.search(text)
            or _TOTAL_CORE_LABEL_RE.search(text)
            or _TOTAL_HOLDINGS_LABEL_RE.search(text)
            or _NAV_LABEL_RE.search(text)
        ):
            raise _fail(
                "unpriced statement unexpectedly contains a priced holdings row / NAV",
                source=source,
            )
        return (), None

    nav_hits = _NAV_RE.findall(text)
    if not nav_hits:
        raise _fail(
            "priced statement is missing the 'Ending Account Value Incl. AI' NAV line",
            source=source,
        )
    # Likewise, every NAV label occurrence must strict-match (EOL-anchored).
    if len(nav_hits) != len(_NAV_LABEL_RE.findall(text)):
        raise _fail("an 'Ending Account Value Incl. AI' NAV line is malformed", source=source)
    nav = _money(nav_hits[0], source=source, column=_NAV_COLUMN)
    if any(_money(hit, source=source, column=_NAV_COLUMN) != nav for hit in nav_hits[1:]):
        raise _fail(
            f"inconsistent account NAV across pages; {_WHOLE_DOCUMENT}",
            source=source,
            column=_NAV_COLUMN,
        )
    _require_positive(nav, source=source, column=_NAV_COLUMN)
    # The priced "Your Stock Plan Account Value:" is the same figure as the NAV,
    # printed in the account summary; a divergence is a corrupt/ambiguous statement.
    account_value_num = _money(account_value, source=source, column=_ACCOUNT_VALUE_COLUMN)
    _require_positive(account_value_num, source=source, column=_ACCOUNT_VALUE_COLUMN)
    if account_value_num != nav:
        raise _fail(
            f"'Your Stock Plan Account Value:' does not equal the account NAV; {_WHOLE_DOCUMENT}",
            source=source,
            column=_ACCOUNT_VALUE_COLUMN,
        )
    # The holdings table's grand "Total Holdings" row is a third printed copy of the
    # account total and must agree with the NAV; a divergence is an internal
    # contradiction that refuses.
    total_holdings_hits = _TOTAL_HOLDINGS_RE.findall(text)
    if len(total_holdings_hits) != 1 or len(_TOTAL_HOLDINGS_LABEL_RE.findall(text)) != 1:
        raise _fail(
            f"expected exactly one 'Total Holdings' row; found {len(total_holdings_hits)} "
            f"strict / {len(_TOTAL_HOLDINGS_LABEL_RE.findall(text))} loose",
            source=source,
        )
    total_holdings = _money(
        total_holdings_hits[0][0], source=source, column="Holdings: Total Holdings"
    )
    if total_holdings != nav:
        raise _fail(
            f"the 'Total Holdings' ending value does not equal the account NAV; {_WHOLE_DOCUMENT}",
            source=source,
            column="Holdings: Total Holdings",
        )
    for raw in total_holdings_hits[0][1:]:
        _money(raw, source=source, column="Holdings: Total Holdings")
    return _parse_holdings(text, source=source), nav


def _resolve_txn_date(
    mm_dd: str,
    period_start: _dt.date,
    period_end: _dt.date,
    *,
    source: str,
    column: str,
    row_index: int,
) -> _dt.date:
    """Resolve an ``MM/DD`` transaction date to a full date within the period.

    The page prints transaction dates without a year. The year is whichever of the
    period's start/end year places the date inside ``[period_start, period_end]``;
    exactly one must qualify (else the date is ambiguous or out of period — refuse).
    """
    month, day = (int(part) for part in mm_dd.split("/"))
    candidates = yearless_dates(
        month, day, {period_start.year, period_end.year}, period_start, period_end
    )
    if len(candidates) != 1:
        raise _fail(
            "the transaction date is not uniquely within the statement period",
            source=source,
            column=column,
            row_index=row_index,
        )
    return candidates[0]


def _parse_listed_section(
    text: str,
    *,
    header_re: re.Pattern[str],
    header_loose_re: re.Pattern[str],
    total_re: re.Pattern[str],
    total_loose_re: re.Pattern[str],
    row_re: re.Pattern[str],
    what: str,
    source: str,
) -> tuple[list[re.Match[str]], Decimal, tuple[int, int]] | None:
    """Return ``(rows, section_total, (start, end))`` for the section, or ``None``.

    Returns ``None`` when no trace of the section is found: no header, no ``Total``
    and no row. On a Stock Plan Services report, the caller requires an absent
    dividend or withholding section to have an absent or zero period summary (see
    :func:`_parse_income_activity`). Missing both still parses as zero; Core Fund
    Activity is checked by the per-date sweep identity, and year-end reports skip
    the summary checks. A header / total that
    is present but does not strict-match (e.g. trailing text) makes the loose count
    exceed the strict count and refuses, rather than treating a present section as
    absent and silently dropping its income. When present (exactly one strict header +
    one strict total), the rows are sliced between the header and its ``Total`` line;
    every dated row in that slice must strict-match ``row_re`` (so an unsupported /
    malformed row refuses), and there must be at least one. The row matches, the parsed
    total and the slice's bounds in ``text`` are returned; the slice ends where the
    ``Total`` line starts.
    """
    # A header / total carrying trailing text (so the strict EOL-anchored pattern
    # misses it while the loose line-start pattern still matches) refuses, rather than
    # treating a present section as absent and silently dropping its income.
    if len(header_loose_re.findall(text)) != len(header_re.findall(text)):
        raise _fail(f"{what} section header is malformed", source=source)
    if len(total_loose_re.findall(text)) != len(total_re.findall(text)):
        raise _fail(f"{what} section 'Total' line is malformed", source=source)
    headers = header_re.findall(text)
    totals = total_re.findall(text)
    if not headers:
        # A 'Total' line or a data row with no (or a leading-text-displaced) header
        # means the header is missing/displaced — refuse rather than silently dropping
        # the income. Symmetric with the header-present / total-missing refusal below.
        if totals or row_re.search(text):
            raise _fail(
                f"{what} section content (a row or 'Total') is present without its header",
                source=source,
            )
        return None
    if len(headers) != 1:
        raise _fail(f"expected exactly one {what} section; found {len(headers)}", source=source)
    if len(totals) != 1:
        raise _fail(f"expected exactly one {what} 'Total' line; found {len(totals)}", source=source)
    header_match = header_re.search(text)
    assert header_match is not None  # findall already found exactly one
    total_match = total_re.search(text, header_match.end())
    if total_match is None:
        raise _fail(f"{what} section 'Total' line precedes its header", source=source)
    span = (header_match.end(), total_match.start())
    rows = list(row_re.finditer(text, *span))
    dated_rows = _TXN_DATE_LINE_RE.findall(text, *span)
    if len(rows) != len(dated_rows):
        raise _fail(
            f"{what} section has an unsupported or malformed row "
            f"({len(dated_rows)} dated rows, {len(rows)} parsed)",
            source=source,
        )
    # Every row of this section's type must fall inside the slice; a matching row that
    # appears AFTER the total (or before the header) is not in the slice and would be
    # silently dropped, so refuse. The section row patterns are mutually exclusive and
    # do not match the ESPP "You Bought" purchase rows, so this never false-refuses.
    if len(row_re.findall(text)) != len(rows):
        raise _fail(f"{what} row found outside its section (header..Total)", source=source)
    if not rows:
        raise _fail(f"{what} section is present but has no rows", source=source)
    return rows, _money(total_match.group(1), source=source, column=f"{what}: Total"), span


def _read_lines(rows: list[re.Match[str]], span: tuple[int, int]) -> set[int]:
    """The start of each line a section read: each row's first line and its ``Total``.

    A wrapped name's second line isn't a row, so a dated line there is not read.
    """
    return {row.start() for row in rows} | {span[1]}


def _parse_dividend_section(
    text: str, resolve: _Resolve, *, source: str
) -> tuple[tuple[StatementDividend, ...], set[int]]:
    parsed = _parse_listed_section(
        text, header_re=_DIV_HEADER_RE, header_loose_re=_DIV_HEADER_LOOSE_RE,
        total_re=_DIV_TOTAL_RE, total_loose_re=_DIV_TOTAL_LOOSE_RE, row_re=_DIV_ROW_RE,
        what=DIV_TABLE, source=source,
    )  # fmt: skip
    if parsed is None:
        return (), set()
    rows, total, span = parsed
    dividends: list[StatementDividend] = []
    for i, (mm_dd, security, cusip, amount_raw, name_tail) in enumerate(m.groups() for m in rows):
        gross = _money(amount_raw, source=source, column=f"{DIV_TABLE}: amount", row_index=i)
        _require_positive(gross, source=source, column=f"{DIV_TABLE}: amount", row_index=i)
        name = _rejoined_name(security, name_tail)
        date = resolve(mm_dd, f"{DIV_TABLE}: date", i)
        dividends.append(StatementDividend(date, name, cusip, gross))
    got = sum((d.gross_amount_usd for d in dividends), Decimal(0))
    if got != total:
        raise _fail(
            f"the sum of the dividend rows does not equal the section's 'Total'; {_WHOLE_TABLE}",
            source=source,
            column=f"{DIV_TABLE}: Total",
        )
    return tuple(dividends), _read_lines(rows, span)


def _parse_withholding_section(
    text: str, resolve: _Resolve, *, source: str
) -> tuple[tuple[StatementWithholding, ...], set[int]]:
    parsed = _parse_listed_section(
        text, header_re=_WHT_HEADER_RE, header_loose_re=_WHT_HEADER_LOOSE_RE,
        total_re=_WHT_TOTAL_RE, total_loose_re=_WHT_TOTAL_LOOSE_RE, row_re=_WHT_ROW_RE,
        what=WHT_TABLE, source=source,
    )  # fmt: skip
    if parsed is None:
        return (), set()
    rows, total, span = parsed
    withholdings: list[StatementWithholding] = []
    for i, (mm_dd, security, amount_raw, name_tail) in enumerate(m.groups() for m in rows):
        amount = _money(amount_raw, source=source, column=f"{WHT_TABLE}: amount", row_index=i)
        if amount >= 0:
            raise _fail(
                "Non-Resident Tax must be a negative (withheld) amount",
                source=source,
                column=f"{WHT_TABLE}: amount",
                row_index=i,
            )
        name = _rejoined_name(security, name_tail)
        withholdings.append(
            StatementWithholding(resolve(mm_dd, f"{WHT_TABLE}: date", i), name, amount)
        )
    got = sum((w.amount_usd for w in withholdings), Decimal(0))
    if got != total:
        raise _fail(
            f"the sum of the withholding rows does not equal the section's 'Total'; {_WHOLE_TABLE}",
            source=source,
            column=f"{WHT_TABLE}: Total",
        )
    # The "Total Federal Taxes Withheld" sub-total (no $) must agree: for a non-resident
    # the only withholding is federal (Non-Resident) tax, so it must equal the row sum.
    # It is searched only WITHIN the section slice (between the header and "Total Taxes
    # Withheld"), so a stray/displaced subtotal
    # elsewhere in the document cannot satisfy the check.
    if len(_WHT_FEDERAL_TOTAL_LOOSE_RE.findall(text, *span)) != len(
        _WHT_FEDERAL_TOTAL_RE.findall(text, *span)
    ):
        raise _fail("'Total Federal Taxes Withheld' line is malformed", source=source)
    federal = list(_WHT_FEDERAL_TOTAL_RE.finditer(text, *span))
    if len(federal) != 1:
        raise _fail(
            f"expected exactly one 'Total Federal Taxes Withheld' line; found {len(federal)}",
            source=source,
        )
    federal_column = f"{WHT_TABLE}: Total Federal Taxes Withheld"
    federal_total = _money(federal[0].group(1), source=source, column=federal_column)
    if federal_total != got:
        raise _fail(
            "'Total Federal Taxes Withheld' does not equal the sum of the withholding rows; "
            f"{_WHOLE_TABLE}",
            source=source,
            column=federal_column,
        )
    return tuple(withholdings), _read_lines(rows, span) | {federal[0].start()}


def _parse_mmkt_section(
    text: str, resolve: _Resolve, *, source: str
) -> tuple[tuple[StatementMmktBuy, ...], Decimal | None, set[int]]:
    parsed = _parse_listed_section(
        text, header_re=_MMKT_HEADER_RE, header_loose_re=_MMKT_HEADER_LOOSE_RE,
        total_re=_MMKT_TOTAL_RE, total_loose_re=_MMKT_TOTAL_LOOSE_RE, row_re=_MMKT_ROW_RE,
        what=MMKT_TABLE, source=source,
    )  # fmt: skip
    if parsed is None:
        return (), None, set()
    rows, total, span = parsed
    mmkt_buys: list[StatementMmktBuy] = []
    previous_balance: Decimal | None = None
    for i, (mm_dd, fund, *raws) in enumerate(m.groups() for m in rows):
        quantity, price, amount, balance = (
            _money(
                raw,
                source=source,
                column=f"{MMKT_TABLE}: {field}",
                row_index=i,
                kind=_FIELD_KINDS.get(field, NumberKind.MONEY),
            )
            for raw, field in zip(raws, _MMKT_FIELDS, strict=True)
        )
        for value, field in zip((quantity, price, amount, balance), _MMKT_FIELDS, strict=True):
            _require_positive(value, source=source, column=f"{MMKT_TABLE}: {field}", row_index=i)
        # Displayed quantity is rounded, so allow the 3-dp half-ULP x price (+ cent).
        _require_close(
            amount, quantity * price, _rounding_tolerance(price),
            what="the buy amount and quantity x price", source=source,
            column=f"{MMKT_TABLE}: amount", row_index=i,
        )  # fmt: skip
        if previous_balance is not None and previous_balance + amount != balance:
            raise _fail(
                "the running balance does not equal the previous row's balance plus this "
                "row's amount",
                source=source,
                column=f"{MMKT_TABLE}: balance",
                row_index=i,
            )
        date = resolve(mm_dd, f"{MMKT_TABLE}: date", i)
        mmkt_buys.append(StatementMmktBuy(date, fund.strip(), quantity, price, amount))
        previous_balance = balance
    got = sum((m.amount_usd for m in mmkt_buys), Decimal(0))
    if got != total:
        raise _fail(
            f"the sum of the MMKT buys does not equal the section's 'Total'; {_WHOLE_TABLE}",
            source=source,
            column=f"{MMKT_TABLE}: Total",
        )
    return tuple(mmkt_buys), previous_balance, _read_lines(rows, span)


def _parse_espp_purchase_cash(text: str, resolve: _Resolve, *, source: str) -> set[int]:
    """Read an ESPP purchase's cash in and out, refusing unless they cancel.

    The plan journals the purchase's cash in as ``SPP PURCHASE CREDIT`` rows, footed by
    ``Total Other Activity In``, and the purchase spends it in ``Total Securities
    Bought`` (its ``You Bought`` rows are read by :func:`_parse_espp_purchase_table`).
    The rows must sum to their total, and ``Total Securities Bought`` must be exactly
    that credit as an outflow, so the purchase leaves no cash in the account. The rows
    and both totals appear together or not at all. Returns the start of each line read.
    """
    rows = list(_SPP_CREDIT_ROW_RE.finditer(text))
    credit_totals = list(_OTHER_ACTIVITY_IN_TOTAL_RE.finditer(text))
    bought_totals = list(_SECURITIES_BOUGHT_TOTAL_RE.finditer(text))
    if not (rows or credit_totals or bought_totals):
        return set()
    if not rows or len(credit_totals) != 1 or len(bought_totals) != 1:
        raise _fail(
            "an ESPP purchase's cash must show 'SPP PURCHASE CREDIT' rows with one "
            f"'Total Other Activity In' and one 'Total Securities Bought', or none of them; "
            f"{_WHOLE_DOCUMENT}",
            source=source,
        )
    with localcontext() as context:
        # Keep every cent, including a carry when the credits are added.
        context.prec = max(len(row.group(2)) for row in rows) + len(str(len(rows)))
        context.Emax = context.prec
        credited = Decimal(0)
        for i, (mm_dd, amount_raw) in enumerate(row.groups() for row in rows):
            resolve(mm_dd, f"{_SPP_TABLE}: date", i)
            amount = _money(amount_raw, source=source, column=f"{_SPP_TABLE}: amount", row_index=i)
            _require_positive(amount, source=source, column=f"{_SPP_TABLE}: amount", row_index=i)
            credited += amount
    credit_column = f"{_SPP_TABLE}: Total"
    if credited != _money(credit_totals[0].group(1), source=source, column=credit_column):
        raise _fail(
            f"the SPP PURCHASE CREDIT rows do not sum to the section's 'Total'; {_WHOLE_TABLE}",
            source=source,
            column=credit_column,
        )
    bought_column = f"{_BOUGHT_TABLE}: Total"
    if (
        _money(bought_totals[0].group(1), source=source, column=bought_column)
        != credited.copy_negate()
    ):
        raise _fail(
            "'Total Securities Bought' does not spend exactly the 'Total Other Activity In' "
            f"credit; {_WHOLE_DOCUMENT}",
            source=source,
            column=bought_column,
        )
    return {row.start() for row in rows} | {credit_totals[0].start(), bought_totals[0].start()}


def _unread_dated_line(line: str, *, source: str) -> ParseError:
    """The refusal for a dated line the parser doesn't read: one naming an ESPP
    purchase is a malformed purchase row, any other is unread."""
    if _ESPP_ROW_LOOSE_RE.search(line):
        return _fail(f"an ESPP purchase row is malformed; {_WHOLE_DOCUMENT}", source=source)
    return _fail(
        "a dated line was not consumed by a supported activity section and is not a "
        f"supported ESPP purchase row; {_WHOLE_DOCUMENT}",
        source=source,
    )


def _bought_table_refusal(source: str) -> ParseError:
    return _fail(
        "a 'Securities Bought & Sold' table must show its title, its two-line column header, "
        "its rows, one 'Total Securities Bought' line and one 'Net Securities Bought & Sold' "
        f"line, in that order; {_WHOLE_DOCUMENT}",
        source=source,
    )


def _espp_purchase_table(
    text: str, *, source: str
) -> tuple[list[tuple[int, re.Match[str]]], str, str] | None:
    """Return the ``Securities Bought & Sold`` table's purchase rows, each with its
    line's start, and its ``Total Securities Bought`` and ``Net`` amounts.

    ``None`` means no line names ``Securities Bought`` or ``Net Securities`` and no
    purchase row is printed. Otherwise the statement must print one table: its title,
    then its two-line column header (only blank lines and page-margin lines, which hold
    no lower-case letter, may sit between the two), then its purchase rows, one ``Total
    Securities Bought`` line and one ``Net Securities Bought & Sold`` line, with only
    blank lines between them. Every line naming the table must be its title, ``Total``
    or ``Net`` line, and every purchase row must be one of its rows. Inside the table, a
    line that names the purchase or holds a date and isn't a row refuses as a dated
    line; any other misplaced line refuses as the table's structure.
    """
    lines = [(line.start(), line.group()) for line in re.finditer(r"^[^\n]*", text, re.MULTILINE)]
    labels = {start for start, line in lines if _BOUGHT_LABEL_RE.search(line)}
    rows_anywhere = {start for start, line in lines if _ESPP_ROW_RE.fullmatch(line)}
    if not labels and not rows_anywhere:
        return None
    titles = [i for i, (_, line) in enumerate(lines) if _BOUGHT_TITLE_RE.fullmatch(line)]
    if len(titles) != 1:
        raise _bought_table_refusal(source)

    def next_line(i: int, *, margin: bool = False) -> int:
        """The next line after ``i`` that isn't blank or, with ``margin``, a page-margin
        line; ``len(lines)`` past the end."""
        i += 1
        while i < len(lines) and (
            not lines[i][1].strip() or (margin and not any(c.islower() for c in lines[i][1]))
        ):
            i += 1
        return i

    i = next_line(titles[0], margin=True)
    for header_re in _BOUGHT_HEADER_RES:
        if i == len(lines) or not header_re.fullmatch(lines[i][1]):
            raise _bought_table_refusal(source)
        i = next_line(i)
    rows: list[tuple[int, re.Match[str]]] = []
    while i < len(lines) and not _SECURITIES_BOUGHT_TOTAL_RE.fullmatch(lines[i][1]):
        line_start, line = lines[i]
        row = _ESPP_ROW_RE.fullmatch(line)
        if row is None:
            # A line naming the purchase, even without its date, is a malformed row.
            if _ESPP_ROW_LOOSE_RE.search(line) or _TXN_DATE_LINE_RE.match(line):
                raise _unread_dated_line(line, source=source)
            raise _bought_table_refusal(source)
        rows.append((line_start, row))
        i = next_line(i)
    net_at = next_line(i)
    if net_at >= len(lines):
        raise _bought_table_refusal(source)
    total = _SECURITIES_BOUGHT_TOTAL_RE.fullmatch(lines[i][1])
    net = _NET_BOUGHT_RE.fullmatch(lines[net_at][1])
    if (
        total is None
        or net is None
        or labels != {lines[titles[0]][0], lines[i][0], lines[net_at][0]}
        or rows_anywhere != {start for start, _ in rows}
    ):
        raise _bought_table_refusal(source)
    return rows, total.group(1), net.group(1)


def _espp_purchases_spent(
    rows: list[re.Match[str]], resolve: _Resolve, *, source: str
) -> tuple[tuple[StatementPurchase, ...], Decimal]:
    """Check each purchase row's values and return the rows and the sum of their
    amounts.

    Each row's date must resolve inside the period, its quantity and price must be
    positive, and its amount must be an outflow equal to quantity x price within the
    Holdings tolerance (the quantity prints rounded to 3 dp).
    """
    amount_column = f"{_BOUGHT_TABLE}: amount"
    with localcontext() as context:
        # Keep every digit of each product, difference and sum.
        context.prec = sum(len(raw) for row in rows for raw in row.groups()) + 10
        context.Emax = context.prec
        spent = Decimal(0)
        purchases: list[StatementPurchase] = []
        for index, (_marker, mm_dd, identifier, *raws) in enumerate(row.groups() for row in rows):
            settlement_date = resolve(mm_dd, f"{_BOUGHT_TABLE}: date", index)
            quantity, price, amount = (
                _money(
                    raw,
                    source=source,
                    column=f"{_BOUGHT_TABLE}: {field}",
                    row_index=index,
                    kind=_FIELD_KINDS.get(field, NumberKind.MONEY),
                )
                for raw, field in zip(raws, ("quantity", "price", "amount"), strict=True)
            )
            for value, field in ((quantity, "quantity"), (price, "price")):
                _require_positive(
                    value, source=source, column=f"{_BOUGHT_TABLE}: {field}", row_index=index
                )
            if amount >= 0:
                raise _fail(
                    "an ESPP purchase row's amount is not an outflow",
                    source=source,
                    column=amount_column,
                    row_index=index,
                )
            if (amount.copy_negate() - quantity * price).copy_abs() > _rounding_tolerance(price):
                raise _fail(
                    "an ESPP purchase row's quantity times its price does not equal its amount",
                    source=source,
                    column=amount_column,
                    row_index=index,
                )
            spent += amount
            purchases.append(
                StatementPurchase(settlement_date, identifier, quantity, price, amount)
            )
    return tuple(purchases), spent


def _parse_espp_purchase_table(
    text: str, resolve: _Resolve, *, source: str
) -> tuple[tuple[StatementPurchase, ...], set[int]]:
    """Read and check the ``Securities Bought & Sold`` table's ESPP purchase rows.

    Runs after :func:`_parse_espp_purchase_cash`, whose ``Total Securities Bought``
    spends the purchase's credit. The table must have its exact structure (see
    :func:`_espp_purchase_table`). Each row's footnote marker needs exactly one legend
    line: the marker, then ``Purchase of stock through your company stock plan.``,
    which may run on. The rows' values must hold (see :func:`_espp_purchases_spent`),
    the rows must sum to ``Total Securities Bought``, and the ``Net`` line must equal
    it. Returns the rows, in table order, and the start of each row's line.
    """
    table = _espp_purchase_table(text, source=source)
    if table is None:
        return (), set()
    rows, total_raw, net_raw = table
    legends = [
        (legend.group(1), legend.group(2) is not None) for legend in _LEGEND_RE.finditer(text)
    ]
    for marker in {row.group(1) for _, row in rows}:
        if [complete for letter, complete in legends if letter == marker] != [True]:
            raise _fail(
                "an ESPP purchase row's footnote marker needs exactly one 'Purchase of stock "
                f"through your company stock plan.' legend line; {_WHOLE_DOCUMENT}",
                source=source,
            )
    purchases, spent = _espp_purchases_spent([row for _, row in rows], resolve, source=source)
    bought_column = f"{_BOUGHT_TABLE}: Total"
    bought = _money(total_raw, source=source, column=bought_column)
    if spent != bought:
        raise _fail(
            f"the ESPP purchase rows do not sum to 'Total Securities Bought'; {_WHOLE_TABLE}",
            source=source,
            column=bought_column,
        )
    net_column = f"{_BOUGHT_TABLE} & Sold: Net"
    if _money(net_raw, source=source, column=net_column) != bought:
        raise _fail(
            "'Net Securities Bought & Sold' does not equal 'Total Securities Bought', so the "
            f"table can't prove every activity row was read; {_WHOLE_TABLE}",
            source=source,
            column=net_column,
        )
    return purchases, {start for start, _ in rows}


def _summary_amount(raw: str, *, source: str, column: str) -> Decimal:
    """A summary column: a dash is zero, anything else an amount."""
    return Decimal(0) if raw == "-" else _money(raw, source=source, column=column)


def _summary_candidates(text: str) -> str:
    """Exclude supported data rows and withholding totals from loose label counts."""
    for row_re in (
        _HOLDING_RE,
        _CORE_HOLDING_RE,
        _DIV_ROW_RE,
        _WHT_ROW_RE,
        _MMKT_ROW_RE,
        _WHT_TOTAL_RE,
        _WHT_FEDERAL_TOTAL_RE,
    ):
        text = row_re.sub("", text)
    return text


def _withholding_summary(
    text: str, summary_re: re.Pattern[str], *, source: str
) -> re.Match[str] | None:
    """Return the account summary's ``Taxes Withheld`` line, or ``None``.

    ``summary_re`` is the line's shape for the report type. ``None`` means the
    statement prints no such line, allowed as zero. A malformed, displaced or
    repeated line refuses. On a Stock Plan Services report the line's groups are its
    This Period and Year-to-Date columns: the caller checks This Period against the
    withholding section and, on a statement that starts on January 1 and ends in the
    same year, Year-to-Date against This Period.
    """
    hits = list(summary_re.finditer(text))
    candidates = _WHT_HEADER_RE.sub("", _summary_candidates(text))
    if len(hits) != len(_WHT_SUMMARY_LOOSE_RE.findall(candidates)):
        raise _fail("an account-summary 'Taxes Withheld' line is malformed", source=source)
    if len(hits) > 1:
        raise _fail(
            f"expected at most one account-summary 'Taxes Withheld' line; found {len(hits)}",
            source=source,
        )
    return hits[0] if hits else None


def _income_summary(
    text: str, *, source: str
) -> tuple[dict[str, tuple[Decimal, Decimal]], int] | None:
    """Return the ``Income Summary`` block's rows and its ``Total`` row's line start,
    or ``None``.

    ``None`` means the statement prints no block, allowed as zero. The block must
    have one ``This Period`` / ``Year-to-Date`` header above its rows (left-column
    text may share its line or precede it). Its rows are the first three
    lines after the title that carry a lone dash or a two-decimal amount; they must
    be ``Taxable``, ``Dividends`` and ``Total``, in that order, all printing the same
    two columns. An unsupported row among those three (an ``Interest`` row, say)
    refuses rather than being guessed at.
    """
    titles = list(_INCOME_SUMMARY_TITLE_RE.finditer(text))
    candidates = _summary_candidates(text)
    if len(titles) != len(_INCOME_SUMMARY_LABEL_RE.findall(candidates)):
        raise _fail("an 'Income Summary' title line is malformed", source=source)
    loose_rows = len(_INCOME_SUMMARY_ROW_LOOSE_RE.findall(candidates))
    if not titles:
        if loose_rows:
            raise _fail("an 'Income Summary' row is present without its title", source=source)
        return None
    if len(titles) != 1:
        raise _fail(
            f"expected at most one 'Income Summary' block; found {len(titles)}", source=source
        )
    # Include left-column text on the title's line in the header count.
    block_start = text.rfind("\n", 0, titles[0].start()) + 1
    found = list(_NON_BLANK_LINE_RE.finditer(text, block_start))
    lines = [line.group() for line in found]
    row_at = [i for i, line in enumerate(lines) if i > 0 and _SUMMARY_AMOUNT_TOKEN_RE.search(line)]
    rows: dict[str, tuple[Decimal, Decimal]] = {}
    for index, (label, row_re) in enumerate(_INCOME_SUMMARY_ROW_RES.items()):
        match = row_re.fullmatch(lines[row_at[index]]) if index < len(row_at) else None
        if match is None:
            raise _fail(
                f"'Income Summary' must list exactly the rows Taxable, Dividends and Total; "
                f"its {label!r} row is missing, malformed or follows an unsupported row",
                source=source,
            )
        this_period, year_to_date = match.groups()
        rows[label] = (
            _summary_amount(this_period, source=source, column=f"Income Summary: {label}"),
            _summary_amount(year_to_date, source=source, column=f"Income Summary: {label}"),
        )
    block_rows = [lines[i] for i in row_at[: len(rows)]]
    # Left-column lines may precede the header. Later statement blocks have their own
    # column headers, so look only through this block's Total.
    headers = [
        i
        for i, line in enumerate(lines[: row_at[len(rows) - 1] + 1])
        if "This Period" in line or "Year-to-Date" in line
    ]
    if (
        len(headers) != 1
        or headers[0] > row_at[0]
        or _INCOME_SUMMARY_HEADER_RE.search(lines[headers[0]]) is None
    ):
        raise _fail(
            "'Income Summary' must have a This Period / Year-to-Date column header",
            source=source,
        )
    if loose_rows != sum(len(_INCOME_SUMMARY_ROW_LOOSE_RE.findall(line)) for line in block_rows):
        raise _fail("an 'Income Summary' row appears outside its block", source=source)
    return rows, found[row_at[len(rows) - 1]].start()


def _income_summary_total(
    summary: tuple[dict[str, tuple[Decimal, Decimal]], int] | None, *, source: str
) -> tuple[Decimal, Decimal] | None:
    """Return the block's ``Total`` as its This Period and Year-to-Date columns, or
    ``None`` without a block. Its three rows must print the same amounts. The caller
    checks This Period against the dividend section and, on a statement that starts on
    January 1 and ends in the same year, Year-to-Date against This Period."""
    if summary is None:
        return None
    rows, _total_line = summary
    if len(set(rows.values())) != 1:
        raise _fail(
            "the 'Income Summary' rows Taxable, Dividends and Total disagree (this period or "
            f"year to date); {_WHOLE_TABLE}",
            source=source,
            column="Income Summary",
        )
    return rows["Total"]


def _check_summary(
    summary: Decimal | None,
    detail_total: Decimal | None,
    *,
    summary_line: str,
    column: str,
    section: str,
    source: str,
) -> None:
    """Refuse unless a detail section's ``Total`` matches its summary's This Period.

    An absent section (``detail_total is None``) needs an absent, dash or zero summary;
    a present section needs a summary printing exactly its total.
    """
    if detail_total is None:
        if summary:
            raise _fail(
                f"{summary_line} shows a nonzero amount this period, but the statement has "
                f"no {section} section; {_WHOLE_DOCUMENT}",
                source=source,
                column=column,
            )
        return
    if summary is None:
        raise _fail(
            f"{summary_line} is missing, but the statement has a {section} section; "
            f"{_WHOLE_DOCUMENT}",
            source=source,
            column=column,
        )
    if summary != detail_total:
        raise _fail(
            f"{summary_line} this period does not equal the {section} section's 'Total'; "
            f"{_WHOLE_DOCUMENT}",
            source=source,
            column=column,
        )


def _parse_income_activity(
    text: str,
    period_start: _dt.date,
    period_end: _dt.date,
    report_type: ReportType,
    *,
    source: str,
) -> tuple[
    tuple[StatementDividend, ...],
    tuple[StatementWithholding, ...],
    tuple[StatementMmktBuy, ...],
    tuple[StatementPurchase, ...],
    Decimal | None,
    set[int],
]:
    """Parse the dividend / withholding / MMKT-sweep sections, refusing on any mismatch.

    Each section's row sum is cross-checked against its printed ``Total``. On a Stock
    Plan Services report, the dividend and withholding totals must also match the
    period's summaries (the ``Income Summary`` ``Total`` and the account summary's
    ``Taxes Withheld`` line). An absent dividend or withholding section needs an
    absent or zero summary; missing both still parses as zero. A year-end report
    usually prints no detail sections, so its summaries (an ``Income Summary`` block
    and a one-column ``Taxes Withheld`` line) are checked for shape and not read. Any
    rows it does print are returned: its silence proves nothing, but an income row it
    prints must match the Stock Plan Services reports (#1249). The net
    dividend is swept into the core money-market fund, so on every activity date the
    identity ``gross dividends + signed withholding == MMKT buys`` must hold (e.g.
    ``1.46 + (-0.37) == 1.09``). An ESPP purchase's cash in and out must cancel (see
    :func:`_parse_espp_purchase_cash`), and then its purchase rows are checked (see
    :func:`_parse_espp_purchase_table`). Returned after the income rows are the
    purchase rows, the final core-fund balance and, last, the start of each line
    read: the sections' rows and ``Total`` lines, the ESPP purchase's cash lines and
    rows, and the ``Income Summary`` ``Total``.

    On a Stock Plan Services report that starts on January 1 and ends in the same
    year, This Period and Year-to-Date cover the same days, so each summary's two
    columns must be equal. Those checks run after both summary checks above, income
    first.
    """

    def resolve(mm_dd: str, column: str, row_index: int) -> _dt.date:
        return _resolve_txn_date(
            mm_dd, period_start, period_end, source=source, column=column, row_index=row_index
        )

    dividends, dividend_lines = _parse_dividend_section(text, resolve, source=source)
    withholdings, withholding_lines = _parse_withholding_section(text, resolve, source=source)
    mmkt_buys, mmkt_final_balance, mmkt_lines = _parse_mmkt_section(text, resolve, source=source)
    cash_lines = _parse_espp_purchase_cash(text, resolve, source=source)
    purchases, purchase_lines = _parse_espp_purchase_table(text, resolve, source=source)
    read_lines = dividend_lines | withholding_lines | mmkt_lines | cash_lines | purchase_lines
    income_summary = _income_summary(text, source=source)
    if income_summary is not None:
        read_lines.add(income_summary[1])

    if report_type is ReportType.STOCK_PLAN_SERVICES:
        income_line = "the 'Income Summary' Total"
        withholding_line = "the account-summary 'Taxes Withheld' line"
        income_columns = _income_summary_total(income_summary, source=source)
        _check_summary(
            None if income_columns is None else income_columns[0],
            sum((d.gross_amount_usd for d in dividends), Decimal(0)) if dividends else None,
            summary_line=income_line,
            column=_INCOME_SUMMARY_COLUMN,
            section=f"'{DIV_TABLE}'",
            source=source,
        )
        withholding_summary = _withholding_summary(text, _WHT_SUMMARY_RE, source=source)
        withholding_columns = (
            None
            if withholding_summary is None
            else tuple(
                _summary_amount(raw, source=source, column=_WHT_SUMMARY_COLUMN)
                for raw in withholding_summary.groups()
            )
        )
        _check_summary(
            None if withholding_columns is None else withholding_columns[0],
            sum((w.amount_usd for w in withholdings), Decimal(0)) if withholdings else None,
            summary_line=withholding_line,
            column=_WHT_SUMMARY_COLUMN,
            section=f"'{WHT_TABLE}'",
            source=source,
        )
        # From January 1 to a day in the same year, This Period and Year-to-Date cover
        # the same days, so each summary's two columns must agree.
        if (
            period_start == _dt.date(period_start.year, 1, 1)
            and period_end.year == period_start.year
        ):
            for columns, summary_line, column, scope in (
                (income_columns, income_line, _INCOME_SUMMARY_COLUMN, _WHOLE_TABLE),
                (withholding_columns, withholding_line, _WHT_SUMMARY_COLUMN, _WHOLE_DOCUMENT),
            ):
                if columns is not None and columns[0] != columns[1]:
                    raise _fail(
                        f"{summary_line}'s This Period and Year-to-Date columns differ, but the "
                        f"statement starts on January 1, so both cover the same days; {scope}",
                        source=source,
                        column=column,
                    )
    else:
        withholding_summary = _withholding_summary(text, _YEAR_END_WHT_SUMMARY_RE, source=source)
        if withholding_summary is not None:
            _summary_amount(withholding_summary.group(1), source=source, column=_WHT_SUMMARY_COLUMN)

    if dividends or withholdings or mmkt_buys:
        activity_dates = sorted(
            {d.date for d in dividends}
            | {w.date for w in withholdings}
            | {m.date for m in mmkt_buys}
        )
        table_dates = (
            (DIV_TABLE, [d.date for d in dividends]),
            (WHT_TABLE, [w.date for w in withholdings]),
            (MMKT_TABLE, [m.date for m in mmkt_buys]),
        )
        for position, activity_date in enumerate(activity_dates):
            gross = sum(
                (d.gross_amount_usd for d in dividends if d.date == activity_date), Decimal(0)
            )
            withheld = sum(
                (w.amount_usd for w in withholdings if w.date == activity_date), Decimal(0)
            )
            swept = sum((m.amount_usd for m in mmkt_buys if m.date == activity_date), Decimal(0))
            if gross + withheld != swept:
                raise _fail(
                    "the net dividend (dividends plus withholding) does not equal the MMKT "
                    "sweep on this activity date; its rows: "
                    + _rows_on(activity_date, table_dates),
                    source=source,
                    column="Income activity: daily sweep",
                    row_index=position,
                )

    return dividends, withholdings, mmkt_buys, purchases, mmkt_final_balance, read_lines


def _rows_on(activity_date: _dt.date, table_dates: tuple[tuple[str, list[_dt.date]], ...]) -> str:
    """Name the rows dated ``activity_date`` by table and 0-based position, e.g.
    ``Dividends, Interest & Other Income rows 0 and 1; Core Fund Activity row 0``."""
    parts: list[str] = []
    for table, dates in table_dates:
        rows = [str(i) for i, row_date in enumerate(dates) if row_date == activity_date]
        if len(rows) == 1:
            parts.append(f"{table} row {rows[0]}")
        elif rows:
            parts.append(f"{table} rows {', '.join(rows[:-1])} and {rows[-1]}")
    return "; ".join(parts)


def _require_known_dated_and_total_lines(
    text: str, read_lines: set[int], remnant: re.Match[str] | None, *, source: str
) -> None:
    """Refuse a dated or ``Total`` line the parser didn't read or shape-check.

    ``read_lines`` holds the start of each line the income sections, the ESPP
    purchase's cash and rows, and the ``Income Summary`` read. The identity lines
    (report title, period, holder, ``Participant Number:``), each stock-holding row,
    the core-holding row and its symbol line, and the ``Total Stocks`` / ``Total Core
    Account`` / ``Total Holdings`` lines are set aside too: the parser has already
    checked them, and a holder or holding description may itself hold a date or start
    with ``Total``. Of the remaining lines, a ``Total`` one must have one of the shapes
    accepted unread, and none may be dated. The first other such line in the document
    refuses. Last, ``remnant`` is a printed line with dated text after a page-break
    remnant, if any: the page split let each part be checked, but as printed it is a
    dated line that nothing reads, so it refuses.
    """
    set_aside = set(read_lines)
    for pattern in (
        _REPORT_RE, _PARTICIPANT_RE, _HOLDING_RE, _CORE_HOLDING_RE,
        _TOTAL_STOCKS_RE, _TOTAL_CORE_RE, _TOTAL_HOLDINGS_RE,
    ):  # fmt: skip
        for match in pattern.finditer(text):
            set_aside.add(match.start())
            set_aside.update(i + 1 for i in range(match.start(), match.end()) if text[i] == "\n")
            # The core symbol match leaves a suffix unchecked on its continuation line.
            if pattern is _CORE_HOLDING_RE and re.search(
                _TXN_DATE_TOKEN, text[match.end() :].split("\n", 1)[0]
            ):
                set_aside.discard(text.rfind("\n", 0, match.end()) + 1)
    set_aside.update(text.rfind("\n", 0, match.start(1)) + 1 for match in _HOLDER_RE.finditer(text))
    for line in _DATED_OR_TOTAL_LINE_RE.finditer(text):
        if line.start() in set_aside:
            continue
        if line.group(1):
            for shape, kinds in _UNREAD_TOTAL_LINE_RES:
                match = shape.fullmatch(line.group())
                if match is not None:
                    for raw, kind in zip(match.groups(), kinds, strict=True):
                        if raw is not None and raw not in {"-", "unavailable"}:
                            _money(raw, source=source, column="Total", kind=kind)
                    break
            else:
                raise _fail(
                    "a 'Total' line belongs to no section, holding total or summary this parser "
                    f"reads; {_WHOLE_DOCUMENT}",
                    source=source,
                )
            continue
        raise _unread_dated_line(line.group(), source=source)
    if remnant is not None:
        raise _unread_dated_line(remnant.group(), source=source)


def parse_statement(
    text: str,
    *,
    expected_participant_no: str | None = None,
    source: str = _SECTION,
) -> StatementData:
    """Parse the identity + period of a Fidelity SPS statement, refusing on ambiguity.

    Pure (text in, model out) over ``pdftotext -layout`` output.

    Args:
        text: The extracted statement text.
        expected_participant_no: The config Participant Number; when given, it is
            cross-checked against the document and a mismatch refuses.
        source: Label for the document in error messages.

    Raises:
        ParseError: No report header / participant / holder found, the repeated
            per-page signals disagree, the custodian disclosure is absent, the
            period is inconsistent (start after end, a year-end period that is not
            a full Jan 1 - Dec 31 calendar year, or a year-end title year that
            disagrees with the period), or the participant cross-check fails. On a
            Stock Plan Services report, also when the ``Income Summary`` block or the
            account summary's ``Taxes Withheld`` line is malformed, repeated, or
            disagrees with its detail section (including a section without its
            summary, or a nonzero summary without its section), or, on a report that
            starts on January 1 and ends in the same year, prints different This
            Period and Year-to-Date columns; on a year-end report, when either
            summary has an unsupported shape. On any report, also when a dated line
            or a ``Total`` line is not one the parser read or shape-checked, or an
            ESPP purchase row, its ``Securities Bought & Sold`` table or its legend
            is malformed or doesn't add up. Refusals name ``source`` and never print
            a statement value. Per-row value checks use table/field columns and
            0-based table rows. Whole-document and whole-table value cross-checks
            use row 0 and state their scope. Structural checks may omit ``column``.
    """
    remnant = _PAGE_BREAK_REMNANT_RE.search(text)
    # Before any line is set aside, put dated or ``Total`` text after a page break on a
    # line of its own, so it never shares a line with a header, holder or holding line.
    text = re.sub(rf"(?:\n[ \t]*)?\f[ \t\f]*(?=[^\n]*?{_TXN_DATE_TOKEN}|Total\b)", "\n", text)
    report = _consistent_repeated(
        _REPORT_RE.findall(text),
        len(_REPORT_LABEL_RE.findall(text)),
        empty="no Fidelity SPS statement header found (report type + period)",
        malformed="a report-type header is malformed (report type + period)",
        what="report header",
        source=source,
    )
    phrase, title_year, start_raw, end_raw = report

    participant_no = _consistent_repeated(
        _PARTICIPANT_RE.findall(text),
        len(_PARTICIPANT_LABEL_RE.findall(text)),
        empty="no Participant Number found on the statement",
        malformed="a Participant Number line is malformed",
        what="Participant Number",
        source=source,
    )

    holders = {holder.strip() for holder in _HOLDER_RE.findall(text)}
    if not holders:
        raise _fail("no account holder found above the Participant Number", source=source)
    if len(holders) != 1:
        raise _fail(
            f"the statement names more than one account holder; {_WHOLE_DOCUMENT}",
            source=source,
            column="account holder",
        )
    (account_holder,) = holders
    # An account holder is a name/entity, never a date range. A header missing the
    # holder line (so the period line sits where the holder should) would capture
    # the period string here — refuse rather than file a period as the holder.
    if re.fullmatch(rf"{_DATE}[ \t]*-[ \t]*{_DATE}", account_holder):
        raise _fail(
            "the account holder line looks like a statement period; malformed header",
            source=source,
            column="account holder",
        )

    custodian_match = _CUSTODIAN_RE.search(text)
    if custodian_match is None:
        raise _fail(
            "custodian disclosure ('Fidelity Stock Plan Services LLC') not found; "
            "not a Fidelity SPS statement",
            source=source,
        )

    report_type = ReportType.STOCK_PLAN_SERVICES if phrase == _SPS_PHRASE else ReportType.YEAR_END
    period_start = _date(start_raw, source=source, column="report header: period start")
    period_end = _date(end_raw, source=source, column="report header: period end")
    if period_start > period_end:
        raise _fail(
            f"the period start is after its end; {_WHOLE_DOCUMENT}",
            source=source,
            column="report header: period",
        )
    if report_type is ReportType.YEAR_END:
        if period_start != _dt.date(period_start.year, 1, 1) or period_end != _dt.date(
            period_start.year, 12, 31
        ):
            raise _fail(
                f"the year-end report period is not a full Jan 1 - Dec 31 year; {_WHOLE_DOCUMENT}",
                source=source,
                column="report header: period",
            )
        # The title year (e.g. "2025 YEAR-END...") must match the period year — a
        # mismatch is a contradiction that must refuse, not be silently accepted.
        if int(title_year) != period_start.year:
            raise _fail(
                f"the year-end title year does not match the period year; {_WHOLE_DOCUMENT}",
                source=source,
                column="report header: title year",
            )

    if expected_participant_no is not None and participant_no != expected_participant_no:
        raise _fail(
            "the Participant Number differs from netbenefits.participant_no in user.toml; "
            f"{_WHOLE_DOCUMENT}",
            source=source,
            column="Participant Number",
        )

    holdings, account_nav_usd = _parse_holdings_and_nav(text, source=source)
    dividends, withholdings, mmkt_buys, purchases, mmkt_final_balance, read_lines = (
        _parse_income_activity(text, period_start, period_end, report_type, source=source)
    )
    core_holding = _parse_core_holding(text, source=source)
    # Some income-only statements omit priced holdings; when the core row is absent,
    # the stocks + core == NAV check below remains the completeness guard.
    if (
        account_nav_usd is not None
        and mmkt_final_balance is not None
        and core_holding is not None
        and mmkt_final_balance != core_holding.ending_market_value_usd
    ):
        raise _fail(
            "the final Core Fund Activity balance does not equal the Core Account holding's "
            f"ending market value; {_WHOLE_DOCUMENT}",
            source=source,
            column=f"{MMKT_TABLE}: balance",
        )
    # The account NAV is exactly the stocks subtotal plus the core position. Asserting
    # that identity is what makes it IMPOSSIBLE to silently lose the core fund: it is
    # a Schedule FA asset in its own right, and if a future statement renames the
    # "Core Account" section its holding would parse as absent — reconciling the parts
    # against the whole turns that into a refusal instead of a dropped disclosure.
    if account_nav_usd is not None:
        stocks_total = sum((h.ending_market_value_usd for h in holdings), Decimal(0))
        core_total = (
            core_holding.ending_market_value_usd if core_holding is not None else Decimal(0)
        )
        if stocks_total + core_total != account_nav_usd:
            raise _fail(
                "the stock holdings plus the Core Account do not equal the account NAV; an "
                f"account position is unaccounted for; {_WHOLE_DOCUMENT}",
                source=source,
                column=_NAV_COLUMN,
            )
    _require_known_dated_and_total_lines(text, read_lines, remnant, source=source)
    return StatementData(
        report_type=report_type,
        period_start=period_start,
        period_end=period_end,
        participant_no=participant_no,
        account_holder=account_holder.strip(),
        custodian=custodian_match.group(0),
        holdings=holdings,
        account_nav_usd=account_nav_usd,
        dividends=dividends,
        withholdings=withholdings,
        mmkt_buys=mmkt_buys,
        purchases=purchases,
        core_holding=core_holding,
    )
