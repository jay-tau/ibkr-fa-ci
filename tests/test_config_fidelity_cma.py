"""Runtime refund-status config tests for Fidelity CMA."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.config import (
    FidelityCmaPeakBalanceBound,
    FidelityCmaRefundStatus,
    load_user_config,
)
from ibkr_fa.exceptions import ConfigError
from tests._config_toml import _FIDELITY_CMA_BASE as _BASE
from tests._text_edits import replace_once


def _load(tmp_path: Path, extra: str = ""):
    path = tmp_path / "user.toml"
    path.write_text(_BASE + extra, encoding="utf-8")
    return load_user_config(path).fidelity_cma


def test_cma_config_needs_no_ibkr_account_facts(tmp_path: Path) -> None:
    text = _BASE
    for line in (
        'number = "U1234567"\n',
        "opening_date = 2020-01-01\n",
        'status = "BeneficialOwner"\n',
    ):
        text = text.replace(line, "", 1)
    path = tmp_path / "user.toml"
    path.write_text(text, encoding="utf-8")
    cfg = load_user_config(path)
    assert cfg.account.number is None
    assert cfg.account.opening_date is None
    assert cfg.account.status is None
    assert cfg.fidelity_cma is not None


# ADR 0004 section 6: the statuses that can claim FTC, and those that block it.
_FTC_STATUSES = ("finalized", "no-refund-claim")
_BLOCKING_STATUSES = ("unknown", "not-filed-planned", "filed-pending")


def test_status_groups_cover_every_refund_status() -> None:
    assert {FidelityCmaRefundStatus(value) for value in _FTC_STATUSES + _BLOCKING_STATUSES} == set(
        FidelityCmaRefundStatus
    )


def _tax_year(status: str, body: str = "") -> str:
    return f'\n[[fidelity_cma.us_tax_years]]\ntax_year = 2025\nrefund_status = "{status}"\n{body}'


def _allocation(payment_date: str, amount: str) -> str:
    return (
        'provenance = "p"\n'
        f'eligible_tax = [{{ payment_date = {payment_date}, amount_usd = "{amount}" }}]\n'
    )


@pytest.mark.parametrize("status", _FTC_STATUSES + _BLOCKING_STATUSES)
def test_every_runtime_refund_status_is_supported(tmp_path: Path, status: str) -> None:
    body = 'provenance = "CA event allocation"\n' if status in _FTC_STATUSES else ""
    cfg = _load(tmp_path, _tax_year(status, body))
    assert cfg is not None
    assert cfg.us_tax_year(2025).refund_status is FidelityCmaRefundStatus(status)
    assert cfg.us_tax_year(2026).refund_status is FidelityCmaRefundStatus.UNKNOWN


@pytest.mark.parametrize("status", _FTC_STATUSES)
def test_ftc_status_event_tax_and_provenance_parse(tmp_path: Path, status: str) -> None:
    cfg = _load(
        tmp_path,
        _tax_year(
            status,
            """\
provenance = "2025 Form 1040-NR final transcript"
eligible_tax = [
  { payment_date = 2025-05-01, amount_usd = "0.12" },
  { payment_date = 2025-06-02, amount_usd = "0.00" },
]
""",
        ),
    )
    assert cfg is not None
    year = cfg.us_tax_year(2025)
    assert year.refund_status is FidelityCmaRefundStatus(status)
    assert year.provenance == "2025 Form 1040-NR final transcript"
    assert [(item.payment_date, item.amount_usd) for item in year.eligible_tax] == [
        (date(2025, 5, 1), Decimal("0.12")),
        (date(2025, 6, 2), Decimal("0.00")),
    ]
    rendered = repr(cfg)
    assert "Z99-999999" not in rendered
    assert "final transcript" not in rendered
    assert "0.12" not in rendered


def test_provenance_audit_text_may_span_lines(tmp_path: Path) -> None:
    # #818: provenance is never filed, so a line break in it is allowed.
    body = 'provenance = """2025 Form 1040-NR\nfinal transcript"""\n'
    cfg = _load(tmp_path, _tax_year("finalized", body))
    assert cfg is not None
    assert cfg.us_tax_year(2025).provenance == "2025 Form 1040-NR\nfinal transcript"


@pytest.mark.parametrize("status", _FTC_STATUSES)
@pytest.mark.parametrize(
    ("body", "field"),
    [
        ("", "provenance"),
        ('provenance = ""\n', "provenance"),
        (_allocation("2025-05-01", "-0.12"), "amount_usd"),
        (_allocation("2025-05-01", "0.001"), "amount_usd"),
        (_allocation("2026-05-01", "0.12"), "payment_date"),
        (
            'provenance = "p"\neligible_tax = [\n'
            '  { payment_date = 2025-05-01, amount_usd = "0.12" },\n'
            '  { payment_date = 2025-05-01, amount_usd = "0.00" },\n'
            "]\n",
            "eligible_tax",
        ),
    ],
    ids=[
        "missing-provenance",
        "empty-provenance",
        "negative-amount",
        "sub-cent-amount",
        "other-year-date",
        "duplicate-date",
    ],
)
def test_invalid_ftc_status_config_refuses(
    tmp_path: Path,
    status: str,
    body: str,
    field: str,
) -> None:
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, _tax_year(status, body))
    assert field in caught.value.field


_ELIGIBLE = 'eligible_tax = [{ payment_date = 2025-05-01, amount_usd = "0.12" }]\n'
_FTC_ONLY = "is allowed only when refund_status is finalized or no-refund-claim"
_FIX_ADDS_PROVENANCE = (
    f"{_FTC_ONLY}; set refund_status to one of those and add a non-empty provenance, "
    "or keep refund_status and delete eligible_tax"
)
_FIX_DELETES_PROVENANCE = (
    f"{_FTC_ONLY}; set refund_status to one of those, or keep refund_status and "
    "delete eligible_tax and provenance"
)


@pytest.mark.parametrize("status", _BLOCKING_STATUSES)
@pytest.mark.parametrize(
    ("body", "field", "message"),
    [
        (_ELIGIBLE, "eligible_tax", _FIX_ADDS_PROVENANCE),
        ('provenance = "not final"\n', "provenance", _FTC_ONLY),
    ],
    ids=["eligible-tax", "provenance"],
)
def test_blocking_status_refuses_allocations_and_provenance(
    tmp_path: Path,
    status: str,
    body: str,
    field: str,
    message: str,
) -> None:
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, _tax_year(status, body))
    assert caught.value.field.endswith(f".{field}")
    assert caught.value.message == message


@pytest.mark.parametrize("route", [*_FTC_STATUSES, "keep"])
@pytest.mark.parametrize("status", _BLOCKING_STATUSES)
@pytest.mark.parametrize(
    "provenance",
    ["", 'provenance = ""\n', 'provenance = "not final"\n'],
    ids=["no-provenance", "empty-provenance", "provenance"],
)
def test_blocking_status_eligible_tax_fixes_each_load_on_the_next_run(
    tmp_path: Path, provenance: str, status: str, route: str
) -> None:
    """#1511: the refusal names both fixes in full, so following either one
    (an FTC status, or keeping the status) loads on one reload."""
    text = _BASE + _tax_year(status, _ELIGIBLE + provenance)
    path = tmp_path / "user.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_user_config(path)
    assert caught.value.field == "fidelity_cma.us_tax_years[0].eligible_tax"
    has_provenance = provenance == 'provenance = "not final"\n'
    assert caught.value.message == (
        _FIX_DELETES_PROVENANCE if has_provenance else _FIX_ADDS_PROVENANCE
    )
    if route == "keep":
        text = replace_once(text, _ELIGIBLE, "")
        if has_provenance:
            text = replace_once(text, provenance, "")
    else:
        text = replace_once(text, f'refund_status = "{status}"\n', f'refund_status = "{route}"\n')
        if provenance == 'provenance = ""\n':
            text = replace_once(text, provenance, 'provenance = "final transcript"\n')
        elif not provenance:
            text += 'provenance = "final transcript"\n'
    path.write_text(text, encoding="utf-8")
    cma = load_user_config(path).fidelity_cma
    assert cma is not None
    year = cma.us_tax_year(2025)
    if route == "keep":
        assert (year.refund_status, year.eligible_tax, year.provenance) == (
            FidelityCmaRefundStatus(status),
            (),
            None,
        )
    else:
        assert year.refund_status is FidelityCmaRefundStatus(route)
        assert [(item.payment_date, item.amount_usd) for item in year.eligible_tax] == [
            (date(2025, 5, 1), Decimal("0.12"))
        ]
        assert year.provenance == ("not final" if has_provenance else "final transcript")


@pytest.mark.parametrize("route", [*_FTC_STATUSES, "keep"])
@pytest.mark.parametrize("status", _BLOCKING_STATUSES)
def test_blocking_status_provenance_fixes_each_load_on_the_next_run(
    tmp_path: Path, status: str, route: str
) -> None:
    """#1511: the sibling provenance refusal has no such gap; an FTC status,
    or keeping the status and deleting provenance, loads on one reload."""
    text = _BASE + _tax_year(status, 'provenance = "not final"\n')
    path = tmp_path / "user.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_user_config(path)
    assert caught.value.field == "fidelity_cma.us_tax_years[0].provenance"
    assert caught.value.message == _FTC_ONLY
    if route == "keep":
        text = replace_once(text, 'provenance = "not final"\n', "")
    else:
        text = replace_once(text, f'refund_status = "{status}"\n', f'refund_status = "{route}"\n')
    path.write_text(text, encoding="utf-8")
    cma = load_user_config(path).fidelity_cma
    assert cma is not None
    year = cma.us_tax_year(2025)
    assert year.refund_status is FidelityCmaRefundStatus(status if route == "keep" else route)
    assert year.eligible_tax == ()
    assert year.provenance == (None if route == "keep" else "not final")


@pytest.mark.parametrize("status", _BLOCKING_STATUSES)
def test_blocking_status_accepts_empty_allocation_fields(tmp_path: Path, status: str) -> None:
    cfg = _load(tmp_path, _tax_year(status, 'eligible_tax = []\nprovenance = ""\n'))
    assert cfg is not None
    assert cfg.us_tax_year(2025).eligible_tax == ()


def test_unrecognized_refund_status_refuses(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, _tax_year("not-a-status"))
    assert "refund_status" in caught.value.field


def test_duplicate_tax_year_refuses(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        _load(
            tmp_path,
            """
[[fidelity_cma.us_tax_years]]
tax_year = 2025
refund_status = "unknown"

[[fidelity_cma.us_tax_years]]
tax_year = 2025
refund_status = "filed-pending"
""",
        )
    assert "duplicate tax_year" in caught.value.message


def test_peak_warning_defaults_to_unsuppressed_manual_fill(tmp_path: Path) -> None:
    cfg = _load(tmp_path)
    assert cfg is not None
    assert not cfg.suppress_peak_balance_warning
    assert cfg.peak_balance_bound is None


@pytest.mark.parametrize(
    "bound",
    [FidelityCmaPeakBalanceBound.LOWER, FidelityCmaPeakBalanceBound.UPPER],
)
def test_suppressed_peak_warning_requires_explicit_bound(
    tmp_path: Path,
    bound: FidelityCmaPeakBalanceBound,
) -> None:
    cfg = _load(
        tmp_path,
        (f'\nsuppress_peak_balance_warning = true\npeak_balance_bound = "{bound.value}"\n'),
    )
    assert cfg is not None
    assert cfg.suppress_peak_balance_warning
    assert cfg.peak_balance_bound is bound


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (
            "\nsuppress_peak_balance_warning = true\n",
            "more conservative.*rupee order can differ",
        ),
        (
            '\npeak_balance_bound = "upper"\n',
            "allowed only",
        ),
        (
            ('\nsuppress_peak_balance_warning = true\npeak_balance_bound = "middle"\n'),
            "minimum proven.*rupee order can differ",
        ),
    ],
)
def test_invalid_peak_warning_config_explains_choices(
    tmp_path: Path,
    extra: str,
    message: str,
) -> None:
    with pytest.raises(ConfigError, match=message):
        _load(tmp_path, extra)


def test_cma_status_is_source_specific_and_validated(tmp_path: Path) -> None:
    cfg = _load(tmp_path)
    assert cfg is not None
    assert cfg.status is not None
    assert cfg.status.value == "BeneficialOwner"
    path = tmp_path / "invalid-status.toml"
    path.write_text(
        _BASE.replace(
            (
                "[fidelity_cma]\n"
                'account_id = "Z99-999999"\n'
                'spaxx_isin = "US31617H1023"\n'
                'status = "BeneficialOwner"'
            ),
            (
                "[fidelity_cma]\n"
                'account_id = "Z99-999999"\n'
                'spaxx_isin = "US31617H1023"\n'
                'status = "SigningAuthority"'
            ),
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="INDIVIDUAL TOD"):
        load_user_config(path)
    joint = tmp_path / "joint-status.toml"
    joint.write_text(
        _BASE.replace(
            (
                "[fidelity_cma]\n"
                'account_id = "Z99-999999"\n'
                'spaxx_isin = "US31617H1023"\n'
                'status = "BeneficialOwner"'
            ),
            (
                "[fidelity_cma]\n"
                'account_id = "Z99-999999"\n'
                'spaxx_isin = "US31617H1023"\n'
                'status = "Joint"'
            ),
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Joint"):
        load_user_config(joint)
