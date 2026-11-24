# ADR 0021: Which firm Schedule FA A2 names for the NetBenefits account

- **Status**: Accepted (2026-11-04)
- **Date**: 2026-11-04
- **Issue**: #871
- **Related**: #864, #870, #875, ADR-0003, ADR-0004, ADR-0019
- **Amended**: 2026-11-04 (#875) to accept owner option 4, with a warning

## Context

Schedule FA Table A2 lists each foreign custodial account with the name,
address and ZIP code of its financial institution. For the Fidelity
NetBenefits stock plan account, the tool takes all three from
`[netbenefits.account]`. The docs and examples fill them with Fidelity Stock
Plan Services LLC at "245 Summer Street, Boston, MA". This record asks which
entity belongs there.

Code references are at `cb2b002`. Web sources were retrieved on 2026-11-04
UTC, except the Common Offline Utility (downloaded 2026-11-03 UTC) and the
2013 Participant Agreement and tax FAQ that ADR-0019 quotes (its copies,
retrieved 2026-11-03 UTC).

### What the tool does today

- **Config.** `institution_name`, `address`, `zip` and `country_iso2` in
  `[netbenefits.account]` build the A2 institution (`config.py:946-991`).
  The A2 keys other than `zip` are required, with no default
  (`config.py:3140-3146`). An omitted `zip` becomes empty (`:3147`).
- **Docs and examples.**
  - ADR-0003 says the shares are "custodied at **Fidelity Stock Plan Services
    LLC**" (`docs/decisions/0003-fidelity-netbenefits-espp.md:13`).
  - The `NetBenefitsAccountConfig` docstring calls the table "the Fidelity
    Stock Plan Services custodial account identity" (`config.py:946-957`).
  - The examples give that name with "245 Summer Street, Boston, MA":
    `config.py:105-110`, `config_template.py:267-273` and `README.md:1147-1150`.
    The last two add ZIP `02210`.
- **Parser.** `_CUSTODIAN_RE` matches "Fidelity Stock Plan Services,? LLC"
  as "The custodian disclosure that identifies a genuine Fidelity SPS
  statement" (`parser/netbenefits/statement_pdf.py:67-71`). The module
  docstring (`:13-15`), `StatementData.custodian` (`:518-519`), the `Raises`
  text (`:1141-1145`) and the refusal (`:1182-1187`) call it the custodian.
  The field is set at `:1260` and read nowhere else in `src/`.
- **The sentence the parser matches** says something else. The committed
  fixtures, whose disclosure the tests call public boilerplate
  (`tests/test_parser_netbenefits_statement_pdf.py:3-4`), read: "Items shown
  under "Stock Plans" represent your interests under your company's stock
  plans, for which Fidelity Stock Plan Services LLC provides administrative
  and record keeping services."
  (`tests/fixtures/netbenefits/statement-oct-dec.txt:15`).
- **Fidelity CMA, for comparison.** ADR-0004 names National Financial
  Services LLC (NFS) as the CMA's A2 institution, "the carrying/custodial
  firm" (`docs/decisions/0004-fidelity-cma-classification.md:55-59`,
  `:103-106`). The CMA parser requires the statement disclosure "Brokerage
  services provided by Fidelity Brokerage Services LLC … Brokerage accounts
  carried by National Financial Services LLC"
  (`parser/fidelity_cma.py:81-85`). The CMA run writes NFS at "245 Summer
  Street, Boston, MA", ZIP `02210` (`pipeline_fidelity_cma_fa.py:55-60`).
  That is a different account.

### What A2 column 2 asks for

| Source | What it says |
|---|---|
| Notified [ITR-2][itr2-form] (G.S.R. 227(E), PDF p. 27) and [ITR-3][itr3-form] (G.S.R. 228(E), PDF p. 52) | Table heading: "Details of Foreign Custodial Accounts held (including any beneficial interest) at any time during the calendar year ending as on 31st December, 2025" ("December 2025" in ITR-3). Columns (4)-(6): "Name of financial institution", "Address of financial institution", "ZIP code". |
| [ITR-2 instructions][itr2-instructions], AY 2023-24, the newest found (PDF p. 38) | Lists "Table A2 - Foreign custodian accounts". Its A2 paragraph covers only the peak, closing and gross amounts. |
| ITD [FA/FSI guide][fa-guide] (PDF p. 3) | "Table A2 — Foreign Custodian Accounts". "Accounts maintained with foreign custodians, including peak balance, closing balance, and gross amounts paid or credited". |
| Vendored [schema][itr2-schema] `DtlsForeignCustodialAcc` (`:10384`) | "Details of Foreign Custodial Accounts". `FinancialInstName` (1-125 characters), `FinancialInstAddress` (1-200), `ZipCode` (1-8). No further definition. |
| Common Offline Utility 1.2.5 ([macOS build][offline-utility]) | Labels "Name of financial institution", "Address of financial institution" and "ZIP Code". Help: "Please enter the Name of the financial institution in Table A2 of Schedule FA." and "Please enter ZIP code, if  ZIP code is not available, then enter XXXXXX in Table A2 of Schedule FA." |

The tool's portal header for the column reads "Name of financial institution
in which the account is held" (`output/portal_labels/_ay_2026_27.py:103`).
The notified forms and the utility say only "Name of financial institution".
This record didn't trace where the longer label came from.

None of these texts defines the institution. They don't mention carrying or
clearing firms, introducing brokers, plan administrators or trustees. What
they do say is "Foreign Custodial Accounts", "Foreign custodian accounts"
and "Accounts maintained with foreign custodians".

[Rule 114F][rule-114f] helps as supporting taxonomy, as in ADR-0004
(`:62-66`). Its definitions govern Rules 114F-114H, not Schedule FA. A
"custodial account" is "an account (other than an insurance contract or
annuity contract) for the benefit of another person that holds one or more
financial assets" (PDF p. 1). A "custodial institution" is "any entity that
holds, as a substantial portion of its business, financial assets for the
account of others", with a 20% income test (PDF p. 3).

**Reading (an inference, not a stated rule):** column 2 names the custodian,
the entity that holds the account and its assets for the filer. Examples
are the firm that carries a brokerage account, or a trustee that holds the
shares in trust. An administrator or recordkeeper that holds nothing doesn't
fit, and neither does an introducing broker that holds nothing.

### Which entity holds a NetBenefits stock plan account

**Fidelity Stock Plan Services LLC: the administrator, not the holder.**

- Fidelity's current [Participant Agreement][fidelity-pa-2022] (form
  629474.6.0, PDF dated October 2022, PDF p. 1) opens: "Fidelity Stock Plan
  Services LLC (collectively with its affiliates, the “Administrator”)
  performs recordkeeping and other services (the “Services”) as agent of my
  employer company and its affiliates (the “Company”) and of the Participant
  Trust". It goes on: "I further understand and acknowledge that
  Administrator does not maintain an account for me, does not hold assets for
  me, does not effect transactions for me or on my behalf, and does not
  provide investment advice or recommendations to me."
- The same sentence is in the [2020 version][fidelity-pa-2020] (629474.4.0)
  and in the 2013 version that ADR-0019 cites.
- The statement sentence above, and Fidelity's stock plan tax FAQ (quoted in
  ADR-0019), also call it an administrator and recordkeeper.
- A FINRA [BrokerCheck firm search][brokercheck-search] for "fidelity stock
  plan services" returns no firm of that name.

So, on Fidelity's own public texts, it doesn't hold the account. Every
public source checked agrees.

**Candidate 1: the trustee of the SPS Participant Trust.**

- The Participant Agreement directs that Stock Plan Proceeds "be contributed
  on my behalf and at my direction to the Trustee of Participant Trust (the
  “Trustee”) to be held on my personal behalf, together with any cash amounts
  related to my participation in Company Stock Plans that I transfer to the
  Trustee, in trust, in accordance with the terms of the Participant Trust."
  (PDF p. 1, same in all three versions).
- Fidelity's international participant forms tie the stock plan account to
  that trust:
  - The [beneficiary form][intl-bene] (PDF dated May 2026, PDF p. 1) "is
    used to designate a beneficiary under your company stock compensation
    plans and the SPS Participant Trust."
  - The [proceeds form][intl-proceeds] (PDF dated August 2024), titled
    "Stock Plan Account Proceeds Distribution Instructions", starts "In order
    to receive proceeds from your Stock Plan Account" (PDF p. 1). Its terms
    for a check payment say "I authorize the Trustee of the SPS Participant
    Trust to make distributions to me pursuant to the instructions set forth
    in this Proceeds Distribution Form." (PDF p. 7).
- The only public trust document this research found is a [Declaration of
  Trust template][fidelity-dot]. Its PDF title contains "Declaration of Trust
  TEMPLATE 2011_12_22", and it was last modified in June 2013. It has blanks for
  "[COMPANY NAME]" and the date (PDF p. 1). It says:
  - It is made "by Fidelity Personal Trust Company, FSB, having a principal
    place of business in Boston, Massachusetts, as Trustee".
  - The trust is "to receive and to hold Company Stock, cash related to
    Participants’ participation in Company Stock Plans, and income earned
    with respect to property held in the Trust".
  - "The Trust shall separately account for each Participant’s share of
    items of property, income and expense." (2.3)
  - "The Trustee will provide safekeeping for all securities while they are
    in the custody of the Trust and collect any dividends and other income
    earned on all assets of the Trust." (4.2(ii))
  - The Trustee may register securities "in the name of any custodian of
    such property or of its nominee" (4.3(ix), PDF p. 2). It may engage "any
    Affiliated Entity or any other entity or person to provide services to
    the Trust, including, without limitation, the ability to employ agents,
    custodians" (4.3(xiv), PDF p. 2).
- Fidelity Personal Trust Company, FSB (FPTC) still exists:
  - The OCC's [list of active federal savings associations][occ-thrifts]
    (as of 9/30/2026) gives "Two Contra Way", Merrimack, NH.
  - FDIC [BankFind][fdic-fptc] (data run 10/30/2026) shows it active at
    "2 Contra Way", Merrimack, NH, ZIP `03054`.
  - NFS's [BrokerCheck report][brokercheck-nfs] lists FPTC as "under common
    control with the firm" at 2 Contra Way, Merrimack, NH 03054 (PDF p. 21).

**Unproven:**

- that a given filer's NetBenefits ESPP shares are in this trust;
- who the trustee is today, since the only public declaration found is a
  2011 template;
- whether the trustee holds the shares itself or through a custodian.

**Candidate 2: a brokerage account carried by NFS.**

- Microsoft's ESPP (the plan text in Annex A of the [2021 proxy][msft-proxy],
  effective 1 January 2022 subject to shareholder approval) says:
  - §2.9 (PDF p. 96): "“ESPP Broker” means a stock brokerage or other entity
    designated by the Company to establish accounts for stock purchased
    under the Plan by participants."
  - §16.1 (PDF p. 98): "Following the end of each offering, the number of
    shares of common stock purchased by each participant shall be deposited
    into an account established in the participant’s name at the ESPP
    Broker."
  - It doesn't name the ESPP Broker. §22.2 (PDF p. 99) lets the Administrator
    change the Plan for employees of non-US subsidiaries. This record didn't
    check later amendments.
- For a Fidelity brokerage account, the custodian is NFS:
  - Fidelity Brokerage Services LLC (FBS) "does not hold or maintain funds
    or securities" ([BrokerCheck][brokercheck-fbs], PDF p. 15). It
    "INTRODUCES ITS ACCOUNTS ON A FULLY-DISCLOSED BASIS TO ITS AFFILIATE,
    NATIONAL FINANCIAL SERVICES LLC, FOR SECURITIES CUSTODY, CLEARANCE AND
    SETTLEMENT" (PDF p. 18).
  - NFS "does hold or maintain funds or securities or provide clearing
    services for other broker-dealer(s)" ([BrokerCheck][brokercheck-nfs],
    PDF p. 15).
  - Fidelity's [Account Statement FAQ][fidelity-statement-faq] (PDF dated
    September 2025, PDF p. 7) speaks of "a Fidelity brokerage account
    custodied by National Financial Services, LLC".
- But the same FAQ's sample statement puts stock plans outside that account.
  Under "STOCK PLANS (as of July 31, 2017)" it reads (PDF p. 8):

  > Items shown under "Stock Plans" represent your interests under your
  > company's stock plans, for which Fidelity Stock Plan Services LLC provides
  > administrative and record keeping services. Items shown under "Stock
  > Plans" are not assets held in your Fidelity brokerage account, and
  > therefore are not carried by NFS and are not covered by SIPC. Fidelity
  > Stock Plan Services LLC provides this statement to you as part of
  > administrative and recordkeeping services it provides to the company. See
  > the Participant Agreement for details.

  Its first sentence matches the fixtures' sentence word for word. A second
  sample (PDF p. 24) repeats the "not carried by NFS" sentence.

**Unproven:** that a NetBenefits ESPP holding is in a brokerage account at
all.

**The Form 1042-S doesn't settle it.** A NetBenefits Form 1042-S names its
withholding agent in box 12d (#870). ADR-0019 ("What box 12d names") found
that box 12d names the withholding agent that filed the form, and that the
agent and the custodian can be different persons. If box 12d names NFS
(EIN 04-3523567), NFS could file it for an account it carries, or as an
agent or custodian for the trust (Declaration 4.3(ix), (xiv)). No public
source says which.

### The statement's own wording

The fixtures keep one disclosure sentence. They don't show whether real
NetBenefits statements also print the "not carried by NFS" sentence, name
the SPS Participant Trust or its trustee, or say "carried by National
Financial Services LLC". **Unproven.** No filer's statement was read for
this record.

A check the owner could run needs their OK, asked through the meta thread.
In their own NetBenefits statement disclosures, and in their Participant
Agreement or trust declaration if NetBenefits shows one, they would copy
any sentence that:

1. says the "Stock Plans" items are not assets held in a Fidelity brokerage
   account or are not carried by NFS;
2. names the SPS Participant Trust, a trustee, or Fidelity Personal Trust
   Company;
3. says an account is carried by, or held in custody at, National Financial
   Services LLC or another firm.

The copy would have no amounts, personal names, participant or account
numbers, or dates. The answer goes to the meta thread only.

### Address and ZIP

The ITD texts don't say which address to use when an entity has several.
The public records give:

| Entity | Address and ZIP | Source |
|---|---|---|
| NFS | Main office: 245 Summer Street, Boston, MA 02210. Mailing: Two Destiny Way, Mail Zone WG3D, Westlake, TX 76262. | [BrokerCheck][brokercheck-nfs], PDF p. 4 |
| FPTC | Two Contra Way, Merrimack, NH (OCC). 2 Contra Way, Merrimack, NH, ZIP 03054 (FDIC). The 2011 template says "principal place of business in Boston, Massachusetts". | [OCC][occ-thrifts], [FDIC][fdic-fptc], [template][fidelity-dot] |
| FBS | Main office: 900 Salem Street, Smithfield, RI 02917. | [BrokerCheck][brokercheck-fbs], PDF p. 4 |
| Fidelity Stock Plan Services LLC | Mail only: "P.O. Box 770001, Cincinnati, OH 45277-0030, USA"; "100 Crosby Parkway, Mail Zone: KC1M, Covington, KY 41015, USA". | [proceeds form][intl-proceeds], PDF p. 1 |

The example address, 245 Summer Street, Boston, MA 02210, is NFS's main
office. No public source checked gives it as an address of Fidelity Stock
Plan Services LLC.

## Decision

**Owner decision (2026-11-04, #875): option 4, with a warning.** On this
record's finding that Fidelity Stock Plan Services LLC is not the firm that
holds the account, the owner said: "Dont worry about that. Use this for
now, but just have a warning". On the SPS Participant Trust, the owner
said: "Like ignore the existence of the trust". So:

- Table A2 keeps naming the configured `[netbenefits.account]`
  `institution_name`, `address` and `zip`. The docs and examples keep
  Fidelity Stock Plan Services LLC at "245 Summer Street, Boston, MA".
- When the configured `institution_name` is Fidelity Stock Plan Services,
  the `netbenefits-fa` report warns. That is, once casefolded with only its
  ASCII letters kept, the name reads `fidelitystockplanservices` or
  `fidelitystockplanservicesllc`. The warning says the firm's own
  participant agreement says it doesn't maintain an account or hold assets
  for the participant. It asks the filer to check which firm their
  NetBenefits documents say holds the account, and to change
  `institution_name`, `address` and `zip` if those name a different firm.
- The run doesn't refuse. This is a warning, not a refusal, by the owner's
  explicit choice for this case. No output file or exit code changes.
- **Not adopted:** this record's recommended option 1 (name no entity;
  refuse Fidelity Stock Plan Services and unedited placeholders), the owner
  check of statement wording and the statement cross-check. The follow-up
  draft in PR #874's body is not filed.
- Nothing here covers the SPS Participant Trust, its trustee or Schedule FA
  Table F.

The research under Context stands as written. The rule below stands as this
record's reading, and the warning rests on it; the tool doesn't enforce it.
Passages marked "Earlier recommendation" record what this record proposed
before the owner's decision.

**Rule.** A2 columns 2-4 name the entity that holds the filer's account and
its assets: the custodian. That may be the firm that carries a brokerage
account, or the trustee that holds the shares in trust. It is never an
administrator, recordkeeper or introducing broker that holds nothing. This
follows the reading above, which is an inference.

**Not Fidelity Stock Plan Services LLC.** *Earlier recommendation,
superseded by the owner's decision above.* Its own agreement says it "does
not maintain an account for me, does not hold assets for me". The docs and
examples stop naming it. The parser stops calling it the custodian, and the
tool refuses it as `[netbenefits.account].institution_name`.

**No entity is recommended.** *Earlier recommendation, superseded by the
owner's decision above.* Public sources don't show which entity holds a
NetBenefits ESPP account. It could be the SPS Participant Trust's trustee
(FPTC in the only public template found, with today's trustee unproven), or a
brokerage account that NFS carries. Naming either would be a guess. So:

- The docs and examples name no entity and no address. The template's
  placeholders can't be filed: the tool refuses an unedited placeholder, and
  says which key to fill.
- The filer enters the institution, address and ZIP from their own
  documents that say who holds the account. These include the statement
  disclosures, the Participant Agreement, a trust declaration or an account
  agreement.
- **Address.** Use the entity's main office as its regulator records it:
  BrokerCheck's "Main Office Location" for a broker-dealer, or the OCC or
  FDIC record for a bank or trust company. Don't use a mailing or service
  address. The ITD texts don't settle this; it is this record's choice.
- The A2 institution stays a required, filer-supplied setting, as ADR-0003
  decision 5 and the IBKR `[account.custodian]` table already work. The tool
  can't check a name that no file it reads prints.

**Statement cross-check, after the owner check.** *Earlier recommendation,
superseded by the owner's decision above.* If the owner's statement
names the holder, a later issue makes the parser require that sentence, as
the CMA parser requires its FBS and NFS disclosure. The tool then also
refuses an `institution_name` that differs from the named holder; no parser
does that today. If the statement says the "Stock Plans" items
are not carried by NFS, the tool also refuses NFS for that statement. If
the statement names no holder, this record's rule stands, and the tool still
can't check the name.

### Options for the owner

The owner chose option 4, with a warning, on 2026-11-04. The options keep
their original text below, with the earlier recommendation marked.

1. **Name no entity; the filer supplies it; refuse Fidelity Stock Plan
   Services and unedited placeholders; cross-check after the owner check**
   (this record's earlier recommendation, superseded by the owner's
   decision).
2. **Name NFS** (245 Summer Street, Boston, MA 02210) in the docs and
   examples. Risk: Fidelity's own sample says "Stock Plans" items "are not
   carried by NFS". If the shares are in the SPS Participant Trust, whether
   NFS holds them for the trust, and whether the filer then has an account
   with NFS, is unproven. The 1042-S shows only who filed the form
   (ADR-0019).
3. **Name FPTC** (2 Contra Way, Merrimack, NH 03054). Risk: the only public
   declaration found is a 2011 template, today's trustee is unproven, and so is
   whether this plan uses the trust.
4. **Keep Fidelity Stock Plan Services LLC** (today). Risk: it contradicts
   the entity's own agreement. Every filer who copies the example names a
   firm that says it holds no account, at NFS's address. **Chosen by the
   owner, with a warning.**
5. **Refuse every NetBenefits A2 row until a statement names the holder.**
   It is stricter than option 1. Risk: if the statements name no holder, A2
   can never be written, and the CBDT JSON fragment with it (ADR-0003
   decision 7). That includes filers whose agreement or trust declaration,
   which the tool doesn't read, does name the holder.

## Consequences

- The A2 institution stays config-sourced. Every run writes the configured
  `[netbenefits.account]` name, address and ZIP into A2, as before. The docs
  and examples keep their values, and ADR-0003 doesn't change.
- When the name is Fidelity Stock Plan Services, the `netbenefits-fa`
  report, which `netbenefits-all` also prints, carries the warning (#875).
  The run doesn't refuse. Exit codes and every written file (the A3 and A2
  CSVs, the CBDT JSON fragment and the Quicko workbook) are unchanged; only
  the report text gains the warning.
- The owner accepts option 4's risk: a filer who copies the example and
  ignores the warning names a firm that says it holds no account, at NFS's
  address.
- Under the earlier recommendation, a config that copied the old example
  would have been refused until the filer changed it. The owner's decision
  doesn't adopt that refusal.
- The tool still can't check the name: no file it reads is known to name the
  holder.
- **Still unproven:**
  - which entity holds a NetBenefits ESPP account;
  - what the statement disclosures say about the holder.
- **Related, set aside by the owner** ("Like ignore the existence of the
  trust"): if the shares are in the SPS Participant Trust, is A2 still the
  right table? Schedule FA Table F covers "trusts, created
  under the laws of a country outside India, in which you are a trustee,
  beneficiary or settlor" ([ITR-2][itr2-form], PDF p. 28). The template
  calls the trust a "grantor trust" for each participant (PDF p. 1). This
  record doesn't decide whether the trust also needs a Table F row, or
  whether the filer's interest belongs in A2 at all.

[itr2-form]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-04/Notification%20No.46_2026.pdf
[itr3-form]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-04/Notification_No_47_2026.pdf
[itr2-instructions]: https://www.incometaxindia.gov.in/documents/20117/11038805/Instructions_ITR2_AY_2023_24.pdf/fe470b08-ceb7-340f-d2e3-d8fd1bf877b7
[fa-guide]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-03/Step%20by%20Step%20Guide%20FA%20FSI.pdf
[itr2-schema]: ../../src/ibkr_fa/reference/cbdt_itr2_schema_ay2026_27.json
[offline-utility]: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-10/ITDe-Filing-2026-1.2.5.dmg_.zip
[rule-114f]: https://www.incometaxindia.gov.in/documents/20117/42998/Rule-114F_2026-04-04_05-35-32_b33a3e_en.pdf/9f589641-adcf-a372-e346-57aa5b566bb9
[fidelity-pa-2022]: https://www.fidelity.com/bin-public/060_www_fidelity_com/documents/istock/stock-plan-services-participant-agreement.pdf
[fidelity-pa-2020]: https://workplaceservices.fidelity.com/static/mybenefits/sps/pdf/ParticipantAgreement.pdf
[brokercheck-search]: https://api.brokercheck.finra.org/search/firm?query=fidelity%20stock%20plan%20services&hl=true&nrows=12&start=0&r=25&sort=score+desc&wt=json
[intl-bene]: https://netbenefitsww.fidelity.com/bin-public/070_NB_SPS_Pages/documents/dcl/shared/StockPlanServices/SPS_International_Bene_Form.pdf
[intl-proceeds]: https://workplaceservices.fidelity.com/bin-public/070_NB_SPS_Pages/documents/dcl/shared/StockPlanServices/SPS_INTL_PROCEEDS_FORM.pdf
[fidelity-dot]: https://www.fidelity.com/webxpress/istockonly/docs/DOT.pdf
[occ-thrifts]: https://www.occ.treas.gov/topics/charters-and-licensing/financial-institution-lists/thrifts-by-name.pdf
[fdic-fptc]: https://api.fdic.gov/banks/institutions?filters=CERT%3A35206
[brokercheck-nfs]: https://files.brokercheck.finra.org/firm/firm_13041.pdf
[brokercheck-fbs]: https://files.brokercheck.finra.org/firm/firm_7784.pdf
[msft-proxy]: https://www.sec.gov/Archives/edgar/data/789019/000119312521298757/d189481ddef14a1.pdf
[fidelity-statement-faq]: https://www.fidelity.com/bin-public/060_www_fidelity_com/documents/Account-Statement-FAQs.pdf
