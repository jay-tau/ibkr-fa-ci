"""Emit the combined CBDT Schedule FA JSON fragment from the FA pipeline (#39).

Split out of :mod:`ibkr_fa.pipeline_fa`. The ``schedule-fa --format
cbdt-json-fragment`` path produces ONE file containing
both Table A2 (the custodial account) and Table A3 (the holdings), unlike the
CSV/xlsx path which writes an A3 file plus a separate A2 sibling. This module
holds that single emission point; :func:`ibkr_fa.pipeline_fa.run_fa_a3` calls it
when the format id matches.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Final

from ibkr_fa.compute.fa_a2 import FaA2AttestedPeak
from ibkr_fa.invariants import require
from ibkr_fa.output import RenderContext
from ibkr_fa.output._cleanup import ClaimedOutputCleanup, RunOutputs
from ibkr_fa.output._xlsx_contract import _whole_rupee_cell
from ibkr_fa.output.cbdt_json_fragment import (
    CbdtA2FragmentInput,
    CbdtA2Remedies,
    CbdtA3Remedies,
    CbdtScheduleFaFragmentInput,
    CbdtScheduleFaJsonWriter,
)
from ibkr_fa.output.portal_labels import is_forward_compat_fallback
from ibkr_fa.pipeline import _starting_ay_for_calendar_year
from ibkr_fa.pipeline_fa_a2 import build_fa_a2
from ibkr_fa.pipeline_fa_models import RunReport

if TYPE_CHECKING:
    from pathlib import Path

    from ibkr_fa.compliance.itr_form_selection import ItrForm
    from ibkr_fa.compute.lot_overlap import CyOverlap
    from ibkr_fa.config import UserConfig
    from ibkr_fa.fx.policy_fa import FaExactDatePolicy
    from ibkr_fa.models import Dividend, DividendEvidence, FaA3Row
    from ibkr_fa.models.account_info import AccountInfo
    from ibkr_fa.models.interest_credit import InterestCredit, InterestNonCredit
    from ibkr_fa.models.net_asset_value import NetAssetValueSummary
    from ibkr_fa.parser.format_detect import IbkrCsvFormat

# [[fa.entities]] supplies Table A3 entity values for IBKR and Robinhood alike.
# The gap names the row, never the ISIN (#1048): the review CSV or xlsx shows it.
_FA_ENTITY: Final[str] = "supply [[fa.entities]] with isin = that row's ISIN"
FA_ENTITIES_REMEDIES: Final[CbdtA3Remedies] = CbdtA3Remedies(
    address=lambda _isin: f"{_FA_ENTITY} and its address",
    zip_code=lambda _isin: (
        f"{_FA_ENTITY}, an address (copy the row's own Address of entity if it shows "
        "one; an entry with zip needs one) and its zip (the CBDT JSON requires a "
        "non-blank postal code even though the CSV allows it blank)"
    ),
    nature=lambda _isin: (
        f"{_FA_ENTITY}, an address and zip (copy the row's own Address of entity and "
        "ZIP Code if it shows them; an IBKR row's entry needs an address, and a "
        "Robinhood entry without one stops the run until you verify the address it "
        "finds) and its verified nature_of_entity (1-34 characters)"
    ),
)
_ATTESTED_PEAK_REASON: Final[str] = (
    "user-supplied: the statement has no daily account value to compute the peak from"
)


def _a2_remedies(calendar_year: int) -> CbdtA2Remedies:
    return CbdtA2Remedies(
        peak=(
            "supply [[schedule_fa.manual_values]] with "
            f"calendar_year = {calendar_year} and a2_peak_balance_inr (select the "
            "highest native-currency securities + cash value first, then apply that "
            "date's SBI TTBR; it cannot be derived from the statement)"
        ),
        address="supply [account.custodian].address",
        zip_code=(
            "supply [account.custodian].zip (the institution's postal code; the "
            "CBDT JSON requires it even though the CSV allows it blank)"
        ),
    )


def emit_cbdt_json_fragment(
    *,
    config: UserConfig,
    account_info: AccountInfo,
    nav: NetAssetValueSummary | None,
    dividend_evidence: DividendEvidence,
    input_format: IbkrCsvFormat,
    fx_policy: FaExactDatePolicy,
    calendar_year: int,
    overlap: CyOverlap,
    dividends: list[Dividend],
    interest_credits: list[InterestCredit],
    interest_non_credits: list[InterestNonCredit],
    rows: list[FaA3Row],
    output_path: Path,
    itr_form: ItrForm,
    account_id_cross_checked: bool,
    identity_supplied_from_fallback: frozenset[str],
    fx_policy_appropriate_for_ay: bool,
    short_lots_excluded: int,
) -> RunReport:
    """Emit the combined CBDT Schedule FA JSON fragment (A2 + A3) to ONE file.

    The A2 rows carry the ``[[schedule_fa.manual_values]]`` peak for the year as
    an attested value. Returns a RunReport flagged ``emitted_cbdt_json`` with
    EMPTY manual-fill tuples — a written JSON has no portal-cell gaps.

    Raises:
        CbdtJsonStructuralError: When the account binding isn't verified (a
            masked statement account, or an Account or Name from the config
            fallback) or another structural precondition fails.
        CbdtSchemaError: When a supplied value fails the CBDT schema gate.
        CbdtJsonIncompleteError: When a filed value is missing (e.g. the A2
            peak or a CBDT-required ZIP).
    """
    ay_start = _starting_ay_for_calendar_year(calendar_year)
    manual_value = config.schedule_fa.manual_value_for(calendar_year)
    a2_rows = build_fa_a2(
        config=config,
        account_info=account_info,
        nav=nav,
        dividend_evidence=dividend_evidence,
        input_format=input_format,
        fx_policy=fx_policy,
        calendar_year=calendar_year,
        overlap=overlap,
        dividends=dividends,
        interest_credits=interest_credits,
        interest_non_credits=interest_non_credits,
        peak=(
            None
            if manual_value is None
            else FaA2AttestedPeak(
                peak_inr=Decimal(manual_value.a2_peak_balance_inr),
                source=f"[[schedule_fa.manual_values]] calendar_year = {calendar_year}",
                reason=_ATTESTED_PEAK_REASON,
            )
        ),
    )
    # `account_id_cross_checked` is TRUE even when --accept-anonymised-statement
    # backfilled the account number from config (the cross-check then compares
    # config to itself). A filed JSON has no human reviewer, so require the
    # binding to be VERIFIED against the statement: refuse when the Account (or
    # Name) identity came from the config fallback. The RunReport keeps the RAW
    # flag (its __post_init__ invariant); only the writer sees the stricter one.
    account_verified = account_id_cross_checked and not (
        identity_supplied_from_fallback & {"Account", "Name"}
    )
    # A file at output_path before the write may not be this run's (the CLI's
    # provenance-checked preclear owns it): remove only the one this run publishes.
    with ClaimedOutputCleanup(output_path) as cleanup:
        CbdtScheduleFaJsonWriter.from_context(
            RenderContext(assessment_year_start=ay_start, itr_form=itr_form)
        ).write(
            CbdtScheduleFaFragmentInput(
                a2=tuple(
                    CbdtA2FragmentInput(row=row, account_id_cross_checked=account_verified)
                    for row in a2_rows or (None,)
                ),
                a3_rows=tuple(rows),
                calendar_year=calendar_year,
                assessment_year_start=ay_start,
                itr_form=itr_form,
                a2_remedies=_a2_remedies(calendar_year),
                a3_remedies=FA_ENTITIES_REMEDIES,
            ),
            output_path,
            on_staged=cleanup.claim,
        )
        require(
            output_path.exists(),
            what="pipeline post-write: the CBDT JSON writer must have produced a file",
            output_path=str(output_path),
        )
        return RunReport(
            rows_written=len(rows),
            output_path=output_path,
            manual_fill_columns=(),
            account_id=str(account_info.account_id),
            account_id_cross_checked=account_id_cross_checked,
            input_format=input_format,
            fx_policy_appropriate_for_ay=fx_policy_appropriate_for_ay,
            short_lots_excluded=short_lots_excluded,
            identity_supplied_from_fallback=identity_supplied_from_fallback,
            portal_labels_assessment_year_start=ay_start,
            portal_labels_used_forward_compat_fallback=is_forward_compat_fallback(ay_start),
            a2_rows=a2_rows,
            a2_manual_fill_columns=(),
            emitted_cbdt_json=True,
            # The writer refused a missing row, and one account's rows share a peak.
            cbdt_json_a2_peak_balance_inr=_whole_rupee_cell(
                a2_rows[0].filed.peak_balance_inr, field="Schedule FA A2 peak balance"
            ),
            cbdt_json_itr_form=itr_form,
            run_outputs=RunOutputs((), (cleanup,)),
        )
