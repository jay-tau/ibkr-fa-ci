"""The words #1521 and #1526 give the Robinhood entity-lookup remedies, as the
refusals print them, written out here rather than imported so the pins stay
independent.
"""

# How a refusal over an unusable lookup cache opens its remedy, by whether the
# run fetches: with fetching off, a folder moved aside is only a cache miss.
MOVE_ASIDE = {
    True: "Move that folder aside and re-run",
    False: (
        "Move that folder aside and re-run without --no-fetch-prices (with fetching off, the "
        "lookup then finds nothing)"
    ),
}
# #1526: the opening for a cache rebuilt from SEC when the run's
# robinhood.sec_user_agent is missing or would be refused.
SET_SEC_CONTACT = "set robinhood.sec_user_agent to a name and an email address"
MOVE_ASIDE_SET_CONTACT = {
    True: f"Move that folder aside, {SET_SEC_CONTACT}, and re-run",
    False: (
        f"Move that folder aside, {SET_SEC_CONTACT}, and re-run without --no-fetch-prices (with "
        "fetching off, the lookup then finds nothing)"
    ),
}
# How every lookup-skip remedy ends: the entry's own keys.
SET_IDENTITY_TOO = (
    "Set name, address (and zip, if the issuer has one) and nature_of_entity too in any such "
    "entry that leaves one out."
)
