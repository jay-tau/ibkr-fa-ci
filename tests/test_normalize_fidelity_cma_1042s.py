"""Exact whole-dollar reconciliation tests for Fidelity CMA Form 1042-S."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, Inexact, Rounded, localcontext

import pytest

from ibkr_fa.models.form_1042s import (
    Form1042SAmount,
    Form1042SAmountKind,
)
from ibkr_fa.normalize.fidelity_cma.form_1042s import (
    Cma1042SReconciliationError,
    reconcile_1042s,
)
from ibkr_fa.parser.fidelity_cma import (
    parse_year_end_income_summary,
    validate_statement_bundle,
)
from ibkr_fa.parser.fidelity_cma_1042s import (
    Form1042SPacket,
    Form1042SRecord,
)
from tests._fidelity_cma_1042s_reconcile import (
    JANUARY_2025_REFUSAL,
    _dollars,
    _packet,
    _record,
    _statements,
    _year_end,
)

_ACCOUNT = "Z99-999999"
_HOLDER = "TAX PAYER"


def test_exact_sum_then_round_reconciliation() -> None:
    statements = _statements()
    result = reconcile_1042s(
        validate_statement_bundle(statements), _year_end(), _packet(), tax_year=2025
    )

    assert [bucket.statement_event_count for bucket in result.buckets] == [
        15,
        2,
        8,
    ]
    assert result.withholding_event_count == 8
    assert statements == _statements()
    assert "10.48" not in repr(result)

    with localcontext() as context:
        context.prec = 1
        low_precision = reconcile_1042s(
            validate_statement_bundle(statements),
            _year_end(),
            _packet(),
            tax_year=2025,
        )
    assert low_precision.withholding_event_count == 8


def test_half_up_boundary_and_mismatch_refuse() -> None:
    statements = list(_statements())
    january = statements[0]
    changed = list(january.income_credits)
    changed[0] = replace(changed[0], amount_usd=Decimal("0.09"))
    statements[0] = replace(january, income_credits=tuple(changed))
    year_end = replace(_year_end(), interest_usd=Decimal("1.49"))

    with pytest.raises(Cma1042SReconciliationError, match="Box 2"):
        reconcile_1042s(validate_statement_bundle(statements), year_end, _packet(), tax_year=2025)


@pytest.mark.parametrize("trap_signals", [False, True])
def test_printed_withholding_sum_ignores_decimal_context(trap_signals: bool) -> None:
    statements = list(_statements())
    march = statements[2]
    statements[2] = replace(
        march,
        income_credits=tuple(
            replace(item, amount_usd=Decimal("6.00")) for item in march.income_credits
        ),
        withholdings=tuple(
            replace(item, amount_usd=Decimal("-1.50")) for item in march.withholdings
        ),
    )
    bundle = validate_statement_bundle(statements)
    year_end = replace(
        _year_end(), dividends_usd=Decimal("50.48"), withholding_usd=Decimal("-12.00")
    )
    packet = replace(
        _packet(),
        records=(*_packet().records[:2], _record("06:25.00", "06", "00", "25.00", 48, 12)),
    )
    expected = reconcile_1042s(bundle, year_end, packet, tax_year=2025)

    with localcontext() as context:
        context.prec = 1
        context.traps[Inexact] = trap_signals
        context.traps[Rounded] = trap_signals
        assert reconcile_1042s(bundle, year_end, packet, tax_year=2025) == expected


def test_coverage_identity_and_year_end_fail_closed() -> None:
    with pytest.raises(Cma1042SReconciliationError, match="following-January"):
        reconcile_1042s(
            validate_statement_bundle(_statements()[:-1]), _year_end(), _packet(), tax_year=2025
        )

    secret = "SECRET HOLDER"
    packet = replace(_packet(), account_holder=secret)
    with pytest.raises(Cma1042SReconciliationError) as caught:
        reconcile_1042s(
            validate_statement_bundle(_statements()), _year_end(), packet, tax_year=2025
        )
    assert secret not in str(caught.value)

    wrong_year_end = replace(_year_end(), interest_usd=Decimal("1.49"))
    with pytest.raises(Cma1042SReconciliationError, match="year-end"):
        reconcile_1042s(
            validate_statement_bundle(_statements()),
            wrong_year_end,
            _packet(),
            tax_year=2025,
        )


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"coverage_clauses": ("the caller's first clause", "its second")}],
    ids=["no-clauses", "caller-clauses"],
)
def test_late_start_with_a_carried_balance_names_january_and_the_callers_clauses(
    kwargs: dict[str, tuple[str, ...]],
) -> None:
    """Statements from April 2025 that carry a balance could miss January to
    March: the refusal names the tax year's start and what to add, then the
    caller's clauses (#1515)."""
    with pytest.raises(Cma1042SReconciliationError) as caught:
        reconcile_1042s(
            validate_statement_bundle(_statements()[3:]),
            _year_end(),
            _packet(),
            tax_year=2025,
            **kwargs,
        )
    assert str(caught.value) == "; ".join(
        (JANUARY_2025_REFUSAL, *kwargs.get("coverage_clauses", ()))
    )


def test_parse_year_end_income_summary() -> None:
    text = f"""\
2025 YEAR-END INVESTMENT REPORT
January 1, 2025 - December 31, 2025
FIDELITY® CASH MANAGEMENT ACCOUNT {_HOLDER} -
INDIVIDUAL TOD
Account Number: {_ACCOUNT}
Account # {_ACCOUNT}
Account Summary {_HOLDER} - INDIVIDUAL - TOD
Taxes Withheld -2.00
Ordinary Dividends
Dividends 10.48
Ending Account Value as of Dec 31, 2025 $100.00 Interest 1.50
Holdings {_HOLDER} - INDIVIDUAL - TOD
Brokerage services provided by Fidelity Brokerage Services LLC.
Brokerage accounts carried by National Financial Services LLC.
"""
    summary = parse_year_end_income_summary(
        text,
        expected_currency="USD",
        expected_account_id=_ACCOUNT,
    )
    assert summary.interest_usd == Decimal("1.50")
    assert summary.dividends_usd == Decimal("10.48")
    assert summary.withholding_usd == Decimal("-2.00")


def _packet_with(key: str, **changes: object) -> Form1042SPacket:
    packet = _packet()
    record_key = changes.pop("record_key", key)
    return replace(
        packet,
        records=tuple(
            replace(record, record_key=record_key, form=replace(record.form, **changes))
            if record.record_key == key
            else record
            for record in packet.records
        ),
    )


_UNSUPPORTED = "Form 1042-S record contains unsupported tax semantics"


@pytest.mark.parametrize(
    ("key", "changes", "message"),
    [
        ("06:25.00", {"record_key": "06:30.00"}, "unexpected income-code/withholding-rate records"),
        ("01:0.00", {"income_code": "06"}, _UNSUPPORTED),
        ("01:0.00", {"chapter3_exemption_code": "00"}, _UNSUPPORTED),
        ("06:25.00", {"chapter3_tax_rate_percent": Decimal("30.00")}, _UNSUPPORTED),
        ("01:0.00", {"chapter4_exemption_code": "16"}, _UNSUPPORTED),
        ("01:0.00", {"chapter4_tax_rate_percent": Decimal("1.00")}, _UNSUPPORTED),
        ("01:0.00", {"withholding_allowance": _dollars(1)}, _UNSUPPORTED),
        ("01:0.00", {"net_income": _dollars(1)}, _UNSUPPORTED),
        ("01:0.00", {"tax_withheld_other_agents": _dollars(1)}, _UNSUPPORTED),
        ("01:0.00", {"overwithheld_tax_repaid": _dollars(1)}, _UNSUPPORTED),
        ("01:0.00", {"tax_paid_by_agent": _dollars(1)}, _UNSUPPORTED),
        ("06:25.00", {"total_withholding_credit": _dollars(3)}, _UNSUPPORTED),
        (
            "01:0.00",
            {"federal_tax_withheld": _dollars(1), "total_withholding_credit": _dollars(1)},
            "zero-rate Form 1042-S record contains withholding",
        ),
        (
            "06:0.00",
            {"federal_tax_withheld": _dollars(1), "total_withholding_credit": _dollars(1)},
            "zero-rate Form 1042-S record contains withholding",
        ),
    ],
)
def test_record_checks_refuse_each_unsupported_box(
    key: str,
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(Cma1042SReconciliationError) as caught:
        reconcile_1042s(
            validate_statement_bundle(_statements()),
            _year_end(),
            _packet_with(key, **changes),
            tax_year=2025,
        )
    assert str(caught.value) == f"Fidelity CMA Form 1042-S reconciliation refused: {message}"


def _blank_withheld(record: Form1042SRecord) -> Form1042SRecord:
    return replace(
        record,
        form=replace(
            record.form,
            federal_tax_withheld=Form1042SAmount(Form1042SAmountKind.BLANK, None),
        ),
    )


@pytest.mark.parametrize(
    "records",
    [
        (*_packet().records[:2], _record("06:25.00", "06", "00", "25.00", 8, 3)),
        (*_packet().records, _packet().records[-1]),
        (*_packet().records[:2], _blank_withheld(_packet().records[-1]), _packet().records[-1]),
        (
            *_packet().records[:2],
            replace(
                _packet().records[-1],
                form=replace(
                    _packet().records[-1].form,
                    federal_tax_withheld=Form1042SAmount(
                        Form1042SAmountKind.AMOUNT, Decimal("0.01")
                    ),
                ),
            ),
            _packet().records[-1],
        ),
    ],
)
def test_withholding_must_match_box_7a_once(records: tuple[Form1042SRecord, ...]) -> None:
    with pytest.raises(Cma1042SReconciliationError) as caught:
        reconcile_1042s(
            validate_statement_bundle(_statements()),
            _year_end(),
            replace(_packet(), records=records),
            tax_year=2025,
        )
    assert str(caught.value) == (
        "Fidelity CMA Form 1042-S reconciliation refused: "
        "rounded statement withholding does not match Form 1042-S"
    )
