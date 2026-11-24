"""The bare DTAA article number Form 67, Schedule FSI and Schedule TR file (#1079).

The portal and Quicko want the article number alone, such as ``10``; the models
carry the full citation, such as ``Article 10(2)(b)``.
"""

from __future__ import annotations

import re
from typing import Final

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.invariants import invariant_error

_CITATION_RE: Final[re.Pattern[str]] = re.compile(r"Article ([1-9][0-9]{0,2})(?:\([A-Za-z0-9]+\))*")


def _citation_error(field: str) -> InvariantError:
    """The refusal of a ``field`` value that isn't a supported DTAA citation.

    It carries no context, so no citation reaches the error.
    """
    return invariant_error(what=f"{field} must be a DTAA citation shaped like 'Article 10(2)(b)'")


def require_citation_type(citation: object, *, field: str) -> None:
    """Refuse, as :func:`bare_article_number` does, a ``field`` value that is
    neither ``None`` nor a ``str``."""
    if not isinstance(citation, str | None):
        raise _citation_error(field)


def bare_article_number(citation: object, *, field: str) -> str | None:
    """Return the article number of a citation like ``Article 10(2)(b)``.

    ``None`` gives ``None``. Anything else, including a value that isn't a
    ``str``, raises an :class:`~ibkr_fa.exceptions.InvariantError` that names
    ``field`` and carries no context, so no citation reaches the error. The
    citation isn't stripped or coerced.
    """
    if citation is None:
        return None
    match = _CITATION_RE.fullmatch(citation) if isinstance(citation, str) else None
    if match is None:
        raise _citation_error(field)
    return match.group(1)


__all__ = ["bare_article_number", "require_citation_type"]
