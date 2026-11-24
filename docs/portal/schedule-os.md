# ITR-2 / ITR-3 Schedule OS portal labels — evidence trail

This file records the AY 2026-27 Schedule OS rows relevant to ordinary
portfolio dividends, foreign broker cash-balance interest, and residual
other-source income.

## Primary evidence

Both official V1.2 utilities have the same Schedule OS layout:

* [ITR-2 V1.2](https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-07/ITR2_AY_26-27_V1.2.zip),
  workbook SHA-256
  `e5ce02f3ffc848d7fb708d4157c6c41af12013e68ee84686715b788c1f9fb785`
* [ITR-3 V1.2](https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-07/ITR3_AY_26-27_V1.2.zip),
  workbook SHA-256
  `863281c4f66aa49e374a39ad1d0fc726e321c2ce3d506fc28d7e2d81c8f80c4c`

The labels below were read from `OS!F5:F9` and `OS!G10:G18`. The matching
V1.1 schemas use `DividendOthThan22e`, `Dividend22e`, `Dividend22f`,
`NatofPassThrghIncome`, and `IntrstFrmOthers`.

## Official AY 2026-27 rows

| Row | Utility label | `ibkr-fa` source |
|---|---|---|
| 1a(i) | `Dividend income [other than (ii) and (iii)]` | Supported ordinary cash, special, and choice dividends |
| 1a(ii) | `Dividend income u/s 2(22)(e)` | Not inferred from broker issuer country |
| 1a(iii) | `Dividend income u/s 2(22)(f)` | Not inferred from broker issuer country |
| 1b(iv) | `In the nature of Pass through income/ loss` | Not foreign broker cash interest |
| 1b(ix) | `Others including interest from Companies, NBFCs & HFCs` | Supported foreign broker cash-balance interest |
| 1e | `Any other income (please specify nature)` | Supported residual receipts, including Robinhood securities-lending fees |

The live portal's Section 10 accrual table uses the exact columns `Sl No.`,
`Other Source Income`, `Upto 15/6`, `From 16/6 to 15/9`, `From 16/9 to
15/12`, `From 16/12 to 15/3`, and `From 16/3 to 31/3`. Section 10 has no
total column.

Slab-rate dividends go in row `3a.` (`Dividend Income referred in Sl.no.
1a(i)`), never in row `9.` (`Dividend income taxable at DTAA rates`). Row 9
is the special-rate treaty bucket, tied to Schedule OS row 2f. A section 90
foreign-tax-credit claim does not move a dividend into row 9: the credit is
relief against Indian tax computed under the Act, filed via Schedule FSI,
Schedule TR, and Form 67. Row 9 applies only when the Indian tax on the
dividend is itself computed at the treaty rate.

The [CBDT ITR-2 validation rules for AY 2026-27
V1.0](https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-05/CBDT__e-Filing_ITR%202_Validation%20Rules_AY%202026-27_V1.0.pdf)
enforce this as Category A defects, which block upload:

* Rule 214: the row 3a quarterly break-up must equal `1ai (Dividend, gross)
  - DTAA Dividend other than 2(22)(e) - System calculated value of Interest
  expenditure u/s 57 attributable to Dividend other than 2(22)(e)`.
* Rule 219: the row 9 quarterly break-up must equal the dividend income
  selected at Sl. No. 2f.

So the five row-3a periods equal 1a(i) only when both the 2f treaty amount
and the section 57(1) interest deduction are nil. `ibkr-fa` computes neither
deduction, so a user claiming section 57(1) interest must reduce the row-3a
periods themselves.

## Implemented mapping

`ibkr-fa` emits one 1a(i) total across every supported ordinary dividend,
summing at full precision before whole-rupee rounding. Parsed free-cash-
balance interest goes only to 1b(ix). Separately modelled substitute payments
and securities-lending fees go to 1e under distinct natures; neither is folded
into ordinary dividends or interest. Receipt-level exact values remain in the
review output while filing output groups and rounds each 1e nature at the
portal boundary. It does not populate 1a(ii), 1a(iii), or 1b(iv) without the
specific evidence those rows require.

Schedule FSI's Other Sources income is split from these filed whole rupees
(1a(i), 1b(ix) and each 1e nature), so it adds up to them; see
[Schedule FSI](./schedule-fsi.md) for the split and ITR-3 validation rule 894.

Section 10's quarterly accrual table stays dividend-only: validation rules
214 and 219 constrain rows 3a and 9, and no rule requires an interest
break-up there.

## Filing CSV and workbook

The filing CSV (`filing-csv`) and the filing workbook (`filing-xlsx`) come from
one set of schedule functions in `src/ibkr_fa/output/schedules/os.py` (#1202).
Every amount is read from the compute result: the 1a(i) and 1b(ix) totals,
each 1e nature's whole-rupee total, the five section 10 periods and the Quicko
splits by security and by account. Nothing there rounds, allocates or adds
money.

* **Shared ITR rows:** section 1's fields (1a(i), 1b(ix) and one 1e row per
  nature) and section 10's row 3a periods, as text, each section with its
  instruction to add the amounts to existing values rather than replace them.
* **CSV:** the ITR rows, a blank row, then Quicko's two dividend screens as
  vertical blocks, one per security. A foreign dividend goes under "Add Foreign
  Dividend Income" with its country and a description; the description adds
  the ISIN when two securities of one country share a ticker. Indian-source
  dividends go under "Add Dividend Income" with their total and an expense
  line marked "not computed". The file is UTF-8 with a BOM and LF line endings,
  and every cell is checked for a NUL byte and then a formula trigger
  (`FILING_WORKSHEET` in `src/ibkr_fa/output/filing_csv.py`).
* **Workbook:** the ITR rows on the "ITR Schedule OS" sheet, checked as text
  and then converted: blank text becomes an empty cell and whole-number text a
  number. Then a "Quicko by Account" sheet, when there are dividend lines and
  every one names its account, and a "Quicko by Security" sheet. When both are present, each
  says they are alternative views of the same total, to enter once. Accounts
  that share a label are told apart by their index.

Each artifact evaluates only what it writes. The CSV never asks for the
account split, so dividend lines of which only some name their account refuse
only the workbook.
Both refuse a result without the separately computed interest total before
the CSV is staged or the workbook is created.

## Source-country versus Schedule OS destination

Source-country classification remains necessary for Schedule FSI, Schedule
TR, Form 67, withholding reconciliation, and the audit trail. It does not
split ordinary dividends across Schedule OS rows: both Indian-source and
foreign-source ordinary dividends belong in 1a(i) for AY 2026-27.

An amount belongs in 1a(ii) or 1a(iii) only with explicit evidence that it is
a section 2(22)(e) or 2(22)(f) dividend. Issuer country, ISIN prefix, or ADR
status is not that evidence.
