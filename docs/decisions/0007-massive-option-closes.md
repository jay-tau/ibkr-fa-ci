# ADR 0007: Massive historical option trade closes

- **Status**: Accepted
- **Date**: 2026-08-16
- **Issue**: #487
- **Amended**: 2026-11-11 (#1169) to check response formats before conversion
  and to state `robinhood-all`'s all-or-nothing cleanup

## Context

Robinhood activity and statements prove supported long-Call identity, quantity,
cash, and monthly valuation anchors, but do not contain daily option prices.
Schedule FA A2 cannot yet reconstruct a synchronized daily account series while
option values are missing.

The user requires a free, direct HTTPS API and the actual daily qualifying-trade
close only. OPRA/Cboe is the primary consolidated market source but its
historical products are paid. ThetaData requires a local terminal. Massive
(formerly Polygon.io) Options Basic exposes expired contract metadata and a
rolling two-year daily aggregate history through REST.

Massive confirmed that personal, non-commercial CLI use is permitted provided
ibkr-fa does not expose or redistribute a market-data API. Each user supplies
their own key; market data and caches remain local and private.

## Decision

Use two Massive REST endpoints:

1. Contract Overview with `as_of=<expiration>` to verify the exact standard
   contract, multiplier 100, and absence of adjusted deliverables.
2. Custom Bars with `1/day`, `adjusted=false`, and ascending order to retrieve
   trade-derived daily closes.

No bar on an expected option session is missing evidence, never zero. Prior
closes carry only across known weekends/market holidays. No NBBO midpoint,
intrinsic/model value, stale-session carry, second provider, or silent fallback
is permitted.

The API-key path is stored in `[prices].massive_api_key_file`; the key remains
in a separate private file and never enters TOML, logs, errors, URLs, or caches.
Relative paths resolve beside `user.toml`; absolute paths are supported.

Raw Massive responses are stored in one immutable, provider-specific private
JSON artifact with a canonical SHA-256. A valid cache is read before the key and
network, allowing reproduction after the free rolling window expires. Corrupt
caches are refused rather than overwritten/refetched. The hash proves local
immutability, not that Massive will never publish later corrections.

Issue #490 consumes this cache in the synchronized daily A2 account valuation.
Missing, invalid, identity-mismatched, or incomplete option-close evidence now
blocks the Schedule FA leg. `robinhood-all` still runs and reports its other
legs, but, as for any failed leg, it then attempts to remove every file it
wrote and exits `4`, so the run leaves nothing to file.

Fetched responses and cached artifacts pass the same checks before any value
is converted (#1169):

- The Contract Overview's `status` must be exactly `OK`, checked before the
  contract identity.
- `strike_price`, `shares_per_contract`, and each bar's close `c` and volume
  `v` must be finite native JSON numbers. Numeric strings and booleans are
  refused, never parsed.
- Each bar's transaction count `n` and millisecond timestamp `t` must also be
  exactly integral, never truncated, and the timestamp must resolve to a
  representable New York date.
- `correction` may be absent, null, or finite numeric zero, never a boolean.

The field checks above name the response and field in fixed text, never a
value. Aggregate field checks also name the 1-based `results` row; a non-object
row refusal names the row alone. The identity, adjusted-contract,
positivity, duplicate-day, coverage and pagination refusals are unchanged. A
refused fetch writes no artifact; a refused artifact is left unchanged and is
never refetched. `scripts/preflight_massive_option_coverage.py` keeps a
separate diagnostic parser with looser coercions; it is not the filing reader
and does not validate these checks.

## Consequences

- CY 2025 option closes can be privately acquired and reproduced without a paid
  data tier or local daemon.
- Cold-cache runs are slow because Options Basic allows five requests/minute.
- Illiquid no-trade sessions remain blockers under the user's actual-close-only
  policy.
- Adjusted/non-standard contracts remain unsupported.
- The cache must never be committed or redistributed.
- Massive failures block Schedule FA and remove its partial files.
  `robinhood-all` then attempts to remove every file the run wrote, including
  other legs' outputs.
- `--no-fetch-prices` reads the same immutable cache without network access and
  refuses when a required artifact is absent.
