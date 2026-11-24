# ADR 0010: Evidence-backed legal nature for Robinhood A3

- **Status**: Accepted
- **Date**: 2026-08-23
- **Related**: ADR-0009, #438, #535
- **Amended**: 2026-11-02 (#793) to bind series-ETF nature to the ADR 0011
  registrant

## Context

Schedule FA Table A3 requires a free-text `Nature of entity` value of at most
34 characters. Broker classifications such as `COMMON` and `ETF` describe the
held instrument, not the legal form of its issuer, and remain inadmissible.

GLEIF records expose ISO 20275 Entity Legal Form (ELF) codes and free-text
reserved forms. GLEIF coverage is incomplete for funds, while SEC's exact
mutual-fund class file and registration forms can prove registered investment
company status.

## Decision

Resolve legal nature independently from address/ZIP:

1. An exact GLEIF record must be `ACTIVE`, successor-free, country-consistent,
   and either `ISSUED` or `LAPSED`. `LAPSED` means reference-data revalidation
   was missed; it blocks mutable address use but does not erase a stable ISO
   20275 legal-form identifier when ADR 0009's SEC identity/address checks
   independently match the broker issuer name. If SEC address matching needed
   the lapsed GLEIF name itself, legal nature instead comes independently from
   SEC incorporation evidence. For an ADR, the record is usable only after ADR
   0009's Form F-6 route proves that it belongs to the depositary.
2. Use the unique English label from the vendored GLEIF ISO 20275 table when it
   is active, ASCII, printable, formula-safe, and at most 34 characters.
3. Reserved code `8888` is accepted only for `entity.category == FUND` and
   case-insensitive raw value `fund`, rendered as `Fund`. Code `9999` is never
   inferred.
4. An overlength label requires a reviewed raw-label alias that is a contiguous
   token subsequence. The initial alias is
   `For-Profit Corporation General Stock` to `For-Profit Corporation`.
5. If GLEIF nature is unavailable, exactly one SEC mutual-fund class row plus a
   observed `N-CEN`, `N-CSR`, `N-CSRS`, or `NPORT-P` filing proves
   `Investment Company`. Registrant names and missing forms prove nothing.
6. For a US common stock, exactly one SEC ticker/CIK row, a US
   `stateOfIncorporation`, 10-K/10-K-A filing-family evidence, and an
   affirmative `Inc`/`Incorporated`/`Corp`/`Corporation` legal-name token prove
   `Corporation`. Partnerships, LLCs, and trusts remain manual. Instrument
   label `COMMON` alone still proves nothing.

No N-8B-2 special case is implemented because the current A3 population has no
unmapped fund for which that historical form is present.

Automatic nature is source-first. `[[fa.entities]]` remains fallback;
`prefer_configured_nature = true` is the explicit reviewed opt-out. Address and
nature preferences are independent. A GLEIF request is skipped only when both
configured address and nature are preferred.

> **Amendment (2026-11-02, #793):** For a series ETF (`sub_category = "ETF"`,
> not a depositary receipt), rule 5 is the only route, and it also needs ADR
> 0011's registrant. `Investment Company` is accepted only when the ETF's N-CEN
> registrant passes every ADR 0011 check and the SEC fund row names the same
> CIK, series and class. GLEIF legal form is never used for an ETF, and the
> ETF's ISIN is not sent to GLEIF. A registrant without this nature fills no
> cell either (ADR 0011). An ETF with an `[[fa.entities]]` entry gets no
> automatic nature: its configured nature stands, or the cell stays manual,
> whatever `prefer_configured_nature` says. A depositary receipt still takes
> its legal form from its Form F-6-proven depositary record (rule 1).

The GLEIF per-ISIN artifact schema retains raw ELF code/`other`, category,
statuses, and provenance. ISO 20275 labels are vendored as a deterministic
reference CSV with a source/hash companion file. Automatic nature evidence
records the raw value, selected output, transformation reason, and LEI or exact
SEC CIK/series/class/forms.

## Consequences

- A GLEIF record with ELF code `H1UM` (US-CA) files `For-Profit Corporation`
  through rule 4's alias; one with `XTIQ` (US-DE) files `Corporation`.
- Exact registered investment-company evidence files `Investment Company`.
- Unresolved ADR, inactive/successor, missing, ambiguous, non-English,
  portal-incompatible, and otherwise unsupported records remain manual.
- Nature-only evidence cannot change or blank address/ZIP.
- Lapsed address evidence remains unusable even when its stable legal form is
  accepted.
