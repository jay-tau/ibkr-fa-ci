"""Emit the CBDT Schedule-FA JSON fragment for the NetBenefits ESPP account (#337).

The ``netbenefits-fa`` review CSV leaves the A2 peak balance as a manual-fill
column (the monthly Fidelity statements carry no daily account NAV). The JSON
path builds the A2 row with the ``[[netbenefits.a2_peak_overrides]]`` peak the
user supplies for the year (:func:`netbenefits_a2_peak`), then writes the SAME
A3 rows + that A2 row as a form-bound importable fragment through the shared
CBDT-JSON writer, with NetBenefits remedy text for every config-backed gap.

The A2 ``AccountNumber`` must be the NetBenefits ``participant_no``, which
:func:`run_netbenefits_fa` cross-checks against every confirmation and
statement before this is reached; any other number is refused as not
cross-checked.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Final

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.fa_a2 import FaA2AttestedPeak
from ibkr_fa.config import NetBenefitsConfig
from ibkr_fa.models.fa_a2_row import FaA2Row
from ibkr_fa.models.fa_a3_row import FaA3Row
from ibkr_fa.output import RenderContext
from ibkr_fa.output._cleanup import FileIdentity
from ibkr_fa.output.cbdt_json_fragment import (
    CbdtA2FragmentInput,
    CbdtA2Remedies,
    CbdtA3Remedies,
    CbdtScheduleFaFragmentInput,
    CbdtScheduleFaJsonWriter,
)
from ibkr_fa.pipeline import _starting_ay_for_calendar_year

_A3_REMEDIES: Final[CbdtA3Remedies] = CbdtA3Remedies(
    address=lambda _isin: "supply [netbenefits.security].entity_address",
    zip_code=lambda _isin: (
        "set [netbenefits.security].entity_zip (the CBDT JSON requires the issuer "
        "postal code even though the review CSV allows it blank)"
    ),
    nature=lambda _isin: "supply [netbenefits.security].nature_of_entity (1-34 characters)",
)


def netbenefits_a2_peak(cfg: NetBenefitsConfig, calendar_year: int) -> FaA2AttestedPeak | None:
    """The year's ``[[netbenefits.a2_peak_overrides]]`` peak as an attested A2
    value, or ``None`` when the user supplied none."""
    override = cfg.a2_peak_for(calendar_year)
    if override is None:
        return None
    return FaA2AttestedPeak(
        peak_inr=Decimal(override.peak_balance_inr),
        source=f"[[netbenefits.a2_peak_overrides]] calendar_year = {calendar_year}",
        reason=(
            "user-supplied: the monthly statements have no daily account value "
            "to compute the peak from"
        ),
    )


def emit_netbenefits_cbdt_json(
    rows: list[FaA3Row],
    a2_row: FaA2Row,
    *,
    cfg: NetBenefitsConfig,
    calendar_year: int,
    output_path: Path,
    itr_form: ItrForm,
    on_staged: Callable[[FileIdentity], None],
) -> None:
    """Write the CBDT Schedule-FA JSON fragment. ``on_staged`` receives the
    staged file's identity before it is published (see
    :meth:`CbdtScheduleFaJsonWriter.write`).

    Raises:
        CbdtJsonStructuralError: when the A2 row's identifier differs from
            ``participant_no``, or another structural precondition fails.
        CbdtSchemaError: when a supplied value fails the CBDT schema gate.
        CbdtJsonIncompleteError: when a filed value is missing (e.g. the A2
            peak or a CBDT-required ZIP); gap text names the NetBenefits config
            keys.
    """
    ay_start = _starting_ay_for_calendar_year(calendar_year)
    CbdtScheduleFaJsonWriter.from_context(
        RenderContext(assessment_year_start=ay_start, itr_form=itr_form)
    ).write(
        CbdtScheduleFaFragmentInput(
            a2=(
                CbdtA2FragmentInput(
                    row=a2_row,
                    account_id_cross_checked=a2_row.account_number == cfg.participant_no,
                ),
            ),
            a3_rows=tuple(rows),
            calendar_year=calendar_year,
            assessment_year_start=ay_start,
            itr_form=itr_form,
            a2_remedies=CbdtA2Remedies(
                peak=(
                    "add [[netbenefits.a2_peak_overrides]] with "
                    f"calendar_year = {calendar_year} and peak_balance_inr (select the "
                    "highest native-currency securities + cash value first, then apply "
                    "that date's TTBR; the monthly statements carry no daily NAV, so it "
                    "cannot be derived)"
                ),
                address="supply [netbenefits.account].address",
                zip_code=(
                    "set [netbenefits.account].zip (the CBDT JSON requires the "
                    "institution postal code even though the review CSV allows it blank)"
                ),
            ),
            a3_remedies=_A3_REMEDIES,
        ),
        output_path,
        on_staged=on_staged,
    )
