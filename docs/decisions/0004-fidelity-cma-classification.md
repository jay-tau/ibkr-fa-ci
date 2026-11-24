# ADR 0004: Fidelity Cash Management Account source classification

- **Status**: Accepted
- **Date**: 2026-07-15
- **Deciders**: jay-tau
- **Issue**: #387 (tracker), #391 (this ADR)
- **Amended**: 2026-11-01 (#702) to let `no-refund-claim` years claim FTC;
  2026-11-01 (#622) to set the SPAXX A3 peak window and basis;
  2026-11-07 (#917) to put SPAXX capital gains in Schedule FSI;
  2026-11-12 (#1244) to keep SPAXX cost and proceeds shares exact;
  2026-11-12 (#1251) to accept a debit-card purchase dated in the previous month;
  2026-11-12 (#1274) to keep interest and card purchase dates inside the
  account's opening statement

## Context

A Fidelity Cash Management Account (CMA) bundle is private and gitignored.
This ADR records only the supported layouts and requirements: no source
filenames, account identifiers, holder details, private addresses, amounts,
report counts, or date spans.

The supported bundle has three document layouts:

1. **Monthly Investment Report** - consecutive monthly statements. The first
   report may be a partial month with no carried account value; every later
   report covers a full month. The layout
   contains account summary, holdings, core-fund activity with a running
   balance, deposits/withdrawals, income and withholding, and optional debit
   card activity. It has two supported valuation variants:
   - account value equals SPAXX holdings; or
   - net account value includes both SPAXX holdings and a separately stated
     Fidelity Free Credit Balance (FCB).
2. **Year-End Investment Report** - one calendar-year aggregate report with
   year-end holdings and annual flows, but no transaction ledger. It overlaps the
   December monthly report deliberately and is not a duplicate.
3. **Form 1042-S packet** - one tax-year packet containing repeated
   recipient-copy forms and the fields needed to identify, amend, and reconcile
   each form.

The supported core position is Fidelity Government Money Market Fund
(SPAXX). A monthly statement may also show a non-zero FCB, separate from
SPAXX. The statement disclosure says FCB is payable on demand by Fidelity and
distinguishes it from money-market and FDIC core positions. An FDIC Deposit
Sweep, Program Bank balance, non-core holding, or general securities-trade
section is outside the supported layout. Generic FDIC text in the PDF
boilerplate is not evidence of a bank deposit.

| Evidence | Monthly report | Year-end report | Form 1042-S |
|---|---|---|---|
| Identity and account registration | Present | Present | Recipient and withholding-agent fields |
| Statement/tax period | Exact period | Calendar year | Tax year |
| Beginning/ending account value | Aggregate account/net account value | Present | Absent |
| Holdings and core position | SPAXX position and running core ledger; FCB in one valuation variant | SPAXX year-end position | Absent |
| FDIC sweep / Program Banks | Not present | Not present | Not present |
| Trades | SPAXX buys, sells, and reinvestments only | Annual aggregates only | Absent |
| Deposits, withdrawals, debit card | Detail sections when active | Annual aggregates | Absent |
| Transfers and fees | No dedicated section observed; only explicit labeled rows are supportable | Annual aggregates | Absent |
| Dividends/interest and withholding | Transaction detail and summaries | Annual summaries | Gross income, income code, tax rate, and withholding |
| Exact account opening date | Absent | Absent | Absent |
| Exact daily A2 peak | Absent | Absent | Absent |

The account statements say brokerage services are provided by Fidelity
Brokerage Services LLC (FBS), while the account is carried and custodial
services are provided by National Financial Services LLC (NFS). Fidelity's
current customer agreement likewise identifies FBS as broker and NFS as its
clearing firm.

The governing classification sources are:

- [Income-tax Rule 114F][rule-114f], whose explanations distinguish a
  depository account maintained in banking business from a custodial account
  that holds financial assets for another person. Its definitions govern Rules
  114F-114H; they are supporting taxonomy, not a binding Schedule FA mapping.
- The vendored official [ITR-2 AY 2026-27 schema][itr-schema-source], which has
  separate required objects for A1 `DetailsForiegnBank`, A2
  `DtlsForeignCustodialAcc`, and A3 `DtlsForeignEquityDebtInterest`.
- The official [Schedule FA/FSI guide][fa-guide], which describes A2 as
  accounts maintained with foreign custodians and A3 as investments in foreign
  equities and debt instruments, held at any time during the relevant calendar
  year.
- Fidelity's [CMA overview][cma-overview], which calls a CMA a brokerage
  account and says Fidelity is not a bank.
- Fidelity's [brokerage protection FAQ][brokerage-faq], which says FBS and NFS
  are the brokerage entities and that a money-market fund held in a brokerage
  account is a security.
- The [Fidelity Account Customer Agreement][customer-agreement], pages 1-2 and
  4, 7-8, which identifies FBS and NFS, calls NFS the clearing firm, and
  distinguishes a money-market core, bank sweep, and FCB. The 2026 agreement
  corroborates rather than replaces the contemporaneous statement disclosure.
- The US [Customer Protection Rule][sec-fcb], which defines an FCB as a
  broker-dealer liability subject to immediate cash payment on demand.
- The [SPAXX summary prospectus][spaxx-prospectus], dated June 29, 2026, which
  identifies the held interest as shares of Fidelity Government Money Market
  Fund.
- Fidelity's [2026 FDIC Sweep disclosure][fdic-disclosure], which distinguishes
  Program Bank Deposits from an alternative money-market-fund core.

## Decision

### 1. Classify only what the supplied source proves

| Component | Schedule FA treatment | Reason |
|---|---|---|
| Fidelity CMA brokerage account | **A2 - Foreign Custodial Account** | NFS carries the account and provides custodial services; the account holds a financial asset for its owner. |
| SPAXX shares | **A3 - Foreign Equity and Debt Interest** | SPAXX is a money-market mutual fund security, not a bank deposit. The A3 entity is the fund, not FBS, NFS, or its investment adviser. |
| Fidelity Free Credit Balance | **A2 only - account cash; no A1/A3 row** | FCB is operational brokerage cash payable on demand, not an evidenced Program Bank deposit or an investment in a debt instrument. It has no truthful A3 acquisition date, initial investment, or sale/redemption proceeds. |
| FDIC Deposit Sweep / Program Bank deposits | **Unsupported; emit no A1 row** | The supported layout has no such position. An actual Program Bank deposit may belong in A1, but the statements prove no bank identity, account identifier/opening date, daily peak, closing balance, interest, or A1/A2 allocation. A future observed sweep must refuse until primary evidence and CA or official guidance settle them. |
| Any other Fidelity instrument or layout | **Unsupported** | Do not generalize from SPAXX. Insurance, derivatives, other funds, securities, or statement versions require instrument-specific evidence and classification. |

The A2 financial institution is **National Financial Services LLC**, the
carrying/custodial firm. "Fidelity Investments" is not an acceptable
placeholder. FBS and NFS labels must cross-check across the source bundle, and
the filing address must come from verified public/configured provenance.

The A3 entity is **Fidelity Government Money Market Fund**. Its identity and
address must cross-check against a current official prospectus; the fund
adviser is not substituted for the fund.

A2 and A3 deliberately overlap: A2 discloses the account and A3 discloses the
SPAXX shares held inside it. Do not add their balances or credits when testing
thresholds or taxable income. Do not invent an A2 value net of A3.

The broker-reported aggregate account value is the A2 valuation. For the
observed FCB variant, `Net Account Value = Free Credit Balance + Market Value
of Holdings` is a required document-internal cross-check, not a universal
reconstruction method. FCB is never netted out of A2.

FCB's transient existence affects A2 peak/closing fields, not table
classification. Use the broker's aggregate value at each instant; never add
independently determined FCB and holdings peaks from different dates. A zero
Dec-31 FCB changes neither the requirement to include it in an earlier A2 peak
nor the conclusion that it is not an A3 instrument.

### 2. Do not infer missing filed facts

- A first statement date is not normally account-opening evidence. For a
  CMA bundle, it may be used only under the explicit user-attestation
  contract: the bundle starts with that partial statement, both account and
  SPAXX beginning values are absent (no carried balance), every later statement
  is gap-free, and the CLI always warns the filer to verify the legal date.
- Monthly statements do not provide one timestamp/order across their separate
  activity sections, so they cannot prove one exact intraday A2 peak. They do
  enumerate every supported dated posting: reconstructed FCB and SPAXX
  quantities must reproduce every month end. With fetched daily NAV and TTBR,
  the source can prove a lower bound (maximum start/end-of-day value) and a
  conservative upper bound (same-day positive postings before debits). No bound
  is filed without an explicit runtime attestation; SPAXX is never assumed to
  remain exactly USD 1.
- SPAXX A3 acquisition dates come from actual purchase/reinvestment
  transactions, not the statement start date. Daily A3 peak value requires
  exact holdings, official daily NAV, and daily TTBR evidence.
- The SPAXX A3 peak uses the shared close-basis rule (`resolve_fa_a3_peak`)
  with two CMA settings, chosen by the owner on 2026-11-01 (#622):
  - **A peak only the disposal day could set is not filed.** The close window
    includes a closed lot's disposal day. If the selected peak (the earliest
    maximum, compared in the configured peak currency) falls on that day, the
    A3 peak is blank and the run exits 3. Lots bought and redeemed on the same
    day, and lots redeemed on 1 January, are therefore blank. A disposal-day
    tie keeps the earlier winner. Every filed peak is the same whether or not
    the disposal day counts.
  - **`schedule_fa.peak_basis = "high"` is refused** with
    `FidelityCmaFaError` before any output is written.

  No fetched Indian Schedule FA text settles either point. The [A.Y. 2026-27
  ITR-2][itr2-ay2026] (Notification No. 46/2026, PDF p.27) heads Table A3
  "Details of Foreign Equity and Debt Interest held (including any beneficial
  interest) in any entity at any time during the calendar year ending as on
  31st December, 2025" and labels the column "Peak value of investment during
  the period". The [ITR-2 instructions for AY 2023-24][itr2-instructions], the
  latest official edition found, ask for the "peak value of investment during
  the accounting period" (p.38), say "the period means the period comprising:-
  (a) from 1st January 2022 to 31st December 2022 in respect of foreign assets
  or accounts etc. held in those jurisdictions where calendar year is adopted
  as basis for the purpose of closing of accounts and tax filings", and convert
  at the TTBR "as on the date of peak balance in the account or on the date of
  investment or the closing date of the accounting period" (p.40). The
  [CBDT schema][itr2-schema-ay2026] gives `PeakBalanceDuringPeriod` only an
  integer type and bounds. None of these says whether the disposal day counts
  or at what time of day the peak is valued; the end-of-day "while held"
  window is a project convention.

  [17 CFR 270.22c-1(a)][cfr-22c-1] bars redeeming "except at a price based on
  the current net asset value of such security which is next computed after
  receipt of a tender of such security for redemption", and the ledger checks
  each in-year execution price against that day's close lookup. Whether the
  value at which a holding is redeemed enters the A3 peak is not settled, so a
  peak that depends on it is left for manual entry. Nor do the texts prescribe
  a daily close or an intraday high. 22c-1(b)(1) has NAV computed "no less
  frequently than once daily, Monday through Friday, at the specific time or
  times during the day that the board of directors of the investment company
  sets", so nothing establishes what a Nasdaq `high` means for SPAXX.
- No general security parser is claimed: only the SPAXX core ledger is
  supported.

### 3. Source roles and deduplication

Monthly statements are primary. The year-end report and Form 1042-S are
cross-checks and never replace transaction-level statement facts.

- A dividend reinvestment is one income credit plus one SPAXX purchase; the
  purchase is not counted as a second income event.
- `Credit Interest` is interest on FCB: it contributes to A2 gross interest and
  the OS/FSI interest path, but not to SPAXX dividend totals or the SPAXX A3 row.
- Moving principal between FCB and SPAXX is an internal account movement, not a
  second income event or gross credit.
- Income-summary, year-end, and tax-form totals are corroboration, not new
  transactions.
- Form 1042-S copies are grouped by unique form identifier, copy designation,
  amendment flag, and amendment number. Recipient copies are never summed.
  Ambiguous amendment precedence refuses.

### 4. Minimum complete input

| Downstream result | Minimum evidence |
|---|---|
| Parse a requested window | Every monthly report from account opening through the window end, with no date gap. A partial first statement may supply the warned/attested opening date only when both carried account and SPAXX values are absent. |
| Schedule FA for a calendar year | Statements from a no-carried-balance opening through Dec 31, the year-end report, fetched daily SPAXX NAV, and an explicit lower/upper A2 peak-bound attestation when automatic peak output is requested. |
| OS / FSI / TR / Form 67 for an Indian financial year | Every monthly report covering the FY, transaction-level distribution/withholding pairing, and each overlapping calendar-year Form 1042-S when available. Calendar-year form totals never become FY amounts directly. Schedule FSI includes the FY's SPAXX capital gains, so an FY with a SPAXX redemption also needs the CG / CG-LTCG evidence below. |
| CG / CG-LTCG | Complete acquisition-to-disposal history and Indian-basis evidence for every redeemed lot. SPAXX redemptions are FIFO-matched from the recorded purchase and redemption amounts and filed as STCG by `fidelity-cma-cg` and in `fidelity-cma-income`'s Schedule FSI; an in-FY lot held over 24 months refuses. USD 1 basis or zero gain is not assumed. *(Shipped 2026-07-17 by #400 for #389; FSI 2026-11-07 by #986.)* |

A calendar year is complete only when its monthly sequence runs from the
no-carried-balance first report through December and the year-end report is
available. The first report's date is used as a warned, user-attested opening
date; A2 peak output uses the explicit lower/upper bound contract above. A
partial calendar-year sequence must not be accepted as a complete CY bundle.

### 5. Required cross-checks

Every downstream parser/pipeline must fail closed on:

1. account identity, registration, FBS/NFS role, currency, or statement-layout
   mismatch;
2. a gap or overlap in monthly periods, except the separately identified
   year-end aggregate;
3. adjacent monthly ending/beginning account values that do not match;
4. failure of the signed Account Summary components to reproduce Ending
   Account Value;
5. for the no-FCB valuation variant, either Account Value or the final SPAXX
   core balance failing to match Total Holdings;
6. for the FCB valuation variant, Net Account Value failing to equal FCB plus
   Market Value of Holdings, or either Total Holdings or the final SPAXX core
   balance failing to match Market Value of Holdings;
7. a detail-section total not matching its deposits, withdrawals, core
   activity, debit-card activity, income, or withholding rows;
8. December holdings/value not matching the year-end report;
9. monthly calendar-year flows, distributions, or withholding not matching
   the corresponding year-end and deduplicated Form 1042-S totals; or
10. an unrecognized section, instrument, tax-form copy/amendment, correction,
    reversal, transfer, currency, or structural FDIC Deposit Sweep / Program
    Bank position. Generic disclosure prose is not a position marker.
    In the read regions listed in the parser's module docstring, each line must match a supported row, fixed shape or page furniture (`src/ibkr_fa/parser/fidelity_cma.py`, #1095).

A debit-card row's posting date must be in the statement period, and its
purchase date on or before the posting date. A credit-interest date and a
card purchase date must be in the period or, on a statement that isn't the
account's opening statement, in the calendar month before it. On the opening
statement (a partial first month or a dash beginning value, §4) they must be
inside the period, so neither predates the account (#1274). No output reads
the purchase date: the cash reconstruction uses the posting date and amount
(§8, #1251).

Errors and logs must not include source text, account/holder identifiers,
addresses, amounts, or private filenames. Tests use synthetic exact-shape
extracts only after a parser exists; this inventory adds no speculative
fixture.

### 6. Income classification and FTC eligibility

SPAXX distributions remain foreign dividends for Indian tax even when Fidelity
reports that US `interest-related dividend` treatment may make some US
withholding refundable. They flow to Schedule OS 1a(i), Schedule FSI, and
[India-US DTAA][india-us-dtaa] Article 10(2)(b), whose portfolio-dividend
limit is 25%.
`Credit Interest` on FCB is foreign interest under Article 11. Article 25(2)(a)
is the relief article.

Rule 115 conversion follows the existing source-neutral income policies:
SPAXX dividends use the preceding-calendar-month-end TTBR, while Credit
Interest uses the financial-year-end Other Sources policy. Foreign tax uses the
Rule 128(5)(ii) preceding-calendar-month-end TTBR per payment event. Monthly
statement events remain the filed amounts; neither the year-end report nor
whole-dollar Form 1042-S boxes replace them.

For undisputed tax, Indian credit does not wait for the US liability to become
final. [Rule 128(1)][rule-128] of the Income-tax Rules, 1962 and
[Rule 76(1)][rules-2026] of the Income-tax Rules, 2026 allow credit for "any
foreign tax paid by him in a country or specified territory outside India, by
way of deduction or otherwise". Article 25(2)(a) allows "an amount equal to
the income tax paid in the United States, whether directly or by deduction".
Neither text conditions credit on a US return or a refund decision; the
evidence, Form 67 / Form No. 44 and credit-limit conditions still apply. The
Rules contemplate a foreign refund after credit has been claimed: Rule 128(10)
requires Form 67 where "the carry backward of loss of the current year results
in refund of foreign tax for which credit has been claimed", and Rule 76(14)
extends that to "revision of return or similar statement" or "any other
reason". Form No. 44 Part C collects the details. That reporting duty does not
make known excess withholding creditable when first claimed. Rule 128(4) and
Rule 76(5) exclude tax "disputed in any manner by the assessee" until the
dispute is settled, the liability is shown to be discharged, and the assessee
undertakes that no refund has been or will be claimed. Neither Rule defines
"disputed"; not seeking a refund does not by itself show that there is no
dispute.

The amount is capped. Rule 128(5)(i) and Rule 76(7)(a) provide that "where the
foreign tax paid exceeds the amount of tax payable in accordance with the
provisions of the agreement for relief or avoidance of double taxation, such
excess shall be ignored". Article 10(2) lets the United States tax dividends
"according to the laws of that state". For RIC dividends beneficially owned by
an Indian resident it caps that tax at "25 per cent of the gross amount of the
dividends", unless Article 10(4) (a US permanent establishment or fixed base)
or Article 1(3)'s saving clause (US residents and citizens) applies. Article
1(2) keeps any US domestic exemption, and Article 1(4)(a) keeps Article 25
relief. [26 U.S.C. §871(k)(1)(A)][usc-871] provides that "no tax shall be
imposed" under §871(a)(1)(A) on a qualifying interest-related dividend,
subject to the §852(a) requirement, the §871(k)(1)(B) exceptions (including
the non-US-owner statement) and the §871(k)(1)(C) definition and
reported-amount limits. This ADR reads the proviso to ignore withholding above
the US tax legally imposed within the treaty limit, such as on an exempt
interest-related part of a SPAXX dividend, even if it is not reclaimed. That
is an inference from the quoted texts, which do not settle it expressly; the
other reading applies only the 25% cap. No circular or ruling settling the
point was found, and this ADR takes the reading that claims less.

Each US calendar tax year therefore has a runtime status: `unknown`,
`not-filed-planned`, `filed-pending`, `finalized`, or `no-refund-claim`; an
omitted year is `unknown`. Leaving a year out is not evidence of filing or
refund intent.

Only `finalized` and `no-refund-claim` can produce FTC. Each requires:

1. static provenance identifying the evidence for the US-law liability within
   the treaty limit and its per-date allocation, for example a final
   US-return/transcript and CA allocation for `finalized`; `no-refund-claim`
   needs no US return, transcript, or refund decision;
2. exact reconciliation of a parser-supported Form 1042-S and matching
   year-end report to the complete statement year; and
3. one non-negative whole-cent eligible-tax allocation for every actual
   statement withholding date in that US tax year, including explicit zeroes.

Each allocation is the US tax payable on that payment under US law within the
Article 10(2)(b) limit, excluding withholding US law does not impose, and its
evidence shows that the payment qualifies for that treaty treatment. The tool
never derives an allocation from gross withholding or Form 1042-S totals. An
allocation cannot exceed actual withholding. `unknown`, `not-filed-planned`,
and `filed-pending` produce no FTC.

FTC is all-or-nothing at Indian-FY level. If any calendar year represented by
in-FY withholding is not both FTC-eligible and reconciled, emit gross income
but suppress all Schedule TR/Form 67 credit rows for the FY. An FTC-eligible
but parser-unreconciled year soft-blocks FTC rather than aborting the income
outputs.

If a US refund later covers tax already credited in India, the user reports it
under Rule 128(10) (carry-back only) or Rule 76(14) and Form No. 44 Part C.

### 7. SPAXX capital gains boundary

SPAXX purchases and dividend reinvestments create acquisition lots at the
actual amount paid. Redemptions consume those lots FIFO. Each closed slice uses
the matched acquisition amount as cost, the statement sale amount as proceeds,
and the source quantity times price as its gross-proceeds audit figure. A
reinvested dividend is still one income event plus one purchase; its lot cost is
the net amount actually reinvested, not the separately reported gross dividend.

A slice or an open lot that holds part of a purchase or sale takes that
amount's share by quantity. The shares are exact rationals until the filed
rounding (#1244), so the shares of one amount add back to it whenever its
parts add back to its quantity. Schedule CG cost and consideration, Schedule
FSI income and the Schedule FA A3 initial value round the exact values to whole
rupees. A Decimal kept for review is the share where a Decimal holds it, else
one division of its rational. A share that needs more digits than exact
arithmetic allows refuses, naming the 1-based statement and Core Fund activity
row of the sale, or of the purchase for an open lot.

Rule 115 capital-gains conversion follows the existing resident-assessee path:
cost and proceeds are converted once at the transfer month's specified TTBR.
No acquisition-date FX conversion or assumed USD 1 NAV is introduced. Schedule
CG A/5's "assets other than A1-A4" path covers these foreign fund units.

The Finance (No. 2) Act, 2024 definition of `Specified Mutual Fund` effective
for AY 2026-27 reaches funds investing more than 65% in debt and money-market
instruments, but no controlling CBDT or judicial guidance was found on its
application to a US-organized fund whose instruments are not SEBI-regulated.
A FIFO closure within 24 months doesn't require that unresolved choice: it
is STCG under either interpretation.
`fidelity-cma-cg` therefore emits only those convergent short-term results and
refuses an in-FY longer-held closure until authoritative treatment is supplied.
An out-of-FY longer-held closure remains FIFO/audit history and cannot block the
requested FY.

No sales/basis tax form is read. Monthly statements remain primary and are
accepted only when the bundle begins without a carried SPAXX position, every
sale FIFO-matches, and cumulative transaction quantity exactly reproduces each
monthly holding. No US wash-sale basis is used or inferred.

### 8. Schedule FA A2 peak-bound attestation

For each day, the ledger reconstructs FCB from signed income, withholding,
deposits, withdrawals, transfers, card post dates, and the offsetting cash leg
of SPAXX core activity. Core movements are value-neutral only after their
execution price exactly matches fetched daily NAV. Both reconstructed paths
must reproduce every reported month-end FCB, SPAXX quantity, and Dec-31 account
value.

The annual lower bound selects the maximum native-USD start/end-of-day value
and then applies that date's TTBR. The annual upper bound selects the maximum
native-USD value after placing every positive same-day account-value posting
before debits, then applies that date's TTBR. The warned
`schedule_fa.peak_currency = "inr"` override instead maximizes the daily
INR-converted bounds. Because native mode orders the bounds in USD and converts
each at its own date's TTBR, the rupee order can differ from the native order:
the lower bound can convert to more rupees than the upper.
Prior-month Credit Interest appearing in the following statement introduces a
posting-date range: lower delays it to that statement's end; upper places it at
the statement start. Both paths converge at every month end.

Default output audits both bounds but leaves A2 peak blank. Automatic filing
requires `suppress_peak_balance_warning = true` plus an explicit
`peak_balance_bound = "lower" | "upper"`; omitting the choice is an error, and
upper is described as more conservative in native currency but potentially
overstated. Warning suppression never removes the two audit figures. When a
bound is attested and the lower bound's INR value exceeds the upper's, the run
refuses with `FidelityCmaFaError` (exit 4) before writing any output (owner
decision, 2026-11-06, #888). The refusal names the attested
`peak_balance_bound` value, says that converting each native-ordered bound at
its own date's TTBR makes the lower bound the larger one in INR, and says that
removing `suppress_peak_balance_warning` and `peak_balance_bound` from
`[fidelity_cma]` writes output with a blank A2 peak. It prints no bound's date,
native or INR value, or TTBR, because those come from the statements (#1047).
Without an attestation the inverted pair stays in the audit columns and the A2
peak stays blank.
The `filing-csv` upload (#1385) has no audit columns: the bounds stay in
review output, and an upload's blank A2 peak is named for manual entry.

## Consequences

A complete calendar-year source supports A2 posting-order bounds and SPAXX
A3 rows. A SPAXX A3 peak that only the disposal day could set, including every
same-day lot, is left blank for manual entry (§2). The first no-carried-balance statement date is used as an attested
opening date with a warning. A2 files one row per gross-credit nature, and
the `filing-csv` upload carries each nature's letter.

Issue #392 may implement only the two observed monthly valuation variants and
must refuse all other Fidelity CMA variants, including a structural FDIC
Deposit Sweep / Program Bank position. `fidelity-cma-fa` emits the A2 rows
and the SPAXX A3 rows, with the A2 peak blank unless a bound is attested
(section 8). *(Shipped 2026-07-18 by #401 for #393.)* Issues #388-#390 may consume
the same primary transactions only after their FY/CY and deduplication checks
are implemented.

No A1 writer, generic Fidelity parser, plugin layer, or guessed Program Bank
support is added.

[rule-114f]: https://incometaxindia.gov.in/w/rule-114f
[itr-schema-source]: ../../src/ibkr_fa/reference/cbdt_itr2_schema_ay2026_27.json
[fa-guide]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-03/Step%20by%20Step%20Guide%20FA%20FSI.pdf
[cma-overview]: https://www.fidelity.com/spend-save/fidelity-cash-management-account/overview
[brokerage-faq]: https://www.fidelity.com/trading/faqs-about-account
[customer-agreement]: https://www.fidelity.com/bin-public/060_www_fidelity_com/documents/customer-service/updated-agreements/Updated-Fidelity-Account-Customer-Agreement.pdf
[sec-fcb]: https://www.govinfo.gov/content/pkg/CFR-2025-title17-vol4/xml/CFR-2025-title17-vol4-sec240-15c3-3.xml
[spaxx-prospectus]: https://institutional.fidelity.com/app/proxy/content?literatureURL=/B-CSPU-SUM.PDF
[fdic-disclosure]: https://www.fidelity.com/bin-public/060_www_fidelity_com/documents/PDF_Fidelity_Cash_Management_Account_FDIC_Disclosure_document_%28PDF%29.pdf
[india-us-dtaa]: https://www.irs.gov/pub/irs-trty/india.pdf
[rule-128]: https://www.incometaxindia.gov.in/documents/20117/42998/Rule-128_2026-01-13_11-37-01_1c629b_en.pdf/145a3343-3b83-7223-7064-8fd9194f161c
[rules-2026]: https://incometaxindia.gov.in/documents/d/guest/en-notified-it-rules-2026-20-03-2026-pdf
[usc-871]: https://www.govinfo.gov/content/pkg/USCODE-2024-title26/html/USCODE-2024-title26-subtitleA-chap1-subchapN-partII-subpartA-sec871.htm
[itr2-ay2026]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-04/Notification%20No.46_2026.pdf
[itr2-instructions]: https://www.incometaxindia.gov.in/documents/20117/11038805/Instructions_ITR2_AY_2023_24.pdf/fe470b08-ceb7-340f-d2e3-d8fd1bf877b7
[itr2-schema-ay2026]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-08/ITR-2_2026_Main_V1.2.json
[cfr-22c-1]: https://www.govinfo.gov/content/pkg/CFR-2025-title17-vol5/xml/CFR-2025-title17-vol5-sec270-22c-1.xml
