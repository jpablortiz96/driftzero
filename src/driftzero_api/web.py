"""M6 — serving the DRIFTZERO product surface.

The pages and assets live at their task-declared paths under ``src/driftzero/web/``.
They are HTML, CSS and JavaScript — not Python — so the M0 purity guard, which scans
``*.py`` imports, is untouched by them. The *serving* code needs FastAPI, so it lives
out here with the rest of the transport.

These routes sit behind the same Cloud Run IAM boundary as every other route on this
service. Nothing here is public, and no route was added to make the UI reachable without
the authorization the rest of the API requires.

The surface is a client of the T094 API. It gets no privileged endpoint of its own: the
browser calls the same ``/api/v1`` routes any other caller does, and is refused the same
way.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

router = APIRouter()

WEB_ROOT = Path(__file__).resolve().parents[1] / "driftzero" / "web"
TEMPLATES = WEB_ROOT / "templates"
STATIC = WEB_ROOT / "static"

PAGES = {
    "delta": "delta.html",       # T127 — what changed, and what to do
    "verify": "verify.html",     # T128 — show that it is done
    "workflow": "workflow.html",  # T130 — workflow state visualisation
    "proof": "proof.html",       # T130 — Change Proof display
}

MEDIA_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
}


@router.get("/web/{page}", include_in_schema=False)
def serve_page(page: str) -> FileResponse:
    """Serve one product page by name.

    Only names in :data:`PAGES` resolve. The requested value never becomes part of a
    filesystem path, so no traversal is possible regardless of what is asked for.
    """
    filename = PAGES.get(page)
    if filename is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "NO_SUCH_PAGE", "detail": f"unknown page {page!r}"},
        )
    return FileResponse(TEMPLATES / filename, media_type="text/html; charset=utf-8")


@router.get("/web/static/{asset}", include_in_schema=False)
def serve_asset(asset: str) -> FileResponse:
    """Serve one static asset from the surface's own directory.

    ``asset`` is matched against the directory listing rather than joined onto a path,
    so ``..`` and absolute paths cannot escape: a name that is not an actual file in
    ``static/`` is simply not found.
    """
    candidates = {path.name: path for path in STATIC.iterdir() if path.is_file()}
    path = candidates.get(asset)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "NO_SUCH_ASSET", "detail": f"unknown asset {asset!r}"},
        )
    return FileResponse(
        path, media_type=MEDIA_TYPES.get(path.suffix, "application/octet-stream")
    )
