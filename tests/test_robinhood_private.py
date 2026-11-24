"""The shared private Robinhood loader reads one root and refuses partial data (#838);
the PDF checks skip when their folders hold no PDF to check (#1739)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests import _robinhood_private_bundles as private_bundles
from tests._private_checks import private_test
from tests._robinhood_private import load_private_accounts_reconciliation, private_robinhood_pdfs
from tests._robinhood_refusals import CRYPTO_REFUSAL

_ENV = "IBKR_FA_PRIVATE_DATA"
_PARTS = (
    "statements",
    "report/individual.csv",
    "report/managed-individual.csv",
    "report/ranges.toml",
)
_ROLE_FOLDERS = {"statement": "statements", "tax": "tax"}


@pytest.fixture
def robinhood_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Select a tmp private root through the variable, from an unrelated cwd."""
    root = tmp_path / "private"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv(_ENV, str(root))
    monkeypatch.chdir(elsewhere)
    load_private_accounts_reconciliation.cache_clear()
    yield root / "rh"
    load_private_accounts_reconciliation.cache_clear()


def _outcome() -> tuple[type[BaseException], str]:
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as caught:
        load_private_accounts_reconciliation()
    return caught.type, str(caught.value)


@pytest.mark.parametrize("missing", _PARTS)
def test_loader_fails_when_one_part_of_the_dataset_is_missing(
    robinhood_root: Path, missing: str
) -> None:
    for part in _PARTS:
        path = robinhood_root / part
        if part == missing:
            continue
        if part == "statements":
            path.mkdir(parents=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
    assert _outcome() == (
        pytest.fail.Exception,
        f"private dataset {', '.join(f'rh/{part}' for part in _PARTS)} is incomplete; "
        f"missing rh/{missing}",
    )


def test_loader_skips_when_the_whole_dataset_is_absent(robinhood_root: Path) -> None:
    assert _outcome()[0] is pytest.skip.Exception


@pytest.mark.parametrize("role", _ROLE_FOLDERS)
def test_pdf_lookup_skips_when_the_role_folder_holds_no_pdf(
    robinhood_root: Path, role: str
) -> None:
    """Only the other role's folder holds a PDF, so the lookup checks the
    role's own PDFs, not every PDF under ``rh``."""
    for folder in _ROLE_FOLDERS.values():
        (robinhood_root / folder).mkdir(parents=True)
    [other] = (folder for name, folder in _ROLE_FOLDERS.items() if name != role)
    (robinhood_root / other / "sample.pdf").write_bytes(b"")
    with pytest.raises(pytest.skip.Exception) as caught:
        private_robinhood_pdfs(role)
    assert str(caught.value) == f"private dataset rh/<{role} folder> holds no PDF"


@pytest.mark.parametrize("role", _ROLE_FOLDERS)
def test_pdf_lookup_returns_the_pdf_in_the_role_folder(robinhood_root: Path, role: str) -> None:
    folder = robinhood_root / _ROLE_FOLDERS[role]
    folder.mkdir(parents=True)
    pdf = folder / "sample.pdf"
    pdf.write_bytes(b"")
    assert private_robinhood_pdfs(role) == (robinhood_root, [pdf])


def test_bundle_check_skips_when_no_statement_pdf_is_a_brokerage_statement(
    robinhood_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    crypto = robinhood_root / "statements" / "crypto"
    crypto.mkdir(parents=True)
    (crypto / "sample.pdf").write_bytes(b"")
    monkeypatch.setattr(private_bundles, "read_private_statement", lambda _path: CRYPTO_REFUSAL)
    with pytest.raises(pytest.skip.Exception) as caught:
        private_test(private_bundles.check_private_statement_series)()
    assert str(caught.value) == (
        "private dataset rh/<statement folder> holds no brokerage statement"
    )
