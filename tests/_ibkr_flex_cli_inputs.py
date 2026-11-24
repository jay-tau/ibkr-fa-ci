"""The Flex Query Open Positions input and earlier Schedule FA outputs that the
CLI tests and several other test modules share.

- ``CLI_OPEN_POSITIONS_SECTION``: an ``Open Positions (LOT)`` section, a header
  and one USD stock lot.
- ``earlier_schedule_fa_outputs``: writes an earlier run's Schedule FA outputs
  for an output path and returns their paths.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests._flex_section_rows import OPEN_POSITIONS_HEADER

# Synthetic test data in the same shape as test_pipeline.py, so any input
# the pipeline accepts also flows cleanly through the CLI.
CLI_OPEN_POSITIONS_SECTION = (
    OPEN_POSITIONS_HEADER
    + """
"USD","1","STK","COMMON","XQAW","SAMPLE ISSUER AW INC","99990E103","US99990E1038","NASDAQ","US","20251231","10","41.25","412.5","38.4","384","Long","LOT","20250312","20250312"
"""  # noqa: E501
)


def earlier_schedule_fa_outputs(out: Path, format_id: str) -> tuple[Path, ...]:
    """Write an earlier run's outputs for ``out``: the A3 file and its A2
    sibling, or a CBDT JSON fragment ibkr-fa wrote."""
    if format_id == "cbdt-json-fragment":
        out.write_text(
            json.dumps({"_ibkrFa": {"format": "cbdt-json-fragment"}, "ScheduleFA": {}}),
            encoding="utf-8",
        )
        return (out,)
    earlier = (out, out.with_name(f"schedule-fa-a2{out.suffix}"))
    for path in earlier:
        path.write_text("earlier run", encoding="utf-8")
    return earlier
