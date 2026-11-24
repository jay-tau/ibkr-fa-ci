"""Following a printed Robinhood remedy word for word: the config entries the
remedy tests build from a remedy or identity guidance file, and the real
loader's read of one entry. :func:`followed_remedy_entry` follows the
interest-payer and option-classification remedies (#1470) with its own
account and placeholder values; every other caller keeps its own values,
folders and schema-key checks."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ibkr_fa.config import load_user_config
from tests._robinhood_toml import robinhood_account_toml, robinhood_toml, write


def loaded_entry(folder: Path, account: str, table: str, entry: str) -> Any:
    """The one ``[[robinhood.<table>]]`` entry the real loader reads from
    ``entry``, written to ``folder``'s ``user.toml`` beside a configured
    account ``account``."""
    first = robinhood_account_toml(
        number=account, opening_date="2020-01-01", source="synthetic statement"
    )
    path = write(folder, robinhood_toml(first=first, extra=f"\n[[robinhood.{table}]]\n{entry}"))
    robinhood = load_user_config(path).robinhood
    assert robinhood is not None
    (loaded,) = getattr(robinhood, table)
    return loaded


FOLLOWED_REMEDY_ACCOUNT = "999999999"
# Each placeholder a remedy names, and what a filer writes in its place.
FOLLOWED_REMEDY_PLACEHOLDERS = {
    "<account number>": FOLLOWED_REMEDY_ACCOUNT,
    "<FY start year, e.g. 2025 for 2025-26>": "2025",
    '<each kind it earned: "brokerage", "program-bank">': '"brokerage"',
    "<what you reviewed>": "synthetic reviewed terms",
    "<why every payer is a US entity>": "the synthetic payer is a US entity",
    "<capital-gains, speculative-business or non-speculative-business>": "capital-gains",
    "<why that treatment applies>": "synthetic reviewed treatment",
}


def followed_remedy_entry(tmp_path: Path, remedy: str, table: str) -> tuple[Any, set[str]]:
    """Load the ``[[robinhood.<table>]]`` entry with every ``key = value``
    ``remedy`` names after the table (and the account it is for), each
    placeholder filled in, beside a configured account
    ``FOLLOWED_REMEDY_ACCOUNT``. Returns the loaded entry and the keys."""
    named = re.search(rf"\[\[robinhood\.{table}\]\] entry (?:for account \d+ )?with (.*)", remedy)
    assert named is not None, remedy
    fields = named[1]
    for placeholder in re.findall(r"<[^>]*>", fields):
        fields = fields.replace(placeholder, FOLLOWED_REMEDY_PLACEHOLDERS[placeholder])
    pairs = re.findall(r'(\w+) = ("[^"]*"|\[[^\]]*\]|\w+)', fields)
    entry = "".join(f"{key} = {value}\n" for key, value in pairs)
    return loaded_entry(tmp_path, FOLLOWED_REMEDY_ACCOUNT, table, entry), {
        key for key, _value in pairs
    }


def lending_fee_entry(remedy: str, filled: Mapping[str, str]) -> tuple[str, list[str]]:
    """The entry the lending-fee ``remedy`` spells out after "entry for the
    account with", each placeholder replaced by its ``filled`` value, and its keys."""
    fields = remedy.partition(" entry for the account with ")[2]
    for placeholder in re.findall(r"<[^>]*>", fields):
        fields = fields.replace(placeholder, filled[placeholder])
    pairs = re.findall(r'(\w+) = ("[^"]*"|\w+)', fields)
    return "".join(f"{key} = {value}\n" for key, value in pairs), [key for key, _value in pairs]


def security_entry(remedy: str, symbol: str, cusip: str, isin: str) -> tuple[str, list[str]]:
    """The entry with exactly the keys the unconfigured-security ``remedy``
    lists after "entry for it with", each given a synthetic reviewed value
    for ``symbol``, ``cusip`` and ``isin``, and those keys."""
    named = remedy.partition(" entry for it with ")[2]
    keys = re.split(r", and |, | and ", named.removesuffix(", and re-run"))
    values = {
        "symbol": f'"{symbol}"',
        "cusip": f'"{cusip}"',
        "isin": f'"{isin}"',
        "listing_exchange": '"NASDAQ"',
        "sub_category": '"COMMON"',
        "asset_class": '"STK"',
        "issuer_country_code": '"US"',
        "is_depositary_receipt": "false",
        "source": '"synthetic issuer filing"',
    }
    return "".join(f"{key} = {values[key]}\n" for key in keys), keys


def filled_guidance(path: Path, reviewed: Mapping[str, str]) -> str:
    """The identity guidance file ``path``'s block with each ``reviewed`` key's
    one ``"REPLACE"`` placeholder line replaced by its value, and no
    placeholder left."""
    block = path.read_text(encoding="utf-8")
    for key, value in reviewed.items():
        block, count = re.subn(rf'^{key} = "REPLACE".*$', f"{key} = {value}", block, flags=re.M)
        assert count == 1
    assert "REPLACE" not in block
    return block
