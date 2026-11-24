"""Robinhood refusal and action texts that more than one test module pins (#1677, #1689, #1693,
#1701, #1708).

Each is written once here, so a wording change is re-pinned in one place.
"""

from __future__ import annotations

from typing import Final

from ibkr_fa.compute.robinhood_options import RobinhoodOptionBlockerKind

CRYPTO_REFUSAL: Final = "Robinhood Crypto statements are not supported by the brokerage parser"
MIXED_FAMILIES_REFUSAL: Final = (
    "statement mixes the Robinhood Crypto and brokerage statement families"
)
TRANSFER_CONTRADICTION: Final = "resolved transfer quantity contradicts its raw displays"
REC_CONTRADICTION: Final = "security receipt resolution 0 contradicts the REC income treatment"
CUSIP_CONTRADICTION: Final = "statement CUSIP contradicts activity CUSIP"
PENDING_CASH_REFUSAL: Final = (
    "Robinhood reconciliation refused: trade-only unsettled cash disagrees with Pending Trades"
)
# A negative statement position's refusal; fill in the row's account index,
# section, page, row and column.
NEGATIVE_POSITION_REFUSAL: Final = (
    "Robinhood reconciliation refused: account_index={account_index}, statement_index=0, "
    "section '{section}', page {page}, row {row}, column '{column}': "
    "negative equity positions are unsupported"
)
# The statement activity and cash row signing refusals.
ACTIVITY_NEGATIVE_REFUSAL: Final = "statement activity debit/credit must be non-negative"
ACTIVITY_BOTH_REFUSAL: Final = "statement activity row populates both debit and credit"
CASH_NEGATIVE_REFUSAL: Final = "statement cash debit/credit must be non-negative"
CASH_BOTH_REFUSAL: Final = "statement cash row populates both debit and credit"
# The cash identity and pending trade lifecycle refusals.
CASH_IDENTITY_REFUSAL: Final = "combined cash identity does not reconcile"
PENDING_SETTLE_REFUSAL: Final = "pending trade does not settle in the next statement period"
PENDING_MATCH_REFUSAL: Final = "pending trades do not match next-period settlements"
# The statement parser's refusal for a malformed holdings total row.
MALFORMED_TOTAL_REFUSAL: Final = "holdings total row is malformed"
# The statement parser's refusal for a malformed option contract in an activity
# row's description.
MALFORMED_ACTIVITY_CONTRACT_REFUSAL: Final = "activity option contract is malformed"
CORRECTED_STATEMENT_MISMATCH: Final = (
    "corrected statement does not match a superseded account/period"
)
# What a refusal or warning prints before the activity events it names.
EVENT_REFERENCES: Final = "Internal account_index:event_ordinal references (not account numbers): "
# The fixed line an income run's Schedule OS refusal starts with.
OS_UNRESOLVED: Final = "Robinhood Schedule OS contains unresolved source evidence blockers"
# The fixed line of an income run whose FTC schedules stay blocked after
# Schedule OS completed.
FTC_OS_REMOVED: Final = (
    "FTC schedules remain blocked; Schedule OS is complete but was removed, so this "
    "failed run leaves no output"
)
# A lot-engine anchor drift's cause; fill ``moved`` with what the blocker's
# lots were taken by.
ANCHOR_DRIFT_CAUSE: Final = (
    "A monthly statement shows a quantity of a security that differs from the one replayed "
    "from the activity report, and the difference isn't explained by a proved transfer "
    "between the accounts, so the lots its {moved} took can't be matched exactly."
)
# The Massive reader's refusal for a fractional transaction count.
MASSIVE_COUNT_REFUSAL: Final = (
    "Massive aggregate response, results row 2, column n: the transaction count must be a "
    "finite, exactly integral JSON number"
)
# Where the Massive reader's located refusals for row 2 of the aggregate
# response, and for the contract response, start.
MASSIVE_ROW_TWO: Final = "Massive aggregate response, results row 2"
MASSIVE_CONTRACT: Final = "Massive contract response"
# The Massive reader's other refusals that the reader and CLI tests both pin.
MASSIVE_JSON_REFUSAL: Final = "Massive returned malformed JSON"
MASSIVE_CLOSE_REFUSAL: Final = (
    f"{MASSIVE_ROW_TWO}, column c: the close must be a finite JSON number"
)
MASSIVE_VOLUME_REFUSAL: Final = (
    f"{MASSIVE_ROW_TWO}, column v: the volume must be a finite JSON number"
)
MASSIVE_TIMESTAMP_REFUSAL: Final = (
    f"{MASSIVE_ROW_TWO}, column t: the timestamp must be a finite, exactly integral JSON "
    "number of milliseconds"
)
MASSIVE_TIMESTAMP_RANGE_REFUSAL: Final = (
    f"{MASSIVE_ROW_TWO}, column t: the timestamp must resolve to a representable New York date"
)
MASSIVE_ROW_REFUSAL: Final = f"{MASSIVE_ROW_TWO}: the row must be an object"
MASSIVE_STATUS_REFUSAL: Final = f"{MASSIVE_CONTRACT}, field status: the status must be OK"
MASSIVE_STRIKE_REFUSAL: Final = (
    f"{MASSIVE_CONTRACT}, results field strike_price: the strike must be a finite JSON number"
)
MASSIVE_MULTIPLIER_REFUSAL: Final = (
    f"{MASSIVE_CONTRACT}, results field shares_per_contract: the multiplier must be a finite "
    "JSON number"
)
MASSIVE_CORRECTION_REFUSAL: Final = (
    f"{MASSIVE_CONTRACT}, results field correction: the correction must be absent, null or "
    "finite numeric zero, excluding booleans"
)
# The equity close coverage refusal: the A2 peak compute raises it alone, and
# the price step follows it with its counts and the fix.
EQUITY_COVERAGE_REFUSAL: Final = "Robinhood equity close coverage is incomplete"
# The start of the remedy for a price series cached without the closes or
# highs a run needs.
PRICE_CACHE_MOVE: Final = (
    "Move each such holding's cache file, and any .lock beside it, out of the price cache "
    "directory (--price-cache-dir) and re-run with price fetching"
)
# The output folder refusal; a CLI run follows it with ": <folder>".
PRIVATE_OUTPUT_DIR_REFUSAL: Final = (
    "Robinhood output directory must be owned by the current user and private "
    "(use an owner-owned directory and run `chmod 700` on it, or use a filesystem "
    "that preserves POSIX modes)"
)
# The F&O warning that starts an option run's refusal and warnings.
FNO_FEMA_WARNING: Final = (
    "FEMA/LRS F&O warning: RBI's LRS Master Direction says remittances in the nature of "
    "margins or margin calls to overseas exchanges or counterparties are not allowed under "
    "LRS. ibkr-fa does not determine whether these trades were LRS-funded or otherwise "
    "FEMA-compliant; fema_risk_acknowledged = true enables tax reporting only after "
    "independent review."
)
# The entry an FY 2025-26 option run with no classification asks for, after
# FNO_FEMA_WARNING; fill ``accounts`` with the accounts it names.
OPTION_CLASSIFICATION_RECIPE: Final = (
    "Robinhood F&O requires an explicit account/FY [[robinhood.option_classifications]] "
    "entry for {accounts} with "
    'account_number = "<account number>", '
    "financial_year = 2025, "
    'treatment = "<capital-gains, speculative-business or non-speculative-business>", '
    "fema_risk_acknowledged = true, "
    'source = "<what you reviewed>", '
    'reason = "<why that treatment applies>"'
)
# The payer recipe an interest-payer blocker prints.
INTEREST_PAYER_RECIPE: Final = (
    "Robinhood interest has no reviewed US payer: FSI, TR and Form 67 are refused "
    "until each account with interest in the financial year has one "
    "[[robinhood.interest_payer_countries]] entry with "
    'account_number = "<account number>", '
    "financial_year = <FY start year, e.g. 2025 for 2025-26>, "
    'country_iso2 = "US", '
    'interest_kinds = [<each kind it earned: "brokerage", "program-bank">], '
    'source = "<what you reviewed>", '
    'reason = "<why every payer is a US entity>"'
)
# The income leg's action for a fractional Form 67 rate in form-67.csv (#1447).
FRACTIONAL_RATE_ACTION: Final = (
    "form-67.csv files a fractional Rate(%) that the portal's CSV importer may blank; "
    "after upload, enter it by hand as the Form 67 warning says"
)
# Each option blocker kind's message, so a changed message fails the tests.
OPTION_BLOCKER_MESSAGES: Final[dict[RobinhoodOptionBlockerKind, str]] = {
    RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY: (
        "an option contract is sold to close more than the activity report shows bought "
        "to open, so check that the report starts before the opening trades"
    ),
    RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION: (
        "an option position treated as business income is still open at the end of the "
        "financial year, and its closing stock isn't valued, so complete it by hand"
    ),
    RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY: (
        "an option contract has same-day trades whose order isn't known (opens and closes "
        "on one day, or fills at different prices), so match its lots by hand"
    ),
    RobinhoodOptionBlockerKind.POSITION_RECONCILIATION: (
        "an option position on a monthly statement doesn't match the quantity the activity "
        "report's trades give, so check that the report and the statements agree"
    ),
    RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE: (
        "an option position treated as capital gains was closed before 2024-07-23, and "
        "gains under the earlier holding-period rules aren't computed, so compute them by "
        "hand"
    ),
    RobinhoodOptionBlockerKind.PREMIUM_RECONCILIATION: (
        "an option trade's amount doesn't match its quantity times price times 100, so "
        "check that trade in the activity report"
    ),
    RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT: (
        "an option contract or statement position isn't a long call with a positive whole "
        "number of contracts and a market value of zero or more, so check its right, "
        "quantity and market value, and report an unsupported contract by hand"
    ),
    RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE: (
        "an option contract was still held at expiry or has activity after it, and "
        "expiry, exercise and assignment aren't computed, so report it by hand"
    ),
}
INDIA_REFUSAL: Final = (
    "InvariantError: FaA3Row.country_code must not be India (Table A3 is foreign assets only)"
)
