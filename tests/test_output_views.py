"""Tests for the shared portal-cell views in :mod:`ibkr_fa.output._views` (#616)."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

import ibkr_fa
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.models.schedule_tr_row import ScheduleTrRow, TaxReliefSection
from ibkr_fa.output import portal_labels
from ibkr_fa.output._views.fsi import FSI_PORTAL_FIELDS, FsiPortalCells, fsi_portal_cells
from ibkr_fa.output._views.tr import TrPortalCells, tr_portal_cells

_PACKAGE_ROOT = Path(ibkr_fa.__file__).parent


def _fsi_row(
    *,
    foreign_tax_inr: Decimal = Decimal("400"),
    tax_payable_inr: Decimal | None = None,
    tax_relief_inr: Decimal | None = None,
    dtaa_article: str | None = None,
    taxpayer_id_number: str | None = None,
) -> ScheduleFsiRow:
    return ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("1000.5"),
        foreign_tax_inr=foreign_tax_inr,
        event_count=1,
        tax_payable_inr=tax_payable_inr,
        tax_relief_inr=tax_relief_inr,
        dtaa_article=dtaa_article,
        taxpayer_id_number=taxpayer_id_number,
    )


def _tr_row(
    *,
    tax_relief_inr_total: Decimal | None = None,
    section_under_which_claimed: TaxReliefSection | None = None,
    taxpayer_id_number: str | None = None,
) -> ScheduleTrRow:
    return ScheduleTrRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        foreign_tax_inr_total=Decimal("400"),
        event_count=1,
        tax_relief_inr_total=tax_relief_inr_total,
        section_under_which_claimed=section_under_which_claimed,
        taxpayer_id_number=taxpayer_id_number,
    )


# ---------------------------------------------------------------------------
# Schedule FSI view
# ---------------------------------------------------------------------------


def test_fsi_cells_render_blank_for_missing_values() -> None:
    cells = fsi_portal_cells(_fsi_row())

    assert cells == FsiPortalCells(
        country_code="2",
        tin="",
        head="Other Sources",
        income="1001",
        tax_paid="400",
        tax_payable="",
        tax_relief="",
        dtaa_article="",
    )


def test_fsi_cells_render_computed_values() -> None:
    row = _fsi_row(
        tax_payable_inr=Decimal("0"),
        tax_relief_inr=Decimal("0"),
        dtaa_article="Article 10(2)(b)",
        taxpayer_id_number="123456789",
    )

    cells = fsi_portal_cells(row)

    assert (cells.tin, cells.tax_payable, cells.tax_relief, cells.dtaa_article) == (
        "123456789",
        "0",
        "0",
        "Article 10(2)(b)",
    )


def test_fsi_fields_pair_with_portal_headers() -> None:
    headers = dict(zip(FSI_PORTAL_FIELDS, portal_labels.PORTAL_COLUMN_HEADERS_FSI, strict=True))

    assert headers == {
        "country_code": "Country Code",
        "tin": "Taxpayer Identification number",
        "head": "Head of Income",
        "income": "Income from outside India (included in PART B-TI)",
        "tax_paid": "Tax paid outside India",
        "tax_payable": "Tax payable on such income under normal provisions in India",
        "tax_relief": "Tax relief available in India (e)= (c) or (d) whichever is lower",
        "dtaa_article": "Relevant article of DTAA if relief claimed u/s 90 or 90A",
    }


def test_fsi_as_row_follows_field_order() -> None:
    cells = fsi_portal_cells(
        _fsi_row(
            tax_payable_inr=Decimal("300"),
            tax_relief_inr=Decimal("300"),
            dtaa_article="Article 10(2)(b)",
            taxpayer_id_number="123456789",
        ),
    )

    assert cells.as_row() == [
        "2",
        "123456789",
        "Other Sources",
        "1001",
        "400",
        "300",
        "300",
        "Article 10(2)(b)",
    ]


# ---------------------------------------------------------------------------
# Schedule TR view
# ---------------------------------------------------------------------------


def test_tr_cells_render_blank_for_missing_values() -> None:
    cells = tr_portal_cells(_tr_row())

    assert cells == TrPortalCells(
        country_code="2",
        tin="",
        tax_paid_total="400",
        tax_relief_total="",
        section="",
    )
    assert cells.as_row() == ["2", "", "400", "", ""]


def test_tr_cells_render_computed_zero_relief() -> None:
    row = _tr_row(
        tax_relief_inr_total=Decimal("0"),
        section_under_which_claimed=TaxReliefSection.SECTION_90,
        taxpayer_id_number="123456789",
    )

    cells = tr_portal_cells(row)

    assert cells.as_row() == ["2", "123456789", "400", "0", "90"]


# ---------------------------------------------------------------------------
# Import boundaries
# ---------------------------------------------------------------------------

_SHARED_WRITER_MODULE = re.compile(
    r"ibkr_fa\.output\.(schedule_\w+_review_csv|schedule_\w+_filing_csv|form67_\w+|schedules\.\w+)"
)


def _imports(path: Path) -> Iterator[tuple[int, str, tuple[str, ...]]]:
    """Yield ``(line, module, imported names)`` for each import in ``path``.

    Relative imports are resolved against the module's package.
    """
    package = path.relative_to(_PACKAGE_ROOT.parent).with_suffix("").parts[:-1]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, ()
        elif isinstance(node, ast.ImportFrom):
            parts = list(package[: len(package) + 1 - node.level]) if node.level else []
            if node.module:
                parts.append(node.module)
            yield node.lineno, ".".join(parts), tuple(alias.name for alias in node.names)


def _in_package(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


@pytest.mark.parametrize(
    "module",
    ["output/schedules/fsi.py", "output/schedules/tr.py", "pipeline_tr.py"],
)
def test_filing_paths_import_no_private_writer_names(module: str) -> None:
    offenders = [
        f"{line}: from {source} import {name}"
        for line, source, names in _imports(_PACKAGE_ROOT / module)
        if _SHARED_WRITER_MODULE.fullmatch(source)
        for name in names
        if name.startswith("_")
    ]
    assert offenders == []


@pytest.mark.parametrize(
    ("source", "allowed"),
    [
        ("from ibkr_fa.output import _views", True),
        ("from .. import _views", True),
        ("def f():\n    from .. import _views", True),
        ("import ibkr_fa.output._views.tr", True),
        ("from .tr import tr_portal_cells", True),
        ("from ibkr_fa.output import portal_labels", False),
        ("from .. import portal_labels", False),
        ("def f():\n    from ibkr_fa.output.schedules.form67 import _rate", False),
        ("import ibkr_fa.output.portal_labels", False),
        ("from ibkr_fa import output", False),
    ],
)
def test_view_import_boundary_resolves_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, allowed: bool
) -> None:
    package = tmp_path / "ibkr_fa"
    views = package / "output" / "_views"
    views.mkdir(parents=True)
    for name in ("__init__.py", "fsi.py", "tr.py"):
        (views / name).write_text(source if name == "fsi.py" else "", encoding="utf-8")
    monkeypatch.setitem(globals(), "_PACKAGE_ROOT", package)
    if allowed:
        test_views_import_nothing_else_from_output()
    else:
        with pytest.raises(AssertionError):
            test_views_import_nothing_else_from_output()


def test_views_import_nothing_else_from_output() -> None:
    paths = sorted((_PACKAGE_ROOT / "output" / "_views").glob("*.py"))
    assert len(paths) == 3
    offenders = []
    for path in paths:
        for line, source, names in _imports(path):
            targets = [f"{source}.{name}" for name in names] if names else [source]
            if any(
                _in_package(target, "ibkr_fa.output")
                and not _in_package(target, "ibkr_fa.output._views")
                for target in targets
            ):
                offenders.append(f"{path.name}:{line}: {source} {names}")
    assert offenders == []
