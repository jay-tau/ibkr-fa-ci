"""#1507: an unidentified Robinhood dividend's withholding and statement
Dividends line print no "complete by hand" line the securities entry clears.

A dividend whose symbol resolves to no security gets an identity blocker and
no payment. Pairing a withholding with it needs only the account, symbol,
Activity Date and the description's record date, payment date, shares and
rate. A statement's Dividends totals need only the account, Activity Date and
amount. Neither needs the security, so the first run pairs the withholding and
counts the dividend in the totals before identity resolution. The parity cases
below use a US issuer and no annual form: their first runs raise the re-run's
blockers plus identity. The runs reuse #1487's harness (real income compute
behind the existing pipeline stubs) with synthetic inputs only.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import ibkr_fa.pipeline_robinhood_core as core
from ibkr_fa.compute.robinhood_income import (
    RobinhoodIncomeBlocker,
    RobinhoodIncomeBlockerCause,
    RobinhoodIncomeBlockerKind,
    RobinhoodIncomeEventReference,
)
from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.pipeline_robinhood_core import RobinhoodPipelineError
from tests._no_echo import assert_no_markers
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_income_blockers import INCOME_BLOCKER_LINES as _LINES
from tests._robinhood_income_blockers import IncomeCase as _Case
from tests._robinhood_income_blockers import compute_income_case as _compute
from tests._robinhood_income_blockers import income_case as _case
from tests._robinhood_income_blockers import run_income_command as _run
from tests._robinhood_income_blockers import unconfigured_security_entry as _security_entry
from tests._robinhood_income_inputs import (
    INCOME_ACCOUNT,
    INCOME_CUSIP,
    INCOME_OTHER_ACCOUNT,
    INCOME_PAYMENT_DATE,
    INCOME_SYMBOL,
    income_base_events,
    income_config,
    income_event,
    income_packet,
)
from tests._robinhood_pipeline_stubs import stub_income_bundle as _stub_income_bundle
from tests._robinhood_refusals import OS_UNRESOLVED as _OS_REFUSAL

_Cause = RobinhoodIncomeBlockerCause
_Kind = RobinhoodIncomeBlockerKind
_K = RobinhoodEventKind
_event = income_event
_UNCONFIGURED = replace(income_config(), securities=())
_IDENTITY_LINE = _LINES[_Cause.UNCONFIGURED_SECURITY]
_MISSING_FORM_LINE = _LINES[_Cause.ANNUAL_FORM_MISSING]
_BY_HAND = (
    _LINES[_Cause.WITHHOLDING_WITHOUT_DIVIDEND],
    _LINES[_Cause.STATEMENT_DIVIDENDS],
)

_BUY, _DIVIDEND, _NRA = income_base_events()
# A foreign-tax row on the dividend's Activity Date, in place of the NRA row.
_FOREIGN_TAX = _event(_K.FOREIGN_TAX_WITHHOLDING, 3, INCOME_PAYMENT_DATE, "-25.00")
_WITHHOLDINGS = {"nra": _NRA, "foreign-tax": _FOREIGN_TAX}
_DIVIDEND_REFERENCE = RobinhoodIncomeEventReference(0, _DIVIDEND.source_record_index)
_TAX_REFERENCE = RobinhoodIncomeEventReference(0, _NRA.source_record_index)
_ONE = (Decimal("1.00"), Decimal("1.00"))
_FEB_2026 = date(2026, 2, 10)


def _on(day: date, event: RobinhoodEvent) -> RobinhoodEvent:
    """``event`` with its Activity Date (and the dates that may not precede
    it) moved to ``day``."""
    return replace(event, activity_date=day, process_date=day, settle_date=day)


def _scope(
    blockers: tuple[RobinhoodIncomeBlocker, ...],
) -> set[tuple[object, ...]]:
    return {
        (blocker.kind, blocker.causes, blocker.account_indices, blocker.tax_years)
        for blocker in blockers
    }


def _missing(year: int = 2025) -> tuple[object, ...]:
    return (_Kind.ANNUAL_FORM_COVERAGE, (_Cause.ANNUAL_FORM_MISSING,), (0,), (year,))


def _pairing(cause: RobinhoodIncomeBlockerCause) -> tuple[object, ...]:
    return (_Kind.PAYMENT_PAIRING, (cause,), (0,), (2025,))


def _statement(cause: RobinhoodIncomeBlockerCause, year: int = 2025) -> tuple[object, ...]:
    return (_Kind.STATEMENT_RECONCILIATION, (cause,), (0,), (year,))


# Each shape's events and statement changes, with no Form 1042-S, and the
# blockers other than identity that the identified run raises.
_SHAPES: dict[str, tuple[dict[str, Any], set[tuple[object, ...]]]] = {
    "nra": ({"events": (_BUY, _DIVIDEND, _NRA)}, {_missing()}),
    "foreign-tax": ({"events": (_BUY, _DIVIDEND, _FOREIGN_TAX)}, {_missing()}),
    "nra-on-another-rate": (
        {"events": (_BUY, _DIVIDEND, replace(_NRA, description_rate_usd=Decimal("11")))},
        {_missing(), _pairing(_Cause.WITHHOLDING_WITHOUT_DIVIDEND)},
    ),
    "foreign-tax-on-another-day": (
        {"events": (_BUY, _DIVIDEND, _on(date(2025, 6, 16), _FOREIGN_TAX))},
        {_missing(), _pairing(_Cause.WITHHOLDING_WITHOUT_DIVIDEND)},
    ),
    "withholding-on-two-dividends": (
        {"events": (_BUY, _DIVIDEND, _NRA, replace(_DIVIDEND, source_record_index=4))},
        {_missing(), _pairing(_Cause.WITHHOLDING_SEVERAL_DIVIDENDS)},
    ),
    "dividend-with-two-withholdings": (
        {"events": (_BUY, _DIVIDEND, _NRA, replace(_FOREIGN_TAX, source_record_index=4))},
        {_missing(), _pairing(_Cause.DIVIDEND_SEVERAL_WITHHOLDINGS)},
    ),
    "statement-dividends-disagree": (
        {
            "events": (_BUY, _DIVIDEND, _NRA),
            "statement_deltas": {(INCOME_ACCOUNT, date(2025, 1, 1), "Dividends"): _ONE},
        },
        {_missing(), _statement(_Cause.STATEMENT_DIVIDENDS)},
    ),
    # A dividend in the FY's last calendar year reads that whole year's
    # statements, the one after the FY end included.
    "statement-after-the-fy-end": (
        {
            "events": (_BUY, _on(_FEB_2026, _DIVIDEND), _on(_FEB_2026, _NRA)),
            "extra_period": (date(2026, 4, 1), date(2026, 6, 30)),
            "statement_deltas": {(INCOME_ACCOUNT, date(2026, 4, 1), "Interest Earned"): _ONE},
        },
        {_statement(_Cause.STATEMENT_INTEREST, 2026)},
    ),
}


def _shape(name: str, config: RobinhoodConfig) -> _Case:
    shape, _expected = _SHAPES[name]
    return _case(packets=(), config=config, **shape)


@pytest.mark.parametrize("name", _SHAPES)
def test_an_unidentified_dividend_raises_what_the_identified_one_raises(name: str) -> None:
    """In these US-issuer, no-form cases, the same events raise the same
    non-identity blockers, accounts, years and event references. On the base,
    these cases have an extra pairing or statement blocker, or omit the
    post-FY statement blocker."""
    identified = _compute(_shape(name, income_config()))
    unidentified = _compute(_shape(name, _UNCONFIGURED))

    assert _scope(identified.blockers) == _SHAPES[name][1]
    assert (
        tuple(item for item in unidentified.blockers if item.kind is not _Kind.IDENTITY)
        == identified.blockers
    )
    assert {item.causes for item in unidentified.blockers if item.kind is _Kind.IDENTITY} == {
        (_Cause.UNCONFIGURED_SECURITY,)
    }


@pytest.mark.parametrize("withholding", _WITHHOLDINGS)
def test_the_withholding_rides_on_its_dividends_identity_blocker(withholding: str) -> None:
    """The withholding gets the identity blocker its dividend has, for its
    own event: the coverage check finds every income event in a blocker. On
    main a `withholding-without-dividend` blocker carried it."""
    case = _case((_BUY, _DIVIDEND, _WITHHOLDINGS[withholding]), packets=(), config=_UNCONFIGURED)
    income = _compute(case)

    identity = RobinhoodIncomeBlocker(
        kind=_Kind.IDENTITY,
        account_indices=(0,),
        tax_years=(2025,),
        causes=(_Cause.UNCONFIGURED_SECURITY,),
    )
    assert income.blockers == (
        RobinhoodIncomeBlocker(
            kind=_Kind.ANNUAL_FORM_COVERAGE,
            account_indices=(0,),
            tax_years=(2025,),
            causes=(_Cause.ANNUAL_FORM_MISSING,),
        ),
        replace(identity, event_references=(_DIVIDEND_REFERENCE,)),
        replace(identity, event_references=(_TAX_REFERENCE,)),
    )
    assert income.dividend_payments == income.withholding_payments == ()

    bundle = cast("Any", SimpleNamespace(paired=case[1]))
    core.validate_owned_events(bundle, core.ComputedRobinhood(income=income))
    uncovered = replace(income, blockers=income.blockers[:-1])
    with pytest.raises(RobinhoodPipelineError, match="income layer"):
        core.validate_owned_events(bundle, core.ComputedRobinhood(income=uncovered))


@pytest.mark.parametrize("command", ["robinhood-income", "robinhood-all"])
@pytest.mark.parametrize("withholding", _WITHHOLDINGS)
def test_the_first_run_prints_only_the_lines_a_remedy_clears(
    withholding: str,
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The identity and missing-form lines, once each and where #1504 prints
    them; no line that directs only completion by hand."""
    case = _case((_BUY, _DIVIDEND, _WITHHOLDINGS[withholding]), packets=(), config=_UNCONFIGURED)

    code, out, err, _loaded = _run(command, case, tmp_path, monkeypatch, capsys)
    # robinhood-income's Schedule OS refusal prints on stderr; robinhood-all's
    # report, its leg refusals included, on stdout.
    output, other = (err, out) if command == "robinhood-income" else (out, err)

    assert code == 4
    assert other == ""
    assert output.count(_IDENTITY_LINE) == output.count(_MISSING_FORM_LINE) == 1, output
    for line in _BY_HAND:
        assert line not in output
    assert "; complete " not in output, output
    if command == "robinhood-income":
        assert output.startswith(f"error: {_OS_REFUSAL}\n{_IDENTITY_LINE}\n")
        assert output.endswith(f"\n{_MISSING_FORM_LINE}\n")
    else:
        assert (
            "  error        income-ftc\n"
            "                 -> unresolved income blockers\n"
            f"{_MISSING_FORM_LINE}\n"
        ) in output
    assert_no_markers(out + err, INCOME_ACCOUNT, INCOME_OTHER_ACCOUNT, "100.00", "25.00")


@pytest.mark.parametrize("identified", [False, True], ids=["unidentified", "identified"])
@pytest.mark.parametrize("command", ["robinhood-income", "robinhood-all"])
@pytest.mark.parametrize(
    ("name", "cause"),
    [
        ("nra-on-another-rate", _Cause.WITHHOLDING_WITHOUT_DIVIDEND),
        ("foreign-tax-on-another-day", _Cause.WITHHOLDING_WITHOUT_DIVIDEND),
        ("statement-dividends-disagree", _Cause.STATEMENT_DIVIDENDS),
    ],
)
def test_an_unmatched_withholding_or_disagreeing_statement_still_prints_its_line(
    name: str,
    cause: RobinhoodIncomeBlockerCause,
    command: str,
    identified: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A withholding that matches no dividend in the activity report, or a
    Dividends line that disagrees with it, the dividend counted: its line,
    once, with the dividend unidentified or identified."""
    config = income_config() if identified else _UNCONFIGURED

    code, out, err, _loaded = _run(command, _shape(name, config), tmp_path, monkeypatch, capsys)
    # robinhood-income's Schedule OS refusal prints on stderr; robinhood-all's
    # report, its leg refusals included, on stdout.
    output, other = (err, out) if command == "robinhood-income" else (out, err)

    assert code == 4
    assert other == ""
    assert output.count(_LINES[cause]) == 1, output
    assert output.count(_IDENTITY_LINE) == (0 if identified else 1)


@pytest.mark.parametrize("withholding", _WITHHOLDINGS)
def test_following_every_printed_remedy_clears_the_run_in_one_rerun(
    withholding: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The first run names the securities entry and the Form 1042-S. The
    entry, read by the real config loader, and the form, added together: the
    next run raises no income blocker, and ``robinhood-income`` writes
    Schedule OS, FSI and TR. With this shared config, which supplies no Indian
    tax payable, it then stops at Form 67's Indian-tax-payable action (exit 3);
    the issue leaves that action out of scope."""
    config, paired, packets = _case(
        (_BUY, _DIVIDEND, _WITHHOLDINGS[withholding]), packets=(), config=_UNCONFIGURED
    )
    causes = {
        cause
        for blocker in _compute((config, paired, packets)).blockers
        for cause in blocker.causes
    }
    assert causes == {_Cause.ANNUAL_FORM_MISSING, _Cause.UNCONFIGURED_SECURITY}
    assert packets == ()

    form = income_packet()
    followed = replace(
        config,
        securities=(_security_entry(tmp_path, INCOME_SYMBOL, INCOME_CUSIP),),
    )
    income = _compute((followed, paired, (form,)))
    assert income.blockers == ()
    assert [item.event_reference for item in income.withholding_payments] == [_TAX_REFERENCE]

    _stub_income_bundle(monkeypatch, tmp_path, followed, paired, (form,))

    code = _main("robinhood-income", tmp_path)

    captured = capsys.readouterr()
    output = captured.out
    assert code == 3, captured
    assert captured.err == ""
    assert output.startswith("Robinhood income-ftc: manual\n")
    (action,) = (line for line in output.splitlines() if line.startswith("  Action: "))
    assert action.startswith(
        "  Action: one or more Form 67 entries lack Indian tax payable and credit"
    )
    assert _OS_REFUSAL not in output
    assert not any(line in output for line in _LINES.values())
    assert {path.name for path in (tmp_path / "out").iterdir()} >= {
        "schedule-os.xlsx",
        "schedule-fsi.csv",
        "schedule-tr.csv",
    }
