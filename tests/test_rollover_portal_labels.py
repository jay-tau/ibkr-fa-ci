"""Tests for :mod:`scripts.rollover_portal_labels` (issues #66, #686)."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

from ibkr_fa.exceptions import IbkrFaError

# The script lives in scripts/ which isn't a package, so import it
# from its file path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "rollover_portal_labels.py"
_spec = importlib.util.spec_from_file_location("rollover_portal_labels", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
rollover_portal_labels = importlib.util.module_from_spec(_spec)
sys.modules["rollover_portal_labels"] = rollover_portal_labels
_spec.loader.exec_module(rollover_portal_labels)


def _write_minimal_source_snapshot(package_dir: Path, *, from_ay: int = 2026) -> Path:
    """Write a minimal valid-looking source snapshot for tests.

    The rollover helper does NOT parse Python — it copies bytes and
    text-substitutes the AY label. A short fixture is enough.
    """
    end_short = (from_ay + 1) % 100
    path = package_dir / f"_ay_{from_ay}_{end_short:02d}.py"
    path.write_text(
        f'"""ITR-2 portal labels — pinned snapshot for AY {from_ay}-{end_short:02d}.\n'
        f"\n"
        f"Verified against the AY {from_ay}-{end_short:02d} ITR-2 utility.\n"
        f'"""\n'
        f"\n"
        f"from __future__ import annotations\n"
        f"\n"
        f"PORTAL_DATE_FORMAT = '%d/%m/%Y'\n",
        encoding="utf-8",
    )
    return path


def test_scaffold_writes_target_with_todo_header(tmp_path: Path) -> None:
    """Happy path: the target file is created with the TODO block on top."""
    _write_minimal_source_snapshot(tmp_path, from_ay=2026)

    result = rollover_portal_labels.scaffold_next_ay_snapshot(
        from_ay_start=2026,
        to_ay_start=2027,
        package_dir=tmp_path,
    )

    assert isinstance(result, rollover_portal_labels.ScaffoldResult)
    assert result.from_ay_start == 2026
    assert result.to_ay_start == 2027
    assert result.target_path == tmp_path / "_ay_2027_28.py"
    assert result.target_path.exists()
    assert result.overwrote_existing is False
    assert result.bytes_written > 0

    target_text = result.target_path.read_text(encoding="utf-8")
    # TODO header is present.
    assert "TODO (issue #66 rollover)" in target_text
    assert "scaffolded from _ay_2026_27.py" in target_text
    # Header is comments, file is still importable-looking.
    assert "from __future__ import annotations" in target_text


def test_scaffold_rewrites_ay_label_in_docstring(tmp_path: Path) -> None:
    """The source AY label in the docstring is rewritten to the target AY.

    The maintainer still needs to verify everything else, but the
    docstring at least claims the correct AY so future readers
    don't get confused.
    """
    _write_minimal_source_snapshot(tmp_path, from_ay=2026)

    rollover_portal_labels.scaffold_next_ay_snapshot(
        from_ay_start=2026,
        to_ay_start=2027,
        package_dir=tmp_path,
    )

    target_text = (tmp_path / "_ay_2027_28.py").read_text(encoding="utf-8")
    # Docstring now says AY 2027-28 (not 2026-27).
    assert "snapshot for AY 2027-28" in target_text
    # The original AY label in the docstring is gone (the rewrite is
    # a full string-replace — we tolerate that older AY references
    # in a docstring become the new label, which is the desired
    # behaviour for a forward-only scaffolder).
    assert "snapshot for AY 2026-27" not in target_text


def test_scaffold_refuses_to_overwrite_by_default(tmp_path: Path) -> None:
    """Without --force, an existing target file must not be clobbered."""
    _write_minimal_source_snapshot(tmp_path, from_ay=2026)
    (tmp_path / "_ay_2027_28.py").write_text("# in-progress edits", encoding="utf-8")

    with pytest.raises(rollover_portal_labels.SnapshotTargetExistsError) as excinfo:
        rollover_portal_labels.scaffold_next_ay_snapshot(
            from_ay_start=2026,
            to_ay_start=2027,
            package_dir=tmp_path,
        )

    assert excinfo.value.to_ay_start == 2027
    assert "_ay_2027_28.py" in str(excinfo.value)
    # The in-progress edits MUST still be there.
    assert (tmp_path / "_ay_2027_28.py").read_text(encoding="utf-8") == "# in-progress edits"


def test_scaffold_force_overwrites_existing(tmp_path: Path) -> None:
    """With --force, the existing target file is clobbered + flagged."""
    _write_minimal_source_snapshot(tmp_path, from_ay=2026)
    (tmp_path / "_ay_2027_28.py").write_text("# old", encoding="utf-8")

    result = rollover_portal_labels.scaffold_next_ay_snapshot(
        from_ay_start=2026,
        to_ay_start=2027,
        package_dir=tmp_path,
        force=True,
    )

    assert result.overwrote_existing is True
    assert (tmp_path / "_ay_2027_28.py").read_text(encoding="utf-8") != "# old"


def test_scaffold_raises_on_missing_source(tmp_path: Path) -> None:
    """Source-not-found surfaces the structured domain error."""
    with pytest.raises(rollover_portal_labels.SnapshotSourceNotFoundError) as excinfo:
        rollover_portal_labels.scaffold_next_ay_snapshot(
            from_ay_start=2026,
            to_ay_start=2027,
            package_dir=tmp_path,
        )

    assert excinfo.value.from_ay_start == 2026
    assert "AY 2026-27" in str(excinfo.value)
    assert "_ay_2026_27.py" in str(excinfo.value.source_path.name)


def test_scaffold_raises_on_backwards_direction(tmp_path: Path) -> None:
    """``to_ay < from_ay`` is rejected up front (no file I/O)."""
    _write_minimal_source_snapshot(tmp_path, from_ay=2027)

    with pytest.raises(rollover_portal_labels.InvalidRolloverDirectionError) as excinfo:
        rollover_portal_labels.scaffold_next_ay_snapshot(
            from_ay_start=2027,
            to_ay_start=2026,
            package_dir=tmp_path,
        )

    assert excinfo.value.from_ay_start == 2027
    assert excinfo.value.to_ay_start == 2026
    # Source still exists; no file was created at the target name.
    assert (tmp_path / "_ay_2027_28.py").exists()
    assert not (tmp_path / "_ay_2026_27.py").exists()


def test_scaffold_raises_on_equal_ays(tmp_path: Path) -> None:
    """``to_ay == from_ay`` is rejected (no-op rollover)."""
    _write_minimal_source_snapshot(tmp_path, from_ay=2026)

    with pytest.raises(rollover_portal_labels.InvalidRolloverDirectionError):
        rollover_portal_labels.scaffold_next_ay_snapshot(
            from_ay_start=2026,
            to_ay_start=2026,
            package_dir=tmp_path,
        )


def test_scaffold_raises_on_implausible_from_ay(tmp_path: Path) -> None:
    """Typo guard: 3-digit AYs are rejected."""
    with pytest.raises(rollover_portal_labels.ImplausibleAssessmentYearError) as excinfo:
        rollover_portal_labels.scaffold_next_ay_snapshot(
            from_ay_start=226,
            to_ay_start=2027,
            package_dir=tmp_path,
        )

    assert excinfo.value.field == "from_ay_start"
    assert excinfo.value.assessment_year_start == 226


def test_scaffold_raises_on_implausible_to_ay(tmp_path: Path) -> None:
    """Typo guard applies to the target AY too."""
    with pytest.raises(rollover_portal_labels.ImplausibleAssessmentYearError) as excinfo:
        rollover_portal_labels.scaffold_next_ay_snapshot(
            from_ay_start=2026,
            to_ay_start=227,
            package_dir=tmp_path,
        )

    assert excinfo.value.field == "to_ay_start"


def test_scaffold_can_skip_multiple_ays(tmp_path: Path) -> None:
    """The helper supports skipping AYs (e.g., scaffold from 2026 to 2028).

    Useful when the maintainer has been away for a year and CBDT
    published a new utility two AYs later. The TODO header still
    cites the correct source.
    """
    _write_minimal_source_snapshot(tmp_path, from_ay=2026)

    result = rollover_portal_labels.scaffold_next_ay_snapshot(
        from_ay_start=2026,
        to_ay_start=2028,
        package_dir=tmp_path,
    )

    assert result.target_path == tmp_path / "_ay_2028_29.py"
    target_text = result.target_path.read_text(encoding="utf-8")
    assert "scaffolded from _ay_2026_27.py" in target_text
    assert "for AY 2028-29" in target_text


def test_all_rollover_errors_are_ibkr_fa_domain_errors() -> None:
    """All rollover-raised errors must be catchable as IbkrFaError.

    Mirrors the contract established in PR #200: domain errors
    inherit from IbkrFaError so :func:`main`'s
    ``except IbkrFaError`` handler renders one-line user-facing
    messages instead of letting a traceback escape.
    """
    assert issubclass(rollover_portal_labels.SnapshotSourceNotFoundError, IbkrFaError)
    assert issubclass(rollover_portal_labels.SnapshotTargetExistsError, IbkrFaError)
    assert issubclass(rollover_portal_labels.InvalidRolloverDirectionError, IbkrFaError)
    assert issubclass(rollover_portal_labels.ImplausibleAssessmentYearError, IbkrFaError)


def test_package_dir_is_the_checkouts_portal_labels_package() -> None:
    """The script writes into this checkout's source tree (issue #686).

    Never into an installed ``ibkr_fa`` package: the path is derived
    from the script's own location, and it must hold the AY 2026-27
    seed snapshot from issue #65.
    """
    pkg_dir = rollover_portal_labels.PACKAGE_DIR
    assert pkg_dir == _REPO_ROOT / "src" / "ibkr_fa" / "output" / "portal_labels"
    assert (pkg_dir / "_ay_2026_27.py").exists()


def test_real_ay_2026_27_snapshot_round_trips_through_scaffolder(tmp_path: Path) -> None:
    """End-to-end: scaffold from the real AY 2026-27 snapshot.

    Copies the real snapshot into a tmp package dir, scaffolds AY
    2027-28 from it, and verifies the resulting file would import
    cleanly (it must parse as Python). This catches accidental
    text substitutions that produce invalid Python.
    """
    import ast  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    real_pkg = rollover_portal_labels.PACKAGE_DIR
    shutil.copy(real_pkg / "_ay_2026_27.py", tmp_path / "_ay_2026_27.py")

    result = rollover_portal_labels.scaffold_next_ay_snapshot(
        from_ay_start=2026,
        to_ay_start=2027,
        package_dir=tmp_path,
    )

    target_text = result.target_path.read_text(encoding="utf-8")
    # Must be syntactically valid Python (the TODO header is a
    # comment block; the rewritten docstring still parses).
    ast.parse(target_text)
    # All the labels the package re-exports must still be present
    # as identifiers — the rewrite must not have accidentally
    # collided with a label name.
    for label_name in (
        "PORTAL_COLUMN_HEADERS_FA_A3",
        "PORTAL_DATE_FORMAT",
        "PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND",
        "PORTAL_FIELD_LABEL_OS_OTHER_INTEREST",
        "PORTAL_FIELD_LABEL_CG_FOREIGN_STCG",
        "PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM",
        "PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG",
        "PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG_BY_ITR_FORM",
        "PORTAL_SECTION_REF_CG_FOREIGN_STCG_BY_ITR_FORM",
        "PORTAL_SECTION_REF_CG_FOREIGN_LTCG_BY_ITR_FORM",
        "PORTAL_COLUMN_HEADERS_CG_TABLE_F",
        "PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM",
        "PORTAL_ROW_LABEL_CG_TABLE_F_LTCG_BY_ITR_FORM",
        "PORTAL_COLUMN_HEADERS_TR_BY_ITR_FORM",
        "format_country_for_portal",
        "format_date_for_portal",
        "format_amount_for_portal",
    ):
        assert label_name in target_text, f"{label_name} missing from scaffolded file"


# ---- _parse_assessment_year (argparse type) -------------------------------


def test_parse_assessment_year_short_form() -> None:
    assert rollover_portal_labels._parse_assessment_year("2026-27") == 2026


def test_parse_assessment_year_long_form() -> None:
    assert rollover_portal_labels._parse_assessment_year("2026-2027") == 2026


def test_parse_assessment_year_rejects_short_form_mismatch() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="does not match"):
        rollover_portal_labels._parse_assessment_year("2026-28")


def test_parse_assessment_year_rejects_long_form_mismatch() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="does not equal"):
        rollover_portal_labels._parse_assessment_year("2026-2028")


def test_parse_assessment_year_rejects_bad_format() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="must be in"):
        rollover_portal_labels._parse_assessment_year("AY2026")


# ---- main() ----------------------------------------------------------------


def test_main_scaffolds_into_package_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: ``--from-ay 2026-27 --to-ay 2027-28`` writes into PACKAGE_DIR."""
    _write_minimal_source_snapshot(tmp_path, from_ay=2026)
    monkeypatch.setattr(rollover_portal_labels, "PACKAGE_DIR", tmp_path)

    exit_code = rollover_portal_labels.main(["--from-ay", "2026-27", "--to-ay", "2027-28"])

    assert exit_code == 0
    assert (tmp_path / "_ay_2027_28.py").exists()
    out = capsys.readouterr().out
    assert "scaffolded for AY 2027-28" in out
    assert "_ay_2027_28.py" in out
    assert "Next steps" in out


def test_main_refusal_exits_1_with_one_line_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A domain refusal (here: target exists, no --force) exits 1, file untouched."""
    _write_minimal_source_snapshot(tmp_path, from_ay=2026)
    (tmp_path / "_ay_2027_28.py").write_text("# in-progress", encoding="utf-8")
    monkeypatch.setattr(rollover_portal_labels, "PACKAGE_DIR", tmp_path)

    exit_code = rollover_portal_labels.main(["--from-ay", "2026-27", "--to-ay", "2027-28"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "already exists" in err
    assert (tmp_path / "_ay_2027_28.py").read_text(encoding="utf-8") == "# in-progress"


def test_main_rejects_invalid_ay_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unparseable ``--from-ay`` value exits via argparse (code 2)."""
    monkeypatch.setattr(rollover_portal_labels, "PACKAGE_DIR", tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        rollover_portal_labels.main(["--from-ay", "garbage", "--to-ay", "2027-28"])

    assert excinfo.value.code == 2
