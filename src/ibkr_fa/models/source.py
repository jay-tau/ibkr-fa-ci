"""The parsed records one source file hands the schedule pipelines.

Each IBKR dispatcher in :mod:`ibkr_fa.pipeline_dispatch` returns a
:class:`SourceBundle` (#355, #625). The dispatcher checks the file's
coverage against the report before it builds the bundle: an Activity
Statement's stated period, or a Flex Query's ``BOF`` period and lot report
dates.
``dividend_evidence`` and ``net_asset_value`` carry the dividend-section
evidence and Net Asset Value the Schedule FA build needs (#626), so it
never re-reads the file.

:class:`SourceBundle` is IBKR-only. Each non-IBKR source gets its own
typed record with only the fields it can fill (#721), such as
:class:`FidelityCmaSourceRecord` and :class:`NetBenefitsSourceRecord`,
rather than a fabricated IBKR :class:`AccountInfo` or empty lot lists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from ibkr_fa.models.account_info import AccountInfo
from ibkr_fa.models.closed_lot import ClosedLot
from ibkr_fa.models.dividend import Dividend
from ibkr_fa.models.interest_credit import InterestCredit, InterestNonCredit
from ibkr_fa.models.net_asset_value import NetAssetValueSummary
from ibkr_fa.models.open_lot import OpenLot
from ibkr_fa.models.withholding_tax import WithholdingTax


class DividendEvidence(StrEnum):
    """What a source file proves about its dividend activity.

    Only ``ABSENT`` proves the file has no dividend activity, so only it
    lets Schedule FA file a ₹0 dividend total (PR #137 "zero-when-sure").
    """

    PRESENT = "present"
    ABSENT = "absent"
    NOT_DETERMINABLE = "not-determinable"


@dataclass(frozen=True, slots=True)
class StatementPeriod:
    """Inclusive date range a statement covers.

    Attributes:
        start_date: First date of the reporting period.
        end_date: Last date of the reporting period.
    """

    start_date: date
    end_date: date


@dataclass(frozen=True, slots=True)
class FlexPeriod:
    """The report dates in a Flex Query's ``BOF`` header record.

    Attributes:
        start_date: The record's report from date.
        end_date: The record's report to date, no earlier than ``start_date``.
        line: The zero-based line the ``BOF`` record starts on.
    """

    start_date: date
    end_date: date
    line: int


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceBundle:
    """Common bundle every source dispatcher hands the schedule pipelines.

    Attributes:
        account_info: The account the file belongs to.
        open_lots: Lots still open at the file's report date.
        closed_lots: Lots closed within the file.
        dividends: Parsed dividend credits; empty when the format has no
            dividend parser.
        withholding_tax: Parsed withholding rows; empty likewise.
        interest_credits: Parsed interest credits; empty likewise.
        interest_non_credits: The interest rows that aren't credits:
            debit interest and unproven rows (#905); empty likewise.
        identity_fallback_used: IBKR field names (``"Name"`` /
            ``"Account"``) supplied from ``user.toml`` because the
            statement hid them (#141); empty when none were.
        dividend_evidence: Whether the file has dividend activity. The
            default, ``NOT_DETERMINABLE``, never proves zero.
        net_asset_value: The file's Net Asset Value summary when the
            caller asked the dispatcher for it, else ``None``.
    """

    account_info: AccountInfo
    open_lots: list[OpenLot]
    closed_lots: list[ClosedLot]
    dividends: list[Dividend]
    withholding_tax: list[WithholdingTax]
    interest_credits: list[InterestCredit]
    interest_non_credits: list[InterestNonCredit]
    identity_fallback_used: frozenset[str]
    dividend_evidence: DividendEvidence = DividendEvidence.NOT_DETERMINABLE
    net_asset_value: NetAssetValueSummary | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FidelityCmaSourceRecord:
    """The income one Fidelity CMA account's monthly statements hand the pipelines.

    It has no account or lot fields: the CMA account is not an IBKR
    :class:`AccountInfo`, and its lots are FIFO output of
    :mod:`ibkr_fa.compute.fidelity_cma_capital_gains`, not parsed records.

    Attributes:
        dividends: SPAXX dividends, in statement order, then in their
            order within the statement. A row may be dated outside its
            statement's period.
        interest_credits: Credit interest, ordered likewise.
        withholding_tax: Non-resident tax withheld, ordered likewise.
    """

    dividends: tuple[Dividend, ...]
    interest_credits: tuple[InterestCredit, ...]
    withholding_tax: tuple[WithholdingTax, ...]

    def income_in_window(
        self, *, start: date, end: date
    ) -> tuple[list[Dividend], list[InterestCredit], list[WithholdingTax]]:
        """The rows dated ``start``..``end``, both inclusive, in record order."""
        return (
            [item for item in self.dividends if start <= item.payment_date <= end],
            [item for item in self.interest_credits if start <= item.credit_date <= end],
            [item for item in self.withholding_tax if start <= item.payment_date <= end],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class NetBenefitsSourceRecord:
    """The FY income a Fidelity NetBenefits bundle's statements hand the pipelines.

    Like :class:`FidelityCmaSourceRecord`, it has no account or lot fields:
    the NetBenefits account is not an IBKR :class:`AccountInfo`, and its lots
    are not parsed into it.

    Attributes:
        dividends: In-FY dividends, one per date and CUSIP, in the order
            first seen.
        withholding_tax: In-FY non-resident tax, each matched to one dividend,
            in the order first seen.
    """

    dividends: tuple[Dividend, ...]
    withholding_tax: tuple[WithholdingTax, ...]
