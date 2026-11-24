"""Pipeline dispatchers — Activity Statement + Flex Query parsing fan-out.

Extracted from :mod:`ibkr_fa.pipeline` (issue #155 splits the
2700+-line monolith into per-subcommand modules with a thin shared
core). This module owns the parser-layer dispatch logic shared by
every ``run_*`` entry-point function in
:mod:`ibkr_fa.pipeline_fa`, :mod:`ibkr_fa.pipeline_os`,
:mod:`ibkr_fa.pipeline_cg`, and :mod:`ibkr_fa.pipeline_cg_ltcg`:

* :func:`_dispatch_flex_query` — Flex Query CSV →
  :class:`~ibkr_fa.models.SourceBundle` of parsed records.
* :func:`_dispatch_activity_statement` — Activity Statement CSV →
  :class:`~ibkr_fa.models.SourceBundle` of parsed records, with
  caller-controlled required-section set and identity-fallback knobs.
* :func:`_cross_check_account_id` — common account-ID safety net.
* Helpers: :func:`_require_sections`, :func:`_require_activity_sections`,
  :func:`_activity_section_rows`, :func:`_parse_optional_multi_block_section`,
  :func:`_single_section`.
* Section-set constants:
  :data:`_FA_DEFAULT_REQUIRED_ACTIVITY_SECTIONS`,
  :data:`_OS_DEFAULT_REQUIRED_ACTIVITY_SECTIONS`,
  :data:`_CG_DEFAULT_REQUIRED_ACTIVITY_SECTIONS`.

Logic is byte-for-byte identical to the pre-extraction code.
:mod:`ibkr_fa.pipeline` re-exports the two dispatchers,
:func:`_cross_check_account_id` and the CG, FSI and OS section sets, so
``from ibkr_fa.pipeline import _dispatch_…`` and
``ibkr_fa.pipeline._dispatch_…`` keep working and resolve to the same
objects as the names here.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Final

from ibkr_fa.compute.lot_matching import match_fifo
from ibkr_fa.exceptions import ConfigError, ParseError
from ibkr_fa.invariants import invariant_error, require
from ibkr_fa.models import (
    AccountInfo,
    ClosedLot,
    Dividend,
    DividendEvidence,
    InterestCredit,
    InterestNonCredit,
    OpenLot,
    SourceBundle,
    WithholdingTax,
)
from ibkr_fa.models.net_asset_value import NavRefusalReason, NetAssetValueSummary
from ibkr_fa.models.source import FlexPeriod
from ibkr_fa.parser import (
    KnownSection,
    SectionRows,
    parse_account_info,
    parse_closed_lots,
    parse_open_positions,
    split_flex_query,
)
from ibkr_fa.parser.activity_statement import (
    KnownActivitySection,
    Section,
    build_dec31_close_prices,
    build_instrument_lookup,
    dividend_evidence,
    parse_activity_statement_account_info,
    parse_activity_statement_dividends,
    parse_activity_statement_interest,
    parse_activity_statement_trades,
    parse_activity_statement_withholding_tax,
    split_activity_statement_sections,
    validate_open_positions_values,
    validate_reconciliation,
)
from ibkr_fa.parser.activity_statement.change_in_nav_income import (
    check_income_against_change_in_nav,
)
from ibkr_fa.parser.activity_statement.net_asset_value import parse_net_asset_value
from ibkr_fa.parser.activity_statement.performance_summary import check_cost_adjustments
from ibkr_fa.parser.activity_statement.statement_period import (
    StatementPeriod,
    parse_statement_broker_names,
    parse_statement_period,
)
from ibkr_fa.pii import is_masked_account_id

# The Flex Trades section's LevelOfDetail of a closed-lot row.
_CLOSED_LOT_LEVEL: Final = "CLOSED_LOT"

# Required Activity-Statement sections for the FA-A3 pipeline. Hard-
# fails the dispatcher if any are absent. DIVIDENDS and
# WITHHOLDING_TAX are NOT in this set: they are conditionally parsed
# because IBKR omits empty sections from Activity Statement exports.
# (Schedule FA checks them against Change in NAV, as the income
# commands do: #1279, #1324.)
#
# Open Positions is NOT in the FA required set (#162): IBKR
# legitimately omits an empty Open Positions section from Activity
# Statement exports for calendar years where every position opened in
# the period was also fully closed in the period. The dispatcher
# accepts these closure-only exports.
_FA_DEFAULT_REQUIRED_ACTIVITY_SECTIONS: Final[frozenset[KnownActivitySection]] = frozenset(
    {
        KnownActivitySection.STATEMENT,
        KnownActivitySection.ACCOUNT_INFORMATION,
        KnownActivitySection.FINANCIAL_INSTRUMENT_INFORMATION,
        KnownActivitySection.TRADES,
    }
)

# Required Activity-Statement sections for the Schedule OS dispatch.
# Only STATEMENT + ACCOUNT_INFORMATION are mandatory — DIVIDENDS is
# Tier-2 conditional (a buy-and-hold account or one with no foreign
# dividend payments in the FY legitimately exports without a Dividends
# section). A missing Dividends, Withholding Tax or Interest section is
# accepted only when the statement's Change in NAV, if printed, doesn't
# show that income (#1279). FII / TRADES / OPEN_POSITIONS are not in this set: the
# OS path skips the FII + Trades + reconciliation block when TRADES
# is absent (see :func:`_dispatch_activity_statement`).
_OS_DEFAULT_REQUIRED_ACTIVITY_SECTIONS: Final[frozenset[KnownActivitySection]] = frozenset(
    {
        KnownActivitySection.STATEMENT,
        KnownActivitySection.ACCOUNT_INFORMATION,
    }
)

# Required Activity-Statement sections for the Schedule CG dispatch.
# Same set as the FA pipeline because the CG pathway depends on the
# FIFO-matched ClosedLot stream built from the Trades section, which
# in turn requires FII (instrument lookup) + TRADES. OPEN_POSITIONS
# is NOT required here for the same reason it isn't in the FA set
# (#162): a closure-only year has no surviving Dec-31 positions to
# summarise, but the closures themselves are the entire point of
# Schedule CG. DIVIDENDS / WITHHOLDING_TAX are not required (CG is
# gain-on-disposal, not income-on-payment).
_CG_DEFAULT_REQUIRED_ACTIVITY_SECTIONS: Final[frozenset[KnownActivitySection]] = frozenset(
    {
        KnownActivitySection.STATEMENT,
        KnownActivitySection.ACCOUNT_INFORMATION,
        KnownActivitySection.FINANCIAL_INSTRUMENT_INFORMATION,
        KnownActivitySection.TRADES,
    }
)

# Required Activity-Statement sections for the Schedule FSI dispatch.
# FSI is a multi-source aggregator: it consumes foreign-source dividends
# (foreign-source details from Schedule OS row 1a(i)), broker credit-interest (Schedule OS row
# 1b(ix)), short-term + long-term capital gains (Schedule CG STCG +
# LTCG), and withholding tax (Dividends-section WHT) into per-(country,
# head-of-income) FSI rows. The strictest sub-pipeline's required set
# applies — Schedule CG's FII + TRADES requirement carries through here
# because CG is the most-likely-to-be-non-empty source for an account
# with any FY trading activity. Dividends / WithholdingTax / Interest
# are Tier-2 conditional (parsed if present, absent → empty bucket,
# unless the statement's Change in NAV shows that income, #1279).
#
# Aliased to the CG constant: byte-for-byte equality is intentional
# (the strictest sub-pipeline's set IS the CG set). If FSI ever
# diverges (e.g., a follow-up issue relaxes the TRADES requirement
# for dividend-only exports), redefine this as a separate frozenset
# at that time. (T4 r1 #3 — make the intentional equality explicit.)
#
# v1 limitation (#35 step 6/7): an account with foreign dividends but
# no Trades section (pure dividend-only export) cannot generate
# Schedule FSI via this pipeline. Workaround: run `schedule-os` for
# the dividend totals and manually transcribe them into the portal's
# Schedule FSI cells; the per-row FSI portal contract is per-(country,
# head), so manual filling from OS output is straightforward when only
# dividends are present. A follow-up issue tracks relaxing this
# requirement (#TBD: opportunistic CG parsing for FSI).
_FSI_DEFAULT_REQUIRED_ACTIVITY_SECTIONS: Final[frozenset[KnownActivitySection]] = (
    _CG_DEFAULT_REQUIRED_ACTIVITY_SECTIONS
)


def _refuse_missing_sections(
    missing: list[str], *, noun: str, remedy: str, input_path: Path
) -> None:
    """Raise one :class:`ParseError` naming every section in ``missing``, in order (#784).

    One missing section keeps the single-section wording; ``noun`` is the
    singular ("section", "Activity Statement section").
    """
    if not missing:
        return
    if len(missing) == 1:
        subject, these = f"{noun} {missing[0]!r}", "this section"
    else:
        subject, these = f"{noun}s {', '.join(map(repr, missing))}", "these sections"
    raise ParseError(
        section=", ".join(missing),
        row_index=0,
        column=None,
        message=f"required {subject} not found in {input_path}. {remedy} with {these} enabled.",
        raw_value="",
    )


def _require_sections(
    sections: dict, required: tuple[KnownSection, ...], *, input_path: Path
) -> None:
    _refuse_missing_sections(
        [section.value for section in required if section not in sections],
        noun="section",
        remedy="Re-run the Flex Query",
        input_path=input_path,
    )


def _require_activity_sections(
    sections: dict[KnownActivitySection, list[Section]],
    required: frozenset[KnownActivitySection],
    *,
    input_path: Path,
) -> None:
    # Declaration order: a frozenset of StrEnum members iterates in
    # PYTHONHASHSEED order, so the error would change between runs.
    missing = sorted(
        (section for section in required if section not in sections or not sections[section]),
        key=list(KnownActivitySection).index,
    )
    _refuse_missing_sections(
        [section.value for section in missing],
        noun="Activity Statement section",
        remedy="Re-export the Activity Statement",
        input_path=input_path,
    )


def _activity_section_rows(
    sections: dict[KnownActivitySection, list[Section]],
    section: KnownActivitySection,
) -> list[dict[str, str]]:
    """Flatten every block for an Activity Statement section in input order.

    Each row's file line is lost here, so a section's printed totals are
    checked before this: Trades' by the splitter (#1090), and the income
    sections' by their parsers, one block at a time (#908).
    """
    return [row for block in sections[section] for row in block.rows]


def _parse_optional_multi_block_section[T](
    sections: dict[KnownActivitySection, list[Section]],
    section: KnownActivitySection,
    parser: Callable[[Section], list[T]],
) -> list[T]:
    """Run ``parser`` over every block of ``section`` if present, else ``[]``.

    Sister helper to :func:`_activity_section_rows` for the
    optional-section pattern shared by the Dividends and Withholding
    Tax blocks (issues #31, #138). Each parser
    receives one block at a time (preserving per-block row-index
    reporting in :class:`ParseError`, and the file lines its printed
    totals are checked against, #908); results across blocks are
    concatenated in file order. Returns ``[]`` when the
    section is absent from the input — IBKR omits empty sections,
    so absence is not an error here. The income commands and Schedule
    FA then check the section's absence against the statement's
    ``Change in NAV`` (``check_income_against_nav``, #1279, #1324).

    The multi-block iteration matches PR #137 Tier-3 P2's
    contract: the splitter's data model is plural, and partial
    blocks must NEVER be silently dropped to ``[0]``.
    """
    if section not in sections:
        return []
    out: list[T] = []
    for block in sections[section]:
        out.extend(parser(block))
    return out


def _single_section(
    sections: dict[KnownActivitySection, list[Section]],
    section: KnownActivitySection,
) -> Section:
    """Return the sole block for a single-block-by-contract section, or raise.

    Sister to :func:`_activity_section_rows`. The splitter's data
    model is plural (``dict[K, list[Section]]``) because IBKR can
    legitimately re-emit a section header mid-file — Net Asset Value
    is the splitter's docstring example, and Dividends / Withholding
    Tax are observed to occasionally emit multiple blocks too
    (handled by iteration; see #137 Tier-3 fix). For sections we
    consume as a single logical block (``STATEMENT``,
    ``ACCOUNT_INFORMATION``), silently taking ``[0]`` would mask a
    real multi-block export and silently drop the later blocks
    (which may carry data the user expects to flow through). #144:
    convert the implicit ``[0]`` assumption into a loud-fail
    :class:`~ibkr_fa.exceptions.InvariantError` so any future IBKR
    shape change surfaces immediately with the section name and the
    ``block_count`` context key.

    Use :func:`_activity_section_rows` for sections where multi-block
    flattening is the intentional contract.
    """
    blocks = sections[section]
    require(
        len(blocks) == 1,
        what=(
            f"Activity Statement section {section.value!r} consumed as a "
            f"single logical block but the splitter returned multiple "
            f"blocks — IBKR may have re-emitted the section header "
            f"mid-file in a shape ibkr-fa hasn't validated. Silently "
            f"taking the first block would drop the later blocks' rows; "
            f"refusing output instead so the unexpected shape is "
            f"reported, not absorbed. Please open a GitHub issue with "
            f"the input statement attached so the multi-block contract "
            f"for this section can be specified. See "
            f"https://github.com/jay-tau/ibkr-fa/issues/144."
        ),
        section=section.value,
        block_count=len(blocks),
    )
    return blocks[0]


def _cross_check_account_id(
    account_info: AccountInfo,
    *,
    config_account_number: str,
    allow_masked_account: bool,
) -> bool:
    """Verify the parsed account ID matches ``user.toml``.

    Returns:
        ``True`` if the cross-check ran and passed; ``False`` if it
        was skipped because the source statement masked the account
        ID AND the caller opted out via ``allow_masked_account``.

    Raises:
        ConfigError: If the account ID doesn't match
            ``config_account_number``, OR if the source statement
            masked the account ID and ``allow_masked_account`` is
            ``False``. The message is fixed text: it never prints
            either account number, masked or not (#1047).
    """
    parsed = str(account_info.account_id)
    if is_masked_account_id(parsed):
        if not allow_masked_account:
            raise ConfigError(
                field="account.number",
                message=(
                    "the statement in --input masks its account number, so it "
                    "can't be checked against account.number in user.toml. "
                    "This is the default behaviour for IBKR Activity "
                    "Statement exports. Either re-export with 'Show full "
                    "account number' enabled, or pass --allow-masked-account "
                    "to skip the safety net (you are then responsible for "
                    "ensuring the statement belongs to the same account as "
                    "user.toml)."
                ),
            )
        return False

    if parsed != config_account_number:
        raise ConfigError(
            field="account.number",
            message=(
                "the account number in --input doesn't match account.number "
                "in user.toml. Refusing to process — this defence prevents "
                "accidentally filing someone else's holdings on your return."
            ),
        )
    return True


def _require_closed_lots_within_period(
    trade_rows: SectionRows, closed_lots: list[ClosedLot], period: FlexPeriod
) -> None:
    """Refuse a CLOSED_LOT row whose ``TradeDate`` is outside ``period`` (#1313).

    ``closed_lots`` are :func:`~ibkr_fa.parser.parse_closed_lots`'s result
    for ``trade_rows``: one lot per CLOSED_LOT row, in row order. The
    refusal reports the row's index in the Trades section and prints no
    date.
    """
    lot_rows = [
        row_index
        for row_index, row in enumerate(trade_rows)
        if row["LevelOfDetail"] == _CLOSED_LOT_LEVEL
    ]
    require(
        len(lot_rows) == len(closed_lots),
        what="_require_closed_lots_within_period: one closed lot per CLOSED_LOT row",
    )
    for row_index, lot in zip(lot_rows, closed_lots, strict=True):
        if not period.start_date <= lot.close_date_time <= period.end_date:
            raise ParseError(
                section=KnownSection.TRADES.value,
                row_index=row_index,
                column="TradeDate",
                message=(
                    "this CLOSED_LOT row's TradeDate is outside the period in the "
                    "Flex Query's header record; re-export the Flex Query."
                ),
                raw_value="",
            )


def _dispatch_flex_query(
    input_path: Path,
    *,
    snapshot_validator: Callable[[list[OpenLot]], None],
    period_validator: Callable[[FlexPeriod | None], None],
    parse_nav: bool = False,
) -> SourceBundle:
    """Parse a Flex Query CSV into a :class:`~ibkr_fa.models.SourceBundle`.

    Only the Flex Query's ``BOF`` header record states its reporting
    period, so ``period_validator`` and ``snapshot_validator`` both check
    the file. Its dividend evidence is ``NOT_DETERMINABLE``: there is no Flex
    dividend parser. With ``parse_nav``, ``net_asset_value`` is a refusal
    summary, since the Flex NAV section is not parsed.

    ``snapshot_validator`` receives the parsed open lots right after the
    LOT section is parsed, before the Trades section, and must raise to
    refuse them. The FA pipeline passes a
    ``functools.partial(require_open_lots_at_calendar_year_end,
    calendar_year=...)``.

    The Trades section is required too, checked after ``snapshot_validator``:
    its CLOSED_LOT rows are the only Flex record of a holding sold during
    the year (#1286). A Trades section with a header and no rows is present
    and means nothing was traded; with no Trades section at all, a sold
    holding would vanish from Schedule FA, so that refuses.

    The period checks run last, after the closed lots are parsed (#1313).
    ``period_validator`` receives the ``BOF`` record's period, or ``None``
    when the file has no header and trailer records, and must raise to
    refuse either. The FA pipeline passes a
    ``functools.partial(require_flex_calendar_year_period,
    calendar_year=...)``. Then every CLOSED_LOT row's ``TradeDate`` must
    fall within that period: a row outside it contradicts the record.
    """
    flex = split_flex_query(input_path)
    sections = flex.sections
    _require_sections(
        sections,
        (KnownSection.ACCOUNT_INFORMATION, KnownSection.OPEN_POSITIONS_LOT),
        input_path=input_path,
    )

    account_info = parse_account_info(sections[KnownSection.ACCOUNT_INFORMATION].rows)
    open_lots = parse_open_positions(sections[KnownSection.OPEN_POSITIONS_LOT].rows)
    snapshot_validator(open_lots)
    if KnownSection.TRADES not in sections:
        raise ParseError(
            section=KnownSection.TRADES.value,
            row_index=0,
            column=None,
            message=(
                f"required section 'Trades' not found in {input_path}. Schedule FA "
                "must list every holding held during the calendar year, and a Flex "
                "Query shows the ones sold before December 31 only in its Trades "
                "section. Re-run the Flex Query with the Trades section enabled "
                "and its Closed Lots checkbox turned on."
            ),
            raw_value="",
        )
    trade_rows = sections[KnownSection.TRADES].rows
    closed_lots = parse_closed_lots(trade_rows)
    period_validator(flex.period)
    if flex.period is None:
        raise invariant_error(
            what="_dispatch_flex_query: period_validator must refuse a Flex Query with no period"
        )
    _require_closed_lots_within_period(trade_rows, closed_lots, flex.period)
    # Flex Query has no Dividends parsing yet — the existing Flex
    # Query format doesn't expose the structured dividend rows the
    # Activity Statement Dividends section does. Future PR will add
    # a Flex-Query-side dividend parser (likely from the
    # CashTransactions section). For now, returning [] preserves the
    # pre-#31 manual-fill behaviour for Flex Query inputs.
    dividends: list[Dividend] = []
    # Flex Query has no Withholding Tax parsing yet — sister gap to
    # the Dividends gap above. The CashTransactions section's
    # ``Withholding @ <rate>%`` rows would be the source. Tracked
    # under the same #33 / #138 umbrella. Returning [] keeps the
    # bundle shape uniform across both input formats so downstream
    # callers don't need to special-case on input_format.
    withholding_tax: list[WithholdingTax] = []
    # Flex Query has no Interest parsing yet — sister gap to the
    # Dividends and Withholding Tax gaps above. Tracked under #12
    # (Cash Transactions section). Returning [] keeps the bundle
    # shape uniform.
    interest_credits: list[InterestCredit] = []
    interest_non_credits: list[InterestNonCredit] = []
    # #141: Flex Query's Account Information has different shape
    # (one-row-with-N-columns vs Activity Statement's row-per-field)
    # and IBKR doesn't offer a hide-Name/Account export option for
    # Flex. The anonymised-statement fallback is therefore an
    # Activity-Statement-only concern; Flex always returns an empty
    # fallback-used set so the dispatcher contract is uniform.
    return SourceBundle(
        account_info=account_info,
        open_lots=open_lots,
        closed_lots=closed_lots,
        dividends=dividends,
        withholding_tax=withholding_tax,
        interest_credits=interest_credits,
        interest_non_credits=interest_non_credits,
        identity_fallback_used=frozenset(),
        dividend_evidence=DividendEvidence.NOT_DETERMINABLE,
        net_asset_value=(
            NetAssetValueSummary(
                closing_total=None,
                refusal_reason=NavRefusalReason.FLEX_QUERY,
            )
            if parse_nav
            else None
        ),
    )


def _dispatch_activity_statement(
    input_path: Path,
    *,
    period_validator: Callable[[StatementPeriod], None],
    broker_validator: Callable[[tuple[str, ...]], None] | None = None,
    required_sections: frozenset[KnownActivitySection] = _FA_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
    fallback_account_holder_name: str | None = None,
    fallback_account_id: str | None = None,
    parse_nav: bool = False,
    check_income_against_nav: bool = False,
) -> SourceBundle:
    """Parse an Activity Statement CSV into a :class:`~ibkr_fa.models.SourceBundle`.

    The Activity Statement path produces :class:`OpenLot` records
    via lot-reconstruction from the Trades section (Activity
    Statement's Open Positions Summary is per-symbol aggregate, not
    per-lot — see #101 for the design rationale). The Trades parser
    emits one execution per row, and
    :func:`~ibkr_fa.compute.lot_matching.match_fifo` FIFO-matches
    closing trades (``Code`` token ``C``, #104) against earlier opens
    on the same symbol, emitting one :class:`ClosedLot` per FIFO slice;
    statements with no closures still produce an empty ``closed_lots``
    list.

    Parameters
    ----------
    period_validator
        Callback that validates the parsed :class:`StatementPeriod`
        against the caller's domain (calendar year for FA, financial
        year for Schedule OS, etc.). Must raise on failure; return
        value is ignored. The FA pipeline passes a
        ``functools.partial(require_calendar_year_period,
        calendar_year=...)``; the Schedule OS pipeline will pass a
        financial-year validator (#31).
    broker_validator
        Optional callback that receives every ``BrokerName`` value in
        the ``Statement`` section (see
        :func:`parse_statement_broker_names`) and raises to refuse the
        statement. Runs right after ``period_validator``; ``None``
        skips it. The Schedule FSI compute passes a guard that
        accepts only IBKR LLC (#665).
    required_sections
        Sections that hard-fail the dispatcher if absent. Defaults
        to :data:`_FA_DEFAULT_REQUIRED_ACTIVITY_SECTIONS` (the
        4-section set the FA-A3 pipeline needs). When ``TRADES`` is
        NOT in this set, the FII / Trades / Open-Positions /
        reconciliation block is skipped entirely (``open_lots`` and
        ``closed_lots`` returned as ``[]``) — the Schedule OS
        pipeline relies on this gate to accept dividend-only AS
        exports. When ``TRADES`` IS in this set but ``OPEN_POSITIONS``
        is absent from the input file, the dispatcher accepts the
        closure-only case (#162): ``dec31_close_prices`` falls
        through to ``{}`` and reconciliation runs against an empty
        rows list. The matcher's #133 refusal still raises
        :class:`~ibkr_fa.exceptions.LotMatchingError` if any surviving
        open lot lacks a Dec-31 close price.
    fallback_account_holder_name
        Optional fallback for the Activity Statement's
        ``Account Information.Name`` row when IBKR's export hides
        it (#141). Threaded through to
        :func:`parse_activity_statement_account_info`. ``None``
        preserves the pre-#141 hard-fail-on-absence behaviour.
        Callers should set this from ``user.toml``'s
        ``[account].holder_name`` only when the user opts in via
        ``--accept-anonymised-statement`` (the CLI default is
        ``None`` so non-flag-passing users see no behavior change).
    fallback_account_id
        Optional fallback for the Activity Statement's
        ``Account Information.Account`` row when IBKR's export
        hides it. Same opt-in semantics as
        ``fallback_account_holder_name``. Note this is orthogonal
        to ``--allow-masked-account`` (which handles a present-but-
        masked ``"U**********"`` Account row): fallback only fires
        when the row is absent entirely.
    parse_nav
        Parse the ``Net Asset Value`` / ``Change in NAV`` sections into
        ``net_asset_value``. Only Schedule FA Table A2 needs them, so other
        callers skip the parse and a malformed NAV cannot refuse their run.
    check_income_against_nav
        Check the Dividends, Withholding Tax and Interest sections against
        the ``Change in NAV`` fields once they are parsed
        (:func:`~ibkr_fa.parser.activity_statement.change_in_nav_income.check_income_against_change_in_nav`,
        #1279). Schedule OS, the Schedule FSI compute and Schedule FA
        (#1324) set it; a statement without ``Change in NAV`` isn't checked.

    Returns
    -------
    A :class:`~ibkr_fa.models.SourceBundle` whose ``dividend_evidence``
    comes from the split sections (:func:`dividend_evidence`). Its
    ``identity_fallback_used`` enumerates IBKR field names (``"Name"`` /
    ``"Account"``) substituted from fallback; it is empty when no
    fallback was used. The pipeline forwards this set to
    :class:`RunReport` so the CLI can surface a loud "identity supplied
    from user.toml, not from statement" warning.

    Raises
    ------
    ParseError
        Among the section parsers' other refusals: a Trades, Dividends,
        Withholding Tax or Interest row dated outside the parsed
        statement Period (#1143). The parser that read the row refuses
        it, so the Trades check runs before
        :func:`~ibkr_fa.compute.lot_matching.match_fifo` and no income
        computation sees the row. After reconciliation, an Open Positions
        ``Summary`` row whose ``Value`` is missing, isn't a finite decimal
        within its limits, or is more than 0.01 from its ``Quantity`` times
        ``Close Price`` (#1315,
        :func:`~ibkr_fa.parser.activity_statement.validate_open_positions_values`).
    ChangeInNavIncomeError
        With ``check_income_against_nav``: an income section that the
        statement's ``Change in NAV`` contradicts (#1279).
    """
    sections = split_activity_statement_sections(input_path)
    # Every command refuses a cost adjustment, like the splitter's own
    # refusals (#913).
    check_cost_adjustments(
        sections.get(KnownActivitySection.REALIZED_UNREALIZED_PERFORMANCE_SUMMARY, [])
    )
    _require_activity_sections(sections, required_sections, input_path=input_path)

    statement_rows = _single_section(sections, KnownActivitySection.STATEMENT).rows
    period = parse_statement_period(statement_rows)
    period_validator(period)
    if broker_validator is not None:
        broker_validator(parse_statement_broker_names(statement_rows))

    account_info, identity_supplied_from_fallback = parse_activity_statement_account_info(
        _single_section(sections, KnownActivitySection.ACCOUNT_INFORMATION).rows,
        fallback_account_holder_name=fallback_account_holder_name,
        fallback_account_id=fallback_account_id,
    )

    open_lots: list[OpenLot]
    closed_lots: list[ClosedLot]
    if KnownActivitySection.TRADES in required_sections:
        instruments = build_instrument_lookup(
            _activity_section_rows(sections, KnownActivitySection.FINANCIAL_INSTRUMENT_INFORMATION)
        )
        open_positions_blocks = sections.get(KnownActivitySection.OPEN_POSITIONS)
        if open_positions_blocks:
            open_positions_rows = _activity_section_rows(
                sections, KnownActivitySection.OPEN_POSITIONS
            )
            dec31_close_prices = build_dec31_close_prices(open_positions_rows)
        else:
            open_positions_rows = []
            dec31_close_prices = {}
        executions = parse_activity_statement_trades(
            _activity_section_rows(sections, KnownActivitySection.TRADES),
            instruments=instruments,
            period=period,
        )
        open_lots, closed_lots = match_fifo(
            executions,
            dec31_close_prices=dec31_close_prices,
            report_date=period.end_date,
        )
        validate_reconciliation(open_lots, open_positions_rows)
        validate_open_positions_values(open_positions_rows)
    else:
        open_lots = []
        closed_lots = []

    # Issue #31: parse Dividends section if present. Buy-and-hold
    # accounts or accounts with no dividend ex-dates in the period
    # may legitimately have no Dividends section — IBKR omits empty
    # sections from the Activity Statement export. Returning [] in
    # that case is correct: the per-symbol total for every symbol
    # resolves to None, the CLI surfaces the manual-fill warning,
    # and behaviour is identical to pre-#31. The income commands and
    # Schedule FA accept the absence only when Change in NAV doesn't
    # show dividends (#1279, #1324, below).
    #
    # Tier-3 PR #137 review (Codex GPT-5.5 high, P2): iterate over
    # ALL blocks under KnownActivitySection.DIVIDENDS via
    # ``_parse_optional_multi_block_section``. The splitter
    # preserves multi-block lists (its docstring's explicit example
    # is Net Asset Value; the Dividends section in real exports
    # observed to date emits one block, but the data-model contract
    # is plural). Pre-fix this code path took ``[0]`` only,
    # silently dropping any later block. Joining ``parse(...)``
    # output across blocks is safe — each block's rows are
    # independently parsed under the same schema, and downstream
    # ``aggregate_dividends_by_symbol_inr`` is order-insensitive.
    dividends = _parse_optional_multi_block_section(
        sections,
        KnownActivitySection.DIVIDENDS,
        partial(parse_activity_statement_dividends, period=period),
    )

    # Issue #138: parse Withholding Tax section if present. Same
    # optional-multi-block pattern as Dividends. Accounts with a
    # W-9 on file at IBKR (US persons) or buy-and-hold accounts
    # with no dividend-paying foreign holdings legitimately have no
    # Withholding Tax section in the export; empty list is the
    # correct passthrough, which the income commands and Schedule FA
    # check against Change in NAV (#1279, #1324, below). Pairing with
    # the matching dividend row, reversal handling, and Schedule OS /
    # FSI / TR / Form 67 aggregation are compute-layer concerns tracked
    # under #33.
    withholding_tax = _parse_optional_multi_block_section(
        sections,
        KnownActivitySection.WITHHOLDING_TAX,
        partial(parse_activity_statement_withholding_tax, period=period),
    )

    # Issue #139: parse Interest section if present. Accounts whose
    # cash balance never exceeded IBKR's minimum-balance threshold
    # for credit interest legitimately have no Interest section in
    # the export; empty lists are the correct passthrough, which the
    # income commands and Schedule FA check against Change in NAV
    # (#1279, #1324, below). Each block is parsed separately and the
    # results joined in file order, as for Dividends; the parser takes
    # the whole block so each non-credit row keeps its file line (#905).
    interest_credits: list[InterestCredit] = []
    interest_non_credits: list[InterestNonCredit] = []
    for block in sections.get(KnownActivitySection.INTEREST, []):
        block_credits, block_non_credits = parse_activity_statement_interest(block, period=period)
        interest_credits.extend(block_credits)
        interest_non_credits.extend(block_non_credits)

    # #1279: a missing or partial income section is refused when the
    # statement's own Change in NAV summary contradicts it.
    if check_income_against_nav:
        check_income_against_change_in_nav(
            sections,
            base_currency=account_info.base_currency,
            dividends=dividends,
            withholding_tax=withholding_tax,
            interest_credits=interest_credits,
            interest_non_credits=interest_non_credits,
        )

    net_asset_value = (
        parse_net_asset_value(
            sections.get(KnownActivitySection.NET_ASSET_VALUE),
            sections.get(KnownActivitySection.CHANGE_IN_NAV),
        )
        if parse_nav
        else None
    )

    return SourceBundle(
        account_info=account_info,
        open_lots=open_lots,
        closed_lots=closed_lots,
        dividends=dividends,
        withholding_tax=withholding_tax,
        interest_credits=interest_credits,
        interest_non_credits=interest_non_credits,
        identity_fallback_used=identity_supplied_from_fallback,
        dividend_evidence=dividend_evidence(sections),
        net_asset_value=net_asset_value,
    )
