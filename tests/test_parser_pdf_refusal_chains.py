"""#1209: a ``pdftotext`` failure reaches each priority-source PDF reader's
refusal without chaining the subprocess's output, stderr or undecodable bytes.

``subprocess.run`` is stubbed to raise each failure as it would, carrying a
neutral content marker; the real ``pdftotext_layout`` runs under each public
reader. The refusal keeps its class, fields and text, holds no marker anywhere
in its exception graph and chains nothing. The PDF's own path is the user's
input and may show; the markers never sit in it.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.fidelity_cma import load_fidelity_cma_statements
from ibkr_fa.parser.fidelity_cma_1042s import read_1042s_packet
from ibkr_fa.parser.netbenefits.bundle import load_bundle, load_statements
from ibkr_fa.parser.robinhood_1042s import read_robinhood_1042s_packet
from ibkr_fa.parser.robinhood_statement.parser import (
    read_robinhood_statement,
    read_robinhood_statement_kind,
)
from tests._crash_reports import assert_unchained_refusal
from tests._fidelity_cma import _ACCOUNT
from tests._netbenefits import CLOSED_EMPTY_CSV
from tests._netbenefits_statement_pdf import PARTICIPANT_NO as _PNO

# Content markers each failure carries: a timeout's captured output and
# stderr, a failed run's output and stderr, and the bytes that didn't decode.
_TIMEOUT_OUTPUT = "QZPDFOUTPUT"
_TIMEOUT_STDERR = "QZPDFTIMEOUTERR"
_FAILED_OUTPUT = "QZPDFFAILOUT"
_FAILED_STDERR = "QZPDFSTDERR"
_UNDECODABLE = "QZPDFBYTES"

_UNDECODABLE_BYTES = f"page one {_UNDECODABLE} \xff".encode("latin-1")
_BAD_BYTE = _UNDECODABLE_BYTES.index(b"\xff")


def _timeout(cmd: list[str]) -> Exception:
    return subprocess.TimeoutExpired(
        cmd,
        30,
        output=f"page one {_TIMEOUT_OUTPUT}".encode(),
        stderr=f"Syntax Warning: {_TIMEOUT_STDERR}".encode(),
    )


def _failed(cmd: list[str]) -> Exception:
    return subprocess.CalledProcessError(
        1, cmd, output=f"page one {_FAILED_OUTPUT}", stderr=f"Syntax Error: {_FAILED_STDERR}"
    )


def _undecodable(_cmd: list[str]) -> Exception:
    return UnicodeDecodeError(
        "utf-8", _UNDECODABLE_BYTES, _BAD_BYTE, _BAD_BYTE + 1, "invalid start byte"
    )


# A failure ``subprocess.run`` raises (built from the command it was given),
# the markers it carries, and the refusal's message for a given source label.
_FAILURES = [
    pytest.param(
        _timeout,
        (_TIMEOUT_OUTPUT, _TIMEOUT_STDERR),
        lambda source: f"pdftotext timed out reading {source}",
        id="timeout",
    ),
    pytest.param(
        _failed,
        (_FAILED_OUTPUT, _FAILED_STDERR),
        lambda _source: "pdftotext failed",
        id="failed",
    ),
    pytest.param(
        _undecodable,
        (_UNDECODABLE,),
        lambda _source: "pdftotext output was not valid UTF-8",
        id="not-utf8",
    ),
]

_PDF_NAME = "statement.pdf"


def _pdf_folder(tmp_path: Path, *names: str) -> Path:
    """A folder holding the NetBenefits closed-lots CSV and placeholder PDFs."""
    folder = tmp_path / "input"
    folder.mkdir()
    (folder / "View closed lots.csv").write_text(CLOSED_EMPTY_CSV, encoding="utf-8")
    for name in names:
        (folder / name).write_bytes(b"%PDF-1.4 placeholder")
    return folder


def _read_robinhood_kind(folder: Path) -> object:
    return read_robinhood_statement_kind(folder / _PDF_NAME)


def _read_robinhood_statement(folder: Path) -> object:
    return read_robinhood_statement(folder / _PDF_NAME)


def _read_robinhood_1042s(folder: Path) -> object:
    return read_robinhood_1042s_packet(folder / _PDF_NAME)


def _load_netbenefits_bundle(folder: Path) -> object:
    return load_bundle(folder, require_open_lots=False)


def _load_netbenefits_statements(folder: Path) -> object:
    return load_statements(folder, _PNO, include_year_end=False)


def _load_cma_statements(folder: Path) -> object:
    return load_fidelity_cma_statements(
        folder, expected_account_id=_ACCOUNT, spaxx_isin="US99971W1062"
    )


def _read_cma_1042s(folder: Path) -> object:
    return read_1042s_packet(folder / _PDF_NAME)


# Each public reader over the folder, and the source label it passes.
_CALLERS = [
    pytest.param(_read_robinhood_kind, "Robinhood bundle discovery", id="robinhood-kind"),
    pytest.param(
        _read_robinhood_statement, "Robinhood brokerage statement", id="robinhood-statement"
    ),
    pytest.param(_read_robinhood_1042s, "Robinhood Form 1042-S packet", id="robinhood-1042s"),
    pytest.param(_load_netbenefits_bundle, _PDF_NAME, id="netbenefits-bundle"),
    pytest.param(_load_netbenefits_statements, _PDF_NAME, id="netbenefits-statements"),
    pytest.param(_load_cma_statements, "Fidelity CMA statement PDF", id="cma-statements"),
    pytest.param(_read_cma_1042s, "Fidelity CMA Form 1042-S packet", id="cma-1042s"),
]


def _stub_run(
    monkeypatch: pytest.MonkeyPatch, failures: dict[str, Callable[[list[str]], Exception]]
) -> None:
    """Make ``subprocess.run`` raise the failure keyed by the PDF's file name."""

    def run(args: list[str], **_kwargs: object) -> NoReturn:
        raise failures[Path(args[4]).name](args)

    monkeypatch.setattr(subprocess, "run", run)


def _assert_refusal(
    error: ParseError, section: str, message: str, markers: tuple[str, ...]
) -> None:
    assert_unchained_refusal(
        error,
        ParseError(section=section, row_index=0, column=None, message=message, raw_value=""),
        f"ParseError in section {section!r} at row 0: {message}",
        markers=markers,
    )


@pytest.mark.parametrize(("make_failure", "markers", "message"), _FAILURES)
@pytest.mark.parametrize(("read", "section"), _CALLERS)
def test_pdftotext_failure_refuses_without_its_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read: Callable[[Path], object],
    section: str,
    make_failure: Callable[[list[str]], Exception],
    markers: tuple[str, ...],
    message: Callable[[str], str],
) -> None:
    folder = _pdf_folder(tmp_path, _PDF_NAME)
    _stub_run(monkeypatch, {_PDF_NAME: make_failure})
    with pytest.raises(ParseError) as caught:
        read(folder)
    _assert_refusal(caught.value, section, message(section), markers)


@pytest.mark.parametrize(
    ("read", "section"),
    [
        pytest.param(_load_netbenefits_bundle, "a.pdf", id="netbenefits-bundle"),
        pytest.param(_load_netbenefits_statements, "a.pdf", id="netbenefits-statements"),
        pytest.param(_load_cma_statements, "Fidelity CMA statement PDF", id="cma-statements"),
    ],
)
def test_first_pdf_failure_refuses_before_the_next_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read: Callable[[Path], object],
    section: str,
) -> None:
    """Two PDFs fail differently: the first in file order, a timeout, still
    refuses, and its content doesn't reach the refusal."""
    folder = _pdf_folder(tmp_path, "a.pdf", "b.pdf")
    _stub_run(monkeypatch, {"a.pdf": _timeout, "b.pdf": _failed})
    with pytest.raises(ParseError) as caught:
        read(folder)
    _assert_refusal(
        caught.value,
        section,
        f"pdftotext timed out reading {section}",
        (_TIMEOUT_OUTPUT, _TIMEOUT_STDERR),
    )
