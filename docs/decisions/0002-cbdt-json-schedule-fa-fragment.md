# ADR 0002: CBDT Schedule FA JSON fragment — design decisions

- **Status**: Accepted; dependency decision superseded by ADR-0016; merge
  decision narrowed to strict merge-once (#299)
- **Date**: 2026-06-13
- **Deciders**: jay-tau
- **Issue**: #39

## Context

The official offline ITR-2 utility imports a JSON file conforming to the
CBDT-published ITR-2 JSON Schema. `ibkr-fa` can produce two of the
`ScheduleFA` tables — A2 `DtlsForeignCustodialAcc` (the IBKR account) and
A3 `DtlsForeignEquityDebtInterest` (the holdings). Emitting a JSON
*fragment* the user merges into their saved utility file would let them
import the foreign-asset disclosure instead of hand-copying every row.

The authoritative schema (AY 2026-27, vendored verbatim + pinned by sha256
in `src/ibkr_fa/reference/cbdt_itr2_schema_ay2026_27.json`) marks **every**
A2/A3 field `required`. But several required fields are ones `ibkr-fa`
deliberately leaves blank/manual-fill because they are genuinely
uncomputable from the IBKR Activity Statement — most critically the **A2
`PeakBalanceDuringPeriod`**, which (per Rule 114F + the CRS account-value
basis) is the highest *total* value (securities + cash) on any single day,
and the statement carries no daily cash/NAV series.

The CSV/xlsx writers leave such cells blank for the user to fill during the
manual portal copy. JSON cannot: a `required integer` must be present and
real. Fabricating one violates the project's governing rule ("it's taxes —
never be wrong"); omitting one produces a fragment the utility rejects.

This design was reviewed by a rubber-duck pass and a three-model external
panel (Gemini 3.1 Pro, Claude Opus 4.8, GPT-5.5).

## Decisions

1. **Gap-handling = config-supplied completion + all-or-nothing refuse.**
   The user supplies the genuinely-manual figures (e.g. the A2 peak) in
   config; the writer emits a *complete, schema-valid* fragment only when
   every required field is present, else **refuses** with an exact "supply
   X" message. Never fabricate, default, infer, clamp, or emit `null`.
   Config-supplied figures are treated as filed tax data (echoed in the run
   summary) and required as **integer rupees** (rounding a taxpayer's filed
   figure is not allowed).

2. **Manual values are scoped by calendar year *and* account number.** A
   peak is year- and account-specific; a mismatch against the run refuses,
   so a stale figure can't silently carry across years/accounts/amended
   statements.

3. **Validate every emitted item against the vendored schema at write
   time**, not just in tests — making the refuse gate comprehensive
   (maxLength, enum, pattern, type, sign, required). This needs a JSON
   Schema validator; see decision 6.

4. **JSON requires real per-issuer data; placeholders are forbidden.**
   JSON import bypasses the human review the CSV flow relies on, so a
   generic country-name A3 address or a blank ZIP would become *filed*
   data. The writer requires real `[[fa.entities]]` address + ZIP + verified
   nature of entity per holding (and refuses on `maxLength` overflow, e.g. a
   `12345-6789` ZIP) rather than emit a placeholder.

5. **A complete `ScheduleFA` fragment requires `[account.custodian]`.** The
   default refuses if A2 cannot be produced (no custodian configured),
   because a holdings-only (A3-only) fragment would silently omit the
   account-level disclosure. An A3-only emission, if offered, is an
   explicit expert opt-in with a loud "not a complete Schedule FA" warning.

6. **Superseded by ADR-0016.** `jsonschema` is now mandatory because
   `robinhood-all` automatically emits a validated fragment when its Schedule
   FA data is complete.

7. **The writer hard-binds to the schema's AY.** Unlike the per-AY portal
   labels (which forward-compat-fall-back to the latest snapshot), a
   schema-bound fragment must refuse when the run's AY ≠ the pinned
   `CBDT_SCHEMA_VERSION` AY.

8. **Ship a `merge` subcommand as the primary path.** Hand-merging a
   fragment into a possibly-minified multi-megabyte saved ITR JSON risks
   silently dropping the sibling tables (A1/A4–A10) or other-broker rows,
   or duplicating on re-run — a Black Money Act exposure. A tested
   `ibkr-fa merge` into the user's actual saved file is the documented
   primary path; raw fragment hand-merge is an expert fallback. The merge
   rule ("append/replace by identity") is superseded by "Strict
   merge-once" below.

9. **Stamp the fragment with an `_ibkrFa` provenance marker** (PR4). The
   emitted object is `{"_ibkrFa": {"format": "cbdt-json-fragment",
   "itrForm": "ITR-2", "schemaVersion": …}, "ScheduleFA": {…}}` — the marker sits BESIDE
   `ScheduleFA` (never inside it), so `merge` copies only the two
   `ScheduleFA` arrays and never carries the marker into the user's ITR.
   It is the only RELIABLE way for the `schedule-fa` re-run to tell its
   own prior output (safe to overwrite/remove) from a file it did not
   author: a bare `{"ScheduleFA": …}` shape is reproducible by a user's
   hand-merged file or another tool, so shape alone must not be treated as
   authorship. Before a JSON run the CLI pre-clears `--output` only when it
   carries this marker; any other existing file is preserved and the run
   is refused. (The earlier "bare fragment" framing of decisions 5/8 is
   superseded by this marker; the two-array merge contract is unchanged.)

## Consequences

Implemented across PRs: PR1 vendors the schema + the validation harness
(this PR); PR2 the CY+account-scoped `[schedule_fa.manual_values]` config;
PR3 the `cbdt-json-fragment` writer (mapping + runtime validation +
refuse-if-incomplete); PR4 the `--format` / `run-all` wiring; PR5 the
`merge` subcommand.

The full field mapping, schema element names, and panel synthesis are
recorded in the issue #39 working notes.

## Strict merge-once (2026-06-14, #299)

Decision 8 first said `merge` would append or replace rows **by identity**
(A2 by account number, A3 by lot). That was never built: when `merge` shipped
in #299, the owner chose strict merge-once instead. This section was added by
#960 to record that choice.

- An existing Table A2 row for the fragment's account is refused.
- Any existing Table A3 row is refused. The CBDT A3 item carries no ISIN or
  account key, so a per-lot identity would be a guess (entity name, dates,
  amounts), and two identical lots are legitimate.
- The user exports a clean return from the offline utility and merges into it
  once. Other accounts' Table A2 rows are kept, and the fragment's rows are
  appended after them.

## ITR-3 extension (2026-07-20, #414)

- Standalone fragments now require `--itr-form ITR-2|ITR-3` and stamp that form
  in `_ibkrFa.itrForm`.
- `merge` detects `ITR.ITR2.Form_ITR2` or `ITR.ITR3.Form_ITR3`, then refuses a
  fragment/target form mismatch.
- ITR-3 A2/A3 rows are validated at runtime against a pinned,
  validation-equivalent extract of the official AY 2026-27 ITR-3 V1.1 schema.
  The extract contains the exact A2/A3 definitions and their complete `$ref`
  closure, without the unrelated 1.1 MB business-return schedules.
- Legacy fragments without `itrForm` remain ITR-2-only because that was the only
  supported target when they were emitted.
