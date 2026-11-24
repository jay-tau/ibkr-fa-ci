"""Tests for ITR-2 Schedule OS portal labels."""

from __future__ import annotations

import inspect
import typing

from ibkr_fa.output.portal_labels import (
    PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND,
    PORTAL_FIELD_LABEL_OS_OTHER_INTEREST,
    _ay_2026_27,
)


def test_labels_match_official_ay2026_27_utility() -> None:
    assert PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND == (
        "Schedule OS / 1a(i) Dividend income [other than (ii) and (iii)]"
    )
    assert PORTAL_FIELD_LABEL_OS_OTHER_INTEREST == (
        "Schedule OS / 1b(ix) Others including interest from Companies, NBFCs & HFCs"
    )


def test_labels_are_clean_strings() -> None:
    for label in (PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND, PORTAL_FIELD_LABEL_OS_OTHER_INTEREST):
        assert label
        assert label.strip() == label
        assert label.isascii()


def test_labels_are_annotated_final_str_in_canonical_snapshot() -> None:
    """The label's CANONICAL home is the per-AY snapshot module
    (issue #65). :mod:`ibkr_fa.output.portal_labels` re-exports it
    from the current-AY snapshot. The ``Final[str]`` annotation
    must live on the snapshot's definition, not the re-export
    sites — that's what this test pins."""
    hints = typing.get_type_hints(_ay_2026_27, include_extras=True)
    assert "PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND" in hints
    assert "PORTAL_FIELD_LABEL_OS_OTHER_INTEREST" in hints
    source = inspect.getsource(_ay_2026_27)
    assert "PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND: Final[str]" in source
    assert "PORTAL_FIELD_LABEL_OS_OTHER_INTEREST: Final[str]" in source
