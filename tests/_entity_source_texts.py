"""Failure texts the Robinhood entity sources (identity, GLEIF legal-address
and N-CEN fund) give, as several test modules check them, written out here
rather than imported so the pins stay independent. Each caller keeps its own
prefix, cache path and remedy.
"""

# The GLEIF legal-address request failure a test's unreachable GLEIF raises.
GLEIF_HTTP_503 = "GLEIF legal-address request failed with HTTP 503"
# The problem a GLEIF legal-address cache artifact with no or a wrong hash gives.
GLEIF_CACHE_INTEGRITY = "GLEIF legal-address cache artifact failed its integrity check"
# The warning for one ETF whose N-CEN source failed.
NCEN_SOURCE_FAILURE = (
    "ETF registrant identity remains manual for 1 securities (ncen-source-failure:1)"
)
# The identity snapshot's failure for a cache artifact that isn't a JSON object.
IDENTITY_CACHE_MALFORMED = "the identity source cache artifact is malformed"
# The identity snapshot's failure when the Nasdaq Trader directory can't be fetched.
NASDAQ_UNAVAILABLE = "the Nasdaq Trader traded-symbol directory could not be retrieved"
