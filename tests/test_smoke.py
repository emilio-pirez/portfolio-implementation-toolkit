import importlib

import pit

SUBPACKAGES = [
    "db",
    "data",
    "portfolio",
    "equitization",
    "orders",
    "compliance",
    "risk",
    "workflow",
]


def test_version_is_semver() -> None:
    parts = pit.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_subpackages_import() -> None:
    for name in SUBPACKAGES:
        module = importlib.import_module(f"pit.{name}")
        assert module.__doc__
