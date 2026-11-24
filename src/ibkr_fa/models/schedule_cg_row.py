"""Schedule CG STCG line: one foreign short-term capital-gains row.

Each :class:`ScheduleCgStcgLine` represents one disposed tax lot whose
holding period is **<= 24 months** (the foreign-equity STCG threshold per
Section 2(42A) of the Income-tax Act, 1961, post the Finance (No. 2) Act,
2024 simplification), with the realized capital gain converted to INR via
Rule 115(1), Explanation (2)(f) (TTBR of the last day of the month immediately
preceding the **transfer** date — a single conversion at the disposal
event).

Rule 115 single-conversion rationale (T5 Codex review, PR #163):

Rule 115(1), read with Explanation (2)(f), prescribes the capital-gains
specified date as the last day
of the calendar month immediately preceding the month in which the
capital asset is transferred. For ordinary foreign-currency-cost
shares, both the cost basis and proceeds are converted at the same
transfer-date TTBR. A source with a statutory historical INR basis, such
as section 49(2AA) employee-share FMV, sets it as
:attr:`~ibkr_fa.models.closed_lot.ClosedLot.cost_basis_inr_override`, and the
CG engines use it in place of the converted USD cost.

The special conversion/reconversion pathway is the **first proviso to
section 48 plus Rule 115A**, and applies to non-residents transferring
shares/debentures of an Indian company. The second proviso is indexation.
For ordinary foreign-currency-cost shares held by a resident, *Havells
India Ltd v ACIT*, ITA 4695/Del/2012, para 14, supports computing the
foreign-currency gain first and then applying Rule 115.

The model is the CG counterpart to
:class:`~ibkr_fa.compute.schedule_os.ScheduleOsDividendLine`. The
whole-rupee portal figures come from
:mod:`~ibkr_fa.compute.schedule_cg_portal_amounts`; this dataclass
stores values at full :class:`~decimal.Decimal` precision.

Foreign-equity STCG threshold rationale:

* Under Section 2(42A), a "short-term capital asset" is any capital
  asset held for not more than 24 months (for assets other than listed
  Indian securities and a few other carve-outs).
* US-listed equities held by a resident Indian fall outside the
  carve-outs (no STT paid in India, not listed on an Indian stock
  exchange), so the 24-month threshold applies.
* Holding period is computed by calendar-month arithmetic
  (:func:`is_short_term_holding`) — the close date must be on or
  before ``open_date + 24 calendar months`` (with last-day-of-month
  clamping) to be classified as STCG.

The companion :class:`~ibkr_fa.compute.schedule_cg.ScheduleCgStcgComputeResult`
partitions :class:`~ibkr_fa.models.ClosedLot` records into:

* :class:`ScheduleCgStcgLine` rows (in-FY, <= 24mo, non-Indian-source).
* LTCG-deferred rows (in-FY, > 24mo) — handled by :mod:`ibkr_fa.compute.schedule_cg_ltcg`.
* Indian-source exclusions — defense-in-depth bucket only; the
  upstream Activity Statement parser already hard-rejects IN ISINs
  for FA purposes (#143), so this bucket is unreachable through the
  current parser path. Kept in the result shape so a future parser
  relaxation that admits IN ISINs does not silently route them
  through the foreign-STCG aggregate.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Final, Literal, Protocol

from ibkr_fa.invariants import require
from ibkr_fa.models._identifier_checks import is_isin_shape, is_iso_alpha2
from ibkr_fa.models._row_checks import require_amounts
from ibkr_fa.types import Isin, Ticker

if TYPE_CHECKING:
    from ibkr_fa.models.schedule_cg_ltcg_row import ScheduleCgLtcgLine

STCG_HOLDING_THRESHOLD_MONTHS: Final[int] = 24
"""Foreign-equity STCG holding-period threshold in **calendar months**.

Per Section 2(42A) of the Income-tax Act, 1961, a "short-term capital
asset" is an asset (other than the carve-out classes such as listed
Indian securities) held for not more than ``24`` months. Foreign-
equity holdings by a resident Indian fall outside the carve-outs, so
this 24-month threshold applies.

The compute layer applies this via :func:`is_short_term_holding` which
walks calendar-month arithmetic exactly: the cutoff date for a lot
opened on ``open_date`` is ``open_date + 24 calendar months`` (with
month-end day-cap; February-end rolls correctly). A close date on or
before the cutoff is STCG; a close date AFTER the cutoff is LTCG.

A naive ``720`` day approximation (24 * 30) under-counts: e.g. an
acquisition on 2024-04-01 with disposal on 2026-03-31 is 729 days but
< 24 calendar months, so STCG. The day-count approximation would
mis-route this to the LTCG bucket and understate Schedule CG Section A.
T3 (Codex CLI gpt-5.5) PR #163 review.
"""


@dataclass(frozen=True, slots=True)
class ScheduleCgExactAmounts:
    """A CG line's exact amounts, where its Decimal fields can't hold them (#1241).

    A Robinhood lot's share of a cost or sale split by quantity can be a
    rational such as a third. Its line then carries the exact amounts here,
    and each same-named Decimal field is
    :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of one of them. The
    filed portal totals and Schedule FSI nets add these instead.
    """

    cost_basis_usd: Fraction
    proceeds_usd: Fraction
    cost_basis_inr: Fraction
    proceeds_inr: Fraction
    realized_gain_inr: Fraction


class _CgLine(Protocol):
    """The fields :func:`require_exact_amounts` reads, shared by the STCG and LTCG lines."""

    @property
    def fx_rate_inr_per_usd(self) -> Decimal: ...

    @property
    def exact_amounts(self) -> ScheduleCgExactAmounts | None: ...


def exact_amount(
    line: _CgLine, column: Literal["cost_basis_inr", "proceeds_inr", "realized_gain_inr"]
) -> Fraction | None:
    """The line's exact ``column`` where its Decimal field rounds it, else ``None`` (#1241)."""
    return None if line.exact_amounts is None else getattr(line.exact_amounts, column)


def require_exact_amounts(line: _CgLine, name: str) -> None:
    """Check a line's :class:`ScheduleCgExactAmounts` against its Decimal fields.

    Each Decimal field must be
    :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of the exact amount of
    its name, the exact proceeds the exact USD proceeds at the line's rate,
    and the exact gain the exact proceeds less the exact cost. The INR cost
    isn't tied to the rate, as for a line without exact amounts.
    """
    # Imported here: a module-level import would run ibkr_fa.compute's
    # package init, which imports these models back.
    from ibkr_fa.compute.exact_inr import exact_fraction, is_exact_decimal  # noqa: PLC0415

    exact = line.exact_amounts
    rate = exact_fraction(line.fx_rate_inr_per_usd)
    require(
        type(exact) is ScheduleCgExactAmounts
        and rate is not None
        and all(type(getattr(exact, field.name)) is Fraction for field in fields(exact))
        and all(
            is_exact_decimal(getattr(line, field.name), getattr(exact, field.name))
            for field in fields(exact)
        )
        and exact.proceeds_inr == exact.proceeds_usd * rate
        and exact.realized_gain_inr == exact.proceeds_inr - exact.cost_basis_inr,
        what=(
            f"{name}.exact_amounts must hold the exact amounts its Decimal fields "
            "round, with proceeds_inr = proceeds_usd * fx_rate_inr_per_usd and "
            "realized_gain_inr = proceeds_inr - cost_basis_inr"
        ),
    )


def require_cg_line_amounts_and_holding_days(
    line: ScheduleCgStcgLine | ScheduleCgLtcgLine, class_name: str
) -> None:
    """Run the checks both CG line classes start with, in order (#1653).

    The finite amounts, the quantity, USD cost, rate and INR cost signs, the
    INR cost's source, then the dates and holding-period days. Messages passed
    directly to ``require`` start with ``class_name``. Each class checks its own
    holding-period class and calls :func:`require_cg_line_fx_issuer_and_derived`.
    """
    require_amounts(
        line,
        "quantity",
        "cost_basis_usd",
        "proceeds_usd",
        "fx_rate_inr_per_usd",
        "cost_basis_inr",
        "proceeds_inr",
        "realized_gain_inr",
    )
    require(
        line.quantity > 0,
        what=f"{class_name}.quantity must be positive",
        quantity=line.quantity,
        symbol=line.symbol,
        isin=line.isin,
    )
    require(
        line.cost_basis_usd >= 0,
        what=f"{class_name}.cost_basis_usd must be non-negative",
        cost_basis_usd=line.cost_basis_usd,
        symbol=line.symbol,
        isin=line.isin,
    )
    # T5 r5 (Codex bot) PR #163 P2: proceeds_usd may legitimately
    # be NEGATIVE (IBKR commissions on a tiny / deep-loss sale
    # can exceed gross proceeds, making net proceeds negative).
    # The model does not constrain its sign — the writer escapes
    # via csv_safe_signed.
    require(
        line.fx_rate_inr_per_usd > 0,
        what=f"{class_name}.fx_rate_inr_per_usd must be positive",
        fx_rate_inr_per_usd=line.fx_rate_inr_per_usd,
        symbol=line.symbol,
        isin=line.isin,
    )
    require(
        line.cost_basis_inr >= 0,
        what=f"{class_name}.cost_basis_inr must be non-negative",
        cost_basis_inr=line.cost_basis_inr,
        symbol=line.symbol,
        isin=line.isin,
    )
    require(
        bool(line.cost_basis_inr_source.strip()),
        what=f"{class_name}.cost_basis_inr_source must be non-empty",
        symbol=line.symbol,
        isin=line.isin,
    )
    # T5 r5 (Codex bot) PR #163 P2: proceeds_inr inherits the
    # sign of proceeds_usd (single conversion at a positive FX
    # rate). Negative proceeds_inr is therefore legitimate;
    # writer escapes via csv_safe_signed.
    require(
        line.close_date >= line.open_date,
        what=f"{class_name}.close_date must be on or after open_date",
        open_date=line.open_date,
        close_date=line.close_date,
        symbol=line.symbol,
        isin=line.isin,
    )
    require(
        line.holding_period_days == (line.close_date - line.open_date).days,
        what=f"{class_name}.holding_period_days must equal (close_date - open_date).days exactly",
        holding_period_days=line.holding_period_days,
        expected=(line.close_date - line.open_date).days,
        symbol=line.symbol,
        isin=line.isin,
    )
    require(
        line.holding_period_days >= 0,
        what=f"{class_name}.holding_period_days must be non-negative",
        holding_period_days=line.holding_period_days,
        symbol=line.symbol,
        isin=line.isin,
    )


def require_cg_line_fx_issuer_and_derived(
    line: ScheduleCgStcgLine | ScheduleCgLtcgLine,
    class_name: str,
    result_name: str,
    term: Literal["STCG", "LTCG"],
) -> None:
    """Run the checks both CG line classes end with, in order (#1653).

    The FX dates, the issuer country and ISIN, then the exact amounts or the
    two derived INR fields. Each message starts with ``class_name``; the
    refusal of an Indian issuer names ``result_name``'s Indian-source bucket
    and the foreign ``term`` aggregate.
    """
    require(
        line.fx_specified_date < line.close_date,
        what=(
            f"{class_name}.fx_specified_date must be strictly "
            "before close_date (Rule 115 capital-gains preceding-month-end is "
            "strictly before the transfer event)"
        ),
        fx_specified_date=line.fx_specified_date,
        close_date=line.close_date,
        symbol=line.symbol,
        isin=line.isin,
    )
    require(
        type(line.fx_rate_publication_date) is date,
        what=f"{class_name}.fx_rate_publication_date must be a date",
        fx_rate_publication_date=repr(line.fx_rate_publication_date),
        symbol=line.symbol,
        isin=line.isin,
    )
    require(
        line.fx_rate_publication_date <= line.fx_specified_date,
        what=(
            f"{class_name}.fx_rate_publication_date must be on or "
            "before fx_specified_date (carry-back goes strictly backwards "
            "in time; equal when no carry-back occurred)"
        ),
        fx_rate_publication_date=line.fx_rate_publication_date,
        fx_specified_date=line.fx_specified_date,
        symbol=line.symbol,
        isin=line.isin,
    )
    require(
        line.issuer_country_iso2 != "IN",
        what=(
            f"{class_name}.issuer_country_iso2 cannot be 'IN' "
            "(Indian-source rows belong in "
            f"{result_name}.excluded_indian_source, "
            f"not in the foreign {term} aggregate)"
        ),
        issuer_country_iso2=line.issuer_country_iso2,
        symbol=line.symbol,
        isin=line.isin,
    )
    require(
        is_iso_alpha2(line.issuer_country_iso2),
        what=(
            f"{class_name}.issuer_country_iso2 must be a 2-letter uppercase ISO 3166-1 alpha-2 code"
        ),
        issuer_country_iso2=line.issuer_country_iso2,
        symbol=line.symbol,
        isin=line.isin,
    )
    require(
        is_isin_shape(line.isin),
        what=(
            f"{class_name}.isin must match ISO 6166 (12 chars: 2 "
            "uppercase letters + 9 alphanumerics + 1 digit)"
        ),
        isin=line.isin,
        symbol=line.symbol,
    )
    # Imported here: a module-level import would run ibkr_fa.compute's
    # package init, which imports these models back.
    from ibkr_fa.compute.exact_inr import (  # noqa: PLC0415
        exact_difference,
        exact_product,
    )

    if line.exact_amounts is not None:
        require_exact_amounts(line, class_name)
        return
    # Both derived fields are checked against the exact result (#1184).
    # A failed product or difference, e.g. an overflow of 1E+999999 *
    # 86.20 (#937), is None and refuses as a mismatch.
    expected_proceeds = exact_product(line.proceeds_usd, line.fx_rate_inr_per_usd)
    require(
        expected_proceeds is not None and line.proceeds_inr == expected_proceeds,
        what=(
            f"{class_name}.proceeds_inr must equal "
            "proceeds_usd * fx_rate_inr_per_usd exactly (derived field)"
        ),
        proceeds_inr=line.proceeds_inr,
        proceeds_usd=line.proceeds_usd,
        fx_rate_inr_per_usd=line.fx_rate_inr_per_usd,
        symbol=line.symbol,
        isin=line.isin,
    )
    expected_gain = exact_difference(line.proceeds_inr, line.cost_basis_inr)
    require(
        expected_gain is not None and line.realized_gain_inr == expected_gain,
        what=(
            f"{class_name}.realized_gain_inr must equal "
            "proceeds_inr - cost_basis_inr exactly (derived field)"
        ),
        realized_gain_inr=line.realized_gain_inr,
        expected_gain=expected_gain,
        proceeds_inr=line.proceeds_inr,
        cost_basis_inr=line.cost_basis_inr,
        symbol=line.symbol,
        isin=line.isin,
    )


def is_short_term_holding(open_date: date, close_date: date) -> bool:
    """Return ``True`` iff the holding is short-term under Section 2(42A).

    Computes the 24-calendar-month cutoff: ``open_date + 24 months``
    (with last-day-of-month clamping for cases like Feb 28/29). Holding
    is short-term when ``close_date <= cutoff``; long-term otherwise.

    Stdlib-only (no ``dateutil.relativedelta``) — the project keeps a
    zero-runtime-dependency stance.

    Args:
        open_date: Acquisition date of the lot.
        close_date: Disposal date of the lot.

    Returns:
        ``True`` for STCG (<= 24 months), ``False`` for LTCG (> 24 months).
    """
    cutoff = _add_calendar_months(open_date, STCG_HOLDING_THRESHOLD_MONTHS)
    return close_date <= cutoff


def _add_calendar_months(d: date, months: int) -> date:
    """Add ``months`` calendar months to ``d`` with last-day-of-month clamping.

    Examples:
        ``_add_calendar_months(date(2024, 1, 31), 1)`` → ``date(2024, 2, 29)``
        (Feb 2024 has 29 days; the 31 is clamped to 29).

        ``_add_calendar_months(date(2024, 4, 1), 24)`` → ``date(2026, 4, 1)``.
    """
    total_months = (d.month - 1) + months
    new_year = d.year + total_months // 12
    new_month = total_months % 12 + 1
    last_day_of_new_month = calendar.monthrange(new_year, new_month)[1]
    new_day = min(d.day, last_day_of_new_month)
    return date(new_year, new_month, new_day)


@dataclass(frozen=True, slots=True)
class ScheduleCgStcgLine:
    """One foreign-source STCG line ready for Schedule CG.

    Each instance represents a single in-FY, non-Indian-source closed
    lot whose holding period satisfies the STCG threshold
    (``close_date - open_date <= 24 months``), with cost basis and
    proceeds pre-converted to INR via Rule 115(1), Explanation (2)(f),
    TTBR at the transfer preceding-month-end.

    Attributes:
        symbol: Ticker of the closed instrument.
        isin: ISIN of the closed instrument (ISO 6166).
        issuer_country_iso2: ISO 3166-1 alpha-2 country code derived
            from the ISIN's leading two characters. Always
            non-``"IN"`` in this dataclass — Indian-source rows live
            in :attr:`ScheduleCgStcgComputeResult.excluded_indian_source`.
        description: Verbatim audit copy of
            :attr:`~ibkr_fa.models.ClosedLot.description`, preserved
            so the user can trace any CG line back to the exact IBKR
            row.
        open_date: Date the lot was originally opened (acquired).
        close_date: Date the lot was closed (disposed). Always
            ``>= open_date``.
        holding_period_days: ``(close_date - open_date).days``. Always
            non-negative. The calendar-month STCG cutoff is enforced
            separately via :func:`is_short_term_holding` — a 730-day
            holding can still be STCG (24 calendar months > 720 days).
        quantity: Number of shares disposed in this slice. Strictly
            positive.
        cost_basis_usd: Cost basis in USD allocated to this slice
            (from :attr:`ClosedLot.cost_basis_money`). Non-negative
            (the underlying invariant on ``ClosedLot.cost_basis_money``
            is ``>= 0``; ``ScheduleCgStcgLine`` mirrors).
        proceeds_usd: Gross sale proceeds in USD allocated to this
            slice (from :attr:`ClosedLot.proceeds_money`).
            Non-negative.
        fx_specified_date: Rule 115 capital-gains specified date for the
            transfer event — last day of the calendar month
            immediately preceding :attr:`close_date`'s month. Always
            strictly before :attr:`close_date`. For ordinary purchased shares the
            capital-gains specified date is the transfer-date's
            preceding-month-end; the acquisition-date FX is NOT used
            for residents holding foreign equities (the open-event
            conversion/reconversion pathway in the first proviso to section 48
            plus Rule 115A is non-resident/Indian-company-only).
        fx_rate_publication_date: The SBI archive date the rate
            actually came from. Equals :attr:`fx_specified_date` when
            SBI published on the specified date itself; strictly
            earlier when the policy carried back over a weekend,
            holiday or unpublished day. The review writers show it
            so an auditor can match the rate to the SBI row (#741),
            as the Schedule OS lines do (#160).
        fx_rate_inr_per_usd: INR-per-USD rate applied at the transfer
            (close) event (Rule115CapitalGainsPolicy resolution).
            Strictly positive.
        cost_basis_inr: Statutory INR cost basis at full
            :class:`~decimal.Decimal` precision. For ordinary purchased shares,
            this is ``cost_basis_usd * fx_rate_inr_per_usd``. A lot with
            :attr:`~ibkr_fa.models.closed_lot.ClosedLot.cost_basis_inr_override`
            (section 49(2AA) employee shares) carries that INR amount instead.
        cost_basis_inr_source: Audit provenance for ``cost_basis_inr``.
        proceeds_inr: ``proceeds_usd * fx_rate_inr_per_usd`` at full
            :class:`~decimal.Decimal` precision. Non-negative. Same
            transfer-date FX as :attr:`cost_basis_inr`.
        realized_gain_inr: ``proceeds_inr - cost_basis_inr`` at full
            :class:`~decimal.Decimal` precision. May be negative (a
            STCG loss is a valid Schedule CG row — it offsets other
            short-term gains and, if a net loss, is carried forward
            for 8 AYs per Section 74).
        exact_amounts: The exact amounts where the lot's
            :class:`~ibkr_fa.models.closed_lot.ClosedLot` carries a rational
            share (#1241); each same-named Decimal field is then
            :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of one of them.
            ``None`` where every Decimal field is exact.

    Invariants enforced in :meth:`__post_init__`:

    * ``quantity``, ``cost_basis_usd``, ``proceeds_usd``,
      ``fx_rate_inr_per_usd``, ``cost_basis_inr``, ``proceeds_inr`` and
      ``realized_gain_inr`` are finite :class:`~decimal.Decimal` values
    * ``quantity > 0``
    * ``cost_basis_usd >= 0``
    * ``fx_rate_inr_per_usd > 0``
    * ``cost_basis_inr >= 0``
    * ``close_date >= open_date``
    * ``holding_period_days == (close_date - open_date).days``
    * ``holding_period_days >= 0``
    * Calendar-month STCG cutoff: ``close_date <= open_date +
      24 calendar months`` (per :func:`is_short_term_holding`)
    * ``fx_specified_date < close_date``
    * ``fx_rate_publication_date`` is a :class:`~datetime.date` (not a
      :class:`~datetime.datetime`) and ``<= fx_specified_date``
    * ``issuer_country_iso2 != "IN"`` and is a 2-letter uppercase
      ISO 3166-1 alpha-2 code
    * ``isin`` matches ISO 6166
    * ``proceeds_inr == proceeds_usd * fx_rate_inr_per_usd`` and
      ``realized_gain_inr == proceeds_inr - cost_basis_inr``, each
      against the exact result of
      :func:`~ibkr_fa.compute.exact_inr.exact_product` or
      :func:`~ibkr_fa.compute.exact_inr.exact_difference` (#1184), which
      widen the active context's precision to at least
      :data:`~ibkr_fa.compute.exact_inr.EXACT_SUM_DIGITS` and never round.
      A product or difference that can't be computed exactly refuses with
      the same message. ``cost_basis_inr`` is not tied to the rate,
      because a section 49(2AA) lot carries its own INR cost.
    * With ``exact_amounts``, those two checks hold of the exact amounts
      instead, and each Decimal field must be the exact amount's
      :func:`~ibkr_fa.compute.exact_inr.exact_decimal`
      (:func:`require_exact_amounts`).

    Notably absent: ``proceeds_usd >= 0`` and ``proceeds_inr >= 0``.
    Net proceeds can legitimately be NEGATIVE — the underlying
    :class:`ClosedLot` permits a negative ``proceeds_money``: commissions
    on a tiny / deep-loss sale can exceed the sale price (IBKR Flex
    derives proceeds as ``CostBasis + FifoPnlRealized``, and the
    realized P/L can fall below ``-CostBasis``). Forcing the value
    non-negative would abort the entire ``schedule-cg`` run on
    legitimate input. The writer escapes
    negative proceeds via :func:`~ibkr_fa.output._writer_contract.csv_safe_signed` so the CSV-
    formula-injection guard does not trip.
    """

    symbol: Ticker
    isin: Isin
    issuer_country_iso2: str
    description: str
    open_date: date
    close_date: date
    holding_period_days: int
    quantity: Decimal
    cost_basis_usd: Decimal
    proceeds_usd: Decimal
    fx_specified_date: date
    fx_rate_publication_date: date
    fx_rate_inr_per_usd: Decimal
    cost_basis_inr: Decimal
    cost_basis_inr_source: str
    proceeds_inr: Decimal
    realized_gain_inr: Decimal
    exact_amounts: ScheduleCgExactAmounts | None = None

    def __post_init__(self) -> None:
        require_cg_line_amounts_and_holding_days(self, "ScheduleCgStcgLine")
        # T3 (Codex CLI) PR #163 review: classification is by calendar-
        # month arithmetic (Section 2(42A) "twenty-four months"), not
        # by a 720-day approximation. The model invariant therefore
        # checks the calendar-month cutoff directly rather than a
        # day-count ceiling — a lot acquired 2024-04-01 and disposed
        # 2026-03-31 is 729 days but still <= 24 calendar months, so
        # STCG-eligible. The compute layer guarantees only short-term
        # lots reach the constructor; this defense-in-depth check
        # rejects mis-routed LTCG slipping through.
        require(
            is_short_term_holding(self.open_date, self.close_date),
            what=(
                "ScheduleCgStcgLine: close_date must be on or before "
                f"open_date + {STCG_HOLDING_THRESHOLD_MONTHS} calendar months "
                "(foreign-equity STCG threshold per Section 2(42A); long-term "
                "lots belong in the LTCG bucket — see #30)"
            ),
            open_date=self.open_date,
            close_date=self.close_date,
            symbol=self.symbol,
            isin=self.isin,
        )
        require_cg_line_fx_issuer_and_derived(
            self, "ScheduleCgStcgLine", "ScheduleCgStcgComputeResult", "STCG"
        )
