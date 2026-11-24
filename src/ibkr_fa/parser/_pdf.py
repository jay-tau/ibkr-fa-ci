"""Shared ``pdftotext -layout`` helper for PDF parsers.

PDF parsers and pipelines render a PDF to text with poppler's ``pdftotext``
(already on PATH — no new dependency) and parse the text. This module centralises
that one subprocess call so the parsers stay pure over text and the shell-out /
"install poppler-utils" handling lives in one place.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ibkr_fa.exceptions import ParseError, errno_name

_TIMEOUT_SECONDS = 30


def pdftotext_layout(path: Path, *, doc_kind: str, source: str) -> str:
    """Return the ``pdftotext -layout`` text of ``path``.

    ``doc_kind`` names the document in the missing-binary message (e.g.
    ``"NetBenefits PDFs"``), and ``source`` labels it in every refusal.

    Raises:
        ParseError: ``pdftotext`` is missing, can't start, times out, fails or
            prints non-UTF-8 text. The refusal names ``source`` and, for an
            ``OSError``, its errno name; never ``path``, pdftotext's stderr or
            the exception's text, and it chains no exception.
    """
    try:
        return subprocess.run(
            ["pdftotext", "-enc", "UTF-8", "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=_TIMEOUT_SECONDS,
        ).stdout
    except FileNotFoundError as exc:
        message = (
            f"'pdftotext' not found on PATH ({errno_name(exc)}); "
            f"install poppler-utils to read {doc_kind}"
        )
    except subprocess.TimeoutExpired:
        message = f"pdftotext timed out reading {source}"
    except subprocess.CalledProcessError:
        message = "pdftotext failed"
    except UnicodeDecodeError:
        message = "pdftotext output was not valid UTF-8"
    except OSError as exc:
        # pdftotext is present but could not be started (e.g. not executable) —
        # surface the repo's ParseError contract rather than a raw OS traceback.
        message = f"could not run pdftotext ({errno_name(exc)})"
    # Refused outside the handler: pdftotext's output and stderr ride on the
    # subprocess errors, and the undecodable bytes on a decode error.
    raise ParseError(section=source, row_index=0, column=None, message=message, raw_value="")
