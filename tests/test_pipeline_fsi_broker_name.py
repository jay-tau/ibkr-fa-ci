"""#665: the IBKR FSI / TR / Form 67 paths refuse any statement whose
``Statement`` section does not name exactly one ``Interactive Brokers LLC``.

The shared assembler files all broker interest under the US, which is right
only for IBKR LLC accounts. Every other ``BrokerName`` state is refused,
whether or not the statement has interest. The refusal never prints the
``BrokerName`` cells, which a malformed statement can fill with the holder's
name, and its ``raw_value`` is empty (#1047).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa.cli import main
from ibkr_fa.exceptions import ParseError
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_form67 import run_form67
from ibkr_fa.pipeline_fsi import run_schedule_fsi
from ibkr_fa.pipeline_tr import run_schedule_tr
from tests._ibkr_user_toml import US_PERSON_USER_TOML
from tests._income_blocks import with_totals

_FY_2025_26 = FinancialYear(start_year=2025)
_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
)
_LLC = "Interactive Brokers LLC"
_LLC_ROW = f"Statement,Data,BrokerName,{_LLC},,,,,,,,,,,,,\n"
_IRELAND = "Interactive Brokers Ireland Limited"
# A holder's name in the BrokerName cell, as a malformed statement could give.
_HOLDER = "Synthetic Holder Two"


_INTEREST_HEADER = "Interest,Header,Currency,Date,Description,Amount,,,,,,,,,,,"
_IN_FY_CREDIT = "Interest,Data,USD,30-04-2025,USD Credit Interest for Apr-2025,1.23,,,,,,,,,,,"
# None of these is in-FY credit interest: before the FY, zero, debit, accrual.
_NO_IN_FY_CREDIT = (
    "Interest,Data,USD,31-03-2025,USD Credit Interest for Mar-2025,1.10,,,,,,,,,,,",
    "Interest,Data,USD,31-05-2025,USD Credit Interest for May-2025,0.00,,,,,,,,,,,",
    "Interest,Data,USD,30-06-2025,USD Debit Interest for Jun-2025,-0.50,,,,,,,,,,,",
    "Interest,Data,USD,31-07-2025,USD Credit Interest Accrual for Jul-2025,0.80,,,,,,,,,,,",
)


def _broker_row(value: str) -> str:
    return f"Statement,Data,BrokerName,{value},,,,,,,,,,,,,\n"


def _write_statement(
    tmp_path: Path, broker_rows: tuple[str, ...], interest_rows: tuple[str, ...]
) -> Path:
    text = _FIXTURE.read_text(encoding="utf-8")
    assert text.count(_LLC_ROW) == 1
    text = text.replace(_LLC_ROW, "".join(_broker_row(v) for v in broker_rows))
    if interest_rows:
        text += "\n".join((_INTEREST_HEADER, *interest_rows)) + "\n"
    path = tmp_path / "statement.csv"
    path.write_text(with_totals(text), encoding="utf-8")
    return path


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "user.toml"
    path.write_text(US_PERSON_USER_TOML, encoding="utf-8")
    return path


_RUNNERS: dict[str, Callable[..., Any]] = {
    "schedule-fsi": run_schedule_fsi,
    "schedule-tr": run_schedule_tr,
    "form-67": run_form67,
}

# Each case's BrokerName cells, and how the fixed refusal describes them.
_BROKER_CASES = {
    "ireland": ((_IRELAND,), "names another broker"),
    "holder-name": ((_HOLDER,), "names another broker"),
    "missing": ((), "has no BrokerName row"),
    "blank": (("",), "has a blank BrokerName"),
    "conflicting": ((_LLC, _IRELAND), "has 2 BrokerName rows"),
    "llc-and-holder-name": ((_LLC, _HOLDER), "has 2 BrokerName rows"),
    "duplicated-llc": ((_LLC, _LLC), "has 2 BrokerName rows"),
}


def _refusal(found: str) -> str:
    """The exact refusal: the statement's BrokerName state, never its cells (#1047)."""
    return (
        "ParseError in section 'Statement' at row 0, column 'BrokerName': "
        f"The Activity Statement {found}; only Interactive Brokers LLC statements are "
        "supported."
    )


_INTEREST_CASES = {
    "in-fy-credit": (_IN_FY_CREDIT,),
    "no-interest-rows": (),
    "no-in-fy-credit": _NO_IN_FY_CREDIT,
}


@pytest.mark.parametrize("interest", _INTEREST_CASES.values(), ids=_INTEREST_CASES.keys())
@pytest.mark.parametrize(("broker_rows", "found"), _BROKER_CASES.values(), ids=_BROKER_CASES.keys())
@pytest.mark.parametrize("runner", _RUNNERS.values(), ids=_RUNNERS.keys())
def test_non_llc_statement_is_refused(
    tmp_path: Path,
    runner: Callable[..., Any],
    broker_rows: tuple[str, ...],
    found: str,
    interest: tuple[str, ...],
) -> None:
    out = tmp_path / "result.csv"
    with pytest.raises(ParseError) as exc:
        runner(
            input_path=_write_statement(tmp_path, broker_rows, interest),
            config_path=_write_config(tmp_path),
            output_path=out,
            financial_year=_FY_2025_26,
        )
    message = str(exc.value)
    assert message == _refusal(found)
    assert exc.value.raw_value == ""
    # The fixed text names Interactive Brokers LLC itself; every other cell is absent.
    for cell in {value for value in broker_rows if value.strip()} - {_LLC}:
        assert cell.lower() not in message.lower()
    assert not out.exists()


@pytest.mark.parametrize("runner", _RUNNERS.values(), ids=_RUNNERS.keys())
def test_llc_statement_with_in_fy_interest_still_runs(
    tmp_path: Path, runner: Callable[..., Any]
) -> None:
    report = runner(
        input_path=_write_statement(tmp_path, ("Interactive Brokers LLC",), (_IN_FY_CREDIT,)),
        config_path=_write_config(tmp_path),
        output_path=tmp_path / "result.csv",
        financial_year=_FY_2025_26,
    )
    # form-67 writes its review CSV instead when the row is blank (#601).
    assert report.output_path.exists()


@pytest.mark.parametrize("broker", [_IRELAND, _HOLDER], ids=["ireland", "holder-name"])
def test_run_all_refuses_a_non_llc_statement_before_any_schedule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], broker: str
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    refused = ("schedule-fsi.csv", "schedule-tr.csv", "form-67.csv")
    for name in refused:
        (out / name).write_text("stale", encoding="utf-8")

    rc = main(
        [
            "run-all",
            "--fy-input",
            str(_write_statement(tmp_path, (broker,), (_IN_FY_CREDIT,))),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
            "--config",
            str(_write_config(tmp_path)),
            "--output-dir",
            str(out),
        ]
    )

    assert rc == 4
    # The shared FSI build refuses the statement in run-all's Form 67 preflight
    # (#601), before any schedule writes, and the stale output is removed.
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert err == f"error: run-all: {_refusal('names another broker')}\n"
    assert broker.lower() not in err.lower()
