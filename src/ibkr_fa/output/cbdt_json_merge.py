"""Merge an ibkr-fa Schedule FA fragment into a saved ITR-2 or ITR-3 return.

The offline utilities export/import the FULL return under ``ITR.ITR2`` or
``ITR.ITR3``. Hand-splicing a
fragment into that (often minified, multi-MB) file risks silently dropping the
sibling tables (A1/A4-A10) or other-broker rows, or duplicating on a re-run — a
**Black Money Act exposure** (ADR 0002 dec. 8). This module is the tested merge.

**Strict merge-once** (the chosen scope): it refuses on ANY ambiguity rather
than risk a wrong filing. The target's Table A3 must be EMPTY (so there is no
per-lot identity to guess — the A3 CBDT item carries no ISIN/account key), and
an existing Table A2 row for ibkr-fa's own account is a conflict. The
``--fragment`` is treated as UNTRUSTED input (the ``_ibkrFa`` provenance marker
is forgeable, and the vendored schema's date ``pattern`` is unanchored + its
``nonEmptyString`` is a no-op — so a hand-edited fragment can pass ``validate_item``
with ``2026-02-30`` or a whitespace-only account number); every imported item is
re-validated as strictly as the writer BUILDS it. That includes the
:class:`~ibkr_fa.models.fa_a2_row.FaA2Row` rules the schema gate misses: a Table A2
account number must be ASCII and not IBKR's masked ``U****`` shape, and its peak and
closing balances must not be below the CBDT 14-digit bound, which the ITR-3 schema
doesn't set for them. Every value outside the two
Table A2/A3 lists is kept, each number at its exact decimal value via a ``Decimal``
round-trip, not a float approximation (a plain decimal such as ``-1500.50`` keeps
its text). The output is rewritten as indented UTF-8 JSON, so its layout and
bytes differ from the input.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import os
import tempfile
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from ibkr_fa.exceptions import IbkrFaError, errno_name
from ibkr_fa.models.money import CBDT_AMOUNT_BOUND
from ibkr_fa.output._atomic import discard_staging_file, staging_file_pattern
from ibkr_fa.output._cleanup import (
    ClaimedOutputCleanup,
    FileIdentity,
    OutputRemovalError,
    RunOutputs,
)
from ibkr_fa.output.cbdt_json_fragment import (
    _CBDT_DATE_PATTERN,
    CBDT_JSON_FRAGMENT_FORMAT_ID,
    _a2_row_consistency_errors,
    is_ibkr_fa_cbdt_fragment,
)
from ibkr_fa.output.cbdt_json_schema import (
    A2_DEFINITION,
    A3_DEFINITION,
    CBDT_ITR_FORMS,
    CBDT_SCHEMA_ASSESSMENT_YEAR_START,
    CbdtItrForm,
    CbdtSchemaError,
    schema_version_for,
    validate_item,
)
from ibkr_fa.path_checks import exists
from ibkr_fa.pii import is_masked_account_id
from ibkr_fa.text_checks import first_control_character

if TYPE_CHECKING:
    from collections.abc import Iterable

_PROVENANCE_KEY: Final[str] = "_ibkrFa"
_SCHEDULE_FA_KEY: Final[str] = "ScheduleFA"

# Where ScheduleFA lives in each supported saved return.
_ITR_KEY: Final[str] = "ITR"
_FORM_PATHS: Final[dict[CbdtItrForm, tuple[str, str]]] = {
    "ITR-2": ("ITR2", "Form_ITR2"),
    "ITR-3": ("ITR3", "Form_ITR3"),
}

# Target-identity literals. The schema's own patterns are unanchored / use the
# `.` metachar (so `ITR-2`/`2026`/`Ver1.0` would also match `ITR-20`/`2026-28`/
# `Ver1x0`), so we pin exact values instead. Grounded against a REAL filed ITR-2
# JSON: `Form_ITR2.AssessmentYear` is the bare 4-digit START year of the AY
# (e.g. AY 2021-22 → "2021"; the "2026-27" label format is used only by other
# fields such as ScheduleESOP), and `SchemaVer` / `FormVer` are INDEPENDENT (a
# real return had SchemaVer="Ver1.1" with FormVer="Ver1.0"). We pin only
# SchemaVer — it governs the JSON item structure our A2/A3 mappings must match;
# FormVer is form-layout and not structurally relevant, so coupling them would
# wrongly refuse a return saved against a minor schema revision. Pinning errs
# toward REFUSE (safe — never mis-files); re-verify these when the schema is
# re-vendored (alongside CBDT_SCHEMA_VERSION).
_EXPECTED_ASSESSMENT_YEAR: Final[str] = str(CBDT_SCHEMA_ASSESSMENT_YEAR_START)
_EXPECTED_SCHEMA_VER: Final[str] = "Ver1.0"

# Required-field partitions per table (from the vendored schema), used to layer
# the writer's emitter invariants on top of `validate_item` for UNTRUSTED input.
_A2_DATE_FIELDS: Final[tuple[str, ...]] = ("AccOpenDate",)
_A2_STR_FIELDS: Final[tuple[str, ...]] = (
    "CountryName",
    "FinancialInstName",
    "FinancialInstAddress",
    "ZipCode",
    "AccountNumber",
)
_A3_DATE_FIELDS: Final[tuple[str, ...]] = ("InterestAcquiringDate",)
_A3_STR_FIELDS: Final[tuple[str, ...]] = (
    "CountryName",
    "NameOfEntity",
    "AddressOfEntity",
    "ZipCode",
    "NatureOfEntity",
)
# FaA3Row refuses a negative value for each of these; the ITR-2 schema allows
# one and the ITR-3 schema sets no minimum.
_A3_AMOUNT_FIELDS: Final[tuple[str, ...]] = (
    "InitialValOfInvstmnt",
    "PeakBalanceDuringPeriod",
    "ClosingBalance",
    "TotGrossAmtPaidCredited",
    "TotGrossProceeds",
)
_A2_ACCOUNT_KEY: Final[str] = "AccountNumber"
_A2_NATURE_KEY: Final[str] = "NatureOfAmount"
# FaA2Row lets each of these be negative, but not below the CBDT bound; the ITR-3
# schema sets no minimum for them.
_A2_SIGNED_AMOUNT_FIELDS: Final[tuple[str, ...]] = ("PeakBalanceDuringPeriod", "ClosingBalance")


class CbdtJsonMergeError(IbkrFaError):
    """The merge cannot proceed safely, so it refuses (rather than risk a
    dropped / duplicated / clobbered / corrupted filed row).

    Its messages identify the affected file or validation location, never a
    value from the fragment or the return (#1048): the return holds the holder's
    PAN, name, address and bank accounts.
    """


# --------------------------------------------------------------------------- #
# Safe JSON load / lossless dump                                              #
# --------------------------------------------------------------------------- #


def _reject_duplicate_keys(label: str, path: Path, pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that REFUSES any duplicate key, naming the
    ``label`` file at ``path`` but not the key.

    Python's default keeps the LAST of duplicate keys and silently drops the
    earlier ones — so a plain load→dump of a return that happens to contain a
    duplicate key (anywhere, e.g. in an unrelated schedule) would corrupt it.
    """
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise CbdtJsonMergeError(
                f"refusing to merge: the {label} file {path} has a duplicate key in a JSON "
                "object; loading it would silently drop data. Re-export a clean file."
            )
        out[key] = value
    return out


def _reject_non_finite(token: str) -> Any:
    raise CbdtJsonMergeError(
        f"refusing to merge: the JSON contains a non-finite number ({token}); "
        "NaN/Infinity are not valid JSON and cannot be filed."
    )


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    """Load a JSON OBJECT from ``path`` with the tax-safety guards: reject a
    BOM, a non-object root, duplicate keys anywhere, and NaN/Infinity; preserve
    every number EXACTLY as a :class:`~decimal.Decimal` (a plain float would
    rewrite ``-1500.50`` as ``-1500.5`` and lose >15-digit precision)."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CbdtJsonMergeError(
            f"could not read the {label} file {path} ({errno_name(exc)})"
        ) from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise CbdtJsonMergeError(
            f"the {label} file {path} starts with a UTF-8 BOM; re-save it as plain UTF-8."
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CbdtJsonMergeError(
            f"the {label} file {path} is not valid UTF-8 (at byte offset {exc.start})"
        ) from exc
    try:
        obj = json.loads(
            text,
            parse_float=Decimal,
            object_pairs_hook=functools.partial(_reject_duplicate_keys, label, path),
            parse_constant=_reject_non_finite,
        )
    except json.JSONDecodeError as exc:
        raise CbdtJsonMergeError(
            f"the {label} file {path} is not valid JSON (at line {exc.lineno}, column {exc.colno})"
        ) from exc
    if not isinstance(obj, dict):
        raise CbdtJsonMergeError(
            f"the {label} file {path} must be a JSON object, not a {type(obj).__name__}."
        )
    return cast("dict[str, Any]", obj)


def _dumps_lossless(obj: dict[str, Any]) -> str:
    """Serialise ``obj`` to indented JSON, emitting every :class:`~decimal.Decimal`
    as a BARE number with its ``str()`` text and exact decimal value, not a float
    approximation (a plain decimal such as ``-1500.50`` keeps its text).
    The merge keeps every value outside the two Table A2/A3 lists and writes
    this text as indented UTF-8 JSON, so its layout and bytes differ from the input.
    Uses a collision-proof placeholder + replace,
    because the stdlib encoder cannot emit a ``Decimal`` as a bare number."""
    token = uuid.uuid4().hex
    subs: dict[str, str] = {}

    def _default(value: object) -> str:
        if isinstance(value, Decimal):
            key = f"__ibkrfa_dec_{token}_{len(subs)}__"
            subs[key] = str(value)
            return key
        raise TypeError(f"cannot serialise {type(value).__name__}")  # pragma: no cover

    text = json.dumps(obj, default=_default, ensure_ascii=False, indent=2)
    for key, decimal_text in subs.items():
        text = text.replace(f'"{key}"', decimal_text)
    return text + "\n"


# --------------------------------------------------------------------------- #
# UNTRUSTED fragment validation                                               #
# --------------------------------------------------------------------------- #


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CbdtJsonMergeError(message)


def _is_clean_nonblank(value: object) -> bool:
    """A non-whitespace string with no control characters (a hand-edited
    fragment can carry ``"   "`` or ``"IBKR\\nEVIL"`` past the schema). The
    builder refuses the same control characters
    (:func:`~ibkr_fa.text_checks.first_control_character`)."""
    return isinstance(value, str) and value.strip() != "" and first_control_character(value) is None


def _is_valid_cbdt_date(value: object) -> bool:
    """A real ``YYYY-MM-DD`` calendar date. The schema's pattern is unanchored
    and not calendar-aware (it passes ``2026-02-30`` and ``2026-01-01T00:00:00``),
    so require BOTH a full-match AND :meth:`datetime.date.fromisoformat`."""
    if not isinstance(value, str) or _CBDT_DATE_PATTERN.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _validate_untrusted_item(
    item: object,
    definition: str,
    *,
    row_index: int,
    label: str,
    date_fields: Iterable[str],
    str_fields: Iterable[str],
    amount_fields: Iterable[str] = (),
    itr_form: CbdtItrForm,
) -> dict[str, Any]:
    """Validate an item from the UNTRUSTED fragment, ``row_index`` (0-based) of
    its table, as strictly as the writer builds it: the CBDT schema gate
    (types/enums/maxLength/required) PLUS the emitter invariants the schema is
    too weak to enforce — calendar-valid dates, non-blank, control-char-free
    required strings and non-negative amounts."""
    _require(isinstance(item, dict), f"{label} must be a JSON object")
    item = cast("dict[str, Any]", item)
    try:
        validate_item(item, definition, itr_form, row_index=row_index)
    except CbdtSchemaError as exc:
        raise CbdtJsonMergeError(
            f"{label} failed CBDT schema validation: {exc.rule_text()}"
        ) from exc
    for field in date_fields:
        _require(
            _is_valid_cbdt_date(item.get(field)),
            f"{label} field {field!r} is not a valid YYYY-MM-DD calendar date "
            "— the fragment looks hand-edited or corrupt",
        )
    for field in str_fields:
        _require(
            _is_clean_nonblank(item.get(field)),
            f"{label} field {field!r} is blank/whitespace or has control characters "
            "— the fragment looks hand-edited or corrupt",
        )
    for field in amount_fields:
        # The schema gate above made each one an int.
        _require(
            item[field] >= 0,
            f"{label} field {field!r} is negative — the fragment looks hand-edited or corrupt",
        )
    return item


def _validate_untrusted_a2_item(
    item: object, *, row_index: int, itr_form: CbdtItrForm
) -> dict[str, Any]:
    """Validate Table A2 row ``row_index`` (0-based) of the UNTRUSTED fragment:
    :func:`_validate_untrusted_item`, then the
    :class:`~ibkr_fa.models.fa_a2_row.FaA2Row` rules the schema gate misses. The
    account number must be ASCII and not IBKR's masked shape, and the peak and
    closing balances must not be below the CBDT bound. Values are checked as
    given, never stripped or rounded."""
    label = f"fragment Table A2 row {row_index + 1}"
    checked = _validate_untrusted_item(
        item,
        A2_DEFINITION,
        row_index=row_index,
        label=label,
        date_fields=_A2_DATE_FIELDS,
        str_fields=_A2_STR_FIELDS,
        itr_form=itr_form,
    )
    # The checks above made the account number a string and each amount an int.
    account = checked[_A2_ACCOUNT_KEY]
    _require(
        account.isascii(),
        f"{label} field {_A2_ACCOUNT_KEY!r} has a non-ASCII character "
        "— the fragment looks hand-edited or corrupt",
    )
    _require(
        not is_masked_account_id(account),
        f"{label} field {_A2_ACCOUNT_KEY!r} is a masked account number "
        "— the fragment looks hand-edited or corrupt",
    )
    for field in _A2_SIGNED_AMOUNT_FIELDS:
        _require(
            checked[field] >= -CBDT_AMOUNT_BOUND,
            f"{label} field {field!r} is below the CBDT 14-digit bound "
            "— the fragment looks hand-edited or corrupt",
        )
    return checked


def _validate_fragment(
    fragment_path: Path, expected_form: CbdtItrForm
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate the UNTRUSTED fragment fully; return ``(a2_items, a3_items)``."""
    frag = _load_json_object(fragment_path, "fragment")
    marker = frag.get(_PROVENANCE_KEY)
    _require(isinstance(marker, dict), "fragment _ibkrFa marker must be an object")
    assert isinstance(marker, dict)
    _require(
        marker.get("format") == CBDT_JSON_FRAGMENT_FORMAT_ID,
        f"fragment _ibkrFa.format must be {CBDT_JSON_FRAGMENT_FORMAT_ID!r}",
    )
    marker_form_value = marker.get("itrForm")
    if marker_form_value is None:
        # Legacy fragments predate the itrForm marker and were ITR-2-only.
        fragment_form: CbdtItrForm = "ITR-2"
    else:
        _require(
            marker_form_value in CBDT_ITR_FORMS,
            f"fragment _ibkrFa.itrForm must be one of {CBDT_ITR_FORMS!r}",
        )
        fragment_form = cast("CbdtItrForm", marker_form_value)
    _require(
        fragment_form == expected_form,
        f"fragment targets {fragment_form}, but the saved return is {expected_form}; "
        "regenerate the fragment for the target form",
    )
    expected_schema_version = schema_version_for(fragment_form)
    _require(
        marker.get("schemaVersion") == expected_schema_version,
        "fragment was built for a different CBDT schema "
        f"(its _ibkrFa.schemaVersion is not {expected_schema_version!r}); "
        "regenerate it with this version of ibkr-fa",
    )
    sched = frag.get(_SCHEDULE_FA_KEY)
    _require(isinstance(sched, dict), "fragment ScheduleFA must be an object")
    assert isinstance(sched, dict)
    _require(
        set(sched) <= {A2_DEFINITION, A3_DEFINITION},
        f"fragment ScheduleFA has a table other than {A2_DEFINITION} and {A3_DEFINITION}",
    )
    a2_list = sched.get(A2_DEFINITION)
    _require(
        isinstance(a2_list, list) and len(a2_list) >= 1,
        f"fragment must carry at least one {A2_DEFINITION} (Table A2) row",
    )
    assert isinstance(a2_list, list)
    # The writer always emits this table, so an absent one is not "no holdings".
    a3_list = sched.get(A3_DEFINITION)
    _require(
        isinstance(a3_list, list),
        f"fragment must carry a {A3_DEFINITION} (Table A3) list, even an empty one",
    )
    assert isinstance(a3_list, list)

    a2_items = [
        _validate_untrusted_a2_item(row, row_index=index, itr_form=fragment_form)
        for index, row in enumerate(a2_list)
    ]
    a2_errors = _a2_row_consistency_errors(a2_items)
    _require(not a2_errors, "fragment " + "; ".join(a2_errors))
    a3_items = [
        _validate_untrusted_item(
            row,
            A3_DEFINITION,
            row_index=i,
            label=f"fragment Table A3 row {i + 1}",
            date_fields=_A3_DATE_FIELDS,
            str_fields=_A3_STR_FIELDS,
            amount_fields=_A3_AMOUNT_FIELDS,
            itr_form=fragment_form,
        )
        for i, row in enumerate(a3_list)
    ]
    return a2_items, a3_items


# --------------------------------------------------------------------------- #
# Target validation + strict merge                                            #
# --------------------------------------------------------------------------- #


def _require_dict(value: object, what: str) -> dict[str, Any]:
    _require(isinstance(value, dict), f"the target return has no valid {what}")
    return cast("dict[str, Any]", value)


def _check_target_identity(
    return_body: dict[str, Any], itr_form: CbdtItrForm, form_key: str
) -> None:
    """Refuse unless the target is an AY 2026-27 supported ITR at the vendored schema
    version (so AY2026-27 FA rows are never spliced into the wrong form/year)."""
    form = _require_dict(return_body.get(form_key), f"{form_key} block")
    _require(
        form.get("FormName") == itr_form,
        f"the target is not an {itr_form} ({form_key}.FormName is not {itr_form!r})",
    )
    _require(
        form.get("AssessmentYear") == _EXPECTED_ASSESSMENT_YEAR,
        f"the target's assessment year ({form_key}.AssessmentYear) is not "
        f"AY 2026-27 (expected {_EXPECTED_ASSESSMENT_YEAR!r}) — this fragment is "
        "for AY 2026-27 only",
    )
    _require(
        form.get("SchemaVer") == _EXPECTED_SCHEMA_VER,
        "the target was saved against a different JSON schema revision "
        f"({form_key}.SchemaVer is not {_EXPECTED_SCHEMA_VER!r}) "
        "— ibkr-fa's Schedule FA mapping is pinned to that revision. Open an "
        "issue with your SchemaVer so it can be supported.",
    )
    # FormVer is form-layout, not JSON structure, so its VALUE is not pinned (a
    # real return had FormVer != SchemaVer); but it is a required field, so its
    # absence means the target is not a genuine offline-utility return export.
    form_ver = form.get("FormVer")
    _require(
        isinstance(form_ver, str) and form_ver.strip() != "",
        f"the target's {form_key} has no FormVer — it does not look like a genuine "
        f"{itr_form} export from the offline utility",
    )


def _detect_target(itr: dict[str, Any]) -> tuple[CbdtItrForm, str, dict[str, Any]]:
    present = [
        (itr_form, return_key, form_key)
        for itr_form, (return_key, form_key) in _FORM_PATHS.items()
        if return_key in itr
    ]
    _require(
        len(present) == 1,
        "the target must contain exactly one supported return wrapper under ITR: ITR2 or ITR3",
    )
    itr_form, return_key, form_key = present[0]
    return_body = _require_dict(itr.get(return_key), f"{_ITR_KEY}.{return_key}")
    _check_target_identity(return_body, itr_form, form_key)
    return itr_form, return_key, return_body


def _canonical_account(value: object) -> str | None:
    """Canonical form for A2 account-number CONFLICT comparison only (never the
    filed value): trim + casefold, so ``" U1234567"`` / ``"u1234567"`` are
    recognised as the same account."""
    if not isinstance(value, str):
        return None
    stripped = value.strip().casefold()
    return stripped or None


@dataclasses.dataclass(frozen=True, slots=True)
class CbdtMergeReport:
    """What the merge did, for the loud non-quietable summary."""

    output_path: Path
    itr_form: CbdtItrForm
    a2_rows_added: int
    a2_account_numbers: tuple[str, ...]
    a3_rows_added: int
    preserved_a2_rows: int
    preserved_a2_accounts: tuple[str, ...]
    created_schedule_fa: bool
    # The merged return, removed only while it is still the file the merge published.
    run_outputs: RunOutputs = dataclasses.field(kw_only=True)


def merge_cbdt_fragment(
    *, fragment_path: Path, itr_path: Path, output_path: Path
) -> CbdtMergeReport:
    """Splice ``fragment_path``'s Table A2 + A3 into a saved ITR-2/ITR-3 at
    ``itr_path``, writing the result to a NEW ``output_path`` (the inputs are
    never modified). Refuses on any ambiguity. An existing fragment that cannot
    be inspected raises ``CbdtJsonUnreadableError``; other validation failures
    raise :class:`CbdtJsonMergeError`.
    """
    # Inspect before resolve() can fail on an existing uninspectable symlink.
    if not is_ibkr_fa_cbdt_fragment(fragment_path):
        raise CbdtJsonMergeError(
            f"{fragment_path} is not an ibkr-fa Schedule FA fragment (missing the "
            f"_ibkrFa.format == {CBDT_JSON_FRAGMENT_FORMAT_ID!r} provenance marker). "
            "Generate it with `schedule-fa --format cbdt-json-fragment`."
        )
    # --- path safety (never clobber/alias an input) --------------------------
    out_resolved = output_path.resolve()
    for other, label in ((itr_path, "--itr"), (fragment_path, "--fragment")):
        _require(
            out_resolved != other.resolve(),
            f"--output must be a different path from {label} (refusing to overwrite an input)",
        )
    _require(
        not exists(output_path),
        f"--output {output_path} already exists; choose a new path (the merge never "
        "overwrites a file, so it can't clobber a return you care about)",
    )

    # --- load/identify target, then validate the fragment for that form -------
    target = _load_json_object(itr_path, "ITR")
    itr = _require_dict(target.get(_ITR_KEY), f"{_ITR_KEY} root")
    itr_form, return_key, return_body = _detect_target(itr)
    a2_items, a3_items = _validate_fragment(fragment_path, itr_form)

    # --- locate / create ScheduleFA (preserve all sibling tables) -----------
    created_schedule_fa = _SCHEDULE_FA_KEY not in return_body
    if created_schedule_fa:
        return_body[_SCHEDULE_FA_KEY] = {}
    sched = _require_dict(return_body.get(_SCHEDULE_FA_KEY), f"{return_key}.ScheduleFA")

    # --- Table A2: refuse on a same-account conflict, else append ------------
    existing_a2 = sched.get(A2_DEFINITION, [])
    _require(
        isinstance(existing_a2, list) and all(isinstance(r, dict) for r in existing_a2),
        f"{return_key}.ScheduleFA.DtlsForeignCustodialAcc (Table A2) is malformed "
        "(not a list of objects)",
    )
    existing_a2 = cast("list[dict[str, Any]]", existing_a2)
    our_accounts = tuple(str(item[_A2_ACCOUNT_KEY]) for item in a2_items)
    our_canonical = tuple(_canonical_account(account) for account in our_accounts)
    our_keys = tuple(
        (account, str(item[_A2_NATURE_KEY]))
        for account, item in zip(our_canonical, a2_items, strict=True)
    )
    _require(
        all(account is not None for account in our_canonical)
        and len(set(our_keys)) == len(our_keys),
        "fragment Table A2 rows contain duplicate AccountNumber/NatureOfAmount pairs "
        "or a blank AccountNumber",
    )
    preserved: list[str] = []
    for existing_index, row in enumerate(existing_a2):
        existing_canonical = _canonical_account(row.get(_A2_ACCOUNT_KEY))
        _require(
            existing_canonical is not None,
            f"target Table A2 row {existing_index + 1} field 'AccountNumber' is missing/blank; "
            "refusing to merge into a target with a corrupt A2 row",
        )
        for index, canonical in enumerate(our_canonical):
            _require(
                existing_canonical != canonical,
                f"fragment Table A2 row {index + 1} field {_A2_ACCOUNT_KEY!r} is already "
                f"disclosed in the target's Table A2 row {existing_index + 1} — merge into "
                f"a CLEAN {itr_form} export, once (re-export from the utility)",
            )
        preserved.append(str(row.get(_A2_ACCOUNT_KEY)))

    # --- Table A3: must be empty (no per-lot identity to merge by) -----------
    existing_a3 = sched.get(A3_DEFINITION, [])
    _require(
        isinstance(existing_a3, list),
        f"{return_key}.ScheduleFA.DtlsForeignEquityDebtInterest is malformed",
    )
    assert isinstance(existing_a3, list)
    _require(
        len(existing_a3) == 0,
        "the target already has Table A3 (foreign equity/debt) rows; merge ibkr-fa "
        f"into a CLEAN {itr_form} export, once (re-export from the utility, then merge)",
    )

    # --- compose (read ONLY the two arrays from the fragment) ----------------
    sched[A2_DEFINITION] = [*existing_a2, *a2_items]
    sched[A3_DEFINITION] = list(a3_items)

    # --- paranoia: the items WE inserted must still be schema-valid + no marker.
    # Only OUR rows — the user's pre-existing other-account A2 rows are their
    # filed data, preserved verbatim; the offline utility validates them, not us
    # (our vendored schema snapshot may be stricter than a legitimately-filed
    # return). a2_item/a3_items already passed validation, so this never raises.
    for index, a2_item in enumerate(a2_items):
        validate_item(a2_item, A2_DEFINITION, itr_form, row_index=index)
    for index, item in enumerate(a3_items):
        validate_item(item, A3_DEFINITION, itr_form, row_index=index)
    _require(
        not _contains_key(target, _PROVENANCE_KEY),
        "internal error: the _ibkrFa marker leaked into the merged return",
    )

    published = ClaimedOutputCleanup(output_path)
    published.claim(_atomic_publish(output_path, _dumps_lossless(target)))
    return CbdtMergeReport(
        output_path=output_path,
        itr_form=itr_form,
        a2_rows_added=len(a2_items),
        a2_account_numbers=tuple(dict.fromkeys(our_accounts)),
        a3_rows_added=len(a3_items),
        preserved_a2_rows=len(existing_a2),
        preserved_a2_accounts=tuple(dict.fromkeys(preserved)),
        created_schedule_fa=created_schedule_fa,
        run_outputs=RunOutputs((), (published,)),
    )


def _contains_key(node: object, target_key: str) -> bool:
    """Depth-first: does ``target_key`` appear anywhere as a dict key?"""
    if isinstance(node, dict):
        if target_key in node:
            return True
        return any(_contains_key(v, target_key) for v in node.values())
    if isinstance(node, list):
        return any(_contains_key(v, target_key) for v in node)
    return False


def _atomic_publish(output_path: Path, text: str) -> FileIdentity:
    """Write ``text`` to ``output_path`` with NO-CLOBBER + NO-PARTIAL guarantees.

    The content is fully written + ``fsync``'d to a sibling staging file FIRST
    (so the destination name never names a partial file), then published with
    ``os.link`` — a single syscall that is both atomic and fails with
    ``FileExistsError`` if anything created ``output_path`` in the meantime.
    The staging file is then removed.

    A staging file that cannot be removed refuses with
    :class:`~ibkr_fa.output._cleanup.OutputRemovalError` naming it, never a
    silent leftover copy of the return:

    - before publication, from the first failure, with nothing published by
      this run at ``output_path``;
    - after publication, having attempted to remove ``output_path`` again if
      its own entry is still the staging file. A replacement, or an entry that
      cannot be checked, is kept and named as possibly not this run's. A failed
      rollback names both paths.

    Returns the identity of the file published at ``output_path``.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp"
    )
    staged = Path(tmp_name)
    try:
        fh = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        try:
            staged_stat = os.fstat(fh.fileno())
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        except BaseException:
            # Keep the first failure: a close error must not replace it.
            with contextlib.suppress(BaseException):
                fh.close()
            raise
        fh.close()
        try:
            os.link(tmp_name, output_path)
        except FileExistsError as exc:
            raise CbdtJsonMergeError(
                f"--output {output_path} was created by another process; aborting."
            ) from exc
    except BaseException as exc:
        discard_staging_file(staged, output_path, exc)
        raise
    try:
        staged.unlink(missing_ok=True)
    except OSError as exc:
        raise OutputRemovalError(
            f"the merge could not remove its staging file {staging_file_pattern(output_path)}, "
            f"a second link to the merged return ({errno_name(exc)}); "
            f"{_unpublish(output_path, staged_stat)}"
        ) from exc
    with contextlib.suppress(OSError):
        dir_fd = os.open(output_path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    return staged_stat.st_dev, staged_stat.st_ino


def _unpublish(output_path: Path, staged: os.stat_result) -> str:
    """Remove ``output_path`` if its own entry is still the ``staged`` file;
    describe what is left there."""
    try:
        entry = output_path.lstat()
    except OSError as exc:
        return (
            f"did not remove --output {output_path}, which may not be this run's "
            f"({errno_name(exc)})"
        )
    if not os.path.samestat(entry, staged):
        return f"did not remove --output {output_path}, which may not be this run's"
    try:
        output_path.unlink()
    except OSError as exc:
        return (
            f"could not remove --output {output_path} either, which still holds the "
            f"merged return ({errno_name(exc)})"
        )
    return f"removed --output {output_path}, so the merge published nothing"
