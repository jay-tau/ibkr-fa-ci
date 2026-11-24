# ADR 0017: Complete Robinhood report assignment evidence

- **Status**: Accepted
- **Date**: 2026-08-29
- **Deciders**: jay-tau
- **Issue**: #555
- **Amends**: ADR-0006 section 3

## Context

ADR-0006 originally assigned activity reports using a reduced activity probe,
then checked account family, configured opening date, attested report range,
and complete statement boundaries afterward. Two reduced probes could appear
ambiguous even when those remaining facts admitted only one complete
assignment.

## Decision

Evaluate both complete report/bundle bijections. For each candidate, validate
account family and range, bound both statement bundles, then reconcile both
activity legs against their bounded counterpart. Exactly one complete
candidate must survive. Report-independent opening-layout facts are validated
before candidate evaluation.

The selected bounded bundles and activity reconciliations are reused for cash
and transfer processing. Reduced windowed probes are removed.

## Consequences

- Complete evidence can disambiguate otherwise identical activity.
- Genuine ambiguity and zero-candidate cases remain fail-closed.
- Candidate failures retain privacy-safe report/bundle indices and original
  reason text.
- Cash evidence never breaks an identity tie.
