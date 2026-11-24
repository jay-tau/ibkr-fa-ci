# Architecture Decision Records (ADRs)

This directory contains decisions that shaped `ibkr-fa`. Each ADR captures
*why* we chose one approach over the alternatives, so future contributors
(and our future selves) don't have to re-litigate the same trade-offs.

## Format

We use a lightweight [Michael Nygard ADR][nygard] format — four sections:

- **Status**: `Accepted` / `Superseded by ADR-NNNN` / `Deprecated` / `Proposed`.
- **Context**: the situation we faced.
- **Decision**: what we decided.
- **Consequences**: what follows from the decision (good and bad).

[nygard]: https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions

## When to write one

Write an ADR when a decision:

- has multiple defensible alternatives,
- will be hard or expensive to reverse later, or
- is likely to be questioned by a future reader.

Skip ADRs for routine choices the linter / CI / typechecker would already
catch.

## Numbering and filenames

- Four-digit zero-padded sequential ID: `0001`, `0002`, …
- Filename: `NNNN-short-kebab-title.md` (e.g., `0001-ttbr-vendoring.md`).
- Numbers are never reused. If a decision is superseded, the new ADR
  references the old one in its `Status`, and the old one is updated to
  `Superseded by ADR-NNNN`.

## Index

| ID | Status | Title |
|---|---|---|
| [0001](0001-ttbr-vendoring.md) | Accepted | Vendor SBI TTBR CSV (don't use a git submodule) |
| [0002](0002-cbdt-json-schedule-fa-fragment.md) | Accepted; dependency superseded by ADR-0016; merge narrowed to strict merge-once | CBDT Schedule FA JSON fragment — design decisions |
| [0003](0003-fidelity-netbenefits-espp.md) | Accepted | Fidelity NetBenefits (Microsoft ESPP) data model |
| [0004](0004-fidelity-cma-classification.md) | Accepted | Fidelity Cash Management Account source classification |
| [0005](0005-robinhood-classification.md) | Superseded by ADR-0006 | Robinhood brokerage source classification |
| [0006](0006-robinhood-multi-account-evidence.md) | Accepted; amended by ADR-0012, ADR-0013, ADR-0014, ADR-0015, ADR-0017 | Robinhood multi-account evidence and lot reconstruction |
| [0007](0007-massive-option-closes.md) | Accepted | Massive historical option trade closes |
| [0008](0008-robinhood-identity-sources.md) | Accepted; superseded in part by ADR-0009 | Account-free Robinhood security identity evidence |
| [0009](0009-robinhood-legal-addresses.md) | Accepted | Exact-ISIN legal addresses for Robinhood A3 |
| [0010](0010-robinhood-legal-nature.md) | Accepted | Evidence-backed legal nature for Robinhood A3 |
| [0011](0011-robinhood-etf-registrants.md) | Accepted | Period-bound registrant identity for Robinhood series ETFs |
| [0012](0012-robinhood-cash-sweep-a2.md) | Accepted | Robinhood cash sweep remains in A2 |
| [0013](0013-robinhood-crypto-out-of-scope.md) | Accepted; amended by ADR-0015 | Robinhood Crypto is out of scope |
| [0014](0014-schedule-fa-a2-nature-rows.md) | Accepted | Repeat Robinhood A2 rows by credit nature |
| [0015](0015-robinhood-spending-out-of-scope.md) | Accepted | Robinhood Spending is out of scope |
| [0016](0016-mandatory-cbdt-json-validation.md) | Accepted | Mandatory CBDT JSON validation |
| [0017](0017-robinhood-complete-report-assignment.md) | Accepted | Complete Robinhood report assignment evidence |
| [0018](0018-ibkr-a3-entity-evidence.md) | Accepted | Source-neutral entity evidence for IBKR A3 rows |
| [0019](0019-quicko-deductor-name.md) | Accepted (2026-11-04) | Whose name fills Quicko's "Name of the deductor" |
| [0020](0020-quicko-purchase-value.md) | Accepted (2026-11-04) | Basis for Quicko Purchase Value on ESPP holdings |
| [0021](0021-netbenefits-a2-institution.md) | Accepted (2026-11-04) | Which firm Schedule FA A2 names for the NetBenefits account |
| [0022](0022-output-bindings.md) | Accepted (2026-11-11) | Output bindings separate public formats from writer classes |
| [0023](0023-command-formats-and-selections.md) | Accepted (2026-11-11) | Command formats and selections come from the output declaration |

> Update this index in the same PR that adds or supersedes an ADR.
