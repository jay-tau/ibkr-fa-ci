"""Rule 115(1), Explanation (2)(f), FX policy for capital-asset transfers.

Income-tax Rules, 1962, Rule 115(1) read with Explanation (2)(f) prescribes that
"income chargeable
under the head 'Capital gains' on the transfer of a capital asset" must
be converted to INR using the SBI TT-Buying rate of the **last day of the
calendar month immediately preceding the month in which the capital asset
is transferred**.

The preceding-month-end semantic is preserved verbatim under the
Income-tax Rules, 2026 framework (cross-reference structure identical
to the Rule 115(1), Explanation (2)(e) dividend pathway documented in
:mod:`ibkr_fa.fx.policy_rule115`). The TT-Buying rate itself is
defined in Rule 207(3)(b) of the 2026 Rules.

This module exists as a thin subclass of
:class:`~ibkr_fa.fx.policy_rule115.Rule115Policy` to:

* Distinguish the policy in raised :class:`MissingFxRateError` instances
  via the Rule 115(1), Explanation (2)(f), citation and an event-noun of
  ``"a capital-asset transfer"`` in the rendered message — users
  debugging a missing-rate failure should not be told the error is
  about a dividend when the source event was a stock disposal.
* Allow future divergence between the dividend and capital-gains FX
  resolution algorithms without touching the dividend pathway.

For Schedule CG of foreign equities held by Indian residents, this
policy is invoked **once per disposal** — at the transfer-event date
(last calendar day of the preceding month of the lot's close date) —
to convert ``(proceeds_usd - cost_basis_usd)`` to INR. Both the
``cost_basis_inr`` and ``proceeds_inr`` audit values use the same
transfer-date TTBR; the realized INR gain is the difference at that
rate.

This single-conversion semantic is supported by *Havells India Ltd v
ACIT*, ITA 4695/Del/2012, para 14. The special conversion/reconversion
pathway is the **first proviso to section 48 plus Rule 115A**, and
applies only to non-residents transferring shares or debentures of an
Indian company. The second proviso is indexation.

Authorities:

* https://indiankanoon.org/doc/91892030/ — *Havells*, para 14.
* https://indiankanoon.org/doc/152820106/ — *New Enterprise Associates
  15 LP*, paras 4.31 and 7.16 (Rule 115 specified date).
"""

from __future__ import annotations

from typing import ClassVar

from ibkr_fa.fx.policy_citations import _ay_2026_27
from ibkr_fa.fx.policy_rule115 import Rule115Policy


class Rule115CapitalGainsPolicy(Rule115Policy):
    """Schedule CG FX policy under Rule 115(1), Explanation (2)(f).

    Same preceding-month-end + carry-back algorithm as
    :class:`Rule115Policy`, but with a distinct :attr:`POLICY_KIND`
    discriminator and a capital-gains-flavoured event-noun in raised
    :class:`MissingFxRateError` messages so users can distinguish a
    missing-rate failure on a dividend (Schedule OS) from one on a
    capital-asset transfer (Schedule CG) at a glance.

    See :class:`Rule115Policy` for the full algorithm specification.

    Attributes:
        POLICY_KIND: Capital-gains citation — overrides the dividend-
            flavoured ``"Rule 115(1), Explanation (2)(e)"`` of the parent class.
    """

    POLICY_KIND: ClassVar[str] = _ay_2026_27.POLICY_KIND_RULE_115_2F
    _CITATION_ATTR: ClassVar[str] = "POLICY_KIND_RULE_115_2F"

    def _policy_kind_for(self) -> str:
        return f"{self.policy_kind_str()} (preceding-month-end for a capital-asset transfer)"
