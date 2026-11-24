"""Focused checks for the reconciled Robinhood long-option adapter."""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from datetime import date, timedelta
from decimal import ROUND_DOWN, Context, Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.lot_matching import FifoPool
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionBlocker,
    RobinhoodOptionBlockerKind,
    RobinhoodOptionCapitalGainBucket,
    RobinhoodOptionFaDisposition,
    RobinhoodOptionReconstruction,
    RobinhoodOptionsError,
    RobinhoodOptionsResult,
    RobinhoodOptionTradeEvidence,
    _match_lots,
    compute_robinhood_option_business_filing,
    compute_robinhood_option_business_fsi,
    compute_robinhood_options,
    option_blocker_message,
    option_blocker_summary,
    option_treatment_warnings,
)
from ibkr_fa.compute.schedule_cg_table_f import ScheduleCgTableFCase, ScheduleCgTableFRow
from ibkr_fa.config import (
    RobinhoodConfig,
    RobinhoodOptionTreatment,
)
from ibkr_fa.exceptions import IbkrFaError, InvariantError
from ibkr_fa.normalize.robinhood.accounts import (
    RobinhoodAccountsReconciliation,
)
from ibkr_fa.normalize.robinhood.inventory import RobinhoodPositionReference
from ibkr_fa.normalize.robinhood.security import RobinhoodSecurityEventReference
from ibkr_fa.output import _writer_contract
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.portal_labels import (
    PORTAL_COLUMN_HEADERS_CG_TABLE_F,
    PORTAL_ROW_LABEL_CG_TABLE_F_LTCG_BY_ITR_FORM,
    PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM,
)
from ibkr_fa.output.robinhood_options_csv import write_robinhood_options_csv
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatementPosition,
    RobinhoodStatementSection,
    SourceLocation,
)
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_robinhood_outputs import write_options
from tests._crash_reports import assert_prints_no_marker
from tests._exact_refusals import INEXACT_SUM
from tests._fx_stubs import AuditedFxStub
from tests._private_checks import private_test
from tests._robinhood_option_inputs import OPTION_FINANCIAL_YEAR as _FINANCIAL_YEAR
from tests._robinhood_option_inputs import OPTION_FY_END as _FY_END
from tests._robinhood_option_inputs import OPTION_FY_START as _FY_START
from tests._robinhood_option_inputs import SYNTH_DESCRIPTION as _SYNTH_DESCRIPTION
from tests._robinhood_option_inputs import SYNTH_EXPIRATION as _SYNTH_EXPIRATION
from tests._robinhood_option_inputs import SYNTH_MARKERS as _SYNTH_MARKERS
from tests._robinhood_option_inputs import SYNTH_UNDERLYING as _SYNTH_UNDERLYING
from tests._robinhood_option_inputs import StandInStatement as _Statement
from tests._robinhood_option_inputs import closed_option_events as _closed_events
from tests._robinhood_option_inputs import compute_synthetic_options as _compute
from tests._robinhood_option_inputs import option_account as _account
from tests._robinhood_option_inputs import option_cash_event as _cash_event
from tests._robinhood_option_inputs import option_classification as _classification
from tests._robinhood_option_inputs import option_event as _option
from tests._robinhood_option_inputs import option_paired as _paired
from tests._robinhood_option_inputs import option_statement_position as _position
from tests._robinhood_option_inputs import synth_option_trade as _synth_trade
from tests._robinhood_private import (
    load_private_accounts_reconciliation,
    load_private_report_records,
)
from tests._robinhood_refusals import OPTION_BLOCKER_MESSAGES


def _workpaper_rows(
    result: RobinhoodOptionsResult,
    tmp_path: Path,
    *,
    itr_form: ItrForm = "ITR-3",
) -> list[dict[str, str]]:
    path = write_options(
        result,
        output_dir=tmp_path,
        financial_year=_FINANCIAL_YEAR,
        itr_form=itr_form,
    )
    assert path is not None
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_capital_election_reconstructs_exact_manual_evidence(tmp_path: Path) -> None:
    result = _compute(
        _paired(_closed_events()),
        RobinhoodOptionTreatment.CAPITAL_GAINS,
    )

    assert result.blockers == ()
    assert len(result.trades) == 2
    assert len(result.lot_slices) == 1
    matched = result.lot_slices[0]
    assert matched.cost_usd == Decimal("199.99")
    assert matched.net_proceeds_usd == Decimal("300.00")
    assert matched.net_pnl_usd == Decimal("100.01")
    assert result.open_lots == ()
    assert len(result.validated_valuation_anchors) == 1
    assert result.classifications[0].schedule_fa_disposition is (
        RobinhoodOptionFaDisposition.TABLE_D
    )
    line = result.capital_gain_lines[0]
    assert line.bucket is RobinhoodOptionCapitalGainBucket.STCG_OTHER_ASSETS
    assert line.cost_inr == Decimal("15999.20")
    assert line.net_proceeds_inr == Decimal("24000.00")
    assert line.net_gain_inr == Decimal("8000.80")
    assert result.required_itr_form is None
    assert result.manual_work_required
    assert result.a2_gross_credit_references == (matched.close_references[0],)

    rows = _workpaper_rows(result, tmp_path)
    assert not any(row["record_type"] == "itr-3-note" for row in rows)
    assert option_treatment_warnings(result) == ()


def _capital_result(*, long_term: bool) -> RobinhoodOptionsResult:
    """:func:`_closed_events`' gain under capital treatment: bought May 1, 2025,
    or, ``long_term``, May 1, 2023; sold June 1, 2025, for a gain of ₹8000.80."""
    if not long_term:
        return _compute(_paired(_closed_events()), RobinhoodOptionTreatment.CAPITAL_GAINS)
    events = (
        _option(
            RobinhoodEventKind.OPTION_BUY_TO_OPEN,
            2,
            date(2023, 5, 1),
            date(2023, 5, 2),
        ),
        _option(
            RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
            3,
            date(2025, 6, 1),
            date(2025, 6, 2),
            price="1.50",
            amount="300.00",
        ),
    )
    statements = (
        _Statement(date(2023, 5, 1), date(2023, 5, 31), (_position(1),)),
        _Statement(date(2025, 5, 1), date(2025, 5, 31), (_position(1),)),
        _Statement(date(2025, 6, 1), date(2025, 6, 30), ()),
    )
    paired = _paired(
        events,
        statements=statements,
        report_start=date(2023, 5, 1),
    )
    return _compute(
        paired,
        RobinhoodOptionTreatment.CAPITAL_GAINS,
        opening_date=date(2023, 1, 1),
    )


@pytest.mark.parametrize(
    ("long_term", "itr_form", "expected_target", "wrong_target"),
    [
        (False, "ITR-2", "A / 5(e)", "A / 6(g)"),
        (False, "ITR-3", "A / 6(g)", "A / 5(e)"),
        (True, "ITR-2", "B / 8(e)", "B / 9(e)"),
        (True, "ITR-3", "B / 9(e)", "B / 8(e)"),
    ],
)
def test_capital_workpaper_target_follows_term_and_itr_form(
    tmp_path: Path,
    long_term: bool,
    itr_form: ItrForm,
    expected_target: str,
    wrong_target: str,
) -> None:
    result = _capital_result(long_term=long_term)

    [row] = [
        row
        for row in _workpaper_rows(result, tmp_path, itr_form=itr_form)
        if row["record_type"] == "capital-gain"
    ]
    assert row["treatment_or_kind"] == ("ltcg-other-assets" if long_term else "stcg-other-assets")
    assert expected_target in row["manual_target"]
    assert wrong_target not in row["manual_target"]
    assert "Schedule CFL (if a loss)" in row["manual_target"]
    assert "Schedule FSI (CG)" in row["manual_target"]
    assert "Schedule FA Table D" in row["manual_target"]


# ---- Schedule CG Table F date bands for option capital gains (#1362) --------

_STCG = RobinhoodOptionCapitalGainBucket.STCG_OTHER_ASSETS
_LTCG = RobinhoodOptionCapitalGainBucket.LTCG_OTHER_ASSETS
_TABLE_F_COLUMNS = ("i", "ii", "iii", "iv", "v")
_COMPUTED_NOTE = (
    "Robinhood option gains closed in this band, net, before any set-off; the five "
    "bands add up to these option gains' exact total rounded half up, or are 0 when "
    "it isn't positive."
)
_LOSS_ACROSS_BANDS_NOTE = (
    "Blank: a date band nets a loss and two or more bands net gains. Choose how the loss "
    "lowers them: no band below 0, and the five add up to this section's result."
)
_PAST_THE_BOUND_NOTE = (
    "Blank: these option gains round past the CBDT 14-digit bound, so no band is computed."
)
# The SHA-256 of robinhood-options.csv on e992964, before Table F rows (#1362).
_MAIN_WORKPAPER_SHA256 = {
    (False, "ITR-2"): "c0191457c8281fb0b9b1e362e982ee28fa2dbd2a069c4eb498256b40b82e54d1",
    (False, "ITR-3"): "25afd4028fed87e94203151c54d6192d1a6bf354ccd4325da51fb8097b545680",
    (True, "ITR-2"): "aba42f1252c15a787e552f5020d1716a5a998826be47c58912c3c6f10509e773",
    (True, "ITR-3"): "59514492b63b5ea893624bb0d8dbdd4848188ce9e8c15159117eae9cca256249",
}
_MAIN_BUSINESS_WORKPAPER_SHA256 = "d8d8f95cfeb9827585c86963f336c53c28595c5cf35e8ce5ce3f3cf02ed2b995"


def _computed(*bands: int) -> ScheduleCgTableFRow:
    return ScheduleCgTableFRow(
        ScheduleCgTableFCase.COMPUTED, tuple(Decimal(band) for band in bands)
    )


def _table_f_target(bucket: RobinhoodOptionCapitalGainBucket, itr_form: ItrForm, note: str) -> str:
    """Every ``table-f`` row's ``manual_target`` after its band prefix."""
    stcg = bucket is _STCG
    labels = (
        PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM
        if stcg
        else PORTAL_ROW_LABEL_CG_TABLE_F_LTCG_BY_ITR_FORM
    )
    section = {"ITR-2": "A5" if stcg else "B8", "ITR-3": "A6" if stcg else "B9"}[itr_form]
    return (
        f"{labels[itr_form]} {note} Add the same band's amounts from schedule-cg.xlsx's "
        "equity bands and every other capital-gains source, then lower the bands for any "
        "capital loss set off against them, so the row totals that Schedule BFLA item. "
        f"If the {section} figures you enter for these options round to a different total, "
        "the row must still total Schedule BFLA."
    )


def _table_f_rows(
    result: RobinhoodOptionsResult, tmp_path: Path, *, itr_form: ItrForm = "ITR-2"
) -> list[tuple[str, ...]]:
    """The workpaper's ``table-f`` rows, each with every cell, after checking
    each row's band prefix and its fixed cells."""
    rows = [
        row
        for row in _workpaper_rows(result, tmp_path, itr_form=itr_form)
        if row["record_type"] == "table-f"
    ]
    for index, row in enumerate(rows):
        column = _TABLE_F_COLUMNS[index % 5]
        heading = PORTAL_COLUMN_HEADERS_CG_TABLE_F[index % 5]
        assert row["activity_or_close_date"] == heading
        bucket = RobinhoodOptionCapitalGainBucket(row["treatment_or_kind"])
        assert row["manual_target"].startswith(
            f"Schedule CG Table F row {bucket.table_f_row}({column}) {heading}: "
        )
        assert (row["account_index"], row["quantity"]) == ("", "")
        assert (row["cost_or_cash_usd"], row["proceeds_or_pnl_usd"]) == ("", "")
    return [
        (
            row["treatment_or_kind"],
            row["amount_inr"],
            row["manual_target"].split(": ", 1)[1],
        )
        for row in rows
    ]


@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
@pytest.mark.parametrize("long_term", [False, True], ids=["stcg", "ltcg"])
def test_capital_gains_file_their_table_f_bands(
    tmp_path: Path, long_term: bool, itr_form: ItrForm
) -> None:
    """#1362: fails on e992964, which gave no Table F row. The ₹8000.80 gain
    closed June 1, 2025, so band (i) of row 3, or row 5 when long-term, is
    ₹8001 and the other four 0. The five rows follow the capital-gain row,
    and every other byte of the workpaper is e992964's."""
    result = _capital_result(long_term=long_term)
    bucket = _LTCG if long_term else _STCG

    target = _table_f_target(bucket, itr_form, _COMPUTED_NOTE)
    assert _table_f_rows(result, tmp_path, itr_form=itr_form) == [
        (bucket.value, amount, target) for amount in ("8001", "0", "0", "0", "0")
    ]
    assert result.table_f == {bucket: _computed(8001, 0, 0, 0, 0)}
    written = (tmp_path / "robinhood-options.csv").read_bytes().splitlines(keepends=True)
    assert [line.split(b",", 1)[0] for line in written[-6:]] == [
        b"capital-gain",
        *[b"table-f"] * 5,
    ]
    kept = b"".join(line for line in written if not line.startswith(b"table-f,"))
    assert hashlib.sha256(kept).hexdigest() == _MAIN_WORKPAPER_SHA256[long_term, itr_form]


def test_a_run_without_option_capital_gains_writes_main_s_workpaper(tmp_path: Path) -> None:
    """#1362 control: business-treated options have no capital-gain line, so
    no Table F row, and the workpaper is e992964's byte for byte."""
    result = _compute(_paired(_closed_events()), RobinhoodOptionTreatment.SPECULATIVE_BUSINESS)

    _workpaper_rows(result, tmp_path, itr_form="ITR-3")
    written = (tmp_path / "robinhood-options.csv").read_bytes()
    assert hashlib.sha256(written).hexdigest() == _MAIN_BUSINESS_WORKPAPER_SHA256


def test_table_f_splits_rational_option_gains_exactly(tmp_path: Path) -> None:
    """#1362, #1245: 3 calls bought May 1, 2025 for $10.00; 1 sold May 15 for
    $4.00 and 2 sold July 15 for $7.00, at ₹80.5. The gains are exactly
    ₹161/3 and ₹161/6, ₹80.50 in all, so the bands split ₹81 as 54 and 27.
    Their shown Decimals stop at 60 digits and add up to just under 80.5,
    whose whole rupees are 80."""
    events = (
        _option(
            _BTO, 2, date(2025, 5, 1), date(2025, 5, 2), quantity="3", price="0.03", amount="-10.00"
        ),
        _option(
            _STC, 3, date(2025, 5, 15), date(2025, 5, 16), quantity="1", price="0.04", amount="4.00"
        ),
        _option(
            _STC,
            4,
            date(2025, 7, 15),
            date(2025, 7, 16),
            quantity="2",
            price="0.035",
            amount="7.00",
        ),
    )
    statements = (
        _Statement(date(2025, 5, 1), date(2025, 5, 31), (_position(1, quantity="2"),)),
        _Statement(date(2025, 6, 1), date(2025, 6, 30), (_position(1, quantity="2"),)),
        _Statement(date(2025, 7, 1), date(2025, 7, 31), ()),
    )

    result = _compute(
        _paired(events, statements=statements),
        RobinhoodOptionTreatment.CAPITAL_GAINS,
        capital_rate=Decimal("80.5"),
    )

    assert result.blockers == ()
    target = _table_f_target(_STCG, "ITR-2", _COMPUTED_NOTE)
    assert _table_f_rows(result, tmp_path) == [
        (_STCG.value, amount, target) for amount in ("54", "27", "0", "0", "0")
    ]
    assert result.table_f == {_STCG: _computed(54, 27, 0, 0, 0)}
    lines = result.capital_gain_lines
    assert [line.exact_net_gain_inr for line in lines] == [Fraction(161, 3), Fraction(161, 6)]
    # Added exactly, the shown Decimals fall short of the half rupee.
    assert 80 < sum(Fraction(line.net_gain_inr) for line in lines) < Fraction(161, 2)


def _option_round_trip(
    row: int, opened: date, closed: date, cost: str, proceeds: str, quantity: str = "1"
) -> tuple[RobinhoodEvent, RobinhoodEvent]:
    """``quantity`` contracts bought on ``opened`` for ``cost`` and sold on
    ``closed`` for ``proceeds``, at activity report rows ``row`` and ``row + 1``."""
    contracts = Decimal(quantity) * 100
    return (
        _option(
            _BTO,
            row,
            opened,
            opened + timedelta(days=1),
            quantity=quantity,
            price=str(Decimal(cost) / contracts),
            amount=f"-{cost}",
        ),
        _option(
            _STC,
            row + 1,
            closed,
            closed + timedelta(days=1),
            quantity=quantity,
            price=str(Decimal(proceeds) / contracts),
            amount=proceeds,
        ),
    )


_MAY_TRIP = (date(2025, 5, 1), date(2025, 5, 20))
_JULY_TRIP = (date(2025, 7, 1), date(2025, 7, 20))
_OCTOBER_TRIP = (date(2025, 10, 1), date(2025, 10, 20))


@pytest.mark.parametrize(
    ("trips", "table_f", "amounts", "note"),
    [
        (
            ((*_MAY_TRIP, "100.00", "50.00"),),
            _computed(0, 0, 0, 0, 0),
            ("0",) * 5,
            _COMPUTED_NOTE,
        ),
        (
            ((*_MAY_TRIP, "100.00", "300.00"), (*_JULY_TRIP, "300.00", "200.00")),
            _computed(8000, 0, 0, 0, 0),
            ("8000", "0", "0", "0", "0"),
            _COMPUTED_NOTE,
        ),
        (
            (
                (*_MAY_TRIP, "100.00", "300.00"),
                (*_JULY_TRIP, "100.00", "300.00"),
                (*_OCTOBER_TRIP, "300.00", "100.00"),
            ),
            ScheduleCgTableFRow(ScheduleCgTableFCase.LOSS_ACROSS_BANDS, None),
            ("",) * 5,
            _LOSS_ACROSS_BANDS_NOTE,
        ),
    ],
    ids=["net-loss-files-zeros", "one-gain-band-takes-the-total", "loss-across-bands-is-blank"],
)
def test_option_table_f_follows_the_shared_band_rules(
    tmp_path: Path,
    trips: tuple[tuple[date, date, str, str], ...],
    table_f: ScheduleCgTableFRow,
    amounts: tuple[str, ...],
    note: str,
) -> None:
    """#1362: #1360's rules (b), (d) and (e) at ₹80: a net loss files five
    zeros; a ₹16,000 May gain beside an ₹8,000 July loss puts the ₹8,000
    total in band (i); and a July gain beside a May gain and an October loss
    leaves the bands blank, naming why."""
    events = tuple(
        event
        for index, trip in enumerate(trips)
        for event in _option_round_trip(2 + 2 * index, *trip)
    )

    result = _compute(_paired(events, statements=()), RobinhoodOptionTreatment.CAPITAL_GAINS)

    assert result.blockers == ()
    target = _table_f_target(_STCG, "ITR-2", note)
    assert _table_f_rows(result, tmp_path) == [(_STCG.value, amount, target) for amount in amounts]
    assert result.table_f == {_STCG: table_f}


@pytest.mark.parametrize(
    ("price", "table_f", "amounts", "note"),
    [
        (
            "125000000.00",
            _computed(99999999992000, 0, 0, 0, 0),
            ("99999999992000", "0", "0", "0", "0"),
            _COMPUTED_NOTE,
        ),
        (
            "125000000.01",
            ScheduleCgTableFRow(ScheduleCgTableFCase.MANUAL_SECTION, None),
            ("",) * 5,
            _PAST_THE_BOUND_NOTE,
        ),
    ],
    ids=["within-the-bound", "past-the-bound"],
)
def test_option_table_f_past_the_cbdt_bound_is_blank(
    tmp_path: Path,
    price: str,
    table_f: ScheduleCgTableFRow,
    amounts: tuple[str, ...],
    note: str,
) -> None:
    """#1362: 100 contracts bought for $100.00 and sold at ``price``, at ₹80.
    A total within the CBDT 14-digit bound is filed; ₹1,00,00,00,00,00,000,
    one rupee past it, leaves the bands blank and says why rather than
    refusing a run that writes the workpaper today."""
    proceeds = str(Decimal(price) * 10000)
    events = _option_round_trip(2, *_MAY_TRIP, "100.00", proceeds, quantity="100")

    result = _compute(_paired(events, statements=()), RobinhoodOptionTreatment.CAPITAL_GAINS)

    assert result.blockers == ()
    target = _table_f_target(_STCG, "ITR-2", note)
    assert _table_f_rows(result, tmp_path) == [(_STCG.value, amount, target) for amount in amounts]
    assert result.table_f == {_STCG: table_f}


@pytest.mark.parametrize(
    ("opened", "closes", "blocked_close"),
    [
        # 28 months: long-term under the 24-month rule, short-term under the
        # 36-month one that applied before the cutover.
        (date(2022, 1, 10), (date(2024, 5, 15),), date(2024, 5, 15)),
        # Short-term under both rules, still blocked (owner decision 1 on #643).
        (date(2023, 6, 1), (date(2024, 5, 15),), date(2024, 5, 15)),
        (date(2023, 6, 1), (date(2024, 7, 23),), None),
        (date(2023, 6, 1), (date(2024, 5, 15), date(2024, 8, 15)), date(2024, 5, 15)),
    ],
)
def test_capital_slice_closed_before_cutover_is_blocked(
    opened: date,
    closes: tuple[date, ...],
    blocked_close: date | None,
) -> None:
    close_quantity = Decimal(2) / len(closes)
    events = (
        _option(RobinhoodEventKind.OPTION_BUY_TO_OPEN, 2, opened, opened + timedelta(days=1)),
        *(
            _option(
                RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
                3 + index,
                close,
                close + timedelta(days=1),
                quantity=str(close_quantity),
                price="1.50",
                amount=str(close_quantity * Decimal("150.00")),
            )
            for index, close in enumerate(closes)
        ),
    )
    statements = (_Statement(opened.replace(day=1), opened.replace(day=28), (_position(1),)),)
    result = _compute(
        _paired(events, statements=statements, report_start=opened.replace(day=1)),
        RobinhoodOptionTreatment.CAPITAL_GAINS,
        opening_date=opened.replace(day=1),
        financial_year=FinancialYear(2024),
    )

    assert RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE == "pre-cutover-holding-rule"
    assert result.blockers == tuple(
        RobinhoodOptionBlocker(
            kind=RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE,
            account_index=0,
            contracts=(item.contract,),
            event_references=item.close_references,
            located_at=item.close_references[0],
            columns=("Activity Date",),
        )
        for item in result.lot_slices
        if item.close_date == blocked_close
    )
    assert [line.source_slice.close_date for line in result.capital_gain_lines] == [
        close for close in closes if close != blocked_close
    ]
    assert result.validated_valuation_anchors == result.valuation_anchors != ()


def test_every_option_blocker_kind_has_its_own_message() -> None:
    """#802: a new kind fails here until it gets a message."""
    messages = {kind: option_blocker_message(kind) for kind in RobinhoodOptionBlockerKind}

    assert all(messages.values())
    assert len(set(messages.values())) == len(messages)
    assert "2024-07-23" in messages[RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE]
    assert messages == OPTION_BLOCKER_MESSAGES
    assert not any("evidence" in message for message in messages.values())


def test_option_blocker_summary_counts_each_kind_in_enum_order() -> None:
    """Each kind is located at its first blocker as given, not its lowest row (#1134)."""
    blockers = (
        RobinhoodOptionBlocker(
            kind=RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT,
            account_index=0,
            located_at=RobinhoodSecurityEventReference(0, 7),
            columns=("Quantity",),
        ),
        RobinhoodOptionBlocker(
            kind=RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION, account_index=1
        ),
        RobinhoodOptionBlocker(
            kind=RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT,
            account_index=0,
            located_at=RobinhoodSecurityEventReference(0, 3),
            columns=("Description",),
        ),
    )

    summary = option_blocker_summary(iter(blockers))

    assert summary == (
        f"{OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION]} (1 blocker; "
        "first at account 2, option positions open at the financial year's end); "
        f"{OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT]} (2 blockers; "
        "first at account 1, activity report row 7; column Quantity)"
    )


_ROW_7 = RobinhoodSecurityEventReference(0, 7)
_NO_ROW = "Robinhood option blocker has no row in its account"
_NAMES_A_ROW = "account-wide Robinhood option blocker names a row"


@pytest.mark.parametrize(
    ("blocker", "what"),
    [
        (
            RobinhoodOptionBlocker(
                kind=RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT, account_index=0
            ),
            _NO_ROW,
        ),
        (
            RobinhoodOptionBlocker(
                kind=RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT,
                account_index=0,
                located_at=_ROW_7,
            ),
            _NO_ROW,
        ),
        (
            RobinhoodOptionBlocker(
                kind=RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT,
                account_index=1,
                located_at=_ROW_7,
                columns=("Quantity",),
            ),
            _NO_ROW,
        ),
        (
            RobinhoodOptionBlocker(
                kind=RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION,
                account_index=0,
                located_at=_ROW_7,
                columns=("Quantity",),
            ),
            _NAMES_A_ROW,
        ),
        (
            RobinhoodOptionBlocker(
                kind=RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION,
                account_index=0,
                missing_from_statement_index=0,
            ),
            _NAMES_A_ROW,
        ),
    ],
    ids=["no-row", "no-columns", "other-account", "open-row", "open-statement"],
)
@pytest.mark.parametrize("after_valid", [False, True], ids=["first", "later"])
def test_option_blocker_summary_refuses_a_blocker_it_cannot_locate(
    blocker: RobinhoodOptionBlocker,
    what: str,
    after_valid: bool,
) -> None:
    row_kind = blocker.kind is RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT
    valid = RobinhoodOptionBlocker(
        kind=blocker.kind,
        account_index=0,
        located_at=_ROW_7 if row_kind else None,
        columns=("Quantity",) if row_kind else (),
    )
    with pytest.raises(InvariantError) as excinfo:
        option_blocker_summary((valid, blocker) if after_valid else (blocker,))
    assert excinfo.value.what == what


@pytest.mark.parametrize("value", ["unmapped-kind", []], ids=["string", "unhashable"])
def test_option_blocker_message_refuses_an_unmapped_kind(value: object) -> None:
    unmapped = cast("RobinhoodOptionBlockerKind", value)
    blockers = (
        RobinhoodOptionBlocker(
            kind=RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION, account_index=0
        ),
        RobinhoodOptionBlocker(kind=unmapped, account_index=0),
    )

    with pytest.raises(InvariantError, match="has no message"):
        option_blocker_message(unmapped)
    with pytest.raises(InvariantError, match="has no message"):
        option_blocker_summary(blockers)


def test_business_election_uses_fy_end_rate_and_a2_only(tmp_path: Path) -> None:
    result = _compute(
        _paired(_closed_events()),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )

    assert result.blockers == ()
    assert result.capital_gain_lines == ()
    assert result.classifications[0].schedule_fa_disposition is (
        RobinhoodOptionFaDisposition.A2_ONLY
    )
    summary = result.business_summaries[0]
    assert summary.matched_premium_paid_usd == Decimal("199.99")
    assert summary.matched_premium_received_usd == Decimal("300.00")
    assert summary.net_pnl_usd == Decimal("100.01")
    assert summary.fx_specified_date == _FY_END
    assert summary.net_pnl_inr == Decimal("8500.85")
    assert summary.schedule_bp_bucket == "speculative-business"
    assert summary.filing_complete
    assert result.required_itr_form == "ITR-3"
    assert not result.manual_work_required
    assert not result.business_fsi_manual_work_required
    assert not result.fsi_manual_work_required

    fsi = compute_robinhood_option_business_fsi(result)
    assert len(fsi.rows) == 1
    assert fsi.rows[0].income_inr == Decimal("8501")
    filing = compute_robinhood_option_business_filing(result)
    assert filing is not None
    assert filing.turnover_inr_full_precision == Decimal("8500.85")
    assert filing.net_income_inr_full_precision == Decimal("8500.85")
    assert filing.turnover_inr == Decimal("8501")
    assert filing.gross_profit_inr == Decimal("8501")
    assert filing.expenditure_inr == 0
    assert filing.net_income_inr == Decimal("8501")
    assert filing.bp_additions_inr == 0
    assert filing.bp_deductions_inr == 0
    assert filing.bp_income_inr == Decimal("8501")
    assert filing.cfl_loss_inr == 0
    assert filing.event_count == 1

    rows = _workpaper_rows(result, tmp_path)
    audit = {
        row["record_type"]: row for row in rows if row["record_type"].startswith("classification")
    }
    assert audit["classification-source"]["manual_target"].endswith("synthetic reviewed advice")
    assert audit["classification-reason"]["manual_target"].endswith(
        "synthetic option classification"
    )
    warning = audit["classification-warning"]["manual_target"]
    assert all(
        marker in warning
        for marker in (
            "section 43(5)",
            "section 73",
            "sections 80 and 139(3)",
            "BTO-to-STC",
            "puts",
            "delivery-settled",
        )
    )
    fields = {
        row["treatment_or_kind"]: row["amount_inr"]
        for row in rows
        if row["record_type"] == "itr-3-field"
    }
    assert fields == {
        "Nature of business 21010": "",
        "P&L 65i": "8501",
        "P&L 65ii": "8501",
        "P&L 65iii": "0",
        "P&L 65iv": "8501",
        "BP 2a": "8501",
        "BP B39": "8501",
        "BP B40": "0",
        "BP B41": "0",
        "BP B42": "8501",
        "CFL current AY / column 6": "0",
    }
    assert any(
        row["treatment_or_kind"] == "icai-5.11a-differences"
        for row in rows
        if row["record_type"] == "itr-3-audit"
    )
    assert any(
        row["treatment_or_kind"] == "net-income-full-precision"
        and Decimal(row["amount_inr"]) == Decimal("8500.85")
        for row in rows
        if row["record_type"] == "itr-3-audit"
    )
    assert any(
        row["treatment_or_kind"] == "BP A1" and "auto-populates" in row["manual_target"]
        for row in rows
        if row["record_type"] == "itr-3-note"
    )
    assert all(
        "Schedule FSI emitted" in row["manual_target"]
        for row in rows
        if row["record_type"] == "business-summary"
    )


def test_non_speculative_workpaper_warns_that_exchange_basis_is_unresolved(
    tmp_path: Path,
) -> None:
    result = _compute(
        _paired(_closed_events()),
        RobinhoodOptionTreatment.NON_SPECULATIVE_BUSINESS,
    )

    assert result.classifications[0].schedule_fa_disposition is (
        RobinhoodOptionFaDisposition.A2_ONLY
    )
    rows = _workpaper_rows(result, tmp_path)
    warning = next(
        row["manual_target"] for row in rows if row["record_type"] == "classification-warning"
    )
    assert all(
        marker in warning
        for marker in (
            "UNRESOLVED section 43(5)(d) basis",
            "prescribed/notified recognised stock exchange",
            "has not established that a US exchange qualifies",
            "section 72",
            "sections 80 and 139(3)",
            "exercise",
            "assignment",
        )
    )
    assert "section 73" not in warning


def test_mixed_business_classifications_keep_both_account_warnings() -> None:
    result = _compute(
        _paired(_closed_events()),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )
    mixed = replace(
        result,
        classifications=(
            result.classifications[0],
            replace(
                result.classifications[0],
                account_index=1,
                treatment=RobinhoodOptionTreatment.NON_SPECULATIVE_BUSINESS,
            ),
        ),
    )

    warnings = option_treatment_warnings(mixed)
    assert len(warnings) == 2
    assert "account index 0" in warnings[0]
    assert "section 73" in warnings[0]
    assert "account index 1" in warnings[1]
    assert "UNRESOLVED section 43(5)(d) basis" in warnings[1]


def test_classification_inputs_are_safely_labelled_and_nul_checked(
    tmp_path: Path,
) -> None:
    result = _compute(
        _paired(_closed_events()),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )
    formula_like = replace(
        result,
        classifications=(
            replace(
                result.classifications[0],
                source="=CA memo",
                reason="@Firm conclusion",
            ),
        ),
    )

    rows = _workpaper_rows(formula_like, tmp_path)
    source = next(
        row["manual_target"] for row in rows if row["record_type"] == "classification-source"
    )
    reason = next(
        row["manual_target"] for row in rows if row["record_type"] == "classification-reason"
    )
    assert source.startswith("User-stated classification source")
    assert source.endswith("=CA memo")
    assert reason.startswith("User-stated classification reason")
    assert reason.endswith("@Firm conclusion")

    nul_dir = tmp_path / "nul"
    nul_dir.mkdir()
    nul = replace(
        result,
        classifications=(replace(result.classifications[0], source="CA\x00memo"),),
    )
    with pytest.raises(WriterContractError, match="NUL byte"):
        write_options(
            nul,
            output_dir=nul_dir,
            financial_year=_FINANCIAL_YEAR,
            itr_form="ITR-3",
        )
    assert list(nul_dir.iterdir()) == []


def test_business_workpaper_includes_books_and_audit_advisory(tmp_path: Path) -> None:
    result = _compute(
        _paired(_closed_events()),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )

    rows = _workpaper_rows(result, tmp_path)
    advisories = [
        row
        for row in rows
        if row["record_type"] == "itr-3-note" and row["treatment_or_kind"] == "Sections 44AA/44AB"
    ]
    assert len(advisories) == 1
    assert advisories[0]["amount_inr"] == "8501"
    assert all(
        marker in advisories[0]["manual_target"]
        for marker in ("44AA(2)", "44AB(a)", "three preceding years", "5% or less")
    )

    unresolved = replace(
        result,
        classifications=(replace(result.classifications[0], turnover_basis=None),),
    )
    assert compute_robinhood_option_business_filing(unresolved) is None
    assert unresolved.manual_work_required
    unresolved_rows = _workpaper_rows(unresolved, tmp_path)
    unresolved_advisories = [
        row
        for row in unresolved_rows
        if row["record_type"] == "itr-3-note" and row["treatment_or_kind"] == "Sections 44AA/44AB"
    ]
    assert len(unresolved_advisories) == 1
    assert unresolved_advisories[0]["amount_inr"] == ""


def test_mixed_business_treatments_leave_fsi_manual() -> None:
    result = _compute(
        _paired(_closed_events()),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )
    speculative = result.business_summaries[0]
    mixed = replace(
        result,
        business_summaries=(
            speculative,
            replace(
                speculative,
                account_index=1,
                treatment=RobinhoodOptionTreatment.NON_SPECULATIVE_BUSINESS,
                schedule_bp_bucket="business-other-than-speculative",
            ),
        ),
    )

    assert compute_robinhood_option_business_fsi(mixed).rows == []
    assert mixed.business_fsi_manual_work_required
    assert mixed.fsi_manual_work_required


def test_reviewed_expenses_and_bp_adjustments_apply_once() -> None:
    result = _compute(
        _paired(_closed_events()),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )
    reviewed = replace(
        result,
        classifications=(
            replace(
                result.classifications[0],
                external_expenses_inr=Decimal("10.4"),
                bp_additions_inr=Decimal("20.4"),
                bp_deductions_inr=Decimal("5.4"),
            ),
        ),
    )

    filing = compute_robinhood_option_business_filing(reviewed)
    fsi = compute_robinhood_option_business_fsi(reviewed)

    assert filing is not None
    assert filing.turnover_inr == Decimal("8501")
    assert filing.expenditure_inr == Decimal("10")
    assert filing.net_income_inr_full_precision == Decimal("8490.45")
    assert filing.net_income_inr == Decimal("8491")
    assert filing.bp_additions_inr == Decimal("20")
    assert filing.bp_deductions_inr == Decimal("5")
    assert filing.bp_income_inr == Decimal("8506")
    assert len(fsi.rows) == 1
    assert fsi.rows[0].income_inr == Decimal("8506")


def test_speculative_loss_flows_to_no_books_expenditure_and_cfl(tmp_path: Path) -> None:
    opened, closed = _closed_events()
    result = _compute(
        _paired((opened, replace(closed, price_usd=Decimal("0.50"), amount_usd=Decimal("100")))),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )

    filing = compute_robinhood_option_business_filing(result)

    assert filing is not None
    assert filing.turnover_inr_full_precision == Decimal("8499.15")
    assert filing.net_income_inr_full_precision == Decimal("-8499.15")
    assert filing.turnover_inr == Decimal("8499")
    assert filing.gross_profit_inr == 0
    assert filing.expenditure_inr == Decimal("8499")
    assert filing.net_income_inr == Decimal("-8499")
    assert filing.bp_income_inr == Decimal("-8499")
    assert filing.cfl_loss_inr == Decimal("8499")

    rows = _workpaper_rows(result, tmp_path)
    assert any("non-positive BP B42; no Schedule FSI row" in row["manual_target"] for row in rows)
    fields = {
        row["treatment_or_kind"]: row["amount_inr"]
        for row in rows
        if row["record_type"] == "itr-3-field"
    }
    assert [fields[name] for name in ("P&L 65iv", "BP 2a", "BP B39", "BP B42")] == ["'-8499"] * 4
    assert fields["CFL current AY / column 6"] == "8499"
    audit = {
        row["treatment_or_kind"]: row["amount_inr"]
        for row in rows
        if row["record_type"] == "itr-3-audit"
    }
    assert audit["net-income-full-precision"] == "'-8499.15"


def test_workpaper_checks_every_written_row_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _compute(
        _paired(_closed_events()),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )
    checked: list[int] = []
    real_check = _writer_contract._assert_no_csv_formula_injection

    def spy_check(row: Sequence[str], *, row_index: int) -> None:
        checked.append(row_index)
        real_check(row, row_index=row_index)

    monkeypatch.setattr(_writer_contract, "_assert_no_csv_formula_injection", spy_check)

    path = write_options(
        result,
        output_dir=tmp_path,
        financial_year=_FINANCIAL_YEAR,
        itr_form="ITR-3",
    )

    assert path is not None
    with path.open(encoding="utf-8-sig", newline="") as handle:
        written = list(csv.reader(handle))
    assert {row[0] for row in written} >= {"record_type", "business-summary", "itr-3-field"}
    assert checked == list(range(len(written)))


def test_workpaper_refuses_nul_outside_audit_notes(tmp_path: Path) -> None:
    blocker = SimpleNamespace(account_index=0, kind=SimpleNamespace(value="missing\x00evidence"))
    options = SimpleNamespace(
        trades=(object(),),
        classifications=(),
        capital_gain_lines=(),
        table_f={},
        business_summaries=(),
        blockers=(blocker,),
    )

    with pytest.raises(WriterContractError, match="NUL byte") as exc_info:
        write_robinhood_options_csv(
            cast("Any", options),
            tmp_path,
            assessment_year_start=_FINANCIAL_YEAR.assessment_year_start,
            itr_form="ITR-3",
            business_filing=None,
            business_target="",
        )

    assert exc_info.value.row_index == 1
    assert list(tmp_path.iterdir()) == []


def test_statement_positions_follow_settlement_not_activity_date() -> None:
    events = (
        _option(
            RobinhoodEventKind.OPTION_BUY_TO_OPEN,
            2,
            date(2025, 5, 31),
            date(2025, 6, 2),
        ),
        _option(
            RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
            3,
            date(2025, 7, 1),
            date(2025, 7, 2),
            price="1.50",
            amount="300.00",
        ),
    )
    statements = (
        _Statement(date(2025, 5, 1), date(2025, 5, 31), ()),
        _Statement(date(2025, 6, 1), date(2025, 6, 30), (_position(1),)),
        _Statement(date(2025, 7, 1), date(2025, 7, 31), ()),
    )
    result = _compute(
        _paired(events, statements=statements),
        RobinhoodOptionTreatment.CAPITAL_GAINS,
    )
    assert result.blockers == ()


@pytest.mark.parametrize(
    ("paired", "treatment", "expected"),
    [
        (
            _paired(
                _closed_events(),
                statements=(
                    _Statement(date(2025, 5, 1), date(2025, 5, 31), ()),
                    _Statement(date(2025, 6, 1), date(2025, 6, 30), ()),
                ),
            ),
            RobinhoodOptionTreatment.CAPITAL_GAINS,
            RobinhoodOptionBlockerKind.POSITION_RECONCILIATION,
        ),
        (
            _paired(
                (
                    _option(
                        RobinhoodEventKind.OPTION_BUY_TO_OPEN,
                        2,
                        date(2025, 5, 1),
                        date(2025, 5, 2),
                        amount="-20.00",
                    ),
                    _closed_events()[1],
                )
            ),
            RobinhoodOptionTreatment.CAPITAL_GAINS,
            RobinhoodOptionBlockerKind.PREMIUM_RECONCILIATION,
        ),
        (
            _paired(
                (
                    _closed_events()[0],
                    replace(
                        _closed_events()[1],
                        activity_date=date(2025, 5, 1),
                        settle_date=date(2025, 5, 2),
                    ),
                ),
                statements=(_Statement(date(2025, 5, 1), date(2025, 5, 31), ()),),
            ),
            RobinhoodOptionTreatment.CAPITAL_GAINS,
            RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY,
        ),
        (
            _paired(
                (
                    _option(
                        RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
                        2,
                        date(2025, 6, 1),
                        date(2025, 6, 2),
                        price="1.50",
                        amount="300.00",
                    ),
                ),
                statements=(_Statement(date(2025, 6, 1), date(2025, 6, 30), ()),),
            ),
            RobinhoodOptionTreatment.CAPITAL_GAINS,
            RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY,
        ),
        (
            _paired(
                (
                    _option(
                        RobinhoodEventKind.OPTION_BUY_TO_OPEN,
                        2,
                        date(2025, 5, 1),
                        date(2025, 5, 2),
                        right="Put",
                    ),
                ),
                statements=(_Statement(date(2025, 5, 1), date(2025, 5, 31), ()),),
            ),
            RobinhoodOptionTreatment.CAPITAL_GAINS,
            RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT,
        ),
        (
            _paired(
                (
                    _option(
                        RobinhoodEventKind.OPTION_BUY_TO_OPEN,
                        2,
                        date(2025, 12, 20),
                        date(2025, 12, 22),
                    ),
                ),
                statements=(_Statement(date(2025, 12, 1), date(2025, 12, 31), ()),),
            ),
            RobinhoodOptionTreatment.CAPITAL_GAINS,
            RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE,
        ),
    ],
)
def test_expected_option_blockers(
    paired: RobinhoodAccountsReconciliation,
    treatment: RobinhoodOptionTreatment | None,
    expected: RobinhoodOptionBlockerKind,
) -> None:
    result = _compute(paired, treatment)
    assert expected in {blocker.kind for blocker in result.blockers}
    assert _slices_reconcile_with_trades(result)


def test_close_spanning_two_opens_keeps_unit_costs_and_proceeds_remainder() -> None:
    events = (
        _option(
            RobinhoodEventKind.OPTION_BUY_TO_OPEN,
            2,
            date(2025, 5, 1),
            date(2025, 5, 2),
        ),
        _option(
            RobinhoodEventKind.OPTION_BUY_TO_OPEN,
            3,
            date(2025, 5, 15),
            date(2025, 5, 16),
            quantity="3",
            price="1.50",
            amount="-450.03",
        ),
        _option(
            RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
            4,
            date(2025, 6, 2),
            date(2025, 6, 3),
            quantity="3",
            price="2.00",
            amount="600.01",
        ),
    )
    statements = (
        _Statement(date(2025, 5, 1), date(2025, 5, 31), (_position(1, quantity="5"),)),
        _Statement(date(2025, 6, 1), date(2025, 6, 30), (_position(1),)),
    )

    result = _compute(
        _paired(events, statements=statements),
        RobinhoodOptionTreatment.CAPITAL_GAINS,
    )

    first_open = (RobinhoodSecurityEventReference(0, 2),)
    second_open = (RobinhoodSecurityEventReference(0, 3),)
    first, second = result.lot_slices
    assert (first.open_references, first.open_date, first.quantity, first.cost_usd) == (
        first_open,
        date(2025, 5, 1),
        Decimal("2"),
        Decimal("199.99"),
    )
    # Each slice's proceeds are its exact share of the sale's 3 contracts (#1245):
    # 2/3 and 1/3 of 600.01, which add back to the cash. #1148 rounded them to
    # 400.006667 and 200.003333.
    assert first.exact_net_proceeds_usd == Fraction("600.01") * 2 / 3
    assert first.net_proceeds_usd == _cut(Fraction("600.01") * 2 / 3)
    assert first.net_pnl_usd == _cut(Fraction("600.01") * 2 / 3 - Fraction("199.99"))
    assert (second.open_references, second.open_date, second.quantity, second.cost_usd) == (
        second_open,
        date(2025, 5, 15),
        Decimal("1"),
        Decimal("150.01"),
    )
    assert second.exact_net_proceeds_usd == Fraction("600.01") / 3
    assert second.net_proceeds_usd == _cut(Fraction("600.01") / 3)
    assert second.net_pnl_usd == _cut(Fraction("600.01") / 3 - Fraction("150.01"))
    assert first.exact_net_proceeds_usd + second.exact_net_proceeds_usd == Fraction("600.01")
    assert [
        (lot.open_references, lot.open_date, lot.remaining_quantity, lot.remaining_cost_usd)
        for lot in result.open_lots
    ] == [(second_open, date(2025, 5, 15), Decimal("2"), Decimal("300.02"))]
    # The open remainder reaches its expiry inside the report window.
    assert [(blocker.kind, blocker.event_references) for blocker in result.blockers] == [
        (RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE, second_open)
    ]


def test_close_beyond_open_quantity_emits_no_slices_or_open_lots() -> None:
    events = (
        _closed_events()[0],
        _option(
            RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
            3,
            date(2025, 6, 1),
            date(2025, 6, 2),
            quantity="3",
            price="1.50",
            amount="450.00",
        ),
    )

    result = _compute(_paired(events), RobinhoodOptionTreatment.CAPITAL_GAINS)

    assert (
        RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY,
        (RobinhoodSecurityEventReference(0, 3),),
    ) in {(blocker.kind, blocker.event_references) for blocker in result.blockers}
    assert result.lot_slices == ()
    assert result.open_lots == ()


def test_put_only_activity_does_not_require_tax_classification() -> None:
    put = _option(
        RobinhoodEventKind.OPTION_BUY_TO_OPEN,
        2,
        date(2025, 5, 1),
        date(2025, 5, 2),
        right="Put",
    )

    result = _compute(_paired((put,), statements=()), None)

    assert len(result.trades) == 1
    assert result.classifications == ()
    assert result.capital_gain_lines == ()
    assert result.business_summaries == ()
    assert RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT in {
        blocker.kind for blocker in result.blockers
    }


def test_blocked_put_does_not_change_speculative_business_totals() -> None:
    put_round_trip = (
        _option(
            RobinhoodEventKind.OPTION_BUY_TO_OPEN,
            4,
            date(2025, 7, 1),
            date(2025, 7, 2),
            right="Put",
        ),
        _option(
            RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
            5,
            date(2025, 8, 1),
            date(2025, 8, 4),
            price="1.50",
            amount="300.00",
            right="Put",
        ),
    )

    result = _compute(
        _paired((*_closed_events(), *put_round_trip)),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )

    summary = result.business_summaries[0]
    assert summary.matched_premium_paid_usd == Decimal("199.99")
    assert summary.matched_premium_received_usd == Decimal("300.00")
    assert summary.net_pnl_usd == Decimal("100.01")
    assert not summary.filing_complete
    assert RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT in {
        blocker.kind for blocker in result.blockers
    }


def test_call_anchor_blocker_does_not_remove_business_totals() -> None:
    bad_anchor = replace(_position(1), market_value_usd=Decimal("-1"))
    result = _compute(
        _paired(
            _closed_events(),
            statements=(
                _Statement(date(2025, 5, 1), date(2025, 5, 31), (bad_anchor,)),
                _Statement(date(2025, 6, 1), date(2025, 6, 30), ()),
            ),
        ),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )

    summary = result.business_summaries[0]
    assert summary.matched_premium_paid_usd == Decimal("199.99")
    assert summary.matched_premium_received_usd == Decimal("300.00")
    assert summary.net_pnl_usd == Decimal("100.01")
    assert not summary.filing_complete


def test_option_treatment_is_mandatory() -> None:
    with pytest.raises(RobinhoodOptionsError, match="explicit account/FY"):
        _compute(_paired(_closed_events()), None)


def test_open_business_position_blocks_complete_bp_amount() -> None:
    """A supported call (2 whole contracts, market value 240, expiring after
    the FY end) still open at the FY end is the only blocker, and on its own
    keeps the business amount from being complete."""
    later = date(2026, 6, 19)
    opened = _option(
        RobinhoodEventKind.OPTION_BUY_TO_OPEN,
        2,
        date(2025, 5, 1),
        date(2025, 5, 2),
        expiration=later,
    )
    held = _position(1, expiration=later)
    result = _compute(
        _paired((opened,), statements=(_Statement(date(2025, 5, 1), _FY_END, (held,)),)),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )

    assert [(blocker.kind, blocker.account_index) for blocker in result.blockers] == [
        (RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION, 0)
    ]
    assert result.business_summaries[0].filing_complete is False


def test_unsupported_call_anchor_still_counts_as_open_business_position() -> None:
    """A call whose statement anchor is unsupported (market value -1) stays in
    the FY-end open check: its UNSUPPORTED_CONTRACT blocker doesn't hide the
    OPEN_BUSINESS_POSITION one. The call expires in the FY with no closing
    trade, which adds UNSUPPORTED_LIFECYCLE."""
    bad_anchor = replace(_position(1), market_value_usd=Decimal("-1"))
    result = _compute(
        _paired(
            (_closed_events()[0],),
            statements=(
                _Statement(
                    date(2025, 5, 1),
                    _FY_END,
                    (bad_anchor,),
                ),
            ),
        ),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )
    assert {blocker.kind for blocker in result.blockers} == {
        RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT,
        RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION,
        RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE,
    }
    assert result.business_summaries[0].filing_complete is False


def test_repeated_unexplained_positions_return_blockers_without_crashing() -> None:
    statements = (
        _Statement(date(2025, 5, 1), date(2025, 5, 31), (_position(1),)),
        _Statement(date(2025, 6, 1), date(2025, 6, 30), (_position(2),)),
    )
    result = _compute(
        _paired((_cash_event(),), statements=statements),
        None,
    )
    position_blockers = [
        blocker
        for blocker in result.blockers
        if blocker.kind is RobinhoodOptionBlockerKind.POSITION_RECONCILIATION
    ]
    assert len(position_blockers) == 2


def test_prior_fy_opening_basis_closes_in_current_fy() -> None:
    events = (
        _option(
            RobinhoodEventKind.OPTION_BUY_TO_OPEN,
            2,
            date(2025, 3, 1),
            date(2025, 3, 3),
        ),
        _option(
            RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
            3,
            date(2025, 4, 15),
            date(2025, 4, 16),
            price="1.50",
            amount="300.00",
        ),
    )
    statements = (
        _Statement(date(2025, 3, 1), date(2025, 3, 31), (_position(1),)),
        _Statement(date(2025, 4, 1), date(2025, 4, 30), ()),
    )
    result = _compute(
        _paired(
            events,
            statements=statements,
            report_start=date(2025, 1, 1),
        ),
        RobinhoodOptionTreatment.CAPITAL_GAINS,
        opening_date=date(2025, 1, 1),
    )
    assert result.blockers == ()
    assert result.capital_gain_lines[0].source_slice.open_date == date(2025, 3, 1)
    assert result.capital_gain_lines[0].source_slice.close_date == date(2025, 4, 15)


def _private_options_outcome(
    paired: RobinhoodAccountsReconciliation,
    config: RobinhoodConfig,
) -> RobinhoodOptionsResult | None:
    """The reconstruction, or ``None`` for a typed refusal. Any other
    exception fails after the ``except`` block, so pytest prints no source
    exception."""
    try:
        return compute_robinhood_options(
            paired,
            config=config,
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            capital_gains_fx_policy=AuditedFxStub(Decimal("1")),
            business_fx_policy=AuditedFxStub(Decimal("1"), specified_date=_FY_END),
        )
    except IbkrFaError:
        untyped = False
    except Exception:
        untyped = True
    if untyped:
        pytest.fail("private option reconstruction failed with an untyped error", pytrace=False)
    return None


def _trades_match_report_rows(
    paired: RobinhoodAccountsReconciliation,
    result: RobinhoodOptionsResult,
    records: tuple[tuple[tuple[str, ...], ...], ...],
) -> bool:
    """One trade per BTO or STC report row (read without the parser), of
    the row's kind and quantity."""
    kinds = {
        "BTO": RobinhoodEventKind.OPTION_BUY_TO_OPEN,
        "STC": RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
    }
    rows = {
        RobinhoodSecurityEventReference(account_index, row_index): (
            kinds[row[5]],
            Decimal(row[6].removesuffix("S")),
        )
        for account_index, account in enumerate(paired.accounts)
        for row_index, row in enumerate(records[account.report_index], start=1)
        if row[5] in kinds
    }
    trades = {trade.event_reference: (trade.kind, trade.quantity) for trade in result.trades}
    return len(trades) == len(result.trades) and trades == rows


def _slices_reconcile_with_trades(result: RobinhoodOptionsResult) -> bool:
    """Each traded contract retains lot output or a lot-matching refusal.
    For every contract with lot output: the slices close exactly the
    contract's sold quantity, slices and open lots together hold exactly
    its bought quantity, each slice cites only that contract's opening and
    closing trades, each closing group's slices share out exactly its
    proceeds, and each opening group's slices and open lot share out exactly
    its cost exactly (#1148, #1245)."""
    trades = {trade.event_reference: trade for trade in result.trades}
    unmatched = {
        contract
        for blocker in result.blockers
        if blocker.kind
        in {
            RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY,
            RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY,
        }
        for contract in blocker.contracts
    }
    contracts = {item.contract for item in (*result.lot_slices, *result.open_lots)} | (
        {trade.contract for trade in result.trades} - unmatched
    )
    for contract in contracts:
        bought = {
            reference: trade
            for reference, trade in trades.items()
            if trade.contract == contract and trade.kind is RobinhoodEventKind.OPTION_BUY_TO_OPEN
        }
        sold = {
            reference: trade
            for reference, trade in trades.items()
            if trade.contract == contract and trade.kind is RobinhoodEventKind.OPTION_SELL_TO_CLOSE
        }
        slices = [item for item in result.lot_slices if item.contract == contract]
        sliced = sum((item.quantity for item in slices), Decimal())
        still_open = sum(
            (item.remaining_quantity for item in result.open_lots if item.contract == contract),
            Decimal(),
        )
        if not (
            sliced == sum((trade.quantity for trade in sold.values()), Decimal())
            and sliced + still_open == sum((trade.quantity for trade in bought.values()), Decimal())
            and all(
                set(item.open_references) <= set(bought) and set(item.close_references) <= set(sold)
                for item in slices
            )
        ):
            return False
        lots = [item for item in result.open_lots if item.contract == contract]
        for close_references in {item.close_references for item in slices}:
            shared = sum(
                (
                    item.exact_net_proceeds_usd
                    for item in slices
                    if item.close_references == close_references
                ),
                Fraction(),
            )
            if shared != _cash(trades, close_references):
                return False
        for open_references in {item.open_references for item in (*slices, *lots)}:
            shared = sum(
                (item.exact_cost_usd for item in slices if item.open_references == open_references),
                Fraction(),
            ) + sum(
                (
                    item.exact_remaining_cost_usd
                    for item in lots
                    if item.open_references == open_references
                ),
                Fraction(),
            )
            if shared != _cash(trades, open_references):
                return False
    return True


def _cash(
    trades: dict[RobinhoodSecurityEventReference, RobinhoodOptionTradeEvidence],
    references: tuple[RobinhoodSecurityEventReference, ...],
) -> Fraction:
    return sum(
        (Fraction(trades[reference].exact_cash_usd.copy_abs()) for reference in references),
        Fraction(),
    )


def test_option_retention_detects_dropped_contract_output() -> None:
    result = _compute(_paired(_closed_events()), RobinhoodOptionTreatment.CAPITAL_GAINS)
    assert _slices_reconcile_with_trades(result)
    assert not _slices_reconcile_with_trades(replace(result, lot_slices=(), open_lots=()))


@private_test
def test_private_option_population_reconstructs_without_value_disclosure() -> None:
    """The reconstruction either refuses with a typed error or reconciles
    with the report rows and trades it came from."""
    paired = load_private_accounts_reconciliation()
    records = load_private_report_records()
    accounts = tuple(
        _account(
            account.activity.primary_bundle.account_number,
            account.evidence.start_date,
        )
        for account in paired.accounts
    )
    classifications = tuple(
        _classification(
            RobinhoodOptionTreatment.CAPITAL_GAINS,
            account_number=account.number,
        )
        for account in accounts
    )
    result = _private_options_outcome(
        paired,
        RobinhoodConfig(accounts=accounts, option_classifications=classifications),
    )
    if result is not None and not (
        _trades_match_report_rows(paired, result, records) and _slices_reconcile_with_trades(result)
    ):
        pytest.fail("private option reconstruction disagrees with its sources", pytrace=False)


# ---- filed amounts and the CBDT 14-digit bound (#830) -----------------------


def _reviewed_business(**classification: Decimal) -> RobinhoodOptionsResult:
    result = _compute(
        _paired(_closed_events()),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )
    return replace(
        result,
        classifications=(replace(result.classifications[0], **classification),),
    )


_BUSINESS_FILING_AMOUNTS = [
    "turnover_inr",
    "gross_profit_inr",
    "expenditure_inr",
    "net_income_inr",
    "bp_additions_inr",
    "bp_deductions_inr",
    "bp_income_inr",
    "cfl_loss_inr",
]


@pytest.mark.parametrize("field", _BUSINESS_FILING_AMOUNTS)
@pytest.mark.parametrize("sign", ["", "-"])
def test_business_filing_amount_within_the_cbdt_bound_accepted(field: str, sign: str) -> None:
    filing = compute_robinhood_option_business_filing(_reviewed_business())
    assert filing is not None
    amount = Decimal(sign + "99999999999999")

    assert getattr(replace(filing, **{field: amount}), field) == amount


@pytest.mark.parametrize("field", _BUSINESS_FILING_AMOUNTS)
@pytest.mark.parametrize("sign", ["", "-"])
def test_business_filing_amount_beyond_the_cbdt_bound_rejected(field: str, sign: str) -> None:
    filing = compute_robinhood_option_business_filing(_reviewed_business())
    assert filing is not None

    with pytest.raises(InvariantError) as excinfo:
        replace(filing, **{field: Decimal(sign + "100000000000000")})

    assert excinfo.value.what.startswith(
        f"RobinhoodOptionBusinessFiling.{field} must round to a whole-rupee amount"
    )


def test_reviewed_bp_additions_rounding_within_the_cbdt_bound_accepted() -> None:
    """Equal deductions keep BP income in range."""
    amount = Decimal("99999999999999.4")
    filing = compute_robinhood_option_business_filing(
        _reviewed_business(bp_additions_inr=amount, bp_deductions_inr=amount)
    )

    assert filing is not None
    assert filing.bp_additions_inr == filing.bp_deductions_inr == Decimal("99999999999999")


def test_reviewed_bp_additions_rounding_beyond_the_cbdt_bound_refuse() -> None:
    with pytest.raises(
        InvariantError, match=r"RobinhoodOptionBusinessFiling\.bp_additions_inr must round"
    ):
        compute_robinhood_option_business_filing(
            _reviewed_business(
                bp_additions_inr=Decimal("99999999999999.5"),
                bp_deductions_inr=Decimal("99999999999999.4"),
            )
        )


@pytest.mark.parametrize("field", ["books_maintained", "other_speculative_business"])
def test_unsupported_business_settings_keep_the_options_leg_manual(field: str) -> None:
    """#1417: the reviewed business result files ₹8,501 of business income as
    kept without books; with `field` set to true it has no filing and the
    options leg stays manual."""
    reviewed = _reviewed_business()
    assert compute_robinhood_option_business_filing(reviewed) is not None
    assert reviewed.manual_work_required is False

    unsupported = replace(
        reviewed,
        classifications=(replace(reviewed.classifications[0], **{field: True}),),
    )
    assert compute_robinhood_option_business_filing(unsupported) is None
    assert unsupported.manual_work_required is True


# ---- option cost conservation and exact option arithmetic (#1148) -----------

_BTO = RobinhoodEventKind.OPTION_BUY_TO_OPEN
_STC = RobinhoodEventKind.OPTION_SELL_TO_CLOSE
# Default synthetic activity privacy markers; each refusal test checks only
# markers its input carries, including in any chained exception.
_DESCRIPTION = "SYNTHDESC-4417"
_UNDERLYING = "ZQXV"
_DIGIT_RUN = "918273"
_MARKERS = (_DESCRIPTION, _UNDERLYING, _DIGIT_RUN)
# 52 significant digits, so a product with a 9-digit amount needs 61.
_LONG_RATE = Decimal("80." + (_DIGIT_RUN * 10)[:50])
# Past exact_fraction's digit cap (#1245): 1002 digits before the point, with
# a leading 9 so the digit run stays whole in the text, and a rate within the
# FX policy's (1, 200] with 1001 decimal places.
_PAST_THE_CAP = Decimal("9" + _DIGIT_RUN + "E+995")
_RATE_PAST_THE_CAP = Decimal("80." + (_DIGIT_RUN * 167)[:1001])


def _marked(
    kind: RobinhoodEventKind,
    ordinal: int,
    activity_date: date,
    *,
    quantity: str,
    price: str,
    amount: str,
) -> RobinhoodEvent:
    return _option(
        kind,
        ordinal,
        activity_date,
        activity_date + timedelta(days=1),
        quantity=quantity,
        price=price,
        amount=amount,
        symbol=_UNDERLYING,
        description=_DESCRIPTION,
    )


def _assert_refusal(
    excinfo: pytest.ExceptionInfo[RobinhoodOptionsError],
    expected: str,
    markers: tuple[str, ...] = _MARKERS,
) -> None:
    assert str(excinfo.value) == expected
    assert_prints_no_marker(excinfo.value, markers=markers)


def _amount_refusal(row: int) -> str:
    return (
        f"Robinhood option activity row {row}, column Amount: the trade amount needs more "
        "digits than exact option arithmetic allows"
    )


def _total_refusal(row: int) -> str:
    return (
        f"Robinhood option activity row {row}, column Amount: an option cost, proceeds or "
        "gain total needs more digits than exact option arithmetic allows"
    )


def _quantity_refusal(row: int) -> str:
    return (
        f"Robinhood option activity row {row}, column Quantity: the contract quantity needs "
        "more digits than exact option arithmetic allows"
    )


def _cut(value: Fraction) -> Decimal:
    """Expected 60-significant-digit cut for these test amounts (#1245)."""
    with localcontext(Context(prec=60, rounding=ROUND_DOWN)):
        return Decimal(value.numerator) / Decimal(value.denominator)


def _three_contract_events(*sales: str) -> tuple[RobinhoodEvent, ...]:
    """A purchase of 3 contracts at 0.33 for -100.00, then one-contract sales
    for ``sales`` on separate days."""
    return (
        _option(
            _BTO,
            2,
            date(2025, 5, 1),
            date(2025, 5, 2),
            quantity="3",
            price="0.33",
            amount="-100.00",
        ),
        *(
            _option(
                _STC,
                3 + index,
                date(2025, 6, 2 + index),
                date(2025, 6, 3 + index),
                quantity="1",
                price=str(Decimal(amount) / 100),
                amount=amount,
            )
            for index, amount in enumerate(sales)
        ),
    )


def _three_contract_statements(still_held: str | None = None) -> tuple[_Statement, ...]:
    return (
        _Statement(date(2025, 5, 1), date(2025, 5, 31), (_position(1, quantity="3"),)),
        _Statement(
            date(2025, 6, 1),
            date(2025, 6, 30),
            () if still_held is None else (_position(1, quantity=still_held),),
        ),
    )


def test_partial_closes_conserve_the_purchase_cost_and_file(tmp_path: Path) -> None:
    """Fails on cab08f7: the slices' 60-digit unit costs added to 100 - 1E-58."""
    result = _compute(
        _paired(
            _three_contract_events("40.00", "40.00", "20.00"),
            statements=_three_contract_statements(),
        ),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )

    assert result.blockers == ()
    assert not result.manual_work_required
    # Exact thirds (#1245); #1148 rounded them to 33.333333, 33.333334, 33.333333.
    costs = [item.exact_cost_usd for item in result.lot_slices]
    assert costs == [Fraction(100, 3)] * 3
    assert sum(costs) == 100
    assert [item.cost_usd for item in result.lot_slices] == [_cut(Fraction(100, 3))] * 3
    summary = result.business_summaries[0]
    assert summary.matched_premium_paid_usd == Decimal("100.00")
    assert summary.matched_premium_received_usd == Decimal("100.00")
    assert summary.net_pnl_usd == 0
    filing = compute_robinhood_option_business_filing(result)
    assert filing is not None
    assert (
        filing.turnover_inr,
        filing.gross_profit_inr,
        filing.expenditure_inr,
        filing.net_income_inr,
        filing.bp_income_inr,
        filing.cfl_loss_inr,
        filing.event_count,
    ) == (Decimal("2266"), Decimal("1133"), Decimal("1133"), 0, 0, 0, 3)
    assert [
        row["amount_inr"]
        for row in _workpaper_rows(result, tmp_path)
        if row["record_type"] == "itr-3-field"
        and row["treatment_or_kind"] == "CFL current AY / column 6"
    ] == ["0"]


@pytest.mark.parametrize(
    ("sales", "net_income_inr"),
    [
        (("50.00", "30.00", "20.00"), Decimal("0")),
        (("40.00", "40.00", "40.00"), Decimal("1700")),
    ],
    ids=["50-30-20", "40x3"],
)
def test_partial_closes_file_their_exact_net_income(
    sales: tuple[str, ...], net_income_inr: Decimal
) -> None:
    """Fails on cab08f7: 50/30/20 hit the reconciliation; 40x3 carried 1E-56 noise."""
    result = _compute(
        _paired(_three_contract_events(*sales), statements=_three_contract_statements()),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )

    filing = compute_robinhood_option_business_filing(result)

    assert filing is not None
    assert filing.net_income_inr_full_precision == net_income_inr
    assert result.business_summaries[0].net_pnl_inr == net_income_inr
    assert _slices_reconcile_with_trades(result)


def test_open_remainder_keeps_the_rest_of_the_purchase_cost() -> None:
    result = _compute(
        _paired(
            _three_contract_events("40.00"),
            statements=_three_contract_statements(still_held="2"),
        ),
        RobinhoodOptionTreatment.CAPITAL_GAINS,
    )

    [matched] = result.lot_slices
    assert matched.exact_cost_usd == Fraction(100, 3)
    assert matched.cost_usd == _cut(Fraction(100, 3))
    [open_lot] = result.open_lots
    assert open_lot.remaining_quantity == Decimal("2")
    # Exactly the rest (#1245); #1148 kept 66.666667.
    assert open_lot.exact_remaining_cost_usd == Fraction(200, 3)
    assert open_lot.remaining_cost_usd == _cut(Fraction(200, 3))


@pytest.mark.parametrize(
    ("contracts", "cash", "price"),
    [(10, "-0.06", "0.00006"), (7, "-0.000001", "0.0000000014")],
    ids=["10-for-0.06", "7-for-0.000001"],
)
def test_no_draw_of_a_purchase_cost_is_negative(contracts: int, cash: str, price: str) -> None:
    events = (
        _option(
            _BTO,
            2,
            date(2025, 5, 1),
            date(2025, 5, 2),
            quantity=str(contracts),
            price=price,
            amount=cash,
        ),
        *(
            _option(
                _STC,
                3 + day,
                date(2025, 6, 2 + day),
                date(2025, 6, 3 + day),
                quantity="1",
                price="0.0001",
                amount="0.01",
            )
            for day in range(contracts)
        ),
    )

    result = _compute(_paired(events, statements=()), RobinhoodOptionTreatment.CAPITAL_GAINS)

    costs = [item.exact_cost_usd for item in result.lot_slices]
    assert len(costs) == contracts
    assert all(cost >= 0 for cost in costs)
    assert all(item.cost_usd >= 0 for item in result.lot_slices)
    assert sum(costs) == -Fraction(cash)


def test_allocation_of_cash_finer_than_a_millionth_is_exact() -> None:
    """#1148 rounded these thirds of 1.00000001 to 0.33333334, 0.33333333 and
    0.33333334, at the cash's exponent; they are exact now (#1245)."""
    events = (
        _option(
            _BTO,
            2,
            date(2025, 5, 1),
            date(2025, 5, 2),
            quantity="3",
            price="0.0033333334",
            amount="-1.00000001",
        ),
        *(
            _option(
                _STC,
                3 + day,
                date(2025, 6, 2 + day),
                date(2025, 6, 3 + day),
                quantity="1",
                price="0.0001",
                amount="0.01",
            )
            for day in range(3)
        ),
    )

    result = _compute(_paired(events, statements=()), RobinhoodOptionTreatment.CAPITAL_GAINS)

    third = Fraction("1.00000001") / 3
    assert [item.exact_cost_usd for item in result.lot_slices] == [third] * 3
    assert [item.cost_usd for item in result.lot_slices] == [_cut(third)] * 3


def test_capital_gain_costs_add_back_to_the_converted_purchase_cash() -> None:
    result = _compute(
        _paired(
            _three_contract_events("40.00", "40.00", "20.00"),
            statements=_three_contract_statements(),
        ),
        RobinhoodOptionTreatment.CAPITAL_GAINS,
    )

    lines = result.capital_gain_lines
    assert len(lines) == 3
    assert sum(line.source_slice.exact_cost_usd * Fraction(line.fx_rate) for line in lines) == 8000
    assert [line.cost_inr for line in lines] == [_cut(Fraction(8000, 3))] * 3


def test_purchase_of_three_closed_by_one_and_two_files_as_before() -> None:
    events = (
        _option(_BTO, 2, date(2025, 5, 1), date(2025, 5, 2), quantity="3", amount="-300.00"),
        _option(
            _STC, 3, date(2025, 6, 2), date(2025, 6, 3), quantity="1", price="1.50", amount="150.00"
        ),
        _option(
            _STC, 4, date(2025, 6, 3), date(2025, 6, 4), quantity="2", price="1.50", amount="300.00"
        ),
    )

    result = _compute(
        _paired(events, statements=_three_contract_statements()),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )

    assert [item.cost_usd for item in result.lot_slices] == [Decimal("100"), Decimal("200")]
    filing = compute_robinhood_option_business_filing(result)
    assert filing is not None
    assert (filing.turnover_inr, filing.gross_profit_inr, filing.net_income_inr) == (
        Decimal("12750"),
        Decimal("12750"),
        Decimal("12750"),
    )


@pytest.mark.parametrize(
    ("quantity", "sales", "cash"),
    [
        ("1" * 56, ("1" * 56,), "123.45600"),
        ("2" * 56, ("1" * 56, "1" * 56), "61.72800"),
    ],
    ids=["whole-draw", "partial-draws"],
)
def test_large_quantity_draws_keep_exact_cash_text(
    quantity: str, sales: tuple[str, ...], cash: str
) -> None:
    events = (
        _option(
            _BTO,
            2,
            date(2025, 5, 1),
            date(2025, 5, 2),
            quantity=quantity,
            price="1E-55",
            amount="-123.45600",
        ),
        *(
            _option(
                _STC,
                3 + index,
                date(2025, 6, 2 + index),
                date(2025, 6, 3 + index),
                quantity=sold,
                price="1E-55",
                amount=cash,
            )
            for index, sold in enumerate(sales)
        ),
    )

    result = _compute(_paired(events, statements=()), RobinhoodOptionTreatment.CAPITAL_GAINS)

    assert result.blockers == ()
    assert [(str(item.cost_usd), str(item.net_proceeds_usd)) for item in result.lot_slices] == [
        (cash, cash)
    ] * len(sales)
    assert _slices_reconcile_with_trades(result)


def _same_day_purchase_trades(
    first: tuple[str, str], second: tuple[str, str]
) -> tuple[RobinhoodAccountsReconciliation, tuple[RobinhoodOptionTradeEvidence, ...]]:
    """Two same-day purchases, as ``(quantity, cash)``, rebuilt as trade evidence
    without the premium band, which would refuse some of these amounts first."""
    events = (
        _marked(_BTO, 2, date(2025, 5, 1), quantity="1", price="0.01", amount="-1.00"),
        _marked(_BTO, 3, date(2025, 5, 1), quantity="1", price="0.01", amount="-1.00"),
    )
    paired = _paired(events, statements=())
    trades = _compute(paired, RobinhoodOptionTreatment.CAPITAL_GAINS).trades
    return paired, tuple(
        replace(trade, quantity=Decimal(quantity), exact_cash_usd=Decimal(cash))
        for trade, (quantity, cash) in zip(trades, (first, second), strict=True)
    )


_SIXTY_DIGIT_CASH = "-" + _DIGIT_RUN * 9 + "9182.73"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ((("1", _SIXTY_DIGIT_CASH)), ("1", _SIXTY_DIGIT_CASH)),
        ((("1", _SIXTY_DIGIT_CASH)), ("3", "-1.00")),
    ],
    ids=["cash-sum", "price-comparison"],
)
def test_same_day_purchase_cash_that_cannot_be_grouped_exactly_refuses(
    first: tuple[str, str], second: tuple[str, str]
) -> None:
    """Fails on cab08f7: the 60-digit cash sum and the price quotient rounded."""
    paired, trades = _same_day_purchase_trades(first, second)

    with pytest.raises(RobinhoodOptionsError) as excinfo:
        _match_lots(paired, trades, financial_year_start=_FY_START, financial_year_end=_FY_END)

    _assert_refusal(excinfo, _amount_refusal(2))


# -(0.3 + 1E-60): 60 significant digits.
_JUST_OVER_THREE_TENTHS = "-0.3" + "0" * 58 + "1"


def test_same_day_prices_compare_exactly_by_cross_multiplying() -> None:
    """Fails on cab08f7: both cash amounts divided by 2 to the same 60 digits."""
    paired, trades = _same_day_purchase_trades(("2", "-0.3"), ("2", _JUST_OVER_THREE_TENTHS))

    _, _, blockers = _match_lots(
        paired, trades, financial_year_start=_FY_START, financial_year_end=_FY_END
    )

    assert [blocker.kind for blocker in blockers] == [RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY]


def test_same_day_prices_refuse_first_in_the_premium_band() -> None:
    """Fails on cab08f7: the cash's half-ulp allowance needs 61 digits (:696)."""
    events = (
        _marked(_BTO, 2, date(2025, 4, 15), quantity="1", price="91.8273", amount="-9182.73"),
        _marked(_BTO, 3, date(2025, 5, 1), quantity="2", price="0.0015", amount="-0.3"),
        _marked(
            _BTO,
            4,
            date(2025, 5, 1),
            quantity="2",
            price="0.0015",
            amount=_JUST_OVER_THREE_TENTHS,
        ),
    )

    with pytest.raises(RobinhoodOptionsError) as excinfo:
        _compute(_paired(events, statements=()), RobinhoodOptionTreatment.CAPITAL_GAINS)

    _assert_refusal(excinfo, _amount_refusal(4), markers=(_DESCRIPTION, _UNDERLYING))


def test_failed_premium_band_residual_that_cannot_subtract_exactly_refuses() -> None:
    """Fails on cab08f7: the fallback's cash - mid (:744) rounded to 60 digits."""
    events = (
        _marked(_BTO, 2, date(2025, 5, 1), quantity="1", price="0.000001", amount="-9.18273E+57"),
    )

    with pytest.raises(RobinhoodOptionsError) as excinfo:
        _compute(_paired(events, statements=()), RobinhoodOptionTreatment.CAPITAL_GAINS)

    _assert_refusal(excinfo, _amount_refusal(2), markers=(_DESCRIPTION, _UNDERLYING))


def test_same_day_purchases_at_one_price_still_group() -> None:
    events = (
        _option(
            _BTO,
            2,
            date(2025, 5, 1),
            date(2025, 5, 2),
            quantity="1",
            price="0.3333",
            amount="-33.33",
        ),
        _option(
            _BTO,
            3,
            date(2025, 5, 1),
            date(2025, 5, 2),
            quantity="2",
            price="0.3333",
            amount="-66.66",
        ),
        _option(
            _STC, 4, date(2025, 6, 2), date(2025, 6, 3), quantity="3", price="0.40", amount="120.00"
        ),
    )

    result = _compute(_paired(events, statements=()), RobinhoodOptionTreatment.CAPITAL_GAINS)

    assert RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY not in {
        blocker.kind for blocker in result.blockers
    }
    [matched] = result.lot_slices
    assert matched.open_references == (
        RobinhoodSecurityEventReference(0, 2),
        RobinhoodSecurityEventReference(0, 3),
    )
    assert matched.cost_usd == Decimal("99.99")


def test_slice_pnl_past_sixty_digits_is_exact() -> None:
    """#1148 refused this 58-digit proceeds less an 8-decimal cost, which needs
    64 digits. Exact shares can't round, so it computes (#1245)."""
    proceeds = _DIGIT_RUN * 9 + "82.73"
    events = (
        _marked(
            _BTO, 2, date(2025, 5, 1), quantity="1", price="0.0000000001", amount="-0.00000001"
        ),
        _marked(
            _STC,
            3,
            date(2025, 6, 2),
            quantity="1",
            price=str(Decimal(proceeds).scaleb(-2)),
            amount=proceeds,
        ),
    )

    result = _compute(_paired(events, statements=()), RobinhoodOptionTreatment.CAPITAL_GAINS)

    [item] = result.lot_slices
    assert item.exact_net_pnl_usd == Fraction(proceeds) - Fraction("0.00000001")
    assert item.net_pnl_usd == _cut(item.exact_net_pnl_usd)
    assert _slices_reconcile_with_trades(result)


def _seven_contract_events(*sales: str) -> tuple[RobinhoodEvent, ...]:
    """A purchase of 7 contracts for -9182.73, so each slice cost has 6 decimals,
    then one-contract sales for ``sales``."""
    return (
        _marked(_BTO, 2, date(2025, 5, 1), quantity="7", price="13.1182", amount="-9182.73"),
        *(
            _marked(
                _STC,
                3 + index,
                date(2025, 6, 2 + index),
                quantity="1",
                price=str(Decimal(amount).scaleb(-2)),
                amount=amount,
            )
            for index, amount in enumerate(sales)
        ),
    )


def _seven_contract_statements(still_held: int) -> tuple[_Statement, ...]:
    return (
        _Statement(
            date(2025, 5, 1),
            date(2025, 5, 31),
            (_position(1, quantity="7", symbol=_UNDERLYING, description=_DESCRIPTION),),
        ),
        _Statement(
            date(2025, 6, 1),
            date(2025, 6, 30),
            ()
            if still_held == 0
            else (
                _position(
                    1, quantity=str(still_held), symbol=_UNDERLYING, description=_DESCRIPTION
                ),
            ),
        ),
    )


_SEVEN_SALES = ("1500.00", "1100.00", "1500.00", "1100.00", "1500.00", "1100.00", "1500.00")


def _half_up(value: Fraction) -> Decimal:
    """A non-negative ``value`` rounded half up to whole rupees."""
    assert value >= 0
    return Decimal(int(value + Fraction(1, 2)))


def test_capital_gains_at_a_long_rate_are_exact() -> None:
    """#1148 refused each slice gain times a 52-digit rate, which needs over 60
    digits. Exact shares can't round, so they convert (#1245)."""
    result = _compute(
        _paired(_seven_contract_events(*_SEVEN_SALES), statements=_seven_contract_statements(0)),
        RobinhoodOptionTreatment.CAPITAL_GAINS,
        capital_rate=_LONG_RATE,
    )

    assert len(result.capital_gain_lines) == 7
    for line in result.capital_gain_lines:
        exact = line.source_slice.exact_net_pnl_usd * Fraction(_LONG_RATE)
        assert line.net_gain_inr == _cut(exact)


def test_business_summary_at_a_long_rate_is_exact() -> None:
    """#1148 refused the matched cost times a 52-digit rate (#1245)."""
    result = _compute(
        _paired(_seven_contract_events("1500.00"), statements=_seven_contract_statements(6)),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
        business_rate=_LONG_RATE,
    )

    [summary] = result.business_summaries
    cost = Fraction("9182.73") / 7
    assert summary.exact_net_pnl_inr == (1500 - cost) * Fraction(_LONG_RATE)
    assert summary.matched_premium_paid_inr == _cut(cost * Fraction(_LONG_RATE))


def test_business_filing_at_a_long_rate_files_the_exact_rupees() -> None:
    """#1148 refused the turnover total times a 52-digit rate (#1245)."""
    result = _compute(
        _paired(_seven_contract_events(*_SEVEN_SALES), statements=_seven_contract_statements(0)),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
        business_rate=_LONG_RATE,
    )
    assert result.blockers == ()

    filing = compute_robinhood_option_business_filing(result)

    assert filing is not None
    cost, rate = Fraction("9182.73") / 7, Fraction(_LONG_RATE)
    gains = sum(Fraction(sale) - cost for sale in _SEVEN_SALES if Fraction(sale) > cost)
    losses = sum(cost - Fraction(sale) for sale in _SEVEN_SALES if Fraction(sale) < cost)
    assert filing.gross_profit_inr == _half_up(gains * rate)
    assert filing.turnover_inr == _half_up(gains * rate) + _half_up(losses * rate)
    assert filing.net_income_inr_full_precision == _cut((gains - losses) * rate)


def test_business_fsi_over_two_accounts_adds_their_exact_net_income() -> None:
    """Two accounts' exact net INR, ₹561/6 and ₹561/3, add to ₹280.50 and file
    ₹281. #1148 added their 60-digit Decimals, here cut to ₹280.49...9 (#1245)."""
    result = _compute(
        _paired(_seven_contract_events(*_SEVEN_SALES), statements=_seven_contract_statements(0)),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
    )
    [summary] = result.business_summaries
    two_accounts = replace(
        result,
        business_summaries=tuple(
            replace(summary, exact_net_pnl_inr=exact, net_pnl_inr=_cut(exact))
            for exact in (Fraction(561, 6), Fraction(561, 3))
        ),
    )

    [row] = compute_robinhood_option_business_fsi(two_accounts).rows

    assert row.income_inr == Decimal("280.5")


def _oversold_contract_events() -> tuple[RobinhoodEvent, ...]:
    """1E61 contracts bought, then 1 and 1E61 sold: one more than was bought."""
    return (
        _marked(
            _BTO, 2, date(2025, 5, 1), quantity="1E61", price="9.18273E-58", amount="-918273.00"
        ),
        _marked(_STC, 3, date(2025, 6, 2), quantity="1", price="0.01", amount="1.00"),
        _marked(
            _STC, 4, date(2025, 6, 3), quantity="1E61", price="9.18273E-58", amount="918273.00"
        ),
    )


def test_contract_quantity_that_cannot_net_exactly_refuses_under_business_treatment() -> None:
    """Fails on 0d3547b: the option engine's ``_consume`` trap refused 1E61 - 1
    at row 3. FIFO now subtracts exactly, and the 60-digit net quantity
    refuses at the first trade's row."""
    with pytest.raises(RobinhoodOptionsError) as excinfo:
        _compute(
            _paired(_oversold_contract_events(), statements=()),
            RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
        )

    _assert_refusal(excinfo, _quantity_refusal(2))


def test_contract_sold_past_an_exact_1e61_holding_is_negative_inventory() -> None:
    """Fails on 0d3547b: the option engine's ``_consume`` trap refused 1E61 - 1
    at row 3. FIFO now holds exactly 1E61 - 1 contracts after row 3, so row 4's
    1E61 is one contract short."""
    result = _compute(
        _paired(_oversold_contract_events(), statements=()),
        RobinhoodOptionTreatment.CAPITAL_GAINS,
    )

    row_4 = RobinhoodSecurityEventReference(0, 4)
    assert [
        (blocker.kind, blocker.event_references, blocker.located_at) for blocker in result.blockers
    ] == [(RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY, (row_4,), row_4)]
    assert result.lot_slices == ()
    assert result.open_lots == ()


def test_fifo_pool_takes_one_contract_from_1e61_exactly() -> None:
    """Fails on 0d3547b: a plain ``consume`` of 1 from a 1E61 pool left 1E61.
    (Base's ``consume`` takes no ``at`` either.)"""
    opens: FifoPool[str] = FifoPool()
    opens.add("opened", Decimal("1E61"))

    opens.consume(Decimal("1"), at="Robinhood option activity row 3, column Quantity")

    assert opens.remaining() == (("opened", Decimal(10**61 - 1)),)


@pytest.mark.parametrize(
    "treatment",
    [RobinhoodOptionTreatment.CAPITAL_GAINS, RobinhoodOptionTreatment.SPECULATIVE_BUSINESS],
)
def test_contract_quantity_fifo_cannot_subtract_within_the_digit_cap_refuses(
    treatment: RobinhoodOptionTreatment,
) -> None:
    """1E+600 contracts less 1E-500 needs 1,101 digits. On 0d3547b the
    ``_consume`` trap refused this at row 3 as a ``RobinhoodOptionsError``;
    FIFO's exact subtraction now refuses it at the same row (#1273)."""
    events = (
        _marked(
            _BTO, 2, date(2025, 5, 1), quantity="1E+600", price="9.18273E-598", amount="-918273.00"
        ),
        _marked(_STC, 3, date(2025, 6, 2), quantity="1E-500", price="1E+498", amount="1.00"),
    )

    with pytest.raises(InvariantError) as excinfo:
        _compute(_paired(events, statements=()), treatment)

    error = excinfo.value
    assert str(error) == (
        f"InvariantError: Robinhood option activity row 3, column Quantity: {INEXACT_SUM}"
    )
    assert error.context == ()
    assert error.__cause__ is None
    assert error.__context__ is None
    texts = (str(error), repr(error), *map(str, error.args))
    assert not any(marker in text for text in texts for marker in _MARKERS)


def test_same_day_contract_quantities_that_cannot_add_exactly_refuse() -> None:
    """Fails on cab08f7: the group quantity 1E60 + 1 rounded (:618)."""
    events = (
        _marked(
            _BTO, 2, date(2025, 5, 1), quantity="1E60", price="9.18273E-57", amount="-918273.00"
        ),
        _marked(
            _BTO, 3, date(2025, 5, 1), quantity="1", price="9.18273E-57", amount="-9.18273E-55"
        ),
    )

    with pytest.raises(RobinhoodOptionsError) as excinfo:
        _compute(_paired(events, statements=()), RobinhoodOptionTreatment.CAPITAL_GAINS)

    _assert_refusal(excinfo, _quantity_refusal(2))


def test_activity_quantity_sum_without_a_statement_row_refuses() -> None:
    """Fails on cab08f7: the expected position 1E60 + 1 rounded (:917)."""
    events = (
        _marked(
            _BTO, 2, date(2025, 5, 1), quantity="1E60", price="9.18273E-57", amount="-918273.00"
        ),
        _marked(_BTO, 3, date(2025, 5, 2), quantity="1", price="0.01", amount="-1.00"),
    )
    statements = (_Statement(date(2025, 5, 1), date(2025, 5, 31), ()),)

    with pytest.raises(RobinhoodOptionsError) as excinfo:
        _compute(_paired(events, statements=statements), RobinhoodOptionTreatment.CAPITAL_GAINS)

    _assert_refusal(excinfo, _quantity_refusal(2))


def test_opening_quantity_sum_that_cannot_add_exactly_refuses() -> None:
    """Fails on cab08f7: the quantity held at the FY start, 1E60 + 1, rounded (:1115)."""
    events = (
        _marked(
            _BTO, 2, date(2025, 3, 3), quantity="1E60", price="9.18273E-57", amount="-918273.00"
        ),
        _marked(_BTO, 3, date(2025, 3, 4), quantity="1", price="0.01", amount="-1.00"),
    )

    with pytest.raises(RobinhoodOptionsError) as excinfo:
        _compute(
            _paired(events, statements=(), report_start=date(2025, 1, 1)),
            RobinhoodOptionTreatment.CAPITAL_GAINS,
        )

    _assert_refusal(excinfo, _quantity_refusal(2))


def test_statement_position_quantity_sum_that_cannot_add_exactly_refuses() -> None:
    """Fails on cab08f7: 1E60 + 1 rounded, giving a position-reconciliation blocker."""
    markers = ("SYNTHDESC-4418", "ZQXU", "918274")
    positions = tuple(
        _position(
            row,
            quantity=quantity,
            price="918274.00",
            page=2,
            symbol="ZQXU",
            description="SYNTHDESC-4418",
        )
        for row, quantity in ((3, "1E60"), (4, "1"))
    )
    statements = (_Statement(date(2025, 5, 1), date(2025, 5, 31), positions),)

    with pytest.raises(RobinhoodOptionsError) as excinfo:
        _compute(_paired((_cash_event(),), statements=statements), None)

    _assert_refusal(
        excinfo,
        "Robinhood option account 1, Securities Held in Account: statement 1 page 2 row 4, "
        "column Qty: the position quantity needs more digits than exact option arithmetic "
        "allows",
        markers=markers,
    )


def test_open_remainder_cost_past_sixty_digits_is_exact() -> None:
    """#1148 refused the remaining 3E10 - 1 contracts' share of 1E+55 rounded
    to 1E-6, which needs 61 digits. It is exactly the rest now (#1245)."""
    events = (
        _marked(_BTO, 2, date(2025, 5, 1), quantity="3E10", price="3.3333E+42", amount="-1E+55"),
        _marked(_STC, 3, date(2025, 6, 2), quantity="1", price="91.8273", amount="9182.73"),
    )

    result = _compute(_paired(events, statements=()), RobinhoodOptionTreatment.CAPITAL_GAINS)

    [open_lot] = result.open_lots
    assert open_lot.exact_remaining_cost_usd == Fraction(10**55) * (3 * 10**10 - 1) / (3 * 10**10)


# ---- exact option shares (#1245) --------------------------------------------


def _split_across_fys() -> RobinhoodAccountsReconciliation:
    """3 calls bought for $10.00 in March 2025: 1 sold that March for $4.00, in
    FY 2024-25, and 2 sold in May for $10.00, in FY 2025-26."""
    return _paired(
        (
            _option(
                _BTO,
                2,
                date(2025, 3, 10),
                date(2025, 3, 11),
                quantity="3",
                price="0.03",
                amount="-10.00",
            ),
            _option(
                _STC,
                3,
                date(2025, 3, 20),
                date(2025, 3, 21),
                quantity="1",
                price="0.04",
                amount="4.00",
            ),
            _option(
                _STC,
                4,
                date(2025, 5, 15),
                date(2025, 5, 16),
                quantity="2",
                price="0.05",
                amount="10.00",
            ),
        ),
        statements=(
            _Statement(date(2025, 3, 1), date(2025, 3, 31), (_position(1, price="0.05"),)),
            _Statement(date(2025, 4, 1), date(2025, 4, 30), (_position(1, price="0.05"),)),
            _Statement(date(2025, 5, 1), date(2025, 5, 31), ()),
        ),
        report_start=date(2025, 1, 1),
    )


def test_a_purchase_split_across_fys_files_its_exact_business_income() -> None:
    """Fails on 02ff79d: the FY 2025-26 slice cost 6.666667, so the profit was
    ₹280.49997195 and filed ₹280. Exactly, it is ($10 - $20/3) x 84.15 =
    ₹280.50, which files ₹281 (#1245)."""
    result = _compute(
        _split_across_fys(),
        RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
        business_rate=Decimal("84.15"),
    )
    assert result.blockers == ()
    assert not result.manual_work_required

    filing = compute_robinhood_option_business_filing(result)

    assert filing is not None
    assert filing.net_income_inr_full_precision == Decimal("280.50")
    assert (filing.turnover_inr, filing.net_income_inr, filing.bp_income_inr) == (
        Decimal("281"),
        Decimal("281"),
        Decimal("281"),
    )
    [row] = compute_robinhood_option_business_fsi(result).rows
    assert row.income_inr == Decimal("281")


def test_a_purchase_split_across_fys_has_an_exact_capital_gain_line() -> None:
    """Fails on 02ff79d: the FY 2025-26 line cost ₹561.00002805 and gained
    ₹280.49997195. Its exact cost is $20/3 x 84.15 = ₹561 (#1245)."""
    result = _compute(
        _split_across_fys(),
        RobinhoodOptionTreatment.CAPITAL_GAINS,
        capital_rate=Decimal("84.15"),
    )

    [line] = result.capital_gain_lines
    assert line.source_slice.close_date == date(2025, 5, 15)
    assert (line.cost_inr, line.net_proceeds_inr, line.net_gain_inr) == (
        Decimal("561"),
        Decimal("841.50"),
        Decimal("280.50"),
    )


def _assert_digit_cap_refusal(
    excinfo: pytest.ExceptionInfo[RobinhoodOptionsError], row: int
) -> None:
    """The total refusal at ``row``, with no marker in its text, repr or args
    and no exception chained, suppressed context included (#1245)."""
    # Each input past the cap carries the digit run.
    assert _DIGIT_RUN in str(_PAST_THE_CAP)
    assert _DIGIT_RUN in str(_RATE_PAST_THE_CAP)
    _assert_refusal(excinfo, _total_refusal(row))
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_a_draw_of_purchase_cash_past_the_digit_cap_refuses() -> None:
    """A sale of 1 of 2 contracts bought for 1002 digits of cash: its exact
    share can't be had (#1245)."""
    events = (
        _marked(
            _BTO,
            2,
            date(2025, 5, 1),
            quantity="2",
            price=str(_PAST_THE_CAP / 200),
            amount=str(-_PAST_THE_CAP),
        ),
        _marked(_STC, 3, date(2025, 6, 2), quantity="1", price="0.01", amount="1.00"),
    )

    with pytest.raises(RobinhoodOptionsError) as excinfo:
        _compute(_paired(events, statements=()), RobinhoodOptionTreatment.CAPITAL_GAINS)

    _assert_digit_cap_refusal(excinfo, 3)


def test_an_open_remainder_past_the_digit_cap_refuses() -> None:
    """A purchase of 1002 digits of cash that stays open (#1245)."""
    events = (
        _marked(
            _BTO,
            2,
            date(2025, 5, 1),
            quantity="1",
            price=str(_PAST_THE_CAP / 100),
            amount=str(-_PAST_THE_CAP),
        ),
    )

    with pytest.raises(RobinhoodOptionsError) as excinfo:
        _compute(_paired(events, statements=()), RobinhoodOptionTreatment.CAPITAL_GAINS)

    _assert_digit_cap_refusal(excinfo, 2)


def _round_trip() -> RobinhoodAccountsReconciliation:
    return _paired(
        (
            _marked(_BTO, 2, date(2025, 5, 1), quantity="1", price="91.8273", amount="-9182.73"),
            _marked(_STC, 3, date(2025, 6, 2), quantity="1", price="91.8274", amount="9182.74"),
        ),
        statements=(),
    )


@pytest.mark.parametrize(
    ("treatment", "capital_rate", "business_rate"),
    [
        (RobinhoodOptionTreatment.CAPITAL_GAINS, _RATE_PAST_THE_CAP, Decimal("85")),
        (RobinhoodOptionTreatment.SPECULATIVE_BUSINESS, Decimal("80"), _RATE_PAST_THE_CAP),
    ],
    ids=["capital-gain-line", "business-summary"],
)
def test_a_rate_past_the_digit_cap_refuses(
    treatment: RobinhoodOptionTreatment, capital_rate: Decimal, business_rate: Decimal
) -> None:
    """#1245: the rate has no exact rational to convert at."""
    with pytest.raises(RobinhoodOptionsError) as excinfo:
        _compute(_round_trip(), treatment, capital_rate=capital_rate, business_rate=business_rate)

    _assert_digit_cap_refusal(excinfo, 3)


def test_business_filing_expenses_past_the_digit_cap_refuse() -> None:
    """#1245: reviewed external expenses with no exact rational."""
    result = _compute(_round_trip(), RobinhoodOptionTreatment.SPECULATIVE_BUSINESS)
    reviewed = replace(
        result,
        classifications=(replace(result.classifications[0], external_expenses_inr=_PAST_THE_CAP),),
    )

    with pytest.raises(RobinhoodOptionsError) as excinfo:
        compute_robinhood_option_business_filing(reviewed)

    _assert_digit_cap_refusal(excinfo, 3)


# ---- whole option contracts and located option blockers (#1134) -------------

_CG = RobinhoodOptionTreatment.CAPITAL_GAINS
_REF_2 = RobinhoodSecurityEventReference(0, 2)
_MAY = (date(2025, 5, 1), date(2025, 5, 31))
_JUNE = (date(2025, 6, 1), date(2025, 6, 30))
_JULY = (date(2025, 7, 1), date(2025, 7, 31))
# A report window that runs past the synthetic expiry.
_PAST_EXPIRY = date(2031, 3, 31)


def _synth_position(row: int, *, quantity: str = "2", page: int = 1) -> RobinhoodStatementPosition:
    return _position(
        row,
        quantity=quantity,
        price="736251.00",
        page=page,
        symbol=_SYNTH_UNDERLYING,
        description=_SYNTH_DESCRIPTION,
        expiration=_SYNTH_EXPIRATION,
    )


def _statement(period: tuple[date, date], *positions: RobinhoodStatementPosition) -> _Statement:
    return _Statement(period[0], period[1], positions)


def _kind_blockers(
    result: RobinhoodOptionReconstruction, kind: RobinhoodOptionBlockerKind
) -> list[RobinhoodOptionBlocker]:
    return [blocker for blocker in result.blockers if blocker.kind is kind]


def _assert_located(
    blockers: Iterable[RobinhoodOptionBlocker], kind: RobinhoodOptionBlockerKind, located: str
) -> None:
    """``blockers`` summarise as ``kind``'s literal message located at
    ``located``, with no synthetic marker."""
    summary = option_blocker_summary(blockers)
    assert summary == f"{OPTION_BLOCKER_MESSAGES[kind]} ({located})"
    assert not any(marker in summary for marker in _SYNTH_MARKERS)


@pytest.mark.parametrize(
    ("cash", "blocks"),
    [
        ("-249.00", True),
        ("-202.00", True),
        ("-201.01", True),
        ("-198.99", True),
        ("-201.005", True),
        ("-198.995", True),
        ("-201.00", False),
        ("-200.00", False),
        ("-199.00", False),
    ],
)
def test_premium_band_allows_only_price_and_cash_display_rounding(cash: str, blocks: bool) -> None:
    """Fails on cab08f7: half a contract either side accepted 149.25 to 251.25."""
    purchase, sale = _closed_events()

    result = _compute(_paired((replace(purchase, amount_usd=Decimal(cash)), sale)), _CG)

    assert result.blockers == (
        (
            RobinhoodOptionBlocker(
                kind=RobinhoodOptionBlockerKind.PREMIUM_RECONCILIATION,
                account_index=0,
                contracts=(result.trades[0].contract,),
                event_references=(_REF_2,),
                located_at=_REF_2,
                columns=("Quantity", "Price", "Amount"),
            ),
        )
        if blocks
        else ()
    )


@pytest.mark.parametrize(
    ("quantity", "amount"),
    [
        ("2." + "0" * 60, "-200.00"),
        ("1" + "0" * 59, "-1E61"),
        ("1" + "0" * 56, "-1E58"),
    ],
    ids=["quantity-step", "large-quantity-step", "previous-bound-product"],
)
def test_tighter_premium_band_preserves_existing_exactness_refusals(
    quantity: str, amount: str
) -> None:
    purchase = _option(
        _BTO,
        7,
        _MAY[0],
        _MAY[0] + timedelta(days=1),
        quantity=quantity,
        price="1.00",
        amount=amount,
        expiration=_SYNTH_EXPIRATION,
    )

    with pytest.raises(RobinhoodOptionsError) as excinfo:
        _compute(_paired((purchase,), statements=()), _CG)

    assert str(excinfo.value) == _amount_refusal(7)


def test_previous_premium_precision_limit_still_accepts_its_exact_boundary() -> None:
    purchase = _option(
        _BTO,
        7,
        _MAY[0],
        _MAY[0] + timedelta(days=1),
        quantity="1" + "0" * 55,
        price="1.00",
        amount="-1E57",
        expiration=_SYNTH_EXPIRATION,
    )

    result = _compute(_paired((purchase,), statements=()), _CG)

    assert result.blockers == ()
    assert len(result.open_lots) == 1


def test_fractional_activity_quantity_is_an_unsupported_contract() -> None:
    """Fails on cab08f7: 2.5 contracts were reconstructed like any other trade."""
    purchase = _option(
        _BTO, 2, date(2025, 5, 1), date(2025, 5, 2), quantity="2.5", amount="-250.00"
    )

    result = _compute(_paired((purchase,), statements=()), _CG)

    assert [
        (blocker.event_references, blocker.located_at, blocker.columns)
        for blocker in _kind_blockers(result, RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT)
    ] == [((_REF_2,), _REF_2, ("Quantity",))]


def test_whole_activity_quantity_written_with_a_decimal_place_is_supported() -> None:
    purchase, sale = _closed_events()
    purchase = replace(purchase, quantity=Decimal("2.0"), amount_usd=Decimal("-200.00"))

    assert _compute(_paired((purchase, sale)), _CG).blockers == ()


@pytest.mark.parametrize(
    ("quantities", "blocked_rows"),
    [(("1.5",), [1]), (("0.5", "1.5"), [1, 2]), (("2",), []), (("2.0",), [])],
    ids=["one-and-a-half", "halves-adding-to-two", "two", "two-point-zero"],
)
def test_each_statement_position_is_checked_for_whole_contracts(
    quantities: tuple[str, ...], blocked_rows: list[int]
) -> None:
    """Fails on cab08f7 for the fractions: each position is checked before any sum."""
    positions = (_position(row, quantity=q) for row, q in enumerate(quantities, start=1))

    result = _compute(
        _paired(_closed_events(), statements=(_statement(_MAY, *positions), _statement(_JUNE))),
        _CG,
    )

    assert [
        (blocker.position_references, blocker.located_at, blocker.columns)
        for blocker in _kind_blockers(result, RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT)
    ] == [
        ((reference,), reference, ("Qty",))
        for reference in (
            RobinhoodPositionReference(
                0, 0, SourceLocation(page=1, section=RobinhoodStatementSection.SECURITIES, row=row)
            )
            for row in blocked_rows
        )
    ]
    if not blocked_rows:
        assert result.blockers == ()


@pytest.mark.parametrize(
    ("position", "columns"),
    [
        (replace(_position(1, quantity="0"), market_value_usd=Decimal("0.00")), ("Qty",)),
        (replace(_position(1, quantity="-2"), market_value_usd=Decimal("0.00")), ("Qty",)),
        (_position(1, right="Put"), ("Description",)),
        (replace(_position(1), market_value_usd=Decimal("-1")), ("Mkt Value",)),
        (
            replace(_position(1, quantity="-1.5", right="Put"), market_value_usd=Decimal("-1")),
            ("Description", "Qty", "Mkt Value"),
        ),
    ],
    ids=["zero", "negative", "put", "negative-market-value", "all-three"],
)
def test_statement_position_refusal_names_each_failing_column(
    position: RobinhoodStatementPosition, columns: tuple[str, ...]
) -> None:
    result = _compute(
        _paired(_closed_events(), statements=(_statement(_MAY, position), _statement(_JUNE))),
        _CG,
    )

    assert [
        blocker.columns
        for blocker in _kind_blockers(result, RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT)
    ] == [columns]


def _table_case(
    events: tuple[RobinhoodEvent, ...],
    treatment: RobinhoodOptionTreatment | None = _CG,
    *,
    statements: tuple[_Statement, ...] = (),
    **kwargs: Any,
) -> Callable[..., RobinhoodOptionReconstruction]:
    """Builds the case with ``_compute``, which also checks that the
    reconstruction alone gives the same blockers (#1145)."""
    paired_keys = {"report_start", "report_end", "other_statements"}
    paired_kwargs = {key: value for key, value in kwargs.items() if key in paired_keys}
    compute_kwargs = {key: value for key, value in kwargs.items() if key not in paired_keys}
    return lambda: _compute(
        _paired(events, statements=statements, **paired_kwargs), treatment, **compute_kwargs
    )


_JUNE_2 = date(2025, 6, 2)
# One case per blocker creation site, each with its literal location.
_SITE_CASES = [
    pytest.param(
        _table_case((_synth_trade(_BTO, 2, _MAY[0], right="Put"),), None),
        RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT,
        "1 blocker; first at account 1, activity report row 2; column Description",
        id="activity-right",
    ),
    pytest.param(
        _table_case((_synth_trade(_BTO, 7, _MAY[0], quantity="2.5"),)),
        RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT,
        "1 blocker; first at account 1, activity report row 7; column Quantity",
        id="activity-fraction",
    ),
    pytest.param(
        _table_case(
            (_synth_trade(_BTO, 2, _MAY[0]), _synth_trade(_STC, 3, date(2031, 3, 3))),
            report_end=_PAST_EXPIRY,
        ),
        RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE,
        "1 blocker; first at account 1, activity report row 3; columns Activity Date and "
        "Description",
        id="activity-after-expiry",
    ),
    pytest.param(
        _table_case((_synth_trade(_BTO, 9, _MAY[0], price="3681.256"),)),
        RobinhoodOptionBlockerKind.PREMIUM_RECONCILIATION,
        "1 blocker; first at account 1, activity report row 9; columns Quantity, Price and Amount",
        id="premium",
    ),
    pytest.param(
        _table_case(
            (_cash_event(),),
            None,
            other_statements=(
                _statement(_MAY),
                _statement(_JUNE, _synth_position(4, quantity="1.5", page=3)),
            ),
        ),
        RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT,
        "1 blocker; first at account 2, Securities Held in Account: statement 2 page 3 row 4; "
        "column Qty",
        id="statement-fraction",
    ),
    pytest.param(
        _table_case(
            (_synth_trade(_BTO, 2, _MAY[0], quantity="1"), _synth_trade(_STC, 3, _JUNE_2)),
            statements=(_statement(_JUNE),),
        ),
        RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY,
        # The statement's check and the sale's: both at the sale that oversold.
        "2 blockers; first at account 1, activity report row 3; columns Trans Code and Quantity",
        id="negative-inventory",
    ),
    pytest.param(
        _table_case(
            (_synth_trade(_BTO, 2, _MAY[0], quantity="1"), _synth_trade(_STC, 3, _JUNE_2)),
        ),
        RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY,
        "1 blocker; first at account 1, activity report row 3; columns Trans Code and Quantity",
        id="fifo-negative-inventory",
    ),
    pytest.param(
        _table_case(
            (_synth_trade(_BTO, 2, _MAY[0]),),
            statements=(_statement(_MAY, _synth_position(1, quantity="3")),),
        ),
        RobinhoodOptionBlockerKind.POSITION_RECONCILIATION,
        "1 blocker; first at account 1, Securities Held in Account: statement 1 page 1 row 1; "
        "column Qty",
        id="position-printed",
    ),
    pytest.param(
        _table_case((_synth_trade(_BTO, 2, _MAY[0]),), statements=(_statement(_MAY),)),
        RobinhoodOptionBlockerKind.POSITION_RECONCILIATION,
        "1 blocker; first at account 1, activity report row 2; column Quantity "
        "(no matching position on statement 1)",
        id="position-missing",
    ),
    pytest.param(
        _table_case((_synth_trade(_BTO, 2, _MAY[0]), _synth_trade(_STC, 3, _MAY[0]))),
        RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY,
        "1 blocker; first at account 1, activity report row 2; columns Activity Date and "
        "Trans Code",
        id="opens-and-closes",
    ),
    pytest.param(
        _table_case(
            (
                _synth_trade(_BTO, 2, _MAY[0], quantity="1"),
                _synth_trade(_BTO, 3, _MAY[0], quantity="1", price="7362.5101", amount="736251.01"),
            )
        ),
        RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY,
        "1 blocker; first at account 1, activity report row 2; columns Activity Date, "
        "Quantity and Amount",
        id="fill-prices",
    ),
    pytest.param(
        _table_case((_synth_trade(_BTO, 2, _MAY[0]),), report_end=_PAST_EXPIRY),
        RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE,
        "1 blocker; first at account 1, activity report row 2; columns Description and Quantity",
        id="held-at-expiry",
    ),
    pytest.param(
        _table_case(
            (
                _synth_trade(_BTO, 2, date(2023, 6, 1)),
                _synth_trade(_STC, 3, date(2024, 5, 15)),
            ),
            report_start=date(2023, 6, 1),
            opening_date=date(2023, 6, 1),
            financial_year=FinancialYear(2024),
        ),
        RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE,
        "1 blocker; first at account 1, activity report row 3; column Activity Date",
        id="pre-cutover",
    ),
    pytest.param(
        _table_case(
            (_synth_trade(_BTO, 2, _MAY[0]),), RobinhoodOptionTreatment.SPECULATIVE_BUSINESS
        ),
        RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION,
        "1 blocker; first at account 1, option positions open at the financial year's end",
        id="open-business-position",
    ),
]


@pytest.mark.parametrize(("build", "kind", "located"), _SITE_CASES)
def test_each_blocker_site_names_its_first_row_and_columns(
    build: Callable[..., RobinhoodOptionReconstruction],
    kind: RobinhoodOptionBlockerKind,
    located: str,
) -> None:
    """Fails on cab08f7: the summary gave each kind's message and count only."""
    _assert_located(_kind_blockers(build(), kind), kind, located)


@pytest.mark.parametrize(
    ("later_day", "columns"),
    [
        (
            (
                _synth_trade(_STC, 7, _JUNE_2, quantity="1"),
                _synth_trade(_BTO, 9, _JUNE_2, quantity="1"),
            ),
            "columns Activity Date and Trans Code",
        ),
        (
            (
                _synth_trade(_STC, 7, _JUNE_2, quantity="1"),
                _synth_trade(_STC, 9, _JUNE_2, quantity="1", price="7362.5101", amount="736251.01"),
            ),
            "columns Activity Date, Quantity and Amount",
        ),
    ],
    ids=["opens-and-closes", "fill-prices"],
)
def test_ordering_blocker_is_located_at_the_first_row_of_the_failing_day(
    later_day: tuple[RobinhoodEvent, ...], columns: str
) -> None:
    """Not at row 2, the contract's first trade, nor at the purchase group that
    sorts first on the failing day."""
    events = (_synth_trade(_BTO, 2, _MAY[0]), *later_day)

    result = _compute(_paired(events, statements=()), _CG)

    kind = RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY
    _assert_located(
        result.blockers, kind, f"1 blocker; first at account 1, activity report row 7; {columns}"
    )


@pytest.mark.parametrize(
    ("sale_day", "june", "located"),
    [
        (
            date(2025, 8, 1),
            "2",
            "1 blocker; first at account 1, activity report row 2; column Quantity "
            "(no matching position on statement 3)",
        ),
        (
            date(2025, 7, 1),
            "3",
            "1 blocker; first at account 1, Securities Held in Account: statement 2 page 1 "
            "row 1; column Qty",
        ),
    ],
    ids=["missing-from-statement-3", "printed-mismatch"],
)
def test_position_blocker_is_located_at_its_trade_or_its_printed_row(
    sale_day: date, june: str, located: str
) -> None:
    events = (_synth_trade(_BTO, 2, date(2025, 5, 31)), _synth_trade(_STC, 3, sale_day))
    statements = (
        _statement(_MAY),
        _statement(_JUNE, _synth_position(1, quantity=june)),
        _statement(_JULY),
    )

    result = _compute(_paired(events, statements=statements), _CG)

    kind = RobinhoodOptionBlockerKind.POSITION_RECONCILIATION
    _assert_located(result.blockers, kind, located)


def test_fractional_put_is_one_blocker_naming_both_columns() -> None:
    put = _synth_trade(_BTO, 2, _MAY[0], quantity="2.5", right="Put")

    result = _compute(_paired((put,), statements=()), None)

    kind = RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT
    _assert_located(
        result.blockers,
        kind,
        "1 blocker; first at account 1, activity report row 2; columns Description and Quantity",
    )
