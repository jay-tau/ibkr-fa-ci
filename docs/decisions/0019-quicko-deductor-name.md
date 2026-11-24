# ADR 0019: Whose name fills Quicko's "Name of the deductor"

- **Status**: Accepted (2026-11-04)
- **Date**: 2026-11-04
- **Issue**: #864
- **Related**: #429, #645, #865, #870, #871, #1327, ADR-0003, ADR-0004
- **Amended**: 2026-11-04 (#870) to accept owner option 3, a setting for
  the withholding agent's name; 2026-11-13 (#1327) so that a missing setting
  leaves the value blank for the filer instead of refusing the run

## Context

The Schedule TR filing worksheet (`schedule-tr.csv`) ends with a Quicko
block: one form per Form 67 entry that has an exact Indian tax and a §90 or
§91 credit (`output/quicko_foreign_tax.py:66-80`). Each form has a "Name of
the deductor" row. This record asks whose name belongs there: the custodian
that holds the account, or the withholding agent named on Form 1042-S (box
12d).

Code references are at `b9965fa`. Web sources were retrieved on 2026-11-04
IST (2026-11-03 UTC).

### What the tool writes today

- Only NetBenefits writes the block. `pipeline_netbenefits_income.py:231-241`
  passes a `QuickoForeignTaxInput` whose `deductor_name` is the NetBenefits
  account identity's institution name, i.e.
  `[netbenefits.account].institution_name`. `docs/portal/schedule-tr.md:68-74`
  documents this. `output/quicko_foreign_tax.py:31-32` calls it "the source's
  custodial institution name", and `:128` writes it.
- The same setting is the Schedule FA Table A2 custodian
  (`config.py:946-957`, `:975-981`; `pipeline_netbenefits_fa.py:397`). Its
  example value is `"Fidelity Stock Plan Services LLC"` (`config.py:106`,
  `config_template.py:271`).
- The name first appeared in #429 (`87f922c`) as a hard-coded
  `_QUICKO_DEDUCTOR = "Fidelity Stock Plan Services LLC"`. #645 (#861) moved
  it to the configured setting.
- NetBenefits reads no Form 1042-S. It only takes a user-supplied 1042-S
  withholding total to reconcile against (`pipeline_netbenefits_income.py:72`,
  `:92`).

### What Quicko means by the field

Quicko's help article ["Claim Tax Relief on Foreign Income"][quicko-ftc]
(the page shows "Updated" 09 Apr 2026) gives the path "File > Tax Credits",
"Add Tax Credits", "Add Manually", "Taxes withheld outside India", then lists
the fields:

> Country: The country where the income was earned
> Nature of Income: Type of income earned abroad
> Description: Name of the withholder
> Credit Amount: Total foreign income on which tax was deducted
> Tax Withheld: Amount of tax deducted abroad
> TIN: Tax Identification Number issued by the foreign country
> Tax Relief Section and Article: These fields will auto-fill based on the
> information provided

So Quicko asks for the name of whoever withheld the tax. The article calls
the field "Description". The tool's label, "Name of the deductor", comes
from #429, which said the block "mirrors the live form". No public source
shows the live label. Both wordings name the party that deducted the tax,
not the party that holds the account.

**Does Quicko file the field anywhere?** Public sources don't show it.
Quicko's article doesn't say. Its ["How to file Form 67"][quicko-form67]
article (updated 27 Oct 2026) says Form 67 is filed on the Income Tax
portal, before the ITR, so the Quicko form is not Form 67. The next section
shows that none of the named ITD forms has a field for the name.

### Matching fields in the ITD forms

- **Form 67.** The [CBDT Form No. 67][form67] Part A asks for the assessee's
  name, PAN or Aadhaar, address and assessment year (items 1-4). Item 5 has
  columns (1)-(13): serial number, country, source of income, income, tax
  paid outside India (amount and rate), tax payable under normal provisions
  and under section 115JB/JC, credit under section 90/90A (article, rate,
  amount), credit under section 91, and total credit. Part B asks about
  refunds and disputed tax. No column names the deductor. The portal's
  15-column upload template has no such column either
  (`FORM67_UPLOAD_HEADERS` in `output/schedules/form67.py`, from the template
  recorded in #417).
- **Rule 128.** [Rule 128(8)(ii)][rule128] asks for a certificate or
  statement of the tax deducted "(a) from the tax authority of the country or
  specified territory outside India; or (b) from the person responsible for
  deduction of such tax; or (c) signed by the assessee". The person
  responsible for deduction appears only as the issuer of that attachment,
  not as a field.
- **ITR-2.** The notified form ([G.S.R. 227(E)][itr2-form], PDF pp. 26-27)
  gives Schedule FSI the columns country code, taxpayer identification
  number, head of income, income from outside India, tax paid outside India,
  tax payable in India, tax relief and DTAA article. Schedule TR has country
  code, taxpayer identification number, total taxes paid, total tax relief
  and section. The AY 2026-27 [schema][itr2-schema] matches: `ScheduleFSI`
  (`:10023`), `ScheduleFSIDtls` (`:10036`), `ScheduleTR1` (`:10088`),
  `ScheduleTR` (`:10155`) and `ScheduleFSIIncType` (`:14273`) all set
  `additionalProperties: false`, and none has a name property.
- **ITR-3.** The notified form ([G.S.R. 228(E)][itr3-form], PDF pp. 51-52)
  has the same Schedule FSI and TR columns, plus the "Business or
  Profession" head. The [ITR-3 schema V1.1][itr3-schema] defines the same
  five objects, with `additionalProperties: false` and no name property.

None of these forms has a field for the deductor's name. A schema-valid ITR
JSON cannot carry one in Schedule FSI or TR.

### What box 12d names

The IRS [2025 Instructions for Form 1042-S][i1042s-2025] (dated Dec 4,
2024):

- **Who the withholding agent is** (PDF p. 5): "any person, U.S. or
  foreign, that has control, receipt, or custody of an amount subject to
  withholding under chapter 3 who can disburse or make payments of an amount
  subject to withholding, or who makes a withholdable payment under chapter
  4." "In most cases, the U.S. person who pays (or causes to be paid) the
  item of U.S. source income to a foreign person (or to its agent) must
  withhold. However, other persons may be required to withhold." "A person
  may be a withholding agent even if there is no requirement to withhold
  from a payment or if another person has already withheld the required
  amount from a payment."
- **Box 12d** (PDF p. 29): "Enter your name and address in the appropriate
  boxes." Box 12d names the withholding agent that files that form.
- **Who actually withheld** (PDF p. 28): box 7a is "the total amount of U.S.
  federal tax you actually withheld". Box 8: "If you are a withholding agent
  filing a Form 1042-S to report income that has already been subject to
  withholding by another withholding agent, enter the amount actually
  withheld by the other agent(s) in box 8." Boxes 14a and 14b (PDF
  pp. 32-33) then give "the name and EIN of the withholding agent that
  withheld the tax. If multiple withholding agents withheld amounts reported
  on the same Form 1042-S, report the name of any one of the withholding
  agents that withheld amounts."
- **Payer** (PDF p. 33): boxes 16a-16e give the payer's name "if different
  from the withholding agent shown in boxes 12a, 12d, and 12e", for example
  when "a transfer agent or a paying agent" acts "as a withholding agent on
  behalf of a payer".
- **Custodian** (PDF p. 22, Example 17): "The interest is paid by a U.S.
  withholding agent (WA) as custodian of the securities for NQI."

The instructions define the role by function, not by who holds the account.
A custodian can be the withholding agent (Example 17), but nothing makes it
one by default. The payer, the agent that files the form (box 12d) and the
agent that withheld (box 14a) can be different persons. So the person who
deducted box 7a tax is the box 12d agent. Box 8 tax was deducted by another
agent, and box 14a names that agent or, when several withheld, only one of
them. Box 14a alone doesn't show that one person withheld all the box 8 tax.

### Which entity issues the NetBenefits 1042-S

Public sources don't settle it.

- Fidelity's [stock plan tax FAQ][fidelity-faq] says dividends "and any
  taxes withheld will be reported on IRS Form 1042-S for the applicable tax
  year. Log in to Fidelity NetBenefits to access this form online." It names
  no issuer. The same page says "Fidelity Stock Plan Services, LLC, provides
  recordkeeping and/or administrative services to your company's equity
  compensation plan" and lists "Fidelity Brokerage Services LLC, Member
  NYSE, SIPC".
- A [Fidelity Stock Plan Services LLC Participant Agreement][fidelity-pa]
  (PDF metadata April 2013; for one company's Participant Trust plan) says
  the administrator "does not maintain an account for me, does not hold
  assets for me". It names "National Financial Services LLC and Fidelity
  Brokerage Services LLC" among the agents. It is old and plan-specific. It
  shows only that Fidelity splits these roles across entities.
- For the Fidelity CMA, the 1042-S parser requires box 12d to read
  `NATIONAL FINANCIAL SERVICES LLC` (`parser/fidelity_cma_1042s.py:271-273`),
  and ADR-0004 names that firm as the CMA's A2 custodian. That is a
  different account. It suggests, but doesn't prove, the NetBenefits form's
  agent.

**Unproven from public sources:** the name in box 12d of the NetBenefits
1042-S, and whether that form uses box 8 and box 14a. No public source shows
Fidelity Stock Plan Services LLC as the withholding agent. That was the
public-research finding. The owner's decision below rests on the form
instead: a NetBenefits Form 1042-S names its withholding agent in box 12d,
and that agent need not be the configured `institution_name`. Whether the
NetBenefits 1042-S uses box 8 and box 14a stays unproven.

### One Quicko form, several 1042-S forms

Each Quicko form comes from one Form 67 entry: one country and one source of
income, summed over an Indian financial year (`compute/form67.py:20-22`).
Every withholding agent "must file an information return on Form 1042-S to
report amounts paid during the preceding calendar year" ([2025
Instructions][i1042s-2025], PDF p. 3). So one Quicko form's tax can come
from two 1042-S forms, and each can name a different agent. Quicko's article doesn't say what to enter when more than one person
withheld the tax in one form. **Unproven.**

## Decision

**Owner decision (2026-11-04, #870): option 3.** The owner chose option 3,
"Add a setting for the withholder's name". The requirement: a NetBenefits
Form 1042-S names its withholding agent in box 12d, and that agent can be a
different entity from the documented `institution_name` example, Fidelity
Stock Plan Services LLC. The setting's example is a public entity, National
Financial Services LLC (EIN 04-3523567). For NetBenefits, this
replaces the "Fill only from evidence" requirement and "No new setting is
added" below. The "blank, with a notice" outcome stays only for a missing
setting (#1327):

- Quicko's "Name of the deductor" is
  `[netbenefits.account].withholding_agent_name`. The filer copies it from
  their Form 1042-S: the box 12d withholding agent for tax in box 7a, or the
  agent box 14a names for tax in box 8. `institution_name` stays the Table
  A2 institution and no longer feeds the Quicko block.
- The setting has no default. In an otherwise successful run that writes a
  Quicko form without it, each form's "Name of the deductor" value is blank
  and every file is still written, as with the setting. `netbenefits-income`
  prints one fixed notice naming the setting and exits 3, as for a blank
  TIN. `netbenefits-all` shows its income leg as `manual-fill`. Other
  refusals still apply. A run that writes no Quicko form doesn't need the
  setting and prints no notice.

**Owner decision (2026-11-12, #1327): "Blank + fill by hand".** Until
#1327, a missing setting refused the run before any write when it would
write at least one Quicko form, and `netbenefits-all` refused before its
first leg. That stopped the Form 67 upload and the Schedule OS, FSI and TR
filing files over a value none of their portal fields carries. The owner
chose to leave the value blank, write every file, and exit 3 so the filer
fills it in.

The rule below and the "Other sources" paragraph stand. Passages marked
"Earlier recommendation" record what this record proposed before the
owner's decision.

**Rule.** Quicko's "Name of the deductor" names the person that withheld the
tax in that Quicko form. It is not the custodian, and not
`[netbenefits.account].institution_name`. On a Form 1042-S, that person is
the box 12d withholding agent for tax in box 7a. For tax in box 8 it is
another agent; box 14a names that agent, or one of several.

**Fill only from evidence.** *Earlier recommendation; for NetBenefits, the
owner's decision above replaces it.* The tool fills the field only when
parsed withholding evidence shows that one named person withheld all the
tax in that Quicko form. A configured string is not that evidence, and
neither is box 14a alone. Otherwise the field stays blank, and the run
tells the filer to copy the name from their Form 1042-S.
This matches the blank TIN, which the run already announces
(`pipeline_netbenefits_income.py:264-269`).

**NetBenefits.** *Earlier recommendation, superseded by the owner's
decision above.* NetBenefits reads no 1042-S, so its field stays blank, with
a notice. `[netbenefits.account].institution_name` stays the A2 custodian
setting and stops feeding the Quicko block. No new setting is added.

**Other sources.** A later Quicko block for IBKR, StockPlan, Fidelity CMA or
Robinhood (#645 decision 3) follows the same rule. The CMA and Robinhood
parsers already read box 12d (`parser/fidelity_cma_1042s.py:271-273`,
`parser/robinhood_1042s.py:109-120`). Whether their evidence also covers box
8, box 14a and the several-forms case is for that issue to check.

**Why blank and not refuse.** *Earlier recommendation, superseded by the
owner's decision above.* The block's other values are computed. The
name reaches no field in Form 67, Schedule FSI or Schedule TR. Refusing the
block would drop the computed values to protect one field the filer can copy
from their Form 1042-S when they have it.

### Options for the owner

1. **Leave it blank, with a notice** (the earlier recommendation,
   superseded by the owner's decision). The tool names no entity whose role
   is unproven.
2. **Keep the configured `institution_name`** (today's rule). Risk:
   Fidelity describes Fidelity Stock Plan Services, LLC as a recordkeeper
   and administrator, and no source shows it withheld the tax. The worksheet
   would keep telling filers to enter an entity whose role is unproven.
3. **Add a setting for the withholder's name** (chosen by the owner,
   2026-11-04). Risk: it is still a configured string, and the tool can't
   check it against a 1042-S it doesn't read. One value can't cover a year
   whose two 1042-S forms name different agents. A wrong or stale value
   repeats into every Quicko form, and the output looks checked. It adds a
   setting for a field that Form 67 and Schedule FSI and TR don't have.
4. **Parse the NetBenefits 1042-S.** This gives real evidence, but needs a
   new parser and a layout no public source shows. A check against a
   filer's own 1042-S needs the owner's OK, asked through the meta thread.
   That is far more work than this field needs. It could be its own issue if
   the owner wants the field filled.
5. **Refuse the block.** Risk: it drops the computed Quicko values, as
   above.

## Consequences

- The worksheet names the withholding agent the filer configured in
  `[netbenefits.account].withholding_agent_name`, not the A2 institution.
  The A2 identity doesn't change. #870 implements this.
- The tool can't check the configured name against a 1042-S it doesn't
  read. A wrong or stale value repeats into every Quicko form (option 3's
  risk).
- In an otherwise successful run without the setting that writes a Quicko
  form, that form's "Name of the deductor" value is blank and the run exits
  3 with a notice; the filer copies the name from their Form 1042-S into
  Quicko. Every other value in every file is the same as with the setting.
  A run that writes no Quicko form doesn't need it. (Until #1327 the run
  refused before writing anything.)
- One rule covers any later Quicko block, so #645 decision 3 doesn't need to
  re-decide it.
- **Still unproven:** Quicko's live label, whether Quicko sends the field
  anywhere, whether the NetBenefits 1042-S uses box 8 and box 14a, and what
  Quicko expects when several persons withheld the tax in one form.
- **Related, not in scope:** ADR-0003 says the shares are "custodied at
  Fidelity Stock Plan Services LLC", and the config example uses that name
  for A2. The Fidelity texts above describe that firm as a recordkeeper and
  administrator, and the 2013 agreement says it does not hold assets.
  Whether A2 should name another entity is a separate question (#871).
  This record doesn't change it.

[quicko-ftc]: https://help.quicko.com/portal/en/kb/articles/claim-tax-relief-on-foreign-income
[quicko-form67]: https://help.quicko.com/portal/en/kb/articles/how-to-file-form-67-on-income-tax-portal
[form67]: https://www.incometaxindia.gov.in/documents/20117/6105154/itr62form67.pdf/75090f08-e887-89ea-0eaf-7f3e46a106f3
[rule128]: https://www.incometaxindia.gov.in/documents/20117/42998/Rule-128_2026-01-13_11-37-01_1c629b_en.pdf/145a3343-3b83-7223-7064-8fd9194f161c
[itr2-form]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-04/Notification%20No.46_2026.pdf
[itr3-form]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-04/Notification_No_47_2026.pdf
[itr2-schema]: ../../src/ibkr_fa/reference/cbdt_itr2_schema_ay2026_27.json
[itr3-schema]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-07/ITR-3_2026_Main_V1.1.json
[i1042s-2025]: https://www.irs.gov/pub/irs-prior/i1042s--2025.pdf
[fidelity-faq]: https://www.fidelity.com/stock-plan-services/global/faqs-taxes
[fidelity-pa]: https://www.fidelity.com/webxpress/istockonly/docs/ParticipantAgreement.pdf
