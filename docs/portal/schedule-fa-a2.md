# Schedule FA — Table A2 (Foreign Custodial Accounts)

Table A2 of ITR-2/ITR-3 Schedule FA discloses the foreign **custodial account itself**
— the Interactive Brokers account — as opposed to Table A3, which discloses each
foreign equity holding *inside* that account. An IBKR-only filer reports **both**:
the account in A2 and every holding in A3 (issue #18).

`ibkr-fa` emits the A2 row only when you opt in by supplying `[account.custodian]`
in `user.toml` (the institution's identity is not in the statement). The row is
written to a **separate sibling file** next to the A3 output. NetBenefits writes
`schedule-fa-a2.csv` in the portal's accepted 12-column bulk-upload shape, and
so do `robinhood-fa --format filing-csv` (`schedule-fa-a2.csv` in its output
directory) and `fidelity-cma-fa --format filing-csv` (the `-a2` sibling of its
`--output`).
The downloaded template contains a stray trailing comma (a false 13th field);
the live uploader skips every row unless both header and data contain exactly
12 fields / 11 commas. Audit columns remain available through the explicit
`review-csv` writer; they are not mixed into the filing file.

### Bulk-upload tokens differ from the visible form

The CSV uploader does not accept the display text used by manual entry:

* `Country/Region name`: readable uppercase name, e.g.
  `UNITED STATES OF AMERICA`.
* `Country Name and Code`: internal numeric code only, e.g. `2`—not the
  visible `2-UNITED STATES OF AMERICA`.
* `Status`: internal token such as `BENEFICIAL_OWNER`.
* `Nature of Amount`: `I`, `D`, `S`, `O`, or `N` (no amount).
* Dates: strict ISO `YYYY-MM-DD`, not the manual form's `DD/MM/YYYY`.

The portal also splits data rows on raw commas without honouring CSV quoting.
The upload rows therefore replace commas in text fields (notably addresses)
with spaces; otherwise a standards-compliant quoted address silently shifts the
column count and produces `common.errors.csv_row_skip`. A value made only of
commas and spaces would then leave its cell blank, so the row refuses it (see
[Text length limits](#text-length-limits)).

### Upload rows and file dialect

Two parts make the upload file, and neither uses the manual-entry labels in
`src/ibkr_fa/output/portal_labels/`:

* **Schedule rows** (`src/ibkr_fa/output/schedules/fa.py`): the 12-column
  header and one row per account, built only when the writer reads it. For
  each row the country is checked against the selected form's dropdown first.
  The amounts are read from the row's filed whole-rupee values next. Then the
  text is cleaned and the cells are rendered in column order.
* **Upload dialect** (`FA_UPLOAD` in `src/ibkr_fa/output/filing_csv.py`): UTF-8
  without a BOM and CRLF line endings. Every cell is checked for a NUL byte, and
  then the first six cells, the text ones, are checked for a formula trigger.
  The signed amounts after them stay plain numbers, such as a margin closing
  balance of `-6`. The shared portal CSV renderer stages the file and publishes
  it only after the last row.

### Text length limits

Each Table A2 row refuses, when it is built, text longer than both vendored
AY 2026-27 CBDT schemas allow: country name 55, institution name 125, address
200, ZIP 8 and account number 34 characters. The limits count the raw text,
before the upload rows replace commas with spaces and collapse whitespace
runs. A blank ZIP is still allowed: the run names the ZIP Code among the
cells to fill by hand and exits 3 (#1358). A value over its limit exits 4
with a fixed message that never shows the value, such as
`error: InvariantError: FaA2Row.institution_name must be at most 125 characters`.

The row also refuses a country name, institution name, address, ZIP or account
number made only of commas and spaces, because the upload rows would write it
as an empty cell. The message names only the field, such as
`error: InvariantError: FaA2Row.institution_zip must contain a character other than a comma or a space`.
Such a value over its limit gets the length refusal first. An empty or
whitespace-only value is handled as before.

## Provenance

Columns, field tokens, and validations were verified in both official
AY 2026-27 V1.2 workbooks at `TR_FA!D35:O45`. They are identical across
ITR-2 and ITR-3 except for the [country dropdown](./schedule-fa-a3.md#country-dropdown):
ITR-3 omits Yemen and Zimbabwe. Unlike A3, Table A2 has **no opening/initial-value column**
and **no account-closure-date column** — only Peak and Closing balance.

The evidence artifacts and hashes are recorded in
[`schedule-fa-a3.md`](./schedule-fa-a3.md#provenance).

## Portal column layout

| # | Column | Source |
|---|--------|--------|
| 1 | Country Name and Code | IBKR: `[account.custodian].country_iso2`; CMA: verified NFS country |
| 2 | Name of financial institution in which the account is held | IBKR: `[account.custodian].institution_name`; CMA: National Financial Services LLC; NetBenefits: `[netbenefits.account].institution_name`, with a warning when it is Fidelity Stock Plan Services ([ADR 0021](../decisions/0021-netbenefits-a2-institution.md)) |
| 3 | Address of financial institution | IBKR: `[account.custodian].address`; CMA: verified NFS address; NetBenefits: `[netbenefits.account].address` |
| 4 | ZIP Code | IBKR: `[account.custodian].zip`; CMA: verified NFS ZIP; NetBenefits: `[netbenefits.account].zip`; Robinhood: `[[robinhood.accounts]] zip` |
| 5 | Account Number | IBKR: real `[account].number`; CMA: `[fidelity_cma].account_id` |
| 6 | Status | IBKR: `[account].status`; CMA: `[fidelity_cma].status` (observed layout supports BeneficialOwner only) |
| 7 | Account opening date | IBKR: `[account].opening_date`; CMA: first no-carried-balance statement date under the warned attestation contract |
| 8 | Peak Balance During the Period | manual-fill unless a source-specific audited value/bound is explicitly selected |
| 9 | Closing balance | broker Dec-31 NAV × SBI TTBR, or manual-fill |
| 10 | Gross amount paid/credited — Nature of amount | dropdown token, or manual-fill |
| 11 | Gross amount paid/credited — Amount | INR whole rupees, or manual-fill |

### Status dropdown tokens (emitted verbatim)

`OWNER`, `BENEFICIAL_OWNER`, `BENIFICIARY` (the official utility's spelling,
typo included). The config `status` is translated: `BeneficialOwner` →
`BENEFICIAL_OWNER`, `Joint` → `OWNER`. Loading `user.toml` refuses
`SigningAuthority`: ibkr-fa doesn't support signing-authority accounts.

### Nature-of-amount dropdown tokens (emitted verbatim)

`Interest`, `Dividend`, `Proceeds from sale or redemption of financial assets`,
`No Amount paid/credited`, `Other income`.

## Balances and FX

* **Closing balance** = the broker's stated Dec-31 Net Asset Value (the
  `Net Asset Value` section's `Total` row `Current Total`), cross-checked
  against the `Change in NAV` `Ending Value`, converted to INR at the SBI TTBR
  on Dec-31. Without a `Total` row, the closing balance is the printed
  `Ending Value`, used only when every asset-class row has a `Current Total`
  and their sum matches it within the 0.01 tolerance; the sum itself is never
  filed. It is left **manual-fill** when the NAV could not be determined with
  certainty or the base currency is not USD (USD-only auto-fill in v1). Each
  blank closing balance carries exactly one reason, and the `schedule-fa` and
  `run-all` run report and the CBDT JSON gap name it with its remedy (#1013):
  * the `Net Asset Value` section is absent, or has no asset-class block
    with a `Current Total` column;
  * it has more than one such block;
  * the block has more than one `Total` row;
  * the `Total` row's `Current Total` is blank;
  * the block has no `Total` row and no asset-class rows;
  * it has no `Total` row, and an asset-class row's `Current Total` is blank;
  * `Change in NAV` has more than one `Ending Value`;
  * the block has no `Total` row and there is no `Ending Value` to check the
    asset-class sum against;
  * the `Ending Value` is blank;
  * the input is a Flex Query, whose NAV isn't parsed;
  * the NAV parsed, but the base currency is not USD.

  A statement that contradicts itself stops the run instead (exit 4,
  `ParseError`, #1309): a `Total` row its asset-class rows don't add up to
  within 0.01 (or a nonzero `Total` with no asset-class rows), or a NAV
  figure more than 0.01 from the `Ending Value`. Re-download the Activity
  Statement from IBKR or fix the file, then re-run.

  When an A2 conversion needs a Dec-31 TTBR that the FX policy can't
  resolve, every source stops with `MissingFxRateError` (exit 4).
  Proven gross sale proceeds of 0 need that rate too.
  A negative (margined) NAV is preserved, not clamped.
* **Peak balance** is manual-fill for the generic IBKR path because its
  statement carries no daily account NAV. A source-specific pipeline may
  populate it only with audited daily evidence. Computed paths select the
  highest native-currency balance first and then apply that date's SBI TTBR.
  `schedule_fa.peak_currency = "inr"` instead maximizes daily INR values and
  emits a warning. Each TTBR conversion is the exact product of the daily
  balance and the rate (#1260), so INR ties and the filed rupee follow the
  exact amounts; one that can't be computed exactly refuses the run (exit 4)
  with `InvariantError: Schedule FA peak, INR: product must be exact within
  Decimal precision`. The Robinhood and Fidelity CMA daily account values
  behind these peaks are exact too (#1267): each day's cash and quantity
  ledgers, position values and statement checks keep every digit, so the only
  rounding left is the filed whole-rupee rounding. A value that needs more
  digits than the widened precision (the greater of the active precision
  and 1,000 digits) refuses the run (exit 4) with a fixed
  `InvariantError` naming the A2 value it was computing. Fidelity CMA
  computes and preserves explicit lower/upper posting-order bounds; it files
  neither by default and requires an attested choice. Each bound is
  converted at its own date's TTBR, so the rupee order can differ from the
  native order. An attested bound is refused (exit 4) when the lower bound
  converts to more rupees than the upper. In the `filing-csv` upload, which
  has no audit columns, a blank CMA peak is named as a manual-fill cell and
  the command exits 3; the bounds stay in `review-csv` and `xlsx` output.

## Gross amount paid/credited

The total credited to the account during the CY — dividends + interest + sale /
redemption proceeds. Every nature uses the December 31 SBI TTBR. This is one
uniform Schedule FA disclosure policy; taxable-income schedules continue to
apply their separate Rule 115 conversion dates.

When an A2 conversion needs a December 31 TTBR that the FX policy can't
resolve, every source stops with `MissingFxRateError` (exit 4). Proven
gross sale proceeds of 0 need that rate too.

Generic IBKR, Fidelity CMA, and Robinhood emit one complete account row per
non-zero nature, with
the account identity, peak, and closing fields repeated and only that nature's
amount in columns 10–11. It never collapses multiple natures into `Other
income`. This is the official utility's native shape: A2 is an expandable row
table, JSON import adds rows beyond the six initially visible rows, and
validation checks each row independently without requiring unique account
numbers. Unresolved nature totals add a blank placeholder row, remain manual,
and suppress filing JSON. A Robinhood nature stays unresolved only with a
Schedule OS blocker in its source income, which refuses the Schedule FA leg
instead (exit 4, no A2 or A3 file). Robinhood's sale-proceeds nature (`S`) is the sum
of each in-CY equity sale's activity `Amount` and each option sell-to-close's
cash at the December 31 TTBR (#1383). Any option blocker refuses the Schedule
FA leg instead (exit 4, no A2 or A3 file). NetBenefits currently proves no
account credits and therefore emits one `No Amount paid/credited` row.

An IBKR Activity Statement whose `Change in NAV` contradicts its `Dividends`,
`Withholding Tax` or `Interest` section (for example, it shows interest but
the statement has no `Interest` section) stops the run instead (exit 4,
`ChangeInNavIncomeError`, no A2 or A3 file; #1324), as in the income commands
(#1279), rather than file `No Amount paid/credited` or a total missing that
income.

The dividend, interest and sale-proceeds totals are exact sums (#1168, under
the owner's 2026-11-10 decision on #1166), as are the NetBenefits securities
total behind its closing balance and each symbol's dividend total. Adding in
Python's default 28-digit decimal context could drop a digit and change a
filed rupee: six interest credits totalling exactly $1.50 at ₹1/$ filed ₹1
instead of ₹2. These additions widen the precision to at least 1,000 digits
instead, preserving a higher active precision, so they never round. A total
that can't be added exactly (an amount that isn't finite, an overflow, or a
sum still rounding at that precision) refuses the run (exit 4). The refusal
names the total, the 1-based position of the failing input and its field:
`input row <n>` for an interest credit or a dividend payment (plus its source
row when the parser recorded one), `input lot <n>` for a closed lot or a
NetBenefits lot, and `contribution <n>` for a symbol's dividend total; for
example `InvariantError: Schedule FA A2 gross proceeds, input lot 3, field
gross_proceeds_inr: sum must be exact within Decimal precision`. Positions
count every input before the calendar-year filter. The December 31
conversions of the dividend and interest totals are exact products too
(#1312): `Schedule FA per-symbol dividends, symbol total <n>, INR` and
`Schedule FA A2 gross interest, INR` refuse as the closing balance below does.

The closing balance (the December 31 NAV × TTBR) and each closed lot's gross
proceeds conversion are exact products (#1250), and NetBenefits' check that its
Σ A3 closing equals the securities total × TTBR compares exact values. A
product that can't be computed exactly refuses the run (exit 4): for example
`InvariantError: Schedule FA A2 closing balance, INR: product must be exact
within Decimal precision`.

IBKR interest counts only the Interest section's credits: positive rows whose
description reads exactly `<CCY> Credit Interest for <Mon>-<YYYY>`, such as
`USD Credit Interest for Apr-2025`. Debit interest (a negative `Debit
Interest` row) is margin interest paid, not a credit, and is left out. Any
other Interest row (an accrual-labelled row, a positive row in other wording,
or a negative or zero row that isn't debit interest) is unproven, so the
Interest nature is unresolved: `schedule-fa` names the row and adds the blank
placeholder row (#905, #1280).

## ⚠️ Do not double-count A2 + A3 for the BMA threshold

Table A2 (the account) and Table A3 (the holdings inside it) deliberately
overlap. Do **not** add the A2 closing balance to the A3 holding values when
testing the Black Money Act ₹20-lakh aggregate threshold — that double-counts
the same assets. Threshold evaluation is fact-specific; consult a CA.

See the [Schedule FA automation and manual-fill matrix](./schedule-fa-manual-fill.md)
for source-specific gaps and exit behavior.

## Re-verification checklist before filing

1. Confirm the custodian identity (cols 1–4) matches the source account's legal
   entity (IBKR entity or CMA's NFS).
2. Confirm the account number (col 5) belongs to the same source: IBKR uses
   `[account].number`; CMA uses `[fidelity_cma].account_id`.
3. For CMA, verify the first no-carried-balance statement date is the legal
   account opening date; `[account].opening_date` belongs to the separate IBKR
   account.
4. **Fill or verify the Peak Balance (col 8)** — select the highest
   native-currency account value during the CY, then apply that date's SBI TTBR.
5. Verify the Closing balance (col 9) against your December statement.
6. Verify every gross-credit nature has one row and repeated account balances
   have not been added together. If a blank placeholder row exists, resolve
   its unknown nature and amount before filing.
