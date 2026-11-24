"""Tests for :mod:`ibkr_fa.compute.schedule_os_interest`."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.compute.schedule_os_interest import (
    ScheduleOsInterestComputeResult,
    ScheduleOsInterestLine,
    compute_schedule_os_interest_lines,
)
from ibkr_fa.exceptions import InvariantError, MissingFxRateError
from ibkr_fa.fx.policy_rule115 import Rule115Policy
from ibkr_fa.output.portal_labels import PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST
from tests._compute_builders import make_interest_credit as _make_credit
from tests._ibkr_user_toml import ACCOUNT_AND_FX_USER_TOML

# ---- model invariants -----------------------------------------------------


def _make_line(**overrides: object) -> ScheduleOsInterestLine:
    defaults: dict[str, object] = {
        "credit_date": date(2025, 4, 30),
        "fx_specified_date": date(2025, 3, 31),
        "fx_rate_publication_date": date(2025, 3, 31),
        "fx_rate_inr_per_usd": Decimal("85.50"),
        "amount_gross_usd": Decimal("1.23"),
        "amount_gross_inr": Decimal("105.165"),
        "description": "USD Credit Interest for Apr-2025",
    }
    defaults.update(overrides)
    return ScheduleOsInterestLine(**defaults)  # type: ignore[arg-type]


def test_line_happy_path() -> None:
    """Well-formed line round-trips every field."""
    ln = _make_line()
    assert ln.credit_date == date(2025, 4, 30)
    assert ln.fx_specified_date == date(2025, 3, 31)
    assert ln.amount_gross_inr == Decimal("105.165")


def test_line_zero_amount_rejected() -> None:
    """The model is a defence-in-depth guard for the parser's
    silently-skipped non-positive rows; if a direct caller bypasses
    the parser and tries to construct a zero-amount line, raise."""
    with pytest.raises(InvariantError):
        _make_line(amount_gross_usd=Decimal("0"), amount_gross_inr=Decimal("0"))


def test_line_negative_amount_rejected() -> None:
    """Sister to test_line_zero_amount_rejected for negative amounts."""
    with pytest.raises(InvariantError):
        _make_line(
            amount_gross_usd=Decimal("-1"),
            amount_gross_inr=Decimal("-85.50"),
        )


def test_line_rate_publication_after_specified_rejected() -> None:
    """Carry-back must be backward in time; publication_date later
    than specified_date is a contract violation."""
    with pytest.raises(InvariantError):
        _make_line(
            fx_specified_date=date(2025, 3, 31),
            fx_rate_publication_date=date(2025, 4, 1),
        )


def test_line_specified_date_equal_to_credit_date_allowed() -> None:
    """Rule 115(1), Explanation (2)(c) specified date is the FY-end date,
    which CAN equal any in-FY credit_date (e.g., a Mar 31 credit
    in an FY ending Mar 31). The original PR #188 incorrectly imposed
    a ``fx_specified_date < credit_date`` invariant inherited from the
    dividend-sister model; T5 codex P1 finding caught the wrong rule
    sub-clause (was (2)(f), should be (2)(c)) and the wrong per-event
    semantic. This test locks the dropped invariant so a future refactor
    doesn't reintroduce it."""
    ln = _make_line(
        credit_date=date(2026, 3, 31),
        fx_specified_date=date(2026, 3, 31),
        fx_rate_publication_date=date(2026, 3, 31),
    )
    assert ln.fx_specified_date == ln.credit_date


def test_line_specified_date_after_credit_date_allowed() -> None:
    """An April credit in FY 2025-26 has fx_specified_date = Mar 31
    2026 (the FY-end), which is AFTER the credit_date. Same rationale
    as test_line_specified_date_equal_to_credit_date_allowed: the
    per-event preceding-month-end invariant doesn't apply under
    Rule 115(1), Explanation (2)(c)."""
    ln = _make_line(
        credit_date=date(2025, 4, 30),
        fx_specified_date=date(2026, 3, 31),
        fx_rate_publication_date=date(2026, 3, 31),
    )
    assert ln.fx_specified_date > ln.credit_date


def test_line_empty_description_rejected() -> None:
    """Audit-trail invariant."""
    with pytest.raises(InvariantError):
        _make_line(description="")


def test_compute_result_negative_count_rejected() -> None:
    """The result dataclass guards against accidental negative
    counts (e.g., a future refactor that subtracts wrongly), which
    would silently underflow the CLI's out-of-FY skip count.

    Also serves as the T1 quality fix on PR #188 — original test
    lacked a docstring; this one explains both WHAT and WHY."""
    with pytest.raises(InvariantError):
        ScheduleOsInterestComputeResult(excluded_out_of_fy_count=-1)


def test_compute_result_negative_total_rejected() -> None:
    """Negative INR total can never be valid for interest income
    (always credit, never debit per this module's positive-only
    scope) — surface as an InvariantError if a future refactor ever
    produces one. T1 quality docstring addition (PR #188)."""
    with pytest.raises(InvariantError):
        ScheduleOsInterestComputeResult(total_inr_full=Decimal("-1"))


# ---- compute pipeline -----------------------------------------------------


@pytest.fixture
def fx_policy() -> Rule115Policy:
    """The production Rule115Policy backed by the vendored SBI TTBR archive."""
    return Rule115Policy()


def test_empty_input_returns_empty_result(fx_policy: Rule115Policy) -> None:
    """Most common scenario: accounts whose cash balance never
    exceeded IBKR's minimum-balance threshold for credit interest
    parse zero InterestCredit rows. Compute must return an empty
    result with zero total — not raise, and not silently drop."""
    result = compute_schedule_os_interest_lines(
        [],
        financial_year_start=date(2025, 4, 1),
        financial_year_end=date(2026, 3, 31),
        fx_policy=fx_policy,
    )
    assert result.lines == []
    assert result.total_inr_full == Decimal("0")
    assert result.total_inr_rounded == Decimal("0")
    assert result.excluded_out_of_fy_count == 0


def test_single_in_fy_credit_computed(fx_policy: Rule115Policy) -> None:
    """Happy path: one in-FY credit, FX-converted, rounded total."""
    credit = _make_credit(
        credit_date=date(2025, 5, 5),
        amount_gross=Decimal("1.23"),
    )
    result = compute_schedule_os_interest_lines(
        [credit],
        financial_year_start=date(2025, 4, 1),
        financial_year_end=date(2026, 3, 31),
        fx_policy=fx_policy,
    )
    assert len(result.lines) == 1
    [ln] = result.lines
    assert ln.credit_date == date(2025, 5, 5)
    assert ln.amount_gross_usd == Decimal("1.23")
    assert ln.fx_specified_date == date(2025, 4, 30)
    assert ln.fx_rate_inr_per_usd > 0
    assert ln.amount_gross_inr == Decimal("1.23") * ln.fx_rate_inr_per_usd
    assert result.total_inr_full == ln.amount_gross_inr
    # Total rounded is HALF_UP to whole rupees.
    assert result.total_inr_rounded == result.total_inr_full.quantize(Decimal("1"))


def test_out_of_fy_credits_silently_filtered(fx_policy: Rule115Policy) -> None:
    """Multi-period IBKR exports may straddle FY boundaries; out-of-FY
    credits silently filter, with the count surfaced for the CLI advisory."""
    interest_credits = [
        _make_credit(credit_date=date(2025, 3, 31)),  # FY 2024-25 last day, out
        _make_credit(credit_date=date(2025, 4, 1)),  # FY 2025-26 first day, in
        _make_credit(credit_date=date(2026, 3, 31)),  # FY 2025-26 last day, in
        _make_credit(credit_date=date(2026, 4, 1)),  # FY 2026-27 first day, out
    ]
    result = compute_schedule_os_interest_lines(
        interest_credits,
        financial_year_start=date(2025, 4, 1),
        financial_year_end=date(2026, 3, 31),
        fx_policy=fx_policy,
    )
    assert len(result.lines) == 2
    assert result.excluded_out_of_fy_count == 2


def test_in_fy_credits_sorted_by_date(fx_policy: Rule115Policy) -> None:
    """The output line ordering is deterministic (sorted by
    credit_date) regardless of input order, so the per-row audit
    block in the review CSV doesn't depend on IBKR's emission order."""
    interest_credits = [
        _make_credit(credit_date=date(2025, 7, 31), source_row_index=0),
        _make_credit(credit_date=date(2025, 5, 5), source_row_index=1),
        _make_credit(credit_date=date(2025, 6, 15), source_row_index=2),
    ]
    result = compute_schedule_os_interest_lines(
        interest_credits,
        financial_year_start=date(2025, 4, 1),
        financial_year_end=date(2026, 3, 31),
        fx_policy=fx_policy,
    )
    assert [ln.credit_date for ln in result.lines] == [
        date(2025, 5, 5),
        date(2025, 6, 15),
        date(2025, 7, 31),
    ]


def test_total_inr_rounded_half_up(fx_policy: Rule115Policy) -> None:
    """CBDT-FAQ rounding is HALF_UP to whole rupees applied once at
    the aggregate level (not per-row) so per-row Decimals sum to a
    precise pre-rounded total."""
    interest_credits = [
        _make_credit(credit_date=date(2025, 5, 5), amount_gross=Decimal("0.001")),
        _make_credit(credit_date=date(2025, 6, 5), amount_gross=Decimal("0.002")),
    ]
    result = compute_schedule_os_interest_lines(
        interest_credits,
        financial_year_start=date(2025, 4, 1),
        financial_year_end=date(2026, 3, 31),
        fx_policy=fx_policy,
    )
    expected_full = sum((ln.amount_gross_inr for ln in result.lines), Decimal("0"))
    assert result.total_inr_full == expected_full
    assert result.total_inr_rounded == expected_full.quantize(Decimal("1"))


def test_january_credit_rolls_to_prior_december_specified_date_with_dividend_policy(
    fx_policy: Rule115Policy,
) -> None:
    """When the compute function is exercised against the generic
    :class:`Rule115Policy` (dividends, sub-clause (2)(e)) — NOT the
    interest-specific :class:`Rule115OtherSourcesPolicy` — the
    fx_specified_date is the preceding-month-end of credit_date. For
    a January credit, that rolls to Dec 31 of the prior calendar year.

    This test pre-dates the T5 codex P1 finding on PR #188 that
    interest should use Rule 115(1), Explanation (2)(c) (FY-end-fixed),
    not (2)(e) per-event. It now serves as a regression-lock that the
    compute function does NOT mutate the FX policy's specified_date
    semantic — whatever the FxPolicy returns flows through unchanged.
    See test_all_in_fy_credits_share_single_fx_specified_date for the
    production interest path."""
    credit = _make_credit(credit_date=date(2026, 1, 15))
    result = compute_schedule_os_interest_lines(
        [credit],
        financial_year_start=date(2025, 4, 1),
        financial_year_end=date(2026, 3, 31),
        fx_policy=fx_policy,
    )
    [ln] = result.lines
    assert ln.fx_specified_date == date(2025, 12, 31)


def test_generator_input_materialised(fx_policy: Rule115Policy) -> None:
    """A generator argument must be materialised; the iterator must
    not be exhausted by an internal precondition pass before the
    main loop runs (rev-12 quality P1-NEW-20 pattern)."""
    interest_credits = [_make_credit(credit_date=date(2025, 5, 5))]
    result = compute_schedule_os_interest_lines(
        (c for c in interest_credits),  # generator, not list
        financial_year_start=date(2025, 4, 1),
        financial_year_end=date(2026, 3, 31),
        fx_policy=fx_policy,
    )
    assert len(result.lines) == 1


def test_fy_start_after_fy_end_rejected(fx_policy: Rule115Policy) -> None:
    """Defensive precondition: caller error if start > end."""
    with pytest.raises(InvariantError):
        compute_schedule_os_interest_lines(
            [],
            financial_year_start=date(2026, 4, 1),
            financial_year_end=date(2025, 4, 1),
            fx_policy=fx_policy,
        )


def test_missing_fx_rate_propagates(fx_policy: Rule115Policy) -> None:
    """If the credit_date's preceding-month-end is before the
    archive's earliest published date, the FX policy raises
    MissingFxRateError; the compute layer propagates it (does NOT
    silently drop the row)."""
    # The vendored SBI TTBR archive starts in the early 2000s, so a
    # 1980 credit date is reliably out-of-archive.
    credit = _make_credit(credit_date=date(1980, 5, 5))
    with pytest.raises(MissingFxRateError) as exc:
        compute_schedule_os_interest_lines(
            [credit],
            financial_year_start=date(1980, 4, 1),
            financial_year_end=date(1981, 3, 31),
            fx_policy=fx_policy,
        )
    # Neither the credit date nor its specified date is printed (#1047).
    assert "1980-05-05" not in str(exc.value)
    assert "1980-04-30" not in str(exc.value)


def test_non_usd_credit_rejected_at_compute_layer(fx_policy: Rule115Policy) -> None:
    """T5 codex P2 finding on PR #188: defense-in-depth non-USD
    guard mirroring compute_schedule_os_dividend_lines. The parser
    already gates this so reaching the compute layer indicates a
    parser bypass or schema drift; silently applying a USD-only FX
    rate to a non-USD credit would corrupt the Schedule OS 1b(ix)
    INR total."""
    eur_credit = _make_credit(
        credit_date=date(2025, 5, 5),
        amount_gross=Decimal("1.23"),
        currency="EUR",
    )
    with pytest.raises(InvariantError) as exc:
        compute_schedule_os_interest_lines(
            [eur_credit],
            financial_year_start=date(2025, 4, 1),
            financial_year_end=date(2026, 3, 31),
            fx_policy=fx_policy,
        )
    assert "USD" in exc.value.what
    assert ("non_usd_currencies", None) in exc.value.context


def test_non_usd_credit_outside_fy_does_not_raise(fx_policy: Rule115Policy) -> None:
    """The non-USD guard inspects every credit regardless of FY
    bounds — a bypass even on out-of-FY credits would suggest a
    larger schema drift worth surfacing. This locks the broader
    discipline so a future refactor doesn't quietly exempt out-of-FY
    rows from the guard."""
    eur_credit = _make_credit(
        credit_date=date(2024, 5, 5),  # outside FY 2025-26
        amount_gross=Decimal("1.23"),
        currency="EUR",
    )
    with pytest.raises(InvariantError):
        compute_schedule_os_interest_lines(
            [eur_credit],
            financial_year_start=date(2025, 4, 1),
            financial_year_end=date(2026, 3, 31),
            fx_policy=fx_policy,
        )


def test_rule_115_other_sources_policy_uses_2c_citation() -> None:
    """The pipeline wires :class:`Rule115OtherSourcesPolicy`
    (Rule 115(1), Explanation (2)(c)) rather than
    :class:`Rule115Policy` (Rule 115(1), Explanation (2)(e),
    dividends) for interest, so any
    :class:`MissingFxRateError` raised on an interest credit cites the
    correct rule sub-clause and event noun.

    T5 codex P1 finding on PR #188 round 2 caught the original (wrong)
    citation: (2)(f) is capital gains, not "income from other sources".
    Verified against Indian Kanoon Rule 115 text (https://indiankanoon.org/doc/34257484/):

    * (2)(c) — "income chargeable under the head ... 'Income from other
      sources' (not being income by way of dividends and 'Interest on
      securities')" — last day of the assessee's previous year.
    * (2)(e) — dividends — preceding calendar-month-end of payment date.
    * (2)(f) — capital gains — preceding calendar-month-end of
      transfer date.

    This test inspects the policy class hierarchy + POLICY_KIND +
    rendered policy_kind_for to lock the citation against future drift.
    """
    from ibkr_fa.fx import Rule115OtherSourcesPolicy, Rule115Policy  # noqa: PLC0415

    policy = Rule115OtherSourcesPolicy(previous_year_end=date(2026, 3, 31))
    # Subclass relationship: shares the SBI archive + carry-back algorithm.
    assert isinstance(policy, Rule115Policy)
    # Correct citation (was wrongly "Rule 115(2)(f)" until PR #188 round 3).
    assert policy.POLICY_KIND == "Rule 115(1), Explanation (2)(c)"
    # The rendered policy_kind_for cites the interest-received noun AND
    # surfaces the fixed FY-end date, which comes from the FY, so a
    # missing-rate error names the specified date. It takes no event date:
    # the credit's date is input and is never printed (#1047).
    rendered = policy._policy_kind_for()
    assert rendered == (
        "Rule 115(1), Explanation (2)(c) (last day of previous year 2026-03-31 for "
        "interest received)"
    )


def test_rule_115_other_sources_policy_uses_fy_end_for_every_event() -> None:
    """Locks the fundamental (2)(c) FY-end semantic: ``rate_for`` /
    ``rate_for_audited`` return the SAME specified_date for every
    event_date in the FY, regardless of when in the FY the event
    occurred. This is what differentiates (2)(c) from (2)(e) for
    dividends (per-event preceding-month-end).
    """
    from ibkr_fa.fx import Rule115OtherSourcesPolicy  # noqa: PLC0415

    fy_end = date(2026, 3, 31)
    policy = Rule115OtherSourcesPolicy(previous_year_end=fy_end)

    # Cross-FY range of credit dates: Apr 2 (FY-start), Aug 15, Jan 5,
    # Mar 31 (FY-end itself). Every one must resolve to the same
    # specified_date == fy_end.
    for credit_date in (
        date(2025, 4, 2),
        date(2025, 8, 15),
        date(2026, 1, 5),
        date(2026, 3, 31),
    ):
        resolution = policy.rate_for_audited(credit_date)
        assert resolution.specified_date == fy_end, (
            f"Rule 115(1), Explanation (2)(c) specified date must be the FY-end "
            f"{fy_end} for every credit; got {resolution.specified_date} "
            f"for credit_date={credit_date}"
        )


def test_rule_115_other_sources_policy_previous_year_end_property() -> None:
    """The constructor arg is exposed read-only via a property so
    pipeline / CLI code can introspect the policy's specified date
    without reaching into the dunder."""
    from ibkr_fa.fx import Rule115OtherSourcesPolicy  # noqa: PLC0415

    policy = Rule115OtherSourcesPolicy(previous_year_end=date(2026, 3, 31))
    assert policy.previous_year_end == date(2026, 3, 31)


def test_all_in_fy_credits_share_single_fx_specified_date() -> None:
    """End-to-end through the compute function with the production
    :class:`Rule115OtherSourcesPolicy`: every in-FY credit on the
    output lines shares the SAME ``fx_specified_date`` (the FY-end
    date), and therefore the SAME ``fx_rate_inr_per_usd``. This is
    the user-visible consequence of Rule 115(1), Explanation (2)(c) and
    locks the FY-end-fixed semantic in the compute layer."""
    from ibkr_fa.fx import Rule115OtherSourcesPolicy  # noqa: PLC0415

    fy_end = date(2026, 3, 31)
    policy = Rule115OtherSourcesPolicy(previous_year_end=fy_end)

    interest_credits = [
        _make_credit(credit_date=date(2025, 4, 30), source_row_index=0),
        _make_credit(credit_date=date(2025, 8, 15), source_row_index=1),
        _make_credit(credit_date=date(2025, 12, 31), source_row_index=2),
        _make_credit(credit_date=date(2026, 3, 31), source_row_index=3),
    ]
    result = compute_schedule_os_interest_lines(
        interest_credits,
        financial_year_start=date(2025, 4, 1),
        financial_year_end=fy_end,
        fx_policy=policy,
    )
    assert len(result.lines) == 4
    # Every line's fx_specified_date must equal the FY-end.
    for ln in result.lines:
        assert ln.fx_specified_date == fy_end
    # And therefore every line's rate must be the same Decimal.
    rates = {ln.fx_rate_inr_per_usd for ln in result.lines}
    assert len(rates) == 1, (
        f"All in-FY credits must share one Rule 115(1), Explanation (2)(c) rate; got {rates}"
    )


def test_apr_credit_has_fx_specified_date_after_credit_date() -> None:
    """Regression-locks the dropped ``fx_specified_date < credit_date``
    invariant: an April credit (earliest in the FY) has fx_specified_date
    = the FY-end (Mar 31 of the next calendar year), which is AFTER
    the credit_date. This combination would have been rejected by the
    pre-PR-188-round-3 model invariant."""
    from ibkr_fa.fx import Rule115OtherSourcesPolicy  # noqa: PLC0415

    fy_end = date(2026, 3, 31)
    policy = Rule115OtherSourcesPolicy(previous_year_end=fy_end)
    credit = _make_credit(credit_date=date(2025, 4, 30))
    result = compute_schedule_os_interest_lines(
        [credit],
        financial_year_start=date(2025, 4, 1),
        financial_year_end=fy_end,
        fx_policy=policy,
    )
    [ln] = result.lines
    assert ln.credit_date == date(2025, 4, 30)
    assert ln.fx_specified_date == fy_end
    assert ln.fx_specified_date > ln.credit_date


# ---- e2e fixture flow -----------------------------------------------------


def test_e2e_fixture_interest_threads_through_pipeline(tmp_path: Path) -> None:
    """End-to-end: a fixture with an Interest section runs through
    run_schedule_os and surfaces the total in both the filing CSV and report."""
    from ibkr_fa.pipeline import FinancialYear  # noqa: PLC0415
    from ibkr_fa.pipeline_os import run_schedule_os  # noqa: PLC0415

    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "e2e_activity_statement_schedule_os_with_interest_fy2025_26.csv"
    )
    cfg = tmp_path / "user.toml"
    cfg.write_text(ACCOUNT_AND_FX_USER_TOML, encoding="utf-8")
    out = tmp_path / "filing.csv"
    report = run_schedule_os(
        input_path=fixture,
        config_path=cfg,
        output_path=out,
        financial_year=FinancialYear(start_year=2025),
        format_id="filing-csv",
    )
    # Fixture has 3 in-FY Interest rows: Apr/May/Jun 2025.
    assert report.interest_lines_count == 3
    assert report.interest_total_inr_rounded > Decimal("0")
    assert report.interest_credits_out_of_fy_count == 0
    with out.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))
    assert [
        PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST,
        str(report.interest_total_inr_rounded),
        "",
        "",
        "",
        "",
        "",
    ] in rows
