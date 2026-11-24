"""Schedule FA checks the income sections against ``Change in NAV`` (#1324).

``schedule-fa`` (every format, with or without ``[account.custodian]``) and
``run-all``'s Schedule FA leg run the income commands' check (#1279) on an
Activity Statement. Without it, missing ``Dividends`` and ``Interest`` sections
could file ``0`` in Table A3's gross amount paid and, with a configured custodian,
``No Amount paid/credited`` in Table A2 even when ``Change in NAV`` showed income.
Now such a statement exits 4 and leaves no output, and one that passes the
existing check files what Schedule FA filed before the check. Every
amount here is synthetic, and no refusal prints one.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from ibkr_fa.parser.activity_statement.change_in_nav_income import ChangeInNavIncomeError
from ibkr_fa.pipeline_fa import run_fa_a3
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
from tests._ibkr_flex_cli_inputs import earlier_schedule_fa_outputs as _earlier_schedule_fa_outputs
from tests._ibkr_run_all import seed_every_target as _seed_every_target
from tests._ibkr_user_toml import CUSTODIAN_TOML
from tests._income_blocks import with_totals
from tests._portal_parity import fix_xlsx_clock

_FIXTURES = Path(__file__).parent / "fixtures"
_CY_FIXTURE = _FIXTURES / "e2e_activity_statement_with_nav_2025.csv"
_FY_FIXTURE = _FIXTURES / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
_NAV_HEADER = "Net Asset Value,Header,"
_ENDING_VALUE = "Change in NAV,Data,Ending Value,2107.60\n"
# The fixture's closing NAV, which no refusal prints either.
_NAV_FIGURE = "2107.60"
_CONFIGS = {"custodian": _CONFIG + CUSTODIAN_TOML, "no-custodian": _CONFIG}
_WITH_AND_WITHOUT_CUSTODIAN = pytest.mark.parametrize("config", sorted(_CONFIGS))
_FORMATS = pytest.mark.parametrize(
    ("format_id", "suffix", "extra"),
    [
        pytest.param("review-csv", ".csv", (), id="review-csv"),
        pytest.param("filing-csv", ".csv", (), id="filing-csv"),
        pytest.param("xlsx", ".xlsx", (), id="xlsx"),
        pytest.param(
            "cbdt-json-fragment", ".json", ("--itr-form", "ITR-2"), id="cbdt-json-fragment"
        ),
    ],
)


def _statement(
    *blocks: str,
    fields: Sequence[tuple[str, str]] = (),
    edits: Sequence[tuple[str, str]] = (),
) -> str:
    """The CY 2025 NAV fixture with ``edits`` applied, ``blocks`` before its
    ``Net Asset Value`` and ``fields`` printed in its ``Change in NAV``, with
    the printed totals."""
    text = _CY_FIXTURE.read_text(encoding="utf-8")
    for old, new in edits:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    assert text.count(_NAV_HEADER) == text.count(_ENDING_VALUE) == 1
    lines = "".join(f"Change in NAV,Data,{label},{value}\n" for label, value in fields)
    text = text.replace(_NAV_HEADER, "".join(blocks) + _NAV_HEADER)
    return with_totals(text.replace(_ENDING_VALUE, lines + _ENDING_VALUE))


def _write(run_dir: Path, statement: str, config: str = "custodian") -> tuple[Path, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    statement_path = run_dir / "statement.csv"
    statement_path.write_text(statement, encoding="utf-8")
    config_path = run_dir / "user.toml"
    config_path.write_text(_CONFIGS.get(config, config), encoding="utf-8")
    return statement_path, config_path


def _schedule_fa(
    statement: Path,
    config: Path,
    out: Path,
    format_id: str = "filing-csv",
    extra: tuple[str, ...] = (),
) -> list[str]:
    return [
        "schedule-fa",
        *("--input", str(statement), "--config", str(config), "--output", str(out)),
        *("--calendar-year", "2025", "--format", format_id, *extra, "--no-fetch-prices"),
    ]


def _run(
    run_dir: Path,
    capsys: pytest.CaptureFixture[str],
    statement: str,
    config: str = "custodian",
    format_id: str = "filing-csv",
    suffix: str = ".csv",
    extra: tuple[str, ...] = (),
) -> tuple[int, str, str, dict[str, bytes]]:
    """Run ``schedule-fa`` on ``statement``; return its exit, its stdout and
    stderr with ``run_dir`` masked, and its output files."""
    statement_path, config_path = _write(run_dir, statement, config)
    out = run_dir / "out"
    out.mkdir()
    capsys.readouterr()
    rc = main(
        _schedule_fa(statement_path, config_path, out / f"schedule-fa{suffix}", format_id, extra)
    )
    captured = capsys.readouterr()
    files = {path.name: path.read_bytes() for path in sorted(out.iterdir())}
    return (
        rc,
        captured.out.replace(str(run_dir), "<run>"),
        captured.err.replace(str(run_dir), "<run>"),
        files,
    )


def _without_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ``schedule-fa`` as it ran before #1324, without the check."""
    monkeypatch.setattr(
        "ibkr_fa.pipeline_dispatch.check_income_against_change_in_nav",
        lambda *_args, **_kwargs: None,
    )


# ---- the issue's example: a field whose section is missing -------------------


@pytest.mark.parametrize("case", sorted(_MISSING))
@_FORMATS
@_WITH_AND_WITHOUT_CUSTODIAN
def test_schedule_fa_refuses_a_field_whose_section_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
    format_id: str,
    suffix: str,
    extra: tuple[str, ...],
    config: str,
) -> None:
    """On main, cases that wrote output filed 0 in Table A3's gross amount
    paid and, with a custodian, ``No Amount paid/credited`` in Table A2.
    Main exited 3 except for the CBDT fragment without a custodian, which
    already refused with exit 4.
    Each line now refuses alone; the first one checked is named."""
    fields, message = _MISSING[case]
    statement, config_path = _write(tmp_path, _statement(fields=fields), config)
    out = tmp_path / f"schedule-fa{suffix}"
    earlier = _earlier_schedule_fa_outputs(out, format_id)

    rc = main(_schedule_fa(statement, config_path, out, format_id, extra))

    assert rc == 4
    captured = capsys.readouterr()
    assert captured == ("", f"error: {message}\n")
    _assert_no_amount(captured.err, _NAV_FIGURE, *(value for _, value in fields))
    assert not any(path.exists() for path in earlier)
    assert list(tmp_path.glob("schedule-fa*")) == []


@pytest.mark.parametrize("case", sorted(_MISSING))
@_WITH_AND_WITHOUT_CUSTODIAN
def test_run_all_schedule_fa_leg_refuses_a_field_whose_section_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str, config: str
) -> None:
    """The FY schedules read an ordinary ``--fy-input``; Schedule FA refuses
    the ``--cy-input``, and the run removes every target, an earlier run's
    included (#927)."""
    fields, message = _MISSING[case]
    cy_input, config_path = _write(tmp_path, _statement(fields=fields), config)
    out = tmp_path / "out"
    _seed_every_target(out)

    rc = main(
        [
            "run-all",
            *("--fy-input", str(_FY_FIXTURE), "--cy-input", str(cy_input)),
            *("--config", str(config_path), "--output-dir", str(out)),
            *("--financial-year", "2025-26", "--itr-form", "ITR-2", "--no-fetch-prices"),
        ]
    )

    assert rc == 4
    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    err = captured.err
    assert err.splitlines().count(f"error: {message}") == 1
    error_label = cli._RUN_ALL_STATUS_LABEL["error"]
    errors = [line.split()[1] for line in err.splitlines() if line.lstrip().startswith(error_label)]
    assert errors == ["schedule-fa"]
    for stream in (captured.out, err):
        _assert_no_amount(stream, _NAV_FIGURE, *(value for _, value in fields))


@pytest.mark.parametrize("case", sorted(_MISSING))
def test_the_refusal_holds_no_amount(tmp_path: Path, case: str) -> None:
    """The pipeline raises the check's refusal itself: no amount in its
    ``str``, ``repr``, args or fields, and no chained exception."""
    fields, message = _MISSING[case]
    statement, config = _write(tmp_path, _statement(fields=fields))

    with pytest.raises(ChangeInNavIncomeError) as raised:
        run_fa_a3(
            input_path=statement,
            config_path=config,
            output_path=tmp_path / "schedule-fa.csv",
            calendar_year=2025,
        )

    assert str(raised.value) == message
    _assert_private_error(raised.value, _NAV_FIGURE, *(value for _, value in fields))


# ---- lines that agree file what Schedule FA filed before the check -----------

_SECTIONS = (_DIVIDENDS, _WITHHOLDING, _INTEREST)


@_FORMATS
@_WITH_AND_WITHOUT_CUSTODIAN
def test_lines_that_agree_change_no_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    format_id: str,
    suffix: str,
    extra: tuple[str, ...],
    config: str,
) -> None:
    """All three sections and their lines file exactly what the sections
    without the lines filed before the check, streams included. The
    workbook clock is fixed, so the two XLSX runs compare byte for byte
    (#1343)."""
    fix_xlsx_clock(monkeypatch)
    run = (config, format_id, suffix, extra)
    agreeing = _run(tmp_path / "with", capsys, _statement(*_SECTIONS, fields=_SECTION_FIELDS), *run)
    _without_the_check(monkeypatch)
    before = _run(tmp_path / "without", capsys, _statement(*_SECTIONS), *run)

    # The peak is manual (exit 3). Without a custodian there's no Table A2,
    # which the fragment needs (exit 4).
    no_a2_fragment = (config, format_id) == ("no-custodian", "cbdt-json-fragment")
    assert before[0] == (4 if no_a2_fragment else 3)
    assert agreeing == before


@pytest.mark.parametrize(
    ("label", "section", "total"),
    [
        pytest.param("Dividends", _DIVIDENDS, Decimal("9.87"), id="dividends"),
        pytest.param("Withholding Tax", _WITHHOLDING, Decimal("-2.47"), id="withholding"),
        pytest.param("Interest", _INTEREST, Decimal("4.31"), id="interest"),
    ],
)
@pytest.mark.parametrize(
    ("delta", "refuses"), [("0.01", False), ("-0.01", False), ("0.02", True), ("-0.02", True)]
)
def test_a_present_section_must_agree_to_the_cent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    label: str,
    section: str,
    total: Decimal,
    delta: str,
    refuses: bool,
) -> None:
    field = str(total + Decimal(delta))
    result = _run(tmp_path / "with", capsys, _statement(section, fields=((label, field),)))

    if refuses:
        assert result == (4, "", f"error: {_differs(label, f'the {label!r} section')}\n", {})
        _assert_no_amount(result[2], str(total), field, _NAV_FIGURE)
    else:
        _without_the_check(monkeypatch)
        before = _run(tmp_path / "without", capsys, _statement(section))
        assert before[0] == 3
        assert result == before


# ---- each newly refused class ------------------------------------------------

_EUR_BASE = (
    "Account Information,Data,Base Currency,USD,",
    "Account Information,Data,Base Currency,EUR,",
)


@pytest.mark.parametrize(
    ("blocks", "fields", "edits", "message", "amounts"),
    [
        pytest.param(
            (_INTEREST,),
            (),
            (),
            _differs("Interest", "the 'Interest' section"),
            ("4.31",),
            id="present-section-without-a-line",
        ),
        pytest.param(
            (_DIVIDENDS,),
            (("Dividends", "9.87"),),
            (_EUR_BASE,),
            _other_currency("Dividends", "Dividends"),
            ("9.87", "0.47"),
            id="non-usd-base-dividends",
        ),
        pytest.param(
            (_WITHHOLDING,),
            (("Withholding Tax", "-2.47"),),
            (_EUR_BASE,),
            _other_currency("Withholding Tax", "Withholding Tax"),
            ("-2.47", "0.47"),
            id="non-usd-base-withholding",
        ),
        pytest.param(
            (_INTEREST + _DEBIT_ROW,),
            (("Interest", "3.22"),),
            (),
            _other_currency("Interest", "Interest"),
            ("4.31", "-1.09", "3.22"),
            id="debit-interest-in-another-currency",
        ),
        pytest.param(
            (),
            (("Dividends", "9.87"), ("DIVIDENDS", "0")),
            (),
            "the Activity Statement's Change in NAV prints more than one 'Dividends' "
            "line (ignoring case), so the amount to check the 'Dividends' section "
            f"against is ambiguous. {_RE_EXPORT}",
            ("9.87",),
            id="label-printed-twice",
        ),
    ],
)
@_WITH_AND_WITHOUT_CUSTODIAN
def test_a_statement_the_check_refuses_exits_4(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    blocks: tuple[str, ...],
    fields: tuple[tuple[str, str], ...],
    edits: tuple[tuple[str, str], ...],
    message: str,
    amounts: tuple[str, ...],
    config: str,
) -> None:
    """Main filed each of these (exit 3: the peak is manual)."""
    result = _run(tmp_path, capsys, _statement(*blocks, fields=fields, edits=edits), config)

    assert result == (4, "", f"error: {message}\n", {})
    _assert_no_amount(result[2], _NAV_FIGURE, *amounts)


@_WITH_AND_WITHOUT_CUSTODIAN
def test_a_malformed_change_in_nav_refuses_with_or_without_a_custodian(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], config: str
) -> None:
    """Main read ``Change in NAV`` only for Table A2, so a statement without
    ``[account.custodian]`` filed this one."""
    statement = _statement(fields=(("Commissions", "0"), ("Commissions", "0")))

    result = _run(tmp_path, capsys, statement, config)

    assert result == (
        4,
        "",
        "error: ParseError in section 'Change in NAV' at row 1, column 'Field Name': "
        "repeats an earlier row's Field Name; refusing rather than overwriting the "
        "earlier amount\n",
        {},
    )
    _assert_no_amount(result[2], _NAV_FIGURE)


def test_amounts_too_large_to_compare_exactly_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """As in the income commands' test, the readers' digit limits (#1276) are
    stubbed out to reach the comparison."""
    for module in ("activity_statement._totals", "_cells"):
        monkeypatch.setattr(
            f"ibkr_fa.parser.{module}.number_limit_breach",
            lambda _value, _kind: None,
            raising=False,
        )
    withholding = _WITHHOLDING.replace(",-2.47,", ",-9E+999999,")
    assert withholding != _WITHHOLDING
    statement = _statement(withholding, fields=(("Withholding Tax", "9E+999999"),))

    result = _run(tmp_path, capsys, statement)

    assert result == (
        4,
        "",
        "error: the Activity Statement's Change in NAV 'Withholding Tax' amount can't "
        "be compared exactly with the total of the 'Withholding Tax' section: the "
        f"amounts are too large or too small. {_RE_EXPORT}\n",
        {},
    )
    _assert_no_amount(result[2], _NAV_FIGURE, "9E+999999")


# ---- refusal order -----------------------------------------------------------

_ROC_ROW = (
    "Dividends,Header,Currency,Date,Description,Amount\n"
    "Dividends,Data,USD,12-09-2025,XQBB(US99995B1044) Return Of Capital USD 0.37 per Share,"
    "3.70\n"
)


@pytest.mark.parametrize(
    ("blocks", "fields", "edits", "config", "message"),
    [
        pytest.param(
            (),
            _SECTION_FIELDS,
            (),
            _CONFIGS["custodian"].replace('number = "U12345678"', 'number = "U99999999"'),
            _absent("Dividends", "Dividends"),
            id="account-number-mismatch",
        ),
        pytest.param(
            (),
            _SECTION_FIELDS,
            (
                (
                    "Net Asset Value,Data,Cash,0,100.00,0,100.00,100.00",
                    "Net Asset Value,Data,Cash,0,150.00,0,150.00,150.00",
                ),
            ),
            "custodian",
            _absent("Dividends", "Dividends"),
            id="nav-total-disagrees-with-its-rows",
        ),
        pytest.param(
            (_ROC_ROW,),
            (("Dividends", "3.70"), ("Interest", "4.31")),
            (),
            "custodian",
            _absent("Interest", "Interest"),
            id="return-of-capital-row",
        ),
    ],
)
def test_the_check_comes_before_the_refusals_after_dispatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    blocks: tuple[str, ...],
    fields: tuple[tuple[str, str], ...],
    edits: tuple[tuple[str, str], ...],
    config: str,
    message: str,
) -> None:
    """The check runs in the dispatcher right after the income parsers, so
    it now refuses before the account-number check, the ``Net Asset Value``
    check (#1287, #1309) and the return-of-capital refusal (#1310), each of
    which main printed for these statements."""
    result = _run(tmp_path, capsys, _statement(*blocks, fields=fields, edits=edits), config)

    assert result == (4, "", f"error: {message}\n", {})
    _assert_no_amount(result[2], _NAV_FIGURE, *(value for _, value in fields), "150.00")
