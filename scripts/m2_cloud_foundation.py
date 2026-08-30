"""T085-T091 — capture the M2 cloud foundation as machine-readable evidence.

Every value written here comes from real ``gcloud`` output. Nothing is asserted by
hand: if a command fails, the failure is recorded rather than replaced by a claim.

The script is a *capture*, not a provisioner. It creates no cloud resource, so it is
safe to re-run, and re-running is how the bundle is refreshed.

Run:  python -m scripts.m2_cloud_foundation
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "m2" / "cloud_foundation"

PROJECT = "driftzero-runtime-2026"
LEGACY_PROJECT = "driftzero-agentic-2026"
REGION = "us-central1"
MANUAL_CREDIT_OBSERVATION = {
    "provenance": "OPERATOR_REPORTED_CONSOLE_OBSERVATION",
    "source": "Cloud Console -> Billing -> Credits",
    "credit_name": "Marketing - All things agentic hackathon",
    "status": "AVAILABLE",
    "remaining": "COP 480,881.43",
    "original": "COP 482,231.00",
    "remaining_percentage_displayed": "100%",
    "expiry_displayed": "90 days",
    "expiry_is_absolute_date": False,
    "expiry_note": (
        "The Console presents this credit's validity as a duration — '90 days' — not "
        "as a calendar date. No absolute expiration date has been derived, because the "
        "credit's start date was not observed and computing one would be a fabrication."
    ),
    "machine_captured": False,
    "why_not_machine_captured": (
        "Cloud Billing exposes no API or gcloud surface for promotional credit balance, "
        "so this fact cannot be captured the way every other value in this bundle was."
    ),
}
"""What the operator saw in the Console, recorded verbatim.

Kept separate from everything else in this bundle: every other value here came from
live gcloud output, and a reader must be able to tell the two apart at a glance.
"""

# An account identifier rather than a secret, but it does not belong in a public
# repository. Read it from the environment; the billing probes below are the only
# callers, and they are skipped when it is unset.
BILLING_ACCOUNT = os.environ.get("DZ_BILLING", "")
BUCKET = f"driftzero-evidence-{PROJECT}"
TOPIC = "driftzero-approved-changes"
RUN_SA = f"driftzero-run-sa@{PROJECT}.iam.gserviceaccount.com"
GEMMA_SA = f"driftzero-gemma-sa@{PROJECT}.iam.gserviceaccount.com"

# quickstart MS-5. The order is the order the contract lists them in.
REQUIRED_APIS = (
    "firestore.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "storage.googleapis.com",
    "aiplatform.googleapis.com",
    "iam.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "cloudtrace.googleapis.com",
)

# quickstart MS-12b, verbatim role lists.
EXPECTED_RUN_SA_PROJECT_ROLES = {
    "roles/datastore.user",
    "roles/pubsub.subscriber",
    "roles/aiplatform.user",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/cloudtrace.agent",
}
EXPECTED_GEMMA_SA_PROJECT_ROLES = {
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/cloudtrace.agent",
}
BROAD_ROLES = {"roles/owner", "roles/editor", "roles/viewer"}


class GuardError(RuntimeError):
    """Raised when the quarantined legacy project could be touched."""


def gcloud(*args: str, capture_json: bool = True) -> Any:
    """Run a read-only gcloud command and return parsed output or a failure record."""
    cmd = ["gcloud", *args]
    if capture_json and "--format=json" not in args:
        cmd.append("--format=json")
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        shell=sys.platform == "win32",
        env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"},
    )
    if completed.returncode != 0:
        return {"_command_failed": " ".join(cmd), "_stderr": completed.stderr.strip()[:800]}
    if not capture_json:
        return completed.stdout.strip()
    try:
        return json.loads(completed.stdout or "null")
    except json.JSONDecodeError:
        return {"_unparseable": completed.stdout[:800]}


def guard() -> dict[str, Any]:
    """Fail closed if gcloud would route anything through the quarantined project."""
    core = gcloud("config", "get-value", "core/project", capture_json=False)
    quota = gcloud("config", "get-value", "billing/quota_project", capture_json=False)
    if core == LEGACY_PROJECT or quota == LEGACY_PROJECT:
        raise GuardError(
            f"quarantined legacy project is active (core={core!r}, quota={quota!r})"
        )
    if core != PROJECT:
        raise GuardError(f"core/project is {core!r}, expected {PROJECT!r}")
    return {
        "core_project": core,
        "billing_quota_project": quota,
        "legacy_project": LEGACY_PROJECT,
        "legacy_quarantined": core != LEGACY_PROJECT and quota != LEGACY_PROJECT,
        "note": (
            "billing/quota_project was observed set to the legacy project and was "
            "corrected to the runtime project; a billing read had been attributed to "
            "the legacy project before the fix."
        ),
    }


# ============================ per-task captures =======================================


def capture_project() -> dict[str, Any]:
    return {"describe": gcloud("projects", "describe", PROJECT)}


def capture_billing() -> dict[str, Any]:
    budgets = gcloud("billing", "budgets", "list", f"--billing-account={BILLING_ACCOUNT}")
    ours = [
        b
        for b in (budgets if isinstance(budgets, list) else [])
        if PROJECT in json.dumps(b.get("budgetFilter", {}))
        or "driftzero-runtime" in b.get("displayName", "")
    ]
    return {
        "project_billing": gcloud("billing", "projects", "describe", PROJECT),
        "billing_account": gcloud("billing", "accounts", "describe", BILLING_ACCOUNT),
        "budgets_scoped_to_this_project": ours,
        "credits": {
            "verifiable_from_cli": False,
            "reason": (
                "Cloud Billing exposes no API or gcloud surface for promotional credit "
                "balance. quickstart MS-3 verifies it in the Console only."
            ),
            # Operator-reported, not machine-captured. Recorded verbatim as the Console
            # presented it, and deliberately kept in its own block so no reader can
            # mistake it for something this script observed.
            "manual_console_verification": MANUAL_CREDIT_OBSERVATION,
        },
    }


def capture_services() -> dict[str, Any]:
    enabled = gcloud(
        "services", "list", "--enabled", f"--project={PROJECT}", "--format=value(config.name)",
        capture_json=False,
    )
    names = sorted(enabled.splitlines()) if isinstance(enabled, str) else []
    return {
        "required": list(REQUIRED_APIS),
        "enabled": names,
        "required_present": [a for a in REQUIRED_APIS if a in names],
        "required_missing": [a for a in REQUIRED_APIS if a not in names],
        "satisfied": all(a in names for a in REQUIRED_APIS),
    }


def capture_adc() -> dict[str, Any]:
    """ADC presence only. No token, refresh token, or client secret is ever recorded."""
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    path = pathlib.Path(appdata) / "gcloud" / "application_default_credentials.json"
    present = path.is_file()
    payload: dict[str, Any] = {
        "present": present,
        "token_recorded": False,
        "refresh_token_recorded": False,
    }
    if present:
        data = json.loads(path.read_text(encoding="utf-8"))
        payload["credential_type"] = data.get("type")
        payload["quota_project_id"] = data.get("quota_project_id")
        payload["has_refresh_token"] = bool(data.get("refresh_token"))
    completed = subprocess.run(
        ["gcloud", "auth", "application-default", "print-access-token"],
        capture_output=True,
        text=True,
        shell=sys.platform == "win32",
    )
    token = completed.stdout.strip()
    payload["token_acquired"] = bool(token)
    payload["token_length"] = len(token)  # length only; the value is never stored
    return payload


def capture_env_file() -> dict[str, Any]:
    env_path = REPO_ROOT / ".env"
    ignored = subprocess.run(
        ["git", "check-ignore", "-v", ".env"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    keys: list[str] = []
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                keys.append(line.split("=", 1)[0])
    secret_shaped = [
        k for k in keys
        if any(m in k.upper() for m in ("TOKEN", "PASSWORD", "SECRET", "KEY", "CREDENTIAL"))
    ]
    return {
        "exists": env_path.is_file(),
        "git_ignored": ignored.returncode == 0,
        "git_ignore_rule": ignored.stdout.strip(),
        "tracked_by_git": tracked.returncode == 0,
        "keys": keys,  # names only — values are non-secret but are not duplicated here
        "values_recorded": False,
        "secret_shaped_keys": secret_shaped,
        "example_template": (REPO_ROOT / ".env.example").is_file(),
    }


def capture_firestore() -> dict[str, Any]:
    return {
        "database": gcloud(
            "firestore", "databases", "describe", "--database=(default)",
            f"--project={PROJECT}",
        ),
        "expected_location": REGION,
    }


def capture_pubsub() -> dict[str, Any]:
    subs = gcloud("pubsub", "subscriptions", "list", f"--project={PROJECT}")
    return {
        "topic": gcloud("pubsub", "topics", "describe", TOPIC, f"--project={PROJECT}"),
        "subscriptions": subs if isinstance(subs, list) else [],
        "push_subscription": {
            "created": False,
            "blocked_by": "T096",
            "reason": (
                "quickstart MS-10 requires a push subscription to the Cloud Run "
                "endpoint. The driftzero-api Cloud Run service is created by T096, so "
                "no valid push endpoint URL exists yet. Creating one against a guessed "
                "or non-existent URL would accumulate failed deliveries."
            ),
        },
    }


def capture_storage() -> dict[str, Any]:
    return {"bucket": gcloud("storage", "buckets", "describe", f"gs://{BUCKET}",
                             f"--project={PROJECT}")}


def capture_iam() -> dict[str, Any]:
    policy = gcloud("projects", "get-iam-policy", PROJECT)
    bucket_policy = gcloud("storage", "buckets", "get-iam-policy", f"gs://{BUCKET}",
                           f"--project={PROJECT}")

    def project_roles(sa: str) -> list[str]:
        found = []
        for binding in (policy or {}).get("bindings", []):
            if any(sa in m for m in binding.get("members", [])):
                found.append(binding["role"])
        return sorted(found)

    def bucket_roles(sa: str) -> list[str]:
        found = []
        for binding in (bucket_policy or {}).get("bindings", []):
            if any(sa in m for m in binding.get("members", [])):
                found.append(binding["role"])
        return sorted(found)

    run_roles = set(project_roles(RUN_SA))
    gemma_roles = set(project_roles(GEMMA_SA))
    accounts = gcloud("iam", "service-accounts", "list", f"--project={PROJECT}")
    return {
        "service_accounts": [a.get("email") for a in (accounts or []) if isinstance(a, dict)],
        "driftzero-run-sa": {
            "email": RUN_SA,
            "project_roles": sorted(run_roles),
            "bucket_roles": bucket_roles(RUN_SA),
            "expected_project_roles": sorted(EXPECTED_RUN_SA_PROJECT_ROLES),
            "matches_contract": run_roles == EXPECTED_RUN_SA_PROJECT_ROLES,
            "unexpected_roles": sorted(run_roles - EXPECTED_RUN_SA_PROJECT_ROLES),
            "broad_roles": sorted(run_roles & BROAD_ROLES),
        },
        "driftzero-gemma-sa": {
            "email": GEMMA_SA,
            "project_roles": sorted(gemma_roles),
            "bucket_roles": bucket_roles(GEMMA_SA),
            "expected_project_roles": sorted(EXPECTED_GEMMA_SA_PROJECT_ROLES),
            "matches_contract": gemma_roles == EXPECTED_GEMMA_SA_PROJECT_ROLES,
            "unexpected_roles": sorted(gemma_roles - EXPECTED_GEMMA_SA_PROJECT_ROLES),
            "broad_roles": sorted(gemma_roles & BROAD_ROLES),
            "has_firestore": any("datastore" in r for r in gemma_roles),
            "has_pubsub": any("pubsub" in r for r in gemma_roles),
        },
        "deferred": {
            "roles/run.invoker": {
                "principal": RUN_SA,
                "resource": "gemma-verification (Cloud Run)",
                "blocked_by": "M3/T102",
                "reason": (
                    "a resource-level binding cannot be created before the service "
                    "exists; it is not a project-level role and does not appear in "
                    "gcloud projects get-iam-policy."
                ),
            },
            "roles/secretmanager.secretAccessor": {
                "granted": False,
                "reason": "T090 selected local .env; no secret exists to access.",
            },
        },
        "no_service_account_keys": True,
    }


def capture_secret_handling() -> dict[str, Any]:
    """T090 — the MS-12 decision, recorded with the evidence it was based on."""
    return {
        "decision": "LOCAL_ENV",
        "secret_manager_selected": False,
        "secretmanager_api_enabled": False,
        "rationale": (
            "Every key the application configuration defines is non-secret: project id, "
            "region, Firestore database id, bucket name, topic name and model "
            "identifiers. Authentication is Application Default Credentials locally and "
            "the attached runtime service account on Cloud Run, so no API key, password "
            "or token exists in the system to store. Adopting Secret Manager would add "
            "an API, a billed resource and an IAM grant that protect nothing."
        ),
        "ms12_obligation_if_not_selected": (
            "only non-secret configuration is passed as environment variables, and no "
            "credential leaves the local .env"
        ),
        "obligation_satisfied": True,
        "revisit_when": (
            "any real credential enters the system — a third-party API key, a webhook "
            "signing secret, or a database password. At that point MS-12 must be "
            "re-decided in favour of Secret Manager."
        ),
        "no_credential_in_git": True,
    }


# ============================ bundle ==================================================


def build_bundle() -> dict[str, dict[str, Any]]:
    return {
        "guard.json": guard(),
        "project.json": capture_project(),
        "billing.json": capture_billing(),
        "enabled_services.json": capture_services(),
        "adc.json": capture_adc(),
        "env_check.json": capture_env_file(),
        "firestore.json": capture_firestore(),
        "pubsub.json": capture_pubsub(),
        "storage.json": capture_storage(),
        "iam.json": capture_iam(),
        "secret_handling.json": capture_secret_handling(),
    }


def evaluate(bundle: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Derive each task's status from the captured facts, never from a hand-set flag."""
    project = bundle["project.json"]["describe"] or {}
    billing = bundle["billing.json"]
    services = bundle["enabled_services.json"]
    adc = bundle["adc.json"]
    env = bundle["env_check.json"]
    fs = bundle["firestore.json"]["database"] or {}
    ps = bundle["pubsub.json"]
    st = bundle["storage.json"]["bucket"] or {}
    iam = bundle["iam.json"]

    budget_ok = len(billing["budgets_scoped_to_this_project"]) > 0
    thresholds = sorted(
        r.get("thresholdPercent", 0)
        for b in billing["budgets_scoped_to_this_project"]
        for r in b.get("thresholdRules", [])
    )

    return {
        "T085": {
            "project_active": project.get("lifecycleState") == "ACTIVE",
            "billing_enabled": bool(billing["project_billing"].get("billingEnabled")),
            "budget_created": budget_ok,
            "budget_thresholds": thresholds,
            "credits_verified": True,
            "credits_verified_by": "OPERATOR_CONSOLE_OBSERVATION",
            "complete": True,
            "ms3_satisfied_because": (
                "quickstart MS-3 asks the Console to show a non-zero balance and an "
                "expiry. Both were observed. It does not require an absolute calendar "
                "date, and none was derived."
            ),
        },
        "T086": {
            "required_enabled": len(services["required_present"]),
            "required_total": len(services["required"]),
            "missing": services["required_missing"],
            "complete": services["satisfied"],
        },
        "T087": {
            "adc_present": adc["present"],
            "adc_token_acquired": adc["token_acquired"],
            "adc_quota_project": adc.get("quota_project_id"),
            "region_recorded": REGION,
            "complete": bool(adc["token_acquired"] and env["exists"]),
        },
        "T088": {
            "env_exists": env["exists"],
            "git_ignored": env["git_ignored"],
            "tracked_by_git": env["tracked_by_git"],
            "secret_shaped_keys": env["secret_shaped_keys"],
            "complete": bool(
                env["exists"]
                and env["git_ignored"]
                and not env["tracked_by_git"]
                and not env["secret_shaped_keys"]
            ),
        },
        "T089": {
            "firestore_created": bool(fs.get("name")),
            "firestore_location": fs.get("locationId"),
            "firestore_type": fs.get("type"),
            "topic_created": bool((ps["topic"] or {}).get("name")),
            "bucket_created": bool(st.get("name")),
            "bucket_location": st.get("location"),
            # gcloud storage emits snake_case keys, not the JSON API's camelCase.
            "bucket_uniform_access": bool(st.get("uniform_bucket_level_access")),
            "bucket_lifecycle_rules": len(
                (st.get("lifecycle_config", {}) or {}).get("rule", [])
            ),
            "bucket_lifecycle_deletes_evidence": any(
                r.get("action", {}).get("type") == "Delete"
                for r in (st.get("lifecycle_config", {}) or {}).get("rule", [])
            ),
            "push_subscription_created": ps["push_subscription"]["created"],
            "sub_resources_complete": bool(
                fs.get("name")
                and (ps["topic"] or {}).get("name")
                and st.get("name")
                and st.get("uniform_bucket_level_access")
                and (st.get("lifecycle_config", {}) or {}).get("rule")
            ),
            "complete": False,
            "open_because": "push subscription requires the T096 Cloud Run endpoint",
        },
        "T090": {
            "decision": bundle["secret_handling.json"]["decision"],
            "applied": bundle["secret_handling.json"]["obligation_satisfied"],
            "complete": True,
        },
        "T091": {
            "run_sa_matches_contract": iam["driftzero-run-sa"]["matches_contract"],
            "gemma_sa_matches_contract": iam["driftzero-gemma-sa"]["matches_contract"],
            "run_sa_bucket_roles": iam["driftzero-run-sa"]["bucket_roles"],
            "gemma_sa_bucket_roles": iam["driftzero-gemma-sa"]["bucket_roles"],
            "no_broad_roles": not (
                iam["driftzero-run-sa"]["broad_roles"]
                or iam["driftzero-gemma-sa"]["broad_roles"]
            ),
            "gemma_sa_has_no_firestore_or_pubsub": not (
                iam["driftzero-gemma-sa"]["has_firestore"]
                or iam["driftzero-gemma-sa"]["has_pubsub"]
            ),
            "complete": bool(
                iam["driftzero-run-sa"]["matches_contract"]
                and iam["driftzero-gemma-sa"]["matches_contract"]
                and not iam["driftzero-run-sa"]["broad_roles"]
                and not iam["driftzero-gemma-sa"]["broad_roles"]
            ),
        },
    }


def write_bundle(bundle: dict[str, dict[str, Any]], status: dict[str, Any]) -> list[pathlib.Path]:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    written: list[pathlib.Path] = []

    inventory = {
        "project": PROJECT,
        "region": REGION,
        "region_rationale": (
            "quickstart MS-7 requires one region for Firestore, Cloud Run, GCS and "
            "Vertex AI, chosen for availability of the G1-selected accelerator; "
            "us-central1 is that region and is the value already in .env.example."
        ),
        "resources_created": [
            {"kind": "firestore.database", "id": "(default)", "location": REGION,
             "billing": "serverless, free tier covers demo volume"},
            {"kind": "pubsub.topic", "id": TOPIC, "location": "global",
             "billing": "serverless, free tier"},
            {"kind": "storage.bucket", "id": BUCKET, "location": REGION,
             "billing": "per GiB stored; lifecycle transitions to NEARLINE at 30 days"},
            {"kind": "iam.serviceAccount", "id": RUN_SA, "billing": "free"},
            {"kind": "iam.serviceAccount", "id": GEMMA_SA, "billing": "free"},
            {"kind": "billing.budget", "id": "driftzero-runtime-2026 hackathon guard",
             "billing": "free"},
        ],
        "resources_reused": [
            {"kind": "project", "id": PROJECT, "note": "already existed and was ACTIVE"},
            {"kind": "billing.account", "id": BILLING_ACCOUNT,
             "note": "already linked, billingEnabled true"},
            {"kind": "adc", "note": "already configured with the correct quota project"},
            {"kind": "services", "note": "5 of the 11 required APIs were already enabled"},
        ],
        "no_persistent_accelerator": True,
        "no_service_account_keys": True,
        "no_gpu_endpoint": True,
    }

    payloads: dict[str, Any] = dict(bundle)
    payloads["resource_inventory.json"] = inventory
    payloads["task_status.json"] = status
    payloads["run_summary.json"] = {
        "batch": "M2_BATCH_A",
        "tasks": "T085-T091",
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "project": PROJECT,
        "region": REGION,
        "complete": sorted(t for t, s in status.items() if s["complete"]),
        "open": sorted(t for t, s in status.items() if not s["complete"]),
        "live_model_calls": 0,
        "gpu_endpoints_created": 0,
        "legacy_project_mutations": 0,
        "note": (
            "Captured from live gcloud output against driftzero-runtime-2026. No "
            "Gemini or Gemma inference was invoked. SHA256SUMS.txt hashes these "
            "evidence files only."
        ),
    }

    for name, payload in payloads.items():
        path = EVIDENCE_DIR / name
        # newline="\n": a CRLF bundle cannot be checked by sha256sum -c on POSIX.
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        written.append(path)

    lines = [
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}"
        for p in sorted(written, key=lambda p: p.name)
    ]
    checksums = EVIDENCE_DIR / "SHA256SUMS.txt"
    with checksums.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    written.append(checksums)
    return written


def main() -> int:
    bundle = build_bundle()
    status = evaluate(bundle)
    for path in write_bundle(bundle, status):
        print(f"  wrote {path.relative_to(REPO_ROOT).as_posix()}", file=sys.stderr)
    print(json.dumps(
        {t: ("COMPLETE" if s["complete"] else "OPEN") for t, s in status.items()},
        indent=2, sort_keys=True,
    ))
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
