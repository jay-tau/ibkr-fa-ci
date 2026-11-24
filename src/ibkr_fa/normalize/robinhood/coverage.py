"""Bundle-level coverage, inventory-routing and corrected-statement checks."""

from __future__ import annotations

from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.normalize.robinhood.accounts import RobinhoodAccountsReconciliation, account_name
from ibkr_fa.normalize.robinhood.bundle import RobinhoodBundleError
from ibkr_fa.normalize.robinhood.inventory import RobinhoodSecurityQuantityResult
from ibkr_fa.parser.robinhood_statement import RobinhoodStatement

REPORT_DATES = "with that report's [[robinhood.reports]] {dates} set to match"
# What clears short or misaligned evidence, ``{end}`` its last day: the loader's
# refusals (#1498) and compute's EVIDENCE_SHORT_OF_FINANCIAL_YEAR line (#1487).
EVIDENCE_PERIOD_REMEDY = (
    "an activity report and monthly statements from the account's opening date to {end}, "
    + REPORT_DATES.format(dates="start_date and end_date")
    + ", and re-run"
)


def evidence_period_remedy(financial_year: FinancialYear, *, tax_forms: bool) -> str:
    """The remedy a refusal for short or misaligned evidence ends with: the
    period ``financial_year`` needs, for each account the refusal names.

    With ``tax_forms`` (the run reads Forms 1042-S, parsed only after these
    checks), it also names the later end income compute requires of an
    account whose form is for the FY's second calendar year (#1503).
    """
    end = f"{_last_day(financial_year)}, the end of financial year {financial_year}"
    if tax_forms:
        year = financial_year.end_year
        end += (
            f" (31 December {year} for any account whose Form 1042-S for {year} is in the "
            "input's tax-forms folder, even if not named in this refusal)"
        )
    return f"For each account named, supply {EVIDENCE_PERIOD_REMEDY.format(end=end)}"


def _last_day(financial_year: FinancialYear) -> str:
    return f"31 March {financial_year.end_year}"


def _accounts(paired: RobinhoodAccountsReconciliation, indices: list[int]) -> str:
    """``indices`` named by statement family, with the index when both
    accounts share one, as the income blocker lines name them."""
    names = [account_name(paired.account_labels, index) for index in indices]
    return f"{' and '.join(names)} account{'s' if len(names) > 1 else ''}"


def validate_robinhood_corrected_statement(
    selected: RobinhoodStatement,
    superseded: RobinhoodStatement,
) -> None:
    """Refuse a superseded statement for another account or period."""

    if (superseded.account_number, superseded.period_start, superseded.period_end) != (
        selected.account_number,
        selected.period_start,
        selected.period_end,
    ):
        raise RobinhoodBundleError("corrected statement does not match a superseded account/period")


def validate_robinhood_report_coverage(
    paired: RobinhoodAccountsReconciliation,
    config: RobinhoodConfig,
    financial_year: FinancialYear,
    *,
    tax_forms: bool,
) -> None:
    """Refuse reports and statements that don't cover the CY/FY union;
    ``tax_forms`` as for ``evidence_period_remedy``."""

    union_end = financial_year.end_date
    account_by_number = {item.number: item for item in config.accounts}
    for account in paired.accounts:
        configured = account_by_number[account.activity.primary_bundle.account_number]
        if account.evidence.start_date > configured.opening_date:
            raise RobinhoodBundleError(
                "activity report starts after its reconciled account opening date"
            )
        if account.evidence.end_date < union_end:
            short = [
                index
                for index, item in enumerate(paired.accounts)
                if item.evidence.end_date < union_end
            ]
            raise RobinhoodBundleError(
                f"activity report ends before {_last_day(financial_year)} "
                f"({_accounts(paired, short)}). "
                f"{evidence_period_remedy(financial_year, tax_forms=tax_forms)}"
            )
        if account.activity.primary_bundle.coverage_end < union_end:
            raise RobinhoodBundleError(
                "issued brokerage statements end before the requested CY/FY union"
            )


def validate_robinhood_inventory_partition(
    paired: RobinhoodAccountsReconciliation,
    quantities: RobinhoodSecurityQuantityResult,
) -> None:
    """Refuse unless inventory routing covers exactly the parsed events."""

    expected = {
        (account_index, event.source_record_index)
        for account_index, account in enumerate(paired.accounts)
        for event in account.evidence.events
    }
    routed = {
        (item.event_reference.account_index, item.event_reference.event_ordinal)
        for item in quantities.acquisitions
    }
    routed.update(
        (reference.account_index, reference.event_ordinal)
        for movement in quantities.movements
        for reference in movement.event_references
    )
    routed.update(
        (reference.account_index, reference.event_ordinal)
        for reference in (
            *quantities.option_event_references,
            *quantities.neutral_event_references,
        )
    )
    if routed != expected:
        raise RobinhoodBundleError("Robinhood inventory routing does not cover every parsed event")
