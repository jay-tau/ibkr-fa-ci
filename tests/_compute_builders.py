"""Synthetic compute inputs shared by the ESPP, Schedule CG and Schedule OS
interest tests and the FX resolution contract test (#1882).

- ``espp_config``: a NetBenefits config, loaded from a ``user.toml`` it writes
  to a temporary directory and then removes.
- ``espp_purchase`` and ``espp_closed_lot``: an ESPP purchase confirmation and
  the closed lot sold from it.
- ``stcg_closed_lot`` and ``ltcg_closed_lot``: an in-FY closed lot held short
  of, and over, the 24-month boundary.
- ``make_interest_credit``: a USD credit interest posting.

Each builder but ``espp_config`` takes field overrides as keyword arguments.
"""

from __future__ import annotations

import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from ibkr_fa.config import NetBenefitsConfig, load_user_config
from ibkr_fa.models import BasisProvenance, ClosedLot, InterestCredit
from ibkr_fa.parser.netbenefits.confirmation_pdf import EspPurchase
from ibkr_fa.parser.netbenefits.lots_csv import EspClosedLot
from ibkr_fa.types import CountryCode, Isin, Ticker

_ESPP_CONFIG_TOML = """\
[account]
number = "U1234567"
holder_name = "Sample Holder"
opening_date = 2024-02-01
status = "BeneficialOwner"

[account.foreign_tins]
US = "123456789"

[netbenefits]
espp_discount_pct = "10"
participant_no = "I99999999"

[[netbenefits.cg_fmv_values]]
purchase_date = 2020-01-01
section17_gross_fmv_total_inr = "24000"
source = "Employer perquisite worksheet"

[[netbenefits.cg_fmv_values]]
purchase_date = 2023-06-01
section17_gross_fmv_total_inr = "24000"
source = "Employer perquisite worksheet"

[[netbenefits.cg_fmv_values]]
purchase_date = 2026-01-15
section17_gross_fmv_total_inr = "24000"
source = "Employer perquisite worksheet"

[netbenefits.account]
institution_name = "Fidelity Stock Plan Services LLC"
address = "245 Summer Street, Boston, MA"
zip = "02210"
country_iso2 = "US"
account_opening_date = 2025-11-01
status = "BeneficialOwner"

[netbenefits.security]
symbol = "XESP"
cusip = "99999E104"
isin = "US99999E1047"
entity_address = "1 Sample Issuer Street, Sample City, XX"
entity_zip = "00000"
listing_exchange = "NASDAQ"
issuer_country_code = "US"
nature_of_entity = "Listed company shares"
"""


def espp_config() -> NetBenefitsConfig:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "user.toml"
        path.write_text(_ESPP_CONFIG_TOML, encoding="utf-8")
        netbenefits = load_user_config(path).netbenefits
    assert netbenefits is not None
    return netbenefits


def espp_purchase(**overrides: object) -> EspPurchase:
    # Discount-consistent: price = FMV/share x (1 - 10%); price x shares ==
    # contribution; fmv_total > contribution (FMV != cost paid). fmv/share = 150,
    # price = 135, contribution = 270, perquisite = 30.
    defaults: dict[str, object] = {
        "purchase_date": date(2026, 1, 15),
        "settlement_date": date(2026, 1, 15),
        "offering_period_start": date(2025, 11, 3),
        "offering_period_end": date(2026, 1, 15),
        "symbol": "XESP",
        "security_description": "EXAMPLE CORP",
        "cusip": "99999E104",
        "participant_no": "I99999999",
        "shares": Decimal("2"),
        "purchase_price_per_share_usd": Decimal("135"),
        "fmv_total_usd": Decimal("300"),  # fmv/share = 150; 150 x 0.9 = 135
        "contribution_usd": Decimal("270"),
        "perquisite_usd": Decimal("30"),
    }
    defaults.update(overrides)
    return EspPurchase(**defaults)  # type: ignore[arg-type]


def espp_closed_lot(**overrides: object) -> EspClosedLot:
    defaults: dict[str, object] = {
        "acquire_date": date(2026, 1, 15),
        "sold_or_transferred_date": date(2026, 6, 15),
        "quantity": Decimal("2"),
        "proceeds_usd": Decimal("400"),
        "contribution_usd": Decimal("270"),
        "fidelity_term": "Short",
    }
    defaults.update(overrides)
    return EspClosedLot(**defaults)  # type: ignore[arg-type]


def stcg_closed_lot(**overrides: object) -> ClosedLot:
    defaults: dict[str, object] = {
        "currency_primary": "USD",
        "sub_category": "COMMON",
        "symbol": Ticker("XQAQ"),
        "description": "SAMPLE ISSUER AQ INC",
        "isin": Isin("US99992A1097"),
        "listing_exchange": "NASDAQ",
        "issuer_country_code": CountryCode("US"),
        "quantity": Decimal("10"),
        "cost_basis_money": Decimal("1500"),
        "proceeds_money": Decimal("1800"),
        "open_date_time": date(2025, 6, 15),
        "close_date_time": date(2025, 12, 10),
        "basis_provenance": BasisProvenance.ENGINE_FIFO,
    }
    defaults.update(overrides)
    return ClosedLot(**defaults)  # ty: ignore[invalid-argument-type]


def ltcg_closed_lot(**overrides: object) -> ClosedLot:
    defaults: dict[str, object] = {
        "currency_primary": "USD",
        "sub_category": "COMMON",
        "symbol": Ticker("XQAQ"),
        "description": "SAMPLE ISSUER AQ INC",
        "isin": Isin("US99992A1097"),
        "listing_exchange": "NASDAQ",
        "issuer_country_code": CountryCode("US"),
        "quantity": Decimal("10"),
        "cost_basis_money": Decimal("1500"),
        "proceeds_money": Decimal("2200"),
        # Default LTCG-eligible disposal: held ~2 years (just over the
        # 24-month boundary) and disposed in FY 2025-26 (post-cutover).
        "open_date_time": date(2023, 4, 1),
        "close_date_time": date(2025, 12, 10),
        "basis_provenance": BasisProvenance.ENGINE_FIFO,
    }
    defaults.update(overrides)
    return ClosedLot(**defaults)  # ty: ignore[invalid-argument-type]


def make_interest_credit(**overrides: object) -> InterestCredit:
    defaults: dict[str, object] = {
        "credit_date": date(2025, 5, 5),  # IBKR-typical: posted in May for Apr accrual
        "amount_gross": Decimal("1.23"),
        "currency": "USD",
        "description": "USD Credit Interest for Apr-2025",
        "source_row_index": 0,
    }
    defaults.update(overrides)
    return InterestCredit(**defaults)  # type: ignore[arg-type]
