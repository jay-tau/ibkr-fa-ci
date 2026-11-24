"""Schedule FA Table A2 row — the foreign **custodial account** itself.

Schedule FA Table A2 discloses the foreign brokerage / custodial account (the
IBKR account), as opposed to Table A3 which discloses each foreign equity
holding INSIDE that account (issue #18). An IBKR-only filer reports BOTH: the
account in A2 and every holding in A3.

Portal columns (official utility ``TR_FA``; one row per account/credit nature)::

    Country Name and Code | Name of institution | Address | ZIP |
    Account Number | Status | Account opening date |
    Peak Balance | Closing balance | Nature of amount | Gross amount paid/credited

There is NO opening/initial-balance column and NO account-closure-date column
(unlike A3, which has an "Initial value" column).

"Never be wrong" stance, mirroring A3:

* :attr:`peak_balance_inr` defaults to ``None`` (manual-fill). A source-specific
  pipeline may populate it only with provenance: audited lower/upper values and
  an explicit selected bound, or a user-attested value with its source and
  reason (``audit_peak_override_*``).
* :attr:`closing_balance_inr` is the broker-stated Dec-31 NAV x SBI TTBR;
  ``None`` (manual-fill) when the NAV could not be determined with
  certainty or the base currency is unsupported, with the reason in
  :attr:`closing_blank_reason` (#1013).
* Peak/closing balances may be **negative** (a margined account can carry a
  negative NAV) — they are NOT sign-constrained, unlike A3's holding values.
* Every field's type is checked first (a date is exactly a ``date``, never a
  ``datetime``; an amount is a finite ``Decimal``). Each filed INR amount
  rounds HALF_UP to a whole rupee within ±99,999,999,999,999, the CBDT
  14-digit bound, negative ones too (#788).
* Text stays within the AY 2026-27 CBDT schemas' maximum lengths: country name
  55, institution name 125, address 200, ZIP 8 and account number 34
  characters. The limits apply to the raw text, so a value that the filing CSV
  would shorten by collapsing commas and whitespace runs is refused too.
  An empty ZIP is still allowed (manual fill).
* Upload text (#1329), checked last so every other check keeps its message: a
  country name, institution name, address, ZIP or account number with a
  character other than whitespace must keep one in its bulk-upload rendering
  (:func:`~ibkr_fa.models._row_checks.upload_text`), so a value of only commas
  and spaces is refused. An empty or whitespace-only value is left to the other
  checks.
* :attr:`nature_of_amount` / :attr:`gross_amount_inr` are coupled (both set or
  both ``None``). Known natures are split into separate rows; both are
  ``None`` only for an unresolved placeholder. A ``No Amount paid/credited``
  row carries exactly zero; another nature may also carry zero.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal

from ibkr_fa.invariants import require
from ibkr_fa.models._identifier_checks import INDIA_CBDT_CODE, require_no_control_character
from ibkr_fa.models._row_checks import (
    require_amounts,
    require_bools,
    require_dates,
    require_filed_amounts,
    require_max_lengths,
    require_text,
    require_upload_text,
)
from ibkr_fa.models.account_identity import TaxpayerStatus
from ibkr_fa.models.money import optional_whole_rupees
from ibkr_fa.models.net_asset_value import NavRefusalReason
from ibkr_fa.pii import is_masked_account_id

_CBDT_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9]+$")


class FaA2Status(StrEnum):
    """Schedule FA Table A2 "Status" dropdown tokens.

    These are the EXACT strings the e-filing offline utility validates against
    (named range ``FA_A2_Status``) — including the official ``BENIFICIARY``
    typo. They are NOT the same vocabulary as
    :class:`~ibkr_fa.models.account_identity.TaxpayerStatus` (``BeneficialOwner``
    / ``Joint`` / ``SigningAuthority``); :data:`FA_A2_STATUS_BY_TAXPAYER_STATUS`
    translates between them.
    """

    OWNER = "OWNER"
    BENEFICIAL_OWNER = "BENEFICIAL_OWNER"
    BENEFICIARY = "BENIFICIARY"  # misspelled verbatim per the official utility


# SIGNING_AUTHORITY has NO Table A2 equivalent — a pure signing authority over
# an account you do not own belongs in Table A8, not A2 — so it has no entry.
FA_A2_STATUS_BY_TAXPAYER_STATUS: Final[Mapping[TaxpayerStatus, FaA2Status]] = MappingProxyType(
    {
        TaxpayerStatus.BENEFICIAL_OWNER: FaA2Status.BENEFICIAL_OWNER,
        TaxpayerStatus.JOINT: FaA2Status.OWNER,
    }
)


class FaA2NatureOfAmount(StrEnum):
    """Schedule FA Table A2 "Nature of amount" dropdown tokens (verbatim)."""

    INTEREST = "Interest"
    DIVIDEND = "Dividend"
    PROCEEDS = "Proceeds from sale or redemption of financial assets"
    NO_AMOUNT = "No Amount paid/credited"
    OTHER_INCOME = "Other income"


# The natures an account credit can have, in A2 row order.
FA_A2_CREDIT_NATURES: Final[tuple[FaA2NatureOfAmount, ...]] = (
    FaA2NatureOfAmount.INTEREST,
    FaA2NatureOfAmount.DIVIDEND,
    FaA2NatureOfAmount.PROCEEDS,
    FaA2NatureOfAmount.OTHER_INCOME,
)


class FaA2ClosingBlankReason(StrEnum):
    """Why the engine left a Table A2 closing balance blank (#1013).

    Each blank closing balance has exactly one: the NAV's
    :class:`~ibkr_fa.models.net_asset_value.NavRefusalReason` (same value),
    or :attr:`NON_USD_BASE` when the NAV parsed but the base currency isn't
    USD. The remedy differs by reason, so the renderers name it.
    """

    NO_ASSET_CLASS_BLOCK = NavRefusalReason.NO_ASSET_CLASS_BLOCK.value
    MULTIPLE_ASSET_CLASS_BLOCKS = NavRefusalReason.MULTIPLE_ASSET_CLASS_BLOCKS.value
    MULTIPLE_TOTAL_ROWS = NavRefusalReason.MULTIPLE_TOTAL_ROWS.value
    BLANK_TOTAL = NavRefusalReason.BLANK_TOTAL.value
    NO_ASSET_CLASS_ROWS = NavRefusalReason.NO_ASSET_CLASS_ROWS.value
    BLANK_ASSET_CLASS_AMOUNT = NavRefusalReason.BLANK_ASSET_CLASS_AMOUNT.value
    MULTIPLE_ENDING_VALUES = NavRefusalReason.MULTIPLE_ENDING_VALUES.value
    NO_ENDING_VALUE = NavRefusalReason.NO_ENDING_VALUE.value
    BLANK_ENDING_VALUE = NavRefusalReason.BLANK_ENDING_VALUE.value
    FLEX_QUERY = NavRefusalReason.FLEX_QUERY.value
    # The NAV parsed, but v1 converts only a USD base.
    NON_USD_BASE = "non-usd-base"


FaA2PeakBound = Literal["lower", "upper"]


@dataclass(frozen=True, slots=True)
class FaA2FiledValues:
    """What Schedule FA Table A2 files for one row: whole rupees, rounded half-up.

    Every writer and report reads these, and none of them rounds. A column the
    row leaves blank (manual fill) is ``None``.
    """

    peak_balance_inr: Decimal | None
    closing_balance_inr: Decimal | None
    gross_amount_inr: Decimal | None


@dataclass(frozen=True, slots=True)
class FaA2Row:
    """One Schedule FA Table A2 account-and-credit-nature row.

    Attributes:
        country_code: CBDT ITR-2 numeric code of the INSTITUTION's country
            (e.g. ``"2"`` for the United States — where the IBKR entity is,
            which may differ from the holdings' countries). Never India.
        country_name: Portal country name for the institution's country.
        institution_name: Custodial institution name (e.g.
            ``"Interactive Brokers LLC"``).
        institution_address: Institution address.
        institution_zip: Institution ZIP/postal code; may be empty. The CSV
            and XLSX formats then leave the cell blank, the run names it as a
            cell to fill by hand and exits 3, and
            ``--format cbdt-json-fragment`` reports it as a gap (both CBDT
            schemas require a ZIP).
        account_number: The REAL account number (never a masked literal, even
            when the statement masked it).
        status: Account ownership :class:`FaA2Status`.
        account_opening_date: Date the account was opened.
        peak_balance_inr: Peak account balance during the CY, in INR. ``None``
            when the source cannot resolve or explicitly attest a value.
        closing_balance_inr: Dec-31 closing balance in INR (broker NAV x
            Dec-31 TTBR). ``None`` when undeterminable (manual-fill). May be
            negative (margined account); NOT sign-constrained.
        closing_blank_reason: Why :attr:`closing_balance_inr` is blank, as a
            :class:`FaA2ClosingBlankReason` member; set exactly when it is
            ``None`` (#1013). Not a CSV/XLSX column: the run report and the
            CBDT JSON gap name it.
        nature_of_amount: Nature of the gross amount credited, or ``None``
            when source evidence cannot resolve a nature. Coupled with
            :attr:`gross_amount_inr` (both set or both ``None``).
        gross_amount_inr: Gross amount paid/credited to the account during the
            CY, in INR (non-negative). Foreign-source income uses the
            December 31 SBI TTBR, as do sale/redemption proceeds.
            ``Decimal("0")`` with nature
            :attr:`FaA2NatureOfAmount.NO_AMOUNT` when nothing was credited;
            that nature with any other amount is refused.
        base_currency: Statement base currency (audit; e.g. ``"USD"``).
        audit_closing_balance_base_ccy: Broker Dec-31 NAV in base currency
            before TTBR conversion (audit); ``None`` when closing manual-fill.
        audit_ttbr_dec31: SBI TTBR applied to the closing balance
            and/or gross credits (audit).
        audit_account_number_from_config: ``True`` when the statement masked
            the account number, so the filed number came from user config.
        audit_nav_cross_checked: ``True`` when the NAV total agreed with the
            Change-in-NAV ``Ending Value``.
        audit_gross_components: Per-nature gross breakdown (audit) — populated
            especially when :attr:`nature_of_amount` is ``None`` (multiple
            natures) so the user/CA can split it in the portal.
    """

    # ---- Portal-facing columns ----
    country_code: str
    country_name: str
    institution_name: str
    institution_address: str
    institution_zip: str
    account_number: str
    status: FaA2Status
    account_opening_date: date
    peak_balance_inr: Decimal | None
    closing_balance_inr: Decimal | None
    nature_of_amount: FaA2NatureOfAmount | None
    gross_amount_inr: Decimal | None

    # ---- Audit columns ----
    base_currency: str
    audit_closing_balance_base_ccy: Decimal | None = None
    audit_ttbr_dec31: Decimal | None = None
    audit_account_number_from_config: bool = False
    audit_nav_cross_checked: bool = False
    audit_gross_components: Mapping[str, Decimal] = field(default_factory=dict)
    # Period-end (Dec 31) cash component of the NAV total, in base currency —
    # a REFERENCE for the manual-fill peak (Rule 114F: the custodial-account
    # peak/closing includes cash). Uncoupled / optional: None when the NAV
    # breakdown carried no "Cash" row (e.g. an all-securities account).
    audit_period_end_cash_base_ccy: Decimal | None = None
    audit_peak_balance_lower_inr: Decimal | None = None
    audit_peak_balance_upper_inr: Decimal | None = None
    audit_peak_balance_lower_date: date | None = None
    audit_peak_balance_upper_date: date | None = None
    audit_peak_balance_bound: FaA2PeakBound | None = None
    audit_peak_balance_base_ccy: Decimal | None = None
    audit_peak_ttbr: Decimal | None = None
    audit_statement_peak_floor_base_ccy: Decimal | None = None
    audit_statement_peak_floor_date: date | None = None
    audit_peak_override_source: str | None = None
    audit_peak_override_reason: str | None = None
    closing_blank_reason: FaA2ClosingBlankReason | None = None

    @property
    def filed(self) -> FaA2FiledValues:
        """The whole-rupee amounts Schedule FA Table A2 files for this row."""
        return FaA2FiledValues(
            peak_balance_inr=optional_whole_rupees(self.peak_balance_inr),
            closing_balance_inr=optional_whole_rupees(self.closing_balance_inr),
            gross_amount_inr=optional_whole_rupees(self.gross_amount_inr),
        )

    def __post_init__(self) -> None:  # noqa: PLR0915
        # Types first, so a wrong one is refused by name before a check below
        # fails on it.
        require_text(
            self,
            "country_code",
            "country_name",
            "institution_name",
            "institution_address",
            "institution_zip",
            "account_number",
            "base_currency",
        )
        require_text(
            self,
            "audit_peak_balance_bound",
            "audit_peak_override_source",
            "audit_peak_override_reason",
            optional=True,
        )
        require_dates(self, "account_opening_date")
        require_dates(
            self,
            "audit_peak_balance_lower_date",
            "audit_peak_balance_upper_date",
            "audit_statement_peak_floor_date",
            optional=True,
        )
        # Peak/closing may be negative (margin).
        require_filed_amounts(
            self, "peak_balance_inr", "closing_balance_inr", "gross_amount_inr", optional=True
        )
        require_amounts(
            self,
            "audit_closing_balance_base_ccy",
            "audit_ttbr_dec31",
            "audit_period_end_cash_base_ccy",
            "audit_peak_balance_lower_inr",
            "audit_peak_balance_upper_inr",
            "audit_peak_balance_base_ccy",
            "audit_peak_ttbr",
            "audit_statement_peak_floor_base_ccy",
            optional=True,
        )
        require_bools(self, "audit_account_number_from_config", "audit_nav_cross_checked")
        require(
            isinstance(self.status, FaA2Status),
            what="FaA2Row.status must be a FaA2Status",
            type=type(self.status).__name__,
        )
        require(
            self.nature_of_amount is None or isinstance(self.nature_of_amount, FaA2NatureOfAmount),
            what="FaA2Row.nature_of_amount must be a FaA2NatureOfAmount or None",
            type=type(self.nature_of_amount).__name__,
        )
        require(
            self.closing_blank_reason is None
            or isinstance(self.closing_blank_reason, FaA2ClosingBlankReason),
            what=(
                "FaA2Row.closing_blank_reason must be None or a FaA2ClosingBlankReason enum member"
            ),
            closing_blank_reason=self.closing_blank_reason,
        )
        require(
            (self.closing_balance_inr is None) == (self.closing_blank_reason is not None),
            what=(
                "FaA2Row.closing_blank_reason must be set exactly when closing_balance_inr is blank"
            ),
            closing_balance_inr=str(self.closing_balance_inr),
            closing_blank_reason=self.closing_blank_reason,
        )
        require(
            isinstance(self.audit_gross_components, Mapping)
            and all(
                isinstance(name, str) and isinstance(amount, Decimal) and amount.is_finite()
                for name, amount in self.audit_gross_components.items()
            ),
            what="FaA2Row.audit_gross_components must be a mapping of str to finite Decimal",
        )
        require(
            _CBDT_CODE_PATTERN.fullmatch(self.country_code) is not None,
            what="FaA2Row.country_code must be a numeric CBDT ITR-2 code",
            country_code=self.country_code,
        )
        # Table A2 is foreign-source only, so an India row is invalid
        # (defence-in-depth — config validation refuses "IN" upstream).
        require(
            self.country_code != INDIA_CBDT_CODE,
            what="FaA2Row.country_code must not be India (Table A2 is foreign-source only)",
            country_code=self.country_code,
        )
        for name, value in (
            ("country_name", self.country_name),
            ("institution_name", self.institution_name),
            ("institution_address", self.institution_address),
            ("base_currency", self.base_currency),
        ):
            require(value != "", what=f"FaA2Row.{name} must be non-empty")
        require_no_control_character("FaA2Row", "account_number", self.account_number)
        require(
            self.account_number != "" and self.account_number.isascii(),
            what="FaA2Row.account_number must be a non-empty ASCII string",
            account_number=self.account_number,
        )
        require(
            not is_masked_account_id(self.account_number),
            what=(
                "FaA2Row.account_number must be the real account number, never a "
                "masked literal (Table A2's account number is a filed value)"
            ),
            account_number=self.account_number,
        )
        require_max_lengths(
            self,
            country_name=55,
            institution_name=125,
            institution_address=200,
            institution_zip=8,
            account_number=34,
        )
        bounds_present = (
            self.audit_peak_balance_lower_inr is not None
            and self.audit_peak_balance_upper_inr is not None
            and self.audit_peak_balance_lower_date is not None
            and self.audit_peak_balance_upper_date is not None
        )
        bounds_absent = (
            self.audit_peak_balance_lower_inr is None
            and self.audit_peak_balance_upper_inr is None
            and self.audit_peak_balance_lower_date is None
            and self.audit_peak_balance_upper_date is None
        )
        override_present = (
            self.audit_peak_override_source is not None
            and self.audit_peak_override_reason is not None
        )
        override_absent = (
            self.audit_peak_override_source is None and self.audit_peak_override_reason is None
        )
        require(
            bounds_present or bounds_absent,
            what="FaA2Row peak-bound amounts and dates must all be set or all be None",
        )
        require(
            (self.audit_peak_balance_base_ccy is None) == (self.audit_peak_ttbr is None),
            what=(
                "FaA2Row audit_peak_balance_base_ccy and audit_peak_ttbr "
                "must both be set or both be None"
            ),
        )
        if self.audit_peak_ttbr is not None:
            require(
                self.audit_peak_ttbr > 0,
                what="FaA2Row.audit_peak_ttbr must be positive",
            )
        require(
            (self.audit_statement_peak_floor_base_ccy is None)
            == (self.audit_statement_peak_floor_date is None),
            what=("FaA2Row statement peak floor amount and date must both be set or both be None"),
        )
        require(
            override_present or override_absent,
            what="FaA2Row peak override source and reason must both be set or both be None",
        )
        if override_present:
            assert self.audit_peak_override_source is not None
            assert self.audit_peak_override_reason is not None
            require(
                self.audit_peak_override_source.strip() != ""
                and self.audit_peak_override_reason.strip() != "",
                what="FaA2Row peak override source and reason must be non-empty",
            )
        if self.peak_balance_inr is None:
            require(
                self.audit_peak_balance_bound is None and override_absent,
                what=(
                    "FaA2Row cannot select a peak bound or override while peak_balance_inr is None"
                ),
            )
        else:
            require(
                (
                    bounds_present
                    and self.audit_peak_balance_bound in {"lower", "upper"}
                    and override_absent
                )
                or (bounds_absent and self.audit_peak_balance_bound is None and override_present),
                what=(
                    "FaA2Row populated peak requires exactly one provenance mode: "
                    "audited bounds with a selected bound, or an override source/reason"
                ),
            )
            if bounds_present:
                expected_peak = (
                    self.audit_peak_balance_lower_inr
                    if self.audit_peak_balance_bound == "lower"
                    else self.audit_peak_balance_upper_inr
                )
                require(
                    self.peak_balance_inr == expected_peak,
                    what=("FaA2Row populated peak must equal the explicitly selected audit bound"),
                )
        # nature_of_amount and gross_amount_inr are coupled: both or neither.
        require(
            (self.nature_of_amount is None) == (self.gross_amount_inr is None),
            what=("FaA2Row.nature_of_amount and gross_amount_inr must both be set or both be None"),
            nature_of_amount=self.nature_of_amount,
            gross_amount_inr=str(self.gross_amount_inr),
        )
        if self.gross_amount_inr is not None:
            require(
                self.gross_amount_inr >= 0,
                what="FaA2Row.gross_amount_inr must be non-negative when set",
                gross_amount_inr=str(self.gross_amount_inr),
            )
        if self.nature_of_amount is FaA2NatureOfAmount.NO_AMOUNT:
            require(
                self.gross_amount_inr == 0,
                what="FaA2Row.gross_amount_inr must be zero when nature_of_amount is NO_AMOUNT",
            )
        # Closing balance and its base-currency provenance are coupled. The
        # Dec-31 TTBR may also be present when closing is manual-fill because
        # Schedule FA gross credits use the same rate.
        require(
            (self.closing_balance_inr is None) == (self.audit_closing_balance_base_ccy is None),
            what=(
                "FaA2Row.closing_balance_inr and audit_closing_balance_base_ccy "
                "must both be set or both be None"
            ),
            closing_balance_inr=str(self.closing_balance_inr),
            audit_closing_balance_base_ccy=str(self.audit_closing_balance_base_ccy),
        )
        gross_credits_use_dec31_ttbr = any(
            name in {nature.value for nature in FA_A2_CREDIT_NATURES} and amount != 0
            for name, amount in self.audit_gross_components.items()
        )
        require(
            (self.audit_ttbr_dec31 is not None)
            == (self.closing_balance_inr is not None or gross_credits_use_dec31_ttbr),
            what=(
                "FaA2Row.audit_ttbr_dec31 must be set exactly when a closing "
                "balance or Schedule FA gross credit uses it"
            ),
            closing_balance_inr=str(self.closing_balance_inr),
            gross_credits_use_dec31_ttbr=gross_credits_use_dec31_ttbr,
            audit_ttbr_dec31=str(self.audit_ttbr_dec31),
        )
        if self.audit_ttbr_dec31 is not None:
            require(
                self.audit_ttbr_dec31 > 0,
                what="FaA2Row.audit_ttbr_dec31 must be positive when set",
                audit_ttbr_dec31=str(self.audit_ttbr_dec31),
            )
        # Upload rendering (#1329), last so every earlier refusal keeps its message.
        require_upload_text(
            self,
            "country_name",
            "institution_name",
            "institution_address",
            "institution_zip",
            "account_number",
        )
