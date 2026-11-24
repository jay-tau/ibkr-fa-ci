"""One synthetic Fidelity CMA Schedule FA run, shared by the CMA Schedule FA
pipeline tests, the exact-sum CLI tests and the portal parity CLI tests.

``CMA_FA_CONFIG`` is the run's base config, with the sweep fund, a joint account
and a beneficial-owner CMA status, and ``UPPER_BOUND_CONFIG`` is the extra
config text that attests the upper A2 peak bound. ``cma_fa_year_end`` builds a
year-end income summary with one SPAXX position, and ``redemption_statement``
the base annual statement with a buy and a 4-unit redemption.
``cma_fa_price_series`` is a synthetic SPAXX NAV of 1, or the NAV ``navs``
gives, on every US market session from the one that seeds January 1, 2025.
``prepare_cma_fa`` patches the Schedule FA pipeline's statement loader to
return the given statements (the base annual statement by default) and year-end
summary, and its price loader to return a dictionary containing
``cma_fa_price_series``'s result; it writes the user config and returns that
path and the output path. ``run_cma_fa_cli`` runs
``fidelity-cma-fa`` for calendar year 2025 through the CLI and returns its exit
code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import ibkr_fa.pipeline_fidelity_cma_fa as fa_mod
from ibkr_fa.cli import main
from ibkr_fa.parser.fidelity_cma import (
    CmaCoreTransaction,
    CmaCoreTransactionKind,
    CmaStatement,
    CmaYearEndIncomeSummary,
    CmaYearEndPosition,
    to_source_record,
    validate_statement_bundle,
)
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.types import Ticker
from tests._fidelity_cma import annual_statement as _statement
from tests._fidelity_cma import cma_config, session_series, year_end_summary

CMA_FA_CONFIG = cma_config(
    spaxx_isin="US31617H1023", account_status="Joint", cma_status="BeneficialOwner"
)
UPPER_BOUND_CONFIG = '\nsuppress_peak_balance_warning = true\npeak_balance_bound = "upper"\n'


def cma_fa_year_end(*, quantity: str = "10", market_value: str = "10") -> CmaYearEndIncomeSummary:
    return year_end_summary(
        position=CmaYearEndPosition(
            quantity=Decimal(quantity),
            price_per_unit_usd=Decimal(1),
            market_value_usd=Decimal(market_value),
            income_earned_usd=Decimal(0),
        ),
    )


def redemption_statement(
    *,
    bought: date = date(2025, 1, 1),
    redeemed: date = date(2025, 4, 4),
    price: str = "1",
) -> CmaStatement:
    """The base statement with its 10-unit buy on ``bought`` and a 4-unit
    redemption at ``price`` on ``redeemed``, so the year has one closed and one
    open lot. With the defaults, under both peak currencies the closed lot's
    peak is not its disposal day: the NAV is flat, and the 2025-04-04 TTBR
    (84.8) is below the earlier 87.1."""
    base = _statement()
    (buy,) = base.core_transactions
    proceeds = Decimal(4) * Decimal(price)
    account_value = Decimal(8) + proceeds
    return replace(
        base,
        core_transactions=(
            replace(buy, date=bought),
            CmaCoreTransaction(
                redeemed,
                CmaCoreTransactionKind.SELL,
                Decimal("-4"),
                Decimal(price),
                -proceeds,
                Decimal("6"),
            ),
        ),
        ending_account_value_usd=account_value,
        account_value_usd=account_value,
        free_credit_balance_usd=Decimal(2) + proceeds,
        position=replace(
            base.position, quantity=Decimal("6"), ending_market_value_usd=Decimal("6")
        ),
    )


def cma_fa_price_series(now: datetime, navs: Mapping[date, Decimal] | None = None) -> PriceSeries:
    # Every US market session, from the one that seeds January 1.
    return session_series(
        Ticker("SPAXX"), date(2024, 12, 31), date(2025, 12, 31), fetched_at=now, overrides=navs
    )


def prepare_cma_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra_config: str = "",
    statement: CmaStatement | tuple[CmaStatement, ...] | None = None,
    year_end: CmaYearEndIncomeSummary | None = None,
    navs: Mapping[date, Decimal] | None = None,
) -> tuple[Path, Path]:
    statements = statement if isinstance(statement, tuple) else (statement or _statement(),)
    year_end = year_end or cma_fa_year_end()

    def load_statements(*_args: object, spaxx_isin: str, **_kwargs: object) -> object:
        bundle = validate_statement_bundle(statements)
        return (
            bundle,
            {year_end.tax_year: year_end},
            to_source_record(bundle, spaxx_isin=spaxx_isin),
        )

    monkeypatch.setattr(fa_mod, "load_fidelity_cma_statements", load_statements)

    def fetch(
        _ticker: Ticker,
        *,
        start: date,
        end: date,
        now: datetime,
        fetch_symbol: str | None = None,
    ) -> PriceSeries:
        del start, end, fetch_symbol
        return cma_fa_price_series(now, navs)

    def load_prices(*_args: object, **kwargs: object) -> dict[Ticker, PriceSeries]:
        cache_dir = kwargs["cache_dir"]
        assert isinstance(cache_dir, Path)
        assert cache_dir.name == "nasdaq-mutual-funds"
        now = kwargs.get("now")
        assert now is None or isinstance(now, datetime)
        return {
            Ticker("SPAXX"): fetch(
                Ticker("SPAXX"),
                start=date(2025, 1, 1),
                end=date(2025, 12, 31),
                now=now or datetime(2026, 1, 1, tzinfo=UTC),
            )
        }

    monkeypatch.setattr(fa_mod, "load_price_series", load_prices)
    config = tmp_path / "user.toml"
    config.write_text(CMA_FA_CONFIG + extra_config, encoding="utf-8")
    output = tmp_path / "schedule-fa.csv"
    return config, output


def run_cma_fa_cli(input_dir: Path, config: Path, output: Path, *extra: str) -> int:
    return main(
        [
            "fidelity-cma-fa",
            "--input",
            str(input_dir),
            "--config",
            str(config),
            "--output",
            str(output),
            "--calendar-year",
            "2025",
            *extra,
        ]
    )
