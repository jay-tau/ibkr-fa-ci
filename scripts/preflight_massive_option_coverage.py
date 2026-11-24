"""Privacy-safe Massive coverage preflight for Robinhood CY 2025 options."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests._fx_stubs import AuditedFxStub
from tests._private_data import private_dataset
from tests._robinhood_private import (
    load_private_accounts_reconciliation,
)

from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionContract,
    compute_robinhood_options,
)
from ibkr_fa.config import (
    RobinhoodAccountConfig,
    RobinhoodConfig,
    RobinhoodOptionClassificationConfig,
    RobinhoodOptionTreatment,
    TaxpayerStatus,
    load_user_config,
)
from ibkr_fa.normalize.robinhood.accounts import (
    RobinhoodAccountsReconciliation,
)
from ibkr_fa.reference.us_market_calendar import us_market_sessions

_CY: Final[int] = 2025
_START: Final[date] = date(_CY, 1, 1)
_END: Final[date] = date(_CY, 12, 31)
_FY_START: Final[date] = date(2025, 4, 1)
_FY_END: Final[date] = date(2026, 3, 31)
_NY: Final[ZoneInfo] = ZoneInfo("America/New_York")
_API_ROOT: Final[str] = "https://api.massive.com"
_MIN_REQUEST_INTERVAL_SECONDS: Final[float] = 12.1
_MAX_OSI_ROOT_LENGTH: Final[int] = 6
_HTTP_OK: Final[int] = 200
_HTTP_NOT_FOUND: Final[int] = 404
_HTTP_TOO_MANY_REQUESTS: Final[int] = 429


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _api_key() -> str:
    [config_path] = private_dataset("user.toml")
    key_filename = load_user_config(config_path).prices.massive_api_key_file
    if key_filename is None:
        return ""
    configured_path = Path(key_filename)
    path = (
        configured_path if configured_path.is_absolute() else config_path.parent / configured_path
    )
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "MASSIVE_API_KEY":
                return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _config(paired: RobinhoodAccountsReconciliation) -> RobinhoodConfig:
    accounts = tuple(
        RobinhoodAccountConfig(
            number=account.activity.primary_bundle.account_number,
            opening_date=account.evidence.start_date,
            status=TaxpayerStatus.BENEFICIAL_OWNER,
            institution_name="Robinhood Securities, LLC",
            address="private statement",
            zip_code="",
            country_iso2="US",
            same_beneficial_owner=True,
            source="private statement",
        )
        for account in paired.accounts
    )
    return RobinhoodConfig(
        accounts=accounts,
        option_classifications=tuple(
            RobinhoodOptionClassificationConfig(
                account_number=account.number,
                financial_year=2025,
                fema_risk_acknowledged=True,
                treatment=RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
                source="user-selected tax treatment",
                reason="private preflight",
            )
            for account in accounts
        ),
    )


def _sessions(start: date, end: date) -> tuple[date, ...]:
    return us_market_sessions(max(start, _START), min(end, _END))


def _osi(contract: RobinhoodOptionContract) -> str | None:
    root = contract.symbol
    if not root.isalpha() or root != root.upper() or len(root) > _MAX_OSI_ROOT_LENGTH:
        return None
    strike_millis = contract.strike_usd * 1000
    if strike_millis != strike_millis.to_integral_value():
        return None
    return f"O:{root}{contract.expiration:%y%m%d}C{int(strike_millis):08d}"


class _Client:
    def __init__(self, key: str) -> None:
        self._key = key
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._last_request_at = 0.0
        self.requests_made = 0
        self.http_429_count = 0

    def get(self, path: str, query: dict[str, str] | None = None) -> tuple[int, Any | None]:
        elapsed = time.monotonic() - self._last_request_at
        if self.requests_made and elapsed < _MIN_REQUEST_INTERVAL_SECONDS:
            time.sleep(_MIN_REQUEST_INTERVAL_SECONDS - elapsed)
        url = f"{_API_ROOT}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Accept": "application/json",
                "User-Agent": "ibkr-fa-private-option-preflight",
            },
        )
        self.requests_made += 1
        self._last_request_at = time.monotonic()
        try:
            with self._opener.open(request, timeout=30) as response:
                if response.geturl().split("/", 3)[:3] != ["https:", "", "api.massive.com"]:
                    return 0, None
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == _HTTP_TOO_MANY_REQUESTS:
                self.http_429_count += 1
            return exc.code, None
        except (OSError, ValueError):
            return 0, None


def _identity_ok(payload: Any, contract: RobinhoodOptionContract, ticker: str) -> bool:
    if not isinstance(payload, dict):
        return False
    result = payload.get("results")
    if not isinstance(result, dict):
        return False
    try:
        return (
            result["ticker"] == ticker
            and result["underlying_ticker"] == contract.symbol
            and date.fromisoformat(result["expiration_date"]) == contract.expiration
            and Decimal(str(result["strike_price"])) == contract.strike_usd
            and result["contract_type"] == "call"
            and Decimal(str(result["shares_per_contract"])) == contract.multiplier
            and result.get("correction") in (None, 0)
            and not result.get("additional_underlyings")
        )
    except (KeyError, TypeError, ValueError):
        return False


def _bars(payload: Any) -> tuple[dict[date, Decimal], int]:
    if not isinstance(payload, dict) or payload.get("status") != "OK":
        return {}, 0
    if payload.get("adjusted") is not False:
        return {}, 0
    results = payload.get("results")
    if not isinstance(results, list):
        return {}, 0
    valid: dict[date, Decimal] = {}
    nonpositive = 0
    for row in results:
        try:
            close = Decimal(str(row["c"]))
            volume = Decimal(str(row["v"]))
            transactions = int(row["n"])
            day = datetime.fromtimestamp(int(row["t"]) / 1000, _NY).date()
        except (KeyError, TypeError, ValueError):
            nonpositive += 1
            continue
        if (
            not _START <= day <= _END
            or not close.is_finite()
            or close <= 0
            or volume <= 0
            or transactions <= 0
        ):
            nonpositive += 1
            continue
        valid[day] = close
    return valid, nonpositive


def main() -> int:  # noqa: PLR0912, PLR0915
    key = _api_key()
    if not key:
        raise SystemExit("prices.massive_api_key_file does not provide a key")

    started = time.monotonic()
    paired = load_private_accounts_reconciliation()
    options = compute_robinhood_options(
        paired,
        config=_config(paired),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        capital_gains_fx_policy=AuditedFxStub(Decimal("1")),
        business_fx_policy=AuditedFxStub(Decimal("1")),
    )
    if options.blockers or options.open_lots:
        raise SystemExit("private option reconstruction is not complete")

    required_by_contract: dict[RobinhoodOptionContract, set[date]] = defaultdict(set)
    for lot in options.lot_slices:
        required_by_contract[lot.contract].update(
            _sessions(lot.open_date, lot.close_date - timedelta(days=1))
        )

    client = _Client(key)
    identity_ok = identity_failed = identity_required_as_of = 0
    bars_present = bars_missing = bars_nonpositive = 0
    contracts_with_missing = required_after_expiration = 0
    bars_by_contract: dict[RobinhoodOptionContract, dict[date, Decimal]] = {}

    for contract, required in sorted(required_by_contract.items()):
        ticker = _osi(contract)
        if ticker is None:
            identity_failed += 1
            continue
        path = f"/v3/reference/options/contracts/{urllib.parse.quote(ticker, safe=':')}"
        status, payload = client.get(path)
        if status == _HTTP_NOT_FOUND:
            status, payload = client.get(path, {"as_of": contract.expiration.isoformat()})
            if status == _HTTP_OK:
                identity_required_as_of += 1
        if status != _HTTP_OK or not _identity_ok(payload, contract, ticker):
            identity_failed += 1
            continue
        identity_ok += 1

        status, payload = client.get(
            (
                f"/v2/aggs/ticker/{urllib.parse.quote(ticker, safe=':')}"
                f"/range/1/day/{_START.isoformat()}/{_END.isoformat()}"
            ),
            {"adjusted": "false", "sort": "asc", "limit": "5000"},
        )
        bars, nonpositive = _bars(payload if status == _HTTP_OK else None)
        bars_nonpositive += nonpositive
        bars_by_contract[contract] = bars
        available = bars.keys()
        missing = required - available
        bars_present += len(required & available)
        bars_missing += len(missing)
        contracts_with_missing += bool(missing)
        required_after_expiration += sum(day > contract.expiration for day in required)

    anchor_pairs = anchors_without_session = 0
    delta_buckets = [0, 0, 0, 0]
    for anchor in options.validated_valuation_anchors:
        bars = bars_by_contract.get(anchor.contract, {})
        session = max((day for day in bars if day <= anchor.statement_date), default=None)
        if session is None:
            anchors_without_session += 1
            continue
        anchor_pairs += 1
        provider_value = bars[session] * anchor.quantity * anchor.contract.multiplier
        if anchor.market_value_usd == 0:
            delta_buckets[3] += 1
            continue
        relative_delta = abs(provider_value - anchor.market_value_usd) / abs(
            anchor.market_value_usd
        )
        if relative_delta <= Decimal("0.005"):
            delta_buckets[0] += 1
        elif relative_delta <= Decimal("0.02"):
            delta_buckets[1] += 1
        elif relative_delta <= Decimal("0.10"):
            delta_buckets[2] += 1
        else:
            delta_buckets[3] += 1

    metrics = {
        "contracts_required": len(required_by_contract),
        "identity_ok": identity_ok,
        "identity_failed": identity_failed,
        "identity_required_as_of": identity_required_as_of,
        "held_contract_session_pairs": sum(map(len, required_by_contract.values())),
        "bars_present_on_required_sessions": bars_present,
        "bars_missing": bars_missing,
        "contracts_with_any_missing_bar": contracts_with_missing,
        "required_sessions_after_expiration": required_after_expiration,
        "bars_with_n_or_volume_le_0": bars_nonpositive,
        "anchor_pairs_compared": anchor_pairs,
        "anchors_with_no_session_on_or_before_period_end": anchors_without_session,
        "relative_delta_le_0_5_pct": delta_buckets[0],
        "relative_delta_le_2_pct": delta_buckets[1],
        "relative_delta_le_10_pct": delta_buckets[2],
        "relative_delta_gt_10_pct": delta_buckets[3],
        "requests_made": client.requests_made,
        "http_429_count": client.http_429_count,
        "elapsed_seconds": int(time.monotonic() - started),
    }
    for name, value in metrics.items():
        print(f"{name}={value}")

    gates = (
        identity_failed,
        bars_missing,
        contracts_with_missing,
        required_after_expiration,
        bars_nonpositive,
        anchors_without_session,
        client.http_429_count,
    )
    return 0 if not any(gates) else 3


if __name__ == "__main__":
    raise SystemExit(main())
