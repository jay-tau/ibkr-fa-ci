"""``user.toml`` inputs the config tests share (#1879).

- ``VALID_TOML``: the base IBKR ``[account]`` text; ``NON_UTF8_TOML``: its bytes
  with a holder name that isn't UTF-8;
- ``VALID_NETBENEFITS``: a ``[netbenefits]`` section; ``_A2_PEAK``: an A2 peak
  override for it; ``_nb_with_binding``: the section with the overrides'
  participant binding; ``_netbenefits``: ``VALID_TOML`` and the section with one
  edit;
- ``_form67_override``: a ``[[form67.tax_payable_overrides]]`` entry;
- ``_fidelity_cma``: ``VALID_TOML`` and a ``[fidelity_cma]`` table with one US
  tax year; ``_FIDELITY_CMA_BASE``: a Fidelity CMA ``user.toml``;
- ``unknown_key``, ``UNKNOWN_COUNTRY_CODE`` and ``UNVERIFIED_STOCKPLAN_ISSUER``:
  refusal texts.
"""

from __future__ import annotations

from tests._fidelity_cma import cma_config
from tests._text_edits import replace_once

VALID_TOML = """\
[account]
number = "U1234567"
holder_name = "Sample Holder"
opening_date = 2024-02-01
status = "BeneficialOwner"
"""


# A Windows-1252 editor saves "é" as the lone byte 0xE9, which isn't UTF-8 (#934).
NON_UTF8_TOML = VALID_TOML.encode().replace(b"Sample Holder", b"PRIVATE-MARKER\xe9")


def unknown_key(allowed: str) -> str:
    """The refusal of an unknown key in a table whose allowed keys are ``allowed``."""
    return f"has an unknown key; allowed keys: {allowed}"


UNKNOWN_COUNTRY_CODE = "unknown country code (not in the ITR country list)"


UNVERIFIED_STOCKPLAN_ISSUER = (
    "Morgan Stanley StockPlan exports are verified only for Alphabet GOOG (US02079K1079); "
    "refusing the configured issuer until its real exports are reviewed (#382)."
)


VALID_NETBENEFITS = """
[netbenefits]
espp_discount_pct = "10"
participant_no = "OQ1234567"

[netbenefits.account]
institution_name = "Fidelity Stock Plan Services LLC"
address = "245 Summer Street, Boston, MA"
zip = "02210"
country_iso2 = "US"
account_opening_date = 2025-12-31
status = "BeneficialOwner"
withholding_agent_name = "National Financial Services LLC"

[netbenefits.security]
symbol = "XESP"
cusip = "99999E104"
isin = "US99999E1047"
entity_address = "1 Sample Issuer Street, Sample City, XX"
entity_zip = "00000"
listing_exchange = "NASDAQ"
issuer_country_code = "US"
nature_of_entity = "Listed company shares"

[[netbenefits.price_overrides]]
price_date = 2025-12-31
price_per_share_usd = "213.47"
source = "SPS year-end statement"
reason = "12-31 lot unpriced in CSV"

[[netbenefits.cg_fmv_values]]
purchase_date = 2025-12-31
section17_gross_fmv_total_inr = "41850.00"
source = "Employer perquisite worksheet"
"""


_A2_PEAK = """
[[netbenefits.a2_peak_overrides]]
calendar_year = 2025
peak_balance_inr = 66503
"""


# The participant binding is a top-level [netbenefits] key, so it must be inserted
# into the [netbenefits] section (before its sub-tables), not appended.
def _nb_with_binding(participant_no: str = "OQ1234567") -> str:
    return VALID_NETBENEFITS.replace(
        'participant_no = "OQ1234567"\n',
        f'participant_no = "OQ1234567"\na2_peak_overrides_participant_no = "{participant_no}"\n',
    )


def _netbenefits(old: str, new: str) -> str:
    return VALID_TOML + replace_once(VALID_NETBENEFITS, old, new)


def _form67_override() -> str:
    return (
        "\n[[form67.tax_payable_overrides]]\n"
        'country = "US"\nsource = "DIVIDEND"\ntax_payable_normal_inr = "100"\n'
    )


def _fidelity_cma(status: str, body: str) -> str:
    return (
        VALID_TOML
        + '\n[fidelity_cma]\naccount_id = "Z99-999999"\nspaxx_isin = "US31617H1023"\n'
        + f'\n[[fidelity_cma.us_tax_years]]\ntax_year = 2025\nrefund_status = "{status}"\n{body}'
    )


_FIDELITY_CMA_BASE = cma_config(spaxx_isin="US31617H1023", cma_status="BeneficialOwner")
