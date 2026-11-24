# ADR 0016: Mandatory CBDT JSON validation

- **Status**: Accepted
- **Date**: 2026-08-29
- **Deciders**: jay-tau
- **Issues**: #552, #564
- **Supersedes**: ADR-0002 decision 6

## Context

`robinhood-all` automatically emits a CBDT Schedule FA JSON fragment when its
filing data is complete. Keeping `jsonschema` optional made that normal command
depend on an undeclared runtime capability. Automatic JSON generation also
shared the requested CSV/XLSX rollback boundary, so its failure could delete
valid requested output.

## Decision

`jsonschema` is a mandatory runtime dependency. `robinhood-all` writes the
automatic JSON only when it is eligible, and skips it without a notice when it
isn't. It skips it with a warning when `schedule-fa.json` is a file ibkr-fa
didn't write, or when the AY isn't the vendored schema's. An attempted write
that fails, or that finds required filing values incomplete, makes the FA leg
an error. The failed run then removes every output it wrote, the requested
CSV/XLSX included.

*Amended 2026-11-15:* until the owner's decision of 2026-11-01 on #719
(PR #725), a failed automatic JSON kept the requested output.

## Consequences

- Every installation can validate emitted JSON against the vendored schema.
- The retired `cbdt-json` extra has no compatibility alias.
- A failed automatic JSON attempt fails the run, which removes the requested
  FA output too (#719).
- Direct `cbdt-json-fragment` commands remain fail-closed.
