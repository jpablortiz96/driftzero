"""FastAPI application for the DRIFTZERO Hero Console.

Endpoints are narrow and parameterless by design. There is no request body anywhere in
this API: the browser cannot choose an identity, a tool, an action id, a filesystem
path, or a patch. It can ask for the current state, run the canonical scenario, run the
security probe, reset the session, or read one evidence document by id — nothing else.

That is the whole trust boundary. A frontend that cannot express a privileged request
cannot make one.

Run locally::

    python -m driftzero_console.app
    uvicorn driftzero_console.app:app --reload --port 8080
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from driftzero_console.schemas import EvidenceDocument, HeroState
from driftzero_console.service import HeroConsoleService

STATIC_DIR = Path(__file__).parent / "static"
HOST = "127.0.0.1"
PORT = 8080

app = FastAPI(
    title="DRIFTZERO Hero Console",
    version="0.1.0",
    description="Local interactive surface over the real remediation and security path.",
)

_service = HeroConsoleService()


def get_service() -> HeroConsoleService:
    """Accessor so tests can drive the same instance the API uses."""
    return _service


@app.get("/api/hero/state", response_model=HeroState)
def read_state() -> HeroState:
    """Current demo state, projected from real application data."""
    return HeroState(**_service.get_state())


@app.post("/api/hero/reset", response_model=HeroState)
def reset() -> HeroState:
    """Start a fresh session.

    Never claims a dispatched action was undone — a new session gets a new ledger, a new
    repository, and a new action identity.
    """
    return HeroState(**_service.reset_demo())


@app.post("/api/hero/deploy", response_model=HeroState)
def deploy() -> HeroState:
    """Execute the real path: agent → policy → mutation tool → Crossing 2."""
    return HeroState(**_service.deploy_change())


@app.post("/api/hero/security-test", response_model=HeroState)
def security_test() -> HeroState:
    """Drive a genuinely unauthorized identity at the real authorization seam."""
    return HeroState(**_service.run_security_test())


@app.get("/api/hero/evidence/{evidence_id}", response_model=EvidenceDocument)
def read_evidence(evidence_id: str) -> EvidenceDocument:
    """Return one evidence document for inspection."""
    document = _service.get_evidence(evidence_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"no evidence recorded for {evidence_id!r}")
    return EvidenceDocument(evidence_id=evidence_id, document=document)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:  # pragma: no cover - exercised by running the console
    import uvicorn

    print("DRIFTZERO Hero Console")
    print(f"http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    main()
