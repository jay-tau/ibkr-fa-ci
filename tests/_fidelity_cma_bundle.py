"""The synthetic Fidelity CMA bundle that the CMA golden tests and several other
test modules run the CMA commands on.

Sixteen monthly statements (January 2025 to April 2026), the 2025 year-end
report and a 2025 Form 1042-S packet, written as placeholder PDFs with
``pdftotext`` stubbed to render each one, and the user config. Beside them: the
``cma_inputs`` fixture, the SPAXX price stub, a folder digest helper, a
finalized 2025 US tax year and the digests of the Schedule FA files a
``fidelity-cma-fa`` run on the bundle writes.

Account and transaction values are synthetic; the texts keep the broker and
fund names and identifiers the parser keys on.
"""

from __future__ import annotations

import calendar
import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.types import Ticker
from tests._fidelity_cma import _ACCOUNT, _HOLDER, cma_config, patch_pdftotext, session_series
from tests._fidelity_cma_1042s import packet_text

_DEPOSIT = Decimal("100.00")
_INTEREST = Decimal("0.10")
_DIVIDEND = Decimal("4.00")
_WITHHOLDING = Decimal("-1.00")
# Months whose dividend has 25% US withholding, and sweep-fund sales by month.
_WITHHELD_MONTHS = frozenset({(2025, 2), (2025, 5), (2025, 8), (2025, 11)})
_SALES = {(2025, 3): Decimal(20), (2025, 6): Decimal(80), (2026, 2): Decimal(30)}
# #1251: a month's debit-card purchase (Trans. Date MM/DD, then Post Date as the
# month's day), funded by a same-day sweep-fund sale.
CARD_MARKER = "SYNTHQCARD"
CARD_AMOUNT = Decimal("-6.47")
CardsByMonth = dict[tuple[int, int], tuple[str, int]]

BUNDLE_CONFIG = cma_config(
    spaxx_isin="US31617H1023", us_tin="123456789", cma_status="BeneficialOwner"
)


@dataclass(frozen=True, slots=True)
class _Month:
    year: int
    month: int

    @property
    def last_day(self) -> int:
        return calendar.monthrange(self.year, self.month)[1]

    @property
    def dividend_day(self) -> int:
        """The month's last weekday."""
        day = self.last_day
        while date(self.year, self.month, day).weekday() >= 5:
            day -= 1
        return day

    def mmdd(self, day: int) -> str:
        return f"{self.month:02d}/{day:02d}"


_MONTHS = tuple(
    _Month(year, month)
    for year, month in [(2025, month) for month in range(1, 13)]
    + [(2026, month) for month in range(1, 5)]
)


def _usd(value: Decimal) -> str:
    return f"-${-value:.2f}" if value < 0 else f"${value:.2f}"


def _section(header: str, columns: str, rows: list[str], total_label: str, total: str) -> str:
    return "\n".join([header, columns, *rows, f"{total_label} {total}"]) + "\n"


def _statement_text(
    month: _Month, beginning: Decimal | None, card: tuple[str, int] | None = None
) -> tuple[str, Decimal]:
    """One monthly statement in the parser's layout, and its ending value.

    Every inflow is swept into SPAXX the same day and every withdrawal or card
    purchase is funded by a same-day sale, so the free credit balance stays zero.
    """
    withholding = _WITHHOLDING if (month.year, month.month) in _WITHHELD_MONTHS else Decimal(0)
    sale = _SALES.get((month.year, month.month))
    core: list[tuple[int, str, Decimal]] = [
        (5, "You Bought", _DEPOSIT),
        (10, "You Bought", _INTEREST),
    ]
    if sale is not None:
        core.append((16, "You Sold", -sale))
    core.append((month.dividend_day, "Reinvestment", _DIVIDEND + withholding))
    if card is not None:
        core.append((card[1], "You Sold", CARD_AMOUNT))
        core.sort(key=lambda row: row[0])

    balance = beginning or Decimal(0)
    core_rows: list[str] = []
    for day, kind, amount in core:
        balance += amount
        core_rows.append(
            f"{month.mmdd(day)} CASH {kind} FIDELITY GOVERNMENT MONEY MARKET "
            f"{amount:.3f} $1.0000 {_usd(amount)} {_usd(balance)}\n"
            + ("REINVEST @ $1.000" if kind == "Reinvestment" else "MORNING TRADE @ 1")
        )
    ending = balance
    additions = _DEPOSIT + _INTEREST + _DIVIDEND
    subtractions = withholding - (sale or 0) + (CARD_AMOUNT if card is not None else 0)

    begin_summary = "-" if beginning is None else _usd(beginning)
    begin_holding = "unavailable" if beginning is None else _usd(beginning)
    income = [
        f"{month.mmdd(10)} CREDIT INTEREST - - {_usd(_INTEREST)}",
        f"{month.mmdd(month.dividend_day)} FIDELITY GOVERNMENT MONEY 31617H102 "
        f"Dividend Received - - {_usd(_DIVIDEND)}\nMARKET",
    ]
    sections = [
        _section(
            "Dividends, Interest & Other Income",
            "(Includes dividend reinvestment)\nSettlement    Symbol/\n"
            "Date Security Name CUSIP Description Quantity Price Amount",
            income,
            "Total Dividends, Interest & Other Income",
            _usd(_INTEREST + _DIVIDEND),
        ),
        _section(
            "Deposits",
            "Date Reference Description Amount",
            [f"{month.mmdd(5)} PAYROLL DEPOSIT {_usd(_DEPOSIT)}"],
            "Total Deposits",
            _usd(_DEPOSIT),
        ),
    ]
    if sale is not None:
        sections.append(
            _section(
                "Withdrawals",
                "Date Reference Description Amount",
                [f"{month.mmdd(16)} ACH PAYMENT {_usd(-sale)}"],
                "Total Withdrawals",
                _usd(-sale),
            )
        )
    if withholding:
        sections.append(
            "Taxes Withheld\nDate Security Description Amount\n"
            f"{month.mmdd(month.dividend_day)} FIDELITY GOVERNMENT MONEY MARKET "
            f"Non-Resident Tax {_usd(withholding)}\n"
            f"Total Federal Taxes Withheld {withholding:.2f}\n"
            f"Total Taxes Withheld {_usd(withholding)}\n"
        )
    sections.append(
        _section(
            "Core Fund Activity",
            "For more information about the operation of your core account, please refer to "
            "your Customer Agreement.\nSettlement    Account\n"
            "Date Type Transaction Description Quantity Price Amount Balance",
            core_rows,
            "Total Core Fund Activity",
            _usd(sum((amount for _day, _kind, amount in core), Decimal(0))),
        )
    )
    if card is not None:
        sections.append(
            "Debit Card Summary    Debit Card Activity\n"
            "Beginning Balance    $0.00    Fidelity® Debit Card (*0000)\n"
            f"Total Purchases    {CARD_AMOUNT}\nTRANSACTIONS\nTotal Payments    0.00\n"
            "Ending Balance    $0.00    Trans. Date Post Date    "
            "Location Reference/Description    Amount\nPurchases\n"
            f"{card[0]} {month.mmdd(card[1])} {CARD_MARKER} {_usd(CARD_AMOUNT)}\n"
            f"Total Purchases {_usd(CARD_AMOUNT)}\n"
            f"Fidelity® Debit Card (*0000) Subtotal {_usd(CARD_AMOUNT)}\n"
            f"Total Debit Card Activity {_usd(CARD_AMOUNT)}\n"
        )
    name = calendar.month_name[month.month]
    text = f"""\
INVESTMENT REPORT
{name} 1, {month.year} - {name} {month.last_day}, {month.year}

FIDELITY® CASH MANAGEMENT ACCOUNT {_HOLDER} -
INDIVIDUAL TOD
Account Number: {_ACCOUNT}
Your Account Value: {_usd(ending)}
Account # {_ACCOUNT}
Account Summary {_HOLDER} - INDIVIDUAL - TOD
Account Value: {_usd(ending)}    Account Holdings
This Period    Year-to-Date
Beginning Account Value {begin_summary} {begin_summary}
Additions {additions:.2f} {additions:.2f}
Subtractions {subtractions:.2f} {subtractions:.2f}
Change in Investment Value * 0.00 0.00
Ending Account Value {_usd(ending)} {_usd(ending)}
Accrued Interest (AI) 0.00
Ending Account Value Incl. AI {_usd(ending)}
Holdings {_HOLDER} - INDIVIDUAL - TOD
Core Account
FIDELITY GOVERNMENT MONEY {begin_holding} {ending:.3f} $1.0000 {_usd(ending)} $4.00
MARKET (SPAXX) 4.000%
Total Core Account (100% of account {_usd(ending)} $4.00
holdings)
Total Holdings {_usd(ending)} $4.00
Activity {_HOLDER} - INDIVIDUAL - TOD
{"".join(sections)}
Brokerage services provided by Fidelity Brokerage Services LLC.
Brokerage accounts carried by National Financial Services LLC.
"""
    return text, ending


def _year_end_text(december_value: Decimal) -> str:
    months = [month for month in _MONTHS if month.year == 2025]
    withheld = sum(
        (_WITHHOLDING for month in months if (month.year, month.month) in _WITHHELD_MONTHS),
        Decimal(0),
    )
    return f"""\
2025 YEAR-END INVESTMENT REPORT
January 1, 2025 - December 31, 2025
FIDELITY® CASH MANAGEMENT ACCOUNT {_HOLDER} -
INDIVIDUAL TOD
Account Number: {_ACCOUNT}
Account # {_ACCOUNT}
Account Summary {_HOLDER} - INDIVIDUAL - TOD
Dividends {_DIVIDEND * len(months):.2f}
Interest {_INTEREST * len(months):.2f}
Taxes Withheld {withheld:.2f}
Holdings
Core Account
Description    Quantity    Per Unit    Market Value    Income Earned
FIDELITY GOVERNMENT MONEY {december_value:.3f} $1.0000 {_usd(december_value)} $4.00
MARKET (SPAXX)
Total Core Account (100% of account {_usd(december_value)} $4.00
holdings)
Total Holdings {_usd(december_value)} $4.00
Brokerage services provided by Fidelity Brokerage Services LLC.
Brokerage accounts carried by National Financial Services LLC.
"""


def _form_1042s_text() -> str:
    # 2025 statement income, summed then rounded to whole dollars: interest
    # 12 x $0.10, unwithheld dividends 8 x $4.00, withheld dividends 4 x $4.00
    # with 4 x $1.00 tax.
    records = (
        ("1234567890", "01", "02", "0.00", 1, 0),
        ("2234567890", "06", "02", "0.00", 32, 0),
        ("3234567890", "06", "00", "25.00", 16, 4),
    )
    return packet_text(records, account=_ACCOUNT, holder=_HOLDER)


def _rendered_pdfs(tmp_path: Path, cards: CardsByMonth) -> tuple[Path, Path, dict[str, str]]:
    """Write placeholder PDFs and return the text ``pdftotext`` renders for each,
    with ``cards`` as each month's card purchase."""
    statements = tmp_path / "statements"
    tax_forms = tmp_path / "tax-forms"
    statements.mkdir()
    tax_forms.mkdir()
    texts: dict[str, str] = {}
    beginning: Decimal | None = None
    december_value = None
    for month in _MONTHS:
        text, beginning = _statement_text(month, beginning, cards.get((month.year, month.month)))
        texts[str(statements / f"{month.year}-{month.month:02d}.pdf")] = text
        if (month.year, month.month) == (2025, 12):
            december_value = beginning
    assert december_value is not None
    texts[str(statements / "2025-year-end.pdf")] = _year_end_text(december_value)
    texts[str(tax_forms / "2025-1042-s.pdf")] = _form_1042s_text()
    for path in texts:
        Path(path).write_bytes(b"%PDF placeholder")
    return statements, tax_forms, texts


def bundle_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cards: CardsByMonth
) -> tuple[Path, Path, Path]:
    """The bundle under ``tmp_path``, ``pdftotext`` stubbed to render it."""
    statements, tax_forms, texts = _rendered_pdfs(tmp_path, cards)
    patch_pdftotext(monkeypatch, texts)
    config = tmp_path / "user.toml"
    config.write_text(BUNDLE_CONFIG, encoding="utf-8")
    return statements, tax_forms, config


@pytest.fixture
def cma_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    return bundle_inputs(tmp_path, monkeypatch, {})


def spaxx_nav(
    ticker: Ticker,
    *,
    start: date,
    end: date,
    now: datetime,
    fetch_symbol: str | None = None,
) -> PriceSeries:
    assert ticker == Ticker("SPAXX")
    assert fetch_symbol is None
    return session_series(ticker, start, end, fetched_at=now, price="1.0000")


def folder_digests(directory: Path) -> dict[str, str]:
    """sha256 of every file under ``directory``, keyed by its relative path."""
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


# A [[fidelity_cma.us_tax_years]] entry that finalizes tax year 2025, with
# eligible tax on the bundle's four withheld dividend dates.
FINALIZED_2025 = """
[[fidelity_cma.us_tax_years]]
tax_year = 2025
refund_status = "finalized"
provenance = "final IRS transcript and CA event allocation"
eligible_tax = [
    { payment_date = 2025-02-28, amount_usd = "1.00" },
    { payment_date = 2025-05-30, amount_usd = "1.00" },
    { payment_date = 2025-08-29, amount_usd = "1.00" },
    { payment_date = 2025-11-28, amount_usd = "0.75" },
]
"""


# sha256 of the two files a review-csv fidelity-cma-fa run for calendar year
# 2025 writes from the bundle, priced by spaxx_nav.
FA_SHA256 = {
    "schedule-fa-a2.csv": "1025007465741efcbdabab07ed5d2a537cec8617f6cfe0e5359f6b0d43bcd82a",
    "schedule-fa.csv": "f21c9bc23c5a059f164977323004819cb5626d2be6e2db59660cc97fecd4c4f2",
}
