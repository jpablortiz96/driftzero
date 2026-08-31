"""The public judge surface — what it renders, and what it structurally cannot do.

This is the only DRIFTZERO service on the public internet, so the tests that matter most
here are the negative ones. A public page that can reach a model is an unmetered bill; a
public page that can forward an arbitrary path to a private backend is that backend made
public with extra steps. Both are asserted against the route table and the backend
client, not against a configuration flag someone can flip.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from driftzero_public import backend as backend_module
from driftzero_public import views
from driftzero_public.app import SERVABLE, app
from driftzero_public.backend import READABLE, BackendStatus, PathNotReadable, PrivateBackend

PAGES = ("/", "/demo", "/architecture", "/proof")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client whose backend probe never leaves the process.

    The health call is stubbed so the suite makes no network attempt: a test that
    silently depends on metadata-server reachability would pass locally and mean nothing.
    """
    monkeypatch.setattr(
        PrivateBackend,
        "health",
        lambda self: BackendStatus(
            reachable=True,
            label="SERVING",
            detail="stubbed for tests",
            checked_at="2026-01-01T00:00:00+00:00",
        ),
    )
    return TestClient(app, raise_server_exceptions=False)


# ------------------------------------------------------------------ it serves


def test_root_returns_200_and_is_not_a_404(client: TestClient) -> None:
    """The judge experience starts at ``/``; a Not Found there is the whole failure."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


@pytest.mark.parametrize("path", PAGES)
def test_every_public_page_renders(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert len(response.text) > 1_500, f"{path} rendered suspiciously little HTML"


def test_health_reports_this_service_only(client: TestClient) -> None:
    payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["service"] == "driftzero-web"
    assert payload["surface"] == "public"


def test_hero_states_the_product_in_the_first_screen(client: TestClient) -> None:
    body = client.get("/").text
    assert "DRIFTZERO" in body
    assert "The autonomous last-mile for operational change." in body
    assert "It's deployed when the work changes." in body


def test_home_shows_the_five_stage_flow(client: TestClient) -> None:
    body = client.get("/").text
    for stage in ("SOURCE CHANGE", "IMPACT", "ACTION", "FRONTLINE VERIFICATION", "CHANGE PROOF"):
        assert stage in body, f"the flow is missing {stage}"


def test_github_repository_is_linked_on_every_page(client: TestClient) -> None:
    for path in PAGES:
        assert "https://github.com/jpablortiz96/driftzero" in client.get(path).text


def test_unknown_path_renders_the_product_not_a_raw_error(client: TestClient) -> None:
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert "DRIFTZERO" in response.text
    assert "text/html" in response.headers["content-type"]


# --------------------------------------------------------- real recorded content


def test_demo_shows_the_real_fail_then_pass_chronology(client: TestClient) -> None:
    body = client.get("/demo").text
    assert "FAIL" in body and "PASS" in body
    # Order matters: the failure came first, and the page must not reorder history.
    assert body.index("FAIL") < body.rindex("PASS")


def test_demo_reports_the_recorded_model_calls(client: TestClient) -> None:
    body = client.get("/demo").text
    assert "gemma-4-26b-a4b-it-maas" in body
    for observation in ("LEFT", "TOP_RIGHT"):
        assert observation in body


def test_proof_page_renders_the_verified_artifact(client: TestClient) -> None:
    document = views.proof_document()
    body = client.get("/proof").text
    for key in ("change_id", "affected_artifact_id", "proof_id", "content_hash"):
        assert str(document[key]) in body, f"the proof page omits {key}"


def test_proof_page_states_the_hash_semantics_exactly(client: TestClient) -> None:
    """The bounded claim is the product. It must survive any future edit to this page."""
    body = client.get("/proof").text
    assert "excluding its own" in body
    for denied in (
        "not</b> a digital signature",
        "not</b> an attestation",
        "not</b> a trusted timestamp",
    ):
        assert denied in body, f"the proof page no longer denies: {denied}"


def test_screenshots_are_labelled_as_recorded_evidence(client: TestClient) -> None:
    """Recorded evidence must never be presented as a live interaction.

    Whitespace is flattened first: a caption that wraps across two source lines is the
    same caption to a reader, and an assertion that disagrees is testing the line width.
    """

    def prose(path: str) -> str:
        return re.sub(r"\s+", " ", client.get(path).text).lower()

    assert "recorded evidence from a real run" in prose("/")
    assert "not a live interaction" in prose("/demo")


def test_shipped_evidence_matches_the_repository_originals() -> None:
    """The page and the audit trail must not drift apart."""
    repo_root = Path(__file__).resolve().parents[2]
    shipped = json.loads((views.ASSETS / "change_proof.json").read_text(encoding="utf-8"))
    original = json.loads(
        (
            repo_root / "evidence" / "final_live_pilot_2026_08_26" / "change_proof_DZ-001.json"
        ).read_text(encoding="utf-8")
    )
    assert shipped == original

    shipped_run = json.loads((views.ASSETS / "hero_run.json").read_text(encoding="utf-8"))
    original_run = json.loads(
        (repo_root / "evidence" / "runs" / "hero_run_001" / "real_camera_hero_run.json").read_text(
            encoding="utf-8"
        )
    )
    assert shipped_run == original_run


# ------------------------------------------------------- no mutation surface


def test_the_only_mutating_routes_are_the_three_live_pilot_ones() -> None:
    """The mutating surface is enumerated, not merely guarded.

    The public service is no longer read-only: it runs the real pilot. What keeps that
    safe is that exactly three POST routes exist, all belonging to the live pilot, and
    every other verb on every other path is absent rather than rejected. A fourth POST
    appearing here is a change that must be argued for, not noticed later.
    """
    mutating = {
        (route.path, verb)
        for route in app.routes
        if getattr(route, "methods", None)
        for verb in route.methods - {"GET", "HEAD", "OPTIONS"}
    }
    assert mutating == {
        ("/live/start", "POST"),
        ("/live/verify", "POST"),
        ("/live/upload", "POST"),
    }, f"unexpected mutating routes on the public surface: {sorted(mutating)}"


def test_no_presentation_route_accepts_a_mutation(client: TestClient) -> None:
    """The pages that existed before the live pilot stay reads."""
    for path in ("/", "/demo", "/architecture", "/proof", "/health"):
        assert client.post(path).status_code in (404, 405)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/changes",
        "/api/v1/workflows/wf-1/verify",
        "/api/v1/workflows/wf-1/proof",
        "/pubsub/push",
        "/ready",
    ],
)
def test_no_backend_operational_route_is_republished(client: TestClient, path: str) -> None:
    """Creating work, submitting evidence and ingesting events stay off the internet."""
    assert client.get(path).status_code == 404
    assert client.post(path).status_code in (404, 405)


def test_ready_is_not_proxied(client: TestClient) -> None:
    """`/ready` carries stale pilot wording and internal detail; it stays unpublished."""
    assert client.get("/ready").status_code == 404
    assert "/ready" not in READABLE


def test_no_route_accepts_an_upload(client: TestClient) -> None:
    """No public path may accept a photograph, which is what would trigger a model call."""
    response = client.post("/verify", files={"photo": ("x.jpg", b"\xff\xd8\xff", "image/jpeg")})
    assert response.status_code in (404, 405)


def test_public_module_imports_no_model_or_data_plane_client() -> None:
    """A public process that cannot import a data plane cannot reach one by accident."""
    forbidden = (
        "google.cloud.firestore",
        "google.cloud.storage",
        "google.adk",
        "vertexai",
        "driftzero.truth_engine",
        "driftzero_cloud",
        "driftzero_adk",
        "driftzero_api",
    )
    import driftzero_public.app as app_module

    for module in (app_module, backend_module, views):
        source = inspect.getsource(module)
        for name in forbidden:
            assert name not in source, f"{module.__name__} references {name}"


# ------------------------------------------------------ backend boundary


def test_backend_reads_are_allow_listed_not_parameterised() -> None:
    backend = PrivateBackend("https://example.invalid")
    with pytest.raises(PathNotReadable):
        backend._read("/api/v1/changes")
    with pytest.raises(PathNotReadable):
        backend._read("/ready")
    with pytest.raises(PathNotReadable):
        backend._read("/../admin")


def test_only_health_is_readable() -> None:
    assert READABLE == frozenset({"/health"})


def test_backend_client_offers_no_write_method() -> None:
    writes = [
        name
        for name in dir(PrivateBackend)
        if not name.startswith("__")
        and any(verb in name.lower() for verb in ("post", "put", "patch", "delete", "write"))
    ]
    assert not writes, f"the backend client exposes write helpers: {writes}"


def test_identity_token_is_private_and_never_returned_to_a_page() -> None:
    """The browser must never receive a backend token."""
    assert not hasattr(PrivateBackend, "identity_token"), "the token accessor must stay private"
    status_fields = set(BackendStatus.__dataclass_fields__)
    assert not status_fields & {"token", "authorization", "credential", "id_token"}

    rendered = inspect.getsource(views)
    for leak in ("Authorization", "Bearer", "id_token", "_identity_token"):
        assert leak not in rendered, f"the view layer references {leak}"


def test_rendered_pages_carry_no_credential_shaped_material(client: TestClient) -> None:
    patterns = {
        "bearer": r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}",
        "jwt": r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}",
        "api_key": r"\bAIza[0-9A-Za-z_\-]{35}\b",
        "oauth": r"\bya29\.[0-9A-Za-z_\-]{20,}",
        "private_key": r"BEGIN [A-Z ]*PRIVATE KEY",
        "billing": r"\b0[0-9A-F]{5}-[0-9A-F]{6}-[0-9A-F]{6}\b",
    }
    for path in (*PAGES, "/health"):
        body = client.get(path).text
        for name, pattern in patterns.items():
            assert not re.search(pattern, body), f"{path} leaked {name}"


def test_backend_failure_degrades_instead_of_500ing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A downstream timeout must not take the public page down."""
    import httpx

    def explode(*_args: object, **_kwargs: object) -> None:
        raise httpx.ConnectTimeout("no route")

    monkeypatch.setattr(backend_module.httpx, "get", explode)
    status = PrivateBackend("https://example.invalid").health()
    assert status.reachable is False
    assert status.label == "UNREACHABLE"

    with TestClient(app, raise_server_exceptions=False) as raw:
        assert raw.get("/").status_code == 200


def test_unconfigured_backend_reports_honestly_rather_than_claiming_health() -> None:
    status = PrivateBackend("").health()
    assert status.reachable is False
    assert status.label == "UNVERIFIED"


# ------------------------------------------------------------------- assets


def test_static_assets_are_allow_listed(client: TestClient) -> None:
    assert client.get("/static/public.css").status_code == 200
    for hostile in ("../../etc/passwd", "..%2f..%2fpyproject.toml", "hero_run.json"):
        assert client.get(f"/static/{hostile}").status_code == 404


def test_every_referenced_asset_is_servable_and_present(client: TestClient) -> None:
    referenced = set()
    for path in PAGES:
        referenced.update(re.findall(r'src="/static/([^"]+)"', client.get(path).text))
        referenced.update(re.findall(r'href="/static/([^"]+)"', client.get(path).text))
    assert referenced, "no assets referenced at all — the pages lost their images"
    for asset in sorted(referenced):
        assert asset in SERVABLE, f"{asset} is referenced but not servable"
        assert (views.ASSETS / asset).is_file(), f"{asset} is missing from the package"
        assert client.get(f"/static/{asset}").status_code == 200


def test_security_headers_are_present(client: TestClient) -> None:
    headers = client.get("/").headers
    assert "script-src 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"


def test_pages_render_no_script_tag(client: TestClient) -> None:
    """The CSP forbids script; the pages must not need one."""
    for path in PAGES:
        assert "<script" not in client.get(path).text.lower()


def test_the_public_extra_declares_everything_the_routes_need() -> None:
    """A missing form parser is a container that never listens, not an import error.

    FastAPI analyses Form and File parameters at startup, so an undeclared
    python-multipart passes every local test (the dev extra installs it) and then fails
    the Cloud Run health check. Asserting the declaration is what catches it on the
    ground rather than in a deploy log.
    """
    import re
    from pathlib import Path as _Path

    pyproject = (_Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    block = re.search(r"^public = \[(.*?)^\]", pyproject, re.S | re.M)
    assert block, "the public extra is missing from pyproject.toml"
    declared = block.group(1)
    for requirement in ("fastapi", "uvicorn", "httpx", "python-multipart"):
        assert requirement in declared, f"the public extra does not declare {requirement}"
    # The public image must not pull a data-plane client into the internet-facing service.
    for forbidden in ("google-cloud-firestore", "google-cloud-storage", "google-adk"):
        assert forbidden not in declared, f"the public extra pulls in {forbidden}"
