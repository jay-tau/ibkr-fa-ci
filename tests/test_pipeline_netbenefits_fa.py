"""Tests for the ``netbenefits-fa`` pipeline (Schedule FA Table A3 for a Fidelity
ESPP holding) and its CLI subcommand.

Inputs are synthetic: the lots CSVs and the confirmation text are made up, and
the tests need no binary PDF. ``pdftotext`` is neutralised by monkeypatching the
module-level ``pdftotext_layout`` to return fixture text keyed by file name;
Yahoo is neutralised by an injected fetcher.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import errno
import hashlib
import json
import lzma
import re
import warnings
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Callable, Iterator
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn
from zipfile import BadZipFile, ZipFile

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet._reader import WorkSheetParser

import ibkr_fa.cli
import ibkr_fa.pipeline_netbenefits_fa_outputs
from ibkr_fa.cli import main
from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.dec31_close import CloseSource, ClosingPriceError
from ibkr_fa.compute.form67 import Form67Readiness
from ibkr_fa.exceptions import ConfigError, IbkrFaError, MissingFxRateError
from ibkr_fa.fx import policy_citations
from ibkr_fa.fx.policy_rule115 import Rule115Policy
from ibkr_fa.models import FaA3PeakBlankReason, FaA3Row
from ibkr_fa.models.fa_a2_row import FaA2FiledValues, FaA2Row
from ibkr_fa.models.fa_a3_row import FaA3FiledValues
from ibkr_fa.output import netbenefits_quicko_fa_xlsx
from ibkr_fa.output._cleanup import OutputCollisionError, OutputRemovalError, RunOutputs
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.base import Subcommand
from ibkr_fa.output.cbdt_json_fragment import CBDT_JSON_FRAGMENT_FORMAT_ID
from ibkr_fa.output.fa_manual_fill import FA_A3_PEAK_VALUE_COLUMN, MANUAL_FILL_PORTAL_COLUMNS
from ibkr_fa.output.netbenefits_quicko_fa_xlsx import (
    HEADERS as QUICKO_HEADERS,
)
from ibkr_fa.output.netbenefits_quicko_fa_xlsx import (
    QUICKO_TEMPLATE_FILENAME,
    QuickoFaWorkbookInput,
    QuickoFaXlsxError,
    QuickoFaXlsxWriter,
    _replace_cell,
)
from ibkr_fa.parser.netbenefits.bundle import NetBenefitsBundleError
from ibkr_fa.parser.netbenefits.statement_pdf import (
    ReportType,
    StatementData,
    StatementDividend,
    StatementPurchase,
)
from ibkr_fa.pipeline_netbenefits_fa import (
    NetBenefitsFaError,
    NetBenefitsFaRunReport,
    run_netbenefits_fa,
)
from ibkr_fa.prices.cache import _cache_path, write_cache
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.prices.yahoo import (
    DailyCloseFetcher,
    YahooResponseFormatError,
    fetch_daily_closes_usd,
)
from ibkr_fa.reference.us_market_calendar import us_market_sessions
from ibkr_fa.types import CountryCode, Isin, Ticker
from ibkr_fa.xlsx_checks import ALLOWED_WARNING, require_cells_in_order
from tests._cbdt_fragment_inputs import unchecked_row as _unchecked
from tests._crash_reports import assert_replaced_unchained, crash_pattern, internal_error_line
from tests._dec31_close import (
    MARKER_PRICE,
    MARKER_REASON,
    MARKER_SOURCE,
    MARKER_SOURCE_DATE,
    MARKER_SYMBOL,
    assert_markers_absent,
)
from tests._netbenefits import (
    CLOSED_EMPTY_CSV,
    CLOSED_GAP_CSV,
    CLOSED_INTRA_CY_CSV,
    CLOSED_RENAMED_PROCEEDS_CSV,
    CLOSED_RENAMED_PROCEEDS_REFUSAL,
    CONF_DEC,
    CONF_MAR,
    DATED_LINE_UNREAD,
    OPEN_CSV,
    OPEN_RECASED_CSV,
    a2_credit_refusal,
    confirmation,
    dec_confirmation,
    mar_confirmation,
    patch_pdftotext,
    statement_data,
    table_row,
)
from tests._netbenefits_fa import (
    A3_ZIP_LINE,
    BASE_CONFIG,
    NB_BY_HAND,
    NB_MARKET_SESSION_REFUSAL,
    NB_OVERRIDE_SESSION_REFUSAL,
    PRICE_NOW,
    QUICKO_ZIP_LINE,
    nb_peak_line,
    session_fetcher,
    write_quicko_template,
)
from tests._netbenefits_statement_pdf import OTHER_CASH_ACTIVITY, dec31_unpriced_text
from tests._text_edits import replace_once
from tests._writer_stubs import StubWriter, patch_writers
from tests._yahoo_prices import NO_ZONE_REFUSAL, serve_no_zone_response

# --- fixtures (synthetic) -----------------------------------------------------


# The refusal of a CY-overlapping closed lot after its "closed-lots data row N" (#1042).
_CLOSED_LOT_OVERLAP_REFUSAL = (
    "overlaps CY2025 but is not a Dec-31-held gap-window lot (acquired on Dec-31 and sold "
    "after it); NetBenefits intra-CY closed-lot Schedule FA A3 disclosure is not yet supported "
    "(see #I13) — refusing rather than emit an A3 that omits a disclosable holding"
)

# Open snapshot after a PARTIAL post-Dec-31 sale: only 2.4820 of the 3.4820-share
# Dec-31 purchase still shows as held (the other 1.0 was sold the next CY).
OPEN_CSV_PARTIAL = replace_once(
    OPEN_CSV,
    "Dec-31-2025,3.4820,668.96,192.12,626.24,-42.72,",
    "Dec-31-2025,2.4820,476.84,192.12,446.39,-30.45,",
)

# Open snapshot after the WHOLE Dec-31 purchase was sold the next CY: no Dec-31
# lot survives (only the out-of-window Mar-31-2026 lot remains).
OPEN_CSV_NO_DEC = replace_once(
    OPEN_CSV, "Dec-31-2025,3.4820,668.96,192.12,626.24,-42.72,-,-,Nov-03-2025,SP,Short\n", ""
)

# The 1.0 gap-window share that reconciles OPEN_CSV_PARTIAL back to the 3.4820-share
# confirmation (same per-share cost 192.12 as the open lot).
CLOSED_GAP_PARTIAL_CSV = (
    "\ufeffDate acquired,Quantity,Date sold or transferred,Proceeds,Cost basis,Gain/loss,Term\n"
    "Dec-31-2025,1.0000,Apr-20-2026,500.00,192.12,307.88,Short\n"
    ",\n"
    "The values are displayed in USD\n"
)

# The full 3.4820-share Dec-31 purchase, all sold the next CY (reconstructs alongside
# OPEN_CSV_NO_DEC): contribution 668.96 == confirmation, per-share 192.12.
CLOSED_GAP_FULL_CSV = (
    "\ufeffDate acquired,Quantity,Date sold or transferred,Proceeds,Cost basis,Gain/loss,Term\n"
    "Dec-31-2025,3.4820,Apr-20-2026,800.00,668.96,131.04,Short\n"
    ",\n"
    "The values are displayed in USD\n"
)

# A gap-window row whose per-share cost (192.12 / 2.0 = 96.06) no longer matches the
# confirmation price 192.12 — the shape a 2:1 split between Dec-31 and the sale takes.
CLOSED_GAP_SPLIT_CSV = (
    "\ufeffDate acquired,Quantity,Date sold or transferred,Proceeds,Cost basis,Gain/loss,Term\n"
    "Dec-31-2025,2.0000,Apr-20-2026,900.00,192.12,707.88,Short\n"
    ",\n"
    "The values are displayed in USD\n"
)


# A statement PDF (income/holdings) — ignored by Table A3, used to check the
# classifier does not treat it as a confirmation.
STATEMENT_TEXT = (
    "Fidelity Stock Plan Services, LLC\n"
    "STOCK PLAN SERVICES REPORT\n"
    "02/01/2026 - 03/31/2026\n"
    "Participant Number: I99999999\n"
)


def _stub_fetcher(close: str = "213.47", *, high: str | None = None) -> DailyCloseFetcher:
    def fetch(ticker, *, start, end, now, fetch_symbol=None):
        closes = {}
        highs = {}
        day = start
        while day <= end:
            closes[day] = Decimal(close)
            if high is not None:
                highs[day] = Decimal(high)
            day += dt.timedelta(days=1)
        return PriceSeries(
            ticker=ticker,
            currency="USD",
            closes=closes,
            highs=highs,
            has_split_in_range=False,
            source="stub",
            fetched_at=now,
        )

    return fetch


def _marked_config(price_date: str | None) -> str:
    """BASE_CONFIG under the symbol marker, with its override dated
    ``price_date`` and carrying the price, source and reason markers, or with no
    override when ``price_date`` is ``None`` (#1165)."""
    # The confirmation's issuer-name grammar rejects the lowercase description marker.
    config = BASE_CONFIG.replace('symbol = "XESP"', f'symbol = "{MARKER_SYMBOL}"')
    head, _override = config.split("[[netbenefits.price_overrides]]")
    if price_date is None:
        return head
    return head + (
        "[[netbenefits.price_overrides]]\n"
        f"price_date = {price_date}\n"
        f'price_per_share_usd = "{MARKER_PRICE}"\n'
        f'source = "{MARKER_SOURCE}"\n'
        f'reason = "{MARKER_REASON}"\n'
    )


# CONF_DEC under the symbol marker, to go with _marked_config.
_MARKED_CONFIRMATIONS = {
    "espp-purchase_2025-12-31.pdf": CONF_DEC.replace("SYMBOL: XESP", f"SYMBOL: {MARKER_SYMBOL}")
}


_PNO = "I99999999"


# CONF_DEC's row in a statement (#1151): settled 01/05, its CUSIP, shares, price and
# contribution as an outflow.
_DEC31_ROW = StatementPurchase(
    dt.date(2026, 1, 5), "99999E104", Decimal("3.482"), Decimal("192.12000"), Decimal("-668.96")
)


def _stmt_data(
    period_start: dt.date,
    period_end: dt.date,
    *,
    dividends: tuple = (),
    withholdings: tuple = (),
    mmkt_buys: tuple = (),
    purchases: tuple = (),
) -> StatementData:
    """A synthetic monthly SPS statement (unpriced, no holdings) for the A2 cash
    proof — income tuples default empty (the supported no-cash case)."""
    return statement_data(
        period_start,
        period_end,
        participant_no=_PNO,
        dividends=dividends,
        withholdings=withholdings,
        mmkt_buys=mmkt_buys,
        purchases=purchases,
    )


def _covering(**income: tuple) -> StatementData:
    """A statement from December 1, 2025 through January 31, 2026 with CONF_DEC's row:
    it covers the A2 cash proof's window and the #1151 lot proof's, through ten days
    after Dec-31."""
    return _stmt_data(dt.date(2025, 12, 1), dt.date(2026, 1, 31), purchases=(_DEC31_ROW,), **income)


# The e2e January report: it prints CONF_DEC's row in Fidelity's layout (#1151).
_JANUARY_REPORT = (
    Path(__file__).parent / "fixtures" / "netbenefits" / "statement-jan.txt"
).read_text(encoding="utf-8")

# Default statement covering the A2 cash-proof window (BASE_CONFIG opens the
# account 2025-12-31, so the window is Dec-31 alone) and the lot proof's, carrying
# no income.
_DEFAULT_STATEMENTS = {"statement_2025-12.pdf": "STOCK PLAN SERVICES REPORT\n::cover::\n"}


def _quicko_row() -> FaA3Row:
    acquired = dt.date(2025, 12, 31)
    return FaA3Row(
        country_name="United States of America",
        country_code="2",
        name_of_entity="EXAMPLE CORP",
        address_of_entity="1 Sample Issuer Street, Sample City, XX",
        zip_code="00000",
        nature_of_entity="Listed company shares",
        date_of_acquiring=acquired,
        initial_value_inr=Decimal("66503"),
        peak_value_inr=Decimal("66503"),
        closing_value_inr=Decimal("66503"),
        gross_amount_paid_credited_inr=Decimal("0"),
        gross_proceeds_from_sale_inr=Decimal("0"),
        source_isin=Isin("US99999E1047"),
        source_ticker=Ticker("XESP"),
        source_country_iso2=CountryCode("US"),
        audit_cost_basis_usd=Decimal("743.30"),
        audit_acquisition_date=acquired,
        audit_close_price_usd=Decimal("213.47"),
        audit_ttbr_acquisition=Decimal("89.47"),
        audit_ttbr_dec31=Decimal("89.47"),
        audit_disposal_date=None,
        peak_blank_reason=None,
    )


def _fill_quicko(template: Path, output: Path, *rows: FaA3Row) -> None:
    """Fill ``template`` into ``output`` with ``rows`` (default one :func:`_quicko_row`)."""
    QuickoFaXlsxWriter().write(
        QuickoFaWorkbookInput(
            rows=rows or (_quicko_row(),),
            account_number="I99999999",
            template_path=template,
        ),
        output,
    )


def _unreadable_template(template: Path) -> str:
    """The refusal for a template the loader can't read as a workbook (#1042)."""
    return f"cannot read Quicko template {template}: it is not a readable XLSX workbook"


def _build_dir(
    tmp_path: Path,
    *,
    open_csv: str = OPEN_CSV,
    closed_csv: str | None = CLOSED_EMPTY_CSV,
    confirmations: dict[str, str] | None = None,
    statements: dict[str, str] | None = None,
    quicko_headers: tuple[str, ...] | None = QUICKO_HEADERS,
) -> Path:
    """Lay out a bundle directory. CSVs are written as text files; PDFs are empty
    placeholder files whose text is supplied via the monkeypatched reader (see
    ``_patch_pdf``). Returns the directory."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "View open lots.csv").write_text(open_csv, encoding="utf-8")
    if closed_csv is not None:
        (bundle / "View closed lots.csv").write_text(closed_csv, encoding="utf-8")
    confs = {"espp-purchase_2025-12-31.pdf": CONF_DEC} if confirmations is None else confirmations
    stmts = _DEFAULT_STATEMENTS if statements is None else statements
    for name in {**confs, **stmts}:
        (bundle / name).write_bytes(b"%PDF-1.4 placeholder")
    if quicko_headers is not None:
        write_quicko_template(bundle / QUICKO_TEMPLATE_FILENAME, quicko_headers)
    return bundle


def _patch_pdf(
    monkeypatch: pytest.MonkeyPatch,
    texts: dict[str, str],
    *,
    statement_data: dict[str, StatementData] | None = None,
) -> None:
    """Monkeypatch ``pdftotext_layout`` (text by file name) and ``parse_statement``
    (synthetic :class:`StatementData` for the A2 cash proof). A statement text
    keyed ``::name::`` resolves to ``statement_data[name]``; any other statement
    yields :func:`_covering`'s statement with no income."""

    patch_pdftotext(monkeypatch, texts)

    data_map = statement_data or {}

    def fake_parse(
        text: str, *, expected_participant_no: str | None = None, source: str = ""
    ) -> StatementData:
        for key, data in data_map.items():
            if f"::{key}::" in text:
                return data
        return _covering()

    monkeypatch.setattr("ibkr_fa.parser.netbenefits.bundle.parse_statement", fake_parse)


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    open_csv: str = OPEN_CSV,
    closed_csv: str | None = CLOSED_EMPTY_CSV,
    confirmations: dict[str, str] | None = None,
    statements: dict[str, str] | None = None,
    statement_data: dict[str, StatementData] | None = None,
    config: str = BASE_CONFIG,
    calendar_year: int = 2025,
    fetch_prices: bool = True,
    fetcher_close: str = "213.47",
    fetcher_high: str | None = None,
    format_id: str = "filing-csv",
    itr_form: ItrForm | None = None,
    output_name: str = "a3.csv",
    quicko_headers: tuple[str, ...] | None = QUICKO_HEADERS,
    quicko_workbook: bool = False,
    price_fetcher: DailyCloseFetcher | None = None,
):
    bundle = _build_dir(
        tmp_path,
        open_csv=open_csv,
        closed_csv=closed_csv,
        confirmations=confirmations,
        statements=statements,
        quicko_headers=quicko_headers,
    )
    confs = {"espp-purchase_2025-12-31.pdf": CONF_DEC} if confirmations is None else confirmations
    stmts = _DEFAULT_STATEMENTS if statements is None else statements
    _patch_pdf(monkeypatch, {**confs, **stmts}, statement_data=statement_data)
    cfg = tmp_path / "user.toml"
    cfg.write_text(config, encoding="utf-8")
    out = tmp_path / output_name
    report = run_netbenefits_fa(
        input_dir=bundle,
        config_path=cfg,
        output_path=out,
        calendar_year=calendar_year,
        fetch_prices=fetch_prices,
        price_cache_dir=tmp_path / "cache",
        price_fetcher=price_fetcher or _stub_fetcher(fetcher_close, high=fetcher_high),
        price_now=PRICE_NOW,
        format_id=format_id,
        itr_form=itr_form,
        quicko_workbook=quicko_workbook,
    )
    return report, out


# The files a default-named ``_run`` writes: Table A3, Table A2 and the Quicko workbook.
_FA_OUTPUTS = ("a3.csv", "a3-a2.csv", "a3-quicko.xlsx")

_QUICKO_HEADERS_REFUSAL = "Quicko template Shares & Securities headers do not match"

# A missing template, one whose last header does not match Quicko's, and one with
# an extra header after Quicko's, each with its exact refusal. Only the missing
# template's refusal names the path and errno, never the OSError's text (#1042).
_BAD_QUICKO_TEMPLATES = [
    pytest.param(
        None,
        lambda bundle: f"cannot read Quicko template {bundle / QUICKO_TEMPLATE_FILENAME} (ENOENT)",
        id="missing",
    ),
    pytest.param(
        (*QUICKO_HEADERS[:-1], "Unexpected column"),
        lambda _bundle: _QUICKO_HEADERS_REFUSAL,
        id="wrong-headers",
    ),
    pytest.param(
        (*QUICKO_HEADERS, "Broker ID*"), lambda _bundle: _QUICKO_HEADERS_REFUSAL, id="extra-header"
    ),
]

# Distinct markers for a Quicko failure's message, its file name and its cause;
# no Quicko refusal or crash line may print any of them (#1042).
_QUICKO_MESSAGE_MARKER = "Synthetic Strerror Marker"
_QUICKO_FILE_MARKER = "Synthetic Holder Two/x.xlsx"
_QUICKO_CAUSE_MARKER = "Chained Marker U1234567"
_QUICKO_MARKERS = (
    "Synthetic Strerror Marker",
    "Synthetic Holder Two",
    "Chained Marker",
    "U1234567",
)


def _assert_no_quicko_markers(text: str) -> None:
    for marker in _QUICKO_MARKERS:
        assert marker.casefold() not in text.casefold(), marker


def _assert_fixed_quicko_refusal(exc: BaseException, expected: str) -> None:
    """``exc`` is exactly ``expected``, raised ``from None`` so no chain
    carries the failure's text, and prints no marker (#1042)."""
    assert str(exc) == expected
    assert exc.__cause__ is None
    assert exc.__suppress_context__
    _assert_no_quicko_markers(str(exc))


def _raise_marked(failure: BaseException) -> NoReturn:
    """Raise ``failure`` from a cause holding :data:`_QUICKO_CAUSE_MARKER`."""
    raise failure from ValueError(_QUICKO_CAUSE_MARKER)


def _marked_text() -> str:
    return f"{_QUICKO_MESSAGE_MARKER} in '{_QUICKO_FILE_MARKER}'"


def _eacces() -> OSError:
    return OSError(errno.EACCES, _QUICKO_MESSAGE_MARKER, _QUICKO_FILE_MARKER)


def _no_errno() -> OSError:
    return OSError(None, _QUICKO_MESSAGE_MARKER, _QUICKO_FILE_MARKER)


def _crash() -> RuntimeError:
    return RuntimeError(_marked_text())


def _mkdir(path: Path) -> Path:
    """Create (and return) a fresh directory, for tests that run two bundles."""
    path.mkdir()
    return path


def _row_dict(out: Path) -> dict[str, str]:
    with out.open(encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert len(rows) == 2, "expected a header + exactly one data row"
    return dict(zip(rows[0], rows[1], strict=True))


def _a2_row_dict(a2_path: Path) -> dict[str, str]:
    with a2_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    assert len(rows) == 2, "expected an A2 header + exactly one data row"
    return dict(zip(rows[0], rows[1], strict=True))


def _assert_absent(message: str, *values: str) -> None:
    """None of ``values``, input the pre-#1042 refusal printed, is in
    ``message``, compared case-insensitively."""
    for value in values:
        assert value.casefold() not in message.casefold(), value


# The Dec-31 purchase as the ESPP refusals name it: 1-based by purchase date (#1042).
_CONF_1 = "confirmation 1 (by purchase date)"


def _a2_dividend_refusal(statement: int) -> str:
    """The A2 cash proof's refusal for an in-window dividend on row 1 of the
    statement at ``statement`` among every report, year-end ones included (#1042)."""
    return a2_credit_refusal(
        "dividend", table_row(statement, 1, "Dividends, Interest & Other Income")
    )


# --- happy path ---------------------------------------------------------------


def test_quicko_template_shared_strings_and_multiple_rows(tmp_path: Path) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    first = _quicko_row()
    second = replace(
        first,
        name_of_entity="CONTOSO CORP",
        initial_value_inr=Decimal("58219"),
        gross_proceeds_from_sale_inr=Decimal("20"),
        gross_amount_paid_credited_inr=Decimal("10"),
    )

    QuickoFaXlsxWriter().write(
        QuickoFaWorkbookInput(
            rows=(first, second),
            account_number="I99999999",
            template_path=template,
        ),
        output,
    )

    workbook = load_workbook(output, data_only=True)
    worksheet = workbook["Shares & Securities"]
    assert worksheet["A2"].value == "EXAMPLE CORP"
    assert worksheet["A3"].value == "CONTOSO CORP"
    assert worksheet["J2"].value == 66503
    assert worksheet["J3"].value == 58219
    assert worksheet["M2"].value is None
    assert worksheet["N2"].value is None
    assert worksheet["M3"].value == 20
    assert worksheet["N3"].value == 10
    with ZipFile(output) as package:
        shared = package.read("xl/sharedStrings.xml").decode("utf-8")
    assert "EXAMPLE CORP" in shared
    assert "CONTOSO CORP" in shared


def test_quicko_template_uses_its_exact_country_label(tmp_path: Path) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    row = replace(
        _quicko_row(),
        country_name="United Kingdom",
        country_code="44",
        source_country_iso2=CountryCode("GB"),
        source_isin=Isin("GB99970K1039"),
    )

    QuickoFaXlsxWriter().write(
        QuickoFaWorkbookInput(
            rows=(row,),
            account_number="I99999999",
            template_path=template,
        ),
        output,
    )

    workbook = load_workbook(output, data_only=True)
    assert (
        workbook["Shares & Securities"]["C2"].value
        == "United Kingdom Of Great Britain And Northern Ireland"
    )


def test_quicko_targets_named_sheet_when_physical_parts_are_swapped(tmp_path: Path) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    with ZipFile(template) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}
    sheet1 = "xl/worksheets/sheet1.xml"
    sheet2 = "xl/worksheets/sheet2.xml"
    payloads[sheet1], payloads[sheet2] = payloads[sheet2], payloads[sheet1]
    relationships = payloads["xl/_rels/workbook.xml.rels"]
    relationships = relationships.replace(
        b'Target="/xl/worksheets/sheet1.xml"', b'Target="/xl/worksheets/pending.xml"', 1
    )
    relationships = relationships.replace(
        b'Target="/xl/worksheets/sheet2.xml"', b'Target="/xl/worksheets/sheet1.xml"', 1
    )
    payloads["xl/_rels/workbook.xml.rels"] = relationships.replace(
        b'Target="/xl/worksheets/pending.xml"', b'Target="/xl/worksheets/sheet2.xml"', 1
    )
    temporary = template.with_suffix(".tmp")
    with ZipFile(temporary, "w") as package:
        for info in infos:
            package.writestr(info, payloads[info.filename])
    temporary.replace(template)

    row = _quicko_row()
    QuickoFaXlsxWriter().write(
        QuickoFaWorkbookInput(
            rows=(row,),
            account_number="I99999999",
            template_path=template,
        ),
        output,
    )

    workbook = load_workbook(output, data_only=True)
    assert workbook["Shares & Securities"]["A2"].value == "EXAMPLE CORP"
    assert workbook["Instructions"]["B32"].value == "United States Of America"


def test_quicko_sparse_template_cell_replacement_keeps_inserted_cells() -> None:
    sheet = '<row r="221"><c r="C221" s="34"/></row>'
    sheet = _replace_cell(sheet, "A221", '<c r="A221"><v>1</v></c>')
    sheet = _replace_cell(sheet, "B221", '<c r="B221"><v>2</v></c>')
    sheet = _replace_cell(sheet, "C221", '<c r="C221"><v>3</v></c>')

    assert sheet.index('r="A221"') < sheet.index('r="B221"') < sheet.index('r="C221"')


def test_quicko_self_closing_row_keeps_cells_under_the_right_row() -> None:
    sheet = '<row r="2"/><row r="3"></row>'
    updated = _replace_cell(sheet, "A2", '<c r="A2"><v>1</v></c>')

    assert '<row r="2"><c r="A2"><v>1</v></c></row>' in updated
    assert '<row r="3"></row>' in updated


@pytest.mark.parametrize(
    ("parse", "message"),
    [
        pytest.param(
            lambda xml: netbenefits_quicko_fa_xlsx._worksheet_xml_path(
                {"xl/workbook.xml": xml.encode(), "xl/_rels/workbook.xml.rels": b"<r/>"}
            ),
            "Quicko template workbook relationships are invalid",
            id="workbook-xml",
        ),
        pytest.param(
            netbenefits_quicko_fa_xlsx._shared_strings,
            "Quicko template sharedStrings.xml is invalid",
            id="shared-strings",
        ),
    ],
)
def test_quicko_template_xml_parse_failure_refuses_without_its_parser_text(
    parse: Any, message: str
) -> None:
    """Backstops behind openpyxl's own read (#1042): malformed XML refuses in
    fixed text with no chained parser error."""
    with pytest.raises(QuickoFaXlsxError) as exc_info:
        parse("<Synthetic Holder Two")
    assert str(exc_info.value) == message
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__


def test_quicko_template_refuses_invalid_shared_string_counts(tmp_path: Path) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    with ZipFile(template) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}
    payloads["xl/sharedStrings.xml"] = payloads["xl/sharedStrings.xml"].replace(
        b'count="14"', b'count="bad"', 1
    )
    broken = template.with_suffix(".tmp")
    with ZipFile(broken, "w") as package:
        for info in infos:
            package.writestr(info, payloads[info.filename])
    broken.replace(template)

    with pytest.raises(QuickoFaXlsxError) as caught:
        QuickoFaXlsxWriter().write(
            QuickoFaWorkbookInput(
                rows=(_quicko_row(),),
                account_number="I99999999",
                template_path=template,
            ),
            output,
        )

    _assert_fixed_quicko_refusal(
        caught.value, "Quicko template sharedStrings.xml has invalid counts"
    )
    assert not output.exists()


def test_quicko_template_wraps_malformed_workbook_xml(tmp_path: Path) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    with ZipFile(template) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}
    payloads["xl/workbook.xml"] = b"<workbook>"
    broken = template.with_suffix(".tmp")
    with ZipFile(broken, "w") as package:
        for info in infos:
            package.writestr(info, payloads[info.filename])
    broken.replace(template)

    with pytest.raises(QuickoFaXlsxError) as caught:
        _fill_quicko(template, output)

    # #1042: fixed text after the template path, never the XML parser's text.
    _assert_fixed_quicko_refusal(caught.value, _unreadable_template(template))
    assert not output.exists()


def test_quicko_template_refuses_illegal_xml_controls(tmp_path: Path) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    # The row refuses a control character itself (#1367); this pins the writer.
    row = _unchecked(_quicko_row(), name_of_entity="EXAM\x0bPLE CORP")

    with pytest.raises(QuickoFaXlsxError) as caught:
        _fill_quicko(template, output, _quicko_row(), row)
    # #1042: the row and cell, never the character's code point.
    assert str(caught.value) == "Quicko row 2 cell 0 contains an illegal XML control character"
    assert "U+" not in str(caught.value)
    assert "000B" not in str(caught.value)
    assert not output.exists()


def _edit_quicko_sheet(template: Path, old: str, new: str) -> None:
    """Replace the first ``old`` in the template's Shares & Securities sheet XML."""
    with ZipFile(template) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}
    sheet = payloads["xl/worksheets/sheet2.xml"].decode("utf-8")
    assert old in sheet
    payloads["xl/worksheets/sheet2.xml"] = sheet.replace(old, new, 1).encode("utf-8")
    temporary = template.with_suffix(".tmp")
    with ZipFile(temporary, "w") as package:
        for info in infos:
            package.writestr(info, payloads[info.filename])
    temporary.replace(template)


def test_quicko_template_with_trailing_empty_header_cells_is_filled(tmp_path: Path) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    _edit_quicko_sheet(template, "</row>", '<c r="O1" s="1" /><c r="P1" /></row>')

    QuickoFaXlsxWriter().write(
        QuickoFaWorkbookInput(
            rows=(_quicko_row(),),
            account_number="I99999999",
            template_path=template,
        ),
        output,
    )

    worksheet = load_workbook(output, data_only=True)["Shares & Securities"]
    assert [cell.value for cell in worksheet[1]] == [*QUICKO_HEADERS, None, None]
    assert worksheet["A2"].value == "EXAMPLE CORP"


@pytest.mark.parametrize(
    ("headers", "old", "new", "message"),
    [
        pytest.param(
            (*QUICKO_HEADERS, "Broker ID*"),
            '<dimension ref="A1:O3" />',
            '<dimension ref="A1:N3" />',
            "headers do not match",
            id="extra-header",
        ),
        pytest.param(
            QUICKO_HEADERS,
            "</sheetData>",
            '<row r="9"><c r="A9" t="inlineStr"><is><t>x</t></is></c></row></sheetData>',
            "is not blank",
            id="data-row",
        ),
    ],
)
def test_quicko_template_check_reads_past_its_declared_dimension(
    tmp_path: Path, headers: tuple[str, ...], old: str, new: str, message: str
) -> None:
    # The sheet's <dimension> tag understates its content; the check reads every cell.
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template, headers)
    _edit_quicko_sheet(template, old, new)

    with pytest.raises(QuickoFaXlsxError, match=message):
        QuickoFaXlsxWriter().write(
            QuickoFaWorkbookInput(
                rows=(_quicko_row(),),
                account_number="I99999999",
                template_path=template,
            ),
            output,
        )

    assert not output.exists()


_QUICKO_TEMPLATE_ROW_2 = '<row r="2">' + "".join(
    f'<c r="{column}2" s="1" t="n" />' for column in "ABCDEFGHIJKL"
)


@pytest.mark.parametrize(
    ("headers", "edits", "message"),
    [
        pytest.param(
            (*QUICKO_HEADERS, "Broker ID*"),
            (
                (
                    '<c r="N1" t="s"><v>13</v></c><c r="O1" t="s"><v>14</v></c>',
                    '<c r="O1" t="s"><v>14</v></c><c r="N1" t="s"><v>13</v></c>',
                ),
            ),
            "cell N1 is stored out of order",
            id="header-before-a-lower-column",
        ),
        pytest.param(
            QUICKO_HEADERS,
            (('<row r="2">', '<row r="2"><c r="M2" t="n"><v>777</v></c>'),),
            "cell A2 is stored out of order",
            id="sale-value-before-a-lower-column",
        ),
        pytest.param(
            QUICKO_HEADERS,
            (
                (f"{_QUICKO_TEMPLATE_ROW_2}</row>", ""),
                (
                    "</sheetData>",
                    f'{_QUICKO_TEMPLATE_ROW_2}<c r="O2" t="n"><v>555</v></c></row></sheetData>',
                ),
            ),
            "row 2 is stored out of order",
            id="row-after-a-higher-row",
        ),
        pytest.param(
            QUICKO_HEADERS,
            (
                (
                    '<c r="L2" s="1" t="n" />',
                    '<c r="L2" t="n"><v>777</v></c><c r="L2" s="1" t="n" />',
                ),
            ),
            "cell L2 is stored out of order",
            id="repeated-cell",
        ),
        pytest.param(
            QUICKO_HEADERS,
            (("</sheetData>", '<row r="3"><c r="O3" t="n"><v>555</v></c></row></sheetData>'),),
            "row 3 is stored out of order",
            id="repeated-row",
        ),
    ],
)
def test_quicko_template_refuses_rows_or_cells_stored_out_of_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    headers: tuple[str, ...],
    edits: tuple[tuple[str, str], ...],
    message: str,
) -> None:
    # Read-only openpyxl drops a cell stored before a lower column and skips a row
    # stored after a higher one, so each edit hides a value from the blank checks (#961).
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template, headers)
    for old, new in edits:
        _edit_quicko_sheet(template, old, new)
    # #1042: the refusal is fixed text, so record which check fired.
    check = require_cells_in_order
    found: list[str] = []

    def recording_check(worksheet: Any) -> None:
        try:
            check(worksheet)
        except ValueError as exc:
            found.append(str(exc))
            raise

    monkeypatch.setattr(
        "ibkr_fa.output.netbenefits_quicko_fa_xlsx.require_cells_in_order", recording_check
    )

    with pytest.raises(QuickoFaXlsxError) as caught:
        _fill_quicko(template, output)

    _assert_fixed_quicko_refusal(caught.value, _unreadable_template(template))
    assert found == [message]
    assert not output.exists()


def test_quicko_template_refuses_formula_with_malformed_cached_value(tmp_path: Path) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    # Quicko reads the formula, so the scan must not decode its ignored cached value.
    _edit_quicko_sheet(
        template,
        '<c r="A2" s="1" t="n" />',
        '<c r="A2" t="inlineStr"><f>1</f><is bogus="x"><t>ignored</t></is></c>',
    )

    with pytest.raises(QuickoFaXlsxError, match="is not blank"):
        QuickoFaXlsxWriter().write(
            QuickoFaWorkbookInput(
                rows=(_quicko_row(),),
                account_number="I99999999",
                template_path=template,
            ),
            output,
        )

    assert not output.exists()


@pytest.mark.parametrize(
    ("cell_xml", "extra_row"),
    [
        pytest.param(
            '<c r="B3" t="inlineStr"><is bogus="x"><t>ignored</t></is></c>',
            "",
            id="invalid-string-attribute",
        ),
        pytest.param(
            '<c r="B3" t="inlineStr"><is><to_tree/></is></c>',
            "",
            id="invalid-string-child",
        ),
        pytest.param(
            '<c r="B3" t="d"><v>PT100000000000000S</v></c>',
            "",
            id="duration-overflow",
        ),
        pytest.param(
            '<c r="B3"><f t="shared" si="0">&quot;</f></c>',
            "",
            id="invalid-shared-formula",
        ),
        pytest.param(
            '<c r="B3"><f t="shared" si="0">A1</f></c>',
            '<row r="4"><c r="A4"><f t="shared" si="0"/></c></row>',
            id="shared-formula-out-of-range",
        ),
    ],
)
def test_quicko_template_refuses_malformed_content_after_nonblank_row(
    tmp_path: Path, cell_xml: str, extra_row: str
) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    # The full order scan reaches content after the old blank check's first refusal.
    _edit_quicko_sheet(
        template, '<c r="A2" s="1" t="n" />', '<c r="A2" t="inlineStr"><is><t>nonblank</t></is></c>'
    )
    _edit_quicko_sheet(template, '<c r="B3" s="1" t="n" />', cell_xml)
    if extra_row:
        _edit_quicko_sheet(template, "</sheetData>", f"{extra_row}</sheetData>")

    with pytest.raises(QuickoFaXlsxError) as caught:
        _fill_quicko(template, output)

    # #1042: fixed text, never openpyxl's message about the cell.
    _assert_fixed_quicko_refusal(caught.value, _unreadable_template(template))
    assert not output.exists()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(zlib.error("invalid block type"), id="deflate"),
        pytest.param(lzma.LZMAError("Corrupt input data"), id="lzma"),
    ],
)
def test_quicko_template_refuses_late_sheet_decompression_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    _edit_quicko_sheet(
        template, '<c r="A2" s="1" t="n" />', '<c r="A2" t="inlineStr"><is><t>nonblank</t></is></c>'
    )
    original_parse = WorkSheetParser.parse

    def fail_after_rows(parser: WorkSheetParser):
        yield from original_parse(parser)
        raise error

    monkeypatch.setattr(WorkSheetParser, "parse", fail_after_rows)
    with pytest.raises(QuickoFaXlsxError) as caught:
        _fill_quicko(template, output)

    # #1042: fixed text, never the decompressor's message.
    _assert_fixed_quicko_refusal(caught.value, _unreadable_template(template))
    assert str(error) not in str(caught.value)
    assert not output.exists()


def test_quicko_template_refuses_hidden_formula_cells(tmp_path: Path) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    workbook = Workbook()
    instructions = workbook.active
    assert instructions is not None
    instructions.title = "Instructions"
    worksheet = workbook.create_sheet("Shares & Securities")
    worksheet.append(QUICKO_HEADERS)
    worksheet["A2"] = '=HYPERLINK("https://example.com", "not blank")'
    workbook.save(template)

    with pytest.raises(QuickoFaXlsxError, match="is not blank"):
        QuickoFaXlsxWriter().write(
            QuickoFaWorkbookInput(
                rows=(_quicko_row(),),
                account_number="I99999999",
                template_path=template,
            ),
            output,
        )

    assert not output.exists()


def test_quicko_row_whose_country_the_template_lacks_refuses_in_fixed_text(
    tmp_path: Path,
) -> None:
    """#1042: names the Quicko row, never the holding's country."""
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    german = replace(
        _quicko_row(),
        country_name="Germany",
        country_code="49",
        source_country_iso2=CountryCode("DE"),
        source_isin=Isin("DE99966B1094"),
    )

    with pytest.raises(QuickoFaXlsxError) as caught:
        _fill_quicko(template, output, _quicko_row(), german)

    assert str(caught.value) == (
        "Quicko row 2: the template's country list has no entry for the holding's country"
    )
    assert "germany" not in str(caught.value).casefold()
    assert not output.exists()


def test_quicko_template_with_other_sheets_refuses_without_naming_them(tmp_path: Path) -> None:
    """#1042: the sheet names are template content, so the refusal names only the
    sheets Quicko's template has."""
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    workbook = Workbook()
    instructions = workbook.active
    assert instructions is not None
    instructions.title = "Instructions"
    workbook.create_sheet("Synthetic Holder Two").append(QUICKO_HEADERS)
    workbook.save(template)

    with pytest.raises(QuickoFaXlsxError) as caught:
        _fill_quicko(template, output)

    assert str(caught.value) == (
        "Quicko template must contain exactly the Instructions and Shares & Securities sheets, "
        "in that order"
    )
    _assert_no_quicko_markers(str(caught.value))
    assert not output.exists()


# Failures the template check expects from the workbook loader, each built fresh
# with markers in its message and file name (#1042).
_TEMPLATE_LOAD_FAILURES = [
    pytest.param(lambda: BadZipFile(_marked_text()), id="bad-zip"),
    pytest.param(lambda: KeyError(_marked_text()), id="key-error"),
    pytest.param(lambda: ET.ParseError(_marked_text()), id="xml-parse-error"),
    pytest.param(lambda: ValueError(_marked_text()), id="value-error"),
    pytest.param(lambda: IndexError(_marked_text()), id="index-error"),
    pytest.param(lambda: InvalidFileException(_marked_text()), id="invalid-file"),
    pytest.param(lambda: zlib.error(_marked_text()), id="deflate-error"),
    pytest.param(lambda: lzma.LZMAError(_marked_text()), id="lzma-error"),
]

# OSErrors reading the template, each with the errno name its refusal prints.
_TEMPLATE_OS_ERRORS = [
    pytest.param(_eacces, "EACCES", id="eacces"),
    pytest.param(_no_errno, "unknown error", id="no-errno"),
]


def _fail_template_load(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    """Make the template check's workbook loader raise ``failure``, chained."""

    def load(*_args: object, **_kwargs: object) -> NoReturn:
        _raise_marked(failure)

    monkeypatch.setattr("ibkr_fa.output.netbenefits_quicko_fa_xlsx.load_workbook", load)


@pytest.mark.parametrize("make", _TEMPLATE_LOAD_FAILURES)
def test_quicko_template_load_failure_refuses_in_fixed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Callable[[], Exception]
) -> None:
    """#1042: the template path and fixed text, never the loader's message, its
    file name or its cause, and no exception chain."""
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    _fail_template_load(monkeypatch, make())

    with pytest.raises(QuickoFaXlsxError) as caught:
        _fill_quicko(template, output)

    _assert_fixed_quicko_refusal(caught.value, _unreadable_template(template))
    assert not output.exists()


@pytest.mark.parametrize(("make", "errno_name"), _TEMPLATE_OS_ERRORS)
def test_quicko_template_load_os_error_refuses_with_its_errno_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make: Callable[[], OSError],
    errno_name: str,
) -> None:
    """#1042: the template path and the errno name, never the OSError's text,
    file name or cause."""
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    _fail_template_load(monkeypatch, make())

    with pytest.raises(QuickoFaXlsxError) as caught:
        _fill_quicko(template, output)

    _assert_fixed_quicko_refusal(
        caught.value, f"cannot read Quicko template {template} ({errno_name})"
    )
    assert not output.exists()


def test_quicko_template_load_crash_propagates_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1042: a loader failure the check doesn't expect is a crash, not a
    Quicko refusal. Its catch list never held RuntimeError, so this passes on
    the pre-#1042 code too; the CLI test of the crash is the one that changed."""
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    failure = _crash()
    _fail_template_load(monkeypatch, failure)

    with pytest.raises(RuntimeError) as caught:
        _fill_quicko(template, output)

    assert caught.value is failure
    assert not output.exists()


def _fail_template_scan(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    def parse(_parser: WorkSheetParser) -> NoReturn:
        _raise_marked(failure)

    monkeypatch.setattr(WorkSheetParser, "parse", parse)


@pytest.mark.parametrize("make", _TEMPLATE_LOAD_FAILURES)
def test_quicko_template_scan_format_failure_refuses_in_fixed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Callable[[], Exception]
) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    _fail_template_scan(monkeypatch, make())

    with pytest.raises(QuickoFaXlsxError) as caught:
        _fill_quicko(template, output)

    _assert_fixed_quicko_refusal(caught.value, _unreadable_template(template))
    assert not output.exists()


@pytest.mark.parametrize(("make", "errno_name"), _TEMPLATE_OS_ERRORS)
def test_quicko_template_scan_os_error_refuses_with_its_errno_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make: Callable[[], OSError],
    errno_name: str,
) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    _fail_template_scan(monkeypatch, make())

    with pytest.raises(QuickoFaXlsxError) as caught:
        _fill_quicko(template, output)

    _assert_fixed_quicko_refusal(
        caught.value, f"cannot read Quicko template {template} ({errno_name})"
    )
    assert not output.exists()


@pytest.mark.parametrize("kind", [RuntimeError, AssertionError])
def test_quicko_template_scan_crash_propagates_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: type[Exception]
) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    failure = kind(_marked_text())
    _fail_template_scan(monkeypatch, failure)

    with pytest.raises(kind) as caught:
        _fill_quicko(template, output)

    assert caught.value is failure
    assert not output.exists()


# openpyxl's warnings can quote template text, and its out-of-range date cell reads
# as the text "#VALUE!", so any warning but the allowed one refuses (#1103).
_QUICKO_WARNING_MARKER = "Synthetic Holder Two U1234567"


def _quicko_refusal_without_escaped_warnings(
    capsys: pytest.CaptureFixture[str], template: Path, output: Path
) -> QuickoFaXlsxError:
    """The refusal filling ``template`` raises; no warning escapes, nothing reaches
    stderr and no output is written."""
    with warnings.catch_warnings(record=True) as escaped:
        warnings.simplefilter("always")
        with pytest.raises(QuickoFaXlsxError) as caught:
            _fill_quicko(template, output)
    assert escaped == []
    assert capsys.readouterr().err == ""
    assert not output.exists()
    return caught.value


@pytest.mark.parametrize(
    ("sheet", "coordinate"),
    [
        pytest.param("Shares & Securities", "A2", id="shares-and-securities"),
        pytest.param("Instructions", "B33", id="instructions-country-list"),
    ],
)
def test_quicko_template_date_styled_number_out_of_range_refuses_in_fixed_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], sheet: str, coordinate: str
) -> None:
    # openpyxl warns with the cell's number, then reads it as "#VALUE!": the blank
    # check would refuse it in other words, and the country list would accept it.
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"

    def overflow(workbook: Workbook) -> None:
        cell = workbook[sheet][coordinate]
        cell.value = 1e100
        cell.number_format = "yyyy-mm-dd"

    write_quicko_template(template, edit=overflow)

    error = _quicko_refusal_without_escaped_warnings(capsys, template, output)

    _assert_fixed_quicko_refusal(error, _unreadable_template(template))
    assert "1e+100" not in str(error)
    assert "1e100" not in str(error)


def test_quicko_template_unusable_print_area_refuses_in_fixed_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # openpyxl warns with the defined name's text while it loads the workbook.
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"

    def unusable_print_area(workbook: Workbook) -> None:
        workbook["Shares & Securities"].defined_names["_xlnm.Print_Area"] = DefinedName(
            "_xlnm.Print_Area", attr_text=_QUICKO_WARNING_MARKER
        )

    write_quicko_template(template, edit=unusable_print_area)

    error = _quicko_refusal_without_escaped_warnings(capsys, template, output)

    _assert_fixed_quicko_refusal(error, _unreadable_template(template))


def test_quicko_template_any_warning_refuses_in_fixed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)

    def warn_then_nothing(_parser: WorkSheetParser) -> Iterator[tuple[int, list[dict[str, Any]]]]:
        warnings.warn(_QUICKO_WARNING_MARKER, stacklevel=1)
        yield from ()

    monkeypatch.setattr(WorkSheetParser, "parse", warn_then_nothing)

    error = _quicko_refusal_without_escaped_warnings(capsys, template, output)

    _assert_fixed_quicko_refusal(error, _unreadable_template(template))


def test_quicko_template_without_named_styles_is_filled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Owner decision (#1103): openpyxl's fixed "no default style" warning is allowed.
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    with ZipFile(template) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}
    styles = payloads["xl/styles.xml"].decode("utf-8")
    edited = re.sub(r"<cellStyles\b.*?</cellStyles>", "", styles, flags=re.DOTALL)
    assert edited != styles
    payloads["xl/styles.xml"] = edited.encode("utf-8")
    with ZipFile(template, "w") as package:
        for info in infos:
            package.writestr(info, payloads[info.filename])
    with warnings.catch_warnings(record=True) as raised:
        warnings.simplefilter("always")
        load_workbook(template, read_only=True).close()
    assert [(w.category, str(w.message)) for w in raised] == [(UserWarning, ALLOWED_WARNING)]

    with warnings.catch_warnings(record=True) as escaped:
        warnings.simplefilter("always")
        _fill_quicko(template, output)

    assert escaped == []
    assert capsys.readouterr().err == ""
    with warnings.catch_warnings():
        # The filled workbook keeps the template's styles, so it warns the same way.
        warnings.simplefilter("ignore")
        filled = load_workbook(output)
    assert filled["Shares & Securities"]["A2"].value == "EXAMPLE CORP"


def _fail_package_read(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> list[object]:
    """Make the writer's read of the template package (after the template check,
    which opens it through openpyxl) raise ``failure``, chained; returns the
    paths it was asked to open."""
    opened: list[object] = []

    def open_package(path: object, *_args: object, **_kwargs: object) -> NoReturn:
        opened.append(path)
        _raise_marked(failure)

    monkeypatch.setattr("ibkr_fa.output.netbenefits_quicko_fa_xlsx.ZipFile", open_package)
    return opened


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: BadZipFile(_marked_text()), id="bad-zip"),
        pytest.param(lambda: KeyError(_marked_text()), id="key-error"),
        pytest.param(lambda: zlib.error(_marked_text()), id="deflate-error"),
        pytest.param(lambda: lzma.LZMAError(_marked_text()), id="lzma-error"),
        pytest.param(
            lambda: UnicodeDecodeError("utf-8", b"\xff", 0, 1, _marked_text()),
            id="unicode-error",
        ),
    ],
)
def test_quicko_template_package_read_failure_refuses_in_fixed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Callable[[], Exception]
) -> None:
    """#1042: the template path and fixed text, never the failure's text, file
    name or cause."""
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    opened = _fail_package_read(monkeypatch, make())

    with pytest.raises(QuickoFaXlsxError) as caught:
        _fill_quicko(template, output)

    assert opened == [template]
    _assert_fixed_quicko_refusal(caught.value, f"Quicko template {template} package is invalid")
    assert not output.exists()


@pytest.mark.parametrize(("make", "errno_name"), _TEMPLATE_OS_ERRORS)
def test_quicko_template_package_read_os_error_refuses_with_its_errno_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make: Callable[[], OSError],
    errno_name: str,
) -> None:
    """#1042: the template path and the errno name, never the OSError's text,
    file name or cause."""
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    opened = _fail_package_read(monkeypatch, make())

    with pytest.raises(QuickoFaXlsxError) as caught:
        _fill_quicko(template, output)

    assert opened == [template]
    _assert_fixed_quicko_refusal(
        caught.value, f"cannot read Quicko template {template} ({errno_name})"
    )
    assert not output.exists()


def test_quicko_template_package_read_crash_propagates_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1042: a failure the package read doesn't expect is a crash, not a
    Quicko refusal. Its catch list never held RuntimeError, so this passes on
    the pre-#1042 code too; the CLI test of the crash is the one that changed."""
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    failure = _crash()
    _fail_package_read(monkeypatch, failure)

    with pytest.raises(RuntimeError) as caught:
        _fill_quicko(template, output)

    assert caught.value is failure
    assert not output.exists()


# --- #1076: attribute order and the filled-sheet check ------------------------

_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _filled_sheet_xml(output: Path) -> bytes:
    with ZipFile(output) as package:
        return package.read("xl/worksheets/sheet2.xml")


def _row_references(output: Path, row_number: str) -> list[str]:
    """Every cell reference in row ``row_number`` of the filled sheet, in file order."""
    root = ET.fromstring(_filled_sheet_xml(output))
    (row,) = [row for row in root.iter(f"{_SHEET_NS}row") if row.get("r") == row_number]
    return [cell.get("r", "") for cell in row.iter(f"{_SHEET_NS}c")]


def _filled_cell_style(output: Path, reference: str) -> str | None:
    root = ET.fromstring(_filled_sheet_xml(output))
    (cell,) = [cell for cell in root.iter(f"{_SHEET_NS}c") if cell.get("r") == reference]
    return cell.get("s")


def _assert_cells_in_order(output: Path) -> None:
    workbook = load_workbook(output, read_only=True)
    try:
        require_cells_in_order(workbook["Shares & Securities"])
    finally:
        workbook.close()


def test_quicko_cell_with_style_before_reference_is_filled_in_place(tmp_path: Path) -> None:
    """#1076: a template cell listing ``s`` before ``r`` is replaced once and keeps
    its style, so the sheet matches the unedited template's output."""
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    unedited = tmp_path / "unedited.xlsx"
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    _fill_quicko(template, unedited)
    _edit_quicko_sheet(template, '<c r="A2" s="1" t="n" />', '<c s="1" r="A2" t="n" />')

    _fill_quicko(template, output)

    assert _row_references(output, "2").count("A2") == 1
    assert _filled_cell_style(output, "A2") == "1"
    assert load_workbook(output)["Shares & Securities"]["A2"].value == "EXAMPLE CORP"
    _assert_cells_in_order(output)
    assert _filled_sheet_xml(output) == _filled_sheet_xml(unedited)


def test_quicko_cell_insert_finds_an_anchor_whose_style_comes_first() -> None:
    sheet = '<row r="2"><c s="1" r="C2"/></row>'

    updated = _replace_cell(sheet, "A2", '<c r="A2"><v>1</v></c>')

    assert updated == '<row r="2"><c r="A2"><v>1</v></c><c s="1" r="C2"/></row>'


@pytest.mark.parametrize(
    ("attributes", "style"),
    [
        ('s="1"', "1"),
        ('synthetic:s="0"', None),
        ('s="1" synthetic:s="0"', "1"),
        ('synthetic:s="0" s="1"', "1"),
    ],
)
def test_quicko_cell_style_reads_only_the_cells_own_s_attribute(
    attributes: str, style: str | None
) -> None:
    # #1076: a namespaced attribute ending in ``:s`` is not the cell's style, so
    # the cell keeps its own style even where main took the namespaced value.
    tag = f'<c r="A2" xmlns:synthetic="urn:synthetic" {attributes}/>'
    assert netbenefits_quicko_fa_xlsx._cell_style(tag, "A2") == style


def test_quicko_row_with_spans_before_reference_takes_an_inserted_cell(tmp_path: Path) -> None:
    """#1076: the row is found by its ``r`` after ``spans``, and the income cell the
    template lacks lands after L2."""
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    _edit_quicko_sheet(template, '<row r="2">', '<row spans="1:14" r="2">')
    row = replace(_quicko_row(), gross_amount_paid_credited_inr=Decimal("10"))

    _fill_quicko(template, output, row)

    assert _row_references(output, "2") == [*(f"{column}2" for column in "ABCDEFGHIJKL"), "N2"]
    assert load_workbook(output)["Shares & Securities"]["N2"].value == 10
    _assert_cells_in_order(output)


def test_quicko_self_closing_row_with_spans_before_reference_takes_the_cell() -> None:
    sheet = '<row spans="1:14" r="2"/><row r="3"></row>'

    updated = _replace_cell(sheet, "A2", '<c r="A2"><v>1</v></c>')

    assert updated == '<row spans="1:14" r="2"><c r="A2"><v>1</v></c></row><row r="3"></row>'


@pytest.mark.parametrize(
    ("edit", "reference", "style"),
    [
        pytest.param(
            ('<c r="I2" s="1" t="n" />', '<c r="I2" s="0" t="n" />'),
            "I2",
            "1",
            id="restyled-purchase-date",
        ),
        pytest.param(('<c r="I2" s="1" t="n" />', ""), "I2", "1", id="no-purchase-date-cell"),
        pytest.param(None, "N2", None, id="no-income-cell"),
    ],
)
def test_quicko_filled_cell_style_follows_the_template(
    tmp_path: Path, edit: tuple[str, str] | None, reference: str, style: str | None
) -> None:
    """#1076: the purchase date always takes the account-number cell's style; any
    other inserted cell the template lacks gets none. The fill passes the new check."""
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    if edit is not None:
        _edit_quicko_sheet(template, *edit)
    row = replace(_quicko_row(), gross_amount_paid_credited_inr=Decimal("10"))

    _fill_quicko(template, output, row)

    assert _filled_cell_style(output, reference) == style
    _assert_cells_in_order(output)


def _filled_element(sheet: str, tag: str, reference: str) -> str:
    """The first ``<tag r="reference" …>`` element of ``sheet``, as written."""
    match = re.search(rf'<{tag} r="{reference}"[^>]*?(?:/>|>.*?</{tag}>)', sheet)
    assert match is not None, reference
    return match.group()


def _repeat(tag: str, reference: str) -> Callable[[str], str]:
    """Place a copy of the element right after it."""

    def fault(sheet: str) -> str:
        element = _filled_element(sheet, tag, reference)
        return sheet.replace(element, element * 2, 1)

    return fault


def _swap(tag: str, first: str, second: str) -> Callable[[str], str]:
    """Store the element ``second`` before its neighbour ``first``."""

    def fault(sheet: str) -> str:
        pair = _filled_element(sheet, tag, first) + _filled_element(sheet, tag, second)
        assert pair in sheet
        swapped = _filled_element(sheet, tag, second) + _filled_element(sheet, tag, first)
        return sheet.replace(pair, swapped, 1)

    return fault


def _edit_cell(reference: str, old: str, new: str) -> Callable[[str], str]:
    def fault(sheet: str) -> str:
        cell = _filled_element(sheet, "c", reference)
        assert old in cell
        return sheet.replace(cell, cell.replace(old, new, 1), 1)

    return fault


def _row_entry_refusal(row_entry: int) -> str:
    return (
        f"Quicko workbook check failed: row entry {row_entry} of the filled Shares & "
        "Securities sheet repeats an earlier row or is out of order"
    )


def _cell_entry_refusal(cell_entry: int, row_entry: int) -> str:
    return (
        f"Quicko workbook check failed: cell entry {cell_entry} of row entry {row_entry} of "
        "the filled Shares & Securities sheet repeats a cell, belongs to another row, or is "
        "out of column order"
    )


def _written_cell_refusal(reference: str) -> str:
    return (
        f"Quicko workbook check failed: cell {reference} of the filled Shares & Securities "
        "sheet is missing, or holds a value or style other than the one written"
    )


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        pytest.param(
            lambda sheet: sheet.replace("</sheetData>", f"<{_QUICKO_MESSAGE_MARKER}</sheetData>"),
            "Quicko workbook check failed: the filled Shares & Securities sheet is not "
            "well-formed XML",
            id="not-xml",
        ),
        pytest.param(_repeat("row", "2"), _row_entry_refusal(3), id="repeated-row"),
        pytest.param(_swap("row", "2", "3"), _row_entry_refusal(3), id="rows-out-of-order"),
        pytest.param(_repeat("row", "1"), _row_entry_refusal(2), id="header-row-copy"),
        pytest.param(_repeat("c", "J2"), _cell_entry_refusal(11, 2), id="repeated-cell"),
        pytest.param(_swap("c", "A2", "B2"), _cell_entry_refusal(2, 2), id="cells-out-of-order"),
        pytest.param(
            _edit_cell("E2", 'r="E2"', 'r="E3"'), _cell_entry_refusal(5, 2), id="other-row-cell"
        ),
        pytest.param(_repeat("c", "A1"), _cell_entry_refusal(2, 1), id="header-first-cell-copy"),
        pytest.param(_repeat("c", "A2"), _cell_entry_refusal(2, 2), id="holding-first-cell-copy"),
        pytest.param(_repeat("c", "A3"), _cell_entry_refusal(2, 3), id="unused-first-cell-copy"),
        pytest.param(
            lambda sheet: sheet.replace(_filled_element(sheet, "c", "A2"), "", 1),
            _written_cell_refusal("A2"),
            id="written-cell-left-out",
        ),
        pytest.param(
            _edit_cell("J2", "<v>66503</v>", "<v>66504</v>"),
            _written_cell_refusal("J2"),
            id="wrong-value",
        ),
        pytest.param(
            _edit_cell("J2", "<v>66503</v>", '<v bogus="1">66503</v>'),
            _written_cell_refusal("J2"),
            id="value-attributes",
        ),
        pytest.param(
            _edit_cell("J2", "<v>66503</v>", "<v>66503<extra/></v>"),
            _written_cell_refusal("J2"),
            id="value-child",
        ),
        pytest.param(
            _edit_cell("J2", "<v>66503</v>", "extra<v>66503</v>"),
            _written_cell_refusal("J2"),
            id="cell-text",
        ),
        pytest.param(
            _edit_cell("J2", "<v>66503</v>", "<v>66503</v>extra"),
            _written_cell_refusal("J2"),
            id="value-tail",
        ),
        pytest.param(
            _edit_cell("A2", "<v>14</v>", "<v>0</v>"),
            _written_cell_refusal("A2"),
            id="other-shared-string",
        ),
        pytest.param(
            _edit_cell("A2", 's="1"', 's="0"'), _written_cell_refusal("A2"), id="wrong-style"
        ),
    ],
)
def test_quicko_filled_sheet_check_refuses_a_faulty_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: Callable[[str], str],
    message: str,
) -> None:
    """#1076: the check reads the filled sheet before it is published and refuses
    in fixed text that names no value, account number or XML."""
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    populate = netbenefits_quicko_fa_xlsx._populate_sheet

    def faulty_populate(*args: Any, **kwargs: Any) -> tuple[str, Any]:
        sheet, written = populate(*args, **kwargs)
        faulty = fault(sheet).replace(
            "</worksheet>", f"<!--{_QUICKO_MESSAGE_MARKER}--></worksheet>"
        )
        assert faulty != sheet
        return faulty, written

    monkeypatch.setattr(netbenefits_quicko_fa_xlsx, "_populate_sheet", faulty_populate)
    row = replace(_quicko_row(), name_of_entity="Synthetic Holder Two")

    with pytest.raises(QuickoFaXlsxError) as caught:
        QuickoFaXlsxWriter().write(
            QuickoFaWorkbookInput(rows=(row,), account_number="U1234567", template_path=template),
            output,
        )

    _assert_fixed_quicko_refusal(caught.value, message)
    assert not output.exists()
    assert not tuple(tmp_path.glob(f".{output.name}.*.tmp"))


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("Synthetic\rHolder Two", id="cr"),
        pytest.param("Synthetic\r\nHolder Two", id="crlf"),
        pytest.param("Synthetic_x005F_Holder Two", id="escaped-underscore"),
        pytest.param("Synthetic\ufffeHolder Two", id="noncharacter-fffe"),
        pytest.param("Synthetic\uffffHolder Two", id="noncharacter-ffff"),
        pytest.param("Synthetic\ud800Holder Two", id="surrogate"),
    ],
)
def test_quicko_filled_sheet_check_refuses_changed_shared_text(tmp_path: Path, name: str) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    # The row refuses a CR itself (#1367), so it is built past its checks.
    row = _unchecked(_quicko_row(), name_of_entity=name)

    with pytest.raises(QuickoFaXlsxError) as caught:
        QuickoFaXlsxWriter().write(
            QuickoFaWorkbookInput(rows=(row,), account_number="U1234567", template_path=template),
            output,
        )

    _assert_fixed_quicko_refusal(caught.value, _written_cell_refusal("A2"))
    assert not output.exists()
    assert not tuple(tmp_path.glob(f".{output.name}.*.tmp"))


@pytest.mark.parametrize(
    ("suffix", "name"),
    [
        pytest.param(
            '<rPh sb="0" eb="1"><t>Synthetic Holder Two</t></rPh>',
            "Description*Synthetic Holder Two",
            id="phonetic-annotation",
        ),
        pytest.param("\n", "Description*\n", id="formatting-whitespace"),
    ],
)
def test_quicko_filled_sheet_check_uses_shared_text_without_metadata(
    tmp_path: Path, suffix: str, name: str
) -> None:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    output = tmp_path / "schedule-fa-quicko.xlsx"
    write_quicko_template(template)
    with ZipFile(template) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}
    payloads["xl/sharedStrings.xml"] = payloads["xl/sharedStrings.xml"].replace(
        b"</si>", f"{suffix}</si>".encode(), 1
    )
    temporary = template.with_suffix(".tmp")
    with ZipFile(temporary, "w") as package:
        for info in infos:
            package.writestr(info, payloads[info.filename])
    temporary.replace(template)
    # The row refuses a line feed itself (#1367), so it is built past its checks.
    row = _unchecked(_quicko_row(), name_of_entity=name)

    with pytest.raises(QuickoFaXlsxError) as caught:
        _fill_quicko(template, output, row)

    _assert_fixed_quicko_refusal(caught.value, _written_cell_refusal("A2"))
    assert not output.exists()
    assert not tuple(tmp_path.glob(f".{output.name}.*.tmp"))


def test_quicko_existing_shared_string_carriage_return_is_preserved() -> None:
    value = "Synthetic\rHolder Two"
    shared = netbenefits_quicko_fa_xlsx._shared_strings(
        f'<sst xmlns="{_SHEET_NS[1:-1]}"><si><t>Synthetic&#13;Holder Two</t></si></sst>'
    )
    cell = netbenefits_quicko_fa_xlsx._render_cell("A2", style=None, value=value, shared=shared)
    sheet = (
        f'<worksheet xmlns="{_SHEET_NS[1:-1]}"><sheetData><row r="2">{cell}</row>'
        "</sheetData></worksheet>"
    ).encode()

    assert shared.values == [value]
    assert netbenefits_quicko_fa_xlsx._shared_strings(shared.payload().decode()).values == [value]
    netbenefits_quicko_fa_xlsx._check_filled_sheet(sheet, {"A2": (value, None)}, shared.values)


def test_happy_path_emits_one_a3_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report, out = _run(tmp_path, monkeypatch, quicko_workbook=True)
    assert report.rows_written == 1
    assert report.security_name == "EXAMPLE CORP"
    assert report.close_sources == ((CloseSource.OVERRIDE, report.close_sources[0][1]),)

    row = _row_dict(out)
    assert row["Country/Region name"] == "1"
    assert row["Country Name and Code"] == "2"
    assert row["Name of entity"] == "EXAMPLE CORP"
    assert row["Address of entity"] == "1 Sample Issuer Street Sample City XX"
    assert row["ZIP Code"] == "00000"
    assert row["Nature of entity"] == "Listed company shares"
    assert row["Date of acquiring the interest"] == "2025-12-31"
    assert not any(key.startswith("_audit:") for key in row)
    # Bought ON Dec-31 → A3 initial == peak == closing (all from the same
    # FMV/close), and Quicko Purchase Value is the A3 initial value.
    initial = row["Initial value of the investment"]
    assert initial != ""
    assert int(initial) == 66503
    assert row["Peak value of investment during the Period"] == initial
    assert row["Closing balance"] == initial
    assert (
        row["Total gross amount paid/credited with respect to the holding during the period"] == "0"
    )
    assert (
        row["Total gross proceeds from sale or redemption of investment during the period"] == "0"
    )
    assert report.quicko_output_path == tmp_path / "a3-quicko.xlsx"
    workbook = load_workbook(report.quicko_output_path, data_only=True)
    assert workbook.sheetnames == ["Instructions", "Shares & Securities"]
    sheet = workbook["Shares & Securities"]
    assert [cell.value for cell in sheet[1]] == list(QUICKO_HEADERS)
    assert [cell.value for cell in sheet[2]] == [
        "EXAMPLE CORP",
        "I99999999",
        "United States Of America",
        "1 Sample Issuer Street",
        None,
        "Sample City",
        "XX",
        "00000",
        "31/12/2025",
        int(initial),
        int(initial),
        int(initial),
        None,
        None,
    ]
    assert sheet["B2"].number_format == "@"
    assert sheet["I2"].number_format == "@"


@pytest.mark.parametrize("quicko_headers", [QUICKO_HEADERS, None], ids=["template", "no-template"])
def test_unrequested_quicko_workbook_is_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quicko_headers: tuple[str, ...] | None
) -> None:
    report, out = _run(tmp_path, monkeypatch, quicko_headers=quicko_headers)
    assert out.exists()
    assert (tmp_path / "a3-a2.csv").exists()
    assert report.quicko_output_path is None
    assert not (tmp_path / "a3-quicko.xlsx").exists()


@pytest.mark.parametrize(("quicko_headers", "message"), _BAD_QUICKO_TEMPLATES)
def test_requested_quicko_workbook_with_a_bad_template_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    quicko_headers: tuple[str, ...] | None,
    message: Callable[[Path], str],
) -> None:
    with pytest.raises(QuickoFaXlsxError) as caught:
        _run(tmp_path, monkeypatch, quicko_headers=quicko_headers, quicko_workbook=True)
    assert str(caught.value) == message(tmp_path / "bundle")
    assert not any((tmp_path / name).exists() for name in _FA_OUTPUTS)


@pytest.mark.parametrize("format_id", ["review-csv", CBDT_JSON_FRAGMENT_FORMAT_ID])
def test_quicko_workbook_request_needs_filing_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, format_id: str
) -> None:
    with pytest.raises(NetBenefitsFaError, match="filing-csv"):
        _run(tmp_path, monkeypatch, format_id=format_id, quicko_workbook=True)
    assert not any((tmp_path / name).exists() for name in _FA_OUTPUTS)


def test_a2_emitted_with_identity_and_derived_closing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Table A2 (the account itself) emits a sibling file; its closing balance is the
    # held securities (Σ A3 closing) + the proven-$0 cash; peak remains manual.
    report, out = _run(tmp_path, monkeypatch)
    assert report.a2_output_path is not None
    assert report.a2_output_path.exists()
    assert report.a2_manual_fill_columns == ("Peak Balance During the Period",)
    a3 = _row_dict(out)
    a2 = _a2_row_dict(report.a2_output_path)
    assert a2["Account Number"] == "I99999999"
    assert a2["Closing balance"] == a3["Closing balance"]  # securities + proven $0 cash
    assert a2["Peak Balance During the Period"] == ""
    assert not any(key.startswith("_audit:") for key in a2)
    assert "Fidelity Stock Plan Services" in a2["Name of financial institution"]


def test_review_csv_format_keeps_audit_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, out = _run(tmp_path, monkeypatch, format_id="review-csv")
    assert any(key.startswith("_audit:") for key in _row_dict(out))
    assert report.a2_output_path is not None
    assert any(key.startswith("_audit:") for key in _a2_row_dict(report.a2_output_path))
    assert report.quicko_output_path is None
    assert not (tmp_path / "a3-quicko.xlsx").exists()


def test_a2_refuses_when_statement_shows_income(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A dividend dated on/before Dec-31 means the account held cash → refuse the
    # whole run (A2 mandatory; the NetBenefitsA2Error surfaces as NetBenefitsFaError).
    div = StatementDividend(dt.date(2025, 12, 31), "EXAMPLE CORP", "99999E104", Decimal("1.00"))
    cover = _covering(dividends=(div,))
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(tmp_path, monkeypatch, statement_data={"cover": cover})
    # #1042: names the statement and row, never the dividend's date; the proof's
    # refusal is re-raised from None, so no chain carries it.
    assert str(caught.value) == _a2_dividend_refusal(statement=1)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    _assert_absent(str(caught.value), "2025-12-31")


def test_a2_refusal_numbers_statements_by_period_start_with_year_end_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1042: the A2 proof gets every statement, year-end reports included, by
    period start (path order only breaks ties), so the year-end report in
    b-ye.pdf is statement 1 and the December statement in a-dec.pdf statement 2."""
    div = StatementDividend(dt.date(2025, 12, 14), "EXAMPLE CORP", "99999E104", Decimal("1.00"))
    year_end = replace(
        _stmt_data(dt.date(2025, 1, 1), dt.date(2025, 12, 31)), report_type=ReportType.YEAR_END
    )
    december = _covering(dividends=(div,))
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            statements={
                "a-dec.pdf": "STOCK PLAN SERVICES REPORT\n::december::\n",
                "b-ye.pdf": "STOCK PLAN SERVICES REPORT\n::year-end::\n",
            },
            statement_data={"december": december, "year-end": year_end},
        )
    assert str(caught.value) == _a2_dividend_refusal(statement=2)
    _assert_absent(str(caught.value), "2025-12-14")


def test_a3_write_and_cleanup_failures_name_what_is_left_and_roll_back_a2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Make A3 writing and cleanup fail; A2 must go, and the refusal names the A3
    # path left behind, keeping only the original exception's class (#1211).
    class _BoomWriter(StubWriter):
        def write(self, rows: object, path: Path) -> None:
            path.mkdir()
            raise KeyboardInterrupt

    patch_writers(monkeypatch, Subcommand.FA, _BoomWriter)
    with pytest.raises(OutputRemovalError, match="may still be there") as exc_info:
        _run(tmp_path, monkeypatch)
    assert str(tmp_path / "a3.csv") in str(exc_info.value)
    assert_replaced_unchained(exc_info.value, KeyboardInterrupt)
    assert (tmp_path / "a3.csv").is_dir()
    assert not (tmp_path / "a3-a2.csv").exists()
    assert not (tmp_path / "a3-quicko.xlsx").exists()


def _quicko_write_refusal(quicko: Path, errno_name: str) -> str:
    """The run's refusal for an ``OSError`` writing ``quicko`` (#1042)."""
    return f"could not write the requested Quicko workbook {quicko} ({errno_name})"


# Failures of the Quicko write that refuse the run, each built fresh, with the
# refusal for the Quicko path, or None when the domain error itself propagates.
# An OSError is named by its errno name, never its text, file name or cause (#1042).
_QUICKO_REFUSALS = [
    pytest.param(_eacces, lambda quicko: _quicko_write_refusal(quicko, "EACCES"), id="io-error"),
    pytest.param(
        _no_errno,
        lambda quicko: _quicko_write_refusal(quicko, "unknown error"),
        id="io-error-no-errno",
    ),
    pytest.param(lambda: QuickoFaXlsxError("bad template"), None, id="quicko-error"),
    pytest.param(
        lambda: WriterContractError(
            violation="formula_trigger",
            row_index=1,
            cell_index=5,
            trigger="-",
        ),
        None,
        id="writer-contract",
    ),
]

# Any unexpected exception is a crash, not a Quicko refusal: it propagates unchanged (#1042).
_QUICKO_CRASH = pytest.param(_crash, None, id="unexpected")


def _crash_site(module: ModuleType, code: str) -> str:
    """The CLI's crash site for a ``RuntimeError`` raised by the call on the one
    line of ``module`` that contains ``code``: the innermost ``ibkr_fa`` frame,
    since the test's own failing stand-in is not in the package."""
    path = Path(str(module.__file__))
    [number] = [
        index
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if code in line
    ]
    package = Path(str(ibkr_fa.__file__)).parent.parent
    return f"RuntimeError at {path.relative_to(package).as_posix()}:{number}"


def _quicko_write_crash_site() -> str:
    """The crash site of an exception the Quicko writer raises: its call in the
    outputs module."""
    return _crash_site(
        ibkr_fa.pipeline_netbenefits_fa_outputs, "QuickoFaXlsxWriter.from_context(ctx).write("
    )


def _fail_quicko_write(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, *, leave_directory: bool = False
) -> None:
    """Make the Quicko writer leave something at its path, then raise ``failure``
    from a cause holding :data:`_QUICKO_CAUSE_MARKER`."""

    def boom(self: QuickoFaXlsxWriter, value: QuickoFaWorkbookInput, path: Path) -> None:
        if leave_directory:
            path.mkdir()
        else:
            path.write_bytes(b"partial")
        _raise_marked(failure)

    monkeypatch.setattr(QuickoFaXlsxWriter, "write", boom)


@pytest.mark.parametrize(("make", "refusal"), _QUICKO_REFUSALS)
def test_quicko_write_failure_removes_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make: Callable[[], Exception],
    refusal: Callable[[Path], str] | None,
) -> None:
    failure = make()
    _fail_quicko_write(monkeypatch, failure)
    with pytest.raises(IbkrFaError) as exc_info:
        _run(tmp_path, monkeypatch, quicko_workbook=True)
    if refusal is None:
        assert exc_info.value is failure
    else:
        assert isinstance(exc_info.value, NetBenefitsFaError)
        _assert_fixed_quicko_refusal(exc_info.value, refusal(tmp_path / "a3-quicko.xlsx"))
    assert not any((tmp_path / name).exists() for name in _FA_OUTPUTS)


def test_quicko_write_crash_propagates_and_removes_every_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1042: a crash in the Quicko write is not turned into a Quicko refusal; it
    propagates unchanged, and every output still goes."""
    failure = _crash()
    _fail_quicko_write(monkeypatch, failure)
    with pytest.raises(RuntimeError) as exc_info:
        _run(tmp_path, monkeypatch, quicko_workbook=True)
    assert exc_info.value is failure
    assert not any((tmp_path / name).exists() for name in _FA_OUTPUTS)


@pytest.mark.parametrize(("make", "refusal"), [*_QUICKO_REFUSALS, _QUICKO_CRASH])
def test_quicko_write_failure_names_an_output_it_could_not_remove(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make: Callable[[], Exception],
    refusal: Callable[[Path], str] | None,
) -> None:
    failure = make()
    _fail_quicko_write(monkeypatch, failure, leave_directory=True)
    with pytest.raises(OutputRemovalError, match="may still be there") as exc_info:
        _run(tmp_path, monkeypatch, quicko_workbook=True)
    quicko = tmp_path / "a3-quicko.xlsx"
    assert str(quicko) in str(exc_info.value)
    _assert_no_quicko_markers(str(exc_info.value))
    # The removal error keeps none of the failure, nor of its Quicko refusal
    # (#1211). Its cause stands in for the first crash the failure holds: the
    # failure itself, or the ValueError holding the cause marker.
    crash = RuntimeError if isinstance(failure, RuntimeError) else ValueError
    replaced = () if refusal is None else (refusal(quicko),)
    markers = (
        _QUICKO_MARKERS if isinstance(failure, (OSError, RuntimeError)) else _QUICKO_MARKERS[2:]
    )
    assert_replaced_unchained(exc_info.value, crash, markers=(*markers, *replaced))
    if crash is RuntimeError:
        assert str(exc_info.value.__cause__) == _quicko_write_crash_site()
    assert quicko.is_dir()
    assert not (tmp_path / "a3.csv").exists()
    assert not (tmp_path / "a3-a2.csv").exists()


def test_missing_zip_is_manual_fill_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = BASE_CONFIG.replace('entity_zip = "00000"\n', "")
    assert config != BASE_CONFIG
    report, _ = _run(tmp_path, monkeypatch, config=config, quicko_workbook=True)
    assert report.quicko_output_path is not None
    workbook = load_workbook(report.quicko_output_path, data_only=True)
    assert workbook["Shares & Securities"]["H2"].value is None
    assert report.quicko_zip_manual_fill_rows == 1
    # #1330: the A3 upload's blank ZIP is named too, not only Quicko's.
    assert report.a3_manual_fill_columns == ("ZIP Code",)
    lines = ibkr_fa.cli._render_netbenefits_fa_report(report).splitlines()
    assert lines.count(A3_ZIP_LINE) == 1
    assert lines.count(QUICKO_ZIP_LINE) == 1


def test_refused_run_clears_stale_prior_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Clear current outputs and the pre-#430 legacy A2 name.
    out = tmp_path / "schedule-fa-a3.csv"
    a2 = tmp_path / "schedule-fa-a2.csv"
    legacy_a2 = tmp_path / "schedule-fa-a3-a2.csv"
    quicko = tmp_path / "schedule-fa-quicko.xlsx"
    for path in (out, a2, legacy_a2, quicko):
        path.write_text("stale", encoding="utf-8")
    div = StatementDividend(dt.date(2025, 12, 31), "EXAMPLE CORP", "99999E104", Decimal("1.00"))
    cover = _covering(dividends=(div,))
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            statement_data={"cover": cover},
            output_name=out.name,
        )
    assert str(caught.value) == _a2_dividend_refusal(statement=1)
    assert not any(path.exists() for path in (out, a2, legacy_a2, quicko))


def test_bad_output_target_still_clears_stale_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "schedule-fa-a3.csv"
    out.mkdir()
    sidecars = (
        tmp_path / "schedule-fa-a2.csv",
        tmp_path / "schedule-fa-a3-a2.csv",
        tmp_path / "schedule-fa-quicko.xlsx",
    )
    for path in sidecars:
        path.write_text("stale", encoding="utf-8")

    with pytest.raises(OutputRemovalError, match="could not remove the previous output") as exc:
        _run(tmp_path, monkeypatch, output_name=out.name)

    assert str(out) in str(exc.value)
    assert out.is_dir()
    assert not any(path.exists() for path in sidecars)


def test_early_refusal_also_clears_stale_prior_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An EARLY refusal (a non-gap-window intra-CY closed-lot overlap, before the
    # per-lot resolution work) must also clear stale prior outputs — the clear runs
    # before any refusing step.
    out = tmp_path / "a3.csv"
    a2 = tmp_path / "a3-a2.csv"
    out.write_text("stale a3", encoding="utf-8")
    a2.write_text("stale a2", encoding="utf-8")
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(tmp_path, monkeypatch, closed_csv=CLOSED_INTRA_CY_CSV)
    assert str(caught.value) == f"closed-lots data row 1 {_CLOSED_LOT_OVERLAP_REFUSAL}"
    assert not out.exists()
    assert not a2.exists()


def _run_refused_before_bundle(
    tmp_path: Path,
    *,
    config: str = BASE_CONFIG,
    config_name: str = "user.toml",
    calendar_year: int = 2025,
    format_id: str = "filing-csv",
    output_name: str = "schedule-fa-a3.csv",
) -> None:
    """Run netbenefits-fa where a check before bundle discovery refuses (the
    input directory does not even exist)."""
    cfg = tmp_path / config_name
    cfg.write_text(config, encoding="utf-8")
    run_netbenefits_fa(
        input_dir=tmp_path / "no-bundle",
        config_path=cfg,
        output_path=tmp_path / output_name,
        calendar_year=calendar_year,
        fetch_prices=False,
        format_id=format_id,
        itr_form="ITR-2",
    )


_EARLY_REFUSALS = [
    pytest.param({"config": "not = [valid toml"}, ConfigError, id="malformed-config"),
    pytest.param(
        {"config": BASE_CONFIG.split("[netbenefits]", maxsplit=1)[0]},
        NetBenefitsFaError,
        id="no-netbenefits-block",
    ),
    pytest.param({"calendar_year": 1989}, NetBenefitsFaError, id="calendar-year-out-of-range"),
]


@pytest.mark.parametrize(("kwargs", "error"), _EARLY_REFUSALS)
def test_refusal_before_config_checks_clears_stale_csv_outputs(
    tmp_path: Path, kwargs: dict[str, Any], error: type[Exception]
) -> None:
    # Clearing runs before config load and the year check, so even these refusals
    # leave no prior-run A3 / A2 / legacy-A2 / Quicko output behind.
    stale = (
        tmp_path / "schedule-fa-a3.csv",
        tmp_path / "schedule-fa-a2.csv",
        tmp_path / "schedule-fa-a3-a2.csv",
        tmp_path / "schedule-fa-quicko.xlsx",
    )
    for path in stale:
        path.write_text("stale", encoding="utf-8")
    with pytest.raises(error):
        _run_refused_before_bundle(tmp_path, **kwargs)
    assert not any(path.exists() for path in stale)


@pytest.mark.parametrize(("kwargs", "error"), _EARLY_REFUSALS)
def test_refusal_before_config_checks_clears_stale_json_fragment(
    tmp_path: Path, kwargs: dict[str, Any], error: type[Exception]
) -> None:
    fragment = tmp_path / "schedule-fa.json"
    fragment.write_text(
        json.dumps({"_ibkrFa": {"format": CBDT_JSON_FRAGMENT_FORMAT_ID}}), encoding="utf-8"
    )
    with pytest.raises(error):
        _run_refused_before_bundle(
            tmp_path,
            format_id=CBDT_JSON_FRAGMENT_FORMAT_ID,
            output_name=fragment.name,
            **kwargs,
        )
    assert not fragment.exists()


@pytest.mark.parametrize(
    "config_name",
    [
        "schedule-fa-a3.csv",
        "schedule-fa-a2.csv",
        "schedule-fa-a3-a2.csv",
        "schedule-fa-quicko.xlsx",
    ],
)
def test_output_that_is_the_config_file_is_refused_untouched(
    tmp_path: Path, config_name: str
) -> None:
    # Clearing first must never delete the config before it is read; a malformed
    # config proves the refusal comes before any parse.
    targets = (
        tmp_path / "schedule-fa-a3.csv",
        tmp_path / "schedule-fa-a2.csv",
        tmp_path / "schedule-fa-a3-a2.csv",
        tmp_path / "schedule-fa-quicko.xlsx",
    )
    for path in targets:
        path.write_bytes(f"stale {path.name}\r\n".encode())
    config = "not = [valid toml"
    (tmp_path / config_name).write_text(config, encoding="utf-8")
    before = {path: path.read_bytes() for path in targets}
    with pytest.raises(OutputCollisionError, match=r"--config .* is the same file as"):
        _run_refused_before_bundle(tmp_path, config=config, config_name=config_name)
    assert {path: path.read_bytes() for path in targets} == before


@pytest.mark.parametrize("other_cash", [False, True], ids=["no-other-cash", "other-cash"])
def test_cli_refuses_unread_cash_on_the_dec31_statement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    other_cash: bool,
) -> None:
    """#1088: the real statement parser reads the unpriced Dec-31 report, whose
    clean form proves the Dec-31 cash zero. With an ``Other Cash Activity`` block the
    parser refuses, so the run exits 4 and writes no A2 row."""
    extra = OTHER_CASH_ACTIVITY if other_cash else ""
    texts = {
        "espp-purchase_2025-12-31.pdf": CONF_DEC,
        "statement_2025.pdf": dec31_unpriced_text(extra),
        # #1151: the reports cover through ten days after Dec-31 and print CONF_DEC's row.
        "statement_2026-01.pdf": _JANUARY_REPORT,
    }
    bundle = _build_dir(
        tmp_path, statements={"statement_2025.pdf": "", "statement_2026-01.pdf": ""}
    )
    patch_pdftotext(monkeypatch, texts)
    cfg = tmp_path / "user.toml"
    cfg.write_text(BASE_CONFIG, encoding="utf-8")
    code = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(tmp_path / "a3.csv"),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )
    err = capsys.readouterr().err
    if not other_cash:
        # Written, with only the A2 peak left to fill in by hand.
        assert code == 3, err
        assert (tmp_path / "a3-a2.csv").exists()
        return
    assert code == 4
    assert not any((tmp_path / name).exists() for name in _FA_OUTPUTS)
    assert err == (
        "error: ParseError in section 'statement statement_2025.pdf' at row 0: "
        + DATED_LINE_UNREAD
        + "\n"
    )


def test_statement_pdf_is_ignored_not_misclassified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _ = _run(
        tmp_path,
        monkeypatch,
        statements={"statement_2026-02.pdf": STATEMENT_TEXT},
    )
    assert report.rows_written == 1


def test_unrelated_csv_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = _build_dir(tmp_path)
    (bundle / "notes.csv").write_text("foo,bar\n1,2\n", encoding="utf-8")
    _patch_pdf(monkeypatch, {"espp-purchase_2025-12-31.pdf": CONF_DEC, **_DEFAULT_STATEMENTS})
    cfg = tmp_path / "user.toml"
    cfg.write_text(BASE_CONFIG, encoding="utf-8")
    report = run_netbenefits_fa(
        input_dir=bundle,
        config_path=cfg,
        output_path=tmp_path / "a3.csv",
        calendar_year=2025,
        fetch_prices=False,
    )
    assert report.rows_written == 1


def _bundle_with_extra_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, text: str
) -> tuple[Path, Path]:
    """The default bundle plus one more CSV; returns the bundle and the config."""
    bundle = _build_dir(tmp_path)
    (bundle / name).write_text(text, encoding="utf-8")
    _patch_pdf(monkeypatch, {"espp-purchase_2025-12-31.pdf": CONF_DEC, **_DEFAULT_STATEMENTS})
    cfg = tmp_path / "user.toml"
    cfg.write_text(BASE_CONFIG, encoding="utf-8")
    return bundle, cfg


def test_cli_refuses_a_renamed_closed_lots_export_and_clears_stale_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#911: the bundle's other closed-lots export is empty, so ignoring the
    renamed one would drop its sale."""
    bundle, cfg = _bundle_with_extra_csv(
        tmp_path, monkeypatch, "View closed lots (1).csv", CLOSED_RENAMED_PROCEEDS_CSV
    )
    out = tmp_path / "a3.csv"
    out.write_text("stale", encoding="utf-8")
    code = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )
    assert code == 4
    assert not out.exists()
    err = capsys.readouterr().err
    assert err == f"error: View closed lots (1).csv {CLOSED_RENAMED_PROCEEDS_REFUSAL}\n"
    # #1041: the renamed header cell is file content.
    assert "proceeds (usd)" not in err.casefold()


def test_open_lots_export_with_a_changed_header_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #911: netbenefits-cg ignores it (it has only open-lots columns); FA reads open lots.
    bundle, cfg = _bundle_with_extra_csv(
        tmp_path, monkeypatch, "View open lots (1).csv", OPEN_RECASED_CSV
    )
    with pytest.raises(NetBenefitsBundleError) as caught:
        run_netbenefits_fa(
            input_dir=bundle,
            config_path=cfg,
            output_path=tmp_path / "a3.csv",
            calendar_year=2025,
            fetch_prices=False,
        )
    # #1041: names the expected header and counts the unexpected column; the
    # recased 'Holding Period' cell is file content and is never printed (checked
    # case-sensitively: the expected 'Holding period' is a code constant).
    assert str(caught.value) == (
        "View open lots (1).csv looks like a Fidelity lots export but matches neither lots "
        "header: compared with the open-lots header ('Date acquired', 'Quantity', "
        "'Cost basis', 'Cost basis/share', 'Value', 'Gain/loss', 'Sale availability date', "
        "'Transfer availability date', 'Grant date', 'Share source', 'Holding period'), "
        "missing: 'Holding period'; unexpected: 1 column(s). If Fidelity changed the export, "
        "ibkr-fa must be updated to read it; otherwise move the file out of the input directory"
    )
    assert "Holding Period" not in str(caught.value)


def test_uppercase_extensions_are_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A browser may save the exports with uppercase extensions on some OSes;
    # discovery must be case-insensitive (else a confusing false refusal).
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "View open lots.CSV").write_text(OPEN_CSV, encoding="utf-8")
    (bundle / "View closed lots.CSV").write_text(CLOSED_EMPTY_CSV, encoding="utf-8")
    (bundle / "espp-purchase_2025-12-31.PDF").write_bytes(b"%PDF-1.4 placeholder")
    (bundle / "statement_2025-12.PDF").write_bytes(b"%PDF-1.4 placeholder")
    _patch_pdf(
        monkeypatch,
        {
            "espp-purchase_2025-12-31.PDF": CONF_DEC,
            "statement_2025-12.PDF": "STOCK PLAN SERVICES REPORT\n::cover::\n",
        },
    )
    cfg = tmp_path / "user.toml"
    cfg.write_text(BASE_CONFIG, encoding="utf-8")
    report = run_netbenefits_fa(
        input_dir=bundle,
        config_path=cfg,
        output_path=tmp_path / "a3.csv",
        calendar_year=2025,
        fetch_prices=False,
    )
    assert report.rows_written == 1


def test_no_fetch_uses_override_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report, out = _run(tmp_path, monkeypatch, fetch_prices=False)
    assert report.close_sources[0][0] is CloseSource.OVERRIDE
    # For a Dec-31-only lot, peak == closing (single-day holding) and is filled
    # from the resolved close even without fetched prices.
    row = _row_dict(out)
    peak = row["Peak value of investment during the Period"]
    assert peak != ""
    assert peak == row["Closing balance"]


def test_peak_equals_closing_when_yahoo_differs_from_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Yahoo close (213.465) differs from the accepted override (213.47) by under
    # tolerance; peak must still equal closing (never a cent below it), derived
    # from the resolved override, not the Yahoo series.
    report, out = _run(tmp_path, monkeypatch, fetcher_close="213.465")
    assert report.close_sources[0][0] is CloseSource.OVERRIDE
    row = _row_dict(out)
    assert row["Peak value of investment during the Period"] == row["Closing balance"]


# --- refusals (it's taxes — never be wrong) -----------------------------------


def test_missing_closed_csv_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(NetBenefitsBundleError) as caught:
        _run(tmp_path, monkeypatch, closed_csv=None)
    assert str(caught.value) == (
        "expected exactly one closed-lots CSV (required; its absence leaves Dec-31 quantity "
        "unprovable) in the input directory; found 0 (none)"
    )


def test_two_open_csvs_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = _build_dir(tmp_path)
    (bundle / "View open lots (copy).csv").write_text(OPEN_CSV, encoding="utf-8")
    _patch_pdf(monkeypatch, {"espp-purchase_2025-12-31.pdf": CONF_DEC, **_DEFAULT_STATEMENTS})
    cfg = tmp_path / "user.toml"
    cfg.write_text(BASE_CONFIG, encoding="utf-8")
    with pytest.raises(NetBenefitsBundleError) as caught:
        run_netbenefits_fa(
            input_dir=bundle,
            config_path=cfg,
            output_path=tmp_path / "a3.csv",
            calendar_year=2025,
            fetch_prices=False,
        )
    # #1041: each CSV by its path relative to --input.
    assert str(caught.value) == (
        "expected exactly one open-lots CSV in the input directory; found 2 "
        "(View open lots (copy).csv, View open lots.csv)"
    )


def test_gap_window_inconsistent_with_confirmation_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A gap-window lot whose quantity does NOT reconcile to the confirmation (here the
    # full 3.4820 is still open AND a 1.0 gap lot claims to have been held) must refuse
    # — the confirmation proves only 3.4820 shares were purchased on Dec-31.
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(tmp_path, monkeypatch, closed_csv=CLOSED_GAP_CSV)
    # #1042: names the folded position's lot rows and the confirmation, never the
    # confirmed (3.482) or folded (4.4820) share count.
    assert str(caught.value) == (
        "the position folded from open-lots data row 2 and closed-lots data row 1: the "
        f"Quantity differs from the shares on {_CONF_1}"
    )
    _assert_absent(str(caught.value), "3.482", "4.482")


def test_gap_window_partial_sale_reconstructs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A partial sale: 2.4820 still open + a 1.0 gap-window share sold the next CY
    # reconstruct to the full 3.4820-share Dec-31 holding — identical A3 to the
    # never-sold happy path (closing == initial, one row).
    _happy, happy_out = _run(_mkdir(tmp_path / "happy"), monkeypatch)
    recon, recon_out = _run(
        _mkdir(tmp_path / "recon"),
        monkeypatch,
        open_csv=OPEN_CSV_PARTIAL,
        closed_csv=CLOSED_GAP_PARTIAL_CSV,
    )
    assert recon.rows_written == 1
    row = _row_dict(recon_out)
    assert row["Date of acquiring the interest"] == "2025-12-31"
    assert row["Closing balance"] == _row_dict(happy_out)["Closing balance"]
    assert row["Closing balance"] == row["Initial value of the investment"]
    assert row["Closing balance"] != ""


def test_gap_window_full_sale_reconstructs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole Dec-31 purchase sold the next CY (no surviving open lot) is still
    # reconstructed from the gap-window closed lot + its confirmation — never silently
    # omitted.
    _happy, happy_out = _run(_mkdir(tmp_path / "happy"), monkeypatch)
    recon, recon_out = _run(
        _mkdir(tmp_path / "recon"),
        monkeypatch,
        open_csv=OPEN_CSV_NO_DEC,
        closed_csv=CLOSED_GAP_FULL_CSV,
    )
    assert recon.rows_written == 1
    assert _row_dict(recon_out)["Closing balance"] == _row_dict(happy_out)["Closing balance"]


def test_gap_window_split_mismatch_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A stock split (or any basis change) between Dec-31 and the sale leaves the
    # gap-window row's per-share cost no longer matching the confirmation price →
    # refuse rather than file a wrong Dec-31 quantity.
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            open_csv=OPEN_CSV_PARTIAL,
            closed_csv=CLOSED_GAP_SPLIT_CSV,
        )
    # #1042: names the closed-lots row and the confirmation, never the lot's date,
    # its per-share cost (96.06), the price (192.12) or the tolerance.
    assert str(caught.value) == (
        "closed-lots data row 1: the gap-window lot's per-share cost differs from the purchase "
        f"price on {_CONF_1} by more than the rounding tolerance; a split/transfer or foreign "
        "row between Dec-31 and the sale can't be reconciled — refusing"
    )
    _assert_absent(str(caught.value), "2025-12-31", "96.06", "192.12", "0.10606")


def test_missing_confirmation_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the Mar confirmation present; the Dec lot has no matching confirmation.
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(tmp_path, monkeypatch, confirmations={"espp-purchase_2026-03-31.pdf": CONF_MAR})
    # #1042: names the open-lots row, never the lot's acquisition date.
    assert str(caught.value) == (
        "open-lots data row 2: no purchase confirmation covers this position; the FMV cost "
        "basis cannot be sourced"
    )
    _assert_absent(str(caught.value), "2025-12-31")


def test_participant_mismatch_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conf = dec_confirmation(participant="I12345678")
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(tmp_path, monkeypatch, confirmations={"espp-purchase_2025-12-31.pdf": conf})
    # #1042: names the confirmation and the config field, never either number.
    assert str(caught.value) == (
        f"{_CONF_1}: the PARTICIPANT NO. differs from netbenefits.participant_no in user.toml"
    )
    _assert_absent(str(caught.value), "I12345678", "I99999999")


def test_quantity_mismatch_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An internally-consistent open lot (qty 2.0, cost basis 384.24 = 2.0 x 192.12)
    # whose quantity disagrees with the confirmation's 3.482 shares.
    bad_open = OPEN_CSV.replace(
        "Dec-31-2025,3.4820,668.96,192.12,626.24,-42.72,-,-,Nov-03-2025,SP,Short\n",
        "Dec-31-2025,2.0000,384.24,192.12,300.00,-84.24,-,-,Nov-03-2025,SP,Short\n",
    )
    assert bad_open != OPEN_CSV
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(tmp_path, monkeypatch, open_csv=bad_open)
    # #1042: names the open-lots row and the confirmation, never either quantity.
    assert str(caught.value) == (
        f"open-lots data row 2: the Quantity differs from the shares on {_CONF_1}"
    )
    _assert_absent(str(caught.value), "3.482", "2.0000")


def test_discount_mismatch_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = BASE_CONFIG.replace('espp_discount_pct = "10"', 'espp_discount_pct = "25"')
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(tmp_path, monkeypatch, config=cfg)
    # #1042: names the confirmation and the config field, never the price
    # (192.12), the FMV per share (743.30 / 3.482), the discount or the deviation.
    assert str(caught.value) == (
        f"{_CONF_1}: the purchase price is not the FMV per share less the "
        "netbenefits.espp_discount_pct discount, within the rounding tolerance"
    )
    _assert_absent(str(caught.value), "192.12", "213.4", "25%", "160.1")


def test_close_disagrees_with_fmv_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Drop the config override so the close comes from Yahoo only; feed a Yahoo
    # close that disagrees with the confirmation FMV → refuse.
    cfg = BASE_CONFIG.split("[[netbenefits.price_overrides]]", maxsplit=1)[0]
    with pytest.raises(ClosingPriceError) as caught:
        _run(tmp_path, monkeypatch, config=cfg, fetcher_close="500.00")
    # #1042: names the lot row, the close's source and the confirmation, never the
    # close (3.482 x 500.00 = 1741.00), the FMV (743.30) or their difference.
    assert str(caught.value) == (
        "open-lots data row 2: the Dec-31 close (yahoo_fallback) differs from the Market Value "
        f"at Purchase on {_CONF_1} by more than the tolerance; {NB_BY_HAND}"
    )
    _assert_absent(str(caught.value), "1741", "743.3", "997.7")


def test_override_on_an_earlier_trading_day_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #1165: an override on an earlier trading day within the 3-day window (here
    # Dec-29) is passed on, not silently ignored, and refuses: CY2025's required
    # US market session is Dec-31.
    cfg = BASE_CONFIG.replace("price_date = 2025-12-31", "price_date = 2025-12-29")
    with pytest.raises(ClosingPriceError) as caught:
        _run(tmp_path, monkeypatch, config=cfg, fetch_prices=False)
    assert str(caught.value) == NB_OVERRIDE_SESSION_REFUSAL


@pytest.mark.parametrize(
    ("fetch_prices", "price_fetcher"),
    [(False, None), (True, session_fetcher())],
    ids=["no-market-evidence", "beside-a-valid-dec31-close"],
)
def test_override_off_the_required_session_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_prices: bool,
    price_fetcher: DailyCloseFetcher | None,
) -> None:
    # #1165: the Dec-30 override is selected and refused, never dropped for the
    # market close, even beside a Dec-31 close that matches the confirmation FMV.
    with pytest.raises(ClosingPriceError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            config=_marked_config(MARKER_SOURCE_DATE),
            confirmations=_MARKED_CONFIRMATIONS,
            fetch_prices=fetch_prices,
            price_fetcher=price_fetcher,
        )
    assert str(caught.value) == NB_OVERRIDE_SESSION_REFUSAL
    assert_markers_absent(
        caught.value, MARKER_SYMBOL, MARKER_PRICE, MARKER_SOURCE, MARKER_REASON, MARKER_SOURCE_DATE
    )


@pytest.mark.parametrize("dec30_close", ["213.47", MARKER_PRICE], ids=["fmv-close", "other-close"])
def test_market_close_from_an_earlier_session_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dec30_close: str
) -> None:
    # #1165: no override, and the series ends on Dec-30, so it has no close for
    # CY2025's required Dec-31 session; the Dec-30 close is never filed in its place.
    with pytest.raises(ClosingPriceError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            config=_marked_config(None),
            confirmations=_MARKED_CONFIRMATIONS,
            price_fetcher=session_fetcher(dec30=dec30_close, dec31=None),
        )
    assert str(caught.value) == NB_MARKET_SESSION_REFUSAL
    assert_markers_absent(caught.value, MARKER_SYMBOL, dec30_close, MARKER_SOURCE_DATE)


def test_filed_closing_value_is_the_required_session_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #1165: the series' Dec-30 close differs, and the filed A3 and A2 closing
    # values are the Dec-31 session close's, the same as the override-priced run's.
    _happy, happy_out = _run(_mkdir(tmp_path / "happy"), monkeypatch)
    report, out = _run(
        _mkdir(tmp_path / "market"),
        monkeypatch,
        config=_marked_config(None),
        confirmations=_MARKED_CONFIRMATIONS,
        price_fetcher=session_fetcher(dec30=MARKER_PRICE),
    )
    assert report.close_sources[0][0] is CloseSource.YAHOO_FALLBACK
    closing = _row_dict(out)["Closing balance"]
    assert closing == _row_dict(happy_out)["Closing balance"] == "66503"
    assert report.a2_output_path is not None
    assert _a2_row_dict(report.a2_output_path)["Closing balance"] == closing


def test_split_cache_refusal_clears_once_the_cache_file_is_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #1425: only a price-cache file on the current-split basis gives a series
    # with an unsafe split, and NetBenefits has no statement total to fall back
    # on. This one was fetched on the run's day, so it is reused. Moving it out
    # of the cache folder and fetching again, as the refusal says, clears it.
    cache_file = _cache_path(tmp_path / "cache", Ticker("XESP"))
    write_cache(
        cache_file,
        PriceSeries(
            ticker=Ticker("XESP"),
            currency="USD",
            closes={dt.date(2025, 12, 31): Decimal("213.47")},
            has_split_in_range=True,
            source="Synthetic current-split-basis source",
            fetched_at=PRICE_NOW,
        ),
        covered_start=dt.date(2024, 12, 1),
        covered_end=dt.date(2025, 12, 31),
        fetch_symbol="XESP",
    )
    with pytest.raises(ClosingPriceError) as caught:
        _run(tmp_path, monkeypatch, fetch_prices=False)
    assert str(caught.value) == (
        "split in range for 2025-12-31: Yahoo and any per-share override (x the current, "
        "rebased quantity) are unsafe and there is no broker statement total to fall back on; "
        "the price cache holds this security's closes on the current-split basis, so move its "
        "cache file, and any .lock beside it, out of the price cache directory and rerun with "
        "price fetching on"
    )
    assert not (tmp_path / "a3.csv").exists()

    cache_file.rename(_mkdir(tmp_path / "aside") / cache_file.name)
    # ``_run`` lays out a new bundle folder; the cache folder stays.
    (tmp_path / "bundle").rename(tmp_path / "refused-bundle")
    _report, out = _run(tmp_path, monkeypatch, fetch_prices=True)
    assert _row_dict(out)["Closing balance"] == "66503"


@pytest.mark.parametrize(
    "first_date", ["2025-12-31", "2025-12-29"], ids=["one-on-the-session", "both-off-the-session"]
)
def test_multiple_overrides_in_window_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_date: str
) -> None:
    # Two overrides within the carry-back window are ambiguous → refuse, before
    # the selected override's session is checked (#1165).
    extra = (
        "\n[[netbenefits.price_overrides]]\n"
        "price_date = 2025-12-30\n"
        'price_per_share_usd = "213.47"\n'
        'source = "dup"\n'
        'reason = "dup"\n'
    )
    config = BASE_CONFIG.replace("price_date = 2025-12-31", f"price_date = {first_date}")
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(tmp_path, monkeypatch, config=config + extra, fetch_prices=False)
    # #1042: names both config entries, never the valuation date.
    assert str(caught.value) == (
        "netbenefits.price_overrides[0] and netbenefits.price_overrides[1] are each dated "
        "within 3 days on/before the valuation date; the override is ambiguous"
    )
    _assert_absent(str(caught.value), "2025-12-31")


def test_held_across_cy_lot_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A lot acquired before Dec-31 has a non-empty in-CY held window → its
    # dividend can't be proven here (deferred to #I10).
    open_csv = OPEN_CSV.replace(
        "Dec-31-2025,3.4820,668.96,192.12,626.24,-42.72,-,-,Nov-03-2025,SP,Short\n",
        "Oct-15-2025,3.4820,668.96,192.12,626.24,-42.72,-,-,Jul-01-2025,SP,Short\n",
    )
    assert open_csv != OPEN_CSV
    conf = confirmation(
        purchase="OCT/15/2025",
        offering="JUL/01/2025 - OCT/15/2025",
        mv="743.30",
        contrib="668.96",
        shares="3.482",
        price="192.1200",
        gain="74.34",
        trade_settle="10-15-25 10-16-25",
    )
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            open_csv=open_csv,
            confirmations={"espp-purchase_2025-10-15.pdf": conf},
        )
    # #1042: names the lot row and the confirmation, never the purchase date.
    assert str(caught.value) == (
        f"open-lots data row 2: {_CONF_1} is not a Dec-31 purchase, so the lot is held across "
        "part of CY2025; its in-CY dividend can't be proven here — wire dividends via #I10"
    )
    _assert_absent(str(caught.value), "2025-10-15")


def test_no_netbenefits_block_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = BASE_CONFIG.split("[netbenefits]", maxsplit=1)[0]
    with pytest.raises(NetBenefitsFaError, match=r"no \[netbenefits\] block"):
        _run(tmp_path, monkeypatch, config=cfg)


def test_ambiguous_pdf_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A PDF matching neither marker can't be classified.
    with pytest.raises(NetBenefitsBundleError) as caught:
        _run(tmp_path, monkeypatch, confirmations={"mystery.pdf": "RANDOM CONTENT"})
    assert str(caught.value) == (
        "cannot classify PDF mystery.pdf: it matches neither confirmation nor statement "
        "markers; remove non-NetBenefits PDFs from the input directory"
    )


def test_input_not_a_directory_refuses(tmp_path: Path) -> None:
    not_dir = tmp_path / "nope.txt"
    not_dir.write_text("x", encoding="utf-8")
    cfg = tmp_path / "user.toml"
    cfg.write_text(BASE_CONFIG, encoding="utf-8")
    with pytest.raises(NetBenefitsBundleError, match="not a directory"):
        run_netbenefits_fa(
            input_dir=not_dir,
            config_path=cfg,
            output_path=tmp_path / "a3.csv",
            calendar_year=2025,
            fetch_prices=False,
        )


def test_symbol_mismatch_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conf = dec_confirmation(symbol="XQBN")
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(tmp_path, monkeypatch, confirmations={"espp-purchase_2025-12-31.pdf": conf})
    # #1042: names the confirmation and the config field, never either symbol.
    assert str(caught.value) == (
        f"{_CONF_1}: the SYMBOL differs from netbenefits.security.symbol in user.toml"
    )
    _assert_absent(str(caught.value), "XQBN", "XESP")


def test_cusip_mismatch_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conf = dec_confirmation(cusip="99993A108")
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(tmp_path, monkeypatch, confirmations={"espp-purchase_2025-12-31.pdf": conf})
    # #1042: names the confirmation and the config field, never either CUSIP.
    assert str(caught.value) == (
        f"{_CONF_1}: the CUSIP differs from netbenefits.security.cusip in user.toml"
    )
    _assert_absent(str(caught.value), "99993A108", "99999E104")


def test_contribution_mismatch_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Internally-consistent open lot (qty 3.482, cost/share 200.00 -> cost basis
    # 696.40) whose contribution disagrees with the confirmation's 668.96.
    bad_open = OPEN_CSV.replace(
        "Dec-31-2025,3.4820,668.96,192.12,626.24,-42.72,-,-,Nov-03-2025,SP,Short\n",
        "Dec-31-2025,3.4820,696.40,200.00,626.24,-70.16,-,-,Nov-03-2025,SP,Short\n",
    )
    assert bad_open != OPEN_CSV
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(tmp_path, monkeypatch, open_csv=bad_open)
    # #1042: names the lot row and the confirmation, never either amount.
    assert str(caught.value) == (
        f"open-lots data row 2: the Cost basis differs from the Accumulated Contributions on "
        f"{_CONF_1}"
    )
    _assert_absent(str(caught.value), "668.96", "696.4")


def test_price_mismatch_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Shares and contribution agree, but the confirmation's per-share price
    # (192.13) differs from the CSV's cost/share (192.12).
    conf = dec_confirmation(price="192.1300")
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(tmp_path, monkeypatch, confirmations={"espp-purchase_2025-12-31.pdf": conf})
    # #1042: names the lot row and the confirmation, never either price.
    assert str(caught.value) == (
        f"open-lots data row 2: the Cost basis/share differs from the purchase price on {_CONF_1}"
    )
    _assert_absent(str(caught.value), "192.13", "192.12")


def test_two_lots_same_acquire_date_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_csv = OPEN_CSV.replace(
        "Mar-31-2026,4.6315,755.31,163.08,832.98,77.67,-,-,Jan-05-2026,SP,Short\n",
        "Dec-31-2025,1.0000,192.12,192.12,170.00,-22.12,-,-,Nov-03-2025,SP,Short\n",
    )
    assert open_csv != OPEN_CSV
    with pytest.raises(NetBenefitsFaError, match="share an acquisition date"):
        _run(tmp_path, monkeypatch, open_csv=open_csv)


def test_two_confirmations_same_date_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(NetBenefitsBundleError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            confirmations={"conf-a.pdf": CONF_DEC, "conf-b.pdf": CONF_DEC},
        )
    # #1041: names both confirmations, never the shared purchase date (DEC/31/2025).
    message = str(caught.value)
    assert message == (
        "confirmations conf-a.pdf and conf-b.pdf share a purchase date; the bundle is ambiguous"
    )
    for date_form in ("2025", "12-31", "12/31", "dec"):
        assert date_form not in message.casefold()


def test_no_lot_in_calendar_year_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # CY2024: both fixture lots (2025-12-31, 2026-03-31) are after Dec-31-2024.
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(tmp_path, monkeypatch, calendar_year=2024)
    # #1042: the CLI's calendar year only, never the year-end date.
    assert str(caught.value) == (
        "no ESPP lot acquired on or before Dec-31; nothing to report for CY2024"
    )
    _assert_absent(str(caught.value), "2024-12-31")


def test_degenerate_calendar_year_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A year outside [1990, 2099] (here 0, which would crash date(...)) must
    # refuse cleanly, not escape as a raw ValueError.
    with pytest.raises(NetBenefitsFaError, match="out of the supported range"):
        _run(tmp_path, monkeypatch, calendar_year=0)


def test_in_cy_closed_lot_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A round-trip fully inside the CY (acquired AND sold within it) WAS held
    # during the CY and belongs on Schedule FA A3, but NetBenefits closed-lot A3
    # disclosure isn't wired yet (#I13) — refuse rather than silently omit it.
    # #1042: a lot sold before the CY (row 1) is skipped, so the refusal names row 2,
    # never the lot's dates or the year-end date.
    closed = (
        "\ufeffDate acquired,Quantity,Date sold or transferred,Proceeds,Cost basis,Gain/loss,Term\n"
        "Mar-31-2024,1.0000,Jun-15-2024,500.00,192.12,307.88,Short\n"
        "Mar-31-2025,1.0000,Jun-15-2025,500.00,192.12,307.88,Short\n"
        ",\n"
        "The values are displayed in USD\n"
    )
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(tmp_path, monkeypatch, closed_csv=closed)
    assert str(caught.value) == f"closed-lots data row 2 {_CLOSED_LOT_OVERLAP_REFUSAL}"
    _assert_absent(str(caught.value), "2025-03-31", "2025-06-15", "2025-12-31")


def test_pre_cy_closed_lot_does_not_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A closed lot fully BEFORE the CY (sold before Jan 1) was not held during the
    # CY, so it neither belongs on A3 nor undercuts the Dec-31 quantity → accepted.
    closed = (
        "\ufeffDate acquired,Quantity,Date sold or transferred,Proceeds,Cost basis,Gain/loss,Term\n"
        "Mar-31-2024,1.0000,Jun-15-2024,500.00,192.12,307.88,Short\n"
        ",\n"
        "The values are displayed in USD\n"
    )
    report, _ = _run(tmp_path, monkeypatch, closed_csv=closed)
    assert report.rows_written == 1


# --- ESPP perquisite Form-16 advisory (#I12) ----------------------------------


def _both_confirmations() -> dict[str, str]:
    return {
        "espp-purchase_2025-12-31.pdf": CONF_DEC,
        "espp-purchase_2026-03-31.pdf": CONF_MAR,
    }


def test_perquisite_advisories_for_both_fy_purchases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _ = _run(tmp_path, monkeypatch, confirmations=_both_confirmations())
    advs = report.perquisite_advisories
    assert [a.purchase_date for a in advs] == [dt.date(2025, 12, 31), dt.date(2026, 3, 31)]
    assert [a.perquisite_usd for a in advs] == [Decimal("74.34"), Decimal("83.92")]
    assert all(a.financial_year == "2025-26" for a in advs)
    assert all(a.perquisite_inr is not None and a.perquisite_inr > 0 for a in advs)


def test_perquisite_advisory_excludes_out_of_fy_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A purchase dated in FY 2026-27 (after this CY2025 run's FY 2025-26) must NOT
    # produce an advisory — the FY filter keeps cross-year reminders out.
    later = confirmation(
        purchase="JUN/30/2026",
        offering="APR/01/2026 - JUN/30/2026",
        mv="500.00",
        contrib="450.00",
        shares="1.0000",
        price="450.0000",
        gain="50.00",
        trade_settle="06-30-26 07-01-26",
    )
    report, _ = _run(
        tmp_path,
        monkeypatch,
        confirmations={**_both_confirmations(), "espp-purchase_2026-06-30.pdf": later},
    )
    dates = [a.purchase_date for a in report.perquisite_advisories]
    assert dt.date(2026, 6, 30) not in dates
    assert dates == [dt.date(2025, 12, 31), dt.date(2026, 3, 31)]


def test_out_of_fy_confirmation_before_dec31_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #1151: a purchase dated in FY 2024-25 is a lot bought before Dec-31, which the
    # A3 path can't support, so the run refuses rather than leave it out.
    old = confirmation(
        purchase="MAR/31/2025",
        offering="JAN/01/2025 - MAR/31/2025",
        mv="500.00",
        contrib="450.00",
        shares="1.0000",
        price="450.0000",
        gain="50.00",
        trade_settle="03-31-25 04-01-25",
    )
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            confirmations={**_both_confirmations(), "espp-purchase_2025-03-31.pdf": old},
        )
    assert str(caught.value) == (
        "confirmation 1 (by purchase date), purchase date: dated before Dec-31, so a lot was "
        "bought before or during CY2025; netbenefits-fa supports only an account whose lots "
        "were all bought on Dec-31"
    )
    assert not any((tmp_path / name).exists() for name in _FA_OUTPUTS)


def test_perquisite_advisory_refuses_foreign_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A bundle confirmation that belongs to a different participant must refuse
    # even when it is not the A3 lot's confirmation (don't remind on someone else's).
    foreign = mar_confirmation(participant="I12345678")
    out = tmp_path / "a3.csv"
    with pytest.raises(NetBenefitsFaError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            confirmations={
                "espp-purchase_2025-12-31.pdf": CONF_DEC,
                "espp-purchase_2026-03-31.pdf": foreign,
            },
        )
    # #1042: the Mar-31-2026 purchase is the second by purchase date; neither
    # participant number is printed.
    assert str(caught.value) == (
        "confirmation 2 (by purchase date): the PARTICIPANT NO. differs from "
        "netbenefits.participant_no in user.toml"
    )
    _assert_absent(str(caught.value), "I12345678", "I99999999")
    # Fail-closed: the foreign confirmation refuses BEFORE any A3 CSV is written.
    assert not out.exists()


def test_perquisite_advisory_cites_the_run_assessment_year(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #610: a CY2026 run's perquisite rate comes from the AY 2027-28 dividend policy,
    # not the legacy AY 2026-27 citation.
    class _CapturedCitationError(Exception):
        pass

    def capture(self: Rule115Policy, _day: dt.date) -> Decimal:
        raise _CapturedCitationError(self.policy_kind_str())

    monkeypatch.setattr("ibkr_fa.fx.policy_rule115.Rule115Policy.rate_for", capture)
    # The bundled TTBR archive does not reach Dec-31-2026.
    monkeypatch.setattr(
        "ibkr_fa.fx.policy_fa.FaExactDatePolicy.rate_for", lambda _self, _day: Decimal("90")
    )
    # The bundled calendar lists CY2025 only; Thursday Dec-31-2026 is a session (#1165).
    monkeypatch.setattr("ibkr_fa.compute.dec31_close.supports_calendar_year", lambda _year: True)
    conf = confirmation(
        purchase="DEC/31/2026",
        offering="NOV/03/2026 - DEC/31/2026",
        mv="743.30",
        contrib="668.96",
        shares="3.482",
        price="192.1200",
        gain="74.34",
        trade_settle="12-31-26 01-04-27",
    )
    open_csv = (
        "\ufeffDate acquired,Quantity,Cost basis,Cost basis/share,Value,Gain/loss,"
        "Sale availability date,Transfer availability date,Grant date,Share source,Holding period\n"
        "Dec-31-2026,3.4820,668.96,192.12,626.24,-42.72,-,-,Nov-03-2026,SP,Short\n"
        ",\n"
        "The values are displayed in USD\n"
    )
    # #1151: one report covers the lot proof's window and prints the purchase's row.
    covering = _stmt_data(
        dt.date(2026, 1, 1),
        dt.date(2027, 1, 31),
        purchases=(replace(_DEC31_ROW, settlement_date=dt.date(2027, 1, 4)),),
    )
    with pytest.raises(_CapturedCitationError) as captured:
        _run(
            tmp_path,
            monkeypatch,
            open_csv=open_csv,
            confirmations={"espp-purchase_2026-12-31.pdf": conf},
            statement_data={"cy2026": covering},
            statements={"statement_2026.pdf": "STOCK PLAN SERVICES REPORT\n::cy2026::\n"},
            calendar_year=2026,
        )
    assert str(captured.value) == policy_citations.for_ay(2027).POLICY_KIND_RULE_115_2E


def test_perquisite_advisory_degrades_when_ttbr_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A missing purchase-date TTBR must NOT fail the FA run — the advisory degrades
    # to a USD-only reminder (INR None).
    def boom(self: object, day: dt.date) -> Decimal:
        raise MissingFxRateError(currency="USD", last_available=None, max_carry_forward_days=14)

    monkeypatch.setattr("ibkr_fa.fx.policy_rule115.Rule115Policy.rate_for", boom)
    bundle = _build_dir(tmp_path)
    _patch_pdf(monkeypatch, {"espp-purchase_2025-12-31.pdf": CONF_DEC, **_DEFAULT_STATEMENTS})
    cfg = tmp_path / "user.toml"
    cfg.write_text(BASE_CONFIG, encoding="utf-8")
    code = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(tmp_path / "a3.csv"),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )
    assert code == 3
    stdout = capsys.readouterr().out
    assert "preceding-month-end TTBR unavailable" in stdout
    assert "USD 74.34" in stdout


def test_perquisite_advisory_honors_configured_fx_carry_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With max_carry_forward_days = 0 the 2025-12-31 purchase's preceding-month-end
    # (Sun 2025-11-30, unpublished) has no TTBR, so the advisory degrades to USD-only
    # — proving the configured FX carry-back is threaded into the perquisite policy
    # (the default 14-day window would otherwise resolve it).
    config = "[fx]\nmax_carry_forward_days = 0\n\n" + BASE_CONFIG
    report, _ = _run(tmp_path, monkeypatch, config=config)
    assert [a.perquisite_inr for a in report.perquisite_advisories] == [None]


def test_cli_renders_perquisite_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _build_dir(tmp_path, confirmations=_both_confirmations())
    _patch_pdf(monkeypatch, {**_both_confirmations(), **_DEFAULT_STATEMENTS})
    cfg = tmp_path / "user.toml"
    cfg.write_text(BASE_CONFIG, encoding="utf-8")
    code = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(tmp_path / "a3.csv"),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )
    assert code == 3
    stdout = capsys.readouterr().out
    assert "ESPP salary perquisite" in stdout
    assert "FY 2025-26" in stdout
    assert "Form 16" in stdout
    assert "§49(2AA)" in stdout
    assert "2026-03-31" in stdout  # both purchases rendered


# --- CLI ----------------------------------------------------------------------


def test_cli_emits_a3_and_exits_manual_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _build_dir(tmp_path)
    _patch_pdf(monkeypatch, {"espp-purchase_2025-12-31.pdf": CONF_DEC, **_DEFAULT_STATEMENTS})
    cfg = tmp_path / "user.toml"
    cfg.write_text(BASE_CONFIG, encoding="utf-8")
    out = tmp_path / "a3.csv"
    code = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )
    assert code == 3
    stdout = capsys.readouterr().out
    assert "Table A2 (the account itself)" in stdout
    assert "MANUAL-FILL" in stdout
    assert "NOT filing-complete" in stdout
    assert out.exists()


def _main_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *extra: str,
    quicko_headers: tuple[str, ...] | None = QUICKO_HEADERS,
) -> int:
    """Run ``netbenefits-fa`` through ``main`` on the default bundle, writing ``a3.csv``."""
    bundle = _build_dir(tmp_path, quicko_headers=quicko_headers)
    _patch_pdf(monkeypatch, {"espp-purchase_2025-12-31.pdf": CONF_DEC, **_DEFAULT_STATEMENTS})
    cfg = tmp_path / "user.toml"
    cfg.write_text(BASE_CONFIG, encoding="utf-8")
    return main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(tmp_path / "a3.csv"),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
            *extra,
        ]
    )


@pytest.mark.parametrize(("make", "refusal"), _QUICKO_REFUSALS)
def test_cli_quicko_write_failure_exits_domain_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    make: Callable[[], Exception],
    refusal: Callable[[Path], str] | None,
) -> None:
    failure = make()
    _fail_quicko_write(monkeypatch, failure)
    assert _main_fa(tmp_path, monkeypatch, "--quicko-workbook") == 4
    expected = str(failure) if refusal is None else refusal(tmp_path / "a3-quicko.xlsx")
    err = capsys.readouterr().err
    assert err == f"error: {expected}\n"
    _assert_no_quicko_markers(err)
    assert not any((tmp_path / name).exists() for name in _FA_OUTPUTS)


# Where a crash in the Quicko write can start, and the CLI's crash site for it:
# the writer itself, its template loader and worksheet scan, and the package read (#1042).
_QUICKO_CRASHES = [
    pytest.param(
        lambda monkeypatch: _fail_quicko_write(monkeypatch, _crash()),
        _quicko_write_crash_site,
        id="writer",
    ),
    pytest.param(
        lambda monkeypatch: _fail_template_load(monkeypatch, _crash()),
        lambda: _crash_site(netbenefits_quicko_fa_xlsx, "workbook = load_workbook(path"),
        id="template-load",
    ),
    pytest.param(
        lambda monkeypatch: _fail_template_scan(monkeypatch, _crash()),
        lambda: _crash_site(ibkr_fa.xlsx_checks, "for row, cells in parser.parse():"),
        id="template-scan",
    ),
    pytest.param(
        lambda monkeypatch: _fail_package_read(monkeypatch, _crash()),
        lambda: _crash_site(netbenefits_quicko_fa_xlsx, "with ZipFile(value.template_path)"),
        id="package-read",
    ),
]


@pytest.mark.parametrize(("crash", "site"), _QUICKO_CRASHES)
def test_cli_quicko_write_crash_is_an_internal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    crash: Callable[[pytest.MonkeyPatch], object],
    site: Callable[[], str],
) -> None:
    """#1042: the CLI reports a crash in the Quicko write as an internal error,
    by its class and source line, never as a Quicko refusal or with its text."""
    crash(monkeypatch)
    assert _main_fa(tmp_path, monkeypatch, "--quicko-workbook") == 1
    err = capsys.readouterr().err
    assert re.fullmatch(internal_error_line(RuntimeError), err)
    assert err == f"error: internal error ({site()})\n"
    _assert_no_quicko_markers(err)
    assert "Quicko" not in err
    assert not any((tmp_path / name).exists() for name in _FA_OUTPUTS)


def test_cli_quicko_removal_failure_exits_domain_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fail_quicko_write(monkeypatch, QuickoFaXlsxError("bad template"), leave_directory=True)
    assert _main_fa(tmp_path, monkeypatch, "--quicko-workbook") == 4
    err = capsys.readouterr().err
    assert "may still be there" in err
    assert str(tmp_path / "a3-quicko.xlsx") in err


@pytest.mark.parametrize(("quicko_headers", "message"), _BAD_QUICKO_TEMPLATES)
def test_cli_requested_quicko_workbook_with_a_bad_template_exits_domain_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    quicko_headers: tuple[str, ...] | None,
    message: Callable[[Path], str],
) -> None:
    code = _main_fa(tmp_path, monkeypatch, "--quicko-workbook", quicko_headers=quicko_headers)
    assert code == 4
    assert capsys.readouterr().err == f"error: {message(tmp_path / 'bundle')}\n"
    assert not any((tmp_path / name).exists() for name in _FA_OUTPUTS)


def test_cli_unrequested_quicko_workbook_without_template_is_manual_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _main_fa(tmp_path, monkeypatch, quicko_headers=None) == 3
    assert "Quicko" not in capsys.readouterr().out
    assert (tmp_path / "a3.csv").exists()
    assert (tmp_path / "a3-a2.csv").exists()
    assert not (tmp_path / "a3-quicko.xlsx").exists()


@pytest.mark.parametrize(
    "format_args",
    [
        ("--format", "review-csv"),
        ("--format", CBDT_JSON_FRAGMENT_FORMAT_ID, "--itr-form", "ITR-2"),
    ],
    ids=["review-csv", "cbdt-json-fragment"],
)
def test_cli_quicko_workbook_request_needs_filing_csv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    format_args: tuple[str, ...],
) -> None:
    assert _main_fa(tmp_path, monkeypatch, *format_args, "--quicko-workbook") == 2
    assert "--quicko-workbook" in capsys.readouterr().err


def _main_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    quicko_headers: tuple[str, ...] | None = QUICKO_HEADERS,
) -> tuple[int, list[int]]:
    """Run ``netbenefits-all --quicko-workbook`` into ``tmp_path / "out"``. The real
    FA handler and pipeline run; only the other two legs are stubbed. Returns the
    exit code and the FA leg's exit codes."""
    fa_codes: list[int] = []
    real_fa = ibkr_fa.cli._run_netbenefits_fa

    def fa(args: argparse.Namespace) -> int:
        fa_codes.append(real_fa(args))
        return fa_codes[-1]

    monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_fa", fa)
    monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_income", lambda _args: 0)
    monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_cg", lambda _args: 0)
    monkeypatch.setattr(
        "ibkr_fa.cli.netbenefits_form67_readiness", lambda **_kwargs: Form67Readiness.READY
    )
    bundle = _build_dir(tmp_path, quicko_headers=quicko_headers)
    _patch_pdf(monkeypatch, {"espp-purchase_2025-12-31.pdf": CONF_DEC, **_DEFAULT_STATEMENTS})
    cfg = tmp_path / "user.toml"
    cfg.write_text(BASE_CONFIG, encoding="utf-8")
    code = main(
        [
            "netbenefits-all",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output-dir",
            str(tmp_path / "out"),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
            "--no-fetch-prices",
            "--quicko-workbook",
        ]
    )
    return code, fa_codes


def test_netbenefits_all_reports_an_fa_leg_quicko_crash_as_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1042: a crash in the FA leg's Quicko write escapes the leg (so it records
    no exit code), is reported by its class and source line, and fails the run
    with exit 1; every output still goes."""
    _fail_quicko_write(monkeypatch, _crash())
    assert _main_all(tmp_path, monkeypatch) == (1, [])
    err = capsys.readouterr().err
    first = err.splitlines()[0]
    assert re.fullmatch(
        rf"error: netbenefits-all: netbenefits-fa crashed unexpectedly "
        rf"\({crash_pattern(RuntimeError)}\)",
        first,
    )
    assert first == (
        f"error: netbenefits-all: netbenefits-fa crashed unexpectedly "
        f"({_quicko_write_crash_site()})"
    )
    _assert_no_quicko_markers(err)
    assert "could not write the requested Quicko workbook" not in err
    assert list((tmp_path / "out").iterdir()) == []


@pytest.mark.parametrize(("quicko_headers", "message"), _BAD_QUICKO_TEMPLATES)
def test_netbenefits_all_fa_leg_refuses_a_bad_quicko_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    quicko_headers: tuple[str, ...] | None,
    message: Callable[[Path], str],
) -> None:
    assert _main_all(tmp_path, monkeypatch, quicko_headers=quicko_headers) == (4, [4])
    # The FA leg runs first, so its refusal is the first line.
    assert capsys.readouterr().err.splitlines()[0] == f"error: {message(tmp_path / 'bundle')}"
    assert list((tmp_path / "out").iterdir()) == []


def test_cli_refusal_exits_domain_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _build_dir(tmp_path, closed_csv=None)
    _patch_pdf(monkeypatch, {"espp-purchase_2025-12-31.pdf": CONF_DEC, **_DEFAULT_STATEMENTS})
    cfg = tmp_path / "user.toml"
    cfg.write_text(BASE_CONFIG, encoding="utf-8")
    code = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(tmp_path / "a3.csv"),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )
    assert code == 4
    assert "closed-lots CSV" in capsys.readouterr().err


def test_cli_refuses_a_confirmation_of_another_participant_without_either_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1042: the confirmation's header and row agree with each other, so it
    parses, but not with user.toml; the refusal names the confirmation and the
    config field, never either participant number."""
    conf = dec_confirmation(participant="I12345678")
    confirmations = {"espp-purchase_2025-12-31.pdf": conf}
    bundle = _build_dir(tmp_path, confirmations=confirmations)
    _patch_pdf(monkeypatch, {**confirmations, **_DEFAULT_STATEMENTS})
    config = BASE_CONFIG.replace('participant_no = "I99999999"', 'participant_no = "I00000001"')
    assert config != BASE_CONFIG
    cfg = tmp_path / "user.toml"
    cfg.write_text(config, encoding="utf-8")
    out = tmp_path / "a3.csv"
    code = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )
    assert code == 4
    err = capsys.readouterr().err
    assert err == (
        "error: confirmation 1 (by purchase date): the PARTICIPANT NO. differs from "
        "netbenefits.participant_no in user.toml\n"
    )
    _assert_absent(err, "I12345678", "I00000001")
    assert not out.exists()


def test_cli_refuses_a_shifted_open_lots_row_without_printing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1041: a cell that shifts a row is file content (here a holder's name), so
    the refusal names the file, by its path relative to --input, and the row,
    and prints none of the row's cells."""
    shifted = OPEN_CSV.replace(
        "Dec-31-2025,3.4820,668.96,192.12,626.24,-42.72,-,-,Nov-03-2025,SP,Short\n",
        "Dec-31-2025,Synthetic Holder Two,3.4820,668.96,192.12,626.24,-42.72,-,-,Nov-03-2025,"
        "SP,Short\n",
    )
    assert shifted != OPEN_CSV
    bundle = _build_dir(tmp_path, open_csv=shifted)
    _mkdir(bundle / "2026")
    (bundle / "View open lots.csv").rename(bundle / "2026" / "View open lots.csv")
    _patch_pdf(monkeypatch, {"espp-purchase_2025-12-31.pdf": CONF_DEC, **_DEFAULT_STATEMENTS})
    cfg = tmp_path / "user.toml"
    cfg.write_text(BASE_CONFIG, encoding="utf-8")
    code = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(tmp_path / "a3.csv"),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )
    assert code == 4
    err = capsys.readouterr().err
    assert err == (
        "error: ParseError in section 'open lots 2026/View open lots.csv' at row 2: row has "
        "12 columns, expected 11 (possible column shift / corruption)\n"
    )
    for cell in (
        "Synthetic Holder Two",
        "Dec-31-2025",
        "3.4820",
        "668.96",
        "192.12",
        "626.24",
        "-42.72",
        "Nov-03-2025",
    ):
        assert cell.casefold() not in err.casefold()
    assert not (tmp_path / "a3.csv").exists()


def test_yahoo_fallback_close_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No config override → the Dec-31 close comes from the fetched Yahoo series,
    # flagged as a market fallback.
    cfg = BASE_CONFIG.split("[[netbenefits.price_overrides]]", maxsplit=1)[0]
    report, _ = _run(tmp_path, monkeypatch, config=cfg)
    assert report.close_sources[0][0] is CloseSource.YAHOO_FALLBACK


@pytest.mark.parametrize(
    ("override_isin", "expected_fetch_symbol"),
    [("US99999E1047", "XESP-YF"), ("US99993A1088", None)],  # XESP's ISIN; XQBN's ISIN
)
def test_ticker_override_changes_only_the_fetched_symbol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override_isin: str,
    expected_fetch_symbol: str | None,
) -> None:
    # An override applies only on an ISIN match, and only to the symbol sent to
    # the price source. No config price override, so the Dec-31 close needs the
    # series keyed by XESP; the row stays tied to XESP. (#667)
    cfg = BASE_CONFIG.split("[[netbenefits.price_overrides]]", maxsplit=1)[0] + (
        '[[prices.ticker_overrides]]\nibkr_ticker = "XESP"\n'
        f'isin = "{override_isin}"\nyahoo_symbol = "XESP-YF"\n'
    )
    calls: list[tuple[Ticker, str | None]] = []
    stub = _stub_fetcher()

    def fetcher(ticker: Ticker, *, start, end, now, fetch_symbol=None):
        calls.append((ticker, fetch_symbol))
        return stub(ticker, start=start, end=end, now=now)

    report, out = _run(
        tmp_path, monkeypatch, config=cfg, format_id="review-csv", price_fetcher=fetcher
    )
    assert calls == [(Ticker("XESP"), expected_fetch_symbol)]
    assert report.close_sources[0][0] is CloseSource.YAHOO_FALLBACK
    row = _row_dict(out)
    assert (row["_audit: Source ticker"], row["_audit: Source ISIN"]) == ("XESP", "US99999E1047")


def test_price_response_with_no_exchange_zone_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1139: through the loader's default fetcher, a Yahoo response with no
    exchange zone refuses the run after one request, with no price cache and
    no FA output written."""
    urls = serve_no_zone_response(monkeypatch)
    with pytest.raises(YahooResponseFormatError) as exc:
        _run(tmp_path, monkeypatch, price_fetcher=fetch_daily_closes_usd)
    assert str(exc.value) == NO_ZONE_REFUSAL
    assert len(urls) == 1
    assert not (tmp_path / "cache").exists()
    assert not any((tmp_path / name).exists() for name in _FA_OUTPUTS)


def test_render_flags_market_fallback() -> None:
    from ibkr_fa.cli import _render_netbenefits_fa_report  # noqa: PLC0415

    report = NetBenefitsFaRunReport(
        rows_written=1,
        output_path=Path("/tmp/a3.csv"),
        security_name="EXAMPLE CORP",
        close_sources=((CloseSource.YAHOO_FALLBACK, "Yahoo close on/before 2025-12-31"),),
        quicko_output_path=Path("/tmp/schedule-fa-quicko.xlsx"),
        run_outputs=RunOutputs((), ()),
        a2_institution_is_fidelity_sps=False,
    )
    rendered = _render_netbenefits_fa_report(report)
    assert "market fallback" in rendered
    assert "Quicko Foreign Shares & Securities" in rendered
    assert "NOT filing-complete" in rendered


def test_render_names_every_blank_a3_column_in_portal_order() -> None:
    """#1330: one line per blank A3 column, in portal order, whether or not
    Quicko was written; only the ZIP line names a setting, the one config
    value that may be blank."""
    report = NetBenefitsFaRunReport(
        rows_written=1,
        output_path=Path("/tmp/a3.csv"),
        security_name="EXAMPLE CORP",
        close_sources=(),
        a3_manual_fill_columns=MANUAL_FILL_PORTAL_COLUMNS,
        a3_peak_blank_rows=((FaA3PeakBlankReason.SESSION_WITHOUT_HIGH, (1, 2)),),
        run_outputs=RunOutputs((), ()),
        a2_institution_is_fidelity_sps=False,
    )
    by_hand = "MANUAL-FILL — fill it in by hand before filing."
    assert ibkr_fa.cli._render_netbenefits_fa_report(report).splitlines() == [
        "Wrote 1 Schedule FA Table A3 row(s) for EXAMPLE CORP to /tmp/a3.csv.",
        f"  A3 Name of entity: {by_hand}",
        f"  A3 Address of entity: {by_hand}",
        A3_ZIP_LINE,
        f"  A3 Nature of entity: {by_hand}",
        nb_peak_line(FaA3PeakBlankReason.SESSION_WITHOUT_HIGH, rows="1, 2"),
        "  A3 Total gross amount paid/credited with respect to the holding during the "
        f"period: {by_hand}",
        "  A3 Total gross proceeds from sale or redemption of investment during the "
        f"period: {by_hand}",
        "",
        "!! NOT filing-complete: fill the A2 peak balance above, then emit the",
        "   CBDT JSON fragment with a verified [[netbenefits.a2_peak_overrides]] value.",
    ]


# --- ADR 0021: Fidelity Stock Plan Services as the A2 institution ----------------

# BASE_CONFIG plus the A2 peak the CBDT JSON fragment files.
_JSON_CONFIG = BASE_CONFIG.replace(
    'participant_no = "I99999999"\n',
    'participant_no = "I99999999"\na2_peak_overrides_participant_no = "I99999999"\n',
) + ("\n[[netbenefits.a2_peak_overrides]]\ncalendar_year = 2025\npeak_balance_inr = 66503\n")

_A2_INSTITUTION_WARNING = (
    "  warning: [netbenefits.account] institution_name names Fidelity Stock Plan "
    "Services. Its participant agreement says it does not maintain an account or "
    "hold assets for the participant (ADR 0021). This run still wrote the configured "
    "name into Table A2. Check your NetBenefits documents for the firm that "
    "holds your account; if they name a different firm, change institution_name, "
    "address and zip under [netbenefits.account]."
)


@pytest.mark.parametrize("format_id", ["filing-csv", CBDT_JSON_FRAGMENT_FORMAT_ID])
@pytest.mark.parametrize(
    ("institution_name", "flagged"),
    [("Fidelity Stock Plan Services, LLC", True), ("National Financial Services LLC", False)],
)
def test_report_flags_a_fidelity_stock_plan_services_a2_institution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    format_id: str,
    institution_name: str,
    *,
    flagged: bool,
) -> None:
    json_run = format_id == CBDT_JSON_FRAGMENT_FORMAT_ID
    report, _ = _run(
        tmp_path,
        monkeypatch,
        config=_JSON_CONFIG.replace(
            'institution_name = "Fidelity Stock Plan Services LLC"',
            f'institution_name = "{institution_name}"',
        ),
        format_id=format_id,
        itr_form="ITR-2" if json_run else None,
        output_name="a3.json" if json_run else "a3.csv",
    )
    assert report.emitted_cbdt_json is json_run
    assert report.a2_institution_is_fidelity_sps is flagged


def test_report_peak_is_the_a2_rows_filed_peak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1077: the report reads the row's filed peak, as the fragment does, and
    never rounds it."""
    seven = Decimal("7")
    monkeypatch.setattr(FaA3Row, "filed", property(lambda _row: FaA3FiledValues(*(seven,) * 5)))
    monkeypatch.setattr(FaA2Row, "filed", property(lambda _row: FaA2FiledValues(*(seven,) * 3)))
    report, _ = _run(
        tmp_path,
        monkeypatch,
        config=_JSON_CONFIG,
        format_id=CBDT_JSON_FRAGMENT_FORMAT_ID,
        itr_form="ITR-2",
        output_name="a3.json",
    )
    doc = json.loads((tmp_path / "a3.json").read_text(encoding="utf-8"))
    assert report.cbdt_json_a2_peak_balance_inr == 7
    assert doc["ScheduleFA"]["DtlsForeignCustodialAcc"][0]["PeakBalanceDuringPeriod"] == 7


def test_csv_report_has_no_filed_json_peak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A CSV run files no JSON fragment, so its report carries no JSON peak."""
    report, _ = _run(tmp_path, monkeypatch)
    assert report.emitted_cbdt_json is False
    assert report.cbdt_json_a2_peak_balance_inr is None


@pytest.mark.parametrize("json_run", [False, True], ids=["csv", "cbdt-json"])
@pytest.mark.parametrize("flagged", [False, True])
def test_render_warns_about_a_fidelity_stock_plan_services_a2_institution(
    *, json_run: bool, flagged: bool
) -> None:
    from ibkr_fa.cli import _render_netbenefits_fa_report  # noqa: PLC0415

    report = NetBenefitsFaRunReport(
        rows_written=1,
        output_path=Path("/tmp/a3.json" if json_run else "/tmp/a3.csv"),
        security_name="EXAMPLE CORP",
        close_sources=((CloseSource.OVERRIDE, "manual price override"),),
        a2_output_path=None if json_run else Path("/tmp/a3-a2.csv"),
        emitted_cbdt_json=json_run,
        cbdt_json_a2_peak_balance_inr=66503 if json_run else None,
        cbdt_json_itr_form="ITR-2" if json_run else None,
        run_outputs=RunOutputs((), ()),
        a2_institution_is_fidelity_sps=flagged,
    )
    lines = _render_netbenefits_fa_report(report).splitlines()
    assert (_A2_INSTITUTION_WARNING in lines) is flagged
    assert any("ADR 0021" in line for line in lines) is flagged


# --- peak_basis = "high" (opt-in intraday-high peak) --------------------------

_HIGH_CONFIG = BASE_CONFIG + '\n[schedule_fa]\npeak_basis = "high"\n'
# The A3 peak a high-basis run files from a fetched Dec-31 high of 219.00: the
# 3.4820 shares of OPEN_CSV's Dec-31 lot at that high, at 89.47, the bundled SBI
# TTBR for 31 December 2025, are INR 68,226.06, filed in whole rupees.
_PEAK_AT_THE_HIGH = str(
    (Decimal("3.4820") * Decimal("219.00") * Decimal("89.47")).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
)


def test_peak_basis_high_uses_intraday_high(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With peak_basis = "high" and a fetched Dec-31 high above the close, the A3
    Peak is the holding at that high, above the Closing (which stays anchored to
    the 213.47 override)."""
    report, out = _run(
        tmp_path, monkeypatch, config=_HIGH_CONFIG, fetcher_close="213.47", fetcher_high="219.00"
    )
    row = _row_dict(out)
    assert row["Peak value of investment during the Period"] == _PEAK_AT_THE_HIGH
    peak = Decimal(row["Peak value of investment during the Period"])
    closing = Decimal(row["Closing balance"])
    assert peak > closing  # the intraday high (219) drives it, above the 213.47 close
    assert report.a3_peak_blank_rows == ()
    assert report.a3_manual_fill_columns == ()


@pytest.mark.parametrize("quicko_workbook", [False, True], ids=["no-quicko", "quicko"])
def test_peak_basis_high_without_fetch_manual_fills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quicko_workbook: bool
) -> None:
    """peak_basis = "high" needs a price fetch; without one the intraday high can't
    be established, so the A3 Peak is left blank (manual-fill) and surfaced."""
    report, out = _run(
        tmp_path,
        monkeypatch,
        config=_HIGH_CONFIG,
        fetch_prices=False,
        quicko_workbook=quicko_workbook,
    )
    row = _row_dict(out)
    assert row["Peak value of investment during the Period"] == ""
    assert row["Closing balance"] != ""  # closing still resolves (from the override)
    assert report.a3_peak_blank_rows == ((FaA3PeakBlankReason.PRICES_NOT_FETCHED, (1,)),)
    assert report.a3_manual_fill_columns == (FA_A3_PEAK_VALUE_COLUMN,)
    rendered = ibkr_fa.cli._render_netbenefits_fa_report(report)
    peak_label = "A3 / Quicko" if quicko_workbook else "A3"
    assert nb_peak_line(FaA3PeakBlankReason.PRICES_NOT_FETCHED, label=peak_label) in (
        rendered.splitlines()
    )
    assert ("Quicko" in rendered) is quicko_workbook
    if quicko_workbook:
        assert report.quicko_output_path is not None
        workbook = load_workbook(report.quicko_output_path, data_only=True)
        assert workbook["Shares & Securities"]["K2"].value is None


def _write_close_only_cache(path: Path) -> None:
    """A valid legacy (v1, close-only) contemporaneous cache for XESP whose span
    covers the run's request: every US market session from 2024-11-01 through
    2025-12-31 closes at 213.47, and no row has a high."""
    start, end = dt.date(2024, 11, 1), dt.date(2025, 12, 31)
    body = (
        "# ticker=XESP\n# fetch_symbol=XESP\n# currency=USD\n# has_split_in_range=False\n"
        "# split_ratios={}\n# price_basis=contemporaneous\n"
        "# source=Synthetic close-only source\n# fetched_at=2026-01-02T00:00:00+00:00\n"
        f"# covered_start={start.isoformat()}\n# covered_end={end.isoformat()}\n"
        "date,close_usd\n"
        + "".join(f"{day.isoformat()},213.47\n" for day in us_market_sessions(start, end))
    )
    digest = hashlib.sha256(body.encode()).hexdigest()
    path.parent.mkdir(parents=True)
    path.write_text(f"# payload_sha256={digest}\n{body}", encoding="utf-8")


@pytest.mark.parametrize("remedy", ["empty-cache-dir", "cache-moved-aside"])
def test_peak_advice_clears_a_matching_close_only_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    remedy: str,
) -> None:
    """#1471: a cache that matches the request is reused before any fetch, so
    with a close-only (legacy) one, price fetching alone leaves the "high" peak
    blank. Each way the advice names, an empty --price-cache-dir or moving the
    holding's cache file aside, makes the rerun fetch and fill the peak."""
    bundle = _build_dir(tmp_path)
    _patch_pdf(monkeypatch, {"espp-purchase_2025-12-31.pdf": CONF_DEC, **_DEFAULT_STATEMENTS})
    cfg = tmp_path / "user.toml"
    cfg.write_text(_HIGH_CONFIG, encoding="utf-8")
    cache_file = _cache_path(bundle / ".ibkr-fa-price-cache", Ticker("XESP"))
    _write_close_only_cache(cache_file)
    calls: list[Ticker] = []
    stub = _stub_fetcher("213.47", high="219.00")

    def fetcher(ticker, *, start, end, now, fetch_symbol=None):
        calls.append(ticker)
        return stub(ticker, start=start, end=end, now=now, fetch_symbol=fetch_symbol)

    monkeypatch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", fetcher)
    out = tmp_path / "a3.csv"
    args = [
        "netbenefits-fa",
        *("--input", str(bundle), "--config", str(cfg), "--output", str(out)),
        *("--calendar-year", "2025"),
    ]
    peak = "Peak value of investment during the Period"

    assert main(args) == 3
    assert calls == []
    assert _row_dict(out)[peak] == ""
    assert nb_peak_line(FaA3PeakBlankReason.SESSION_WITHOUT_HIGH) in (
        capsys.readouterr().out.splitlines()
    )

    if remedy == "empty-cache-dir":
        flag = ["--price-cache-dir", str(_mkdir(tmp_path / "empty-cache"))]
        # netbenefits-all prints this advice through its FA leg, so it takes the flag too.
        all_args = ibkr_fa.cli._build_parser().parse_args(
            [
                "netbenefits-all",
                *("--input", str(bundle), "--config", str(cfg), "--output-dir", str(tmp_path)),
                *("--financial-year", "2025-26", "--itr-form", "ITR-2", *flag),
            ]
        )
        assert all_args.price_cache_dir == tmp_path / "empty-cache"
        args += flag
    else:
        cache_file.rename(_mkdir(tmp_path / "aside") / cache_file.name)
    assert main(args) == 3
    assert calls == [Ticker("XESP")]
    row = _row_dict(out)
    assert row[peak] == _PEAK_AT_THE_HIGH
    assert Decimal(row[peak]) > Decimal(row["Closing balance"])  # the fetched 219.00 high
    rerun = capsys.readouterr().out
    assert "Peak value(s) for row(s)" not in rerun
    # Exit 3 now comes only from the A2 peak balance, which no price can fill.
    assert "!! NOT filing-complete: fill the A2 peak balance above, then emit the" in rerun


def test_peak_basis_close_default_peak_equals_closing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression lock: the DEFAULT close basis still yields peak == closing for a
    Dec-31 lot even with a fetched high present (the high must not leak in)."""
    report, out = _run(tmp_path, monkeypatch, fetcher_close="213.47", fetcher_high="219.00")
    row = _row_dict(out)
    assert row["Peak value of investment during the Period"] == row["Closing balance"]
    assert report.a3_peak_blank_rows == ()
