"""Starter ``user.toml`` template for the ``ibkr-fa init`` subcommand.

``ibkr-fa init`` scaffolds a fully-annotated ``user.toml`` so a new user
does not have to hand-author the config from the docs. Two design rules,
both flowing from the project's "it's taxes — never be wrong" philosophy:

* **Everything is commented out.** A freshly-scaffolded file does NOT
  load — every command reports ``account: missing required table`` until
  the user uncomments and edits the ``[account]`` block. This is
  deliberate: of the four ``[account]`` fields an IBKR run needs, only ``number``
  is cross-checked against the statement (see
  ``pipeline_dispatch._cross_check_account_id``); ``holder_name``,
  ``opening_date`` and ``status`` flow UNVERIFIED into the filed Schedule
  FA output. Shipping live example values for those would let a user who
  edited only ``number`` silently file the example name / date / status.
  Commenting the whole file forces a conscious value for every field.

* **One canonical source.** :data:`_EXAMPLE_USER_TOML` is a single,
  fully-valid annotated config exercising every supported section; it is
  parsed by the test-suite so a typo in any example value fails CI.
  :data:`USER_TOML_TEMPLATE` (what ``init`` actually writes) is that
  example with each *config* line commented out — the prose annotations
  stay readable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from ibkr_fa.exceptions import PathRefusalError
from ibkr_fa.path_checks import is_dir

# A complete, schema-valid example covering every supported [section].
# Each value a user must change is tagged ``REPLACE``. This is the single
# source of truth: the test-suite loads it through ``load_user_config`` so
# any typo / schema drift here fails CI. ``USER_TOML_TEMPLATE`` below is
# this text with the config lines commented out.
_EXAMPLE_USER_TOML: Final[str] = """\
# holder_name is the only required field in [account].
[account]
number = "U1234567"  # REPLACE: IBKR account ID; IBKR commands only — delete it for other sources
holder_name = "Your Legal Name"  # REPLACE: as on your ITR (NOT cross-checked — set carefully)
opening_date = 2020-01-01  # REPLACE: IBKR commands only; YYYY-MM-DD (NOT cross-checked)
status = "BeneficialOwner"  # REPLACE: IBKR and StockPlan commands: BeneficialOwner | Joint
us_person = false  # true ONLY if you have a W-9 on file at IBKR (US citizen / green-card)
allow_masked_account = false  # true to skip the account-ID cross-check on a masked statement
accept_anonymised_statement = false  # true to take holder_name/number from here when hidden

# Your foreign Taxpayer Identification Number per country (ISO 3166-1
# alpha-2 -> TIN), auto-filled into Schedule FSI + TR. Each key must be a
# code the ITR country list has (e.g. GB, not UK); never "IN". Omit a
# country to leave its TIN blank. If no TIN was allotted, use your passport
# number.
[account.foreign_tins]
US = "123456789"  # REPLACE: your US TIN, or passport number if none was allotted

# Your IBKR custodial institution, for Schedule FA Table A2 (the account
# itself). country_iso2 is the INSTITUTION's country (where the IBKR
# entity is), never "IN". Omit this whole block if you do not file Table A2.
[account.custodian]
institution_name = "Interactive Brokers LLC"  # REPLACE with the entity named on your statement
address = "One Pickwick Plaza, Greenwich, CT"  # REPLACE
zip = "06830"  # needed for upload; blank = manual fill (exit 3)
country_iso2 = "US"  # REPLACE: the institution's country

# SBI TTBR staleness ceiling (Rule 115), in days. Defaults to 14 when
# omitted; uncomment only to override.
[fx]
max_carry_forward_days = 14

# ADR / GDR source attribution for FSI/TR/Form 67. Defaults to
# "depositary". "underlying-issuer" does not change Schedule OS 1a(i).
[os]
schedule_os_indian_adr_routing = "depositary"

# Optional exact Form 67 inputs. Without this block, Indian-tax and FTC
# columns stay blank because broker data cannot establish Indian tax
# attributable to foreign income.
[form67]
# Each command's files cover only the income it read. Per-(country, source)
# EXACT Indian tax payable covers that income. Every command run with this file
# applies the full amount to its own income, so when commands that read different
# files report the same country and source, leave it out and enter one combined
# row by hand (see the README's [form67] section).
# Valid `source` names: SALARY, HOUSE_PROPERTY, BUSINESS_INCOME,
# LONG_TERM_CAPITAL_GAIN, SHORT_TERM_CAPITAL_GAIN, INTEREST_INCOME,
# DIVIDEND, ROYALTY, FEES_FOR_TECHNICAL_SERVICES, OTHERS.
[[form67.tax_payable_overrides]]
country = "US"  # ISO 3166-1 alpha-2 in the ITR country list (e.g. GB, not UK); never "IN"
source = "DIVIDEND"  # one of the names listed above
tax_payable_normal_inr = "12345.67"  # REPLACE

# §91 (non-DTAA) claims remain fail-closed. They require both fields below
# and an exact matching tax_payable_overrides entry above.
[form67.section_91]
total_indian_tax_before_91_relief_inr = "250000.00"  # REPLACE
total_income_inr = "1000000.00"  # REPLACE

[prices]
# File containing MASSIVE_API_KEY=...; relative paths resolve beside user.toml,
# and absolute paths are used directly. Never put the key itself in TOML.
# massive_api_key_file = "env"

# Map an IBKR ticker to the correct Yahoo Finance symbol (ISIN-guarded)
# when the automatic lookup picks the wrong / wrong-currency listing.
# One entry per ibkr_ticker; the ISIN must match the held lot to apply.
# Used by schedule-fa, robinhood-fa, stockplan-fa and netbenefits-fa; for
# a Robinhood, StockPlan or NetBenefits holding, ibkr_ticker is its
# configured symbol. Verify yahoo_symbol is the same security.
[[prices.ticker_overrides]]
ibkr_ticker = "XNEW"  # REPLACE: the ticker as it appears in your IBKR statement
isin = "CA99999B1004"  # REPLACE: the held security's ISIN
yahoo_symbol = "XOLD"  # REPLACE: the correct Yahoo Finance symbol

# Per-issuer Schedule FA Table A3 identity, keyed by the holding's ISIN.
# Verify nature_of_entity from authoritative issuer material (prospectus /
# registry); broker labels such as ETF/COMMON do not prove legal entity type.
# Robinhood prefers exact legal-entity evidence. Set either independent opt-out
# only when the corresponding configured value has been explicitly reviewed.
# address is required for an IBKR holding. For a Robinhood holding you may leave
# it out (and then zip and prefer_configured_address): the run then writes the
# address it finds to the private robinhood-identity-guidance.toml and stops
# until you verify it here.
[[fa.entities]]
isin = "US99999A1025"  # REPLACE: the holding's ISIN
address = "1 Example Plaza, Anytown, NY"  # REPLACE
zip = "99999"  # optional
name = "Example Holdings Inc."  # optional; omit to keep the statement's name
nature_of_entity = "Company"  # REPLACE: verified free text, 1-34 chars
# prefer_configured_address = true
# prefer_configured_nature = true
# prefer_configured_name = true

# Manual figures for the CBDT Schedule FA JSON fragment (`schedule-fa
# --format cbdt-json-fragment`, #39) that can't be computed from the
# statement. Only needed if you generate that JSON. manual_values_account_number
# MUST equal [account].number (binds these filed figures to this account), and
# the manual values also need [account].opening_date.
[schedule_fa]
manual_values_account_number = "U1234567"  # REPLACE: same as [account].number

# How the FA-A3 "Peak value during the period" is valued (applies to every
# holding, IBKR and NetBenefits; fidelity-cma-fa rejects "high" with exit 4
# and writes neither A2 nor A3 output):
#   "close" (default) — highest native-currency daily CLOSING value. A close
#                       carries only across weekends and listed market holidays.
#                       Each day needs the latest session's own close; if missing,
#                       the holding is left for manual fill.
#   "high"            — highest native-currency intraday HIGH value (a more
#                       conservative reading). Needs fetched prices with a high
#                       on every US market session in the holding window, else
#                       the holding fails closed to manual-fill.
# Full-year holiday coverage is 2025 only; 2024-12-25 is also listed.
# In other years, "close" treats unlisted weekday holidays as sessions,
# while "high" still needs a high on every weekday. Missing required
# prices leave the holding for manual fill.
# Under either basis, a fetched USD price dated on a weekend or a listed market
# closure, inside the holding window or carried into it, refuses the run
# (exit 4): the price source and the market calendar disagree.
# peak_basis = "close"
#
# Peaks are selected in the foreign currency first, then converted using the
# selected date's SBI TTBR. To instead maximize each day's INR-converted value:
# peak_currency = "inr"  # non-default override; emits a warning

# One block per Schedule FA calendar year. a2_peak_balance_inr is the
# HIGHEST total native-currency value (securities + cash, per Rule 114F)
# your custodial account reached on any single day, converted to WHOLE rupees
# at that peak date's SBI TTBR.
[[schedule_fa.manual_values]]
calendar_year = 2025  # REPLACE: the Schedule FA calendar year (Jan-Dec)
a2_peak_balance_inr = 8950  # REPLACE: from IBKR's daily NAV report

# Robinhood support consumes one two-account brokerage bundle. Report filenames
# identify files/ranges only; statement/activity reconciliation assigns each
# report to its account. Use a dedicated output directory for Robinhood.
[robinhood]
# Required only for a cold official SEC identity fetch. This identifies the
# client under SEC fair-access rules; it is not an API key or account.
# sec_user_agent = "Your Name your-email@example.com"
# Optional private manual exceptions for stale cross-document CUSIPs.
# Relative paths resolve beside user.toml; the file stays outside user.toml.
# cusip_transitions_file = "overrides/robinhood/cusip-transitions.toml"

[[robinhood.accounts]]
number = "123456789"  # REPLACE: first brokerage account number
opening_date = 2025-01-01  # REPLACE: exact account opening date
status = "BeneficialOwner"
institution_name = "Robinhood Securities, LLC"
address = "500 Colonial Center Parkway, Lake Mary, FL"
zip = "32746"  # needed for upload; blank = manual fill (exit 3)
country_iso2 = "US"
same_beneficial_owner = true
source = "Robinhood individual brokerage statement"  # REPLACE provenance

[[robinhood.accounts]]
number = "987654321"  # REPLACE: second brokerage account number
opening_date = 2025-10-01  # REPLACE
status = "BeneficialOwner"
institution_name = "Robinhood Securities, LLC"
address = "500 Colonial Center Parkway, Lake Mary, FL"
zip = "32746"  # needed for upload; blank = manual fill (exit 3)
country_iso2 = "US"
same_beneficial_owner = true
source = "Robinhood managed brokerage statement"  # REPLACE provenance

[[robinhood.securities]]
symbol = "MSFT"  # REPLACE
cusip = "594918104"  # REPLACE
isin = "US5949181045"  # REPLACE
listing_exchange = "NASDAQ"  # REPLACE
sub_category = "COMMON"  # REPLACE: COMMON or ETF
asset_class = "STK"
issuer_country_code = "US"  # REPLACE
is_depositary_receipt = false  # REPLACE from issuer evidence
source = "Issuer and exchange records"  # REPLACE provenance

# Every REC row hard-fails until one exact row-bound quantity/basis resolution
# is supplied. Set income_reported=true only after the documented reward FMV
# is included in the applicable Indian return/workpaper.
# [[robinhood.security_receipt_resolutions]]
# account_number = "123456789"
# source_record_index = 4
# cusip = "594918104"
# treatment = "promotional-reward"
# exact_quantity = "0.027318"
# reward_fair_market_value_usd = "5.00"
# income_reported_financial_year = 2024
# income_reported = true
# source = "Robinhood reward confirmation and filed Indian return/workpaper"
# reason = "Promotional stock reward reported as income at documented FMV"

[[robinhood.option_classifications]]
account_number = "123456789"  # REPLACE
financial_year = 2025
# Required opt-in after independently reviewing FEMA/LRS implications.
# This enables tax reporting only; it is not a FEMA-compliance conclusion.
fema_risk_acknowledged = true
# capital-gains | speculative-business | non-speculative-business
treatment = "speculative-business"
# These preserve your reviewed input; they are not proof that a US exchange
# satisfies section 43(5)(d)'s prescribed/notified-exchange condition.
source = "Reviewed CA memo identifying the section 43(5) treatment"  # REPLACE
reason = "Supported long options closed BTO-to-STC without delivery"  # REPLACE

# Exactly two activity report files, directly under <bundle>/activity/.
[[robinhood.reports]]
file = "activity/individual.csv"
start_date = 2025-01-01  # selected report range; must start no later than account opening
end_date = 2026-03-31  # must align to an issued statement period end
source = "Robinhood account activity export"  # REPLACE

[[robinhood.reports]]
file = "activity/managed-individual.csv"
start_date = 2025-01-01
end_date = 2026-03-31
source = "Robinhood managed account activity export"  # REPLACE

# Optional explicit winner when Robinhood issued a corrected statement.
# [[robinhood.corrected_statements]]
# selected_file = "statements/2025-12-corrected.pdf"
# supersedes = ["statements/2025-12.pdf"]
# source = "Robinhood correction notice"

# Fidelity NetBenefits (e.g. Microsoft ESPP) support. Participant Number is
# printed on Fidelity documents and is the Schedule FA A2 account ID.
# Read by the Fidelity NetBenefits flow (added incrementally; tracking issue
# #305) — the IBKR commands ignore it. Omit this whole block if you have no
# NetBenefits holding.
[netbenefits]
espp_discount_pct = "10"  # REPLACE: your ESPP discount percent (> 0, <= 100)
participant_no = "OQ1234567"  # REPLACE: NetBenefits Participant No. / A2 account ID

# Required together when supplying the manual A2 peak for CBDT JSON:
# a2_peak_overrides_participant_no = "OQ1234567"  # same as participant_no
# [[netbenefits.a2_peak_overrides]]
# calendar_year = 2025
# peak_balance_inr = 57314  # native peak converted at its date's TTBR, whole INR

# Required only when this purchase is sold. Use the historical GROSS INR FMV
# actually taken into account under section 17(2)(vi), from the employer's
# perquisite worksheet (not the smaller taxable discount shown in Form 16).
# Not used yet: netbenefits-cg refuses an FY with a sale until #1150.
[[netbenefits.cg_fmv_values]]
purchase_date = 2025-12-31  # REPLACE: confirmation purchase date
section17_gross_fmv_total_inr = "41850.00"  # REPLACE: all shares in that purchase
source = "Employer perquisite worksheet reconciled to Form 16"  # REPLACE

# The Fidelity custodial account, for Schedule FA Table A2. The Participant
# Number above is used as the A2 account ID. account_opening_date remains
# REQUIRED (never inferred); country is the institution's, never "IN".
# The run warns when institution_name is Fidelity Stock Plan Services, whose
# participant agreement says it holds no account for you (ADR 0021): check
# which firm your NetBenefits documents say holds the account.
[netbenefits.account]
institution_name = "Fidelity Stock Plan Services LLC"  # REPLACE
address = "245 Summer Street, Boston, MA"  # REPLACE
zip = "02210"  # needed for upload; blank = manual fill (exit 3)
country_iso2 = "US"  # REPLACE: the institution's country
account_opening_date = 2025-12-31  # REPLACE: YYYY-MM-DD, as filed
status = "BeneficialOwner"  # REPLACE: BeneficialOwner | Joint
# Quicko's "Name of the deductor" (not part of A2): copy the withholding agent
# from your Form 1042-S, box 12d for tax in box 7a, or the agent box 14a names
# for tax in box 8. Without it, a run that writes a Quicko form leaves that
# value blank and exits 3.
withholding_agent_name = "National Financial Services LLC"  # REPLACE

# The ESPP security, for Schedule FA Table A3 and the holding fields no
# Fidelity file supplies. A US isin must equal "US" + cusip + check digit.
[netbenefits.security]
symbol = "MSFT"  # REPLACE
cusip = "594918104"  # REPLACE: 9-character CUSIP
isin = "US5949181045"  # REPLACE: ISO 6166 ISIN
entity_address = "One Microsoft Way, Redmond, WA"  # REPLACE: the issuer's address
entity_zip = "98052"  # optional; may be blank
listing_exchange = "NASDAQ"  # REPLACE
issuer_country_code = "US"  # REPLACE: the issuer's country
nature_of_entity = "Listed company shares"  # REPLACE

# Fidelity Cash Management Account support. This account is distinct from
# NetBenefits and from [account].number. SPAXX is the only supported security.
[fidelity_cma]
account_id = "Z99-999999"  # REPLACE: Fidelity CMA account ID from the statement
spaxx_isin = "US31617H1023"  # verified Fidelity Government Money Market Fund ISIN
# fidelity-cma-fa needs status; fidelity-cma-income and fidelity-cma-cg don't read it.
status = "BeneficialOwner"  # observed INDIVIDUAL TOD registration; only supported CMA status
# Default false: keep the A2 peak blank and print the intraday-order warning.
# If true, peak_balance_bound is mandatory; choose lower or upper. In native
# currency, upper may overstate and lower may understate the intraday peak.
# Each bound converts at its own date's TTBR, so in rupees lower can exceed
# upper; fidelity-cma-fa then refuses the attested bound (exit 4).
suppress_peak_balance_warning = false

# One runtime refund status per US calendar tax year. Valid statuses:
#   unknown | not-filed-planned | filed-pending | finalized | no-refund-claim
# Missing years behave as "unknown". Only finalized and no-refund-claim can
# claim FTC; any other state with in-FY withholding emits income but blocks ALL
# FTC for the Indian FY. A claiming year also needs a reconciled year-end report
# and parser-supported Form 1042-S in the same run.
[[fidelity_cma.us_tax_years]]
tax_year = 2025
refund_status = "unknown"
# When finalized or no-refund-claim, add `provenance` plus one entry for EVERY
# actual withholding date in that tax year (including zero eligible tax). The
# amount is the US tax owed under US law within the DTAA Article 10(2)(b) 25%
# limit, never gross withholding: exclude withholding US law doesn't impose,
# such as on an exempt interest-related dividend part. No US return is needed
# for no-refund-claim. See the Fidelity CMA README section for the syntax.

# Optional manual per-share USD prices for a date the automatic lookup can't
# cover (e.g. a year-end lot the open-lots CSV leaves unpriced). Full
# provenance required. One block per date.
[[netbenefits.price_overrides]]
price_date = 2025-12-31  # REPLACE: the last US market session on/before Dec 31
price_per_share_usd = "312.45"  # REPLACE: the USD per-share price (> 0)
source = "SPS year-end statement"  # REPLACE: where the figure came from
reason = "12-31 lot shows '-' (unpriced) in the open-lots CSV"  # REPLACE

# Morgan Stanley StockPlan (Shareworks) support, e.g. Google/Alphabet GSUs
# (RSUs). Used by `stockplan-income` and `stockplan-fa`.
[stockplan]
# Optional: the date the StockPlan account opened. Set it if that falls inside
# a period you file: the Capital Gain/Loss report must then cover that period
# from this date, not from its start. [account].opening_date is never used.
opening_date = 2025-04-14  # REPLACE or delete: StockPlan account opening date, YYYY-MM-DD

# The holding's identity. The ISIN is not in any StockPlan export, so it is
# configured here (its own check digit is validated); `symbol` is cross-checked
# against the report. symbol + isin are all `stockplan-income` needs; the
# remaining fields are only required by `stockplan-fa` (Schedule FA).
# Only Alphabet GOOG / US02079K1079 exports are verified so far; StockPlan
# commands refuse any other symbol/ISIN pair until its exports are (#382).
[stockplan.security]
symbol = "GOOG"  # REPLACE: the trading symbol, as it appears in the report
isin = "US02079K1079"  # REPLACE: ISO 6166 ISIN (this is Alphabet Class C)
entity_name = "Alphabet Inc."  # REPLACE: the issuer's name (FA)
entity_address = "1600 Amphitheatre Parkway, Mountain View, CA"  # REPLACE (FA)
entity_zip = "94043"  # optional; may be blank (FA)
listing_exchange = "NASDAQ"  # REPLACE (FA)
issuer_country_code = "US"  # REPLACE: the issuer's country (FA)
nature_of_entity = "Listed company shares"  # REPLACE (FA)
"""

# Guidance header prepended to the written file. Explains that the file is
# inert by design and how to activate it.
_HEADER: Final[str] = """\
# ibkr-fa configuration — generated by `ibkr-fa init`.
#
# This file is a TEMPLATE: every setting below is COMMENTED OUT on
# purpose, so a freshly-generated config never files example data. Until
# you edit it, ibkr-fa will report "account: missing required table".
#
# To activate:
#   1. Uncomment and fill the [account] table (REQUIRED: holder_name;
#      number and opening_date only if you run IBKR commands, status only
#      for IBKR and StockPlan commands). Replace every value tagged REPLACE
#      that you keep — only `number` is cross-checked, against your IBKR
#      statement, so set holder_name / opening_date / status carefully.
#   2. Uncomment any OPTIONAL sections you need (the rest can stay off).
#   3. Run e.g. `ibkr-fa run-all --config user.toml ...`.
#
# Field reference: docs/ and the ibkr_fa.config module docstring.
"""


def _comment_config_lines(sample: str) -> str:
    """Comment out the config lines of *sample*, leaving prose readable.

    A line is treated as *config* (and gets a ``# `` prefix) when, after
    stripping leading whitespace, it begins a TOML table header (``[``) or
    a key assignment. Blank lines and existing ``#`` prose/annotation
    lines are emitted unchanged so the rendered template stays legible.
    """
    out: list[str] = []
    for line in sample.splitlines():
        stripped = line.lstrip()
        if stripped == "" or stripped.startswith("#"):
            out.append(line)
        else:
            out.append(f"# {line}")
    return "\n".join(out)


# The exact text `ibkr-fa init` writes: the guidance header followed by the
# canonical example with every config line commented out.
USER_TOML_TEMPLATE: Final[str] = _HEADER + "\n" + _comment_config_lines(_EXAMPLE_USER_TOML) + "\n"


def write_user_toml_template(path: Path, *, force: bool = False) -> None:
    """Write the starter ``user.toml`` template to *path*.

    Args:
        path: Destination file path.
        force: Overwrite an existing file when True. When False (default)
            an existing *path* is left untouched.

    Raises:
        FileExistsError: If *path* already exists and *force* is False.
        PathRefusalError: If *path*'s parent directory does not exist
            (``init`` does not create directories — refusing avoids
            silently materialising a mistyped path like ``confg/user.toml``).
        OSError: For other write failures (permissions, etc.).
    """
    parent = path.parent
    if not is_dir(parent):
        raise PathRefusalError(
            "parent directory does not exist (create it first, or choose an existing directory)",
            parent,
        )
    # "x" = exclusive create: refuses to clobber an existing file (the
    # non-force safety net) without a check-then-write race window.
    mode = "w" if force else "x"
    with path.open(mode, encoding="utf-8") as handle:
        handle.write(USER_TOML_TEMPLATE)
