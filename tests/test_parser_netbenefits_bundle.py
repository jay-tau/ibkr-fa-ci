"""Tests for NetBenefits bundle discovery (:mod:`ibkr_fa.parser.netbenefits.bundle`).

``pdftotext_layout`` and ``parse_statement`` are monkeypatched so each PDF's text
(and parsed statement) comes from the test, keyed by file name or, for nested
files, by the path relative to the bundle. The pdftotext refusal tests stub
``subprocess.run`` instead, so the real ``pdftotext_layout`` runs.

#1041: discovered-file refusals use paths relative to ``--input`` and print no
file contents or exception text. Invalid-directory refusals name the typed path.
"""

from __future__ import annotations

import errno
import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.netbenefits.bundle import (
    NetBenefitsBundleError,
    load_bundle,
    load_statements,
)
from ibkr_fa.parser.netbenefits.statement_pdf import ReportType, StatementData
from tests._malformed_csv import assert_malformed
from tests._netbenefits import (
    CLOSED_EMPTY_CSV,
    CLOSED_HEADER,
    CLOSED_RENAMED_PROCEEDS_CSV,
    CLOSED_RENAMED_PROCEEDS_REFUSAL,
    CONF_DEC,
    OPEN_CSV,
    OPEN_RECASED_CSV,
    patch_pdftotext,
)
from tests._netbenefits_income import income_statement as _statement
from tests._netbenefits_statement_pdf import PARTICIPANT_NO as _PNO

_FIXTURES = Path(__file__).parent / "fixtures" / "netbenefits"

# Markers that must never reach a refusal (#1041): an OSError's strerror and
# file name, and a holder's name in a file's cells.
_STRERROR_MARKER = "Synthetic Strerror Marker"
_HOLDER_MARKER = "Synthetic Holder Two"
# An errno with no ``errno.errorcode`` name.
_UNMAPPED_ERRNO = 99999


def _write_pdfs(bundle: Path, names: list[str]) -> None:
    for name in names:
        (bundle / name).parent.mkdir(parents=True, exist_ok=True)
        (bundle / name).write_bytes(b"%PDF-1.4 placeholder")


def _assert_no_markers(message: str, *markers: str) -> None:
    """None of ``markers``, nor the two synthetic markers, appear in ``message``
    in any case."""
    for marker in (_STRERROR_MARKER, _HOLDER_MARKER, *markers):
        assert marker.casefold() not in message.casefold()


def test_load_bundle_optional_open_lots_accepts_no_open_csv(tmp_path: Path) -> None:
    # netbenefits-cg uses load_bundle(require_open_lots=False): a bundle
    # with only a closed-lots CSV (a full liquidation → no open lots) is accepted.
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "View closed lots.csv").write_text(CLOSED_EMPTY_CSV, encoding="utf-8")
    result = load_bundle(bundle, require_open_lots=False)
    assert result.open_lots == ()
    assert result.closed_lots == ()


def test_load_bundle_default_still_requires_open_lots(tmp_path: Path) -> None:
    # FA behaviour unchanged: the default requires the open-lots CSV.
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "View closed lots.csv").write_text(CLOSED_EMPTY_CSV, encoding="utf-8")
    with pytest.raises(NetBenefitsBundleError, match="open-lots CSV"):
        load_bundle(bundle)


def test_load_bundle_optional_open_lots_ignores_extra(tmp_path: Path) -> None:
    # When optional (CG), open-lots CSVs are ignored entirely — even >1 does not
    # refuse (CG never reads them), and open_lots comes back empty.
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "View open lots.csv").write_text(OPEN_CSV, encoding="utf-8")
    (bundle / "View open lots (1).csv").write_text(OPEN_CSV, encoding="utf-8")
    (bundle / "View closed lots.csv").write_text(CLOSED_EMPTY_CSV, encoding="utf-8")
    result = load_bundle(bundle, require_open_lots=False)
    assert result.open_lots == ()


def _bundle_of(tmp_path: Path, csvs: dict[str, str]) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name, text in csvs.items():
        (bundle / name).parent.mkdir(parents=True, exist_ok=True)
        (bundle / name).write_text(text, encoding="utf-8")
    return bundle


@pytest.mark.parametrize("require_open_lots", [True, False])
def test_load_bundle_refuses_a_closed_lots_near_miss(
    tmp_path: Path, require_open_lots: bool
) -> None:
    # #911: without the refusal the empty export is the only closed-lots CSV, so
    # the renamed export's sale would be dropped.
    bundle = _bundle_of(
        tmp_path,
        {
            "View open lots.csv": OPEN_CSV,
            "View closed lots.csv": CLOSED_EMPTY_CSV,
            "2026/View closed lots.csv": CLOSED_RENAMED_PROCEEDS_CSV,
        },
    )
    with pytest.raises(NetBenefitsBundleError) as caught:
        load_bundle(bundle, require_open_lots=require_open_lots)
    # #1041: the expected header is named and the renamed cell only counted.
    assert str(caught.value) == f"2026/View closed lots.csv {CLOSED_RENAMED_PROCEEDS_REFUSAL}"
    assert "proceeds (usd)" not in str(caught.value).casefold()


@pytest.mark.parametrize(
    ("header", "difference"),
    [
        pytest.param(
            # A holder's name where "Proceeds" belongs, and an extra "ANN" cell.
            f"Date acquired,Quantity,Date sold or transferred,{_HOLDER_MARKER},Cost basis,"
            "Gain/loss,Term,ANN",
            f"compared with the closed-lots header {CLOSED_HEADER}, missing: 'Proceeds'; "
            "unexpected: 2 column(s)",
            id="unexpected-cells",
        ),
        pytest.param(
            # Every closed-lots column, in another order: the old text printed the
            # row's own order.
            "Date acquired,Quantity,Date sold or transferred,Cost basis,Proceeds,Gain/loss,Term",
            f"it has the closed-lots columns, but not in the expected order {CLOSED_HEADER}",
            id="reordered",
        ),
    ],
)
def test_load_bundle_near_miss_refusal_prints_no_header_cell(
    tmp_path: Path, header: str, difference: str
) -> None:
    """#1041: a near-miss header's cells are file content; the refusal names the
    file by its relative path, the expected header (code constants) and the
    missing columns, and counts the unexpected ones."""
    bundle = _bundle_of(
        tmp_path,
        {"View closed lots.csv": CLOSED_EMPTY_CSV, "2026/lots.csv": f"{header}\n"},
    )
    with pytest.raises(NetBenefitsBundleError) as caught:
        load_bundle(bundle, require_open_lots=False)
    message = str(caught.value)
    assert message == (
        f"2026/lots.csv looks like a Fidelity lots export but matches neither lots header: "
        f"{difference}. If Fidelity changed the export, ibkr-fa must be updated to read it; "
        "otherwise move the file out of the input directory"
    )
    _assert_no_markers(message)
    assert re.search(r"(?i)\bANN\b", message) is None
    # The reordered row's own order is not printed either.
    assert "'Cost basis', 'Proceeds'" not in message


def test_load_bundle_ignores_an_open_lots_near_miss_only_when_open_lots_are_optional(
    tmp_path: Path,
) -> None:
    bundle = _bundle_of(
        tmp_path,
        {
            "View open lots.csv": OPEN_CSV,
            "View open lots (1).csv": OPEN_RECASED_CSV,
            "View closed lots.csv": CLOSED_EMPTY_CSV,
        },
    )
    assert load_bundle(bundle, require_open_lots=False).open_lots == ()
    with pytest.raises(NetBenefitsBundleError, match=r"^View open lots \(1\)\.csv looks like"):
        load_bundle(bundle)


@pytest.mark.parametrize(
    "header",
    [
        # No column is unique to closed lots, but one is not an
        # open-lots column either: it could be either export.
        "Date acquired,Quantity,Value (USD),Sale availability date",
        # Only columns both exports have.
        "Date acquired,Quantity,Cost basis,Gain/loss",
        # An open-lots column beside a closed-lots one.
        "Date acquired,Quantity,Proceeds,Sale availability date",
    ],
)
def test_load_bundle_refuses_a_near_miss_that_may_be_closed_lots_even_for_cg(
    tmp_path: Path, header: str
) -> None:
    bundle = _bundle_of(
        tmp_path,
        {"View closed lots.csv": CLOSED_EMPTY_CSV, "lots.csv": f"{header}\n"},
    )
    with pytest.raises(NetBenefitsBundleError, match=r"^lots\.csv looks like"):
        load_bundle(bundle, require_open_lots=False)


@pytest.mark.parametrize("require_open_lots", [True, False])
def test_load_bundle_still_ignores_unrelated_csvs(tmp_path: Path, require_open_lots: bool) -> None:
    bundle = _bundle_of(
        tmp_path,
        {
            "View open lots.csv": OPEN_CSV,
            "View closed lots.csv": CLOSED_EMPTY_CSV,
            "notes.csv": "foo,bar\n1,2\n",
            # A saved Schedule CG review CSV: "Holding period (days)" is not a lots column.
            "schedule-cg.csv": "Symbol,Open date (DD/MM/YYYY),Holding period (days),Quantity\n",
        },
    )
    assert load_bundle(bundle, require_open_lots=require_open_lots).closed_lots == ()


@pytest.mark.parametrize("require_open_lots", [True, False])
def test_load_bundle_refuses_an_unrelated_csv_it_cannot_read(
    tmp_path: Path, require_open_lots: bool
) -> None:
    """#1098: a CSV whose first record can't be read can't be ruled out as a
    lots export, so it refuses, named as listed from ``--input``. It used to
    be ignored: the reader repaired the record."""
    bundle = _bundle_of(
        tmp_path,
        {
            "View open lots.csv": OPEN_CSV,
            "View closed lots.csv": CLOSED_EMPTY_CSV,
            "notes/unrelated.csv": f'"{_HOLDER_MARKER}"x\n',
        },
    )
    with pytest.raises(ParseError) as caught:
        load_bundle(bundle, require_open_lots=require_open_lots)
    assert_malformed(
        caught.value,
        section="notes/unrelated.csv",
        row_index=0,
        line=1,
        reexport="re-export it from NetBenefits.",
        markers=(_HOLDER_MARKER,),
    )


# #1097: CSVs with markup. Privacy markers stay in file contents; file names stay neutral.
_MARKER = "Synthetic Marker Q"
_OPEN_HEADER_LINE, _OPEN_ROWS = OPEN_CSV.lstrip("\ufeff").split("\n", 1)
_CLOSED_HEADER_LINE = CLOSED_EMPTY_CSV.lstrip("\ufeff").split("\n", 1)[0]
_SALE = "Mar-31-2026,2.0000,Jun-15-2026,900.00,755.31,144.69,Short"
_FOOTER = ",\nThe values are displayed in USD\n"
_COMMENT_OPEN = f"<!-- {_HOLDER_MARKER}\n"
_COMMENT_CLOSE = f"--> {_MARKER}\n"
_CLOSED_HEADER_IN_COMMENT = (
    f"{_COMMENT_OPEN}{_CLOSED_HEADER_LINE}\n{_COMMENT_CLOSE}{_SALE}\n{_FOOTER}"
)
# The issue's examples: a Quantity of 1<span>0</span> with amounts that agree
# with 10 shares, and a comment from the end of one sale's row to a line after
# the next sale.
_OPEN_SPAN_IN_QUANTITY = OPEN_CSV.replace(
    "Mar-31-2026,4.6315,755.31,163.08,832.98,77.67,",
    "Mar-31-2026,1<span>0</span>,1630.80,163.08,1750.00,119.20,",
).replace("119.20,-,", f"119.20,<!-- {_HOLDER_MARKER} {_MARKER} -->,")
_CLOSED_COMMENT_ACROSS_SALES = (
    f"{_CLOSED_HEADER_LINE}\n{_SALE}<!-- {_HOLDER_MARKER}\n"
    f"Dec-31-2025,1.0000,Jun-15-2026,450.00,300.00,150.00,Short\n{_MARKER} -->\n{_FOOTER}"
)


def _markup_refusal(label: str) -> str:
    return (
        f"{label} holds markup ('<' or '>') other than the closed-lots header's documented "
        "<span> decoration, so it could hide a Fidelity lots export and can't be ignored. "
        "If Fidelity changed the export, ibkr-fa must be updated to read it; otherwise move "
        "the file out of the input directory"
    )


@pytest.mark.parametrize("require_open_lots", [True, False])
@pytest.mark.parametrize(
    ("name", "text"),
    [
        pytest.param("2026/lots.csv", _CLOSED_HEADER_IN_COMMENT, id="closed-header-in-comment"),
        pytest.param(
            "2026/lots.csv",
            f'<span title="{_HOLDER_MARKER}"\n{_CLOSED_HEADER_LINE}\n>\n'
            f"<!-- {_MARKER} -->\n{_SALE}\n{_FOOTER}",
            id="closed-header-in-partial-span",
        ),
        pytest.param(
            "2026/lots.csv",
            f'<div title="{_MARKER}">\n{_CLOSED_HEADER_IN_COMMENT}',
            id="div-then-comment",
        ),
        pytest.param(
            "2026/lots.csv",
            f"{_COMMENT_OPEN}{_OPEN_HEADER_LINE}\n{_CLOSED_HEADER_LINE}\n{_COMMENT_CLOSE}"
            f"{_SALE}\n{_FOOTER}",
            id="open-and-closed-headers-in-comment",
        ),
        pytest.param(
            "2026/lots.csv",
            f"<!--{_HOLDER_MARKER},{_OPEN_HEADER_LINE}-->\n<!-- {_MARKER} -->\n{_OPEN_ROWS}",
            id="open-header-wrapped-in-comment",
        ),
        pytest.param("notes.csv", f"foo,<{_HOLDER_MARKER}>\n1,{_MARKER}\n", id="unrelated-csv"),
        pytest.param("2026/lots.csv", _CLOSED_COMMENT_ACROSS_SALES, id="comment-across-sales"),
    ],
)
def test_load_bundle_refuses_a_csv_with_markup(
    tmp_path: Path, name: str, text: str, require_open_lots: bool
) -> None:
    bundle = _bundle_of(
        tmp_path,
        {"View open lots.csv": OPEN_CSV, "View closed lots.csv": CLOSED_EMPTY_CSV, name: text},
    )
    with pytest.raises(NetBenefitsBundleError) as caught:
        load_bundle(bundle, require_open_lots=require_open_lots)
    assert str(caught.value) == _markup_refusal(name)
    for marker in (_HOLDER_MARKER, _MARKER):
        assert marker.casefold() not in str(caught.value).casefold()


@pytest.mark.parametrize(
    ("name", "text"),
    [
        pytest.param(
            "2026/lots.csv",
            f"{_COMMENT_OPEN}{_OPEN_HEADER_LINE}\n{_COMMENT_CLOSE}{_OPEN_ROWS}",
            id="open-header-in-comment",
        ),
        pytest.param("View open lots.csv", _OPEN_SPAN_IN_QUANTITY, id="span-in-open-lots-cell"),
        pytest.param(
            "2026/lots.csv",
            f"{_COMMENT_OPEN}{_OPEN_HEADER_LINE.replace('Holding period', 'Holding  Period')}\n"
            f"{_COMMENT_CLOSE}{_OPEN_ROWS}",
            id="open-only-near-miss-in-comment",
        ),
    ],
)
def test_load_bundle_ignores_an_open_lots_only_markup_csv_only_when_open_lots_are_optional(
    tmp_path: Path, name: str, text: str
) -> None:
    bundle = _bundle_of(
        tmp_path,
        {"View open lots.csv": OPEN_CSV, "View closed lots.csv": CLOSED_EMPTY_CSV, name: text},
    )
    result = load_bundle(bundle, require_open_lots=False)
    assert (result.open_lots, result.closed_lots) == ((), ())
    with pytest.raises(NetBenefitsBundleError) as caught:
        load_bundle(bundle)
    assert str(caught.value) == _markup_refusal(name)
    for marker in (_HOLDER_MARKER, _MARKER):
        assert marker.casefold() not in str(caught.value).casefold()


def test_load_bundle_never_parses_a_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # load_bundle classifies a statement PDF but doesn't parse it; the
    # capital-gains runs parse statements through load_statements instead (#666).
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "View closed lots.csv").write_text(CLOSED_EMPTY_CSV, encoding="utf-8")
    _write_pdfs(bundle, ["confirmation.pdf", "statement.pdf"])
    patch_pdftotext(
        monkeypatch,
        {"confirmation.pdf": CONF_DEC, "statement.pdf": "STOCK PLAN SERVICES REPORT\n"},
    )

    def refuse(*_args: object, **_kwargs: object) -> StatementData:
        raise AssertionError("load_bundle parsed a statement")

    monkeypatch.setattr("ibkr_fa.parser.netbenefits.bundle.parse_statement", refuse)

    result = load_bundle(bundle, require_open_lots=False)

    assert list(result.confirmations_by_date) == [date(2025, 12, 31)]


_JAN = _statement(date(2026, 1, 1), date(2026, 1, 31))
_FEB = _statement(date(2026, 2, 1), date(2026, 2, 28))
_YEAR_END = _statement(date(2025, 1, 1), date(2025, 12, 31), report_type=ReportType.YEAR_END)


@pytest.mark.parametrize(
    ("include_year_end", "expected"),
    [(True, [_YEAR_END, _JAN, _FEB]), (False, [_JAN, _FEB])],
    ids=["with-year-end", "itemizing-only"],
)
def test_load_statements_sorts_by_period_and_filters_year_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_year_end: bool,
    expected: list[StatementData],
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    by_name = {"a-feb.pdf": _FEB, "b-jan.pdf": _JAN, "c-year-end.pdf": _YEAR_END}
    _write_pdfs(bundle, [*by_name, "confirmation.pdf"])
    patch_pdftotext(
        monkeypatch,
        {
            "a-feb.pdf": "STOCK PLAN SERVICES REPORT\n::a-feb.pdf::\n",
            "b-jan.pdf": "STOCK PLAN SERVICES REPORT\n::b-jan.pdf::\n",
            "c-year-end.pdf": "YEAR-END INVESTMENT REPORT\n::c-year-end.pdf::\n",
            "confirmation.pdf": CONF_DEC,
        },
    )
    calls: list[tuple[str | None, str]] = []

    def fake_parse(
        text: str, *, expected_participant_no: str | None = None, source: str = ""
    ) -> StatementData:
        calls.append((expected_participant_no, source))
        return by_name[text.split("::")[1]]

    monkeypatch.setattr("ibkr_fa.parser.netbenefits.bundle.parse_statement", fake_parse)

    statements = load_statements(bundle, _PNO, include_year_end=include_year_end)

    assert statements == expected
    assert calls == [(_PNO, f"statement {name}") for name in by_name]


def test_load_statements_refuses_a_non_directory(tmp_path: Path) -> None:
    not_dir = tmp_path / "nope.txt"
    not_dir.write_text("x", encoding="utf-8")
    with pytest.raises(NetBenefitsBundleError, match="input path is not a directory: "):
        load_statements(not_dir, _PNO, include_year_end=False)


# --- #1238: folders that can't be read or searched ----------------------------

_PERMISSIONS_ENFORCED = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)
# In the locked subfolder's and its file's names; never in a refusal.
_LOCKED_MARKER = "QZ1238"


def _record_renders(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record each PDF the loaders render; every PDF reads as a confirmation."""
    return patch_pdftotext(monkeypatch, defaultdict(lambda: CONF_DEC), by_source=True)


def _load(loader: str, bundle: Path) -> object:
    if loader == "load_bundle":
        return load_bundle(bundle, require_open_lots=False)
    return load_statements(bundle, _PNO, include_year_end=False)


@_PERMISSIONS_ENFORCED
@pytest.mark.parametrize("loader", ["load_bundle", "load_statements"])
def test_input_folder_check_raises_eacces_instead_of_calling_it_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, loader: str
) -> None:
    """#1238: the input-folder check raises EACCES instead of calling the folder missing."""
    blocked = tmp_path / "blocked"
    bundle = blocked / "bundle"
    bundle.mkdir(parents=True)
    blocked.chmod(0)
    request.addfinalizer(lambda: blocked.chmod(0o700))
    rendered = _record_renders(monkeypatch)
    with pytest.raises(PermissionError) as raised:
        _load(loader, bundle)
    assert type(raised.value) is PermissionError
    assert str(raised.value) == f"[Errno {errno.EACCES}] Permission denied: {str(bundle)!r}"
    assert rendered == []


@_PERMISSIONS_ENFORCED
@pytest.mark.parametrize("mode", [0o444, 0o000], ids=["listable-not-searchable", "unreadable"])
@pytest.mark.parametrize(
    ("loader", "suffix"), [("load_bundle", ".csv"), ("load_statements", ".pdf")]
)
def test_folder_walk_refuses_a_subfolder_it_cannot_read_instead_of_dropping_its_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    loader: str,
    suffix: str,
    mode: int,
) -> None:
    """#1238: the folder walk refuses a subfolder it can't read or search instead of
    dropping its files."""
    folder = f"{_LOCKED_MARKER} folder"
    bundle = _bundle_of(
        tmp_path,
        {"View closed lots.csv": CLOSED_EMPTY_CSV, f"{folder}/{_LOCKED_MARKER}{suffix}": ""},
    )
    # Without the locked folder this is a valid bundle with a PDF to render.
    _write_pdfs(bundle, ["confirmation.pdf"])
    locked = bundle / folder
    locked.chmod(mode)
    request.addfinalizer(lambda: locked.chmod(0o700))
    rendered = _record_renders(monkeypatch)
    with pytest.raises(NetBenefitsBundleError) as raised:
        _load(loader, bundle)
    error = raised.value
    assert type(error) is NetBenefitsBundleError
    assert str(error) == "could not read every folder and file in the input directory (EACCES)"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert rendered == []


# --- #1041: relative labels, unreadable files and pdftotext failures ----------


def _jan_statement(participant_no: str) -> str:
    """The committed January statement's text with ``participant_no``."""
    text = (_FIXTURES / "statement-jan.txt").read_text(encoding="utf-8")
    assert "I99999999" in text
    return text.replace("I99999999", participant_no)


@pytest.mark.parametrize("refusing", ["first", "second"])
def test_same_basename_statements_are_labelled_by_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusing: str
) -> None:
    """Two nested statements share a basename; the real parser's refusal names the
    one it refused by its path relative to the bundle, and neither Participant
    Number."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_pdfs(bundle, ["first/statement.pdf", "second/statement.pdf"])
    patch_pdftotext(
        monkeypatch,
        {
            f"{name}/statement.pdf": _jan_statement(
                "I12345678" if name == refusing else "I00000001"
            )
            for name in ("first", "second")
        },
        by_source=True,
    )
    with pytest.raises(ParseError) as caught:
        load_statements(bundle, "I00000001", include_year_end=False)
    error = caught.value
    assert error.section == f"statement {refusing}/statement.pdf"
    assert error.column == "Participant Number"
    assert error.row_index == 0
    assert error.message == (
        "the Participant Number differs from netbenefits.participant_no in user.toml; "
        "the whole document was checked"
    )
    assert error.raw_value == ""
    _assert_no_markers(str(error), "I12345678", "I00000001")


@pytest.mark.parametrize("refusing", ["first", "second"])
@pytest.mark.parametrize("loader", ["load_bundle", "load_statements"])
def test_classify_refusal_names_a_same_basename_pdf_by_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loader: str, refusing: str
) -> None:
    """Both loaders classify every PDF; the one that matches neither title is named
    by its path relative to the bundle, not its shared basename."""
    bundle = _bundle_of(tmp_path, {"View closed lots.csv": CLOSED_EMPTY_CSV})
    _write_pdfs(bundle, ["first/statement.pdf", "second/statement.pdf"])
    patch_pdftotext(
        monkeypatch,
        {
            f"{name}/statement.pdf": (
                f"{_HOLDER_MARKER}\n" if name == refusing else "STOCK PLAN SERVICES REPORT\n"
            )
            for name in ("first", "second")
        },
        by_source=True,
    )
    # load_bundle never parses a statement; load_statements reaches the refusal
    # first if the refusing PDF sorts first, else after parsing the first one.
    monkeypatch.setattr(
        "ibkr_fa.parser.netbenefits.bundle.parse_statement",
        lambda *_args, **_kwargs: _statement(date(2026, 1, 1), date(2026, 1, 31)),
    )
    with pytest.raises(NetBenefitsBundleError) as caught:
        if loader == "load_bundle":
            load_bundle(bundle, require_open_lots=False)
        else:
            load_statements(bundle, _PNO, include_year_end=False)
    assert str(caught.value) == (
        f"cannot classify PDF {refusing}/statement.pdf: it matches neither confirmation nor "
        "statement markers; remove non-NetBenefits PDFs from the input directory"
    )
    # Classification already hid file contents; the new assertion above pins the relative label.
    _assert_no_markers(str(caught.value))


def test_classify_refusal_names_a_pdf_matching_both_titles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle_of(tmp_path, {"View closed lots.csv": CLOSED_EMPTY_CSV})
    _write_pdfs(bundle, ["2026/both.pdf"])
    patch_pdftotext(
        monkeypatch, {"2026/both.pdf": f"{CONF_DEC}\nSTOCK PLAN SERVICES REPORT\n"}, by_source=True
    )
    with pytest.raises(NetBenefitsBundleError) as caught:
        load_bundle(bundle, require_open_lots=False)
    assert str(caught.value) == (
        "cannot classify PDF 2026/both.pdf: it matches both confirmation and statement "
        "markers; remove non-NetBenefits PDFs from the input directory"
    )


def test_nested_confirmation_refusal_names_its_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #914's signed gain ("$74.34-") makes the real confirmation parser refuse.
    signed_gain = CONF_DEC.replace("$74.34\n", "$74.34-\n")
    assert signed_gain != CONF_DEC
    bundle = _bundle_of(tmp_path, {"View closed lots.csv": CLOSED_EMPTY_CSV})
    _write_pdfs(bundle, ["2026/espp-purchase.pdf"])
    patch_pdftotext(monkeypatch, {"2026/espp-purchase.pdf": signed_gain}, by_source=True)
    with pytest.raises(ParseError) as caught:
        load_bundle(bundle, require_open_lots=False)
    assert str(caught.value) == (
        "ParseError in section 'confirmation 2026/espp-purchase.pdf' at row 0, column 'Gain': "
        "expected exactly one Gain; found 0"
    )


def test_two_same_basename_confirmations_on_one_date_name_both_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Names both confirmations by their relative paths, never the purchase date
    they share (DEC/31/2025)."""
    bundle = _bundle_of(tmp_path, {"View closed lots.csv": CLOSED_EMPTY_CSV})
    _write_pdfs(bundle, ["a/espp-purchase.pdf", "b/espp-purchase.pdf"])
    patch_pdftotext(
        monkeypatch,
        {"a/espp-purchase.pdf": CONF_DEC, "b/espp-purchase.pdf": CONF_DEC},
        by_source=True,
    )
    with pytest.raises(NetBenefitsBundleError) as caught:
        load_bundle(bundle, require_open_lots=False)
    message = str(caught.value)
    assert message == (
        "confirmations a/espp-purchase.pdf and b/espp-purchase.pdf share a purchase date; "
        "the bundle is ambiguous"
    )
    for date_form in ("2025", "12-31", "12/31", "dec"):
        assert date_form not in message.casefold()


@pytest.mark.parametrize(
    ("csvs", "require_open_lots", "expected"),
    [
        # The "none" forms read as before #1041; they pin the refusal's full text.
        pytest.param(
            {"View closed lots.csv": CLOSED_EMPTY_CSV},
            True,
            "expected exactly one open-lots CSV in the input directory; found 0 (none)",
            id="no-open-lots",
        ),
        pytest.param(
            {
                "View closed lots.csv": CLOSED_EMPTY_CSV,
                "View open lots.csv": OPEN_CSV,
                "2026/View open lots.csv": OPEN_CSV,
            },
            True,
            "expected exactly one open-lots CSV in the input directory; found 2 "
            "(2026/View open lots.csv, View open lots.csv)",
            id="two-open-lots",
        ),
        pytest.param(
            {"View open lots.csv": OPEN_CSV},
            False,
            "expected exactly one closed-lots CSV (required; its absence leaves Dec-31 "
            "quantity unprovable) in the input directory; found 0 (none)",
            id="no-closed-lots",
        ),
        pytest.param(
            {
                "first/View closed lots.csv": CLOSED_EMPTY_CSV,
                "second/View closed lots.csv": CLOSED_EMPTY_CSV,
            },
            False,
            "expected exactly one closed-lots CSV (required; its absence leaves Dec-31 "
            "quantity unprovable) in the input directory; found 2 "
            "(first/View closed lots.csv, second/View closed lots.csv)",
            id="two-same-basename-closed-lots",
        ),
    ],
)
def test_require_one_lists_csvs_by_relative_path(
    tmp_path: Path, csvs: dict[str, str], require_open_lots: bool, expected: str
) -> None:
    bundle = _bundle_of(tmp_path, csvs)
    with pytest.raises(NetBenefitsBundleError) as caught:
        load_bundle(bundle, require_open_lots=require_open_lots)
    assert str(caught.value) == expected


def _fail_reads(
    monkeypatch: pytest.MonkeyPatch, target: Path, error: Exception, *, after: int
) -> None:
    """Make ``target.read_text`` raise ``error`` once it has been read ``after``
    times (0: the lots sniff; 1: the parse that follows it). Other paths read
    as usual."""
    real_read_text = Path.read_text
    reads = 0

    def read_text(self: Path, *args, **kwargs) -> str:
        nonlocal reads
        if self == target:
            reads += 1
            if reads > after:
                raise error
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


# Which read fails: ``detect_fidelity_lots_csv``'s sniff, labelled by the bare
# relative path, or ``read_open_lots`` / ``read_closed_lots``'s, labelled
# "<kind> <relative path>".
_LOTS_READS = [
    pytest.param("sub/View open lots.csv", 0, "sub/View open lots.csv", id="open-sniff"),
    pytest.param("sub/View open lots.csv", 1, "open lots sub/View open lots.csv", id="open-parse"),
    pytest.param("sub/View closed lots.csv", 0, "sub/View closed lots.csv", id="closed-sniff"),
    pytest.param(
        "sub/View closed lots.csv", 1, "closed lots sub/View closed lots.csv", id="closed-parse"
    ),
]


def _nested_lots_bundle(tmp_path: Path) -> Path:
    return _bundle_of(
        tmp_path,
        {"sub/View open lots.csv": OPEN_CSV, "sub/View closed lots.csv": CLOSED_EMPTY_CSV},
    )


@pytest.mark.parametrize(("name", "after", "section"), _LOTS_READS)
@pytest.mark.parametrize(
    ("code", "errno_text"),
    [
        pytest.param(errno.EACCES, "EACCES", id="eacces"),
        pytest.param(None, "unknown error", id="no-errno"),
        pytest.param(_UNMAPPED_ERRNO, "unknown error", id="unmapped-errno"),
    ],
)
def test_unreadable_nested_lots_csv_names_its_relative_path_and_errno(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    after: int,
    section: str,
    code: int | None,
    errno_text: str,
) -> None:
    """The refusal names the file by its relative path and the errno by name; never
    the OSError's text, its file name or the absolute path."""
    assert _UNMAPPED_ERRNO not in errno.errorcode
    bundle = _nested_lots_bundle(tmp_path)
    _fail_reads(
        monkeypatch,
        bundle / name,
        OSError(code, _STRERROR_MARKER, f"{_HOLDER_MARKER}/x"),
        after=after,
    )
    with pytest.raises(ParseError) as caught:
        load_bundle(bundle)
    error = caught.value
    assert str(error) == (
        f"ParseError in section {section!r} at row 0: could not read the file ({errno_text})"
    )
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_no_markers(str(error), str(tmp_path))


@pytest.mark.parametrize(("name", "after", "section"), _LOTS_READS)
def test_non_utf8_nested_lots_csv_names_its_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, after: int, section: str
) -> None:
    """A fixed encoding refusal, labelled by the file's relative path; never the
    decoder's text or the absolute path."""
    bundle = _nested_lots_bundle(tmp_path)
    if after == 0:
        # A real non-UTF-8 file fails the sniff.
        (bundle / name).write_bytes(f"{_HOLDER_MARKER}\xff\n".encode("latin-1"))
    else:
        # The sniff reads the file; the parse read then fails to decode.
        _fail_reads(
            monkeypatch,
            bundle / name,
            UnicodeDecodeError("utf-8", _HOLDER_MARKER.encode(), 0, 1, _STRERROR_MARKER),
            after=after,
        )
    with pytest.raises(ParseError) as caught:
        load_bundle(bundle)
    error = caught.value
    assert str(error) == f"ParseError in section {section!r} at row 0: the file is not UTF-8 text"
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_no_markers(str(error), str(tmp_path))


_PDF_NAME = "sub/statement.pdf"

# A failure ``subprocess.run`` raises (built from the PDF's absolute path), and
# the message ``pdftotext_layout`` refuses with.
_PDFTOTEXT_FAILURES = [
    pytest.param(
        lambda pdf: subprocess.CalledProcessError(
            1,
            ["pdftotext", "-enc", "UTF-8", "-layout", str(pdf), "-"],
            output="",
            stderr=f"{_STRERROR_MARKER}: could not open {pdf} for {_HOLDER_MARKER}",
        ),
        "pdftotext failed",
        id="pdftotext-failed",
    ),
    pytest.param(
        lambda _pdf: OSError(errno.EACCES, _STRERROR_MARKER, f"{_HOLDER_MARKER}/pdftotext"),
        "could not run pdftotext (EACCES)",
        id="oserror-eacces",
    ),
    pytest.param(
        lambda _pdf: OSError(None, _STRERROR_MARKER, f"{_HOLDER_MARKER}/pdftotext"),
        "could not run pdftotext (unknown error)",
        id="oserror-no-errno",
    ),
    pytest.param(
        lambda _pdf: OSError(_UNMAPPED_ERRNO, _STRERROR_MARKER, f"{_HOLDER_MARKER}/pdftotext"),
        "could not run pdftotext (unknown error)",
        id="oserror-unmapped-errno",
    ),
    pytest.param(
        lambda _pdf: FileNotFoundError(
            errno.ENOENT, _STRERROR_MARKER, f"{_HOLDER_MARKER}/pdftotext"
        ),
        "'pdftotext' not found on PATH (ENOENT); install poppler-utils to read NetBenefits PDFs",
        id="not-found-enoent",
    ),
    pytest.param(
        lambda _pdf: FileNotFoundError(None, _STRERROR_MARKER, f"{_HOLDER_MARKER}/pdftotext"),
        "'pdftotext' not found on PATH (unknown error); install poppler-utils to read "
        "NetBenefits PDFs",
        id="not-found-no-errno",
    ),
    pytest.param(
        lambda _pdf: FileNotFoundError(
            _UNMAPPED_ERRNO, _STRERROR_MARKER, f"{_HOLDER_MARKER}/pdftotext"
        ),
        "'pdftotext' not found on PATH (unknown error); install poppler-utils to read "
        "NetBenefits PDFs",
        id="not-found-unmapped-errno",
    ),
    pytest.param(
        lambda pdf: subprocess.TimeoutExpired(
            ["pdftotext", str(pdf)], 30, output=_HOLDER_MARKER, stderr=_STRERROR_MARKER
        ),
        f"pdftotext timed out reading {_PDF_NAME}",
        id="timeout",
    ),
    pytest.param(
        lambda _pdf: UnicodeDecodeError(
            "utf-8", f"{_HOLDER_MARKER}\xff".encode("latin-1"), 20, 21, _STRERROR_MARKER
        ),
        "pdftotext output was not valid UTF-8",
        id="non-utf8-output",
    ),
]


@pytest.mark.parametrize(("make_failure", "message"), _PDFTOTEXT_FAILURES)
@pytest.mark.parametrize("loader", ["load_bundle", "load_statements"])
def test_pdftotext_failure_names_the_pdf_by_relative_path_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loader: str,
    make_failure: Callable[[Path], Exception],
    message: str,
) -> None:
    """The real ``pdftotext_layout`` runs on a nested PDF and ``subprocess.run``
    fails. The refusal's section is the PDF's path relative to the bundle; it
    prints no absolute path, no stderr and no exception text or file name."""
    bundle = _bundle_of(tmp_path, {"View closed lots.csv": CLOSED_EMPTY_CSV})
    _write_pdfs(bundle, [_PDF_NAME])
    failure = make_failure(bundle / _PDF_NAME)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ParseError) as caught:
        if loader == "load_bundle":
            load_bundle(bundle, require_open_lots=False)
        else:
            load_statements(bundle, _PNO, include_year_end=False)
    error = caught.value
    assert str(error) == f"ParseError in section {_PDF_NAME!r} at row 0: {message}"
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_no_markers(str(error), str(tmp_path))
