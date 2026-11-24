"""Robinhood ``user.toml`` text for tests. Each ``robinhood_<entry>_toml``
builder returns one entry of a ``[[robinhood.…]]`` table; :func:`robinhood_toml`
returns a whole file (the global ``[account]`` table, an optional ``[robinhood]``
table and two accounts by default); :func:`write` writes text to a folder's
``user.toml`` and returns its path."""

from __future__ import annotations

from pathlib import Path

_ROBINHOOD_GLOBAL_TOML = """\
[account]
holder_name = "Tax Payer"
"""


def robinhood_account_toml(
    *,
    number: str,
    opening_date: str,
    source: str,
    status: str = "BeneficialOwner",
    institution_name: str = "Robinhood Securities, LLC",
    address: str = "500 Colonial Center Parkway, Lake Mary, FL",
    zip_code: str | None = "32746",
    country_iso2: str = "US",
    same_beneficial_owner: str = "true",
) -> str:
    zip_line = f'zip = "{zip_code}"\n' if zip_code is not None else ""
    return f"""\

[[robinhood.accounts]]
number = "{number}"
opening_date = {opening_date}
status = "{status}"
institution_name = "{institution_name}"
address = "{address}"
{zip_line}country_iso2 = "{country_iso2}"
same_beneficial_owner = {same_beneficial_owner}
source = "{source}"
"""


def robinhood_security_toml(
    *,
    symbol: str = "XQCA",
    cusip: str = "99972B101",
    isin: str = "US99972B1017",
    listing_exchange: str = "NASDAQ",
    sub_category: str = "COMMON",
    asset_class: str = "STK",
    issuer_country_code: str = "US",
    is_depositary_receipt: str = "false",
    source: str = "synthetic issuer evidence",
) -> str:
    return f"""\

[[robinhood.securities]]
symbol = "{symbol}"
cusip = "{cusip}"
isin = "{isin}"
listing_exchange = "{listing_exchange}"
sub_category = "{sub_category}"
asset_class = "{asset_class}"
issuer_country_code = "{issuer_country_code}"
is_depositary_receipt = {is_depositary_receipt}
source = "{source}"
"""


def robinhood_option_classification_toml(
    *,
    account_number: str = "100000001",
    financial_year: str = "2025",
    fema_risk_acknowledged: str = "true",
    treatment: str = "speculative-business",
    business_filing: str = "",
    source: str = "synthetic reviewed advice",
    reason: str = "synthetic US-listed option activity",
) -> str:
    return f"""\

[[robinhood.option_classifications]]
account_number = "{account_number}"
financial_year = {financial_year}
fema_risk_acknowledged = {fema_risk_acknowledged}
treatment = "{treatment}"
{business_filing}source = "{source}"
reason = "{reason}"
"""


_OPTION_BUSINESS_FILING = (
    "books_maintained = false\n"
    "other_speculative_business = false\n"
    'nature_of_business_code = "21010"\n'
    'turnover_basis = "icai-5.11a-differences"\n'
    'external_expenses_inr = "0"\n'
    'bp_additions_inr = "0"\n'
    'bp_deductions_inr = "0"\n'
)


def robinhood_security_receipt_resolution_toml(
    *,
    account_number: str = "100000001",
    source_record_index: str = "4",
    cusip: str = "99972B101",
    treatment: str = "promotional-reward",
    exact_quantity: str = '"0.052876"',
    reward_fair_market_value_usd: str = '"5.00"',
    income_reported_financial_year: str = "2024",
    income_reported: str = "true",
) -> str:
    return f"""\

[[robinhood.security_receipt_resolutions]]
account_number = "{account_number}"
source_record_index = {source_record_index}
cusip = "{cusip}"
treatment = "{treatment}"
exact_quantity = {exact_quantity}
reward_fair_market_value_usd = {reward_fair_market_value_usd}
income_reported_financial_year = {income_reported_financial_year}
income_reported = {income_reported}
source = "Robinhood reward confirmation and filed Indian return/workpaper"
reason = "Promotional stock reward reported as income at documented FMV"
"""


def robinhood_report_toml(
    *,
    file: str,
    start_date: str = "2025-01-01",
    end_date: str = "2026-03-31",
    source: str = "synthetic report export",
) -> str:
    return f"""\

[[robinhood.reports]]
file = "{file}"
start_date = {start_date}
end_date = {end_date}
source = "{source}"
"""


def robinhood_corrected_statement_toml(
    *,
    selected_file: str = "statements/2025-12-corrected.pdf",
    supersedes: str = '["statements/2025-12.pdf"]',
) -> str:
    return f"""\

[[robinhood.corrected_statements]]
selected_file = "{selected_file}"
supersedes = {supersedes}
source = "synthetic correction notice"
"""


_ROBINHOOD_FIRST = robinhood_account_toml(
    number="100000001",
    opening_date="2025-01-01",
    source="synthetic individual statement",
)


_ROBINHOOD_SECOND = robinhood_account_toml(
    number="200000002",
    opening_date="2025-10-01",
    source="synthetic managed statement",
)


def robinhood_toml(
    *,
    first: str = _ROBINHOOD_FIRST,
    second: str = _ROBINHOOD_SECOND,
    parent_fields: str = "",
    extra: str = "",
) -> str:
    parent = f"\n[robinhood]\n{parent_fields}" if parent_fields else ""
    return f"{_ROBINHOOD_GLOBAL_TOML}{parent}{first}{second}{extra}"


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "user.toml"
    p.write_text(text, encoding="utf-8")
    return p
