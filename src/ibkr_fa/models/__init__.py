"""Domain models for ibkr-fa.

Models are frozen dataclasses that check their invariants in
``__post_init__``, so a constructed model is trustworthy. Source-format
parsing lives in :mod:`ibkr_fa.parser`.
"""

from __future__ import annotations

from ibkr_fa.models.account_identity import AccountIdentity, InstitutionIdentity, TaxpayerStatus
from ibkr_fa.models.account_info import AccountInfo
from ibkr_fa.models.closed_lot import BasisProvenance, ClosedLot, StatutoryInrCostBasis
from ibkr_fa.models.distribution_kind import (
    ALLOWED_DISTRIBUTION_KINDS,
    DistributionKind,
)
from ibkr_fa.models.dividend import Dividend
from ibkr_fa.models.execution_event import ExecutionEvent, ExecutionKind
from ibkr_fa.models.fa_a3_row import FaA3PeakBlankReason, FaA3Row
from ibkr_fa.models.form67_entry import Form67Entry, Form67SourceOfIncome
from ibkr_fa.models.form_1042s import (
    Form1042SAmount,
    Form1042SAmountKind,
    Form1042SCopy,
    Form1042SCopyType,
)
from ibkr_fa.models.instrument_identity import InstrumentIdentity
from ibkr_fa.models.interest_credit import InterestCredit, InterestNonCredit, InterestRowGroup
from ibkr_fa.models.open_lot import OpenLot, Side
from ibkr_fa.models.other_source_income import OtherSourceIncome
from ibkr_fa.models.schedule_cg_ltcg_row import (
    NEW_REGIME_CUTOVER_DATE,
    ScheduleCgLtcgLine,
)
from ibkr_fa.models.schedule_cg_row import (
    STCG_HOLDING_THRESHOLD_MONTHS,
    ScheduleCgStcgLine,
    is_short_term_holding,
)
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.models.schedule_tr_row import ScheduleTrRow, TaxReliefSection
from ibkr_fa.models.source import (
    DividendEvidence,
    FidelityCmaSourceRecord,
    NetBenefitsSourceRecord,
    SourceBundle,
    StatementPeriod,
)
from ibkr_fa.models.substitute_dividend_income import SubstituteDividendIncome
from ibkr_fa.models.tax_year import FinancialYear, starting_ay_for_calendar_year
from ibkr_fa.models.withholding_tax import WithholdingTax

__all__ = [
    "ALLOWED_DISTRIBUTION_KINDS",
    "NEW_REGIME_CUTOVER_DATE",
    "STCG_HOLDING_THRESHOLD_MONTHS",
    "AccountIdentity",
    "AccountInfo",
    "BasisProvenance",
    "ClosedLot",
    "DistributionKind",
    "Dividend",
    "DividendEvidence",
    "ExecutionEvent",
    "ExecutionKind",
    "FaA3PeakBlankReason",
    "FaA3Row",
    "FidelityCmaSourceRecord",
    "FinancialYear",
    "Form67Entry",
    "Form67SourceOfIncome",
    "Form1042SAmount",
    "Form1042SAmountKind",
    "Form1042SCopy",
    "Form1042SCopyType",
    "HeadOfIncome",
    "InstitutionIdentity",
    "InstrumentIdentity",
    "InterestCredit",
    "InterestNonCredit",
    "InterestRowGroup",
    "NetBenefitsSourceRecord",
    "OpenLot",
    "OtherSourceIncome",
    "ScheduleCgLtcgLine",
    "ScheduleCgStcgLine",
    "ScheduleFsiRow",
    "ScheduleTrRow",
    "Side",
    "SourceBundle",
    "StatementPeriod",
    "StatutoryInrCostBasis",
    "SubstituteDividendIncome",
    "TaxReliefSection",
    "TaxpayerStatus",
    "WithholdingTax",
    "is_short_term_holding",
    "starting_ay_for_calendar_year",
]
