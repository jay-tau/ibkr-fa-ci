"""An ``InvariantError`` keeps no input value (#1252).

``require()`` and ``invariant_error`` keep each context key, in call order, and
only the ``int`` value of a position key, so no other value is in the error's
fields, args, ``repr`` or exception graph. One refusal from each layer: a
model's ISIN check, a compute cross-check, and two output refusals (the A3
upload's ASCII check and the CG filing workbook's missing-context refusal,
which built its error directly before #1252).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.compute.netbenefits_fa_a2 import build_netbenefits_a2_row
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.output import Subcommand, output_binding
from ibkr_fa.output.base import RenderContext
from tests._crash_reports import assert_prints_no_marker
from tests._fa_a3_name_runs import A3_ASCII_ERROR
from tests._fa_a3_rows import fa_filing_csv_writer as _filing_writer
from tests._fa_a3_rows import make_fa_a3_row as make_a3_row
from tests._fa_a3_rows import make_fa_a3_upload_row as make_upload_row
from tests._netbenefits_a2_identity import netbenefits_a2_identity as _identity

# Neutral markers. The calls name them by constant, so the
# rendered traceback's source lines can't hold them.
_ISIN = "SYNTHQISIN"
_A3_CLOSING_INR = Decimal("12345.6789")
_SECURITIES_USD = Decimal("987.65")
_ADDRESS = "SYNTHQADDR Straße 1"
_SOURCE_LABEL = "SYNTHQLABEL"


def test_model_isin_refusal_keeps_no_isin() -> None:
    with pytest.raises(InvariantError) as caught:
        make_a3_row(source_isin=_ISIN)
    assert str(caught.value) == (
        "InvariantError: FaA3Row.source_isin must match ISO 6166 (context keys: source_isin)"
    )
    assert_prints_no_marker(caught.value, markers=(_ISIN,))
    assert caught.value.context == (("source_isin", None),)


def test_compute_netbenefits_a2_mismatch_keeps_no_amount() -> None:
    # A TTBR of 1 makes the NAV product the USD total itself, so both amounts
    # were context values before #1252.
    with pytest.raises(InvariantError) as caught:
        build_netbenefits_a2_row(
            identity=_identity(),
            securities_inr=_A3_CLOSING_INR,
            securities_usd=_SECURITIES_USD,
            ttbr_dec31=Decimal("1"),
        )
    assert str(caught.value) == (
        "InvariantError: NetBenefits A2 closing: Σ A3 closing INR differs from securities USD "
        "x Dec-31 TTBR (context keys: a3_closing_inr, nav_closing_inr)"
    )
    assert_prints_no_marker(caught.value, markers=(str(_A3_CLOSING_INR), str(_SECURITIES_USD)))
    assert caught.value.context == (("a3_closing_inr", None), ("nav_closing_inr", None))


def test_output_a3_ascii_refusal_keeps_no_text(tmp_path: Path) -> None:
    out = tmp_path / "schedule-fa.csv"
    with pytest.raises(InvariantError) as caught:
        _filing_writer().write([make_upload_row(address=_ADDRESS)], out)
    assert str(caught.value) == A3_ASCII_ERROR
    assert_prints_no_marker(caught.value, markers=("SYNTHQADDR",))
    assert caught.value.context == (("field", None), ("value", None))
    assert not out.exists()


def test_output_cg_workbook_context_refusal_keeps_no_label() -> None:
    context = RenderContext(assessment_year_start=2026, source_label=_SOURCE_LABEL)
    with pytest.raises(InvariantError) as caught:
        output_binding(Subcommand.CG_FILING, "filing-xlsx").writer(context)
    assert str(caught.value) == (
        "InvariantError: the Schedule CG filing workbook needs the run's ITR form and source "
        "label (context keys: itr_form, source_label)"
    )
    assert_prints_no_marker(caught.value, markers=(_SOURCE_LABEL,))
    assert caught.value.context == (("itr_form", None), ("source_label", None))
