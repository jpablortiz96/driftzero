"""Test isolation for the M1-A semantic layer."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _no_registered_client() -> Iterator[None]:
    """Every test starts and ends with no globally registered model provider."""
    from driftzero.agents import model_client

    model_client.clear_model_client_provider()
    yield
    model_client.clear_model_client_provider()
