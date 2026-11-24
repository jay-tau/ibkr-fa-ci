"""#1570: while an identity or lot blocker withholds Robinhood's equity CG,
``robinhood-cg``'s refusal also names the stops the run already knows.

These are the filing workbook's stops on the sales already resolved (a positive
LTCG line, a lot on an unreviewed venue) and, in FY 2024-25, the pre-cutover
stop of a blocked sale made before 23 July 2024. On 1d77505 the refusal named
only the identity guidance or the lot cause, and the rerun after following
the identity guidance stopped at one of these.

The command cases run the real account reconciliation, identity resolution
and CG compute over synthetic statements and activity reports through the
CLI or combined pipeline, behind the pipeline stubs #1550's tests use.
ABCD (US99971T1034), the CG tests' traded security, is configured whenever
traded; XQBB (US99995B1044) is configured only where a case says so. Trades
take managed activity rows 3 onward, in date order.

``robinhood-income`` reprints ``robinhood-cg``'s lines for the CG blockers in
its FTC refusal (#1545). It never builds the filing workbook, so it gains no
filing line, but it gains the pre-cutover line, which blocks its FTC
schedules too.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import ibkr_fa.compute.robinhood_capital_gains as cg_mod
import ibkr_fa.compute.schedule_cg_filing as filing_mod
import ibkr_fa.pipeline_robinhood as pipeline
from ibkr_fa import cli
from ibkr_fa.compute.robinhood_capital_gains import (
    RobinhoodCapitalGainsBlocker,
    RobinhoodCapitalGainsBlockerKind,
    RobinhoodCapitalGainsResult,
    compute_robinhood_capital_gains,
)
from ibkr_fa.config import RobinhoodConfig, RobinhoodSecurityConfig, load_user_config
from ibkr_fa.exceptions import ConfigError
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountsReconciliation,
    RobinhoodSecurityEventReference,
    reconcile_robinhood_accounts,
    resolve_robinhood_security_identities,
)
from ibkr_fa.parser.robinhood import RobinhoodEventKind
from ibkr_fa.pipeline_robinhood import run_robinhood_all
from ibkr_fa.pipeline_robinhood_bundle import identity_failure_guidance
from ibkr_fa.pipeline_robinhood_models import RobinhoodLegStatus
from tests._fx_stubs import AuditedFxStub
from tests._robinhood_a3_load import a3_bundle
from tests._robinhood_account_inputs import accounts_pair as _reconciliation_pair
from tests._robinhood_account_inputs import statement_bundle as _reconciliation_bundle
from tests._robinhood_account_inputs import statement_position as _statement_position
from tests._robinhood_account_inputs import statement_row as _statement_row
from tests._robinhood_cg import (
    CG_FTC_CAUSE,
    FILING_50CA_NOTE,
    FILING_LTCG_KIND,
    FILING_LTCG_ROUTE,
    FILING_VENUE_KIND,
    FILING_VENUE_ROUTE,
    SAME_DAY_CAUSE,
    SAME_DAY_ROUTE,
    cg_result,
    sale,
)
from tests._robinhood_cg import CG_UNRESOLVED as _CG
from tests._robinhood_cg import PRE_CUTOVER_CAUSE as _PRE_CUTOVER
from tests._robinhood_cg_inputs import CG_CUSIP as _CUSIP
from tests._robinhood_cg_inputs import CG_ISIN as _ISIN
from tests._robinhood_cg_inputs import CG_SYMBOL as _SYMBOL
from tests._robinhood_cg_inputs import cg_trade as _trade
from tests._robinhood_cli import robinhood_argv
from tests._robinhood_income_stubs import (  # noqa: F401 - autouse fixture
    silenced_default_income_warnings as _silence_default_income_warnings,
)
from tests._robinhood_refusals import EVENT_REFERENCES
from tests._robinhood_remedies import filled_guidance
from tests._robinhood_security import (
    IDENTITY_GUIDANCE_FILE,
    configured_securities,
    identity_guidance_line,
    robinhood_security,
)
from tests._robinhood_toml import (
    robinhood_account_toml,
    robinhood_security_toml,
    robinhood_toml,
    write,
)

type _Security = tuple[str, str, str]
type _Trade = tuple[_Security, tuple[date, str, str, str]]

_ABCD: _Security = (_SYMBOL, _CUSIP, str(_ISIN))
_XQBB: _Security = ("XQBB", "99995B104", "US99995B1044")
_BLOCKED = "Schedule CG filing is blocked: "
_LTCG = (f"{_BLOCKED}{FILING_LTCG_KIND}", FILING_LTCG_ROUTE)
# ABCD's sale is row 1:5 in the identity remedy cases.
_VENUE = (f"{_BLOCKED}{FILING_VENUE_KIND}", FILING_VENUE_ROUTE.format(sales="1:5"))
_SAME_DAY = f"{SAME_DAY_CAUSE}{EVENT_REFERENCES}sales {{sales}}{SAME_DAY_ROUTE}"
_SUFFIX = " (context keys: reasons)"
# robinhood-cg ends its refusal with the section 50CA note while any sale is
# resolved, as on 1d77505.
_FILING_NOTE = FILING_50CA_NOTE

_FY_2025 = FinancialYear(2025)
_FY_2024 = FinancialYear(2024)
# One share bought for $10.00 and sold for $12.00 on 13 June 2025 (row 1:5):
# long-term from May 2023, short-term from May 2025.
_ABCD_LONG = [
    (_ABCD, (date(2023, 5, 1), "1.0000", "10.00", "-10.00")),
    (_ABCD, (date(2025, 6, 13), "1.0000", "12.00", "12.00")),
]
_ABCD_SHORT = [
    (_ABCD, (date(2025, 5, 1), "1.0000", "10.00", "-10.00")),
    (_ABCD, (date(2025, 6, 13), "1.0000", "12.00", "12.00")),
]
# One XQBB share bought in May 2025 (row 1:4) and sold in June (row 1:6).
_XQBB_SHORT = [
    (_XQBB, (date(2025, 5, 2), "1.0000", "10.00", "-10.00")),
    (_XQBB, (date(2025, 6, 16), "1.0000", "12.00", "12.00")),
]
# One XQSE share bought and sold after XQBB's sale (rows 1:7 and 1:8): a
# short-term sale that is resolved whether or not XQBB is configured.
_XQSE: _Security = ("XQSE", "99995A105", "US99995A1052")
_XQSE_SHORT = [
    (_XQSE, (date(2025, 6, 17), "1.0000", "10.00", "-10.00")),
    (_XQSE, (date(2025, 6, 20), "1.0000", "12.00", "12.00")),
]
# FY 2024-25: one XQBB share bought in May 2024 (row 1:3), sold in June (row 1:4).
_XQBB_PRE_CUTOVER = [
    (_XQBB, (date(2024, 5, 1), "1.0000", "10.00", "-10.00")),
    (_XQBB, (date(2024, 6, 14), "1.0000", "12.00", "12.00")),
]


def _same_day(buy: date, sold: date) -> list[_Trade]:
    """#1546's same-day lots in XQBB: buys at $10 and $11 on ``buy``, then
    half a share sold on ``sold``."""
    return [
        (_XQBB, (buy, "1.0000", "10.00", "-10.00")),
        (_XQBB, (buy, "1.0000", "11.00", "-11.00")),
        (_XQBB, (sold, "0.5000", "12.00", "6.00")),
    ]


# What a filer who reviewed XQBB's evidence writes for each placeholder.
_REVIEWED = {
    "isin": f'"{_XQBB[2]}"',
    "listing_exchange": '"NASDAQ"',
    "sub_category": '"COMMON"',
    "asset_class": '"STK"',
    "issuer_country_code": '"US"',
    "is_depositary_receipt": "false",
    "source": '"synthetic issuer evidence"',
}


def _paired(
    trades: list[_Trade], financial_year: FinancialYear
) -> tuple[RobinhoodAccountsReconciliation, Any]:
    """Both accounts, open from 1 January 2023 to at least the FY's end, with
    ``trades`` in the managed account, as ``_traded_result`` builds one
    security's. Its statement closes holding what the trades leave at $10."""
    bundles, reports, accounts = _reconciliation_pair()
    start = date(2023, 1, 1)
    end = max(financial_year.end_date, *(report.end_date for report in reports))
    accounts = tuple(replace(account, opening_date=start) for account in accounts)
    events, rows = [], []
    held = dict.fromkeys((security[0] for security, _trade_fields in trades), Decimal())
    ordered = sorted(trades, key=lambda trade: trade[1][0])
    for ordinal, ((symbol, cusip, _isin), (day, quantity, price, amount)) in enumerate(
        ordered, start=3
    ):
        buy = amount.startswith("-")
        kind = (
            RobinhoodEventKind.MANAGED_EQUITY_BUY if buy else RobinhoodEventKind.MANAGED_EQUITY_SELL
        )
        event = _trade(kind, ordinal, day, quantity, price, amount)
        events.append(
            replace(event, instrument=symbol, cusip=cusip, description=f"Synthetic {symbol}")
        )
        rows.append(
            _statement_row(
                row=ordinal,
                code="Buy" if buy else "Sell",
                transaction_date=day,
                symbol=symbol,
                cusip=cusip,
                quantity=Decimal(quantity),
                price=Decimal(price),
                debit=-Decimal(amount) if buy else None,
                credit=None if buy else Decimal(amount),
            )
        )
        held[symbol] += Decimal(quantity) if buy else -Decimal(quantity)
    positions = tuple(
        _statement_position(row=row, symbol=symbol, quantity=quantity)
        for row, (symbol, quantity) in enumerate(held.items(), start=1)
        if quantity
    )
    individual, managed = (bundle.statements[0] for bundle in bundles)
    summary = managed.account_summary
    if positions:
        summary = replace(
            summary,
            closing_total_securities_usd=sum((item.quantity * 10 for item in positions), Decimal()),
        )
    paired = reconcile_robinhood_accounts(
        (
            _reconciliation_bundle(replace(individual, period_start=start, period_end=end)),
            _reconciliation_bundle(
                replace(
                    managed,
                    period_start=start,
                    period_end=end,
                    activities=(*managed.activities, *rows),
                    positions=positions or managed.positions,
                    account_summary=summary,
                )
            ),
        ),
        (
            replace(reports[0], start_date=start, end_date=end),
            replace(
                reports[1], events=(*reports[1].events, *events), start_date=start, end_date=end
            ),
        ),
        accounts,
    )
    return paired, accounts


def _security(security: _Security, listing_exchange: str = "NASDAQ") -> RobinhoodSecurityConfig:
    symbol, cusip, isin = security
    return robinhood_security(
        symbol=symbol,
        cusip=cusip,
        isin=isin,
        listing_exchange=listing_exchange,
        source="synthetic issuer evidence",
    )


def _load(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    trades: list[_Trade],
    financial_year: FinancialYear,
    *securities: RobinhoodSecurityConfig,
) -> Any:
    """Make every command load ``trades`` with ``securities`` configured, as
    #1550's ``_load`` does for one security. Returns the bundle."""
    paired, accounts = _paired(trades, financial_year)
    bundle = a3_bundle(paired, RobinhoodConfig(accounts=accounts, securities=securities), run_dir)
    bundle.identity_resolution = None
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    return bundle


def _user_toml(run_dir: Path, bundle: Any, block: str) -> Path:
    """A user.toml with an entry for each configured security, then ``block``."""
    configured = "".join(
        "\n"
        + robinhood_security_toml(
            symbol=item.instrument.symbol,
            cusip=item.cusip,
            isin=item.instrument.isin,
            listing_exchange=item.instrument.listing_exchange,
        )
        for item in bundle.robinhood.securities
    )
    account = robinhood_account_toml(
        number="999999999", opening_date="2020-01-01", source="synthetic statement"
    )
    return write(run_dir, robinhood_toml(first=account, extra=f"{configured}\n{block}"))


def _follow(run_dir: Path, bundle: Any, guidance: Path) -> None:
    """Follow the identity guidance word for word: add the private file's
    block, placeholders replaced, to user.toml, and load it."""
    followed = run_dir / "followed"
    followed.mkdir()
    block = filled_guidance(guidance, _REVIEWED)
    robinhood = load_user_config(_user_toml(followed, bundle, block)).robinhood
    assert robinhood is not None
    bundle.robinhood = replace(bundle.robinhood, securities=robinhood.securities)
    bundle.securities = configured_securities(bundle.robinhood)
    bundle.identities = resolve_robinhood_security_identities(bundle.paired, robinhood.securities)


def _run(
    command: str,
    run_dir: Path,
    financial_year: FinancialYear,
    capsys: pytest.CaptureFixture[str],
) -> tuple[str, str]:
    """``command``'s stdout and stderr; it exits 4 and writes no Schedule CG."""
    capsys.readouterr()
    code = cli.main(
        robinhood_argv(command, run_dir, financial_year=str(financial_year), no_fetch_prices=True)
    )
    assert code == 4
    assert not (run_dir / "out" / "schedule-cg.xlsx").exists()
    captured = capsys.readouterr()
    return captured.out, captured.err


def _equity_leg(run_dir: Path, financial_year: FinancialYear) -> str:
    """``robinhood-all``'s failed equity-cg leg reason; no workbook is written."""
    report = run_robinhood_all(
        input_dir=run_dir / "bundle",
        config_path=run_dir / "user.toml",
        output_dir=run_dir / "all",
        financial_year=financial_year,
        itr_form="ITR-2",
        fetch_prices=False,
    )
    leg = next(leg for leg in report.legs if leg.name == "equity-cg")
    assert leg.status is RobinhoodLegStatus.ERROR
    assert not (run_dir / "all" / "schedule-cg.xlsx").exists()
    (reason,) = leg.reasons
    return reason


def _refusal(
    command: str, run_dir: Path, financial_year: FinancialYear, capsys: pytest.CaptureFixture[str]
) -> str:
    """``robinhood-cg``'s refusal, or robinhood-all's equity-cg leg reason."""
    if command == "robinhood-all":
        return _equity_leg(run_dir, financial_year)
    _out, err = _run(command, run_dir, financial_year, capsys)
    assert err.startswith("error: ")
    return err.removeprefix("error: ").removesuffix("\n")


# Each issue example's trades, ABCD's venue (``None``: not traded), FY, the
# lines the first refusal names before (``ahead``) and after the guidance
# line (``after``), and the rerun's refusal once the guidance is followed.
_ITEMS: dict[str, tuple[Any, ...]] = {
    "positive-ltcg": (
        [*_ABCD_LONG, *_XQBB_SHORT],
        "NASDAQ",
        _FY_2025,
        (),
        _LTCG,
        f"InvariantError: {'\n'.join(_LTCG)}{_SUFFIX}",
    ),
    "unreviewed-venue": (
        [*_ABCD_SHORT, *_XQBB_SHORT],
        "NYSE AMERICAN",
        _FY_2025,
        (),
        _VENUE,
        f"InvariantError: {'\n'.join(_VENUE)}{_SUFFIX}",
    ),
    "pre-cutover": (
        _XQBB_PRE_CUTOVER,
        None,
        _FY_2024,
        (_PRE_CUTOVER.format(sales="1:4"),),
        (),
        f"{_CG}\n{_PRE_CUTOVER.format(sales='1:4 (lots 1:3)')}",
    ),
}


@pytest.mark.parametrize("command", ["robinhood-cg", "robinhood-all"])
@pytest.mark.parametrize(
    ("trades", "venue", "financial_year", "ahead", "after", "rerun"),
    _ITEMS.values(),
    ids=_ITEMS.keys(),
)
def test_following_the_identity_guidance_reaches_only_a_stop_it_named(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    trades: list[_Trade],
    venue: str | None,
    financial_year: FinancialYear,
    ahead: tuple[str, ...],
    after: tuple[str, ...],
    rerun: str,
) -> None:
    """The issue's examples: XQBB, unconfigured, sold beside ABCD's long-term
    sale or ABCD's sale on a venue the filing position hasn't reviewed, or
    sold alone in FY 2024-25 before the cutover. The first refusal names the
    stop beside the guidance; following the guidance word for word, the
    rerun stops at exactly that stop. On 1d77505 the first refusal named the
    guidance alone."""
    securities = () if venue is None else (_security(_ABCD, venue),)
    bundle = _load(monkeypatch, tmp_path, trades, financial_year, *securities)
    out = tmp_path / ("all" if command == "robinhood-all" else "out")
    first = "\n".join((_CG, *ahead, identity_guidance_line(out / IDENTITY_GUIDANCE_FILE), *after))
    note = f" {_FILING_NOTE}" if command == "robinhood-cg" and securities else ""

    assert _refusal(command, tmp_path, financial_year, capsys) == f"{first}{note}"

    _follow(tmp_path, bundle, out / IDENTITY_GUIDANCE_FILE)

    assert _refusal(command, tmp_path, financial_year, capsys) == rerun


# Each lot-blocker case's trades, FY and the lines its refusal names.
_LOT_CASES: dict[str, tuple[Any, ...]] = {
    # ABCD's long-term sale (row 1:6) beside XQBB's same-day lots (rows 1:4,
    # 1:5) and sale (row 1:7).
    "positive-ltcg": (
        [*_ABCD_LONG, *_same_day(date(2025, 5, 2), date(2025, 6, 16))],
        _FY_2025,
        (_SAME_DAY.format(sales="1:7 (lots 1:4, 1:5)"), *_LTCG),
    ),
    "pre-cutover": (
        _same_day(date(2024, 5, 1), date(2024, 6, 14)),
        _FY_2024,
        (_SAME_DAY.format(sales="1:5 (lots 1:3, 1:4)"), _PRE_CUTOVER.format(sales="1:5")),
    ),
}


@pytest.mark.parametrize("command", ["robinhood-cg", "robinhood-all"])
@pytest.mark.parametrize(
    ("trades", "financial_year", "lines"), _LOT_CASES.values(), ids=_LOT_CASES.keys()
)
def test_a_lot_blocker_names_the_stops_the_run_already_knows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    trades: list[_Trade],
    financial_year: FinancialYear,
    lines: tuple[str, ...],
) -> None:
    """Every traded security is configured; XQBB's same-day lots block its sale. The
    refusal names the lot cause, then the filing stop of ABCD's resolved
    long-term sale or, in FY 2024-25, the blocked sale's pre-cutover stop. On
    1d77505 it named the lot cause alone."""
    abcd = (_security(_ABCD),) if any(item[0] == _ABCD for item in trades) else ()
    _load(monkeypatch, tmp_path, trades, financial_year, *abcd, _security(_XQBB))
    note = f" {_FILING_NOTE}" if command == "robinhood-cg" and abcd else ""

    assert _refusal(command, tmp_path, financial_year, capsys) == "\n".join((_CG, *lines)) + note


def _ftc_actions(out: str) -> list[str]:
    """robinhood-income's ``Action:`` lines after the FTC refusal's own."""
    actions = [line.removeprefix("  Action: ") for line in out.splitlines() if "Action: " in line]
    assert actions[0].startswith("FTC schedules remain blocked")
    return actions[1:]


@pytest.mark.parametrize("command", ["robinhood-cg", "robinhood-all", "robinhood-income"])
@pytest.mark.parametrize("lot_blocked", [False, True], ids=["identity", "lot"])
def test_older_financial_year_keeps_its_existing_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    lot_blocked: bool,
) -> None:
    """The added pre-cutover line is limited to FY 2024-25. Earlier years
    keep their identity or lot refusal, including robinhood-income's text."""
    financial_year = FinancialYear(2023)
    securities = (_security(_XQBB),) if lot_blocked else ()
    trades = (
        _same_day(date(2023, 5, 1), date(2023, 6, 14))
        if lot_blocked
        else [
            (_XQBB, (date(2023, 5, 1), "1.0000", "10.00", "-10.00")),
            (_XQBB, (date(2023, 6, 14), "1.0000", "12.00", "12.00")),
        ]
    )
    _load(monkeypatch, tmp_path, trades, financial_year, *securities)
    out = tmp_path / ("all" if command == "robinhood-all" else "out")
    line = (
        _SAME_DAY.format(sales="1:5 (lots 1:3, 1:4)")
        if lot_blocked
        else identity_guidance_line(out / IDENTITY_GUIDANCE_FILE)
    )

    if command == "robinhood-income":
        stdout, _err = _run(command, tmp_path, financial_year, capsys)
        assert _ftc_actions(stdout) == [CG_FTC_CAUSE, _CG, line]
    else:
        assert _refusal(command, tmp_path, financial_year, capsys) == f"{_CG}\n{line}"


@pytest.mark.parametrize(
    ("trades", "venue", "financial_year", "ahead"),
    [item[:4] for item in _ITEMS.values()],
    ids=_ITEMS.keys(),
)
def test_robinhood_income_gains_only_the_pre_cutover_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    trades: list[_Trade],
    venue: str | None,
    financial_year: FinancialYear,
    ahead: tuple[str, ...],
) -> None:
    """robinhood-income's FTC refusal reprints robinhood-cg's CG lines but no
    filing stop: it files no Schedule CG workbook, and once the guidance is
    followed no filing stop blocks it. The pre-cutover line, which blocks its
    FTC schedules too, prints in FY 2024-25. On 1d77505 it printed neither."""
    securities = () if venue is None else (_security(_ABCD, venue),)
    _load(monkeypatch, tmp_path, trades, financial_year, *securities)

    out, _err = _run("robinhood-income", tmp_path, financial_year, capsys)

    assert _ftc_actions(out) == [
        CG_FTC_CAUSE,
        _CG,
        *ahead,
        identity_guidance_line(tmp_path / "out" / IDENTITY_GUIDANCE_FILE),
    ]


@pytest.mark.parametrize(
    ("borrowed", "refusal"),
    [
        pytest.param((2,), "US ISIN must contain the configured CUSIP", id="isin"),
        pytest.param((1, 2), "duplicates another configured security CUSIP", id="cusip-and-isin"),
    ],
)
def test_the_fixed_security_cannot_join_a_resolved_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, borrowed: tuple[int, ...], refusal: str
) -> None:
    """The guidance block carries XQBB's own CUSIP, symbol and, once filled,
    ISIN. user.toml refuses the block with ABCD's ISIN, or ABCD's CUSIP and
    ISIN, in their place, so following the guidance can't merge XQBB's sale
    into ABCD's lots or lines."""
    bundle = _load(monkeypatch, tmp_path, [*_ABCD_LONG, *_XQBB_SHORT], _FY_2025, _security(_ABCD))
    guidance = tmp_path / IDENTITY_GUIDANCE_FILE
    guidance.write_text(identity_failure_guidance(bundle), encoding="utf-8")
    block = filled_guidance(guidance, _REVIEWED)
    for field in borrowed:
        assert block.count(f'"{_XQBB[field]}"') == 1
        block = block.replace(f'"{_XQBB[field]}"', f'"{_ABCD[field]}"')
    (tmp_path / "own").mkdir()
    assert load_user_config(
        _user_toml(tmp_path / "own", bundle, filled_guidance(guidance, _REVIEWED))
    )

    with pytest.raises(ConfigError, match=refusal):
        load_user_config(_user_toml(tmp_path, bundle, block))


def test_resolving_the_blocked_sale_keeps_every_resolved_lot_and_line() -> None:
    """Each filing check reads one resolved lot or line, so a stop named
    while XQBB's sale is blocked still applies once it resolves: every lot
    and line of the blocked result, ABCD's long-term and XQSE's short-term,
    is in the resolved one, and the resolved filing refuses with exactly the
    text the blocked result names."""
    paired, accounts = _paired([*_ABCD_LONG, *_XQBB_SHORT, *_XQSE_SHORT], _FY_2025)

    def compute(*securities: RobinhoodSecurityConfig) -> RobinhoodCapitalGainsResult:
        config = RobinhoodConfig(accounts=accounts, securities=securities)
        return compute_robinhood_capital_gains(
            paired,
            config=config,
            securities=configured_securities(config),
            financial_year_start=_FY_2025.start_date,
            financial_year_end=_FY_2025.end_date,
            fx_policy=AuditedFxStub(Decimal(85)),
        )

    abcd, xqse = _security(_ABCD, "NYSE AMERICAN"), _security(_XQSE)
    blocked, resolved = compute(abcd, xqse), compute(abcd, xqse, _security(_XQBB))

    assert [item.kind for item in blocked.blockers] == [RobinhoodCapitalGainsBlockerKind.IDENTITY]
    assert resolved.blockers == ()
    assert len(blocked.attributed_lots) == 2
    assert len(resolved.attributed_lots) == 3
    assert all(item in resolved.attributed_lots for item in blocked.attributed_lots)
    assert all(line in resolved.ltcg.ltcg_lines for line in blocked.ltcg.ltcg_lines)
    assert blocked.stcg.stcg_lines
    assert all(line in resolved.stcg.stcg_lines for line in blocked.stcg.stcg_lines)
    filing = resolved.filing(())
    assert filing is not None
    assert [blocker.kind.value for blocker in filing.blockers] == [
        "unreviewed-listing-venue",
        "section-54-decision-pending",
    ]
    assert cg_mod.resolved_filing_reasons(blocked) == (
        filing_mod.schedule_cg_filing_blocked_text(filing.blockers),
    )
    assert cg_mod.resolved_filing_reasons(blocked) == (
        f"{_BLOCKED}{FILING_VENUE_KIND}; {FILING_LTCG_KIND}\n{_VENUE[1]}\n{FILING_LTCG_ROUTE}",
    )


@pytest.mark.parametrize("kind", RobinhoodCapitalGainsBlockerKind)
def test_only_identity_and_lot_blockers_add_the_filing_stops(
    kind: RobinhoodCapitalGainsBlockerKind,
) -> None:
    """A resolved long-term sale with a positive LTCG line, beside one blocker
    of ``kind``. Only an identity or lot blocker adds the filing stop; beside
    any other kind the refusal is byte-identical to 1d77505's."""
    blocker = RobinhoodCapitalGainsBlocker(
        kind=kind,
        account_index=0,
        disposal_references=(RobinhoodSecurityEventReference(0, 9),),
    )
    result = replace(cg_result(sale("10", "12", long_term=True)), blockers=(blocker,))

    expected = (
        (f"{_BLOCKED}{FILING_LTCG_KIND}\n{FILING_LTCG_ROUTE}",)
        if kind
        in {
            RobinhoodCapitalGainsBlockerKind.IDENTITY,
            RobinhoodCapitalGainsBlockerKind.LOT_ATTRIBUTION,
        }
        else ()
    )
    assert cg_mod.resolved_filing_reasons(result) == expected
