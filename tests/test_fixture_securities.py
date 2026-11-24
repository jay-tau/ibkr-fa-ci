"""Every security identifier in the broker test fixtures is synthetic (#1032).

The IBKR fixtures (``tests/fixtures/e2e_*.csv`` and the pinned lots in
``tests/fixtures/lot_matching/``) and the NetBenefits fixtures stand in for
real statements, so every security in them is made up:

* a synthetic CUSIP has a valid check digit and an issuer number (its first
  six characters) in the range CUSIP Global Services reserves for private use,
  990000-999999 or 99000A-99999Z, which no listed security has;
* a synthetic ISIN is a country code, a synthetic CUSIP and a valid ISO 6166
  check digit. For a security that a fixture names by both, the ISIN embeds
  that CUSIP, as a US ISIN does.

Distinct securities in one IBKR fixture keep distinct tickers, ISINs, CUSIPs
and descriptions. A real identifier is allowed only when ``src`` keys on it; the
allow-list names each with its reason. ``test_unit_test_securities`` checks
every file under ``tests/``, unit tests included.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from ibkr_fa.reference.fidelity_core_funds import FYIXX_CUSIP, FYIXX_ISIN
from ibkr_fa.types import cusip_check_digit, isin_check_digit

_FIXTURES = Path(__file__).parent / "fixtures"
_IBKR_STATEMENTS = sorted(_FIXTURES.glob("e2e_*.csv"))
_LOT_PINS = sorted((_FIXTURES / "lot_matching").glob("*.json"))
_NETBENEFITS = sorted(path for path in (_FIXTURES / "netbenefits").iterdir() if path.is_file())

# The CUSIP issuer numbers reserved for private use: 990000-999999 and
# 99000A-99999Z.
_SYNTHETIC_ISSUER = re.compile(r"99[0-9]{3}[0-9A-Z]")

# Real identifiers a fixture may hold because src keys on them, with the reason.
_CORE_FUND = "Fidelity's core money-market fund: ibkr_fa.reference.fidelity_core_funds keys on it"
_ALLOWED = {FYIXX_CUSIP: _CORE_FUND, FYIXX_ISIN: _CORE_FUND}

_ISIN = re.compile(r"[A-Z]{2}[0-9A-Z]{9}[0-9]")
_CUSIP = re.compile(r"[0-9A-Z]{8}[0-9]")
_ISIN_TOKEN = re.compile(r"(?<![0-9A-Za-z])[A-Z]{2}[0-9A-Z]{9}[0-9](?![0-9A-Za-z])")
_CUSIP_TOKEN = re.compile(r"(?<![0-9A-Za-z])[0-9A-Z]{8}[0-9](?![0-9A-Za-z])")
# ``TICKER(ISIN)`` at the start of a Dividends or Withholding Tax description.
_PAYMENT = re.compile(r"(?<![0-9A-Za-z])([0-9A-Z.]+)\(([0-9A-Z]+)\)")


@dataclass(frozen=True, slots=True)
class _Security:
    isin: str
    ticker: str
    description: str | None = None
    cusip: str | None = None


def _cusip_problems(cusip: str) -> list[str]:
    if not _CUSIP.fullmatch(cusip):
        return [f"{cusip!r} is not a CUSIP"]
    if cusip_check_digit(cusip[:8]) != cusip[8]:
        return [f"CUSIP {cusip} has a bad check digit"]
    if cusip in _ALLOWED:
        return []
    if not _SYNTHETIC_ISSUER.fullmatch(cusip[:6]):
        return [f"CUSIP {cusip} is outside the private-use issuer range"]
    return []


def _isin_problems(isin: str) -> list[str]:
    if not _ISIN.fullmatch(isin):
        return [f"{isin!r} is not an ISIN"]
    if isin_check_digit(isin[:11]) != isin[11]:
        return [f"ISIN {isin} has a bad check digit"]
    if isin in _ALLOWED:
        return []
    if _cusip_problems(isin[2:11]) or not _SYNTHETIC_ISSUER.fullmatch(isin[2:8]):
        return [f"ISIN {isin} does not embed a synthetic CUSIP"]
    return []


def _identifier_problems(text: str) -> list[str]:
    """Problems with every ISIN- and CUSIP-shaped token in ``text``."""
    # Account and participant fields can have the same shape as a CUSIP.
    text = re.sub(
        r"(Participant Number:\s*|PARTICIPANT NO\.\s*|PARTICIPANT ID\.[^\n]*\n\s*)[0-9A-Z]+",
        r"\1",
        text,
    )
    masked = io.StringIO()
    writer = csv.writer(masked)
    account_column: int | None = None
    for row in csv.reader(io.StringIO(text)):
        if not row:
            account_column = None
        elif row[0] in _FLEX_RECORDS:
            row[1] = ""
        elif "ClientAccountID" in row:
            account_column = row.index("ClientAccountID")
        elif account_column is not None:
            row[account_column] = ""
        elif row[:3] == ["Account Information", "Data", "Account"]:
            row[3] = ""
        writer.writerow(row)
    text = masked.getvalue()
    isins = sorted(set(_ISIN_TOKEN.findall(text)))
    cusips = sorted(set(_CUSIP_TOKEN.findall(text)))
    return [p for isin in isins for p in _isin_problems(isin)] + [
        p for cusip in cusips for p in _cusip_problems(cusip)
    ]


def _activity_statement_securities(text: str) -> list[_Security]:
    """Each Financial Instrument Information row, and each ``TICKER(ISIN)`` payment."""
    securities: list[_Security] = []
    header: list[str] = []
    for row in csv.reader(io.StringIO(text)):
        if row[:2] == ["Financial Instrument Information", "Header"]:
            header = row
        elif row[:2] == ["Financial Instrument Information", "Data"]:
            cells = dict(zip(header, row, strict=True))
            securities += [
                _Security(
                    isin=cells[column], ticker=cells["Symbol"], description=cells["Description"]
                )
                for column in ("Security ID", "ISIN")
                if column in cells
            ]
    securities += [_Security(isin=isin, ticker=ticker) for ticker, isin in _PAYMENT.findall(text)]
    return securities


# A Flex Query's header and trailer records, which hold its account ID (#1313).
_FLEX_RECORDS = ("BOF", "EOF")


def _flex_query_securities(text: str) -> list[_Security]:
    """Each row of a Flex Query section that names an ISIN."""
    securities: list[_Security] = []
    header: list[str] = []
    for row in csv.reader(io.StringIO(text)):
        if not row or row[0] in _FLEX_RECORDS:
            header = []
        elif "ISIN" in row and "Symbol" in row:
            header = row
        elif header:
            cells = dict(zip(header, row, strict=True))
            securities.append(
                _Security(
                    isin=cells["ISIN"],
                    ticker=cells["Symbol"],
                    description=cells["Description"],
                    cusip=cells["CUSIP"] or None,
                )
            )
    return securities


def _statement_securities(path: Path) -> list[_Security]:
    text = path.read_text(encoding="utf-8")
    if text.startswith("Statement,Header,"):
        return _activity_statement_securities(text)
    if text.startswith(('"ClientAccountID",', '"BOF",')):
        return _flex_query_securities(text)
    raise ValueError(f"{path.name} is neither an Activity Statement nor a Flex Query")


def _pinned_lot_securities() -> Iterable[object]:
    """One pytest param per fixture pinned in the lots JSON."""
    for path in _LOT_PINS:
        for fixture, lots in json.loads(path.read_text(encoding="utf-8")).items():
            securities = [
                _Security(isin=lot["isin"], ticker=lot["symbol"], description=lot["description"])
                for kind in ("open_lots", "closed_lots")
                for lot in lots[kind]
            ]
            yield pytest.param(securities, id=f"{path.name}:{fixture}")


def _security_problems(securities: list[_Security]) -> list[str]:
    """Bad identifiers, an ISIN that doesn't embed its CUSIP, and any ticker,
    description or CUSIP shared by two ISINs (or two of them on one ISIN)."""
    problems = [p for s in securities for p in _isin_problems(s.isin)]
    problems += [p for s in securities if s.cusip is not None for p in _cusip_problems(s.cusip)]
    problems += [
        f"ISIN {s.isin} does not embed its CUSIP {s.cusip}"
        for s in securities
        if s.cusip is not None and s.isin[2:11] != s.cusip
    ]
    for field in ("ticker", "description", "cusip"):
        values: defaultdict[str, set[str]] = defaultdict(set)
        isins: defaultdict[str, set[str]] = defaultdict(set)
        for security in securities:
            value = getattr(security, field)
            if field == "cusip" and value is None:
                value = security.isin[2:11]
            if value is not None:
                values[security.isin].add(value)
                isins[value].add(security.isin)
        problems += [f"ISIN {k} has {field}s {sorted(v)}" for k, v in values.items() if len(v) > 1]
        problems += [
            f"{field} {k!r} names ISINs {sorted(v)}" for k, v in isins.items() if len(v) > 1
        ]
    return problems


# ---- the fixtures ---------------------------------------------------------


def test_the_guard_reads_every_fixture_folder() -> None:
    assert _IBKR_STATEMENTS
    assert _LOT_PINS
    assert _NETBENEFITS


@pytest.mark.parametrize(
    "path",
    [*_IBKR_STATEMENTS, *_LOT_PINS, *_NETBENEFITS],
    ids=lambda path: f"{path.parent.name}/{path.name}",
)
def test_fixture_identifiers_are_synthetic(path: Path) -> None:
    assert _identifier_problems(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("path", _IBKR_STATEMENTS, ids=lambda path: path.name)
def test_statement_securities_are_distinct(path: Path) -> None:
    assert _security_problems(_statement_securities(path)) == []


@pytest.mark.parametrize("securities", list(_pinned_lot_securities()))
def test_pinned_lot_securities_are_distinct(securities: list[_Security]) -> None:
    assert securities
    assert _security_problems(securities) == []


# ---- the checks themselves ------------------------------------------------

# Made-up securities for the checks below (not in any fixture).
_XQZA = _Security(
    isin="US99998A1034", ticker="XQZA", description="SAMPLE ISSUER ZA INC", cusip="99998A103"
)
_XQZB = _Security(isin="US99998B1017", ticker="XQZB", description="SAMPLE ISSUER ZB INC")


def test_identifier_checks_accept_synthetic_and_allowed_identifiers() -> None:
    text = f"XQZA({_XQZA.isin}) {_XQZA.cusip} IE99998A1039 {FYIXX_CUSIP} {FYIXX_ISIN}"
    assert _identifier_problems(text) == []
    assert all(_ALLOWED.values())


@pytest.mark.parametrize(
    "text",
    [
        "Account Information,Data,Account,U88888888\n",
        '"ClientAccountID","CurrencyPrimary"\n"U88888888","USD"\n',
        "Participant Number: I88888888\n",
        "PARTICIPANT NO.\nI88888888\nPARTICIPANT ID.\nI88888888\n",
    ],
)
def test_identifier_checks_skip_labeled_account_and_participant_ids(text: str) -> None:
    assert _identifier_problems(text) == []


def test_identifier_checks_refuse_a_letter_leading_cusip() -> None:
    body = "ABC00010"
    cusip = body + cusip_check_digit(body)
    assert _identifier_problems(cusip) == [f"CUSIP {cusip} is outside the private-use issuer range"]


def test_identifier_checks_do_not_extend_the_allow_list_to_other_isins() -> None:
    body = "IE" + FYIXX_CUSIP
    isin = body + isin_check_digit(body)
    assert isin not in _ALLOWED
    assert _identifier_problems(isin) == [f"ISIN {isin} does not embed a synthetic CUSIP"]


@pytest.mark.parametrize(
    "text",
    [
        "Account Information,Data,Account,{cusip}\nCUSIP,{cusip}\n",
        '"ClientAccountID","CurrencyPrimary"\n"{cusip}","USD"\n\nCUSIP,{cusip}\n',
        "Participant Number: {cusip}\nCUSIP: {cusip}\n",
        "PARTICIPANT NO.\n{cusip}\nPARTICIPANT ID.\n{cusip} 1 000 CUSIP {cusip}\n",
    ],
)
def test_identifier_checks_still_check_a_security_matching_an_account_id(text: str) -> None:
    body = "ABC00010"
    cusip = body + cusip_check_digit(body)
    assert _identifier_problems(text.format(cusip=cusip)) == [
        f"CUSIP {cusip} is outside the private-use issuer range"
    ]


# An all-zero US ISIN, built here so that no real-looking token is quoted (#1070).
_ZERO_ISIN = "US" + "0" * 9 + "2"


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("US99998A1035", "ISIN US99998A1035 has a bad check digit"),
        ("99998A104", "CUSIP 99998A104 has a bad check digit"),
        (_ZERO_ISIN, f"ISIN {_ZERO_ISIN} does not embed a synthetic CUSIP"),
        ("000000000", "CUSIP 000000000 is outside the private-use issuer range"),
    ],
    ids=["isin-check-digit", "cusip-check-digit", "isin-outside-range", "cusip-outside-range"],
)
def test_identifier_checks_refuse(text: str, problem: str) -> None:
    assert _identifier_problems(text) == [problem]


def test_security_checks_accept_distinct_securities() -> None:
    assert _security_problems([_XQZA, _XQZB, _Security(isin=_XQZA.isin, ticker="XQZA")]) == []


def test_security_checks_require_distinct_embedded_cusips_across_countries() -> None:
    first = replace(_XQZA, cusip=None)
    body = "IE" + _XQZB.isin[2:11]
    second = replace(_XQZB, isin=body + isin_check_digit(body))
    assert _security_problems([first, second]) == []

    body = "IE" + first.isin[2:11]
    second = replace(second, isin=body + isin_check_digit(body))
    assert _security_problems([first, second]) == [
        f"cusip {first.isin[2:11]!r} names ISINs {sorted([first.isin, second.isin])}"
    ]


@pytest.mark.parametrize(
    ("security", "problem"),
    [
        (_Security(isin="US99998A103", ticker="XQZC"), "'US99998A103' is not an ISIN"),
        (
            _Security(isin="US99998C1007", ticker="XQZC", cusip="99998C10"),
            "'99998C10' is not a CUSIP",
        ),
        (
            _Security(isin=FYIXX_CUSIP, ticker="XQZC"),
            f"{FYIXX_CUSIP!r} is not an ISIN",
        ),
        (
            _Security(isin=_XQZA.isin, ticker="XQZC", cusip=FYIXX_ISIN),
            f"{FYIXX_ISIN!r} is not a CUSIP",
        ),
        (
            _Security(isin="US99998C1007", ticker="XQZC", cusip=_XQZA.cusip),
            "ISIN US99998C1007 does not embed its CUSIP 99998A103",
        ),
    ],
    ids=[
        "isin-shape",
        "cusip-shape",
        "allowed-cusip-in-isin-field",
        "allowed-isin-in-cusip-field",
        "isin-without-its-cusip",
    ],
)
def test_security_checks_refuse_a_bad_identifier(security: _Security, problem: str) -> None:
    assert problem in _security_problems([security])


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("ticker", "XQZA", "ticker 'XQZA' names ISINs ['US99998A1034', 'US99998B1017']"),
        (
            "description",
            "SAMPLE ISSUER ZA INC",
            "description 'SAMPLE ISSUER ZA INC' names ISINs ['US99998A1034', 'US99998B1017']",
        ),
        ("cusip", "99998A103", "cusip '99998A103' names ISINs ['US99998A1034', 'US99998B1017']"),
    ],
)
def test_security_checks_refuse_two_securities_sharing_a_field(
    field: str, value: str, problem: str
) -> None:
    assert problem in _security_problems([_XQZA, replace(_XQZB, **{field: value})])


def test_security_checks_refuse_one_isin_with_two_tickers() -> None:
    alias = _Security(isin=_XQZA.isin, ticker="XQZD")
    assert _security_problems([_XQZA, alias]) == ["ISIN US99998A1034 has tickers ['XQZA', 'XQZD']"]


def test_activity_statement_parser_reads_instruments_and_payments() -> None:
    statement = (
        "Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,"
        "Security ID,ISIN\n"
        f"Financial Instrument Information,Data,Stocks,XQZA,SAMPLE ISSUER ZA INC,1,{_XQZA.isin},"
        f"{_XQZA.isin}\n"
        f"Dividends,Data,USD,15-09-2025,XQZB({_XQZB.isin}) Cash Dividend,1.00\n"
    )
    instrument = _Security(isin=_XQZA.isin, ticker="XQZA", description="SAMPLE ISSUER ZA INC")
    assert _activity_statement_securities(statement) == [
        instrument,
        instrument,
        _Security(isin=_XQZB.isin, ticker="XQZB"),
    ]


def test_flex_query_parser_reads_each_section_row() -> None:
    flex = (
        '"Symbol","Description","CUSIP","ISIN"\n'
        f'"XQZA","SAMPLE ISSUER ZA INC","{_XQZA.cusip}","{_XQZA.isin}"\n'
        "\n"
        '"Symbol","Description","CUSIP","ISIN","Quantity"\n'
        f'"XQZB","SAMPLE ISSUER ZB INC","","{_XQZB.isin}","1"\n'
    )
    assert _flex_query_securities(flex) == [_XQZA, _XQZB]


def test_statement_of_an_unknown_format_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "e2e_unknown.csv"
    path.write_text("Symbol,ISIN\n", encoding="utf-8")
    with pytest.raises(ValueError, match="neither an Activity Statement nor a Flex Query"):
        _statement_securities(path)
