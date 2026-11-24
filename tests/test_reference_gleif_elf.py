from __future__ import annotations

import hashlib
from importlib import resources
from typing import TYPE_CHECKING

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.reference import gleif_elf
from ibkr_fa.reference.gleif_elf import load_gleif_elf_codes, lookup_gleif_elf

if TYPE_CHECKING:
    from pathlib import Path

_DIGEST_REFUSAL = "gleif_elf_codes.csv content doesn't match the pinned SHA-256"


def _refusal_for_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: bytes
) -> InvariantError:
    """Serve ``content`` as the vendored CSV and return the loader's refusal."""
    (tmp_path / "gleif_elf_codes.csv").write_bytes(content)
    monkeypatch.setattr(resources, "files", lambda _pkg: tmp_path)
    load_gleif_elf_codes.cache_clear()
    try:
        with pytest.raises(InvariantError) as excinfo:
            load_gleif_elf_codes()
    finally:
        load_gleif_elf_codes.cache_clear()
    return excinfo.value


def test_vendored_elf_codes_pin_expected_legal_forms() -> None:
    entries = load_gleif_elf_codes()

    assert len(entries) == 3599
    assert entries["H1UM"].english_name == "For-Profit Corporation General Stock"
    assert entries["XTIQ"].english_name == "Corporation"
    assert entries["T91T"].english_name == "Limited Partnership"


def test_reserved_or_unknown_elf_code_has_no_vendored_label() -> None:
    assert lookup_gleif_elf("8888") is None
    assert lookup_gleif_elf("9999") is None


@pytest.mark.parametrize(
    ("row", "what"),
    [
        ('"XT\nIQ",US,US-OR,ACTV,Corporation', "code must be printable"),
        ('"XT"IQ,US,US-OR,ACTV,Corporation', "malformed CSV"),
        ('XTIQ,US,US-OR,ACTV,"Corpo\nration"', "english_name must be printable"),
        ("XTIQ,US,US-OR,ACTV, Corporation", "english_name must be printable with no leading"),
        ("XTIQ,US,US-OR,RETIRED,Corporation", "status must be ACTV or INAC"),
        ("XTIQ,US,US_OR,ACTV,Corporation", "subdivision_code must be empty or ISO 3166-2"),
    ],
)
def test_load_refuses_a_cell_that_is_not_a_whole_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, row: str, what: str
) -> None:
    """Strict parsing and whole-cell checks (#945): the old loader joined a
    quoted multi-line cell, read ``"XT"IQ`` as ``XTIQ``, and took any
    label, subdivision or non-empty status. The digest is pinned to the
    synthetic content so the shape checks run (#946)."""
    content = f"code,country_iso2,subdivision_code,status,english_name\n{row}\n".encode()
    monkeypatch.setattr(gleif_elf, "_CSV_SHA256", hashlib.sha256(content).hexdigest())
    refusal = _refusal_for_copy(monkeypatch, tmp_path, content)
    assert what in refusal.what
    assert "gleif_elf_codes.csv" in refusal.what


def test_pinned_digest_and_row_count_match_the_source_file() -> None:
    """A re-vendor that updates the ``.source`` file but not the loader's
    constants, or the other way round, fails here (#946)."""
    text = (
        resources.files("ibkr_fa.reference")
        .joinpath("gleif_elf_codes.csv.source")
        .read_text(encoding="utf-8")
    )
    source = dict(line.split("=", 1) for line in text.splitlines() if not line.startswith("#"))
    assert source["csv_sha256"] == gleif_elf._CSV_SHA256
    assert int(source["rows"]) == gleif_elf._PINNED_ROW_COUNT


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param(
            b"\nXTIQ,US,US-DE,ACTV,Corporation\n",
            b"\nXTIQ,US,US-DE,ACTV,Limited Partnership\n",
            id="relabelled-row",
        ),
        pytest.param(b"\n09K3,FO,,ACTV,\n", b"\n09K3,FO,,ACTV,X\n", id="byte-in-blank-label"),
        pytest.param(
            b"code,country_iso2,", b"code;country_iso2,", id="bad-header-refused-by-digest-first"
        ),
    ],
)
def test_load_refuses_a_copy_whose_bytes_differ_from_the_pinned_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, old: bytes, new: bytes
) -> None:
    """The first two edits pass every shape and count check, so only the
    digest catches them; the third shows the digest is checked before
    parsing (#946). The refusal names the file and holds no row text."""
    vendored = resources.files("ibkr_fa.reference").joinpath("gleif_elf_codes.csv").read_bytes()
    assert vendored.count(old) == 1
    refusal = _refusal_for_copy(monkeypatch, tmp_path, vendored.replace(old, new))
    assert refusal.what == _DIGEST_REFUSAL
    assert refusal.context == ()
