"""Load + validate against the vendored CBDT ITR-2/ITR-3 JSON Schemas.

ibkr-fa emits a Schedule FA JSON *fragment* (Table A2
``DtlsForeignCustodialAcc`` + Table A3 ``DtlsForeignEquityDebtInterest``)
for import into the official offline ITR-2 or ITR-3 utility. Because the JSON import
bypasses the human review step the CSV/xlsx flow relies on, every emitted
item is validated against the AUTHORITATIVE CBDT schema before it is
written — a wrong ``maxLength`` / ``enum`` / ``type`` / date would otherwise
become filed tax data the utility silently rejects or mis-imports.

The full ITR-2 schema and a validation-equivalent ITR-3 Schedule FA extract
are pinned by sha256 (see the ``.source`` sidecars and
:func:`schema_sha256`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from functools import lru_cache
from importlib import resources
from types import MappingProxyType
from typing import Any, Final

import jsonschema
from jsonschema.protocols import Validator

from ibkr_fa.compliance.itr_form_selection import ITR_FORMS, ItrForm
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.models.fa_a2_row import FaA2NatureOfAmount

CbdtItrForm = ItrForm

# Pinned identities of the vendored schemas. Keep the ITR-2 alias for callers
# and old fragments created before ITR-3 support.
CBDT_ITR_FORMS: Final[tuple[CbdtItrForm, ...]] = ITR_FORMS
CBDT_SCHEMA_VERSION: Final[str] = "ITR-2 AY2026-27 Main V1.2 (2026-08-13)"
CBDT_SCHEMA_VERSIONS: Final[dict[CbdtItrForm, str]] = {
    "ITR-2": CBDT_SCHEMA_VERSION,
    "ITR-3": "ITR-3 AY2026-27 Main V1.1 Schedule FA",
}
CBDT_SCHEMA_ASSESSMENT_YEAR_START: Final[int] = 2026

_SCHEMA_PACKAGE: Final[str] = "ibkr_fa.reference"
_SCHEMA_RESOURCES: Final[dict[CbdtItrForm, str]] = {
    "ITR-2": "cbdt_itr2_schema_ay2026_27.json",
    "ITR-3": "cbdt_itr3_schedule_fa_schema_ay2026_27.json",
}

# JSON-pointer definition names of the two ScheduleFA tables ibkr-fa emits.
A2_DEFINITION: Final[str] = "DtlsForeignCustodialAcc"
A3_DEFINITION: Final[str] = "DtlsForeignEquityDebtInterest"

# Table A2 "Nature of amount" -> the schema's single-letter enum, which the
# portal-template CSV files too.
A2_NATURE_LETTERS: Final[Mapping[FaA2NatureOfAmount, str]] = MappingProxyType(
    {
        FaA2NatureOfAmount.INTEREST: "I",
        FaA2NatureOfAmount.DIVIDEND: "D",
        FaA2NatureOfAmount.PROCEEDS: "S",
        FaA2NatureOfAmount.OTHER_INCOME: "O",
        FaA2NatureOfAmount.NO_AMOUNT: "N",
    }
)


# The Schedule FA table each emitted definition fills.
_TABLES: Final[Mapping[str, str]] = MappingProxyType({A2_DEFINITION: "A2", A3_DEFINITION: "A3"})


class CbdtSchemaError(IbkrFaError):
    """A Schedule FA item failed validation against the vendored CBDT schema.

    Names where and which rule, never the value (#1048): the item's table and
    its 0-based ``row_index`` in table order, the schema ``field`` (``None``
    when the failure is about the item as a whole) and the schema ``keyword``,
    such as ``maxLength`` or ``required``. jsonschema's own message, which
    quotes the value, is never used.
    """

    def __init__(
        self,
        *,
        itr_form: CbdtItrForm,
        definition: str,
        row_index: int,
        field: str | None,
        keyword: str,
    ) -> None:
        self.itr_form = itr_form
        self.definition = definition
        self.row_index = row_index
        self.field = field
        self.keyword = keyword
        super().__init__(
            f"{itr_form} Schedule FA Table {_TABLES[definition]} ({definition}) item at "
            f"index {row_index} failed CBDT schema validation: {self.rule_text()}"
        )

    def rule_text(self) -> str:
        """Which field broke which schema rule, as fixed text."""
        where = "the item" if self.field is None else f"field {self.field!r}"
        return f"{where} breaks the schema's {self.keyword!r} rule"


def schema_version_for(itr_form: CbdtItrForm) -> str:
    return CBDT_SCHEMA_VERSIONS[itr_form]


def _schema_bytes(itr_form: CbdtItrForm) -> bytes:
    return (resources.files(_SCHEMA_PACKAGE) / _SCHEMA_RESOURCES[itr_form]).read_bytes()


@lru_cache(maxsize=2)
def schema_sha256(itr_form: CbdtItrForm = "ITR-2") -> str:
    """SHA-256 of the vendored schema file (pins it against silent drift)."""
    return hashlib.sha256(_schema_bytes(itr_form)).hexdigest()


@lru_cache(maxsize=2)
def _full_schema(itr_form: CbdtItrForm) -> dict[str, Any]:
    return json.loads(_schema_bytes(itr_form))


def _item_subschema(
    definition: str, itr_form: CbdtItrForm, *, check_required: bool = True
) -> dict[str, Any]:
    """A standalone schema validating ONE ScheduleFA item against the named
    ``#/definitions`` entry, carrying the full ``definitions`` block so its
    internal ``$ref``s (country code, dates) resolve locally. Without
    ``check_required``, the entry's ``required`` list is dropped."""
    definitions = _full_schema(itr_form)["definitions"]
    if not check_required:
        entry = {key: rule for key, rule in definitions[definition].items() if key != "required"}
        definitions = {**definitions, definition: entry}
    return {
        "allOf": [{"$ref": f"#/definitions/{definition}"}],
        "definitions": definitions,
    }


def require_validator(
    definition: str, itr_form: CbdtItrForm = "ITR-2", *, check_required: bool = True
) -> Validator:
    """Build a Draft-4 validator for one ScheduleFA item subschema.

    The ``format`` checker is enabled defensively, but note this schema
    enforces dates via an explicit ``pattern`` (``YYYY-MM-DD``), not a
    ``format`` keyword (it has none) — so date validity rests on the pattern.
    The pattern is unanchored (CBDT's own), so it matches a valid date
    *substring*; the emitter (PR3) must therefore feed ``date.isoformat()``
    from a :class:`datetime.date`, never a stringified ``datetime``.

    """
    return jsonschema.Draft4Validator(
        _item_subschema(definition, itr_form, check_required=check_required),
        format_checker=jsonschema.Draft4Validator.FORMAT_CHECKER,
    )


def validate_item(
    item: dict[str, Any],
    definition: str,
    itr_form: CbdtItrForm = "ITR-2",
    *,
    row_index: int,
    check_required: bool = True,
) -> None:
    """Validate one ScheduleFA item, ``row_index`` (0-based) of its table,
    against its CBDT subschema; raise on the FIRST failure, naming the field
    and the schema keyword (see :class:`CbdtSchemaError`). With
    ``check_required=False`` an absent field is not a failure, so a caller can
    check the values it has before reporting the ones it lacks."""
    validator = require_validator(definition, itr_form, check_required=check_required)
    errors = sorted(validator.iter_errors(item), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        if first.absolute_path:
            # The schema forbids additional properties, so this is a schema field.
            field = str(first.absolute_path[0])
        elif first.validator == "required":
            # The missing field comes from the schema's list, not the message.
            field = next(name for name in first.validator_value if name not in first.instance)
        else:
            field = None
        raise CbdtSchemaError(
            itr_form=itr_form,
            definition=definition,
            row_index=row_index,
            field=field,
            keyword=str(first.validator),
        )
