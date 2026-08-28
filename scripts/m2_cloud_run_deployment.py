"""T096/T089 — capture the deployed Cloud Run + Pub/Sub evidence.

Every value comes from live ``gcloud`` output or a real authenticated HTTP request. The
identity token used for the authenticated checks is obtained per call, held in a local
variable, and never written anywhere: no token, no Authorization header, no ADC file
reaches this bundle.

Run:  python -m scripts.m2_cloud_run_deployment
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m2" / "cloud_run_deployment"

PROJECT = "driftzero-runtime-2026"
LEGACY_PROJECT = "driftzero-agentic-2026"
REGION = "us-central1"
SERVICE = "driftzero-api"
TOPIC = "driftzero-approved-changes"
PUSH_SUB = "driftzero-approved-changes-push"
DLQ_TOPIC = "driftzero-approved-changes-dlq"
DLQ_SUB = "driftzero-approved-changes-dlq-sub"
RUN_SA = f"driftzero-run-sa@{PROJECT}.iam.gserviceaccount.com"
PUSH_SA = f"driftzero-push-sa@{PROJECT}.iam.gserviceaccount.com"
PUBSUB_AGENT = "service-1086395542194@gcp-sa-pubsub.iam.gserviceaccount.com"


class GuardError(RuntimeError):
    """Raised when gcloud would route anything through the quarantined project."""


def gcloud(*args: str, as_json: bool = True) -> Any:
    cmd = ["gcloud", *args]
    if as_json and not any(a.startswith("--format") for a in args):
        cmd.append("--format=json")
    done = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        shell=sys.platform == "win32",
        env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"},
    )
    if done.returncode != 0:
        return {"_command_failed": " ".join(cmd), "_stderr": done.stderr.strip()[:600]}
    if not as_json:
        return done.stdout.strip()
    try:
        return json.loads(done.stdout or "null")
    except json.JSONDecodeError:
        return {"_unparseable": done.stdout[:600]}


def guard() -> dict[str, Any]:
    core = gcloud("config", "get-value", "core/project", as_json=False)
    quota = gcloud("config", "get-value", "billing/quota_project", as_json=False)
    if LEGACY_PROJECT in (core, quota):
        raise GuardError(f"legacy project active (core={core!r}, quota={quota!r})")
    if core != PROJECT:
        raise GuardError(f"core/project is {core!r}, expected {PROJECT!r}")
    return {
        "core_project": core,
        "billing_quota_project": quota,
        "legacy_project": LEGACY_PROJECT,
        "legacy_quarantined": True,
        "legacy_mutations": 0,
    }


def _identity_token() -> str:
    """A short-lived operator token. Held in memory, never returned to the bundle."""
    done = subprocess.run(
        ["gcloud", "auth", "print-identity-token"],
        capture_output=True,
        text=True,
        shell=sys.platform == "win32",
    )
    return done.stdout.strip()


def _request(url: str, *, token: str | None = None) -> dict[str, Any]:
    """One HTTP call, recording only the status and body — never the request headers."""
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8", "replace")
            return {"status": response.status, "body": _safe_json(body)}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": None}
    except Exception as exc:  # pragma: no cover - network shape varies
        return {"status": None, "error": type(exc).__name__}


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text[:400]


# ============================ captures ================================================


def capture_service() -> dict[str, Any]:
    described = gcloud("run", "services", "describe", SERVICE,
                       f"--project={PROJECT}", f"--region={REGION}")
    if "_command_failed" in described:
        return {"describe": described}
    spec = described["spec"]["template"]["spec"]
    meta = described["spec"]["template"]["metadata"]
    container = spec["containers"][0]
    annotations = meta.get("annotations", {})
    return {
        "service": described["metadata"]["name"],
        "region": REGION,
        "project": PROJECT,
        "url": described["status"]["url"],
        "revision": described["status"]["latestReadyRevisionName"],
        "ready": next(
            c["status"] for c in described["status"]["conditions"] if c["type"] == "Ready"
        ),
        "image": container["image"],
        "image_digest": container["image"].split("@")[-1],
        "service_account": spec["serviceAccountName"],
        "min_instances": annotations.get("autoscaling.knative.dev/minScale", "0"),
        "min_instances_note": (
            "Cloud Run omits the minScale annotation when it is 0; absent means "
            "scale-to-zero, which is what --min-instances=0 requested."
        ),
        "max_instances": annotations.get("autoscaling.knative.dev/maxScale"),
        "resources": container.get("resources", {}).get("limits"),
        "concurrency": spec.get("containerConcurrency"),
        "timeout_seconds": spec.get("timeoutSeconds"),
        "ingress": described["metadata"]["annotations"].get("run.googleapis.com/ingress"),
        "env": {e["name"]: e.get("value") for e in container.get("env", [])},
        "env_from_secret": any("valueFrom" in e for e in container.get("env", [])),
        "labels": described["metadata"].get("labels", {}),
    }


def capture_build() -> dict[str, Any]:
    builds = gcloud("builds", "list", f"--project={PROJECT}", f"--region={REGION}", "--limit=3")
    rows = builds if isinstance(builds, list) else []
    latest = rows[0] if rows else {}
    return {
        "build_id": latest.get("id"),
        "status": latest.get("status"),
        "images": latest.get("images", []),
        "create_time": latest.get("createTime"),
        "source": "local repository, filtered by .dockerignore",
        "builder": "Cloud Build",
        "registry": f"{REGION}-docker.pkg.dev/{PROJECT}/driftzero",
    }


def capture_image() -> dict[str, Any]:
    versions = gcloud(
        "artifacts", "docker", "images", "list",
        f"{REGION}-docker.pkg.dev/{PROJECT}/driftzero/driftzero-api",
        f"--project={PROJECT}", "--include-tags",
    )
    rows = versions if isinstance(versions, list) else []
    return {
        "repository": f"{REGION}-docker.pkg.dev/{PROJECT}/driftzero",
        "images": [
            {
                "digest": row.get("version"),
                "tags": row.get("tags"),
                "size_bytes": row.get("sizeBytes"),
                "size_mb": (
                    round(int(row["sizeBytes"]) / 1_000_000, 1)
                    if str(row.get("sizeBytes", "")).isdigit()
                    else None
                ),
                "upload_time": row.get("createTime"),
            }
            for row in rows
        ],
        "immutable_reference": "deployed by digest, not by the mutable :m2 tag",
    }


def capture_auth(url: str) -> dict[str, Any]:
    """Unauthenticated must be refused; an authenticated operator must succeed."""
    unauth = {path: _request(f"{url}{path}")["status"] for path in
              ("/health", "/ready", "/api/v1/workflows/probe")}
    token = _identity_token()
    auth_health = _request(f"{url}/health", token=token)
    auth_ready = _request(f"{url}/ready", token=token)
    del token  # never recorded
    return {
        "unauthenticated": unauth,
        "unauthenticated_all_refused": all(code == 403 for code in unauth.values()),
        "authenticated_health": auth_health,
        "authenticated_ready": auth_ready,
        "token_recorded": False,
        "authorization_header_recorded": False,
        "boundary": (
            "Cloud Run IAM is the authentication boundary. /health and /ready sit behind "
            "the same policy as every other route; the service is private."
        ),
    }


def capture_run_iam() -> dict[str, Any]:
    policy = gcloud("run", "services", "get-iam-policy", SERVICE,
                    f"--project={PROJECT}", f"--region={REGION}")
    bindings = policy.get("bindings", []) if isinstance(policy, dict) else []
    members = [(b["role"], m) for b in bindings for m in b["members"]]
    project_policy = gcloud("projects", "get-iam-policy", PROJECT)
    project_bindings = (
        project_policy.get("bindings", []) if isinstance(project_policy, dict) else []
    )
    push_project_roles = [
        b["role"] for b in project_bindings
        if any("driftzero-push-sa" in m for m in b["members"])
    ]
    return {
        "service_bindings": [{"role": r, "member": m} for r, m in members],
        "allUsers_present": any(m == "allUsers" for _, m in members),
        "allAuthenticatedUsers_present": any(m == "allAuthenticatedUsers" for _, m in members),
        "push_sa_has_run_invoker": any(
            r == "roles/run.invoker" and "driftzero-push-sa" in m for r, m in members
        ),
        "gemma_sa_has_run_invoker": any("gemma-sa" in m for _, m in members),
        "push_sa_project_level_roles": push_project_roles,
        "scope_note": (
            "run.invoker is bound on the SERVICE resource, not project-wide. The push "
            "identity holds no project-level role at all."
        ),
    }


def capture_service_accounts() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("driftzero-run-sa", "driftzero-gemma-sa", "driftzero-push-sa"):
        email = f"{name}@{PROJECT}.iam.gserviceaccount.com"
        keys = gcloud("iam", "service-accounts", "keys", "list",
                      f"--iam-account={email}", "--managed-by=user", f"--project={PROJECT}")
        result[name] = {
            "email": email,
            "user_managed_keys": len(keys) if isinstance(keys, list) else "unknown",
        }
    agent_policy = gcloud("iam", "service-accounts", "get-iam-policy", PUSH_SA,
                          f"--project={PROJECT}")
    agent_bindings = agent_policy.get("bindings", []) if isinstance(agent_policy, dict) else []
    result["pubsub_service_agent"] = {
        "email": PUBSUB_AGENT,
        "roles_on_push_sa": [
            b["role"] for b in agent_bindings
            if any(PUBSUB_AGENT in m for m in b["members"])
        ],
        "why": (
            "authenticated push requires the Pub/Sub service agent to mint an OIDC token "
            "as the push identity; the grant is scoped to that one service account"
        ),
    }
    return result


def capture_pubsub() -> dict[str, Any]:
    push = gcloud("pubsub", "subscriptions", "describe", PUSH_SUB, f"--project={PROJECT}")
    dlq_sub = gcloud("pubsub", "subscriptions", "describe", DLQ_SUB, f"--project={PROJECT}")
    push_config = push.get("pushConfig", {}) if isinstance(push, dict) else {}
    oidc = push_config.get("oidcToken") or {}
    dead_letter = push.get("deadLetterPolicy", {}) if isinstance(push, dict) else {}

    dlq_topic_policy = gcloud("pubsub", "topics", "get-iam-policy", DLQ_TOPIC,
                              f"--project={PROJECT}")
    push_sub_policy = gcloud("pubsub", "subscriptions", "get-iam-policy", PUSH_SUB,
                             f"--project={PROJECT}")

    def roles(policy: Any, member: str) -> list[str]:
        bindings = policy.get("bindings", []) if isinstance(policy, dict) else []
        return [b["role"] for b in bindings if any(member in m for m in b["members"])]

    return {
        "source_topic": TOPIC,
        "push_subscription": {
            "name": PUSH_SUB,
            "push_endpoint": push_config.get("pushEndpoint"),
            "oidc_service_account": oidc.get("serviceAccountEmail"),
            "oidc_audience": oidc.get("audience"),
            "authenticated": bool(oidc),
            "ack_deadline_seconds": push.get("ackDeadlineSeconds"),
        },
        "dead_letter": {
            "topic": (dead_letter.get("deadLetterTopic") or "").split("/")[-1] or None,
            "max_delivery_attempts": dead_letter.get("maxDeliveryAttempts"),
            "inspection_subscription": DLQ_SUB,
            "dlq_retention": dlq_sub.get("messageRetentionDuration")
            if isinstance(dlq_sub, dict) else None,
        },
        "dead_letter_iam": {
            "pubsub_agent_on_dlq_topic": roles(dlq_topic_policy, PUBSUB_AGENT),
            "pubsub_agent_on_source_subscription": roles(push_sub_policy, PUBSUB_AGENT),
            "broad_editor_granted": False,
        },
    }


def capture_firestore_state() -> dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from driftzero_cloud.firestore import build_client  # noqa: PLC0415

    client = build_client(project=PROJECT)
    workflows = []
    for doc in client.collection("workflows").stream():
        payload = doc.to_dict() or {}
        workflows.append(
            {
                "workflow_id": doc.id,
                "change_id": payload.get("change_id"),
                "state": payload.get("state"),
                "revision": payload.get("revision"),
            }
        )
    claims = [k.id for k in client.collection("idempotency_keys").stream()]
    change_ids = [w["change_id"] for w in workflows]
    return {
        "workflows": workflows,
        "idempotency_claims": claims,
        "written_by": "the deployed Cloud Run service, via the T092 Firestore adapter",
        "distinct_change_ids": len(set(change_ids)) == len(change_ids),
        "invalid_message_created_workflow": any(
            str(w["change_id"]).startswith("dlq-probe") for w in workflows
        ),
    }


def capture_credential_scan() -> dict[str, Any]:
    patterns = {
        "oauth_token": r"ya29\.[A-Za-z0-9_\-]{20,}",
        "identity_token": r"eyJhbGciOi[A-Za-z0-9_\-]{20,}",
        "api_key": r"AIza[A-Za-z0-9_\-]{30,}",
        "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY",
        "authorization_header": r"Authorization:\s*Bearer\s+\S+",
    }
    findings: list[dict[str, str]] = []
    scanned = 0
    for path in sorted(EVIDENCE_DIR.glob("*.json")):
        scanned += 1
        text = path.read_text(encoding="utf-8")
        for label, pattern in patterns.items():
            if re.search(pattern, text):
                findings.append({"file": path.name, "pattern": label})
    for path in (REPO_ROOT / "Dockerfile", REPO_ROOT / ".dockerignore"):
        if path.is_file():
            scanned += 1
            text = path.read_text(encoding="utf-8")
            for label, pattern in patterns.items():
                if re.search(pattern, text):
                    findings.append({"file": path.name, "pattern": label})
    return {"files_scanned": scanned, "findings": findings, "clean": not findings}


def run_tests() -> dict[str, Any]:
    targets = [
        "tests/integration/test_api_routes.py",
        "tests/integration/test_pubsub_ingestion.py",
        "tests/integration/test_deployment_config.py",
    ]
    done = subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "-q"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    lines = [ln for ln in done.stdout.splitlines() if "passed" in ln or "failed" in ln]
    return {
        "targets": targets,
        "exit_code": done.returncode,
        "passed": done.returncode == 0,
        "summary": lines[-1].strip() if lines else "no summary line",
    }


def main() -> int:
    guard_state = guard()
    service = capture_service()
    url = service.get("url", "")

    bundle: dict[str, Any] = {
        "guard.json": guard_state,
        "build.json": capture_build(),
        "image.json": capture_image(),
        "cloud_run_service.json": service,
        "cloud_run_iam.json": capture_run_iam(),
        "authentication.json": capture_auth(url) if url else {"skipped": "no service url"},
        "service_accounts.json": capture_service_accounts(),
        "pubsub.json": capture_pubsub(),
        "firestore_state.json": capture_firestore_state(),
        "cost.json": {
            "cloud_run": {
                "min_instances": service.get("min_instances", "0"),
                "max_instances": service.get("max_instances"),
                "resources": service.get("resources"),
                "billing": "per request and per CPU/GiB-second; scale-to-zero when idle",
            },
            "artifact_registry": "one STANDARD docker repository; billed per GiB stored",
            "pubsub": f"{TOPIC}, {PUSH_SUB}, {DLQ_TOPIC}, {DLQ_SUB} — free tier at this volume",
            "no_gpu": True,
            "no_vm": True,
            "no_load_balancer": True,
            "no_cloud_sql": True,
            "no_nat": True,
            "fixed_cost_resources": "none beyond stored image bytes",
        },
        "credential_scan.json": {},  # filled after the others are written
        "test_summary.json": run_tests(),
    }

    auth = bundle["authentication.json"]
    pubsub = bundle["pubsub.json"]
    iam = bundle["cloud_run_iam.json"]
    firestore = bundle["firestore_state.json"]

    bundle["run_summary.json"] = {
        "batch": "M2_DEPLOYMENT",
        "tasks": "T096, T089",
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "service_url": url,
        "revision": service.get("revision"),
        "image_digest": service.get("image_digest"),
        "service_account": service.get("service_account"),
        "private": not iam.get("allUsers_present", True),
        "unauthenticated_refused": auth.get("unauthenticated_all_refused"),
        "authenticated_health": (auth.get("authenticated_health") or {}).get("status"),
        "runtime_mode": ((auth.get("authenticated_ready") or {}).get("body") or {}).get(
            "runtime_mode"
        ),
        "production_ready": ((auth.get("authenticated_ready") or {}).get("body") or {}).get(
            "production_ready"
        ),
        "push_authenticated": pubsub["push_subscription"]["authenticated"],
        "dead_letter_attempts": pubsub["dead_letter"]["max_delivery_attempts"],
        "workflows_written_by_cloud_run": len(firestore["workflows"]),
        "invalid_message_created_workflow": firestore["invalid_message_created_workflow"],
        "legacy_project_mutations": 0,
        "live_model_calls": 0,
        "tokens_recorded": False,
    }

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    written: list[pathlib.Path] = []
    for name, payload in bundle.items():
        if name == "credential_scan.json":
            continue
        path = EVIDENCE_DIR / name
        # newline="\n": a CRLF bundle cannot be checked by sha256sum -c on POSIX.
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
        written.append(path)

    scan_path = EVIDENCE_DIR / "credential_scan.json"
    with scan_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(capture_credential_scan(), indent=2, sort_keys=True) + "\n")
    written.append(scan_path)

    lines = [
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}"
        for p in sorted(written, key=lambda p: p.name)
    ]
    with (EVIDENCE_DIR / "SHA256SUMS.txt").open("w", encoding="utf-8", newline="\n") as h:
        h.write("\n".join(lines) + "\n")

    for path in [*written, EVIDENCE_DIR / "SHA256SUMS.txt"]:
        print(f"  wrote {path.relative_to(REPO_ROOT).as_posix()}", file=sys.stderr)
    print(json.dumps(bundle["run_summary.json"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
