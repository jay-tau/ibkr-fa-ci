"""One realised cash-balance interest credit, ready for INR conversion.

An :class:`InterestCredit` is the typed shape of one row from the IBKR
Activity Statement's ``Interest`` section. Each row records a credit
of cash-balance interest to the user's account on a specific date —
typically IBKR's monthly USD-credit-interest payment on idle USD cash
above the minimum-balance threshold (see IBKR's interest-rate schedule).

Schedule OS taxes this as **interest income from other sources** at
the user's marginal slab rate (residuary Section 56(1) /
Section 56(1) residuary charge — *not* Section 56(2)(id), which is
narrowly "interest on securities" as defined by
Section 2(28B) covering Government / debenture coupons, slab-rated,
not 1a-dividend treatment). Schedule FSI breaks it down per source
country (the issuer's country — for IBKR cash-balance interest the
issuer is IBKR LLC, US-domiciled, so source country is US).
Schedule TR / Form 67 then claims any foreign-tax credit if a treaty
applies; IBKR-emitted cash-balance interest is generally NOT
withheld-on at source under the India-US DTAA but the model carries
gross amount uniformly so downstream code can apply per-country
treaty logic without re-parsing.

IBKR's ``Interest`` section can carry credit interest (positive),
debit interest (negative: margin interest the user paid), signed
corrections and accrual rows. The parser makes an
:class:`InterestCredit` only from a positive row whose description reads
exactly ``<CCY> Credit Interest for <Mon>-<YYYY>`` (#1280), so this model
is always positive. Every
other non-footer row becomes an :class:`InterestNonCredit`: debit interest, which
isn't income, or an unproven row, which the commands that use interest
refuse (#905; see :mod:`ibkr_fa.parser.activity_statement.interest`).

Sister model to :class:`~ibkr_fa.models.Dividend` (also a
periodic-cash-credit on a holding) — same fail-fast field-level
invariants. Unlike :class:`Dividend`, :class:`InterestCredit` has no
``symbol`` / ``isin``: cash-balance interest is paid by the broker on
the cash balance itself, not on a specific security, so the
ticker-ISIN fields would be meaningless.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from ibkr_fa.invariants import require
from ibkr_fa.models._identifier_checks import is_iso_currency_code


@dataclass(frozen=True, slots=True)
class InterestCredit:
    """One cash-balance interest credit received during the reporting period.

    Construction-time invariants (see :meth:`__post_init__`) enforce:

    * ``amount_gross`` is a finite positive :class:`~decimal.Decimal`.
      The Interest-section parser turns every other non-footer row (debit
      interest, zero, a signed correction, an accrual, a positive row
      without IBKR's credit-interest wording) into an
      :class:`InterestNonCredit` instead, so they never reach this
      model in the dispatcher path. This ``__post_init__`` invariant
      therefore acts as a defence-in-depth check (raises
      :class:`~ibkr_fa.exceptions.InvariantError`, not
      :class:`~ibkr_fa.exceptions.ParseError`) for direct callers
      that construct an :class:`InterestCredit` outside the parser
      path (e.g., test fixtures). Note that this is the opposite
      of :class:`~ibkr_fa.models.WithholdingTax`'s sign policy:
      WHT carries SIGNED amounts (negative = paid, positive =
      reversal) because the FSI / Form 67 reconciler needs the
      original signed pair; InterestCredit carries only positive
      amounts because Schedule OS only files credits.
    * ``currency`` is a 3-letter ISO 4217 uppercase code. v1 only
      handles USD downstream but the model carries the value so
      future multi-currency support (#52) doesn't require a model
      change.
    * Non-empty ``description`` — preserved verbatim for the audit
      trail (lets a reviewer map this record back to a specific row
      in the user's statement).

    Attributes:
        credit_date: Calendar date IBKR credited the interest to
            the user's cash balance. Used as the FX-conversion date
            for per-credit INR derivation under Rule 115 (or
            Rule 128 for FTC) in the compute layer.
        amount_gross: Gross interest credit in :attr:`currency`,
            BEFORE any source-country withholding tax. Positive
            :class:`~decimal.Decimal`.
        currency: ISO 4217 code of :attr:`amount_gross`.
        description: Raw verbatim description from the IBKR row
            (e.g., ``"USD Credit Interest for Apr-2025"``).
            Preserved for the audit trail.
        source_row_index: 0-based row index in the source IBKR CSV's
            Interest section, threaded by the parser so future
            error messages can point the user to a specific row in
            their statement. ``None`` allowed for backwards
            compatibility with test fixtures and any future caller
            that constructs an :class:`InterestCredit` outside the
            parser path; populated by the production parser path.
    """

    credit_date: date
    amount_gross: Decimal
    currency: str
    description: str
    source_row_index: int | None = None

    def __post_init__(self) -> None:
        """Fail-fast structural invariants. See class docstring."""
        require(
            self.amount_gross.is_finite(),
            what="InterestCredit.amount_gross must be a finite Decimal",
            amount_gross=str(self.amount_gross),
        )
        require(
            self.amount_gross > 0,
            what=(
                "InterestCredit.amount_gross must be positive (IBKR "
                "emits credit interest as positive amounts; negative "
                "or zero suggests a malformed row, debit interest or "
                "an unproven correction, which the parser returns as "
                "InterestNonCredit)"
            ),
            amount_gross=str(self.amount_gross),
        )
        require(
            is_iso_currency_code(self.currency),
            what="InterestCredit.currency must be a 3-letter uppercase ISO 4217 code",
            currency=self.currency,
        )
        require(
            self.description != "",
            what="InterestCredit.description must be non-empty (audit trail)",
        )
        if self.source_row_index is not None:
            require(
                self.source_row_index >= 0,
                what="InterestCredit.source_row_index must be non-negative when present",
                source_row_index=self.source_row_index,
            )


class InterestRowGroup(StrEnum):
    """Why an Activity Statement Interest row is not an interest credit (#905).

    Debit interest is margin interest the user paid, which isn't income.
    Every other member is unproven: the row could change filed interest, but
    the statement doesn't show how. Each value is the reason as the
    refusals print it.
    """

    DEBIT_INTEREST = "debit interest"
    UNPROVEN_ACCRUAL = "its description mentions an accrual"
    UNPROVEN_POSITIVE = "a positive amount without IBKR's credit-interest wording"
    UNPROVEN_NON_CREDIT = "a negative or zero amount that isn't debit interest"

    @property
    def unproven(self) -> bool:
        """Whether a row in this group blocks the commands that use interest."""
        return self is not InterestRowGroup.DEBIT_INTEREST


@dataclass(frozen=True, slots=True, kw_only=True)
class InterestNonCredit:
    """One Activity Statement Interest row that isn't an interest credit (#905).

    The parser classifies each non-footer row; the first match wins:

    1. :attr:`InterestRowGroup.UNPROVEN_ACCRUAL`: the description contains
       ``"accrual"`` (case-insensitive), whatever the sign;
    2. a positive amount whose description reads exactly ``<CCY> Credit
       Interest for <Mon>-<YYYY>`` is an :class:`InterestCredit`, not this
       record;
    3. :attr:`InterestRowGroup.UNPROVEN_POSITIVE`: any other positive
       amount, e.g. a refund of debit interest (#1280);
    4. :attr:`InterestRowGroup.DEBIT_INTEREST`: a negative amount whose
       description contains ``"debit interest"`` (case-insensitive);
    5. :attr:`InterestRowGroup.UNPROVEN_NON_CREDIT`: anything else, e.g. a
       negative correction to a credit, or a zero row.

    Debit interest is left out of income and of Table A2's gross amount
    credited. An unproven row makes ``schedule-os``, ``schedule-fsi``,
    ``schedule-tr`` and ``form-67`` refuse, and leaves Table A2's Interest
    nature unresolved.

    Attributes:
        group: Which of the groups above the row is in.
        amount: The signed ``Amount`` cell.
        currency: The ``Currency`` cell, which the parser checks is three
            uppercase ASCII letters. :meth:`describe` leaves it out: a short
            name such as ``ANN`` has the same shape, so a shifted row could
            put the holder's name there (#1027).
        row_date: The ``Date`` cell.
        description: The ``Description`` cell, verbatim.
        file_line: The 1-based line of the statement the row starts on.
    """

    group: InterestRowGroup
    amount: Decimal
    currency: str
    row_date: date
    description: str
    file_line: int

    def __post_init__(self) -> None:
        """Fail-fast structural invariants. See class docstring."""
        require(
            self.amount.is_finite(),
            what="InterestNonCredit.amount must be a finite Decimal",
            amount=str(self.amount),
        )
        require(
            self.group is InterestRowGroup.UNPROVEN_ACCRUAL
            or (self.group is InterestRowGroup.UNPROVEN_POSITIVE and self.amount > 0)
            or (self.group is InterestRowGroup.DEBIT_INTEREST and self.amount < 0)
            or (self.group is InterestRowGroup.UNPROVEN_NON_CREDIT and self.amount <= 0),
            what="InterestNonCredit.amount must match its group's sign",
            group=self.group.name,
            amount=str(self.amount),
        )
        require(
            self.description != "",
            what="InterestNonCredit.description must be non-empty (audit trail)",
        )
        require(
            self.file_line >= 1,
            what="InterestNonCredit.file_line must be a 1-based line number",
            file_line=self.file_line,
        )

    def describe(self) -> str:
        """The row as the refusals and run reports name it: its line and group.

        The description, amount and date are input, so the user finds the
        row by its line (owner decision on #1047).
        """
        return f"line {self.file_line} ({self.group.value})"
