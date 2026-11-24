"""Tests for the shared IBKR Activity Statement preamble (#609)."""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa import pipeline_ibkr_input
from ibkr_fa.compute.source_coverage import require_financial_year_contained_in_period
from ibkr_fa.exceptions import ParseError
from ibkr_fa.pipeline import _OS_DEFAULT_REQUIRED_ACTIVITY_SECTIONS, FinancialYear
from ibkr_fa.pipeline_ibkr_input import _IbkrActivityInput, _load_ibkr_activity_input

_FIXTURES = Path(__file__).parent / "fixtures"
_FY_2025_26_INPUT = _FIXTURES / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
_FLEX_INPUT = _FIXTURES / "e2e_year_end_2025.csv"

# [account] stays the last table so tests can append opt-ins to it.
_USER_TOML = """\
[account]
number = "U12345678"
holder_name = "Sample Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"
"""


def _load(
    tmp_path: Path,
    *,
    input_path: Path = _FY_2025_26_INPUT,
    config_text: str = _USER_TOML,
    allow_masked_account: bool = False,
    accept_anonymised_statement: bool = False,
) -> _IbkrActivityInput:
    config_path = tmp_path / "user.toml"
    config_path.write_text(config_text, encoding="utf-8")
    return _load_ibkr_activity_input(
        input_path=input_path,
        config_path=config_path,
        period_validator=functools.partial(
            require_financial_year_contained_in_period,
            financial_year=FinancialYear(2025),
            report_name="Test reporting",
        ),
        broker_validator=None,
        required_sections=_OS_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
        unsupported_format_message=lambda input_format: f"caller text: {input_format.value}",
        allow_masked_account=allow_masked_account,
        accept_anonymised_statement=accept_anonymised_statement,
        check_income_against_nav=False,
    )


def test_flex_input_raises_with_the_callers_message(tmp_path: Path) -> None:
    """The refusal carries the caller's fixed message and an empty
    ``raw_value``: the input path no longer rides along in it (#1047)."""
    with pytest.raises(ParseError) as exc:
        _load(tmp_path, input_path=_FLEX_INPUT)

    assert exc.value.section == "<input format>"
    assert exc.value.row_index == 0
    assert exc.value.column == "<format detection>"
    assert exc.value.message == "caller text: flex-query"
    assert exc.value.raw_value == ""
    assert str(exc.value) == (
        "ParseError in section '<input format>' at row 0, column '<format detection>': "
        "caller text: flex-query"
    )


@pytest.mark.parametrize(
    ("statement_account", "cross_checked"),
    [("U12345678", True), ("U********", False)],
)
def test_allow_masked_account_skips_only_a_masked_cross_check(
    tmp_path: Path, statement_account: str, cross_checked: bool
) -> None:
    statement = tmp_path / "statement.csv"
    statement.write_text(
        _FY_2025_26_INPUT.read_text(encoding="utf-8").replace(
            "Account,U12345678", f"Account,{statement_account}"
        ),
        encoding="utf-8",
    )

    loaded = _load(tmp_path, input_path=statement, allow_masked_account=True)

    assert loaded.extract.account_info.account_id == statement_account
    assert loaded.account_id_cross_checked is cross_checked


@pytest.mark.parametrize(
    ("cli_flag", "config_opt_in", "fallbacks"),
    [
        (False, "", (None, None)),
        (True, "", ("Sample Holder", "U12345678")),
        (False, "accept_anonymised_statement = true\n", ("Sample Holder", "U12345678")),
    ],
)
def test_anonymised_fallbacks_reach_dispatch_only_when_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_flag: bool,
    config_opt_in: str,
    fallbacks: tuple[str | None, str | None],
) -> None:
    seen: list[tuple[str | None, str | None]] = []
    dispatch = pipeline_ibkr_input._dispatch_activity_statement

    def record(input_path: Path, **kwargs: Any) -> Any:
        seen.append((kwargs["fallback_account_holder_name"], kwargs["fallback_account_id"]))
        return dispatch(input_path, **kwargs)

    monkeypatch.setattr(pipeline_ibkr_input, "_dispatch_activity_statement", record)

    _load(
        tmp_path,
        config_text=_USER_TOML + config_opt_in,
        accept_anonymised_statement=cli_flag,
    )

    assert seen == [fallbacks]
