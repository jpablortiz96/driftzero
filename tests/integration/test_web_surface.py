"""M6 — the product surface (T127 delta, T128 verify, T129 minimums, T130 proof).

The pages are static HTML driven by JavaScript against the real API, so the assertions
split in two: the served documents are checked structurally, and the API behaviour they
depend on is checked by driving the real seam. Both matter — a page that renders
beautifully against an endpoint that does not exist is not a product.

The recurring question is the same one the backend answers: can the *client* cause the
system to look like it succeeded? Every answer here has to be no.

Offline throughout: deterministic model substitutes, no cloud, no billable call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from driftzero.agents import field_verify as fv
from driftzero.agents import model_client as mc
from driftzero_api.app import create_app
from driftzero_api.runtime import ApiRuntime
from driftzero_api.web import PAGES
from tests.integration._pilot import arm_for_service, clear_change_intelligence
from tests.integration.test_restart_persistence import OfflineGemma

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB = REPO_ROOT / "src" / "driftzero" / "web"
TEMPLATES = WEB / "templates"
STATIC = WEB / "static"
FIXTURES = REPO_ROOT / "fixtures"
HERO_FIXTURE = FIXTURES / "hero_change.json"
LEFT_IMG = FIXTURES / "multimodal" / "label_left_01.jpg"
TOP_RIGHT_IMG = FIXTURES / "multimodal" / "label_top_right_01.jpg"


def hero_body() -> dict[str, Any]:
    payload = json.loads(HERO_FIXTURE.read_text(encoding="utf-8"))
    return {k: v for k, v in payload.items() if not k.startswith("_")}


@pytest.fixture
def runtime() -> ApiRuntime:
    return ApiRuntime(fixtures_dir=FIXTURES)


@pytest.fixture
def client(runtime: ApiRuntime) -> TestClient:
    return TestClient(create_app(runtime))


@pytest.fixture
def providers() -> Any:
    import os

    previous = os.environ.get("DRIFTZERO_FIELD_PROVIDER")
    os.environ["DRIFTZERO_FIELD_PROVIDER"] = "vertex_maas"
    gemma = OfflineGemma(["LEFT", "TOP_RIGHT"])
    fv.register_field_observation_provider(lambda _c: gemma)
    yield gemma
    clear_change_intelligence()
    fv.clear_field_observation_provider()
    mc.clear_model_client_provider()
    if previous is None:
        os.environ.pop("DRIFTZERO_FIELD_PROVIDER", None)
    else:
        os.environ["DRIFTZERO_FIELD_PROVIDER"] = previous


def page(name: str) -> str:
    return (TEMPLATES / PAGES[name]).read_text(encoding="utf-8")


def script(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def strip_comments(value: str) -> str:
    """Remove JS and HTML comments, leaving only what a user could ever see."""
    without_block = re.sub(r"/\*.*?\*/", " ", value, flags=re.S)
    without_line = re.sub(r"(?m)^\s*//.*$", " ", without_block)
    return re.sub(r"<!--.*?-->", " ", without_line, flags=re.S)


def flat(value: str) -> str:
    """Collapse whitespace so a phrase that wraps across lines still matches.

    Block-comment ``*`` line prefixes are dropped too: a sentence wrapped inside a JS
    comment otherwise reads as "the server * derives ...", which no search would match.
    """
    without_prefixes = re.sub(r"(?m)^\s*\*[ 	]?", " ", value)
    return re.sub(r"\s+", " ", without_prefixes)


# ============================ the declared paths ======================================


def test_every_task_declared_file_exists() -> None:
    """T127/T128/T130 name these paths exactly."""
    for name in ("delta.html", "verify.html", "workflow.html", "proof.html"):
        assert (TEMPLATES / name).is_file(), name
    assert STATIC.is_dir()
    assert (STATIC / "driftzero.css").is_file()


def test_the_surface_adds_no_python_to_the_purity_boundary() -> None:
    """HTML and CSS are assets; a .py here would need FastAPI and break the guard."""
    assert list(WEB.rglob("*.py")) == []


def test_the_surface_assets_are_declared_as_package_data() -> None:
    """Setuptools ships only *.py by default.

    Without this declaration the wheel installs cleanly and every /web route 500s in
    the container — which is exactly what the first M6 deployment did. The data is
    attached to the parent package because driftzero/web/ deliberately holds no
    __init__.py.
    """
    import tomllib

    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    data = config["tool"]["setuptools"]["package-data"]
    assert "driftzero" in data, "the surface assets are not declared as package data"
    patterns = data["driftzero"]
    assert "web/templates/*.html" in patterns
    assert "web/static/*" in patterns


def test_every_shipped_asset_matches_a_declared_pattern() -> None:
    """A new asset in a directory nobody declared would ship locally and 500 deployed."""
    import fnmatch
    import tomllib

    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["setuptools"]["package-data"]["driftzero"]
    for path in sorted(WEB.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(WEB.parent).as_posix()
        assert any(fnmatch.fnmatch(relative, pattern) for pattern in patterns), (
            f"{relative} would not be packaged into the wheel"
        )


def test_every_page_is_served(client: TestClient) -> None:
    for name in PAGES:
        response = client.get(f"/web/{name}")
        assert response.status_code == 200, name
        assert response.headers["content-type"].startswith("text/html")


def test_static_assets_are_served(client: TestClient) -> None:
    for asset, media in (
        ("driftzero.css", "text/css"),
        ("worker.js", "text/javascript"),
        ("proof.js", "text/javascript"),
    ):
        response = client.get(f"/web/static/{asset}")
        assert response.status_code == 200, asset
        assert response.headers["content-type"].startswith(media)


@pytest.mark.parametrize(
    "asset",
    ["../config.py", "../../driftzero/config.py", "..", "/etc/passwd", "worker.js/../x"],
)
def test_asset_traversal_is_refused(client: TestClient, asset: str) -> None:
    assert client.get(f"/web/static/{asset}").status_code == 404


def test_an_unknown_page_is_404(client: TestClient) -> None:
    assert client.get("/web/admin").status_code == 404


# ============================ T127 — the delta view ===================================


def test_the_delta_view_shows_before_and_after(client: TestClient) -> None:
    html = page("delta")
    assert 'id="before-value"' in html
    assert 'id="after-value"' in html
    assert 'id="requirement"' in html
    assert 'id="artifact-context"' in html


def test_the_delta_view_states_the_product_thesis(client: TestClient) -> None:
    html = page("delta")
    assert "isn&rsquo;t deployed when the document changes" in html
    assert "deployed when the work changes" in html


def test_the_delta_is_rendered_from_the_api_not_composed_in_the_client() -> None:
    """No second explanation. The client reads what the Truth Engine composed."""
    js = script("worker.js")
    assert "state.delta" in js
    assert "delta.before_value" in js and "delta.after_value" in js
    # Nothing that would fabricate an instruction.
    for invented in ("Move the label", "please move", "instructions ="):
        assert invented not in js


def test_a_workflow_without_a_delta_says_so_rather_than_inventing_one() -> None:
    js = script("worker.js")
    assert "Not available yet" in js
    assert "has not been delivered yet" in js


def test_the_api_supplies_the_delta_the_view_needs(
    client: TestClient, runtime: ApiRuntime, providers: Any
) -> None:
    workflow_id = client.post("/api/v1/changes", json=hero_body()).json()["workflow_id"]
    service = runtime.live_service(workflow_id)
    arm_for_service(service)
    service.analyze_change()
    service.deploy_change()
    service.deliver_to_frontline()

    status = client.get(f"/api/v1/workflows/{workflow_id}").json()
    assert status["delta"] is not None
    assert status["delta"]["before_value"] == "LEFT"
    assert status["delta"]["after_value"] == "TOP_RIGHT"
    assert status["delta"]["artifact_id"] == "wi-packing-standard-001"
    assert status["delivery_established"] is True


def test_before_delivery_the_api_reports_no_delta(client: TestClient) -> None:
    """The worker surface opens on validated delivery, not on composition."""
    workflow_id = client.post("/api/v1/changes", json=hero_body()).json()["workflow_id"]
    status = client.get(f"/api/v1/workflows/{workflow_id}").json()
    assert status["delta"] is None
    assert status["delivery_established"] is False


# ============================ T128 — evidence submission ==============================


def test_the_verify_page_offers_camera_and_file_fallback() -> None:
    """One input serves both: capture on a phone, file picker everywhere else."""
    html = page("verify")
    assert 'type="file"' in html
    assert 'accept="image/*"' in html
    assert 'capture="environment"' in html
    assert "choose an image file" in html.lower()


def test_the_file_input_keeps_its_label_and_keyboard_reachability() -> None:
    """A visually replaced input must stay in the a11y tree, so never display:none."""
    html = page("verify")
    assert 'for="photo"' in html
    assert 'id="photo"' in html
    css = (STATIC / "driftzero.css").read_text(encoding="utf-8")
    file_rule = css[css.index(".file-input {") : css.index(".hint {")]
    assert "display: none" not in file_rule
    assert "clip:" in file_rule
    assert ".file-input:focus-visible + label" in css


def test_a_stable_submission_id_is_computed_from_the_bytes() -> None:
    """T128 requires a stable submission_id; retrying one photo must reuse it."""
    js = script("worker.js")
    assert "submissionId" in js
    assert 'digest("SHA-256"' in js
    assert 'form.append("submission_id"' in js


def test_the_submission_id_is_sent_as_a_claim_not_as_authority() -> None:
    js = script("worker.js")
    body = flat(js)
    assert "is a *claim*" in body
    assert "server derives the authoritative submission identity" in body


def test_the_client_never_submits_a_verdict() -> None:
    """The one thing a client must not be able to do."""
    js = script("worker.js")
    body = js[js.index("submit.addEventListener") : js.index("global.DriftZero")]
    for forbidden in ("verification_result", "expected", "observed", "verdict", "proof"):
        assert f'append("{forbidden}"' not in body, forbidden
    assert body.count("form.append") == 2, "only the file and the submission id are sent"


def test_the_server_ignores_a_client_supplied_submission_id(
    client: TestClient, runtime: ApiRuntime, providers: Any
) -> None:
    workflow_id = client.post("/api/v1/changes", json=hero_body()).json()["workflow_id"]
    service = runtime.live_service(workflow_id)
    arm_for_service(service)
    service.analyze_change()
    service.deploy_change()
    service.deliver_to_frontline()

    response = client.post(
        f"/api/v1/workflows/{workflow_id}/verify",
        files={"file": ("left.jpg", LEFT_IMG.read_bytes(), "image/jpeg")},
        data={"submission_id": "client-forged-id"},
    )
    assert response.status_code == 200
    assert response.json()["submission_id"] != "client-forged-id"


def test_the_client_re_reads_state_rather_than_trusting_its_own_response() -> None:
    """What the worker sees is the server's state, not this request's echo."""
    js = script("worker.js")
    assert "return loadStatus();" in js
    assert "not this request" in js or "not trusted as the final word" in js


# ============================ FAIL -> retry -> PASS ===================================


def test_failure_is_rendered_as_recoverable_not_terminal() -> None:
    js = script("worker.js")
    assert "Not done yet" in js
    assert "take another photo" in js.lower()
    # The retry affordance is restored, not removed.
    assert 'text("photo-label", "Take another photo")' in js


def test_inconclusive_is_distinct_from_failure() -> None:
    js = script("worker.js")
    assert "Can't tell from that photo" in js
    assert "clearly visible" in js


def test_the_verdict_mapping_is_total() -> None:
    """Every verdict the server can send has an explicit branch."""
    js = script("worker.js")
    start = js.index("function renderVerdict")
    body = js[start : js.index("// ------", start)]
    for verdict in ("PASS", "FAIL", "INCONCLUSIVE"):
        assert f'"{verdict}"' in body, verdict
    assert "Waiting for verification" in body, "an unknown value falls back to waiting"


def test_the_real_fail_then_pass_flow_over_the_api(
    client: TestClient, runtime: ApiRuntime, providers: Any
) -> None:
    workflow_id = client.post("/api/v1/changes", json=hero_body()).json()["workflow_id"]
    service = runtime.live_service(workflow_id)
    arm_for_service(service)
    service.analyze_change()
    service.deploy_change()
    service.deliver_to_frontline()

    failed = client.post(
        f"/api/v1/workflows/{workflow_id}/verify",
        files={"file": ("left.jpg", LEFT_IMG.read_bytes(), "image/jpeg")},
    )
    assert failed.json()["verification_result"] == "FAIL"
    service.generate_proof()

    passed = client.post(
        f"/api/v1/workflows/{workflow_id}/verify",
        files={"file": ("top.jpg", TOP_RIGHT_IMG.read_bytes(), "image/jpeg")},
    )
    assert passed.json()["verification_result"] == "PASS"
    service.generate_proof()

    status = client.get(f"/api/v1/workflows/{workflow_id}").json()
    assert status["state"] == "PROOF_COMPLETE"
    assert status["verification_results"] == ["FAIL", "PASS"], "the FAIL is kept"


# ============================ T129 — frontline minimums ===============================


def test_every_page_declares_a_mobile_viewport() -> None:
    for name in PAGES:
        assert 'name="viewport"' in page(name), name
        assert "width=device-width" in page(name), name


def test_status_is_never_communicated_by_colour_alone() -> None:
    """Spec § Frontline Surface Minimums, item 2."""
    js = script("worker.js")
    # Each status sets a WORD, not only a class.
    assert 'setStatus("pass", "Verified"' in js
    assert 'setStatus(\n        "fail",\n        "Not done yet"' in js or '"Not done yet"' in js
    assert '"Can\'t tell from that photo"' in js
    css = (STATIC / "driftzero.css").read_text(encoding="utf-8")
    assert "Never colour alone" in css


def test_status_regions_are_announced() -> None:
    for name in ("delta", "verify", "workflow", "proof"):
        html = page(name)
        assert 'role="status"' in html, name
        assert 'aria-live="polite"' in html, name


def test_errors_are_announced_as_alerts_and_readable_as_text() -> None:
    """Spec item 5 — critical validation and error feedback readable as text."""
    for name in ("delta", "verify", "workflow", "proof"):
        html = page(name)
        assert 'role="alert"' in html, name
        assert 'id="error-detail"' in html, name


def test_hero_controls_carry_accessible_text_labels() -> None:
    """Spec item 3. Every control says what it does, in words."""
    html = flat(page("verify"))
    assert "> Take or choose a photo </label>" in html
    assert ">Send photo</button>" in html
    assert 'alt="' in html, "the preview image is described"
    assert ">Take or upload photo</a>" in flat(page("delta"))


def test_error_messages_distinguish_network_from_refusal() -> None:
    """'Check your connection' is unhelpful when the server replied 404."""
    js = script("worker.js")
    assert "No connection" in js
    assert "Not found" in js
    assert "error.status === 0" in js


def test_controls_are_keyboard_operable_and_focus_is_visible() -> None:
    """Spec item 6. Real buttons and links, plus a visible focus ring."""
    html = page("verify")
    assert "<button" in html
    assert "onclick=" not in html, "inline handlers on non-interactive elements"
    css = (STATIC / "driftzero.css").read_text(encoding="utf-8")
    assert ":focus-visible" in css
    assert "outline: 3px solid" in css


def test_touch_targets_are_large_enough_for_the_frontline() -> None:
    css = (STATIC / "driftzero.css").read_text(encoding="utf-8")
    tap = re.search(r"--tap:\s*(\d+)px", css)
    assert tap and int(tap.group(1)) >= 48, "touch targets below 48px"
    assert "min-height: var(--tap)" in css


def test_nothing_can_scroll_the_page_sideways() -> None:
    css = (STATIC / "driftzero.css").read_text(encoding="utf-8")
    assert "overflow-x: hidden" in css
    assert "max-width: 100%" in css, "images must not exceed the viewport"
    assert "overflow-x: auto" in css, "wide JSON scrolls inside its own box"


def test_the_worker_surface_uses_no_developer_vocabulary() -> None:
    """A worker should never meet a task id or a milestone name."""
    for name in ("delta", "verify"):
        html = page(name)
        for jargon in ("T080", "T094", "T097", "M0", "M1 ", "Truth Engine", "Crossing",
                       "workflow_id", "JSON", "SYNTHETIC", "idempotency"):
            assert jargon not in html, f"{name}.html exposes {jargon!r}"


# ============================ T130 — workflow and proof ===============================


def test_the_proof_page_explains_before_it_shows_json() -> None:
    html = page("proof")
    assert html.index("What this proves") < html.index("Canonical JSON")
    assert "<details" in html, "the raw JSON is behind an inspector"


def test_the_proof_page_surfaces_the_required_facts() -> None:
    js = script("proof.js")
    for label in (
        "Source change", "Affected work", "Delivery", "Verification",
        "Completed", "Proof id", "Proof content hash",
    ):
        assert f'"{label}"' in js, label


def test_the_hash_wording_is_exact_and_claims_nothing_more() -> None:
    html = page("proof")
    assert "Proof content hash" in html
    assert "excluding its own" in html
    assert "content_hash" in html
    # Every one of these words appears on the page — inside the sentence that DENIES
    # them. Searching the raw text would flag the disclaimer as the overclaim it exists
    # to prevent, so the denials are removed before the search.
    body = flat(html.lower())
    denials = re.findall(r"(?:is not|not a|nor a|never a)[^.]*?\.", body)
    for denial in denials:
        body = body.replace(denial, " ")
    for overclaim in ("digitally signed", "digital signature", "attestation",
                      "non-repudiation", "blockchain", "trusted timestamp", "notarised"):
        assert overclaim not in body, f"{overclaim!r} claimed outside a denial"
    # And the denial itself must actually be present.
    assert "not a digital signature" in flat(html.lower())


def test_the_page_explains_why_the_file_hash_differs() -> None:
    html = page("proof")
    assert "SHA-256 of the whole" in html
    assert "expected to differ" in html


def test_integrity_verification_recomputes_rather_than_asserting() -> None:
    js = script("proof.js")
    assert 'key !== "content_hash"' in js, "the preimage must exclude the hash field"
    assert "sha256Hex" in js
    assert "does NOT match" in js, "a mismatch must be reported as a mismatch"


def test_the_browser_canonicaliser_matches_the_engine_rules() -> None:
    js = script("proof.js")
    body = js[js.index("function canonical(") : js.index("function sha256Hex")]
    assert "Object.keys(value).sort()" in body, "sorted keys"
    assert '","' in body and '":"' in body, "no insignificant whitespace"


def test_the_workflow_view_keeps_every_verification_attempt() -> None:
    html = page("workflow")
    assert "Verification history" in html
    assert "including the ones that failed" in html
    js = script("proof.js")
    assert "state.verification_results" in js


def test_a_workflow_without_a_proof_says_so(client: TestClient) -> None:
    workflow_id = client.post("/api/v1/changes", json=hero_body()).json()["workflow_id"]
    assert client.get(f"/api/v1/workflows/{workflow_id}/proof").status_code == 404
    js = script("proof.js")
    assert "PROOF_NOT_COMPLETE" in js
    assert "No proof yet" in js


def test_the_proof_view_renders_the_real_stored_proof(
    client: TestClient, runtime: ApiRuntime, providers: Any
) -> None:
    from driftzero.models.proof import ChangeProof
    from driftzero.truth_engine.proof_generator import compute_proof_hash

    workflow_id = client.post("/api/v1/changes", json=hero_body()).json()["workflow_id"]
    service = runtime.live_service(workflow_id)
    arm_for_service(service)
    service.analyze_change()
    service.deploy_change()
    service.deliver_to_frontline()
    service.submit_field_evidence(LEFT_IMG.read_bytes())
    service.generate_proof()
    service.submit_field_evidence(TOP_RIGHT_IMG.read_bytes())
    service.generate_proof()

    payload = client.get(f"/api/v1/workflows/{workflow_id}/proof").json()
    proof = ChangeProof.model_validate(payload["document"])
    assert compute_proof_hash(proof) == proof.content_hash
    assert payload["canonical_json"], "the page needs the exact canonical bytes"


# ============================ honesty =================================================


def test_no_page_claims_production_readiness() -> None:
    for name in PAGES:
        html = page(name).lower()
        assert "production ready" not in html
        assert "production-ready" not in html


def test_the_runtime_mode_shown_is_whatever_the_server_reports() -> None:
    js = script("proof.js")
    assert "ready.runtime_mode" in js
    assert '"CLOUD_PILOT"' not in js, "the mode must be read, never hardcoded"


def test_no_page_hardcodes_pilot_identifiers() -> None:
    """Fixture data may contain them; product code may not."""
    for name in PAGES:
        html = page(name)
        for hardcoded in ("DZ-001", "WI-114", "wi-packing-standard-001", "chg-2026"):
            assert hardcoded not in html, f"{name}.html hardcodes {hardcoded!r}"
    for asset in ("worker.js", "proof.js"):
        js = script(asset)
        for hardcoded in ("DZ-001", "WI-114", "wi-packing-standard-001", "chg-2026"):
            assert hardcoded not in js, f"{asset} hardcodes {hardcoded!r}"


def test_no_page_hardcodes_this_pilots_domain() -> None:
    """A headline once read "Packing step updated" — which would tell a second customer
    their change was about packing. Product copy must not name this pilot's industry."""
    # Comments are stripped first: the comment recording *why* this rule exists names
    # the very word it forbids, and searching raw source would flag the explanation.
    for asset in ("worker.js", "proof.js"):
        body = flat(strip_comments(script(asset))).lower()
        for domain in ("packing", "warehouse", "forklift", "shipping label", "pallet"):
            assert domain not in body, f"{asset} hardcodes the pilot domain {domain!r}"
    for name in PAGES:
        body = flat(strip_comments(page(name))).lower()
        for domain in ("packing", "warehouse", "forklift", "pallet"):
            assert domain not in body, f"{name}.html hardcodes {domain!r}"


def test_identifiers_are_humanised_for_the_worker() -> None:
    """A worker should read "Label position", not a variable name."""
    js = script("worker.js")
    assert "function humanise(" in js
    assert "humanise(delta.requirement_id)" in js
    assert 'replace(/[_\-]+/g, " ")' in js


def test_a_disabled_action_does_not_read_as_a_primary_action() -> None:
    css = (STATIC / "driftzero.css").read_text(encoding="utf-8")
    rule = css[css.index(".action[disabled]") : css.index(".action[disabled]") + 300]
    assert "background: transparent" in rule, "a dimmed fill still reads as a button"
    assert "cursor: not-allowed" in rule


def test_the_client_cannot_fabricate_success() -> None:
    """No path renders PROOF_COMPLETE without the server having said so."""
    js = script("worker.js")
    assert 'state.state === "PROOF_COMPLETE"' in js
    # Success is only ever derived from a fetched state object.
    assert "PROOF_COMPLETE" not in js.replace('state.state === "PROOF_COMPLETE"', "")


def test_there_is_no_force_pass_or_complete_control() -> None:
    for name in PAGES:
        html = page(name).lower()
        for danger in ("force pass", "force-pass", "mark complete", "complete workflow",
                       "skip verification", "override"):
            assert danger not in html, f"{name}.html offers {danger!r}"


def test_the_surface_gets_no_privileged_endpoint_of_its_own() -> None:
    """The browser calls the same /api/v1 routes as any other client."""
    for asset in ("worker.js", "proof.js"):
        js = script(asset)
        assert 'var API = "/api/v1"' in js
        assert "/api/hero/" not in js, "the console's demo endpoints are not the product"
