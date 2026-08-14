"""Repository integrity.

These tests exist because an unanchored `.gitignore` rule (`data/`) silently
excluded `libs/core/src/atp_core/data/` — the entire market-data layer — from
the repository. Everything worked locally, where the files were on disk, and
only CI saw the gap, because CI only ever sees what was committed.

A missing package is invisible to every other test in this suite: nothing else
imports the data ports yet, so nothing else fails. That is exactly the kind of
hole worth an explicit check.
"""

from __future__ import annotations

import importlib

import pytest

#: Every subpackage the platform is built from. Adding one here is deliberate —
#: it is the list that says "this must exist in a fresh checkout".
CORE_SUBPACKAGES = [
    "analytics",
    "backtest",
    "brokers",
    "data",
    "domain",
    "execution",
    "indicators",
    "persistence",
    "risk",
    "strategy",
]

#: Modules that define a port. If one of these vanishes, an adapter silently
#: loses its contract and the dependency rule stops being enforceable.
PORT_MODULES = [
    "atp_core.brokers.ports",
    "atp_core.data.ports",
]


@pytest.mark.parametrize("name", CORE_SUBPACKAGES)
def test_core_subpackage_is_importable(name: str) -> None:
    """Fails loudly in a fresh checkout if a package was never committed."""
    importlib.import_module(f"atp_core.{name}")


@pytest.mark.parametrize("module", PORT_MODULES)
def test_port_module_is_importable(module: str) -> None:
    importlib.import_module(module)


def test_core_package_ships_py_typed() -> None:
    """PEP 561 marker.

    Without it, `atp_core`'s inline annotations are invisible to consumers and
    mypy reports every cross-package import as untyped — which is precisely
    what CI hit once the package was actually present.
    """
    import atp_core

    package_dir = importlib.resources.files(atp_core)
    assert (package_dir / "py.typed").is_file(), (
        "atp_core must ship py.typed, or its types are invisible to apps/*"
    )
