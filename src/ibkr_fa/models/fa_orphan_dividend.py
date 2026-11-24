"""A dividend symbol that Schedule FA Table A3 could not attach to a holding (#142).

:func:`ibkr_fa.compute.fa_a3_dividends.classify_orphan_dividends` builds
these; :class:`~ibkr_fa.pipeline_fa_models.RunReport` carries them so the
CLI can point the user at Schedule OS / Schedule CG.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Literal

from ibkr_fa.invariants import require
from ibkr_fa.types import Ticker

# #142: orphan-dividend classification — a dividend symbol that was
# parsed and aggregated but never matched an FA-A3 output lot.
# Pre-#142 we emitted only the symbol with a one-size-fits-all
# "typical cause is Cash-In-Lieu / spinoff on never-held tickers"
# message. That misleads users on the most common real-world
# scenario: a held-over-ex-date / sold-before-pay-date sequence
# (e.g., shares bought 10/06, ex-div 10/20, sold 10/27, paid 11/14).
# Classifying via the trades stream lets us tailor the
# Schedule OS / Schedule CG advisory accurately.
_ORPHAN_DIVIDEND_CLASSIFICATIONS: Final[frozenset[str]] = frozenset(
    {"previously_held", "never_held"}
)


@dataclass(frozen=True, slots=True)
class OrphanDividendInfo:
    """A dividend payment dropped from FA A3 because no Dec-31 holding survived.

    Surfaced via :attr:`RunReport.orphan_dividends` so the CLI can
    render a classification-specific advisory instead of the
    pre-#142 one-size-fits-all "never acquired" message.

    Attributes:
        symbol: The ticker the orphan dividend was paid on.
        classification: ``"previously_held"`` if the symbol appears
            as a LONG ``OpenLot`` (still held at Dec 31) OR any
            ``ClosedLot`` in the in-period Trades. SHORT opens are
            excluded (T3 PR #170 P2: a short is a borrowed-and-
            resold liability, not an "owned and sold" holding;
            classifying a SHORT-only PIL recipient as
            previously_held would emit the misleading
            "sale belongs on Schedule CG" advisory).
            The held-over-ex-date / sold-before-pay-date case is
            the most common LONG real-world scenario for an orphan
            dividend (though in current pipeline that scenario
            actually produces a FA-A3 row, not an orphan — see PR
            #170 body's "Note on previously_held reachability").
            ``"never_held"`` if the symbol never appears in any
            Trades row, OR appears only as a SHORT open — typical
            cause is spinoff / true broker-error orphan /
            Payment-In-Lieu on borrowed shares.
        total_amount_inr: Sum of in-period INR-converted dividend
            payments for this symbol (rounded HALF_UP to whole
            rupees on render only; the stored value is full
            Decimal precision per the codebase convention).
        latest_payment_date: Date of the last in-period dividend
            payment on this symbol. Used in the CLI message to
            anchor the advisory date.
        last_sold_date: For ``"previously_held"`` orphans: the
            latest :attr:`ClosedLot.close_date_time` for this
            symbol in the parsed Trades. ``None`` for
            ``"never_held"`` orphans (no trade data to reference).
            After the T3 SHORT-filter fix (PR #170 P2),
            ``previously_held`` with ``last_sold_date=None`` is
            structurally unreachable in the current pipeline (any
            LONG open that's still held at Dec 31 becomes a FA-A3
            row, not an orphan); the permissive invariant is kept
            as defensive forward-compatibility.
    """

    symbol: Ticker
    classification: Literal["previously_held", "never_held"]
    total_amount_inr: Decimal
    latest_payment_date: date
    last_sold_date: date | None

    def __post_init__(self) -> None:
        require(
            self.classification in _ORPHAN_DIVIDEND_CLASSIFICATIONS,
            what=(
                "OrphanDividendInfo.classification must be one of 'previously_held' / 'never_held'"
            ),
            classification=self.classification,
        )
        # Cross-field invariant: never-held orphans by definition
        # have no Trades row, so last_sold_date must be None.
        # previously-held orphans always have at least one Trade
        # row (per the classification rule), so last_sold_date is
        # ALMOST always populated — but a row that's an OPEN-only
        # (never closed in-period) for the symbol would leave it
        # None. v1: accept both shapes; the CLI handles None
        # gracefully by suppressing the "last sold" clause.
        if self.classification == "never_held":
            require(
                self.last_sold_date is None,
                what=(
                    "OrphanDividendInfo: never_held orphans cannot have a "
                    "last_sold_date (the symbol never appeared in Trades)"
                ),
                symbol=str(self.symbol),
                last_sold_date=str(self.last_sold_date),
            )
