"""Generic output-writer Protocol, per-subcommand Protocol aliases,
the Subcommand enum, the output keys, and the shared error types.

Architecture (#790, #1200):

* :class:`OutputWriter` is the single, generic Protocol every output
  writer satisfies — parameterised by the input type ``T`` that
  the writer consumes. Implementations declare a ``format_id``
  class attribute (the CLI ``--format`` value), a ``from_context``
  classmethod and a ``write(value, path)`` method.
* :class:`OutputKey` — a :class:`Destination` and an :class:`Artifact` —
  is the registry key: each writer registers under its own key in
  :mod:`ibkr_fa.output`.
* :class:`Subcommand` is the closed enum of CLI subcommands. The
  output catalog in :mod:`ibkr_fa.output` maps each ``(Subcommand,
  format_id)`` slot to an :class:`OutputBinding` (the key and the writer
  that renders it), which is where a subcommand's ``--format`` choices
  come from.
* The type-aliases (:data:`FaA3Writer`, :data:`FaA2Writer`,
  :data:`ScheduleOsWriter`, :data:`ScheduleCgWriter`,
  :data:`ScheduleCgLtcgWriter`, :data:`ScheduleFsiWriter`,
  :data:`ScheduleTrWriter`, :data:`Form67Writer`) bind the generic ``OutputWriter[T]`` to each
  subcommand's concrete input type.

Pre-#156 history (kept for context): the original v1 N=2 design
used four sibling per-subcommand Protocols and four sibling
registries. #156 unified them under a single registry partitioned by
:class:`Subcommand`; #644 added :class:`OutputKey` as a view over it,
and #790 made the key the registry key and retired the
per-subcommand wrappers.

A run looks up its slot's binding by ``--format`` value via
:func:`ibkr_fa.output.output_binding`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, Self

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.form67 import Form67ComputeResult
from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from ibkr_fa.compute.schedule_cg_ltcg import ScheduleCgLtcgComputeResult
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.compute.schedule_os import ScheduleOsComputeResult
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.invariants import require
from ibkr_fa.models.fa_a2_row import FaA2Row
from ibkr_fa.models.fa_a3_row import FaA3Row
from ibkr_fa.output.quicko_foreign_tax import QuickoForeignTaxInput

if TYPE_CHECKING:
    from pathlib import Path

    from ibkr_fa.output.filing_csv import PortalCsv, PortalCsvWriter
    from ibkr_fa.output.filing_xlsx import FilingXlsx, FilingXlsxWriter


class Subcommand(Enum):
    """Closed enum of CLI subcommands whose ``--format`` choices the
    output catalog lists.

    The value of each member is the literal CLI subcommand name used
    by :mod:`ibkr_fa.cli` argparse (e.g., ``"schedule-fa"``) — this
    keeps a catalog slot human-meaningful at debug time and makes
    a UnknownOutputFormatError message recognisable to a user who
    has just typed the command name.

    Adding a new subcommand:

    1. Add a member here (e.g., ``FORM67 = "form-67"``).
    2. Define a per-subcommand type alias in this module
       (e.g., ``Form67Writer = OutputWriter[Form67ComputeResult]``),
       and :class:`Artifact` members for its tables.
    3. Decorate each writer class with
       ``@register(OutputKey(Destination.REVIEW, Artifact.FORM67_CSV))``
       (its own key) in :mod:`ibkr_fa.output.form67_review_csv` or
       similar, and give it a :meth:`OutputWriter.from_context` classmethod.
    4. Import the writer class in the bottom-of-module import block in
       :mod:`ibkr_fa.output` (next to ``ReviewCsvWriter`` /
       ``ScheduleOsReviewCsvWriter`` / etc.) so its ``@register``
       decorator actually runs at process start-up.
    5. Bind each new ``(Subcommand, format_id)`` to an
       ``OutputBinding(key, writer)`` in the catalog in :mod:`ibkr_fa.output`,
       and list each new artifact's schedule in its artifact table; a lookup
       refuses a writer the catalog doesn't bind (#790, #1200).
    6. List each new format in the command formats of every command that
       offers it (``_COMMAND_FORMATS`` in :mod:`ibkr_fa.output`, #1206).

    Attributes:
        FA: :data:`Subcommand.FA` — ``schedule-fa`` (Schedule FA
            Table A3 foreign-asset disclosure).
        OS: ``schedule-os`` — Schedule OS row 1a(i) ordinary dividends.
        CG: ``schedule-cg`` — Schedule CG Section A STCG (foreign
            equity short-term capital gains).
        CG_LTCG: ``schedule-cg-ltcg`` — Schedule CG Section B LTCG
            (foreign equity long-term capital gains).
        CG_FILING: ``schedule-cg-filing`` — the portal-shaped Schedule CG
            filing workbook (STCG and LTCG sections). Registry-only: a
            source's CG command writes it as ``schedule-cg.xlsx``, so it
            has no CLI subcommand of its own (#642).
        FSI: ``schedule-fsi`` — Schedule FSI (Foreign Source Income)
            per-(country, head-of-income) aggregate rows.
        TR: ``schedule-tr`` — Schedule TR (Summary of Tax Relief
            Claimed) per-country aggregate rows.
        FORM67: ``form-67`` — CBDT Form 67 (Statement of income from
            a country or specified territory outside India and Foreign
            Tax Credit) per-(country, source-of-income) aggregate rows;
            filed via the e-filing portal's Form 67 service per Rule
            128(9) of the Income-tax Rules, 1962.
    """

    FA = "schedule-fa"
    FA_A2 = "schedule-fa-a2"
    OS = "schedule-os"
    CG = "schedule-cg"
    CG_LTCG = "schedule-cg-ltcg"
    CG_FILING = "schedule-cg-filing"
    FSI = "schedule-fsi"
    TR = "schedule-tr"
    FORM67 = "form-67"


class Destination(Enum):
    """Where a written file goes: who or what consumes it (#644).

    Attributes:
        PORTAL_ENTRY: Typed into the ITR portal by hand (``filing-csv``,
            ``filing-xlsx``, ``portal-csv``).
        REVIEW: A review / audit copy with provenance columns (``review-csv``).
        WORKBOOK: A formatted workbook for a person to read (``xlsx``).
        CBDT_JSON: Imported into the CBDT offline utility.
        QUICKO_IMPORT: Uploaded to Quicko through its import template.
    """

    PORTAL_ENTRY = "portal-entry"
    REVIEW = "review"
    WORKBOOK = "workbook"
    CBDT_JSON = "cbdt-json"
    QUICKO_IMPORT = "quicko-import"


class Artifact(Enum):
    """Which schedule table a file holds, and its file kind (#644).

    ``CG_XLSX`` is the filing workbook, which holds the STCG and LTCG sections
    together; ``FA_A2_A3_JSON`` is the combined CBDT Schedule FA fragment.
    """

    FA_A2_CSV = "schedule-fa-a2-csv"
    FA_A2_XLSX = "schedule-fa-a2-xlsx"
    FA_A3_CSV = "schedule-fa-a3-csv"
    FA_A3_XLSX = "schedule-fa-a3-xlsx"
    FA_A2_A3_JSON = "schedule-fa-a2-a3-json"
    OS_CSV = "schedule-os-csv"
    OS_XLSX = "schedule-os-xlsx"
    CG_STCG_CSV = "schedule-cg-stcg-csv"
    CG_STCG_XLSX = "schedule-cg-stcg-xlsx"
    CG_LTCG_CSV = "schedule-cg-ltcg-csv"
    CG_LTCG_XLSX = "schedule-cg-ltcg-xlsx"
    CG_XLSX = "schedule-cg-xlsx"
    FSI_CSV = "schedule-fsi-csv"
    FSI_XLSX = "schedule-fsi-xlsx"
    TR_CSV = "schedule-tr-csv"
    TR_XLSX = "schedule-tr-xlsx"
    FORM67_CSV = "form-67-csv"
    FORM67_XLSX = "form-67-xlsx"


@dataclass(frozen=True)
class OutputKey:
    """A writer's destination and artifact (#644): the registry key (#790).

    The output catalog maps each schedule writer's ``(Subcommand, format_id)``
    to its key; the CBDT JSON fragment and Quicko writers have a key only.
    """

    destination: Destination
    artifact: Artifact

    def __post_init__(self) -> None:
        require(
            isinstance(self.destination, Destination) and isinstance(self.artifact, Artifact),
            what="OutputKey requires Destination and Artifact enum members",
            destination=self.destination,
            artifact=self.artifact,
        )


@dataclass(frozen=True)
class RenderContext:
    """What a run tells its writers (#644). See :meth:`OutputWriter.from_context`.

    Attributes:
        assessment_year_start: The run's AY (e.g. ``2026`` for AY 2026-27);
            selects the portal-labels snapshot.
        itr_form: The target ITR form, or ``None`` for the writer's default.
        source_label: The source named on workpaper rows, or ``None``.
        account_labels: Privacy-safe account labels by account index, or ``None``.
        debug: Whether this is a ``--debug`` run.
        quicko_foreign_tax: What the Schedule TR filing CSV needs to add
            Quicko's foreign tax credit block, or ``None`` for no block.
    """

    assessment_year_start: int
    itr_form: ItrForm | None = None
    source_label: str | None = None
    account_labels: Mapping[int, str] | None = None
    debug: bool = False
    quicko_foreign_tax: QuickoForeignTaxInput | None = None


class OutputWriter[T](Protocol):
    """Generic Protocol for any output writer.

    Parameterised by ``T``, the type of the value the writer consumes
    on each :meth:`write` call. Each CLI subcommand instantiates this
    Protocol at a concrete ``T`` matching its compute layer's output
    (see :data:`FaA3Writer`, :data:`ScheduleOsWriter`,
    :data:`ScheduleCgWriter`, :data:`ScheduleCgLtcgWriter`,
    :data:`ScheduleFsiWriter`, :data:`ScheduleTrWriter`).

    Conformant classes must declare:

    * A class-level :attr:`format_id` string (the ``--format`` CLI
      argument its catalog slot lists). Must be a non-empty lowercase-
      kebab-case string matching ``^[a-z][a-z0-9]*(-[a-z0-9]+)*$``
      (e.g., ``"review-csv"``, ``"cbdt-json"``). Enforced by
      :func:`ibkr_fa.output.register` at import time.
    * A :meth:`from_context` classmethod (#644), required at registration.
    * A :meth:`write` method matching the signature below.

    Attributes:
        format_id: Stable identifier for this output format. Unique
            within the subcommand's catalog slots (the same ``format_id``
            string may be listed once per subcommand).
    """

    format_id: ClassVar[str]

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:
        """Build the writer for a run from ``ctx``.

        Passes the constructor only the fields this writer uses. A ``None``
        field leaves the constructor's default (current-AY labels, ITR-2
        headers); a field the writer cannot do without refuses when ``None``.
        """
        ...

    def write(self, value: T, path: Path) -> None:
        """Serialise ``value`` to ``path``.

        Implementations must:
          - Overwrite ``path`` if it exists; create parent directories
            only if explicitly documented (the v1 review-csv writers
            do NOT create parent directories — fail loud on a typo).
          - Raise :class:`OSError` (or a subclass) on filesystem failure,
            except :class:`~ibkr_fa.output._cleanup.OutputRemovalError`
            when the writer can't remove its own staging file.

        Args:
            value: The input the writer consumes. Type-aliased per
                subcommand (a sequence of :class:`FaA3Row` for FA,
                a compute-result dataclass for OS / CG / CG-LTCG /
                FSI / TR).
            path: Destination file path.
        """
        ...


@dataclass(frozen=True)
class OutputBinding:
    """What renders one catalog slot (#1200).

    The output catalog in :mod:`ibkr_fa.output` maps each public
    ``(Subcommand, format_id)`` slot to a binding: ``key`` names the file the
    slot writes and ``target`` renders it. A target is either the writer class
    registered under ``key`` or a shared portal renderer: a
    :class:`~ibkr_fa.output.filing_csv.PortalCsv` holding the schedule's row
    function and the artifact's dialect (#1201), or a
    :class:`~ibkr_fa.output.filing_xlsx.FilingXlsx` holding its sheet
    function (#1202).
    """

    key: OutputKey
    target: type[OutputWriter[Any]] | PortalCsv | FilingXlsx

    def writer(self, ctx: RenderContext) -> OutputWriter[Any] | PortalCsvWriter | FilingXlsxWriter:
        """Build this run's writer from ``ctx`` (see :meth:`OutputWriter.from_context`)."""
        return self.target.from_context(ctx)


# Per-subcommand type aliases. These are the Protocols ty sees on each
# subcommand's writer call site. They're aliases (not separate Protocols)
# so a single OutputWriter[T] implementation automatically satisfies the
# right alias.
FaA3Writer = OutputWriter[Iterable[FaA3Row]]
"""Schedule FA Table A3 writer alias of :class:`OutputWriter`.

Consumes a sequence of :class:`~ibkr_fa.models.fa_a3_row.FaA3Row`
records (one row per open lot) and serialises them to a single
output file.
"""

FaA2Writer = OutputWriter[Iterable[FaA2Row]]
"""Schedule FA Table A2 writer alias of :class:`OutputWriter`.

Consumes a sequence of :class:`~ibkr_fa.models.fa_a2_row.FaA2Row`
records (exactly one row — the custodial account itself) and
serialises them to a separate sibling output file alongside A3.
"""

ScheduleOsWriter = OutputWriter[ScheduleOsComputeResult]
"""Schedule OS writer alias of :class:`OutputWriter`.

Consumes a single
:class:`~ibkr_fa.compute.schedule_os.ScheduleOsComputeResult`
(which bundles the aggregated lines, the Indian-source audit rows,
the full-precision INR audit row, and the rupee TOTAL) and serialises the whole bundle
in one call.
"""

ScheduleCgWriter = OutputWriter[ScheduleCgStcgComputeResult]
"""Schedule CG STCG writer alias of :class:`OutputWriter`.

Consumes a single
:class:`~ibkr_fa.compute.schedule_cg.ScheduleCgStcgComputeResult`
(STCG lines + LTCG-deferred audit + Indian-source audit +
out-of-FY audit + full-precision INR audit row + TOTAL).
"""

ScheduleCgLtcgWriter = OutputWriter[ScheduleCgLtcgComputeResult]
"""Schedule CG LTCG writer alias of :class:`OutputWriter`.

Consumes a single
:class:`~ibkr_fa.compute.schedule_cg_ltcg.ScheduleCgLtcgComputeResult`
(LTCG lines + STCG-deferred audit + pre-2024-07-23-deferred audit +
Indian-source audit + out-of-FY audit + TOTAL).
"""

ScheduleFsiWriter = OutputWriter[ScheduleFsiComputeResult]
"""Schedule FSI writer alias of :class:`OutputWriter`.

Consumes a single
:class:`~ibkr_fa.compute.schedule_fsi.ScheduleFsiComputeResult`
(per-(country, head-of-income) aggregate rows). Unlike OS / CG, the
FSI output has no portal-TOTAL cell — each row is its own portal-
form row. The writer emits one CSV / XLSX row per aggregate row, in
the compute-layer sort order ((cbdt_country_code, head_of_income)
ascending; lexicographic on the CBDT code per
:class:`~ibkr_fa.compute.schedule_fsi.ScheduleFsiComputeResult`).
"""

ScheduleTrWriter = OutputWriter[ScheduleTrComputeResult]
"""Schedule TR writer alias of :class:`OutputWriter`.

Consumes a single
:class:`~ibkr_fa.compute.schedule_tr.ScheduleTrComputeResult`
(per-country tax-relief-summary rows; Schedule FSI's per-(country,
head) rows aggregated by summing foreign_tax_inr across heads).
Same per-row no-portal-TOTAL contract as Schedule FSI: each emitted
row IS a portal-form row, sorted by cbdt_country_code
lexicographically.
"""

Form67Writer = OutputWriter[Form67ComputeResult]
"""Form 67 writer alias of :class:`OutputWriter`.

Consumes a single
:class:`~ibkr_fa.compute.form67.Form67ComputeResult`
(per-(country, source-of-income) aggregate entries) and serialises
it to a single output file. Same per-row no-portal-TOTAL contract as
Schedule FSI / TR: each emitted row IS a portal-form row, sorted by
``(cbdt_country_code, source_of_income.value)`` ascending with the
compute layer's 1-indexed :attr:`Form67Entry.serial_number` preserved.
"""


class UnknownOutputFormatError(IbkrFaError):
    """Raised when an output-format lookup fails.

    Raised by :func:`ibkr_fa.output.output_binding` when the catalog
    lists no such ``format_id`` under the subcommand. The message lists
    every format in the same subcommand slot so the CLI can surface
    a helpful error when the user passes an invalid ``--format`` value.
    """

    def __init__(self, format_id: str, available: list[str]) -> None:
        self.format_id = format_id
        self.available = available
        listed = ", ".join(repr(f) for f in available) if available else "(none)"
        super().__init__(f"unknown output format {format_id!r}; available formats: {listed}")


class OutputCatalogError(IbkrFaError):
    """Raised when the output catalog and the registered writers disagree (#790).

    Raised by every lookup and listing in :mod:`ibkr_fa.output` before it
    answers, so a writer can't silently drop out of a ``--format`` list: a
    malformed catalog slot, binding or key, two slots listing one key, a slot
    listing another schedule's or a key-only key, a listed key with no writer,
    a binding whose target isn't the key's registered writer (#1200), a writer
    whose ``format_id`` differs, or a registered schedule writer the catalog
    doesn't list. The CLI builds its ``--format`` choices from the
    catalog, so it exits 4 on this error before parsing.
    """
