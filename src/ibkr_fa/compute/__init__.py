"""Compute layer: pure transformations from parsed inputs to FA outputs.

Stages:

* :mod:`ibkr_fa.compute.lot_overlap` — filter parsed lots to those
  overlapping the target calendar year.
* :mod:`ibkr_fa.compute.initial_value` — INR conversion of cost
  basis at acquisition-date TTBR.
* :mod:`ibkr_fa.compute.closing_value` — INR conversion of
  Dec-31 position value at year-end TTBR.
* :mod:`ibkr_fa.compute.fa_a3_gross_proceeds` — INR conversion of
  proven gross sale consideration at December 31 TTBR; ``None`` when
  a closed lot supplies only net cash, or 0 for an open lot.
* :mod:`ibkr_fa.compute.dividends` — aggregate per-symbol dividend
  totals in INR for Schedule FA Table A3 column 10 ("Total gross
  amount paid/credited with respect to the holding").
* :mod:`ibkr_fa.compute.fa_a3` — compose the per-stage outputs into
  ``list[FaA3Row]`` ready for the writer.
* :mod:`ibkr_fa.compute.schedule_os` — classify dividends into the
  Schedule OS line bucket and the Indian-source excluded bucket
  (per-payment Rule 115 FX conversion); non-ordinary distributions
  refuse (#1310).
* :mod:`ibkr_fa.compute.schedule_fsi` — aggregate per-(country,
  head-of-income) totals for Schedule FSI from upstream Schedule
  OS / CG INR results (no FX work; reads already-converted INR).

Each stage is a pure function (no I/O, no FX-rate fetching) — its
inputs are parsed dataclasses and its outputs are equally typed
dataclasses. FX policies are passed in by the pipeline at call time.
"""

from __future__ import annotations

from ibkr_fa.compute.cg_section import (
    LTCG_SECTION_112_VS_112A_ADVISORY,
    STCG_SLAB_VS_111A_ADVISORY,
)
from ibkr_fa.compute.closing_value import closing_value_inr, closing_value_inr_for_lot
from ibkr_fa.compute.dividends import aggregate_dividends_by_symbol_inr
from ibkr_fa.compute.fa_a3 import build_fa_a3_rows
from ibkr_fa.compute.initial_value import initial_value_inr, initial_value_inr_for_lot
from ibkr_fa.compute.lot_overlap import CyOverlap, overlapping_lots_for_cy
from ibkr_fa.compute.schedule_os import (
    ScheduleOsComputeResult,
    ScheduleOsDividendLine,
    UnrecognisedDistributionTypeError,
    compute_schedule_os_dividend_lines,
)
from ibkr_fa.compute.schedule_os_interest import ScheduleOsInterestLine

__all__ = [
    "LTCG_SECTION_112_VS_112A_ADVISORY",
    "STCG_SLAB_VS_111A_ADVISORY",
    "CyOverlap",
    "ScheduleOsComputeResult",
    "ScheduleOsDividendLine",
    "ScheduleOsInterestLine",
    "UnrecognisedDistributionTypeError",
    "aggregate_dividends_by_symbol_inr",
    "build_fa_a3_rows",
    "closing_value_inr",
    "closing_value_inr_for_lot",
    "compute_schedule_os_dividend_lines",
    "initial_value_inr",
    "initial_value_inr_for_lot",
    "overlapping_lots_for_cy",
]
