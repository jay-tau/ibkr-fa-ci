"""Shared steps of the Robinhood evidence caches and their HTTP fetches.

The identity, GLEIF legal-entity, N-CEN fund-entity and F-6 ADR source
modules hash and publish their immutable JSON artifacts, check SHA-256 digest
shapes and collapse whitespace through these names. The N-CEN and F-6 modules
also open artifacts here, and the identity and GLEIF modules retry requests.
Each caller supplies its own error class, texts and chain style.

The canonical JSON is hashed into every artifact's payload hash and into the
fingerprints that name the artifact folders, so a change here makes every
existing artifact unusable.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, NoReturn, cast

from ibkr_fa.exceptions import errno_name

HASH_KEY: Final[str] = "payload_sha256"
MAX_ATTEMPTS: Final[int] = 3
TIMEOUT_SECONDS: Final[float] = 30.0
RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})
# A whole string of exactly 64 lowercase hex digits: a SHA-256 hex digest.
SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{64}\Z")

# (request, timeout) -> (final URL after redirects, body).
type UrlOpen = Callable[[urllib.request.Request, float], tuple[str, bytes]]


def canonical_json(payload: Mapping[str, object]) -> str:
    """``payload`` as compact JSON with sorted keys; a non-finite number raises ``ValueError``."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_sha256(payload: Mapping[str, object]) -> str:
    """The hex SHA-256 of ``payload``'s :func:`canonical_json`, encoded as UTF-8."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def as_object(value: object) -> Mapping[str, object] | None:
    """``value`` when it is a JSON object (a ``dict``), else ``None``."""
    return cast("Mapping[str, object]", value) if isinstance(value, dict) else None


def is_schema(value: object, version: int) -> bool:
    """True when ``value`` is the integer ``version``; a ``bool`` never is."""
    return isinstance(value, int) and not isinstance(value, bool) and value == version


def collapsed(value: str) -> str:
    """Join the items from ``value.split()`` with one space between them."""
    return " ".join(value.split())


def as_text(value: object) -> str:
    """:func:`collapsed` ``value`` when it is a ``str``, else ``""``."""
    return collapsed(value) if isinstance(value, str) else ""


def publish_once(
    path: Path,
    payload: Mapping[str, object],
    *,
    refuse: Callable[[OSError], NoReturn],
) -> None:
    """Write ``payload`` plus its :data:`HASH_KEY` hash to ``path``, never over a file.

    The canonical JSON and a newline are staged in a temporary file beside
    ``path`` and hard-linked into place; the staged file is removed either way.
    ``refuse`` is called inside the ``except`` for the hard link's ``OSError``
    (``FileExistsError`` when ``path`` exists), so the caller's refusal chains
    as if it were raised there. The hash, ``mkdir``, staging and write errors
    propagate unmapped.
    """
    artifact = dict(payload)
    artifact[HASH_KEY] = canonical_sha256(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(artifact))
            handle.write("\n")
        try:
            path.hardlink_to(tmp_path)
        except OSError as exc:
            refuse(exc)
    finally:
        tmp_path.unlink(missing_ok=True)


def open_sealed_artifact(
    path: Path,
    *,
    label: str,
    error: Callable[[str], Exception],
) -> tuple[Mapping[str, object], str] | None:
    """Open the ADR depositary or N-CEN selection artifact at ``path``.

    Returns ``None`` when ``path`` doesn't exist. Otherwise returns the
    artifact and the SHA-256 recomputed over it without :data:`HASH_KEY`,
    which the caller compares with the stored hash. Caught ``OSError`` failures
    use ``error``, ``label``, an errno name and ``from None``; other caught errors
    use ``from exc``. A non-object refuses as malformed without an explicit cause.
    """
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise error(f"{label} could not be inspected ({errno_name(exc)})") from None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except RecursionError as exc:
        raise error(f"{label} is nested too deeply") from exc
    except OSError as exc:
        raise error(f"{label} could not be read ({errno_name(exc)})") from None
    except (MemoryError, ValueError) as exc:
        raise error(f"{label} is unreadable") from exc
    artifact = as_object(raw)
    if artifact is None:
        raise error(f"{label} is malformed")
    payload = {key: value for key, value in artifact.items() if key != HASH_KEY}
    try:
        actual = canonical_sha256(payload)
    except RecursionError as exc:
        raise error(f"{label} is nested too deeply") from exc
    except ValueError as exc:
        raise error(f"{label} contains a non-finite number") from exc
    return artifact, actual


def fetch_with_retries(
    request: urllib.request.Request,
    *,
    transport: UrlOpen,
    backoff_seconds: float,
    sleep: Callable[[float], None],
    refuse_status: Callable[[urllib.error.HTTPError], NoReturn],
    refuse_unreachable: Callable[[Exception], NoReturn],
) -> tuple[str, bytes]:
    """``transport``'s final URL and body for ``request``, after up to :data:`MAX_ATTEMPTS` tries.

    Each try has a :data:`TIMEOUT_SECONDS` timeout. Before try ``n + 1`` it
    sleeps ``backoff_seconds * n``. An HTTP status outside
    :data:`RETRYABLE_STATUS`, or any status on the last try, goes to
    ``refuse_status``; a transport failure on the last try goes to
    ``refuse_unreachable``. Both are called inside the ``except``, so the
    caller's refusal chains as if it were raised there. The caller checks the
    final URL and the body.
    """
    for attempt in range(MAX_ATTEMPTS):
        final_attempt = attempt == MAX_ATTEMPTS - 1
        try:
            final_url, body = transport(request, TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_STATUS or final_attempt:
                refuse_status(exc)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            if final_attempt:
                refuse_unreachable(exc)
        else:
            return final_url, body
        sleep(backoff_seconds * (attempt + 1))
    raise AssertionError("the last try returns or refuses")  # pragma: no cover
