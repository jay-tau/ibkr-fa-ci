# ADR 0005: Robinhood brokerage source classification

- **Status**: Superseded by ADR-0006
- **Date**: 2026-08-11
- **Deciders**: jay-tau
- **Issue**: #364 (tracker), #439 (this ADR)

## Context

The supplied Robinhood individual-brokerage account-activity CSV is private
and gitignored. It has one exact nine-column layout:

1. Activity Date
2. Process Date
3. Settle Date
4. Instrument
5. Description
6. Trans Code
7. Quantity
8. Price
9. Amount

A local audit established, without publishing holdings, dates, values,
identifiers, descriptions, or activity counts, that every non-structural row
matches exactly one event variant listed below. The file also contains an
expected blank separator and a scope footer stating that Robinhood Crypto and
Spending activity are excluded.

The CSV is a transaction ledger, not a complete account record. It does not
independently establish account identity or opening date, reporting-period
coverage, opening/closing balances, cash-sweep participation, original basis
for received or transferred securities, exact execution fees, transfer
counterparties, or every tax classification. The current shared
`SourceExtract` cannot preserve cash transfers, fees, security transfers,
options, Stock Lending, all three dates, description-carried record/payment
dates, or transfer quantity suffixes. Mapping the CSV directly into the shared
tax models would therefore discard evidence before it can be reconciled.

Current public Robinhood documents, pinned through Robinhood's
[legal-document index][robinhood-legal-index], settle the legal roles of the
Robinhood entities and the possible structures for uninvested cash. They do not
prove which structure applied to this account in any period. Terms also changed
within recent calendar years: the current High-Yield Cash Program includes a
brokerage-held tier that must not be back-applied to an earlier period without
the contemporaneous agreement and statements.

## Decision

### 1. Give each source a bounded evidentiary role

There is no total precedence order in which one document may overwrite every
other document. Each source is primary for particular facts and verifies the
others where they overlap:

| Evidence | Primary facts | Required cross-check |
|---|---|---|
| Full-history account-activity CSV | Event occurrence, three source dates, instrument, description, code, quantity, displayed price, and signed cash amount | Statements, lots, confirmations, tax forms, and transfer evidence |
| Monthly/quarterly and year-end statements | Account/carrying-entity identity, statement coverage, positions, settled cash, account value, corrected-statement status, and program-bank balances | Activity ledger, position reconstruction, and year-end evidence |
| Tax-lot/cost-basis evidence | Broker lot identity, original acquisition date, reported basis, covered/noncovered status, and transfer/corporate-action history | Activity quantities, statements, and confirmations |
| Trade confirmations | Order/execution identity and ordering, gross consideration, fees, option contract terms, and primary-issue details | Activity rows and tax-lot evidence |
| Transfer evidence and counterpart-account records | Cash/security direction, counterparty, unchanged beneficial ownership, and carried acquisition date/basis | Both accounts' statements/lots where available |
| Form 1042-S and other issued annual tax forms | Annual income code, jurisdiction, gross income, withholding, amendment status, and reported tax treatment | Event-level income/withholding and annual statement totals |
| Robinhood agreements, Form CRS, and regulatory records | Legal-entity roles and the available cash-program structures for their effective period | The account's contemporaneous statements for actual participation |

A mismatch on the same fact refuses; no source silently replaces another.
Corrected/reissued documents take precedence only through an explicit
version/amendment rule. Missing evidence remains missing rather than becoming
zero, absent, or a current-date/current-price substitute.

### 2. Preserve a lossless source event ledger before adapting

Issue #440 first parses every activity row into a source-specific typed event.
It preserves all nine cells semantically, all three dates separately, raw code
and description, physical row occurrence, and any quantity suffix. Exact
duplicate-looking rows remain distinct. No parser-side deduplication, FIFO,
tax classification, or schedule mapping is allowed.

Classification uses the combination of transaction code, recognized
description grammar, populated-field layout, sign, and quantity suffix. Code
alone is not authoritative. Any new combination refuses rather than falling
through to a nearby type.

The accepted taxonomy and its first downstream owner are:

| Event variant | Required distinction | Owner and disposition |
|---|---|---|
| ACH deposit | `ACH`; no instrument/quantity/price; positive amount; deposit grammar | #441 settled-cash and statement reconciliation |
| ACH withdrawal | `ACH`; no instrument/quantity/price; negative amount; withdrawal grammar | #441 settled-cash and statement reconciliation |
| ACH cancellation | `ACH`; cancellation grammar and opposite-signed original required | #441 reversal pairing and cash reconciliation |
| Ordinary equity purchase | `Buy`; instrument/quantity/price; negative amount; no special annotation | #442 lot provenance, then #446 CG and #447 FA |
| Dividend-reinvestment purchase | `Buy`; same populated layout; explicit dividend-reinvestment annotation | #442 acquisition lot; paired income remains with #443 |
| Primary-issue purchase | `Buy`; same populated layout; explicit primary-issue annotation | #442 lot/confirmation provenance |
| Equity sale | `Sell`; instrument/quantity/price; positive amount | #442 lot identity, #446 FIFO/proceeds, and #447 FA |
| Option buy-to-open | `BTO`; instrument/quantity/price; negative amount | #445 option lifecycle and tax/FA treatment |
| Option sell-to-close | `STC`; instrument/quantity/price; positive amount | #445 option lifecycle and tax/FA treatment |
| Cash dividend | `CDIV`; instrument; positive amount; no quantity/price columns | #443 dividend, withholding, annual-form, and tax schedules |
| Cash interest | `INT`; positive amount; no instrument/quantity/price | #443 interest and tax schedules |
| NRA withholding | `NRAT`; instrument; negative amount | #443 payment pairing and FTC evidence |
| Foreign-tax withholding | `DTAX`; instrument; negative amount and source grammar | #443 jurisdiction proof and paired-payment handling |
| Stock Lending income | `SLIP`; instrument; positive amount; no quantity/price columns | #444 subtype/tax treatment and #441 cash reconciliation |
| ADR fee | `AFEE`; instrument; negative amount; description-carried shares/rate | #444 fee treatment and #441 cash reconciliation |
| External debit-card transfer | `DCF`; no instrument/quantity/price; positive amount | #441 transfer/cash reconciliation; never income by code alone |
| Instant-bank transfer | `RTP`; no instrument/quantity/price; positive amount | #441 transfer/cash reconciliation |
| Inter-account cash transfer in | cash-layout `ITRF`; no instrument/quantity/price; positive amount | #460 cash evidence, then #461 reciprocal transfer proof |
| Inter-account cash transfer out | cash-layout `ITRF`; no instrument/quantity/price; negative amount | #460 cash evidence, then #461 reciprocal transfer proof |
| In-kind security transfer | security-layout `ITRF`; instrument and suffixed quantity; no price/amount | #461 reciprocal proof, then #442 carried lot provenance |
| Security receipt | `REC`; instrument and quantity; no price/amount | #442 event-specific receipt/lot treatment |

Positive `NRAT` and negative `CDIV` correction/reversal layouts are not
accepted speculatively: no exact source row/description evidence for those
layouts is currently available. #440 refuses them as new combinations. If
supplied later, the taxonomy and synthetic fixtures must first be extended;
#443 then owns equal-and-opposite pairing through the existing withholding
reversal semantics and source-specific dividend correction handling.

Description-carried CUSIP, record date, payment date, shares, rate, option
contract terms, and other redundant facts remain on the source event even when
the existing canonical model has no field for them. Adapters may produce
`OpenLot`, `ClosedLot`, `Dividend`, `WithholdingTax`, or `InterestCredit` only
after the evidence needed by that model is complete.

The exact header, one expected structural blank separator, and the exact scope
footer are validated structural records rather than typed events. A duplicate
footer, shifted column, non-blank structural row, row after the footer, or new
footer wording refuses.

Every typed event ultimately receives exactly one disposition:

1. adapted into a canonical tax/FA model;
2. consumed by account, transfer, or tax-form reconciliation;
3. proven outside the requested CY/FY; or
4. reported as a hard blocker.

There is no "parsed but unused" disposition. Issue #448 owns the end-to-end
coverage assertion.

### 3. Freeze date roles and the unsettled-cash invariant

All consumers use the same date mapping:

| Consumer | Source date |
|---|---|
| Acquisition/disposal date, trade CY/FY membership, Indian holding period, and A3 holding window | Activity Date |
| Canonical dividend, interest, and withholding credit date; Rule 115/Rule 128 event date | Activity Date |
| Settled-cash movement and A2 cash ledger | Settle Date |
| Audit/provenance | Process Date only; it never drives a filed figure |

Description-carried record/payment dates remain source-event audit fields and
must cross-check annual evidence. They do not replace the frozen canonical date
without a later superseding ADR.

For each cash-bearing event, the signed Amount is an unsettled
receivable/payable from end of Activity Date through the day before Settle
Date. At end of Settle Date it moves exactly once into settled cash and clears
from unsettled amounts. A same-day settlement has no closing unsettled balance.
Settle Date earlier than Activity Date is malformed and refuses. An event
proved never to settle produces no settled-cash movement; its cancellation
pair is resolved before any peak is taken. A later reversal is a separate
dated cash movement only when statements prove that the original event first
settled.

For trades, the security quantity changes on Activity Date. This produces the
end-of-day identity:

`A2 value = supported securities + brokerage-held settled cash +`
`unsettled receivables/payables + evidenced open-option value`

A purchase therefore adds the security and a negative payable until cash
settles; a sale removes the security and adds a positive receivable. Fees and
net/gross differences must first be reconciled from confirmation evidence.
The same invariant applies to income, withholding, fees, and transfers when
Activity Date and Settle Date differ.

`settled cash` means cash carried in the brokerage account. If a Program Bank
held cash during the period, the activity CSV cannot partition that cash from
the brokerage balance. The CSV-derived daily A2 peak is then invalid: #447
must use a statement-derived partitioned ledger or an account/CY-bound A2 peak
override. It never files a peak with swept cash folded into brokerage cash.
Any asset outside the terms enumerated above also makes the affected day
unpriceable.

Issue #441 owns the ledger and statement anchors. Issue #447 owns daily A2
valuation. Settle Date is present in the observed activity layout, but a
missing/malformed date or an unexplained statement difference blocks A2 rather
than weakening this invariant. A3 retains the existing end-of-day holding
window, including disposal-day exclusion for close-based peak valuation.

### 4. Classify the carrying entity and cash by its actual legal location

The public evidence used for this decision is:

| Source | Effective/published | Decided fact |
|---|---|---|
| [RHF Customer Relationship Summary (Form CRS)][robinhood-crs] | Effective 2026-07-31 | RHF is the introducing broker; RHS custodies cash/securities and executes, clears, settles, and services the account |
| [RHF/RHS Customer Agreement][robinhood-customer-agreement], section 2.6 | Revised 2026-07-06 | RHF introduces the account on a fully disclosed basis; RHS carries it and owns clearing/bookkeeping duties |
| [RHS Statement of Financial Condition][robinhood-rhs-financial], Note 1 | As of 2026-06-30; published 2026-08-04 | RHS is the clearing broker-dealer for RHF-introduced retail accounts |
| FINRA BrokerCheck: [RHF CRD 165998][robinhood-rhf-brokercheck] and [RHS CRD 287900][robinhood-rhs-brokercheck] | Live registry | Distinct registered legal entities and the current registered address |
| [High-Yield Cash Program Agreement][robinhood-cash-program], sections A-C | Published 2026-06-24 | Brokerage-held free credit and Program Bank Deposit Accounts are distinct; statements carry bank activity/balances |
| [Cash sweep program help][robinhood-sweep-help] | Program-bank list dated 2026-07-01 | Enrollment can be enabled/disabled and the bank list changes |
| [Reports and statements help][robinhood-reports] | Current | Activity CSVs are transaction reports separate from monthly/quarterly statements |

The Form CRS says Robinhood Financial LLC (RHF) is the introducing
broker-dealer and that customer cash and securities are custodied by Robinhood
Securities, LLC (RHS). Customer Agreement section 2.6 likewise says RHF
introduces the account to RHS on a fully disclosed basis and RHS carries the
account and performs clearing and bookkeeping. RHS's statement of financial
condition identifies it as the clearing broker-dealer.

The Schedule FA A2 institution for the supported brokerage account is therefore
**Robinhood Securities, LLC**, the carrying/custodial firm. RHF remains
provenance as introducing broker and customer-facing agent; "Robinhood",
"Robinhood Markets", or a marketing address is not substituted for the legal
custodian. The legal name and contemporaneous address must cross-check the
statement and RHS BrokerCheck record; a mismatch refuses.

The brokerage account is an A2 foreign custodial account under the same
[Rule 114F][rule-114f], [official Schedule FA/FSI guide][fa-guide], and
[vendored ITR schema][itr-schema-source] taxonomy used by ADR 0004. Supported
securities held inside it are also disclosed separately in A3. A2 and A3
overlap by design and are never added together for threshold or reconciliation
purposes.

Robinhood cash has two materially different possible locations:

| Cash location | Evidence-backed legal fact | Filing contract |
|---|---|---|
| Brokerage-held free credit balance | Payable on demand by RHS and held in the brokerage account | Include in A2 account value; no A1 row |
| Cash swept to a Program Bank | Held in a NOW/DDA/MMDA Deposit Account; a direct obligation of that bank, not Robinhood, although RHS acts as agent/custodian | Do not hide it as brokerage-only cash; report potential A1 manual work under #19/#17 |
| Eligible cash awaiting a sweep | Remains in the investing account until deposited | Include in A2 until statement evidence proves the bank deposit |

The current High-Yield Cash Program Agreement says a brokerage-held tier
remains at Robinhood and amounts above that tier can enter the Cash Sweep
Program. It also says statements show all Deposit Account activity, per-bank
opening/closing balances, and interest. Enrollment, eligibility, account type,
program-bank sequence, thresholds, and program terms can vary. Current terms
must not be applied to an earlier period.

Consequently:

- sweep participation is never assumed in either direction;
- an activity CSV without sweep rows does not prove non-participation or zero
  bank balances;
- the dated Program Bank list validates a statement name but never supplies a
  balance;
- account-period statements must prove whether and when any Program Bank held
  cash; and
- a bank-held balance invalidates any unpartitioned CSV-derived A2 peak and
  prevents filing-complete success because A1 generation is out of scope.

Program Bank interest is partitioned from brokerage interest using the
statement's per-bank interest evidence. It remains foreign interest for the
income/FSI path, but it is associated with the potential A1 deposit rather
than reported as an A2 gross credit. An `INT` row that cannot be partitioned
when Program Bank participation is proved blocks the affected outputs.

Partitioning runs account-wide over the complete statement range. The parsed
Deposit Sweep Activity ledger is already running-balance and source-total
checked, while the combined cash identity proves every sweep credit also
appears in Account Activity. An exact `(date, amount)` multiset match between
an `INT` event and a positive-credit `INTEREST PAYMENT` sweep row proves bank
interest. An unmatched `INT` proves brokerage interest only when every
covering statement has complete or absent sweep evidence and every sweep
interest row is consumed. Partial/extra matches, unknown sweep descriptions,
interest debits, or banks without a sweep ledger leave all unresolved
references blocked. Exact source amounts remain unchanged for OS/FSI.

Unknown sweep status is not an error in this ADR. It is a frozen runtime
hard-block: #441 parses/detects the statement facts, #447 keeps bank-held cash
out of an A2-only completeness claim, and #448 reports the A1 requirement with
exit 3 and emits no filing-complete CBDT fragment. This lets the source contract
close without misrepresenting currently unavailable account evidence.

### 5. Freeze configuration ownership

Robinhood does not reuse IBKR-specific account facts merely because they
currently live under a generic-looking table.

| Configuration | Contract | Owner / missing-value behavior |
|---|---|---|
| `[account].holder_name` | Shared taxpayer fact | Reused; required by all filing flows |
| `[account].us_person` | Shared taxpayer expectation; Robinhood W-8/W-9 evidence must cross-check it | #441; mismatch refuses |
| `[account.foreign_tins]` | Shared per-country taxpayer TIN | Reused by FSI/TR/Form 67 |
| `[account].number`, `.opening_date`, `.status` | Legacy IBKR account facts; never reused for Robinhood | #441 generalizes loading only with a source-specific account block and preserves required guards in every existing consumer |
| `[[robinhood.accounts]]` | `number`, `opening_date`, `status`, `institution_name`, `address`, `zip`, `country_iso2`, `same_beneficial_owner`, `source` | #469/#470; each filed account is statement-cross-checked and produces its own A2 row |
| `[[robinhood.securities]]` | `symbol`, `cusip`, `isin`, `listing_exchange`, `sub_category`, `asset_class`, `issuer_country_code` | #442; missing/mismatched identity blocks every affected security; `issuer_country_code = "IN"` can't be filed in Schedule FA, whose Table A3 excludes India, so `robinhood-fa` and `robinhood-all` exit 4 (#1325) |
| `[[fa.entities]]` | Shared ISIN-keyed A3 `name`, `address`, `zip`, and `nature_of_entity` presentation | #442 resolves Robinhood security to ISIN; #447 requires filed fields |
| `[[prices.ticker_overrides]]` | Shared `(broker ticker, isin) -> yahoo_symbol` price identity; the existing config key remains `ibkr_ticker` | #447 consumes it; no rename or second registry |
| `[[robinhood.a2_peak_overrides]]` | No longer accepted | #628; automatic A2 valuation never read it, so a config that sets it is refused on load |
| `[[robinhood.period_attestations]]` | `start_date`, `end_date`, `crypto_activity`, `spending_activity`, `source` | #448; absent/unsupported scope remains exit 3 |
| `[[robinhood.option_classifications]]` | `account_number`, `financial_year`, `fema_risk_acknowledged`, `treatment`, `source`, `reason` | #445; F&O processing refuses unless both the FEMA-risk acknowledgement and reviewed treatment are explicit |

> **Note (2026-11-15, #1433):** Three rows above don't match what shipped.
> There is no `[[robinhood.period_attestations]]` table: a config that sets
> one is refused as an unknown key.
> [ADR 0006](0006-robinhood-multi-account-evidence.md) instead puts Crypto and
> Spending out of scope under [ADR 0013](0013-robinhood-crypto-out-of-scope.md)
> and [ADR 0015](0015-robinhood-spending-out-of-scope.md).
> `[[robinhood.securities]]` also requires `is_depositary_receipt` and
> `source`. No Robinhood command reads `[account].us_person` or refuses on a
> W-8/W-9 mismatch. Only the IBKR `schedule-fa` US-withholding advisory
> reads it.

`[[robinhood.accounts]].number` is the sole binding identifier for all
source-specific config. `opening_date` is never inferred from first activity.
The only supported account type is the statement-proven individual brokerage
account; there is no config knob that relabels another account type as
supported.

The shared-loader change must retain explicit `number`, `opening_date`, and
`status` requirements in every IBKR path and in `stockplan-fa`, which currently
consumes the global opening date. A source-specific block relaxes only
irrelevant top-level account fields; it does not make an existing pipeline
accept less evidence.

> **Note (2026-10-29, #600):** StockPlan commands no longer read or require
> `[account].opening_date`, which is the IBKR account's date. Their Capital
> Gain/Loss coverage window starts at the optional `[stockplan].opening_date`,
> or covers the whole FY/CY without it. Without a StockPlan date the window
> can only widen, so no existing path accepts less evidence.
> `[account].status` is still required.

`[account].us_person` remains the only W-8/W-9 expectation key. #441
cross-checks any explicit statement status; #443 cross-checks issued tax forms
and withholding behavior. Missing or conflicting account evidence is surfaced,
not replaced by a second Robinhood-specific boolean.

> See the 2026-11-15 note under the table: these cross-checks didn't ship.

For `[[robinhood.securities]]`, the description CUSIP must match config. A US
security's ISIN must equal `US` + CUSIP + a valid ISO 6166 check digit. That
verified ISIN then selects `[[fa.entities]]`; the entity override controls only
filed presentation and cannot replace CUSIP, ISIN, issuer country, or asset
class. ADR 0009 separately resolves the held instrument's legal issuer address
from its exact ISIN; SEC business/headquarters addresses never populate A3.
Configured address values are fallback or explicit reviewed opt-out data, while
configured name and nature remain presentation inputs. Missing A3 presentation
fields remain explicit manual-fill and prevent CBDT JSON output.

> **Note (2026-11-02, #777):** SEC principal business addresses can populate
> A3. [ADR 0009](0009-robinhood-legal-addresses.md)'s 2026-08-27 amendment adds
> a bounded US common-stock fallback to the SEC filer's principal business
> address. Since its 2026-11-02 amendment (#792) the fallback runs only after
> a verified GLEIF zero-match or an otherwise valid `LAPSED` GLEIF record.

`[[prices.ticker_overrides]]` remains one shared mechanism. Its existing
`ibkr_ticker` name is retained for compatibility and interpreted as the
source-broker ticker when Robinhood calls it. The override remains ISIN-bound.
No alias or Robinhood-specific price registry is added.

> See the 2026-11-15 note under the table: the period attestations below
> didn't ship.

The period-attestation values are:

- `crypto_activity` / `spending_activity`: `absent` or `separate-source`.

Attestation ranges must tile the union of the requested Schedule FA calendar
year and tax-schedule financial year exactly, without gaps or overlaps. A
missing attestation or a separate out-of-scope product keeps the complete run
at exit 3. Program-bank status is not configurable: #441 derives it from
complete statements, and missing statement proof means unknown/exit 3. The
exact supplied footer is the accepted scope contract; a future footer with
another excluded product is an unknown layout and refuses until this contract
is updated.

Option `treatment` is one of `capital-gains`, `speculative-business`, or
`non-speculative-business`. It is a reviewed, account/FY-bound tax decision,
not an inference from trading frequency. Business treatment remains explicit
ITR-3 P&L/BP/CFL/FSI manual work under #445. A U.S.-exchange derivative does
not qualify for section 43(5)(d)'s prescribed recognised-exchange exception;
an explicit non-speculative treatment therefore requires another reviewed
statutory basis.

Config values that drive filed output are surfaced as config attestations in
review output. Account/period binders prevent copied stale values. A malformed
or contradictory value refuses; an absent value blocks only the affected
schedule, while `robinhood-all` reports the incomplete leg and exits 3.

### 6. Refuse known ambiguity classes

| Ambiguity | Required resolution |
|---|---|
| Indistinguishable duplicate-looking rows without transaction IDs | Preserve every occurrence; require confirmation/lot/statement evidence before any affected ordering or FIFO result |
| Same-day buys and sells | Confirmation or tax-lot ordering; never CSV row order alone |
| Displayed sale price does not explain signed cash within rounding | Confirmation-sourced gross proceeds and fees |
| ACH cancellation | Evidence-backed opposite-signed original and settled/reversed status |
| Unrecognized income/withholding correction or reversal sign | Refuse until an exact source variant is added; once supported, require an equal-and-opposite original and refuse partial/orphaned/cross-source pairing |
| Cash/security transfer direction or counterparty | #461 reciprocal proof plus #462 counterpart registry |
| Transferred/received security lacks acquisition date or basis | Hard block; never zero basis or receipt date |
| `REC` meaning | Event-specific evidence for transfer, corporate action, or other receipt |
| Stock Lending payment flavor | Statement/tax-form evidence for lending fee, substitute payment, or other category |
| `DTAX` jurisdiction | Paired payment plus annual evidence; v1 accepts only jurisdiction matching verified ISIN country |
| Option lifecycle or Indian tax treatment | Confirmation/lot evidence plus #445 reviewed classification |
| Account opening balance and complete coverage | Statements or complete surviving-lot evidence; never infer zero from earliest activity |
| Cash sweep, Crypto, or Spending absence | Period evidence/attestation; absence from this CSV is not proof |

Every unresolved class has an owning downstream issue and blocks the affected
filing-complete result. It is never converted to a warning followed by exit 0.

### 7. Keep implementation checks with their implementation

This ADR defines, but does not add, parser fixtures or runtime assertions:

- #440 owns one synthetic fixture per accepted event variant and every
  structural refusal, plus the optional skip-gated local audit of the private
  export;
- #448 owns the end-to-end assertion that every typed event has exactly one
  disposition; and
- CI never depends on private files.

No parser, config class, fixture, CLI, calculation, output writer, plugin
framework, or generic security master is added by #439.

## Consequences

Robinhood implementation starts with #440's lossless event ledger and #449's
evidence parsers. Tax and Schedule FA adapters may be added only after the
relevant identity, balance, basis, transfer, and classification evidence is
available.

The supported account is reported in A2 under Robinhood Securities, LLC, with
its supported securities separately in A3. Brokerage-held cash can remain in
A2. Program-bank cash is not silently folded into an A2-only completeness
claim; until A1 support lands, its presence produces explicit manual work and
exit 3.

The currently unavailable statements, tax lots, confirmations, transfer
records, and annual forms do not keep this architecture decision open. Their
absence is represented by named runtime blockers. New event shapes, statement
layouts, excluded products, carrying entities, or cash-program terms require a
new or superseding decision rather than a permissive parser extension.

[rule-114f]: https://incometaxindia.gov.in/w/rule-114f
[itr-schema-source]: ../../src/ibkr_fa/reference/cbdt_itr2_schema_ay2026_27.json
[fa-guide]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-03/Step%20by%20Step%20Guide%20FA%20FSI.pdf
[robinhood-crs]: https://cdn.robinhood.com/assets/robinhood/legal/RHF%20Customer%20Relationship%20Summary.pdf
[robinhood-customer-agreement]: https://cdn.robinhood.com/assets/robinhood/legal/Robinhood-Customer-Agreement.pdf
[robinhood-cash-program]: https://cdn.robinhood.com/assets/robinhood/legal/IntraFi%20Network%20Deposit%20Sweep%20Program%20Agreement.pdf
[robinhood-sweep-help]: https://robinhood.com/us/en/support/articles/deposit-sweep-program/
[robinhood-reports]: https://robinhood.com/us/en/support/articles/reports-and-statements/
[robinhood-rhs-financial]: https://cdn.robinhood.com/assets/robinhood/legal/RHS-Unaudited-Statement-of-Financial-Condition.pdf
[robinhood-rhf-brokercheck]: https://brokercheck.finra.org/firm/summary/165998
[robinhood-rhs-brokercheck]: https://brokercheck.finra.org/firm/summary/287900
[robinhood-legal-index]: https://cdn.robinhood.com/assets/robinhood/legal/log_file_revised_full.us_en.json
