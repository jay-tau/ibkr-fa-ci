"""Tests for the user.toml loader."""

from __future__ import annotations

import errno
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import ibkr_fa.config as config_module
from ibkr_fa.config import (
    AccountConfig,
    Form67Config,
    FxConfig,
    OsConfig,
    RobinhoodConfig,
    RobinhoodInterestKind,
    RobinhoodOptionTreatment,
    RobinhoodOptionTurnoverBasis,
    RobinhoodSecurityReceiptTreatment,
    ScheduleOsIndianAdrRoutingMode,
    TaxpayerStatus,
    UserConfig,
    effective_anonymisation_flags,
    load_user_config,
)
from ibkr_fa.exceptions import ConfigError, IbkrFaError, PathRefusalError
from ibkr_fa.models.account_identity import AccountIdentity, InstitutionIdentity
from ibkr_fa.models.form67_entry import Form67SourceOfIncome
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_cg import run_schedule_cg
from ibkr_fa.pipeline_cg_ltcg import run_schedule_cg_ltcg
from ibkr_fa.pipeline_fa import run_fa_a3
from ibkr_fa.pipeline_fsi_compute import _compute_schedule_fsi_for_pipeline
from ibkr_fa.pipeline_os import run_schedule_os
from ibkr_fa.pipeline_stockplan_fa import (
    inspect_stockplan_cg_evidence,
    run_stockplan_fa,
)
from ibkr_fa.pipeline_stockplan_income import run_stockplan_income
from tests._config_toml import (
    _A2_PEAK,
    _FIDELITY_CMA_BASE,
    NON_UTF8_TOML,
    UNKNOWN_COUNTRY_CODE,
    UNVERIFIED_STOCKPLAN_ISSUER,
    VALID_NETBENEFITS,
    VALID_TOML,
    _nb_with_binding,
    unknown_key,
)
from tests._ibkr_user_toml import CUSTODIAN_TOML
from tests._repr_text import repr_without_addresses
from tests._robinhood_toml import (
    _OPTION_BUSINESS_FILING,
    _ROBINHOOD_FIRST,
    _ROBINHOOD_GLOBAL_TOML,
    _ROBINHOOD_SECOND,
    robinhood_account_toml,
    robinhood_corrected_statement_toml,
    robinhood_option_classification_toml,
    robinhood_report_toml,
    robinhood_security_receipt_resolution_toml,
    robinhood_security_toml,
    robinhood_toml,
    write,
)

_PERMISSIONS_ENFORCED = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)


def robinhood_stock_lending_fee_fsi_source_toml(
    *,
    account_number: str = "100000001",
    financial_year: str = "2025",
    country_iso2: str = "US",
    source: str = "synthetic reviewed agreement",
    reason: str = "RHS is the principal loan-fee obligor",
) -> str:
    return f"""\

[[robinhood.stock_lending_fee_fsi_sources]]
account_number = "{account_number}"
financial_year = {financial_year}
country_iso2 = "{country_iso2}"
source = "{source}"
reason = "{reason}"
"""


def robinhood_interest_payer_country_toml(
    *,
    account_number: str = "100000001",
    financial_year: str = "2025",
    country_iso2: str = '"US"',
    interest_kinds: str = '["brokerage", "program-bank"]',
    source: str = "synthetic reviewed interest terms",
    reason: str = "every payer of this interest is a US entity",
) -> str:
    return f"""\

[[robinhood.interest_payer_countries]]
account_number = "{account_number}"
financial_year = {financial_year}
country_iso2 = {country_iso2}
interest_kinds = {interest_kinds}
source = "{source}"
reason = "{reason}"
"""


def robinhood_a2_peak_override_toml(
    *,
    account_number: str = "100000001",
    calendar_year: str = "2025",
    peak_balance_inr: str = "5000",
    source: str = "synthetic reviewed valuation",
    reason: str = "synthetic daily peak",
) -> str:
    return f"""\

[[robinhood.a2_peak_overrides]]
account_number = "{account_number}"
calendar_year = {calendar_year}
peak_balance_inr = {peak_balance_inr}
source = "{source}"
reason = "{reason}"
"""


ROBINHOOD_TOML = robinhood_toml()
_OLD_ROBINHOOD_ACCOUNT = """\

[robinhood.account]
number = "100000001"
opening_date = 2025-01-01
status = "BeneficialOwner"
institution_name = "Robinhood Securities, LLC"
address = "500 Colonial Center Parkway, Lake Mary, FL"
country_iso2 = "US"
"""
_OLD_ROBINHOOD_COUNTERPART = """\

[[robinhood.counterpart_accounts]]
source_reference = "300000003"
account_number = "200000002"
institution_name = "Robinhood Securities, LLC"
country_iso2 = "US"
same_beneficial_owner = true
source = "synthetic counterpart statement"
"""


def test_minimal_valid_config(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    assert cfg.account.number == "U1234567"
    assert cfg.account.holder_name == "Sample Holder"
    assert cfg.account.opening_date == date(2024, 2, 1)
    assert cfg.account.status is TaxpayerStatus.BENEFICIAL_OWNER
    assert cfg.fx.max_carry_forward_days == 14  # default


def test_account_number_may_be_omitted_for_source_specific_flows(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML.replace('number = "U1234567"\n', "")))
    assert cfg.account.number is None
    with pytest.raises(ConfigError, match="required for IBKR statement commands"):
        cfg.account.require_number()


def test_optional_fx_table_overrides_default(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML + "\n[fx]\nmax_carry_forward_days = 30\n"))
    assert cfg.fx.max_carry_forward_days == 30


def test_missing_file_raises_path_refusal(tmp_path: Path) -> None:
    path = tmp_path / "nonexistent.toml"
    with pytest.raises(PathRefusalError) as raised:
        load_user_config(path)
    assert raised.value == PathRefusalError("user.toml not found", path)
    assert str(raised.value) == f"user.toml not found: {path}"


@_PERMISSIONS_ENFORCED
def test_file_in_an_unsearchable_folder_raises_eacces(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """#1238: the user.toml existence check raises EACCES instead of calling the file missing."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    path = write(blocked, VALID_TOML)
    blocked.chmod(0)
    request.addfinalizer(lambda: blocked.chmod(0o700))
    with pytest.raises(PermissionError) as raised:
        load_user_config(path)
    assert type(raised.value) is PermissionError
    assert str(raised.value) == f"[Errno {errno.EACCES}] Permission denied: {str(path)!r}"


def test_invalid_toml_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, "this is = not valid TOML ["))
    assert exc_info.value.field == "<file>"
    assert "invalid TOML" in exc_info.value.message


def test_non_utf8_file_raises_config_error_without_quoting_it(tmp_path: Path) -> None:
    assert VALID_TOML.encode() != NON_UTF8_TOML
    path = tmp_path / "user.toml"
    path.write_bytes(NON_UTF8_TOML)
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(path)
    assert exc_info.value.field == "<file>"
    offset = NON_UTF8_TOML.index(b"\xe9")
    assert exc_info.value.message == (
        f"must be UTF-8 text, but the byte at offset {offset} (counting from 0) is not valid UTF-8"
    )
    assert "PRIVATE-MARKER" not in str(exc_info.value)

    path.write_bytes(NON_UTF8_TOML.replace(b"\xe9", "é".encode()))
    assert load_user_config(path).account.holder_name == "PRIVATE-MARKERé"


def test_unknown_top_level_table_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, VALID_TOML + "\n[surprise]\nx = 1\n"))
    assert exc_info.value.field == "<root>"
    assert exc_info.value.message == unknown_key(
        "account, fa, fidelity_cma, form67, fx, netbenefits, os, prices, robinhood, "
        "schedule_fa, stockplan"
    )
    assert "surprise" not in str(exc_info.value)


def test_missing_account_table_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, "[fx]\nmax_carry_forward_days = 7\n"))
    assert exc_info.value.field == "account"


def test_account_missing_required_field_rejected(tmp_path: Path) -> None:
    bad = VALID_TOML.replace('holder_name = "Sample Holder"\n', "")
    assert bad != VALID_TOML
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, bad))
    assert exc_info.value.field == "account.holder_name"
    assert "missing required field" in exc_info.value.message


def test_account_unknown_field_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, VALID_TOML + 'extra = "ignored"\n'))
    assert exc_info.value.field == "account"
    assert exc_info.value.message == unknown_key(
        "accept_anonymised_statement, allow_masked_account, custodian, foreign_tins, "
        "holder_name, number, opening_date, status, us_person"
    )
    assert "extra" not in str(exc_info.value)


def test_account_number_must_be_non_empty(tmp_path: Path) -> None:
    bad = VALID_TOML.replace('number = "U1234567"', 'number = ""')
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, bad))
    assert exc_info.value.field == "account.number"
    assert "non-empty" in exc_info.value.message


def test_account_number_must_be_ascii(tmp_path: Path) -> None:
    bad = VALID_TOML.replace('number = "U1234567"', 'number = "Ü1234567"')
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, bad))
    assert exc_info.value.field == "account.number"
    assert "ASCII" in exc_info.value.message


def test_account_number_must_be_string(tmp_path: Path) -> None:
    bad = VALID_TOML.replace('number = "U1234567"', "number = 1234567")
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, bad))
    assert exc_info.value.field == "account.number"
    assert "string" in exc_info.value.message


def test_account_number_must_not_be_masked_pattern(tmp_path: Path) -> None:
    """#141 T3 P2: a masked-IBKR-pattern user.toml [account].number is
    refused at config-load time. Without this guard the combination of
    --accept-anonymised-statement (uses config.account.number as fallback
    when the statement omits Account) and --allow-masked-account (skips
    cross-check on masked IDs) would let a run proceed with no real
    account ID anywhere — completely defeating the safety net.
    """
    bad = VALID_TOML.replace('number = "U1234567"', 'number = "U**********"')
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, bad))
    assert exc_info.value.field == "account.number"
    assert "mask" in exc_info.value.message.lower()
    assert "U**********" in exc_info.value.message
    assert "--accept-anonymised-statement" in exc_info.value.message
    assert "--allow-masked-account" in exc_info.value.message


def test_holder_name_must_be_non_empty(tmp_path: Path) -> None:
    bad = VALID_TOML.replace('holder_name = "Sample Holder"', 'holder_name = "   "')
    assert bad != VALID_TOML
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, bad))
    assert exc_info.value.field == "account.holder_name"


def test_opening_date_iso_string_accepted(tmp_path: Path) -> None:
    """TOML datetime literals are nice but ISO strings should also work."""
    bad = VALID_TOML.replace("opening_date = 2024-02-01", 'opening_date = "2024-02-01"')
    cfg = load_user_config(write(tmp_path, bad))
    assert cfg.account.opening_date == date(2024, 2, 1)


def test_opening_date_invalid_string_rejected(tmp_path: Path) -> None:
    bad = VALID_TOML.replace("opening_date = 2024-02-01", 'opening_date = "not-a-date"')
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, bad))
    assert exc_info.value.field == "account.opening_date"


def test_opening_date_in_future_rejected(tmp_path: Path) -> None:
    bad = VALID_TOML.replace("opening_date = 2024-02-01", "opening_date = 9999-12-31")
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, bad))
    assert exc_info.value.field == "account.opening_date"
    assert "future" in exc_info.value.message


def test_opening_date_wrong_type_rejected(tmp_path: Path) -> None:
    bad = VALID_TOML.replace("opening_date = 2024-02-01", "opening_date = 12345")
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, bad))
    assert exc_info.value.field == "account.opening_date"


def test_opening_date_toml_datetime_rejected(tmp_path: Path) -> None:
    """datetime.datetime is a subclass of datetime.date in Python, so a TOML
    datetime literal like `opening_date = 2024-02-01T12:30:00` would silently
    pass the isinstance(date) branch and then crash on the date-vs-datetime
    comparison. Reject explicitly with a clear ConfigError.
    """
    bad = VALID_TOML.replace("opening_date = 2024-02-01", "opening_date = 2024-02-01T12:30:00")
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, bad))
    assert exc_info.value.field == "account.opening_date"
    assert "datetime" in exc_info.value.message


def test_opening_date_toml_offset_datetime_rejected(tmp_path: Path) -> None:
    """Same case but with an explicit UTC offset — also a TOML datetime."""
    bad = VALID_TOML.replace(
        "opening_date = 2024-02-01", "opening_date = 2024-02-01T12:30:00+05:30"
    )
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, bad))
    assert exc_info.value.field == "account.opening_date"
    assert "datetime" in exc_info.value.message


def test_status_must_be_known_value(tmp_path: Path) -> None:
    bad = VALID_TOML.replace('status = "BeneficialOwner"', 'status = "OwnerSomehow"')
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, bad))
    assert exc_info.value.field == "account.status"
    assert exc_info.value.message == "must be one of: BeneficialOwner, Joint"


@pytest.mark.parametrize("status", [TaxpayerStatus.BENEFICIAL_OWNER, TaxpayerStatus.JOINT])
def test_supported_taxpayer_statuses_accepted(tmp_path: Path, status: TaxpayerStatus) -> None:
    text = VALID_TOML.replace('status = "BeneficialOwner"', f'status = "{status.value}"')
    cfg = load_user_config(write(tmp_path, text))
    assert cfg.account.status is status


def test_optional_legacy_account_facts_have_explicit_guards(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, '[account]\nholder_name = "Tax Payer"\n'))
    assert cfg.account.opening_date is None
    assert cfg.account.status is None
    with pytest.raises(ConfigError) as opening_exc:
        cfg.account.require_opening_date()
    with pytest.raises(ConfigError) as status_exc:
        cfg.account.require_status()
    assert opening_exc.value.field == "account.opening_date"
    assert status_exc.value.field == "account.status"


# ---- Robinhood filed-account config (#469) -------------------------------


def test_robinhood_module_schema_documents_only_two_account_shape() -> None:
    schema = config_module.__doc__ or ""
    assert schema.count("[[robinhood.accounts]]") == 2
    assert schema.count("[[robinhood.securities]]") == 1
    assert schema.count("[[robinhood.security_receipt_resolutions]]") == 1
    assert schema.count("[[robinhood.option_classifications]]") == 1
    assert schema.count("[[robinhood.stock_lending_fee_fsi_sources]]") == 1
    assert schema.count("[[robinhood.interest_payer_countries]]") == 1
    assert "[[robinhood.a2_peak_overrides]]" not in schema
    assert schema.count("[[robinhood.reports]]") == 2
    assert schema.count("[[robinhood.corrected_statements]]") == 1
    assert "[robinhood.account]" not in schema
    assert "counterpart_accounts" not in schema


def test_robinhood_only_config_loads_two_filed_accounts(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, ROBINHOOD_TOML))
    assert cfg.account.number is None
    assert cfg.account.opening_date is None
    assert cfg.account.status is None
    assert cfg.robinhood is not None
    first, second = cfg.robinhood.accounts
    assert first.number == "100000001"
    assert first.opening_date == date(2025, 1, 1)
    assert first.status is TaxpayerStatus.BENEFICIAL_OWNER
    assert first.zip_code == "32746"
    assert first.same_beneficial_owner is True
    assert first.source == "synthetic individual statement"
    assert second.number == "200000002"
    assert second.opening_date == date(2025, 10, 1)
    assert second.source == "synthetic managed statement"
    rendered = (
        repr_without_addresses(cfg.robinhood)
        + repr_without_addresses(first)
        + repr_without_addresses(second)
    )
    assert all(
        value not in rendered
        for value in (
            "100000001",
            "200000002",
            "2025-01-01",
            "32746",
            "500 Colonial Center Parkway",
            "synthetic individual statement",
            "synthetic managed statement",
        )
    )


def test_robinhood_account_facts_are_independent_and_zip_is_optional(tmp_path: Path) -> None:
    second = robinhood_account_toml(
        number="200000002",
        opening_date="2025-10-01",
        status="Joint",
        institution_name="Other Foreign Custodian",
        address="Different Address",
        zip_code=None,
        country_iso2="CA",
        source="déclaration synthétique",
    )
    cfg = load_user_config(write(tmp_path, robinhood_toml(second=second)))
    assert cfg.robinhood is not None
    parsed = cfg.robinhood.accounts[1]
    assert parsed.status is TaxpayerStatus.JOINT
    assert parsed.institution_name == "Other Foreign Custodian"
    assert parsed.address == "Different Address"
    assert parsed.zip_code == ""
    assert parsed.country_iso2 == "CA"
    assert parsed.source == "déclaration synthétique"


@pytest.mark.parametrize(
    "text",
    [
        _ROBINHOOD_GLOBAL_TOML + "\n[robinhood]\naccounts = []\n",
        robinhood_toml(second=""),
        robinhood_toml(
            extra=robinhood_account_toml(
                number="300000003",
                opening_date="2026-01-01",
                source="synthetic third statement",
            )
        ),
    ],
    ids=["zero", "one", "three"],
)
def test_robinhood_requires_exactly_two_accounts(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == "robinhood.accounts"
    assert exc.value.message == "must contain exactly two filed accounts"


def test_robinhood_accounts_must_be_an_array_of_tables(tmp_path: Path) -> None:
    text = _ROBINHOOD_GLOBAL_TOML + "\n[robinhood]\naccounts = {}\n"
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == "robinhood.accounts"
    assert exc.value.message == "must be an array of tables"

    text = _ROBINHOOD_GLOBAL_TOML + "\n[robinhood]\naccounts = [1, 2]\n"
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == "robinhood.accounts[0]"
    assert exc.value.message == "must be a TOML table"


def test_robinhood_old_only_schema_reports_missing_accounts(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, _ROBINHOOD_GLOBAL_TOML + _OLD_ROBINHOOD_ACCOUNT))
    assert exc.value.field == "robinhood.accounts"
    assert exc.value.message == "missing required field"


_ROBINHOOD_KEYS = (
    "accounts, corrected_statements, cusip_transitions_file, interest_payer_countries, "
    "option_classifications, reports, sec_user_agent, securities, "
    "security_receipt_resolutions, stock_lending_fee_fsi_sources"
)


@pytest.mark.parametrize("extra", [_OLD_ROBINHOOD_ACCOUNT, _OLD_ROBINHOOD_COUNTERPART])
def test_robinhood_old_keys_are_unknown_with_valid_accounts(tmp_path: Path, extra: str) -> None:
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=extra)))
    assert exc.value.field == "robinhood"
    assert exc.value.message == unknown_key(_ROBINHOOD_KEYS)


@pytest.mark.parametrize(
    ("index", "line", "field"),
    [
        (0, 'number = "100000001"\n', "number"),
        (1, "opening_date = 2025-10-01\n", "opening_date"),
        (1, 'status = "BeneficialOwner"\n', "status"),
        (1, 'institution_name = "Robinhood Securities, LLC"\n', "institution_name"),
        (1, 'address = "500 Colonial Center Parkway, Lake Mary, FL"\n', "address"),
        (1, 'country_iso2 = "US"\n', "country_iso2"),
        (1, "same_beneficial_owner = true\n", "same_beneficial_owner"),
        (1, 'source = "synthetic managed statement"\n', "source"),
    ],
)
def test_robinhood_required_fields_refused_at_source_index(
    tmp_path: Path, index: int, line: str, field: str
) -> None:
    blocks = [_ROBINHOOD_FIRST, _ROBINHOOD_SECOND]
    blocks[index] = blocks[index].replace(line, "")
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(first=blocks[0], second=blocks[1])))
    assert exc.value.field == f"robinhood.accounts[{index}].{field}"
    assert exc.value.message == "missing required field"


@pytest.mark.parametrize(
    ("index", "old", "new", "field"),
    [
        (0, 'number = "100000001"', 'number = ""', "number"),
        (0, 'number = "100000001"', 'number = "*********"', "number"),
        (0, 'number = "100000001"', "number = 100000001", "number"),
        (1, "opening_date = 2025-10-01", "opening_date = 2999-01-01", "opening_date"),
        (1, 'status = "BeneficialOwner"', 'status = "Unknown"', "status"),
        (
            1,
            'address = "500 Colonial Center Parkway, Lake Mary, FL"',
            'address = " "',
            "address",
        ),
        (1, 'country_iso2 = "US"', 'country_iso2 = "IN"', "country_iso2"),
        (
            0,
            "same_beneficial_owner = true",
            "same_beneficial_owner = false",
            "same_beneficial_owner",
        ),
        (
            1,
            "same_beneficial_owner = true",
            'same_beneficial_owner = "true"',
            "same_beneficial_owner",
        ),
        (
            1,
            'source = "synthetic managed statement"',
            'source = " "',
            "source",
        ),
    ],
)
def test_robinhood_invalid_values_refused_at_source_index(
    tmp_path: Path,
    index: int,
    old: str,
    new: str,
    field: str,
) -> None:
    blocks = [_ROBINHOOD_FIRST, _ROBINHOOD_SECOND]
    blocks[index] = blocks[index].replace(old, new)
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(first=blocks[0], second=blocks[1])))
    assert exc.value.field == f"robinhood.accounts[{index}].{field}"


def test_robinhood_duplicate_account_refused_without_value_leaks(tmp_path: Path) -> None:
    duplicate = _ROBINHOOD_SECOND.replace('number = "200000002"', 'number = "100000001"')
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(second=duplicate)))
    assert exc.value.field == "robinhood.accounts[1].number"
    assert exc.value.message == "duplicates another configured account number"
    assert all(
        value not in str(exc.value)
        for value in (
            "100000001",
            "200000002",
            "500 Colonial Center Parkway",
            "synthetic managed statement",
        )
    )


_ROBINHOOD_ACCOUNT_KEYS = (
    "address, country_iso2, institution_name, number, opening_date, same_beneficial_owner, "
    "source, status, zip"
)


@pytest.mark.parametrize(
    ("text", "field", "message"),
    [
        (
            robinhood_toml(parent_fields="surprise = true\n"),
            "robinhood",
            unknown_key(_ROBINHOOD_KEYS),
        ),
        (
            robinhood_toml(
                first=_ROBINHOOD_FIRST.replace(
                    'source = "synthetic individual statement"',
                    'source = "synthetic individual statement"\nsurprise = true',
                )
            ),
            "robinhood.accounts[0]",
            unknown_key(_ROBINHOOD_ACCOUNT_KEYS),
        ),
        (
            robinhood_toml(
                second=_ROBINHOOD_SECOND.replace(
                    'source = "synthetic managed statement"',
                    'source = "synthetic managed statement"\nsurprise = true',
                )
            ),
            "robinhood.accounts[1]",
            unknown_key(_ROBINHOOD_ACCOUNT_KEYS),
        ),
    ],
)
def test_robinhood_unknown_fields_refused(
    tmp_path: Path, text: str, field: str, message: str
) -> None:
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == field
    assert exc.value.message == message
    assert "surprise" not in str(exc.value)


def test_robinhood_security_identity_round_trips_privately(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, robinhood_toml(extra=robinhood_security_toml())))
    assert cfg.robinhood is not None
    security = cfg.robinhood.securities[0]
    assert security.instrument.symbol == "XQCA"
    assert security.cusip == "99972B101"
    assert str(security.instrument.isin) == "US99972B1017"
    assert security.instrument.issuer_country_code == "US"
    assert security.is_depositary_receipt is False
    rendered = repr(cfg.robinhood) + repr(security)
    assert "XQCA" not in rendered
    assert "99972B101" not in rendered
    assert "synthetic issuer evidence" not in rendered


def test_robinhood_cusip_transition_file_is_only_a_private_path_pointer(
    tmp_path: Path,
) -> None:
    cfg = load_user_config(
        write(
            tmp_path,
            robinhood_toml(
                parent_fields=(
                    'cusip_transitions_file = "overrides/robinhood/cusip-transitions.toml"\n'
                )
            ),
        )
    )
    assert cfg.robinhood is not None
    assert cfg.robinhood.cusip_transitions_file == "overrides/robinhood/cusip-transitions.toml"


def test_robinhood_security_receipt_resolution_is_exact_and_private(tmp_path: Path) -> None:
    text = robinhood_toml(
        extra=(robinhood_security_toml() + robinhood_security_receipt_resolution_toml())
    )
    cfg = load_user_config(write(tmp_path, text))
    assert cfg.robinhood is not None
    (resolution,) = cfg.robinhood.security_receipt_resolutions
    assert resolution.source_record_index == 4
    assert resolution.treatment is RobinhoodSecurityReceiptTreatment.PROMOTIONAL_REWARD
    assert resolution.exact_quantity == Decimal("0.052876")
    assert resolution.reward_fair_market_value_usd == Decimal("5.00")
    assert resolution.income_reported_financial_year == 2024
    assert resolution.income_reported is True
    rendered = repr_without_addresses(cfg.robinhood) + repr_without_addresses(resolution)
    assert "100000001" not in rendered
    assert "99972B101" not in rendered
    assert "5.00" not in rendered


@pytest.mark.parametrize(
    ("block", "field"),
    [
        (
            robinhood_security_receipt_resolution_toml(income_reported="false"),
            "robinhood.security_receipt_resolutions[0].income_reported",
        ),
        (
            robinhood_security_receipt_resolution_toml(reward_fair_market_value_usd="5.0"),
            "robinhood.security_receipt_resolutions[0].reward_fair_market_value_usd",
        ),
        (
            robinhood_security_receipt_resolution_toml(exact_quantity="0.052876"),
            "robinhood.security_receipt_resolutions[0].exact_quantity",
        ),
        (
            robinhood_security_receipt_resolution_toml(exact_quantity='"0"'),
            "robinhood.security_receipt_resolutions[0].exact_quantity",
        ),
        (
            robinhood_security_receipt_resolution_toml().replace(
                'exact_quantity = "0.052876"\n',
                "",
            ),
            "robinhood.security_receipt_resolutions[0].exact_quantity",
        ),
        (
            robinhood_security_receipt_resolution_toml(treatment="gift"),
            "robinhood.security_receipt_resolutions[0].treatment",
        ),
        (
            robinhood_security_receipt_resolution_toml(account_number="300000003"),
            "robinhood.security_receipt_resolutions[0].account_number",
        ),
    ],
)
def test_robinhood_security_receipt_resolution_invalid_values_refused(
    tmp_path: Path,
    block: str,
    field: str,
) -> None:
    assert block != robinhood_security_receipt_resolution_toml()
    text = robinhood_toml(extra=robinhood_security_toml() + block)
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == field


def test_robinhood_security_receipt_resolution_duplicate_row_refused(tmp_path: Path) -> None:
    block = robinhood_security_receipt_resolution_toml()
    text = robinhood_toml(extra=robinhood_security_toml() + block + block)
    with pytest.raises(ConfigError, match="duplicates another account/source-row"):
        load_user_config(write(tmp_path, text))


def test_receipt_resolution_can_reference_an_automatically_generated_security(
    tmp_path: Path,
) -> None:
    block = robinhood_security_receipt_resolution_toml(cusip="99972L109")
    cfg = load_user_config(write(tmp_path, robinhood_toml(extra=robinhood_security_toml() + block)))
    assert cfg.robinhood is not None
    assert cfg.robinhood.security_receipt_resolutions[0].cusip == "99972L109"


def test_robinhood_sec_user_agent_round_trips(tmp_path: Path) -> None:
    cfg = load_user_config(
        write(
            tmp_path,
            robinhood_toml(parent_fields='sec_user_agent = "Tax Tool contact@example.com"\n'),
        )
    )
    assert cfg.robinhood is not None
    assert cfg.robinhood.sec_user_agent == "Tax Tool contact@example.com"


def test_robinhood_option_classification_is_account_and_fy_bound(tmp_path: Path) -> None:
    block = robinhood_option_classification_toml()
    cfg = load_user_config(write(tmp_path, robinhood_toml(extra=block)))
    assert cfg.robinhood is not None
    classification = cfg.robinhood.option_classification_for("100000001", 2025)
    assert classification is not None
    assert classification.fema_risk_acknowledged is True
    assert classification.treatment is RobinhoodOptionTreatment.SPECULATIVE_BUSINESS
    assert classification.source == "synthetic reviewed advice"
    assert classification.reason == "synthetic US-listed option activity"
    assert cfg.robinhood.option_classification_for("100000001", 2024) is None
    rendered = repr_without_addresses(cfg.robinhood) + repr_without_addresses(classification)
    assert "100000001" not in rendered
    assert "synthetic reviewed advice" not in rendered


def test_robinhood_option_business_filing_election_is_exact(tmp_path: Path) -> None:
    block = robinhood_option_classification_toml(business_filing=_OPTION_BUSINESS_FILING)
    cfg = load_user_config(write(tmp_path, robinhood_toml(extra=block)))
    assert cfg.robinhood is not None
    classification = cfg.robinhood.option_classification_for("100000001", 2025)
    assert classification is not None
    assert classification.books_maintained is False
    assert classification.other_speculative_business is False
    assert classification.nature_of_business_code == "21010"
    assert classification.turnover_basis is RobinhoodOptionTurnoverBasis.ICAI_5_11A_DIFFERENCES
    assert classification.external_expenses_inr == 0
    assert classification.bp_additions_inr == 0
    assert classification.bp_deductions_inr == 0


def test_robinhood_option_classification_requires_fema_acknowledgement(
    tmp_path: Path,
) -> None:
    block = robinhood_option_classification_toml().replace(
        "fema_risk_acknowledged = true\n",
        "",
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=block)))
    assert exc.value.field == "robinhood.option_classifications[0].fema_risk_acknowledged"


@pytest.mark.parametrize(
    ("block", "field"),
    [
        (
            robinhood_option_classification_toml(account_number="300000003"),
            "account_number",
        ),
        (
            robinhood_option_classification_toml(financial_year="true"),
            "financial_year",
        ),
        (
            robinhood_option_classification_toml(financial_year="1999"),
            "financial_year",
        ),
        (
            robinhood_option_classification_toml(treatment="unknown"),
            "treatment",
        ),
        (
            robinhood_option_classification_toml(fema_risk_acknowledged="false"),
            "fema_risk_acknowledged",
        ),
        (
            robinhood_option_classification_toml(fema_risk_acknowledged='"yes"'),
            "fema_risk_acknowledged",
        ),
        (
            robinhood_option_classification_toml(
                business_filing=_OPTION_BUSINESS_FILING.replace(
                    "books_maintained = false",
                    'books_maintained = "no"',
                )
            ),
            "books_maintained",
        ),
        (
            robinhood_option_classification_toml(
                business_filing=_OPTION_BUSINESS_FILING.replace(
                    'turnover_basis = "icai-5.11a-differences"',
                    'turnover_basis = "unknown"',
                )
            ),
            "turnover_basis",
        ),
        (
            robinhood_option_classification_toml(
                business_filing=_OPTION_BUSINESS_FILING.replace(
                    'nature_of_business_code = "21010"',
                    'nature_of_business_code = "21009"',
                )
            ),
            "nature_of_business_code",
        ),
        (
            robinhood_option_classification_toml(
                business_filing=_OPTION_BUSINESS_FILING.replace(
                    'external_expenses_inr = "0"',
                    "external_expenses_inr = 0.0",
                )
            ),
            "external_expenses_inr",
        ),
        (
            robinhood_option_classification_toml(source=" "),
            "source",
        ),
        (
            robinhood_option_classification_toml(reason=" "),
            "reason",
        ),
    ],
)
def test_robinhood_option_classification_invalid_values_refused(
    tmp_path: Path,
    block: str,
    field: str,
) -> None:
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=block)))
    assert exc.value.field == f"robinhood.option_classifications[0].{field}"


def test_robinhood_option_business_filing_fields_are_all_or_none(tmp_path: Path) -> None:
    partial = robinhood_option_classification_toml(business_filing="books_maintained = false\n")
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=partial)))
    assert exc.value.field == "robinhood.option_classifications[0]"

    capital = robinhood_option_classification_toml(
        treatment="capital-gains",
        business_filing=_OPTION_BUSINESS_FILING,
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=capital)))
    assert exc.value.field == "robinhood.option_classifications[0]"


def test_robinhood_option_classification_duplicate_binder_refused(tmp_path: Path) -> None:
    block = robinhood_option_classification_toml()
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=block + block)))
    assert exc.value.field == "robinhood.option_classifications[1]"
    assert exc.value.message == "duplicates another account/financial-year option classification"


def test_robinhood_option_classifications_must_be_array_of_tables(tmp_path: Path) -> None:
    text = robinhood_toml(parent_fields="option_classifications = {}\n")
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == "robinhood.option_classifications"

    text = robinhood_toml(parent_fields="option_classifications = [1]\n")
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == "robinhood.option_classifications[0]"


def test_robinhood_stock_lending_fee_fsi_source_is_account_and_fy_bound(
    tmp_path: Path,
) -> None:
    block = robinhood_stock_lending_fee_fsi_source_toml()
    cfg = load_user_config(write(tmp_path, robinhood_toml(extra=block)))
    assert cfg.robinhood is not None
    reviewed = cfg.robinhood.stock_lending_fee_fsi_source_for("100000001", 2025)
    assert reviewed is not None
    assert reviewed.country_iso2 == "US"
    assert reviewed.source == "synthetic reviewed agreement"
    assert cfg.robinhood.stock_lending_fee_fsi_source_for("100000001", 2024) is None
    assert "100000001" not in repr_without_addresses(reviewed)


@pytest.mark.parametrize(
    ("block", "field"),
    [
        (
            robinhood_stock_lending_fee_fsi_source_toml(account_number="300000003"),
            "account_number",
        ),
        (
            robinhood_stock_lending_fee_fsi_source_toml(financial_year="true"),
            "financial_year",
        ),
        (
            robinhood_stock_lending_fee_fsi_source_toml(financial_year="1999"),
            "financial_year",
        ),
        (
            robinhood_stock_lending_fee_fsi_source_toml(country_iso2="IN"),
            "country_iso2",
        ),
        (
            robinhood_stock_lending_fee_fsi_source_toml(source=" "),
            "source",
        ),
        (
            robinhood_stock_lending_fee_fsi_source_toml(reason=" "),
            "reason",
        ),
    ],
)
def test_robinhood_stock_lending_fee_fsi_source_invalid_values_refused(
    tmp_path: Path,
    block: str,
    field: str,
) -> None:
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=block)))
    assert exc.value.field == f"robinhood.stock_lending_fee_fsi_sources[0].{field}"


def test_robinhood_stock_lending_fee_fsi_source_duplicate_binder_refused(
    tmp_path: Path,
) -> None:
    block = robinhood_stock_lending_fee_fsi_source_toml()
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=block + block)))
    assert exc.value.field == "robinhood.stock_lending_fee_fsi_sources[1]"
    assert exc.value.message == (
        "duplicates another account/financial-year Stock Lending fee FSI source"
    )


def test_robinhood_stock_lending_fee_fsi_sources_must_be_array_of_tables(
    tmp_path: Path,
) -> None:
    text = robinhood_toml(parent_fields="stock_lending_fee_fsi_sources = {}\n")
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == "robinhood.stock_lending_fee_fsi_sources"

    text = robinhood_toml(parent_fields="stock_lending_fee_fsi_sources = [1]\n")
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == "robinhood.stock_lending_fee_fsi_sources[0]"


# ---- Robinhood interest payer country (#698) -----------------------------


@pytest.mark.parametrize("other_country", ["IE", "IN"])
def test_robinhood_interest_payer_countries_load(tmp_path: Path, other_country: str) -> None:
    block = robinhood_interest_payer_country_toml() + robinhood_interest_payer_country_toml(
        financial_year="2024", country_iso2=f'"{other_country}"', interest_kinds='["brokerage"]'
    )
    cfg = load_user_config(write(tmp_path, robinhood_toml(extra=block)))
    assert cfg.robinhood is not None
    first, second = cfg.robinhood.interest_payer_countries
    assert (first.account_number, first.financial_year, first.country_iso2) == (
        "100000001",
        2025,
        "US",
    )
    assert first.interest_kinds == frozenset(RobinhoodInterestKind)
    assert first.source == "synthetic reviewed interest terms"
    assert first.reason == "every payer of this interest is a US entity"
    # A well-formed non-US entry loads; the income compute refuses it, so
    # Schedule OS and FA still run.
    assert (second.financial_year, second.country_iso2) == (2024, other_country)
    assert second.interest_kinds == frozenset({RobinhoodInterestKind.BROKERAGE})
    assert "100000001" not in repr_without_addresses(first)


def test_robinhood_interest_payer_countries_default_to_none(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, ROBINHOOD_TOML))
    assert cfg.robinhood is not None
    assert cfg.robinhood.interest_payer_countries == ()


_INVALID_INTEREST_PAYER_COUNTRIES = {
    "unconfigured-account": (
        {"account_number": "300000003"},
        "account_number",
        "does not match a configured Robinhood account",
    ),
    "non-digit-account": (
        {"account_number": "ACCOUNT"},
        "account_number",
        "must contain ASCII digits only",
    ),
    "bool-year": (
        {"financial_year": "true"},
        "financial_year",
        "must be an integer financial-year start year",
    ),
    "float-year": (
        {"financial_year": "2025.0"},
        "financial_year",
        "must be an integer financial-year start year",
    ),
    "year-out-of-range": (
        {"financial_year": "1999"},
        "financial_year",
        "must be between 2000 and 2100",
    ),
    "int-country": (
        {"country_iso2": "1"},
        "country_iso2",
        "must be a 2-letter uppercase ISO 3166-1 alpha-2 code",
    ),
    "lowercase-country": (
        {"country_iso2": '"us"'},
        "country_iso2",
        "must be a 2-letter uppercase ISO 3166-1 alpha-2 code",
    ),
    "unmapped-country": (
        {"country_iso2": '"ZZ"'},
        "country_iso2",
        UNKNOWN_COUNTRY_CODE,
    ),
    "no-kinds": (
        {"interest_kinds": "[]"},
        "interest_kinds",
        "must be a non-empty array of interest kinds",
    ),
    "string-kinds": (
        {"interest_kinds": '"brokerage"'},
        "interest_kinds",
        "must be a non-empty array of interest kinds",
    ),
    "unknown-kind": (
        {"interest_kinds": '["Brokerage"]'},
        "interest_kinds",
        "must contain only: brokerage, program-bank",
    ),
    "int-kind": (
        {"interest_kinds": "[1]"},
        "interest_kinds",
        "must contain only: brokerage, program-bank",
    ),
    "repeated-kind": (
        {"interest_kinds": '["brokerage", "brokerage"]'},
        "interest_kinds",
        "must not repeat an interest kind",
    ),
    "blank-source": ({"source": " "}, "source", "must be non-empty"),
    "blank-reason": ({"reason": " "}, "reason", "must be non-empty"),
}


@pytest.mark.parametrize("case", _INVALID_INTEREST_PAYER_COUNTRIES)
def test_robinhood_interest_payer_country_invalid_values_refused(
    tmp_path: Path,
    case: str,
) -> None:
    overrides, field, message = _INVALID_INTEREST_PAYER_COUNTRIES[case]
    block = robinhood_interest_payer_country_toml(**overrides)
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=block)))
    assert exc.value.field == f"robinhood.interest_payer_countries[0].{field}"
    assert exc.value.message == message


@pytest.mark.parametrize(
    ("edit", "field", "message"),
    [
        (
            lambda block: block.replace("interest_kinds = ", "# interest_kinds = "),
            "robinhood.interest_payer_countries[0].interest_kinds",
            "missing required field",
        ),
        (
            lambda block: block + 'note = "synthetic"\n',
            "robinhood.interest_payer_countries[0]",
            unknown_key(
                "account_number, country_iso2, financial_year, interest_kinds, reason, source"
            ),
        ),
    ],
    ids=["missing", "unknown"],
)
def test_robinhood_interest_payer_country_fields_are_exact(
    tmp_path: Path,
    edit: Callable[[str], str],
    field: str,
    message: str,
) -> None:
    block = edit(robinhood_interest_payer_country_toml())
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=block)))
    assert exc.value.field == field
    assert exc.value.message == message


def test_robinhood_interest_payer_country_duplicate_account_fy_refused(tmp_path: Path) -> None:
    """A second entry for one account/FY is refused even when it conflicts,
    rather than one of them being picked."""
    block = robinhood_interest_payer_country_toml() + robinhood_interest_payer_country_toml(
        interest_kinds='["brokerage"]'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=block)))
    assert exc.value.field == "robinhood.interest_payer_countries[1]"
    assert exc.value.message == ("duplicates another account/financial-year interest payer country")


@pytest.mark.parametrize(
    ("parent_fields", "field"),
    [
        ("interest_payer_countries = {}\n", "robinhood.interest_payer_countries"),
        ("interest_payer_countries = [1]\n", "robinhood.interest_payer_countries[0]"),
    ],
)
def test_robinhood_interest_payer_countries_must_be_array_of_tables(
    tmp_path: Path, parent_fields: str, field: str
) -> None:
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(parent_fields=parent_fields)))
    assert exc.value.field == field


@pytest.mark.parametrize(
    "text",
    [
        robinhood_toml(extra=robinhood_a2_peak_override_toml()),
        robinhood_toml(extra=robinhood_a2_peak_override_toml(account_number="300000003")),
        robinhood_toml(parent_fields="a2_peak_overrides = {}\n"),
        robinhood_toml(parent_fields="a2_peak_overrides = [1]\n"),
    ],
)
def test_robinhood_a2_peak_overrides_are_refused(tmp_path: Path, text: str) -> None:
    """#628: automatic Robinhood A2 valuation never read this table, so a
    config that sets it is refused by name, whatever it holds."""
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == "robinhood.a2_peak_overrides"
    assert exc.value.message == (
        "is no longer used: automatic Robinhood A2 valuation never reads it; "
        "delete every [[robinhood.a2_peak_overrides]] table"
    )
    rendered = str(exc.value)
    for private in (
        "100000001",
        "300000003",
        "5000",
        "synthetic reviewed valuation",
        "synthetic daily peak",
    ):
        assert private not in rendered


def test_robinhood_runtime_manifest_round_trips_privately(tmp_path: Path) -> None:
    extra = (
        robinhood_report_toml(file="activity/individual.csv")
        + robinhood_report_toml(file="activity/managed.csv")
        + robinhood_corrected_statement_toml()
    )
    cfg = load_user_config(write(tmp_path, robinhood_toml(extra=extra)))
    assert cfg.robinhood is not None
    assert tuple(item.file for item in cfg.robinhood.reports) == (
        "activity/individual.csv",
        "activity/managed.csv",
    )
    assert cfg.robinhood.corrected_statements[0].supersedes == ("statements/2025-12.pdf",)
    rendered = repr(cfg.robinhood)
    assert "activity/individual.csv" not in rendered
    assert "2025-12-corrected.pdf" not in rendered


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        (
            robinhood_report_toml(file="../outside.csv"),
            "robinhood.reports[0].file",
        ),
        (
            robinhood_report_toml(file="/absolute.csv"),
            "robinhood.reports[0].file",
        ),
        (
            robinhood_report_toml(
                file="activity/one.csv",
                start_date="2026-01-01",
                end_date="2025-01-01",
            ),
            "robinhood.reports[0].end_date",
        ),
        (
            robinhood_corrected_statement_toml(
                supersedes='["statements/a.pdf", "statements/a.pdf"]'
            ),
            "robinhood.corrected_statements[0].supersedes",
        ),
    ],
)
def test_robinhood_runtime_manifest_invalid_values_refused(
    tmp_path: Path,
    extra: str,
    field: str,
) -> None:
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=extra)))
    assert exc.value.field == field


_UNSAFE_PATH = "must be a safe relative POSIX-style path without '.' or '..'"
# Each runtime-manifest path field: a builder that puts a TOML basic-string body
# there, and a getter for the loaded value.
_ROBINHOOD_PATH_FIELDS: dict[str, tuple[Callable[[str], str], Callable[[RobinhoodConfig], str]]] = {
    "robinhood.reports[0].file": (
        lambda body: robinhood_report_toml(file=body),
        lambda rh: rh.reports[0].file,
    ),
    "robinhood.corrected_statements[0].selected_file": (
        lambda body: robinhood_corrected_statement_toml(selected_file=body),
        lambda rh: rh.corrected_statements[0].selected_file,
    ),
    "robinhood.corrected_statements[0].supersedes[0]": (
        lambda body: robinhood_corrected_statement_toml(supersedes=f'["{body}"]'),
        lambda rh: rh.corrected_statements[0].supersedes[0],
    ),
}


@pytest.mark.parametrize("field", _ROBINHOOD_PATH_FIELDS)
@pytest.mark.parametrize(
    "value",
    [
        ".",
        "./b.pdf",
        "statements/./b.pdf",
        "statements//b.pdf",
        "statements/",
        "statements/../b.pdf",
        "..",
        "/statements/b.pdf",
        "statements/b:1.pdf",
        "statements\\b.pdf",
    ],
)
def test_robinhood_runtime_manifest_unsafe_path_refused(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    build, _ = _ROBINHOOD_PATH_FIELDS[field]
    extra = build(json.dumps(value)[1:-1])  # the TOML basic-string body, "\" escaped
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=extra)))
    assert (exc.value.field, exc.value.message) == (field, _UNSAFE_PATH)


@pytest.mark.parametrize("field", _ROBINHOOD_PATH_FIELDS)
@pytest.mark.parametrize(
    "value",
    ["statements/2025-11.pdf", "b.pdf", "a/b/c.pdf", "...", "..b.pdf", "b..", "statements/.b.pdf"],
)
def test_robinhood_runtime_manifest_safe_path_loads(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    build, loaded = _ROBINHOOD_PATH_FIELDS[field]
    cfg = load_user_config(write(tmp_path, robinhood_toml(extra=build(value))))
    assert cfg.robinhood is not None
    assert loaded(cfg.robinhood) == value


@pytest.mark.parametrize(
    ("supersedes", "field"),
    [
        # Another spelling of selected_file passed the selected-in-supersedes check.
        (
            '["statements/./2025-12-corrected.pdf"]',
            "robinhood.corrected_statements[0].supersedes[0]",
        ),
        # Another spelling of an entry passed the duplicate check.
        (
            '["statements/2025-12.pdf", "statements//2025-12.pdf"]',
            "robinhood.corrected_statements[0].supersedes[1]",
        ),
    ],
)
def test_robinhood_corrected_statement_other_spelling_refused(
    tmp_path: Path,
    supersedes: str,
    field: str,
) -> None:
    extra = robinhood_corrected_statement_toml(supersedes=supersedes)
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=extra)))
    assert (exc.value.field, exc.value.message) == (field, _UNSAFE_PATH)


def test_robinhood_runtime_manifest_duplicate_report_refused(
    tmp_path: Path,
) -> None:
    report = robinhood_report_toml(file="activity/one.csv")
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=report + report)))
    assert exc.value.field == "robinhood.reports"


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (
            robinhood_security_toml(
                symbol="XQDH",
                cusip="99972E105",
                isin="US99972E1055",
                issuer_country_code="GB",
                is_depositary_receipt="false",
            ),
            False,
        ),
        (
            robinhood_security_toml(
                symbol="XQCD",
                cusip="99972A103",
                isin="US99972A1034",
                issuer_country_code="US",
                is_depositary_receipt="true",
            ),
            True,
        ),
    ],
)
def test_robinhood_depositary_status_is_independent_attestation(
    tmp_path: Path,
    block: str,
    expected: bool,
) -> None:
    cfg = load_user_config(write(tmp_path, robinhood_toml(extra=block)))
    assert cfg.robinhood is not None
    assert cfg.robinhood.securities[0].is_depositary_receipt is expected


_PLACEHOLDER = "must replace the generated placeholder"
_SUB_CATEGORY = "must be one of: COMMON, ETF"


@pytest.mark.parametrize(
    ("old", "new", "field", "message"),
    [
        (
            'symbol = "XQCA"',
            'symbol = "xqca"',
            "symbol",
            "must be an uppercase ASCII trading symbol",
        ),
        ('symbol = "XQCA"', 'symbol = "REPLACE"', "symbol", _PLACEHOLDER),
        (
            'cusip = "99972B101"',
            'cusip = "99972B102"',
            "cusip",
            "CUSIP check digit is invalid",
        ),
        (
            'isin = "US99972B1017"',
            'isin = "US99972B1018"',
            "isin",
            "ISIN check digit is invalid",
        ),
        (
            'cusip = "99972B101"',
            'cusip = "99990A309"',
            "isin",
            "US ISIN must contain the configured CUSIP",
        ),
        (
            "is_depositary_receipt = false",
            'is_depositary_receipt = "false"',
            "is_depositary_receipt",
            "must be a boolean (true or false)",
        ),
        (
            'source = "synthetic issuer evidence"',
            'source = " "',
            "source",
            "must be non-empty",
        ),
        (
            'listing_exchange = "NASDAQ"',
            'listing_exchange = "REPLACE"',
            "listing_exchange",
            _PLACEHOLDER,
        ),
        ('sub_category = "COMMON"', 'sub_category = "REPLACE"', "sub_category", _PLACEHOLDER),
        *(
            (
                'sub_category = "COMMON"',
                f'sub_category = "{value}"',
                "sub_category",
                _SUB_CATEGORY,
            )
            for value in ("ETFF", "etf", "Common", "PREFERRED")
        ),
        ('asset_class = "STK"', 'asset_class = "REPLACE"', "asset_class", _PLACEHOLDER),
        (
            'source = "synthetic issuer evidence"',
            'source = "REPLACE"',
            "source",
            _PLACEHOLDER,
        ),
    ],
)
def test_robinhood_security_invalid_values_refused(
    tmp_path: Path,
    old: str,
    new: str,
    field: str,
    message: str,
) -> None:
    block = robinhood_security_toml().replace(old, new)
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=block)))
    assert exc.value.field == f"robinhood.securities[0].{field}"
    assert exc.value.message == message


@pytest.mark.parametrize("sub_category", ["COMMON", "ETF"])
def test_robinhood_security_accepted_sub_categories_load(
    tmp_path: Path,
    sub_category: str,
) -> None:
    block = robinhood_security_toml(sub_category=sub_category)
    cfg = load_user_config(write(tmp_path, robinhood_toml(extra=block)))
    assert cfg.robinhood is not None
    assert cfg.robinhood.securities[0].sub_category == sub_category


def test_robinhood_security_required_and_unknown_fields_refused(tmp_path: Path) -> None:
    missing = robinhood_security_toml().replace('source = "synthetic issuer evidence"\n', "")
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=missing)))
    assert exc.value.field == "robinhood.securities[0].source"

    unknown = robinhood_security_toml().replace(
        'source = "synthetic issuer evidence"',
        'source = "synthetic issuer evidence"\nsurprise = true',
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, robinhood_toml(extra=unknown)))
    assert exc.value.field == "robinhood.securities[0]"
    assert exc.value.message == unknown_key(
        "asset_class, cusip, is_depositary_receipt, isin, issuer_country_code, "
        "listing_exchange, source, sub_category, symbol"
    )


@pytest.mark.parametrize(
    ("first", "second", "field"),
    [
        (
            robinhood_security_toml(),
            robinhood_security_toml(
                symbol="XQAB",
                cusip="99972B101",
                isin="US99972B1017",
            ),
            "cusip",
        ),
        (
            robinhood_security_toml(
                isin="NL99971F1030",
                issuer_country_code="NL",
            ),
            robinhood_security_toml(
                symbol="XQAB",
                cusip="99990A309",
                isin="NL99971F1030",
                issuer_country_code="NL",
            ),
            "isin",
        ),
        (
            robinhood_security_toml(),
            robinhood_security_toml(
                symbol="XQCA",
                cusip="99990A309",
                isin="US99990A3095",
            ),
            "symbol",
        ),
    ],
)
def test_robinhood_security_duplicate_identity_refused_without_leaks(
    tmp_path: Path,
    first: str,
    second: str,
    field: str,
) -> None:
    text = robinhood_toml(extra=first + second)
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == f"robinhood.securities[1].{field}"
    assert "99972B101" not in str(exc.value)
    assert "US99972B1017" not in str(exc.value)


def test_robinhood_securities_must_be_array_of_tables(tmp_path: Path) -> None:
    text = robinhood_toml(parent_fields="securities = {}\n")
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == "robinhood.securities"

    text = robinhood_toml(parent_fields="securities = [1]\n")
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == "robinhood.securities[0]"


def _stockplan_runners(tmp_path: Path, config_path: Path) -> tuple[Callable[[], object], ...]:
    financial_year = FinancialYear(2025)
    return (
        lambda: run_stockplan_income(
            input_dir=tmp_path,
            config_path=config_path,
            output_dir=tmp_path,
            financial_year=financial_year,
        ),
        lambda: inspect_stockplan_cg_evidence(
            input_dir=tmp_path,
            config_path=config_path,
            financial_year=financial_year,
        ),
        lambda: run_stockplan_fa(
            input_dir=tmp_path,
            config_path=config_path,
            output_path=tmp_path / "output.csv",
            calendar_year=2025,
            fetch_prices=False,
        ),
    )


# StockPlan reads [stockplan].opening_date, never [account].opening_date (#600),
# so its runners keep only the account.status guard.
@pytest.mark.parametrize(
    ("present_legacy_fact", "missing_field", "stockplan_guarded"),
    [
        ('status = "BeneficialOwner"\n', "account.opening_date", False),
        ("opening_date = 2025-01-01\n", "account.status", True),
    ],
)
def test_every_legacy_entrypoint_retains_account_fact_guards(
    tmp_path: Path,
    present_legacy_fact: str,
    missing_field: str,
    stockplan_guarded: bool,
) -> None:
    config_text = ROBINHOOD_TOML.replace(
        'holder_name = "Tax Payer"\n',
        f'holder_name = "Tax Payer"\n{present_legacy_fact}',
    )
    config_path = write(tmp_path, config_text)
    input_path = tmp_path / "missing-input"
    output_path = tmp_path / "output.csv"
    financial_year = FinancialYear(2025)
    runners: tuple[Callable[[], object], ...] = (
        lambda: run_schedule_os(
            input_path=input_path,
            config_path=config_path,
            output_path=output_path,
            financial_year=financial_year,
        ),
        lambda: _compute_schedule_fsi_for_pipeline(
            input_path=input_path,
            config_path=config_path,
            financial_year=financial_year,
        ),
        lambda: run_fa_a3(
            input_path=input_path,
            config_path=config_path,
            output_path=output_path,
            calendar_year=2025,
        ),
        lambda: run_schedule_cg(
            input_path=input_path,
            config_path=config_path,
            output_path=output_path,
            financial_year=financial_year,
        ),
        lambda: run_schedule_cg_ltcg(
            input_path=input_path,
            config_path=config_path,
            output_path=output_path,
            financial_year=financial_year,
        ),
    )
    if stockplan_guarded:
        runners += _stockplan_runners(tmp_path, config_path)
    for runner in runners:
        with pytest.raises(ConfigError) as exc:
            runner()
        assert exc.value.field == missing_field


STOCKPLAN_TOML = """\
[account]
holder_name = "Tax Payer"
status = "BeneficialOwner"

[stockplan.security]
symbol = "XQCB"
isin = "US99971P1012"
entity_name = "SAMPLE ISSUER CB Inc."
entity_address = "1 Sample Issuer Street, Sample City, XX"
listing_exchange = "NASDAQ"
issuer_country_code = "US"
nature_of_entity = "Listed company shares"
"""


def test_stockplan_entrypoints_do_not_require_account_opening_date(tmp_path: Path) -> None:
    # Each runner gets past the config guards to input discovery. (#600)
    # The issuer gate runs before discovery, so this config names the verified issuer.
    config_path = write(
        tmp_path,
        """\
[account]
holder_name = "Tax Payer"
status = "BeneficialOwner"

[stockplan.security]
symbol = "GOOG"
isin = "US02079K1079"
entity_name = "Alphabet Inc."
entity_address = "1600 Amphitheatre Parkway, Mountain View, CA"
listing_exchange = "NASDAQ"
issuer_country_code = "US"
nature_of_entity = "Listed company shares"
""",
    )
    for runner in _stockplan_runners(tmp_path, config_path):
        with pytest.raises(IbkrFaError, match=r"\.xlsx found in"):
            runner()


def _stockplan_toml(symbol: str, isin: str) -> str:
    return STOCKPLAN_TOML.replace('symbol = "XQCB"', f'symbol = "{symbol}"').replace(
        'isin = "US99971P1012"', f'isin = "{isin}"'
    )


@pytest.mark.parametrize(
    ("symbol", "isin", "markers"),
    [
        ("XESP", "US99999E1047", ("XESP", "US99999E1047")),
        ("GOOG", "US99999E1047", ("US99999E1047",)),
        ("XESP", "US02079K1079", ("XESP",)),
    ],
)
def test_stockplan_entrypoints_refuse_unverified_issuer(
    tmp_path: Path, symbol: str, isin: str, markers: tuple[str, ...]
) -> None:
    # Only Alphabet GOOG exports are verified (#679). The input dir has no export,
    # so a ConfigError instead of "no .xlsx found" shows each runner refuses
    # before it looks for, let alone reads, a StockPlan export.
    config_path = write(tmp_path, _stockplan_toml(symbol, isin))
    stale = (tmp_path / "schedule-os.csv", tmp_path / "output.csv")
    for path in stale:
        path.write_text("STALE", encoding="utf-8")
    for runner in _stockplan_runners(tmp_path, config_path):
        with pytest.raises(ConfigError) as exc:
            runner()
        assert exc.value.field == "stockplan.security"
        assert exc.value.message == UNVERIFIED_STOCKPLAN_ISSUER
        for marker in markers:
            assert marker not in str(exc.value)
    assert not any(path.exists() for path in stale)


def test_unverified_stockplan_issuer_still_loads(tmp_path: Path) -> None:
    # The guard lives in the StockPlan runners, so IBKR and other commands can
    # still load a config whose [stockplan.security] is unverified.
    cfg = load_user_config(write(tmp_path, _stockplan_toml("XESP", "US99999E1047")))
    assert cfg.stockplan is not None
    assert cfg.stockplan.security.symbol == "XESP"


@pytest.mark.parametrize(
    ("stockplan_table", "expected"),
    [
        ("", None),
        ("[stockplan]\nopening_date = 2025-11-10\n\n", date(2025, 11, 10)),
    ],
)
def test_stockplan_opening_date_is_optional(
    tmp_path: Path, stockplan_table: str, expected: date | None
) -> None:
    text = STOCKPLAN_TOML.replace("[stockplan.security]", f"{stockplan_table}[stockplan.security]")
    cfg = load_user_config(write(tmp_path, text))
    assert cfg.stockplan is not None
    assert cfg.stockplan.opening_date == expected


@pytest.mark.parametrize(("key", "value"), [("asset_class", "STK"), ("sub_category", "COMMON")])
def test_stockplan_security_refuses_a_removed_lot_key(tmp_path: Path, key: str, value: str) -> None:
    """#629: as for [netbenefits.security], an old config that still sets
    one of these keys is refused by name."""
    text = STOCKPLAN_TOML.replace(
        "[stockplan.security]\n", f'[stockplan.security]\n{key} = "{value}"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == f"stockplan.security.{key}"
    assert exc.value.message == "is no longer used; delete it from [stockplan.security]"


def test_stockplan_complete_fa_config_builds_its_instrument(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, STOCKPLAN_TOML))
    assert cfg.stockplan is not None
    sec = cfg.stockplan.security
    assert sec.fa_fields_present()
    assert sec.instrument is not None
    assert sec.instrument.symbol == sec.symbol == "XQCB"
    assert sec.instrument.isin == sec.isin == "US99971P1012"
    assert sec.instrument.listing_exchange == "NASDAQ"
    assert sec.instrument.issuer_country_code == "US"


@pytest.mark.parametrize(
    "dropped",
    [
        (
            "entity_name",
            "entity_address",
            "listing_exchange",
            "issuer_country_code",
            "nature_of_entity",
        ),
        ("entity_name",),
        ("entity_address",),
        ("listing_exchange",),
        ("issuer_country_code",),
        ("nature_of_entity",),
    ],
)
def test_stockplan_income_only_or_partial_config_has_no_instrument(
    tmp_path: Path, dropped: tuple[str, ...]
) -> None:
    text = "".join(
        line
        for line in STOCKPLAN_TOML.splitlines(keepends=True)
        if line.split(" = ")[0] not in dropped
    )
    cfg = load_user_config(write(tmp_path, text))
    assert cfg.stockplan is not None
    sec = cfg.stockplan.security
    assert sec.symbol == "XQCB"
    assert sec.isin == "US99971P1012"
    assert sec.instrument is None
    assert not sec.fa_fields_present()


def _stockplan_issuer_country_toml(value: str) -> str:
    return STOCKPLAN_TOML.replace('issuer_country_code = "US"', f"issuer_country_code = {value}")


_NOT_ISO2 = "must be a 2-letter uppercase ISO 3166-1 alpha-2 code"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("false", _NOT_ISO2),
        ("0", _NOT_ISO2),
        ("[]", _NOT_ISO2),
        ("{}", _NOT_ISO2),
        ('""', _NOT_ISO2),
        ('"IN"', "must not be 'IN'"),
        ('"ZZ"', UNKNOWN_COUNTRY_CODE),
    ],
)
def test_stockplan_supplied_issuer_country_code_is_validated(
    tmp_path: Path, value: str, message: str
) -> None:
    """#933: a supplied key is validated whatever its value; a falsy one used
    to load as if the key were absent."""
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, _stockplan_issuer_country_toml(value)))
    assert exc.value.field == "stockplan.security.issuer_country_code"
    assert exc.value.message == message


def test_stockplan_entrypoints_refuse_a_falsy_issuer_country_code(tmp_path: Path) -> None:
    # #933: stockplan-income used to run with this value, and stockplan-fa
    # refused it as incomplete. Every runner now refuses it at config load,
    # before discovery (the input dir has no export) and before any write.
    config_path = write(tmp_path, _stockplan_issuer_country_toml("false"))
    for runner in _stockplan_runners(tmp_path, config_path):
        with pytest.raises(ConfigError) as exc:
            runner()
        assert exc.value.field == "stockplan.security.issuer_country_code"
    assert list(tmp_path.iterdir()) == [config_path]


@pytest.mark.parametrize("field", ["entity_name", "entity_address", "nature_of_entity"])
def test_stockplan_fa_fields_present_still_checks_entity_fields(tmp_path: Path, field: str) -> None:
    cfg = load_user_config(write(tmp_path, STOCKPLAN_TOML))
    assert cfg.stockplan is not None
    cleared = replace(cfg.stockplan.security, **{field: ""})
    assert cleared.instrument is not None
    assert not cleared.fa_fields_present()


def test_stockplan_opening_date_must_be_a_date(tmp_path: Path) -> None:
    text = STOCKPLAN_TOML.replace(
        "[stockplan.security]", '[stockplan]\nopening_date = "not-a-date"\n\n[stockplan.security]'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == "stockplan.opening_date"


def test_account_us_person_absent_defaults_false(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    assert cfg.account.us_person is False


def test_account_us_person_true(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML + "us_person = true\n"))
    assert cfg.account.us_person is True


def test_account_us_person_false_explicit(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML + "us_person = false\n"))
    assert cfg.account.us_person is False


def test_account_us_person_integer_rejected(tmp_path: Path) -> None:
    """bool is a subclass of int; us_person = 1 must NOT silently coerce."""
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, VALID_TOML + "us_person = 1\n"))
    assert exc_info.value.field == "account.us_person"
    assert "boolean" in exc_info.value.message


def test_account_us_person_string_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, VALID_TOML + 'us_person = "true"\n'))
    assert exc_info.value.field == "account.us_person"


# ---- [account] anonymisation opt-ins (mirror the CLI flags) ----------------


def test_account_anonymisation_opt_ins_default_false(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    assert cfg.account.allow_masked_account is False
    assert cfg.account.accept_anonymised_statement is False


def test_account_allow_masked_account_true(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML + "allow_masked_account = true\n"))
    assert cfg.account.allow_masked_account is True


def test_account_accept_anonymised_statement_true(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML + "accept_anonymised_statement = true\n"))
    assert cfg.account.accept_anonymised_statement is True


def test_account_allow_masked_account_integer_rejected(tmp_path: Path) -> None:
    """bool is a subclass of int; a `= 1` typo must NOT silently coerce."""
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, VALID_TOML + "allow_masked_account = 1\n"))
    assert exc_info.value.field == "account.allow_masked_account"
    assert "boolean" in exc_info.value.message


def test_account_accept_anonymised_statement_string_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, VALID_TOML + 'accept_anonymised_statement = "yes"\n'))
    assert exc_info.value.field == "account.accept_anonymised_statement"


def test_effective_anonymisation_flags_or_semantics(tmp_path: Path) -> None:
    """Either the CLI flag OR the [account] opt-in enables the behaviour."""
    cfg_off = load_user_config(write(tmp_path, VALID_TOML))
    # Neither source -> disabled.
    assert effective_anonymisation_flags(
        cfg_off, allow_masked_account=False, accept_anonymised_statement=False
    ) == (False, False)
    # CLI flag alone (config opt-ins false) -> enabled.
    assert effective_anonymisation_flags(
        cfg_off, allow_masked_account=True, accept_anonymised_statement=True
    ) == (True, True)
    # Config opt-in alone (no CLI flag) -> enabled.
    cfg_on = load_user_config(
        write(
            tmp_path,
            VALID_TOML + "allow_masked_account = true\naccept_anonymised_statement = true\n",
        )
    )
    assert effective_anonymisation_flags(
        cfg_on, allow_masked_account=False, accept_anonymised_statement=False
    ) == (True, True)


# ---- [account.foreign_tins] map --------------------------------------------


def test_account_foreign_tins_absent_defaults_empty(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    assert dict(cfg.account.foreign_tins) == {}


def test_account_foreign_tins_parsed(tmp_path: Path) -> None:
    cfg = load_user_config(
        write(
            tmp_path,
            VALID_TOML + '\n[account.foreign_tins]\nUS = "P1234567"\nGB = "AB123456C"\n',
        )
    )
    assert dict(cfg.account.foreign_tins) == {"US": "P1234567", "GB": "AB123456C"}


@pytest.mark.parametrize(
    ("country", "tin"),
    [
        ("US", "123-45-6789"),
        ("US", "P1234567"),
        ("GB", "X" * 75),
    ],
)
def test_account_foreign_tins_preserve_country_formats(
    tmp_path: Path, country: str, tin: str
) -> None:
    cfg = load_user_config(
        write(tmp_path, VALID_TOML + f'\n[account.foreign_tins]\n{country} = "{tin}"\n')
    )
    assert cfg.account.foreign_tins[country] == tin


def test_account_foreign_tins_legacy_na_is_treated_as_missing(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML + '\n[account.foreign_tins]\nUS = "NA"\n'))
    assert "US" not in cfg.account.foreign_tins


@pytest.mark.parametrize("tin", [" na ", "X" * 76])
def test_account_foreign_tins_refuse_placeholder_or_schema_overflow(
    tmp_path: Path, tin: str
) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, VALID_TOML + f'\n[account.foreign_tins]\nUS = "{tin}"\n'))
    assert exc_info.value.field == "account.foreign_tins[0]"


def test_account_foreign_tins_lowercase_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, VALID_TOML + '\n[account.foreign_tins]\nus = "x"\n'))
    assert exc_info.value.field == "account.foreign_tins[0]"
    assert "ISO 3166-1 alpha-2" in exc_info.value.message


def test_account_foreign_tins_india_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(
            write(tmp_path, VALID_TOML + '\n[account.foreign_tins]\nIN = "ABCDE1234F"\n')
        )
    assert exc_info.value.field == "account.foreign_tins[0]"
    assert "foreign-source" in exc_info.value.message


@pytest.mark.parametrize("key", ["UK", "ZZ"])
def test_account_foreign_tins_key_not_in_itr_country_list_rejected(
    tmp_path: Path, key: str
) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(
            write(tmp_path, VALID_TOML + f'\n[account.foreign_tins]\n{key} = "AB123456C"\n')
        )
    assert exc_info.value.field == "account.foreign_tins[0]"
    assert exc_info.value.message == UNKNOWN_COUNTRY_CODE


def test_account_foreign_tins_empty_value_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, VALID_TOML + '\n[account.foreign_tins]\nUS = "  "\n'))
    assert exc_info.value.field == "account.foreign_tins[0]"
    assert "non-empty" in exc_info.value.message


def test_account_foreign_tins_non_string_value_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, VALID_TOML + "\n[account.foreign_tins]\nUS = 12345\n"))
    assert exc_info.value.field == "account.foreign_tins[0]"


def test_account_foreign_tins_three_letter_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, VALID_TOML + '\n[account.foreign_tins]\nUSA = "x"\n'))
    assert exc_info.value.field == "account.foreign_tins[0]"


def test_fx_unknown_field_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, VALID_TOML + "\n[fx]\nbogus_thing = 1\n"))
    assert exc_info.value.field == "fx"
    assert exc_info.value.message == unknown_key("max_carry_forward_days")


def test_fx_max_carry_forward_days_negative_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, VALID_TOML + "\n[fx]\nmax_carry_forward_days = -1\n"))
    assert exc_info.value.field == "fx.max_carry_forward_days"
    assert "non-negative" in exc_info.value.message


def test_fx_max_carry_forward_days_must_be_int(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, VALID_TOML + "\n[fx]\nmax_carry_forward_days = 14.5\n"))
    assert exc_info.value.field == "fx.max_carry_forward_days"


def test_fx_max_carry_forward_days_zero_allowed(tmp_path: Path) -> None:
    """zero disables carry-forward — already covered by FaExactDatePolicy tests."""
    cfg = load_user_config(write(tmp_path, VALID_TOML + "\n[fx]\nmax_carry_forward_days = 0\n"))
    assert cfg.fx.max_carry_forward_days == 0


def test_fx_bool_for_max_carry_forward_days_rejected(tmp_path: Path) -> None:
    """bool is a subclass of int in Python; explicitly reject it for safety."""
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, VALID_TOML + "\n[fx]\nmax_carry_forward_days = true\n"))
    assert exc_info.value.field == "fx.max_carry_forward_days"


def test_account_table_must_be_a_table(tmp_path: Path) -> None:
    bad = '[fx]\naccount = "x"\n'
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, bad))
    # Either ConfigError(account, missing) or ConfigError(account, must be table)
    # Acceptable as long as the path is `account`.
    assert exc_info.value.field == "account"


def test_user_config_dataclass_is_frozen(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    with pytest.raises(AttributeError):
        cfg.account = AccountConfig(  # ty: ignore[invalid-assignment]
            number=cfg.account.number,
            holder_name="other",
            opening_date=cfg.account.opening_date,
            status=cfg.account.status,
        )


def test_fx_default_when_no_fx_table(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    assert cfg.fx == FxConfig()
    assert cfg.fx.max_carry_forward_days == 14


# ---- #183 [os] schedule_os_indian_adr_routing -----------------------------


def test_os_default_when_no_os_table(tmp_path: Path) -> None:
    """Absent ``[os]`` table → default OsConfig (depositary mode)."""
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    assert cfg.os == OsConfig()
    assert cfg.os.schedule_os_indian_adr_routing == ScheduleOsIndianAdrRoutingMode.DEPOSITARY


def test_os_explicit_depositary_mode(tmp_path: Path) -> None:
    """``[os].schedule_os_indian_adr_routing = "depositary"`` is the
    explicit form of the default mode — verifies the loader accepts
    the explicit setting (not just the implicit default)."""
    cfg = load_user_config(
        write(
            tmp_path,
            VALID_TOML + '\n[os]\nschedule_os_indian_adr_routing = "depositary"\n',
        )
    )
    assert cfg.os.schedule_os_indian_adr_routing == ScheduleOsIndianAdrRoutingMode.DEPOSITARY


def test_os_explicit_underlying_issuer_mode(tmp_path: Path) -> None:
    """Issue #183: opt-in to underlying-issuer routing."""
    cfg = load_user_config(
        write(
            tmp_path,
            VALID_TOML + '\n[os]\nschedule_os_indian_adr_routing = "underlying-issuer"\n',
        )
    )
    assert cfg.os.schedule_os_indian_adr_routing == ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER


def test_os_invalid_mode_rejected(tmp_path: Path) -> None:
    """Issue #183: typo'd modes (e.g., ``"underlying_issuer"`` with an
    underscore instead of a hyphen) are loud failures, not silent
    fallbacks. This is the same anti-silent-typo principle the rest
    of the loader follows.
    """
    with pytest.raises(ConfigError) as exc:
        load_user_config(
            write(
                tmp_path,
                VALID_TOML + '\n[os]\nschedule_os_indian_adr_routing = "underlying_issuer"\n',
            )
        )
    assert exc.value.field == "os.schedule_os_indian_adr_routing"
    # Error message lists the valid values.
    assert "depositary" in str(exc.value)
    assert "underlying-issuer" in str(exc.value)


def test_os_unknown_field_rejected(tmp_path: Path) -> None:
    """Extra fields under ``[os]`` are strict typos."""
    with pytest.raises(ConfigError) as exc:
        load_user_config(
            write(
                tmp_path,
                VALID_TOML + '\n[os]\nsome_typo = "value"\n',
            )
        )
    assert exc.value.field == "os"
    assert exc.value.message == unknown_key("schedule_os_indian_adr_routing")


def test_os_table_must_be_a_table(tmp_path: Path) -> None:
    """``os = "something"`` (scalar instead of table) is a loud failure.

    Note: ``os = "scalar"`` after ``[account]`` in TOML attaches to the
    ``[account]`` table (not the top level), so the test places the
    bad value BEFORE ``[account]`` to ensure it lands at the top level.
    """
    toml = 'os = "scalar"\n' + VALID_TOML
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "os"


def test_os_mode_must_be_string(tmp_path: Path) -> None:
    """Non-string values for ``schedule_os_indian_adr_routing`` (e.g.,
    a TOML integer literal) are loud failures, not silent coercions.
    Matches the same anti-silent-typo principle that protects
    ``account.us_person`` from accidental ``1``/``0`` integer values.
    """
    with pytest.raises(ConfigError) as exc:
        load_user_config(
            write(
                tmp_path,
                VALID_TOML + "\n[os]\nschedule_os_indian_adr_routing = 42\n",
            )
        )
    assert exc.value.field == "os.schedule_os_indian_adr_routing"


def test_user_config_round_trip_via_toml_datetime_literal(tmp_path: Path) -> None:
    """TOML date literal (no quotes) is the recommended form in user.toml."""
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    assert isinstance(cfg.account.opening_date, type(date(2024, 1, 1)))
    assert cfg.account.opening_date.isoformat() == "2024-02-01"


def test_user_config_namespace_export(tmp_path: Path) -> None:
    """UserConfig and dependents are importable from ibkr_fa.config top-level."""
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    assert isinstance(cfg, UserConfig)
    assert isinstance(cfg.account, AccountConfig)
    assert isinstance(cfg.fx, FxConfig)


# ============================================================================
# [form67] table — PR step 4c/N of #37
# ============================================================================


def test_form67_default_when_no_form67_table(tmp_path: Path) -> None:
    """A config without [form67] still loads with no exact inputs."""
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    assert isinstance(cfg.form67, Form67Config)
    assert cfg.form67.tax_payable_overrides == ()
    assert cfg.form67.section_91 is None


_FORM67_RETIRED_EXPLANATION = (
    ". Form 67 no longer derives Indian tax payable from a rate, regime or threshold, so "
    "rows without an exact [[form67.tax_payable_overrides]] entry leave the Indian-tax and "
    "FTC columns blank. Put exact source-level amounts in [[form67.tax_payable_overrides]]; "
    "non-DTAA (section 91) claims also need the [form67.section_91] inputs"
)
_FORM67_EXACT_OVERRIDE = (
    '\n[[form67.tax_payable_overrides]]\ncountry = "US"\nsource = "DIVIDEND"\n'
    'tax_payable_normal_inr = "100"\n'
)


def _delete_form67_keys(toml: str, keys: list[str]) -> str:
    """Delete each named key's line from the TOML, as the refusal says to."""
    for key in keys:
        toml, count = re.subn(rf"^{key} = .*\n", "", toml, flags=re.MULTILINE)
        assert count == 1
    return toml


@pytest.mark.parametrize("with_override", [False, True], ids=["bare", "with-override"])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("default_marginal_rate_percent", '"31.2"'),
        ("tax_regime", '"new"'),
        ("income_over_threshold", "true"),
    ],
)
def test_form67_removed_fields_have_targeted_migration_error(
    tmp_path: Path, key: str, value: str, with_override: bool
) -> None:
    """#1518: a retired [form67] key refuses by name and says to delete it,
    even beside an exact override; deleting it loads on the next run."""
    override = _FORM67_EXACT_OVERRIDE if with_override else ""
    toml = VALID_TOML + f"\n[form67]\n{key} = {value}\n" + override
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == f"form67.{key}"
    assert exc.value.message == (
        "is no longer supported; delete it from [form67]" + _FORM67_RETIRED_EXPLANATION
    )
    cfg = load_user_config(write(tmp_path, _delete_form67_keys(toml, [key])))
    assert len(cfg.form67.tax_payable_overrides) == int(with_override)


@pytest.mark.parametrize("with_override", [False, True], ids=["bare", "with-override"])
@pytest.mark.parametrize("reverse", [False, True], ids=["listed-order", "reverse-order"])
@pytest.mark.parametrize(
    "keys",
    [
        ("default_marginal_rate_percent", "tax_regime", "income_over_threshold"),
        ("default_marginal_rate_percent", "tax_regime"),
        ("default_marginal_rate_percent", "income_over_threshold"),
        ("tax_regime", "income_over_threshold"),
    ],
)
def test_form67_names_every_retired_key_at_once(
    tmp_path: Path, keys: tuple[str, ...], reverse: bool, with_override: bool
) -> None:
    """#1518: one refusal names every retired [form67] key the table sets, in
    the order the loader checks them whatever order the table sets them in,
    and says to delete them; deleting exactly the named keys loads on the
    next run."""
    values = {
        "default_marginal_rate_percent": '"20"',
        "tax_regime": '"new"',
        "income_over_threshold": "true",
    }
    lines = [f"{key} = {values[key]}\n" for key in (reversed(keys) if reverse else keys)]
    override = _FORM67_EXACT_OVERRIDE if with_override else ""
    toml = VALID_TOML + "\n[form67]\n" + "".join(lines) + override
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == f"form67.{keys[0]}"
    prefix, suffix = "keys no longer supported: ", "; delete them from [form67]"
    assert exc.value.message == (prefix + ", ".join(keys) + suffix + _FORM67_RETIRED_EXPLANATION)
    named = exc.value.message.removeprefix(prefix).split(suffix)[0].split(", ")
    cfg = load_user_config(write(tmp_path, _delete_form67_keys(toml, named)))
    assert len(cfg.form67.tax_payable_overrides) == int(with_override)


def test_form67_single_override_only(tmp_path: Path) -> None:
    """Minimal: one exact override supplied, nothing else."""
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "US"\n'
        + 'source = "DIVIDEND"\n'
        + 'tax_payable_normal_inr = "12345.67"\n'
    )
    cfg = load_user_config(write(tmp_path, toml))
    assert len(cfg.form67.tax_payable_overrides) == 1
    ov = cfg.form67.tax_payable_overrides[0]
    assert ov.iso_alpha2 == "US"
    assert ov.source_of_income is Form67SourceOfIncome.DIVIDEND
    assert ov.tax_payable_normal_inr == Decimal("12345.67")


def test_form67_multiple_overrides_preserve_toml_order(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "US"\n'
        + 'source = "DIVIDEND"\n'
        + 'tax_payable_normal_inr = "100"\n'
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "GB"\n'
        + 'source = "DIVIDEND"\n'
        + 'tax_payable_normal_inr = "200"\n'
    )
    cfg = load_user_config(write(tmp_path, toml))
    assert len(cfg.form67.tax_payable_overrides) == 2
    assert [(o.iso_alpha2, o.source_of_income) for o in cfg.form67.tax_payable_overrides] == [
        ("US", Form67SourceOfIncome.DIVIDEND),
        ("GB", Form67SourceOfIncome.DIVIDEND),
    ]


def test_form67_override_tax_payable_must_be_string_not_bare_integer(
    tmp_path: Path,
) -> None:
    """Same rejection for an override row's tax_payable_normal_inr field."""
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "US"\n'
        + 'source = "DIVIDEND"\n'
        + "tax_payable_normal_inr = 100\n"
    )
    with pytest.raises(ConfigError, match="Decimal-formatted string"):
        load_user_config(write(tmp_path, toml))


def test_form67_override_rejects_invalid_iso(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "USA"\n'  # 3 letters — wrong
        + 'source = "DIVIDEND"\n'
        + 'tax_payable_normal_inr = "100"\n'
    )
    with pytest.raises(ConfigError, match="ISO 3166-1 alpha-2"):
        load_user_config(write(tmp_path, toml))


def test_form67_override_rejects_lowercase_iso(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "us"\n'
        + 'source = "DIVIDEND"\n'
        + 'tax_payable_normal_inr = "100"\n'
    )
    with pytest.raises(ConfigError, match="uppercase"):
        load_user_config(write(tmp_path, toml))


@pytest.mark.parametrize(
    ("country", "message"),
    [
        ("ZZ", UNKNOWN_COUNTRY_CODE),
        ("IN", "must not be 'IN' — Form 67 covers foreign tax only"),
    ],
)
def test_form67_override_rejects_country_outside_foreign_itr_list(
    tmp_path: Path, country: str, message: str
) -> None:
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[[form67.tax_payable_overrides]]\n"
        + f'country = "{country}"\n'
        + 'source = "DIVIDEND"\n'
        + 'tax_payable_normal_inr = "100"\n'
    )
    with pytest.raises(ConfigError) as exc_info:
        load_user_config(write(tmp_path, toml))
    assert exc_info.value.field == "form67.tax_payable_overrides[0].country"
    assert exc_info.value.message == message


def test_form67_override_rejects_invalid_source_name(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "US"\n'
        + 'source = "Dividend"\n'  # enum VALUE, not NAME — wrong
        + 'tax_payable_normal_inr = "100"\n'
    )
    with pytest.raises(ConfigError, match="enum NAME"):
        load_user_config(write(tmp_path, toml))


def test_form67_override_accepts_all_source_enum_names(tmp_path: Path) -> None:
    """Every Form67SourceOfIncome enum NAME must be a valid source value
    (parses cleanly via Form67SourceOfIncome[name])."""
    for name in [m.name for m in Form67SourceOfIncome]:
        toml = (
            VALID_TOML
            + "\n[form67]\n"
            + "\n[[form67.tax_payable_overrides]]\n"
            + 'country = "US"\n'
            + f'source = "{name}"\n'
            + 'tax_payable_normal_inr = "100"\n'
        )
        cfg = load_user_config(write(tmp_path, toml))
        ov = cfg.form67.tax_payable_overrides[0]
        assert ov.source_of_income is Form67SourceOfIncome[name]


def test_form67_override_tax_payable_must_be_non_negative(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "US"\n'
        + 'source = "DIVIDEND"\n'
        + 'tax_payable_normal_inr = "-100"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "form67.tax_payable_overrides[0].tax_payable_normal_inr"
    assert exc.value.message == "must be a decimal number of at least 0"


def test_form67_override_tax_payable_zero_is_valid(tmp_path: Path) -> None:
    """Zero is a legitimate value: foreign income is wholly absorbed
    by carry-forward losses, so Indian tax payable on it is ₹0."""
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "US"\n'
        + 'source = "DIVIDEND"\n'
        + 'tax_payable_normal_inr = "0"\n'
    )
    cfg = load_user_config(write(tmp_path, toml))
    assert cfg.form67.tax_payable_overrides[0].tax_payable_normal_inr == Decimal("0")


def test_form67_override_tax_payable_must_be_string_not_float(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "US"\n'
        + 'source = "DIVIDEND"\n'
        + "tax_payable_normal_inr = 100.50\n"
    )
    with pytest.raises(ConfigError, match="Decimal-formatted string"):
        load_user_config(write(tmp_path, toml))


def test_form67_rejects_duplicate_overrides(tmp_path: Path) -> None:
    """Two overrides for the same (country, source) pair would silently
    foot-gun the user: the second silently overriding the first would
    leave the FIRST value un-applied with no warning. Reject at parse
    time."""
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "US"\n'
        + 'source = "DIVIDEND"\n'
        + 'tax_payable_normal_inr = "100"\n'
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "US"\n'
        + 'source = "DIVIDEND"\n'
        + 'tax_payable_normal_inr = "200"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "form67.tax_payable_overrides[1]"
    assert exc.value.message == "duplicates another override for the same country and source"


def test_form67_distinct_iso_or_distinct_source_is_not_a_duplicate(
    tmp_path: Path,
) -> None:
    """Two overrides for the same country but different source-of-income
    are distinct; same source but different country are distinct."""
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "US"\n'
        + 'source = "DIVIDEND"\n'
        + 'tax_payable_normal_inr = "100"\n'
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "US"\n'
        + 'source = "INTEREST_INCOME"\n'
        + 'tax_payable_normal_inr = "150"\n'
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "GB"\n'
        + 'source = "DIVIDEND"\n'
        + 'tax_payable_normal_inr = "200"\n'
    )
    cfg = load_user_config(write(tmp_path, toml))
    assert len(cfg.form67.tax_payable_overrides) == 3


def test_form67_table_must_be_a_table(tmp_path: Path) -> None:
    """`form67 = "string"` (not a table) is invalid TOML for our schema.

    The scalar must be at the file's top-level scope; in TOML, once you
    enter a sub-table header like ``[account]``, subsequent ``key =
    value`` lines are nested under that sub-table — so to express
    "form67 is a top-level scalar", it must be declared BEFORE any
    sub-table header.
    """
    toml = 'form67 = "not a table"\n\n' + VALID_TOML
    with pytest.raises(ConfigError, match="must be a TOML table"):
        load_user_config(write(tmp_path, toml))


def test_form67_unknown_field_rejected(tmp_path: Path) -> None:
    toml = VALID_TOML + "\n[form67]\n" + 'totally_made_up_field = "x"\n'
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "form67"
    assert exc.value.message == unknown_key("section_91, tax_payable_overrides")


def test_form67_override_unknown_field_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "US"\n'
        + 'source = "DIVIDEND"\n'
        + 'tax_payable_normal_inr = "100"\n'
        + 'totally_made_up_field = "x"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "form67.tax_payable_overrides[0]"
    assert exc.value.message == unknown_key("country, source, tax_payable_normal_inr")


def test_form67_override_missing_required_field_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "US"\n'
        + 'source = "DIVIDEND"\n'
        # missing tax_payable_normal_inr
    )
    with pytest.raises(ConfigError, match="missing required field"):
        load_user_config(write(tmp_path, toml))


def test_form67_overrides_array_must_be_a_list(tmp_path: Path) -> None:
    """If user writes `tax_payable_overrides = "x"` (string instead of
    array of tables), reject with a pointer to the right TOML syntax."""
    toml = VALID_TOML + "\n[form67]\n" + 'tax_payable_overrides = "not an array"\n'
    with pytest.raises(ConfigError, match="array of tables"):
        load_user_config(write(tmp_path, toml))


# ============================================================================
# [form67.section_91] sub-table — PR step 4d/N of #37
# ============================================================================


def test_form67_section_91_absent_keeps_section_91_none(tmp_path: Path) -> None:
    """Backward compat: a [form67] table without a section_91 sub-table
    sets Form67Config.section_91 = None. The compute layer's §91
    pathway will leave cols 12 + 13 as None for NO_DTAA countries."""
    toml = VALID_TOML + "\n[form67]\n"
    cfg = load_user_config(write(tmp_path, toml))
    assert cfg.form67.section_91 is None


def test_form67_section_91_both_fields_supplied(tmp_path: Path) -> None:
    """Happy path: both required fields supplied as Decimal strings."""
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[form67.section_91]\n"
        + 'total_indian_tax_before_91_relief_inr = "500000"\n'
        + 'total_income_inr = "1500000"\n'
    )
    cfg = load_user_config(write(tmp_path, toml))
    assert cfg.form67.section_91 is not None
    assert cfg.form67.section_91.total_indian_tax_before_91_relief_inr == Decimal("500000")
    assert cfg.form67.section_91.total_income_inr == Decimal("1500000")


def test_form67_section_91_missing_total_tax_rejected(tmp_path: Path) -> None:
    """Both fields are required; missing either is a parse-time error."""
    toml = (
        VALID_TOML + "\n[form67]\n" + "\n[form67.section_91]\n" + 'total_income_inr = "1500000"\n'
    )
    with pytest.raises(ConfigError, match="total_indian_tax_before_91_relief_inr"):
        load_user_config(write(tmp_path, toml))


def test_form67_section_91_missing_total_income_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[form67.section_91]\n"
        + 'total_indian_tax_before_91_relief_inr = "500000"\n'
    )
    with pytest.raises(ConfigError, match="total_income_inr"):
        load_user_config(write(tmp_path, toml))


def test_form67_section_91_total_income_must_be_positive(tmp_path: Path) -> None:
    """Zero total_income would make the §91 Indian average rate
    undefined (0 / 0); reject at parse time."""
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[form67.section_91]\n"
        + 'total_indian_tax_before_91_relief_inr = "500000"\n'
        + 'total_income_inr = "0"\n'
    )
    with pytest.raises(ConfigError, match="must be > 0"):
        load_user_config(write(tmp_path, toml))


def test_form67_section_91_total_tax_zero_is_legitimate(tmp_path: Path) -> None:
    """Total Indian tax = 0 is legitimate (e.g., total income below the
    basic exemption limit). Indian average rate = 0, so §91 relief
    will compute as 0 — which is structurally correct."""
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[form67.section_91]\n"
        + 'total_indian_tax_before_91_relief_inr = "0"\n'
        + 'total_income_inr = "300000"\n'
    )
    cfg = load_user_config(write(tmp_path, toml))
    assert cfg.form67.section_91 is not None
    assert cfg.form67.section_91.total_indian_tax_before_91_relief_inr == Decimal("0")


def test_form67_section_91_total_tax_negative_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[form67.section_91]\n"
        + 'total_indian_tax_before_91_relief_inr = "-1"\n'
        + 'total_income_inr = "1500000"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "form67.section_91.total_indian_tax_before_91_relief_inr"
    assert exc.value.message == "must be a decimal number of at least 0"


def test_form67_section_91_total_income_negative_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[form67.section_91]\n"
        + 'total_indian_tax_before_91_relief_inr = "500000"\n'
        + 'total_income_inr = "-1"\n'
    )
    # Negative is caught by the require_non_negative check before the
    # positive-required check.
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "form67.section_91.total_income_inr"
    assert exc.value.message == "must be a decimal number of at least 0"


def test_form67_section_91_fields_must_be_string_not_float(tmp_path: Path) -> None:
    """Same Decimal-as-string rule as the other §90 fields. Audit-
    grade precision matters since the average rate is derived."""
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[form67.section_91]\n"
        + "total_indian_tax_before_91_relief_inr = 500000.50\n"
        + 'total_income_inr = "1500000"\n'
    )
    with pytest.raises(ConfigError, match="Decimal-formatted string"):
        load_user_config(write(tmp_path, toml))


def test_form67_section_91_unknown_field_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[form67.section_91]\n"
        + 'total_indian_tax_before_91_relief_inr = "500000"\n'
        + 'total_income_inr = "1500000"\n'
        + 'completely_made_up_field = "x"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "form67.section_91"
    assert exc.value.message == unknown_key(
        "total_income_inr, total_indian_tax_before_91_relief_inr"
    )


def test_form67_section_91_must_be_a_table(tmp_path: Path) -> None:
    """`section_91 = "string"` is invalid TOML for our schema."""
    toml = VALID_TOML + "\n[form67]\n" + 'section_91 = "not a table"\n'
    with pytest.raises(ConfigError, match="must be a TOML table"):
        load_user_config(write(tmp_path, toml))


def test_form67_with_override_and_section_91(
    tmp_path: Path,
) -> None:
    """Exact override and section_91 inputs round-trip cleanly."""
    toml = (
        VALID_TOML
        + "\n[form67]\n"
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "US"\n'
        + 'source = "DIVIDEND"\n'
        + 'tax_payable_normal_inr = "12345.67"\n'
        + "\n[form67.section_91]\n"
        + 'total_indian_tax_before_91_relief_inr = "500000"\n'
        + 'total_income_inr = "1500000"\n'
    )
    cfg = load_user_config(write(tmp_path, toml))
    assert len(cfg.form67.tax_payable_overrides) == 1
    assert cfg.form67.section_91 is not None
    assert cfg.form67.section_91.total_income_inr == Decimal("1500000")


# ---- [prices] ticker_overrides --------------------------------------------


def test_prices_absent_defaults_to_empty(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    assert cfg.prices.massive_api_key_file is None
    assert cfg.prices.ticker_overrides == ()


@pytest.mark.parametrize("value", ["env", "private/env", "../env", "/tmp/key"])
def test_prices_massive_key_file_accepts_relative_and_absolute_paths(
    tmp_path: Path,
    value: str,
) -> None:
    cfg = load_user_config(
        write(
            tmp_path,
            VALID_TOML + f'\n[prices]\nmassive_api_key_file = "{value}"\n',
        )
    )
    assert cfg.prices.massive_api_key_file == value


@pytest.mark.parametrize("value", ['""', "true"])
def test_prices_massive_key_file_rejects_invalid_value(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(ConfigError) as exc:
        load_user_config(
            write(
                tmp_path,
                VALID_TOML + f"\n[prices]\nmassive_api_key_file = {value}\n",
            )
        )
    assert exc.value.field == "prices.massive_api_key_file"


def test_prices_ticker_override_parsed(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[[prices.ticker_overrides]]\n"
        + 'ibkr_ticker = "XQAL"\n'
        + 'isin = "CA99991A1003"\n'
        + 'yahoo_symbol = "XQAM"\n'
    )
    cfg = load_user_config(write(tmp_path, toml))
    assert len(cfg.prices.ticker_overrides) == 1
    ov = cfg.prices.ticker_overrides[0]
    assert ov.ibkr_ticker == "XQAL"
    assert ov.isin == "CA99991A1003"
    assert ov.yahoo_symbol == "XQAM"


def test_prices_ticker_override_rejects_bad_isin(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[[prices.ticker_overrides]]\n"
        + 'ibkr_ticker = "XQAL"\n'
        + 'isin = "NOT-AN-ISIN"\n'
        + 'yahoo_symbol = "XQAM"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "prices.ticker_overrides[0].isin"


def test_prices_ticker_override_requires_all_fields(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[[prices.ticker_overrides]]\n"
        + 'ibkr_ticker = "XQAL"\n'
        + 'yahoo_symbol = "XQAM"\n'  # missing isin
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert "isin" in exc.value.field


def test_prices_ticker_override_rejects_duplicate_ticker_same_isin(tmp_path: Path) -> None:
    block = (
        "\n[[prices.ticker_overrides]]\n"
        + 'ibkr_ticker = "XQAL"\n'
        + 'isin = "CA99991A1003"\n'
        + 'yahoo_symbol = "XQAM"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + block + block))
    assert "duplicate" in exc.value.message


def test_prices_ticker_override_rejects_duplicate_ticker_different_isin(tmp_path: Path) -> None:
    # The price fetch is ticker-keyed, so two overrides for one ticker
    # (even with different ISINs) cannot both apply — reject to avoid a
    # silent wrong-security valuation.
    toml = (
        VALID_TOML
        + "\n[[prices.ticker_overrides]]\n"
        + 'ibkr_ticker = "XQAL"\n'
        + 'isin = "CA99991A1003"\n'
        + 'yahoo_symbol = "XQAM"\n'
        + "\n[[prices.ticker_overrides]]\n"
        + 'ibkr_ticker = "XQAL"\n'
        + 'isin = "US99992A1097"\n'
        + 'yahoo_symbol = "XQMA"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert "duplicate" in exc.value.message
    assert "ticker-keyed" in exc.value.message


def test_prices_ticker_override_rejects_unknown_field(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[[prices.ticker_overrides]]\n"
        + 'ibkr_ticker = "XQAL"\n'
        + 'isin = "CA99991A1003"\n'
        + 'yahoo_symbol = "XQAM"\n'
        + 'bogus = "x"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "prices.ticker_overrides[0]"
    assert exc.value.message == unknown_key("ibkr_ticker, isin, yahoo_symbol")
    assert "bogus" not in str(exc.value)


# ---- [account.custodian] (Schedule FA Table A2) ---------------------------


def test_custodian_absent_is_none(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    assert cfg.account.custodian is None


def test_custodian_parsed(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML + CUSTODIAN_TOML))
    cust = cfg.account.custodian
    assert cust is not None
    assert cust.institution_name == "Interactive Brokers LLC"
    assert cust.zip_code == "06830"
    assert cust.country_iso2 == "US"


def test_custodian_zip_optional(tmp_path: Path) -> None:
    toml = (
        VALID_TOML + "\n[account.custodian]\n"
        'institution_name = "Interactive Brokers LLC"\n'
        'address = "Greenwich, CT"\n'
        'country_iso2 = "US"\n'
    )
    cfg = load_user_config(write(tmp_path, toml))
    assert cfg.account.custodian is not None
    assert cfg.account.custodian.zip_code == ""


def test_custodian_country_india_rejected(tmp_path: Path) -> None:
    toml = VALID_TOML + CUSTODIAN_TOML.replace('country_iso2 = "US"', 'country_iso2 = "IN"')
    with pytest.raises(ConfigError):
        load_user_config(write(tmp_path, toml))


def test_custodian_country_unknown_rejected(tmp_path: Path) -> None:
    toml = VALID_TOML + CUSTODIAN_TOML.replace('country_iso2 = "US"', 'country_iso2 = "ZZ"')
    with pytest.raises(ConfigError):
        load_user_config(write(tmp_path, toml))


def test_custodian_missing_institution_name_rejected(tmp_path: Path) -> None:
    toml = VALID_TOML + '\n[account.custodian]\naddress = "Greenwich, CT"\ncountry_iso2 = "US"\n'
    with pytest.raises(ConfigError):
        load_user_config(write(tmp_path, toml))


def test_custodian_unknown_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_user_config(write(tmp_path, VALID_TOML + CUSTODIAN_TOML + 'surprise = "x"\n'))


# ---- [[fa.entities]] (Schedule FA Table A3 per-issuer overrides) -----------

_FA_ENTITIES_TOML = (
    "\n[[fa.entities]]\n"
    'isin = "US99990A3095"\n'
    'address = "100 Example Way, Sampleton, CA"\n'
    'zip = "99990"\n'
    'name = "Sample Issuer AA Inc."\n'
    'nature_of_entity = "Company"\n'
)


def test_fa_absent_is_empty(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    assert cfg.fa.entities == ()


def test_fa_entities_parsed(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML + _FA_ENTITIES_TOML))
    assert len(cfg.fa.entities) == 1
    e = cfg.fa.entities[0]
    assert e.isin == "US99990A3095"
    assert e.address == "100 Example Way, Sampleton, CA"
    assert e.zip_code == "99990"
    assert e.name == "Sample Issuer AA Inc."
    assert e.nature_of_entity == "Company"
    assert e.prefer_configured_address is False
    assert e.prefer_configured_nature is False
    assert e.prefer_configured_name is False


def test_fa_entity_optional_zip_and_name(tmp_path: Path) -> None:
    toml = (
        VALID_TOML + '\n[[fa.entities]]\nisin = "US99990B1098"\naddress = "50 Sample Plaza, NY"\n'
    )
    cfg = load_user_config(write(tmp_path, toml))
    assert cfg.fa.entities[0].zip_code == ""
    assert cfg.fa.entities[0].name is None
    assert cfg.fa.entities[0].nature_of_entity is None
    assert cfg.fa.entities[0].prefer_configured_address is False
    assert cfg.fa.entities[0].prefer_configured_nature is False
    assert cfg.fa.entities[0].prefer_configured_name is False


def test_fa_entity_can_prefer_reviewed_configured_address(tmp_path: Path) -> None:
    toml = _FA_ENTITIES_TOML + "prefer_configured_address = true\n"
    cfg = load_user_config(write(tmp_path, VALID_TOML + toml))

    assert cfg.fa.entities[0].prefer_configured_address is True


def test_fa_entity_rejects_non_boolean_address_preference(tmp_path: Path) -> None:
    toml = _FA_ENTITIES_TOML + 'prefer_configured_address = "yes"\n'

    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + toml))

    assert exc.value.field == "fa.entities[0].prefer_configured_address"


def test_fa_entity_can_prefer_reviewed_configured_nature(tmp_path: Path) -> None:
    toml = _FA_ENTITIES_TOML + "prefer_configured_nature = true\n"
    cfg = load_user_config(write(tmp_path, VALID_TOML + toml))

    assert cfg.fa.entities[0].prefer_configured_nature is True


def test_fa_entity_rejects_non_boolean_nature_preference(tmp_path: Path) -> None:
    toml = _FA_ENTITIES_TOML + 'prefer_configured_nature = "yes"\n'

    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + toml))

    assert exc.value.field == "fa.entities[0].prefer_configured_nature"


def test_fa_entity_can_prefer_reviewed_configured_name(tmp_path: Path) -> None:
    toml = _FA_ENTITIES_TOML + "prefer_configured_name = true\n"
    cfg = load_user_config(write(tmp_path, VALID_TOML + toml))

    assert cfg.fa.entities[0].prefer_configured_name is True


def test_fa_entity_rejects_non_boolean_name_preference(tmp_path: Path) -> None:
    toml = _FA_ENTITIES_TOML + 'prefer_configured_name = "yes"\n'

    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + toml))

    assert exc.value.field == "fa.entities[0].prefer_configured_name"


@pytest.mark.parametrize("value", ["", "   ", "x" * 35])
def test_fa_entity_invalid_nature_rejected(tmp_path: Path, value: str) -> None:
    toml = (
        VALID_TOML
        + '\n[[fa.entities]]\nisin = "US99990A3095"\naddress = "Sampleton, CA"\n'
        + f'nature_of_entity = "{value}"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "fa.entities[0].nature_of_entity"


_ADDRESSLESS_ENTITY = '\n[[fa.entities]]\nisin = "US99990A3095"\n'


@pytest.mark.parametrize(
    "keys",
    [
        pytest.param("", id="isin-only"),
        pytest.param('name = "Sample Issuer AA Inc."\n', id="name"),
        pytest.param("prefer_configured_address = false\n", id="prefer-address-false"),
        pytest.param(
            'nature_of_entity = "Company"\nprefer_configured_name = true\n'
            "prefer_configured_nature = true\n",
            id="nature-and-preferences",
        ),
    ],
)
def test_fa_entity_without_address_loads(tmp_path: Path, keys: str) -> None:
    """#1449: only isin is required. An entry that leaves out address loads
    with none, for a Robinhood run to find one for the filer to verify."""
    cfg = load_user_config(write(tmp_path, VALID_TOML + _ADDRESSLESS_ENTITY + keys))

    (entity,) = cfg.fa.entities
    assert (entity.address, entity.zip_code) == (None, "")


_SET_ADDRESS_FOR_ZIP = "set address too: zip is filed only with this entry's address"


@pytest.mark.parametrize(
    ("keys", "field", "message"),
    [
        pytest.param('zip = "99990"\n', "fa.entities[0].zip", _SET_ADDRESS_FOR_ZIP, id="zip"),
        pytest.param('zip = ""\n', "fa.entities[0].zip", _SET_ADDRESS_FOR_ZIP, id="empty-zip"),
        pytest.param(
            "prefer_configured_address = true\n",
            "fa.entities[0].prefer_configured_address",
            "set address too: prefer_configured_address = true keeps this entry's address "
            "instead of an automatic one",
            id="prefer-address",
        ),
        pytest.param(
            'address = ""\n', "fa.entities[0].address", "must be non-empty", id="empty-address"
        ),
        pytest.param(
            "address = 1\n", "fa.entities[0].address", "must be a string", id="non-string-address"
        ),
    ],
)
def test_fa_entity_address_rules_still_refuse(
    tmp_path: Path, keys: str, field: str, message: str
) -> None:
    """#1449: zip and prefer_configured_address = true need this entry's
    address, and a present address must still be a non-empty string."""
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + _ADDRESSLESS_ENTITY + keys))

    assert (exc.value.field, exc.value.message) == (field, message)


def test_fa_entity_bad_isin_rejected(tmp_path: Path) -> None:
    toml = VALID_TOML + '\n[[fa.entities]]\nisin = "NOTANISIN"\naddress = "x"\n'
    with pytest.raises(ConfigError):
        load_user_config(write(tmp_path, toml))


def test_fa_entities_duplicate_isin_rejected(tmp_path: Path) -> None:
    toml = VALID_TOML + _FA_ENTITIES_TOML + _FA_ENTITIES_TOML
    with pytest.raises(ConfigError):
        load_user_config(write(tmp_path, toml))


def test_fa_entity_unknown_key_rejected(tmp_path: Path) -> None:
    toml = VALID_TOML + _FA_ENTITIES_TOML.rstrip() + '\ncountry = "US"\n'
    with pytest.raises(ConfigError):
        load_user_config(write(tmp_path, toml))


# ---- [schedule_fa] manual values (CBDT JSON fragment, #39) -----------------

_SCHEDULE_FA_TOML = (
    "\n[schedule_fa]\n"
    'manual_values_account_number = "U1234567"\n'
    "\n[[schedule_fa.manual_values]]\n"
    "calendar_year = 2025\n"
    "a2_peak_balance_inr = 8950\n"
)


def test_schedule_fa_absent_defaults_to_empty(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    assert cfg.schedule_fa.manual_values_account_number is None
    assert cfg.schedule_fa.manual_values == ()
    assert cfg.schedule_fa.manual_value_for(2025) is None


def test_schedule_fa_manual_value_parsed(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML + _SCHEDULE_FA_TOML))
    assert cfg.schedule_fa.manual_values_account_number == "U1234567"
    assert len(cfg.schedule_fa.manual_values) == 1
    entry = cfg.schedule_fa.manual_value_for(2025)
    assert entry is not None
    assert entry.calendar_year == 2025
    assert entry.a2_peak_balance_inr == 8950
    assert cfg.schedule_fa.manual_value_for(2024) is None


def test_schedule_fa_negative_peak_accepted(tmp_path: Path) -> None:
    # A margined account can have a negative net peak; the CBDT schema permits
    # a negative integer balance, so the loader must not clamp/reject it.
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2025\n"
        + "a2_peak_balance_inr = -1500\n"
    )
    cfg = load_user_config(write(tmp_path, toml))
    entry = cfg.schedule_fa.manual_value_for(2025)
    assert entry is not None
    assert entry.a2_peak_balance_inr == -1500


def test_schedule_fa_fractional_peak_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2025\n"
        + "a2_peak_balance_inr = 8950.5\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values[0].a2_peak_balance_inr"


def test_schedule_fa_bool_peak_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2025\n"
        + "a2_peak_balance_inr = true\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values[0].a2_peak_balance_inr"


def test_schedule_fa_bool_calendar_year_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = true\n"
        + "a2_peak_balance_inr = 8950\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values[0].calendar_year"


def test_schedule_fa_out_of_range_calendar_year_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 1999\n"
        + "a2_peak_balance_inr = 8950\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values[0].calendar_year"


def test_schedule_fa_out_of_range_peak_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2025\n"
        + "a2_peak_balance_inr = 100000000000000\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values[0].a2_peak_balance_inr"


def test_schedule_fa_missing_account_binding_rejected(tmp_path: Path) -> None:
    # manual_values present but no account binding → refuse (filed figure must
    # be bound to an account so it can't silently carry across a copied config).
    toml = (
        VALID_TOML
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2025\n"
        + "a2_peak_balance_inr = 8950\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values_account_number"


def test_schedule_fa_account_number_mismatch_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U9999999"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2025\n"
        + "a2_peak_balance_inr = 8950\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values_account_number"


def test_schedule_fa_account_binding_requires_ibkr_number(tmp_path: Path) -> None:
    toml = VALID_TOML.replace('number = "U1234567"\n', "") + _SCHEDULE_FA_TOML
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values_account_number"
    assert "require a real [account].number" in exc.value.message


def test_schedule_fa_account_binding_mismatch_rejected_even_without_values(
    tmp_path: Path,
) -> None:
    # The cross-check fires whenever a binding is configured, even with no
    # manual values yet — a wrong binding is a bug regardless.
    toml = VALID_TOML + "\n[schedule_fa]\n" + 'manual_values_account_number = "U9999999"\n'
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values_account_number"


def test_schedule_fa_masked_account_number_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U*******"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2025\n"
        + "a2_peak_balance_inr = 8950\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values_account_number"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ('"U*******"', "must be the real account number, not the IBKR export mask"),
        ("123", "must be a string"),
        (
            '"U***\\n"',
            "has a control character at position 5; filed text may not contain control characters",
        ),
        (
            '"Ü1234567\\n"',
            "has a control character at position 9; filed text may not contain control characters",
        ),
    ],
    ids=["mask", "not-a-string", "mask-with-line-break", "non-ascii-with-line-break"],
)
def test_schedule_fa_account_number_refusal_order(tmp_path: Path, value: str, message: str) -> None:
    """#854: a control character is refused before the content checks, and
    before the binding check could print [account].number."""
    toml = VALID_TOML + "\n[schedule_fa]\n" + f"manual_values_account_number = {value}\n"
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values_account_number"
    assert exc.value.message == message
    assert "1234567" not in exc.value.message


def test_schedule_fa_empty_account_number_rejected(tmp_path: Path) -> None:
    toml = VALID_TOML + "\n[schedule_fa]\n" + 'manual_values_account_number = ""\n'
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values_account_number"


def test_schedule_fa_duplicate_year_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2025\n"
        + "a2_peak_balance_inr = 8950\n"
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2025\n"
        + "a2_peak_balance_inr = 9100\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values[1]"


def test_schedule_fa_manual_value_missing_required_field_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2025\n"  # missing a2_peak_balance_inr
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert "a2_peak_balance_inr" in exc.value.field


def test_schedule_fa_manual_value_unknown_key_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2025\n"
        + "a2_peak_balance_inr = 8950\n"
        + "a2_closing_balance_inr = 8000\n"  # not a known field
    )
    with pytest.raises(ConfigError):
        load_user_config(write(tmp_path, toml))


def test_schedule_fa_unknown_table_key_rejected(tmp_path: Path) -> None:
    toml = VALID_TOML + "\n[schedule_fa]\n" + 'surprise = "x"\n'
    with pytest.raises(ConfigError):
        load_user_config(write(tmp_path, toml))


def test_schedule_fa_manual_values_not_array_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "manual_values = 5\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values"


def test_schedule_fa_manual_values_require_legacy_opening_date(tmp_path: Path) -> None:
    toml = """\
[account]
number = "U1234567"
holder_name = "Tax Payer"
status = "BeneficialOwner"

[schedule_fa]
manual_values_account_number = "U1234567"

[[schedule_fa.manual_values]]
calendar_year = 2025
a2_peak_balance_inr = 8950
"""
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "account.opening_date"


def test_schedule_fa_calendar_year_before_account_opening_rejected(tmp_path: Path) -> None:
    # VALID_TOML opens the account 2024-02-01; a peak for CY 2023 is impossible.
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2023\n"
        + "a2_peak_balance_inr = 8950\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values[0].calendar_year"


def test_schedule_fa_calendar_year_equal_to_opening_year_accepted(tmp_path: Path) -> None:
    # The opening year itself is valid (the account existed for part of it).
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2024\n"
        + "a2_peak_balance_inr = 8950\n"
    )
    cfg = load_user_config(write(tmp_path, toml))
    assert cfg.schedule_fa.manual_value_for(2024) is not None


def test_schedule_fa_peak_at_max_bound_accepted(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2025\n"
        + "a2_peak_balance_inr = 99999999999999\n"
    )
    cfg = load_user_config(write(tmp_path, toml))
    entry = cfg.schedule_fa.manual_value_for(2025)
    assert entry is not None
    assert entry.a2_peak_balance_inr == 99999999999999


def test_schedule_fa_peak_at_min_bound_accepted(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2025\n"
        + "a2_peak_balance_inr = -99999999999999\n"
    )
    cfg = load_user_config(write(tmp_path, toml))
    entry = cfg.schedule_fa.manual_value_for(2025)
    assert entry is not None
    assert entry.a2_peak_balance_inr == -99999999999999


def test_schedule_fa_peak_below_min_bound_rejected(tmp_path: Path) -> None:
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2025\n"
        + "a2_peak_balance_inr = -100000000000000\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values[0].a2_peak_balance_inr"


def test_schedule_fa_integer_valued_float_peak_rejected(tmp_path: Path) -> None:
    # `8950.0` is a TOML float, not an int — a filed figure must be supplied as
    # a whole-rupee integer literal, never an (even integer-valued) float.
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2025\n"
        + "a2_peak_balance_inr = 8950.0\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values[0].a2_peak_balance_inr"


def test_schedule_fa_calendar_year_above_max_rejected(tmp_path: Path) -> None:
    # Symmetry with the lower-bound case: lock BOTH ends of [2000, 2100].
    toml = (
        VALID_TOML
        + "\n[schedule_fa]\n"
        + 'manual_values_account_number = "U1234567"\n'
        + "\n[[schedule_fa.manual_values]]\n"
        + "calendar_year = 2101\n"
        + "a2_peak_balance_inr = 8950\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values[0].calendar_year"


def test_schedule_fa_account_number_must_be_string(tmp_path: Path) -> None:
    toml = VALID_TOML + "\n[schedule_fa]\n" + "manual_values_account_number = 1234567\n"
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "schedule_fa.manual_values_account_number"


# --- [netbenefits] (Fidelity NetBenefits ESPP support, #307) --------------


def test_netbenefits_absent_by_default(tmp_path: Path) -> None:
    assert load_user_config(write(tmp_path, VALID_TOML)).netbenefits is None


def test_netbenefits_happy_path_round_trips(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML + VALID_NETBENEFITS))
    nb = cfg.netbenefits
    assert nb is not None
    assert nb.espp_discount_pct == Decimal("10")
    assert nb.participant_no == "OQ1234567"
    assert nb.account.institution_name == "Fidelity Stock Plan Services LLC"
    assert nb.account.zip_code == "02210"
    assert nb.account.country_iso2 == "US"
    assert nb.account.account_opening_date == date(2025, 12, 31)
    assert nb.account.status is TaxpayerStatus.BENEFICIAL_OWNER
    assert nb.account.withholding_agent_name == "National Financial Services LLC"
    assert nb.security.instrument.symbol == "XESP"
    assert nb.security.cusip == "99999E104"
    assert str(nb.security.instrument.isin) == "US99999E1047"
    assert nb.security.instrument.listing_exchange == "NASDAQ"
    assert nb.security.instrument.issuer_country_code == "US"
    assert len(nb.price_overrides) == 1
    ov = nb.price_overrides[0]
    assert ov.price_date == date(2025, 12, 31)
    assert ov.price_per_share_usd == Decimal("213.47")
    assert ov.source == "SPS year-end statement"
    assert len(nb.cg_fmv_values) == 1
    cg_fmv = nb.cg_fmv_values[0]
    assert cg_fmv.purchase_date == date(2025, 12, 31)
    assert cg_fmv.section17_gross_fmv_total_inr == Decimal("41850.00")
    assert cg_fmv.source == "Employer perquisite worksheet"
    assert nb.cg_fmv_for(date(2025, 12, 31)) is cg_fmv
    assert nb.cg_fmv_for(date(2025, 12, 30)) is None


_NETBENEFITS_AGENT_LINE = 'withholding_agent_name = "National Financial Services LLC"\n'


def test_netbenefits_withholding_agent_name_is_optional(tmp_path: Path) -> None:
    assert VALID_NETBENEFITS.count(_NETBENEFITS_AGENT_LINE) == 1
    toml = VALID_TOML + VALID_NETBENEFITS.replace(_NETBENEFITS_AGENT_LINE, "")
    nb = load_user_config(write(tmp_path, toml)).netbenefits
    assert nb is not None
    assert nb.account.withholding_agent_name is None


@pytest.mark.parametrize(
    ("value", "message"),
    [('""', "must be non-empty"), ('"   "', "must be non-empty"), ("1", "must be a string")],
)
def test_netbenefits_withholding_agent_name_refuses_a_bad_value(
    tmp_path: Path, value: str, message: str
) -> None:
    toml = VALID_TOML + VALID_NETBENEFITS.replace(
        _NETBENEFITS_AGENT_LINE, f"withholding_agent_name = {value}\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.account.withholding_agent_name"
    assert exc.value.message == message


@pytest.mark.parametrize("withholding_agent", [True, False], ids=["with-agent", "without-agent"])
def test_netbenefits_account_maps_to_a2_identity(tmp_path: Path, withholding_agent: bool) -> None:
    """The withholding agent is not part of the A2 identity (ADR-0019)."""
    netbenefits = (
        VALID_NETBENEFITS
        if withholding_agent
        else VALID_NETBENEFITS.replace(_NETBENEFITS_AGENT_LINE, "")
    )
    nb = load_user_config(write(tmp_path, VALID_TOML + netbenefits)).netbenefits
    assert nb is not None
    assert nb.account.to_account_identity(participant_no=nb.participant_no) == AccountIdentity(
        institution=InstitutionIdentity(
            name="Fidelity Stock Plan Services LLC",
            address="245 Summer Street, Boston, MA",
            zip_code="02210",
            country_iso2="US",
        ),
        account_ref="OQ1234567",
        opening_date=date(2025, 12, 31),
        status=TaxpayerStatus.BENEFICIAL_OWNER,
        base_currency="USD",
        statement_masked_account_ref=False,
    )


def test_netbenefits_config_needs_no_ibkr_account_facts(tmp_path: Path) -> None:
    toml = VALID_TOML
    for line in (
        'number = "U1234567"\n',
        "opening_date = 2024-02-01\n",
        'status = "BeneficialOwner"\n',
    ):
        toml = toml.replace(line, "")
    toml += VALID_NETBENEFITS
    cfg = load_user_config(write(tmp_path, toml))
    assert cfg.account.number is None
    assert cfg.account.opening_date is None
    assert cfg.account.status is None
    assert cfg.netbenefits is not None
    assert cfg.netbenefits.participant_no == "OQ1234567"


def test_netbenefits_cg_fmv_duplicate_date_refused(tmp_path: Path) -> None:
    block = """
[[netbenefits.cg_fmv_values]]
purchase_date = 2025-12-31
section17_gross_fmv_total_inr = "42000"
source = "Duplicate"
"""
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + VALID_NETBENEFITS + block))
    assert exc.value.field == "netbenefits.cg_fmv_values[1]"
    assert exc.value.message == (
        "duplicates another value's purchase_date; each purchase date may appear at most once"
    )
    assert "2025-12-31" not in str(exc.value)


@pytest.mark.parametrize("bad", ['"0"', '"-1"', "41850.0"])
def test_netbenefits_cg_fmv_invalid_amount_refused(tmp_path: Path, bad: str) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'section17_gross_fmv_total_inr = "41850.00"',
        f"section17_gross_fmv_total_inr = {bad}",
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert "section17_gross_fmv_total_inr" in exc.value.field


def test_netbenefits_a2_peak_override_round_trips(tmp_path: Path) -> None:
    nb = load_user_config(write(tmp_path, VALID_TOML + _nb_with_binding() + _A2_PEAK)).netbenefits
    assert nb is not None
    assert len(nb.a2_peak_overrides) == 1
    ov = nb.a2_peak_overrides[0]
    assert (ov.calendar_year, ov.peak_balance_inr) == (2025, 66503)
    assert nb.a2_peak_for(2025) is ov
    assert nb.a2_peak_for(2024) is None
    assert nb.a2_peak_overrides_participant_no == nb.participant_no == "OQ1234567"


def test_netbenefits_a2_peak_override_absent_defaults_empty(tmp_path: Path) -> None:
    nb = load_user_config(write(tmp_path, VALID_TOML + VALID_NETBENEFITS)).netbenefits
    assert nb is not None
    assert nb.a2_peak_overrides == ()
    assert nb.a2_peak_overrides_participant_no is None


def test_netbenefits_a2_peak_override_requires_participant_binding(tmp_path: Path) -> None:
    # Overrides present but no a2_peak_overrides_participant_no → refuse.
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + VALID_NETBENEFITS + _A2_PEAK))
    assert "a2_peak_overrides_participant_no" in exc.value.field


def test_netbenefits_a2_peak_override_participant_binding_mismatch_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + _nb_with_binding("WRONG999") + _A2_PEAK))
    assert "must equal [netbenefits].participant_no" in str(exc.value)


def test_netbenefits_a2_peak_override_duplicate_year_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + _nb_with_binding() + _A2_PEAK + _A2_PEAK))
    assert "duplicate" in str(exc.value)


def test_netbenefits_a2_peak_override_before_opening_refused(tmp_path: Path) -> None:
    # The account opens 2025-12-31, so a 2024 peak is impossible.
    block = _A2_PEAK.replace("calendar_year = 2025", "calendar_year = 2024")
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + _nb_with_binding() + block))
    assert exc.value.field == "netbenefits.a2_peak_overrides[0].calendar_year"
    assert exc.value.message == (
        "is before the year of netbenefits.account.account_opening_date; a Schedule FA "
        "peak for a year the account did not exist is impossible"
    )


def test_netbenefits_a2_peak_override_float_refused(tmp_path: Path) -> None:
    block = _A2_PEAK.replace("peak_balance_inr = 66503", "peak_balance_inr = 66503.5")
    assert block != _A2_PEAK
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + _nb_with_binding() + block))
    assert "peak_balance_inr" in exc.value.field


def test_netbenefits_a2_peak_override_unknown_key_refused(tmp_path: Path) -> None:
    block = _A2_PEAK + 'note = "x"\n'
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + _nb_with_binding() + block))
    assert "a2_peak_overrides" in str(exc.value)


def test_netbenefits_optional_zips_default_empty(tmp_path: Path) -> None:
    assert 'entity_zip = "00000"\n' in VALID_NETBENEFITS
    toml = (
        (VALID_TOML + VALID_NETBENEFITS)
        .replace('zip = "02210"\n', "")
        .replace('entity_zip = "00000"\n', "")
    )
    nb = load_user_config(write(tmp_path, toml)).netbenefits
    assert nb is not None
    assert nb.account.zip_code == ""
    assert nb.security.entity_zip == ""


def test_netbenefits_must_be_a_table(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, "netbenefits = 1\n\n" + VALID_TOML))
    assert exc.value.field == "netbenefits"


@pytest.mark.parametrize(
    ("remove_line", "expected_field"),
    [
        ("account_opening_date = 2025-12-31\n", "netbenefits.account.account_opening_date"),
        (
            'institution_name = "Fidelity Stock Plan Services LLC"\n',
            "netbenefits.account.institution_name",
        ),
        ('symbol = "XESP"\n', "netbenefits.security.symbol"),
        ('isin = "US99999E1047"\n', "netbenefits.security.isin"),
        ('listing_exchange = "NASDAQ"\n', "netbenefits.security.listing_exchange"),
        ('issuer_country_code = "US"\n', "netbenefits.security.issuer_country_code"),
    ],
)
def test_netbenefits_missing_required_field_refused(
    tmp_path: Path, remove_line: str, expected_field: str
) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(remove_line, "")
    assert toml != VALID_TOML + VALID_NETBENEFITS
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == expected_field
    assert "missing required field" in exc.value.message


@pytest.mark.parametrize(("key", "value"), [("asset_class", "STK"), ("sub_category", "COMMON")])
def test_netbenefits_security_refuses_a_removed_lot_key(
    tmp_path: Path, key: str, value: str
) -> None:
    """#629: no lot field reads these keys any more, so an old config that
    still sets one is refused by name rather than silently ignored."""
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        "[netbenefits.security]\n", f'[netbenefits.security]\n{key} = "{value}"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == f"netbenefits.security.{key}"
    assert exc.value.message == "is no longer used; delete it from [netbenefits.security]"


@pytest.mark.parametrize(
    ("text", "table"),
    [
        (VALID_TOML + VALID_NETBENEFITS, "netbenefits.security"),
        (STOCKPLAN_TOML, "stockplan.security"),
    ],
    ids=["netbenefits", "stockplan"],
)
@pytest.mark.parametrize(
    "keys",
    [("asset_class", "sub_category"), ("sub_category", "asset_class")],
    ids=["listed-order", "reverse-order"],
)
def test_security_names_every_removed_lot_key_at_once(
    tmp_path: Path, text: str, table: str, keys: tuple[str, str]
) -> None:
    """#1511: one refusal names every retired key in the table, in
    _REMOVED_LOT_SECURITY_KEYS order whatever order the table sets them in,
    so deleting the named keys loads on the next run."""
    header = f"[{table}]\n"
    assert text.count(header) == 1
    edited = text.replace(header, header + "".join(f'{key} = "X"\n' for key in keys))
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, edited))
    assert exc.value.field == f"{table}.asset_class"
    assert exc.value.message == (
        f"keys no longer used: asset_class, sub_category; delete them from [{table}]"
    )
    for key in ("asset_class", "sub_category"):
        edited = edited.replace(f'{key} = "X"\n', "")
    cfg = load_user_config(write(tmp_path, edited))
    assert (cfg.netbenefits if table == "netbenefits.security" else cfg.stockplan) is not None


def test_netbenefits_missing_status_refused(tmp_path: Path) -> None:
    # The netbenefits status line is identical to the [account] one, so anchor
    # the removal to the preceding (unique) account_opening_date line.
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'account_opening_date = 2025-12-31\nstatus = "BeneficialOwner"\n',
        "account_opening_date = 2025-12-31\n",
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.account.status"
    assert "missing required field" in exc.value.message


def test_netbenefits_unknown_key_refused(tmp_path: Path) -> None:
    toml = VALID_TOML + VALID_NETBENEFITS + "\n[netbenefits]\nsurprise = 1\n"
    # second [netbenefits] header merges keys; instead inject inline:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'participant_no = "OQ1234567"\n', 'participant_no = "OQ1234567"\nsurprise = 1\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits"
    assert exc.value.message == unknown_key(
        "a2_peak_overrides, a2_peak_overrides_participant_no, account, cg_fmv_values, "
        "espp_discount_pct, participant_no, price_overrides, security"
    )


_NETBENEFITS_ACCOUNT_KEYS = (
    "account_opening_date, address, country_iso2, institution_name, status, "
    "withholding_agent_name, zip"
)


def test_netbenefits_account_unknown_key_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'country_iso2 = "US"\naccount_opening_date',
        'country_iso2 = "US"\nsurprise = 1\naccount_opening_date',
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.account"
    assert exc.value.message == unknown_key(_NETBENEFITS_ACCOUNT_KEYS)


@pytest.mark.parametrize(
    ("text", "field"),
    [
        (
            VALID_TOML.replace('status = "BeneficialOwner"', 'status = "SigningAuthority"'),
            "account.status",
        ),
        # Anchor to the (unique) account_opening_date line so only the netbenefits
        # status flips — the [account] block also has status = "BeneficialOwner".
        (
            (VALID_TOML + VALID_NETBENEFITS).replace(
                'account_opening_date = 2025-12-31\nstatus = "BeneficialOwner"',
                'account_opening_date = 2025-12-31\nstatus = "SigningAuthority"',
            ),
            "netbenefits.account.status",
        ),
        (
            robinhood_toml(
                second=_ROBINHOOD_SECOND.replace(
                    'status = "BeneficialOwner"', 'status = "SigningAuthority"'
                )
            ),
            "robinhood.accounts[1].status",
        ),
    ],
    ids=["account", "netbenefits", "robinhood"],
)
def test_signing_authority_status_refused(tmp_path: Path, text: str, field: str) -> None:
    # #887: ibkr-fa doesn't support signing-authority accounts, whatever the source.
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, text))
    assert exc.value.field == field
    assert exc.value.message == (
        "signing-authority accounts aren't supported; the supported statuses are "
        "BeneficialOwner and Joint"
    )


def test_netbenefits_opening_date_datetime_with_time_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        "account_opening_date = 2025-12-31", "account_opening_date = 2025-12-31T09:30:00"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.account.account_opening_date"


def test_netbenefits_opening_date_future_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        "account_opening_date = 2025-12-31", "account_opening_date = 2999-01-01"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.account.account_opening_date"
    assert "future" in exc.value.message


def test_netbenefits_whitespace_only_required_string_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'institution_name = "Fidelity Stock Plan Services LLC"',
        'institution_name = "   "',
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.account.institution_name"


def test_netbenefits_account_country_in_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace('country_iso2 = "US"', 'country_iso2 = "IN"')
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.account.country_iso2"


def test_netbenefits_separate_account_number_is_not_a_config_field(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'country_iso2 = "US"', 'country_iso2 = "US"\naccount_number = "X12345678"'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.account"
    assert exc.value.message == unknown_key(_NETBENEFITS_ACCOUNT_KEYS)


def test_netbenefits_bad_isin_shape_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace('isin = "US99999E1047"', 'isin = "NOTANISIN"')
    assert toml != VALID_TOML + VALID_NETBENEFITS
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.security.isin"


def test_netbenefits_isin_cusip_mismatch_refused(tmp_path: Path) -> None:
    # A valid US ISIN whose embedded CUSIP does not match the configured cusip
    # -> refused (the ISIN must be the US ISIN derived from the cusip).
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'cusip = "99999E104"', 'cusip = "99992A109"'
    )  # XQAQ cusip, configured isin -> mismatch
    assert toml != VALID_TOML + VALID_NETBENEFITS
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.security.isin"
    assert "CUSIP" in exc.value.message


def test_netbenefits_isin_bad_check_digit_refused(tmp_path: Path) -> None:
    # A US ISIN with the wrong check digit (the configured ISIN ends ...047; ...040 is wrong) is
    # refused: it does not equal the US ISIN derived from the configured cusip.
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'isin = "US99999E1047"', 'isin = "US99999E1040"'
    )
    assert toml != VALID_TOML + VALID_NETBENEFITS
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.security.isin"


def test_netbenefits_bad_cusip_shape_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace('cusip = "99999E104"', 'cusip = "99999"')
    assert toml != VALID_TOML + VALID_NETBENEFITS
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.security.cusip"


def test_netbenefits_discount_above_100_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'espp_discount_pct = "10"', 'espp_discount_pct = "150"'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.espp_discount_pct"


def test_netbenefits_discount_zero_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'espp_discount_pct = "10"', 'espp_discount_pct = "0"'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.espp_discount_pct"


def test_netbenefits_discount_float_refused(tmp_path: Path) -> None:
    # A bare TOML float loses audit precision -> must be a Decimal-string.
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'espp_discount_pct = "10"', "espp_discount_pct = 10.0"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.espp_discount_pct"
    assert "Form 67" not in exc.value.message


def test_netbenefits_price_override_non_positive_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'price_per_share_usd = "213.47"', 'price_per_share_usd = "0"'
    )
    assert toml != VALID_TOML + VALID_NETBENEFITS
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.price_overrides[0].price_per_share_usd"


def test_netbenefits_price_override_partial_provenance_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace('reason = "12-31 lot unpriced in CSV"\n', "")
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.price_overrides[0].reason"
    assert "missing required field" in exc.value.message


def test_netbenefits_duplicate_price_override_date_refused(tmp_path: Path) -> None:
    dup = (
        "\n[[netbenefits.price_overrides]]\n"
        "price_date = 2025-12-31\n"
        'price_per_share_usd = "500"\n'
        'source = "another"\n'
        'reason = "dup"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + VALID_NETBENEFITS + dup))
    assert exc.value.field == "netbenefits.price_overrides[1]"
    assert "duplicate" in exc.value.message


def test_netbenefits_opening_date_iso_string_accepted(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        "account_opening_date = 2025-12-31", 'account_opening_date = "2025-12-31"'
    )
    nb = load_user_config(write(tmp_path, toml)).netbenefits
    assert nb is not None
    assert nb.account.account_opening_date == date(2025, 12, 31)


def test_netbenefits_opening_date_invalid_string_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        "account_opening_date = 2025-12-31", 'account_opening_date = "not-a-date"'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.account.account_opening_date"
    assert exc.value.message == "must be an ISO date (YYYY-MM-DD)"
    assert "not-a-date" not in str(exc.value)


def test_netbenefits_opening_date_wrong_type_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        "account_opening_date = 2025-12-31", "account_opening_date = 12345"
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.account.account_opening_date"


def test_netbenefits_unknown_country_code_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'issuer_country_code = "US"', 'issuer_country_code = "ZZ"'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.security.issuer_country_code"
    assert "unknown" in exc.value.message


def test_netbenefits_invalid_status_value_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'account_opening_date = 2025-12-31\nstatus = "BeneficialOwner"',
        'account_opening_date = 2025-12-31\nstatus = "Nonsense"',
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.account.status"
    assert exc.value.message == "must be one of: BeneficialOwner, Joint"


def test_netbenefits_non_us_isin_refused(tmp_path: Path) -> None:
    # NetBenefits ESPP is a US-listed, CUSIP-backed security: a non-US ISIN is
    # refused (it can't be cross-checked against the CUSIP) -> never-be-wrong.
    assert 'isin = "US99999E1047"' in VALID_NETBENEFITS
    toml = (
        (VALID_TOML + VALID_NETBENEFITS)
        .replace('isin = "US99999E1047"', 'isin = "GB99970K1039"')
        .replace('issuer_country_code = "US"', 'issuer_country_code = "GB"')
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.security.isin"
    assert "US ISIN" in exc.value.message


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_netbenefits_discount_non_finite_refused(tmp_path: Path, bad: str) -> None:
    # A non-finite Decimal must refuse cleanly, not crash on `value >= 0`.
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'espp_discount_pct = "10"', f'espp_discount_pct = "{bad}"'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.espp_discount_pct"


@pytest.mark.parametrize("bad", ["NaN", "Infinity"])
def test_netbenefits_price_non_finite_refused(tmp_path: Path, bad: str) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'price_per_share_usd = "213.47"', f'price_per_share_usd = "{bad}"'
    )
    assert toml != VALID_TOML + VALID_NETBENEFITS
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.price_overrides[0].price_per_share_usd"


def test_netbenefits_bad_cusip_check_digit_refused(tmp_path: Path) -> None:
    # 99999E104 is the valid configured CUSIP (check digit 4); ...103 is a typo.
    toml = (VALID_TOML + VALID_NETBENEFITS).replace('cusip = "99999E104"', 'cusip = "99999E103"')
    assert toml != VALID_TOML + VALID_NETBENEFITS
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.security.cusip"
    assert "check digit" in exc.value.message


def test_netbenefits_issuer_country_in_refused(tmp_path: Path) -> None:
    # Schedule FA is for FOREIGN assets — an Indian issuer never belongs on A3,
    # mirroring the IBKR open-positions parser's refusal.
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'issuer_country_code = "US"', 'issuer_country_code = "IN"'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.security.issuer_country_code"
    assert "IN" in exc.value.message


@pytest.mark.parametrize(
    ("field_line", "padded", "expected_field"),
    [
        (
            'participant_no = "OQ1234567"',
            'participant_no = "OQ1234567 "',
            "netbenefits.participant_no",
        ),
        ('symbol = "XESP"', 'symbol = "XESP "', "netbenefits.security.symbol"),
        (
            'institution_name = "Fidelity Stock Plan Services LLC"',
            'institution_name = "Fidelity Stock Plan Services LLC "',
            "netbenefits.account.institution_name",
        ),
    ],
)
def test_netbenefits_surrounding_whitespace_refused(
    tmp_path: Path, field_line: str, padded: str, expected_field: str
) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(field_line, padded)
    assert toml != VALID_TOML + VALID_NETBENEFITS
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == expected_field
    assert "whitespace" in exc.value.message


@pytest.mark.parametrize(
    ("field_line", "bad", "expected_field"),
    [
        ('zip = "02210"', 'zip = "   "', "netbenefits.account.zip"),
        ('zip = "02210"', 'zip = " 02210"', "netbenefits.account.zip"),
        ('entity_zip = "00000"', 'entity_zip = "00000 "', "netbenefits.security.entity_zip"),
    ],
)
def test_netbenefits_optional_zip_surrounding_whitespace_refused(
    tmp_path: Path, field_line: str, bad: str, expected_field: str
) -> None:
    # Optional zips may be empty, but a present value must be canonical.
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(field_line, bad)
    assert toml != VALID_TOML + VALID_NETBENEFITS
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == expected_field
    assert "whitespace" in exc.value.message


@pytest.mark.parametrize(
    ("field_line", "padded", "expected_field"),
    [
        (
            'espp_discount_pct = "10"',
            'espp_discount_pct = " 10 "',
            "netbenefits.espp_discount_pct",
        ),
        (
            'price_per_share_usd = "213.47"',
            'price_per_share_usd = "213.47 "',
            "netbenefits.price_overrides[0].price_per_share_usd",
        ),
    ],
)
def test_netbenefits_decimal_surrounding_whitespace_refused(
    tmp_path: Path, field_line: str, padded: str, expected_field: str
) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(field_line, padded)
    assert toml != VALID_TOML + VALID_NETBENEFITS
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == expected_field
    assert "whitespace" in exc.value.message


def test_netbenefits_zip_non_string_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace('zip = "02210"', "zip = 2210")
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.account.zip"
    assert "must be a string" in exc.value.message


def test_netbenefits_masked_participant_no_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'participant_no = "OQ1234567"', 'participant_no = "OQ****5678"'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.participant_no"
    assert "masked" in exc.value.message


def test_netbenefits_participant_no_equal_ibkr_number_refused(tmp_path: Path) -> None:
    toml = (VALID_TOML + VALID_NETBENEFITS).replace(
        'participant_no = "OQ1234567"', 'participant_no = "U1234567"'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "netbenefits.participant_no"
    assert "[account].number" in exc.value.message


# ---- [schedule_fa] peak_basis (FA-A3 peak valuation) ----------------------


def test_peak_basis_defaults_to_close(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    assert cfg.schedule_fa.peak_basis == "close"


def test_peak_basis_high_accepted(tmp_path: Path) -> None:
    toml = VALID_TOML + '\n[schedule_fa]\npeak_basis = "high"\n'
    cfg = load_user_config(write(tmp_path, toml))
    assert cfg.schedule_fa.peak_basis == "high"


def test_peak_basis_close_explicit_accepted(tmp_path: Path) -> None:
    toml = VALID_TOML + '\n[schedule_fa]\npeak_basis = "close"\n'
    cfg = load_user_config(write(tmp_path, toml))
    assert cfg.schedule_fa.peak_basis == "close"


def test_peak_basis_invalid_value_rejected(tmp_path: Path) -> None:
    toml = VALID_TOML + '\n[schedule_fa]\npeak_basis = "intraday"\n'
    with pytest.raises(ConfigError, match="peak_basis"):
        load_user_config(write(tmp_path, toml))


def test_peak_basis_non_string_rejected(tmp_path: Path) -> None:
    toml = VALID_TOML + "\n[schedule_fa]\npeak_basis = 5\n"
    with pytest.raises(ConfigError, match="peak_basis"):
        load_user_config(write(tmp_path, toml))


def test_peak_currency_defaults_to_native(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, VALID_TOML))
    assert cfg.schedule_fa.peak_currency == "native"


def test_peak_currency_inr_override_accepted(tmp_path: Path) -> None:
    toml = VALID_TOML + '\n[schedule_fa]\npeak_currency = "inr"\n'
    cfg = load_user_config(write(tmp_path, toml))
    assert cfg.schedule_fa.peak_currency == "inr"


def test_peak_currency_invalid_value_rejected(tmp_path: Path) -> None:
    toml = VALID_TOML + '\n[schedule_fa]\npeak_currency = "usd"\n'
    with pytest.raises(ConfigError, match="peak_currency"):
        load_user_config(write(tmp_path, toml))


# ---- control characters in filed text (#818) -------------------------------

# Every key in #818's matrix, in one config that loads. Keys whose format check
# already excludes control characters (country codes, ISINs, CUSIPs, Robinhood
# account numbers and symbols) aren't in the matrix.
_FILED_TEXT_TOML = (
    """
[account]
number = "U1234567"
holder_name = "Sample Holder"
opening_date = 2024-02-01
status = "BeneficialOwner"

[account.foreign_tins]
US = "123456789"
"""
    + CUSTODIAN_TOML
    + _FA_ENTITIES_TOML
    + VALID_NETBENEFITS
    + """
[stockplan.security]
symbol = "XQCB"
isin = "US99971P1012"
entity_name = "SAMPLE ISSUER CB Inc."
entity_address = "1 Sample Issuer Street, Sample City, XX"
entity_zip = "00001"
listing_exchange = "NASDAQ"
issuer_country_code = "US"
nature_of_entity = "Listed company shares"
"""
    + _ROBINHOOD_FIRST
    + _ROBINHOOD_SECOND
    + """
[schedule_fa]
manual_values_account_number = "U1234567"
"""
)
_FILED_TEXT_FIELDS = (
    "account.number",
    "schedule_fa.manual_values_account_number",
    "account.foreign_tins.US",
    "account.custodian.institution_name",
    "account.custodian.address",
    "account.custodian.zip",
    "fa.entities[0].name",
    "fa.entities[0].address",
    "fa.entities[0].zip",
    "fa.entities[0].nature_of_entity",
    "netbenefits.participant_no",
    "netbenefits.account.institution_name",
    "netbenefits.account.address",
    "netbenefits.account.zip",
    "netbenefits.account.withholding_agent_name",
    "netbenefits.security.symbol",
    "netbenefits.security.entity_address",
    "netbenefits.security.entity_zip",
    "netbenefits.security.nature_of_entity",
    "stockplan.security.symbol",
    "stockplan.security.entity_name",
    "stockplan.security.entity_address",
    "stockplan.security.entity_zip",
    "stockplan.security.nature_of_entity",
    "robinhood.accounts[0].institution_name",
    "robinhood.accounts[0].address",
    "robinhood.accounts[0].zip",
)
_CONTROL_CHARACTER_VALUES = (
    *((f"AAAA{char}ZZZZ", char, 5) for char in ("\n", "\t", "\x00", "\x7f")),
    ("\n", "\n", 1),
    ("AAAA\t", "\t", 5),
)


def _control_character_message(position: int) -> str:
    return (
        f"has a control character at position {position}; "
        "filed text may not contain control characters"
    )


def _toml_basic_string(value: str) -> str:
    """``value`` as a TOML basic string, with each control character escaped."""
    escaped = (f"\\u{ord(ch):04X}" if ch < " " or ch == "\x7f" else ch for ch in value)
    return f'"{"".join(escaped)}"'


# The refusal names a [account.foreign_tins] entry by its position, never by
# its key (#1045).
_REPORTED_FIELD = {"account.foreign_tins.US": "account.foreign_tins[0]"}


def _with_filed_value(field_path: str, value: str) -> str:
    """``_FILED_TEXT_TOML`` with the key at ``field_path`` set to ``value``.
    ``[0]`` names the first entry of an array of tables."""
    table, key = field_path.rsplit(".", 1)
    header = f"[[{table.removesuffix('[0]')}]]" if table.endswith("[0]") else f"[{table}]"
    text = _FILED_TEXT_TOML
    section = text.index(f"\n{header}\n")
    start = text.index(f"\n{key} = ", section) + 1
    assert "\n[" not in text[section + 1 : start], f"{key} is not under {header}"
    end = text.index("\n", start)
    return f"{text[:start]}{key} = {_toml_basic_string(value)}{text[end:]}"


def test_filed_text_fixture_loads_every_matrix_key(tmp_path: Path) -> None:
    cfg = load_user_config(write(tmp_path, _FILED_TEXT_TOML))
    assert cfg.account.number == "U1234567"
    assert cfg.account.foreign_tins == {"US": "123456789"}
    assert cfg.account.custodian is not None
    assert cfg.account.custodian.zip_code == "06830"
    assert cfg.fa.entities[0].name == "Sample Issuer AA Inc."
    assert cfg.netbenefits is not None
    assert cfg.netbenefits.security.entity_zip == "00000"
    assert cfg.stockplan is not None
    assert cfg.stockplan.security.entity_zip == "00001"
    assert cfg.robinhood is not None
    assert cfg.robinhood.accounts[0].zip_code == "32746"
    assert cfg.schedule_fa.manual_values_account_number == "U1234567"


def test_custodian_institution_name_with_a_line_break_is_refused(tmp_path: Path) -> None:
    toml = VALID_TOML + CUSTODIAN_TOML.replace(
        '"Interactive Brokers LLC"', '"Interactive Brokers\\nLLC"'
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, toml))
    assert exc.value.field == "account.custodian.institution_name"
    assert exc.value.message == _control_character_message(20)
    assert "Interactive" not in str(exc.value)
    assert "LLC" not in str(exc.value)


@pytest.mark.parametrize("field_path", _FILED_TEXT_FIELDS)
@pytest.mark.parametrize(("value", "char", "position"), _CONTROL_CHARACTER_VALUES)
def test_filed_text_refuses_a_control_character_without_echoing_the_value(
    tmp_path: Path, field_path: str, value: str, char: str, position: int
) -> None:
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, _with_filed_value(field_path, value)))
    assert exc.value.field == _REPORTED_FIELD.get(field_path, field_path)
    assert exc.value.message == _control_character_message(position)
    assert "AAAA" not in str(exc.value)
    assert "ZZZZ" not in str(exc.value)
    assert repr(char)[1:-1] not in str(exc.value)


@pytest.mark.parametrize(
    ("table", "field_path"),
    [
        (
            '\n[[fa.entities]]\nisin = "US99992A1097\\n"\naddress = "Sampletown, CA"\n',
            "fa.entities[0].isin",
        ),
        (
            '\n[[prices.ticker_overrides]]\nibkr_ticker = "XQAQ"\n'
            'isin = "US99992A1097\\n"\nyahoo_symbol = "XQAQ"\n',
            "prices.ticker_overrides[0].isin",
        ),
    ],
)
def test_isin_with_a_trailing_line_break_is_refused(
    tmp_path: Path, table: str, field_path: str
) -> None:
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, VALID_TOML + table))
    assert exc.value.field == field_path
    assert exc.value.message == (
        "must be a valid ISO 6166 ISIN (2-letter country + 9 alphanumerics + check digit)"
    )


@pytest.mark.parametrize(
    ("toml", "isin", "typo", "field_path"),
    [
        (
            VALID_TOML + '\n[[prices.ticker_overrides]]\nibkr_ticker = "XQAL"\n'
            'isin = "CA99991A1003"\nyahoo_symbol = "XQAM"\n',
            "CA99991A1003",
            "CA99991A1004",
            "prices.ticker_overrides[0].isin",
        ),
        (
            VALID_TOML + '\n[[fa.entities]]\nisin = "US99972B1017"\naddress = "Sampletown, CA"\n',
            "US99972B1017",
            "US99972B1016",
            "fa.entities[0].isin",
        ),
        (STOCKPLAN_TOML, "US99971P1012", "US99971P1011", "stockplan.security.isin"),
    ],
    ids=["ticker_override", "fa_entity", "stockplan"],
)
def test_isin_with_a_wrong_check_digit_is_refused(
    tmp_path: Path, toml: str, isin: str, typo: str, field_path: str
) -> None:
    # The valid ISIN loads; the same ISIN with a wrong last digit is refused.
    load_user_config(write(tmp_path, toml))
    typo_toml = toml.replace(isin, typo)
    assert typo_toml != toml
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, typo_toml))
    assert exc.value.field == field_path
    assert exc.value.message == "invalid check digit"


def test_netbenefits_price_override_audit_text_may_span_lines(tmp_path: Path) -> None:
    toml = VALID_TOML + VALID_NETBENEFITS.replace(
        'source = "SPS year-end statement"',
        'source = """SPS year-end\nstatement"""',
    ).replace(
        'reason = "12-31 lot unpriced in CSV"',
        'reason = """12-31 lot\nunpriced in CSV"""',
    )
    cfg = load_user_config(write(tmp_path, toml))
    assert cfg.netbenefits is not None
    override = cfg.netbenefits.price_overrides[0]
    assert override.source == "SPS year-end\nstatement"
    assert override.reason == "12-31 lot\nunpriced in CSV"


# ---- Every missing required field, in a fixed order (#1442) ------------------

# A Robinhood security missing two of its required fields.
_SECURITY_MISSING_TWO = robinhood_toml(
    extra=robinhood_security_toml()
    .replace("is_depositary_receipt = false\n", "")
    .replace('source = "synthetic issuer evidence"\n', "")
)


def test_several_missing_required_fields_are_named_sorted(tmp_path: Path) -> None:
    """The error names the first missing field by sorted name and lists every
    missing one, sorted, so they can all be fixed in one pass."""
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, _SECURITY_MISSING_TWO))
    assert (exc.value.field, exc.value.message) == (
        "robinhood.securities[0].is_depositary_receipt",
        "missing required fields: is_depositary_receipt, source",
    )


_LOAD_AND_PRINT_REFUSAL = """\
import sys
from pathlib import Path

from ibkr_fa.config import load_user_config
from ibkr_fa.exceptions import ConfigError

try:
    load_user_config(Path(sys.argv[1]))
except ConfigError as error:
    print(error)
else:
    sys.exit("no ConfigError")
"""


def test_several_missing_required_fields_error_ignores_hash_seed(tmp_path: Path) -> None:
    """Most tables' required fields are a frozenset of names, which iterates in
    ``PYTHONHASHSEED`` order. The refusal must not (as #784)."""
    config_path = write(tmp_path, _SECURITY_MISSING_TWO)
    runs = [
        subprocess.run(
            [sys.executable, "-c", _LOAD_AND_PRINT_REFUSAL, str(config_path)],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            text=True,
            check=False,
        )
        for seed in range(8)
    ]
    assert [run.returncode for run in runs] == [0] * 8, [run.stderr for run in runs]
    assert {run.stdout for run in runs} == {
        "ConfigError at field 'robinhood.securities[0].is_depositary_receipt': "
        "missing required fields: is_depositary_receipt, source\n"
    }


def _toml_inline(value: object) -> str:
    """A parsed TOML value written back as inline TOML."""
    if isinstance(value, dict):
        items = ", ".join(f"{key} = {_toml_inline(item)}" for key, item in value.items())
        return f"{{{items}}}"
    if isinstance(value, list):
        return f"[{', '.join(_toml_inline(item) for item in value)}]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    return str(value)  # an integer or a date


def _edited(text: str, path: str, *, remove: tuple[str, ...] = (), unknown: bool = False) -> str:
    """``text`` with the ``remove`` keys taken out of the table at ``path`` (a
    loader error path such as ``robinhood.accounts[0]``) and, if ``unknown``,
    an unknown key added to it, written back as inline TOML."""
    raw = tomllib.loads(text)
    table: Any = raw
    for part in filter(None, re.split(r"[.\[\]]+", path)):
        table = table[int(part) if part.isdigit() else part]
    for name in remove:
        del table[name]
    if unknown:
        table["surprise"] = True
    return "".join(f"{key} = {_toml_inline(value)}\n" for key, value in raw.items())


_FIDELITY_CMA_TOML = (
    _FIDELITY_CMA_BASE
    + """
[[fidelity_cma.us_tax_years]]
tax_year = 2025
refund_status = "finalized"
provenance = "synthetic transcript"
eligible_tax = [{ payment_date = 2025-05-01, amount_usd = "0.12" }]
"""
)

_NETBENEFITS_FULL = VALID_TOML + VALID_NETBENEFITS


# Every table with two or more required fields: a config that loads, the
# table's error path, and the required fields to remove, in sorted order.
_EVERY_TABLE_WITH_REQUIRED_FIELDS = [
    (
        VALID_TOML + CUSTODIAN_TOML,
        "account.custodian",
        ("address", "country_iso2", "institution_name"),
    ),
    (
        VALID_TOML
        + "\n[[form67.tax_payable_overrides]]\n"
        + 'country = "US"\nsource = "DIVIDEND"\ntax_payable_normal_inr = "100"\n',
        "form67.tax_payable_overrides[0]",
        ("country", "tax_payable_normal_inr"),
    ),
    (
        VALID_TOML
        + "\n[form67.section_91]\n"
        + 'total_indian_tax_before_91_relief_inr = "500000"\ntotal_income_inr = "1500000"\n',
        "form67.section_91",
        ("total_income_inr", "total_indian_tax_before_91_relief_inr"),
    ),
    (
        VALID_TOML
        + "\n[[prices.ticker_overrides]]\n"
        + 'ibkr_ticker = "XQAL"\nisin = "CA99991A1003"\nyahoo_symbol = "XQAM"\n',
        "prices.ticker_overrides[0]",
        ("isin", "yahoo_symbol"),
    ),
    (
        VALID_TOML + _SCHEDULE_FA_TOML,
        "schedule_fa.manual_values[0]",
        ("a2_peak_balance_inr", "calendar_year"),
    ),
    (
        ROBINHOOD_TOML,
        "robinhood.accounts[0]",
        ("address", "institution_name", "same_beneficial_owner"),
    ),
    (
        robinhood_toml(extra=robinhood_security_toml()),
        "robinhood.securities[0]",
        ("is_depositary_receipt", "source"),
    ),
    (
        robinhood_toml(extra=robinhood_option_classification_toml()),
        "robinhood.option_classifications[0]",
        ("fema_risk_acknowledged", "reason"),
    ),
    (
        robinhood_toml(extra=robinhood_stock_lending_fee_fsi_source_toml()),
        "robinhood.stock_lending_fee_fsi_sources[0]",
        ("country_iso2", "financial_year"),
    ),
    (
        robinhood_toml(extra=robinhood_interest_payer_country_toml()),
        "robinhood.interest_payer_countries[0]",
        ("interest_kinds", "source"),
    ),
    (
        robinhood_toml(
            extra=robinhood_security_toml() + robinhood_security_receipt_resolution_toml()
        ),
        "robinhood.security_receipt_resolutions[0]",
        ("cusip", "treatment"),
    ),
    (
        robinhood_toml(extra=robinhood_report_toml(file="activity/individual.csv")),
        "robinhood.reports[0]",
        ("end_date", "start_date"),
    ),
    (
        robinhood_toml(extra=robinhood_corrected_statement_toml()),
        "robinhood.corrected_statements[0]",
        ("source", "supersedes"),
    ),
    (_NETBENEFITS_FULL, "netbenefits.account", ("address", "institution_name")),
    (_NETBENEFITS_FULL, "netbenefits.security", ("cusip", "nature_of_entity")),
    (
        _FIDELITY_CMA_TOML,
        "fidelity_cma.us_tax_years[0].eligible_tax[0]",
        ("amount_usd", "payment_date"),
    ),
    (
        _NETBENEFITS_FULL,
        "netbenefits.price_overrides[0]",
        ("price_per_share_usd", "reason"),
    ),
    (_NETBENEFITS_FULL, "netbenefits.cg_fmv_values[0]", ("purchase_date", "source")),
    (
        VALID_TOML + _nb_with_binding() + _A2_PEAK,
        "netbenefits.a2_peak_overrides[0]",
        ("calendar_year", "peak_balance_inr"),
    ),
    (STOCKPLAN_TOML, "stockplan.security", ("isin", "symbol")),
    (_FIDELITY_CMA_TOML, "fidelity_cma.us_tax_years[0]", ("refund_status", "tax_year")),
    (_FIDELITY_CMA_TOML, "fidelity_cma", ("account_id", "spaxx_isin")),
    (_NETBENEFITS_FULL, "netbenefits", ("espp_discount_pct", "security")),
]


@pytest.mark.parametrize(
    ("text", "path", "names"),
    _EVERY_TABLE_WITH_REQUIRED_FIELDS,
    ids=[path for _text, path, _names in _EVERY_TABLE_WITH_REQUIRED_FIELDS],
)
def test_every_table_names_every_missing_required_field(
    tmp_path: Path, text: str, path: str, names: tuple[str, ...]
) -> None:
    """With ``names`` removed, the error names the first and lists them all;
    with one removed, it is the one-field error; an unknown key is refused
    first, naming the table."""
    assert load_user_config(write(tmp_path, _edited(text, path))) == load_user_config(
        write(tmp_path, text)
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, _edited(text, path, remove=names)))
    assert (exc.value.field, exc.value.message) == (
        f"{path}.{names[0]}",
        f"missing required fields: {', '.join(names)}",
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, _edited(text, path, remove=names[-1:])))
    assert (exc.value.field, exc.value.message) == (
        f"{path}.{names[-1]}",
        "missing required field",
    )
    with pytest.raises(ConfigError) as exc:
        load_user_config(write(tmp_path, _edited(text, path, remove=names, unknown=True)))
    assert exc.value.field == path
    assert exc.value.message.startswith("has an unknown key; allowed keys: ")
