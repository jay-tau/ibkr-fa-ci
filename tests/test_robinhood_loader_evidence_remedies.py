"""#1498: the Robinhood loader's refusals for short, late-starting or
misaligned evidence name the account, the period the run needs and the
remedy, and following that remedy clears them.

Each run is a real Robinhood command on a synthetic two-account bundle read
by the real loader, reconciliation and coverage check; only the PDF and CSV
readers are stubbed, to return parsed statements and activity reports. Each
account holds one cash deposit and has a CY 2025 statement, a January to
February 2026 statement and a March 2026 one, unless a case stops its
statements in February. With complete evidence every command exits 0.

Two refusals the issue lists can't be reached, so their text is unchanged:
"activity report starts after its reconciled account opening date" (the
reconciliation's own opening-date check refuses the same report first, as
the late-start case shows) and "issued brokerage statements end before the
requested CY/FY union" (a reconciled account's statements always end on its
report's end date, so the report check refuses first).

#1503: a report ending before its account's first statement can receive the
remedy when the assignment and period rules allow it. The mixed managed-trade
case gets none. Remedies for runs that read Forms 1042-S also name 31 December
of the FY's second calendar year for any account whose form is for that year,
as robinhood-income needs. The form cases add 2026 interest to the first
account and its 2026 form, and exercise statements through December 2026.

#1508: an assignment whose report holds an event before its account's
opening date is set aside like a managed-trade mismatch, so the other one
can carry the remedy. An account opening in the FY's second calendar year
gets the remedy unless the command builds Schedule FA, whose compute refuses
it. The form-year case for 2025, the one tax year the real Form 1042-S
parser reads, moves the harness a year earlier to FY 2024-25.

#1516: a named report holding no event by the FY's end keeps the remedy
when its account's statements show activity from the opening date to then,
which the report the remedy asks for holds.
"""

from __future__ import annotations

import re
from calendar import monthrange
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest

import ibkr_fa.config as config_module
import ibkr_fa.parser.robinhood_1042s as tax_form_parser
import ibkr_fa.parser.robinhood_statement.parser as statement_parser
import ibkr_fa.pipeline_robinhood_bundle as bundle_mod
import ibkr_fa.pipeline_robinhood_core as core
from ibkr_fa import cli
from ibkr_fa.config import load_user_config
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.normalize.robinhood.coverage import evidence_period_remedy
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import RobinhoodStatement, RobinhoodStatementFamily
from tests._no_echo import assert_no_markers
from tests._robinhood_1042s import robinhood_1042s_text as _packet_text
from tests._robinhood_account_inputs import managed_buy_event as _managed_event
from tests._robinhood_cli import robinhood_argv
from tests._robinhood_fa_inputs import FA_OPENING as _OPENING
from tests._robinhood_fa_inputs import fa_cash as _cash
from tests._robinhood_fa_inputs import fa_statement as _statement
from tests._robinhood_income_inputs import income_event, income_packet
from tests._robinhood_toml import robinhood_account_toml, robinhood_toml

_INDIVIDUAL = "111111111"
_MANAGED = "222222222"
_FAMILIES = {
    _INDIVIDUAL: RobinhoodStatementFamily.INDIVIDUAL,
    _MANAGED: RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
}
_LABELS = {"Individual": _INDIVIDUAL, "Managed Individual": _MANAGED}
_MANAGED_DAY = date(2025, 1, 12)
_DEPOSITS: dict[str, RobinhoodEvent] = {
    _INDIVIDUAL: _cash(),
    _MANAGED: replace(
        _cash(),
        amount_usd=Decimal(7),
        activity_date=_MANAGED_DAY,
        process_date=_MANAGED_DAY,
        settle_date=_MANAGED_DAY,
    ),
}
# An account's configured opening date when a case moves it from _OPENING.
_OPENINGS: dict[str, date] = {}
_FEBRUARY_END = date(2026, 2, 28)
_FY_END = date(2026, 3, 31)
_FORM_YEAR_END = date(2026, 12, 31)
# The first account's 2026 interest, which its 2026 Form 1042-S reports.
_INTEREST = income_event(RobinhoodEventKind.CASH_INTEREST, 3, date(2026, 2, 10), "7.00")
_FORM_2026 = income_packet(
    account_reference=_INDIVIDUAL,
    tax_year=2026,
    income_code="01",
    gross="7.00",
    tax="0.00",
    rate="0.00",
)
_PAYER = (
    '\n[[robinhood.interest_payer_countries]]\naccount_number = "111111111"\n'
    'financial_year = 2025\ncountry_iso2 = "US"\ninterest_kinds = ["brokerage"]\n'
    'source = "synthetic payer review"\nreason = "synthetic US payer"\n'
)
_COMMANDS = (
    "robinhood-all",
    "robinhood-income",
    "robinhood-cg",
    "robinhood-fa",
    "robinhood-options",
)


@dataclass(frozen=True)
class _Evidence:
    """One account's ``[[robinhood.reports]]`` dates and its last statement's
    end; with ``managed_trade``, its report also holds a managed trade."""

    start: date = _OPENING
    end: date = _FY_END
    statements_end: date = _FY_END
    managed_trade: bool = False


class _AfterFormYearMeta(type):
    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, date)


class _AfterFormYear(date, metaclass=_AfterFormYearMeta):
    """``date`` as the config loader sees it on 15 April 2027, once a 2026
    Form 1042-S exists, so a report ending 31 December 2026 isn't in the future."""

    @classmethod
    def today(cls) -> Self:
        return cls(2027, 4, 15)


_REMEDY = evidence_period_remedy(FinancialYear(2025), tax_forms=False)
_UNASSIGNED = "Robinhood reconciliation refused: no activity report assignment reconciles"
_SHORT = _Evidence(end=_FEBRUARY_END, statements_end=_FEBRUARY_END)
# Each case's evidence (an account not listed has complete evidence) and refusal.
_CASES = {
    "both-end-early": (
        {_INDIVIDUAL: _SHORT, _MANAGED: _SHORT},
        "activity report ends before 31 March 2026 (Individual and Managed Individual "
        f"accounts). {_REMEDY}",
    ),
    "one-ends-early": (
        {_MANAGED: _SHORT},
        f"activity report ends before 31 March 2026 (Managed Individual account). {_REMEDY}",
    ),
    "report-starts-late": (
        {_INDIVIDUAL: _Evidence(start=date(2025, 1, 6))},
        f"{_UNASSIGNED} (report 0/bundle 0: activity report starts after the Individual "
        "account's opening date; report 0/bundle 1: activity report starts after the Managed "
        f"Individual account's opening date). {_REMEDY}",
    ),
    "report-ends-mid-statement": (
        {_INDIVIDUAL: _Evidence(end=date(2026, 3, 15))},
        f"{_UNASSIGNED} (report 0/bundle 0: activity report ends partway through one of the "
        "Individual account's statement periods; report 0/bundle 1: activity report ends "
        "partway through one of the Managed Individual account's statement periods). "
        f"{_REMEDY}",
    ),
    "statements-end-early": (
        {_INDIVIDUAL: _Evidence(statements_end=_FEBRUARY_END)},
        f"{_UNASSIGNED} (report 0/bundle 0: activity report ends after the Individual "
        "account's last statement; report 1/bundle 0: activity report ends after the "
        f"Individual account's last statement). {_REMEDY}",
    ),
    # #1503: the report ends before the first statement (CY 2025) does.
    "report-ends-inside-first-statement": (
        {_INDIVIDUAL: _Evidence(end=date(2025, 1, 15))},
        f"{_UNASSIGNED} (report 0/bundle 0: activity report range contains no complete "
        "statement period of the Individual account; report 0/bundle 1: activity report range "
        f"contains no complete statement period of the Managed Individual account). {_REMEDY}",
    ),
}
_READS_FORMS = frozenset({"robinhood-all", "robinhood-income", "robinhood-fa"})
# The commands that build Schedule FA, whose compute refuses an account
# opened after its calendar year (#1508).
_BUILDS_SCHEDULE_FA = frozenset({"robinhood-all", "robinhood-fa"})
_OPENS_AFTER_FA_YEAR = "Robinhood account opens after the Schedule FA year"
_REMEDY_WITH_FORMS = evidence_period_remedy(FinancialYear(2025), tax_forms=True)
# Each case's evidence and refusal, before its remedy, in runs whose input has
# the first account's 2026 Form 1042-S.
_FORM_CASES = {
    "both-end-early": (
        {_INDIVIDUAL: _SHORT, _MANAGED: _SHORT},
        "activity report ends before 31 March 2026 (Individual and Managed Individual accounts). ",
    ),
    "only-account-without-form-ends-early": (
        {_MANAGED: _SHORT},
        "activity report ends before 31 March 2026 (Managed Individual account). ",
    ),
    "report-ends-mid-september": (
        {_INDIVIDUAL: _Evidence(end=date(2026, 9, 15), statements_end=_FORM_YEAR_END)},
        f"{_UNASSIGNED} (report 0/bundle 0: activity report ends partway through one of the "
        "Individual account's statement periods; report 0/bundle 1: activity report ends after "
        "the Managed Individual account's last statement). ",
    ),
}


def _statements(
    number: str, statements_end: date, *, interest: bool
) -> dict[str, RobinhoodStatement]:
    """``number``'s statements by file name: CY 2025 with its deposit, then
    January to February 2026 (with ``_INTEREST`` when ``interest``), March
    2026 and each later month of 2026 through ``statements_end``; all a year
    earlier under ``_a_year_earlier``."""
    family = _FAMILIES[number]
    cash = _DEPOSITS[number].amount_usd
    assert cash is not None
    second = _FORM_YEAR_END.year
    year = _statement(
        number,
        family,
        (_DEPOSITS[number],),
        cash=cash,
        position_quantity=Decimal(),
        start=date(second - 1, 1, 1),
        end=date(second - 1, 12, 31),
    )
    periods = [(date(second, 1, 1), _FEBRUARY_END)] + [
        (date(second, month, 1), date(second, month, monthrange(second, month)[1]))
        for month in range(3, 13)
    ]
    earned = _INTEREST.amount_usd if interest else Decimal()
    assert earned is not None
    following = {}
    for start, end in periods:
        if end > statements_end:
            continue
        first = start == date(second, 1, 1)
        statement = _statement(
            number,
            family,
            (_INTEREST,) if interest and first else (),
            cash=cash + earned,
            position_quantity=Decimal(),
            start=start,
            end=end,
            opening_cash=cash if first else cash + earned,
            opening_securities=Decimal(),
        )
        income = tuple(
            replace(row, this_period_usd=earned if first else Decimal(), year_to_date_usd=earned)
            if row.label == "Interest Earned"
            else row
            for row in statement.income
        )
        following[f"{family.value}-{end:%Y-%m}.pdf"] = replace(statement, income=income)
    return {f"{family.value}-{second - 1}.pdf": year, **following}


def _from_opening(opening: date, first_end: date) -> Callable[..., dict[str, RobinhoodStatement]]:
    """A ``_statements`` for accounts opening on ``opening``: a first statement
    from the start of its month to ``first_end`` with the account's deposit,
    then one per month through the ``statements_end`` asked for."""

    def statements(
        number: str, statements_end: date, *, interest: bool
    ) -> dict[str, RobinhoodStatement]:
        assert not interest
        deposit = _DEPOSITS[number]
        assert deposit.amount_usd is not None
        periods = [(opening.replace(day=1), first_end)]
        while periods[-1][1] < statements_end:
            start = periods[-1][1] + timedelta(days=1)
            periods.append((start, start.replace(day=monthrange(start.year, start.month)[1])))
        return {
            f"{_FAMILIES[number].value}-{end:%Y-%m}.pdf": _statement(
                number,
                _FAMILIES[number],
                () if index else (deposit,),
                cash=deposit.amount_usd,
                position_quantity=Decimal(),
                start=start,
                end=end,
                **(
                    {"opening_cash": deposit.amount_usd, "opening_securities": Decimal()}
                    if index
                    else {}
                ),
            )
            for index, (start, end) in enumerate(periods)
        }

    return statements


def _open_on(monkeypatch: pytest.MonkeyPatch, opening: date, first_end: date) -> None:
    """Open both accounts on ``opening``, with ``_from_opening``'s statements
    and each deposit on its day of ``opening``'s month."""
    monkeypatch.setattr(f"{__name__}._OPENING", opening)
    for number, event in _DEPOSITS.items():
        day = opening.replace(day=event.activity_date.day)
        monkeypatch.setitem(
            _DEPOSITS, number, replace(event, activity_date=day, process_date=day, settle_date=day)
        )
    monkeypatch.setattr(f"{__name__}._statements", _from_opening(opening, first_end))


def _earlier(event: RobinhoodEvent) -> RobinhoodEvent:
    day = event.activity_date.replace(year=event.activity_date.year - 1)
    return replace(event, activity_date=day, process_date=day, settle_date=day)


def _a_year_earlier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Move every harness date, event and the interest payer entry a year
    earlier, for FY 2024-25."""
    for name in ("_OPENING", "_FEBRUARY_END", "_FY_END", "_FORM_YEAR_END"):
        day = globals()[name]
        monkeypatch.setattr(f"{__name__}.{name}", day.replace(year=day.year - 1))
    for number, event in _DEPOSITS.items():
        monkeypatch.setitem(_DEPOSITS, number, _earlier(event))
    monkeypatch.setattr(f"{__name__}._INTEREST", _earlier(_INTEREST))
    monkeypatch.setattr(
        f"{__name__}._PAYER", _PAYER.replace("financial_year = 2025", "financial_year = 2024")
    )


def _report_entry(number: str, evidence: _Evidence, keys: tuple[str, str]) -> str:
    """``number``'s report entry, its dates under the ``keys`` a remedy names."""
    start_key, end_key = keys
    return (
        f'\n[[robinhood.reports]]\nfile = "activity/{_FAMILIES[number].value}.csv"\n'
        f"{start_key} = {evidence.start.isoformat()}\n{end_key} = {evidence.end.isoformat()}\n"
        'source = "synthetic report export"\n'
    )


def _write_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence: dict[str, _Evidence],
    keys: tuple[str, str] = ("start_date", "end_date"),
    *,
    forms: bool = False,
) -> None:
    """Write the bundle and config for ``evidence`` (complete for an account
    not listed), replacing any earlier ones, and point the readers at it.

    With ``forms``, the first account also earns ``_INTEREST``, the input has
    a Form 1042-S file and the config the account's interest payer; the case
    supplies what the form reader returns.
    """
    bundle = tmp_path / "bundle"
    statements: dict[str, RobinhoodStatement] = {}
    reports: dict[str, tuple[RobinhoodEvent, ...]] = {}
    entries = []
    for number in (_INDIVIDUAL, _MANAGED):
        account = evidence.get(number, _Evidence(_OPENING, _FY_END, _FY_END))
        interest = forms and number == _INDIVIDUAL
        statements |= _statements(number, account.statements_end, interest=interest)
        reports[f"{_FAMILIES[number].value}.csv"] = (
            _DEPOSITS[number],
            *((_INTEREST,) if interest else ()),
            *((_managed_event(),) if account.managed_trade else ()),
        )
        entries.append(_report_entry(number, account, keys))
    if forms:
        (bundle / "tax-forms").mkdir(parents=True, exist_ok=True)
        (bundle / "tax-forms" / "form-1042-s.pdf").write_text("form", encoding="utf-8")
        entries.append(_PAYER)
    for folder, names in (("statements", statements), ("activity", reports)):
        (bundle / folder).mkdir(parents=True, exist_ok=True)
        for path in (bundle / folder).iterdir():
            path.unlink()
        for name in names:
            (bundle / folder / name).write_text(name, encoding="utf-8")
    first, second = (
        robinhood_account_toml(
            number=number,
            opening_date=_OPENINGS.get(number, _OPENING).isoformat(),
            source="synthetic statement",
            institution_name="Synthetic Broker",
            address="1 Example Road",
            zip_code="99999",
        )
        for number in (_INDIVIDUAL, _MANAGED)
    )
    (tmp_path / "user.toml").write_text(
        robinhood_toml(first=first, second=second, extra="".join(entries)), encoding="utf-8"
    )
    monkeypatch.setattr(bundle_mod, "read_robinhood_statement", lambda path: statements[path.name])
    monkeypatch.setattr(bundle_mod, "read_robinhood_activity", lambda path: reports[path.name])
    monkeypatch.setattr(statement_parser, "pdftotext_layout", lambda _path, **_kwargs: "Brokerage")


def _run(command: str, tmp_path: Path, financial_year: str = "2025-26") -> int:
    # robinhood-options fetches no prices, so it has no such flag.
    return cli.main(
        robinhood_argv(
            command,
            tmp_path,
            financial_year=financial_year,
            no_fetch_prices=command != "robinhood-options",
        )
    )


def _followed(
    refusal: str, form_accounts: frozenset[str] = frozenset()
) -> tuple[dict[str, _Evidence], tuple[str, str]]:
    """What following ``refusal``'s remedy word for word supplies: for each
    account it names, and any form account the clause includes, a report
    from the account's opening date to the end it names for that account,
    with statements to that end; and the two keys it says to set."""
    end = re.search(r"opening date to (\d+ March \d{4}), the end of financial year", refusal)
    form_end = re.search(
        r"\((31 December (\d{4})) for any account whose Form 1042-S for \2 is in the input's "
        r"tax-forms folder, even if not named in this refusal\)",
        refusal,
    )
    keys = re.search(r"\[\[robinhood\.reports\]\] (\w+) and (\w+) set to match", refusal)
    assert end is not None
    assert keys is not None
    named = {_LABELS[label] for label in re.findall(r"Managed Individual|Individual", refusal)}
    if form_end is not None:
        named |= form_accounts
    supplied = {}
    for number in named:
        last = form_end if form_end is not None and number in form_accounts else end
        day = datetime.strptime(last.group(1), "%d %B %Y").date()
        supplied[number] = _Evidence(
            start=_OPENINGS.get(number, _OPENING), end=day, statements_end=day
        )
    return supplied, (keys.group(1), keys.group(2))


@pytest.mark.parametrize("command", _COMMANDS)
def test_complete_evidence_passes_every_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """The control: with every report and statement from the opening date to
    31 March 2026, every command loads the bundle and exits 0."""
    _write_bundle(tmp_path, monkeypatch, {})

    assert _run(command, tmp_path) == 0

    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("command", _COMMANDS)
@pytest.mark.parametrize("case", list(_CASES))
def test_short_or_misaligned_evidence_names_the_remedy_and_following_it_clears_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    command: str,
) -> None:
    """Each command refuses with exit 4, naming the accounts by statement
    family (never their numbers), the period FY 2025-26 needs and what to
    supply and set. Supplying that for each account named and setting the
    keys it names, as written, then lets the same command exit 0."""
    evidence, refusal = _CASES[case]
    _write_bundle(tmp_path, monkeypatch, evidence)

    assert _run(command, tmp_path) == 4

    printed = capsys.readouterr()
    assert printed.err == f"error: {refusal}\n"
    assert printed.out == ""
    assert_no_markers(printed.err, _INDIVIDUAL, _MANAGED)

    supplied, keys = _followed(refusal)
    _write_bundle(tmp_path, monkeypatch, evidence | supplied, keys)
    robinhood = load_user_config(tmp_path / "user.toml").robinhood
    assert robinhood is not None
    assert {(report.start_date, report.end_date) for report in robinhood.reports} == {
        (_OPENING, _FY_END)
    }

    assert _run(command, tmp_path) == 0

    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("command", _COMMANDS)
def test_late_reports_that_both_hold_managed_trades_name_no_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """#1503: both reports hold a managed trade and start a day after the
    opening date. Each assignment gives one to the Individual account, which
    no evidence for the run's period clears, so the refusal names no remedy."""
    late = _Evidence(start=date(2025, 1, 6), managed_trade=True)
    _write_bundle(tmp_path, monkeypatch, {_INDIVIDUAL: late, _MANAGED: late})

    assert _run(command, tmp_path) == 4

    printed = capsys.readouterr()
    assert printed.err == (
        f"error: {_UNASSIGNED} (report 0/bundle 0: managed activity is assigned to a "
        "non-managed statement account; report 0/bundle 1: activity report starts after the "
        "Managed Individual account's opening date; report 1/bundle 0: managed activity is "
        "assigned to a non-managed statement account; report 1/bundle 1: activity report "
        "starts after the Managed Individual account's opening date)\n"
    )
    assert printed.out == ""


@pytest.mark.parametrize("command", _COMMANDS)
def test_a_crossed_report_with_an_event_before_its_accounts_opening_keeps_the_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """#1508: the Managed Individual account opens on its deposit's day, 12
    January 2025, and its report ends 15 February 2025, before its first
    statement does. The crossed assignment gives the Individual account's 11
    January deposit to the later account, which no evidence for any period
    clears, so it is set aside; the other assignment fails only the short
    end. Following the remedy as written lets the same command exit 0."""
    monkeypatch.setitem(_OPENINGS, _MANAGED, _MANAGED_DAY)
    evidence = {_MANAGED: _Evidence(start=_MANAGED_DAY, end=date(2025, 2, 15))}
    _write_bundle(tmp_path, monkeypatch, evidence)

    assert _run(command, tmp_path) == 4

    refusal = (
        f"{_UNASSIGNED} (report 0/bundle 1: activity event predates the configured account "
        "opening date; report 1/bundle 0: activity report starts after the Individual "
        "account's opening date; report 1/bundle 1: activity report range contains no "
        f"complete statement period of the Managed Individual account). {_REMEDY}"
    )
    printed = capsys.readouterr()
    assert printed.err == f"error: {refusal}\n"
    assert printed.out == ""

    supplied, keys = _followed(refusal)
    _write_bundle(tmp_path, monkeypatch, evidence | supplied, keys)

    assert _run(command, tmp_path) == 0

    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("command", _COMMANDS)
@pytest.mark.parametrize(
    ("individual_day", "crossed"),
    [
        (
            _DEPOSITS[_INDIVIDUAL].activity_date,
            "report 0/bundle 1: activity event predates the configured account opening date; ",
        ),
        (date(2025, 1, 15), ""),
    ],
    ids=["crossed-assignment-set-aside", "nothing-set-aside"],
)
def test_a_remedy_that_would_empty_a_report_is_withheld(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    individual_day: date,
    crossed: str,
) -> None:
    """#1508: the Managed Individual account opens on 12 January 2025 but
    stays idle until after the FY, its only deposit on 5 April 2026. Following
    the FY-end remedy would empty its report and trip the earlier no-events
    check, so no remedy is named. With the Individual account's deposit on 11
    January the crossed assignment is set aside; with it on 15 January
    nothing is, and main named the remedy."""
    monkeypatch.setitem(
        _DEPOSITS,
        _INDIVIDUAL,
        replace(
            _DEPOSITS[_INDIVIDUAL],
            activity_date=individual_day,
            process_date=individual_day,
            settle_date=individual_day,
        ),
    )
    monkeypatch.setitem(_OPENINGS, _MANAGED, _MANAGED_DAY)
    day = date(2026, 4, 5)
    monkeypatch.setitem(
        _DEPOSITS,
        _MANAGED,
        replace(_DEPOSITS[_MANAGED], activity_date=day, process_date=day, settle_date=day),
    )
    statements = _statements

    def idle_statements(
        number: str, statements_end: date, *, interest: bool
    ) -> dict[str, RobinhoodStatement]:
        originals = statements(number, statements_end, interest=interest)
        if number != _MANAGED:
            return originals
        return {
            name: _statement(
                number,
                _FAMILIES[number],
                (),
                cash=Decimal(),
                position_quantity=Decimal(),
                start=statement.period_start,
                end=statement.period_end,
                opening_cash=None if index == 0 else Decimal(),
                opening_securities=None if index == 0 else Decimal(),
            )
            for index, (name, statement) in enumerate(originals.items())
        }

    monkeypatch.setattr(f"{__name__}._statements", idle_statements)
    evidence = {_MANAGED: _Evidence(start=_MANAGED_DAY, end=date(2026, 4, 30))}
    _write_bundle(tmp_path, monkeypatch, evidence)

    assert _run(command, tmp_path) == 4

    refusal = (
        f"{_UNASSIGNED} ({crossed}report 1/bundle 0: activity report starts after the "
        "Individual account's opening date; report 1/bundle 1: activity report ends after the "
        "Managed Individual account's last statement)"
    )
    printed = capsys.readouterr()
    assert printed.err == f"error: {refusal}\n"
    assert printed.out == ""

    supplied, keys = _followed(f"{refusal}. {_REMEDY}")
    _write_bundle(tmp_path, monkeypatch, evidence | supplied, keys)
    monkeypatch.setattr(
        bundle_mod,
        "read_robinhood_activity",
        lambda path: (_DEPOSITS[_INDIVIDUAL],) if path.stem == "individual" else (),
    )

    assert _run(command, tmp_path) == 4
    assert capsys.readouterr().err == (
        "error: Robinhood reconciliation refused: activity report contains no events\n"
    )


@pytest.mark.parametrize("command", _COMMANDS)
def test_a_report_missing_activity_its_statements_show_keeps_the_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """#1516: both accounts open on 5 January 2025, with monthly statements
    from January 2025 to April 2026. The Individual account's statements show
    its 11 January deposit and another of 3.00 on 5 April 2026, but its report
    covers only April 2026, so it holds no event by 31 March 2026. The Managed
    Individual report runs from the opening date to 31 March 2026. A report
    from the Individual account's opening date to 31 March 2026, as the remedy
    asks, holds the January deposit, so every command names the remedy.
    Following it as written, with each report holding its account's events
    inside its own dates, lets the same command exit 0."""
    april_end = date(2026, 4, 30)
    _open_on(monkeypatch, _OPENING, date(2025, 1, 31))
    day = date(2026, 4, 5)
    april = replace(
        _DEPOSITS[_INDIVIDUAL],
        source_record_index=3,
        activity_date=day,
        process_date=day,
        settle_date=day,
        amount_usd=Decimal(3),
    )
    monthly = _statements

    def statements(
        number: str, statements_end: date, *, interest: bool
    ) -> dict[str, RobinhoodStatement]:
        found = monthly(number, statements_end, interest=interest)
        name = f"{_FAMILIES[number].value}-{day:%Y-%m}.pdf"
        if number != _INDIVIDUAL or name not in found:
            return found
        return found | {
            name: _statement(
                number,
                _FAMILIES[number],
                (april,),
                cash=Decimal(8),
                position_quantity=Decimal(),
                start=day.replace(day=1),
                end=april_end,
                opening_cash=Decimal(5),
                opening_securities=Decimal(),
            )
        }

    monkeypatch.setattr(f"{__name__}._statements", statements)
    activity = {
        _FAMILIES[_INDIVIDUAL].value: (_DEPOSITS[_INDIVIDUAL], april),
        _FAMILIES[_MANAGED].value: (_DEPOSITS[_MANAGED],),
    }

    def write(
        evidence: dict[str, _Evidence], keys: tuple[str, str] = ("start_date", "end_date")
    ) -> None:
        _write_bundle(tmp_path, monkeypatch, evidence, keys)
        dates = {_FAMILIES[number].value: item for number, item in evidence.items()}
        monkeypatch.setattr(
            bundle_mod,
            "read_robinhood_activity",
            lambda path: tuple(
                event
                for event in activity[path.stem]
                if dates[path.stem].start <= event.activity_date <= dates[path.stem].end
            ),
        )

    evidence = {
        _INDIVIDUAL: _Evidence(day.replace(day=1), april_end, april_end),
        _MANAGED: _Evidence(_OPENING, _FY_END, april_end),
    }
    write(evidence)

    assert _run(command, tmp_path) == 4

    refusal = (
        f"{_UNASSIGNED} (report 0/bundle 0: activity report starts after the Individual "
        "account's opening date; report 0/bundle 1: activity report starts after the Managed "
        f"Individual account's opening date). {_REMEDY}"
    )
    printed = capsys.readouterr()
    assert printed.err == f"error: {refusal}\n"
    assert printed.out == ""

    supplied, keys = _followed(refusal)
    write(evidence | supplied, keys)

    assert _run(command, tmp_path) == 0

    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("command", _COMMANDS)
@pytest.mark.parametrize("case", ["empty-report", "first-statement-past-fy", "unnamed-report"])
def test_a_report_that_already_reconciles_is_guarded_only_when_the_remedy_names_its_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    case: str,
) -> None:
    """A crossed period failure names the Managed account even though its
    correct pairing passes. Shortening its report to March would empty it
    or end before its first statement, so neither guard may skip that pair.
    An unnamed report stays as supplied and must not block the remedy."""
    first_statement_past_fy = case == "first-statement-past-fy"
    named = case != "unnamed-report"
    opening = date(2026, 1, 5)
    april_end = date(2026, 4, 30)
    _open_on(monkeypatch, opening, date(2026, 1, 31))
    managed_opening = opening if named else date(2026, 1, 12)
    monkeypatch.setitem(_OPENINGS, _MANAGED, managed_opening)
    day = date(2026, 1 if first_statement_past_fy else 4, 12)
    deposit = replace(
        _DEPOSITS[_MANAGED],
        activity_date=day,
        process_date=day,
        settle_date=day,
        amount_usd=Decimal(20),
    )
    day += timedelta(days=1)
    trade = replace(_managed_event(ordinal=3), activity_date=day, process_date=day, settle_date=day)
    monkeypatch.setitem(_DEPOSITS, _MANAGED, deposit)
    monkeypatch.setattr(f"{__name__}._managed_event", lambda: trade)
    individual_statements = _statements

    def statements(
        number: str, statements_end: date, *, interest: bool
    ) -> dict[str, RobinhoodStatement]:
        if number == _INDIVIDUAL:
            return individual_statements(number, statements_end, interest=interest)
        periods = (
            [(date(2026, 1, 1), april_end)]
            if first_statement_past_fy
            else [
                (date(2026, month, 1), date(2026, month, monthrange(2026, month)[1]))
                for month in range(1, 5)
            ]
        )
        return {
            f"managed-{end:%Y-%m}.pdf": _statement(
                number,
                _FAMILIES[number],
                (deposit, *((trade,) if named else ())) if start <= day <= end else (),
                cash=Decimal(10 if named else 20) if day <= end else Decimal(),
                position_quantity=Decimal(1) if named and day <= end else Decimal(),
                start=start,
                end=end,
                opening_cash=None if index == 0 else Decimal(),
                opening_securities=None if index == 0 else Decimal(),
            )
            for index, (start, end) in enumerate(periods)
        }

    monkeypatch.setattr(f"{__name__}._statements", statements)
    evidence = {
        _INDIVIDUAL: _Evidence(
            opening, date(2026, 1, 15) if first_statement_past_fy else date(2026, 2, 15), _FY_END
        ),
        _MANAGED: _Evidence(managed_opening, april_end, april_end, managed_trade=named),
    }
    _write_bundle(tmp_path, monkeypatch, evidence)

    assert _run(command, tmp_path) == 4
    printed = capsys.readouterr()
    assert (_REMEDY in printed.err) == (not named and command not in _BUILDS_SCHEDULE_FA)
    assert "report 1/bundle 1:" not in printed.err
    assert "Individual account" in printed.err
    assert ("Managed Individual account" in printed.err) == named

    supplied, keys = _followed(f"{printed.err}. {_REMEDY}")
    if first_statement_past_fy:
        supplied[_MANAGED] = replace(supplied[_MANAGED], managed_trade=True)
    _write_bundle(tmp_path, monkeypatch, evidence | supplied, keys)
    if not named:
        if command in _BUILDS_SCHEDULE_FA:
            assert _run(command, tmp_path) == 4
            printed = capsys.readouterr()
            assert _OPENS_AFTER_FA_YEAR in printed.err + printed.out
        else:
            assert _run(command, tmp_path) == 0
            assert capsys.readouterr().err == ""
        return
    if not first_statement_past_fy:
        monkeypatch.setattr(
            bundle_mod,
            "read_robinhood_activity",
            lambda path: (_DEPOSITS[_INDIVIDUAL],) if path.stem == "individual" else (),
        )

    assert _run(command, tmp_path) == 4
    expected = (
        "activity report range contains no complete statement period of the Managed Individual "
        "account"
        if first_statement_past_fy
        else "activity report contains no events"
    )
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize("command", _COMMANDS)
@pytest.mark.parametrize(
    ("statements_end", "short", "refusal"),
    [
        (
            _FY_END,
            {_MANAGED: date(2026, 2, 15)},
            f"{_UNASSIGNED} (report 1/bundle 0: activity report ends partway through one of the "
            "Individual account's statement periods; report 1/bundle 1: activity report ends "
            "partway through one of the Managed Individual account's statement periods)",
        ),
        (
            date(2026, 1, 31),
            {_INDIVIDUAL: date(2026, 1, 15)},
            f"{_UNASSIGNED} (report 0/bundle 0: activity report range contains no complete "
            "statement period of the Individual account; report 0/bundle 1: activity report "
            "range contains no complete statement period of the Managed Individual account)",
        ),
    ],
    ids=["report-ends-mid-february", "report-ends-inside-first-statement"],
)
def test_accounts_opening_in_the_fys_second_calendar_year_get_the_remedy_without_schedule_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    statements_end: date,
    short: dict[str, date],
    refusal: str,
) -> None:
    """#1508: both accounts open on 5 January 2026, after the Schedule FA
    calendar year (2025), with monthly statements from January 2026. In the
    first case they run through March 2026 and the Managed Individual report
    ends 15 February 2026; in the second, only January's exists and the
    Individual report ends 15 January. Commands that don't build Schedule FA
    name the remedy, and following it lets them exit 0. robinhood-fa and
    robinhood-all name none: following it anyway stops on Schedule FA's own
    refusal for an account opened after its year, which no evidence clears."""
    opening = date(2026, 1, 5)
    _open_on(monkeypatch, opening, date(2026, 1, 31))
    evidence = {
        number: _Evidence(opening, short.get(number, statements_end), statements_end)
        for number in _FAMILIES
    }
    _write_bundle(tmp_path, monkeypatch, evidence)

    assert _run(command, tmp_path) == 4

    builds_fa = command in _BUILDS_SCHEDULE_FA
    printed = capsys.readouterr()
    assert printed.err == (f"error: {refusal}\n" if builds_fa else f"error: {refusal}. {_REMEDY}\n")
    assert printed.out == ""

    supplied, keys = _followed(f"{refusal}. {_REMEDY}")
    _write_bundle(tmp_path, monkeypatch, evidence | supplied, keys)

    if builds_fa:
        assert _run(command, tmp_path) == 4
        printed = capsys.readouterr()
        assert _OPENS_AFTER_FA_YEAR in printed.err + printed.out
    else:
        assert _run(command, tmp_path) == 0
        assert capsys.readouterr().err == ""


@pytest.mark.parametrize("command", _COMMANDS)
@pytest.mark.parametrize(
    ("opening", "first_end"),
    [
        (date(2026, 5, 5), date(2026, 5, 31)),
        (date(2025, 1, 5), date(2026, 5, 31)),
    ],
    ids=["after-fy", "first-period-after-fy"],
)
def test_first_statement_outside_the_remedys_period_names_no_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    opening: date,
    first_end: date,
) -> None:
    """The period remedy cannot move an opening into the FY or complete a
    first statement ending after its guaranteed FY endpoint."""
    monkeypatch.setattr(
        config_module, "_dt", SimpleNamespace(date=_AfterFormYear, datetime=datetime)
    )
    _open_on(monkeypatch, opening, first_end)
    evidence = {
        number: _Evidence(start=opening, end=first_end, statements_end=first_end)
        for number in _FAMILIES
    }
    evidence[_INDIVIDUAL] = replace(evidence[_INDIVIDUAL], end=opening.replace(day=15))
    _write_bundle(tmp_path, monkeypatch, evidence)

    assert _run(command, tmp_path) == 4

    printed = capsys.readouterr()
    assert printed.err == (
        f"error: {_UNASSIGNED} (report 0/bundle 0: activity report range contains no complete "
        "statement period of the Individual account; report 0/bundle 1: activity report range "
        "contains no complete statement period of the Managed Individual account)\n"
    )
    assert printed.out == ""


@pytest.mark.parametrize("command", _COMMANDS)
@pytest.mark.parametrize("case", list(_FORM_CASES))
def test_a_run_given_a_2026_form_model_names_that_year_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    command: str,
) -> None:
    """#1503: the form reader returns a parsed model of the Individual
    account's 2026 Form 1042-S (the real parser reads only tax year 2025; see
    the FY 2024-25 test below), and the config loader's clock reads 15 April
    2027. A command that reads forms names 31 December 2026 for such an
    account; one that doesn't names only 31 March 2026. Following the remedy
    as written, with the Individual account's evidence to the end it names
    for that account, lets the same command exit 0. For the commands that
    file its income, evidence to 31 March 2026 alone leaves robinhood-income's
    form-year coverage stop."""
    monkeypatch.setattr(bundle_mod, "read_robinhood_1042s_packet", lambda _path: _FORM_2026)
    monkeypatch.setattr(
        config_module, "_dt", SimpleNamespace(date=_AfterFormYear, datetime=datetime)
    )
    evidence, refusal = _FORM_CASES[case]
    _write_bundle(tmp_path, monkeypatch, evidence, forms=True)

    assert _run(command, tmp_path) == 4

    remedy = _REMEDY_WITH_FORMS if command in _READS_FORMS else _REMEDY
    printed = capsys.readouterr()
    assert printed.err == f"error: {refusal}{remedy}\n"
    assert_no_markers(printed.err, _INDIVIDUAL, _MANAGED)

    if command in {"robinhood-all", "robinhood-income"}:
        to_march, keys = _followed(f"{refusal}{_REMEDY}")
        _write_bundle(tmp_path, monkeypatch, evidence | to_march, keys, forms=True)
        assert _run(command, tmp_path) == 4
        assert (
            "evidence-coverage (Individual account, 2026): the account's activity report or "
            "statements stop before 31 December of the listed year"
        ) in capsys.readouterr().out

    supplied, keys = _followed(f"{refusal}{remedy}", frozenset({_INDIVIDUAL}))
    _write_bundle(tmp_path, monkeypatch, evidence | supplied, keys, forms=True)

    assert _run(command, tmp_path) == 0

    assert capsys.readouterr().err == ""


# The Individual account's 2025 Form 1042-S, as text the real parser reads:
# _INTEREST's 7.00 under income code 01, with no tax withheld.
_FORM_2025_TEXT = _packet_text(
    account=_INDIVIDUAL, income_code="01", gross="7.00", tax="0.00", rate="00"
)
_REMEDY_2024_25 = evidence_period_remedy(FinancialYear(2024), tax_forms=False)
_FORM_YEAR_STOP_2025 = (
    "evidence-coverage (Individual account, 2025): the account's activity report or "
    "statements stop before 31 December of the listed year"
)
_FORM_CASES_2024_25 = {
    "both-end-early": (
        {
            number: _Evidence(date(2024, 1, 5), date(2025, 2, 28), date(2025, 2, 28))
            for number in _FAMILIES
        },
        "activity report ends before 31 March 2025 (Individual and Managed Individual accounts). ",
    ),
    "only-account-without-form-ends-early": (
        {
            _INDIVIDUAL: _Evidence(date(2024, 1, 5), date(2025, 3, 31), date(2025, 3, 31)),
            _MANAGED: _Evidence(date(2024, 1, 5), date(2025, 2, 28), date(2025, 2, 28)),
        },
        "activity report ends before 31 March 2025 (Managed Individual account). ",
    ),
    "report-ends-mid-september": (
        {
            _INDIVIDUAL: _Evidence(date(2024, 1, 5), date(2025, 9, 15), date(2025, 12, 31)),
            _MANAGED: _Evidence(date(2024, 1, 5), date(2025, 3, 31), date(2025, 3, 31)),
        },
        f"{_UNASSIGNED} (report 0/bundle 0: activity report ends partway through one of the "
        "Individual account's statement periods; report 0/bundle 1: activity report ends after "
        "the Managed Individual account's last statement). ",
    ),
}


def _income_stops(tmp_path: Path) -> tuple[str, ...]:
    """robinhood-income's loader and source-income compute for FY 2024-25 on
    the bundle: the income blocker lines they leave."""
    financial_year = FinancialYear(2024)
    bundle = core.load_bundle(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        financial_year=financial_year,
        include_tax_forms=True,
        schedule_fa=False,
        fetch_identities=False,
    )
    income = core.compute_source_income(bundle, financial_year)
    labels = bundle.paired.account_labels
    return (
        *core.income_blocker_reasons(income, labels, schedule_os=True),
        *core.income_blocker_reasons(income, labels, schedule_os=False),
    )


@pytest.mark.parametrize("case", list(_FORM_CASES_2024_25))
def test_a_2025_form_read_by_the_real_parser_names_its_year_end_and_the_remedy_clears_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """#1508: FY 2024-25, whose second calendar year, 2025, is the tax year
    the Form 1042-S parser reads. Every harness date moves a year earlier,
    and the real reader parses the Individual account's 2025 form from
    synthetic packet text. robinhood-income names 31 December 2025 for that
    account. With evidence to 31 March 2025 only, its loader accepts the
    bundle and its income compute stops on the account's 2025 form year;
    following the remedy as written clears that stop too. (The command itself
    can't exit 0 for FY 2024-25, as Schedule OS has no FX-citation snapshot
    for AY 2025-26, so the re-runs check the loader and income compute.)"""
    _a_year_earlier(monkeypatch)
    monkeypatch.setattr(
        tax_form_parser, "pdftotext_layout", lambda _path, **_kwargs: _FORM_2025_TEXT
    )
    evidence, refusal = _FORM_CASES_2024_25[case]
    _write_bundle(tmp_path, monkeypatch, evidence, forms=True)

    assert _run("robinhood-income", tmp_path, "2024-25") == 4

    remedy = evidence_period_remedy(FinancialYear(2024), tax_forms=True)
    printed = capsys.readouterr()
    assert printed.err == f"error: {refusal}{remedy}\n"
    assert_no_markers(printed.err, _INDIVIDUAL, _MANAGED)

    to_march, keys = _followed(f"{refusal}{_REMEDY_2024_25}")
    _write_bundle(tmp_path, monkeypatch, evidence | to_march, keys, forms=True)
    stops = _income_stops(tmp_path)
    assert len(stops) == 1
    assert stops[0].startswith(_FORM_YEAR_STOP_2025)

    supplied, keys = _followed(f"{refusal}{remedy}", frozenset({_INDIVIDUAL}))
    _write_bundle(tmp_path, monkeypatch, evidence | supplied, keys, forms=True)

    assert _income_stops(tmp_path) == ()
