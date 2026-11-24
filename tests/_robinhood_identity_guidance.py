"""Stand-ins for a Robinhood run that refuses for one unconfigured security,
shared by the Robinhood no-echo, FA blocker-reason and report-render cleanup
tests.

``unconfigured_paired`` returns a stand-in paired-accounts result whose one
account has one activity event holding a security with no configured identity,
and ``guidance_bundle`` a stand-in bundle holding that result (or the given
one) and no security identities. ``stub_identity_refusals`` stubs a successful
run, then loads that bundle (or the given one) and makes capital gains refuse
with an identity blocker and the FA inventory gate refuse with one inventory
blocker, whose lines ``inventory_refusal`` returns. ``assert_guidance_file``
checks that the private identity guidance file such a run leaves in its output
folder is a regular file with mode 0600 and one security entry, with the given
symbol and the CUSIP from ``tests/_no_echo.py``.
"""

from __future__ import annotations

import stat
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
import ibkr_fa.pipeline_robinhood_core as core
from ibkr_fa.compute.robinhood_capital_gains import RobinhoodCapitalGainsBlockerKind
from ibkr_fa.compute.robinhood_security_identities import RobinhoodSecurityIdentities
from ibkr_fa.normalize.robinhood.inventory import (
    RobinhoodInventoryBlocker,
    RobinhoodInventoryBlockerKind,
    RobinhoodInventoryUnprovedFact,
)
from tests._no_echo import CUSIP, HOLDER
from tests._robinhood_fa_blocker_lines import ANCHOR, INVENTORY_INCOMPLETE, REFERENCES
from tests._robinhood_pipeline_stubs import (
    complete_fa,
    stand_in_bundle,
    stand_in_cg,
    stub_successful_all,
)


def unconfigured_paired(symbol: str = HOLDER, cusip: str = CUSIP) -> SimpleNamespace:
    """One account whose one activity event holds an unconfigured security."""
    event = SimpleNamespace(cusip=cusip, instrument=symbol, source_record_index=1)
    return SimpleNamespace(
        account_labels={0: "Individual", 1: "Managed Individual"},
        accounts=(
            SimpleNamespace(
                evidence=SimpleNamespace(events=(event,)),
                activity=SimpleNamespace(
                    primary_bundle=SimpleNamespace(statements=()), cusip_transition_uses=()
                ),
            ),
        ),
    )


def guidance_bundle(paired: SimpleNamespace | None = None) -> SimpleNamespace:
    bundle = stand_in_bundle()
    bundle.paired = unconfigured_paired() if paired is None else paired
    bundle.securities = RobinhoodSecurityIdentities(())
    bundle.identity_resolution = None
    return bundle


def _identity_cg() -> SimpleNamespace:
    return stand_in_cg(
        blockers=(
            SimpleNamespace(
                kind=RobinhoodCapitalGainsBlockerKind.IDENTITY,
                source_lot_blockers=(),
                source_identity_blockers=(object(),),
            ),
        )
    )


def stub_identity_refusals(
    monkeypatch: pytest.MonkeyPatch, bundle: SimpleNamespace | None = None
) -> None:
    """Stub a run whose CG and FA both refuse for the unconfigured security."""
    stub_successful_all(monkeypatch, complete_fa())
    loaded = guidance_bundle() if bundle is None else bundle
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: loaded)
    monkeypatch.setattr(pipeline, "_compute_cg", lambda *_args: _identity_cg())
    monkeypatch.setattr(pipeline, "_fa_prices", core.fa_prices)
    monkeypatch.setattr(
        pipeline, "_fa_inventory", getattr(core, "fa_inventory", None), raising=False
    )
    monkeypatch.setattr(
        core,
        "resolved_robinhood_quantities",
        lambda _bundle: SimpleNamespace(blockers=(_INVENTORY_BLOCKER,)),
    )


# One inventory blocker; its line comes before the guidance line (#1550).
_INVENTORY_BLOCKER = RobinhoodInventoryBlocker(
    account_index=0,
    cusip=None,
    unproved_fact=RobinhoodInventoryUnprovedFact.QUANTITY,
    kind=RobinhoodInventoryBlockerKind.ANCHOR_MISMATCH,
    statement_index=0,
)


def inventory_refusal() -> str:
    """The inventory gate's lines for :data:`_INVENTORY_BLOCKER`."""
    line = ANCHOR.format(references=f"{REFERENCES}account_index 0 statement 1")
    return f"{INVENTORY_INCOMPLETE}\n{line}"


def assert_guidance_file(path: Path, symbol: str = HOLDER) -> None:
    assert stat.S_IMODE(path.lstat().st_mode) == 0o600
    assert stat.S_ISREG(path.lstat().st_mode)
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    (entry,) = document["robinhood"]["securities"]
    assert (entry["symbol"], entry["cusip"]) == (symbol, CUSIP)
