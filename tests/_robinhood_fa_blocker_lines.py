"""Robinhood Schedule FA blocker lines, written out for the tests that match
them, so no expected line comes from the renderer it checks (#1550, #1694,
#1701)."""

# The fixed line a Schedule FA blocker refusal starts with.
FA_UNRESOLVED = "Robinhood Schedule FA contains unresolved evidence blockers"
# The fixed line an equity inventory refusal starts with.
INVENTORY_INCOMPLETE = "Robinhood equity inventory evidence is incomplete"
REFERENCES = "Internal references (not account numbers): "
NOT_WRITTEN = "Robinhood Schedule FA isn't written while this stands"
BY_HAND = f"{NOT_WRITTEN}, so complete the whole Robinhood Schedule FA by hand"
REPORT_ROUTE = (
    f"{NOT_WRITTEN}: if the activity report misses a trade or transfer of that security, "
    "supply the complete report and re-run; otherwise complete the whole Robinhood "
    "Schedule FA by hand."
)
# An anchor-mismatch inventory blocker's line: fill ``references`` with
# REFERENCES and the blocker's statements.
ANCHOR = (
    "A monthly statement shows a different number of shares of a security than the "
    "activity report's trades and transfers leave in the account by the statement's last "
    "day, so the shares held can't be proved. {references}. " + REPORT_ROUTE
)
# The causes of an a3-closing and a credit-split blocker's lines, before
# their references and route.
CLOSING_CAUSE = (
    "Table A3's closing balance of these lots comes from the statement ending on 31 "
    "December, and it can't be used: that statement shows none of the security's shares "
    "or a different number than the lots hold (as when every share was bought on 31 "
    "December and settles in January), or the security's closing price for 31 December is "
    "missing from its price series or disagrees with the statement's total."
)
CREDIT_CAUSE = (
    "A dividend or Stock Lending credit in the calendar year can't be split among these "
    "lots: on its holding date the account that received it held none of them, which "
    "account held a moved lot isn't known, or the credit's share count differs from the "
    "shares the lots held there."
)
