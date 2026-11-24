"""FX policy citation strings — AY 2026-27 snapshot (1961-Act regime).

The Income-tax Rules, 1962 govern AY 2026-27 (FY 2025-26) and earlier
years' FX conversions for Schedule OS / CG / FSI / TR / Form 67 /
Schedule FA computations. Each constant here is the short, human-
readable citation that appears in
:class:`~ibkr_fa.exceptions.MissingFxRateError` discriminators and in
audit-trail strings rendered by
:meth:`~ibkr_fa.fx.policy_rule115.Rule115Policy._policy_kind_for` (and
its sister overrides on the other policy classes).

The constants are verified verbatim against the gazetted text via
Indian Kanoon (https://indiankanoon.org/doc/34257484/) and the
CBDT-hosted Rules-1962 PDF; see :mod:`ibkr_fa.fx.policy_rule115` and
:mod:`ibkr_fa.fx.policy_rule128` module docstrings for primary-source
links and the structural rationale.

For the parallel AY 2027-28+ snapshot under the Income-tax Rules,
2026 framework (Rule 206 / 207 / 76), see :mod:`._ay_2027_28`.
"""

from __future__ import annotations

from typing import Final

# Rule 115(1), Explanation (2)(e) — foreign dividends. Used by
# :class:`~ibkr_fa.fx.policy_rule115.Rule115Policy` (Schedule OS row 1a).
POLICY_KIND_RULE_115_2E: Final[str] = "Rule 115(1), Explanation (2)(e)"

# Rule 115(1), Explanation (2)(f) — capital-asset transfers. Used by
# :class:`~ibkr_fa.fx.policy_rule115_capital_gains.Rule115CapitalGainsPolicy`
# (Schedule CG STCG/LTCG).
POLICY_KIND_RULE_115_2F: Final[str] = "Rule 115(1), Explanation (2)(f)"

# Rule 115(1), Explanation (2)(c) — "Income from other sources" residual.
# Used by :class:`~ibkr_fa.fx.policy_rule115.Rule115OtherSourcesPolicy`
# (Schedule OS row 1b(ix) "Other Interest"). Distinguished from
# (2)(e) by the FY-end specified-date convention — see the class
# docstring.
POLICY_KIND_RULE_115_EXPLANATION_2C: Final[str] = "Rule 115(1), Explanation (2)(c)"

# Rule 128(5)(ii) — foreign tax paid for Foreign Tax Credit. Used by
# :class:`~ibkr_fa.fx.policy_rule128.Rule128Policy` (Form 67 +
# Schedule TR).
POLICY_KIND_RULE_128_5II: Final[str] = "Rule 128(5)(ii)"
