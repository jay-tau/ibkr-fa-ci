"""Pipeline orchestrator for the Schedule FA Table A3 subcommand.

Extracted from :mod:`ibkr_fa.pipeline` by issue #155. This module owns
the Schedule FA Table A3 entry point; shared orchestration remains in
:mod:`ibkr_fa.pipeline`.
"""

from __future__ import annotations

import functools
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Final

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute import build_fa_a3_rows, overlapping_lots_for_cy
from ibkr_fa.compute.dividends import (
    us_source_dividend_count,
    us_source_withholding_tax_net_paid,
)
from ibkr_fa.compute.fa_a3 import fa_a3_manual_fill_gaps
from ibkr_fa.compute.fa_a3_dividends import classify_orphan_dividends, resolve_fa_a3_dividends
from ibkr_fa.compute.schedule_os import require_no_non_ordinary_distributions
from ibkr_fa.compute.source_coverage import (
    require_calendar_year_period,
    require_flex_calendar_year_period,
    require_open_lots_at_calendar_year_end,
)
from ibkr_fa.config import effective_anonymisation_flags, load_user_config
from ibkr_fa.exceptions import ConfigError, IbkrFaError
from ibkr_fa.fx.policy_fa import FaExactDatePolicy
from ibkr_fa.invariants import require
from ibkr_fa.models import Side, SourceBundle
from ibkr_fa.output import RenderContext, Subcommand, output_binding
from ibkr_fa.output._cleanup import RunOutputs, remove_previous_outputs
from ibkr_fa.output.cbdt_json_fragment import CbdtJsonStructuralError, CbdtScheduleFaJsonWriter
from ibkr_fa.output.fa_manual_fill import (  # noqa: F401 — MANUAL_FILL_PORTAL_COLUMNS re-exported
    MANUAL_FILL_PORTAL_COLUMNS,
    fa_a3_manual_fill_columns,
    manual_fill_holdings,
    peak_manual_fill_holdings,
)
from ibkr_fa.output.portal_labels import is_forward_compat_fallback
from ibkr_fa.parser.format_detect import IbkrCsvFormat, detect_ibkr_csv_format

# All five names come through pipeline.py, which defines _check_fx_policy_for_ay,
# aliases models.tax_year's starting_ay_for_calendar_year, and re-exports the
# two dispatchers and the account cross-check from pipeline_dispatch.py. The
# import binds each name in this module, and run_fa_a3 calls the dispatchers
# through these bindings, so a test that fakes a dispatcher must patch
# ``ibkr_fa.pipeline_fa._dispatch_*``. Patching ``ibkr_fa.pipeline._dispatch_*``
# leaves run_fa_a3 calling the real function.
from ibkr_fa.pipeline import (
    _check_fx_policy_for_ay,
    _cross_check_account_id,
    _dispatch_activity_statement,
    _dispatch_flex_query,
    _starting_ay_for_calendar_year,
)
from ibkr_fa.pipeline_fa_a2 import build_and_write_fa_a2, fa_a2_path
from ibkr_fa.pipeline_fa_cbdt_json import emit_cbdt_json_fragment

# Re-export the #155 model split for legacy imports.
from ibkr_fa.pipeline_fa_models import (  # noqa: F401 — re-export for compat
    OrphanDividendInfo,
    RunReport,
)
from ibkr_fa.pipeline_fa_prices import load_fa_price_series
from ibkr_fa.pipeline_ibkr_input import _require_ibkr_account
from ibkr_fa.prices.yahoo import DailyCloseFetcher

# Supported reporting years: OpenLot's report_date.year bounds, so a bad
# --calendar-year refuses before date(calendar_year, ...) can raise ValueError.
_MIN_CALENDAR_YEAR: Final[int] = 1990
_MAX_CALENDAR_YEAR: Final[int] = 2099


class UnsupportedCalendarYearError(IbkrFaError):
    """The Schedule FA calendar year is outside the supported range (#928)."""


def _calendar_year_income(bundle: SourceBundle, *, calendar_year: int) -> SourceBundle:
    """``bundle`` with only the dividend, withholding-tax and interest rows
    dated in ``calendar_year``.

    An Activity Statement may start before the year (#1288). Its rows dated
    in the year are the ones a statement for the year alone holds, so such a
    statement passes through unchanged, and so does a Flex Query, which has
    none of these rows.
    """
    return replace(
        bundle,
        dividends=[row for row in bundle.dividends if row.payment_date.year == calendar_year],
        withholding_tax=[
            row for row in bundle.withholding_tax if row.payment_date.year == calendar_year
        ],
        interest_credits=[
            row for row in bundle.interest_credits if row.credit_date.year == calendar_year
        ],
        interest_non_credits=[
            row for row in bundle.interest_non_credits if row.row_date.year == calendar_year
        ],
    )


def run_fa_a3(
    *,
    input_path: Path,
    config_path: Path,
    output_path: Path,
    calendar_year: int,
    format_id: str = "review-csv",
    itr_form: ItrForm | None = None,
    allow_masked_account: bool = False,
    suppress_cross_ay_warning: bool = False,
    accept_anonymised_statement: bool = False,
    fetch_prices: bool = False,
    price_cache_dir: Path | None = None,
    price_fetcher: DailyCloseFetcher | None = None,
    price_now: datetime | None = None,
) -> RunReport:
    """Run the v1 pipeline: IBKR CSV in, review CSV out.

    Pipeline stages, in order, once any earlier ``output_path`` and its A2
    sibling are removed (so a refusal leaves no output behind; the CBDT JSON
    fragment's target is cleared by the CLI instead) and ``calendar_year`` is
    checked against the supported range:

    1. ``load_user_config(config_path)`` — parses ``user.toml``.
    2. ``_check_fx_policy_for_ay(calendar_year, suppress_cross_ay_warning)``
       — evaluates whether the FX policy is verified appropriate for
       the target AY. Does NOT block; sets the
       :attr:`RunReport.fx_policy_appropriate_for_ay` field. CY <= 2025
       always passes (1961 Act applies via Section 536(2)(c));
       CY >= 2026 passes only when the user attests via
       ``suppress_cross_ay_warning=True``. See
       :mod:`ibkr_fa.pipeline` module-level commentary on
       ``_FIRST_AY_REQUIRING_2025_ACT_FRAMEWORK``.
    3. ``detect_ibkr_csv_format(input_path)`` — sniffs the first
       non-blank line to pick Flex Query vs Activity Statement.
    4. Dispatch to the matching parser layer (see
       :func:`_dispatch_activity_statement` /
       :func:`_dispatch_flex_query` for the returned ``SourceBundle`` and the
       per-section / fallback / closure-only (#162) semantics). For
       Activity Statement, the dispatcher also validates that the statement
       period ends on Dec 31 of the CY and starts on or before Jan 1, and
       checks the Dividends, Withholding Tax and Interest sections against the
       statement's ``Change in NAV`` over its whole period (#1279, #1324); the
       pipeline then keeps only the CY's dividend / withholding / interest
       rows (#1288). Flex Query returns empty dividend / withholding /
       interest lists (no Flex-side parsers yet — #138, #12).
    5. **Cross-check** ``AccountInfo.account_id`` against ``user.toml``'s
       ``[account].number`` (a masked source ID requires
       ``allow_masked_account=True`` to skip — see
       :func:`_cross_check_account_id`). Then
       :func:`~ibkr_fa.compute.schedule_os.require_no_non_ordinary_distributions`
       refuses a return of capital, long-term capital gain or cash in
       lieu row dated in the CY (#1310).
    6. ``overlapping_lots_for_cy(...)`` filters to CY-overlapping lots,
       then ``build_fa_a3_rows(...)`` composes per-lot :class:`FaA3Row`
       records under an :class:`FaExactDatePolicy`, and
       ``output_binding(Subcommand.FA, format_id)``'s writer serialises them to
       ``output_path``.

    Args:
        input_path: Path to the IBKR CSV. Format is auto-detected.
        config_path: Path to ``user.toml``.
        output_path: Destination path for the review CSV. Parent
            directory must exist (writer fails loud on a typo).
        calendar_year: The CY being reported (e.g., ``2025`` for
            AY 2026-27), from 1990 to 2099. Drives the pipeline's CY-boundary checks,
            the FA archive's overlap window, and the year-end TTBR
            lookup.
        format_id: Output format. Defaults to ``"review-csv"``
            (the only format shipped in v1). Future formats
            register themselves via :func:`ibkr_fa.output.register`
            and a catalog entry.
        itr_form: Target form for filing CSV/JSON; review outputs ignore it.
        allow_masked_account: Opt out of the account-ID cross-check
            when the source statement masks the account ID. Default
            ``False``. **Only** disables the safety net for
            statements where the masked-pattern is detected; a
            real account ID that just happens to mismatch still
            raises :class:`ConfigError`.
        suppress_cross_ay_warning: Suppress the AY 2027-28+ cross-AY
            verification warning. Default ``False`` (warning prints).
            Pass ``True`` only after independently verifying the
            FX policy is correct for the target AY — typically by
            consulting a CA who's checked the rule against the
            2025-Act framework. Has no effect for CY <= 2025
            (AY 2026-27 and earlier), where no warning fires.
        accept_anonymised_statement: Opt into the #141 fallback path
            for Activity Statement inputs where IBKR's privacy
            exporter has hidden the ``Name`` and/or ``Account`` rows
            from the Account Information section. Default ``False``
            (hard-fails on missing identity fields, preserving the
            pre-#141 safety net). When ``True``, the dispatcher
            backfills the absent fields from ``config.account.
            holder_name`` / ``config.account.number`` and records the
            substituted field names in
            :attr:`RunReport.identity_supplied_from_fallback`. No-op
            on present fields (statement-provided values always win),
            on Flex Query inputs (Flex's Account Information has no
            hide-at-export option), and on Activity Statement inputs
            whose Name + Account rows are both present.

    Returns:
        :class:`RunReport` summary the CLI uses to render success +
        warning output.

    Raises:
        UnsupportedCalendarYearError: If ``calendar_year`` is outside
            1990-2099. Raised once earlier outputs are removed, before
            ``config_path`` or ``input_path`` is read.
        FileNotFoundError: If ``input_path`` or ``config_path``
            doesn't exist.
        ConfigError: If ``user.toml`` is malformed, OR if the IBKR
            account number in the input CSV doesn't match the
            ``user.toml`` ``[account].number``, OR if the source
            statement masks the account ID and
            ``allow_masked_account`` is ``False``, OR if a held ISIN's
            ``[[fa.entities]]`` entry has no address (#1449). **Note**: the
            cross-AY FX-policy check at stage 2 does NOT raise; it
            sets :attr:`RunReport.fx_policy_appropriate_for_ay`
            instead, deliberately allowing CY 2026+ runs to proceed
            with an informational warning (changed in PR #132 after
            the gazette text confirmed the rate source is preserved
            under the 2025-Act framework).
        ParseError: If the input format isn't recognised, if any
            section is malformed, if required sections are missing,
            or (for Activity Statement) if the statement period
            doesn't span the full CY.
        ChangeInNavIncomeError: If the Activity Statement's ``Change in
            NAV`` contradicts its Dividends, Withholding Tax or Interest
            section, including a missing one (#1324). Raised at dispatch,
            before the account-number cross-check.
        NonOrdinaryDistributionError: If the statement's Dividends section
            has a return of capital, long-term capital gain or cash in lieu
            row dated in the CY (#1310).
        MissingFxRateError: If any lot's acquisition date or the
            target ``Dec 31`` lacks an SBI TTBR rate within the
            policy's carry-forward window.
        CountryCodeNotFoundError: If any lot's
            ``IssuerCountryCode`` is not in the vendored CBDT
            country-code table.
        TickerIsinCollisionError: If one symbol names more than one
            ISIN among the lots priced for the CY (see
            :func:`~ibkr_fa.pipeline_fa_prices.load_fa_price_series`).
        PriceCacheError: If a price cache or its lock exists but can't be
            used (see :func:`~ibkr_fa.prices.cache.load_or_fetch`).
        PriceSourceContradictionError: If a fetched price response has two
            bars on one date (see :func:`~ibkr_fa.prices.load_price_series`).
        YahooResponseFormatError: If a fetched Yahoo response doesn't have
            the accepted shape (see :func:`~ibkr_fa.prices.load_price_series`).
        OutputCollisionError: If ``output_path`` or its A2 sibling is
            ``input_path`` or ``config_path``.
        OutputRemovalError: If an earlier output, or the output of a run
            that fails during or after its write, cannot be removed.
        UnknownOutputFormatError: If ``format_id`` is not
            registered.
    """
    # The CLI's preclear owns a JSON target: it removes only a fragment ibkr-fa wrote.
    a2_path = fa_a2_path(output_path)
    if format_id != CbdtScheduleFaJsonWriter.format_id:
        remove_previous_outputs(
            (output_path, a2_path), (("--input", input_path), ("--config", config_path))
        )
    if not _MIN_CALENDAR_YEAR <= calendar_year <= _MAX_CALENDAR_YEAR:
        raise UnsupportedCalendarYearError(
            f"calendar year {calendar_year} is out of the supported range "
            f"[{_MIN_CALENDAR_YEAR}, {_MAX_CALENDAR_YEAR}]"
        )
    config = load_user_config(config_path)
    account_number = _require_ibkr_account(config)
    allow_masked_account, accept_anonymised_statement = effective_anonymisation_flags(
        config,
        allow_masked_account=allow_masked_account,
        accept_anonymised_statement=accept_anonymised_statement,
    )

    # Cross-AY warning evaluation — runs FIRST, before any parser /
    # format-detection work, keeping the AY-check logic adjacent to the
    # calendar_year argument it depends on. This no longer blocks (PR #132
    # Codex finding: Rule 206(1) + Rule 207(3)(b) of the Income-tax Rules,
    # 2026 preserve the SBI TTBR rate source; carry-forward fallback and
    # FA-specific date selection rest on CBDT FAQ / practice); it just
    # decides whether the run is "known-verified" or "needs user
    # verification".
    fx_policy_appropriate_for_ay = _check_fx_policy_for_ay(
        calendar_year=calendar_year,
        suppress_cross_ay_warning=suppress_cross_ay_warning,
    )

    input_format = detect_ibkr_csv_format(input_path)
    # Only Table A2 reads the NAV, and A2 is built only for a configured custodian.
    parse_nav = config.account.custodian is not None
    if input_format is IbkrCsvFormat.FLEX_QUERY:
        bundle = _dispatch_flex_query(
            input_path,
            snapshot_validator=functools.partial(
                require_open_lots_at_calendar_year_end, calendar_year=calendar_year
            ),
            period_validator=functools.partial(
                require_flex_calendar_year_period, calendar_year=calendar_year
            ),
            parse_nav=parse_nav,
        )
    else:
        bundle = _dispatch_activity_statement(
            input_path,
            period_validator=functools.partial(
                require_calendar_year_period, calendar_year=calendar_year
            ),
            fallback_account_holder_name=(
                config.account.holder_name if accept_anonymised_statement else None
            ),
            fallback_account_id=(account_number if accept_anonymised_statement else None),
            parse_nav=parse_nav,
            # #1324: Table A3's gross amount paid and Table A2's credits read
            # these sections, so a section Change in NAV shows missing refuses.
            check_income_against_nav=True,
        )
    bundle = _calendar_year_income(bundle, calendar_year=calendar_year)

    # Post-dispatch invariants (PR #125): the dispatcher contract is
    # that every emitted open lot's report_date matches the CY-end
    # snapshot we asked for. Verifies the parser layer respected the
    # CY constraint; a leak here would silently mis-attribute lots
    # to the wrong reporting year.
    cy_end = date(calendar_year, 12, 31)
    for lot in bundle.open_lots:
        require(
            lot.report_date == cy_end,
            what=(
                "pipeline post-dispatch: every OpenLot's report_date "
                "must equal Dec 31 of --calendar-year"
            ),
            symbol=lot.symbol,
            report_date=lot.report_date,
            cy_end=cy_end,
        )

    account_id_cross_checked = _cross_check_account_id(
        bundle.account_info,
        config_account_number=account_number,
        allow_masked_account=allow_masked_account,
    )
    # #1310: Table A3 would sum such a row into its gross amount paid.
    require_no_non_ordinary_distributions(bundle.dividends)

    overlap = overlapping_lots_for_cy(
        bundle.open_lots, bundle.closed_lots, calendar_year=calendar_year
    )
    # #1449: IBKR looks no issuer address up (ADR 0018), so an entry for a
    # held ISIN needs one. An entry for an ISIN IBKR doesn't hold is unused.
    held_isins = {lot.isin for lot in (*overlap.open_lots, *overlap.closed_lots)}
    for index, entity in enumerate(config.fa.entities):
        if entity.address is None and entity.isin in held_isins:
            raise ConfigError(
                field=f"fa.entities[{index}].address",
                message=(
                    "missing required field: IBKR Schedule FA looks up no issuer address, "
                    "so an entry for an IBKR holding needs one"
                ),
            )
    # Issue #109 — count short open lots that were excluded from
    # FA-A3 output by overlapping_lots_for_cy so the CLI can tell
    # the user. Computed by subtracting filtered LONG count from
    # input LONG+SHORT count (the date filter only drops lots with
    # open_date_time AFTER Dec 31 of CY, which the upstream
    # report-date check forbids — so anything dropped here is
    # dropped by the SHORT filter).
    short_lots_excluded = sum(1 for lot in bundle.open_lots if lot.side == Side.SHORT)

    max_carry = config.fx.max_carry_forward_days
    fx_policy = FaExactDatePolicy(max_carry_forward_days=max_carry)
    # Issue #31 + PR #137 zero-when-sure (see helper docstring).
    dividends_by_symbol_inr, zeroed_no_cy_divs = resolve_fa_a3_dividends(
        bundle.dividends,
        calendar_year=calendar_year,
        fx_policy=fx_policy,
        dividend_evidence=bundle.dividend_evidence,
    )

    # Issue #14: source per-ticker daily closes to compute each lot's FA
    # peak value. Disabled by default at the library level so pure-pipeline
    # tests stay network-free and deterministic; the CLI enables it
    # (``--no-fetch-prices`` to opt out). When disabled, only complete
    # integrity-checked caches are read.
    prices_by_ticker = load_fa_price_series(
        overlap,
        calendar_year=calendar_year,
        input_path=input_path,
        fetch_prices=fetch_prices,
        price_cache_dir=price_cache_dir,
        price_fetcher=price_fetcher,
        price_now=price_now,
        ticker_overrides=config.prices.ticker_overrides,
    )

    rows = build_fa_a3_rows(
        overlap,
        calendar_year=calendar_year,
        fx_policy=fx_policy,
        dividends_by_symbol_inr=dividends_by_symbol_inr,
        prices_by_ticker=prices_by_ticker,
        prices_fetched=fetch_prices,
        entity_overrides={entity.isin: entity for entity in config.fa.entities},
        peak_basis=config.schedule_fa.peak_basis,
        peak_currency=config.schedule_fa.peak_currency,
    )

    # #137 Tier-2 P3 + #142: dividend symbols no A3 row carries, classified so
    # the CLI advisory can point at Schedule OS (dividend) and Schedule CG (sale).
    orphan_dividends = classify_orphan_dividends(
        dividends_by_symbol_inr,
        a3_symbols={row.source_ticker for row in rows},
        dividends=bundle.dividends,
        open_lots=bundle.open_lots,
        # The year's sales only, as a statement for the year alone holds (#1288).
        closed_lots=overlap.closed_lots,
        calendar_year=calendar_year,
    )

    # Issue #31 + #14 + PR #137 Tier-1 P1.2: a column is listed as
    # manual-fill whenever ANY output row leaves it blank.
    a3_gaps = fa_a3_manual_fill_gaps(rows, dividends_resolved=dividends_by_symbol_inr is not None)

    # #39: the CBDT JSON fragment is ONE combined A2+A3 file (not the two-file
    # CSV/xlsx path) — emit or refuse here, skipping the writes below.
    if format_id == CbdtScheduleFaJsonWriter.format_id:
        if itr_form is None:
            raise CbdtJsonStructuralError(
                ["select ITR-2 or ITR-3 with --itr-form for the standalone fragment"]
            )
        return emit_cbdt_json_fragment(
            config=config,
            account_info=bundle.account_info,
            nav=bundle.net_asset_value,
            dividend_evidence=bundle.dividend_evidence,
            input_format=input_format,
            fx_policy=fx_policy,
            calendar_year=calendar_year,
            overlap=overlap,
            dividends=bundle.dividends,
            interest_credits=bundle.interest_credits,
            interest_non_credits=bundle.interest_non_credits,
            rows=rows,
            output_path=output_path,
            itr_form=itr_form,
            account_id_cross_checked=account_id_cross_checked,
            identity_supplied_from_fallback=bundle.identity_fallback_used,
            fx_policy_appropriate_for_ay=fx_policy_appropriate_for_ay,
            short_lots_excluded=short_lots_excluded,
        )
    run_outputs = RunOutputs((a2_path, output_path), ())
    # Write A2 first so its validation runs before either filing table is installed.
    with run_outputs.removed_on_failure():
        a2_rows, a2_output_path, a2_manual_fill_columns = build_and_write_fa_a2(
            config=config,
            account_info=bundle.account_info,
            nav=bundle.net_asset_value,
            dividend_evidence=bundle.dividend_evidence,
            input_format=input_format,
            fx_policy=fx_policy,
            calendar_year=calendar_year,
            overlap=overlap,
            dividends=bundle.dividends,
            interest_credits=bundle.interest_credits,
            interest_non_credits=bundle.interest_non_credits,
            output_path=output_path,
            format_id=format_id,
            itr_form=itr_form,
        )
        binding = output_binding(Subcommand.FA, format_id)
        binding.writer(
            RenderContext(
                assessment_year_start=_starting_ay_for_calendar_year(calendar_year),
                itr_form=itr_form,
            )
        ).write(rows, output_path)
        require(
            output_path.exists(),
            what="pipeline post-write: writer did not produce output_path",
            output_path=str(output_path),
        )

        return RunReport(
            rows_written=len(rows),
            output_path=output_path,
            manual_fill_columns=fa_a3_manual_fill_columns(a3_gaps),
            name_of_entity_manual_fill_holdings=manual_fill_holdings(a3_gaps.name_of_entity),
            address_of_entity_manual_fill_holdings=manual_fill_holdings(a3_gaps.address_of_entity),
            zip_code_manual_fill_holdings=manual_fill_holdings(a3_gaps.zip_code),
            nature_of_entity_manual_fill_holdings=manual_fill_holdings(a3_gaps.nature_of_entity),
            peak_manual_fill_holdings=peak_manual_fill_holdings(a3_gaps.peak),
            gross_proceeds_manual_fill_holdings=manual_fill_holdings(a3_gaps.gross_proceeds),
            account_id=str(bundle.account_info.account_id),
            account_id_cross_checked=account_id_cross_checked,
            input_format=input_format,
            fx_policy_appropriate_for_ay=fx_policy_appropriate_for_ay,
            short_lots_excluded=short_lots_excluded,
            orphan_dividends=orphan_dividends,
            withholding_tax_entries=len(bundle.withholding_tax),
            interest_credits_entries=len(bundle.interest_credits),
            interest_unproven_rows=tuple(
                row for row in bundle.interest_non_credits if row.group.unproven
            ),
            us_person=config.account.us_person,
            # Issue #145 advisory inputs.
            us_source_dividend_count=us_source_dividend_count(bundle.dividends),
            us_source_withholding_tax_net_paid=us_source_withholding_tax_net_paid(
                bundle.withholding_tax
            ),
            identity_supplied_from_fallback=bundle.identity_fallback_used,
            portal_labels_assessment_year_start=_starting_ay_for_calendar_year(calendar_year),
            portal_labels_used_forward_compat_fallback=is_forward_compat_fallback(
                _starting_ay_for_calendar_year(calendar_year)
            ),
            fa_gross_amount_zeroed_no_cy_dividends=zeroed_no_cy_divs,
            a2_rows=a2_rows,
            a2_output_path=a2_output_path,
            a2_manual_fill_columns=a2_manual_fill_columns,
            run_outputs=run_outputs,
        )
