"""#1545: ``robinhood-income`` names the capital-gains cause when a Robinhood
CG blocker stops its FTC schedules.

Schedule FSI, Schedule TR and Form 67 include the Robinhood equity capital
gains, so any CG blocker stops them. On 5cff6af, with no income blocker,
``robinhood-income`` then exited 4 printing only its fixed FTC line. Now its
FTC report, and its Schedule OS refusal when that stops first, add a line
saying so and the text ``robinhood-cg`` prints for the same CG result. Each
case runs the real income compute and a real CG result behind the existing
pipeline stubs, with synthetic inputs only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
import ibkr_fa.pipeline_robinhood_core as core
import ibkr_fa.pipeline_robinhood_outputs as outputs
from ibkr_fa.compute.form67 import Form67ComputeResult, Form67Readiness, form67_row_readiness
from ibkr_fa.compute.robinhood_capital_gains import (
    RobinhoodCapitalGainsBlockerKind,
    RobinhoodCapitalGainsResult,
    compute_robinhood_capital_gains,
)
from ibkr_fa.compute.robinhood_income import RobinhoodIncomeBlockerCause as _Cause
from ibkr_fa.compute.robinhood_lots import RobinhoodLotBlocker, RobinhoodLotBlockerKind
from ibkr_fa.compute.schedule_fsi import (
    ScheduleFsiComputeResult,
    require_fsi_relief_matches_portal,
)
from ibkr_fa.compute.schedule_fsi_relief import (
    enrich_fsi_with_form67_relief,
    require_form67_credit_reconciles,
)
from ibkr_fa.compute.schedule_tr import compute_schedule_tr_from_fsi
from ibkr_fa.models import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.parser.robinhood import RobinhoodEventKind
from ibkr_fa.pipeline_robinhood_models import RobinhoodLegStatus
from ibkr_fa.pipeline_robinhood_outputs import IDENTITY_GUIDANCE_FILENAME
from tests._fx_stubs import AuditedFxStub
from tests._robinhood_cg import CG_FTC_CAUSE as _CAUSE
from tests._robinhood_cg import CG_UNRESOLVED as _CG
from tests._robinhood_cg import (
    LOT_SHORTFALL_CAUSE,
    PRE_CUTOVER_CAUSE,
    UNSUPPORTED_CLASS_CAUSE,
    cg_result,
)
from tests._robinhood_cg_inputs import (
    CG_CUSIP,
    CG_RATE,
    CG_SALE_DATE,
    LONG_TERM_BUY,
    SALE_AT_100,
    SALE_AT_110,
    SALE_ORDER_REASON,
    SHORT_TERM_BUY,
    canonical_cg_case,
    cg_base_events,
    cg_base_slices,
    cg_config,
    cg_from_lots,
    cg_lot_result,
    cg_reference,
    cg_security,
    cg_slice,
    cg_trade,
    sale_order_case,
)
from tests._robinhood_cli import FINANCIAL_YEAR as _FY
from tests._robinhood_cli import outputs_left
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_income_blockers import INCOME_BLOCKER_CASES as _CASES
from tests._robinhood_income_blockers import INCOME_BLOCKER_LINES as _LINES
from tests._robinhood_income_blockers import IncomeCase as _Case
from tests._robinhood_income_blockers import compute_income_case as _income
from tests._robinhood_income_blockers import income_case as _case
from tests._robinhood_pipeline_stubs import stub_income_bundle as _stub_income_bundle
from tests._robinhood_refusals import FTC_OS_REMOVED as _FIXED
from tests._robinhood_refusals import OS_UNRESOLVED as _OS_REFUSAL
from tests._robinhood_security import configured_securities, identity_guidance_line

_Kind = RobinhoodCapitalGainsBlockerKind
_SALE_ORDER = (_CG, SALE_ORDER_REASON)
_FAKE_GUIDANCE = '[[robinhood.securities]]\nisin = "REPLACE"'
_FTC_ONLY = [cause for cause in _Cause if _income(_CASES[cause]()).schedule_os_complete]


def _one_sale(sale_date: date, **security: str) -> RobinhoodCapitalGainsResult:
    """The real CG compute over one 2024 buy and its sale on ``sale_date``."""
    events = (
        cg_trade(RobinhoodEventKind.EQUITY_BUY, 1, date(2024, 1, 1), "1.0000", "10.00", "-10.00"),
        cg_trade(RobinhoodEventKind.EQUITY_SELL, 2, sale_date, "1.0000", "50.00", "50.00"),
    )
    lot = cg_slice(1, (2,), quantity="1.0000", cost="10.00", original_date=date(2024, 1, 1))
    fy_start = date(sale_date.year if sale_date.month > 3 else sale_date.year - 1, 4, 1)
    return cg_from_lots(
        events,
        cg_lot_result((lot,)),
        config=cg_config(securities=(cg_security(**security),)),
        financial_year_start=fy_start,
        financial_year_end=date(fy_start.year + 1, 3, 31),
    )


def _identity_cg() -> RobinhoodCapitalGainsResult:
    """The real CG compute over a sale whose security has no configured identity."""
    paired, config = canonical_cg_case()
    config = replace(config, securities=())
    return compute_robinhood_capital_gains(
        paired,
        config=config,
        securities=configured_securities(config),
        financial_year_start=date(2024, 4, 1),
        financial_year_end=date(2025, 3, 31),
        fx_policy=AuditedFxStub(CG_RATE),
    )


# Real CG results, each with the lines robinhood-cg prints after the fixed CG
# line for its one blocker kind (#1546); evidence-coverage prints none.
_KIND_CG: dict[
    RobinhoodCapitalGainsBlockerKind,
    tuple[Callable[[], RobinhoodCapitalGainsResult], tuple[str, ...]],
] = {
    _Kind.EVIDENCE_COVERAGE: (
        lambda: cg_from_lots(
            cg_base_events(),
            cg_lot_result(cg_base_slices()),
            report_end=date(2025, 12, 31),
        ),
        (),
    ),
    _Kind.LOT_ATTRIBUTION: (
        lambda: cg_from_lots(
            cg_base_events(),
            cg_lot_result(
                (),
                blockers=(
                    RobinhoodLotBlocker(
                        account_index=0,
                        cusip=CG_CUSIP,
                        kind=RobinhoodLotBlockerKind.QUANTITY_SHORTFALL,
                        event_references=(cg_reference(3), cg_reference(4)),
                    ),
                ),
            ),
        ),
        (LOT_SHORTFALL_CAUSE.format(references="sales 0:3, 0:4"),),
    ),
    _Kind.PRE_CUTOVER_HOLDING_RULE: (
        lambda: _one_sale(date(2024, 7, 22)),
        (PRE_CUTOVER_CAUSE.format(sales="0:2 (lots 0:1)"),),
    ),
    _Kind.UNSUPPORTED_SECURITY_CLASS: (
        lambda: _one_sale(CG_SALE_DATE, asset_class="OPT"),
        (
            UNSUPPORTED_CLASS_CAUSE.format(
                sales="0:2 (lots 0:1)", conditions='asset_class isn\'t "STK"'
            ),
        ),
    ),
}


def _stub(
    case: _Case,
    cg: RobinhoodCapitalGainsResult | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load ``case`` for the real income compute, with ``cg`` as the CG
    result (``None``: the harness's, which has no blocker)."""
    _stub_income_bundle(monkeypatch, tmp_path, *case)
    if cg is not None:
        monkeypatch.setattr(pipeline, "_compute_cg", lambda *_args: cg)


def _run(
    command: str,
    case: _Case,
    cg: RobinhoodCapitalGainsResult | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    """Run ``command`` as :func:`_stub` sets it up; return its exit code,
    stdout and stderr."""
    tmp_path.mkdir(exist_ok=True)
    _stub(case, cg, tmp_path, monkeypatch)
    code = _main(command, tmp_path)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _actions(stdout: str) -> list[str]:
    """The ``Action:`` lines of ``stdout``, one per reason."""
    return [
        line.removeprefix("  Action: ")
        for line in stdout.splitlines()
        if line.startswith("  Action: ")
    ]


def test_the_issues_sale_order_blocker_names_the_cg_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1540's example with no income blocker: exit 4 and no output, as on
    5cff6af, which printed the fixed line alone. Now the cause line and
    robinhood-cg's text, #1540's ordering line included, follow it."""
    code, out, err = _run(
        "robinhood-income", _case(), sale_order_case(), tmp_path, monkeypatch, capsys
    )

    assert _actions(out) == [_FIXED, _CAUSE, *_SALE_ORDER]
    assert code == 4
    assert err == ""
    assert outputs_left(tmp_path) == []


@pytest.mark.parametrize("kind", _KIND_CG)
def test_each_other_cg_blocker_kind_gets_the_cause_line_and_its_cg_text(
    kind: RobinhoodCapitalGainsBlockerKind,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """After the cause line, robinhood-cg's text for the kind: the fixed CG
    line, then the kind's own line (#1546), none for evidence-coverage."""
    make_cg, lines = _KIND_CG[kind]
    cg = make_cg()
    assert {blocker.kind for blocker in cg.blockers} == {kind}

    code, out, _err = _run("robinhood-income", _case(), cg, tmp_path, monkeypatch, capsys)

    assert _actions(out) == [_FIXED, _CAUSE, _CG, *lines]
    assert code == 4


def test_an_identity_blocker_names_the_guidance_file_written_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The identity guidance line, as robinhood-cg prints it, names the
    private file; the run writes it once and keeps it."""
    cg = _identity_cg()
    assert {blocker.kind for blocker in cg.blockers} == {_Kind.IDENTITY}
    monkeypatch.setattr(core, "identity_failure_guidance", lambda _bundle: _FAKE_GUIDANCE)
    path = tmp_path / "out" / IDENTITY_GUIDANCE_FILENAME
    writes: list[Path] = []
    atomic_text_output = outputs.atomic_text_output

    def counted(target: Path, *args: Any, **kwargs: Any) -> Any:
        writes.append(target)
        return atomic_text_output(target, *args, **kwargs)

    monkeypatch.setattr(outputs, "atomic_text_output", counted)

    code, out, _err = _run("robinhood-income", _case(), cg, tmp_path, monkeypatch, capsys)

    assert _actions(out) == [_FIXED, _CAUSE, _CG, identity_guidance_line(path)]
    assert code == 4
    assert writes.count(path) == 1
    assert path.read_text(encoding="utf-8") == _FAKE_GUIDANCE
    assert outputs_left(tmp_path) == [IDENTITY_GUIDANCE_FILENAME]


@pytest.mark.parametrize("cause", _FTC_ONLY)
def test_a_cg_blocker_beside_an_ftc_only_income_blocker_lists_both(
    cause: _Cause,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CG lines come after the fixed line; the income lines follow as
    they read without the CG blocker."""
    _code, alone, _err = _run(
        "robinhood-income", _CASES[cause](), None, tmp_path, monkeypatch, capsys
    )
    code, out, _err = _run(
        "robinhood-income",
        _CASES[cause](),
        sale_order_case(),
        tmp_path,
        monkeypatch,
        capsys,
    )

    fixed, *income_lines = _actions(alone)
    assert fixed == _FIXED
    assert _LINES[cause] in income_lines
    assert _actions(out) == [_FIXED, _CAUSE, *_SALE_ORDER, *income_lines]
    assert code == 4


def test_the_schedule_os_refusal_lists_the_cg_cause_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Schedule OS refuses first for Capital Gains Distributions, beside an
    FTC-only form blocker and #1540's CG blocker: the CG lines open the FTC
    part, before the FTC-only income line."""
    config, paired, _packets = _CASES[_Cause.CAPITAL_GAINS_DISTRIBUTIONS]()
    case = (config, paired, _CASES[_Cause.UNSUPPORTED_FORM_CONTENT]()[2])
    assert not _income(case).schedule_os_complete

    code, out, err = _run(
        "robinhood-income", case, sale_order_case(), tmp_path, monkeypatch, capsys
    )

    lines = (
        _OS_REFUSAL,
        _LINES[_Cause.CAPITAL_GAINS_DISTRIBUTIONS],
        _CAUSE,
        *_SALE_ORDER,
        _LINES[_Cause.UNSUPPORTED_FORM_CONTENT],
    )
    assert err == "error: " + "\n".join(lines) + "\n"
    assert out == ""
    assert code == 4
    assert outputs_left(tmp_path) == []


def test_robinhood_all_keeps_its_income_ftc_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Control: robinhood-all names the CG stop on its equity-cg leg and keeps
    5cff6af's income-ftc text."""
    _stub(_case(), sale_order_case(), tmp_path, monkeypatch)

    report = pipeline.run_robinhood_all(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path / "all",
        financial_year=_FY,
        itr_form="ITR-2",
        fetch_prices=False,
    )

    legs = {leg.name: leg for leg in report.legs}
    assert legs["equity-cg"].reasons == ("\n".join(_SALE_ORDER),)
    assert legs["income-ftc"].status is RobinhoodLegStatus.ERROR
    assert legs["income-ftc"].reasons == ("unresolved income blockers",)


def test_following_the_cause_line_completes_what_the_run_withholds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Follow the text word for word. The schedules it says to complete by
    hand are withheld. Complete their workpapers for the same income and
    #1540's gains after a confirmation establishes the $100 sale ran first.
    The same CG input still refuses; this is no configuration or rerun route."""
    blocked, control = tmp_path / "blocked", tmp_path / "control"
    _code, out, _err = _run(
        "robinhood-income", _case(), sale_order_case(), blocked, monkeypatch, capsys
    )
    assert _actions(out)[1:], "the run names no cause"
    cause, *cg_lines = _actions(out)[1:]

    assert cause.endswith(" complete Schedule FSI, Schedule TR and Form 67 by hand")
    assert outputs_left(blocked) == []
    _code, _out, _err = _run("robinhood-income", _case(), None, control, monkeypatch, capsys)
    # Schedule FSI, Schedule TR and Form 67, by the file each is written to.
    ftc_files = {"schedule-fsi.csv", "schedule-tr.csv", "form-67-debug.csv"}
    assert set(outputs_left(control)) - set(outputs.OS_FILENAMES) == ftc_files

    # The control supplies only the unchanged dividend workpaper, not a CG remedy.
    bundle = pipeline._load(
        input_dir=control / "bundle",
        config_path=control / "user.toml",
        financial_year=_FY,
        include_tax_forms=True,
        schedule_fa=False,
    )
    _income_result, assembled, _tr = core.compute_income(
        bundle, _FY, cg_result(), suppress_cross_ay_warning=False
    )
    assert assembled is not None
    [entry] = assembled.form67_result.entries
    assert entry.eligible_foreign_tax_inr is not None
    # Exact Indian tax amounts are synthetic filer inputs, not inferred from the exports.
    indian_dividend_tax, indian_cg_tax = Decimal("3000"), Decimal("1000")
    credit = min(indian_dividend_tax, entry.eligible_foreign_tax_inr)
    entry = replace(
        entry,
        tax_payable_normal_inr=indian_dividend_tax,
        section_90_credit_inr=credit,
        total_credit_inr=credit,
    )
    assert form67_row_readiness(entry).readiness is Form67Readiness.READY
    manual_form67 = Form67ComputeResult(entries=(entry,))
    [dividend] = enrich_fsi_with_form67_relief(assembled.fsi_result, manual_form67).rows

    ltcg = (Decimal(SALE_AT_100[3]) + Decimal(LONG_TERM_BUY[3])) * CG_RATE
    stcg = (Decimal(SALE_AT_110[3]) + Decimal(SHORT_TERM_BUY[3])) * CG_RATE
    assert (stcg, ltcg) == (Decimal("7650"), Decimal("-1700"))
    # The long-term loss cannot offset STCG on Schedule FSI.
    manual_cg = ScheduleFsiRow(
        cbdt_country_code=dividend.cbdt_country_code,
        country_name_portal=dividend.country_name_portal,
        iso_alpha2=dividend.iso_alpha2,
        head_of_income=HeadOfIncome.CAPITAL_GAINS,
        income_inr=stcg,
        foreign_tax_inr=Decimal(0),
        event_count=1,
        tax_payable_inr=indian_cg_tax,
        tax_relief_inr=Decimal(0),
        taxpayer_id_number=dividend.taxpayer_id_number,
    )
    manual_fsi = ScheduleFsiComputeResult(rows=[manual_cg, dividend])
    require_form67_credit_reconciles(manual_fsi, manual_form67)
    require_fsi_relief_matches_portal(manual_fsi.rows, manual_form67)
    assert all(
        row.taxpayer_id_number and row.filed.tax_payable_inr is not None for row in manual_fsi.rows
    )
    [manual_tr] = compute_schedule_tr_from_fsi(manual_fsi).rows
    assert manual_tr.foreign_tax_inr_total == entry.filed.foreign_tax_inr
    assert manual_tr.tax_relief_inr_total == entry.filed.total_credit_inr
    assert manual_tr.section_under_which_claimed is not None
    assert entry.filed.income_inr == dividend.filed.income_inr

    code, _out, err = _run("robinhood-cg", _case(), sale_order_case(), blocked, monkeypatch, capsys)
    assert code == 4
    assert err == "error: " + "\n".join(cg_lines) + "\n"
