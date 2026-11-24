# ADR 0012: Robinhood cash sweep remains in A2

- **Status**: Accepted
- **Date**: 2026-08-26
- **Amends**: ADR-0006

## Context

Robinhood opens Program Bank deposit accounts as agent, but the taxpayer's
customer-facing accounts are the Robinhood custodial accounts already disclosed
in Schedule FA A2. Program Bank account numbers and bank-issued statements are
not available. No CBDT authority addresses omnibus broker cash sweeps, and a
four-provider review split between A1+A2 and A2-only.

## Decision

Use the taxpayer's reviewed A2-only filing position:

- emit one A2 row per Robinhood account and no A1 row;
- include brokerage cash plus aggregate Deposit Sweep cash in A2 peak and
  closing balances;
- treat transfers between brokerage cash and the sweep as internal movements
  that do not change A2 value;
- include both brokerage and Program Bank interest in A2 gross-credit evidence;
- retain the parsed per-bank balances and interest classification as audit
  evidence, not separate filing rows.

No Program Bank account number, opening date, allocation, or A1 row is inferred.

## Consequences

- Program Bank presence no longer creates manual A1 work or suppresses the
  Schedule FA fragment by itself.
- Exact statement reconciliation, daily A2 valuation, interest reconciliation,
  and all unrelated A2/A3/option/product-scope gates remain unchanged.
- This decision is specific to the supplied Robinhood custodial relationship;
  it does not classify another broker's bank-sweep product.
