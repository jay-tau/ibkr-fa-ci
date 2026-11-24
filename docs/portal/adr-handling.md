# ADR / GDR attribution on Schedule FA Table A3

This page explains how `ibkr-fa` handles **American Depositary Receipts
(ADRs)** and **Global Depositary Receipts (GDRs)** when attributing
Schedule FA Table A3 column 1 (the "Country / Region name and code" of
the entity that issued the asset).

The short version: ADRs of Indian companies on US exchanges (INFY,
HDB, IBN, WIT, RDY, etc.) land under **"United States of America"**
on Schedule FA, not under India. This page explains why, and what to
do if your CA prefers the contrarian underlying-issuer attribution.

## What `ibkr-fa` does

For IBKR rows, the `Country / Region name and code` cell is derived
from the **first two characters of the ISIN** (ISO 6166), which is
the ISO 3166-1 alpha-2 country code of the security's primary
identifier registry. For ADRs/GDRs, that's the depositary's country
— typically **USA** for NYSE/NASDAQ-listed ADRs, **GBR** /
**LUX** for older Indian ADRs trading on London or Luxembourg as
GDRs.

Other sources don't read the ISIN for this cell. Robinhood uses each
`[[robinhood.securities]]` entry's `issuer_country_code`; NetBenefits
and StockPlan use `issuer_country_code` in `[netbenefits.security]` /
`[stockplan.security]`; Fidelity CMA's SPAXX rows are always the
United States. When loading config, `ibkr-fa` checks a configured
code against the CBDT country table, not against the ISIN prefix.

The two IBKR inputs reach that country differently:

- **Activity Statement** lots take the country from the ISIN prefix.
- **Flex Query** lots, both Open Positions and Closed Lots, take IBKR's
  `IssuerCountryCode` column. IBKR does not document that column, nor what
  it means for an ADR. When it differs from the ISIN prefix, which country
  A3 should report is **unresolved**: no primary authority has been recorded
  for either. Until one is, `ibkr-fa` refuses such a lot with a parse error
  naming its row (exit 4;
  [#794](https://github.com/jay-tau/ibkr-fa/issues/794)).

Foreign tax withheld on a dividend (Schedule FSI, Schedule TR and Form 67)
is filed under the same ISIN-prefix country. An Activity Statement
withholding row can also state which country took the tax: its description
ends ` - US Tax`, ` - NL Tax` and so on. A stated country must match the
ISIN's country, or `ibkr-fa` refuses the run with an error naming the
row's statement line (exit 4;
[#1282](https://github.com/jay-tau/ibkr-fa/issues/1282)), because filing it
would credit it as the wrong country's tax. Under the default
`depositary` routing this includes an Indian ADR whose withholding names
India. Under `underlying-issuer` routing (path 2 below), mapped Indian-ADR
rows are left out before the check. A row without that exact ending
(two uppercase letters) states no country and is filed as before.

For Robinhood A3 identity fields, a supplied SEC Form F-6 can additionally
prove the exact depositary charter name. `ibkr-fa` accepts only reviewed
charter-name-to-LEI bindings, then uses that depositary's exact active GLEIF
record for name, legal address, ZIP, and legal form. Unknown depositaries stay
manual; the underlying issuer's GLEIF record is never substituted.

## Why depositary-country attribution

An ADR is, legally, **a US-issued security**. You don't own shares of
the underlying Indian company — you own a US-issued claim on shares
held in trust by a US depositary bank (typically BNY Mellon, JPMorgan,
or Deutsche Bank's US trust arm). The ADR has:

- A US-prefixed ISIN (e.g., Infosys ADR ISIN `US4567881085` —
  prefix `US`).
- A US CUSIP.
- US securities-law trading rules (Reg S / 144A as relevant).
- A US-domiciled issuer of record (the depositary bank), with the
  underlying Indian shares as the asset backing the issue.

The depositary's ISIN identifies the asset you actually hold and can
dispose of in your IBKR account. Schedule FA's "Country code of
country/region" column asks about the asset's issuer **as held**,
which is the depositary, not the foreign operating company whose
business backs the depositary's issue.

This is the same convention used by:

- IBKR's own US-side regulatory reporting (1099-DIV / 8949 attribute
  ADRs to the depositary's US filings).
- The CBDT FAQ on Schedule FA (which doesn't address ADRs explicitly
  but consistently uses the asset's own jurisdiction throughout).

## Concrete examples

The Indian companies most commonly held by Indian residents through
US ADR programmes:

| Underlying company | ADR ticker | ADR ISIN |
|--|--|--|
| Infosys | INFY | `US4567881085` |
| ICICI Bank | IBN | `US45104G1040` |
| HDFC Bank | HDB | `US40415F1012` |
| Wipro | WIT | `US97651M1099` |
| Dr. Reddy's Laboratories | RDY | `US2561352038` |
| WNS (Holdings) | WNS | `JE00B3DCF752` (Jersey-prefixed ISIN) |
| Yatra Online | YTRA | `KYG983381099` (Cayman-prefixed) |

(Azure Power Global / AZRE was previously listed here but was
**delisted from NYSE on 13 November 2023**; post-delisting trading
is OTC as `AZREF`, and no public dividend events are expected from
the delisted, governance-troubled SPV. Dropped from the
[`INDIAN_ADR_MAPPING`](../../src/ibkr_fa/reference/indian_adr_mapping.py)
table per PR #184 T1 correctness P1.)

All of the first five land on the Schedule FA review CSV under
**United States of America** — `ibkr-fa` is doing the right thing by
the asset-as-held rule. The bottom two are interesting edge cases:
some Indian-business ADRs have non-US depositaries (Jersey, Cayman)
— `ibkr-fa` attributes those to their respective issuance
jurisdictions, again per the ISIN-prefix rule.

GDRs trading on London Stock Exchange or Luxembourg Stock Exchange
follow the same logic: country attributed per the GDR's depositary
location (`GB` / `LU`), not the underlying Indian issuer.

## The contrarian school: underlying-issuer attribution

Some CAs file ADRs of Indian companies under **India** rather than
**USA**, on the theory that Schedule FA is intended to capture
beneficial economic exposure to a foreign issuer, and an ADR is "just
a wrapper" for the underlying Indian shares. Under this interpretation:

- Indian-company ADRs would NOT appear on Schedule FA at all (since
  India ≠ "foreign").
- The economic exposure would be captured elsewhere on the return
  (Schedule OS / CG for dividends and disposal gains; no Schedule FA
  disclosure).

This interpretation is **uncommon** but not unheard of in practice. It
appeals to the substance-over-form principle and the policy intent of
Schedule FA (capturing genuinely foreign exposure).

`ibkr-fa` does not implement this interpretation as a default for
Schedule FA. If your CA prefers underlying-issuer attribution, you
have two paths:

1. **Manually drop the affected rows** (if your CA's interpretation is
   that Indian-company ADRs don't need Schedule FA reporting at all).
   Table A3's country dropdown has no India (`Country_NoIndia`; see
   [`schedule-fa-a3.md`](./schedule-fa-a3.md)), so a row can't be
   moved to India instead. Find the rows with the review CSV's audit
   columns: `_audit: Source ISIN` keeps the full ADR ISIN, and
   `_audit: Source country (ISO 3166-1 alpha-2)` shows the country
   the row is filed under. For an IBKR row, the portal-facing
   `Name of entity` (column 2) is the `[[fa.entities]]` `name` when
   one is set, else the statement's security description, which for
   an ADR typically includes "ADR" or "AMERICAN DEPOSITARY". Skip
   those rows when you enter Table A3.

2. **Opt in to underlying-issuer source attribution for income/FTC
   schedules** (issue #183): set
   `[os].schedule_os_indian_adr_routing = "underlying-issuer"` in
   `user.toml`. This attributes dividends from the hardcoded mapping
   table (INFY, IBN, HDB, WIT, RDY, plus the non-US-depositary
   Indian-business cases WNS / YTRA) to India rather than the
   depositary country for FSI/TR/Form 67, with the audit trail
   preserved. Every supported ordinary dividend enters Schedule OS
   1a(i), so this option does not change the Schedule OS total.
   **Note**: Schedule FA Table A3 continues to attribute ADRs to the
   depositary country per the default convention above. To leave
   them off Schedule FA too, drop them by hand (path 1).

   **Symmetric WHT exclusion (issue #225)**: when
   `underlying-issuer` routing is active, the WHT withheld on
   re-routed Indian-ADR dividends is also excluded — symmetrically —
   from Schedule FSI's per-country foreign-tax-paid aggregation and
   from Form 67's per-(country, source) bucket. Without this
   symmetric exclusion, INFY/HDB/IBN/WIT/RDY/WNS/YTRA WHT (keyed by
   depositary jurisdiction = `ISIN[:2]`) would silently absorb into
   the depositary country's bucket (e.g., US) alongside genuine
   non-Indian-ADR WHT and over-claim Foreign Tax Credit. The skip
   covers both paid WHT and downstream refunds (negative WHT rows).
   For users entitled to recover the withheld tax against their
   Indian tax liability, the recovery path under `underlying-issuer`
   routing is via Schedule TDS / Self-Assessment Tax / refund
   mechanisms — not via Schedule FSI + Form 67's §90/§91 FTC
   pathway, which is only for genuinely foreign-source income.

Either path requires the user's explicit decision — `ibkr-fa` cannot
automate it without making a policy choice that isn't its to make.
Path 2 is automation behind an explicit opt-in config flag (the
default remains the asset-as-held convention).

## Why this matters

- Indian retail investors very commonly hold Indian-company ADRs via
  IBKR (it's the canonical way to invest in Indian companies from a
  US-based brokerage account).
- The "USA vs India" attribution decision affects:
  - Which Schedule FA rows appear.
  - The Schedule FSI per-country FTC breakdown (foreign tax credit
    for taxes paid abroad).
  - Whether the row counts towards Schedule FA's "foreign assets held
    by an Indian resident" disclosure (the entire raison d'être of
    Schedule FA).
- Without explicit documentation, a user might wonder why their INFY
  / HDB holdings appear under "United States of America" — and a CA
  asking *"why is this Indian company on Schedule FA at all?"* needs
  a quick answer.

## Audit-trail preservation

The Schedule FA review CSV's audit columns preserve every datum
needed to find ADR rows by hand:

| Column | Content for an ADR row |
|--|--|
| `_audit: Source ISIN` | The full 12-character ADR ISIN (e.g., `US4567881085`) |
| `_audit: Source ticker` | The IBKR-emitted ticker handle (e.g., `INFY`) — primary identification key |
| `_audit: Source country (ISO 3166-1 alpha-2)` | Depositary country derived from `ISIN[:2]` (e.g., `US`) |
| `Name of entity` (portal column 2) | For an IBKR row, the `[[fa.entities]]` `name` when set, else the statement's security description — for an ADR, typically includes "ADR" or "AMERICAN DEPOSITARY" |

A user (or auditor) who wants to apply the underlying-issuer
interpretation can identify the rows by `Source ticker` (the
recognisable ADR ticker like `INFY` / `HDB`) and leave those rows
out of Table A3 (path 1). The A3 country dropdown has no India, so
an Indian-company ADR can't be re-attributed there.

## Related

- Code reference: see
  `FinancialInstrument.isin` in
  `src/ibkr_fa/parser/activity_statement/trades.py` for the
  attribution rule, and the docstring there for the design
  rationale.
- Issue: [#148](https://github.com/jay-tau/ibkr-fa/issues/148).
- Surfaced by: PR #146 Tier-1 r1 correctness P3-4 (the country
  attribution is more visible after #143 IN-prefix rejection plus
  #146 ISIN normalisation).
