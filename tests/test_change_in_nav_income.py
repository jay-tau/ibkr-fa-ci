"""The income commands check the income sections against ``Change in NAV`` (#1279).

IBKR leaves an empty section out of the export, so a missing ``Dividends``,
``Withholding Tax`` or ``Interest`` section reads as no income. When the
statement prints ``Change in NAV``, each of its ``Dividends``, ``Withholding
Tax`` and ``Interest`` fields (and ``Payment In Lieu Of Dividends``, when
printed) must agree with its section to within 0.01: ``schedule-os``,
``schedule-fsi``, ``schedule-tr``, ``form-67`` and ``run-all`` exit 4 and
leave no output otherwise. ``schedule-cg`` and ``schedule-cg-ltcg`` don't
read income and are unchanged. A statement that passes these checks files
exactly what it files without ``Change in NAV``. Every amount here is synthetic, and
no refusal prints one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa import pipeline_dispatch
from ibkr_fa.cli import main
from ibkr_fa.exceptions import ParseError
from ibkr_fa.models import SourceBundle, StatementPeriod, WithholdingTax
from ibkr_fa.parser import _exact
from ibkr_fa.parser.activity_statement import KnownActivitySection, Section, change_in_nav_income
from ibkr_fa.parser.activity_statement.change_in_nav_income import ChangeInNavIncomeError
from ibkr_fa.pipeline_dispatch import (
    _OS_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
    _dispatch_activity_statement,
)
from tests._change_in_nav_checks import CHANGE_IN_NAV_SECTION_FIELDS as _SECTION_FIELDS
from tests._change_in_nav_checks import CHANGE_IN_NAV_USER_TOML as _CONFIG
from tests._change_in_nav_checks import DIVIDENDS_SECTION as _DIVIDENDS
from tests._change_in_nav_checks import EUR_DEBIT_INTEREST_ROW as _DEBIT_ROW
from tests._change_in_nav_checks import INTEREST_SECTION as _INTEREST
from tests._change_in_nav_checks import MISSING_SECTION_CASES as _MISSING
from tests._change_in_nav_checks import RE_EXPORT_HINT as _RE_EXPORT
from tests._change_in_nav_checks import WITHHOLDING_TAX_SECTION as _WITHHOLDING
from tests._change_in_nav_checks import absent_section_refusal as _absent
from tests._change_in_nav_checks import assert_no_amount as _assert_no_amount
from tests._change_in_nav_checks import assert_private_error as _assert_private_error
from tests._change_in_nav_checks import change_in_nav_differs as _differs
from tests._change_in_nav_checks import other_currency_refusal as _other_currency
from tests._crash_reports import assert_unchained_refusal
from tests._income_blocks import with_totals
from tests._number_limits import MONEY

_FY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
)
_FY = ("--financial-year", "2025-26")
_ITR = ("--itr-form", "ITR-2")

_IN_LIEU_ROW = (
    "Dividends,Data,USD,15-10-2025,"
    "XQBA(US99995C1027) Payment In Lieu Of Dividend USD 0.23 per Share,4.31\n"
)


def _base() -> str:
    """The FY 2025-26 fixture (Statement, Account Information, Trades) without
    its income sections."""
    lines = _FY_FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith(("Dividends,", "Withholding Tax,"))]
    assert len(kept) < len(lines)
    return "".join(kept)


def _change_in_nav(*fields: tuple[str, str]) -> str:
    """A ``Net Asset Value`` block and a ``Change in NAV`` block printing ``fields``."""
    return (
        "Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,"
        "Current Total,Change\n"
        "Net Asset Value,Data,Cash,0,11.71,0,11.71,11.71\n"
        "Net Asset Value,Data,Total,0,11.71,0,11.71,11.71\n"
        "Change in NAV,Header,Field Name,Field Value\n"
        "Change in NAV,Data,Starting Value,0\n"
        + "".join(f"Change in NAV,Data,{label},{value}\n" for label, value in fields)
        + "Change in NAV,Data,Ending Value,11.71\n"
    )


def _write(run_dir: Path, *blocks: str, base: str | None = None) -> tuple[Path, Path]:
    """Write the statement (``base`` or :func:`_base`, then ``blocks``, with the
    printed totals) and the config into ``run_dir``."""
    run_dir.mkdir(parents=True, exist_ok=True)
    statement = run_dir / "statement.csv"
    text = (_base() if base is None else base) + "".join(blocks)
    statement.write_text(with_totals(text), encoding="utf-8")
    config = run_dir / "user.toml"
    config.write_text(_CONFIG, encoding="utf-8")
    return statement, config


# ---- CLI: a missing section refuses ------------------------------------------

_REFUSING = [
    pytest.param("schedule-os", (), ("schedule-os.csv",), id="schedule-os"),
    pytest.param("schedule-fsi", (), ("schedule-fsi.csv",), id="schedule-fsi"),
    pytest.param("schedule-tr", _ITR, ("schedule-tr.csv",), id="schedule-tr"),
    pytest.param("form-67", (), ("form-67.csv", "form-67-debug.csv"), id="form-67"),
]


def _stale(out: Path, names: tuple[str, ...]) -> None:
    out.mkdir()
    for name in names:
        (out / name).write_bytes(b"STALE")


@pytest.mark.parametrize("case", sorted(_MISSING))
@pytest.mark.parametrize(("command", "extra", "targets"), _REFUSING)
def test_income_command_refuses_a_field_whose_section_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
    command: str,
    extra: tuple[str, ...],
    targets: tuple[str, ...],
) -> None:
    fields, message = _MISSING[case]
    statement, config = _write(tmp_path, _change_in_nav(*fields))
    out = tmp_path / "out"
    _stale(out, targets)

    rc = main(
        [
            command,
            *("--input", str(statement), "--config", str(config)),
            *("--output", str(out / targets[0])),
            *_FY,
            *extra,
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {message}\n"
    _assert_no_amount(captured.err, "11.71", *(value for _, value in fields))
    assert list(out.iterdir()) == []


_RUN_ALL_OUTPUTS = (
    "schedule-os.csv",
    "schedule-cg.csv",
    "schedule-cg-ltcg.csv",
    "schedule-fsi.csv",
    "schedule-tr.csv",
    "form-67.csv",
    "form-67-debug.csv",
)


@pytest.mark.parametrize("case", sorted(_MISSING))
def test_run_all_refuses_a_field_whose_section_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    fields, message = _MISSING[case]
    statement, config = _write(tmp_path, _change_in_nav(*fields))
    out = tmp_path / "out"
    _stale(out, _RUN_ALL_OUTPUTS)

    rc = main(
        [
            "run-all",
            *("--fy-input", str(statement), "--config", str(config)),
            *("--output-dir", str(out)),
            *_FY,
            *_ITR,
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: run-all: {message}\n"
    _assert_no_amount(captured.err, "11.71", *(value for _, value in fields))
    assert list(out.iterdir()) == []


# ---- CLI: matching statements file what they file today ----------------------

_ALL_COMMANDS = [
    pytest.param("schedule-os", (), "schedule-os.csv", id="schedule-os"),
    pytest.param("schedule-cg", _ITR, "schedule-cg.csv", id="schedule-cg"),
    pytest.param("schedule-cg-ltcg", _ITR, "schedule-cg-ltcg.csv", id="schedule-cg-ltcg"),
    pytest.param("schedule-fsi", (), "schedule-fsi.csv", id="schedule-fsi"),
    pytest.param("schedule-tr", _ITR, "schedule-tr.csv", id="schedule-tr"),
    pytest.param("form-67", (), "form-67.csv", id="form-67"),
]


def _run(
    run_dir: Path,
    capsys: pytest.CaptureFixture[str],
    blocks: tuple[str, ...],
    argv: Callable[[Path, Path, Path], list[str]],
) -> tuple[int, str, str, dict[str, bytes]]:
    """Run ``argv(statement, config, out)`` on ``blocks``; return its exit, its
    stdout and stderr with ``run_dir`` masked, and its output files."""
    statement, config = _write(run_dir, *blocks)
    out = run_dir / "out"
    out.mkdir()
    capsys.readouterr()
    rc = main(argv(statement, config, out))
    captured = capsys.readouterr()
    files = {path.name: path.read_bytes() for path in sorted(out.iterdir())}
    return (
        rc,
        captured.out.replace(str(run_dir), "<run>"),
        captured.err.replace(str(run_dir), "<run>"),
        files,
    )


def _command(command: str, extra: tuple[str, ...], target: str) -> Callable[..., list[str]]:
    def argv(statement: Path, config: Path, out: Path) -> list[str]:
        return [
            command,
            *("--input", str(statement), "--config", str(config)),
            *("--output", str(out / target)),
            *_FY,
            *extra,
        ]

    return argv


def _run_all(statement: Path, config: Path, out: Path) -> list[str]:
    return [
        "run-all",
        *("--fy-input", str(statement), "--config", str(config)),
        *("--output-dir", str(out)),
        *_FY,
        *_ITR,
    ]


_SECTIONS = (_DIVIDENDS, _WITHHOLDING, _INTEREST)


@pytest.mark.parametrize(("command", "extra", "target"), _ALL_COMMANDS)
def test_matching_change_in_nav_changes_no_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    extra: tuple[str, ...],
    target: str,
) -> None:
    argv = _command(command, extra, target)
    baseline = _run(tmp_path / "without", capsys, _SECTIONS, argv)
    assert baseline[0] in (0, 3)
    assert baseline[3]

    matching = _run(tmp_path / "with", capsys, (*_SECTIONS, _change_in_nav(*_SECTION_FIELDS)), argv)

    assert matching == baseline


def test_run_all_with_matching_change_in_nav_changes_no_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = _run(tmp_path / "without", capsys, _SECTIONS, _run_all)
    assert baseline[0] in (0, 3)
    assert {"schedule-os.csv", "schedule-fsi.csv", "schedule-tr.csv"} <= set(baseline[3])

    matching = _run(
        tmp_path / "with", capsys, (*_SECTIONS, _change_in_nav(*_SECTION_FIELDS)), _run_all
    )

    assert matching == baseline


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param(
            "Net Asset Value,Data,Cash,0,11.71,0,11.71,11.71\n",
            "Net Asset Value,Data,Cash,0,50.00,0,50.00,50.00\n",
            id="rows-disagree-with-total",
        ),
        pytest.param(
            "Change in NAV,Data,Ending Value,11.71\n",
            "Change in NAV,Data,Ending Value,14.62\n",
            id="ending-value-disagrees-with-total",
        ),
    ],
)
@pytest.mark.parametrize(("command", "extra", "target"), _ALL_COMMANDS)
def test_nav_figures_that_disagree_change_no_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    old: str,
    new: str,
    command: str,
    extra: tuple[str, ...],
    target: str,
) -> None:
    """#1309: only Schedule FA reads the Net Asset Value figures, so these
    commands run on a statement whose figures contradict each other."""
    argv = _command(command, extra, target)
    nav = _change_in_nav(*_SECTION_FIELDS)
    assert nav.count(old) == 1
    baseline = _run(tmp_path / "agreeing", capsys, (*_SECTIONS, nav), argv)
    assert baseline[0] in (0, 3)
    assert baseline[3]

    disagreeing = _run(tmp_path / "disagreeing", capsys, (*_SECTIONS, nav.replace(old, new)), argv)

    assert disagreeing == baseline


@pytest.mark.parametrize("command", ["schedule-cg", "schedule-cg-ltcg"])
def test_capital_gains_commands_ignore_change_in_nav(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    """The repro (all three fields, no income section) changes no CG file."""
    argv = _command(command, _ITR, f"{command}.csv")
    baseline = _run(tmp_path / "without", capsys, (), argv)
    assert baseline[0] == 0
    assert baseline[3]

    assert _run(tmp_path / "repro", capsys, (_change_in_nav(*_SECTION_FIELDS),), argv) == baseline


# ---- CLI: a present section must agree to the cent ---------------------------


@pytest.mark.parametrize(
    ("label", "section", "total"),
    [
        pytest.param("Dividends", _DIVIDENDS, Decimal("9.87"), id="dividends"),
        pytest.param("Withholding Tax", _WITHHOLDING, Decimal("-2.47"), id="withholding"),
        pytest.param("Interest", _INTEREST, Decimal("4.31"), id="interest"),
    ],
)
@pytest.mark.parametrize(
    ("delta", "refuses"),
    [
        ("0.01", False),
        ("-0.01", False),
        ("0.02", True),
        ("-0.02", True),
    ],
)
def test_a_present_section_must_agree_to_the_cent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    label: str,
    section: str,
    total: Decimal,
    delta: str,
    refuses: bool,
) -> None:
    argv = _command("schedule-os", (), "schedule-os.csv")
    field = str(total + Decimal(delta))
    result = _run(tmp_path / "with", capsys, (section, _change_in_nav((label, field))), argv)

    if refuses:
        assert result == (4, "", f"error: {_differs(label, f'the {label!r} section')}\n", {})
        amounts = (str(total), field, "11.71")
        if label != "Interest":
            amounts += ("0.47",)
        _assert_no_amount(result[2], *amounts)
    else:
        assert result == _run(tmp_path / "without", capsys, (section,), argv)
        assert result[0] == 0


# ---- The check itself, through the dispatcher --------------------------------


def _dispatch(tmp_path: Path, *blocks: str, base: str | None = None) -> SourceBundle:
    statement, _config = _write(tmp_path, *blocks, base=base)
    return _dispatch_activity_statement(
        statement,
        period_validator=lambda _period: None,
        required_sections=_OS_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
        check_income_against_nav=True,
    )


def _refusal(
    tmp_path: Path, *blocks: str, amounts: tuple[str, ...], base: str | None = None
) -> ChangeInNavIncomeError:
    with pytest.raises(ChangeInNavIncomeError) as excinfo:
        _dispatch(tmp_path, *blocks, base=base)
    error = excinfo.value
    _assert_private_error(error, "11.71", *amounts)
    return error


_IN_LIEU = "Payment In Lieu Of Dividends"
_WITH_IN_LIEU = _DIVIDENDS + _IN_LIEU_ROW


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param((("Dividends", "9.87"), (_IN_LIEU, "4.31")), id="in-lieu-printed"),
        pytest.param(
            (("dividends", "9.87"), (_IN_LIEU.upper(), "4.31")), id="in-lieu-printed-any-case"
        ),
        pytest.param((("Dividends", "14.18"),), id="in-lieu-not-printed"),
    ],
)
def test_payments_in_lieu_are_checked_on_their_own_line_when_printed(
    tmp_path: Path, fields: tuple[tuple[str, str], ...]
) -> None:
    bundle = _dispatch(tmp_path, _WITH_IN_LIEU, _change_in_nav(*fields))

    assert [dividend.amount_gross for dividend in bundle.dividends] == [
        Decimal("9.87"),
        Decimal("4.31"),
    ]


@pytest.mark.parametrize(
    ("dividends", "fields", "message", "amounts"),
    [
        pytest.param(
            _WITH_IN_LIEU,
            (("Dividends", "14.18"), (_IN_LIEU, "4.31")),
            _differs("Dividends", "the 'Dividends' section's rows other than payments in lieu"),
            ("9.87", "0.47", "4.31", "0.23", "14.18"),
            id="dividends-line-counts-in-lieu-rows",
        ),
        pytest.param(
            _DIVIDENDS,
            (("Dividends", "9.87"), (_IN_LIEU, "4.31")),
            _differs(_IN_LIEU, "the 'Dividends' section's payment-in-lieu rows"),
            ("9.87", "0.47", "4.31"),
            id="in-lieu-line-without-in-lieu-rows",
        ),
        pytest.param(
            "",
            ((_IN_LIEU, "4.31"),),
            _absent(_IN_LIEU, "Dividends"),
            ("4.31",),
            id="in-lieu-line-without-dividends-section",
        ),
    ],
)
def test_payment_in_lieu_mismatch_refuses(
    tmp_path: Path,
    dividends: str,
    fields: tuple[tuple[str, str], ...],
    message: str,
    amounts: tuple[str, ...],
) -> None:
    assert str(_refusal(tmp_path, dividends, _change_in_nav(*fields), amounts=amounts)) == message


def test_a_field_matches_its_label_in_any_case(tmp_path: Path) -> None:
    error = _refusal(tmp_path, _change_in_nav((" WITHHOLDING TAX ", "-2.47")), amounts=("-2.47",))

    assert str(error) == _absent("Withholding Tax", "Withholding Tax")


@pytest.mark.parametrize("label", ["Dividends", _IN_LIEU, "Withholding Tax", "Interest"])
def test_a_label_printed_twice_in_different_cases_refuses(tmp_path: Path, label: str) -> None:
    error = _refusal(
        tmp_path, _change_in_nav((label, "9.87"), (label.upper(), "0")), amounts=("9.87",)
    )

    section = "Dividends" if label == _IN_LIEU else label
    assert str(error) == (
        f"the Activity Statement's Change in NAV prints more than one {label!r} "
        f"line (ignoring case), so the amount to check the {section!r} "
        f"section against is ambiguous. {_RE_EXPORT}"
    )


def test_a_present_section_with_no_field_refuses(tmp_path: Path) -> None:
    """A field that isn't printed counts as zero: IBKR leaves zero lines out."""
    error = _refusal(tmp_path, _INTEREST, _change_in_nav(), amounts=("4.31",))

    assert str(error) == _differs("Interest", "the 'Interest' section")


@pytest.mark.parametrize(
    "blocks",
    [
        pytest.param((_change_in_nav(),), id="no-field-no-section"),
        pytest.param(
            (_change_in_nav(*((label, "0.00") for label, _ in _SECTION_FIELDS)),), id="zero"
        ),
        pytest.param(
            (
                "Dividends,Header,Currency,Date,Description,Amount\n",
                _change_in_nav(("Dividends", "0.01")),
            ),
            id="header-only-section-within-a-cent",
        ),
        pytest.param((_DIVIDENDS + _WITHHOLDING + _INTEREST,), id="no-change-in-nav"),
    ],
)
def test_statements_with_nothing_to_contradict_pass(
    tmp_path: Path, blocks: tuple[str, ...]
) -> None:
    _dispatch(tmp_path, *blocks)


def test_a_nonzero_field_within_a_cent_still_needs_its_section(tmp_path: Path) -> None:
    error = _refusal(tmp_path, _change_in_nav(("Dividends", "0.01")), amounts=("0.01",))

    assert str(error) == _absent("Dividends", "Dividends")


def test_a_non_usd_base_account_with_income_refuses(tmp_path: Path) -> None:
    """``Change in NAV`` is in the base currency; the USD rows can't be checked."""
    base = _base().replace("Base Currency,USD,", "Base Currency,EUR,")
    assert base != _base()

    error = _refusal(
        tmp_path,
        _DIVIDENDS,
        _change_in_nav(("Dividends", "9.87")),
        amounts=("9.87", "0.47"),
        base=base,
    )

    assert str(error) == _other_currency("Dividends", "Dividends")


def test_a_non_usd_base_account_without_income_passes(tmp_path: Path) -> None:
    base = _base().replace("Base Currency,USD,", "Base Currency,EUR,")

    _dispatch(tmp_path, _change_in_nav(), base=base)


def test_non_usd_debit_interest_refuses(tmp_path: Path) -> None:
    """A USD account's EUR debit interest can't be checked against the USD line."""
    error = _refusal(
        tmp_path,
        _INTEREST + _DEBIT_ROW,
        _change_in_nav(("Interest", "3.22")),
        amounts=("4.31", "-1.09", "3.22"),
    )

    assert str(error) == _other_currency("Interest", "Interest")


def test_fields_in_a_second_change_in_nav_block_are_checked(tmp_path: Path) -> None:
    second = "Change in NAV,Header,Field Name,Field Value\nChange in NAV,Data,Interest,4.31\n"

    error = _refusal(tmp_path, _change_in_nav(), second, amounts=("4.31",))

    assert str(error) == _absent("Interest", "Interest")


def test_amounts_too_large_to_compare_exactly_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 9E+999999 row and field can't be compared exactly. Since #1276 the
    readers refuse an amount that size by its digit limit first
    (:func:`test_an_amount_past_its_digit_limit_refuses_when_read`), so this
    stubs the limits of the readers on its path out to reach the comparison."""
    for module in ("activity_statement._totals", "_cells"):
        monkeypatch.setattr(
            f"ibkr_fa.parser.{module}.number_limit_breach",
            lambda _value, _kind: None,
            raising=False,
        )
    withholding = _WITHHOLDING.replace(",-2.47,", ",-9E+999999,")
    assert withholding != _WITHHOLDING

    error = _refusal(
        tmp_path,
        withholding,
        _change_in_nav(("Withholding Tax", "9E+999999")),
        amounts=("9E+999999", "0.47"),
    )

    assert str(error) == (
        "the Activity Statement's Change in NAV 'Withholding Tax' amount can't be "
        "compared exactly with the total of the 'Withholding Tax' section: the "
        f"amounts are too large or too small. {_RE_EXPORT}"
    )


class _SubtractionOutOfMemory(Decimal):
    """A row amount that runs out of memory when subtracted from the field."""

    __slots__ = ()

    def __rsub__(self, other: object) -> Decimal:
        raise MemoryError


def test_shared_exact_helper_refuses_subtraction_out_of_memory() -> None:
    """Other callers still need the shared helper's own memory refusal."""
    message = "the comparison can't be kept exactly"
    with pytest.raises(ChangeInNavIncomeError) as excinfo:
        _exact.exactly(
            lambda field, row: field - row,
            Decimal("-7242.47"),
            _SubtractionOutOfMemory("-7242.47"),
            refusal=lambda _: ChangeInNavIncomeError(message),
        )
    assert str(excinfo.value) == message
    _assert_private_error(excinfo.value, "7242")


def test_creating_the_comparison_context_out_of_memory_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Context creation keeps the same precision refusal and empty chain."""

    def _out_of_memory_context(**_: object) -> None:
        raise MemoryError

    # Main owns the context; the branch delegates it to the shared helper.
    monkeypatch.setattr(change_in_nav_income, "localcontext", _out_of_memory_context, raising=False)
    monkeypatch.setattr(_exact, "localcontext", _out_of_memory_context)
    with pytest.raises(ChangeInNavIncomeError) as excinfo:
        change_in_nav_income._check(
            label="Dividends",
            field=Decimal("7242.47"),
            section=KnownActivitySection.DIVIDENDS,
            present=True,
            rows="the 'Dividends' section",
            amounts=[],
            base_currency="USD",
        )
    assert str(excinfo.value) == (
        "the Activity Statement's Change in NAV 'Dividends' amount can't be "
        "compared exactly with the total of the 'Dividends' section: the "
        f"amounts are too large or too small. {_RE_EXPORT}"
    )
    _assert_private_error(excinfo.value, "7242")


def test_running_out_of_memory_in_the_comparison_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``MemoryError`` while subtracting a row from the field refuses with
    the precision message, chains nothing and echoes neither amount."""
    parse = pipeline_dispatch.parse_activity_statement_withholding_tax

    def _out_of_memory_rows(block: Section, *, period: StatementPeriod) -> list[WithholdingTax]:
        return [
            replace(row, amount=_SubtractionOutOfMemory(row.amount))
            for row in parse(block, period=period)
        ]

    monkeypatch.setattr(
        pipeline_dispatch, "parse_activity_statement_withholding_tax", _out_of_memory_rows
    )
    withholding = _WITHHOLDING.replace(",-2.47,", ",-7242.47,")
    assert withholding != _WITHHOLDING

    error = _refusal(
        tmp_path,
        withholding,
        _change_in_nav(("Withholding Tax", "-7242.47")),
        amounts=("7242",),
    )

    assert str(error) == (
        "the Activity Statement's Change in NAV 'Withholding Tax' amount can't be "
        "compared exactly with the total of the 'Withholding Tax' section: the "
        f"amounts are too large or too small. {_RE_EXPORT}"
    )


def test_a_malformed_change_in_nav_refuses_the_income_commands(tmp_path: Path) -> None:
    """The income commands read ``Change in NAV`` with the FA reader's row checks."""
    with pytest.raises(ParseError) as excinfo:
        _dispatch(tmp_path, _change_in_nav(("Commissions", "0"), ("Commissions", "0")))

    assert str(excinfo.value) == (
        "ParseError in section 'Change in NAV' at row 2, column 'Field Name': "
        "repeats an earlier row's Field Name; refusing rather "
        "than overwriting the earlier amount"
    )
    _assert_private_error(excinfo.value, "11.71")


@pytest.mark.parametrize(
    ("fraction", "refuses"),
    [("00999999999999999999", True), ("01", False), ("02", False)],
)
def test_comparison_beyond_28_digits_is_exact(tmp_path: Path, fraction: str, refuses: bool) -> None:
    # Subtracting the small row first must retain its digits beside the large
    # field. With the first fraction the field differs from the rows by
    # 0.01000000000000000001, past the tolerance; at Decimal's default 28
    # digits the field plus 0.02 (34 digits, each amount within the money
    # limit) rounds, and the difference to 0.01, within it.
    large = "100000000000000"
    withholding = (
        _WITHHOLDING.replace(",-2.47,", ",-0.02,")
        + _WITHHOLDING.splitlines(keepends=True)[1].replace(",-2.47,", f",-{large}.00,")
        + f"Withholding Tax,Data,Total,,,-{large}.02,\n"
    )
    field = f"-{large}.{fraction}"
    nav = _change_in_nav(("Withholding Tax", field))

    if refuses:
        error = _refusal(
            tmp_path,
            withholding,
            nav,
            amounts=("-0.02", f"-{large}.00", f"-{large}.02", field, "0.47"),
        )
        assert str(error) == _differs("Withholding Tax", "the 'Withholding Tax' section")
    else:
        _dispatch(tmp_path, withholding, nav)


@pytest.mark.parametrize(
    ("blocks", "section", "row_index", "column"),
    [
        pytest.param(
            (
                _WITHHOLDING.replace(",-2.47,", ",-7242E+999995,"),
                _change_in_nav(("Withholding Tax", "-2.47")),
            ),
            "Withholding Tax",
            0,
            "Amount",
            id="row-near-the-exponent-limit",
        ),
        pytest.param(
            (
                _WITHHOLDING.replace(",-2.47,", ",-0.02,")
                + _WITHHOLDING.splitlines(keepends=True)[1].replace(
                    ",-2.47,", f",-7242{'0' * 25}.00,"
                ),
                _change_in_nav(("Withholding Tax", "-2.47")),
            ),
            "Withholding Tax",
            1,
            "Amount",
            id="row-29-whole-digits",
        ),
        pytest.param(
            (_WITHHOLDING, _change_in_nav(("Withholding Tax", "7242E+999995"))),
            "Change in NAV",
            1,
            "Field Value",
            id="field-near-the-exponent-limit",
        ),
        pytest.param(
            (_WITHHOLDING, _change_in_nav(("Withholding Tax", f"-7242{'0' * 25}.00"))),
            "Change in NAV",
            1,
            "Field Value",
            id="field-29-whole-digits",
        ),
    ],
)
def test_an_amount_past_its_digit_limit_refuses_when_read(
    tmp_path: Path, blocks: tuple[str, ...], section: str, row_index: int, column: str
) -> None:
    """#1276: the amounts the exact-comparison tests above once read refuse
    when their row is read, with the money limit and no value, before any
    comparison."""
    with pytest.raises(ParseError) as excinfo:
        _dispatch(tmp_path, *blocks)

    assert_unchained_refusal(
        excinfo.value,
        ParseError(
            section=section, row_index=row_index, column=column, message=MONEY, raw_value=""
        ),
        f"ParseError in section {section!r} at row {row_index}, column {column!r}: {MONEY}",
        markers=("7242",),
    )
