"""Tests for the ``netbenefits-cg`` pipeline (#357 / the ``#I13`` wiring).

The compute (``compute_espp_capital_gains``) and bundle discovery
(``load_bundle``) are tested in their own suites; here we pin the pipeline
WIRING: discovery result -> the statement evidence checks (#666) -> real §49(2AA)
compute -> real CG writers -> report, plus the config/output guards. Since #666 a
run files only an FY the Stock Plan Services statements prove had no sale, so every
in-FY closed lot refuses before the compute (whose sale rules stay covered in
``test_compute_espp_capital_gains.py`` until #1150). Statements are synthetic
:class:`StatementData` behind the income suite's fake ``pdftotext_layout`` +
``parse_statement``, or the committed text fixtures parsed for real. FX is a flat
deterministic policy so the test doesn't depend on the vendored TTBR CSV.
"""

from __future__ import annotations

import csv
import errno
import hashlib
import os
from datetime import date
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest

import ibkr_fa.cli
from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.espp_capital_gains import EsppCapitalGainsResult
from ibkr_fa.exceptions import InvariantError, ParseError
from ibkr_fa.parser.netbenefits.bundle import NetBenefitsBundle
from ibkr_fa.parser.netbenefits.confirmation_pdf import EspPurchase
from ibkr_fa.parser.netbenefits.lots_csv import EspClosedLot
from ibkr_fa.parser.netbenefits.statement_pdf import ReportType, StatementData
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_netbenefits_cg import (
    NetBenefitsCgError,
    NetBenefitsCgRunReport,
    run_netbenefits_cg,
)
from tests._cg_lines import (
    loss_across_bands_ltcg_result,
    loss_across_bands_stcg_result,
    ltcg_result,
    stcg_result,
)
from tests._compute_builders import espp_closed_lot
from tests._crash_reports import assert_no_markers
from tests._fx_stubs import AuditedFxStub
from tests._netbenefits import CG_SHORT_REFUSAL as _SHORT_REFUSAL
from tests._netbenefits import CG_WINDOW as _CG_WINDOW
from tests._netbenefits import (
    CLOSED_EMPTY_CSV,
    CLOSED_RENAMED_PROCEEDS_CSV,
    CLOSED_RENAMED_PROCEEDS_REFUSAL,
    CONF_DEC,
    INSTITUTION_TOML,
    OPEN_RECASED_CSV,
    PARTICIPANT_TOML,
    SECURITY_TOML,
    esp_purchase,
    in_fy_sale_refusal,
    patch_pdftotext,
)
from tests._netbenefits import DATED_LINE_UNREAD as _UNREAD_DATED_LINE
from tests._netbenefits import cg_opening_refusal as _opening_refusal
from tests._netbenefits_income import income_statement as _statement
from tests._netbenefits_income import patch_statements as _patch_statements

_CONFIG = f"""\
[account]
number = "U1234567"
holder_name = "Sample Holder"
opening_date = 2024-02-01
status = "BeneficialOwner"

[account.foreign_tins]
US = "123456789"

{PARTICIPANT_TOML}
[[netbenefits.cg_fmv_values]]
purchase_date = 2023-06-01
section17_gross_fmv_total_inr = "24000"
source = "Employer perquisite worksheet"

[[netbenefits.cg_fmv_values]]
purchase_date = 2026-01-15
section17_gross_fmv_total_inr = "24000"
source = "Employer perquisite worksheet"

{INSTITUTION_TOML}account_opening_date = 2025-11-01
status = "BeneficialOwner"

{SECURITY_TOML}"""

_FY = FinancialYear(start_year=2026)
_FX_RATE = Decimal("85")


_FlatFxPolicy = partial(AuditedFxStub, _FX_RATE)

# sha256 of the two empty CSVs a run with no in-FY sale writes, taken at a839ac33
# (#1149, before #666) for FY 2025-26 and FY 2026-27 alike: #666 adds checks, not
# output, so a proven-zero run writes the same bytes. #1361: each CSV gains its
# Table F block of five zeros; without it each hashes to its earlier digest.
_EMPTY_OUTPUT_SHA256 = {
    "schedule-cg-ltcg.csv": "14e46d53cdff8f12b8116653491ed743d290150ef6e4a1079334d5e9efb32035",
    "schedule-cg.csv": "892feba67df26de34f2d24ff2b3fb76a6971f8139c5fa18de26abec1424baca4",
}


def _output_sha256(out: Path) -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in out.iterdir()}


# Stock Plan Services reports covering FY 2026-27's capital-gains window: the
# configured opening (2025-11-01) is before the FY, so the window runs from
# 2026-04-01 through ten days after the FY end, 2027-04-10.
_FY_REPORTS: dict[str, StatementData] = {
    "statement_2026-04.pdf": _statement(date(2026, 4, 1), date(2026, 12, 31)),
    "statement_2027-01.pdf": _statement(date(2027, 1, 1), date(2027, 4, 30)),
}

_GAP_REFUSAL = (
    f"the statements (year-end reports excluded) leave a gap in {_CG_WINDOW} "
    "(coverage is needed to prove no sale was missed there)"
)


def _in_fy_refusal(row: int, fy: str = "2026-27") -> str:
    return in_fy_sale_refusal(row, fy)


def _stub_reports(
    monkeypatch: pytest.MonkeyPatch,
    bundle_dir: Path,
    reports: dict[str, StatementData] = _FY_REPORTS,
) -> None:
    """Put ``reports`` in ``bundle_dir`` behind the income suite's fake parser."""
    for name in reports:
        (bundle_dir / name).write_bytes(b"%PDF-1.4 placeholder")
    _patch_statements(monkeypatch, reports)


def _purchase(**overrides: object) -> EspPurchase:
    defaults: dict[str, object] = {
        "purchase_date": date(2026, 1, 15),
        "settlement_date": date(2026, 1, 15),
        "offering_period_end": date(2026, 1, 15),
        "shares": Decimal("2"),
        "purchase_price_per_share_usd": Decimal("135"),
        "fmv_total_usd": Decimal("300"),  # fmv/share 150; 150 x 0.9 = 135
        "contribution_usd": Decimal("270"),
        "perquisite_usd": Decimal("30"),
    }
    return esp_purchase(**{**defaults, **overrides})


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    closed: tuple[EspClosedLot, ...],
    purchases: tuple[EspPurchase, ...],
    config: str = _CONFIG,
    reports: dict[str, StatementData] = _FY_REPORTS,
) -> tuple[Path, Path, Path]:
    """Serve a bundle of the ``closed`` lots and ``purchases``, the flat FX policy and
    ``reports``; write ``config``. Returns the bundle folder, the config path and the
    output folder, made unless it exists."""
    bundle = NetBenefitsBundle(
        open_lots=(),
        closed_lots=closed,
        confirmations_by_date={p.purchase_date: p for p in purchases},
    )
    monkeypatch.setattr(
        "ibkr_fa.pipeline_netbenefits_cg.load_bundle", lambda _input_dir, **_kw: bundle
    )
    monkeypatch.setattr("ibkr_fa.fx.context.Rule115CapitalGainsPolicy", _FlatFxPolicy)
    cfg = tmp_path / "user.toml"
    cfg.write_text(config, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    (bundle_dir := tmp_path / "bundle").mkdir()
    _stub_reports(monkeypatch, bundle_dir, reports)
    return bundle_dir, cfg, out


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    closed: tuple[EspClosedLot, ...],
    purchases: tuple[EspPurchase, ...],
    config: str = _CONFIG,
    itr_form: ItrForm = "ITR-2",
    reports: dict[str, StatementData] = _FY_REPORTS,
    financial_year: FinancialYear = _FY,
):
    bundle_dir, cfg, out = _setup(
        tmp_path, monkeypatch, closed=closed, purchases=purchases, config=config, reports=reports
    )
    report = run_netbenefits_cg(
        input_dir=bundle_dir,
        config_path=cfg,
        output_dir=out,
        financial_year=financial_year,
        itr_form=itr_form,
    )
    return report, out


def _run_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    closed: tuple[EspClosedLot, ...],
    purchases: tuple[EspPurchase, ...],
    config: str = _CONFIG,
) -> tuple[int, Path]:
    """Run ``netbenefits-cg`` for FY 2026-27 and ITR-2 on :func:`_setup`'s inputs.
    Returns the exit code and the output folder."""
    bundle_dir, cfg, out = _setup(
        tmp_path, monkeypatch, closed=closed, purchases=purchases, config=config
    )
    rc = ibkr_fa.cli.main(
        [
            "netbenefits-cg",
            "--input",
            str(bundle_dir),
            "--config",
            str(cfg),
            "--output-dir",
            str(out),
            "--financial-year",
            "2026-27",
            "--itr-form",
            "ITR-2",
        ]
    )
    return rc, out


@pytest.mark.parametrize(
    ("closed", "purchase"),
    [
        pytest.param(espp_closed_lot(), _purchase(), id="short-term"),
        pytest.param(
            espp_closed_lot(acquire_date=date(2023, 6, 1), fidelity_term="Long"),
            _purchase(
                purchase_date=date(2023, 6, 1),
                offering_period_start=date(2023, 6, 1),
                offering_period_end=date(2023, 6, 1),
            ),
            id="long-term",
        ),
        pytest.param(espp_closed_lot(proceeds_usd=Decimal("0")), _purchase(), id="zero-proceeds"),
    ],
)
def test_in_fy_sale_refuses_before_the_compute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closed: EspClosedLot,
    purchase: EspPurchase,
) -> None:
    """#666: the statements cover the window, but the closed-lots CSV lists an in-FY
    sale, which ibkr-fa can't yet check against them. It refuses before the compute,
    whatever the sale's term or proceeds; a zero-proceeds row meets this refusal
    ahead of the compute's own (#894)."""
    monkeypatch.setattr(
        "ibkr_fa.compute.espp_capital_gains.compute_espp_capital_gains",
        lambda *_args, **_kwargs: pytest.fail("the compute ran for an in-FY sale"),
    )
    with pytest.raises(NetBenefitsCgError) as caught:
        _run(tmp_path, monkeypatch, closed=(closed,), purchases=(purchase,))
    assert str(caught.value) == _in_fy_refusal(1)
    assert list((tmp_path / "out").iterdir()) == []
    # #666: the row's dates, quantity and amounts are file content.
    for marker in ("2026-06-15", "2026-01-15", "2023-06-01", "400", "270"):
        assert marker not in str(caught.value)


def test_in_fy_sale_names_its_closed_lots_data_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_of_fy = espp_closed_lot(
        acquire_date=date(2025, 12, 1), sold_or_transferred_date=date(2026, 1, 15)
    )
    with pytest.raises(NetBenefitsCgError) as caught:
        _run(tmp_path, monkeypatch, closed=(out_of_fy, espp_closed_lot()), purchases=(_purchase(),))
    assert str(caught.value) == _in_fy_refusal(2)
    assert_no_markers(str(caught.value), "XESP", "99999E104", "2026-06-15", "24000")
    assert list((tmp_path / "out").iterdir()) == []


_YEAR_END = _statement(date(2026, 1, 1), date(2026, 12, 31), report_type=ReportType.YEAR_END)


@pytest.mark.parametrize(
    ("reports", "refusal"),
    [
        pytest.param({}, _SHORT_REFUSAL, id="no-statement"),
        # A year-end report's silence proves nothing, so it never counts.
        pytest.param({"statement_ye.pdf": _YEAR_END}, _SHORT_REFUSAL, id="only-year-end"),
        pytest.param(
            {
                "statement_2026-04.pdf": _statement(date(2026, 4, 1), date(2026, 5, 31)),
                "statement_2026-07.pdf": _statement(date(2026, 7, 1), date(2027, 4, 30)),
            },
            _GAP_REFUSAL,
            id="gap-inside-the-window",
        ),
        pytest.param(
            {"statement_2026-04.pdf": _statement(date(2026, 4, 1), date(2027, 3, 31))},
            _SHORT_REFUSAL,
            id="ends-at-the-fy-end",
        ),
        pytest.param(
            {"statement_2026-04.pdf": _statement(date(2026, 4, 1), date(2027, 4, 9))},
            _SHORT_REFUSAL,
            id="ends-a-day-short-of-the-ten-days",
        ),
        pytest.param(
            {"statement_2026-04.pdf": _statement(date(2026, 4, 1), date(2027, 2, 28))},
            _SHORT_REFUSAL,
            id="ends-before-the-fy-end",
        ),
    ],
)
def test_reports_that_dont_cover_the_window_refuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reports: dict[str, StatementData],
    refusal: str,
) -> None:
    """#666: without reports covering the FY through ten days after its end, a
    sale could be missing from the closed-lots CSV, so nothing is filed."""
    with pytest.raises(NetBenefitsCgError) as caught:
        _run(tmp_path, monkeypatch, closed=(), purchases=(), reports=reports)
    assert str(caught.value) == refusal
    assert_no_markers(str(caught.value), "XESP", "99999E104", "2025-11-01", "24000")
    assert list((tmp_path / "out").iterdir()) == []


def test_reports_through_exactly_ten_days_after_the_fy_end_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = {"statement_2026-04.pdf": _statement(date(2026, 4, 1), date(2027, 4, 10))}
    report, out = _run(tmp_path, monkeypatch, closed=(), purchases=(), reports=reports)
    assert (report.stcg_rows, report.ltcg_rows) == (0, 0)
    assert _output_sha256(out) == _EMPTY_OUTPUT_SHA256


# An opening after FY 2025-26's end. (The config refuses a future opening date, so
# an opening after the current FY's end can't load at all.)
_LATE_OPENING_CONFIG = _CONFIG.replace(
    "account_opening_date = 2025-11-01", "account_opening_date = 2026-04-15"
)
_FY_2025_26 = FinancialYear(start_year=2025)


def test_opening_after_the_fy_end_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#666: an opening date after the FY end leaves an empty window, which would
    prove nothing."""
    assert _LATE_OPENING_CONFIG != _CONFIG
    with pytest.raises(NetBenefitsCgError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            closed=(),
            purchases=(),
            config=_LATE_OPENING_CONFIG,
            financial_year=_FY_2025_26,
        )
    assert str(caught.value) == _opening_refusal("2025-26")
    assert_no_markers(str(caught.value), "XESP", "99999E104", "2026-04-15", "24000")
    assert list((tmp_path / "out").iterdir()) == []


def _with_stale_outputs(tmp_path: Path) -> Path:
    """An output folder holding an earlier run's two CSVs."""
    out = tmp_path / "out"
    out.mkdir()
    for name in ("schedule-cg.csv", "schedule-cg-ltcg.csv"):
        (out / name).write_text("stale", encoding="utf-8")
    return out


@pytest.mark.parametrize(
    ("reports", "config", "closed", "financial_year", "refusal"),
    [
        pytest.param({}, _CONFIG, (), _FY, _SHORT_REFUSAL, id="no-reports"),
        pytest.param(
            _FY_REPORTS,
            _LATE_OPENING_CONFIG,
            (),
            _FY_2025_26,
            _opening_refusal("2025-26"),
            id="opening-after-the-fy-end",
        ),
        pytest.param(
            _FY_REPORTS, _CONFIG, (espp_closed_lot(),), _FY, _in_fy_refusal(1), id="in-fy-sale"
        ),
    ],
)
def test_a_refusal_removes_the_earlier_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reports: dict[str, StatementData],
    config: str,
    closed: tuple[EspClosedLot, ...],
    financial_year: FinancialYear,
    refusal: str,
) -> None:
    out = _with_stale_outputs(tmp_path)
    with pytest.raises(NetBenefitsCgError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            closed=closed,
            purchases=(_purchase(),),
            config=config,
            reports=reports,
            financial_year=financial_year,
        )
    assert str(caught.value) == refusal
    assert_no_markers(str(caught.value), "XESP", "99999E104", "24000")
    assert list(out.iterdir()) == []


# ---- the committed statement fixtures, parsed for real (#666) ------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "netbenefits"
# FY 2025-26's window: from the configured opening (2025-11-01, inside the first
# report) through 2026-04-10, covered by these four reports.
_FIXTURE_REPORTS = (
    "statement-oct-dec",
    "statement-jan",
    "statement-feb-mar",
    "statement-apr",
)
# A marker-prefixed sale row, which #1149's parser refuses as an unread dated line.
_SALE_ROW = "t {mm_dd} SYNTHETIC HOLDER TWO 99997Q104 You Sold -1.000 $400.00000 - $400.00\n"


def _run_fixture_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra: dict[str, str] | None = None,
) -> tuple[NetBenefitsCgRunReport, Path]:
    """Run FY 2025-26 with no closed lot over the committed report fixtures, each
    ``extra`` line appended to its report, parsed by the real statement parser."""
    texts: dict[str, str] = {
        f"{name}.pdf": (_FIXTURES / f"{name}.txt").read_text(encoding="utf-8")
        for name in _FIXTURE_REPORTS
    }
    for name, line in (extra or {}).items():
        texts[name] += line
    bundle = NetBenefitsBundle(open_lots=(), closed_lots=(), confirmations_by_date={})
    monkeypatch.setattr(
        "ibkr_fa.pipeline_netbenefits_cg.load_bundle", lambda _input_dir, **_kw: bundle
    )
    patch_pdftotext(monkeypatch, texts)
    monkeypatch.setattr("ibkr_fa.fx.context.Rule115CapitalGainsPolicy", _FlatFxPolicy)
    (bundle_dir := tmp_path / "bundle").mkdir()
    for name in texts:
        (bundle_dir / name).write_bytes(b"%PDF-1.4 placeholder")
    cfg = tmp_path / "user.toml"
    cfg.write_text(_CONFIG, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    report = run_netbenefits_cg(
        input_dir=bundle_dir,
        config_path=cfg,
        output_dir=out,
        financial_year=FinancialYear(start_year=2025),
    )
    return report, out


def test_no_sales_writes_empty_csvs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#666 proven zero: the reports cover FY 2025-26 through 2026-04-10 and hold no
    sale, and the closed-lots CSV lists none, so the run writes the same empty
    CSVs as before #666. (FY 2025-26 here; the test used FY 2026-27 before.)"""
    report, out = _run_fixture_reports(tmp_path, monkeypatch)
    assert (report.stcg_rows, report.ltcg_rows) == (0, 0)
    assert _output_sha256(out) == _EMPTY_OUTPUT_SHA256


@pytest.mark.parametrize(
    ("report", "mm_dd"),
    [
        pytest.param("statement-apr.pdf", "04/06", id="settling-in-the-ten-days-after"),
        pytest.param("statement-feb-mar.pdf", "03/20", id="inside-the-fy"),
    ],
)
def test_a_report_holding_a_sale_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: str, mm_dd: str
) -> None:
    """#666: netbenefits-cg now parses the statements, so a sale row in any of them
    refuses through #1149's parser, even with an empty closed-lots CSV."""
    out = _with_stale_outputs(tmp_path)
    with pytest.raises(ParseError) as caught:
        _run_fixture_reports(tmp_path, monkeypatch, extra={report: _SALE_ROW.format(mm_dd=mm_dd)})
    error = caught.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        f"statement {report}",
        0,
        None,
        "",
    )
    assert error.message == _UNREAD_DATED_LINE
    for marker in ("Synthetic Holder Two", "99997Q104", "400.00", mm_dd):
        assert marker.casefold() not in str(error).casefold()
    assert list(out.iterdir()) == []


def _bundle_of_csvs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, csvs: dict[str, str]) -> Path:
    """A real bundle directory holding only ``csvs`` (name to text), read by the
    real ``load_bundle``."""
    monkeypatch.setattr("ibkr_fa.fx.context.Rule115CapitalGainsPolicy", _FlatFxPolicy)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name, text in csvs.items():
        (bundle / name).write_text(text, encoding="utf-8")
    (tmp_path / "user.toml").write_text(_CONFIG, encoding="utf-8")
    (tmp_path / "out").mkdir()
    return bundle


def test_open_lots_export_with_a_changed_header_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #911: every column is an open-lots column, and CG never reads open lots.
    bundle = _bundle_of_csvs(
        tmp_path,
        monkeypatch,
        {"View closed lots.csv": CLOSED_EMPTY_CSV, "View open lots.csv": OPEN_RECASED_CSV},
    )
    _stub_reports(monkeypatch, bundle)
    report = run_netbenefits_cg(
        input_dir=bundle,
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path / "out",
        financial_year=_FY,
    )
    assert (report.stcg_rows, report.ltcg_rows) == (0, 0)


def test_cli_refuses_a_renamed_closed_lots_export_and_clears_stale_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#911: the bundle's other closed-lots export is empty, so ignoring the
    renamed one would file no sales."""
    bundle = _bundle_of_csvs(
        tmp_path,
        monkeypatch,
        {
            "View closed lots.csv": CLOSED_EMPTY_CSV,
            "View closed lots (1).csv": CLOSED_RENAMED_PROCEEDS_CSV,
        },
    )
    out = tmp_path / "out"
    for name in ("schedule-cg.csv", "schedule-cg-ltcg.csv"):
        (out / name).write_text("stale", encoding="utf-8")
    rc = ibkr_fa.cli.main(
        [
            "netbenefits-cg",
            "--input",
            str(bundle),
            "--config",
            str(tmp_path / "user.toml"),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
        ]
    )
    assert rc == 4
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert err == f"error: View closed lots (1).csv {CLOSED_RENAMED_PROCEEDS_REFUSAL}\n"
    # #1041: the renamed header cell is file content.
    assert "proceeds (usd)" not in err.casefold()


def test_cli_refuses_a_signed_confirmation_gain_and_clears_stale_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#914: ``$74.34-`` used to parse as +74.34, which still passes the
    perquisite check (743.30 - 668.96)."""
    bundle = _bundle_of_csvs(tmp_path, monkeypatch, {"View closed lots.csv": CLOSED_EMPTY_CSV})
    (bundle / "espp-purchase_2025-12-31.pdf").write_bytes(b"%PDF-1.4 placeholder")
    signed_gain = CONF_DEC.replace("$74.34\n", "$74.34-\n")
    assert signed_gain != CONF_DEC
    patch_pdftotext(monkeypatch, {"espp-purchase_2025-12-31.pdf": signed_gain})
    out = tmp_path / "out"
    for name in ("schedule-cg.csv", "schedule-cg-ltcg.csv"):
        (out / name).write_text("stale", encoding="utf-8")
    rc = ibkr_fa.cli.main(
        [
            "netbenefits-cg",
            "--input",
            str(bundle),
            "--config",
            str(tmp_path / "user.toml"),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
        ]
    )
    assert rc == 4
    assert list(out.iterdir()) == []
    assert "column 'Gain': expected exactly one Gain; found 0" in capsys.readouterr().err


def test_missing_netbenefits_block_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    no_nb = (
        '[account]\nnumber = "U1234567"\nholder_name = "Sample Holder"\n'
        'opening_date = 2024-02-01\nstatus = "BeneficialOwner"\n'
    )
    with pytest.raises(NetBenefitsCgError, match=r"no \[netbenefits\] block"):
        _run(
            tmp_path,
            monkeypatch,
            closed=(espp_closed_lot(),),
            purchases=(_purchase(),),
            config=no_nb,
        )


def test_output_not_a_directory_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = NetBenefitsBundle(
        open_lots=(), closed_lots=(espp_closed_lot(),), confirmations_by_date={}
    )
    monkeypatch.setattr(
        "ibkr_fa.pipeline_netbenefits_cg.load_bundle", lambda _input_dir, **_kw: bundle
    )
    cfg = tmp_path / "user.toml"
    cfg.write_text(_CONFIG, encoding="utf-8")
    with pytest.raises(NetBenefitsCgError, match="output path is not a directory"):
        run_netbenefits_cg(
            input_dir=tmp_path,
            config_path=cfg,
            output_dir=tmp_path / "nonexistent",
            financial_year=_FY,
        )


_PERMISSIONS_ENFORCED = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)


@_PERMISSIONS_ENFORCED
def test_output_folder_check_raises_eacces_instead_of_calling_it_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """#1238: the output-folder check raises EACCES instead of calling the folder missing."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    loads: list[Path] = []
    for loader in ("load_bundle", "load_statements"):
        monkeypatch.setattr(
            f"ibkr_fa.pipeline_netbenefits_cg.{loader}",
            lambda path, *_a, **_kw: loads.append(path),
        )
    cfg = tmp_path / "user.toml"
    cfg.write_text(_CONFIG, encoding="utf-8")
    blocked = tmp_path / "blocked"
    out = blocked / "out"
    out.mkdir(parents=True)
    blocked.chmod(0)
    request.addfinalizer(lambda: blocked.chmod(0o700))
    with pytest.raises(PermissionError) as raised:
        run_netbenefits_cg(input_dir=bundle, config_path=cfg, output_dir=out, financial_year=_FY)
    assert type(raised.value) is PermissionError
    assert str(raised.value) == f"[Errno {errno.EACCES}] Permission denied: {str(out)!r}"
    assert loads == []
    blocked.chmod(0o700)
    assert list(out.iterdir()) == []


def test_cli_netbenefits_cg_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#666: a run succeeds only as a proven zero, and its note says so."""
    rc, out = _run_cli(tmp_path, monkeypatch, closed=(), purchases=(_purchase(),))
    assert rc == 0
    stdout = capsys.readouterr().out
    assert "Wrote Schedule CG" in stdout
    assert "the statements prove this FY had no sale" in stdout
    assert _output_sha256(out) == _EMPTY_OUTPUT_SHA256


def test_refused_run_clears_stale_prior_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail-closed: a prior success leaves schedule-cg*.csv; a later run that
    # refuses must clear both, leaving no stale fileable output. Here an in-FY
    # sold lot with no matching confirmation meets #666's in-FY refusal before the
    # compute could refuse its missing FMV.
    out = tmp_path / "out"
    out.mkdir()
    stcg = out / "schedule-cg.csv"
    ltcg = out / "schedule-cg-ltcg.csv"
    stcg.write_text("stale stcg", encoding="utf-8")
    ltcg.write_text("stale ltcg", encoding="utf-8")
    # closed lot present, but NO confirmation -> compute can't find the FMV.
    bundle_dir, cfg, _out = _setup(tmp_path, monkeypatch, closed=(espp_closed_lot(),), purchases=())
    with pytest.raises(NetBenefitsCgError) as caught:
        run_netbenefits_cg(
            input_dir=bundle_dir, config_path=cfg, output_dir=out, financial_year=_FY
        )
    assert str(caught.value) == _in_fy_refusal(1)
    assert not stcg.exists()
    assert not ltcg.exists()


def test_cli_refusal_maps_to_domain_error_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _out = _run_cli(tmp_path, monkeypatch, closed=(espp_closed_lot(),), purchases=())
    assert rc == ibkr_fa.cli._EXIT_DOMAIN_ERROR
    assert capsys.readouterr().err == f"error: {_in_fy_refusal(1)}\n"


def test_cli_refuses_zero_proceeds_lot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out = _run_cli(
        tmp_path,
        monkeypatch,
        closed=(espp_closed_lot(proceeds_usd=Decimal("0")),),
        purchases=(_purchase(),),
    )
    # #666: the in-FY refusal fires before the compute's zero-proceeds one (#894).
    assert rc == ibkr_fa.cli._EXIT_DOMAIN_ERROR
    assert capsys.readouterr().err == f"error: {_in_fy_refusal(1)}\n"
    assert list(out.iterdir()) == []


def test_missing_config_refusal_clears_stale_prior_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail-closed even for an EARLY (config-level) refusal: a missing [netbenefits]
    # block must still clear stale prior CSVs — the clear runs before config load.
    out = tmp_path / "out"
    out.mkdir()
    stcg = out / "schedule-cg.csv"
    ltcg = out / "schedule-cg-ltcg.csv"
    stcg.write_text("stale stcg", encoding="utf-8")
    ltcg.write_text("stale ltcg", encoding="utf-8")
    no_nb = (
        '[account]\nnumber = "U1234567"\nholder_name = "Sample Holder"\n'
        'opening_date = 2024-02-01\nstatus = "BeneficialOwner"\n'
    )
    cfg = tmp_path / "user.toml"
    cfg.write_text(no_nb, encoding="utf-8")
    (tmp_path / "bundle").mkdir()
    with pytest.raises(NetBenefitsCgError, match=r"no \[netbenefits\] block"):
        run_netbenefits_cg(
            input_dir=tmp_path / "bundle", config_path=cfg, output_dir=out, financial_year=_FY
        )
    assert not stcg.exists()
    assert not ltcg.exists()


def test_out_of_fy_disposal_counted_and_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lot sold in a DIFFERENT FY is excluded from CG (not this FY's gain) and
    # surfaced as a count so a wrong --financial-year isn't a silent empty run.
    out_of_fy = espp_closed_lot(
        acquire_date=date(2025, 12, 1), sold_or_transferred_date=date(2026, 1, 15)
    )
    report, _ = _run(tmp_path, monkeypatch, closed=(out_of_fy,), purchases=())
    assert report.stcg_rows == 0
    assert report.ltcg_rows == 0
    assert report.out_of_fy_disposals == 1


def test_cli_surfaces_out_of_fy_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out_of_fy = espp_closed_lot(
        acquire_date=date(2025, 12, 1), sold_or_transferred_date=date(2026, 1, 15)
    )
    rc, _out = _run_cli(tmp_path, monkeypatch, closed=(out_of_fy,), purchases=())
    assert rc == 0
    assert "fell OUTSIDE FY" in capsys.readouterr().err


def test_cli_in_fy_sale_beside_an_out_of_fy_one_refuses_without_the_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An all-history export: an in-FY sale plus an out-of-FY disposal. Since #666
    # the in-FY sale refuses (ibkr-fa can't yet check it against the statements),
    # and the refused run prints no out-of-FY warning.
    in_fy = espp_closed_lot()  # sold in FY 2026-27
    out_of_fy = espp_closed_lot(
        acquire_date=date(2025, 12, 1), sold_or_transferred_date=date(2026, 1, 15)
    )
    rc, out = _run_cli(tmp_path, monkeypatch, closed=(in_fy, out_of_fy), purchases=(_purchase(),))
    assert rc == ibkr_fa.cli._EXIT_DOMAIN_ERROR
    assert capsys.readouterr().err == f"error: {_in_fy_refusal(1)}\n"
    assert list(out.iterdir()) == []


def test_run_reports_the_component_totals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#615: both RunReport totals carry the Policy A component balance."""
    monkeypatch.setattr(
        "ibkr_fa.compute.espp_capital_gains.compute_espp_capital_gains",
        lambda *_args, **_kwargs: EsppCapitalGainsResult(
            stcg=stcg_result("1000.4", "1200.6"),
            ltcg=ltcg_result("1000.4", "1200.6"),
        ),
    )

    report, out = _run(tmp_path, monkeypatch, closed=(), purchases=())

    assert report.total_stcg_inr_rounded == Decimal("-201")
    assert report.total_ltcg_inr_rounded == Decimal("-201")
    for filename in ("schedule-cg.csv", "schedule-cg-ltcg.csv"):
        with (out / filename).open(encoding="utf-8-sig", newline="") as fh:
            assert list(csv.reader(fh))[-1][-1] == "'-201"


def test_loss_across_bands_names_each_table_f_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1361: each file whose bands are blank for a loss across bands is named
    with its Table F row, and the command exits 3. No input reaches this until
    an in-FY sale can pass (#1150), so the compute is stubbed."""
    monkeypatch.setattr(
        "ibkr_fa.compute.espp_capital_gains.compute_espp_capital_gains",
        lambda *_args, **_kwargs: EsppCapitalGainsResult(
            stcg=loss_across_bands_stcg_result(), ltcg=loss_across_bands_ltcg_result()
        ),
    )

    report, out = _run(tmp_path, monkeypatch, closed=(), purchases=())

    assert report.table_f_loss_across_bands == (
        (3, "schedule-cg.csv"),
        (5, "schedule-cg-ltcg.csv"),
    )
    monkeypatch.setattr("ibkr_fa.cli.run_netbenefits_cg", lambda **_kwargs: report)
    rc = ibkr_fa.cli.main(
        [
            "netbenefits-cg",
            "--input",
            str(tmp_path / "bundle"),
            "--config",
            str(tmp_path / "user.toml"),
            "--output-dir",
            str(out),
            "--financial-year",
            "2026-27",
            "--itr-form",
            "ITR-2",
        ]
    )
    err = capsys.readouterr().err.splitlines()
    assert rc == 3
    for row, name in report.table_f_loss_across_bands:
        assert (
            f"⚠️  MANUAL-FILL: Schedule CG Table F row {row} needs its date bands entered by "
            f"hand: a band nets a loss and two or more bands net gains; {name} leaves them blank."
        ) in err


@pytest.mark.parametrize("refused", ["STCG", "LTCG"])
def test_refused_schedule_writes_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refused: str
) -> None:
    """#615 owner decision 2: the refused schedule's CSV is never written."""
    amounts = {"STCG": ("1000", "900"), "LTCG": ("1000", "900")}
    amounts[refused] = ("0.40", "0.60")
    monkeypatch.setattr(
        "ibkr_fa.compute.espp_capital_gains.compute_espp_capital_gains",
        lambda *_args, **_kwargs: EsppCapitalGainsResult(
            stcg=stcg_result(*amounts["STCG"]),
            ltcg=ltcg_result(*amounts["LTCG"]),
        ),
    )

    with pytest.raises(InvariantError) as excinfo:
        _run(tmp_path, monkeypatch, closed=(), purchases=())

    assert dict(excinfo.value.context) == {
        "schedule": None,
        "consideration_inr": None,
        "cost_of_acquisition_inr": None,
    }
    refused_name = "schedule-cg.csv" if refused == "STCG" else "schedule-cg-ltcg.csv"
    assert not (tmp_path / "out" / refused_name).exists()


# ---- filed amounts past the CBDT bound (#830) --------------------------------


def test_section_49_2aa_cost_past_the_cbdt_bound_meets_the_in_fy_refusal_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two purchases' section 49(2AA) costs of ₹60,000,000,000,000 each are in
    range; the filed STCG cost of acquisition, their sum, is not. Since #666 their
    in-FY sales refuse before the compute, so no writer runs either way; the bound
    itself stays covered in ``test_compute_espp_capital_gains.py``."""
    early = date(2025, 7, 15)
    monkeypatch.setattr(
        "ibkr_fa.pipeline_netbenefits_cg.write_output_bundle",
        lambda *_args: pytest.fail("a writer ran for a filing past the CBDT bound"),
    )
    # Two purchases still short-term at the sale, each with ₹60,000,000,000,000 of INR FMV.
    config = _CONFIG.replace("2023-06-01", early.isoformat())
    rc, out = _run_cli(
        tmp_path,
        monkeypatch,
        closed=(espp_closed_lot(), espp_closed_lot(acquire_date=early)),
        purchases=(
            _purchase(),
            _purchase(
                purchase_date=early,
                offering_period_start=date(2025, 4, 1),
                offering_period_end=early,
            ),
        ),
        config=config.replace('"24000"', '"60000000000000"'),
    )
    assert rc == 4
    assert capsys.readouterr().err == f"error: {_in_fy_refusal(1)}\n"
    assert list(out.iterdir()) == []
