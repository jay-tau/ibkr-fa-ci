# ITR-2 / ITR-3 Schedule TR portal labels — evidence trail

This file pins the exact column headers the `schedule-tr` review CSV /
XLSX writers emit for Schedule TR ("Summary of Tax Relief Claimed for
Taxes Paid Outside India"), so the user can paste each per-country
row into the matching utility cells.

## Current column headers

Schedule TR accepts ONE row per source country (Schedule FSI's per-
(country, head) rows aggregated by summing `foreign_tax_inr` across
heads). Each command's file covers only the income that command read. When
commands that read different files each have a row for one country, enter one
row whose (c) and (d) total (c) and (e) of that country's combined Schedule FSI
rows, as the labels below say. The 5 portal columns the writers emit, in order,
are pinned in `src/ibkr_fa/output/portal_labels/_ay_2026_27.py::PORTAL_COLUMN_HEADERS_TR`:

| Col | Form letter | Label string | Fill | Verified against |
|-----|-------------|--------------|------------------|------------------|
| 0 | (a) | `Country Code` | filing worksheet: combined portal dropdown value, e.g. `2-UNITED STATES OF AMERICA`; debug output: CBDT numeric | Live portal + ITR-2/ITR-3 V1.2 |
| 1 | (b) | `Taxpayer Identification Number` | user-configured TIN; otherwise EMPTY | ITR-2/ITR-3 V1.2 |
| 2 | (c) | `Total taxes paid outside India (total of (c) of Schedule FSI in respect of each country)` | filled (sum of FSI's HALF_UP-rounded `foreign_tax_inr` per row across heads; T3 r1 P2 #1 fix) | ITR-2/ITR-3 V1.2 |
| 3 | (d) | `Total tax relief available (total of (e) of Schedule FSI in respect of each country)` | sum of each FSI row's relief rounded to whole rupees, when every contributing row has computed relief and the positive-relief rows resolve to one section; otherwise EMPTY | ITR-2/ITR-3 V1.2 |
| 4 | (e) | `Tax Relief Claimed under section (specify 90, 90A or 91)` | `90`, `90A`, or `91`, filled together with (d); otherwise EMPTY | ITR-2 V1.2 |

ITR-3 uses `Section under which relief claimed (specify 90, 90A or 91)`
for column (e). `--itr-form` selects the matching header; the row model
and calculations remain shared.

The writers append 4 audit columns AFTER these 5 portal columns
(`_audit: ISO alpha-2`, `_audit: Country name (portal)`,
`_audit: Total tax paid INR (full precision)`, `_audit: FSI rows
aggregated count`). The audit columns are NOT copied into the
portal — they're for spreadsheet reconciliation against the upstream
Schedule FSI rows.

The `_audit: Total tax paid INR (full precision)` column reads
`ScheduleTrRow.foreign_tax_inr_total_full_precision` when populated
(compute-driven path: full-precision sum of FSI's `foreign_tax_inr`
across heads, preserved for FSI⇄TR reconciliation distinct from the
per-row-rounded portal cell at column 2). It falls back to
`foreign_tax_inr_total` when the full-precision field is `None`
(legacy direct-constructor path). This dual-population pattern was
introduced by PR #216 T2/T3 r2 follow-up after the per-row HALF_UP
rounding fix (T3 r1 P2 #1) made the portal cell mathematically
distinct from the full-precision audit value.

Notable design notes:

* **Country dropdown display.** The schema column remains `Country Code`,
  but the live portal dropdown displays the combined
  `"<CBDT code>-<portal country name>"` token. The filing worksheet mirrors
  that value; debug/review output keeps the raw numeric code and country name
  separately.

* **Quicko block.** NetBenefits appends `Quicko` and then the navigation path
  `Tax Credits -> Add Manually -> Taxes Withheld Outside India`
  on separate rows below the horizontal TR table.
  It mirrors the live form: Country/Nature, Credit/Tax, and Section/Article are
  paired side by side with headings above their values and one blank row after
  each pair; single fields (deductor and TIN) use compact one-row label/value
  entries. Two blank rows separate ITD from Quicko; one separates the Quicko
  heading/path from its fields. Redundant helper prose is omitted.
  When `[account.foreign_tins]` has no entry for the form's country, the TIN
  field stays blank, the run exits 3, and the CLI directs the filer to add the
  foreign TIN, or their passport number if that country did not allot them one.
  Its values come from the same Form 67 / FSI reconciliation. Quicko and Form
  67 receive the main withholding-article number only (`10`), while Schedule
  FSI deliberately receives the relief article (`25` for India-US). Debug/audit
  data retains the precise citations.
  `src/ibkr_fa/output/quicko_foreign_tax.py` builds the block from the Form 67
  result: one form per Form 67 entry (country and source of income) that has
  an exact Indian tax and a §90 or §91 credit. "Name of the deductor" is the
  person that withheld the tax (ADR-0019): for NetBenefits,
  `[netbenefits.account].withholding_agent_name`, which the filer copies from
  their Form 1042-S (box 12d for tax in box 7a; the agent box 14a names for
  tax in box 8). The tool reads no NetBenefits 1042-S, so it can't check the
  name. `[netbenefits.account].institution_name` is the Schedule FA Table A2
  institution and never fills this field. In an otherwise successful run
  that writes a Quicko form without the setting, the value stays blank, as
  a missing TIN does, and every file is still written:
  `netbenefits-income` prints one notice naming the setting and exits `3`,
  and `netbenefits-all` shows its income leg as `manual-fill`. Other refusals
  still apply. A run that writes no Quicko form doesn't need it. Only the
  NetBenefits run passes these inputs to the TR filing rows; IBKR,
  StockPlan, Fidelity CMA and Robinhood write the TR rows only.

* **When relief is filled.** A country gets a TR row only when its foreign
  tax, summed from each FSI row's tax rounded to whole rupees, is positive.
  For such a country, relief (d) and section (e) are filled only when every
  contributing FSI row has computed relief and the positive-relief rows
  resolve to one section. Both are EMPTY when some row has no computed relief
  (incomplete evidence), and when the country's relief is all zero before
  rounding (for example, Indian tax on the foreign income is ₹0).
  Positive-relief rows on both §90 and §91 refuse.
  `src/ibkr_fa/compute/schedule_tr.py` owns these rules. No command writes a
  country whose relief is zero or rounds to zero, though: that relief is a
  Form 67 credit that files as ₹0, a refused row (#601). When a country TR
  writes has an FSI row with foreign tax and a blank Indian tax payable,
  `schedule-tr` prints the "INDIAN TAX CALCULATION REQUIRED" warning and exits
  `3` in every format, with the file still written (#1328). A row without
  foreign tax, such as a capital gain, does neither. A refused Form 67 row
  instead stops `schedule-tr` in every format with exit `4` before anything
  is written, as in `form-67` and `run-all` (#1480).

* **EMPTY vs "computed zero" contract.** Optional cells are EMPTY when not
  computed. When a country's positive relief rounds to zero, the engine
  computes relief `0` with its section, which no command files (#601); the
  writers render any supplied zero relief as **literal `"0"`** (CSV) /
  **numeric `0`** (XLSX) to distinguish "computed = ₹0" from "not computed".
  See PR #215 T3 r1 P2 #1 for the model-layer rationale.

* **No portal-TOTAL cell.** Schedule TR's per-country rows are
  aggregated server-side into DTAA-vs-non-DTAA sub-totals (which
  are themselves separate cells on the portal form, not part of
  the per-country row table). The writers emit ONE row per
  `ScheduleTrRow` and stop.

* **Refund-disclosure sub-table not modelled.** The portal also
  requires "If any foreign tax (on which relief was allowed in
  India) is REFUNDED by the foreign tax authority during the year
  ..." — refund amount + AY in which relief was originally allowed.
  This is a per-event sub-table on the portal form; the TR row model
  does NOT model it, so any refund is entered on the portal by hand.
  See `src/ibkr_fa/models/schedule_tr_row.py` module docstring for
  the rationale.

## Filing worksheet rows and file dialect

Two parts make the `filing-csv` worksheet, whichever command writes it:

* **Schedule rows** (`tr_filing_rows` in `src/ibkr_fa/output/schedules/tr.py`):
  a plain row function. Building the writer resolves the AY's labels and the
  selected form's five headers (ITR-2's when no form is given). Writing first
  checks the relief of the FSI rows the TR result carries, so a relief the
  portal would overwrite refuses before any file is staged. The header and
  one row per country, with the combined country dropdown value, are built as
  they are read. The Quicko block stays in this file: it follows only when the
  run supplies Quicko's inputs, and its forms are built from the Form 67
  result only after the last TR row has been read. The cells come from
  `tr_portal_cells`, the view the review CSV shares.

* **File dialect** (`FILING_WORKSHEET` in `src/ibkr_fa/output/filing_csv.py`):
  UTF-8 with a BOM and LF line endings. Every cell is checked for a NUL byte
  and then for a formula trigger. The shared portal CSV renderer stages the
  file and publishes it only after the last row.

## Verification evidence

The strings were read from `TR_FA!D4:I10` in both official AY 2026-27
V1.2 workbooks. See
[`schedule-fa-a3.md`](./schedule-fa-a3.md#provenance) for utility links
and workbook hashes. The V1.1 `ScheduleTR` row schema is identical
between forms; the visible column-(e) wording differs as noted above.
