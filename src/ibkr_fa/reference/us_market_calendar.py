"""Regular U.S. equity-market sessions for the supported calendar years.

A session is a Monday-to-Friday date that is not a listed full-day NYSE
closure. Early-close days are full sessions. Coverage is CY2025 plus
2024-12-25, so the session before 2025-01-02 resolves to 2024-12-31.

A ``False`` from :func:`is_us_market_session` is a known non-session in any
year: a weekend, or a listed closure. Everything else assumes every closure
of the year is listed, and an unlisted holiday reads as a session. So gate
a ``True`` from :func:`is_us_market_session`, and every other helper, on
:func:`supports_calendar_year`, unless reading an unlisted holiday as a
session can only refuse. The Schedule FA Table A3 close basis relies on
that: it needs a close on the session :func:`us_market_session_on_or_before`
returns, in any year, so an unlisted holiday leaves the peak blank (#939).

Sources for the closure dates (Intercontinental Exchange press releases):

* 2024-12-25: "NYSE Group Announces 2024, 2025 and 2026 Holiday and Early
  Closings Calendar", 2023-11-10,
  https://ir.theice.com/press/news-details/2023/NYSE-Group-Announces-2024-2025-and-2026-Holiday-and-Early-Closings-Calendar/default.aspx
* The ten 2025 holidays: "NYSE Group Announces 2025, 2026 and 2027 Holiday
  and Early Closings Calendar", 2024-11-08,
  https://ir.theice.com/press/news-details/2024/NYSE-Group-Announces-2025-2026-and-2027-Holiday-and-Early-Closings-Calendar/default.aspx
* 2025-01-09, the National Day of Mourning for former President Jimmy
  Carter: "The New York Stock Exchange Will Close Markets on January 9 to
  Honor the Passing of Former President Jimmy Carter on National Day of
  Mourning", 2024-12-30,
  https://ir.theice.com/press/news-details/2024/The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of-Mourning/default.aspx
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Final

# ponytail: CY2025 only; add the next year's closure set when a caller needs it.
_SUPPORTED_CALENDAR_YEAR: Final[int] = 2025
_WEEKEND_START: Final[int] = 5
_CLOSURES: Final[frozenset[date]] = frozenset(
    {
        date(2024, 12, 25),
        date(2025, 1, 1),
        date(2025, 1, 9),
        date(2025, 1, 20),
        date(2025, 2, 17),
        date(2025, 4, 18),
        date(2025, 5, 26),
        date(2025, 6, 19),
        date(2025, 7, 4),
        date(2025, 9, 1),
        date(2025, 11, 27),
        date(2025, 12, 25),
    }
)


def supports_calendar_year(year: int) -> bool:
    """Whether every closure in ``year`` is listed."""

    return year == _SUPPORTED_CALENDAR_YEAR


def is_us_market_session(day: date) -> bool:
    """Whether ``day`` is a regular U.S. market session."""

    return day.weekday() < _WEEKEND_START and day not in _CLOSURES


def us_market_sessions(start: date, end: date) -> tuple[date, ...]:
    """Return regular U.S. market sessions in ``[start, end]``."""

    if start > end:
        return ()
    sessions: list[date] = []
    day = start
    while day <= end:
        if is_us_market_session(day):
            sessions.append(day)
        day += timedelta(days=1)
    return tuple(sessions)


def previous_us_market_session(day: date) -> date:
    """Return the latest regular session strictly before ``day``."""

    candidate = day - timedelta(days=1)
    while not is_us_market_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


def us_market_session_on_or_before(day: date) -> date:
    """Return ``day`` if it is a session, else the latest session before it."""

    if is_us_market_session(day):
        return day
    return previous_us_market_session(day)
