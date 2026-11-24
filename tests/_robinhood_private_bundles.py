"""The private Robinhood statement-series check.

``check_private_statement_series`` reads each PDF that
``private_robinhood_pdfs("statement")`` finds under ``rh``, passes over the
ones the parser refuses as Robinhood Crypto statements and fails on any other
refusal. It skips when no PDF there is a brokerage statement. It then fails
when the brokerage statements don't form validated bundles, or when the
bundles don't match the statements they were built from
(``_bundles_match_statements``). It reads ``read_private_statement`` from this
module's globals when it runs, so a test can patch it here, as
``tests/test_robinhood_private.py`` does. Callers run it through
``private_test``, which lets its skips through and turns any failure into one
fixed message.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise

import pytest

from ibkr_fa.normalize.robinhood import (
    RobinhoodReconciliationError,
    RobinhoodStatementBundle,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatement,
    RobinhoodStatementFamily,
)
from tests._robinhood_private import private_robinhood_pdfs, read_private_statement
from tests._robinhood_refusals import CRYPTO_REFUSAL


def _bundles_match_statements(
    bundles: tuple[RobinhoodStatementBundle, ...],
    statements: list[RobinhoodStatement],
) -> bool:
    """The bundles partition exactly the parsed statements, one account
    each; each bundle's statements run in period order without gaps, its
    coverage spans them (start <= end), and its family is the explicit
    family its statements print."""
    bundled = [id(statement) for bundle in bundles for statement in bundle.statements]
    return (
        sorted(bundled) == sorted(id(statement) for statement in statements)
        and len({bundle.account_number for bundle in bundles}) == len(bundles)
        and all(
            bundle.statements
            and {statement.account_number for statement in bundle.statements}
            == {bundle.account_number}
            and bundle.coverage_start == bundle.statements[0].period_start
            and bundle.coverage_end == bundle.statements[-1].period_end
            and bundle.coverage_start <= bundle.coverage_end
            and all(
                current.period_start == previous.period_end + timedelta(days=1)
                for previous, current in pairwise(bundle.statements)
            )
            and bundle.family is not RobinhoodStatementFamily.UNSPECIFIED
            and bundle.family in {statement.family for statement in bundle.statements}
            for bundle in bundles
        )
    )


def _validated_bundles(
    statements: list[RobinhoodStatement],
) -> tuple[RobinhoodStatementBundle, ...] | None:
    try:
        return validate_robinhood_statement_bundles(statements)
    except RobinhoodReconciliationError:
        return None


def check_private_statement_series() -> None:
    _root, paths = private_robinhood_pdfs("statement")
    statements: list[RobinhoodStatement] = []
    for path in paths:
        statement = read_private_statement(path)
        if isinstance(statement, str):
            if statement != CRYPTO_REFUSAL:
                pytest.fail("private statement parsing failed", pytrace=False)
            continue
        statements.append(statement)
    if not statements:
        pytest.skip("private dataset rh/<statement folder> holds no brokerage statement")
    bundles = _validated_bundles(statements)
    if bundles is None:
        pytest.fail("private statement bundle validation failed", pytrace=False)
    if not _bundles_match_statements(bundles, statements):
        pytest.fail("private statement bundle retention audit failed", pytrace=False)
