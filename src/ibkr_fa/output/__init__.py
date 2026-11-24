"""Output-writer registry and public-slot binding catalog.

Architecture (#790, #1200, #1206):

* One store: ``_REGISTRY: dict[OutputKey, type]``. Each writer registers under
  its own ``OutputKey(destination, artifact)`` with the one decorator,
  :func:`register`, which checks the key, the writer's ``format_id``, its
  ``write`` and ``from_context`` (#644 decision 1), and refuses a second writer
  for a key.
* One catalog, the single support/binding declaration: ``_CATALOG`` maps each
  public CLI ``--format`` slot, ``(Subcommand, format_id)``, to an
  :class:`~ibkr_fa.output.base.OutputBinding`: the key of the file it writes
  and the target that renders it, either the writer registered under that key
  or a shared renderer (#1201, #1202), whose key no writer registers.
  :func:`output_binding` and :func:`available_formats` read it. The CBDT JSON
  fragment and Quicko writers are key-only: their input is not a schedule
  result, so no slot lists them, and their pipelines use the writer classes
  directly.
* Command formats and selections (#1206) extend the same declaration:
  ``_COMMAND_FORMATS`` covers each command with ``--format``, plus
  ``netbenefits-all``: its choices, default and substitutes
  (:func:`command_formats`). ``INCOME_FILING``, ``INCOME_REVIEW``,
  ``CG_FILING_WORKBOOK`` and ``OS_FILING_WORKBOOK`` name the slots workflows
  write whatever ``--format`` says. They choose what a run writes; filenames,
  cleanup targets, promised files, preflights and transaction windows stay in
  each workflow.
* Every lookup and listing first checks the catalog against the store, and
  the command formats and selections against the catalog, and raises
  :class:`~ibkr_fa.output.base.OutputCatalogError` on any disagreement, so a
  writer can't silently drop out of a ``--format`` list or render another
  slot's output.

The store and the catalog are **module-global** by design: there's one set of
output formats per process, and each CLI subcommand's ``--format`` choice list
is derived from the declaration. Writers self-register on import to keep the
add-a-format workflow to a writer module, an import wired in at the bottom of
this module, a catalog entry below it, and the command formats that offer it.

Why explicit decorator-based registration (vs auto-discovery): keeps the import
graph obvious, avoids surprise side-effects, and lets ty check that each
registered class satisfies :class:`~ibkr_fa.output.base.OutputWriter`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Final, NamedTuple

from ibkr_fa.invariants import require
from ibkr_fa.output.base import (
    Artifact,
    Destination,
    FaA2Writer,
    FaA3Writer,
    Form67Writer,
    OutputBinding,
    OutputCatalogError,
    OutputKey,
    OutputWriter,
    RenderContext,
    ScheduleCgLtcgWriter,
    ScheduleCgWriter,
    ScheduleFsiWriter,
    ScheduleOsWriter,
    ScheduleTrWriter,
    Subcommand,
    UnknownOutputFormatError,
)

# The writer store: one writer class per key.
_REGISTRY: dict[OutputKey, type[OutputWriter[Any]]] = {}

# Destinations whose input is not a schedule result: their writers are
# registered by key only, and no catalog slot may list them.
_KEY_ONLY_DESTINATIONS: Final = frozenset({Destination.CBDT_JSON, Destination.QUICKO_IMPORT})

# The schedule table each artifact holds, so a catalog slot can't list another
# schedule's key. The combined CBDT FA fragment holds two tables, so none.
_SCHEDULE_OF: Final[Mapping[Artifact, Subcommand]] = {
    Artifact.FA_A2_CSV: Subcommand.FA_A2,
    Artifact.FA_A2_XLSX: Subcommand.FA_A2,
    Artifact.FA_A3_CSV: Subcommand.FA,
    Artifact.FA_A3_XLSX: Subcommand.FA,
    Artifact.OS_CSV: Subcommand.OS,
    Artifact.OS_XLSX: Subcommand.OS,
    Artifact.CG_STCG_CSV: Subcommand.CG,
    Artifact.CG_STCG_XLSX: Subcommand.CG,
    Artifact.CG_LTCG_CSV: Subcommand.CG_LTCG,
    Artifact.CG_LTCG_XLSX: Subcommand.CG_LTCG,
    Artifact.CG_XLSX: Subcommand.CG_FILING,
    Artifact.FSI_CSV: Subcommand.FSI,
    Artifact.FSI_XLSX: Subcommand.FSI,
    Artifact.TR_CSV: Subcommand.TR,
    Artifact.TR_XLSX: Subcommand.TR,
    Artifact.FORM67_CSV: Subcommand.FORM67,
    Artifact.FORM67_XLSX: Subcommand.FORM67,
}

# Lowercase kebab-case: starts with a letter, then letter/digit, with
# optional hyphen-separated letter/digit groups. Matches the contract
# documented on :attr:`OutputWriter.format_id` and produces strings
# that are safe and readable as a CLI ``--format`` choice value.
_FORMAT_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


def _validate_format_id(cls: type, format_id_attr: object) -> str:
    """Defend against the two ways a writer can mis-declare format_id:
    a non-string attribute or a string that doesn't match the
    lowercase-kebab-case shape required for CLI ``--format`` safety.
    """
    if not isinstance(format_id_attr, str):
        raise TypeError(
            f"Writer class {cls.__qualname__} must declare a string "
            f"class attribute 'format_id'; got {format_id_attr!r}"
        )
    if not _FORMAT_ID_RE.fullmatch(format_id_attr):
        raise ValueError(
            f"Writer class {cls.__qualname__} declares format_id "
            f"{format_id_attr!r}, which does not match the required lowercase "
            f"kebab-case pattern {_FORMAT_ID_RE.pattern!r} (e.g., "
            f"'review-csv', 'cbdt-json'). Pick a value that is safe as "
            f"a CLI --format choice."
        )
    return format_id_attr


def _validate_writer_shape(cls: type) -> None:
    """Defend against a class lacking a callable ``write`` method or
    ``from_context`` classmethod.

    Belt-and-suspenders for the structural :class:`OutputWriter`
    Protocol: :func:`register` can only bind ``OutputWriter[Any]``, and a
    test or out-of-tree writer may not be type-checked at all; this
    runtime guard catches a missing method at decoration time with an
    actionable error rather than at first :meth:`OutputWriter.write`
    call (which would fail with a non-obvious ``AttributeError``).
    """
    write = getattr(cls, "write", None)
    if not callable(write):
        raise TypeError(
            f"Writer class {cls.__qualname__} must define a callable "
            f"'write' method to satisfy the OutputWriter Protocol; "
            f"got {write!r}"
        )
    # #644 owner decision 1: every writer is built through from_context, so a
    # writer without it can't fall back to current-AY labels.
    from_context = getattr(cls, "from_context", None)
    if not callable(from_context) or getattr(from_context, "__self__", None) is not cls:
        raise TypeError(
            f"Writer class {cls.__qualname__} must define a callable "
            f"'from_context' classmethod to satisfy the OutputWriter Protocol; "
            f"got {from_context!r}"
        )


def _valid_key(key: object) -> OutputKey | None:
    """``key`` if it is an OutputKey of enum members, else ``None``; checked
    before it is hashed or its fields read (a key built bypassing
    ``__post_init__`` can hold anything)."""
    if (
        isinstance(key, OutputKey)
        and isinstance(getattr(key, "destination", None), Destination)
        and isinstance(getattr(key, "artifact", None), Artifact)
    ):
        return key
    return None


def _key_name(key: object) -> str:
    """``destination/artifact`` for a valid key; otherwise a description that
    neither hashes ``key`` nor needs its fields."""
    valid = _valid_key(key)
    if valid is not None:
        return f"{valid.destination.value}/{valid.artifact.value}"
    if isinstance(key, OutputKey):
        destination, artifact = getattr(key, "destination", None), getattr(key, "artifact", None)
        return f"OutputKey({destination!r}, {artifact!r})"
    return repr(key)


def register[WriterT: type[OutputWriter[Any]]](key: OutputKey) -> Callable[[WriterT], WriterT]:
    """Return a class decorator that registers a writer under ``key``.

    Usage::

        from ibkr_fa.output import register
        from ibkr_fa.output.base import Artifact, Destination, OutputKey


        @register(OutputKey(Destination.REVIEW, Artifact.FA_A3_CSV))
        class ReviewCsvWriter:
            format_id = "review-csv"

            @classmethod
            def from_context(cls, ctx): ...

            def write(self, rows, path): ...

    A schedule writer also needs its catalog entry in ``_CATALOG``,
    ``(Subcommand, format_id)`` bound to ``OutputBinding(key, writer)``; a
    writer for a key-only destination (CBDT JSON, Quicko) must have none.

    Raises:
        InvariantError: If ``key`` is not an OutputKey of a Destination and an
            Artifact; nothing is registered.
        TypeError: At decoration time, if the decorated class's
            ``format_id`` is missing or not a string, or it has no
            callable ``write`` or ``from_context``.
        ValueError: At decoration time, if ``format_id`` doesn't
            match the lowercase-kebab-case pattern OR another writer is
            already registered under ``key``.
    """
    require(
        _valid_key(key) is not None,
        what="register requires an OutputKey of a Destination and an Artifact",
        key=_key_name(key),
    )

    def decorator(cls: WriterT) -> WriterT:
        _validate_format_id(cls, getattr(cls, "format_id", None))
        _validate_writer_shape(cls)
        existing = _REGISTRY.get(key)
        if existing is not None:
            raise ValueError(
                f"output key {_key_name(key)} is already registered to "
                f"{existing.__qualname__}; cannot also register {cls.__qualname__}"
            )
        _REGISTRY[key] = cls
        return cls

    return decorator


def _check_entry(slot: object, binding: object, listed: dict[OutputKey, object]) -> None:
    """Refuse one catalog entry, and add its key to ``listed`` (see :func:`_check_catalog`)."""
    if not isinstance(slot, tuple) or len(slot) != 2:  # noqa: PLR2004 — (Subcommand, format_id)
        raise OutputCatalogError(f"catalog slot {slot!r} is not a (Subcommand, format_id) pair")
    subcommand, format_id = slot
    if not isinstance(subcommand, Subcommand):
        raise OutputCatalogError(f"catalog slot {slot!r}: {subcommand!r} is not a Subcommand")
    if not isinstance(format_id, str):
        raise OutputCatalogError(f"catalog slot {slot!r}: format_id is not a string")
    if not isinstance(binding, OutputBinding):
        raise OutputCatalogError(f"catalog slot {slot!r}: binding is not an OutputBinding")
    key, target = getattr(binding, "key", None), getattr(binding, "target", None)
    name = _key_name(key)
    key = _valid_key(key)
    if key is None:
        raise OutputCatalogError(
            f"catalog slot {slot!r} lists {name}, which is not an OutputKey "
            f"of a Destination and an Artifact"
        )
    if key in listed:
        raise OutputCatalogError(f"catalog slots {listed[key]!r} and {slot!r} both list {name}")
    listed[key] = slot
    if key.destination in _KEY_ONLY_DESTINATIONS:
        raise OutputCatalogError(
            f"catalog slot {slot!r} lists {name}, a key-only destination's key"
        )
    if _SCHEDULE_OF.get(key.artifact) is not subcommand:
        raise OutputCatalogError(
            f"catalog slot {slot!r} lists {name}, whose artifact is not a {subcommand.value} table"
        )
    _check_target(slot, format_id, key, target)


def _check_target(slot: object, format_id: str, key: OutputKey, target: object) -> None:
    """Refuse a target other than ``key``'s registered writer declaring
    ``format_id``, unless it is a renderer and no writer is registered (#1201)."""
    name, writer = _key_name(key), _REGISTRY.get(key)
    if writer is None and isinstance(target, PortalCsv | FilingXlsx):
        return
    if writer is None:
        raise OutputCatalogError(f"catalog slot {slot!r} lists {name}, but no writer is registered")
    if target is not writer:
        raise OutputCatalogError(
            f"catalog slot {slot!r} binds {name} to the wrong target; its registered "
            f"writer is {writer.__qualname__}"
        )
    declared = getattr(writer, "format_id", None)
    if declared != format_id:
        raise OutputCatalogError(
            f"catalog slot {slot!r} lists {name}, whose writer "
            f"{writer.__qualname__} declares format_id {declared!r}"
        )


def _check_catalog() -> None:
    """Refuse a catalog that disagrees with the registered writers (#790).

    Runs before every lookup and listing, so a registration or catalog change
    after an earlier lookup is caught too.

    Raises:
        OutputCatalogError: If a slot is not a ``(Subcommand, str)`` pair or
            binds something that is not an OutputBinding of a key; two slots
            list one key; a slot lists a key-only key or another schedule's
            key; no writer is registered under a listed key, unless a renderer
            targets it (#1201); a binding's target is not that writer (#1200);
            the writer's ``format_id`` differs from the slot's; or a registered
            key is malformed, or is neither listed nor key-only; or a command's
            formats or a selection disagree with the catalog (#1206, see
            :func:`_check_commands`).
    """
    listed: dict[OutputKey, object] = {}
    for slot, binding in _CATALOG.items():
        _check_entry(slot, binding, listed)
    for key, writer in _REGISTRY.items():
        if _valid_key(key) is None:
            raise OutputCatalogError(
                f"{writer.__qualname__} is registered under {_key_name(key)}, "
                f"which is not an OutputKey of a Destination and an Artifact"
            )
        if key not in listed and key.destination not in _KEY_ONLY_DESTINATIONS:
            raise OutputCatalogError(
                f"{writer.__qualname__} is registered under {_key_name(key)}, "
                f"which has no catalog entry"
            )
    _check_commands()


def output_binding(subcommand: Subcommand, format_id: str) -> OutputBinding:
    """Return the :class:`OutputBinding` the catalog lists for ``subcommand``'s ``format_id``.

    Raises:
        UnknownOutputFormatError: If the catalog lists no ``format_id`` under
            ``subcommand``; lists the slot's formats.
        InvariantError: If the arguments are not a Subcommand and a string.
        OutputCatalogError: See :func:`_check_catalog`.
    """
    require(
        isinstance(subcommand, Subcommand) and isinstance(format_id, str),
        what="output_binding requires a Subcommand and string format_id",
        subcommand=subcommand,
        format_id=format_id,
    )
    _check_catalog()
    binding = _CATALOG.get((subcommand, format_id))
    if binding is None:
        raise UnknownOutputFormatError(format_id, available_formats(subcommand))
    return binding


def available_formats(subcommand: Subcommand) -> list[str]:
    """Return every ``format_id`` the catalog lists under ``subcommand``,
    sorted alphabetically.

    Used by the CLI to populate the subcommand's ``--format``
    argument choice list and by :class:`UnknownOutputFormatError`
    to render its available-formats message.

    Raises:
        InvariantError: If ``subcommand`` is not a Subcommand.
        OutputCatalogError: See :func:`_check_catalog`.
    """
    require(
        isinstance(subcommand, Subcommand),
        what="available_formats requires a Subcommand",
        subcommand=subcommand,
    )
    _check_catalog()
    return sorted(format_id for slot, format_id in _CATALOG if slot is subcommand)


class CommandFormats(NamedTuple):
    """One CLI command's formats (#1206): its ``--format`` choices in
    ``--help`` order (none: the command has no ``--format`` and always writes
    ``default``), and the schedules it writes in them. ``substitutes`` maps a
    ``(schedule, choice)`` pair to the format the command writes instead.
    """

    schedules: tuple[Subcommand, ...]
    choices: tuple[str, ...]
    default: str
    substitutes: Mapping[tuple[Subcommand, str], str] = MappingProxyType({})


def _check_commands() -> None:
    """Refuse a command's formats or a selection the catalog doesn't back (#1206).

    Every choice and default must be a slot of each of the command's schedules,
    or the format ID of a CBDT JSON writer (the combined FA fragment); a default
    must be one of the choices when there are any; a substitute must replace a
    choice with a slot. Every selection must be a slot.
    """
    cbdt_json = {
        w.format_id for k, w in _REGISTRY.items() if k.destination is Destination.CBDT_JSON
    }
    for command, formats in _COMMAND_FORMATS.items():
        listed = (*formats.choices, formats.default)
        needed = {(s, f) for s in formats.schedules for f in listed if f not in cbdt_json}
        needed |= {(s, f) for (s, _), f in formats.substitutes.items()}
        offered = {(s, f) for s in formats.schedules for f in formats.choices}
        if (
            (formats.choices and formats.default not in formats.choices)
            or not formats.substitutes.keys() <= offered
            or not needed <= _CATALOG.keys()
        ):
            raise OutputCatalogError(f"command {command!r}'s formats disagree with the catalog")
    if not set(_SELECTED) <= _CATALOG.keys():
        raise OutputCatalogError("a selection lists a slot the catalog lacks")


def command_formats(command: str) -> CommandFormats:
    """Return CLI ``command``'s formats (see :class:`CommandFormats`).

    Raises:
        InvariantError: If the declaration lists no ``command``.
        OutputCatalogError: See :func:`_check_catalog`.
    """
    _check_catalog()
    require(
        isinstance(command, str) and command in _COMMAND_FORMATS,
        what="command_formats requires a declared command",
        command=command,
    )
    return _COMMAND_FORMATS[command]


# ---------------------------------------------------------------------------
# Writer imports (must come last to break the circular import:
# review_csv -> ibkr_fa.output (for @register) -> review_csv). Importing a
# writer module runs its @register; the key-only writers' modules are imported
# for that alone.
# ---------------------------------------------------------------------------

from ibkr_fa.output import cbdt_json_fragment, netbenefits_quicko_fa_xlsx  # noqa: E402, F401
from ibkr_fa.output.filing_csv import (  # noqa: E402
    FA_UPLOAD,
    FILING_WORKSHEET,
    FORM67_UPLOAD,
    PortalCsv,
)
from ibkr_fa.output.filing_xlsx import FilingXlsx  # noqa: E402
from ibkr_fa.output.form67_review_csv import Form67ReviewCsvWriter  # noqa: E402
from ibkr_fa.output.form67_xlsx import Form67XlsxWriter  # noqa: E402
from ibkr_fa.output.review_csv import ReviewCsvWriter  # noqa: E402

# Re-export the Schedule CG marker-row prefix constants. CG-flavour
# Indian-source marker is renamed to avoid collision with the OS
# Indian-source audit marker (Schedule OS source attribution and CG domestic-CG are
# different routes with the same source filter).
from ibkr_fa.output.schedule_cg_ltcg_review_csv import (  # noqa: E402
    EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER,
    EXCLUDED_LTCG_OUT_OF_FY_MARKER,
    EXCLUDED_PRE_CUTOVER_MARKER,
    EXCLUDED_STCG_MARKER,
    ScheduleCgLtcgReviewCsvWriter,
)
from ibkr_fa.output.schedule_cg_ltcg_xlsx import ScheduleCgLtcgXlsxWriter  # noqa: E402
from ibkr_fa.output.schedule_cg_review_csv import (  # noqa: E402
    EXCLUDED_CG_INDIAN_SOURCE_MARKER,
    EXCLUDED_LTCG_DEFERRED_MARKER,
    EXCLUDED_OUT_OF_FY_MARKER,
    ScheduleCgReviewCsvWriter,
)
from ibkr_fa.output.schedule_cg_xlsx import ScheduleCgXlsxWriter  # noqa: E402
from ibkr_fa.output.schedule_fa_a2_review_csv import ScheduleFaA2ReviewCsvWriter  # noqa: E402
from ibkr_fa.output.schedule_fa_a2_xlsx import ScheduleFaA2XlsxWriter  # noqa: E402
from ibkr_fa.output.schedule_fsi_review_csv import ScheduleFsiReviewCsvWriter  # noqa: E402
from ibkr_fa.output.schedule_fsi_xlsx import ScheduleFsiXlsxWriter  # noqa: E402
from ibkr_fa.output.schedule_os_review_csv import ScheduleOsReviewCsvWriter  # noqa: E402
from ibkr_fa.output.schedule_os_xlsx import ScheduleOsXlsxWriter  # noqa: E402
from ibkr_fa.output.schedule_tr_review_csv import ScheduleTrReviewCsvWriter  # noqa: E402
from ibkr_fa.output.schedule_tr_xlsx import ScheduleTrXlsxWriter  # noqa: E402
from ibkr_fa.output.schedules.cg_filing import cg_filing_sheets  # noqa: E402
from ibkr_fa.output.schedules.fa import a2_upload_rows, a3_upload_rows  # noqa: E402
from ibkr_fa.output.schedules.form67 import form67_upload_rows  # noqa: E402
from ibkr_fa.output.schedules.fsi import fsi_filing_rows  # noqa: E402
from ibkr_fa.output.schedules.os import filing_csv_rows, filing_workbook_sheets  # noqa: E402
from ibkr_fa.output.schedules.tr import tr_filing_rows  # noqa: E402
from ibkr_fa.output.xlsx import XlsxWriter  # noqa: E402

# The catalog (#790, #1200): each public ``--format`` slot, ``(Subcommand,
# format_id)``, bound to the key of the file it writes and the writer or shared
# renderer (#1201, #1202, #1205) that renders it.
_CATALOG: dict[tuple[Subcommand, str], OutputBinding] = {
    (Subcommand.FA, "filing-csv"): OutputBinding(
        OutputKey(Destination.PORTAL_ENTRY, Artifact.FA_A3_CSV),
        PortalCsv(a3_upload_rows, FA_UPLOAD),
    ),
    (Subcommand.FA, "review-csv"): OutputBinding(
        OutputKey(Destination.REVIEW, Artifact.FA_A3_CSV), ReviewCsvWriter
    ),
    (Subcommand.FA, "xlsx"): OutputBinding(
        OutputKey(Destination.WORKBOOK, Artifact.FA_A3_XLSX), XlsxWriter
    ),
    (Subcommand.FA_A2, "filing-csv"): OutputBinding(
        OutputKey(Destination.PORTAL_ENTRY, Artifact.FA_A2_CSV),
        PortalCsv(a2_upload_rows, FA_UPLOAD),
    ),
    (Subcommand.FA_A2, "review-csv"): OutputBinding(
        OutputKey(Destination.REVIEW, Artifact.FA_A2_CSV), ScheduleFaA2ReviewCsvWriter
    ),
    (Subcommand.FA_A2, "xlsx"): OutputBinding(
        OutputKey(Destination.WORKBOOK, Artifact.FA_A2_XLSX), ScheduleFaA2XlsxWriter
    ),
    (Subcommand.OS, "filing-csv"): OutputBinding(
        OutputKey(Destination.PORTAL_ENTRY, Artifact.OS_CSV),
        PortalCsv(filing_csv_rows, FILING_WORKSHEET),
    ),
    (Subcommand.OS, "filing-xlsx"): OutputBinding(
        OutputKey(Destination.PORTAL_ENTRY, Artifact.OS_XLSX), FilingXlsx(filing_workbook_sheets)
    ),
    (Subcommand.OS, "review-csv"): OutputBinding(
        OutputKey(Destination.REVIEW, Artifact.OS_CSV), ScheduleOsReviewCsvWriter
    ),
    (Subcommand.OS, "xlsx"): OutputBinding(
        OutputKey(Destination.WORKBOOK, Artifact.OS_XLSX), ScheduleOsXlsxWriter
    ),
    (Subcommand.CG, "review-csv"): OutputBinding(
        OutputKey(Destination.REVIEW, Artifact.CG_STCG_CSV), ScheduleCgReviewCsvWriter
    ),
    (Subcommand.CG, "xlsx"): OutputBinding(
        OutputKey(Destination.WORKBOOK, Artifact.CG_STCG_XLSX), ScheduleCgXlsxWriter
    ),
    (Subcommand.CG_LTCG, "review-csv"): OutputBinding(
        OutputKey(Destination.REVIEW, Artifact.CG_LTCG_CSV), ScheduleCgLtcgReviewCsvWriter
    ),
    (Subcommand.CG_LTCG, "xlsx"): OutputBinding(
        OutputKey(Destination.WORKBOOK, Artifact.CG_LTCG_XLSX), ScheduleCgLtcgXlsxWriter
    ),
    (Subcommand.CG_FILING, "filing-xlsx"): OutputBinding(
        OutputKey(Destination.PORTAL_ENTRY, Artifact.CG_XLSX), FilingXlsx(cg_filing_sheets)
    ),
    (Subcommand.FSI, "filing-csv"): OutputBinding(
        OutputKey(Destination.PORTAL_ENTRY, Artifact.FSI_CSV),
        PortalCsv(fsi_filing_rows, FILING_WORKSHEET),
    ),
    (Subcommand.FSI, "review-csv"): OutputBinding(
        OutputKey(Destination.REVIEW, Artifact.FSI_CSV), ScheduleFsiReviewCsvWriter
    ),
    (Subcommand.FSI, "xlsx"): OutputBinding(
        OutputKey(Destination.WORKBOOK, Artifact.FSI_XLSX), ScheduleFsiXlsxWriter
    ),
    (Subcommand.TR, "filing-csv"): OutputBinding(
        OutputKey(Destination.PORTAL_ENTRY, Artifact.TR_CSV),
        PortalCsv(tr_filing_rows, FILING_WORKSHEET),
    ),
    (Subcommand.TR, "review-csv"): OutputBinding(
        OutputKey(Destination.REVIEW, Artifact.TR_CSV), ScheduleTrReviewCsvWriter
    ),
    (Subcommand.TR, "xlsx"): OutputBinding(
        OutputKey(Destination.WORKBOOK, Artifact.TR_XLSX), ScheduleTrXlsxWriter
    ),
    (Subcommand.FORM67, "portal-csv"): OutputBinding(
        OutputKey(Destination.PORTAL_ENTRY, Artifact.FORM67_CSV),
        PortalCsv(form67_upload_rows, FORM67_UPLOAD),
    ),
    (Subcommand.FORM67, "review-csv"): OutputBinding(
        OutputKey(Destination.REVIEW, Artifact.FORM67_CSV), Form67ReviewCsvWriter
    ),
    (Subcommand.FORM67, "xlsx"): OutputBinding(
        OutputKey(Destination.WORKBOOK, Artifact.FORM67_XLSX), Form67XlsxWriter
    ),
}

# Selections (#1206): the slots a workflow writes whatever --format says,
# each in write order. A selection names what a run writes, never what it
# removes: filenames, historical cleanup targets, promised files, preflights
# and transaction windows stay explicit in each workflow.
INCOME_FILING: Final = (
    (Subcommand.OS, "filing-csv"),
    (Subcommand.FSI, "filing-csv"),
    (Subcommand.TR, "filing-csv"),
    (Subcommand.FORM67, "portal-csv"),
)
INCOME_REVIEW: Final = tuple((subcommand, "review-csv") for subcommand, _ in INCOME_FILING)
# Robinhood's portal workbooks, written whatever its --format says.
CG_FILING_WORKBOOK: Final = (Subcommand.CG_FILING, "filing-xlsx")
OS_FILING_WORKBOOK: Final = (Subcommand.OS, "filing-xlsx")
_SELECTED: Final = (*INCOME_FILING, *INCOME_REVIEW, CG_FILING_WORKBOOK, OS_FILING_WORKBOOK)

# Command exposure (#1206): each CLI command's formats; see CommandFormats.
# Support is not exposure: a command lists only the formats its workflow
# writes, so a source command can offer fewer than the catalog supports.
_CBDT_JSON: Final = cbdt_json_fragment.CBDT_JSON_FRAGMENT_FORMAT_ID
_FA: Final = (Subcommand.FA, Subcommand.FA_A2)
_CG: Final = (Subcommand.CG, Subcommand.CG_LTCG)
_REVIEW_CSV_XLSX: Final = ("review-csv", "xlsx")
_COMMAND_FORMATS: Final[Mapping[str, CommandFormats]] = {
    "schedule-fa": CommandFormats(
        _FA, ("filing-csv", "review-csv", "xlsx", _CBDT_JSON), "review-csv"
    ),
    "netbenefits-fa": CommandFormats(_FA, ("filing-csv", "review-csv", _CBDT_JSON), "filing-csv"),
    "fidelity-cma-fa": CommandFormats(_FA, ("filing-csv", *_REVIEW_CSV_XLSX), "review-csv"),
    "robinhood-fa": CommandFormats(
        _FA, (_CBDT_JSON, "filing-csv", "review-csv", "xlsx"), "review-csv"
    ),
    # robinhood-cg's --format picks its debug files; see CG_FILING_WORKBOOK.
    "robinhood-cg": CommandFormats(_CG, _REVIEW_CSV_XLSX, "review-csv"),
    "robinhood-all": CommandFormats((*_CG, *_FA), _REVIEW_CSV_XLSX, "review-csv"),
    # netbenefits-all has no --format: it files Schedule FA as the upload CSV.
    "netbenefits-all": CommandFormats(_FA, (), "filing-csv"),
    "schedule-os": CommandFormats(
        (Subcommand.OS,), ("filing-csv", "filing-xlsx", "review-csv", "xlsx"), "review-csv"
    ),
    "schedule-cg": CommandFormats((Subcommand.CG,), _REVIEW_CSV_XLSX, "review-csv"),
    "schedule-cg-ltcg": CommandFormats((Subcommand.CG_LTCG,), _REVIEW_CSV_XLSX, "review-csv"),
    "schedule-fsi": CommandFormats(
        (Subcommand.FSI,), ("filing-csv", "review-csv", "xlsx"), "review-csv"
    ),
    "schedule-tr": CommandFormats(
        (Subcommand.TR,), ("filing-csv", "review-csv", "xlsx"), "review-csv"
    ),
    "form-67": CommandFormats(
        (Subcommand.FORM67,), ("portal-csv", "review-csv", "xlsx"), "portal-csv"
    ),
    "run-all": CommandFormats(
        (Subcommand.OS, *_CG, Subcommand.FSI, Subcommand.TR, Subcommand.FORM67, *_FA),
        _REVIEW_CSV_XLSX,
        "review-csv",
        # Under its default, run-all files Form 67 as the upload CSV (#601).
        MappingProxyType({(Subcommand.FORM67, "review-csv"): "portal-csv"}),
    ),
}

__all__ = [
    "CG_FILING_WORKBOOK",
    "EXCLUDED_CG_INDIAN_SOURCE_MARKER",
    "EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER",
    "EXCLUDED_LTCG_DEFERRED_MARKER",
    "EXCLUDED_LTCG_OUT_OF_FY_MARKER",
    "EXCLUDED_OUT_OF_FY_MARKER",
    "EXCLUDED_PRE_CUTOVER_MARKER",
    "EXCLUDED_STCG_MARKER",
    "INCOME_FILING",
    "INCOME_REVIEW",
    "OS_FILING_WORKBOOK",
    "Artifact",
    "CommandFormats",
    "Destination",
    "FaA2Writer",
    "FaA3Writer",
    "Form67Writer",
    "OutputBinding",
    "OutputCatalogError",
    "OutputKey",
    "OutputWriter",
    "RenderContext",
    "ScheduleCgLtcgWriter",
    "ScheduleCgWriter",
    "ScheduleFsiWriter",
    "ScheduleOsWriter",
    "ScheduleTrWriter",
    "Subcommand",
    "UnknownOutputFormatError",
    "available_formats",
    "command_formats",
    "output_binding",
    "register",
]
