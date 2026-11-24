"""``stockplan-fa`` pipeline: Schedule FA Table A3 for a Morgan Stanley StockPlan
(Shareworks) holding, e.g. Google/Alphabet GSUs (RSUs). (#365)

Reconstructs the Dec-31 holdings from the ``IN - Capital Gain/Loss Report``'s
per-vest ``Releases Report`` (vests on/before Dec-31) with
:mod:`ibkr_fa.compute.stockplan_positions`, and routes them through the SAME
broker-agnostic Schedule FA A3 compute the IBKR and NetBenefits pipelines use
(:func:`ibkr_fa.compute.fa_a3.build_fa_a3_rows`).

Fail-closed ("it's taxes — never be wrong"):

* The reconstructed lots are reconciled against the ``Vested Share Holdings``
  aggregate, a snapshot as of its Selected Date: the vests before that date
  must equal the Holdings' exact ``(shares, acquisition value)``, no vest may
  fall on it, the Releases Report must be run on or after it, and it must be
  strictly after Dec-31 (#1290).
* Any sale refuses — intra-year lot disposal (Schedule CG / FIFO lot matching)
  is not yet supported (a future ``stockplan-cg``).
* Each lot's Dec-31 close is resolved via
  :func:`ibkr_fa.compute.dec31_close.resolve_dec31_close` (refuses unless the
  price series has a usable close for the required US market session); its
  peak via the shared resolver (manual-fill if it can't be proven).

Table A2 (the custodial account itself) is a deliberate follow-up: the Dec-31
account cash balance can't be derived from these reports (the Holdings report is
an as-of-run-date snapshot, not Dec-31), so A2 needs a separate cash input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compute.dec31_close import resolve_dec31_close
from ibkr_fa.compute.dividends import aggregate_dividends_by_symbol_inr
from ibkr_fa.compute.fa_a3 import build_fa_a3_rows
from ibkr_fa.compute.holding_valuation import value_holding_lot
from ibkr_fa.compute.lot_overlap import overlapping_lots_for_cy
from ibkr_fa.compute.stockplan_positions import (
    StockplanFaError,
    reconstruct_stockplan_dec31_vests,
    stockplan_holding_lots,
)
from ibkr_fa.config import FaEntityOverride, load_user_config
from ibkr_fa.fx.policy_fa import FaExactDatePolicy
from ibkr_fa.models import FaA3PeakBlankReason
from ibkr_fa.output import RenderContext, Subcommand, output_binding
from ibkr_fa.output._cleanup import RunOutputs, remove_previous_outputs, scan_inputs
from ibkr_fa.parser.stockplan import require_verified_issuer
from ibkr_fa.parser.stockplan.capital_gain_loss import (
    parse_stockplan_capital_gains_report,
    parse_stockplan_releases,
)
from ibkr_fa.parser.stockplan.dividend_report import parse_stockplan_dividend_report
from ibkr_fa.parser.stockplan.holdings import parse_stockplan_holdings
from ibkr_fa.pipeline import (
    FinancialYear,
    _starting_ay_for_calendar_year,
)
from ibkr_fa.prices.cache import DEFAULT_PRICE_CACHE_DIR_NAME
from ibkr_fa.prices.loader import load_price_series, resolve_override_symbols
from ibkr_fa.prices.yahoo import DailyCloseFetcher
from ibkr_fa.types import Isin, Ticker

_MIN_CALENDAR_YEAR = 1990
_MAX_CALENDAR_YEAR = 2099

_CG_PREFIX = "IN - Capital Gain_Loss Report"
_HOLDINGS_PREFIX = "Vested Share Holdings"
_DIVIDEND_PREFIX = "IN - Dividend Report"
_A2_WARNING = (
    "Schedule FA Table A2 was not generated because these StockPlan exports do not "
    "prove the Dec-31 account cash balance or daily account NAV. Obtain complete "
    "calendar-year cash/NAV evidence and fill Table A2 manually (tracked by #375)."
)


@dataclass(frozen=True, slots=True)
class StockplanFaRunReport:
    """Summary of one successful :func:`run_stockplan_fa` call."""

    output_path: Path
    calendar_year: int
    rows_written: int
    held_shares: Decimal
    reconciled_as_of: date
    # One per row whose A3 peak is blank, in row order (#962).
    peak_blank_reasons: tuple[FaA3PeakBlankReason, ...]
    dividend_inr: Decimal
    warnings: tuple[str, ...]
    run_outputs: RunOutputs = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class StockplanCgEvidenceReport:
    """Strict Schedule CG presence evidence from the StockPlan export."""

    report_path: Path
    sales_count: int
    coverage_start: date
    coverage_end: date


def stockplan_inputs(input_dir: Path, config_path: Path) -> tuple[tuple[str, Path], ...]:
    """The config and every export a StockPlan run may read, for the output
    collision guard."""
    prefixes = (_CG_PREFIX, _HOLDINGS_PREFIX, _DIVIDEND_PREFIX)
    exports = [p for p in scan_inputs(input_dir, "*.xlsx") if p.stem.startswith(prefixes)]
    return (("--config", config_path), *(("--input", path) for path in (input_dir, *exports)))


def _discover_one(input_dir: Path, prefix: str) -> Path:
    """The single ``<prefix>*.xlsx`` in ``input_dir``, or refuse."""
    matches = sorted(p for p in input_dir.glob("*.xlsx") if p.stem.startswith(prefix))
    if not matches:
        raise StockplanFaError(f"no {prefix!r} .xlsx found in {input_dir}")
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        raise StockplanFaError(f"multiple {prefix!r} .xlsx in {input_dir} (ambiguous): {names}")
    return matches[0]


def _require_cg_coverage(
    *,
    report_path: Path,
    coverage: tuple[date, date],
    period: tuple[date, date],
    opening_date: date | None,
) -> None:
    """Refuse a CG report that does not cover the account's relevant period.

    That runs from ``opening_date`` (``[stockplan].opening_date``), or from the
    start of ``period`` when it is unset, to the end of ``period``. An opening
    date after ``period`` leaves nothing to prove and refuses. (#600) Neither
    refusal prints a date.
    """
    period_start, period_end = period
    if opening_date is not None and opening_date > period_end:
        raise StockplanFaError(
            f"[stockplan].opening_date is after the period checked, so {report_path.name} "
            "cannot prove zero sales for it; correct the date, or do not run StockPlan "
            "commands for this period"
        )
    required_start = period_start if opening_date is None else max(period_start, opening_date)
    coverage_start, coverage_end = coverage
    if coverage_start > required_start or coverage_end < period_end:
        hint = ""
        if opening_date is None and coverage_start > period_start:
            hint = (
                "; if the StockPlan account opened mid-period, set [stockplan].opening_date "
                "to bound the window"
            )
        raise StockplanFaError(
            f"{report_path.name}'s reporting period does not span the relevant account period; "
            f"sales cannot be proven zero{hint}"
        )


def inspect_stockplan_cg_evidence(
    *, input_dir: Path, config_path: Path, financial_year: FinancialYear
) -> StockplanCgEvidenceReport:
    """Return the proven sale count without generating Schedule CG output."""
    config = load_user_config(config_path)
    config.account.require_status()
    if config.stockplan is None:
        raise StockplanFaError(f"no [stockplan] block in {config_path}")
    require_verified_issuer(config.stockplan.security.symbol, str(config.stockplan.security.isin))
    report_path = _discover_one(input_dir, _CG_PREFIX)
    sales, coverage = parse_stockplan_capital_gains_report(
        report_path, symbol=Ticker(config.stockplan.security.symbol)
    )
    _require_cg_coverage(
        report_path=report_path,
        coverage=coverage,
        period=(financial_year.start_date, financial_year.end_date),
        opening_date=config.stockplan.opening_date,
    )
    return StockplanCgEvidenceReport(
        report_path=report_path,
        sales_count=len(sales),
        coverage_start=coverage[0],
        coverage_end=coverage[1],
    )


def run_stockplan_fa(
    *,
    input_dir: Path,
    config_path: Path,
    output_path: Path,
    calendar_year: int,
    fetch_prices: bool,
    price_cache_dir: Path | None = None,
    price_fetcher: DailyCloseFetcher | None = None,
    price_now: datetime | None = None,
) -> StockplanFaRunReport:
    """Emit a Schedule FA Table A3 review CSV for the StockPlan holding.

    Raises:
        StockplanFaError: Missing/incomplete [stockplan] config, a missing/ambiguous
            input, a sale (unsupported), or a failed Holdings reconciliation.
        ConfigError: ``[stockplan.security]`` is not a verified issuer.
        ClosingPriceError / IbkrFaError: Propagated from the Dec-31 close resolver,
            config loading, parsers, or FX.
    """
    # Fail-closed: clear the target BEFORE any fallible work (config load, parse,
    # reconcile, compute, write) so ANY refused run leaves no stale prior-run CSV,
    # unless the target is the config or an export this run reads.
    remove_previous_outputs((output_path,), stockplan_inputs(input_dir, config_path))

    config = load_user_config(config_path)
    config.account.require_status()
    if config.stockplan is None:
        raise StockplanFaError(
            f"no [stockplan] block in {config_path}; add one to file Schedule FA"
        )
    sec = config.stockplan.security
    require_verified_issuer(sec.symbol, str(sec.isin))
    instrument = sec.instrument
    if instrument is None or not sec.fa_fields_present():
        raise StockplanFaError(
            "incomplete [stockplan.security]: Schedule FA needs entity_name, entity_address, "
            "listing_exchange, issuer_country_code, nature_of_entity"
        )
    if not _MIN_CALENDAR_YEAR <= calendar_year <= _MAX_CALENDAR_YEAR:
        raise StockplanFaError(
            f"calendar year is out of range [{_MIN_CALENDAR_YEAR}, {_MAX_CALENDAR_YEAR}]"
        )
    cy_start = date(calendar_year, 1, 1)
    cy_end = date(calendar_year, 12, 31)
    symbol = Ticker(sec.symbol)
    isin = Isin(sec.isin)

    cg_path = _discover_one(input_dir, _CG_PREFIX)
    holdings_path = _discover_one(input_dir, _HOLDINGS_PREFIX)
    dividend_path = _discover_one(input_dir, _DIVIDEND_PREFIX)

    vests, releases_run_on = parse_stockplan_releases(cg_path, symbol=symbol)
    sales, cg_coverage = parse_stockplan_capital_gains_report(cg_path, symbol=symbol)
    if sales:
        raise StockplanFaError(
            f"{cg_path.name} has {len(sales)} sale(s); stockplan-fa does not yet handle intra-year "
            "disposals (FIFO lot matching / Schedule CG) — that is a future stockplan-cg"
        )
    _require_cg_coverage(
        report_path=cg_path,
        coverage=cg_coverage,
        period=(cy_start, cy_end),
        opening_date=config.stockplan.opening_date,
    )
    holdings = parse_stockplan_holdings(
        holdings_path, symbol=symbol, listing_exchange=instrument.listing_exchange
    )
    dividends, _wht, coverage = parse_stockplan_dividend_report(
        dividend_path, symbol=symbol, isin=isin
    )
    # Fail-closed: the dividend report must span the whole CY, else an in-CY
    # dividend could be missing and the A3 "amount credited" would understate.
    cov_start, cov_end = coverage
    if cov_start > cy_start or cov_end < cy_end:
        raise StockplanFaError(
            f"{dividend_path.name}'s reporting period does not span the calendar year; a "
            "dividend could be missing from the A3 'amount credited' column"
        )

    dec31 = reconstruct_stockplan_dec31_vests(
        vests,
        holdings.shares,
        holdings.acquisition_value,
        cy_end,
        selected_date=holdings.selected_date,
        releases_run_on=releases_run_on,
    )

    prices = load_price_series(
        {symbol: (cy_start, cy_end)},
        cache_dir=price_cache_dir or (input_dir / DEFAULT_PRICE_CACHE_DIR_NAME),
        fetch=fetch_prices,
        fetcher=price_fetcher,
        now=price_now,
        fetch_symbols=resolve_override_symbols(
            ((symbol, isin),),
            {(o.ibkr_ticker, o.isin): o.yahoo_symbol for o in config.prices.ticker_overrides},
        ),
    )
    series = prices.get(symbol)

    # StockPlan has no Dec-31 broker total, so Yahoo is the close source.
    open_lots = [
        value_holding_lot(
            lot,
            report_date=cy_end,
            close=resolve_dec31_close(
                valuation_date=cy_end,
                quantity=lot.quantity,
                statement_total_usd=None,
                series=series,
                missing_source_remedy=(
                    "StockPlan has no manual Dec-31 price setting, so the run needs a usable "
                    "Yahoo close, fetched or from a valid price cache; if the Yahoo symbol is "
                    "wrong, check [[prices.ticker_overrides]]"
                ),
            ),
        )
        for lot in stockplan_holding_lots(dec31.held, instrument, sec.entity_name)
    ]

    fx_policy = FaExactDatePolicy(max_carry_forward_days=config.fx.max_carry_forward_days)
    dividends_by_symbol_inr = aggregate_dividends_by_symbol_inr(
        dividends, calendar_year=calendar_year, fx_policy=fx_policy
    )
    overlap = overlapping_lots_for_cy(open_lots, [], calendar_year=calendar_year)
    rows = build_fa_a3_rows(
        overlap,
        calendar_year=calendar_year,
        fx_policy=fx_policy,
        nature_of_entity=sec.nature_of_entity,
        dividends_by_symbol_inr=dividends_by_symbol_inr,
        prices_by_ticker=prices,
        prices_fetched=fetch_prices,
        peak_basis=config.schedule_fa.peak_basis,
        peak_currency=config.schedule_fa.peak_currency,
        entity_overrides={
            isin: FaEntityOverride(isin=isin, address=sec.entity_address, zip_code=sec.entity_zip)
        },
    )

    ay_start = _starting_ay_for_calendar_year(calendar_year)
    # The FA A3 writer streams to the final path, so a failure from the write to
    # the report would leave a PARTIAL or unreported CSV — remove it if anything
    # raises (matches netbenefits-fa).
    run_outputs = RunOutputs((output_path,), ())
    with run_outputs.removed_on_failure():
        output_binding(Subcommand.FA, "review-csv").writer(
            RenderContext(assessment_year_start=ay_start)
        ).write(rows, output_path)

        return StockplanFaRunReport(
            output_path=output_path,
            calendar_year=calendar_year,
            rows_written=len(rows),
            held_shares=dec31.held_shares,
            reconciled_as_of=dec31.reconciled_as_of,
            peak_blank_reasons=tuple(
                r.peak_blank_reason for r in rows if r.peak_blank_reason is not None
            ),
            dividend_inr=dividends_by_symbol_inr.get(symbol, Decimal("0")),
            warnings=(_A2_WARNING,),
            run_outputs=run_outputs,
        )
