"""Tests for :mod:`ibkr_fa.compute.schedule_os` (Todo 4 of #31)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields, replace
from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.compute import (
    ScheduleOsComputeResult,
    ScheduleOsDividendLine,
    UnrecognisedDistributionTypeError,
    compute_schedule_os_dividend_lines,
)
from ibkr_fa.compute.schedule_os import (
    NonOrdinaryDistributionError,
    PaymentInLieuOfDividendError,
    ScheduleOsDomesticDividendLine,
    require_no_non_ordinary_distributions,
)
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.fx import Rule115Policy
from ibkr_fa.models import DistributionKind, Dividend
from ibkr_fa.reference.indian_adr_mapping import INDIAN_ADR_MAPPING
from ibkr_fa.types import Isin, Ticker
from tests._crash_reports import assert_prints_no_marker
from tests._fx_stubs import AuditedFxStub
from tests._os_dividend_inputs import make_div
from tests._schedule_os_refusals import (
    malformed_type_slice_message,
    missing_kind_prefix_message,
    missing_usd_marker_message,
    unsupported_kind_message,
)

_FY_START = date(2025, 4, 1)
_FY_END = date(2026, 3, 31)


def _line(
    *,
    symbol: str = "XQAA",
    isin: str = "US99990A3095",
    issuer_country_iso2: str = "US",
    payment_date: date = date(2025, 6, 15),
    fx_specified_date: date = date(2025, 5, 31),
    fx_rate_publication_date: date | None = None,
    fx_rate: str = "85",
    amount_usd: str = "1.00",
    amount_inr: str = "85.00",
    description: str = "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share",
) -> ScheduleOsDividendLine:
    if fx_rate_publication_date is None:
        fx_rate_publication_date = fx_specified_date
    return ScheduleOsDividendLine(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        issuer_country_iso2=issuer_country_iso2,
        payment_date=payment_date,
        fx_specified_date=fx_specified_date,
        fx_rate_publication_date=fx_rate_publication_date,
        fx_rate_inr_per_usd=Decimal(fx_rate),
        amount_gross_usd=Decimal(amount_usd),
        amount_gross_inr=Decimal(amount_inr),
        description=description,
        kind=DistributionKind.CASH_DIVIDEND,
    )


# ---- compute_schedule_os_dividend_lines: happy path -----------------------


def test_empty_input_returns_empty_result() -> None:
    fx = AuditedFxStub(Decimal("85"))
    result = compute_schedule_os_dividend_lines(
        [],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert result.lines == []
    assert result.domestic_lines == []


def test_single_cash_dividend_produces_one_line() -> None:
    fx = AuditedFxStub(Decimal("85"))
    result = compute_schedule_os_dividend_lines(
        [make_div(amount="1.00", payment_date=date(2025, 6, 15))],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.symbol == Ticker("XQAA")
    assert line.isin == Isin("US99990A3095")
    assert line.issuer_country_iso2 == "US"
    assert line.payment_date == date(2025, 6, 15)
    assert line.fx_specified_date == date(2025, 5, 31)
    # The stub reports no carry-back, so the line's
    # fx_rate_publication_date is the stub's rate_date, equal to
    # fx_specified_date. Pinned so a wrong-but-still-≤-specified value
    # can't slip past the invariant.
    assert line.fx_rate_publication_date == date(2025, 5, 31)
    assert line.fx_rate_inr_per_usd == Decimal("85")
    assert line.amount_gross_usd == Decimal("1.00")
    assert line.amount_gross_inr == Decimal("85.00")


def test_source_account_index_survives_dividend_conversion() -> None:
    result = compute_schedule_os_dividend_lines(
        [make_div(source_account_index=3)],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=AuditedFxStub(Decimal("85")),
    )
    assert result.lines[0].source_account_index == 3


def test_special_dividend_in_allow_list() -> None:
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        description="XQAA(US99990A3095) Special Dividend USD 0.50 per Share",
    )
    result = compute_schedule_os_dividend_lines(
        [div],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert len(result.lines) == 1
    assert result.domestic_lines == []


def test_choice_dividend_in_allow_list() -> None:
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        description="XQAA(US99990A3095) Choice Dividend USD 0.25 per Share",
    )
    result = compute_schedule_os_dividend_lines(
        [div],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert len(result.lines) == 1


@pytest.mark.parametrize(
    ("symbol", "isin", "bucket"),
    [
        pytest.param("XQAA", "US99990A3095", "lines", id="foreign"),
        pytest.param("XQCD", "IN99970W1077", "domestic_lines", id="direct-indian"),
        pytest.param("INFY", "US4567881085", "domestic_lines", id="adr-rerouted"),
    ],
)
def test_line_carries_dividend_kind(symbol: str, isin: str, bucket: str) -> None:
    """Each line-construction site copies :attr:`Dividend.kind`, which
    the review writers render as the distribution type (#606)."""
    div = make_div(
        symbol=symbol,
        isin=isin,
        description=f"{symbol}({isin}) Special Dividend USD 0.50 per Share",
    )
    result = compute_schedule_os_dividend_lines(
        [div],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=AuditedFxStub(Decimal("85")),
        adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER,
    )
    (line,) = getattr(result, bucket)
    assert line.kind == DistributionKind.SPECIAL_DIVIDEND


def test_multi_payment_multi_month_produces_ordered_lines() -> None:
    fx = AuditedFxStub(Decimal("85"))
    divs = [
        make_div(symbol="XQDD", isin="US99972J1043", payment_date=date(2025, 9, 1)),
        make_div(symbol="XQAA", isin="US99990A3095", payment_date=date(2025, 7, 1)),
        make_div(symbol="XQAQ", isin="US99992A1097", payment_date=date(2025, 9, 1)),
    ]
    result = compute_schedule_os_dividend_lines(
        divs,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert [line.symbol for line in result.lines] == [
        Ticker("XQAA"),
        Ticker("XQAQ"),
        Ticker("XQDD"),
    ]


# ---- FY filter ------------------------------------------------------------


def test_fy_boundary_apr_1_included() -> None:
    fx = AuditedFxStub(Decimal("85"))
    result = compute_schedule_os_dividend_lines(
        [make_div(payment_date=_FY_START)],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert len(result.lines) == 1


def test_fy_boundary_mar_31_included() -> None:
    fx = AuditedFxStub(Decimal("85"))
    result = compute_schedule_os_dividend_lines(
        [make_div(payment_date=_FY_END)],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert len(result.lines) == 1


def test_before_fy_silently_filtered() -> None:
    fx = AuditedFxStub(Decimal("85"))
    result = compute_schedule_os_dividend_lines(
        [make_div(payment_date=date(2025, 3, 31))],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert result.lines == []
    assert result.domestic_lines == []


def test_after_fy_silently_filtered() -> None:
    fx = AuditedFxStub(Decimal("85"))
    result = compute_schedule_os_dividend_lines(
        [make_div(payment_date=date(2026, 4, 1))],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert result.lines == []


# ---- IN-issuer source attribution -----------------------------------------


def test_in_issuer_ordinary_dividend_attributed_to_domestic_lines() -> None:
    """Per issue #158, an allow-list-classified Indian-issuer
    (ISIN prefix ``"IN"``) ordinary dividend is promoted to
    :attr:`ScheduleOsComputeResult.domestic_lines` as a
    :class:`ScheduleOsDomesticDividendLine` with full Rule 115 FX
    conversion. The rationale is IT Act §9(1)(iv): Indian-company
    ADR / GDR dividends paid in USD via a US depositary may be
    treated as Indian-source for FSI/FTC.

    Pre-#158, the row was carved out into a raw ``excluded_indian_source:
    list[Dividend]`` audit-block bucket with no FX conversion and no
    portal-TOTAL row — out of v1 scope.
    """
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        symbol="XQCD",
        isin="IN99970W1077",
        description="XQCD(IN99970W1077) Cash Dividend USD 0.10 per Share",
        amount="0.10",
        payment_date=date(2025, 6, 15),
        source_account_index=2,
    )
    result = compute_schedule_os_dividend_lines(
        [div],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert result.lines == []
    assert len(result.domestic_lines) == 1
    line = result.domestic_lines[0]
    assert line.symbol == Ticker("XQCD")
    assert line.isin == Isin("IN99970W1077")
    assert line.issuer_country_iso2 == "IN"
    assert line.payment_date == date(2025, 6, 15)
    assert line.fx_specified_date == date(2025, 5, 31)
    assert line.fx_rate_inr_per_usd == Decimal("85")
    assert line.amount_gross_usd == Decimal("0.10")
    assert line.amount_gross_inr == Decimal("0.10") * Decimal("85")
    assert line.description == "XQCD(IN99970W1077) Cash Dividend USD 0.10 per Share"
    assert line.source_account_index == 2


def test_indian_company_us_adr_routes_to_foreign_lines_not_domestic_lines() -> None:
    """v1 scope-limitation anti-regression (T5 codex P1 for issue #158):
    Indian-company ADRs / GDRs carry US-prefix (or Jersey / Marshall
    Islands / Cayman) ISINs, NOT IN-prefix. Per the asset-as-held
    convention (matches docs/portal/adr-handling.md for Schedule FA),
    those rows remain in foreign-source ``lines``, NOT
    Indian-source ``domestic_lines`` — even though the underlying issuer is
    an Indian company. The contrarian underlying-issuer view (IT Act
    §9(1)(iv) "paid by an Indian company outside India" → 1a(i)) is
    deliberately not implemented in v1; issue #183 tracks the opt-in
    mapping. This test pins the v1 scope so a future maintainer
    cannot silently re-route ADRs without an explicit design decision.
    """
    fx = AuditedFxStub(Decimal("85"))
    # Real Infosys ADR ISIN per docs/portal/adr-handling.md (US-prefixed
    # — the depositary's country, NOT India).
    div = make_div(
        symbol="INFY",
        isin="US4567881085",
        description="INFY(US4567881085) Cash Dividend USD 0.10 per Share",
        amount="0.10",
        payment_date=date(2025, 6, 15),
        source_account_index=3,
    )
    result = compute_schedule_os_dividend_lines(
        [div],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert result.domestic_lines == []
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.symbol == Ticker("INFY")
    assert line.isin == Isin("US4567881085")
    # ADR's ISIN-derived country is the depositary's country (US),
    # NOT the underlying Indian-company issuer's country (IN).
    assert line.issuer_country_iso2 == "US"
    assert line.source_account_index == 3


# ---- #183 opt-in underlying-issuer routing ------------------------------


def test_183_underlying_issuer_mode_attributes_mapped_adr_to_india() -> None:
    """Issue #183: when ``adr_routing_mode == UNDERLYING_ISSUER`` AND
    the dividend's ISIN appears in the hardcoded
    :data:`INDIAN_ADR_MAPPING` table (INFY US4567881085), the row
    moves from foreign-source :attr:`ScheduleOsComputeResult.lines` to
    Indian-source :attr:`ScheduleOsComputeResult.domestic_lines` with the
    audit-trail fields populated (``adr_source_isin`` ==
    underlying ADR ISIN; ``adr_underlying_issuer_name`` ==
    ``"Infosys Ltd"`` from the mapping table).
    """
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        symbol="INFY",
        isin="US4567881085",
        description="INFY(US4567881085) Cash Dividend USD 0.10 per Share",
        amount="0.10",
        payment_date=date(2025, 6, 15),
        source_account_index=3,
    )
    result = compute_schedule_os_dividend_lines(
        [div],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
        adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER,
    )
    assert result.lines == []
    assert len(result.domestic_lines) == 1
    line = result.domestic_lines[0]
    assert line.symbol == Ticker("INFY")
    # ISIN is preserved verbatim (we don't synthesise a fake IN-prefix
    # value — the original ADR ISIN is the audit-trail anchor back to
    # the IBKR Dividends row).
    assert line.isin == Isin("US4567881085")
    # issuer_country_iso2 is OVERRIDDEN to "IN" (the underlying-issuer
    # country, NOT the depositary country).
    assert line.issuer_country_iso2 == "IN"
    # Audit-trail fields populated.
    assert line.adr_source_isin == Isin("US4567881085")
    assert line.adr_underlying_issuer_name == "Infosys Ltd"
    assert line.source_account_index == 3
    # FX conversion is unchanged (Rule 115(1), Explanation (2)(e)
    # is income-type-anchored, not issuer-country-anchored).
    assert line.amount_gross_inr == Decimal("0.10") * Decimal("85")


def test_183_depositary_mode_default_preserves_v1_adr_routing() -> None:
    """Issue #183: the default mode (DEPOSITARY) preserves the v1 /
    PR #182 behaviour — mapped ADRs remain foreign-source per their
    depositary-country ISIN. This is the
    backward-compat anti-regression test.
    """
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        symbol="INFY",
        isin="US4567881085",
        description="INFY(US4567881085) Cash Dividend USD 0.10 per Share",
        amount="0.10",
        payment_date=date(2025, 6, 15),
    )
    # Explicit default to make the test's intent obvious.
    result = compute_schedule_os_dividend_lines(
        [div],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
        adr_routing_mode=ScheduleOsIndianAdrRoutingMode.DEPOSITARY,
    )
    assert result.domestic_lines == []
    assert len(result.lines) == 1
    assert result.lines[0].issuer_country_iso2 == "US"


def test_183_underlying_issuer_mode_unmapped_adr_stays_in_1a_ii() -> None:
    """Issue #183: opt-in is EXHAUSTIVE — only ISINs in the hardcoded
    :data:`INDIAN_ADR_MAPPING` table are re-routed. A non-mapped
    foreign issuer (a US company like XQAQ US99992A1097)
    remains foreign-source regardless of mode. No heuristic
    matching: silent re-routing of a non-Indian-company dividend
    would be a tax-correctness bug.
    """
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        symbol="XQAQ",
        isin="US99992A1097",
        description="XQAQ(US99992A1097) Cash Dividend USD 0.25 per Share",
        amount="0.25",
        payment_date=date(2025, 6, 15),
    )
    result = compute_schedule_os_dividend_lines(
        [div],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
        adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER,
    )
    assert result.domestic_lines == []
    assert len(result.lines) == 1
    assert result.lines[0].symbol == Ticker("XQAQ")
    assert result.lines[0].issuer_country_iso2 == "US"


def test_183_underlying_issuer_mode_in_prefix_isin_skips_adr_audit() -> None:
    """Issue #183: a direct Indian-share dividend (IBKR-emitted
    IN-prefix ISIN) continues to route to 1a(i) regardless of mode,
    and its ``adr_source_isin`` / ``adr_underlying_issuer_name``
    fields are ``None`` (not populated) — because this row isn't
    being RE-routed, it's been classified as Indian-source from the
    start. The ADR audit fields are only populated for rows where
    the routing decision involved the issue #183 mapping lookup.
    """
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        symbol="XQCD",
        isin="IN99970W1077",
        description="XQCD(IN99970W1077) Cash Dividend USD 0.10 per Share",
        amount="0.10",
        payment_date=date(2025, 6, 15),
    )
    result = compute_schedule_os_dividend_lines(
        [div],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
        adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER,
    )
    assert len(result.domestic_lines) == 1
    line = result.domestic_lines[0]
    assert line.issuer_country_iso2 == "IN"
    assert line.isin == Isin("IN99970W1077")
    # Audit fields stay None for the direct-IN-share case (no ADR
    # mapping lookup involved).
    assert line.adr_source_isin is None
    assert line.adr_underlying_issuer_name is None


def test_183_underlying_issuer_mode_all_mapped_indian_adrs_reroute() -> None:
    """Issue #183: smoke-test the full mapping table — every ISIN in
    :data:`INDIAN_ADR_MAPPING` must re-route in UNDERLYING_ISSUER
    mode. This locks the mapping-table coverage so a future maintainer
    cannot accidentally delete a row without a test failure.
    """

    fx = AuditedFxStub(Decimal("85"))
    dividends = [
        make_div(
            symbol=entry.canonical_ticker,
            isin=isin,
            description=f"{entry.canonical_ticker}({isin}) Cash Dividend USD 0.10 per Share",
            amount="0.10",
            payment_date=date(2025, 6, 15),
        )
        for isin, entry in INDIAN_ADR_MAPPING.items()
    ]
    result = compute_schedule_os_dividend_lines(
        dividends,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
        adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER,
    )
    assert result.lines == []
    assert len(result.domestic_lines) == len(INDIAN_ADR_MAPPING)
    # Each re-routed row has the audit fields populated with the
    # entry's underlying-issuer name.
    actual_names = {line.adr_underlying_issuer_name for line in result.domestic_lines}
    expected_names = {entry.underlying_issuer_name for entry in INDIAN_ADR_MAPPING.values()}
    assert actual_names == expected_names


def test_in_issuer_return_of_capital_refuses_not_domestic_lines() -> None:
    """A non-ordinary distribution (Return Of Capital) from an Indian
    issuer is country-agnostically non-ordinary: it must refuse (#1310),
    NOT land in :attr:`ScheduleOsComputeResult.domestic_lines` (which
    would be the silent-mis-classification bug T3-r18 P2 closed). It
    used to route to the ``excluded_non_ordinary`` audit block.
    """
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        symbol="XQCD",
        isin="IN99970W1077",
        description="XQCD(IN99970W1077) Return Of Capital USD 0.10 per Share",
        source_row_index=2,
    )
    with pytest.raises(NonOrdinaryDistributionError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=fx,
        )
    assert ei.value.kind is DistributionKind.RETURN_OF_CAPITAL
    assert ei.value.source_row_index == 2
    assert str(ei.value) == _non_ordinary_message("Return Of Capital", 2)


def test_in_issuer_missing_kind_prefix_raises_not_silently_buckets() -> None:
    """A malformed Indian-issuer description (no ``SYMBOL(ISIN)
    <whitespace>`` structural prefix) must fail fast, NOT be silently
    bucketed to :attr:`ScheduleOsComputeResult.domestic_lines` (T3-r18
    P2; bucket name changed in #158 from ``excluded_indian_source`` to
    ``domestic_lines`` but the fail-fast invariant is unchanged)."""
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        symbol="XQCD",
        isin="IN99970W1077",
        description="Cash Dividend USD 0.10 per Share",
    )
    with pytest.raises(UnrecognisedDistributionTypeError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=fx,
        )
    assert ei.value == UnrecognisedDistributionTypeError(violation="missing_kind_prefix")
    assert str(ei.value) == missing_kind_prefix_message()


def test_in_issuer_missing_usd_marker_raises_not_silently_buckets() -> None:
    """An Indian-issuer description without the canonical ``" USD "``
    marker must fail fast (parser-input shape mismatch), NOT be
    silently bucketed to
    :attr:`ScheduleOsComputeResult.domestic_lines` (T3-r18 P2; bucket
    name changed in #158 but the fail-fast invariant is unchanged)."""
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        symbol="XQCD",
        isin="IN99970W1077",
        description="XQCD(IN99970W1077) Cash Dividend",
    )
    with pytest.raises(UnrecognisedDistributionTypeError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=fx,
        )
    assert ei.value == UnrecognisedDistributionTypeError(violation="missing_usd_marker")
    assert str(ei.value) == missing_usd_marker_message()


def test_in_issuer_unsupported_kind_raises_not_silently_buckets() -> None:
    """An Indian-issuer description with a well-formed but
    unrecognised distribution kind must fail fast, NOT be silently
    bucketed to :attr:`ScheduleOsComputeResult.domestic_lines` (T3-r18
    P2 — the silent-misclassification bug routed every IN row to
    ``excluded_indian_source`` regardless of distribution kind; bucket
    name changed in #158 but the routing invariant is unchanged)."""
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        symbol="XQCD",
        isin="IN99970W1077",
        description="XQCD(IN99970W1077) Mystery Distribution USD 0.10 per Share",
    )
    with pytest.raises(UnrecognisedDistributionTypeError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=fx,
        )
    assert ei.value == UnrecognisedDistributionTypeError(violation="unsupported_kind")
    assert str(ei.value) == unsupported_kind_message()


# ---- reject list ---------------------------------------------------------


def _non_ordinary_message(kind: str, row_index: int) -> str:
    return (
        f"{kind} in the Dividends section (Dividends-section row index"
        f" {row_index}; 0-based, so data row {row_index + 1}): ibkr-fa can't yet"
        " file an IBKR return of capital, long-term capital gain distribution or"
        " cash in lieu. Skipping it, or counting it as a dividend, could misstate"
        " your income, capital gains, foreign tax credit or Schedule FA, so this"
        " run stops. Locate the row in your IBKR Activity Statement CSV by its"
        " Dividends-section row index."
    )


def _refuses_non_ordinary(description: str, kind: DistributionKind) -> None:
    """A row with ``description`` alone refuses Schedule OS dispatch with its
    kind and row index (#1310); it used to route to the
    ``excluded_non_ordinary`` audit block."""
    div = make_div(description=description, source_row_index=3)
    assert div.kind is kind
    with pytest.raises(NonOrdinaryDistributionError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=AuditedFxStub(Decimal("85")),
        )
    assert ei.value.kind is kind
    assert ei.value.source_row_index == 3
    assert ei.value.args == (_non_ordinary_message(kind.value, 3),)


def test_return_of_capital_refuses() -> None:
    _refuses_non_ordinary(
        "XQAA(US99990A3095) Return Of Capital USD 0.10 per Share",
        DistributionKind.RETURN_OF_CAPITAL,
    )


def test_long_term_capital_gain_refuses() -> None:
    _refuses_non_ordinary(
        "XQAA(US99990A3095) Long-Term Capital Gain USD 0.05 per Share",
        DistributionKind.LONG_TERM_CAPITAL_GAIN,
    )


def test_payment_in_lieu_refuses() -> None:
    """An in-FY payment in lieu refuses rather than leaving the audit
    block to hold it (#1281)."""
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        description=("XQAA(US99990A3095) Payment In Lieu Of Dividend USD 0.10 per Share"),
        source_row_index=4,
    )
    with pytest.raises(PaymentInLieuOfDividendError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=fx,
        )
    assert ei.value.source_row_index == 4
    assert str(ei.value).startswith(
        "Payment In Lieu Of Dividend in the Dividends section (Dividends-section"
        " row index 4; 0-based, so data row 5): "
    )


def test_out_of_fy_payment_in_lieu_is_skipped() -> None:
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        description=("XQAA(US99990A3095) Payment In Lieu Of Dividend USD 0.10 per Share"),
        payment_date=date(2025, 3, 31),
    )
    result = compute_schedule_os_dividend_lines(
        [div],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert result.lines == []
    assert result.domestic_lines == []


def test_cash_in_lieu_refuses() -> None:
    _refuses_non_ordinary(
        "XQAA(US99990A3095) Cash in Lieu USD 0.03 per Share",
        DistributionKind.CASH_IN_LIEU,
    )


def test_sub_qualifier_with_reject_primary_is_rejected() -> None:
    """A description whose primary kind is non-ordinary refuses even with
    the ``(Ordinary Dividend)`` sub-qualifier after it (#1310)."""
    _refuses_non_ordinary(
        "XQAA(US99990A3095) Long-Term Capital Gain USD 0.05 per Share (Ordinary Dividend)",
        DistributionKind.LONG_TERM_CAPITAL_GAIN,
    )


_ROC = "XQAA(US99990A3095) Return Of Capital USD 0.10 per Share"
_PIL = "XQAA(US99990A3095) Payment In Lieu Of Dividend USD 0.10 per Share"


def test_out_of_fy_non_ordinary_distribution_is_skipped() -> None:
    """Rows dated before or after the FY are skipped before any kind check,
    as for a payment in lieu (#1310)."""
    rows = [
        make_div(description=_ROC, payment_date=date(2025, 3, 31), source_row_index=0),
        make_div(source_row_index=1),
        make_div(
            description="XQAA(US99990A3095) Cash in Lieu USD 0.03",
            payment_date=date(2026, 4, 1),
            source_row_index=2,
        ),
    ]
    assert rows[2].kind is DistributionKind.CASH_IN_LIEU
    result = compute_schedule_os_dividend_lines(
        rows,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=AuditedFxStub(Decimal("85")),
    )
    assert [line.amount_gross_usd for line in result.lines] == [Decimal("1.00")]
    assert result.domestic_lines == []


@pytest.mark.parametrize(
    ("first", "expected"),
    [
        pytest.param(_PIL, PaymentInLieuOfDividendError, id="payment-in-lieu-first"),
        pytest.param(_ROC, NonOrdinaryDistributionError, id="return-of-capital-first"),
        pytest.param(
            "XQAA(US99990A3095) Mystery Distribution USD 0.10 per Share",
            UnrecognisedDistributionTypeError,
            id="unrecognised-first",
        ),
        pytest.param(
            "XQAA(US99990A3095) Return Of Capital USD 0.10 per Share extra",
            UnrecognisedDistributionTypeError,
            id="malformed-first",
        ),
    ],
)
def test_the_first_refusing_row_decides_the_error(first: str, expected: type[Exception]) -> None:
    """Rows are checked in order (#1310): whichever refusing in-FY row
    comes first in the section decides the error, and an earlier ordinary
    row changes nothing."""
    second = _PIL if first == _ROC else _ROC
    rows = [
        make_div(source_row_index=0),
        make_div(description=first, source_row_index=1),
        make_div(description=second, source_row_index=2),
    ]
    with pytest.raises(expected) as ei:
        compute_schedule_os_dividend_lines(
            rows,
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=AuditedFxStub(Decimal("85")),
        )
    error = ei.value
    assert isinstance(
        error,
        PaymentInLieuOfDividendError
        | NonOrdinaryDistributionError
        | UnrecognisedDistributionTypeError,
    )
    assert error.source_row_index == 1


# Each kind's type-slice, and the error it refuses with (``None``: it files).
_EVERY_KIND: dict[DistributionKind, tuple[str, type[Exception] | None]] = {
    DistributionKind.CASH_DIVIDEND: ("Cash Dividend USD 0.35 per Share", None),
    DistributionKind.SPECIAL_DIVIDEND: ("Special Dividend USD 0.35 per Share", None),
    DistributionKind.CHOICE_DIVIDEND: ("Choice Dividend USD 0.35 per Share", None),
    DistributionKind.PAYMENT_IN_LIEU_OF_DIVIDEND: (
        "Payment In Lieu Of Dividend USD 0.10 per Share",
        PaymentInLieuOfDividendError,
    ),
    DistributionKind.RETURN_OF_CAPITAL: (
        "Return Of Capital USD 0.10 per Share",
        NonOrdinaryDistributionError,
    ),
    DistributionKind.LONG_TERM_CAPITAL_GAIN: (
        "Long-Term Capital Gain USD 0.05 per Share",
        NonOrdinaryDistributionError,
    ),
    DistributionKind.CASH_IN_LIEU: ("Cash in Lieu USD 0.03", NonOrdinaryDistributionError),
    DistributionKind.UNRECOGNISED: (
        "Mystery Distribution USD 0.10 per Share",
        UnrecognisedDistributionTypeError,
    ),
    DistributionKind.MALFORMED: ("Cash Dividend 0.35 per Share", UnrecognisedDistributionTypeError),
}


def test_every_kind_has_a_case() -> None:
    """A new :class:`DistributionKind` member needs a case below (#1363)."""
    assert set(_EVERY_KIND) == set(DistributionKind)


@pytest.mark.parametrize("kind", list(_EVERY_KIND), ids=lambda kind: kind.name)
def test_every_kind_refuses_or_files(kind: DistributionKind) -> None:
    """An in-FY row of every kind either refuses with its existing error or
    lands in ``lines`` (foreign issuer) or ``domestic_lines`` (Indian issuer);
    no row is set aside (#1363)."""
    type_slice, refusal = _EVERY_KIND[kind]
    rows = [
        make_div(description=f"XQAA(US99990A3095) {type_slice}", source_row_index=0),
        make_div(
            symbol="XQCD",
            isin="IN99970W1077",
            description=f"XQCD(IN99970W1077) {type_slice}",
            source_row_index=1,
        ),
    ]
    assert [row.kind for row in rows] == [kind, kind]

    def compute() -> ScheduleOsComputeResult:
        return compute_schedule_os_dividend_lines(
            rows,
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=AuditedFxStub(Decimal("85")),
        )

    if refusal is None:
        result = compute()
        assert [(line.isin, line.kind) for line in result.lines] == [(Isin("US99990A3095"), kind)]
        assert [(line.isin, line.kind) for line in result.domestic_lines] == [
            (Isin("IN99970W1077"), kind)
        ]
        return
    with pytest.raises(refusal) as ei:
        compute()
    error = ei.value
    assert type(error) is refusal
    assert isinstance(
        error,
        PaymentInLieuOfDividendError
        | NonOrdinaryDistributionError
        | UnrecognisedDistributionTypeError,
    )
    assert error.source_row_index == 0
    if isinstance(error, NonOrdinaryDistributionError):
        assert error.kind is kind
    if isinstance(error, UnrecognisedDistributionTypeError):
        assert error.violation == (
            "unsupported_kind" if kind is DistributionKind.UNRECOGNISED else "missing_usd_marker"
        )


def test_require_no_non_ordinary_distributions_refuses_the_first_one() -> None:
    """The shared Schedule CG / Schedule FA check (#1310) reads every row it
    is given, whatever its date, and refuses the first of the three kinds;
    every other kind passes it."""
    others = [
        make_div(source_row_index=0),
        make_div(description=_PIL, source_row_index=1),
        make_div(
            description="XQAA(US99990A3095) Mystery Distribution USD 0.10 per Share",
            source_row_index=2,
        ),
        make_div(description=f"{_ROC} extra", source_row_index=3),
    ]
    assert [row.kind for row in others] == [
        DistributionKind.CASH_DIVIDEND,
        DistributionKind.PAYMENT_IN_LIEU_OF_DIVIDEND,
        DistributionKind.UNRECOGNISED,
        DistributionKind.MALFORMED,
    ]
    require_no_non_ordinary_distributions(others)
    require_no_non_ordinary_distributions([])

    rows = [
        *others,
        make_div(
            description="XQAA(US99990A3095) Cash in Lieu USD 0.03",
            payment_date=date(1990, 1, 2),
            source_row_index=4,
        ),
        make_div(description=_ROC, source_row_index=5),
    ]
    with pytest.raises(NonOrdinaryDistributionError) as ei:
        require_no_non_ordinary_distributions(iter(rows))
    assert ei.value.kind is DistributionKind.CASH_IN_LIEU
    assert ei.value.args == (_non_ordinary_message("Cash in Lieu", 4),)


def test_non_ordinary_message_without_a_row_index() -> None:
    """A :class:`Dividend` built outside the parser has no row index, and
    the message then names none."""
    error = NonOrdinaryDistributionError(DistributionKind.RETURN_OF_CAPITAL, None)
    assert error.source_row_index is None
    assert str(error) == (
        "Return Of Capital in the Dividends section: ibkr-fa can't yet file an"
        " IBKR return of capital, long-term capital gain distribution or cash in"
        " lieu. Skipping it, or counting it as a dividend, could misstate your"
        " income, capital gains, foreign tax credit or Schedule FA, so this run"
        " stops. Locate the row in your IBKR Activity Statement CSV by its"
        " Dividends-section row index."
    )


def test_substring_collision_with_allow_list_is_not_silently_accepted() -> None:
    """A novel IBKR label whose leading words collide with an allow-list
    entry (here ``"Cash Dividend"``) must NOT be silently accepted as
    an ordinary dividend. The classifier uses exact-kind dispatch
    (split on ``" USD "``), not ``startswith``, so a hypothetical
    ``"Cash Dividend In Lieu Of Stock USD ..."`` label routes to
    :class:`UnrecognisedDistributionTypeError` — guarding the fail-fast
    contract documented in the module docstring."""
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        description=("XQAA(US99990A3095) Cash Dividend In Lieu Of Stock USD 0.35 per Share"),
    )
    with pytest.raises(UnrecognisedDistributionTypeError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=fx,
        )
    assert ei.value == UnrecognisedDistributionTypeError(violation="unsupported_kind")
    assert str(ei.value) == unsupported_kind_message()


# ---- description prefix shape (parser ↔ classifier round-trip) -----------


def test_spaced_symbol_isin_prefix_classifies_via_regex_strip() -> None:
    """The parser's ``_DESCRIPTION_PATTERN`` accepts ``\\s*`` between
    SYMBOL and ``(``, so a hand-normalised description like
    ``"XQAA (US99990A3095) Cash Dividend USD 0.35 per Share"`` (with
    a space) passes parsing. The compute-layer classifier MUST then
    strip that same prefix shape — using a regex, not the literal
    ``f"{symbol}({isin}) "`` — otherwise the dividend would be
    silently raised with ``violation="missing_kind_prefix"`` and the
    whole ``schedule-os`` run would abort (T5-r20 P2 finding).
    """
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        description="XQAA (US99990A3095) Cash Dividend USD 0.35 per Share",
    )
    result = compute_schedule_os_dividend_lines(
        [div],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert len(result.lines) == 1
    assert result.domestic_lines == []


def test_tab_separated_symbol_isin_prefix_classifies_via_regex_strip() -> None:
    """The parser's ``_DESCRIPTION_PATTERN`` accepts ``\\s*`` between
    SYMBOL and ``(``, which includes tabs and other whitespace — the
    classifier MUST strip the same shape (T5-r20 P2 regression-
    boundary case: any character matching the parser's ``\\s*`` must
    classify here, not just literal spaces).
    """
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        description="XQAA\t(US99990A3095) Cash Dividend USD 0.35 per Share",
    )
    result = compute_schedule_os_dividend_lines(
        [div],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert len(result.lines) == 1


def test_multiple_spaces_symbol_isin_prefix_classifies_via_regex_strip() -> None:
    """The parser's ``_DESCRIPTION_PATTERN`` ``\\s*`` matches zero or
    more whitespace characters between SYMBOL and ``(``, so descriptions
    with double / triple spaces classify the same as the canonical
    no-space form (T5-r20 P2 regression-boundary case)."""
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        description="XQAA   (US99990A3095) Cash Dividend USD 0.35 per Share",
    )
    result = compute_schedule_os_dividend_lines(
        [div],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert len(result.lines) == 1


# ---- unrecognised --------------------------------------------------------


def test_unrecognised_type_raises_with_violation_and_row_index() -> None:
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        description=("XQAA(US99990A3095) Magic Beans Distribution USD 1.00 per Share"),
        source_row_index=3,
    )
    with pytest.raises(UnrecognisedDistributionTypeError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=fx,
        )
    err = ei.value
    assert err == UnrecognisedDistributionTypeError(
        violation="unsupported_kind", source_row_index=3
    )
    assert str(err) == unsupported_kind_message(3)


def test_type_slice_missing_usd_marker_raises_even_for_allow_list_kind() -> None:
    """A malformed type-slice without the canonical ``" USD "`` marker
    must NOT be silently accepted as an ordinary dividend even when
    the whole stripped slice equals an allow-listed kind (e.g.,
    ``"Cash Dividend"`` with no amount payload). The strict-classifier
    guard fails fast with the ``missing_usd_marker`` violation
    discriminator so a parser-bypass or upstream-schema-drift never
    routes such a payload into the OS aggregate."""
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(
        description="XQAA(US99990A3095) Cash Dividend",
    )
    with pytest.raises(UnrecognisedDistributionTypeError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=fx,
        )
    assert ei.value == UnrecognisedDistributionTypeError(violation="missing_usd_marker")
    assert str(ei.value) == missing_usd_marker_message()


@pytest.mark.parametrize(
    "type_slice",
    [
        "Cash Dividend USD nonsense",
        "Cash Dividend USD -5 per Share (New Adjustment)",
        "Cash Dividend USD 0.25 per Share (Return of Capital)",
        "Cash Dividend USD 0.25 per Share - US Tax",
        "Special Dividend USD  per Share",
        "Cash Dividend USD 0.25 USD 9 per Share",
        "Cash Dividend USD -5 per Share",
        "Cash Dividend USD +5 per Share",
    ],
)
@pytest.mark.parametrize("isin", ["US99990A3095", "IN99970W1077"], ids=["US", "IN"])
def test_known_kind_outside_the_grammar_raises_malformed_type_slice(
    type_slice: str, isin: str
) -> None:
    """#1096: a known kind label whose whole type-slice isn't
    ``<kind> USD <amount> per Share[ (Ordinary Dividend)]`` fails fast
    with the ``malformed_type_slice`` violation, for any issuer country,
    instead of entering 1a(i) on its label alone."""
    div = make_div(isin=isin, description=f"XQAA({isin}) {type_slice}", source_row_index=2)
    assert div.kind is DistributionKind.MALFORMED
    with pytest.raises(UnrecognisedDistributionTypeError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=AuditedFxStub(Decimal("85")),
        )
    err = ei.value
    assert err == UnrecognisedDistributionTypeError(
        violation="malformed_type_slice", source_row_index=2
    )
    assert str(err) == malformed_type_slice_message(2)


def test_symbol_only_description_raises() -> None:
    """Description that is exactly ``SYMBOL(ISIN)`` (no whitespace,
    no type-words) doesn't match the structural prefix regex (which
    requires ``\\s+`` after the ``)``), so this routes to
    ``missing_kind_prefix``."""
    fx = AuditedFxStub(Decimal("85"))
    div = make_div(description="XQAA(US99990A3095)")
    with pytest.raises(UnrecognisedDistributionTypeError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=fx,
        )
    assert ei.value.violation == "missing_kind_prefix"


# ---- USD-only precondition ------------------------------------------------


def test_non_usd_eur_raises_invariant_error() -> None:
    fx = AuditedFxStub(Decimal("85"))
    div = Dividend(
        symbol=Ticker("XQCV"),
        isin=Isin("NL99971E1064"),
        payment_date=date(2025, 6, 15),
        amount_gross=Decimal("1.00"),
        currency="EUR",
        description="XQCV(NL99971E1064) Cash Dividend EUR 1.00 per Share",
    )
    with pytest.raises(InvariantError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=fx,
        )
    msg = str(ei.value)
    assert "#52" in msg
    assert "dividends.py" in msg


def test_non_usd_gbp_raises_invariant_error() -> None:
    fx = AuditedFxStub(Decimal("85"))
    div = Dividend(
        symbol=Ticker("XQCW"),
        isin=Isin("GB99970L1012"),
        payment_date=date(2025, 6, 15),
        amount_gross=Decimal("1.00"),
        currency="GBP",
        description="XQCW(GB99970L1012) Cash Dividend GBP 1.00 per Share",
    )
    with pytest.raises(InvariantError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=fx,
        )
    msg = str(ei.value)
    assert "#52" in msg


# ---- precision ----------------------------------------------------------


def test_full_precision_inr_sum_invariant() -> None:
    """Each line's INR amount equals USD * rate at full Decimal
    precision (no per-payment quantization)."""

    fx = AuditedFxStub(
        {
            date(2025, 6, 15): Decimal("82.6417"),
            date(2025, 9, 15): Decimal("86.3958"),
        }
    )
    divs = [
        make_div(amount="0.0135", payment_date=date(2025, 6, 15)),
        make_div(amount="0.02875", payment_date=date(2025, 9, 15)),
    ]
    result = compute_schedule_os_dividend_lines(
        divs,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert result.lines[0].amount_gross_inr == Decimal("0.0135") * Decimal("82.6417")
    assert result.lines[1].amount_gross_inr == Decimal("0.02875") * Decimal("86.3958")


def test_generator_input_consumed_via_list_materialisation() -> None:
    """Generator argument is materialised to list before the
    precondition pass — the iterator must not be exhausted by the
    USD-only check."""
    fx = AuditedFxStub(Decimal("85"))

    def _gen():
        yield make_div()

    result = compute_schedule_os_dividend_lines(
        _gen(),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert len(result.lines) == 1


# ---- ScheduleOsDividendLine invariants ----------------------------------


def test_amount_gross_usd_must_be_positive() -> None:
    with pytest.raises(InvariantError):
        _line(amount_usd="0", amount_inr="0.01")
    with pytest.raises(InvariantError):
        _line(amount_usd="-1", amount_inr="0.01")


def test_fx_rate_must_be_positive() -> None:
    with pytest.raises(InvariantError):
        _line(fx_rate="0")
    with pytest.raises(InvariantError):
        _line(fx_rate="-1")


def test_amount_gross_inr_must_be_positive() -> None:
    with pytest.raises(InvariantError):
        _line(amount_inr="0")
    with pytest.raises(InvariantError):
        _line(amount_inr="-1")


def test_fx_specified_date_must_be_le_payment_date() -> None:
    with pytest.raises(InvariantError):
        _line(
            payment_date=date(2025, 6, 15),
            fx_specified_date=date(2025, 6, 16),
        )


def test_issuer_country_iso2_cannot_be_in() -> None:
    with pytest.raises(InvariantError):
        _line(issuer_country_iso2="IN", isin="IN99970W1077")


def test_issuer_country_iso2_must_be_2_letter_uppercase_alpha() -> None:
    with pytest.raises(InvariantError):
        _line(issuer_country_iso2="us")
    with pytest.raises(InvariantError):
        _line(issuer_country_iso2="USA")
    with pytest.raises(InvariantError):
        _line(issuer_country_iso2="U1")


def test_issuer_country_iso2_with_a_non_ascii_capital_rejected() -> None:
    """#854: ``isupper`` and ``isalpha`` accept ``"Ü"``; ISO 3166-1 is ASCII."""
    with pytest.raises(InvariantError) as exc:
        _line(issuer_country_iso2="ÜS")
    assert exc.value.what == (
        "issuer_country_iso2 must be a 2-letter uppercase ISO 3166-1 alpha-2 code"
    )
    assert exc.value.context == (
        ("issuer_country_iso2", None),
        ("symbol", None),
        ("isin", None),
    )


# ---- UnrecognisedDistributionTypeError ----------------------------------


def _assert_absent(message: str, *values: str) -> None:
    for value in values:
        assert value.lower() not in message.lower(), value


def _assert_absent_token(message: str, *tokens: str) -> None:
    """For short values that fixed text contains as letters ("SYM" in
    "symbol", "ANN" in "cannot"), check the whole token."""
    for token in tokens:
        assert re.search(rf"(?i)\b{re.escape(token)}\b", message) is None, token


def test_unrecognised_distribution_type_error_is_dataclass() -> None:
    """The error holds only the violation and the row index (#1285)."""
    err = UnrecognisedDistributionTypeError(violation="unsupported_kind", source_row_index=1)
    assert hasattr(UnrecognisedDistributionTypeError, "__slots__")
    assert [f.name for f in fields(err)] == ["violation", "source_row_index"]
    with pytest.raises(FrozenInstanceError):
        err.source_row_index = 2  # ty: ignore[invalid-assignment]


def test_unrecognised_distribution_type_error_unknown_violation_has_fixed_text() -> None:
    err = UnrecognisedDistributionTypeError(violation="unsupported_kind", source_row_index=42)
    err = replace(err, violation="Synthetic Holder Two U1234567 ANN")
    msg = str(err)
    assert msg == (
        "UnrecognisedDistributionTypeError: unknown violation kind"
        " (Dividends-section row index 42; 0-based, so data row 43)."
        " This indicates a bypass of the ``ty`` type-checker at the"
        " construction site; report this as a bug."
    )
    _assert_absent(msg, "Synthetic Holder Two", "U1234567")
    _assert_absent_token(msg, "ANN")


def test_unrecognised_distribution_type_error_str_contains_actionable_wording() -> None:
    err = UnrecognisedDistributionTypeError(violation="unsupported_kind")
    msg = str(err)
    assert msg == unsupported_kind_message()
    assert "/issues/new/choose" in msg
    # The old copy/paste marker carried the row's values (#1047).
    assert "unsupported-distribution-type" not in msg
    # #1425: no self-edit remedy. The parser gives an unknown label the
    # UNRECOGNISED kind, which refuses before any list is read.
    assert "src/ibkr_fa" not in msg
    assert "re-run" not in msg


def test_unrecognised_distribution_type_error_str_has_no_row_value_marker() -> None:
    """#1047: the old 'unsupported-distribution-type: …' marker printed
    the symbol, ISIN, payment date, currency, per-share amount and
    kind; the fixed text carries none of them."""
    div = make_div(
        symbol="SYM",
        isin="US99971T1035",
        payment_date=date(2025, 6, 1),
        description="SYM(US99971T1035) Magic USD 0.35 per Share",
    )
    err = UnrecognisedDistributionTypeError.from_dividend(div, violation="unsupported_kind")
    msg = str(err)
    assert msg == unsupported_kind_message()
    assert "unsupported-distribution-type" not in msg
    _assert_absent(msg, "US99971T1035", "2025-06-01", "0.35", "Magic")
    _assert_absent_token(msg, "SYM")


def test_unrecognised_distribution_type_error_str_for_missing_kind_prefix_violation() -> None:
    """The ``missing_kind_prefix`` discriminator must produce a message
    that names the parser-input problem (not "novel distribution
    kind") so the user files a parser-fix issue, not a new-kind
    feature request."""
    div = make_div(description="XQAA (US99990A3095) Cash Dividend USD 0.35 per Share")
    err = UnrecognisedDistributionTypeError.from_dividend(div, violation="missing_kind_prefix")
    msg = str(err)
    assert msg == missing_kind_prefix_message()
    # The old 'malformed-dividend-description: …' marker and the quoted
    # description printed the row's values (#1047).
    assert "malformed-dividend-description" not in msg
    _assert_absent(msg, "XQAA", "US99990A3095", "2025-06-15", "0.35")
    assert "unsupported-distribution-type:" not in msg
    assert "feat(os):" not in msg


def test_unrecognised_distribution_type_error_str_for_missing_usd_marker_violation() -> None:
    """The ``missing_usd_marker`` discriminator must produce a message
    that names the parser-input problem (not "novel distribution
    kind") and cites the multi-currency follow-up issue #52."""
    div = make_div(description="XQAA(US99990A3095) Cash Dividend")
    err = UnrecognisedDistributionTypeError.from_dividend(div, violation="missing_usd_marker")
    msg = str(err)
    assert msg == missing_usd_marker_message()
    assert "#52" in msg
    # The old 'malformed-dividend-description: …' marker and the quoted
    # type-slice printed the row's values (#1047).
    assert "malformed-dividend-description" not in msg
    _assert_absent(msg, "XQAA", "US99990A3095", "2025-06-15")
    assert "unsupported-distribution-type:" not in msg
    assert "feat(os):" not in msg


def test_from_dividend_keeps_only_violation_and_row_index() -> None:
    """#1285: the factory copies nothing from the row but its index."""
    div = make_div(
        description="XQAA(US99990A3095) Foo Dividend USD 0.35 per Share",
        source_row_index=7,
    )
    err = UnrecognisedDistributionTypeError.from_dividend(div, violation="unsupported_kind")
    assert err == UnrecognisedDistributionTypeError(
        violation="unsupported_kind", source_row_index=7
    )


# ---- ScheduleOsComputeResult ---------------------------------------------


def test_schedule_os_compute_result_holds_two_buckets() -> None:
    result = ScheduleOsComputeResult(
        lines=[],
        domestic_lines=[],
    )
    assert result.lines == []
    assert result.domestic_lines == []


# ---- #154 source_row_index threading into the error ---------------------


def test_unrecognised_distribution_type_error_str_includes_row_index_when_present() -> None:
    """#154: when the source Dividend carries a source_row_index (the
    production parser path threads it per #154), the error message
    includes the 0-based row position + 1-based human-readable
    descriptor so the user can jump straight to the offending row in
    their IBKR Activity Statement CSV.
    """
    div = make_div(
        description="XQAA(US99990A3095) Foo Dividend USD 0.35 per Share",
        source_row_index=7,
    )
    err = UnrecognisedDistributionTypeError.from_dividend(
        div,
        violation="unsupported_kind",
    )
    assert err.source_row_index == 7
    rendered = str(err)
    assert rendered == unsupported_kind_message(7)
    assert "Dividends-section row index 7" in rendered
    assert "0-based" in rendered
    assert "so data row 8" in rendered  # 1-based descriptor for human comfort


@pytest.mark.parametrize(
    ("row_index", "clause"),
    [
        (0, " (Dividends-section row index 0; 0-based, so data row 1)"),
        (10, " (Dividends-section row index 10; 0-based, so data row 11)"),
    ],
)
def test_unrecognised_distribution_type_error_row_clause_has_no_ordinal_suffix(
    row_index: int, clause: str
) -> None:
    """#1069: the row clause gives the 1-based row as a plain number, with
    no ordinal suffix (the old text printed "the 1th data row")."""
    div = make_div(
        description="XQAA(US99990A3095) Foo Dividend USD 0.35 per Share",
        source_row_index=row_index,
    )
    rendered = str(
        UnrecognisedDistributionTypeError.from_dividend(div, violation="unsupported_kind")
    )
    assert rendered.startswith(
        f"Unrecognised dividend distribution type in the Dividends section{clause}: "
    )
    assert rendered == unsupported_kind_message(row_index)
    assert "th data row" not in rendered


def test_unrecognised_distribution_type_error_str_omits_row_when_absent() -> None:
    """#154: when source_row_index is None (test fixtures or non-
    parser construction paths), the error message omits the row
    clause entirely. The fixed advice to locate the row by its
    Dividends-section row index stays (#1047)."""
    div = make_div(
        description="XQAA(US99990A3095) Foo Dividend USD 0.35 per Share",
    )
    err = UnrecognisedDistributionTypeError.from_dividend(
        div,
        violation="unsupported_kind",
    )
    assert err.source_row_index is None
    rendered = str(err)
    assert rendered == unsupported_kind_message()
    assert " (Dividends-section row index" not in rendered
    assert "0-based" not in rendered


def test_unrecognised_distribution_type_error_str_includes_row_for_missing_kind_prefix() -> None:
    """#154 symmetric coverage: missing_kind_prefix violation also
    renders the row clause when present."""
    div = make_div(
        description="malformed without prefix structure",
        source_row_index=3,
    )
    err = UnrecognisedDistributionTypeError.from_dividend(div, violation="missing_kind_prefix")
    assert str(err) == missing_kind_prefix_message(3)


def test_unrecognised_distribution_type_error_str_includes_row_for_missing_usd_marker() -> None:
    """#154 symmetric coverage: missing_usd_marker violation also
    renders the row clause when present."""
    div = make_div(
        description="XQAA(US99990A3095) Cash Dividend",  # no " USD "
        source_row_index=5,
    )
    err = UnrecognisedDistributionTypeError.from_dividend(div, violation="missing_usd_marker")
    assert str(err) == missing_usd_marker_message(5)


def test_unrecognised_distribution_type_error_str_for_malformed_type_slice() -> None:
    """#1096: the malformed_type_slice violation renders the row clause
    when present and omits it otherwise."""
    div = make_div(description="XQAA(US99990A3095) Cash Dividend USD 0.25 per Share - US Tax")
    err = UnrecognisedDistributionTypeError.from_dividend(div, violation="malformed_type_slice")
    assert str(err) == malformed_type_slice_message()
    assert str(replace(err, source_row_index=5)) == malformed_type_slice_message(5)
    assert err == UnrecognisedDistributionTypeError(violation="malformed_type_slice")


# ---- #1047: the error never echoes the row ------------------------------

# A column-shifted or malformed Dividends row can put the holder's name and
# account into the Description.
_LEAKY_WORDS = "Synthetic Holder Two U1234567 ANN"
# What every row below puts in its Description or Dividend: the words, the
# symbol, ISIN, payment date (as a date's repr prints it) and amount.
_ROW_MARKERS = (
    "Synthetic Holder Two",
    "U1234567",
    "ANN",
    "ZZSYN",
    "US99971T1035",
    "2025, 3, 1",
    "1234567.89",
)


@pytest.mark.parametrize("row_index", [None, 7], ids=["no-row", "row-7"])
@pytest.mark.parametrize(
    ("description", "violation", "expected"),
    [
        pytest.param(
            f"ZZSYN(US99971T1035) {_LEAKY_WORDS} Payout USD 1234567.89 per Share",
            "unsupported_kind",
            unsupported_kind_message,
            id="unsupported-kind",
        ),
        pytest.param(
            f"{_LEAKY_WORDS} Cash Dividend USD 1234567.89 per Share",
            "missing_kind_prefix",
            missing_kind_prefix_message,
            id="missing-kind-prefix",
        ),
        pytest.param(
            f"ZZSYN(US99971T1035) {_LEAKY_WORDS} Cash Dividend 1234567.89 per Share",
            "missing_usd_marker",
            missing_usd_marker_message,
            id="missing-usd-marker",
        ),
        pytest.param(
            f"ZZSYN(US99971T1035) Cash Dividend USD 1234567.89 per Share ({_LEAKY_WORDS} Payout)",
            "malformed_type_slice",
            malformed_type_slice_message,
            id="malformed-type-slice",
        ),
    ],
)
def test_unrecognised_distribution_type_error_prints_no_row_values(
    description: str,
    violation: str,
    expected: Callable[[int | None], str],
    row_index: int | None,
) -> None:
    """Each violation refuses with its exact fixed text: the row index
    when known, the rule and the GitHub link, but not the Description
    (with a name and an account in it), symbol, ISIN, payment date or
    per-share amount. The error keeps only the violation and the row
    index, so none of them is in its fields, repr or args either
    (#1285)."""
    div = make_div(
        symbol="ZZSYN",
        isin="US99971T1035",
        payment_date=date(2025, 3, 1),
        amount="1234567.89",
        description=description,
        source_row_index=row_index,
    )
    with pytest.raises(UnrecognisedDistributionTypeError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=date(2024, 4, 1),
            financial_year_end=date(2025, 3, 31),
            fx_policy=AuditedFxStub(Decimal("85")),
        )
    err = ei.value
    assert err.violation == violation
    assert err.source_row_index == row_index
    assert err.args == ()
    assert_prints_no_marker(
        err, markers=(*_ROW_MARKERS, *(["Payout"] if "Payout" in description else []))
    )
    msg = str(err)
    assert msg == expected(row_index)
    assert "https://github.com/jay-tau/ibkr-fa/issues/new/choose" in msg
    _assert_absent(
        msg,
        "Synthetic Holder Two",
        "U1234567",
        "ZZSYN",
        "US99971T1035",
        "2025-03-01",
        "1234567.89",
        "Payout",
        "unsupported-distribution-type",
        "malformed-dividend-description",
    )
    _assert_absent_token(msg, "ANN")


# ---- #160: fx_rate_publication_date threading via Rule115Policy ----------


def test_compute_with_rule115_policy_threads_publication_date_no_carry_back() -> None:
    """A Jun 15 2025 dividend resolves to specified-date 2025-05-31
    (Sat, published). The compute layer must thread the same date as
    the rate publication date through to ScheduleOsDividendLine
    (no carry-back occurred).
    """
    fx = Rule115Policy()
    div = make_div(payment_date=date(2025, 6, 15))
    result = compute_schedule_os_dividend_lines(
        [div],
        financial_year_start=date(2025, 4, 1),
        financial_year_end=date(2026, 3, 31),
        fx_policy=fx,
    )
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.fx_specified_date == date(2025, 5, 31)
    assert line.fx_rate_publication_date == date(2025, 5, 31)
    # 2025-05-31 TT BUY = 85.10
    assert line.fx_rate_inr_per_usd == Decimal("85.10")


def test_compute_with_rule115_policy_threads_publication_date_with_carry_back() -> None:
    """The Sep 15 / Aug 31 example from issue #160.

    A Sep 15 2025 dividend has specified-date 2025-08-31 (Sun,
    absent), with carry-back to 2025-08-30 (Sat, published). The
    compute layer must thread BOTH dates: fx_specified_date stays
    the statutory date, fx_rate_publication_date surfaces the
    actual SBI archive date so the review CSV can show an auditor
    a date-rate pair that matches an actual SBI row.
    """
    fx = Rule115Policy()
    div = make_div(payment_date=date(2025, 9, 15))
    result = compute_schedule_os_dividend_lines(
        [div],
        financial_year_start=date(2025, 4, 1),
        financial_year_end=date(2026, 3, 31),
        fx_policy=fx,
    )
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.fx_specified_date == date(2025, 8, 31)
    assert line.fx_rate_publication_date == date(2025, 8, 30)
    # 2025-08-30 TT BUY = 87.70
    assert line.fx_rate_inr_per_usd == Decimal("87.70")


def test_dividend_line_rejects_publication_date_after_specified_date() -> None:
    """ScheduleOsDividendLine.__post_init__ enforces
    fx_rate_publication_date <= fx_specified_date — the
    counterpart to the Rule115Resolution invariant.
    """
    with pytest.raises(InvariantError, match="fx_rate_publication_date"):
        ScheduleOsDividendLine(
            symbol=Ticker("XQCA"),
            isin=Isin("US99972B1017"),
            issuer_country_iso2="US",
            payment_date=date(2025, 6, 15),
            fx_specified_date=date(2025, 5, 31),
            fx_rate_publication_date=date(2025, 6, 1),
            fx_rate_inr_per_usd=Decimal("85"),
            amount_gross_usd=Decimal("1"),
            amount_gross_inr=Decimal("85"),
            description="XQCA(US99972B1017) Cash Dividend USD 0.35 per Share",
            kind=DistributionKind.CASH_DIVIDEND,
        )


# ---- Dividend left at the kind/type_slice defaults -----------------------


@pytest.mark.parametrize(
    "description",
    [
        "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share",
        "XQAA(US99990A3095) Return Of Capital USD 0.50 per Share",
    ],
)
def test_default_kind_and_type_slice_raise_missing_kind_prefix_for_well_formed_description(
    description: str,
) -> None:
    """A :class:`Dividend` left at its ``kind`` / ``type_slice`` defaults
    fails closed with ``missing_kind_prefix`` even when its description
    is well-formed: compute never classifies descriptions (#606)."""
    div = Dividend(
        symbol=Ticker("XQAA"),
        isin=Isin("US99990A3095"),
        payment_date=date(2025, 6, 15),
        amount_gross=Decimal("1.00"),
        currency="USD",
        description=description,
    )
    assert div.kind == DistributionKind.UNRECOGNISED
    assert div.type_slice == ""

    with pytest.raises(UnrecognisedDistributionTypeError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=AuditedFxStub(Decimal("85")),
        )
    assert ei.value.violation == "missing_kind_prefix"


def test_legacy_dividend_with_malformed_description_still_raises_missing_kind_prefix() -> None:
    """A defaulted :class:`Dividend` whose description lacks the
    structural SYMBOL(ISIN) prefix also triggers ``missing_kind_prefix``,
    so the parser-bypass safety net is preserved."""
    fx = AuditedFxStub(Decimal("85"))
    div = Dividend(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),
        payment_date=date(2025, 6, 15),
        amount_gross=Decimal("1.00"),
        currency="USD",
        description="Cash Dividend USD 0.10 per Share",  # missing SYMBOL(ISIN) prefix
    )
    with pytest.raises(UnrecognisedDistributionTypeError) as ei:
        compute_schedule_os_dividend_lines(
            [div],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=fx,
        )
    assert ei.value.violation == "missing_kind_prefix"


def test_legacy_positional_source_row_index_binds_to_source_row_index_not_kind() -> None:
    """T5 codex P2 finding on PR #189 round 2: pre-#153 callers used
    the positional form ``Dividend(..., source_row_index)`` (the 7th
    positional arg was source_row_index). PR #189 round 1 inserted
    the new ``kind`` / ``type_slice`` fields BEFORE source_row_index,
    which silently re-bound that positional integer to ``kind``,
    routing the row to ``unsupported_kind`` instead of normal OS
    dispatch.

    Round 2 reordered the fields (source_row_index stays at position
    7; kind / type_slice moved to positions 8 / 9, both with defaults
    so they're optional). This test pins the positional binding so a
    future refactor doesn't accidentally re-introduce the regression.
    """
    div = Dividend(
        Ticker("XQAA"),
        Isin("US99990A3095"),
        date(2025, 6, 15),
        Decimal("1.00"),
        "USD",
        "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share",
        7,  # 7th positional arg — MUST bind to source_row_index, not kind
    )
    assert div.source_row_index == 7
    # The new fields stay at their UNRECOGNISED / "" defaults.
    assert div.kind == DistributionKind.UNRECOGNISED
    assert div.type_slice == ""


# ============================================================================
# PR #223: ScheduleOsDomesticDividendLine.adr_underlying_issuer_name
# whitespace-only string gap
# ============================================================================
#
# The existing both-None-or-both-non-None coupling at
# compute/schedule_os.py:510-526 counts an empty / whitespace string as
# "present", letting a row through with adr_source_isin populated but
# adr_underlying_issuer_name = "" — which would render a blank audit-trail
# cell on the FA-A3 disclosure. The new whitespace invariant rejects empty
# / pure-whitespace values at construction time. Parallel to the
# Form67Entry.section_90_dtaa_article whitespace check at
# models/form67_entry.py:360-369 (PR #221 r4).


def _make_adr_routed_line(
    *,
    adr_underlying_issuer_name: str | None = "SAMPLE ISSUER CD Ltd",
) -> ScheduleOsDomesticDividendLine:
    """Build an ADR-routed (issue #183) ScheduleOsDomesticDividendLine
    fixture for #223 invariant testing. ADR-routed case requires
    issuer_country_iso2 == "IN" with isin NOT starting with "IN" plus
    both adr_* fields populated."""
    return ScheduleOsDomesticDividendLine(
        symbol=Ticker("XQCD"),
        isin=Isin("US99972A1034"),  # ADR ISIN — does NOT start with "IN"
        issuer_country_iso2="IN",
        payment_date=date(2025, 7, 1),
        fx_specified_date=date(2025, 6, 30),
        fx_rate_publication_date=date(2025, 6, 30),
        fx_rate_inr_per_usd=Decimal("83.50"),
        amount_gross_usd=Decimal("12.34"),
        amount_gross_inr=Decimal("1030.39"),
        description="XQCD(US99972A1034) Cash Dividend USD 0.3500 per Share (Ordinary Dividend)",
        kind=DistributionKind.CASH_DIVIDEND,
        adr_source_isin=Isin("US99972A1034"),
        adr_underlying_issuer_name=adr_underlying_issuer_name,
    )


def test_domestic_line_rejects_empty_adr_underlying_issuer_name() -> None:
    """PR #223 forward: an empty string for adr_underlying_issuer_name
    bypasses the both-None-or-both-non-None coupling (which counts
    "" as "present") and would silently render a blank audit-trail
    cell on FA-A3. The new invariant rejects at construction."""
    with pytest.raises(InvariantError, match="non-empty string when provided"):
        _make_adr_routed_line(adr_underlying_issuer_name="")


def test_domestic_line_rejects_whitespace_only_adr_underlying_issuer_name() -> None:
    """PR #223: whitespace-only strings (spaces, tabs) are similarly
    rejected — strip-then-empty is the test."""
    with pytest.raises(InvariantError, match="non-empty string when provided"):
        _make_adr_routed_line(adr_underlying_issuer_name="   ")


def test_domestic_line_rejects_tab_only_adr_underlying_issuer_name() -> None:
    with pytest.raises(InvariantError, match="non-empty string when provided"):
        _make_adr_routed_line(adr_underlying_issuer_name="\t\n")


def test_domestic_line_accepts_valid_adr_underlying_issuer_name() -> None:
    """The legitimate ADR-routed case still works."""
    line = _make_adr_routed_line(adr_underlying_issuer_name="SAMPLE ISSUER CD Ltd")
    assert line.adr_underlying_issuer_name == "SAMPLE ISSUER CD Ltd"


def test_domestic_line_accepts_none_adr_underlying_issuer_name_with_none_adr_source_isin() -> None:
    """The legitimate direct-Indian-share case (both ADR fields None)
    is unaffected by the whitespace invariant — the invariant is
    only triggered when adr_underlying_issuer_name is not None."""
    line = ScheduleOsDomesticDividendLine(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),  # IN-prefixed (direct share)
        issuer_country_iso2="IN",
        payment_date=date(2025, 7, 1),
        fx_specified_date=date(2025, 6, 30),
        fx_rate_publication_date=date(2025, 6, 30),
        fx_rate_inr_per_usd=Decimal("83.50"),
        amount_gross_usd=Decimal("12.34"),
        amount_gross_inr=Decimal("1030.39"),
        description="XQCD(IN99970W1077) Cash Dividend USD 0.3500 per Share (Ordinary Dividend)",
        kind=DistributionKind.CASH_DIVIDEND,
        adr_source_isin=None,
        adr_underlying_issuer_name=None,
    )
    assert line.adr_underlying_issuer_name is None
    assert line.adr_source_isin is None


# ---- line kind invariant (#606) ------------------------------------------


@pytest.mark.parametrize(
    "kind", [DistributionKind.RETURN_OF_CAPITAL, DistributionKind.UNRECOGNISED]
)
def test_dividend_line_rejects_non_allow_listed_kind(kind: DistributionKind) -> None:
    with pytest.raises(InvariantError, match="allow-listed ordinary-dividend kind"):
        replace(_line(), kind=kind)


@pytest.mark.parametrize(
    "kind", [DistributionKind.RETURN_OF_CAPITAL, DistributionKind.UNRECOGNISED]
)
def test_domestic_line_rejects_non_allow_listed_kind(kind: DistributionKind) -> None:
    with pytest.raises(InvariantError, match="allow-listed ordinary-dividend kind"):
        replace(_make_adr_routed_line(), kind=kind)
