"""Typed loader for ``user.toml``.

``user.toml`` is the per-user configuration file the CLI reads (path
passed via ``--config``). It carries source-specific account facts and
shared taxpayer settings. The IBKR account number is optional at load
time for non-IBKR flows, but every IBKR statement command requires and
cross-checks it before parsing or writing output.

Schema (minimal v1; future fields will be added without breaking
compatibility):

.. code-block:: toml

    [account]
    number        = "U1234567"           # IBKR account ID; omit for NetBenefits-only
    holder_name   = "Sample Holder"
    opening_date  = 2024-02-01           # required by IBKR commands
    status        = "BeneficialOwner"    # required by IBKR/StockPlan commands
    us_person     = false                # optional, default false; true if
                                         # W-9 on file at IBKR (US citizen
                                         # or green-card holder). Drives the
                                         # advisory message about
                                         # withholding-tax expectations
                                         # (see issue #145).
    # Optional persistent opt-ins mirroring the CLI safety flags (either the
    # flag OR the opt-in enables the behaviour; the loud warning still fires):
    allow_masked_account        = false  # skip the account-ID cross-check
                                         # when IBKR masks the Account row
                                         # (== --allow-masked-account)
    accept_anonymised_statement = false  # take holder name / number from
                                         # this table when the export hides
                                         # them (== --accept-anonymised-statement)

    # Optional: your foreign Taxpayer Identification Number per country,
    # auto-filled into the Schedule FSI + TR "Taxpayer Identification Number"
    # column (keyed by ISO 3166-1 alpha-2; never "IN"). Omit to leave blank.
    [account.foreign_tins]
    US = "123456789"                   # TIN, or passport if none was allotted

    # Optional: your IBKR custodial institution's details, used to fill the
    # Schedule FA Table A2 (foreign custodial account) row. country_iso2 is the
    # INSTITUTION's country (where the IBKR entity is), never "IN". Omit if you
    # don't generate Table A2.
    [account.custodian]
    institution_name = "Interactive Brokers LLC"
    address          = "One Pickwick Plaza, Greenwich, CT"
    zip              = "06830"           # needed for upload; blank = manual fill (exit 3)
    country_iso2     = "US"

    [fx]
    # Optional. Defaults to 14 (FaExactDatePolicy default).
    max_carry_forward_days = 14

    [os]
    # Optional. Defaults to "depositary" (the asset-as-held
    # convention — matches docs/portal/adr-handling.md for
    # Schedule FA). Issue #183: opt-in to "underlying-issuer"
    # to treat mapped Indian-company ADR/GDR dividends as Indian-source
    # rather than depositary-country-source for FSI/TR/Form 67. This
    # does not change the Schedule OS 1a(i) total.
    schedule_os_indian_adr_routing = "depositary"

    [form67]
    # Optional exact per-(country, source) Indian tax payable.
    # Rows without an override leave Indian-tax and FTC columns blank.
    [[form67.tax_payable_overrides]]
    country = "US"
    source = "DIVIDEND"
    tax_payable_normal_inr = "12345.67"

    # Optional. Per-issuer Schedule FA Table A3 details, keyed by the
    # holding's ISIN. Nature must come from authoritative issuer evidence;
    # broker instrument labels do not establish the entity's legal form.
    [[fa.entities]]
    isin = "US99999A1025"
    address = "1 Example Plaza, Anytown, NY"
    zip = "99999"
    name = "Example Holdings Inc."  # optional; omit to keep the statement name
    nature_of_entity = "Company"    # verified free text, 1-34 chars

    # Optional. Manual figures for the CBDT Schedule FA JSON fragment (#39)
    # that cannot be computed from the statement. Only needed if you generate
    # that JSON. ``manual_values_account_number`` MUST equal ``[account].number``
    # (binds these filed figures to this account so a stale peak can't carry
    # across a copied config). One [[schedule_fa.manual_values]] block per
    # calendar year; ``a2_peak_balance_inr`` is whole rupees (integer).
    [schedule_fa]
    manual_values_account_number = "U1234567"
    peak_currency = "native"
    [[schedule_fa.manual_values]]
    calendar_year       = 2025
    a2_peak_balance_inr = 8950           # native peak converted at peak-date TTBR

    # Optional: Fidelity NetBenefits (e.g. Microsoft ESPP) support. Participant
    # No. is the sole observed cross-document account identifier and is used for
    # Schedule FA A2.
    [netbenefits]
    espp_discount_pct = "10"
    participant_no    = "OQ1234567"
    # For CBDT JSON A2 peak:
    # a2_peak_overrides_participant_no = "OQ1234567"
    # [[netbenefits.a2_peak_overrides]]
    # calendar_year = 2025
    # peak_balance_inr = 57314
    [netbenefits.account]
    institution_name     = "Fidelity Stock Plan Services LLC"
    address              = "245 Summer Street, Boston, MA"
    country_iso2         = "US"
    account_opening_date = 2025-12-31     # required; never inferred
    status               = "BeneficialOwner"
    # Quicko "Name of the deductor": the withholding agent on Form 1042-S (box
    # 12d for tax in box 7a; the agent box 14a names for tax in box 8). Needed
    # only when the run writes a Quicko form.
    withholding_agent_name = "National Financial Services LLC"
    [netbenefits.security]
    symbol = "MSFT"
    cusip = "594918104"
    isin = "US5949181045"
    entity_address = "One Microsoft Way, Redmond, WA"
    listing_exchange = "NASDAQ"
    issuer_country_code = "US"
    nature_of_entity = "Listed company shares"
    [[netbenefits.price_overrides]]
    price_date = 2025-12-31
    price_per_share_usd = "312.45"
    source = "SPS year-end statement"
    reason = "12-31 lot unpriced in CSV"

    # Optional: the exact two-account Robinhood brokerage bundle. These account
    # facts never reuse the legacy IBKR number/opening_date/status above.
    [[robinhood.accounts]]
    number = "123456789"
    opening_date = 2025-01-01
    status = "BeneficialOwner"
    institution_name = "Robinhood Securities, LLC"
    address = "500 Colonial Center Parkway, Lake Mary, FL"
    zip = "32746"
    country_iso2 = "US"
    # Taxpayer attestation that beneficial ownership is unchanged across both
    # configured accounts; #470 separately proves statement transfer evidence.
    same_beneficial_owner = true
    source = "individual brokerage statement"

    [[robinhood.accounts]]
    number = "987654321"
    opening_date = 2025-10-01
    status = "BeneficialOwner"
    institution_name = "Robinhood Securities, LLC"
    address = "500 Colonial Center Parkway, Lake Mary, FL"
    zip = "32746"
    country_iso2 = "US"
    same_beneficial_owner = true
    source = "managed brokerage statement"

    [[robinhood.securities]]
    symbol = "MSFT"
    cusip = "594918104"
    isin = "US5949181045"
    listing_exchange = "NASDAQ"
    sub_category = "COMMON"  # COMMON or ETF
    asset_class = "STK"
    issuer_country_code = "US"
    # Independently verified legal form; never derived from ISIN/country.
    is_depositary_receipt = false
    source = "issuer and exchange records"

    # Every REC row requires one exact resolution. Set income_reported=true
    # only after filing/documenting that reward FMV as Indian income.
    # [[robinhood.security_receipt_resolutions]]
    # account_number = "123456789"
    # source_record_index = 4
    # cusip = "594918104"
    # treatment = "promotional-reward"
    # exact_quantity = "0.027318"
    # reward_fair_market_value_usd = "5.00"
    # income_reported_financial_year = 2024
    # income_reported = true
    # source = "Robinhood reward confirmation and filed Indian return/workpaper"
    # reason = "Promotional stock reward reported as income at documented FMV"

    [[robinhood.option_classifications]]
    account_number = "123456789"
    financial_year = 2025
    fema_risk_acknowledged = true
    treatment = "speculative-business"
    books_maintained = false
    other_speculative_business = false
    nature_of_business_code = "21010"
    turnover_basis = "icai-5.11a-differences"
    external_expenses_inr = "0"
    bp_additions_inr = "0"
    bp_deductions_inr = "0"
    source = "reviewed CA memo identifying the section 43(5) treatment"
    reason = "supported long options closed BTO-to-STC without delivery"

    [[robinhood.stock_lending_fee_fsi_sources]]
    account_number = "123456789"
    financial_year = 2025
    country_iso2 = "US"
    source = "reviewed Robinhood Stock Lending agreement and tax position"
    reason = "RHS is the principal borrower and loan-fee obligor"

    [[robinhood.interest_payer_countries]]
    account_number = "123456789"
    financial_year = 2025
    country_iso2 = "US"
    interest_kinds = ["brokerage", "program-bank"]
    source = "reviewed Robinhood brokerage interest and Cash Sweep Program terms"
    reason = "every payer of this account's FY interest is a US entity"

    [[robinhood.reports]]
    file = "activity/individual.csv"
    start_date = 2025-01-01
    end_date = 2026-03-31
    source = "Robinhood account activity export"

    [[robinhood.reports]]
    file = "activity/managed-individual.csv"
    start_date = 2025-01-01
    end_date = 2026-03-31
    source = "Robinhood managed account activity export"

    # Optional explicit winner when Robinhood issued a corrected statement.
    [[robinhood.corrected_statements]]
    selected_file = "statements/2025-12-corrected.pdf"
    supersedes = ["statements/2025-12.pdf"]
    source = "Robinhood corrected monthly statement"

The loader is strict: extra top-level tables or unknown fields raise
:class:`~ibkr_fa.exceptions.ConfigError` naming the table that holds them,
because silent typos in tax config files have caused real-world
mis-filings. A refusal names the field path and the rule broken, never the
configured value (#1045). Nor does its exception graph hold one: no configured
content is in the refusal's fields, args, ``str``, ``repr`` or rendered
traceback, or in any ``__cause__`` or ``__context__`` it links to, suppressed
or not (``raise … from None`` still keeps the context). So a check whose
failure would raise an exception holding content (a lookup's ``ValueError``
quoting the value, a country lookup's code, a decode error's bytes, a TOML
error's text or, on Python 3.14, its whole document) tests membership first,
uses a lookup returning a sentinel, or keeps only a fixed message and refuses
after the ``except`` block has ended (#1210). The one input a refusal may show
is a path the user supplied, where its message is meant to: a missing
``user.toml`` names that path.
"""

from __future__ import annotations

import datetime as _dt
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

from ibkr_fa.exceptions import ConfigError, PathRefusalError
from ibkr_fa.models.account_identity import (
    SIGNING_AUTHORITY_UNSUPPORTED,
    AccountIdentity,
    InstitutionIdentity,
    TaxpayerStatus,
)
from ibkr_fa.models.form67_entry import Form67SourceOfIncome
from ibkr_fa.models.instrument_identity import InstrumentIdentity
from ibkr_fa.models.money import CBDT_AMOUNT_BOUND
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.path_checks import exists
from ibkr_fa.pii import is_masked_account_id
from ibkr_fa.reference.country_codes import load_country_codes
from ibkr_fa.text_checks import first_control_character
from ibkr_fa.types import (
    CUSIP_PATTERN,
    AccountId,
    CountryCode,
    Isin,
    PeakBasis,
    PeakCurrency,
    Ticker,
    cusip_check_digit_matches,
    isin_check_digit,
)
from ibkr_fa.types import ISIN_PATTERN as _ISIN_PATTERN


class ScheduleOsIndianAdrRoutingMode(StrEnum):
    """Indian-company ADR / GDR source-attribution mode (issue #183).

    Determines whether dividends from Indian-company ADRs / GDRs
    held in an IBKR account are foreign-source by depositary ISIN or
    Indian-source by underlying issuer for FSI/TR/Form 67. Every
    supported ordinary dividend still enters Schedule OS 1a(i).

    Default is :attr:`DEPOSITARY` (conservative, matches Schedule FA
    convention). Users / CAs who prefer the underlying-issuer view
    must explicitly opt in via ``schedule_os_indian_adr_routing =
    "underlying-issuer"`` in ``user.toml``.

    Values:
        DEPOSITARY: Attribute ADR/GDR dividends to their ISIN-derived
            depositary country (US / Jersey / Cayman). Default.
        UNDERLYING_ISSUER: ADR / GDR dividends whose ISIN appears in
            the hardcoded mapping are attributed to India. Non-mapped
            ADRs/GDRs remain depositary-country-source. Users who hold
            an Indian-company ADR/GDR
            not in the mapping should file an issue with the
            IBKR-emitted ISIN + the underlying company name to
            request a mapping addition.
    """

    DEPOSITARY = "depositary"
    UNDERLYING_ISSUER = "underlying-issuer"


class FidelityCmaRefundStatus(StrEnum):
    """US refund/finality state for one Form 1042-S tax year."""

    UNKNOWN = "unknown"
    NOT_FILED_PLANNED = "not-filed-planned"
    FILED_PENDING = "filed-pending"
    FINALIZED = "finalized"
    NO_REFUND_CLAIM = "no-refund-claim"


# The statuses whose per-date allocations can claim FTC (ADR 0004 section 6).
FIDELITY_CMA_FTC_STATUSES: Final[frozenset[FidelityCmaRefundStatus]] = frozenset(
    {FidelityCmaRefundStatus.FINALIZED, FidelityCmaRefundStatus.NO_REFUND_CLAIM}
)


class RobinhoodOptionTreatment(StrEnum):
    """Reviewed Indian tax treatment for one Robinhood account/FY."""

    CAPITAL_GAINS = "capital-gains"
    SPECULATIVE_BUSINESS = "speculative-business"
    NON_SPECULATIVE_BUSINESS = "non-speculative-business"


class RobinhoodOptionTurnoverBasis(StrEnum):
    """Reviewed ICAI turnover interpretation for one option business."""

    ICAI_5_11A_DIFFERENCES = "icai-5.11a-differences"


class RobinhoodSecurityReceiptTreatment(StrEnum):
    """Reviewed treatment for one basis-less Robinhood ``REC`` row."""

    PROMOTIONAL_REWARD = "promotional-reward"


class RobinhoodInterestKind(StrEnum):
    """Robinhood interest, as the sweep-ledger partition classifies it.

    ``PROGRAM_BANK`` covers every Program Bank that paid the account's sweep
    interest: the partition matches interest to sweep rows by date and amount,
    never to a named bank.
    """

    BROKERAGE = "brokerage"
    PROGRAM_BANK = "program-bank"


class FidelityCmaPeakBalanceBound(StrEnum):
    """User-attested bound for an intraday-ambiguous CMA A2 peak."""

    LOWER = "lower"
    UPPER = "upper"


@dataclass(frozen=True, slots=True)
class FxConfig:
    """Optional ``[fx]`` table.

    Attributes:
        max_carry_forward_days: Override for
            :class:`~ibkr_fa.fx.policy_fa.FaExactDatePolicy`'s default
            14-day staleness ceiling. Useful when running against a
            stale bundled TTBR archive (e.g., on an offline machine).
    """

    max_carry_forward_days: int = 14


@dataclass(frozen=True, slots=True)
class OsConfig:
    """Optional ``[os]`` table (issue #183).

    Attributes:
        schedule_os_indian_adr_routing: Indian-company ADR / GDR
            source-attribution mode for FSI/TR/Form 67. Default
            :attr:`ScheduleOsIndianAdrRoutingMode.DEPOSITARY`
            (asset-as-held; matches v1 behaviour and the Schedule FA
            ADR convention from ``docs/portal/adr-handling.md``). Set
            to ``"underlying-issuer"`` to attribute mapped ADRs to
            India rather than the depositary country. Schedule OS
            remains one 1a(i) total in either mode.
    """

    schedule_os_indian_adr_routing: ScheduleOsIndianAdrRoutingMode = (
        ScheduleOsIndianAdrRoutingMode.DEPOSITARY
    )


@dataclass(frozen=True, slots=True)
class Form67TaxPayableOverride:
    """One per-(country, source-of-income) explicit Indian tax-payable
    override for Form 67 col 7.

    Used when exact Indian tax payable for a specific
    (source-country, source-of-income) slice is available.

    Attributes:
        iso_alpha2: ISO 3166-1 alpha-2 source country code (e.g.,
            ``"US"``). Same convention as everywhere else in the
            codebase. Must be in the ITR country list and never
            ``"IN"``, since an override applies only by exact match
            to a foreign-source Form 67 row.
        source_of_income: Which Form 67 source-of-income row this
            override applies to (currently only
            :attr:`Form67SourceOfIncome.DIVIDEND` is emitted by the
            compute layer; the field is enum-typed so future
            interest / royalty / fees-for-technical-services support
            doesn't change the override schema).
        tax_payable_normal_inr: Per-source Indian tax payable in INR
            (verbatim — the compute layer does not re-apply any rate
            calculation). Must be >= 0; zero is a legitimate value
            (e.g., when the foreign income is wholly absorbed by
            carry-forward losses).
    """

    iso_alpha2: str
    source_of_income: Form67SourceOfIncome
    tax_payable_normal_inr: Decimal


@dataclass(frozen=True, slots=True)
class Form67Section91Config:
    """Optional ``[form67.section_91]`` sub-table (step 4d/N of #37).

    Drives Form 67's §91 unilateral relief computation for NO_DTAA-
    vendored countries (cols 12 + 13). §91 of the Income-tax Act,
    1961 grants relief for income from a country with which India
    has NO DTAA under §90 — the relief is computed at the LOWER of
    the Indian average rate of tax and the foreign rate of tax,
    applied to the doubly-taxed income.

    The §91 Explanation in the Act defines:

      * "Indian rate of tax" = total Indian income-tax (after
        Chapter VI-A relief but BEFORE §91 relief) divided by
        the assessee's total income.
      * "rate of tax of the said country" = foreign income-tax
        paid divided by foreign income as assessed in that
        country.
      * "doubly taxed income" = the part of income included in the
        assessee's total income under the Act AND taxed in the
        foreign country.

    Per the rubber-duck split-fields recommendation (audit
    transparency over compact-derived-rate field), this config
    expects the two numerator/denominator inputs SEPARATELY so the
    derived Indian average rate is reproducible from the
    user-supplied numbers. A single ``indian_average_rate_percent``
    field would have been simpler but would hide the underlying
    computation from auditor reconciliation.

    The two fields cannot be derived from the IBKR Activity
    Statement alone — the taxpayer (or their CA) supplies them
    based on the full ITR-2 computation across all heads of income.
    Both fields are REQUIRED together when ``[form67.section_91]``
    is supplied; the loader rejects partial population at parse
    time.

    Attributes:
        total_indian_tax_before_91_relief_inr: Sum of Indian
            income-tax payable AFTER Chapter VI-A relief but
            BEFORE §91 relief (per the §91 Explanation's
            \"Indian rate of tax\" numerator). Must be >= 0; zero
            is legitimate (e.g., total income below the basic
            exemption limit — in which case §91 relief is
            structurally zero too).
        total_income_inr: Sum of total income under the Income-tax
            Act (per the §91 Explanation's \"Indian rate of tax\"
            denominator). Must be > 0; zero would make the average
            rate undefined (zero-divided-by-zero).
    """

    total_indian_tax_before_91_relief_inr: Decimal
    total_income_inr: Decimal


@dataclass(frozen=True, slots=True)
class Form67Config:
    """Optional exact Form 67 tax-payable and section 91 inputs.

    Entries without an exact override leave Indian-tax and FTC columns
    blank. Non-DTAA section 91 claims require both an exact source-level
    tax-payable override and the statutory aggregate inputs.
    """

    tax_payable_overrides: tuple[Form67TaxPayableOverride, ...] = field(default_factory=tuple)
    section_91: Form67Section91Config | None = None


@dataclass(frozen=True, slots=True)
class TickerOverride:
    """One ``[[prices.ticker_overrides]]`` entry (Schedule FA peak value).

    Maps an IBKR holding to the symbol the price source (Yahoo) actually
    lists it under, for the cases where the IBKR ticker does not resolve
    — a post-rename / cross-listing mismatch (e.g. after a rename IBKR
    exports a holding as ``XNEW``, but Yahoo still lists the US security
    under the pre-rename ticker ``XOLD``). Used by ``schedule-fa``,
    ``robinhood-fa``, ``stockplan-fa`` and ``netbenefits-fa``. Robinhood,
    StockPlan and NetBenefits holdings use the same entries, keyed by the
    ``symbol`` and ``isin`` of their configured security. Only the
    fetched symbol changes; the output keeps the holding's own symbol.

    **Why the ISIN is required** (not just an IBKR-ticker -> symbol map):
    a bare ticker map is tax-unsafe — tickers get reused / renamed, so a
    bare ``XNEW -> XOLD`` could silently misapply XOLD prices to a
    *different* ``XNEW`` holding. Binding the override to the holding's
    ISIN (which the IBKR statement always carries, and which is globally
    unique) means the override applies **only** to the exact security the
    user intended. A lot must match BOTH ``ibkr_ticker`` AND ``isin`` for
    the override to take effect; otherwise the holding's own symbol is used
    unchanged (and the price layer's currency / identity / split /
    staleness guards still apply on top, so a wrong ``yahoo_symbol`` that
    points at a non-USD or mismatched security still fails safe to
    manual-fill or a refusal rather than producing a wrong number).

    Attributes:
        ibkr_ticker: The symbol as it appears in the IBKR statement
            (e.g. ``"XNEW"``), or the configured ``symbol`` of a Robinhood,
            StockPlan or NetBenefits security. The key keeps its IBKR name.
        isin: The holding's ISO 6166 ISIN (e.g. ``"CA99999B1004"``), check
            digit validated at load — the override applies only to a lot whose
            ISIN matches.
        yahoo_symbol: The symbol the price source lists the same security
            under (e.g. ``"XOLD"``). Must be a USD listing for the peak
            compute to use it (the currency guard rejects non-USD).
    """

    ibkr_ticker: Ticker
    isin: Isin
    yahoo_symbol: str


@dataclass(frozen=True, slots=True)
class PricesConfig:
    """Optional ``[prices]`` TOML table (Schedule FA peak-value sourcing).

    Attributes:
        massive_api_key_file: Optional file path containing
            ``MASSIVE_API_KEY=...``. Relative paths resolve beside ``user.toml``;
            absolute paths are used directly. The key itself never enters TOML.
        ticker_overrides: Tuple of :class:`TickerOverride` entries used by
            ``schedule-fa``, ``robinhood-fa``, ``stockplan-fa`` and ``netbenefits-fa``,
            resolved at FA-compute time as a dict keyed by ``(ibkr_ticker, isin)``.
            For Robinhood, StockPlan and NetBenefits, ``ibkr_ticker`` is the
            configured ``symbol``. Empty by default (no overrides;
            every holding is fetched under its own symbol).
    """

    massive_api_key_file: str | None = None
    ticker_overrides: tuple[TickerOverride, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class FaEntityOverride:
    """One ``[[fa.entities]]`` entry — per-issuer Schedule FA Table A3 identity.

    Lets a filer supply the real issuer address / ZIP, an optional cleaner
    entity name, and the verified legal nature of the entity for a holding.
    Bound to the holding's ISIN (globally unique, always on the statement) so
    the override applies only to the intended security.

    Robinhood prefers an exact-ISIN GLEIF legal address when available.
    ``prefer_configured_address`` and ``prefer_configured_nature`` are the
    independent reviewed opt-outs; configured name and nature otherwise fill
    source gaps. A Robinhood series ETF is the exception (#793): with an entry,
    it takes only the configured cells, whatever the ``prefer_configured_*``
    flags say, and a cell left out, including ``name``, stays manual, except
    ``address``: an entry without one stops Schedule FA (exit 4) and writes
    the guidance file ``robinhood-identity-guidance.toml`` (#1449).

    Attributes:
        isin: The holding's ISO 6166 ISIN, check digit validated at load — the
            override applies only to a lot whose ISIN matches.
        address: The issuer's address (free text; the portal's
            "Address of entity" cell accepts up to 200 chars). ``None`` when
            the entry leaves it out (#1449). For a held security, Robinhood
            Schedule FA stops until the filer verifies an address, and IBKR
            Schedule FA refuses.
            Automatic overrides always set it.
        zip_code: The issuer's ZIP / postal code; empty string by default.
        name: Optional override of the entity name (the portal's "Name of
            entity"); ``None`` keeps the name from the IBKR statement.
        nature_of_entity: Verified free-text value for the portal's "Nature of
            entity" cell. ``None`` leaves the review cell blank and triggers a
            manual-fill warning. The official AY 2026-27 schema requires
            1-34 characters when populated.
        prefer_configured_address: Keep the configured address/ZIP for
            Robinhood instead of applying GLEIF legal-address data. GLEIF may
            still be queried for nature unless both preferences are true.
        prefer_configured_nature: Keep the configured nature for Robinhood
            instead of applying automatic legal-form evidence.
        prefer_configured_name: Keep the configured name for Robinhood instead
            of applying an automatic legal-issuer name.
    """

    isin: Isin
    address: str | None = None
    zip_code: str = ""
    name: str | None = None
    nature_of_entity: str | None = None
    prefer_configured_address: bool = False
    prefer_configured_nature: bool = False
    prefer_configured_name: bool = False


@dataclass(frozen=True, slots=True)
class FaConfig:
    """Optional ``[fa]`` TOML table — Schedule FA presentation overrides.

    Attributes:
        entities: Per-issuer :class:`FaEntityOverride` entries, resolved at
            FA-compute time as a dict keyed by ISIN. Empty by default.
    """

    entities: tuple[FaEntityOverride, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ScheduleFaManualValue:
    """One ``[[schedule_fa.manual_values]]`` entry — the per-calendar-year
    manual figures the CBDT Schedule FA JSON fragment (#39) needs but which
    cannot be computed from the IBKR statement.

    Currently just the **A2 peak balance**: select the highest TOTAL
    native-currency value (securities + cash, per Rule 114F) first, then convert
    it at that date's TTBR and supply the whole-INR result. The statement carries
    no daily account NAV, so this is genuinely uncomputable here. Per-CY because
    a peak is year-specific (a stale figure must not silently carry forward).

    Attributes:
        calendar_year: The Schedule FA calendar year (Jan-Dec) this figure is
            for. Matched against the run's ``--calendar-year``.
        a2_peak_balance_inr: The Table A2 peak balance in whole INR (integer;
            a fractional value is rejected — a filed figure is never silently
            rounded). May be negative for a margined account.
    """

    calendar_year: int
    a2_peak_balance_inr: int


@dataclass(frozen=True, slots=True)
class ScheduleFaConfig:
    """Optional ``[schedule_fa]`` TOML table — manual values for the CBDT
    Schedule FA JSON fragment (#39).

    Attributes:
        manual_values_account_number: Must equal ``[account].number`` when any
            ``manual_values`` are present — a guard against a stale peak
            silently carrying across a user.toml copied to a different account.
            ``None`` when no manual values are configured.
        manual_values: Per-calendar-year :class:`ScheduleFaManualValue`
            entries, at most one per calendar year. Empty by default.
        peak_basis: How the FA-A3 "Peak value during the period" is valued —
            ``"close"`` (default, daily closing price) or ``"high"`` (daily
            intraday high, a more conservative reading). Applies uniformly to
            every FA-A3 lot (IBKR and NetBenefits); ``fidelity-cma-fa``
            rejects ``"high"`` with exit 4 and writes neither A2 nor A3
            output (#622).
        peak_currency: Currency in which peaks are selected. ``"native"``
            (default) selects the foreign-currency peak before conversion;
            ``"inr"`` is the warned alternative that maximizes daily
            INR-converted values.
    """

    manual_values_account_number: str | None = None
    manual_values: tuple[ScheduleFaManualValue, ...] = field(default_factory=tuple)
    peak_basis: PeakBasis = "close"
    peak_currency: PeakCurrency = "native"

    def manual_value_for(self, calendar_year: int) -> ScheduleFaManualValue | None:
        """The configured manual value for ``calendar_year``, or ``None``."""
        for entry in self.manual_values:
            if entry.calendar_year == calendar_year:
                return entry
        return None


@dataclass(frozen=True, slots=True)
class CustodianConfig:
    """The optional ``[account.custodian]`` sub-table — the IBKR custodial
    institution's identity for Schedule FA **Table A2**.

    The statement does not reliably carry the institution's legal name /
    address / country (the Activity Statement's Account Information has no
    IBKR entity), and the institution country can differ from the holdings'
    countries (IBKR LLC = US, IBKR Ireland = IE, ...), so this is config-driven.

    Attributes:
        institution_name: Custodial institution legal name (e.g.
            ``"Interactive Brokers LLC"``).
        address: Institution address (free text).
        zip_code: Institution ZIP / postal code; may be empty. The CSV and
            XLSX formats then leave the cell blank, the run names it as a
            cell to fill by hand and exits 3, and
            ``--format cbdt-json-fragment`` reports it as a gap (both CBDT
            schemas require a ZIP).
        country_iso2: ISO 3166-1 alpha-2 code of the INSTITUTION's country.
            Validated against the CBDT country table; never ``"IN"`` (Table A2
            is a FOREIGN custodial account).
    """

    institution_name: str
    address: str
    zip_code: str
    country_iso2: str


@dataclass(frozen=True, slots=True)
class AccountConfig:
    """The ``[account]`` table — everything Schedule FA needs to identify
    the holder and the account.

    Attributes:
        number: IBKR account number. Optional for source-specific flows such
            as Fidelity NetBenefits; every IBKR statement pipeline requires
            and cross-checks it before parsing or writing output.
        holder_name: Account holder's legal name as it appears on the
            ITR. Not validated against IBKR's ``account_holder_name``
            (legitimate casing/initial differences).
        opening_date: Date the IBKR account was first opened. Required only
            by IBKR commands; other sources never read it.
        status: :class:`TaxpayerStatus` — how the user owns the IBKR account.
            Optional only for source-specific flows with their own account
            facts. ``SigningAuthority`` is refused at load.
        us_person: Optional. ``True`` if the user has a W-9 on file at
            IBKR (US citizen or green-card holder). Default ``False``
            (the W-8BEN / non-US-person case). Drives the CLI's
            withholding-tax advisory: a US person legitimately sees
            ``$0`` NRA withholding on US-source dividends (IBKR
            doesn't withhold on W-9 accounts), while a non-US-person
            seeing ``$0`` likely has a W-8BEN issue. Issue #145.
            Not validated against IBKR (the export doesn't carry the
            W-8 / W-9 status); ``user.toml`` is the source of truth.
        allow_masked_account: Optional. ``True`` opts the account-ID
            cross-check out persistently for a statement whose Account
            row IBKR masked (``U**********``), so a repeat user need not
            pass ``--allow-masked-account`` on every run. Default
            ``False``. The CLI flag OR this setting enables the skip
            (either source), and the loud "cross-check SKIPPED" warning
            still fires. The #141 guard still forbids a masked
            :attr:`number`, so a real account ID must live here.
        accept_anonymised_statement: Optional. ``True`` opts into the
            #141 identity-fallback path persistently — when IBKR's
            export hides the Account ``Name`` / ``Account`` rows, the
            holder name / number are taken from this ``[account]`` table
            — so a repeat user need not pass
            ``--accept-anonymised-statement`` every run. Default
            ``False``. CLI flag OR this setting enables it; the loud
            "identity supplied from user.toml" warning still fires.
        foreign_tins: Optional ``[account.foreign_tins]`` map of ISO
            3166-1 alpha-2 country code → the assessee's Taxpayer
            Identification Number assigned by that country (e.g.
            ``US = "123456789"``). Stamped onto the matching Schedule
            FSI / TR rows so the portal's "Taxpayer Identification
            Number" column auto-populates. Empty by default (the column
            stays manual-fill). Keys must be 2-letter uppercase ISO
            alpha-2 codes in the ITR country list (never ``"IN"`` —
            FSI/TR are foreign-source only);
            values are non-empty strings. The value is the user's own
            foreign tax ID and is written verbatim into the review
            output (as required for filing).
    """

    number: AccountId | None
    holder_name: str
    opening_date: _dt.date | None
    status: TaxpayerStatus | None
    us_person: bool = False
    allow_masked_account: bool = False
    accept_anonymised_statement: bool = False
    foreign_tins: Mapping[str, str] = field(default_factory=dict)
    custodian: CustodianConfig | None = None

    def require_number(self) -> AccountId:
        """Return the real IBKR account ID, or refuse an IBKR statement run."""
        if self.number is None:
            raise ConfigError(
                field="account.number",
                message=(
                    "is required for IBKR statement commands; it may be omitted only "
                    "for source-specific flows such as Fidelity NetBenefits"
                ),
            )
        return self.number

    def require_opening_date(self) -> _dt.date:
        """Return the IBKR opening date, or refuse an IBKR run."""
        if self.opening_date is None:
            raise ConfigError(
                field="account.opening_date",
                message=(
                    "is required for IBKR commands; it may be omitted for other sources, "
                    "which never read it"
                ),
            )
        return self.opening_date

    def require_status(self) -> TaxpayerStatus:
        """Return the IBKR ownership status, or refuse a legacy-source run."""
        if self.status is None:
            raise ConfigError(
                field="account.status",
                message=(
                    "is required for IBKR and StockPlan commands; it may be omitted only "
                    "for source-specific flows with their own account facts"
                ),
            )
        return self.status


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodAccountConfig:
    """One filed Robinhood brokerage account.

    ``same_beneficial_owner`` attests that the filing taxpayer's beneficial
    ownership is unchanged across this account and every other configured
    Robinhood account. It is taxpayer-supplied context, never reconciliation
    proof; #470 proves transfer ownership from statement evidence.
    """

    number: str
    opening_date: _dt.date
    status: TaxpayerStatus
    institution_name: str
    address: str
    zip_code: str
    country_iso2: str
    same_beneficial_owner: bool
    source: str


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodOptionClassificationConfig:
    """One reviewed account/FY option-tax decision."""

    account_number: str
    financial_year: int
    fema_risk_acknowledged: bool
    treatment: RobinhoodOptionTreatment
    source: str
    reason: str
    books_maintained: bool | None = None
    other_speculative_business: bool | None = None
    nature_of_business_code: str | None = None
    turnover_basis: RobinhoodOptionTurnoverBasis | None = None
    external_expenses_inr: Decimal | None = None
    bp_additions_inr: Decimal | None = None
    bp_deductions_inr: Decimal | None = None


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodStockLendingFeeFsiSource:
    """One reviewed account/FY source country for generic loan fees."""

    account_number: str
    financial_year: int
    country_iso2: str
    source: str
    reason: str


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodInterestPayerCountry:
    """One reviewed account/FY payer country for the listed interest kinds (#698)."""

    account_number: str
    financial_year: int
    country_iso2: str
    interest_kinds: frozenset[RobinhoodInterestKind]
    source: str
    reason: str


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodSecurityReceiptResolution:
    """One exact ``REC`` row resolved from external quantity/basis evidence."""

    account_number: str
    source_record_index: int
    cusip: str
    treatment: RobinhoodSecurityReceiptTreatment
    exact_quantity: Decimal
    reward_fair_market_value_usd: Decimal
    income_reported_financial_year: int
    income_reported: bool
    source: str
    reason: str


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodReportConfig:
    """One configured activity report and its selected export range."""

    file: str
    start_date: _dt.date
    end_date: _dt.date
    source: str


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodCorrectedStatementConfig:
    """Explicit statement winner and the files it supersedes."""

    selected_file: str
    supersedes: tuple[str, ...]
    source: str


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodConfig:
    """The exact two filed Robinhood accounts.

    Tuple order is config-file order and exists only as a diagnostic locator.
    Account identity is ``number`` and #470 binds it to statement evidence.
    """

    accounts: tuple[RobinhoodAccountConfig, ...]
    securities: tuple[RobinhoodSecurityConfig, ...] = ()
    security_receipt_resolutions: tuple[RobinhoodSecurityReceiptResolution, ...] = ()
    option_classifications: tuple[RobinhoodOptionClassificationConfig, ...] = ()
    stock_lending_fee_fsi_sources: tuple[RobinhoodStockLendingFeeFsiSource, ...] = ()
    interest_payer_countries: tuple[RobinhoodInterestPayerCountry, ...] = ()
    reports: tuple[RobinhoodReportConfig, ...] = ()
    corrected_statements: tuple[RobinhoodCorrectedStatementConfig, ...] = ()
    sec_user_agent: str | None = None
    cusip_transitions_file: str | None = None

    def option_classification_for(
        self,
        account_number: str,
        financial_year: int,
    ) -> RobinhoodOptionClassificationConfig | None:
        """Return the exact reviewed account/FY option classification."""

        return next(
            (
                item
                for item in self.option_classifications
                if item.account_number == account_number and item.financial_year == financial_year
            ),
            None,
        )

    def stock_lending_fee_fsi_source_for(
        self,
        account_number: str,
        financial_year: int,
    ) -> RobinhoodStockLendingFeeFsiSource | None:
        """Return the exact reviewed account/FY loan-fee FSI source."""

        return next(
            (
                item
                for item in self.stock_lending_fee_fsi_sources
                if item.account_number == account_number and item.financial_year == financial_year
            ),
            None,
        )


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodSecurityConfig:
    """One filing-critical Robinhood security identity.

    ``sub_category`` and ``asset_class`` gate identity resolution and CG scope;
    no other source has them. ``sub_category`` is exactly ``"COMMON"`` or
    ``"ETF"``, the two strings the entity routes match.
    """

    instrument: InstrumentIdentity
    cusip: str
    sub_category: str
    asset_class: str
    is_depositary_receipt: bool
    source: str


def effective_anonymisation_flags(
    config: UserConfig,
    *,
    allow_masked_account: bool,
    accept_anonymised_statement: bool,
) -> tuple[bool, bool]:
    """Merge the CLI anonymisation flags with their ``[account]`` opt-ins.

    Returns ``(allow_masked_account, accept_anonymised_statement)`` where
    either source enables the behaviour (CLI flag OR config setting). Lets a
    repeat user with a masked / anonymised statement set the opt-in once in
    ``user.toml`` instead of passing ``--allow-masked-account`` /
    ``--accept-anonymised-statement`` on every run; the loud per-schedule
    warnings still fire regardless of source.
    """
    return (
        allow_masked_account or config.account.allow_masked_account,
        accept_anonymised_statement or config.account.accept_anonymised_statement,
    )


@dataclass(frozen=True, slots=True)
class NetBenefitsAccountConfig:
    """``[netbenefits.account]`` — the Fidelity Stock Plan Services custodial
    account identity for Schedule FA **Table A2**, plus the Quicko withholding
    agent name (ADR-0019), which is not part of that identity.

    Fidelity's observed documents expose the Participant Number as the only
    cross-document account identifier. It lives on :class:`NetBenefitsConfig`,
    not here. The opening date is config-sourced and never inferred from the
    first purchase date (ADR-0003 decision 6).

    Attributes:
        institution_name: Custodial institution legal name (e.g. ``"Fidelity
            Stock Plan Services LLC"``).
        address: Institution address (free text).
        zip_code: Institution ZIP / postal code; may be empty. The CSV
            formats then leave the cell blank, the run names it as a cell to
            fill by hand and exits 3, and ``--format cbdt-json-fragment``
            reports it as a gap (both CBDT schemas require a ZIP).
        country_iso2: ISO 3166-1 alpha-2 of the INSTITUTION's country; resolves
            in the CBDT country table, never ``"IN"`` (A2 is a FOREIGN account).
        account_opening_date: The account opening date, as filed. Required.
        status: Ownership status (mapped to the A2 token by
            ``compute/fa_a2.py``; ``SigningAuthority`` is refused at load).
        withholding_agent_name: Filed as Quicko's "Name of the deductor": the
            withholding agent copied from Form 1042-S. ``None`` when absent; a
            run that writes a Quicko form then leaves that value blank and
            needs manual fill (#1327).
    """

    institution_name: str
    address: str
    zip_code: str
    country_iso2: str
    account_opening_date: _dt.date
    status: TaxpayerStatus
    withholding_agent_name: str | None = None

    def to_account_identity(self, *, participant_no: str) -> AccountIdentity:
        """Return the Table A2 identity. The account number is the Participant
        Number, which is cross-checked against the statements, so they never
        mask it."""
        return AccountIdentity(
            institution=InstitutionIdentity(
                name=self.institution_name,
                address=self.address,
                zip_code=self.zip_code,
                country_iso2=self.country_iso2,
            ),
            account_ref=participant_no,
            opening_date=self.account_opening_date,
            status=self.status,
            base_currency="USD",
            statement_masked_account_ref=False,
        )


@dataclass(frozen=True, slots=True)
class NetBenefitsSecurityConfig:
    """``[netbenefits.security]`` — the ESPP holding's identity for Schedule FA
    **Table A3** and the lot fields no Fidelity file supplies.

    ADR-0003 decision 5: ``listing_exchange`` / ``issuer_country_code`` are
    config-sourced known constants, never fabricated defaults.

    Attributes:
        instrument: Symbol, ISIN, listing exchange and issuer country. The ISIN
            must equal ``"US" + cusip + check-digit`` (cross-checked so a typo
            can't file the wrong security).
        cusip: 9-character CUSIP (e.g. ``"594918104"``).
        entity_address: Issuer address for the A3 "Address of entity" cell.
        entity_zip: Issuer ZIP / postal code; may be empty.
        nature_of_entity: A3 "Nature of entity" free text.
    """

    instrument: InstrumentIdentity
    cusip: str
    entity_address: str
    entity_zip: str
    nature_of_entity: str


@dataclass(frozen=True, slots=True)
class NetBenefitsPriceOverride:
    """One ``[[netbenefits.price_overrides]]`` entry — a manually-supplied,
    USD per-share price for a date the automatic price lookup can't cover
    (e.g. the year-end lot the open-lots CSV leaves unpriced).

    A filed figure, so it carries full provenance: a partial entry is refused.
    USD-only (ADR-0003: Fidelity/MSFT is USD-denominated); the currency is
    encoded in the field name rather than a free-text field that could be
    mis-set.

    Attributes:
        price_date: The trading date the price is for (unique per config). A
            Dec-31 close override must be dated on the last US market session
            on or before Dec 31.
        price_per_share_usd: The price per share in USD (> 0).
        source: Where the figure came from (e.g. ``"SPS year-end statement"``).
        reason: Why a manual override is needed (audit trail).
    """

    price_date: _dt.date
    price_per_share_usd: Decimal
    source: str
    reason: str


@dataclass(frozen=True, slots=True)
class NetBenefitsCgFmvValue:
    """Historical INR FMV for one ESPP purchase under section 49(2AA).

    ``section17_gross_fmv_total_inr`` is the gross FMV taken into account for
    section 17(2)(vi), not the smaller taxable perquisite after subtracting the
    employee contribution.
    """

    purchase_date: _dt.date
    section17_gross_fmv_total_inr: Decimal
    source: str


@dataclass(frozen=True, slots=True)
class NetBenefitsA2PeakOverride:
    """One ``[[netbenefits.a2_peak_overrides]]`` entry — the manual Schedule FA
    Table A2 peak balance for a calendar year.

    The monthly Fidelity statements carry no daily account NAV, so the highest
    single-day total (securities + cash, per Rule 114F) cannot be derived — the
    user supplies it here to complete the CBDT JSON fragment (the review CSV
    leaves the peak as a manual-fill column). A filed figure, so whole rupees.

    Attributes:
        calendar_year: The Schedule FA calendar year this peak is for (unique).
        peak_balance_inr: The native-currency peak converted at its date's
            TTBR, in whole rupees.
    """

    calendar_year: int
    peak_balance_inr: int


@dataclass(frozen=True, slots=True)
class NetBenefitsConfig:
    """Optional ``[netbenefits]`` table — Fidelity NetBenefits (ESPP) support.

    Attributes:
        account: The A2 custodial-account identity.
        security: The A3 holding identity + ``OpenLot`` constants.
        espp_discount_pct: The ESPP discount percent (> 0, <= 100).
        participant_no: NetBenefits Participant No. Cross-checked against
            statements/confirmations and used as the sole Schedule FA A2
            account identifier.
        price_overrides: Manual USD per-share price overrides, keyed by date.
        cg_fmv_values: Historical gross INR FMVs used for section 17(2)(vi),
            keyed by purchase date. Required only for an in-FY sale.
        a2_peak_overrides: Manual Schedule FA Table A2 peak balances, keyed by
            calendar year — the one figure the statements can't derive; consumed
            only by the ``cbdt-json-fragment`` output.
        a2_peak_overrides_participant_no: Must equal ``participant_no`` when any
            ``[[netbenefits.a2_peak_overrides]]`` are present — binds the manual
            peak to this participant so it can't silently carry across a copied
            config.
    """

    account: NetBenefitsAccountConfig
    security: NetBenefitsSecurityConfig
    espp_discount_pct: Decimal
    participant_no: str
    price_overrides: tuple[NetBenefitsPriceOverride, ...] = field(default_factory=tuple)
    cg_fmv_values: tuple[NetBenefitsCgFmvValue, ...] = field(default_factory=tuple)
    a2_peak_overrides: tuple[NetBenefitsA2PeakOverride, ...] = field(default_factory=tuple)
    a2_peak_overrides_participant_no: str | None = None

    def cg_fmv_for(self, purchase_date: _dt.date) -> NetBenefitsCgFmvValue | None:
        """The section 49(2AA) INR FMV for ``purchase_date``, if configured."""
        for entry in self.cg_fmv_values:
            if entry.purchase_date == purchase_date:
                return entry
        return None

    def a2_peak_for(self, calendar_year: int) -> NetBenefitsA2PeakOverride | None:
        """The A2 peak override for ``calendar_year``, or ``None`` if not configured."""
        for entry in self.a2_peak_overrides:
            if entry.calendar_year == calendar_year:
                return entry
        return None


@dataclass(frozen=True, slots=True)
class StockplanSecurityConfig:
    """``[stockplan.security]`` — the holding's identity stamped onto every
    dividend / withholding parsed from a Morgan Stanley StockPlan (Shareworks)
    report. The ISIN is not present in any StockPlan export, so it is configured
    here (and its own check digit validated so a typo can't file the wrong
    security). ``symbol`` is cross-checked against the report by the parser.

    ``symbol`` / ``isin`` are all ``stockplan-income`` needs. The remaining
    fields are OPTIONAL and only required by ``stockplan-fa`` (Schedule FA Table
    A3); an income-only config omits them (they default to empty, and
    ``instrument`` to ``None``).

    Attributes:
        symbol: Trading symbol (e.g. ``"GOOG"``).
        isin: ISO 6166 ISIN (e.g. ``"US02079K1079"`` for Alphabet Class C).
        entity_address: Issuer address for the A3 "Address of entity" cell (FA).
        entity_zip: Issuer ZIP / postal code; may be empty (FA).
        nature_of_entity: A3 "Nature of entity" free text (FA).
        instrument: The same symbol and ISIN plus the listing exchange (also
            expected after the symbol in the Holdings ``Fund Name``, e.g.
            ``"NASDAQ"`` in ``"GOOG - NASDAQ"``) and the issuer country. Set
            only when every Schedule FA field is configured (FA).
    """

    symbol: str
    isin: Isin
    entity_name: str = ""
    entity_address: str = ""
    entity_zip: str = ""
    nature_of_entity: str = ""
    instrument: InstrumentIdentity | None = None

    def fa_fields_present(self) -> bool:
        """True if every Schedule FA field (all but ``entity_zip``) is set."""
        return self.instrument is not None and all(
            (self.entity_name, self.entity_address, self.nature_of_entity)
        )


@dataclass(frozen=True, slots=True)
class StockplanConfig:
    """Optional ``[stockplan]`` table — Morgan Stanley StockPlan (Shareworks)
    support, e.g. Google/Alphabet GSUs (RSUs). (#365)

    Attributes:
        security: The holding's identity (income + optional Schedule FA fields).
        opening_date: Optional StockPlan account opening date. When set, the
            Capital Gain/Loss report must cover the period from this date;
            when absent, the whole period. Never inferred, and never taken
            from ``[account].opening_date``. (#600)
    """

    security: StockplanSecurityConfig
    opening_date: _dt.date | None = None


@dataclass(frozen=True, slots=True, repr=False)
class FidelityCmaEligibleTax:
    """Final non-refundable US tax allocated to one withholding event."""

    payment_date: _dt.date
    amount_usd: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class FidelityCmaUsTaxYearConfig:
    """Runtime US refund/finality choice for one calendar tax year."""

    tax_year: int
    refund_status: FidelityCmaRefundStatus
    eligible_tax: tuple[FidelityCmaEligibleTax, ...] = ()
    provenance: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class FidelityCmaConfig:
    """Optional CMA source identity, US-tax finality, and A2 peak attestation."""

    account_id: str
    spaxx_isin: Isin
    us_tax_years: tuple[FidelityCmaUsTaxYearConfig, ...] = ()
    suppress_peak_balance_warning: bool = False
    peak_balance_bound: FidelityCmaPeakBalanceBound | None = None
    status: TaxpayerStatus | None = None

    def us_tax_year(self, tax_year: int) -> FidelityCmaUsTaxYearConfig:
        for entry in self.us_tax_years:
            if entry.tax_year == tax_year:
                return entry
        return FidelityCmaUsTaxYearConfig(
            tax_year=tax_year,
            refund_status=FidelityCmaRefundStatus.UNKNOWN,
        )


@dataclass(frozen=True, slots=True)
class UserConfig:
    """Top-level ``user.toml`` shape."""

    account: AccountConfig
    fx: FxConfig
    os: OsConfig
    form67: Form67Config
    prices: PricesConfig
    fa: FaConfig
    schedule_fa: ScheduleFaConfig
    netbenefits: NetBenefitsConfig | None = None
    stockplan: StockplanConfig | None = None
    fidelity_cma: FidelityCmaConfig | None = None
    robinhood: RobinhoodConfig | None = None


_KNOWN_TOP_LEVEL = frozenset(
    {
        "account",
        "fx",
        "os",
        "form67",
        "prices",
        "fa",
        "schedule_fa",
        "netbenefits",
        "stockplan",
        "fidelity_cma",
        "robinhood",
    }
)
_REQUIRED_ACCOUNT = frozenset({"holder_name"})
_OPTIONAL_ACCOUNT = frozenset(
    {
        "number",
        "opening_date",
        "status",
        "us_person",
        "allow_masked_account",
        "accept_anonymised_statement",
        "foreign_tins",
        "custodian",
    }
)
_KNOWN_ACCOUNT = _REQUIRED_ACCOUNT | _OPTIONAL_ACCOUNT
_KNOWN_CUSTODIAN = frozenset({"institution_name", "address", "zip", "country_iso2"})
_KNOWN_FX = frozenset({"max_carry_forward_days"})
_KNOWN_OS = frozenset({"schedule_os_indian_adr_routing"})
_KNOWN_FORM67 = frozenset(
    {
        "tax_payable_overrides",
        "section_91",
    }
)
_KNOWN_FORM67_OVERRIDE = frozenset({"country", "source", "tax_payable_normal_inr"})
# [form67] keys Form 67 no longer reads: it doesn't derive Indian tax payable
# from a rate, regime or threshold. Refused by name, not as an unknown key.
_REMOVED_FORM67_KEYS: Final[tuple[str, ...]] = (
    "default_marginal_rate_percent",
    "tax_regime",
    "income_over_threshold",
)
_KNOWN_FORM67_SECTION91 = frozenset({"total_indian_tax_before_91_relief_inr", "total_income_inr"})
_KNOWN_PRICES = frozenset({"massive_api_key_file", "ticker_overrides"})
_KNOWN_TICKER_OVERRIDE = frozenset({"ibkr_ticker", "isin", "yahoo_symbol"})
_KNOWN_FA = frozenset({"entities"})
_KNOWN_FA_ENTITY = frozenset(
    {
        "isin",
        "address",
        "zip",
        "name",
        "nature_of_entity",
        "prefer_configured_address",
        "prefer_configured_nature",
        "prefer_configured_name",
    }
)
_KNOWN_SCHEDULE_FA = frozenset(
    {"manual_values_account_number", "manual_values", "peak_basis", "peak_currency"}
)
_KNOWN_SCHEDULE_FA_MANUAL_VALUE = frozenset({"calendar_year", "a2_peak_balance_inr"})
_KNOWN_NETBENEFITS = frozenset(
    {
        "account",
        "security",
        "espp_discount_pct",
        "participant_no",
        "price_overrides",
        "cg_fmv_values",
        "a2_peak_overrides",
        "a2_peak_overrides_participant_no",
    }
)
_KNOWN_NETBENEFITS_ACCOUNT = frozenset(
    {
        "institution_name",
        "address",
        "zip",
        "country_iso2",
        "account_opening_date",
        "status",
        "withholding_agent_name",
    }
)
_KNOWN_NETBENEFITS_SECURITY = frozenset(
    {
        "symbol",
        "cusip",
        "isin",
        "entity_address",
        "entity_zip",
        "listing_exchange",
        "issuer_country_code",
        "nature_of_entity",
    }
)
_KNOWN_NETBENEFITS_PRICE_OVERRIDE = frozenset(
    {"price_date", "price_per_share_usd", "source", "reason"}
)
_KNOWN_NETBENEFITS_CG_FMV_VALUE = frozenset(
    {"purchase_date", "section17_gross_fmv_total_inr", "source"}
)
_KNOWN_NETBENEFITS_A2_PEAK_OVERRIDE = frozenset({"calendar_year", "peak_balance_inr"})
_KNOWN_STOCKPLAN = frozenset({"security", "opening_date"})
_KNOWN_FIDELITY_CMA = frozenset(
    {
        "account_id",
        "spaxx_isin",
        "us_tax_years",
        "suppress_peak_balance_warning",
        "peak_balance_bound",
        "status",
    }
)
_KNOWN_FIDELITY_CMA_US_TAX_YEAR = frozenset(
    {"tax_year", "refund_status", "eligible_tax", "provenance"}
)
_KNOWN_FIDELITY_CMA_ELIGIBLE_TAX = frozenset({"payment_date", "amount_usd"})
_KNOWN_ROBINHOOD = frozenset(
    {
        "accounts",
        "securities",
        "security_receipt_resolutions",
        "option_classifications",
        "stock_lending_fee_fsi_sources",
        "interest_payer_countries",
        "reports",
        "corrected_statements",
        "sec_user_agent",
        "cusip_transitions_file",
    }
)
_KNOWN_ROBINHOOD_ACCOUNT = frozenset(
    {
        "number",
        "opening_date",
        "status",
        "institution_name",
        "address",
        "zip",
        "country_iso2",
        "same_beneficial_owner",
        "source",
    }
)
_ROBINHOOD_ACCOUNT_COUNT: Final[int] = 2
_KNOWN_ROBINHOOD_SECURITY = frozenset(
    {
        "symbol",
        "cusip",
        "isin",
        "listing_exchange",
        "sub_category",
        "asset_class",
        "issuer_country_code",
        "is_depositary_receipt",
        "source",
    }
)
_KNOWN_ROBINHOOD_SECURITY_RECEIPT_RESOLUTION = frozenset(
    {
        "account_number",
        "source_record_index",
        "cusip",
        "treatment",
        "exact_quantity",
        "reward_fair_market_value_usd",
        "income_reported_financial_year",
        "income_reported",
        "source",
        "reason",
    }
)
_ROBINHOOD_OPTION_BUSINESS_FIELDS = frozenset(
    {
        "books_maintained",
        "other_speculative_business",
        "nature_of_business_code",
        "turnover_basis",
        "external_expenses_inr",
        "bp_additions_inr",
        "bp_deductions_inr",
    }
)
_ROBINHOOD_OPTION_BUSINESS_CODES = frozenset({"21010"})
_REQUIRED_ROBINHOOD_OPTION_CLASSIFICATION = frozenset(
    {
        "account_number",
        "financial_year",
        "fema_risk_acknowledged",
        "treatment",
        "source",
        "reason",
    }
)
_KNOWN_ROBINHOOD_OPTION_CLASSIFICATION = (
    _REQUIRED_ROBINHOOD_OPTION_CLASSIFICATION | _ROBINHOOD_OPTION_BUSINESS_FIELDS
)
_KNOWN_ROBINHOOD_STOCK_LENDING_FEE_FSI_SOURCE = frozenset(
    {"account_number", "financial_year", "country_iso2", "source", "reason"}
)
_KNOWN_ROBINHOOD_INTEREST_PAYER_COUNTRY = frozenset(
    {"account_number", "financial_year", "country_iso2", "interest_kinds", "source", "reason"}
)
_KNOWN_ROBINHOOD_REPORT = frozenset({"file", "start_date", "end_date", "source"})
_KNOWN_ROBINHOOD_CORRECTED_STATEMENT = frozenset({"selected_file", "supersedes", "source"})
_ROBINHOOD_SYMBOL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9.-]*$")
_ROBINHOOD_REPLACE_PLACEHOLDER: Final[str] = "REPLACE"
# The entity routes and the Schedule CG filing position match these exact
# strings; a typo can silently take the direct GLEIF route (#930).
_ROBINHOOD_SECURITY_CLASSES: Final[tuple[str, ...]] = ("COMMON", "ETF")
_ROBINHOOD_MIN_FINANCIAL_YEAR: Final[int] = 2000
_ROBINHOOD_MAX_FINANCIAL_YEAR: Final[int] = 2100
_FIDELITY_CMA_MIN_TAX_YEAR: Final[int] = 2000
_FIDELITY_CMA_MAX_TAX_YEAR: Final[int] = 2100
_FIDELITY_CMA_CENT_EXPONENT: Final[int] = -2
_KNOWN_STOCKPLAN_SECURITY = frozenset(
    {
        "symbol",
        "isin",
        "entity_name",
        "entity_address",
        "entity_zip",
        "listing_exchange",
        "issuer_country_code",
        "nature_of_entity",
    }
)
# Security keys the lot models no longer have (#629). A config that still
# sets one is refused with a pointer, rather than as an unknown field.
_REMOVED_LOT_SECURITY_KEYS: Final[tuple[str, ...]] = ("asset_class", "sub_category")

# CBDT ITR-2 ScheduleFA integer-balance bounds (schema min/max on the A2/A3
# PeakBalanceDuringPeriod / ClosingBalance integer fields) are
# ±CBDT_AMOUNT_BOUND. A manual figure outside this range can never produce a
# schema-valid fragment, so reject it at config time.

_FA_A3_NATURE_MAX_LENGTH: Final[int] = 34

# Plausible Schedule FA calendar-year bounds (sanity guard on the manual-value
# key; the run's --calendar-year is validated separately).
_SCHEDULE_FA_CY_MIN: Final[int] = 2000
_SCHEDULE_FA_CY_MAX: Final[int] = 2100

# ISO 3166-1 alpha-2 country codes are exactly 2 characters.
_ISO_ALPHA2_LENGTH = 2
_INDIA_ISO_ALPHA2: Final[str] = "IN"
_FOREIGN_TIN_MAX_LENGTH: Final[int] = 75

# Upper bound for percentage fields parsed with ``require_at_most_100``:
# a percentage above 100% is a user error.
_MAX_RATE_PERCENT = Decimal("100")

_UNKNOWN_COUNTRY_CODE: Final[str] = "unknown country code (not in the ITR country list)"


def _ensure(condition: bool, field: str, message: str) -> None:
    if not condition:
        raise ConfigError(field=field, message=message)


def _member[E: StrEnum](enum: type[E], raw: str, field: str, message: str) -> E:
    """``enum``'s member whose value is ``raw``, else a refusal. Checked before
    the lookup, because a failed lookup's ``ValueError`` quotes ``raw`` and would
    stay on the refusal as its context (#1210)."""
    _ensure(raw in enum, field, message)
    return enum(raw)


def _refuse_control_character(value: str, field: str) -> None:
    """Refuse a filed text value with a control character (#818), the rule the
    CBDT JSON builder and ``ibkr-fa merge`` apply (#798). The message gives the
    position but never the value or the character (#1045)."""
    found = first_control_character(value)
    if found is not None:
        index, _char = found
        raise ConfigError(
            field=field,
            message=(
                f"has a control character at position {index + 1}; "
                "filed text may not contain control characters"
            ),
        )


def _check_keys(table: dict[str, object], allowed: frozenset[str], path: str) -> None:
    """Refuse a key outside ``allowed``. The error names the table and lists the
    allowed keys, never the unknown key, which may be a pasted value (#1045)."""
    _ensure(
        allowed.issuperset(table),
        path,
        f"has an unknown key; allowed keys: {', '.join(sorted(allowed))}",
    )


def _require_fields(table: dict[str, object], required: Iterable[str], path: str) -> None:
    """Refuse ``table`` if any name in ``required`` is missing. The error names
    the first missing name in sorted order and, when several are missing, lists
    them all sorted, so it never depends on a set's hash-seed order (#1442)."""
    missing = sorted(name for name in required if name not in table)
    if missing:
        message = "missing required field"
        if len(missing) > 1:
            message = f"missing required fields: {', '.join(missing)}"
        raise ConfigError(field=f"{path}.{missing[0]}", message=message)


def _require_bounded_int(
    raw: object, field: str, type_message: str, low: int, high: int, *, bound_note: str = ""
) -> int:
    """``raw`` as an integer from ``low`` to ``high``, else a refusal: first
    ``type_message`` for a non-integer, then the range. A bool is an int
    subclass and is refused too, so a TOML ``true`` never becomes 1."""
    _ensure(isinstance(raw, int) and not isinstance(raw, bool), field, type_message)
    assert isinstance(raw, int)
    _ensure(low <= raw <= high, field, f"must be between {low} and {high}{bound_note}")
    return raw


def _require_cusip(cusip: str, field: str, shape_message: str, check_digit_message: str) -> None:
    """Refuse a CUSIP whose shape, then whose check digit, is wrong."""
    _ensure(CUSIP_PATTERN.fullmatch(cusip) is not None, field, shape_message)
    _ensure(cusip_check_digit_matches(cusip), field, check_digit_message)


def _require_isin_shape(
    isin: str,
    field: str,
    message: str = (
        "must be a valid ISO 6166 ISIN (2-letter country + 9 alphanumerics + check digit)"
    ),
) -> None:
    """Refuse an ISIN whose ISO 6166 shape is wrong."""
    _ensure(_ISIN_PATTERN.fullmatch(isin) is not None, field, message)


def _require_isin(isin: str, field: str) -> None:
    """Refuse an ISIN whose ISO 6166 shape or check digit is wrong."""
    _require_isin_shape(isin, field)
    _ensure(isin_check_digit(isin[:11]) == isin[11], field, "invalid check digit")


def _refuse_retired_keys(
    table: dict[str, object],
    retired: tuple[str, ...],
    path: str,
    status: str = "no longer used",
    reason: str = "",
) -> None:
    """Refuse ``table`` if it sets a key in ``retired``. The error names the
    first in ``retired`` order and, when several are set, lists them all, says
    to delete them so one edit clears it, then appends ``reason`` (#1511,
    #1518). It names keys only, never a configured value."""
    present = [key for key in retired if key in table]
    if present:
        message = f"is {status}; delete it from [{path}]"
        if len(present) > 1:
            message = f"keys {status}: {', '.join(present)}; delete them from [{path}]"
        raise ConfigError(field=f"{path}.{present[0]}", message=message + reason)


def _optional_table(raw: dict[str, object], key: str) -> dict[str, Any] | None:
    """Return ``raw[key]`` as a dict, or ``None`` when absent; refuse a non-table."""
    value = raw.get(key)
    if value is None:
        return None
    _ensure(isinstance(value, dict), key, "must be a TOML table")
    return cast("dict[str, Any]", value)


def _parse_account(table: dict[str, object]) -> AccountConfig:
    _check_keys(table, _KNOWN_ACCOUNT, "account")
    _require_fields(table, _REQUIRED_ACCOUNT, "account")

    number_raw = table.get("number")
    number: AccountId | None = None
    if number_raw is not None:
        _ensure(isinstance(number_raw, str), "account.number", "must be a string")
        assert isinstance(number_raw, str)  # narrow for ty
        _refuse_control_character(number_raw, "account.number")
        _ensure(bool(number_raw), "account.number", "must be non-empty")
        _ensure(number_raw.isascii(), "account.number", "must be ASCII")
        # #141 T3 P2: refuse a masked account-number literal in user.toml.
        _ensure(
            not is_masked_account_id(number_raw),
            "account.number",
            (
                "must be a real IBKR account ID, not the IBKR export mask "
                "(e.g. 'U**********'). Putting the mask string here would "
                "defeat the safety net: --accept-anonymised-statement uses "
                "this value as fallback when the statement omits the Account "
                "row (#141), and a masked fallback combined with "
                "--allow-masked-account would let a run proceed with no "
                "real account ID anywhere."
            ),
        )
        number = AccountId(number_raw)

    holder = table["holder_name"]
    _ensure(isinstance(holder, str), "account.holder_name", "must be a string")
    assert isinstance(holder, str)
    _ensure(bool(holder.strip()), "account.holder_name", "must be non-empty")

    opening = (
        _parse_iso_date(table["opening_date"], "account.opening_date")
        if "opening_date" in table
        else None
    )
    status = (
        _parse_taxpayer_status(table["status"], "account.status") if "status" in table else None
    )

    # Optional [account].us_person flag (issue #145). Absent → False
    # (the W-8BEN / non-US-person default). isinstance check guards
    # against `bool` being a subclass of `int` in Python: a TOML
    # integer literal like `us_person = 1` must NOT be silently
    # coerced to True, because that's the exact silent-typo class
    # the rest of this loader is designed to catch.
    us_person_raw = table.get("us_person", False)
    _ensure(
        isinstance(us_person_raw, bool),
        "account.us_person",
        "must be a boolean (true or false)",
    )
    assert isinstance(us_person_raw, bool)

    # Optional persistent opt-ins mirroring the --allow-masked-account /
    # --accept-anonymised-statement CLI flags (either source enables the
    # behaviour). Same strict-boolean check as us_person so a `= 1` typo is
    # rejected rather than silently truthy.
    allow_masked_raw = table.get("allow_masked_account", False)
    _ensure(
        isinstance(allow_masked_raw, bool),
        "account.allow_masked_account",
        "must be a boolean (true or false)",
    )
    assert isinstance(allow_masked_raw, bool)
    accept_anon_raw = table.get("accept_anonymised_statement", False)
    _ensure(
        isinstance(accept_anon_raw, bool),
        "account.accept_anonymised_statement",
        "must be a boolean (true or false)",
    )
    assert isinstance(accept_anon_raw, bool)

    foreign_tins = _parse_foreign_tins(table.get("foreign_tins"))
    custodian = _parse_custodian(table.get("custodian"))

    return AccountConfig(
        number=number,
        holder_name=holder,
        opening_date=opening,
        status=status,
        us_person=us_person_raw,
        allow_masked_account=allow_masked_raw,
        accept_anonymised_statement=accept_anon_raw,
        foreign_tins=foreign_tins,
        custodian=custodian,
    )


def _parse_foreign_tins(raw: object) -> dict[str, str]:
    """Parse the optional ``[account.foreign_tins]`` ISO-alpha-2 → TIN map.

    Returns an empty dict when absent. Each key must be a 2-letter uppercase
    ISO 3166-1 alpha-2 code in the ITR country list (``GB``, not ``UK``),
    because rows only take a TIN by exact code match; never ``"IN"`` —
    Schedule FSI / TR are foreign-source only, so an Indian TIN here would
    never match a row and signals user confusion. Each value must match the
    filing schema's non-empty ASCII string limit. When a country has not
    allotted a TIN, the Income Tax Department instructs the filer to use
    their passport number.
    """
    if raw is None:
        return {}
    _ensure(isinstance(raw, dict), "account.foreign_tins", "must be a TOML table")
    assert isinstance(raw, dict)
    out: dict[str, str] = {}
    # A key may be a TIN pasted in the wrong place, so the path names the
    # entry by its 0-based position in file order, never by its key (#1045).
    for index, (key, value) in enumerate(raw.items()):
        field_path = f"account.foreign_tins[{index}]"
        country = _require_country_iso2(key, field_path, forbid_india=False)
        _ensure(
            country != _INDIA_ISO_ALPHA2,
            field_path,
            (
                "must not be 'IN' — Schedule FSI / TR report foreign-source "
                "income only, so an Indian TIN never applies here"
            ),
        )
        _ensure(isinstance(value, str), field_path, "TIN must be a string")
        assert isinstance(value, str)
        _refuse_control_character(value, field_path)
        if value == "NA":
            # Backward compatibility for the placeholder documented before
            # passport fallback was supported.  Treat it as missing; never
            # stamp it into a filing artifact.
            continue
        _ensure(value.strip() != "", field_path, "TIN must be non-empty")
        _ensure(value.isascii(), field_path, "TIN must be ASCII")
        _ensure(
            len(value) <= _FOREIGN_TIN_MAX_LENGTH,
            field_path,
            f"TIN must be at most {_FOREIGN_TIN_MAX_LENGTH} characters",
        )
        _ensure(
            value.strip().upper() != "NA",
            field_path,
            "use your passport number when no TIN was allotted; 'NA' is not a TIN",
        )
        out[country] = value
    return out


def _parse_custodian(raw: object) -> CustodianConfig | None:
    """Parse the optional ``[account.custodian]`` sub-table for Schedule FA A2.

    Returns ``None`` when absent. ``institution_name`` + ``address`` +
    ``country_iso2`` are required; ``zip`` is optional (may be empty).
    ``country_iso2`` must be a 2-letter uppercase ISO 3166-1 alpha-2 code that
    resolves in the CBDT country table and is NOT ``"IN"`` (Table A2 is a
    foreign custodial account).
    """
    if raw is None:
        return None
    _ensure(isinstance(raw, dict), "account.custodian", "must be a TOML table")
    table = cast("dict[str, Any]", raw)
    _check_keys(table, _KNOWN_CUSTODIAN, "account.custodian")
    _require_fields(table, _KNOWN_CUSTODIAN - {"zip"}, "account.custodian")

    name = table.get("institution_name")
    _ensure(
        isinstance(name, str), "account.custodian.institution_name", "must be a non-empty string"
    )
    assert isinstance(name, str)
    _refuse_control_character(name, "account.custodian.institution_name")
    _ensure(name.strip() != "", "account.custodian.institution_name", "must be a non-empty string")

    address = table.get("address")
    _ensure(isinstance(address, str), "account.custodian.address", "must be a non-empty string")
    assert isinstance(address, str)
    _refuse_control_character(address, "account.custodian.address")
    _ensure(address.strip() != "", "account.custodian.address", "must be a non-empty string")

    zip_raw = table.get("zip", "")
    _ensure(isinstance(zip_raw, str), "account.custodian.zip", "must be a string")
    assert isinstance(zip_raw, str)
    _refuse_control_character(zip_raw, "account.custodian.zip")

    country = _require_country_iso2(
        table.get("country_iso2"),
        "account.custodian.country_iso2",
        forbid_india=True,
        india_message="must not be 'IN' — Table A2 is a FOREIGN custodial account",
    )
    return CustodianConfig(
        institution_name=name,
        address=address,
        zip_code=zip_raw,
        country_iso2=country,
    )


def _parse_fx(table: dict[str, object] | None) -> FxConfig:
    default = FxConfig()
    if table is None:
        return default
    _check_keys(table, _KNOWN_FX, "fx")
    raw = table.get("max_carry_forward_days", default.max_carry_forward_days)
    _ensure(
        isinstance(raw, int) and not isinstance(raw, bool),
        "fx.max_carry_forward_days",
        "must be an integer",
    )
    assert isinstance(raw, int)
    _ensure(
        raw >= 0,
        "fx.max_carry_forward_days",
        "must be non-negative",
    )
    return FxConfig(max_carry_forward_days=raw)


def _parse_os(table: dict[str, object] | None) -> OsConfig:
    default = OsConfig()
    if table is None:
        return default
    _check_keys(table, _KNOWN_OS, "os")
    raw = table.get("schedule_os_indian_adr_routing", default.schedule_os_indian_adr_routing)
    _ensure(
        isinstance(raw, str),
        "os.schedule_os_indian_adr_routing",
        "must be a string",
    )
    assert isinstance(raw, str)
    valid = ", ".join(m.value for m in ScheduleOsIndianAdrRoutingMode)
    mode = _member(
        ScheduleOsIndianAdrRoutingMode,
        raw,
        "os.schedule_os_indian_adr_routing",
        f"must be one of: {valid}",
    )
    return OsConfig(schedule_os_indian_adr_routing=mode)


def _parse_decimal_string(
    raw: object,
    field_path: str,
    *,
    kind: NumberKind,
    require_non_negative: bool = True,
    require_at_most_100: bool = False,
) -> Decimal:
    """Parse a Decimal-as-string TOML value with optional bound checks.

    Common helper for Decimal-valued amount and percentage fields. TOML has no
    native Decimal type, so all Decimal-valued fields are accepted
    as strings only (a TOML float ``= 31.2`` would silently lose
    audit-grade precision — Decimal("0.1") + Decimal("0.2") yields
    the audit-correct ``Decimal("0.3")`` but float ``0.1 + 0.2`` is
    ``0.30000000000000004``).

    Args:
        raw: The TOML value to parse. MUST be a string.
        field_path: Dotted field path for ConfigError messages
            (e.g., ``"form67.section_91.total_income_inr"``).
        kind: The kind of number, whose read-time digit limits
            (:mod:`ibkr_fa.number_checks`) apply after the checks below.
        require_non_negative: When True (default), reject Decimals
            ``< 0``.
        require_at_most_100: When True, reject Decimals ``> 100``.
            Used for percentage fields where a value above 100%
            indicates user error.
    """
    _ensure(
        isinstance(raw, str),
        field_path,
        (
            'must be a Decimal-formatted string (e.g., "31.2"); '
            "TOML floats lose audit-grade precision and are rejected "
            "for Decimal-valued fields"
        ),
    )
    assert isinstance(raw, str)
    # A Decimal-as-string is a filed figure: reject surrounding whitespace rather
    # than let `Decimal(" 10 ")` silently normalise it (consistent with the
    # non-empty string fields, which also refuse non-canonical values).
    _ensure(raw == raw.strip(), field_path, "must not have leading or trailing whitespace")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(field=field_path, message="must be a decimal number") from exc
    # Reject NaN / Infinity before any ordering comparison: `Decimal("NaN") >= 0`
    # raises InvalidOperation, which would crash the load instead of refusing
    # with a field-scoped ConfigError (and a NaN/Inf is never a valid figure).
    _ensure(value.is_finite(), field_path, "must be a finite decimal number, not NaN or Infinity")
    if require_non_negative:
        _ensure(value >= 0, field_path, "must be a decimal number of at least 0")
    if require_at_most_100:
        _ensure(
            value <= _MAX_RATE_PERCENT,
            field_path,
            f"must be a decimal number of at most {_MAX_RATE_PERCENT}",
        )
    breach = number_limit_breach(value, kind)
    if breach is not None:
        raise ConfigError(field=field_path, message=breach)
    return value


def _parse_form67_override(table: dict[str, object], index: int) -> Form67TaxPayableOverride:
    """Parse one ``[[form67.tax_payable_overrides]]`` entry."""
    path = f"form67.tax_payable_overrides[{index}]"
    _check_keys(table, _KNOWN_FORM67_OVERRIDE, path)
    _require_fields(table, _KNOWN_FORM67_OVERRIDE, path)

    country = _require_country_iso2(table["country"], f"{path}.country", forbid_india=False)
    _ensure(
        country != _INDIA_ISO_ALPHA2,
        f"{path}.country",
        "must not be 'IN' — Form 67 covers foreign tax only",
    )

    source_raw = table["source"]
    _ensure(isinstance(source_raw, str), f"{path}.source", "must be a string")
    assert isinstance(source_raw, str)
    source = Form67SourceOfIncome.__members__.get(source_raw)
    if source is None:
        valid = ", ".join(m.name for m in Form67SourceOfIncome)
        raise ConfigError(
            field=f"{path}.source",
            message=(f"must be a Form67SourceOfIncome enum NAME (uppercase). Valid names: {valid}"),
        )

    tax_payable = _parse_decimal_string(
        table["tax_payable_normal_inr"],
        f"{path}.tax_payable_normal_inr",
        kind=NumberKind.MONEY,
        require_non_negative=True,
        require_at_most_100=False,
    )

    return Form67TaxPayableOverride(
        iso_alpha2=country,
        source_of_income=source,
        tax_payable_normal_inr=tax_payable,
    )


def _parse_form67_section91(table: dict[str, object]) -> Form67Section91Config:
    """Parse the ``[form67.section_91]`` sub-table.

    Both ``total_indian_tax_before_91_relief_inr`` and
    ``total_income_inr`` are REQUIRED when the sub-table is present
    (the §91 average-rate formula needs both inputs; partial
    population would be a silent-bug foot-gun).
    """
    _check_keys(table, _KNOWN_FORM67_SECTION91, "form67.section_91")
    _require_fields(table, _KNOWN_FORM67_SECTION91, "form67.section_91")

    total_tax = _parse_decimal_string(
        table["total_indian_tax_before_91_relief_inr"],
        "form67.section_91.total_indian_tax_before_91_relief_inr",
        kind=NumberKind.MONEY,
        require_non_negative=True,
        require_at_most_100=False,
    )
    total_income = _parse_decimal_string(
        table["total_income_inr"],
        "form67.section_91.total_income_inr",
        kind=NumberKind.MONEY,
        require_non_negative=True,
        require_at_most_100=False,
    )
    _ensure(
        total_income > 0,
        "form67.section_91.total_income_inr",
        (
            "must be > 0 (the §91 Explanation's Indian average rate "
            "is total_indian_tax / total_income; zero denominator "
            "would make the rate undefined)"
        ),
    )

    return Form67Section91Config(
        total_indian_tax_before_91_relief_inr=total_tax,
        total_income_inr=total_income,
    )


def _parse_form67(table: dict[str, object] | None) -> Form67Config:
    if table is None:
        return Form67Config()
    _refuse_retired_keys(
        table,
        _REMOVED_FORM67_KEYS,
        "form67",
        status="no longer supported",
        reason=(
            ". Form 67 no longer derives Indian tax "
            "payable from a rate, regime or threshold, so rows without an "
            "exact [[form67.tax_payable_overrides]] entry leave the "
            "Indian-tax and FTC columns blank. Put exact source-level "
            "amounts in [[form67.tax_payable_overrides]]; non-DTAA "
            "(section 91) claims also need the [form67.section_91] inputs"
        ),
    )
    _check_keys(table, _KNOWN_FORM67, "form67")

    overrides_raw = table.get("tax_payable_overrides", [])
    _ensure(
        isinstance(overrides_raw, list),
        "form67.tax_payable_overrides",
        "must be a TOML array of tables (use [[form67.tax_payable_overrides]])",
    )
    assert isinstance(overrides_raw, list)
    overrides: list[Form67TaxPayableOverride] = []
    seen_keys: set[tuple[str, Form67SourceOfIncome]] = set()
    for i, ov_raw in enumerate(overrides_raw):
        _ensure(
            isinstance(ov_raw, dict),
            f"form67.tax_payable_overrides[{i}]",
            "must be a TOML table",
        )
        assert isinstance(ov_raw, dict)
        # cast: ty narrows isinstance(_, dict) to dict[Unknown, Unknown]
        # which is invariance-incompatible with dict[str, object]. The
        # TOML library guarantees string keys, so the cast is safe.
        ov_table = cast(dict[str, Any], ov_raw)
        ov = _parse_form67_override(ov_table, i)
        key = (ov.iso_alpha2, ov.source_of_income)
        _ensure(
            key not in seen_keys,
            f"form67.tax_payable_overrides[{i}]",
            "duplicates another override for the same country and source",
        )
        seen_keys.add(key)
        overrides.append(ov)

    section_91_raw = table.get("section_91")
    section_91: Form67Section91Config | None = None
    if section_91_raw is not None:
        _ensure(
            isinstance(section_91_raw, dict),
            "form67.section_91",
            "must be a TOML table",
        )
        assert isinstance(section_91_raw, dict)
        section_91 = _parse_form67_section91(cast(dict[str, Any], section_91_raw))

    return Form67Config(
        tax_payable_overrides=tuple(overrides),
        section_91=section_91,
    )


def _parse_ticker_override(table: dict[str, object], index: int) -> TickerOverride:
    """Parse one ``[[prices.ticker_overrides]]`` entry."""
    path = f"prices.ticker_overrides[{index}]"
    _check_keys(table, _KNOWN_TICKER_OVERRIDE, path)
    _require_fields(table, _KNOWN_TICKER_OVERRIDE, path)

    ibkr_ticker_raw = table["ibkr_ticker"]
    _ensure(isinstance(ibkr_ticker_raw, str), f"{path}.ibkr_ticker", "must be a string")
    assert isinstance(ibkr_ticker_raw, str)
    _ensure(ibkr_ticker_raw != "", f"{path}.ibkr_ticker", "must be non-empty")

    isin_raw = table["isin"]
    _ensure(isinstance(isin_raw, str), f"{path}.isin", "must be a string")
    assert isinstance(isin_raw, str)
    _require_isin(isin_raw, f"{path}.isin")

    yahoo_symbol_raw = table["yahoo_symbol"]
    _ensure(isinstance(yahoo_symbol_raw, str), f"{path}.yahoo_symbol", "must be a string")
    assert isinstance(yahoo_symbol_raw, str)
    _ensure(yahoo_symbol_raw != "", f"{path}.yahoo_symbol", "must be non-empty")

    return TickerOverride(
        ibkr_ticker=Ticker(ibkr_ticker_raw),
        isin=Isin(isin_raw),
        yahoo_symbol=yahoo_symbol_raw,
    )


def _parse_prices(table: dict[str, object] | None) -> PricesConfig:
    if table is None:
        return PricesConfig()
    _check_keys(table, _KNOWN_PRICES, "prices")

    key_file_raw = table.get("massive_api_key_file")
    key_file: str | None = None
    if key_file_raw is not None:
        key_file = _require_nonempty_str(
            key_file_raw,
            "prices.massive_api_key_file",
            ascii_only=True,
        )

    overrides_raw = table.get("ticker_overrides", [])
    _ensure(
        isinstance(overrides_raw, list),
        "prices.ticker_overrides",
        "must be a TOML array of tables (use [[prices.ticker_overrides]])",
    )
    assert isinstance(overrides_raw, list)
    overrides: list[TickerOverride] = []
    seen_tickers: set[str] = set()
    for i, ov_raw in enumerate(overrides_raw):
        _ensure(isinstance(ov_raw, dict), f"prices.ticker_overrides[{i}]", "must be a TOML table")
        assert isinstance(ov_raw, dict)
        ov = _parse_ticker_override(cast(dict[str, Any], ov_raw), i)
        # Reject more than one override per ibkr_ticker. The price fetch
        # is ticker-keyed (windows, cache filename, and the row-builder
        # lookup all key on the IBKR ticker), so two overrides sharing a
        # ticker but differing by ISIN could not both be honoured — the
        # second would silently shadow the first and a lot could be
        # valued against the WRONG security's prices. One override per
        # ticker keeps the config shape honest with the fetch layer; the
        # ISIN still must match the held lot for the override to apply.
        _ensure(
            str(ov.ibkr_ticker) not in seen_tickers,
            f"prices.ticker_overrides[{i}]",
            (
                "duplicates another override's ibkr_ticker; each ibkr_ticker may "
                "appear at most once (the price fetch is ticker-keyed, so two "
                "overrides for one ticker cannot both apply)"
            ),
        )
        seen_tickers.add(str(ov.ibkr_ticker))
        overrides.append(ov)

    return PricesConfig(
        massive_api_key_file=key_file,
        ticker_overrides=tuple(overrides),
    )


def _parse_fa_entity(table: dict[str, object], index: int) -> FaEntityOverride:
    """Parse one ``[[fa.entities]]`` entry."""
    path = f"fa.entities[{index}]"
    _check_keys(table, _KNOWN_FA_ENTITY, path)
    _require_fields(table, ("isin",), path)

    isin_raw = table["isin"]
    _ensure(isinstance(isin_raw, str), f"{path}.isin", "must be a string")
    assert isinstance(isin_raw, str)
    _require_isin(isin_raw, f"{path}.isin")

    # #1449: an entry may leave out address (Robinhood Schedule FA then stops for
    # a held security until the filer verifies an address), but not when it sets zip or
    # prefer_configured_address = true.
    address_raw = table.get("address")
    if address_raw is not None:
        _ensure(isinstance(address_raw, str), f"{path}.address", "must be a string")
        assert isinstance(address_raw, str)
        _refuse_control_character(address_raw, f"{path}.address")
        _ensure(address_raw.strip() != "", f"{path}.address", "must be non-empty")

    zip_raw = table.get("zip", "")
    _ensure(isinstance(zip_raw, str), f"{path}.zip", "must be a string")
    assert isinstance(zip_raw, str)
    _refuse_control_character(zip_raw, f"{path}.zip")
    _ensure(
        "zip" not in table or address_raw is not None,
        f"{path}.zip",
        "set address too: zip is filed only with this entry's address",
    )

    name_raw = table.get("name")
    if name_raw is not None:
        _ensure(isinstance(name_raw, str), f"{path}.name", "must be a string")
        assert isinstance(name_raw, str)
        _refuse_control_character(name_raw, f"{path}.name")
        _ensure(name_raw.strip() != "", f"{path}.name", "must be non-empty when present")

    nature_raw = table.get("nature_of_entity")
    if nature_raw is not None:
        _ensure(
            isinstance(nature_raw, str),
            f"{path}.nature_of_entity",
            "must be a string",
        )
        assert isinstance(nature_raw, str)
        _refuse_control_character(nature_raw, f"{path}.nature_of_entity")
        _ensure(
            nature_raw.strip() != "",
            f"{path}.nature_of_entity",
            "must be non-empty when present",
        )
        _ensure(
            len(nature_raw) <= _FA_A3_NATURE_MAX_LENGTH,
            f"{path}.nature_of_entity",
            f"must be at most {_FA_A3_NATURE_MAX_LENGTH} characters",
        )

    prefer_configured_address = table.get("prefer_configured_address", False)
    _ensure(
        isinstance(prefer_configured_address, bool),
        f"{path}.prefer_configured_address",
        "must be true or false",
    )
    assert isinstance(prefer_configured_address, bool)
    _ensure(
        not prefer_configured_address or address_raw is not None,
        f"{path}.prefer_configured_address",
        "set address too: prefer_configured_address = true keeps this entry's address "
        "instead of an automatic one",
    )
    prefer_configured_nature = table.get("prefer_configured_nature", False)
    _ensure(
        isinstance(prefer_configured_nature, bool),
        f"{path}.prefer_configured_nature",
        "must be true or false",
    )
    assert isinstance(prefer_configured_nature, bool)
    prefer_configured_name = table.get("prefer_configured_name", False)
    _ensure(
        isinstance(prefer_configured_name, bool),
        f"{path}.prefer_configured_name",
        "must be true or false",
    )
    assert isinstance(prefer_configured_name, bool)

    return FaEntityOverride(
        isin=Isin(isin_raw),
        address=address_raw,
        zip_code=zip_raw,
        name=name_raw,
        nature_of_entity=nature_raw,
        prefer_configured_address=prefer_configured_address,
        prefer_configured_nature=prefer_configured_nature,
        prefer_configured_name=prefer_configured_name,
    )


def _parse_fa(table: dict[str, object] | None) -> FaConfig:
    if table is None:
        return FaConfig()
    _check_keys(table, _KNOWN_FA, "fa")

    entities_raw = table.get("entities", [])
    _ensure(
        isinstance(entities_raw, list),
        "fa.entities",
        "must be a TOML array of tables (use [[fa.entities]])",
    )
    assert isinstance(entities_raw, list)
    entities: list[FaEntityOverride] = []
    seen_isins: set[str] = set()
    for i, entry_raw in enumerate(entities_raw):
        _ensure(isinstance(entry_raw, dict), f"fa.entities[{i}]", "must be a TOML table")
        assert isinstance(entry_raw, dict)
        entity = _parse_fa_entity(cast(dict[str, Any], entry_raw), i)
        _ensure(
            str(entity.isin) not in seen_isins,
            f"fa.entities[{i}]",
            "duplicates another override's ISIN; each ISIN may appear at most once",
        )
        seen_isins.add(str(entity.isin))
        entities.append(entity)

    return FaConfig(entities=tuple(entities))


def _parse_schedule_fa_manual_value(table: dict[str, object], index: int) -> ScheduleFaManualValue:
    """Parse one ``[[schedule_fa.manual_values]]`` entry."""
    path = f"schedule_fa.manual_values[{index}]"
    _check_keys(table, _KNOWN_SCHEDULE_FA_MANUAL_VALUE, path)
    _require_fields(table, _KNOWN_SCHEDULE_FA_MANUAL_VALUE, path)

    calendar_year = _require_bounded_int(
        table["calendar_year"],
        f"{path}.calendar_year",
        "must be a TOML integer (e.g. 2025)",
        _SCHEDULE_FA_CY_MIN,
        _SCHEDULE_FA_CY_MAX,
    )
    # Require a TOML integer: a filed tax figure is supplied as whole rupees and
    # is never silently rounded (a TOML float `= 8950.5` is a type error here).
    peak = _require_bounded_int(
        table["a2_peak_balance_inr"],
        f"{path}.a2_peak_balance_inr",
        "must be a whole-rupee TOML integer (a filed figure is not rounded; "
        "a fractional / float value is rejected)",
        -CBDT_AMOUNT_BOUND,
        CBDT_AMOUNT_BOUND,
        bound_note=" (CBDT schema bound)",
    )

    return ScheduleFaManualValue(calendar_year=calendar_year, a2_peak_balance_inr=peak)


def _parse_schedule_fa(table: dict[str, object] | None) -> ScheduleFaConfig:
    default = ScheduleFaConfig()
    if table is None:
        return default
    _check_keys(table, _KNOWN_SCHEDULE_FA, "schedule_fa")

    values_raw = table.get("manual_values", [])
    _ensure(
        isinstance(values_raw, list),
        "schedule_fa.manual_values",
        "must be a TOML array of tables (use [[schedule_fa.manual_values]])",
    )
    assert isinstance(values_raw, list)
    values: list[ScheduleFaManualValue] = []
    seen_years: set[int] = set()
    for i, entry_raw in enumerate(values_raw):
        _ensure(
            isinstance(entry_raw, dict),
            f"schedule_fa.manual_values[{i}]",
            "must be a TOML table",
        )
        assert isinstance(entry_raw, dict)
        entry = _parse_schedule_fa_manual_value(cast(dict[str, Any], entry_raw), i)
        _ensure(
            entry.calendar_year not in seen_years,
            f"schedule_fa.manual_values[{i}]",
            "duplicates another configured year",
        )
        seen_years.add(entry.calendar_year)
        values.append(entry)

    account_number_raw = table.get("manual_values_account_number")
    account_number: str | None = None
    if account_number_raw is not None:
        _ensure(
            isinstance(account_number_raw, str),
            "schedule_fa.manual_values_account_number",
            "must be a string",
        )
        assert isinstance(account_number_raw, str)
        _refuse_control_character(account_number_raw, "schedule_fa.manual_values_account_number")
        _ensure(
            account_number_raw != "" and account_number_raw.isascii(),
            "schedule_fa.manual_values_account_number",
            "must be a non-empty ASCII string",
        )
        _ensure(
            not is_masked_account_id(account_number_raw),
            "schedule_fa.manual_values_account_number",
            "must be the real account number, not the IBKR export mask",
        )
        account_number = account_number_raw

    # Manual values are filed tax data; require the account binding so a peak
    # can't silently carry across a user.toml copied to a different account.
    _ensure(
        not values or account_number is not None,
        "schedule_fa.manual_values_account_number",
        "is required when [[schedule_fa.manual_values]] are present (set it to "
        "your [account].number so the manual figures are bound to this account)",
    )

    peak_basis_raw = table.get("peak_basis", default.peak_basis)
    _ensure(
        peak_basis_raw in ("close", "high"),
        "schedule_fa.peak_basis",
        'must be "close" (daily closing price, the default) or "high" (daily intraday high)',
    )
    peak_basis = cast(PeakBasis, peak_basis_raw)

    peak_currency_raw = table.get("peak_currency", default.peak_currency)
    _ensure(
        peak_currency_raw in ("native", "inr"),
        "schedule_fa.peak_currency",
        'must be "native" (foreign-currency peak, the default) or '
        '"inr" (maximum daily INR-converted value)',
    )
    peak_currency = cast(PeakCurrency, peak_currency_raw)

    return ScheduleFaConfig(
        manual_values_account_number=account_number,
        manual_values=tuple(values),
        peak_basis=peak_basis,
        peak_currency=peak_currency,
    )


def _parse_iso_date(raw: object, field_path: str) -> _dt.date:
    """Parse a TOML date / ISO-date string into a non-future ``date``.

    ``datetime`` subclasses ``date``, so a TOML ``= 2025-01-01T00:00:00`` literal
    would slip past an ``isinstance(date)`` check; reject it explicitly. Every
    date this loader reads (account opening, price date) is historical, so a
    future date is always a user error.
    """
    if isinstance(raw, _dt.datetime):
        raise ConfigError(
            field=field_path,
            message="must be a date (YYYY-MM-DD), not a datetime with time-of-day",
        )
    if isinstance(raw, _dt.date):
        value = raw
    elif isinstance(raw, str):
        try:
            value = _dt.date.fromisoformat(raw)
        except ValueError:
            value = None
        # Refused after the handler: the ValueError quotes ``raw`` (#1210).
        if value is None:
            raise ConfigError(field=field_path, message="must be an ISO date (YYYY-MM-DD)")
    else:
        raise ConfigError(field=field_path, message="must be a TOML date or ISO-format string")
    _ensure(value <= _dt.date.today(), field_path, "must not be in the future")
    return value


def _parse_taxpayer_status(raw: object, field_path: str) -> TaxpayerStatus:
    _ensure(isinstance(raw, str), field_path, "must be a string")
    assert isinstance(raw, str)
    valid = ", ".join(
        member.value for member in TaxpayerStatus if member is not TaxpayerStatus.SIGNING_AUTHORITY
    )
    status = _member(TaxpayerStatus, raw, field_path, f"must be one of: {valid}")
    _ensure(
        status is not TaxpayerStatus.SIGNING_AUTHORITY, field_path, SIGNING_AUTHORITY_UNSUPPORTED
    )
    return status


def _require_nonempty_str(
    raw: object, field_path: str, *, ascii_only: bool = False, filed: bool = False
) -> str:
    """Require a non-empty string with no leading/trailing whitespace.

    Every value this validates is either filed verbatim (a name, address,
    account number) or used for matching (a participant / symbol), so a stray
    surrounding space is always an error — refuse it rather than file or match a
    non-canonical value. Optionally ASCII-only. ``filed`` refuses a control
    character first (#818); audit text such as a ``reason`` leaves it off.
    """
    _ensure(isinstance(raw, str), field_path, "must be a string")
    assert isinstance(raw, str)
    if filed:
        _refuse_control_character(raw, field_path)
    _ensure(raw.strip() != "", field_path, "must be non-empty")
    _ensure(raw == raw.strip(), field_path, "must not have leading or trailing whitespace")
    if ascii_only:
        _ensure(raw.isascii(), field_path, "must be ASCII")
    return raw


def _optional_str(raw: object, field_path: str, *, filed: bool = False) -> str:
    """An optional string (absent -> ``""``); may be empty, never surrounding ws.
    ``filed`` refuses a control character first, as for
    :func:`_require_nonempty_str`."""
    if raw is None:
        return ""
    _ensure(isinstance(raw, str), field_path, "must be a string")
    assert isinstance(raw, str)
    if filed:
        _refuse_control_character(raw, field_path)
    _ensure(raw == raw.strip(), field_path, "must not have leading or trailing whitespace")
    return raw


def _require_country_iso2(
    raw: object, field_path: str, *, forbid_india: bool, india_message: str = "must not be 'IN'"
) -> str:
    """Require a 2-letter uppercase ISO 3166-1 alpha-2 code resolvable in the
    CBDT country table; optionally forbid ``"IN"`` with ``india_message``."""
    _ensure(
        isinstance(raw, str)
        and len(raw) == _ISO_ALPHA2_LENGTH
        and raw.isascii()
        and raw.isalpha()
        and raw.isupper(),
        field_path,
        "must be a 2-letter uppercase ISO 3166-1 alpha-2 code",
    )
    assert isinstance(raw, str)
    if forbid_india:
        _ensure(raw != _INDIA_ISO_ALPHA2, field_path, india_message)
    _ensure(raw in load_country_codes(), field_path, _UNKNOWN_COUNTRY_CODE)
    return raw


def _require_robinhood_account_number(raw: object, field_path: str) -> str:
    value = _require_nonempty_str(raw, field_path, ascii_only=True)
    _ensure(value.isdigit(), field_path, "must contain ASCII digits only")
    return value


def _parse_robinhood_account(raw: object, index: int) -> RobinhoodAccountConfig:
    path = f"robinhood.accounts[{index}]"
    _ensure(isinstance(raw, dict), path, "must be a TOML table")
    table = cast("dict[str, object]", raw)
    _check_keys(table, _KNOWN_ROBINHOOD_ACCOUNT, path)
    _require_fields(table, _KNOWN_ROBINHOOD_ACCOUNT - {"zip"}, path)

    status = _parse_taxpayer_status(table["status"], f"{path}.status")
    same_owner = table["same_beneficial_owner"]
    _ensure(
        isinstance(same_owner, bool),
        f"{path}.same_beneficial_owner",
        "must be a boolean (true or false)",
    )
    assert isinstance(same_owner, bool)
    _ensure(
        same_owner,
        f"{path}.same_beneficial_owner",
        "must be true; ownership-changing transfers are unsupported",
    )
    return RobinhoodAccountConfig(
        number=_require_robinhood_account_number(table["number"], f"{path}.number"),
        opening_date=_parse_iso_date(table["opening_date"], f"{path}.opening_date"),
        status=status,
        institution_name=_require_nonempty_str(
            table["institution_name"], f"{path}.institution_name", filed=True
        ),
        address=_require_nonempty_str(table["address"], f"{path}.address", filed=True),
        zip_code=_optional_str(table.get("zip"), f"{path}.zip", filed=True),
        country_iso2=_require_country_iso2(
            table["country_iso2"], f"{path}.country_iso2", forbid_india=True
        ),
        same_beneficial_owner=same_owner,
        source=_require_nonempty_str(table["source"], f"{path}.source"),
    )


def _parse_robinhood_security(raw: object, index: int) -> RobinhoodSecurityConfig:
    path = f"robinhood.securities[{index}]"
    _ensure(isinstance(raw, dict), path, "must be a TOML table")
    table = cast("dict[str, object]", raw)
    _check_keys(table, _KNOWN_ROBINHOOD_SECURITY, path)
    _require_fields(table, _KNOWN_ROBINHOOD_SECURITY, path)

    symbol = _require_nonempty_str(table["symbol"], f"{path}.symbol", ascii_only=True)
    _ensure(
        _ROBINHOOD_SYMBOL_PATTERN.fullmatch(symbol) is not None,
        f"{path}.symbol",
        "must be an uppercase ASCII trading symbol",
    )
    _ensure(
        symbol != _ROBINHOOD_REPLACE_PLACEHOLDER,
        f"{path}.symbol",
        "must replace the generated placeholder",
    )
    cusip = _require_nonempty_str(table["cusip"], f"{path}.cusip", ascii_only=True)
    _require_cusip(
        cusip,
        f"{path}.cusip",
        "must be a 9-character uppercase-alphanumeric CUSIP",
        "CUSIP check digit is invalid",
    )
    isin_raw = _require_nonempty_str(table["isin"], f"{path}.isin", ascii_only=True)
    _require_isin_shape(isin_raw, f"{path}.isin", "must be a valid ISO 6166 ISIN")
    _ensure(
        isin_check_digit(isin_raw[:11]) == isin_raw[11],
        f"{path}.isin",
        "ISIN check digit is invalid",
    )
    if isin_raw.startswith("US"):
        _ensure(
            isin_raw[2:11] == cusip,
            f"{path}.isin",
            "US ISIN must contain the configured CUSIP",
        )

    listing_exchange = _require_nonempty_str(
        table["listing_exchange"], f"{path}.listing_exchange", ascii_only=True
    )
    sub_category = _require_nonempty_str(
        table["sub_category"], f"{path}.sub_category", ascii_only=True
    )
    asset_class = _require_nonempty_str(
        table["asset_class"], f"{path}.asset_class", ascii_only=True
    )
    issuer_country = _require_country_iso2(
        table["issuer_country_code"],
        f"{path}.issuer_country_code",
        forbid_india=False,
    )
    depositary_raw = table["is_depositary_receipt"]
    _ensure(
        isinstance(depositary_raw, bool),
        f"{path}.is_depositary_receipt",
        "must be a boolean (true or false)",
    )
    assert isinstance(depositary_raw, bool)
    source = _require_nonempty_str(table["source"], f"{path}.source")
    for field_name, value in (
        ("listing_exchange", listing_exchange),
        ("sub_category", sub_category),
        ("asset_class", asset_class),
        ("source", source),
    ):
        _ensure(
            value != _ROBINHOOD_REPLACE_PLACEHOLDER,
            f"{path}.{field_name}",
            "must replace the generated placeholder",
        )
    _ensure(
        sub_category in _ROBINHOOD_SECURITY_CLASSES,
        f"{path}.sub_category",
        f"must be one of: {', '.join(_ROBINHOOD_SECURITY_CLASSES)}",
    )
    return RobinhoodSecurityConfig(
        instrument=InstrumentIdentity(
            symbol=Ticker(symbol),
            isin=Isin(isin_raw),
            listing_exchange=listing_exchange,
            issuer_country_code=CountryCode(issuer_country),
        ),
        cusip=cusip,
        sub_category=sub_category,
        asset_class=asset_class,
        is_depositary_receipt=depositary_raw,
        source=source,
    )


def _parse_robinhood_option_classification(
    raw: object,
    index: int,
) -> RobinhoodOptionClassificationConfig:
    path = f"robinhood.option_classifications[{index}]"
    _ensure(isinstance(raw, dict), path, "must be a TOML table")
    table = cast("dict[str, object]", raw)
    _check_keys(table, _KNOWN_ROBINHOOD_OPTION_CLASSIFICATION, path)
    _require_fields(table, _REQUIRED_ROBINHOOD_OPTION_CLASSIFICATION, path)

    financial_year = _require_bounded_int(
        table["financial_year"],
        f"{path}.financial_year",
        "must be an integer financial-year start year",
        _ROBINHOOD_MIN_FINANCIAL_YEAR,
        _ROBINHOOD_MAX_FINANCIAL_YEAR,
    )
    treatment_raw = _require_nonempty_str(
        table["treatment"],
        f"{path}.treatment",
        ascii_only=True,
    )
    treatment = _member(
        RobinhoodOptionTreatment,
        treatment_raw,
        f"{path}.treatment",
        f"must be one of: {', '.join(t.value for t in RobinhoodOptionTreatment)}",
    )
    business_fields = _ROBINHOOD_OPTION_BUSINESS_FIELDS.intersection(table)
    _ensure(
        not business_fields or business_fields == _ROBINHOOD_OPTION_BUSINESS_FIELDS,
        path,
        "business filing fields must be supplied together or omitted together",
    )
    _ensure(
        not business_fields or treatment is RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
        path,
        "business filing fields are supported only for speculative-business treatment",
    )
    fema_acknowledged = table["fema_risk_acknowledged"]
    _ensure(
        isinstance(fema_acknowledged, bool),
        f"{path}.fema_risk_acknowledged",
        "must be a boolean (true or false)",
    )
    _ensure(
        fema_acknowledged is True,
        f"{path}.fema_risk_acknowledged",
        (
            "must be true after independently reviewing the FEMA/LRS risk: RBI's "
            "LRS Master Direction disallows remittances in the nature of margins "
            "or margin calls to overseas exchanges/counterparties; this flag "
            "enables tax reporting only, not a compliance conclusion"
        ),
    )
    books_maintained = table.get("books_maintained")
    _ensure(
        books_maintained is None or isinstance(books_maintained, bool),
        f"{path}.books_maintained",
        "must be a boolean (true or false)",
    )
    other_speculative_business = table.get("other_speculative_business")
    _ensure(
        other_speculative_business is None or isinstance(other_speculative_business, bool),
        f"{path}.other_speculative_business",
        "must be a boolean (true or false)",
    )
    nature_of_business_code = table.get("nature_of_business_code")
    if nature_of_business_code is not None:
        nature_of_business_code = _require_nonempty_str(
            nature_of_business_code,
            f"{path}.nature_of_business_code",
            ascii_only=True,
        )
        _ensure(
            nature_of_business_code in _ROBINHOOD_OPTION_BUSINESS_CODES,
            f"{path}.nature_of_business_code",
            "must be 21010 (Futures and Options trading)",
        )
    turnover_basis_raw = table.get("turnover_basis")
    turnover_basis = None
    if turnover_basis_raw is not None:
        turnover_basis = _member(
            RobinhoodOptionTurnoverBasis,
            _require_nonempty_str(turnover_basis_raw, f"{path}.turnover_basis", ascii_only=True),
            f"{path}.turnover_basis",
            "must be: icai-5.11a-differences",
        )

    def optional_amount(name: str) -> Decimal | None:
        value = table.get(name)
        return (
            None
            if value is None
            else _parse_decimal_string(value, f"{path}.{name}", kind=NumberKind.MONEY)
        )

    return RobinhoodOptionClassificationConfig(
        account_number=_require_robinhood_account_number(
            table["account_number"],
            f"{path}.account_number",
        ),
        financial_year=financial_year,
        fema_risk_acknowledged=True,
        treatment=treatment,
        source=_require_nonempty_str(table["source"], f"{path}.source"),
        reason=_require_nonempty_str(table["reason"], f"{path}.reason"),
        books_maintained=cast("bool | None", books_maintained),
        other_speculative_business=cast("bool | None", other_speculative_business),
        nature_of_business_code=nature_of_business_code,
        turnover_basis=turnover_basis,
        external_expenses_inr=optional_amount("external_expenses_inr"),
        bp_additions_inr=optional_amount("bp_additions_inr"),
        bp_deductions_inr=optional_amount("bp_deductions_inr"),
    )


def _parse_robinhood_stock_lending_fee_fsi_source(
    raw: object,
    index: int,
) -> RobinhoodStockLendingFeeFsiSource:
    path = f"robinhood.stock_lending_fee_fsi_sources[{index}]"
    _ensure(isinstance(raw, dict), path, "must be a TOML table")
    table = cast("dict[str, object]", raw)
    _check_keys(table, _KNOWN_ROBINHOOD_STOCK_LENDING_FEE_FSI_SOURCE, path)
    _require_fields(table, _KNOWN_ROBINHOOD_STOCK_LENDING_FEE_FSI_SOURCE, path)
    financial_year = _require_bounded_int(
        table["financial_year"],
        f"{path}.financial_year",
        "must be an integer financial-year start year",
        _ROBINHOOD_MIN_FINANCIAL_YEAR,
        _ROBINHOOD_MAX_FINANCIAL_YEAR,
    )
    return RobinhoodStockLendingFeeFsiSource(
        account_number=_require_robinhood_account_number(
            table["account_number"],
            f"{path}.account_number",
        ),
        financial_year=financial_year,
        country_iso2=_require_country_iso2(
            table["country_iso2"],
            f"{path}.country_iso2",
            forbid_india=True,
        ),
        source=_require_nonempty_str(table["source"], f"{path}.source"),
        reason=_require_nonempty_str(table["reason"], f"{path}.reason"),
    )


def _parse_robinhood_interest_payer_country(
    raw: object,
    index: int,
) -> RobinhoodInterestPayerCountry:
    path = f"robinhood.interest_payer_countries[{index}]"
    _ensure(isinstance(raw, dict), path, "must be a TOML table")
    table = cast("dict[str, object]", raw)
    _check_keys(table, _KNOWN_ROBINHOOD_INTEREST_PAYER_COUNTRY, path)
    _require_fields(table, _KNOWN_ROBINHOOD_INTEREST_PAYER_COUNTRY, path)
    financial_year = _require_bounded_int(
        table["financial_year"],
        f"{path}.financial_year",
        "must be an integer financial-year start year",
        _ROBINHOOD_MIN_FINANCIAL_YEAR,
        _ROBINHOOD_MAX_FINANCIAL_YEAR,
    )
    kinds_raw = table["interest_kinds"]
    kinds_path = f"{path}.interest_kinds"
    _ensure(
        isinstance(kinds_raw, list) and bool(kinds_raw),
        kinds_path,
        "must be a non-empty array of interest kinds",
    )
    known_kinds = [kind.value for kind in RobinhoodInterestKind]
    kinds = cast("list[object]", kinds_raw)
    _ensure(
        all(isinstance(kind, str) and kind in known_kinds for kind in kinds),
        kinds_path,
        f"must contain only: {', '.join(known_kinds)}",
    )
    _ensure(len(set(kinds)) == len(kinds), kinds_path, "must not repeat an interest kind")
    return RobinhoodInterestPayerCountry(
        account_number=_require_robinhood_account_number(
            table["account_number"],
            f"{path}.account_number",
        ),
        financial_year=financial_year,
        country_iso2=_require_country_iso2(
            table["country_iso2"],
            f"{path}.country_iso2",
            forbid_india=False,
        ),
        interest_kinds=frozenset(RobinhoodInterestKind(cast("str", kind)) for kind in kinds),
        source=_require_nonempty_str(table["source"], f"{path}.source"),
        reason=_require_nonempty_str(table["reason"], f"{path}.reason"),
    )


def _parse_robinhood_security_receipt_resolution(
    raw: object,
    index: int,
) -> RobinhoodSecurityReceiptResolution:
    path = f"robinhood.security_receipt_resolutions[{index}]"
    _ensure(isinstance(raw, dict), path, "must be a TOML table")
    table = cast("dict[str, object]", raw)
    _check_keys(table, _KNOWN_ROBINHOOD_SECURITY_RECEIPT_RESOLUTION, path)
    _require_fields(table, _KNOWN_ROBINHOOD_SECURITY_RECEIPT_RESOLUTION, path)

    source_record_index = table["source_record_index"]
    _ensure(
        isinstance(source_record_index, int)
        and not isinstance(source_record_index, bool)
        and source_record_index > 0,
        f"{path}.source_record_index",
        "must be a positive source row integer",
    )
    cusip = _require_nonempty_str(table["cusip"], f"{path}.cusip", ascii_only=True)
    _require_cusip(
        cusip,
        f"{path}.cusip",
        "must be a valid 9-character CUSIP",
        "must be a valid 9-character CUSIP",
    )
    treatment_raw = table["treatment"]
    _ensure(isinstance(treatment_raw, str), f"{path}.treatment", "must be a string")
    assert isinstance(treatment_raw, str)
    treatment = _member(
        RobinhoodSecurityReceiptTreatment,
        treatment_raw,
        f"{path}.treatment",
        "must be promotional-reward",
    )
    exact_quantity = _parse_decimal_string(
        table["exact_quantity"],
        f"{path}.exact_quantity",
        kind=NumberKind.QUANTITY,
    )
    _ensure(
        exact_quantity > 0,
        f"{path}.exact_quantity",
        "must be greater than zero",
    )
    reward_value = _parse_decimal_string(
        table["reward_fair_market_value_usd"],
        f"{path}.reward_fair_market_value_usd",
        kind=NumberKind.MONEY,
    )
    _ensure(
        reward_value > 0,
        f"{path}.reward_fair_market_value_usd",
        "must be greater than zero",
    )
    financial_year = _require_bounded_int(
        table["income_reported_financial_year"],
        f"{path}.income_reported_financial_year",
        "must be an integer FY start year",
        _ROBINHOOD_MIN_FINANCIAL_YEAR,
        _ROBINHOOD_MAX_FINANCIAL_YEAR,
    )
    _ensure(
        table["income_reported"] is True,
        f"{path}.income_reported",
        "must be true only after the reward FMV is included in the Indian return/workpaper",
    )
    return RobinhoodSecurityReceiptResolution(
        account_number=_require_robinhood_account_number(
            table["account_number"],
            f"{path}.account_number",
        ),
        source_record_index=cast("int", source_record_index),
        cusip=cusip,
        treatment=treatment,
        exact_quantity=exact_quantity,
        reward_fair_market_value_usd=reward_value,
        income_reported_financial_year=financial_year,
        income_reported=True,
        source=_require_nonempty_str(table["source"], f"{path}.source"),
        reason=_require_nonempty_str(table["reason"], f"{path}.reason"),
    )


def _require_safe_relative_path(raw: object, field_path: str) -> str:
    value = _require_nonempty_str(raw, field_path, ascii_only=True)
    # Check the literal segments: ``Path(value).parts`` drops "." and empty
    # segments, so "a/./b", "a//b" and "./a/b" would pass as spellings of "a/b" (#1639).
    _ensure(
        not Path(value).is_absolute()
        and ":" not in value
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/")),
        field_path,
        "must be a safe relative POSIX-style path without '.' or '..'",
    )
    return value


def _parse_robinhood_report(raw: object, index: int) -> RobinhoodReportConfig:
    path = f"robinhood.reports[{index}]"
    _ensure(isinstance(raw, dict), path, "must be a TOML table")
    table = cast("dict[str, object]", raw)
    _check_keys(table, _KNOWN_ROBINHOOD_REPORT, path)
    _require_fields(table, _KNOWN_ROBINHOOD_REPORT, path)
    start = _parse_iso_date(table["start_date"], f"{path}.start_date")
    end = _parse_iso_date(table["end_date"], f"{path}.end_date")
    _ensure(start <= end, f"{path}.end_date", "must be on or after start_date")
    return RobinhoodReportConfig(
        file=_require_safe_relative_path(table["file"], f"{path}.file"),
        start_date=start,
        end_date=end,
        source=_require_nonempty_str(table["source"], f"{path}.source"),
    )


def _parse_robinhood_corrected_statement(
    raw: object,
    index: int,
) -> RobinhoodCorrectedStatementConfig:
    path = f"robinhood.corrected_statements[{index}]"
    _ensure(isinstance(raw, dict), path, "must be a TOML table")
    table = cast("dict[str, object]", raw)
    _check_keys(table, _KNOWN_ROBINHOOD_CORRECTED_STATEMENT, path)
    _require_fields(table, _KNOWN_ROBINHOOD_CORRECTED_STATEMENT, path)
    selected = _require_safe_relative_path(
        table["selected_file"],
        f"{path}.selected_file",
    )
    supersedes_raw = table["supersedes"]
    _ensure(
        isinstance(supersedes_raw, list) and bool(supersedes_raw),
        f"{path}.supersedes",
        "must be a non-empty array of relative paths",
    )
    supersedes = tuple(
        _require_safe_relative_path(item, f"{path}.supersedes[{i}]")
        for i, item in enumerate(cast("list[object]", supersedes_raw))
    )
    _ensure(
        len(set(supersedes)) == len(supersedes),
        f"{path}.supersedes",
        "must not contain duplicate paths",
    )
    _ensure(
        selected not in supersedes,
        f"{path}.selected_file",
        "must not also appear in supersedes",
    )
    return RobinhoodCorrectedStatementConfig(
        selected_file=selected,
        supersedes=supersedes,
        source=_require_nonempty_str(table["source"], f"{path}.source"),
    )


def _parse_robinhood(raw: object) -> RobinhoodConfig | None:  # noqa: PLR0915
    if raw is None:
        return None
    _ensure(isinstance(raw, dict), "robinhood", "must be a TOML table")
    table = cast("dict[str, object]", raw)
    _ensure("accounts" in table, "robinhood.accounts", "missing required field")
    # Retired (#628): automatic A2 valuation never read it. Refused by name,
    # without echoing the configured values, rather than as an unknown field.
    _ensure(
        "a2_peak_overrides" not in table,
        "robinhood.a2_peak_overrides",
        "is no longer used: automatic Robinhood A2 valuation never reads it; "
        "delete every [[robinhood.a2_peak_overrides]] table",
    )
    _check_keys(table, _KNOWN_ROBINHOOD, "robinhood")
    accounts_raw = table["accounts"]
    _ensure(
        isinstance(accounts_raw, list),
        "robinhood.accounts",
        "must be an array of tables",
    )
    accounts_list = cast("list[object]", accounts_raw)
    _ensure(
        len(accounts_list) == _ROBINHOOD_ACCOUNT_COUNT,
        "robinhood.accounts",
        "must contain exactly two filed accounts",
    )
    accounts = tuple(_parse_robinhood_account(entry, i) for i, entry in enumerate(accounts_list))
    seen_numbers: set[str] = set()
    for i, account in enumerate(accounts):
        # A duplicate is the only possible self-reference in the account set.
        _ensure(
            account.number not in seen_numbers,
            f"robinhood.accounts[{i}].number",
            "duplicates another configured account number",
        )
        seen_numbers.add(account.number)

    securities_raw = table.get("securities", [])
    _ensure(
        isinstance(securities_raw, list),
        "robinhood.securities",
        "must be an array of tables",
    )
    securities = tuple(
        _parse_robinhood_security(entry, i)
        for i, entry in enumerate(cast("list[object]", securities_raw))
    )
    seen_cusips: set[str] = set()
    seen_isins: set[Isin] = set()
    seen_symbols: set[str] = set()
    for i, security in enumerate(securities):
        path = f"robinhood.securities[{i}]"
        _ensure(
            security.cusip not in seen_cusips,
            f"{path}.cusip",
            "duplicates another configured security CUSIP",
        )
        _ensure(
            security.instrument.isin not in seen_isins,
            f"{path}.isin",
            "duplicates another configured security ISIN",
        )
        _ensure(
            security.instrument.symbol not in seen_symbols,
            f"{path}.symbol",
            "duplicates another configured canonical symbol",
        )
        seen_cusips.add(security.cusip)
        seen_isins.add(security.instrument.isin)
        seen_symbols.add(security.instrument.symbol)

    receipt_resolutions_raw = table.get("security_receipt_resolutions", [])
    _ensure(
        isinstance(receipt_resolutions_raw, list),
        "robinhood.security_receipt_resolutions",
        "must be an array of tables",
    )
    receipt_resolutions = tuple(
        _parse_robinhood_security_receipt_resolution(entry, i)
        for i, entry in enumerate(cast("list[object]", receipt_resolutions_raw))
    )
    seen_receipt_resolutions: set[tuple[str, int]] = set()
    for i, resolution in enumerate(receipt_resolutions):
        path = f"robinhood.security_receipt_resolutions[{i}]"
        _ensure(
            resolution.account_number in seen_numbers,
            f"{path}.account_number",
            "does not match a configured Robinhood account",
        )
        key = (resolution.account_number, resolution.source_record_index)
        _ensure(
            key not in seen_receipt_resolutions,
            path,
            "duplicates another account/source-row receipt resolution",
        )
        seen_receipt_resolutions.add(key)

    classifications_raw = table.get("option_classifications", [])
    _ensure(
        isinstance(classifications_raw, list),
        "robinhood.option_classifications",
        "must be an array of tables",
    )
    classifications = tuple(
        _parse_robinhood_option_classification(entry, i)
        for i, entry in enumerate(cast("list[object]", classifications_raw))
    )
    account_numbers = {account.number for account in accounts}
    seen_classifications: set[tuple[str, int]] = set()
    for i, classification in enumerate(classifications):
        path = f"robinhood.option_classifications[{i}]"
        _ensure(
            classification.account_number in account_numbers,
            f"{path}.account_number",
            "does not match a configured Robinhood account",
        )
        key = (classification.account_number, classification.financial_year)
        _ensure(
            key not in seen_classifications,
            path,
            "duplicates another account/financial-year option classification",
        )
        seen_classifications.add(key)

    fee_sources_raw = table.get("stock_lending_fee_fsi_sources", [])
    _ensure(
        isinstance(fee_sources_raw, list),
        "robinhood.stock_lending_fee_fsi_sources",
        "must be an array of tables",
    )
    fee_sources = tuple(
        _parse_robinhood_stock_lending_fee_fsi_source(entry, i)
        for i, entry in enumerate(cast("list[object]", fee_sources_raw))
    )
    seen_fee_sources: set[tuple[str, int]] = set()
    for i, fee_source in enumerate(fee_sources):
        path = f"robinhood.stock_lending_fee_fsi_sources[{i}]"
        _ensure(
            fee_source.account_number in account_numbers,
            f"{path}.account_number",
            "does not match a configured Robinhood account",
        )
        key = (fee_source.account_number, fee_source.financial_year)
        _ensure(
            key not in seen_fee_sources,
            path,
            "duplicates another account/financial-year Stock Lending fee FSI source",
        )
        seen_fee_sources.add(key)

    payer_countries_raw = table.get("interest_payer_countries", [])
    _ensure(
        isinstance(payer_countries_raw, list),
        "robinhood.interest_payer_countries",
        "must be an array of tables",
    )
    payer_countries = tuple(
        _parse_robinhood_interest_payer_country(entry, i)
        for i, entry in enumerate(cast("list[object]", payer_countries_raw))
    )
    seen_payer_countries: set[tuple[str, int]] = set()
    for i, payer_country in enumerate(payer_countries):
        path = f"robinhood.interest_payer_countries[{i}]"
        _ensure(
            payer_country.account_number in account_numbers,
            f"{path}.account_number",
            "does not match a configured Robinhood account",
        )
        key = (payer_country.account_number, payer_country.financial_year)
        _ensure(
            key not in seen_payer_countries,
            path,
            "duplicates another account/financial-year interest payer country",
        )
        seen_payer_countries.add(key)

    reports_raw = table.get("reports", [])
    _ensure(
        isinstance(reports_raw, list),
        "robinhood.reports",
        "must be an array of tables",
    )
    reports = tuple(
        _parse_robinhood_report(entry, i)
        for i, entry in enumerate(cast("list[object]", reports_raw))
    )
    _ensure(
        len({item.file for item in reports}) == len(reports),
        "robinhood.reports",
        "must not contain duplicate report files",
    )

    corrected_raw = table.get("corrected_statements", [])
    _ensure(
        isinstance(corrected_raw, list),
        "robinhood.corrected_statements",
        "must be an array of tables",
    )
    corrected = tuple(
        _parse_robinhood_corrected_statement(entry, i)
        for i, entry in enumerate(cast("list[object]", corrected_raw))
    )
    corrected_paths = [
        path for item in corrected for path in (item.selected_file, *item.supersedes)
    ]
    _ensure(
        len(set(corrected_paths)) == len(corrected_paths),
        "robinhood.corrected_statements",
        "a file may appear in only one corrected-statement selection",
    )
    sec_user_agent = (
        _require_nonempty_str(
            table["sec_user_agent"],
            "robinhood.sec_user_agent",
            ascii_only=True,
        )
        if "sec_user_agent" in table
        else None
    )
    cusip_transitions_file = (
        _require_nonempty_str(
            table["cusip_transitions_file"],
            "robinhood.cusip_transitions_file",
        )
        if "cusip_transitions_file" in table
        else None
    )
    return RobinhoodConfig(
        accounts=accounts,
        securities=securities,
        security_receipt_resolutions=receipt_resolutions,
        option_classifications=classifications,
        stock_lending_fee_fsi_sources=fee_sources,
        interest_payer_countries=payer_countries,
        reports=reports,
        corrected_statements=corrected,
        sec_user_agent=sec_user_agent,
        cusip_transitions_file=cusip_transitions_file,
    )


def _parse_netbenefits_account(raw: object) -> NetBenefitsAccountConfig:
    """Parse ``[netbenefits.account]`` — the Schedule FA Table A2 identity, plus
    the optional Quicko ``withholding_agent_name``."""
    _ensure(isinstance(raw, dict), "netbenefits.account", "must be a TOML table")
    table = cast("dict[str, Any]", raw)
    _check_keys(table, _KNOWN_NETBENEFITS_ACCOUNT, "netbenefits.account")
    _require_fields(
        table, _KNOWN_NETBENEFITS_ACCOUNT - {"zip", "withholding_agent_name"}, "netbenefits.account"
    )

    institution_name = _require_nonempty_str(
        table["institution_name"], "netbenefits.account.institution_name", filed=True
    )
    address = _require_nonempty_str(table["address"], "netbenefits.account.address", filed=True)
    zip_raw = _optional_str(table.get("zip"), "netbenefits.account.zip", filed=True)
    country = _require_country_iso2(
        table["country_iso2"], "netbenefits.account.country_iso2", forbid_india=True
    )
    opening = _parse_iso_date(
        table["account_opening_date"],
        "netbenefits.account.account_opening_date",
    )

    status = _parse_taxpayer_status(table["status"], "netbenefits.account.status")

    withholding_agent_name = (
        _require_nonempty_str(
            table["withholding_agent_name"],
            "netbenefits.account.withholding_agent_name",
            filed=True,
        )
        if "withholding_agent_name" in table
        else None
    )

    return NetBenefitsAccountConfig(
        institution_name=institution_name,
        address=address,
        zip_code=zip_raw,
        country_iso2=country,
        account_opening_date=opening,
        status=status,
        withholding_agent_name=withholding_agent_name,
    )


def _parse_netbenefits_security(raw: object) -> NetBenefitsSecurityConfig:
    """Parse ``[netbenefits.security]`` — the Schedule FA Table A3 identity."""
    _ensure(isinstance(raw, dict), "netbenefits.security", "must be a TOML table")
    table = cast("dict[str, Any]", raw)
    _refuse_retired_keys(table, _REMOVED_LOT_SECURITY_KEYS, "netbenefits.security")
    _check_keys(table, _KNOWN_NETBENEFITS_SECURITY, "netbenefits.security")
    _require_fields(table, _KNOWN_NETBENEFITS_SECURITY - {"entity_zip"}, "netbenefits.security")

    symbol = _require_nonempty_str(
        table["symbol"], "netbenefits.security.symbol", ascii_only=True, filed=True
    )
    cusip = _require_nonempty_str(table["cusip"], "netbenefits.security.cusip", ascii_only=True)
    # Validate the CUSIP's own check digit, so a single-character typo can't slip
    # through (and later be filed) even if a matching ISIN is supplied alongside.
    _require_cusip(
        cusip,
        "netbenefits.security.cusip",
        "must be a 9-character uppercase-alphanumeric CUSIP",
        "invalid check digit",
    )
    isin_raw = _require_nonempty_str(table["isin"], "netbenefits.security.isin", ascii_only=True)
    _require_isin_shape(isin_raw, "netbenefits.security.isin")
    # A NetBenefits ESPP holding is a US-listed, CUSIP-backed security, so its
    # ISIN is exactly "US" + the CUSIP + the ISIN check digit. Requiring that
    # binds the ISIN and CUSIP to one security in a single check (a non-US ISIN,
    # or one describing a different security, is refused — never-be-wrong). A
    # non-US case can be added if/when a non-US NetBenefits plan is supported.
    expected_isin = "US" + cusip + isin_check_digit("US" + cusip)
    _ensure(
        isin_raw == expected_isin,
        "netbenefits.security.isin",
        "must be the US ISIN for the configured CUSIP ('US', the CUSIP, then the ISIN check digit)",
    )

    entity_address = _require_nonempty_str(
        table["entity_address"], "netbenefits.security.entity_address", filed=True
    )
    entity_zip = _optional_str(
        table.get("entity_zip"), "netbenefits.security.entity_zip", filed=True
    )
    listing_exchange = _require_nonempty_str(
        table["listing_exchange"], "netbenefits.security.listing_exchange"
    )
    issuer_country = _require_country_iso2(
        table["issuer_country_code"],
        "netbenefits.security.issuer_country_code",
        forbid_india=True,
    )
    nature = _require_nonempty_str(
        table["nature_of_entity"], "netbenefits.security.nature_of_entity", filed=True
    )
    _ensure(
        len(nature) <= _FA_A3_NATURE_MAX_LENGTH,
        "netbenefits.security.nature_of_entity",
        f"must be at most {_FA_A3_NATURE_MAX_LENGTH} characters",
    )

    return NetBenefitsSecurityConfig(
        instrument=InstrumentIdentity(
            symbol=Ticker(symbol),
            isin=Isin(isin_raw),
            listing_exchange=listing_exchange,
            issuer_country_code=CountryCode(issuer_country),
        ),
        cusip=cusip,
        entity_address=entity_address,
        entity_zip=entity_zip,
        nature_of_entity=nature,
    )


def _parse_stockplan(raw: object) -> StockplanConfig | None:
    """Parse the optional ``[stockplan]`` table (Morgan Stanley StockPlan)."""
    if raw is None:
        return None
    _ensure(isinstance(raw, dict), "stockplan", "must be a TOML table")
    table = cast("dict[str, Any]", raw)
    _check_keys(table, _KNOWN_STOCKPLAN, "stockplan")
    opening = (
        _parse_iso_date(table["opening_date"], "stockplan.opening_date")
        if "opening_date" in table
        else None
    )
    _ensure("security" in table, "stockplan.security", "missing required table")
    sec_raw = table["security"]
    _ensure(isinstance(sec_raw, dict), "stockplan.security", "must be a TOML table")
    sec = cast("dict[str, Any]", sec_raw)
    _refuse_retired_keys(sec, _REMOVED_LOT_SECURITY_KEYS, "stockplan.security")
    _check_keys(sec, _KNOWN_STOCKPLAN_SECURITY, "stockplan.security")
    _require_fields(sec, ("symbol", "isin"), "stockplan.security")

    symbol = _require_nonempty_str(
        sec["symbol"], "stockplan.security.symbol", ascii_only=True, filed=True
    )
    isin_raw = _require_nonempty_str(sec["isin"], "stockplan.security.isin", ascii_only=True)
    # No CUSIP is required (StockPlan exports supply neither CUSIP nor ISIN),
    # so the ISIN is self-checked, not cross-checked against a CUSIP as
    # [netbenefits.security] does.
    _require_isin(isin_raw, "stockplan.security.isin")

    # Optional Schedule FA fields (only stockplan-fa needs them). An absent one
    # is left empty, as in an income-only config. issuer_country_code is
    # validated whenever the key is present, whatever its value.
    issuer_country = ""
    if "issuer_country_code" in sec:
        issuer_country = _require_country_iso2(
            sec["issuer_country_code"], "stockplan.security.issuer_country_code", forbid_india=True
        )
    nature = _optional_str(
        sec.get("nature_of_entity"), "stockplan.security.nature_of_entity", filed=True
    )
    _ensure(
        len(nature) <= _FA_A3_NATURE_MAX_LENGTH,
        "stockplan.security.nature_of_entity",
        f"must be at most {_FA_A3_NATURE_MAX_LENGTH} characters",
    )
    entity_name = _optional_str(
        sec.get("entity_name"), "stockplan.security.entity_name", filed=True
    )
    entity_address = _optional_str(
        sec.get("entity_address"), "stockplan.security.entity_address", filed=True
    )
    entity_zip = _optional_str(sec.get("entity_zip"), "stockplan.security.entity_zip", filed=True)
    listing_exchange = _optional_str(
        sec.get("listing_exchange"), "stockplan.security.listing_exchange"
    )
    # The FA instrument exists only for a complete Schedule FA config; an
    # income-only (or partial) one gets None and stockplan-fa refuses it.
    instrument = (
        InstrumentIdentity(
            symbol=Ticker(symbol),
            isin=Isin(isin_raw),
            listing_exchange=listing_exchange,
            issuer_country_code=CountryCode(issuer_country),
        )
        if all((entity_name, entity_address, listing_exchange, issuer_country, nature))
        else None
    )
    security = StockplanSecurityConfig(
        symbol=symbol,
        isin=Isin(isin_raw),
        entity_name=entity_name,
        entity_address=entity_address,
        entity_zip=entity_zip,
        nature_of_entity=nature,
        instrument=instrument,
    )
    return StockplanConfig(security=security, opening_date=opening)


def _parse_fidelity_cma_eligible_tax(
    raw: object,
    *,
    year_path: str,
    tax_year: int,
    index: int,
) -> FidelityCmaEligibleTax:
    path = f"{year_path}.eligible_tax[{index}]"
    _ensure(isinstance(raw, dict), path, "must be a TOML table")
    table = cast("dict[str, object]", raw)
    _check_keys(table, _KNOWN_FIDELITY_CMA_ELIGIBLE_TAX, path)
    _require_fields(table, _KNOWN_FIDELITY_CMA_ELIGIBLE_TAX, path)
    payment_date = _parse_iso_date(table["payment_date"], f"{path}.payment_date")
    _ensure(
        payment_date.year == tax_year,
        f"{path}.payment_date",
        f"must fall in the US tax year given by {year_path}.tax_year",
    )
    amount = _parse_decimal_string(table["amount_usd"], f"{path}.amount_usd", kind=NumberKind.MONEY)
    exponent = amount.as_tuple().exponent
    _ensure(
        amount >= 0 and isinstance(exponent, int) and exponent >= _FIDELITY_CMA_CENT_EXPONENT,
        f"{path}.amount_usd",
        "must be a non-negative whole-cent USD amount (at most 2 decimal places)",
    )
    return FidelityCmaEligibleTax(payment_date=payment_date, amount_usd=amount)


def _parse_fidelity_cma_us_tax_year(
    raw: object,
    index: int,
) -> FidelityCmaUsTaxYearConfig:
    path = f"fidelity_cma.us_tax_years[{index}]"
    _ensure(isinstance(raw, dict), path, "must be a TOML table")
    table = cast("dict[str, object]", raw)
    _check_keys(table, _KNOWN_FIDELITY_CMA_US_TAX_YEAR, path)
    _require_fields(table, ("tax_year", "refund_status"), path)
    tax_year = _require_bounded_int(
        table["tax_year"],
        f"{path}.tax_year",
        "must be an integer calendar year",
        _FIDELITY_CMA_MIN_TAX_YEAR,
        _FIDELITY_CMA_MAX_TAX_YEAR,
    )
    status_raw = table["refund_status"]
    _ensure(isinstance(status_raw, str), f"{path}.refund_status", "must be a string")
    assert isinstance(status_raw, str)
    valid = ", ".join(value.value for value in FidelityCmaRefundStatus)
    status = _member(
        FidelityCmaRefundStatus, status_raw, f"{path}.refund_status", f"must be one of: {valid}"
    )

    eligible_raw = table.get("eligible_tax", [])
    _ensure(isinstance(eligible_raw, list), f"{path}.eligible_tax", "must be an array of tables")
    eligible_list = cast("list[object]", eligible_raw)
    eligible = tuple(
        _parse_fidelity_cma_eligible_tax(entry, year_path=path, tax_year=tax_year, index=i)
        for i, entry in enumerate(eligible_list)
    )
    dates = [entry.payment_date for entry in eligible]
    _ensure(
        len(dates) == len(set(dates)),
        f"{path}.eligible_tax",
        "contains duplicate payment_date entries",
    )
    provenance = _optional_str(table.get("provenance"), f"{path}.provenance")
    if status in FIDELITY_CMA_FTC_STATUSES:
        _ensure(
            provenance != "",
            f"{path}.provenance",
            "is required when refund_status is finalized or no-refund-claim",
        )
    else:
        # Name both fixes in full, so either one loads on the next run (#1511):
        # an FTC status also needs a provenance, and a blocking one allows none.
        fix = (
            "set refund_status to one of those, or keep refund_status and "
            "delete eligible_tax and provenance"
            if provenance
            else "set refund_status to one of those and add a non-empty provenance, "
            "or keep refund_status and delete eligible_tax"
        )
        _ensure(
            not eligible,
            f"{path}.eligible_tax",
            f"is allowed only when refund_status is finalized or no-refund-claim; {fix}",
        )
        _ensure(
            provenance == "",
            f"{path}.provenance",
            "is allowed only when refund_status is finalized or no-refund-claim",
        )
    return FidelityCmaUsTaxYearConfig(
        tax_year=tax_year,
        refund_status=status,
        eligible_tax=eligible,
        provenance=provenance or None,
    )


def _parse_fidelity_cma(raw: object) -> FidelityCmaConfig | None:
    """Parse the optional Fidelity Cash Management Account source config."""
    if raw is None:
        return None
    _ensure(isinstance(raw, dict), "fidelity_cma", "must be a TOML table")
    table = cast("dict[str, object]", raw)
    _check_keys(table, _KNOWN_FIDELITY_CMA, "fidelity_cma")
    _require_fields(table, ("account_id", "spaxx_isin"), "fidelity_cma")
    account_id = _require_nonempty_str(
        table["account_id"], "fidelity_cma.account_id", ascii_only=True
    )
    _ensure(
        re.fullmatch(r"[A-Z][0-9]{2}-[0-9]{6}", account_id) is not None,
        "fidelity_cma.account_id",
        "must match the observed Fidelity account shape A99-999999",
    )
    spaxx_isin = _require_nonempty_str(
        table["spaxx_isin"], "fidelity_cma.spaxx_isin", ascii_only=True
    )
    _ensure(
        spaxx_isin == "US31617H1023",
        "fidelity_cma.spaxx_isin",
        "must be the verified SPAXX ISIN US31617H1023",
    )
    years_raw = table.get("us_tax_years", [])
    _ensure(
        isinstance(years_raw, list),
        "fidelity_cma.us_tax_years",
        "must be an array of tables",
    )
    years_list = cast("list[object]", years_raw)
    years = tuple(_parse_fidelity_cma_us_tax_year(entry, i) for i, entry in enumerate(years_list))
    year_keys = [entry.tax_year for entry in years]
    _ensure(
        len(year_keys) == len(set(year_keys)),
        "fidelity_cma.us_tax_years",
        "contains duplicate tax_year entries",
    )
    suppress_raw = table.get("suppress_peak_balance_warning", False)
    _ensure(
        isinstance(suppress_raw, bool),
        "fidelity_cma.suppress_peak_balance_warning",
        "must be a boolean (true or false)",
    )
    assert isinstance(suppress_raw, bool)
    bound_raw = table.get("peak_balance_bound")
    bound: FidelityCmaPeakBalanceBound | None = None
    if bound_raw is not None:
        _ensure(
            isinstance(bound_raw, str),
            "fidelity_cma.peak_balance_bound",
            'must be "lower" or "upper"',
        )
        assert isinstance(bound_raw, str)
        bound = _member(
            FidelityCmaPeakBalanceBound,
            bound_raw,
            "fidelity_cma.peak_balance_bound",
            (
                'must be "lower" (minimum proven native balance; may understate the '
                'intraday peak) or "upper" (more conservative in native currency; '
                "may overstate it); each converts at its own date's TTBR, so the "
                "rupee order can differ, and fidelity-cma-fa refuses the bound when it does"
            ),
        )
    if suppress_raw:
        _ensure(
            bound is not None,
            "fidelity_cma.peak_balance_bound",
            (
                "is required when suppress_peak_balance_warning = true; choose "
                '"upper" (more conservative in native currency; may overstate) or "lower" '
                "(minimum proven native balance; may understate the intraday peak); each "
                "converts at its own date's TTBR, so the rupee order can differ, and "
                "fidelity-cma-fa refuses the bound when it does"
            ),
        )
    else:
        _ensure(
            bound is None,
            "fidelity_cma.peak_balance_bound",
            "is allowed only when suppress_peak_balance_warning = true",
        )
    status_raw = table.get("status")
    status: TaxpayerStatus | None = None
    if status_raw is not None:
        _ensure(
            isinstance(status_raw, str),
            "fidelity_cma.status",
            "must be a string",
        )
        assert isinstance(status_raw, str)
        status = _member(
            TaxpayerStatus,
            status_raw,
            "fidelity_cma.status",
            "must be BeneficialOwner for the observed INDIVIDUAL TOD layout",
        )
        _ensure(
            status is TaxpayerStatus.BENEFICIAL_OWNER,
            "fidelity_cma.status",
            (
                "must be BeneficialOwner for the observed INDIVIDUAL TOD layout; "
                "Joint and SigningAuthority registrations are unsupported"
            ),
        )
    return FidelityCmaConfig(
        account_id=account_id,
        spaxx_isin=Isin(spaxx_isin),
        us_tax_years=years,
        suppress_peak_balance_warning=suppress_raw,
        peak_balance_bound=bound,
        status=status,
    )


def _parse_netbenefits_price_override(
    table: dict[str, object], index: int
) -> NetBenefitsPriceOverride:
    """Parse one ``[[netbenefits.price_overrides]]`` entry."""
    path = f"netbenefits.price_overrides[{index}]"
    _check_keys(table, _KNOWN_NETBENEFITS_PRICE_OVERRIDE, path)
    _require_fields(table, _KNOWN_NETBENEFITS_PRICE_OVERRIDE, path)

    price_date = _parse_iso_date(table["price_date"], f"{path}.price_date")
    price = _parse_decimal_string(
        table["price_per_share_usd"], f"{path}.price_per_share_usd", kind=NumberKind.PRICE
    )
    _ensure(
        price > 0,
        f"{path}.price_per_share_usd",
        "must be > 0",
    )
    source = _require_nonempty_str(table["source"], f"{path}.source")
    reason = _require_nonempty_str(table["reason"], f"{path}.reason")
    return NetBenefitsPriceOverride(
        price_date=price_date,
        price_per_share_usd=price,
        source=source,
        reason=reason,
    )


def _parse_netbenefits_cg_fmv_value(table: dict[str, object], index: int) -> NetBenefitsCgFmvValue:
    """Parse one ``[[netbenefits.cg_fmv_values]]`` entry."""
    path = f"netbenefits.cg_fmv_values[{index}]"
    _check_keys(table, _KNOWN_NETBENEFITS_CG_FMV_VALUE, path)
    _require_fields(table, _KNOWN_NETBENEFITS_CG_FMV_VALUE, path)

    purchase_date = _parse_iso_date(table["purchase_date"], f"{path}.purchase_date")
    fmv_total = _parse_decimal_string(
        table["section17_gross_fmv_total_inr"],
        f"{path}.section17_gross_fmv_total_inr",
        kind=NumberKind.MONEY,
    )
    _ensure(
        0 < fmv_total <= CBDT_AMOUNT_BOUND,
        f"{path}.section17_gross_fmv_total_inr",
        f"must be > 0 and <= {CBDT_AMOUNT_BOUND}",
    )
    source = _require_nonempty_str(table["source"], f"{path}.source")
    return NetBenefitsCgFmvValue(
        purchase_date=purchase_date,
        section17_gross_fmv_total_inr=fmv_total,
        source=source,
    )


def _parse_netbenefits_a2_peak_override(
    table: dict[str, object], index: int, *, opening_year: int
) -> NetBenefitsA2PeakOverride:
    """Parse one ``[[netbenefits.a2_peak_overrides]]`` entry."""
    path = f"netbenefits.a2_peak_overrides[{index}]"
    _check_keys(table, _KNOWN_NETBENEFITS_A2_PEAK_OVERRIDE, path)
    _require_fields(table, _KNOWN_NETBENEFITS_A2_PEAK_OVERRIDE, path)

    calendar_year = _require_bounded_int(
        table["calendar_year"],
        f"{path}.calendar_year",
        "must be a TOML integer (e.g. 2025)",
        _SCHEDULE_FA_CY_MIN,
        _SCHEDULE_FA_CY_MAX,
    )
    # A peak for a year before the account opened is impossible (it didn't exist).
    _ensure(
        calendar_year >= opening_year,
        f"{path}.calendar_year",
        "is before the year of netbenefits.account.account_opening_date; a Schedule FA "
        "peak for a year the account did not exist is impossible",
    )

    # A filed figure is supplied as whole rupees and never silently rounded (a
    # TOML float `= 57314.4` is a type error here).
    peak = _require_bounded_int(
        table["peak_balance_inr"],
        f"{path}.peak_balance_inr",
        "must be a whole-rupee TOML integer (a filed figure is not rounded; "
        "a fractional / float value is rejected)",
        -CBDT_AMOUNT_BOUND,
        CBDT_AMOUNT_BOUND,
        bound_note=" (CBDT schema bound)",
    )

    return NetBenefitsA2PeakOverride(calendar_year=calendar_year, peak_balance_inr=peak)


def _parse_netbenefits(  # noqa: PLR0915
    table: dict[str, object] | None,
) -> NetBenefitsConfig | None:
    """Parse the optional ``[netbenefits]`` table. Returns ``None`` when absent."""
    if table is None:
        return None
    _check_keys(table, _KNOWN_NETBENEFITS, "netbenefits")
    _require_fields(
        table, ("account", "security", "espp_discount_pct", "participant_no"), "netbenefits"
    )

    participant_no = _require_nonempty_str(
        table["participant_no"], "netbenefits.participant_no", ascii_only=True, filed=True
    )
    _ensure(
        "*" not in participant_no,
        "netbenefits.participant_no",
        "must be the real Participant No., not a masked value (no '*' characters)",
    )
    account = _parse_netbenefits_account(table["account"])
    security = _parse_netbenefits_security(table["security"])

    discount = _parse_decimal_string(
        table["espp_discount_pct"],
        "netbenefits.espp_discount_pct",
        kind=NumberKind.RATE,
        require_at_most_100=True,
    )
    _ensure(
        discount > 0,
        "netbenefits.espp_discount_pct",
        "must be a percent > 0 and <= 100",
    )
    overrides_raw = table.get("price_overrides", [])
    _ensure(
        isinstance(overrides_raw, list),
        "netbenefits.price_overrides",
        "must be a TOML array of tables (use [[netbenefits.price_overrides]])",
    )
    assert isinstance(overrides_raw, list)
    overrides: list[NetBenefitsPriceOverride] = []
    seen_dates: set[_dt.date] = set()
    for i, entry_raw in enumerate(overrides_raw):
        _ensure(
            isinstance(entry_raw, dict), f"netbenefits.price_overrides[{i}]", "must be a TOML table"
        )
        assert isinstance(entry_raw, dict)
        override = _parse_netbenefits_price_override(cast("dict[str, Any]", entry_raw), i)
        _ensure(
            override.price_date not in seen_dates,
            f"netbenefits.price_overrides[{i}]",
            "duplicates another override's price_date; each date may appear at most once",
        )
        seen_dates.add(override.price_date)
        overrides.append(override)

    cg_fmv_values_raw = table.get("cg_fmv_values", [])
    _ensure(
        isinstance(cg_fmv_values_raw, list),
        "netbenefits.cg_fmv_values",
        "must be a TOML array of tables (use [[netbenefits.cg_fmv_values]])",
    )
    assert isinstance(cg_fmv_values_raw, list)
    cg_fmv_values: list[NetBenefitsCgFmvValue] = []
    seen_cg_fmv_dates: set[_dt.date] = set()
    for i, entry_raw in enumerate(cg_fmv_values_raw):
        _ensure(
            isinstance(entry_raw, dict),
            f"netbenefits.cg_fmv_values[{i}]",
            "must be a TOML table",
        )
        assert isinstance(entry_raw, dict)
        value = _parse_netbenefits_cg_fmv_value(cast("dict[str, Any]", entry_raw), i)
        _ensure(
            value.purchase_date not in seen_cg_fmv_dates,
            f"netbenefits.cg_fmv_values[{i}]",
            "duplicates another value's purchase_date; each purchase date may appear at most once",
        )
        seen_cg_fmv_dates.add(value.purchase_date)
        cg_fmv_values.append(value)

    peak_overrides_raw = table.get("a2_peak_overrides", [])
    _ensure(
        isinstance(peak_overrides_raw, list),
        "netbenefits.a2_peak_overrides",
        "must be a TOML array of tables (use [[netbenefits.a2_peak_overrides]])",
    )
    assert isinstance(peak_overrides_raw, list)
    peak_overrides: list[NetBenefitsA2PeakOverride] = []
    seen_years: set[int] = set()
    opening_year = account.account_opening_date.year
    for i, entry_raw in enumerate(peak_overrides_raw):
        _ensure(
            isinstance(entry_raw, dict),
            f"netbenefits.a2_peak_overrides[{i}]",
            "must be a TOML table",
        )
        assert isinstance(entry_raw, dict)
        peak_override = _parse_netbenefits_a2_peak_override(
            cast("dict[str, Any]", entry_raw), i, opening_year=opening_year
        )
        _ensure(
            peak_override.calendar_year not in seen_years,
            f"netbenefits.a2_peak_overrides[{i}]",
            "duplicates another configured year",
        )
        seen_years.add(peak_override.calendar_year)
        peak_overrides.append(peak_override)

    # The A2 peak is filed tax data; require a participant binding so it can't
    # silently carry across a user.toml copied to a different account (mirrors
    # schedule_fa.manual_values_account_number).
    peak_participant_raw = table.get("a2_peak_overrides_participant_no")
    peak_participant_no: str | None = None
    if peak_participant_raw is not None:
        _ensure(
            isinstance(peak_participant_raw, str),
            "netbenefits.a2_peak_overrides_participant_no",
            "must be a string",
        )
        assert isinstance(peak_participant_raw, str)
        peak_participant_no = peak_participant_raw
    _ensure(
        not peak_overrides or peak_participant_no is not None,
        "netbenefits.a2_peak_overrides_participant_no",
        "is required when [[netbenefits.a2_peak_overrides]] are present (set it to the "
        "same value as [netbenefits].participant_no)",
    )
    if peak_participant_no is not None:
        _ensure(
            peak_participant_no == participant_no,
            "netbenefits.a2_peak_overrides_participant_no",
            "must equal [netbenefits].participant_no; the manual A2 peak is bound "
            "to that participant",
        )

    return NetBenefitsConfig(
        account=account,
        security=security,
        espp_discount_pct=discount,
        participant_no=participant_no,
        price_overrides=tuple(overrides),
        cg_fmv_values=tuple(cg_fmv_values),
        a2_peak_overrides=tuple(peak_overrides),
        a2_peak_overrides_participant_no=peak_participant_no,
    )


# Python 3.12 and 3.13 give a TOML error's position only as this suffix of the
# message; 3.14 adds ``lineno`` and ``colno``.
_TOML_POSITION_SUFFIX: Final[re.Pattern[str]] = re.compile(
    r"\(at line ([0-9]+), column ([0-9]+)\)\Z"
)


def toml_error_message(exc: tomllib.TOMLDecodeError) -> str:
    """Fixed text and the error's line and column, when known. Never the
    parser's message, which can quote a key, and a key may be a pasted value
    (#1045)."""
    lineno = getattr(exc, "lineno", None)
    colno = getattr(exc, "colno", None)
    if not (isinstance(lineno, int) and isinstance(colno, int)):
        match = _TOML_POSITION_SUFFIX.search(str(exc))
        if match is None:
            return "invalid TOML"
        lineno, colno = int(match[1]), int(match[2])
    return f"invalid TOML at line {lineno}, column {colno}"


def load_user_config(path: Path) -> UserConfig:
    """Load and validate a ``user.toml`` file.

    Args:
        path: Filesystem path to the TOML file.

    Returns:
        A :class:`UserConfig` mirroring the file's contents.

    Raises:
        PathRefusalError: If ``path`` does not exist.
        FileNotFoundError: If ``path`` disappears between that check and
            opening it.
        ConfigError: If the file is not UTF-8, is not valid TOML or
            fails schema validation. The error's ``field`` attribute is
            the field path (e.g., ``"account.opening_date"``), the containing
            table for an unknown key, or ``"<file>"`` for a file-level error.
    """
    if not exists(path):
        raise PathRefusalError("user.toml not found", path)

    with path.open("rb") as f:
        try:
            raw = tomllib.load(f)
            message = None
        except UnicodeDecodeError as exc:
            message = (
                f"must be UTF-8 text, but the byte at offset {exc.start} "
                "(counting from 0) is not valid UTF-8"
            )
        except tomllib.TOMLDecodeError as exc:
            message = toml_error_message(exc)
    # Refused after the handlers: a decode error holds the file's bytes, and a
    # TOML error its text (a duplicate key, or the whole document on 3.14).
    if message is not None:
        raise ConfigError(field="<file>", message=message)

    _check_keys(raw, _KNOWN_TOP_LEVEL, "<root>")
    _ensure("account" in raw, "account", "missing required table")
    account_raw = raw["account"]
    _ensure(isinstance(account_raw, dict), "account", "must be a TOML table")
    assert isinstance(account_raw, dict)
    fx_raw = _optional_table(raw, "fx")
    os_raw = _optional_table(raw, "os")
    form67_raw = _optional_table(raw, "form67")
    prices_raw = _optional_table(raw, "prices")
    fa_raw = _optional_table(raw, "fa")
    schedule_fa_raw = _optional_table(raw, "schedule_fa")
    netbenefits_raw = _optional_table(raw, "netbenefits")
    stockplan_raw = _optional_table(raw, "stockplan")
    fidelity_cma_raw = _optional_table(raw, "fidelity_cma")
    robinhood_raw = _optional_table(raw, "robinhood")

    account = _parse_account(account_raw)
    fx = _parse_fx(fx_raw)
    os_cfg = _parse_os(os_raw)
    form67_cfg = _parse_form67(form67_raw)
    prices_cfg = _parse_prices(prices_raw)
    fa_cfg = _parse_fa(fa_raw)
    schedule_fa_cfg = _parse_schedule_fa(schedule_fa_raw)
    netbenefits_cfg = _parse_netbenefits(netbenefits_raw)
    stockplan_cfg = _parse_stockplan(stockplan_raw)
    fidelity_cma_cfg = _parse_fidelity_cma(fidelity_cma_raw)
    robinhood_cfg = _parse_robinhood(robinhood_raw)
    # The Fidelity NetBenefits Participant Number is distinct from the IBKR
    # account ID. Refuse a copied IBKR value.
    if netbenefits_cfg is not None and account.number is not None:
        ibkr_number = str(account.number)
        _ensure(
            netbenefits_cfg.participant_no != ibkr_number,
            "netbenefits.participant_no",
            "must not equal [account].number; the IBKR account ID is a different "
            "identifier from the Fidelity Participant Number",
        )
    # Cross-check the manual-values account binding against [account].number so
    # a manual figure can't be filed against the wrong account (the parser only
    # validates the binding is present; this confirms it matches).
    if schedule_fa_cfg.manual_values_account_number is not None:
        _ensure(
            account.number is not None,
            "schedule_fa.manual_values_account_number",
            "manual IBKR Schedule FA values require a real [account].number",
        )
        _ensure(
            schedule_fa_cfg.manual_values_account_number == str(account.number),
            "schedule_fa.manual_values_account_number",
            (
                "must equal [account].number; the manual Schedule FA figures are "
                "bound to that account"
            ),
        )
    # A peak for a calendar year before the account was opened is logically
    # impossible (the account didn't exist) — refuse rather than file it. Needs
    # both the parsed account (for opening_date) and schedule_fa, so it lives
    # here, not in _parse_schedule_fa.
    if schedule_fa_cfg.manual_values:
        opening_date = account.require_opening_date()
        for i, entry in enumerate(schedule_fa_cfg.manual_values):
            _ensure(
                entry.calendar_year >= opening_date.year,
                f"schedule_fa.manual_values[{i}].calendar_year",
                (
                    "is before the year of account.opening_date; a Schedule FA peak "
                    "for a year the account did not exist is impossible"
                ),
            )
    return UserConfig(
        account=account,
        fx=fx,
        os=os_cfg,
        form67=form67_cfg,
        prices=prices_cfg,
        fa=fa_cfg,
        schedule_fa=schedule_fa_cfg,
        netbenefits=netbenefits_cfg,
        stockplan=stockplan_cfg,
        fidelity_cma=fidelity_cma_cfg,
        robinhood=robinhood_cfg,
    )
