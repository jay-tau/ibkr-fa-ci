# Schedule FA automation and manual-fill matrix

This page records what each supported source can prove for Schedule FA.
Missing evidence is never estimated.

| Symbol | Meaning |
|---|---|
| ✅ | Computed from complete source evidence |
| ⚙️ | Requires validated configuration or an explicit user selection |
| 🟡 | Manual-fill; command exits 3 |
| ⛔ | Source contradiction or unsupported case; command exits 4 |
| — | Not applicable to the supported source |

## Table A2 — foreign custodial account

| Source | Account identity | Peak balance | Closing balance | Gross credits by nature |
|---|---|---|---|---|
| IBKR Activity Statement | ⚙️ `[account.custodian]`; absent means no A2: exit 3 for review output, ⛔ for CBDT JSON. A blank `zip` leaves the ZIP Code 🟡 (#1358) | 🟡 Complete daily account NAV is absent; #352, #377, #379 | ✅ USD NAV when present and cross-checked; otherwise 🟡 | ✅ One row per proven nature; unresolved evidence adds a blank placeholder |
| IBKR Flex Query | ⚙️ `[account.custodian]`; a blank `zip` leaves the ZIP Code 🟡 (#1358) | 🟡 #352, #377, #379 | 🟡 NAV section is not parsed | 🟡 Complete account credits are unavailable |
| Fidelity NetBenefits | ⚙️ Account configuration and participant number; without `[netbenefits.account].zip` the ZIP Code is 🟡 (#1358) | 🟡 Daily account cash/NAV is unavailable; #376 | ✅ When Dec-31 securities and zero cash are proved; otherwise ⛔ | ✅ `No Amount paid/credited` for the currently supported income-free account window |
| Morgan Stanley StockPlan | — A2 is not emitted | 🟡 | 🟡 | 🟡 Entire A2 awaits account cash/NAV evidence in #375 |
| Fidelity CMA | ✅ Statement/config identity | ⚙️ Select the computed lower or upper posting-order bound; no selection means exit 3, in the `filing-csv` upload too, where the blank peak is named for manual entry | ✅ | ✅ One row per proven nature |
| Robinhood | ✅ Statement/config identity; a blank `[[robinhood.accounts]] zip` leaves the ZIP Code 🟡 (#1358) | ✅ From synchronized daily account value; there is no manual peak, and a config with `[[robinhood.a2_peak_overrides]]` is refused (#628) | ✅ | ✅ One row per proven nature. Sale proceeds are each in-CY equity sale's activity `Amount` and option sell-to-close's cash (#1383). ⛔ Any option blocker, or any Schedule OS blocker in the source income (the only way a nature stays unresolved; `a3-credit-attribution`), refuses the whole Schedule FA leg, so no A2 or A3 file is written |

Every A2 nature row repeats the same account identity, peak, and closing
balance. Repeated rows are one account, not additional assets.

## Table A3 — individual equity/debt interest

Whitespace-only names remain subject to the row checks and the filing CSV's
ASCII check.

| Source | Entity identity | Peak value | Closing value | Gross paid/credited | Gross sale/redemption proceeds |
|---|---|---|---|---|---|
| IBKR Activity Statement | ⚙️ Verified `[[fa.entities]]`; a missing address, ZIP or nature, or a whitespace-only security description without a configured `name`, remains 🟡 (#1289, #1369). Country: the ISIN prefix | ✅ With complete price evidence; otherwise 🟡 | ✅ | ✅ With complete CY distribution evidence; otherwise 🟡 | ✅ Pre-fee quantity x execution price |
| IBKR Flex Query | ⚙️ Verified `[[fa.entities]]`; a missing address, ZIP or nature, or a whitespace-only security description without a configured `name`, remains 🟡 (#1289, #1369). Country: `IssuerCountryCode`; ⛔ when it differs from the ISIN prefix, an unresolved attribution (#794) | ✅ With complete price evidence; otherwise 🟡 | ✅ | 🟡 No complete Dividends section | 🟡 Only net cash is supplied; net is never filed as gross |
| Fidelity NetBenefits | ⚙️ Security/entity configuration; without `[netbenefits.security].entity_zip` the A3 ZIP Code is 🟡 in the upload, and in the Quicko workbook when written (#1330) | ✅ For the supported close basis; high basis needs a high on every US market session | ✅ | ✅ Proven zero for the currently supported Dec-31 acquisition window | ✅ Open lots are zero; intra-CY disposals are currently ⛔ |
| Morgan Stanley StockPlan | ⚙️ Security/entity configuration | ✅ With complete prices; otherwise 🟡 | ✅ | ✅ From the full dividend report | — Any sale currently causes ⛔ rather than partial FA output |
| Fidelity CMA | ✅ Verified fund identity | ✅ Reconstructed SPAXX value; 🟡 when only the redemption day could set the peak, including same-day lots; `peak_basis = "high"` is ⛔ | ✅ | ✅ | ✅ Pre-fee quantity x sale price |
| Robinhood | ✅ When GLEIF resolves, or the SEC fallback does after a GLEIF zero-match or a valid lapsed record; otherwise ⚙️ `[[fa.entities]]` or 🟡. ⛔ A corrupt GLEIF cache artifact or a failed GLEIF fetch, on the exact-ISIN, ADR depositary or ETF registrant lookup, or a corrupt or unreadable SEC Form F-6 or N-CEN selection cache artifact, or an unusable entry in the post-FY SEC identity snapshot search; a valid but unusable registrant record leaves its route unresolved. ETF: ✅ all four cells only when its N-CEN registrant and SEC fund nature resolve for the same class, otherwise 🟡; with an `[[fa.entities]]` entry, ⚙️ only, and a cell it leaves out, including the name, is 🟡, except the address: an entry without `address` is ⛔ (#1449). A whitespace-only activity-report issuer name that no configured or automatic name replaces is 🟡 (`a3-identity`, #1369) | ✅ With complete synchronized prices | ✅ | ✅ When the lots take every credit and the source income has no Schedule OS blocker; otherwise ⛔ (`a3-credit-attribution`, no A2 or A3 file) | ✅ Activity `Amount`, filed as gross by owner decision (#1383) |

All computed Schedule FA gross credits and proceeds use the December 31 SBI
TTBR. Peak values select the native-currency maximum first, then apply the
selected date's TTBR; the warned INR-max override remains available. For
every source, a fetched USD price bar dated on a weekend or a listed US market
closure, inside a holding's valued window or carried into it, is ⛔ (#895).

## Command completion status

| Command | Exit 0 | Exit 3 | Exit 4 |
|---|---|---|---|
| `schedule-fa` | A2 and A3 are complete, or complete CBDT JSON was emitted | Any blank A2/A3 field, or missing A2 in review output; CBDT JSON missing a filed value | Invalid/contradictory source evidence; CBDT JSON with a structural problem (unsupported ITR form or AY, missing or unverified A2 account, contradictory A2 rows) or a value the CBDT schema rejects |
| `netbenefits-fa` | Complete CBDT JSON with an audited A2 peak override | Review output while A2 peak or another field remains manual; CBDT JSON missing a filed value | Unsupported or contradictory bundle; CBDT JSON with a structural problem (e.g. an A2 identifier other than the participant number) or a value the CBDT schema rejects |
| `fidelity-cma-fa` | A2 peak bound selected and no unresolved field remains, in review output and the `filing-csv` upload alike | Peak selection or another filed field remains manual, in either | Unsupported or contradictory bundle |
| `robinhood-fa` | All account/holding evidence resolves | Explicit manual work remains, including a blank `[[robinhood.accounts]] zip` (`a2-zip`), a whitespace-only Table A3 Name of entity (`a3-identity`; set `name` in the ISIN's `[[fa.entities]]` entry) or a blank Table A3 peak (`a3-peak`, once per reason, with the A3 rows it blanks, its cause and remedy; #1476, #1560), or requested CBDT JSON misses a filed value | A blocker prevents a safe result; a held security's `[[fa.entities]]` entry has no `address` (the address found is in the private `robinhood-identity-guidance.toml` to verify, and no Schedule FA file is written); requested CBDT JSON with a structural problem, a value the CBDT schema rejects, or a `schedule-fa.json` ibkr-fa didn't write in the output directory |
| `stockplan-fa` | Not currently possible because A2 is not generated | Every successful run until #375 supplies A2 evidence | Unsupported or contradictory bundle |

## Evidence-blocked work

- Generic IBKR A2 peak: #352, #377, #379.
- NetBenefits A2 peak: #376.
- StockPlan A2: #375.
- A3 distributions without complete source evidence: #15.
- Security-specific identity gaps remain in their individual research issues.
- Missing or ambiguous prices, FX, account identity, or transaction evidence
  remain fail-closed rather than becoming estimates.
