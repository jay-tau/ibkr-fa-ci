"""Black Money Act, 2015 (BMA) disclosure pre-flight advisory.

The Black Money (Undisclosed Foreign Income and Assets) and Imposition
of Tax Act, 2015 ("BMA"; Act 22 of 2015, in force 1 July 2015) imposes
severe penalties for **non-disclosure** of foreign assets and foreign
income by Indian residents.

For an ``ibkr-fa`` user — i.e., someone who files their ITR but might
inadvertently omit Schedule FA — the directly-applicable sections are:

* **Section 3** ("Charge of tax") — flat **30%** tax on the
  undisclosed foreign income / asset value. No exemption, no slab
  benefit, no deduction.
* **Section 10** prescribes the computation of that tax for an
  assessment year.
* **Section 41** ("Penalty in relation to undisclosed foreign income
  and assets") — a penalty of **three times the tax computed under
  section 10** (i.e., **300% of the §3 tax**, working out to ~90% of
  the undisclosed value) in addition to the tax itself. The combined
  charge therefore comes to ~120% of the undisclosed value when §3
  + §41 both apply.
* **Section 43** ("Penalty for failure to furnish in return of income
  an information or furnish inaccurate particulars about an asset
  (including financial interest in any entity) located outside India")
  — a flat **₹10 lakh** penalty (irrespective of tax payable) for
  filing an ITR but omitting the foreign asset / inaccurately
  disclosing it. **This is the section most directly applicable to
  the ibkr-fa use case** (user filed an ITR but skipped Schedule FA).
  **Finance (No.2) Act, 2024 (effective 1 October 2024) replaced the
  earlier narrow ₹5 lakh foreign-bank-account-only carve-out with a
  broader ₹20 lakh aggregate-value carve-out covering ALL foreign
  assets other than immovable property** — the §43 penalty therefore
  does NOT bite if the aggregate value of all undisclosed foreign
  assets (foreign equity, foreign bank balances, foreign mutual
  funds, etc., excluding foreign immovable property) does not exceed
  **₹20 lakh at any time during the relevant previous year** (the
  statutory test is a peak-balance test against the PY, not a
  year-end snapshot against the AY; per the substituted proviso
  text quoted verbatim by CBDT Instruction F. No. 285/46/2021-IT(Inv.
  V)/88 dated 18 August 2025, ¶2 and ¶5). IBKR-held foreign equities
  count toward this aggregate; a USD-denominated portfolio that
  briefly peaks above ₹20L on intra-year FX or market moves falls
  outside the carve-out even if it ends the PY below the threshold.
* **Section 50** ("Punishment for failure to furnish in return of
  income any information about an asset (including financial interest
  in any entity) located outside India") — rigorous imprisonment of
  **6 months to 7 years** plus a fine, for wilful failure to disclose
  foreign-asset information in a furnished return. Triggers on
  *wilfulness*; an honest mistake doesn't reach §50. **Finance Act,
  2026 (Act 4 of 2026; introduced as Finance Bill, 2026 Clause 144,
  received Presidential assent on 30 March 2026) inserted a matching
  ₹20 lakh aggregate-foreign-asset (other than immovable property)
  proviso to §§49 and 50, deemed to have been inserted with effect
  from 1 October 2024** (the deeming clause is built into the
  proviso text itself and is therefore self-executing on commencement
  — the §§49/50 carve-out now mirrors the §§42/43 carve-out at the
  same threshold and effective date). For the 17-month period
  between 1 October 2024 and Presidential assent, prosecution under
  §§49 and 50 in sub-₹20L cases was already administratively
  foreclosed via **CBDT Instruction F. No. 285/46/2021-IT(Inv. V)/88
  dated 18 August 2025** (issued under §84 BMA r/w §119 IT Act, ¶5),
  which directs that prosecution proceedings under §§49/50 BMA "would
  not be initiated in cases where penalty under section 42 and/or 43
  of the BMA, 2015 is not imposed or imposable" by reason of the
  ₹20 lakh proviso.

Adjacent sections worth knowing (less directly applicable to ibkr-fa
but commonly cited together):

* **Section 42** — same ₹10 lakh penalty as §43, but for failure to
  *furnish the return at all* (vs. furnishing-but-omitting in §43).
  Same ₹20 lakh aggregate-foreign-asset (other than immovable
  property) carve-out introduced by Finance (No.2) Act, 2024 from
  1 October 2024 (replacing the earlier ₹5 lakh foreign-bank-only
  carve-out).
* **Section 49** — same 6 months to 7 years RI as §50, but for wilful
  failure to *furnish the return at all*. Same ₹20 lakh aggregate-
  foreign-asset (other than immovable property) carve-out inserted
  by Finance Act, 2026 with effect from 1 October 2024 (per the
  same deeming clause as §50; see CBDT Instruction dated 18 August
  2025 for the bridging administrative directive between 2024-10-01
  and 2026-03-30).
* **Section 51** — wilful attempt to *evade tax* (vs. wilful failure
  to disclose): rigorous imprisonment of **3 to 10 years** plus fine.
  No ₹20 lakh carve-out (the carve-out applies to non-disclosure,
  not to evasion).

The BMA's disclosure / penalty / prosecution framework hooks on the
return-filing step, NOT on the underlying foreign transaction. Any
Indian resident with a foreign asset (foreign equity, foreign bank
account, foreign mutual fund, foreign insurance policy, etc.) must
disclose it on ITR-2 Schedule FA every assessment year, even if no
income arises and no capital event occurs. The §43 ₹10 lakh penalty
applies even if the user's overall tax position is zero — because the
penalty attaches to the *disclosure failure*, not to tax evaded
(subject to the ₹20 lakh aggregate-foreign-asset carve-out introduced
by Finance (No.2) Act, 2024).

``ibkr-fa`` exists to make the Schedule FA / OS / CG disclosure
mechanically correct, but the **decision to file** is the user's.
This advisory surfaces the BMA penalty exposure on each run of the
subcommands listed below so the user cannot inadvertently treat the
tool's output as optional or defer the filing without acknowledging
the cost of inaction.

Printed by the IBKR subcommands (schedule-fa, schedule-os,
schedule-cg, schedule-cg-ltcg, schedule-fsi, schedule-tr, form-67 and
run-all), merge and the robinhood-* subcommands; the NetBenefits,
Fidelity CMA, StockPlan and init subcommands don't print it. It goes
to stderr for schedule-os, schedule-cg, schedule-cg-ltcg,
schedule-fsi, schedule-tr, form-67 and run-all, and to stdout as part
of the run report for schedule-fa, merge and robinhood-*.
Non-blocking — does not affect the exit code.
Cannot be silenced via ``--quiet``: tax-safety advisories are
non-quietable by design, matching the existing §111A / §112A
advisory pattern from PR #176.

RNOR (resident-but-not-ordinarily-resident) assessees are
carved-out: BMA §2 defines "assessee" as a person "being a resident
other than not ordinarily resident in India within the meaning of
clause (6) of section 6 of the Income-tax Act", so non-RNOR
residents (the default for someone living in India ≥ 2 years) are
in scope. Non-residents (NRIs, foreigners) are NOT in scope for
*current-year* obligations — **but** the 2019 retrospective
amendment (Finance Act, 2019) extends BMA to non-residents who
**were resident** at the time the foreign asset was acquired or
the foreign income was earned. An NRI who acquired a foreign
asset while still resident in India remains in scope for that
historical disclosure obligation, even if they have since become
a non-resident.

References:

* Black Money (Undisclosed Foreign Income and Assets) and
  Imposition of Tax Act, 2015 — Act 22 of 2015, in force
  1 July 2015. Bare statute text: search for the title on
  https://www.indiacode.nic.in/ or via the CBDT downloads page.
* en.wikipedia.org page on the Act (well-cited; cross-reference
  for the section numbers and the 2019 retrospective amendment).
* GitHub issue #63 — design intent and scope.
"""

from __future__ import annotations

from typing import Final

#: Loud BMA disclosure advisory printed by the IBKR subcommands,
#: ``merge`` and the ``robinhood-*`` subcommands (not by the
#: NetBenefits, Fidelity CMA, StockPlan or ``init`` subcommands; see
#: the module docstring). The phrasing is intentionally direct
#: about the penalty amount and imprisonment exposure so a
#: first-time user who underestimates the disclosure obligation
#: sees the real cost of inaction.
#:
#: Placement: appended to the safety-warning block. On the
#: ``schedule-fa`` path it routes to **stdout** as part of
#: :func:`ibkr_fa.cli._render_run_report`; on the ``schedule-os``
#: / ``schedule-cg`` / ``schedule-cg-ltcg`` paths it routes to
#: **stderr** as part of the corresponding
#: ``_render_*_safety_warnings`` block (consistent with the
#: existing safety-warning routing per subcommand).
#:
#: Non-quietable: ``--quiet`` reduces stdout to a single TOTAL
#: line but does NOT suppress the stderr safety-warning block,
#: so the advisory still fires under ``--quiet`` for the three
#: subcommands that route via stderr. The ``schedule-fa`` path's
#: advisory lives on stdout but the FA subcommand has no
#: ``--quiet`` mode (it is required to print the manual-fill
#: warnings every run).
#:
#: Matches the non-quietable design pattern established by
#: :data:`~ibkr_fa.compute.cg_section.LTCG_SECTION_112_VS_112A_ADVISORY`
#: and :data:`~ibkr_fa.compute.cg_section.STCG_SLAB_VS_111A_ADVISORY`
#: from PR #176.
BMA_DISCLOSURE_ADVISORY: Final[str] = (
    "⚠️  **Black Money Act disclosure reminder**: Under the Black Money "
    "(Undisclosed Foreign Income and Assets) and Imposition of Tax "
    "Act, 2015, you (as an Indian resident) MUST disclose every "
    "foreign asset and every foreign income source on ITR-2 each "
    "assessment year. Filing an ITR but omitting the Schedule FA "
    "disclosure can trigger a flat ₹10 lakh penalty under Section 43, "
    "irrespective of the tax position. Since Finance (No.2) Act, 2024 "
    "(effective 1 October 2024), Section 43 has a ₹20 lakh aggregate-"
    "value carve-out covering ALL foreign assets other than immovable "
    "property — the penalty does not apply where the aggregate value "
    "of such assets does not exceed ₹20 lakh at any time during the "
    "relevant previous year (the statutory test is a peak-balance "
    "test against the previous year, not a year-end snapshot). "
    "Wilful non-disclosure additionally carries 6 months to 7 years "
    "rigorous imprisonment under Section 50; Finance Act, 2026 "
    "(Act 4 of 2026; Presidential assent 30 March 2026) inserted a "
    "matching ₹20 lakh aggregate-foreign-asset (other than immovable "
    "property) proviso to Section 50 (and Section 49), deemed to "
    "have been inserted with effect from 1 October 2024 — so the "
    "Section 50 carve-out now mirrors the Section 43 carve-out at "
    "the same threshold and effective date.\n"
    "   Undisclosed foreign income / asset value attracts a flat 30% "
    "tax under Section 3, and a penalty of 300% of that tax (~90% of "
    "the undisclosed value) under Section 41 — for a combined "
    "exposure of ~120% of the undisclosed value, in addition to the "
    "Section 43 fixed penalty and any Section 50 prosecution.\n"
    "   The Schedule FA / OS / CG output ibkr-fa produces is the "
    "*mechanically correct* disclosure; the *decision to file* the "
    "return is yours. Do not defer filing without consulting a CA "
    "familiar with foreign-asset disclosure. RNOR (resident-but-not-"
    "ordinarily-resident) assessees have a Section 2 carve-out; "
    "current Indian residents (the default for someone living in "
    "India ≥ 2 years) are squarely in scope. Note that the 2019 "
    "retrospective amendment (Finance Act, 2019) extends BMA to "
    "non-residents / NRIs in respect of foreign assets acquired "
    "*while they were resident* — consult a CA before assuming "
    "non-resident status fully exempts you.\n"
    "   This advisory fires on every ibkr-fa subcommand run and "
    "cannot be silenced via --quiet (tax-safety diagnostics are "
    "non-quietable by design)."
)
