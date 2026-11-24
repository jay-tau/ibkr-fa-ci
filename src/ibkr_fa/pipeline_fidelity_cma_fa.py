"""Fidelity CMA Schedule FA Tables A2 and A3 pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compute import aggregate_dividends_by_symbol_inr
from ibkr_fa.compute.fa_a2 import (
    FaA2PeakBounds,
    build_fa_a2_row,
    build_gross_evidence,
    expand_fa_a2_nature_rows,
    fa_a2_manual_fill_gaps,
)
from ibkr_fa.compute.fa_a3 import build_fa_a3_rows, fa_a3_peak_blank_rows
from ibkr_fa.compute.fidelity_cma_capital_gains import (
    build_fidelity_cma_lot_inventory,
)
from ibkr_fa.compute.fidelity_cma_fa import (
    FidelityCmaFaError,
    FidelityCmaPeakBounds,
    compute_fidelity_cma_peak_bounds,
    require_supported_calendar_year,
)
from ibkr_fa.compute.lot_overlap import overlapping_lots_for_cy
from ibkr_fa.config import (
    FaEntityOverride,
    FidelityCmaPeakBalanceBound,
    load_user_config,
)
from ibkr_fa.fx.policy_fa import FaExactDatePolicy
from ibkr_fa.models import FaA3PeakBlankReason
from ibkr_fa.models.account_identity import AccountIdentity, InstitutionIdentity
from ibkr_fa.models.fa_a2_row import FaA2Row
from ibkr_fa.models.net_asset_value import NetAssetValueSummary
from ibkr_fa.models.open_lot import REPORT_DATE_MAX_YEAR as _MAX_CALENDAR_YEAR
from ibkr_fa.models.open_lot import REPORT_DATE_MIN_YEAR as _MIN_CALENDAR_YEAR
from ibkr_fa.normalize.fidelity_cma.year_end import CmaYearEndError, require_year_end_match
from ibkr_fa.output import RenderContext, Subcommand, command_formats
from ibkr_fa.output._cleanup import RunOutputs, remove_previous_outputs
from ibkr_fa.output.bundle import spec_for, write_output_bundle
from ibkr_fa.output.fa_manual_fill import (
    FA_A2_PEAK_LABEL,
    FA_A3_PEAK_VALUE_COLUMN,
    FIDELITY_CMA_A2_GROSS_LABEL,
)
from ibkr_fa.parser.fidelity_cma import load_fidelity_cma_statements
from ibkr_fa.pii import is_masked_account_id
from ibkr_fa.pipeline import _starting_ay_for_calendar_year
from ibkr_fa.pipeline_fidelity_cma_income import fidelity_cma_inputs
from ibkr_fa.prices import load_price_series
from ibkr_fa.prices.cache import DEFAULT_PRICE_CACHE_DIR_NAME
from ibkr_fa.prices.nasdaq import fetch_nasdaq_mutual_fund_daily_closes_usd
from ibkr_fa.prices.yahoo import DailyCloseFetcher
from ibkr_fa.types import Isin, Ticker

_A2_INSTITUTION = InstitutionIdentity(
    name="National Financial Services LLC",
    address="245 Summer Street, Boston, MA",
    zip_code="02210",
    country_iso2="US",
)
_SPAXX_ENTITY = FaEntityOverride(
    isin=Isin("US31617H1023"),
    address="245 Summer Street, Boston, MA",
    zip_code="02210",
    name="Fidelity Government Money Market Fund",
)
_SPAXX = Ticker("SPAXX")
# Manual-fill columns: the portal labels, prefixed with their table.
A2_PEAK_MANUAL_FILL = f"A2 {FA_A2_PEAK_LABEL}"
_A2_GROSS_MANUAL_FILL = f"A2 {FIDELITY_CMA_A2_GROSS_LABEL}"
A3_PEAK_MANUAL_FILL = f"A3 {FA_A3_PEAK_VALUE_COLUMN}"


@dataclass(frozen=True, slots=True, repr=False)
class FidelityCmaFaRunReport:
    output_path: Path
    a2_output_path: Path
    calendar_year: int
    format_id: str
    a3_rows: int
    peak_bounds: FidelityCmaPeakBounds
    selected_peak_bound: FidelityCmaPeakBalanceBound | None
    manual_fill_columns: tuple[str, ...]
    a2_rows: tuple[FaA2Row, ...]
    run_outputs: RunOutputs = field(kw_only=True)
    # Each reason an A3 peak is blank, with the 1-based rows it blanks (#1552).
    a3_peak_blank_rows: tuple[tuple[FaA3PeakBlankReason, tuple[int, ...]], ...] = field(
        kw_only=True
    )

    @property
    def filing_complete(self) -> bool:
        return not self.manual_fill_columns


def _refuse_inverted_attested_bound(
    bounds: FidelityCmaPeakBounds, selected: FidelityCmaPeakBalanceBound | None
) -> None:
    """Refuse to file a bound when the native-ordered bounds invert in INR.

    The message names the config field but not the bounds, their dates or
    TTBRs: they come from the statements (#1047).
    """
    if selected is None or bounds.lower_inr <= bounds.upper_inr:
        return
    raise FidelityCmaFaError(
        f'fidelity-cma-fa refuses peak_balance_bound = "{selected.value}": the A2 '
        "peak bounds are ordered in native currency, and converting each at its "
        "own date's TTBR reverses their rupee order, so the lower bound is the "
        "larger one in INR. "
        "Remove suppress_peak_balance_warning and peak_balance_bound from "
        "[fidelity_cma] to write output with a blank A2 peak."
    )


def run_fidelity_cma_fa(  # noqa: PLR0912, PLR0915
    *,
    input_dir: Path,
    config_path: Path,
    output_path: Path,
    calendar_year: int,
    format_id: str = "review-csv",
    price_cache_dir: Path | None = None,
    price_fetcher: DailyCloseFetcher | None = None,
    price_now: datetime | None = None,
) -> FidelityCmaFaRunReport:
    """Emit CMA A2/A3 review or upload output with audited peak bounds."""
    try:
        a2_path = output_path.with_name(f"{output_path.stem}-a2{output_path.suffix}")
    except ValueError:
        raise FidelityCmaFaError(
            "fidelity-cma-fa requires --output to name a file for the A2 companion output"
        ) from None
    try:
        remove_previous_outputs((output_path, a2_path), fidelity_cma_inputs(input_dir, config_path))
    except ValueError:
        raise FidelityCmaFaError(
            "fidelity-cma-fa requires valid paths for previous-output cleanup"
        ) from None
    if not _MIN_CALENDAR_YEAR <= calendar_year <= _MAX_CALENDAR_YEAR:
        raise FidelityCmaFaError(
            f"calendar year must be between {_MIN_CALENDAR_YEAR} and {_MAX_CALENDAR_YEAR}"
        )
    if format_id not in command_formats("fidelity-cma-fa").choices:
        raise FidelityCmaFaError("fidelity-cma-fa supports filing-csv, review-csv and xlsx only")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    config = load_user_config(config_path)
    if config.fidelity_cma is None:
        raise FidelityCmaFaError("missing [fidelity_cma] config")
    cfg = config.fidelity_cma
    if config.schedule_fa.peak_basis == "high":
        raise FidelityCmaFaError(
            'fidelity-cma-fa refuses schedule_fa.peak_basis = "high": no source '
            "establishes an intraday high for SPAXX, a money market fund"
        )
    if cfg.status is None:
        raise FidelityCmaFaError(
            'fidelity-cma-fa requires status = "BeneficialOwner" in [fidelity_cma], '
            "the observed INDIVIDUAL TOD registration and the only one Schedule FA A2 supports"
        )
    bundle, year_ends, record = load_fidelity_cma_statements(
        input_dir,
        expected_account_id=cfg.account_id,
        spaxx_isin=cfg.spaxx_isin,
    )
    cy_end = date(calendar_year, 12, 31)
    if bundle.statements[0].period_end > cy_end:
        raise FidelityCmaFaError("no CMA monthly statements cover the requested calendar year")
    cy_bundle = bundle.through(cy_end)
    cy_statements = cy_bundle.statements
    try:
        require_year_end_match(
            bundle.statements,
            cy_statements[-1],
            year_ends.get(calendar_year),
            calendar_year=calendar_year,
        )
    except CmaYearEndError as exc:
        raise FidelityCmaFaError(str(exc)) from exc
    require_supported_calendar_year(calendar_year)

    cy_start = max(date(calendar_year, 1, 1), cy_statements[0].period_start)
    cache_root = price_cache_dir or (input_dir / DEFAULT_PRICE_CACHE_DIR_NAME)
    prices = load_price_series(
        {_SPAXX: (cy_start, cy_end)},
        cache_dir=cache_root / "nasdaq-mutual-funds",
        fetcher=price_fetcher or fetch_nasdaq_mutual_fund_daily_closes_usd,
        now=price_now,
    )
    series = prices.get(_SPAXX)
    if series is None:
        raise FidelityCmaFaError(
            "SPAXX daily NAV could not be fetched or loaded: the Nasdaq price source was "
            "unreachable or returned no usable closes. Re-run to fetch it again. If a re-run "
            "still can't get a usable series, enter the Fidelity CMA Schedule FA by hand."
        )
    fx_policy = FaExactDatePolicy(
        max_carry_forward_days=config.fx.max_carry_forward_days,
    )
    bounds = compute_fidelity_cma_peak_bounds(
        cy_bundle,
        calendar_year=calendar_year,
        series=series,
        fx_policy=fx_policy,
        peak_currency=config.schedule_fa.peak_currency,
    )
    _refuse_inverted_attested_bound(bounds, cfg.peak_balance_bound)
    inventory = build_fidelity_cma_lot_inventory(
        cy_bundle,
        spaxx_isin=cfg.spaxx_isin,
        report_date=cy_end,
        mark_price=bounds.closing_nav_usd,
    )
    overlap = overlapping_lots_for_cy(
        inventory.open_lots,
        inventory.closed_lots,
        calendar_year=calendar_year,
    )
    dividends, interests, _withholdings = record.income_in_window(
        start=date(calendar_year, 1, 1),
        end=cy_end,
    )
    dividend_totals = aggregate_dividends_by_symbol_inr(
        dividends,
        calendar_year=calendar_year,
        fx_policy=fx_policy,
    )
    a3_rows = build_fa_a3_rows(
        overlap,
        calendar_year=calendar_year,
        fx_policy=fx_policy,
        nature_of_entity="Money market mutual fund shares",
        dividends_by_symbol_inr=dividend_totals,
        prices_by_ticker={_SPAXX: series},
        entity_overrides={cfg.spaxx_isin: _SPAXX_ENTITY},
        peak_basis="close",
        peak_currency=config.schedule_fa.peak_currency,
        refuse_disposal_day_peak=True,
    )

    gross_evidence = build_gross_evidence(
        dividends,
        interests,
        inventory.closed_lots,
        interest_non_credits=(),
        calendar_year=calendar_year,
        fx_policy=fx_policy,
        dividend_activity_present=False,
    )
    december = cy_statements[-1]
    nav = NetAssetValueSummary(
        closing_total=bounds.closing_usd,
        refusal_reason=None,
        cross_checked=True,
    )
    identity = AccountIdentity(
        institution=_A2_INSTITUTION,
        account_ref=cfg.account_id,
        # [account].opening_date belongs to the separate IBKR account. Per the
        # CMA attestation contract, the no-carried-balance first statement is
        # the CMA opening evidence and is always surfaced with a CLI warning.
        opening_date=cy_statements[0].period_start,
        status=cfg.status,
        base_currency="USD",
        statement_masked_account_ref=is_masked_account_id(december.account_id),
    )
    selected = cfg.peak_balance_bound
    base_a2 = build_fa_a2_row(
        identity=identity,
        nav=nav,
        closing_ttbr_dec31=bounds.closing_ttbr,
        gross_components_inr=gross_evidence.known_components,
        period_end_cash_base_ccy=december.free_credit_balance_usd or Decimal(0),
        peak=FaA2PeakBounds(
            lower_inr=bounds.lower_inr,
            lower_date=bounds.lower_date,
            upper_inr=bounds.upper_inr,
            upper_date=bounds.upper_date,
            selected=None if selected is None else selected.value,
        ),
    )
    a2_rows = expand_fa_a2_nature_rows(base_a2, gross_evidence)

    ctx = RenderContext(assessment_year_start=_starting_ay_for_calendar_year(calendar_year))
    # If anything raises from the first write to the report, remove A2 and A3.
    run_outputs = RunOutputs((output_path, a2_path), ())
    with run_outputs.removed_on_failure():
        write_output_bundle(
            [
                spec_for(Subcommand.FA_A2, format_id, a2_rows, a2_path),
                spec_for(Subcommand.FA, format_id, a3_rows, output_path),
            ],
            ctx,
        )

        a2_gaps = fa_a2_manual_fill_gaps(a2_rows)
        manual: list[str] = []
        if a2_gaps.peak:
            manual.append(A2_PEAK_MANUAL_FILL)
        if a2_gaps.gross:
            manual.append(_A2_GROSS_MANUAL_FILL)
        # Not fa_a3_manual_fill_gaps: it flags the peak of a run with no rows,
        # and a cash-only CMA year has none.
        peak_blank_rows = fa_a3_peak_blank_rows(a3_rows)
        if peak_blank_rows:
            manual.append(A3_PEAK_MANUAL_FILL)
        return FidelityCmaFaRunReport(
            output_path=output_path,
            a2_output_path=a2_path,
            calendar_year=calendar_year,
            format_id=format_id,
            a3_rows=len(a3_rows),
            peak_bounds=bounds,
            selected_peak_bound=selected,
            manual_fill_columns=tuple(manual),
            a2_rows=a2_rows,
            run_outputs=run_outputs,
            a3_peak_blank_rows=peak_blank_rows,
        )
