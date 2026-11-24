"""The ``user.toml`` texts that several test modules share.

- ``ACCOUNT_AND_FX_USER_TOML``: an ``[account]`` table for the synthetic IBKR
  account and an ``[fx]`` table that sets ``max_carry_forward_days``.
  ``tests._activity_statement_e2e``, ``tests._fa_a3_address`` and
  ``tests._schedule_os_e2e`` bind their public names to it.
- ``US_PERSON_USER_TOML``: the same account plus ``us_person = true``, with
  ``max_carry_forward_days = 4``. IBKR command tests write it, among them
  the Schedule CG end-to-end runs and the writers in ``tests._ibkr_run_all``
  and ``tests._ibkr_form67_runs``.
- ``CUSTODIAN_TOML``: the IBKR ``[account.custodian]`` table that Schedule FA
  Table A2 needs, with a leading newline so it can follow the line before it.
- ``US_FOREIGN_TIN_TOML``: an ``[account.foreign_tins]`` table with a US TIN,
  with a leading newline like ``CUSTODIAN_TOML``.
"""

ACCOUNT_AND_FX_USER_TOML = """\
[account]
number = "U12345678"
holder_name = "Sample Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"

[fx]
max_carry_forward_days = 14
"""

US_PERSON_USER_TOML = """\
[account]
number = "U12345678"
holder_name = "Sample Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"
us_person = true

[fx]
max_carry_forward_days = 4
"""

CUSTODIAN_TOML = """
[account.custodian]
institution_name = "Interactive Brokers LLC"
address = "One Pickwick Plaza, Greenwich, CT"
zip = "06830"
country_iso2 = "US"
"""

US_FOREIGN_TIN_TOML = """
[account.foreign_tins]
US = "123456789"
"""
