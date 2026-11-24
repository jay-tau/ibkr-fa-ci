"""``netbenefits-fa`` files the exact Dec-31 close (#1250).

A price override with more significant digits than Decimal's default 28 lost
them in the close and closing products, so the run filed a rounding artefact.
These run the real pipeline and CLI on the lot-proof bundle, with only
``pdftotext`` stubbed: 3.482 shares bought on Dec-31, valued at the vendored
2025-12-31 TTBR of 89.47.

Since #1275 user.toml refuses a price with more than 20 decimal places when it's
read, so no file can carry these overrides: 3.482 x a 3-digit price with 20
places has at most 27 digits, which Decimal's default context already holds.
The run's loaded config hands each long price to the pipeline in place of the
file's in-limit one, as a hand-built config can; the exact close and its digit
cap stay the backstop for such a value.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import ibkr_fa.pipeline_netbenefits_fa as nb_mod
from ibkr_fa import cli
from ibkr_fa.config import UserConfig
from ibkr_fa.exceptions import InvariantError
from tests._crash_reports import assert_no_markers, assert_prints_no_marker
from tests._exact_refusals import INEXACT_PRODUCT
from tests._netbenefits_fa import BASE_CONFIG
from tests._netbenefits_fa_lot_bundle import LOT_PROOF_DEC_PERIOD as _DEC
from tests._netbenefits_fa_lot_bundle import LOT_PROOF_JAN_PERIOD as _JAN
from tests._netbenefits_fa_lot_bundle import LOT_PROOF_OWN_ROW as _OWN
from tests._netbenefits_fa_lot_bundle import LotProofBundle as _Bundle
from tests._netbenefits_fa_lot_bundle import lot_proof_bundle as _bundle
from tests._netbenefits_fa_lot_bundle import lot_proof_report as _report
from tests._netbenefits_fa_lot_bundle import run_lot_proof_bundle as _run
from tests._netbenefits_fa_lot_bundle import write_lot_proof_bundle as _write
from tests._text_edits import replace_once

# A distinctive 30-significant-digit override: 3.482 x price x 89.47 is
# 66503.49999999999999999999999993335122, so the exact close files ₹66,503.
_DIGITS = "470711786885653192740682943"
_PRICE = f"213.{_DIGITS}"
_SOURCE = "SYNTHQSRC"
_REASON = "SYNTHQWHY"
_MARKERS = (_SOURCE, _REASON, _PRICE, _DIGITS)

# The same override with 1000 more digits: its total for 3.482 shares needs
# more digits than the exact products allow.
_CAP_PRICE = _PRICE + "1" * 1000
_CAP_MARKERS = (*_MARKERS, "1" * 30)
_CAP_MESSAGE = f"InvariantError: Dec-31 close, override total: {INEXACT_PRODUCT}"

_A3 = (
    b"Country/Region name,Country Name and Code,Name of entity,Address of entity,ZIP Code,"
    b"Nature of entity,Date of acquiring the interest,Initial value of the investment,"
    b"Peak value of investment during the Period,Closing balance,Total gross amount "
    b"paid/credited with respect to the holding during the period,Total gross proceeds from "
    b"sale or redemption of investment during the period\r\n"
    b"1,2,EXAMPLE CORP,1 Sample Issuer Street Sample City XX,00000,Listed company shares,"
    b"2025-12-31,66503,66503,66503,0,0\r\n"
)
_A2 = (
    b"Country/Region name,Country Name and Code,Name of financial institution,"
    b"Address of financial institution,ZIP Code,Account Number,Status,Account opening date,"
    b"Peak Balance During the Period,Closing balance,Nature of Amount,Amount\r\n"
    b"UNITED STATES OF AMERICA,2,Fidelity Stock Plan Services LLC,245 Summer Street Boston MA,"
    b"02210,I99999999,BENEFICIAL_OWNER,2025-12-31,,66503,N,0\r\n"
)
_OUTPUTS = ("a3-a2.csv", "a3.csv")


def _bundle_at(monkeypatch: pytest.MonkeyPatch, price: str) -> _Bundle:
    """The lot-proof bundle whose Dec-31 override is ``price``, with the
    ``SYNTHQSRC`` source and ``SYNTHQWHY`` reason markers.

    The file keeps the base config's in-limit ``213.47``; the run's loaded
    config carries ``price`` instead, which user.toml can't hold since #1275."""
    config = BASE_CONFIG
    for old, new in (
        ('source = "SPS year-end statement"', f'source = "{_SOURCE}"'),
        ('reason = "12-31 lot unpriced in CSV"', f'reason = "{_REASON}"'),
    ):
        config = replace_once(config, old, new)
    load = nb_mod.load_user_config

    def load_with_price(path: Path) -> UserConfig:
        loaded = load(path)
        assert loaded.netbenefits is not None
        [override] = loaded.netbenefits.price_overrides
        assert override.price_per_share_usd == Decimal("213.47")
        overrides = (replace(override, price_per_share_usd=Decimal(price)),)
        return replace(loaded, netbenefits=replace(loaded.netbenefits, price_overrides=overrides))

    monkeypatch.setattr(nb_mod, "load_user_config", load_with_price)
    return _bundle(_report(_DEC), _report(_JAN, _OWN), config=config)


def _assert_exact_outputs(out: Path) -> None:
    assert sorted(path.name for path in out.iterdir()) == list(_OUTPUTS)
    assert (out / "a3.csv").read_bytes() == _A3
    assert (out / "a3-a2.csv").read_bytes() == _A2


def _write_stale(out: Path) -> None:
    for name in _OUTPUTS:
        (out / name).write_text("stale\n", encoding="utf-8")


def test_long_override_files_the_exact_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exact close files ₹66,503 in A3 closing, A3 peak and A2 closing.
    Main rounded 3.482 x price to 28 digits and filed ₹66,504 in all three."""
    _write(tmp_path, monkeypatch, _bundle_at(monkeypatch, _PRICE))

    _run(tmp_path)

    _assert_exact_outputs(tmp_path / "out")
    assert capsys.readouterr().err == ""


def test_long_override_files_the_exact_close_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same bytes through ``netbenefits-fa``, and stderr names neither
    marker nor the price. The run exits 3, not filing-complete, because the
    statements can't prove the A2 peak; stdout echoes the override's own
    source and reason by design. Main filed ₹66,504."""
    args = _write(tmp_path, monkeypatch, _bundle_at(monkeypatch, _PRICE))

    assert cli.main(args) == 3

    _assert_exact_outputs(tmp_path / "out")
    err = capsys.readouterr().err
    assert err == ""
    assert_no_markers(err, *_MARKERS)


def test_override_past_the_digit_cap_refuses_and_removes_stale_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An override whose total needs more than 1000 digits refuses with the
    fixed override-total message and no context, through the pipeline and the
    CLI (exit 4); each run removes the earlier outputs and writes none. Neither
    the refusal nor stderr names a marker, the price or its digits. Main
    rounded the total and filed ₹66,504."""
    args = _write(tmp_path, monkeypatch, _bundle_at(monkeypatch, _CAP_PRICE))
    out = tmp_path / "out"
    _write_stale(out)

    with pytest.raises(InvariantError) as caught:
        _run(tmp_path)

    error = caught.value
    assert type(error) is InvariantError
    assert str(error) == _CAP_MESSAGE
    assert error.context == ()
    assert error.__cause__ is None
    assert error.__context__ is None
    assert_prints_no_marker(error, markers=_CAP_MARKERS)
    assert list(out.iterdir()) == []

    _write_stale(out)
    assert cli.main(args) == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {_CAP_MESSAGE}\n"
    assert_no_markers(captured.err, *_CAP_MARKERS)
    assert captured.out == ""
    assert list(out.iterdir()) == []
