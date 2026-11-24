"""Every security identifier under ``tests/`` is synthetic, or a kept identity
at a location allowed for it (#1070).

``test_fixture_securities`` checks the broker fixtures in depth. This guard
reads every file under ``tests/``: the string literals and comments of each
Python file, and the whole text of every other file.

* **ISIN:** each ISIN-shaped body (two letters and nine characters, at least
  one of them a digit, in one letter case), upper-cased and whatever follows
  it, embeds a private-use issuer (990000-999999 or 99000A-99999Z), uses the
  user-assigned country code ``ZZ``, or is allowed at that location. A hex
  digest, an OCC option symbol and an SEC ``CIK##########`` file name aren't
  read as ISINs.
* **CUSIP:** each CUSIP-shaped token that contains a letter, and each token in
  a CUSIP field, has a private-use issuer or is allowed at that location.
  Outside a CUSIP field, a token with ``src``'s account-number shape (one or
  two letters, then digits: an IBKR account or a NetBenefits participant
  number) isn't read as a CUSIP (owner decision on #1070).
* **LEI:** each LEI-shaped token that contains a letter and isn't part of a
  number, and each token in an LEI field, is the ``999900SAMPLE`` form or is
  allowed at that location, whether or not its check digits are valid.
* **SEC ID:** each CIK in an SEC field, EDGAR URL or ``CIK##########`` file
  name is in 9990000-9999999; each series ID is ``S000999…`` and each class
  ID ``C000999…``.

A kept identity is one ``src`` keys on. A test may quote it only at a
location the allow-list names for it: a test function, class or module-level
constant in one file, never a whole Python file. Each entry gives the reason,
and reads the identity from its ``src`` constant where one exists (the
StockPlan issuer gate has none, so its own refusal names the ISIN). Text that
has an identifier's shape but names no security is listed by location too.
Tickers and issuer names can't be checked mechanically.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

import pytest

from ibkr_fa.compute.robinhood_entity_overrides import _REVIEWED_ADDRESS_ALIASES
from ibkr_fa.exceptions import ConfigError
from ibkr_fa.parser.fidelity_cma import _SPAXX_CUSIP
from ibkr_fa.parser.stockplan import require_verified_issuer
from ibkr_fa.pii import _DISPLAYABLE_ACCOUNT_ID_PATTERN
from ibkr_fa.pipeline_fidelity_cma_fa import _SPAXX_ENTITY
from ibkr_fa.reference.fidelity_core_funds import FYIXX_CUSIP, FYIXX_ISIN
from ibkr_fa.reference.indian_adr_mapping import INDIAN_ADR_MAPPING
from ibkr_fa.robinhood_adr_entity_sources import _DEPOSITARY_LEIS
from ibkr_fa.types import cusip_check_digit, isin_check_digit

_TESTS: Final = Path(__file__).parent
_THIS_FILE: Final = Path(__file__).name

# ---- what a security looks like --------------------------------------------

# The CUSIP issuer numbers reserved for private use: 990000-999999 and
# 99000A-99999Z.
_PRIVATE_USE_ISSUER: Final = re.compile(r"99[0-9]{3}[0-9A-Z]")
_SYNTHETIC_LEI_PREFIX: Final = "999900SAMPLE"
_SYNTHETIC_CIKS: Final = range(9990000, 10000000)
_SYNTHETIC_SEC_PREFIX: Final = "000999"

_ISIN_BODY: Final = re.compile(r"(?<![0-9A-Za-z])[A-Za-z]{2}[0-9A-Za-z]{9}")
_CUSIP_TOKEN: Final = re.compile(r"(?<![0-9A-Za-z])[0-9A-Z]{8}[0-9](?![0-9A-Za-z])")
_LEI_TOKEN: Final = re.compile(r"(?<![0-9A-Za-z])[0-9A-Z]{18}[0-9]{2}(?![0-9A-Za-z])")
_SERIES_OR_CLASS: Final = re.compile(r"(?<![0-9A-Za-z])[SC][0-9]{9}(?![0-9A-Za-z])")
_CIK_FILE: Final = re.compile(r"CIK([0-9]{10})")
_CIK_URL: Final = re.compile(r"(?i)(?:edgar/data/|[?&]cik=)([0-9]+)")

# Field values may wrap across Python comments, calls and grouping parentheses.
_FIELD_SPACE: Final = r"(?:\s|#[^\n]*\n)*"
_FIELD_VALUE: Final = (
    rf"(?:(?:[A-Za-z_][\w.]*)?\({_FIELD_SPACE})*"
    r"""(?:(?:br|rb|fr|rf|[brfu])?(?:"{3}|'{3}|["']))?"""
    rf"{_FIELD_SPACE}"
)
_FIELD: Final = rf"[\w.]*[\"'\]]*{_FIELD_SPACE}(?:==|!=|:=|=|:|,|\(){_FIELD_SPACE}{_FIELD_VALUE}"
# Compound field names are valid; only a bare Cusip call supplies field context.
_CUSIP_FIELD: Final = re.compile(
    rf"(?i)(?:cusip[\w.]*[\"'\]]*{_FIELD_SPACE}(?:==|!=|:=|=|:|,)"
    rf"|(?<![a-z])cusip{_FIELD_SPACE}\(){_FIELD_SPACE}{_FIELD_VALUE}"
    r"([0-9A-Za-z]{9})(?![0-9A-Za-z])"
)
_LEI_FIELD: Final = re.compile(
    rf"(?i)(?<![a-z])lei(?![a-z]){_FIELD}([0-9A-Za-z]{{20}})(?![0-9A-Za-z])"
)
# ``{cik:010d}`` is a format field, not a value.
_CIK_FIELD: Final = re.compile(rf"(?i)(?<![a-z{{])cik(?![a-z]){_FIELD}([0-9][0-9_]*)")
# SEC XML uses names such as registrantCik, registrantLei and mgmtInvLei.
_XML_SPACE: Final = r"(?:\s|<!--(?:[^-]|-(?!->))*-->)*"
_XML_FIELD: Final = (
    r"""(?:[^<>"']|"[^"]*"|'[^']*')*(?<!/)>"""
    rf"{_XML_SPACE}(?:<!\[CDATA\[{_XML_SPACE})?"
)
_CUSIP_XML: Final = re.compile(
    rf"(?i)<[\w:.-]*cusip(?=[\s>]){_XML_FIELD}([0-9A-Za-z]{{9}})(?![0-9A-Za-z])"
)
_LEI_XML: Final = re.compile(
    rf"(?i)<[\w:.-]*lei(?=[\s>]){_XML_FIELD}([0-9A-Za-z]{{20}})(?![0-9A-Za-z])"
)
_CIK_XML: Final = re.compile(rf"(?i)<[\w:.-]*cik(?=[\s>]){_XML_FIELD}([0-9][0-9_]*)")
# Text shaped like an identifier that isn't one: a hex digest, an OCC
# option symbol and an SEC submissions file name (checked as a CIK instead).
_NOT_IDENTIFIERS: Final = re.compile(
    r"[0-9a-fA-F]{32,}|[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}|CIK[0-9]{10}"
)
_PYTHON_TEXT_TOKENS: Final = frozenset({tokenize.STRING, tokenize.COMMENT, tokenize.FSTRING_MIDDLE})


def _stockplan_isin() -> str:
    """The ISIN the StockPlan issuer gate accepts, as its own refusal names it."""
    try:
        require_verified_issuer("", "")
    except ConfigError as exc:
        (isin,) = set(re.findall(r"\b[A-Z]{2}[0-9A-Z]{9}[0-9]\b", exc.message))
        return isin
    raise AssertionError("the StockPlan issuer gate accepted an empty issuer")


# ---- kept identities and where tests may quote them -------------------------


@dataclass(frozen=True, slots=True)
class _Kept:
    """Identifiers ``src`` keys on, read from their ``src`` constants."""

    identity: str
    identifiers: frozenset[str]


_STOCKPLAN_ISSUER = _Kept("the StockPlan issuer", frozenset({_stockplan_isin()}))
_CORE_FUND = _Kept("the core money-market fund", frozenset({FYIXX_ISIN, FYIXX_CUSIP}))
_SWEEP_FUND = _Kept("the Fidelity CMA sweep fund", frozenset({_SPAXX_ENTITY.isin, _SPAXX_CUSIP}))
_INDIAN_ADRS = _Kept("the Indian ADR table", frozenset(INDIAN_ADR_MAPPING))
_DEPOSITARIES = _Kept("the reviewed ADR depositaries", frozenset(_DEPOSITARY_LEIS.values()))
_ADDRESS_ALIAS = _Kept(
    "the reviewed address alias", frozenset(key[0] for key in _REVIEWED_ADDRESS_ALIASES)
)


@dataclass(frozen=True, slots=True)
class _Allowed:
    """A location a kept identity may appear at: ``scope`` is the qualified
    name of a test function, class or module-level constant in the Python file
    ``path`` (relative to ``tests/``)."""

    path: str
    scope: str
    kept: _Kept
    reason: str


def _allow(path: str, kept: _Kept, reason: str, *scopes: str) -> tuple[_Allowed, ...]:
    return tuple(_Allowed(path, scope, kept, reason) for scope in scopes)


_STOCKPLAN_GATE = "runs a gated StockPlan entry point, which accepts only the verified issuer"
_STOCKPLAN_GATE_TESTS = (
    "the gate's own tests and the discovery test, which runs the gated entry points"
)
_CORE_FUND_KEY = "reaches the core-fund identity the NetBenefits income normalizer keys on"
_SWEEP_FUND_KEY = (
    "reaches the sweep-fund identity the Fidelity CMA config, parser or pipeline keys on"
)
_ADR_TABLE = "reaches the shipped Indian ADR table"
_DEPOSITARY_TABLE = "reaches the reviewed ADR depositary table"
_ALIAS_TABLE = "reaches the reviewed address alias"

_ALLOWED: Final[tuple[_Allowed, ...]] = (
    *_allow(
        "test_cli_stockplan_all.py",
        _STOCKPLAN_ISSUER,
        _STOCKPLAN_GATE,
        "test_unverified_issuer_stops_the_run_before_any_leg",
    ),
    *_allow(
        "_config_toml.py",
        _STOCKPLAN_ISSUER,
        _STOCKPLAN_GATE_TESTS,
        "UNVERIFIED_STOCKPLAN_ISSUER",
    ),
    *_allow(
        "test_config.py",
        _STOCKPLAN_ISSUER,
        _STOCKPLAN_GATE_TESTS,
        "test_stockplan_entrypoints_refuse_unverified_issuer",
        "test_stockplan_entrypoints_do_not_require_account_opening_date",
    ),
    *_allow(
        "_stockplan_fa_exports.py",
        _STOCKPLAN_ISSUER,
        _STOCKPLAN_GATE,
        "stockplan_fa_config",
    ),
    *_allow(
        "test_pipeline_stockplan_fa.py",
        _STOCKPLAN_ISSUER,
        _STOCKPLAN_GATE,
        "test_ticker_override_changes_only_the_fetched_symbol",
    ),
    *_allow(
        "_stockplan_income.py",
        _STOCKPLAN_ISSUER,
        _STOCKPLAN_GATE,
        "STOCKPLAN_INCOME_CONFIG",
    ),
    *_allow(
        "test_pipeline_stockplan_income.py",
        _STOCKPLAN_ISSUER,
        _STOCKPLAN_GATE,
        "test_cli_unverified_issuer_refuses",
        "test_no_ftc_warning_when_no_withholding",
    ),
    *_allow(
        "test_normalize_netbenefits_income.py",
        _CORE_FUND,
        _CORE_FUND_KEY,
        "test_normalize_income_builds_the_record",
        "test_same_date_withholdings_take_their_own_dividends_identity",
        "test_normalize_income_refuses_a_third_security_dividend",
        "test_normalize_income_refuses_the_core_fund_as_the_security",
    ),
    *_allow("_netbenefits_income.py", _CORE_FUND, _CORE_FUND_KEY, "FUND_DIV"),
    *_allow(
        "test_pipeline_netbenefits_income.py",
        _CORE_FUND,
        _CORE_FUND_KEY,
        "test_configuring_the_core_fund_as_the_security_refuses",
        "test_core_fund_dividend_is_income_labelled_as_the_fund",
        "test_other_cusip_dividend_refuses",
        "test_same_date_dividends_from_both_securities_pair_correctly",
    ),
    *_allow("test_cli.py", _SWEEP_FUND, _SWEEP_FUND_KEY, "_CMA_CONFIG"),
    *_allow("_config_toml.py", _SWEEP_FUND, _SWEEP_FUND_KEY, "_FIDELITY_CMA_BASE"),
    *_allow(
        "test_config_fidelity_cma.py",
        _SWEEP_FUND,
        _SWEEP_FUND_KEY,
        "test_cma_status_is_source_specific_and_validated",
    ),
    *_allow("_config_toml.py", _SWEEP_FUND, _SWEEP_FUND_KEY, "_fidelity_cma"),
    *_allow("_fidelity_cma.py", _SWEEP_FUND, _SWEEP_FUND_KEY, "monthly_statement_text"),
    *_allow(
        "_fidelity_cma_bundle.py", _SWEEP_FUND, _SWEEP_FUND_KEY, "BUNDLE_CONFIG", "_statement_text"
    ),
    *_allow("test_pipeline_fidelity_cma_cg.py", _SWEEP_FUND, _SWEEP_FUND_KEY, "_CONFIG"),
    *_allow("test_pipeline_fidelity_cma_fa.py", _SWEEP_FUND, _SWEEP_FUND_KEY, "_SPAXX_A3_ENTITY"),
    *_allow("_fidelity_cma_fa_run.py", _SWEEP_FUND, _SWEEP_FUND_KEY, "CMA_FA_CONFIG"),
    *_allow("_fidelity_cma_income_run.py", _SWEEP_FUND, _SWEEP_FUND_KEY, "CMA_INCOME_CONFIG"),
    *_allow("test_compute_foreign_tax.py", _INDIAN_ADRS, _ADR_TABLE, "_INFY_ADR"),
    *_allow("test_compute_form67.py", _INDIAN_ADRS, _ADR_TABLE, "_INFY_ADR_ISIN", "_infy_adr_wht"),
    *_allow(
        "test_compute_schedule_fsi.py",
        _INDIAN_ADRS,
        _ADR_TABLE,
        "_INFY_ADR_ISIN_FSI",
        "test_wht_indian_adr_default_depositary_mode_preserves_legacy",
        "test_wht_indian_adr_only_under_underlying_issuer_yields_no_contribution",
        "test_wht_indian_adr_skipped_under_underlying_issuer_mode",
    ),
    *_allow(
        "_ibkr_income_inputs.py",
        _INDIAN_ADRS,
        _ADR_TABLE,
        "DIVIDEND_ADR",
        "FII_DATA_ADR",
        "WHT_ADR",
    ),
    *_allow(
        "test_reference_indian_adr_mapping.py",
        _INDIAN_ADRS,
        _ADR_TABLE,
        "test_lookup_indian_adr_returns_entry_for_mapped_isin",
    ),
    *_allow(
        "test_schedule_os_dividends.py",
        _INDIAN_ADRS,
        _ADR_TABLE,
        "test_line_carries_dividend_kind",
        "test_indian_company_us_adr_routes_to_foreign_lines_not_domestic_lines",
        "test_183_depositary_mode_default_preserves_v1_adr_routing",
        "test_183_underlying_issuer_mode_attributes_mapped_adr_to_india",
    ),
    *_allow("_robinhood_adr.py", _DEPOSITARIES, _DEPOSITARY_TABLE, "ADR_DEPOSITARY_LEI"),
    *_allow(
        "test_compute_robinhood_entity_overrides.py", _ADDRESS_ALIAS, _ALIAS_TABLE, "_ALIAS_ISIN"
    ),
    *_allow(
        "test_robinhood_legal_entity_sources.py",
        _ADDRESS_ALIAS,
        _ALIAS_TABLE,
        "test_ch_share_uses_one_reviewed_ascii_alias_and_retains_unicode_source",
    ),
)


@dataclass(frozen=True, slots=True)
class _NotASecurity:
    """Text at one location that has an identifier's shape but names no security."""

    path: str
    scope: str
    text: str
    reason: str


_MALFORMED_ISIN = "a malformed synthetic ISIN whose head reads as a CUSIP"
_NOT_SECURITIES: Final[tuple[_NotASecurity, ...]] = (
    *(
        _NotASecurity(
            path, "test_isin_must_match_the_whole_iso_6166_shape", "US99992A1", _MALFORMED_ISIN
        )
        for path in (
            "test_model_dividend.py",
            "test_model_withholding_tax.py",
            "test_models_substitute_dividend_income.py",
        )
    ),
    _NotASecurity(
        "test_reference_indian_adr_mapping.py",
        "test_isin_check_digit_valid_rejects_invalid_isins",
        "US99992A1",
        _MALFORMED_ISIN,
    ),
    _NotASecurity(
        "test_parser_isin.py",
        "test_malformed_shape_raises_parse_error_with_context",
        "9992A1097",
        _MALFORMED_ISIN,
    ),
    _NotASecurity(
        "test_models_form67_entry.py",
        "test_source_of_income_enum_values_match_form_67_pdf_verbatim",
        "ITR62FORM67",
        "the file name of the CBDT's Form 67 PDF",
    ),
    _NotASecurity(
        "test_pipeline_netbenefits_fa.py",
        "test_quicko_template_refuses_malformed_content_after_nonblank_row",
        "PT1000000000",
        "an XLSX duration cell",
    ),
    _NotASecurity(
        "test_refresh_ttbr.py",
        "test_render_source_file_includes_commit_and_timestamp",
        "ABC123DEF456",
        "a placeholder commit hash",
    ),
    _NotASecurity(
        "_cg_lines.py", "MARKED_DESCRIPTION", "SYNTHDESC2A", "#1166's synthetic description marker"
    ),
    _NotASecurity(
        "_foreign_tax_exact_sums.py",
        "MARKED_DESCRIPTION",
        "SYNTHDESC2B",
        "#1167's synthetic description marker",
    ),
    _NotASecurity(
        "_fa_exact_sums.py", "DESCRIPTION", "SYNTHDESC2C", "#1168's synthetic description marker"
    ),
    _NotASecurity(
        "_section_91_inputs.py",
        "_SYMBOL",
        "QZ03SEC91",
        "#1177's synthetic symbol marker",
    ),
    _NotASecurity(
        "_robinhood_a3_exact.py",
        "DESCRIPTION",
        "SYNTHA3DESC",
        "#1243's synthetic description marker",
    ),
)


# ---- reading the files ------------------------------------------------------


def _text_spans(path: str, text: str) -> Iterator[tuple[int, str]]:
    """``(first line, text)`` for each string literal and comment of a Python
    file, or the whole text of any other file."""
    if not path.endswith(".py"):
        yield 1, text
        return
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type in _PYTHON_TEXT_TOKENS:
            yield token.start[0], token.string


def _scopes(path: str, text: str) -> list[tuple[int, int, str]]:
    """``(first line, last line, qualified name)`` of each function, class and
    module-level constant of a Python file, decorators included."""
    if not path.endswith(".py"):
        return []
    scopes: list[tuple[int, int, str]] = []

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = prefix + node.name
                first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                scopes.append((first, node.end_lineno or node.lineno, name))
                visit(node.body, name + ".")
            elif not prefix and isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        scopes.append((node.lineno, node.end_lineno or node.lineno, target.id))

    visit(ast.parse(text).body, "")
    return scopes


def _scope_at(scopes: list[tuple[int, int, str]], line: int) -> str | None:
    """The innermost scope holding ``line``, if any."""
    holding = [scope for scope in scopes if scope[0] <= line <= scope[1]]
    return max(holding, key=lambda scope: scope[0])[2] if holding else None


def _decimal_number(token: str) -> bool:
    try:
        Decimal(token)
    except InvalidOperation:
        return False
    return True


def _account_number(token: str) -> bool:
    """True for ``src``'s account-number shape with a letter prefix (an IBKR
    account or a NetBenefits participant number), which a CUSIP can share."""
    match = _DISPLAYABLE_ACCOUNT_ID_PATTERN.fullmatch(token)
    return match is not None and match.group("prefix") != ""


@dataclass(frozen=True, slots=True)
class _Found:
    kind: str
    line: int
    text: str
    ok: bool


def _isin_shaped(body: str) -> bool:
    letters = [c for c in body if c.isalpha()]
    one_case = all(c.isupper() for c in letters) or all(c.islower() for c in letters)
    return one_case and any(c.isdigit() for c in body[2:])


def _private_use(issuer: str) -> bool:
    return _PRIVATE_USE_ISSUER.fullmatch(issuer.upper()) is not None


def _shaped(span: str) -> Iterator[tuple[int, str, str, bool]]:
    """``(offset, kind, token, synthetic)`` for each identifier-shaped token
    in string or comment text."""
    span = _NOT_IDENTIFIERS.sub(lambda m: "#" * len(m.group()), span)
    for match in _ISIN_BODY.finditer(span):
        body = match.group()
        if _isin_shaped(body):
            following = span[match.end() : match.end() + 1]
            token = body + following if following.isalnum() else body
            synthetic = body[:2].upper() == "ZZ" or _private_use(body[2:8])
            yield match.start(), "ISIN", token, synthetic
    for match in _CUSIP_TOKEN.finditer(span):
        token = match.group()
        if not (token.isdigit() or _decimal_number(token) or _account_number(token)):
            yield match.start(), "CUSIP", token, _private_use(token[:6])
    for match in _LEI_TOKEN.finditer(span):
        token = match.group()
        if not (token.isdigit() or _decimal_number(token)):
            yield match.start(), "LEI", token, token.startswith(_SYNTHETIC_LEI_PREFIX)
    for match in _SERIES_OR_CLASS.finditer(span):
        token = match.group()
        yield match.start(), "SEC ID", token, token[1:7] == _SYNTHETIC_SEC_PREFIX


def _in_fields(text: str) -> Iterator[tuple[int, str, str, bool]]:
    """``(offset, kind, token, synthetic)`` for each CUSIP, LEI or CIK in a
    field, EDGAR URL or file name, including fields split across lines."""
    for pattern in (_CUSIP_FIELD, _CUSIP_XML):
        for match in pattern.finditer(text):
            if not match.group(1).isalpha():
                yield match.start(1), "CUSIP", match.group(1), _private_use(match.group(1)[:6])
    for pattern in (_LEI_FIELD, _LEI_XML):
        for match in pattern.finditer(text):
            yield (
                match.start(1),
                "LEI",
                match.group(1),
                match.group(1).startswith(_SYNTHETIC_LEI_PREFIX),
            )
    for pattern in (_CIK_FIELD, _CIK_URL, _CIK_FILE, _CIK_XML):
        for match in pattern.finditer(text):
            value = match.group(1)
            yield match.start(1), "CIK", value, int(value.replace("_", "")) in _SYNTHETIC_CIKS


def _in_annotated_fields(text: str) -> Iterator[_Found]:
    """Use Python's parser to retain field context across type annotations."""
    bindings: list[tuple[str, ast.expr]] = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            bindings.append((ast.unparse(node.target), node.value))
        elif isinstance(node, ast.arguments):
            positional = [*node.posonlyargs, *node.args]
            # Defaults belong to the last positional parameters.
            for argument, value in (
                *zip(reversed(positional), reversed(node.defaults), strict=False),
                *zip(node.kwonlyargs, node.kw_defaults, strict=True),
            ):
                if argument.annotation is not None and value is not None:
                    bindings.append((argument.arg, value))
    for name, value_node in bindings:
        value = ast.get_source_segment(text, value_node)
        assert value is not None
        prefix = name + " = "
        for offset, kind, token, ok in _in_fields(prefix + value):
            if offset >= len(prefix):
                yield _Found(
                    kind,
                    value_node.lineno + value.count("\n", 0, offset - len(prefix)),
                    token,
                    ok,
                )


def _found(path: str, text: str) -> list[_Found]:
    """Every identifier in one file, once per line; ``ok`` when it is synthetic."""
    found = [
        _Found(kind, first + span.count("\n", 0, offset), token, ok)
        for first, span in _text_spans(path, text)
        for offset, kind, token, ok in _shaped(span)
    ]
    found += [
        _Found(kind, 1 + text.count("\n", 0, offset), token, ok)
        for offset, kind, token, ok in _in_fields(text)
    ]
    if path.endswith(".py"):
        found.extend(_in_annotated_fields(text))
    return list(dict.fromkeys(found))


def _problems(path: str, text: str, used: set[_Allowed | _NotASecurity] | None = None) -> list[str]:
    """Each identifier in ``text`` (the file at ``tests/<path>``) that is
    neither synthetic, nor a kept identity allowed at its location, nor text
    listed as not a security there. Adds each entry that matched to ``used``."""
    scopes = _scopes(path, text)
    problems: list[str] = []
    for found in _found(path, text):
        if found.ok:
            continue
        scope = _scope_at(scopes, found.line)
        entries: list[_Allowed | _NotASecurity] = [
            allowed
            for allowed in _ALLOWED
            if found.text in allowed.kept.identifiers
            and (path, scope) == (allowed.path, allowed.scope)
        ]
        entries += [
            entry
            for entry in _NOT_SECURITIES
            if found.text.upper() == entry.text
            and (
                (path, scope) == (entry.path, entry.scope)
                or (path == _THIS_FILE and scope == "_NOT_SECURITIES")
            )
        ]
        if used is not None:
            used.update(entries)
        if not entries:
            problems.append(
                f"{path}:{found.line}: {found.kind} {found.text!r} in {scope or 'module'}"
            )
    return problems


def _test_files() -> list[Path]:
    return sorted(
        path
        for path in _TESTS.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )


# ---- the tests ----------------------------------------------------------------


def test_tests_hold_only_synthetic_or_allowed_securities() -> None:
    used: set[_Allowed | _NotASecurity] = set()
    problems = [
        problem
        for path in _test_files()
        for problem in _problems(
            path.relative_to(_TESTS).as_posix(), path.read_text(encoding="utf-8"), used
        )
    ]
    assert problems == []
    # A stale entry would allow a location that no longer needs it.
    assert [entry for entry in (*_ALLOWED, *_NOT_SECURITIES) if entry not in used] == []


def test_allow_list_names_locations_never_a_whole_python_file() -> None:
    for entry in (*_ALLOWED, *_NOT_SECURITIES):
        assert entry.path.endswith(".py"), entry.path
        assert (_TESTS / entry.path).is_file(), entry.path
        assert entry.scope in {
            name
            for *_, name in _scopes(entry.path, (_TESTS / entry.path).read_text(encoding="utf-8"))
        }
        assert entry.reason


def test_kept_identities_are_read_from_src() -> None:
    assert _STOCKPLAN_ISSUER.identifiers == {_stockplan_isin()}
    for kept in (
        _STOCKPLAN_ISSUER,
        _CORE_FUND,
        _SWEEP_FUND,
        _INDIAN_ADRS,
        _DEPOSITARIES,
        _ADDRESS_ALIAS,
    ):
        assert kept.identifiers, kept.identity
        assert all(_PRIVATE_USE_ISSUER.fullmatch(i[2:8]) is None for i in kept.identifiers)


# ---- the checks themselves ----------------------------------------------------

# Every identifier below is built at run time, so this file quotes none.
_PATH = "test_example.py"
_OUTSIDE = "".join(str(digit) for digit in range(1, 7))  # an issuer outside 990000-999999
_INSIDE = "99999" + "A"


def _cusip(issuer: str) -> str:
    return issuer + "10" + cusip_check_digit(issuer + "10")


def _isin(issuer: str, country: str = "US") -> str:
    body = country + _cusip(issuer)
    return body + isin_check_digit(body)


def _lei(base: str) -> str:
    """``base`` with its ISO 17442 (ISO 7064 MOD 97-10) check digits."""
    number = int("".join(str(int(c, 36)) for c in base) + "00")
    return base + f"{98 - number % 97:02d}"


def _source(token: str, field: str = "X") -> str:
    return f'{field} = "{token}"\n'


@pytest.mark.parametrize(
    ("token", "found"),
    [
        (_isin(_OUTSIDE), _isin(_OUTSIDE)),
        (_isin(_OUTSIDE).lower(), _isin(_OUTSIDE).lower()),
        (_isin(_OUTSIDE)[:11] + "X", _isin(_OUTSIDE)[:11] + "X"),
        (_isin(_OUTSIDE)[:11] + "\\x00", _isin(_OUTSIDE)[:11]),
        (_isin(_OUTSIDE, "GB"), _isin(_OUTSIDE, "GB")),
    ],
    ids=["upper", "lower", "malformed-last-character", "truncated", "other-country"],
)
def test_checker_refuses_an_isin_outside_the_private_use_range(token: str, found: str) -> None:
    assert _problems(_PATH, _source(token)) == [f"{_PATH}:1: ISIN {found!r} in X"]
    assert _problems(_PATH, f"# {token}\n") == [f"{_PATH}:1: ISIN {found!r} in module"]
    assert _problems("fixtures/example.txt", token) == [
        f"fixtures/example.txt:1: ISIN {found!r} in module"
    ]


@pytest.mark.parametrize(
    "token",
    [_isin(_INSIDE), _isin(_INSIDE).lower(), _isin(_INSIDE, "IE"), "ZZ" + _isin(_OUTSIDE)[2:]],
    ids=["synthetic", "synthetic-lower", "synthetic-other-country", "user-assigned-country"],
)
def test_checker_accepts_a_synthetic_isin(token: str) -> None:
    assert _problems(_PATH, _source(token)) == []


@pytest.mark.parametrize(
    "text",
    [
        "Form67" + "EntryRow",  # mixed case: a class name in prose
        "a" * 40 + "0123456789",  # a hex digest
        "O:XQAQ" + "250117C" + "00250000",  # an OCC option symbol
        "1E" + "1000000",  # a number
    ],
)
def test_checker_skips_text_that_is_not_an_identifier(text: str) -> None:
    assert _problems(_PATH, _source(text)) == []


def test_checker_refuses_a_cusip_with_a_letter_or_in_a_cusip_field() -> None:
    letter = "ABC000" + "10"
    letter += cusip_check_digit(letter)
    digits = _OUTSIDE + "10" + cusip_check_digit(_OUTSIDE + "10")
    assert _problems(_PATH, _source(letter)) == [f"{_PATH}:1: CUSIP {letter!r} in X"]
    assert _problems(_PATH, _source(digits)) == []
    assert _problems(_PATH, _source(digits, "cusip")) == [f"{_PATH}:1: CUSIP {digits!r} in cusip"]
    assert _problems(_PATH, f'Cusip("{digits}")\n') == [f"{_PATH}:1: CUSIP {digits!r} in module"]
    assert _problems(_PATH, f"X = 'cusip = \"{digits}\"'\n") == [
        f"{_PATH}:1: CUSIP {digits!r} in X"
    ]
    assert _problems(_PATH, _source(_cusip(_INSIDE), "cusip")) == []


@pytest.mark.parametrize(
    "token", ["U" + "12345678", "I" + "99999999", "DU" + "1234567"], ids=["ibkr", "nb", "paper"]
)
def test_checker_skips_account_numbers_but_not_in_a_security_field(token: str) -> None:
    assert _problems(_PATH, _source(token, "account_id")) == []
    assert _problems(_PATH, _source(token, "participant_no")) == []
    assert _problems(_PATH, f'assert "{token}" not in out\n') == []
    assert _problems(_PATH, _source(token, "cusip")) == [f"{_PATH}:1: CUSIP {token!r} in cusip"]


@pytest.mark.parametrize(
    ("field", "token"),
    [("cusip", "1" * 9), ("cusip", "U" + "1" * 8), ("lei", "1" * 20), ("cik", _OUTSIDE + "7")],
    ids=["numeric-cusip", "account-shaped-cusip", "numeric-lei", "cik"],
)
@pytest.mark.parametrize(
    ("source", "scope", "line"),
    [
        ("{field} = (\n{value}\n)\n", "{field}", 2),
        ('X = {{"{field}":\n{value}}}\n', "X", 2),
        ("{field} = (  # field comment\n(\n{value}\n))\n", "{field}", 3),
        ('X = {{"{field}":  # field comment\n{value}}}\n', "X", 2),
        ("{field}: {annotation} = {value}\n", "{field}", 1),
        ("{field}: Final[{annotation}] = (\n{value}\n)\n", "{field}", 2),
        ("def f({field}: {annotation} = {value}): pass\n", "f", 1),
        ("def f({field}: {annotation} = {value}, /): pass\n", "f", 1),
        ("def f(*, {field}: {annotation} = {value}): pass\n", "f", 1),
        ("({field} := {value})\n", "module", 1),
        ("assert {field} != {value}\n", "module", 1),
        ("{field} = '''{token}'''\n", "{field}", 1),
        ('{field} = """{token}"""\n', "{field}", 1),
        ("{field} = '''\n{token}\n'''\n", "{field}", 2),
        ("{field} = u'{token}'\n", "{field}", 1),
        ("{field} = fr'{token}'\n", "{field}", 1),
        ("{field} = rf'{token}'\n", "{field}", 1),
        ("{field} = br'{token}'\n", "{field}", 1),
        ("{field} = rb'{token}'\n", "{field}", 1),
    ],
    ids=[
        "parenthesized-assignment",
        "dict",
        "commented-nested-grouping",
        "commented-dict",
        "annotated-variable",
        "final-annotation",
        "annotated-default",
        "annotated-positional-only-default",
        "annotated-keyword-only-default",
        "assignment-expression",
        "inequality",
        "triple-single-quotes",
        "triple-double-quotes",
        "multiline-string",
        "unicode-prefix",
        "formatted-raw-prefix",
        "raw-formatted-prefix",
        "bytes-raw-prefix",
        "raw-bytes-prefix",
    ],
)
def test_checker_refuses_non_synthetic_security_fields(
    field: str, token: str, source: str, scope: str, line: int
) -> None:
    value = token if field == "cik" else repr(token)
    annotation = "int" if field == "cik" else "str"
    assert _problems(
        _PATH, source.format(field=field, value=value, token=token, annotation=annotation)
    ) == [f"{_PATH}:{line}: {field.upper()} {token!r} in {scope.format(field=field)}"]


def test_checker_does_not_read_a_constructor_name_as_a_cusip_field() -> None:
    account = "1" * 9
    assert _problems(_PATH, f"RobinhoodCusipTransition(\n'{account}',\n)\n") == []


@pytest.mark.parametrize("field", ["securityCusip", "cusipValue", "CusipNumber"])
@pytest.mark.parametrize("token", ["1" * 9, "U" + "1" * 8])
def test_checker_refuses_compound_cusip_fields(field: str, token: str) -> None:
    assert _problems(_PATH, _source(token, field)) == [f"{_PATH}:1: CUSIP {token!r} in {field}"]
    assert _problems(_PATH, f'X = {{"{field}": "{token}"}}\n') == [
        f"{_PATH}:1: CUSIP {token!r} in X"
    ]


@pytest.mark.parametrize(
    ("tag", "token", "kind", "synthetic"),
    [
        ("cusip", "1" * 9, "CUSIP", _cusip(_INSIDE)),
        ("securityCusip", "U" + "1" * 8, "CUSIP", _cusip(_INSIDE)),
        ("registrantLei", "1" * 20, "LEI", _lei(_SYNTHETIC_LEI_PREFIX + "000099")),
        ("mgmtInvLei", "1" * 20, "LEI", _lei(_SYNTHETIC_LEI_PREFIX + "000099")),
        ("registrantCik", "000" + _OUTSIDE + "7", "CIK", str(_SYNTHETIC_CIKS.start).zfill(10)),
    ],
)
@pytest.mark.parametrize(
    ("template", "line"),
    [
        ("<{tag}>{token}</{tag}>", 1),
        ('<root xmlns:n="urn:sample"><n:{tag}>{token}</n:{tag}></root>', 1),
        ('<{tag} note="a > b">{token}</{tag}>', 1),
        ("<{tag}><![CDATA[{token}]]></{tag}>", 1),
        ("<root>\n<{tag}>\n<!-- sample --><!-- second -->\n{token}\n</{tag}>\n</root>", 4),
    ],
    ids=["plain", "namespace", "quoted-attribute", "cdata", "multiline-comment"],
)
def test_checker_refuses_xml_security_fields(
    tag: str, token: str, kind: str, synthetic: str, template: str, line: int
) -> None:
    xml = template.format(tag=tag, token=token)
    inside = template.format(tag=tag, token=synthetic)
    for path, text, valid_text, scope in (
        ("fixtures/example.xml", xml, inside, "module"),
        (_PATH, "X = '''" + xml + "'''\n", "X = '''" + inside + "'''\n", "X"),
    ):
        assert _problems(path, text) == [f"{path}:{line}: {kind} {token!r} in {scope}"]
        assert _problems(path, valid_text) == []


def test_checker_does_not_read_xml_tail_text_as_a_security_field() -> None:
    for tag, token in (
        ("securityCusip", "U" + "1" * 8),
        ("registrantLei", "1" * 20),
        ("registrantCik", _OUTSIDE + "7"),
    ):
        assert _problems("fixtures/example.xml", f"<root><{tag} />{token}</root>") == []


def test_checker_refuses_an_lei_with_a_valid_or_changed_check() -> None:
    valid = _lei("5493" + "00" + "ABCDEFGHIJ" + "12")
    changed = valid[:18] + f"{(int(valid[18:]) + 1) % 100:02d}"
    for token in (valid, changed):
        assert _problems(_PATH, _source(token)) == [f"{_PATH}:1: LEI {token!r} in X"]
    digits = "1" * 20
    assert _problems(_PATH, _source(digits)) == []
    assert _problems(_PATH, _source(digits, "lei")) == [f"{_PATH}:1: LEI {digits!r} in lei"]
    assert _problems(_PATH, _source(_lei(_SYNTHETIC_LEI_PREFIX + "000099"), "lei")) == []
    assert _problems(_PATH, _source("1." + "7976931348623157E308")) == []


@pytest.mark.parametrize(
    ("text", "cik"),
    [
        ("cik={cik}\n", None),
        ("CIK = {cik}\n", None),
        ("X = {{'cik': '{cik:010d}'}}\n", "{cik:010d}"),
        ("X = 'https://www.sec.gov/Archives/edgar/data/{cik}/a.htm'\n", None),
        ("X = 'https://www.sec.gov/cgi-bin/browse-edgar?CIK={cik}'\n", None),
        ("X = 'CIK{cik:010d}-submissions-001.json'\n", "{cik:010d}"),
        ("cik={cik:_}\n", "{cik:_}"),
    ],
    ids=["field", "constant", "dict", "edgar-path", "edgar-query", "file-name", "underscores"],
)
def test_checker_refuses_a_cik_outside_the_synthetic_range(text: str, cik: str | None) -> None:
    outside, inside = 1_234_567, 9_991_234
    shown = (cik or "{cik}").format(cik=outside)
    problems = _problems(_PATH, text.format(cik=outside))
    assert len(problems) == 1
    assert f": CIK {shown!r} in " in problems[0]
    assert _problems(_PATH, text.format(cik=inside)) == []


def test_checker_refuses_series_and_class_ids_outside_the_synthetic_range() -> None:
    for prefix in ("S", "C"):
        outside, inside = prefix + "0" * 8 + "1", prefix + _SYNTHETIC_SEC_PREFIX + "101"
        assert _problems(_PATH, _source(outside)) == [f"{_PATH}:1: SEC ID {outside!r} in X"]
        assert _problems(_PATH, _source(inside)) == []


@pytest.mark.parametrize(
    "allowed",
    [
        next(entry for entry in _ALLOWED if entry.kept is kept)
        for kept in (
            _STOCKPLAN_ISSUER,
            _CORE_FUND,
            _SWEEP_FUND,
            _INDIAN_ADRS,
            _DEPOSITARIES,
            _ADDRESS_ALIAS,
        )
    ],
    ids=lambda allowed: allowed.kept.identity,
)
def test_kept_identity_is_allowed_only_at_its_locations(allowed: _Allowed) -> None:
    token = sorted(allowed.kept.identifiers)[0]
    assert _problems(allowed.path, _source(token, allowed.scope)) == []
    for path, scope in ((allowed.path, "OTHER"), (_PATH, allowed.scope)):
        problems = _problems(path, _source(token, scope))
        assert len(problems) == 1
        assert f" {token!r} in {scope}" in problems[0]
    # Only the identifier itself: a lower-case copy is refused there too.
    if token[:2].isalpha():
        assert len(_problems(allowed.path, _source(token.lower(), allowed.scope))) == 1
