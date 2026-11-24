# ADR 0015: Robinhood Spending is out of scope

- **Status**: Accepted
- **Date**: 2026-08-26
- **Amends**: ADR-0006, ADR-0013

## Context

Robinhood Spending/Cash Card is a separate prepaid-card product, not brokerage
cash, ACH activity, or the Deposit Sweep. The taxpayer does not have a
Robinhood Spending account and does not want that product supported.

## Decision

Robinhood Spending is not part of `ibkr-fa`:

- add no Spending parser, tax computation, or foreign-asset output;
- do not infer anything about Spending from brokerage reports;
- do not let Spending scope block supported brokerage schedules.

Together with ADR-0013, this removes the obsolete Crypto/Spending scope
attestation configuration and makes the `product-scope` run leg not required.

## Consequences

- A successful Robinhood run covers only the documented brokerage surfaces.
- Adding Spending later requires a new decision and explicit user request.
