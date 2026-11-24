# ADR 0011: Period-bound registrant identity for Robinhood series ETFs

- **Status**: Accepted
- **Date**: 2026-08-24
- **Related**: ADR-0009, ADR-0010, #438, #535, #540
- **Amended**: 2026-11-02 (#793) to make this route the only automatic ETF
  source, with its nature; 2026-11-02 (#826) to stop the run when the GLEIF
  registrant lookup fails; 2026-11-03 (#846) to stop the run on an unusable
  N-CEN selection cache; 2026-11-05 (#540) to record why one ETF stays manual;
  2026-11-09 (#1083) to make malformed N-CEN evidence an SEC source failure

## Context

Several held US ETF ISINs have no direct ISIN-to-LEI mapping. The legal issuer
is the SEC registrant trust, while the held product is one designated series.
Using only the series name mismatches the registrant legal address; using only
the registrant name loses the exact held-series identity.

Large trusts file separate N-CEN forms for different fiscal-year-end groups.
Selecting the registrant's newest filing can therefore choose a filing that
does not contain the held series or that belongs to a later reporting period.

## Decision

For a CY-overlapping Robinhood ETF class:

1. Require exactly one SEC `company_tickers_mf.json` row for its observed
   symbol, yielding CIK, series ID, and class ID.
2. Enumerate current and historical SEC submissions indexes.
3. Consider only N-CEN/N-CEN/A filings whose report period is on or before the
   Schedule FA calendar-year end.
4. Order by report period, amendment status, acceptance time, and accession,
   descending. Select the first filing whose XML contains the exact series ID.
   No post-period fallback is permitted.
5. Require the selected series' exact class ID and ticker to agree with the
   mutual-fund directory.
6. Extract N-CEN registrant name/LEI and official series name. Resolve the
   registrant LEI through exact GLEIF by-LEI lookup.
7. Require GLEIF category `FUND`, `ISSUED`/`ACTIVE`, no successor, country
   agreement, and whitespace/case-normalized equality between GLEIF legal name
   and N-CEN registrant name.
8. File:

   ```text
   Name: <exact GLEIF registrant legalName> - <exact N-CEN series name>
   Address/ZIP: GLEIF registrant legalAddress
   Nature: Investment Company
   ```

Exact source casing and symbols such as `(R)` are preserved. The combined label
must be ASCII, printable, formula-safe, and no longer than 125 characters after
portal rendering; it is never truncated. Multiple series may share one
registrant legal address. Series LEIs are provenance only and never select the
legal person.

Each N-CEN selection is cached immutably by CIK/series/class/cutoff with source
hashes, checked-candidate reasons, and normalized extracted fields. The GLEIF
registrant record uses a separate immutable by-LEI cache key. Cache-only runs
never contact SEC or GLEIF.

Configured address, name, and nature preferences are independent.
`prefer_configured_name = true` keeps a reviewed configured name. Automatic
ETF registrant identity supersedes direct ISIN address evidence when both exist.

> **Amendment (2026-11-02, #793):** This route is now an ETF's only automatic
> source, and its four cells come together or not at all. Name, address, ZIP
> and `Investment Company` are filed only when the registrant passes every
> check above and ADR 0010 rule 5's SEC fund evidence names the same CIK,
> series and class. Otherwise every cell stays manual: no direct exact-ISIN
> GLEIF record fills an ETF's address, ZIP or nature, and SEC fund nature never
> fills a cell alone. An ETF with an `[[fa.entities]]` entry gets no automatic
> cell. Its configured cells stand whatever its `prefer_configured_*` flags
> say, and a cell it leaves out, including the name, stays manual. The
> registrant and nature audits count each failure by reason, and the
> `a3-identity` task names the affected securities and lots.

One ETF remains manual under #540 because its N-CEN registrant LEI resolves
to its adviser rather than to its trust.

> **Amendment (2026-11-05, #540):** The N-CEN value is a filing error, and
> the ETF stays manual. Form N-CEN Item B.1.d asks for the registrant's own
> LEI; an adviser's LEI belongs in Item C.9.a.iv. The trust reports its
> adviser's LEI as `registrantLei` in every N-CEN checked, for report periods
> from 2019 to 2026, amendments included. The ETF's N-PORT for 2025-12-31
> reports the same LEI as `regLei`. The trust's own LEI (GLEIF category
> `FUND`) appears in none of the trust's own filings in EDGAR full-text
> search. GLEIF ties that LEI to the trust's exact CIK and EDGAR
> registration. It records the ETF's series LEI under its series ID as a
> sub-fund of the trust, and the adviser only as fund manager. The trust and
> the adviser share one GLEIF legal address. The trust's name is spelled
> differently in its Delaware certificate of trust, on EDGAR and in N-CEN,
> and in GLEIF, whose spelling changed during 2025. #540 records the filings
> and identifiers.
>
> Nothing replaces the reported LEI. A GLEIF lookup by CIK would override the
> issuer's own filing. So the route keeps refusing this ETF as
> `registrant-identity-mismatch`, and its four cells stay manual for an
> `[[fa.entities]]` entry. If a selectable N-CEN or N-CEN/A ever reports the
> trust's own LEI, the unchanged route checks it like any other registrant,
> and rule 8 files GLEIF's spelling.

## Consequences

- Current ETF classes can resolve through registrant evidence; the #540 ETF
  stays manual.
- Series of one trust correctly share that trust's registered address.
- Post-period renames do not rewrite CY2025 identity.
- Missing historical indexes, exact series/class/ticker disagreement, adviser
  LEIs, source outages, or portal-shape failures remain manual.
  > **Amendment (2026-11-02, #826):** A GLEIF registrant lookup that fails now
  > stops the run (exit 4) instead of leaving the ETF manual: a corrupt or
  > unreadable registrant cache artifact, a failed, malformed or mismatched
  > GLEIF response, or an invalid registrant LEI. The ADR depositary route
  > shares this lookup and stops the same way. SEC failures, including an
  > unusable N-CEN selection cache, still leave the registrant route
  > unresolved, as does a registrant record that is valid but fails a check
  > above. A cache-only run with no registrant artifact is still a cache miss.
  >
  > **Amendment (2026-11-03, #846):** An unusable N-CEN selection cache
  > artifact now stops the run (exit 4) too, naming a fixed problem and the
  > N-CEN cache folder, never the ETF's ISIN or the
  > artifact's path: one that can't be inspected or read, isn't JSON, is
  > nested too deeply, holds a non-finite number, fails its integrity check,
  > or fails any of the reader's checks. Each field must have the JSON type
  > its writer writes, and a cached identity must pass the fresh N-CEN checks
  > on its registrant LEI, form and accession. SEC fetch failures still leave
  > the registrant route unresolved, and a cache-only run with no N-CEN
  > artifact is still a cache miss.
  > (Changed 2026-11-09 by #1044, PR #1060.)
  >
  > **Amendment (2026-11-09, #1083):** A malformed series ID, form cell,
  > acceptance time or other selected N-CEN XML field is an SEC source failure
  > that leaves the ETF manual, never a reason to select an older filing.
- Legal nature and every financial/lot field remain unchanged.
  > **Note (2026-11-02, #793):** Legal nature is now part of this route. A
  > resolved ETF files `Investment Company`; an ETF whose direct GLEIF record
  > used to supply another legal form, or an address, now files the
  > registrant's cells or stays manual.
