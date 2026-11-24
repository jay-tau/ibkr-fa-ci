"""The ``[form67]`` tax payable override that several test modules add to a
config."""

# An exact Indian tax payable for US dividends makes their Form 67 row READY
# (#601).
FORM67_READY_OVERRIDE = """
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "1000"
"""
