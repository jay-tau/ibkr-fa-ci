"""Schedule FA Table A2 for the Fidelity NetBenefits ESPP account (#I11).

Table A2 discloses the custodial ACCOUNT itself. Its Dec-31 closing balance is
the account's market value = held securities + cash. For the supported case the
securities value is the SAME resolved Dec-31 close that Table A3 already files —
so A2 closing (securities USD x Dec-31 TTBR) must equal ``Σ A3 closing_value_inr``
exactly, or the build refuses — and the cash is PROVEN ``$0``, never assumed. The
monthly statements do not provide a complete daily account-value series, so the
peak balance stays manual-fill unless the caller passes a user-attested peak.

The row goes through :func:`~ibkr_fa.compute.fa_a2.build_fa_a2_row`, which files
the securities USD x Dec-31 TTBR.

The cash-zero proof refuses (``NetBenefitsFaError``) unless EVERY leg holds — it
proves zero, it does not infer it from an absence of rows:

* every emitted lot was acquired ON Dec-31, and the caller's lot proof
  (:mod:`ibkr_fa.compute.espp_lot_completeness`) shows from the statements and
  confirmations that no other lot was held in the CY ⇒ no shares were held
  earlier ⇒ no dividend could have been received before/at year-end;
* ``round(shares x price, 2) == contribution`` per lot ⇒ payroll cash was fully
  consumed by the purchase, no residual;
* the account opened WITHIN the calendar year ⇒ no prior-year cash carried in,
  and no future opening date (which would invert the window and skip the scan);
* no INTRA-CY disposal ⇒ no sale proceeds sat as cash (a *gap-window* lot sold
  the NEXT CY was still held at Dec-31, so its proceeds did not exist as
  year-end cash): the closed-lots export lists none, and the same lot proof
  shows that no lot other than the Dec-31 purchase was held to be sold;
* the monthly SPS statements GAP-FREE cover ``[account-opening, Dec-31]`` and
  show NO dividend / withholding / MMKT-sweep activity dated ON OR BEFORE Dec-31
  (the scan is NOT bounded below by the configured opening date, so a misconfigured
  opening can't hide earlier in-statement income) — a missing month refuses (we
  cannot prove "no income" from statements we never saw), and a PRICED Dec-31
  statement refuses (its stated account NAV must be filed manually — this pipeline
  derives A2 only from the unpriced year-end case). The year-end report is excluded
  from coverage (its silence is not income proof — see #I10), but an income row it
  prints is evidence (#1249): one dated on or before Dec-31 refuses, as on an SPS
  statement. That scan runs after the coverage walk.

That closed set (payroll residual, prior-year carry-in, dividends/MMKT, sale
proceeds) is the only way a Fidelity SPS ESPP account can hold cash, and the
supported Dec-31 statement is unpriced, so the proof above is the guard. The proof
sees only what the statement parser returns, so that parser refuses any dated line
(one holding an ``MM/DD`` token, whatever precedes it) or ``Total`` line it doesn't
read or shape-check (see :mod:`ibkr_fa.parser.netbenefits.statement_pdf`): cash
activity from any other source refuses rather than passing as absent.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from string import ascii_lowercase
from typing import TYPE_CHECKING, Final

from ibkr_fa.compute.exact_inr import checked_product, checked_sum
from ibkr_fa.compute.fa_a2 import FaA2PeakInput, build_fa_a2_row
from ibkr_fa.compute.source_coverage import first_coverage_gap
from ibkr_fa.invariants import require
from ibkr_fa.models.fa_a2_row import FaA2Row
from ibkr_fa.models.net_asset_value import NetAssetValueSummary
from ibkr_fa.normalize.netbenefits.statement_label import statement_among_all_reports as _statement
from ibkr_fa.parser.netbenefits.statement_pdf import (
    DIV_TABLE,
    MMKT_TABLE,
    WHT_TABLE,
    ReportType,
)

if TYPE_CHECKING:
    from ibkr_fa.models.account_identity import AccountIdentity
    from ibkr_fa.models.fa_a3_row import FaA3Row
    from ibkr_fa.parser.netbenefits.confirmation_pdf import EspPurchase
    from ibkr_fa.parser.netbenefits.statement_pdf import StatementData


class NetBenefitsA2Error(Exception):
    """Internal marker; the pipeline wraps proof failures in NetBenefitsFaError."""


def prove_dec31_cash_zero(
    *,
    purchases: list[EspPurchase],
    statements: list[StatementData],
    account_opening_date: date,
    calendar_year: int,
) -> None:
    """Prove the account's Dec-31 cash is exactly ``$0`` or raise
    :class:`NetBenefitsA2Error`. See the module docstring for the legs.

    Refusals name a statement by its 1-based place in ``statements`` (every
    report type, sorted by period start), an income event by its 1-based row in
    its statement table, and coverage or the opening date by the config field;
    never a value."""
    cy_start = date(calendar_year, 1, 1)
    cy_end = date(calendar_year, 12, 31)

    if not cy_start <= account_opening_date <= cy_end:
        raise NetBenefitsA2Error(
            f"netbenefits.account.account_opening_date is outside the FA year {calendar_year}; "
            f"the Dec-31 cash can't be proven zero (a prior-year opening could carry cash in, a "
            f"later one contradicts a {calendar_year} holding) — file A2 manually"
        )

    for p in purchases:
        # Payroll fully consumed by the purchase ⇒ no residual cash. Mirror the
        # #309 parser's shares-display-rounding tolerance (the printed share count
        # is only 3-4 dp); an EXACT-cent check here would false-refuse a valid lot
        # whose 3-dp shares don't reconstruct the contribution to the cent.
        residual = abs(p.shares * p.purchase_price_per_share_usd - p.contribution_usd)
        tol = p.purchase_price_per_share_usd * Decimal("0.0005") + Decimal("0.005")
        if residual > tol:
            # A backstop: the confirmation parser applies the same check.
            raise NetBenefitsA2Error(
                "a purchase confirmation's shares x purchase price differs from its Accumulated "
                "Contributions by more than the rounding tolerance; residual cash can't be "
                "proven zero"
            )

    window_start = max(cy_start, account_opening_date)
    _require_income_free_sps_coverage(statements, window_start, cy_end)


def _require_income_free_sps_coverage(
    statements: list[StatementData], window_start: date, window_end: date
) -> None:
    """Refuse unless the monthly SPS statements GAP-FREE cover ``[window_start,
    window_end]`` AND no statement carries dividend/withholding/MMKT activity on or
    before ``window_end`` (the income scan is NOT bounded below by the configured
    opening date — a misconfigured opening must not let earlier in-statement income
    hide). Year-end reports are scanned after the coverage walk (#1249).
    A priced Dec-31 statement of ANY report type (incl. a priced Year-End Report,
    whose stated NAV is the authoritative Dec-31 account value incl. accrued income)
    is refused: this pipeline derives A2 only from the unpriced year-end case; a
    stated account NAV must be filed manually."""
    # The priced-Dec-31 refusal spans ALL report types — a priced YEAR_END report
    # carries the authoritative Dec-31 account NAV that the securities-only
    # derivation can't reproduce, so it must not be excluded with the SPS-only scan.
    for number, s in enumerate(statements, start=1):
        if s.account_nav_usd is not None and s.period_start <= window_end <= s.period_end:
            raise NetBenefitsA2Error(
                f"{_statement(number)} is a PRICED Dec-31 statement (it states an account NAV); "
                f"its stated account value is the authoritative A2 closing balance — file Table "
                f"A2 from it manually (this pipeline derives A2 only from the unpriced year-end "
                f"case)"
            )
    sps = sorted(
        (
            (number, s)
            for number, s in enumerate(statements, start=1)
            if s.report_type is ReportType.STOCK_PLAN_SERVICES
        ),
        key=lambda numbered: numbered[1].period_start,
    )
    for number, s in sps:
        if (
            s.core_holding is not None
            and s.period_end >= window_start
            and s.period_start <= window_end
        ):
            raise NetBenefitsA2Error(
                f"{_statement(number)} lists a Core Account money-market fund holding in the "
                "FA year; it is a "
                f"separate foreign asset this tool does not yet auto-disclose — {_FILE_FUND}"
            )
    # Scan EVERY SPS statement for in-window income — independent of the coverage
    # walk below, which early-returns once Dec-31 is covered (so a later
    # duplicate/overlapping statement must not slip past unscanned). The year-end
    # report's income SILENCE is not proof (#I10), so coverage stays SPS-only; the
    # monthly covering each date itemises any actual income.
    _refuse_income(sps, window_end)
    # Gap-free coverage walk over [window_start, window_end].
    gap = first_coverage_gap(
        ((s.period_start, s.period_end) for _, s in sps),
        window_start=window_start,
        window_end=window_end,
    )
    if gap is not None and gap.short_end:
        raise NetBenefitsA2Error(
            f"the monthly SPS statements end before Dec-31, so they don't cover {_WINDOW}; "
            f"supply the missing month(s)"
        )
    if gap is not None:
        raise NetBenefitsA2Error(
            f"the monthly SPS statements leave a gap in {_WINDOW}; can't prove the account held "
            f"no cash-generating activity there — supply the missing monthly statement"
        )
    # A year-end report's silence proves nothing, but an income row it prints is
    # evidence (#1249). Scanned last, so an input that refused before keeps its refusal.
    _refuse_income(
        [
            (number, s)
            for number, s in enumerate(statements, start=1)
            if s.report_type is ReportType.YEAR_END
        ],
        window_end,
    )


def _refuse_income(numbered: list[tuple[int, StatementData]], window_end: date) -> None:
    """Refuse the first dividend, withholding or MMKT sweep dated on or before
    ``window_end`` in the ``(number, statement)`` pairs."""
    for number, s in numbered:
        for day, kind, table, row in _income_rows(s):
            if day > window_end:
                continue
            where = f"{_statement(number)}, row {row} of its {table} table"
            if kind == "MMKT sweep":
                raise NetBenefitsA2Error(
                    f"the money-market sweep at {where}, dated on/before year-end, created a FID "
                    f"Treasury MMKT fund holding (the net dividend is swept into the core fund) — "
                    f"a SEPARATE foreign asset this tool does not yet auto-disclose; {_FILE_FUND}"
                )
            raise NetBenefitsA2Error(
                f"the {kind} at {where} credits the account on/before year-end; its Dec-31 cash "
                f"balance can't be proven zero — file Table A2 manually"
            )


# The window the monthly statements must cover, as refusals name it.
_WINDOW: Final = (
    "the window from the later of Jan-1 and netbenefits.account.account_opening_date through Dec-31"
)
# The remedy both money-market fund refusals end with: the filer discloses the fund.
_FILE_FUND: Final = (
    "file the fund's own Schedule FA Table A3 holding and include its value in the A2 "
    "account balances manually"
)


def _income_rows(s: StatementData) -> Iterator[tuple[date, str, str, int]]:
    """Each income event's date, kind, statement table and 1-based row there."""
    # MMKT sweeps first: the net dividend is swept into the fund, and the sweep's
    # refusal is the more useful one (it names the deferred Table A3 fund holding),
    # so surface it ahead of the dividend/withholding legs.
    for row, m in enumerate(s.mmkt_buys, start=1):
        yield m.date, "MMKT sweep", MMKT_TABLE, row
    for row, d in enumerate(s.dividends, start=1):
        yield d.date, "dividend", DIV_TABLE, row
    for row, w in enumerate(s.withholdings, start=1):
        yield w.date, "withholding", WHT_TABLE, row


@dataclass(frozen=True, slots=True)
class NetBenefitsA2Result:
    """The NetBenefits A2 row."""

    row: FaA2Row


def build_netbenefits_a2_row(
    *,
    identity: AccountIdentity,
    securities_inr: Decimal,
    securities_usd: Decimal,
    ttbr_dec31: Decimal,
    peak: FaA2PeakInput | None = None,
) -> NetBenefitsA2Result:
    """Build the Table A2 row: closing = securities (Σ A3) + proven-$0 cash,
    gross = 0 (income-free proven), and ``peak`` (``None`` = manual-fill).

    Raises:
        InvariantError: When a total is not a finite ``Decimal`` (securities
            non-negative, TTBR positive), ``securities_usd x ttbr_dec31``
            can't be computed exactly, or ``securities_inr`` (Σ A3 closing)
            differs from that exact product (#1250).
    """
    require(
        isinstance(securities_inr, Decimal) and securities_inr.is_finite() and securities_inr >= 0,
        what="NetBenefits A2 needs a finite non-negative Σ A3 closing INR",
        securities_inr=securities_inr,
    )
    require(
        isinstance(securities_usd, Decimal) and securities_usd.is_finite() and securities_usd >= 0,
        what="NetBenefits A2 needs a finite non-negative securities USD total",
        securities_usd=securities_usd,
    )
    require(
        isinstance(ttbr_dec31, Decimal) and ttbr_dec31.is_finite() and ttbr_dec31 > 0,
        what="NetBenefits A2 needs a finite positive December 31 TTBR",
        ttbr_dec31=ttbr_dec31,
    )
    nav_closing_inr = checked_product(
        securities_usd, ttbr_dec31, at="NetBenefits A2 closing, securities INR"
    )
    require(
        securities_inr == nav_closing_inr,
        what="NetBenefits A2 closing: Σ A3 closing INR differs from securities USD x Dec-31 TTBR",
        a3_closing_inr=securities_inr,
        nav_closing_inr=nav_closing_inr,
    )
    row = build_fa_a2_row(
        identity=identity,
        # No priced Dec-31 NAV to cross-check (CY2025 unpriced).
        nav=NetAssetValueSummary(
            closing_total=securities_usd, refusal_reason=None, cross_checked=False
        ),
        closing_ttbr_dec31=ttbr_dec31,
        gross_components_inr={},
        period_end_cash_base_ccy=Decimal("0"),  # proven zero
        peak=peak,
    )
    return NetBenefitsA2Result(row=row)


def prove_and_build_netbenefits_a2(
    *,
    identity: AccountIdentity,
    purchases: list[EspPurchase],
    statements: list[StatementData],
    calendar_year: int,
    a3_rows: Sequence[FaA3Row],
    securities_usd: Decimal,
    ttbr_dec31: Decimal,
    peak: FaA2PeakInput | None = None,
) -> NetBenefitsA2Result:
    """Sum the A3 closing values exactly, prove the Dec-31 cash is zero (reads
    ``statements``), then build the A2 row (securities = Σ A3 closing + the
    proven-$0 cash) with ``peak`` (``None`` = manual-fill). Raises
    :class:`NetBenefitsA2Error` on any unprovable leg, and ``InvariantError``
    when the A3 closing values can't be added exactly. Performs NO write — the
    caller writes after every proof so a failure leaves no file behind."""
    closings = [row.closing_value_inr for row in a3_rows]
    # The A2 build refuses unless the exact Σ A3 closing equals the exact
    # securities USD x Dec-31 TTBR (#1250).
    securities_inr = checked_sum(
        enumerate(closings, start=1),
        at=lambda position: (
            f"NetBenefits A2 closing, input lot {position}, field closing_value_inr"
        ),
    )
    prove_dec31_cash_zero(
        purchases=purchases,
        statements=statements,
        account_opening_date=identity.opening_date,
        calendar_year=calendar_year,
    )
    return build_netbenefits_a2_row(
        identity=identity,
        securities_inr=securities_inr,
        securities_usd=securities_usd,
        ttbr_dec31=ttbr_dec31,
        peak=peak,
    )


# ADR 0021: Fidelity Stock Plan Services, as casefolded ASCII letters.
_FIDELITY_STOCK_PLAN_SERVICES: Final = frozenset(
    {"fidelitystockplanservices", "fidelitystockplanservicesllc"}
)


def names_fidelity_stock_plan_services(institution_name: str) -> bool:
    """Whether an A2 institution name is Fidelity Stock Plan Services (ADR 0021).

    The name is casefolded and only its ASCII letters are kept, so case, spaces
    and punctuation don't matter (``"Fidelity Stock Plan Services, LLC"``).
    """
    letters = "".join(ch for ch in institution_name.casefold() if ch in ascii_lowercase)
    return letters in _FIDELITY_STOCK_PLAN_SERVICES
