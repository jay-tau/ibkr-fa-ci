"""Guard the layering: lower layers never import the pipeline or the CLI (#603).

Output writers, the Schedule OS compute module (#606), FTC
eligibility (#624) and source coverage (#625) never import the parser
either.

Scans every module under the lower-layer packages, including imports
inside ``if TYPE_CHECKING:`` blocks and function bodies, the
``from ibkr_fa import pipeline`` form, and relative imports.

The Schedule FA A2 builders and their models never import
``ibkr_fa.config`` (#618).

The normalize layer never imports compute or output (#636).

The NetBenefits parser and normalizer never import config, compute or
output (#631).

Compute never imports a network or cache module (#634), or the output
layer (#759).

Compute takes the Form 1042-S amount and copy types from ``models``; only
``robinhood_income`` still imports Robinhood's wrapper types (#637).

The shared Schedule CG filing result never imports a writer or a Robinhood
module (#642).
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

import ibkr_fa

_PACKAGE_ROOT = Path(ibkr_fa.__file__).parent
_LOWER_LAYERS = (
    "parser",
    "normalize",
    "compute",
    "output",
    "fx",
    "models",
    "reference",
    "prices",
)


def _is_upper_layer(module: str) -> bool:
    """True for ``ibkr_fa.pipeline``, any ``ibkr_fa.pipeline_*`` and ``ibkr_fa.cli``."""
    parts = module.split(".")
    return (
        len(parts) > 1
        and parts[0] == "ibkr_fa"
        and (parts[1] in {"pipeline", "cli"} or parts[1].startswith("pipeline_"))
    )


def _is_parser(module: str) -> bool:
    """True for ``ibkr_fa.parser`` and any module under it."""
    return module == "ibkr_fa.parser" or module.startswith("ibkr_fa.parser.")


def _is_config(module: str) -> bool:
    """True for ``ibkr_fa.config`` and any name imported from it."""
    return module == "ibkr_fa.config" or module.startswith("ibkr_fa.config.")


def _is_output(module: str) -> bool:
    """True for ``ibkr_fa.output`` and any module under it."""
    return module == "ibkr_fa.output" or module.startswith("ibkr_fa.output.")


def _is_compute_or_output(module: str) -> bool:
    """True for ``ibkr_fa.compute``, ``ibkr_fa.output`` and any module under them."""
    return any(
        module == f"ibkr_fa.{layer}" or module.startswith(f"ibkr_fa.{layer}.")
        for layer in ("compute", "output")
    )


_FETCHER_MODULES = (
    "ibkr_fa.evidence_cache",
    "ibkr_fa.robinhood_identity_sources",
    "ibkr_fa.robinhood_legal_entity_sources",
    "ibkr_fa.robinhood_adr_entity_sources",
    "ibkr_fa.robinhood_fund_entity_sources",
    "ibkr_fa.prices.massive_options",
    "ibkr_fa.prices.yahoo",
    "ibkr_fa.prices.nasdaq",
    "ibkr_fa.prices.loader",
    "ibkr_fa.prices.cache",
    "urllib",
    "http.client",
)


def _is_fetcher(module: str) -> bool:
    """True for a network or cache module, or any name imported from one."""
    return any(module == banned or module.startswith(f"{banned}.") for banned in _FETCHER_MODULES)


def _is_form_1042s_parser(module: str) -> bool:
    """True for either issuer's Form 1042-S parser and any name imported from it."""
    return any(
        module == f"ibkr_fa.parser.{name}" or module.startswith(f"ibkr_fa.parser.{name}.")
        for name in ("fidelity_cma_1042s", "robinhood_1042s")
    )


def _upper_layer_imports(source: str, package: tuple[str, ...]) -> list[str]:
    """Return ``"<line>: <statement>"`` for each pipeline / CLI import in ``source``.

    ``package`` is the importing module's package, used to resolve
    relative imports.
    """
    return _matching_imports(source, package, _is_upper_layer)


def _matching_imports(
    source: str, package: tuple[str, ...], is_banned: Callable[[str], bool]
) -> list[str]:
    """Return ``"<line>: <statement>"`` for each import in ``source`` that ``is_banned``."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            parts = list(package[: len(package) + 1 - node.level]) if node.level else []
            if node.module:
                parts.append(node.module)
            module = ".".join(parts)
            targets = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        else:
            continue
        if any(is_banned(target) for target in targets):
            found.append(f"{node.lineno}: {ast.unparse(node)}")
    return found


def test_lower_layers_do_not_import_pipeline_or_cli() -> None:
    offenders = []
    for layer in _LOWER_LAYERS:
        layer_dir = _PACKAGE_ROOT / layer
        assert layer_dir.is_dir(), f"lower layer {layer!r} not found at {layer_dir}"
        paths = sorted(layer_dir.rglob("*.py"))
        assert paths, f"no Python modules found in lower layer {layer!r} at {layer_dir}"
        for path in paths:
            package = path.relative_to(_PACKAGE_ROOT.parent).with_suffix("").parts[:-1]
            offenders.extend(
                f"{path.relative_to(_PACKAGE_ROOT)}:{hit}"
                for hit in _upper_layer_imports(path.read_text(encoding="utf-8"), package)
            )
    assert offenders == []


def test_outputs_and_parser_free_compute_do_not_import_parser() -> None:
    output_paths = sorted((_PACKAGE_ROOT / "output").rglob("*.py"))
    assert output_paths, f"no Python modules found under {_PACKAGE_ROOT / 'output'}"
    compute_paths = [
        _PACKAGE_ROOT / "compute" / name
        for name in ("schedule_os.py", "ftc_eligibility.py", "source_coverage.py")
    ]
    for path in compute_paths:
        assert path.is_file(), f"{path} not found"
    offenders = []
    for path in [*output_paths, *compute_paths]:
        package = path.relative_to(_PACKAGE_ROOT.parent).with_suffix("").parts[:-1]
        offenders.extend(
            f"{path.relative_to(_PACKAGE_ROOT)}:{hit}"
            for hit in _matching_imports(path.read_text(encoding="utf-8"), package, _is_parser)
        )
    assert offenders == []


def test_normalize_does_not_import_compute_or_output() -> None:
    paths = sorted((_PACKAGE_ROOT / "normalize").rglob("*.py"))
    assert paths, f"no Python modules found under {_PACKAGE_ROOT / 'normalize'}"
    offenders = []
    for path in paths:
        package = path.relative_to(_PACKAGE_ROOT.parent).with_suffix("").parts[:-1]
        offenders.extend(
            f"{path.relative_to(_PACKAGE_ROOT)}:{hit}"
            for hit in _matching_imports(
                path.read_text(encoding="utf-8"), package, _is_compute_or_output
            )
        )
    assert offenders == []


def test_netbenefits_parser_and_normalizer_do_not_import_config_compute_or_output() -> None:
    paths = sorted(
        path
        for package in ("parser/netbenefits", "normalize/netbenefits")
        for path in (_PACKAGE_ROOT / package).rglob("*.py")
    )
    assert paths, "no NetBenefits parser or normalizer modules found"
    offenders = []
    for path in paths:
        package = path.relative_to(_PACKAGE_ROOT.parent).with_suffix("").parts[:-1]
        offenders.extend(
            f"{path.relative_to(_PACKAGE_ROOT)}:{hit}"
            for hit in _matching_imports(
                path.read_text(encoding="utf-8"),
                package,
                lambda module: _is_config(module) or _is_compute_or_output(module),
            )
        )
    assert offenders == []


def test_compute_does_not_import_fetchers() -> None:
    paths = sorted((_PACKAGE_ROOT / "compute").rglob("*.py"))
    assert paths, f"no Python modules found under {_PACKAGE_ROOT / 'compute'}"
    offenders = []
    for path in paths:
        package = path.relative_to(_PACKAGE_ROOT.parent).with_suffix("").parts[:-1]
        offenders.extend(
            f"{path.relative_to(_PACKAGE_ROOT)}:{hit}"
            for hit in _matching_imports(path.read_text(encoding="utf-8"), package, _is_fetcher)
        )
    assert offenders == []


def test_compute_does_not_import_output() -> None:
    paths = sorted((_PACKAGE_ROOT / "compute").rglob("*.py"))
    assert paths, f"no Python modules found under {_PACKAGE_ROOT / 'compute'}"
    offenders = []
    for path in paths:
        package = path.relative_to(_PACKAGE_ROOT.parent).with_suffix("").parts[:-1]
        offenders.extend(
            f"{path.relative_to(_PACKAGE_ROOT)}:{hit}"
            for hit in _matching_imports(path.read_text(encoding="utf-8"), package, _is_output)
        )
    assert offenders == []


def test_compute_takes_form_1042s_types_from_models() -> None:
    hits: dict[str, list[str]] = {}
    for path in sorted((_PACKAGE_ROOT / "compute").rglob("*.py")):
        package = path.relative_to(_PACKAGE_ROOT.parent).with_suffix("").parts[:-1]
        found = _matching_imports(path.read_text(encoding="utf-8"), package, _is_form_1042s_parser)
        if found:
            hits[str(path.relative_to(_PACKAGE_ROOT))] = [hit.split(": ", 1)[1] for hit in found]
    assert hits == {
        "compute/robinhood_income.py": [
            "from ibkr_fa.parser.robinhood_1042s import "
            "Robinhood1042SCopy, Robinhood1042SPacket, Robinhood1042SSummaryRow"
        ]
    }


def test_fa_a2_builders_and_models_do_not_import_config() -> None:
    paths = [
        _PACKAGE_ROOT / name
        for name in (
            "compute/fa_a2.py",
            "compute/netbenefits_fa_a2.py",
            "models/account_identity.py",
            "models/fa_a2_row.py",
        )
    ]
    offenders = []
    for path in paths:
        assert path.is_file(), f"{path} not found"
        package = path.relative_to(_PACKAGE_ROOT.parent).with_suffix("").parts[:-1]
        offenders.extend(
            f"{path.relative_to(_PACKAGE_ROOT)}:{hit}"
            for hit in _matching_imports(path.read_text(encoding="utf-8"), package, _is_config)
        )
    assert offenders == []


def test_guard_rejects_empty_layers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for layer in _LOWER_LAYERS:
        (tmp_path / layer).mkdir()
    monkeypatch.setitem(globals(), "_PACKAGE_ROOT", tmp_path)
    with pytest.raises(AssertionError, match="no Python modules found"):
        test_lower_layers_do_not_import_pipeline_or_cli()


def test_fa_a2_source_adapters_do_not_set_peak_directly() -> None:
    for name in ("compute/robinhood_fa.py", "pipeline_fidelity_cma_fa.py"):
        assert "peak_balance_inr=" not in (_PACKAGE_ROOT / name).read_text(encoding="utf-8")


def test_fa_account_cross_checks_are_not_literal_true() -> None:
    offenders = [
        str(path.relative_to(_PACKAGE_ROOT))
        for path in sorted(_PACKAGE_ROOT.rglob("*.py"))
        if "account_id_cross_checked=True" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_cbdt_writer_does_not_name_source_config_keys() -> None:
    source = (_PACKAGE_ROOT / "output/cbdt_json_fragment.py").read_text(encoding="utf-8")
    for literal in ("[[schedule_fa.", "[account.custodian]", "[[fa.entities]]"):
        assert literal not in source


@pytest.mark.parametrize(
    "source",
    [
        "import ibkr_fa.pipeline",
        "import ibkr_fa.pipeline_fa as fa",
        "import ibkr_fa.cli",
        "from ibkr_fa.pipeline import FinancialYear",
        "from ibkr_fa.pipeline_cg import run_schedule_cg",
        "from ibkr_fa import pipeline",
        "from ibkr_fa import models, pipeline_dispatch",
        "from ibkr_fa import cli",
        "from .. import pipeline",
        "from ..pipeline_fa import run_fa_a3",
        "if TYPE_CHECKING:\n    from ibkr_fa.pipeline import FinancialYear",
        "def f():\n    from ibkr_fa.cli import main",
    ],
)
def test_guard_flags_upper_layer_import(source: str) -> None:
    assert len(_upper_layer_imports(source, ("ibkr_fa", "compute"))) == 1


@pytest.mark.parametrize(
    "source",
    [
        "from ibkr_fa.models import FinancialYear",
        "from ibkr_fa import models",
        "import ibkr_fa.compute.pipeline_helpers",
        "from . import pipeline_helpers",
    ],
)
def test_guard_ignores_lower_layer_import(source: str) -> None:
    assert _upper_layer_imports(source, ("ibkr_fa", "compute")) == []


@pytest.mark.parametrize(
    "source",
    [
        "from ibkr_fa.parser.activity_statement._description import _DESCRIPTION_PATTERN",
        "import ibkr_fa.parser.robinhood",
        "from ibkr_fa import parser",
        "from ..parser import activity_statement",
    ],
)
def test_parser_guard_flags_parser_import(source: str) -> None:
    assert len(_matching_imports(source, ("ibkr_fa", "output"), _is_parser)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "from ibkr_fa.models import DistributionKind",
        "import ibkr_fa.parsers_extra",
        "from . import parser_helpers",
    ],
)
def test_parser_guard_ignores_other_imports(source: str) -> None:
    assert _matching_imports(source, ("ibkr_fa", "output"), _is_parser) == []


@pytest.mark.parametrize(
    "source",
    [
        "from ibkr_fa.config import TaxpayerStatus",
        "import ibkr_fa.config",
        "from ibkr_fa import config",
        "from .. import config",
        "if TYPE_CHECKING:\n    from ibkr_fa.config import NetBenefitsAccountConfig",
    ],
)
def test_config_guard_flags_config_import(source: str) -> None:
    assert len(_matching_imports(source, ("ibkr_fa", "compute"), _is_config)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "from ibkr_fa.models.account_identity import TaxpayerStatus",
        "import ibkr_fa.configs_extra",
        "from . import config_helpers",
    ],
)
def test_config_guard_ignores_other_imports(source: str) -> None:
    assert _matching_imports(source, ("ibkr_fa", "compute"), _is_config) == []


@pytest.mark.parametrize(
    "source",
    [
        "from ibkr_fa.compute.robinhood_lots import combined_disposal",
        "import ibkr_fa.output.base",
        "from ibkr_fa import compute",
        "from ... import output",
        "if TYPE_CHECKING:\n    from ibkr_fa.compute import CyOverlap",
        "def f():\n    from ibkr_fa.output import get_writer",
    ],
)
def test_normalize_guard_flags_compute_or_output_import(source: str) -> None:
    assert (
        len(_matching_imports(source, ("ibkr_fa", "normalize", "robinhood"), _is_compute_or_output))
        == 1
    )


@pytest.mark.parametrize(
    "source",
    [
        "from ibkr_fa.parser.robinhood import RobinhoodEvent",
        "from ibkr_fa.config import RobinhoodConfig",
        "import ibkr_fa.computed_extra",
        "from . import compute_helpers",
    ],
)
def test_normalize_guard_ignores_other_imports(source: str) -> None:
    assert (
        _matching_imports(source, ("ibkr_fa", "normalize", "robinhood"), _is_compute_or_output)
        == []
    )


@pytest.mark.parametrize(
    "source",
    [
        "from ibkr_fa.robinhood_identity_sources import IdentitySourceBundle",
        "import ibkr_fa.robinhood_legal_entity_sources",
        "from ibkr_fa import robinhood_adr_entity_sources",
        "from .. import robinhood_fund_entity_sources",
        "from ibkr_fa.prices.massive_options import MassiveOptionCloseSeries",
        "from ibkr_fa.prices import yahoo",
        "from ..prices.cache import read_cache",
        "import urllib.request",
        "from urllib.parse import urlsplit",
        "from http import client",
        "if TYPE_CHECKING:\n    from ibkr_fa.prices.loader import load_price_series",
        "def f():\n    from ibkr_fa.prices.nasdaq import fetch",
    ],
)
def test_fetcher_guard_flags_fetcher_import(source: str) -> None:
    assert len(_matching_imports(source, ("ibkr_fa", "compute"), _is_fetcher)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "from ibkr_fa.prices.series import MassiveOptionCloseSeries, PriceSeries",
        "from ibkr_fa.models.reference_identity import IdentitySourceBundle",
        "from http import HTTPStatus",
        "import ibkr_fa.robinhood_identity_sources_extra",
        "from . import robinhood_entity_overrides",
    ],
)
def test_fetcher_guard_ignores_other_imports(source: str) -> None:
    assert _matching_imports(source, ("ibkr_fa", "compute"), _is_fetcher) == []


@pytest.mark.parametrize(
    "source",
    [
        "from ibkr_fa.output.fa_manual_fill import fa_a2_manual_fill_columns",
        "import ibkr_fa.output",
        "from ibkr_fa import output",
        "from .. import output",
        "if TYPE_CHECKING:\n    from ibkr_fa.output.base import FaWriter",
        "def f():\n    from ..output import get_writer",
    ],
)
def test_output_guard_flags_output_import(source: str) -> None:
    assert len(_matching_imports(source, ("ibkr_fa", "compute"), _is_output)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "from ibkr_fa.models.fa_a2_row import FaA2Row",
        "import ibkr_fa.outputs_extra",
        "from . import output_helpers",
    ],
)
def test_output_guard_ignores_other_imports(source: str) -> None:
    assert _matching_imports(source, ("ibkr_fa", "compute"), _is_output) == []


def _is_output_or_robinhood(module: str) -> bool:
    """True for ``ibkr_fa.output`` (or under it) and any module path naming Robinhood."""
    return _is_output(module) or any(part.startswith("robinhood") for part in module.split("."))


def test_schedule_cg_filing_result_is_broker_neutral() -> None:
    """#642: the shared filing result imports no writer and no Robinhood module."""
    path = _PACKAGE_ROOT / "compute" / "schedule_cg_filing.py"
    package = path.relative_to(_PACKAGE_ROOT.parent).with_suffix("").parts[:-1]

    assert (
        _matching_imports(path.read_text(encoding="utf-8"), package, _is_output_or_robinhood) == []
    )
