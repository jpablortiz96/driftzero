"""DRIFTZERO — deterministic change-deployment core.

This package root and the ``models`` / ``truth_engine`` subpackages are the
deterministic layer (M0). They MUST NOT import Google Cloud SDKs, ADK, or any
model client. See specs/001-hero-change-deployment/plan.md.
"""

__version__ = "0.1.0"
