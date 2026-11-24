# ITR-2 / ITR-3 Schedule CG STCG portal labels — evidence trail

The `schedule-cg` review output reports resident foreign-security STCG
in the residual, slab-rate Schedule CG bucket. The bucket number differs
between ITR-2 and ITR-3.

## AY 2026-27 destinations

| Form | Destination | Review label |
|---|---|---|
| ITR-2 | A5e | `Schedule CG / A / 5(e) STCG on assets other than at A1 or A2 or A3 or A4 (A5e)` |
| ITR-3 | A6g | `Schedule CG / A / 6(g) STCG on assets other than at A1 or A2 or A3 or A4 or A5 (A6g)` |

`--itr-form` selects the matching label; the calculation is shared.

## Primary evidence

* ITR-2 V1.2 workbook: `CG!E74:R94`, terminal code `A5e`.
* ITR-3 V1.2 workbook: `CG!E76:R96`, terminal code `A6g`.
* The official V1.1 schemas enumerate the same destinations and
  descriptions.
* Utility links and workbook hashes:
  [`schedule-fa-a3.md`](./schedule-fa-a3.md#provenance).

## Why the residual bucket applies

Foreign equity held by a resident Indian assessee:

* uses the 24-calendar-month short-/long-term threshold implemented by
  `STCG_HOLDING_THRESHOLD_MONTHS = 24`;
* is not eligible for section 111A's STT-paid Indian-security rate;
* therefore enters the form's residual STCG bucket.

The ITR-3 form inserts a slump-sale/business-related block before the
residual row, which shifts A5e to A6g without changing the underlying
foreign-security computation.

## Whole-rupee filed total

Every CG output files the same whole-rupee total:
`round_half_up(Σ proceeds_inr) − round_half_up(Σ cost_basis_inr)`, the
form's Balance. The audit TOTAL keeps the full-precision gain. Owner
decision, #615 (2026-11-01).

* The notified form computes the gain from its components. Form ITR-2
  (G.S.R. 227(E), 30-03-2026), Schedule CG item A5, gazette p.43:
  "c Balance (5aiii – biv)" and "e STCG on assets other than at A1 or A2
  or A3 or A4 above (5c + 5d)".
* Consideration and cost are whole rupees. In the ITR-2 and ITR-3 V1.2
  utilities (V1.4 has the same formulas and validations on these cells),
  consideration (A5a(ii), ITR-3 A6a(ii)) accepts "Amount entered should
  be Numeric, Non negative, No decimal." and cost of acquisition "Non
  negative, no decimal, upto 99,999,999,999,999". Balance is a formula:
  ITR-2 `CG!Q87` is `=(STCG.A5a_FullConsideration-STCG.A5biv_TotalDedn)`.
  The ITR-2 V1.2 and ITR-3 V1.1 schemas type `FullValueConsdOthUnqshr`,
  `AquisitCost`, `BalanceCG` and `CapgainonAssets` as `"integer"`.
* CBDT's AY 2026-27 validation rules enforce that arithmetic. Category A
  means "Return will not be allowed to be uploaded. Error message will be
  displayed." ITR-2 r.123: "In schedule CG, Sl. No. A5c Balance should be
  equal to A5(aiii-biv)"; r.124: "In Schedule CG Sl.no. A5e of STCG should
  be equal to the sum of A(5c+5d)". ITR-3 r.381: "In schedule CG, Sl.No.
  A6c Balance should be equal to A6(aiii-biv)"; r.382: "In Schedule CG
  Sl.No. A6g should be equal to the sum of A(6c+6d+6e -6f)".
* So with this tool's zero adjustments, separately rounded consideration
  and cost give this total. Rounding the summed gain instead can differ
  by ₹1 and could not be entered without misstating a component.
* Sections 288A and 288B round "total income" and "Any amount payable,
  and the amount of refund due" to ₹10 after ignoring paise. Neither
  says how consideration or cost is rounded, and no reviewed primary
  source prescribes a tie-break for those fields; HALF_UP is a
  convention.

The schedule is refused (exit 4) when its rounded consideration is
negative, or is zero while its rounded cost is ₹1 or more. The schema
and utility refuse negative consideration, and ITR-2 r.104 refuses
deductions against zero consideration: "In Schedule CG, Full Value of
Consideration (Sl. No. A(5)(aiii)) is zero then expenses (Sl. No.
A(5)(biv)) should not be claimed" (ITR-3 r.361 for A6). This can refuse
one source's figures that another source's consideration would make
valid in the same row (owner decision 2, #615).

Each source also refuses the schedule (exit 4), before it writes any
output, when the rounded consideration, cost or Balance falls outside
±99,999,999,999,999. The utility caps cost at that value, and the owner
chose one symmetric 14-digit bound for every filed amount (#788, #830).
A line's own proceeds, cost and gain are review detail, so they aren't
bounded.

Each consideration, cost and audit-gain total is the exact sum of its lines
(owner decision on #1166, 2026-11-10). A sale split across lots by quantity
can give per-lot amounts of 28 to 60 significant digits (IBKR lot
matching, Robinhood's allocation, Fidelity CMA), and adding them can need a
digit more than Python's default 28-digit decimal context holds. These
additions widen the precision to at least 1,000 digits instead, preserving
a higher active precision, so they never round. Consideration and cost are
rounded HALF_UP separately; their difference is the filed gain. A total that
can't be added exactly (an amount that isn't finite, an overflow, or a sum
still rounding at that precision) refuses the schedule (exit 4), naming the
schedule, the 1-based input row and the column, for example
`InvariantError: Schedule CG STCG, input row 3, column proceeds_inr: sum must
be exact within Decimal precision`.

Each line's INR amounts are exact in the same way (#1184): the cost
(unless a statutory INR cost replaces it) and the proceeds are the USD
amounts times the rate, and the gain is proceeds minus cost, each at the
same widened precision. A conversion or gain that can't be computed exactly
refuses the schedule (exit 4), naming the lot's 1-based position among all
the closed lots given to the schedule, for example
`InvariantError: Schedule CG LTCG, input lot 2, column cost_basis_inr:
product must be exact within Decimal precision` (`proceeds_inr` likewise),
or `InvariantError: Schedule CG STCG, input lot 1, column realized_gain_inr:
sum must be exact within Decimal precision`.

A Robinhood lot's share of a cost or a sale's cash split by quantity is an
exact rational (#1241), such as a third, that no Decimal can hold. Such a
line also carries its exact amounts: each total adds the rationals, and the
filed consideration and cost are their exact HALF_UP. The line's INR
amounts and the audit-gain total show one division of the rational.
Fidelity CMA's (#1244) and IBKR's (#1273) FIFO lots split their cost and
proceeds the same way, and their FIFO quantities are subtracted exactly, so
every lot's shares add back to the trade they came from.

Sources: [Form ITR-2](https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-04/Notification%20No.46_2026.pdf),
the [ITR-2](https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-07/ITR2_AY_26-27_V1.2.zip)
and [ITR-3](https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-07/ITR3_AY_26-27_V1.2.zip)
utilities, the [ITR-2](https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-08/ITR-2_2026_Main_V1.2.json)
and [ITR-3](https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-07/ITR-3_2026_Main_V1.1.json)
schemas, the [ITR-2](https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-05/CBDT__e-Filing_ITR%202_Validation%20Rules_AY%202026-27_V1.0.pdf)
and [ITR-3](https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-06/CBDT_e-filing_ITR-3_Validation%20Rules_V1.0_AY%2026-27.pdf)
validation rules, and sections [288A](https://www.incometaxindia.gov.in/w/section-288a-58)
and [288B](https://www.incometaxindia.gov.in/w/section-288b-58) of the
Income-tax Act, 1961.

## Robinhood filing workbook

Robinhood's non-debug output is `schedule-cg.xlsx`, a manual-entry
workbook with the applicable residual STCG fields. Full lot and FX evidence
is emitted only with `--debug`.

The workbook covers Robinhood equity disposals only. ADR 0006 sends a
reviewed capital treatment of options to the same fields by hand, so the
workbook never says a section holding an option capital gain needs no
entry. Each such section's note says instead: "Manual entry in A5:
Robinhood option capital gains are not in this workbook; enter them by
hand from robinhood-options.csv, which robinhood-options and robinhood-all
write" (A5/B8 for ITR-2, A6/B9 for ITR-3), and
`robinhood-cg` reports `manual` (exit 3), and `robinhood-all`'s equity-cg
leg also reports `manual`, unless an existing refusal applies (#1322).
`robinhood-cg` doesn't write `robinhood-options.csv`, so its reason also
says to run `robinhood-options` with the same inputs and output folder
(#1470).
`RobinhoodCapitalGainsResult.filing` maps the option lines' term buckets to
the per-section flags passed to `build_schedule_cg_filing_result`; no
option amount is computed or written in the workbook.

`robinhood-options.csv` gives the option gains' Table F date bands instead
(#1362). After its `capital-gain` rows, each bucket with option capital
gains gets five `table-f` rows: row 3 for `stcg-other-assets`, row 5 for
`ltcg-other-assets`, one per band heading below, with the band's whole
rupees in `amount_inr`. `RobinhoodOptionsResult.table_f` builds each row
with the shared Table F builder below, from each line's close date and
exact gain (the slice's exact net P&L at its rate, #1245), to R, the HALF_UP
whole rupees of the bucket's exact total gain, which is what its
`capital-gain` rows add up to. Rules (b) to (e) apply as they do for the
workbook. The bands are the option gains alone, before any set-off: each
row's `manual_target` names its Table F row and the Schedule BFLA item it
must add up to (ITR-2 3v/3vii, ITR-3 5viii/5x), says to add the workbook's
equity bands and every other source's, and says that if the A5/B8 (A6/B9)
figures entered for these options round to a different total, the row must
still total Schedule BFLA. A loss across bands leaves the bucket's bands
blank with the workbook's text for that case. A bucket whose total rounds
past the CBDT 14-digit bound, which no return can file, is blank too,
saying so, rather than refusing a run that writes the workpaper today.
Either way the options leg, already `manual` (exit 3) whenever an option
capital gain exists, adds "Schedule CG Table F row 3 needs its option date
bands entered by hand; robinhood-options.csv leaves them blank" (row 5 for
the LTCG bucket). Option fees are unchanged. ADR 0006 doesn't prove an
option's Amount is pre-charge consideration, but by the owner's decision of
2026-11-13 the bands are filled from the net gain the CSV already lists, as
equity sales now file their recorded Amount (below).

For exchange-listed securities proven by the Robinhood identity layer to
trade on ARCA, NASDAQ, or NYSE, the workbook adopts the reviewed filing
position that the full value of consideration belongs in ITR-2 A5(a)(ii) or
ITR-3 A6(a)(ii), "assets other than unquoted shares." The three
unquoted-share fields are zero; market price is never represented as
prescribed Rule 11UA FMV.

Robinhood passes FINRA TAF and SEC fees on sales to customers but does not
say whether a sale's activity `Amount` is before or after them. By the
owner's decision of 2026-11-14 (#1383; ADR 0006 section 7 gives the reasons),
each sale's `Amount` is its full value of consideration, with 0 transfer
expenditure. A5(a)(ii)/A6(a)(ii) and (a)(iii) are the HALF_UP of the
section's exact `Amount`s at their transfer-month TTBRs, (b)(iii) is 0, and
the (b)(iv) total, the (c) balance, the A5(e)/A6(g) result and its Table F
bands follow as for any other source. Each section sheet's tax position note
ends with "Robinhood's recorded sale Amount is filed as each sale's full value
of consideration, with 0 transfer expenditure (owner decision, 2026-11-14).
Robinhood passes FINRA TAF and SEC fees on sales but doesn't say whether
Amount is before or after them. If a fee is inside Amount, consideration is
lower by that fee and the gain is still right. If a fee was collected outside
Amount, the gain is higher by that fee: slightly more tax, never less." The
(a)(ii) and (b)(iii) Basis cells point to that note.

The visible sheet omits fields whose Robinhood contribution is forced to
zero by the adopted filing position or supported asset scope: the three
unquoted-share fields, cost of improvement, Schedule DCG, and sections
54G/54GA. One note names every omitted field and instructs the user to
preserve values from other sources. A section's zero transfer
expenditure and the section 94(7)/(8) disallowance remain documented in the
debug view because their values reflect source/evidence limits rather than
an impossible legal outcome. The engine supplies both zeros:
`build_schedule_cg_filing_result` (`compute/schedule_cg_filing.py`) puts
every amount a section prints into a `ScheduleCgFilingSection`, and the
writer renders those amounts without computing or defaulting any (#1078).
Zero is the only value any source supports today; a non-zero transfer
expenditure or disallowance needs its own evidence and review.

Non-debug output contains only nonzero fields with editable portal
controls, plus Table F bands left blank. Zero editable fields and a
section's portal-calculated totals, balance, and final gain/loss are
shown in the workbook only with `--debug`; the debug
CSVs continue to carry the full lot and FX audit. If disposals exist but
every editable amount rounds to zero, the filing sheet says explicitly
that no numeric Robinhood entry is required, or, when its section also
holds an option capital gain, that the sheet needs no numeric entry.

When no LTCG disposal exists, the workbook omits the empty B8/B9 sheet and
records that negative assurance on the STCG sheet, unless the section holds
an option capital gain. A run with no equity CG disposals emits one
explanatory summary sheet rather than a blank workbook; it names any
section holding an option capital gain for manual entry.

The workbook and CLI retain a warning that no CBDT guidance or binding
ruling directly resolves whether a foreign exchange satisfies section
50CA's quoted-share definition. So each source's reviewed listing venues
and security classes are data in `compliance/schedule_cg_filing_position.py`:
Robinhood's are ARCA, NASDAQ and NYSE, and COMMON and ETF, and no other
source is opted in. `build_schedule_cg_filing_result`
(`compute/schedule_cg_filing.py`) records a blocker for any other venue or
class, and the shared `filing-xlsx` writer refuses a blocked result without
writing the workbook (exit 4). `robinhood-cg`, `robinhood-income` and
`robinhood-all` refuse a sale whose proven gross proceeds differ from its
recorded `Amount`.

### Table F: capital gains by date band (#1360)

Schedule CG part F, "Information about accrual/receipt of capital gain",
asks for each kind of capital gain in five date bands: (i) "Upto 15/6",
(ii) "16/6 to 15/9", (iii) "16/9 to 15/12", (iv) "16/12 to 15/3" and (v)
"16/3 to 31/3". These are Schedule OS section 10's bands, and both tables
use the same band function (`accrual_period_index` in
`compute/schedule_os_periods.py`).

- **Sources.** The notified ITR-2 for AY 2026-27 (Notification 46/2026,
  G.S.R. 227(E)) prints row 3 as "Short-term capital gains taxable at
  applicable rates. Enter value from item 3v of schedule BFLA, if any." and
  row 5 as "Long- term capital gains taxable at the rate of 12.5%. Enter
  value from item 3vii of schedule BFLA, if any." The ITR-3 (G.S.R. 228(E))
  has the same rows, from BFLA items 5viii and 5x.
- **The portal refuses a mismatch.** The AY 2026-27 validation rules check
  each row's total, all in Category A: ITR-2 rule 170 (row 3 against BFLA
  3(v)) and rule 578 (row 5 against 3(vii)), both "(Not applicable for
  Resident Senior Citizens)", and ITR-3 rules 426 (5viii) and 452 (5x). The
  vendored CBDT schema holds the rows as `AccruOrRecOfCG`'s
  `ShortTermUnderAppRate` and `LongTermUnder12_5Per`, five integers each, none
  below 0.
- **Which figure.** The newest official ITR-2 instructions found (AY
  2023-24, Part F) take the amounts "as computed in column 3 of Schedule
  BFLA", that is, after set-off of current-year and brought-forward losses.
  No source says which band a set-off loss lowers, and the same
  instructions use the bands to compute section 234C interest.
- **Rows.** The tool's foreign STCG (A5/A6) is taxed at applicable rates, so
  it goes in row 3; its section 112 LTCG at 12.5% (B8/B9) goes in row 5. The
  other rows (section 111A, an FII's A4e, the non-resident DTAA rows and
  virtual digital assets) never hold this tool's gains.

`compute/schedule_cg_table_f.py` builds one section's row once, and the
STCG and LTCG engine results expose it as `table_f`. A gain's band is the
band of its transfer date, the date that also sets its FY and Rule 115
rate. A band's net is the exact sum of its gains, each line's exact
rational where it has one (#1241), with nothing rounded before the split
(#1166). R is the section's filed result, the balance that A5(e), A6(g),
B8(e) or B9(e) files. The first rule that applies decides the row:

- **(a) Manual section:** no bands, because the filer types the result.
  Since #1383 only a `robinhood-options.csv` bucket past the CBDT bound
  (above) builds this case; the workbook and review-file writers refuse it
  with a fixed `InvariantError`.
- **(b) R is 0 or less:** five zeros. The section adds nothing to Table F.
- **(c) No band nets a loss:** the nets split to R. Each band is its net's
  floor or one rupee more, and the leftover rupees go to the largest exact
  remainders, the earlier band first on a tie (`split_whole_rupees`, the
  Schedule OS rule). R is the rounded consideration less the rounded cost,
  within one rupee of the exact total, so it always fits.
- **(d) Exactly one band nets a gain:** that band gets R and the others 0.
- **(e) Otherwise,** a band nets a loss and two or more bands net gains: no
  bands. Which gain band the loss lowers changes section 234C interest, and
  no source decides it, so the filer chooses.

The row records which case it is (computed, loss across bands, or, for an
option bucket only, manual section), so the workbook and the run report read
it rather than work it out.

**In the workbook.** The STCG sheet ends with five Table F row 3 rows,
`F3(i)` to `F3(v)`, and the LTCG sheet with five row 5 rows, `F5(i)` to
`F5(v)`. Each row's description is its band heading and the row's text for
the run's ITR form, and its action is "Enter/add". The default and
`--debug` filters apply as for any other field: a zero band is left out and
named after the section's own omitted fields, and a blank band shows. A
computed band's Basis says it is this source's sales dated in the band,
net, before any set-off, and that the five add up to the section's result
or are 0 when it isn't positive. It then says to add the same band's
amounts from every other capital-gains file (other brokers,
`robinhood-options.csv`'s option gains, and Indian sales), and to lower the
bands for any capital loss set off against them, so the row totals its
Schedule BFLA item; the portal checks only the total. The tool never sees
that set-off: Indian gains and losses, other brokers' gains and losses,
brought-forward losses, or one source's net short-term loss set against its
own long-term gain. So the note is guidance only and doesn't make the run
manual, as with Schedule OS section 10's section 57(1) note. A loss across
bands leaves them blank with "Blank: a date band nets a loss and two or more
bands net gains. Choose how the loss lowers them: no band below 0, and the
five add up to this section's result." That reason is followed by the same
guidance a computed band's Basis gives, from "Add the same band's amounts"
on (#1379). The A5(e)/A6(g) Basis says a non-positive result contributes
zero to Table F.

**In the run report.** A loss across bands adds "Schedule CG Table F row 3
needs its date bands entered by hand: a band nets a loss and two or more
bands net gains; schedule-cg.xlsx leaves them blank" (row 5 for the LTCG
section). It keeps the leg manual (exit 3) and prints no figure, date or
symbol. No Table F gap refuses.

**In the review files (#1361).** Every STCG and LTCG review CSV and XLSX
(`schedule-cg`, `schedule-cg-ltcg`, `run-all`, `fidelity-cma-cg`,
`netbenefits-cg`, `netbenefits-all`, and Robinhood's `--debug` copies)
ends with a Table F block just before its audit TOTAL row, so the portal
TOTAL row stays last: a blank row, a `_table_f:` marker row, five band
rows for row 3 (STCG) or row 5 (LTCG), then one fixed note row. Each band
row's first cell joins `Schedule CG / F / 3(i)` (or `5(i)` and so on) with
the band heading and the row's text for the run's ITR form, as the
workbook does, and its last cell, under the TOTAL column, holds the band's
whole rupees from the section's `table_f` row (a number in the XLSX). A
blank band leaves that cell empty, and its second cell holds the
workbook's blank Basis for a loss across bands (`table_f_blank_basis` in
`output/schedules/cg_filing.py`, one shared function). The block is always
written; a section with no in-FY sale or a net loss shows five zeros. The
note row says the bands are this file's sales alone, net by sale date,
before any set-off; to add the same row's band amounts from every other
capital-gains file, including Robinhood's and any Indian ones; and then to
lower the bands for any capital loss set off against them, so the row
totals Schedule BFLA item 3v or 3vii (ITR-3: 5viii or 5x). Every source
behind these files bounds its filed totals to the CBDT 14 digits before
any writer runs (#830), so no band can pass that bound.

When a review file's bands are blank for a loss across bands, the command
prints "⚠️  MANUAL-FILL: Schedule CG Table F row 3 needs its date bands
entered by hand: a band nets a loss and two or more bands net gains;
schedule-cg.csv leaves them blank." (row 5 and the LTCG file's name for
that section) to stderr, also under `--quiet`, and exits 3: `schedule-cg`,
`schedule-cg-ltcg`, `fidelity-cma-cg` and `netbenefits-cg`, and `run-all`
and `netbenefits-all` through them. The line names the row and the file,
never a figure, date or symbol. NetBenefits files have no line until an
in-FY sale can pass (#1150), so they can't reach it yet. Robinhood's
debug copies print nothing new: the run report already names the blank row
(exit 3). An ordinary run's stdout and stderr are unchanged.

Positive STCG no longer blocks the workbook (#1360); its Table F bands follow
the rules above. Positive LTCG still refuses for its section 54F decision.

The workbook is not itself uploadable. The Income Tax portal accepts these
Schedule CG fields only inside a complete ITR JSON; without a saved base
return, the workbook is the closest truthful portal-aligned output.

### How the workbook is built

`schedule-cg.xlsx` is one combined artifact for both sections: the STCG sheet
(ITR-2 A5, ITR-3 A6), then the LTCG sheet (ITR-2 B8, ITR-3 B9), each only
when its section has a disposal, or one summary sheet when neither has.
Each section sheet ends with its five Table F bands. Compute decides every
amount, blank, Table F row and blocker in
`ScheduleCgFilingResult` (`compute/schedule_cg_filing.py`). The plain
function `cg_filing_sheets` in `src/ibkr_fa/output/schedules/cg_filing.py`
(#1205) only projects that result: each sheet's rows, which of them default
and `--debug` output show, and the notes. It never calculates or rounds an
amount. The shared filing-workbook renderer
(`src/ibkr_fa/output/filing_xlsx.py`) lays each sheet out as an `EntrySheet`:
three notes, a blank row, the header at row 5, then the rows. It checks each
row for a NUL byte, then a formula trigger, then a text cell longer than an
Excel cell can hold (32,767 characters, counted as Excel counts them, so an
emoji counts as two), then a control character a workbook cell can't store
(#1357), before appending it, and publishes the file only after
every sheet is built. A longer cell refuses rather than being cut short
(#1323). A control character refuses naming only the row and cell index,
never the cell's text.

The refusals come in a fixed order, all before the workbook is staged.
Building the run's writer refuses a run without an ITR form or source label,
then an AY with no portal labels. Writing refuses a blocked result before
either section is projected.
