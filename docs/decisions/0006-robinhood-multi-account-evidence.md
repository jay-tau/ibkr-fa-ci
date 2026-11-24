# ADR 0006: Robinhood multi-account evidence and lot reconstruction

- **Status**: Accepted
- **Date**: 2026-08-13
- **Deciders**: jay-tau
- **Issue**: #468
- **Supersedes**: ADR-0005
- **Amended by**: ADR-0012, ADR-0013, ADR-0014, ADR-0015, ADR-0017

## Context

ADR 0005 was based on one supplied individual-brokerage activity report. It
correctly froze strict event classification, date roles, settled/unsettled
cash, Robinhood Securities, LLC as custodian, Program Bank handling, privacy,
and fail-closed output. Later evidence invalidated three of its assumptions:

1. a taxpayer can hold two distinct same-owner Robinhood brokerage accounts,
   one individual and one managed-individual, each with its own activity report,
   statement bundle, cash, positions, opening date, and account number;
2. the managed report adds exact event shapes not covered by ADR 0005; and
3. Robinhood offers no documented self-service lot export or bulk confirmation
   download. A per-account/year realized gain/loss CSV can be requested through
   support, and trade details can be viewed one at a time, but neither is a
   supplied source for this integration.

Each supported activity file retains ADR 0005's exact nine-column header and
scope footer. A privacy-safe audit found only these additional shapes:

- `Buy` with a third description line exactly `Managed Trade`;
- `Sell` with a third description line exactly `Managed Trade`;
- `ADVFEE` with description exactly `Management Fee`, an amount-only layout,
  and a negative signed amount; and
- security `ITRF` with instrument, CUSIP, and positive quantity but no `S`
  quantity suffix. The individual-account form carries `S`.

No private account-identifying or economic dates, securities, quantities,
values, identifiers, descriptions, or counts are part of this decision. The
generic broker vocabulary above is the minimum exact parser contract.

Robinhood's [reports help][robinhood-reports] says account-activity CSVs are
generated for a user-selected account type, start date, and end date. The CSV
itself carries no account identity or selected-range metadata. It therefore
cannot prove that it belongs to a statement account or spans a filing period
merely because of its filename or earliest/latest event.

Robinhood does not publish a column dictionary for activity `Amount`, `Price`,
`Quantity`, or transaction codes, and does not say whether trade amounts are
gross or net of regulatory or exchange fees. Statements can prove the same
cash movement but are not independent evidence of those field semantics.

Robinhood Asset Management's [Form ADV brochure][robinhood-ram-brochure] and
[Advisory Agreement][robinhood-advisory-agreement] do settle the managed
economics. Management fees accrue daily, are charged monthly in arrears,
apply separately to each managed account, and are deducted directly from that
account. Trading/regulatory costs are client-borne and separate from the
management fee. Managed orders may be aggregated and allocated to clients at
an average price. None of those documents defines the CSV encoding.

Indian law adds a separate ambiguity. Section 45(2A) of the Income-tax Act,
1961 applies mandatory FIFO to securities transferred by a "depository" or
"participant" defined through the [Depositories Act,
1996][depositories-act]. That definition is limited to a SEBI-registered
Indian depository, so it does not expressly cover a US broker. [CBDT Circular
704][circular-704] applies FIFO when purchase and sale lots cannot otherwise
be correlated but does not state whether multiple foreign accounts are pooled.
For Indian demat accounts, [Circular 768][circular-768] paragraph 5(b) and
[*Radhika Roy / Dr. Prannoy Roy v. DCIT*][prannoy-roy] apply FIFO
account-wise. The Tribunal order is under a [pending Delhi High Court
appeal][prannoy-roy-appeal] whose interim order was made absolute during the
appeal. No located primary authority settles the same-owner, foreign-account
transfer boundary. Circular 768 is reported at (1998) 232 ITR 5 (St.); the
official URL requires browser access, while the cited Tribunal order quotes
its account-wise paragraphs.

## Decision

### 1. Carry forward ADR 0005 unless this ADR changes it

The following ADR 0005 decisions remain binding:

- classification is exact code + description grammar + populated layout +
  sign + suffix, never code alone;
- every source field and duplicate occurrence is preserved;
- Activity Date drives acquisition/disposal, income, withholding, and holding
  windows; Settle Date drives settled cash; Process Date is audit only;
- trade-dated securities plus settle-dated cash use explicit unsettled
  receivables/payables;
- Robinhood Securities, LLC is the A2 custodian;
- Program Bank cash remains in the Robinhood custodial A2 account under
  ADR-0012;
- Crypto and Spending are explicitly out of scope under ADR-0013 and ADR-0015;
  futures absence is not inferred from the brokerage report;
- positive-`NRAT`, negative-`CDIV`, unknown corrections, and every unknown
  source combination refuse; and
- models and failures remain privacy-safe, with no private CI dependency.

This ADR also explicitly replaces ADR 0005's individual-account-only type
restriction and ambiguity rows that required unavailable confirmation/lot
evidence for duplicate ordering, same-day trades, and transferred basis. Where
ADR 0005 says one account, full-history CSV, required tax-lot or confirmation
evidence, suffixed ITRF, or one-account orchestration, this ADR replaces it.
Transfer direction/counterparty authority is #461's statement proof against
#469's configured account set, never the unsigned CSV row or #462's replaced
counterpart registry. Singular peak/manual-value binders are likewise replaced
by an exact account number on every account-scoped entry.

### 2. Treat every statement account as a filed account

The individual and managed-individual accounts are separate foreign custodial
accounts. Each requires:

- its own Schedule FA A2 row;
- account number, opening date, status, institution/address, and country;
- statement and report coverage;
- settled cash, Program Bank status, closing balance, and daily/attested peak;
- gross-credit components; and
- independent income/tax-form coverage.

Configuration becomes `[[robinhood.accounts]]`, exactly two strict entries for
the supplied v1 bundle. Supporting a different account count requires a
superseding decision because #461 currently proves one reciprocal account
pair. Account-scoped overrides and attestations carry an exact
`account_number` binder. Shared taxpayer facts, security identity, entity
presentation, and market-price mappings remain shared.

The singular `[robinhood.account]` and transfer-only
`[[robinhood.counterpart_accounts]]` schema has not shipped through the
template or CLI. It is replaced, not retained as a compatibility alias.

Every configured account must match exactly one validated statement bundle,
and every bundle must match one configured account. Distinct statement
accounts may not share an A2 row or inherit one another's config.

An internal movement does not split the economic security interest for A3. One
transferred lot produces one continuous A3 holding row across both accounts,
retaining its original acquisition date and never creating an A3 disposal or
duplicate peak/proceeds row. A2 remains account-specific.

### 3. Bind reports by evidence, not filenames

Each account-activity report has an account-bound attestation for the start and
end dates selected when it was generated. All activity dates must lie inside
that range. ADR-0017 amends assignment to evaluate attested ranges, configured
opening dates, account family, complete statement boundaries, and strict
activity reconciliation together for each complete candidate.

Both complete report/bundle bijections are tested. Assignment is accepted only
when exactly one survives all evidence above. Filenames and directory names
never establish identity. A swapped, duplicate, absent, or multiply matching
complete assignment refuses.

Every parsed pending-settlement row requires the statement's printed pending
debit/credit total, and the parsed rows must reconcile to it exactly. A
statement without pending rows does not require that section or total.

Each bundle is primary in exactly one activity and cash reconciliation. The
other bundle is its counterpart. Both runs reuse the same sorted bundle
objects, so statement references remain resolvable. Reciprocal transfer proof
runs once over the pair; it is not duplicated per direction. #470 owns this
two-report reconciliation and assignment contract.

A config opening date alone never narrows coverage. Once it is reconciled
against the source evidence below, each account's required activity-report and
statement coverage starts at the later of the requested period start and the
account opening date:

- the date lies inside the first statement period;
- the first statement carries the exact source opening-layout evidence with
  unavailable (`N/A`) opening components;
- the full-period activity report has no earlier account event; and
- no earlier statement, balance, position, or account reference exists.

Otherwise the pre-statement span is a coverage gap and the computed A2/A3 peak
is incomplete; an account/CY-bound override or manual-fill remains required.
From the accepted statement start through the requested end, statement
coverage must be complete for every account contributing to that output.

CSV events settling after the latest issued statement remain pending
settlement evidence. Trade-dated figures may proceed when the trade itself is
statement-proved through pending-trade evidence and ADR 0005's unsettled
receivable/payable invariant. A pending row proves the trade only if the first
CUSIP it prints, when it prints one, is that trade's CUSIP. An option row proves
its trade only if its contract equals the trade's. Settled cash,
closing balance, and peak figures that depend on the later settlement remain
blocked until statement coverage reaches it.

At an issued statement boundary with proved Pending Trades, maintain both
trade-date and settlement-date quantity ledgers. Accept the position snapshot
only when it matches exactly one ledger within the existing display tolerance
and the complete difference is explained by the statement-matched pending
trades. A2 then values trade-date quantity plus unsettled cash; lot and
statement reconciliation retain the explicitly proved snapshot basis. If
neither or both ledgers match, the snapshot-basis blocker remains.

Schedule FA A3's lots are trade-dated. So where an account's Dec-31 snapshot
rests on the settlement-date basis and shows shares, that account contributes
the proved trade-date quantity and its own statement total scaled to that
quantity: total × trade-date quantity / statement quantity, kept as an exact
rational (#1246). Other accounts contribute their statement quantities and
totals. For each security, A3 compares the combined open-lot quantity with the
combined effective quantity within the summed anchor tolerance, then allocates
the combined total by lot quantity. The Dec-31 close cross-check compares each
exact scaled share. Existing blockers still refuse an unproved anchor or basis,
a closing quantity mismatch beyond tolerance, and a failed close cross-check.
Open lots without any Dec-31 statement shares still get the closing blocker;
a zero-statement account cannot obtain a price from another account's scaled
holding in that security. Snapshots on the trade-date or both-ledgers basis
keep their statement contributions; wholly unscaled securities keep main's path.

Statement-only `COIN` rows are proved brokerage-cash transfers to or from the
separate Crypto product. They enter the A2 brokerage cash ledger on settlement
date when stated, otherwise transaction date. They do not create a brokerage
security position or imply anything about the separate Crypto asset balance.

### 4. Extend the event taxonomy narrowly

Issue #440 adds:

| Event | Exact accepted shape | First owner |
|---|---|---|
| Managed equity purchase | `Buy`; exact `Managed Trade` annotation; ordinary trade fields; negative amount | #442 / #446 / #447 |
| Managed equity sale | `Sell`; exact `Managed Trade` annotation; ordinary trade fields; positive amount | #442 / #446 / #447 |
| Management fee | `ADVFEE`; exact `Management Fee`; amount only; negative amount | cash/A2 reconciliation, then #444 |
| Managed in-kind transfer | Existing security `ITRF` shape with suffix absent rather than `S` | #461 quantity proof, then #442 |

Managed purchases and sales are distinct typed kinds. Account family is a
file-level fact and cannot replace row-level annotation provenance. The
managed kinds enter the same trade/pending-cash sets as ordinary equity trades.

For in-kind transfers, only the two observed quantity suffix states are
accepted: `S` and absent. The parser preserves which occurred. Any other
suffix, annotation, sign, or populated-field layout refuses. Before a
suffix-less quantity is used in inventory, both accounts' statement positions
must prove per-security continuity.

A row carrying both `Managed Trade` and another special annotation, including
dividend reinvestment or primary issue, is an unknown combination and refuses.

`MANAGEMENT_FEE` is a cash debit only. It is not acquisition basis, sale
expense, dividend netting, or deductible income expense unless #444 later
establishes an authority-backed treatment. Until then it is reconciliation
evidence and an explicit tax-treatment blocker where a filed deduction is
requested. The [Form ADV brochure][robinhood-ram-brochure],
[Advisory Agreement][robinhood-advisory-agreement], and [Strategies
FAQ][robinhood-strategies] independently confirm per-account accrual and
direct monthly debit, but do not define the CSV row. Insufficient fee cash may
cause a separate asset sale; no sale is paired to a fee without source
evidence.

### 5. Use only available basis and execution evidence

The primary source bundle is:

| Evidence | Bounded role |
|---|---|
| Per-account activity CSV | Event occurrence, three dates, instrument/CUSIP, quantity, displayed price, signed cash, and exact variant |
| Per-account statements | Account identity, coverage, positions, settled cash/value, fees, transfers, and Program Bank evidence |
| Form 1042-S packet | Account-bound annual income/withholding classification and totals; not proceeds or basis |
| Public/legal documents | Product mechanics, legal roles, fee schedules, and available report types |
| Optional support-generated realized gain/loss CSV | Corroboration only after an exact supplied format is parsed under #454 |
| Per-trade in-app confirmation | Manual ambiguity resolution only; no bulk parser or assumed availability |

An unavailable optional document is not a permanent dependency. It remains a
named resolution path for an exact ambiguity. No schema, field, fee, order, or
lot is inferred from a document that was not supplied.

Optional broker lot evidence may identify which lots physically moved between
accounts when its exact format and internal consistency are proved. That is a
source fact. A broker's US lot selection for a sale never replaces the
independently computed Indian FIFO tax rule.

### 6. Reconstruct FIFO account-wise

For Indian demat accounts, [Circular 768][circular-768] and the [Delhi
ITAT][prannoy-roy] apply FIFO to each account, not to the taxpayer's pooled
holdings. The statute does not expressly extend that rule to foreign brokers,
and the Tribunal order remains under appeal. Account-wise queues are therefore
an explicit foreign-account policy by analogy, not a claim of settled law or
of always producing a more conservative tax result. They preserve the CBDT's
rationale that securities still lying in another account were not sold from
the debited account.

Maintain FIFO by original acquisition date per `(account, verified security
identity)`. The queue boundary remains the reportable account with its own
account number, statement bundle, and A2 row.

An internal transfer between the two proved same-owner accounts:

- is not a disposal and creates no gain/loss;
- uses exact broker-identified lots when later optional evidence supplies an
  internally consistent identity; otherwise it dequeues source-account FIFO;
- enqueues the same quantity into the destination account on the transfer
  date, preserving source FIFO order within a same-date transfer batch;
- carries original cost, original acquisition date, and the complete
  account/statement/event provenance chain; and
- never creates a zero-basis or receipt-date acquisition.

Original cost is preserved because no consideration or beneficial-owner
change creates a basis reset. The internal-transfer date never becomes an FX
or cost-basis date. USD cost carries unchanged, and each existing schedule FX
policy remains unmodified: [exact-date TTBR][fa-fx-policy] for Schedule FA and
one [Rule 115(1), Explanation (2)(f), conversion][cg-fx-policy] at disposal
for capital gains.

The reviewed policy retains original acquisition date and cost because the
paired accounts have unchanged beneficial ownership and the proved internal
transfer is not a disposal. [Circular 704][circular-704] and [Circular
768's][circular-768] contract-note cross-reference support that holding-period
start. Destination entry remains account-level holding-interval evidence but
does not reset tax-lot identity, FIFO ordering, basis, or holding period.

Any unresolved matched-lot or disposal attribution from same-day ordering
blocks the affected CG, A3 rows, and any CBDT fragment containing those rows.

Same-day source order is not a tax rule. Same-day acquisitions originating in
one account for one verified security retain each member quantity/cost. A
disposal may treat the group as order-invariant only when it consumes none or
all of the group, or every member has the same unit cost. That test compares
each member's exact cost and quantity (#1326), so an earlier pro-rata sale's
rounding is no difference in cost, and a later partial sale of the group can
split it pro rata again. Partial consumption
of differing-cost same-day members blocks affected CG and per-lot A3 rather
than inventing a weighted-average tax lot. Same-day disposals may aggregate
quantity/proceeds only after that basis condition is satisfied.

`compute.robinhood_lots.combined_disposal` owns that same-account,
same-security, same-trade-date grouping for every downstream schedule. Schedule
CG and Schedule FA use the same exact proceeds allocator. When source execution
prices differ, each lot row receives a quantity-proportional allocation with
the exact residual assigned to the final ordered slice; the source-group cash
total remains exact. Schedule CG blocks a group whose lots are both short-term
and long-term and whose sales' exact cash per share differs (#1540): the sale
executed first takes the oldest lots, so the sale order, which the activity
report doesn't give, decides the short/long-term split. Schedule FA keeps the
quantity-proportional allocation for that group.
Each lot's share of a sale's cash, and of its own cost
when it is partly sold or transferred, is also kept as an exact rational until
the filed rounding (#1241): a third of a cost stays a third. A same-day pro-rata
split keeps each lot's quantity exact too (#1263), and later splits by quantity
read it. Schedule CG and
FSI convert and add those rationals: filed CG consideration and cost and FSI
income are their exact HALF_UP; CG balances subtract the rounded components.
A review amount is one division of its rational. Schedule FA A3 reads the same
rationals (#1243). The FA audit records whether both displayed execution
prices and exact net cash per share were uniform, or whether incomplete price
evidence made that unknown. This allocation does not prove a gross-proceeds/fee
split; section 7 files each lot's allocated `Amount` as its gross.
If exact recorded cash falls outside the displayed quantity-times-price
precision band, every Robinhood command that consumes the sale emits a
privacy-safe source-reference warning. The exact recorded amount remains
unchanged and no fee is inferred.

Statement and activity CUSIPs for the same matched account/ticker/date must
agree. A reviewed private `cusip_transitions_file` may attest one exact
old/new CUSIP pair and effective date per account/ticker when a statement
prints the opposite side of a known transition. The activity CUSIP remains
authoritative; the statement then corroborates symbol only, and every use is
warned. A transition never covers a row that prints no symbol. Real event
history containing both CUSIPs remains unsupported because the canonical-symbol
and corporate-action quantity/basis invariants are not relaxed.

Transfer-in lots are never aggregated with each other or with local
acquisitions because they retain distinct original dates and costs. They rank
by original acquisition date. Lots sharing that date have no source order;
partial consumption requires invariant unit basis or affected CG blocks.
Activity Date is the broker-reported trade date; it is not timezone-shifted
into another Indian FY. CSV/import order is never used as a tax tie-break.

For disposals on or after 23 July 2024, the [Finance (No. 2) Act,
2024][finance-act-2024] gives foreign securities not listed on a recognized
stock exchange in India the general 24-month long-term holding threshold. The
[Income-tax Act, 2025][income-tax-act-2025-holding-period] retains that split.
The retained original acquisition date controls that threshold; destination
account entry does not reset the holding period. Earlier disposals use their
instrument/date-specific statutory threshold; #446 must not back-apply the
24-month general rule.

`REC`, unsupported corporate actions, insufficient pre-period history, or an
inventory mismatch remain hard blockers. Every `REC` row requires one exact
row-bound resolution. A promotional reward resolution supplies documented
exact quantity and USD FMV only after the user attests that value was included
in the applicable Indian return/workpaper. Exact quantity must unambiguously
round-trip to the raw row's displayed precision: rounded to it half-even and
half-up, it gives the displayed number both times. The comparison is
numerical, so an exact quantity written with fewer trailing zeros than the
display, such as `1` for `1.0000`, passes when both quantizations succeed and
give the displayed number. The existing precision limit still applies. The
raw event remains unchanged while resolved inventory is rebuilt from the
override. Estimates and interactive answers are not evidence. Every
account/security/period quantity must reconcile to statement positions under
an explicit display precision band.

A statement equity row is never below zero. A negative `Qty` in Securities
Held in Account or Loaned Securities, or a negative `Shares Lent` in Stock
Lending - Loaned Securities, is unsupported source evidence that a sum could
net against a positive row. Quantity reconciliation refuses it as a fatal
reconciliation error before the row's identity lookup and before the
loaned-partition and securities-total sums. The error names only the internal
account and statement indices and the fixed section, page, row and column.
A zero row is accepted as before. A row with any option field keeps its
existing checks: a partial option identity refuses, and an option position
goes to the option engine.

A transfer display residual within its proved transfer tolerance is audit
evidence only: it never creates destination tax-lot quantity or basis. The
actual source-lot quantity, cost, and original acquisition date transfer
unchanged. A later statement anchor may absorb drift only when that drift
exactly equals the same source-bound transfer residual and the transfer
predates the statement period end; every unexplained or larger drift remains
a lot blocker.

The mirror under-display case may derive a resolved transfer quantity without
config only when the first source anchor after transfer and one unique later
destination disposal residual require the same correction, and only the raw
transfer display precision can explain it. Both transfer legs must be
single-row, the destination must have no prior position, chronology and
account/CUSIP bindings must agree, and the exact correction must
unambiguously round-trip to both displays, numerically as for `REC`. Raw
displays and tolerance remain audit evidence. A second reconciliation is
accepted only when final quantities are unchanged, the drift and residual
disappear exactly, and no other blocker, anchor, residual, or movement
regresses; otherwise the raw blocked result remains authoritative.

### 7. Separate cash truth from filing semantics

Activity `Amount` is the exact signed source cash movement and remains the
cash-ledger value after statement reconciliation.

Statement cash is signed exactly too. Account Activity, Pending Trades and
Deposit Sweep rows reach activity matching, cash reconciliation, transfer
pairing and the A2 `COIN` cash ledger through one signing rule. A negative
debit or credit refuses first, then a row populating both. A nonzero credit is
its printed value unchanged and a nonzero debit the same digits negated, never
rounded, whatever the active decimal context. A lone zero of any sign or scale
is canonical `0`, so a pending zero debit still matches its next-period zero
settlement. A row with neither cell has no signed amount. The activity CSV's
parenthesised `Amount` and a cash transfer's counterpart lookup negate without
rounding as well, so each comparison sees every source digit on both sides.
Two amounts that differ only past the context's precision therefore no longer
match: they are a source discrepancy and refuse.

For an ordinary or managed purchase, the cash paid is the acquisition cost,
including any purchase-side trading charge included in that debit.
Robinhood's [cost-basis help][robinhood-cost-basis] confirms that tax basis
includes applicable trading fees, although it does not define the activity CSV
fields. Average-price allocation for a managed trade does not weaken the cash
identity. The displayed `quantity * price` product is a rounding cross-check,
not a substitute.

For an equity sale, activity `Amount` is carried unchanged as the
source-reported proceeds in the CG computation and its review audit. It is
also the sale's full value of consideration, with 0 transfer expenditure, in
Schedule CG, Schedule FA A2 and A3, and Schedule FSI (owner decision,
2026-11-14, #1383). This replaces #752's manual Schedule CG split and #602's
manual Schedule FA gross proceeds. Robinhood passes FINRA TAF and SEC fees on
sells to customers ([trading fees][robinhood-fees],
[fee schedule][robinhood-fee-schedule]) but does not say whether `Amount` is
before or after them. `Amount` is filed anyway, because:

- Robinhood options come from the same broker, with the same FINRA TAF and
  SEC sale fees and the same `Amount` column. Option gains have been filed
  from the net `Amount` since the owner's decision of 2026-11-13 (#1362).
- No supported source splits a sale's charges out of `Amount`, so manual
  entry has nothing better to enter than `Amount` itself.
- Any error overstates the gain, never understates it. If a charge is inside
  `Amount`, consideration is understated by that charge with 0 expense, and
  the gain is exact. If a charge is collected outside `Amount`, the gain is
  overstated by the charge: slightly more tax, never less.
- The fees are cents per sale. #752's body, citing #602's research, gives the
  published rules: "Robinhood passes the FINRA trading activity fee (TAF) and
  the SEC fee on sells to customers", with "date- and size-based exceptions:
  no TAF on equity sales of 50 shares or less; no SEC fee on equity sales of
  $500 or less; a TAF pause from 1 October to 31 December 2026", and "At the
  December 2025 equity TAF rate, one 100-share execution incurs $0.02".

Each Schedule CG section holding a sale is therefore filled as for any other
source, Table F bands included, and each section sheet's tax position note
states this position. Each A3 sale fragment files its exact share of `Amount`
as gross proceeds. A2 files each in-CY equity sale's `Amount` and each in-CY
option sell-to-close's cash as the account's proceeds nature; an option
blocker refuses the Schedule FA leg instead (section 12). Schedule FSI
follows, because its capital-gains rows are the same CG lines. A lot whose
proven gross differs from its recorded `Amount` is refused, because no path
yet files the difference as transfer expenditure, and a lot with no gross is
refused rather than filed without one. Displayed `quantity * price` remains
an audit cross-check only.
No fee is inferred, added, or subtracted from price residuals, public fee
schedules, execution assumptions, or allocation assumptions. If a future
source proves pre-fee principal or a separate sale charge, file and preserve
it separately; until then, match the exported record exactly.

Robinhood documents commission-free managed execution, aggregate orders, and
average-price allocation. Managed tax-loss harvesting may select specific US
tax lots. Those US selections do not replace this ADR's independently computed
Indian account-wise FIFO or supply unavailable CSV lot identity.

Option price, quantity multiplier, regulatory/exchange fees, exercise,
assignment, expiry, and other lifecycle semantics remain #445 blockers unless
the available activity/statement evidence uniquely proves them.

### 8. Bind annual forms per account

Robinhood [documents][robinhood-tax-documents] separate Form 1099 reporting
for multiple investing and managed accounts. Form 1042-S is delivered
separately and generally contains no proceeds or cost basis. The supplied
parser preserves recipient and summary account references.

Activity and statements own exact income, dates, account attribution, and cash
withholding. Annual forms are secondary whole-dollar income-code/withholding
evidence. A form with one account reference binds directly; no arithmetic-only
cross-account rebinding is allowed. Missing, duplicate, amended, unreferenced,
or contradictory evidence blocks any affected FTC claim.

Form 1042-S is reconciled against US dividends only (#1321). A non-US dividend
never enters a form bucket and never leaves a bound form unreconciled: the
statement Income summaries below prove it, and the `DTAX` pairing proves any
tax withheld from it. NRA tax on a non-US dividend stays a jurisdiction
blocker, and a form whose dividend totals do not reconcile to its matched US
dividends still refuses.

The singleton account reference repeated by the packet summary and every
recipient copy must exactly match one unique reconciled statement account.
Recipient identities and withholding agents must each remain consistent across
accepted forms; recipient country is India and agent country is US.
`registration_lines` remains lossless statement header/contact text, but
Robinhood layouts do not guarantee that it contains a holder name, so it is
never compared to the Form 1042-S recipient name. No fuzzy name or configured
alias substitutes for the exact account-reference binding.

[IRS Publication 1187][irs-pub-1187] requires Form 1042-S income and
withholding fields to be reported in whole dollars. Exact source payments are
summed first and rounded half-up once per printed box for reconciliation only;
the unrounded source totals remain the calculation and audit values alongside
the printed form totals. A missing code-01 bucket can reconcile only when all
account/year interest credits sum to a whole-dollar zero. Those source
references are retained explicitly and no form bucket is fabricated. This
absence rule does not apply to dividends or Stock Lending; code-34 remains
mandatory for substitute-dividend classification. Printed non-whole-dollar
amounts remain blocked.

Rounding to whole dollars can print $0 on a code-06 copy whose exact events are
positive: Box 2 under $0.50 of dividends, or Box 7a under $0.50 of withholding
at a positive chapter 3 rate (#1247). Such a copy passes only the same
once-per-box reconciliation: each matched dividend's withholding must equal the
copy's rate times its gross, rounded half up to cents, or be absent where that
rounds to $0.00. A zero rate with nonzero printed tax stays blocked. A $0 Box 2
also needs a $0 Box 7a and at least one source dividend that the copy's rate
matches, so an empty zero-gross copy beside the copy that covers the dividends
is unsupported form semantics. Codes 01 and 34 still need a positive Box 2.

One semantically valid code-01 bucket with zero rate and tax may remain
unreconciled when its printed gross differs from exact statement interest by
less than one dollar. This exception requires complete account/year source
evidence, no overlapping source blocker, and independent reconciliation of at
least one non-code-01 bucket in the same packet. The code-01 bucket becomes a
typed warning rather than reconciled evidence; its statement interest enters
FSI with zero foreign tax, and no Form 67/TR relief is derived from the form.
The form-above/form-below direction and exact source references are retained
without storing private amounts. Positive tax/rate, larger differences,
multiple code-01 buckets, identity defects, or any residual non-code-01
mismatch remain blockers.

Annual forms are required only for account/calendar years whose 31 December
falls inside the requested filing source period. Extra statements beyond that
period do not change a replay's requirements. The FY-end calendar year therefore
uses complete activity/statements through 31 March without requiring future
April-December evidence or a form issued after that source period. If a packet
for that year is supplied, its exact account/year is re-added to annual-form
scope: full Jan-Dec evidence and normal reconciliation become mandatory. A
bound packet for either FY calendar year is reconciled whether or not that
account-year has income, and packets for other calendar years are not used.
Stock-lending packet eligibility follows the same rule, while substitute-code
contradictions are still scanned before binding.

Every account's statements in the FY's two calendar years are checked against
the recognized income, whether or not the account has any. Each statement's
Income summary must print exactly the recognized Dividends, Interest Earned
and Stock Lending, This Period and Year to Date. A nonzero Capital Gains
Distributions amount in either column is unsupported income, and a statement
straddling a year end blocks reconciliation. An account-year holding a
dividend or interest payment the filing recognizes, Stock Lending in any year,
or a bound Form 1042-S reads every statement of its calendar year. Any other
account-year reads only statements that start on or before the FY end, so an
extra statement after the source period doesn't change the replay's
requirements. A dividend whose security isn't identified yet still counts
toward the Dividends line and the whole-year rule, as it will once identified
(#1507).

FY-end-year dividends and interest, plus their paired withholding debits, are
retained in a privacy-safe pending-form record. Loan fees remain in their
existing unreported-fee audit set and never enter this record. Exact source
withholding may feed Form 67/TR, but the output warns that statement debits,
not Form 1042-S, support that current-year FTC evidence and that later forms
must be checked. US dividends without a paired debit are explicitly warned as
zero-foreign-tax source rows rather than silently treated as annual-form
proved. Schedule FA keeps its independent full-calendar-year A2/A3 coverage
and Dec-31 statement requirements.

Program Bank versus brokerage interest is still decided independently from Form
1042-S coverage by ADR 0005's account-wide exact sweep-ledger pairing. Both
remain foreign interest for OS/FSI and annual-form reconciliation, and ADR-0012
includes both in A2 gross credits. Ambiguous interest still blocks filing
completeness.

### 9. Classify Stock Lending and retain fees without deductions

The exact Robinhood `Stock Lending` / `SLIP` description is a securities-loan
fee. Section 5 of the [Robinhood Stock Lending Agreement][robinhood-stock-lending]
defines a distinct monthly `Loan Fee`; the agreement separately defines
substitute payments in lieu of dividends. The supplied Robinhood activity
format likewise uses `Manufactured Div.` for that separate event. The adapter
therefore does not infer a substitute dividend merely because the payment is
security-keyed.

The [Form 1042-S instructions][irs-1042s] identify income code `34` as a
substitute dividend and codes `33`, `35`, `53`, and `54` as other substitute
payment variants. Any such code is affirmative evidence that contradicts a
population containing only generic `SLIP` fee rows, so it produces a typed
Stock Lending classification blocker before annual-form binding or
reconciliation. Their absence proves neither fee character nor source country.
In particular, the 2026 Form 1042-S is due in March 2027 and cannot be required
evidence for an Indian FY 2025-26 filing. Historical annual forms remain
required for FTC reconciliation where applicable, but are not a prerequisite
for classifying the broker's exact loan-fee event.
For generic fee-only activity, an annual form is contradiction evidence when
issued, not a coverage prerequisite; its absence never substitutes for the
activity/statement/agreement proof.

For AY 2026-27, Schedule OS 1b(ix) is interest and row 1e is `Any other
income`. Each positive generic `SLIP` row therefore becomes a gross
securities-lending-fee receipt in row 1e. [Rule 115(1)][india-rules],
Explanation (2)(c)'s FY-end TTBR applies to every receipt; exact receipt-level
USD and INR amounts remain audit evidence and only the grouped portal amount
is rounded.
Source country is unnecessary for Schedule OS, so an unresolved jurisdiction
may continue to block FSI/TR/Form 67 without suppressing OS. Every `SLIP` row
must produce this income or an explicit blocker; no known subset is filed.

Indian authority does not supply an automatic foreign-country rule for this
fee, and [IRS Notice 2025-63][irs-borrow-fees]'s recipient-residence rule is
US sourcing guidance rather than an Indian Schedule FSI rule. The adapter
therefore requires one exact
`[[robinhood.stock_lending_fee_fsi_sources]]` review per affected account/FY.
The review records a non-India ISO country plus its evidence and reasoning; it
is never inferred from issuer country, ISIN, withholding-agent country, broker
address, or downstream borrower. Missing or partial review coverage remains a
typed jurisdiction blocker; duplicate account/FY reviews reject the config
before computation.

For the supplied FY 2025-26 evidence, the reviewed filing position is United
States. The current agreement (SHA-256
`03a266b176a753eb71a98e417a856cb0e7917de148a3f120c5f86ca65a8eb8b2`)
names RHS as borrower, makes each party liable as principal, and makes RHS the
monthly Loan Fee obligor. This reviewed fee country applies independently of
the lent security's issuer/ISIN country. It does not classify or source a
`Manufactured Div.` or Form 1042-S substitute payment.

`AFEE` and `ADVFEE` remain explicit
`reconciliation-only-no-deduction` records. The parser already proves an ADR
fee's shares-times-rate arithmetic, and both fees already affect cash/A2
reconciliation. Neither fee reduces dividend/Stock Lending income, proceeds,
or basis. [Section 57][india-act] does not support netting ADR fees from
dividends, and the supplied evidence cannot allocate an account-level
management fee wholly and exclusively to one taxable receipt. Declining a
deduction is conservative and does not become an AY blocker; a later claimed
deduction requires new primary authority and explicit allocation provenance.

### 10. Reconstruct only proved long-option semantics

The supported option scenario is long Call `BTO` and `STC` activity only.
Put contract identity is parsed so a typed contract blocker can refuse it
without aborting unrelated schedules. In-CY Put STC cash stays in the A2
review audit with PROCEEDS unresolved (section 7), but blocked Put contracts
produce no option tax output.
The account-local contract key is account, symbol, expiry, right, and strike.
Activity Date owns acquisition, disposal, FIFO, and FY attribution; settlement
date owns statement-position anchors. CSV order is never a tax tie-break.
Same-day mixed directions or differing per-unit cash remain blocked.

Statement position arithmetic and every activity cash/display band prove the
standard `100` multiplier. Exact signed cash remains the basis/proceeds truth.
The difference from displayed `quantity * price * 100` remains a precision
residual, not a fabricated regulatory or exchange fee. Put, short, expiry,
exercise, assignment, cancellation, adjusted-contract, transfer, negative
inventory, and unexplained statement-position semantics remain blockers.

`[[robinhood.option_classifications]]` binds `account_number`,
`financial_year`, `fema_risk_acknowledged`, `treatment`, `source`, and
`reason`. F&O processing refuses unless the acknowledgement is explicitly
`true` and treatment is selected; treatment is never inferred from frequency.
The acknowledgement enables tax reporting only. RBI's LRS Master Direction
paragraph 5 disallows remittances in the nature of margins or margin calls to
overseas exchanges/counterparties, but the source evidence does not establish
whether a trade was LRS-funded or otherwise FEMA-compliant. Every processed
F&O run therefore emits a non-quietable FEMA/LRS warning. Section 43(5)(d)
applies only to eligible derivatives on a prescribed/notified recognised stock
exchange; the supplied U.S.-exchange trades do not qualify automatically. A
reviewed `non-speculative-business` decision must therefore rely on another
statutory basis outside the source.
The option workpaper preserves the configured `source` and `reason` as
user-stated classification inputs, not as proof, and emits a separate
account-indexed section 43(5)/73/80 warning.

A reviewed capital treatment produces exact net residual-other-assets Schedule
CG and CFL evidence under Rule 115(1), Explanation (2)(f): ITR-2 A5(e)/B8(e), or
ITR-3 A6(g)/B9(e). It remains manual because the existing CG models require an
equity ISIN. `schedule-cg.xlsx` covers equities only, so a section
holding an option capital gain carries a note naming it for manual entry from
`robinhood-options.csv`, and `robinhood-cg` reports `manual` (exit 3) unless
an existing refusal applies (#1322). It also requires manual Schedule FSI head `CG` and
Schedule FA Table D. Table D applies even when the contract closed before 31
December because it covers assets held at any time in the calendar year.

A capital slice closed before 23 July 2024 is blocked
(`pre-cutover-holding-rule`), whatever its holding period. Until that date,
[section 2(42A)][income-tax-act-1961-holding-period]'s general threshold was 36
months, and none of its shorter-period provisos covers a U.S.-listed option. The
[Finance (No. 2) Act, 2024][finance-act-2024] made 24 months the general
threshold from that date. Only later slices are bucketed by the 24-month rule;
earlier ones are not re-bucketed under the old one.

A reviewed business treatment uses Rule 115(1), Explanation (2)(c)'s FY-end TTBR
and produces exact matched premium paid, premium received, and net P&L. It
requires ITR-3. A complete positive net P&L is emitted under Schedule FSI
`Business or Profession`; zero or a loss creates no FSI row because the
official schema accepts only non-negative foreign income. Form 67 and Schedule
TR receive no option row: the supported option scenario carries no foreign tax.

The official AY 2026-27 utility maps a no-books speculative result through P&L
65i-iv, BP 2a/B39-B42, and current-year CFL column 6. ICAI's Revised 2026
[Guidance Note][icai-tax-audit-2026] leaves foreign speculative options between
paragraph 5.11(a)'s differences-only rule and paragraph 5.11(b)'s
option-premium wording; the same text appears in ICAI's official
[Revised 2025 PDF][icai-tax-audit-2025]. For the supported closed long-only
scenario, the user reviewed and selected
`icai-5.11a-differences`: turnover is the sum of absolute exact cash P&L by
matched slice. This is also the figure independently supported by 5.11(b)(i);
it does not reconstruct gross premium or fees from rounded `Price`. The
election is accepted only when config also records no books, no other
speculative business, Nature of Business code, exact external expenses, and BP
additions/deductions. The reviewed code is `21010` (Futures and Options
trading), which describes the activity; its speculative treatment remains the
separate section 43(5) decision. Missing facts, mixed business buckets, another
option lifecycle, or an FY other than 2025-26 remain manual.

P&L 65ii is the sum of favourable differences. P&L 65iii is the sum of
unfavourable differences plus reviewed external expenses; 65iv is their
difference. `external_expenses_inr` is an accounting expense included in 65iii;
BP B40/B41 are later tax adjustments wholly attributable to this foreign
option business and must not repeat that expense. Each portal component is
rounded to whole rupees before dependent fields are derived. Turnover is
rounded favourable plus rounded unfavourable differences, 65iv = 65ii - 65iii,
and B42 = B39 + B40 - B41, so every formula remains exact after portal
rounding. The full-precision inputs stay in option audit rows. Schedule FSI
uses the reconciled whole-rupee positive B42, which can differ by ₹1 from
rounding exact net P&L directly; a negative B42 creates no FSI row and flows to
CFL.

Option shares are exact until the filed rounding (#1245). A sale that takes `q`
of a purchase's `Q` contracts costs exactly `C × q / Q` of its cash `C`, a
sale's proceeds split across purchases the same way, and an open remainder
keeps the rest, so the shares add back to the cash exactly. The FY business
totals, the capital-gains lines and their INR conversions keep these rationals
until filing. Gross profit and unfavourable differences file their exact
HALF_UP; the no-books filing derives turnover, expenditure, net income, BP and
CFL from those whole rupees and the rounded reviewed config amounts. Schedule
FSI uses that BP income when a filing is available, otherwise the exact net
P&L. This replaces #1148's cumulative rounding of each share to a millionth
of a dollar, which no ADR recorded. That rounding cancelled only within one
total at one rate, so a
purchase split across financial years, or capital-gains lines at different
monthly rates, could file a rupee short of a total on 50 paise. A workpaper or
audit amount shows its exact value where a Decimal of at most 60 digits holds
it, else that value cut toward zero with at least 60 digits, widened if needed
to retain a tenth, which rounds to the same whole rupees. An amount or rate
past the 1,000-digit cap of exact arithmetic refuses with the fixed
option-total message naming the activity row.

The AY utility auto-populates BP A1 profit before tax from the applicable P&L
items, including presumptive-profession row 62ii and speculative row 65iv. The
option workpaper therefore emits a guard note rather than a partial A1 amount:
65iv must already be present in P&L and must not be added to A1 again.

The option workpaper also reports that its no-books fields do not determine
books-of-account or tax-audit applicability. Section 44AA(2) depends on an
individual/HUF's business income or turnover in the three preceding years, or
likely current-year figures for a newly set-up business. Section 44AB(a) uses
current total business turnover and raises its base threshold only when
aggregate cash receipts and cash payments each stay within 5%. The adapter
knows only this year's Robinhood activity, not other businesses, preceding-year
figures, new-business status, or aggregate cash percentages.

Different business buckets remain manual and are never netted across
speculative/non-speculative treatments. Schedule FA Table G expressly excludes
business/profession income, and Table D excludes stock-in-trade/business
assets, so the option itself has no additional Schedule FA table: only the
custodial account remains in A2. An open business position at FY-end blocks a
complete business-income amount because the source does not prove the
closing-stock/accounting policy.

Option values observed in statements are components already included in the
reported account NAV; they are cross-check anchors, never additive A2 value.
Positive in-CY STC cash stays in the review audit and adds to the account's
A2 PROCEEDS (section 7); an option blocker refuses the Schedule FA leg instead
(section 12). Options never enter A3. Monthly/period-end statement values do
not prove a daily price series or the A2 peak.

### 11. Build two account-bound A2 rows and transfer-safe A3 rows

Each configured and statement-proved brokerage account produces one A2 row.
The Dec-31 closing is statement brokerage cash plus Deposit Sweep cash plus
total securities. The parser and reconciliation layers independently prove the
position total, both cash locations, and their aggregate. Statement option
values are already inside total securities and are never added again.

The daily A2 peak is reconstructed automatically in native USD from one
synchronized end-of-day series:

- economic account cash keeps brokerage-to-sweep transfers internal, so
  unsettled receivables/payables and swept cash are included once;
- equity quantities use the reconciled CUSIP-aware activity ledger and
  statement anchors;
- Yahoo closes are normalized back to each date's contemporaneous split basis,
  exactly: a close or high with no exact decimal within a unit price's digit
  limits on that basis, as under most 1-for-3 splits, refuses the fetch
  (#1307);
- supported standard Calls use Massive `adjusted=false` qualifying-trade
  closes; statement option marks are reconciliation anchors only; and
- weekends and known closures carry the preceding session close, while a
  missing close on an actual held trading session refuses.

The maximum is selected in USD, with exact ties resolved to the earliest date.
Only that selected USD value is converted using that date's Schedule FA TTBR.
The warned `schedule_fa.peak_currency = "inr"` override instead maximizes the
daily converted account value. The engine never sums independently maximized
A3 positions. Statement USD opening/closing values are retained as audit
floors, not replacement prices. Broker option-mark differences remain audit
evidence rather than blockers.

Any incomplete cash, identity, quantity, split, equity-price, option-price, or
FX evidence hard-fails the Schedule FA leg. There is no manual A2 peak: a
config that sets the retired `[[robinhood.a2_peak_overrides]]` table is refused
on load (#628). `--no-fetch-prices` means cache-only automatic computation, not
a manual fallback.

A2 gross credits are reconstructed on the Schedule FA calendar year from raw
payment records, not FY-filtered filing accessors. Dividends, brokerage and
Program Bank interest, and substitute-dividend Stock Lending remain separate
natures. Each in-CY equity sale's `Amount` and option sell-to-close's cash
adds to the proceeds nature, and its exact cash stays in the review audit
(section 7); an option blocker refuses the Schedule FA leg instead. ADR-0014
repeats the complete account row once per exact credit nature. A blocked or
ambiguous nature receives no partial total and keeps A2 manual.

*Corrected 2026-11-15:* #1384's wording said an option blocker left A2
proceeds unresolved; an option blocker has refused the Schedule FA leg since
#497.

A3 follows economic lots rather than account rows. An internal transfer keeps
one original lot ID, acquisition date, and cost across both accounts. Transfer
slices are not disposals. In-CY sale slices become closed fragments; sale
slices after Dec 31 and final open fragments are aggregated by original lot ID
as one Dec-31 holding. Quantity, cost, allocated statement market value, and
gross credits conserve exactly. Each row's carried cost rational, its share of
a statement total split across open fragments by quantity, and its share of
each credit split across holders by quantity stay exact rationals until the
row's filed rounding (#1243): these initial values, closing balances and gross
credits file the exact HALF_UP of the rational times the TTBR, and a review
amount is one division of its rational. Costs without a separate rational keep
the existing Decimal initial-value product (#1250). Statement-total and credit
shares split by a lot's exact quantity after a same-day pro-rata sale, and its
peak converts at that exact quantity (#1263). A share whose operand has
more than 1,000 digits, or a digit more than 1,000 places from the units digit,
refuses.

Dec-31 equity closing uses the shared statement-first
`resolve_dec31_close` contract. Daily peak uses the shared A3 price/TTBR
calculation. Robinhood closed rows file their exact allocated sale `Amount`
at the December 31 TTBR as A3 gross proceeds (section 7), with no inferred
fee adjustment.
Dividends are attributed to fragments held in the account that received the
credit at the end of the day before the record date, while the Activity Date (the canonical payment date, ADR 0005) owns
CY inclusion; the description's `P/D` only groups one dividend's payments, and the calendar-year closing
date owns Schedule FA income FX. That rule assumes US T+1 settlement, so an
in-CY dividend whose record date precedes May 29, 2024 refuses A3 attribution. Stock Lending uses its activity
date by default, again only for fragments held in the account that received
it. Each credit is split exactly by the quantity each fragment held in that
account (#1318). A part of a lot
moved by an internal transfer counts for the account that held it that day,
traced back through the lot's holding intervals; a part whose account that day
the transfers can't settle blocks A3 credit attribution. If a generic monthly
Loan Fee posts after the earning lot closed, so the receiving account holds no
fragment of the security on the fee date, a fallback may use the exact per-account statement Loan Activity
ledger: the preceding month's opening lent balance must match the prior
month-end snapshot, every lend/return must conserve a non-negative balance,
and exactly one economic fragment must be capable of supplying every lent
share. Carried loan balance may remain through the sale's proved settlement
date, but no new lending may begin after trade-date disposal. The fee payment
date still owns CY inclusion and Schedule FA FX. Missing or ambiguous loan
evidence remains an A3 credit-attribution blocker. Transfers do not interrupt
beneficial ownership. Options never enter A3.

Program Bank presence is retained as A2 audit evidence and creates no A1 row or
manual gate. Capital-option treatment creates Table D manual work. Business
option treatment creates no additional Schedule FA work beyond A2.

The shared CBDT fragment input carries an array of independently verified,
account-bound A2 rows. Every A2 peak and account cross-check travels with its
own row; no global peak or verification flag may be reused across accounts.
Actual writer invocation, stale-output clearing, and exit-status rendering
remain #448.

### 12. Discover and orchestrate one complete source bundle

The production bundle has `activity/`, `statements/`, and optional
`tax-forms/` directories. Exactly two `[[robinhood.reports]]` entries bind
safe relative filenames to selected report ranges. Filenames never assign an
account; #470's evidence-derived bijection remains authoritative. Each report
starts no later than its reconciled account opening date and ends on an issued
statement period boundary. Statement history begins with the first-ever
unavailable-opening statement and covers the requested CY/FY union.

Crypto and Spending are separate products outside the supported bundle.
Discovered Crypto statements are counted and warned as out of scope; neither
product is parsed or treated as a brokerage completeness gate.

Identical documents may be hash-deduplicated. Non-identical duplicate statement
periods refuse unless `[[robinhood.corrected_statements]]` explicitly selects
one safe relative file and names every superseded original. Filename wording
alone never establishes correction precedence. Amended Form 1042-S remains a
parser refusal until its exact semantics are evidenced.

Inventory routing is the exact global event partition and owns enum
exhaustiveness. Income, CG, options, and transfer layers are exact only over
their respective kinds; legitimate cross-layer reuse is not a duplicate
disposition. The CLI exposes `robinhood-cg`, `robinhood-income`,
`robinhood-fa`, `robinhood-options`, and `robinhood-all`.

Every leg pre-clears only its exact CSV/XLSX targets and rolls them back as a
unit on write failure. CBDT JSON uses provenance-aware clearing and never
deletes a foreign or hand-merged return. Before clearing, a leg refuses if any
target is the config, a bundle file, or a file the config names; it loads the
config for that check, so a config that cannot load refuses with nothing
cleared (#673). A dedicated output directory per
source is required. Exit 0 is filing-complete, exit 3 preserves valid output
with explicit manual work, and exit 4 is an evidence/domain refusal. Option
blockers fail FA. Every kind but a business position still open at FY-end
also fails the option leg when there are option trades; when blockers of
those kinds are their only problem, `robinhood-cg` and `robinhood-income` run
on their own still write their output and exit 3, naming them. A blocked FTC
leg exits 4, and `robinhood-income` removes Schedule OS with its other output.
`robinhood-all` is all or nothing: when any leg ends in error, it removes
every output the run wrote except the identity guidance file, and leaves a
`schedule-fa.json` it didn't write untouched. That includes the FA leg that
any option blocker fails, a blocked FTC leg, and a failed or incomplete
automatic Schedule FA JSON. Capital-option income or an incomplete business
reconstruction prevents the income/FTC leg from claiming Schedule FSI
completeness; a complete business reconstruction does not.

*Amended 2026-11-15:* this section said a failed run kept its valid outputs;
since the owner's decisions of 2026-11-01 (#719, PR #725; #745, PR #769) it
removes them.

### 13. Keep blockers narrow and explicit

| Unresolved fact | Result |
|---|---|
| Missing/ambiguous report-to-account assignment | Refuse the bundle |
| Negative statement equity position or Stock Lending shares-lent row | Refuse the bundle |
| Required post-opening period outside report/statement coverage, or unproved pre-opening span | Block affected output |
| Same-day transfer/disposal order is unproved | Block affected CG and A3 lots |
| Partial differing-cost same-day lot consumption | Block affected CG and A3 lots |
| Same-day sales at differing cash per share over short- and long-term lots | Block affected CG; A3 keeps the quantity split (#1540) |
| Unresolved `REC` | Hard-fail with one required row-bound config block per receipt |
| Unsupported corporate-action basis/quantity transform | Block affected security |
| Sale gross/expense split unavailable | File recorded `Amount` as Schedule CG consideration with 0 transfer expenditure (section 7) |
| Option multiplier, ordering, position, or lifecycle unavailable | Block affected option output |
| Option tax treatment absent for the account/FY | Preserve reconstruction; block tax disposition |
| Option capital slice closed before 23 July 2024 | Block option tax output and Schedule FA |
| Business option remains open at FY-end | Preserve realized evidence; block complete BP/CFL amount |
| Source payment identity, pairing, or classification blocked | Leave A2/A3 gross credits manual; never file zero or a known subset |
| Sale gross/fee split unavailable | File exact recorded cash as A2/A3 proceeds |
| Program Bank present during CY | Include aggregate sweep cash and interest in the account's A2 row |
| Automatic A2 price/reconstruction evidence incomplete | Hard-fail Schedule FA |
| A3 Dec-31 statement/market mismatch | Block the affected open economic lot |
| Management-fee tax treatment unavailable | Reconciliation-only; no deduction |
| Missing account/FY loan-fee FSI source review | Block FSI/TR/Form 67; the run exits 4 and removes Schedule OS with its other output (section 12) |
| Form 1042-S substitute-payment code contradicts generic `SLIP` fees | Block Schedule OS and FTC classification |
| Unresolved or residual annual-form coverage | Block affected income/FTC output |
| Program Bank cash present | Apply ADR-0012 A2-only treatment |

An unaffected schedule's output is kept only when the run exits 3; an exit-4
run removes every output it wrote (section 12). No blocker becomes a warning
followed by exit 0.

## Consequences

- #440 reopens for the managed event variants and a privacy-safe audit of the
  supplied activity reports.
- #469 replaces the singular Robinhood config before security, income, FA, or
  orchestration work.
- #470 owns report/account assignment and the two primary activity/cash runs.
- #454 becomes optional support-report evidence and no longer blocks lot work.
- #455 closes because no bulk confirmation source is supplied or documented.
- #442 builds two account-wise inventories and preserves transfer provenance.
- #443 reconciles annual forms per account.
- #444 owns management-fee tax treatment in addition to Stock Lending/ADR fee.
- #445 uses only proved option semantics.
- #446 blocks only genuinely unresolved CG fields/results.
- #447 emits one A2 row per account.
- #448 discovers and proves one complete multi-account bundle.

This design accepts more source evidence without weakening any trust boundary.
It also exposes unavoidable legal/source ambiguity instead of pretending an
unavailable document or undocumented CSV field settled it.

[robinhood-reports]: https://robinhood.com/us/en/support/articles/finding-your-reports-and-statements/
[robinhood-tax-documents]: https://robinhood.com/us/en/support/articles/about-tax-documents/
[robinhood-cost-basis]: https://robinhood.com/us/en/support/articles/cost-basis/
[robinhood-fees]: https://robinhood.com/us/en/support/articles/trading-fees-on-robinhood/
[robinhood-fee-schedule]: https://cdn.robinhood.com/assets/robinhood/legal/RHF%20Fee%20Schedule.pdf
[robinhood-stock-lending]: https://cdn.robinhood.com/assets/robinhood/legal/Robinhood_Stock_Lending_Agreement.pdf
[robinhood-strategies]: https://robinhood.com/us/en/support/articles/strategies-faq/
[robinhood-ram-brochure]: https://cdn.robinhood.com/assets/robinhood/legal/RAM_Brochure_and_Brochure_Supplements.pdf
[irs-1042s]: https://www.irs.gov/instructions/i1042s
[irs-borrow-fees]: https://www.irs.gov/pub/irs-drop/n-25-63.pdf
[irs-pub-1187]: https://www.irs.gov/pub/irs-pdf/p1187.pdf
[india-rules]: https://www.incometaxindia.gov.in/pages/rules/income-tax-rules-1962.aspx
[india-act]: https://www.incometaxindia.gov.in/pages/acts/income-tax-act.aspx
[robinhood-advisory-agreement]: https://cdn.robinhood.com/assets/robinhood/legal/ram_advisory_agreement.pdf
[circular-704]: https://incometaxindia.gov.in/w/704-circular-no.-704-dated-28-4-1995
[circular-768]: https://incometaxindia.gov.in/w/768-circular-no.-768-dated-24-6-1998-1
[prannoy-roy]: https://indiankanoon.org/docfragment/51561346/
[prannoy-roy-appeal]: https://indiankanoon.org/doc/30582871/
[depositories-act]: https://indiankanoon.org/doc/1167929/
[finance-act-2024]: https://archive.org/details/in.gazette.central.e.2024-08-16.256436
[income-tax-act-1961-holding-period]: http://web.archive.org/web/20240419165835/https://www.indiacode.nic.in/bitstream/123456789/2435/1/a1961-43.pdf
[income-tax-act-2025-holding-period]: https://indiankanoon.org/doc/50400663/
[fa-fx-policy]: ../../src/ibkr_fa/fx/policy_fa.py
[cg-fx-policy]: ../../src/ibkr_fa/fx/policy_rule115_capital_gains.py
[icai-tax-audit-2025]: https://resource.cdn.icai.org/87317dtc-aps1808gn-tax-audit2025.pdf
[icai-tax-audit-2026]: https://publication.icai.org/publication/968
