"""The ``netbenefits-fa`` lot-proof bundle the lot-proof and exact-close pipeline
tests share: a Stock Plan Services report's text with its ESPP purchase rows, a
bundle's inputs, laying the bundle out with ``pdftotext`` stubbed, and a run of
``run_netbenefits_fa`` on it. Every value is synthetic.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

import pytest

from ibkr_fa.pipeline_netbenefits_fa import run_netbenefits_fa
from tests import _netbenefits_statement_pdf as statements
from tests._netbenefits import (
    CLOSED_EMPTY_CSV,
    CONF_DEC,
    OPEN_CSV,
    PURCHASE_ROW_NAME,
    patch_pdftotext,
    purchase_cash,
    purchase_row,
    spp_credit,
)
from tests._netbenefits_fa import BASE_CONFIG

# The purchase rows' default identifier, a privacy marker.
LOT_PROOF_CUSIP = "99999E104"
_SPS = "STOCK PLAN SERVICES REPORT"
LOT_PROOF_DEC_PERIOD = "December 1, 2025 - December 31, 2025"
LOT_PROOF_JAN_PERIOD = "January 1, 2026 - January 31, 2026"


class LotProofRow(NamedTuple):
    """One ESPP purchase row in Fidelity's layout (#1149); by default ``CONF_DEC``'s."""

    day: str = "01/05"
    quantity: str = "3.482"
    price: str = "192.12000"
    amount: str = "668.96"
    identifier: str = LOT_PROOF_CUSIP

    def line(self) -> str:
        return purchase_row(
            day=self.day,
            quantity=self.quantity,
            price=self.price,
            amount=self.amount,
            identifier=self.identifier,
        )


LOT_PROOF_OWN_ROW = LotProofRow()


def _purchases(rows: tuple[LotProofRow, ...]) -> str:
    """The ``Securities Bought & Sold`` table holding ``rows`` and the purchase cash
    that pays for them, one credit per row on its settlement date."""
    total = sum((Decimal(row.amount) for row in rows), Decimal(0))
    return purchase_cash(
        "".join(row.line() for row in rows),
        "".join(spp_credit(day=row.day, amount=row.amount) for row in rows),
        total=f"{total}",
        legend="t   Purchase of stock through your company stock plan.\n",
    )


def _priced(holdings: tuple[tuple[str, str, str], ...]) -> str:
    """A priced body whose Holdings table lists each ``(symbol, quantity, price)``."""
    rows = ""
    total = Decimal(0)
    for symbol, quantity, price in holdings:
        ending = (Decimal(quantity) * Decimal(price)).quantize(Decimal("0.01"))
        rows += (
            f"{PURCHASE_ROW_NAME} ({symbol})   unavailable   {quantity}   ${price}   ${ending}   "
            f"${ending}   $0.00   $0.00\n"
        )
        total += ending
    return (
        f"       Your Stock Plan Account Value:                 ${total}\n"
        f"          Ending Account Value Incl. AI               ${total}\n"
        "Holdings\nStocks\nCommon Stock\n"
        + rows
        + f"Total Stocks (100% of account holdings)   unavailable   ${total}   ${total}   "
        "$0.00   $0.00\n"
        f"Total Holdings   ${total}   ${total}   $0.00   $0.00\n"
    )


def lot_proof_report(
    period: str,
    *rows: LotProofRow,
    holdings: tuple[tuple[str, str, str], ...] | None = None,
    body: str | None = None,
    title: str = _SPS,
) -> str:
    """A report for ``period`` printing ``rows``: unpriced, unless ``holdings`` or a
    priced ``body`` is given."""
    if body is None:
        body = statements.UNPRICED_BODY if holdings is None else _priced(holdings)
    return statements.statement_text(title, period, body=body + (_purchases(rows) if rows else ""))


class LotProofBundle(NamedTuple):
    """A bundle's inputs: report and confirmation text by file name, the lots CSVs and
    the config."""

    reports: dict[str, str]
    confirmations: dict[str, str]
    open_csv: str = OPEN_CSV
    closed_csv: str = CLOSED_EMPTY_CSV
    config: str = BASE_CONFIG


def lot_proof_bundle(
    *reports: str,
    confirmations: tuple[str, ...] = (),
    dec31: str = CONF_DEC,
    **inputs: str,
) -> LotProofBundle:
    """``reports`` in period order (named ``a.pdf``, ``b.pdf``, ...), with the Dec-31
    confirmation ``dec31`` and any other ``confirmations``."""
    return LotProofBundle(
        {f"{chr(ord('a') + index)}.pdf": text for index, text in enumerate(reports)},
        {f"conf-{index}.pdf": text for index, text in enumerate((dec31, *confirmations))},
        **inputs,
    )


def write_lot_proof_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bundle: LotProofBundle
) -> list[str]:
    """Lay ``bundle`` out and stub ``pdftotext``; return the CLI arguments that run
    ``netbenefits-fa`` on it with ``out/a3.csv`` as its output."""
    directory = tmp_path / "bundle"
    directory.mkdir()
    (directory / "View open lots.csv").write_text(bundle.open_csv, encoding="utf-8")
    (directory / "View closed lots.csv").write_text(bundle.closed_csv, encoding="utf-8")
    texts = {**bundle.reports, **bundle.confirmations}
    for name in texts:
        (directory / name).write_bytes(b"%PDF-1.4 placeholder")
    patch_pdftotext(monkeypatch, texts)
    config = tmp_path / "user.toml"
    config.write_text(bundle.config, encoding="utf-8")
    (tmp_path / "out").mkdir()
    return [
        "netbenefits-fa",
        *("--input", str(directory), "--config", str(config)),
        *("--output", str(tmp_path / "out" / "a3.csv"), "--calendar-year", "2025"),
        *("--no-fetch-prices", "--price-cache-dir", str(tmp_path / "cache")),
    ]


def run_lot_proof_bundle(tmp_path: Path) -> object:
    return run_netbenefits_fa(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_path=tmp_path / "out" / "a3.csv",
        calendar_year=2025,
        fetch_prices=False,
        price_cache_dir=tmp_path / "cache",
    )
