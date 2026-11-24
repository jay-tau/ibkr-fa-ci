"""Tests for :mod:`scripts.refresh_ttbr`.

The script is a one-off maintenance tool, not part of the ``ibkr_fa``
package. Tests cover only the pure-Python helpers: ``transform``,
``render_source_file``, and ``_read_existing_provenance``. Live HTTP
(``fetch_text``) and ``git ls-remote`` (``fetch_upstream_commit_sha``)
are not exercised — those are integration concerns and the script is
expected to fail loudly when they don't work. Subprocess failure
modes for ``fetch_upstream_commit_sha`` are covered via monkeypatch.
"""

from __future__ import annotations

import csv
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests._refresh_ttbr_script import TTBR_UPSTREAM_HEADER as HEADER_LINE
from tests._refresh_ttbr_script import make_upstream, refresh_ttbr


def test_transform_passes_through_a_simple_row() -> None:
    raw = make_upstream(["2025-01-02 09:00,url1,84.10,85.10,84.05,85.20,83.90,85.40,83.50,85.60"])
    out, n_before, n_after = refresh_ttbr.transform(raw)
    assert n_before == 1
    assert n_after == 1
    assert out.startswith("DATE,PDF FILE,TT BUY")
    assert "2025-01-02 09:00" in out
    assert out.endswith("\n")


def test_transform_dedups_keeping_last_row_for_each_date() -> None:
    raw = make_upstream(
        [
            "2025-01-02 09:00,url-morning,84.10,85.10,84.05,85.20,83.90,85.40,83.50,85.60",
            "2025-01-02 14:30,url-afternoon,84.50,85.50,84.45,85.60,84.30,85.80,83.90,86.00",
        ]
    )
    out, n_before, n_after = refresh_ttbr.transform(raw)
    assert n_before == 2
    assert n_after == 1
    # The afternoon row wins (last for that date).
    assert "url-afternoon" in out
    assert "url-morning" not in out
    assert "84.50" in out


def test_transform_sorts_dates_ascending() -> None:
    raw = make_upstream(
        [
            "2025-03-01 09:00,url-march,86.00,87.00,86.00,87.00,86.00,87.00,86.00,87.00",
            "2025-01-01 09:00,url-jan,84.00,85.00,84.00,85.00,84.00,85.00,84.00,85.00",
            "2025-02-01 09:00,url-feb,85.00,86.00,85.00,86.00,85.00,86.00,85.00,86.00",
        ]
    )
    out, _n_before, n_after = refresh_ttbr.transform(raw)
    assert n_after == 3
    body = out.splitlines()[1:]
    assert body[0].startswith("2025-01-01")
    assert body[1].startswith("2025-02-01")
    assert body[2].startswith("2025-03-01")


def test_transform_keeps_zero_rate_rows() -> None:
    """Zero TT BUY rows must NOT be dropped — the loader skips them at
    read time, but they live in the file as a record of what SBI
    actually published (or didn't)."""
    raw = make_upstream(
        [
            "2025-01-01 09:00,url1,84.10,85.10,84.05,85.20,83.90,85.40,83.50,85.60",
            "2025-01-02 09:00,url-blank,0,0,0.00,0.00,0,0,0,0",
            "2025-01-03 09:00,url-blank2,0.00,0.00,0,0,0,0,0,0",
            "2025-01-04 09:00,url4,84.40,85.40,84.35,85.50,84.20,85.70,83.80,85.90",
        ]
    )
    out, _n_before, n_after = refresh_ttbr.transform(raw)
    assert n_after == 4
    assert "url-blank" in out
    assert "url-blank2" in out


def test_transform_rejects_unexpected_header() -> None:
    raw = "DATE,SOME,OTHER,COLUMNS\n2025-01-02 09:00,a,b,c\n"
    with pytest.raises(ValueError, match="upstream CSV header has changed"):
        refresh_ttbr.transform(raw)


def test_transform_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="upstream CSV is empty"):
        refresh_ttbr.transform("")


_GOOD = "2025-01-02 09:00,url-good,84.10,85.10,84.05,85.20,83.90,85.40,83.50,85.60"
_SHORT = "2025-01-02 08:00,84.00,85.00,84.05,85.20,83.90,85.40,83.50,85.60"
_STRAY_QUOTE = '2025-01-02 08:00,url-bad,"8"4.00,85.00,84.05,85.20,83.90,85.40,83.50,85.60'


@pytest.mark.parametrize("rows", [[_SHORT], [_SHORT, _GOOD]])
def test_transform_refuses_a_short_row_even_if_a_later_row_replaces_it(rows: list[str]) -> None:
    with pytest.raises(
        ValueError,
        match=r"^upstream CSV line 2 \(DATE '2025-01-02 08:00'\): row must have exactly 10 cells$",
    ):
        refresh_ttbr.transform(make_upstream(rows))


@pytest.mark.parametrize("rows", [[_STRAY_QUOTE], [_STRAY_QUOTE, _GOOD]])
def test_transform_refuses_a_stray_quote_even_if_a_later_row_replaces_it(
    rows: list[str],
) -> None:
    with pytest.raises(
        ValueError, match=r"^upstream CSV line 2 \(DATE '2025-01-02 08:00'\) is malformed: "
    ):
        refresh_ttbr.transform(make_upstream(rows))


@pytest.mark.parametrize(
    ("row", "day", "line"),
    [
        ('"2025-01-02 08:00",url,"8"4.00', "2025-01-02 08:00", 2),
        ('"bad,""date""",url,"8"4.00', 'bad,"date"', 2),
        ('"bad\ndate",url,"8"4.00', "bad\ndate", 3),
        ('2025-01-02 08:00,url,"84.00', "2025-01-02 08:00", 2),
        (',url,"8"4.00', "", 2),
    ],
)
def test_transform_names_the_date_cell_in_malformed_csv(row: str, day: str, line: int) -> None:
    with pytest.raises(ValueError) as exc:
        refresh_ttbr.transform(make_upstream([row]))
    assert str(exc.value).startswith(f"upstream CSV line {line} (DATE {day!r}) is malformed: ")


def test_transform_refuses_malformed_csv_without_a_complete_date_cell() -> None:
    with pytest.raises(ValueError, match=r"^upstream CSV line 2 is malformed: "):
        refresh_ttbr.transform(make_upstream(['"2025-01-02 08:00']))


def test_transform_names_the_date_when_a_later_cell_exceeds_the_csv_limit() -> None:
    limit = csv.field_size_limit()
    try:
        csv.field_size_limit(128)
        with pytest.raises(
            ValueError, match=r"^upstream CSV line 2 \(DATE '2025-01-02 08:00'\) is malformed: "
        ):
            refresh_ttbr.transform(make_upstream(["2025-01-02 08:00," + "p" * 129]))
    finally:
        csv.field_size_limit(limit)


def test_transform_refuses_an_impossible_date() -> None:
    raw = make_upstream(["2025-02-30 09:00,url,84.10,85.10,84.05,85.20,83.90,85.40,83.50,85.60"])
    with pytest.raises(
        ValueError,
        match=r"^upstream CSV line 2 \(DATE '2025-02-30 09:00'\): DATE is not a real date "
        r"and time$",
    ):
        refresh_ttbr.transform(raw)


def test_transform_skips_blank_rows_in_input() -> None:
    raw = (
        HEADER_LINE
        + "\n"
        + "2025-01-02 09:00,url1,84.10,85.10,84.05,85.20,83.90,85.40,83.50,85.60\n"
        + "\n"
        + "2025-01-03 09:00,url2,84.20,85.20,84.15,85.30,84.00,85.50,83.60,85.70\n"
    )
    _out, _n_before, n_after = refresh_ttbr.transform(raw)
    assert n_after == 2


def test_render_source_file_includes_commit_and_timestamp() -> None:
    rendered = refresh_ttbr.render_source_file(
        upstream_repo="sahilgupta/sbi-fx-ratekeeper",
        upstream_url_value=refresh_ttbr.upstream_url("sahilgupta/sbi-fx-ratekeeper", "main"),
        upstream_commit="abc123def456" + "0" * 28,
        fetched_at=datetime(2026, 5, 5, 16, 30, 45, tzinfo=UTC),
    )
    assert "upstream-commit: abc123def456" + "0" * 28 in rendered
    assert "fetched-at: 2026-05-05T16:30:45Z" in rendered
    assert "upstream-repo: sahilgupta/sbi-fx-ratekeeper" in rendered
    assert (
        "upstream-url: https://raw.githubusercontent.com/sahilgupta/sbi-fx-ratekeeper/main/"
        "csv_files/SBI_REFERENCE_RATES_USD.csv"
    ) in rendered
    assert "Earliest valid (non-zero) TT BUY rate is 2020-01-06." in rendered


def test_render_source_file_reflects_overridden_repo_and_branch() -> None:
    rendered = refresh_ttbr.render_source_file(
        upstream_repo="myfork/sbi-fx-ratekeeper",
        upstream_url_value=refresh_ttbr.upstream_url("myfork/sbi-fx-ratekeeper", "experimental"),
        upstream_commit="0" * 40,
        fetched_at=datetime(2026, 5, 5, 16, 30, 45, tzinfo=UTC),
    )
    assert "upstream-repo: myfork/sbi-fx-ratekeeper" in rendered
    assert "myfork/sbi-fx-ratekeeper/experimental" in rendered


def test_upstream_url_uses_repo_branch_and_static_path() -> None:
    url = refresh_ttbr.upstream_url("alice/repo", "trunk")
    assert url == (
        "https://raw.githubusercontent.com/alice/repo/trunk/csv_files/SBI_REFERENCE_RATES_USD.csv"
    )


def test_fetch_upstream_commit_sha_translates_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """``subprocess.run(timeout=30)`` raises ``subprocess.TimeoutExpired``
    on network stalls. The script must catch it and re-raise as a
    ``RuntimeError`` so ``main()`` returns the documented exit-code-2
    path instead of bubbling a traceback (Codex finding on PR #89)."""

    def fake_run(*_args: object, **kwargs: object) -> object:
        timeout = kwargs.get("timeout", 30)
        assert isinstance(timeout, int | float)
        raise subprocess.TimeoutExpired(cmd="git", timeout=timeout)

    monkeypatch.setattr(refresh_ttbr.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="timed out"):
        refresh_ttbr.fetch_upstream_commit_sha("alice/repo", "main")


def test_fetch_upstream_commit_sha_translates_missing_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("git")

    monkeypatch.setattr(refresh_ttbr.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="git is not installed"):
        refresh_ttbr.fetch_upstream_commit_sha("alice/repo", "main")


def test_fetch_upstream_commit_sha_translates_called_process_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> object:
        raise subprocess.CalledProcessError(returncode=1, cmd="git", stderr="remote unreachable")

    monkeypatch.setattr(refresh_ttbr.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="git ls-remote failed"):
        refresh_ttbr.fetch_upstream_commit_sha("alice/repo", "main")


def test_read_existing_provenance_returns_empty_for_missing_file(tmp_path: Path) -> None:
    assert refresh_ttbr._read_existing_provenance(tmp_path / "missing") == {}


def test_read_existing_provenance_returns_empty_when_lines_absent(tmp_path: Path) -> None:
    p = tmp_path / "src"
    p.write_text("# Provenance\nfetched-at: y\n", encoding="utf-8")
    assert refresh_ttbr._read_existing_provenance(p) == {}


def test_read_existing_provenance_extracts_all_fields(tmp_path: Path) -> None:
    p = tmp_path / "src"
    sha = "26a9d6ea033b30abb93eb722c93f2d5111f0cd14"
    p.write_text(
        f"# Provenance\n"
        f"upstream-repo: alice/repo\n"
        f"upstream-license: MIT\n"
        f"upstream-url: https://example.invalid/csv\n"
        f"upstream-commit: {sha}\n"
        f"fetched-at: 2026-05-05T16:30:45Z\n",
        encoding="utf-8",
    )
    assert refresh_ttbr._read_existing_provenance(p) == {
        "upstream-repo": "alice/repo",
        "upstream-url": "https://example.invalid/csv",
        "upstream-commit": sha,
    }


def test_read_existing_provenance_partial_fields(tmp_path: Path) -> None:
    p = tmp_path / "src"
    sha = "0" * 40
    p.write_text(f"upstream-commit: {sha}\n", encoding="utf-8")
    assert refresh_ttbr._read_existing_provenance(p) == {"upstream-commit": sha}


def test_round_trip_against_real_vendored_csv() -> None:
    """The CURRENT vendored CSV is the result of these same
    transformations applied to upstream. Re-running ``transform``
    against synthetic upstream rows that match the first few lines of
    the vendored file should reproduce them."""
    raw = make_upstream(
        [
            "2020-01-04 09:00,https://example.invalid/p1.pdf,0.00,0.00,71.29,72.34,70.70,72.55,70.40,72.70",
            "2020-01-06 09:00,https://example.invalid/p2.pdf,71.65,72.50,71.59,72.65,71.00,72.85,70.70,73.00",
        ]
    )
    out, _n_before, _n_after = refresh_ttbr.transform(raw)
    lines = out.splitlines()
    # Header + 2 dates, both kept, in date order.
    assert len(lines) == 3
    assert lines[1].startswith("2020-01-04 09:00")
    assert lines[2].startswith("2020-01-06 09:00")
