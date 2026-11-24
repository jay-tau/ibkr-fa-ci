"""Shared Schedule FA rows and CBDT JSON fragment writer inputs (#1834).

:func:`fragment_a2_row` and :func:`fragment_a3_row` build a complete, valid
Table A2 row and open-lot Table A3 row; :func:`with_attested_peak` and
:func:`unchecked_row` adjust a row. :func:`fragment_input` builds a complete,
valid writer input bundle with :data:`FRAGMENT_A2_REMEDIES` and
:data:`FRAGMENT_A3_REMEDIES`. :func:`fail_staging_fstat` makes the staging file
of a write to one path uninspectable.
"""

from __future__ import annotations

import copy
import errno
import os
import tempfile
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa.models.fa_a2_row import FaA2NatureOfAmount, FaA2Row, FaA2Status
from ibkr_fa.models.fa_a3_row import FaA3PeakBlankReason, FaA3Row
from ibkr_fa.output.cbdt_json_fragment import (
    CbdtA2FragmentInput,
    CbdtA2Remedies,
    CbdtA3Remedies,
    CbdtScheduleFaFragmentInput,
)
from tests._no_echo import HOLDER, STRERROR


def fragment_a2_row(**overrides: Any) -> FaA2Row:
    """A complete, valid Table A2 row; override any field via kwargs."""
    fields: dict[str, Any] = {
        "country_code": "2",
        "country_name": "United States",
        "institution_name": "Interactive Brokers LLC",
        "institution_address": "One Pickwick Plaza, Greenwich, CT",
        "institution_zip": "06830",
        "account_number": "U1234567",
        "status": FaA2Status.BENEFICIAL_OWNER,
        "account_opening_date": date(2024, 2, 1),
        "peak_balance_inr": Decimal("8950"),
        "closing_balance_inr": Decimal("8950.40"),
        "nature_of_amount": FaA2NatureOfAmount.DIVIDEND,
        "gross_amount_inr": Decimal("1234.50"),
        "base_currency": "USD",
        "audit_closing_balance_base_ccy": Decimal("100.00"),
        "audit_ttbr_dec31": Decimal("89.50"),
        "audit_peak_override_source": "test config",
        "audit_peak_override_reason": "test peak",
    }
    fields.update(overrides)
    return FaA2Row(**fields)


def with_attested_peak(row: FaA2Row, peak: Decimal | None) -> FaA2Row:
    """``row`` filing ``peak`` as an attested value, or no peak when ``None``."""
    return replace(
        row,
        peak_balance_inr=peak,
        audit_peak_override_source=None if peak is None else "test config",
        audit_peak_override_reason=None if peak is None else "test peak",
    )


def unchecked_row[Row: (FaA2Row, FaA3Row)](row: Row, **changes: Any) -> Row:
    """A copy of ``row`` with ``changes`` set past the row's construction checks,
    as ``object.__setattr__`` on a frozen row would (#788)."""
    unchecked = copy.copy(row)
    for name, value in changes.items():
        object.__setattr__(unchecked, name, value)
    return unchecked


def fragment_a3_row(**overrides: Any) -> FaA3Row:
    """A complete, valid open-lot Table A3 row; override any field via kwargs."""
    fields: dict[str, Any] = {
        "country_name": "United States",
        "country_code": "2",
        "name_of_entity": "SAMPLE ISSUER AA INC",
        "address_of_entity": "100 Example Plaza, Sampleville, CA",
        "zip_code": "99990",
        "nature_of_entity": "Listed Company",
        "date_of_acquiring": date(2024, 3, 15),
        "initial_value_inr": Decimal("73372.30"),
        "peak_value_inr": Decimal("85000.70"),
        "closing_value_inr": Decimal("83000.20"),
        "gross_amount_paid_credited_inr": Decimal("500.50"),
        "gross_proceeds_from_sale_inr": Decimal("0"),
        "source_isin": "US99990A3095",
        "source_ticker": "XQAA",
        "source_country_iso2": "US",
        "audit_cost_basis_usd": Decimal("884.00"),
        "audit_acquisition_date": date(2024, 3, 15),
        "audit_close_price_usd": Decimal("140.00"),
        "audit_ttbr_acquisition": Decimal("83.00"),
        "audit_ttbr_dec31": Decimal("89.50"),
        "audit_disposal_date": None,
    }
    fields.update(overrides)
    fields.setdefault(
        "peak_blank_reason",
        FaA3PeakBlankReason.PRICES_NOT_FETCHED if fields["peak_value_inr"] is None else None,
    )
    return FaA3Row(**fields)


# The writer names no source's config keys; each caller supplies its remedies.
# An A3 remedy gets the holding's ISIN but never prints it (#1048).
FRAGMENT_A2_REMEDIES = CbdtA2Remedies(
    peak="supply the peak", address="supply the address", zip_code="supply the zip"
)
FRAGMENT_A3_REMEDIES = CbdtA3Remedies(
    address=lambda _isin: "supply the entity address",
    zip_code=lambda _isin: "supply the entity zip",
    nature=lambda _isin: "supply the entity nature",
)


def fragment_input(**overrides: Any) -> CbdtScheduleFaFragmentInput:
    """A complete, valid writer input bundle; override any field via kwargs."""
    row = overrides.pop("a2_row", fragment_a2_row())
    if "a2_peak_balance_inr" in overrides:
        row = with_attested_peak(row, overrides.pop("a2_peak_balance_inr"))
    cross_checked = overrides.pop("account_id_cross_checked", True)
    fields: dict[str, Any] = {
        "a2": (CbdtA2FragmentInput(row=row, account_id_cross_checked=cross_checked),),
        "a3_rows": (fragment_a3_row(),),
        "calendar_year": 2025,
        "assessment_year_start": 2026,
        "itr_form": "ITR-2",
        "a2_remedies": FRAGMENT_A2_REMEDIES,
        "a3_remedies": FRAGMENT_A3_REMEDIES,
    }
    fields.update(overrides)
    return CbdtScheduleFaFragmentInput(**fields)


def fail_staging_fstat(monkeypatch: pytest.MonkeyPatch, path: Path) -> tuple[OSError, list[Path]]:
    """Make ``os.fstat`` raise ``EIO``, with marker text and file name (#1048),
    for the staging descriptor of a write to ``path``. Returns the error, and
    the staging files created."""
    failure = OSError(errno.EIO, STRERROR, f"{HOLDER}/x")
    staging: list[Path] = []
    descriptors: set[int] = set()
    mkstemp, fstat = tempfile.mkstemp, os.fstat

    def record(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, name = mkstemp(*args, **kwargs)
        if Path(name).name.startswith(f".{path.name}."):
            staging.append(Path(name))
            descriptors.add(fd)
        return fd, name

    def cannot_inspect(fd: int) -> os.stat_result:
        if fd in descriptors:
            descriptors.discard(fd)
            raise failure
        return fstat(fd)

    monkeypatch.setattr("ibkr_fa.output._atomic.tempfile.mkstemp", record)
    monkeypatch.setattr("ibkr_fa.output._atomic.os.fstat", cannot_inspect)
    return failure, staging
