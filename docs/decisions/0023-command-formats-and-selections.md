# ADR 0023: Command formats and selections come from the output declaration

- **Status**: Accepted (2026-11-11)
- **Date**: 2026-11-11
- **Issue**: #1206
- **Amends**: ADR-0022
- **Related**: #601, #642, #1201

## Context

ADR-0022 made the catalog the single support/binding declaration: which
`(Subcommand, format_id)` slots exist and what renders each. Which slots a
command offered or wrote was still spread across the CLI and the pipelines:

- `cli.py` built `--format` choices in three ways: from the catalog, as
  intersections of two schedules' catalog formats, or as literal lists.
- `netbenefits-all` fixed its Schedule FA leg to `filing-csv`, and `run-all`
  swapped Form 67 to `portal-csv` under its default `review-csv` (#601).
- Robinhood wrote the Schedule CG and OS filing workbooks under literal slots
  (#642), and the income bundle listed its slots next to its filenames.

## Decision

- The declaration in `ibkr_fa.output` also records command exposure.
  `_COMMAND_FORMATS` gives each command with `--format`, plus `netbenefits-all`,
  a `CommandFormats`: the schedules it writes in its chosen format, its
  `--format` choices in `--help` order, its default, and any substitutes (the
  `run-all` Form 67 swap).
  `netbenefits-all` is listed with no choices, so its FA leg writes the
  default, `filing-csv`. The CLI reads every `--format` through
  `command_formats(command)`.
- Selections name the slots a workflow writes whatever `--format` says:
  `INCOME_FILING` and `INCOME_REVIEW` (in write order), and Robinhood's
  `CG_FILING_WORKBOOK` and `OS_FILING_WORKBOOK`.
- Support isn't exposure. A command lists only the formats its workflow
  writes, so source restrictions stay: `netbenefits-fa` offers no `xlsx`.
- Every lookup and listing checks the command formats and selections against
  the catalog, after the ADR-0022 checks. It refuses a choice or default that
  isn't a slot of each of the command's schedules (the CBDT JSON fragment's
  format excepted), a default that isn't a choice when choices exist, a
  substitute that doesn't replace one of the command's choices with a slot,
  and a selection the catalog lacks.
- Selections choose what a run writes, never what it removes. Filenames,
  historical cleanup targets, promised-file checks, preflights, JSON ownership
  and `remove_outputs_on_failure` windows stay explicit in each workflow. The
  income bundle keeps its own filename inventory, and its callers clear and
  remove those names even if a selection drops a slot.

## Consequences

- Every choice list, order, default, filename and output byte is unchanged.
- A new format appears on a command only when that command's entry lists it.
- Renderer cutovers (#1201 onwards) change bindings, not command formats or
  selections.
