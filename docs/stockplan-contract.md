# Morgan Stanley StockPlan support contract

This page is the contract for the three StockPlan commands, `stockplan-income`,
`stockplan-fa` and `stockplan-all`. It lists the exports they accept, every
rule they check, what they write, what they leave to you, and how they exit.
Anything this page doesn't list as accepted is refused (exit `4`).

**No StockPlan run is filing-complete today.** Schedule FA Table A2 is never
generated, so a successful `stockplan-fa` or `stockplan-all` exits `3`
(manual fill), never `0`. Schedule CG is never generated: `stockplan-all` can
prove that no sale happened, and refuses when one did.

## Validation provenance

| Level | What it covers |
|---|---|
| Verified against a real export | Alphabet `GOOG` (ISIN `US02079K1079`) only. The first parsers and the orchestrator (PRs #366–#369, July 2026) were checked against a real Google GSU export when they merged. No real export with a sale has been seen (#381). The repository keeps no real export, redacted or not. |
| Synthetic fixtures only | StockPlan workbook tests build synthetic workbooks with openpyxl (`tests/test_parser_stockplan_*.py`, `tests/test_pipeline_stockplan_*.py` and `tests/test_cli_stockplan_all.py`); orchestrator tests also use stubbed handlers. The hardening merged after #369 (#380, #383, #384, #600, #679, #680, #682) has no recorded run against a real export, so treat every rule it added as synthetic-tested only. |
| Structurally generic, issuer semantics unverified | The parsers read the symbol, ISIN and listing exchange from `[stockplan.security]`, so another issuer's export might parse. Its plan names, tax payment methods, dividend activities and share classes are unverified, so every StockPlan command refuses any symbol/ISIN pair other than `GOOG` / `US02079K1079` before it reads an export (#679). Microsoft RSUs belong to a later epic seeded by #382. |
| Blocked pending real sample data | Sales and Schedule CG (#381). Table A2 (#375). Issuers other than Alphabet `GOOG` (#382). Release rows with cash proceeds or another tax payment method, and dividend rows with populated share or price columns, stay refused until a real export shows their meaning. |

## Configuration

Every StockPlan command needs:

- `[account].status`, `BeneficialOwner` or `Joint`. StockPlan commands refuse
  without it (`ConfigError at field 'account.status'`), and loading the config
  refuses `SigningAuthority`. StockPlan does not use
  `[account].number` or `[account].opening_date` as its account identity or
  opening date, but shared config validation still checks them when supplied.
- `[stockplan.security]` with `symbol` and `isin`. The ISIN is in no StockPlan
  export, so its check digit is validated instead. The pair must be
  `GOOG` / `US02079K1079`.

`stockplan-fa` (and so `stockplan-all`) also needs `entity_name`,
`entity_address`, `listing_exchange`, `issuer_country_code` and
`nature_of_entity`. `entity_zip` is optional. A supplied
`issuer_country_code` is validated even when only `stockplan-income` runs.
It must be a 2-letter uppercase ISO 3166-1 alpha-2 code in the ITR country
list, and not `IN`.

`[stockplan].opening_date` is optional. `stockplan-fa` and `stockplan-all`
require the Capital Gains Report to cover from the later of this date and
the period start, or from the period start when unset. A date after the
period checked refuses. `stockplan-income` does not use this setting.

Settings shared with other sources also apply: `[account.foreign_tins]`,
`[form67]`, `[fx]`, `[schedule_fa]` (`peak_basis`, `peak_currency`) and
`[[prices.ticker_overrides]]`. `ibkr-fa init` writes an annotated template.

## Input discovery

Each command reads the top level of `--input` only. A file is found by
extension (`.xlsx`) and by the start of its name. Exactly one file must
match each prefix the command reads. None, or more than one, refuses.

| Report | File name starts with | Read by |
|---|---|---|
| Dividend Report | `IN - Dividend Report` | `stockplan-income`, `stockplan-fa`, `stockplan-all` |
| Capital Gain/Loss workbook | `IN - Capital Gain_Loss Report` | `stockplan-fa`, `stockplan-all` |
| Vested Share Holdings | `Vested Share Holdings` | `stockplan-fa`, `stockplan-all` |

Other files in `--input` are not read. The output guard still refuses a run
whose output target is the config or one of these exports.

## Input support matrix

### Rules for every workbook

- The file must open as an `.xlsx` workbook. Cells are read as literal stored
  values: a cell that stores a formula refuses, whether or not a
  value was stored with it, wherever it is stored (title, header, data or
  footer row, or past the sheet's declared size). A text cell that starts with
  `=` is text, not a formula. A cell that stores a spreadsheet error value,
  such as `#N/A` or `#VALUE!`, refuses the same way; a text cell that reads
  `#N/A` is text.
- The workbook's sheets must be exactly its required sheets, plus optionally
  `Quote History`. A missing sheet or an extra sheet refuses. The Vested Share
  Holdings workbook also requires `Quote History` and reads it for the
  Selected Date (below); in the other workbooks it is never read.
- Excel row 1 is a title row: one text cell in column A matching the report's
  title pattern below. The header row must match the listed columns exactly,
  with nothing populated after them.
- After the header, a row is blank (skipped), one of the report's allowed
  text rows (column A only, skipped), or a data row: column A holds a date,
  or any later column is populated. Data rows are checked column by column,
  and a populated cell beyond the header width refuses. Any other row refuses
  as `unknown non-data row; the StockPlan report schema may have changed`.
- Dates must be date cells. Numbers must be numeric cells, or text that
  Python's `Decimal` reads as a finite number, such as `1_000`, `1e3`, `+5`
  or a number padded with spaces. In fields converted as numbers, text that
  `Decimal` reads as `NaN`, `sNaN`, `Infinity` or `-Infinity` refuses as
  `non-finite <column>`, and a `TRUE` or `FALSE` cell refuses as
  `non-numeric <column>`.
- `DD-Mon-YYYY` dates in titles use English three-letter month names, and
  each must be a real date, or the report refuses. A report's dates are the
  ones in its title's closing `(report run on …)` clause; a date earlier in
  the title, or in any other cell, is never used.
- Refusals name the row as a zero-based index: `row 0` is Excel row 1.

### Dividend Report

File `IN - Dividend Report*.xlsx`, required sheet `IN - Dividend Report`.

| Row | Accepted content |
|---|---|
| Title (row 1) | `<any text> - IN - Dividend Report (report run on DD-Mon-YYYY for reporting period DD-Mon-YYYY to DD-Mon-YYYY)` |
| Header (row 2) | `Entry Date`, `Activity`, `Cash`, `Symbol`, `Number of Units or Shares`, `Unit or Share Price`, `Book Value` |
| Allowed text rows | `All currencies are in US Dollars unless specified.` (exactly once in the sheet); `No records found` (at most once); `Unable to get quote as at YYYY/MM/DD, funds = (…[.SYMBOL.<id>.]…),` and `Unable to retrieve market price for fund: [.SYMBOL.<id>.] on date: YYYY/MM/DD. Returned $<amount> USD instead.`, where `SYMBOL` is the configured symbol |

| Column | Rule | Use |
|---|---|---|
| `Entry Date` | Date within the title's reporting period, first and last day included | Payment date |
| `Activity` | `Dividend Paid` or `IRS Nonresident Alien Withholding` | Row kind |
| `Cash` | `Dividend Paid` > 0; `IRS Nonresident Alien Withholding` < 0 | Gross dividend (USD); US tax withheld (USD) |
| `Symbol` | Equals `[stockplan.security].symbol` | Identity check |
| `Number of Units or Shares` | Blank | Validated only |
| `Unit or Share Price` | Blank | Validated only |
| `Book Value` | Numeric zero | Validated only |

- At most one `Dividend Paid` row and one withholding row per `Entry Date`.
- A report with no dividend or withholding row must carry the `No records found`
  row, and a report with that row must have no dividend or withholding row.
- The title's run date must be after the reporting period's last day.
- Every dividend and withholding row's `Entry Date` must fall within the
  reporting period, first and last day included, or the report is refused.
- `stockplan-income` needs the period to span the financial year;
  `stockplan-fa` needs it to span the calendar year.
- A withholding row in the financial year needs a dividend with the same
  payment date. The shared income engine refuses one without
  (`Schedule FSI: foreign withholding tax was deducted on (ISIN, payment_date) event(s) …`).

### Capital Gain/Loss workbook: Releases Report

File `IN - Capital Gain_Loss Report*.xlsx`, required sheets `Releases Report`
and `Capital Gains Report`.

| Row | Accepted content |
|---|---|
| Title (row 1) | `<any text> - Releases Report (report run on DD-Mon-YYYY)` |
| Header (row 2) | `Vest Date`, `Order Number`, `Plan`, `Type`, `Status`, `Price`, `Quantity`, `Net Cash Proceeds`, `Net Share Proceeds`, `Tax Payment Method` |
| Allowed text rows | `All currencies are in US Dollars unless specified.` (exactly once in the sheet) |

| Column | Rule | Use |
|---|---|---|
| `Vest Date` | Date, on or before the title's run date | Lot acquisition date |
| `Order Number` | Nonblank, unique in the sheet | Lot identity; tie-break when vests share a date |
| `Plan` | Equals `[stockplan.security].symbol` | Identity check |
| `Type` | `Release` | Validated only |
| `Status` | `Complete` | Validated only |
| `Price` | > 0 | Fair market value per share at vest (USD) |
| `Quantity` | Number, at least `Net Share Proceeds` | Shares vested before sell-to-cover |
| `Net Cash Proceeds` | Zero | Validated only |
| `Net Share Proceeds` | > 0 and ≤ `Quantity` | Shares deposited: the lot's quantity |
| `Tax Payment Method` | `Fractional Shares` | Validated only |

The sheet has no reporting period. Its completeness is proved only by the
reconciliation with the Holdings Report (below). A sheet with no release row
parses, and `stockplan-fa` then refuses (`no vest on or before …`).

A completed vest dated after the title's run date refuses: the sheet can't
hold a vest that hasn't happened yet. A vest on the run date itself passes,
because the title gives no run time. This check comes after every other check
of the sheet, so it never hides one of their refusals. It doesn't make the
sheet fresh enough to file: the Holdings reconciliation (below) separately
needs the run date to be on or after the Holdings Report's Selected Date,
which must be after 31 December.

### Capital Gain/Loss workbook: Capital Gains Report

| Row | Accepted content |
|---|---|
| Title (row 1) | `<any text> - Capital Gains Report (report run on DD-Mon-YYYY for reporting period DD-Mon-YYYY to DD-Mon-YYYY)` |
| Section row (row 2) | `Sale Details` in column A, `Acquisition Details` in column F, nothing else |
| Header (row 3) | `Sale Date`, `Sale Price Per Share`, `Number of Shares Sold`, `Share Class`, `Total Gross Proceeds`, `Acquisition Date` |
| Allowed text rows | `No records found` (at most once); `All currencies are in US Dollars unless specified.` (exactly once in the sheet) |

| Column | Rule | Use |
|---|---|---|
| `Sale Date` | Date, on or before the title's run date | Sale count only |
| `Sale Price Per Share` | ≥ 0 | Validated only |
| `Number of Shares Sold` | > 0 | Validated only |
| `Share Class` | Equals `[stockplan.security].symbol` | Identity check |
| `Total Gross Proceeds` | ≥ 0 | Validated only |
| `Acquisition Date` | Date, on or before the title's run date | Validated only |

- The sheet must hold either sale rows or the `No records found` row, never
  both and never neither.
- The reporting period must not end before it starts, and the run date must
  be after its last day.
- A sale row's `Sale Date` and `Acquisition Date` must each be on or before
  the title's run date; the run date itself passes, because the title gives
  no run time. This check comes after every other check of the sheet, period
  checks included, and a row's `Sale Date` is checked before its
  `Acquisition Date`.
- The period must cover the relevant window: from the later of
  `[stockplan].opening_date` and the period start (or the period start, when
  unset) to the period end. `stockplan-fa` checks
  the calendar year, and `stockplan-all`'s Schedule CG check checks the
  financial year.
- **Any sale row blocks every StockPlan schedule that reads this workbook.**
  `stockplan-fa` refuses whatever the sale's date, and `stockplan-all` reports
  Schedule CG as `ERROR`. Sale figures are parsed and sign-checked, but no
  output uses them (#381).

### Vested Share Holdings

File `Vested Share Holdings*.xlsx`, required sheets `Holdings Report` and
`Quote History`.

The report is a snapshot as of its **Selected Date**, the day picked when
exporting it, not as of its run date. Its `Market Value` is the market price
as at the Selected Date × the held shares, and the `Quote History` sheet gives
that price and its date.

#### `Holdings Report` sheet

| Row | Accepted content |
|---|---|
| Title (row 1) | `<any text> - Holdings Report (report run on DD-Mon-YYYY)` |
| Header (row 2) | `Employee Number`, `Savings Plan Name`, `Fund Name`, `Number of Shares`, `Acquisition Value`, `Market Value` |
| Allowed text rows | `Market Value Definition = Market Price as at Selected Date * Total Held SharesAcquisition Value Definition = Fair Market Value at Vest/Exercise * Total Held Shares`, optionally followed by `The numbers on this statement reflect post-split values from stock split on M/D/YYYY` (non-breaking spaces read as spaces) |

| Column | Rule | Use |
|---|---|---|
| `Employee Number` | Nonblank; the same on every row | Validated only |
| `Savings Plan Name` | Nonblank; the same on every row | Validated only |
| `Fund Name` | `Cash - USD` on exactly one row, and `<symbol> - <listing_exchange>` (for example `GOOG - NASDAQ`) on exactly one row. Any other fund refuses | Row kind |
| `Number of Shares` | Stock row > 0. Cash row ≥ 0 and equal to its other two values | Stock row: shares held at the snapshot |
| `Acquisition Value` | Stock row > 0. Cash row as above | Stock row: Σ (fair market value at vest × held shares), USD |
| `Market Value` | Stock row > 0 and equal to the stock's `Quote History` `Price` × `Number of Shares` exactly. Cash row as above | Ties the Selected Date to the snapshot |

The sheet has no currency statement. Its USD figures are confirmed by
reconciling them exactly with the USD-confirmed Releases Report. The
`Cash - USD` row is checked but isn't Table A2 evidence: the report is a
snapshot on its Selected Date, not on 31 December.

#### `Quote History` sheet

This sheet is read only after the `Holdings Report` sheet passes, so that
sheet's refusals come first.

| Row | Accepted content |
|---|---|
| Title (row 1) | `Quote History` |
| Header (row 2) | `Fund`, `Quote Date`, `Price` |
| Allowed text rows | None |

| Column | Rule | Use |
|---|---|---|
| `Fund` | `Cash - USD` on exactly one row, and `<symbol> - <listing_exchange>` on exactly one row. Any other fund refuses | Row kind |
| `Quote Date` | Date; the same on both rows, and on or before the Holdings Report's run date | The Selected Date |
| `Price` | `Cash - USD` row: 1. Stock row: see `Market Value` above | Stock row: the price as at the Selected Date (USD) |

### Cross-report checks (`stockplan-fa`)

1. The Capital Gains Report holds no sale and covers the calendar year, from
   `[stockplan].opening_date` when that is later than 1 January.
2. The Dividend Report covers the whole calendar year.
3. At least one vest is on or before 31 December.
4. **Holdings reconciliation**, against the Holdings Report's Selected Date,
   in this order:
   1. The Releases Report was run on or after the Selected Date, so it lists
      every vest the snapshot could hold.
   2. No vest is dated on the Selected Date: the snapshot may or may not
      include it.
   3. Every vest dated before the Selected Date, together, equals the
      Holdings stock row exactly: the sum of `Net Share Proceeds` equals
      `Number of Shares`, and the sum of `Price × Net Share Proceeds` equals
      `Acquisition Value`. A mismatch means a vest is missing or shares left
      the account. The products and sums are exact: no vest rounds away,
      however small. Shares that left and came back, or a transfer out offset by
      a transfer in of the same shares and value, can't be seen in these
      reports.
5. **Year-end freshness.** The Selected Date is strictly after 31 December, so
   every vest on or before 31 December is included in the reconciled aggregate.
   A snapshot on 31 December can miss a vest later that day. With
   check 4.1, this also means the Releases Report was run after 31 December.

Each vest on or before 31 December becomes one Table A3 row. Its initial
value is the exact `Price` × `Net Share Proceeds` converted at the vest date's
TTBR, rounded HALF_UP to whole rupees.

The closing value uses a positive, finite USD Yahoo close from a split-safe
price series, dated on the required session: the last regular US market
session on or before 31 December, at most three calendar days earlier
(2025-12-31 for 2025). A close from an earlier session, or a bar dated on a
non-session day, never stands in for it. The bundled US market calendar lists
every closure for 2025 only, so other calendar years refuse.
`--no-fetch-prices` reads only a complete, matching, integrity-checked cache;
without a usable year-end close, the run refuses.

### Exporting reports that pass

For FY `YYYY-YY` with `stockplan-all` (Schedule FA uses calendar year `YYYY`):

- Give the Dividend Report and the Capital Gain/Loss Report a reporting
  period that ends on or after 31 March `YYYY+1`, and run each after its
  period ends. The Dividend Report period must start on or before 1 January
  `YYYY`. The Capital Gain/Loss period must start on or before 1 January
  `YYYY`, or on or before `[stockplan].opening_date` when that is later.
- Export the Vested Share Holdings report on the same day as the Capital
  Gain/Loss Report, with that day as its Selected Date, so that the Releases
  Report lists every vest the snapshot could hold. The Selected Date may be
  earlier, but it must be after 31 December `YYYY` and on or before the day
  the Capital Gain/Loss Report is run. If a vest falls on the Selected Date,
  pick another date.

## Output support matrix

### Schedules

| Output | Status | Where |
|---|---|---|
| Schedule OS 1a(i) | ✅ Generated: the gross dividend at the Rule 115 rate | `stockplan-income`: `schedule-os.csv` |
| Schedule FSI | ✅ Generated: dividend income and US tax. Dividend only: no capital-gains head | `schedule-fsi.csv` |
| Schedule TR | ✅ Generated | `schedule-tr.csv` |
| Form 67 | ✅ `form-67.csv` (official upload CSV) when every row is ready. 🟡 When a row has no Indian tax payable and no credit, `form-67-debug.csv` instead, exit `3`. ⛔ Any other gap refuses before writing | `form-67.csv` / `form-67-debug.csv` |
| Schedule FA Table A3 | ✅ Generated as a review CSV (portal columns plus `_audit` columns), one row per held vest. 🟡 A peak that can't be established is left blank | `stockplan-fa`: `--output`; `stockplan-all`: `schedule-fa.csv` |
| Schedule FA Table A2 | 🟡 Not generated. Manual fill on every successful run: these exports don't prove the 31 December account cash or the daily account value (#375) | — |
| Schedule CG | Not generated. `stockplan-all` proves it from the Capital Gains Report, which must cover the financial year: no sale row means `not required (zero sales proven)`; any sale row is `ERROR`, exit `4` (#381); unproven evidence is `ERROR` | — |
| Schedule FA upload files: Table A3 `filing-csv`, CBDT JSON fragment, Quicko workbook | Not generated | — |
| Quicko TR form fields | Not generated | — |
| RSU vest perquisite | Out of scope: salary income, not a foreign-income schedule | — |

`--debug` on `stockplan-income` or `stockplan-all` also writes
`schedule-os-debug.csv`, `schedule-fsi-debug.csv`, `schedule-tr-debug.csv` and
`form-67-debug.csv`. Those are review files, not filing files.

`stockplan-income` on its own never reads the Capital Gain/Loss workbook, so
it can't show that Schedule FSI needs no capital-gains head. A standalone run
flags that gap: every successful run prints the zero-sales warning below and
exits `3`. Run `stockplan-all` to prove zero sales. Inside `stockplan-all`,
the income leg leaves the gap to the Schedule CG check, which proves zero
sales or fails the run.

### Commands, exit codes and cleanup

| Command | Exit `0` | Exit `3` | Exit `4` |
|---|---|---|---|
| `stockplan-income` | Never on its own: zero sales are unproven | Every successful run. A BLANK Form 67 row also writes `form-67-debug.csv` instead of `form-67.csv` | Any refusal below |
| `stockplan-fa` | Never: Table A2 is always manual fill | Every successful run | Any refusal below |
| `stockplan-all` | Never: Table A2 is always manual fill | Every leg succeeded and no sale was found | Any leg refused, any sale, a refusal before the legs ran, or a failed console write |

Exit `1` is an unexpected crash; exit `2` is a command-line usage error.

**Cleanup.** Outputs are cleared before computation, subject to input
collision checks and filesystem failures:

- `stockplan-income` deletes all eight income files from `--output-dir` (the
  four filing files and the four `-debug.csv` files) before reading anything.
  `--output-dir` must already exist.
- `stockplan-fa` deletes `--output` before reading anything.
- `stockplan-all` creates `--output-dir`, then, before any leg runs,
  attempts to delete the eight income files, `schedule-fa.csv`,
  `schedule-cg.csv` and `schedule-cg-ltcg.csv`. If one can't be deleted, it
  still attempts the rest, then refuses, naming each file it couldn't delete.
- Each refuses first, without deleting anything, when one of those targets is
  the config, the `--input` directory, or a StockPlan export in it.
- A run that fails after writing, including while printing its report,
  attempts to remove what it wrote. When any `stockplan-all` leg fails,
  cleanup attempts to remove every leg's output.
  When removal succeeds, the legs that had succeeded are marked
  `(output removed because the run failed)`.
- Preparation can refuse before cleanup, and a deletion failure can leave
  stale or partial output behind. While the error stream still works, the
  error names the paths it could not remove. After a failed `stockplan-all`
  cleanup, successful legs instead show
  `(the run failed: do NOT file this output)`. Do not file retained output.

**`stockplan-all` order.** It checks Form 67 readiness before any leg writes:
a Form 67 refusal, or any refusal while loading the config or reading the
Dividend Report, stops the run with no leg run. It then runs `stockplan-income`, `stockplan-fa` and the
Schedule CG check, all three even after one fails. A failed write to the
console, for example to a closed pipe, instead stops the run at once: no later
leg runs, it attempts to remove every output, and it exits `4`. It prints one status line
per leg, plus `manual-fill  Schedule FA Table A2 (complete daily cash/NAV
evidence unavailable)`. The status labels are `ok`, `manual-fill`, `ERROR`
and `not required (zero sales proven)`. The `stockplan-income` leg doesn't
print the zero-sales warning: it is `manual-fill` when a Form 67 row is BLANK,
`form-67.csv` files a fractional `Rate(%)` or a relief row's foreign Taxpayer
Identification Number is blank, and `ok` otherwise. `stockplan-all` exits with
the worst of `1` > `4` > `3` > `0`.

### Before you file

1. Fill Schedule FA Table A2 for the StockPlan account yourself, from account
   evidence these exports don't hold.
2. Fill any Table A3 peak the run left blank (the stdout report counts them).
3. If Form 67 is BLANK, complete it as the `[form67]` section of the README
   describes.
4. Act on every `warning:` line.
5. If `stockplan-all` found a sale, ibkr-fa can't produce your StockPlan
   Schedule CG or Schedule FA. Report them some other way.

## Warnings

Warnings identify review work on otherwise valid computed results. A later
`stockplan-all` leg can still fail and cause the output to be removed.
Warnings go to stderr as `warning: …` lines.

| Command | Warning | When |
|---|---|---|
| `stockplan-fa` | `Schedule FA Table A2 was not generated because these StockPlan exports do not prove the Dec-31 account cash balance or daily account NAV. Obtain complete calendar-year cash/NAV evidence and fill Table A2 manually (tracked by #375).` | Every successful run |
| `stockplan-income` | `Schedule FSI and TR leave the foreign Taxpayer Identification Number blank on a relief-claiming row; add it under [account.foreign_tins], or use your passport number if that country did not allot you a TIN` | A Schedule FSI row has foreign tax and `[account.foreign_tins]` has no entry for its country. The run exits `3`, and the `stockplan-all` income leg is `manual-fill` |
| `stockplan-income` | `the FX policy has not been verified for this assessment year; …` | AY 2027-28 or later, unless `--suppress-cross-ay-warning` |
| `stockplan-income` | `Form 67 CSV importer may blank fractional Rate(%) …` | A rendered Form 67 tax rate has a fractional part; enter and verify that rate manually after upload. The run exits `3`, and the `stockplan-all` income leg is `manual-fill` |
| `stockplan-income` | `INDIAN TAX CALCULATION REQUIRED: …` | A Form 67 entry has no Indian tax payable amount; calculate and enter it before filing |
| `stockplan-income` | `zero StockPlan sales are not proven: stockplan-income does not read the Capital Gain/Loss workbook, so a sale would be missing from Schedule CG and Schedule FSI. Run stockplan-all to prove zero sales, or check the Capital Gains Report for sales before filing.` | Every successful standalone run. Inside `stockplan-all`, the Schedule CG check covers this instead |

`stockplan-fa` also prints `Table A2: MANUAL-FILL` on stdout, and
`A3 Peak value(s): MANUAL-FILL` when a peak couldn't be established.

## Refusals

A refusal prints `error: …` to stderr and exits `4`. Each StockPlan command
then prints a pointer to this page. Parser refusals read
`ParseError in section '<sheet>' at row <n>, column '<column>': <message>`.
A parser, coverage or reconciliation refusal prints no cell, row, employee
number, workbook-provided sheet name, amount or date. Parser refusals name the
sheet and row, plus the column when applicable; coverage and reconciliation
refusals name the report and field.
The phrases below are stable. Text in `<>` varies.

### Configuration and discovery

| Refusal | Cause |
|---|---|
| `ConfigError at field 'stockplan.security': Morgan Stanley StockPlan exports are verified only for Alphabet GOOG (US02079K1079); …` | The issuer isn't `GOOG` / `US02079K1079` |
| `ConfigError at field 'account.status': is required for IBKR and StockPlan commands; …` | `[account].status` is missing |
| `ConfigError at field 'account.status': signing-authority accounts aren't supported; …` | `[account].status` is `SigningAuthority` |
| `ConfigError at field 'stockplan.security.issuer_country_code': …` | A supplied `issuer_country_code` isn't a 2-letter uppercase code in the ITR country list, or is `IN`. Every StockPlan command refuses, `stockplan-income` included |
| `ConfigError at field '<file>': must be UTF-8 text, but the byte at offset <n> (counting from 0) is not valid UTF-8` | `user.toml` was saved in another encoding, such as Windows-1252. Every StockPlan command refuses with exit `4`; `stockplan-all` refuses in its Form 67 readiness check, before any leg runs |
| `no [stockplan] block in <config>…` | No `[stockplan]` table |
| `incomplete [stockplan.security]: Schedule FA needs …` | A Schedule FA field is missing |
| `[stockplan].opening_date is after the period checked, …` | The opening date is after the period checked |
| `no '<prefix>' .xlsx found in <dir>` / `multiple '<prefix>' .xlsx in <dir> (ambiguous): …` | Discovery |
| `calendar year is out of range [1990, 2099]` | `--calendar-year` outside 1990–2099 |

Other `ConfigError` refusals come from loading `user.toml`, as for every
command.

### Workbook shape (every report)

| Refusal | Cause |
|---|---|
| `could not open <file> as an .xlsx workbook` / `could not read the '<sheet>' sheet's rows from <file>` | Not a readable `.xlsx` |
| `could not open <file> (<errno>)` / `could not read the '<sheet>' sheet's rows from <file> (<errno>)` | The operating system couldn't read the file; `<errno>` is a name such as `EACCES`, or `unknown error` |
| `<file> stores a formula in this cell; only stored values are supported. Re-export the report.` | A cell in the sheet stores a formula, with or without a stored value; the refusal names its row and its column as `#<n>` (1-based), never the formula or its value |
| `This cell stores a spreadsheet error value. Re-export the report.` | A cell in a sheet with no formula stores an error value, such as `#N/A` or `#VALUE!`; the refusal names the first such cell's row and its column as `#<n>` (1-based), never the file name or the error code |
| `<file> has no '<sheet>' sheet` / `<file> is missing required sheet(s): […]` / `<file> has <n> unexpected sheet(s); expected only […]` | The sheet set changed |
| `expected <description> in <file>` / `expected exact header row …` / `expected exact Sale Details / Acquisition Details section row …` | The title, section or header row changed |
| `unexpected populated column beyond the known <n>-column schema` | A new column is populated |
| `unknown non-data row; the StockPlan report schema may have changed` | A text row that isn't on the allowed list |
| `could not uniquely confirm <file> is denominated in US Dollars …` | The USD statement is missing or repeated |
| `non-numeric <column>` / `data row has a missing or non-date <column>` | A cell has the wrong type |
| `non-finite <column>` | A field converted as a number reads as `NaN`, `sNaN`, `Infinity` or `-Infinity` |
| `malformed … date in <file>` | A title date isn't a real date |
| `<file> was not run strictly after its reporting period ended, so it can miss later <dividends\|sales>; …` | The report was run too early |

### Dividend Report

| Refusal | Cause |
|---|---|
| `unrecognised dividend-report Activity; …` | Another `Activity` |
| `row Symbol does not match the configured [stockplan.security] symbol` | Another security |
| `'Dividend Paid' must be a positive gross amount` / `'IRS Nonresident Alien Withholding' must be a negative (withheld) amount` | Wrong sign |
| `duplicate '<activity>' for an Entry Date an earlier row already has` | Two rows of one kind on one date |
| `'<activity>' is dated outside the report's stated reporting period` | An `Entry Date` outside the title's period |
| `<column> is populated but its StockPlan semantics are unsupported` | `Number of Units or Shares` or `Unit or Share Price` is populated |
| `Book Value must be zero for the validated StockPlan dividend schema` | Nonzero `Book Value` |
| `'No records found' row also carries data` / `duplicate 'No records found' row` / `report has both 'No records found' and dividend/withholding rows` | A dirty or contradictory sentinel |
| `<file> has no dividend/withholding rows and no 'No records found' sentinel; cannot prove zero activity` | Zero activity isn't proven |
| `<file>'s reporting period does not span the financial year; …` / `… does not span the calendar year; …` | The period is too short |

### Releases Report

| Refusal | Cause |
|---|---|
| `Release row has a blank Order Number` / `duplicate vest Order Number` | Order number |
| `unexpected row Type; only 'Release' rows are supported` | Another `Type` |
| `vest Status is not 'Complete'` | Another `Status` |
| `row Plan does not match the configured [stockplan.security] symbol` | Another plan |
| `non-zero release cash is unsupported` | `Net Cash Proceeds` isn't zero |
| `unexpected Tax Payment Method; only 'Fractional Shares' is validated` | Another tax payment method |
| `Fair Market Value (Price) must be positive` / `Net Share Proceeds (shares deposited) must be positive` / `shares deposited must be > 0 and <= GSUs vested` | Impossible figures |
| `completed vest date must not be after this sheet's report run date` | A `Vest Date` after the title's run date; the run date itself passes |

### Capital Gains Report

| Refusal | Cause |
|---|---|
| `row Share Class does not match the configured [stockplan.security] symbol` | Another share class |
| `Number of Shares Sold must be positive` / `Sale Price Per Share must not be negative` / `Total Gross Proceeds must not be negative` | Impossible figures |
| `'No records found' row also carries sale data — ambiguous/corrupt` / `duplicate 'No records found' row` / `… has BOTH a 'No records found' sentinel and sale row(s) …` / `… has no sale rows AND no 'No records found' sentinel …` | Zero sales aren't proven |
| `Capital Gains reporting period ends before it starts` | Inverted period |
| `sale date must not be after this sheet's report run date` / `acquisition date must not be after this sheet's report run date` | A `Sale Date` or `Acquisition Date` after the title's run date; the run date itself passes |
| `<file>'s reporting period does not span the relevant account period; sales cannot be proven zero` | The period is too short |
| `<file> has <n> sale(s); stockplan-fa does not yet handle intra-year disposals …` | `stockplan-fa` found a sale |
| `Schedule CG was not generated: <file> contains <n> sale(s), but stockplan-cg is not implemented. …` | `stockplan-all` found a sale |
| `could not prove StockPlan Schedule CG status: …` | `stockplan-all`'s Schedule CG check refused; the rest of the line says why |

### Vested Share Holdings

| Refusal | Cause |
|---|---|
| `Holdings row must carry a nonblank employee and plan identity` / `Holdings rows have inconsistent employee/plan identities` | Identity |
| `unexpected holding; expected only 'Cash - USD' and the configured [stockplan.security] symbol and listing exchange` | Another fund or security |
| `expected exactly one 'Cash - USD' row, found <n>` / `expected exactly one holdings row for the configured stock, found <n>` | Missing or repeated rows |
| `Cash - USD values must not be negative` / `Cash - USD shares, acquisition value, and market value must match` / `the configured stock's holdings must have positive shares, acquisition value, and market value` | Impossible figures |
| `<file> has no 'Quote History' sheet` | The `Quote History` sheet is missing |
| `expected title row …` / `expected exact header row …` / `unknown non-data row; …` / `unexpected populated column …` in section `Quote History` | The `Quote History` sheet's shape changed |
| `unexpected quote; expected only 'Cash - USD' and the configured [stockplan.security] symbol and listing exchange` | Another fund or security in `Quote History` |
| `expected exactly one 'Cash - USD' quote and one quote for the configured stock` | Missing or repeated quotes |
| `the 'Cash - USD' quote must be 1` / `the 'Cash - USD' and stock quotes must share one Quote Date` / `the Quote Date must not be after the Holdings Report's run date` | The quotes don't give one Selected Date |
| `Market Value must equal the 'Quote History' sheet's Price x Number of Shares exactly` | The stock's `Market Value` isn't priced at the Selected Date |

### Reconciliation and valuation (`stockplan-fa`)

| Refusal | Cause |
|---|---|
| `the Releases Report has no Vest Date on or before Dec-31 of the calendar year; nothing to report` | No held vest |
| `the Releases Report was run before the Holdings Report's Selected Date, so a vest the Holdings Report counts can be missing from it; export the Releases Report on or after the Selected Date, for example both reports on the same day` | Check 4.1 |
| `a Releases Report vest is dated on the Holdings Report's Selected Date, so the Holdings Report may or may not include it; re-export the Vested Share Holdings report with a later Selected Date, and the Releases Report on or after that new Selected Date, for example both on the same day` | Check 4.2 |
| `cumulative vests do not reconcile to the Holdings Report's Number of Shares and Acquisition Value; a vest lot may be missing, or there are unaccounted sales/transfers — refusing to file an unverified Schedule FA holding` | Check 4.3: a vest is missing, or shares left the account before the Selected Date |
| `StockPlan year-end freshness: the Holdings Report's Selected Date (the 'Quote History' sheet's Quote Date) is not strictly after Dec-31 of the calendar year, so it can't show which vests were still held then; re-export the Vested Share Holdings report with a Selected Date after year-end, and the Releases Report on or after that Selected Date, for example both on the same day` | Check 5 |
| `InvariantError: StockPlan vest shares: sum must be exact within Decimal precision` / `InvariantError: StockPlan vest acquisition value: sum must be exact within Decimal precision` / `… product must be exact …` | A product or sum in check 4.3 is nonfinite, fails Decimal arithmetic, or needs more than 1,000 digits. Cells within the read limits can't trigger these refusals |
| `Dec-31 market close: the price series has no usable Close for the required US market session` | The price series has no positive, finite USD close dated on the required session above, or it has a bar dated after that session on or before 31 December |
| `Dec-31 per-share pricing, field calendar_year: calendar year <year> is unsupported` | The calendar year isn't 2025, the only year the bundled US market calendar lists every closure for |
| Another Dec-31 closing-price refusal | No usable year-end close under the USD and split-safety rules above, for example with `--no-fetch-prices` and no usable cached prices. StockPlan has no manual Dec-31 price setting, so the final no-source refusal asks for a Yahoo close, fetched or from a valid price cache, and points at `[[prices.ticker_overrides]]` in case the Yahoo symbol is wrong |

### Income and orchestration

| Refusal | Cause |
|---|---|
| `ConfigError at field 'form67': Form 67 cannot be produced: …` | A Form 67 row is REFUSED |
| `output path is not a directory: <dir>` | `stockplan-income`'s `--output-dir` doesn't exist |
| `could not create --output-dir …` / an output-collision refusal | `stockplan-all` couldn't prepare `--output-dir` |
| `could not remove the previous output …` / `the run failed, and its output may still be there: …` | Cleanup failed; the error names output that may remain. Do not file it |
| `<leg> exited <code> without its promised output: …` | A leg reported success without writing its files |
| `<command>: a value in the input is too large or too precise to compute (<exception>)` | Decimal arithmetic failed, including during report rendering |
| `[Errno 32] Broken pipe` or another console write error | `stockplan-all` couldn't write to stdout or stderr, for example after a pipe's reader exited. Printed only while stderr still works |

Filesystem, output-writing, FX, TTBR and price refusals are shared with every source.

## An export this page doesn't describe

When a refusal says the schema may have changed, reports an unexpected sheet,
column, value or row, or refuses your issuer:

1. Don't edit the export to make it pass. The checks exist because an unseen
   shape can carry a figure that belongs in your return.
2. Check that you exported the right report for the right period, as
   [Exporting reports that pass](#exporting-reports-that-pass) describes.
3. If it still refuses, open an issue at
   <https://github.com/jay-tau/ibkr-fa/issues> with the command, its exit code,
   and the `error:` line, with names, employee numbers and amounts replaced.
   Name the sheet, row and column it reports. Never attach the export.

Support for a new shape is added only after a redacted real export shows what
it means.

## Maintainer checklist

Update this page in the same PR whenever a StockPlan change accepts a new
sheet, row, column, value, issuer, report or schedule, or changes a refusal,
warning, output, exit code or cleanup rule. In that PR:

- [ ] Add the shape to the parser's explicit allow list: never a broad skip.
      Add a test that accepts it and one that refuses a near miss.
- [ ] Update the matching table on this page, and its provenance:
      real-export verified, or synthetic only.
- [ ] New issuer: add its symbol/ISIN pair to `require_verified_issuer` only
      after real-export tests pass (#382), and update the provenance table.
- [ ] New schedule or output: update the output matrix, the exit-code
      table, the cleanup targets and the `stockplan-all` status lines.
- [ ] New or changed refusal or warning: update the refusal or warning tables.
- [ ] Keep the StockPlan rows of the README subcommand table, the CLI help of
      `stockplan-income`, `stockplan-fa` and `stockplan-all`,
      [`docs/architecture.md`](architecture.md) and
      [`docs/portal/schedule-fa-manual-fill.md`](portal/schedule-fa-manual-fill.md)
      consistent with this page.

`tests/test_docs_stockplan_contract.py` checks that the listed sheet, header,
value and export-prefix constants appear on this page and that the A2 and
zero-sales warnings are quoted verbatim. Title and allowed-text regex shapes
are reviewed by hand.
