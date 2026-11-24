"""A Robinhood Schedule FA result for the CBDT JSON writer, and the write
that publishes it, shared by the Robinhood outputs and pipeline tests, the
source fragment cleanup tests and the Robinhood pipeline stubs (#1845).

:func:`robinhood_json_result` builds, by default, a result whose two A2 rows
and one A3 row are complete and valid; :func:`schema_invalid_json_result` gives its A3 row a
ZIP the CBDT schema refuses. :func:`write_fa_json` writes a result in CBDT JSON
format inside the claim :func:`fa_json_claim` returns, as the runs do.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from ibkr_fa import pipeline_robinhood_outputs as outputs
from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.models.fa_a2_row import FaA2NatureOfAmount, FaA2Row, FaA2Status
from ibkr_fa.models.fa_a3_row import FaA3Row
from ibkr_fa.output._cleanup import ClaimedOutputCleanup
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._cbdt_fragment_inputs import unchecked_row
from tests._robinhood_cli import FINANCIAL_YEAR

ITR_FORM: ItrForm = "ITR-3"


def fa_json_claim(output_dir: Path) -> ClaimedOutputCleanup:
    """The claim on the JSON path that a run passes ``write_fa``."""
    return ClaimedOutputCleanup(output_dir / outputs.JSON_FILENAME)


def _robinhood_a2_row(account: str, *, peak_inr: str, peak_usd: str) -> FaA2Row:
    return FaA2Row(
        country_code="2",
        country_name="UNITED STATES OF AMERICA",
        institution_name="Robinhood Securities, LLC",
        institution_address="500 Colonial Center Parkway, Lake Mary, FL",
        institution_zip="32746",
        account_number=account,
        status=FaA2Status.BENEFICIAL_OWNER,
        account_opening_date=date(2025, 1, 5),
        peak_balance_inr=Decimal(peak_inr),
        closing_balance_inr=Decimal("800.40"),
        nature_of_amount=FaA2NatureOfAmount.NO_AMOUNT,
        gross_amount_inr=Decimal("0"),
        base_currency="USD",
        audit_closing_balance_base_ccy=Decimal("10.005"),
        audit_ttbr_dec31=Decimal("80"),
        audit_period_end_cash_base_ccy=Decimal("10.005"),
        audit_peak_balance_lower_inr=Decimal(peak_inr),
        audit_peak_balance_upper_inr=Decimal(peak_inr),
        audit_peak_balance_lower_date=date(2025, 3, 14),
        audit_peak_balance_upper_date=date(2025, 3, 14),
        audit_peak_balance_bound="lower",
        audit_peak_balance_base_ccy=Decimal(peak_usd),
        audit_peak_ttbr=Decimal("86.5"),
        audit_statement_peak_floor_base_ccy=Decimal("10.005"),
        audit_statement_peak_floor_date=date(2025, 12, 31),
    )


def robinhood_json_result(
    *, second_account_verified: bool = True, eligible: bool = True, **a3_changes: Any
) -> Any:
    """A Robinhood FA result whose A2/A3 rows are complete and valid, apart from
    ``a3_changes`` to its A3 row."""
    a3 = FaA3Row(
        country_name="United States of America",
        country_code="2",
        name_of_entity="SAMPLE ISSUER CA CORP",
        address_of_entity="1 Sample Way, Sampletown, WA",
        zip_code="99990",
        nature_of_entity="Listed company shares",
        date_of_acquiring=date(2025, 3, 14),
        initial_value_inr=Decimal("700.10"),
        peak_value_inr=Decimal("820.50"),
        closing_value_inr=Decimal("800.40"),
        gross_amount_paid_credited_inr=Decimal("0"),
        gross_proceeds_from_sale_inr=Decimal("0"),
        source_isin=Isin("US99972B1017"),
        source_ticker=Ticker("XQCA"),
        source_country_iso2=CountryCode("US"),
        audit_cost_basis_usd=Decimal("8.75"),
        audit_acquisition_date=date(2025, 3, 14),
        audit_close_price_usd=Decimal("10.005"),
        audit_ttbr_acquisition=Decimal("80.01"),
        audit_ttbr_dec31=Decimal("80"),
        audit_disposal_date=None,
        peak_blank_reason=None,
    )
    return cast(
        Any,
        SimpleNamespace(
            a2=(
                SimpleNamespace(
                    row=_robinhood_a2_row("111111111", peak_inr="12344.50", peak_usd="142.71"),
                    account_id_cross_checked=True,
                ),
                SimpleNamespace(
                    row=_robinhood_a2_row("222222222", peak_inr="400.5", peak_usd="4.63"),
                    account_id_cross_checked=second_account_verified,
                ),
            ),
            a3_rows=(replace(a3, **a3_changes),),
            cbdt_json_eligible=eligible,
        ),
    )


def schema_invalid_json_result(**kwargs: Any) -> Any:
    """:func:`robinhood_json_result` with an A3 ZIP the CBDT schema refuses,
    set past the row's own length check, which would refuse it first (#1189)."""
    result = robinhood_json_result(**kwargs)
    result.a3_rows = tuple(unchecked_row(row, zip_code="123456789") for row in result.a3_rows)
    return result


def write_fa_json(
    tmp_path: Path,
    result: Any | None = None,
    *,
    financial_year: FinancialYear = FINANCIAL_YEAR,
    json_available: bool = True,
) -> tuple[tuple[Path, ...], bool, tuple[str, ...]]:
    # As the runs do, arm the claim that removes the fragment if the write fails.
    with fa_json_claim(tmp_path) as json_cleanup:
        return outputs.write_fa(
            robinhood_json_result() if result is None else result,
            output_dir=tmp_path,
            format_id="cbdt-json-fragment",
            financial_year=financial_year,
            itr_form=ITR_FORM,
            json_available=json_available,
            json_cleanup=json_cleanup,
        )
