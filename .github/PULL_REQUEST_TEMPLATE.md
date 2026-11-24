<!--
  PR title MUST match the squash-commit subject.
  Format: <type>(<scope>): <subject>  — see CONTRIBUTING.md.
  Example: feat(parser): parse Open Positions LOT section with hard input validation
-->

## Summary

<!-- One paragraph: what changes and why. Reference the user-visible effect. -->

## Linked issue

<!-- Closes #NN  (or  Refs #NN  if this is a partial step toward an issue). -->
Closes #

## How I tested it

<!--
  Concrete commands and their outcomes. Examples:
  - `uv run pytest tests/test_parser_open_positions.py -v` → 12 passed
  - `uv run ty check src/ibkr_fa/parser/open_positions.py` → 0 errors
-->

## Checklist

- [ ] Conventional-commit subject as PR title (will become the squash-commit on `main`).
- [ ] One logical change per PR (no drive-by fixes).
- [ ] Tests added / updated for new behaviour; coverage not regressed.
- [ ] `ruff check`, `ruff format --check`, `ty check`, `pytest --cov` all green locally.
- [ ] No personal data, secrets, or real broker statements committed (`.gitignore` excludes `data/`, `*.csv`, `user.toml`; `git ls-files -ci --exclude-standard` must print nothing). Real-statement tests read private files from `IBKR_FA_PRIVATE_DATA` or `data/`, in the layout documented in `tests/_private_data.py`.
- [ ] Documentation updated if user-visible behaviour changed (README, CONTRIBUTING).

## Tax-correctness check (skip only for chore/docs/ci)

- [ ] If this affects a number that ends up in a Schedule FA cell, the value is independently traceable in the audit columns of the output CSV.
- [ ] Edge cases tested: empty input, single lot, lot acquired on Dec 31, lot disposed on Jan 1 of next CY, weekend acquisition (TTBR carry-forward).
- [ ] If this changes a `Decimal` rounding rule, the choice is documented inline and the test asserts the exact rounded output.
