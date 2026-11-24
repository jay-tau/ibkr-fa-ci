"""Parse the IBKR Activity Statement ``Net Asset Value`` + ``Change in NAV``
sections into a :class:`~ibkr_fa.models.net_asset_value.NetAssetValueSummary`.

Feeds Schedule FA **Table A2** (issue #18): A2's "Closing balance" is the
account's ending NAV. IBKR emits the figure twice — once in ``Net Asset
Value`` (an asset-class breakdown whose ``Total`` row's ``Current Total`` is
the ending NAV) and once in ``Change in NAV`` (the ``Ending Value`` field).
This parser reads BOTH and cross-checks them, refusing (rather than guessing) a
figure for this filed, required column whenever the statement is ambiguous.

The filed figure is always one the statement prints (#906): the ``Total``
row's ``Current Total`` or, when the asset-class block has no ``Total`` row,
the ``Change in NAV`` ``Ending Value``. The asset-class sum is never filed;
it's only a check. With a ``Total`` row, the sum must match the ``Total``
within tolerance (#1287); with no asset-class rows the sum is zero, so only
a zero ``Total`` matches. Without a ``Total`` row, every asset-class row
needs a ``Current Total``, exactly one ``Ending Value`` must be printed, and
the sum must match it within tolerance. More than one ``Ending Value`` row (across
every ``Change in NAV`` block with ``Field Name`` and ``Field Value`` columns)
is ambiguous whether or not a ``Total`` row is printed.

:func:`parse_change_in_nav_fields` returns the other ``Change in NAV``
fields, with the same row checks, for the check of the income sections
that the income commands and Schedule FA run (#1279, #1324).

The ``Net Asset Value`` shape, with made-up figures (note: the splitter emits
TWO header-led blocks under this one section — the asset-class table AND a
"Time Weighted Rate of Return" block, which nothing files — so every other
block must have ``Asset Class`` and ``Current Total`` columns; and the
``Total`` line is an ordinary ``Data`` row carrying ``Asset Class ==
"Total"``, NOT a ``Total``-type row, which the splitter refuses)::

  Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,Current Total,Change
  Net Asset Value,Data,Cash ,380.12,412.506318,0,412.506318,32.386318
  Net Asset Value,Data,Stock,4975.1,5230.4,0,5230.4,255.3
  Net Asset Value,Data,Total,5355.22,5642.906318,0,5642.906318,287.686318
  Net Asset Value,Header,Time Weighted Rate of Return
  Net Asset Value,Data,4.87%

  Change in NAV,Header,Field Name,Field Value
  ...
  Change in NAV,Data,Ending Value,5642.906318

The figure is returned in the statement's BASE CURRENCY; the Schedule FA
compute layer applies the USD-only gate and the Dec-31 SBI TTBR (Rule 115).

Input the parser can't read raises :class:`~ibkr_fa.exceptions.ParseError`
(a real format change, surfaced loudly) instead of being skipped (#1065):

- a ``Change in NAV`` block without ``Field Name`` and ``Field Value``
  columns, or a ``Net Asset Value`` block other than the Time Weighted Rate
  of Return block without ``Asset Class`` and ``Current Total`` columns;
- a ``Change in NAV`` row other than ``Ending Value`` with a blank ``Field
  Name``, a blank ``Field Value`` or a ``Field Name`` an earlier row already
  used;
- an asset-class row with a blank or repeated ``Asset Class`` or a blank
  ``Current Total``, once the block yields a figure;
- a ``Current Total`` or ``Field Value`` read by the parser that isn't a
  finite decimal.

The asset-class sums and NAV cross-checks use exact decimal arithmetic; a
range or memory failure raises ``ParseError`` rather than losing precision.

A statement that contradicts itself also raises ``ParseError``, with a fixed
message telling the user to re-download or fix it (#1309): a ``Total`` its
asset-class rows don't add up to within tolerance (#1287), and a NAV figure
more than the tolerance from a single non-blank ``Ending Value``.

A missing or ambiguous figure is a soft refusal
(``closing_total=None`` + a :class:`~ibkr_fa.models.net_asset_value.NavRefusalReason`),
leaving A2 closing manual-fill with that reason (#1013). That covers a blank
or repeated ``Ending Value`` and, without a ``Total`` row, a blank
asset-class ``Current Total`` (#906).
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import MAX_PREC, Decimal, DecimalException, Inexact, localcontext
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.models.net_asset_value import NavRefusalReason, NetAssetValueSummary
from ibkr_fa.number_checks import NumberKind
from ibkr_fa.parser._cells import parse_decimal_cell
from ibkr_fa.parser.activity_statement.multi_section import Section

_SECTION: Final[str] = "Net Asset Value"
_CHANGE_SECTION: Final[str] = "Change in NAV"

_ASSET_CLASS_COL: Final[str] = "Asset Class"
_CURRENT_TOTAL_COL: Final[str] = "Current Total"
_TOTAL_ROW_LABEL: Final[str] = "Total"
_TWR_COL: Final[str] = "Time Weighted Rate of Return"

_FIELD_NAME_COL: Final[str] = "Field Name"
_FIELD_VALUE_COL: Final[str] = "Field Value"
_ENDING_VALUE_LABEL: Final[str] = "Ending Value"

# NAV "Total" and Change-in-NAV "Ending Value" are the same figure to the cent
# in real statements; allow a one-paisa rounding slack before declaring a
# disagreement.
_CROSS_CHECK_TOLERANCE: Final[Decimal] = Decimal("0.01")

# A statement that contradicts itself stops the run (#1309): there's no
# figure to leave for manual fill that the statement itself agrees on.
_CONTRADICTION_REMEDY: Final[str] = (
    "The statement contradicts itself, so Schedule FA Table A2's closing balance can't be "
    "filed. Re-download the Activity Statement from IBKR or fix the file, then re-run."
)
_TOTAL_BREAKDOWN_MISMATCH: Final[str] = (
    "the asset-class rows' Current Total amounts don't add up to this Total row's Current "
    "Total within 0.01, or there are no asset-class rows and the Total isn't zero. "
    + _CONTRADICTION_REMEDY
)
_ENDING_VALUE_MISMATCH: Final[str] = (
    "this Ending Value differs by more than 0.01 from the Net Asset Value figure (its Total "
    "row's Current Total or, with no Total row, the sum of its asset-class rows). "
    + _CONTRADICTION_REMEDY
)


def _parse_nav_decimal(raw: str, *, section: str, column: str, row_index: int) -> Decimal:
    """Parse a finite Decimal, raising :class:`ParseError` on malformed input.

    The error carries no cell value, in its message or ``raw_value``: a
    shifted cell can hold the holder's name or account number.
    """
    return parse_decimal_cell(
        raw, section=section, row_index=row_index, column=column, kind=NumberKind.MONEY
    )


def _require_columns(block: Section, *, section: str, columns: tuple[str, str]) -> None:
    """Raise :class:`ParseError` unless ``block``'s header has every one of ``columns``.

    The error names the header's file line and the first missing column.
    """
    for column in columns:
        if column not in block.columns:
            raise ParseError(
                section=section,
                row_index=block.header_line,
                column=column,
                message=(
                    f"the header on line {block.header_line} has no {column!r} "
                    f"column, so the block's rows can't be read; refusing rather "
                    f"than skipping the block. IBKR may have changed the "
                    f"section's format."
                ),
                raw_value="",
            )


def _row_error(*, section: str, row_index: int, column: str, message: str) -> ParseError:
    """A refusal naming a row's index in its block and a column, with no cell value."""
    return ParseError(
        section=section, row_index=row_index, column=column, message=message, raw_value=""
    )


def _parse_change_in_nav(
    change_in_nav_blocks: list[Section] | None,
) -> tuple[list[Decimal | None], dict[str, Decimal]]:
    """Return the ``Change in NAV`` section's ``Ending Value`` rows and its other fields.

    Every block must have ``Field Name`` and ``Field Value`` columns. The
    first result has one entry per ``Ending Value`` row, in file order,
    with ``None`` for a blank ``Field Value``; it is empty when the section
    (or the row) is absent. The caller refuses more than one, or a blank
    one (#906). The second maps every other row's stripped ``Field Name``
    to its ``Field Value``.

    Raises:
        ParseError: When a block lacks either column; when a row other than
            ``Ending Value`` has a blank ``Field Name`` or ``Field Value``, or
            repeats an earlier row's ``Field Name`` in any block; or when a
            ``Field Value`` is present but not a finite decimal.
    """
    fields: dict[str, Decimal] = {}
    ending_values: list[Decimal | None] = []
    for block in change_in_nav_blocks or []:
        _require_columns(
            block, section=_CHANGE_SECTION, columns=(_FIELD_NAME_COL, _FIELD_VALUE_COL)
        )
        for row_index, row in enumerate(block.rows):
            name = row.get(_FIELD_NAME_COL, "").strip()
            raw = row.get(_FIELD_VALUE_COL, "")
            if name == _ENDING_VALUE_LABEL:
                # A blank or repeated Ending Value is the caller's soft refusal (#906).
                ending = None if raw.strip() == "" else _parse_change_value(raw, row_index)
                ending_values.append(ending)
                continue
            if name == "":
                raise _row_error(
                    section=_CHANGE_SECTION,
                    row_index=row_index,
                    column=_FIELD_NAME_COL,
                    message="must not be blank",
                )
            if name in fields:
                raise _row_error(
                    section=_CHANGE_SECTION,
                    row_index=row_index,
                    column=_FIELD_NAME_COL,
                    message=(
                        "repeats an earlier row's Field Name; refusing rather "
                        "than overwriting the earlier amount"
                    ),
                )
            if raw.strip() == "":
                raise _row_error(
                    section=_CHANGE_SECTION,
                    row_index=row_index,
                    column=_FIELD_VALUE_COL,
                    message="must not be blank",
                )
            fields[name] = _parse_change_value(raw, row_index)
    return ending_values, fields


def _parse_change_value(raw: str, row_index: int) -> Decimal:
    return _parse_nav_decimal(
        raw, section=_CHANGE_SECTION, column=_FIELD_VALUE_COL, row_index=row_index
    )


def parse_change_in_nav_fields(change_in_nav_blocks: list[Section]) -> dict[str, Decimal]:
    """Every ``Change in NAV`` field other than ``Ending Value``, by stripped ``Field Name``.

    The income commands and Schedule FA read the income fields from this
    (#1279, #1324). The rows get the checks :func:`parse_net_asset_value`
    gives them, ``Ending Value`` included; ``Net Asset Value`` isn't read.

    Raises:
        ParseError: As :func:`parse_net_asset_value` does for a ``Change in
            NAV`` block or row.
    """
    return _parse_change_in_nav(change_in_nav_blocks)[1]


def _refuse(reason: NavRefusalReason) -> NetAssetValueSummary:
    return NetAssetValueSummary(closing_total=None, refusal_reason=reason, cross_checked=False)


def _nav_total_current(block: Section) -> tuple[Decimal, bool] | NetAssetValueSummary:
    """Return ``(figure, printed)`` from the asset-class block, or a refusal summary.

    With an ``Asset Class == "Total"`` row, ``figure`` is its ``Current
    Total`` and ``printed`` is ``True``: the figure that's filed, once the
    caller has checked it against the asset-class rows (#1287) and refused
    a disagreement (#1309). Without
    one, ``figure`` is the sum of every asset-class row's ``Current Total``
    and ``printed`` is ``False``: it's only a check on the ``Change in NAV``
    ``Ending Value``, which is filed instead (#906), and which the caller
    refuses when they disagree (#1309). A blank ``Current
    Total`` on the ``Total`` row, or on any asset-class row when there's no
    ``Total`` row, is a soft refusal; a non-numeric one raises ParseError.
    """
    total_rows = [
        (row_index, row)
        for row_index, row in enumerate(block.rows)
        if row.get(_ASSET_CLASS_COL, "").strip() == _TOTAL_ROW_LABEL
    ]
    if len(total_rows) > 1:
        return _refuse(NavRefusalReason.MULTIPLE_TOTAL_ROWS)
    if len(total_rows) == 1:
        [(total_index, total_row)] = total_rows
        raw = total_row.get(_CURRENT_TOTAL_COL, "")
        if raw.strip() == "":
            return _refuse(NavRefusalReason.BLANK_TOTAL)
        return (
            _parse_nav_decimal(
                raw, section=_SECTION, column=_CURRENT_TOTAL_COL, row_index=total_index
            ),
            True,
        )
    if not block.rows:
        return _refuse(NavRefusalReason.NO_ASSET_CLASS_ROWS)
    row_sum = Decimal("0")
    blank = False
    with localcontext(prec=MAX_PREC) as context:
        context.traps[Inexact] = True
        for row_index, row in enumerate(block.rows):
            raw = row.get(_CURRENT_TOTAL_COL, "")
            if raw.strip() == "":
                blank = True
                continue
            try:
                row_sum += _parse_nav_decimal(
                    raw, section=_SECTION, column=_CURRENT_TOTAL_COL, row_index=row_index
                )
            except (DecimalException, MemoryError) as exc:
                raise _row_error(
                    section=_SECTION,
                    row_index=row_index,
                    column=_CURRENT_TOTAL_COL,
                    message="the asset-class amounts cannot be added exactly",
                ) from exc
    if blank:
        return _refuse(NavRefusalReason.BLANK_ASSET_CLASS_AMOUNT)
    return row_sum, False


def _check_total_against_breakdown(
    block: Section, total: Decimal, breakdown: Mapping[str, Decimal]
) -> None:
    """Refuse a printed ``Total`` that differs from the exact sum of the asset-class rows.

    More than the 0.01 tolerance apart is a disagreement (#1309). With no
    asset-class rows the parts add up to zero, and only a zero ``Total``
    agrees with them: there's no rounding to allow for.

    Raises:
        ParseError: Naming the ``Total`` row, when the statement contradicts
            itself this way, or on a range or memory failure in the exact sum
            or comparison.
    """
    total_index = next(
        row_index
        for row_index, row in enumerate(block.rows)
        if row.get(_ASSET_CLASS_COL, "").strip() == _TOTAL_ROW_LABEL
    )
    if not breakdown:
        disagrees = total != 0
    else:
        try:
            with localcontext(prec=MAX_PREC) as context:
                context.traps[Inexact] = True
                disagrees = (
                    abs(total - sum(breakdown.values(), Decimal(0))) > _CROSS_CHECK_TOLERANCE
                )
        except (DecimalException, MemoryError) as exc:
            raise _row_error(
                section=_SECTION,
                row_index=total_index,
                column=_CURRENT_TOTAL_COL,
                message="the Total and the asset-class amounts cannot be compared exactly",
            ) from exc
    if disagrees:
        raise _row_error(
            section=_SECTION,
            row_index=total_index,
            column=_CURRENT_TOTAL_COL,
            message=_TOTAL_BREAKDOWN_MISMATCH,
        )


def _ending_value_refusal(
    ending_values: list[Decimal | None], *, printed: bool
) -> NavRefusalReason | None:
    """Why the ``Change in NAV`` ``Ending Value`` rows can't confirm the NAV figure, or ``None``.

    ``printed`` says the NAV figure is the ``Total`` row's, which stands
    alone when no ``Ending Value`` is printed. Otherwise it's the asset-class
    sum, and the single ``Ending Value`` it matches is what's filed (#906).
    A single non-blank ``Ending Value`` gets no reason here: the caller
    compares it with the NAV figure, and refuses a contradiction (#1309).
    """
    if len(ending_values) > 1:
        return NavRefusalReason.MULTIPLE_ENDING_VALUES
    if not ending_values:
        return None if printed else NavRefusalReason.NO_ENDING_VALUE
    [ending] = ending_values
    if ending is None:
        return NavRefusalReason.BLANK_ENDING_VALUE
    return None


def _check_ending_value(
    change_in_nav_blocks: list[Section] | None, nav_total: Decimal, ending: Decimal
) -> None:
    """Refuse an ``Ending Value`` more than the 0.01 tolerance from the NAV figure (#1309).

    Raises:
        ParseError: Naming the ``Ending Value`` row, when the statement
            contradicts itself this way, or on a range or memory failure in
            the exact comparison.
    """
    ending_row_index = next(
        row_index
        for block in change_in_nav_blocks or []
        for row_index, row in enumerate(block.rows)
        if row.get(_FIELD_NAME_COL, "").strip() == _ENDING_VALUE_LABEL
    )
    try:
        with localcontext(prec=MAX_PREC) as context:
            context.traps[Inexact] = True
            disagrees = abs(nav_total - ending) > _CROSS_CHECK_TOLERANCE
    except (DecimalException, MemoryError) as exc:
        raise _row_error(
            section=_CHANGE_SECTION,
            row_index=ending_row_index,
            column=_FIELD_VALUE_COL,
            message="the NAV and Ending Value cannot be cross-checked exactly",
        ) from exc
    if disagrees:
        raise _row_error(
            section=_CHANGE_SECTION,
            row_index=ending_row_index,
            column=_FIELD_VALUE_COL,
            message=_ENDING_VALUE_MISMATCH,
        )


def parse_net_asset_value(
    nav_blocks: list[Section] | None,
    change_in_nav_blocks: list[Section] | None,
) -> NetAssetValueSummary:
    """Determine the account's ending NAV for Schedule FA Table A2.

    Args:
        nav_blocks: The ``Net Asset Value`` section's blocks from
            :func:`~ibkr_fa.parser.activity_statement.multi_section.split_activity_statement_sections`
            (``None`` / empty when the section is absent).
        change_in_nav_blocks: The ``Change in NAV`` section's blocks (``None``
            / empty when absent) — used to cross-check the NAV total, and
            filed in its place when there's no ``Total`` row.

    Returns:
        A :class:`NetAssetValueSummary`. ``closing_total`` is populated only
        when a single asset-class block yields a printed figure: its
        ``Total`` row's ``Current Total``, which the asset-class rows'
        ``Current Total`` must sum to within tolerance, cross-checked
        against the ``Change in NAV`` ``Ending Value`` when one is printed;
        or, without a ``Total`` row, the single ``Ending Value``, which the
        asset-class rows' ``Current Total`` must sum to within tolerance.
        When the figure is missing or ambiguous, ``closing_total`` is
        ``None`` with a ``refusal_reason``.

    Raises:
        ParseError: On input the parser can't read (a real upstream format
            change): a block without the columns it needs; a ``Change in
            NAV`` row other than ``Ending Value`` with a blank or repeated
            ``Field Name`` or a blank ``Field Value``; once the
            asset-class block yields a figure, a row with a blank or repeated
            ``Asset Class`` or a blank ``Current Total``; or a figure read by
            the parser that isn't a finite decimal; or a range or memory failure in
            the exact NAV sums or cross-checks. Also when the statement
            contradicts itself (#1309): a ``Total`` its asset-class rows
            don't add up to within tolerance, naming the ``Total`` row, or a
            NAV figure more than the tolerance from a single non-blank
            ``Ending Value``, naming that row. The error names the header's file
            line, or the row's index in its block, and the column, and
            carries no cell value, in its message or ``raw_value``: a shifted
            cell can hold the holder's name or account number.
    """
    ending_values, _fields = _parse_change_in_nav(change_in_nav_blocks)
    change_ending = ending_values[0] if len(ending_values) == 1 else None

    asset_class_blocks: list[Section] = []
    for block in nav_blocks or []:
        if block.columns == (_TWR_COL,):
            continue  # a return percentage, which nothing files
        _require_columns(block, section=_SECTION, columns=(_ASSET_CLASS_COL, _CURRENT_TOTAL_COL))
        asset_class_blocks.append(block)
    if not asset_class_blocks:
        return _refuse(NavRefusalReason.NO_ASSET_CLASS_BLOCK)
    if len(asset_class_blocks) > 1:
        return _refuse(NavRefusalReason.MULTIPLE_ASSET_CLASS_BLOCKS)

    nav_figure = _nav_total_current(asset_class_blocks[0])
    if isinstance(nav_figure, NetAssetValueSummary):
        return nav_figure  # a refusal
    nav_total, printed = nav_figure
    breakdown = _asset_class_breakdown(asset_class_blocks[0])
    if printed:
        _check_total_against_breakdown(asset_class_blocks[0], nav_total, breakdown)
    reason = _ending_value_refusal(ending_values, printed=printed)
    if reason is not None:
        return _refuse(reason)
    if change_ending is not None:
        _check_ending_value(change_in_nav_blocks, nav_total, change_ending)

    return NetAssetValueSummary(
        closing_total=nav_total if printed else change_ending,
        refusal_reason=None,
        cross_checked=change_ending is not None,
        asset_class_current_totals=breakdown,
    )


def _asset_class_breakdown(block: Section) -> dict[str, Decimal]:
    """Each asset-class row's ``Current Total`` by its stripped ``Asset Class``.

    For example ``{"Cash": …, "Stock": …}``. The ``Total`` row is left out:
    :func:`_nav_total_current` reads it.

    Raises:
        ParseError: When a row's ``Asset Class`` is blank or repeats an
            earlier row's, or its ``Current Total`` is blank or not a finite
            decimal.
    """
    breakdown: dict[str, Decimal] = {}
    for row_index, row in enumerate(block.rows):
        asset_class = row.get(_ASSET_CLASS_COL, "").strip()
        if asset_class == _TOTAL_ROW_LABEL:
            continue
        if asset_class == "":
            raise _row_error(
                section=_SECTION,
                row_index=row_index,
                column=_ASSET_CLASS_COL,
                message="must not be blank",
            )
        if asset_class in breakdown:
            raise _row_error(
                section=_SECTION,
                row_index=row_index,
                column=_ASSET_CLASS_COL,
                message=(
                    "repeats an earlier row's Asset Class; refusing rather "
                    "than overwriting the earlier Current Total"
                ),
            )
        raw = row.get(_CURRENT_TOTAL_COL, "")
        if raw.strip() == "":
            raise _row_error(
                section=_SECTION,
                row_index=row_index,
                column=_CURRENT_TOTAL_COL,
                message="must not be blank",
            )
        breakdown[asset_class] = _parse_nav_decimal(
            raw, section=_SECTION, column=_CURRENT_TOTAL_COL, row_index=row_index
        )
    return breakdown
