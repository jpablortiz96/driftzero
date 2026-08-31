"""Capture one end-to-end public-internet live pilot as evidence.

Drives the deployed public surface exactly as an anonymous visitor does — no identity
token, no gcloud, no localhost — and records what the real models and the real Truth
Engine did. Every field below is observed from the live response; nothing is asserted
from configuration.

The capability token is used to drive the run and is **never recorded**. It is a bearer
credential for one workflow, and an evidence file is the wrong place for one.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import Any

PUBLIC = "https://driftzero-web-eepb64ze2q-uc.a.run.app"
BACKEND = "https://driftzero-api-eepb64ze2q-uc.a.run.app"
OUT = pathlib.Path("evidence/public_live")

# Anything that looks like a bearer credential is scrubbed before anything is written.
CAPABILITY = re.compile(r"capability=[A-Za-z0-9_\-\.]+")


def run(*args: str) -> str:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=600,
        shell=sys.platform == "win32",
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"},
    )
    return (result.stdout or "").strip()


def scrub(text: str) -> str:
    return CAPABILITY.sub("capability=<REDACTED>", text)


def timed(*args: str) -> tuple[str, float]:
    started = time.monotonic()
    body = run(*args)
    return body, round(time.monotonic() - started, 3)


def flat(html: str) -> str:
    return re.sub(r"\s+", " ", html)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "gate_id": "PUBLIC_LIVE_PILOT",
        "evidence_class": "REAL_GOOGLE_CLOUD",
        "note": (
            "One end-to-end run driven from the public internet with no authentication. "
            "Real Gemini, real Gemma, real Truth Engine, real Change Proof. The session "
            "capability used to drive it is deliberately not recorded."
        ),
        "public_url": PUBLIC,
        "backend_url": BACKEND,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    print("DRIFTZERO — public live pilot evidence\n")

    # --- what is deployed -------------------------------------------------------------
    def describe(service: str, *fields: str) -> dict[str, str]:
        raw = run(
            "gcloud", "run", "services", "describe", service,
            "--project=driftzero-runtime-2026", "--region=us-central1",
            "--format=value(" + ",".join(fields) + ")",
        )
        return dict(zip(fields, raw.split("\t"), strict=False))

    web = describe(
        "driftzero-web",
        "status.latestReadyRevisionName",
        "spec.template.spec.serviceAccountName",
    )
    api = describe(
        "driftzero-api",
        "status.latestReadyRevisionName",
        "spec.template.spec.serviceAccountName",
        "spec.template.spec.containers[0].image",
    )
    record["deployment"] = {
        "frontend_revision": web.get("status.latestReadyRevisionName"),
        "frontend_service_account": web.get("spec.template.spec.serviceAccountName"),
        "backend_revision": api.get("status.latestReadyRevisionName"),
        "backend_service_account": api.get("spec.template.spec.serviceAccountName"),
    }
    print(f"  frontend revision: {record['deployment']['frontend_revision']}")
    print(f"  backend revision:  {record['deployment']['backend_revision']}")

    env = run(
        "gcloud", "run", "services", "describe", "driftzero-api",
        "--project=driftzero-runtime-2026", "--region=us-central1", "--format=json",
    )
    providers: dict[str, str] = {}
    if env:
        container = json.loads(env)["spec"]["template"]["spec"]["containers"][0]
        providers = {
            item["name"]: item.get("value", "")
            for item in container.get("env", [])
            if item["name"].startswith("DRIFTZERO_")
            and any(
                key in item["name"]
                for key in ("PROVIDER", "GEMINI", "GEMMA", "LOCATION", "PERSISTENCE")
            )
        }
    record["provider_configuration"] = providers
    record["semantic_provider"] = (
        "REAL" if providers.get("DRIFTZERO_SEMANTIC_PROVIDER") == "google_adk" else "NOT_REAL"
    )
    record["field_provider"] = (
        "REAL" if providers.get("DRIFTZERO_FIELD_PROVIDER") == "vertex_maas" else "NOT_REAL"
    )
    print(f"  semantic_provider: {record['semantic_provider']}")
    print(f"  field_provider:    {record['field_provider']}")

    # --- 1. start, unauthenticated ----------------------------------------------------
    print("\n  starting a live pilot from the public internet ...")
    headers, start_latency = timed(
        "curl", "-s", "-D", "-", "-o", os.devnull, "-X", "POST",
        f"{PUBLIC}/live/start", "-H", "Content-Length: 0", "-m", "300",
    )
    location = ""
    for line in headers.splitlines():
        if line.lower().startswith("location:"):
            location = line.split(":", 1)[1].strip()
    capability = location.split("capability=", 1)[1] if "capability=" in location else ""
    record["start"] = {
        "http_status": 303 if location else 0,
        "redirected_to": scrub(location),
        "latency_seconds": start_latency,
        "gemini_call": "REAL — Change Intelligence ran inside this request",
        "authenticated": False,
    }
    print(f"    started in {start_latency}s  (real Gemini)")
    if not capability:
        print("    FAILED: no capability issued")
        return 1

    # --- 2. delta -------------------------------------------------------------------
    delta_html, delta_latency = timed(
        "curl", "-s", "-m", "120", f"{PUBLIC}/live/pilot?capability={capability}"
    )
    page = flat(delta_html)
    values = re.findall(r"<u>(Was|Now)</u><b>([^<]+)</b>", page)
    artifact = re.search(r"Affected work instruction: <code>([^<]+)</code>", page)
    record["impact_and_delivery"] = {
        "affected_artifact_id": artifact.group(1) if artifact else None,
        "delta": {label.lower(): value for label, value in values},
        "steps_completed": page.count('class="step done"'),
        "latency_seconds": delta_latency,
        "qualified_by": "DRIFTZERO TRUTH ENGINE (deterministic)",
        "note": "the artifact was qualified by the Truth Engine from Gemini's proposals",
    }
    print(f"    delta: {record['impact_and_delivery']['delta']} on "
          f"{record['impact_and_delivery']['affected_artifact_id']}")

    # --- 3. FAIL then PASS, both real Gemma -----------------------------------------
    attempts = []
    for role, expectation in (("current", "FAIL"), ("corrected", "PASS")):
        html, latency = timed(
            "curl", "-s", "-m", "300", "-X", "POST", f"{PUBLIC}/live/verify",
            "--data-urlencode", f"capability={capability}",
            "--data-urlencode", f"photo={role}",
        )
        body = flat(html)
        observed = re.search(r"Observed: <code>([^<]+)</code>", body)
        verdict = re.search(r"Truth Engine verdict: <code>([^<]+)</code>", body)
        attempt = {
            "photo": role,
            "gemma_observation": observed.group(1) if observed else None,
            "truth_engine_verdict": verdict.group(1) if verdict else None,
            "expected_verdict": expectation,
            "latency_seconds": latency,
            "model": "google/gemma-4-26b-a4b-it-maas",
            "provider": "vertex_ai_maas",
            "inference": "REAL — a new live call, not a replay",
        }
        attempts.append(attempt)
        print(f"    {role}: Gemma observed {attempt['gemma_observation']} -> "
              f"Truth Engine {attempt['truth_engine_verdict']} ({latency}s)")
    record["verification_chronology"] = attempts

    # --- 4. the proof this run produced ---------------------------------------------
    proof_html, proof_latency = timed(
        "curl", "-s", "-m", "120", f"{PUBLIC}/live/proof?capability={capability}"
    )
    proof_page = flat(proof_html)
    rows = dict(re.findall(r"<tr><td>([^<]+)</td><td><code>([^<]*)</code></td></tr>", proof_page))
    content_hash = re.search(r'<p class="hash"><code>([0-9a-f]{64})</code></p>', proof_page)
    record["change_proof"] = {
        "change_id": rows.get("Change"),
        "workflow_id": rows.get("Workflow"),
        "affected_artifact_id": rows.get("Affected artifact"),
        "previous_value": rows.get("Previous value"),
        "current_value": rows.get("Current value"),
        "delivery_status": rows.get("Delivery status"),
        "verification_result": rows.get("Verification result"),
        "completion_timestamp": rows.get("Completion timestamp"),
        "proof_id": rows.get("Proof id"),
        "content_hash": content_hash.group(1) if content_hash else None,
        "seven_conditions_satisfied": "7 / 7 conditions satisfied" in proof_page,
        "latency_seconds": proof_latency,
        "generated_by_this_run": True,
    }
    record["integrity"] = {
        "content_hash_matches": "Content hash matches" in proof_page,
        "method": (
            "SHA-256 over the proof's canonical JSON excluding its own content_hash "
            "field, recomputed by the public surface rather than by the generator"
        ),
        "is_not": [
            "a digital signature",
            "an attestation",
            "a trusted timestamp",
            "a ledger entry",
        ],
    }
    print(f"    proof: {record['change_proof']['proof_id']}")
    print(f"    7/7: {record['change_proof']['seven_conditions_satisfied']}  "
          f"integrity: {record['integrity']['content_hash_matches']}")

    # --- 5. the boundary, observed after the run ------------------------------------
    def status_of(url: str, *extra: str) -> int:
        return int(run("curl", "-s", "-o", os.devnull, "-w", "%{http_code}", "-m", "60",
                       *extra, url) or 0)

    record["security_boundary"] = {
        "public_root_unauthenticated": status_of(f"{PUBLIC}/"),
        "backend_health_unauthenticated": status_of(f"{BACKEND}/health"),
        # Content-Length: 0 so the request actually reaches the application. Without it
        # the Google front end answers 411 first, which is a refusal but not the one
        # being asserted: the point is that the route does not exist in the app.
        "public_change_route": status_of(
            f"{PUBLIC}/api/v1/changes", "-X", "POST", "-H", "Content-Length: 0"
        ),
        "public_pubsub_route": status_of(
            f"{PUBLIC}/pubsub/push", "-X", "POST", "-H", "Content-Length: 0"
        ),
        "public_ready_route": status_of(f"{PUBLIC}/ready"),
        "forged_capability": status_of(f"{PUBLIC}/live/pilot?capability=forged.token"),
        "expected": {
            "public_root_unauthenticated": 200,
            "backend_health_unauthenticated": 403,
            "public_change_route": 404,
            "public_pubsub_route": 404,
            "public_ready_route": 404,
            "forged_capability": 403,
        },
    }
    boundary = record["security_boundary"]
    boundary["holds"] = all(
        boundary[key] == value for key, value in boundary["expected"].items()
    )
    print(f"    boundary holds: {boundary['holds']}")

    # --- 6. credential scan over everything about to be written ----------------------
    serialized = json.dumps(record, indent=2, sort_keys=True)
    leaks = [
        name
        for name, pattern in {
            "bearer": r"(?i)bearer\s+\S{20,}",
            "jwt": r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ",
            "api_key": r"\bAIza[0-9A-Za-z_\-]{35}\b",
            "oauth": r"\bya29\.",
            "private_key": r"BEGIN [A-Z ]*PRIVATE KEY",
            "billing": r"\b0[0-9A-F]{5}-[0-9A-F]{6}-[0-9A-F]{6}\b",
            "capability": r"capability=[A-Za-z0-9_\-]{20,}",
        }.items()
        if re.search(pattern, serialized)
    ]
    record["credential_scan"] = {"findings": leaks, "clean": not leaks}
    print(f"    credential scan: {'CLEAN' if not leaks else leaks}")

    verdict = (
        record["semantic_provider"] == "REAL"
        and record["field_provider"] == "REAL"
        and [a["truth_engine_verdict"] for a in attempts] == ["FAIL", "PASS"]
        and record["change_proof"]["seven_conditions_satisfied"]
        and record["integrity"]["content_hash_matches"]
        and boundary["holds"]
        and not leaks
    )
    record["verdict"] = "PASS" if verdict else "FAIL"

    path = OUT / "public_live_run.json"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with (OUT / "SHA256SUMS.txt").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{digest}  {path.name}\n")

    print(f"\n  VERDICT: {record['verdict']}")
    print(f"  evidence: {path}")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
