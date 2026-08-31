"""The production ASGI application — the T096 Cloud Run entrypoint.

Deliberately small. It composes a runtime from configuration, mounts the T094 routes and
the T095 push handler, and does nothing else. T096 needs only to build a container whose
command runs ``uvicorn driftzero_api.app:app``; no code has to move.

This is a separate application from ``driftzero_console.app``, which is the Mission
Control demo console. They share the same service layer underneath, so there is one
implementation of the flow and two transports over it — not two flows.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from driftzero_api import pubsub, routes, web
from driftzero_api.runtime import ApiRuntime, build_runtime
from driftzero_providers import composition

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURES = REPO_ROOT / "fixtures"

API_TITLE = "DRIFTZERO API"
API_DESCRIPTION = (
    "Approved-change ingestion, workflow status, field verification, and Change Proof "
    "retrieval. Every authoritative decision belongs to the Truth Engine; this surface "
    "only transports requests."
)


def create_app(runtime: ApiRuntime | None = None) -> FastAPI:
    """Build the application, optionally over an injected runtime.

    Injection is how tests compose a runtime with in-memory doubles. Left to itself the
    app builds from configuration, which defaults to ``memory`` — so importing or
    starting this module never reaches Google Cloud on its own.
    """
    app = FastAPI(title=API_TITLE, description=API_DESCRIPTION, version="1.0.0")

    # Composition root. Without it a deployed instance reads DRIFTZERO_SEMANTIC_PROVIDER
    # and DRIFTZERO_FIELD_PROVIDER, finds no registered client, and silently degrades to
    # "no analysis was performed" — configured for live models and never calling one.
    # Neither call raises, and neither reaches a model: registration is not invocation.
    app.state.provider_status = {
        "semantic": composition.configure_semantic_provider(),
        "field": composition.configure_field_provider(),
    }
    app.state.runtime = runtime or build_runtime(
        fixtures_dir=Path(os.environ.get("DRIFTZERO_FIXTURES_DIR", DEFAULT_FIXTURES))
    )
    app.include_router(routes.router)
    app.include_router(pubsub.router)
    # The M6 product surface. Behind the same IAM boundary as everything else.
    app.include_router(web.router)
    return app


app = create_app()
