"""Form 67 / FTC FX policy under Rule 128(5)(ii) of the Income-tax Rules, 1962.

Rule 128 governs the conversion of foreign-currency tax payments to
INR for the foreign-tax-credit (FTC) computation surfaced on Form 67
and Schedule TR. Sub-clause **(5)(ii)** specifies the FX convention:

  > "the credit shall be determined by conversion of the currency of
  > payment of foreign tax at the telegraphic transfer buying rate on
  > the last day of the month immediately preceding the month in which
  > such tax has been paid or deducted."

Source: https://indiankanoon.org/doc/82134701/ (Indian Kanoon's
authoritative text of Rule 128 sub-section (5), verified June 2026).

This is structurally identical to Rule 115(1), Explanation (2)(e) for dividends — the
SBI TTBR of the preceding calendar-month-end of the event date — so
:class:`Rule128Policy` reuses the entire :class:`Rule115Policy`
algorithm (preceding-month-end lookup + carry-back + SBI TTBR archive
+ sanity bounds) and only overrides the citation tag.

Issue-tracker citation correction (#34): the original sub-issue text
describes Rule 128 as "TTBR of the day **immediately preceding** the
date tax was paid abroad", and the legacy package-level docstring
echoed that phrasing. **Both are wrong** — Rule 128(5)(ii) uses
preceding-month-end, not preceding-day. This module fixes both the
implementation and the documentation. The "preceding day" wording
likely arose from a confused reading of Rule 26 (which IS preceding-
day, for TDS/WHT *deducted by an Indian payer* on payments TO a
non-resident — out of scope for ibkr-fa) being misapplied to Rule 128
(which is the FTC rule for tax paid BY an Indian resident on foreign
income).

Differences from :class:`Rule115Policy` (which is dividends-specific
under Rule 115(1), Explanation (2)(e)):

* **POLICY_KIND**: ``"Rule 128(5)(ii)"`` instead of
  ``"Rule 115(1), Explanation (2)(e)"``. Rendered into
  :class:`~ibkr_fa.exceptions.MissingFxRateError` so the user / auditor
  knows which rule failed when an FTC rate is missing.
* **Event noun**: the overridden :meth:`_policy_kind_for` renders
  ``"foreign tax paid"`` instead of ``"a dividend"`` — same
  actionable shape, correct audit-trail wording.
* **Currency**: ``"USD"`` for v1 (mirrors the existing policies);
  multi-currency support is tracked at issue #52.

The lookup algorithm + SBI TTBR archive + carry-back behaviour are
inherited unchanged from the parent class. A FTC computation that
needs to convert (e.g.) a USD 12.34 dividend-WHT line dated
2025-08-15 to INR therefore looks up the SBI TT-Buying rate for
**2025-07-31** (or carries back to the most recent prior published
business day if Jul 31 was a holiday).

Built by :attr:`ibkr_fa.fx.context.TaxFxContext.wht` for the Form 67
and Schedule FSI foreign-tax conversions; Schedule TR reads the
converted FSI rows.
"""

from __future__ import annotations

from typing import ClassVar

from ibkr_fa.fx.policy_citations import _ay_2026_27
from ibkr_fa.fx.policy_rule115 import Rule115Policy


class Rule128Policy(Rule115Policy):
    """Form 67 / FTC FX policy under Rule 128(5)(ii).

    Subclass of :class:`Rule115Policy` that reuses the preceding-
    calendar-month-end lookup algorithm + SBI TTBR archive +
    carry-back behaviour, overriding only the citation tag and the
    event-noun phrasing rendered into
    :class:`~ibkr_fa.exceptions.MissingFxRateError`.

    Construct without arguments (or with ``max_carry_forward_days``
    to override the inherited default):

    >>> from ibkr_fa.fx import Rule128Policy
    >>> from datetime import date
    >>> from decimal import Decimal
    >>> policy = Rule128Policy()
    >>> policy.POLICY_KIND
    'Rule 128(5)(ii)'
    >>> isinstance(policy, Rule115Policy)
    True

    The event noun in :meth:`_policy_kind_for` says ``"foreign tax
    paid"`` (vs. ``"a dividend"`` in the parent) so the raised
    :class:`MissingFxRateError` reads correctly for an FTC auditor.

    See the module docstring for the statute citation rationale and
    why this is preceding-month-end (NOT preceding-day, per the
    sub-issue #34 text correction).
    """

    POLICY_KIND: ClassVar[str] = _ay_2026_27.POLICY_KIND_RULE_128_5II
    _CITATION_ATTR: ClassVar[str] = "POLICY_KIND_RULE_128_5II"

    def _policy_kind_for(self) -> str:
        return f"{self.policy_kind_str()} (preceding-month-end for foreign tax paid)"
