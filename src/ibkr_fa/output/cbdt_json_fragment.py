"""Emit a form-bound CBDT Schedule FA JSON fragment for ITR-2 or ITR-3.

The official offline ITR-2 and ITR-3 utilities can import JSON; this writer produces
the **Schedule FA** portion as a merge-ready fragment carrying a small top-level
``_ibkrFa`` provenance marker beside the disclosure::

    {"_ibkrFa": {"format": "cbdt-json-fragment", "itrForm": "ITR-2",
                 "schemaVersion": "..."},
     "ScheduleFA": {"DtlsForeignCustodialAcc":        [<A2 item>],
                    "DtlsForeignEquityDebtInterest":  [<A3 item>, ...]}}

(the user merges only those two ``ScheduleFA`` arrays into the detected
``ITR.ITR2.ScheduleFA`` or ``ITR.ITR3.ScheduleFA`` of their full return via ``ibkr-fa merge``
(:mod:`ibkr_fa.output.cbdt_json_merge`); the marker is never merged). The marker
lets ibkr-fa recognise its own prior output so a re-run can safely overwrite it
without risking a user's hand-merged file (see :func:`is_ibkr_fa_cbdt_fragment`).

Because the JSON import **bypasses the human review step** the CSV/xlsx flow
relies on, every emitted figure becomes filed tax data the moment it is
imported. So this writer is **all-or-nothing** and obeys the governing rule
("it's taxes — never be wrong"): it emits a COMPLETE, schema-valid fragment, or
it REFUSES with an exact list of what to supply — it never fabricates, rounds
away, infers, or clamps a missing/uncertain value.

Refusal layers, in order:

1. Structural preconditions (:class:`CbdtJsonStructuralError`): the input must
   be a well-formed bundle (the expected types, and A3 remedies that return
   text); the ITR form must be ITR-2 or ITR-3; the run's assessment year must
   match the vendored schema's AY and equal ``calendar_year + 1``; Table A2 (the
   custodial account itself) must be present, and each account's number
   cross-checked against its statement (not a masked/anonymised run); A2 rows
   must not repeat an account/nature pair, carry a blank account number or
   disagree on a supplied account-level value; every date must be a plain
   calendar date the CBDT pattern accepts; and no filed text value may carry a
   control character, which ``ibkr-fa merge`` refuses.
2. The authoritative CBDT schema gate (:class:`CbdtSchemaError`): every
   SUPPLIED value is validated against the vendored schema
   (:func:`ibkr_fa.output.cbdt_json_schema.validate_item`), catching any
   ``maxLength`` / ``enum`` / ``pattern`` / ``type`` violation. A missing value
   is never itself treated as an invalid one.
3. Completeness (:class:`CbdtJsonIncompleteError`, collecting ALL gaps): every
   filed A2/A3 cell must be determinable — the A2 peak balance, non-blank ZIPs,
   the A3 peak value, the gross-amount / proceeds figures, etc. Each source
   passes the remedy text for the gaps its own config fills
   (:class:`CbdtA2Remedies`, :class:`CbdtA3Remedies`), so this writer names no
   source's config keys.

Callers report a missing cell as manual fill (exit 3) and a structural or
schema refusal as a domain error (exit 4).

Dates are emitted as ``date.isoformat()`` (``YYYY-MM-DD``). The schema's date
``pattern`` is UNANCHORED (it matches a valid date as a *substring*), so a
``datetime`` (which is-a ``date``) would slip a ``...T00:00:00`` suffix past it;
:func:`_iso_date` guards against that explicitly.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, Self

from ibkr_fa.exceptions import IbkrFaError, errno_name
from ibkr_fa.models.fa_a2_row import FaA2ClosingBlankReason, FaA2Row
from ibkr_fa.models.fa_a3_row import FaA3PeakBlankReason, FaA3Row
from ibkr_fa.output import register
from ibkr_fa.output._atomic import atomic_text_output
from ibkr_fa.output._cleanup import OutputRemovalError, OutputWriteError
from ibkr_fa.output._xlsx_contract import _whole_rupee_cell
from ibkr_fa.output.base import Artifact, Destination, OutputKey, RenderContext
from ibkr_fa.output.cbdt_json_schema import (
    A2_DEFINITION,
    A2_NATURE_LETTERS,
    A3_DEFINITION,
    CBDT_ITR_FORMS,
    CBDT_SCHEMA_ASSESSMENT_YEAR_START,
    CbdtItrForm,
    schema_version_for,
    validate_item,
)
from ibkr_fa.output.fa_manual_fill import FA_A2_CLOSING_BLANK_TEXT, FA_A3_PEAK_BLANK_TEXT
from ibkr_fa.text_checks import first_control_character

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from ibkr_fa.output._cleanup import ClaimedOutputCleanup, FileIdentity

# The ``--format`` identifier PR4 surfaces on the CLI (``schedule-fa --format
# cbdt-json-fragment``).
CBDT_JSON_FRAGMENT_FORMAT_ID: Final[str] = "cbdt-json-fragment"

_SCHEDULE_FA_KEY: Final[str] = "ScheduleFA"

# Top-level provenance marker stamped on every emitted fragment. It is the only
# RELIABLE way to recognise our own prior output: a bare ``{"ScheduleFA": ...}``
# shape is reproducible by a user's hand-merged file or another tool, so the CLI
# must not treat shape as authorship when deciding whether an existing output
# file is safe to overwrite/remove. Sits beside ``ScheduleFA`` (not inside it),
# so a merge that copies only the two ``ScheduleFA`` arrays never carries it.
_PROVENANCE_KEY: Final[str] = "_ibkrFa"

# The CBDT date pattern, used with ``fullmatch`` (the schema uses it UNANCHORED).
_CBDT_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[12]\d{3}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])"
)

_A2_ACCOUNT_LEVEL_FIELDS: Final[tuple[str, ...]] = (
    "CountryName",
    "CountryCodeExcludingIndia",
    "FinancialInstName",
    "FinancialInstAddress",
    "ZipCode",
    "AccountNumber",
    "Status",
    "AccOpenDate",
    "PeakBalanceDuringPeriod",
    "ClosingBalance",
)


class CbdtJsonIncompleteError(IbkrFaError):
    """A filed Schedule FA cell is missing, so a complete CBDT JSON fragment
    cannot be emitted. Callers report it as manual fill (exit 3).

    Carries every missing cell (a manual value such as the A2 peak, a blank
    ZIP, an unresolved A2 nature and gross amount, ...) so the user can supply
    them all in one pass rather than discovering them one at a time.
    """

    def __init__(self, reasons: Iterable[str]) -> None:
        self.reasons: tuple[str, ...] = tuple(reasons)
        body = "\n  - ".join(self.reasons)
        super().__init__(
            "Refusing to emit the CBDT Schedule FA JSON fragment — it would file "
            "an incomplete disclosure. Supply the following and "
            f"re-run:\n  - {body}"
        )


class CbdtJsonStructuralError(IbkrFaError):
    """A structural precondition of the CBDT JSON fragment fails (a malformed
    input, an unsupported ITR form or assessment year, a missing or unverified
    Table A2 account, contradictory A2 rows, a date the CBDT pattern can't
    carry, a text value with a control character), so no fragment can be
    emitted, whatever cells are supplied. Callers refuse it as a domain error
    (exit 4), not as manual fill.

    Carries every reason found before refusing.
    """

    def __init__(self, reasons: Iterable[str]) -> None:
        self.reasons: tuple[str, ...] = tuple(reasons)
        body = "\n  - ".join(self.reasons)
        super().__init__(
            "Refusing to emit the CBDT Schedule FA JSON fragment — its inputs are "
            f"structurally invalid. Resolve the following and re-run:\n  - {body}"
        )


class CbdtJsonUnreadableError(IbkrFaError):
    """A file at a fragment path exists but cannot be read or parsed, so it is
    neither proven ibkr-fa's fragment nor proven foreign: refuse rather than
    keep or remove it."""


@dataclass(frozen=True, slots=True)
class CbdtA2FragmentInput:
    """One account-bound A2 row and its filing-only guard.

    The row carries the peak with its provenance, and the fragment files its
    whole-rupee ``row.filed.peak_balance_inr``; ``None`` is a gap.
    """

    row: FaA2Row | None
    account_id_cross_checked: bool


@dataclass(frozen=True, slots=True)
class CbdtA2Remedies:
    """How the source's user supplies each config-backed Table A2 value.

    Each text completes the writer's ``"A2 <field>: "`` gap line.
    """

    peak: str
    address: str
    zip_code: str


@dataclass(frozen=True, slots=True)
class CbdtA3Remedies:
    """How the source's user supplies each config-backed Table A3 entity value.

    Each function takes the holding's ISIN and completes the writer's
    ``"A3 holding in row <n> <field>: "`` gap line, which must not print the
    ISIN or any other value (#1048).
    """

    address: Callable[[str], str]
    zip_code: Callable[[str], str]
    nature: Callable[[str], str]


@dataclass(frozen=True, slots=True)
class CbdtScheduleFaFragmentInput:
    """Everything the JSON fragment writer needs in one bundle.

    Attributes:
        a2: Account-bound Table A2 inputs. Every participating account must be
            present and independently verified.
        a3_rows: The Table A3 holding rows (one per lot; may be empty for a
            cash-only account).
        calendar_year: The Schedule FA calendar year being filed (Jan-Dec).
        assessment_year_start: The assessment year start of the run (e.g. 2026
            for AY 2026-27). Must match the vendored schema's AY and equal
            ``calendar_year + 1``.
        itr_form: The standalone fragment's target form. Merge later verifies it
            against the saved return instead of trusting it.
        a2_remedies: The source's remedy text for Table A2 gaps.
        a3_remedies: The source's remedy text for Table A3 entity gaps.
    """

    a2: tuple[CbdtA2FragmentInput, ...]
    a3_rows: tuple[FaA3Row, ...]
    calendar_year: int
    assessment_year_start: int
    itr_form: CbdtItrForm
    a2_remedies: CbdtA2Remedies
    a3_remedies: CbdtA3Remedies


def _ay_label(assessment_year_start: int) -> str:
    """``2026`` -> ``"2026-27"`` (the conventional Indian AY label)."""
    return f"{assessment_year_start}-{(assessment_year_start + 1) % 100:02d}"


def _text_or_gap(value: str, label: str, remedy: str, gaps: list[str]) -> str | None:
    """Return ``value`` if it is non-blank; else record a gap and return ``None``.

    Whitespace-only counts as blank: the CBDT schema's ``minLength=1`` would
    accept ``"   "`` but it is not a real supplied value, and silently filing a
    trimmed/blank string would be wrong. We never trim-and-file — we refuse and
    ask for the exact value.
    """
    if value.strip() == "":
        gaps.append(f"{label}: {remedy}")
        return None
    return value


def _amount_or_gap(value: Decimal | None, label: str, remedy: str, gaps: list[str]) -> int | None:
    """The filed whole-rupee ``value`` as an ``int``; record a gap if ``None``.

    ``value`` is a row's ``filed`` amount: the model rounded it, so this
    writer, like the CSV and xlsx writers, never rounds.
    """
    if value is None:
        gaps.append(f"{label}: {remedy}")
        return None
    return _whole_rupee_cell(value, field=label)


def _iso_date(value: date, label: str, structural: list[str]) -> str | None:
    """Format a calendar date as ``YYYY-MM-DD``, guarding the unanchored pattern.

    ``datetime`` is a subclass of ``date``; ``datetime(...).isoformat()`` carries
    a ``T..`` time suffix that the schema's UNANCHORED date pattern would match
    as a substring, slipping an invalid value past validation. So we require an
    exact ``date`` whose ``isoformat()`` *fullmatches* the CBDT date pattern,
    and record any other value as a structural reason.
    """
    if type(value) is not date:
        structural.append(f"{label}: must be a plain calendar date (got {type(value).__name__})")
        return None
    iso = value.isoformat()
    if _CBDT_DATE_PATTERN.fullmatch(iso) is None:
        structural.append(f"{label}: is not a CBDT-acceptable YYYY-MM-DD date")
        return None
    return iso


def _control_character_reasons(
    item: str, fields: Iterable[tuple[str, str]], structural: list[str]
) -> None:
    """Record a structural reason for each filed text value of ``item`` with a
    control character. It reads the supplied text, before a blank value becomes
    a missing cell, and never shows the value (#1048)."""
    for field, value in fields:
        found = first_control_character(value)
        if found is not None:
            index, char = found
            structural.append(
                f"{item} {field}: the value (not shown) has the control character {char!r} "
                f"at position {index + 1}; ibkr-fa merge would refuse it"
            )


def _a2_row_consistency_errors(items: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    """Contradictions between Table A2 rows, comparing supplied values only.

    ``None`` marks a missing cell: it is never compared, and never filled from
    another row. For each account-level field, the first row of an account that
    supplies a value is the baseline every later supplied value must equal. A
    row forms an account/nature pair only when it supplies its nature.
    """
    baselines: dict[str, dict[str, Any]] = {}
    pairs: set[tuple[str, object]] = set()
    errors: list[str] = []
    for item in items:
        account = str(item.get("AccountNumber", "")).strip().casefold()
        nature = item.get("NatureOfAmount")
        if not account:
            errors.append("Table A2 contains a blank AccountNumber")
            continue
        if nature is not None:
            pair = (account, nature)
            if pair in pairs:
                errors.append("Table A2 contains a duplicate AccountNumber/NatureOfAmount pair")
            pairs.add(pair)
        baseline = baselines.setdefault(account, {})
        changed: list[str] = []
        for field in _A2_ACCOUNT_LEVEL_FIELDS:
            supplied = item.get(field)
            if supplied is not None and baseline.setdefault(field, supplied) != supplied:
                changed.append(field)
        if changed:
            errors.append(
                "Table A2 rows for one account disagree on account-level field(s): "
                + ", ".join(changed)
            )
    return tuple(dict.fromkeys(errors))


def _a2_closing_gap(reason: FaA2ClosingBlankReason | None) -> str:
    """Why a blank A2 closing balance is blank, and its remedy (#1013)."""
    if reason is None:
        # Only reached for a filled closing balance, whose gap text is never used.
        return ""
    cause, remedy = FA_A2_CLOSING_BLANK_TEXT[reason]
    return (
        f"the closing balance is blank because {cause}. {remedy} No config value can "
        "supply an A2 closing balance, so to enter it by hand use the review CSV or xlsx "
        "output instead"
    )


def _build_a2_item(
    index: int, row: FaA2Row, remedies: CbdtA2Remedies, gaps: list[str], structural: list[str]
) -> dict[str, Any]:
    """Map a :class:`FaA2Row` to a ``DtlsForeignCustodialAcc`` item, with
    ``None`` for (and a gap recorded for) every required-but-undeterminable
    field. The filed amounts are the row's :attr:`~FaA2Row.filed` whole-rupee
    values. The account number is never a gap: a blank one is a structural
    reason (:func:`_a2_row_consistency_errors`)."""
    _control_character_reasons(
        f"A2 account {index}",
        (
            ("CountryName", row.country_name),
            ("FinancialInstName", row.institution_name),
            ("FinancialInstAddress", row.institution_address),
            ("ZipCode", row.institution_zip),
            ("AccountNumber", row.account_number),
        ),
        structural,
    )
    filed = row.filed
    peak_inr = _amount_or_gap(
        filed.peak_balance_inr, "A2 PeakBalanceDuringPeriod", remedies.peak, gaps
    )
    if row.nature_of_amount is None or filed.gross_amount_inr is None:
        gaps.append(
            "A2 GrossAmtPaidCredited / NatureOfAmount: source evidence could not "
            "resolve every account credit; verify the unresolved nature and amount"
        )
        nature_letter: str | None = None
        gross: int | None = None
    else:
        nature_letter = A2_NATURE_LETTERS[row.nature_of_amount]
        gross = _whole_rupee_cell(filed.gross_amount_inr, field="A2 GrossAmtPaidCredited")
    closing_inr = _amount_or_gap(
        filed.closing_balance_inr,
        "A2 ClosingBalance",
        _a2_closing_gap(row.closing_blank_reason),
        gaps,
    )
    return {
        "CountryName": _text_or_gap(
            row.country_name, "A2 CountryName", "the institution country name is blank", gaps
        ),
        "CountryCodeExcludingIndia": row.country_code,
        "FinancialInstName": _text_or_gap(
            row.institution_name, "A2 FinancialInstName", "the institution name is blank", gaps
        ),
        "FinancialInstAddress": _text_or_gap(
            row.institution_address, "A2 FinancialInstAddress", remedies.address, gaps
        ),
        "ZipCode": _text_or_gap(row.institution_zip, "A2 ZipCode", remedies.zip_code, gaps),
        "AccountNumber": row.account_number,
        "Status": row.status.value,
        "AccOpenDate": _iso_date(row.account_opening_date, "A2 AccOpenDate", structural),
        "PeakBalanceDuringPeriod": peak_inr,
        "ClosingBalance": closing_inr,
        "GrossAmtPaidCredited": gross,
        "NatureOfAmount": nature_letter,
    }


def _a3_remedy(remedy: Callable[[str], str], name: str, isin: str, entity: str) -> str:
    """The source's A3 ``name`` remedy text for ``isin``, the holding ``entity``
    labels; refuse a remedy that raises or returns anything but text."""
    try:
        text = remedy(isin)
    except Exception as exc:
        raise CbdtJsonStructuralError(
            [f"the A3 {name} remedy for the {entity} raised {type(exc).__name__}"]
        ) from exc
    if not isinstance(text, str):
        raise CbdtJsonStructuralError(
            [f"the A3 {name} remedy for the {entity} returned {type(text).__name__}, not text"]
        )
    return text


def _a3_peak_gap(reason: FaA3PeakBlankReason | None) -> str:
    """Why a blank A3 peak is blank, its remedy, and where to enter it by hand (#962)."""
    if reason is None:
        # Only reached for a filled peak, whose gap text is never used.
        return ""
    cause, remedy = FA_A3_PEAK_BLANK_TEXT[reason]
    return (
        f"the peak is blank because {cause}. {remedy} No config value can supply an "
        "A3 peak itself, so to enter it by hand use the review CSV or xlsx output instead"
    )


def _build_a3_item(
    index: int,
    first_index: int,
    row: FaA3Row,
    remedies: CbdtA3Remedies,
    gaps: list[str],
    structural: list[str],
) -> dict[str, Any]:
    """Map a :class:`FaA3Row` to a ``DtlsForeignEquityDebtInterest`` item.

    Labels name rows by position, never by ISIN, date or any other value
    (#1048). Per-lot gaps (peak / gross / proceeds / dates) name the row
    (``index``); entity-level gaps shared by every lot of an ISIN (ZIP / name /
    address / nature / country) name the holding by its first row
    (``first_index``), so duplicates across lots of one holding collapse on
    de-duplication.
    """
    isin = row.source_isin
    filed = row.filed
    lot = f"A3 row {index + 1}"
    entity = f"A3 holding in row {first_index + 1}"
    _control_character_reasons(
        lot,
        (
            ("CountryName", row.country_name),
            ("NameOfEntity", row.name_of_entity),
            ("AddressOfEntity", row.address_of_entity),
            ("ZipCode", row.zip_code),
            ("NatureOfEntity", row.nature_of_entity),
        ),
        structural,
    )
    peak_inr = _amount_or_gap(
        filed.peak_value_inr,
        f"{lot} PeakBalanceDuringPeriod",
        _a3_peak_gap(row.peak_blank_reason),
        gaps,
    )
    closing_inr = _amount_or_gap(
        filed.closing_value_inr, f"{lot} ClosingBalance", "the closing value is missing", gaps
    )
    return {
        "CountryName": _text_or_gap(
            row.country_name, f"{entity} CountryName", "the country name is blank", gaps
        ),
        "CountryCodeExcludingIndia": row.country_code,
        "NameOfEntity": _text_or_gap(
            row.name_of_entity, f"{entity} NameOfEntity", "the entity name is blank", gaps
        ),
        "AddressOfEntity": _text_or_gap(
            row.address_of_entity,
            f"{entity} AddressOfEntity",
            _a3_remedy(remedies.address, "address", isin, entity),
            gaps,
        ),
        "ZipCode": _text_or_gap(
            row.zip_code,
            f"{entity} ZipCode",
            _a3_remedy(remedies.zip_code, "zip_code", isin, entity),
            gaps,
        ),
        "NatureOfEntity": _text_or_gap(
            row.nature_of_entity,
            f"{entity} NatureOfEntity",
            _a3_remedy(remedies.nature, "nature", isin, entity),
            gaps,
        ),
        "InterestAcquiringDate": _iso_date(
            row.date_of_acquiring, f"{lot} InterestAcquiringDate", structural
        ),
        "InitialValOfInvstmnt": _amount_or_gap(
            filed.initial_value_inr,
            f"{lot} InitialValOfInvstmnt",
            "the initial value is missing",
            gaps,
        ),
        "PeakBalanceDuringPeriod": peak_inr,
        "ClosingBalance": closing_inr,
        "TotGrossAmtPaidCredited": _amount_or_gap(
            filed.gross_amount_paid_credited_inr,
            f"{lot} TotGrossAmtPaidCredited",
            "dividend data is absent (e.g. a Flex Query input without a Dividends "
            "section); file the gross amount paid/credited manually",
            gaps,
        ),
        "TotGrossProceeds": _amount_or_gap(
            filed.gross_proceeds_from_sale_inr,
            f"{lot} TotGrossProceeds",
            "the gross sale proceeds are missing",
            gaps,
        ),
    }


def _require_well_formed(value: object) -> None:
    """Refuse an input that isn't the bundle this writer reads, so no later
    check fails on it with a raw exception."""
    if not isinstance(value, CbdtScheduleFaFragmentInput):
        raise CbdtJsonStructuralError(
            [f"the input must be a CbdtScheduleFaFragmentInput (got {type(value).__name__})"]
        )
    reasons: list[str] = []
    if not isinstance(value.a2, tuple) or not all(
        isinstance(item, CbdtA2FragmentInput) for item in value.a2
    ):
        reasons.append("a2 must be a tuple of CbdtA2FragmentInput")
    else:
        for index, item in enumerate(value.a2, start=1):
            if item.row is not None and not isinstance(item.row, FaA2Row):
                reasons.append(
                    f"A2 account {index}: row must be a FaA2Row or None "
                    f"(got {type(item.row).__name__})"
                )
            if type(item.account_id_cross_checked) is not bool:
                reasons.append(
                    f"A2 account {index}: account_id_cross_checked must be a bool "
                    f"(got {type(item.account_id_cross_checked).__name__})"
                )
    if not isinstance(value.a3_rows, tuple) or not all(
        isinstance(row, FaA3Row) for row in value.a3_rows
    ):
        reasons.append("a3_rows must be a tuple of FaA3Row")
    for name, year in (
        ("assessment_year_start", value.assessment_year_start),
        ("calendar_year", value.calendar_year),
    ):
        if type(year) is not int:
            reasons.append(f"{name} must be an int (got {type(year).__name__})")
    a2_remedies = value.a2_remedies
    if not isinstance(a2_remedies, CbdtA2Remedies) or not all(
        isinstance(text, str)
        for text in (a2_remedies.peak, a2_remedies.address, a2_remedies.zip_code)
    ):
        reasons.append("a2_remedies must be a CbdtA2Remedies of text")
    a3_remedies = value.a3_remedies
    if not isinstance(a3_remedies, CbdtA3Remedies) or not all(
        callable(remedy)
        for remedy in (a3_remedies.address, a3_remedies.zip_code, a3_remedies.nature)
    ):
        reasons.append("a3_remedies must be a CbdtA3Remedies of callables")
    if reasons:
        raise CbdtJsonStructuralError(reasons)


def _checked_items(
    value: CbdtScheduleFaFragmentInput,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], tuple[str, ...]]:
    """Run the structural and schema checks; return the A2 and A3 items, with
    ``None`` for each missing cell, and the missing cells' reasons.

    Raises:
        CbdtJsonStructuralError: When a structural precondition fails.
        CbdtSchemaError: When a supplied value fails the CBDT schema gate.
    """
    # --- 1. Structural preconditions -----------------------------------------
    _require_well_formed(value)
    if value.itr_form not in CBDT_ITR_FORMS:
        raise CbdtJsonStructuralError(["unsupported ITR form; expected ITR-2 or ITR-3"])
    if value.assessment_year_start != CBDT_SCHEMA_ASSESSMENT_YEAR_START:
        raise CbdtJsonStructuralError(
            [
                "the vendored CBDT schema is for AY "
                f"{_ay_label(CBDT_SCHEMA_ASSESSMENT_YEAR_START)}, "
                f"but this run targets AY {_ay_label(value.assessment_year_start)}; the JSON "
                "fragment must not be emitted against a mismatched schema (re-vendor the "
                "schema for that AY first)"
            ]
        )
    if value.assessment_year_start != value.calendar_year + 1:
        raise CbdtJsonStructuralError(
            [
                f"the assessment year start ({value.assessment_year_start}) must equal the "
                f"Schedule FA calendar year ({value.calendar_year}) + 1"
            ]
        )
    if not value.a2:
        raise CbdtJsonStructuralError(
            [
                "Table A2 (the foreign custodial account itself) is missing. "
                "A complete Schedule FA must disclose every participating account"
            ]
        )
    structural: list[str] = []
    for index, item in enumerate(value.a2, start=1):
        if not item.account_id_cross_checked:
            structural.append(
                f"A2 account {index}: the account number was not cross-checked "
                "against its statement"
            )
        if item.row is None:
            structural.append(f"Table A2 account {index}: the custodial-account row is missing")
    if structural:
        raise CbdtJsonStructuralError(structural)

    gaps: list[str] = []
    a2_items = [
        _build_a2_item(index, item.row, value.a2_remedies, gaps, structural)
        for index, item in enumerate(value.a2, start=1)
        if item.row is not None
    ]
    first_rows: dict[str, int] = {}
    a3_items = [
        _build_a3_item(
            i,
            first_rows.setdefault(row.source_isin, i),
            row,
            value.a3_remedies,
            gaps,
            structural,
        )
        for i, row in enumerate(value.a3_rows)
    ]
    structural.extend(_a2_row_consistency_errors(a2_items))
    if structural:
        raise CbdtJsonStructuralError(dict.fromkeys(structural))

    # --- 2. Supplied values against the CBDT schema --------------------------
    for definition, items in ((A2_DEFINITION, a2_items), (A3_DEFINITION, a3_items)):
        for row_index, item in enumerate(items):
            validate_item(
                {field: cell for field, cell in item.items() if cell is not None},
                definition,
                value.itr_form,
                row_index=row_index,
                check_required=False,
            )
    # De-duplicate while preserving order (entity-level gaps repeat per lot).
    return a2_items, a3_items, tuple(dict.fromkeys(gaps))


def check_cbdt_schedule_fa_fragment(value: CbdtScheduleFaFragmentInput) -> None:
    """Refuse a structural or schema problem, but not a missing cell: the
    checks of :func:`build_cbdt_schedule_fa_fragment` a caller runs when it
    won't write the fragment.

    Raises:
        CbdtJsonStructuralError / CbdtSchemaError: As
            :func:`build_cbdt_schedule_fa_fragment`.
    """
    _checked_items(value)


def build_cbdt_schedule_fa_fragment(value: CbdtScheduleFaFragmentInput) -> dict[str, Any]:
    """Build the validated ``{"ScheduleFA": {...}}`` fragment, or refuse.

    Raises:
        CbdtJsonStructuralError: When a structural precondition fails (see the
            module docstring), whatever cells are missing.
        CbdtSchemaError: When a supplied value fails the authoritative CBDT
            schema gate (e.g. a too-long ZIP / name), whatever cells are missing.
        CbdtJsonIncompleteError: When any filed A2/A3 cell is undeterminable
            (lists every one).
    """
    a2_items, a3_items, gaps = _checked_items(value)
    # --- 3. Missing cells ----------------------------------------------------
    if gaps:
        raise CbdtJsonIncompleteError(gaps)
    # Every cell is now supplied: validate the emitted items in full (``required``
    # too), so a ``None`` no gap accounts for is never filed.
    for row_index, a2_item in enumerate(a2_items):
        validate_item(a2_item, A2_DEFINITION, value.itr_form, row_index=row_index)
    for row_index, a3_item in enumerate(a3_items):
        validate_item(a3_item, A3_DEFINITION, value.itr_form, row_index=row_index)

    # The provenance marker (see _PROVENANCE_KEY) sits beside ScheduleFA so the
    # file is self-identifying as ibkr-fa output; the merge step copies only the
    # two ScheduleFA arrays, never this marker.
    return {
        _PROVENANCE_KEY: {
            "format": CBDT_JSON_FRAGMENT_FORMAT_ID,
            "itrForm": value.itr_form,
            "schemaVersion": schema_version_for(value.itr_form),
        },
        _SCHEDULE_FA_KEY: {A2_DEFINITION: a2_items, A3_DEFINITION: a3_items},
    }


def is_ibkr_fa_cbdt_fragment(path: Path) -> bool:
    """Return ``True`` iff ``path`` is a JSON file carrying ibkr-fa's CBDT
    Schedule FA fragment provenance marker (top-level ``_ibkrFa.format ==
    CBDT_JSON_FRAGMENT_FORMAT_ID``).

    Lets the CLI tell a prior ibkr-fa fragment (safe to overwrite/remove on a
    re-run) from a file it did not author (which must be preserved). A missing
    file, a JSON non-object, or a missing/mismatched marker returns ``False``.

    Raises:
        CbdtJsonUnreadableError: If ``path`` exists but cannot be read or
            parsed, whatever the error: it is neither proven ours nor proven
            foreign, so the caller must refuse and leave it untouched.
    """
    try:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        with path.open(encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as exc:
        raise CbdtJsonUnreadableError(
            f"could not inspect {path} to tell whether ibkr-fa wrote it "
            f"({_failure_kind(exc)}); it was left untouched. Fix, move or "
            "remove it, then re-run."
        ) from exc
    if not isinstance(doc, dict):
        return False
    marker = doc.get(_PROVENANCE_KEY)
    return isinstance(marker, dict) and marker.get("format") == CBDT_JSON_FRAGMENT_FORMAT_ID


def preclear_own_cbdt_fragment(path: Path) -> bool:
    """Before a run that writes the fragment to ``path``, remove a fragment
    ibkr-fa wrote there earlier, so a run that refuses leaves nothing to import.

    Returns:
        ``True`` once ``path`` is vacant: nothing was there, or ibkr-fa's own
        fragment was removed. ``False`` for any other file, which is left
        untouched (a hand-merged return, another tool's output); the caller
        refuses.

    Raises:
        CbdtJsonUnreadableError: If ``path`` can't be inspected (see
            :func:`is_ibkr_fa_cbdt_fragment`); it is left untouched.
        OutputRemovalError: If ibkr-fa's own fragment can't be removed.
    """
    if not is_ibkr_fa_cbdt_fragment(path):
        return not path.exists()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise OutputRemovalError(
            f"could not remove the previous output {path} ({errno_name(exc)})"
        ) from exc
    return True


def _failure_kind(failure: BaseException) -> str:
    """``failure`` as fixed text: an ``OSError``'s class and errno name, the
    message of an output-layer error (fixed text and allowed paths), or else
    the class name alone (#1048)."""
    if isinstance(failure, OSError):
        return f"{type(failure).__name__}: {errno_name(failure)}"
    if isinstance(failure, OutputRemovalError | OutputWriteError):
        return f"{type(failure).__name__}: {failure}"
    return type(failure).__name__


def refuse_failed_fragment_write(
    path: Path,
    cleanup: ClaimedOutputCleanup,
    failure: Exception,
    refusal: Callable[[str], IbkrFaError],
) -> None:
    """Refuse ``failure``, raised while :class:`CbdtScheduleFaJsonWriter` wrote
    ``path`` with ``on_staged=cleanup.claim``, unless the caller handles it:
    an :class:`~ibkr_fa.exceptions.IbkrFaError` (such as a missing filed value,
    :class:`CbdtJsonIncompleteError`) raised before staging, with ``path``
    still vacant.

    Whether the writer staged its file comes from the claim, not from what is
    at ``path``: once staged, it may have been published, removed or replaced.

    Raises:
        refusal: From ``failure``: once staged, whatever ``failure`` is (the
            cleanup removes ``path`` only while it is the claimed file); before
            staging, if a file has appeared at ``path``, which is left
            untouched, or if ``failure`` is not an ``IbkrFaError``.
        OutputRemovalError: From ``failure``, if ``path`` can't be inspected
            before staging; it is left untouched.
    """
    detail = _failure_kind(failure)
    if cleanup.claimed:
        raise refusal(
            f"the Schedule FA JSON fragment {path} failed after it was staged or written: {detail}"
        ) from failure
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise OutputRemovalError(
            f"the Schedule FA JSON fragment was not written, and could not inspect {path} "
            f"({errno_name(exc)}); it was left untouched ({detail})"
        ) from failure
    else:
        raise refusal(
            f"{path} appeared while the Schedule FA JSON fragment was being written; this "
            f"run did not write it, and it was left untouched ({detail})"
        ) from failure
    if not isinstance(failure, IbkrFaError):
        raise refusal(
            f"the Schedule FA JSON fragment {path} was not written: {detail}"
        ) from failure


@register(OutputKey(Destination.CBDT_JSON, Artifact.FA_A2_A3_JSON))
class CbdtScheduleFaJsonWriter:
    """The combined Schedule FA A2 + A3 fragment for the CBDT offline utility.

    Registered by key only: its input carries its own AY and ITR form, which
    stay authoritative, so :meth:`from_context` reads nothing from ``ctx``.
    """

    format_id: ClassVar[str] = CBDT_JSON_FRAGMENT_FORMAT_ID

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:  # noqa: ARG003 — OutputWriter protocol
        """Build the writer; ``ctx`` is unused (see the class docstring)."""
        return cls()

    def write(
        self,
        value: CbdtScheduleFaFragmentInput,
        path: Path,
        *,
        on_staged: Callable[[FileIdentity], None] | None = None,
    ) -> None:
        """Build, validate, and atomically write the fragment to ``path``.

        The fragment is built and validated FIRST (raising on refusal), so a
        refused emission never creates or truncates ``path``. On success the
        file is written via a temp-file + ``os.replace`` so a reader never sees
        a partial file. ``on_staged``, if given, receives the staged file's
        identity once the payload is written and before it is published; if it
        raises, nothing is published.

        Raises:
            CbdtJsonStructuralError / CbdtSchemaError / CbdtJsonIncompleteError:
                As :func:`build_cbdt_schedule_fa_fragment`.
            OSError: On filesystem failure.
            OutputWriteError: If the staged file can't be inspected for
                ``on_staged``.
            OutputRemovalError: If the staging file can't be removed after a
                failure.
        """
        fragment = build_cbdt_schedule_fa_fragment(value)
        payload = json.dumps(fragment, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        output = atomic_text_output(path, encoding="utf-8", newline="\n")
        with output as fh:
            fh.write(payload)
            if on_staged is not None:
                on_staged(output.staged_identity())
