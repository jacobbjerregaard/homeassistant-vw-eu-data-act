"""Test bootstrap for the VW Group EU Data Act integration.

The integration package imports Home Assistant at import time, so it cannot be
loaded in a plain unit-test environment. The portal client under ``api`` is
however free of Home Assistant, and is the part most worth testing.

To exercise it in isolation the ``api`` directory is registered as a standalone
namespace package named ``euda_api``, so tests can do::

    from euda_api.dataset import Dataset

without pulling in Home Assistant.
"""

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_API_DIR = _REPO_ROOT / "custom_components" / "vwg_eu_data_act" / "api"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Make ``custom_components`` importable for the Home Assistant tests.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _register_namespace_package(name: str, path: Path) -> None:
    """Expose ``path`` as an importable namespace package called ``name``."""
    if name in sys.modules:
        return
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [str(path)]
    sys.modules[name] = importlib.util.module_from_spec(spec)


_register_namespace_package("euda_api", _API_DIR)


def load_fixture(name: str) -> dict:
    """Return a JSON fixture from tests/fixtures."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# tests/integration needs Home Assistant + pytest-homeassistant-custom-component.
# When those aren't installed (the pure-logic venv) skip collecting that
# directory so the rest of the suite still runs.
_HAS_HOMEASSISTANT = (
    importlib.util.find_spec("pytest_homeassistant_custom_component") is not None
)


def pytest_ignore_collect(collection_path, config):
    """Skip tests/integration when Home Assistant isn't installed."""
    if _HAS_HOMEASSISTANT:
        return None
    if "integration" in collection_path.parts:
        return True
    return None
