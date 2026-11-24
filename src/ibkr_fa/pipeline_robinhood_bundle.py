"""Discover and reconcile one complete two-account Robinhood source bundle."""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path

from ibkr_fa.compute.robinhood_entity_nature import (
    RobinhoodEntityNatureAudit,
    RobinhoodEntityNatureEvidence,
    merge_robinhood_entity_natures,
    robinhood_entity_nature_supersession_warnings,
)
from ibkr_fa.compute.robinhood_entity_overrides import (
    GleifLegalAddressAudit,
    RobinhoodAdrEntityAudit,
    RobinhoodFundEntityAudit,
)
from ibkr_fa.compute.robinhood_identity import (
    RobinhoodEntityRoute,
    RobinhoodIdentityResolution,
    RobinhoodSecEntityAudit,
    RobinhoodUnverifiedEntityAddress,
    merge_robinhood_entity_overrides,
    observed_robinhood_identity_inputs,
    resolve_robinhood_security_table,
    robinhood_entity_override_supersession_warnings,
    robinhood_identity_manual_fields,
)
from ibkr_fa.compute.robinhood_security_identities import RobinhoodSecurityIdentities
from ibkr_fa.config import (
    FaEntityOverride,
    RobinhoodConfig,
    UserConfig,
    load_user_config,
)
from ibkr_fa.exceptions import PathRefusalError
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountsReconciliation,
    RobinhoodActivityReportEvidence,
    RobinhoodBundleError,
    RobinhoodReconciliationError,
    RobinhoodSecurityIdentityResult,
    RobinhoodSecurityQuantityResult,
    RobinhoodStatementBundle,
    reconcile_robinhood_accounts,
    reconcile_robinhood_security_quantities,
    render_robinhood_security_toml,
    resolve_robinhood_security_identities,
    resolve_robinhood_security_quantities,
    validate_robinhood_corrected_statement,
    validate_robinhood_inventory_partition,
    validate_robinhood_report_coverage,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.normalize.robinhood.coverage import evidence_period_remedy
from ibkr_fa.normalize.robinhood.identity_exceptions import (
    RobinhoodCusipTransition,
    configured_cusip_transitions_path,
    cusip_transition_warnings,
    load_configured_cusip_transitions,
)
from ibkr_fa.normalize.robinhood.security import toml_string
from ibkr_fa.output._cleanup import refuse_input_collisions, scan_inputs
from ibkr_fa.parser.robinhood import read_robinhood_activity
from ibkr_fa.parser.robinhood_1042s import (
    Robinhood1042SPacket,
    read_robinhood_1042s_packet,
)
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatementKind,
    read_robinhood_statement,
    read_robinhood_statement_kind,
)
from ibkr_fa.path_checks import is_dir, is_file
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_robinhood_entities import resolve_robinhood_presentation_evidence
from ibkr_fa.pipeline_robinhood_option_prices import massive_api_key_path
from ibkr_fa.prices.cache import DEFAULT_PRICE_CACHE_DIR_NAME
from ibkr_fa.robinhood_identity_sources import (
    IdentitySourceAudit,
    identity_input_fingerprint,
    load_identity_sources,
)
from ibkr_fa.types import Isin

_ACCOUNT_COUNT = 2
_MISSING_REPORT = "each Robinhood report must be an existing file directly under activity/"
_UNCONFIGURED_REPORT = "activity/ must contain exactly the two configured report CSVs"
_MISSING_SELECTION = "corrected-statement selection names a missing PDF"


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodScopeStatus:
    """Out-of-scope product discovery retained for audit warnings."""

    crypto_statement_count: int
    cusip_transition_references: tuple[str, ...] = ()

    @property
    def warnings(self) -> tuple[str, ...]:
        warnings = []
        if self.crypto_statement_count:
            warnings.append(
                f"{self.crypto_statement_count} Robinhood Crypto statement"
                f"{'s were' if self.crypto_statement_count != 1 else ' was'} "
                "detected and excluded as out of scope"
            )
        warnings.extend(cusip_transition_warnings(self.cusip_transition_references))
        return tuple(warnings)


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodBundle:
    """Parsed, reconciled source documents shared by every Robinhood leg."""

    config: UserConfig
    robinhood: RobinhoodConfig
    securities: RobinhoodSecurityIdentities
    paired: RobinhoodAccountsReconciliation
    identities: RobinhoodSecurityIdentityResult
    packets: tuple[Robinhood1042SPacket, ...]
    quantities: RobinhoodSecurityQuantityResult
    scope: RobinhoodScopeStatus
    identity_source_audit: IdentitySourceAudit | None = None
    presentation_identity_source_audit: IdentitySourceAudit | None = None
    identity_resolution: RobinhoodIdentityResolution | None = None
    automatic_entity_overrides: tuple[FaEntityOverride, ...] = ()
    unverified_entity_addresses: tuple[RobinhoodUnverifiedEntityAddress, ...] = ()
    legal_address_audit: GleifLegalAddressAudit | None = None
    automatic_nature_evidence: tuple[RobinhoodEntityNatureEvidence, ...] = ()
    nature_audit: RobinhoodEntityNatureAudit | None = None
    fund_entity_audit: RobinhoodFundEntityAudit | None = None
    adr_entity_audit: RobinhoodAdrEntityAudit | None = None
    sec_entity_audit: RobinhoodSecEntityAudit | None = None

    @property
    def entity_overrides(self) -> dict[Isin, FaEntityOverride]:
        return merge_robinhood_entity_overrides(
            self.automatic_entity_overrides,
            self.config.fa.entities,
        )

    @property
    def nature_by_isin(self) -> dict[Isin, str]:
        return merge_robinhood_entity_natures(
            self.automatic_nature_evidence,
            self.config.fa.entities,
        )

    @property
    def entity_address_warnings(self) -> tuple[str, ...]:
        audit = () if self.legal_address_audit is None else self.legal_address_audit.warnings
        nature_audit = () if self.nature_audit is None else self.nature_audit.warnings
        fund_audit = () if self.fund_entity_audit is None else self.fund_entity_audit.warnings
        adr_audit = () if self.adr_entity_audit is None else self.adr_entity_audit.warnings
        sec_audit = () if self.sec_entity_audit is None else self.sec_entity_audit.warnings
        source_audit = self.presentation_identity_source_audit
        # Not the artifact's path: its folder name is a fingerprint of the
        # statement securities (#1044).
        source_warning = (
            () if source_audit is None or source_audit.failure is None else (source_audit.failure,)
        )
        return (
            *robinhood_entity_override_supersession_warnings(
                self.automatic_entity_overrides,
                self.config.fa.entities,
            ),
            *robinhood_entity_nature_supersession_warnings(
                self.automatic_nature_evidence,
                self.config.fa.entities,
            ),
            *audit,
            *nature_audit,
            *fund_audit,
            *adr_audit,
            *sec_audit,
            *source_warning,
        )


def _fail(message: str) -> RobinhoodBundleError:
    return RobinhoodBundleError(message)


def identity_failure_guidance(bundle: RobinhoodBundle) -> str:
    """The private identity guidance file's TOML text, or "" when every
    observed security has a resolved identity.

    It holds statement CUSIPs and symbols, so it is only ever written to the
    private guidance file, never printed (#1044).
    """

    resolution = bundle.identity_resolution
    checks = (
        {}
        if resolution is None
        else {failure.cusip: failure.checks for failure in resolution.failures}
    )
    rendered = render_robinhood_security_toml(
        bundle.paired,
        securities=bundle.securities.securities,
        failure_checks=checks,
    )
    if not rendered:
        return ""
    return (
        "# Private: holds statement CUSIPs and symbols. Review the source evidence,\n"
        "# replace every placeholder, and add these blocks to user.toml.\n\n" + rendered
    )


_ADDRESS_GUIDANCE_HEADER = (
    "# Private: Schedule FA Table A3 issuer addresses for [[fa.entities]] entries\n"
    "# that have no address. Nothing below has been filed. For each block, verify\n"
    "# the address against the issuer's own filings (annual report or regulatory\n"
    "# filings), uncomment its address and zip lines, correcting them if they are\n"
    "# wrong, and replace that ISIN's [[fa.entities]] entry in user.toml with the\n"
    "# block. prefer_configured_address = true files your verified address instead\n"
    "# of any automatic one. Then re-run.\n"
)
_FOUND_BY = {
    RobinhoodEntityRoute.LEGAL: "found by GLEIF (the ISIN's legal address)",
    RobinhoodEntityRoute.SEC: (
        "found by SEC (the filer's business address, after GLEIF found no usable record)"
    ),
    RobinhoodEntityRoute.ADR: (
        "found by GLEIF (the legal address of the depositary its Form F-6 names)"
    ),
    RobinhoodEntityRoute.FUND: (
        "found by GLEIF (the legal address of the registrant its Form N-CEN names)"
    ),
}
# Such an entry takes no automatic cell either, so the note also names the
# other keys an ETF's entry needs (#1521).
_ETF_NOT_LOOKED_UP = (
    "no address looked up for an ETF with an entry (ADR 0011): remove the entry to let "
    "the N-CEN route fill all four cells, or add the registrant's legal address, and "
    "name and nature_of_entity if this block has none"
)
_NOT_FOUND = "no address found; add the issuer's legal address"


def address_verification_guidance(bundle: RobinhoodBundle) -> str:
    """The private guidance file's address section, or "" when no held
    security's ``[[fa.entities]]`` entry leaves out ``address`` (#1449).

    One block per such entry: the entry's own keys, the address and ZIP found
    for it commented out, an empty ``nature_of_entity`` commented out when no
    nature is filed for it either (#1568), and ``prefer_configured_address =
    true``, so the block refuses at load until the filer uncomments a verified
    address. It holds ISINs, names and addresses, so it is only ever written to
    the private guidance file, never printed (#1044, #1048).
    """

    blocks = []
    for item in bundle.unverified_entity_addresses:
        entry, found = item.entry, item.found
        if found is not None:
            note, address, zip_code = _FOUND_BY[found.route], found.address, found.zip_code
        else:
            note, address, zip_code = (_ETF_NOT_LOOKED_UP if item.etf else _NOT_FOUND), "", ""
        blocks.append(
            "\n".join(
                (
                    f"# fa.entities[{item.entry_index}]: {note}",
                    "[[fa.entities]]",
                    f"isin = {toml_string(entry.isin)}",
                    *(() if entry.name is None else (f"name = {toml_string(entry.name)}",)),
                    *(
                        (f"nature_of_entity = {toml_string(entry.nature_of_entity)}",)
                        if entry.nature_of_entity is not None
                        else ('# nature_of_entity = ""',)
                        if item.nature_missing
                        else ()
                    ),
                    f"# address = {toml_string(address)}",
                    f"# zip = {toml_string(zip_code)}",
                    "prefer_configured_address = true",
                    *(("prefer_configured_name = true",) if entry.prefer_configured_name else ()),
                    *(
                        ("prefer_configured_nature = true",)
                        if entry.prefer_configured_nature
                        else ()
                    ),
                )
            )
        )
    if not blocks:
        return ""
    return _ADDRESS_GUIDANCE_HEADER + "\n" + "\n\n".join(blocks) + "\n"


def resolved_robinhood_quantities(
    bundle: RobinhoodBundle,
) -> RobinhoodSecurityQuantityResult:
    """Reconcile exact row-bound receipt quantity and basis evidence."""

    return resolve_robinhood_security_quantities(
        bundle.paired,
        bundle.identities,
        bundle.robinhood.security_receipt_resolutions,
    )


def _resolve(path: Path, *, refusal: str) -> Path:
    """``path.resolve()``, or refuse with ``refusal``.

    This and the other link refusals here are raised outside any handler, and
    containment is checked with ``is_relative_to``, never ``relative_to``, so no
    refusal chains an error naming a resolved path or a link's target (#1248).
    """
    with contextlib.suppress(OSError, RuntimeError):  # RuntimeError: a symlink loop on Python 3.12
        return path.resolve()
    raise _fail(refusal)


def _safe_path(root: Path, relative: str, *, label: str, missing: str) -> Path:
    candidate = _resolve(root / relative, refusal=missing)
    if not candidate.is_relative_to(root.resolve()):
        raise _fail(f"{label} escapes the configured input directory")
    return candidate


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entry_error(input_dir: Path, entry: Path, problem: str) -> RobinhoodBundleError:
    # Unlike the other bundle refusals this names a path, relative to the input
    # directory, because nothing else says which of many entries is broken.
    return _fail(f"{entry.relative_to(input_dir).as_posix()} {problem}")


def _unreadable_pdf(input_dir: Path, entry: Path) -> RobinhoodBundleError:
    return _entry_error(input_dir, entry, "is not a readable PDF file")


def _discover_pdfs(input_dir: Path, root: Path, *, label: str) -> dict[Path, Path]:
    """Map each ``*.pdf`` entry under ``root``, in sorted order, to its resolved path.

    Every entry is visited, so no PDF is skipped silently: a folder that can't be
    listed refuses, and so does an entry not named ``*.pdf`` that is a linked
    folder or a broken or looping link. Linked folders aren't followed, because
    the input-collision check (``scan_inputs``) doesn't follow them either.
    """

    entries: dict[Path, Path] = {}
    pending = _listing(input_dir, root)
    while pending:
        entry = pending.pop()
        if fnmatch.fnmatch(entry.name, "*.pdf"):
            resolved = None
            with contextlib.suppress(OSError, RuntimeError):  # RuntimeError: as in _resolve
                resolved = entry.resolve()
            if resolved is None:
                raise _unreadable_pdf(input_dir, entry)
            if not resolved.is_relative_to(input_dir.resolve()):
                raise _fail(f"{label} PDF symlink escapes the input directory")
            entries[entry] = resolved
        elif _is_plain_folder(input_dir, entry):
            pending.extend(_listing(input_dir, entry))
    return entries


def _listing(input_dir: Path, folder: Path) -> list[Path]:
    """``folder``'s entries, reverse-sorted so popping visits them in sorted order."""

    try:
        return sorted(folder.iterdir(), reverse=True)
    except OSError as exc:
        raise _entry_error(input_dir, folder, "is not a readable folder") from exc


def _is_plain_folder(input_dir: Path, entry: Path) -> bool:
    """Whether an entry not named ``*.pdf`` is a real folder to walk. A link to a
    folder, or one that can't be followed, refuses: PDFs behind it would be skipped."""

    try:
        mode = entry.lstat().st_mode
    except OSError as exc:  # its folder can be listed but not searched
        raise _entry_error(input_dir, entry.parent, "is not a readable folder") from exc
    if stat.S_ISLNK(mode):
        try:
            target = entry.stat().st_mode
        except OSError as exc:
            raise _entry_error(input_dir, entry, "is a broken or looping link") from exc
        if stat.S_ISDIR(target):
            raise _entry_error(
                input_dir, entry, "is a linked folder; replace it with a real folder"
            )
    return stat.S_ISDIR(mode)


def _readable_hashes(input_dir: Path, entries: dict[Path, Path]) -> dict[Path, str]:
    """Hash each entry's resolved path, refusing an entry that isn't a readable
    regular file. Only a regular file is opened, so a FIFO cannot block."""

    hashes: dict[Path, str] = {}
    for entry, path in entries.items():
        if path in hashes:
            continue
        digest = None
        with contextlib.suppress(OSError):
            if path.is_file():
                digest = _hash(path)
        if digest is None:
            raise _unreadable_pdf(input_dir, entry)
        hashes[path] = digest
    return hashes


def _dedupe_identical(hashes: dict[Path, str]) -> tuple[Path, ...]:
    by_hash: dict[str, Path] = {}
    for path in sorted(hashes):
        by_hash.setdefault(hashes[path], path)
    return tuple(by_hash.values())


def _statement_paths(
    input_dir: Path,
    config: RobinhoodConfig,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    root = input_dir / "statements"
    try:
        is_folder = is_dir(root)
    except OSError as exc:  # e.g. a link into a folder that can't be searched
        raise _entry_error(input_dir, root, "is not a readable folder") from exc
    if not is_folder:
        raise _fail("Robinhood bundle is missing the statements directory")
    entries = _discover_pdfs(input_dir, root, label="statement")
    discovered = set(entries.values())
    selection_paths: set[Path] = set()
    for selection in config.corrected_statements:
        selected = _safe_path(
            input_dir,
            selection.selected_file,
            label="selected statement",
            missing=_MISSING_SELECTION,
        )
        superseded = {
            _safe_path(input_dir, item, label="superseded statement", missing=_MISSING_SELECTION)
            for item in selection.supersedes
        }
        named = {selected, *superseded}
        if not named <= discovered or not all(is_file(path) for path in named):
            raise _fail(_MISSING_SELECTION)
        selection_paths |= named
        discovered -= superseded
    # A named entry keeps the check above; every other entry is checked before
    # the corrected statements are read.
    hashes = _readable_hashes(
        input_dir,
        {entry: path for entry, path in entries.items() if path not in selection_paths},
    )
    _validate_corrected_statements(input_dir, config)
    hashes.update((path, _hash(path)) for path in discovered & selection_paths)
    discovered = set(_dedupe_identical(hashes))

    brokerage: list[Path] = []
    crypto: list[Path] = []
    for path in sorted(discovered):
        if read_robinhood_statement_kind(path) is RobinhoodStatementKind.CRYPTO:
            crypto.append(path)
        else:
            brokerage.append(path)
    if not brokerage:
        raise _fail("Robinhood bundle contains no brokerage statements")
    return tuple(brokerage), tuple(crypto)


def _reports(
    input_dir: Path,
    config: RobinhoodConfig,
) -> tuple[RobinhoodActivityReportEvidence, RobinhoodActivityReportEvidence]:
    if len(config.reports) != _ACCOUNT_COUNT:
        raise _fail("configure exactly two [[robinhood.reports]] entries")
    activity_root = input_dir / "activity"
    if not is_dir(activity_root):
        raise _fail("Robinhood bundle is missing the activity directory")
    configured_paths = tuple(
        _safe_path(input_dir, item.file, label="activity report", missing=_MISSING_REPORT)
        for item in config.reports
    )
    if any(
        path.parent != activity_root.resolve() or not is_file(path) for path in configured_paths
    ):
        raise _fail(_MISSING_REPORT)
    discovered = {
        _resolve(path, refusal=_UNCONFIGURED_REPORT) for path in activity_root.glob("*.csv")
    }
    if discovered != set(configured_paths):
        raise _fail(_UNCONFIGURED_REPORT)
    reports = tuple(
        RobinhoodActivityReportEvidence(
            events=read_robinhood_activity(path),
            start_date=item.start_date,
            end_date=item.end_date,
        )
        for item, path in zip(config.reports, configured_paths, strict=True)
    )
    if len(reports) != _ACCOUNT_COUNT:
        raise _fail("internal error: report count changed after validation")
    return reports[0], reports[1]


def _tax_form_paths(input_dir: Path) -> tuple[Path, ...]:
    root = input_dir / "tax-forms"
    try:
        try:
            root.lstat()
        except FileNotFoundError:
            return ()  # only a missing entry means no tax forms
        is_folder = stat.S_ISDIR(root.stat().st_mode)
    except OSError as exc:  # a broken or looping link, or an entry that can't be inspected
        raise _fail("tax-forms must be a directory when present") from exc
    if not is_folder:
        raise _fail("tax-forms must be a directory when present")
    entries = _discover_pdfs(input_dir, root, label="tax-form")
    return _dedupe_identical(_readable_hashes(input_dir, entries))


def _validate_corrected_statements(
    input_dir: Path,
    config: RobinhoodConfig,
) -> None:
    for selection in config.corrected_statements:
        selected = read_robinhood_statement(
            _safe_path(
                input_dir,
                selection.selected_file,
                label="selected statement",
                missing=_MISSING_SELECTION,
            )
        )
        for relative in selection.supersedes:
            superseded = read_robinhood_statement(
                _safe_path(
                    input_dir, relative, label="superseded statement", missing=_MISSING_SELECTION
                )
            )
            validate_robinhood_corrected_statement(selected, superseded)


def refuse_robinhood_input_collisions(
    targets: tuple[Path, ...], *, input_dir: Path, config_path: Path
) -> None:
    """Refuse before any cleanup if a target is the config, a bundle file
    discovery may read, or a file the config names.

    ``--config`` and the bundle are checked before the config is parsed, so a
    config saved as a target refuses even when malformed. The config is then
    loaded only to name its files: a missing config names none (the bundle load
    reports it as before), and one that cannot load raises here, before anything
    is deleted, because its files cannot be checked.
    """
    refuse_input_collisions(targets, (("--config", config_path), ("--input", input_dir)))
    bundle_files = (
        *scan_inputs(input_dir / "activity", "*.csv"),
        *scan_inputs(input_dir / "statements", "*.pdf", recursive=True),
        *scan_inputs(input_dir / "tax-forms", "*.pdf", recursive=True),
    )
    refuse_input_collisions(targets, (("--input", path) for path in bundle_files))
    try:
        config = load_user_config(config_path)
    except (PathRefusalError, FileNotFoundError):
        return
    named: list[tuple[str, Path]] = []
    key_path = massive_api_key_path(config, config_path)
    if key_path is not None:
        named.append(("prices.massive_api_key_file", key_path))
    robinhood = config.robinhood
    if robinhood is not None:
        if robinhood.cusip_transitions_file is not None:
            named.append(
                (
                    "robinhood.cusip_transitions_file",
                    configured_cusip_transitions_path(
                        config_path, robinhood.cusip_transitions_file
                    ),
                )
            )
        named.extend(("robinhood.reports", input_dir / item.file) for item in robinhood.reports)
        named.extend(
            ("robinhood.corrected_statements", input_dir / relative)
            for selection in robinhood.corrected_statements
            for relative in (selection.selected_file, *selection.supersedes)
        )
    refuse_input_collisions(targets, named)


def _reconcile(
    bundles: tuple[RobinhoodStatementBundle, ...],
    reports: tuple[RobinhoodActivityReportEvidence, RobinhoodActivityReportEvidence],
    robinhood: RobinhoodConfig,
    cusip_transitions: tuple[RobinhoodCusipTransition, ...],
    financial_year: FinancialYear,
    *,
    tax_forms: bool,
    schedule_fa: bool,
) -> RobinhoodAccountsReconciliation:
    """Pass the FY, and whether the run builds Schedule FA, to normalize for
    its remedy mark, then append the text only to marked refusals, with
    ``tax_forms`` when the run reads Forms 1042-S.
    """
    try:
        return reconcile_robinhood_accounts(
            bundles,
            reports,
            robinhood.accounts,
            cusip_transitions,
            financial_year=financial_year,
            schedule_fa=schedule_fa,
        )
    except RobinhoodReconciliationError as exc:
        if not exc.evidence_period:
            raise
        message = exc.message
    # Raised after the handler, so it chains to nothing.
    raise RobinhoodReconciliationError(
        f"{message}. {evidence_period_remedy(financial_year, tax_forms=tax_forms)}",
        evidence_period=True,
    )


def load_robinhood_bundle(
    *,
    input_dir: Path,
    config_path: Path,
    financial_year: FinancialYear,
    include_tax_forms: bool = False,
    schedule_fa: bool = True,
    fetch_identities: bool = True,
    identity_cache_dir: Path | None = None,
) -> RobinhoodBundle:
    """Discover, parse, reconcile, and coverage-check one Robinhood bundle;
    ``schedule_fa`` says whether the run builds Schedule FA."""

    if not is_dir(input_dir):
        raise _fail("Robinhood input path must be a directory")
    config = load_user_config(config_path)
    if config.robinhood is None:
        raise _fail("missing [robinhood] config")
    robinhood = config.robinhood
    cusip_transitions = load_configured_cusip_transitions(
        config_path,
        robinhood.cusip_transitions_file,
        account_numbers=frozenset(account.number for account in robinhood.accounts),
    )
    cache_dir = identity_cache_dir or (input_dir / DEFAULT_PRICE_CACHE_DIR_NAME)
    reports = _reports(input_dir, robinhood)
    # Tax forms are checked before any statement is extracted, and parsed last.
    tax_form_paths = _tax_form_paths(input_dir) if include_tax_forms else ()
    brokerage_paths, crypto_paths = _statement_paths(input_dir, robinhood)
    statements = tuple(read_robinhood_statement(path) for path in brokerage_paths)
    bundles = validate_robinhood_statement_bundles(statements)
    tax_forms = bool(tax_form_paths)
    paired = _reconcile(
        bundles,
        reports,
        robinhood,
        cusip_transitions,
        financial_year,
        tax_forms=tax_forms,
        schedule_fa=schedule_fa,
    )
    validate_robinhood_report_coverage(paired, robinhood, financial_year, tax_forms=tax_forms)
    observed = observed_robinhood_identity_inputs(paired)
    manual_fields = robinhood_identity_manual_fields(robinhood.securities)
    fingerprint = identity_input_fingerprint(observed, manual_fields)
    missing = {item.cusip for item in observed} - {item.cusip for item in robinhood.securities}
    source_audit: IdentitySourceAudit | None = None
    resolution: RobinhoodIdentityResolution | None = None
    identity_sources = None
    securities = RobinhoodSecurityIdentities(robinhood.securities)
    if missing:
        identity_sources, source_audit = load_identity_sources(
            observed,
            manual_fields,
            cache_dir=cache_dir,
            fetch=fetch_identities,
            sec_user_agent=robinhood.sec_user_agent,
        )
        if identity_sources is not None:
            securities, resolution = resolve_robinhood_security_table(
                observed,
                identity_sources,
                robinhood.securities,
            )
    elif source_audit is None:
        source_audit = IdentitySourceAudit(fingerprint, True, None, None)
    identities = resolve_robinhood_security_identities(paired, securities.securities)
    quantities = reconcile_robinhood_security_quantities(paired, identities)
    validate_robinhood_inventory_partition(paired, quantities)
    presentation = resolve_robinhood_presentation_evidence(
        robinhood=robinhood,
        securities=securities.securities,
        configured=config.fa.entities,
        observed=observed,
        manual_fields=manual_fields,
        identity_sources=identity_sources,
        quantities=quantities,
        calendar_year=financial_year.start_year,
        cache_dir=cache_dir,
        fetch=fetch_identities,
    )
    scope = RobinhoodScopeStatus(
        crypto_statement_count=len(crypto_paths),
        cusip_transition_references=paired.cusip_transition_references,
    )
    return RobinhoodBundle(
        config=config,
        robinhood=robinhood,
        securities=securities,
        paired=paired,
        identities=identities,
        packets=tuple(read_robinhood_1042s_packet(path) for path in tax_form_paths),
        quantities=quantities,
        scope=scope,
        identity_source_audit=source_audit,
        presentation_identity_source_audit=presentation.identity_source_audit,
        identity_resolution=resolution,
        automatic_entity_overrides=presentation.entity_overrides,
        unverified_entity_addresses=presentation.unverified_addresses,
        legal_address_audit=presentation.legal_address_audit,
        automatic_nature_evidence=presentation.nature_evidence,
        nature_audit=presentation.nature_audit,
        fund_entity_audit=presentation.fund_audit,
        adr_entity_audit=presentation.adr_audit,
        sec_entity_audit=presentation.sec_entity_audit,
    )
