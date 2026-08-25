"""Concrete external providers for DRIFTZERO.

Deliberately **outside** ``src/driftzero``. That package's purity guard asserts its whole
third-party surface is pydantic, so anything that imports a cloud SDK, an HTTP client, or
a credential library lives here and is wired in at the composition root.

The separation is not cosmetic: it is what keeps the deterministic core installable and
testable with no credentials, no network, and no Google dependency present at all.
"""
