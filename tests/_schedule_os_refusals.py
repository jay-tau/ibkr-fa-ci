"""The whole :class:`UnrecognisedDistributionTypeError` texts the Schedule OS
tests expect, one builder per violation (#1760)."""

from __future__ import annotations

# The error's text is fixed (#1047): it names the Dividends row and the rule,
# keeps the GitHub new-issue link, and asks the user to describe the wording
# rather than paste the row.
_DO_NOT_PASTE = (
    "describe its wording in your own words; don't paste the Description or the "
    "row's symbol, ISIN, date or amounts"
)
_NEW_ISSUE = (
    "Please file a new issue at https://github.com/jay-tau/ibkr-fa/issues/new/choose "
    "using the 'Atomic-unit issue' template; the title MUST be a conventional-commit "
    "subject"
)
_LOCATE_ROW = (
    "No Schedule OS output was written for this run. Locate the row in your IBKR "
    "Activity Statement CSV by its Dividends-section row index."
)
_NOT_A_NEW_KIND = (
    "Do NOT add the type to the allow / reject list — this is not a novel "
    "distribution kind, it is a parser-input shape mismatch."
)


def _row_clause(row_index: int | None) -> str:
    if row_index is None:
        return ""
    return f" (Dividends-section row index {row_index}; 0-based, so data row {row_index + 1})"


def unsupported_kind_message(row_index: int | None = None) -> str:
    return (
        "Unrecognised dividend distribution type in the Dividends section"
        f"{_row_clause(row_index)}: the row's distribution kind (the Description's "
        "leading words before the currency code) is not in the allow / reject list. "
        f"{_LOCATE_ROW} {_NEW_ISSUE} (suggested: 'feat(os): support a new dividend "
        f"distribution type'), and in the body {_DO_NOT_PASTE}."
    )


def missing_kind_prefix_message(row_index: int | None = None) -> str:
    return (
        "Malformed dividend description in the Dividends section"
        f"{_row_clause(row_index)}: the Description does not start with the expected "
        "SYMBOL(ISIN) <whitespace> structural prefix (matched by the parser's "
        "``_DESCRIPTION_PATTERN`` regex in "
        "``ibkr_fa.parser.activity_statement._description``, which tolerates optional "
        "whitespace between SYMBOL and the opening parenthesis to accept legacy / "
        "hand-normalised exports). The classifier cannot extract a distribution kind "
        f"safely. {_LOCATE_ROW} This typically indicates a parser bypass or an IBKR "
        f"Activity Statement format change. {_NEW_ISSUE} (suggested: 'fix(parser): "
        "handle dividend description without canonical SYMBOL(ISIN) structural "
        f"prefix'), and in the body {_DO_NOT_PASTE}. {_NOT_A_NEW_KIND}"
    )


def missing_usd_marker_message(row_index: int | None = None) -> str:
    return (
        "Malformed dividend type-slice in the Dividends section"
        f"{_row_clause(row_index)}: the Description's text after the SYMBOL(ISIN) "
        "prefix is missing the canonical ' USD ' currency marker, so the distribution "
        "kind cannot be classified safely (even though the whole stripped slice may "
        "coincide with an allow-listed kind like 'Cash Dividend'). The canonical IBKR "
        "format is '{kind} USD {amount} per Share [({qualifier})]'; a slice without "
        "' USD ' indicates a parser bypass, an upstream-schema drift, or a "
        "multi-currency dividend (multi-currency support is tracked at #52). "
        f"{_LOCATE_ROW} {_NEW_ISSUE} (suggested: 'fix(parser): handle dividend "
        'type-slice without canonical " USD " marker\'), and in the body '
        f"{_DO_NOT_PASTE}. {_NOT_A_NEW_KIND}"
    )


def malformed_type_slice_message(row_index: int | None = None) -> str:
    return (
        "Malformed dividend description in the Dividends section"
        f"{_row_clause(row_index)}: the row's Description column, after the SYMBOL(ISIN) "
        "prefix, isn't '<kind> USD <amount> per Share', optionally followed by "
        "' (Ordinary Dividend)', so its distribution kind can't be classified safely. "
        f"{_LOCATE_ROW} {_NEW_ISSUE} (suggested: 'fix(parser): handle a dividend "
        "description outside the supported grammar'), and in the body "
        f"{_DO_NOT_PASTE}. {_NOT_A_NEW_KIND}"
    )
