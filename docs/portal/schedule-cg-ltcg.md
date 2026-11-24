# ITR-2 / ITR-3 Schedule CG LTCG portal labels — evidence trail

The `schedule-cg-ltcg` review output reports resident foreign-security
LTCG in the residual Schedule CG bucket. The bucket number differs
between ITR-2 and ITR-3.

## AY 2026-27 destinations

| Form | Destination | Review label |
|---|---|---|
| ITR-2 | B8e | `Schedule CG / B / 8(e) LTCG from sale of assets where B1 to B7 are not applicable (B8e)` |
| ITR-3 | B9e | `Schedule CG / B / 9(e) LTCG from sale of assets where B1 to B8 are not applicable (B9e)` |

`--itr-form` selects the matching label; the calculation is shared.

## Primary evidence

* [CBDT Notification 46/2026, Schedule CG pp. 46–47](https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-04/Notification%20No.46_2026.pdf)
* ITR-2 V1.2 workbook: residual block ending at `CG!R366 = B8e`.
* ITR-3 V1.2 workbook: residual block ending at `CG!R374 = B9e`.
* The official V1.1 schemas enumerate the same destinations and
  descriptions.
* Utility links and workbook hashes:
  [`schedule-fa-a3.md`](./schedule-fa-a3.md#provenance).

ITR-2 B7 and ITR-3 B8 are the Chapter XII-A foreign-exchange-asset
rows for an NRI; neither is the resident foreign-share destination.
ITR-3's additional earlier block shifts the residual row by one.

Robinhood adds a minimal B8/B9 sheet to `schedule-cg.xlsx` only when an
LTCG equity disposal exists. The workbook covers equities only: a
long-term option capital gain is entered by hand from
`robinhood-options.csv`, and the workbook names B8/B9 for that manual
entry instead of saying no entry is required (#1322; see
[`schedule-cg.md`](./schedule-cg.md#robinhood-filing-workbook)).
A Robinhood LTCG result containing any positive gain
currently fails closed because the broker evidence cannot decide whether
the taxpayer will claim a section 54F deduction:
`build_schedule_cg_filing_result` (`compute/schedule_cg_filing.py`) records
a blocker for any LTCG line with a positive gain, and the shared
`filing-xlsx` writer refuses the blocked result. The listing venues and
security classes the sheet's filing position covers are Robinhood's, in
`compliance/schedule_cg_filing_position.py`. Zero-gain and loss-only
results remain filing-safe. The sheet omits unquoted-share, improvement,
and section 54F fields whose Robinhood contribution is zero under the
adopted filing position and positive-gain gate. Each sale's activity
`Amount` is its full value of consideration, with 0 transfer expenditure,
as for STCG (owner decision 2026-11-14, #1383; see
[`schedule-cg.md`](./schedule-cg.md#robinhood-filing-workbook)). The
section's zero transfer expenditure remains available for review with
`--debug`. The full lot audit is written with `--debug`, but a refused
filing workbook removes it too (all or nothing, #745), and the refusal
names the reason. Non-debug output shows only nonzero
editable consideration and acquisition-cost controls; the
portal-calculated LTCG rows appear only with `--debug`.

## Whole-rupee filed total

The LTCG outputs file `round_half_up(Σ proceeds_inr) −
round_half_up(Σ cost_basis_inr)`, the form's Balance, as for STCG; see
[`schedule-cg.md`](./schedule-cg.md#whole-rupee-filed-total) for the
sources, the exact sums and the zero/negative-consideration refusal. For B8 the notified
Form ITR-2 (gazette p.47) prescribes "c Balance (aiii – biv)" and "e
Long-term Capital Gains on assets at B8 above (8c – 8d)". Category A
rules ITR-2 r.148 ("In Schedule CG, Sl. No. B8c Balance should be equal
to B(8aiii-biv)") and r.149 ("In Schedule CG Sl. No. B8e of LTCG should
be equal to B(8c-8d)"), and ITR-3 r.407/408 for B9, enforce it. ITR-2
r.108 and ITR-3 r.365 refuse deductions against zero B8/B9 consideration.

## Holding period and rate regime

Foreign equity uses a 24-calendar-month holding threshold and is not
eligible for section 112A's STT-paid Indian-security rate.

For disposals on or after **2024-07-23**, the applicable regime is
12.5% without indexation. The current `schedule-cg-ltcg` path covers
that regime. Pre-cutover disposals remain outside automatic output and
are surfaced for manual handling because they require the old
20%-with-indexation computation and Cost Inflation Index evidence.
