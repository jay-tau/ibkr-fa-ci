"""Build the Schedule FA Table A2 row for one custodial account (issue #18).

Pure compute: given the source-neutral account identity, the Net Asset Value
summary, the Dec-31 TTBR, and the calendar-year gross credits, produce a base
:class:`~ibkr_fa.models.fa_a2_row.FaA2Row`, then expand it to the official
one-row-per-credit-nature shape. Refuses (leaves a cell ``None`` =
manual-fill) rather than guessing any uncertain filed figure, mirroring
Table A3's discipline:

* Peak balance is manual-fill unless the caller passes a peak with its
  provenance: audited lower/upper bounds with a selected bound, or a value
  the user attested in config (see :data:`FaA2PeakInput`).
* Closing balance is NAV x Dec-31 TTBR, filled only for a USD base with a
  confident NAV. A missing Dec-31 TTBR raises
  :class:`~ibkr_fa.exceptions.MissingFxRateError` instead of leaving it blank.
  A blank closing balance carries its
  :class:`~ibkr_fa.models.fa_a2_row.FaA2ClosingBlankReason` (#1013).
* Known credit natures become separate A2 rows. Any unresolved nature
  leaves one blank placeholder row so it cannot disappear silently.

The filed account number is ALWAYS the identity's real ``account_ref``
(:class:`~ibkr_fa.models.account_identity.AccountIdentity` refuses a masked
literal), never the statement's masked id.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from ibkr_fa.compute.dividends import aggregate_dividends_by_symbol_inr
from ibkr_fa.compute.exact_inr import checked_addition, checked_product, checked_sum
from ibkr_fa.compute.fa_a3_gross_proceeds import gross_proceeds_inr_for_lot
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.invariants import require
from ibkr_fa.models import ClosedLot, Dividend, InterestCredit, InterestNonCredit
from ibkr_fa.models.account_identity import (
    SIGNING_AUTHORITY_UNSUPPORTED,
    AccountIdentity,
    TaxpayerStatus,
)
from ibkr_fa.models.fa_a2_row import (
    FA_A2_CREDIT_NATURES,
    FA_A2_STATUS_BY_TAXPAYER_STATUS,
    FaA2ClosingBlankReason,
    FaA2NatureOfAmount,
    FaA2PeakBound,
    FaA2Row,
)
from ibkr_fa.models.net_asset_value import NetAssetValueSummary
from ibkr_fa.reference.country_codes import lookup_country


@dataclass(frozen=True, slots=True)
class FaA2StatusError(IbkrFaError):
    """The account's ownership status has no Schedule FA Table A2 token.

    Args:
        status: The refused status (``SigningAuthority``).
    """

    status: TaxpayerStatus

    def __str__(self) -> str:
        return f"status {self.status.value!r}: {SIGNING_AUTHORITY_UNSUPPORTED}"


@dataclass(frozen=True, slots=True)
class FaA2GrossEvidence:
    """Known and unresolved account-credit natures for one A2 account."""

    known_components: Mapping[FaA2NatureOfAmount, Decimal]
    unresolved_natures: tuple[FaA2NatureOfAmount, ...] = ()

    def __post_init__(self) -> None:
        allowed = set(FA_A2_CREDIT_NATURES)
        require(
            set(self.known_components) <= allowed,
            what="FaA2GrossEvidence known natures must be A2 credit natures",
        )
        require(
            all(value.is_finite() and value >= 0 for value in self.known_components.values()),
            what="FaA2GrossEvidence known amounts must be finite and non-negative",
        )
        require(
            len(set(self.unresolved_natures)) == len(self.unresolved_natures)
            and set(self.unresolved_natures) <= allowed,
            what="FaA2GrossEvidence unresolved natures must be unique A2 credit natures",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FaA2PeakBounds:
    """An audited A2 peak range and the bound filed from it.

    Posting-order bounds (Fidelity CMA) are each converted at their own date's
    TTBR. Under the default ``peak_currency = "native"`` each bound is its
    highest native value, so the rupee order can differ from the native order
    and ``lower_inr`` can exceed ``upper_inr``. Under ``"inr"`` each bound is
    its highest daily INR value. A single computed peak (Robinhood) passes one
    value and date as both bounds.

    Attributes:
        lower_inr: The lower bound's peak, in INR.
        lower_date: The date of ``lower_inr``.
        upper_inr: The upper bound's peak, in INR.
        upper_date: The date of ``upper_inr``.
        selected: The bound to file, or ``None`` to leave the peak
            manual-fill with both bounds kept for review.
        base_ccy: The peak in the account's base currency, before conversion.
        ttbr: The SBI TTBR that converted ``base_ccy``.
        statement_floor_base_ccy: The highest account value a statement
            states, in base currency; the peak can't be lower.
        statement_floor_date: The date of ``statement_floor_base_ccy``.
    """

    lower_inr: Decimal
    lower_date: date
    upper_inr: Decimal
    upper_date: date
    selected: FaA2PeakBound | None
    base_ccy: Decimal | None = None
    ttbr: Decimal | None = None
    statement_floor_base_ccy: Decimal | None = None
    statement_floor_date: date | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FaA2AttestedPeak:
    """An A2 peak the user supplied, with where it came from and why it is used.

    Attributes:
        peak_inr: The filed peak, in INR.
        source: Where the value came from (e.g. the config table and year).
        reason: Why a supplied value stands in for a computed one.
    """

    peak_inr: Decimal
    source: str
    reason: str


# How a caller supplies the A2 peak; ``None`` leaves it manual-fill.
FaA2PeakInput = FaA2PeakBounds | FaA2AttestedPeak


def gross_interest_inr(
    interest_credits: Sequence[InterestCredit],
    *,
    calendar_year: int,
    fx_policy: FxPolicy,
) -> Decimal:
    """Total calendar-year interest credits in INR at the December 31 TTBR.

    Credits dated outside ``calendar_year`` are ignored. Returns 0 without an
    FX lookup when no credit falls in the year. The credits add exactly
    (#1168).

    Raises:
        InvariantError: When a credit is not in ``fx_policy.currency``, or
            the total can't be added exactly; that refusal names the credit's
            1-based position in ``interest_credits`` and its
            ``source_row_index`` when it has one. When the total can't be
            converted exactly, ``Schedule FA A2 gross interest, INR`` (#1312).
        MissingFxRateError: Propagated when the December 31 rate is missing.
    """
    require(
        all(credit.currency == fx_policy.currency for credit in interest_credits),
        what="gross_interest_inr: every credit must be in the FX policy currency",
        currency=fx_policy.currency,
    )
    return _numbered_interest_inr(
        enumerate(interest_credits, start=1), calendar_year=calendar_year, fx_policy=fx_policy
    )


def _numbered_interest_inr(
    numbered_credits: Iterable[tuple[int, InterestCredit]],
    *,
    calendar_year: int,
    fx_policy: FxPolicy,
) -> Decimal:
    """:func:`gross_interest_inr` over ``(position, credit)`` pairs, so a
    refusal names the caller's 1-based position for the credit."""
    total = Decimal(0)
    in_year = False
    for position, credit in numbered_credits:
        if credit.credit_date.year != calendar_year:
            continue
        in_year = True
        total = checked_addition(
            total,
            credit.amount_gross,
            at=f"Schedule FA A2 gross interest, input row {position}, column amount_gross",
            source_row_index=credit.source_row_index,
        )
    if not in_year:
        return Decimal(0)
    return checked_product(
        total,
        fx_policy.rate_for(date(calendar_year, 12, 31)),
        at="Schedule FA A2 gross interest, INR",
    )


def build_gross_evidence(
    dividends: Sequence[Dividend],
    interest_credits: Sequence[InterestCredit],
    closed_lots: Iterable[ClosedLot],
    *,
    interest_non_credits: Sequence[InterestNonCredit],
    calendar_year: int,
    fx_policy: FxPolicy,
    dividend_activity_present: bool,
) -> FaA2GrossEvidence:
    """Resolve each A2 credit nature independently without dropping known totals.

    Every nature converts at the December 31 TTBR. A nature is unresolved when
    a dividend section parsed to nothing, an interest credit is not in
    ``fx_policy.currency``, an interest row is unproven (#905), or a closed lot
    lacks proven gross proceeds. Debit interest in ``interest_non_credits`` is
    left out. Known partial totals stay in ``known_components`` even when their
    nature is also unresolved. The dividend, interest and proceeds totals add
    exactly (#1168).

    Raises:
        InvariantError: When a total can't be added exactly. Gross proceeds
            name the lot's 1-based position in ``closed_lots``; the dividend
            total names the symbol total's 1-based position in
            :func:`~ibkr_fa.compute.dividends.aggregate_dividends_by_symbol_inr`'s
            result, and interest the credit's 1-based position in
            ``interest_credits``, counting credits in other currencies. Also
            when a dividend or interest total can't be converted exactly
            (#1312), naming the symbol total's position for dividends.
        MissingFxRateError: When a nature needs the missing December 31 rate,
            including proven gross proceeds of 0 (#735).
    """
    components: dict[FaA2NatureOfAmount, Decimal] = {}
    unresolved: set[FaA2NatureOfAmount] = set()

    def add(nature: FaA2NatureOfAmount, amount: Decimal) -> None:
        if amount > 0:
            components[nature] = amount

    def proceeds_total() -> Decimal:
        total = Decimal(0)
        for position, lot in enumerate(closed_lots, start=1):
            if lot.close_date_time.year != calendar_year:
                continue
            proceeds = gross_proceeds_inr_for_lot(
                lot,
                calendar_year=calendar_year,
                fx_policy=fx_policy,
            )
            if proceeds is None:
                unresolved.add(FaA2NatureOfAmount.PROCEEDS)
            else:
                total = checked_addition(
                    total,
                    proceeds,
                    at=(
                        f"Schedule FA A2 gross proceeds, input lot {position}, "
                        "field gross_proceeds_inr"
                    ),
                )
        return total

    if dividend_activity_present and not dividends:
        unresolved.add(FaA2NatureOfAmount.DIVIDEND)
    add(
        FaA2NatureOfAmount.DIVIDEND,
        checked_sum(
            enumerate(
                aggregate_dividends_by_symbol_inr(
                    dividends,
                    calendar_year=calendar_year,
                    fx_policy=fx_policy,
                ).values(),
                start=1,
            ),
            at=lambda position: (
                f"Schedule FA A2 gross dividends, contribution {position}, field dividend_total_inr"
            ),
        ),
    )
    in_currency = [
        (position, credit)
        for position, credit in enumerate(interest_credits, start=1)
        if credit.currency == fx_policy.currency
    ]
    if len(in_currency) != len(interest_credits) or any(
        row.group.unproven for row in interest_non_credits
    ):
        unresolved.add(FaA2NatureOfAmount.INTEREST)
    add(
        FaA2NatureOfAmount.INTEREST,
        _numbered_interest_inr(
            in_currency,
            calendar_year=calendar_year,
            fx_policy=fx_policy,
        ),
    )
    add(FaA2NatureOfAmount.PROCEEDS, proceeds_total())
    return FaA2GrossEvidence(
        known_components=components,
        unresolved_natures=tuple(sorted(unresolved, key=lambda nature: nature.value)),
    )


def fa_a2_closing_ttbr(
    nav: NetAssetValueSummary,
    *,
    base_currency: str,
    gross_evidence: FaA2GrossEvidence,
    calendar_year: int,
    fx_policy: FxPolicy,
) -> Decimal | None:
    """December 31 TTBR for an A2 row, or ``None`` when no A2 value needs it.

    A parsed USD NAV converts at the December 31 rate. Without one (a non-USD
    base, or a NAV that wasn't parsed), the closing balance stays manual, but
    a non-zero known gross credit still needs the rate
    (:func:`build_fa_a2_row` requires it, and refuses any rate for a non-USD
    base, whose IBKR gross evidence has no known credits).

    Raises:
        MissingFxRateError: When the NAV or a known gross credit needs the
            missing December 31 rate.
    """
    if (base_currency == "USD" and nav.closing_total is not None) or any(
        amount > 0 for amount in gross_evidence.known_components.values()
    ):
        return fx_policy.rate_for(date(calendar_year, 12, 31))
    return None


@dataclass(frozen=True, slots=True)
class FaA2ManualFillGaps:
    """The A2 columns some row leaves blank for the user to fill by hand:
    the ZIP code, peak, closing balance and gross amount."""

    zip_code: bool
    peak: bool
    closing: bool
    gross: bool


def fa_a2_manual_fill_gaps(rows: Sequence[FaA2Row]) -> FaA2ManualFillGaps:
    """Find the A2 columns any of ``rows`` leaves blank: a ZIP that is empty
    or whitespace-only (the CBDT JSON fragment's rule), or an amount that is
    ``None``."""
    return FaA2ManualFillGaps(
        zip_code=any(row.institution_zip.strip() == "" for row in rows),
        peak=any(row.peak_balance_inr is None for row in rows),
        closing=any(row.closing_balance_inr is None for row in rows),
        gross=any(row.gross_amount_inr is None for row in rows),
    )


def expand_fa_a2_nature_rows(
    base: FaA2Row,
    evidence: FaA2GrossEvidence,
) -> tuple[FaA2Row, ...]:
    """Emit one A2 row per known nature plus one unresolved placeholder."""

    require(
        dict(base.audit_gross_components)
        == {nature.value: amount for nature, amount in evidence.known_components.items()},
        what="A2 row audit components must match its gross evidence",
    )
    unresolved = set(evidence.unresolved_natures)
    rows = [
        replace(base, nature_of_amount=nature, gross_amount_inr=amount)
        for nature in FA_A2_CREDIT_NATURES
        if (amount := evidence.known_components.get(nature, Decimal())) != 0
        and nature not in unresolved
    ]
    if unresolved:
        rows.append(replace(base, nature_of_amount=None, gross_amount_inr=None))
    return tuple(rows) if rows else (base,)


def _gross_amount_and_nature(
    gross_components_inr: Mapping[FaA2NatureOfAmount, Decimal] | None,
) -> tuple[FaA2NatureOfAmount | None, Decimal | None]:
    """Pick the A2 gross-amount cell + nature.

    ``None`` input → not determinable → manual-fill (both ``None``). An empty
    map → confidently nothing credited → ``("No Amount paid/credited", 0)``. A
    single non-zero nature → that nature + amount. Multiple non-zero natures →
    manual-fill (both ``None``), never auto-collapsed to "Other income".
    """
    if gross_components_inr is None:
        return None, None
    nonzero = {n: amt for n, amt in gross_components_inr.items() if amt != 0}
    if not nonzero:
        return FaA2NatureOfAmount.NO_AMOUNT, Decimal("0")
    if len(nonzero) == 1:
        (nature, amount) = next(iter(nonzero.items()))
        return nature, amount
    return None, None


def build_fa_a2_row(
    *,
    identity: AccountIdentity,
    nav: NetAssetValueSummary,
    closing_ttbr_dec31: Decimal | None,
    gross_components_inr: Mapping[FaA2NatureOfAmount, Decimal] | None,
    period_end_cash_base_ccy: Decimal | None = None,
    peak: FaA2PeakInput | None = None,
) -> FaA2Row:
    """Build the base Schedule FA Table A2 row for one account.

    Call :func:`expand_fa_a2_nature_rows` when more than one credit
    nature or an unresolved nature may apply.

    Args:
        identity: The account and its custodial institution, as filed.
        nav: Net Asset Value summary (base-currency closing total).
        closing_ttbr_dec31: SBI TTBR on Dec-31 to convert the NAV and
            Schedule FA income to INR, or ``None`` when no A2 value needs it
            (see :func:`fa_a2_closing_ttbr`).
        gross_components_inr: Per-nature CY credit totals (INR), or ``None``
            when the credits could not be confidently aggregated.
        period_end_cash_base_ccy: Period-end cash in base currency (audit,
            a manual-fill peak reference), or ``None`` when unknown.
        peak: The A2 peak with its provenance, or ``None`` to leave it
            manual-fill. It fills the row's peak audit columns, so
            :class:`~ibkr_fa.models.fa_a2_row.FaA2Row` checks it.

    The closing balance is the exact ``nav.closing_total`` x
    ``closing_ttbr_dec31`` for a USD base (#1250). Otherwise it is blank,
    with the NAV's refusal reason or, when the NAV parsed, the
    non-USD-base reason in
    :attr:`~ibkr_fa.models.fa_a2_row.FaA2Row.closing_blank_reason` (#1013).

    Raises:
        FaA2StatusError: When the status has no A2 equivalent (SigningAuthority).
        InvariantError: When ``peak`` breaks a row provenance rule (e.g. a
            blank attested source or reason) or is not a supported peak input;
            when a USD base's confident NAV comes without
            ``closing_ttbr_dec31``; when a non-USD base comes with one; or
            when the closing balance can't be computed exactly.
    """
    institution = identity.institution
    country = lookup_country(institution.country_iso2)
    status_token = FA_A2_STATUS_BY_TAXPAYER_STATUS.get(identity.status)
    if status_token is None:
        raise FaA2StatusError(status=identity.status)

    gross_credits_use_dec31_ttbr = any(
        nature in set(FA_A2_CREDIT_NATURES) and amount != 0
        for nature, amount in (gross_components_inr or {}).items()
    )
    require(
        not gross_credits_use_dec31_ttbr or closing_ttbr_dec31 is not None,
        what="Schedule FA A2 gross credits require the December 31 TTBR",
    )

    is_usd = identity.base_currency == "USD"
    require(
        not is_usd or nav.closing_total is None or closing_ttbr_dec31 is not None,
        what="Schedule FA A2: a USD base's confident NAV requires the December 31 TTBR",
    )
    require(
        is_usd or closing_ttbr_dec31 is None,
        what="Schedule FA A2: a non-USD base takes no December 31 TTBR (v1 converts USD only)",
        base_currency=identity.base_currency,
    )
    closing_blank_reason: FaA2ClosingBlankReason | None = None
    if nav.refusal_reason is not None:
        closing_blank_reason = FaA2ClosingBlankReason(nav.refusal_reason.value)
    elif not is_usd:
        closing_blank_reason = FaA2ClosingBlankReason.NON_USD_BASE
    if nav.closing_total is not None and closing_ttbr_dec31 is not None:
        closing_balance_inr: Decimal | None = checked_product(
            nav.closing_total, closing_ttbr_dec31, at="Schedule FA A2 closing balance, INR"
        )
        audit_base_ccy: Decimal | None = nav.closing_total
    else:
        closing_balance_inr = None
        audit_base_ccy = None
    audit_ttbr = (
        closing_ttbr_dec31 if audit_base_ccy is not None or gross_credits_use_dec31_ttbr else None
    )

    nature, gross = _gross_amount_and_nature(gross_components_inr)

    row = FaA2Row(
        country_code=country.cbdt_code,
        country_name=country.country_name_portal,
        institution_name=institution.name,
        institution_address=institution.address,
        institution_zip=institution.zip_code,
        account_number=identity.account_ref,
        status=status_token,
        account_opening_date=identity.opening_date,
        peak_balance_inr=None,
        closing_balance_inr=closing_balance_inr,
        nature_of_amount=nature,
        gross_amount_inr=gross,
        base_currency=identity.base_currency,
        audit_closing_balance_base_ccy=audit_base_ccy,
        audit_ttbr_dec31=audit_ttbr,
        audit_account_number_from_config=identity.statement_masked_account_ref,
        audit_nav_cross_checked=nav.cross_checked,
        audit_gross_components={n.value: amt for n, amt in (gross_components_inr or {}).items()},
        audit_period_end_cash_base_ccy=period_end_cash_base_ccy,
        closing_blank_reason=closing_blank_reason,
    )
    if isinstance(peak, FaA2AttestedPeak):
        return replace(
            row,
            peak_balance_inr=peak.peak_inr,
            audit_peak_override_source=peak.source,
            audit_peak_override_reason=peak.reason,
        )
    if isinstance(peak, FaA2PeakBounds):
        return replace(
            row,
            peak_balance_inr=(
                peak.lower_inr
                if peak.selected == "lower"
                else peak.upper_inr
                if peak.selected == "upper"
                else None
            ),
            audit_peak_balance_lower_inr=peak.lower_inr,
            audit_peak_balance_upper_inr=peak.upper_inr,
            audit_peak_balance_lower_date=peak.lower_date,
            audit_peak_balance_upper_date=peak.upper_date,
            audit_peak_balance_bound=peak.selected,
            audit_peak_balance_base_ccy=peak.base_ccy,
            audit_peak_ttbr=peak.ttbr,
            audit_statement_peak_floor_base_ccy=peak.statement_floor_base_ccy,
            audit_statement_peak_floor_date=peak.statement_floor_date,
        )
    require(
        peak is None,
        what="Schedule FA A2 peak must be audited bounds, an attested value, or None",
    )
    return row
