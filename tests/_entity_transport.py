"""Transports the entity-source tests answer requests with (#1702)."""

from __future__ import annotations

import email.message
import urllib.error
from collections.abc import Iterator, Mapping

import pytest


class RecordingOpener:
    """Answers each URL from its own copy of ``responses`` and records every
    request; a URL it has no reply for fails the test."""

    def __init__(self, responses: Mapping[str, bytes]) -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"unexpected URL {url}")
        return self.responses[url]


class FixedReplyOpener:
    """Answers every URL with ``response`` and records every request."""

    def __init__(self, response: bytes) -> None:
        self.response = response
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        return self.response


def blocked_opener(url: str) -> bytes:
    """An opener for a run that must not reach the network: any request fails the test."""
    raise AssertionError(f"network call not allowed: {url}")


# Every HTTP error a test builds, closed after it: Python 3.14 warns about an
# unclosed one when it is collected.
_HTTP_ERRORS: list[urllib.error.HTTPError] = []


@pytest.fixture(autouse=True)
def close_http_errors() -> Iterator[None]:
    """Close every HTTP error :func:`http_error` built during the test. Import
    it into a test module to make it autouse there."""
    yield
    while _HTTP_ERRORS:
        _HTTP_ERRORS.pop().close()


def http_error(
    url: str, code: int, reason: str, *, header: str | None = None
) -> urllib.error.HTTPError:
    """An HTTP error for ``url``, closed after the test; ``header`` is the value
    of one ``X-Synthetic`` response header."""
    headers = email.message.Message()
    if header is not None:
        headers["X-Synthetic"] = header
    error = urllib.error.HTTPError(url, code, reason, headers, None)
    _HTTP_ERRORS.append(error)
    return error
