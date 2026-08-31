"""The public live pilot: what a visitor may do, and what they structurally cannot.

Adding a mutating surface to an anonymous public page is the riskiest thing in this
repository, so most of what follows is negative. The properties asserted here are the
ones that keep "run the real product from a browser" from also meaning "drive our Gemini
bill from a browser" or "submit evidence into somebody else's workflow".
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from driftzero_public import capability as cap
from driftzero_public import live, live_views
from driftzero_public.app import app
from driftzero_public.backend import BackendStatus, PrivateBackend
from driftzero_public.live import LivePilot, UnsupportedEvidence, recompute_content_hash

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        PrivateBackend,
        "health",
        lambda self: BackendStatus(True, "SERVING", "stubbed", "2026-01-01T00:00:00+00:00"),
    )
    return TestClient(app, raise_server_exceptions=False)


# ------------------------------------------------------------------ capabilities


def test_a_capability_round_trips() -> None:
    token = cap.issue("wf-1", "chg-1")
    held = cap.verify(token)
    assert held.workflow_id == "wf-1"
    assert held.change_id == "chg-1"
    assert not held.expired


def test_a_tampered_capability_is_refused() -> None:
    """Editing the payload must not survive, or the workflow id is caller-controlled."""
    token = cap.issue("wf-mine", "chg-1")
    body, signature = token.split(".", 1)
    forged = cap._b64(  # noqa: SLF001
        json.dumps(
            {"v": cap.VERSION, "w": "wf-somebody-else", "c": "x", "iat": 0, "exp": 9_999_999_999},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    with pytest.raises(cap.CapabilityInvalid):
        cap.verify(f"{forged}.{signature}")
    assert body  # the original body is untouched; only the forged one is rejected


@pytest.mark.parametrize(
    "token", ["", "not-a-token", "a.b", "....", "eyJ9.zzzz", "onlyonepart"]
)
def test_malformed_capabilities_are_refused(token: str) -> None:
    with pytest.raises(cap.CapabilityInvalid):
        cap.verify(token)


def test_an_expired_capability_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    token = cap.issue("wf-1", "chg-1", ttl_seconds=1)
    # Capture the real clock first: patching time.time with a lambda that calls
    # time.time() would recurse into the patch.
    later = time.time() + 120
    monkeypatch.setattr(time, "time", lambda: later)
    with pytest.raises(cap.CapabilityInvalid):
        cap.verify(token)


def test_refusals_do_not_say_which_failure_occurred() -> None:
    """A forged token and a malformed one must be indistinguishable to the caller."""
    forged = cap.issue("wf-1", "chg-1").split(".")[0] + ".AAAA"
    with pytest.raises(cap.CapabilityInvalid) as forged_error:
        cap.verify(forged)
    with pytest.raises(cap.CapabilityInvalid) as junk_error:
        cap.verify("junk.junk")
    assert str(forged_error.value) == str(junk_error.value)


def test_a_capability_names_one_workflow_and_carries_no_other_authority() -> None:
    fields = set(cap.Capability.__dataclass_fields__)
    assert fields == {"workflow_id", "change_id", "issued_at", "expires_at"}
    for forbidden in ("verification_result", "workflow_state", "proof_id", "model", "prompt"):
        assert forbidden not in fields


# --------------------------------------------------- the public mutation boundary


def test_no_live_route_accepts_a_workflow_id(client: TestClient) -> None:
    """A caller who guesses a workflow id must have nowhere to put it."""
    import driftzero_public.app as app_module

    for name in ("live_pilot", "live_verify", "live_upload", "live_proof", "live_start"):
        signature = inspect.signature(getattr(app_module, name))
        assert "workflow_id" not in signature.parameters, f"{name} accepts a workflow id"


def test_no_live_route_accepts_a_prompt_or_model_parameter() -> None:
    import driftzero_public.app as app_module

    forbidden = {"prompt", "model", "temperature", "max_tokens", "system", "instruction"}
    for name in ("live_start", "live_verify", "live_upload", "live_pilot", "live_proof"):
        parameters = set(inspect.signature(getattr(app_module, name)).parameters)
        assert not parameters & forbidden, f"{name} exposes model control"


def test_the_source_change_belongs_to_the_server() -> None:
    """A visitor supplies nothing semantic — only the server's fixture reaches Gemini."""
    payload = live.canonical_change("dz-live-test")
    assert payload["change_id"] == "dz-live-test"
    assert payload["source_procedure_id"] == "proc-warehouse-packing"
    assert payload["previous_value"] == "LEFT"
    assert payload["current_value"] == "TOP_RIGHT"
    # Provenance keys are stripped: the API forbids unknown fields.
    assert not [key for key in payload if key.startswith("_")]


def test_start_takes_no_caller_supplied_body(client: TestClient) -> None:
    """Posting a change of one's own must not reach the backend."""
    import driftzero_public.app as app_module

    assert not inspect.signature(app_module.live_start).parameters


def test_each_run_gets_a_distinct_change_id() -> None:
    ids = {live.new_change_id() for _ in range(200)}
    assert len(ids) == 200, "change ids collide, so two visitors could share a workflow"


def test_no_pubsub_or_admin_route_is_published(client: TestClient) -> None:
    for path in ("/pubsub/push", "/api/v1/pubsub/push", "/live/admin", "/ready", "/api/v1/changes"):
        assert client.get(path).status_code == 404
        assert client.post(path).status_code in (404, 405)


def test_the_live_client_exposes_only_enumerated_verbs() -> None:
    """No generic request helper a caller could reach with a path of their own."""
    public = {name for name in dir(LivePilot) if not name.startswith("_")}
    assert public == {
        "start", "advance", "status", "verify", "proof", "ensure_proof", "pilot_photo",
    }


def test_pilot_photo_selects_a_role_not_a_path() -> None:
    for hostile in ("../../etc/passwd", "public.css", "canonical_change.json", "", "hero_run.json"):
        with pytest.raises(UnsupportedEvidence):
            LivePilot.pilot_photo(hostile)
    raw, name, media = LivePilot.pilot_photo("current")
    # The pilot photographs are phone captures: named .jpg, actually HEIC. The returned
    # media type must describe the bytes, not the filename.
    assert media == live.sniff_image(raw)
    assert media.startswith("image/")
    assert name in live.PILOT_PHOTOS.values()


# ------------------------------------------------------------------- evidence


def test_image_type_is_derived_from_bytes_not_claims() -> None:
    assert live.sniff_image(JPEG) == "image/jpeg"
    assert live.sniff_image(PNG) == "image/png"
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32
    assert live.sniff_image(heic) == "image/heic"


@pytest.mark.parametrize(
    "payload",
    [b"", b"not an image at all", b"<?php echo 1; ?>", b"%PDF-1.7 not an image"],
)
def test_non_images_are_refused(payload: bytes) -> None:
    with pytest.raises(UnsupportedEvidence):
        live.sniff_image(payload)


def test_oversized_uploads_are_refused() -> None:
    with pytest.raises(UnsupportedEvidence):
        live.sniff_image(JPEG + b"\x00" * live.MAX_UPLOAD_BYTES)


def test_upload_route_refuses_a_bad_capability_before_reading_bytes(client: TestClient) -> None:
    response = client.post(
        "/live/upload",
        data={"capability": "forged.token"},
        files={"file": ("x.jpg", JPEG, "image/jpeg")},
    )
    assert response.status_code == 403


def test_verify_route_refuses_a_bad_capability(client: TestClient) -> None:
    response = client.post("/live/verify", data={"capability": "nope", "photo": "current"})
    assert response.status_code == 403


def test_pilot_and_proof_refuse_a_bad_capability(client: TestClient) -> None:
    assert client.get("/live/pilot?capability=nope").status_code == 403
    assert client.get("/live/proof?capability=nope").status_code == 403


# ------------------------------------------------------------------ integrity


def test_content_hash_is_recomputed_independently() -> None:
    """The verifier must reimplement the rule, not call the generator."""
    body = {"change_id": "DZ-1", "verification_result": "PASS", "workflow_id": "wf-1"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    document = {**body, "content_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}
    assert recompute_content_hash(document)["matches"] is True


def test_an_altered_proof_fails_the_integrity_check() -> None:
    body = {"change_id": "DZ-1", "verification_result": "PASS"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    document = {**body, "content_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}
    document["verification_result"] = "FAIL"
    assert recompute_content_hash(document)["matches"] is False


def test_a_proof_without_a_hash_does_not_silently_pass() -> None:
    assert recompute_content_hash({"change_id": "DZ-1"})["matches"] is False


# ------------------------------------------------------------------- rendering


def test_the_landing_page_offers_the_run(client: TestClient) -> None:
    body = client.get("/live").text
    assert "Run live pilot" in body
    assert 'action="/live/start"' in body


def test_root_leads_with_the_live_pilot(client: TestClient) -> None:
    """In the hero, the live pilot must be the primary action.

    Compared inside the hero section rather than across the whole document: the nav bar
    links every page, and its ordering is navigation rather than emphasis.
    """
    body = client.get("/").text
    hero = body[body.index('<section class="hero">') : body.index("## The problem")
                if "## The problem" in body else body.index("</section>")]
    assert 'href="/live"' in hero
    assert hero.index('href="/live"') < hero.index('href="/demo"'), (
        "the recorded run is offered before the live pilot in the hero"
    )
    assert "Run live pilot" in hero


def test_recorded_evidence_remains_available(client: TestClient) -> None:
    assert client.get("/demo").status_code == 200
    assert client.get("/proof").status_code == 200


def test_progress_is_rendered_from_backend_state_only() -> None:
    """No stage may tick without the backend having reported it."""
    nothing = live_views._steps({})  # noqa: SLF001
    assert "done" not in nothing
    delivered = live_views._steps(  # noqa: SLF001
        {"analysed": True, "qualified": True, "remediated": True, "delivered": True}
    )
    assert delivered.count("done") == 4


def test_the_live_pages_contain_no_script(client: TestClient) -> None:
    """The strict CSP stays intact: the whole flow is forms and redirects."""
    for path in ("/live", "/"):
        assert "<script" not in client.get(path).text.lower()
    assert "script-src 'none'" in client.get("/live").headers["content-security-policy"]


def test_a_verdict_page_states_the_verdict_in_words_not_only_colour() -> None:
    for result, expected in (("PASS", "Verified"), ("FAIL", "Not done yet"),
                             ("INCONCLUSIVE", "Not readable")):
        markup = live_views.verdict(result, "LEFT", {}, "tok", proof_ready=False)
        assert expected in markup


def test_inconclusive_is_shown_rather_than_hidden() -> None:
    markup = live_views.verdict("INCONCLUSIVE", "INCONCLUSIVE", {}, "tok", proof_ready=False)
    assert "INCONCLUSIVE" in markup
    assert "Verified" not in markup


def test_a_failed_verification_offers_a_retry_not_a_proof() -> None:
    markup = live_views.verdict("FAIL", "LEFT", {}, "tok", proof_ready=False)
    assert "Verify corrected state" in markup
    assert "View Change Proof" not in markup


def test_a_pass_without_a_proof_does_not_offer_one() -> None:
    """PASS is necessary but not sufficient: seven conditions decide."""
    markup = live_views.verdict("PASS", "TOP_RIGHT", {}, "tok", proof_ready=False)
    assert "View Change Proof" not in markup


def test_the_live_proof_page_keeps_the_exact_hash_language() -> None:
    document = {"change_id": "DZ-1", "content_hash": "abc"}
    markup = live_views.live_proof(document, {"matches": True})
    for denied in ("not</b> a digital signature", "not</b> an attestation",
                   "not</b> a trusted timestamp", "not</b> a ledger entry"):
        assert denied in markup


def test_rendered_live_pages_carry_no_credential_shaped_material(client: TestClient) -> None:
    patterns = {
        "bearer": r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}",
        "jwt": r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}",
        "api_key": r"\bAIza[0-9A-Za-z_\-]{35}\b",
        "private_key": r"BEGIN [A-Z ]*PRIVATE KEY",
    }
    for path in ("/live", "/", "/demo"):
        body = client.get(path).text
        for name, pattern in patterns.items():
            assert not re.search(pattern, body), f"{path} leaked {name}"


def test_the_signing_key_never_reaches_a_rendered_page() -> None:
    rendered = inspect.getsource(live_views)
    for leak in ("_signing_key", "DRIFTZERO_SESSION_HMAC", "Authorization", "Bearer"):
        assert leak not in rendered


def test_an_absent_signing_key_does_not_fall_back_to_a_shared_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hard-coded fallback key would be forgeable by anyone reading the source."""
    monkeypatch.delenv(cap.SECRET_ENV, raising=False)
    monkeypatch.setattr(cap, "_EPHEMERAL_KEY", None)
    first = cap._signing_key()  # noqa: SLF001
    monkeypatch.setattr(cap, "_EPHEMERAL_KEY", None)
    second = cap._signing_key()  # noqa: SLF001
    assert first != second, "the ephemeral key is deterministic, so tokens are forgeable"
    assert cap.signing_key_is_durable() is False


def test_the_proof_envelope_is_unwrapped_before_hashing() -> None:
    """Re-hashing the API's wrapper instead of the proof would verify the wrong bytes."""
    document = {"change_id": "DZ-1", "content_hash": "abc"}
    envelope = {"proof_ref": "proof:x", "content_hash": "abc", "document": document}
    assert LivePilot._document(envelope) == document  # noqa: SLF001
    # A bare document (no envelope) must pass through unchanged.
    assert LivePilot._document(document) == document  # noqa: SLF001
