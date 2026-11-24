"""Find the Schedule OS review totals by label (#1074).

Shared by the review CSV, xlsx and end-to-end tests, so none of them finds a
total by its position from the end of the file.
"""

from __future__ import annotations

from collections.abc import Sequence

from ibkr_fa.output.portal_labels import (
    PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND,
    PORTAL_FIELD_LABEL_OS_OTHER_INTEREST,
)

AUDIT_TOTAL_LABEL = "_audit: 1a(i) TOTAL Gross INR (full precision)"


def only_row_index(rows: Sequence[Sequence[object]], label: object) -> int:
    """The index of the one row whose first cell is ``label``."""
    indices = [index for index, row in enumerate(rows) if row and row[0] == label]
    assert len(indices) == 1, (label, indices)
    return indices[0]


def total_rows(rows: Sequence[Sequence[object]]) -> tuple[int, int, int]:
    """The indices of the 1a(i) audit, 1a(i) portal and 1b(ix) total rows.

    Each label appears once. A blank row comes before the audit total, the
    three rows follow each other, and the 1b(ix) row ends the file unless a
    blank row and the ``_other_income:`` 1e block follow it.
    """
    audit = only_row_index(rows, AUDIT_TOTAL_LABEL)
    portal = only_row_index(rows, PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND)
    interest = only_row_index(rows, PORTAL_FIELD_LABEL_OS_OTHER_INTEREST)
    assert (portal, interest) == (audit + 1, audit + 2)
    assert not any(rows[audit - 1])
    after = rows[interest + 1 :]
    assert not after or (not any(after[0]) and after[1][0] == "_other_income:")
    return audit, portal, interest
