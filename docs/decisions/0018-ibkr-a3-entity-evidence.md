# ADR 0018: Source-neutral entity evidence for IBKR A3 rows

- **Status**: Accepted
- **Date**: 2026-11-01
- **Issue**: #638
- **Related**: ADR-0008, ADR-0009, ADR-0010, ADR-0011, #351, #438, #539,
  #540, #588, #628, #634, #635
- **Amended**: 2026-11-02 (#776) to record the decision 7 evidence for
  options (b) and (c); 2026-11-02 (#813) to accept owner decision 7 = (a)
  and park decisions 1 to 6; 2026-11-02 (#827) to describe Robinhood series
  ETFs after #793; 2026-11-05 (#881) to correct where the price-cache
  defaults sit; 2026-11-09 (#1082) to narrow the Robinhood name-containment
  limit; 2026-11-12 (#1289) to leave the address blank, not the country name,
  for an ISIN without an entry

## Context

This record asks one question. Can the GLEIF and SEC entity-evidence routes
built for Robinhood (ADR-0008 to ADR-0011) take a source-neutral instrument
identity and fill IBKR Schedule FA A3 `Name of entity`, `Address of entity`,
`ZIP Code` and `Nature of entity` cells without guessing any value?

Code references are at `e8f7d00`. #638 was drafted at `71dd830`. Since then
#733 removed the unread IBKR instrument fields from the lot models, and #755
split each Robinhood loader from its compute decision. Both changes are
reflected below. #768 (#628) merged after the pin; the input contract says
what it changed.

### IBKR A3 identity today

- `pipeline_fa.py:311-320` passes `build_fa_a3_rows` only the
  `[[fa.entities]]` entries (`:317`) and no `nature_by_isin`.
- An ISIN without an entry gets the country name as its address
  (`compute/fa_a3.py:179`) and `_DEFAULT_ZIP_CODE = ""` as its ZIP (`:76`,
  `:180`). Its nature is blank (`:186-194`), and the CLI names the holding
  (`cli.py:2982-2990`).
  > **Note (2026-11-12, #1289):** The country name proved nothing about the
  > issuer's address, so the owner chose to leave it blank. An ISIN without an
  > entry now gets a blank address and a blank ZIP, and the CLI names the
  > holding under `Address of entity` and `ZIP Code` as it does for nature.
  > An entry without `zip` is named under `ZIP Code`. The same blank applies
  > to Robinhood rows without an automatic identity or an entry. Decisions 1
  > to 7 are unchanged.
- The official AY 2026-27 ITR-2 schema requires all four cells:
  `NameOfEntity` 1-125, `AddressOfEntity` 1-200, `ZipCode` 1-8 and
  `NatureOfEntity` 1-34 characters ([schema][itr2-schema],
  `definitions.DtlsForeignEquityDebtInterest`).
- The notified ITR-2 form ([Notification 46/2026][itr2-ay2026], Schedule FA
  table A3, PDF p. 27) only names the columns. The ITD FA/FSI guide
  ([Table A3][fa-guide]) and the newest ITR-2 instructions found
  ([AY 2023-24, PDF p. 38][itr2-instructions]) describe the A3 amounts but
  define none of the four identity cells.

### Robinhood routes

| Route | Loader (I/O) | Decision (compute) | Keyed by | Fills |
|---|---|---|---|---|
| GLEIF exact-ISIN legal address | `robinhood_legal_entity_sources.py:739` | `compute/robinhood_entity_overrides.py:316` | ISIN | address, ZIP |
| SEC business-address fallback | `robinhood_identity_sources.py:943`, `:1092` | `compute/robinhood_identity.py:366` | symbol; broker description by CUSIP | address, ZIP |
| SEC series-ETF registrant (N-CEN) | `robinhood_fund_entity_sources.py:623` | `compute/robinhood_entity_overrides.py:381` | symbol | name, address, ZIP |
| ADR Form F-6 depositary | `robinhood_adr_entity_sources.py:285` | `compute/robinhood_entity_overrides.py:346` | CUSIP and symbol; configured F-6 URL | name, address, ZIP |
| Nature: GLEIF ELF, then SEC fund, then SEC common | records from the routes above | `compute/robinhood_entity_nature.py:236` | ISIN; symbol for SEC | nature |

`resolve_robinhood_presentation_evidence`
(`pipeline_robinhood_entities.py:91-255`) selects the candidates, runs each
loader and decision, and keeps one automatic override per ISIN. Later routes
win: legal < SEC < ADR < fund (`compute/robinhood_identity.py:608-618`).

> **Note (2026-11-02, after #793):** A series ETF (`sub_category = "ETF"`,
> not a depositary receipt) no longer follows this table. It takes its name,
> address and ZIP from the N-CEN registrant route, and its nature only from
> SEC fund evidence for the same CIK, series and class: all four cells or
> none. Its ISIN is not sent to the exact-ISIN GLEIF route, so no direct
> record applies; the registrant's own GLEIF record, fetched by LEI, still
> does. No other route fills or combines with its cells, and a configured ETF
> gets no automatic cell. See ADR-0011's 2026-11-02 amendment.

#634 (PR #755) moved the record types to `models/reference_identity.py`:
`ObservedIdentityInput` (`:38`), `IdentitySourceBundle` (`:124`),
`GleifLegalAddressRecord` (`:158`), `GleifRegistrantRecord` (`:183`) and the
lookup results (`:240`, `:250`). The decisions live in
`compute/robinhood_entity_overrides.py`, `compute/robinhood_identity.py` and
`compute/robinhood_entity_nature.py`. `tests/test_layering_imports.py`
refuses any compute import of the source modules.

### Input contract

These are all the fields the five routes and the pipeline candidate gates
read:

| Field | Read at | Robinhood value |
|---|---|---|
| `isin` | Every route. GLEIF cache key and query: `robinhood_legal_entity_sources.py:94-108`, `:125-127`. Reviewed ASCII alias: `compute/robinhood_entity_overrides.py:35-44`, `:175-184`. | Config or generated |
| `symbol` | Snapshot coverage: `pipeline_robinhood_entities.py:174`, `:176`, `:197`, `:200`. SEC address: `compute/robinhood_identity.py:274-279`. Fund selection: `robinhood_fund_entity_sources.py:657`. SEC nature: `compute/robinhood_entity_nature.py:174`, `:209`. ADR description key and fingerprint: `robinhood_adr_entity_sources.py:139`, `:322`. | Config or generated |
| `cusip` | Held-in-year set: `pipeline_robinhood_entities.py:64-88`, `:105-106`. ADR description key and fingerprint: `robinhood_adr_entity_sources.py:138`, `:322`. SEC broker description: `compute/robinhood_identity.py:378`, `:398`. | Config or generated |
| `listing_exchange` | SEC address: `compute/robinhood_identity.py:284-291`, checked against `_NASDAQ_EXCHANGE` and `_SEC_EXCHANGE_COMPATIBLE` (`:131-151`). | Config or generated |
| `sub_category` | Fund gate: `pipeline_robinhood_entities.py:134`, `robinhood_fund_entity_sources.py:644`. SEC common gate: `pipeline_robinhood_entities.py:161`, `compute/robinhood_identity.py:269`, `compute/robinhood_entity_nature.py:204`. | Config, or the Nasdaq ETF flag |
| `asset_class` | SEC common gate: `pipeline_robinhood_entities.py:162`, `compute/robinhood_identity.py:270`, `compute/robinhood_entity_nature.py:205`. | Config, or `STK` after SEC evidence |
| `issuer_country_code` | SEC common gate: `pipeline_robinhood_entities.py:163`, `compute/robinhood_identity.py:271`, `compute/robinhood_entity_nature.py:206`. Record country checks: `compute/robinhood_entity_overrides.py:170`, `:225`, `:293`; `compute/robinhood_identity.py:304`; `compute/robinhood_entity_nature.py:98`. ADR fingerprint: `robinhood_adr_entity_sources.py:140`. | Config, or `US` after SEC evidence |
| `is_depositary_receipt` | Candidates: `pipeline_robinhood_entities.py:107`, `:134`. GLEIF skip: `robinhood_legal_entity_sources.py:766-768`. ADR selection: `robinhood_adr_entity_sources.py:311`. Fund skip: `robinhood_fund_entity_sources.py:644`. SEC skip: `compute/robinhood_identity.py:268`. Nature: `compute/robinhood_entity_nature.py:203`, `:256-258`. | Config, or `false` after SEC evidence |
| `source` (F-6 URL) | `robinhood_adr_entity_sources.py:117-125`, `:142`, `:328`. | Config only |
| Broker description | `ObservedIdentityInput.description`, keyed by `(cusip, symbol)` for ADRs (`robinhood_adr_entity_sources.py:300-305`, `:322-326`, issuer match at `:190`) and by CUSIP for SEC (`compute/robinhood_identity.py:247-248`, `:293-298`). | Activity CSV reports |
| `[[fa.entities]]` `prefer_configured_*` | Candidates: `pipeline_robinhood_entities.py:139-153`, `:164-169`. Loaders: `robinhood_legal_entity_sources.py:757-764`, `robinhood_adr_entity_sources.py:313-320`, `robinhood_fund_entity_sources.py:646-652`. Nature: `compute/robinhood_entity_nature.py:253-255`. | Config |
| SEC contact | `RobinhoodConfig.sec_user_agent` (`config.py:882`), passed at `pipeline_robinhood_entities.py:127`, `:191`, `:225`. | Config |
| Snapshot fingerprint inputs | `identity_input_fingerprint(observed, manual_fields)` (`robinhood_identity_sources.py:208-244`); `manual_fields` comes from `[[robinhood.securities]]` (`pipeline_robinhood_bundle.py:566-577`). | Activity CSV reports, statements and config |
| Holding coverage | `_held_cusips` reads the calendar year, interval `from_date`/`to_date`, acquisition and movement `activity_date`, anchor `period_end`/`statement_quantity` and each `cusip` (`pipeline_robinhood_entities.py:64-88`). The candidate gate uses the held set (`:105-106`). | Reconciled activity CSV reports and statements |
| Filing period | ADR and fund `cutoff` is the CY's 31 December (`pipeline_robinhood_entities.py:124`, `:222`); the SEC snapshot's `financial_year_end` is the next 31 March (`:179`, `:204`). | Calendar year |
| Nature readiness | `depositary_isins`, `sec_common_isins` and `lapsed_gleif_isins` come from ADR records and SEC evidence, including `name_source == "broker"` (`pipeline_robinhood_entities.py:233-237`), and gate nature evidence (`compute/robinhood_entity_nature.py:256-270`). | Earlier route results |

Holding coverage, the filing period and nature readiness belong to the
caller, not to a per-instrument input.

No gate reads a raw broker token. A Robinhood security is either a reviewed
`[[robinhood.securities]]` entry (`config.py:934-946`, parsed at
`:2553-2595`) or a generated identity. `_resolve_one`
(`compute/robinhood_identity.py:452-540`) uses CUSIPs and descriptions from
activity CSV reports, symbols from those reports or monthly statements
(`:419-448`), and Nasdaq and SEC records:

- the CUSIP must have a valid check digit, and the ISIN is `US` + CUSIP +
  check digit (`:459`, `:524-533`);
- `sub_category` is the Nasdaq ETF flag (`:470`, `:535`);
- a non-ETF whose Nasdaq name carries an `ADR`, `ADS`, `DEPOSITARY`,
  `ORDINARY`, `PREFERRED`, `UNIT`, `WARRANT`, `RIGHT`, `NOTE`, `ETN` or
  `BENEFICIAL` token is refused (`:51-70`, `:506`);
- a non-ETF needs one SEC ticker row whose filer has a US state of
  incorporation and 10-K filings (`:472-477`, `:511-519`);
- the broker description must agree with the Nasdaq name, and a non-ETF's
  also with the SEC ticker name (`:478-494`). An ETF needs only one SEC
  fund-class row (`:508-511`), which carries no name.

Only then does it set `asset_class="STK"`, `issuer_country_code="US"` and
`is_depositary_receipt=False` (`:536-538`).

**#628's `InstrumentIdentity`** merged after this record's pin, in #768
(`b3dd8b0`). It carries `symbol`, `isin`, `listing_exchange` and
`issuer_country_code` (`models/instrument_identity.py` at `b3dd8b0`).
`RobinhoodSecurityConfig` keeps `cusip`, `sub_category`, `asset_class`,
`is_depositary_receipt` and `source` beside it. #768 changed the routes only
to read the identity fields through `security.instrument`; what they read is
unchanged. So `InstrumentIdentity` can supply the identity fields of a
neutral resolver input, but not the CUSIP, the route classification,
depositary evidence, an F-6 reference or the broker description.

**Robinhood-bound pieces:**

| Piece | Proposed | Why |
|---|---|---|
| `RobinhoodSecurityConfig` (`config.py:934-946`) | Left alone | It is user config. The resolvers stop taking it, and Robinhood maps it onto the neutral input. |
| `ObservedIdentityInput` (`models/reference_identity.py:38-55`) | Left alone | Its three fields are broker evidence of any source. Robinhood keys it by CUSIP and uses it for identity generation, which stays Robinhood normalisation (ADR-0008). The resolvers instead read the description from the neutral input, keyed by ISIN. |
| `identity_input_fingerprint` (`robinhood_identity_sources.py:208-244`) | Left alone | It has no broker discriminator: identical triples and manual fields give the same fingerprint for either broker (`:223-239`). That is harmless, because a snapshot holds only public registry records. Robinhood fingerprints stay byte-identical. |
| `ibkr-fa/robinhood-*` fingerprint domains: identity `robinhood_identity_sources.py:103`, post-FY request `:957`, legal `robinhood_legal_entity_sources.py:40-41`, ADR `robinhood_adr_entity_sources.py:41`, fund `robinhood_fund_entity_sources.py:43` | Left alone | Each artifact domain is hashed into its artifact path. A rename orphans every verified artifact, and a refetch can return newer registry data for a past year. The post-FY domain names an audit request, not an artifact path (`robinhood_identity_sources.py:955-962`, `:1033`). The legal, registrant and N-CEN keys hold only public identifiers and dates, so either broker can reuse an artifact. |
| `robinhood-*` cache subdirectories: identity `robinhood_identity_sources.py:100`, legal `robinhood_legal_entity_sources.py:33`, ADR `robinhood_adr_entity_sources.py:37`, fund `robinhood_fund_entity_sources.py:39` | Left alone | Existing paths stay. Both defaults put `.ibkr-fa-price-cache` beside their input (`pipeline_fa_prices.py:50`, `pipeline_robinhood_bundle.py:553`), so the roots differ only when the inputs do. Sharing is harmless for the same reason. |
| Route functions | Renamed and parameterised | The GLEIF, ADR and fund loaders, the legal, ADR, fund and SEC decisions, the nature resolver and the pipeline gates take `RobinhoodSecurityConfig`. They would take the neutral input, with neutral names. The identity-source loaders take observed triples and manual fields (`robinhood_identity_sources.py:1092-1101`) or symbol sets (`:943-950`) and stay as they are. |

> **Note (2026-11-05, #881):** The cache-subdirectories row is wrong about
> Robinhood. IBKR's `--input` is a statement file, and its default cache sits
> beside that file (`pipeline_fa_prices.py:50`). Robinhood's `--input` is a
> directory, and its default cache sits inside it
> (`pipeline_robinhood_bundle.py:555`). So the two default roots are the same
> folder only when the IBKR statement sits directly in the Robinhood input
> directory. Sharing stays harmless for the same reason. Code references in
> this note are at `7ae99a9`.

### What IBKR inputs carry

IBKR's Reporting Reference (pages last updated 20 March 2025) documents
these fields only loosely:

- Its Flex Financial Instrument Information page defines `Subcategory` as
  "Stocks only. The specific type of stock or share (e.g., Common or
  Preferred)" and `Listing Exchange` as "Stocks only. The exchange the
  security is listed on." ([Flex FII][ibkr-fii-flex]).
- The Open Positions and Trades pages define `Asset Class` only as "The asset
  class of the open position" or "of the instrument traded"
  ([Open Positions][ibkr-op], [Trades][ibkr-trades]).
- The checked pages give no list of values for `SubCategory` or
  `ListingExchange`, and do not document `IssuerCountryCode`.
- The Activity Statement page documents `Type` only as "Put or call."
  ([Activity Statement FII][ibkr-fii-as]). Yet IBKR's own samples use that
  column for `COMMON`, for `ADR` (BABA, JD, LI, SNE;
  [model statement][ibkr-sample]) and for `REIT` (AMT, DLR, EQIX, PLD, SBAC;
  [daily sample][ibkr-daily]).

| Field | Flex Open Positions (LOT) | Flex Trades and closed lots | Activity Statement | Kept on lots at `e8f7d00` |
|---|---|---|---|---|
| `isin` | `ISIN` | `ISIN` | `Security ID`, refused unless it is an ISIN | All lots |
| `symbol` | `Symbol` | `Symbol` | `Symbol` | All lots |
| Description | `Description` | `Description` | `Description` | All lots |
| `cusip` | `CUSIP` | `CUSIP` | None | None |
| `listing_exchange` | `ListingExchange` | `ListingExchange` | `Listing Exch` | Closed lots only |
| `sub_category` | `SubCategory` | `SubCategory` | `Type`, not read | Flex closed lots only |
| `asset_class` | `AssetClass` | `AssetClass` | `Asset Category`, read but not kept | None |
| `issuer_country_code` | `IssuerCountryCode` | `IssuerCountryCode` | ISIN prefix | All lots |
| Depositary flag, F-6 reference | None | None | None | None |

The Flex columns are required by the section signatures
(`parser/multi_section.py:64-87`, `:91-121`, `:129-162`). The Activity
Statement columns come from its Financial Instrument Information section
(`parser/activity_statement/trades.py:458-462`; header in, for example,
`tests/fixtures/e2e_activity_statement_multi_country_2025.csv`). IBKR says
`Security ID` holds a CUSIP for subscribers to its CUSIP service
([Activity Statement FII][ibkr-fii-as]); the parser accepts only an ISIN
(`:476-478`). The Flex parsers build lots at
`parser/open_positions.py:220-234` and `parser/closed_lots.py:293-312`. The
Activity Statement parser builds execution events at
`parser/activity_statement/trades.py:942-950`, and
`compute/lot_matching.py:284-305` and `:327-339` build their closed and open
lots.

### Binding to registry records

The Nasdaq and SEC files carry no CUSIP or ISIN
(`models/reference_identity.py:58-97`), so no registry record names the held
instrument. Generated Robinhood identities rest on a standard that ADR-0008
accepted: the broker's activity reports bind the CUSIP to a symbol, and
Nasdaq and SEC records match that symbol to a listed security and filer by
name.
Nothing compares the held identifier with a registry identifier. A
synthetic row with one company's valid CUSIP and another company's symbol
and name passes `_resolve_one` and gets the other company's filer. The
standard has two further limits:

- `_name_agrees` accepts token-set containment
  (`compute/robinhood_identity.py:233-240`). Since #1082 every broker name
  observed for the CUSIP must pass that check against Nasdaq. After noise
  words and `CO` are removed, its words must be nonempty and all occur in
  the normalized Nasdaq name, but a shortened or single-word broker name
  still agrees with a longer Nasdaq name: `EXAMPLE` agrees with
  `EXAMPLE DIFFERENT ISSUER`;
- receipt tokens are checked only in the Nasdaq security name, not in the
  broker description (`:556`).

An IBKR row binds its `ISIN`, `Symbol` and `Description` within the broker
export, which is the same kind of binding. These checks test consistency.
They do not establish security identity or depositary status.

Two other sources could bind the held identifier. This record has not
evaluated either:

- **GLEIF's exact ISIN filter** is itself an identifier lookup
  (`robinhood_legal_entity_sources.py:125-127`). It can map an ADR's ISIN to
  the underlying issuer (ADR-0009 lines 36-37). A candidate guard would
  require the ISIN prefix, the GLEIF legal-address country and the IBKR
  issuer country to agree, and the GLEIF legal name to agree strictly
  (`_strict_name_agrees`, `compute/robinhood_identity.py:232-245`) with the
  IBKR description. An ADR mapped to its foreign underlying issuer would fail
  the country check, and one mapped to its depositary would fail the name
  check. Whether any receipt passes both is unproven.
- **An SEC source keyed by CUSIP**, such as the list of Section 13(f)
  securities, might bind a US ISIN's CUSIP to an issue. Whether it states
  the class and receipt status is unverified.

### Evidence by instrument

No row is auto-eligible on current evidence. Neither format supplies a
verified binding from the held ISIN to a registry record, or verified
depositary status, and country agreement establishes neither. Each verdict
holds for both formats. Their country sources differ: Flex
`IssuerCountryCode` against the Activity Statement ISIN prefix. The last
column names the owner decision 7 option that could change the verdict.

| Instrument | What the checked evidence shows | Flex Open Positions and Closed Lots | Activity Statement | Could change with |
|---|---|---|---|---|
| US domestic common stock | Nasdaq and SEC symbol, name and domestic-filing checks exist (`compute/robinhood_identity.py:452-540`) but do not verify the held ISIN. | Manual | Manual | 7(b) for GLEIF address, ZIP and nature; 7(c) or 7(d) for the SEC routes |
| US-listed ETF | The Nasdaq ETF flag and a symbol-keyed SEC fund-class row (`:470`, `:508-511`) carry no ISIN and no name (`models/reference_identity.py:90-97`). | Manual | Manual | 7(c) or 7(d) |
| ETF whose registrant evidence conflicts, such as the ETF under #540 | Its N-CEN registrant LEI names the adviser, so the registrant check fails (`compute/robinhood_entity_overrides.py:289-292`). | Manual | Manual | #540 plus verified binding and non-receipt evidence under decision 7; no substitute route |
| Other fund | No Nasdaq ETF flag, or no unique SEC fund-class row. #351's grantor trust shows that an IBKR `ETF` label decides nothing. | Manual | Manual | None identified |
| ADR or ADS | Neither format supplies verified depositary status or an F-6 reference. IBKR's samples label such rows `ADR`. | Manual | Manual | None proposed |
| Foreign ordinary share | No binding, and no IBKR field proves the row is not a receipt (gap a). | Manual | Manual | 7(b) |
| `Preferred`, IBKR's second example | The absence of a rejection token proves nothing about its class. | Manual | Manual | 7(b) |
| Any other value, such as `REIT` in IBKR's daily sample | The checked pages publish no list of values. | Manual | Manual | 7(b) |

### Input gaps

**(a) Depositary receipts.** Robinhood's evidence is a reviewed or generated
`is_depositary_receipt` plus a configured F-6 URL. IBKR lots have neither.

- IBKR's own labels are Flex `SubCategory` and the Activity Statement `Type`
  column. As shown above, IBKR publishes no list of their values, so `ADR`
  is a value IBKR uses but does not document. The parsers do not read `Type`
  and keep `SubCategory` only on Flex closed lots.
- A label can veto a route. Its absence cannot prove that a row is not a
  receipt unless IBKR documents its vocabulary as complete.
- `INDIAN_ADR_MAPPING` (`reference/indian_adr_mapping.py:116`) lists only
  Indian-company ADRs and GDRs, and feeds only the opt-in Schedule OS and FTC
  routing (`:8-13`). It is a closed list for that routing, and an unlisted
  Indian ADR is a known coverage gap (`:49-59`). Absence from it proves
  nothing, so it has no role here.
- The issuer-country check is not a receipt guard for Flex rows. Activity
  Statement lots take the country from the ISIN prefix
  (`parser/activity_statement/trades.py:749`, `:949`), so an ADR's US prefix
  fails against an underlying issuer's foreign GLEIF record
  (`compute/robinhood_entity_overrides.py:170`). Flex lots take IBKR's
  `IssuerCountryCode` instead (`parser/open_positions.py:199-210`,
  `parser/closed_lots.py:265-275`). IBKR documents neither that field nor
  its meaning for an ADR. Its ISIN glossary says the prefix is "the country
  in which this issuer is domiciled" ([ISIN][ibkr-isin]), and its sample ADR
  ISINs start with `US`, but that describes the ISIN, not
  `IssuerCountryCode`.
- No checked source proves that an IBKR row is not a receipt. A valid check
  digit, matching names, US incorporation, 10-K filings and an ETF flag do
  not establish the held instrument's identity or depositary status.
- **Unresolved, so manual.** The GLEIF ISIN and SEC routes stay off for IBKR
  rows. Owner decision 7 lists the evidence that could change this.

**(b) Listing exchange.** The SEC address route requires the security's
exchange to equal the Nasdaq directory's exchange after `_NASDAQ_EXCHANGE`,
and to be compatible with the SEC exchange
(`compute/robinhood_identity.py:131-151`, `:284-291`). Its vocabulary is
`NASDAQ`, `NYSE`, `NYSE AMERICAN`, `ARCA`, `CBOE` and `IEX`.

- IBKR supplies Flex `ListingExchange` and the Activity Statement
  `Listing Exch`. Closed lots keep it (`parser/closed_lots.py:299`;
  `compute/lot_matching.py:296`). Open lots have dropped it since #733
  (`models/open_lot.py:91-101`).
- IBKR ties no list of values to the field. Its exchange catalog
  ([products and exchanges][ibkr-exchanges], fetched 2026-11-01) has `AMEX`
  "American Stock Exchange", `BATS` "BATS Trading Inc", `ARCA` "Archipelago"
  and `CBOE` "Chicago Board Options Exchange". The vocabulary's `CBOE` is
  Nasdaq's code `Z` (`compute/robinhood_identity.py:137`). So equal strings
  do not prove the same venue, and different strings do not prove different
  venues.
- Generated Robinhood identities take the exchange from the Nasdaq directory,
  not from the broker (`compute/robinhood_identity.py:495`, `:534`). The
  equality check at `:287` then compares that value with itself, so no
  broker-venue check runs.
- **Unresolved, so manual.** Taking Nasdaq's exchange maps no IBKR token
  and verifies nothing about the held security. The SEC symbol routes need a
  verified IBKR exchange mapping or an identifier binding (owner
  decision 7).

**(c) Symbol spelling.** The symbol-keyed lookups match exactly, after
whitespace collapse and upper-casing (`robinhood_identity_sources.py:192-193`;
Nasdaq `:410`, SEC tickers `:489`, `:498`, SEC funds `:522`, `:525`).

- A spelling difference finds no row, which fails closed to manual.
  IBKR documents no share-class symbol convention, and neither its samples
  nor this repo's fixtures contain one.
- The real risk is a symbol that matches a different security. The fund
  route selects by symbol alone (`robinhood_fund_entity_sources.py:657`), and
  so does SEC fund nature (`compute/robinhood_entity_nature.py:174`).
  A reviewed Robinhood config supplies its own binding. Generated
  identities rely on `_resolve_one`'s name checks
  (`compute/robinhood_identity.py:478-494`), which compare names, not
  identifiers (option 7(d)).
- `[[prices.ticker_overrides]]` (`TickerOverride`, `config.py:456-496`) is
  ISIN-bound and changes only the symbol used to fetch prices (`:467-468`).
  Reusing it would let a Yahoo spelling select an SEC registrant, so it is
  not reused.
- **Unresolved, so manual.** A spelling gap, or a symbol bound to the ISIN
  only by the broker row, keeps the symbol-keyed routes off. Price overrides
  and name checks do not bind a symbol to an issue.

**(d) CUSIP for Activity Statement lots.** Four places read a CUSIP: the
Robinhood held-in-year set, the ADR route (off for IBKR), the SEC route's
broker-description lookup (`compute/robinhood_identity.py:378`, `:398`) and
the identity-snapshot fingerprint (`robinhood_identity_sources.py:208-244`).

- A description carried per ISIN needs no CUSIP to look up. An
  identity-snapshot triple can carry the Flex `CUSIP` or an empty string
  (`robinhood_identity_sources.py:223-224`).
- Flex supplies a `CUSIP` column, but no lot keeps it since #733. The
  Activity Statement has no CUSIP column; its `Security ID` is read as the
  ISIN (`parser/activity_statement/trades.py:460`, `:476-478`).
- `_resolve_one` checks a CUSIP (`compute/robinhood_identity.py:459`) and
  builds the ISIN from it (`:524-533`), so an IBKR classifier cannot call it
  unchanged. A US ISIN embeds the CUSIP ([ISIN][ibkr-isin];
  `config.py:2546-2551`). A CUSIP-keyed source under option 7(c) would need
  that embedded CUSIP.
- **Description lookup resolved by rekeying; binding unresolved.** Carrying
  the description per ISIN removes that lookup's CUSIP dependency, but
  establishes no registry identity. The key and guards for any CUSIP-keyed
  source belong to step 2.

### Configured and automatic values

- **Today.** A source-specific `nature_by_isin` beats a configured
  `[[fa.entities]]` nature (`compute/fa_a3.py:186-194`, documented at
  `:258-260`). IBKR passes no `nature_by_isin`, so its configured values
  always win.
- **Robinhood.** Automatic values win unless the entry sets
  `prefer_configured_address`, `prefer_configured_nature` or
  `prefer_configured_name` (`compute/robinhood_identity.py:567-605`,
  `compute/robinhood_entity_nature.py:285-297`). These flags sit on the
  shared `FaEntityOverride` but are documented as Robinhood-only
  (`config.py:519-562`; ADR-0010 lines 50-53).

  > **Note (2026-11-02, after #793):** A series ETF is the exception. One
  > with an `[[fa.entities]]` entry gets no automatic cell, whatever its
  > `prefer_configured_*` flags say. Its configured cells stand, and a cell
  > the entry leaves out, including the name, stays manual. See ADR-0011's
  > 2026-11-02 amendment.
- **One table.** IBKR (`pipeline_fa.py:317`) and Robinhood
  (`pipeline_robinhood_bundle.py:616`) read the same `[[fa.entities]]`
  entries, so an ISIN held at both brokers has one entry.
- **Risk.** If IBKR adopted Robinhood's merge unchanged, the next run would
  replace every reviewed IBKR address and nature that has a resolvable
  automatic value, because every flag defaults to `false`.
- **Mixed rows.** Robinhood's merge fills cells one at a time, so a row can
  mix a configured name with an automatic address and nature.
  `FaEntityOverride` has no legal-person identifier (`config.py:554-561`),
  and the row builder picks each cell on its own
  (`compute/fa_a3.py:179-194`), so nothing checks that the cells describe
  one entity.

  > **Note (2026-11-02, after #793):** A series ETF's cells never mix.
  > Without an entry, it takes all four from its N-CEN registrant and
  > matching SEC fund evidence, or none; with one, only its configured cells
  > fill. See ADR-0011's 2026-11-02 amendment.
- **Proposed, if a route is ever enabled for IBKR.** An ISIN with any
  `[[fa.entities]]` entry gets no automatic cell and keeps today's output,
  including cells the entry leaves blank. An unconfigured row gets automatic
  cells only when every cell, including a retained IBKR name, is shown to
  identify the same legal person for the reporting period. A GLEIF country
  match and a non-empty legal name are not enough
  (`compute/robinhood_entity_overrides.py:158-209`). Missing or conflicting
  evidence keeps the row manual. The `prefer_configured_*` flags stay
  Robinhood-only. Rows that change from the country-name fallback or a blank
  are listed with their source, as the Robinhood audits do. Owner decision 4
  settles this.

### Operational effects

- **Network disclosure.** Today an IBKR FA run fetches only per-ticker daily
  closes (`pipeline_fa.py:300-309`; `pipeline_fa_prices.py:21`).
  Classification and the routes would also contact Nasdaq, GLEIF and SEC:
  - Nasdaq and SEC receive whole-directory requests that carry no held
    identifier (`robinhood_identity_sources.py:92-96`, `:1055-1063`).
  - GLEIF receives each held ISIN on a cold lookup
    (`robinhood_legal_entity_sources.py:125-127`) and each fund registrant
    LEI (`:130-131`). The F-6 route stays off for IBKR, so no depositary LEI
    is sent.
  - SEC receives per-CIK submission requests
    (`robinhood_identity_sources.py:1071-1080`;
    `robinhood_fund_entity_sources.py:229-260`) and filing-archive requests
    (`robinhood_fund_entity_sources.py:76-79`, `:374`), which show which
    issuers and funds are held. Classification alone requests the filer of
    every held symbol that has an SEC ticker row, ADRs included.
- **SEC `User-Agent`.** SEC requests need a declared contact
  (`robinhood_identity_sources.py:306-321`). It comes from
  `RobinhoodConfig.sec_user_agent` (`config.py:882`). A cold identity-source
  fetch without it returns an audit failure before it sends any request
  (`robinhood_identity_sources.py:1139`); a warm cache needs none. An IBKR-only
  user has no `[robinhood]` table, so IBKR needs its own config home for the
  contact.
- **Caching.** Every artifact is immutable and hashed, and its integrity is
  checked before it supplies route evidence.
  Identity snapshots are keyed by the normalized CUSIP, symbol and
  description triples, the manual fields, the domain and the schema
  (`robinhood_identity_sources.py:208-244`). GLEIF artifacts are keyed by
  ISIN or LEI (`robinhood_legal_entity_sources.py:94-122`), N-CEN selections
  by CIK, series, class, ticker and cutoff
  (`robinhood_fund_entity_sources.py:109-120`), and F-6 selections by ISIN,
  CUSIP, symbol, issuer country, description, source and cutoff
  (`robinhood_adr_entity_sources.py:128-145`). Robinhood ties the
  lookups to the price-fetch switch (`pipeline_robinhood.py:85`, `:182`,
  `:270`, `:450`), so `--no-fetch-prices` is cache-only
  (`docs/portal/schedule-fa-a3.md:162-163`). IBKR's equivalent is
  `fetch_prices` (`pipeline_fa.py:79`, `:304`). The SEC fallback also needs a
  snapshot fetched after the FY's 31 March, and a pre-cutoff snapshot is never
  refreshed in place (ADR-0009 lines 49-50, 88-94;
  `robinhood_identity_sources.py:943-1038`).

### Inherited open questions and conflicts

- #539 (issuer identity across a holding-company reorganisation) and #540 (an
  N-CEN registrant LEI that names the adviser) apply unchanged. IBKR inherits
  whatever they settle.
- Any IBKR-specific divergence from Robinhood, such as ADR rows staying
  manual, goes in the #438 parity and divergence ledger.
- **Doc conflict.** These texts say or imply that SEC business addresses
  are never used: `docs/portal/schedule-fa-a3.md:150-153`,
  `README.md:409-410`, ADR-0005 line 321, and ADR-0009 lines 19-20
  ("Business/headquarters addresses are not substitutes"), 26 and 102-104.
  ADR-0009's 2026-08-27 amendment (lines 8, 45-67) and
  `compute/robinhood_identity.py:261-363` do use the SEC principal business
  address as a US common-stock fallback. This record does not fix the docs;
  #777 does.
- **Where IBKR would be stricter than Robinhood.** One Robinhood behaviour
  is broader than this record proposes for IBKR. It is not changed here.
  - Since #809, the SEC fallback runs only after one of two GLEIF outcomes
    for the ISIN: `zero-match`, or `lapsed`, a type-verified `LAPSED` record
    that passes every other GLEIF legal-address check. Every other outcome
    blocks it, including `country-mismatch`. At `6c65222`, see
    `SEC_FALLBACK_GLEIF_OUTCOMES` (`compute/robinhood_entity_overrides.py:38`)
    and the gate in `resolve_robinhood_sec_entity_overrides`
    (`compute/robinhood_identity.py:402-411`). ADR-0009's 2026-11-02
    amendment (#792) gives the full conditions. Decision item 3 is still
    stricter: it allows the SEC route only after a verified `zero-match`.
  - Series ETFs are no longer broader. Since #793, a Robinhood series ETF
    takes all four cells from its N-CEN registrant and matching SEC fund
    evidence, or none, and gets no direct ISIN record. That matches Decision
    item 3. At `c85d18b`, see the direct-route filter
    (`pipeline_robinhood_entities.py:110-116`) and the registrant candidates
    (`:140-146`). ADR-0011's 2026-11-02 amendment gives the full conditions.
- **ADR country attribution.** `docs/portal/adr-handling.md:15-22` sets the
  IBKR ADR A3 country from the ISIN prefix. Activity Statement lots do that;
  Flex lots use `IssuerCountryCode` (gap a), which IBKR does not document.
  Whether the two agree for a Flex ADR is unverified. This record changes no
  country attribution.

## Decision

No IBKR route. Every IBKR A3 entity row stays configured (`[[fa.entities]]`)
or manual (owner decision 7 = (a), 2026-11-02). Item 1 is that decision.
Items 2 and 4 are done, and item 3 is the shape a route would take.

1. **No IBKR route on current evidence.** IBKR A3 identity stays as it is:
   `[[fa.entities]]` or manual. No checked source binds a held IBKR ISIN to
   the record a route uses, or shows that the row is not a receipt (gaps a
   to c).
2. **Evaluate binding evidence.** Evaluate options 7(b) and 7(c) under
   Notes with public examples only. For each, show whether it binds the held
   identifier and excludes receipts, including ADRs, GDRs and depositary
   shares, and list what it leaves manual. Done (#776): the 2026-11-02
   amendment under Notes records the result, and neither option supports
   moving from (a).
3. **Only after new binding evidence and a new owner decision accept a
   route,** adopt it in this shape:
   - one neutral per-ISIN input for the routes. It carries the identity
     fields, the broker description, a depositary status (a receipt with its
     F-6 reference, not a receipt, or unknown), an instrument class and their
     provenance. An unknown status turns off every route. Robinhood output,
     cache paths and fingerprints do not change. #628's `InstrumentIdentity`
     can supply the identity fields;
   - no automatic fallback. A lapsed, refused, conflicting or unavailable
     GLEIF result keeps the row manual. The SEC address route needs its own
     verified binding and owner decision 2, and may run only after a
     verified `zero-match` GLEIF result for that ISIN
     (`robinhood_legal_entity_sources.py:421`). A cache miss, invalid cache,
     failed request or `multi-match` result never allows it;
   - an ETF takes all its cells only from its N-CEN registrant and that
     registrant's SEC fund evidence. No direct ISIN record substitutes;
   - the F-6 route stays off;
   - the precedence and one-legal-person rule under Context;
   - lookups follow `fetch_prices` and use IBKR's cache root. Owner decisions
     3 and 6 settle the default and the SEC contact.

   Issues for this step are filed only after that decision.
4. **Doc conflict.** Fix the texts listed under Context separately. Done
   (#777).

Follow-up issues: #776 (step 2) and #777 (step 4). A follow-up that changes
IBKR output also updates the IBKR rows of
`docs/portal/schedule-fa-manual-fill.md` (lines 32-33).

## Consequences

- No IBKR output changes now. Rows keep `[[fa.entities]]` values or manual
  cells, and the CLI keeps naming each manual holding.
- Step 2 evaluates evidence and changes no output. Any route is enabled
  only in step 3, after the owner decisions, and only with the evaluated
  binding evidence for that route.
- If routes are later enabled, ADRs stay manual, unlike Robinhood's
  configured F-6 route. Any such difference goes in the #438 ledger.
- No raw broker type token becomes a filed entity value or selects a route.
- Nasdaq, GLEIF and SEC would become network recipients for IBKR runs only
  after step 3.
- Robinhood output, cache artifacts and fingerprints do not change.

## Notes

### Owner decisions

Decision 7 is decided: (a), on 2026-11-02, in the owner's words "none yet:
every IBKR A3 entity row stays configured or manual". Decisions 1 to 6 only
matter once a binding route exists, so they are parked until new binding
evidence exists, for example a FIRDS evaluation, which the 2026-11-02
amendment below lists as not evaluated. All seven keep their original text
below. Decisions 1 and 2 are tax-position questions. The AY 2026-27 ITR-2
schema and Notification 46/2026 name the four A3 identity cells. The ITD
FA/FSI guide and the AY 2023-24 ITR-2 instructions describe the A3 amounts.
None of these texts, all linked under Context, defines the four identity
cells. So each tax-position decision needs a citation that does, or must
record that none was found. Decisions 3 to 7 are product, privacy and
evidence-standard choices.

1. **Automatic IBKR identity at all.** Should IBKR A3 rows get automatic
   name, address, ZIP or nature? #351 kept IBKR nature config-only; its
   acceptance criterion says generic IBKR rows never receive an inferred or
   static nature. Address has always come from `[[fa.entities]]` or the
   country-name fallback.
2. **SEC business address.** Is an SEC principal business address acceptable
   for `Address of entity` on IBKR rows? ADR-0009 lines 12-17 note that the
   schedule does not define legal versus headquarters address, and its
   amendment already uses the SEC address as a Robinhood fallback.
3. **Opt-in or default.** Should IBKR lookups be opt-in or on by default,
   given that GLEIF would receive held ISINs and SEC would receive per-CIK
   requests?
4. **Precedence.** For IBKR users, should automatic values or existing
   `[[fa.entities]]` values win? This record proposes that an ISIN with an
   entry gets no automatic cell.
5. **`SubCategory` routing.** May IBKR `SubCategory` select a resolver
   route, given #351's non-goals (no broker-token-to-legal-entity mapping,
   and no extra raw broker type metadata without an audit-output need)?
   GLEIF and SEC records are external registry records, not broker tokens.
   This record proposes that `SubCategory` and `Type` select no route and may
   at most veto one.
6. **SEC contact.** Where does an IBKR run read its SEC `User-Agent`
   contact? Today it lives only in `[robinhood]`.
7. **Binding evidence.** Which evidence, if any, may bind an IBKR row's ISIN
   to the record a route uses and show that the row is not a receipt? See
   "Binding to registry records".
   - (a) None yet: every IBKR row stays manual until (b) or (c) is
     evaluated. This record recommends (a).
   - (b) GLEIF's exact ISIN filter with the country and strict-name guards,
     for the GLEIF route only.
   - (c) An SEC source keyed by CUSIP.
   - (d) The ADR-0008 standard of generated Robinhood identities. A synthetic
     row with one company's identifier and another's symbol and name passes
     it.

### Decision 7 evidence (amended 2026-11-02, #776)

This amendment carries out Decision item 2. It tests options 7(b) and 7(c)
on public records and decides nothing. Decisions 1 to 6 are unchanged. The
lookups ran on 2026-11-02. Code references in this amendment are at
`6c609f1`.

**Result.** Neither option shows that a row is not a depositary receipt. So
neither lets a route fill an IBKR cell on its own, and every IBKR row stays
manual, as under option (a).

#### Examples

Each option was tried on these instruments except example 1, which is made
up throughout: it stands for a plain US common stock, and its identifiers
and records only take the shape a real one's do. A row marked public in the
last column comes from IBKR's [model statement][ibkr-sample] for November
2020, which is in Activity Statement format. Every other IBKR input is
synthetic, and no example claims verified IBKR coverage. The FIRDS records
give each ISIN's ISO 10962 CFI code, which says what the ISIN names: `ES` a
share, `ED` a depositary receipt, `CE` an exchange-traded fund. FIRDS serves
here as a reference, not as an evaluated option ([ESMA FIRDS][esma-firds],
publication of 2026-11-01; [FCA FIRDS][fca-firds], file of 2026-10-31).

| # | Instrument | Public records and identifiers | IBKR row |
|---|---|---|---|
| 1 | US common stock: SAMPLE ISSUER AA INC (made up) | ISIN `US99990A3095`, CUSIP `99990A309`. A FIRDS record would give CFI `ESVUFR` and the issuer's LEI. | Synthetic: `XQAA`, `SAMPLE ISSUER AA INC`, `US99990A3095`, `NASDAQ`, `COMMON` |
| 2 | Sponsored ADR: Alibaba Group Holding ADS | ISIN `US01609W1027`, CUSIP `01609W102`. [Form F-6EF][baba-f6], filed 2022-02-04: issuer Alibaba Group Holding Limited (Cayman Islands), depositary Citibank, N.A., each ADS eight ordinary shares; the form gives no CUSIP. ESMA FIRDS: CFI `EDSXFR`, issuer LEI `5493001NTNQJDH60PM02`, underlying `KYG017191142`. | Public: `BABA`, `ALIBABA GROUP HOLDING-SP ADR`, `US01609W1027`, `NYSE`, `ADR` |
| 3 | GDR with a non-US prefix: The Federal Bank Limited Reg S GDR | ISIN `XS0229331755`. FCA FIRDS: CFI `EDXXXM`, short name `THE FEDERAL BAN/DR EQ REGS`, issuer LEI `335800J8VWGE2HGWF417` (The Federal Bank Limited, `IN`), underlying `INE171A01029`. ESMA FIRDS: CFI `EDXXXR`, short name `DEUTSCHE BANK T/DR`. | Synthetic |
| 4 | US preferred depositary share: JPMorgan Chase & Co. depositary shares, each a 1/400th interest in a share of 4.20% Non-Cumulative Preferred Stock, Series MM | ISIN `US48128B5232`, CUSIP `48128B523`. JPMorgan's [outstanding preferred stock table][jpm-ir] lists the ISIN as Series MM (`JPM PR M`), issued 2021-07-29. Its [prospectus supplement][jpm-mm] names Computershare Inc. as depositary. No ESMA FIRDS record. | Synthetic |
| 5 | Foreign ordinary share listed in the US: Logitech International S.A. registered share | ISIN `CH0025751329`. ESMA FIRDS: CFI `ESVUFR`, issuer LEI `549300SN6PRK6IJJC215`. | Public: `LOGI`, `LOGITECH INTERNATIONAL-REG`, `CH0025751329`, `NASDAQ`, `COMMON` |
| 6 | ETF series: VanEck Gold Miners ETF, a series of VanEck ETF Trust | ISIN `US92189F1066`, CUSIP `92189F106`. ESMA FIRDS: CFI `CEOJES`, issuer LEI `54930031TORTKAM9TJ98`. | Synthetic |

#### Option (b): GLEIF's exact ISIN filter

**Source.**

- GLEIF publishes the records. The ISIN-to-LEI pairs come from a mapping
  process that ANNA, the ISIN registration authority, established and GLEIF
  certified. The route sends
  `https://api.gleif.org/api/v1/lei-records?filter[isin]=<ISIN>&page[size]=2`
  (`robinhood_legal_entity_sources.py:125-127`). The API returns the
  ISIN-LEI mappings "provided according to the mapping process established by
  Association of National Numbering Agencies (ANNA)" ([GLEIF API][gleif-api]).
- GLEIF's factsheet ([ISIN-to-LEI factsheet][gleif-isin-factsheet], v1.0,
  2021-08-06) says:
  - each pair has two fields, `ISIN` and `LEI`, "The LEI of the issuer
    entity";
  - the mapping treats the ISIN issuer's name, address and legal form as the
    equivalents of the LEI record's legal name, legal address and legal form;
  - the table "only includes active ISINs where a LEI mapping is available
    from those NNAs who have joined the initiative";
  - users may challenge a pair.
- ANNA lists the participating agencies, including CUSIP Global Services for
  `US` ([ANNA][anna-identifiers]). Participation does not mean coverage:
  several ISINs below have no pair.

**Key and evidence chain.** The key is the held ISIN, unchanged. The chain
runs from the ISIN to ANNA's ISIN-LEI pair, then to the LEI's GLEIF record:
`entity.legalName`, `entity.legalAddress`, `entity.category`,
`entity.legalForm` and the status fields that `_legal_override` checks
(`compute/robinhood_entity_overrides.py:158-209`).

- The pair links two identifiers. The factsheet lists issuer names,
  addresses and legal forms as the fields considered for mapping. The pair
  does not say which fields matched.
- **Missing link: instrument type.** The pair does not say whether the ISIN
  is a share, a receipt or a fund unit.
- **Conflicting link: receipts.** ANNA's ISIN structure gives a depositary
  receipt the country code "of the organisation that issued the receipt,
  rather than instead of the one that issued the underlying security"
  ([ANNA][anna-identifiers]). The pair can name the underlying issuer
  instead. Example 2's ISIN maps to Alibaba Group Holding Limited (legal
  address `KY`), whose LEI also carries the ordinary share `KYG017191142`.

**Guards.**

- **Country.** The ISIN prefix, the GLEIF `legalAddress.country` and the IBKR
  issuer country must be equal.
  - Activity Statement lots take the issuer country from the ISIN prefix
    (`parser/activity_statement/trades.py:749`, `:949`). For them the guard
    is one comparison: prefix against GLEIF.
  - Flex lots take `IssuerCountryCode` (`parser/open_positions.py:199-211`,
    `parser/closed_lots.py:264-276`), a third input that IBKR does not
    document (gap a). No public IBKR Flex sample was found, so every Flex
    value below is synthetic.
  - Both parsers refuse `IN`, and any code missing from the ITR country
    table, before a route runs (`parser/_scope_guards.py:38-95`).
- **Name.** `_strict_name_agrees` (`compute/robinhood_identity.py:233-245`)
  compares the GLEIF legal name with the IBKR description. It drops `CO` and
  the noise tokens (`:36-51`, such as `INC`, `CORP` and `CLASS`). It accepts
  equal token sets, or a strict subset whose extra tokens are all US state
  codes.
- `_legal_override`'s other checks still apply: lifecycle, completeness and
  portal shape. They compare the GLEIF country with the issuer country only,
  not with the ISIN prefix.

**Results.** "Unproven" marks a Flex value that IBKR does not publish. The
other `_legal_override` checks were run on the saved GLEIF records; example
1's made-up record is shaped to pass them.

| # | GLEIF exact-ISIN result | Prefix and GLEIF country | Activity Statement country (the prefix) | Flex `IssuerCountryCode` | Strict name | Other `_legal_override` checks | Result |
|---|---|---|---|---|---|---|---|
| 1 | Made up: Sample Issuer AA Inc., `US`, `GENERAL` | `US` = `US`: pass | `US`: pass | Unproven: passes only if `US` | `SAMPLE ISSUER AA INC` against `Sample Issuer AA Inc.`: pass | Pass | Guards pass. Receipt status unknown: manual |
| 2 | Alibaba Group Holding Limited, `KY`, `GENERAL` | `US` against `KY`: fail | `US`: fail | Fails for any value, since prefix and GLEIF differ | Fail: `SP` and `ADR` against `LIMITED` | Pass with a Flex `KY` | Manual |
| 3 | No record | Not applicable | Not applicable: parse refuses `XS`, which the ITR table lacks | Not applicable: parse refuses `IN`, and any other value finds no record | Not applicable | Not applicable | Refused or manual |
| 4 | JPMORGAN CHASE & CO., `US`, `GENERAL`, LEI `8I5DZWZKVSZI1NUHU748`, which the common stock `US46625H1005` also maps to | `US` = `US`: pass | `US`: pass | Unproven: passes only if `US` | Synthetic `JPMORGAN CHASE & CO`: pass. Synthetic `JPMORGAN CHASE & CO 4.2% PFD MM`: fail | Pass | A receipt can pass both guards. Manual |
| 5 | LOGITECH INTERNATIONAL S.A., `CH`, `GENERAL` | `CH` = `CH`: pass | `CH`: pass | Unproven: passes only if `CH` | Fail: `REG` against `S` | Pass | Manual |
| 6 | VanEck Gold Miners ETF, `US`, `FUND`, legal form `Sub-Fund`, `registeredAs` `S000009191`, umbrella fund VANECK ETF TRUST | `US` = `US`: pass | `US`: pass | Unproven: passes only if `US` | Synthetic `VANECK GOLD MINERS ETF`: pass | Pass | Guards pass on a series record. Decision item 3 bars it: manual |

**Receipt exclusion: not established.** Option (b) does not establish
receipt exclusion. It cannot support automatic routing for ADRs, GDRs or
depositary shares.

- The pair carries no instrument type, and one LEI can carry both a share and
  its receipt (example 2).
- The country guard compares countries, not instrument types. A US
  preferred depositary share has a `US` prefix and maps to its US issuer, so
  the guard passes it on Activity Statement lots and with a Flex `US`
  (example 4).
- The prefix comparison is what stops example 2 on Flex lots. With a Flex
  `KY`, `_legal_override` alone resolves the Cayman address for the ADR.
- The name guard compares names, not instrument types. A plain issuer
  description passes for example 4. IBKR's samples add `-SP ADR` or `-ADR` to
  ADR rows, but IBKR documents no description format. Nothing shows that
  every receipt carries such a token.
- Receipt status stays unknown on this evidence, and an unknown status keeps
  the row manual (Decision item 3).

**Funds.** A mapped ETF ISIN can name the series, not the registrant.
Example 6 passes both guards against a series record. Decision item 3 takes
every ETF cell from the N-CEN registrant only, and the stated guards do not
read `entity.category`.

**What it would fill.** Nothing on its own, because receipt status stays
unknown, so all six examples stay manual. Suppose another identifier-bound
source supplied receipt status and instrument class. Of the examples, only
the US common stock would then pass every check. Example 4 would be a known
receipt, and example 6 a fund that Decision item 3 sends to its N-CEN
registrant. The others have no record or fail a guard.

**Network disclosure.** Nothing new. GLEIF receives each held ISIN on a cold
lookup, as Operational effects already says.

#### Option (c): an SEC source keyed by CUSIP

This tests the Official List of Section 13(f) Securities, the example the
option names.

**Source.**

- The SEC's Division of Investment Management publishes the list quarterly
  ([list page][sec-13f-list]). Rule 13f-1(c) lets a manager "rely on the most
  recent list" ([17 CFR 240.13f-1][cfr-13f-1]). This amendment used the 2026
  Q3 list, "current as of September 30, 2026" ([PDF][sec-13f-2026q3]). Since
  4Q 2025 the list also comes as a text file ([FAQ][sec-13f-faq], question
  10).
- The PDF's cover says CUSIP Global Services (CGS) permits the use of the
  CUSIPs and descriptions and forbids redistribution without its permission.
  It also says "CGS does not guarantee the accuracy or completeness of the
  CUSIP Numbers and standard descriptions included herein". Neither the list
  page nor the FAQ says how the list is compiled.
- A fetch from `www.sec.gov` must declare a `User-Agent` with a contact, in
  the form "Sample Company Name AdminContact@<sample company domain>.com",
  at no more than 10 requests a second ([SEC fair access][sec-access]).
  A route would download the quarterly text file that way. This evaluation
  read the SEC pages and the list through a separate web fetcher, because
  `www.sec.gov` rate-limited its address, and checked the saved PDF as
  extracted text.
- Each row has `CUSIP NO`, `ISSUER NAME`, `ISSUER DESCRIPTION` and `STATUS`.
  An asterisk marks a security with listed options. A row has no CIK, LEI,
  ISIN, ticker or exchange.
- The list "primarily includes U.S. exchange-traded stocks", and shares of
  mutual funds are not on it ([FAQ][sec-13f-faq], question 7).

**Key.** A valid US ISIN's characters 3 to 11 are its CUSIP.

- An ISIN is a two-letter country code, a nine-character national number and
  a check digit ([ANNA][anna-identifiers]). IBKR's ISIN glossary says the US
  national number "is a CUSIP" ([ISIN][ibkr-isin]). The code already requires
  a Robinhood US ISIN to contain its CUSIP (`config.py:2504-2509`).
- The key needs a `US` prefix and valid ISIN and CUSIP check digits
  (`types.py:64-94`).
- No cited source says that a non-US national number is a CUSIP, so the key
  is unsupported for non-US ISINs.
  - Example 5's `CH0025751329` holds `002575132`, its Swiss national number.
    That string passes the CUSIP check digit, yet no row on the list has it,
    and the list carries Logitech as `H50430232`. No public link from that
    ISIN to `H50430232` was found.
  - Example 3's `XS0229331755` holds `022933175`, which fails the CUSIP check
    digit.
  - CSIQ's `CA1366351098` holds `136635109`, and the list has `136635109`
    `CANADIAN SOLAR INC` `COM`. Without a source for that link, the match is
    not evidence.

**Evidence chain.** The held ISIN gives the CUSIP, and the CUSIP selects a
row with an issuer name and a description. The chain stops there.

- **Missing link to the filer.** The row has no CIK. The SEC route reads
  `company_tickers_exchange.json` and per-CIK submissions, whose records
  carry no CUSIP (`models/reference_identity.py:77-120`). Only a name or
  symbol match could join them, which is option 7(d)'s standard.
- **Missing link to the fund series, class and registrant.** The list names
  the trust as the issuer and puts an abbreviated series name in the
  description (example 6). `company_tickers_mf.json` has CIK, series, class
  and symbol, but no CUSIP (`models/reference_identity.py:90-97`). The N-CEN
  fields the fund route reads carry no CUSIP or ISIN either
  (`robinhood_fund_entity_sources.py:275-315`).

**Results.**

| # | Key | 13(f) row | Filer, series or class link | Result |
|---|---|---|---|---|
| 1 | `99990A309`, valid | Made up: `99990A309` `SAMPLE ISSUER AA INC` `COM` | None by identifier | Manual |
| 2 | `01609W102`, valid | `01609W102` `ALIBABA GROUP HLDG LTD` `SPONSORED ADS` | None. The token vetoes | Manual |
| 3 | Unsupported: `XS` prefix | None | Not applicable | Manual |
| 4 | `48128B523`, valid | None: no row starts `48128B` | Not applicable | Manual |
| 5 | Unsupported: `CH` prefix | `H50430232` `LOGITECH INTL S A` `SHS`, reachable only by name | None by identifier | Manual |
| 6 | `92189F106`, valid | `92189F106` `VANECK ETF TRUST` `GOLD MINERS ETF` | None. The row names the trust, not a series or class | Manual |

**Receipt exclusion: not established.** Option (c) does not establish
receipt exclusion. It cannot support automatic routing for ADRs, GDRs or
depositary shares.

- The description is CGS's abbreviated standard description. The list
  publishes no vocabulary for it, and CGS does not guarantee it.
- Receipts appear under many spellings, such as `SPONSORED ADS`,
  `SPONSORED ADR`, `SPON ADS`, `ADS`, `ADR`, `SPNSRD ADS NEW` and
  `7.25% DEP SHS A`. The ETF `00768Y206` (ESMA FIRDS CFI `CEOXXX`) has the
  description `DORSEY WRIGT ADR`.
- A token can veto a route. Its absence cannot show that a row is not a
  receipt (gap a).
- A missing row proves nothing either: neither the GDR nor the preferred
  depositary share is on the list.

**What it would fill.** Nothing, so all six examples stay manual. No SEC
route record is reached by an identifier, because nothing links the row to a
CIK, series, class or registrant.

**Network disclosure.** The list is one whole-file download a quarter and
sends no held identifier. It needs the SEC contact (decision 6). Any filer
lookup by CIK would add the per-CIK requests listed under Operational
effects. The CGS notice governs any cached or bundled copy.

#### Other public rows checked

- The model statement's other foreign `COMMON` rows have no GLEIF record:
  NXPI (`NL0009538784`), CHKP (`IL0010824113`), CYBR (`IL0011334468`), FLEX
  (`SG9999000020`) and CSIQ (`CA1366351098`).
- DOX (`GB0022569080`) maps to Amdocs Limited with a `GG` legal address, so
  the country guard refuses the right issuer. ANNA lists the London Stock
  Exchange as the agency for `GB`, also covering `GG`, `IM` and `JE`.
- The ADRs JD (`US47215P1066`) and LI (`US50202M1027`) map to Cayman issuers,
  like BABA. The GDRs `US7594701077` and `US8565522039` map to Reliance
  Industries Limited and State Bank of India, both `IN`. Each fails the
  country guard.
- Of the US-listed ETFs checked, some have no GLEIF record and others map to
  their trusts, category `FUND`.

#### Recommendation

This amendment decides nothing. For decision 7:

- **(b) does not support moving from (a).** GLEIF binds a mapped ISIN to an
  issuer LEI by identifier, which name matching (option 7(d)) does not. But
  the pair has no instrument type. The guards pass a US preferred depositary
  share with a plain description, and an ETF series record. Many held ISINs
  have no pair, and the country guard refuses right issuers such as Amdocs.
  The filter could be one link in a chain only if another identifier-bound
  source settled receipt status and instrument class.
- **(c) does not support moving from (a).** The list binds a valid US CUSIP
  to an issuer name and an abbreviated description, and to nothing else. It
  reaches no CIK, series, class or registrant except by name, which is option
  7(d)'s standard. Its descriptions can veto a receipt but cannot clear one,
  and no non-US ISIN has a supported key.

**Not evaluated.** Each FIRDS record cited above carries a CFI code, an
issuer LEI and, for a receipt, the underlying ISIN. Those fields bear on
receipt status and instrument class, which neither option supplies. Example 4
has no ESMA FIRDS record. Evaluating FIRDS would be a new option under
decision 7, outside this amendment.

[itr2-schema]: ../../src/ibkr_fa/reference/cbdt_itr2_schema_ay2026_27.json
[itr2-ay2026]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-04/Notification%20No.46_2026.pdf
[fa-guide]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-03/Step%20by%20Step%20Guide%20FA%20FSI.pdf
[itr2-instructions]: https://www.incometaxindia.gov.in/documents/20117/11038805/Instructions_ITR2_AY_2023_24.pdf/fe470b08-ceb7-340f-d2e3-d8fd1bf877b7
[ibkr-fii-flex]: https://www.ibkrguides.com/reportingreference/reportguide/financialinstrumentinformationfq.htm
[ibkr-fii-as]: https://www.ibkrguides.com/reportingreference/reportguide/financialinstrumentinformation_default.htm
[ibkr-op]: https://www.ibkrguides.com/reportingreference/reportguide/open%20positionsfq.htm
[ibkr-trades]: https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm
[ibkr-sample]: https://www.ibkrguides.com/reportingreference/sample_model_statement.html
[ibkr-daily]: https://www.ibkrguides.com/reportingreference/reportguide/daily_concatenated_sample.html
[ibkr-isin]: https://www.interactivebrokers.com/campus/glossary-terms/isin/
[ibkr-exchanges]: https://www.interactivebrokers.com/en/trading/products-exchanges.php
[gleif-api]: https://documenter.getpostman.com/view/7679680/SVYrrxuU?version=latest
[gleif-isin-factsheet]: https://www.gleif.org/lei-data/lei-mapping/download-isin-to-lei-relationship-files/2021-06-08_isin-to-lei_factsheet_v1.pdf
[anna-identifiers]: https://anna-web.org/identifiers/
[esma-firds]: https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_firds
[fca-firds]: https://data.fca.org.uk/artefacts/FIRDS/FULINS_E_20261031_01of01.zip
[baba-f6]: https://www.sec.gov/Archives/edgar/data/1472033/000119380522000150/e621191_f6ef-aghl.htm
[jpm-ir]: https://www.jpmorganchase.com/ir/fixed-income
[jpm-mm]: https://www.jpmorganchase.com/content/dam/jpmc/jpmorgan-chase-and-co/investor-relations/documents/US48128B5232-Prospectus.pdf
[sec-13f-list]: https://www.sec.gov/rules-regulations/staff-guidance/division-investment-management-frequently-asked-questions/official-list-section-13f-securities
[sec-13f-faq]: https://www.sec.gov/rules-regulations/staff-guidance/division-investment-management-frequently-asked-questions/frequently-asked-questions-about-form-13f
[sec-13f-2026q3]: https://www.sec.gov/files/investment/13flist2026q3.pdf
[sec-access]: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
[cfr-13f-1]: https://www.govinfo.gov/content/pkg/CFR-2025-title17-vol4/xml/CFR-2025-title17-vol4-sec240-13f-1.xml
