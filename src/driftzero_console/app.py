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

from driftzero_console.schemas import EvidenceDocument, FrontlineView, HeroState
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


@app.post("/api/hero/session", response_model=HeroState)
def new_session() -> HeroState:
    """Begin a new change session.

    Never claims a dispatched action was undone — a new session gets a new ledger, a new
    repository, and a new action identity.
    """
    return HeroState(**_service.start_new_session())


@app.post("/api/hero/reset", response_model=HeroState, include_in_schema=False)
def reset() -> HeroState:
    """Retained alias for the session endpoint."""
    return HeroState(**_service.start_new_session())


@app.post("/api/hero/deploy", response_model=HeroState)
def deploy() -> HeroState:
    """Execute the real path: agent → policy → mutation tool → Crossing 2."""
    return HeroState(**_service.deploy_change())


@app.post("/api/hero/security-test", response_model=HeroState)
def security_test() -> HeroState:
    """Drive a genuinely unauthorized identity at the real authorization seam."""
    return HeroState(**_service.run_security_test())


@app.get("/api/hero/frontline/{change_id}", response_model=FrontlineView)
def read_frontline(change_id: str) -> FrontlineView:
    """The operational delta for one change, as a worker sees it."""
    payload = _service.get_frontline(change_id)
    if payload is None:
        raise HTTPException(
            status_code=404, detail=f"no composed delta for change {change_id!r}"
        )
    return FrontlineView(**payload)


@app.post("/api/hero/frontline/{change_id}/acknowledge", response_model=FrontlineView)
def acknowledge(change_id: str) -> FrontlineView:
    """Record that the operator read the delta.

    An application event only: it does not establish delivery, is never PASS, and
    produces no Change Proof.
    """
    payload = _service.acknowledge(change_id)
    if payload is None:
        raise HTTPException(
            status_code=404, detail=f"no composed delta for change {change_id!r}"
        )
    return FrontlineView(**payload)


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


@app.get("/frontline/{change_id}", include_in_schema=False)
def frontline_page(change_id: str) -> FileResponse:
    """Worker-oriented, phone-friendly view of one change."""
    return FileResponse(STATIC_DIR / "frontline.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:  # pragma: no cover - exercised by running the console
    import uvicorn

    print("DRIFTZERO Hero Console")
    print(f"http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    main()
