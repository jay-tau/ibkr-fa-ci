"""Tests for the ``config_template`` module (``ibkr-fa init`` scaffold)."""

from __future__ import annotations

import re
import tomllib
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.compute.robinhood_identity import (
    RobinhoodEntityRoute,
    RobinhoodFoundEntityAddress,
    RobinhoodUnverifiedEntityAddress,
    robinhood_unverified_entity_addresses,
)
from ibkr_fa.config import _KNOWN_FA_ENTITY, _KNOWN_TOP_LEVEL, FaEntityOverride, load_user_config
from ibkr_fa.config_template import (
    _EXAMPLE_USER_TOML,
    USER_TOML_TEMPLATE,
    _comment_config_lines,
    write_user_toml_template,
)
from ibkr_fa.exceptions import ConfigError, PathRefusalError
from ibkr_fa.types import Isin
from tests._robinhood_security import robinhood_security

# Every nested table the canonical example must keep exercising, so a
# future schema addition that drops a section from the template is caught.
_REQUIRED_NESTED_HEADERS = (
    "[account.foreign_tins]",
    "[account.custodian]",
    "[[form67.tax_payable_overrides]]",
    "[form67.section_91]",
    "[[prices.ticker_overrides]]",
    "[[fa.entities]]",
    "[[schedule_fa.manual_values]]",
)

# #448 owns the Robinhood init scaffold after its CLI contract is wired.
_TABLES_PENDING_TEMPLATE = frozenset({"robinhood"})


def _uncomment_config_lines(template: str) -> str:
    """Inverse of :func:`_comment_config_lines`: strip the ``# `` prefix
    from commented config lines, leaving prose / blank lines untouched.

    Mirrors what a user does by hand to activate the scaffolded file.
    """
    out: list[str] = []
    for line in template.splitlines():
        if line.startswith("# ") and (line[2:].lstrip().startswith("[") or " = " in line[2:]):
            out.append(line[2:])
        else:
            out.append(line)
    return "\n".join(out)


# ---- canonical example: the schema-validity / drift guard -----------------


def test_canonical_example_parses_and_exercises_every_section(tmp_path: Path) -> None:
    """The single source-of-truth example must fully load AND populate
    every supported section, so a typo or schema drift fails CI."""
    path = tmp_path / "example.toml"
    path.write_text(_EXAMPLE_USER_TOML, encoding="utf-8")

    cfg = load_user_config(path)
    # The example securities are compared with the template's own text.
    example = tomllib.loads(_EXAMPLE_USER_TOML)

    assert cfg.account.number == "U1234567"
    assert cfg.account.holder_name == "Your Legal Name"
    assert cfg.account.foreign_tins == {"US": "123456789"}
    assert cfg.account.custodian is not None
    assert cfg.account.custodian.country_iso2 == "US"
    assert cfg.fx.max_carry_forward_days == 14
    assert cfg.os.schedule_os_indian_adr_routing.value == "depositary"
    assert len(cfg.form67.tax_payable_overrides) == 1
    assert cfg.form67.section_91 is not None
    assert len(cfg.prices.ticker_overrides) == 1
    assert len(cfg.fa.entities) == 1
    assert cfg.fa.entities[0].nature_of_entity == "Company"
    assert cfg.fa.entities[0].prefer_configured_address is False
    assert cfg.fa.entities[0].prefer_configured_nature is False
    assert cfg.fa.entities[0].prefer_configured_name is False
    assert cfg.schedule_fa.manual_values_account_number == "U1234567"
    assert len(cfg.schedule_fa.manual_values) == 1
    entry_2025 = cfg.schedule_fa.manual_value_for(2025)
    assert entry_2025 is not None
    assert entry_2025.a2_peak_balance_inr == 8950
    assert cfg.netbenefits is not None
    assert cfg.netbenefits.espp_discount_pct == Decimal("10")
    assert cfg.netbenefits.participant_no == "OQ1234567"
    assert len(cfg.netbenefits.cg_fmv_values) == 1
    assert cfg.netbenefits.cg_fmv_values[0].section17_gross_fmv_total_inr == Decimal("41850.00")
    assert cfg.netbenefits.account.institution_name == "Fidelity Stock Plan Services LLC"
    assert cfg.netbenefits.account.withholding_agent_name == "National Financial Services LLC"
    assert cfg.netbenefits.participant_no == "OQ1234567"
    netbenefits_security = example["netbenefits"]["security"]
    assert cfg.netbenefits.security.instrument.symbol == netbenefits_security["symbol"]
    assert str(cfg.netbenefits.security.instrument.isin) == netbenefits_security["isin"]
    assert len(cfg.netbenefits.price_overrides) == 1
    assert cfg.stockplan is not None
    stockplan_security = example["stockplan"]["security"]
    assert cfg.stockplan.security.symbol == stockplan_security["symbol"]
    assert str(cfg.stockplan.security.isin) == stockplan_security["isin"]
    assert cfg.stockplan.security.fa_fields_present()
    instrument = cfg.stockplan.security.instrument
    assert instrument is not None
    assert instrument.listing_exchange == "NASDAQ"


def test_canonical_example_documents_every_top_level_table() -> None:
    """Drift guard: each known top-level config table appears as a header
    in the template, so a newly-added table must be documented here too."""
    assert _TABLES_PENDING_TEMPLATE <= _KNOWN_TOP_LEVEL
    for table in _KNOWN_TOP_LEVEL - _TABLES_PENDING_TEMPLATE:
        assert f"[{table}]" in _EXAMPLE_USER_TOML or f"[[{table}" in _EXAMPLE_USER_TOML, (
            f"top-level table [{table}] is missing from the init template"
        )


def test_canonical_example_documents_required_nested_tables() -> None:
    for header in _REQUIRED_NESTED_HEADERS:
        assert header in _EXAMPLE_USER_TOML, f"{header} missing from the init template"


def test_account_comments_match_loader(tmp_path: Path) -> None:
    """As the template's comments say, holder_name is the only required
    [account] field: the example loads with [account]'s own keys cut down to
    holder_name (its sub-tables stay). The [schedule_fa] manual values go too,
    since their comment says they need [account].number and opening_date."""
    kept: list[str] = []
    dropped: list[str] = []
    table = ""
    for line in _EXAMPLE_USER_TOML.splitlines():
        if line.startswith("["):
            table = line
        key = line.partition(" = ")[0]
        if (
            line
            and not line.startswith("#")
            and (
                (table == "[account]" and line != table and key != "holder_name")
                or (table == "[schedule_fa]" and key == "manual_values_account_number")
                or table == "[[schedule_fa.manual_values]]"
            )
        ):
            dropped.append(key)
        else:
            kept.append(line)
    assert dropped == [
        "number",
        "opening_date",
        "status",
        "us_person",
        "allow_masked_account",
        "accept_anonymised_statement",
        "manual_values_account_number",
        "[[schedule_fa.manual_values]]",
        "calendar_year",
        "a2_peak_balance_inr",
    ]
    path = tmp_path / "user.toml"
    path.write_text("\n".join(kept), encoding="utf-8")

    cfg = load_user_config(path)
    assert cfg.account.holder_name == "Your Legal Name"
    assert (cfg.account.number, cfg.account.opening_date, cfg.account.status) == (None, None, None)

    without_holder = [line for line in kept if not line.startswith("holder_name = ")]
    path.write_text("\n".join(without_holder), encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_user_config(path)
    assert excinfo.value.field == "account.holder_name"


# ---- written template: inert-by-design ------------------------------------


def test_written_template_is_inert(tmp_path: Path) -> None:
    """A freshly-scaffolded file must NOT load: every config line is
    commented, so the required [account] table is absent."""
    path = tmp_path / "user.toml"
    write_user_toml_template(path)

    with pytest.raises(ConfigError) as excinfo:
        load_user_config(path)
    assert excinfo.value.field == "account"


def test_written_template_uncommented_parses(tmp_path: Path) -> None:
    """Activating the scaffold (uncommenting its config lines) yields a
    valid config — proves the written template faithfully carries the
    canonical example, not just that the canonical source parses."""
    activated = _uncomment_config_lines(USER_TOML_TEMPLATE)
    path = tmp_path / "user.toml"
    path.write_text(activated, encoding="utf-8")

    cfg = load_user_config(path)
    assert cfg.account.number == "U1234567"
    assert len(cfg.fa.entities) == 1
    # The commented corrected-statement example's paths load (#1639).
    assert cfg.robinhood is not None
    assert cfg.robinhood.corrected_statements[0].supersedes == ("statements/2025-12.pdf",)


def test_fa_entities_comment_robinhood_holding_loads(tmp_path: Path) -> None:
    """#1458: the activated [[fa.entities]] sample, with address and every key
    the comment's Robinhood sentence names left out, loads, and a held
    security's run then stops for the filer to verify the address found
    (#1449). On be2d3f1 that sentence named no key, and the sample's zip
    refused without address."""
    activated = _uncomment_config_lines(USER_TOML_TEMPLATE)
    comment = " ".join(line.removeprefix("# ") for line in activated.splitlines())
    sentence = re.search(r"For a Robinhood holding [^:]*", comment)
    assert sentence is not None
    left_out = {"address"} | {
        key for key in _KNOWN_FA_ENTITY if re.search(rf"\b{key}\b", sentence[0])
    }
    kept: list[str] = []
    table = ""
    for line in activated.splitlines():
        if line.startswith("["):
            table = line
        if table != "[[fa.entities]]" or line.partition(" = ")[0] not in left_out:
            kept.append(line)
    path = tmp_path / "user.toml"
    path.write_text("\n".join(kept), encoding="utf-8")

    entities = load_user_config(path).fa.entities
    isin = Isin("US99999A1025")
    assert entities == (
        FaEntityOverride(
            isin=isin,
            name="Example Holdings Inc.",
            nature_of_entity="Company",
            prefer_configured_nature=True,
            prefer_configured_name=True,
        ),
    )
    found = RobinhoodFoundEntityAddress(
        RobinhoodEntityRoute.LEGAL, "1 Example Plaza, Anytown, DE", "99999"
    )
    assert robinhood_unverified_entity_addresses(
        entities,
        (robinhood_security(cusip="99999A102", isin=isin),),
        (FaEntityOverride(isin=isin, address=found.address, zip_code=found.zip_code),),
        {isin: RobinhoodEntityRoute.LEGAL},
        (),
    ) == (
        RobinhoodUnverifiedEntityAddress(
            entry_index=0, entry=entities[0], found=found, etf=False, nature_missing=False
        ),
    )


def test_written_template_starts_with_guidance_header() -> None:
    assert USER_TOML_TEMPLATE.startswith("# ibkr-fa configuration")
    assert "COMMENTED OUT" in USER_TOML_TEMPLATE


# ---- _comment_config_lines -------------------------------------------------


def test_comment_config_lines_comments_only_config() -> None:
    sample = '[account]\nnumber = "U1"\n# a prose note\n\n[fx]'
    result = _comment_config_lines(sample)
    assert result == '# [account]\n# number = "U1"\n# a prose note\n\n# [fx]'


def test_comment_config_lines_preserves_trailing_annotations() -> None:
    sample = 'number = "U1"  # REPLACE me'
    assert _comment_config_lines(sample) == '# number = "U1"  # REPLACE me'


# ---- write_user_toml_template ----------------------------------------------


def test_write_creates_file_with_template_content(tmp_path: Path) -> None:
    path = tmp_path / "user.toml"
    write_user_toml_template(path)
    assert path.read_text(encoding="utf-8") == USER_TOML_TEMPLATE


def test_write_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    path = tmp_path / "user.toml"
    path.write_text("PRECIOUS EDITS", encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_user_toml_template(path, force=False)
    # Original content untouched.
    assert path.read_text(encoding="utf-8") == "PRECIOUS EDITS"


def test_write_force_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "user.toml"
    path.write_text("stale", encoding="utf-8")

    write_user_toml_template(path, force=True)
    assert path.read_text(encoding="utf-8") == USER_TOML_TEMPLATE


def test_write_refuses_missing_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "user.toml"

    with pytest.raises(PathRefusalError) as raised:
        write_user_toml_template(path)
    assert raised.value == PathRefusalError(
        "parent directory does not exist (create it first, or choose an existing directory)",
        path.parent,
    )
    assert not path.exists()
