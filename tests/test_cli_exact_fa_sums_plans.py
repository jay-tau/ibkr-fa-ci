"""Exact Schedule FA sums through the NetBenefits, StockPlan and Fidelity CMA
commands (#1168).

Owner decision (2026-11-10, on #1166, applied to #1168): the Schedule FA
securities, per-symbol dividend and A2 gross-credit totals never round. They
add in a copy of the active context widened to at least ``EXACT_SUM_DIGITS``
and refuse only a nonfinite amount, a decimal arithmetic failure or an
addition that would still round at that precision (the cap).

Each refusal here is reached through the real pipeline, with only an upstream
source of operands replaced where no fixture can carry them (each test says
which and why). All values are synthetic; the shared amounts and privacy
markers come from :mod:`tests._fa_exact_sums`.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import pytest

import ibkr_fa.cli
import ibkr_fa.pipeline_fidelity_cma_fa as cma_mod
import ibkr_fa.pipeline_netbenefits_fa as nb_mod
import ibkr_fa.pipeline_stockplan_fa as sp_mod
from ibkr_fa.cli import (
    _EXIT_DOMAIN_ERROR,
    _RUN_ALL_RULE,
    _RUN_ALL_STATUS_LABEL,
    _STOCKPLAN_CONTRACT_POINTER,
    main,
)
from ibkr_fa.compute.dec31_close import CloseSource, ResolvedClose
from ibkr_fa.config import load_user_config
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import Dividend, InterestCredit
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.models.source import FidelityCmaSourceRecord
from ibkr_fa.output.netbenefits_quicko_fa_xlsx import QUICKO_TEMPLATE_FILENAME
from ibkr_fa.pipeline_fidelity_cma_fa import _SPAXX_ENTITY
from ibkr_fa.pipeline_stockplan_fa import run_stockplan_fa
from ibkr_fa.types import Isin, Ticker
from tests._crash_reports import assert_no_markers
from tests._fa_exact_sums import (
    DESCRIPTION,
    HUGE,
    ISIN,
    MARKED_DATE,
    MINUTE,
    NUMERIC,
    SIX,
    SOURCE,
    SYMBOL,
    assert_refusal,
    message,
)
from tests._fidelity_cma_fa_run import cma_fa_year_end as _year_end
from tests._fidelity_cma_fa_run import prepare_cma_fa as cma_prepare
from tests._fidelity_cma_fa_run import redemption_statement as _redemption_statement
from tests._fidelity_cma_fa_run import run_cma_fa_cli as cma_cli
from tests._fx_stubs import AuditedFxStub
from tests._netbenefits_e2e import (
    NB_E2E_CBDT_FRAGMENT,
    NB_E2E_PDF_TEXT,
    NB_E2E_TOML_WITH_PEAK,
    nb_e2e_bundle,
)
from tests._netbenefits_fa import write_quicko_template
from tests._source_all import SUMMARY_FOOTER as NB_SUMMARY_FOOTER
from tests._source_all import SUMMARY_FOOTER as SP_SUMMARY_FOOTER
from tests._source_all import patch_netbenefits_handlers as stub_netbenefits_legs
from tests._source_all import patch_stockplan_handlers as stub_stockplan_legs
from tests._source_all import real_netbenefits_fa as _real_netbenefits_fa
from tests._source_all import real_stockplan_fa as _real_stockplan_fa
from tests._source_all import write_stale_netbenefits_outputs as _stale_all_outputs
from tests._stockplan_fa_exports import STOCKPLAN_PRICE_NOW as _PRICE_NOW
from tests._stockplan_fa_exports import setup_stockplan_exports as stockplan_exports
from tests._stockplan_fa_exports import stub_fetcher

_IN_CY = date(2025, 9, 10)
_BEFORE_CY = date(2024, 12, 10)


def _assert_context(error: InvariantError, source_row_index: int | None) -> None:
    """``error`` keeps no context value but the source row index, if any."""
    expected = () if source_row_index is None else (("source_row_index", source_row_index),)
    assert error.context == expected


def _dividend(
    amount: str,
    *,
    symbol: str = SYMBOL,
    isin: str = ISIN,
    payment_date: date = _IN_CY,
    source_row_index: int | None = None,
) -> Dividend:
    """A synthetic USD cash dividend carrying the description and source markers."""
    return Dividend(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        payment_date=payment_date,
        amount_gross=Decimal(amount),
        currency="USD",
        description=DESCRIPTION,
        source_row_index=source_row_index,
        kind=DistributionKind.CASH_DIVIDEND,
        type_slice=SOURCE,
    )


def _interest(
    amount: str, *, credit_date: date = _IN_CY, source_row_index: int | None = None
) -> InterestCredit:
    """A synthetic USD interest credit carrying the description marker."""
    return InterestCredit(
        credit_date=credit_date,
        amount_gross=Decimal(amount),
        currency="USD",
        description=f"{DESCRIPTION} {SOURCE}",
        source_row_index=source_row_index,
    )


# ---- netbenefits-fa / netbenefits-all: securities USD -----------------------

_NB_AT = "NetBenefits FA securities, input lot {}, field total_usd"
# The confirmation fixture's Market Value at Purchase, which each Dec-31 close
# must match within its anchor tolerance (about $0.03 for 3.482 shares).
_NB_FMV = "743.30"
# 1001 significant digits: even 0 + this close rounds at EXACT_SUM_DIGITS.
_NB_LONE = "743.30" + "0" * 995 + "1"
# 1000 significant digits, exact alone; added to _NB_FMV the sum needs 1001.
_NB_SECOND = "743.3" + "0" * 995 + "1"
# (closes, failing lot, lots): one lot whose close can't be held exactly, and
# the bundle's one Dec-31 lot resolved twice with closes whose sum can't.
_NB_CASES = pytest.mark.parametrize(
    ("closes", "lot", "lots"),
    [((_NB_LONE,), 1, 1), ((_NB_FMV, _NB_SECOND), 2, 2)],
    ids=["one-lot", "two-lots"],
)
# The numeric marker on the unused override: NUMERIC's first 20 places, since a
# user.toml price may have at most 20 (#1275).
_NB_NUMERIC = NUMERIC[: len("1.") + 20]
# The markers the marked bundle carries.
_NB_MARKERS = (SYMBOL, DESCRIPTION, SOURCE, MARKED_DATE.isoformat(), _NB_NUMERIC)


def _marked_netbenefits_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closes: Sequence[str],
    lots: int,
) -> tuple[Path, Path]:
    """The e2e NetBenefits bundle with a Quicko template, under the symbol,
    description, source and date markers, its Dec-31 lot resolved ``lots``
    times and closed at ``closes`` in turn.

    A real bundle resolves one lot (the confirmations are keyed by date), and
    its close is quantity x a 28-digit price, so no bundle can carry closes
    that need more than EXACT_SUM_DIGITS digits: the close resolver and, for
    two lots, the lot reconstruction are replaced. Each close is a valid
    total-only (statement) close within the confirmation's anchor tolerance.
    """
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    write_quicko_template(bundle / QUICKO_TEMPLATE_FILENAME)
    confirmation = NB_E2E_PDF_TEXT["confirmation-dec.pdf"]
    assert "SYMBOL: XESP " in confirmation
    assert "\nEXAMPLE CORP " in confirmation
    monkeypatch.setitem(
        NB_E2E_PDF_TEXT,
        "confirmation-dec.pdf",
        confirmation.replace("SYMBOL: XESP ", f"SYMBOL: {SYMBOL} ").replace(
            "\nEXAMPLE CORP ", f"\n{DESCRIPTION}  "
        ),
    )
    toml = NB_E2E_TOML_WITH_PEAK
    for old, new in (
        ('symbol = "XESP"', f'symbol = "{SYMBOL}"'),
        ('source = "SPS year-end statement"', f'source = "{SOURCE}"'),
    ):
        assert old in toml
        toml = toml.replace(old, new)
    # A second override, months before the carry-back window, so no run uses it.
    toml += (
        f"\n[[netbenefits.price_overrides]]\nprice_date = {MARKED_DATE}\n"
        f'price_per_share_usd = "{_NB_NUMERIC}"\nsource = "{SOURCE}"\nreason = "{DESCRIPTION}"\n'
    )
    cfg.write_text(toml, encoding="utf-8")

    if lots > 1:
        reconstruct = nb_mod.reconstruct_espp_dec31_lots

        def repeated(*args: Any, **kwargs: Any) -> list[Any]:
            [lot] = reconstruct(*args, **kwargs)
            return [lot] * lots

        monkeypatch.setattr(nb_mod, "reconstruct_espp_dec31_lots", repeated)
    totals = iter(closes)

    def resolve_close(*_args: object, **_kwargs: object) -> ResolvedClose:
        return ResolvedClose(
            total_usd=Decimal(next(totals)),
            per_share_usd=None,
            source=CloseSource.STATEMENT,
            provenance=f"{SOURCE}: {DESCRIPTION}",
        )

    monkeypatch.setattr("ibkr_fa.compute.espp_positions._resolve_close", resolve_close)
    return bundle, cfg


@pytest.mark.parametrize(
    ("output", "format_args", "stale"),
    [
        pytest.param(
            "schedule-fa-a3.csv",
            ("--quicko-workbook",),
            (
                "schedule-fa-a3.csv",
                "schedule-fa-a2.csv",
                "schedule-fa-a3-a2.csv",
                "schedule-fa-quicko.xlsx",
            ),
            id="filing-csv",
        ),
        pytest.param(
            "schedule-fa.json",
            ("--format", "cbdt-json-fragment", "--itr-form", "ITR-2"),
            ("schedule-fa.json",),
            id="cbdt-json",
        ),
    ],
)
@_NB_CASES
def test_netbenefits_fa_securities_past_the_exact_cap_refuse_and_clear_the_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    closes: tuple[str, ...],
    lot: int,
    lots: int,
    output: str,
    format_args: tuple[str, ...],
    stale: tuple[str, ...],
) -> None:
    """#1168: Dec-31 closes whose sum needs more than EXACT_SUM_DIGITS digits
    refuse at the lot that can't be added. Exit 4, the refusal alone on
    stderr, nothing on stdout, no marker printed, and an earlier run's filing
    output is gone. Base rounded the securities total, then filed two lots
    and refused one at the A2 closing check."""
    bundle, cfg = _marked_netbenefits_bundle(tmp_path, monkeypatch, closes, lots)
    out = tmp_path / "out"
    out.mkdir()
    for name in stale:
        prior = json.dumps(NB_E2E_CBDT_FRAGMENT) if name.endswith(".json") else "stale\n"
        (out / name).write_text(prior, encoding="utf-8")

    rc = main(
        [
            "netbenefits-fa",
            *("--input", str(bundle), "--config", str(cfg), "--output", str(out / output)),
            *("--calendar-year", "2025", "--no-fetch-prices", *format_args),
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {message(_NB_AT.format(lot))}\n"
    assert captured.out == ""
    assert_no_markers(captured.out + captured.err, *_NB_MARKERS)
    assert list(out.iterdir()) == []


@_NB_CASES
def test_netbenefits_fa_securities_cap_refusal_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closes: tuple[str, ...],
    lot: int,
    lots: int,
) -> None:
    """#1168: the cap refusal leaves ``run_netbenefits_fa`` as the bare
    InvariantError (no chained error, no context value, no marker) and no
    A3, A2 or Quicko file is written."""
    bundle, cfg = _marked_netbenefits_bundle(tmp_path, monkeypatch, closes, lots)
    out = tmp_path / "schedule-fa-a3.csv"

    with pytest.raises(InvariantError) as raised:
        nb_mod.run_netbenefits_fa(
            input_dir=bundle,
            config_path=cfg,
            output_path=out,
            calendar_year=2025,
            fetch_prices=False,
            quicko_workbook=True,
        )

    assert_refusal(raised.value, message(_NB_AT.format(lot)), markers=_NB_MARKERS)
    _assert_context(raised.value, None)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bundle", "user.toml"]


def test_netbenefits_all_fa_leg_securities_cap_fails_the_run_and_clears_every_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1168: the real FA leg refuses two Dec-31 closes whose sum needs more
    than EXACT_SUM_DIGITS digits (income and CG are stubbed). The run exits 4,
    leaves no output and prints the leg's refusal in the orchestrator's frame,
    as for any other FA-leg refusal (#1165)."""
    calls = stub_netbenefits_legs(monkeypatch, {"fa": 0, "income": 0, "cg": 0})
    monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_fa", _real_netbenefits_fa)
    bundle, cfg = _marked_netbenefits_bundle(tmp_path, monkeypatch, (_NB_FMV, _NB_SECOND), 2)
    _stale_all_outputs(tmp_path)
    out = tmp_path / "out"

    rc = main(
        [
            "netbenefits-all",
            *("--input", str(bundle), "--config", str(cfg), "--output-dir", str(out)),
            *("--financial-year", "2025-26", "--itr-form", "ITR-2", "--quicko-workbook"),
            "--no-fetch-prices",
        ]
    )

    assert rc == _EXIT_DOMAIN_ERROR
    assert sorted(calls) == ["cg", "income"]
    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    removed = "(output removed because the run failed)"
    assert captured.err == "\n".join(
        (
            f"error: {message(_NB_AT.format(2))}",
            _RUN_ALL_RULE,
            "  netbenefits-all summary (FY 2025-26)",
            _RUN_ALL_RULE,
            f"  {_RUN_ALL_STATUS_LABEL['error']}  netbenefits-fa",
            f"  {_RUN_ALL_STATUS_LABEL['ok']}  netbenefits-income  {removed}",
            f"  {_RUN_ALL_STATUS_LABEL['ok']}  netbenefits-cg  {removed}",
            f"{NB_SUMMARY_FOOTER}\n",
        )
    )
    assert captured.out == "".join(
        f"\n{_RUN_ALL_RULE}\n  netbenefits-all: {leg}\n{_RUN_ALL_RULE}\n"
        for leg in ("netbenefits-fa", "netbenefits-income", "netbenefits-cg")
    )
    assert_no_markers(captured.out + captured.err, *_NB_MARKERS)


# ---- stockplan-fa / stockplan-all: per-symbol dividends ---------------------

_DIVIDEND_AT = "Schedule FA per-symbol dividends, input row {}, column amount_gross"
_SP_COVERAGE = (date(1900, 1, 1), date(2026, 6, 21))
_SP_DIVIDEND_MARKERS = (SYMBOL, ISIN, DESCRIPTION, SOURCE, MARKED_DATE.isoformat(), NUMERIC)


def _cap_dividends(source_row_index: int | None) -> list[Dividend]:
    """A 2024 payment, the numeric marker on the date marker, then HUGE and
    MINUTE: the symbol total can't hold input row 4 exactly."""
    return [
        _dividend("1", payment_date=_BEFORE_CY),
        _dividend(NUMERIC, payment_date=MARKED_DATE),
        _dividend(HUGE),
        _dividend(MINUTE, payment_date=date(2025, 12, 10), source_row_index=source_row_index),
    ]


def _patch_stockplan_dividends(
    monkeypatch: pytest.MonkeyPatch, dividends: Sequence[Dividend]
) -> None:
    """Replace the StockPlan dividend report parser with ``dividends``.

    The report's amounts are workbook numbers read as floats, so it can't
    carry 1E-995 or a 29-digit amount; the pipeline still discovers the real
    report and checks the returned coverage spans the year.
    """

    def parse(_path: Path, **_kwargs: object) -> tuple[list[Dividend], list[Any], Any]:
        return list(dividends), [], _SP_COVERAGE

    monkeypatch.setattr(sp_mod, "parse_stockplan_dividend_report", parse)


def _run_stockplan(input_dir: Path, cfg: Path, out: Path) -> sp_mod.StockplanFaRunReport:
    return run_stockplan_fa(
        input_dir=input_dir,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
        fetch_prices=True,
        price_cache_dir=input_dir / ".cache",
        price_fetcher=stub_fetcher,
        price_now=_PRICE_NOW,
    )


_SOURCE_ROW = pytest.mark.parametrize(
    "source_row_index", [None, 7], ids=["no-source-row", "source-row"]
)


@_SOURCE_ROW
def test_stockplan_fa_dividend_cap_refusal_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_row_index: int | None
) -> None:
    """#1168: a symbol total past the exact cap leaves ``run_stockplan_fa`` as
    the bare InvariantError naming the payment's position before date
    filtering (and its source row when the model has one); it is never a zero
    or manual-fill dividend, and the stale A3 CSV is gone. Base added at 28
    digits and filed."""
    input_dir, cfg, out = stockplan_exports(tmp_path)
    out.write_text("stale\n", encoding="utf-8")
    _patch_stockplan_dividends(monkeypatch, _cap_dividends(source_row_index))

    with pytest.raises(InvariantError) as raised:
        _run_stockplan(input_dir, cfg, out)

    assert_refusal(
        raised.value,
        message(_DIVIDEND_AT.format(4), source_row_index=source_row_index),
        markers=_SP_DIVIDEND_MARKERS,
    )
    _assert_context(raised.value, source_row_index)
    assert not out.exists()


@_SOURCE_ROW
def test_cli_stockplan_fa_dividend_cap_refuses_and_clears_the_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source_row_index: int | None,
) -> None:
    """#1168: exit 4, the refusal then the StockPlan contract pointer on
    stderr, nothing on stdout, no marker printed, and no A3 CSV left."""
    input_dir, cfg, out = stockplan_exports(tmp_path)
    out.write_text("stale\n", encoding="utf-8")
    _patch_stockplan_dividends(monkeypatch, _cap_dividends(source_row_index))
    monkeypatch.setattr(
        "ibkr_fa.cli.run_stockplan_fa",
        partial(run_stockplan_fa, price_fetcher=stub_fetcher, price_now=_PRICE_NOW),
    )

    rc = main(
        [
            "stockplan-fa",
            *("--input", str(input_dir), "--config", str(cfg)),
            *("--output", str(out), "--calendar-year", "2025"),
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    expected = message(_DIVIDEND_AT.format(4), source_row_index=source_row_index)
    assert captured.err == f"error: {expected}\n{_STOCKPLAN_CONTRACT_POINTER}\n"
    assert captured.out == ""
    assert_no_markers(captured.out + captured.err, *_SP_DIVIDEND_MARKERS)
    assert not out.exists()


def test_cli_stockplan_all_fa_leg_dividend_cap_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1168: the real stockplan-fa leg refuses a symbol total past the exact
    cap (income succeeds and Schedule CG is stubbed as not required). The run exits 4,
    every output is removed and the summary is the one any FA-leg refusal
    prints (#1165)."""
    stub_stockplan_legs(monkeypatch, {"income": 0, "fa": 0, "cg": ibkr_fa.cli._SOURCE_NOT_REQUIRED})
    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_fa", _real_stockplan_fa)
    monkeypatch.setattr(
        "ibkr_fa.cli.run_stockplan_fa",
        partial(run_stockplan_fa, price_fetcher=stub_fetcher, price_now=_PRICE_NOW),
    )
    input_dir, cfg, _ = stockplan_exports(tmp_path)
    _patch_stockplan_dividends(monkeypatch, _cap_dividends(None))
    out = tmp_path / "out"
    out.mkdir()
    for names in ibkr_fa.cli._STOCKPLAN_ALL_TARGETS.values():
        for name in names:
            (out / name).write_text("stale", encoding="utf-8")

    rc = main(
        [
            "stockplan-all",
            *("--input", str(input_dir), "--config", str(cfg), "--output-dir", str(out)),
            *("--financial-year", "2025-26", "--itr-form", "ITR-2"),
        ]
    )

    assert rc == _EXIT_DOMAIN_ERROR
    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    assert captured.out == "".join(
        f"\n{_RUN_ALL_RULE}\n  stockplan-all: {name}\n{_RUN_ALL_RULE}\n"
        for name in ("stockplan-income", "stockplan-fa", "Schedule CG")
    )
    assert captured.err == (
        f"error: {message(_DIVIDEND_AT.format(4))}\n"
        f"{_RUN_ALL_RULE}\n"
        "  stockplan-all summary (FY 2025-26)\n"
        f"{_RUN_ALL_RULE}\n"
        f"  {_RUN_ALL_STATUS_LABEL['ok']}  stockplan-income  "
        "(output removed because the run failed)\n"
        f"  {_RUN_ALL_STATUS_LABEL['error']}  stockplan-fa\n"
        "  not required (zero sales proven)  Schedule CG\n"
        f"  {_RUN_ALL_STATUS_LABEL['manual_fill']}  Schedule FA Table A2 "
        "(complete daily cash/NAV evidence unavailable)\n"
        f"{SP_SUMMARY_FOOTER}\n"
        f"{_STOCKPLAN_CONTRACT_POINTER}\n"
    )
    assert_no_markers(captured.out + captured.err, *_SP_DIVIDEND_MARKERS)


_A3_GROSS = "Total gross amount paid/credited with respect to the holding during the period"


def _a3_gross_cells(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [row[_A3_GROSS] for row in csv.DictReader(handle)]


@pytest.mark.parametrize(
    ("amounts", "total"),
    [
        pytest.param(SIX, "1.50", id="six-payments"),
        pytest.param(tuple(reversed(SIX)), "1.50", id="discarded-zeros"),
        pytest.param(("0.40", "0.35", "0.75"), "1.50", id="cents"),
    ],
)
def test_stockplan_fa_files_the_exact_dividend_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, amounts: tuple[str, ...], total: str
) -> None:
    """#1168: at ₹1/$ the six-payment repro (LONG then five TINY) totals
    exactly $1.50 and the first A3 row files ₹2 (base kept LONG and filed ₹1).
    Preservation pins: the reversed six (whose 29th digit is a zero) and
    ordinary cent amounts file ₹2 as before. The parser is replaced because
    the report's amounts are workbook floats; the FX policy is a flat ₹1."""
    input_dir, cfg, out = stockplan_exports(tmp_path)
    stockplan = load_user_config(cfg).stockplan
    assert stockplan is not None
    isin = stockplan.security.isin  # the fixture's GOOG ISIN
    _patch_stockplan_dividends(
        monkeypatch,
        [_dividend(amount, symbol="GOOG", isin=isin) for amount in amounts],
    )
    monkeypatch.setattr(sp_mod, "FaExactDatePolicy", partial(AuditedFxStub, Decimal(1)))

    report = _run_stockplan(input_dir, cfg, out)

    assert report.dividend_inr == Decimal(total)
    assert _a3_gross_cells(out) == ["2", "0", "0", "0"]


# ---- fidelity-cma-fa: per-symbol dividends and A2 gross credits -------------

_SPAXX_ISIN = _SPAXX_ENTITY.isin  # the CMA fixture config's spaxx_isin
_GROSS_DIVIDENDS_AT = "Schedule FA A2 gross dividends, contribution {}, field dividend_total_inr"
_INTEREST_AT = "Schedule FA A2 gross interest, input row {}, column amount_gross"
_PROCEEDS_AT = "Schedule FA A2 gross proceeds, input lot {}, field gross_proceeds_inr"
_CMA_MARKERS = (SYMBOL, ISIN, DESCRIPTION, SOURCE, MARKED_DATE.isoformat(), NUMERIC)


def _marked_dividend() -> Dividend:
    """An accepted dividend carrying every marker, the numeric one as its amount."""
    return _dividend(NUMERIC, payment_date=MARKED_DATE)


def _marked_interest() -> InterestCredit:
    """An accepted interest credit with the numeric marker on the date marker."""
    return _interest(NUMERIC, credit_date=MARKED_DATE)


@dataclass(frozen=True, kw_only=True)
class _CmaCase:
    """One CMA run's synthetic income and closed-lot gross proceeds, and the
    refusal's ``at`` label and source row (``at`` is unused by accepted runs)."""

    dividends: tuple[Dividend, ...] = ()
    interests: tuple[InterestCredit, ...] = ()
    proceeds: tuple[str, ...] = ()
    at: str = ""
    source_row_index: int | None = None


# Each site's refusal, with the marked dividend (and interest) accepted first
# wherever the site allows.
_CMA_CAP_CASES = pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _CmaCase(
                dividends=(_marked_dividend(), _dividend(HUGE), _dividend(MINUTE)),
                at=_DIVIDEND_AT.format(3),
            ),
            id="per-symbol-dividends",
        ),
        pytest.param(
            _CmaCase(
                dividends=(
                    _dividend(NUMERIC, symbol="XQDA", payment_date=MARKED_DATE),
                    _dividend(HUGE, symbol="XQDB"),
                    _dividend(MINUTE),
                ),
                at=_GROSS_DIVIDENDS_AT.format(3),
            ),
            id="gross-dividends",
        ),
        pytest.param(
            _CmaCase(
                dividends=(_marked_dividend(),),
                interests=(
                    _marked_interest(),
                    _interest(HUGE),
                    _interest(MINUTE, source_row_index=4),
                ),
                at=_INTEREST_AT.format(3),
                source_row_index=4,
            ),
            id="interest",
        ),
        pytest.param(
            _CmaCase(
                dividends=(_marked_dividend(),),
                interests=(_marked_interest(),),
                proceeds=(HUGE, MINUTE),
                at=_PROCEEDS_AT.format(2),
            ),
            id="proceeds",
        ),
    ],
)


def _prepare_cma(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: _CmaCase,
    *,
    flat_fx: bool = False,
) -> tuple[Path, Path]:
    """The CMA fixture statements (with a redemption when ``case`` has
    proceeds), with ``case``'s income in place of the statements' and, for
    proceeds, the redeemed lot repeated once per ``case`` gross proceeds.

    CMA statements print amounts to the cent and a redemption's proceeds are
    units x NAV, so no statement carries 1E-995 or a 29-digit amount, or
    redemptions whose INR proceeds need more than EXACT_SUM_DIGITS digits:
    the source record's income and the inventory's closed lots are replaced.
    ``flat_fx`` converts every date at ₹1/$.
    """
    redemption = bool(case.proceeds)
    config, output = cma_prepare(
        tmp_path,
        monkeypatch,
        statement=_redemption_statement() if redemption else None,
        year_end=_year_end(quantity="6", market_value="6") if redemption else None,
    )
    load_statements = cma_mod.load_fidelity_cma_statements

    def with_income(*args: Any, **kwargs: Any) -> tuple[Any, Any, FidelityCmaSourceRecord]:
        bundle, year_ends, record = load_statements(*args, **kwargs)
        assert record.dividends == record.interest_credits == ()
        return (
            bundle,
            year_ends,
            replace(record, dividends=case.dividends, interest_credits=case.interests),
        )

    monkeypatch.setattr(cma_mod, "load_fidelity_cma_statements", with_income)
    if redemption:
        build_inventory = cma_mod.build_fidelity_cma_lot_inventory

        def with_proceeds(*args: Any, **kwargs: Any) -> Any:
            inventory = build_inventory(*args, **kwargs)
            [lot] = inventory.closed_lots
            return replace(
                inventory,
                closed_lots=tuple(
                    replace(lot, gross_proceeds_money=Decimal(amount)) for amount in case.proceeds
                ),
            )

        monkeypatch.setattr(cma_mod, "build_fidelity_cma_lot_inventory", with_proceeds)
    if flat_fx:
        monkeypatch.setattr(cma_mod, "FaExactDatePolicy", partial(AuditedFxStub, Decimal(1)))
    return config, output


def _cma_outputs(output: Path) -> tuple[Path, Path]:
    return output, output.with_name(f"{output.stem}-a2{output.suffix}")


@_CMA_CAP_CASES
def test_fidelity_cma_fa_cap_refusal_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: _CmaCase
) -> None:
    """#1168: a per-symbol dividend, A2 gross dividend, interest or proceeds
    total past the exact cap leaves ``run_fidelity_cma_fa`` as the bare
    InvariantError at its site, with the source row only when the failing
    model has one; no A3 or A2 output is left. Base added at 28 digits and
    wrote both files."""
    config, output = _prepare_cma(tmp_path, monkeypatch, case)
    for path in _cma_outputs(output):
        path.write_bytes(b"STALE")

    with pytest.raises(InvariantError) as raised:
        cma_mod.run_fidelity_cma_fa(
            input_dir=tmp_path, config_path=config, output_path=output, calendar_year=2025
        )

    assert_refusal(
        raised.value,
        message(case.at, source_row_index=case.source_row_index),
        markers=_CMA_MARKERS,
    )
    _assert_context(raised.value, case.source_row_index)
    assert not any(path.exists() for path in _cma_outputs(output))


@_CMA_CAP_CASES
def test_cli_fidelity_cma_fa_cap_refuses_and_clears_the_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: _CmaCase,
) -> None:
    """#1168: exit 4, the refusal alone on stderr, nothing on stdout, no
    marker printed, and the stale A3 and A2 files are gone."""
    config, output = _prepare_cma(tmp_path, monkeypatch, case)
    for path in _cma_outputs(output):
        path.write_bytes(b"STALE")

    assert cma_cli(tmp_path, config, output) == 4
    captured = capsys.readouterr()
    expected = message(case.at, source_row_index=case.source_row_index)
    assert captured.err == f"error: {expected}\n"
    assert captured.out == ""
    assert_no_markers(captured.out + captured.err, *_CMA_MARKERS)
    assert not any(path.exists() for path in _cma_outputs(output))


def _a2_gross_by_nature(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    prefix = "Gross amount paid/credited to the account during the period"
    return {row[f"{prefix} - Nature of amount"]: row[f"{prefix} - Amount"] for row in rows}


@pytest.mark.parametrize(
    ("dividends", "interests", "proceeds"),
    [
        pytest.param(SIX, SIX, SIX, id="six"),
        pytest.param(("0.40", "1.10"), ("1.25", "0.25"), ("1.5",), id="cents"),
    ],
)
def test_fidelity_cma_fa_files_exact_gross_totals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dividends: tuple[str, ...],
    interests: tuple[str, ...],
    proceeds: tuple[str, ...],
) -> None:
    """#1168: at ₹1/$ the six-amount repro totals exactly $1.50 for the SPAXX
    dividend, the interest and the redemption proceeds, so A3 and every A2
    nature file ₹2 (base kept LONG and filed ₹1). Preservation pin: ordinary
    amounts with the same totals file ₹2 as before."""
    case = _CmaCase(
        dividends=tuple(_dividend(a, symbol="SPAXX", isin=_SPAXX_ISIN) for a in dividends),
        interests=tuple(_interest(a) for a in interests),
        proceeds=proceeds,
    )
    config, output = _prepare_cma(tmp_path, monkeypatch, case, flat_fx=True)

    cma_mod.run_fidelity_cma_fa(
        input_dir=tmp_path, config_path=config, output_path=output, calendar_year=2025
    )

    assert _a3_gross_cells(output)[0] == "2"
    assert _a2_gross_by_nature(_cma_outputs(output)[1]) == {
        "Dividend": "2",
        "Interest": "2",
        "Proceeds from sale or redemption of financial assets": "2",
    }
