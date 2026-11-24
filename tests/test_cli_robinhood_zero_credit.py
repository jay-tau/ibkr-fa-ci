"""Robinhood's income commands refuse a Form 67 credit that files as ₹0
before writing schedule outputs (#601, #1491).

#1084's relief run, with the real income compute and everything else
stubbed: a US dividend of USD 100.00 with USD 25.00 withheld and its 1042-S.
An exact Indian tax payable of ₹0 makes the Form 67 credit and Schedule FSI
relief exactly ₹0, leaving Schedule TR relief and section blank; ₹0.4 makes
the credit and FSI relief positive but computes TR relief as ₹0 in whole
rupees. ``robinhood-income`` and ``robinhood-all`` aggregate Schedule TR from
that relief, then exit 4 with the #601 refusal before any schedule file is
written, so no Schedule TR relief they compute reaches an output.
``robinhood-all`` runs in one format: it stops before any writer reads the
format.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import ibkr_fa.pipeline_robinhood_core as robinhood_core
from ibkr_fa import cli
from ibkr_fa.config import Form67Config, Form67TaxPayableOverride
from ibkr_fa.models.form67_entry import Form67SourceOfIncome
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome
from tests._ftc_warnings import US_ZERO_CREDIT_REFUSAL as _REFUSAL
from tests._robinhood_cli import robinhood_argv as _common
from tests._robinhood_pipeline_stubs import stub_relief_run as _stub_relief_run


@pytest.mark.parametrize("tax_payable", ["0", "0.4"], ids=["exactly-zero", "rounds-to-zero"])
@pytest.mark.parametrize(
    "args",
    [
        ("robinhood-income",),
        ("robinhood-all", "--format", "review-csv"),
    ],
    ids=["robinhood-income", "robinhood-all-review-csv"],
)
def test_zero_credit_refuses_after_schedule_tr_and_before_any_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: tuple[str, ...],
    tax_payable: str,
) -> None:
    bundle = _stub_relief_run(tmp_path, monkeypatch, "US")
    bundle.config = replace(
        bundle.config,
        form67=Form67Config(
            (Form67TaxPayableOverride("US", Form67SourceOfIncome.DIVIDEND, Decimal(tax_payable)),)
        ),
    )
    aggregated: list[Any] = []
    real = robinhood_core.compute_schedule_tr_from_fsi

    def recording(fsi_result: Any) -> Any:
        aggregated.append(fsi_result)
        return real(fsi_result)

    monkeypatch.setattr(robinhood_core, "compute_schedule_tr_from_fsi", recording)
    out = tmp_path / "out"
    out.mkdir()
    out.chmod(0o700)

    rc = cli.main([*_common(args[0], tmp_path), *args[1:], "--no-fetch-prices"])

    captured = capsys.readouterr()
    assert rc == 4
    assert (captured.out, captured.err) == (
        "",
        f"error: ConfigError at field 'form67': {_REFUSAL}\n",
    )
    assert list(out.iterdir()) == []
    # Schedule TR was aggregated from the US relief Form 67's credit sets.
    [fsi_result] = aggregated
    [relief_row] = [
        row
        for row in fsi_result.rows
        if row.iso_alpha2 == "US" and row.head_of_income is HeadOfIncome.OTHER_SOURCES
    ]
    assert relief_row.foreign_tax_inr > 0
    assert relief_row.tax_relief_inr == Decimal(tax_payable)
