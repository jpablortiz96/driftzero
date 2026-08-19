"""M0 purity guard — no cloud or LLM dependency in the deterministic packages.

This is the M0-A slice of the guard that T060 will assert as a hard gate: the
deterministic packages must import cleanly with zero Google Cloud, ADK, or model
client dependencies.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

DETERMINISTIC_PACKAGES = ("driftzero", "driftzero.models", "driftzero.truth_engine")

FORBIDDEN_PREFIXES = (
    "google",
    "google_cloud",
    "googleapiclient",
    "vertexai",
    "openai",
    "anthropic",
    "langchain",
    "requests",
    "httpx",
    "urllib3",
    "aiohttp",
)


def _source_files() -> list[Path]:
    root = Path(__file__).resolve().parents[3] / "src" / "driftzero"
    files = sorted(root.rglob("*.py"))
    assert files, f"no source files found under {root}"
    return files


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
def test_no_forbidden_imports_in_source(path: Path) -> None:
    offending = sorted(r for r in _imported_roots(path) if r in FORBIDDEN_PREFIXES)
    assert not offending, f"{path} imports forbidden dependency: {offending}"


def test_deterministic_packages_import_without_cloud_modules_loaded() -> None:
    for name in DETERMINISTIC_PACKAGES:
        importlib.import_module(name)
    pkg = importlib.import_module("driftzero.models")
    for mod in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"driftzero.models.{mod.name}")

    loaded = {m.split(".")[0] for m in sys.modules}
    leaked = sorted(loaded & set(FORBIDDEN_PREFIXES))
    assert not leaked, f"forbidden modules loaded after importing deterministic packages: {leaked}"
