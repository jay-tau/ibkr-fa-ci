"""Ways the entity-source tests change or damage a sealed cache artifact (#1702).

Each caller passes its own markers and changes; these only hold the steps.
"""

from __future__ import annotations

import errno
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


def reseal(artifact: dict[str, Any]) -> None:
    """Re-hash a changed artifact in place, so its payload hash stays valid."""
    payload = {key: value for key, value in artifact.items() if key != "payload_sha256"}
    artifact["payload_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def reseal_file(path: Path, change: Callable[[dict[str, Any]], object]) -> None:
    """Change a cache artifact, then re-hash so it passes its integrity check."""
    artifact = json.loads(path.read_text(encoding="utf-8"))
    change(artifact)
    reseal(artifact)
    path.write_text(json.dumps(artifact), encoding="utf-8")


def files(tmp_path: Path) -> dict[Path, bytes]:
    """Every file below ``tmp_path`` with its bytes."""
    return {item: item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()}


def break_only_artifact(folder: Path) -> None:
    """Make the one cache artifact under ``folder`` unreadable."""
    (path,) = (item for item in folder.rglob("*") if item.is_file())
    path.write_bytes(b"not json")


def dangle(path: Path) -> None:
    """Replace the artifact with a link to a file that doesn't exist."""
    path.unlink()
    path.symlink_to(path.parent / "missing.json")


def denied(method: str, strerror: str) -> Callable[[Path, pytest.MonkeyPatch], tuple[str, ...]]:
    """``Path.<method>`` fails on the artifact with its real path as the file
    name, as ``os.lstat`` and ``open`` do, and ``strerror`` as its text."""

    def damage(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
        original = getattr(Path, method)

        def failing(self: Path, *args: object, **kwargs: object) -> object:
            if self == path:
                raise PermissionError(errno.EACCES, strerror, str(self))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, failing)
        return (path.parent.name, strerror)

    return damage


def write_undecodable(path: Path, marker: str) -> tuple[str, ...]:
    """A cache document that isn't UTF-8: a decode error keeps the file's
    bytes, so ``marker``."""
    path.write_bytes(b'{"' + marker.encode() + b'": "\xff"}')
    return (marker,)


def write_unparsable(path: Path, marker: str) -> tuple[str, ...]:
    """A cache document that isn't JSON: a JSON error keeps the document, so
    ``marker``."""
    path.write_text(f'{{"{marker}": ', encoding="utf-8")
    return (marker,)
