"""NetBenefits blank A3 peak texts and Dec-31 close refusal routes (#1549).

CLI cases use the e2e NetBenefits bundle (real parsers,
synthetic fixtures): ``netbenefits-fa`` and the FA leg of ``netbenefits-all``.
Its default confirmation buys 3.482 shares on 31 December 2025 with a Market Value at
Purchase of USD 743.30 (213.47 a share, to the cent), and its config prices
that close with a ``[[netbenefits.price_overrides]]`` entry of 213.47 dated
2025-12-31, CY 2025's required US market session.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from ibkr_fa.cli import main
from ibkr_fa.compute import dec31_close
from ibkr_fa.models import FaA3PeakBlankReason
from ibkr_fa.prices.cache import _cache_path
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.prices.yahoo import DailyCloseFetcher
from ibkr_fa.reference.us_market_calendar import us_market_sessions
from ibkr_fa.types import Ticker
from tests._dec31_close import MARKET_SESSION_REFUSAL, OVERRIDE_SESSION_REFUSAL
from tests._netbenefits_e2e import (
    NB_E2E_PDF_TEXT,
    NB_E2E_PRICE_OVERRIDE,
    NB_E2E_TOML,
    NB_E2E_TOML_WITH_PEAK,
    csv_file_rows,
    nb_e2e_bundle,
)
from tests._netbenefits_fa import (
    NB_BY_HAND,
    NB_MARKET_SESSION_REFUSAL,
    NB_MISMATCH_REFUSAL,
    NB_NO_CLOSE_FETCHED_HIGH_REFUSAL,
    NB_NO_CLOSE_NOT_FETCHED_HIGH_REFUSAL,
    NB_NO_CLOSE_REFUSAL,
    NB_OVERRIDE_ANCHOR_ROUTE,
    NB_OVERRIDE_SESSION_REFUSAL,
    nb_peak_line,
)
from tests._text_edits import replace_once

_DEC30 = dt.date(2025, 12, 30)
_DEC31 = dt.date(2025, 12, 31)
_PEAK = "Peak value of investment during the Period"
_CLOSING = "Closing balance"
_OVERRIDE_PRICE = 'price_per_share_usd = "213.47"\n'
_OVERRIDE_DATE = "price_date = 2025-12-31\n"
_HIGH_BASIS = '\n[schedule_fa]\npeak_basis = "high"\n'
# The text the run printed for every blank peak before #1549.
_OLD_PEAK_TEXT = "intraday high could not be established"


def _filed_value(price_usd: str) -> str:
    """The bundle's 3.4820 Dec-31 shares (its open-lots row) at ``price_usd`` a
    share, at 89.47, the bundled SBI TTBR for 31 December 2025, in whole rupees."""
    value = Decimal("3.4820") * Decimal(price_usd) * Decimal("89.47")
    return str(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# The filed A3 peak: at the working fetch's 219.00 high under the high basis
# (INR 68,226.06), and at the 213.47 close under the close basis (INR 66,503.28).
_PEAK_AT_THE_HIGH = _filed_value("219.00")
_PEAK_AT_THE_CLOSE = _filed_value("213.47")


def _fetcher(
    close: str = "213.47", *, high: str | None = None, dec31: bool = True
) -> DailyCloseFetcher:
    """``close`` (and ``high``, when given) on every US market session in the
    request, with no 31 December bar when ``dec31`` is false."""

    def fetch(ticker, *, start, end, now, fetch_symbol=None):
        del fetch_symbol
        sessions = [day for day in us_market_sessions(start, end) if dec31 or day != _DEC31]
        return PriceSeries(
            ticker=ticker,
            currency="USD",
            closes=dict.fromkeys(sessions, Decimal(close)),
            highs={} if high is None else dict.fromkeys(sessions, Decimal(high)),
            has_split_in_range=False,
            source="stub",
            fetched_at=now,
        )

    return fetch


def _no_series(ticker, *, start, end, now, fetch_symbol=None) -> None:
    """A fetch that finds no series, as for an unknown symbol."""
    del ticker, start, end, now, fetch_symbol


class _Bundle:
    """The e2e bundle plus a config the test edits, run through either command."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.bundle, self.cfg = nb_e2e_bundle(tmp_path, monkeypatch)
        self.cfg.write_text(toml, encoding="utf-8")
        self.cache = tmp_path / "cache"
        self.runs = 0

    def edit(self, old: str, new: str) -> None:
        toml = self.cfg.read_text(encoding="utf-8")
        self.cfg.write_text(replace_once(toml, old, new), encoding="utf-8")

    def run(
        self,
        command: str,
        capsys: pytest.CaptureFixture[str],
        *,
        fetcher: Callable[..., PriceSeries | None] | None,
    ) -> tuple[int, str, str, Path]:
        """Run ``command`` into a new output folder, fetching with ``fetcher``
        (``--no-fetch-prices`` when ``None``) into the bundle's one price
        cache folder. Returns the exit code, stdout, stderr and the Table A3
        path."""
        self.runs += 1
        out = self.tmp_path / f"out-{self.runs}"
        out.mkdir()
        if fetcher is not None:
            self.monkeypatch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", fetcher)
        common = ("--input", str(self.bundle), "--config", str(self.cfg))
        prices = (
            *("--price-cache-dir", str(self.cache)),
            *(() if fetcher else ("--no-fetch-prices",)),
        )
        a3 = out / "schedule-fa-a3.csv"
        if command == "netbenefits-fa":
            args = [command, *common, "--output", str(a3), "--calendar-year", "2025", *prices]
        else:
            args = [
                command,
                *common,
                *("--output-dir", str(out), "--financial-year", "2025-26", "--itr-form", "ITR-2"),
                *prices,
            ]
        rc = main(args)
        captured = capsys.readouterr()
        return rc, captured.out, captured.err, a3


_COMMANDS = pytest.mark.parametrize("command", ["netbenefits-fa", "netbenefits-all"])


# --- blank A3 peaks ------------------------------------------------------------


# Each reason a NetBenefits input reaches, with the fetcher that reaches it
# (None: --no-fetch-prices with an empty cache). The rest can't be reached:
# every lot is an open lot bought on 31 December of the only calendar year
# with a supported market calendar (2025, a session), so the one-day window
# is never empty and holds no split day, and only Fidelity CMA refuses a
# disposal-day peak; the Dec-31 close resolver refuses a split-unsafe or
# non-USD series, or one without a usable 31 December close, before any
# peak; and the closing value needs the same day's TTBR first.
_REACHABLE_REASONS = pytest.mark.parametrize(
    ("reason", "fetcher"),
    [
        pytest.param(FaA3PeakBlankReason.PRICES_NOT_FETCHED, None, id="prices-not-fetched"),
        pytest.param(FaA3PeakBlankReason.NO_USD_SERIES, _no_series, id="no-usd-series"),
        pytest.param(
            FaA3PeakBlankReason.SESSION_WITHOUT_HIGH, _fetcher(), id="session-without-high"
        ),
        # Closes and highs of 213.465: half a cent a share below the override,
        # inside the cross-check's half-cent-a-share envelope, so every high
        # is below the closing value.
        pytest.param(
            FaA3PeakBlankReason.HIGH_BELOW_CLOSING,
            _fetcher("213.465", high="213.465"),
            id="high-below-closing",
        ),
    ],
)


@_REACHABLE_REASONS
@_COMMANDS
def test_a_blank_peak_names_its_reason_its_row_and_the_shared_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    reason: FaA3PeakBlankReason,
    fetcher: DailyCloseFetcher | None,
) -> None:
    """Each run leaves row 1's peak blank, exits 3 and prints that reason's
    shared cause and remedy for row 1 once, never the missing-high text."""
    bundle = _Bundle(tmp_path, monkeypatch, NB_E2E_TOML + _HIGH_BASIS)
    rc, out, err, a3 = bundle.run(command, capsys, fetcher=fetcher)
    assert rc == 3
    header, row = csv_file_rows(a3)
    assert row[header.index(_PEAK)] == ""
    assert row[header.index(_CLOSING)] != ""
    assert out.splitlines().count(nb_peak_line(reason)) == 1
    assert "Peak value(s) for row(s)" not in out.replace(nb_peak_line(reason), "")
    assert _OLD_PEAK_TEXT not in out
    if command == "netbenefits-fa":
        assert err == ""


def test_the_high_below_closing_line_gives_no_refetch_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every high was fetched, so the line sends the filer to fill the peak by
    hand and not to fetch again."""
    bundle = _Bundle(tmp_path, monkeypatch, NB_E2E_TOML + _HIGH_BASIS)
    _rc, out, _err, _a3 = bundle.run(
        "netbenefits-fa", capsys, fetcher=_fetcher("213.465", high="213.465")
    )
    assert (
        "  A3 Peak value(s) for row(s) 1: MANUAL-FILL — the peak is blank because "
        "peak_basis = \"high\" found the price series' peak below the statement's closing "
        "value, so the two sources disagree. Enter it by hand; a ticker override or a "
        "re-fetch won't help."
    ) in out.splitlines()
    assert "Re-run" not in out


def test_a_missing_high_still_gives_a_fetch_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _Bundle(tmp_path, monkeypatch, NB_E2E_TOML + _HIGH_BASIS)
    _rc, out, _err, _a3 = bundle.run("netbenefits-fa", capsys, fetcher=_fetcher())
    (line,) = (line for line in out.splitlines() if "Peak value(s)" in line)
    assert "Re-run with price fetching and an empty --price-cache-dir" in line


@pytest.mark.parametrize(
    ("reason", "first", "edit", "peak"),
    [
        # "Re-run without --no-fetch-prices to compute it."
        pytest.param(
            FaA3PeakBlankReason.PRICES_NOT_FETCHED, None, None, _PEAK_AT_THE_HIGH, id="fetch"
        ),
        # "If the fetch failed, re-run."
        pytest.param(
            FaA3PeakBlankReason.NO_USD_SERIES, _no_series, None, _PEAK_AT_THE_HIGH, id="refetch"
        ),
        # The remedy's close-basis route.
        pytest.param(
            FaA3PeakBlankReason.SESSION_WITHOUT_HIGH,
            _fetcher(),
            ('peak_basis = "high"', 'peak_basis = "close"'),
            _PEAK_AT_THE_CLOSE,
            id="close-basis",
        ),
    ],
)
def test_following_the_peak_remedy_fills_the_peak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reason: FaA3PeakBlankReason,
    first: DailyCloseFetcher | None,
    edit: tuple[str, str] | None,
    peak: str,
) -> None:
    """The rerun the remedy names files the peak and prints no peak line."""
    bundle = _Bundle(tmp_path, monkeypatch, NB_E2E_TOML + _HIGH_BASIS)
    _rc, out, _err, _a3 = bundle.run("netbenefits-fa", capsys, fetcher=first)
    assert nb_peak_line(reason) in out.splitlines()
    if edit is not None:
        bundle.edit(*edit)
    # A working fetch: closes of 213.47 and highs of 219.00 on every session.
    rc, out, err, a3 = bundle.run(
        "netbenefits-fa", capsys, fetcher=_fetcher("213.47", high="219.00")
    )
    assert rc == 3  # the A2 peak balance is still manual fill
    header, row = csv_file_rows(a3)
    assert row[header.index(_PEAK)] == peak
    assert "Peak value(s)" not in out
    assert err == ""


# --- Dec-31 close refusals -----------------------------------------------------

_OVERRIDE_ANCHOR_REFUSAL = (
    "open-lots data row 2: the Dec-31 close (override) differs from the Market Value at "
    "Purchase on confirmation 1 (by purchase date) by more than the tolerance; "
    f"{NB_OVERRIDE_ANCHOR_ROUTE}"
)
_MARKET_ANCHOR_REFUSAL = (
    "open-lots data row 2: the Dec-31 close (yahoo_fallback) differs from the Market Value "
    f"at Purchase on confirmation 1 (by purchase date) by more than the tolerance; {NB_BY_HAND}"
)


def _refuses(result: tuple[int, str, str, Path], refusal: str) -> None:
    """Exit 4 with ``refusal`` on its own stderr line and no Table A3."""
    rc, _out, err, a3 = result
    assert rc == 4
    assert f"error: {refusal}" in err.splitlines()
    assert not a3.exists()


def _clears(result: tuple[int, str, str, Path]) -> None:
    """Exit 3 (the A2 peak balance stays manual fill) with Table A3 written and
    no refusal (``netbenefits-all``'s income leg warns on stderr)."""
    rc, _out, err, a3 = result
    assert rc == 3
    assert "error:" not in err
    header, row = csv_file_rows(a3)
    assert row[header.index(_CLOSING)] != ""


@_COMMANDS
def test_no_market_close_for_the_session_names_it_and_a_fresh_fetch_clears_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """The fetched series ends on 30 December and is cached. Rerunning reuses
    the cache; moving the cache file aside, as the route says, makes the rerun
    fetch a series with the 31 December close, which clears the refusal."""
    bundle = _Bundle(tmp_path, monkeypatch, NB_E2E_TOML)
    _refuses(bundle.run(command, capsys, fetcher=_fetcher(dec31=False)), NB_MARKET_SESSION_REFUSAL)
    cache_file = _cache_path(bundle.cache, Ticker("XESP"))
    assert cache_file.is_file()
    _refuses(bundle.run(command, capsys, fetcher=_fetcher()), NB_MARKET_SESSION_REFUSAL)
    cache_file.rename(tmp_path / cache_file.name)
    _clears(bundle.run(command, capsys, fetcher=_fetcher()))


@_COMMANDS
def test_an_override_off_the_session_names_the_date_it_must_carry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """A 30 December override refuses; dating it 2025-12-31 at 743.30 / 3.482
    to the cent (213.47), as the route says, clears it."""
    bundle = _Bundle(
        tmp_path, monkeypatch, NB_E2E_TOML.replace(_OVERRIDE_DATE, "price_date = 2025-12-30\n")
    )
    _refuses(bundle.run(command, capsys, fetcher=None), NB_OVERRIDE_SESSION_REFUSAL)
    _refuses(bundle.run(command, capsys, fetcher=_fetcher()), NB_OVERRIDE_SESSION_REFUSAL)
    bundle.edit("price_date = 2025-12-30\n", _OVERRIDE_DATE)
    _clears(bundle.run(command, capsys, fetcher=None))
    _clears(bundle.run(command, capsys, fetcher=_fetcher()))


# A 30 December override at the 31 December market close of 214.47, which is
# off the confirmation's 743.30 / 3.482 = 213.47 a share to the cent (#1561).
_OFF_DATE = "price_date = 2025-12-30\n"
_MARKET_PRICE = 'price_per_share_usd = "214.47"\n'


@_COMMANDS
@pytest.mark.parametrize("fetched", [False, True], ids=["no-series", "market-close"])
def test_an_override_off_the_session_and_the_confirmation_follows_the_date_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    fetched: bool,
) -> None:
    """The override refuses for its date. Dating it 2025-12-31 at the market
    close refuses at the confirmation. Dating it there at 213.47, as the route
    says, clears it with no price series. With the 214.47 market close fetched,
    213.47 refuses beside it, and that route ends at the by-hand route the
    date route names for a differing market close."""
    fetcher = _fetcher("214.47") if fetched else None
    toml = NB_E2E_TOML.replace(_OVERRIDE_DATE, _OFF_DATE).replace(_OVERRIDE_PRICE, _MARKET_PRICE)
    bundle = _Bundle(tmp_path, monkeypatch, toml)
    _refuses(bundle.run(command, capsys, fetcher=fetcher), NB_OVERRIDE_SESSION_REFUSAL)
    bundle.edit(_OFF_DATE, _OVERRIDE_DATE)
    _refuses(bundle.run(command, capsys, fetcher=fetcher), _OVERRIDE_ANCHOR_REFUSAL)
    bundle.edit(_MARKET_PRICE, _OVERRIDE_PRICE)
    if fetched:
        _refuses(bundle.run(command, capsys, fetcher=fetcher), NB_MISMATCH_REFUSAL)
    else:
        _clears(bundle.run(command, capsys, fetcher=fetcher))


def test_the_followed_date_route_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With the A2 peak set in the config, netbenefits-fa's CBDT JSON
    fragment needs nothing by hand, so the run the route leads to exits 0."""
    toml = NB_E2E_TOML_WITH_PEAK.replace(_OVERRIDE_DATE, _OFF_DATE).replace(
        _OVERRIDE_PRICE, _MARKET_PRICE
    )
    bundle = _Bundle(tmp_path, monkeypatch, toml)
    fragment = tmp_path / "fa.json"
    args = [
        "netbenefits-fa",
        *("--input", str(bundle.bundle), "--config", str(bundle.cfg)),
        *("--output", str(fragment), "--calendar-year", "2025", "--no-fetch-prices"),
        *("--format", "cbdt-json-fragment", "--itr-form", "ITR-2"),
    ]
    assert main(args) == 4
    assert f"error: {NB_OVERRIDE_SESSION_REFUSAL}" in capsys.readouterr().err.splitlines()
    assert not fragment.exists()
    bundle.edit(_OFF_DATE, _OVERRIDE_DATE)
    bundle.edit(_MARKET_PRICE, _OVERRIDE_PRICE)
    assert main(args) == 0
    assert capsys.readouterr().err == ""
    assert fragment.is_file()


@_COMMANDS
def test_a_mistyped_override_that_disagrees_with_the_market_close_is_corrected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """An override of 214.47 beside a 213.47 market close refuses. It differs
    from 743.30 / 3.482 to the cent (213.47), so the route says to correct it,
    and the corrected entry clears the refusal."""
    bundle = _Bundle(
        tmp_path,
        monkeypatch,
        NB_E2E_TOML.replace(_OVERRIDE_PRICE, 'price_per_share_usd = "214.47"\n'),
    )
    _refuses(bundle.run(command, capsys, fetcher=_fetcher()), NB_MISMATCH_REFUSAL)
    bundle.edit('price_per_share_usd = "214.47"\n', _OVERRIDE_PRICE)
    _clears(bundle.run(command, capsys, fetcher=_fetcher()))


@_COMMANDS
def test_a_market_close_that_disagrees_with_a_matching_override_leads_to_the_by_hand_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """The override is 213.47, which matches the confirmation to the cent, and
    the market close is 214.47. The route's check finds nothing to correct,
    so it ends at completing the NetBenefits Schedule FA by hand. Setting the
    override to the market close instead refuses at the confirmation, whose
    route also ends there."""
    bundle = _Bundle(tmp_path, monkeypatch, NB_E2E_TOML)
    _refuses(bundle.run(command, capsys, fetcher=_fetcher("214.47")), NB_MISMATCH_REFUSAL)
    bundle.edit(_OVERRIDE_PRICE, 'price_per_share_usd = "214.47"\n')
    _refuses(bundle.run(command, capsys, fetcher=_fetcher("214.47")), _OVERRIDE_ANCHOR_REFUSAL)


@_COMMANDS
def test_an_override_off_the_confirmation_is_corrected_to_its_market_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """With no price series, an override of 214.47 refuses at the
    confirmation; setting it to 743.30 / 3.482 to the cent (213.47), as the
    route says, clears it."""
    bundle = _Bundle(
        tmp_path,
        monkeypatch,
        NB_E2E_TOML.replace(_OVERRIDE_PRICE, 'price_per_share_usd = "214.47"\n'),
    )
    _refuses(bundle.run(command, capsys, fetcher=None), _OVERRIDE_ANCHOR_REFUSAL)
    bundle.edit('price_per_share_usd = "214.47"\n', _OVERRIDE_PRICE)
    _clears(bundle.run(command, capsys, fetcher=None))


@_COMMANDS
def test_a_market_close_off_the_confirmation_names_the_by_hand_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """With no override, a market close of 214.47 refuses at the confirmation
    and names the by-hand route. An override at the confirmation's value
    then refuses beside that market close, whose route also ends there."""
    bundle = _Bundle(tmp_path, monkeypatch, NB_E2E_TOML.replace(NB_E2E_PRICE_OVERRIDE, ""))
    _refuses(bundle.run(command, capsys, fetcher=_fetcher("214.47")), _MARKET_ANCHOR_REFUSAL)
    bundle.cfg.write_text(NB_E2E_TOML, encoding="utf-8")
    _refuses(bundle.run(command, capsys, fetcher=_fetcher("214.47")), NB_MISMATCH_REFUSAL)


@_COMMANDS
@pytest.mark.parametrize("fetch_prices", [False, True], ids=["anchor", "mismatch"])
def test_a_refused_cent_rounded_override_names_the_by_hand_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    fetch_prices: bool,
) -> None:
    """The accepted confirmation's FMV/share rounds to zero. Entering that
    value, as advised, refuses in config, so the route must also name by hand."""
    text = NB_E2E_PDF_TEXT["confirmation-dec.pdf"]
    for old, new in (
        ("3.482", "10"),
        ("192.1200", "0.0010"),
        ("743.30", "0.02"),
        ("668.96", "0.01"),
        ("74.34", "0.01"),
    ):
        assert old in text
        text = text.replace(old, new)
    monkeypatch.setitem(NB_E2E_PDF_TEXT, "confirmation-dec.pdf", text)
    bundle = _Bundle(
        tmp_path,
        monkeypatch,
        NB_E2E_TOML.replace('espp_discount_pct = "10"', 'espp_discount_pct = "50"').replace(
            _OVERRIDE_PRICE, 'price_per_share_usd = "0.1"\n'
        ),
    )
    lots = bundle.bundle / "open-lots.csv"
    text = lots.read_text(encoding="utf-8")
    old = "Dec-31-2025,3.4820,668.96,192.12,626.24,-42.72,"
    assert old in text
    lots.write_text(text.replace(old, "Dec-31-2025,10,0.01,0.0010,0,-0.01,"), encoding="utf-8")
    fetcher = _fetcher("0.002") if fetch_prices else None
    result = bundle.run(command, capsys, fetcher=fetcher)
    _refuses(result, NB_MISMATCH_REFUSAL if fetch_prices else _OVERRIDE_ANCHOR_REFUSAL)
    assert "if the rounded value is refused as price_per_share_usd, or " in result[2]
    assert NB_BY_HAND in result[2]

    rounded = (Decimal("0.02") / Decimal("10")).quantize(Decimal("0.01"))
    assert rounded == 0
    bundle.edit('price_per_share_usd = "0.1"\n', f'price_per_share_usd = "{rounded}"\n')
    rc, _out, err, a3 = bundle.run(command, capsys, fetcher=fetcher)
    assert rc == 4
    assert "netbenefits.price_overrides[0].price_per_share_usd': must be > 0" in err
    assert not a3.exists()


# --- no Dec-31 close at all (#1565) ---------------------------------------------

# A working fetch: closes of 213.47 and highs of 219.00 on every session.
_WORKING_FETCH = _fetcher("213.47", high="219.00")


@_COMMANDS
@pytest.mark.parametrize("fetcher", [None, _no_series], ids=["not-fetched", "no-series"])
def test_no_close_under_the_close_basis_clears_with_the_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    fetcher: DailyCloseFetcher | None,
) -> None:
    """With no override and no price series, the route names the override
    alone, as before #1565. Adding it at that session's close (213.47) files
    both the closing value and the peak."""
    bundle = _Bundle(tmp_path, monkeypatch, NB_E2E_TOML.replace(NB_E2E_PRICE_OVERRIDE, ""))
    _refuses(bundle.run(command, capsys, fetcher=fetcher), NB_NO_CLOSE_REFUSAL)
    bundle.cfg.write_text(NB_E2E_TOML, encoding="utf-8")
    result = bundle.run(command, capsys, fetcher=fetcher)
    _clears(result)
    header, row = csv_file_rows(result[3])
    assert row[header.index(_PEAK)] != ""
    assert "Peak value(s)" not in result[1]


@_COMMANDS
def test_no_close_without_fetching_under_the_high_basis_reruns_with_a_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """With --no-fetch-prices, an empty cache and no override, the route
    names a rerun with fetching on, because an override would leave the
    high-basis A3 peak blank. That rerun, with no config change, files both
    the closing value and the peak."""
    bundle = _Bundle(
        tmp_path, monkeypatch, NB_E2E_TOML.replace(NB_E2E_PRICE_OVERRIDE, "") + _HIGH_BASIS
    )
    _refuses(bundle.run(command, capsys, fetcher=None), NB_NO_CLOSE_NOT_FETCHED_HIGH_REFUSAL)
    result = bundle.run(command, capsys, fetcher=_WORKING_FETCH)
    _clears(result)
    header, row = csv_file_rows(result[3])
    assert row[header.index(_PEAK)] == _PEAK_AT_THE_HIGH
    assert "Peak value(s)" not in result[1]


@_COMMANDS
def test_no_close_after_a_fetch_under_the_high_basis_names_the_by_hand_peak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """A fetch that finds no series and no override: the route names the
    override and then the A3 peak by hand. With the override added, the
    rerun files the closing value and leaves only the A3 peak blank, the
    step the route named."""
    bundle = _Bundle(
        tmp_path, monkeypatch, NB_E2E_TOML.replace(NB_E2E_PRICE_OVERRIDE, "") + _HIGH_BASIS
    )
    _refuses(bundle.run(command, capsys, fetcher=_no_series), NB_NO_CLOSE_FETCHED_HIGH_REFUSAL)
    bundle.cfg.write_text(NB_E2E_TOML + _HIGH_BASIS, encoding="utf-8")
    result = bundle.run(command, capsys, fetcher=_no_series)
    _clears(result)
    header, row = csv_file_rows(result[3])
    assert row[header.index(_PEAK)] == ""
    peak_lines = [line for line in result[1].splitlines() if "Peak value(s)" in line]
    assert peak_lines == [nb_peak_line(FaA3PeakBlankReason.NO_USD_SERIES)]


# --- the resolver ----------------------------------------------------------------


def _routes() -> dec31_close.Dec31SessionRoutes:
    """Routes naming their branch and the session they get. Built per test, so
    the module collects on a base without the class."""
    return dec31_close.Dec31SessionRoutes(
        market_close=lambda session: f"market route {session}",
        override_date=lambda session: f"date route {session}",
        mismatch=lambda session: f"mismatch route {session}",
    )


def _series(*, dec31: bool = True) -> PriceSeries:
    """Closes of 213.47 on 30 December 2025 and, when ``dec31``, on 31 December."""
    closes = {_DEC30: Decimal("213.47")} | ({_DEC31: Decimal("213.47")} if dec31 else {})
    return PriceSeries(
        ticker=Ticker("XESP"),
        currency="USD",
        closes=closes,
        has_split_in_range=False,
        source="test",
        fetched_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )


@pytest.mark.parametrize(
    ("series", "override_on", "override_price", "expected"),
    [
        pytest.param(
            _series(dec31=False),
            None,
            None,
            f"{MARKET_SESSION_REFUSAL}; market route 2025-12-31",
            id="market",
        ),
        pytest.param(
            None, _DEC30, "213.47", f"{OVERRIDE_SESSION_REFUSAL}; date route 2025-12-31", id="date"
        ),
        pytest.param(
            _series(),
            _DEC31,
            "214.47",
            "Dec-31 close mismatch: the override and yahoo_fallback totals differ by more than "
            "the tolerance; refusing rather than file an unverified closing value; mismatch "
            "route 2025-12-31",
            id="mismatch",
        ),
    ],
)
def test_the_resolver_appends_each_route_to_its_own_refusal_only(
    series: PriceSeries | None,
    override_on: dt.date | None,
    override_price: str | None,
    expected: str,
) -> None:
    """Each route gets the required session. Without routes (StockPlan) the
    fixed text stands alone, as tests/test_compute_dec31_close.py pins."""
    with pytest.raises(dec31_close.ClosingPriceError) as caught:
        dec31_close.resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=Decimal("3.482"),
            statement_total_usd=None,
            series=series,
            override_per_share_usd=None if override_price is None else Decimal(override_price),
            override_priced_on=override_on,
            session_routes=_routes(),
        )
    assert str(caught.value) == expected


def test_the_missing_source_refusal_carries_no_session_route() -> None:
    with pytest.raises(dec31_close.ClosingPriceError) as caught:
        dec31_close.resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=Decimal("3.482"),
            statement_total_usd=None,
            series=None,
            missing_source_remedy="the caller's remedy",
            session_routes=_routes(),
        )
    assert str(caught.value) == (
        "no Dec-31 close available for 2025-12-31: there is no statement total, no override "
        "and no usable Yahoo close; the caller's remedy"
    )
