# ADR 0014: Repeat Robinhood A2 rows by credit nature

- **Status**: Accepted
- **Date**: 2026-08-26
- **Amends**: ADR-0006

## Context

Each Schedule FA A2 row has one `NatureOfAmount` and one
`GrossAmtPaidCredited`, while one custodial account can receive interest,
dividends, sale proceeds, and other income in the same calendar year.
Collapsing those categories loses filing information.

The official AY 2026-27 ITR-2 and ITR-3 schemas define A2 as an array without
`uniqueItems`. The official Excel utilities provide `AddRows_A2_FA`, expand the
A2 table when JSON import contains more rows than the six visible defaults, and
validate every populated row independently. They do not reject repeated account
numbers.

## Decision

For Robinhood, emit one complete A2 row per
`(account number, nature of amount)`:

- repeat the institution, account, opening date, peak, and closing fields;
- aggregate each nature's exact calendar-year credits into its own amount;
- emit `No Amount paid/credited` only when the account has no credits;
- never collapse multiple natures into `Other income`;
- retain a manual placeholder and suppress filing JSON when any nature's total
  remains unresolved.

The JSON merger treats `(AccountNumber, NatureOfAmount)` as the fragment row
identity. It still refuses to merge into a target that already contains the
same account, preserving the clean-export/merge-once contract.

## Consequences

- Multiple exact credit natures no longer create manual A2 work.
- Repeated peak and closing balances are the official utility's required row
  shape; they are not separate economic accounts and must not be added together.
- Each in-CY Robinhood option sell-to-close's cash, at the December 31 TTBR,
  adds to the account's sale-proceeds row with its equity sale `Amount`s (owner
  decision 2026-11-14, #1383; ADR 0006 section 7). An option blocker refuses
  the whole Schedule FA leg rather than leaving proceeds for manual entry.
  *(Changed 2026-11-14 by #1384; option proceeds were manual before.)*
