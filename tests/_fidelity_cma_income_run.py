"""One synthetic Fidelity CMA income run, shared by the CMA income pipeline tests
and the Form 67 broker runs.

``CMA_INCOME_CONFIG`` is the run's config, with the sweep fund and a synthetic
US TIN, and ``CMA_FINALIZED_2025_TAX_CONFIG`` finalizes US tax year 2025.
``patch_cma_income_sources`` patches the income and capital gains pipelines'
statement loaders to use the given statements or ``cma_income_bundle()``
(April 2025 to April 2026), the income pipeline's Form 1042-S reader to return
an empty ``packet()``, and its reconciliation, which is skipped or, with
``check_1042s_coverage``, run for real and required to refuse that packet's
records. ``write_cma_tax_forms`` writes the
placeholder packet PDF that reader stands in for, and ``run_cma_income_cli``
runs ``fidelity-cma-income`` through the CLI and returns its exit code.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import ibkr_fa.pipeline_fidelity_cma_cg as cg_mod
import ibkr_fa.pipeline_fidelity_cma_income as income_mod
from ibkr_fa.cli import main
from ibkr_fa.normalize.fidelity_cma.form_1042s import (
    Cma1042SReconciliationError,
    reconcile_1042s,
)
from ibkr_fa.parser.fidelity_cma import (
    CmaIncomeCredit,
    CmaIncomeKind,
    CmaStatement,
    CmaWithholding,
    CmaYearEndIncomeSummary,
    to_source_record,
    validate_statement_bundle,
)
from tests._fidelity_cma import (
    CREDIT_INTEREST,
    DIVIDEND_WITHHOLDING,
    SPAXX_DIVIDEND,
    cma_config,
    income_year_end,
    monthly_statement,
)
from tests._fidelity_cma_1042s import packet

CMA_INCOME_CONFIG = cma_config(spaxx_isin="US31617H1023", us_tin="123456789")

CMA_FINALIZED_2025_TAX_CONFIG = """

[[fidelity_cma.us_tax_years]]
tax_year = 2025
refund_status = "finalized"
provenance = "final IRS transcript and CA event allocation"
eligible_tax = [{ payment_date = 2025-04-15, amount_usd = "0.10" }]
"""


def cma_income_statement(year: int, month: int) -> CmaStatement:
    income = ()
    withholding = ()
    if (year, month) == (2025, 4):
        income = (SPAXX_DIVIDEND,)
        withholding = (DIVIDEND_WITHHOLDING,)
    elif (year, month) == (2025, 5):
        income = (CREDIT_INTEREST,)
    return replace(monthly_statement(year, month), income_credits=income, withholdings=withholding)


def cma_income_bundle(*, second_year_withholding: bool = False) -> tuple[CmaStatement, ...]:
    statements = tuple(
        cma_income_statement(year, month)
        for year, month in (
            *((2025, month) for month in range(4, 13)),
            *((2026, month) for month in range(1, 5)),
        )
    )
    if not second_year_withholding:
        return statements
    january = statements[9]
    return (
        *statements[:9],
        replace(
            january,
            income_credits=(
                CmaIncomeCredit(
                    date(2026, 1, 15),
                    CmaIncomeKind.SPAXX_DIVIDEND,
                    Decimal("0.20"),
                    "99971W106",
                ),
            ),
            withholdings=(CmaWithholding(date(2026, 1, 15), Decimal("-0.05")),),
        ),
        *statements[10:],
    )


_NO_RECORDS = (
    "Fidelity CMA Form 1042-S reconciliation refused: "
    "unexpected income-code/withholding-rate records"
)


def _covered_1042s(*args: Any, **kwargs: Any) -> None:
    """Run the real Form 1042-S reconciliation. ``packet()`` has no records,
    so a run past every coverage check must refuse its records next; require
    that refusal, and fail the test if the reconciliation returns instead."""
    try:
        reconcile_1042s(*args, **kwargs)
    except Cma1042SReconciliationError as refusal:
        if str(refusal) != _NO_RECORDS:
            raise
    else:
        pytest.fail("expected the empty-packet record refusal")


def patch_cma_income_sources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    statements: tuple[CmaStatement, ...] | None = None,
    year_ends: dict[int, CmaYearEndIncomeSummary] | None = None,
    check_1042s_coverage: bool = False,
) -> None:
    bundle = validate_statement_bundle(
        statements if statements is not None else cma_income_bundle()
    )
    summaries = year_ends if year_ends is not None else {2025: income_year_end()}

    def load(*_args: object, spaxx_isin: str, **_kwargs: object) -> object:
        return bundle, summaries, to_source_record(bundle, spaxx_isin=spaxx_isin)

    # The CG pipeline reads the same statements, so a test can compare the two.
    monkeypatch.setattr(income_mod, "load_fidelity_cma_statements", load)
    monkeypatch.setattr(cg_mod, "load_fidelity_cma_statements", load)
    # The real discovery finds write_cma_tax_forms()'s placeholder and reads it here.
    monkeypatch.setattr(income_mod, "read_1042s_packet", lambda _path: packet())
    monkeypatch.setattr(
        income_mod,
        "reconcile_1042s",
        _covered_1042s if check_1042s_coverage else lambda *_args, **_kwargs: None,
    )


def write_cma_tax_forms(root: Path) -> None:
    """Give ``root``'s tax-form folder the one placeholder packet PDF that
    ``patch_cma_income_sources`` reads as ``packet()``."""
    tax_forms = root / "tax-forms"
    tax_forms.mkdir(exist_ok=True)
    (tax_forms / "2025-1042-s.pdf").write_bytes(b"%PDF placeholder")


def run_cma_income_cli(tmp_path: Path, config_text: str, *extra: str) -> int:
    """Run the ``fidelity-cma-income`` CLI on a fresh output directory."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(config_text, encoding="utf-8")
    write_cma_tax_forms(tmp_path)
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return main(
        [
            "fidelity-cma-income",
            "--itr-form",
            "ITR-2",
            "--input",
            str(tmp_path / "statements"),
            "--tax-forms",
            str(tmp_path / "tax-forms"),
            "--config",
            str(cfg),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
            *extra,
        ]
    )
