"""Tests for :mod:`ibkr_fa.output` registry and :mod:`ibkr_fa.output.base`."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from ibkr_fa import cli, output, pipeline
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.fa_a3_row import FaA3Row
from ibkr_fa.output import (
    _CATALOG,
    _REGISTRY,
    OutputCatalogError,
    Subcommand,
    UnknownOutputFormatError,
    available_formats,
    output_binding,
    register,
)
from ibkr_fa.output.base import (
    Artifact,
    Destination,
    FaA3Writer,
    OutputBinding,
    OutputKey,
    OutputWriter,
    RenderContext,
)
from ibkr_fa.output.bundle import spec_for
from ibkr_fa.output.cbdt_json_fragment import CbdtScheduleFaJsonWriter
from ibkr_fa.output.filing_csv import (
    FA_UPLOAD,
    FILING_WORKSHEET,
    FORM67_UPLOAD,
    PortalCsv,
    PortalCsvWriter,
)
from ibkr_fa.output.filing_xlsx import FilingXlsx, FilingXlsxWriter
from ibkr_fa.output.netbenefits_quicko_fa_xlsx import QuickoFaXlsxWriter
from ibkr_fa.output.portal_labels import _SNAPSHOTS_BY_AY_START, UnknownAssessmentYearError
from ibkr_fa.output.review_csv import ReviewCsvWriter
from ibkr_fa.output.schedules.cg_filing import cg_filing_sheets
from ibkr_fa.output.schedules.fa import a2_upload_rows, a3_upload_rows
from ibkr_fa.output.schedules.form67 import form67_upload_rows
from ibkr_fa.output.schedules.fsi import fsi_filing_rows
from ibkr_fa.output.schedules.os import filing_csv_rows, filing_workbook_sheets
from ibkr_fa.output.schedules.tr import tr_filing_rows
from ibkr_fa.output.xlsx import XlsxWriter
from tests._writer_stubs import StubWriter

pytestmark = pytest.mark.usefixtures("isolated_output_registry")

# Keys no in-tree writer uses: two FA schedule keys (a test lists them in the
# catalog when it looks them up) and a key-only one.
_SPARE_FA_KEY = OutputKey(Destination.WORKBOOK, Artifact.FA_A3_CSV)
_SPARE_FA_KEY_2 = OutputKey(Destination.REVIEW, Artifact.FA_A3_XLSX)
_UNUSED_KEY = OutputKey(Destination.QUICKO_IMPORT, Artifact.OS_CSV)
_CBDT_KEY = OutputKey(Destination.CBDT_JSON, Artifact.FA_A2_A3_JSON)
_QUICKO_KEY = OutputKey(Destination.QUICKO_IMPORT, Artifact.FA_A3_XLSX)
_FA_A3_UPLOAD_KEY = OutputKey(Destination.PORTAL_ENTRY, Artifact.FA_A3_CSV)

# The slots the shared portal renderers serve (#1201, #1202, #1203, #1204, #1205),
# with their targets.
_RENDERED = {
    (Subcommand.FA, "filing-csv"): PortalCsv(a3_upload_rows, FA_UPLOAD),
    (Subcommand.FA_A2, "filing-csv"): PortalCsv(a2_upload_rows, FA_UPLOAD),
    (Subcommand.OS, "filing-csv"): PortalCsv(filing_csv_rows, FILING_WORKSHEET),
    (Subcommand.OS, "filing-xlsx"): FilingXlsx(filing_workbook_sheets),
    (Subcommand.FORM67, "portal-csv"): PortalCsv(form67_upload_rows, FORM67_UPLOAD),
    (Subcommand.FSI, "filing-csv"): PortalCsv(fsi_filing_rows, FILING_WORKSHEET),
    (Subcommand.TR, "filing-csv"): PortalCsv(tr_filing_rows, FILING_WORKSHEET),
    (Subcommand.CG_FILING, "filing-xlsx"): FilingXlsx(cg_filing_sheets),
}

# Registration checks the same rules for a schedule writer and a key-only one.
_registration_keys = pytest.mark.parametrize(
    "key", [_SPARE_FA_KEY, _UNUSED_KEY], ids=["schedule", "key-only"]
)


def _list_fa(format_id: str, key: OutputKey) -> None:
    """Bind the schedule-fa ``format_id`` to ``key`` and its registered writer."""
    _CATALOG[(Subcommand.FA, format_id)] = OutputBinding(key, cast(Any, _REGISTRY.get(key)))


def test_register_and_get_writer_roundtrip() -> None:
    @register(_SPARE_FA_KEY)
    class _Dummy(StubWriter):
        format_id: ClassVar[str] = "test-roundtrip"

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    _list_fa("test-roundtrip", _SPARE_FA_KEY)
    assert output_binding(Subcommand.FA, "test-roundtrip").target is _Dummy


@_registration_keys
def test_register_returns_class_unchanged(key: OutputKey) -> None:
    class _Dummy(StubWriter):
        format_id: ClassVar[str] = "test-return"

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    assert register(key)(_Dummy) is _Dummy


@_registration_keys
def test_register_rejects_missing_format_id(key: OutputKey) -> None:
    class _Bad:
        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    with pytest.raises(TypeError, match="must declare a string"):
        register(key)(_Bad)
    assert key not in _REGISTRY


@_registration_keys
def test_register_rejects_empty_format_id(key: OutputKey) -> None:
    class _Bad:
        format_id: ClassVar[str] = ""

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    with pytest.raises(ValueError, match="does not match the required"):
        register(key)(_Bad)
    assert key not in _REGISTRY


@_registration_keys
def test_register_rejects_non_string_format_id(key: OutputKey) -> None:
    class _Bad:
        format_id: ClassVar[int] = 42

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    with pytest.raises(TypeError, match="must declare a string"):
        register(key)(_Bad)
    assert key not in _REGISTRY


@_registration_keys
@pytest.mark.parametrize(
    "bad_id",
    [
        "Review-CSV",  # uppercase
        "review_csv",  # underscore
        "review csv",  # space
        "-review",  # leading hyphen
        "review-",  # trailing hyphen
        "review--csv",  # consecutive hyphens
        "1review",  # leading digit
        "review.csv",  # punctuation
        "review/csv",  # slash
        "rëview",  # non-ASCII
    ],
)
def test_register_rejects_non_kebab_case_format_id(key: OutputKey, bad_id: str) -> None:
    class _Bad:
        format_id: ClassVar[str] = bad_id

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    with pytest.raises(ValueError, match="does not match the required"):
        register(key)(_Bad)
    assert key not in _REGISTRY


@_registration_keys
@pytest.mark.parametrize(
    "good_id",
    [
        "a",  # minimal
        "test-csv",  # canonical (note: avoid the live "review-csv")
        "cbdt-json",  # multi-segment
        "ay-2027-28",  # digits inside segments
        "writer1",  # trailing digit, no hyphen
        "writer1-foo2",  # digits across segments
    ],
)
def test_register_accepts_valid_kebab_case_format_id(key: OutputKey, good_id: str) -> None:
    class _Good(StubWriter):
        format_id: ClassVar[str] = good_id

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    register(key)(_Good)
    assert _REGISTRY[key] is _Good


@_registration_keys
def test_register_rejects_duplicate_key(key: OutputKey) -> None:
    @register(key)
    class _First(StubWriter):
        format_id: ClassVar[str] = "test-dup"

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    class _Second(StubWriter):
        format_id: ClassVar[str] = "test-dup"

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    with pytest.raises(ValueError, match="already registered to"):
        register(key)(_Second)
    # The first registration must still be intact.
    assert _REGISTRY[key] is _First


def test_get_writer_unknown_raises_with_listing() -> None:
    @register(_SPARE_FA_KEY)
    class _A(StubWriter):
        format_id: ClassVar[str] = "test-a"

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    @register(_SPARE_FA_KEY_2)
    class _B(StubWriter):
        format_id: ClassVar[str] = "test-b"

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    _list_fa("test-a", _SPARE_FA_KEY)
    _list_fa("test-b", _SPARE_FA_KEY_2)
    with pytest.raises(UnknownOutputFormatError) as exc:
        output_binding(Subcommand.FA, "not-a-format")
    assert exc.value.format_id == "not-a-format"
    assert "test-a" in str(exc.value)
    assert "test-b" in str(exc.value)


def test_unknown_output_format_error_with_empty_registry() -> None:
    err = UnknownOutputFormatError("anything", [])
    assert "(none)" in str(err)
    assert err.format_id == "anything"
    assert err.available == []


def test_available_formats_sorted() -> None:
    @register(_SPARE_FA_KEY)
    class _Z(StubWriter):
        format_id: ClassVar[str] = "test-zeta"

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    @register(_SPARE_FA_KEY_2)
    class _A(StubWriter):
        format_id: ClassVar[str] = "test-alpha"

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    _list_fa("test-zeta", _SPARE_FA_KEY)
    _list_fa("test-alpha", _SPARE_FA_KEY_2)
    formats = available_formats(Subcommand.FA)
    a_idx = formats.index("test-alpha")
    z_idx = formats.index("test-zeta")
    assert a_idx < z_idx


def test_protocol_satisfied_by_minimal_class() -> None:
    """Statically conforming class is accepted by register and the
    Protocol type. Runtime-only verification — the real check is at
    type-check time via ty.
    """

    class _Minimal(StubWriter):
        format_id: ClassVar[str] = "test-protocol"

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    register(_SPARE_FA_KEY)(_Minimal)
    _list_fa("test-protocol", _SPARE_FA_KEY)
    target = output_binding(Subcommand.FA, "test-protocol").target
    assert not isinstance(target, PortalCsv | FilingXlsx)
    looked_up: type[FaA3Writer] = target
    assert looked_up is _Minimal


# ---- #156: generic OutputWriter[T] / unified registry API -----------------


def test_subcommand_enum_values_match_cli_subcommand_names() -> None:
    """The Subcommand enum's .value is the literal CLI subcommand
    name (used as the human-meaningful catalog slot and surfaced
    in UnknownOutputFormatError). #156.
    """

    assert Subcommand.FA.value == "schedule-fa"
    assert Subcommand.OS.value == "schedule-os"
    assert Subcommand.CG.value == "schedule-cg"
    assert Subcommand.CG_LTCG.value == "schedule-cg-ltcg"


def test_generic_available_formats_returns_sorted_per_subcommand_slot() -> None:
    """available_formats(Subcommand.X) lists the catalog's X slots, sorted."""

    assert available_formats(Subcommand.FA) == ["filing-csv", "review-csv", "xlsx"]
    for subcommand in Subcommand:
        listed = sorted(fmt for sc, fmt in _CATALOG if sc is subcommand)
        assert available_formats(subcommand) == listed


def test_generic_get_writer_raises_unknown_output_format() -> None:
    """get_writer raises UnknownOutputFormatError with the slot's
    available-formats list.
    """

    with pytest.raises(UnknownOutputFormatError) as ei:
        output_binding(Subcommand.OS, "nope")
    assert "'nope'" in str(ei.value)
    assert ei.value.available == ["filing-csv", "filing-xlsx", "review-csv", "xlsx"]


def test_format_id_uniqueness_scoped_per_key() -> None:
    """The same format_id may be registered under two keys (each key's
    uniqueness is independent). Registering a second writer under the
    same key raises ValueError.
    """

    @register(_SPARE_FA_KEY)
    class _FaA(StubWriter):
        format_id: ClassVar[str] = "test-cross-sc"

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    @register(OutputKey(Destination.WORKBOOK, Artifact.OS_CSV))
    class _OsA(StubWriter):
        format_id: ClassVar[str] = "test-cross-sc"

        def write(self, result: object, path: Path) -> None:
            del result, path

    # Same format_id, different key: both register cleanly.
    assert _REGISTRY[_SPARE_FA_KEY] is _FaA
    # Re-registering under the SAME key raises.
    with pytest.raises(ValueError, match="already registered"):

        @register(_SPARE_FA_KEY)
        class _FaDup(StubWriter):
            format_id: ClassVar[str] = "test-cross-sc"

            def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
                del rows, path


_RETIRED_SUFFIXES = ("fa", "fa_a2", "os", "cg", "cg_ltcg", "cg_filing", "fsi", "tr", "form67")
_RETIRED_NAMES = (
    *(f"register_{suffix}" for suffix in _RETIRED_SUFFIXES),
    *(f"get_writer_{suffix}" for suffix in _RETIRED_SUFFIXES),
    *(f"available_{suffix}_formats" for suffix in _RETIRED_SUFFIXES if suffix != "cg_filing"),
    *(f"_{suffix.upper()}_REGISTRY" for suffix in _RETIRED_SUFFIXES if suffix != "cg_filing"),
    "register_key",
    "get_writer",
    "output_key",
    "writer_for",
    "_KEY_ONLY_REGISTRY",
    "_OUTPUT_KEYS",
    "_SLOT_BY_KEY",
)


@pytest.mark.parametrize("name", _RETIRED_NAMES)
def test_retired_registry_names_are_gone(name: str) -> None:
    """#790 (#644 owner decision 2): one key per writer, so the per-subcommand
    decorators, lookups and listings, the ``Subcommand``-keyed aliases and the
    key-only side store are gone, from the package and the pipeline re-exports."""
    assert not hasattr(output, name)
    assert name not in output.__all__
    assert not hasattr(pipeline, name)


def test_register_rejects_class_without_write_method() -> None:
    """Defense-in-depth (T3-r1 P2): register() rejects any class lacking a
    callable write attribute, so a malformed registration fails at
    decoration time rather than at first writer.write() call.
    """

    with pytest.raises(TypeError, match="callable 'write'"):

        @register(_SPARE_FA_KEY)
        class _NoWrite:
            format_id: ClassVar[str] = "test-no-write-method"
            # deliberately missing write()


def test_register_rejects_class_with_non_callable_write_attribute() -> None:
    """Same guard, different failure mode: a class with a 'write'
    attribute that isn't callable (e.g., a string typo'd in place
    of a method) fails at decoration time.
    """

    with pytest.raises(TypeError, match="callable 'write'"):

        @register(_SPARE_FA_KEY)
        class _StringWrite:
            format_id: ClassVar[str] = "test-string-write"
            write = "not a method"  # type: ignore[assignment]


def test_subcommand_enum_values_match_cli_subparser_names() -> None:
    """Anti-drift lock (T3-r1 P2): the Subcommand enum values must
    stay in sync with the writer-bearing CLI subparser literals. If
    someone renames one side (e.g., adds a `-v2` suffix to a
    subparser) without updating the other, the registry lookup
    silently fails to find the writer.

    Parse the CLI argparser at runtime and assert every Subcommand
    value appears as a registered subparser name, and vice versa.
    Subparsers that do NOT produce a per-compute-result output file
    (e.g., ``init``, which scaffolds a starter user.toml and has no
    writer registry) are listed in
    :data:`_NON_WRITER_SUBPARSERS` and excluded from the comparison.
    """
    from ibkr_fa.cli import _build_parser  # noqa: PLC0415  — internal API

    # Subparsers that intentionally have no entry in the Subcommand
    # enum because they do not consume a compute layer's output:
    #
    #   * run-all: one-shot orchestrator that re-runs the other
    #     subcommands' handlers; it has no writer / output format of
    #     its own (each schedule's own Subcommand entry covers it).
    #   * init: scaffolds a starter user.toml; writes a config file,
    #     not a per-compute-result output artifact.
    #   * merge (#39): splices a Schedule FA JSON fragment into a saved
    #     ITR-2 return; it transforms two JSON files, not a compute result.
    #   * netbenefits-fa (#313): emits a Schedule FA Table A3 review CSV for
    #     the Fidelity ESPP holding, REUSING the schedule-fa FA A3 writer
    #     registry (Subcommand.FA) — it owns no writer registry of its own.
    #   * netbenefits-cg (#357): emits Schedule CG STCG + LTCG review CSVs for
    #     sold Fidelity ESPP lots, REUSING the schedule-cg / schedule-cg-ltcg
    #     writers (Subcommand.CG / Subcommand.CG_LTCG) — owns none of its own.
    #   * netbenefits-all (#355): one-shot orchestrator that re-runs the three
    #     netbenefits-* handlers; owns no writer registry of its own (like run-all).
    #   * stockplan-income (#365): emits Schedule OS/FSI/TR/Form 67 review CSVs for
    #     a Morgan Stanley StockPlan (GSU) dividend, REUSING the schedule-os / fsi /
    #     tr / form67 writers (like netbenefits-income) — owns none of its own.
    #   * fidelity-cma-income (#388): emits the same four review CSVs for a
    #     Fidelity CMA, reusing their existing writer registries.
    #   * fidelity-cma-cg (#389): reuses the Schedule CG writer registries.
    #   * fidelity-cma-fa (#393): reuses the Schedule FA A2/A3 writer registries.
    _non_writer_subparsers = {
        "run-all",
        "init",
        "merge",
        "netbenefits-fa",
        "netbenefits-income",
        "netbenefits-cg",
        "netbenefits-all",
        "stockplan-income",
        "stockplan-fa",
        "stockplan-all",
        "fidelity-cma-income",
        "fidelity-cma-cg",
        "fidelity-cma-fa",
        "robinhood-cg",
        "robinhood-income",
        "robinhood-fa",
        "robinhood-options",
        "robinhood-all",
    }

    # Subcommand-enum values that intentionally have no CLI subparser
    # YET because their writers + registry land in one PR and the
    # CLI subparser lands in a follow-up PR (keeps each PR scope small).
    # PR #213 added the schedule-fsi subparser (#35 step 6/7); PR #218
    # added the schedule-tr subparser (#36 step 4/N) alongside their
    # respective pipeline orchestrators. PR #226 added the form-67
    # Subcommand + writer + registry; the form-67 CLI subparser
    # subsequently shipped in PR (post-#226 ship) step 3/N of #37 —
    # this set is therefore empty post-#37-step-3.
    _pending_cli_subparsers: set[str] = set()

    # Subcommand-enum values that are registry-only BY DESIGN: they key a
    # writer registry but are emitted as a SIBLING file by another
    # subcommand, so they never get their own CLI subparser. Schedule FA
    # Table A2 (schedule-fa-a2) is written alongside Table A3 by the
    # `schedule-fa` command (the IBKR custodial account itself, #18). The
    # Schedule CG filing workbook (schedule-cg-filing) is written as
    # schedule-cg.xlsx by a source's CG command, today `robinhood-cg` (#642).
    _registry_only_subcommands = {"schedule-fa-a2", "schedule-cg-filing"}

    parser = _build_parser()
    # The subparsers action lives on the top-level parser; find it.
    subparsers_action = next(
        a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"
    )
    # argparse's _SubParsersAction.choices is Optional[dict] per the
    # stubs but is always populated once subparsers have been added.
    assert subparsers_action.choices is not None
    parser_names = set(subparsers_action.choices) - _non_writer_subparsers
    enum_values = (
        {sc.value for sc in Subcommand} - _pending_cli_subparsers - _registry_only_subcommands
    )
    assert parser_names == enum_values, (
        f"Subcommand <-> CLI parser drift: "
        f"enum={enum_values}, parsers={parser_names}, "
        f"missing-in-cli={enum_values - parser_names}, "
        f"missing-in-enum={parser_names - enum_values}"
    )


@pytest.mark.parametrize("command", ["schedule-cg", "schedule-cg-ltcg", "robinhood-cg"])
def test_cg_commands_do_not_offer_the_filing_workbook_format(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The filing workbook has its own catalog slot, not a CG ``--format`` choice (#642)."""
    from ibkr_fa.cli import _build_parser  # noqa: PLC0415  — internal API

    with pytest.raises(SystemExit):
        _build_parser().parse_args([command, "--format", "filing-xlsx"])

    assert f"argument --format: invalid value; run 'ibkr-fa {command} --help'" in (
        capsys.readouterr().err
    )


# --- Destination / artifact keys and RenderContext (#644) ---------------------


def _registered_schedule_writers() -> dict[tuple[Subcommand, str], type[OutputWriter[Any]]]:
    return {
        slot: _REGISTRY[binding.key] for slot, binding in _CATALOG.items() if slot not in _RENDERED
    }


def test_output_keys_cover_every_registered_schedule_writer_one_to_one() -> None:
    """The catalog is total over the in-tree schedule writers (16, beside the
    eight renderer slots) and one-to-one; every other registered key is
    key-only."""
    registered = _registered_schedule_writers()
    keys = {binding.key for binding in _CATALOG.values()}
    assert len(registered) == 16
    assert len(_CATALOG) == 24
    assert len(keys) == len(_CATALOG)
    assert set(_REGISTRY) - keys == {_CBDT_KEY, _QUICKO_KEY}
    for (_subcommand, format_id), writer in registered.items():
        assert writer.format_id == format_id


def test_output_binding_resolves_every_slot_to_its_registered_writer() -> None:
    """Each slot's binding targets the writer registered under its key (#1200),
    except the FA uploads, the OS filing CSV and workbook, the Form 67 upload,
    the FSI and TR filing worksheets and the CG filing workbook, which the shared
    portal renderers serve with no registered writer (#1201, #1202, #1203,
    #1204, #1205)."""
    for (subcommand, format_id), binding in _CATALOG.items():
        assert output_binding(subcommand, format_id) is binding
        if (subcommand, format_id) in _RENDERED:
            assert binding.target == _RENDERED[subcommand, format_id]
            assert binding.key not in _REGISTRY
        else:
            assert binding.target is _REGISTRY[binding.key]
    assert _REGISTRY[_CBDT_KEY] is CbdtScheduleFaJsonWriter
    assert _REGISTRY[_QUICKO_KEY] is QuickoFaXlsxWriter


def test_os_filing_csv_and_xlsx_have_different_keys() -> None:
    assert (
        output_binding(Subcommand.OS, "filing-csv").key
        != output_binding(Subcommand.OS, "filing-xlsx").key
    )


def test_output_binding_for_an_unmapped_format_refuses() -> None:
    with pytest.raises(UnknownOutputFormatError) as exc:
        output_binding(Subcommand.FA, "cbdt-json-fragment")
    assert exc.value.available == ["filing-csv", "review-csv", "xlsx"]


@pytest.mark.parametrize(
    ("destination", "artifact"),
    [
        ("cbdt-json", Artifact.OS_CSV),
        ([], Artifact.OS_CSV),
        (Destination.CBDT_JSON, "schedule-os-csv"),
        (Destination.CBDT_JSON, []),
    ],
)
def test_output_key_rejects_malformed_members(destination: Any, artifact: Any) -> None:
    with pytest.raises(InvariantError, match="OutputKey requires Destination and Artifact"):
        OutputKey(destination, artifact)


@pytest.mark.parametrize(
    ("subcommand", "format_id"),
    [(None, "filing-csv"), ([], "filing-csv"), (Subcommand.FA, []), (Subcommand.FA, None)],
)
def test_output_binding_rejects_malformed_lookup_arguments(subcommand: Any, format_id: Any) -> None:
    with pytest.raises(InvariantError, match="output_binding requires a Subcommand and string"):
        output_binding(subcommand, format_id)


def _key_bypassing_checks(destination: object, artifact: object = Artifact.OS_CSV) -> OutputKey:
    """An OutputKey whose components skipped ``__post_init__`` (a corrupted key)."""
    key = object.__new__(OutputKey)
    object.__setattr__(key, "destination", destination)
    object.__setattr__(key, "artifact", artifact)
    return key


_MALFORMED_KEYS = [
    pytest.param(None, id="none"),
    pytest.param([], id="list"),
    pytest.param("cbdt-json/schedule-os-csv", id="str"),
    pytest.param(_key_bypassing_checks("cbdt-json"), id="str-destination"),
    pytest.param(_key_bypassing_checks([]), id="unhashable-destination"),
    pytest.param(_key_bypassing_checks(Destination.REVIEW, []), id="unhashable-artifact"),
]


@pytest.mark.parametrize("key", _MALFORMED_KEYS)
def test_register_rejects_malformed_keys(key: Any) -> None:
    """A malformed key is refused before it is hashed or its fields read (#790)."""
    before = dict(_REGISTRY)
    with pytest.raises(InvariantError, match="register requires an OutputKey"):
        register(key)
    assert before == _REGISTRY


@pytest.mark.parametrize(
    "lookup",
    [
        pytest.param(lambda: output_binding(cast(Any, None), "review-csv"), id="binding-none"),
        pytest.param(
            lambda: output_binding(cast(Any, "schedule-fa"), "review-csv"), id="binding-str"
        ),
        pytest.param(
            lambda: output_binding(Subcommand.FA, cast(Any, ["review-csv"])), id="binding-list"
        ),
        pytest.param(lambda: available_formats(cast(Any, "schedule-fa")), id="formats-str"),
        pytest.param(lambda: available_formats(cast(Any, None)), id="formats-none"),
    ],
)
def test_generic_readers_refuse_malformed_arguments(lookup: Callable[[], object]) -> None:
    """Not a raw KeyError / TypeError from indexing the store (#790)."""
    with pytest.raises(InvariantError, match="requires a Subcommand"):
        lookup()


# --- Catalog integrity (#790) ---------------------------------------------------


def _register_unlisted_schedule_writer() -> None:
    @register(_SPARE_FA_KEY)
    class _Unlisted(StubWriter):
        format_id: ClassVar[str] = "test-unlisted"

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path


def _move(slot: object, to: object) -> None:
    cast(Any, _CATALOG)[to] = _CATALOG.pop(cast(Any, slot))


def _swap_review_csv_targets() -> None:
    fa, os_ = (Subcommand.FA, "review-csv"), (Subcommand.OS, "review-csv")
    _CATALOG[fa], _CATALOG[os_] = _CATALOG[os_], _CATALOG[fa]


def _set_binding(binding: object) -> None:
    cast(Any, _CATALOG)[(Subcommand.FA, "review-csv")] = binding


def _set_key(key: object) -> None:
    _set_binding(OutputBinding(cast(Any, key), ReviewCsvWriter))


def _register_a_writer_under_a_renderer_key() -> None:
    @register(_FA_A3_UPLOAD_KEY)
    class _Shadow(StubWriter):
        format_id: ClassVar[str] = "filing-csv"

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path


_NOT_A_PAIR = r"is not a \(Subcommand, format_id\) pair"

_CORRUPTIONS: dict[str, tuple[Callable[[], None], str]] = {
    "slot-none": (
        lambda: _move((Subcommand.FA, "review-csv"), None),
        _NOT_A_PAIR,
    ),
    "slot-empty": (
        lambda: _move((Subcommand.FA, "review-csv"), ()),
        _NOT_A_PAIR,
    ),
    "slot-one": (
        lambda: _move((Subcommand.FA, "review-csv"), (Subcommand.FA,)),
        _NOT_A_PAIR,
    ),
    "slot-str-subcommand": (
        lambda: _move((Subcommand.FA, "review-csv"), ("schedule-fa", "review-csv")),
        "not a Subcommand",
    ),
    "slot-int-format": (
        lambda: _move((Subcommand.FA, "review-csv"), (Subcommand.FA, 1)),
        "format_id is not a string",
    ),
    "binding-none": (lambda: _set_binding(None), "not an OutputBinding"),
    "binding-bare-key": (
        lambda: _set_binding(_CATALOG[(Subcommand.FA, "review-csv")].key),
        "not an OutputBinding",
    ),
    "binding-without-fields": (
        lambda: _set_binding(object.__new__(OutputBinding)),
        "not an OutputKey",
    ),
    "key-none": (lambda: _set_key(None), "not an OutputKey"),
    "key-str": (lambda: _set_key("review/schedule-fa-a3-csv"), "not an OutputKey"),
    "key-bad-component": (
        lambda: _set_key(_key_bypassing_checks("review", Artifact.FA_A3_CSV)),
        "not an OutputKey",
    ),
    "key-unhashable-component": (
        lambda: _set_key(_key_bypassing_checks([], Artifact.FA_A3_CSV)),
        "not an OutputKey",
    ),
    "duplicate-target": (
        lambda: _CATALOG.__setitem__(
            (Subcommand.FORM67, "filing-csv"), _CATALOG[(Subcommand.OS, "filing-csv")]
        ),
        "both list",
    ),
    "moved-to-another-slot": (
        lambda: _move((Subcommand.OS, "filing-csv"), (Subcommand.FORM67, "filing-csv")),
        "not a form-67 table",
    ),
    "review-csv-swap": (_swap_review_csv_targets, "not a schedule-fa table"),
    "key-only-target": (
        lambda: _CATALOG.__setitem__(
            (Subcommand.FA, QuickoFaXlsxWriter.format_id),
            OutputBinding(_QUICKO_KEY, QuickoFaXlsxWriter),
        ),
        "key-only",
    ),
    "missing-writer": (lambda: _list_fa("test-unwritten", _SPARE_FA_KEY), "no writer"),
    "unregistered-target": (
        lambda: _set_binding(
            OutputBinding(_CATALOG[(Subcommand.FA, "review-csv")].key, XlsxWriter)
        ),
        "its registered writer is ReviewCsvWriter",
    ),
    "format-id-mismatch": (
        lambda: _move((Subcommand.FA, "review-csv"), (Subcommand.FA, "test-renamed")),
        "declares format_id 'review-csv'",
    ),
    "unlisted-schedule-writer": (_register_unlisted_schedule_writer, "no catalog entry"),
    "writer-under-a-renderer-key": (
        _register_a_writer_under_a_renderer_key,
        r"binds portal-entry/schedule-fa-a3-csv to the wrong target; its registered writer is "
        r"\S+_Shadow",
    ),
    "store-bad-key": (
        lambda: cast(Any, _REGISTRY).__setitem__("review/schedule-fa-a3-csv", StubWriter),
        "not an OutputKey",
    ),
}

_ACCESSORS: dict[str, Callable[[], object]] = {
    "output-binding": lambda: output_binding(Subcommand.TR, "xlsx"),
    "available-formats": lambda: available_formats(Subcommand.TR),
    "spec-for": lambda: spec_for(Subcommand.TR, "xlsx", None, Path("tr.xlsx")),
}


@pytest.mark.parametrize("corruption", _CORRUPTIONS)
@pytest.mark.parametrize("accessor", _ACCESSORS)
@pytest.mark.parametrize("looked_up_before", [False, True], ids=["first-access", "after-lookup"])
def test_every_lookup_refuses_an_inconsistent_catalog(
    corruption: str, accessor: str, *, looked_up_before: bool
) -> None:
    """Every lookup and listing checks the catalog against the registered
    writers first, so a bad state introduced after a successful lookup is
    refused too, with a typed error rather than a silent drop (#790)."""
    if looked_up_before:
        assert _ACCESSORS[accessor]() is not None
    corrupt, message = _CORRUPTIONS[corruption]
    corrupt()
    with pytest.raises(OutputCatalogError, match=message):
        _ACCESSORS[accessor]()


@pytest.mark.parametrize("as_target", [False, True], ids=["binding", "target"])
def test_binding_errors_do_not_echo_unchecked_values(*, as_target: bool) -> None:
    marker = "SYNTHETIC_INPUT_MARKER"
    bad_binding: Any = marker
    if as_target:
        bad_binding = replace(_CATALOG[(Subcommand.FA, "review-csv")], target=cast(Any, marker))
    _set_binding(bad_binding)

    with pytest.raises(
        OutputCatalogError, match=r"not an OutputBinding|its registered writer"
    ) as exc:
        output_binding(Subcommand.FA, "review-csv")

    assert marker not in str(exc.value)


def test_cli_refuses_an_inconsistent_catalog_before_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The parser lists formats from the catalog, so a bad catalog stops the
    CLI with exit 4 and its diagnostic, before any run starts (#790)."""
    del _CATALOG[(Subcommand.OS, "xlsx")]

    def _never(**_kwargs: object) -> None:
        raise AssertionError("schedule-os must not run")

    monkeypatch.setattr(cli, "run_schedule_os", _never)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    argv = ["schedule-os", "--input", str(tmp_path / "in.csv"), "--config", str(tmp_path / "u")]
    argv += ["--output", str(out_dir / "schedule-os.csv"), "--financial-year", "2025-26"]

    assert cli.main(argv) == 4

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "workbook/schedule-os-xlsx" in captured.err
    assert "no catalog entry" in captured.err
    assert list(out_dir.iterdir()) == []


def test_register_rejects_class_without_from_context() -> None:
    """#644 owner decision 1: every writer is built through from_context, so
    one without it is refused at registration, not built with current-AY labels."""

    class _NoFromContext:
        format_id: ClassVar[str] = "test-no-from-context"

        def __init__(self) -> None:
            pass

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    with pytest.raises(TypeError, match="callable 'from_context'"):
        register(_SPARE_FA_KEY)(_NoFromContext)
    assert _SPARE_FA_KEY not in _REGISTRY


@pytest.mark.parametrize(
    "factory",
    ["not a method", lambda self, ctx: self, staticmethod(lambda ctx: None)],
    ids=["non-callable", "instance-method", "static-method"],
)
def test_register_rejects_invalid_from_context(factory: Any) -> None:
    class _InvalidFromContext:
        format_id: ClassVar[str] = "test-string-from-context"
        from_context = factory

        def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
            del rows, path

    with pytest.raises(TypeError, match="callable 'from_context'"):
        register(_SPARE_FA_KEY)(_InvalidFromContext)
    assert _SPARE_FA_KEY not in _REGISTRY


def test_register_key_only_writer_registers_without_a_catalog_entry() -> None:
    @register(_UNUSED_KEY)
    class _KeyOnly(StubWriter):
        format_id: ClassVar[str] = "test-key-only"

        def write(self, value: object, path: Path) -> None:
            del value, path

    assert _REGISTRY[_UNUSED_KEY] is _KeyOnly
    assert "test-key-only" not in available_formats(Subcommand.OS)


def test_register_rejects_a_duplicate_key_only_key() -> None:
    class _Again(StubWriter):
        format_id: ClassVar[str] = "test-again"

        def write(self, value: object, path: Path) -> None:
            del value, path

    with pytest.raises(ValueError, match="already registered to CbdtScheduleFaJsonWriter"):
        register(_CBDT_KEY)(_Again)


def test_register_rejects_a_schedule_writers_key() -> None:
    class _Clash(StubWriter):
        format_id: ClassVar[str] = "test-clash"

        def write(self, value: object, path: Path) -> None:
            del value, path

    with pytest.raises(ValueError, match="already registered to ScheduleOsReviewCsvWriter"):
        register(output_binding(Subcommand.OS, "review-csv").key)(_Clash)


@pytest.mark.parametrize(
    ("attrs", "error", "match"),
    [
        ({"format_id": "Not_Kebab"}, ValueError, "kebab-case"),
        ({"format_id": None}, TypeError, "string class attribute 'format_id'"),
        ({"format_id": "test-no-write", "write": None}, TypeError, "callable 'write'"),
        ({"format_id": "test-no-ctx", "from_context": None}, TypeError, "callable 'from_context'"),
        (
            {"from_context": lambda self, ctx: self},
            TypeError,
            "callable 'from_context'",
        ),
        (
            {"from_context": staticmethod(lambda ctx: None)},
            TypeError,
            "callable 'from_context'",
        ),
    ],
    ids=["kebab", "missing-format-id", "write", "from-context", "instance-method", "static-method"],
)
def test_register_checks_a_key_only_writers_shape(
    attrs: dict[str, object], error: type[Exception], match: str
) -> None:
    namespace: dict[str, object] = {
        "format_id": "test-malformed",
        "from_context": classmethod(lambda cls, _ctx: cls()),
        "write": lambda _self, _value, _path: None,
        **attrs,
    }
    writer = type("_Malformed", (), {k: v for k, v in namespace.items() if v is not None})
    with pytest.raises(error, match=match):
        register(_UNUSED_KEY)(writer)
    assert _UNUSED_KEY not in _REGISTRY


def _shim_kwargs(writer: type, ctx: RenderContext) -> dict[str, object]:
    """The constructor kwargs the deleted ``_construct_writer_with_optional_ay``
    passed: each non-``None`` context field the constructor names."""
    candidates = {
        "assessment_year_start": ctx.assessment_year_start,
        "itr_form": ctx.itr_form,
        "source_label": ctx.source_label,
        "account_labels": None if ctx.account_labels is None else dict(ctx.account_labels),
        "include_checks": ctx.debug,
    }
    params = inspect.signature(writer.__init__).parameters
    return {k: v for k, v in candidates.items() if v is not None and k in params}


_IN_TREE_WRITERS = [
    *sorted(_registered_schedule_writers().values(), key=lambda cls: cls.__qualname__),
    CbdtScheduleFaJsonWriter,
    QuickoFaXlsxWriter,
]
_FULL_CONTEXT = RenderContext(
    assessment_year_start=2026,
    itr_form="ITR-3",
    source_label="Test source",
    account_labels={0: "Individual"},
    debug=True,
)


@pytest.mark.parametrize("writer", _IN_TREE_WRITERS, ids=lambda cls: cls.__qualname__)
def test_from_context_builds_what_the_old_shim_built(writer: Any) -> None:
    """Every in-tree writer's from_context passes the fields its constructor
    takes, as the deleted signature-inspection shim did."""
    built = writer.from_context(_FULL_CONTEXT)
    assert type(built) is writer
    assert vars(built) == vars(writer(**_shim_kwargs(writer, _FULL_CONTEXT)))


@pytest.mark.parametrize("writer", _IN_TREE_WRITERS, ids=lambda cls: cls.__qualname__)
def test_from_context_leaves_constructor_defaults_for_none_fields(writer: Any) -> None:
    """A None field keeps the constructor default (ITR-2 headers, no labels)."""
    ctx = RenderContext(assessment_year_start=2027)
    assert vars(writer.from_context(ctx)) == vars(writer(**_shim_kwargs(writer, ctx)))


@pytest.mark.parametrize("missing", ["itr_form", "source_label"])
def test_cg_filing_writer_refuses_a_missing_context_field(missing: str) -> None:
    ctx = replace(_FULL_CONTEXT, **{missing: None})
    binding = output_binding(Subcommand.CG_FILING, "filing-xlsx")
    with pytest.raises(InvariantError, match="needs the run's ITR form and source label"):
        binding.writer(ctx)


def test_cg_filing_writer_needs_no_account_labels() -> None:
    """The workbook names no account since no section is manual (#1383)."""
    binding = output_binding(Subcommand.CG_FILING, "filing-xlsx")
    assert binding.writer(replace(_FULL_CONTEXT, account_labels=None)) is not None


@pytest.mark.parametrize("slot", _RENDERED, ids=lambda slot: f"{slot[0].value} {slot[1]}")
def test_a_renderer_slot_resolves_the_runs_labels_when_its_writer_is_built(
    slot: tuple[Subcommand, str],
) -> None:
    """Building the writer resolves the AY's labels, as the deleted FA upload,
    OS filing, Form 67 upload, FSI and TR filing and CG filing writers'
    from_context did (#1201, #1202, #1203, #1204, #1205): the AY after the
    latest snapshot falls back to it, and a later one refuses before any write
    starts."""
    latest = max(_SNAPSHOTS_BY_AY_START)
    binding = output_binding(*slot)

    built = binding.writer(replace(_FULL_CONTEXT, assessment_year_start=latest + 1))
    assert isinstance(built, PortalCsvWriter | FilingXlsxWriter)
    with pytest.raises(UnknownAssessmentYearError):
        binding.writer(replace(_FULL_CONTEXT, assessment_year_start=latest + 2))
