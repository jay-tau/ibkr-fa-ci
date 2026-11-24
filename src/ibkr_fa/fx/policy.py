"""The :class:`FxPolicy` Protocol that every FX-conversion policy implements.

A policy answers one question: given a date, what conversion rate
should I use? The exact semantics ("exact-date with carry-forward",
"last day of preceding month", "preceding day") are policy-specific;
the protocol just declares the function shape.

:class:`AuditedFxPolicy` extends :class:`FxPolicy` for policies that
can additionally surface the *actual* source date the rate was read
from (distinct from the policy's statutory specified date, when
carry-back occurs). The Schedule OS, Schedule CG and Robinhood
options compute require it, so each line's rate and dates come from
one resolution and the review CSV can show an auditor a (date, rate)
pair that matches an actual archive row (#160, #611).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ibkr_fa.fx.policy_rule115 import Rule115Resolution


class FxPolicy(Protocol):
    """Convert one foreign-currency date into an INR conversion rate.

    Implementations:

    - :class:`ibkr_fa.fx.policy_fa.FaExactDatePolicy` (Schedule FA).
    - :class:`ibkr_fa.fx.policy_rule115.Rule115Policy` (Schedule OS
      1a dividends — Rule 115(1), Explanation (2)(e)).
    - :class:`ibkr_fa.fx.policy_rule115.Rule115OtherSourcesPolicy`
      (Schedule OS 1b(ix) interest and ordinary business income —
      Rule 115(1), Explanation (2)(c)).
    - :class:`ibkr_fa.fx.policy_rule115_capital_gains.Rule115CapitalGainsPolicy`
      (Schedule CG STCG/LTCG — Rule 115(1), Explanation (2)(f)).
    - :class:`ibkr_fa.fx.policy_rule128.Rule128Policy` (Form 67 /
      Schedule TR — Rule 128(5)(ii)).

    Attributes:
        currency: ISO 4217 code of the source currency this policy
          knows how to convert (e.g., ``"USD"``). A single policy
          instance handles exactly one currency; multi-currency support
          composes multiple policies.

    Each policy owns its own data backend (e.g., the FA policy reads
    ``src/ibkr_fa/reference/sbi_ttbr_*.csv``); policies are free to
    cache or share data internally.
    """

    currency: ClassVar[str]

    def rate_for(self, event_date: date) -> Decimal:
        """Return the INR rate for one unit of :attr:`currency` on
        ``event_date``.

        Implementations should:

        - Apply their convention (carry-forward, preceding-month, etc.)
          deterministically. Two calls with the same ``event_date``
          must return the same :class:`~decimal.Decimal`.
        - Raise :class:`~ibkr_fa.exceptions.MissingFxRateError` (with
          structured context) when no rate is sourceable. Never return
          ``None``, ``0``, or a placeholder value.
        - Return a positive :class:`~decimal.Decimal` (rate per 1
          unit of :attr:`currency`).

        Args:
            event_date: Calendar date of the event to convert. Tax
                schedules use ``datetime.date`` (timezone-naive); time
                of day is not relevant for any Indian-tax FX rule.

        Returns:
            The conversion rate. e.g., for USD on a date when the SBI
            TT-Buying rate was ``83.40``, returns ``Decimal("83.40")``.

        Raises:
            MissingFxRateError: If no rate can be sourced for
                ``event_date`` under this policy's rules.
        """
        ...


@runtime_checkable
class AuditedFxPolicy(FxPolicy, Protocol):
    """An :class:`FxPolicy` that can also surface the *actual* source
    date the rate was read from.

    The standard :meth:`FxPolicy.rate_for` returns only the rate;
    when the policy's underlying lookup involves a discrepancy
    between the statutory "specified" date and the actual archive
    row used (e.g., :class:`~ibkr_fa.fx.policy_rule115.Rule115Policy`
    carrying back from a Sunday preceding-month-end to the prior
    Saturday's published rate), that discrepancy is invisible to
    downstream consumers. :meth:`rate_for_audited` exposes it.

    Compute functions that label a line with its specified date take
    this protocol, not :class:`FxPolicy`, and read the rate, the
    specified date and the rate date from one
    :class:`~ibkr_fa.fx.policy_rule115.Rule115Resolution`. The policy
    alone defines the specified date; compute never works it out.

    Implementations:

    - :class:`~ibkr_fa.fx.policy_rule115.Rule115Policy` and its
      subclasses :class:`~ibkr_fa.fx.policy_rule115.Rule115OtherSourcesPolicy`,
      :class:`~ibkr_fa.fx.policy_rule115_capital_gains.Rule115CapitalGainsPolicy`
      and :class:`~ibkr_fa.fx.policy_rule128.Rule128Policy`.
    """

    def rate_for_audited(self, event_date: date) -> Rule115Resolution:
        """Return the rate together with the actual source date.

        See :class:`~ibkr_fa.fx.policy_rule115.Rule115Resolution` for
        the return shape (``rate``, ``specified_date``,
        ``rate_date``). Same lookup algorithm and
        same raise contract as :meth:`FxPolicy.rate_for`.
        """
        ...
