"""A whitespace-only Robinhood Table A3 address or ZIP is manual work (#1391).

Robinhood's ``a3-identity`` task now comes from the shared A3 gap check
(:func:`~ibkr_fa.compute.fa_a3.fa_a3_manual_fill_gaps`), which counts a
whitespace-only cell as blank, as IBKR's ``schedule-fa`` does. On main
Robinhood's own copy tested ``not row.zip_code``, so a configured
``zip = " "`` filed a blank upload cell and ``robinhood-fa`` exited 0, and
``robinhood-all`` failed its Schedule FA leg on the automatic JSON (exit 4).
Now both report the ``a3-identity`` action and exit 3; the files are the
complete run's with the ZIP cells as main writes them. Since #1483 the action
names the blank ZIP Code, what fills it and the holding.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ibkr_fa.compute.robinhood_fa import RobinhoodFaManualKind, compute_robinhood_fa
from ibkr_fa.compute.robinhood_identity import (
    RobinhoodEntityRoute,
    RobinhoodFoundEntityAddress,
    RobinhoodUnverifiedEntityAddress,
    merge_robinhood_entity_overrides,
    robinhood_entity_override_supersession_warnings,
    robinhood_unverified_entity_addresses,
)
from ibkr_fa.config import FaEntityOverride, load_user_config
from ibkr_fa.types import Isin
from tests._fa_a3_name_runs import A3_ASCII_REFUSAL as _ASCII
from tests._fa_a3_name_runs import output_table as _table
from tests._fa_a3_name_runs import robinhood_all_legs as _legs
from tests._fa_cli_runs import CliRun as _Run
from tests._fa_cli_runs import merge_entity_block, robinhood_run
from tests._portal_parity import fix_xlsx_clock
from tests._robinhood_a3_exact import SYMBOL
from tests._robinhood_a3_load import load_two_accounts
from tests._robinhood_fa_actions import ZIP, listed
from tests._robinhood_fa_address import ADDRESS_SUPERSEDED_ONE
from tests._robinhood_fa_inputs import FA_ISIN as _ISIN
from tests._robinhood_fa_inputs import FlatFx as _FlatFx
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_fa_inputs import fa_config as _config
from tests._robinhood_fa_inputs import fa_entity as _entity
from tests._robinhood_fa_inputs import fa_income as _income
from tests._robinhood_fa_inputs import fa_options as _options
from tests._robinhood_fa_inputs import fa_paired as _paired
from tests._robinhood_fa_inputs import fa_price_series as _prices
from tests._robinhood_security import configured_securities, robinhood_security

_ZIP_COLUMN = "ZIP Code"
_COMPLETE_ZIP = "99999"
# An empty ZIP was already manual on main: the control for each case.
_BLANK_ZIPS = {"empty": "", "space": " ", "no-break-space": "\u00a0"}
_SUFFIX = {"filing-csv": "csv", "review-csv": "csv", "xlsx": "xlsx"}
# #1483: the blank-ZIP cause and remedy line, then the bundle's holding.
_ACTION = listed(ZIP, f"{SYMBOL} ({_ISIN})")


# ---- Compute: the shared rule -----------------------------------------------------


@pytest.mark.parametrize(
    ("address", "zip_code", "identity_task"),
    [
        pytest.param("One Synthetic Way", " ", True, id="space-zip"),
        pytest.param("One Synthetic Way", "\u00a0", True, id="no-break-space-zip"),
        pytest.param("   ", "99990", True, id="spaces-address"),
        pytest.param("\u00a0", "99990", True, id="no-break-space-address"),
        pytest.param("One Synthetic Way", "", True, id="empty-zip"),
        pytest.param("", "99990", True, id="empty-address"),
        pytest.param("One Synthetic Way", "99990", False, id="complete"),
    ],
)
def test_a_whitespace_only_address_or_zip_needs_identity_work(
    address: str, zip_code: str, *, identity_task: bool
) -> None:
    """The row keeps the cell as given; a whitespace-only one, like an empty
    one, gives the security one ``a3-identity`` task and makes the result
    ineligible for CBDT JSON. No config path yields a whitespace-only address,
    so this is its only test."""
    paired, accounts = _paired(sold=False, dividend=False)
    config = _config(accounts)

    result = compute_robinhood_fa(
        paired,
        config=config,
        securities=configured_securities(config),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides={_ISIN: replace(_entity()[_ISIN], address=address, zip_code=zip_code)},
    )

    assert [(row.address_of_entity, row.zip_code) for row in result.a3_rows] == [
        (address, zip_code)
    ]
    tasks = [item for item in result.manual_work if item.kind is RobinhoodFaManualKind.A3_IDENTITY]
    expected = [((0,), (result.a3[0].lot_id,))] if identity_task else []
    assert [(item.security_config_indices, item.lot_ids) for item in tasks] == expected
    assert result.cbdt_json_eligible is not identity_task


# ---- CLI: the issue's [[fa.entities]] block ----------------------------------------


def _entity_block(zip_code: str) -> str:
    """The issue's entity block for the bundle's security, with ``zip_code``."""
    return (
        "\n[[fa.entities]]\n"
        f'isin = "{_ISIN}"\n'
        'address = "1 Example Way Example City"\n'
        f'zip = "{zip_code}"\n'
        "prefer_configured_address = true\n"
    )


def _robinhood(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    label: str,
    zip_code: str,
    command: str,
    *extra: str,
) -> _Run:
    """``command`` on the #1243 two-account bundle (one buy of 2 shares, held at
    Dec 31), whose user.toml adds the issue's ``[[fa.entities]]`` block with
    ``zip_code``. The block is loaded and merged over the bundle's automatic
    entity, as every Robinhood run does."""
    run_dir = tmp_path / label
    run_dir.mkdir()
    bundle = load_two_accounts(monkeypatch, run_dir, _buy("2"))
    merge_entity_block(bundle, run_dir, _entity_block(zip_code), tuple(_entity().values()))
    assert bundle.entity_overrides[_ISIN].zip_code == zip_code
    return robinhood_run(run_dir, capsys, command, *extra)


def _assert_only_zips_differ(blank: Path, complete: Path, zip_code: str) -> None:
    """``blank`` holds ``complete``'s files: in the A3 file the one ZIP Code
    cell holds ``zip_code`` (empty in the upload CSV, which collapses
    whitespace; an empty workbook cell reads back as ``None``) where
    ``complete`` has 99999, and every other file is byte for byte the same."""
    names = sorted(path.name for path in complete.iterdir())
    assert sorted(path.name for path in blank.iterdir()) == names
    (a3_file,) = (name for name in names if name.startswith("schedule-fa-a3."))
    for file_name in names:
        if file_name != a3_file:
            assert (blank / file_name).read_bytes() == (complete / file_name).read_bytes(), (
                file_name
            )
    expected = _table(complete / a3_file)
    column = expected[0].index(_ZIP_COLUMN)
    assert [row[column] for row in expected[1:]] == [_COMPLETE_ZIP]
    filed = "" if expected[0][0] == "Country/Region name" else zip_code
    expected[1][column] = None if filed == "" and a3_file.endswith(".xlsx") else filed
    assert _table(blank / a3_file) == expected


_FA_CASES = [
    pytest.param(fmt, label, id=f"{fmt}-{label}")
    for fmt in _SUFFIX
    for label in _BLANK_ZIPS
    if (fmt, label) != ("filing-csv", "no-break-space")
]


@pytest.mark.parametrize(("fmt", "label"), _FA_CASES)
def test_robinhood_fa_reports_a_blank_zip_as_identity_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fmt: str,
    label: str,
) -> None:
    """With ``zip = "99999"`` the leg is ``ok`` and exits 0. With an empty or
    whitespace-only ZIP it is ``manual`` with one ``a3-identity`` action and
    exits 3; for a whitespace-only one main reported ``ok`` and exited 0. The
    files are the complete run's with the ZIP cell blank in the upload and as
    configured in review."""
    fix_xlsx_clock(monkeypatch)
    args = ("robinhood-fa", "--format", fmt)
    complete = _robinhood(tmp_path, monkeypatch, capsys, "complete", _COMPLETE_ZIP, *args)
    assert complete.rc == 0
    assert complete.out.startswith("Robinhood schedule-fa: ok\n")
    assert "Action:" not in complete.out

    zip_code = _BLANK_ZIPS[label]
    blank = _robinhood(tmp_path, monkeypatch, capsys, "blank", zip_code, *args)

    lines = complete.out.splitlines(keepends=True)
    assert lines[1:3] == [
        f"  Wrote: <run>/out/schedule-fa-a{table}.{_SUFFIX[fmt]}\n" for table in (2, 3)
    ]
    assert blank.rc == 3
    assert blank.out == "".join(
        ["Robinhood schedule-fa: manual\n", *lines[1:3], f"  Action: {_ACTION}\n", *lines[3:]]
    )
    assert blank.err == complete.err
    _assert_only_zips_differ(blank.out_dir, complete.out_dir, zip_code)


@pytest.mark.parametrize("label", ["empty", "space"])
def test_robinhood_fa_json_reports_a_blank_zip_as_identity_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], label: str
) -> None:
    """``cbdt-json-fragment`` exits 3 and writes no fragment, with one
    ``a3-identity`` action. For a whitespace-only ZIP main printed the JSON's
    ZIP gap instead."""
    args = ("robinhood-fa", "--format", "cbdt-json-fragment")
    complete = _robinhood(tmp_path, monkeypatch, capsys, "complete", _COMPLETE_ZIP, *args)
    assert complete.rc == 0
    assert (complete.out_dir / "schedule-fa.json").is_file()

    blank = _robinhood(tmp_path, monkeypatch, capsys, "blank", _BLANK_ZIPS[label], *args)

    assert blank.rc == 3
    assert blank.out.startswith(f"Robinhood schedule-fa: manual\n  Action: {_ACTION}\n")
    assert blank.out.count("Action:") == 1
    assert "ZipCode" not in blank.out + blank.err
    assert list(blank.out_dir.iterdir()) == []
    assert blank.err == complete.err


@pytest.mark.parametrize("fmt", ["review-csv", "xlsx"])
@pytest.mark.parametrize("label", sorted(_BLANK_ZIPS))
def test_robinhood_all_reports_a_blank_zip_as_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fmt: str,
    label: str,
) -> None:
    """The leg is ``manual`` with the ``a3-identity`` action, the run exits 3,
    and it keeps the complete run's files, less the automatic JSON it does not
    attempt, with the ZIP cell as configured. For a whitespace-only ZIP main
    failed the leg on the automatic JSON's blank ZIP, exited 4 and removed
    every output."""
    fix_xlsx_clock(monkeypatch)
    args = ("robinhood-all", "--format", fmt)
    complete = _robinhood(tmp_path, monkeypatch, capsys, "complete", _COMPLETE_ZIP, *args)
    assert complete.rc == 0
    assert (complete.out_dir / "schedule-fa.json").is_file()

    zip_code = _BLANK_ZIPS[label]
    blank = _robinhood(tmp_path, monkeypatch, capsys, "blank", zip_code, *args)

    assert blank.rc == 3
    assert _legs(blank.out) == {**_legs(complete.out), "schedule-fa": "manual"}
    assert blank.out.count(f"-> {_ACTION}\n") == 1
    assert "automatic Schedule FA JSON" not in blank.out + blank.err
    assert "ZipCode" not in blank.out + blank.err
    assert not (blank.out_dir / "schedule-fa.json").exists()
    assert blank.err == complete.err
    (complete.out_dir / "schedule-fa.json").unlink()
    _assert_only_zips_differ(blank.out_dir, complete.out_dir, zip_code)


# ---- Controls ----------------------------------------------------------------------


def test_robinhood_fa_keeps_the_upload_ascii_refusal_for_a_no_break_space_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The upload CSV's ASCII check still refuses before any manual work is
    reported, as on main: exit 4, no output."""
    args = ("robinhood-fa", "--format", "filing-csv")
    run = _robinhood(tmp_path, monkeypatch, capsys, "run", "\u00a0", *args)

    assert run.rc == 4
    assert run.out == ""
    assert run.err == _ASCII
    assert list(run.out_dir.iterdir()) == []


# ---- The a3-identity name remedy (#1430) -------------------------------------------


def test_a3_name_remedy_entry_loads_and_files_the_name(tmp_path: Path) -> None:
    """A GLEIF exact-ISIN address comes with a ZIP but no name. The old remedy's
    entry, ``isin`` and ``name`` alone, loads, and the Robinhood run stops for
    the filer to verify the GLEIF address found for it (#1449). The entry the
    remedy asks for, the review file's address and ZIP copied in, files the
    configured name over the automatic address and ZIP with no supersession
    warning. Copying the address alone still warns."""
    isin = Isin("US99995B1044")
    address = "1 Example Plaza, Anytown, DE"
    automatic = (FaEntityOverride(isin=isin, address=address, zip_code="99999"),)
    held = (robinhood_security(cusip="99995B104", isin=isin),)
    account = '[account]\nholder_name = "Tax Payer"\n\n[[fa.entities]]\n'
    name = 'name = "Tax Payer Holdings Inc."\n'

    def load(entry: str) -> tuple[FaEntityOverride, ...]:
        config_path = tmp_path / "user.toml"
        config_path.write_text(f'{account}isin = "{isin}"\n{entry}', encoding="utf-8")
        return load_user_config(config_path).fa.entities

    name_only = load(name)
    assert robinhood_unverified_entity_addresses(
        name_only, held, automatic, {isin: RobinhoodEntityRoute.LEGAL}, ()
    ) == (
        RobinhoodUnverifiedEntityAddress(
            entry_index=0,
            entry=FaEntityOverride(isin=isin, name="Tax Payer Holdings Inc."),
            found=RobinhoodFoundEntityAddress(RobinhoodEntityRoute.LEGAL, address, "99999"),
            etf=False,
            nature_missing=True,
        ),
    )

    configured = load(f'address = "{address}"\nzip = "99999"\n{name}')
    assert robinhood_unverified_entity_addresses(configured, held, automatic, {}, ()) == ()
    assert merge_robinhood_entity_overrides(automatic, configured)[isin] == FaEntityOverride(
        isin=isin, address=address, zip_code="99999", name="Tax Payer Holdings Inc."
    )
    assert robinhood_entity_override_supersession_warnings(automatic, configured) == ()

    address_only = load(f'address = "{address}"\n{name}')
    assert robinhood_entity_override_supersession_warnings(automatic, address_only) == (
        ADDRESS_SUPERSEDED_ONE,
    )
