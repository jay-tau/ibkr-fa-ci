"""CLI advisory constants for Schedule CG output (#62).

The CLI (:mod:`ibkr_fa.cli`) prints the matching advisory below
whenever any LTCG / STCG row is written.

The long-term / short-term split is decided upstream by
:func:`~ibkr_fa.models.schedule_cg_row.is_short_term_holding`, which
applies one fixed threshold of 24 calendar months
(:data:`~ibkr_fa.models.schedule_cg_row.STCG_HOLDING_THRESHOLD_MONTHS`)
to every lot. On the IBKR path the parsers refuse Indian rows before
they reach Schedule CG: the Activity Statement trades parser refuses
IN-prefix ISINs, and the Flex closed-lots and open-positions parsers
refuse rows whose ``IssuerCountryCode`` is ``IN``.
"""

from __future__ import annotations

from typing import Final

#: Loud user-facing advisory surfaced on stderr at every
#: ``schedule-cg-ltcg`` run that writes ≥ 1 row. Spells out
#: the §112 vs §112A distinction in concrete portal-cell terms
#: so a user filling in the ITR-2 utility manually does not
#: confuse the two and accidentally claim the §112A ₹1.25 lakh
#: exemption against a §112 liability. Closes #62.
LTCG_SECTION_112_VS_112A_ADVISORY: Final[str] = (
    "⚠️  Schedule CG LTCG output is taxed under **Section 112** of the "
    "Income-tax Act, 1961 (12.5% flat without indexation post-"
    "2024-07-23, NO ₹1.25 lakh exemption). It is NOT Section 112A.\n"
    "   Section 112A is reserved for LTCG on Indian listed equity "
    "(STT-paid) only; the ₹1.25 lakh exemption applies ONLY to that "
    "section. Resident foreign shares and fund units handled here fall "
    "under the general Section 112.\n"
    "   On the ITR-2 utility: paste this run's TOTAL into "
    "**Schedule CG / Section B / Item 8(e), B8e** (LTCG from assets "
    "where B1 to B7 do not apply). Do NOT enter it on the "
    "Section 112A row of Schedule CG (the row that captures LTCG on "
    "STT-paid Indian listed equity). Entering it in the §112A row "
    "would silently apply the ₹1.25 lakh exemption to a liability "
    "where it does not apply (under-payment is discoverable on "
    "AIS/26AS reconciliation)."
)

#: Companion advisory for the STCG path (foreign STCG = slab-rate
#: under the regular Chapter IV-D provisions, NOT Section 111A).
#: Surfaced by the ``schedule-cg`` subcommand on every run that
#: writes ≥ 1 row.
#:
#: The §111A rate is *intentionally* qualified by both regimes.
#: §111A was 15% for transfers before 2024-07-23 and 20% on or
#: after (per Finance (No. 2) Act, 2024). The ``schedule-cg``
#: STCG pipeline does NOT filter by the cutover date (foreign
#: STCG is slab-rate in both regimes — the FA(No.2)-2024 rate
#: change is §111A-only), so the same advisory may fire on pre-
#: and post-cutover runs. Naming a single §111A rate would
#: mislead one of those audiences (T5-r1 finding).
#:
#: Note this advisory does NOT call foreign STCG "Section 111" —
#: §111 of the Income-tax Act, 1961 is "Tax on accumulated
#: balance of recognised provident fund", an unrelated provision.
#: Foreign equity STCG simply has no special-rate section and
#: is taxed at the assessee's marginal slab rate per the
#: regular Chapter IV-D provisions (T5-r2 finding).
STCG_SLAB_VS_111A_ADVISORY: Final[str] = (
    "⚠️  Schedule CG STCG output is taxed at the assessee's **slab "
    "rate** under the regular Chapter IV-D provisions of the "
    "Income-tax Act, 1961 — there is NO special-rate section for "
    "this path. It is NOT Section 111A.\n"
    "   Section 111A is the flat-rate regime reserved for STCG on "
    "Indian listed equity (STT-paid) only (the §111A rate is "
    "15% for pre-2024-07-23 transfers and 20% on or after, per "
    "Finance (No. 2) Act, 2024 — both differ from the slab-rate "
    "path). Foreign equity STCG (every row this subcommand emits) "
    "is taxed at the assessee's marginal slab rate, with no "
    "special-rate section code under the Act.\n"
    "   On the ITR-2 utility: paste this run's TOTAL into "
    "**Schedule CG / Section A / Item 5** (STCG on assets other than "
    "at A1, A2, A3 or A4 — taxed at slab rate). Do NOT enter it on the "
    "Section 111A row of Schedule CG (the row that captures STCG on "
    "STT-paid Indian listed equity at the §111A flat rate). The "
    "slab-rate vs §111A-flat regime delta can be either an under- "
    "or over-payment depending on the assessee's marginal slab."
)
