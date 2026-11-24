# ADR 0013: Robinhood Crypto is out of scope

- **Status**: Accepted
- **Date**: 2026-08-26
- **Amends**: ADR-0006
- **Amended by**: ADR-0015

## Context

Robinhood brokerage reports explicitly exclude Robinhood Crypto, and supplied
Crypto PDFs use a separate statement family. Supporting that product would
require separate asset, transfer, tax, and Schedule FA policy.

## Decision

Robinhood Crypto is not part of `ibkr-fa`:

- discover and count Crypto PDFs so they are never mistaken for brokerage
  statements or silently lost;
- emit an explicit out-of-scope warning;
- do not parse Crypto holdings or activity and do not generate Crypto tax or
  foreign-asset output;
- do not let Crypto statements or attestation status block supported brokerage
  schedules.

Statement-only `COIN` rows remain brokerage cash evidence because they affect
the supported Robinhood custodial account. ADR-0015 separately excludes
Robinhood Spending.

## Consequences

- A successful Robinhood run does not claim Crypto coverage.
- Adding Crypto later requires a new decision and explicit user request.
