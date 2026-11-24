"""Distribution kinds shared by every dividend source.

Every source that builds a :class:`~ibkr_fa.models.Dividend` sets its
:attr:`~ibkr_fa.models.Dividend.kind`. The IBKR Activity Statement
parser classifies the description with
:func:`~ibkr_fa.parser.activity_statement.dividends.classify_distribution`;
the other source adapters set the kind directly. Schedule OS dispatches
on the kind through :data:`ALLOWED_DISTRIBUTION_KINDS`, and its review
writers render the kind's value, so nothing downstream re-parses the
description.

Schedule OS raises on :attr:`DistributionKind.UNRECOGNISED` /
:attr:`DistributionKind.MALFORMED` /
:attr:`DistributionKind.PAYMENT_IN_LIEU_OF_DIVIDEND` /
:attr:`DistributionKind.RETURN_OF_CAPITAL` /
:attr:`DistributionKind.LONG_TERM_CAPITAL_GAIN` /
:attr:`DistributionKind.CASH_IN_LIEU` when such a row reaches its
dispatch in the FY. Schedule CG refuses the last three kinds on any date,
and Schedule FA in the calendar year (#1310). Schedule FA Table A3
column 10 sums the gross amounts of the other kinds' rows without
dispatching on the kind.

Issue #153.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class DistributionKind(StrEnum):
    """Distribution kind of one :class:`~ibkr_fa.models.Dividend`, set by its source.

    The string values are the human-readable labels shown in the
    Schedule OS review outputs' "Distribution type" column. They are
    the keywords IBKR emits in the Activity Statement ``Description``
    column, which the IBKR parser matches case-insensitively.

    The members fall into three groups:

    * **Allow-listed (ordinary dividend) kinds** — flow into Schedule
      OS row 1a(i), retaining source country for FSI/FTC. Treated identically by the FX policy
      (Rule 115(1), Explanation (2)(e), preceding-calendar-month-end SBI TTBR):
      :attr:`CASH_DIVIDEND`, :attr:`SPECIAL_DIVIDEND`,
      :attr:`CHOICE_DIVIDEND`.

    * **Refused kinds** — ibkr-fa can't yet file any of these, so a
      command that reaches one refuses:

      * :attr:`PAYMENT_IN_LIEU_OF_DIVIDEND` is other income, not a
        dividend. ibkr-fa can't yet file an IBKR payment in lieu as other
        income with its withholding, so Schedule OS refuses an in-FY row
        with
        :class:`~ibkr_fa.compute.schedule_os.PaymentInLieuOfDividendError`
        (#1281).
      * :attr:`RETURN_OF_CAPITAL`, :attr:`LONG_TERM_CAPITAL_GAIN` and
        :attr:`CASH_IN_LIEU` aren't ordinary dividends. Schedule OS
        refuses an in-FY row, Schedule CG a row on any date and Schedule
        FA a row in the calendar year, with
        :class:`~ibkr_fa.compute.schedule_os.NonOrdinaryDistributionError`
        (#1310).

    * **Classification failure markers** — set by the IBKR parser
      when it can't classify a description:
      :attr:`UNRECOGNISED` (kind keyword didn't match any known kind)
      and :attr:`MALFORMED` (type-slice missing the ``" USD "``
      currency marker, or a known kind whose whole type-slice isn't
      ``<kind> USD <amount> per Share[ (Ordinary Dividend)]`` or
      ``Cash in Lieu USD <amount>``). Compute layer raises
      :class:`~ibkr_fa.compute.schedule_os.UnrecognisedDistributionTypeError`
      when an UNRECOGNISED / MALFORMED row reaches a Schedule OS
      dispatch path; Schedule FA Table A3 ignores the discriminator
      and sums the row's gross amount regardless.

    The string values are stable: they're exported as the canonical
    lookup key for any future external integration that wants to
    filter by distribution kind without depending on Python.
    """

    CASH_DIVIDEND = "Cash Dividend"
    SPECIAL_DIVIDEND = "Special Dividend"
    CHOICE_DIVIDEND = "Choice Dividend"

    PAYMENT_IN_LIEU_OF_DIVIDEND = "Payment In Lieu Of Dividend"
    RETURN_OF_CAPITAL = "Return Of Capital"
    LONG_TERM_CAPITAL_GAIN = "Long-Term Capital Gain"
    CASH_IN_LIEU = "Cash in Lieu"

    UNRECOGNISED = "<unrecognised>"
    MALFORMED = "<malformed>"


ALLOWED_DISTRIBUTION_KINDS: Final[frozenset[DistributionKind]] = frozenset(
    {
        DistributionKind.CASH_DIVIDEND,
        DistributionKind.SPECIAL_DIVIDEND,
        DistributionKind.CHOICE_DIVIDEND,
    }
)
"""The ordinary-dividend kinds that flow into Schedule OS line 1a.

Pinned in spec order: matches the README allow-list and the legacy
``_ALLOWED_DISTRIBUTION_TYPES`` tuple in
:mod:`ibkr_fa.compute.schedule_os` byte-for-byte. Compute-layer
dispatch uses ``div.kind in ALLOWED_DISTRIBUTION_KINDS`` for O(1)
membership testing.
"""
