"""DRIFTZERO public judge surface.

A presentation client, and nothing more. It renders the product story from recorded
evidence and reads one allow-listed health path from the private backend using the
service identity Cloud Run attaches to it.

It holds no domain logic. The Truth Engine, the trust boundaries and the proof generator
live behind the private API and are not importable from here — a public surface that
could adjudicate anything would defeat the point of keeping the backend private.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
