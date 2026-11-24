"""Indian-company ADR / GDR underlying-issuer mapping (issue #183).

This module is the hardcoded source of truth for mapping an IBKR-emitted
ADR / GDR ISIN to its **underlying Indian-company issuer**, so income
and FTC pipelines can optionally treat the dividend as Indian-source
rather than depositary-country-source.

The mapping is **opt-in only** — gated on
``user.toml`` ``[os].schedule_os_indian_adr_routing =
"underlying-issuer"`` per the policy convention established by
``docs/portal/adr-handling.md``: *"Either path requires the user's
explicit decision — ibkr-fa cannot automate it without making a
policy choice that isn't its to make."*

Source of truth: the table in ``docs/portal/adr-handling.md``.

Coverage:

* **US ADRs of Indian companies** — INFY, IBN, HDB, WIT, RDY. These
  are the canonical Indian-business ADRs trading on NYSE / NASDAQ
  with US-prefix ISINs.
* **Non-US-prefix Indian-business depositary receipts** — WNS (Jersey
  ``JE...`` prefix), YTRA (Cayman ``KY...`` prefix). These are
  flagged because the contrarian underlying-issuer view applies to
  them too even though they don't have a US ISIN prefix. (An entry
  was dropped from this list — see *Historical / dropped entries*
  below.)

Out of scope (per issue #183 acceptance criteria):

* **GDRs** (London / Luxembourg) — the ``GB`` / ``LU`` prefixed
  depositary receipts. Less common in IBKR holdings; can be added
  later if real-world demand surfaces.
* **Auto-detection from IBKR description text** — too brittle (the
  description format varies across IBKR product types and the
  underlying-issuer is not consistently present).

Extending the table:

To add a new ADR, edit the :data:`INDIAN_ADR_MAPPING` dict below and
add the ISIN → ``IndianAdrEntry`` tuple. The ISIN must be the
12-character ISO 6166 identifier IBKR emits in the Dividends section
description prefix (cross-reference ``docs/portal/adr-handling.md``
for the canonical list). After adding, run the gates + the e2e
schedule-os tests against your IBKR Activity Statement to verify the
new entry is picked up by the compute layer's lookup.

Coverage gaps:

The mapping table is **exhaustive** — only ISINs explicitly listed
in :data:`INDIAN_ADR_MAPPING` are re-routed. There is no heuristic
matching (e.g., on description text like "INDIAN DEPOSITARY" or
"INDIA LTD ADR"), because heuristic-driven re-routing would silently
mis-classify rows in either direction (false positives: a US-issued
"India ETF" matching the heuristic would be wrongly routed to 1a(i);
false negatives: a new Indian ADR without "INDIA" in the description
would silently NOT be re-routed). Users who hold an Indian-company
ADR / GDR not in the mapping should file an issue with the IBKR-
emitted ISIN + the underlying company name to request a mapping
addition.

Historical / dropped entries:

* PR #184 dropped an entry whose source-derived ISIN failed the
  ISO 6166 check, rather than chase the correct ISIN. See
  ``docs/portal/adr-handling.md`` for the user-facing rationale
  and ``_isin_check_digit_valid`` below for the check added in the
  same PR, which guards the table against a mistyped entry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from ibkr_fa.types import Isin, Ticker, isin_check_digit


@dataclass(frozen=True, slots=True)
class IndianAdrEntry:
    """One row of the Indian-company ADR mapping table.

    Attributes:
        canonical_ticker: The IBKR-emitted ticker handle for this ADR
            (e.g., ``Ticker("INFY")``). Pinned here as defensive
            cross-reference so a future maintainer who edits an ISIN
            without updating the ticker (or vice versa) is more
            likely to notice the mismatch.
        underlying_issuer_name: Human-readable name of the underlying
            Indian company (e.g., ``"Infosys Ltd"``). Surfaced in the
            CLI advisory ("N ADR row(s) re-routed: INFY → Infosys,
            HDB → HDFC Bank, ...") so the user can verify the
            re-routing matches their CA's expectation.
        depositary_country_iso2: ISO 3166-1 alpha-2 of the depositary
            country (the country derived from the ISIN's first two
            characters). Pinned here for the audit trail; lets the
            compute layer surface the BEFORE state alongside the
            AFTER state in the audit columns.
    """

    canonical_ticker: Ticker
    underlying_issuer_name: str
    depositary_country_iso2: str


# Canonical mapping of IBKR-emitted ADR / GDR ISIN → underlying Indian
# company. ISINs verified against ``docs/portal/adr-handling.md`` table.
INDIAN_ADR_MAPPING: Final[dict[Isin, IndianAdrEntry]] = {
    Isin("US4567881085"): IndianAdrEntry(
        canonical_ticker=Ticker("INFY"),
        underlying_issuer_name="Infosys Ltd",
        depositary_country_iso2="US",
    ),
    Isin("US45104G1040"): IndianAdrEntry(
        canonical_ticker=Ticker("IBN"),
        underlying_issuer_name="ICICI Bank Ltd",
        depositary_country_iso2="US",
    ),
    Isin("US40415F1012"): IndianAdrEntry(
        canonical_ticker=Ticker("HDB"),
        underlying_issuer_name="HDFC Bank Ltd",
        depositary_country_iso2="US",
    ),
    Isin("US97651M1099"): IndianAdrEntry(
        canonical_ticker=Ticker("WIT"),
        underlying_issuer_name="Wipro Ltd",
        depositary_country_iso2="US",
    ),
    Isin("US2561352038"): IndianAdrEntry(
        canonical_ticker=Ticker("RDY"),
        underlying_issuer_name="Dr. Reddy's Laboratories Ltd",
        depositary_country_iso2="US",
    ),
    # Non-US-prefix Indian-business depositary receipts (per
    # docs/portal/adr-handling.md). The contrarian underlying-issuer
    # view still applies — they're Indian-company exposure via a
    # non-US depositary structure (Jersey / Cayman). Listed here so a
    # user opting into the "underlying-issuer" routing mode gets
    # consistent treatment across all their Indian-company
    # depositary-receipt holdings.
    Isin("JE00B3DCF752"): IndianAdrEntry(
        canonical_ticker=Ticker("WNS"),
        underlying_issuer_name="WNS (Holdings) Ltd",
        depositary_country_iso2="JE",
    ),
    Isin("KYG983381099"): IndianAdrEntry(
        canonical_ticker=Ticker("YTRA"),
        underlying_issuer_name="Yatra Online Inc",
        depositary_country_iso2="KY",
    ),
}


def lookup_indian_adr(isin: Isin) -> IndianAdrEntry | None:
    """Look up an ISIN in the Indian-company ADR mapping.

    Args:
        isin: 12-character ISO 6166 ISIN as emitted by IBKR.

    Returns:
        The matching :class:`IndianAdrEntry` if the ISIN is mapped,
        ``None`` otherwise. Callers in the compute layer use this
        in the ``"underlying-issuer"`` routing branch only; the
        ``"depositary"`` (default) branch never calls this and
        treats all non-IN-prefix dividends as foreign-source.
    """
    return INDIAN_ADR_MAPPING.get(isin)


def _isin_check_digit_valid(isin: str) -> bool:
    """Verify the ISO 6166 modified-Luhn check digit of an ISIN.

    The digit itself comes from :func:`ibkr_fa.types.isin_check_digit`.
    That helper expands characters with ``int(c, 36)``, which accepts
    lowercase letters and non-ASCII decimal digits and raises on
    punctuation, so the shape (uppercase ASCII only) is screened here
    first.

    This is the validation the upstream parser layer in
    :mod:`ibkr_fa.parser._isin` explicitly defers (per its own
    docstring: *"IBKR-emitted ISINs are not observed to fail Luhn,
    so deferring the check is the right priority trade-off"*). Used
    here in the hardcoded :data:`INDIAN_ADR_MAPPING` table to catch
    typos / press-release errors in at-rest data (PR #184 found two
    entries of the original table that failed Luhn).

    Args:
        isin: A 12-character ISIN candidate. Anything else returns
            ``False`` (we never raise — the regex-shape check is the
            upstream parser's job, this helper only verifies the
            check digit).

    Returns:
        ``True`` if the check digit matches, ``False`` otherwise.
    """
    if _ISIN_SHAPE.fullmatch(isin) is None:
        return False
    return isin_check_digit(isin[:-1]) == isin[-1]


# 11 uppercase-ASCII alphanumerics then a decimal check digit.
_ISIN_SHAPE: Final[re.Pattern[str]] = re.compile(r"[0-9A-Z]{11}[0-9]")
