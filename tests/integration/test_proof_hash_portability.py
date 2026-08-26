"""Change Proof hash portability — verification exactly as an external auditor does it.

The real final pilot produced a valid proof whose `content_hash` did not match
`Get-FileHash` over the downloaded file. That was correct behaviour with misleading
wording, and the gap that let it reach a human was here: no test ever reproduced the
hash the way a third party must.

These tests close that gap. The third-party test deliberately **re-implements** the
canonicalisation from the documented rules rather than importing ``compute_proof_hash``,
so a change that breaks external reproducibility fails here even if the internal check
still agrees with itself.

Fully offline. No Gemini, Gemma, or Vertex call.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.agents import field_verify as fv  # noqa: E402
from driftzero.agents import model_client as mc  # noqa: E402
from driftzero.agents.field_verify import ProviderObservation  # noqa: E402
from driftzero.models.proof import ChangeProof  # noqa: E402
from driftzero.proof.store import (  # noqa: E402
    DOWNLOAD_HASH_NOTE,
    HASH_MEANING,
    HASH_PREIMAGE_LABEL,
)
from driftzero.truth_engine.proof_generator import (  # noqa: E402
    ProofValidator,
    canonical_proof_material,
    compute_proof_hash,
)
from driftzero_console import app as app_module  # noqa: E402
from driftzero_console.service import HeroConsoleService  # noqa: E402

from ._pilot import arm_for_service, clear_change_intelligence  # noqa: E402

TOP_RIGHT_IMG = REPO_ROOT / "fixtures" / "multimodal" / "label_top_right_01.jpg"
LIVE_PILOT = REPO_ROOT / "evidence" / "final_live_pilot_2026_08_26"

# The real artifacts from the first end-to-end physical pilot. Pinned so this patch —
# or any later one — cannot quietly rewrite them.
LIVE_PROOF_CONTENT_HASH = (
    "5c66dd80ca882602c7a263cdb6435c66b4462cbc0c24d43ac542511ca95a0c5e"
)
LIVE_PROOF_FILE_SHA256 = (
    "75925f5ecb14d1cfcd1eeee0c3e8f17a8e7d274131e2eb4bc4118a2b91a80af1"
)


# ============================ the independent reference implementation ================


def third_party_content_hash(proof_json_bytes: bytes) -> str:
    """Recompute ``content_hash`` from downloaded bytes, using no DRIFTZERO code.

    This is the algorithm published in ``docs/verifying_a_change_proof.md``, transcribed
    by hand. It must never be replaced by a call to ``compute_proof_hash``: the whole
    point is to detect the day the internal and external answers diverge.
    """
    doc = json.loads(proof_json_bytes.decode("utf-8"))
    material = {key: value for key, value in doc.items() if key != "content_hash"}
    canonical = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class StubGemma:
    name = "stub_gemma"

    def observe(self, **_kwargs: Any) -> ProviderObservation:
        return ProviderObservation(
            raw_output="TOP_RIGHT", provider=self.name, model="stub/gemma"
        )


@pytest.fixture(autouse=True)
def _isolate() -> Any:
    clear_change_intelligence()
    fv.clear_field_observation_provider()
    yield
    clear_change_intelligence()
    fv.clear_field_observation_provider()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("DRIFTZERO_FIELD_PROVIDER", "vertex_maas")
    monkeypatch.setenv("DRIFTZERO_GCP_PROJECT", "driftzero-runtime-2026")
    service = HeroConsoleService()
    monkeypatch.setattr(app_module, "_service", service)
    arm_for_service(service)
    fv.register_field_observation_provider(lambda _c: StubGemma())
    with TestClient(app_module.app) as test_client:
        yield test_client


def drive_to_proof(client: Any) -> dict[str, Any]:
    client.post("/api/hero/analyze")
    client.post("/api/hero/deploy")
    client.post("/api/hero/deliver")
    client.post("/api/hero/field-evidence", content=TOP_RIGHT_IMG.read_bytes())
    body = client.post("/api/hero/proof").json()
    assert body["proof"]["status"] == "PROOF_COMPLETE"
    return body


# ============================ 3. third-party verification =============================


def test_a_third_party_verifies_the_download_from_bytes_alone(client: Any) -> None:
    """Exactly what an external auditor does: download, parse, recompute, compare."""
    body = drive_to_proof(client)
    response = client.get("/api/hero/proof/download")
    assert response.status_code == 200

    downloaded = response.content
    doc = json.loads(downloaded.decode("utf-8"))
    stated = doc["content_hash"]

    recomputed = third_party_content_hash(downloaded)

    assert recomputed == stated
    assert stated == body["proof"]["content_hash"]
    assert stated == response.headers["X-Proof-Content-Hash"]


def test_the_published_recipe_verifies_the_real_pilot_proof() -> None:
    """The doc's algorithm, run against the actual first end-to-end pilot artifact."""
    path = LIVE_PILOT / "change_proof_DZ-001.json"
    if not path.exists():
        pytest.skip("final live pilot evidence is not present in this checkout")

    raw = path.read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    assert doc["content_hash"] == LIVE_PROOF_CONTENT_HASH
    assert third_party_content_hash(raw) == LIVE_PROOF_CONTENT_HASH


def test_the_documented_recipe_matches_the_implementation(client: Any) -> None:
    """The published rules and the frozen code must agree on the preimage, byte for byte."""
    drive_to_proof(client)
    downloaded = client.get("/api/hero/proof/download").content
    proof = ChangeProof.model_validate(json.loads(downloaded.decode("utf-8")))

    # Independent transcription of the four canonicalisation rules.
    material = {
        k: v
        for k, v in json.loads(downloaded.decode("utf-8")).items()
        if k != "content_hash"
    }
    external = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    internal = json.dumps(
        canonical_proof_material(proof),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert external == internal
    assert third_party_content_hash(downloaded) == compute_proof_hash(proof)


def test_the_recipe_is_documented_and_not_called_rfc_8785() -> None:
    doc = (REPO_ROOT / "docs" / "verifying_a_change_proof.md").read_text(encoding="utf-8")
    for rule in ("sort_keys=True", 'separators=(",", ":")', "ensure_ascii=False", "utf-8"):
        assert rule in doc, f"the recipe must state {rule}"
    assert "hashlib.sha256" in doc
    # RFC 8785 conformance is not implemented, so it must not be claimed.
    lowered = doc.lower()
    assert "not rfc 8785" in lowered or "deliberately not described as jcs" in lowered


# ============================ 4. self-exclusion =======================================


def test_content_hash_does_not_contribute_to_its_own_preimage(client: Any) -> None:
    """Changing only ``content_hash`` must not change the recomputed material hash."""
    drive_to_proof(client)
    downloaded = client.get("/api/hero/proof/download").content
    proof = ChangeProof.model_validate(json.loads(downloaded.decode("utf-8")))

    altered = proof.model_copy(update={"content_hash": "f" * 64})
    assert altered.content_hash != proof.content_hash
    # The preimage is identical, so the derived hash is identical.
    assert canonical_proof_material(altered) == canonical_proof_material(proof)
    assert compute_proof_hash(altered) == compute_proof_hash(proof)
    # ...and that shared value is the *original* proof's stated hash, which is why a
    # tampered content_hash is detectable: it no longer equals its own material hash.
    assert compute_proof_hash(altered) == proof.content_hash
    assert altered.content_hash != compute_proof_hash(altered)


def test_the_preimage_omits_exactly_one_field(client: Any) -> None:
    drive_to_proof(client)
    downloaded = client.get("/api/hero/proof/download").content
    doc = json.loads(downloaded.decode("utf-8"))
    proof = ChangeProof.model_validate(doc)

    material = canonical_proof_material(proof)
    assert "content_hash" not in material
    assert set(doc) - set(material) == {"content_hash"}


def test_altering_any_other_field_does_change_the_hash(client: Any) -> None:
    """Self-exclusion must not weaken alteration detection for real content."""
    drive_to_proof(client)
    downloaded = client.get("/api/hero/proof/download").content
    proof = ChangeProof.model_validate(json.loads(downloaded.decode("utf-8")))

    for field, value in (
        ("current_value", "LEFT"),
        ("affected_artifact_id", "WI-999"),
        ("delivery_ref", "forged:receipt"),
    ):
        tampered = proof.model_copy(update={field: value})
        assert compute_proof_hash(tampered) != proof.content_hash, field


# ============================ 5. the expected whole-file difference ===================


def test_the_whole_file_sha256_differs_from_content_hash_by_design(client: Any) -> None:
    """EXPECTED INEQUALITY — do not "fix" this.

    The downloaded file is the complete proof, ``content_hash`` included. The digest is
    taken over the proof *without* that field, because a field cannot contain the hash of
    a document containing it. ``sha256(file) == content_hash`` is therefore impossible,
    and asserting it would be asserting a contradiction.

    What must hold instead is the test above: removing ``content_hash`` and
    canonicalising reproduces the stated digest.
    """
    drive_to_proof(client)
    downloaded = client.get("/api/hero/proof/download").content
    stated = json.loads(downloaded.decode("utf-8"))["content_hash"]

    whole_file = hashlib.sha256(downloaded).hexdigest()
    assert whole_file != stated, "a self-referential hash is arithmetically impossible"
    # The difference is exactly the serialized content_hash member.
    preimage_length = len(
        json.dumps(
            {
                k: v
                for k, v in json.loads(downloaded.decode("utf-8")).items()
                if k != "content_hash"
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    assert len(downloaded) > preimage_length
    assert len(downloaded) - preimage_length == len(',"content_hash":"' + stated + '"')


def test_the_real_pilot_file_shows_the_same_expected_difference() -> None:
    path = LIVE_PILOT / "change_proof_DZ-001.json"
    if not path.exists():
        pytest.skip("final live pilot evidence is not present in this checkout")
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == LIVE_PROOF_FILE_SHA256
    assert LIVE_PROOF_FILE_SHA256 != LIVE_PROOF_CONTENT_HASH
    assert third_party_content_hash(raw) == LIVE_PROOF_CONTENT_HASH


# ============================ 6-8. unchanged semantics ================================


def test_the_proof_validator_still_accepts_a_valid_proof(client: Any) -> None:
    drive_to_proof(client)
    service = app_module.get_service()
    session = service._session
    stored = session.proof_store.find_workflow(session.workflow.workflow_id)
    context = service._proof_context(session)

    result = ProofValidator().validate(stored.proof, context)
    assert result.valid is True
    assert result.failures == ()
    assert result.failed_conditions == ()


def test_replay_audit_hash_verified_is_still_true(client: Any) -> None:
    drive_to_proof(client)
    audit = client.get("/api/hero/proof/replay").json()
    assert audit["hash_verified"] is True
    assert audit["side_effects_executed"] == 0
    # Its wording must scope the claim to the content-hash contract, not the file.
    assert "content_hash" in audit["hash_meaning"]


def test_the_download_still_serves_the_complete_stored_proof(client: Any) -> None:
    body = drive_to_proof(client)
    downloaded = client.get("/api/hero/proof/download").content
    document = client.get("/api/hero/proof").json()

    assert downloaded.decode("utf-8") == document["canonical_json"]
    assert json.loads(downloaded.decode("utf-8")) == document["document"]
    assert "content_hash" in json.loads(downloaded.decode("utf-8"))
    assert body["proof"]["content_hash"] == document["content_hash"]


def test_the_download_names_its_preimage(client: Any) -> None:
    drive_to_proof(client)
    response = client.get("/api/hero/proof/download")
    assert response.headers["X-Proof-Hash-Preimage"] == HASH_PREIMAGE_LABEL
    assert response.headers["X-Proof-Hash-Preimage"] == (
        "canonical-json-excluding-content_hash"
    )
    assert response.headers["X-Proof-Content-Hash"] == json.loads(
        response.content.decode("utf-8")
    )["content_hash"]


# ============================ 9-10. wording ===========================================


def test_no_source_claims_the_downloaded_bytes_were_hashed() -> None:
    """The three statements the audit found false must not come back."""
    banned = (
        "byte-for-byte what the SHA-256 was computed over",
        "The exact bytes the hash was computed over",
        "what a caller downloads is byte-for-byte what was hashed",
        "downloaded file is byte-for-byte what",
    )
    for root in ("driftzero", "driftzero_console", "driftzero_adk", "driftzero_providers"):
        for path in sorted((REPO_ROOT / "src" / root).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for claim in banned:
                assert claim not in source, f"{path.name} still claims: {claim}"


def test_the_hash_meaning_names_the_preimage() -> None:
    assert "excluding its own content_hash" in HASH_MEANING
    assert "canonical JSON" in HASH_MEANING
    assert "identity" in HASH_MEANING.lower()
    assert "content_hash" in DOWNLOAD_HASH_NOTE
    assert "expected to differ" in DOWNLOAD_HASH_NOTE


def test_the_ui_explains_the_difference_and_never_implies_file_hashing() -> None:
    app_js = (REPO_ROOT / "src" / "driftzero_console" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "download_hash_note" in app_js, "the UI must surface the explanation"
    assert "hash_meaning" in app_js
    lowered = app_js.lower()
    for implication in ("get-filehash", "sha256sum", "hash of the file", "file hash"):
        assert implication not in lowered, f"the UI implies {implication}"


def test_no_overclaim_of_cryptographic_authorship() -> None:
    targets = [
        REPO_ROOT / "src" / "driftzero" / "proof" / "store.py",
        REPO_ROOT / "src" / "driftzero_console" / "service.py",
        REPO_ROOT / "src" / "driftzero_console" / "app.py",
        REPO_ROOT / "src" / "driftzero_console" / "static" / "app.js",
        REPO_ROOT / "docs" / "verifying_a_change_proof.md",
    ]
    for path in targets:
        # Markdown emphasis is stripped so "**not** a digital signature" reads as the
        # denial it is rather than as the claim it denies.
        lowered = path.read_text(encoding="utf-8").lower().replace("*", "").replace("_", " ")
        # Sentences, not lines: a denial routinely wraps across a line break, and
        # checking line-by-line would read the tail of "it is not a signature, an
        # attestation, or a blockchain proof" as an assertion.
        sentences = [" ".join(part.split()) for part in lowered.split(". ")]
        for overclaim in ("digital signature", "blockchain", "non-repudiation", "notari"):
            # These may be *denied* anywhere; they may never be asserted.
            for sentence in sentences:
                if overclaim in sentence:
                    assert any(
                        deny in sentence
                        for deny in ("not ", "never", "nothing", "no ", "neither")
                    ), f"{path.name} asserts {overclaim}: {sentence.strip()}"


# ============================ 11-12. hygiene ==========================================


def test_no_live_provider_is_reachable_from_this_suite() -> None:
    clear_change_intelligence()
    fv.clear_field_observation_provider()
    assert mc.has_model_client_provider() is False
    assert fv.has_field_observation_provider() is False


def test_the_final_live_evidence_is_byte_identical() -> None:
    """This patch must not rewrite the first end-to-end pilot's artifacts."""
    if not LIVE_PILOT.exists():
        pytest.skip("final live pilot evidence is not present in this checkout")
    expected = {
        "change_proof_DZ-001.json": LIVE_PROOF_FILE_SHA256,
        "change_proof_DZ-001-api.json": LIVE_PROOF_FILE_SHA256,
    }
    for name, digest in expected.items():
        path = LIVE_PILOT / name
        assert path.exists(), f"{name} is missing"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
