"""#1555: a blocked Robinhood Schedule CG filing workbook's refusal gives each
reachable blocker a route, and the positive-LTCG one names section 54F.

On 85b42f9 ``robinhood-cg`` and ``robinhood-all`` printed only "Schedule CG
filing is blocked: <kinds>", naming the deduction section 54, which covers the
sale of a residential house; Robinhood's residual-asset section B8/B9 has only a
section 54F deduction field. The refusal and remedy cases run the real account
reconciliation and CG compute over synthetic statements and activity reports,
behind the existing pipeline stubs, as #1546's tests do, and the real filing
workbook writer.

- Positive LTCG names no sales: the LTCG engine's lines are sorted by close
  date and symbol and don't say which lot each came from.
- An unreviewed listing venue is reached through a ``[[robinhood.securities]]``
  entry, whose ``listing_exchange`` can name an unreviewed venue, and through identity
  resolution, which maps Nasdaq Trader's exchange codes A, Z and V to NYSE
  AMERICAN, CBOE and IEX. Only an ETF can resolve to IEX: a common stock needs
  an SEC exchange compatible with its listing, and SEC's has no IEX.
- An unreviewed security class can't be reached: ``[[robinhood.securities]]``
  accepts only ``sub_category`` COMMON or ETF, identity resolution yields one
  of the two, and those are the only constructors of a Robinhood security. Its
  kind keeps its bare text.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.compliance.schedule_cg_filing_position import ROBINHOOD_SCHEDULE_CG_FILING_POSITION
from ibkr_fa.compute.robinhood_capital_gains import RobinhoodCapitalGainsResult
from ibkr_fa.compute.robinhood_identity import resolve_robinhood_standard_identities
from ibkr_fa.compute.schedule_cg_filing import require_unblocked_schedule_cg_filing
from ibkr_fa.config import RobinhoodSecurityConfig, load_user_config
from ibkr_fa.exceptions import ConfigError, InvariantError
from tests._robinhood_cg import (
    FILING_CLASS_KIND,
    FILING_LTCG_KIND,
    FILING_LTCG_ROUTE,
    FILING_VENUE_KIND,
    FILING_VENUE_ROUTE,
    cg_result,
    sale,
)
from tests._robinhood_cg_inputs import CG_CUSIP as _CUSIP
from tests._robinhood_cg_inputs import CG_SYMBOL as _SYMBOL
from tests._robinhood_cg_inputs import traded_cg_result as _traded_result
from tests._robinhood_cli import outputs_left as _left
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_identity_inputs import issuer_identity_sources as _sources
from tests._robinhood_income_stubs import (  # noqa: F401 - autouse fixture
    silenced_default_income_warnings as _silence_default_income_warnings,
)
from tests._robinhood_pipeline_stubs import assert_cg_refused as _assert_cg_refused
from tests._robinhood_pipeline_stubs import stub_cg_run as _stub_cg_run
from tests._robinhood_security import robinhood_security
from tests._robinhood_toml import robinhood_security_toml, robinhood_toml

_BLOCKED = "InvariantError: Schedule CG filing is blocked: "
# The managed account's sale row, 1:4, after its buy (row 1:3).
_VENUE_ROUTE = FILING_VENUE_ROUTE.format(sales="1:4")
_SUFFIX = " (context keys: reasons)"
_FY_2025 = date(2025, 4, 1)
_TRADED = {"symbol": _SYMBOL, "cusip": _CUSIP, "isin": "US99971T1034"}
# One share bought for $10.00 and sold on 13 June 2025 for $12.00, a ₹170 gain
# at ₹85: short-term from a May 2025 buy, long-term from a May 2023 one.
_SHORT_TERM = (date(2025, 5, 1), "1.0000", "10.00", "-10.00")
_LONG_TERM = (date(2023, 5, 1), "1.0000", "10.00", "-10.00")
_SALE = (date(2025, 6, 13), "1.0000", "12.00", "12.00")


def _sold(
    buy: tuple[date, str, str, str], security: RobinhoodSecurityConfig | None = None
) -> RobinhoodCapitalGainsResult:
    """The real CG compute for FY 2025-26 over ``buy`` and ``_SALE`` in the
    managed account (rows 1:3 and 1:4), of ``security`` when given."""
    return _traded_result(
        buy,
        _SALE,
        financial_year_start=_FY_2025,
        rate=Decimal(85),
        report_start=date(2023, 1, 1),
        security=security,
    )


def _refusal(*kinds: str, routes: tuple[str, ...]) -> str:
    """The refusal of a filing blocked on ``kinds``, then ``routes``, in blocker order."""
    lines = ("; ".join(kinds), *routes)
    return f"{_BLOCKED}{'\n'.join(lines)}{_SUFFIX}"


def test_positive_ltcg_names_section_54f_and_the_by_hand_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A share held from May 2023 and sold in June 2025 at a ₹170 gain. On
    85b42f9 the refusal named a section 54 decision and no route."""
    _assert_cg_refused(
        tmp_path,
        monkeypatch,
        capsys,
        lambda: _sold(_LONG_TERM),
        _refusal(FILING_LTCG_KIND, routes=(FILING_LTCG_ROUTE,)),
    )


def _configured(listing_exchange: str) -> RobinhoodSecurityConfig:
    return robinhood_security(
        symbol=_SYMBOL, cusip=_CUSIP, isin=_TRADED["isin"], listing_exchange=listing_exchange
    )


@pytest.mark.parametrize("venue", ["NYSE AMERICAN", "Nasdaq"])
def test_a_configured_unreviewed_venue_names_the_sale_and_both_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], venue: str
) -> None:
    """A short-term sale of a security whose ``[[robinhood.securities]]``
    entry sets an exchange outside the reviewed set, or a reviewed one
    spelled differently. The refusal names the sale row, never the venue. On
    85b42f9 it named neither the sale nor a route."""
    _assert_cg_refused(
        tmp_path,
        monkeypatch,
        capsys,
        lambda: _sold(_SHORT_TERM, _configured(venue)),
        _refusal(FILING_VENUE_KIND, routes=(_VENUE_ROUTE,)),
    )


def test_both_blockers_print_both_routes_in_blocker_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The long-term sale of a security configured on NYSE AMERICAN."""
    _assert_cg_refused(
        tmp_path,
        monkeypatch,
        capsys,
        lambda: _sold(_LONG_TERM, _configured("NYSE AMERICAN")),
        _refusal(FILING_VENUE_KIND, FILING_LTCG_KIND, routes=(_VENUE_ROUTE, FILING_LTCG_ROUTE)),
    )


def test_correcting_a_wrong_listing_exchange_and_rerunning_writes_the_workbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Follow the venue route word for word. user.toml's entry for the sold
    security sets ``listing_exchange = "NYSE AMERICAN"``, deliberately wrong:
    the synthetic security is listed on NYSE. Correcting that line to NYSE,
    the exchange that lists it, and re-running writes schedule-cg.xlsx and
    exits 0."""
    config = tmp_path / "user.toml"
    config.write_text(
        robinhood_toml(extra=robinhood_security_toml(**_TRADED, listing_exchange="NYSE AMERICAN")),
        encoding="utf-8",
    )

    def make_cg() -> RobinhoodCapitalGainsResult:
        robinhood = load_user_config(config).robinhood
        assert robinhood is not None
        [security] = robinhood.securities
        return _sold(_SHORT_TERM, security)

    _stub_cg_run(monkeypatch, make_cg)
    assert _main("robinhood-cg", tmp_path) == 4
    assert _VENUE_ROUTE in capsys.readouterr().err
    assert _left(tmp_path) == []

    wrong = 'listing_exchange = "NYSE AMERICAN"\n'
    assert config.read_text(encoding="utf-8").count(wrong) == 1
    config.write_text(
        config.read_text(encoding="utf-8").replace(wrong, 'listing_exchange = "NYSE"\n'),
        encoding="utf-8",
    )

    assert _main("robinhood-cg", tmp_path) == 0
    assert capsys.readouterr().err == ""
    assert _left(tmp_path) == ["schedule-cg.xlsx"]


# Each venue identity resolution reaches outside the reviewed set: Nasdaq
# Trader's exchange code, the SEC exchange a common stock needs (None for an
# ETF, which needs a fund record instead) and whether it's an ETF.
_RESOLVED_VENUES = {
    "NYSE AMERICAN": ("A", "NYSE", False),
    "CBOE": ("Z", "CBOE", False),
    "IEX": ("V", None, True),
}


def _resolved(code: str, sec_exchange: str | None, etf: bool) -> RobinhoodSecurityConfig:
    """The identity the real resolver generates for the traded security from
    official records listing it under ``code``."""
    observed, sources = _sources(symbol=_SYMBOL, cusip=_CUSIP, etf="Y" if etf else "N", fund=etf)
    tickers = sources.sec_tickers_by_symbol
    if sec_exchange is not None:
        tickers = {_SYMBOL: tuple(replace(row, exchange=sec_exchange) for row in tickers[_SYMBOL])}
    sources = replace(
        sources,
        nasdaq_by_symbol={
            _SYMBOL: replace(sources.nasdaq_by_symbol[_SYMBOL], listing_exchange=code)
        },
        sec_tickers_by_symbol=tickers,
    )
    resolution = resolve_robinhood_standard_identities((observed,), sources, ())
    assert resolution.failures == ()
    [security] = resolution.generated
    return security


@pytest.mark.parametrize(("venue", "listing"), _RESOLVED_VENUES.items(), ids=_RESOLVED_VENUES)
def test_an_identity_resolved_unreviewed_venue_refuses_with_the_by_hand_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    venue: str,
    listing: tuple[str, str | None, bool],
) -> None:
    """A security with no ``[[robinhood.securities]]`` entry resolves to an
    unreviewed venue from official records, and its short-term sale refuses.
    With no entry setting a wrong exchange, the route that applies is the
    whole schedule by hand. On 85b42f9 the refusal named no route."""
    security = _resolved(*listing)
    assert security.instrument.listing_exchange == venue

    _assert_cg_refused(
        tmp_path,
        monkeypatch,
        capsys,
        lambda: _sold(_SHORT_TERM, security),
        _refusal(FILING_VENUE_KIND, routes=(_VENUE_ROUTE,)),
    )


@pytest.mark.parametrize("sub_category", ["COMMON", "ETF", "PREFERRED"])
def test_no_robinhood_security_reaches_an_unreviewed_class(
    tmp_path: Path, sub_category: str
) -> None:
    """The unreviewed-class blocker is unreachable: user.toml accepts only the
    two reviewed classes, and identity resolution yields one of them."""
    config = tmp_path / "user.toml"
    config.write_text(
        robinhood_toml(extra=robinhood_security_toml(sub_category=sub_category)), encoding="utf-8"
    )
    reviewed = ROBINHOOD_SCHEDULE_CG_FILING_POSITION.reviewed_security_classes

    if sub_category not in reviewed:
        with pytest.raises(ConfigError, match=r"sub_category': must be one of: COMMON, ETF"):
            load_user_config(config)
        return
    robinhood = load_user_config(config).robinhood
    assert robinhood is not None
    assert robinhood.securities[0].sub_category == sub_category
    assert _resolved("Q", "Nasdaq", sub_category == "ETF").sub_category == sub_category


def test_the_unreviewed_class_keeps_its_bare_text() -> None:
    """With no input reaching it, the kind gets no route line."""
    item = sale("12", "10")
    result = cg_result(replace(item, closed_lot=replace(item.closed_lot, sub_category="PREFERRED")))
    filing = result.filing(())
    assert filing is not None

    with pytest.raises(InvariantError) as raised:
        require_unblocked_schedule_cg_filing(filing.blockers)

    assert str(raised.value) == f"{_BLOCKED}{FILING_CLASS_KIND}{_SUFFIX}"


def test_the_venue_route_names_only_the_sales_on_an_unreviewed_venue() -> None:
    """Of two sales, rows 0:2 and 0:4, only the second's lot is on an
    unreviewed venue, so the route names only that sale."""
    unreviewed = sale("12", "10", lot_ordinal=3, sale_ordinal=4)
    unreviewed = replace(
        unreviewed, closed_lot=replace(unreviewed.closed_lot, listing_exchange="IEX")
    )
    filing = cg_result(sale("12", "10"), unreviewed).filing(())
    assert filing is not None

    [blocker] = filing.blockers

    assert blocker.route == _VENUE_ROUTE.replace(": 1:4. ", ": 0:4. ")
