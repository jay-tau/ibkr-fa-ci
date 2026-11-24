"""ITR form selection guidance for foreign-asset / foreign-income holders.

An Indian taxpayer files exactly one ITR form per assessment year,
chosen by income shape. For someone whose income includes foreign
holdings (the ``ibkr-fa`` user), the relevant choices narrow to:

* **ITR-1 (Sahaj)** — **NEVER applicable** to an ibkr-fa user.
  The ITR-1 form header itself bears the exclusion verbatim:
  *"Not for an individual who … has assets (including financial
  interest in any entity) located outside India"*. A user with
  even a single foreign-equity holding cannot use ITR-1,
  regardless of total income.
* **ITR-4 (Sugam)** — also **NEVER applicable** to an ibkr-fa
  user. ITR-4 is the presumptive-taxation form for
  small-business / professional filers under §§44AD/44ADA/44AE;
  it carries the same foreign-asset exclusion as ITR-1. A
  presumptive-scheme filer who also holds foreign assets falls
  back to ITR-3, not ITR-4.
* **ITR-2** — the **default for ibkr-fa users**. Applies to
  individuals and HUFs whose income includes salary, house
  property, capital gains, *foreign assets / foreign income*,
  and other sources, but who have **no income from business or
  profession**.
* **ITR-3** — required when ITR-2's income shape is present **plus**
  income from business or profession (intraday equity trading,
  F&O / derivatives trading, freelance / consulting income,
  professional fees, partnership-firm distributive share, etc.).
  Holders of foreign assets who also run a business or trade
  intraday cannot use ITR-2; they must use ITR-3.

CBDT treats intraday equity trading (delivery same day, no
delivery actually taken) as "speculative business" under
Section 43(5) of the Income-tax Act. Proviso (d) excludes only
eligible derivatives carried out on a prescribed/notified
recognised stock exchange from that speculative definition.
Foreign-exchange derivatives do not automatically qualify for
that carve-out. Any business treatment still pushes the assessee
out of ITR-2 territory and into ITR-3.
Long-term and short-term capital gains from delivery-based
equity (the ``ibkr-fa`` use case) are not business income and
remain in ITR-2 territory.

``ibkr-fa`` cannot determine from its inputs alone whether a user
has business / profession income elsewhere (intraday / F&O /
freelance / consulting), so the guidance is necessarily
conditional: "file ITR-2 if your only income is salary + foreign
equity capital gains + foreign dividends; switch to ITR-3 if you
also have business or profession income". The user is the final
authority on their full income shape.

Printed by the IBKR subcommands (schedule-fa, schedule-os,
schedule-cg, schedule-cg-ltcg, schedule-fsi, schedule-tr, form-67 and
run-all) and the robinhood-* subcommands; merge and the NetBenefits,
Fidelity CMA, StockPlan and init subcommands don't print it. It goes
to stderr for schedule-os, schedule-cg, schedule-cg-ltcg,
schedule-fsi, schedule-tr, form-67 and run-all, and to stdout as part
of the run report for schedule-fa and robinhood-*. Non-blocking —
does not affect the exit code. Cannot be silenced via ``--quiet``:
tax-safety advisories are non-quietable by design, matching the
existing §111A / §112A / BMA advisory pattern from PRs #176 and #178.

References:

* CBDT ITR-1 (Sahaj) form header — bears the foreign-asset
  exclusion verbatim on the form face (year-stable wording
  verified for AY 2025-26 via the e-filing portal). The same
  exclusion appears on ITR-4 (Sugam).
* Income-tax Act, 1961 §43(5) — definition of "speculative
  transaction" with the proviso (d) carve-out for eligible
  derivatives on a recognised stock exchange.
* Income-tax Act, 1961 §139 — return-filing obligation that
  hooks the ITR-form choice.
* CBDT ITR Forms FAQ — annually-updated FAQ on form selection
  (https://incometaxindia.gov.in/Pages/i-t-return-forms.aspx).
* GitHub issue #64 — design intent and scope.
"""

from __future__ import annotations

from typing import Final, Literal

ItrForm = Literal["ITR-2", "ITR-3"]
ITR_FORMS: Final[tuple[ItrForm, ...]] = ("ITR-2", "ITR-3")

#: Loud ITR-form selection guidance printed by the IBKR subcommands
#: and the ``robinhood-*`` subcommands (not by ``merge`` or the
#: NetBenefits, Fidelity CMA, StockPlan or ``init`` subcommands; see
#: the module docstring). The phrasing is deliberately
#: conditional on the user's broader income shape because
#: ``ibkr-fa`` cannot determine business / profession income
#: from its IBKR-only inputs.
#:
#: Placement: appended to the safety-warning block after the BMA
#: disclosure advisory (compliance advisories cluster at the end
#: of the warning block). On the ``schedule-fa`` path it routes
#: to **stdout** via :func:`ibkr_fa.cli._render_run_report`; on
#: the ``schedule-os`` / ``schedule-cg`` / ``schedule-cg-ltcg``
#: paths it routes to **stderr** via the corresponding
#: ``_render_*_safety_warnings`` block.
#:
#: Non-quietable: same design as
#: :data:`~ibkr_fa.compliance.black_money_act.BMA_DISCLOSURE_ADVISORY` —
#: tax-safety diagnostics survive ``--quiet``.
ITR_FORM_SELECTION_ADVISORY: Final[str] = (
    "ℹ️  **ITR form selection**: Foreign-asset / foreign-income "  # noqa: RUF001
    "holders cannot use ITR-1 (Sahaj) or ITR-4 (Sugam) — both form "
    "headers explicitly exclude anyone with assets located outside "
    "India. The two applicable forms are:\n"
    "   * **ITR-2** — default for ibkr-fa users. Use if your "
    "income consists of salary + house property + capital gains + "
    "foreign assets / income + other sources, with NO business "
    "or profession income.\n"
    "   * **ITR-3** — required if you ALSO have business or "
    "profession income (intraday equity trading, F&O / "
    "derivatives, freelance / consulting fees, professional "
    "income, partnership-firm distributive share, ...). A "
    "presumptive-scheme filer under §44AD/44ADA/44AE who would "
    "otherwise use ITR-4 also falls back to ITR-3 once they hold "
    "foreign assets.\n"
    "   Long-term and short-term capital gains from delivery-"
    "based equity (the ibkr-fa use case) are *typically* NOT "
    "business income and remain in ITR-2 territory when treated "
    "as capital assets / investments — but CBDT Circular 6/2016 "
    "recognises that listed shares can ALSO be held as "
    "stock-in-trade, in which case the gains become business "
    "income and ITR-3 applies. The fact-specific tests (frequency, "
    "intent, treatment in books) outside the >12-month safe "
    "harbor put the burden on the assessee; if you actively trade "
    "your foreign equity as a business, treat the income as "
    "business income and use ITR-3. Intraday equity trades are "
    "speculative transactions under Section 43(5), generally "
    "reported as speculative business income. Section 43(5) proviso (d) excludes "
    "eligible derivatives on a prescribed/notified recognised stock "
    "exchange from that speculative definition; foreign-exchange "
    "derivatives do not automatically qualify. Either business "
    "classification pushes you to ITR-3. ibkr-fa "
    "cannot tell from your IBKR statement alone whether you have "
    "business income elsewhere; this is your call. When in doubt, "
    "consult a CA."
)
