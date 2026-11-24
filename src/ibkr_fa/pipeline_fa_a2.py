"""Schedule FA Table A2 wiring — build + write IBKR custodial-account rows.

Extracted from :mod:`ibkr_fa.pipeline_fa` so the custodial-account table is
built apart from the A3 holdings. :func:`build_and_write_fa_a2` is the
single entry point the FA pipeline calls before writing the A3 rows; it is a
no-op unless ``[account.custodian]`` is configured.
"""

from __future__ import annotations

from pathlib import Path

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.fa_a2 import (
    FA_A2_CREDIT_NATURES,
    FaA2GrossEvidence,
    FaA2PeakInput,
    FaA2StatusError,
    build_fa_a2_row,
    build_gross_evidence,
    expand_fa_a2_nature_rows,
    fa_a2_closing_ttbr,
    fa_a2_manual_fill_gaps,
)
from ibkr_fa.compute.lot_overlap import CyOverlap
from ibkr_fa.config import UserConfig
from ibkr_fa.exceptions import ConfigError
from ibkr_fa.fx.policy_fa import FaExactDatePolicy
from ibkr_fa.invariants import require
from ibkr_fa.models import Dividend, DividendEvidence
from ibkr_fa.models.account_identity import (
    SIGNING_AUTHORITY_UNSUPPORTED,
    AccountIdentity,
    InstitutionIdentity,
)
from ibkr_fa.models.account_info import AccountInfo
from ibkr_fa.models.fa_a2_row import FaA2Row
from ibkr_fa.models.interest_credit import InterestCredit, InterestNonCredit
from ibkr_fa.models.net_asset_value import NetAssetValueSummary
from ibkr_fa.output import RenderContext, Subcommand, output_binding
from ibkr_fa.output.fa_manual_fill import fa_a2_manual_fill_columns
from ibkr_fa.parser.format_detect import IbkrCsvFormat
from ibkr_fa.pii import is_masked_account_id
from ibkr_fa.pipeline import _starting_ay_for_calendar_year


def build_fa_a2(
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
    peak: FaA2PeakInput | None = None,
) -> tuple[FaA2Row, ...]:
    """Build Schedule FA A2 nature rows, or none when custodian is unconfigured.

    This is the pure compute half of :func:`build_and_write_fa_a2` (no file
    write), so a caller that needs A2 rows WITHOUT the CSV/xlsx sibling — the
    CBDT JSON path, which emits A2 + A3 in one file — can reuse the same NAV /
    Dec-31 TTBR / gross-components preprocessing rather than calling
    :func:`build_fa_a2_row` directly (which, without that preprocessing, would
    refuse a confident USD NAV that has no rate, or yield a row with
    ``gross_amount_inr`` ``None`` and cause spurious downstream gaps). Config loading refuses a
    ``SigningAuthority`` status; one set in code raises ``ConfigError``
    (translated from the builder's
    :class:`~ibkr_fa.compute.fa_a2.FaA2StatusError`), as in the CSV path.
    ``peak`` is passed to :func:`build_fa_a2_row` (``None`` leaves it
    manual-fill). ``nav`` and ``dividend_evidence`` come from the source
    bundle; a configured custodian requires the dispatcher to have parsed
    the NAV (``parse_nav=True``).
    """
    custodian = config.account.custodian
    if custodian is None:
        return ()
    require(
        nav is not None,
        what="build_fa_a2: a configured custodian needs the bundle's Net Asset Value summary",
    )
    assert nav is not None  # narrowed for the type checker; require() raised above

    is_usd = account_info.base_currency == "USD"
    gross_evidence = (
        build_gross_evidence(
            dividends,
            interest_credits,
            overlap.closed_lots,
            interest_non_credits=interest_non_credits,
            calendar_year=calendar_year,
            fx_policy=fx_policy,
            # Fail closed: anything but proven absence counts as dividend activity.
            dividend_activity_present=dividend_evidence is not DividendEvidence.ABSENT,
        )
        if (is_usd and input_format is IbkrCsvFormat.ACTIVITY_STATEMENT)
        else FaA2GrossEvidence({}, FA_A2_CREDIT_NATURES)
    )
    closing_ttbr = fa_a2_closing_ttbr(
        nav,
        base_currency=account_info.base_currency,
        gross_evidence=gross_evidence,
        calendar_year=calendar_year,
        fx_policy=fx_policy,
    )

    identity = AccountIdentity(
        institution=InstitutionIdentity(
            name=custodian.institution_name,
            address=custodian.address,
            zip_code=custodian.zip_code,
            country_iso2=custodian.country_iso2,
        ),
        account_ref=str(config.account.require_number()),
        opening_date=config.account.require_opening_date(),
        status=config.account.require_status(),
        base_currency=account_info.base_currency,
        statement_masked_account_ref=is_masked_account_id(account_info.account_id),
    )
    try:
        base = build_fa_a2_row(
            identity=identity,
            nav=nav,
            closing_ttbr_dec31=closing_ttbr,
            gross_components_inr=gross_evidence.known_components,
            # "Cash" is IBKR's NAV asset-class label. The breakdown refuses a
            # blank, repeated or unreadable row, so no "Cash" key means the
            # statement printed no Cash row (or the NAV refused): no reference.
            period_end_cash_base_ccy=nav.asset_class_current_totals.get("Cash"),
            peak=peak,
        )
    except FaA2StatusError as exc:
        raise ConfigError(field="account.status", message=SIGNING_AUTHORITY_UNSUPPORTED) from exc
    return expand_fa_a2_nature_rows(base, gross_evidence)


def fa_a2_path(output_path: Path) -> Path:
    """Table A2's sibling of the A3 ``output_path``: ``<stem>-a2<suffix>``."""
    return output_path.with_name(f"{output_path.stem}-a2{output_path.suffix}")


def build_and_write_fa_a2(
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
    output_path: Path,
    format_id: str,
    itr_form: ItrForm | None = None,
) -> tuple[tuple[FaA2Row, ...], Path | None, tuple[str, ...]]:
    """Build + write Schedule FA Table A2 rows for the IBKR custodial account.

    Returns ``(rows, output_path, manual_fill_columns)``; a no-op
    ``((), None, ())`` unless ``[account.custodian]`` is configured (the
    institution identity is not in the statement). The A2 rows are written to a
    SEPARATE sibling file (``<output>-a2.<ext>``), so the A3 output is
    untouched. Closing balance = broker NAV x Dec-31 TTBR (USD-only in v1),
    manual-fill when the NAV or credits could not be determined; the peak is
    left manual-fill (only the CBDT JSON path files the config peak).

    Raises:
        MissingFxRateError: When the NAV or a gross credit needs a missing
            Dec-31 TTBR; no A2 file is written.
    """
    a2_path = fa_a2_path(output_path)
    rows = build_fa_a2(
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
    )
    if not rows:
        return (), None, ()
    output_binding(Subcommand.FA_A2, format_id).writer(
        RenderContext(
            assessment_year_start=_starting_ay_for_calendar_year(calendar_year), itr_form=itr_form
        )
    ).write(rows, a2_path)
    require(
        a2_path.exists(),
        what="pipeline post-write: the Table A2 writer must have produced a file",
        a2_output_path=str(a2_path),
    )
    return rows, a2_path, fa_a2_manual_fill_columns(fa_a2_manual_fill_gaps(rows))
