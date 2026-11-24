"""Follow a Robinhood Schedule FA identity remedy, for the Schedule FA manual
action tests and the lookup-skip remedy tests.

``named_entity_keys`` reads the ``[[fa.entities]]`` keys an action line names,
and ``run_with_entity_block`` runs a Robinhood command on #1243's two-account
bundle with a given ``[[fa.entities]]`` block over the given automatic identity
and nature evidence.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa.compute.robinhood_entity_nature import (
    RobinhoodEntityNatureEvidence,
    merge_robinhood_entity_natures,
)
from ibkr_fa.config import _KNOWN_FA_ENTITY as ENTITY_KEYS
from ibkr_fa.config import FaEntityOverride, RobinhoodConfig
from ibkr_fa.normalize.robinhood import resolve_robinhood_security_identities
from tests._fa_cli_runs import CliRun, merge_entity_block, robinhood_run
from tests._robinhood_a3_load import load_two_accounts
from tests._robinhood_fa_inputs import fa_buy
from tests._robinhood_security import configured_securities


def named_entity_keys(action: str) -> list[str]:
    """The ``[[fa.entities]]`` keys ``action`` names, by whole word."""
    return sorted(key for key in ENTITY_KEYS if re.search(rf"\b{key}\b", action))


def run_with_entity_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    label: str,
    command: str,
    *extra: str,
    block: str = "",
    automatic: tuple[FaEntityOverride, ...] = (),
    natures: tuple[RobinhoodEntityNatureEvidence, ...] = (),
    etf: bool = False,
    adr: bool = False,
    issuer: str | None = None,
) -> CliRun:
    """``command`` on the #1243 two-account bundle (one buy of 2 shares, held at
    Dec 31) with ``automatic`` identity and nature evidence, the security an
    ETF if ``etf`` or a depositary receipt if ``adr``, and the buy's issuer
    name ``issuer`` if given. Its user.toml adds ``block``, which is loaded and
    merged over the automatic evidence as every Robinhood run does."""
    run_dir = tmp_path / label
    run_dir.mkdir()
    buy = fa_buy("2") if issuer is None else replace(fa_buy("2"), issuer_name=issuer)
    bundle = load_two_accounts(monkeypatch, run_dir, buy)
    if etf or adr:
        kind: dict[str, Any] = {"sub_category": "ETF"} if etf else {"is_depositary_receipt": True}
        security = replace(bundle.robinhood.securities[0], **kind)
        bundle.robinhood = RobinhoodConfig(
            accounts=bundle.robinhood.accounts, securities=(security,)
        )
        bundle.securities = configured_securities(bundle.robinhood)
        bundle.identities = resolve_robinhood_security_identities(bundle.paired, (security,))
    merge_entity_block(bundle, run_dir, block, automatic)
    bundle.nature_by_isin = merge_robinhood_entity_natures(natures, bundle.config.fa.entities)
    return robinhood_run(run_dir, capsys, command, *extra)
