# ITR-2 / ITR-3 Schedule FSI portal labels — evidence trail

This file pins the exact column headers the `schedule-fsi` review CSV /
XLSX writers emit for Schedule FSI ("Details of Income from outside
India and tax relief"), so the user can paste each per-(country, head)
row into the matching utility cells.

## Current column headers

Schedule FSI accepts ONE row per `(country_code, head_of_income)` (the
portal computes totals server-side — there is no portal-TOTAL cell).
Each command's file covers only the income that command read. When commands
that read different files each have a row for one country and head, enter one
row: add their (b) and (c), and enter the Indian tax on the combined income as
(d) (see the README's `[form67]` section).
The 8 portal columns the writers emit, in order, are pinned in
`src/ibkr_fa/output/portal_labels/_ay_2026_27.py::PORTAL_COLUMN_HEADERS_FSI`:

| Col | Form position | Label string | v1 fill | Verified against |
|-----|-------------|--------------|---------|------------------|
| 0 | outer | `Country Code` | filing worksheet: combined portal dropdown value, e.g. `2-UNITED STATES OF AMERICA`; debug output: CBDT numeric | Live portal + ITR-2/ITR-3 V1.2 |
| 1 | outer | `Taxpayer Identification number` | user-configured TIN; otherwise EMPTY | ITR-2/ITR-3 V1.2 |
| 2 | (a) | `Head of Income` | filled (StrEnum value) | ITR-2/ITR-3 V1.2 |
| 3 | (b) | `Income from outside India (included in PART B-TI)` | filled: Other Sources from Schedule OS's whole rupees (below), other heads INR HALF_UP | ITR-2/ITR-3 V1.2 |
| 4 | (c) | `Tax paid outside India` | filled (INR HALF_UP; PR #211) | ITR-2/ITR-3 V1.2 |
| 5 | (d) | `Tax payable on such income under normal provisions in India` | filled for reconciled Other Sources with a matching computed Form 67 credit and all needed exact source overrides, as the Indian tax on this command's income only; otherwise EMPTY | ITR-2/ITR-3 V1.2 |
| 6 | (e) | `Tax relief available in India (e)= (c) or (d) whichever is lower` | computed internally for TR/Form 67 reconciliation, but omitted from the filing worksheet because the live portal auto-populates this read-only field | Live portal + ITR-2/ITR-3 V1.2 |
| 7 | (f) | `Relevant article of DTAA if relief claimed u/s 90 or 90A` | relief article for computed §90/§90A credit; otherwise EMPTY | ITR-2/ITR-3 V1.2 |

The writers append 5 audit columns AFTER these 8 portal columns
(`_audit: ISO alpha-2`, `_audit: Country name (portal)`,
`_audit: Income INR (full precision)`, `_audit: Tax INR (full
precision)`, `_audit: Event count`). The audit columns are NOT
copied into the portal — they're for spreadsheet reconciliation
against the upstream Schedule OS / CG / interest / WHT aggregator
outputs.

Notable design notes:

* **Other Sources income is Schedule OS's rupees (#1319).** Each country's
  Other Sources income is its share of the Schedule OS fields it comes from,
  not the HALF_UP of its own exact total, so the FSI rows add up to those
  fields. Each filed field's whole rupees are split across its countries by
  the section 10 period rule (`split_whole_rupees` in
  `src/ibkr_fa/compute/schedule_os_periods.py`): each country's exact amount
  is floored, and the rupees still needed go one each to the largest
  remainders, the earlier country first on a tie. 1a(i) splits across its
  issuer countries in ISO order and then India, whose share stays out of FSI;
  1b(ix) is wholly its payer country's; each 1e nature splits across its
  source countries in ISO order. A country's income is the sum of its shares,
  so it differs from the HALF_UP of its exact income by at most one rupee per
  field, and it can be ₹0. The review CSV's full-precision audit income stays
  the exact total, which the relief reconciliation reads. Capital gains and
  business income come from no Schedule OS field and keep their own HALF_UP.
  Rounding each row on its own let FSI's Other Sources income exceed Schedule
  OS's by a rupee, which the ITR-3 portal refuses when relief is claimed
  against Other Sources. ITR-3's
  [validation rule 894](https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-06/CBDT_e-filing_ITR-3_Validation%20Rules_V1.0_AY%2026-27.pdf),
  Category A ("Return will not be allowed to be uploaded"), reads: "In
  Schedule FSI If tax relief is claimed against other sources then amount of
  Income shown in other sources should not be less than the amount of income
  shown under the head other sources". ITR-2's rules have no Other Sources
  counterpart.

* **Capital-gains income is exact.** Each country's STCG and LTCG nets, and
  the section 70 combination of the two, are exact sums of the Schedule CG
  lines' gains: like the Schedule CG totals, they widen to at least 1,000
  digits, preserving a higher active precision, and never round (owner decision
  on #1166, 2026-11-10). The review CSV's full-precision audit income can then
  show more than 28 digits. A net that can't be added exactly
  refuses (exit 4) as `Schedule FSI capital gains STCG net, input row <n>,
  column realized_gain_inr: sum must be exact within Decimal precision`
  (`LTCG net` likewise), where `<n>` counts all of that schedule's lines,
  every country's; a combination refuses as `Schedule FSI capital gains,
  fields stcg_net and ltcg_net: …`. Combinations are added before the
  net-loss filter, so a country whose row would be omitted refuses too.

* **Foreign tax and relief sums are exact.** The withholding nets shared by
  Schedule FSI, Form 67 and the orphan-withholding check (each country's
  signed net and each payment's paid tax before refunds in USD and INR,
  and each ISIN's signed INR net), Form 67's per-country dividend income, per-payment
  gross dividend and treaty-eligible tax, the merge of withholding onto the
  FSI rows, and the relief reconciliation's income and tax-payable sums all
  add the same way: at least 1,000 digits, never rounding (the owner's
  decision on #1166, applied by #1167). The nets stay signed, so refunds and
  reversals still net against tax paid, and a positive INR net still drops
  the country. Paid tax and each country's tax are negated without rounding.
  A sum that can't be added exactly refuses (exit 4), naming the 1-based
  position and the field, never a value, for example `Foreign tax country
  net, included row 3, column amount_inr: sum must be exact within Decimal
  precision`. The position counts the in-year withholding rows the nets
  read, the dividend lines Form 67 reads, the payments the treaty-rate test
  reads (all countries'), or the withholding contributions merged onto FSI;
  the relief sums name their fields instead. Each withholding row's INR
  conversion and the treaty-rate test's and cap's products are exact too
  (#1312), and reversals pair only with an original equal to every digit. An
  exact sum can't restore digits lost later: Schedule TR's per-country totals
  still run at the precision of their own context, adding bounded whole-rupee
  cells. The section 91 credit is computed exactly and proven to file as its
  exact whole rupee (#1177; see the README's `[form67]` section).

* **Country dropdown display.** The underlying Schedule FSI schema field is
  named `Country Code`, but the live portal dropdown displays the combined
  `"<CBDT code>-<portal country name>"` token. The filing worksheet mirrors
  that value (for example, `2-UNITED STATES OF AMERICA`); debug/review output
  preserves the raw numeric code and country name separately for auditing.

* **Relief field is read-only.** The portal computes (e) as the lower of (c)
  and (d). The compute model still carries the value because Schedule TR must
  reconcile to it, but the filing worksheet omits it: there is nothing for the
  user to enter. When the computed relief differs from the lower of the
  whole-rupee (c) and (d), as a treaty-capped credit below the tax paid does,
  the portal would overwrite it. Every Schedule FSI and Schedule TR output
  then refuses, in every format (`filing-csv`, `review-csv` and `xlsx`), and
  so does every income or all command that writes them: exit 4, nothing
  written, and earlier outputs removed. Rows whose relief or (d) is empty
  are not compared. A section 91 relief is checked with the former formula's
  credit first and then the exact one, each with its own fixed message
  (#1177). Form 67 keeps the treaty-capped credit, and the
  standalone `form-67` command still writes it.

* **Indian tax is never inferred from foreign withholding.** Column (d) stays
  empty unless an exact source-level amount is configured. Rule 128(5)(i)
  limits FTC to the lower of Indian tax payable on that income and eligible
  foreign tax, but does not prescribe an average- or marginal-rate formula.
  For ordinary income,
  [*Promac Engineering Industries Ltd v ACIT*](https://indiankanoon.org/doc/4726975/),
  ITA 501/Bang/2018, paras 12-13, applied foreign income multiplied by the
  average Indian tax rate (`Indian income-tax on total income / total income`).
  The case concerned AY 2014-15, before Rule 128 applied. The Karnataka High
  Court later [disposed of the Revenue's appeal on monetary-limit grounds while
  keeping the question of law open](https://indiankanoon.org/doc/75630410/).
  No reported ruling was found settling average versus marginal rate under
  Rule 128(5)(i); confirm the relevant DTAA/current-law method and any
  special-rate treatment. When column (d) is unavailable and unrounded foreign
  tax is positive (even if it displays as `0`), the filing worksheet leaves the
  numeric cell empty and repeats the full stderr warning on the next row,
  labelled `Manual fill guidance`. That row is explanatory text, not a portal
  field; do not copy it into the portal. Such a row also makes `schedule-fsi`
  exit `3` in every format, with the file still written (#1328). A row
  without foreign tax, such as a capital gain, keeps (d) empty with no
  guidance row and doesn't change the exit code. A refused Form 67 row
  instead stops `schedule-fsi` in every format with exit `4` before anything
  is written, as in `form-67` and `run-all` (#1480).

* **Treaty rate is tested before FX conversion, per payment.** Dividend income
  and foreign tax use different statutory FX dates under Rule 115 and Rule 128.
  Whether withholding exceeds a DTAA rate is therefore decided from
  source-currency income/tax, for each dividend payment: the dividend lines and
  paid withholding sharing source account, ISIN and payment date, before
  refunds, with half a cent of rounding tolerance per paid withholding row.
  Rule 128(5)(i)'s proviso ignores tax above the treaty rate but doesn't say
  whether that excess is measured per payment or on the year's total for a
  source, and no ruling settling it was found. Until it is settled, the
  owner's interim, conservative policy caps each payment on its own, so a
  payment withheld above the rate is never averaged with the country's other
  payments (#890). Tax of a payment within the treaty rate remains actual
  Rule-128-converted tax in FSI/Form 67/TR; an INR ratio produced from unlike
  FX dates never creates a treaty excess. A payment above the rate retains the
  conservative legacy cap, the treaty rate times its Rule-115 income, pending
  per-event eligible-tax conversion. The credit is left blank, and refused when
  Form 67 column (7) is set, when no source-currency rate is available, when a
  refund or reversal in the country can't be attributed to an above-rate
  payment, or when withholding can't be matched to a single payment. Several
  dividend lines on one payment's source account, ISIN and payment date can't
  be matched to a single payment for any broker, so withholding on them leaves
  the credit blank too until the payments are split by hand.

* **DTAA article entry is numeric.** This field asks for the article under which
  relief is claimed, so the filing field receives only the main relief-article
  number (for example, `25` for India-US Article 25(2)(a)). Debug/review output
  retains the precise citation. Form 67 separately uses Article 10(2)(b) beside
  its treaty-rate field.

* **EMPTY vs zero contract.** Optional cells are EMPTY (NOT `"0"`) when
  unavailable, explicitly signalling "not computed; user must fill" rather
  than "computed and equals zero". A foreign-tax cell of literal `"0"` is
  emitted when the compute layer returns `Decimal("0")` (for example, a CG-only
  bucket with no foreign withholding) — that's a computed-zero outcome.

* **No portal-TOTAL cell.** Schedule FSI's per-(country, head) rows
  are summed server-side. The writers therefore emit ONE row per
  `ScheduleFsiRow` and stop — no audit-TOTAL or portal-TOTAL row is
  appended.

* **Reviewed residual income country.** Generic Schedule OS 1e receipts enter
  FSI only when their source country is explicitly evidenced. Robinhood loan
  fees use the configured account/FY review and aggregate under `Other
  Sources`; issuer/ISIN country is not reused. The FSI dispatcher consumes the
  fee-only compute result rather than Schedule OS's combined 1e presentation
  lines, preventing substitute payments from being counted twice.

## Filing worksheet rows and file dialect

Two parts make the `filing-csv` worksheet, whichever command writes it:

* **Schedule rows** (`fsi_filing_rows` in `src/ibkr_fa/output/schedules/fsi.py`):
  a plain row function. Building the writer resolves the AY's labels. Writing
  first checks every row's relief, so a relief the portal would overwrite
  refuses before any file is staged. When the first row is read, the rows are
  grouped the way the portal form stacks them: the combined country dropdown
  value and the TIN once per country and TIN, then, for each head of income,
  a blank row, the head, income, tax paid, tax payable, the `Manual fill
  guidance` row when (d) is empty and foreign tax is positive, and the bare
  DTAA article number. A blank row separates countries. The read-only relief
  (e) is never written. The cells come from `fsi_portal_cells`, the view the
  review CSV shares.

* **File dialect** (`FILING_WORKSHEET` in `src/ibkr_fa/output/filing_csv.py`):
  UTF-8 with a BOM and LF line endings. Every cell is checked for a NUL byte
  and then for a formula trigger. The shared portal CSV renderer stages the
  file and publishes it only after the last row.

## Verification evidence

The strings and field positions were read from `FSI!D3:M7` in both
official AY 2026-27 V1.2 workbooks. The visible headers are identical.
Their V1.1 schemas share the same outer Schedule FSI wrapper; ITR-3's
detail type additionally contains `IncFromBusiness`. ibkr-fa fills it only
for Robinhood options under a reviewed business treatment:
`robinhood-income` and `robinhood-all` file complete, positive net business
income from those options as one US `Business or Profession` row (#549).
That row is already in the Schedule FSI files, so don't enter it again.
When `robinhood-options.csv` says "ITR-3 P&L/BP/CFL/Schedule FSI
(manual)", no row is filed and you enter it yourself. Complete any other
foreign business income from your business records. No other source
produces business income, and ibkr-fa never infers blank or zero from
brokerage data.

See [`schedule-fa-a3.md`](./schedule-fa-a3.md#provenance) for utility
links and workbook hashes. For a future AY, repeat this check against
both forms rather than assuming ITR-2 and ITR-3 remain aligned.
