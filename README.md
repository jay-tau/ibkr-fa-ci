# ibkr-fa

> ⚠️ **Verify every number with your CA before filing. This is not tax advice.**

A Python CLI that turns your IBKR, Fidelity (NetBenefits ESPP and Cash
Management Account), Morgan Stanley StockPlan, and Robinhood exports into
ITR-2/ITR-3 review and filing outputs for your Indian Income Tax Return.
Portal-facing columns use the e-filing portal's form labels verbatim.
Form 67 and Schedule FA Tables A3/A2 also have upload files (see
[What it isn't](#what-it-isnt)); you copy the other schedules into the
portal field by field. Review outputs (`--format review-csv`,
or the `--debug` sidecars) add `_audit` columns with source details,
such as a dividend's USD amount, FX rate, and FX-rate date, so you can
check the INR figures independently.

## Subcommands

| Subcommand | ITR-2/ITR-3 destination | Reporting period | Input format |
|---|---|---|---|
| `schedule-fa` | Schedule FA Table A3 (foreign equity & debt — capital balances) and Table A2 (the foreign custodial account itself, when `[account.custodian]` is configured) | Calendar Year (Jan 1 – Dec 31) | IBKR Flex Query or Activity Statement |
| `schedule-os` | Schedule OS 1a(i) ordinary dividends and 1b(ix) other interest | Indian Financial Year (Apr 1 – Mar 31) | IBKR Activity Statement only |
| `schedule-cg` | Schedule CG Section A (foreign-equity short-term capital gains, holding ≤ 24 months) | Indian Financial Year (Apr 1 – Mar 31) | IBKR Activity Statement only |
| `schedule-cg-ltcg` | Schedule CG Section B (foreign-equity long-term capital gains, holding > 24 months, new regime post-2024-07-23) | Indian Financial Year (Apr 1 – Mar 31) | IBKR Activity Statement only |
| `schedule-fsi` | Schedule FSI (per-country, per-head-of-income foreign-source income summary) | Indian Financial Year (Apr 1 – Mar 31) | IBKR Activity Statement only |
| `schedule-tr` | Schedule TR (per-country summary of tax relief claimed) | Indian Financial Year (Apr 1 – Mar 31) | IBKR Activity Statement only |
| `form-67` | CBDT Form 67 (per-country, per-source-of-income Foreign Tax Credit statement) | Indian Financial Year (Apr 1 – Mar 31) | IBKR Activity Statement only |
| `run-all` | One-shot: produces every schedule above into `--output-dir` (the six financial-year schedules from `--fy-input`; Schedule FA from `--cy-input`, skipped when omitted, which also deletes any earlier Schedule FA file in `--output-dir`) | Both — derives Schedule FA's calendar year from `--financial-year` | IBKR Activity Statement(s) |
| `netbenefits-fa` | Schedule FA Table A3 (the held Microsoft ESPP shares) + Table A2 (the Fidelity custodial account itself) | Calendar Year (Jan 1 – Dec 31) | Fidelity NetBenefits export (open/closed-lots CSV + statement & confirmation PDFs) |
| `netbenefits-income` | Schedule OS 1a(i) ordinary dividend + Schedule FSI + Schedule TR + Form 67 | Indian Financial Year (Apr 1 – Mar 31) | Fidelity NetBenefits export |
| `netbenefits-cg` | Schedule CG STCG + LTCG for ESPP lots (cost of acquisition = the historical gross INR FMV configured in `[[netbenefits.cg_fmv_values]]`, prorated to the shares sold). For now only for an FY the statements prove had no sale; an FY with a sale refuses until #1150 | Indian Financial Year (Apr 1 – Mar 31) | Fidelity NetBenefits export (closed-lots CSV + purchase-confirmation PDFs + the Stock Plan Services statements covering the capital-gains window described below) |
| `netbenefits-all` | One-shot: `netbenefits-fa` + `netbenefits-income` + `netbenefits-cg` into `--output-dir`. Unlike standalone `netbenefits-income`, its Schedule FSI also includes the capital gains, so FSI and Schedule CG reconcile. For now capital gains are filed only for an FY the statements prove had no sale; an FY with a sale refuses until #1150 | Both — derives Schedule FA's calendar year from `--financial-year` | Fidelity NetBenefits export, with the Stock Plan Services statements covering the capital-gains window described below |
| `fidelity-cma-income` | Schedule OS + Schedule FSI + Schedule TR + Form 67 for SPAXX distributions, Credit Interest, and final eligible US tax. Schedule FSI also includes SPAXX capital gains computed as in `fidelity-cma-cg`. While the FTC is blocked, `form-67-debug.csv` replaces `form-67.csv` | Indian Financial Year (Apr 1 – Mar 31) | Fidelity CMA monthly/year-end reports + observed-version Form 1042-S packet |
| `fidelity-cma-cg` | Schedule CG A5e/A6g for FIFO-matched SPAXX redemptions | Indian Financial Year (Apr 1 – Mar 31) | Fidelity CMA monthly/year-end reports |
| `fidelity-cma-fa` | Schedule FA A2 (CMA custodial account) + A3 (SPAXX lots) | Calendar Year (Jan 1 – Dec 31) | Fidelity CMA monthly/year-end reports + fetched daily SPAXX NAV |
| `stockplan-income` | Schedule OS 1a(i) ordinary dividend + Schedule FSI + Schedule TR + Form 67 for an Alphabet GSU dividend (the only verified StockPlan issuer) | Indian Financial Year (Apr 1 – Mar 31) | StockPlan "IN - Dividend Report" .xlsx |
| `stockplan-fa` | Schedule FA Table A3 review CSV (held StockPlan shares; refuses on any sale or vest-reconciliation gap). Table A2 is not generated, so a successful run exits `3` | Calendar Year (Jan 1 – Dec 31) | StockPlan .xlsx exports (Capital Gain/Loss, Vested Share Holdings, Dividend) + fetched daily closes |
| `stockplan-all` | One-shot: `stockplan-income` + `stockplan-fa` into `--output-dir`; also proves whether Schedule CG is needed (any sale exits `4`) and reports Table A2 as manual-fill, so it never exits `0` | Both — derives Schedule FA's calendar year from `--financial-year` | StockPlan .xlsx exports |
| `robinhood-cg` | Robinhood foreign-equity Schedule CG STCG/LTCG, each section ending with its Schedule CG Table F date bands (F3/F5). Each sale's recorded `Amount` is filed as its full value of consideration, with 0 transfer expenditure (#1383). A positive STCG no longer refuses; a loss across bands leaves the bands blank and named. A positive LTCG still refuses for section 54F. When option capital-gain lines exist, its workbook names their sections for manual entry from `robinhood-options.csv` and the run exits `3`, unless an existing refusal applies | Indian Financial Year | Two activity CSVs + complete brokerage statements |
| `robinhood-income` | Robinhood Schedule OS plus FSI/TR/Form 67 when FTC evidence is complete | Indian Financial Year | Robinhood bundle; Form 1042-S packets for the FTC leg |
| `robinhood-fa` | Two Robinhood A2 account rows + transfer-safe A3 holdings | CY derived from FY start year | Robinhood bundle + Yahoo equity closes + Massive option closes |
| `robinhood-options` | Option CG review, with each term bucket's Schedule CG Table F date bands (row 3 or 5, the option gains alone, blank and named for a loss across bands), or reviewed ITR-3 P&L/BP/CFL/FSI fields | Indian Financial Year | Robinhood bundle |
| `robinhood-all` | All supported Robinhood schedules and completeness status; also emits validated Schedule FA JSON when complete | CY + FY union | Complete two-account Robinhood bundle |
| `merge` | Splices one ibkr-fa Schedule FA JSON fragment (`--format cbdt-json-fragment` output) into a saved ITR-2/ITR-3 return, writing a new file | — | Fragment JSON + saved return JSON exported from the offline utility |
| `init` | Scaffolds an annotated starter `user.toml` | — | — |

Commands that emit Schedule CG or TR require `--itr-form ITR-2|ITR-3`
because their AY 2026-27 destination labels differ. The calculations are shared.
For ITR-3, Schedule FSI `IncFromBusiness` comes only from Robinhood options
under a reviewed business treatment: `robinhood-income` and `robinhood-all`
file complete, positive net business income from those options as a US
`Business or Profession` row, so don't enter it again. Complete any other
foreign business income from business records; never infer zero.

All schedule-producing subcommands share the same `user.toml`
config (account identity + FX-staleness policy).

### Unexpected errors

An unexpected error (a bug in ibkr-fa) prints only the error's type and the
ibkr-fa source line,
such as `error: internal error (KeyError at ibkr_fa/cli.py:1234)`, and exits
`1`. Include that line when you report the bug. To see Python's full traceback
instead, re-run with `IBKR_FA_TRACEBACK=1` set in the environment. The
traceback may contain values from your input files, such as account numbers or
amounts, so check it before you share it.

## Configuration

The quickest way to get a config is `ibkr-fa init`, which scaffolds an
annotated `user.toml` (every setting commented out — uncomment and edit
the `[account]` table, which is required, plus any optional sections you
need). Pass a path to write elsewhere, and `--force` to overwrite.

Example `user.toml`:

```toml
[account]
number = "U12345678"          # IBKR account number; cross-checked against the CSV
holder_name = "Jane Doe"
opening_date = 2020-01-01
status = "BeneficialOwner"    # BeneficialOwner | Joint
us_person = false             # optional, default false. Set true if you
                              # have a W-9 on file at IBKR (US citizen or
                              # green-card holder). Drives the CLI's
                              # withholding-tax advisory — see issue #145.
# Optional persistent opt-ins for the two safety flags (the CLI flag OR the
# opt-in enables the behaviour; the loud warning still prints either way):
allow_masked_account = false  # == --allow-masked-account: skip the account-ID
                              # cross-check when IBKR masks the Account row
accept_anonymised_statement = false  # == --accept-anonymised-statement: take
                              # holder_name / number from here when the export
                              # hides them

# Optional: your foreign Taxpayer Identification Number per country, auto-filled
# into the Schedule FSI + TR "Taxpayer Identification Number" column. Keyed by
# ISO 3166-1 alpha-2 (never "IN" — these schedules are foreign-source only).
# Each key must be a code the ITR country list has (e.g. GB, not UK).
# Mandatory when claiming §90/90A/91 relief: without it, a relief row's TIN cell
# stays blank and these commands exit 3: schedule-fsi, schedule-tr, run-all,
# robinhood-income, robinhood-all, netbenefits-income, netbenefits-all,
# stockplan-income, stockplan-all, and fidelity-cma-income.
# netbenefits-all and stockplan-all also show their income leg as manual-fill.
# If a country did not allot you a TIN, use your passport number.
[account.foreign_tins]
US = "123456789"              # your US TIN, or passport number if none was allotted

# Optional: your IBKR custodial institution. Required to emit Schedule FA
# Table A2 (the foreign custodial account itself) — the institution's name /
# address / country are not in the IBKR statement. Omit and `schedule-fa`
# emits only Table A3 (the holdings) and warns that A2 was skipped.
# country_iso2 is the INSTITUTION's country (never "IN").
[account.custodian]
institution_name = "Interactive Brokers LLC"
address = "One Pickwick Plaza, Greenwich, CT"
zip = "06830"                 # needed for upload; blank = manual fill (exit 3)
country_iso2 = "US"

[fx]
max_carry_forward_days = 14   # staleness ceiling on the bundled SBI TTBR archive

# Optional: per-issuer Schedule FA Table A3 details, keyed by the holding's
# ISIN. Verify nature_of_entity from authoritative issuer material; broker
# labels such as ETF/COMMON do not prove the entity's legal nature. Each
# entry needs isin. For a Robinhood holding you may leave out address: the run
# looks the issuer's address up, writes it to robinhood-identity-guidance.toml
# in the output folder and stops until you verify it and add it here. IBKR
# looks no address up, so an IBKR holding's entry needs address. zip and
# prefer_configured_address = true need address too.
[[fa.entities]]
isin = "US99999A1025"
address = "1 Example Plaza, Anytown, NY"
zip = "99999"
name = "Example Holdings Inc." # optional; omit to keep the statement's name
nature_of_entity = "Company" # required to auto-fill A3 Nature of entity

# Optional: manual figures for the CBDT Schedule FA JSON fragment (see the
# `schedule-fa --format cbdt-json-fragment` import path) that cannot be derived
# from the statement — currently the Table A2 peak balance (select the highest
# native-currency securities + cash value first, then apply that date's SBI
# TTBR; the statement carries no daily NAV series). manual_values_account_number
# MUST equal [account].number, binding these filed figures to this account.
[schedule_fa]
manual_values_account_number = "U12345678"
peak_currency = "native"       # default; "inr" is a warned override
[[schedule_fa.manual_values]]   # one block per Schedule FA calendar year
calendar_year = 2025
a2_peak_balance_inr = 8950      # whole rupees (integer; never rounded for you)
```

### Robinhood bundle

Use a dedicated output directory for Robinhood; its fixed schedule filenames
are intentionally the same portal-facing names used by other sources.
Schedule OS is the exception: Robinhood emits a formatted
`schedule-os.xlsx` with a compact ITR sheet plus separate horizontal Quicko
views by account and by security. The Quicko sheets are alternatives—never
enter both—because each reconciles to the same dividend total.

```bash
ibkr-fa robinhood-all \
  --input robinhood-bundle \
  --config user.toml \
  --output-dir out/robinhood \
  --financial-year 2025-26 \
  --itr-form ITR-2
```

Let the command create `--output-dir`: it makes the folder readable only by
you. A folder that already exists must be yours and private (`chmod 700`), or
every Robinhood command refuses (exit 4).

```text
robinhood-bundle/
├── activity/
│   ├── individual.csv
│   └── managed-individual.csv
├── statements/
│   └── ... brokerage statement PDFs ...
└── tax-forms/
    └── ... Form 1042-S packet PDFs when required ...
```

`[[robinhood.reports]]` names the two activity files and their selected export
ranges. Filenames never assign accounts: statement/activity reconciliation
derives that mapping. Reports must start no later than the configured account
opening date and end on an issued statement period boundary. Statements must
include each account's first-ever unavailable-opening statement through the
requested CY/FY union.

Robinhood Crypto and Robinhood Spending/Cash Card are explicitly out of scope.
Crypto PDFs are discovered, counted, and warned, but never parsed. Spending is
not parsed. Neither product blocks supported brokerage output.

`tax-forms/` supplies secondary annual income-code and withholding evidence;
Robinhood activity/statements remain the exact income ledger. A Form 1042-S
is reconciled against US dividends only; non-US dividends are proven by the
statements and the `DTAX` pairing. Positive
unmatched tax, contradictory character, or invalid form identity blocks the
FTC leg; the run then exits 4 and removes Schedule OS with the rest of its
output, even when Schedule OS is complete. A
bounded whole-dollar mismatch on a valid zero-tax code-01 interest bucket is
reported as a warning while exact statement interest remains calculation
truth. Current-year forms that are not yet due are not required to classify
exact broker-source receipts.

For the FY-end calendar year, complete activity/statements through 31 March
drive FSI and any source-paired withholding. No future April-December coverage
is required unless a Form 1042-S for that calendar year is actually supplied;
supplying one re-enables full-year coverage and normal reconciliation. A bound
packet for either FY calendar year is reconciled whether or not that
account-year has income, and packets for other calendar years are not used. The
CLI warns when Form 67/TR relies on statement withholding and when a US dividend
has no paired withholding debit.

Robinhood `Stock Lending` / `SLIP` credits are monthly securities-loan fees and
go to Schedule OS 1e `Any other income`, using the FY-end Rule 115 TTBR.
Robinhood's separate `Manufactured Div.` event is not inferred from a generic
credit. Any Form 1042-S substitute-payment code, including code 34, contradicts
generic fee rows and blocks the classification instead of silently changing or
dropping income.

Schedule FA A3 normally attributes credits to lots held on the source holding
date. A delayed monthly loan fee may instead use Robinhood's dated per-account
Loan Activity ledger when that ledger and adjacent month-end snapshots prove
one exclusive economic lot; payment date controls CY inclusion and attribution,
while the calendar-year closing date controls Schedule FA income FX.

Robinhood does not document whether a sale's activity `Amount` is before or
after the FINRA TAF and SEC fees it passes on. By the owner's decision of
2026-11-14 (#1383), each equity sale's `Amount` is filed as its full value of
consideration, with 0 transfer expenditure, in Schedule CG, Schedule FA A2 and
A3, and Schedule FSI, as option gains are filed from their net `Amount`
(#1362). If a fee is inside `Amount`, the gain is still right; if one was
collected outside it, the gain is higher by that fee: slightly more tax, never
less. Each in-CY sale's A3 `Total gross proceeds` is its `Amount` at the
December 31 TTBR, split exactly across the lots it sold. The account's A2
sale-proceeds nature adds each in-CY equity sale's `Amount` and each option
sell-to-close's cash. Any option blocker refuses the Schedule FA leg instead
(exit 4). Each `schedule-cg.xlsx` section sheet's tax position note states
this position, and
[ADR 0006](docs/decisions/0006-robinhood-multi-account-evidence.md) section 7
gives the reasons.

Schedule FSI never infers a loan-fee country from the issuer, ISIN, withholding
agent, or broker address. A reviewed account/FY source is required:

```toml
[[robinhood.stock_lending_fee_fsi_sources]]
account_number = "123456789"
financial_year = 2025
country_iso2 = "US"
source = "reviewed Robinhood Stock Lending agreement and filing position"
reason = "RHS is the principal borrower and monthly Loan Fee obligor"
```

The review applies only to generic loan fees. Substitute payments retain their
own classification and source rules. Missing or partial account/FY coverage
keeps FSI/TR/Form 67 blocked; Schedule OS is complete, but the failed run
removes it with the rest of its output. Duplicate reviews reject the config
before any output is written.

Schedule FSI files Robinhood interest under the US only when a reviewed
account/FY entry says every payer of that interest is a US entity. The account
country doesn't count. Each account with interest in the financial year needs
one entry whose `interest_kinds` list every kind it earned: `brokerage`, and
`program-bank` for Cash Sweep Program interest from any Program Bank:

```toml
[[robinhood.interest_payer_countries]]
account_number = "123456789"
financial_year = 2025
country_iso2 = "US"
interest_kinds = ["brokerage", "program-bank"]
source = "reviewed Robinhood brokerage interest and Cash Sweep Program terms"
reason = "every payer of this account's FY interest is a US entity"
```

A missing entry, an entry for another account or FY, a non-US country, or a
kind the account earned but the entry doesn't list keeps FSI/TR/Form 67
blocked, and the run says so. As with the loan-fee review, Schedule OS is
complete, but the failed run removes it with the rest of its output.
Duplicate or malformed entries reject the config before any output is
written.

When the statements don't settle which interest credits are brokerage and
which are program-bank interest, no entry can clear the block: Schedule OS,
FSI, TR and Form 67 are all refused, the run says so, and you complete them
by hand.

Use `[[robinhood.corrected_statements]]` to select an explicit corrected PDF
and name every superseded original. Identical files are hash-deduplicated;
non-identical duplicate statement periods otherwise refuse. Amended Form
1042-S is not selected by filename and remains unsupported.

Robinhood exit status is `0` only when every applicable surface is complete,
`3` when valid output still needs manual FA/option/Form-67 work (such as a
fractional Form 67 `Rate(%)` to enter after upload) or a relief
row's foreign Taxpayer Identification Number is blank, and `4` for
evidence/reconciliation refusal, a Form 67 row refused under `[form67]`
below, or Schedule FA's stop for a held security's `[[fa.entities]]` entry
without address (below). `robinhood-all` checks Form 67 readiness before writing any schedule;
a refused row stops it before any leg is reported. Otherwise it reports
every leg, but if any leg fails it removes every file the run wrote
(a `schedule-fa.json` it did not write is kept) and exits `4`, so a failed run
leaves nothing to file. A file it cannot remove is named in the error. A
Schedule OS written before a blocked FTC leg is removed too, by
`robinhood-income` on its own as well. Business-option treatment requires
`--itr-form ITR-3`. The reviewed Robinhood filing position keeps aggregate
Program Bank sweep cash and interest inside each custodial A2 account; no A1
row is emitted.

Robinhood F&O is disabled unless the affected account/FY has an explicit
`[[robinhood.option_classifications]]` block containing both a tax `treatment`
and `fema_risk_acknowledged = true`. Every processed F&O run prints a
non-quietable warning: paragraph 5 of the
[RBI LRS Master Direction](https://www.rbi.org.in/scripts/NotificationUser.aspx?Id=10192)
disallows LRS remittances in the nature of margins or margin calls to overseas
exchanges/counterparties. The acknowledgement enables tax reporting only;
`ibkr-fa` does not decide whether the trades were LRS-funded or otherwise
FEMA-compliant. The option workpaper preserves the user-stated classification
source and reason as inputs, not legal proof, and prints a separate section
43(5) warning. Speculative losses are ring-fenced under section 73; carrying
forward either speculative or ordinary-business losses requires a timely loss
return under sections 80 and 139(3).
When option activity exists, a missing classification/acknowledgement is a
run-level error for every Robinhood command, including `robinhood-all`; no
schedule output is produced until the explicit gate is satisfied.

### `[form67]` configuration (Foreign Tax Credit)

Every command populates the **Foreign Tax Credit** columns of CBDT Form 67
only when exact source-level Indian tax is supplied. Without an exact amount,
Schedule FSI col (d) and Form 67 cols 7, 11, and 13 remain blank, and the
official Form 67 CSV is withheld (below): foreign tax withheld is not evidence
of Indian tax attributable to that income.

Rule 128(5)(i) limits FTC to the lower of Indian tax payable on the income
and eligible foreign tax, but does not prescribe an average- or marginal-rate
formula. For ordinary income,
[*Promac Engineering Industries Ltd v ACIT*](https://indiankanoon.org/doc/4726975/),
ITA 501/Bang/2018, paras 12-13, applied
`foreign income × average Indian tax rate`; §2(10) defines that average as
`Indian income-tax on total income ÷ total income`. *Promac* concerned
AY 2014-15, before Rule 128 applied. The Karnataka High Court later
[disposed of the Revenue's appeal on monetary-limit grounds while expressly
keeping the question of law open](https://indiankanoon.org/doc/75630410/).
No reported ruling was found settling average versus marginal rate under
Rule 128(5)(i), so confirm the method under the relevant DTAA/current law and
any special-rate treatment before supplying the exact amount below.

An exact `[[form67.tax_payable_overrides]]` amount populates the matching
source. Schedule FSI files a country's dividends, interest and other income in
one Other Sources row, so its relief is filled only when each source in the row
has an exact amount: `DIVIDEND`, `INTEREST_INCOME` for interest, and one
`OTHERS` amount for substitute payments and reviewed Robinhood stock-lending
fees together. §91/no-DTAA claims remain fail-closed unless both an exact
source-level override and the statutory `[form67.section_91]` average-rate
inputs are provided.

A command's `form-67.csv`, Schedule FSI and Schedule TR cover only the income
that command read, and an override is the Indian tax on that one command's
income from its country and source. Overrides are keyed by country and source
alone, so every command run with the same `user.toml` applies the full amount
to its own income from that country and source.

When commands that read different files, such as `robinhood-income` and
`netbenefits-income`, report the same country and source, the portal still
needs one combined entry: don't upload or enter each command's rows for it side
by side. Leave that override out and enter one Form 67 row by hand (see
**Blank rows** below): add the income and the foreign tax across the commands,
use the Indian tax on the combined income, and claim the lower of that and the
combined eligible foreign tax. Combine that country's Schedule FSI and
Schedule TR rows too
([docs/portal/schedule-fsi.md](docs/portal/schedule-fsi.md),
[docs/portal/schedule-tr.md](docs/portal/schedule-tr.md)).

Use each command's `form-67-debug.csv` where it has a row; otherwise complete
its figures by hand. For a blank Form 67 row from `schedule-fsi` or
`schedule-tr`, also run `form-67` on the same input to obtain that audit file.
If Fidelity CMA FTC is blocked, its Form 67 audit file has no rows; follow the
run's warning before combining its figures.

The regular `form-67.csv` is CBDT's official 15-column `IncomeDetails.csv`
upload shape (numeric country/source codes, no audit columns; see
[docs/portal/form-67.md](docs/portal/form-67.md)). It is written
only when every row has its Indian tax payable and a credit of at least ₹1:
the portal will not save a row whose credit is blank. The engine rounds the
filed amounts to whole rupees and the filed rates to two places, half-up;
readiness and the portal columns of every Form 67 output use those values
(#898). Every command decides this the same way (#601):

- **Blank rows** (no exact override): `form-67`, `run-all`, every
  `*-income` command and every `*-all` command withhold `form-67.csv`, write
  `form-67-debug.csv` even without `--debug`, keep Schedule OS/FSI/TR, print
  the manual-calculation warning and exit `3`. That warning (see
  [docs/portal/form-67.md](docs/portal/form-67.md#importer-warnings)) names
  each blank row an exact `[[form67.tax_payable_overrides]]` amount can't
  clear, with its gap. Enter those rows on the portal by hand from
  `form-67-debug.csv`: an override on one makes it a refused row, which stops
  the run with exit `4`. For the other rows, add what the warning asks for and
  re-run, or enter them by hand too. `schedule-fsi` and
  `schedule-tr` (every `--format`) write their files, print the same warning
  and exit `3` when a Schedule FSI row with foreign tax has a blank Indian tax
  payable (`schedule-tr`: in a country it writes); `run-all` shows each
  affected leg as `manual-fill`.
- **Blocked Fidelity CMA FTC** (#1453): while `fidelity-cma-income`'s FTC is
  blocked, its Form 67 has no rows though there is US tax to claim, so a
  header-only `form-67.csv` would read as nothing to claim. The run withholds
  it the same way, writes `form-67-debug.csv` even without `--debug`, and
  exits `3`. An FY with no US withholding still writes the header-only
  `form-67.csv`.
- **Refused rows** (neither complete nor blank) stop the run with exit `4`
  before any normal schedule output is written. The error names each row and its gap: a
  country with no vendored DTAA row, missing `[form67.section_91]` inputs,
  a credit that rounds to ₹0 in whole rupees, even when the unrounded credit
  is positive (refused under the owner's #601 policy; a section 91 row
  refuses with its own fixed message, see below), withholding that can't be
  matched to a single dividend payment (#890), or a DTAA
  row whose source-currency tax rate can't be established, for example when a
  same-year refund nets the source-currency tax to zero (#767).
  Withholding can't be matched when one security pays two dividends on the
  same day in one account, when a payment's withholding has no dividend or
  is more than it, or when a payment is withheld above the DTAA rate and the
  country's tax is also refunded or reversed in the same FY. `form-67`,
  `schedule-fsi`, `schedule-tr`, `run-all`, every `*-income` command and
  every `*-all` command can reach it. To produce the other outputs, remove
  the row's `[[form67.tax_payable_overrides]]` entry; the row is then left
  blank for manual Form 67 work.
- **Fractional rates** (#1447): the portal's CSV importer may blank a
  `Rate(%)` (column 6) that isn't a whole number. When `form-67.csv` files
  one, it keeps the exact rate, the importer warning says to enter it by hand
  after upload, and the run reports manual work: `form-67` and every
  `*-income` command exit `3`, and `run-all` and every `*-all` command show
  that leg as `manual-fill` (Robinhood: `manual`) and exit `3`.
  `form-67 --format review-csv`/`xlsx` and `run-all --format xlsx` write no
  upload file, so a fractional rate doesn't change their exit code.

For diagnostics, standalone `form-67 --format review-csv` (17-column audit
view, including a manual-Indian-tax-calculation flag) and `--format xlsx`
still write every row and exit `0`.
Source-specific income commands accept one
`--debug` flag and write `schedule-os-debug.csv`, `schedule-fsi-debug.csv`,
`schedule-tr-debug.csv`, and `form-67-debug.csv`. Files containing `-debug`
are audit artifacts, not filing inputs.

```toml
[form67]
# Optional: exact per-(country, source) Indian tax payable. Repeat the
# block per source. Without it, Indian-tax and FTC cells remain blank.
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"               # DIVIDEND | INTEREST_INCOME | SHORT_TERM_CAPITAL_GAIN | LONG_TERM_CAPITAL_GAIN | OTHERS
tax_payable_normal_inr = "12345.67"

# §91 unilateral relief for income from a country with NO DTAA. Both
# fields and a matching exact override above are required.
[form67.section_91]
total_indian_tax_before_91_relief_inr = "150000"   # your total Indian tax for the year
total_income_inr = "1000000"                        # your total taxable income for the year
```

If the source-currency foreign withholding rate exceeds the DTAA treaty
cap, the excess is **not** creditable in India and the CLI warns you
(recover it from the source country, not via Form 67). INR income/tax
ratios produced from different Rule 115/Rule 128 FX dates are not used
to infer excess withholding. When an exact override is lower than the treaty-eligible foreign tax, the
credit is capped at that exact Indian tax and the remainder is left
unrelieved. Without an override, the Indian-tax and FTC columns are left
blank and the run prints a separate "INDIAN TAX CALCULATION REQUIRED"
warning; such rows are handled as described under **Blank rows** above.

#### The section 91 credit is exact (#1177)

A no-treaty country's credit is computed only when all three gates hold, as
before: the vendored DTAA table marks the country no-treaty (a treaty country
takes section 90, an unknown country neither), `[form67.section_91]` has both
fields, and an exact `[[form67.tax_payable_overrides]]` entry sets col 7.
The unrounded Decimal in Form 67 cols 12 and 13 is computed from the exact

```text
min(foreign tax, total Indian tax × country income ÷ total income)
```

The two sides are compared as exact fractions. When the foreign tax is lower
or equal, the credit is that foreign tax itself. Otherwise the fraction's
numerator and denominator are divided once, rounding down, at the run's
decimal precision. That value is never above the exact credit, so it stays
below the foreign tax. Before it is used, the engine proves that it rounds
half-up to the same whole rupee as the exact fraction `n/d` does, which is
`(2n + d) // (2d)`. It also checks that the value stays within the foreign tax
and is positive exactly when the exact credit is.

The unrounded value is kept. Form 67 cols 12 and 13 and Schedule FSI's relief
round it to whole rupees where they always have, and Schedule TR adds those
filed whole-rupee cells. A repeating average rate, such as tax 1 on income 3,
is ordinary and computes. Decimal flags the run already has set, such as
`Inexact` or `Rounded`, don't cause a refusal. The section 91 credit computation
leaves its caller's precision, rounding, traps and flags unchanged.

The former formula, `min(foreign tax, (total tax ÷ total income) × income)` at
the run's precision, still runs as a check, so nothing it refused passes. A
refusal exits `4` with one of these fixed messages. Each names the country by
its position among the countries with withholding in the input, counted
before any is dropped, and never by a value. Preflight refusals run first,
then the existing checks on other rows, then corrected-value refusals.
Within either section 91 phase, the first refusing position is named.

| Refusal | `error: InvariantError: Form 67 section 91, country input row <n>, …` |
| --- | --- |
| The credit can't be converted or proven (an extreme operand, precision or exponent limit) | `fields section_91_credit_inr and total_credit_inr: exact filed credit cannot be guaranteed within Decimal precision` |
| The former credit filed ₹0, where readiness applies | `field section_91_credit_inr: the credit as filed today rounds to zero, and a filed credit must be positive` |
| The former relief broke the FSI portal rule | `field section_91_credit_inr: the relief as filed today isn't the lower of the filed foreign tax and the filed Indian tax` |
| The former formula failed another credit check (a decimal trap, the filed-amount bound) | `field section_91_credit_inr: the credit as filed today fails an existing Form 67 credit check` |
| The exact credit files ₹0, where readiness applies | `field section_91_credit_inr: the exact credit rounds to zero, and a filed credit must be positive` |
| The exact relief breaks the FSI portal rule | `field section_91_credit_inr: the exact relief isn't the lower of the filed foreign tax and the filed Indian tax` |
| The exact credit fails another credit check (the filed-amount bound) | `field section_91_credit_inr: the exact credit fails an existing Form 67 credit check` |

Each check runs only where it ran before:

- **Readiness:** applies to `form-67 --format portal-csv`; `schedule-fsi`,
  `schedule-tr` and `run-all` in every format (#1480); and every `*-income`
  and `*-all` command. A credit the former
  formula filed as ₹0 still refuses, even where the exact credit files ₹1 (for
  example, tax 100000 on income 2800000 gives exactly 0.5 on income 14). A
  §91 row with a zero credit either way, such as zero total Indian tax, now
  refuses with the "as filed today" message instead of the error naming the
  row. An exact credit that newly files ₹0 refuses too: 0.4999… that the
  former formula rounded up to 0.5000… and filed as ₹1.
  - Other REFUSED rows keep their own error.
  - Standalone `form-67 --format review-csv` and `--format xlsx` still write
    every row with its readiness, the corrected credit included.
- **The Schedule FSI portal rule:** applies to `schedule-fsi`, `schedule-tr`,
  `run-all` and every `*-income` and `*-all` command. A relief the former
  credit broke it with still refuses, even where the exact relief now matches.
  An exact relief that newly breaks it refuses too. `form-67` doesn't apply
  this rule.

Filed values change only where the former formula rounded across a half
rupee. The exact credit is then the correct one: ₹2 instead of ₹1 for tax
300000 on income 2800000 with income 14 and foreign tax 2.10 (exactly 1.5),
and ₹1 instead of ₹2 when the former 28-digit product rounded
1.49999999999999999999999999975 up to 1.5.

### `[prices]` configuration (Schedule FA peak value)

`schedule-fa` computes the "Peak value of investment during the Period"
column by fetching each holding's daily closing prices. It keys on the
ticker in your IBKR statement; for most US-listed holdings that resolves
directly. Occasionally it does not — a security your broker lists under
a ticker the price source doesn't recognise (a recent rename, or a
cross-listing). In that case the peak is left blank for manual entry
(never guessed). The CLI names why each blank peak is blank, and prints
the override snippet below only when the price source has no USD series
for the ticker. A peak left blank for another reason (`--no-fetch-prices`,
a split, a US market session with no close, a missing TTBR, …) gets that
reason's own remedy instead.

Every lot of one ticker reads the same price series. So `schedule-fa`
refuses a statement whose priced lots (held at year end or sold during
the year) list one ticker under two ISINs, with or without an override.
That covers a reused ticker and a security whose ISIN changed during the
year under the same ticker.

If you can **verify** that a USD price-source symbol is the **same
security** as your holding (match the **ISIN**), map it:

```toml
[[prices.ticker_overrides]]
ibkr_ticker = "XNEW"          # the symbol in your IBKR statement (or configured symbol)
isin = "CA99999B1004"         # the holding's ISIN — the override applies ONLY on a match
yahoo_symbol = "XOLD"         # the USD symbol the price source lists it under
```

The ISIN is **required** and must match the holding: it binds the
override to that exact security so it can't misapply prices to a
different holding with a reused ticker. The override is still subject to
the same safety guards as any fetched price — a non-USD listing, a
mismatched resolved symbol, a stock split after the lot's first in-year
day and on or before the earlier of its disposal date and December 31,
or a missing required market session refuses rather than producing a
wrong figure. Yahoo's
dated split events normalize older closes back to their contemporaneous basis,
so a post-year split does not rewrite a filed value. A
**wrong** mapping can still produce a wrong number if it points at a
different USD security, so confirm the ISIN before relying on it. The
fetched prices are cached next to the input statement (or under
`--price-cache-dir`) with source, split basis, coverage, and an integrity hash.
Every run checks that hash before it reads a cache, online or not. A cache file
with no valid hash, or one that doesn't parse, refuses the run (exit 4) and is
left as it is: move it aside so the next online run fetches a fresh one, or
restore the original. Supported table headers are `date,close_usd` (legacy)
and `date,close_usd,high_usd`, with rows of that width. Each non-comment,
non-empty line is one strict CSV record; metadata keys may appear at most once.
Prices use ASCII decimal or exponent text, with no present high below its close;
blank or absent highs remain unavailable. Split ratios, when present, are a JSON
object of date keys and positive decimal strings in the same grammar. Comments,
well-formed CSV quoting, unrepeated unknown keys and missing optional metadata
remain allowed. Layout, CSV, duplicate-key, numeric-text and high-below-close
refusals name the physical file line, counting comments, metadata and blanks.
Unrepresentable decimal exponents keep the generic `doesn't parse` refusal.

Overrides apply to `schedule-fa`, `robinhood-fa`, `stockplan-fa` and
`netbenefits-fa`, and to the `run-all` and `*-all` runs that include them.
For a Robinhood, StockPlan or NetBenefits holding, `ibkr_ticker` (the key keeps
its name) is the `symbol` in its `[[robinhood.securities]]`,
`[stockplan.security]` or `[netbenefits.security]` entry. Only the symbol sent
to the price source changes; the output still shows the configured symbol.

For mandatory Robinhood option valuation, keep the Massive API key outside TOML
and reference its file:

```toml
[prices]
massive_api_key_file = "env"  # relative to user.toml; absolute paths also work
```

The referenced file contains `MASSIVE_API_KEY=...`. `robinhood-fa` and
`robinhood-all` use Massive Contract Overview plus `1/day` Custom Bars to cache
qualifying-trade closes for supported standard Calls. Every held market session
must have a close; a no-trade/missing bar blocks Schedule FA rather than using a
broker mark or stale session. Statement option marks remain reconciliation
anchors only.

`--no-fetch-prices` is strict cache-only mode. It performs the same automatic A2
calculation without network access and blocks Schedule FA if any equity or
option cache is missing, incomplete, identity-mismatched, or integrity-invalid.
Other independently valid `robinhood-all` legs still run and report, but the
failed run then removes their output.

An exceptional historical equity series may be acquired once from a reviewed
source and manually hash-locked beside the normal CSV cache. A valid lock forces
all future runs to use those exact bytes—even under refresh—and a missing,
modified, or incomplete locked cache hard-fails without contacting another
provider. This does not add that one-off source as a production price provider.

Robinhood `REC` rows prove that shares arrived but not their cost basis. Each
such row hard-fails the affected calculation and prints a required
`[[robinhood.security_receipt_resolutions]]` block. Promotional rewards require
the exact documented share quantity and USD reward FMV plus an attestation that
the FMV was included in the applicable Indian return/workpaper. The exact
quantity must unambiguously round back to the source row's display; terminal
answers and estimated statement values are never accepted.

Missing Robinhood security identities are enriched automatically from cached
official Nasdaq Trader and SEC bulk records. The cache is keyed by the broker
CUSIP/symbol/description evidence, so unchanged runs perform no identity
network requests. Automatic resolution is intentionally limited to proved US
common stocks and ETFs; ADRs, foreign shares and ambiguous instruments still
hard-fail with explicit config guidance.

Schedule FA A3 legal addresses are a separate exact-ISIN lookup. Robinhood tries
GLEIF `legalAddress` first (series ETFs excepted, below) and never uses GLEIF
headquarters. Exact GLEIF
records must be issued, active, successor-free, country-consistent and
portal-compatible, and their postal codes are never truncated. A US common stock
may then use the SEC filer's principal business address under the conditions of
[ADR 0009's amendments](docs/decisions/0009-robinhood-legal-addresses.md), but
only when GLEIF has no record for its ISIN (a verified zero-match) or returns a
`LAPSED` record that passes every other GLEIF check and whose legal name agrees
with both SEC names. Any other GLEIF result, such as a country mismatch, an
ambiguous match or a cache-only miss, blocks the SEC address, ZIP and
SEC-derived nature. An SEC ZIP+4 is
reduced to its ZIP5 for the portal, and the exact ZIP+4 stays in the immutable
cache. A supplied Robinhood ADR Form F-6 can bind the broker issuer to a
reviewed depositary LEI; GLEIF then supplies that depositary's name, legal
address, and legal form. Unreviewed depositaries and other records without a
usable GLEIF address remain manual unless the SEC fallback (US common stock
only, after the GLEIF results above) or a reviewed `[[fa.entities]]`
address/ZIP fills them. Results are cached immutably under hashed paths. A
corrupt or unreadable GLEIF cache artifact, a failed GLEIF fetch, or a GLEIF
response with a field of an unexpected JSON type stops the run (exit 4), on the
exact-ISIN lookup and on the ADR depositary and ETF registrant lookups alike. So
does a corrupt or unreadable SEC Form F-6 or N-CEN selection cache artifact, or
any unusable entry in the cached SEC identity snapshots searched for the post-FY
snapshot, whichever securities that entry covers. A
registrant record that is valid but unusable (lifecycle, name or country
mismatch, incomplete address) still leaves its route unresolved, and configured
`[[fa.entities]]` values still apply. A cold lookup
discloses the held ISIN or reviewed depositary LEI to GLEIF, while
`--no-fetch-prices` remains strict cache-only mode. Set
`prefer_configured_address = true` in one `[[fa.entities]]` entry to retain an
explicitly reviewed configured address.

A Robinhood `[[fa.entities]]` entry may leave out `address` (and then `zip` and
`prefer_configured_address = true`). For a security held in the calendar year,
`robinhood-fa` and `robinhood-all` then file nothing found without your check:
they write the automatic address and ZIP found for the entry's ISIN (GLEIF, the
SEC fallback or the F-6 depositary, chosen as above), or a note that none was
found, to the private `robinhood-identity-guidance.toml` in the output folder,
write no Schedule FA file and exit `4`; `robinhood-all` removes its other
outputs too. A configured series ETF takes no automatic route, so its block
says nothing was looked up. Each block repeats the entry with the address and
ZIP commented out and `prefer_configured_address = true`, so it refuses at load
until you verify the address against the issuer's own filings, uncomment it,
and replace the entry in `user.toml` with the block. Stdout and stderr name
only the file. `robinhood-cg`, `robinhood-income` and `robinhood-options` don't
file Table A3 and run as without the entry; an entry for a security not held is
unused.

Robinhood legal nature is resolved separately from address. Exact active GLEIF
ISO 20275 English labels are used verbatim when they fit the 34-character
portal limit; reserved/overlength values require reviewed mappings. Unmapped
funds may use exact SEC mutual-fund class plus registered-investment-company
filing evidence (an ETF also needs its registrant, below). Broker labels and
registrant-name heuristics are never used. Set `prefer_configured_nature = true`
to retain a reviewed configured value.

For SEC series ETFs, `ibkr-fa` binds the exact SEC CIK/series/class to a
pre-calendar-year-end N-CEN, then resolves its registrant LEI through GLEIF. A3
files the registrant-first combined name, the registrant legal address and ZIP,
and `Investment Company` together, and only when the SEC fund evidence names
the same CIK, series and class. Otherwise all four cells stay manual. An ETF
never uses an exact-ISIN GLEIF record, and its ISIN is not sent to GLEIF. An
ETF with an `[[fa.entities]]` entry gets no automatic cell, whatever its
`prefer_configured_*` flags say; a cell the entry leaves out, including the
name, stays manual ([ADR 0011](docs/decisions/0011-robinhood-etf-registrants.md)).
The address is the exception: an ETF entry without `address` stops Schedule FA
as above (exit `4`).

### Activity Statement export settings

Export the Activity Statement as CSV with IBKR's "Hide Details for
Positions, Trades and Client Fees Sections?" setting left at **Yes**, the
standard layout. With details shown, Open Positions gains an `Open` column
and Trades adds a row per fill and per closed lot. An Open Positions header
with an exact `Open` column or a Trades `Data` row of kind `Trade`,
`ClosedLot` or `ClosedLot*` refuses (exit code 4) and asks you to re-export
it. The fees and costs the tool needs, each trade's
`Comm/Fee` and `Basis`, remain in the standard layout.

Use the downloaded CSV, Activity Statement or Flex Query, unedited. Opening
it in an editor or spreadsheet and saving it again can re-encode it: a file
that starts with a garbled byte-order mark (the text `ï»¿`) refuses (exit
code 4), since other parts of it may be damaged too. Download it from IBKR
again.

### Activity Statement date formats

IBKR exports the per-row dates in the Dividends, Withholding Tax, and
Interest sections in one of two shapes, depending on the account's
date-format preference:

- `DD-MM-YYYY` — the IBKR-India flavor (e.g. `18-09-2025`)
- `YYYY-MM-DD` — the IBKR-LLC / US flavor (e.g. `2025-12-31`)

Both are accepted automatically — you don't need to convert or re-export.
(Trades use a distinct `YYYY-MM-DD, HH:MM:SS` datetime and have always
been ISO.) A US-person account (`us_person = true`, W-9 on file) whose
statement is ISO-dated and shows `$0` NRA withholding on US-source
dividends is the expected, correct shape — the CLI surfaces the
`FTC-via-US-Form-1040` advisory (see issue #145), since the foreign-tax
credit for such accounts runs through the US 1040 + India–US treaty
resident-sourcing rules, not Form 67 / Schedule TR.

Every parsed execution and cash-event date (each Trades `Date/Time`, and
each Dividends, Withholding Tax and Interest `Date`) must fall inside the
statement's own `Statement.Period`, both ends included. A row dated
outside it contradicts the period, so the run refuses (exit code 4),
naming the section, row and column; re-export the Activity Statement for
one period.

## `schedule-fa` subcommand

```bash
uv sync                                                       # install
uv run ibkr-fa schedule-fa \
    --input flex.csv \
    --config user.toml \
    --output review.csv \
    --calendar-year 2025
```

To produce **every** schedule in one go (recommended), use `run-all`. It
runs the six financial-year schedules from `--fy-input` and Schedule FA
from `--cy-input` (a statement ending Dec 31 of the calendar year, see
[Activity Statement period](#activity-statement-period); omit it to skip FA), writes
one file per schedule into `--output-dir`, and prints a status-first
summary. Skipping FA also deletes any `schedule-fa.csv`/`.xlsx` or
`schedule-fa-a2.csv`/`.xlsx` an earlier run left in `--output-dir`, so an
older Schedule FA never sits beside this run's files:

```bash
uv run ibkr-fa run-all \
    --fy-input  activity_FY2025-26.csv \
    --cy-input  activity_CY2025.csv \
    --financial-year 2025-26 \
    --itr-form ITR-2 \
    --config user.toml \
    --output-dir ./itr-2025-26
```

For each lot in your IBKR Flex Query or Activity Statement that
overlapped the target calendar year, the tool produces one row of a
review CSV. Column headers match the e-filing portal's Schedule FA
Table A3 form verbatim, so you can copy each row across into the
portal field by field. Audit columns on the right preserve the USD
source values, FX rates, and dates so you can independently recompute
every INR figure.

v1 computes **`Initial value of the investment`** and **`Closing balance`**
per lot, using SBI TT-Buying rates on the acquisition date and on Dec 31
of the calendar year respectively. For Activity Statement inputs that
include a `Dividends` section with in-CY payments, v1 also computes
**`Total gross amount paid/credited`** (column 10) per holding,
totaling the in-CY USD payments and converting them at the December 31
SBI TTBR. Peak value is selected in native currency from fetched daily
prices and then converted at the selected date's TTBR. Closed-lot gross
proceeds use proven pre-fee consideration, or Robinhood's recorded sale
`Amount` (#1383), and the December 31 TTBR; other sources that do not prove
it (IBKR Flex) remain manual-fill, and NetBenefits ESPP refuses any lot sold
in the calendar year (exit 4). Any value that
cannot be established with certainty remains blank, and the CLI
identifies the required manual fill.

The [Schedule FA automation and manual-fill matrix](docs/portal/schedule-fa-manual-fill.md)
shows each source's computed, configured, manual, and refused fields.

> **FX-policy note.** `ibkr-fa` uses the SBI TTBR on the acquisition
> date, peak date, and December 31 for Schedule FA investment,
> peak, closing, and all gross-credit/proceeds values respectively. Peaks are selected in
> native currency before conversion unless the warned
> `schedule_fa.peak_currency = "inr"` override is used. Some
> practitioners apply Rule 115(1), Explanation (2)(f), by
> analogy with Schedule CG and use the preceding-month-end TTBR
> instead. Both readings are defensible; the disagreement is confined
> to Schedule FA (Schedule CG is identical). See
> [`docs/portal/fx-policy-choice.md`](docs/portal/fx-policy-choice.md)
> for a worked example and what to do if your CA prefers the
> Rule-115-by-analogy interpretation.

### Activity Statement period

An Activity Statement for `schedule-fa` must end on Dec 31 of the calendar
year and start on or before Jan 1. Schedule FA rebuilds each holding from its
opening trades, so a holding bought in an earlier year needs those years too:
export the statement from Jan 1, or from your account's first trade if that is
earlier, through Dec 31. The tool
files only the calendar year's holdings, sales, dividends and interest from
it. A statement that ends on another day or starts after Jan 1 refuses (exit
code 4), and so does one that leaves out a holding's opening trades.

### Required Flex Query sections

In your IBKR Flex Query configuration, enable:

* **Account Information** — drives the account-number cross-check that
  prevents accidentally filing someone else's holdings.
* **Open Positions (Level of Detail = LOT)** — the year-end snapshot. The
  Flex Query's `ReportDate` must equal Dec 31 of the calendar year you're
  filing for.
* **Trades, with Closed Lots** — always required for Schedule FA. Enable the
  "Closed Lots" checkbox inside the Trades section: a holding sold before
  Dec 31 reaches Table A3 only through its `CLOSED_LOT` rows. A Flex Query with
  no Trades section is refused. A Trades section with a header and no rows
  means nothing was traded. For each ISIN, the closing executions'
  quantities (`Open/CloseIndicator` `C`), without their signs, must add up
  exactly to its `CLOSED_LOT` quantities, and every execution's
  `Open/CloseIndicator` must be `O` or `C`; otherwise the input is refused.

The Flex CSV states its period only in IBKR's header and trailer records, so
they are required too: in the Flex Query's delivery settings, turn on
**Include header and trailer records** (IBKR's Reporting Reference,
[Delivery Configuration and General Configuration](https://www.ibkrguides.com/reportingreference/reportguide/delivery%20configuration%20and%20general%20configuration.htm)).
Schedule FA reads the begin-of-file (`BOF`) record's from and to dates: the
period must start on or before Jan 1 and end on Dec 31 of the calendar year,
the same rule as the [Activity Statement period](#activity-statement-period),
and every `CLOSED_LOT` row's `TradeDate` must fall within it. A Flex CSV
without the records, with a shorter period, or whose records break a layout rule
the tool checks (one account, so one `BOF` record first and one `EOF` record
last) refuses (exit code 4), because a query over part of the year can leave
out a sale without saying so.

IBKR's [Flex Web Service](https://www.ibkrguides.com/orgportal/performanceandstatements/flex3.htm)
allows a from-date/to-date override of up to 365 days, so a Flex Query may not
cover a leap year such as 2024 from Jan 1 to Dec 31. If yours can't, use an
Activity Statement for that year instead.

Flex lots whose holding-period date differs from the open date are refused.
IBKR shifts that date for US wash sales, and the lot's Flex cost basis may then
include a US adjustment that the Indian return does not recognise. Run
`schedule-fa` on an IBKR Activity Statement that covers the lot's acquisition
instead, from Jan 1, or from your account's first trade if that is earlier,
through Dec 31 of the calendar year
(see [Activity Statement period](#activity-statement-period)); it rebuilds the
cost from the opening trades (Activity Statement rows coded `LD` are still
refused, #265).

### Anonymised Activity Statement exports (`--accept-anonymised-statement`)

IBKR's Activity Statement exporter has a privacy option that hides
the `Name` and `Account` rows from the Account Information section
(observed in real-world exports).
Without intervention the parser refuses such exports because the
account-ID cross-check needs the statement's Account row to verify
the file belongs to your `user.toml`. Pass
`--accept-anonymised-statement` (works on every IBKR subcommand:
`run-all`, `schedule-*`, and `form-67`) to
fall back to `[account].holder_name` and `[account].number` from
`user.toml`. When fallback fires, the CLI prints a loud warning that
the run is built on user-attested identity rather than statement-
verified identity — you are then responsible for confirming the
statement belongs to the same account as `user.toml` before filing.
Orthogonal to `--allow-masked-account` (which handles a present-but-
masked `U**********` Account row); both flags can coexist on the
same invocation.

### `schedule-fa` exit codes

| Code | Meaning |
|---|---|
| 0 | Success — no manual-fill needed |
| 1 | Generic / unexpected error |
| 2 | Argparse usage error (missing flag, invalid `--format`, etc.) |
| 3 | Success BUT manual-fill columns must be edited before paste; or a `cbdt-json-fragment` run was REFUSED because a required filed value (e.g. the A2 peak) is missing — supply it and re-run |
| 4 | Pipeline raised a documented error (bad config, bad CSV, a `--calendar-year` outside 1990–2099, missing FX rate, or `--output` points at a non-ibkr-fa file the JSON writer won't overwrite); or a `cbdt-json-fragment` run was REFUSED for a structural problem (unsupported ITR form or AY, no `[account.custodian]` Table A2, an account number not cross-checked against the statement, contradictory A2 rows) or a value the CBDT schema rejects |

#### `--format cbdt-json-fragment` (import into an offline ITR-2 or ITR-3 utility)

Instead of the review CSV, `schedule-fa --format cbdt-json-fragment --itr-form
ITR-2` (or `ITR-3`) emits ONE
JSON file containing both Table A2 and Table A3 for import into the official
offline utility. The form is explicit because a standalone fragment cannot infer
which return it will later enter. Because JSON import bypasses the human review the CSV
flow relies on, it is **all-or-nothing**: it emits a complete, schema-validated
fragment, or it REFUSES listing exactly what to fix — it never fabricates or
rounds away a filed figure. A missing filed value exits `3` (supply it and
re-run). A structural problem exits `4`, and so does a supplied value the CBDT
schema rejects (e.g. a ZIP over 8 characters); both are checked before missing
values. Structural problems are: an unsupported ITR form or assessment year, no
Table A2 account, an account number not cross-checked against the statement,
or contradictory A2 rows. So missing A2 is exit `3` for the review CSV but exit
`4` here. You must therefore configure `[account.custodian]`, a
`[[schedule_fa.manual_values]]` A2 peak for the year, and a `[[fa.entities]]`
ZIP + verified `nature_of_entity` per holding, and run against a
full-account-number statement (a masked/anonymised run is refused with exit
`4`).
The file carries a top-level
`_ibkrFa` provenance marker; use `ibkr-fa merge` to splice it into your return.

#### `ibkr-fa merge` (splice the fragment into your saved ITR-2 or ITR-3)

Hand-merging the fragment into an often-minified, multi-MB saved return risks
silently dropping the other Schedule FA tables (A1, A4–A10) or other-broker rows,
or duplicating on a re-run — a Black Money Act exposure. `ibkr-fa merge` does it
safely:

```bash
uv run ibkr-fa merge \
    --fragment schedule-fa.json \
    --itr      saved-itr.json \
    --output   merged-itr.json
```

It auto-detects `ITR.ITR2.Form_ITR2` or `ITR.ITR3.Form_ITR3`, verifies that the
fragment targets the same form, and splices Table A2 + A3 into that form's
`ScheduleFA`. Every value outside Table A2 and A3 is kept, each number at its
**exact** decimal value rather than a float approximation (a plain decimal such as
`-1500.50` keeps its text). The output is rewritten as indented UTF-8 JSON, so its
layout and bytes differ from your saved return; the inputs are never modified, and
`--output` must be a new path. It is **strict merge-once**: it REFUSES (exit 4)
rather than risk a wrong filing — the target's Table A3 must be empty and its
Table A2 must not already disclose this account, so you export a **clean** return
from the utility and merge once. The `--fragment` is treated as untrusted and
fully re-validated (a hand-edited fragment with an invalid date, a blank field, a
missing table, a negative Table A3 amount, a masked or non-ASCII Table A2 account
number, or a Table A2 peak or closing balance below the CBDT 14-digit bound is
rejected). The `_ibkrFa` marker is
stripped from the output. Re-import
`merged-itr.json` into the matching offline utility.

> Note: `merge` pins `Form_ITR2`/`Form_ITR3` to the matching `FormName`,
> `AssessmentYear == "2026"` (the bare start-year of AY 2026-27), and
> `SchemaVer == "Ver1.0"` (the literal pattern text in both vendored schemas,
> ITR-2 Main V1.2 and ITR-3 Main V1.1; `FormVer` is independent and not
> checked). A return saved against a different schema revision is refused (safe —
> it never mis-files); open an issue with your `SchemaVer` so it can be supported.

### ADR / GDR attribution

ADRs of Indian companies (INFY, HDB, IBN, WIT, RDY, etc.) land on
Schedule FA under **"United States of America"** because their ISINs
have the `US` prefix — they are legally US-issued depositary receipts,
not Indian shares. See [docs/portal/adr-handling.md](docs/portal/adr-handling.md)
for the full rationale, concrete examples, the contrarian
underlying-issuer interpretation, and how to leave them out of
Table A3 if your CA prefers that approach.

## `schedule-os` subcommand

```bash
uv run ibkr-fa schedule-os \
    --input activity-statement.csv \
    --config user.toml \
    --output schedule-os-review.csv \
    --financial-year 2025-26
```

`--financial-year` accepts both the canonical ITR-portal short form
(`2025-26`) and the long form (`2025-2026`); both normalise to the same
Indian Financial Year (Apr 1, 2025 – Mar 31, 2026). The input CSV's
`Statement.Period` field must contain the whole FY. It may start earlier
or end later, as `schedule-fsi` and the capital-gains commands allow, so
`run-all` can use one multi-year statement; only in-FY rows are filed.
Partial-year and calendar-year exports are rejected with a domain error. Flex Query
exports are also rejected; only Activity Statement inputs carry the
structured `Cash Dividend (Ordinary Dividend)` / `Special Dividend` /
etc. distribution-type labels the OS classifier needs.

For each supported `Cash Dividend` / `Special Dividend` / `Choice Dividend`
row, the tool emits one country-labelled detail row and combines every source
country into **Schedule OS 1a(i) "Dividend income [other than (ii) and
(iii)]"**. Rows 1a(ii) and 1a(iii) are reserved for explicitly evidenced
sections 2(22)(e) and 2(22)(f), which ordinary broker dividend rows do not
establish.

All ordinary dividends use the same Rule 115(1), Explanation (2)(e) conversion. Source country
is retained independently for FSI/TR/Form 67. The optional
`[os].schedule_os_indian_adr_routing` setting changes that source attribution
for mapped Indian-company ADRs; it does not change the 1a(i) total.

An in-FY Return Of Capital, Long-Term Capital Gain or Cash in Lieu
refuses with exit 4 and no output, whatever the issuer's country (#1310):
ibkr-fa can't yet file an IBKR return of capital, long-term capital gain
distribution or cash in lieu, and skipping the row, or counting it as a
dividend, could misstate income, capital gains, foreign tax credit or
Schedule FA. `schedule-fsi`, `schedule-tr`, `form-67` and `run-all`
refuse the same way. `schedule-cg` and `schedule-cg-ltcg` refuse such a
row on any date in the statement, since one before the FY can change the
cost of a lot sold in it, and `schedule-fa` refuses one dated in the
calendar year.
An in-FY Payment In Lieu Of Dividend refuses with exit 4 and no output
(#1281): it's other income, not a dividend, and ibkr-fa can't yet file
an IBKR payment in lieu as other income with its withholding. Leaving it
out could also credit any withholding against an ordinary dividend.
`schedule-fsi`, `schedule-tr`, `form-67` and `run-all` refuse the same
way. `schedule-fa` (Table A3 sums the row) and the
capital-gains commands are unchanged.
Novel IBKR distribution-type labels that match neither list fail fast
with `UnrecognisedDistributionTypeError` (exit code 4) — never silently
dropped. So does a known label whose description, after `SYMBOL(ISIN)`,
isn't `<kind> USD <amount> per Share`, optionally followed by
` (Ordinary Dividend)` (or `Cash in Lieu USD <amount>`): any other
qualifier, suffix, sign or second amount refuses rather than letting the
label alone decide. The rounded 1a(i) portal total is followed by the rounded 1b(ix)
interest total row; that row is the last one unless the 1e block follows.

### Missing income sections and `Change in NAV` (#1279)

IBKR leaves an empty section out of the export, so a missing
`Dividends`, `Withholding Tax` or `Interest` section reads as no income.
That's accepted only when the statement's `Change in NAV` summary, if
printed, doesn't contradict it. Each of its `Dividends`, `Withholding Tax`
and `Interest` lines must agree with its section to within 0.01, and a
line that isn't printed counts as zero. A `Payment In Lieu Of Dividends`
line, if printed, is checked against the `Dividends` section's
payment-in-lieu rows, and the `Dividends` line against the other rows.
The run refuses with exit 4 and writes nothing when a line is nonzero and
its section is missing, or differs from its section by more than 0.01.
It also refuses when a section has a row in a currency other than the
statement's base currency, since `Change in NAV` is in the base currency
and can't be checked against it. `schedule-fsi`, `schedule-tr`, `form-67`,
`schedule-fa` (#1324, every format, with or without `[account.custodian]`)
and `run-all` run the same check; `schedule-cg` and `schedule-cg-ltcg`
don't. Each section is checked over the statement's whole period, so a
`schedule-fa` statement that starts before January 1 (#1288) is checked
before the calendar year's rows are picked. A statement without `Change in
NAV` isn't checked.

### Schedule OS row 1b(ix) interest income (issue #32)

In addition to the 1a(i) dividend total, `schedule-os`
aggregates foreign cash-balance interest credits from the IBKR Activity
Statement's `Interest` section (parsed in PR #186 / issue #139) into a
single rounded rupee total for Schedule OS row **1b(ix) "Others including
interest from Companies, NBFCs & HFCs"**. The total is surfaced at the bottom
of the verbose `schedule-os` stdout:

```
Schedule OS / 1b(ix) Others including interest from Companies, NBFCs & HFCs: ₹<N>
```

The user manually enters the rounded INR total into the portal 1b(ix)
cell. The review file carries the same total as its 1b(ix) row, right
after the 1a(i) total, printed even when it is `0`; it has no per-credit
1b(ix) detail block. The stdout line only fires when there's at
least one in-FY interest credit; accounts whose cash balance never
crossed IBKR's minimum-balance threshold for credit interest emit
no stdout line.

Statute basis: foreign broker cash-balance interest paid by IBKR LLC
is "income from other sources" under the residuary clause of
Section 56(1) — NOT Section 56(2)(id) ("interest on securities" per
the narrow Section 2(28B) definition covering Government / debenture
coupons). Slab rate. FX conversion uses **Rule 115(1), Explanation
(2)(c)** ("income chargeable under the head ... 'Income from other
sources' (not being income by way of dividends and 'Interest on
securities')") — the SBI TT-Buying rate of **the last day of the
previous year of the assessee** (Section 3 "previous year" = the
financial year being reported, so Mar 31 of the FY-end year for an
Indian individual). **Every interest credit in the FY uses that
single fixed specified date**, NOT preceding-month-end (which is
Explanation (2)(e) for dividends / (2)(f) for capital gains). Implemented as
`Rule115OtherSourcesPolicy`, a subclass of `Rule115Policy` that
overrides `POLICY_KIND` (cites (2)(c)), takes `previous_year_end`
at construction, and overrides `_preceding_month_end` to return
that fixed date regardless of event_date. `MissingFxRateError`
cites the correct rule sub-clause and says "last day of previous
year {fy_end} for interest received", with no event date, so a
missing-rate failure is unambiguous.

The Interest section parser sorts every non-footer row into one group; the
first match wins (#905, #1280):

1. **Unproven:** the description contains `accrual`
   (case-insensitive), whatever the sign.
2. **Credit:** a positive amount whose description reads exactly
   `<CCY> Credit Interest for <Mon>-<YYYY>`, IBKR's credit-interest
   wording, such as `USD Credit Interest for Apr-2025`. `<CCY>` is the
   row's currency, `<Mon>` a three-letter English month (`Jan` to
   `Dec`). Only these are filed as interest (realized USD credits only).
3. **Unproven:** any other positive amount, such as a refund of debit
   interest or a label the parser doesn't know.
4. **Debit interest:** a negative amount whose description contains
   `debit interest` (case-insensitive). This is margin interest you
   paid, not income, so it's left out of Schedule OS, FSI, TR,
   Form 67 and Table A2.
5. **Unproven:** anything else, such as a negative correction to a
   credit, or a zero row.

An unproven row may change the interest you received without showing
by how much. `schedule-os`, `schedule-fsi`, `schedule-tr` and
`form-67` refuse such a statement, naming each row's file line, and
write nothing; `run-all` refuses as a whole when its `--fy-input` has
one. `schedule-cg` and Table A3 don't use interest and still run.
`schedule-fa` leaves Table A2's Interest amount blank for manual fill
(exit 3); `--format cbdt-json-fragment` refuses instead, as for any
unresolved A2 credit.

### Rule 115(1), Explanation (2)(e) FX policy

Indian tax law (Income-tax Rules, 1962 — Rule 115(1), Explanation (2)(e), preserved
by the Income-tax Rules, 2026) converts foreign dividend income to
INR at the SBI TT-Buying rate on **the last day of the month
immediately preceding the month in which the dividend is declared,
distributed or paid** by the company. In practice ibkr-fa keys off
the **payment date** because that is the only one of the three
events IBKR Activity Statements expose; for the typical case where
declaration, distribution and payment fall in the same month this
yields the same specified date the rule prescribes.
If that "specified date" is a Saturday, Sunday, or holiday on which
SBI did not publish a rate, the policy carries back to the most
recent prior published rate within the `[fx].max_carry_forward_days`
ceiling.

**Worked example:** a dividend paid on **2025-05-17** uses the
SBI TTBR for **2025-04-30** (the preceding month-end). A
$100 dividend × ₹84.25 = **₹8,425** for that payment.

If the preceding month-end falls on a non-publication day,
the carry-back resolves it; e.g., a dividend paid 2025-09-15
specifies 2025-08-31 (Sunday) and carries back to 2025-08-30
(Saturday → next published rate). When no rate is available
within the carry-forward window, the run fails with
`MissingFxRateError` (exit code 4) rather than silently
substituting a stale rate.

### `schedule-os` exit codes

| Code | Meaning |
|---|---|
| 0 | Success (the writer fills every TOTAL cell deterministically; no manual-fill mode) |
| 2 | Argparse usage error (missing flag, malformed `--financial-year`, etc.) |
| 4 | Pipeline raised a documented error (bad config, partial-FY period, missing FX rate, Flex Query input, unrecognised distribution type, etc.) |

`schedule-os` emits only `0/2/4` and never `3` — there are no manual-fill
columns in the OS review CSV. (Any other exit code, e.g. the Python
interpreter's default `1` on an uncaught exception, indicates a bug,
not an application-emitted status.) Safety warnings (cross-AY, masked
account, zero qualifying dividends, excluded-row counts) always
route to stderr regardless of `--quiet`. For `schedule-os`, `--quiet` emits
`<1a(i) ordinary dividends> <1b(ix) other interest>`. This intentionally
replaces the incorrect pre-#415 three-field format.
Under `--quiet`, `schedule-cg` and `schedule-cg-ltcg` print a single
scalar rounded-INR TOTAL (may be negative for a net loss).
`schedule-fsi`, `schedule-tr` and `form-67` print CSV with a header row
and one line per row; the header prints even when there are no rows.

### `schedule-os` audit columns: SBI TTBR source date

The OS review CSV emits **two** FX audit dates per row, not
one. The `_audit: FX specified date` column shows the
statutory preceding-month-end (always present, per Rule
115(1), Explanation (2)(e), under AY 2026-27 /
Rule 206(2)(b)(i) Sl. No. 5
under AY 2027-28+). The `_audit: SBI TTBR source date`
column shows the actual SBI archive row the rate was read
from — populated **only when the policy carried back**
because SBI did not publish on the specified date itself
(Sunday / public holiday) or the loader filtered the row
(zero-`TT BUY` filter at `ibkr_fa.fx.ttbr_loader`); blank
otherwise. Worked example: a dividend paid 2025-09-15 has
specified date 2025-08-31 (Sunday, absent from the SBI
archive), the carry-back resolves to 2025-08-30 (Saturday,
published, TT BUY 87.70); the row's specified-date cell
reads `2025-08-31` and the TTBR-source-date cell reads
`2025-08-30`, so an auditor can independently verify the
rate against the SBI row for Aug 30. For a no-carry-back
row (e.g., a Jun 2025 dividend with specified date
2025-05-31 Sat which is published), the TTBR-source-date
cell stays blank — a populated cell is the visual cue
that the specified date was absent from the archive.

> **Migration notes**:
>
> * Issue #160 inserted the TTBR-source-date column between the
>   then-existing `Rule 115 specified date` and
>   `Rule 115 TTBR (INR/USD)` columns. If you have downstream
>   tooling that references CSV column indices by position
>   rather than header name, columns after the inserted one
>   (TTBR, full-precision INR, rounded ₹) have shifted right
>   by one. Total column count is now 11 (was 10).
> * Issue #207 renamed the three audit columns from
>   `_audit: Rule 115 specified date` /
>   `_audit: Rule 115 TTBR source date` /
>   `_audit: Rule 115 TTBR (INR/USD)` to citation-neutral
>   wording (`_audit: FX specified date` /
>   `_audit: SBI TTBR source date` /
>   `_audit: SBI TTBR (INR/USD)`) so the same headers work
>   under both AY 2026-27 (Rule 115) and AY 2027-28+
>   (Rule 206). Downstream tools that match column names by
>   exact text (rather than by position or `_audit:` prefix)
>   need to update the matched strings; column count and
>   positions are unchanged.

## `schedule-cg` subcommand

```bash
uv run ibkr-fa schedule-cg \
    --input activity-statement.csv \
    --config user.toml \
    --output schedule-cg-review.csv \
    --financial-year 2025-26 \
    --itr-form ITR-2
```

Produces a Schedule CG STCG review CSV for ITR-2 A5e or ITR-3 A6g
(foreign-equity short-term capital gains). For each closed lot in the
Activity Statement's `Trades` section whose FIFO holding period is
≤ 24 months (the Section 2(42A) threshold for foreign equities), the
tool emits one detail row in the review CSV with the realized gain
converted to INR. Long-term closures (holding > 24 months) and
out-of-FY closures are partitioned into audit blocks but excluded from
the STCG aggregate.

Long-term capital gains (Schedule CG Section B) are produced by the
sister `schedule-cg-ltcg` subcommand, not here. Indian-source disposals
(domestic CG) belong on a different Schedule CG section and are out of
v1 scope. The Activity Statement parser refuses any `Trades` row for an
instrument whose ISIN starts with `IN`, whatever its date, so the run
writes nothing and exits `4`.

Like `schedule-os`, `schedule-cg` requires an Activity Statement
input (Flex Query rejected), and the export must *cover* the FY but
can extend earlier and later. For `schedule-cg` it typically should: FIFO lot
matching against closing trades requires the corresponding opening
trades, which may have been acquired years before the reporting FY;
re-export from your account's first trade, or from Apr 1 of the FY if
that is earlier, so the statement holds the opening trade of every lot
held during the FY, including lots sold in it. A multi-year (e.g.
2020-2026) export is normal for Schedule CG.
The FIFO-closure matching implemented in #104 is the foundation —
`schedule-cg` is downstream of the `closed_lots` stream the
`Trades` parser produces.

### Rule 115 capital-gains FX policy

Income-tax Rules, 1962 Rule 115(1), read with Explanation (2)(f),
prescribes the specified date for income chargeable under "Capital gains" as
specified date is **the last day of the calendar month immediately
preceding the month in which the capital asset is transferred** —
i.e., the transfer-event's preceding month-end. Both the cost basis
(in USD) and the proceeds (in USD) are converted at the **same
transfer-date TTBR**, and the realized INR gain is the difference at
that rate.

**Why not a "double conversion" (acquisition-date FX for basis,
transfer-date FX for proceeds)?** The special conversion/reconversion
pathway is the **first proviso to section 48 plus Rule 115A** and applies only to
non-residents transferring shares or debentures of an **Indian**
company. The second proviso is indexation. For a resident assessee
transferring ordinary foreign-currency-cost shares (the v1 scope of
`schedule-cg`), Rule 115(1), Explanation (2)(f), applies: single
conversion at the transfer date.

**Worked example:** 10 shares opened 2025-05-15 (USD basis 1500)
and closed 2025-11-20 (USD proceeds 1800, USD gain 300). The
specified date is **2025-10-31** (preceding month-end of
November); the bundled SBI TTBR for that date is the only FX rate
used. Realized INR gain = (1800 − 1500) × TTBR(2025-10-31). Both
`cost_basis_inr` and `proceeds_inr` in the review CSV are derived
at the same rate.

If the preceding month-end is a non-publication day, the policy
carries back to the most recent prior published rate within the
`[fx].max_carry_forward_days` ceiling (same algorithm as
`schedule-os`).

### Schedule CG audit columns: SBI TTBR source date

The STCG and LTCG review CSV/XLSX show two FX audit dates per row,
as `schedule-os` does. The
`_audit: Transfer preceding-month-end date` column shows the
specified date. The `_audit: SBI TTBR source date` column right
after it shows the SBI archive row the rate was read from. It is
filled **only when the policy carried back**, because SBI did not
publish on the specified date or the loader filtered a zero
`TT BUY` row, and blank otherwise. Worked example: a sale on
2025-12-10 has specified date 2025-11-30 (Sunday, absent from the
SBI archive). The carry-back resolves to 2025-11-29 (Saturday,
TT BUY 88.95), so the row reads `2025-11-30` and `2025-11-29`. A
sale in November 2025 uses 2025-10-31, which SBI published, so its
source-date cell stays blank. The same columns appear wherever these
writers run: `schedule-cg`, `schedule-cg-ltcg`, `run-all`,
`netbenefits-cg`, `netbenefits-all`, `fidelity-cma-cg`, and the
`robinhood-cg --debug` / `robinhood-all --debug` audits. Filing
outputs don't change.

> **Migration note**: issue #741 inserted the source-date column
> between the specified-date and `_audit: Transfer SBI TTBR (INR/USD)`
> columns. Rows now have 16 cells (was 15). The TTBR, INR, cost-basis
> source and rounded-gain columns shift right by one, and both TOTAL
> amounts move from column 15 to column 16. Exclusion-block rows keep
> their positions and gain one empty trailing cell.

### Schedule CG review files: Table F date bands

ITR-2 and ITR-3 Schedule CG part F ("Table F") asks for each kind of
capital gain in five date bands, (i) "Upto 15/6" to (v) "16/3 to 31/3",
and the portal refuses a return whose row doesn't add up to Schedule
BFLA ([docs/portal/schedule-cg.md](docs/portal/schedule-cg.md)). Every
STCG and LTCG review CSV and XLSX ends with a Table F block just before
its audit TOTAL row, so the portal TOTAL row is still the last row: a
blank row, a `_table_f:` marker row, five band rows of Table F row 3
(STCG) or row 5 (LTCG), then a note row. A band row's first cell is its
label, such as `Schedule CG / F / 3(i) Upto 15/6: Short-term capital
gains taxable at applicable rates. Enter value from item 3v of schedule
BFLA, if any.`, and its last cell, under the TOTAL column, is the band's
whole rupees (a number in the XLSX). The block is always written: a
section with no in-FY sale or a net loss shows five zeros. The
same block appears wherever these writers run: `schedule-cg`,
`schedule-cg-ltcg`, `run-all`, `netbenefits-cg`, `netbenefits-all`,
`fidelity-cma-cg`, and the `robinhood-cg --debug` / `robinhood-all
--debug` audits.

The bands are this file's sales alone, net by sale date, before any
set-off. The note row says to add the same row's band amounts from
every other capital-gains file, including Robinhood's and any Indian
ones, and then to lower the bands for any capital loss set off against
them, so the row totals Schedule BFLA item 3v or 3vii (ITR-3: 5viii or
5x). A band is blank, with the reason in its second cell, when the
section's filed result is positive, a date band nets a loss and two or
more bands net gains. No source says which gain band the loss lowers, so
the filer chooses. The command then prints `⚠️  MANUAL-FILL: Schedule CG
Table F row 3 needs its date bands entered by hand: a band nets a loss
and two or more bands net gains; schedule-cg.csv leaves them blank.`
(row 5 for the LTCG file) to stderr, also under `--quiet`, and exits 3.
This applies to `schedule-cg`, `schedule-cg-ltcg`, `fidelity-cma-cg` and
`netbenefits-cg`, and to `run-all` and `netbenefits-all` through them.
Robinhood's debug copies print nothing new: the run report already names
the blank row (exit 3).

> **Migration note**: issue #1361 inserted the eight Table F rows
> before the audit TOTAL row of every Schedule CG review CSV and XLSX.
> The audit TOTAL is now the second-to-last row rather than the row
> after the last exclusion block. The portal TOTAL row is still the
> last row, and every other row and column is unchanged.

### `schedule-cg` exit codes

| Code | Meaning |
|---|---|
| 0 | Success (the writer fills every TOTAL cell and Table F band deterministically) |
| 2 | Argparse usage error (missing flag, malformed `--financial-year`, etc.) |
| 3 | Success BUT the Table F date bands are blank because a band nets a loss and two or more bands net gains; enter them by hand (see above) |
| 4 | Pipeline raised a documented error (bad config, partial-FY period, missing FX rate, Flex Query input, FIFO basis mismatch, etc.) |

Net STCG losses (TOTAL < 0) are valid Schedule CG inputs (Section 74
carry-forward, 8 AYs). The review CSV escapes leading `-` with a
single quote (OWASP CSV-injection mitigation) so Excel strips the
apostrophe on display while raw CSV readers receive the literal
`'-1500` text; a `--quiet` invocation returns the filed INR TOTAL
as a single line (positive or negative) suitable for shell-script
consumption. Safety warnings (cross-AY, masked account,
LTCG-deferred / IN-source / out-of-FY exclusion counts, net-loss
advisory) and the Table F manual-fill line always route to stderr
regardless of `--quiet`.

## `schedule-cg-ltcg` subcommand

```bash
uv run ibkr-fa schedule-cg-ltcg \
    --input activity-statement.csv \
    --config user.toml \
    --output schedule-cg-ltcg-review.csv \
    --financial-year 2025-26 \
    --itr-form ITR-2
```

Produces a Schedule CG LTCG review CSV for ITR-2 B8e or ITR-3 B9e
(foreign-equity **long-term** capital gains under the new regime).
Each closed lot in the Activity Statement's `Trades` section whose
FIFO holding period is **> 24 calendar months** (the Section 2(42A)
threshold for foreign equities) AND whose disposal date is on or
after **2024-07-23** (the Finance (No. 2) Act, 2024 cut-in for the
new 12.5%-without-indexation regime) is emitted as a detail row
with the realized gain converted to INR via Rule 115(1), Explanation
(2)(f). Short-term
closures (handled by `schedule-cg`), pre-cutover disposals (old
regime; 20% with indexation via CBDT Cost Inflation Index data —
out of v1 scope), and out-of-FY closures are partitioned into audit
blocks but excluded from the LTCG aggregate. Indian-source disposals
are out of v1 scope too: as with `schedule-cg`, a `Trades` row for an
instrument whose ISIN starts with `IN` makes the run write nothing and
exit `4`.

The FIFO-closure matching from #104 and the Rule 115 single-
conversion FX semantics are identical to `schedule-cg` (STCG). Same
multi-year Activity Statement requirement (the export must cover
the FY, and should start at your account's first trade, or at Apr 1
of the FY if that is earlier, so it holds the opening trade of every
lot held during the FY, including lots sold in it).

### Pre-cutover (old regime) handling

Disposals strictly **before 2024-07-23** fall under the old regime
(20% with indexation), which requires CBDT-published Cost Inflation
Index (CII) data to inflate the historical cost basis. CII data is
not yet bundled with `ibkr-fa`; the affected closures are visible
in the review CSV's `_excluded_pre_2024_07_23:` audit block (so the
user can confirm their existence) but are NOT emitted into the
LTCG TOTAL. The user must manually add them to the Schedule CG
filing using the old-regime computation, with the user's own CII
lookup.

For FY 2025-26 onwards the cutover is irrelevant — all FY 2025-26
disposals are post-2024-07-23 by definition. The pre-cutover audit
block exists for FY 2024-25 filings (and any historical / cleanup
filings using the same toolchain).

### `schedule-cg-ltcg` exit codes

Same as `schedule-cg`: 0/2/3/4, with exit 3 for a blank Table F row 5.
Net losses are accepted (Section 74
8-AY long-term-loss carry-forward — note that long-term losses can
only set off against long-term gains in subsequent AYs, NOT
against short-term gains).

### Section 112 vs 112A (foreign LTCG ≠ Indian listed equity LTCG)

Every row this subcommand emits is taxed under **Section 112** of
the Income-tax Act, 1961 (12.5% flat without indexation post-
2024-07-23, **no ₹1.25 lakh exemption**). It is NOT Section 112A.
Section 112A is the special regime for LTCG on Indian listed
equity on which STT has been paid; the ₹1.25 lakh exemption per
AY is the §112A(2) sub-clause and applies ONLY to that section.

Paste this run's TOTAL into **ITR-2 B8e** or **ITR-3 B9e**, as selected
by `--itr-form`. Do **not** enter it on the Section 112A row
(the row that captures LTCG on STT-paid Indian listed equity).
Entering it in the §112A row would silently apply the ₹1.25 lakh
exemption to a liability where it does not apply, under-paying
tax — the under-payment is discoverable on AIS/26AS reconciliation
but filing-time CBDT validation will not catch it. The CLI
surfaces a loud advisory on every `schedule-cg-ltcg` run that
writes ≥ 1 row to make the distinction impossible to miss.

The equivalent advisory fires on `schedule-cg` for foreign STCG
(every row is taxed at the assessee's slab rate under the
regular Chapter IV-D provisions, NOT Section 111A flat-rate —
§111A was 15% before 2024-07-23 and 20% on or after, per Finance
(No. 2) Act, 2024; foreign STCG itself has no special-rate
section, since §111 of the Act is about Recognised Provident
Fund accumulated balances, not residual STCG).

## Fidelity NetBenefits (Microsoft ESPP) subcommands

`netbenefits-fa` and `netbenefits-income` cover a Microsoft employee's
**Fidelity NetBenefits ESPP** holding — a separate input pipeline from the
IBKR ones, reading the Fidelity portal export (the open/closed-lots CSV plus
the statement and trade-confirmation PDFs) instead of an IBKR statement. The
design is recorded in [ADR 0003](docs/decisions/0003-fidelity-netbenefits-espp.md).

Both read the bundle directory with `--input <dir>`, including every folder
under it except linked folders, which aren't followed. A folder in it that
can't be read or searched refuses the run.

```bash
ibkr-fa netbenefits-all \
  --input private/netbenefits \
  --config user.toml \
  --output-dir out/netbenefits \
  --financial-year 2025-26 \
  --itr-form ITR-2
```

`netbenefits-all` creates `--output-dir`; `netbenefits-income` and
`netbenefits-cg` need it to exist.

For a NetBenefits-only filing, `[account].number` may be omitted; it is an
IBKR account ID and must never be filled with the Fidelity Participant Number
or a dummy. IBKR statement commands still require and cross-check it.

- `netbenefits-fa --input DIR --config user.toml --calendar-year YYYY --output schedule-fa-a3.csv` defaults
  to the portal-shaped `filing-csv`: **Schedule FA Table A3** (the held ESPP
  shares) plus sibling **Table A2** (the Fidelity custodial account itself) at
  `schedule-fa-a2.csv`. With `--quicko-workbook` (`netbenefits-all` takes it
  too) it also writes a separate Quicko
  **Foreign → Shares & Securities → Other Broker** workbook at
  `schedule-fa-quicko.xlsx`. Quicko Purchase Value is
  the A3 initial value: the FMV at purchase converted at the acquisition-date
  TTBR; Peak / Fair Market / Sale / Income use the corresponding A3 values. The
  workbook does not replace either ITD table upload. Put Quicko's downloaded
  `Personal+Assets+Foreign+Shares+And+Securities+Template.xlsx` in the
  NetBenefits input directory; the tool fills that exact workbook package
  because a newly-created XLSX is rejected during Quicko processing. If the
  requested workbook cannot be written, including when the template is
  missing, the run refuses (exit `4`) and removes Tables A2 and A3 as well.
  `--quicko-workbook` needs `filing-csv`; with any other `--format` the command
  refuses (exit `2`). Use
  `--format review-csv` for the audit columns. A3's
  **initial value** is the confirmation FMV converted at the acquisition-date
  TTBR; it is distinct from the future §49(2AA) capital-gains basis. A2 closing
  is the held securities plus a *proven-zero* Dec-31 cash position. The A2
  **peak balance is manual-fill** (monthly
  statements carry no daily account NAV), so the command exits `3`
  (manual-fill required). It also prints an **ESPP salary-perquisite advisory**
  (verify the §17(2) perquisite is in your Form 16 — advisory only, not a
  filed figure). With `--format cbdt-json-fragment` (and a configured
  `[[netbenefits.a2_peak_overrides]]` peak for the year) it instead emits the
  **importable ITR-2 or ITR-3 Schedule-FA JSON** (A2 + A3; selected with
  `--itr-form`) and exits `0`.
- `netbenefits-income --input DIR --config user.toml --financial-year YYYY-YY --itr-form ITR-2 --output-dir OUT` emits
  **Schedule OS** (1a(i) ordinary dividend), **FSI**, **TR**, and **Form 67**
  (the ESPP dividend and its US withholding → Foreign Tax Credit). Statements
  whose periods overlap must list the same dividends and withholdings for the
  days they share within the financial year; if one lists an event the other
  leaves out, the run refuses (exit `4`) and leaves no income output.

The review outputs (`netbenefits-fa --format review-csv`,
`netbenefits-income --debug`) add `_audit` columns: the Schedule FA
(Table A3 and A2) and Schedule OS files show USD amounts and SBI TTBRs;
the FSI, TR, and Form 67 files show full-precision INR but no USD amounts
or TTBRs. The calendar-year FA and financial-year income split mirrors the
IBKR subcommands (Schedule FA is a Dec-31 snapshot; OS/FTC are the Apr–Mar
financial year).

Emitting the **CBDT JSON Schedule-FA fragment** for the NetBenefits account
requires the A2 peak balance — the highest native-currency single-day account
value converted at that date's TTBR, which the monthly statements can't derive
— supplied via `[[netbenefits.a2_peak_overrides]]`
(bound to the Participant Number via `a2_peak_overrides_participant_no` so it
can't carry across a copied config). The
fragment covers ONLY the NetBenefits account; combine it with any IBKR Schedule
FA separately. (`ibkr-fa merge` splices one fragment into a saved return.)

**Schedule FA needs statements that prove the Dec-31 purchase is the only
lot.** `netbenefits-fa` supports an account opened in the calendar year whose
lots are all bought on Dec-31. The lots CSVs carry no download date, so they
can't show on their own that no other lot was bought and sold during the year.
`netbenefits-fa` (and `netbenefits-all`) therefore also read every statement in
`--input` and check them against every trade confirmation there. A statement
prints a purchase on its settlement date, so the check assumes every purchase
settles within ten days of its trade. It refuses (exit `4`) and leaves no output
when:

- a confirmation is dated before Dec-31, or the Dec-31 purchase's confirmation
  settles more than ten days after Dec-31;
- the Stock Plan Services reports don't cover, with no gap, the window from the
  later of Jan-1 and `netbenefits.account.account_opening_date` through **ten
  days after Dec-31**;
- on shared days through ten days after Dec-31, two Stock Plan Services reports
  list different ESPP purchase rows or counts, or a year-end report prints a row
  more times than an overlapping Stock Plan Services report (two year-end
  reports aren't compared);
- a purchase row settling on or before ten days after Dec-31, in any report, is
  neither the Dec-31 purchase's own row nor a later purchase's row matching its
  own confirmation in settlement date, identifier, price, quantity and amount;
- no report shows the Dec-31 purchase's own row; or
- a report ending on or before ten days after Dec-31 holds another security, or
  shares that differ from those of the matched purchases settled by its end
  beyond the Holdings rounding tolerance.

**Capital gains need statements that prove the FY had no sale.** The
closed-lots CSV carries no download date, so on its own it can't show that it
lists every sale in the FY. `netbenefits-cg` and `netbenefits-all` therefore
also read the Stock Plan Services statements in `--input`. Each statement prints
every buy and sell as a dated activity row, and the parser refuses a statement
holding any dated row it doesn't read, a sale or transfer included. Capital
gains are written only when:

- the statements (year-end reports excluded) cover, with no gap, the window from
  the later of the FY start and `netbenefits.account.account_opening_date`
  through **ten days after the FY end**. The extra days catch a sale traded by
  the FY end that settles a few business days later;
- `netbenefits.account.account_opening_date` is not after the FY end; and
- the closed-lots CSV lists no sale or transfer dated in the FY.

The result is then a proven zero: empty Schedule CG files, and no capital-gains
row in Schedule FSI. Anything else refuses (exit `4`) and leaves no output:
a gap, statements that stop before ten days after the FY end, an opening date
after the FY end, a statement holding a sale or transfer, or a closed-lots row
dated in the FY. Until ibkr-fa can check a sale against the statements (#1150),
an FY with a sale or transfer can't be filed through these commands. Lots
closed outside the FY are ignored.

**1042-S / Grantor Letter reconciliation.** The Form-67 FTC
withholding is derived from the statement's *Non-Resident Tax* lines — correct
and sufficient for filing. The *authoritative* annual figure is the US **1042-S**
(and the SPS **Grantor Letter**), but it is issued for a US **calendar** year,
typically the following year — after your Indian FY deadline — so **don't delay
filing for it**. When it arrives, cross-check via
`netbenefits-income --reconcile-1042s-fy-withholding-usd <USD>` (or the same
flag on `netbenefits-all`), passing the
**FY-allocated** portion of the 1042-S total (the part that falls in the Indian
Apr–Mar year, *not* the raw calendar-year box). A total that differs from the
statements **refuses** (exit `4`) and nothing is written: correct the FY
allocation, or find the dividend or withholding the statements miss. A matching
total only confirms the statement figure, which remains the filed amount.

### `[netbenefits]` configuration

```toml
[netbenefits]
espp_discount_pct = "15"        # the ESPP discount % (purchase price = FMV × (1 − pct))
participant_no = "I12345678"    # Fidelity Participant No.; cross-checked against every PDF
# Required only when [[netbenefits.a2_peak_overrides]] are present: re-declare the
# Participant Number so a manual peak can't carry across a copied config.
a2_peak_overrides_participant_no = "I12345678"

# Optional: pin a Dec-31 closing price when the year-end statement is unpriced
# (Fidelity leaves a just-bought lot unpriced at year-end). One block per date.
# price_date must be the last US market session on or before Dec 31
# (2025-12-31 for 2025); any other date within three days refuses. Per-share
# Dec-31 pricing, from an override or Yahoo, supports calendar year 2025 only.
[[netbenefits.price_overrides]]
price_date = 2025-12-31
price_per_share_usd = "312.45"
source = "SPS year-end statement"
reason = "12-31 lot unpriced in the CSV"

# Required for `--format cbdt-json-fragment`: select the highest native-currency
# single-day account value first, then convert it at that date's TTBR. One per CY.
[[netbenefits.a2_peak_overrides]]
calendar_year = 2025
peak_balance_inr = 57314         # whole rupees (integer; never rounded for you)

[netbenefits.account]            # the Fidelity custodial account (Schedule FA Table A2)
# The run warns when institution_name is Fidelity Stock Plan Services, whose participant
# agreement says it holds no account for you (ADR 0021): check which firm your
# NetBenefits documents say holds the account.
institution_name = "Fidelity Stock Plan Services LLC"
address = "245 Summer Street, Boston, MA"
zip = "02210"
country_iso2 = "US"
account_opening_date = 2025-02-19
status = "BeneficialOwner"       # BeneficialOwner | Joint
# Quicko's "Name of the deductor" (not part of A2): copy the withholding agent from
# your Form 1042-S, box 12d for tax in box 7a, or the agent box 14a names for tax
# in box 8. Without it, a run that writes a Quicko form leaves that value blank
# and exits 3.
withholding_agent_name = "National Financial Services LLC"

[netbenefits.security]           # the ESPP security identity (known constants; ADR-0003 §5)
symbol = "MSFT"
cusip = "594918104"
isin = "US5949181045"
entity_address = "One Microsoft Way, Redmond, WA"
entity_zip = "98052"
listing_exchange = "NASDAQ"
issuer_country_code = "US"
nature_of_entity = "Listed company shares"
```

## Fidelity Cash Management Account income

`fidelity-cma-income` reads Fidelity CMA monthly reports as the primary
transaction evidence and uses the matching year-end report and Form 1042-S only
as exact cross-checks. It emits `schedule-os.csv`, `schedule-fsi.csv`,
`schedule-tr.csv`, and `form-67.csv` (or `form-67-debug.csv` while Form 67 rows
are blank or the FTC is blocked; see `[form67]`):

```bash
ibkr-fa fidelity-cma-income \
  --input private/fidelity-cma/statements \
  --tax-forms private/fidelity-cma/tax-forms \
  --financial-year 2025-26 \
  --itr-form ITR-2 \
  --config user.toml \
  --output-dir out
```

`schedule-os.csv` is the Schedule OS filing worksheet: it lists the rounded
1a(i) dividend total and the rounded 1b(ix) Credit Interest total, each as an
amount to add. The CLI summary repeats the same 1b(ix) total, so enter it once.
The dividend review block is in `schedule-os-debug.csv`, which only `--debug`
writes. Schedule FSI includes both dividend and
interest in its `Other Sources` row. It also includes the FY's SPAXX capital
gains in its `Capital Gains` row, computed as `fidelity-cma-cg` computes them
(see [Fidelity CMA capital gains](#fidelity-cma-capital-gains)).

The statement directory must cover the FY through the following month (late
Credit Interest can post one month later). An FY with a SPAXX redemption also
needs the complete acquisition history that `fidelity-cma-cg` needs: statements
from before any carried SPAXX holding, every sale matched to earlier purchases,
and transactions that add up to every monthly holding. A redeemed in-FY lot held
over 24 months is refused as well. Any of these refuses the run (exit `4`)
before anything is written. An FY with no SPAXX redemption has no gain to prove
and doesn't need that history. The tax-form parser supports only the exact
observed 2025 packet layout; an unrecognized version refuses rather than
guessing.

SPAXX distributions are foreign-source ordinary dividends under Schedule OS 1a(i) and
India-US DTAA Article 10(2)(b), with the 25% portfolio-dividend cap. Fidelity
Free Credit Balance `Credit Interest` is foreign interest under Article 11.
Any US `interest-related dividend` treatment does not reclassify a SPAXX
distribution for Indian tax.

US withholding is not automatically eligible Indian FTC. Rule 128(5)(i) and
Rule 76(7)(a) ignore foreign tax above what the DTAA lets the US charge, and the
US charges dividends under its own law within the Article 10(2)(b) 25% limit.
The owner's adopted inference is that India credits only the US tax owed on each
payment, never withholding US law doesn't impose, such as on the exempt part of
an interest-related dividend under IRC §871(k)(1), whether or not you reclaim it.
The reasoning and quoted sources are in
[ADR 0004 §6](docs/decisions/0004-fidelity-cma-classification.md).
Select the known state independently for each US calendar tax year:

```toml
[fidelity_cma]
account_id = "Z99-999999"
spaxx_isin = "US31617H1023"
status = "BeneficialOwner"  # needed by fidelity-cma-fa

[[fidelity_cma.us_tax_years]]
tax_year = 2025
refund_status = "finalized"
provenance = "final IRS transcript and CA event allocation"
eligible_tax = [
  { payment_date = 2025-04-15, amount_usd = "0.10" },
  { payment_date = 2025-05-15, amount_usd = "0.00" },
]
```

Valid states are `unknown`, `not-filed-planned`, `filed-pending`, `finalized`,
and `no-refund-claim`; a missing year is `unknown`. Only `finalized` and
`no-refund-claim` can produce FTC. Each requires `provenance`, reconciled Form
1042-S/year-end evidence, and one whole-cent `eligible_tax` allocation for every
actual withholding date in that US tax year: the US tax owed on that payment.
An allocation may be zero but cannot exceed statement withholding, and the tool
never fills one in from gross withholding or Form 1042-S totals. Use
`no-refund-claim` for a year you won't ask the IRS to refund; it needs no US
return or IRS transcript, only evidence for the amounts. If a US refund later
covers tax already credited in India, report it as Rule 128(10) or Rule 76(14)
requires.

FTC is all-or-nothing for an Indian FY. If any year with in-FY withholding is
not `finalized` or `no-refund-claim`, or is not reconciled, the command still
emits gross income schedules but emits no FTC rows, writes `form-67-debug.csv`
in place of `form-67.csv`, and exits `3`. This version
reads only the US tax year 2025 Form 1042-S packet, so it can reconcile no other
US tax year: an FY with withholding dated in another US tax year (for FY
2025-26, January–March 2026) gets no automatic FTC, whatever that year's refund
status, and its Schedule FSI, Schedule TR and Form 67 must be completed by hand.
Missing US TIN or Indian-tax configuration
also exits `3` with an action-needed warning. A filing-complete run exits `0`.

### Fidelity CMA capital gains

`fidelity-cma-cg` FIFO-matches SPAXX purchases and dividend reinvestments
against redemptions, then writes `schedule-cg.csv` and the standard
`schedule-cg-ltcg.csv` audit companion:

```bash
ibkr-fa fidelity-cma-cg \
  --input private/fidelity-cma/statements \
  --financial-year 2025-26 \
  --itr-form ITR-2 \
  --config user.toml \
  --output-dir out
```

The statements must run through the end of the FY. They must start by the FY
start, or at an account opening during the FY: a first statement with no
carried balance, which shows a dash for its beginning account value. A later
first statement that carries a balance, even cash alone, refuses the run (exit
`4`), because an earlier sale in the FY could be missing; so does one that
starts after the FY ends.

The cost of each FIFO slice is the actual statement amount paid for its SPAXX
units; proceeds are the statement sale amount. Rule 115 converts both at the
transfer month's specified TTBR. The command does not assume that SPAXX remains
at USD 1 or that a redemption has zero gain.

For AY 2026-27, amended section 50AA's application to a US-organized money
market fund remains unsupported by controlling CBDT or judicial guidance.
A lot held 24 months or less is nevertheless short-term under either
interpretation. The command therefore emits those unambiguous STCG rows and
refuses any in-FY longer-held lot rather than choosing a section 50AA position.
Longer-held sales outside the requested FY remain audit history and do not
block the run.

Both CSVs carry the Table F block of the Schedule CG review files (see
[Schedule CG review files: Table F date bands](#schedule-cg-review-files-table-f-date-bands)).
When the STCG bands are blank because a band nets a loss and two or more
bands net gains, the run prints the manual-fill line and exits `3`.

### Fidelity CMA Schedule FA

`fidelity-cma-fa` reconstructs every SPAXX lot and the account's daily
end-of-day cash/holding value, cross-checks all month ends and the year-end
report, and by default writes A3 plus a sibling A2 review file. It needs
`status = "BeneficialOwner"` in `[fidelity_cma]`, the observed INDIVIDUAL TOD
registration; `fidelity-cma-income` and `fidelity-cma-cg` don't read it.

```bash
ibkr-fa fidelity-cma-fa \
  --input private/fidelity-cma/statements \
  --calendar-year 2025 \
  --config user.toml \
  --output schedule-fa.csv
```

`--format filing-csv` writes the same rows as the 12-column Schedule FA
upload files the ITR portal imports, A3 at `--output` and A2 beside it:

```bash
ibkr-fa fidelity-cma-fa \
  --input private/fidelity-cma/statements \
  --calendar-year 2025 \
  --config user.toml \
  --format filing-csv \
  --output out/cma/schedule-fa.csv   # A3; A2 goes to out/cma/schedule-fa-a2.csv
```

The upload has no audit columns, so read the A2 peak bounds below from
`review-csv` or `xlsx` output. A blank A2 peak in the upload is named as a
manual-fill cell, and the command exits `3`.

SPAXX daily NAV comes from Nasdaq's public mutual-fund history endpoint and is
stored in a provider-namespaced subdirectory of the standard auditable price
cache. Every statement execution and month-end NAV must match the fetched
series.

A3 peaks use the shared closing-value rule, except that a redeemed lot's
window also takes its redemption day: when that day alone would set the peak,
including every lot bought and redeemed on the same day, the peak is left blank
and the command exits `3`. `schedule_fa.peak_basis = "high"` is refused,
because the fetched sources do not establish what Nasdaq's `high` means for
SPAXX. See ADR 0004 §2.

The statements identify all postings but do not provide one timestamp/order
across their separate activity sections. The A2 audit columns therefore carry:

- `lower`: the highest proven native-currency start/end-of-day value;
- `upper`: the native-currency value obtained by placing same-day positive
  postings before debits. It may overstate the actual intraday peak.

Each bound is converted at its own date's TTBR, so the rupee order can differ
from the native order: the lower bound can convert to more rupees than the
upper. `schedule_fa.peak_currency = "inr"` instead takes each bound's highest
daily INR value.

By default the peak remains blank, a warning explains the ambiguity, and the
command exits `3`. No bound is silently selected. To attest and file one:

```toml
[fidelity_cma]
account_id = "Z99-999999"
spaxx_isin = "US31617H1023"
status = "BeneficialOwner" # observed INDIVIDUAL TOD registration
suppress_peak_balance_warning = true
peak_balance_bound = "upper" # native upper (may overstate) | lower (may understate)
```

Setting `suppress_peak_balance_warning = true` without choosing a bound is a
configuration error that explains both choices. Both bounds remain in the A2
audit columns after selection. When a bound is chosen and the lower bound
converts to more rupees than the upper, the command refuses with exit `4` and
writes no output; remove the two settings to get output with a blank A2
peak.

The first supplied statement date is used as the account-opening date only
when that statement proves no carried account or SPAXX balance; the CLI always
warns that this is statement-bundle-attested. A2's exact dividend, interest,
and redemption-proceeds breakdown is audited. Each non-zero nature gets its
own repeated A2 account row.

## Morgan Stanley StockPlan (Alphabet GSU) subcommands

`stockplan-income`, `stockplan-fa` and `stockplan-all` read the `.xlsx` reports
Morgan Stanley StockPlan exports for Alphabet GSUs (RSUs): the Dividend
Report, the Capital Gain/Loss Report and Vested Share Holdings. Only Alphabet
`GOOG` (`US02079K1079`) exports have been checked against a real export; any
other issuer refuses (#382). The exports each command accepts, every check
and refusal, the outputs and exit codes are in the
[StockPlan support contract](docs/stockplan-contract.md).

```bash
ibkr-fa stockplan-all \
  --input private/stockplan \
  --config user.toml \
  --output-dir out \
  --financial-year 2025-26 \
  --itr-form ITR-2
```

This run is never filing-complete. Its best result is exit `3`:

- Schedule FA Table A2 for the StockPlan account is always manual fill (#375).
- Schedule CG is never generated. When the Capital Gain/Loss Report proves no
  sale, its status line reads `not required (zero sales proven)`. Any sale
  makes the run refuse (exit `4`) and attempt to remove every output (#381).
  A cleanup failure names files that may remain; do not file them.
- Table A3 is a review CSV, with no upload file, CBDT JSON fragment or Quicko
  workbook.

Configure the holding under `[stockplan.security]` (`ibkr-fa init` writes an
annotated template), and set `[account].status`.

## What it isn't

* Not tax advice. Verify with your CA.
* Not a portal-submission tool — it never files anything. Form 67
  (`portal-csv`) and Schedule FA Tables A3/A2 (`filing-csv`) emit upload CSVs,
  and Schedule FA can emit a validated CBDT JSON fragment for `ibkr-fa merge`;
  every other schedule is still copied into the portal by hand (OS/FSI/TR also
  offer a portal-shaped `filing-csv` worksheet).
* Not multi-currency (#52). v1 supports USD-denominated holdings and USD-paid
  dividends only. `schedule-fa` accepts foreign-issuer ISINs (e.g. CA, IE)
  whose issuer country is in the bundled CBDT country table and rejects
  Indian-issuer (`IN`) holdings; `schedule-os` accepts foreign-issuer ISINs
  but the dividend itself must be paid in USD.
* Not all of Schedule FA. Covers Table A3 (equity & debt) and Table A2 (the
  foreign custodial account, when `[account.custodian]` is set). Tables A1
  (depository / bank accounts), A4 (insurance / annuity), A5 (financial
  interest in an entity), A6 (immovable property), A7 (other capital assets),
  A8 (signing authority), A9 (trusts) and A10 (other foreign income) are out
  of scope.
* Schedule OS output here is **gross** income: ordinary dividends
  (1a(i)), other interest (1b(ix)) and, where a source has it, other
  income by nature (1e). Any row 2f DTAA dividend or section 57(1)
  interest expenditure is yours to subtract. The foreign-
  tax-credit netting it feeds into — Schedule FSI, Schedule TR and CBDT
  Form 67, with §90 / 90A DTAA (and §91 unilateral) relief — is computed
  by the sister subcommands (see the `[form67]` configuration section above).
* Schedule CG output here covers supported **foreign-security STCG + LTCG**
  (the latter under the new regime: 12.5% without indexation,
  post-2024-07-23 disposals). Pre-2024-07-23 long-term disposals
  (old regime: 20% with indexation via CBDT Cost Inflation Index
  data) are visible in the LTCG audit block but not in the LTCG
  TOTAL — the user must compute and add them manually. Indian-source
  disposals belong on a different Schedule CG section and are out of v1
  scope, and no audit block holds them. The IBKR Activity Statement
  parser refuses any `Trades` row for an ISIN starting with `IN`, so
  `schedule-cg` and `schedule-cg-ltcg` write nothing and exit `4`. An
  in-FY Robinhood sale of a `[[robinhood.securities]]` entry with
  `issuer_country_code = "IN"` or an `isin` starting with `IN` is a
  Schedule CG blocker: `robinhood-cg` and `robinhood-all` write no
  Schedule CG file and exit `4`.

## Black Money Act disclosure reminder

The IBKR subcommands (`schedule-*`, `form-67`, `run-all`), `merge`, and the
`robinhood-*` subcommands print a **Black Money Act, 2015**
disclosure-reminder advisory: on stderr for `schedule-os`, `schedule-cg`,
`schedule-cg-ltcg`, `schedule-fsi`, `schedule-tr`, `form-67` and `run-all`
(once per run); on stdout, as part of the run report, for `schedule-fa`,
`merge` and `robinhood-*`. The advisory cannot be silenced via `--quiet`
because tax-safety diagnostics are non-quietable by design.

The Black Money (Undisclosed Foreign Income and Assets) and
Imposition of Tax Act, 2015 (Act 22 of 2015) penalises
**non-disclosure** of foreign assets and foreign income on the
return, with severity that scales independently of whether any
tax was actually evaded. The ibkr-fa-relevant sections, in the
order they bite a user who files an ITR but omits Schedule FA:

* **Section 43** — flat **₹10 lakh penalty** for filing an ITR
  but failing to disclose / inaccurately disclosing a foreign
  asset. Applies irrespective of the tax position. **Finance
  (No.2) Act, 2024 (effective 1 October 2024) replaced the
  earlier narrow ₹5 lakh foreign-bank-only carve-out with a
  broader ₹20 lakh aggregate-value carve-out covering ALL
  foreign assets other than immovable property** — §43 therefore
  does NOT bite where the aggregate value of undisclosed
  foreign assets (foreign equity, foreign bank balances,
  foreign mutual funds, etc., excluding foreign immovable
  property) does **not exceed ₹20 lakh at any time during the
  relevant previous year** (statutorily a peak-balance test
  against the PY, not a year-end snapshot against the AY).
  IBKR-held foreign equities count toward this aggregate; a
  USD-denominated portfolio that briefly peaks above ₹20L on
  intra-year FX or market moves falls outside the carve-out.
* **Section 50** — rigorous imprisonment of **6 months to 7
  years** plus fine, for *wilful* failure to disclose foreign-
  asset information in a furnished return. **Finance Act, 2026
  (Act 4 of 2026; Presidential assent 30 March 2026) inserted
  a matching ₹20 lakh aggregate-foreign-asset (other than
  immovable property) carve-out for §50 (and §49) prosecution,
  deemed to have been inserted with effect from 1 October 2024**
  — the §50 carve-out now mirrors the §43 carve-out at the
  same threshold and effective date. For the 17-month period
  between 1 October 2024 and 30 March 2026, prosecution under
  §§49/50 in sub-₹20L cases was already administratively
  foreclosed via **CBDT Instruction F. No. 285/46/2021-IT(Inv.
  V)/88 dated 18 August 2025** (issued under §84 BMA r/w §119
  IT Act).
* **Section 3** — flat **30%** tax on undisclosed foreign
  income / asset value. No exemption, no slab benefit, no
  deduction.
* **Section 41** — additional penalty of **300% of the §3 tax**
  (≈ 90% of the undisclosed value). The §3 + §41 combination
  works out to ~120% of the undisclosed value, on top of the
  §43 fixed ₹10 lakh and any §50 imprisonment.

Adjacent sections: §42 (₹10 lakh for failure to file the return
at all), §49 (6 months–7 years imprisonment for wilful failure
to file), §51 (3–10 years imprisonment for wilful tax evasion).

The disclosure obligation hooks on the **return-filing step**,
not on the underlying foreign transaction. Any Indian resident
with a foreign asset (foreign equity, bank account, mutual fund,
insurance policy, ...) must disclose it on ITR-2 Schedule FA
every AY, regardless of whether any income or capital event
occurred. **RNOR (resident-but-not-ordinarily-resident)**
assessees have a Section 2 carve-out (BMA §2's "assessee"
definition excludes "not ordinarily resident" per IT Act §6(6));
current Indian residents (the default for someone living in
India ≥ 2 years) are squarely in scope. Note that the
**2019 retrospective amendment** (Finance Act, 2019) extends
BMA to non-residents / NRIs in respect of foreign assets
acquired *while they were resident in India* — so a current NRI
who acquired their foreign holdings while resident may still
have a historical disclosure obligation. Consult a CA before
assuming non-resident status fully exempts you.

`ibkr-fa` produces the **mechanically correct** disclosure rows;
the **decision to file** the return is the user's. The advisory
is a safety reminder, not a substitute for CA consultation.

## ITR form selection (ITR-2 vs ITR-3)

The same subcommands (except `merge`) also print an ITR-form selection
advisory (alongside the BMA reminder above). Foreign-asset /
foreign-income holders cannot use ITR-1 (Sahaj) or ITR-4
(Sugam); both form headers bear the foreign-asset exclusion on
their face: *"Not for an individual who … has assets (including
financial interest in any entity) located outside India"*. The
two applicable forms are:

* **ITR-2** — default for an `ibkr-fa` user. Use if your income
  consists of salary + house property + capital gains + foreign
  assets / income + other sources, with **no** business or
  profession income.
* **ITR-3** — required if you ALSO have business or profession
  income. Common cases ibkr-fa users overlook: intraday equity
  trading (speculative transactions under §43(5), generally
  reported as speculative business income), F&O / derivatives
  trading (business income, but foreign-exchange derivatives do not
  automatically satisfy §43(5) proviso (d)'s prescribed/notified-exchange
  condition), freelance / consulting income, professional fees,
  partnership-firm distributive share. A presumptive-scheme
  filer under §§44AD/44ADA/44AE who would otherwise use ITR-4
  also falls back to ITR-3 once they hold foreign assets.

Long-term and short-term capital gains from **delivery-based**
equity (what `ibkr-fa` processes) are *typically* **not**
business income and remain in ITR-2 territory **when treated as
capital assets / investments**. CBDT Circular 6/2016 recognises
that listed shares can ALSO be held as stock-in-trade — in which
case the gains become business income and ITR-3 applies. If you
actively trade your foreign equity as a business (frequency,
intent, treatment in your books), use ITR-3 even on
delivery-based activity. The intraday / F&O / freelance /
consulting categories always push you to ITR-3.

`ibkr-fa` cannot tell from your IBKR statement alone whether you
have business income elsewhere. When in doubt, consult a CA.

## Architecture

* [`docs/architecture.md`](docs/architecture.md) — high-level architecture and
  runtime sequence diagrams (Mermaid).
* [`docs/portal/schedule-fa-a3.md`](docs/portal/schedule-fa-a3.md) —
  authoritative spec for the Schedule FA Table A3 portal labels,
  validation rules, and data types v1 targets. Sourced from the
  official ITR-2 Excel utility for AY 2025-26 with a re-verification
  checklist for future AYs.
* [`docs/portal/schedule-os.md`](docs/portal/schedule-os.md) — pinned
  portal-label evidence trail for the `schedule-os` review-CSV TOTAL
  row, with the verification procedure for AY 2026-27 / AY 2025-26.
* [`docs/portal/schedule-cg.md`](docs/portal/schedule-cg.md) — pinned
  portal-label evidence trail for the `schedule-cg` review-CSV TOTAL
  row (foreign STCG, Schedule CG Section A), with the same verification
  procedure.
* [`docs/portal/schedule-cg-ltcg.md`](docs/portal/schedule-cg-ltcg.md) —
  pinned portal-label evidence trail for the `schedule-cg-ltcg`
  review-CSV TOTAL row (foreign LTCG, Schedule CG Section B, new
  regime: 12.5% without indexation per Finance (No. 2) Act, 2024).
* [`docs/portal/form-67.md`](docs/portal/form-67.md) — the Form 67 upload
  file: its columns against the form's visible fields, its rows and dialect,
  and the importer warnings.
* [`docs/stockplan-contract.md`](docs/stockplan-contract.md) — the
  Morgan Stanley StockPlan support contract: accepted exports, checks,
  refusals, outputs, exit codes and validation provenance.
* [`docs/decisions/`](docs/decisions/) — Architecture Decision Records with
  dated rationales for non-obvious choices.

## License

Copyright (C) 2026 Joel Tony

`ibkr-fa` is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version (`AGPL-3.0-or-later`). See [LICENSE](LICENSE) for the full text.

Bundled third-party data keeps its own terms; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
