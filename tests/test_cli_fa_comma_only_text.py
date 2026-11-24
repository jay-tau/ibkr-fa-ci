"""Schedule FA text that the bulk-upload file would turn blank (#1329).

The upload file turns each comma into a space and collapses whitespace runs, so
a value of only commas and spaces filed a blank cell in the filing CSV, or the
raw commas in the review CSV, XLSX and CBDT JSON, while the run reported the
cell as filled. The row now refuses it when it is built: in every format the
run exits 4 with a fixed message that names only the row type and field, and
leaves no output, an earlier run's included.
"""

from __future__ import annotations

import csv
from dataclasses import replace
from pathlib import Path

import pytest

from ibkr_fa import cli
from tests import _robinhood_a3_load as a3_load
from tests._fa_a3_address import ZIP_MISSING_HEADING as _ZIP_BLOCK
from tests._fa_a3_address import listed_holdings as _named
from tests._ibkr_schedule_fa_inputs import CY_2025_NAV_INPUT as _FIXTURE
from tests._ibkr_schedule_fa_inputs import SCHEDULE_FA_CONFIG as _FULL_CONFIG
from tests._ibkr_schedule_fa_inputs import flat_close_fetcher as _flat_close_fetcher
from tests._netbenefits_e2e import NB_E2E_TOML, nb_e2e_bundle
from tests._robinhood_fa_inputs import fa_account as _account
from tests._robinhood_fa_inputs import fa_buy as _buy

_RULE = "must contain a character other than a comma or a space"
# The statement's first holding's [[fa.entities]] entry, and its replacements.
_XQBB = """[[fa.entities]]
isin = "US99995B1044"
address = "1 Sample Issuer Street, Sample City, XX"
zip = "00001"
nature_of_entity = "Company"
"""
_XQBB_COMMAS = '[[fa.entities]]\nisin = "US99995B1044"\naddress = ", ,"\nzip = ","\n'
_CUSTODIAN = 'address = "One Pickwick Plaza, Greenwich, CT"\nzip = "06830"'
_FORMATS = [
    pytest.param("filing-csv", ".csv", id="filing-csv"),
    pytest.param("review-csv", ".csv", id="review-csv"),
    pytest.param("xlsx", ".xlsx", id="xlsx"),
    pytest.param("cbdt-json-fragment", ".json", id="cbdt-json-fragment"),
]


def _edit(old: str, new: str, text: str = _FULL_CONFIG) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


def _schedule_fa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str, fmt: str, suffix: str
) -> int:
    """``schedule-fa`` on the NAV statement for CY 2025 into ``out/``, every
    holding priced at a flat close of 100."""
    monkeypatch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", _flat_close_fetcher())
    (tmp_path / "user.toml").write_text(config, encoding="utf-8")
    argv = ["schedule-fa", "--input", str(_FIXTURE), "--config", str(tmp_path / "user.toml")]
    argv += ["--output", str(tmp_path / "out" / f"schedule-fa{suffix}")]
    argv += ["--calendar-year", "2025", "--format", fmt]
    argv += ["--price-cache-dir", str(tmp_path / "cache")]
    if fmt == "cbdt-json-fragment":
        argv += ["--itr-form", "ITR-2"]
    return cli.main(argv)


@pytest.mark.parametrize(("fmt", "suffix"), _FORMATS)
@pytest.mark.parametrize(
    ("config", "message"),
    [
        pytest.param(
            _edit(_XQBB, _XQBB_COMMAS + 'nature_of_entity = ","\n'),
            f"FaA3Row.address_of_entity {_RULE}",
            id="entity",
        ),
        pytest.param(
            _edit(_CUSTODIAN, 'address = ", ,"\nzip = ","'),
            f"FaA2Row.institution_address {_RULE}",
            id="custodian",
        ),
    ],
)
def test_schedule_fa_refuses_comma_only_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: str,
    message: str,
    fmt: str,
    suffix: str,
) -> None:
    """The issue's example and its custodian variant. Main filed blank cells
    in the filing CSV, or raw commas in the review CSV and XLSX (exit 3, the A2
    peak being manual fill) and JSON (exit 0). Its manual-fill report named none
    of these text cells."""
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    assert _schedule_fa(tmp_path, monkeypatch, _FULL_CONFIG, fmt, suffix) in (0, 3)
    assert list(out.iterdir()) != []
    capsys.readouterr()

    assert _schedule_fa(tmp_path, monkeypatch, config, fmt, suffix) == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: InvariantError: {message}\n"
    assert captured.out == ""
    assert list(out.iterdir()) == []


def _filing_rows(tmp_path: Path) -> list[list[str]]:
    with (tmp_path / "out" / "schedule-fa.csv").open(encoding="utf-8", newline="") as handle:
        _header, *rows = csv.reader(handle)
    return rows


@pytest.mark.parametrize(
    ("value", "rendered"), [("a,", "a"), (", a ,", "a"), ("QZCOMMA,", "QZCOMMA")]
)
def test_text_with_another_character_files_its_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
    rendered: str,
) -> None:
    """A nature of ``a,``, ``, a ,`` or ``QZCOMMA,`` files its rendering, as on
    main, with nothing on stderr."""
    (tmp_path / "out").mkdir(mode=0o700)
    config = _edit(_XQBB, _edit('"Company"', f'"{value}"', _XQBB))

    assert _schedule_fa(tmp_path, monkeypatch, config, "filing-csv", ".csv") == 3

    # Columns: serial, code, name, address, ZIP, nature; XQBB's two lots first.
    assert [row[5] for row in _filing_rows(tmp_path)] == [rendered, rendered, "Company"]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("zip_line", ["", 'zip = ""\n', 'zip = " "\n'])
def test_empty_or_whitespace_zip_stays_manual_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    zip_line: str,
) -> None:
    """An empty or whitespace-only ZIP is still a manual-fill cell that the
    report names, as on main."""
    (tmp_path / "out").mkdir(mode=0o700)
    config = _edit(_XQBB, _edit('zip = "00001"\n', zip_line, _XQBB))

    assert _schedule_fa(tmp_path, monkeypatch, config, "filing-csv", ".csv") == 3

    assert [row[4] for row in _filing_rows(tmp_path)] == ["", "", "00002"]
    captured = capsys.readouterr()
    assert captured.err == ""
    assert _named(captured.out, _ZIP_BLOCK) == ["XQBB (US99995B1044)"]


def test_netbenefits_fa_refuses_a_comma_only_entity_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, config = nb_e2e_bundle(tmp_path, monkeypatch)
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    argv = ["netbenefits-fa", "--input", str(bundle), "--config", str(config)]
    argv += ["--output", str(out / "fa.csv"), "--calendar-year", "2025", "--no-fetch-prices"]
    assert cli.main(argv) == 3
    assert list(out.iterdir()) != []
    capsys.readouterr()
    config.write_text(
        _edit('"1 Sample Issuer Street, Sample City, XX"', '", ,"', NB_E2E_TOML), encoding="utf-8"
    )

    assert cli.main(argv) == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: InvariantError: FaA3Row.address_of_entity {_RULE}\n"
    assert captured.out == ""
    assert list(out.iterdir()) == []


def test_robinhood_fa_refuses_a_comma_only_account_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "robinhood-fa",
        *("--input", str(tmp_path / "bundle"), "--config", str(tmp_path / "user.toml")),
        *("--output-dir", str(tmp_path / "out"), "--financial-year", "2025-26"),
        *("--itr-form", "ITR-2", "--no-fetch-prices"),
    ]
    out = tmp_path / "out"
    a3_load.load_two_accounts(monkeypatch, tmp_path, _buy("2"))
    assert cli.main(argv) == 0
    assert list(out.iterdir()) != []
    capsys.readouterr()
    monkeypatch.setattr(
        a3_load, "_account", lambda number: replace(_account(number), address=", ,")
    )
    a3_load.load_two_accounts(monkeypatch, tmp_path, _buy("2"))

    assert cli.main(argv) == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: InvariantError: FaA2Row.institution_address {_RULE}\n"
    assert captured.out == ""
    assert list(out.iterdir()) == []
