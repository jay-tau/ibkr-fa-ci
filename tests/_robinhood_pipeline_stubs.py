"""Stand-ins for the Robinhood pipeline's steps, and the stubs that install
them, shared by the Robinhood pipeline and CLI tests (#1766)."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
import ibkr_fa.pipeline_robinhood_core as core
import ibkr_fa.pipeline_robinhood_outputs as outputs
from ibkr_fa.compute.form67 import Form67ComputeResult, Form67Readiness
from ibkr_fa.compute.robinhood_capital_gains import RobinhoodSalePriceCheck
from ibkr_fa.compute.schedule_fsi import compute_schedule_fsi_from_capital_gains
from ibkr_fa.config import (
    Form67Config,
    Form67TaxPayableOverride,
    RobinhoodConfig,
    RobinhoodOptionTreatment,
)
from ibkr_fa.models.form67_entry import Form67SourceOfIncome
from ibkr_fa.normalize.robinhood.security import RobinhoodSecurityEventReference
from ibkr_fa.output._cleanup import FileIdentity
from ibkr_fa.output.cbdt_json_fragment import CbdtScheduleFaJsonWriter
from ibkr_fa.parser.robinhood import RobinhoodEvent
from ibkr_fa.parser.robinhood_1042s import Robinhood1042SPacket
from ibkr_fa.pipeline_robinhood import RobinhoodLegStatus, run_robinhood_all
from tests._robinhood_cg import cg_result
from tests._robinhood_cli import FINANCIAL_YEAR, outputs_left, robinhood_main
from tests._robinhood_fa_json import robinhood_json_result as _robinhood_json_result
from tests._robinhood_income_inputs import income_base_events as _income_base_events
from tests._robinhood_income_inputs import income_config as _income_config
from tests._robinhood_income_inputs import income_packet as _income_packet
from tests._robinhood_income_inputs import income_paired as _income_paired
from tests._robinhood_income_inputs import shared_user_config as _shared_config
from tests._robinhood_income_stubs import form67_assembly
from tests._robinhood_security import configured_securities


def claim_staged(on_staged: Callable[[FileIdentity], None], path: Path) -> None:
    """Claim the file a stub writer published at ``path``, as the real writer
    claims its staging file."""
    info = os.lstat(path)
    on_staged((info.st_dev, info.st_ino))


def stand_in_options(
    *,
    capital: bool = False,
    has_trades: bool = False,
    treatment: RobinhoodOptionTreatment | None = None,
):
    """A stand-in options result with no blocker. ``capital`` adds a capital-gain
    line that needs manual work, ``has_trades`` a trade, and ``treatment`` the
    first account's classification (capital gains when ``capital``)."""
    if capital and treatment is None:
        treatment = RobinhoodOptionTreatment.CAPITAL_GAINS
    return SimpleNamespace(
        required_itr_form=None,
        trades=(object(),) if has_trades else (),
        lot_slices=(),
        valuation_anchors=(),
        validated_valuation_anchors=(),
        blockers=(),
        capital_gain_lines=(object(),) if capital else (),
        table_f={},
        business_summaries=(),
        business_fsi_manual_work_required=False,
        manual_work_required=capital,
        fsi_manual_work_required=capital,
        classifications=(
            () if treatment is None else (SimpleNamespace(account_index=0, treatment=treatment),)
        ),
    )


def stand_in_bundle(
    *,
    has_options: bool = False,
    warnings: tuple[str, ...] = (),
):
    """A stand-in loaded bundle of two labelled accounts with ``warnings``;
    ``has_options`` gives it an option event."""
    references = (object(),) if has_options else ()
    return SimpleNamespace(
        unverified_entity_addresses=(),
        scope=SimpleNamespace(warnings=warnings),
        config=SimpleNamespace(prices=SimpleNamespace(massive_api_key_file=None)),
        paired=SimpleNamespace(
            account_labels={0: "Individual", 1: "Managed Individual"},
        ),
        quantities=SimpleNamespace(
            option_event_references=references,
            option_position_references=(),
        ),
    )


def stand_in_fa_prices(*, warnings: tuple[str, ...] = ()):
    """What the FA price step returns: no price, and ``warnings`` from the
    option prices."""
    return (
        {},
        SimpleNamespace(warnings=(), errors=()),
        SimpleNamespace(series_by_contract={}, warnings=warnings, errors=()),
        SimpleNamespace(),
    )


def stand_in_cg(
    *,
    blockers: tuple[object, ...] = (),
    checks: tuple[RobinhoodSalePriceCheck, ...] = (),
    attributed_lots: tuple[object, ...] = (),
) -> SimpleNamespace:
    """A stand-in CG result whose sections, which the run report reads for
    their Table F rows (#1360), are empty."""
    empty = cg_result()
    return SimpleNamespace(
        blockers=blockers,
        sale_price_checks=checks,
        attributed_lots=attributed_lots,
        stcg=empty.stcg,
        ltcg=empty.ltcg,
    )


def complete_fa(**overrides: object) -> SimpleNamespace:
    """A stand-in Schedule FA result with no row, blocker or manual work, eligible
    for the automatic JSON; ``overrides`` replace its fields."""
    values: dict[str, object] = {
        "a2": (),
        "a2_rows": (),
        "a3_rows": (),
        "blockers": (),
        "manual_work": (),
        "program_bank": (),
        "cbdt_json_eligible": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# The lot of every stub A3 review (#1476).
A3_LOT = RobinhoodSecurityEventReference(0, 2)


def json_fa(
    *,
    manual_work: tuple[object, ...] = (),
    a2_changes: dict[str, object] | None = None,
    build: Callable[..., Any] = _robinhood_json_result,
    **changes: Any,
) -> SimpleNamespace:
    """``complete_fa`` with the A2/A3 rows of ``build(**changes)``, valid ones
    by default, and ``a2_changes`` applied to every A2 row."""
    result = build(**changes)
    a2 = tuple(
        SimpleNamespace(
            row=replace(item.row, **(a2_changes or {})),
            account_id_cross_checked=item.account_id_cross_checked,
        )
        for item in result.a2
    )
    return complete_fa(
        a2=a2,
        a2_rows=tuple(item.row for item in a2),
        a3=tuple(SimpleNamespace(lot_id=A3_LOT, row=row) for row in result.a3_rows),
        a3_rows=result.a3_rows,
        manual_work=manual_work,
        cbdt_json_eligible=result.cbdt_json_eligible,
    )


def stub_fa_before_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub Schedule FA's inventory gate and its blockers before the prices
    (#1574): a stub bundle has no statement, lot or identity for them."""
    monkeypatch.setattr(pipeline, "_fa_inventory", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr(core, "fa_pre_price_blockers", lambda *_args: ())


def stub_successful_all(
    monkeypatch: pytest.MonkeyPatch,
    fa: SimpleNamespace,
    *,
    cg: SimpleNamespace | None = None,
    price_warnings: tuple[str, ...] = (),
) -> None:
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: stand_in_bundle())
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: stand_in_options())
    monkeypatch.setattr(pipeline, "_reconstruct_options", lambda *_args: stand_in_options())
    stub_fa_before_prices(monkeypatch)
    # Stub options hold no trade to classify (#1567).
    monkeypatch.setattr(core, "option_classification_blockers", lambda *_args: ())
    monkeypatch.setattr(
        pipeline,
        "_compute_cg",
        lambda *_args: stand_in_cg() if cg is None else cg,
    )
    checks = () if cg is None else cg.sale_price_checks
    monkeypatch.setattr(pipeline, "_sale_price_checks", lambda _paired: checks)
    monkeypatch.setattr(
        pipeline,
        "_compute_income",
        lambda *_args, **_kwargs: (
            SimpleNamespace(blockers=()),
            form67_assembly(),
            object(),
        ),
    )
    monkeypatch.setattr(
        pipeline, "_compute_source_income", lambda *_args: SimpleNamespace(blockers=())
    )
    monkeypatch.setattr(
        pipeline,
        "_fa_prices",
        lambda *_args, **_kwargs: stand_in_fa_prices(warnings=price_warnings),
    )
    monkeypatch.setattr(pipeline, "_compute_fa", lambda *_args, **_kwargs: fa)
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    monkeypatch.setattr(pipeline, "_write_cg", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        pipeline, "_write_income", lambda *_args, **_kwargs: ((), Form67Readiness.READY)
    )
    monkeypatch.setattr(pipeline, "_write_options", lambda *_args, **_kwargs: None)


def stub_fa_around_income(monkeypatch: pytest.MonkeyPatch, bundle: SimpleNamespace) -> None:
    """Stub loading (it returns ``bundle``) and the non-income computations:
    options (computed and reconstructed), FA prices, FY capital gains, sale-price
    checks, Schedule FA and its steps before the prices. Everything else runs
    for real unless the test stubs it too, including the income steps, event
    validation, the writers, reporting and cleanup."""
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: stand_in_options())
    monkeypatch.setattr(pipeline, "_reconstruct_options", lambda *_args: stand_in_options())
    monkeypatch.setattr(pipeline, "_fa_prices", lambda *_args, **_kwargs: stand_in_fa_prices())
    stub_fa_before_prices(monkeypatch)
    monkeypatch.setattr(pipeline, "_compute_cg", lambda *_args: cg_result())
    monkeypatch.setattr(pipeline, "_sale_price_checks", lambda _paired: ())
    monkeypatch.setattr(
        pipeline, "_compute_fa", lambda *_args, **_kwargs: complete_fa(cbdt_json_eligible=False)
    )


def stub_income_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config: RobinhoodConfig,
    paired: Any,
    packets: tuple[Robinhood1042SPacket, ...],
) -> SimpleNamespace:
    """Load ``config``, ``paired``'s accounts and ``packets`` for the real
    income compute, stubbed as :func:`stub_fa_around_income` does and with
    owned-event validation skipped. Returns the loaded bundle."""
    bundle = stand_in_bundle()
    # Schedule OS's writer and the income reasons read the account labels too.
    bundle.paired = SimpleNamespace(
        accounts=paired.accounts, account_labels=bundle.paired.account_labels
    )
    bundle.packets = packets
    bundle.robinhood = config
    bundle.securities = configured_securities(config)
    bundle.config = _shared_config(tmp_path)
    stub_fa_around_income(monkeypatch, bundle)
    monkeypatch.setattr(pipeline, "_validate_owned_events", lambda *_args: None)
    return bundle


def stub_writing_all(
    monkeypatch: pytest.MonkeyPatch,
    output_dir: Path,
    *,
    failing: str | None = None,
    fa: SimpleNamespace | None = None,
) -> list[Path]:
    """Stub a full run whose five writing legs, and the automatic JSON, write
    real files. The ``failing`` leg's writer writes its file and then raises.
    Returns every path written, in order."""
    stub_successful_all(monkeypatch, complete_fa() if fa is None else fa)
    options = stand_in_options(
        has_trades=True, treatment=RobinhoodOptionTreatment.SPECULATIVE_BUSINESS
    )
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: stand_in_bundle(has_options=True))
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: options)
    written: list[Path] = []

    def write(leg: str, *names: str) -> tuple[Path, ...]:
        paths = tuple(output_dir / name for name in names)
        for path in paths:
            path.write_text(leg, encoding="utf-8")
        written.extend(paths)
        if leg == failing:
            raise OSError(f"{leg} disk failure")
        return paths

    def write_cg(*_args: object, debug: bool, **_kwargs: object) -> tuple[Path, ...]:
        debug_names = ("schedule-cg-debug.csv", "schedule-cg-ltcg-debug.csv") if debug else ()
        return write("equity-cg", "schedule-cg.xlsx", *debug_names)

    def write_json(
        _self: object, _value: object, path: Path, *, on_staged: Callable[[FileIdentity], None]
    ) -> None:
        write("schedule-fa-json", path.name)
        claim_staged(on_staged, path)

    monkeypatch.setattr(pipeline, "_write_cg", write_cg)
    monkeypatch.setattr(
        pipeline, "_write_os", lambda *_args, **_kwargs: write("schedule-os", "schedule-os.xlsx")
    )
    monkeypatch.setattr(
        pipeline,
        "_write_income",
        lambda *_args, **_kwargs: (
            write("income-ftc", "schedule-fsi.csv", "schedule-tr.csv", "form-67.csv"),
            True,
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "_write_fa",
        lambda *_args, **_kwargs: (
            write("schedule-fa", "schedule-fa-a2.csv", "schedule-fa-a3.csv"),
            True,
            (),
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "_write_options",
        lambda *_args, **_kwargs: write("options", "robinhood-options.csv")[0],
    )
    monkeypatch.setattr(CbdtScheduleFaJsonWriter, "write", write_json)
    return written


def assembly_for(cg: Any) -> SimpleNamespace:
    """An FTC assembly whose Schedule FSI capital-gains rows are computed from ``cg``."""
    return SimpleNamespace(
        form67_result=Form67ComputeResult(entries=()),
        fsi_result=compute_schedule_fsi_from_capital_gains(
            cg_stcg_result=cg.stcg, cg_ltcg_result=cg.ltcg
        ),
    )


def stub_cg_run(monkeypatch: pytest.MonkeyPatch, make_cg: Callable[[], Any]) -> list[str]:
    """Stub every Robinhood command around ``make_cg``, writing CG with the real writer.

    ``make_cg`` is called inside the run, as the CG compute would be. Returns
    the FTC writes (``_write_income`` calls) the run attempted.
    """
    stub_successful_all(monkeypatch, complete_fa(cbdt_json_eligible=False))
    monkeypatch.setattr(pipeline, "_write_fa", lambda *_args, **_kwargs: ((), False, ()))
    monkeypatch.setattr(pipeline, "_compute_cg", lambda *_args: make_cg())
    monkeypatch.setattr(
        pipeline,
        "_compute_income",
        lambda _bundle, _fy, cg, **_kwargs: (
            SimpleNamespace(blockers=()),
            assembly_for(cg),
            object(),
        ),
    )
    monkeypatch.setattr(pipeline, "_write_cg", outputs.write_cg)
    ftc_writes: list[str] = []

    def write_income(*_args: object, **_kwargs: object) -> tuple[tuple[Path, ...], Form67Readiness]:
        ftc_writes.append("income-ftc")
        return (), Form67Readiness.READY

    monkeypatch.setattr(pipeline, "_write_income", write_income)
    return ftc_writes


def assert_cg_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    make_cg: Callable[[], Any],
    refusal: str,
) -> None:
    """With every command stubbed around ``make_cg``, robinhood-cg exits 4
    printing ``refusal`` on stderr and nothing on stdout, and writes nothing;
    robinhood-all's equity-cg leg fails with the same text and leaves no
    ``schedule-cg.xlsx``."""
    stub_cg_run(monkeypatch, make_cg)

    assert robinhood_main("robinhood-cg", tmp_path) == 4

    assert capsys.readouterr() == ("", f"error: {refusal}\n")
    assert outputs_left(tmp_path) == []
    report = run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path / "all",
        financial_year=FINANCIAL_YEAR,
        itr_form="ITR-2",
        fetch_prices=False,
    )
    equity_leg = next(leg for leg in report.legs if leg.name == "equity-cg")
    assert equity_leg.status is RobinhoodLegStatus.ERROR
    assert equity_leg.reasons == (refusal,)
    assert not (tmp_path / "all" / "schedule-cg.xlsx").exists()


TIN_MARKER = "999-00-0001"
OTHER_ACCOUNT = "999900001"


def stub_relief_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tin_country: str,
    *,
    events: tuple[RobinhoodEvent, ...] | None = None,
    other_account: str = OTHER_ACCOUNT,
    other_rows: tuple[RobinhoodEvent, ...] = (),
    packet: Robinhood1042SPacket | None = None,
    robinhood: RobinhoodConfig | None = None,
) -> SimpleNamespace:
    """#1084's example through the real income compute: a US dividend of
    $100.00 with $25.00 withheld, matched by its 1042-S, a ready Form 67, and
    one foreign TIN, filed under ``tin_country``. The keywords replace the
    first account's events, the second account and its rows, the form and the
    Robinhood config. Returns the stubbed bundle."""
    bundle = stub_income_bundle(
        monkeypatch,
        tmp_path,
        robinhood or _income_config(other_account=other_account),
        _income_paired(
            _income_base_events() if events is None else events,
            other_account=other_account,
            other_rows=other_rows,
        ),
        (_income_packet() if packet is None else packet,),
    )
    shared = bundle.config
    bundle.config = replace(
        shared,
        account=replace(shared.account, foreign_tins={tin_country: TIN_MARKER}),
        form67=Form67Config(
            (Form67TaxPayableOverride("US", Form67SourceOfIncome.DIVIDEND, Decimal("3000")),)
        ),
    )
    return bundle
