#!/usr/bin/env python3
"""Validate every fenced ``mermaid`` block in the repo's markdown files.

Exits non-zero if any block fails to render via the Mermaid CLI
(``mmdc``, from ``@mermaid-js/mermaid-cli``). Used by the
``mermaid-validate`` GitHub Actions job; safe to run locally too:

    npm install -g @mermaid-js/mermaid-cli  # one-off
    uv run python scripts/validate_mermaid.py

Why this exists: the original ``docs/architecture.md`` shipped with a
sequence-diagram ``Note`` containing a literal ``;`` mid-string, which
Mermaid's ``sequenceDiagram`` parser interprets as a statement
separator. The diagram silently parsed-then-failed to render on
github.com; neither pre-commit hooks nor the human review noticed.
This script catches that class of bug at CI time.

Implementation note: an earlier version used a regex to find fenced
code blocks. Codex review (PR #83) correctly pointed out a series of
edge cases the regex would miss — token boundary after ``mermaid``,
tilde fences, longer (4+ char) fences, etc. We use ``markdown-it-py``
(an official CommonMark parser) instead so the discovery is
spec-correct.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token

PUPPETEER_CONFIG = json.dumps({"args": ["--no-sandbox"]})


def find_markdown_files(root: Path) -> list[Path]:
    """Return every .md file under ``root`` except those under a ``.venv`` directory."""
    return sorted(p for p in root.rglob("*.md") if ".venv" not in p.parts)


def _is_mermaid(token: Token) -> bool:
    """``token.info`` is the full info string; the language is its first word.

    Examples:
        ``mermaid`` → language ``mermaid``
        ``mermaid title="x"`` → language ``mermaid``
        ``mermaid2`` → language ``mermaid2`` (NOT a match)
    """
    if token.type != "fence":
        return False
    language = (token.info or "").split(maxsplit=1)
    return bool(language) and language[0] == "mermaid"


def extract_blocks(md: Path) -> list[tuple[int, str]]:
    """Return ``[(line_number, block_text), ...]`` for every mermaid block in ``md``.

    ``line_number`` is 1-based and points at the line of the opening
    fence so error messages can reference it. CommonMark fence
    discovery (indented up to 3 spaces, tilde or backtick fences,
    fences of 3+ chars) is delegated to ``markdown-it-py``.
    """
    text = md.read_text(encoding="utf-8")
    parser = MarkdownIt("commonmark")
    blocks: list[tuple[int, str]] = []
    for token in parser.parse(text):
        if not _is_mermaid(token):
            continue
        # token.map is [start_line, end_line) (0-based, half-open). For
        # error reporting we want a 1-based line number pointing at the
        # opening fence.
        line_no = (token.map[0] + 1) if token.map else 0
        blocks.append((line_no, token.content))
    return blocks


def validate_block(block: str) -> tuple[bool, str]:
    """Render ``block`` via mmdc to a tempfile. Return ``(ok, stderr_or_empty)``."""
    with tempfile.TemporaryDirectory() as td:
        in_path = Path(td) / "block.mmd"
        out_path = Path(td) / "block.svg"
        # Puppeteer needs --no-sandbox on GitHub-hosted Ubuntu runners
        # (AppArmor restriction on unprivileged user namespaces). The
        # config is a no-op locally on systems that don't enforce AppArmor.
        config_path = Path(td) / "puppeteer-config.json"
        config_path.write_text(PUPPETEER_CONFIG, encoding="utf-8")
        in_path.write_text(block, encoding="utf-8")
        try:
            result = subprocess.run(
                [
                    "mmdc",
                    "-i",
                    str(in_path),
                    "-o",
                    str(out_path),
                    "--puppeteerConfigFile",
                    str(config_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            return (
                False,
                "mmdc not found on PATH. Install via `npm install -g @mermaid-js/mermaid-cli`.",
            )
        if result.returncode != 0 or not out_path.exists():
            return False, (result.stderr or result.stdout or "").strip()
        return True, ""


def main() -> int:
    root = Path(__file__).parent.parent
    md_files = find_markdown_files(root)
    failures: list[tuple[Path, int, str]] = []
    block_count = 0
    for md in md_files:
        for line_no, block in extract_blocks(md):
            block_count += 1
            ok, err = validate_block(block)
            label = f"{md.relative_to(root)}:{line_no}"
            if ok:
                print(f"  ok    {label}")
            else:
                print(f"  FAIL  {label}")
                print("        " + err.replace("\n", "\n        "))
                failures.append((md, line_no, err))

    print()
    print(f"Checked {block_count} mermaid block(s) across {len(md_files)} markdown file(s).")
    if failures:
        print(f"{len(failures)} block(s) failed to render.")
        return 1
    print("All mermaid blocks render successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
