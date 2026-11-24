"""Schedule CG Table F: one section's gains in five transfer-date bands (#1360).

ITR-2 and ITR-3 Schedule CG part F, "Information about accrual/receipt of
capital gain", asks for each kind of capital gain in five date bands, the
bands of Schedule OS section 10
(:func:`~ibkr_fa.compute.schedule_os_periods.accrual_period_index`). Row 3
takes the short-term gains taxable at applicable rates, where the foreign
STCG of ITR-2 A5 and ITR-3 A6 lands, and row 5 the long-term gains taxable at
12.5%, where the section 112 LTCG of B8 and B9 lands. CBDT's Category A
validation rules refuse a return whose row 3 doesn't add up to Schedule BFLA
item 3v (ITR-2 rule 170; ITR-3 rule 426, item 5viii), or whose row 5 doesn't
add up to item 3vii (ITR-2 rule 578; ITR-3 rule 452, item 5x).

:func:`schedule_cg_table_f_row` builds one section's row from its gains, each
a :class:`TableFGain`, and the section's filed result R, which is what A5(e),
A6(g), B8(e) or B9(e) files. A gain's band is that of its transfer date, the
date that also sets its financial year and Rule 115 rate. A band's net is the
exact sum of its gains (:func:`~ibkr_fa.compute.exact_inr.checked_exact_sum`,
#1166, #1241); nothing is rounded before the split. The row follows the first
rule that applies:

(a) R is entered by hand (``result_inr`` is ``None``): no bands. Only a
    Robinhood option bucket past the CBDT bound does this (#1362); no
    Schedule CG section does (#1383).
(b) R is zero or less: five zeros, because the section adds nothing to
    Table F.
(c) No band nets a loss: the nets split to R
    (:func:`~ibkr_fa.compute.schedule_os_periods.split_whole_rupees`), so
    each band is its net's floor or one rupee more. R is the rounded
    consideration less the rounded cost, so it is within one rupee of the
    exact total of the nets and always fits.
(d) Exactly one band nets a gain: that band gets R and the others 0.
(e) Otherwise, a band nets a loss and two or more bands net gains: no bands.
    No source says which gain band a set-off loss lowers, and the choice
    changes section 234C interest, so the filer makes it.

The row is this source's alone, before any set-off against other sources'
gains or losses or brought-forward losses, which the tool never sees.
:attr:`ScheduleCgTableFRow.case` says which kind of row it is, so writers and
run reports don't work it out again.

The STCG and LTCG engine results expose their row as ``table_f``, built from
their lines by :func:`cg_line_gains`. Any other capital-gains source, such
as option lines, builds its :class:`TableFGain` values and calls
:func:`schedule_cg_table_f_row` itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Final, NamedTuple

from ibkr_fa.compute.exact_inr import checked_exact_sum
from ibkr_fa.compute.schedule_os_periods import accrual_period_index, split_whole_rupees
from ibkr_fa.invariants import require
from ibkr_fa.models.money import require_filed_amount, require_whole_rupees
from ibkr_fa.models.schedule_cg_ltcg_row import ScheduleCgLtcgLine
from ibkr_fa.models.schedule_cg_row import ScheduleCgStcgLine, exact_amount

__all__ = [
    "TABLE_F_BAND_COUNT",
    "ScheduleCgTableFCase",
    "ScheduleCgTableFRow",
    "TableFGain",
    "cg_line_gains",
    "schedule_cg_table_f_row",
    "table_f_loss_across_bands_reason",
]

#: The number of Table F date bands, (i) to (v).
TABLE_F_BAND_COUNT: Final[int] = 5
_ZERO: Final[Decimal] = Decimal("0")


class TableFGain(NamedTuple):
    """One disposal's input to :func:`schedule_cg_table_f_row`.

    Attributes:
        transfer_date: The disposal's transfer date, which picks its band.
        gain_inr: Its INR gain, negative for a loss.
        exact_gain_inr: The gain's exact rational where ``gain_inr`` rounds
            it (#1241), else ``None``.
    """

    transfer_date: date
    gain_inr: Decimal
    exact_gain_inr: Fraction | None


class ScheduleCgTableFCase(StrEnum):
    """Which kind of Table F row a section has.

    ``COMPUTED`` rows hold five bands, rules (b) to (d). The other two are
    blank: ``MANUAL_SECTION`` because the result is entered by hand, rule (a),
    which only a Robinhood option bucket past the CBDT bound builds (#1362),
    and ``LOSS_ACROSS_BANDS`` because a band nets a loss beside two or more
    gain bands, rule (e).
    """

    COMPUTED = "computed"
    MANUAL_SECTION = "manual-section"
    LOSS_ACROSS_BANDS = "loss-across-bands"


def table_f_loss_across_bands_reason(row: int, file_name: str) -> str:
    """Why Table F row ``row`` is blank in ``file_name`` for a
    ``LOSS_ACROSS_BANDS`` section.

    It names the row and the file, never a figure, date or symbol.
    """
    return (
        f"Schedule CG Table F row {row} needs its date bands entered by hand: a band "
        f"nets a loss and two or more bands net gains; {file_name} leaves them blank"
    )


@dataclass(frozen=True, slots=True)
class ScheduleCgTableFRow:
    """One section's Table F row (#1360).

    Attributes:
        case: Which rule built the row.
        bands: The five whole-rupee bands, (i) to (v), in a ``COMPUTED``
            row; ``None``, five blank cells, otherwise.

    Raises:
        InvariantError: At construction, if ``case`` isn't a
            :class:`ScheduleCgTableFCase`, the bands are present in a blank
            row or missing from a computed one, or a computed row doesn't
            have five bands that are each a non-negative whole-rupee
            ``Decimal`` within the CBDT bound.
    """

    case: ScheduleCgTableFCase
    bands: tuple[Decimal, ...] | None

    def __post_init__(self) -> None:
        require(
            isinstance(self.case, ScheduleCgTableFCase),
            what="ScheduleCgTableFRow.case must be a ScheduleCgTableFCase",
        )
        computed = self.case is ScheduleCgTableFCase.COMPUTED
        require(
            (self.bands is not None) is computed,
            what="ScheduleCgTableFRow.bands must be present exactly in a computed row",
        )
        if self.bands is None:
            return
        require(
            isinstance(self.bands, tuple)
            and len(self.bands) == TABLE_F_BAND_COUNT
            and all(isinstance(band, Decimal) and band.is_finite() for band in self.bands),
            what="ScheduleCgTableFRow.bands must be five finite Decimals",
        )
        for band in self.bands:
            require_filed_amount(band, what="ScheduleCgTableFRow band")
            require_whole_rupees(band, what="ScheduleCgTableFRow band")
            require(band >= 0, what="ScheduleCgTableFRow bands must not be negative")


def cg_line_gains(
    lines: Iterable[ScheduleCgStcgLine | ScheduleCgLtcgLine],
) -> Iterator[TableFGain]:
    """Each Schedule CG line's close date, gain and exact gain, in order."""
    return (
        TableFGain(line.close_date, line.realized_gain_inr, exact_amount(line, "realized_gain_inr"))
        for line in lines
    )


def _band_nets(gains: tuple[TableFGain, ...]) -> tuple[Decimal | Fraction, ...]:
    """Each band's exact net gain: every net a rational where any gain has one.

    As the portal totals do, a section with any exact gain adds every gain
    exactly (#1241), so the nets are all rationals or all exact Decimals.
    """
    terms: list[list[tuple[int, Decimal, Fraction | None]]] = [
        [] for _ in range(TABLE_F_BAND_COUNT)
    ]
    for position, gain in enumerate(gains, start=1):
        terms[accrual_period_index(gain.transfer_date)].append(
            (position, gain.gain_inr, gain.exact_gain_inr)
        )
    exact = any(gain.exact_gain_inr is not None for gain in gains)
    sums = (
        checked_exact_sum(
            band, at=lambda position: f"Schedule CG Table F, gain {position}", exact=exact
        )
        for band in terms
    )
    return tuple(total if rational is None else rational for total, rational in sums)


def schedule_cg_table_f_row(
    gains: Iterable[TableFGain], *, result_inr: Decimal | None
) -> ScheduleCgTableFRow:
    """One section's Table F row: its ``gains`` split by transfer date to ``result_inr``.

    ``result_inr`` is the section's filed whole-rupee result, or ``None`` when
    the filer enters it by hand. The module docstring gives rules (a) to (e).

    Raises:
        InvariantError: If a band's net can't be added exactly (#1166), or
            ``result_inr`` is positive but no band nets a gain or, with no
            band netting a loss, it isn't within one rupee per band of the
            nets' floors. Neither can happen when ``result_inr`` is the
            section's rounded consideration less its rounded cost.
    """
    if result_inr is None:
        return ScheduleCgTableFRow(ScheduleCgTableFCase.MANUAL_SECTION, None)
    if result_inr <= 0:
        return ScheduleCgTableFRow(ScheduleCgTableFCase.COMPUTED, (_ZERO,) * TABLE_F_BAND_COUNT)
    nets = _band_nets(tuple(gains))
    if all(net >= 0 for net in nets):
        bands = split_whole_rupees(
            nets,
            target=result_inr,
            what="Schedule CG Table F: the section result must fit its five date bands",
        )
        return ScheduleCgTableFRow(ScheduleCgTableFCase.COMPUTED, bands)
    gain_bands = [index for index, net in enumerate(nets) if net > 0]
    require(
        bool(gain_bands),
        what="Schedule CG Table F: a positive section result needs a date band with a net gain",
    )
    if len(gain_bands) > 1:
        return ScheduleCgTableFRow(ScheduleCgTableFCase.LOSS_ACROSS_BANDS, None)
    [gain_band] = gain_bands
    return ScheduleCgTableFRow(
        ScheduleCgTableFCase.COMPUTED,
        tuple(result_inr if index == gain_band else _ZERO for index in range(TABLE_F_BAND_COUNT)),
    )
