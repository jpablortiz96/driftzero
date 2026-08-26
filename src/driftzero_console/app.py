"""FastAPI application for the DRIFTZERO Hero Console.

Endpoints are narrow and parameterless by design. The browser cannot choose an identity,
a tool, a model, a prompt, an action id, a filesystem path, or a patch. It can ask for
the current state, run the canonical scenario, deliver, run the security probe, reset
the session, submit a field image, or read one evidence document by id — nothing else.

The two upload routes are the only ones that accept a body at all, and that body is
**the image bytes themselves** — not a form, not JSON, not an envelope with fields. A
request with no structure has nowhere to hide a privileged parameter.

That is the whole trust boundary. A frontend that cannot express a privileged request
cannot make one.

Run locally::

    python -m driftzero_console.app
    uvicorn driftzero_console.app:app --reload --port 8080
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from driftzero.config import DriftZeroConfig
from driftzero.field.evidence import MAX_IMAGE_BYTES
from driftzero_console.schemas import EvidenceDocument, FrontlineView, HeroState
from driftzero_console.service import HeroConsoleService

STATIC_DIR = Path(__file__).parent / "static"
HOST = "127.0.0.1"
PORT = 8080

UPLOAD_BODY_LIMIT = MAX_IMAGE_BYTES + 1
"""Read at most one byte past the limit — enough to detect an overrun, never to buffer
an unbounded upload."""

app = FastAPI(
    title="DRIFTZERO Hero Console",
    version="0.1.0",
    description="Local interactive surface over the real remediation and security path.",
)

_service = HeroConsoleService()


def get_service() -> HeroConsoleService:
    """Accessor so tests can drive the same instance the API uses."""
    return _service


def configure_providers() -> str:
    """Composition root: install the live field provider, if one is configured.

    The concrete provider is imported **only** when ``DRIFTZERO_FIELD_PROVIDER`` selects
    it, so an unconfigured instance never needs ``google-auth`` or ``httpx`` installed —
    and the deterministic core never imports them at all.

    Returns a short status line for the startup banner. Never raises: a missing live
    dependency degrades to "no observation is possible", which the UI states plainly,
    rather than to a fabricated observation.

    Kept ASCII-only — this goes to a terminal, and a Windows console encodes cp1252.
    """
    config = DriftZeroConfig.from_env().field_provider
    if not config.is_live:
        return f"field provider: {config.provider} (no live model call is possible)"
    missing = config.missing_settings()
    if missing:
        return "field provider: MISCONFIGURED - missing " + ", ".join(missing)
    try:
        from driftzero_providers.vertex_maas import install  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        return (
            f"field provider: UNAVAILABLE - {exc}. Install the live extra: "
            'pip install -e ".[live]"'
        )
    install()
    return f"field provider: {config.provider} -> {config.model}"


def configure_semantic_provider() -> str:
    """Composition root: install the real Google ADK runtime, if configured.

    The ADK is imported **only** when ``DRIFTZERO_SEMANTIC_PROVIDER`` selects it, so an
    unconfigured instance never needs ``google-adk`` installed — and the deterministic
    core never imports it at all.

    ASCII-only and never raises: a missing dependency degrades to "no analysis is
    possible", which the UI states plainly, rather than to a fabricated proposal.
    """
    config = DriftZeroConfig.from_env().semantic_provider
    if not config.is_live:
        return f"semantic provider: {config.provider} (no live model call is possible)"
    missing = config.missing_settings()
    if missing:
        return "semantic provider: MISCONFIGURED - missing " + ", ".join(missing)
    try:
        from driftzero_adk.install import install_change_intelligence  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        return (
            f"semantic provider: UNAVAILABLE - {exc}. Install the live extra: "
            'pip install -e ".[live]"'
        )
    version = install_change_intelligence(config)
    return (
        f"semantic provider: google_adk (ADK {version}) -> {config.model} "
        f"@ {config.location}"
    )


configure_providers()
configure_semantic_provider()


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


@app.post("/api/hero/analyze", response_model=HeroState)
def analyze() -> HeroState:
    """Run impact analysis: Change Intelligence → Crossing 1 → deterministic gate.

    The server derives the source material, the prompt, the model, and the agent
    identity. The browser supplies nothing — it cannot name a candidate, a target, or a
    ChangeSet, because this endpoint accepts no input at all.
    """
    return HeroState(**_service.analyze_change())


@app.post("/api/hero/deploy", response_model=HeroState)
def deploy() -> HeroState:
    """Execute the real path: agent → policy → mutation tool → Crossing 2.

    Refused server-side unless impact analysis produced exactly one qualified target.
    """
    return HeroState(**_service.deploy_change())


@app.post("/api/hero/deliver", response_model=HeroState)
def deliver() -> HeroState:
    """Deliver the composed delta through the pilot channel and validate at Crossing 3.

    Idempotent on the stable delivery action identity; the server derives the payload
    and destination from authoritative state, so the browser supplies neither.
    """
    return HeroState(**_service.deliver_to_frontline())


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


async def _read_image_body(request: Request) -> bytes:
    """Read the raw image bytes from the request body, bounded.

    The body **is** the image. There is no multipart form, no JSON envelope, and no
    field the browser could smuggle a model id, a prompt, an identity, a filesystem
    path, or an expected answer through — the request has no structure to hide them in.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > UPLOAD_BODY_LIMIT:
            raise HTTPException(status_code=413, detail="field evidence is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _declared_claims(request: Request) -> dict[str, str | None]:
    """What the client claimed about its upload. Recorded, never trusted.

    Both values are echoed into evidence purely so an auditor can see the claim next to
    the truth. The authoritative MIME type is sniffed from the bytes.
    """
    filename = request.headers.get("x-filename")
    return {
        "declared_filename": filename[:255] if filename else None,
        "declared_content_type": request.headers.get("content-type"),
    }


@app.post("/api/hero/field-evidence", response_model=HeroState)
async def submit_field_evidence(request: Request) -> HeroState:
    """Submit a physical field image for observation (Mission Control surface).

    The request body is the image itself. Everything else — prompt, model, agent
    identity, capability, submission identity, MIME type — is derived server-side.
    """
    raw = await _read_image_body(request)
    return HeroState(**_service.submit_field_evidence(raw, **_declared_claims(request)))


@app.post(
    "/api/hero/frontline/{change_id}/field-evidence", response_model=FrontlineView
)
async def submit_frontline_field_evidence(
    change_id: str, request: Request
) -> FrontlineView:
    """Submit a physical field image from the worker surface.

    Delegates to the **same** service use case as Mission Control. The only difference
    is the projection returned, and that the worker route stays closed until delivery
    has been established.
    """
    if _service.get_frontline(change_id) is None:
        raise HTTPException(
            status_code=404, detail=f"no delivered delta for change {change_id!r}"
        )
    raw = await _read_image_body(request)
    _service.submit_field_evidence(raw, **_declared_claims(request))
    payload = _service.get_frontline(change_id)
    if payload is None:  # pragma: no cover - delivery cannot be undone mid-request
        raise HTTPException(status_code=404, detail=f"change {change_id!r} is not open")
    return FrontlineView(**payload)


@app.post("/api/hero/proof", response_model=HeroState)
def generate_proof() -> HeroState:
    """Step 11 — generate the Change Proof if the frozen seven invariants all hold.

    Accepts no input. The browser cannot supply a proof id, a hash, a verdict, or a
    workflow state, because there is nothing in the request to carry them.
    """
    return HeroState(**_service.generate_proof())


@app.get("/api/hero/proof")
def read_proof() -> dict:
    """The stored canonical proof, including the exact bytes its hash covers."""
    document = _service.get_proof_document()
    if document is None:
        raise HTTPException(status_code=404, detail="no Change Proof has been generated")
    return document


@app.get("/api/hero/proof/download")
def download_proof() -> Response:
    """Download the canonical proof bytes verbatim.

    Serves ``canonical_json`` rather than re-serialising the model, so the downloaded
    file is byte-for-byte what the SHA-256 was computed over.
    """
    document = _service.get_proof_document()
    if document is None:
        raise HTTPException(status_code=404, detail="no Change Proof has been generated")
    filename = f"{document['document']['proof_id']}.json"
    return Response(
        content=document["canonical_json"],
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Proof-Content-Hash": document["content_hash"],
        },
    )


@app.get("/api/hero/proof/replay")
def replay_proof_audit() -> dict:
    """Replay the recorded chronology. Executes no side effect of any kind."""
    audit = _service.get_replay_audit()
    if audit is None:
        raise HTTPException(status_code=404, detail="no Change Proof has been generated")
    return audit


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
    # Re-run at startup so `python -m driftzero_console.app` reports what is actually
    # wired, and an operator sees a misconfiguration before uploading a photo.
    print(configure_providers())
    print(configure_semantic_provider())
    print("runtime readiness: LOCAL_PILOT (not production-ready)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    main()
