"""One foreign-withholding-tax line item, ready for FTC computation.

A :class:`WithholdingTax` is the typed shape of one row from the
IBKR Activity Statement's ``Withholding Tax`` section. Each row
pairs with a row in the ``Dividends`` section (sister parser at
:mod:`ibkr_fa.parser.activity_statement.dividends`) — the same
``Description`` ``SYMBOL(ISIN)`` prefix appears in both, with a
``" - US Tax"`` (or other country's) suffix on the WHT side naming the
country that took the tax. After the mapped Indian-ADR skip under
``underlying-issuer`` routing, foreign-tax netting refuses a remaining row
whose stated country differs from its ISIN's (#1282).

Schedule OS itself uses the GROSS dividend — WHT is NOT netted
there; the foreign-tax-credit pathway is Schedule TR + Form 67
(#33), not OS. For Schedule FSI,
WHT aggregates per source country (the company's issuer country)
as "tax paid in source country" (#35). For Schedule TR / Form 67,
the same per-country WHT total is the credit claim against the
Indian tax-on-foreign-income (#36 / #37).

**Sign convention (CRITICAL)**: IBKR emits withholding tax as a
NEGATIVE amount (it's money LEAVING the cash balance, mirroring
the positive Dividends-section row of which it's a reduction).
Reversals — when IBKR has to refund a previously-withheld tax
because the tax was incorrectly applied, or the holding was
declared treaty-exempt after the fact — appear in the same
section as POSITIVE amounts. Per the Paasa methodology note:

  "Reversals (positive amounts) excluded from tax paid totals"

Compute-layer code that sums WHT for Schedule OS / FSI / TR /
Form 67 must net positive reversals against negative paid amounts,
not just sum the negatives. The pairing logic (which reversal
cancels which original) lives in the compute layer, not the
parser — see #138's "Out of scope" note. The model itself
preserves the raw sign so the compute layer can do the right
thing without re-parsing.

v1 scope: one row per IBKR Withholding Tax data row. Sister
sections that ALSO carry WHT (e.g., Interest section's optional
withholding column at #139, or Flex Query's Cash Transactions
section's inline WHT at #12) are out of scope here — those
parsers will construct :class:`WithholdingTax` instances of their
own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ibkr_fa.invariants import require
from ibkr_fa.models._identifier_checks import is_isin_shape, is_iso_alpha2, is_iso_currency_code
from ibkr_fa.types import Isin, Ticker


@dataclass(frozen=True, slots=True)
class WithholdingTax:
    """One withholding-tax line item from the reporting period.

    Construction-time invariants (see :meth:`__post_init__`) enforce:

    * Non-empty ``symbol`` and well-formed ``isin`` (ISO 6166).
    * Non-zero, finite ``amount``: a withholding-tax row with zero
      amount is malformed (IBKR doesn't emit zero-tax rows). Sign
      is NOT constrained here — negative is the normal case (tax
      paid), positive is a reversal (tax refunded). The compute
      layer separates the two; the parser only refuses zero.
    * ``currency`` is a 3-letter ISO 4217 uppercase code. v1 only
      handles USD downstream but the model carries the value so
      future multi-currency support (#52) doesn't require a model
      change.

    Attributes:
        symbol: Ticker symbol of the issuing instrument (e.g.,
            ``"XMPL"``). Extracted by the parser from the
            ``Description`` column via the shared
            :func:`~ibkr_fa.parser.activity_statement._description.parse_symbol_isin_from_description`
            helper.
        isin: ISIN of the issuing instrument (ISO 6166). Also
            extracted from ``Description``. v1 trusts the
            Description-embedded ISIN as-is and does not
            cross-check it against the
            ``Financial Instrument Information`` section's
            ``Security ID`` for the same symbol; the broker is the
            single source of truth.
        payment_date: Calendar date IBKR debited (or, for
            reversals, re-credited) the WHT to the user's cash
            balance. Used as the FX-conversion date for per-line
            INR derivation in the compute layer. IBKR emits this
            in DD-MM-YYYY format (same as the Dividends section);
            the parser normalizes to :class:`datetime.date`.
        amount: Signed :class:`~decimal.Decimal` in
            :attr:`currency`. NEGATIVE for tax paid (normal),
            POSITIVE for tax reversed/refunded. Reversal pairing
            (matching a positive reversal to the negative original
            it cancels) is the compute layer's responsibility —
            this field carries the raw signed amount.
        currency: ISO 4217 code of :attr:`amount`.
        description: Raw verbatim description from the IBKR row
            (e.g., ``"XMPL(US99999A1025) Cash Dividend USD 0.25
            per Share - US Tax"``). Preserved for the audit trail
            and (downstream) for matching against the originating
            Dividend row's description on the same payment date.
        source_account_index: Optional privacy-safe source-account
            index, set by multi-account adapters to the account of the
            dividend this row pairs with (Robinhood). Single-account
            importers leave it ``None``. Form 67 keys each dividend
            payment on (source account, ISIN, payment date) for its
            per-payment treaty-rate test, and the orphan-withholding
            guard matches on the same key (#890).
        tax_country_iso2: The country the row says took the tax: the two
            letters of IBKR's trailing ``" - XX Tax"`` description
            suffix, read only in exactly that form. ``None`` when the
            row has no such suffix and for every other source. After
            the mapped Indian-ADR skip under ``underlying-issuer``
            routing, a set value must equal the ISIN's country or
            foreign-tax netting refuses the run (#1282).
        file_line: The 1-based statement line the row starts on, set by
            the IBKR Activity Statement parser so a refusal can name the
            row without printing its values. ``None`` for other sources.
            Required when :attr:`tax_country_iso2` is set.
    """

    symbol: Ticker
    isin: Isin
    payment_date: date
    amount: Decimal
    currency: str
    description: str
    source_account_index: int | None = field(default=None, kw_only=True)
    tax_country_iso2: str | None = field(default=None, kw_only=True)
    file_line: int | None = field(default=None, kw_only=True)

    @property
    def is_reversal(self) -> bool:
        """``True`` iff this row represents a reversal/refund (positive
        amount), ``False`` for a tax-paid row (negative amount).

        Derived from :attr:`amount` sign per the IBKR convention
        documented at the class docstring's "Sign convention" section.
        """
        return self.amount > 0

    def __post_init__(self) -> None:
        """Fail-fast structural invariants. See class docstring."""
        require(
            self.symbol != "",
            what="WithholdingTax.symbol must be non-empty",
        )
        require(
            is_isin_shape(self.isin),
            what=(
                "WithholdingTax.isin must match ISO 6166 (12 chars, leading 2-letter country code)"
            ),
            isin=self.isin,
        )
        require(
            self.amount.is_finite() and self.amount != 0,
            what=(
                "WithholdingTax.amount must be a non-zero finite "
                "Decimal — negative for tax paid (IBKR convention), "
                "positive for reversal/refund. Zero is malformed; "
                "IBKR does not emit zero-tax rows."
            ),
            amount=self.amount,
        )
        require(
            is_iso_currency_code(self.currency),
            what=("WithholdingTax.currency must be a 3-letter uppercase ISO 4217 code"),
            currency=self.currency,
        )
        require(
            self.description != "",
            what="WithholdingTax.description must be non-empty (audit trail)",
        )
        if self.source_account_index is not None:
            require(
                isinstance(self.source_account_index, int)
                and not isinstance(self.source_account_index, bool)
                and self.source_account_index >= 0,
                what=(
                    "WithholdingTax.source_account_index must be non-negative "
                    "and an integer when provided"
                ),
                source_account_index=self.source_account_index,
            )
        if self.tax_country_iso2 is not None:
            require(
                isinstance(self.tax_country_iso2, str) and is_iso_alpha2(self.tax_country_iso2),
                what="WithholdingTax.tax_country_iso2 must be two uppercase ASCII letters",
            )
            require(
                self.file_line is not None,
                what="WithholdingTax.file_line must be set when tax_country_iso2 is",
            )
        if self.file_line is not None:
            require(
                type(self.file_line) is int and self.file_line >= 1,
                what="WithholdingTax.file_line must be a 1-based line number",
                file_line=self.file_line,
            )
