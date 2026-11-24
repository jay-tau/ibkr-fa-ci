"""Stand-ins for the Robinhood pipeline's income steps, shared by its test modules (#1693)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Literal

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
from ibkr_fa.compute.form67 import Form67ComputeResult
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.models.form67_entry import Form67Entry, Form67SourceOfIncome, Form67TreatyRateStatus

# Each country's CBDT code and portal name.
_COUNTRIES = {"US": ("2", "UNITED STATES OF AMERICA"), "CH": ("41", "SWITZERLAND")}


def silence_default_income_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stubbed income has no warnings, and its Schedule OS computes and writes nothing."""
    monkeypatch.setattr(pipeline, "_annual_form_warnings", lambda _result: ())
    monkeypatch.setattr(pipeline, "_income_warnings", lambda _result: ())
    monkeypatch.setattr(pipeline, "_compute_os", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(pipeline, "_write_os", lambda *_args, **_kwargs: ())


def dividend_entry(
    iso: Literal["US", "CH"],
    *,
    foreign_tax: str,
    serial: int = 1,
    normal_tax: str | None = None,
    credit: str | None = None,
) -> Form67Entry:
    """One payment's Form 67 dividend row: ₹100 of income with ``foreign_tax``
    rupees withheld, the same percent its rate.

    A US row is within the treaty's 25% Article 10(2)(b) rate, with all its
    tax eligible; CH has no treaty row. ``normal_tax`` is the Indian tax
    payable and ``credit`` both the section 90 and total credit; each is blank
    unless given.
    """
    code, name = _COUNTRIES[iso]
    tax = Decimal(foreign_tax)
    treaty = iso == "US"
    return Form67Entry(
        serial_number=serial,
        cbdt_country_code=code,
        country_name_portal=name,
        iso_alpha2=iso,
        source_of_income=Form67SourceOfIncome.DIVIDEND,
        income_inr=Decimal("100"),
        foreign_tax_inr=tax,
        foreign_tax_rate_percent=tax,
        event_count=1,
        tax_payable_normal_inr=None if normal_tax is None else Decimal(normal_tax),
        section_90_dtaa_article="Article 10(2)(b)" if treaty else None,
        section_90_dtaa_rate_percent=Decimal("25") if treaty else None,
        tax_paid_rate_percent=tax if treaty else None,
        highest_payment_rate_percent=tax if treaty else None,
        treaty_rate_status=Form67TreatyRateStatus.WITHIN if treaty else None,
        eligible_foreign_tax_inr=tax if treaty else None,
        section_90_credit_inr=None if credit is None else Decimal(credit),
        total_credit_inr=None if credit is None else Decimal(credit),
    )


def form67_assembly(*entries: Form67Entry) -> Any:
    """An FTC assembly of ``entries`` and no Schedule FSI row."""
    return SimpleNamespace(
        fsi_result=ScheduleFsiComputeResult(rows=[]),
        form67_result=Form67ComputeResult(entries=entries),
    )


@pytest.fixture(autouse=True)
def silenced_default_income_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """:func:`silence_default_income_warnings` around every test. Import it
    into a test module to make it autouse there."""
    silence_default_income_warnings(monkeypatch)
