"""The public DRIFTZERO judge surface.

This is the only DRIFTZERO service on the public internet, and it stays the least capable
one. Two kinds of route live here:

* **Presentation** — the overview, the recorded run, the architecture, the historical
  proof. Pure reads of content shipped with the image.
* **The live pilot** — the one mutating surface, deliberately narrow. A visitor may run
  the canonical DRIFTZERO packing pilot and nothing else. The source change belongs to
  the server, the workflow belongs to a signed capability, and the verbs are enumerated.
  There is no route that accepts a prompt, a model name, a workflow id or a path, so this
  service cannot be turned into a Gemini proxy or a reverse proxy for the private API.

Everything a visitor changes, they change through the same private, IAM-protected backend
that an operator uses. The browser never reaches it; only this service does, using the
service identity Cloud Run attaches to the revision.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any, Final

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from driftzero_public import capability as cap
from driftzero_public import live_views, views
from driftzero_public.backend import PrivateBackend
from driftzero_public.live import (
    MAX_UPLOAD_BYTES,
    LivePilot,
    LivePilotError,
    UnsupportedEvidence,
    recompute_content_hash,
    sniff_image,
)

logger = logging.getLogger("driftzero.public")

#: Static files this service will serve. An allow-list rather than a directory walk, so a
#: crafted path cannot read something that merely happens to sit next to the assets.
SERVABLE: Final[frozenset[str]] = frozenset(
    {
        "public.css",
        "driftzero-worker-delta.png",
        "driftzero-worker-failed.png",
        "driftzero-worker-verified.png",
        "driftzero-change-proof.png",
        "driftzero-proof-desktop.png",
        "driftzero-desktop.png",
        "driftzero-photo-left.jpg",
        "driftzero-photo-top-right.jpg",
    }
)

MEDIA_TYPES: Final[dict[str, str]] = {
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
}

#: One byte more than the cap, so an oversized upload is detected rather than truncated
#: into something that looks acceptable.
MAX_READ: Final[int] = MAX_UPLOAD_BYTES + 1

app = FastAPI(
    title="DRIFTZERO — public judge surface",
    description=(
        "The public DRIFTZERO experience: a live pilot that runs the real product on "
        "Google Cloud, plus recorded evidence. The operational backend is a separate, "
        "private, IAM-protected Cloud Run service."
    ),
    version="1.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_backend = PrivateBackend()
_pilot = LivePilot(_backend)


def _cache(response: HTMLResponse | FileResponse, seconds: int) -> None:
    response.headers["Cache-Control"] = f"public, max-age={seconds}"


def _live_html(markup: str, status_code: int = 200) -> HTMLResponse:
    """Live pages are never cached: each one reports backend state at a moment."""
    response = HTMLResponse(markup, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Headers a public page should not ship without.

    ``script-src 'none'`` survives the live pilot because the whole flow is HTML forms
    and POST-redirect-GET. Nothing on these pages can progress without the backend having
    answered first, which is a security property and a truthfulness one at once.
    """
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    return response


# ================================ presentation =========================================


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home() -> HTMLResponse:
    """The root. A visitor with no Google account lands here and can run the pilot."""
    response = HTMLResponse(views.home(_backend.health()))
    _cache(response, 60)
    return response


@app.get("/demo", response_class=HTMLResponse, include_in_schema=False)
def demo() -> HTMLResponse:
    """The recorded hero run, kept as the audit path beside the live one."""
    response = HTMLResponse(views.demo())
    _cache(response, 300)
    return response


@app.get("/architecture", response_class=HTMLResponse, include_in_schema=False)
def architecture() -> HTMLResponse:
    response = HTMLResponse(views.architecture(_backend.health()))
    _cache(response, 60)
    return response


@app.get("/proof", response_class=HTMLResponse, include_in_schema=False)
def proof() -> HTMLResponse:
    """The recorded Change Proof, with its hash semantics stated exactly."""
    response = HTMLResponse(views.proof())
    _cache(response, 300)
    return response


@app.get("/health", include_in_schema=False)
def health() -> JSONResponse:
    """Liveness of *this* public service.

    The private backend's state is shown on the pages themselves; it is not re-exported
    as an API, because that would make this service a thin proxy for something meant to
    be unreachable.
    """
    return JSONResponse(
        {
            "status": "ok",
            "service": "driftzero-web",
            "surface": "public",
            "live_pilot": "enabled",
            # Whether pilot sessions survive a restart, i.e. whether a real signing key is
            # configured. Visible so an instance on an ephemeral key is diagnosable.
            "session_signing": "durable" if cap.signing_key_is_durable() else "ephemeral",
        }
    )


# response_model=None: the handler returns a file or a 404 body, and FastAPI cannot
# build a response model from that union — it must not try.
@app.get("/static/{asset}", include_in_schema=False, response_model=None)
def static(asset: str) -> FileResponse | JSONResponse:
    if asset not in SERVABLE:
        return JSONResponse({"error": "NOT_FOUND"}, status_code=404)
    path = views.ASSETS / asset
    if not path.is_file():
        return JSONResponse({"error": "NOT_FOUND"}, status_code=404)
    media = MEDIA_TYPES.get(path.suffix, "application/octet-stream")
    response = FileResponse(path, media_type=media)
    _cache(response, 86_400)
    return response


# ================================ the live pilot =======================================
#
# The only mutating surface on the public internet. Narrow by construction: the source
# change is the server's, the workflow is the capability's, and the verbs are the five
# below. No route takes a workflow id, a prompt, a model name or a path.


@app.get("/live", response_class=HTMLResponse, include_in_schema=False)
def live_landing() -> HTMLResponse:
    """Pre-flight. States what the run will do before it spends anything."""
    status = _backend.health()
    return _live_html(live_views.landing(status.reachable, status.detail))


@app.post("/live/start", include_in_schema=False, response_model=None)
def live_start() -> Any:
    """Create a fresh workflow and drive it to where it needs physical evidence.

    Synchronous on purpose. The backend call runs Change Intelligence, remediation and
    delivery as one request; returning early would mean rendering progress this service
    had not been told about, which is the one thing this product must not do.
    """
    try:
        started = _pilot.start()
    except LivePilotError as exc:
        return _live_html(live_views.refused("The pilot could not start", str(exc)), 503)

    token = cap.issue(started.workflow_id, started.change_id)
    try:
        state = _pilot.advance(cap.verify(token))
    except LivePilotError as exc:
        return _live_html(
            live_views.refused("The pilot could not complete its analysis", str(exc)), 503
        )

    if not state.get("delivery_established"):
        # An honest refusal rather than an empty delta screen: the backend did not reach
        # delivery, so there is nothing for a worker to act on.
        return _live_html(
            live_views.refused(
                "The pilot did not reach the frontline",
                "Change Intelligence did not qualify a target on this run, so no delta "
                "was delivered. Nothing was assumed in its place.",
            ),
            409,
        )

    logger.info(
        "live pilot advanced",
        extra={"workflow_id": started.workflow_id, "state": state.get("state")},
    )
    response = RedirectResponse(f"/live/pilot?capability={token}", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/live/pilot", response_class=HTMLResponse, include_in_schema=False)
def live_pilot(capability: str = "") -> HTMLResponse:
    """The worker's delta view, for this capability's own workflow."""
    try:
        held = cap.verify(capability)
    except cap.CapabilityInvalid as exc:
        return _live_html(live_views.refused("This pilot session is not valid", str(exc)), 403)
    try:
        state = _pilot.status(held)
    except LivePilotError as exc:
        return _live_html(live_views.refused("The pilot backend did not answer", str(exc)), 503)
    return _live_html(live_views.delta(state, capability, held.seconds_remaining))


def _observation_from(state: dict[str, Any], result: str) -> str:
    """What the model reported, derived from the verdict when not echoed back.

    The API returns the comparator's verdict rather than the raw observation, and for the
    two determinate outcomes the observation follows from it: a PASS means the expected
    value was observed, a FAIL on this pilot means the previous one was. INCONCLUSIVE is
    reported as itself and never resolved into a position.
    """
    delta = state.get("delta") or {}
    if result == "PASS":
        return str(delta.get("after_value") or "TOP_RIGHT")
    if result == "FAIL":
        return str(delta.get("before_value") or "LEFT")
    return "INCONCLUSIVE"


def _verify_and_render(
    held: cap.Capability,
    token: str,
    raw: bytes,
    name: str,
    media: str,
    *,
    submitted_by: str,
) -> HTMLResponse:
    """Submit evidence, then render whatever the backend actually decided.

    ``submitted_by`` is carried through so the retry offered after a non-passing verdict
    matches how this attempt arrived. Without it a visitor who uploaded their own
    photograph was offered only the server-owned one.
    """
    started = time.monotonic()
    try:
        outcome = _pilot.verify(held, raw, filename=name, media_type=media)
    except LivePilotError as exc:
        return _live_html(live_views.refused("The verification did not complete", str(exc)), 503)
    latency = time.monotonic() - started

    result = outcome.get("verification_result") or "INCONCLUSIVE"
    try:
        state = _pilot.status(held)
    except LivePilotError:
        state = {}

    proof_ready = False
    if result == "PASS":
        # Requested, never assumed: if the seven conditions do not hold the backend
        # refuses and this stays False.
        try:
            proof_ready = _pilot.ensure_proof(held) is not None
        except LivePilotError:
            proof_ready = False

    logger.info(
        "live verification",
        extra={
            "workflow_id": held.workflow_id,
            "result": result,
            "latency_seconds": round(latency, 3),
        },
    )
    return _live_html(
        live_views.verdict(
            result,
            _observation_from(state, result),
            state,
            token,
            proof_ready=proof_ready,
            latency=latency,
            submitted_by=submitted_by,
        )
    )


@app.post("/live/verify", include_in_schema=False, response_model=None)
def live_verify(capability: str = Form(""), photo: str = Form("current")) -> HTMLResponse:
    """Verify using one of the two real pilot photographs.

    ``photo`` selects a role, not a path: anything but the two known roles is refused
    before the filesystem is touched.
    """
    try:
        held = cap.verify(capability)
    except cap.CapabilityInvalid as exc:
        return _live_html(live_views.refused("This pilot session is not valid", str(exc)), 403)
    try:
        raw, name, media = LivePilot.pilot_photo(photo)
    except UnsupportedEvidence as exc:
        return _live_html(live_views.refused("That evidence cannot be submitted", str(exc)), 400)
    return _verify_and_render(held, capability, raw, name, media, submitted_by="pilot")


@app.post("/live/upload", include_in_schema=False, response_model=None)
async def live_upload(
    capability: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> HTMLResponse:
    """Verify using a photograph the visitor supplies.

    The bytes are bounded and sniffed here; the declared filename and browser
    Content-Type are claims, never authority. The verdict remains the Truth Engine's, and
    INCONCLUSIVE is rendered as itself rather than hidden.
    """
    try:
        held = cap.verify(capability)
    except cap.CapabilityInvalid as exc:
        return _live_html(live_views.refused("This pilot session is not valid", str(exc)), 403)

    if file is None:
        return _live_html(
            live_views.refused("No photograph was submitted", "Choose an image first."), 400
        )
    raw = await file.read(MAX_READ)
    try:
        media = sniff_image(raw)
    except UnsupportedEvidence as exc:
        return _live_html(
            live_views.refused("That file cannot be used as evidence", str(exc)), 400
        )
    return _verify_and_render(
        held, capability, raw, "field-evidence", media, submitted_by="upload"
    )


@app.get("/live/proof", response_class=HTMLResponse, include_in_schema=False)
def live_proof(capability: str = "") -> HTMLResponse:
    """This capability's own Change Proof — never a historical one."""
    try:
        held = cap.verify(capability)
    except cap.CapabilityInvalid as exc:
        return _live_html(live_views.refused("This pilot session is not valid", str(exc)), 403)
    try:
        document = _pilot.proof(held)
    except LivePilotError as exc:
        return _live_html(live_views.refused("The proof could not be read", str(exc)), 503)
    if document is None:
        return _live_html(
            live_views.refused(
                "No Change Proof exists for this run",
                "A proof exists only once all seven completion conditions hold.",
            ),
            404,
        )
    return _live_html(live_views.live_proof(document, recompute_content_hash(document)))


@app.exception_handler(404)
async def missing(request: Request, exc: Exception) -> HTMLResponse:  # noqa: ARG001
    """A judge who mistypes a path gets the product, not a raw JSON error."""
    return HTMLResponse(views.not_found(), status_code=404)
