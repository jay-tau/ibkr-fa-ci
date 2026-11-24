"""Schedule FA JSON fragment runs and another file at a fragment's path, shared
by the source fragment cleanup tests and the report render cleanup tests
(#1941).

- ``OTHER_FRAGMENT_BYTES``: the bytes of another file at the fragment's path;
  ``place_other_file``: renames such a file into place at a path, with
  ``_REPLACE``, the real ``os.replace``;
- ``FragmentSource``: a run that writes the fragment, through its entry point
  and, when present, its CLI;
- ``robinhood_fa_source``: the ``robinhood-fa`` JSON fragment run on stubbed
  pipeline steps;
- ``robinhood_all_source``: the ``robinhood-all`` run whose legs write files,
  with the real fragment writer, ``_WRITE``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pytest

import ibkr_fa.pipeline_robinhood_outputs as outputs
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.output.cbdt_json_fragment import (
    CBDT_JSON_FRAGMENT_FORMAT_ID,
    CbdtScheduleFaJsonWriter,
)
from ibkr_fa.pipeline_robinhood import RobinhoodPipelineError, run_robinhood_all, run_robinhood_fa
from tests._robinhood_cli import FINANCIAL_YEAR, robinhood_main, robinhood_main_all
from tests._robinhood_pipeline_stubs import json_fa, stub_successful_all, stub_writing_all

OTHER_FRAGMENT_BYTES = b'{"ScheduleFA": {"someone": "else"}}'
# The real functions, before any test patches them.
_REPLACE = os.replace
_WRITE = CbdtScheduleFaJsonWriter.write


def place_other_file(path: Path) -> None:
    """Put another file at ``path``: written beside it and renamed into place, so
    its identity differs from any file the run staged."""
    other = path.with_name("other.json")
    other.write_bytes(OTHER_FRAGMENT_BYTES)
    _REPLACE(other, path)


@dataclass(frozen=True)
class FragmentSource:
    """A run that writes the fragment, ready to run through either layer."""

    fragment: Path
    refusal: type[IbkrFaError]  # its typed refusal
    entry: Callable[[], object]
    cli: Callable[[], int] | None = None  # None: a helper with no CLI

    def left(self) -> list[str]:
        return sorted(path.name for path in self.fragment.parent.iterdir())


def robinhood_fa_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _request: pytest.FixtureRequest
) -> FragmentSource:
    """No option trades, so the F&O warning doesn't wrap the run's errors."""
    stub_successful_all(monkeypatch, json_fa())
    out = tmp_path / "out"
    return FragmentSource(
        out / outputs.JSON_FILENAME,
        RobinhoodPipelineError,
        partial(
            run_robinhood_fa,
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=out,
            financial_year=FINANCIAL_YEAR,
            itr_form="ITR-2",
            format_id=CBDT_JSON_FRAGMENT_FORMAT_ID,
        ),
        partial(robinhood_main, "robinhood-fa", tmp_path, "--format", CBDT_JSON_FRAGMENT_FORMAT_ID),
    )


def robinhood_all_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _request: pytest.FixtureRequest,
    *,
    failing: str | None = None,
) -> FragmentSource:
    """Every leg writes a file (``failing``'s then raises), and the automatic
    JSON is the real fragment."""
    out = tmp_path / "out"
    stub_writing_all(monkeypatch, out, fa=json_fa(), failing=failing)
    monkeypatch.setattr(CbdtScheduleFaJsonWriter, "write", _WRITE)
    return FragmentSource(
        out / outputs.JSON_FILENAME,
        RobinhoodPipelineError,
        partial(
            run_robinhood_all,
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=out,
            financial_year=FINANCIAL_YEAR,
            itr_form="ITR-2",
            fetch_prices=False,
            debug=True,
        ),
        partial(robinhood_main_all, tmp_path),
    )
