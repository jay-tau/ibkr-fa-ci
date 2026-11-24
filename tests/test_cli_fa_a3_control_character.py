"""A control character in Schedule FA Table A3 upload text (#1367).

Only the Name of entity carries statement text: an IBKR Description, a
NetBenefits confirmation's SECURITY DESCRIPTION or a Robinhood issuer name.
The upload file turned a tab into a space and copied U+0001 and DEL into the
cell. The review CSV kept all three; the workbook kept the tab and DEL but
refused U+0001, and the CBDT JSON refused all three. The Table A3 row now
refuses one when it is built. Each standalone refusing run exits 4 with one
fixed stderr line naming the field and the position, never the value or the
character, and leaves no output, an earlier run's included. The edited names
are synthetic and hold the digit-run marker 473812, which no refusing run prints.
"""

from __future__ import annotations

import csv
from dataclasses import replace
from pathlib import Path

import pytest

from ibkr_fa import cli
from tests._fa_a3_name_runs import A3_ASCII_REFUSAL, edit_statement_names
from tests._ibkr_schedule_fa_inputs import CY_2025_NAV_INPUT as _FIXTURE
from tests._ibkr_schedule_fa_inputs import SCHEDULE_FA_CONFIG as _FULL_CONFIG
from tests._ibkr_schedule_fa_inputs import flat_close_fetcher as _flat_close_fetcher
from tests._netbenefits_e2e import NB_E2E_PDF_TEXT, NB_E2E_TOML_WITH_PEAK, nb_e2e_bundle
from tests._robinhood_a3_load import load_two_accounts
from tests._robinhood_cli import robinhood_main as _robinhood_main
from tests._robinhood_cli import stale_fa_outputs as _stale_fa_outputs
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_fa_inputs import fa_entity as _entity

_MARKER = "473812"
_FIXTURES = Path(__file__).parent / "fixtures"
_FLEX = _FIXTURES / "e2e_year_end_2025.csv"
_FY_INPUT = _FIXTURES / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
_CHARACTERS = [
    pytest.param("\x01", id="U+0001"),
    pytest.param("\t", id="tab"),
    pytest.param("\x7f", id="DEL"),
    pytest.param("\x00", id="NUL"),
]
_FORMATS = [
    pytest.param("filing-csv", ".csv", id="filing-csv"),
    pytest.param("review-csv", ".csv", id="review-csv"),
    pytest.param("xlsx", ".xlsx", id="xlsx"),
    pytest.param("cbdt-json-fragment", ".json", id="cbdt-json-fragment"),
]
# Each format's exit on the unedited NAV statement: the A2 peak is manual fill.
_EXIT = {"filing-csv": 3, "review-csv": 3, "xlsx": 3, "cbdt-json-fragment": 0}


def _refusal(position: int) -> str:
    return (
        "InvariantError: FaA3Row.name_of_entity has a control character at position "
        f"{position}; filed text may not contain control characters"
    )


def _assert_refused(capsys: pytest.CaptureFixture[str], out: Path, position: int) -> None:
    """One fixed stderr line, so no marker; nothing on stdout; no file left."""
    captured = capsys.readouterr()
    assert captured.err == f"error: {_refusal(position)}\n"
    assert captured.out == ""
    assert list(out.iterdir()) == []


# ---- IBKR: schedule-fa and run-all --------------------------------------------


def _schedule_fa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statement: Path, fmt: str, suffix: str
) -> int:
    """``schedule-fa`` on ``statement`` for CY 2025 into ``out/``, with no entity
    name configured and every holding priced at a flat close of 100."""
    monkeypatch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", _flat_close_fetcher())
    (tmp_path / "user.toml").write_text(_FULL_CONFIG, encoding="utf-8")
    argv = ["schedule-fa", "--input", str(statement), "--config", str(tmp_path / "user.toml")]
    argv += ["--output", str(tmp_path / "out" / f"schedule-fa{suffix}")]
    argv += ["--calendar-year", "2025", "--format", fmt, "--itr-form", "ITR-2"]
    argv += ["--price-cache-dir", str(tmp_path / "cache")]
    return cli.main(argv)


@pytest.mark.parametrize(("fmt", "suffix"), _FORMATS)
@pytest.mark.parametrize("character", _CHARACTERS)
def test_schedule_fa_refuses_a_control_character_in_a_description(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    character: str,
    fmt: str,
    suffix: str,
) -> None:
    """The issue's example: XQBB's NAV-statement Description. Main filed U+0001
    and DEL in the filing CSV and a tab as a space (exit 3), kept them in the
    review CSV (exit 3), and refused a NUL there and every one in the JSON only
    with a writer or JSON error."""
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    assert _schedule_fa(tmp_path, monkeypatch, _FIXTURE, fmt, suffix) == _EXIT[fmt]
    assert list(out.iterdir()) != []
    capsys.readouterr()
    statement = edit_statement_names(_FIXTURE, tmp_path, f"SAMPLE{character}ISSUER {_MARKER} INC")

    assert _schedule_fa(tmp_path, monkeypatch, statement, fmt, suffix) == 4

    _assert_refused(capsys, out, 7)


def test_schedule_fa_refuses_a_control_character_in_a_flex_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Flex statement's Description; main filed the U+0001 (exit 3)."""
    (tmp_path / "out").mkdir(mode=0o700)
    assert _schedule_fa(tmp_path, monkeypatch, _FLEX, "filing-csv", ".csv") == _EXIT["filing-csv"]
    assert list((tmp_path / "out").iterdir()) != []
    capsys.readouterr()
    statement = edit_statement_names(_FLEX, tmp_path, f"SAMPLE\x01ISSUER {_MARKER} INC")

    assert _schedule_fa(tmp_path, monkeypatch, statement, "filing-csv", ".csv") == 4

    _assert_refused(capsys, tmp_path / "out", 7)


def test_run_all_refuses_a_control_character_in_the_cy_description(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """run-all's Schedule FA leg; main wrote the U+0001 into schedule-fa.csv
    (exit 3). The leg fails, so the run removes every schedule file."""
    out = tmp_path / "out"
    config = tmp_path / "user.toml"
    config.write_text(_FULL_CONFIG, encoding="utf-8")
    statement = edit_statement_names(_FIXTURE, tmp_path, f"SAMPLE\x01ISSUER {_MARKER} INC")

    rc = cli.main(
        [
            "run-all",
            *("--fy-input", str(_FY_INPUT), "--cy-input", str(statement)),
            *("--config", str(config), "--output-dir", str(out)),
            *("--financial-year", "2025-26", "--format", "review-csv", "--itr-form", "ITR-2"),
            "--no-fetch-prices",
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err.count(f"\nerror: {_refusal(7)}\n") == 1
    error_label = cli._RUN_ALL_STATUS_LABEL["error"]
    errors = [
        line.split()[1]
        for line in captured.err.splitlines()
        if line.lstrip().startswith(error_label)
    ]
    assert errors == ["schedule-fa"]
    assert _MARKER not in captured.out + captured.err
    assert list(out.iterdir()) == []


# ---- What the row doesn't refuse ----------------------------------------------


def _filing_names(tmp_path: Path) -> list[str]:
    """The upload file's Name of entity cells; XQBB's two lots come first."""
    with (tmp_path / "out" / "schedule-fa.csv").open(encoding="utf-8", newline="") as handle:
        _header, *rows = csv.reader(handle)
    return [row[2] for row in rows]


@pytest.mark.parametrize(
    ("name", "upload_error"),
    [
        pytest.param(f"SAMPLE\x85ISSUER {_MARKER} INC", A3_ASCII_REFUSAL, id="U+0085"),
        pytest.param(f"SAMPLE ISSU\u00c9R {_MARKER} INC", A3_ASCII_REFUSAL, id="non-ascii-letter"),
        pytest.param(f"SAMPLE, ISSUER {_MARKER} INC", "", id="comma"),
    ],
)
@pytest.mark.parametrize(("fmt", "suffix"), _FORMATS)
def test_text_without_a_control_character_keeps_its_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    upload_error: str,
    fmt: str,
    suffix: str,
) -> None:
    """As on main: a C1 character or a non-ASCII letter is refused only by the
    upload file's ASCII check, and a comma files as a space."""
    (tmp_path / "out").mkdir(mode=0o700)
    statement = edit_statement_names(_FIXTURE, tmp_path, name)
    error = upload_error if fmt == "filing-csv" else ""

    assert _schedule_fa(tmp_path, monkeypatch, statement, fmt, suffix) == (
        4 if error else _EXIT[fmt]
    )

    assert capsys.readouterr().err == error
    if fmt == "filing-csv" and not error:
        assert _filing_names(tmp_path)[:2] == [f"SAMPLE ISSUER {_MARKER} INC"] * 2


# ---- NetBenefits: netbenefits-fa and netbenefits-all ---------------------------

_CONFIRMATION = "confirmation-dec.pdf"
_NETBENEFITS_FA = [
    pytest.param(("--format", "filing-csv"), id="filing-csv"),
    pytest.param(("--format", "review-csv"), id="review-csv"),
    pytest.param(
        ("--format", "cbdt-json-fragment", "--itr-form", "ITR-2"), id="cbdt-json-fragment"
    ),
]


def _netbenefits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, *extra: str
) -> tuple[list[str], Path]:
    """The argv of ``command`` on the e2e bundle (``pdftotext`` stubbed), with
    the A2 peak configured so the JSON can be written, into ``out/``, and that
    folder."""
    bundle, config = nb_e2e_bundle(tmp_path, monkeypatch)
    config.write_text(NB_E2E_TOML_WITH_PEAK, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    if command == "netbenefits-fa":
        where = ["--output", str(out / "schedule-fa-a3.csv"), "--calendar-year", "2025"]
    else:
        where = ["--output-dir", str(out), "--financial-year", "2025-26", "--itr-form", "ITR-2"]
    argv = [command, "--input", str(bundle), "--config", str(config), *where, *extra]
    return [*argv, "--no-fetch-prices"], out


def _edit_confirmation(monkeypatch: pytest.MonkeyPatch, character: str) -> None:
    """The confirmation's SECURITY DESCRIPTION ``EXAMPLE CORP`` becomes
    ``EXAMPLE<character>CORP 473812``."""
    text = NB_E2E_PDF_TEXT[_CONFIRMATION]
    old = "\nEXAMPLE CORP "
    assert text.count(old) == 1
    monkeypatch.setitem(
        NB_E2E_PDF_TEXT,
        _CONFIRMATION,
        text.replace(old, f"\nEXAMPLE{character}CORP {_MARKER} "),
    )


@pytest.mark.parametrize("extra", _NETBENEFITS_FA)
@pytest.mark.parametrize("character", _CHARACTERS[:2])
def test_netbenefits_fa_refuses_a_control_character_in_the_security_description(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    character: str,
    extra: tuple[str, ...],
) -> None:
    """Main filed the U+0001, and the tab as a space, in the upload file and
    kept each in the review CSV (exit 3)."""
    argv, out = _netbenefits(tmp_path, monkeypatch, "netbenefits-fa", *extra)
    assert cli.main(argv) in (0, 3)
    assert list(out.iterdir()) != []
    capsys.readouterr()
    _edit_confirmation(monkeypatch, character)

    assert cli.main(argv) == 4

    _assert_refused(capsys, out, 8)


def test_netbenefits_all_names_the_refusal_on_its_schedule_fa_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Main wrote the U+0001 into schedule-fa-a3.csv (exit 3)."""
    argv, out = _netbenefits(tmp_path, monkeypatch, "netbenefits-all")
    _edit_confirmation(monkeypatch, "\x01")

    assert cli.main(argv) == 4

    captured = capsys.readouterr()
    assert captured.err.startswith(f"error: {_refusal(8)}\n")
    assert "\n  ERROR        netbenefits-fa\n" in captured.err
    assert _MARKER not in captured.out + captured.err
    assert list(out.iterdir()) == []


# ---- Robinhood: robinhood-fa and robinhood-all ---------------------------------


def _robinhood_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, issuer: str, *, nature: bool = True
) -> None:
    """The exact-A3 bundle whose buy names ``issuer``, its entity override
    naming no entity, and with ``nature`` false no nature either."""
    bundle = load_two_accounts(monkeypatch, tmp_path, replace(_buy("2"), issuer_name=issuer))
    bundle.entity_overrides = {
        isin: replace(
            entity, name=None, nature_of_entity=entity.nature_of_entity if nature else None
        )
        for isin, entity in _entity().items()
    }


@pytest.mark.parametrize(("fmt", "suffix"), _FORMATS)
@pytest.mark.parametrize("character", _CHARACTERS[:2])
def test_robinhood_fa_refuses_a_control_character_in_the_issuer_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    character: str,
    fmt: str,
    suffix: str,
) -> None:
    """Main filed the U+0001, and the tab as a space, and exited 0; its workbook
    took the tab and refused the U+0001 with a writer error, and its JSON
    refused both."""
    out = tmp_path / "out"
    _stale_fa_outputs(out)
    _robinhood_load(monkeypatch, tmp_path, f"Synthetic{character}Corporation {_MARKER}")

    assert _robinhood_main("robinhood-fa", tmp_path, "--no-fetch-prices", "--format", fmt) == 4

    _assert_refused(capsys, out, 10)


def test_robinhood_all_names_the_refusal_on_its_schedule_fa_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With the nature unset the leg is manual work, so main attempted no
    automatic JSON and wrote the U+0001 into its review file (exit 3)."""
    out = tmp_path / "out"
    _stale_fa_outputs(out)
    _robinhood_load(monkeypatch, tmp_path, f"Synthetic\x01Corporation {_MARKER}", nature=False)

    assert _robinhood_main("robinhood-all", tmp_path, "--no-fetch-prices") == 4

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    [leg] = [index for index, line in enumerate(lines) if line.split() == ["error", "schedule-fa"]]
    assert lines[leg + 1].strip() == f"-> {_refusal(10)}"
    assert captured.err == ""
    assert _MARKER not in captured.out
    assert list(out.iterdir()) == []
