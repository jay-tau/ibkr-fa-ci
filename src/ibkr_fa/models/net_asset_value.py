"""Parsed IBKR Activity Statement ``Net Asset Value`` summary.

Feeds Schedule FA **Table A2** (foreign custodial account = the IBKR account
itself, issue #18): Table A2's "Closing balance" column is the account's ending
Net Asset Value on the calendar-year close (Dec 31), reported in INR via the
SBI TTBR on that date (Rule 115). This model carries the broker-stated ending
NAV in the statement's BASE CURRENCY; the Schedule FA compute layer applies the
USD-only gate and the Dec-31 TTBR conversion.

"Never be wrong" stance: :attr:`closing_total` is populated ONLY when the
parser can determine it with certainty — the ``Net Asset Value`` section has a
single asset-class block with a ``Current Total`` column and an unambiguous
printed total. A ``Total`` row must agree with the sum of the asset-class
rows within the 0.01 tolerance (#1287). Any printed ``Change in NAV``
``Ending Value`` must be single, non-blank and agree with the NAV total
within that tolerance.
Without a ``Total`` row, the ``Ending Value`` is the printed total, and the
asset-class rows must all have a ``Current Total`` and sum to it within that
tolerance (#906). A statement whose figures disagree contradicts itself, so
the parser raises ``ParseError`` and no summary is built (#1309). When the
figure is missing or ambiguous,
:attr:`closing_total` is ``None`` and :attr:`refusal_reason` names why (a
:class:`NavRefusalReason`), so the A2 compute layer leaves the Closing-balance
cell manual-fill with that reason rather than filing an uncertain figure
(#1013).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from ibkr_fa.invariants import require


class NavRefusalReason(StrEnum):
    """Why the ending NAV could not be determined with certainty (#1013).

    One member per soft refusal in
    :func:`~ibkr_fa.parser.activity_statement.net_asset_value.parse_net_asset_value`,
    plus one for a Flex Query input, whose NAV isn't parsed. Figures that
    disagree raise ``ParseError`` instead, so no member names them (#1309).
    """

    # No Net Asset Value section, or no block in it with both an
    # ``Asset Class`` and a ``Current Total`` column.
    NO_ASSET_CLASS_BLOCK = "no-asset-class-block"
    # More than one such block.
    MULTIPLE_ASSET_CLASS_BLOCKS = "multiple-asset-class-blocks"
    # More than one ``Total`` row in the block.
    MULTIPLE_TOTAL_ROWS = "multiple-total-rows"
    # The ``Total`` row's ``Current Total`` is blank.
    BLANK_TOTAL = "blank-total"
    # No ``Total`` row and no asset-class rows.
    NO_ASSET_CLASS_ROWS = "no-asset-class-rows"
    # No ``Total`` row, and an asset-class row's ``Current Total`` is blank.
    BLANK_ASSET_CLASS_AMOUNT = "blank-asset-class-amount"
    # More than one ``Change in NAV`` ``Ending Value``.
    MULTIPLE_ENDING_VALUES = "multiple-ending-values"
    # No ``Total`` row and no ``Ending Value`` to check the asset-class sum.
    NO_ENDING_VALUE = "no-ending-value"
    # The ``Ending Value``'s ``Field Value`` is blank.
    BLANK_ENDING_VALUE = "blank-ending-value"
    # A Flex Query input: its Net Asset Value isn't parsed.
    FLEX_QUERY = "flex-query"


@dataclass(frozen=True, slots=True)
class NetAssetValueSummary:
    """Closing Net Asset Value for Schedule FA A2.

    Attributes:
        closing_total: The account's ending NAV in the statement's base
            currency, taken from the ``Net Asset Value`` section's
            ``Asset Class == "Total"`` row, ``Current Total`` column, which the
            asset-class rows must sum to within the 0.01 tolerance (or, when
            no ``Total`` row is present, the ``Change in NAV`` ``Ending
            Value``, which the asset-class rows must sum to within the 0.01
            tolerance; the sum itself is never filed). ``None`` when it cannot
            be determined with certainty —
            see :attr:`refusal_reason`. May be negative (a margined account can
            carry a negative NAV); the column is NOT sign-constrained.
        refusal_reason: Why :attr:`closing_total` is ``None``, as a
            :class:`NavRefusalReason` member. ``None`` exactly when
            :attr:`closing_total` is populated.
        cross_checked: ``True`` when a ``Change in NAV`` ``Ending Value`` was
            present AND agreed with the NAV total within tolerance. ``False``
            when no ``Change in NAV`` section was available to corroborate the
            figure (the NAV total is still the broker's stated total).
        asset_class_current_totals: The NAV asset-class breakdown's ``Current
            Total`` per non-``Total`` asset class (e.g. ``{"Cash": …, "Stock":
            …}``), in base currency. Period-end (Dec 31) figures — surfaced as a
            REFERENCE in the A2 peak manual-fill guidance (Rule 114F: the
            custodial-account peak/closing includes the cash held in it). Empty
            when no breakdown could be parsed.
    """

    closing_total: Decimal | None
    refusal_reason: NavRefusalReason | None
    cross_checked: bool = False
    asset_class_current_totals: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require(
            self.refusal_reason is None or isinstance(self.refusal_reason, NavRefusalReason),
            what=(
                "NetAssetValueSummary.refusal_reason must be None or a NavRefusalReason enum member"
            ),
            refusal_reason=self.refusal_reason,
        )
        require(
            (self.closing_total is None) != (self.refusal_reason is None),
            what=(
                "NetAssetValueSummary: exactly one of closing_total / "
                "refusal_reason must be set (a confident figure XOR a "
                "refusal reason)"
            ),
            closing_total=str(self.closing_total),
            refusal_reason=self.refusal_reason,
        )
        if self.closing_total is not None:
            require(
                self.closing_total.is_finite(),
                what="NetAssetValueSummary: closing_total must be a finite Decimal",
                closing_total=str(self.closing_total),
            )
