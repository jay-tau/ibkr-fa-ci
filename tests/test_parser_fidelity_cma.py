"""Synthetic exact-shape checks for the Fidelity CMA monthly parser."""

from __future__ import annotations

import calendar
import errno
import os
import re
import subprocess
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
from typing import cast

import pytest

from ibkr_fa.exceptions import ConfigError, ParseError
from ibkr_fa.models import DistributionKind, FidelityCmaSourceRecord
from ibkr_fa.parser import fidelity_cma
from ibkr_fa.parser._pdf import pdftotext_layout
from ibkr_fa.parser.fidelity_cma import (
    CmaCashActivityKind,
    CmaDebitCardTransaction,
    CmaDiscoveryError,
    CmaIncomeCredit,
    CmaIncomeKind,
    CmaStatement,
    CmaStatementBundle,
    CmaValuationVariant,
    load_fidelity_cma_statements,
    parse_statement,
    parse_year_end_income_summary,
    to_income_models,
    to_source_record,
    validate_statement_bundle,
)
from tests._crash_reports import assert_unchained_refusal
from tests._fidelity_cma import (
    _ACCOUNT,
    _HOLDER,
    CARD_ORDER_REFUSAL,
    TOTAL_HOLDINGS_REFUSAL,
    TRANSACTION_DATE_REFUSAL,
    patch_pdftotext,
)
from tests._fidelity_cma import MONTHLY_PAGE_BREAK as _PAGE_BREAK
from tests._fidelity_cma import YEAR_END_TEXT as _YEAR_END
from tests._fidelity_cma import monthly_statement_text as _statement
from tests._fidelity_cma import parse_monthly_text as _parse
from tests._text_edits import replace_once as _edit

_PERMISSIONS_ENFORCED = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)


def test_normal_layout_and_source_neutral_income_mapping() -> None:
    statement = _parse(_statement())

    assert statement.valuation_variant is CmaValuationVariant.ACCOUNT_VALUE
    assert statement.free_credit_balance_usd is None
    assert statement.account_value_usd == Decimal("105.00")
    assert statement.position.ending_market_value_usd == Decimal("105.00")
    assert [item.kind for item in statement.income_credits] == [
        CmaIncomeKind.CREDIT_INTEREST,
        CmaIncomeKind.SPAXX_DIVIDEND,
    ]
    assert [item.kind for item in statement.cash_activity] == [
        CmaCashActivityKind.DEPOSIT,
        CmaCashActivityKind.WITHDRAWAL,
        CmaCashActivityKind.TRANSFER_IN,
        CmaCashActivityKind.TRANSFER_OUT,
    ]
    assert len(statement.debit_card_transactions) == 1

    dividends, interests, withholdings = to_income_models(
        statement,
        spaxx_isin="US99971W1062",
    )
    assert dividends[0].amount_gross == Decimal("1.00")
    assert dividends[0].kind is DistributionKind.CASH_DIVIDEND
    assert interests[0].amount_gross == Decimal("0.10")
    assert withholdings[0].amount == Decimal("-0.25")


def test_source_record_keeps_statement_order_and_filters_by_row_date_inclusively() -> None:
    # The interest row dated 2025-12-31 sits in the January 2026 statement.
    january = _parse(_statement().replace("01/10 CREDIT INTEREST", "12/31 CREDIT INTEREST"))
    february = replace(
        january,
        period_start=date(2026, 2, 1),
        period_end=date(2026, 2, 28),
        beginning_account_value_usd=january.account_value_usd,
        income_credits=(
            CmaIncomeCredit(
                date(2026, 2, 27),
                CmaIncomeKind.SPAXX_DIVIDEND,
                Decimal("2.00"),
                "99971W106",
            ),
        ),
        withholdings=(),
    )

    record = to_source_record(
        validate_statement_bundle((january, february)),
        spaxx_isin="US99971W1062",
    )
    assert [(item.payment_date, item.amount_gross) for item in record.dividends] == [
        (date(2026, 1, 31), Decimal("1.00")),
        (date(2026, 2, 27), Decimal("2.00")),
    ]
    assert [item.credit_date for item in record.interest_credits] == [date(2025, 12, 31)]
    assert [(item.payment_date, item.amount) for item in record.withholding_tax] == [
        (date(2026, 1, 31), Decimal("-0.25"))
    ]

    dividends, interests, withholdings = record.income_in_window(
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
    )
    assert dividends == []
    assert [item.credit_date for item in interests] == [date(2025, 12, 31)]
    assert withholdings == []

    dividends, interests, withholdings = record.income_in_window(
        start=date(2026, 1, 31),
        end=date(2026, 2, 27),
    )
    assert [(item.payment_date, item.amount_gross) for item in dividends] == [
        (date(2026, 1, 31), Decimal("1.00")),
        (date(2026, 2, 27), Decimal("2.00")),
    ]
    assert interests == []
    assert [(item.payment_date, item.amount) for item in withholdings] == [
        (date(2026, 1, 31), Decimal("-0.25"))
    ]


def test_fcb_layout_and_first_statement_dash() -> None:
    fcb = _parse(_statement(fcb=True))
    assert fcb.valuation_variant is CmaValuationVariant.NET_ACCOUNT_VALUE_WITH_FCB
    free_credit_balance = fcb.free_credit_balance_usd
    assert free_credit_balance is not None
    assert free_credit_balance == Decimal("2.00")
    assert fcb.account_value_usd == (fcb.position.ending_market_value_usd + free_credit_balance)

    opened = _parse(
        _statement()
        .replace("Beginning Account Value $100.00", "Beginning Account Value -")
        .replace("Additions 10.00", "Additions 110.00")
    )
    assert opened.beginning_account_value_usd is None

    late_interest = _parse(_statement().replace("01/10 CREDIT INTEREST", "12/31 CREDIT INTEREST"))
    assert late_interest.income_credits[0].date == date(2025, 12, 31)

    with pytest.raises(ParseError, match="outside the statement period"):
        _parse(_statement().replace("01/15 PAYROLL DEPOSIT", "12/31 PAYROLL DEPOSIT"))


def test_bundle_gap_and_account_mismatch_are_redacted() -> None:
    statement = _parse(_statement())
    adjacent = replace(
        statement,
        period_start=date(2026, 2, 1),
        period_end=date(2026, 2, 28),
        beginning_account_value_usd=statement.account_value_usd,
    )
    assert validate_statement_bundle((adjacent, statement)).statements == (statement, adjacent)

    incomplete_first = replace(statement, period_start=date(2026, 1, 15))
    with pytest.raises(ParseError, match="partial first statement"):
        validate_statement_bundle((incomplete_first,))

    gap = replace(
        adjacent,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
    )
    with pytest.raises(ParseError, match="bundle has a gap"):
        validate_statement_bundle((statement, gap))

    other = "Z99-999998"
    with pytest.raises(
        ParseError, match="statement account does not match the configured account"
    ) as caught:
        parse_statement(
            _statement().replace(_ACCOUNT, other),
            expected_currency="USD",
            expected_account_id=_ACCOUNT,
        )
    assert other not in str(caught.value)
    assert _ACCOUNT not in str(caught.value)
    assert _HOLDER not in repr(statement)


def _next_month(previous: CmaStatement, start: date, end: date) -> CmaStatement:
    return replace(
        previous,
        period_start=start,
        period_end=end,
        beginning_account_value_usd=previous.account_value_usd,
    )


def test_bundle_construction_and_replace_run_every_check() -> None:
    """No unvalidated bundle can exist, built directly or by ``replace`` (#630)."""
    january = _parse(_statement())
    february = _next_month(january, date(2026, 2, 1), date(2026, 2, 28))
    march = _next_month(february, date(2026, 3, 1), date(2026, 3, 31))
    bundle = CmaStatementBundle((january, february))
    assert validate_statement_bundle((february, january)) == bundle
    # A hand-built statement with an inverted period, then the earlier month
    # with matching continuity: refused even though nothing overlaps.
    inverted = replace(january, period_start=date(2026, 2, 1), period_end=date(2025, 12, 31))
    after_inverted = replace(january, beginning_account_value_usd=inverted.account_value_usd)
    cases: list[tuple[object, str]] = [
        ([january, february], "statement bundle must be a tuple"),
        ((), "statement bundle is empty"),
        ((january, replace(february, account_id="Z99-000000")), "contains multiple accounts"),
        ((january, replace(february, account_holder="OTHER")), "inconsistent account holders"),
        ((january, replace(february, currency="EUR")), "contains multiple currencies"),
        ((replace(january, period_start=date(2026, 1, 15)),), "partial first statement"),
        ((january, january), "duplicate or overlapping periods"),
        ((february, january), "duplicate or overlapping periods"),
        ((inverted, after_inverted), "duplicate or overlapping periods"),
        ((january, march), "bundle has a gap"),
        (
            (january, replace(february, beginning_account_value_usd=Decimal("1.00"))),
            "ending and beginning values do not match",
        ),
    ]
    for statements, message in cases:
        with pytest.raises(ParseError, match=message):
            CmaStatementBundle(cast("tuple[CmaStatement, ...]", statements))
        with pytest.raises(ParseError, match=message):
            replace(bundle, statements=cast("tuple[CmaStatement, ...]", statements))


def test_through_slices_to_a_bundle_ending_on_the_date() -> None:
    january = _parse(_statement())
    february = _next_month(january, date(2026, 2, 1), date(2026, 2, 28))
    bundle = validate_statement_bundle((january, february))

    assert bundle.through(date(2026, 1, 31)) == CmaStatementBundle((january,))
    assert bundle.through(date(2026, 2, 28)) == bundle
    with pytest.raises(ParseError, match="statement bundle does not end at the requested date"):
        bundle.through(date(2026, 2, 15))


def _pdfs(folder: Path, *names: str) -> Path:
    folder.mkdir()
    for name in names:
        (folder / name).write_bytes(b"%PDF placeholder")
    return folder


def test_loader_renders_each_pdf_once_and_splits_year_end_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered: list[str] = []
    mapped: list[CmaStatement] = []
    texts = {"b.pdf": _statement(), "a.pdf": _YEAR_END}

    def render(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        rendered.append(Path(args[4]).name)
        return subprocess.CompletedProcess(args, 0, stdout=texts[Path(args[4]).name], stderr="")

    def map_income(statement: CmaStatement, *, spaxx_isin: str) -> object:
        mapped.append(statement)
        return to_income_models(statement, spaxx_isin=spaxx_isin)

    monkeypatch.setattr(subprocess, "run", render)
    monkeypatch.setattr(fidelity_cma, "to_income_models", map_income)
    bundle, year_ends, record = load_fidelity_cma_statements(
        _pdfs(tmp_path / "statements", "a.pdf", "b.pdf"),
        expected_account_id=_ACCOUNT,
        spaxx_isin="US99971W1062",
    )
    statement = _parse(_statement())
    assert rendered == ["a.pdf", "b.pdf"]
    assert mapped == [statement]
    assert bundle == CmaStatementBundle((statement,))
    assert list(year_ends) == [2025]
    dividends, interests, withholdings = to_income_models(statement, spaxx_isin="US99971W1062")
    assert record == FidelityCmaSourceRecord(
        dividends=dividends,
        interest_credits=interests,
        withholding_tax=withholdings,
    )


@pytest.mark.parametrize(
    ("names", "message"),
    [
        (None, "monthly-statement input must be a directory"),
        ((), "no monthly statement PDFs found"),
        (("first.pdf", "second.pdf"), "duplicate CMA year-end report"),
    ],
)
def test_loader_discovery_refusals_keep_their_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    names: tuple[str, ...] | None,
    message: str,
) -> None:
    patch_pdftotext(monkeypatch, {"first.pdf": _YEAR_END, "second.pdf": _YEAR_END}, by_name=True)
    folder = tmp_path / "statements"
    if names is not None:
        _pdfs(folder, *names)
    with pytest.raises(CmaDiscoveryError) as caught:
        load_fidelity_cma_statements(
            folder,
            expected_account_id=_ACCOUNT,
            spaxx_isin="US99971W1062",
        )
    assert str(caught.value) == message


@_PERMISSIONS_ENFORCED
def test_loader_unsearchable_input_folder_raises_eacces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """#1238: the statement-folder check raises EACCES instead of calling the folder missing."""
    rendered: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda args, **_: rendered.append(args))
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    folder = _pdfs(blocked / "statements", "first.pdf")
    blocked.chmod(0)
    request.addfinalizer(lambda: blocked.chmod(0o700))
    with pytest.raises(PermissionError) as raised:
        load_fidelity_cma_statements(
            folder,
            expected_account_id=_ACCOUNT,
            spaxx_isin="US99971W1062",
        )
    assert type(raised.value) is PermissionError
    assert str(raised.value) == f"[Errno {errno.EACCES}] Permission denied: {str(folder)!r}"
    blocked.chmod(0o700)
    assert [path.name for path in folder.iterdir()] == ["first.pdf"]
    assert rendered == []


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        pytest.param(
            subprocess.CalledProcessError(1, ["pdftotext"], stderr="failed to open SECRET"),
            "pdftotext failed",
            id="pdftotext-failed",
        ),
        pytest.param(
            # A missing executable, as subprocess raises it (#1041).
            FileNotFoundError(
                errno.ENOENT, "Synthetic Strerror Marker", "Synthetic Holder Two/pdftotext"
            ),
            "'pdftotext' not found on PATH (ENOENT); install poppler-utils to read "
            "Fidelity CMA statement PDFs",
            id="pdftotext-not-found",
        ),
    ],
)
def test_loader_pdftotext_failure_keeps_its_section_without_the_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    message: str,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    folder = _pdfs(tmp_path / "SECRET-FOLDER", "SECRET-STATEMENT.pdf")
    with pytest.raises(ParseError) as caught:
        load_fidelity_cma_statements(
            folder,
            expected_account_id=_ACCOUNT,
            spaxx_isin="US99971W1062",
        )
    rendered = str(caught.value)
    assert rendered == f"ParseError in section 'Fidelity CMA statement PDF' at row 0: {message}"
    # CMA already passed source: preserve its privacy guarantee alongside the new errno pin.
    for marker in ("secret", "synthetic strerror marker", "synthetic holder two"):
        assert marker not in rendered.casefold()


def test_unsupported_activity_year_end_currency_and_isin_refuse() -> None:
    extra_holding = _statement().replace(
        "Total Core Account",
        "OTHER HOLDING $1.00\nTotal Core Account",
        1,
    )
    with pytest.raises(ParseError, match="Holdings contains an unsupported line"):
        _parse(extra_holding)

    unsupported = _statement().replace(
        "Core Fund Activity\n",
        "Fees\n01/25 SERVICE FEE -$1.00\nTotal Fees -$1.00\nCore Fund Activity\n",
        1,
    )
    with pytest.raises(ParseError, match="unsupported or malformed dated activity"):
        _parse(unsupported)

    with pytest.raises(ParseError, match="year-end reports are not supported"):
        _parse(_statement().replace("INVESTMENT REPORT", "2026 YEAR-END INVESTMENT REPORT"))

    with pytest.raises(ParseError, match="explicitly confirmed USD"):
        parse_statement(_statement(), expected_currency="EUR")

    with pytest.raises(ConfigError, match="verified 12-character"):
        to_income_models(_parse(_statement()), spaxx_isin="NOT-AN-ISIN")

    statement = _parse(_statement())
    unknown = replace(
        statement.income_credits[0],
        kind=cast("CmaIncomeKind", "Return of Capital"),
    )
    with pytest.raises(ParseError, match="unsupported CMA income classification"):
        to_income_models(
            replace(statement, income_credits=(unknown,)),
            spaxx_isin="US99971W1062",
        )


def test_private_pdf_extraction_failure_does_not_leak_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_path = Path("/private/SECRET-STATEMENT.pdf")

    def fail(*args: object, **kwargs: object) -> object:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=["pdftotext", str(private_path)],
            stderr=f"failed to open {private_path}",
        )

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ParseError) as caught:
        pdftotext_layout(
            private_path,
            doc_kind="Fidelity CMA statement PDFs",
            source="Fidelity CMA statement PDF",
        )
    rendered = str(caught.value)
    assert str(private_path) not in rendered
    assert "failed to open" not in rendered


def test_year_end_parser_corroborates_spaxx_holdings() -> None:
    text = _YEAR_END
    result = parse_year_end_income_summary(
        text,
        expected_currency="USD",
        expected_account_id=_ACCOUNT,
    )
    assert result.position is not None
    assert result.position.quantity == Decimal("10.000")
    assert result.position.market_value_usd == Decimal("10.00")


# #1095: each row is read whole and only in its own section, and every line of
# the sections the parser reads is read or refuses.

_SECTION = "Fidelity CMA monthly statement"
# Privacy markers: no refusal may print an edited word or amount.
_MARKERS = (
    "Synthetic Holder Two",
    "Synthetic Marker Q",
    "777.00",
    "999.00",
    "5.00",
    "0.50",
    "999",
    "SOME FUND",
    "OTHER CREDIT",
    "Exchange Rate",
)
_TWO_COLUMNS = (
    "Account Summary row is malformed: expected This Period and Year-to-Date amounts or dashes"
)
_ONE_AMOUNT = "Account Summary row is malformed: expected one amount"
_TOTAL_CORE = (
    "Total Core Account row is malformed: expected its share of holdings "
    "and two or three dollar amounts"
)
_DATED = "statement contains unsupported or malformed dated activity"
_INCOME = "Dividends, Interest & Other Income"
_CORE_TOTAL = "Total Core Account (100% of account $100.00 $105.00 $4.20\nholdings)"
_DISCLOSURE = "Brokerage services provided by Fidelity Brokerage Services LLC.\n"


def _assert_refuses(
    text: str,
    message: str,
    column: str | None = None,
    *,
    leaks: tuple[str, ...] = (),
    year_end: bool = False,
) -> None:
    with pytest.raises(ParseError) as caught:
        if year_end:
            parse_year_end_income_summary(
                text, expected_currency="USD", expected_account_id=_ACCOUNT
            )
        else:
            _parse(text)
    error = caught.value
    assert (error.section, error.row_index, error.column, error.message, error.raw_value) == (
        _SECTION,
        0,
        column,
        message,
        "",
    )
    rendered = str(error)
    for marker in leaks:
        assert marker in text
    for marker in (*_MARKERS, *leaks):
        if marker in text:
            assert marker not in rendered


@pytest.mark.parametrize(
    ("line", "column", "message"),
    [
        ("Account Value: $105.00", "Account Value:", _ONE_AMOUNT),
        ("Beginning Account Value $100.00 $100.00", "Beginning Account Value", _TWO_COLUMNS),
        ("Additions 10.00 10.00", "Additions", _TWO_COLUMNS),
        ("Subtractions -5.00 -5.00", "Subtractions", _TWO_COLUMNS),
        ("Change in Investment Value * 0.00 0.00", "Change in Investment Value *", _TWO_COLUMNS),
        ("Ending Account Value $105.00 $105.00", "Ending Account Value", _TWO_COLUMNS),
        ("Accrued Interest (AI) 0.00", "Accrued Interest (AI)", _ONE_AMOUNT),
        ("Ending Account Value Incl. AI $105.00", "Ending Account Value Incl. AI", _ONE_AMOUNT),
    ],
)
def test_summary_row_with_an_extra_number_refuses(line: str, column: str, message: str) -> None:
    _assert_refuses(_edit(_statement(), f"\n{line}", f"\n{line} 0.50"), message, column)


@pytest.mark.parametrize(
    ("text", "message", "column", "leaks"),
    [
        pytest.param(
            _edit(
                _statement(),
                "Beginning Account Value $100.00 $100.00",
                "Beginning Account Value $100.00- $100.00",
            ),
            _TWO_COLUMNS,
            "Beginning Account Value",
            ("100.00",),
            id="trailing-sign",
        ),
        pytest.param(
            _edit(
                _statement(fcb=True),
                "Beginning Net Account Value $102.00 $102.00",
                "Beginning Net Account Value $102.00- $102.00",
            ),
            _TWO_COLUMNS,
            "Beginning Net Account Value",
            ("102.00",),
            id="fcb-trailing-sign",
        ),
        pytest.param(
            _edit(_statement(), "Additions 10.00 10.00", "Additions 10.009 10.00"),
            _TWO_COLUMNS,
            "Additions",
            ("10.009",),
            id="third-decimal",
        ),
        pytest.param(
            _edit(_statement(), "Additions 10.00 10.00", "Additions 10.00 10.00 CR"),
            _TWO_COLUMNS,
            "Additions",
            ("CR",),
            id="suffix",
        ),
        pytest.param(
            _edit(_statement(), "Additions 10.00 10.00", "Additions 10.00 Synthetic Marker Q"),
            _TWO_COLUMNS,
            "Additions",
            (),
            id="year-to-date-not-an-amount",
        ),
        pytest.param(
            _edit(_statement(), "Additions 10.00 10.00\n", "Additions 10.00 10.00\n" * 2),
            "Additions row is duplicated in Account Summary",
            "Additions",
            (),
            id="duplicated",
        ),
        pytest.param(
            _edit(
                _statement(),
                "Top Holdings\n",
                "Top Holdings\n  Synthetic Holder Two    0.50    0.50\n",
            ),
            "Account Summary contains an unsupported line",
            None,
            (),
            id="unsupported-line",
        ),
        pytest.param(
            _edit(
                _edit(_statement(), "Accrued Interest (AI) 0.00    Total", "    Total"),
                "01/15 PAYROLL DEPOSIT $10.00",
                "01/15 PAYROLL DEPOSIT Synthetic Holder Two Accrued Interest (AI) 0.00 $10.00",
            ),
            "Accrued Interest (AI) row is missing, malformed or outside Account Summary",
            "Accrued Interest (AI)",
            (),
            id="ai-outside-summary",
        ),
        pytest.param(
            _edit(
                _statement(),
                "Accrued Interest (AI) 0.00    Total",
                "Accrued Interest (AI)\n    Total",
            ),
            _ONE_AMOUNT,
            "Accrued Interest (AI)",
            (),
            id="ai-label-without-amount",
        ),
        pytest.param(
            _edit(_statement(), "Your Account Value: $105.00", "Your Account Value: $777.00"),
            "aggregate account value is missing or inconsistent across the statement",
            None,
            (),
            id="cover-value-differs",
        ),
    ],
)
def test_account_summary_refusals(
    text: str, message: str, column: str | None, leaks: tuple[str, ...]
) -> None:
    _assert_refuses(text, message, column, leaks=leaks)


def test_accrued_interest_printed_above_its_label_parses() -> None:
    """Text extraction can print the Accrued Interest amount on the line above."""
    expected = _parse(_statement())
    moved = _edit(
        _statement(),
        "Accrued Interest (AI) 0.00    Total    $105    100%    S\n",
        "0.00    Total    $105    100%    S\nAccrued Interest (AI)\n",
    )
    assert _parse(moved) == expected


@pytest.mark.parametrize(
    ("text", "message", "column"),
    [
        pytest.param(
            _edit(_statement(), "Total Holdings $105.00", "Total Holdings $777.00 $105.00"),
            TOTAL_HOLDINGS_REFUSAL,
            "Total Holdings",
            id="total-holdings-extra-amount",
        ),
        pytest.param(
            _edit(
                _statement(),
                "Total Holdings $105.00 $4.20",
                "Total Holdings $105.00 $4.20 Synthetic Holder Two",
            ),
            TOTAL_HOLDINGS_REFUSAL,
            "Total Holdings",
            id="total-holdings-word-after",
        ),
        pytest.param(
            _edit(_statement(), "Total Holdings $105.00 $4.20", "Total Holdings $105.00 $4.20-"),
            TOTAL_HOLDINGS_REFUSAL,
            "Total Holdings",
            id="total-holdings-trailing-sign",
        ),
        pytest.param(
            _edit(_statement(), _CORE_TOTAL, _CORE_TOTAL.replace("$100.00", "$999.00")),
            "Total Core Account beginning value does not match the core fund holding",
            "Total Core Account",
            id="total-core-beginning-differs",
        ),
        pytest.param(
            _edit(_statement(), "\nholdings)\n", "\nholdings, Synthetic Holder Two)\n"),
            _TOTAL_CORE,
            "Total Core Account",
            id="total-core-parenthetical",
        ),
        pytest.param(
            _edit(
                _statement(),
                "Total Holdings",
                "OTHER HOLDING Synthetic Holder Two 5.000 1.00\nTotal Holdings",
            ),
            "Holdings contains an unsupported line",
            None,
            id="undollared-holding",
        ),
        pytest.param(
            _edit(_statement(), "MARKET (SPAXX) 4.000%", "MARKET (SPAXX) 999 4.000%"),
            "SPAXX symbol/yield row is malformed",
            "MARKET (SPAXX)",
            id="symbol-line-extra-token",
        ),
        pytest.param(
            _edit(
                _edit(_statement(fcb=True), "Free Credit Balance    $2.00\n", ""),
                _DISCLOSURE,
                _DISCLOSURE + "Free Credit Balance $2.00\n",
            ),
            "Free Credit Balance row is outside Account Summary",
            "Free Credit Balance",
            id="fcb-row-in-disclosure",
        ),
        pytest.param(
            _edit(
                _edit(_statement(fcb=True), "\nNet Account Value    $107.00\n", "\n"),
                _DISCLOSURE,
                _DISCLOSURE + "Net Account Value $107.00\n",
            ),
            "Net Account Value row is outside Holdings",
            "Net Account Value",
            id="net-account-value-in-disclosure",
        ),
        pytest.param(
            _edit(
                _edit(_statement(fcb=True), "\nNet Account Value    $107.00\n", "\n"),
                "Total Holdings",
                "Net Account Value    $107.00\nTotal Holdings",
            ),
            "Net Account Value row precedes Total Holdings",
            "Net Account Value",
            id="net-account-value-before-total-holdings",
        ),
    ],
)
def test_holdings_refusals(text: str, message: str, column: str | None) -> None:
    _assert_refuses(text, message, column)


@pytest.mark.parametrize(
    ("text", "message", "column"),
    [
        pytest.param(
            _edit(_YEAR_END, "Total Holdings $10.00", "Total Holdings $777.00 $10.00"),
            f"year-end {TOTAL_HOLDINGS_REFUSAL}",
            "Total Holdings",
            id="total-holdings-extra-amount",
        ),
        pytest.param(
            _edit(_YEAR_END, "account $10.00", "account $777.00 $10.00"),
            "year-end Total Core Account row is malformed: expected its share of holdings "
            "and two dollar amounts",
            "Total Core Account",
            id="total-core-extra-amount",
        ),
        pytest.param(
            _edit(_YEAR_END, "Total Holdings", "Synthetic Holder Two 5.000\nTotal Holdings"),
            "year-end Holdings contains an unsupported line",
            None,
            id="unsupported-holdings-line",
        ),
        pytest.param(
            _edit(_YEAR_END, "Interest 0.50", "Margin Interest 0.50"),
            "year-end Interest row is malformed",
            "Interest",
            id="margin-interest",
        ),
        pytest.param(
            _edit(_YEAR_END, "Interest 0.50", "Interest 0.50 Synthetic Marker Q"),
            "year-end Interest row is malformed",
            "Interest",
            id="interest-suffix",
        ),
        pytest.param(
            _edit(_YEAR_END, "Dividends 1.00", "Dividends 1.00 0.50"),
            "year-end Account Summary row is malformed: expected one amount and nothing else",
            "Dividends",
            id="dividends-second-number",
        ),
        pytest.param(
            _edit(_YEAR_END, "Holdings\n", "31-Jan-2025 Synthetic Holder Two $5.00\nHoldings\n"),
            _DATED,
            None,
            id="dated-line",
        ),
    ],
)
def test_year_end_refusals(text: str, message: str, column: str | None) -> None:
    _assert_refuses(text, message, column, year_end=True)


def test_year_end_two_column_interest_must_be_the_report_year() -> None:
    base = _edit(
        _YEAR_END, "Interest 0.50", "Ending Account Value as of Dec 31, 2025 $10.00 Interest 0.50"
    )
    summary = parse_year_end_income_summary(
        base, expected_currency="USD", expected_account_id=_ACCOUNT
    )
    assert summary.interest_usd == Decimal("0.50")
    _assert_refuses(
        _edit(base, "Dec 31, 2025", "Dec 31, 2024"),
        "year-end Interest row is malformed",
        "Interest",
        year_end=True,
    )


_DEPOSIT_COLUMNS = "Deposits\nDate Reference Description Amount"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param(
            _edit(_statement(), _DEPOSIT_COLUMNS, f"{_DEPOSIT_COLUMNS} Exchange Rate"),
            "Deposits section column header is missing or unsupported",
            id="added-column",
        ),
        pytest.param(
            _edit(_statement(), _DEPOSIT_COLUMNS, "Deposits\nDate Synthetic Marker Q Amount"),
            "Deposits section column header is missing or unsupported",
            id="replaced-column-header",
        ),
        pytest.param(
            _edit(_statement(), _DEPOSIT_COLUMNS, "Deposits"),
            "Deposits section column header is missing or unsupported",
            id="deleted-column-header",
        ),
        pytest.param(
            _edit(_statement(), "$0.10\n", "$0.10\nExchange Rate Synthetic Marker Q 0.50\n"),
            f"{_INCOME} section contains a line that is not a supported row",
            id="income-exchange-rate-line",
        ),
        pytest.param(
            _edit(
                _statement(),
                "Total Dividends",
                "2026-01-31 SOME FUND Synthetic Holder Two Other Income $5.00\nTotal Dividends",
            ),
            f"{_INCOME} section contains a line that is not a supported row",
            id="income-iso-dated-row",
        ),
        pytest.param(
            _edit(
                _statement(),
                "Total Deposits",
                "OTHER CREDIT Synthetic Holder Two $5.00\nTotal Deposits",
            ),
            "Deposits section contains a line that is not a supported row",
            id="undated-deposit",
        ),
        pytest.param(
            _edit(
                _statement(),
                "Description Amount\n01/15",
                "Description Amount\nSynthetic Holder Two\n01/15",
            ),
            "Deposits section contains a line that is not a supported row",
            id="wrapped-description-before-any-row",
        ),
        pytest.param(
            _edit(
                _statement(),
                "Total Withdrawals",
                "Jan 25 Synthetic Holder Two -$5.00\nTotal Withdrawals",
            ),
            "Withdrawals section contains a line that is not a supported row",
            id="month-name-withdrawal",
        ),
        pytest.param(
            _edit(
                _statement(),
                "01/21 01/22",
                "2026-01-21 2026-01-22 Synthetic Holder Two -$5.00\n01/21 01/22",
            ),
            "Debit Card Activity section contains a line that is not a supported row",
            id="iso-dated-card-row",
        ),
        pytest.param(
            _edit(
                _statement(),
                "REINVEST @ $1.000    S\n",
                "REINVEST @ $1.000    S\n2026-01-20 CASH You Bought FIDELITY GOVERNMENT MONEY "
                "MARKET 5.000 $1.0000 $5.00 $109.25\n",
            ),
            "Core Fund Activity section contains a line that is not a supported row",
            id="iso-dated-core-row",
        ),
        pytest.param(
            _edit(_statement(), "MORNING TRADE @ 1\n", ""),
            "Core Fund Activity section contains a row without its second line",
            id="core-row-without-second-line",
        ),
        pytest.param(
            _edit(
                _statement(),
                "Amount Balance\n01/15",
                "Amount Balance\nMORNING TRADE @ 1\n01/15",
            ),
            "Core Fund Activity section contains a line that is not a supported row",
            id="core-second-line-without-row",
        ),
        pytest.param(
            _edit(_statement(), "Transferred From - -", "Transferred From 5.000 -"),
            "Exchanges In row has an unsupported Quantity or Price",
            id="exchange-in-quantity",
        ),
        pytest.param(
            _edit(_statement(), "Transferred To - -", "Transferred To - $1.0000"),
            "Exchanges Out row has an unsupported Quantity or Price",
            id="exchange-out-price",
        ),
        pytest.param(
            _edit(
                _statement(),
                "Debit Card Summary    Debit Card Activity",
                "Synthetic Marker Q Debit Card Summary    Debit Card Activity",
            ),
            "Debit Card Activity section is missing or displaced",
            id="card-heading-not-whole",
        ),
        pytest.param(
            _edit(_statement(), "TRANSACTIONS\n", "TRANSACTIONS Synthetic Marker Q\n"),
            "Debit Card Activity section column header is missing or unsupported",
            id="card-subheading",
        ),
        pytest.param(
            _edit(_statement(), "Total Purchases -$3.00", "Total Purchases -$5.00"),
            "debit-card Purchases and card subtotals do not match the section total",
            id="card-purchases-total",
        ),
        pytest.param(
            _edit(_statement(), "(*0000) Subtotal -$3.00", "(*0000) Subtotal -$5.00"),
            "debit-card Purchases and card subtotals do not match the section total",
            id="card-subtotal",
        ),
        pytest.param(
            _edit(
                _statement(),
                f"{_HOLDER} - INDIVIDUAL - TOD\nCore Fund Activity (continued)",
                "Synthetic Holder Two - INDIVIDUAL - TOD\nCore Fund Activity (continued)",
            ),
            "Core Fund Activity section contains a line that is not a supported row",
            id="another-holders-page",
        ),
    ],
)
def test_activity_section_refusals(text: str, message: str) -> None:
    _assert_refuses(text, message)


@pytest.mark.parametrize(
    "line",
    [
        "31-Jan-2026 Synthetic Holder Two $5.00",
        "25.01.2026 Synthetic Holder Two $5.00",
        "2026-01-25 Synthetic Holder Two $5.00",
        "01/25/2026 Synthetic Holder Two $5.00",
        "01/25 Synthetic Holder Two $5.00",
        "Jan 25 Synthetic Holder Two $5.00",
        "January 25 Synthetic Holder Two $5.00",
    ],
)
def test_dated_line_between_sections_refuses(line: str) -> None:
    text = _edit(_statement(), "Total Deposits $10.00\n", f"Total Deposits $10.00\n{line}\n")
    _assert_refuses(text, _DATED)


def test_page_furniture_is_allowed_in_every_read_section() -> None:
    expected = _parse(_statement())
    for anchor in ("Additions 10.00 10.00\n", "Core Account\n", "01/15 PAYROLL DEPOSIT $10.00\n"):
        assert _parse(_edit(_statement(), anchor, anchor + _PAGE_BREAK + "S\n")) == expected
    year_end = parse_year_end_income_summary(
        _edit(_YEAR_END, "Core Account\n", "Core Account\nS\n"),
        expected_currency="USD",
        expected_account_id=_ACCOUNT,
    )
    assert year_end == parse_year_end_income_summary(
        _YEAR_END, expected_currency="USD", expected_account_id=_ACCOUNT
    )


def test_supported_layouts_parse_with_unchanged_results() -> None:
    expected = _parse(_statement())
    two_amount_core = _edit(_statement(), _CORE_TOTAL, _CORE_TOTAL.replace("$100.00 ", ""))
    assert _parse(two_amount_core) == expected

    # The first statement's SPAXX row and Total Core Account print no beginning value.
    opened = _parse(
        _edit(
            _edit(
                _edit(
                    _statement(),
                    "Beginning Account Value $100.00 $100.00",
                    "Beginning Account Value - -",
                ),
                "Additions 10.00 10.00",
                "Additions 110.00 110.00",
            ),
            "MONEY $100.00 105.000",
            "MONEY unavailable 105.000",
        ).replace("$100.00 $105.00 $4.20\nholdings)", "unavailable $105.00 $4.20\nholdings)")
    )
    assert opened.beginning_account_value_usd is None
    assert opened.position.beginning_market_value_usd is None

    for suffix in ("1.0000", "2026"):
        text = _edit(_statement(), "PAYROLL DEPOSIT $10.00", f"PAYROLL DEPOSIT {suffix} $10.00")
        deposit = _parse(text).cash_activity[0]
        assert deposit.description == f"PAYROLL DEPOSIT {suffix}"
        assert deposit.amount_usd == Decimal("10.00")

    # A deposit's wrapped description line, right after its row, is read by shape.
    wrapped = _edit(
        _statement(), "PAYROLL DEPOSIT $10.00\n", "PAYROLL DEPOSIT $10.00\nSample Bank, Na 0000\n"
    )
    assert _parse(wrapped) == expected

    # The Quantity and Price dashes are cells, not part of the description.
    assert [item.description for item in expected.cash_activity[2:]] == [
        "Z99-000001-0 Transferred From",
        "Z99-000001-0 Transferred To",
    ]

    fcb_opened = _parse(
        _edit(
            _edit(
                _statement(fcb=True),
                "Beginning Net Account Value $102.00 $102.00",
                "Beginning Net Account Value - -",
            ),
            "Additions 10.00 10.00",
            "Additions 112.00 112.00",
        )
    )
    assert fcb_opened.beginning_account_value_usd is None
    assert fcb_opened.account_value_usd == Decimal("107.00")


@pytest.mark.parametrize(
    ("year_end", "section"), [(False, "Holdings"), (False, "Activity"), (True, "Holdings")]
)
@pytest.mark.parametrize(
    "suffix", ["Synthetic Holder Two - INDIVIDUAL - TOD", "Synthetic Marker Q 777.00"]
)
def test_initial_section_heading_is_read_whole(year_end: bool, section: str, suffix: str) -> None:
    base = _YEAR_END if year_end else _statement()
    heading = "Holdings" if year_end else f"{section} {_HOLDER} - INDIVIDUAL - TOD"
    _assert_refuses(
        _edit(base, f"\n{heading}\n", f"\n{section} {suffix}\n"),
        f"{'year-end ' if year_end else ''}{section} section heading is malformed",
        section,
        year_end=year_end,
        leaks=(suffix,),
    )


@pytest.mark.parametrize(
    ("header", "preamble"),
    [
        (_INCOME, "(Includes dividend reinvestment)\nSettlement    Symbol/"),
        ("Exchanges In", "Symbol/"),
        ("Exchanges Out", "Symbol/"),
        (
            "Core Fund Activity",
            "For more information about the operation of your core account, please refer to your "
            "Customer Agreement.\nSettlement    Account",
        ),
    ],
)
@pytest.mark.parametrize("edit", ["missing", "changed", "after-columns"])
def test_activity_header_requires_its_initial_preamble(
    header: str, preamble: str, edit: str
) -> None:
    columns = (
        "Date Type Transaction Description Quantity Price Amount Balance"
        if header == "Core Fund Activity"
        else "Date Security Name CUSIP Description Quantity Price Amount"
    )
    original = f"{header}\n{preamble}\n{columns}"
    if edit == "missing":
        replacement = f"{header}\n{columns}"
    elif edit == "changed":
        replacement = f"{header}\n{preamble} Synthetic Marker Q\n{columns}"
    else:
        replacement = f"{header}\n{columns}\n{preamble}"
    _assert_refuses(
        _edit(_statement(), original, replacement),
        f"{header} section column header is missing or unsupported",
    )


@pytest.mark.parametrize("fcb", [False, True])
def test_cover_value_allows_margin_s(fcb: bool) -> None:
    base = _statement(fcb=fcb)
    label = "Net Account" if fcb else "Account"
    value = "107.00" if fcb else "105.00"
    assert _parse(
        _edit(base, f"Your {label} Value: ${value}", f"Your {label} Value: ${value} S")
    ) == _parse(base)


def test_income_market_line_allows_margin_s() -> None:
    assert _parse(_edit(_statement(), "\nMARKET\n", "\nMARKET S\n")) == _parse(_statement())


@pytest.mark.parametrize(
    "row",
    [
        "Dividends 1.00",
        "Interest 0.50",
        "Taxes Withheld -0.25",
        "Ending Account Value as of Dec 31, 2025 $10.00 Interest 0.50",
    ],
)
def test_year_end_summary_rows_allow_margin_s(row: str) -> None:
    base = _edit(_YEAR_END, "Interest 0.50", row) if row.startswith("Ending") else _YEAR_END
    actual = parse_year_end_income_summary(
        _edit(base, row, row + " S"), expected_currency="USD", expected_account_id=_ACCOUNT
    )
    assert actual == parse_year_end_income_summary(
        base, expected_currency="USD", expected_account_id=_ACCOUNT
    )


def test_displaced_accrued_interest_label_allows_a_right_column() -> None:
    moved = _edit(
        _statement(),
        "Accrued Interest (AI) 0.00    Total    $105    100%    S\n",
        "0.00\nAccrued Interest (AI)    Total    $105    100%    S\n",
    )
    assert _parse(moved) == _parse(_statement())


@pytest.mark.parametrize(
    ("label", "value"), [("Free Credit Balance", "2.00"), ("Market Value of Holdings", "105.00")]
)
@pytest.mark.parametrize("right_column", ["Income Summary", "Total $105 100%"])
def test_summary_fcb_rows_with_right_columns_refuse_outside_summary(
    label: str, value: str, right_column: str
) -> None:
    _assert_refuses(
        _statement(fcb=True) + f"\n{label} ${value} {right_column}\n",
        f"{label} row is outside Account Summary",
        label,
        leaks=(value,),
    )


@pytest.mark.parametrize(
    ("label", "value", "message"),
    [
        ("Free Credit Balance", "2.00", _ONE_AMOUNT),
        ("Market Value of Holdings", "105.00", _ONE_AMOUNT),
        (
            "Net Account Value",
            "107.00",
            "Net Account Value row is malformed: expected one dollar amount",
        ),
    ],
)
def test_malformed_fcb_value_row_names_its_column(label: str, value: str, message: str) -> None:
    _assert_refuses(
        _edit(_statement(fcb=True), f"\n{label}    ${value}\n", f"\n{label}    ${value}-\n"),
        message,
        label,
        leaks=(value,),
    )


@pytest.mark.parametrize(
    ("row", "kind", "column"),
    [
        ("Total Holdings $105.00 $4.20", "Total Holdings", "Total Holdings"),
        (_CORE_TOTAL, "Total Core Account", "Total Core Account"),
        ("MARKET (SPAXX) 4.000%", "SPAXX symbol/yield", "MARKET (SPAXX)"),
        (
            "FIDELITY GOVERNMENT MONEY $100.00 105.000 $1.0000 $105.00 $4.20",
            "SPAXX holdings",
            "FIDELITY GOVERNMENT MONEY",
        ),
    ],
)
def test_duplicate_holdings_row_names_its_column(row: str, kind: str, column: str) -> None:
    _assert_refuses(
        _edit(_statement(), f"\n{row}\n", f"\n{row}\n{row}\n"),
        f"expected exactly one {kind} row; found 2",
        column,
    )


def test_malformed_spaxx_holding_names_its_column() -> None:
    _assert_refuses(
        _edit(_statement(), "MONEY $100.00 105.000", "MONEY $100.00 Synthetic Marker Q 105.000"),
        "SPAXX holdings row is malformed",
        "FIDELITY GOVERNMENT MONEY",
        leaks=("Synthetic Marker Q",),
    )


def test_activity_preamble_allows_page_furniture() -> None:
    for anchor in ("Settlement    Symbol/\n", "Exchanges In\nSymbol/\n", "Settlement    Account\n"):
        assert _parse(_edit(_statement(), anchor, anchor + _PAGE_BREAK)) == _parse(_statement())


def test_monthly_section_headings_allow_the_bare_titles() -> None:
    base = _statement()
    bare = base.replace(f"Holdings {_HOLDER} - INDIVIDUAL - TOD", "Holdings").replace(
        f"Activity {_HOLDER} - INDIVIDUAL - TOD", "Activity"
    )
    assert _parse(bare) == _parse(base)


@pytest.mark.parametrize(
    ("anchor", "extra", "header"),
    [
        (
            "01/15 PAYROLL DEPOSIT $10.00\n",
            "Date Reference Description Amount\n",
            "Deposits",
        ),
        ("\nMARKET\n", "Settlement Symbol/\n", _INCOME),
        (
            "\nMARKET\n",
            "(Includes dividend reinvestment)\nSettlement Symbol/\n"
            "Date Security Name CUSIP Description Quantity Price Amount\n",
            _INCOME,
        ),
        ("Transferred From - - $1.00\n", "Symbol/\n", "Exchanges In"),
    ],
)
def test_repeated_activity_header_requires_a_continued_heading(
    anchor: str, extra: str, header: str
) -> None:
    _assert_refuses(
        _edit(_statement(), anchor, anchor + extra),
        f"{header} section column header is missing or unsupported",
    )


@pytest.mark.parametrize(
    "repeated",
    [
        "Settlement Symbol/\n",
        "Settlement Symbol/\n(Includes dividend reinvestment)\n"
        "Date Security Name CUSIP Description Quantity Price Amount\n",
    ],
)
def test_repeated_activity_preamble_must_be_complete_and_ordered(repeated: str) -> None:
    anchor = "01/10 CREDIT INTEREST - - $0.10\n"
    _assert_refuses(
        _edit(_statement(), anchor, anchor + f"{_INCOME} (continued)\n" + repeated),
        f"{_INCOME} section column header is missing or unsupported",
    )


def test_continued_activity_allows_the_complete_header_and_page_furniture() -> None:
    anchor = "01/10 CREDIT INTEREST - - $0.10\n"
    repeated = (
        f"{_INCOME} (continued)\n(Includes dividend reinvestment)\n"
        f"{_PAGE_BREAK}Settlement Symbol/\n"
        "Date Security Name CUSIP Description Quantity Price Amount\n"
    )
    assert _parse(_edit(_statement(), anchor, anchor + repeated)) == _parse(_statement())


def test_continued_activity_does_not_require_a_repeated_header() -> None:
    anchor = "01/10 CREDIT INTEREST - - $0.10\n"
    assert _parse(_edit(_statement(), anchor, anchor + f"{_INCOME} (continued)\n")) == _parse(
        _statement()
    )


def test_continued_core_activity_allows_the_complete_preamble() -> None:
    heading = "Core Fund Activity (continued)\n"
    preamble = (
        "For more information about the operation of your core account, please refer "
        "to your Customer Agreement.\nSettlement Account\n"
    )
    assert _parse(_edit(_statement(), heading, heading + preamble)) == _parse(_statement())


def test_minimum_year_transaction_date_refuses_without_an_untyped_exception() -> None:
    _assert_refuses(
        _statement().replace("2026", "0001"),
        TRANSACTION_DATE_REFUSAL,
        leaks=("0001",),
    )


def test_year_end_zero_title_year_with_a_valid_period_refuses() -> None:
    _assert_refuses(
        _YEAR_END.replace("2025 YEAR-END INVESTMENT REPORT", "0000 YEAR-END INVESTMENT REPORT"),
        "year-end report does not cover one complete calendar year",
        year_end=True,
        leaks=("0000",),
    )


@pytest.mark.parametrize(
    ("row", "spaced", "malformed", "message", "column"),
    [
        (
            "Beginning Account Value $100.00 $100.00",
            "Beginning\tAccount  Value $100.00 $100.00",
            "Beginning\tAccount  Value $100.00- $100.00",
            _TWO_COLUMNS,
            "Beginning Account Value",
        ),
        (
            "MARKET (SPAXX) 4.000%",
            "MARKET  (SPAXX) 4.000%",
            "MARKET  (SPAXX) 999 4.000%",
            "SPAXX symbol/yield row is malformed",
            "MARKET (SPAXX)",
        ),
    ],
)
def test_supported_label_spacing_keeps_the_row_specific_refusal(
    row: str, spaced: str, malformed: str, message: str, column: str
) -> None:
    assert _parse(_edit(_statement(), row, spaced)) == _parse(_statement())
    _assert_refuses(_edit(_statement(), row, malformed), message, column)


@pytest.mark.parametrize("year_end", [False, True])
def test_decimal_arithmetic_failure_is_a_fixed_parse_error(year_end: bool) -> None:
    if year_end:
        text = _edit(_YEAR_END, "10.000 $1.0000", "10.000 $100000.0000")
    else:
        text = _edit(
            _statement(),
            "Beginning Account Value $100.00 $100.00",
            "Beginning Account Value $100000.00 $100.00",
        )
    # Lower the exponent limit so the overflow needs only a short synthetic amount.
    with localcontext() as context:
        context.Emax = 4
        _assert_refuses(
            text,
            "statement contains monetary values outside supported arithmetic range",
            year_end=year_end,
            leaks=("100000",),
        )


@pytest.mark.parametrize(
    ("anchor", "header"),
    [("01/15 PAYROLL DEPOSIT $10.00\n", "Deposits"), ("01/20 ACH PAYMENT -$2.00\n", "Withdrawals")],
)
@pytest.mark.parametrize("prefix", ["", "Holdings ", "Activity "])
def test_foreign_registration_is_not_a_wrapped_cash_description(
    anchor: str, header: str, prefix: str
) -> None:
    _assert_refuses(
        _edit(_statement(), anchor, anchor + prefix + "Synthetic Holder Two - INDIVIDUAL - TOD\n"),
        f"{header} section contains a line that is not a supported row",
        leaks=("Synthetic Holder Two",),
    )


def test_wrapped_cash_prose_can_mention_registration_words() -> None:
    anchor = "01/15 PAYROLL DEPOSIT $10.00\n"
    prose = "Synthetic Bank - INDIVIDUAL - TOD record\n"
    assert _parse(_edit(_statement(), anchor, anchor + prose)) == _parse(_statement())


# #1251: a debit-card purchase may be dated in the calendar month before a full
# month's period, on or before its posting date inside the period.
_CARD_MARKER = "SYNTHQCARD"
_CARD_AMOUNT = "6.47"


def _card_statement(dates: str, *, month: int = 1, start: int = 1) -> str:
    """The synthetic statement moved to ``month`` of 2026, its period starting
    on day ``start`` and its 31st rows on the month's last day, whose one card
    row is a SYNTHQCARD purchase of $6.47 with ``dates`` as its Trans. Date and
    Post Date. A period starting after the 10th gets its credit interest on
    its first day, so the interest does not predate the period (#1274)."""
    name = calendar.month_name[month]
    last = calendar.monthrange(2026, month)[1]
    text = (
        _statement()
        .replace("January 1, 2026", f"{name} {start}, 2026")
        .replace("January 31, 2026", f"{name} {last}, 2026")
        .replace("Jan 1, 2026", f"{name[:3]} 1, 2026")
        .replace("Jan 31, 2026", f"{name[:3]} {last}, 2026")
    )
    text = re.sub(r"\b01/31\b", f"01/{last}", text)
    text = re.sub(r"\b01/([0-9]{2})\b", rf"{month:02d}/\1", text)
    if start > 10:
        text = _edit(text, f"{month:02d}/10 CREDIT", f"{month:02d}/{start:02d} CREDIT")
    activity, card = text.split("Debit Card Summary")
    row = f"{month:02d}/21 {month:02d}/22 TEST MERCHANT -$3.00"
    card = _edit(card, row, f"{dates} {_CARD_MARKER} -$3.00").replace("3.00", _CARD_AMOUNT)
    return f"{activity}Debit Card Summary{card}"


def _refusal(message: str) -> ParseError:
    return ParseError(section=_SECTION, row_index=0, column=None, message=message, raw_value="")


def _assert_card_refuses(
    text: str, message: str, *, markers: tuple[str, ...] = (_CARD_MARKER, _CARD_AMOUNT)
) -> None:
    """``text`` refuses with ``message`` complete, no marker (by default the
    card's) anywhere in the refusal or its cause and context, and nothing
    chained."""
    assert all(marker in text for marker in markers)
    with pytest.raises(ParseError) as caught:
        _parse(text)
    assert_unchained_refusal(
        caught.value,
        _refusal(message),
        f"ParseError in section {_SECTION!r} at row 0: {message}",
        markers=markers,
    )


@pytest.mark.parametrize(
    ("month", "start", "dates"),
    [
        pytest.param(1, 1, "01/21 01/22", id="january"),
        pytest.param(3, 1, "03/02 03/02", id="march"),
        pytest.param(7, 1, "07/01 07/01", id="july"),
        pytest.param(1, 15, "01/16 01/16", id="partial-first-month"),
    ],
)
def test_card_statement_variants_parse_with_in_period_dates(
    month: int, start: int, dates: str
) -> None:
    """Controls: each statement the card date tests edit parses with an
    in-period card row, so their refusals come from the card row alone."""
    statement = _parse(_card_statement(dates, month=month, start=start))
    assert statement.period_start == date(2026, month, start)
    assert [item.description for item in statement.debit_card_transactions] == [_CARD_MARKER]


@pytest.mark.parametrize(
    ("month", "dates", "purchase", "post"),
    [
        pytest.param(1, "12/31 01/02", date(2025, 12, 31), date(2026, 1, 2), id="year-end"),
        pytest.param(1, "12/30 01/01", date(2025, 12, 30), date(2026, 1, 1), id="first-day"),
        pytest.param(7, "06/30 07/01", date(2026, 6, 30), date(2026, 7, 1), id="mid-year"),
    ],
)
def test_card_purchase_in_the_previous_month_parses(
    month: int, dates: str, purchase: date, post: date
) -> None:
    """The row refused its whole statement before #1251. The statement parses
    as it does with the purchase moved to the posting date."""
    statement = _parse(_card_statement(dates, month=month))
    assert statement.debit_card_transactions == (
        CmaDebitCardTransaction(purchase, post, _CARD_MARKER, Decimal(f"-{_CARD_AMOUNT}")),
    )
    same_day = _parse(_card_statement(f"{post:%m/%d} {post:%m/%d}", month=month))
    assert replace(statement, debit_card_transactions=()) == replace(
        same_day, debit_card_transactions=()
    )


@pytest.mark.parametrize(
    ("month", "start", "dates"),
    [
        pytest.param(1, 1, "11/30 01/02", id="two-months-back"),
        pytest.param(7, 1, "05/31 07/01", id="two-months-back-mid-year"),
        pytest.param(1, 1, "12/31 12/31", id="posted-before-the-period"),
        pytest.param(1, 1, "01/21 02/01", id="posted-after-the-period"),
        pytest.param(3, 1, "02/30 03/02", id="purchase-not-a-date"),
        pytest.param(1, 1, "01/21 02/30", id="posting-not-a-date"),
        # A partial first month starts on the opening date: its purchase dates
        # stay inside the period.
        pytest.param(1, 15, "01/14 01/16", id="partial-first-month-before-the-period"),
        pytest.param(1, 15, "12/31 01/16", id="partial-first-month-previous-month"),
    ],
)
def test_card_dates_outside_the_rule_still_refuse(month: int, start: int, dates: str) -> None:
    _assert_card_refuses(_card_statement(dates, month=month, start=start), TRANSACTION_DATE_REFUSAL)


@pytest.mark.parametrize(
    ("month", "dates"),
    [
        pytest.param(1, "01/25 01/22", id="january"),
        pytest.param(7, "07/02 07/01", id="a-day-later"),
    ],
)
def test_card_purchase_after_its_posting_refuses(month: int, dates: str) -> None:
    """#1251: this passed before; a purchase can't follow its own posting."""
    _assert_card_refuses(_card_statement(dates, month=month), CARD_ORDER_REFUSAL)


@pytest.mark.parametrize("dates", ["01/25 01/22", "12/31 01/02"])
@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("positive", "positive or zero debit-card rows are unsupported"),
        ("row-total", "debit-card rows do not match their section total"),
        (
            "reinvestment-date",
            "SPAXX dividend, withholding, and reinvestment dates do not reconcile",
        ),
        ("reinvestment-amount", "SPAXX dividend, withholding, and reinvestment do not reconcile"),
        ("dated-line", _DATED),
    ],
)
def test_other_card_refusals_keep_their_existing_message(
    dates: str, fault: str, message: str
) -> None:
    """An independent defect keeps its own complete refusal beside a
    previous-month purchase or an in-period purchase after posting."""
    text = _card_statement(dates)
    row = f"{dates} {_CARD_MARKER} -${_CARD_AMOUNT}\n"
    if fault == "positive":
        text = text.replace(f"-{_CARD_AMOUNT}", _CARD_AMOUNT).replace(
            f"-${_CARD_AMOUNT}", f"${_CARD_AMOUNT}"
        )
    elif fault == "row-total":
        text = _edit(text, row, row * 2)
    elif fault == "reinvestment-date":
        text = _edit(text, "01/31 CASH Reinvestment", "01/30 CASH Reinvestment")
    elif fault == "reinvestment-amount":
        text = _edit(text, "CREDIT INTEREST - - $0.10", "CREDIT INTEREST - - $1.00")
        text = _edit(text, "Dividend Received - - $1.00", "Dividend Received - - $0.10")
    else:
        assert fault == "dated-line"
        text = _edit(text, _DISCLOSURE, "01/25 SYNTHQCARD\n" + _DISCLOSURE)
    _assert_card_refuses(text, message)


def _with_purchase_after_posting(text: str) -> str:
    """January ``text``, one $6.47 card row, with a second one purchased on the
    25th and posted on the 22nd."""
    total = f"{Decimal(_CARD_AMOUNT) * 2:.2f}"
    text = _edit(
        text,
        "Purchases\n",
        f"Purchases\n01/25 01/22 {_CARD_MARKER} -${_CARD_AMOUNT}\n",
    )
    for label in (
        "Total Purchases",
        "Fidelity® Debit Card (*0000) Subtotal",
        "Total Debit Card Activity",
    ):
        text = _edit(text, f"{label} -${_CARD_AMOUNT}", f"{label} -${total}")
    return _edit(text, f"Total Purchases    -{_CARD_AMOUNT}", f"Total Purchases    -{total}")


def test_cross_month_card_beside_a_purchase_after_posting_refuses() -> None:
    """A previous-month purchase doesn't hide another row's purchase after
    its posting."""
    _assert_card_refuses(
        _with_purchase_after_posting(_card_statement("12/31 01/02")), CARD_ORDER_REFUSAL
    )


# #1274: on the account's opening statement (a partial first month or a dash
# beginning value), credit interest and card purchases stay inside the period.
_INTEREST_AMOUNT = "13.57"
_DATED_MARKERS = (_INTEREST_AMOUNT, _CARD_MARKER, _CARD_AMOUNT)


def _dated_statement(
    interest: str,
    card: str | None = None,
    *,
    month: int = 1,
    start: int = 1,
    dash: bool = False,
) -> str:
    """``_card_statement`` with its credit interest a $13.57 row dated
    ``interest`` and its card row dated ``card`` (default: the 21st, posted
    the 22nd); with ``dash``, the opening statement's dash beginning value."""
    text = _card_statement(card or f"{month:02d}/21 {month:02d}/22", month=month, start=start)
    text, rows = re.subn(
        r"^[0-9]{2}/[0-9]{2} CREDIT INTEREST - - \$0\.10$",
        f"{interest} CREDIT INTEREST - - ${_INTEREST_AMOUNT}",
        text,
        flags=re.MULTILINE,
    )
    assert rows == 1
    text = _edit(
        text,
        "Total Dividends, Interest & Other Income $1.10",
        f"Total Dividends, Interest & Other Income ${Decimal(_INTEREST_AMOUNT) + 1:.2f}",
    )
    if dash:
        for old, new in (
            ("Beginning Account Value $100.00 $100.00", "Beginning Account Value - -"),
            ("Additions 10.00 10.00", "Additions 110.00 110.00"),
            ("MONEY $100.00 105.000", "MONEY unavailable 105.000"),
            ("$100.00 $105.00 $4.20\nholdings)", "unavailable $105.00 $4.20\nholdings)"),
        ):
            text = _edit(text, old, new)
    return text


@pytest.mark.parametrize(
    ("month", "start", "dash", "interest", "card"),
    [
        pytest.param(1, 15, False, "01/10", None, id="partial-interest"),
        pytest.param(1, 15, False, "01/01", None, id="partial-interest-on-the-1st"),
        pytest.param(1, 15, False, "01/14", None, id="partial-interest-the-day-before"),
        pytest.param(1, 15, True, "01/14", None, id="partial-dash-interest"),
        pytest.param(7, 15, False, "07/01", None, id="partial-interest-mid-year"),
        pytest.param(7, 15, False, "07/14", None, id="partial-interest-mid-year-day-before"),
        pytest.param(1, 1, True, "12/31", None, id="dash-interest-year-end"),
        pytest.param(4, 1, True, "03/31", None, id="dash-interest-financial-year-end"),
        pytest.param(1, 1, True, "01/10", "12/31 01/02", id="dash-card-year-end"),
        pytest.param(7, 1, True, "07/10", "06/30 07/01", id="dash-card-mid-year"),
    ],
)
def test_interest_or_card_before_the_opening_statement_refuses(
    month: int, start: int, dash: bool, interest: str, card: str | None
) -> None:
    """Each parsed before #1274: the row predates the account."""
    _assert_card_refuses(
        _dated_statement(interest, card, month=month, start=start, dash=dash),
        TRANSACTION_DATE_REFUSAL,
        markers=_DATED_MARKERS,
    )


@pytest.mark.parametrize(
    ("start", "dash", "interest", "card", "credited", "purchased"),
    [
        pytest.param(15, False, "01/15", None, 15, 21, id="partial-opening-day"),
        pytest.param(15, False, "01/20", None, 20, 21, id="partial-later"),
        pytest.param(15, True, "01/15", "01/15 01/16", 15, 15, id="partial-dash-opening-day"),
        pytest.param(1, True, "01/01", "01/01 01/02", 1, 1, id="dash-first-day"),
        pytest.param(1, True, "01/31", "01/31 01/31", 31, 31, id="dash-last-day"),
    ],
)
def test_opening_statement_dates_inside_the_period_parse(
    start: int, dash: bool, interest: str, card: str | None, credited: int, purchased: int
) -> None:
    """Controls: each opening statement the refusals above edit parses with
    its interest and card purchase inside the period."""
    statement = _parse(_dated_statement(interest, card, start=start, dash=dash))
    assert statement.period_start == date(2026, 1, start)
    assert (statement.beginning_account_value_usd is None) is dash
    assert statement.income_credits[0] == CmaIncomeCredit(
        date(2026, 1, credited), CmaIncomeKind.CREDIT_INTEREST, Decimal(_INTEREST_AMOUNT), None
    )
    assert [item.transaction_date for item in statement.debit_card_transactions] == [
        date(2026, 1, purchased)
    ]


@pytest.mark.parametrize(
    ("month", "interest", "card", "previous"),
    [
        pytest.param(1, "12/31", "12/31 01/02", date(2025, 12, 31), id="january"),
        pytest.param(1, "12/01", "12/01 01/02", date(2025, 12, 1), id="january-1st"),
        pytest.param(7, "06/30", "06/30 07/01", date(2026, 6, 30), id="july"),
        pytest.param(4, "03/31", "03/31 04/01", date(2026, 3, 31), id="april"),
    ],
)
def test_carried_beginning_statement_keeps_previous_month_dates(
    month: int, interest: str, card: str, previous: date
) -> None:
    """Controls: a full month with a carried beginning value still reads
    interest and card purchases dated in the month before its period."""
    statement = _parse(_dated_statement(interest, card, month=month))
    assert statement.beginning_account_value_usd == Decimal("100.00")
    assert statement.income_credits[0].date == previous
    assert statement.debit_card_transactions[0].transaction_date == previous


@pytest.mark.parametrize(
    ("start", "dash", "interest"),
    [
        pytest.param(1, False, "11/30", id="carried-two-months-back"),
        pytest.param(1, True, "11/30", id="dash-two-months-back"),
        pytest.param(15, False, "12/31", id="partial-previous-month"),
        pytest.param(15, True, "12/31", id="partial-dash-previous-month"),
    ],
)
def test_interest_outside_the_month_before_still_refuses(
    start: int, dash: bool, interest: str
) -> None:
    """Controls: these refused before #1274, with the same message."""
    _assert_card_refuses(
        _dated_statement(interest, start=start, dash=dash),
        TRANSACTION_DATE_REFUSAL,
        markers=_DATED_MARKERS,
    )


@pytest.mark.parametrize(
    ("start", "dash", "interest", "card"),
    [
        pytest.param(15, False, "01/10", None, id="partial-interest"),
        pytest.param(15, True, "01/10", None, id="partial-dash-interest"),
        pytest.param(1, True, "12/31", None, id="dash-interest"),
        pytest.param(1, True, "01/10", "12/31 01/02", id="dash-card"),
    ],
)
@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("income-total", "income rows do not match their section total"),
        ("withdrawal-sign", "Withdrawals contains a row with the wrong sign"),
        ("purchase-after-posting", CARD_ORDER_REFUSAL),
        (
            "reinvestment-date",
            "SPAXX dividend, withholding, and reinvestment dates do not reconcile",
        ),
        ("dated-line", _DATED),
    ],
)
def test_other_refusals_keep_their_message_beside_a_date_before_the_opening(
    start: int, dash: bool, interest: str, card: str | None, fault: str, message: str
) -> None:
    """An independent defect keeps its own complete refusal beside an opening
    statement's interest or card purchase dated before its period."""
    text = _dated_statement(interest, card, start=start, dash=dash)
    if fault == "income-total":
        label = "Total Dividends, Interest & Other Income"
        text = _edit(
            text,
            f"{label} ${Decimal(_INTEREST_AMOUNT) + 1:.2f}",
            f"{label} ${Decimal(_INTEREST_AMOUNT) + 2:.2f}",
        )
    elif fault == "withdrawal-sign":
        text = _edit(text, "01/20 ACH PAYMENT -$2.00", "01/20 ACH PAYMENT $2.00")
    elif fault == "purchase-after-posting":
        text = _with_purchase_after_posting(text)
    elif fault == "reinvestment-date":
        text = _edit(text, "01/31 CASH Reinvestment", "01/30 CASH Reinvestment")
    else:
        assert fault == "dated-line"
        text = _edit(text, _DISCLOSURE, "01/25 SYNTHQLINE\n" + _DISCLOSURE)
    _assert_card_refuses(text, message, markers=_DATED_MARKERS)


@pytest.mark.parametrize(
    ("fault", "old", "new"),
    [
        ("reinvestment-date", "01/31 CASH Reinvestment", "01/30 CASH Reinvestment"),
        ("dated-line", _DISCLOSURE, "01/25 SYNTHQLINE\n" + _DISCLOSURE),
    ],
)
def test_partial_card_before_the_period_still_refuses_first(fault: str, old: str, new: str) -> None:
    """Controls: a partial month's card purchase before its period refused in
    the card section before #1274, ahead of these later checks; it still does."""
    text = _edit(_dated_statement("01/15", "01/14 01/16", start=15), old, new)
    _assert_card_refuses(text, TRANSACTION_DATE_REFUSAL, markers=_DATED_MARKERS)
