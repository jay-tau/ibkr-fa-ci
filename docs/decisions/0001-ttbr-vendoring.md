# ADR 0001: Vendor SBI TTBR CSV (don't use a git submodule)

- **Status**: Accepted
- **Date**: 2026-05-04
- **Deciders**: jay-tau

## Context

`ibkr-fa` needs daily SBI Telegraphic Transfer Buying (TTBR) rates to
convert USD-denominated foreign-asset values into INR for Schedule FA.
SBI publishes the rates daily as PDFs but does not provide a historical
archive or an API. The community-maintained
[`sahilgupta/sbi-fx-ratekeeper`](https://github.com/sahilgupta/sbi-fx-ratekeeper)
repository (MIT-licensed) downloads those PDFs and emits one CSV per
currency, plus the original PDFs for audit, since January 2020.

We need to make those rates available to the package so that:

1. The package works offline — no network call required at conversion time.
2. The data is reproducible from a recorded upstream commit SHA.
3. Users get refreshed rates without manual intervention.
4. Schedule-FA correctness is preserved when SBI publishes intraday rate
   updates (multiple rows per calendar date in the upstream — see the
   Codex P1 finding on PR #73).

There are three plausible approaches:

1. **Vendor a snapshot CSV** in `src/ibkr_fa/reference/`, with a sibling
   `.source` file recording the upstream commit SHA and a refresh script.
2. **Add `sahilgupta/sbi-fx-ratekeeper` as a git submodule** under
   `vendor/sbi-fx-ratekeeper/` (or similar) and read the CSV directly
   from there.
3. **Fetch over the network at runtime** (e.g., from raw.githubusercontent.com
   on first use, with on-disk caching).

## Decision

**Option 1 — vendor a snapshot CSV.**

The bundled file lives at
`src/ibkr_fa/reference/sbi_ttbr_usd_inr.csv` (data) and
`src/ibkr_fa/reference/sbi_ttbr_usd_inr.csv.source` (provenance:
upstream repo, license, path, URL, commit SHA, fetch timestamp,
documented vendor-time transformations).

`scripts/refresh_ttbr.py` refreshes the snapshot, applying the same
vendor-time transformations described in the `.source` file. The
`ttbr-refresh` GitHub Actions workflow is scheduled to run it every
Sunday at 06:00 UTC and can also be run by hand. When the refresh
changes the USD CSV, the workflow runs the ruff, ty and pytest gates
and, if they pass, opens or updates a refresh PR. New rates reach
users only after that PR is reviewed and merged. *(Script added
2026-05-05 by #89; workflow 2026-05-31 by #180, closing #55.)*

We do **not** use a git submodule. We do **not** fetch at runtime.

## Consequences

### Positive

- **Self-contained installs.** `pip install ibkr-fa` (or `uv tool install`)
  ships the CSV in the wheel; no network or submodule init needed.
- **Reproducible transformations.** We dedup intraday SBI publications
  (keep last per date) and sort ascending at vendor time. The `.source`
  file documents the transformations so anyone can reproduce the
  bundled CSV from `upstream-commit + transformation rules`. A submodule
  would expose raw upstream rows and require duplicating the
  transformation in the loader, complicating both correctness and tests.
- **`csv.DictReader` works directly** on the bundled file (Codex P2 from
  PR #75 — header must be the first record, comments live in the sibling
  `.source` file).
- **Pre-commit `forbid-submodules` stays on.** This hook prevents
  accidental submodule additions; we'd weaken repo-level safety to
  permit one exception.
- **Refresh UX is a normal `git pull`.** Users get new rates by pulling
  `main`, not by remembering `git submodule update --remote`. CI can
  open a refresh PR weekly without anyone having to recurse-init
  submodules locally.
- **Wheel size stays small.** The bundled CSV is ~236 KB. The full
  `sahilgupta/sbi-fx-ratekeeper` repo carries thousands of `pdf_files/`
  (one per SBI publication day) plus 30+ per-currency CSVs we don't use
  in v1 — many tens of MB. A submodule would clone the whole thing.

### Negative / accepted trade-offs

- **The vendored CSV can go stale.** Mitigated by the weekly
  `ttbr-refresh` workflow; rates reach users only after its PR is merged. The
  `FaExactDatePolicy` already raises `MissingFxRateError` (with a 14-day
  staleness ceiling by default) when the archive is too old to cover a
  requested date, so stale data fails loud, not silent.
- **Multi-currency requires multiple vendored files.** Tracked under
  #52 (Tracking: FX & multi-currency support). The loader's filename
  pattern `sbi_ttbr_<currency_lower>_inr.csv` means adding EUR / GBP / etc.
  is a vendor + parser-currency-arg change, not an architectural
  rework.
- **The package doesn't pick up upstream fixes automatically.** We need
  to refresh deliberately. This is also a feature: a Codex- or
  CA-signed-off snapshot is more trustworthy for tax filings than
  whatever HEAD upstream happens to be at runtime. Filings produced
  from a tagged release are reproducible.

### Neutral

- **Attribution.** `THIRD_PARTY_NOTICES.md` includes the upstream MIT
  license text and a link to `.source` for SHA-level traceability.
  This is the same accountability we'd have with a submodule, just
  expressed differently.

## Related

- PR #73 — original vendor of the CSV; fixes from Codex P1 (vendor-time dedup) and P2 (zero-rate string variants).
- Tracking issue [#52](https://github.com/jay-tau/ibkr-fa/issues/52) — `area:fx`, FX & multi-currency support, including `scripts/refresh_ttbr.py` and the scheduled refresh GH Actions job.
- `src/ibkr_fa/reference/sbi_ttbr_usd_inr.csv.source` — provenance file with upstream SHA + documented transformations.
