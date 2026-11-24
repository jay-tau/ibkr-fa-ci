"""Privacy-safe warnings and all-mode leg reporting for Robinhood."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

import ibkr_fa.pipeline_robinhood_outputs as outputs
from ibkr_fa.compliance.foreign_tax_credit import BLANK_RELIEF_TAXPAYER_ID_WARNING
from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.fa_a3 import fa_a3_peak_blank_rows, name_of_entity_is_blank
from ibkr_fa.compute.form67 import (
    Form67Readiness,
    form67_override_gap,
    form67_row_readiness,
    form67_section_91_need,
)
from ibkr_fa.compute.robinhood_capital_gains import (
    RobinhoodCapitalGainsResult,
    RobinhoodSalePriceCheck,
)
from ibkr_fa.compute.robinhood_fa import (
    RobinhoodA3IdentityGap,
    RobinhoodFaManualKind,
    RobinhoodFaManualWork,
    RobinhoodFaResult,
)
from ibkr_fa.compute.robinhood_options import (
    ROBINHOOD_FNO_FEMA_WARNING,
    RobinhoodOptionReconstruction,
    RobinhoodOptionsResult,
    option_blocker_summary,
)
from ibkr_fa.compute.robinhood_options import (
    option_treatment_warnings as _option_treatment_warnings,
)
from ibkr_fa.compute.schedule_cg_table_f import (
    ScheduleCgTableFCase,
    table_f_loss_across_bands_reason,
)
from ibkr_fa.compute.schedule_fsi_relief import relief_row_lacks_taxpayer_id
from ibkr_fa.exceptions import relayed_failure
from ibkr_fa.invariants import require
from ibkr_fa.models import FaA3PeakBlankReason
from ibkr_fa.models.form67_entry import Form67Entry
from ibkr_fa.models.schedule_fsi_row import ScheduleFsiRow
from ibkr_fa.output._cleanup import OutputRemovalError
from ibkr_fa.output.fa_manual_fill import (
    FA_A3_ADDRESS_OF_ENTITY_COLUMN,
    FA_A3_NAME_OF_ENTITY_COLUMN,
    FA_A3_NATURE_OF_ENTITY_COLUMN,
    FA_A3_PEAK_BLANK_TEXT,
    FA_A3_PEAK_VALUE_COLUMN,
    FA_A3_ZIP_CODE_COLUMN,
    manual_fill_holdings,
)
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_robinhood_equity_prices import MOVE_HOLDING_CACHE_ASIDE
from ibkr_fa.pipeline_robinhood_models import (
    NO_RUN_OUTPUTS,
    RobinhoodAllReport,
    RobinhoodLegReport,
    RobinhoodLegStatus,
    error_leg,
)
from ibkr_fa.types import Isin, Ticker


def treatment_warnings(options: RobinhoodOptionReconstruction) -> tuple[str, ...]:
    return _option_treatment_warnings(options) if options.trades else ()


def option_warnings(options: RobinhoodOptionReconstruction) -> tuple[str, ...]:
    return (ROBINHOOD_FNO_FEMA_WARNING, *treatment_warnings(options)) if options.trades else ()


def capital_gains_warnings(
    result: RobinhoodCapitalGainsResult,
    *,
    filing: bool = False,
) -> tuple[str, ...]:
    warnings = []
    if filing and result.attributed_lots:
        warnings.append(
            "Robinhood foreign exchange-listed securities are reported in the "
            "Schedule CG field for assets other than unquoted shares. Section 50CA "
            "prescribed FMV was not computed; no CBDT guidance or binding ruling "
            "directly resolves this classification."
        )
    return (*warnings, *sale_price_warnings(result.sale_price_checks))


def sale_price_warnings(checks: Iterable[RobinhoodSalePriceCheck]) -> tuple[str, ...]:
    """The warning for sales whose exact Amount is outside the displayed price band."""
    failed = sorted(
        (
            check.event_reference
            for check in checks
            if not check.source_cash_within_display_price_band
        ),
        key=lambda reference: (reference.account_index, reference.event_ordinal),
    )
    if not failed:
        return ()
    references = ", ".join(
        f"{reference.account_index}:{reference.event_ordinal}" for reference in failed
    )
    return (
        f"{len(failed)} Robinhood equity sale event(s) had exact recorded Amount outside "
        "the displayed quantity x price precision band; exact Amount was retained "
        "unchanged and no fee was inferred. Internal account_index:event_ordinal "
        f"references (not account numbers): {references}.",
    )


def cg_manual_reasons(
    result: RobinhoodCapitalGainsResult,
    options: RobinhoodOptionsResult,
    *,
    writes_options_csv: bool,
) -> tuple[str, ...]:
    """The manual Schedule CG entry a section's blank Table F bands (#1360)
    or an option capital gain (#1322) leaves. Without ``writes_options_csv``
    the option reason names the command that writes the workpaper (#1470).

    Call it only once the filing workbook is written, so each section's
    Table F row can be built."""
    reasons = [
        table_f_loss_across_bands_reason(row, "schedule-cg.xlsx")
        for row, table_f in ((3, result.stcg.table_f), (5, result.ltcg.table_f))
        if table_f.case is ScheduleCgTableFCase.LOSS_ACROSS_BANDS
    ]
    if options.capital_gain_lines:
        workpaper = outputs.OPTIONS_FILENAMES[0]
        reason = (
            "Robinhood option capital gains need manual Schedule CG entry from "
            f"{workpaper}; schedule-cg.xlsx names their sections"
        )
        if not writes_options_csv:
            reason += (
                f". This command doesn't write {workpaper}: run robinhood-options with "
                "the same --input, --config, --output-dir, --financial-year and "
                "--itr-form to write it"
            )
        reasons.append(reason)
    return tuple(reasons)


def _with_option_blockers(reason: str, options: RobinhoodOptionsResult) -> str:
    """``reason``, then every option blocker, an open business position's
    included (#1554)."""
    if not options.blockers:
        return reason
    return f"{reason}; option blockers: {option_blocker_summary(options.blockers)}"


def option_manual_reasons(options: RobinhoodOptionsResult) -> tuple[str, ...]:
    """The options leg's manual reasons: the option schedules with any open
    business position (#1554), then a reason for each bucket whose Table F
    date bands the workpaper leaves blank (#1362). Call it only for a leg
    that needs manual work."""
    return (
        _with_option_blockers("manual option schedules are required", options),
        *(
            f"Schedule CG Table F row {bucket.table_f_row} needs its option date bands "
            f"entered by hand; {outputs.OPTIONS_FILENAMES[0]} leaves them blank"
            for bucket, table_f in options.table_f.items()
            if table_f.bands is None
        ),
    )


_A2_ZIP_ACTION = (
    "a2-zip: set [[robinhood.accounts]] zip to the institution's postal code "
    "and re-run, or fill the Table A2 ZIP Code by hand before filing"
)
_A3_NAME_ACTION = (
    "a3-identity: a Table A3 Name of entity is blank; set name to the issuer's legal "
    "name in the [[fa.entities]] entry for its ISIN and re-run, or fill it by hand "
    "before filing. Copy the review file's Address of entity and ZIP Code into address "
    "and zip too (the issuer's own if they are blank): an entry without address stops "
    "the next run until you verify the address it finds, and an available automatic "
    "address and ZIP take precedence unless prefer_configured_address = true"
)


# A matching cache is reused before any fetch, highs or not (#1476).
_SESSION_WITHOUT_HIGH_REMEDY = (
    f"{MOVE_HOLDING_CACHE_ASIDE}: a cached series is reused even without highs. Or set "
    '[schedule_fa] peak_basis = "close", or enter it by hand. '
    "A ticker override won't help."
)


def _a3_peak_actions(
    fa: RobinhoodFaResult, items: Iterable[RobinhoodFaManualWork]
) -> tuple[str, ...]:
    """One ``a3-peak`` action for each reason a Table A3 peak is blank, in
    reason order: its cause, the 1-based positions of the rows it blanks in
    ``fa.a3_rows``, the order every Schedule FA writer keeps (#1560), and its
    remedy (#1476)."""
    tasks = tuple(items)
    if not tasks:
        return ()
    for item in tasks:
        require(
            any(
                review.lot_id in item.lot_ids and review.row.peak_blank_reason is not None
                for review in fa.a3
            ),
            what="a Robinhood a3-peak task has no blank Table A3 peak",
        )
    actions = []
    for reason, rows in fa_a3_peak_blank_rows(fa.a3_rows):
        cause, remedy = FA_A3_PEAK_BLANK_TEXT[reason]
        if reason is FaA3PeakBlankReason.SESSION_WITHOUT_HIGH:
            remedy = _SESSION_WITHOUT_HIGH_REMEDY
        actions.append(
            f"a3-peak: the Table A3 {FA_A3_PEAK_VALUE_COLUMN} in row(s) "
            f"{', '.join(map(str, rows))} is blank because {cause}. {remedy}"
        )
    return tuple(actions)


_VERIFIED = "verified against its own filings"
_NO_ADDRESS = "no issuer address was found and no [[fa.entities]] entry gives one"
_A3_ADDRESS_REMEDY = (
    f"Set address and zip to the issuer's legal address and postal code, {_VERIFIED}, "
    "in an [[fa.entities]] entry for each ISIN and re-run, or fill them by hand before filing"
)
# An automatic address and ZIP win over a configured pair unless it is
# preferred, so setting zip alone could leave the cell blank (#1483).
_A3_ZIP_TEXT = (
    f"Table A3 {FA_A3_ZIP_CODE_COLUMN} is blank for these holdings because the issuer "
    f"address filed has no postal code. Set zip to the issuer's postal code, {_VERIFIED}, "
    "with the review file's Address of entity as address and prefer_configured_address = "
    "true, in the [[fa.entities]] entry for each ISIN and re-run, or fill it by hand before "
    "filing"
)
_A3_NATURE_TEXT = (
    f"Table A3 {FA_A3_NATURE_OF_ENTITY_COLUMN} is blank for these holdings because no legal "
    "form was found for the issuer and no [[fa.entities]] entry sets nature_of_entity. Set "
    f"nature_of_entity to the issuer's legal entity type, {_VERIFIED}, in the "
    "[[fa.entities]] entry for each ISIN, with the review file's Address of entity and ZIP "
    "Code copied into address and zip (the issuer's own if they are blank; an entry without "
    "address stops the next run until you verify the address it finds), and re-run, or fill "
    "it by hand before filing"
)
# A series ETF with an entry takes only that entry's cells (#793), so its
# remedy names all four; the automatic registrant name is "<trust> - <series>".
_A3_ETF_NAME_TEXT = (
    f"Table A3 {FA_A3_NAME_OF_ENTITY_COLUMN} is the broker's description, not the legal name, "
    "for these series ETFs. An ETF with an [[fa.entities]] entry takes only that entry's "
    "cells (ADR 0011), so set name (the registrant trust's legal name and the fund's series "
    "name), address and zip (the trust's legal address) and nature_of_entity in the entry for "
    "each ISIN, verified against the fund's own filings, and re-run, or fill them by hand "
    "before filing"
)


def _a3_identity_actions(items: Iterable[RobinhoodFaManualWork]) -> tuple[str, ...]:
    """One ``a3-identity`` action per cause of ``items``' identity gaps, each
    listing its holdings by symbol and ISIN, from the gaps compute carries
    (#1483). A blank address always comes with a blank ZIP: without an
    ``[[fa.entities]]`` entry or an automatic identity both cells are blank,
    and every entry or automatic identity that reaches a row has an address
    (#1490). Such a holding is listed once, under the address."""
    holdings: dict[RobinhoodA3IdentityGap, set[tuple[Ticker, Isin]]] = defaultdict(set)
    for item in items:
        require(bool(item.identity_gaps), what="a Robinhood a3-identity task has no identity gap")
        for gap in item.identity_gaps:
            holdings[gap.gap].add((gap.symbol, gap.isin))
    address = holdings[RobinhoodA3IdentityGap.ADDRESS]
    zip_code = holdings[RobinhoodA3IdentityGap.ZIP_CODE]
    require(
        address <= zip_code,
        what="a Robinhood Table A3 Address of entity is blank and its ZIP Code is not",
    )
    causes = (
        (_A3_ETF_NAME_TEXT, holdings[RobinhoodA3IdentityGap.ETF_NAME]),
        (
            f"Table A3 {FA_A3_ADDRESS_OF_ENTITY_COLUMN} and {FA_A3_ZIP_CODE_COLUMN} are blank "
            f"for these holdings because {_NO_ADDRESS}. {_A3_ADDRESS_REMEDY}",
            address,
        ),
        (_A3_ZIP_TEXT, zip_code - address),
        (_A3_NATURE_TEXT, holdings[RobinhoodA3IdentityGap.NATURE]),
    )
    return tuple(
        f"a3-identity: {text}:" + "".join(f"\n    - {name}" for name in manual_fill_holdings(group))
        for text, group in causes
        if group
    )


_OPTION_TABLE_D_ACTION = (
    "option-table-d: options in an account whose [[robinhood.option_classifications]] "
    'treatment is "capital-gains" are other capital assets for Schedule FA Table D, which '
    "ibkr-fa doesn't write. Enter each option contract the account held at any time in the "
    "calendar year, including any closed before 31 December, in Table D by hand before filing"
)


def fa_manual_reasons(fa: RobinhoodFaResult) -> tuple[str, ...]:
    """Each manual Schedule FA task's action: its kind, once per task. A blank
    Table A2 ZIP Code (#1358) gets one line naming the setting, however many
    accounts leave it blank. So does an identity task whose lots include a
    whitespace-only Table A3 Name of entity (#1369), however many securities,
    each reason a Table A3 peak is blank (#1476), however many lots, and a
    Table D option task, however many accounts (#1483). Each cause of every
    identity task's gaps, that blank-name task's included (#1490), gets one
    line listing its holdings (#1483). The other kinds never reach here: each
    comes with a blocker that refuses Schedule FA."""
    identity = [item for item in fa.manual_work if item.kind is RobinhoodFaManualKind.A3_IDENTITY]
    blank_name = {
        item
        for item in identity
        if any(
            name_of_entity_is_blank(review.row) for review in fa.a3 if review.lot_id in item.lot_ids
        )
    }
    # A blank name may be the task's only gap (#1369).
    gap_actions = _a3_identity_actions(
        item for item in identity if item not in blank_name or item.identity_gaps
    )
    peak_actions = _a3_peak_actions(
        fa, (item for item in fa.manual_work if item.kind is RobinhoodFaManualKind.A3_PEAK)
    )
    reasons: list[str] = []
    for item in fa.manual_work:
        if item.kind is RobinhoodFaManualKind.A2_ZIP:
            actions: tuple[str, ...] = (_A2_ZIP_ACTION,)
        elif item.kind is RobinhoodFaManualKind.A3_IDENTITY:
            actions = (_A3_NAME_ACTION, *gap_actions) if item in blank_name else gap_actions
        elif item.kind is RobinhoodFaManualKind.A3_PEAK:
            actions = peak_actions
        elif item.kind is RobinhoodFaManualKind.OPTION_TABLE_D:
            actions = (_OPTION_TABLE_D_ACTION,)
        else:
            reasons.append(item.kind.value)
            continue
        for action in actions:
            if action not in reasons:
                reasons.append(action)
    return tuple(reasons)


_FRACTIONAL_RATE_ACTION = (
    "form-67.csv files a fractional Rate(%) that the portal's CSV importer may blank; "
    "after upload, enter it by hand as the Form 67 warning says"
)


_FORM67_WITHHELD = (
    "one or more Form 67 entries lack Indian tax payable and credit, so form-67.csv is withheld"
)


def _form67_blank_reason(form67_entries: Iterable[Form67Entry]) -> str:
    """The BLANK Form 67 action. It offers an exact tax-payable override only
    for the rows one can clear, and names each other row with its gap (#1461).
    A no-DTAA row's override also needs the [form67.section_91] inputs, which
    the action then names too (#1469)."""
    checked = [
        (entry, form67_override_gap(entry))
        for entry in form67_entries
        if form67_row_readiness(entry).readiness is Form67Readiness.BLANK
    ]
    gaps = [gap for _entry, gap in checked if gap is not None]
    needs = [
        need
        for need in (form67_section_91_need(entry) for entry, gap in checked if gap is None)
        if need is not None
    ]
    if not gaps:
        reason = (
            f"{_FORM67_WITHHELD}; add an exact [[form67.tax_payable_overrides]] "
            "amount for each country and source of income and rerun, or enter Form 67 "
            "on the portal by hand using form-67-debug.csv as a reference"
        )
    else:
        reason = (
            f"{_FORM67_WITHHELD}. {' '.join(gaps)} An exact [[form67.tax_payable_overrides]] "
            f"amount won't clear {'this entry' if len(gaps) == 1 else 'these entries'}, so "
            "enter Form 67 on the portal by hand using form-67-debug.csv as a reference"
        )
        clearable = [
            f"{entry.iso_alpha2}/{entry.source_of_income.name}"
            for entry, gap in checked
            if gap is None
        ]
        if clearable:
            reason += (
                f"; for {', '.join(clearable)}, you can still add an exact "
                "[[form67.tax_payable_overrides]] amount and rerun first"
            )
    if needs:
        reason += f". {' '.join(needs)}"
    return reason


def income_manual_reasons(
    options: RobinhoodOptionsResult | None,
    form67_readiness: Form67Readiness,
    form67_entries: Iterable[Form67Entry],
    fsi_rows: Iterable[ScheduleFsiRow],
    *,
    form67_ready_with_fractional_rate: bool,
) -> tuple[str, ...]:
    reasons = []
    if options is None or options.fsi_manual_work_required:
        reason = "remaining option items require manual Schedule FSI completion"
        reasons.append(reason if options is None else _with_option_blockers(reason, options))
    if form67_readiness is Form67Readiness.BLANK:
        reasons.append(_form67_blank_reason(form67_entries))
    if form67_ready_with_fractional_rate:
        reasons.append(_FRACTIONAL_RATE_ACTION)
    if relief_row_lacks_taxpayer_id(fsi_rows):
        reasons.append(BLANK_RELIEF_TAXPAYER_ID_WARNING)
    return tuple(reasons)


def reason_with_warnings(reason: str, warnings: tuple[str, ...]) -> str:
    return " ".join((reason, *warnings))


def write_cg_leg(
    result: RobinhoodCapitalGainsResult | None,
    *,
    options: RobinhoodOptionsResult | None,
    failure: str | None,
    warnings: tuple[str, ...],
    output_dir: Path,
    format_id: str,
    financial_year: FinancialYear,
    itr_form: ItrForm,
    debug: bool,
    account_labels: dict[int, str],
    writer: Callable[..., tuple[Path, ...]],
) -> RobinhoodLegReport:
    """Write one all-mode CG leg while retaining its audit warnings.

    Without ``options`` the workbook can't tell which sections hold option
    capital gains, so the leg fails instead of writing it (#1322).
    """
    if result is None or failure is not None:
        return error_leg("equity-cg", failure or "unresolved CG blockers", warnings=warnings)
    if options is None:
        return error_leg(
            "equity-cg",
            "the option computation failed, so schedule-cg.xlsx can't say which "
            "sections need option capital gains entered by hand",
            warnings=warnings,
        )
    try:
        written = writer(
            result,
            options=options,
            output_dir=output_dir,
            format_id=format_id,
            financial_year=financial_year,
            itr_form=itr_form,
            debug=debug,
            account_labels=account_labels,
        )
    except Exception as exc:
        outputs.clear_leg(
            output_dir,
            outputs.CG_FILING_FILENAMES if debug else outputs.CG_FILENAMES,
        )
        if isinstance(exc, OutputRemovalError):
            # The writer could not remove its own output: the caller records
            # that as a cleanup failure.
            raise
        return error_leg(
            "equity-cg",
            relayed_failure(exc, "could not write the equity-cg outputs in", output_dir),
            warnings=warnings,
        )
    # robinhood-all's options leg writes the workpaper.
    reasons = cg_manual_reasons(result, options, writes_options_csv=True)
    return RobinhoodLegReport(
        "equity-cg",
        RobinhoodLegStatus.MANUAL if reasons else RobinhoodLegStatus.OK,
        written,
        reasons,
        warnings,
        run_outputs=NO_RUN_OUTPUTS,
    )


def without_outputs(report: RobinhoodAllReport, note: str) -> RobinhoodAllReport:
    """Drop every leg's output paths, adding ``note`` to each leg that had some."""
    return replace(
        report,
        legs=tuple(
            replace(leg, outputs=(), reasons=(*leg.reasons, note)) if leg.outputs else leg
            for leg in report.legs
        ),
    )
