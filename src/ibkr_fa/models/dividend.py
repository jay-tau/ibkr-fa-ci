"""One foreign-distribution payment, ready for INR conversion.

A :class:`Dividend` is the typed shape of one row from the IBKR
Activity Statement's ``Dividends`` section — historically the model
was named for the ordinary-cash-dividend case but the section
emits any *distribution* (Cash Dividend, Special Dividend, Choice
Dividend, Payment In Lieu Of Dividend, Return Of Capital,
Long-Term Capital Gain, etc.). The model carries them all uniformly
because Schedule FA Table A3 column 10's label is "Total gross
amount **paid/credited** with respect to the holding" — broader
than just cash dividends.

For Schedule FA Table A3, the sum of distributions paid per holding
during the calendar year populates column 10. For Schedule OS,
ordinary cash dividends flow into "Income from Other Sources" at
the user's marginal rate; the more exotic types (Return Of
Capital, etc.) have different OS treatment that downstream code
splits on :attr:`kind`. For Schedule FSI /
Schedule TR / Form 67, gross distributions aggregate per source
country (the company's issuer country, not the broker's country)
for foreign-tax-credit computation.

v1 scope: any positive-amount entry in the IBKR Dividends section.
Interest income is tracked separately at issue #32 (interest is
rare on broker cash balances at IBKR's tier, but conceptually
parallel). Withholding tax is parsed separately into
:class:`WithholdingTax` (also #32) so column 10's "gross" semantic
is preserved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ibkr_fa.invariants import require
from ibkr_fa.models._identifier_checks import is_isin_shape, is_iso_currency_code
from ibkr_fa.models._row_checks import require_amounts
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.types import Isin, Ticker


@dataclass(frozen=True, slots=True)
class Dividend:
    """One dividend payment received during the reporting period.

    Construction-time invariants (see :meth:`__post_init__`) enforce:

    * Non-empty ``symbol`` and well-formed ``isin`` (ISO 6166).
    * Finite, positive ``amount_gross``: a "dividend payment" with zero or
      negative gross amount is not a real dividend (corrections /
      reversals are emitted by IBKR in the ``Change in Dividend
      Accruals`` section, not the ``Dividends`` section).
    * ``currency`` is a 3-letter ISO 4217 uppercase code. v1 only
      handles USD downstream but the model carries the value so
      future multi-currency support (#52) doesn't require a model
      change.
    * ``kind`` is a :class:`DistributionKind` member, the
      :attr:`DistributionKind.UNRECOGNISED` default and the
      :attr:`DistributionKind.MALFORMED` marker included; its raw
      string (e.g. ``"Cash Dividend"``) is refused.

    Attributes:
        symbol: Ticker symbol of the issuing instrument (e.g.,
            ``"XMPL"``). Extracted by the parser from the Activity
            Statement's ``Description`` column.
        isin: ISIN of the issuing instrument (ISO 6166). Also
            extracted from ``Description``. v1 trusts the
            Description-embedded ISIN as-is and does not cross-check
            it against the ``Financial Instrument Information``
            section's ``Security ID`` for the same symbol; the
            broker is the single source of truth. Aggregation in
            :func:`~ibkr_fa.compute.dividends.aggregate_dividends_by_symbol_inr`
            keys on :attr:`symbol` only — see that function's
            module docstring for the ticker-vs-(symbol, ISIN)
            scope note. Preserved on the model so a future
            ISIN-aware enhancement is not blocked.
        payment_date: Calendar date IBKR credited the dividend to
            the user's cash balance. Used as the FX-conversion date
            for Schedule FA calendar-year inclusion and source reconciliation.
        amount_gross: Gross dividend amount in :attr:`currency`,
            BEFORE withholding tax. Positive :class:`~decimal.Decimal`.
        currency: ISO 4217 code of :attr:`amount_gross`.
        description: Raw verbatim description from the source row
            (e.g., ``"XMPL(US99999A1025) Cash Dividend USD 0.25
            per Share (Ordinary Dividend)"``). Preserved for the
            audit trail only: nothing downstream parses it, because
            the distribution kind lives in :attr:`kind`.
        kind: :class:`DistributionKind` set by the source that built
            the row. The IBKR Activity Statement parser classifies the
            description via
            :func:`~ibkr_fa.parser.activity_statement.dividends.classify_distribution`
            (issue #153); the other source adapters set it directly.
            The Schedule OS compute layer dispatches on this enum and
            copies it onto each Schedule OS line. The two
            classification failure markers
            (:attr:`DistributionKind.UNRECOGNISED` and
            :attr:`DistributionKind.MALFORMED`) are surfaced to the
            user via
            :class:`~ibkr_fa.compute.schedule_os.UnrecognisedDistributionTypeError`
            only if the row reaches the Schedule OS dispatch path;
            Schedule FA Table A3 column 10 ignores the discriminator
            and sums the row's gross amount regardless of kind.
            Defaults to :attr:`DistributionKind.UNRECOGNISED`. A row
            left at this default with an empty :attr:`type_slice`
            reaching Schedule OS raises
            :class:`~ibkr_fa.compute.schedule_os.UnrecognisedDistributionTypeError`
            (``violation="missing_kind_prefix"``), whatever its
            description, so callers building rows for Schedule OS
            must set it.
        type_slice: The substring of :attr:`description` AFTER the
            structural ``SYMBOL(ISIN)\\s+`` prefix has been stripped
            by the IBKR parser. Stored as a field so Schedule OS can
            pick the
            :class:`~ibkr_fa.compute.schedule_os.UnrecognisedDistributionTypeError`
            violation without re-running the prefix regex. Other
            sources set display text in the same shape. Empty string
            when left at its default (test fixtures). Issue #153.
        source_row_index: 0-based row index in the source IBKR CSV's
            Dividends section, threaded by the parser so error
            messages (notably
            :class:`~ibkr_fa.compute.schedule_os.UnrecognisedDistributionTypeError`)
            can point the user to a specific row in their statement
            instead of just an opaque (symbol, ISIN, payment_date)
            tuple. ``None`` allowed for backwards compatibility with
            test fixtures and any future caller that constructs a
            :class:`Dividend` outside the parser path; populated by
            the production parser path (#154).
        source_account_index: Optional privacy-safe source-account
            index retained by multi-account adapters. Form 67 keys each
            dividend payment on (source account, ISIN, payment date) for
            its per-payment treaty-rate test, and the orphan-withholding
            guard matches on the same key (#890).
        holding_date: Date at whose end a lot must be held to be
            entitled to this credit. The source adapter sets it when its
            evidence proves the date; the engine never derives it.
            ``None`` means no source-proved holding date. Only the
            Robinhood adapter sets it (issue #623).
    """

    symbol: Ticker
    isin: Isin
    payment_date: date
    amount_gross: Decimal
    currency: str
    description: str
    # source_row_index pre-dates kind/type_slice (#154 added it
    # before #153 added these two). Keep it at its historical
    # field-order position 7 so legacy positional callers
    # ``Dividend(..., source_row_index)`` continue to bind the
    # positional arg to source_row_index — NOT to kind. T5 codex
    # P2 finding on PR #189 round 2 caught the regression caused by
    # the round-1 ordering (kind/type_slice were inserted before
    # source_row_index, which silently captured a positional
    # source_row_index integer as ``kind``).
    source_row_index: int | None = None
    kind: DistributionKind = DistributionKind.UNRECOGNISED
    type_slice: str = ""
    source_account_index: int | None = None
    holding_date: date | None = None

    def __post_init__(self) -> None:
        """Fail-fast structural invariants. See class docstring."""
        require_amounts(self, "amount_gross")
        require(
            isinstance(self.kind, DistributionKind),
            what="Dividend.kind must be a DistributionKind enum member",
            kind=self.kind,
        )
        require(
            self.symbol != "",
            what="Dividend.symbol must be non-empty",
        )
        require(
            is_isin_shape(self.isin),
            what="Dividend.isin must match ISO 6166 (12 chars, leading 2-letter country code)",
            isin=self.isin,
        )
        require(
            self.amount_gross > 0,
            what=(
                "Dividend.amount_gross must be positive (reversals / "
                "corrections go through the Change in Dividend Accruals "
                "section, not the Dividends section)"
            ),
            amount_gross=self.amount_gross,
        )
        require(
            is_iso_currency_code(self.currency),
            what="Dividend.currency must be a 3-letter uppercase ISO 4217 code",
            currency=self.currency,
        )
        require(
            self.description != "",
            what="Dividend.description must be non-empty (audit trail)",
        )
        if self.source_row_index is not None:
            require(
                self.source_row_index >= 0,
                what="Dividend.source_row_index must be non-negative when present",
                source_row_index=self.source_row_index,
            )
        if self.source_account_index is not None:
            require(
                self.source_account_index >= 0,
                what="Dividend.source_account_index must be non-negative when provided",
                source_account_index=self.source_account_index,
            )
