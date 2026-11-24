"""#1497: robinhood-income's Schedule OS refusal names the FTC reasons too,
and the unconfigured-security cause and form-disagreement scope use the right evidence.

1. ``robinhood-income`` stops at Schedule OS before its FTC report. Its
   refusal now also gives the payer recipe and FTC-only cause lines, as
   ``robinhood-all`` gives them on its ``income-ftc`` leg, so one run names
   every remedy. This rendering change leaves ``robinhood-all`` unchanged.
2. A payment whose symbol a statement prints for an unconfigured CUSIP gets
   ``UNCONFIGURED_SECURITY``: a ``[[robinhood.securities]]`` entry for that
   CUSIP resolves the statement's symbols as well as the report's.
3. ``ANNUAL_FORMS_DISAGREE`` names the accounts and years of the forms whose
   identities were compared, not the account/years with source income.

The runs reuse #1487's harness (real income compute behind the existing
pipeline stubs); part 2 reads synthetic statement text and activity CSVs
through the real readers and the loader's reconciliation steps.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import ibkr_fa.parser.robinhood_statement.parser as statement_parser
from ibkr_fa.compute.robinhood_identity import observed_robinhood_identity_inputs
from ibkr_fa.compute.robinhood_income import (
    RobinhoodIncomeBlockerCause,
    RobinhoodIncomeBlockerKind,
    RobinhoodIncomeResult,
    compute_robinhood_income,
)
from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.normalize.robinhood import (
    reconcile_robinhood_security_quantities,
    resolve_robinhood_security_identities,
    validate_robinhood_inventory_partition,
)
from ibkr_fa.normalize.robinhood.security import canonical_statement_symbols
from ibkr_fa.parser.robinhood import RobinhoodEventKind
from ibkr_fa.pipeline_robinhood_bundle import load_robinhood_bundle
from tests import _robinhood_exact_bundle as exact
from tests._no_echo import assert_no_markers
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_income_blockers import FILLED_PLACEHOLDERS as _FILLED
from tests._robinhood_income_blockers import INCOME_BLOCKER_LINES as _LINES
from tests._robinhood_income_blockers import OR_FTC_BY_HAND as _OR_FTC
from tests._robinhood_income_blockers import OR_OS_BY_HAND as _OR_OS
from tests._robinhood_income_blockers import compute_income_case as _compute
from tests._robinhood_income_blockers import income_blocker_remedy as _remedy
from tests._robinhood_income_blockers import income_case as _case
from tests._robinhood_income_blockers import loaded_remedy_entry as _loaded
from tests._robinhood_income_blockers import run_income_command as _run
from tests._robinhood_income_blockers import unconfigured_security_entry as _security_entry
from tests._robinhood_income_inputs import (
    INCOME_ACCOUNT,
    INCOME_CUSIP,
    INCOME_FY_END,
    INCOME_FY_START,
    INCOME_OTHER_ACCOUNT,
    INCOME_PAYMENT_DATE,
    INCOME_SYMBOL,
    income_config,
    income_event,
    income_packet,
    income_paired,
)
from tests._robinhood_pipeline_stubs import stub_income_bundle as _stub_income_bundle
from tests._robinhood_refusals import OS_UNRESOLVED as _OS_REFUSAL
from tests._robinhood_remedies import lending_fee_entry
from tests._robinhood_security import configured_securities
from tests._robinhood_toml import robinhood_account_toml, robinhood_report_toml, robinhood_toml

_Cause = RobinhoodIncomeBlockerCause
_Kind = RobinhoodIncomeBlockerKind
_K = RobinhoodEventKind
_FEE_ACCOUNT = "999999999"
_MANAGED = "(Managed Individual account, 2025)"
_BOTH = "(Individual and Managed Individual accounts, 2025)"


def _scoped(cause: RobinhoodIncomeBlockerCause, scope: str) -> str:
    """#1487's line for ``cause``, naming ``scope`` instead of the first account."""
    return _LINES[cause].replace("(Individual account, 2025)", scope, 1)


# ---- part 1: one robinhood-income refusal names every remedy ----

# A Stock Lending payment in the second account, in a security with no
# [[robinhood.securities]] entry and no reviewed source country: an identity
# blocker that stops Schedule OS and a lending-fee one that stops only FTC.
_LENDING = income_event(_K.STOCK_LENDING_INCOME, 2, INCOME_PAYMENT_DATE, "10.00")
_BUY = income_event(_K.EQUITY_BUY, 1, date(2025, 1, 2), "-1")
_IDENTITY_LINE = _scoped(_Cause.UNCONFIGURED_SECURITY, _MANAGED)
_LENDING_LINE = _scoped(_Cause.LENDING_FEE_SOURCE, _MANAGED)


def _both_blocked(config: RobinhoodConfig | None = None) -> Any:
    return (
        config or replace(income_config(other_account=_FEE_ACCOUNT), securities=()),
        income_paired((_BUY,), other_rows=(_LENDING,), other_account=_FEE_ACCOUNT),
        (),
    )


def test_the_case_raises_one_schedule_os_and_one_ftc_only_blocker() -> None:
    income = _compute(_both_blocked())

    assert [(item.causes, item.stops_schedule_os) for item in income.blockers] == [
        ((_Cause.UNCONFIGURED_SECURITY,), True),
        ((_Cause.LENDING_FEE_SOURCE,), False),
    ]
    assert _IDENTITY_LINE.endswith(_OR_OS)
    assert _LENDING_LINE.endswith(_OR_FTC)


def test_robinhood_income_refusal_names_the_ftc_only_remedy_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On main the refusal stopped after the identity line; the filer saw the
    lending-fee remedy only after clearing it and re-running."""
    code, out, err, _loaded_with = _run(
        "robinhood-income", _both_blocked(), tmp_path, monkeypatch, capsys
    )

    assert code == 4
    # The Schedule OS refusal prints on stderr with nothing on stdout.
    assert (out, err) == ("", f"error: {_OS_REFUSAL}\n{_IDENTITY_LINE}\n{_LENDING_LINE}\n")
    assert_no_markers(out + err, _FEE_ACCOUNT, INCOME_ACCOUNT)


def test_robinhood_all_prints_the_same_lines_once_each_as_on_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``robinhood-all`` names the identity line on its ``schedule-os`` leg and
    the lending-fee line on its ``income-ftc`` leg, unchanged from main."""
    code, out, err, _loaded_with = _run(
        "robinhood-all", _both_blocked(), tmp_path, monkeypatch, capsys
    )

    assert code == 4
    # The report, its leg refusals included, prints on stdout.
    assert err == ""
    assert (
        "  error        schedule-os\n"
        f"                 -> {_OS_REFUSAL}\n"
        f"{_IDENTITY_LINE}\n"
        "  error        income-ftc\n"
        "                 -> unresolved income blockers\n"
        f"{_LENDING_LINE}\n"
    ) in out
    assert out.count(_IDENTITY_LINE) == out.count(_LENDING_LINE) == 1


def _lending_fee_entry(tmp_path: Path) -> Any:
    """The entry the lending-fee remedy spells out, placeholders filled in,
    read by the real config loader."""
    text, _keys = lending_fee_entry(_remedy(_Cause.LENDING_FEE_SOURCE), _FILLED)
    folder = tmp_path / "lending-remedy"
    folder.mkdir()
    return _loaded(folder, "stock_lending_fee_fsi_sources", text)


def test_following_both_remedies_clears_the_run_in_one_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both entries the one refusal names, added together, let the next
    ``robinhood-income`` run file Schedule OS and FTC."""
    config, paired, packets = _both_blocked()
    followed = replace(
        config,
        securities=(_security_entry(tmp_path, INCOME_SYMBOL, INCOME_CUSIP),),
        stock_lending_fee_fsi_sources=(_lending_fee_entry(tmp_path),),
    )
    assert _compute((followed, paired, packets)).blockers == ()

    _stub_income_bundle(monkeypatch, tmp_path, followed, paired, packets)

    code = _main("robinhood-income", tmp_path)

    output = capsys.readouterr()
    assert code == 0, output
    assert _OS_REFUSAL not in output.out + output.err
    assert {path.name for path in (tmp_path / "out").iterdir()} >= {
        "schedule-os.xlsx",
        "schedule-fsi.csv",
    }


# ---- part 2: a statement-only symbol of an unconfigured CUSIP ----

# CUSIP exact.CUSIP is bought under symbol _REPORTED; the statement prints
# its dividend under _RENAMED with that CUSIP, while the activity report's
# dividend row, like every dividend row, carries no CUSIP.
_REPORTED = exact.SYMBOL
_RENAMED = "ZQXL"
_CUSIP_LINE = f"CUSIP: {exact.CUSIP}"
_DIVIDEND_TEXT = "Cash Div: R/D 2025-01-15 P/D 2025-01-20 - 2 shares at 0.5"


def _renamed_dividend_sources() -> Any:
    """Synthetic statement text and activity CSV rows for both accounts."""
    statement = exact.statement(
        closing_cash="81.00",
        positions=(exact.Position("2", "$20.00", price="$10.00", symbol=_REPORTED),),
        rows=(
            exact.Row("ACH", "1/6/2025", credit="$100.00", description="ACH Deposit"),
            exact.Row(
                "Buy",
                "1/10/2025",
                quantity="2",
                price="$10.00",
                debit="$20.00",
                symbol=_REPORTED,
                description="Synthetic Equity",
                lines=(_CUSIP_LINE,),
            ),
            exact.Row(
                "CDIV",
                "1/20/2025",
                credit="$1.00",
                symbol=_RENAMED,
                description=_DIVIDEND_TEXT,
                lines=(_CUSIP_LINE,),
            ),
        ),
    ).replace("Dividends $0.00 $0.00", "Dividends $1.00 $1.00")
    report = (
        ("1/6/2025", "1/6/2025", "1/6/2025", "", "ACH Deposit", "ACH", "", "", "$100.00"),
        (
            "1/10/2025",
            "1/10/2025",
            "1/13/2025",
            _REPORTED,
            f"Synthetic Equity\n{_CUSIP_LINE}",
            "Buy",
            "2",
            "$10.00",
            "($20.00)",
        ),
        ("1/20/2025", "1/20/2025", "1/20/2025", _RENAMED, _DIVIDEND_TEXT, "CDIV", "", "", "$1.00"),
    )
    return (statement, report), exact.MANAGED_ACCOUNT


def _renamed_income(securities: tuple[Any, ...]) -> RobinhoodIncomeResult:
    config = RobinhoodConfig(
        accounts=(exact.account(exact.INDIVIDUAL), exact.account(exact.MANAGED)),
        securities=securities,
    )
    return compute_robinhood_income(
        exact.pair(*_renamed_dividend_sources()),
        (),
        config=config,
        securities=configured_securities(config),
        financial_year_start=INCOME_FY_START,
        financial_year_end=INCOME_FY_END,
    )


def _identity_causes(
    income: RobinhoodIncomeResult,
) -> list[tuple[RobinhoodIncomeBlockerCause, ...]]:
    return [
        blocker.causes
        for blocker in income.blockers
        if blocker.kind is RobinhoodIncomeBlockerKind.IDENTITY
    ]


def test_the_loader_accepts_a_dividend_symbol_only_a_statement_ties_to_its_cusip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real loader accepts complete CY/FY coverage of this shape. Only
    PDF extraction is replaced; the readers and all loader checks run."""
    (tmp_path / "activity").mkdir()
    (tmp_path / "statements").mkdir()
    accounts = []
    reports = []
    for index, (statement, rows) in enumerate(_renamed_dividend_sources()):
        (tmp_path / "activity" / f"{index}.csv").write_text(exact.activity_csv(rows))
        (tmp_path / "statements" / f"{index}-2025-01.pdf").write_text(statement)
        number = exact.INDIVIDUAL if index == 0 else exact.MANAGED
        accounts.append(
            robinhood_account_toml(
                number=number,
                opening_date="2025-01-05",
                source="synthetic statement",
                address="1 Test Road",
                zip_code="00000",
            )
        )
        reports.append(robinhood_report_toml(file=f"activity/{index}.csv"))
        for offset in range(1, 15):
            year, month = 2025 + offset // 12, 1 + offset % 12
            continuation = exact.statement(
                account=number,
                managed=index == 1,
                opening=("81", "20", "101") if index == 0 else ("10", "0", "10"),
                closing_cash="81" if index == 0 else "10",
                positions=(exact.Position("2", "$20.00", price="$10.00"),) if index == 0 else (),
            ).replace(
                "1/1/2025 to 1/31/2025",
                f"{month}/1/{year} to {month}/{monthrange(year, month)[1]}/{year}",
            )
            if index == 0 and year == 2025:
                continuation = continuation.replace(
                    "Dividends $0.00 $0.00", "Dividends $0.00 $1.00"
                )
            (tmp_path / "statements" / f"{index}-{year}-{month:02}.pdf").write_text(continuation)
    config_path = tmp_path / "user.toml"
    config_path.write_text(
        robinhood_toml(first=accounts[0], second=accounts[1], extra="".join(reports))
    )
    monkeypatch.setattr(
        statement_parser, "pdftotext_layout", lambda path, **_kwargs: path.read_text()
    )
    bundle = load_robinhood_bundle(
        input_dir=tmp_path,
        config_path=config_path,
        financial_year=FinancialYear(2025),
        fetch_identities=False,
    )
    paired = bundle.paired

    assert canonical_statement_symbols(paired) == {exact.CUSIP: {_REPORTED, _RENAMED}}
    (observed,) = observed_robinhood_identity_inputs(paired)
    assert (observed.cusip, observed.symbol) == (exact.CUSIP, "")
    identities = resolve_robinhood_security_identities(paired, ())
    assert len(identities.blockers) == 1
    validate_robinhood_inventory_partition(
        paired, reconcile_robinhood_security_quantities(paired, identities)
    )


def test_a_statement_only_symbol_gets_the_securities_remedy_and_the_entry_clears_it(
    tmp_path: Path,
) -> None:
    """On main the dividend got UNMATCHED_SYMBOL ("complete by hand"),
    though the entry for its CUSIP identifies it. The entry clears the
    identity blocker and the statement mismatch it caused, and exposes no
    blocker the first run didn't name: the first run already names the
    dividend's missing Form 1042-S and the evidence short of its year (#1504).
    The financial-year coverage blockers are this one-statement case's own."""
    first = _renamed_income(())
    assert _identity_causes(first) == [(_Cause.UNCONFIGURED_SECURITY,)]

    entry = _security_entry(tmp_path, _RENAMED, exact.CUSIP)
    followed = _renamed_income((entry,))
    assert [
        (blocker.kind, blocker.causes, blocker.account_indices, blocker.tax_years)
        for blocker in followed.blockers
    ] == [
        (_Kind.ANNUAL_FORM_COVERAGE, (_Cause.ANNUAL_FORM_MISSING,), (0,), (2025,)),
        (_Kind.EVIDENCE_COVERAGE, (_Cause.EVIDENCE_SHORT_OF_FINANCIAL_YEAR,), (0,), ()),
        (_Kind.EVIDENCE_COVERAGE, (_Cause.EVIDENCE_SHORT_OF_FORM_YEAR,), (0,), (2025,)),
        (_Kind.EVIDENCE_COVERAGE, (_Cause.EVIDENCE_SHORT_OF_FINANCIAL_YEAR,), (1,), ()),
    ]
    assert set(first.blockers) - set(followed.blockers) == {
        blocker
        for blocker in first.blockers
        if blocker.kind in {_Kind.IDENTITY, _Kind.STATEMENT_RECONCILIATION}
    }
    assert set(followed.blockers) <= set(first.blockers)


# ---- part 3: the forms that disagree name their own accounts and years ----


def _disagreeing_forms() -> tuple[Any, Any]:
    """A 2025 form for each account; the second's agent phone differs."""
    other = income_packet(account_reference=INCOME_OTHER_ACCOUNT)
    other = replace(
        other,
        copies=tuple(
            replace(copy, agent=replace(copy.agent, phone="999-999-9998")) for copy in other.copies
        ),
    )
    return income_packet(), other


@pytest.mark.parametrize(
    "events",
    [
        pytest.param(None, id="source-income-in-one-account"),
        pytest.param((_BUY,), id="source-income-in-neither"),
    ],
)
@pytest.mark.parametrize("command", ["robinhood-income", "robinhood-all"])
def test_disagreeing_forms_name_both_accounts_and_the_forms_year(
    events: tuple[Any, ...] | None,
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On main the line named the account/years with source income: the
    first account only, or no account and no year."""
    case = _case(events, _disagreeing_forms())
    assert any(_Cause.ANNUAL_FORMS_DISAGREE in item.causes for item in _compute(case).blockers)

    code, out, err, _loaded_with = _run(command, case, tmp_path, monkeypatch, capsys)

    assert code == 4
    # Both commands print their report, refusals included, on stdout.
    assert err == ""
    assert out.count(_scoped(_Cause.ANNUAL_FORMS_DISAGREE, _BOTH)) == 1, out
    assert out.count("the supplied Forms 1042-S have different") == 1
