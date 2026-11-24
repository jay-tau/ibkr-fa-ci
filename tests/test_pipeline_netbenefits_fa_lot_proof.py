"""``netbenefits-fa`` proves that the Dec-31 purchase is the only ESPP lot held in the
calendar year (#1151).

The lots exports carry no download date, so the run checks the statements and every
confirmation in the bundle. These tests run the real statement and confirmation
parsers on synthetic ``pdftotext`` text; only the shell-out is stubbed. The
CY2025 Dec-31 purchase (``CONF_DEC``) settles 01/05 (*S*), and the settlement bound
ends ten days after Dec-31, on 01/10 (*T*).
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa import cli
from ibkr_fa.exceptions import IbkrFaError, ParseError
from ibkr_fa.pipeline_netbenefits_fa import NetBenefitsFaError
from tests import _netbenefits_statement_pdf as statements
from tests._crash_reports import assert_prints_no_marker
from tests._netbenefits import (
    ALL_REPORTS,
    CLOSED_INTRA_CY_CSV,
    CONF_DEC,
    DATED_LINE_UNREAD,
    OPEN_CSV,
    PURCHASE_ROW_NAME,
    a2_sweep_refusal,
    confirmation,
    core_only_body,
    table_row,
)
from tests._netbenefits import statement_label as _statement_n
from tests._netbenefits_e2e import NB_E2E_PDF_TEXT, nb_e2e_bundle
from tests._netbenefits_fa import BASE_CONFIG
from tests._netbenefits_fa_lot_bundle import LOT_PROOF_CUSIP as _CUSIP
from tests._netbenefits_fa_lot_bundle import LOT_PROOF_DEC_PERIOD as _DEC
from tests._netbenefits_fa_lot_bundle import LOT_PROOF_JAN_PERIOD as _JAN
from tests._netbenefits_fa_lot_bundle import LOT_PROOF_OWN_ROW as _OWN
from tests._netbenefits_fa_lot_bundle import LotProofBundle as _Bundle
from tests._netbenefits_fa_lot_bundle import LotProofRow as _Row
from tests._netbenefits_fa_lot_bundle import lot_proof_bundle as _bundle
from tests._netbenefits_fa_lot_bundle import lot_proof_report as _report
from tests._netbenefits_fa_lot_bundle import run_lot_proof_bundle as _run
from tests._netbenefits_fa_lot_bundle import write_lot_proof_bundle as _write

# A privacy marker on synthetic purchase and stock Holdings rows.
_NAME = PURCHASE_ROW_NAME
_DEC_JAN = "December 1, 2025 - January 31, 2026"


# A later purchase's row: 1.000 share at $100, settled 01/06 unless edited.
_LATER = _Row("01/06", "1.000", "100.00000", "100.00")


# A priced body holding only the Core Account money-market fund: no stock row.
_CORE_ONLY = core_only_body("FID TREASURY ONLY MMKT FUND CL", percent="0")


def _year_end(
    year: int, *rows: _Row, holdings: tuple[tuple[str, str, str], ...] | None = None
) -> str:
    return _report(
        f"January 1, {year} - December 31, {year}",
        *rows,
        holdings=holdings,
        title=f"{year} YEAR-END INVESTMENT REPORT",
    )


def _later(day: int, settles: int, *, shares: str = "1.000", contrib: str = "100.00") -> str:
    """A later purchase on January ``day``, 2026, at $100 a share, settling on
    January ``settles``."""
    fmv = Decimal(contrib) + Decimal("11.11")
    return confirmation(
        purchase=f"JAN/{day:02d}/2026",
        offering=f"NOV/03/2025 - JAN/{day:02d}/2026",
        mv=f"{fmv}",
        contrib=contrib,
        shares=shares,
        price="100.0000",
        gain="11.11",
        trade_settle=f"01-{day:02d}-26 01-{settles:02d}-26",
    )


def _earlier(purchase: str, trade_settle: str) -> str:
    """A purchase of 1 share at $450 before Dec-31."""
    month, _day, year = purchase.split("/")
    return confirmation(
        purchase=purchase,
        offering=f"{month}/01/{year} - {purchase}",
        mv="500.00",
        contrib="450.00",
        shares="1.0000",
        price="450.0000",
        gain="50.00",
        trade_settle=trade_settle,
    )


# A Dec-31 purchase small enough that a 4.001-share row passes #1149's product check
# against 4.0000 shares: 4 x $10 = $40.00, FMV $44.44 ($11.11 a share).
_SMALL_DEC = confirmation(
    purchase="DEC/31/2025",
    offering="NOV/03/2025 - DEC/31/2025",
    mv="44.44",
    contrib="40.00",
    shares="4.0000",
    price="10.0000",
    gain="4.44",
)
_SMALL_OPEN_CSV = OPEN_CSV.replace(
    "Dec-31-2025,3.4820,668.96,192.12,626.24,-42.72,", "Dec-31-2025,4.0000,40.00,10.00,44.44,4.44,"
)
_SMALL_CONFIG = BASE_CONFIG.replace(
    'price_per_share_usd = "213.47"', 'price_per_share_usd = "11.11"'
)


def _bought(number: int, row: int, column: str) -> str:
    table = "Securities Bought & Sold"
    return f"{_statement_n(number)}, row {row} of its {table} table, {column} column: "


def _unexplained(number: int, row: int) -> str:
    return _bought(number, row, "Settlement Date") + (
        "an ESPP purchase settling within ten days after Dec-31 or earlier is neither the Dec-31 "
        "purchase nor a later purchase with a confirmation of its own, so a lot may have been held "
        "during CY2025; netbenefits-fa supports only lots bought on Dec-31"
    )


def _held(number: int) -> str:
    return (
        f"{_statement_n(number)}, row 1 of its Holdings table, Quantity column: the shares held at "
        "the period end aren't explained by the Dec-31 purchase and the confirmed later "
        "purchases, so netbenefits-fa can't prove no lot was held during CY2025; it supports "
        "only lots bought on Dec-31"
    )


def _other_security(number: int, row: int) -> str:
    return (
        f"{_statement_n(number)}, row {row} of its Holdings table, Description column: a holding "
        "of a security other than netbenefits.security.symbol isn't explained by the matched "
        "purchases"
    )


def _overlap(first: int, second: int) -> str:
    return (
        f"statements {first} and {second} ({ALL_REPORTS}) list "
        "different ESPP purchase rows for the days they share"
    )


_EARLIER = (
    "confirmation 1 (by purchase date), purchase date: dated before Dec-31, so a lot was "
    "bought before or during CY2025; netbenefits-fa supports only an account whose lots were "
    "all bought on Dec-31"
)
_BOUND = (
    "confirmation 1 (by purchase date), settlement date: the Dec-31 purchase settles more than "
    "ten days after Dec-31, so the statements can't be relied on to show every purchase made "
    "on or before Dec-31"
)
_COVERAGE = (
    "the Stock Plan Services reports don't cover the window from the later of Jan-1 and "
    "netbenefits.account.account_opening_date through ten days after Dec-31, so they can't "
    "show every purchase made on or before Dec-31; supply the missing report"
)
_NO_OWN_ROW = (
    "no Stock Plan Services report shows the Dec-31 purchase's row with its confirmation's "
    "settlement date, identifier, price, quantity and amount; the purchase can't be tied to "
    "the statements"
)
_AMBIGUOUS = (
    "an ESPP purchase matches more than one confirmation in every field, so it can't be tied "
    "to one purchase"
)
_NO_TABLE = (
    " prints no Holdings table, so the shares held at its period end can't be tied to the "
    "matched purchases settled by then; the whole statement was checked"
)
_NO_ROW = (
    " lists no holding of netbenefits.security.symbol, though matched purchases had settled by "
    "its period end; the whole statement was checked"
)


# #1249: privacy markers on income rows: the security name, the day and the balanced
# amounts (9.87 gross - 2.47 withheld = 7.40 swept).
_INCOME_MARKERS = ("SYNTHQYEDIV", "12/19", "9.87", "2.47", "7.40")


def _income(*, withheld: bool = True) -> str:
    """The income sections of a 12/19 dividend, its withholding unless not
    ``withheld``, and the sweep of the net amount."""
    swept = "7.40" if withheld else "9.87"
    withholding = (
        "Taxes Withheld\n"
        "Date Security Description Amount\n"
        "12/19 SYNTHQYEDIV Non-Resident Tax -$2.47\n"
        "Total Federal Taxes Withheld -2.47\n"
        "Total Taxes Withheld -$2.47\n"
    )
    return (
        "Dividends, Interest & Other Income\n"
        "(Includes dividend reinvestment)\n"
        "Date Security Name CUSIP Description Quantity Price Amount\n"
        f"12/19 SYNTHQYEDIV {_CUSIP} Dividend Received - - $9.87\n"
        "Total Dividends, Interest & Other Income $9.87\n"
        + (withholding if withheld else "")
        + "Core Fund Activity\n"
        "Date Type Transaction Description Quantity Price Amount Balance\n"
        f"12/19 CASH You Bought FID TREASURY ONLY MMKT FUND CL OUS {swept}0 $1.0000 ${swept} "
        f"${swept}\n"
        f"Total Core Fund Activity ${swept}\n"
    )


def _year_end_income(*, withheld: bool = True) -> str:
    """The 2025 year-end report, unpriced, printing :func:`_income`."""
    return _report(
        "January 1, 2025 - December 31, 2025",
        body=statements.UNPRICED_BODY + _income(withheld=withheld),
        title="2025 YEAR-END INVESTMENT REPORT",
    )


def _sweep_refusal(number: int) -> str:
    """The existing refusal for the sweep at row 1 of statement ``number``."""
    return a2_sweep_refusal(table_row(number, 1, "Core Fund Activity"))


def _assert_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bundle: _Bundle,
    message: str,
    markers: tuple[str, ...],
    error: type[IbkrFaError] = NetBenefitsFaError,
) -> IbkrFaError:
    """``bundle`` refuses with ``message`` through ``run_netbenefits_fa`` and the CLI,
    writing no output, and neither the refusal nor stderr prints a marker of the
    edited text. Returns the refusal ``run_netbenefits_fa`` raised."""
    args = _write(tmp_path, monkeypatch, bundle)
    with pytest.raises(error) as caught:
        _run(tmp_path)
    assert str(caught.value) == message
    assert list((tmp_path / "out").iterdir()) == []
    assert cli.main(args) == 4
    err = capsys.readouterr().err
    assert err == f"error: {message}\n"
    assert list((tmp_path / "out").iterdir()) == []
    edited = "".join((*bundle.reports.values(), *bundle.confirmations.values()))
    for marker in markers:
        assert marker in edited, marker
        assert marker.casefold() not in err.casefold(), marker
    return caught.value


# Every refusal the lot proof adds, each with the markers of its edited lines.
_REFUSALS = [
    pytest.param(
        _bundle(
            _report(
                "June 1, 2025 - December 31, 2025", _Row("06/30", "2.000", "150.00000", "300.00")
            ),
            _report(_JAN, _OWN),
            config=BASE_CONFIG.replace(
                "account_opening_date = 2025-12-31", "account_opening_date = 2025-06-01"
            ),
        ),
        _unexplained(1, 1),
        ("06/30", "2.000", "150.00000", "300.00", _NAME, _CUSIP),
        id="mid-year-row",
    ),
    pytest.param(
        _bundle(
            _report("June 1, 2025 - June 30, 2025", _Row("06/27", "2.000", "150.00000", "300.00")),
            _report(_DEC),
            _report(_JAN, _OWN),
        ),
        _unexplained(1, 1),
        ("06/27", "2.000", "150.00000", "300.00", _NAME),
        id="row-before-the-opening-date",
    ),
    pytest.param(
        _bundle(_report(_DEC), _report(_JAN, _OWN, _Row("01/07", "1.250", "80.00000", "100.00"))),
        _unexplained(2, 2),
        ("01/07", "1.250", "80.00000", "100.00"),
        id="unconfirmed-row-after-s",
    ),
    pytest.param(
        _bundle(_report(_DEC), _report(_JAN, _OWN, _Row("01/10", "1.000", "100.00000", "100.00"))),
        _unexplained(2, 2),
        ("01/10", "1.000", "100.00000", "100.00"),
        id="unconfirmed-row-settling-on-the-bound",
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report(_JAN, _Row("01/12")),
            dec31=CONF_DEC.replace("12-31-25 01-05-26", "12-31-25 01-12-26"),
        ),
        _BOUND,
        ("01-12-26",),
        id="dec31-settles-after-t",
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report(_JAN, _OWN),
            confirmations=(_earlier("JUN/30/2025", "06-30-25 07-01-25"),),
        ),
        _EARLIER,
        ("JUN/30/2025", "06-30-25", "07-01-25"),
        id="earlier-confirmation-in-the-cy",
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report(_JAN, _OWN),
            confirmations=(_earlier("MAR/29/2024", "03-29-24 04-02-24"),),
        ),
        _EARLIER,
        ("MAR/29/2024", "03-29-24", "04-02-24"),
        id="earlier-confirmation-in-an-earlier-year",
    ),
    pytest.param(
        _bundle(_report(_DEC)),
        _COVERAGE,
        ("December 1, 2025",),
        id="reports-stop-at-dec31",
    ),
    pytest.param(
        _bundle(_report(_DEC), _report("January 1, 2026 - January 5, 2026", _OWN)),
        _COVERAGE,
        ("January 5, 2026", "01/05"),
        id="reports-stop-at-s",
    ),
    pytest.param(
        _bundle(_report(_DEC), _report(_JAN)),
        _NO_OWN_ROW,
        ("January 31, 2026",),
        id="no-own-row",
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report(_JAN, _Row("01/05", "4.001", "10.00000", "40.00")),
            dec31=_SMALL_DEC,
            open_csv=_SMALL_OPEN_CSV,
            config=_SMALL_CONFIG,
        ),
        _bought(2, 1, "Quantity")
        + "an ESPP purchase's quantity differs from its confirmation's shares",
        ("4.001", "10.00000", "40.00", _NAME, _CUSIP),
        id="own-row-quantity",
    ),
    pytest.param(
        _bundle(_report(_DEC), _report(_JAN, _OWN._replace(amount="668.97"))),
        _bought(2, 1, "Amount")
        + "an ESPP purchase's amount differs from its confirmation's contribution",
        ("668.97", "3.482", "01/05"),
        id="own-row-amount",
    ),
    pytest.param(
        _bundle(_report(_DEC), _report(_JAN, _OWN._replace(identifier="99997Q104"))),
        _bought(2, 1, "Symbol/CUSIP")
        + "an ESPP purchase is for a security other than netbenefits.security.cusip",
        ("99997Q104", "01/05", "3.482", "668.96"),
        id="own-row-identifier",
    ),
    pytest.param(
        _bundle(_report(_DEC), _report(_JAN, _OWN._replace(identifier="99997Q104"), _OWN)),
        _bought(2, 1, "Symbol/CUSIP")
        + "an ESPP purchase is for a security other than netbenefits.security.cusip",
        ("99997Q104", "01/05"),
        id="stand-in-for-the-own-row",
    ),
    pytest.param(
        _bundle(_report(_DEC), _report(_JAN, _OWN, _OWN._replace(price="192.12004"))),
        _unexplained(2, 2),
        ("192.12004", "01/05", "3.482", "668.96"),
        id="two-distinct-own-rows",
    ),
    pytest.param(
        _bundle(_report(_DEC), _report(_JAN, _OWN, _OWN)),
        _unexplained(2, 2),
        ("01/05", "3.482", "192.12000", "668.96"),
        id="duplicated-own-row",
    ),
    pytest.param(
        _bundle(_report(_DEC_JAN, _OWN), _report(_JAN)),
        _overlap(1, 2),
        ("01/05", "3.482"),
        id="overlapping-reports-disagree",
    ),
    pytest.param(
        _bundle(_report(_DEC), _report(_JAN, _OWN, _LATER, _LATER), confirmations=(_later(2, 6),)),
        _unexplained(2, 3),
        ("01/06", "1.000", "100.00000", "100.00"),
        id="one-to-one",
    ),
    pytest.param(
        _bundle(
            _report(_DEC_JAN, _OWN, _LATER, _LATER),
            _report(_JAN, _OWN, _LATER),
            confirmations=(_later(2, 6),),
        ),
        _overlap(1, 2),
        ("01/06", "1.000", "100.00000"),
        id="multiplicity-across-overlapping-reports",
    ),
    pytest.param(
        _bundle(
            _report(
                "November 1, 2025 - November 30, 2025", holdings=(("XESP", "2.000", "150.0000"),)
            ),
            _report(_DEC),
            _report(_JAN, _OWN),
        ),
        _held(1),
        ("2.000", "150.0000", "300.00", _NAME),
        id="holding-before-dec31",
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report(
                "January 1, 2026 - January 3, 2026",
                _LATER._replace(day="01/02"),
                holdings=(("XESP", "1.750", "100.0000"),),
            ),
            _report("January 4, 2026 - January 31, 2026", _OWN),
            confirmations=(_later(2, 2),),
        ),
        _held(2),
        ("1.750", "175.00", "01/02"),
        id="holding-before-s-exceeds-the-later-purchases",
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report(
                "January 1, 2026 - January 7, 2026", _OWN, holdings=(("XESP", "4.482", "192.1200"),)
            ),
            _report("January 8, 2026 - January 31, 2026"),
        ),
        _held(2),
        ("4.482", "861.08", "01/05"),
        id="holding-between-s-and-t-exceeds-the-matched-purchases",
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report(
                "January 1, 2026 - January 7, 2026",
                _OWN,
                holdings=(("XESP", "3.482", "192.1200"), ("XALT", "1.500", "50.0000")),
            ),
            _report("January 8, 2026 - January 31, 2026"),
        ),
        _other_security(2, 2),
        ("XALT", "1.500", "75.00"),
        id="holding-between-s-and-t-of-another-security",
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report(
                "January 1, 2026 - January 4, 2026",
                _Row("01/02", "1.000", "100.00000", "100.04"),
                _Row("01/03", "1.000", "100.00000", "100.04"),
                holdings=(("XESP", "2.002", "100.0000"),),
            ),
            _report("January 5, 2026 - January 31, 2026", _OWN),
            confirmations=(
                _later(2, 2, shares="1.0004", contrib="100.04"),
                _later(3, 3, shares="1.0004", contrib="100.04"),
            ),
        ),
        _held(2),
        ("2.002", "200.20", "100.04"),
        id="holdings-beyond-rounding",
    ),
    pytest.param(
        _bundle(
            _report(_DEC), _report(_JAN, _OWN, _LATER), confirmations=(_later(2, 6), _later(5, 6))
        ),
        _bought(2, 2, "Settlement Date") + _AMBIGUOUS,
        ("01/06", "1.000", "100.00000", "100.00"),
        id="ambiguous-match",
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report(
                "January 1, 2026 - January 3, 2026",
                _LATER._replace(day="01/02"),
                holdings=(("XALT", "1.000", "100.0000"),),
            ),
            _report("January 4, 2026 - January 31, 2026", _OWN),
            confirmations=(_later(2, 2),),
        ),
        _other_security(2, 1),
        ("XALT", "01/02"),
        id="another-security-in-the-later-purchases-quantity",
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report("January 1, 2026 - January 3, 2026", _LATER._replace(day="01/02")),
            _report("January 4, 2026 - January 31, 2026", _OWN),
            confirmations=(_later(2, 2),),
        ),
        _statement_n(2) + _NO_TABLE,
        ("January 3, 2026", "01/02"),
        id="unpriced-report-before-s-after-a-later-purchase",
    ),
    pytest.param(
        _bundle(
            _year_end(2025, _Row("06/30", "2.000", "150.00000", "300.00")),
            _report(_DEC),
            _report(_JAN, _OWN),
        ),
        _unexplained(1, 1),
        ("06/30", "2.000", "150.00000", "300.00"),
        id="year-end-report-mid-year-row",
    ),
    pytest.param(
        _bundle(
            _year_end(2024, holdings=(("XESP", "2.000", "150.0000"),)),
            _report(_DEC),
            _report(_JAN, _OWN),
        ),
        _held(1),
        ("2.000", "150.0000", "300.00"),
        id="earlier-year-end-report-holding",
    ),
    pytest.param(
        _bundle(_report(_DEC), _report(_JAN, _OWN._replace(price="192.12010"))),
        _bought(2, 1, "Price")
        + "an ESPP purchase's price differs from its confirmation's purchase price",
        ("192.12010", "01/05", "3.482", "668.96"),
        id="price-beyond-the-coarser-precision",
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report(
                "January 1, 2026 - January 3, 2026", _LATER._replace(day="01/02"), body=_CORE_ONLY
            ),
            _report("January 4, 2026 - January 31, 2026", _OWN),
            confirmations=(_later(2, 2),),
        ),
        _statement_n(2) + _NO_ROW,
        ("01/02", "1.090"),
        id="core-only-report-before-s-after-a-later-purchase",
    ),
    pytest.param(
        _bundle(
            _year_end(2025, _Row("12/15", "1.000", "100.00000", "100.00")),
            _report(_DEC),
            _report(_JAN, _OWN),
        ),
        _overlap(1, 2),
        ("12/15", "100.00000"),
        id="year-end-row-the-overlapping-report-leaves-out",
    ),
    pytest.param(
        _bundle(
            _report("December 1, 2024 - January 31, 2025"),
            _year_end(2025, _Row("01/20", "1.000", "100.00000", "100.00")),
            _report(_DEC),
            _report(_JAN, _OWN),
        ),
        _overlap(1, 2),
        ("01/20", "100.00000"),
        id="year-end-row-an-earlier-overlapping-report-leaves-out",
    ),
    pytest.param(
        _bundle(
            _report("December 1, 2024 - December 31, 2024"),
            _year_end(2025),
            _report(_DEC),
            _report(_JAN, _OWN, _Row("01/07", "1.250", "80.00000", "100.00")),
        ),
        _unexplained(4, 2),
        ("01/07", "1.250", "80.00000"),
        id="numbering-with-an-interleaved-year-end-report",
    ),
]


@pytest.mark.parametrize(("bundle", "message", "markers"), _REFUSALS)
def test_lot_proof_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bundle: _Bundle,
    message: str,
    markers: tuple[str, ...],
) -> None:
    _assert_refuses(tmp_path, monkeypatch, capsys, bundle, message, markers)


# A sale row behind a footnote marker, dated before T: #1149's parser refuses it, so
# netbenefits-fa does too (item 9's disposal rule, pinned for #1150).
_MARKED_SALE = "t 01/07 SYNTHETIC HOLDER TWO 99997Q104 You Sold -1.000 $400.00000 - $400.00\n"


def test_sale_row_dated_before_t_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(_report(_DEC), _report(_JAN, _OWN) + _MARKED_SALE)
    _assert_refuses(
        tmp_path,
        monkeypatch,
        capsys,
        bundle,
        f"ParseError in section 'statement b.pdf' at row 0: {DATED_LINE_UNREAD}",
        ("01/07", "99997Q104", "400.00", "1.000", _NAME),
        error=ParseError,
    )


_BOUNDARIES = [
    pytest.param(_bundle(_report(_DEC), _report(_JAN, _OWN)), id="supported-case"),
    pytest.param(
        _bundle(
            _report(_DEC_JAN, _Row("12/31")),
            dec31=CONF_DEC.replace("12-31-25 01-05-26", "12-31-25 12-31-25"),
        ),
        id="own-row-settles-on-dec31",
    ),
    pytest.param(
        _bundle(_report(_DEC_JAN, _OWN), _report(_JAN, _OWN)),
        id="own-row-in-two-overlapping-reports",
    ),
    pytest.param(
        _bundle(_year_end(2025), _report(_DEC_JAN, _OWN)), id="silent-unpriced-year-end-report"
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report(
                "January 1, 2026 - January 4, 2026",
                _Row("01/02", "1.000", "100.00000", "100.04"),
                _Row("01/03", "1.000", "100.00000", "100.04"),
                holdings=(("XESP", "2.001", "100.0000"),),
            ),
            _report("January 5, 2026 - January 31, 2026", _OWN),
            confirmations=(
                _later(2, 2, shares="1.0004", contrib="100.04"),
                _later(3, 3, shares="1.0004", contrib="100.04"),
            ),
        ),
        id="holdings-within-rounding",
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report(_JAN, _LATER._replace(day="01/05"), _OWN),
            confirmations=(_later(2, 5),),
        ),
        id="shared-settlement-date",
    ),
    pytest.param(
        _bundle(_report(_DEC), _report(_JAN, _OWN, _LATER), confirmations=(_later(2, 6),)),
        id="later-purchase-row",
    ),
    pytest.param(
        _bundle(_report(_DEC), _report(_JAN, _OWN, _Row("01/15", "1.250", "80.00000", "100.00"))),
        id="row-settling-after-t",
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report(
                "January 1, 2026 - January 3, 2026",
                _LATER._replace(day="01/02"),
                holdings=(("XESP", "1.000", "100.0000"),),
            ),
            _report("January 4, 2026 - January 31, 2026", _OWN),
            confirmations=(_later(2, 2),),
        ),
        id="later-purchase-held-alone-before-s",
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report(
                "January 1, 2026 - January 5, 2026", _OWN, holdings=(("XESP", "3.482", "192.1200"),)
            ),
            _report("January 6, 2026 - January 31, 2026"),
        ),
        id="report-ending-on-s-holds-the-dec31-lot",
    ),
    pytest.param(
        _bundle(_year_end(2025), _year_end(2025), _report(_DEC), _report(_JAN, _OWN)),
        id="two-copies-of-a-silent-year-end-report",
    ),
    pytest.param(
        _bundle(
            _report(_DEC),
            _report("January 1, 2026 - January 3, 2026", body=_CORE_ONLY),
            _report("January 4, 2026 - January 31, 2026", _OWN),
        ),
        id="core-only-report-before-any-purchase-settles",
    ),
]


@pytest.mark.parametrize("bundle", _BOUNDARIES)
def test_lot_proof_boundary_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bundle: _Bundle
) -> None:
    _write(tmp_path, monkeypatch, bundle)
    _run(tmp_path)
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == ["a3-a2.csv", "a3.csv"]


# A mid-year lot and its confirmation: the resolver refuses the lot before the proof.
_MID_YEAR_OPEN_CSV = OPEN_CSV.replace(
    "Dec-31-2025,",
    "Jun-30-2025,1.0000,450.00,450.00,500.00,50.00,-,-,Apr-01-2025,SP,Short\nDec-31-2025,",
)


@pytest.mark.parametrize(
    ("bundle", "message"),
    [
        pytest.param(
            _bundle(
                _report("December 1, 2025 - December 15, 2025"),
                _report(_JAN, _OWN, _Row("01/07", "1.250", "80.00000", "100.00")),
                config=BASE_CONFIG.replace(
                    "account_opening_date = 2025-12-31", "account_opening_date = 2025-12-01"
                ),
            ),
            _COVERAGE,
            id="a2-coverage-gap-meets-the-lot-proof-coverage-first",
        ),
        pytest.param(
            _bundle(
                _year_end_income(),
                _report("December 1, 2025 - December 15, 2025"),
                _report(_JAN, _OWN),
                config=BASE_CONFIG.replace(
                    "account_opening_date = 2025-12-31", "account_opening_date = 2025-12-01"
                ),
            ),
            _COVERAGE,
            id="coverage-gap-meets-year-end-income-first",
        ),
        pytest.param(
            _bundle(
                _year_end(2025, holdings=(("XESP", "2.000", "150.0000"),)),
                _report(_DEC),
                _report(_JAN, _OWN),
            ),
            _held(1),
            id="priced-year-end-report-meets-the-holdings-check-first",
        ),
        pytest.param(
            _bundle(
                _report(_DEC),
                _report(_JAN, _OWN),
                confirmations=(_earlier("JUN/30/2025", "06-30-25 07-01-25"),),
                open_csv=_MID_YEAR_OPEN_CSV,
            ),
            "open-lots data row 2: confirmation 1 (by purchase date) is not a Dec-31 purchase, "
            "so the lot is held across part of CY2025; its in-CY dividend can't be proven here "
            "— wire dividends via #I10",
            id="non-dec31-lot-refuses-before-an-earlier-confirmation",
        ),
        pytest.param(
            _bundle(_report(_DEC), _report(_JAN), closed_csv=CLOSED_INTRA_CY_CSV),
            "closed-lots data row 1 overlaps CY2025 but is not a Dec-31-held gap-window lot "
            "(acquired on Dec-31 and sold after it); NetBenefits intra-CY closed-lot Schedule FA "
            "A3 disclosure is not yet supported (see #I13) — refusing rather than emit an A3 that "
            "omits a disclosable holding",
            id="intra-cy-disposal-refuses-before-a-missing-own-row",
        ),
    ],
)
def test_inputs_that_already_refuse_name_the_first_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bundle: _Bundle,
    message: str,
) -> None:
    _assert_refuses(tmp_path, monkeypatch, capsys, bundle, message, ())


@pytest.mark.parametrize(
    ("withheld", "markers"),
    [
        pytest.param(True, _INCOME_MARKERS, id="dividend-withholding-and-sweep"),
        pytest.param(False, ("SYNTHQYEDIV", "12/19", "9.87"), id="dividend-and-sweep"),
    ],
)
def test_year_end_income_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    withheld: bool,
    markers: tuple[str, ...],
) -> None:
    """#1249: a year-end report's silence proves nothing, but an income row it prints
    is evidence. Its sweep refuses with the Stock Plan Services report's refusal,
    naming the year-end report, which starts first."""
    bundle = _bundle(_year_end_income(withheld=withheld), _report(_DEC_JAN, _OWN))
    refusal = _assert_refuses(tmp_path, monkeypatch, capsys, bundle, _sweep_refusal(1), markers)
    assert_prints_no_marker(refusal, markers=markers)


def test_income_on_a_stock_plan_services_report_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pin (#1249): the same rows on the Stock Plan Services report refuse with the
    existing sweep refusal, which names that report."""
    body = statements.UNPRICED_BODY + statements.period_summaries("9.87", "-2.47") + _income()
    bundle = _bundle(_year_end(2025), _report(_DEC_JAN, _OWN, body=body))
    refusal = _assert_refuses(
        tmp_path, monkeypatch, capsys, bundle, _sweep_refusal(2), _INCOME_MARKERS
    )
    assert_prints_no_marker(refusal, markers=_INCOME_MARKERS)


def test_new_refusal_removes_stale_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lot-proof refusal removes an earlier run's outputs through the preclear."""
    args = _write(tmp_path, monkeypatch, _bundle(_report(_DEC), _report(_JAN)))
    out = tmp_path / "out"
    stale = ("a3.csv", "a3-a2.csv", "a3-quicko.xlsx")
    for name in stale:
        (out / name).write_text("stale\n", encoding="utf-8")
    assert cli.main(args) == 4
    assert capsys.readouterr().err == f"error: {_NO_OWN_ROW}\n"
    assert list(out.iterdir()) == []


def test_netbenefits_all_fa_leg_refusal_removes_stale_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Through netbenefits-all, the FA leg's lot-proof refusal removes the earlier FA
    outputs too: the e2e bundle's January report loses the Dec-31 purchase's row."""
    bundle, config = nb_e2e_bundle(tmp_path, monkeypatch)
    january = NB_E2E_PDF_TEXT["statement-jan.pdf"]
    start = january.index("Securities Bought & Sold\n")
    end = january.index("i   Purchase of stock through your company stock plan.\n")
    edited = (
        january[:start]
        + january[end + len("i   Purchase of stock through your company stock plan.\n") :]
    )
    monkeypatch.setitem(NB_E2E_PDF_TEXT, "statement-jan.pdf", edited)
    out = tmp_path / "out"
    out.mkdir()
    stale = ("schedule-fa-a3.csv", "schedule-fa-a2.csv", "schedule-fa-quicko.xlsx")
    for name in stale:
        (out / name).write_text("stale\n", encoding="utf-8")
    rc = cli.main(
        [
            "netbenefits-all",
            *("--input", str(bundle), "--config", str(config), "--output-dir", str(out)),
            *("--financial-year", "2025-26", "--itr-form", "ITR-2", "--no-fetch-prices"),
        ]
    )
    assert rc == 4
    err = capsys.readouterr().err
    assert err.startswith(f"error: {_NO_OWN_ROW}\n")
    assert "  ERROR        netbenefits-fa\n" in err
    assert list(out.iterdir()) == []


def test_input_help_says_the_statements_are_read() -> None:
    [commands] = [
        action.choices
        for action in cli._build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    [option] = [a for a in commands["netbenefits-fa"]._actions if "--input" in a.option_strings]
    assert option.help == (
        "The NetBenefits bundle DIRECTORY: exactly one open-lots CSV, exactly one closed-lots "
        "CSV (may be header-only), one trade-confirmation PDF per ESPP purchase, and the "
        "statement PDFs. The statements are read: the Stock Plan Services reports must cover "
        "through ten days after Dec-31."
    )
