"""Google Agent Development Kit runtime for DRIFTZERO's semantic layer.

Deliberately **outside** ``src/driftzero``. The deterministic core's purity guard asserts
its whole third-party surface is pydantic, so the ADK, the GenAI SDK, and credential
handling live here and are wired in at the composition root — the same arrangement as
``driftzero_providers`` (Vertex MaaS) and ``driftzero_console`` (FastAPI).
"""
