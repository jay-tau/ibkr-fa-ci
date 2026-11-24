"""Robinhood identity guidance and relays print no statement values (#1044).

The suggested ``user.toml`` blocks hold statement CUSIPs and symbols, so they
go only to a private file in the output folder; a refusal names that file.
Relays print an ``IbkrFaError``'s text, an ``OSError`` as fixed text and its
errno name, and anything else as a fixed internal-error line.
"""

from __future__ import annotations

import errno
import re
import tomllib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

import ibkr_fa.output._atomic as atomic
import ibkr_fa.pipeline_robinhood as pipeline
import ibkr_fa.pipeline_robinhood_bundle as bundle_mod
import ibkr_fa.pipeline_robinhood_core as core
import ibkr_fa.pipeline_robinhood_outputs as outputs
import tests._robinhood_fa_inputs as fa_inputs
from ibkr_fa import cli
from ibkr_fa.compute.robinhood_fa import RobinhoodFaBlockerKind
from ibkr_fa.compute.robinhood_identity import (
    RobinhoodIdentityFailure,
    observed_robinhood_identity_inputs,
    resolve_robinhood_security_table,
)
from ibkr_fa.config import FxConfig, PricesConfig, RobinhoodConfig, UserConfig
from ibkr_fa.exceptions import ConfigError
from ibkr_fa.models.reference_identity import IdentitySourceBundle
from ibkr_fa.normalize.robinhood import (
    reconcile_robinhood_accounts,
    reconcile_robinhood_cash,
    reconcile_robinhood_security_quantities,
    resolve_robinhood_security_identities,
)
from ibkr_fa.normalize.robinhood.accounts import RobinhoodAccountsReconciliation
from ibkr_fa.normalize.robinhood.bundle import RobinhoodReconciliationError
from ibkr_fa.normalize.robinhood.security import render_robinhood_security_toml
from ibkr_fa.output._cleanup import OutputRemovalError
from ibkr_fa.parser.robinhood import parse_robinhood_activity
from ibkr_fa.pipeline_robinhood import RobinhoodPipelineError, run_robinhood_cg
from ibkr_fa.pipeline_robinhood_bundle import (
    RobinhoodBundle,
    RobinhoodScopeStatus,
    identity_failure_guidance,
)
from ibkr_fa.robinhood_identity_sources import IdentitySourceAudit
from tests._crash_reports import assert_replaced_unchained, crash_pattern, render_without_frames
from tests._no_echo import ACCOUNT, CUSIP, FILENAME, HOLDER, MARKERS, STRERROR, assert_no_markers
from tests._robinhood_account_inputs import CANCELLING_PAIR as _PAIR
from tests._robinhood_account_inputs import NEG_MARKERS as _NEG_MARKERS
from tests._robinhood_account_inputs import ONE_ROW as _ONE
from tests._robinhood_account_inputs import ZQ_MARKERS as _ZQ_MARKERS
from tests._robinhood_account_inputs import ZQ_NEW as _ZQ_NEW
from tests._robinhood_account_inputs import ZQ_OLD as _ZQ_OLD
from tests._robinhood_account_inputs import accounts_pair as _accounts_pair
from tests._robinhood_account_inputs import negative_identities as _negative_identities
from tests._robinhood_account_inputs import negative_rows_pair as _negative_rows_pair
from tests._robinhood_account_inputs import pair_with_zq_symbols as _pair_with_zq_symbols
from tests._robinhood_bundle_stubs import bundle_config_text as _config_text
from tests._robinhood_bundle_stubs import patch_bundle_sources as _patch_sources
from tests._robinhood_bundle_stubs import write_bundle as _write_bundle
from tests._robinhood_cash import CASH_CLEAN_MARKERS as _CLEAN_MARKERS
from tests._robinhood_cash import MARKED_CASH_SWEEP as _MARKED_SWEEP
from tests._robinhood_cash import cash_with_faults as _cash_faults
from tests._robinhood_cg import CG_UNRESOLVED
from tests._robinhood_cli import FINANCIAL_YEAR as _FY
from tests._robinhood_cli import OWNED_FILENAMES as _OWNED
from tests._robinhood_cli import outputs_left as _left
from tests._robinhood_cli import robinhood_argv
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_cli import robinhood_main_all as _main_all
from tests._robinhood_fa_blocker_lines import FA_UNRESOLVED
from tests._robinhood_fa_inputs import fa_paired_events
from tests._robinhood_identity_guidance import assert_guidance_file as _assert_guidance_file
from tests._robinhood_identity_guidance import guidance_bundle as _guidance_bundle
from tests._robinhood_identity_guidance import inventory_refusal as _inventory_refusal
from tests._robinhood_identity_guidance import stub_identity_refusals as _stub_identity_refusals
from tests._robinhood_identity_guidance import unconfigured_paired as _paired
from tests._robinhood_identity_inputs import AQ_BROKER_NAME as _AQ
from tests._robinhood_identity_inputs import AX_BROKER_NAME as _AX
from tests._robinhood_identity_inputs import ZQ_AMBIGUOUS_CHECKS as _ZQ_AMBIGUOUS
from tests._robinhood_identity_inputs import issuer_identity_sources as _identity_sources
from tests._robinhood_identity_inputs import zq_identity_sources as _zq_sources
from tests._robinhood_income_stubs import silence_default_income_warnings
from tests._robinhood_pipeline_stubs import complete_fa as _complete_fa
from tests._robinhood_pipeline_stubs import stand_in_bundle as _bundle
from tests._robinhood_pipeline_stubs import stub_successful_all as _stub_successful_all
from tests._robinhood_pipeline_stubs import stub_writing_all as _stub_writing_all
from tests._robinhood_refusals import ACTIVITY_NEGATIVE_REFUSAL as _ACTIVITY_NEGATIVE
from tests._robinhood_refusals import (
    CASH_BOTH_REFUSAL,
    CASH_IDENTITY_REFUSAL,
    CASH_NEGATIVE_REFUSAL,
    NEGATIVE_POSITION_REFUSAL,
    PENDING_CASH_REFUSAL,
    PENDING_MATCH_REFUSAL,
    PENDING_SETTLE_REFUSAL,
)
from tests._robinhood_security import IDENTITY_GUIDANCE_FILE as _GUIDANCE
from tests._robinhood_security import IDENTITY_GUIDANCE_HEADER, identity_guidance_line
from tests._robinhood_statements import activity_rows_csv as _csv
from tests._robinhood_toml import robinhood_account_toml, robinhood_report_toml, robinhood_toml

_MIXED_CUSIP = "99992A109"
_UNMAPPED = max(errno.errorcode) + 1


@pytest.fixture(autouse=True)
def _silence_default_income_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    silence_default_income_warnings(monkeypatch)


# --- The renderer --------------------------------------------------------------


@pytest.mark.parametrize(
    "symbol",
    [HOLDER, 'Synth"Quote', "Synth\\Back", "Synth\tTab\x01\x7f\n"],
    ids=["spaces", "quote", "backslash", "control"],
)
def test_renderer_writes_each_value_as_a_toml_basic_string(symbol: str) -> None:
    rendered = render_robinhood_security_toml(
        cast("RobinhoodAccountsReconciliation", _paired(symbol))
    )

    (entry,) = tomllib.loads(rendered)["robinhood"]["securities"]
    assert (entry["symbol"], entry["cusip"]) == (symbol, CUSIP)


@pytest.mark.parametrize(
    ("symbol", "cusip"),
    [(f"{HOLDER}\ud800", CUSIP), (HOLDER, f"{CUSIP}\udfff")],
    ids=["symbol", "cusip"],
)
def test_renderer_refuses_a_value_toml_cannot_hold(symbol: str, cusip: str) -> None:
    with pytest.raises(RobinhoodReconciliationError) as raised:
        render_robinhood_security_toml(
            cast("RobinhoodAccountsReconciliation", _paired(symbol, cusip))
        )

    assert str(raised.value) == (
        "Robinhood reconciliation refused: a suggested security identity value cannot be "
        "written as TOML"
    )
    assert_no_markers(str(raised.value))


# --- The guidance file -----------------------------------------------------------


def test_cg_refusal_writes_the_guidance_file_and_prints_only_its_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "out"
    _stub_identity_refusals(monkeypatch)

    assert _main("robinhood-cg", tmp_path) == 4

    captured = capsys.readouterr()
    assert captured.err == (f"error: {CG_UNRESOLVED}\n{identity_guidance_line(out / _GUIDANCE)}\n")
    assert_no_markers(captured.out + captured.err)
    _assert_guidance_file(out / _GUIDANCE)
    # The refusal keeps only the file it wrote.
    assert [path.name for path in out.iterdir()] == [_GUIDANCE]


def test_fa_refusal_writes_the_guidance_file_and_prints_only_its_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "out"
    _stub_identity_refusals(monkeypatch)

    assert _main("robinhood-fa", tmp_path, "--no-fetch-prices") == 4

    captured = capsys.readouterr()
    assert captured.err == (
        f"error: {_inventory_refusal()}\n{identity_guidance_line(out / _GUIDANCE)}\n"
    )
    assert_no_markers(captured.out + captured.err)
    _assert_guidance_file(out / _GUIDANCE)


def test_fa_blocker_refusal_writes_the_guidance_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_successful_all(
        monkeypatch,
        _complete_fa(blockers=(SimpleNamespace(kind=RobinhoodFaBlockerKind.A3_IDENTITY),)),
    )
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: _guidance_bundle())

    with pytest.raises(RobinhoodPipelineError) as raised:
        pipeline.run_robinhood_fa(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path / "out",
            financial_year=_FY,
            itr_form="ITR-2",
        )

    assert str(raised.value) == (
        f"{FA_UNRESOLVED}\n{identity_guidance_line(tmp_path / 'out' / _GUIDANCE)}"
    )
    _assert_guidance_file(tmp_path / "out" / _GUIDANCE)


def test_all_writes_the_guidance_file_once_and_keeps_it_through_the_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both the CG and the FA leg refuse for the same security: one write, and
    the run's rollback removes every other output but keeps the file."""
    out = tmp_path / "out"
    written = _stub_writing_all(monkeypatch, out)
    _stub_identity_refusals(monkeypatch)
    # The writing stub's legs still write, so the rollback has outputs to remove.
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: _guidance_bundle())
    writes: list[Path] = []
    real = outputs.atomic_text_output

    def counting(path: Path, **kwargs: str) -> object:
        writes.append(path)
        return real(path, **kwargs)

    monkeypatch.setattr(outputs, "atomic_text_output", counting)

    assert _main_all(tmp_path) == 4

    stdout = capsys.readouterr().out
    assert writes == [out / _GUIDANCE]
    line = identity_guidance_line(out / _GUIDANCE)
    assert f"-> {CG_UNRESOLVED}\n{line}" in stdout
    assert f"-> {_inventory_refusal()}\n{line}" in stdout
    assert_no_markers(stdout)
    _assert_guidance_file(out / _GUIDANCE)
    assert written
    assert not any(path.exists() for path in written)
    assert [path.name for path in out.iterdir()] == [_GUIDANCE]


@pytest.mark.parametrize("command", ["robinhood-cg", "robinhood-fa", "robinhood-all"])
def test_a_run_that_does_not_refuse_leaves_no_guidance_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    (out / _GUIDANCE).write_text("stale guidance", encoding="utf-8")
    _stub_successful_all(monkeypatch, _complete_fa(cbdt_json_eligible=False))
    monkeypatch.setattr(pipeline, "_write_fa", lambda *_args, **_kwargs: ((), False, ()))

    assert _main(command, tmp_path, "--no-fetch-prices") == 0

    assert not (out / _GUIDANCE).exists()


@pytest.mark.parametrize("command", ["robinhood-options", "robinhood-income"])
def test_commands_that_never_refuse_for_identities_remove_a_stale_guidance_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    (out / _GUIDANCE).write_text("stale guidance", encoding="utf-8")
    _stub_successful_all(monkeypatch, _complete_fa())

    _main(command, tmp_path)

    assert not (out / _GUIDANCE).exists()


def test_a_link_at_the_guidance_path_is_replaced_never_written_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    target = tmp_path / "elsewhere.toml"
    target.write_text("keep", encoding="utf-8")
    (out / _GUIDANCE).symlink_to(target)
    _stub_identity_refusals(monkeypatch)

    assert _main("robinhood-cg", tmp_path) == 4

    assert target.read_text(encoding="utf-8") == "keep"
    _assert_guidance_file(out / _GUIDANCE)


def test_a_guidance_path_that_is_an_input_refuses_before_the_preclear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    config = out / _GUIDANCE
    config.write_text("# the user's config", encoding="utf-8")
    stale = out / "schedule-cg.xlsx"
    stale.write_text("stale", encoding="utf-8")
    _stub_identity_refusals(monkeypatch)

    code = cli.main(
        [
            "robinhood-cg",
            "--input",
            str(tmp_path / "bundle"),
            "--config",
            str(config),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
        ]
    )

    assert code == 4
    assert str(config) in capsys.readouterr().err
    assert config.read_text(encoding="utf-8") == "# the user's config"
    assert stale.read_text(encoding="utf-8") == "stale"


@pytest.mark.parametrize(
    "code", [errno.EACCES, None, _UNMAPPED], ids=["eacces", "none", "unmapped"]
)
def test_a_failed_guidance_write_prints_no_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: int | None,
) -> None:
    out = tmp_path / "out"
    _stub_identity_refusals(monkeypatch)
    failure = OSError(STRERROR) if code is None else OSError(code, STRERROR, FILENAME)

    def fail(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise failure

    monkeypatch.setattr(atomic.tempfile, "mkstemp", fail)

    assert _main("robinhood-cg", tmp_path) == 4

    name = "unknown error" if code != errno.EACCES else "EACCES"
    captured = capsys.readouterr()
    assert captured.err == (
        f"error: could not write the identity guidance file {out / _GUIDANCE} ({name})\n"
    )
    assert_no_markers(captured.out + captured.err)
    assert not (out / _GUIDANCE).exists()


def test_a_failed_guidance_cleanup_prints_no_temp_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The write fails and its staging file can't be removed: the refusal names
    the staging pattern and the errno name, never the staging file itself."""
    out = tmp_path / "out"
    _stub_identity_refusals(monkeypatch)

    def fail_replace(*_args: object) -> None:
        raise OSError(errno.EIO, STRERROR, FILENAME)

    unlink = Path.unlink

    def stuck_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path.name.startswith(f".{_GUIDANCE}."):
            raise OSError(errno.EACCES, STRERROR, FILENAME)
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(atomic.os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", stuck_unlink)

    assert _main("robinhood-cg", tmp_path) == 4

    err = capsys.readouterr().err
    (staged,) = (path.name for path in out.iterdir() if path.name.startswith(f".{_GUIDANCE}."))
    assert err == (
        "error: the write failed, and its output may still be there: could not remove the "
        f"staging file {out}/.{_GUIDANCE}.*.tmp (EACCES)\n"
    )
    assert_no_markers(err, staged)
    assert_no_markers(err)


def test_all_records_a_guidance_staging_file_it_could_not_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    errors: list[Exception] = []
    guidance = outputs.IdentityGuidanceFile(tmp_path, errors)

    def fail_replace(*_args: object) -> None:
        raise OSError(errno.EIO, STRERROR, FILENAME)

    monkeypatch.setattr(atomic.os, "replace", fail_replace)
    monkeypatch.setattr(
        atomic,
        "discard_staging_file",
        lambda *_args: (_ for _ in ()).throw(OutputRemovalError("staging file left")),
    )

    with pytest.raises(OutputRemovalError) as raised:
        guidance("# guidance\n")

    assert errors == [raised.value]


# --- A real identity refusal (#1082) ------------------------------------------------


def _mixed_name_bundle(monkeypatch: pytest.MonkeyPatch) -> RobinhoodBundle:
    """A reconciled bundle whose ``XQAQ`` activity rows print two issuer names:
    two shares bought as ``SAMPLE ISSUER AQ INC`` on 2025-01-10, one sold as
    ``SAMPLE ISSUER AX CORP`` on 2025-03-10, and one held at the end of 2025.
    The real collector and resolver leave the CUSIP without an identity."""
    rows = tuple(
        (day, day, day, "XQAQ", f"{name}\nCUSIP: {_MIXED_CUSIP}", code, quantity, price, amount)
        for day, name, code, quantity, price, amount in (
            ("3/10/2025", _AX, "Sell", "1", "$15.00", "$14.99"),
            ("1/10/2025", _AQ, "Buy", "2", "$10.00", "($20.00)"),
        )
    )
    # The statement's year-end position takes the shared FA inputs' symbol.
    monkeypatch.setattr(fa_inputs, "FA_SYMBOL", "XQAQ")
    paired, accounts = fa_paired_events(
        parse_robinhood_activity(_csv(rows)), position_quantity=Decimal("1")
    )
    _unused, sources = _identity_sources(name=_AQ, nasdaq_name=_AQ)
    securities, resolution = resolve_robinhood_security_table(
        observed_robinhood_identity_inputs(paired), sources, ()
    )
    identities = resolve_robinhood_security_identities(paired, securities.securities)
    return RobinhoodBundle(
        config=cast(
            "UserConfig",
            SimpleNamespace(fx=FxConfig(), prices=PricesConfig()),
        ),
        robinhood=RobinhoodConfig(accounts=accounts),
        securities=securities,
        paired=paired,
        identities=identities,
        packets=(),
        quantities=reconcile_robinhood_security_quantities(paired, identities),
        scope=RobinhoodScopeStatus(crypto_statement_count=0),
        identity_resolution=resolution,
    )


def _mixed_name_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str, financial_year: str
) -> int:
    """Run ``command`` on the mixed-name bundle with the real CG computation
    and FA price lookup."""
    _stub_successful_all(monkeypatch, _complete_fa())
    bundle = _mixed_name_bundle(monkeypatch)
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    monkeypatch.setattr(pipeline, "_compute_cg", core.compute_cg)
    monkeypatch.setattr(pipeline, "_fa_prices", core.fa_prices)
    return cli.main(
        robinhood_argv(command, tmp_path, financial_year=financial_year, no_fetch_prices=True)
    )


def test_cg_refuses_a_sale_whose_broker_names_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The reports cover the required FY 2024-25 interval from account opening,
    including the sale."""
    guidance = tmp_path / "out" / _GUIDANCE

    assert _mixed_name_run(monkeypatch, tmp_path, "robinhood-cg", "2024-25") == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: {CG_UNRESOLVED}\n{identity_guidance_line(guidance)}\n"
    assert "# identity_checks_failed = nasdaq-name-mismatch, sec-name-mismatch" in (
        guidance.read_text(encoding="utf-8").splitlines()
    )
    assert_no_markers(captured.out + captured.err, _AQ, _AX, "XQAQ", _MIXED_CUSIP)


def test_fa_refuses_a_holding_whose_broker_names_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The price stage's refusal names the private guidance file, as CG's
    does (#1567); on 789c17f it printed the first line alone and wrote none."""
    guidance = tmp_path / "out" / _GUIDANCE

    assert _mixed_name_run(monkeypatch, tmp_path, "robinhood-fa", "2025-26") == 4

    captured = capsys.readouterr()
    assert captured.err == (
        "error: Robinhood equity inventory has no configured identity\n"
        f"{identity_guidance_line(guidance)}\n"
    )
    assert "# identity_checks_failed = nasdaq-name-mismatch, sec-name-mismatch" in (
        guidance.read_text(encoding="utf-8").splitlines()
    )
    assert_no_markers(captured.out + captured.err, _AQ, _AX, "XQAQ", _MIXED_CUSIP)


# --- #1187: a CUSIP automatic lookup leaves without a symbol stays unresolved ---------

_ZQ_SOURCE = "SYNTHZQSOURCE"


def _zq_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paired: RobinhoodAccountsReconciliation,
    sources: IdentitySourceBundle,
) -> list[RobinhoodBundle]:
    """The real loader on a stubbed bundle whose paired accounts are
    ``paired`` and whose official sources are ``sources``. Returns the
    bundles the loader produced; FA's price lookup is real."""
    _stub_successful_all(monkeypatch, _complete_fa())
    _write_bundle(tmp_path / "bundle")
    accounts = (
        robinhood_account_toml(number=number, opening_date="2025-01-05", source=_ZQ_SOURCE)
        for number in ("111111111", "222222222")
    )
    (tmp_path / "user.toml").write_text(
        robinhood_toml(
            first=next(accounts),
            second=next(accounts),
            extra="".join(
                robinhood_report_toml(file=f"activity/{name}.csv", source=_ZQ_SOURCE)
                for name in ("one", "two")
            ),
        ),
        encoding="utf-8",
    )
    _patch_sources(monkeypatch)
    monkeypatch.setattr(
        bundle_mod, "reconcile_robinhood_accounts", lambda *_args, **_kwargs: paired
    )
    monkeypatch.setattr(
        bundle_mod,
        "load_identity_sources",
        lambda *_args, **_kwargs: (sources, IdentitySourceAudit("d" * 64, True, None, None)),
    )
    for name, real in (
        ("resolve_robinhood_security_identities", resolve_robinhood_security_identities),
        ("reconcile_robinhood_security_quantities", reconcile_robinhood_security_quantities),
    ):
        monkeypatch.setattr(bundle_mod, name, real)
    loaded: list[RobinhoodBundle] = []

    def load(**kwargs: Any) -> RobinhoodBundle:
        loaded.append(core.load_bundle(**kwargs))
        return loaded[-1]

    monkeypatch.setattr(pipeline, "_load", load)
    monkeypatch.setattr(pipeline, "_fa_prices", core.fa_prices)
    return loaded


@pytest.mark.parametrize(
    ("fixture", "symbols", "unresolved", "generated", "markers"),
    [
        # The statement names ZQXA and ZQXAB, the reports ZQXA.
        pytest.param(
            {"transition": False, "conflict": True},
            ("ZQXA", "ZQXAB"),
            _ZQ_NEW,
            [],
            ("ZQXAB",),
            id="conflict-printed-current",
        ),
        pytest.param(
            {"transition": True, "conflict": True},
            ("ZQXA", "ZQXAB"),
            _ZQ_NEW,
            [],
            ("ZQXAB", _ZQ_OLD),
            id="conflict-transition",
        ),
        # A pending ZQXC trade uses the CUSIP the ZQXA transition replaced.
        # Before #1187 the loader refused: "Robinhood security identities collide".
        pytest.param(
            {"transition": True, "pending": ("ZQXC", _ZQ_OLD)},
            ("ZQXA", "ZQXC"),
            _ZQ_OLD,
            [("ZQXA", _ZQ_NEW)],
            ("ZQXC", _ZQ_OLD),
            id="replaced-cusip",
        ),
    ],
)
def test_fa_refuses_a_cusip_automatic_lookup_leaves_without_a_symbol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fixture: dict[str, Any],
    symbols: tuple[str, ...],
    unresolved: str,
    generated: list[tuple[str, str]],
    markers: tuple[str, ...],
) -> None:
    """#1187: the official sources list every symbol the evidence names, so
    choosing any would resolve an identity. Automatic lookup leaves
    ``unresolved`` without a symbol, so FA gives today's refusal for an
    unresolved identity, and the private guidance it now writes asks for the
    symbol (#1567)."""
    markers = (*_ZQ_MARKERS, _ZQ_SOURCE, *markers)
    loaded = _zq_unresolved(
        tmp_path, monkeypatch, _pair_with_zq_symbols(**fixture), _zq_sources(*symbols)
    )
    path = tmp_path / "out" / _GUIDANCE
    refusal = (
        f"Robinhood equity inventory has no configured identity\n{identity_guidance_line(path)}"
    )

    assert _main("robinhood-fa", tmp_path, "--no-fetch-prices") == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: {refusal}\n"
    assert captured.out == ""
    assert_no_markers(captured.out + captured.err, *markers)
    assert _left(tmp_path) == [_GUIDANCE]
    (bundle,) = loaded
    text = path.read_text(encoding="utf-8")
    assert text.startswith(IDENTITY_GUIDANCE_HEADER)
    assert text == identity_failure_guidance(bundle)
    assert bundle.identity_resolution is not None
    assert bundle.identity_resolution.failures == (
        RobinhoodIdentityFailure(unresolved, _ZQ_AMBIGUOUS),
    )
    assert [(item.instrument.symbol, item.cusip) for item in bundle.securities.generated] == (
        generated
    )
    guidance = identity_failure_guidance(bundle).splitlines()
    assert f"# identity_checks_failed = {', '.join(_ZQ_AMBIGUOUS)}" in guidance
    assert ('symbol = "REPLACE"', f'cusip = "{unresolved}"') == tuple(
        guidance[guidance.index("[[robinhood.securities]]") + 1 :][:2]
    )

    with pytest.raises(RobinhoodPipelineError) as raised:
        pipeline.run_robinhood_fa(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path / "out",
            financial_year=_FY,
            itr_form="ITR-2",
            fetch_prices=False,
        )

    error = raised.value
    assert type(error) is RobinhoodPipelineError
    assert str(error) == refusal
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is False
    assert_no_markers(render_without_frames(error) + repr(error), *markers)
    assert capsys.readouterr() == ("", "")
    assert _left(tmp_path) == [_GUIDANCE]


# --- Relays -----------------------------------------------------------------------


def _chained() -> ValueError:
    """``ValueError(ACCOUNT)`` chained under ``KeyError(HOLDER)``."""
    failure = ValueError(ACCOUNT)
    failure.__cause__ = KeyError(HOLDER)
    return failure


# Each failure, and how it is relayed: an OSError by its errno name, anything
# else by the internal-error line naming its class.
_FAILURES = [
    pytest.param(KeyError(ACCOUNT), KeyError, id="key-error"),
    pytest.param(OSError(errno.EACCES, STRERROR, f"{HOLDER}/x"), "EACCES", id="eacces"),
    pytest.param(OSError(STRERROR), "unknown error", id="no-errno"),
    pytest.param(OSError(_UNMAPPED, STRERROR, FILENAME), "unknown error", id="unmapped"),
    pytest.param(_chained(), ValueError, id="chained"),
]


def _relayed(expected: str | type[BaseException], operation: str) -> str:
    """A pattern for the relay of a failure whose expectation is ``expected``."""
    if isinstance(expected, str):
        return re.escape(f"{operation} ({expected})")
    return rf"internal error \({crash_pattern(expected)}\)"


@pytest.mark.parametrize(("failure", "expected"), _FAILURES)
def test_attempt_compute_relays_only_fixed_text(
    failure: Exception, expected: str | type[BaseException]
) -> None:
    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise failure

    hooks = core.RobinhoodComputeHooks(fail, fail, fail, fail, fail, lambda *_args: None, fail)
    *_, errors = core.attempt_compute(
        cast("Any", _bundle()),
        _FY,
        "ITR-2",
        suppress_cross_ay_warning=False,
        input_dir=Path("bundle"),
        config_path=Path("user.toml"),
        fetch_prices=False,
        price_cache_dir=None,
        price_fetcher=None,
        write_guidance=lambda _text: Path("unused"),
        hooks=hooks,
    )

    assert set(errors) == {"options", "cg", "income", "fa"}
    pattern = _relayed(expected, "a Robinhood file operation failed")
    for step in ("options", "cg"):
        assert re.fullmatch(pattern, errors[step]), errors[step]
        assert_no_markers(errors[step])


@pytest.mark.parametrize(("failure", "expected"), _FAILURES)
def test_standalone_compute_failure_relays_only_fixed_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected: str | type[BaseException],
) -> None:
    """A failure inside the F&O wrapper of ``robinhood-cg``."""
    _stub_successful_all(monkeypatch, _complete_fa())
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: _bundle(has_options=True))
    monkeypatch.setattr(
        pipeline,
        "_compute_options",
        lambda *_args: SimpleNamespace(trades=(object(),), blockers=()),
    )

    def fail(*_args: object) -> object:
        raise failure

    monkeypatch.setattr(pipeline, "_compute_cg", fail)

    with pytest.raises(RobinhoodPipelineError) as raised:
        run_robinhood_cg(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=tmp_path / "out",
            financial_year=_FY,
            itr_form="ITR-2",
        )

    message = str(raised.value)
    pattern = _relayed(expected, "a Robinhood file operation failed")
    assert re.fullmatch(rf"{re.escape(core.ROBINHOOD_FNO_FEMA_WARNING)} {pattern}", message)
    assert_no_markers(message)
    # An OSError is a domain error, so nothing stands in for it.
    crash = None if isinstance(expected, str) else expected
    if isinstance(failure, OSError):
        markers = (STRERROR,)
        if failure.filename is not None:
            markers += (HOLDER if failure.errno == errno.EACCES else FILENAME,)
    else:
        markers = (ACCOUNT, HOLDER) if failure.__cause__ is not None else (ACCOUNT,)
    assert_replaced_unchained(raised.value, crash, markers=markers)


@pytest.mark.parametrize(("failure", "expected"), _FAILURES)
def test_all_writer_failure_relays_only_fixed_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    expected: str | type[BaseException],
) -> None:
    out = tmp_path / "out"
    _stub_writing_all(monkeypatch, out)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(pipeline, "_write_os", fail)

    assert _main_all(tmp_path) == 4

    stdout = capsys.readouterr().out
    pattern = _relayed(expected, f"could not write the schedule-os outputs in {out}")
    assert re.search(rf"-> {pattern}\n", stdout), stdout
    assert_no_markers(stdout)


def test_domain_errors_are_relayed_with_their_own_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_writing_all(monkeypatch, tmp_path / "out")
    domain = ConfigError("robinhood", "refused")

    def fail(*_args: object, **_kwargs: object) -> object:
        raise domain

    monkeypatch.setattr(pipeline, "_write_os", fail)

    assert _main_all(tmp_path) == 4

    assert f"-> {domain}\n" in capsys.readouterr().out


# --- The CLI ------------------------------------------------------------------------


def _parsed_bundle(tmp_path: Path) -> SimpleNamespace:
    """A bundle holding the security of one activity CSV row whose Instrument
    cell is :data:`HOLDER`, read by the real parser."""
    row = (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        HOLDER,
        f"{HOLDER} Inc\nCUSIP: {CUSIP}",
        "Buy",
        "1",
        "$10.00",
        "($10.00)",
    )
    (event,) = parse_robinhood_activity(_csv((row,)))
    assert (event.instrument, event.cusip) == (HOLDER, CUSIP)
    paired = _paired()
    paired.accounts[0].evidence.events = (event,)
    return _guidance_bundle(paired)


@pytest.mark.parametrize("command", ["robinhood-fa", "robinhood-all"])
def test_cli_names_the_private_guidance_file_and_no_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    out = tmp_path / "out"
    _stub_identity_refusals(monkeypatch, _parsed_bundle(tmp_path))

    assert _main(command, tmp_path, "--no-fetch-prices") == 4

    captured = capsys.readouterr()
    line = identity_guidance_line(out / _GUIDANCE)
    if command == "robinhood-fa":
        assert line in captured.err
    else:
        # robinhood-all prints its leg reasons on stdout.
        assert f"-> {_inventory_refusal()}\n{line}" in captured.out
    assert_no_markers(captured.out + captured.err, *MARKERS)
    _assert_guidance_file(out / _GUIDANCE)


# --- #1183: a negative equity row refuses every command before any sum --------


def _negative_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paired: RobinhoodAccountsReconciliation,
) -> None:
    """The real loader on a stubbed bundle whose paired accounts are
    ``paired``; it resolves their identities and reconciles their quantities
    for real."""
    _write_bundle(tmp_path / "bundle")
    (tmp_path / "user.toml").write_text(_config_text(), encoding="utf-8")
    _patch_sources(monkeypatch)
    monkeypatch.setattr(
        bundle_mod, "reconcile_robinhood_accounts", lambda *_args, **_kwargs: paired
    )
    monkeypatch.setattr(bundle_mod, "observed_robinhood_identity_inputs", lambda _paired: ())
    monkeypatch.setattr(
        bundle_mod,
        "resolve_robinhood_security_identities",
        lambda paired, _securities: _negative_identities(paired),
    )
    monkeypatch.setattr(
        bundle_mod,
        "reconcile_robinhood_security_quantities",
        reconcile_robinhood_security_quantities,
    )


@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
@pytest.mark.parametrize("command", _OWNED)
@pytest.mark.parametrize(
    ("held", "loaned", "lent", "message"),
    [
        pytest.param(
            _PAIR,
            (),
            (),
            NEGATIVE_POSITION_REFUSAL.format(
                account_index=1, section="Securities Held in Account", page=2, row=12, column="Qty"
            ),
            id="held",
        ),
        pytest.param(
            (),
            _PAIR,
            _PAIR,
            NEGATIVE_POSITION_REFUSAL.format(
                account_index=1, section="Loaned Securities", page=3, row=22, column="Qty"
            ),
            id="loaned",
        ),
        pytest.param(
            (),
            _ONE,
            _PAIR,
            NEGATIVE_POSITION_REFUSAL.format(
                account_index=1,
                section="Stock Lending - Loaned Securities",
                page=4,
                row=32,
                column="Shares Lent",
            ),
            id="stock-lending",
        ),
    ],
)
def test_negative_equity_row_refuses_every_command_with_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    held: tuple[Decimal, ...],
    loaned: tuple[Decimal, ...],
    lent: tuple[Decimal, ...],
    message: str,
    command: str,
    stale: bool,
) -> None:
    """#1183: each row nets with a positive row to the account's one bought
    share. Every command loads the bundle, so each refuses there, before FA,
    CG or income sum a position, and leaves no output, stale or new."""
    _negative_bundle(
        tmp_path, monkeypatch, _negative_rows_pair(held=held, loaned=loaned, lent=lent)
    )
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    if stale:
        for name in _OWNED[command]:
            (out / name).write_bytes(b"stale")

    # robinhood-options fetches no prices, so it has no such flag.
    no_fetch = () if command == "robinhood-options" else ("--no-fetch-prices",)
    assert _main(command, tmp_path, *no_fetch) == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: {message}\n"
    assert captured.out == ""
    assert_no_markers(captured.out + captured.err, *_NEG_MARKERS)
    assert _left(tmp_path) == []


# --- #1185: a retained statement-cash signing refusal stops every command ----


def _cash_refusal_inputs(case: str):
    """:func:`_accounts_pair` with one refused cash row in the individual
    account's statement: an Account Activity row reaches activity matching
    for both report assignments, a Deposit Sweep row cash reconciliation."""
    bundles, reports, accounts = _accounts_pair()
    (statement,) = bundles[0].statements
    if case == "activity-negative":
        statement = replace(
            statement,
            activities=(
                replace(
                    statement.activities[0],
                    description="SYNTHCASHDESC",
                    account_type="SYNTHCASHACCOUNT",
                    credit_usd=Decimal("-10"),
                ),
                *statement.activities[1:],
            ),
        )
    else:
        debit, credit = (
            (Decimal("1"), Decimal("1")) if case == "cash-both" else (Decimal("-1"), None)
        )
        statement = replace(
            statement,
            sweep_activity=(replace(_MARKED_SWEEP, debit_usd=debit, credit_usd=credit),),
        )
    return (replace(bundles[0], statements=(statement,)), bundles[1]), reports, accounts


@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
@pytest.mark.parametrize("command", _OWNED)
@pytest.mark.parametrize(
    ("case", "message"),
    [
        pytest.param(
            "cash-both",
            f"Robinhood reconciliation refused: {CASH_BOTH_REFUSAL}",
            id="cash-both",
        ),
        pytest.param(
            "cash-negative",
            f"Robinhood reconciliation refused: {CASH_NEGATIVE_REFUSAL}",
            id="cash-negative",
        ),
        pytest.param(
            "activity-negative",
            "Robinhood reconciliation refused: no activity report assignment reconciles "
            f"(report 0/bundle 0: {_ACTIVITY_NEGATIVE}; "
            "report 0/bundle 1: issued activity signature sets disagree; "
            f"report 1/bundle 0: {_ACTIVITY_NEGATIVE})",
            id="activity-negative",
        ),
    ],
)
def test_statement_cash_signing_refusal_stops_every_command_with_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    message: str,
    command: str,
    stale: bool,
) -> None:
    """#1185: exact signing keeps each refusal. The real paired
    reconciliation refuses the bad cash row, every command exits 4 with the
    complete message and no row value, and no output is left, stale or new."""
    _write_bundle(tmp_path / "bundle")
    (tmp_path / "user.toml").write_text(_config_text(), encoding="utf-8")
    _patch_sources(monkeypatch)
    inputs = _cash_refusal_inputs(case)
    monkeypatch.setattr(
        bundle_mod,
        "reconcile_robinhood_accounts",
        lambda *_args, **_kwargs: reconcile_robinhood_accounts(*inputs),
    )
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    if stale:
        for name in _OWNED[command]:
            (out / name).write_bytes(b"stale")

    # robinhood-options fetches no prices, so it has no such flag.
    no_fetch = () if command == "robinhood-options" else ("--no-fetch-prices",)
    assert _main(command, tmp_path, *no_fetch) == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: {message}\n"
    assert captured.out == ""
    markers = (
        ("SYNTHCASHACCOUNT", "SYNTHCASHDESC")
        if case == "activity-negative"
        else ("SYNTHCASHREASON",)
    )
    assert_no_markers(captured.out + captured.err, *markers)
    assert _left(tmp_path) == []


# --- #1190: cash identity and lifecycle refusals still stop every command ----


@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
@pytest.mark.parametrize("command", _OWNED)
@pytest.mark.parametrize(
    ("fault", "message"),
    [
        pytest.param(
            "identity",
            f"Robinhood reconciliation refused: {CASH_IDENTITY_REFUSAL}",
            id="identity",
        ),
        pytest.param("pending-cash", PENDING_CASH_REFUSAL, id="pending-cash"),
        pytest.param(
            "late",
            f"Robinhood reconciliation refused: {PENDING_SETTLE_REFUSAL}",
            id="late",
        ),
        pytest.param(
            "drift",
            f"Robinhood reconciliation refused: {PENDING_MATCH_REFUSAL}",
            id="drift",
        ),
    ],
)
def test_cash_identity_and_lifecycle_refusals_stop_every_command_with_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault: str,
    message: str,
    command: str,
    stale: bool,
) -> None:
    """#1190: unused period fields are gone, but the cash reconciliation's
    identity and lifecycle checks still run on the marked statements. Every
    command exits 4 with the complete message and no account, holder or row
    text, and no output is left, stale or new."""
    _write_bundle(tmp_path / "bundle")
    (tmp_path / "user.toml").write_text(_config_text(), encoding="utf-8")
    _patch_sources(monkeypatch)
    activity, events = _cash_faults(fault)
    monkeypatch.setattr(
        bundle_mod,
        "reconcile_robinhood_accounts",
        lambda *_args, **_kwargs: reconcile_robinhood_cash(activity, events),
    )
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    if stale:
        for name in _OWNED[command]:
            (out / name).write_bytes(b"stale")

    # robinhood-options fetches no prices, so it has no such flag.
    no_fetch = () if command == "robinhood-options" else ("--no-fetch-prices",)
    assert _main(command, tmp_path, *no_fetch) == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: {message}\n"
    assert captured.out == ""
    assert_no_markers(captured.out + captured.err, *_CLEAN_MARKERS)
    assert _left(tmp_path) == []
