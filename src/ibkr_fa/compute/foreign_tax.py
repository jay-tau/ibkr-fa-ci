"""Net foreign withholding tax (WHT) per source country.

Schedule FSI, Form 67 and the income engine's orphan-WHT guard all read
:func:`net_foreign_tax_by_country`, so their per-country foreign tax
cannot drift apart. The steps, in order:

1. In ``UNDERLYING_ISSUER`` mode, drop Indian-ADR rows (#225). Schedule
   OS reroutes their dividends to the domestic bucket, so their WHT
   would otherwise orphan or inflate the depositary country. The skip
   runs before pairing so skipped rows stay out of the cross-FY report.
   Then refuse, with :class:`StatedTaxCountryMismatchError`, any remaining
   row, in or out of the FY, whose stated tax country
   (:attr:`~ibkr_fa.models.WithholdingTax.tax_country_iso2`) differs from
   ``isin[:2]`` (#1282): the statement says another country took the tax.
2. Pair reversals with originals over every remaining row, out-of-FY
   rows included, and find the current-FY reversals whose original is
   in a prior FY (#224, :mod:`~ibkr_fa.compute.wht_reversal_pairing`).
   A current-FY reversal with no original (including a partial refund)
   or with candidate originals in more than one FY refuses with
   :class:`~ibkr_fa.compute.wht_reversal_pairing.UnprovenWhtReversalError`
   (#893): the year of the tax it cancels is unproven.
3. Keep the in-FY rows that are not cross-FY-excluded. Exclusion is by
   index, so value-identical rows stay distinct.

   * Sub-case (a2): a current-FY reversal of a prior-FY original would
     otherwise net against unrelated same-country current-FY WHT and
     understate this FY's claim.
   * Sub-case (b): a reversal in a later FY of a current-FY original
     shows only in the later FY's run, which excludes it and reports
     it so the user can amend the earlier return.

4. Per row: require the policy's currency (#52), look up the country of
   a new ISO, and convert at ``fx_policy.rate_for(payment_date)``.
5. Sum the signed amounts per ``isin[:2]`` (the withholding
   jurisdiction; income keys on the issuer country instead) and per ISIN,
   and the paid rows per dividend payment, keyed on
   :class:`WithholdingPaymentKey` (#890).

Every sum, in native currency and in INR, is exact and never rounds
(#1167): each addition is a
:func:`~ibkr_fa.compute.exact_inr.checked_addition`, and a paid row's
negative amount is subtracted with
:func:`~ibkr_fa.compute.exact_inr.checked_subtraction`. A refusal names
the included row's 1-based position and the column, never a value.

Signs follow IBKR: tax paid is negative, a reversal positive. Callers
negate with ``copy_negate()``, which never rounds, and drop countries
whose tax is not positive.

Every row, refunds included, converts at its own date's Rule 128(5)(ii)
rate. The rule speaks of tax paid or deducted; converting a refund as
its own dated event is this codebase's v1 reading. A refund in a later
FX month than its original therefore leaves a non-zero INR net even
when the foreign-currency net is zero. Whether a refund should use its
original's rate instead is #593.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from ibkr_fa.compute.exact_inr import checked_addition, checked_product, checked_subtraction
from ibkr_fa.compute.schedule_os import ScheduleOsDividendLine
from ibkr_fa.compute.wht_reversal_pairing import (
    WhtCrossFyReversalReport,
    build_cross_fy_report,
    classify_cross_fy_exclusions,
    pair_wht_reversals,
    require_proven_reversal_originals,
)
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.invariants import require
from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.reference.country_codes import lookup_country
from ibkr_fa.reference.indian_adr_mapping import lookup_indian_adr

__all__ = [
    "CountryForeignTax",
    "ForeignTaxNetting",
    "PaidWithholding",
    "StatedTaxCountryMismatchError",
    "WithholdingPaymentKey",
    "net_foreign_tax_by_country",
    "payment_key",
    "require_no_orphan_wht",
]

_ZERO = Decimal("0")


class StatedTaxCountryMismatchError(IbkrFaError):
    """A withholding row states a tax country other than its ISIN's (#1282)."""


def _require_stated_country_matches_isin(rows: Iterable[WithholdingTax]) -> None:
    """Refuse rows whose stated tax country differs from ``isin[:2]`` (#1282).

    Netting files and credits tax under the ISIN's country. A row whose
    statement names another country would be credited as the wrong
    country's tax, so the run refuses. Rows that state no country are
    left alone.

    Raises:
        StatedTaxCountryMismatchError: Naming each such row's statement
            line. Its symbol, ISIN, countries, date and amount are input,
            so the message doesn't print them.
    """
    lines = [
        str(row.file_line)
        for row in rows
        if row.tax_country_iso2 is not None and row.tax_country_iso2 != row.isin[:2]
    ]
    if lines:
        error = StatedTaxCountryMismatchError(
            f"the Withholding Tax row(s) on statement line(s) {', '.join(lines)} "
            "state a tax country (the description's ' - XX Tax' suffix) other than "
            "the country of the row's ISIN. Foreign tax is filed and credited "
            "under the ISIN's country, so the run refuses rather than credit tax "
            "the statement says another country took."
        )
        try:
            raise error
        finally:
            # An active caller's exception can hold input even with `from None`.
            error.__context__ = None


class WithholdingPaymentKey(NamedTuple):
    """One dividend payment: (source account, ISIN, payment date) (#890).

    Single-account importers leave the account ``None``.
    """

    source_account_index: int | None
    isin: str
    payment_date: date


def payment_key(item: WithholdingTax | ScheduleOsDividendLine) -> WithholdingPaymentKey:
    """The payment a withholding row or dividend line belongs to."""
    return WithholdingPaymentKey(item.source_account_index, item.isin.upper(), item.payment_date)


@dataclass(frozen=True, slots=True)
class PaidWithholding:
    """One payment's paid (negative-amount) rows, summed as positive tax."""

    native: Decimal
    inr: Decimal
    row_count: int


@dataclass(frozen=True, slots=True)
class CountryForeignTax:
    """Signed WHT totals for one ``isin[:2]`` country.

    A negative total is net tax paid; a positive one is a net refund.
    """

    iso_alpha2: str
    cbdt_country_code: str
    country_name_portal: str
    signed_inr: Decimal
    signed_native: Decimal
    event_count: int


@dataclass(frozen=True, slots=True)
class ForeignTaxNetting:
    """Result of :func:`net_foreign_tax_by_country`.

    Attributes:
        countries: Totals keyed by ``isin[:2]``, in the order each
            country first appears in :attr:`included_rows`.
        signed_inr_by_isin: Signed INR net keyed by ``isin.upper()``.
        paid_by_payment: The paid rows of :attr:`included_rows`, summed
            per payment. Refunds and reversals are left out.
        included_rows: The rows summed, in input order.
        cross_fy_reversal_report: Pairing diagnostics for the CLI.
    """

    countries: Mapping[str, CountryForeignTax]
    signed_inr_by_isin: Mapping[str, Decimal]
    paid_by_payment: Mapping[WithholdingPaymentKey, PaidWithholding]
    included_rows: tuple[WithholdingTax, ...]
    cross_fy_reversal_report: WhtCrossFyReversalReport


def net_foreign_tax_by_country(
    withholding_tax: Iterable[WithholdingTax],
    *,
    fx_policy: FxPolicy,
    financial_year_start: date,
    financial_year_end: date,
    adr_routing_mode: ScheduleOsIndianAdrRoutingMode,
) -> ForeignTaxNetting:
    """Net WHT per country and per ISIN, as the module docstring describes.

    Raises:
        StatedTaxCountryMismatchError: A row left after the ADR skip
            states a tax country other than its ISIN's.
        :class:`~ibkr_fa.compute.wht_reversal_pairing.UnprovenWhtReversalError`:
            A current-FY reversal is an orphan or ambiguous across FYs.
        :class:`~ibkr_fa.invariants.InvariantError`: An included row's
            currency is not ``fx_policy.currency``, or its INR conversion
            (#1312) or a net can't be computed exactly; those name the
            row's 1-based position among the included rows.
        :class:`~ibkr_fa.reference.country_codes.CountryCodeNotFoundError`:
            An included row's ``isin[:2]`` is not in the country table.
    """
    adr_filtered_rows = [
        row
        for row in withholding_tax
        if not (
            adr_routing_mode is ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER
            and lookup_indian_adr(row.isin) is not None
        )
    ]
    _require_stated_country_matches_isin(adr_filtered_rows)
    pairing_result = pair_wht_reversals(adr_filtered_rows)
    require_proven_reversal_originals(
        adr_filtered_rows=adr_filtered_rows,
        pairing_result=pairing_result,
        financial_year_start=financial_year_start,
        financial_year_end=financial_year_end,
    )
    cross_fy_excluded_indices = classify_cross_fy_exclusions(
        adr_filtered_rows=adr_filtered_rows,
        pairing_result=pairing_result,
        financial_year_start=financial_year_start,
        financial_year_end=financial_year_end,
    )
    cross_fy_reversal_report = build_cross_fy_report(
        adr_filtered_rows=adr_filtered_rows,
        pairing_result=pairing_result,
        cross_fy_excluded_filtered_indices=cross_fy_excluded_indices,
        financial_year_start=financial_year_start,
        financial_year_end=financial_year_end,
    )
    included_rows = tuple(
        row
        for index, row in enumerate(adr_filtered_rows)
        if financial_year_start <= row.payment_date <= financial_year_end
        and index not in cross_fy_excluded_indices
    )

    countries: dict[str, CountryForeignTax] = {}
    signed_inr_by_isin: dict[str, Decimal] = {}
    paid_by_payment: dict[WithholdingPaymentKey, PaidWithholding] = {}
    for position, row in enumerate(included_rows, start=1):
        require(
            row.currency == fx_policy.currency,
            what=(
                "WithholdingTax row currency must match the FX policy's "
                "currency in v1 (multi-currency support deferred to #52)"
            ),
            row_currency=row.currency,
            fx_policy_currency=fx_policy.currency,
            isin=row.isin,
            payment_date=row.payment_date.isoformat(),
        )
        iso = row.isin[:2]
        country = countries.get(iso)
        if country is None:
            entry = lookup_country(iso)
            country = CountryForeignTax(
                iso_alpha2=iso,
                cbdt_country_code=entry.cbdt_code,
                country_name_portal=entry.country_name_portal,
                signed_inr=_ZERO,
                signed_native=_ZERO,
                event_count=0,
            )
        amount_inr = checked_product(
            row.amount,
            fx_policy.rate_for(row.payment_date),
            at=f"Foreign tax INR, included row {position}, column amount",
        )
        countries[iso] = replace(
            country,
            signed_inr=checked_addition(
                country.signed_inr,
                amount_inr,
                at=f"Foreign tax country net, included row {position}, column amount_inr",
            ),
            signed_native=checked_addition(
                country.signed_native,
                row.amount,
                at=f"Foreign tax country net, included row {position}, column amount",
            ),
            event_count=country.event_count + 1,
        )
        isin = row.isin.upper()
        signed_inr_by_isin[isin] = checked_addition(
            signed_inr_by_isin.get(isin, _ZERO),
            amount_inr,
            at=f"Foreign tax ISIN net, included row {position}, column amount_inr",
        )
        if row.amount < 0:
            key = payment_key(row)
            paid = paid_by_payment.get(key, PaidWithholding(_ZERO, _ZERO, 0))
            paid_by_payment[key] = PaidWithholding(
                native=checked_subtraction(
                    paid.native,
                    row.amount,
                    at=f"Foreign tax payment paid total, included row {position}, column amount",
                ),
                inr=checked_subtraction(
                    paid.inr,
                    amount_inr,
                    at=(
                        f"Foreign tax payment paid total, included row {position}, "
                        "column amount_inr"
                    ),
                ),
                row_count=paid.row_count + 1,
            )

    return ForeignTaxNetting(
        countries=countries,
        signed_inr_by_isin=signed_inr_by_isin,
        paid_by_payment=paid_by_payment,
        included_rows=included_rows,
        cross_fy_reversal_report=cross_fy_reversal_report,
    )


def require_no_orphan_wht(
    netting: ForeignTaxNetting,
    os_dividend_lines: Iterable[ScheduleOsDividendLine],
    *,
    financial_year_start: date,
    financial_year_end: date,
) -> None:
    """Refuse paid WHT whose dividend is not among this FY's Schedule OS lines.

    Schedule FSI layers WHT onto ``(country, OTHER_SOURCES)``. A WHT row
    whose dividend is missing, typically a late adjustment for a prior-FY
    dividend, would be absorbed by an unrelated same-country dividend or
    interest row and inflate the credit; §90/§91 and Rule 128 tie the
    credit to its income event.

    A row is checked only when it is paid (negative amount), its ISIN's
    INR net is paid and its country's INR net is paid, because only then
    can it add credit that FSI keeps. Its :func:`payment_key` (source
    account, ISIN, payment date) must match one of ``os_dividend_lines``,
    which hold foreign-source ordinary dividends only. Reversals carry
    their booking date, not the dividend's, and only reduce credit, so
    they are skipped.

    Known v1 limitation: an orphan paid row and its own offsetting refund
    still fail when other matched WHT keeps the ISIN's net paid, although
    FSI would net the pair to zero.

    Raises:
        IbkrFaError: Counting the orphan ``(ISIN, payment_date)`` events.
            Their ISINs, dates and accounts are input, so the message
            doesn't print them (#1047).
    """
    paid_wht_events = {
        key
        for key in netting.paid_by_payment
        if netting.signed_inr_by_isin[key.isin] < 0
        and netting.countries[key.isin[:2]].signed_inr < 0
    }
    dividend_events_in_fy = {payment_key(line) for line in os_dividend_lines}
    orphan_wht_events = paid_wht_events - dividend_events_in_fy
    if orphan_wht_events:
        raise IbkrFaError(
            "Schedule FSI: foreign withholding tax was deducted on "
            f"{len(orphan_wht_events)} (ISIN, payment_date) event(s) in FY "
            f"{financial_year_start.isoformat()} → "
            f"{financial_year_end.isoformat()}, but no matching "
            "in-FY foreign-source ordinary dividend with the same "
            "ISIN AND payment_date (and source account, when set) was found in the Schedule OS "
            "foreign-dividend aggregation (`os_result.lines` — "
            "foreign-source ordinary dividends only; Indian-source "
            "dividends route to `domestic_lines`). Per "
            "§90/§91 + Rule 128(5), the foreign tax credit must be "
            "claimed in the FY in which the underlying foreign-"
            "income event is offered to Indian tax. The FSI WHT "
            "aggregator keys layers on (country, OTHER_SOURCES) — "
            "even within a country, an orphan WHT can be silently "
            "absorbed by an unrelated same-country dividend / "
            "interest row, inflating the credit claim incorrectly. "
            "This check requires a paid (negative-amount) WHT row to "
            "carry the same payment_date as its underlying dividend; "
            "a paid row with no in-FY dividend on that date is "
            "treated as orphaned (typically a late adjustment for a "
            "prior-FY dividend). Positive-amount (reversal) "
            "WHT rows carry the reversal booking date and are "
            "skipped — they reduce credit, not inflate it. This "
            "version can't carry withholding booked in this FY back "
            "to a dividend paid in an earlier one, and a run for that "
            "earlier FY reads only withholding booked inside it, so it "
            "would leave this withholding out without a warning. Claim "
            "its credit by hand in the return for the FY in which the "
            "dividend was offered to tax, and complete what this "
            "command would have written for this FY by hand. A follow-up "
            "issue tracks tagging the WHT model with source-kind so "
            "the layering can be made source-aware."
        )
