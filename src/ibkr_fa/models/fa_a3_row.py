"""One Schedule FA Table A3 row, ready for output.

A :class:`FaA3Row` is the output-side counterpart to the input-side
lot models (:class:`~ibkr_fa.models.open_lot.OpenLot` and
:class:`~ibkr_fa.models.closed_lot.ClosedLot`). One row is emitted per
lot that overlaps the calendar year being reported.

The 12 "portal" fields mirror the on-screen Schedule FA Table A3 entry
form in the e-filing portal. They are the cells the user reads off the
output CSV and types into the matching portal field.

The "audit" fields are NOT typed into the portal. They preserve the
underlying USD source values and the FX rates used to convert them so
the final INR figure is independently traceable. The
:class:`~ibkr_fa.output.review_csv.ReviewCsvWriter` will emit them as
trailing columns clearly marked "ignore when filing".

In v1, :attr:`peak_value_inr` remains optional because unavailable or
incomplete daily pricing fails closed to manual-fill.
:attr:`gross_proceeds_from_sale_inr` uses proven pre-fee
consideration, or Robinhood's recorded Amount filed as consideration
(#1383), and the December 31 TTBR; it is ``None`` when a closed lot's
source has neither, and
:class:`~decimal.Decimal` 0 for open lots. :attr:`gross_amount_paid_credited_inr` is conditionally
populated: filled from per-symbol INR dividend totals when the input
has a Dividends section (Activity Statement, post-#31); ``None``
when absent (Flex Query inputs without a CashTransactions parser).
``nature_of_entity`` may be the empty string in review output when no
verified per-ISIN value is available; the CLI warns and importable JSON
refuses until the user supplies it.
The writer renders ``None`` as an empty cell and the CLI prints a
loud warning enumerating which portal fields require manual entry.
Each ``None`` field has a dedicated tracking-issue sub-issue under
``area:fa-a3-complete`` (#10).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from ibkr_fa.invariants import invariant_error, require
from ibkr_fa.models._identifier_checks import INDIA_CBDT_CODE, is_isin_shape
from ibkr_fa.models._row_checks import (
    require_amounts,
    require_dates,
    require_filed_amounts,
    require_max_lengths,
    require_text,
    require_upload_text,
)
from ibkr_fa.models.money import optional_whole_rupees, to_whole_rupees
from ibkr_fa.reference.country_codes import lookup_country
from ibkr_fa.text_checks import first_control_character
from ibkr_fa.types import CountryCode, Isin, Ticker

_COUNTRY_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{2}$")
_NATURE_OF_ENTITY_MAX_LENGTH: Final[int] = 34

# CBDT ITR-2 country codes are numeric strings (e.g., ``"2"`` for the
# United States, ``"1441"`` for Bermuda).
_CBDT_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9]+$")


class FaA3PeakBlankReason(StrEnum):
    """Why the engine left a Table A3 peak value blank (#962).

    Each blank peak has exactly one. The remedy differs by reason, so the
    renderers name it instead of guessing.
    """

    # No series: fetching was off and no complete cache covered the ticker.
    PRICES_NOT_FETCHED = "prices-not-fetched"
    # No series with fetching on, or a series that isn't in USD.
    NO_USD_SERIES = "no-usd-series"
    # A split in the window, or a series not normalised to per-date prices.
    SPLIT = "split"
    # The latest session on or before a day in the window has no close.
    SESSION_WITHOUT_CLOSE = "session-without-close"
    # The latest close on or before a day in the window is too old to carry.
    STALE_CLOSE = "stale-close"
    # No TTBR for the selected peak day (or any candidate day under "inr").
    NO_TTBR = "no-ttbr"
    # The lot was held at the end of no day in the year.
    EMPTY_WINDOW = "empty-window"
    # Fidelity CMA: the peak falls on the disposal day (#622).
    DISPOSAL_DAY_PEAK = "disposal-day-peak"
    # High basis: a session in the window has no valid intraday high.
    SESSION_WITHOUT_HIGH = "session-without-high"
    # High basis: the series' peak sits below the statement's closing value.
    HIGH_BELOW_CLOSING = "high-below-closing"


@dataclass(frozen=True, slots=True)
class FaA3FiledValues:
    """What Schedule FA Table A3 files for one row: whole rupees, rounded half-up.

    Every writer and report reads these, and none of them rounds. A column the
    row leaves blank (manual fill) is ``None``.
    """

    initial_value_inr: Decimal
    peak_value_inr: Decimal | None
    closing_value_inr: Decimal
    gross_amount_paid_credited_inr: Decimal | None
    gross_proceeds_from_sale_inr: Decimal | None


@dataclass(frozen=True, slots=True)
class FaA3Row:
    """One Schedule FA Table A3 row.

    Construction-time invariants (see :meth:`__post_init__`) enforce:

    * Field types, checked first: text is a ``str``, a date is exactly a
      ``date`` (never a ``datetime``), an amount is a finite ``Decimal``.
      Each filed INR amount rounds HALF_UP to a whole rupee within
      ±99,999,999,999,999, the CBDT 14-digit bound (#788).
    * Non-empty entity/country identity fields.
    * Text within the AY 2026-27 CBDT schemas' maximum lengths: country name
      55, entity name 125, address 200, ZIP 8 and nature 34 characters. The
      limits apply to the raw text, so a value that the filing CSV would
      shorten by collapsing commas and whitespace runs is refused too.
      An empty address, ZIP or nature is still allowed (manual fill).
    * Valid ISIN, country code (ISO 3166-1 alpha-2), and CBDT code shape.
    * One country identity: ``country_code`` is the CBDT code the vendored
      country table maps ``source_country_iso2`` to (an unmapped ISO code
      raises :class:`~ibkr_fa.reference.country_codes.CountryCodeNotFoundError`).
      ``country_name`` isn't compared.
    * Not India: ``country_code`` is never ``"91"``, because Table A3 lists
      foreign entities only and its country dropdown leaves India out
      (#1325). Checked after the country match, so it fires only for a
      ``source_country_iso2`` of ``"IN"``.
    * Non-negative INR figures across all populated portal columns.
    * Non-negative cost basis (USD) and positive acquisition TTBR (a zero
      or negative TTBR is a TTBR-loader bug).
    * Closed-lot-shape consistency: when ``audit_disposal_date`` is
      set, ``closing_value_inr`` MUST be zero and
      ``audit_close_price_usd`` MUST be ``None``. The Dec-31 TTBR is
      present only when the row has foreign-source income.
    * Open-lot-shape consistency: when ``audit_disposal_date`` is
      ``None``, both ``audit_close_price_usd`` and
      ``audit_ttbr_dec31`` MUST be populated (lot is open at year-end
      and needs both for the closing value).
    * Peak reason: ``peak_blank_reason`` is a :class:`FaA3PeakBlankReason`
      member exactly when ``peak_value_inr`` is ``None`` (#962).
    * Acquisition date consistency: ``date_of_acquiring`` and
      ``audit_acquisition_date`` MUST be the same date (the audit
      column exists to preserve the same value distinctly for the
      output CSV, not as an independent value).
    * Upload text (#1329), checked last so every check above keeps its
      message: a name, address, ZIP or nature with a character other than
      whitespace must keep one in its bulk-upload rendering
      (:func:`~ibkr_fa.models._row_checks.upload_text`), so a value of only
      commas and spaces is refused. An empty or whitespace-only value is left
      to the row checks. A whitespace-only name that passes them is named for
      manual fill (#1369); the filing CSV requires ASCII and writes it blank.
    * No control character (#1367), checked after the upload text: a name,
      address, ZIP or nature, in that order, with a character below a space
      or DEL (:func:`~ibkr_fa.text_checks.first_control_character`) is refused
      at its first one. The message names the field and the 1-based position,
      never the value or the character, as ``user.toml``'s does (#1045).

    Attributes:
        country_name: Full country name as the portal accepts it
            (e.g., ``"United States"``). Sourced from the
            ``itr_country_codes`` lookup.
        country_code: CBDT ITR-2 country code as a string
            (e.g., ``"2"`` for the United States). NOT the ISO 3166
            alpha-2 code. Must be the code :attr:`source_country_iso2`
            maps to.
        name_of_entity: Issuer name (e.g., ``"EXAMPLE HOLDINGS INC"``).
            Sourced from :attr:`OpenLot.description` /
            :attr:`ClosedLot.description`. Never empty; a whitespace-only
            name that passes the row checks is named for manual fill. The
            filing CSV requires ASCII and writes it blank (#1369).
        address_of_entity: Issuer address from source-proved or configured
            per-ISIN evidence; empty when none supplies it (#1289).
        zip_code: Issuer postal code from the same evidence; empty when none
            supplies it.
        nature_of_entity: Verified free text describing the entity. Empty
            means manual-fill; populated values are limited to 34 characters
            by the official AY 2026-27 schema.
        date_of_acquiring: Acquisition date of the lot.
        initial_value_inr: Acquisition value in INR at TTBR on
            :attr:`date_of_acquiring`: the cash paid for a purchase, and
            the FMV at acquisition for an ESPP purchase (ADR-0003
            decision 2) or an RSU vest. Full precision; :attr:`filed`
            holds the whole-rupee value every writer renders.
        peak_value_inr: Peak value during the CY (issue #14). By default,
            the maximum native-currency ``close_usd x quantity`` over the
            holding window is selected first and then converted at that
            date's SBI TTBR. The warned ``peak_currency="inr"`` override
            instead maximizes the daily converted value.
            Full precision; :attr:`filed` holds the whole-rupee value
            every writer renders. ``None``
            when no price series was fetched or read from a complete
            cache, or the holding's price could not be established with
            certainty (for example, an unknown/non-USD ticker, a stock
            split after the lot's first in-year day and on or before the
            earlier of its disposal date and December 31, a split in a
            price series not normalized to contemporaneous per-date
            prices, or a price gap too large to carry forward) — it
            renders as an empty cell with a CLI manual-fill warning,
            never a guess. Under the close basis, an open lot acquired on
            December 31 takes its closing value without a price series.
            See :mod:`ibkr_fa.compute.fa_a3_peak_value`.
        closing_value_inr: INR value on Dec 31 of the CY for lots open
            at year-end; ``Decimal("0")`` for lots disposed during the
            CY (subject to G1 confirmation that this matches portal
            expectation for disposed lots).
        gross_amount_paid_credited_inr: Sum of distributions
            credited per holding during the CY, in INR (gross,
            before withholding tax), converted using the December
            31 SBI TTBR. Populated when the input
            contains a Dividends section (Activity Statement post-
            issue #31); ``None`` when absent (Flex Query inputs).
            With multiple lots of the same symbol, the full
            per-symbol total is attributed to the FIRST lot in
            input order; subsequent lots get :class:`Decimal` 0
            (preserves the per-symbol total at portal-sum time
            without double-counting; PR #137 P1.1). ``None``
            renders as an empty cell with a CLI manual-fill
            warning. See ``feat(compute): gross amount paid/
            credited`` (sub-issue of #10) and #31.
        gross_proceeds_from_sale_inr: Sale proceeds in INR for closed
            lots with proven pre-fee consideration, or Robinhood's
            recorded Amount filed as consideration (#1383) —
            ``gross_proceeds_money * TTBR(December 31)``, substituting
            ``gross_proceeds_money_exact`` when the Decimal rounds its share;
            ``None`` when the source has neither;
            :class:`Decimal` 0 for open lots. Full precision; :attr:`filed`
            holds the whole-rupee value every writer renders.
        source_isin: ISIN of the source lot (audit-only).
        source_ticker: Ticker of the source lot (audit-only).
        audit_cost_basis_usd: Cost basis in USD before INR conversion
            (audit-only).
        audit_acquisition_date: Same as :attr:`date_of_acquiring` but
            preserved as a separate audit field for clarity in the
            output CSV.
        audit_close_price_usd: Closing price per share on Dec 31 in
            USD (audit-only). ``None`` for lots disposed during the CY
            (no Dec 31 close needed).
        audit_ttbr_acquisition: TTBR used to convert the cost basis
            (audit-only).
        audit_ttbr_dec31: TTBR used to convert the closing value
            and/or gross credits (audit-only). For a disposed lot,
            populated when a gross credit or gross proceeds is non-zero.
        audit_disposal_date: Date the lot was disposed (audit-only).
            ``None`` for lots still open on Dec 31.
        peak_blank_reason: Why :attr:`peak_value_inr` is blank; set
            exactly when it is ``None`` (#962).
    """

    # ---- Portal-facing columns (12 in total, ordered as on-screen) ----
    country_name: str
    country_code: str
    name_of_entity: str
    address_of_entity: str
    zip_code: str
    nature_of_entity: str
    date_of_acquiring: date
    initial_value_inr: Decimal
    peak_value_inr: Decimal | None
    closing_value_inr: Decimal
    gross_amount_paid_credited_inr: Decimal | None
    gross_proceeds_from_sale_inr: Decimal | None

    # ---- Audit columns (not for portal entry) ----
    source_isin: Isin
    source_ticker: Ticker
    source_country_iso2: CountryCode
    audit_cost_basis_usd: Decimal
    audit_acquisition_date: date
    audit_close_price_usd: Decimal | None
    audit_ttbr_acquisition: Decimal
    audit_ttbr_dec31: Decimal | None
    audit_disposal_date: date | None

    # ---- Engine note (not a column) ----
    peak_blank_reason: FaA3PeakBlankReason | None

    @property
    def filed(self) -> FaA3FiledValues:
        """The whole-rupee amounts Schedule FA Table A3 files for this row."""
        return FaA3FiledValues(
            initial_value_inr=to_whole_rupees(self.initial_value_inr),
            peak_value_inr=optional_whole_rupees(self.peak_value_inr),
            closing_value_inr=to_whole_rupees(self.closing_value_inr),
            gross_amount_paid_credited_inr=optional_whole_rupees(
                self.gross_amount_paid_credited_inr
            ),
            gross_proceeds_from_sale_inr=optional_whole_rupees(self.gross_proceeds_from_sale_inr),
        )

    def __post_init__(self) -> None:
        """Fail-fast structural invariants. See class docstring."""
        # ---- Types, before any check that would fail on a wrong one ------
        require_text(
            self,
            "country_name",
            "country_code",
            "name_of_entity",
            "address_of_entity",
            "zip_code",
            "nature_of_entity",
            "source_isin",
            "source_ticker",
            "source_country_iso2",
        )
        require_dates(self, "date_of_acquiring", "audit_acquisition_date")
        require_dates(self, "audit_disposal_date", optional=True)
        require_filed_amounts(self, "initial_value_inr", "closing_value_inr")
        require_filed_amounts(
            self,
            "peak_value_inr",
            "gross_amount_paid_credited_inr",
            "gross_proceeds_from_sale_inr",
            optional=True,
        )
        require_amounts(self, "audit_cost_basis_usd", "audit_ttbr_acquisition")
        require_amounts(self, "audit_close_price_usd", "audit_ttbr_dec31", optional=True)

        # ---- Identity / format invariants -------------------------------
        require(
            self.country_name != "",
            what="FaA3Row.country_name must be non-empty",
        )
        require(
            _CBDT_CODE_PATTERN.fullmatch(self.country_code) is not None,
            what="FaA3Row.country_code must be a numeric CBDT ITR-2 code",
            country_code=self.country_code,
        )
        require(
            self.name_of_entity != "",
            what="FaA3Row.name_of_entity must be non-empty",
        )
        require(
            self.nature_of_entity == "" or self.nature_of_entity.strip() != "",
            what="FaA3Row.nature_of_entity must be empty or contain non-whitespace text",
            nature_of_entity=self.nature_of_entity,
        )
        require(
            len(self.nature_of_entity) <= _NATURE_OF_ENTITY_MAX_LENGTH,
            what="FaA3Row.nature_of_entity must be at most 34 characters",
            nature_of_entity=self.nature_of_entity,
        )
        require_max_lengths(
            self, country_name=55, name_of_entity=125, address_of_entity=200, zip_code=8
        )
        require(
            self.source_ticker != "",
            what="FaA3Row.source_ticker must be non-empty",
        )
        require(
            is_isin_shape(self.source_isin),
            what="FaA3Row.source_isin must match ISO 6166",
            source_isin=self.source_isin,
        )
        require(
            _COUNTRY_CODE_PATTERN.fullmatch(self.source_country_iso2) is not None,
            what="FaA3Row.source_country_iso2 must be ISO 3166-1 alpha-2",
            source_country_iso2=self.source_country_iso2,
        )
        require(
            self.country_code == lookup_country(self.source_country_iso2).cbdt_code,
            what="FaA3Row.country_code must match the CBDT code for source_country_iso2",
        )
        # Table A3's country dropdown and the CBDT schemas'
        # ``CountryCodeExcludingIndia`` leave India out (#1325).
        require(
            self.country_code != INDIA_CBDT_CODE,
            what="FaA3Row.country_code must not be India (Table A3 is foreign assets only)",
        )

        # ---- INR field signs --------------------------------------------
        require(
            self.initial_value_inr >= 0,
            what="FaA3Row.initial_value_inr must be non-negative",
            initial_value_inr=self.initial_value_inr,
        )
        require(
            self.closing_value_inr >= 0,
            what="FaA3Row.closing_value_inr must be non-negative",
            closing_value_inr=self.closing_value_inr,
        )
        for name, value in (
            ("peak_value_inr", self.peak_value_inr),
            ("gross_amount_paid_credited_inr", self.gross_amount_paid_credited_inr),
            ("gross_proceeds_from_sale_inr", self.gross_proceeds_from_sale_inr),
        ):
            require(
                value is None or value >= 0,
                what=f"FaA3Row.{name} must be None or non-negative",
                value=value,
            )

        require(
            self.peak_blank_reason is None
            or isinstance(self.peak_blank_reason, FaA3PeakBlankReason),
            what="FaA3Row.peak_blank_reason must be None or a FaA3PeakBlankReason enum member",
            peak_blank_reason=self.peak_blank_reason,
        )
        require(
            (self.peak_value_inr is None) == (self.peak_blank_reason is not None),
            what="FaA3Row.peak_blank_reason must be set exactly when peak_value_inr is blank",
            peak_value_inr=self.peak_value_inr,
            peak_blank_reason=self.peak_blank_reason,
        )

        # ---- USD / TTBR audit-field signs -------------------------------
        require(
            self.audit_cost_basis_usd >= 0,
            what=(
                "FaA3Row.audit_cost_basis_usd must be non-negative "
                "(zero-cost lots — e.g., corporate-action lots that "
                "OpenLot/ClosedLot also allow at zero — propagate "
                "through here)"
            ),
            audit_cost_basis_usd=self.audit_cost_basis_usd,
        )
        require(
            self.audit_ttbr_acquisition > 0,
            what="FaA3Row.audit_ttbr_acquisition must be positive",
            audit_ttbr_acquisition=self.audit_ttbr_acquisition,
        )
        require(
            self.audit_close_price_usd is None or self.audit_close_price_usd >= 0,
            what="FaA3Row.audit_close_price_usd must be None or non-negative",
            audit_close_price_usd=self.audit_close_price_usd,
        )
        require(
            self.audit_ttbr_dec31 is None or self.audit_ttbr_dec31 > 0,
            what="FaA3Row.audit_ttbr_dec31 must be None or positive",
            audit_ttbr_dec31=self.audit_ttbr_dec31,
        )

        # ---- Acquisition-date consistency -------------------------------
        require(
            self.date_of_acquiring == self.audit_acquisition_date,
            what=(
                "FaA3Row.date_of_acquiring and audit_acquisition_date must "
                "match (audit column exists to preserve the same value "
                "distinctly for the output CSV)"
            ),
            date_of_acquiring=self.date_of_acquiring,
            audit_acquisition_date=self.audit_acquisition_date,
        )

        # ---- Open-lot vs closed-lot shape consistency -------------------
        # A disposed row has no Dec-31 close, but may use the Dec-31
        # TTBR for Schedule FA gross credits. An open lot always needs the
        # rate for its closing value.
        if self.audit_disposal_date is not None:
            require(
                self.closing_value_inr == 0,
                what=(
                    "FaA3Row with audit_disposal_date set must have "
                    "closing_value_inr == 0 (disposed lots have no year-end "
                    "balance)"
                ),
                audit_disposal_date=self.audit_disposal_date,
                closing_value_inr=self.closing_value_inr,
            )
            require(
                self.audit_close_price_usd is None,
                what=(
                    "FaA3Row with audit_disposal_date set must have "
                    "audit_close_price_usd is None (no Dec 31 close needed "
                    "for a disposed lot)"
                ),
                audit_disposal_date=self.audit_disposal_date,
                audit_close_price_usd=self.audit_close_price_usd,
            )
            gross_credits_use_dec31_ttbr = any(
                value is not None and value > 0
                for value in (
                    self.gross_amount_paid_credited_inr,
                    self.gross_proceeds_from_sale_inr,
                )
            )
            require(
                (self.audit_ttbr_dec31 is not None) == gross_credits_use_dec31_ttbr,
                what=(
                    "FaA3Row disposed-lot audit_ttbr_dec31 must be set exactly "
                    "when Schedule FA gross credits use it"
                ),
                audit_disposal_date=self.audit_disposal_date,
                audit_ttbr_dec31=self.audit_ttbr_dec31,
                gross_credits_use_dec31_ttbr=gross_credits_use_dec31_ttbr,
            )
        else:
            require(
                self.audit_close_price_usd is not None,
                what=(
                    "FaA3Row with audit_disposal_date None (lot open at "
                    "year-end) must have audit_close_price_usd populated"
                ),
            )
            require(
                self.audit_ttbr_dec31 is not None,
                what=(
                    "FaA3Row with audit_disposal_date None (lot open at "
                    "year-end) must have audit_ttbr_dec31 populated"
                ),
            )

        # ---- Upload rendering (#1329) -----------------------------------
        # Last, so every earlier refusal keeps its message.
        require_upload_text(
            self, "name_of_entity", "address_of_entity", "zip_code", "nature_of_entity"
        )

        # ---- Control characters (#1367) ---------------------------------
        # After #1329, so every earlier refusal keeps its message. The message
        # gives the position, never the value or the character (#1045).
        for name in ("name_of_entity", "address_of_entity", "zip_code", "nature_of_entity"):
            found = first_control_character(getattr(self, name))
            if found is not None:
                raise invariant_error(
                    what=(
                        f"FaA3Row.{name} has a control character at position {found[0] + 1}; "
                        "filed text may not contain control characters"
                    )
                )
