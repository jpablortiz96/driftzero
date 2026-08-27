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

import base64
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from driftzero.config import DriftZeroConfig
from driftzero.field.evidence import MAX_IMAGE_BYTES
from driftzero.proof.store import HASH_PREIMAGE_LABEL
from driftzero_adk.hero_workflow import HeroWorkflowRun
from driftzero_console.schemas import EvidenceDocument, FrontlineView, HeroState
from driftzero_console.service import HeroConsoleService
from driftzero_console.workflows import (
    REGISTRY_NOTE,
    FixtureRejected,
    UnknownWorkflow,
    WorkflowRegistry,
    dataset_from_fixture,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
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
    """Download the stored canonical proof bytes verbatim.

    Serves the stored ``canonical_json`` rather than re-serialising the model, so the
    file is exactly what this deployment recorded — the **complete** proof, its own
    ``content_hash`` included.

    That means ``sha256`` of this file deliberately does **not** equal ``content_hash``:
    the digest is taken over the proof *without* that field. ``X-Proof-Hash-Preimage``
    names the preimage so the difference is discoverable from the response alone.
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
            "X-Proof-Hash-Preimage": HASH_PREIMAGE_LABEL,
        },
    )


@app.get("/api/hero/proof/replay")
def replay_proof_audit() -> dict:
    """Replay the recorded chronology. Executes no side effect of any kind."""
    audit = _service.get_replay_audit()
    if audit is None:
        raise HTTPException(status_code=404, detail="no Change Proof has been generated")
    return audit


# ============================ T081 — CLI adapter ======================================
#
# Transport only. Every consequential step below delegates to the same T080 orchestration
# and application use cases the browser drives; no business truth lives in these routes.
# They exist because the CLI runs as separate OS processes and needs one long-lived
# runtime to talk to.

_registry = WorkflowRegistry()


def get_registry() -> WorkflowRegistry:
    """Accessor so tests can inspect or reset what this process is holding."""
    return _registry


@app.post("/api/cli/workflows")
async def cli_inject_change(request: Request) -> dict:
    """Ingest a source change and run T080 steps 1-7, pausing at step 8.

    The body is a source-change fixture. It is validated against a strict allowlist
    first: a fixture that carries an affected artifact, a workflow state, a verdict, or
    a proof is refused outright, because those are conclusions this runtime derives.
    """
    try:
        payload = json.loads((await request.body()).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"fixture is not valid JSON: {exc}") from exc

    directory = _fixture_directory(request)
    try:
        dataset = dataset_from_fixture(payload, directory=directory)
    except FixtureRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # A unique namespace per injection. Without it every injected workflow would be
    # named wf-dz-001-001 and the second would replace the first in the registry.
    service = HeroConsoleService(
        dataset=dataset, workflow_namespace=f"wf-{uuid.uuid4().hex[:12]}"
    )
    workflow_id = _registry.register(service)

    run = HeroWorkflowRun(service=service)
    log = await run.start()
    _registry.set_run(workflow_id, run)

    state = service.get_state()
    return {
        "workflow_id": workflow_id,
        "change_id": state["scenario"]["change_id"],
        "state": state["verdict"]["workflow_state"],
        "paused": log.paused_at is not None,
        "pause_reason": (
            "awaiting physical field evidence" if log.paused_at else None
        ),
        "paused_at": log.paused_at,
        "steps_executed": list(log.executed),
        "impact": state["impact"],
        "runtime_readiness": state["environment"]["runtime_readiness"],
        "registry_note": REGISTRY_NOTE,
    }


@app.get("/api/cli/workflows/{workflow_id}")
def cli_status(workflow_id: str) -> dict:
    """Project the authoritative state of one workflow. Zero side effects."""
    service = _resolve(workflow_id)
    state = service.get_state()
    return {
        "workflow_id": workflow_id,
        "change_id": state["scenario"]["change_id"],
        "workflow_state": state["verdict"]["workflow_state"],
        "impact": state["impact"],
        "remediation": state["remediation_state"],
        "delivery": state["delivery"],
        "field_verification": state["field_verification"],
        "deterministic_verdict": state["verdict"],
        "proof": state["proof"],
        "change_deployed": state["verdict"]["change_deployed"],
        "runtime_readiness": state["environment"]["runtime_readiness"],
        "production_ready": state["environment"]["production_ready"],
        "registry_note": REGISTRY_NOTE,
    }


@app.post("/api/cli/workflows/{workflow_id}/verify")
async def cli_verify(workflow_id: str, request: Request) -> dict:
    """Submit field evidence and resume the same paused workflow.

    The body carries image bytes and two **claims** — filename and declared content
    type — which are recorded and never trusted. The MIME type is sniffed from the bytes
    server-side, exactly as the browser upload path does.
    """
    service = _resolve(workflow_id)
    try:
        payload = json.loads((await request.body()).decode("utf-8"))
        raw = base64.b64decode(payload["content_base64"], validate=True)
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400, detail=f"expected a base64 image envelope: {exc}"
        ) from exc
    if len(raw) > UPLOAD_BODY_LIMIT:
        raise HTTPException(status_code=413, detail="field evidence is too large")

    service.submit_field_evidence(
        raw,
        declared_filename=str(payload.get("filename") or "")[:255] or None,
        declared_content_type=str(payload.get("declared_content_type") or "") or None,
    )

    run = _registry.get_run(workflow_id)
    if run is not None:
        # Resume the same ADK invocation: steps 9-11, without re-running 1-7.
        await run.resume()

    # A *corrected* submission after a FAIL arrives once the sequence has already run to
    # completion, so there is nothing left for ADK to resume and step 11 would never fire
    # again. Invoking the gate directly keeps the recovery path whole. It is the same
    # frozen seven-invariant gate either way, and it is idempotent: a blocked proof stays
    # blocked, and an existing proof is returned unchanged rather than regenerated.
    service.generate_proof()

    return cli_status(workflow_id)


@app.post("/api/cli/workflows/{workflow_id}/proof/validate")
def cli_proof_validate(workflow_id: str) -> dict:
    """Authoritative Change Proof validation through the frozen ProofValidator."""
    service = _resolve(workflow_id)
    result = service.validate_proof()
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"workflow {workflow_id!r} has generated no Change Proof",
        )
    return {"workflow_id": workflow_id, **result}


def _resolve(workflow_id: str) -> HeroConsoleService:
    try:
        return _registry.get(workflow_id)
    except UnknownWorkflow as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _fixture_directory(request: Request) -> Path:
    """Where to look for the source documents that accompany a fixture.

    The client may name a directory it already read the fixture from. It is resolved and
    confined to the repository, so a header cannot walk the filesystem.
    """
    raw = request.headers.get("x-fixture-dir")
    if not raw:
        return REPO_ROOT / "fixtures"
    candidate = (REPO_ROOT / raw).resolve()
    if not candidate.is_dir() or REPO_ROOT not in candidate.parents and candidate != REPO_ROOT:
        raise HTTPException(status_code=400, detail="fixture directory is out of bounds")
    return candidate


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
