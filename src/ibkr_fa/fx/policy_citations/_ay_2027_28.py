"""FX policy citation strings — AY 2027-28 snapshot (Income-tax Rules, 2026).

The Income-tax Rules, 2026 (G.S.R. 198(E) dated 20-03-2026, effective
01-04-2026) govern AY 2027-28 (FY 2026-27) and later years' FX
conversions. The 1962 Rules' substantive FX conventions are preserved
verbatim under new numbering:

* **Rule 206(2)(b)(i) Sl. No. 3** absorbs Rule 115(1), Explanation (2)(c)
  "Other Sources" (single FY-end TTBR per assessee — see the
  "Terminology equivalence" subsection of
  ``docs/architecture.md``).
* **Rule 206(2)(b)(i) Sl. No. 5** absorbs Rule 115(1), Explanation (2)(e)
  dividends (per-event preceding-month-end).
* **Rule 206(2)(b)(i) Sl. No. 6** absorbs Rule 115(1), Explanation (2)(f)
  capital gains (per-event preceding-month-end).
* **Rule 76(7)(b)** absorbs Rule 128(5)(ii) (FTC / Form 67 / Schedule
  TR).

Verified against the CBDT-hosted PDF text-extract of the Rules-2026
gazette (Rule 206 starts on PDF p. 155, Rule 76 on PDF p. 56-ish; see
``docs/architecture.md`` "FX policy by AY" section for the verbatim
Rule 206 Table reproduction and the issue #207 / PR #253 + issue #252
/ PR #255 closure trail).

Carry-back caveat: Rule 206 has NO gazetted carry-back-to-most-recent-
prior-published-TTBR rule (only Rule 207(1) for the TDS / non-resident /
IFSC path). The implementation retains the carry-back behaviour as
historical CBDT-FAQ practice carry-over, but the CLI emits an explicit
"verify against published CBDT FAQ" advisory under AY 2027-28+ runs so
filers are not silently exposed.

Rule 115(1), Explanation (2)(c) "Other Sources" residual mapping
(corrected by issue #252 / PR #255 — the AY 2027-28 snapshot from
PR #253 initially mis-cited Sl. No. 5 with a "behaviour change pending"
legacy marker). The correct mapping is **Rule 206(2)(b)(i) Sl. No. 3**,
which Table-verbatim preserves the FY-end ("last day of the tax year
of the assessee") convention. ``Rule115OtherSourcesPolicy``'s existing
FY-end logic is therefore correct under both AY 2026-27
(Rule 115(1), Explanation (2)(c)) and AY 2027-28+
(Rule 206(2)(b)(i) Sl. No. 3) — no behavioural change is needed,
just the citation refresh.
"""

from __future__ import annotations

from typing import Final

# Rule 206(2)(b)(i) Sl. No. 5 — foreign dividends (verbatim absorption
# of Rule 115(1), Explanation (2)(e)). Used by
# :class:`~ibkr_fa.fx.policy_rule115.Rule115Policy`.
POLICY_KIND_RULE_115_2E: Final[str] = "Rule 206(2)(b)(i) Sl. No. 5"

# Rule 206(2)(b)(i) Sl. No. 6 — capital-asset transfers (verbatim
# absorption of Rule 115(1), Explanation (2)(f)). Used by
# :class:`~ibkr_fa.fx.policy_rule115_capital_gains.Rule115CapitalGainsPolicy`.
POLICY_KIND_RULE_115_2F: Final[str] = "Rule 206(2)(b)(i) Sl. No. 6"

# Rule 206(2)(b)(i) Sl. No. 3 — "Income from other sources (excluding
# dividends and interest on securities)" residual. Verbatim FY-end
# behaviour ("last day of the tax year of the assessee" — terminology
# equivalent to the 1961-Act "previous year of the assessee"). Used by
# :class:`~ibkr_fa.fx.policy_rule115.Rule115OtherSourcesPolicy` for
# IBKR broker credit-interest (Schedule OS row 1b(ix) "Other
# Interest"). No behavioural change vs. the AY 2026-27 mapping
# (Rule 115(1), Explanation (2)(c)) — the policy's existing FY-end logic
# applies verbatim under both regimes. Issue #252 / PR #255 corrects
# PR #253's initial mis-citation that pointed at Sl. No. 5.
POLICY_KIND_RULE_115_EXPLANATION_2C: Final[str] = "Rule 206(2)(b)(i) Sl. No. 3"

# Rule 76(7)(b) — foreign tax paid for Foreign Tax Credit (verbatim
# absorption of Rule 128(5)(ii)). Used by
# :class:`~ibkr_fa.fx.policy_rule128.Rule128Policy`.
POLICY_KIND_RULE_128_5II: Final[str] = "Rule 76(7)(b)"
