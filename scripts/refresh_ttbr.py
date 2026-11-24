#!/usr/bin/env python3
"""Refresh ``src/ibkr_fa/reference/sbi_ttbr_usd_inr.csv`` from upstream.

Fetches the latest SBI TT-Buying-Rate USD CSV from
``sahilgupta/sbi-fx-ratekeeper`` on GitHub, applies the documented
vendor-time transformations (dedup-by-date, sort, keep zero-rate
rows), and rewrites the vendored CSV plus its ``.source`` provenance
file in-place.

Usage::

    uv run python scripts/refresh_ttbr.py            # write changes
    uv run python scripts/refresh_ttbr.py --dry-run  # report only
    uv run python scripts/refresh_ttbr.py \\
        --upstream-repo myfork/sbi-fx-ratekeeper \\
        --upstream-branch main

Idempotent: if the upstream commit and the transformed CSV bytes both
match what's already on disk, exits 0 with "already up to date" and
touches nothing.

Why this script instead of git submodule: the upstream CSV needs
deterministic vendor-time transformations applied (see
``sbi_ttbr_usd_inr.csv.source`` for the full list) before the loader
can use it. A submodule would skip that step, leaving the loader to
re-derive the transformations on every read. Vendoring the
transformed file keeps the read path simple and makes the
transformations auditable in version control.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

from ibkr_fa.fx.ttbr_loader import TTBR_HEADER, parse_ttbr_row

DEFAULT_UPSTREAM_REPO: Final[str] = "sahilgupta/sbi-fx-ratekeeper"
DEFAULT_UPSTREAM_BRANCH: Final[str] = "main"
UPSTREAM_PATH: Final[str] = "csv_files/SBI_REFERENCE_RATES_USD.csv"


def upstream_url(repo: str, branch: str) -> str:
    """Build the raw-content URL for the upstream CSV.

    Defined as a single function so the URL, the GitHub repo, and the
    branch can never get out of sync — every override of repo/branch
    automatically updates the URL too.
    """
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{UPSTREAM_PATH}"


_GIT_SHA_LENGTH: Final[int] = 40

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
CSV_PATH: Final[Path] = REPO_ROOT / "src/ibkr_fa/reference/sbi_ttbr_usd_inr.csv"
SOURCE_PATH: Final[Path] = CSV_PATH.with_suffix(CSV_PATH.suffix + ".source")


def fetch_text(url: str, *, timeout: float = 30.0) -> str:
    """Fetch ``url`` and return its body decoded as UTF-8."""
    req = urllib.request.Request(url, headers={"User-Agent": "ibkr-fa-refresh-ttbr/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body: bytes = resp.read()
    return body.decode("utf-8")


def fetch_upstream_commit_sha(repo: str, branch: str) -> str:
    """Return the current commit SHA of ``branch`` in ``repo`` via ``git ls-remote``.

    Uses ``git ls-remote`` rather than the GitHub API so unauthenticated
    runs aren't subject to API rate-limiting. Returns the full 40-char
    SHA.

    Raises:
        RuntimeError: If ``git`` is not installed, the remote is
            unreachable, or the branch doesn't exist.
    """
    url = f"https://github.com/{repo}.git"
    try:
        result = subprocess.run(
            ["git", "ls-remote", url, f"refs/heads/{branch}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git is not installed; required to look up upstream SHA") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"git ls-remote timed out after {exc.timeout}s for {url} {branch!r}; "
            f"upstream is unreachable or network is degraded"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"git ls-remote failed for {url} {branch!r}: {exc.stderr.strip()}"
        ) from exc
    line = result.stdout.strip().splitlines()
    if not line:
        raise RuntimeError(f"branch {branch!r} not found on {url}")
    sha = line[0].split()[0]
    if len(sha) != _GIT_SHA_LENGTH or not all(c in "0123456789abcdef" for c in sha):
        raise RuntimeError(f"unexpected SHA shape from git ls-remote: {sha!r}")
    return sha


def transform(raw_csv: str) -> tuple[str, int, int]:
    """Apply the documented vendor-time transformations.

    Steps:
      1. Parse strictly (``csv`` ``strict=True``) and verify the header
         is exactly :data:`~ibkr_fa.fx.ttbr_loader.TTBR_HEADER` (fail
         loud on any upstream schema drift).
      2. Check every non-blank row with
         :func:`~ibkr_fa.fx.ttbr_loader.parse_ttbr_row`, the loader's
         own rules, before the dedup: ten printable cells, a real
         ``YYYY-MM-DD HH:MM`` date and a plain-number ``TT BUY``. So a
         row that a later row for the same date replaces is checked
         too. Blank lines are skipped.
      3. Dedup by calendar date; keep the LAST row published for each
         date, matching the close-of-business intuition documented in
         the .source file.
      4. Sort ascending by calendar date.
      5. Zero-rate rows are kept (the loader skips them at read time).

    Args:
        raw_csv: The raw upstream CSV body.

    Returns:
        ``(transformed_csv_text, n_rows_before, n_rows_after)``.
        ``transformed_csv_text`` ends with a single trailing newline,
        matching the existing vendored file's shape.

    Raises:
        ValueError: If the upstream CSV is empty or malformed, its
            header doesn't match the expected shape, or a row breaks
            the loader's rules. The message names the line, and the
            row's ``DATE`` cell when there is one. Caller must
            investigate before refreshing.
    """
    source = io.StringIO(raw_csv, newline="")
    reader = csv.reader(source, strict=True)
    records: list[tuple[int, list[str]]] = []
    record_start = 0
    try:
        for row in reader:
            records.append((reader.line_num, row))
            record_start = source.tell()
    except csv.Error as exc:
        source.seek(record_start)
        # Recover only a complete first cell for diagnostics; the record stays refused.
        first_cell = re.match(
            r'\A(?:"((?:[^"]|"")*)"|([^",\r\n][^,\r\n]*|))(?=,|\r|\n|\Z)', source.read()
        )
        date_context = ""
        if first_cell is not None:
            raw_date = (
                first_cell[1].replace('""', '"') if first_cell[1] is not None else first_cell[2]
            )
            date_context = f" (DATE {raw_date!r})"
        msg = f"upstream CSV line {reader.line_num}{date_context} is malformed: {exc}"
        raise ValueError(msg) from exc
    if not records:
        msg = "upstream CSV is empty (line 1)"
        raise ValueError(msg)
    header = tuple(records[0][1])
    if header != TTBR_HEADER:
        msg = (
            f"upstream CSV header has changed at line {records[0][0]}; expected {TTBR_HEADER!r}, "
            f"got {header!r}. Refusing to refresh — re-verify the columns "
            f"and update TTBR_HEADER in src/ibkr_fa/fx/ttbr_loader.py."
        )
        raise ValueError(msg)
    rows = records[1:]
    n_before = len(rows)
    by_date: dict[date, list[str]] = {}
    for line, row in rows:
        if not row:
            continue
        try:
            day, _rate = parse_ttbr_row(row)
        except ValueError as exc:
            msg = f"upstream CSV line {line} (DATE {row[0]!r}): {exc}"
            raise ValueError(msg) from exc
        by_date[day] = row
    sorted_dates = sorted(by_date)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(TTBR_HEADER)
    for d in sorted_dates:
        writer.writerow(by_date[d])
    return out.getvalue(), n_before, len(sorted_dates)


def render_source_file(
    *, upstream_repo: str, upstream_url_value: str, upstream_commit: str, fetched_at: datetime
) -> str:
    """Render the ``.source`` provenance file with new metadata.

    The content matches the existing file's shape exactly so the diff
    is just a few lines (commit SHA, timestamp, and — if the
    repo/branch were overridden — the URL/repo lines) on each refresh.
    """
    return f"""# Provenance for sbi_ttbr_usd_inr.csv
# Refresh by running scripts/refresh_ttbr.py.

upstream-repo: {upstream_repo}
upstream-license: MIT
upstream-path: {UPSTREAM_PATH}
upstream-url: {upstream_url_value}
upstream-commit: {upstream_commit}
fetched-at: {fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ")}

# ---------------------------------------------------------------------------
# Vendor-time transformations applied to the raw upstream CSV
# (the loader treats this file as-is; reproducible from upstream-commit
# above by applying these same rules):
#
# 1. Dedup by calendar date.
#    Upstream emits one row per SBI publication. SBI sometimes publishes
#    intraday updates, producing 2+ rows per calendar date. We collapse
#    each date to a single row by keeping the LAST published rate of the
#    day (close-of-business intuition).
#
# 2. Sort by calendar date ascending.
#    Required so loader can binary-search and so any human inspection
#    is monotonic.
#
# 3. Zero-rate rows are KEPT in the file.
#    Days when SBI did not publish a TT-Buying rate appear with
#    TT BUY = "0" or "0.00" (both string forms occur). The loader skips
#    rows where the parsed TT BUY value equals 0 (use numeric comparison,
#    not a string match against "0.00", or you'll miss the "0" rows).
#    The FaExactDatePolicy carry-forward then takes over.
#    Earliest valid (non-zero) TT BUY rate is 2020-01-06.
"""


_PROVENANCE_FIELDS: Final[tuple[str, ...]] = (
    "upstream-repo",
    "upstream-url",
    "upstream-commit",
)


def _read_existing_provenance(source_path: Path) -> dict[str, str]:
    """Parse stable provenance fields from an existing ``.source`` file.

    Returns a dict mapping each of :data:`_PROVENANCE_FIELDS` present
    in the file to its value. Missing fields are simply absent from
    the returned dict. ``fetched-at`` is intentionally not included
    since it changes on every refresh and would defeat idempotency.
    """
    if not source_path.exists():
        return {}
    fields: dict[str, str] = {}
    for line in source_path.read_text(encoding="utf-8").splitlines():
        for key in _PROVENANCE_FIELDS:
            if line.startswith(f"{key}:"):
                fields[key] = line.split(":", 1)[1].strip()
                break
    return fields


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh the vendored SBI TTBR USD CSV from upstream.",
    )
    parser.add_argument(
        "--upstream-repo",
        default=DEFAULT_UPSTREAM_REPO,
        help=(
            f"GitHub owner/repo to fetch from (default: {DEFAULT_UPSTREAM_REPO}). "
            f"Override only when refreshing from a fork or mirror."
        ),
    )
    parser.add_argument(
        "--upstream-branch",
        default=DEFAULT_UPSTREAM_BRANCH,
        help=f"Branch to fetch from (default: {DEFAULT_UPSTREAM_BRANCH}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and transform but don't write anything",
    )
    args = parser.parse_args(argv)

    # Resolve the upstream commit SHA FIRST so we can fetch the CSV
    # from a commit-pinned URL. Fetching by branch ref then recording
    # the branch's CURRENT SHA would leave a window where upstream
    # could move between the two calls and the recorded SHA would no
    # longer reproduce the fetched CSV.
    try:
        sha = fetch_upstream_commit_sha(args.upstream_repo, args.upstream_branch)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Upstream {args.upstream_repo}@{args.upstream_branch} commit: {sha}")

    fetch_url = upstream_url(args.upstream_repo, sha)
    print(f"Fetching upstream CSV from {fetch_url}")
    try:
        raw = fetch_text(fetch_url)
    except (urllib.error.URLError, TimeoutError) as exc:
        # urllib.request.urlopen raises socket TimeoutError directly
        # on connection-establishment timeouts (not always wrapped in
        # URLError), so catch both to keep the documented "exit 2 with
        # ERROR: ..." path for transient network failures.
        print(f"ERROR: failed to fetch upstream CSV: {exc}", file=sys.stderr)
        return 2

    try:
        transformed, n_before, n_after = transform(raw)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    print(f"Transformed: {n_before} upstream rows -> {n_after} unique dates")

    existing_csv = CSV_PATH.read_text(encoding="utf-8") if CSV_PATH.exists() else None
    existing_provenance = _read_existing_provenance(SOURCE_PATH)
    new_provenance = {
        "upstream-repo": args.upstream_repo,
        "upstream-url": fetch_url,
        "upstream-commit": sha,
    }
    if existing_csv == transformed and existing_provenance == new_provenance:
        print("Already up to date; nothing to write.")
        return 0

    if existing_csv == transformed:
        print("CSV bytes unchanged; only updating .source provenance.")
    else:
        print("CSV bytes changed; rewriting vendored file.")

    if args.dry_run:
        print("--dry-run set; not writing.")
        return 0

    fetched_at = datetime.now(UTC)
    CSV_PATH.write_text(transformed, encoding="utf-8")
    SOURCE_PATH.write_text(
        render_source_file(
            upstream_repo=args.upstream_repo,
            upstream_url_value=fetch_url,
            upstream_commit=sha,
            fetched_at=fetched_at,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {CSV_PATH.relative_to(REPO_ROOT)}")
    print(f"Wrote {SOURCE_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
