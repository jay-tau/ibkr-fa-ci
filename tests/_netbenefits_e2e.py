"""The NetBenefits end-to-end bundle that several test modules share: the
committed synthetic fixtures (``tests/fixtures/netbenefits/``) behind a stubbed
``pdftotext``, its configs, the CBDT JSON fragment it files, and the Dec-31
session refusals run on it.
"""

from __future__ import annotations

import csv
from collections import ChainMap
from functools import partial
from pathlib import Path

import pytest

from ibkr_fa.output.netbenefits_quicko_fa_xlsx import QUICKO_TEMPLATE_FILENAME
from ibkr_fa.pipeline_netbenefits_fa import run_netbenefits_fa
from ibkr_fa.prices.yahoo import DailyCloseFetcher
from tests._dec31_close import (
    MARKER_PRICE,
    MARKER_REASON,
    MARKER_SOURCE,
    MARKER_SOURCE_DATE,
    MARKER_SYMBOL,
)
from tests._netbenefits import INSTITUTION_TOML, PARTICIPANT_TOML, SECURITY_TOML, patch_pdftotext
from tests._netbenefits_fa import (
    NB_MARKET_SESSION_REFUSAL,
    NB_OVERRIDE_SESSION_REFUSAL,
    PRICE_NOW,
    session_fetcher,
    write_quicko_template,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "netbenefits"


# The bundle PDFs (placeholder bytes on disk) -> the committed pdftotext text the
# stubbed shell-out returns. Statements + the Dec-31 purchase confirmation.
NB_E2E_PDF_TEXT = {
    "confirmation-dec.pdf": (_FIXTURES / "confirmation-dec.txt").read_text(encoding="utf-8"),
    "statement-oct-dec.pdf": (_FIXTURES / "statement-oct-dec.txt").read_text(encoding="utf-8"),
    # #1151: the January report prints the Dec-31 purchase's row, settled 01/05, and
    # covers through ten days after Dec-31.
    "statement-jan.pdf": (_FIXTURES / "statement-jan.txt").read_text(encoding="utf-8"),
    "statement-feb-mar.pdf": (_FIXTURES / "statement-feb-mar.txt").read_text(encoding="utf-8"),
    # #666: capital gains need the statements to cover ten days past the FY end.
    "statement-apr.pdf": (_FIXTURES / "statement-apr.txt").read_text(encoding="utf-8"),
}

# The bundle's Dec-31 2025 price override, as NB_E2E_TOML writes it.
NB_E2E_PRICE_OVERRIDE = (
    "[[netbenefits.price_overrides]]\n"
    "price_date = 2025-12-31\n"
    'price_per_share_usd = "213.47"\n'
    'source = "SPS year-end statement"\n'
    'reason = "12-31 lot unpriced in the CSV and Dec statements"\n'
)

NB_E2E_TOML = f"""\
[account]
holder_name = "Tax Payer"
opening_date = 2024-02-01
status = "BeneficialOwner"

[account.foreign_tins]
US = "123456789"

{PARTICIPANT_TOML}
{NB_E2E_PRICE_OVERRIDE}
{INSTITUTION_TOML}account_opening_date = 2025-11-03
status = "BeneficialOwner"

{SECURITY_TOML}"""


def nb_e2e_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    withholding_usd: str | None = None,
    drop_income_detail: bool = False,
    feb_mar_extra: str = "",
) -> tuple[Path, Path]:
    """Write the e2e bundle (the fixture lots CSVs and placeholder PDFs) and
    ``user.toml`` (NB_E2E_TOML) under ``tmp_path``, and stub ``pdftotext`` with
    NB_E2E_PDF_TEXT, its Feb-Mar statement edited as the keywords say. Returns
    the bundle and the config."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in ("open-lots.csv", "closed-lots.csv"):
        (bundle / name).write_text((_FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")
    for name in NB_E2E_PDF_TEXT:
        (bundle / name).write_bytes(b"%PDF-1.4 placeholder")

    text = NB_E2E_PDF_TEXT["statement-feb-mar.pdf"]
    edits: dict[str, str] = {}
    if drop_income_detail:
        # #903: the detail page is lost; the period summaries still show income.
        start = text.index("Dividends, Interest & Other Income\n")
        end = text.index("Total Core Fund Activity $1.09\n", start)
        edited = text[:start] + text[end + len("Total Core Fund Activity $1.09\n") :]
        assert edited != text
        edits["statement-feb-mar.pdf"] = edited
    elif withholding_usd is not None:
        assert "0.37" in text and "1.090" in text and "$1.09" in text
        edited = (
            text.replace("0.37", withholding_usd)
            .replace("1.090", "1.100")
            .replace("$1.09", "$1.10")
        )
        assert edited != text
        edits["statement-feb-mar.pdf"] = edited
    elif feb_mar_extra:
        # #1149: a line after the statement's activity sections.
        end = "Total Core Fund Activity $1.09\n"
        assert text.count(end) == 1
        edits["statement-feb-mar.pdf"] = text.replace(end, end + feb_mar_extra)
    # The other texts stay live, so a test can still edit NB_E2E_PDF_TEXT after
    # nb_e2e_bundle.
    patch_pdftotext(monkeypatch, ChainMap(edits, NB_E2E_PDF_TEXT))
    cfg = tmp_path / "user.toml"
    cfg.write_text(NB_E2E_TOML, encoding="utf-8")
    return bundle, cfg


def csv_file_rows(path: Path) -> list[list[str]]:
    """The rows of the CSV file at ``path``, without a leading BOM."""
    with path.open(encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


# The A2 peak balance (manual-fill in the CSV) supplied via config for the JSON,
# bound to the Participant Number.
def nb_e2e_toml_with_peak(peak_balance_inr: int = 66503, participant_no: str = "I99999999") -> str:
    base = NB_E2E_TOML.replace(
        'participant_no = "I99999999"\n',
        f'participant_no = "I99999999"\na2_peak_overrides_participant_no = "{participant_no}"\n',
    )
    return base + (
        f"\n[[netbenefits.a2_peak_overrides]]\ncalendar_year = 2025\n"
        f"peak_balance_inr = {peak_balance_inr}\n"
    )


NB_E2E_TOML_WITH_PEAK = nb_e2e_toml_with_peak()

NB_E2E_CBDT_FRAGMENT = {
    "_ibkrFa": {
        "format": "cbdt-json-fragment",
        "itrForm": "ITR-3",
        "schemaVersion": "ITR-3 AY2026-27 Main V1.1 Schedule FA",
    },
    "ScheduleFA": {
        "DtlsForeignCustodialAcc": [
            {
                "CountryName": "UNITED STATES OF AMERICA",
                "CountryCodeExcludingIndia": "2",
                "FinancialInstName": "Fidelity Stock Plan Services LLC",
                "FinancialInstAddress": "245 Summer Street, Boston, MA",
                "ZipCode": "02210",
                "AccountNumber": "I99999999",
                "Status": "BENEFICIAL_OWNER",
                "AccOpenDate": "2025-11-03",
                "PeakBalanceDuringPeriod": 66503,
                "ClosingBalance": 66503,
                "GrossAmtPaidCredited": 0,
                "NatureOfAmount": "N",
            }
        ],
        "DtlsForeignEquityDebtInterest": [
            {
                "CountryName": "United States of America",
                "CountryCodeExcludingIndia": "2",
                "NameOfEntity": "EXAMPLE CORP",
                "AddressOfEntity": "1 Sample Issuer Street, Sample City, XX",
                "ZipCode": "00000",
                "NatureOfEntity": "Listed company shares",
                "InterestAcquiringDate": "2025-12-31",
                "InitialValOfInvstmnt": 66503,
                "PeakBalanceDuringPeriod": 66503,
                "ClosingBalance": 66503,
                "TotGrossAmtPaidCredited": 0,
                "TotGrossProceeds": 0,
            }
        ],
    },
}

# Dated Dec-30-2025, a session but not CY2025's required Dec-31 one.
_DEC30_OVERRIDE = (
    "[[netbenefits.price_overrides]]\n"
    f"price_date = {MARKER_SOURCE_DATE}\n"
    f'price_per_share_usd = "{MARKER_PRICE}"\n'
    f'source = "{MARKER_SOURCE}"\n'
    f'reason = "{MARKER_REASON}"\n'
)

_OVERRIDE_MARKERS = (MARKER_SYMBOL, MARKER_PRICE, MARKER_SOURCE, MARKER_REASON, MARKER_SOURCE_DATE)

# Each refusal's override block, price fetcher (None: --no-fetch-prices with no
# cache), complete message, and the synthetic markers in the run's input.
SESSION_REFUSALS = pytest.mark.parametrize(
    ("override", "fetcher", "refusal", "markers"),
    [
        pytest.param(
            _DEC30_OVERRIDE,
            None,
            NB_OVERRIDE_SESSION_REFUSAL,
            _OVERRIDE_MARKERS,
            id="dec30-override",
        ),
        pytest.param(
            _DEC30_OVERRIDE,
            session_fetcher(),
            NB_OVERRIDE_SESSION_REFUSAL,
            _OVERRIDE_MARKERS,
            id="dec30-override-beside-a-valid-dec31-close",
        ),
        pytest.param(
            "",
            session_fetcher(dec30=MARKER_PRICE, dec31=None),
            NB_MARKET_SESSION_REFUSAL,
            (MARKER_SYMBOL, MARKER_PRICE, MARKER_SOURCE_DATE),
            id="dec30-market-close-only",
        ),
    ],
)


def session_refusal_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: str,
    fetcher: DailyCloseFetcher | None,
) -> tuple[Path, Path, tuple[str, ...]]:
    """The e2e bundle, plus a Quicko template, under the symbol marker and
    ``override`` in place of the Dec-31 override. ``fetcher`` serves the prices,
    or none are read when it is ``None``. Returns the bundle, the config and the
    price arguments."""
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    write_quicko_template(bundle / QUICKO_TEMPLATE_FILENAME)
    confirmation = NB_E2E_PDF_TEXT["confirmation-dec.pdf"]
    assert "SYMBOL: XESP " in confirmation
    monkeypatch.setitem(
        NB_E2E_PDF_TEXT,
        "confirmation-dec.pdf",
        confirmation.replace("SYMBOL: XESP ", f"SYMBOL: {MARKER_SYMBOL} "),
    )
    assert NB_E2E_PRICE_OVERRIDE in NB_E2E_TOML_WITH_PEAK
    toml = NB_E2E_TOML_WITH_PEAK.replace(NB_E2E_PRICE_OVERRIDE, override)
    cfg.write_text(toml.replace('symbol = "XESP"', f'symbol = "{MARKER_SYMBOL}"'), encoding="utf-8")
    if fetcher is None:
        return bundle, cfg, ("--no-fetch-prices",)
    monkeypatch.setattr(
        "ibkr_fa.cli.run_netbenefits_fa",
        partial(run_netbenefits_fa, price_fetcher=fetcher, price_now=PRICE_NOW),
    )
    return bundle, cfg, ("--price-cache-dir", str(tmp_path / "cache"))
