"""Tests for :mod:`ibkr_fa.models._dtaa_article` (#1079)."""

from __future__ import annotations

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models._dtaa_article import bare_article_number

_FIELD = "Form67Entry.section_90_dtaa_article (col 9)"


@pytest.mark.parametrize(
    ("citation", "expected"),
    [("Article 25(2)(a)", "25"), ("Article 10", "10"), (None, None)],
)
def test_bare_article_number(citation: str | None, expected: str | None) -> None:
    assert bare_article_number(citation, field=_FIELD) == expected


@pytest.mark.parametrize(
    "citation",
    [
        "Section 25",
        "Article 0",
        "Article 25 (2)",
        "25",
        "Article 10(2)(b) of India-US DTAA",
        10,
        {},
        [],
        b"Article 10",
    ],
)
def test_bare_article_number_refuses_other_shapes(citation: object) -> None:
    with pytest.raises(InvariantError) as exc:
        bare_article_number(citation, field=_FIELD)
    assert str(exc.value) == (
        f"InvariantError: {_FIELD} must be a DTAA citation shaped like 'Article 10(2)(b)'"
    )
    assert exc.value.context == ()
