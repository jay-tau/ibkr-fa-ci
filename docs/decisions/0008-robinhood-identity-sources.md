# ADR 0008: Account-free Robinhood security identity evidence

- **Status**: Accepted
- **Date**: 2026-08-17
- **Issue**: #491
- **Superseded in part by**: ADR-0009 for A3 legal addresses
- **Amended**: 2026-11-03 (#853) to stop the run on a corrupted snapshot;
  2026-11-09 (#1082) to require every observed broker name to agree;
  2026-11-11 (#1187) to read statement symbols through accepted CUSIP
  transitions under the guidance's symbol rule

## Context

Robinhood statements and activity CSVs bind ticker to CUSIP, but do not supply
the complete filing identity used by the shared A3/CG models. Requiring a
manually researched registry for every holding also prevents automatic A2
pricing because the canonical ticker remains unresolved.

The user prefers official, primary, account-free sources. Identity lookups must
also be reproducible: unchanged broker identity evidence must not silently
change because an upstream directory updates.

## Decision

Use only official structured sources for automatic identity generation:

- Nasdaq Trader's traded-symbol directory for listing, security name and ETF
  status;
- SEC ticker/exchange and fund-class reference files, followed by the required
  per-CIK submissions JSON for issuer and domestic filing evidence.

No account, API key, Massive, Yahoo, OpenFIGI or GLEIF identity dependency is
introduced. SEC's declared User-Agent is fair-access identification, not
authentication.

Cache one immutable normalized source artifact under the existing private cache
root, keyed by the post-selection Robinhood identity fingerprint:

```text
sorted(CUSIP, observed symbol, observed description)
+ relevant manual identity fields
+ resolver schema version
```

A cache hit performs no network request. Changed identity evidence selects a new
artifact. Corrupt or hash-less artifacts are refused rather than overwritten.
Cold fetches disclose the matched public CIKs to the SEC; no account identifier,
CUSIP, quantity or value is sent.

> **Amendment (2026-11-11, #1187):** The observed symbol comes from the same
> evidence and rule as the private `user.toml` guidance. Statement activity
> symbols are grouped by canonical CUSIP: a row that an accepted
> `cusip_transitions_file` transition covers (ADR 0006) counts under its
> activity CUSIP, not its printed one. One statement symbol is the observed
> symbol. With no statement symbol, one activity-report symbol is. Two or more
> statement symbols leave the CUSIP without a symbol (`broker-symbol-ambiguous`)
> whatever the reports say: report evidence never settles a statement
> conflict, so such a CUSIP, with or without a transition, needs a reviewed
> `[[robinhood.securities]]` entry. A CUSIP that an accepted transition
> replaces on a statement row names no symbol either: if activity still
> uses it, the history holds both CUSIPs of the transition, which ADR 0006
> leaves unsupported, so it also needs a reviewed entry. Only transitions
> the existing validator accepted are used. Nothing infers a transition, rewrites a stale position
> identifier or relaxes the name, exchange or jurisdiction checks. A changed
> observed symbol changes the fingerprint, so it selects a new snapshot under
> the rules above.

Schema v2 also retains the optional structured SEC `addresses.business` fields.
Missing, null, or malformed address members are stored as empty evidence rather
than invalidating unrelated identity records. A fetch-disabled run may reuse a
verified schema-v1 artifact; it receives the previous behavior with no automatic
address instead of failing solely because the presentation schema changed. ADR
0009 now permits schema-v2 business addresses only as a strictly checked
fallback after exact GLEIF legal-address evidence fails.

For a filing FY, any verified schema-v2 snapshot fetched after March 31 is
final for that FY and needs no refresh. Presentation resolution may reuse the
newest such snapshot across older input fingerprints. Common-stock and fund
coverage both degrade per security, so one delisted/unavailable symbol or
non-1940-Act ETP cannot suppress unrelated SEC or fund evidence. Every
selectable candidate artifact is integrity-checked first. Pre-cutoff,
legacy-schema, or corrupted snapshots do not qualify.

> **Amendment (2026-11-03, #853):** A corrupted snapshot now stops the run
> (exit 4) instead of only failing to qualify, whichever securities it covers:
> one that can't be read or parsed, fails its integrity check, has a schema
> other than 1 or 2, or fails a record check. Every cached artifact is checked,
> not only the selectable candidates. A valid pre-cutoff or legacy-schema
> snapshot still doesn't qualify.

A covering pre-cutoff artifact is immutable and is never overwritten; the
audit names it and directs the user to a new cache directory for a post-FY
fetch.

Automatic resolution is deliberately narrow:

- domestic US common stock with Robinhood/Nasdaq/SEC name agreement, exact
  exchange agreement and domestic 10-K evidence;
- US ETFs with Robinhood/Nasdaq name agreement and agreeing Nasdaq ETF and
  SEC fund-class evidence.

Name agreement covers every broker issuer name observed for the CUSIP. Each
must pass the existing either-direction normalized-name check against Nasdaq.
After noise words such as `INC` and `CLASS`, and `CO`, are removed, the broker
name must be nonempty and every remaining word must be in the normalized
Nasdaq name. Nasdaq may have extra words. For a common stock, each broker
name must also agree with the SEC name under the either-direction rule.
One agreeing name never outvotes a conflicting one, so a CUSIP whose old and
new activity names do not all agree needs a reviewed
`[[robinhood.securities]]` entry (#1082).

ADR/ADS, foreign ordinary shares, OTC/delisted or reused tickers, units,
warrants, rights, preferreds, notes and ambiguous records remain typed blockers.
The resolver creates the existing `RobinhoodSecurityConfig`; there is no
A2-specific identity shortcut.

Generated identities are merged in memory and never silently written to
`user.toml`. Manual entries remain supported. The merged set must have unique
CUSIP, ISIN and canonical symbol values.

ADR 0009 separately governs exact-ISIN GLEIF legal-address evidence. GLEIF is
never security-identity authority in this module.

## Consequences

- Standard identities and A2 equity pricing can run automatically without an
  account at a market-data provider.
- Unchanged runs are offline and idempotent once identity and price caches exist.
- SEC/Nasdaq transport or schema failures become identity blockers and do not
  suppress the independent options leg.
- Remaining A3 identity work is grouped once per security while retaining every
  affected lot reference.
- SEC state-of-incorporation plus current 10-K evidence can supply
  `Corporation` under ADR 0010.
- #535 was closed as unnecessary for FY 2025-26 because every current snapshot
  was fetched after the user-selected March 31 cutoff.
- Exceptional securities still require reviewed row-specific config evidence.
