"""Test-only network isolation, scoped to the deterministic M0 suite.

M0 must run entirely offline, so outbound socket use is blocked for every test under
``tests/unit/truth_engine/`` and the T060 gate fails loudly if production code ever
reaches for the network.

**Scope is deliberate.** This conftest lives in the Truth Engine test directory rather
than at ``tests/``, because pytest applies conftest fixtures downward: a repository-root
placement would also block future M1/M2 integration tests that legitimately need Gemini,
ADK, Cloud Run, Firestore, or Pub/Sub. The M0 prohibition applies to the deterministic
core, not to the whole future test tree.

Deliberately test-only: nothing here is imported by ``src/driftzero``, and no networking
abstraction is introduced into production code.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest


class NetworkAccessBlocked(RuntimeError):
    """Raised when code under test attempts outbound network access."""


def _blocked(*args: Any, **kwargs: Any) -> Any:
    raise NetworkAccessBlocked(
        "outbound network access is disabled for the deterministic M0 suite"
    )


@pytest.fixture(autouse=True)
def block_network() -> Iterator[None]:
    """Disable outbound connections and DNS resolution around each M0 test.

    Local socket construction stays available so pytest internals keep working; only
    the operations that would actually leave the machine are blocked.

    Function-scoped on purpose. ``socket`` is a process-global module, so a
    session-scoped patch would stay installed for the rest of the run and would still
    reach an integration suite executed in the same pytest invocation — defeating the
    directory scoping. Patching and restoring around each test confines the block to
    this subtree in every execution order.
    """
    originals = {
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
        "create_connection": socket.create_connection,
        "getaddrinfo": socket.getaddrinfo,
    }

    socket.socket.connect = _blocked  # type: ignore[method-assign]
    socket.socket.connect_ex = _blocked  # type: ignore[method-assign]
    socket.create_connection = _blocked  # type: ignore[assignment]
    socket.getaddrinfo = _blocked  # type: ignore[assignment]

    try:
        yield
    finally:
        socket.socket.connect = originals["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = originals["connect_ex"]  # type: ignore[method-assign]
        socket.create_connection = originals["create_connection"]  # type: ignore[assignment]
        socket.getaddrinfo = originals["getaddrinfo"]  # type: ignore[assignment]
