"""M0 purity guard — no cloud, model, or network dependency in the deterministic core.

Enforced by the T060 hard exit gate. Both deterministic packages are protected:

* ``src/driftzero/models/``
* ``src/driftzero/truth_engine/``

Two complementary checks. The static one reads every source file's import statements;
the runtime one imports every module in both packages and inspects ``sys.modules``, so a
*transitively reachable* dependency is caught even if no file names it directly.

Matching is on the exact top-level module name, so harmless stdlib modules are never
rejected for merely containing a generic word (``hashlib``, ``json``, ``datetime``,
``dataclasses`` and friends all pass).
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

DETERMINISTIC_PACKAGES = ("driftzero", "driftzero.models", "driftzero.truth_engine")
GUARDED_SUBPACKAGES = ("driftzero.models", "driftzero.truth_engine")

FORBIDDEN_ROOTS = frozenset(
    {
        # Google Cloud, ADK, GenAI, Vertex — all live under the ``google`` root.
        "google",
        "google_cloud",
        "googleapiclient",
        "google_auth",
        "vertexai",
        # Cloud service clients that ship their own top-level names.
        "firestore",
        "pubsub",
        "gcloud",
        "boto3",
        "botocore",
        # Model / agent clients.
        "openai",
        "anthropic",
        "langchain",
        "llama_index",
        "transformers",
        "ollama",
        # Network clients.
        "requests",
        "httpx",
        "aiohttp",
        "urllib3",
        "grpc",
        "websockets",
    }
)
"""Exact top-level module names that must never be reachable from the M0 core."""


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


def _import_every_module() -> None:
    for name in DETERMINISTIC_PACKAGES:
        importlib.import_module(name)
    for package_name in GUARDED_SUBPACKAGES:
        package = importlib.import_module(package_name)
        for module in pkgutil.iter_modules(package.__path__):
            importlib.import_module(f"{package_name}.{module.name}")


# ============================ static: declared imports ================================


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
def test_no_forbidden_imports_in_source(path: Path) -> None:
    offending = sorted(_imported_roots(path) & FORBIDDEN_ROOTS)
    assert not offending, f"{path} imports forbidden dependency: {offending}"


def test_both_deterministic_packages_are_covered_by_the_static_scan() -> None:
    """The scan must actually reach models/ and truth_engine/, not just the root."""
    scanned = {str(p) for p in _source_files()}
    for package in ("models", "truth_engine"):
        assert any(
            f"driftzero{s}{package}{s}" in path
            for path in scanned
            for s in ("/", "\\")
        ), f"static scan does not cover src/driftzero/{package}/"


def test_no_model_or_agent_sdk_named_anywhere_in_the_core() -> None:
    """Belt-and-braces: no Gemini/Gemma/ADK/Vertex reference in any core source file."""
    banned_text = ("google.adk", "google.genai", "google.cloud", "vertexai")
    for path in _source_files():
        source = path.read_text(encoding="utf-8")
        code_lines = [
            line for line in source.splitlines() if not line.strip().startswith(("#", '"', "'"))
        ]
        joined = "\n".join(code_lines)
        for banned in banned_text:
            assert banned not in joined, f"{path} references {banned}"


# ============================ runtime: reachable dependencies =========================


def test_importing_both_packages_loads_no_forbidden_module() -> None:
    """Catches transitive reachability, which the static scan alone cannot see."""
    _import_every_module()
    loaded = {name.split(".")[0] for name in sys.modules}
    leaked = sorted(loaded & FORBIDDEN_ROOTS)
    assert not leaked, f"forbidden modules reachable from the deterministic core: {leaked}"


def test_runtime_scan_visits_every_module_in_both_packages() -> None:
    _import_every_module()
    for package_name in GUARDED_SUBPACKAGES:
        package = importlib.import_module(package_name)
        for module in pkgutil.iter_modules(package.__path__):
            assert f"{package_name}.{module.name}" in sys.modules


def test_core_declares_only_pydantic_as_a_third_party_dependency() -> None:
    """The deterministic core's entire third-party surface is pydantic."""
    stdlib = set(sys.stdlib_module_names)
    third_party: set[str] = set()
    for path in _source_files():
        for root in _imported_roots(path):
            if root not in stdlib and root != "driftzero":
                third_party.add(root)
    assert third_party <= {"pydantic"}, f"unexpected third-party dependency: {sorted(third_party)}"


# ============================ network isolation =======================================


def test_outbound_network_access_is_blocked_during_the_suite() -> None:
    """Proves the block from this subtree's conftest.py is active.

    Scoped to ``tests/unit/truth_engine/`` so future integration suites that need real
    services are not silently blocked by it.
    """
    import socket

    from .conftest import NetworkAccessBlocked

    with pytest.raises(NetworkAccessBlocked):
        socket.create_connection(("example.com", 80), timeout=1)
    with pytest.raises(NetworkAccessBlocked):
        socket.getaddrinfo("example.com", 80)
    with pytest.raises(NetworkAccessBlocked):
        socket.socket().connect(("example.com", 80))


def test_deterministic_core_completes_a_full_proof_with_network_blocked() -> None:
    """End-to-end proof generation runs with sockets disabled — nothing reaches out."""
    from driftzero.truth_engine.proof_generator import generate_change_proof

    from ._acceptance import make_proof_context

    proof = generate_change_proof(make_proof_context())
    assert proof.content_hash
