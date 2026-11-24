"""Synthetic Schedule FA A3 rows and the A3 bulk-upload CSV writer, shared by
the ``FaA3Row`` model tests, the review CSV tests and the ``InvariantError``
no-echo tests.

- ``make_fa_a3_row``: an ``FaA3Row`` whose defaults are one lot acquired on
  18 March 2026 with a closing value and no disposal date; keyword arguments
  override any field. Unless they set ``peak_blank_reason``, it is
  ``PRICES_NOT_FETCHED`` when the peak value is ``None`` and ``None``
  otherwise.
- ``make_fa_a3_upload_row``: an ``FaA3Row`` built from keyword arguments whose
  defaults are one lot acquired on 15 June 2024 with a closing value and no
  disposal date; its amounts, TT buying rates, country name and address differ
  from ``make_fa_a3_row``'s. Its ``peak_blank_reason`` is
  ``PRICES_NOT_FETCHED`` when ``peak`` is ``None`` and ``None`` otherwise.
- ``fa_filing_csv_writer``: the Schedule FA ``filing-csv`` writer, which writes
  the A3 bulk-upload CSV, for assessment year 2026 and an ITR form (``None``
  for the writer's default).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from ibkr_fa.models import FaA3PeakBlankReason, FaA3Row
from ibkr_fa.output import Subcommand, output_binding
from ibkr_fa.output.base import RenderContext
from ibkr_fa.output.filing_csv import PortalCsvWriter
from ibkr_fa.types import CountryCode, Isin, Ticker


def make_fa_a3_row(**overrides: object) -> FaA3Row:
    """Factory with sensible defaults; overrides via kwargs.

    Defaults represent a typical v1 row: a lot still open on Dec 31
    with initial_value_inr and closing_value_inr populated, and the
    three not-yet-implemented portal fields set to None.
    """
    defaults: dict[str, object] = {
        "country_name": "United States",
        "country_code": "2",
        "name_of_entity": "SAMPLE ISSUER AW INC",
        "address_of_entity": "USA",
        "zip_code": "",
        "nature_of_entity": "Listed Company",
        "date_of_acquiring": date(2026, 3, 18),
        # 386.45 * 84.30 = 32577.735 -> 32578
        "initial_value_inr": Decimal("32578"),
        "peak_value_inr": None,
        # 15 * 32.70 = 490.50; 490.50 * 85.10 = 41741.55 -> 41742
        "closing_value_inr": Decimal("41742"),
        "gross_amount_paid_credited_inr": None,
        "gross_proceeds_from_sale_inr": None,
        "source_isin": Isin("US99990E1038"),
        "source_ticker": Ticker("XQAW"),
        "source_country_iso2": CountryCode("US"),
        "audit_cost_basis_usd": Decimal("386.45"),
        "audit_acquisition_date": date(2026, 3, 18),
        "audit_close_price_usd": Decimal("32.70"),
        "audit_ttbr_acquisition": Decimal("84.30"),
        "audit_ttbr_dec31": Decimal("85.10"),
        "audit_disposal_date": None,
    }
    defaults.update(overrides)
    if "peak_blank_reason" not in overrides:
        defaults["peak_blank_reason"] = (
            FaA3PeakBlankReason.PRICES_NOT_FETCHED if defaults["peak_value_inr"] is None else None
        )
    return FaA3Row(**defaults)  # ty: ignore[invalid-argument-type]


def make_fa_a3_upload_row(
    *,
    country_code: str = "2",
    country_name: str = "United States of America",
    iso2: str = "US",
    name_of_entity: str = "SAMPLE ISSUER AW INC",
    address: str = "United States of America",
    zip_code: str = "",
    nature: str = "Listed Company",
    date_acq: date = date(2024, 6, 15),
    initial: Decimal = Decimal("32268.575"),  # 386.45 * 83.50
    peak: Decimal | None = None,
    closing: Decimal = Decimal("41692.50"),  # 15 * 32.70 * 85.00
    gross_paid: Decimal | None = None,
    gross_proceeds: Decimal | None = None,
    isin: str = "US99990E1038",
    ticker: str = "XQAW",
    cost_usd: Decimal = Decimal("386.45"),
    close_px_usd: Decimal | None = Decimal("32.70"),
    ttbr_acq: Decimal = Decimal("83.50"),
    ttbr_dec31: Decimal | None = Decimal("85.00"),
    disposal: date | None = None,
) -> FaA3Row:
    return FaA3Row(
        country_name=country_name,
        country_code=country_code,
        name_of_entity=name_of_entity,
        address_of_entity=address,
        zip_code=zip_code,
        nature_of_entity=nature,
        date_of_acquiring=date_acq,
        initial_value_inr=initial,
        peak_value_inr=peak,
        closing_value_inr=closing,
        gross_amount_paid_credited_inr=gross_paid,
        gross_proceeds_from_sale_inr=gross_proceeds,
        source_isin=Isin(isin),
        source_ticker=Ticker(ticker),
        source_country_iso2=CountryCode(iso2),
        audit_cost_basis_usd=cost_usd,
        audit_acquisition_date=date_acq,
        audit_close_price_usd=close_px_usd,
        audit_ttbr_acquisition=ttbr_acq,
        audit_ttbr_dec31=ttbr_dec31,
        audit_disposal_date=disposal,
        peak_blank_reason=FaA3PeakBlankReason.PRICES_NOT_FETCHED if peak is None else None,
    )


def fa_filing_csv_writer(itr_form: Any = None) -> PortalCsvWriter:
    writer = output_binding(Subcommand.FA, "filing-csv").writer(RenderContext(2026, itr_form))
    assert isinstance(writer, PortalCsvWriter)
    return writer
