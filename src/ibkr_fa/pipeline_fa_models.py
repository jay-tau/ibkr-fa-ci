"""FA-A3 report dataclasses extracted from :mod:`ibkr_fa.pipeline_fa`.

Part of the #155 split: the FA run-report dataclass
(:class:`RunReport`) lives here, apart from the orchestration in
pipeline_fa.py. :class:`OrphanDividendInfo` moved on to
:mod:`ibkr_fa.models.fa_orphan_dividend` (#626) so compute can build it;
this module re-exports the same object.

Both classes are re-exported from :mod:`ibkr_fa.pipeline_fa` for
backward compatibility — ``from ibkr_fa.pipeline_fa import
RunReport, OrphanDividendInfo`` continues to work. New code SHOULD
import :class:`RunReport` from this module directly:

.. code-block:: python

    from ibkr_fa.pipeline_fa_models import RunReport

See :mod:`ibkr_fa.pipeline_fa` for the :func:`run_fa_a3`
orchestration that constructs these dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compliance.itr_form_selection import ITR_FORMS, ItrForm
from ibkr_fa.invariants import require
from ibkr_fa.models.fa_a2_row import FaA2Row
from ibkr_fa.models.fa_a3_row import FaA3PeakBlankReason
from ibkr_fa.models.fa_orphan_dividend import OrphanDividendInfo
from ibkr_fa.models.interest_credit import InterestNonCredit
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.parser.format_detect import IbkrCsvFormat

# Shared RunReport invariant helpers stay in pipeline.py (the
# per-schedule RunReport classes share them).
from ibkr_fa.pipeline import (
    _require_identity_fallback_subset,
    _require_masked_account_id_if_unchecked,
    _require_plausible_assessment_year_start,
)


@dataclass(frozen=True, slots=True)
class RunReport:
    """Summary of one successful :func:`run_fa_a3` call.

    Attributes:
        rows_written: Total number of :class:`FaA3Row` records the
            writer emitted to ``output_path``. Equal to the
            ``len(open_lots_in_cy) + len(closed_lots_in_cy)`` of
            the input statement.
        output_path: The path the writer wrote to, as the caller gave it
            (not resolved; echoed back so the CLI can include it in its
            success message).
        manual_fill_columns: Verbatim portal column labels the user
            must fill in by hand before pasting rows into the
            portal: each A3 column some row leaves blank (peak value
            also for a zero-row run), plus the dividend column when
            the dividends are unresolved (e.g., Flex Query). Gross
            proceeds are listed for any disposed lot whose source
            does not prove pre-fee consideration. See
            :func:`~ibkr_fa.compute.fa_a3.fa_a3_manual_fill_gaps`
            and :mod:`ibkr_fa.output.fa_manual_fill`. The CLI
            surfaces this as a loud warning and exits non-zero.
        name_of_entity_manual_fill_holdings: Unique ``SYMBOL (ISIN)``
            identifiers whose A3 Name of entity is whitespace only (#1369): the
            statement's security description is blank and no
            ``[[fa.entities]]`` entry sets ``name``.
        address_of_entity_manual_fill_holdings: Unique ``SYMBOL (ISIN)``
            identifiers whose A3 Address of entity cell is blank (#1289): no
            ``[[fa.entities]]`` entry supplies it.
        zip_code_manual_fill_holdings: Unique ``SYMBOL (ISIN)`` identifiers
            whose A3 ZIP Code cell is blank: no entry, or an entry without
            ``zip``.
        nature_of_entity_manual_fill_holdings: Unique ``SYMBOL (ISIN)``
            identifiers whose A3 Nature of entity cell is blank. The CLI
            lists them under the manual-fill warning so the user knows which
            per-ISIN ``[[fa.entities]]`` entries need evidence.
        peak_manual_fill_holdings: Each reason some A3 peak value is blank
            (in :class:`~ibkr_fa.models.FaA3PeakBlankReason` order), with the
            unique ``SYMBOL (ISIN)`` identifiers it applies to. The CLI prints
            each reason's cause and remedy over its holdings (#962).
        gross_proceeds_manual_fill_holdings: Unique ``SYMBOL (ISIN)``
            identifiers whose A3 gross-proceeds cell is blank because
            the source supplied only net cash, not gross consideration.
        account_id: The IBKR account number parsed from the input
            CSV's ``Account Information`` section. When the source
            statement masked the account ID, this contains the
            mask string (e.g., ``"U**********"``).
        account_id_cross_checked: Whether the parsed account ID was
            cross-checked against ``user.toml``'s
            ``[account].number``. ``True`` in the normal case;
            ``False`` only when the user explicitly opted out of
            the safety net via ``allow_masked_account=True`` AND
            the source statement masked the account ID. The CLI
            surfaces a loud warning when this is ``False``.
        input_format: Which IBKR CSV serialisation the input was
            recognised as. Echoed back so the CLI can surface it
            for the user's audit trail.
        fx_policy_appropriate_for_ay: Whether the FX policy used is
            verified appropriate for the AY this run targets.
            ``True`` for CY <= 2025 (AY 2026-27 and earlier, where
            the 1961-Act framework is the verified-applicable
            framework via Section 536(2)(c)). ``True`` for CY >= 2026
            **only when** the user has explicitly attested via
            ``suppress_cross_ay_warning=True`` that they have
            independently verified the FX policy matches the
            2025-Act framework's FA-specific date-selection rule
            and carry-forward fallback.
            ``False`` for CY >= 2026 in the default (un-attested)
            case — the CLI surfaces this as a verification-needed
            warning. Note the underlying rate source is preserved
            (Rule 206(1) + Rule 207(3)(b) of the Income-tax Rules,
            2026, vs Rule 115 of the 1962 Rules); the open questions
            for AY 2027-28+ are the FA-specific date selection and
            the carry-forward fallback across non-publication days,
            both of which rest on CBDT FAQ / practice rather than
            rule text. See #126 for the cross-AY verification work.
        short_lots_excluded: Number of ``Side.SHORT`` open lots
            that were parsed and reconciled successfully but
            dropped from Schedule FA Table A3 output by
            :func:`overlapping_lots_for_cy`. Short positions are
            liabilities, not equity HELD, so they don't belong on
            A3 (which reports "Foreign Equity and Debt Interest
            held" per the ITR-2 utility). Zero in the normal
            buy-and-hold case. The CLI surfaces a non-zero count
            as an informational message so a user holding shorts
            isn't surprised to see them missing from the output.
            Issue #109.
        orphan_dividends: Tuple of :class:`OrphanDividendInfo` records
            for dividend payments that were parsed and aggregated but
            did NOT match any CY-overlapping FA-A3 output lot (so they
            were silently dropped from FA A3 by the per-lot iteration
            in ``build_fa_a3_rows``). Surfaced for two distinct CLI
            advisories per :attr:`OrphanDividendInfo.classification`:

            * ``"previously_held"`` — the symbol DID appear in the
              in-period Trades section (the held-over-ex-date /
              sold-before-pay-date case is the most common real-world
              scenario, e.g., a normal quarterly dividend on a stock
              sold mid-quarter). The dividend belongs on Schedule OS;
              the sale belongs on Schedule CG. Pre-#142 the
              ``"never acquired"`` message would mislead users with
              this pattern.
            * ``"never_held"`` — symbol never appeared in Trades.
              Typical cause is spinoff payments on symbols never
              acquired as a regular lot.

            Empty tuple in the normal case. The CLI renders each
            record with a classification-specific advisory pointing
            at the right ITR schedule. Both schedules are out of v1
            FA scope (#137 Tier-2 P3 + #142).
        withholding_tax_entries: Count of
            :class:`~ibkr_fa.models.WithholdingTax` records parsed
            from the input's ``Withholding Tax`` section (issue
            #138). Zero in the normal case for accounts with a
            W-9 on file at IBKR (US persons — IBKR withholds 0%),
            for buy-and-hold accounts with no dividend-paying
            foreign holdings, or for any Flex Query input (Flex
            doesn't yet have a sister parser — see #138 Out of
            scope and the Flex dispatcher comment). The CLI may
            surface a non-zero count informationally. Schedule
            OS / FSI / TR / Form 67 consumption of these records
            is compute-layer work tracked under #33.
        interest_credits_entries: Count of
            :class:`~ibkr_fa.models.InterestCredit` records parsed
            from the input's ``Interest`` section (issue #139).
            Zero in the normal case for accounts whose cash
            balance never exceeded IBKR's minimum-balance
            threshold for credit interest, or for any Flex Query
            input (Flex doesn't yet have a sister parser —
            tracked under #12 Cash Transactions). The CLI may
            surface a non-zero count informationally. Table A2
            adds the calendar year's credits to the account's
            gross amount credited.
        interest_unproven_rows: The Interest rows that are
            unproven (#905): Table A2 leaves its Interest nature
            for manual fill, and the CLI names each row.
        us_person: Mirror of
            :attr:`~ibkr_fa.config.AccountConfig.us_person` from
            ``user.toml``. ``True`` if the user has a W-9 on file
            at IBKR (US citizen or green-card holder). Drives the
            CLI advisory branches in :func:`~ibkr_fa.cli._render_run_report`
            that distinguish a legitimate zero-withholding US-person
            account from a non-US-person account with a missing /
            expired W-8BEN. Issue #145.
        us_source_dividend_count: Number of parsed
            :class:`~ibkr_fa.models.Dividend` records whose
            ISIN identifies a US-incorporated issuer (ISIN[:2] ==
            ``"US"``). Used together with
            :attr:`us_source_withholding_tax_net_paid` and
            :attr:`us_person` to drive the issue-#145 advisory.
            Zero for Flex Query inputs (no Flex-side dividend
            parser yet — see #138) and for accounts with no
            US-source holdings.
        us_source_withholding_tax_net_paid: Net US-source
            withholding tax actually paid in the period, as an
            unsigned :class:`~decimal.Decimal` (always ``>= 0``).
            Computed as ``max(-sum(w.amount for w in
            withholding_tax if w.isin.startswith("US")), 0)``.
            :attr:`~ibkr_fa.models.WithholdingTax.amount` is signed
            — negative for tax paid, positive for reversals/refunds
            — so the inner sum is the signed net (negative = net
            paid), negated to give a positive paid amount, then
            floored at zero so a period whose refunds exceed
            withholdings reports zero rather than a negative paid
            value. This handles three edge cases uniformly:
            (a) all-paid → positive value; (b) all-reversal → zero
            (PR #152 Codex Tier-5 P1: an all-refund period must not
            look like withholding occurred); (c) paired
            paid+reversal in the same period that cancel to zero →
            zero (PR #152 Codex Tier-3 P2: the system corrected
            itself, the user has no net tax credit to claim, so
            advisory must not treat as withholding-present).
            Combined with :attr:`us_source_dividend_count` and
            :attr:`us_person` to identify the three issue-#145
            advisory scenarios: missing-W-8BEN (non-US-person +
            zero paid WHT + US-source dividends), FTC-via-1040
            (US-person + zero paid WHT + US-source dividends), and
            unexpected-backup-withholding (US-person + nonzero
            paid WHT + US-source dividends).
        identity_supplied_from_fallback: Frozenset of Activity
            Statement field names (``"Name"`` / ``"Account"``) that
            the dispatcher backfilled from ``user.toml`` under the
            #141 ``--accept-anonymised-statement`` flag because the
            IBKR export had hidden them. Empty in the normal case
            (no flag passed, or flag passed but all identity rows
            present). When non-empty, the CLI surfaces a loud
            "identity supplied from user.toml, not from statement"
            warning. Always empty for Flex Query inputs (Flex's
            Account Information has no hide-at-export option;
            dispatcher unconditionally returns ``frozenset()``).
            The invariant :func:`_require_identity_fallback_subset`
            (called from ``__post_init__``) refuses any field name
            outside ``{"Name", "Account"}`` — drift detector for
            any future dispatcher bug.
        portal_labels_assessment_year_start: 4-digit AY start year
            whose portal-labels snapshot the writer used for this
            run (issue #65). Derived from
            ``_starting_ay_for_calendar_year(calendar_year)`` =
            ``calendar_year + 1`` for FA's CY-keyed pipeline. The
            pipeline passes this through to the writer so the
            CSV's portal-facing cells use labels verified for the
            AY this run files into (not the process-wide default).
        portal_labels_used_forward_compat_fallback: ``True`` iff
            the writer's snapshot lookup hit the +1 forward-compat
            fallback in
            :func:`ibkr_fa.output.portal_labels.for_ay` — i.e.,
            the requested AY has no registered snapshot and the
            latest registered snapshot's labels are being used as
            a stop-gap. CLI surfaces a "labels not yet verified
            for this AY" warning to the user when ``True`` so they
            understand the column headers may have shifted in
            CBDT's next ITR-2 schema release. ``False`` in the
            normal case (registered snapshot found exactly).
        run_outputs: Every output the run may have published, which the CLI
            attempts to remove if rendering or printing its report fails (#832, #1227).
    """

    rows_written: int
    output_path: Path
    manual_fill_columns: tuple[str, ...]
    account_id: str
    account_id_cross_checked: bool
    input_format: IbkrCsvFormat
    fx_policy_appropriate_for_ay: bool
    short_lots_excluded: int
    name_of_entity_manual_fill_holdings: tuple[str, ...] = ()
    address_of_entity_manual_fill_holdings: tuple[str, ...] = ()
    zip_code_manual_fill_holdings: tuple[str, ...] = ()
    nature_of_entity_manual_fill_holdings: tuple[str, ...] = ()
    peak_manual_fill_holdings: tuple[tuple[FaA3PeakBlankReason, tuple[str, ...]], ...] = ()
    gross_proceeds_manual_fill_holdings: tuple[str, ...] = ()
    orphan_dividends: tuple[OrphanDividendInfo, ...] = ()
    withholding_tax_entries: int = 0
    interest_credits_entries: int = 0
    interest_unproven_rows: tuple[InterestNonCredit, ...] = ()
    us_person: bool = False
    us_source_dividend_count: int = 0
    us_source_withholding_tax_net_paid: Decimal = Decimal("0")
    identity_supplied_from_fallback: frozenset[str] = frozenset()
    # Issue #65: per-AY portal labels tracking. See
    # :class:`~ibkr_fa.pipeline_os.ScheduleOsRunReport` for the full
    # contract — identical semantics here. The AY has no default, so
    # every constructor must say which AY it files into.
    portal_labels_assessment_year_start: int = field(kw_only=True)
    portal_labels_used_forward_compat_fallback: bool = False
    # FA "gross amount paid/credited" column was set to 0 (not manual-
    # fill) because the input was an Activity Statement covering the full
    # CY with no dividends — IBKR includes a Dividends section iff the
    # period had dividends, so the absence is positive evidence of 0.
    # The CLI surfaces this as an info note so the zero is not silent.
    fa_gross_amount_zeroed_no_cy_dividends: bool = False

    # Schedule FA Table A2 (the IBKR custodial account, #18). Populated ONLY
    # when [account.custodian] is configured; the A2 nature rows are written to a
    # separate sibling file (a2_output_path). a2_manual_fill_columns lists the
    # A2 portal cells left blank (always at least the peak balance).
    a2_rows: tuple[FaA2Row, ...] = ()
    a2_output_path: Path | None = None
    a2_manual_fill_columns: tuple[str, ...] = ()

    # CBDT Schedule FA JSON fragment (#39, `--format cbdt-json-fragment`). When
    # True, output_path holds ONE combined A2+A3 JSON file (no CSV/xlsx, no A2
    # sibling), and manual_fill_columns / a2_manual_fill_columns are EMPTY by
    # construction — the JSON is all-or-nothing, so a written file means every
    # required field was present (a "supply X" gap would have refused instead).
    # cbdt_json_a2_peak_balance_inr is the A2 peak the JSON filed (the A2 row's
    # [[schedule_fa.manual_values]] value) for the summary's provenance line.
    # The CLI exits 0 on a JSON emit.
    emitted_cbdt_json: bool = False
    cbdt_json_a2_peak_balance_inr: int | None = None
    cbdt_json_itr_form: ItrForm | None = None
    run_outputs: RunOutputs = field(kw_only=True)

    def __post_init__(self) -> None:
        """Fail-fast consistency check on the RunReport's own fields.

        PR #125: catches any future caller that constructs a report
        with contradictory state (e.g., cross-check skipped but
        account ID isn't masked — which would mean the safety net
        was bypassed without justification).
        """
        require(
            self.rows_written >= 0,
            what="RunReport.rows_written must be non-negative",
            rows_written=self.rows_written,
        )
        require(
            self.short_lots_excluded >= 0,
            what="RunReport.short_lots_excluded must be non-negative",
            short_lots_excluded=self.short_lots_excluded,
        )
        require(
            self.withholding_tax_entries >= 0,
            what="RunReport.withholding_tax_entries must be non-negative",
            withholding_tax_entries=self.withholding_tax_entries,
        )
        require(
            self.interest_credits_entries >= 0,
            what="RunReport.interest_credits_entries must be non-negative",
            interest_credits_entries=self.interest_credits_entries,
        )
        require(
            self.us_source_dividend_count >= 0,
            what="RunReport.us_source_dividend_count must be non-negative",
            us_source_dividend_count=self.us_source_dividend_count,
        )
        require(
            self.us_source_withholding_tax_net_paid >= Decimal("0"),
            what="RunReport.us_source_withholding_tax_net_paid must be non-negative",
            us_source_withholding_tax_net_paid=str(self.us_source_withholding_tax_net_paid),
        )
        require(
            (self.emitted_cbdt_json and self.cbdt_json_itr_form in ITR_FORMS)
            or (not self.emitted_cbdt_json and self.cbdt_json_itr_form is None),
            what="RunReport: CBDT JSON output must carry exactly one supported ITR form",
            emitted_cbdt_json=self.emitted_cbdt_json,
            cbdt_json_itr_form=self.cbdt_json_itr_form,
        )
        require(
            self.account_id != "",
            what="RunReport.account_id must be non-empty",
        )
        require(
            len(set(self.name_of_entity_manual_fill_holdings))
            == len(self.name_of_entity_manual_fill_holdings),
            what="RunReport.name_of_entity_manual_fill_holdings must be unique",
        )
        require(
            len(set(self.address_of_entity_manual_fill_holdings))
            == len(self.address_of_entity_manual_fill_holdings),
            what="RunReport.address_of_entity_manual_fill_holdings must be unique",
        )
        require(
            len(set(self.zip_code_manual_fill_holdings)) == len(self.zip_code_manual_fill_holdings),
            what="RunReport.zip_code_manual_fill_holdings must be unique",
        )
        require(
            len(set(self.nature_of_entity_manual_fill_holdings))
            == len(self.nature_of_entity_manual_fill_holdings),
            what="RunReport.nature_of_entity_manual_fill_holdings must be unique",
        )
        require(
            len(set(self.gross_proceeds_manual_fill_holdings))
            == len(self.gross_proceeds_manual_fill_holdings),
            what="RunReport.gross_proceeds_manual_fill_holdings must be unique",
        )
        _require_masked_account_id_if_unchecked(
            run_report_class_name="RunReport",
            account_id=self.account_id,
            account_id_cross_checked=self.account_id_cross_checked,
        )
        _require_identity_fallback_subset(
            run_report_class_name="RunReport",
            identity_supplied_from_fallback=self.identity_supplied_from_fallback,
        )
        _require_plausible_assessment_year_start(
            run_report_class_name="RunReport",
            portal_labels_assessment_year_start=self.portal_labels_assessment_year_start,
        )
