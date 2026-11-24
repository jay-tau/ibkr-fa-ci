"""Per-head statutory FX rule choice for one financial year (#610).

Which FX rule converts each head of income is a tax decision, so it is made
here once, not in every pipeline. :meth:`TaxFxContext.for_fy` binds a financial
year and the ``[fx]`` config. Each head's policy is built on first read, since
policy constructors load the TTBR table, and the same object is returned after
that. Schedule FA is calendar-year keyed and keeps
:class:`~ibkr_fa.fx.policy_fa.FaExactDatePolicy` (``docs/portal/fx-policy-choice.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING

from ibkr_fa.fx.policy_rule115 import Rule115OtherSourcesPolicy, Rule115Policy
from ibkr_fa.fx.policy_rule115_capital_gains import Rule115CapitalGainsPolicy
from ibkr_fa.fx.policy_rule128 import Rule128Policy
from ibkr_fa.models.tax_year import FIRST_AY_REQUIRING_2025_ACT_FRAMEWORK

if TYPE_CHECKING:
    from ibkr_fa.config import FxConfig
    from ibkr_fa.models.tax_year import FinancialYear


# The step the NetBenefits, StockPlan and Robinhood warnings that the FX policy isn't
# verified for the assessment year end with (#126, #1470).
CROSS_AY_FX_WARNING_STEP = (
    "pass --suppress-cross-ay-warning once you have independently verified it (see #126)."
)


# Not ``slots=True``: ``cached_property`` stores each policy in the instance ``__dict__``.
@dataclass(frozen=True)
class TaxFxContext:
    """The FX policy for each FY-keyed head of income, plus the AY-framework verdict."""

    financial_year: FinancialYear
    fx_config: FxConfig
    suppress_cross_ay_warning: bool = False

    @classmethod
    def for_fy(
        cls,
        fy: FinancialYear,
        fx_cfg: FxConfig,
        *,
        suppress_cross_ay_warning: bool = False,
    ) -> TaxFxContext:
        """Bind ``fy`` and ``fx_cfg``. No policy is built and no TTBR is loaded yet."""
        return cls(fy, fx_cfg, suppress_cross_ay_warning)

    @property
    def fx_policy_appropriate_for_ay(self) -> bool:
        """Whether the 1961-Act FX framework is verified for this FY's AY.

        ``True`` for AY <= 2026-27: Section 536's transitional saving
        clause keeps Rule 115(1), Explanation (2)(e) of the IT Rules, 1962
        in force. ``False`` for AY >= 2027-28 unless
        ``suppress_cross_ay_warning`` is set: the 2025-Act framework keeps
        the dividend preceding-month-end rule verbatim (Rule 206(2)(b)(i)
        Sl. No. 5), but the carry-back across non-publication days still
        rests on CBDT FAQ / practice pending primary-source verification
        (#126). The CLI shows ``False`` as a warning; the run still proceeds.
        """
        return (
            self.financial_year.assessment_year_start < FIRST_AY_REQUIRING_2025_ACT_FRAMEWORK
            or self.suppress_cross_ay_warning
        )

    @cached_property
    def dividend(self) -> Rule115Policy:
        """Dividends: Rule 115(1), Explanation (2)(e), preceding-month-end TTBR."""
        return Rule115Policy(
            max_carry_forward_days=self.fx_config.max_carry_forward_days,
            assessment_year_start=self.financial_year.assessment_year_start,
        )

    @cached_property
    def other_sources(self) -> Rule115OtherSourcesPolicy:
        """Interest, other sources and business income: Explanation (2)(c), FY-end TTBR."""
        return Rule115OtherSourcesPolicy(
            previous_year_end=self.financial_year.end_date,
            max_carry_forward_days=self.fx_config.max_carry_forward_days,
            assessment_year_start=self.financial_year.assessment_year_start,
        )

    @cached_property
    def capital_gains(self) -> Rule115CapitalGainsPolicy:
        """Capital gains: Rule 115(1), Explanation (2)(f), preceding-month-end TTBR."""
        return Rule115CapitalGainsPolicy(
            max_carry_forward_days=self.fx_config.max_carry_forward_days,
            assessment_year_start=self.financial_year.assessment_year_start,
        )

    @cached_property
    def wht(self) -> Rule128Policy:
        """Foreign tax credit: Rule 128(5)(ii), preceding-month-end TTBR."""
        return Rule128Policy(
            max_carry_forward_days=self.fx_config.max_carry_forward_days,
            assessment_year_start=self.financial_year.assessment_year_start,
        )
