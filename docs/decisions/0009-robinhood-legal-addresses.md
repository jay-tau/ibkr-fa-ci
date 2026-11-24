# ADR 0009: Exact-ISIN legal addresses for Robinhood A3

- **Status**: Accepted
- **Date**: 2026-08-23
- **Supersedes in part**: ADR-0008's A3 business-address decision
- **Related**: #438, #535
- **Extended by**: ADR-0011 for series-ETF registrant identity
- **Amended**: 2026-08-27 to allow a bounded SEC fallback; 2026-11-02 (#792)
  to name the GLEIF outcomes that may lead to it; 2026-11-02 (#793) to take
  series ETFs off the exact-ISIN route; 2026-11-03 (#846) to stop the run on
  an unusable Form F-6 cache; 2026-11-03 (#853) to stop the run on an unusable
  entry in the post-FY snapshot search; 2026-11-09 (#1082) to require every
  observed broker name to agree

## Context

Schedule FA Table A3 asks for the address of the entity whose financial asset
is held but does not define registered/legal address versus operational
headquarters. Those facts can differ. A Delaware corporation headquartered
outside the US can have its Delaware registered office as its GLEIF legal
address while its SEC business address is its principal executive office
abroad.

The filing policy is the legal issuer of the held instrument and that issuer's
legal address of record. Business/headquarters addresses are not substitutes.

> **Note (2026-11-02, #777):** The 2026-08-27 amendment under Decision allows
> one bounded substitute: a US common stock's SEC principal business address.
> The 2026-11-02 amendment (#792) allows it only after a verified GLEIF
> zero-match or an otherwise valid `LAPSED` GLEIF record.

## Decision

Resolve Robinhood A3 legal addresses independently from security-identity
generation, after every security already has a verified or configured ISIN.
Use only the exact GLEIF ISIN filter and `entity.legalAddress`:

- exactly one LEI record;
- registration status `ISSUED`, entity status `ACTIVE`, and no successor;
- legal-address country equal to the A3 row's configured issuer country;
- complete street, city, country, and postal code;
- ASCII portal text, address at most 200 characters, and postal code at most
  eight characters without truncation.

> **Note (2026-11-02, #777):** These checks govern the GLEIF route. The
> 2026-08-27 amendment below adds an SEC business-address fallback for US
> common stock, with its own checks and a ZIP+4-to-ZIP5 reduction.

No ticker/name/fuzzy lookup, OpenFIGI, Massive, parent/sponsor selection, or
GLEIF headquarters field is used. Direct ISIN-to-LEI lookup remains disabled
for ADRs because it commonly maps to the underlying issuer. Instead, supplied
ADR evidence may contain one SEC Form F-6 URL. The cover must name the same
issuer as the broker description and one reviewed depositary charter name.
That name selects the depositary's reviewed LEI; the existing exact-LEI GLEIF
checks then supply its legal name and address. The initial depositaries are
Citibank, N.A. and The Bank of New York Mellon. Any other depositary remains
manual until its name-to-LEI binding is reviewed.

> **Amendment (2026-11-03, #846):** An unusable Form F-6 cache artifact stops
> the run (exit 4), naming a fixed problem and the F-6 cache folder, never the
> ADR's ISIN or the artifact's path: one that
> can't be inspected or read, isn't JSON, is nested too deeply, holds a
> non-finite number, fails its integrity check, or fails any of the reader's
> checks, including a field of an unexpected JSON type. A failed F-6 fetch,
> including a cover that names another issuer, still leaves the row manual,
> and a cache-only run with no F-6 artifact is still a cache miss.
> (Changed 2026-11-09 by #1071, PR #1102.)

When exact GLEIF address evidence is unavailable or lapsed, a US common-stock
fallback may use the SEC filer's current principal business address. It
requires all of:

- an integrity-checked schema-v2 identity snapshot fetched after the target
  FY's March 31 end;
- exact configured CUSIP/symbol/ISIN identity and one SEC ticker/CIK row;
- legal name agreement with both SEC names by every broker issuer name
  observed for the CUSIP, or by the exact-ISIN GLEIF legal name; a GLEIF
  name is usable only from an `ISSUED`/`LAPSED`, `ACTIVE`, successor-free,
  country-consistent record;
- 10-K/10-K-A filing-family evidence;
- a US-state business address with complete street, city, and ZIP;
- portal-safe ASCII fields.

> **Amendment (2026-11-02, #792):** "Unavailable or lapsed" means one of two
> GLEIF outcomes for the held ISIN, and no other:
>
> - a verified zero-match: GLEIF answered the exact-ISIN query with no record;
> - a `LAPSED` record that passes every GLEIF-route check above except
>   `ISSUED`, whose raw LEI, legal-name, status, successor and legal-address
>   fields had their expected JSON types before normalization, and whose legal
>   name agrees strictly with both SEC names. Broker name agreement never
>   overrides a conflicting GLEIF name.
>
> Every other outcome blocks the SEC address, ZIP and SEC common-stock nature:
> a country-mismatched, inactive, successor, incomplete or
> portal-incompatible record, multiple matches, or a cache-only miss.
> The row keeps a reviewed `[[fa.entities]]` value or stays manual, and the run
> counts each blocking outcome. A corrupt or unreadable exact-ISIN GLEIF cache
> artifact, or a failed exact-ISIN GLEIF fetch, stops the run (exit 4) instead
> of leaving the row manual. A `LAPSED` record cached before this amendment
> carries no type proof and stays blocked until its artifact is moved aside and
> fetched again. The
> post-FY snapshot search below still ranks every candidate, including a
> blocked one, so blocking one security never changes another's snapshot.

> **Amendment (2026-11-02, #821):** The type proof is gone. As the loader reads
> each field from a GLEIF response, it checks that value's JSON type before
> cleaning it: the envelope, every record (including each record of a
> multiple match), and each record's fields. This covers the
> exact-ISIN route and the ADR depositary and ETF registrant routes. A field
> of an unexpected type stops the run (exit 4) before the response is used or
> cached, naming the field's JSON path and the GLEIF cache folder, never the
> requested ISIN or LEI, the artifact's path or the field's value.
> (Changed 2026-11-09 by #1044, PR #1060, and 2026-11-10 by #1086, PR #1128.)
> An absent or `null` required object keeps its existing handling.
> Every record the tool uses has passed
> this check, so a `LAPSED` record that passes every other check above allows
> the SEC fallback. Cache artifacts written before this amendment are never
> read: with fetching on, the record is fetched again and checked, and in a
> cache-only run it is a cache miss, which leaves the row configured or
> manual. They no longer need moving aside, as the #792 amendment required.

> **Amendment (2026-11-02, #829):** A record has a successor when GLEIF names
> one in any form: a `successorEntity` with an LEI or with only a name, or any
> `successorEntities` entry. Such a record supplies no GLEIF evidence on any
> route: no exact-ISIN address or nature, no ADR depositary or ETF registrant
> cells, and no SEC fallback. Cache artifacts written before this amendment are
> never read, as for #821.

The fallback never applies to ETFs, ADRs, foreign issuers, name changes,
non-US business addresses, or unsupported security types. SEC and Nasdaq
exchange/name records must also agree. SEC state codes render as `US-XX` to
match the GLEIF region convention. ZIP+4 is reduced to its standard ZIP5
component solely for the portal's eight-character limit; the exact ZIP+4
remains in the immutable cache. GLEIF remains first priority.
One uncovered common stock or fund remains manual without disabling other
covered securities. A fully configured address/name/nature opt-out does not
participate in snapshot coverage.

ADR 0011 adds a second exact route for SEC series ETFs whose ISIN is absent from
GLEIF: exact SEC class/series evidence selects the registrant LEI, and GLEIF
then supplies that registrant's legal name and address.

> **Amendment (2026-11-02, #793):** A series ETF (`sub_category = "ETF"`, not
> a depositary receipt) no longer uses the exact-ISIN GLEIF route at all. Its
> ISIN is not sent to GLEIF, and no direct record fills its address or ZIP,
> even when its registrant route fails. ADR 0011's registrant route is its only
> automatic source.

The CBDT bulk-upload path accepts ASCII only. A non-ASCII legal address remains
manual unless one reviewed ISIN-specific alias exactly equals Unicode NFD
decomposition with only combining marks removed and the result is wholly ASCII.
The raw GLEIF text remains in the immutable artifact and the run warns that an
alias was used. This is not a general transliteration facility.

Configured `[[fa.entities]]` values remain fallback data. Setting
`prefer_configured_address = true` is the explicit reviewed opt-out from GLEIF
for one ISIN. Address and postal code always come atomically from one source;
configured name and nature remain usable with an automatic address.

> **Amendment (2026-11-15, #1449, owner decision):** "If the override doesnt
> have an address, try to find the app address. Write it out in a way that the
> user has to verify the appropriate address beofre generating everything." An
> `[[fa.entities]]` entry may leave out `address`, but not while it sets `zip`
> or `prefer_configured_address = true`. For a held security, Schedule FA then
> stops (exit 4) before any Schedule FA price fetch or Schedule FA output: the
> automatic address and ZIP the routes above already found for the ISIN, or a note that none was
> found, go to the private `robinhood-identity-guidance.toml` as a block with
> the values commented out and `prefer_configured_address = true`, which
> refuses at load until the filer uncomments a verified address. No new lookup
> or cache is added, and a configured series ETF still takes no automatic
> route. A holding with no entry keeps its automatic address as before.

> **Amendment (2026-11-02, #793):** A series ETF is the exception. When it has
> an `[[fa.entities]]` entry, no automatic value fills or replaces any of its
> cells, whatever its `prefer_configured_*` flags say. A cell the entry leaves
> out stays manual, including the name: the broker description does not
> complete an ETF's legal identity.

GLEIF requests obey the existing fetch/cache-only switch. A cold fetch sends
the exact held ISIN to GLEIF. Each outcome is stored in a separate immutable
artifact under a hashed per-ISIN path, including LEI metadata, legal address,
entity/registration status, GLEIF golden-copy date, retrieval time, source URL,
and hashes. The SEC fallback first searches verified post-FY schema-v2 snapshots. If none
exists and fetching is enabled, the ordinary Nasdaq/SEC identity fetch may run;
the resulting snapshot must then pass the same post-FY/schema/coverage checks
before presentation use. A corrupt candidate is surfaced and never hidden by
network fallback.

> **Amendment (2026-11-03, #853):** Surfaced now means the run stops (exit 4),
> naming a fixed problem and the snapshot cache folder, never an entry in it
> (changed 2026-11-09 by #1071, PR #1102), before any network fallback. Every cached
> artifact, whichever securities it covers and whatever its schema, must be
> read, parsed and pass its integrity, schema and record checks before the
> search may skip it; its schema must be 1 or 2. The cache folder and each
> entry in it must be a folder that can be listed, after following a link. A
> missing cache folder, a fingerprint folder without an artifact, a valid
> schema-v1 artifact and a valid artifact that covers none of the requested
> securities are still skipped, and a failed fallback fetch still leaves the
> routes unresolved.

A covering pre-cutoff artifact cannot be refreshed in place; the run names it
and requires a new cache directory for the post-FY snapshot.

## Consequences

- A Delaware corporation headquartered abroad files with US country and its
  Delaware legal address and ZIP; the headquarters abroad is not used.
- Manual identities and exact-mapped funds can receive legal addresses without
  weakening their independently reviewed security identity.
- Missing, ambiguous, stale, inactive, successor, unresolved-ADR,
  country-mismatched, or portal-incompatible records retain the existing
  country-name address, blank ZIP, and A3 manual work.
  > **Note (2026-11-12, #1289):** Such a row without an `[[fa.entities]]`
  > entry now files a blank address instead of the country name. Its blank
  > ZIP and A3 manual work are unchanged.
  > **Note (2026-11-02, #777):** For a US common stock, the 2026-08-27
  > amendment's SEC fallback can still fill a row whose GLEIF lookup was a
  > verified zero-match or found only a lapsed record. Since the 2026-11-02
  > amendment (#792) every other GLEIF result blocks it, closing the gap
  > [ADR 0018](0018-ibkr-a3-entity-evidence.md) recorded.
- GLEIF coverage is incomplete, so legal-address automation is partial and
  never guesses.
- Legal nature is independently governed by ADR 0010; all tax/DTAA
  source-country decisions remain separate.
