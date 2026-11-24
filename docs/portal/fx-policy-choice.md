# FX-policy choice for Schedule FA Table A3

This page explains how `ibkr-fa` picks the SBI TT-Buying Rate ("TTBR")
that converts your USD asset values into INR for Schedule FA Table A3,
why a defensible alternative exists, and what to do if your CA prefers
that alternative.

The short version: `ibkr-fa` selects each peak in the foreign currency
first, then uses the **TTBR on the acquisition date, selected peak date,
and December 31 for closing value and every gross credit, including
sale/redemption proceeds**.
Some practitioners apply **Rule 115(1), Explanation (2)(f), by
analogy** to asset values — the preceding month-end TTBR.
Both asset-value interpretations are defensible. The realised
gain numbers on **Schedule CG** are unaffected (both `ibkr-fa` and
the alternative use Rule 115 there). The disagreement, in principle,
covers the asset-value FX rates that Schedule FA Table A3 needs —
**`initial_value_inr`**, **`peak_value_inr`**,
and **`closing_value_inr`**. Schedule FA foreign-source income uses
the December 31 rate under the ITD guidance quoted below.
For `closing_value_inr`, a *strict* Rule-115-by-analogy reading
uses the preceding month-end of December (the Nov 30 TTBR, carried
back to the most recent published rate) instead of December 31.

## What `ibkr-fa` does

For each applicable Schedule FA date — lot acquisition, peak holding,
or the calendar-year close used for closing value and gross credits — `ibkr-fa`
calls [`FaExactDatePolicy.rate_for(event_date)`][policy] which returns:

1. The TTBR published by SBI on `event_date` if one is available; otherwise
2. The most recent TTBR strictly before `event_date`, provided the gap
   is at most `max_carry_forward_days` (default 14, covering normal
   weekend/holiday gaps in the SBI archive); otherwise
3. A `MissingFxRateError` — never an extrapolated or future rate.

[policy]: ../../src/ibkr_fa/fx/policy_fa.py

A 2025-10-15 acquisition therefore uses the 2025-10-15 TTBR
(₹87.70/USD); a 2025-12-31 acquisition uses the 2025-12-31 TTBR
(₹89.47/USD); each lot's `initial_value_inr` reflects the rate on
that lot's individual trade date.

## ITD Schedule FA dates

The official ITD Schedule FA guide requires the SBI TTBR and lists
the amounts and dates in corresponding order:

| Column                                                                                          | SBI TTBR date                                       |
|-------------------------------------------------------------------------------------------------|-----------------------------------------------------|
| `Initial value of the investment`                                                               | the date of investment                              |
| `Peak value of investment during the Period`                                                    | the date of the peak balance                        |
| `Closing balance`                                                                               | the closing date of the calendar year               |
| `Total gross amount paid/credited with respect to the holding during the period`                | the closing date of the calendar year               |
| `Total gross proceeds from sale or redemption of investment during the period`                  | the closing date of the calendar year (project policy; ITD guide is silent) |

[Official ITD guide, section 6][itd-fa-guide]:

> All peak balances, values of investment, and amounts of foreign-sourced
> income must be converted into Indian currency using the telegraphic
> transfer buying rate of the foreign currency as on the relevant date: the
> date of peak balance in the account, the date of investment, or the
> closing date of the calendar year ending on December 31st.

[itd-fa-guide]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-03/Step%20by%20Step%20Guide%20FA%20FSI.pdf

The guide does not expressly say whether a peak must first be selected
in the foreign currency or by comparing each day's INR-converted value.
`ibkr-fa` defaults to the more literal sequence: select the peak in the
foreign currency, then convert it at that date's TTBR. Set
`schedule_fa.peak_currency = "inr"` to use the alternative maximum
daily INR value; every FA run using that override emits a warning.
Because peak and closing values can use different TTBRs, their converted
INR figures need not remain ordered under the native-currency default.

The guide also does not expressly prescribe an FX date for gross
sale/redemption proceeds. `ibkr-fa` uses December 31 so every Schedule FA
gross credit has one calendar-year closing rate. Gross proceeds are not
capital-gains income; this policy does not apply Rule 115 to the Schedule FA
field. Schedule CG continues to use Rule 115 independently.

## The Rule-115-by-analogy alternative

Capital-gains rule **Rule 115(1), Explanation (2)(f)** of the Income-tax Rules, 1962
(preserved into the 2025 Act framework — see issue #132 and the
"Rule 115 capital-gains FX policy" section of the project [`README`](../../README.md))
prescribes "the telegraphic transfer buying rate... as on the last
day of the month immediately preceding" the transfer of a capital
asset. That rule is **mandatory for Schedule CG** (foreign capital
gains) and `ibkr-fa` implements it as
[`Rule115CapitalGainsPolicy`][rule115cg] there.

[rule115cg]: ../../src/ibkr_fa/fx/policy_rule115_capital_gains.py

Some practitioners apply the same preceding-month-end rule to
**Schedule FA** by analogy with Schedule CG. There is no rule that requires this, and there is no
rule that forbids it; the Schedule FA text simply doesn't have
a Rule 115 equivalent and the practitioner community has resolved
that silence two ways.

## Schedule FA gross credits are separate from taxable-income conversion

Payment or credit dates still determine whether income falls inside
the Schedule FA calendar year and which holding receives it. The
foreign-currency total is then converted at the December 31 TTBR.
Sale dates likewise determine calendar-year inclusion for gross proceeds,
which use that same December 31 TTBR.

The taxable dividend figure on Schedule OS is computed separately
under [`Rule115Policy`][rule115os]. Schedule FA and Schedule OS can
therefore differ because they use different periods and conversion
instructions.

[rule115os]: ../../src/ibkr_fa/fx/policy_rule115.py

## Worked example (made-up holding)

A made-up lot of USD 1,000.00 bought on 2025-10-15 and worth
USD 1,200.00 on 2025-12-31, converted with the published SBI TT BUY
rates in [`sbi_ttbr_usd_inr.csv`][ttbr-csv]:

| Cell | Exact-date (`ibkr-fa`) | Rule 115 by analogy |
|------|------------------------|---------------------|
| `initial_value_inr` | 2025-10-15 TTBR ₹87.70 → ₹87,700.00 | 2025-09-30 TTBR ₹88.35 → ₹88,350.00 |
| `closing_value_inr` | 2025-12-31 TTBR ₹89.47 → ₹107,364.00 | 2025-11-30 is a Sunday with no rate, so 2025-11-29 TTBR ₹88.95 → ₹106,740.00 |

The two readings differ whenever the TTBR moved between the
preceding month-end and the event date, in either direction; the
difference is the USD value times the gap between those two rates.
A peak converts the same way: the selected peak date's TTBR under
exact-date, or the preceding month-end of the peak date under the
analogy.

[ttbr-csv]: ../../src/ibkr_fa/reference/sbi_ttbr_usd_inr.csv

## Practical consequences

* **Schedule CG**: Identical between both readings for ordinary purchased
  shares. Both readings use Rule 115(1), Explanation (2)(f), here, as
  the Income-tax Rules require. Section 49(2AA) employee shares instead use
  the historical INR FMV taken into account under section 17(2)(vi).
* **Schedule FA**: `initial_value_inr`, `peak_value_inr` and
  `closing_value_inr` can differ depending on which interpretation
  your CA endorses (see the worked example above). A strict
  Rule-115-by-analogy reading uses the Nov 30 TTBR for closing;
  users in that camp should override `closing_value_inr` as well
  (see "What to do" below).
* **Section 90/91 FTC / DTAA**: Unaffected — FTC arithmetic does
  not depend on Schedule FA's `initial_value_inr`.

## What to do if your CA prefers Rule 115 by analogy

`ibkr-fa` v1 does **not** offer a `--fx-policy=rule-115` flag for
Schedule FA — exact-date is the only supported policy. The
work to add a configurable Schedule FA FX policy is tracked
on the same issue (#135) that produced this document; if you
need it, please add a comment to that issue and we'll prioritise.

In the meantime, you have three options:

1. **Use ibkr-fa's exact-date numbers.** Both readings are
   defensible. Schedule FA is informational (it does not change
   your tax payable), so the choice is a presentation question and
   `FaExactDatePolicy`'s output is the more literal reading.
2. **Override the affected cells manually** on the portal /
   ITR-2 utility. The Schedule FA A3 review CSV is a review
   artifact, not an upload; you re-enter the values on the
   e-filing portal anyway. For each cell where `ibkr-fa` computed
   an INR figure using exact-date FX, replace it with the
   Rule 115 figure computed from the SBI archive. The Rule 115
   specified date for any event is the **last calendar day of the
   month immediately preceding the event** (e.g., 2025-11-30 for
   a December 2025 event); if SBI did not publish on that date,
   carry back to the most recent prior published rate (e.g.,
   2025-11-29 for Dec 2025 events). This is what the codebase's
   `Rule115Policy` does. The cells your override needs to touch:

   * `initial_value_inr` — always computed by `ibkr-fa` (one
     row per lot held during the calendar year). Replace each
     with
     `usd_initial × TTBR(preceding_calendar_month_end_of_acquisition_date)`
     with the carry-back described above.
   * `gross_amount_paid_credited_inr` — not part of this
     alternative. The ITD Schedule FA guide assigns foreign-source
     income to the December 31 TTBR.
   * `peak_value_inr` — computed from the native-currency peak using
     exact peak-date TTBR when daily prices are available. If you replace
     it, use the FX rate consistent with your CA's preferred policy:
     `usd_peak × TTBR(preceding_calendar_month_end_of_peak_date)`
     for the Rule 115 interpretation;
     `usd_peak × TTBR(date_of_peak)` for exact-date.
   * `gross_proceeds_from_sale_inr` (portal label: "Total gross
     proceeds from sale or redemption of investment during the
     period") — closed lots with proven pre-fee consideration use
     `gross_usd × TTBR(December 31)`; open lots use 0. A source that
     supplies only commission-reduced net cash leaves this cell blank.
     Robinhood files each closed lot's exact share of its sale's
     activity `Amount`, which may be net of sale fees, as gross
     proceeds (`gross_usd`) at the December 31 TTBR (owner decision
     2026-11-14, #1383; ADR 0006 section 7), so don't blank that cell
     or replace it with `quantity × execution price`. For every other
     source, fill or override it only from verified gross
     consideration (`quantity × execution price`). If your CA
     prefers the Rule 115 interpretation, use
     `gross_usd × TTBR(preceding_calendar_month_end_of_disposal_date)`.
   * `closing_value_inr` — if your CA wants a *strict* Rule-115-
     by-analogy reading, replace with
     `usd_close × TTBR(2025-11-30 → carries back to 2025-11-29)`
     (the preceding-calendar-month-end of Dec 31, with the
     standard carry-back). Otherwise leave the Dec 31 figure.
3. **Wait for a configurable FX policy.** Track #135 and PR
   when it lands.

## Deferred from this page

Issue #135's original action list includes the following items
deliberately left out of this PR:

* **A `--fx-policy=rule-115` flag** for the `schedule-fa`
  subcommand (issue #135 item 3). Deferred because the flag
  requires a configurable Schedule FA FX policy abstraction
  and a second SBI lookup pathway with carry-back semantics;
  beyond the doc-only scope of this PR. Track on #135.
* **An FX-policy summary in `ibkr-fa schedule-fa --help`**
  (issue #135 item 2 mentions "README + CLI `--help` text").
  This PR updates the README callout but leaves the CLI
  subparser `description` unchanged to keep this PR doc-only;
  a small follow-up can add a one-paragraph `--help`
  callout pointing at this page.
* **Outreach to another tool's maintainers** (issue #135 item 4).
  External action, outside repo scope.

## Cross-references

* PR #132 / #126 — cross-AY guardrail acknowledging the same
  Rule-115-vs-exact-date ambiguity exists into the 2025-Act
  framework and is not a transition artefact.
* Issue #28 — `feat(fx): Rule 115 policy (TTBR of last day of
  preceding month)` — the underlying Rule 115 implementation
  used by Schedule CG today.
* Issue #34 — Rule 128 policy work. Its historical title said
  "preceding day"; the implemented and legally correct treatment is
  preceding-calendar-month-end for the DTAA FTC computation.
* [`README`](../../README.md) "Rule 115 capital-gains FX policy" — the
  Schedule CG context where Rule 115(1), Explanation (2)(f), is mandatory.
* [`FaExactDatePolicy`][policy] docstring — code-level summary of
  this page.
