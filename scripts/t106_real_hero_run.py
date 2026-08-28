"""T106 — the real camera-capture hero run, end to end.

Real photographs of a real box, observed by the real Gemma 4 model over Vertex AI MaaS,
adjudicated by the frozen deterministic comparator, persisted in real Firestore:

    LEFT photo       -> Crossing 4 -> FAIL -> proof blocked
    TOP_RIGHT photo  -> Crossing 4 -> PASS -> 7/7 -> PROOF_COMPLETE

Exactly two live inferences — one per photograph. The semantic (Gemini) path is not part
of T106 and is driven by the deterministic substitute, so the only billable calls are the
two field observations this task actually requires.

Requires ``DRIFTZERO_LIVE_MAAS=1``. Records to ``evidence/runs/hero_run_001/``, adding
files beside T101's rather than replacing them.

Run:  DRIFTZERO_LIVE_MAAS=1 python -m scripts.t106_real_hero_run
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

RUN_DIR = REPO_ROOT / "evidence" / "runs" / "hero_run_001"
FIXTURES = REPO_ROOT / "fixtures"
MULTIMODAL = FIXTURES / "multimodal"
HERO_FIXTURE = FIXTURES / "hero_change.json"
LEFT = MULTIMODAL / "label_left_01.jpg"
TOP_RIGHT = MULTIMODAL / "label_top_right_01.jpg"

PROJECT = "driftzero-runtime-2026"


def _require_live() -> None:
    if os.environ.get("DRIFTZERO_LIVE_MAAS") != "1":
        raise SystemExit(
            "refusing to run: set DRIFTZERO_LIVE_MAAS=1 to authorise two billable "
            "Vertex AI MaaS inferences"
        )


def run() -> dict[str, Any]:
    _require_live()

    from driftzero.agents import field_verify as fv
    from driftzero.agents import model_client as mc
    from driftzero.config import DriftZeroConfig
    from driftzero.media.container import sniff_mime_type
    from driftzero.truth_engine.proof_generator import compute_proof_hash
    from driftzero_api.runtime import ApiRuntime
    from driftzero_cloud.composition import FirestoreSink
    from driftzero_cloud.firestore import FirestorePersistence, build_client
    from driftzero_providers.vertex_maas import VertexMaaSGemmaObservationProvider
    from tests.integration._pilot import arm_for_service, clear_change_intelligence

    change_id = f"t106-{uuid.uuid4().hex[:10]}"

    # The real field provider. Only the SEMANTIC path is substituted, because T106 is
    # about physical verification, not change extraction — G1 and M1 already evidenced
    # the Gemini path and repeating it here would spend calls to re-answer that.
    field_config = DriftZeroConfig.from_env(
        {"DRIFTZERO_FIELD_PROVIDER": "vertex_maas", "DRIFTZERO_GCP_PROJECT": PROJECT}
    ).field_provider
    calls: list[dict[str, Any]] = []

    class CountingMaaS(VertexMaaSGemmaObservationProvider):
        """The production adapter, with each call recorded for the evidence."""

        def observe(self, **kwargs: Any) -> Any:
            started = time.perf_counter()
            observation = super().observe(**kwargs)
            calls.append(
                {
                    "sequence": len(calls) + 1,
                    "image_sha256": hashlib.sha256(kwargs["image_bytes"]).hexdigest(),
                    "declared_mime_by_extension": "image/jpeg",
                    "actual_mime_type": kwargs["mime_type"],
                    "raw_output": observation.raw_output,
                    "provider": observation.provider,
                    "model": observation.model,
                    "finish_reason": observation.finish_reason,
                    "prompt_tokens": observation.prompt_tokens,
                    "completion_tokens": getattr(observation, "completion_tokens", None),
                    "latency_seconds": round(time.perf_counter() - started, 3),
                    "request_hash": getattr(observation, "request_hash", None),
                }
            )
            return observation

    os.environ["DRIFTZERO_FIELD_PROVIDER"] = "vertex_maas"
    os.environ["DRIFTZERO_GCP_PROJECT"] = PROJECT
    provider = CountingMaaS(field_config)
    fv.register_field_observation_provider(lambda _c: provider)

    client = build_client(project=PROJECT)
    persistence = FirestorePersistence.over(client)
    runtime = ApiRuntime(
        fixtures_dir=FIXTURES,
        sink=FirestoreSink(persistence),
        persistence=persistence,
        instance_id="t106-operator",
    )

    payload = {
        k: v
        for k, v in json.loads(HERO_FIXTURE.read_text(encoding="utf-8")).items()
        if not k.startswith("_")
    }
    payload["change_id"] = change_id

    accepted = runtime.accept_change(payload)
    workflow_id = accepted["workflow_id"]
    service = runtime.live_service(workflow_id)
    arm_for_service(service)

    service.analyze_change()
    service.deploy_change()
    service.deliver_to_frontline()

    left_bytes = LEFT.read_bytes()
    service.submit_field_evidence(
        left_bytes, declared_filename=LEFT.name, declared_content_type="image/jpeg"
    )
    failed = service.generate_proof()
    after_left = service._session.workflow

    right_bytes = TOP_RIGHT.read_bytes()
    service.submit_field_evidence(
        right_bytes, declared_filename=TOP_RIGHT.name, declared_content_type="image/jpeg"
    )
    passed = service.generate_proof()
    workflow = service._session.workflow

    stored = persistence.proofs.find_workflow(workflow_id)
    ledger = persistence.ledger_for(workflow_id).all_records()
    proof_view = service.get_proof_document() or {}

    result = {
        "task": "T106",
        "evidence_class": "REAL_MAAS_EXECUTION",
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "change_id": change_id,
        "workflow_id": workflow_id,
        "serving_route": {
            "provider": "vertex_ai_maas",
            "model": field_config.model,
            "traffic_type": "ON_DEMAND",
            "project": PROJECT,
            "accelerator": None,
            "persistent_endpoint": None,
            "source": "evidence/g1_gemma_feasibility.json (G1 GO)",
        },
        "fixtures": {
            "left": {
                "path": LEFT.relative_to(REPO_ROOT).as_posix(),
                "sha256": hashlib.sha256(left_bytes).hexdigest(),
                "declared_extension": ".jpg",
                "actual_mime_type": sniff_mime_type(left_bytes),
                "capture_method": "PHYSICAL_CAMERA_CAPTURE",
            },
            "top_right": {
                "path": TOP_RIGHT.relative_to(REPO_ROOT).as_posix(),
                "sha256": hashlib.sha256(right_bytes).hexdigest(),
                "declared_extension": ".jpg",
                "actual_mime_type": sniff_mime_type(right_bytes),
                "capture_method": "PHYSICAL_CAMERA_CAPTURE",
            },
        },
        "inferences": calls,
        "inference_count": len(calls),
        "verification_chronology": [
            {
                "sequence": event.event_sequence,
                "observation": str(event.derived_observation),
                "expected": event.expected_value,
                "result": str(event.verification_result),
                "event_id": event.event_id,
                "submission_id": event.submission_id,
            }
            for event in workflow.verification_events
        ],
        "after_left": {
            "state": str(after_left.state),
            "latest_verification": str(after_left.latest_verification_status),
            "proof_generated": failed["proof"]["generated"],
            "blockers": failed["proof"].get("blockers"),
        },
        "after_top_right": {
            "state": str(workflow.state),
            "latest_verification": str(workflow.latest_verification_status),
            "proof_generated": passed["proof"]["generated"],
            "conditions_satisfied": passed["proof"].get("satisfied_count"),
            "conditions_total": passed["proof"].get("total"),
        },
        "proof": {
            "proof_id": stored.proof_id if stored else None,
            "content_hash": stored.content_hash if stored else None,
            "revalidates": bool(stored) and compute_proof_hash(stored) == stored.content_hash,
            "hash_preimage": proof_view.get("hash_preimage"),
        },
        "durability": {
            "backend": "firestore",
            "project": PROJECT,
            "database": "(default)",
            "ledger_actions": {a.action_id: str(a.status) for a in ledger},
            "remediation_dispatch": service._session.repository.dispatch_count,
            "delivery_dispatch": service._session.channel.dispatch_count,
        },
        "authority": {
            "model_returned": [c["raw_output"] for c in calls],
            "verdict_source": "DRIFTZERO TRUTH ENGINE deterministic comparator",
            "model_set_verdict": False,
            "model_set_workflow_state": False,
            "model_set_proof": False,
            "crossing_4_mandatory": True,
        },
    }

    clear_change_intelligence()
    fv.clear_field_observation_provider()
    mc.clear_model_client_provider()

    result["_cleanup"] = {"client": client, "workflow_id": workflow_id, "change_id": change_id}
    return result


def cleanup(result: dict[str, Any]) -> None:
    """Remove only this run's namespaced documents, after verifying ownership."""
    handle = result.pop("_cleanup")
    client, workflow_id, change_id = (
        handle["client"], handle["workflow_id"], handle["change_id"]
    )
    stored = client.collection("workflows").document(workflow_id).get().to_dict() or {}
    if stored.get("change_id") != change_id:
        print(f"  refusing cleanup: {workflow_id} holds {stored.get('change_id')!r}",
              file=sys.stderr)
        return
    for collection in ("workflows", "workflow_inputs", "resume_snapshots"):
        client.collection(collection).document(workflow_id).delete()
    client.collection("idempotency_keys").document(f"change-{change_id}").delete()
    print(f"  cleaned up {workflow_id}", file=sys.stderr)


def main() -> int:
    result = run()
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    cleanup(result)

    path = RUN_DIR / "real_camera_hero_run.json"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")

    # Rebuild the directory checksums so T101's files stay covered alongside this one.
    files = sorted(p for p in RUN_DIR.iterdir() if p.name != "SHA256SUMS.txt")
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in files]
    with (RUN_DIR / "SHA256SUMS.txt").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")

    print(f"  wrote {path.relative_to(REPO_ROOT).as_posix()}", file=sys.stderr)
    print(json.dumps({
        "change_id": result["change_id"],
        "inferences": result["inference_count"],
        "model_returned": result["authority"]["model_returned"],
        "chronology": [e["result"] for e in result["verification_chronology"]],
        "final_state": result["after_top_right"]["state"],
        "conditions": f"{result['after_top_right']['conditions_satisfied']}"
                      f"/{result['after_top_right']['conditions_total']}",
        "proof_revalidates": result["proof"]["revalidates"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
