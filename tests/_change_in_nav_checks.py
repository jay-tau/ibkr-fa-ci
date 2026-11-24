"""The Change in NAV income texts, expected refusal texts and assertion helpers
that the income commands' and Schedule FA's Change in NAV tests share (#1279,
#1324). Every amount here is synthetic.

- ``CHANGE_IN_NAV_USER_TOML``: the shared US-person ``user.toml`` text
  (``tests._ibkr_user_toml.US_PERSON_USER_TOML``) followed by its US foreign
  TIN table (``US_FOREIGN_TIN_TOML``).
- ``DIVIDENDS_SECTION``, ``WITHHOLDING_TAX_SECTION`` and ``INTEREST_SECTION``: a
  ``Dividends``, ``Withholding Tax`` and ``Interest`` section, each a header and
  one USD row; the withholding row is on the dividend.
- ``EUR_DEBIT_INTEREST_ROW``: an ``Interest`` row of EUR debit interest, a
  currency other than the statements' USD base currency.
- ``CHANGE_IN_NAV_SECTION_FIELDS``: the ``Change in NAV`` field and amount that
  match each of those three sections.
- ``RE_EXPORT_HINT``: the refusal sentence that asks for a re-exported
  Activity Statement, which every Activity Statement refusal test that pins
  that whole sentence reads (#1983).
- ``absent_section_refusal``, ``change_in_nav_differs`` and
  ``other_currency_refusal``: the expected refusal text when a section is
  missing, when a ``Change in NAV`` field differs from its section's rows, and
  when a section has a row in another currency.
- ``assert_no_amount``: asserts that a text holds none of the amounts, each
  without its minus sign; ``assert_private_error``: asserts that neither an
  error's text, repr, args nor attributes hold one, and that the error has no
  cause or context.
- ``MISSING_SECTION_CASES``: each missing-section case's printed ``Change in
  NAV`` fields and expected refusal.
"""

from __future__ import annotations

from tests._ibkr_user_toml import US_FOREIGN_TIN_TOML, US_PERSON_USER_TOML

CHANGE_IN_NAV_USER_TOML = US_PERSON_USER_TOML + US_FOREIGN_TIN_TOML

_DIVIDEND_DESCRIPTION = "XQBA(US99995C1027) Cash Dividend USD 0.47 per Share"
DIVIDENDS_SECTION = (
    "Dividends,Header,Currency,Date,Description,Amount\n"
    f"Dividends,Data,USD,12-09-2025,{_DIVIDEND_DESCRIPTION} (Ordinary Dividend),9.87\n"
)
WITHHOLDING_TAX_SECTION = (
    "Withholding Tax,Header,Currency,Date,Description,Amount,Code\n"
    f"Withholding Tax,Data,USD,12-09-2025,{_DIVIDEND_DESCRIPTION} - US Tax,-2.47,\n"
)
INTEREST_SECTION = (
    "Interest,Header,Currency,Date,Description,Amount\n"
    "Interest,Data,USD,30-06-2025,USD Credit Interest for Jun-2025,4.31\n"
)
EUR_DEBIT_INTEREST_ROW = "Interest,Data,EUR,30-06-2025,EUR Debit Interest for Jun-2025,-1.09\n"
CHANGE_IN_NAV_SECTION_FIELDS = (
    ("Dividends", "9.87"),
    ("Withholding Tax", "-2.47"),
    ("Interest", "4.31"),
)

RE_EXPORT_HINT = "The file may have been edited or cut short; re-export the Activity Statement."


def absent_section_refusal(label: str, section: str) -> str:
    return (
        f"the Activity Statement's Change in NAV shows a nonzero {label!r} "
        f"amount, but the statement has no {section!r} section. Re-export "
        f"the Activity Statement with the {section!r} section included; "
        f"the run refuses rather than treat the section as empty."
    )


def change_in_nav_differs(label: str, rows: str) -> str:
    return (
        f"the Activity Statement's Change in NAV {label!r} amount (zero when "
        f"the line isn't printed) differs by more than 0.01 from the total of "
        f"{rows}. A row may be missing or changed. {RE_EXPORT_HINT}"
    )


def other_currency_refusal(label: str, section: str) -> str:
    return (
        f"the Activity Statement's {section!r} section has a row in a "
        f"currency other than the statement's base currency, so it can't be "
        f"checked against the Change in NAV {label!r} amount, which is in the "
        f"base currency. The run refuses rather than skip the check."
    )


def assert_no_amount(text: str, *amounts: str) -> None:
    for amount in amounts:
        assert amount.lstrip("-") not in text, amount


def assert_private_error(error: Exception, *amounts: str) -> None:
    for text in (str(error), repr(error), *map(repr, error.args), repr(vars(error))):
        assert_no_amount(text, *amounts)
    assert error.__cause__ is None
    assert error.__context__ is None


# The issue's repro prints all three fields; each also refuses alone. The
# first field checked is named.
MISSING_SECTION_CASES = {
    "all-three": (CHANGE_IN_NAV_SECTION_FIELDS, absent_section_refusal("Dividends", "Dividends")),
    "dividends": (
        CHANGE_IN_NAV_SECTION_FIELDS[:1],
        absent_section_refusal("Dividends", "Dividends"),
    ),
    "withholding": (
        CHANGE_IN_NAV_SECTION_FIELDS[1:2],
        absent_section_refusal("Withholding Tax", "Withholding Tax"),
    ),
    "interest": (CHANGE_IN_NAV_SECTION_FIELDS[2:], absent_section_refusal("Interest", "Interest")),
}
