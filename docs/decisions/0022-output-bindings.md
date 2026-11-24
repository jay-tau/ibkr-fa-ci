# ADR 0022: Output bindings separate public formats from writer classes

- **Status**: Accepted (2026-11-11)
- **Date**: 2026-11-11
- **Issue**: #1200
- **Related**: #790, #1201, #1206
- **Amended by**: ADR-0023

## Context

Each cataloged schedule output has a public slot, `(Subcommand, format_id)`,
such as `schedule-os` / `filing-csv`. Before #1200 the catalog in
`output/__init__.py` mapped each slot to an `OutputKey` (destination and
artifact), and the writer was whatever class `@register` had stored under that
key. Lookups refused a slot unless the key's writer declared the slot's
`format_id`, and bundle dispatch resolved the writer from the key alone.

The portal renderer cutovers (#1201 onwards) replace the per-schedule portal
writers with one shared CSV renderer and one shared filing-XLSX renderer. One
renderer must then serve slots with different public IDs (`filing-csv` and
`portal-csv`) and different artifacts. A class per key with one `format_id`
can't express that.

## Decision

- The catalog is the single support/binding declaration. It maps each slot to
  an `OutputBinding(key, target)`: the slot holds the public format ID, the
  binding holds the key of the file the slot writes and the target that
  renders it. A renderer cutover adds the schedule's row function and artifact
  profile to the binding; #1206 adds command selection to the same
  declaration. No column, getter, cell-kind or document-schema layer.
- Until its cutover, each binding's target is today's writer class. Writers
  still self-register under their key; every lookup checks the catalog against
  the registered writers and refuses a binding whose target is not the
  writer registered under its key, and a writer whose `format_id` differs from
  its slot's. All earlier catalog refusals stay.
- `output_binding(subcommand, format_id)` is the one lookup; it replaces
  `get_writer` and `output_key`. `binding.writer(ctx)` builds the run's
  writer. `writer_for(key)` is gone: no command looked a writer up by key
  outside bundle dispatch, and the CBDT JSON and Quicko writers are used by
  class.
- A bundle spec keeps its slot. `spec_for` looks the binding up so an unknown
  format refuses before any write, and `write_output_bundle` looks it up again
  as it writes, so a catalog that becomes inconsistent in between still refuses.
- Looking a binding up never reads the payload: no rows are consumed and no
  other artifact is evaluated.

## Consequences

- A shared renderer can serve several slots by being their bindings' target,
  without a writer class per schedule.
- Outputs, exit codes and CLI choices are unchanged; the writers are the same
  classes, built from the same context.
- Each binding names its writer twice for now, in the catalog and in the
  writer module's `@register`; the lookup check keeps the two in step. A
  cutover deletes the writer module and its registration.
- Code that looked writers up by key or by `get_writer` uses
  `output_binding(...)` instead; no aliases remain.
