"""The public DRIFTZERO judge surface.

This is the only DRIFTZERO service on the public internet, and it is deliberately the
least capable one. Every route here is a ``GET`` that renders a page. There is no route
that creates a workflow, submits evidence, invokes a model, publishes to Pub/Sub, or
writes anything anywhere — not gated behind a check, but absent.

That matters for a public surface in a way it would not for an internal one: an
unauthenticated page that can reach a model is an unmetered bill and an open door. The
safest version of this service is one where the dangerous operations were never wired in,
so this module exposes presentation only and reaches the private backend through the
read-only, allow-listed client in :mod:`driftzero_public.backend`.

The operational flow — creating a change, remediating, submitting a photograph, earning a
proof — remains an authenticated operator flow against the private API.
"""

from __future__ import annotations

import logging
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from driftzero_public import views
from driftzero_public.backend import PrivateBackend

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

app = FastAPI(
    title="DRIFTZERO — public judge surface",
    description=(
        "Read-only, evidence-backed public presentation of DRIFTZERO. The operational "
        "backend is a separate, private, IAM-protected Cloud Run service."
    ),
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_backend = PrivateBackend()


def _cache(response: HTMLResponse | FileResponse, seconds: int) -> None:
    response.headers["Cache-Control"] = f"public, max-age={seconds}"


@app.middleware("http")
async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Headers a public page should not ship without.

    The CSP is restrictive because it can be: this surface loads no third-party script,
    no font CDN and no analytics, so nothing legitimate needs a wider policy.
    """
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home() -> HTMLResponse:
    """The root. A visitor with no Google account lands here and understands DRIFTZERO."""
    response = HTMLResponse(views.home(_backend.health()))
    _cache(response, 60)
    return response


@app.get("/demo", response_class=HTMLResponse, include_in_schema=False)
def demo() -> HTMLResponse:
    """The recorded hero run: photographs, chronology, model calls, screenshots."""
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
    """The verified Change Proof artifact, with its hash semantics stated exactly."""
    response = HTMLResponse(views.proof())
    _cache(response, 300)
    return response


@app.get("/health", include_in_schema=False)
def health() -> JSONResponse:
    """Liveness of *this* public service.

    Reports the public surface only. The private backend's state is shown on the pages
    themselves; it is not re-exported as an API, because that would make this service a
    thin proxy for something that is meant to be unreachable.
    """
    return JSONResponse({"status": "ok", "service": "driftzero-web", "surface": "public"})


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


@app.exception_handler(404)
async def missing(request: Request, exc: Exception) -> HTMLResponse:  # noqa: ARG001
    """A judge who mistypes a path gets the product, not a raw JSON error body."""
    return HTMLResponse(views.not_found(), status_code=404)
