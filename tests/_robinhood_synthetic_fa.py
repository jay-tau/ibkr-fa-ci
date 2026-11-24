"""The synthetic Robinhood bundle and the ``robinhood-all`` stubs that the
Schedule FA price tests run (#1715).

Robinhood statements are PDFs, so these tests make ``_load`` return a bundle
of the reconciled synthetic accounts ``tests._robinhood_fa_inputs`` builds,
holding its one configured equity.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import ibkr_fa.pipeline_robinhood as robinhood_pipeline
import ibkr_fa.pipeline_robinhood_core as robinhood_core
from ibkr_fa.config import RobinhoodAccountConfig, load_user_config
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountsReconciliation,
    resolve_robinhood_security_identities,
    resolve_robinhood_security_quantities,
)
from ibkr_fa.pipeline_robinhood_bundle import RobinhoodBundle, RobinhoodScopeStatus
from tests._robinhood_fa_inputs import fa_config
from tests._robinhood_income_stubs import silence_default_income_warnings
from tests._robinhood_pipeline_stubs import stand_in_options, stub_writing_all
from tests._robinhood_security import configured_securities


def synthetic_bundle(
    config_path: Path,
    paired: tuple[
        RobinhoodAccountsReconciliation, tuple[RobinhoodAccountConfig, RobinhoodAccountConfig]
    ],
) -> RobinhoodBundle:
    """The bundle of ``paired``, the reconciled accounts and the account
    configs ``fa_paired_events`` returns, with no 1042-S packet or crypto
    statement; its user config is ``config_path``'s."""
    reconciled, accounts = paired
    config = fa_config(accounts)
    identities = resolve_robinhood_security_identities(reconciled, config.securities)
    return RobinhoodBundle(
        config=replace(load_user_config(config_path), robinhood=config),
        robinhood=config,
        securities=configured_securities(config),
        paired=reconciled,
        identities=identities,
        packets=(),
        quantities=resolve_robinhood_security_quantities(
            reconciled, identities, config.security_receipt_resolutions
        ),
        scope=RobinhoodScopeStatus(crypto_statement_count=0),
    )


def stub_all_but_fa(monkeypatch: pytest.MonkeyPatch, output_dir: Path) -> list[Path]:
    """Stub a ``robinhood-all`` run's writers and its legs other than Schedule
    FA with ``stub_writing_all``, then restore the real Schedule FA pricing
    and computation. Returns what ``stub_writing_all`` returns: every path its
    writers write."""
    # What the stubbed income and OS legs need (the autouse fixture
    # silenced_default_income_warnings). It also stubs _write_os, so it runs
    # first and stub_writing_all's recording writer replaces that stub.
    silence_default_income_warnings(monkeypatch)
    written = stub_writing_all(monkeypatch, output_dir)
    # No option trades, so no option price is needed; the FA leg is real.
    monkeypatch.setattr(robinhood_pipeline, "_compute_options", lambda *_args: stand_in_options())
    monkeypatch.setattr(robinhood_pipeline, "_fa_prices", robinhood_core.fa_prices)
    monkeypatch.setattr(robinhood_pipeline, "_compute_fa", robinhood_core.compute_fa)
    return written
