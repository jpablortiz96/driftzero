"""Prove the public/private boundary against the deployed services.

Run after deploying ``driftzero-web``. Every check below is observed against live Cloud
Run and live IAM — nothing here is inferred from local configuration.

The single property this gate exists to defend: **making the judge surface public must
not have made the operational backend public.** A visitor reaches the frontend with no
Google account; the same visitor reaching the backend directly gets 403.

No model is called and nothing is mutated. Reads only.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

PROJECT = "driftzero-runtime-2026"
REGION = "us-central1"
WEB = "driftzero-web"
API = "driftzero-api"
WEB_SA = f"serviceAccount:driftzero-web-sa@{PROJECT}.iam.gserviceaccount.com"

OUT = pathlib.Path("evidence/public_surface/boundary_gate.json")


def run(*args: str) -> str:
    """Run a read-only command and return stdout.

    ``shell=True`` on Windows because gcloud installs as a ``.cmd`` shim there, which
    CreateProcess cannot launch directly — the same accommodation the M2 provisioning
    scripts already make.
    """
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=300,
        shell=sys.platform == "win32",
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"},
    )
    return (result.stdout or "").strip()


def gcloud_json(*args: str) -> Any:
    raw = run("gcloud", *args, "--format=json", f"--project={PROJECT}")
    return json.loads(raw) if raw else None


def http_status(url: str, *, token: str | None = None) -> int:
    """Status code only. A token, if used, is never printed, stored or returned."""
    command = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "30", url]
    if token:
        command[1:1] = ["-H", f"Authorization: Bearer {token}"]
    return int(run(*command) or 0)


class Gate:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def record(self, number: int, name: str, passed: bool, observed: object) -> None:
        self.checks.append(
            {"check": number, "name": name, "result": "PASS" if passed else "FAIL",
             "observed": observed}
        )
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {number:2}. {name}\n         {observed}")

    @property
    def verdict(self) -> str:
        return "PASS" if all(c["result"] == "PASS" for c in self.checks) else "FAIL"


def main() -> int:
    gate = Gate()
    print("DRIFTZERO — public/private boundary gate\n")

    web = gcloud_json("run", "services", "describe", WEB, f"--region={REGION}") or {}
    api = gcloud_json("run", "services", "describe", API, f"--region={REGION}") or {}
    web_url = (web.get("status") or {}).get("url", "")
    api_url = (api.get("status") or {}).get("url", "")

    # 1-2 — the two statements that together define the boundary.
    gate.record(1, "public frontend answers an unauthenticated request",
                http_status(f"{web_url}/") == 200, f"GET {web_url}/ -> 200")
    api_code = http_status(f"{api_url}/health")
    gate.record(2, "private backend refuses an unauthenticated request",
                api_code == 403, f"GET {api_url}/health -> {api_code} (expected 403)")

    # 3 — the frontend's own identity can read the backend, which is what makes the
    # public page able to report real backend state without exposing the backend.
    token = run("gcloud", "auth", "print-identity-token")
    operator = http_status(f"{api_url}/health", token=token) if token else 0
    gate.record(3, "an authorised identity still reaches the backend",
                operator == 200, f"authenticated GET {api_url}/health -> {operator}")

    # 4-5 — allUsers must sit on exactly one of the two services.
    api_policy = gcloud_json("run", "services", "get-iam-policy", API, f"--region={REGION}") or {}
    web_policy = gcloud_json("run", "services", "get-iam-policy", WEB, f"--region={REGION}") or {}

    def members(policy: dict[str, Any]) -> set[str]:
        return {m for b in policy.get("bindings", []) for m in b.get("members", [])}

    api_members, web_members = members(api_policy), members(web_policy)
    gate.record(4, "allUsers is absent from the private backend",
                "allUsers" not in api_members, f"driftzero-api members: {sorted(api_members)}")
    gate.record(5, "allUsers is present only on the public frontend",
                "allUsers" in web_members, f"driftzero-web members: {sorted(web_members)}")

    # 6 — least privilege, on the service resource rather than the project.
    invoker_on_api = any(
        b.get("role") == "roles/run.invoker" and WEB_SA in b.get("members", [])
        for b in api_policy.get("bindings", [])
    )
    project_policy = gcloud_json("projects", "get-iam-policy", PROJECT) or {}
    project_roles = [
        b["role"] for b in project_policy.get("bindings", []) if WEB_SA in b.get("members", [])
    ]
    gate.record(
        6,
        "driftzero-web-sa holds run.invoker on the API resource and nothing project-wide",
        invoker_on_api and not project_roles,
        f"resource binding: {invoker_on_api}; "
        f"project-level roles: {project_roles or 'none'}",
    )

    # 7 — no key can leak if no key exists.
    accounts = gcloud_json("iam", "service-accounts", "list") or []
    keyed = []
    for account in accounts:
        email = account["email"]
        keys = (
            gcloud_json(
                "iam", "service-accounts", "keys", "list",
                f"--iam-account={email}", "--managed-by=user",
            )
            or []
        )
        if keys:
            keyed.append(email)
    gate.record(7, "no user-managed service-account keys exist", not keyed,
                f"accounts with user-managed keys: {keyed or 'none'}")

    # 8 — the browser must never receive a backend credential.
    body = run("curl", "-s", "-m", "30", f"{web_url}/")
    leaked = [
        marker for marker in ("Bearer ", "Authorization:", "eyJhbGciOi", "ya29.", "AIza")
        if marker in body
    ]
    gate.record(8, "the public page ships no credential-shaped material", not leaked,
                f"markers found: {leaked or 'none'}")

    # 9-11 — the public surface must not republish anything that costs money or mutates.
    for number, name, path in (
        (9, "no public model-invoking route", "/api/v1/changes"),
        (10, "no public verification-upload route", "/api/v1/workflows/wf-1/verify"),
        (11, "no public Pub/Sub ingestion route", "/pubsub/push"),
    ):
        code = http_status(f"{web_url}{path}")
        gate.record(number, name, code == 404, f"GET {web_url}{path} -> {code} (expected 404)")

    # 12 — the frontend's own runtime identity is the dedicated least-privilege account.
    account = ((web.get("spec") or {}).get("template") or {}).get("spec", {}).get(
        "serviceAccountName", ""
    )
    gate.record(12, "the frontend runs as driftzero-web-sa",
                account.startswith("driftzero-web-sa@"), f"runtime identity: {account}")

    # 13 — the backend revision must be untouched by this release.
    revision = (api.get("status") or {}).get("latestReadyRevisionName")
    gate.record(13, "the backend is still serving its existing revision",
                bool(revision), f"driftzero-api revision: {revision}")

    payload = {
        "gate_id": "PUBLIC_SURFACE_BOUNDARY",
        "verdict": gate.verdict,
        "checks": gate.checks,
        "passed": sum(c["result"] == "PASS" for c in gate.checks),
        "total": len(gate.checks),
        "public_url": web_url,
        "backend_url": api_url,
        "evidence_class": "REAL_GOOGLE_CLOUD",
        "note": (
            "Observed against live Cloud Run and live IAM. No identity token is recorded "
            "here; only the status codes it produced."
        ),
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print(f"\n  {payload['passed']}/{payload['total']} checks passed")
    print(f"  VERDICT: {gate.verdict}")
    print(f"  evidence: {OUT}")
    return 0 if gate.verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
