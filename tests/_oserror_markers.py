"""The ``OSError`` privacy markers and their helpers, shared by the output
cleanup tests and the CLI output tests (#1858)."""

import errno

# Privacy markers (#1048): an ``OSError``'s text and file name are never
# printed, only its errno name.
STRERROR_MARKER = "Synthetic Strerror Marker"
FILENAME_MARKER = "Synthetic Holder Two"


def marked_oserror() -> OSError:
    """An ``EACCES`` ``OSError`` (a ``PermissionError``) carrying both markers."""
    return OSError(errno.EACCES, STRERROR_MARKER, f"{FILENAME_MARKER}/x")


def assert_no_oserror_markers(message: str) -> None:
    """Unsupplied markers guard future echoes; their absence alone does not
    demonstrate a change from base."""
    for marker in (STRERROR_MARKER, FILENAME_MARKER):
        assert marker.lower() not in message.lower()
