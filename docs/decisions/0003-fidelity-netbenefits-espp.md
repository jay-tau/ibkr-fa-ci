# ADR 0003: Fidelity NetBenefits (Microsoft ESPP) data model

- **Status**: Accepted
- **Date**: 2026-06-27
- **Deciders**: jay-tau
- **Issue**: #305 (tracker), #306 (this ADR)
- **Amended**: 2026-11-01 (#629) to drop `asset_class` / `sub_category` from
  config (§5)

## Context

`ibkr-fa` ingests only IBKR Flex/Activity CSVs today. A resident taxpayer also
holds **Microsoft ESPP** shares custodied at **Fidelity Stock Plan Services LLC**
(NetBenefits). The same ITR-2 obligations apply — **Schedule FA** (A3 holding +
A2 custodial account) plus **Schedule OS + FSI/TR/Form 67** for the dividend —
but the input is structurally different:

- CUSIP, not ISIN; USD-only; no Dec-31 mark price on the holding.
- A **multi-file bundle** with deliberate duplication: open/closed-lots CSV,
  SPS PDF statements, and per-purchase trade-confirmation PDFs.
- **Three** distinct ESPP cost figures — the employee **contribution**, the
  **FMV at purchase**, and the **perquisite** (the discount taxed as salary) —
  which the US documents and the Indian tax treatment use differently.

`compute/` and `output/` are already broker-agnostic: they consume the models
`OpenLot`, `ClosedLot`, `Dividend`, `WithholdingTax`, `AccountInfo`, `FaA3Row`,
`FaA2Row`. Only `parser/` is IBKR-specific. So Fidelity support is **new parsers
emitting the same models, routed through the existing row builders** — no fork of
the tax logic. The risky part is not the plumbing; it is a handful of
"it's taxes — never be wrong" mapping traps that must be decided **once, up
front**, the role ADR-0001 (TTBR) and ADR-0002 (CBDT JSON) played for their
features. These decisions were reviewed by a rubber-duck pass and a three-model
external panel (Gemini 3.1 Pro, Claude Opus 4.8, GPT-5.5); every consensus fix is
folded in below.

## Decisions

1. **ESPP fields are explicit and unit-safe.** The raw model carries
   `fmv_per_share_usd`, `fmv_total_usd = shares × fmv_per_share_usd`,
   `purchase_price_per_share_usd`, `contribution_usd`, and
   `perquisite_usd = fmv_total_usd − contribution_usd`. The perquisite uses the
   **total** FMV, not the per-share FMV: the panel caught that the shorthand
   "FMV − contribution" with a per-share FMV computes a *negative* perquisite
   for any lot whose contribution exceeds one share's FMV, and would refuse
   valid data. The Yahoo
   cross-check (decision 3) targets `fmv_per_share_usd`.

2. **FA A3 "Initial value of investment" = `fmv_total_usd` converted at the
   acquisition-date SBI TTBR.** Schedule FA is an asset disclosure: the CBDT
   guide requires the value of investment to be converted on the date of
   investment. It is independent of the salary-perquisite conversion and need
   not reproduce Form 12BA. The trade-confirmation "Market Value at Purchase"
   (`fmv_total_usd`, Yahoo-cross-checked per decision 3) is therefore the A3
   source. The CSV "Cost basis" / `cost_per_share` is the **discounted price the
   employee paid** and is never used as `OpenLot.cost_basis_money`.

   This is deliberately distinct from future Schedule CG. Section 49(2AA)
   uses the gross FMV **actually taken into account for §17(2)(vi)**, normally
   evidenced lot-by-lot by the employer's perquisite worksheet. Form 12BA's
   aggregate taxable discount proves the step-up applies but does not allocate
   gross FMV across lots. No authority squarely resolves the FX interaction;
   missing historical employer INR evidence must refuse rather than silently
   substitute the A3 acquisition-date conversion.

   For Schedule CG, `[[netbenefits.cg_fmv_values]]` supplies the exact
   historical **gross INR FMV** taken into account under §17(2)(vi), with its
   source. The adapter prorates that INR amount across partial sales and
   converts only sale proceeds under Rule 115(1), Explanation (2)(f). No direct
   ITAT ruling settles the §49(2AA) FX interaction; missing historical INR
   evidence therefore refuses instead of substituting an acquisition-rate
   proxy or revaluing the basis at the sale rate. *Biplab Adhya v ITO*,
   IT(IT)A 720/Bang/2022, para 10; *Anil Bhansali v Addl CIT*,
   ITA 320/Hyd/2018, paras 16-19; and *Ramamoorthy Sridharan v ITO*,
   ITA 1238/Hyd/2008 address whether FMV qualifies as §49(2AA) cost, not
   this FX timing question.

3. **Source-of-truth: statements for dividends; override or Yahoo for the
   Dec-31 close.** The Stock Plan Services statements are the sole source of
   the dividend gross. No dividend check against Yahoo runs: the price feed
   (`src/ibkr_fa/prices/yahoo.py`) requests only `events=split`. The dividend
   proof is internal to the statements: on each date, the gross dividends plus
   the signed withholding must equal the amount swept into the money-market
   fund; the Stock Plan Services reports must cover the financial year, from
   the account opening date if later, with no gap; each withholding must match
   exactly one dividend of the same date and name; and (#1249) the income rows
   a year-end report prints must match the Stock Plan Services reports. The
   supported year-end statement is unpriced, so the Dec-31 close is a
   `[[netbenefits.price_overrides]]` entry, cross-checked against Yahoo when a
   Yahoo close is available, or else Yahoo as a flagged market fallback. A
   mismatch refuses, and so does having no source
   (`src/ibkr_fa/compute/dec31_close.py`). The close is then anchored to the
   confirmation's Market Value at Purchase for the Dec-31 purchase
   (`src/ibkr_fa/compute/espp_positions.py`).

   *Amended 2026-11-15 (#315, PR #333):* the Yahoo dividend verifier was
   dropped as not implementable; the price feed carries no dividend events.

4. **Dec-31 holdings must be proven, not assumed.** The open-lots CSV is a
   *current* snapshot: a lot held on Dec-31 but sold before the file was
   downloaded vanishes silently. The supported year-end statement is unpriced
   and has no holdings table. The Dec-31 position is the surviving open lot
   plus any closed lot acquired on Dec-31 and sold after it, reconciled exactly
   to the purchase confirmation's shares and contribution. Any other closed lot
   held during the calendar year refuses, because Schedule FA for such lots is
   not supported. The statements, which must cover through ten days after
   Dec-31, and every confirmation must prove that the Dec-31 purchase is the
   only lot held in the year. The closed-lots CSV is required. The tool must
   never assume the closed-lots export is empty.

   *Amended 2026-11-15 (#342, #1160):* gap-window lots are folded in, not
   refused; the proof is the confirmations and statements, not year-end
   holdings.

5. **Identity fields no Fidelity file supplies are config-sourced, never
   fabricated.** `listing_exchange` and `issuer_country_code` come from
   `[netbenefits]` config (known, user-confirmed constants for MSFT), not
   hardcoded defaults.

   *Amended 2026-11-01 (#629):* this ADR deferred a broker-agnostic lot model
   (YAGNI), and #629 has now done it. The lot models carried IBKR Flex columns
   that nothing read (`asset_class`, `cusip`, and on open lots `sub_category`
   and `listing_exchange`), so every non-Flex source had to make up IBKR
   vocabulary for them, and NetBenefits users had to configure
   `asset_class = "STK"` / `sub_category = "COMMON"` for no filed or reviewed
   value. The fields are gone. `[netbenefits.security]` no longer takes those
   two keys, and a config that still sets one is refused with a message to
   delete it. A closed ESPP lot states no security class (`sub_category` is
   `None`).

6. **Participant Number is the A2 account identifier.** The observed
   NetBenefits statements and confirmations expose the Participant Number as the
   only cross-document account identifier, so it is used for A2 and cross-checked
   against those documents. There is no separate NetBenefits account-number
   input. The account opening date remains sourced-or-refused and is **not**
   inferred from the first purchase date.

7. **A3 alone is not a complete FA disclosure.** A3-only output is marked
   "A2 pending — not filing-complete", and the **CBDT JSON FA fragment is gated
   on A2+A3 both present** — consistent with ADR-0002 decision 5. This avoids a
   holdings-only emission that silently omits the account-level disclosure
   (a Black Money Act exposure).

8. **A2 closing = Dec-31 securities value plus proven-zero cash; A2 peak =
   manual unless supplied.** A2 closing is the Dec-31 securities value at the
   Dec-31 TTBR plus cash proven to be $0, and it must equal Σ A3 closing INR
   exactly. A priced Dec-31 statement of any report type refuses, because its
   stated NAV must be filed in A2 by hand. The peak is never derived from A3 or
   month-ends. The CSV leaves it manual; the CBDT JSON files the year's
   `[[netbenefits.a2_peak_overrides]]` value and, without one, writes no
   fragment and exits 3. This exact **A2-vs-A3 reconciliation** (A2 closing =
   Σ A3 closing INR) is in the *spirit* of the IBKR path's existing NAV
   corroboration but is a structurally different computation: the existing
   `audit_nav_cross_checked` (`src/ibkr_fa/compute/fa_a2.py`, passing through
   `nav.cross_checked`) only checks the statement's NAV total against its own
   Change-in-NAV "Ending Value" — both figures internal to the statement, with
   the refuse-on-divergence living in the parser — and never sums A3 holdings.
   The A2-vs-A3 check here is new.

   *Amended 2026-11-15 (#335):* the supported statements are unpriced, so A2
   closing is derived and a priced Dec-31 statement refuses.

9. **Dividend → OS/FTC.** Statement gross is primary (the sole source; no
   Yahoo dividend check runs, see decision 3). Income is
   recognised to the **pay-date FY**. The two amounts use **different** FX rules,
   matching the existing IBKR pipeline: the **gross dividend income** converts via
   **`src/ibkr_fa/fx/policy_rule115.py`** (Rule 115(1), Explanation (2)(e), preceding-month-end TTBR — wired as
   `Rule115Policy` for dividends), while the **foreign tax withheld** converts via
   the **FY-side `src/ibkr_fa/fx/policy_rule128.py`** (Rule 128(5)(ii) — wired as
   `Rule128Policy` for WHT/FTC). Neither uses the FA calendar-year policy. The FA
   gross-paid is a *known* `Decimal("0")` when every in-CY record date predates
   the lot's acquisition — never left blank.

   *Amended 2026-11-12 (#1249):* a year-end investment report's silence proves
   nothing about income, so only the Stock Plan Services reports count towards
   the income coverage (#333) and the Table A2 cash-zero proof's coverage
   (#335). But an income row a year-end report prints is evidence, as #1151
   settled for purchase rows, and it must match the Stock Plan Services
   reports. For income, each in-FY dividend and withholding a year-end report
   prints must appear, with the same date, identity and amount, on a Stock
   Plan Services report covering that day, as many times as the year-end
   report prints it, or the run refuses. For Table A2, a dividend, withholding
   or sweep row a year-end report prints on or before Dec-31 refuses, as it
   does on a Stock Plan Services report.

   *Amended 2026-11-15 (#315, PR #333):* the Yahoo dividend verifier was
   dropped; see decision 3.

10. **FTC magnitude and articles.** Form 67 credit =
    `min(actual WHT, DTAA 25% rate cap, Indian tax on the dividend)`. The 25% cap
    is the **DTAA Art 10(2)(b)** withholding rate (individual / portfolio holder;
    the 15% rate of Art 10(2)(a) is only for ≥10% corporate holders). Form 67
    emits **Art 10(2)(b)** beside that rate; Schedule FSI separately emits
    **Art 25(2)(a)**, the article under which India grants relief. These are the
    `withholding_article` / `relief_article` pair stored for US dividends in
    `src/ibkr_fa/reference/dtaa_dividend_rates.csv`.

11. **Capital-gains class from dates, not Fidelity's US label.** Holding period
    is judged by the Indian 24-month rule (§112, slab-rate STCG; never §112A /
    §111A); Fidelity's "Short/Long" term is kept raw and **ignored** for
    classification. US-listed MSFT is foreign equity not on a recognised Indian
    exchange — already encoded in `src/ibkr_fa/compute/cg_section.py` (every LTCG row is
    §112, never §112A).

    *Note (2026-10-29, #603):* `cg_section.py` now holds only the §112 / slab-rate
    advisory text that the CLI prints. The unused `classify_cg_section` helper was
    removed.

12. **Peak-value methodology is the IBKR A3 method.** A3 selects
    `max(close × qty)` in native currency over daily bars, then applies the
    selected date's TTBR. The warned INR-max override is also shared with the
    IBKR path in `src/ibkr_fa/compute/fa_a3_peak_value.py` (#14), not re-derived.

13. **One input bundle per command.** Each NetBenefits command,
    `netbenefits-all` included, reads one bundle directory (`--input`). Each
    leg picks its own window from it: Schedule FA the calendar year (in
    `netbenefits-all`, the FY's start year), income and CG the Indian FY. The
    separate `--cy-input` / `--fy-input` flags are IBKR `run-all`'s. Nothing
    dated after December 31 enters that calendar year's FA disclosure:
    Schedule FA takes only lots held in the calendar year, and the A2 cash
    proof scans activity dated on or before Dec-31.

    *Amended 2026-11-15 (#331, #360):* the NetBenefits commands were built with
    one `--input` bundle.

## Consequences

This ADR is the contract for the issues tracked under #305. The implementation
is **new parsers only**: open/closed-lots CSV (CUSIP→ISIN derived via a small
helper alongside the existing ISIN-shape validation in `src/ibkr_fa/parser/_isin.py`),
trade-confirmation PDFs, and the SPS statement (`pdftotext` over anonymized text
fixtures, no new PDF dependency). They emit the existing models and route through
`build_fa_a3_rows` / `build_fa_a2_row`, so peak-value, gross-proceeds, and
gross-amount logic stay identical to the IBKR path.

The cost is a stricter refuse surface than the IBKR flow: missing config
identity (decisions 5, 6), an unproven Dec-31 holding (decision 4), an
unverifiable closing value or dividend (decisions 3, 9), or a missing A2
(decision 7) each stop the run with an exact "supply X" message rather than
emit a guessed figure — the deliberate trade-off demanded by "it's taxes —
never be wrong".
