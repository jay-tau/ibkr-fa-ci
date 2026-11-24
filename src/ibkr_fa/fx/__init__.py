"""Foreign-exchange (FX) conversion for ibkr-fa.

Different Indian-tax schedules use different FX conventions:

- **Schedule FA** (foreign asset disclosure): TTBR on the **exact event
  date** (acquisition, peak, Dec 31), with carry-forward to the most
  recent prior published rate when SBI did not publish on that day.
  Implemented by :class:`ibkr_fa.fx.policy_fa.FaExactDatePolicy`.

- **Schedules CG / OS** (dividend / capital-gains income computation):
  Rule 115(1), Explanation (2)(e) / Rule 115(1), Explanation
  (2)(f) — TTBR of the **last day of the month immediately
  preceding** the receipt / transaction date.
  Implemented by :class:`ibkr_fa.fx.policy_rule115.Rule115Policy`
  (Schedule OS 1a dividends) and
  :class:`ibkr_fa.fx.policy_rule115_capital_gains.Rule115CapitalGainsPolicy`
  (Schedule CG STCG/LTCG).

- **Schedule OS 1b(ix) interest and 1e residual income**:
  Rule 115(1), Explanation **(2)(c)** — TTBR of the **last day of
  the assessee's previous year** (i.e., the FY-end date for an
  Indian individual taxpayer). One fixed FX date for every
  applicable receipt in the FY, not per-event. Implemented by
  :class:`ibkr_fa.fx.policy_rule115.Rule115OtherSourcesPolicy`.

- **Form 67 / Schedule TR** (foreign tax credit): Rule 128(5)(ii) —
  TTBR of the **last day of the month immediately preceding** the
  month in which the foreign tax was paid or deducted. Structurally
  identical to Rule 115(1), Explanation (2)(e), but with a different
  statute citation and event noun. Implemented by
  :class:`ibkr_fa.fx.policy_rule128.Rule128Policy`. **NB:** the older
  package docstring (and the sub-issue #34 text) described Rule 128
  as "TTBR of the day immediately preceding" — that's wrong (likely
  confused with Rule 26, which is TDS-on-payments-to-non-residents
  and out of scope for ibkr-fa). The Rule 128 implementation here
  matches the authoritative Indian Kanoon text verified June 2026.

For the FY-keyed schedules, :class:`ibkr_fa.fx.context.TaxFxContext`
decides once which of these policies converts each head of income. Compute
code takes the rate, and for dated lines the specified date, from the policy
it is handed. Schedule FA is calendar-year keyed and builds its
:class:`FaExactDatePolicy` directly.
"""

from __future__ import annotations

from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.fx.policy import AuditedFxPolicy, FxPolicy
from ibkr_fa.fx.policy_fa import DEFAULT_MAX_CARRY_FORWARD_DAYS, FaExactDatePolicy
from ibkr_fa.fx.policy_rule115 import (
    Rule115OtherSourcesPolicy,
    Rule115Policy,
    Rule115Resolution,
)
from ibkr_fa.fx.policy_rule128 import Rule128Policy
from ibkr_fa.fx.ttbr_loader import load_ttbr

__all__ = [
    "DEFAULT_MAX_CARRY_FORWARD_DAYS",
    "AuditedFxPolicy",
    "FaExactDatePolicy",
    "FxPolicy",
    "Rule115OtherSourcesPolicy",
    "Rule115Policy",
    "Rule115Resolution",
    "Rule128Policy",
    "TaxFxContext",
    "load_ttbr",
]
