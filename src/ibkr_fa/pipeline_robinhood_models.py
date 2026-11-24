"""Public report models for Robinhood orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from ibkr_fa.output._cleanup import OutputRemovalError, RunOutputs
from ibkr_fa.pipeline import FinancialYear


def scope_warnings(scope: object, *groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(item for group in groups for item in group) + getattr(scope, "warnings", ())


class RobinhoodLegStatus(StrEnum):
    """Machine-readable per-leg outcome."""

    OK = "ok"
    MANUAL = "manual"
    ERROR = "error"
    NOT_REQUIRED = "not-required"


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodLegReport:
    """One schedule leg's status and output paths.

    ``run_outputs`` is what a standalone leg's run may have published; a leg
    inside ``robinhood-all``, or an error leg, owns none (:data:`NO_RUN_OUTPUTS`).
    """

    name: str
    status: RobinhoodLegStatus
    outputs: tuple[Path, ...] = ()
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    run_outputs: RunOutputs = field(kw_only=True)


# The outputs of a leg that owns none of its own (see RobinhoodLegReport).
NO_RUN_OUTPUTS: Final = RunOutputs((), ())


def error_leg(
    name: str,
    reason: str,
    *,
    warnings: tuple[str, ...] = (),
) -> RobinhoodLegReport:
    return RobinhoodLegReport(
        name,
        RobinhoodLegStatus.ERROR,
        reasons=(reason,),
        warnings=warnings,
        run_outputs=NO_RUN_OUTPUTS,
    )


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodAllReport:
    """Complete Robinhood orchestration result."""

    output_dir: Path
    financial_year: FinancialYear
    calendar_year: int
    legs: tuple[RobinhoodLegReport, ...]
    run_outputs: RunOutputs = field(kw_only=True)

    @property
    def status(self) -> RobinhoodLegStatus:
        statuses = {item.status for item in self.legs}
        if RobinhoodLegStatus.ERROR in statuses:
            return RobinhoodLegStatus.ERROR
        if RobinhoodLegStatus.MANUAL in statuses:
            return RobinhoodLegStatus.MANUAL
        return RobinhoodLegStatus.OK


class RobinhoodAllRemovalError(OutputRemovalError):
    """A failed ``robinhood-all`` run encountered an output-removal error.

    ``report`` is the run's completed leg report, so it can still be shown.
    """

    def __init__(self, message: str, report: RobinhoodAllReport) -> None:
        super().__init__(message)
        self.report = report


@dataclass(frozen=True, slots=True)
class RobinhoodEquityPriceAudit:
    """Privacy-safe required/covered equity close counts."""

    resolved_securities: int = 0
    required_sessions: int = 0
    covered_sessions: int = 0
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    # The error is a held security with no configured identity (#1567).
    unconfigured_identity: bool = False
