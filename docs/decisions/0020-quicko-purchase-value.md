# ADR 0020: Basis for Quicko Purchase Value on ESPP holdings

- **Status**: Accepted (2026-11-04)
- **Date**: 2026-11-04
- **Issue**: #865
- **Related**: ADR-0003, #432, #632, #633, #864, #869

## Context

The NetBenefits Quicko workbook and the tool's Schedule FA Table A3 output
give the same ESPP holding two different acquisition values. This record
asks two questions:

1. For a discounted ESPP purchase, is A3 "Initial value of the investment"
   the amount the employee paid or the FMV at purchase?
2. Does Quicko fill A3 "Initial value of the investment" from the
   workbook's "Purchase Value"?

Code references are at `b9965fa`. Web sources were retrieved on
2026-11-04 (IST).

### The two values today

- **Quicko Purchase Value is the amount paid.** The pipeline passes
  `EsppDec31Position.paid_cost_inr` to the workbook writer
  (`pipeline_netbenefits_fa.py:457`). That value is the contribution times
  the FA FX policy's rate on the purchase date
  (`compute/espp_positions.py:476-481`), and the writer only rounds it
  (`output/netbenefits_quicko_fa_xlsx.py:157`).
  `docs/portal/schedule-fa-a3.md:81-82` and `README.md:1068-1070` document
  it. #432 added the workbook with this value; neither the PR nor its
  commit says why.
- **A3 Initial value is the FMV.** `_holding_lot` puts the confirmation's
  FMV at purchase in `cost_basis_money` (`compute/espp_positions.py:399-412`).
  ADR-0003 decision 2 chose this, and `README.md:1079-1081` repeats it.
- **A test pins the difference.** `tests/test_pipeline_netbenefits_fa.py:810-811`
  says "Quicko Purchase Value is the lower amount actually paid", and the
  workbook row at `:828-843` has a Purchase Value below its A3 initial
  value.
- **Scope.** Only NetBenefits writes the workbook
  (`pipeline_netbenefits_fa_outputs.py:48-58`), and every ESPP position it
  values must be bought on 31 December (`compute/espp_positions.py:446-451`).

### Question 1: what the A3 initial value is

| Source | What it says about the A3 value |
|---|---|
| Notified ITR-2 form, AY 2026-27 ([Notification 46/2026][itr2-ay2026]) | Table A3 column (9) is headed "Initial value of the investment" (PDF p. 27). Tables B, C and D head their amount column "Total Investment (at cost)" (PDF p. 28). No note defines the A3 value. |
| Notified ITR-3 form, AY 2026-27 ([G.S.R. 228(E)][itr3-ay2026]) | The same A3 heading (PDF p. 52). |
| ITR-2 instructions, AY 2023-24, the newest found ([PDF][itr2-instructions]) | p. 38: "In table A3, the initial value of investment, peak value of investment during the accounting period, closing value of investment as at the end of accounting period", and the other A3 amounts, are disclosed after conversion into Indian currency. p. 39: tables B, C and D ask for "the value of total investment at cost", and table E for "the value of peak balance or total investment at cost". p. 40: the TTBR is taken "as on the date of peak balance in the account or on the date of investment or the closing date of the accounting period". |
| ITD FA/FSI guide ([PDF][fa-guide]) | p. 3: Table A3 covers investments "including initial value, peak value, closing value, gross interest paid, total gross amounts paid or credited, and proceeds from sale or redemption". Tables B to E ask for "investment value at cost" (pp. 3-4). §6 (p. 4) gives the same date rule. The guide says it is for AY 2025-26. |
| CBDT ITR-2 JSON schema, AY 2026-27 V1.2 ([vendored copy][itr2-schema]) | `InitialValOfInvstmnt` is an integer with no description. |
| ITR-2 and ITR-3 validation rules, AY 2026-27 V1.0 ([ITR-2][itr2-vr], [ITR-3][itr3-vr]) | No rule on the A3 value. ITR-2 rule 746 and ITR-3 rules 901-902 only require Schedule FA to be filled. |
| ITR-2 Excel utility V1.4 ([zip][itr2-utility]) and Common Offline Utility 1.2.5 ([macOS build][offline-utility]) | The Excel utility has the column label only, with no cell comment or input prompt. The offline utility's help text for the field is "Please enter the Initial value of the investment in Table A3 of Schedule FA." |
| Income-tax Act, 1961, [s.17(2)(vi)][s17] and [s.49(2AA)][s49] | The perquisite is the FMV on the exercise date "as reduced by the amount actually paid by, or recovered from, the assessee". "Where the capital gain arises from the transfer of specified security or sweat equity shares referred to in sub-clause (vi) of clause (2) of section 17, the cost of acquisition of such security or shares shall be the fair market value which has been taken into account for the purposes of the said sub-clause." Neither section mentions Schedule FA. |
| ITAT and High Court rulings | Indian Kanoon searches for `"initial value" "schedule FA"` (no results), `"schedule FA" ESPP` and `"schedule FA" "black money" ESOP` found Black Money Act cases about undisclosed or omitted assets. Examples: *Ketan Ramesh Dhamanaskar v Addl CIT*, [BMA 10/Mum/2024][dhamanaskar] (ESPP shares); *Randhir Singh v DDIT (Inv)*, [BMA 14/Mum/2024][randhir-singh]; *Kumar Ramanathan v DDIT/ADIT (Inv)*, [BMA 20-23/Chny/2025][kumar-ramanathan]. None says which value goes in the A3 cell. |

**Result: no source found says which value.** The forms, the schema and
the utilities only name the field, and the validation rules don't mention
it. The instructions and the ITD guide add only the conversion rule: the
TTBR on the date of investment. ADR-0003 decision 2 cites that rule. It supports the
conversion date, not the choice of FMV over the amount paid. No source
found supports either choice.

The texts point in some directions. Each point below is an inference, not
a finding of any authority:

1. **"Initial value", not "cost".** In the same schedule, the form asks
   for investment "at cost" in Tables B to D, and the instructions and the
   guide in Tables B to E, but all three ask for an "initial value" in A3. Read as a deliberate difference, that
   favours a value (the FMV) over the amount paid. It may also be drafting
   variation: A3 covers debt interests too, where the two figures are the
   same.
2. **The Act's cost of these shares is the FMV.** Even if "initial value"
   meant cost, s.49(2AA) makes the cost of an ESPP share the FMV taken into
   account as a perquisite, not the amount paid. But s.49(2AA) applies only
   where a capital gain arises, and its FMV is the employer's INR figure,
   not the confirmation FMV converted at the TTBR (ADR-0003 decision 2). An
   employer's INR figure would also not follow the Schedule FA rule, which
   converts a foreign-currency value at the TTBR on the date of investment.
3. **The discount is taxed as salary.** Under s.17(2)(vi) the employee is
   taxed on the FMV less the amount paid. The amount paid plus the taxed
   perquisite equals the FMV.
4. **The plain meaning of "investment" favours the amount paid.** The same
   reading gives every RSU an initial value of zero. The tool values
   StockPlan RSU vests at the FMV on the vest date
   (`pipeline_stockplan_fa.py:232-249`), so an amount-paid answer raises
   the same question there.

Practitioner material is not authority, and this record does not rely on
it. Two guides updated in July 2026 say the FMV ([GetScheduleFA][getschedulefa],
[ITRFA][itrfa]) and cite no primary text for it. Answers on Quicko's own
forum conflict (Question 2).

### "Actual cost" and "cash outlay" in the code docs

Five docstrings describe the A3 input as a cash purchase:

- `compute/initial_value.py:3-7` ("the date the cash outlay actually
  happened") and `:53` ("Calendar date of the cash outlay");
- `models/open_lot.py:74-77` ("Total acquisition cost ... It must be the
  actual cost");
- `models/holding_lot.py:33` ("Total acquisition cost");
- `models/fa_a3_row.py:110` ("Cost basis in INR").

For a bought lot the actual cost is the cash paid. ESPP lots
(`compute/espp_positions.py:409`) and StockPlan RSU lots
(`pipeline_stockplan_fa.py:244`) put the FMV at acquisition in the same
field.

- **If the answer is the FMV (ADR-0003 stands),** those docstrings are
  wrong for ESPP and RSU lots. They should say the field holds the
  acquisition value: the cash paid for a purchase, and the FMV at
  acquisition for an ESPP purchase or an RSU vest. The Quicko passages
  (`docs/portal/schedule-fa-a3.md:81-82`, `README.md:1068-1070`) change
  with the workbook.
- **If the answer is the amount paid,** the docstrings are right, and a new
  ADR supersedes ADR-0003 decision 2. `compute/espp_positions.py:399-412`,
  `README.md:1079-1081`, the NetBenefits A3 tests and the end-to-end
  goldens change. StockPlan RSU lots, which cost nothing, need the same
  decision.

### Question 2: how Quicko uses Purchase Value

| Source | What it shows |
|---|---|
| Quicko help, ["Add Schedule Foreign Assets"][quicko-help-fa] (updated 2025-08-07) | Foreign assets are disclosed "under Schedule FA" through File > Additional Details > Personal Assets & Liabilities > Add Personal Assets > Foreign. It defines no field. |
| Quicko help, ["Download ITR JSON"][quicko-help-json] (updated 2025-09-16) | Quicko compiles the return into a JSON file that "is fully compliant with the Income Tax Department’s utility", downloaded from File > Review & E-file. |
| Learn by Quicko, ["Schedule FA (Foreign Assets) Disclosure in ITR"][quicko-learn-fa] (updated 2026-07-28) | The initial value is converted at the TTBR on the "Date on which initial investment is made". Like the ITD guide, it gives the date only. |
| Quicko TaxQ&A, [post 104][qna-104] (a "Team Quicko" account, 2025-09-05) | "The “Description” field is where you enter the name of the foreign entity manually. For the address you only need to select the country. The system will then automatically populate the country name in the address field." So the flow fills Schedule FA rows. It says nothing about Purchase Value. |
| Zerodha TradingQ&A, [post 7][tqna-7] (the Quicko account, 2026-07-28) | Calls the workbook "the template of Schedule FA" and says its dividend column does not map to Schedule FSI. |
| Quicko TaxQ&A, posts [9][qna-9], [41][qna-41] and [43][qna-43] (July 2023) | Conflicting answers on the RSU initial value: "Yes you can enter the investment value to be 0"; "Initial investment = the amount paid to exercise the option"; "Also, the perquisite value will be considered an initial investment." The forum shows no Quicko affiliation for that account today. |

Two more points, both inferences:

- **The other columns line up with A3.** The tool fills Purchase Date from
  the A3 acquisition date, and Peak Value, Fair Market Value, Sale Value and
  Dividend/Interest Income from the A3 peak, closing, proceeds and gross
  credit values (`output/netbenefits_quicko_fa_xlsx.py:156-161`). Purchase
  Value is the only workbook column left for A3 "Initial value of the
  investment".
- **The same flow may feed Schedule AL.** Quicko takes foreign holdings
  under "Personal Assets & Liabilities" ([help article][quicko-help-al],
  updated 2025-07-02). Schedule AL reports assets at "the cost price of such
  asset to the assessee" ([AY 2023-24 instructions, PDF p. 41][itr2-instructions]).
  No source shows whether Quicko also puts Purchase Value into Schedule AL.

**Result: public sources don't show where Purchase Value goes.** The owner
can test it in Quicko with a before-and-after export, with no reads of
`data/`:

1. In the Quicko return that holds the uploaded NetBenefits holding, open
   File > Review & E-file, click "Download JSON" and keep the file.
2. Raise that holding's Purchase Value by ₹1 in Quicko, by editing the
   holding or by uploading the workbook again with only that cell changed.
   Download the JSON again, then restore the original value.
3. Compare the two files, for example with `diff` after
   `python -m json.tool`. List the path of every field whose value
   changed, ignoring `CreationInfo` (it holds the creation date and a
   digest).
4. In each file, check whether that holding's `InitialValOfInvstmnt`
   equals the Purchase Value it was exported with.
5. Report only the changed paths, whether the JSON has a `ScheduleAL`, and
   "yes" or "no" for each file in step 4. No amounts, names or account
   numbers.

The check shows that Purchase Value fills A3 "Initial value of the
investment" unchanged and nothing else only if the one changed field is
that holding's `ScheduleFA` > `DtlsForeignEquityDebtInterest` >
`InitialValOfInvstmnt`, step 4 is "yes" for both files, and the JSON has a
`ScheduleAL` that did not change. Any other result leaves the mapping unknown. That includes a JSON
with no `ScheduleAL`, a JSON Quicko won't build, and a step that can't be
done. The request goes to the owner through the meta thread.

## Decision

**Owner decision, 2026-11-04.** The owner settled both owner decisions
under Notes, without the Quicko check described under Question 2:

- **Rule:** for an ESPP purchase, the acquisition basis is the FMV at
  purchase, not the discounted contribution, as if the shares were bought
  without a discount. The discount (FMV less the amount paid) is salary
  income.
- **Owner decision 1 = (d):** the workbook writes each row's A3 initial
  value as Purchase Value now. The Quicko check is not required.
- **Owner decision 2 = (a):** A3 keeps ADR-0003's FMV basis.
- The refusal this record recommended (item 2, earlier text) is not
  adopted.

The research under Context stands: no source found settles Question 1,
and where Quicko puts Purchase Value (Question 2) is still unverified. The
owner's rule, not a source, sets the tool's position. The items below
describe the accepted outcome.

1. **One value per ITR cell.** The tool never gives a holding two
   different numbers for the same ITR cell, whichever channel writes it.
2. **The workbook writes the A3 initial value.** Purchase Value is each
   row's A3 initial value: the confirmation FMV converted at the
   acquisition-date TTBR, the number the filing CSV and CBDT JSON already
   write. *Earlier recommendation, superseded by the owner's decision:* a
   requested workbook would refuse, with a typed error naming this record,
   whenever a row's amount paid differed from its A3 initial value, which
   is every NetBenefits lot in practice (the config requires an ESPP
   discount above 0, `config.py:3695-3699`). It would have kept refusing
   until a source settled Question 1 and the owner check showed that
   Purchase Value fills A3 "Initial value of the investment" and nothing
   else.
3. **The ITD channel doesn't change.** A3 keeps ADR-0003 decision 2's
   basis, the confirmation FMV at the acquisition-date TTBR (owner
   decision 2 = (a)). This record doesn't change ADR-0003. The owner's
   decision 1 = (d) extends that basis to the Quicko channel now; the
   earlier text left that extension to option 1(b), after the owner check.
4. **Docs.** The follow-up (#869) corrects the five docstrings listed under
   Context so that they describe what the field holds: the cash paid for a
   purchase, and the FMV at acquisition for an ESPP purchase or an RSU
   vest. It also updates the two Quicko passages.

## Consequences

- This record's PR (#868) changed no code. The follow-up (#869) makes the
  workbook write each row's A3 initial value, so the workbook and the ITD
  outputs export the same number for a holding. The filing CSV, CBDT JSON
  and review outputs don't change.
- Runs with `--quicko-workbook` still write the workbook. Under the
  earlier recommendation, in practice every NetBenefits run with
  `--quicko-workbook` would have exited `4` before writing anything; the
  owner's decision does not adopt that refusal.
- The owner accepts option 1(d)'s risk: the mapping is unverified. If
  Quicko also puts Purchase Value into Schedule AL, the FMV lands in a
  "cost price" cell whose basis nobody has decided.
- Purchase Value comes from the row's own A3 initial value, so it is
  broker-neutral, which suits a future non-ESPP Quicko writer (#632's
  OUT-9 note; #371 and #373 for Fidelity `DO`/RSU lots).
- If ADR-0003 decision 2 is reopened, both channels follow the new basis.
- The discount stays salary income. The CSV run's ESPP perquisite
  advisory (`compute/espp_perquisite.py`) still asks the filer to check
  it in Form 16, and stays advisory.
- The s.49(2AA) capital-gains cost (`ClosedLot.cost_basis_inr_override`,
  #633) is a separate basis for a separate schedule and does not change.

## Notes

### Owner decisions

Both are decided, on 2026-11-04: decision 1 = (d) and decision 2 = (a).
The options keep their original text below, with the earlier
recommendations marked.

1. **Quicko Purchase Value.**
   - (a) Refuse a discounted ESPP lot until a source settles Question 1
     and the owner check settles Question 2 (Decision item 2). This record
     recommended (a); the owner's decision superseded it. Cost: no
     NetBenefits run can produce the workbook until then, which may be
     indefinitely.
   - (b) Once the owner check shows that Purchase Value fills A3 "Initial
     value of the investment" and nothing else, write the row's A3 initial
     value. Risk: it extends ADR-0003's FMV, which no source settles, to
     the Quicko channel. It is the number the ITD channel already files, so
     it adds no second position.
   - (c) Write a Purchase Value the filer supplies in config, per lot,
     with its source. Risk: no document found settles the basis, so the
     value records the filer's choice rather than evidence, and the
     mapping is still unproven.
   - (d) Write the A3 initial value now, before the check. Risk: it rests
     on the unproven mapping. If Purchase Value also feeds Schedule AL, the
     FMV lands in a "cost price" cell whose basis nobody has decided.
     **Chosen by the owner (2026-11-04),** with no check required.
   - (e) Keep the amount paid. Risk: if Quicko fills A3 from Purchase
     Value, which the column layout suggests, the two channels file
     different numbers in one cell, and the Quicko number contradicts
     ADR-0003.
2. **A3 initial value of a discounted ESPP lot in the ITD channel
   (Question 1).** No source settles it. What would settle it is a CBDT or
   ITD text, or a ruling, that says which value; none was found.
   - (a) Keep ADR-0003's FMV. This record recommended (a): the owner
     accepted it, no source found contradicts it, the inferences under
     Question 1 lean towards it, and (b) has no better support. **Chosen
     by the owner (2026-11-04).**
   - (b) Use the amount paid in both channels. This needs a new ADR that
     supersedes ADR-0003 decision 2, and the same decision for StockPlan
     RSU lots.
   - (c) Make the cell manual-fill in both channels until a source settles
     it. The filer would supply the value, and runs without it would exit
     `3`. No settling document was found in this research, so this moves
     the same choice to the filer.

### Follow-up

The PR that added this record (#868) carried a draft follow-up issue for
owner option 1(a), and one for option 1(b) to file only after the owner
check passed. Neither was filed as drafted. The owner chose 1(d), and
#869 adapts the 1(b) draft without the owner check.

[itr2-ay2026]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-04/Notification%20No.46_2026.pdf
[itr3-ay2026]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-04/Notification_No_47_2026.pdf
[itr2-instructions]: https://www.incometaxindia.gov.in/documents/20117/11038805/Instructions_ITR2_AY_2023_24.pdf/fe470b08-ceb7-340f-d2e3-d8fd1bf877b7
[fa-guide]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-03/Step%20by%20Step%20Guide%20FA%20FSI.pdf
[itr2-schema]: ../../src/ibkr_fa/reference/cbdt_itr2_schema_ay2026_27.json
[itr2-vr]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-05/CBDT__e-Filing_ITR%202_Validation%20Rules_AY%202026-27_V1.0.pdf
[itr3-vr]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-06/CBDT_e-filing_ITR-3_Validation%20Rules_V1.0_AY%2026-27.pdf
[itr2-utility]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-08/ITR2_AY_26-27_V1.4.zip
[offline-utility]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-10/ITDe-Filing-2026-1.2.5.dmg_.zip
[s17]: https://indiankanoon.org/doc/938899/
[s49]: https://indiankanoon.org/doc/440384/
[dhamanaskar]: https://indiankanoon.org/doc/11700382/
[randhir-singh]: https://indiankanoon.org/doc/106423546/
[kumar-ramanathan]: https://indiankanoon.org/doc/145024573/
[getschedulefa]: https://getschedulefa.com/guide/rsu-espp-schedule-fa
[itrfa]: https://itrfa.in/blog/schedule-fa-table-a2-vs-a3
[quicko-help-fa]: https://help.quicko.com/portal/en/kb/articles/add-schedule-foreign-assets
[quicko-help-json]: https://help.quicko.com/portal/en/kb/articles/download-itr-json
[quicko-help-al]: https://help.quicko.com/portal/en/kb/articles/add-personal-assets-and-liabilities
[quicko-learn-fa]: https://learn.quicko.com/schedule-foreign-assets-in-income-tax-return
[qna-9]: https://qna.tax/t/what-is-schedule-fa/4598/9
[qna-41]: https://qna.tax/t/what-is-schedule-fa/4598/41
[qna-43]: https://qna.tax/t/what-is-schedule-fa/4598/43
[qna-104]: https://qna.tax/t/what-is-schedule-fa/4598/104
[tqna-7]: https://tradingqna.com/t/taxation-related-doubts-related-to-us-investing/186232/7
