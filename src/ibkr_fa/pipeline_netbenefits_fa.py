"""``netbenefits-fa`` pipeline: Schedule FA Table A3 for a Fidelity ESPP holding.

Wires the already-merged NetBenefits parsers (#306-#312) into Table A3 output
for a Microsoft ESPP holding held via Fidelity NetBenefits, from a bundle
directory (``--input <dir>``) of open/closed-lots CSVs, trade-confirmation PDFs and
statement PDFs plus the ``[netbenefits]`` config block.

The supported year-end statement is UNPRICED, with no holdings table to
reconcile against, so Table A3 is built from the lots CSVs and the confirmation
PDFs, and the statements and confirmations then prove no other lot was held:

* **Holdings completeness** is proven from the lots: the Dec-31 position is the
  surviving open lot(s) PLUS any *gap-window* closed lot (acquired ON Dec-31 and
  sold the NEXT CY — still held at the year-end close), folded together and
  reconciled to the confirmation's ``shares`` / ``contribution``. A gap-window
  purchase fully sold the next CY (no surviving open lot) is reconstructed from
  the closed lot alone, never omitted; every OTHER CY-overlapping closed lot (a
  mid-year acquisition, or an intra-CY disposal) is refused (#I13). The
  closed-lots CSV is REQUIRED (its absence makes the Dec-31 quantity unprovable).
* **No other lot** (#1151): the lots exports carry no download date, so the
  statements, which must cover through ten days after Dec-31, and every
  confirmation in the bundle prove that the Dec-31 purchase is the only lot held
  in the CY (:mod:`ibkr_fa.compute.espp_lot_completeness`).
* **Lot identity** is cross-checked against the matching confirmation (purchase
  date, shares, contribution, price) and the config security (participant /
  symbol / CUSIP).
* **Initial value** is the confirmation's FMV at purchase converted under the
  Schedule FA acquisition-date convention, never the discounted contribution.
* **Closing value** is the Dec-31 close from :func:`resolve_dec31_close`
  (config override and/or Yahoo, cross-checked), additionally anchored to the
  confirmation FMV for a Dec-31 purchase.
* **Gross dividend** is a PROVEN ``0``: the lot is purchased on Dec-31, so its
  in-CY held window is empty. A lot acquired earlier is refused (its dividend
  needs the statement-income wiring of #I10).

It also emits Table A2 (the account itself, #I11) with proven-zero Dec-31 cash;
the CSV leaves the peak manual because the statements do not provide a complete
daily account-value series, and the CBDT JSON files the user's
``[[netbenefits.a2_peak_overrides]]`` value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.dec31_close import CloseSource
from ibkr_fa.compute.espp_lot_completeness import prove_dec31_purchase_is_the_only_lot
from ibkr_fa.compute.espp_perquisite import (
    EsppPerquisiteAdvisory,
    build_espp_perquisite_advisory,
)
from ibkr_fa.compute.espp_positions import (
    NetBenefitsFaError,
    check_confirmation_belongs_to_config,
    confirmation_label,
    reconstruct_espp_dec31_lots,
    resolve_espp_dec31_positions,
)
from ibkr_fa.compute.fa_a2 import fa_a2_manual_fill_gaps
from ibkr_fa.compute.fa_a3 import (
    build_fa_a3_rows,
    fa_a3_manual_fill_gaps,
    fa_a3_peak_blank_rows,
)
from ibkr_fa.compute.lot_overlap import overlapping_lots_for_cy
from ibkr_fa.compute.netbenefits_fa_a2 import (
    NetBenefitsA2Error,
    names_fidelity_stock_plan_services,
    prove_and_build_netbenefits_a2,
)
from ibkr_fa.config import (
    FaEntityOverride,
    FxConfig,
    NetBenefitsConfig,
    load_user_config,
)
from ibkr_fa.exceptions import MissingFxRateError, ParseError
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.fx.policy_fa import FaExactDatePolicy
from ibkr_fa.models import FaA3PeakBlankReason
from ibkr_fa.models.open_lot import REPORT_DATE_MAX_YEAR as _MAX_CALENDAR_YEAR
from ibkr_fa.models.open_lot import REPORT_DATE_MIN_YEAR as _MIN_CALENDAR_YEAR
from ibkr_fa.output._cleanup import (
    ClaimedOutputCleanup,
    RunOutputs,
    refuse_input_collisions,
    remove_previous_outputs,
    scan_inputs,
)
from ibkr_fa.output._xlsx_contract import _whole_rupee_cell
from ibkr_fa.output.cbdt_json_fragment import (
    CbdtScheduleFaJsonWriter,
    preclear_own_cbdt_fragment,
    refuse_failed_fragment_write,
)
from ibkr_fa.output.fa_manual_fill import fa_a2_manual_fill_columns, fa_a3_manual_fill_columns
from ibkr_fa.output.netbenefits_quicko_fa_xlsx import QUICKO_TEMPLATE_FILENAME
from ibkr_fa.parser.netbenefits.bundle import NetBenefitsBundle, load_bundle, load_statements
from ibkr_fa.parser.netbenefits.lots_csv import detect_fidelity_lots_csv
from ibkr_fa.pipeline import (
    FinancialYear,
    _starting_ay_for_calendar_year,
)
from ibkr_fa.pipeline_netbenefits_cbdt_json import (
    emit_netbenefits_cbdt_json,
    netbenefits_a2_peak,
)
from ibkr_fa.pipeline_netbenefits_fa_outputs import write_netbenefits_fa_outputs
from ibkr_fa.prices import load_price_series, resolve_override_symbols
from ibkr_fa.prices.cache import DEFAULT_PRICE_CACHE_DIR_NAME
from ibkr_fa.prices.yahoo import DailyCloseFetcher


@dataclass(frozen=True, slots=True)
class NetBenefitsFaRunReport:
    """Summary of one successful :func:`run_netbenefits_fa` call.

    Attributes:
        rows_written: Number of :class:`~ibkr_fa.models.FaA3Row` records written.
        output_path: The path the review CSV (or JSON fragment) was written to.
        security_name: The issuer name on the emitted rows (from the confirmation).
        close_sources: Per-lot Dec-31 close provenance, in output order — one
            ``(CloseSource, provenance)`` per row, so the CLI can flag a
            market (Yahoo) fallback.
        perquisite_advisories: One :class:`EsppPerquisiteAdvisory` per ESPP
            purchase in this FA run's financial year (advisory only).
        a2_output_path: Path the Table A2 row was written to.
        quicko_output_path: Path the Quicko Other Broker workbook was written to
            (only when requested).
        a2_manual_fill_columns: A2 portal columns left blank (manual-fill).
        a3_manual_fill_columns: A3 portal columns left blank (manual-fill), in
            portal order, from the shared A3 gap helpers (#1330).
        a3_peak_blank_rows: Each reason some A3 Peak value was left blank
            (manual-fill), in :class:`~ibkr_fa.models.FaA3PeakBlankReason`
            order, with the 1-based positions of the rows it blanks (#1549).
            Empty under the default close basis for a Dec-31 ESPP lot.
        quicko_zip_manual_fill_rows: Count of Quicko rows whose mandatory issuer
            ZIP Code was left blank because it was not configured.
        emitted_cbdt_json: True when the run wrote the CBDT JSON fragment
            (``--format cbdt-json-fragment``) instead of the review CSVs.
        cbdt_json_a2_peak_balance_inr: The A2 peak (INR) filed in the JSON
            fragment (from ``[[netbenefits.a2_peak_overrides]]``); None for CSV.
        run_outputs: Every output the run may have published, which the CLI
            attempts to remove if rendering or printing its report fails (#832, #1227).
        a2_institution_is_fidelity_sps: True when the A2 institution is
            Fidelity Stock Plan Services, which the CLI warns about (ADR 0021).
    """

    rows_written: int
    output_path: Path
    security_name: str
    close_sources: tuple[tuple[CloseSource, str], ...]
    perquisite_advisories: tuple[EsppPerquisiteAdvisory, ...] = ()
    a2_output_path: Path | None = None
    quicko_output_path: Path | None = None
    a2_manual_fill_columns: tuple[str, ...] = ()
    a3_manual_fill_columns: tuple[str, ...] = ()
    a3_peak_blank_rows: tuple[tuple[FaA3PeakBlankReason, tuple[int, ...]], ...] = ()
    quicko_zip_manual_fill_rows: int = 0
    emitted_cbdt_json: bool = False
    cbdt_json_a2_peak_balance_inr: int | None = None
    cbdt_json_itr_form: ItrForm | None = None
    run_outputs: RunOutputs = field(kw_only=True)
    a2_institution_is_fidelity_sps: bool = field(kw_only=True)


def netbenefits_inputs(input_dir: Path, config_path: Path) -> tuple[tuple[str, Path], ...]:
    """The config and every bundle file a NetBenefits run may read, for the output
    collision guard: each PDF (discovery classifies them all), each CSV that is,
    looks like (#911) or may be a lots export, and the Quicko template. Any other
    CSV, such as an earlier run's output saved inside the bundle, is not an input."""
    files = [
        input_dir,
        input_dir / QUICKO_TEMPLATE_FILENAME,
        *(
            path
            for path in scan_inputs(input_dir, "*", recursive=True)
            if path.suffix.lower() == ".pdf"
            or (path.suffix.lower() == ".csv" and _may_be_lots_csv(path))
        ),
    ]
    return (("--config", config_path), *(("--input", path) for path in files))


def _may_be_lots_csv(path: Path) -> bool:
    try:
        return detect_fidelity_lots_csv(path) is not None
    except ParseError:
        return True  # unreadable, so it cannot be ruled out: protect it


def _espp_perquisite_advisories(
    bundle: NetBenefitsBundle, cfg: NetBenefitsConfig, calendar_year: int, *, fx_cfg: FxConfig
) -> tuple[EsppPerquisiteAdvisory, ...]:
    """Build the Form-16 perquisite advisories for every confirmation whose purchase
    falls in the Indian FY of this FA run (the FY containing the calendar year's
    Dec-31, i.e. the same return the Schedule FA is filed with). Each confirmation
    is identity-checked against config; a missing TTBR degrades to a USD-only
    advisory (INR ``None``) rather than failing the FA run for a non-filed reminder.
    """
    fy = FinancialYear(start_year=calendar_year)
    rate_policy = TaxFxContext.for_fy(fy, fx_cfg).dividend
    advisories: list[EsppPerquisiteAdvisory] = []
    for purchase_date in sorted(bundle.confirmations_by_date):
        if not fy.start_date <= purchase_date <= fy.end_date:
            continue
        purchase = bundle.confirmations_by_date[purchase_date]
        check_confirmation_belongs_to_config(
            purchase, cfg, label=confirmation_label(purchase_date, bundle.confirmations_by_date)
        )
        try:
            # Rule 115(1), Explanation (2)(a) salary FX = preceding-month-end TTBR; its
            # specified date coincides with the (2)(e) dividend rule this policy
            # implements, so the rate is correct for the salary perquisite.
            rate: Decimal | None = rate_policy.rate_for(purchase_date)
        except MissingFxRateError:
            rate = None
        advisories.append(build_espp_perquisite_advisory(purchase, perquisite_inr_rate=rate))
    return tuple(advisories)


def _occupied(path: Path) -> bool:
    """Whether anything is at ``path``. Only a missing path reads as vacant; any
    other error while checking propagates."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _preclear_cbdt_json_fragment(output_path: Path) -> None:
    """Remove a prior ibkr-fa JSON fragment at ``output_path`` (so a refused
    re-run leaves nothing importable); refuse to clobber a file ibkr-fa did not
    author (its provenance marker is absent)."""
    if preclear_own_cbdt_fragment(output_path):
        return
    raise NetBenefitsFaError(
        f"refusing to overwrite {output_path} — it was not generated by ibkr-fa "
        "(no provenance marker), so it may be a hand-merged or unrelated file. "
        "Choose a different --output path, or remove it yourself."
    )


def netbenefits_fa_sibling_paths(output_path: Path) -> tuple[Path, Path, Path]:
    """The Table A2, Quicko workbook and legacy Table A2 paths beside the
    Table A3 ``output_path``: ``schedule-fa-a3.csv`` puts them at
    ``schedule-fa-a2.csv``, ``schedule-fa-quicko.xlsx`` and
    ``schedule-fa-a3-a2.csv``."""
    stem = output_path.stem.removesuffix("-a3")
    return (
        output_path.with_name(f"{stem}-a2{output_path.suffix}"),
        output_path.with_name(f"{stem}-quicko.xlsx"),
        output_path.with_stem(f"{output_path.stem}-a2"),
    )


def run_netbenefits_fa(  # noqa: PLR0915
    *,
    input_dir: Path,
    config_path: Path,
    output_path: Path,
    calendar_year: int,
    fetch_prices: bool,
    price_cache_dir: Path | None = None,
    price_fetcher: DailyCloseFetcher | None = None,
    price_now: datetime | None = None,
    format_id: str = "filing-csv",
    itr_form: ItrForm | None = None,
    quicko_workbook: bool = False,
) -> NetBenefitsFaRunReport:
    """Emit Schedule FA Tables A3 and A2 for the Fidelity ESPP holding.

    Args:
        input_dir: The NetBenefits bundle directory: the open- and closed-lots
            CSVs, the trade-confirmation PDFs and the statement PDFs, which are
            read and must cover through ten days after Dec-31.
        config_path: Path to ``user.toml`` (must contain a ``[netbenefits]`` block).
        output_path: Destination for Table A3 (parent must exist).
        calendar_year: The CY being reported (e.g. ``2025`` for AY 2026-27).
        fetch_prices: Whether to fetch Yahoo daily closes (peak value + the
            Dec-31 close cross-check / fallback).
        price_cache_dir: Price-cache directory; defaults to a
            ``.ibkr-fa-price-cache`` folder inside ``input_dir``.
        price_fetcher: Injectable fetcher (tests pass a stub; no network).
        price_now: Injectable fetch timestamp for deterministic tests.
        format_id: Output format; defaults to the portal-shaped filing CSV.
        itr_form: Target form for filing CSV/JSON; omitted filing CSV defaults
            to ITR-2, while CBDT JSON requires an explicit form.
        quicko_workbook: Also fill the Quicko template in ``input_dir``
            (``filing-csv`` only). Any failure to write it refuses the run.

    Raises:
        NetBenefitsFaError: Missing/duplicate input, an unmatched or unprovable
            lot, or a reconciliation mismatch.
        ClosingPriceError: The Dec-31 close cannot be resolved with certainty.
        IbkrFaError: Propagated from config loading / parsers / FX.
    """
    # Preclear only our own output, BEFORE any fallible work (config load, year and
    # format checks, discovery), so a refused run leaves no stale filing output;
    # writers themselves are build-first atomic. Never clear the config or a bundle
    # file: it would be deleted before it is read.
    emit_cbdt_json = format_id == CbdtScheduleFaJsonWriter.format_id
    a2_path, quicko_path, legacy_a2_path = netbenefits_fa_sibling_paths(output_path)
    targets = (
        (output_path,) if emit_cbdt_json else (output_path, a2_path, legacy_a2_path, quicko_path)
    )
    if emit_cbdt_json:
        refuse_input_collisions(targets, netbenefits_inputs(input_dir, config_path))
        _preclear_cbdt_json_fragment(output_path)
    else:
        remove_previous_outputs(targets, netbenefits_inputs(input_dir, config_path))
    if quicko_workbook and format_id != "filing-csv":
        raise NetBenefitsFaError(
            f"--quicko-workbook needs --format filing-csv; {format_id} never writes "
            "the Quicko workbook"
        )

    config = load_user_config(config_path)
    if config.netbenefits is None:
        raise NetBenefitsFaError(
            f"no [netbenefits] block in {config_path}; add one (see `ibkr-fa init`) to file the "
            f"Fidelity ESPP holding"
        )
    cfg = config.netbenefits
    # Validate OpenLot's year range here so date(...) cannot leak a raw ValueError
    # for a bad --calendar-year.
    if not _MIN_CALENDAR_YEAR <= calendar_year <= _MAX_CALENDAR_YEAR:
        raise NetBenefitsFaError(
            f"calendar year {calendar_year} is out of the supported range "
            f"[{_MIN_CALENDAR_YEAR}, {_MAX_CALENDAR_YEAR}]"
        )
    cy_end = date(calendar_year, 12, 31)
    if emit_cbdt_json and itr_form is None:
        raise NetBenefitsFaError("select ITR-2 or ITR-3 with --itr-form for CBDT JSON")
    bundle = load_bundle(input_dir)
    # Every lot-reconstruction refusal fires here, before any price fetch or cache I/O.
    cy_lots = reconstruct_espp_dec31_lots(
        bundle.open_lots, bundle.closed_lots, bundle.confirmations_by_date, cy_end=cy_end
    )

    symbol = cfg.security.instrument.symbol
    prices = load_price_series(
        {symbol: (date(calendar_year, 1, 1), cy_end)},
        cache_dir=price_cache_dir or (input_dir / DEFAULT_PRICE_CACHE_DIR_NAME),
        fetch=fetch_prices,
        fetcher=price_fetcher,
        now=price_now,
        fetch_symbols=resolve_override_symbols(
            ((symbol, cfg.security.instrument.isin),),
            {(o.ibkr_ticker, o.isin): o.yahoo_symbol for o in config.prices.ticker_overrides},
        ),
    )
    series = prices.get(symbol)

    fx_policy = FaExactDatePolicy(max_carry_forward_days=config.fx.max_carry_forward_days)
    resolved_lots = resolve_espp_dec31_positions(
        cy_lots,
        bundle.confirmations_by_date,
        cfg=cfg,
        calendar_year=calendar_year,
        series=series,
        peak_basis=config.schedule_fa.peak_basis,
        prices_fetched=fetch_prices,
    )
    statements = load_statements(input_dir, cfg.participant_no, include_year_end=True)
    prove_dec31_purchase_is_the_only_lot(
        statements,
        bundle.confirmations_by_date,
        # Every position is the Dec-31 purchase: the resolver refuses any other lot.
        dec31_purchase=resolved_lots.positions[0].purchase,
        cfg=cfg,
        calendar_year=calendar_year,
    )
    security_name = resolved_lots.security_name
    close_sources = tuple(
        (position.close.source, position.close.provenance) for position in resolved_lots.positions
    )

    # Closed lots are CG, not A3 holdings. The resolver rejects intra-CY disposals
    # and folds next-CY gap-window sales back into their Dec-31 open positions,
    # preserving a complete Dec-31 quantity.
    overlap = overlapping_lots_for_cy(
        [position.lot for position in resolved_lots.positions], [], calendar_year=calendar_year
    )
    sec = cfg.security
    rows = build_fa_a3_rows(
        overlap,
        calendar_year=calendar_year,
        fx_policy=fx_policy,
        nature_of_entity=sec.nature_of_entity,
        # Proven 0: every emitted lot is purchased on Dec-31 (empty in-CY held
        # window), so no in-CY dividend can have been received.
        dividends_by_symbol_inr={symbol: Decimal("0")},
        # The unified resolver values peak: under the default close basis a
        # Dec-31-acquired OPEN lot resolves peak == closing directly (no series
        # needed, exact even if the fetched Yahoo close differs by a cent); under
        # the "high" basis it uses the fetched Dec-31 intraday high (or fails
        # closed to manual-fill without a fetch).
        prices_by_ticker=prices,
        prices_fetched=fetch_prices,
        peak_basis=config.schedule_fa.peak_basis,
        peak_currency=config.schedule_fa.peak_currency,
        entity_overrides={
            sec.instrument.isin: FaEntityOverride(
                isin=sec.instrument.isin, address=sec.entity_address, zip_code=sec.entity_zip
            )
        },
    )
    # Build (and identity-validate) the perquisite advisories BEFORE writing any
    # output, so a mismatched in-FY confirmation refuses without leaving a
    # file-able Schedule FA CSV behind (fail-closed, like the rest of the tool).
    perquisite_advisories = _espp_perquisite_advisories(
        bundle, cfg, calendar_year, fx_cfg=config.fx
    )

    # --- Table A2 (the custodial account itself, #I11) ----------------------
    # Prove Dec-31 cash and build mandatory A2 before writing, so failure cannot
    # leave a fileable A3 behind.
    a2_identity = cfg.account.to_account_identity(participant_no=cfg.participant_no)
    a2_institution_is_fidelity_sps = names_fidelity_stock_plan_services(
        a2_identity.institution.name
    )
    try:
        a2 = prove_and_build_netbenefits_a2(
            identity=a2_identity,
            purchases=[position.purchase for position in resolved_lots.positions],
            statements=statements,
            calendar_year=calendar_year,
            a3_rows=rows,
            securities_usd=resolved_lots.securities_usd,
            ttbr_dec31=fx_policy.rate_for(cy_end),
            # Only the JSON files the config peak; the CSV leaves it manual-fill.
            peak=netbenefits_a2_peak(cfg, calendar_year) if emit_cbdt_json else None,
        )
    except NetBenefitsA2Error as exc:
        # The proof's refusals are fixed text naming statements, rows and config fields.
        raise NetBenefitsFaError(str(exc)) from None
    ay_start = _starting_ay_for_calendar_year(calendar_year)
    if emit_cbdt_json:
        assert itr_form is not None
        # The preclear ran before everything above. A file that has appeared since
        # is not this run's, and a successful write would replace it.
        if _occupied(output_path):
            raise NetBenefitsFaError(
                f"{output_path} already exists and this run did not write it; it was left untouched"
            )
        # One combined A2+A3 JSON fragment (not the two-CSV path) — emit or refuse.
        # A failure removes the destination only while it is the file this run staged.
        with ClaimedOutputCleanup(output_path) as cleanup:
            try:
                emit_netbenefits_cbdt_json(
                    rows,
                    a2.row,
                    cfg=cfg,
                    calendar_year=calendar_year,
                    output_path=output_path,
                    itr_form=itr_form,
                    on_staged=cleanup.claim,
                )
            except Exception as exc:
                refuse_failed_fragment_write(output_path, cleanup, exc, NetBenefitsFaError)
                # Refused before staging: a missing filed value is manual fill (exit 3).
                raise
            return NetBenefitsFaRunReport(
                rows_written=len(rows),
                output_path=output_path,
                security_name=security_name,
                close_sources=close_sources,
                perquisite_advisories=perquisite_advisories,
                emitted_cbdt_json=True,
                cbdt_json_a2_peak_balance_inr=_whole_rupee_cell(
                    a2.row.filed.peak_balance_inr, field="Schedule FA A2 peak balance"
                ),
                cbdt_json_itr_form=itr_form,
                run_outputs=RunOutputs((), (cleanup,)),
                a2_institution_is_fidelity_sps=a2_institution_is_fidelity_sps,
            )
    # If anything raises from the first write to the report, remove A3, A2 and
    # the requested Quicko workbook.
    run_outputs = RunOutputs(
        (output_path, a2_path, *((quicko_path,) if quicko_workbook else ())), ()
    )
    with run_outputs.removed_on_failure():
        quicko_output_path = write_netbenefits_fa_outputs(
            rows=rows,
            a2_row=a2.row,
            output_path=output_path,
            a2_path=a2_path,
            quicko_path=quicko_path,
            input_dir=input_dir,
            format_id=format_id,
            assessment_year_start=ay_start,
            itr_form=itr_form,
            quicko_workbook=quicko_workbook,
        )
        return NetBenefitsFaRunReport(
            rows_written=len(rows),
            output_path=output_path,
            security_name=security_name,
            close_sources=close_sources,
            perquisite_advisories=perquisite_advisories,
            a2_output_path=a2_path,
            quicko_output_path=quicko_output_path,
            a2_manual_fill_columns=fa_a2_manual_fill_columns(fa_a2_manual_fill_gaps((a2.row,))),
            # Dividends are the proven 0 the rows were built with.
            a3_manual_fill_columns=fa_a3_manual_fill_columns(
                fa_a3_manual_fill_gaps(rows, dividends_resolved=True)
            ),
            a3_peak_blank_rows=fa_a3_peak_blank_rows(rows),
            quicko_zip_manual_fill_rows=sum(1 for row in rows if not row.zip_code),
            run_outputs=run_outputs,
            a2_institution_is_fidelity_sps=a2_institution_is_fidelity_sps,
        )
