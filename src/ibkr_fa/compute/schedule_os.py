"""Compute Schedule OS ordinary-dividend lines with source attribution.

This module is the OS counterpart to :mod:`ibkr_fa.compute.fa_a3`. It
takes parsed :class:`~ibkr_fa.models.dividend.Dividend` records plus the
selected :class:`~ibkr_fa.fx.policy.AuditedFxPolicy` (Rule 115 for v1, per
Rule 115(1), Explanation (2)(e)) and produces a :class:`ScheduleOsComputeResult` ready for
the OS writer.

Every supported ordinary dividend feeds Schedule OS row 1a(i). The
compute result retains separate foreign- and Indian-source groups only
because FSI/TR/Form 67 require country attribution. Both groups share
the same Rule 115 conversion. An in-FY return of capital, long-term
capital gain or cash in lieu refuses with
:class:`NonOrdinaryDistributionError` (#1310), country-agnostically, so an
Indian-issuer ``Return Of Capital`` refuses rather than landing under the
Indian-source 1a(i) bucket. An in-FY payment in lieu of a dividend refuses with
:class:`PaymentInLieuOfDividendError` (#1281). Malformed descriptions (missing ``SYMBOL(ISIN)
<whitespace>`` structural prefix, missing ``" USD "`` marker, or a known
kind whose text after the prefix isn't the supported
``<kind> USD <amount> per Share[ (Ordinary Dividend)]`` grammar or
``Cash in Lieu USD <amount>``) fail
fast regardless of issuer country.

Classifier design — allow-list dispatch:

Every source sets :attr:`Dividend.kind`, a typed
:class:`~ibkr_fa.models.DistributionKind`, when it builds the
:class:`Dividend`: the IBKR Activity Statement parser classifies the
description, and the other source adapters set the kind directly. The
compute layer dispatches on the enum:

* :data:`~ibkr_fa.models.ALLOWED_DISTRIBUTION_KINDS` (currently
  ``{CASH_DIVIDEND, SPECIAL_DIVIDEND, CHOICE_DIVIDEND}``) is the
  set of allow-listed *primary* distribution-type labels IBKR emits
  in the Activity Statement's Dividends-section description prefix.
  The parser's
  :func:`~ibkr_fa.parser.activity_statement.dividends.classify_distribution`
  extracts the leading
  "kind" of the type-slice (the substring before ``" USD "``) and
  matches it **exactly** (case-insensitive) against the enum values.
  Exact-match (not ``startswith``) is critical: a novel IBKR label
  like ``"Cash Dividend In Lieu Of Stock USD ..."`` whose first two
  words collide with ``"Cash Dividend"`` would otherwise be silently
  accepted as ordinary; the exact-kind dispatch routes it to
  :attr:`DistributionKind.UNRECOGNISED` at parse time → the compute
  layer raises :class:`UnrecognisedDistributionTypeError` with
  ``violation="unsupported_kind"``, exactly as the fail-fast contract
  promises. ``"Ordinary Dividend"`` is **not** in the allow-list
  because IBKR uses it only as a parenthesised sub-qualifier within
  the description (e.g.,
  ``"XMPL(US…) Cash Dividend USD 0.25 per Share (Ordinary Dividend)"``);
  the kind-extractor's ``split(" USD ", 1)[0]`` slices off the
  qualifier before dispatch.
* :attr:`DistributionKind.PAYMENT_IN_LIEU_OF_DIVIDEND` isn't
  allow-listed: an in-FY row refuses with :class:`PaymentInLieuOfDividendError`
  (#1281). It's other income, not a dividend. If its withholding shares
  an ordinary dividend's (ISIN, payment date) key, leaving it out would
  credit that withholding against the dividend.
* :data:`NON_ORDINARY_DISTRIBUTION_KINDS` (Return Of Capital, Long-Term
  Capital Gain, Cash in Lieu) aren't allow-listed either: an in-FY row
  refuses with :class:`NonOrdinaryDistributionError` (#1310). Leaving one
  out could credit its withholding against an ordinary dividend, and none
  of them is filed anywhere else. Schedule CG and Schedule FA refuse them
  too, through :func:`require_no_non_ordinary_distributions`.
* :attr:`DistributionKind.UNRECOGNISED` and
  :attr:`DistributionKind.MALFORMED` are the two parse-time failure
  markers — the parser couldn't classify the kind keyword, or
  couldn't find the ``" USD "`` currency marker or found a known kind
  whose whole type-slice doesn't match the supported grammar,
  respectively. The compute layer raises
  :class:`UnrecognisedDistributionTypeError` — fail-fast, *no silent
  drop* — naming a ``MALFORMED`` row ``"missing_usd_marker"`` when its
  type-slice has no ``" USD "`` and ``"malformed_type_slice"``
  otherwise. Plus a compute-time-only
  violation, ``"missing_kind_prefix"``, fires for a :class:`Dividend`
  built with ``kind`` and ``type_slice`` left at their defaults. No
  production path builds one: the IBKR parser raises
  :class:`~ibkr_fa.exceptions.ParseError` on a description without
  the structural prefix, and every other source sets ``kind``.

Both line types copy the dividend's ``kind`` and require it to be
allow-listed, so the review writers render ``line.kind.value``
instead of re-parsing ``description``.

Source-country grouping:

``lines`` contains foreign-source rows and ``domestic_lines`` contains
Indian-source rows. The optional ADR setting changes this source
attribution for FSI/TR/Form 67; it never changes the one 1a(i) portal
total. Rows 1a(ii) and 1a(iii) are reserved for explicitly evidenced
sections 2(22)(e) and 2(22)(f), which this ordinary-dividend parser
does not infer.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal, Self

from ibkr_fa.compute.exact_inr import exact_inr_conversion, exact_inr_sum
from ibkr_fa.compute.schedule_os_other_income import (
    ScheduleOsOtherIncomeLine,
    grouped_other_income_totals,
)
from ibkr_fa.compute.schedule_os_periods import (
    DIVIDEND_LABEL,
    DividendAmount,
    ScheduleOsPeriodAmount,
    allocate_period_rupees,
    split_dividend_periods,
)
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.policy import AuditedFxPolicy
from ibkr_fa.invariants import require
from ibkr_fa.models import (
    ALLOWED_DISTRIBUTION_KINDS,
    DistributionKind,
    Dividend,
)
from ibkr_fa.models._identifier_checks import is_iso_alpha2
from ibkr_fa.models.money import require_filed_amount
from ibkr_fa.reference.indian_adr_mapping import lookup_indian_adr
from ibkr_fa.types import Isin, Ticker


def _require_ordinary_dividend_fields(
    line: ScheduleOsDividendLine | ScheduleOsDomesticDividendLine, class_name: str
) -> None:
    """Run the seven checks both dividend-line classes start with, in order.

    Each message starts with ``class_name``.
    """
    require(
        line.amount_gross_usd > 0,
        what=f"{class_name}.amount_gross_usd must be positive",
        amount_gross_usd=line.amount_gross_usd,
        symbol=line.symbol,
        isin=line.isin,
    )
    require(
        line.fx_rate_inr_per_usd > 0,
        what=f"{class_name}.fx_rate_inr_per_usd must be positive",
        fx_rate_inr_per_usd=line.fx_rate_inr_per_usd,
        symbol=line.symbol,
        isin=line.isin,
    )
    require(
        line.amount_gross_inr > 0,
        what=f"{class_name}.amount_gross_inr must be positive",
        amount_gross_inr=line.amount_gross_inr,
        symbol=line.symbol,
        isin=line.isin,
    )
    require_filed_amount(
        line.amount_gross_inr,
        what=f"{class_name}.amount_gross_inr",
        symbol=line.symbol,
        isin=line.isin,
    )
    require(
        line.kind in ALLOWED_DISTRIBUTION_KINDS,
        what=f"{class_name}.kind must be an allow-listed ordinary-dividend kind",
        kind=line.kind,
        symbol=line.symbol,
        isin=line.isin,
    )
    require(
        line.fx_specified_date <= line.payment_date,
        what=(
            f"{class_name}.fx_specified_date must be on or before payment_date "
            "(Rule 115(1), Explanation (2)(e) preceding-month-end is strictly "
            "before payment_date)"
        ),
        fx_specified_date=line.fx_specified_date,
        payment_date=line.payment_date,
        symbol=line.symbol,
        isin=line.isin,
    )
    require(
        line.fx_rate_publication_date <= line.fx_specified_date,
        what=(
            f"{class_name}.fx_rate_publication_date must be on or before "
            "fx_specified_date (carry-back goes strictly backwards in time; "
            "equal when no carry-back occurred)"
        ),
        fx_rate_publication_date=line.fx_rate_publication_date,
        fx_specified_date=line.fx_specified_date,
        symbol=line.symbol,
        isin=line.isin,
    )


@dataclass(frozen=True, slots=True)
class ScheduleOsDividendLine:
    """One foreign-source ordinary dividend line feeding Schedule OS 1a(i).

    Each instance represents a single in-FY, **non-Indian-source**,
    allow-list-classified dividend payment with its INR conversion
    pre-computed at full :class:`~decimal.Decimal` precision. The
    writer applies CBDT-FAQ rounding (HALF_UP to whole rupees) at
    serialisation time.

    The separate Indian-source counterpart keeps FSI/FTC source
    attribution explicit; writers combine both types into one 1a(i)
    total.

    Attributes:
        symbol: Ticker of the paying instrument.
        isin: ISIN of the paying instrument (ISO 6166).
        issuer_country_iso2: ISO 3166-1 alpha-2 country code derived
            from the ISIN's leading two characters. Always non-``"IN"``
            in this dataclass — Indian-source rows live in
            :attr:`ScheduleOsComputeResult.domestic_lines` as
            :class:`ScheduleOsDomesticDividendLine` instances.
        payment_date: IBKR cash-credit date of the dividend.
        fx_specified_date: The Rule 115(1), Explanation (2)(e) "specified date" — i.e.,
            the last day of the calendar month immediately preceding
            :attr:`payment_date`'s month. This is the *statutory*
            lookup key before any policy-internal carry-back; the
            actual published rate applied by
            :meth:`FxPolicy.rate_for` may come from an earlier
            archive date (Saturday/Sunday/holiday carry-back). Always
            ``<= payment_date`` (preceding-month-end is strictly
            before payment_date).
        fx_rate_publication_date: The SBI archive date the rate
            actually came from. Equals :attr:`fx_specified_date` when
            no carry-back was needed (SBI published on the statutory
            specified date itself); strictly earlier than
            :attr:`fx_specified_date` when carry-back resolved a
            weekend / holiday / unpublished day to the most recent
            prior published row. Exposed so the review CSV can show
            an auditor a date-rate pair that exactly matches an
            actual SBI TTBR archive row, closing the audit gap
            tracked at #160.
        fx_rate_inr_per_usd: The INR-per-USD rate actually applied
            by :meth:`FxPolicy.rate_for` for this payment. Strictly
            positive.
        amount_gross_usd: Gross dividend amount in USD (mirror of
            :attr:`Dividend.amount_gross`). Strictly positive.
        amount_gross_inr: ``amount_gross_usd * fx_rate_inr_per_usd``
            at full :class:`~decimal.Decimal` precision. The writer
            quantizes to whole rupees at output time.
        description: Verbatim audit copy of :attr:`Dividend.description`,
            preserved so the user can trace any OS line back to the
            exact IBKR row.
        kind: Copy of :attr:`Dividend.kind`. The review writers render
            its value in the "Distribution type" column.
        source_account_index: Optional privacy-safe source-account
            index copied from :class:`Dividend`. It does not affect
            Schedule OS arithmetic; Form 67 keys each payment on it (#890).

    Invariants enforced in :meth:`__post_init__`:

    * ``amount_gross_usd > 0``
    * ``fx_rate_inr_per_usd > 0``
    * ``amount_gross_inr > 0``, rounding HALF_UP within the CBDT 14-digit
      bound: every filed 1a(i) total adds it up (#830)
    * ``kind in ALLOWED_DISTRIBUTION_KINDS``
    * ``fx_specified_date <= payment_date`` (Rule 115 backward semantic)
    * ``fx_rate_publication_date <= fx_specified_date``
      (carry-back goes strictly backwards in time)
    * ``issuer_country_iso2 != "IN"`` (post-filter)
    * ``issuer_country_iso2`` is a 2-letter uppercase ISO 3166-1
      alpha-2 code.
    """

    symbol: Ticker
    isin: Isin
    issuer_country_iso2: str
    payment_date: date
    fx_specified_date: date
    fx_rate_publication_date: date
    fx_rate_inr_per_usd: Decimal
    amount_gross_usd: Decimal
    amount_gross_inr: Decimal
    description: str
    kind: DistributionKind
    source_account_index: int | None = None

    def __post_init__(self) -> None:
        _require_ordinary_dividend_fields(self, "ScheduleOsDividendLine")
        require(
            self.issuer_country_iso2 != "IN",
            what=(
                "ScheduleOsDividendLine.issuer_country_iso2 cannot be 'IN' "
                "(Indian-source rows belong in ScheduleOsComputeResult."
                "domestic_lines for FSI/FTC source attribution)"
            ),
            issuer_country_iso2=self.issuer_country_iso2,
            symbol=self.symbol,
            isin=self.isin,
        )
        require(
            is_iso_alpha2(self.issuer_country_iso2),
            what=("issuer_country_iso2 must be a 2-letter uppercase ISO 3166-1 alpha-2 code"),
            issuer_country_iso2=self.issuer_country_iso2,
            symbol=self.symbol,
            isin=self.isin,
        )
        if self.source_account_index is not None:
            require(
                self.source_account_index >= 0,
                what=(
                    "ScheduleOsDividendLine.source_account_index must be non-negative when provided"
                ),
                source_account_index=self.source_account_index,
            )


@dataclass(frozen=True, slots=True)
class ScheduleOsDomesticDividendLine:
    """One Indian-source ordinary dividend line feeding Schedule OS 1a(i).

    Carries a dividend whose source ISIN starts with ``"IN"`` (per
    IT Act §2(20A) "Indian company" — formed and registered under
    the Companies Act in India). v1 (PR #182 / issue #158) only
    routes here for direct Indian-share holdings (rare IBKR case
    where Indian residents have direct-market-access to NSE/BSE);
    Indian-company ADRs / GDRs (the much more common IBKR case for
    Indian-company exposure from a US-based account) carry US- /
    Jersey- / Cayman-prefixed ISINs and are NOT routed here in v1.
    See the module docstring's "Indian-source ISIN filter" section
    for the full rationale and the asset-as-held vs underlying-issuer
    policy choice; issue #183 tracks an opt-in ADR mapping for users
    who want the §9(1)(iv) underlying-issuer attribution.

    Structurally identical to :class:`ScheduleOsDividendLine`; the
    distinct type preserves Indian-source attribution for FSI/FTC.

    Pre-#158 v1 (issue #31): IN-issuer ordinary dividends were
    routed to the writer's ``_excluded_indian_source:`` audit block as
    raw :class:`Dividend` objects with no FX conversion; the carve-out
    rationale was that v1 didn't have a 1a(i) portal label or compute
    layer. Issue #158 promotes them to first-class
    :class:`ScheduleOsDomesticDividendLine` rows with the same Rule
    115 FX treatment as foreign-source lines.

    Attributes:
        symbol: Ticker of the paying instrument as IBKR emits it. For
            a direct Indian-share dividend this is the NSE / BSE
            ticker (e.g., ``Ticker("INFY")``). For an ADR re-routed
            here via the issue #183 underlying-issuer mapping, this
            is still the ADR ticker IBKR emits (e.g., ``Ticker("INFY")``
            for Infosys ADR) — the audit trail surfaces the original
            ADR ISIN separately via :attr:`adr_source_isin`.
        isin: ISIN of the paying instrument (ISO 6166). For direct
            Indian-share dividends, starts with ``"IN"``. For ADR re-
            routed dividends, this is the **ADR's** ISIN (US / Jersey
            / Marshall Islands / Cayman prefix) — NOT rewritten to
            an IN-prefix synthetic value, because the rewriting would
            destroy the audit trail back to the actual IBKR row.
            Whether the ISIN starts with ``"IN"`` is enforced via the
            cross-field invariant with :attr:`adr_source_isin`: if
            ``adr_source_isin is None``, this must start with ``"IN"``;
            if ``adr_source_isin is not None``, this MUST equal
            ``adr_source_isin`` (the ADR ISIN; not IN-prefixed).
        issuer_country_iso2: Always ``"IN"``. For direct Indian-share
            dividends, this matches ``isin[:2]``. For ADR re-routed
            dividends, this is the **synthetic Indian-issuer country**
            derived from the underlying-issuer mapping — explicitly
            different from ``isin[:2]`` (which is the depositary
            country). The cross-field invariant enforces this:
            ``adr_source_isin is None`` → ``isin[:2] == "IN"``;
            ``adr_source_isin is not None`` → ``isin[:2] != "IN"``
            (and ``issuer_country_iso2 == "IN"`` regardless).
        adr_source_isin: ``None`` for direct Indian-share dividends;
            the ADR's original ISIN for issue #183 re-routed dividends
            (always equals :attr:`isin` in the re-routed case). Used
            by the writer's audit-trail column to surface the BEFORE
            state ("US4567881085 → re-routed as INFY / Infosys Ltd /
            Indian-source per user.toml [os].schedule_os_indian_adr_routing").
        adr_underlying_issuer_name: ``None`` for direct Indian-share
            dividends; the human-readable underlying-issuer name
            (e.g., ``"Infosys Ltd"``) for issue #183 re-routed
            dividends. Sourced from
            :class:`~ibkr_fa.reference.indian_adr_mapping.IndianAdrEntry.underlying_issuer_name`.
        payment_date: IBKR cash-credit date of the dividend.
        fx_specified_date: The Rule 115(1), Explanation (2)(e) "specified date". See
            :attr:`ScheduleOsDividendLine.fx_specified_date` — same
            semantics. Rule 115's preceding-month-end semantic applies
            to all FY-based income lines regardless of issuer country.
        fx_rate_publication_date: SBI archive date the rate came from.
            Same semantics as
            :attr:`ScheduleOsDividendLine.fx_rate_publication_date`.
        fx_rate_inr_per_usd: INR-per-USD rate applied. Strictly
            positive.
        amount_gross_usd: Gross dividend amount in USD (mirror of
            :attr:`Dividend.amount_gross`). Strictly positive.
        amount_gross_inr: ``amount_gross_usd * fx_rate_inr_per_usd``
            at full :class:`~decimal.Decimal` precision.
        description: Verbatim audit copy of :attr:`Dividend.description`.
        kind: Copy of :attr:`Dividend.kind`. Same semantics as
            :attr:`ScheduleOsDividendLine.kind`.
        source_account_index: Optional privacy-safe source-account
            index copied from :class:`Dividend` for workpaper grouping.

    Invariants enforced in :meth:`__post_init__`:

    * ``amount_gross_usd > 0``
    * ``fx_rate_inr_per_usd > 0``
    * ``amount_gross_inr > 0``, rounding HALF_UP within the CBDT 14-digit
      bound: every filed 1a(i) total adds it up (#830)
    * ``kind in ALLOWED_DISTRIBUTION_KINDS``
    * ``fx_specified_date <= payment_date``
    * ``fx_rate_publication_date <= fx_specified_date``
    * ``issuer_country_iso2 == "IN"`` (positive Indian-source
      constraint; this is the inverse of the foreign-side dataclass)
    * Cross-field ADR-routing invariant:

      * If ``adr_source_isin is None`` AND
        ``adr_underlying_issuer_name is None`` (direct Indian-share
        case): ``isin[:2] == "IN"``.
      * If ``adr_source_isin is not None`` AND
        ``adr_underlying_issuer_name is not None`` (issue #183 ADR-
        re-routed case): ``adr_source_isin == isin`` AND
        ``isin[:2] != "IN"`` (the ADR ISIN's depositary prefix is
        retained, NOT a synthetic IN-prefix).
      * The two ADR fields must both be ``None`` or both be ``not
        None``; one-of-two would be a half-populated audit trail.
    """

    symbol: Ticker
    isin: Isin
    issuer_country_iso2: str
    payment_date: date
    fx_specified_date: date
    fx_rate_publication_date: date
    fx_rate_inr_per_usd: Decimal
    amount_gross_usd: Decimal
    amount_gross_inr: Decimal
    description: str
    kind: DistributionKind
    adr_source_isin: Isin | None = None
    adr_underlying_issuer_name: str | None = None
    source_account_index: int | None = None

    def __post_init__(self) -> None:
        _require_ordinary_dividend_fields(self, "ScheduleOsDomesticDividendLine")
        if self.source_account_index is not None:
            require(
                self.source_account_index >= 0,
                what=(
                    "ScheduleOsDomesticDividendLine.source_account_index must "
                    "be non-negative when provided"
                ),
                source_account_index=self.source_account_index,
            )
        require(
            self.issuer_country_iso2 == "IN",
            what=(
                "ScheduleOsDomesticDividendLine.issuer_country_iso2 must be 'IN' "
                "(foreign-source rows belong in ScheduleOsComputeResult.lines as "
                "ScheduleOsDividendLine; this dataclass is the 1a(i) "
                "domestic-Indian-source bucket only)"
            ),
            issuer_country_iso2=self.issuer_country_iso2,
            symbol=self.symbol,
            isin=self.isin,
        )
        # Cross-field ADR-routing invariant (issue #183): the two ADR
        # fields are coupled — both ``None`` (direct Indian-share case)
        # or both ``not None`` (ADR re-routed case). One-of-two would
        # be a half-populated audit trail.
        adr_fields_populated_count = sum(
            1
            for field_value in (self.adr_source_isin, self.adr_underlying_issuer_name)
            if field_value is not None
        )
        require(
            adr_fields_populated_count in (0, 2),
            what=(
                "ScheduleOsDomesticDividendLine.adr_source_isin and "
                "adr_underlying_issuer_name must both be set or both be None "
                "(one-of-two is a half-populated audit trail)"
            ),
            adr_source_isin=self.adr_source_isin,
            adr_underlying_issuer_name=self.adr_underlying_issuer_name,
            symbol=self.symbol,
            isin=self.isin,
        )
        # PR #223 fix: when populated, adr_underlying_issuer_name
        # must be a non-empty-after-strip string. The both-None-or-
        # both-non-None coupling check above counts an empty / pure-
        # whitespace string as "present" (not None), so a row like
        # ``adr_source_isin="US...", adr_underlying_issuer_name=""``
        # would silently render a blank audit-trail cell on the
        # FA-A3 disclosure. Mirrors the parallel whitespace check
        # on Form67Entry.section_90_dtaa_article at
        # models/form67_entry.py:360-369 (PR #221 r4).
        if self.adr_underlying_issuer_name is not None:
            require(
                self.adr_underlying_issuer_name.strip() != "",
                what=(
                    "ScheduleOsDomesticDividendLine.adr_underlying_issuer_name "
                    "must be a non-empty string when provided — empty / "
                    "whitespace-only values would render a blank audit-trail "
                    "cell on the FA-A3 disclosure (parallel to "
                    "Form67Entry.section_90_dtaa_article whitespace check at "
                    "models/form67_entry.py:360-369)"
                ),
                adr_underlying_issuer_name=self.adr_underlying_issuer_name,
                symbol=self.symbol,
                isin=self.isin,
            )
        if self.adr_source_isin is None:
            # Direct Indian-share case: the ISIN-prefix-derived country
            # must match issuer_country_iso2 == "IN", defending against
            # a stale-country-cache bug at the compute layer.
            require(
                self.isin[:2] == "IN",
                what=(
                    "ScheduleOsDomesticDividendLine.isin must start with 'IN' "
                    "in the direct-Indian-share case (adr_source_isin is None); "
                    "the ISIN-prefix-derived country must match "
                    "issuer_country_iso2 to defend against a stale-country-"
                    "cache bug at the compute layer"
                ),
                isin=self.isin,
                issuer_country_iso2=self.issuer_country_iso2,
                symbol=self.symbol,
            )
        else:
            # Issue #183 ADR-re-routed case: adr_source_isin must equal
            # the ISIN (we don't rewrite the ISIN to a synthetic IN-
            # prefix value — that would destroy the audit trail back
            # to the actual IBKR row), AND the ISIN's first two chars
            # must NOT be "IN" (because the row's ISIN-derived
            # depositary country is by-definition non-Indian — that's
            # the whole reason this branch fires).
            require(
                self.adr_source_isin == self.isin,
                what=(
                    "ScheduleOsDomesticDividendLine.adr_source_isin must equal "
                    "isin in the ADR-re-routed case (we don't synthesise a "
                    "fake IN-prefix ISIN; the original ADR ISIN is preserved "
                    "for audit trail)"
                ),
                adr_source_isin=self.adr_source_isin,
                isin=self.isin,
                symbol=self.symbol,
            )
            require(
                self.isin[:2] != "IN",
                what=(
                    "ScheduleOsDomesticDividendLine.isin must NOT start with 'IN' "
                    "in the ADR-re-routed case (adr_source_isin is not None); "
                    "ADR ISINs carry the depositary's country prefix, not the "
                    "underlying issuer's"
                ),
                isin=self.isin,
                adr_source_isin=self.adr_source_isin,
                symbol=self.symbol,
            )


@dataclass(frozen=True, slots=True)
class ScheduleOsSecuritySplit:
    """One security's 1a(i) dividends in whole rupees per section 10 period.

    The one domestic split holds every Indian-source line, so its ``symbol``
    and ``isin`` are ``None``.
    """

    issuer_country_iso2: str
    symbol: Ticker | None
    isin: Isin | None
    period_whole_rupees: tuple[int, ...]

    @property
    def total_whole_rupees(self) -> int:
        """The split's total: the sum of its periods."""
        return sum(self.period_whole_rupees)


@dataclass(frozen=True, slots=True)
class ScheduleOsAccountSplit:
    """One account's 1a(i) dividends from one issuer country, in whole rupees per period."""

    source_account_index: int
    issuer_country_iso2: str
    period_whole_rupees: tuple[int, ...]

    @property
    def total_whole_rupees(self) -> int:
        """The split's total: the sum of its periods."""
        return sum(self.period_whole_rupees)


@dataclass(frozen=True, slots=True)
class ScheduleOsComputeResult:
    """Schedule OS dividend buckets plus the filing-ready interest total.

    Writers combine both ordinary-dividend source groups into one 1a(i)
    block. The split remains for FSI/FTC source attribution.

    Attributes:
        lines: In-FY, **foreign-source** (issuer-country ``!= "IN"``),
            allow-list-classified ordinary-dividend lines.
        domestic_lines: In-FY, **Indian-source** (issuer-country ``==
            "IN"``), allow-list-classified ordinary-dividend lines.
        interest_total_inr_rounded: Schedule OS 1b(ix) residual
            other-source total, rounded to whole rupees. The historical
            field name predates substitute-dividend support; shared pipelines
            combine cash-balance interest and any separately evidenced
            residual-other-source inputs before invoking a writer. The
            dividend-only compute function leaves the filing value unset.
        other_income_lines: Schedule OS 1e lines, substitute dividends
            included.

    Both ordinary groups receive identical Rule 115(1), Explanation (2)(e) conversion.

    The filed 1a(i), section 10, per-security, per-account and 1e values
    are read-only properties derived from the lines, so they stay correct
    after :func:`dataclasses.replace`.
    """

    lines: list[ScheduleOsDividendLine]
    domestic_lines: list[ScheduleOsDomesticDividendLine]
    interest_total_inr_rounded: Decimal | None = None
    other_income_lines: tuple[ScheduleOsOtherIncomeLine, ...] = ()

    @property
    def dividend_periods(self) -> tuple[ScheduleOsPeriodAmount, ...]:
        """The 1a(i) dividends split into the five section 10 periods."""
        return split_dividend_periods([*self.lines, *self.domestic_lines])

    @property
    def ordinary_dividend_total_inr_full(self) -> Decimal:
        """The 1a(i) total at full precision."""
        return exact_inr_sum(
            (line.amount_gross_inr for line in [*self.lines, *self.domestic_lines]),
            label=DIVIDEND_LABEL,
        )

    @property
    def ordinary_dividend_total_inr_rounded(self) -> Decimal:
        """The filed 1a(i) value: the HALF_UP total, which the periods add up to."""
        rounded = self.ordinary_dividend_total_inr_full.quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        period_sum = sum((period.whole_rupees for period in self.dividend_periods), Decimal("0"))
        require(
            rounded == period_sum,
            what="Schedule OS 1a(i) total must equal the sum of its section 10 periods",
            rounded_total=rounded,
            period_sum=period_sum,
        )
        return rounded

    @property
    def dividend_splits_by_security(self) -> tuple[ScheduleOsSecuritySplit, ...]:
        """The section 10 periods split per foreign security, then one domestic split.

        Foreign securities sort by country, symbol and ISIN. Each period's
        whole rupees across the splits add up to :attr:`dividend_periods`.
        """
        by_security: dict[tuple[str, Ticker, Isin], list[ScheduleOsDividendLine]] = {}
        for line in self.lines:
            key = (line.issuer_country_iso2, line.symbol, line.isin)
            by_security.setdefault(key, []).append(line)
        foreign_keys = sorted(by_security)
        groups: list[Iterable[DividendAmount]] = [by_security[key] for key in foreign_keys]
        keys: list[tuple[str, Ticker | None, Isin | None]] = [*foreign_keys]
        if self.domestic_lines:
            groups.append(self.domestic_lines)
            keys.append(("IN", None, None))
        return tuple(
            ScheduleOsSecuritySplit(
                issuer_country_iso2=iso,
                symbol=symbol,
                isin=isin,
                period_whole_rupees=periods,
            )
            for (iso, symbol, isin), periods in zip(
                keys, self._allocate_period_rupees(groups), strict=True
            )
        )

    @property
    def dividend_splits_by_account(self) -> tuple[ScheduleOsAccountSplit, ...]:
        """The section 10 periods split per source account and issuer country.

        Empty when no dividend line carries an account index; refused when
        only some do. When present, each period's whole rupees across the
        splits add up to :attr:`dividend_periods`.
        """
        all_lines = [*self.lines, *self.domestic_lines]
        if all(line.source_account_index is None for line in all_lines):
            return ()
        require(
            all(line.source_account_index is not None for line in all_lines),
            what="Quicko account view requires account metadata on every dividend line",
        )
        grouped: dict[
            tuple[int, str],
            list[ScheduleOsDividendLine | ScheduleOsDomesticDividendLine],
        ] = {}
        for line in all_lines:
            assert line.source_account_index is not None
            grouped.setdefault(
                (line.source_account_index, line.issuer_country_iso2),
                [],
            ).append(line)
        keys = sorted(grouped)
        return tuple(
            ScheduleOsAccountSplit(
                source_account_index=account_index,
                issuer_country_iso2=iso,
                period_whole_rupees=periods,
            )
            for (account_index, iso), periods in zip(
                keys,
                self._allocate_period_rupees([grouped[key] for key in keys]),
                strict=True,
            )
        )

    def _allocate_period_rupees(
        self, groups: Sequence[Iterable[DividendAmount]]
    ) -> tuple[tuple[int, ...], ...]:
        return allocate_period_rupees(
            groups,
            period_targets=[period.whole_rupees for period in self.dividend_periods],
        )

    @property
    def other_income_totals(self) -> tuple[tuple[str, Decimal, Decimal], ...]:
        """Schedule OS 1e nature, exact INR and whole-rupee totals."""
        return grouped_other_income_totals(self.other_income_lines)


@dataclass(frozen=True, slots=True)
class UnrecognisedDistributionTypeError(IbkrFaError):
    """Raised when a dividend row cannot be safely classified for Schedule OS.

    The compute layer fails fast rather than silently dropping or
    mis-classifying rows — Schedule OS treaty / non-treaty rates
    differ by distribution kind, so a silent miss could cause the
    user to under- or over-report income.

    Four distinct classifier failure modes share this exception type
    via the :attr:`violation` discriminator (see
    :class:`~ibkr_fa.output._writer_contract.WriterContractError` for
    the same shape):

    * ``"missing_kind_prefix"``: the :class:`Dividend` was built
      without a parser-stripped structural prefix (the model's
      :attr:`Dividend.type_slice` is empty AND :attr:`Dividend.kind`
      defaulted to :attr:`DistributionKind.UNRECOGNISED`). The
      production parser raises
      :class:`~ibkr_fa.exceptions.ParseError` upfront via
      :func:`~ibkr_fa.parser.activity_statement._description.parse_symbol_isin_and_type_slice_from_description`
      so this branch is unreachable from a parsed statement — it
      only fires for test fixtures / direct constructors that
      bypass the parser without setting ``kind``. Indicates a
      parser bypass or an upstream IBKR Activity Statement format
      change. The remediation is a parser fix (or a test-fixture
      update), NOT adding a new distribution kind.
    * ``"missing_usd_marker"``: the type-slice (after prefix-strip)
      does not contain the canonical ``" USD "`` currency marker.
      A slice without ``" USD "`` cannot be classified safely even
      if it happens to equal an allow-listed kind (e.g.,
      ``"Cash Dividend"`` with no amount payload would otherwise
      silently route through the allow-list). Indicates a parser
      bypass, schema drift, or a multi-currency dividend (multi-
      currency tracking: #52). The remediation is a parser fix or a
      multi-currency follow-up, NOT adding a new distribution kind.
    * ``"malformed_type_slice"``: the type-slice has ``" USD "`` and
      a known kind label before it, but the whole slice isn't
      ``<kind> USD <amount> per Share[ (Ordinary Dividend)]`` (or
      ``Cash in Lieu USD <amount>``): another qualifier, a suffix, a
      second amount, a sign or a footnote. The parser classified it
      :attr:`DistributionKind.MALFORMED` rather than trust the label
      alone. The remediation is a parser fix, NOT adding a new
      distribution kind.
    * ``"unsupported_kind"``: the type-slice is well-formed and the
      derived distribution kind is in neither the allow nor the
      reject list — i.e., a genuinely novel IBKR distribution kind.
      The remediation IS to add the kind to the allow-list or the
      reject-list (with a follow-up issue link).

    This exception lives in the compute layer (not
    :mod:`ibkr_fa.exceptions`) because its semantic is scoped to
    Schedule OS dispatch. It carries only the violation and the row's
    Dividends-section index; nothing else from the row, which is input
    (#1047, #1285). Its :meth:`__str__` is fixed text naming the row
    index and the rule, with an actionable remediation.

    Use the :meth:`from_dividend` classmethod factory to build an
    instance from a :class:`Dividend` plus the violation discriminator.

    Attributes:
        violation: Discriminator naming which classifier failure mode
            triggered. Typed as ``Literal[...]`` so ``ty`` statically
            rejects typos at construction call sites; the
            :meth:`__str__` defensive branch handles bypassed-type-
            system constructions (same pattern as
            :class:`~ibkr_fa.output._writer_contract.WriterContractError`).
        source_row_index: 0-based index of the offending row in the
            source IBKR Dividends section CSV, threaded by the parser
            via :attr:`Dividend.source_row_index` (#154). ``None``
            when the :class:`Dividend` was constructed outside the
            parser path (test fixtures, future direct-construction
            callers). Rendered into :meth:`__str__` when not None so
            the user can jump straight to the offending row in their
            source statement.
    """

    violation: Literal[
        "missing_kind_prefix", "missing_usd_marker", "malformed_type_slice", "unsupported_kind"
    ]
    source_row_index: int | None = None

    @classmethod
    def from_dividend(
        cls,
        dividend: Dividend,
        *,
        violation: Literal[
            "missing_kind_prefix", "missing_usd_marker", "malformed_type_slice", "unsupported_kind"
        ],
    ) -> Self:
        """Build an instance from a :class:`Dividend` + violation discriminator.

        Keeps only the violation and the row's
        :attr:`Dividend.source_row_index` (#1285).

        Args:
            dividend: The offending :class:`Dividend` row.
            violation: Discriminator naming which classifier failure
                mode triggered (see class docstring for the four
                values and their remediations).
        """
        return cls(violation=violation, source_row_index=dividend.source_row_index)

    def __str__(self) -> str:
        # Fixed text only: the Description, symbol, ISIN, date and amounts
        # are input, so the message names the row and the rule (#1047).
        row_clause = (
            f" (Dividends-section row index {self.source_row_index}; 0-based,"
            f" so data row {self.source_row_index + 1})"
            if self.source_row_index is not None
            else ""
        )
        do_not_paste = (
            "describe its wording in your own words; don't paste the"
            " Description or the row's symbol, ISIN, date or amounts"
        )
        if self.violation == "unsupported_kind":
            return (
                "Unrecognised dividend distribution type in the Dividends"
                f" section{row_clause}: the row's distribution kind (the"
                " Description's leading words before the currency code) is"
                " not in the allow / reject list. No Schedule OS output was"
                " written for this run. Locate the row in your IBKR Activity"
                " Statement CSV by its Dividends-section row index. Please file a new"
                " issue at https://github.com/jay-tau/ibkr-fa/issues/new/choose"
                " using the 'Atomic-unit issue' template; the title MUST be a"
                " conventional-commit subject (suggested: 'feat(os): support"
                " a new dividend distribution type'), and in the body"
                f" {do_not_paste}."
            )
        if self.violation == "missing_kind_prefix":
            return (
                "Malformed dividend description in the Dividends"
                f" section{row_clause}: the Description does not start with"
                " the expected SYMBOL(ISIN) <whitespace> structural"
                " prefix (matched by the parser's"
                " ``_DESCRIPTION_PATTERN`` regex in"
                " ``ibkr_fa.parser.activity_statement._description``,"
                " which tolerates optional whitespace between SYMBOL"
                " and the opening parenthesis to accept legacy /"
                " hand-normalised exports). The classifier"
                " cannot extract a distribution kind safely. No"
                " Schedule OS output was written for this run. Locate"
                " the row in your IBKR Activity Statement CSV by its"
                " Dividends-section row index. This"
                " typically indicates a parser bypass or an IBKR"
                " Activity Statement format change. Please file a new"
                " issue at"
                " https://github.com/jay-tau/ibkr-fa/issues/new/choose"
                " using the 'Atomic-unit issue' template; the title MUST"
                " be a conventional-commit subject (suggested:"
                " 'fix(parser): handle dividend description without"
                " canonical SYMBOL(ISIN) structural prefix'), and in the"
                f" body {do_not_paste}. Do NOT add the type to the"
                " allow / reject list — this is not a novel distribution"
                " kind, it is a parser-input shape mismatch."
            )
        if self.violation == "missing_usd_marker":
            return (
                "Malformed dividend type-slice in the Dividends"
                f" section{row_clause}: the Description's text after the"
                " SYMBOL(ISIN) prefix is missing the"
                " canonical ' USD ' currency marker, so the"
                " distribution kind cannot be classified safely (even"
                " though the whole stripped slice may coincide with an"
                " allow-listed kind like 'Cash Dividend'). The canonical"
                " IBKR format is '{kind} USD {amount} per Share"
                " [({qualifier})]'; a slice without ' USD ' indicates"
                " a parser bypass, an upstream-schema drift, or a"
                " multi-currency dividend (multi-currency support is"
                " tracked at #52). No Schedule OS output was written"
                " for this run. Locate the row in your IBKR Activity"
                " Statement CSV by its Dividends-section row index. Please file a"
                " new issue at"
                " https://github.com/jay-tau/ibkr-fa/issues/new/choose"
                " using the 'Atomic-unit issue' template; the title MUST"
                " be a conventional-commit subject (suggested:"
                " 'fix(parser): handle dividend type-slice without"
                ' canonical " USD " marker\'), and in the body'
                f" {do_not_paste}. Do NOT add the type to the allow /"
                " reject list — this is not a novel distribution kind, it"
                " is a parser-input shape mismatch."
            )
        if self.violation == "malformed_type_slice":
            return (
                "Malformed dividend description in the Dividends"
                f" section{row_clause}: the row's Description column, after"
                " the SYMBOL(ISIN) prefix, isn't '<kind> USD <amount> per"
                " Share', optionally followed by ' (Ordinary Dividend)', so"
                " its distribution kind can't be classified safely. No"
                " Schedule OS output was written for this run. Locate the"
                " row in your IBKR Activity Statement CSV by its"
                " Dividends-section row index. Please file a new issue at"
                " https://github.com/jay-tau/ibkr-fa/issues/new/choose"
                " using the 'Atomic-unit issue' template; the title MUST"
                " be a conventional-commit subject (suggested:"
                " 'fix(parser): handle a dividend description outside the"
                " supported grammar'), and in the body"
                f" {do_not_paste}. Do NOT add the type to the allow /"
                " reject list — this is not a novel distribution kind, it"
                " is a parser-input shape mismatch."
            )
        return (
            "UnrecognisedDistributionTypeError: unknown violation kind"
            f"{row_clause}."
            " This indicates a bypass of the ``ty`` type-checker at the"
            " construction site; report this as a bug."
        )


class PaymentInLieuOfDividendError(IbkrFaError):
    """An in-FY Payment In Lieu Of Dividend row reached Schedule OS (#1281).

    A payment in lieu is other income, not a dividend, and ibkr-fa can't
    yet file an IBKR one as other income with its withholding: the
    statement doesn't carry the source-country evidence the substitute
    path needs. If its withholding shares an ordinary dividend's (ISIN,
    payment date) key, leaving the row out would credit that withholding
    against the dividend. Only the IBKR Activity Statement
    parser sets this kind.

    The message is fixed text naming only the row's Dividends-section
    index; the error holds nothing else from the row.

    Attributes:
        source_row_index: The row's 0-based index in the Dividends
            section, or ``None`` for a :class:`Dividend` built outside
            the parser.
    """

    def __init__(self, source_row_index: int | None) -> None:
        self.source_row_index = source_row_index
        row_clause = (
            f" (Dividends-section row index {source_row_index}; 0-based,"
            f" so data row {source_row_index + 1})"
            if source_row_index is not None
            else ""
        )
        super().__init__(
            f"Payment In Lieu Of Dividend in the Dividends section{row_clause}:"
            " ibkr-fa can't yet file an IBKR payment in lieu of a dividend as"
            " other income with its withholding. Leaving it out would understate"
            " your income and could credit any withholding against ordinary"
            " dividends, so this run stops. Locate the row in your IBKR Activity"
            " Statement CSV by its Dividends-section row index."
        )


NON_ORDINARY_DISTRIBUTION_KINDS: Final[frozenset[DistributionKind]] = frozenset(
    {
        DistributionKind.RETURN_OF_CAPITAL,
        DistributionKind.LONG_TERM_CAPITAL_GAIN,
        DistributionKind.CASH_IN_LIEU,
    }
)
"""The kinds :class:`NonOrdinaryDistributionError` refuses (#1310)."""


class NonOrdinaryDistributionError(IbkrFaError):
    """An IBKR return of capital, long-term capital gain or cash in lieu row
    reached a command that can't file it (#1310).

    ibkr-fa can't yet file any of these kinds: a return of capital or cash
    in lieu can change a lot's cost or be a disposal the Trades section
    doesn't show, and a long-term capital gain distribution isn't a
    dividend. Skipping the row, or counting it as a dividend, could misstate
    income, capital gains, foreign tax credit or Schedule FA. Only the IBKR
    Activity Statement parser sets these kinds.

    The message is fixed text naming only the kind's fixed label and the
    row's Dividends-section index; the error holds nothing else from the row.

    Attributes:
        kind: The row's :class:`~ibkr_fa.models.DistributionKind`, one of
            :data:`NON_ORDINARY_DISTRIBUTION_KINDS`.
        source_row_index: The row's 0-based index in the Dividends
            section, or ``None`` for a :class:`Dividend` built outside
            the parser.
    """

    def __init__(self, kind: DistributionKind, source_row_index: int | None) -> None:
        self.kind = kind
        self.source_row_index = source_row_index
        row_clause = (
            f" (Dividends-section row index {source_row_index}; 0-based,"
            f" so data row {source_row_index + 1})"
            if source_row_index is not None
            else ""
        )
        super().__init__(
            f"{kind.value} in the Dividends section{row_clause}: ibkr-fa can't yet"
            " file an IBKR return of capital, long-term capital gain distribution"
            " or cash in lieu. Skipping it, or counting it as a dividend, could"
            " misstate your income, capital gains, foreign tax credit or Schedule"
            " FA, so this run stops. Locate the row in your IBKR Activity"
            " Statement CSV by its Dividends-section row index."
        )


def require_no_non_ordinary_distributions(dividends: Iterable[Dividend]) -> None:
    """Refuse the first row of ``dividends`` whose kind is in
    :data:`NON_ORDINARY_DISTRIBUTION_KINDS` (#1310).

    Schedule CG passes every row of the statement, on any date; Schedule FA
    passes the calendar year's rows. Rows of other kinds pass.

    Raises:
        NonOrdinaryDistributionError: For the first such row.
    """
    for div in dividends:
        if div.kind in NON_ORDINARY_DISTRIBUTION_KINDS:
            raise NonOrdinaryDistributionError(div.kind, div.source_row_index)


def compute_schedule_os_dividend_lines(  # noqa: PLR0912
    dividends: Iterable[Dividend],
    *,
    financial_year_start: date,
    financial_year_end: date,
    fx_policy: AuditedFxPolicy,
    adr_routing_mode: ScheduleOsIndianAdrRoutingMode = (ScheduleOsIndianAdrRoutingMode.DEPOSITARY),
) -> ScheduleOsComputeResult:
    """Compute Schedule OS 1a(i) ordinary dividends for an FY.

    Args:
        dividends: All parsed :class:`Dividend` records. Outside-FY
            rows are silently filtered (a multi-period IBKR Activity
            Statement may emit dividends from before
            ``financial_year_start`` or after ``financial_year_end``);
            in-FY rows are partitioned by source country — see
            :class:`ScheduleOsComputeResult`. A generator argument is
            materialised to a list before any precondition check so
            the iterator is never exhausted by the precondition pass
            (rev-12 quality P1-NEW-20).
        financial_year_start: First day of the Indian FY being
            reported (e.g., ``date(2025, 4, 1)`` for FY 2025-26).
        financial_year_end: Last day of the Indian FY being reported
            (e.g., ``date(2026, 3, 31)`` for FY 2025-26).
        adr_routing_mode: Indian-company ADR / GDR routing mode
            (issue #183). Default
            :attr:`~ibkr_fa.config.ScheduleOsIndianAdrRoutingMode.DEPOSITARY`
            (asset-as-held; ADRs with US- / Jersey- / Cayman-prefix
            ISINs stay foreign-source for FSI/FTC). Set to
            :attr:`~ibkr_fa.config.ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER`
            to opt in to the underlying-issuer view (IT Act §9(1)(iv)
            "paid by an Indian company outside India") — dividends
            whose ISIN appears in the hardcoded
            :data:`~ibkr_fa.reference.indian_adr_mapping.INDIAN_ADR_MAPPING`
            table (INFY, IBN, HDB, WIT, RDY, WNS, YTRA) are
            attributed to :attr:`ScheduleOsComputeResult.domestic_lines`
            (Indian-source) with the audit trail preserved via
            :attr:`ScheduleOsDomesticDividendLine.adr_source_isin` and
            :attr:`ScheduleOsDomesticDividendLine.adr_underlying_issuer_name`.
            Non-mapped ADRs / GDRs continue to route per the
            asset-as-held convention regardless of mode (the mapping
            table is exhaustive opt-in; no heuristic matching).
        fx_policy: The :class:`~ibkr_fa.fx.policy.AuditedFxPolicy`
            to use for per-payment FX conversion. v1 production uses
            :class:`~ibkr_fa.fx.policy_rule115.Rule115Policy`
            (Rule 115(1), Explanation (2)(e), preceding-month-end
            with carry-back). ``rate_for_audited`` is called once
            per allow-list-classified payment, and the line takes
            its rate, ``fx_specified_date`` and
            :attr:`ScheduleOsDividendLine.fx_rate_publication_date`
            (the **actual TTBR source date**: == specified date when
            no carry-back; an earlier date on weekend / holiday /
            absent-from-archive carry-back) from that one resolution,
            so the review CSV can show an auditor a date-rate pair
            that matches an actual SBI archive row (#160).

    Returns:
        A :class:`ScheduleOsComputeResult` with:

        * ``lines``: allow-list-classified foreign-source in-FY rows.
        * ``domestic_lines``: allow-list-classified, **direct Indian-
          share** (IBKR-emitted IN-prefix ISIN), in-FY rows for
          Schedule OS row 1a(i), sorted by ``(payment_date, symbol)``.
          Each entry has the same Rule 115 treatment as :attr:`lines`.
          An in-FY return of capital, long-term capital gain or cash
          in lieu refuses (an Indian-issuer one too, rather than
          landing in :attr:`ScheduleOsComputeResult.domestic_lines`).
          Malformed IN descriptions raise
          :class:`UnrecognisedDistributionTypeError` rather than
          routing to :attr:`domestic_lines` (T3-r18 P2 fix).

    Raises:
        InvariantError: If any input :class:`Dividend` has
            ``currency != "USD"``. v1 only supports USD; multi-currency
            tracking is at #52. Defense-in-depth — the parser at
            ``dividends.py:226-237`` already gates this, so reaching
            this check indicates a parser bypass or schema drift
            (rev-11 quality P1 + rev-12 quality P1-NEW-19).
        UnrecognisedDistributionTypeError: With one of four discriminator
            values on :attr:`UnrecognisedDistributionTypeError.violation`:

            * ``"missing_kind_prefix"`` — the :class:`Dividend` was
              built without a parser-stripped structural prefix
              (:attr:`Dividend.type_slice` empty AND
              :attr:`Dividend.kind` defaulted to
              :attr:`DistributionKind.UNRECOGNISED`). In production
              the parser raises :class:`ParseError` upfront, so this
              fires only for test fixtures / direct constructors,
              whatever their description. Remediation is to set
              ``kind`` where the :class:`Dividend` is built.
            * ``"missing_usd_marker"`` — the parser classified the
              kind as :attr:`DistributionKind.MALFORMED` because the
              type-slice (after prefix-strip) is missing the
              canonical ``" USD "`` currency marker (parser bypass /
              schema drift / multi-currency dividend tracked at #52).
              Remediation is a parser fix, NOT adding a new
              distribution kind.
            * ``"malformed_type_slice"`` — the parser classified the
              kind as :attr:`DistributionKind.MALFORMED` although the
              type-slice has ``" USD "`` and a known kind label,
              because the whole slice isn't
              ``<kind> USD <amount> per Share[ (Ordinary Dividend)]``
              (or ``Cash in Lieu USD <amount>``). Remediation is a
              parser fix, NOT adding a new distribution kind.
            * ``"unsupported_kind"`` — the parser classified the kind
              as :attr:`DistributionKind.UNRECOGNISED` because the
              well-formed type-slice's kind keyword matches no known
              kind. Remediation is to add the kind and either
              allow-list it in
              :data:`~ibkr_fa.models.ALLOWED_DISTRIBUTION_KINDS` or
              refuse it, with a follow-up issue link.

            Fail-fast across all four modes — silent classification
            misses would corrupt Schedule OS treaty-rate reporting.
        PaymentInLieuOfDividendError: If an in-FY row's kind is
            :attr:`DistributionKind.PAYMENT_IN_LIEU_OF_DIVIDEND` (#1281).
        NonOrdinaryDistributionError: If an in-FY row's kind is in
            :data:`NON_ORDINARY_DISTRIBUTION_KINDS` (#1310).

        Rows are checked in order, so the first in-FY row that refuses
        decides the error.
    """
    dividends = list(dividends)

    non_usd = [d for d in dividends if d.currency != "USD"]
    require(
        not non_usd,
        what=(
            "compute_schedule_os_dividend_lines: v1 supports USD only; "
            "multi-currency support is tracked at #52. The parser at "
            "dividends.py:226-237 already gates this, so reaching this "
            "check indicates a parser bypass or schema drift."
        ),
        non_usd_count=len(non_usd),
        non_usd_currencies=tuple(sorted({d.currency for d in non_usd})),
    )

    lines: list[ScheduleOsDividendLine] = []
    domestic_lines: list[ScheduleOsDomesticDividendLine] = []

    for div in dividends:
        if not (financial_year_start <= div.payment_date <= financial_year_end):
            continue

        # Distribution-type classification is country-agnostic — a
        # non-ordinary distribution (Return Of Capital, Long-Term
        # Capital Gain, ...) is non-ordinary regardless of the
        # issuer's country, and a malformed description is malformed
        # regardless of the issuer's country. The ISIN-derived country
        # only determines source attribution: non-IN rows become
        # :attr:`lines`, IN rows become :attr:`domestic_lines`. Both
        # feed the same Schedule OS 1a(i) portal total.
        # Classifying first means an in-FY ``INE... Return Of Capital
        # ...`` row correctly raises (a malformed one
        # ``UnrecognisedDistributionTypeError``, a well-formed one
        # ``NonOrdinaryDistributionError``) instead of being silently
        # rendered as "Indian-source ordinary" (T3 P2 finding).
        #
        # The three pre-#153 failure modes are preserved 1:1
        # (``missing_kind_prefix``, ``missing_usd_marker``,
        # ``unsupported_kind``), plus ``malformed_type_slice``, with
        # the dispatch keyed on the dividend's kind/type_slice:
        #
        # * ``type_slice == ""`` with the default ``UNRECOGNISED``
        #   kind ⇒ ``missing_kind_prefix`` (the :class:`Dividend` was
        #   built with both fields left at their defaults; no
        #   production path does this, but direct constructors fail
        #   closed here rather than being classified on the fly).
        # * ``kind == MALFORMED`` ⇒ ``missing_usd_marker`` when the
        #   type-slice lacks the canonical ``" USD "`` currency
        #   marker, else ``malformed_type_slice`` (a known kind label
        #   whose whole type-slice doesn't match the supported
        #   grammar).
        # * ``kind == UNRECOGNISED`` (with non-empty type_slice) ⇒
        #   ``unsupported_kind`` (the kind keyword isn't in the
        #   allow / reject enum).
        if div.type_slice == "" and div.kind == DistributionKind.UNRECOGNISED:
            raise UnrecognisedDistributionTypeError.from_dividend(
                div, violation="missing_kind_prefix"
            )
        if div.kind == DistributionKind.MALFORMED:
            raise UnrecognisedDistributionTypeError.from_dividend(
                div,
                violation=(
                    "malformed_type_slice" if " USD " in div.type_slice else "missing_usd_marker"
                ),
            )
        if div.kind == DistributionKind.UNRECOGNISED:
            raise UnrecognisedDistributionTypeError.from_dividend(div, violation="unsupported_kind")
        if div.kind == DistributionKind.PAYMENT_IN_LIEU_OF_DIVIDEND:
            raise PaymentInLieuOfDividendError(div.source_row_index)
        if div.kind in NON_ORDINARY_DISTRIBUTION_KINDS:
            raise NonOrdinaryDistributionError(div.kind, div.source_row_index)

        if div.kind in ALLOWED_DISTRIBUTION_KINDS:
            # FX resolution is source-country agnostic: all supported
            # ordinary dividends use Rule 115(1), Explanation (2)(e). Rule 115's
            # rate-source clause is anchored on the income type
            # ("dividend received in foreign currency"), not on the
            # issuer's country — so a direct Indian-share dividend
            # received in USD via a foreign broker (the rare 1a(i)
            # case) still uses the preceding-month-end SBI TTBR,
            # exactly like a US-issuer dividend.
            resolution = fx_policy.rate_for_audited(div.payment_date)
            fx_rate = resolution.rate
            fx_specified_date = resolution.specified_date
            fx_rate_publication_date = resolution.rate_date
            amount_gross_inr = exact_inr_conversion(
                div.amount_gross,
                fx_rate,
                label=DIVIDEND_LABEL,
                source_row_index=div.source_row_index,
            )
            # Source-attribution decision (issue #158 + #183):
            # 1. Direct Indian-share case (IN-prefix ISIN) — always
            #    treat as Indian-source regardless of adr_routing_mode (the
            #    mode only affects ADR / GDR rows whose ISIN has a
            #    NON-IN depositary-country prefix).
            # 2. ADR-re-routed case (issue #183) — fires only when
            #    adr_routing_mode == UNDERLYING_ISSUER AND the ISIN
            #    matches the hardcoded mapping table; preserves the
            #    audit trail via adr_source_isin +
            #    adr_underlying_issuer_name fields.
            # 3. Default foreign-source case — everything else.
            adr_entry = (
                lookup_indian_adr(div.isin)
                if adr_routing_mode == ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER
                else None
            )
            if div.isin[:2] == "IN":
                domestic_lines.append(
                    ScheduleOsDomesticDividendLine(
                        symbol=div.symbol,
                        isin=div.isin,
                        issuer_country_iso2=div.isin[:2],
                        payment_date=div.payment_date,
                        fx_specified_date=fx_specified_date,
                        fx_rate_publication_date=fx_rate_publication_date,
                        fx_rate_inr_per_usd=fx_rate,
                        amount_gross_usd=div.amount_gross,
                        amount_gross_inr=amount_gross_inr,
                        description=div.description,
                        kind=div.kind,
                        source_account_index=div.source_account_index,
                    )
                )
            elif adr_entry is not None:
                # Issue #183 underlying-issuer routing: override the
                # ISIN-derived depositary country with "IN", preserve
                # the ADR ISIN audit trail.
                domestic_lines.append(
                    ScheduleOsDomesticDividendLine(
                        symbol=div.symbol,
                        isin=div.isin,
                        issuer_country_iso2="IN",
                        payment_date=div.payment_date,
                        fx_specified_date=fx_specified_date,
                        fx_rate_publication_date=fx_rate_publication_date,
                        fx_rate_inr_per_usd=fx_rate,
                        amount_gross_usd=div.amount_gross,
                        amount_gross_inr=amount_gross_inr,
                        description=div.description,
                        kind=div.kind,
                        adr_source_isin=div.isin,
                        adr_underlying_issuer_name=adr_entry.underlying_issuer_name,
                        source_account_index=div.source_account_index,
                    )
                )
            else:
                lines.append(
                    ScheduleOsDividendLine(
                        symbol=div.symbol,
                        isin=div.isin,
                        issuer_country_iso2=div.isin[:2],
                        payment_date=div.payment_date,
                        fx_specified_date=fx_specified_date,
                        fx_rate_publication_date=fx_rate_publication_date,
                        fx_rate_inr_per_usd=fx_rate,
                        amount_gross_usd=div.amount_gross,
                        amount_gross_inr=amount_gross_inr,
                        description=div.description,
                        kind=div.kind,
                        source_account_index=div.source_account_index,
                    )
                )
            continue

        # All enum members are either in ALLOWED_DISTRIBUTION_KINDS,
        # the refused PAYMENT_IN_LIEU_OF_DIVIDEND or
        # NON_ORDINARY_DISTRIBUTION_KINDS, or one of the two
        # parse-time failure markers (UNRECOGNISED / MALFORMED)
        # handled above. Reaching this point would indicate a new
        # enum member added without updating the dispatch (which
        # would also be caught by ty's exhaustiveness check against
        # ALLOWED + NON_ORDINARY + {PAYMENT_IN_LIEU_OF_DIVIDEND,
        # UNRECOGNISED, MALFORMED} if a future
        # refactor uses `match`). Defense-in-depth: re-raise as
        # UnrecognisedDistributionTypeError with unsupported_kind so
        # the failure is surfaced loudly rather than silently dropped.
        raise UnrecognisedDistributionTypeError.from_dividend(div, violation="unsupported_kind")

    lines.sort(key=lambda line: (line.payment_date, line.symbol))
    domestic_lines.sort(key=lambda line: (line.payment_date, line.symbol))

    for line in lines:
        require(
            line.issuer_country_iso2 != "IN",
            what="compute_schedule_os_dividend_lines post: no IN-issuer rows in lines",
            symbol=str(line.symbol),
            isin=str(line.isin),
        )
    for domestic_line in domestic_lines:
        require(
            domestic_line.issuer_country_iso2 == "IN",
            what=("compute_schedule_os_dividend_lines post: non-IN issuer row in domestic_lines"),
            symbol=str(domestic_line.symbol),
            isin=str(domestic_line.isin),
        )
    # Strengthen the post-condition from "bucket sum <= input count"
    # to "bucket sum == in-FY input count" so a future accidental
    # ``continue`` in the in-FY path cannot silently drop a dividend
    # while still satisfying the invariant (T3 codex-emulation P3
    # finding for issue #158). The fail-fast classifier means
    # malformed / unsupported in-FY rows ALREADY raise rather than
    # contribute to any bucket — but a malformed in-FY row that
    # raises never reaches this post-condition (the function aborts),
    # so this equality holds across all successful-return paths.
    in_fy_count = sum(
        1
        for dividend in dividends
        if financial_year_start <= dividend.payment_date <= financial_year_end
    )
    require(
        len(lines) + len(domestic_lines) == in_fy_count,
        what=(
            "compute_schedule_os_dividend_lines post: every in-FY dividend "
            "must land in exactly one of (lines, domestic_lines); "
            "silently dropping any row is a bug"
        ),
        lines_count=len(lines),
        domestic_lines_count=len(domestic_lines),
        in_fy_count=in_fy_count,
        total_input_count=len(dividends),
    )

    return ScheduleOsComputeResult(
        lines=lines,
        domestic_lines=domestic_lines,
    )
