"""Load mandatory Massive closes for Robinhood daily A2 valuation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compute.robinhood_a2_peak import (
    RobinhoodA2PeakError,
    robinhood_required_option_sessions,
)
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionContract,
    RobinhoodOptionReconstruction,
)
from ibkr_fa.config import UserConfig
from ibkr_fa.models import FaA3PeakBlankReason
from ibkr_fa.output.fa_manual_fill import FA_A3_PEAK_BLANK_TEXT
from ibkr_fa.prices.cache import DEFAULT_PRICE_CACHE_DIR_NAME
from ibkr_fa.prices.massive_options import (
    MassiveOptionPriceError,
    load_or_fetch_massive_option_closes,
)
from ibkr_fa.prices.series import MassiveOptionCloseSeries

# What to do when a held contract's closes are missing or short (#1523). A
# cached Massive series is read before any fetch, so a re-run with fetching on
# fills a missing series, and a short one only once its file is moved aside
# (#1528), or every contract's file when the filer can't tell which (#1530).
# The Massive reader keeps no lock file.
_KEY_NEEDED = "a Massive fetch needs [prices] massive_api_key_file in user.toml"
_NOT_CACHED = (
    "complete cached Massive option closes are required: "
    f"{FA_A3_PEAK_BLANK_TEXT[FaA3PeakBlankReason.PRICES_NOT_FETCHED][0]}. Re-run without "
    f"--no-fetch-prices ({_KEY_NEEDED}), "
    "or point --price-cache-dir at a cache directory that holds those closes."
)
_NO_KEY = (
    "Massive API key file is not configured and no complete price cache was found: "
    f"complete cached Massive option closes are required, and {_KEY_NEEDED}. Set it to a "
    "file containing MASSIVE_API_KEY=... and re-run, or point --price-cache-dir at a cache "
    "directory that holds those closes."
)
_SHORT = (
    "A held contract's Massive series has no close for a US market session it was held "
    "(Massive has no daily bar for a day the contract didn't trade), and a cached Massive "
    "series is reused before any fetch, so a re-run alone returns the same closes. Move "
    "each such contract's cached file out of the massive-options folder of the price cache "
    f"directory (--price-cache-dir) and re-run with price fetching ({_KEY_NEEDED}): a fresh "
    "fetch can return closes a series cached earlier lacks. If you can't tell which contract "
    "is short, move every .json file in that folder out of it instead and re-run the same "
    "way. If the fresh series is still short, enter the Robinhood Schedule FA by hand: "
    "closes are never guessed."
)


@dataclass(frozen=True, slots=True)
class RobinhoodOptionPriceAudit:
    series_by_contract: Mapping[
        RobinhoodOptionContract,
        MassiveOptionCloseSeries,
    ] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def massive_api_key_path(config: UserConfig, config_path: Path) -> Path | None:
    configured = config.prices.massive_api_key_file
    if configured is None:
        return None
    path = Path(configured)
    return path if path.is_absolute() else config_path.parent / path


def _result(
    *,
    series: Mapping[RobinhoodOptionContract, MassiveOptionCloseSeries] | None = None,
    warning: str | None = None,
    error: str | None = None,
) -> RobinhoodOptionPriceAudit:
    return RobinhoodOptionPriceAudit(
        series_by_contract=series or {},
        warnings=((f"Massive option close audit: {warning}",) if warning else ()),
        errors=((error,) if error else ()),
    )


def collect_robinhood_option_price_audit(  # noqa: PLR0912
    *,
    options: RobinhoodOptionReconstruction,
    config: UserConfig,
    config_path: Path,
    input_dir: Path,
    calendar_year: int,
    fetch: bool,
    price_cache_dir: Path | None,
) -> RobinhoodOptionPriceAudit:
    if not options.trades and not options.valuation_anchors:
        return RobinhoodOptionPriceAudit()
    try:
        required = robinhood_required_option_sessions(options, calendar_year=calendar_year)
    except RobinhoodA2PeakError as exc:
        return _result(error=str(exc))
    year_start = date(calendar_year, 1, 1)
    year_end = date(calendar_year, 12, 31)

    cache_dir = (price_cache_dir or (input_dir / DEFAULT_PRICE_CACHE_DIR_NAME)) / "massive-options"
    key_path = massive_api_key_path(config, config_path)
    # Without a key file a Massive fetch can't run: read cached closes only,
    # and name the setting when a contract has none (#1528).
    read_fetch = fetch and key_path is not None
    total_sessions = covered_sessions = anchor_pairs = 0
    delta_buckets = [0, 0, 0, 0]
    series_by_contract: dict[RobinhoodOptionContract, MassiveOptionCloseSeries] = {}
    try:
        for contract, sessions in sorted(required.items()):
            start = min(year_start, *sessions)
            series = load_or_fetch_massive_option_closes(
                underlying=contract.symbol,
                expiration=contract.expiration,
                strike_usd=contract.strike_usd,
                right=contract.right,
                multiplier=contract.multiplier,
                start=start,
                end=year_end,
                cache_dir=cache_dir,
                fetch=read_fetch,
                api_key_file=key_path,
            )
            if series is None:
                # The reader gives None only when it may not fetch and has no artifact.
                return _result(series=series_by_contract, error=_NO_KEY if fetch else _NOT_CACHED)
            series_by_contract[contract] = series
            total_sessions += len(sessions)
            covered_sessions += sum(day in series.bars for day in sessions)
            if not sessions <= series.bars.keys():
                return _result(
                    series=series_by_contract,
                    error=(
                        "Massive option close coverage is incomplete "
                        f"({covered_sessions}/{total_sessions} held sessions). {_SHORT}"
                    ),
                )
    except MassiveOptionPriceError as exc:
        return _result(series=series_by_contract, error=str(exc))

    for anchor in options.validated_valuation_anchors:
        series = series_by_contract.get(anchor.contract)
        if series is None:
            continue
        session = max(
            (day for day in series.bars if day <= anchor.statement_date),
            default=None,
        )
        if session is None or anchor.market_value_usd == 0:
            continue
        anchor_pairs += 1
        provider_value = (
            series.bars[session].close_usd * anchor.quantity * anchor.contract.multiplier
        )
        delta = abs(provider_value - anchor.market_value_usd) / abs(anchor.market_value_usd)
        if delta <= Decimal("0.005"):
            delta_buckets[0] += 1
        elif delta <= Decimal("0.02"):
            delta_buckets[1] += 1
        elif delta <= Decimal("0.10"):
            delta_buckets[2] += 1
        else:
            delta_buckets[3] += 1

    return _result(
        series=series_by_contract,
        warning=(
            f"complete ({len(required)} contracts, {covered_sessions}/{total_sessions} "
            f"held sessions, {anchor_pairs} statement anchors; relative deltas "
            f"<=0.5%:{delta_buckets[0]}, <=2%:{delta_buckets[1]}, "
            f"<=10%:{delta_buckets[2]}, >10%:{delta_buckets[3]}); "
            f"private cache {cache_dir}"
        ),
    )
