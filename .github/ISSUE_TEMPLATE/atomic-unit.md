---
name: Atomic-unit issue
about: One PR-sized change. Title = the conventional-commit subject the eventual PR will use.
title: ''
labels: []
assignees: []
---

<!--
  Use this template for one PR-sized child issue.
  - Title MUST be the conventional-commit subject (see CONTRIBUTING.md).
    Example: feat(compute): peak value (daily prices x daily TTBR x daily qty)
  - Add an `area:*` label so it's grouped with siblings.
  - Link to the parent tracking issue via the Sub-issues panel.
-->

## Parent

Parent tracking issue: #

## What this change does

<!-- One paragraph. The PR description will largely match this. -->

## Acceptance criteria

<!--
  Concrete, testable conditions. Examples:
  - Function `peak_value_inr(lot, prices, fx_policy, cy)` returns Decimal.
  - Behaviour: max over each calendar day d in [max(acquired, Jan 1), min(closed-1, Dec 31)] of close[d] * qty[d] * TTBR(d).
  - Daily prices source: yfinance via `prices.get_daily_closes`.
  - Tests cover: still-open lot, opened-and-closed-in-CY lot, weekend acquisition, missing price fallback.
  - Coverage in `compute/fa_a3/peak_value.py` ≥ 90 %.
-->

- [ ]
- [ ]
- [ ]

## Files touched (estimated)

<!--
  Helps reviewers understand scope. Example:
  - `src/ibkr_fa/compute/fa_a3/peak_value.py` (new)
  - `tests/test_compute_peak_value.py` (new)
  - `src/ibkr_fa/compute/fa_a3/__init__.py` (extend row-builder to fill peak_value_inr)
-->

## Dependencies

<!-- Other atomic-unit issues that must close before this one is workable. -->

## Notes

<!-- Anything else worth recording: research links, edge cases to remember, follow-ups. -->
