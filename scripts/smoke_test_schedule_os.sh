#!/usr/bin/env bash
# scripts/smoke_test_schedule_os.sh
#
# Manual smoke-test wrapper for `ibkr-fa schedule-os` against a real-data
# IBKR Activity Statement. Parameterised over fixture path + FY + user.toml.
#
# Invariants asserted on the resulting CSV (via inline Python csv.reader
# helper because the writer quotes verbatim descriptions
# that may contain commas — splitting on commas in shell would mis-split):
#   1. CLI exit code is zero.
#   2. Stdout contains the verbose TOTAL line (renderer regression catch).
#   3. CSV file contains no NUL bytes (binary-contamination guard;
#      csv.reader silently accepts NULs as data).
#   4. Every row is exactly 11 columns (uniform-width contract).
#   5. Exactly one row's first cell equals the 1a(i) portal label exactly
#      (no startswith — must lock the user-verified ITR-2 label), and the
#      next row's first cell equals the 1b(ix) interest label exactly.

set -euo pipefail

usage() {
  echo "usage: $0 <input-csv> <financial-year-yyyy-yy> [config-toml]" >&2
  echo "  e.g.: $0 activity-statement.csv 2025-26 user.toml" >&2
  exit 64
}

if [ $# -lt 2 ] || [ $# -gt 3 ]; then
  usage
fi

INPUT="$1"
FY="$2"
CONFIG="${3:-./user.toml}"

if [ ! -f "$INPUT" ]; then
  echo "smoke: input file does not exist: $INPUT" >&2
  exit 64
fi
if [ ! -f "$CONFIG" ]; then
  echo "smoke: config file does not exist: $CONFIG" >&2
  exit 64
fi

OUT_DIR="$(mktemp -d)"
OUT_PATH="$OUT_DIR/schedule_os_smoke.csv"

echo "smoke: running ibkr-fa schedule-os against $INPUT (FY $FY)..." >&2
STDOUT_LOG="$OUT_DIR/stdout.log"
# --allow-masked-account: real IBKR exports mask account ID by default.
ibkr-fa schedule-os \
  --input "$INPUT" \
  --config "$CONFIG" \
  --output "$OUT_PATH" \
  --financial-year "$FY" \
  --allow-masked-account \
  | tee "$STDOUT_LOG"

echo "smoke: CLI exit OK; output at $OUT_PATH" >&2

# Renderer cross-check (catches stdout regressions, e.g., the verbose
# TOTAL prose lines being dropped from _render_schedule_os_report).
label="TOTAL 1a(i) ordinary dividends (rounded ₹)"
grep -q "$label" "$STDOUT_LOG" || {
  echo "smoke: stdout missing '$label' line" >&2
  exit 1
}

echo "smoke: stdout 1a(i) TOTAL line present"

# CSV invariant assertions; the portal label is imported from the package
# so a rename cannot drift from this check.
python3 - "$OUT_PATH" <<'PY'
import csv
import sys

from ibkr_fa.output.portal_labels import (
    PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND,
    PORTAL_FIELD_LABEL_OS_OTHER_INTEREST,
)

path = sys.argv[1]

with open(path, "rb") as fb:
    content = fb.read()
assert b"\x00" not in content, f"{path}: contains NUL byte (binary contamination)"

with open(path, encoding="utf-8-sig", newline="") as f:
    rows = list(csv.reader(f))

assert rows, f"{path}: file is empty"

for i, r in enumerate(rows, 1):
    assert len(r) == 11, f"{path}: row {i} has {len(r)} cols, want 11"

portal_indices = [
    i for i, r in enumerate(rows) if r and r[0] == PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND
]
assert len(portal_indices) == 1, (
    f"{path}: expected exactly one 1a(i) portal-TOTAL row "
    f"({PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND!r}); found {portal_indices}"
)

interest_indices = [
    i for i, r in enumerate(rows) if r and r[0] == PORTAL_FIELD_LABEL_OS_OTHER_INTEREST
]
assert interest_indices == [portal_indices[0] + 1], (
    f"{path}: expected exactly one 1b(ix) interest-TOTAL row "
    f"({PORTAL_FIELD_LABEL_OS_OTHER_INTEREST!r}) right after the 1a(i) "
    f"portal-TOTAL row; found {interest_indices}"
)

print(
    f"smoke: {len(rows)} rows, all width-11, "
    f"1a(i) portal-TOTAL + 1b(ix) interest-TOTAL exact-match: OK"
)
PY

echo "smoke: ALL CHECKS PASSED"
