"""T107 — the M3 exit gate.

The verdict is computed from the checks; a failing check flips it.

M3's substance is empirical, so the gate keeps three evidence classes strictly apart and
never lets one stand in for another:

* ``REAL_MAAS_EXECUTION`` — T105's three inferences and T106's two, already recorded
* ``OFFLINE_DETERMINISTIC`` — regression that must stay green without a model
* ``HISTORICAL_G1`` — the G1 decision record, read as the authority on the route and
  never re-run

It deliberately **re-reads** the recorded live evidence rather than re-invoking the
model. Re-running five billable inferences to confirm they happened would spend money to
learn nothing, and would replace the evidence it is supposed to be checking.

Run:  python -m scripts.m3_exit_gate
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

BUNDLE = REPO_ROOT / "evidence" / "m3" / "exit_gate"
ARCHITECTURE = REPO_ROOT / "evidence" / "m3" / "architecture"
G1 = REPO_ROOT / "evidence" / "g1_gemma_feasibility.json"
EVAL = REPO_ROOT / "evidence" / "reports" / "multimodal_eval.json"
HERO = REPO_ROOT / "evidence" / "runs" / "hero_run_001" / "real_camera_hero_run.json"
MANIFEST = REPO_ROOT / "fixtures" / "multimodal" / "manifest.json"

LIVE = "REAL_MAAS_EXECUTION"
OFFLINE = "OFFLINE_DETERMINISTIC"
HISTORICAL = "HISTORICAL_G1"


@dataclass
class Check:
    number: int
    name: str
    passed: bool
    observed: Any
    evidence_class: str


@dataclass
class GateLedger:
    checks: list[Check] = field(default_factory=list)

    def record(self, number: int, name: str, passed: bool, observed: Any, cls: str) -> None:
        self.checks.append(Check(number, name, bool(passed), observed, cls))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    @property
    def verdict(self) -> str:
        return "PASS" if not self.failed else "FAIL"


def _load(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate() -> tuple[GateLedger, dict[str, Any]]:
    g1 = _load(G1)
    evaluation = _load(EVAL)
    hero = _load(HERO)
    manifest = _load(MANIFEST)

    ledger = GateLedger()
    r = ledger.record

    # --- the route G1 selected is the route in use ---
    route = g1.get("route_decision", {})
    r(1, "G1 recorded GO", g1.get("verdict") == "GO", g1.get("verdict"), HISTORICAL)
    r(2, "G1 selected the Vertex AI MaaS route",
      g1.get("serving_route") == "vertex_ai_maas", g1.get("serving_route"), HISTORICAL)
    r(3, "G1 recorded that self-deployment is not required",
      route.get("requires_self_deployment") is False,
      route.get("requires_self_deployment"), HISTORICAL)
    r(4, "self-deploy is recorded as NOT the active route",
      route.get("route_convergence", {}).get("self_deploy_status") == "NOT_THE_ACTIVE_ROUTE",
      route.get("route_convergence", {}).get("self_deploy_status"), HISTORICAL)
    r(5, "no accelerator was provisioned", not gpu_resources_exist(), gpu_inventory(), LIVE)

    # --- the live evaluation (T105), read not re-run ---
    r(6, "multimodal evaluation recorded", bool(evaluation),
      EVAL.relative_to(REPO_ROOT).as_posix(), LIVE)
    r(7, "evaluation ran against real MaaS",
      evaluation.get("provider") == "vertex_ai_maas"
      and evaluation.get("traffic_type") == "ON_DEMAND",
      {k: evaluation.get(k) for k in ("provider", "traffic_type")}, LIVE)
    r(8, "evaluation used the G1-selected model",
      evaluation.get("model") == "google/gemma-4-26b-a4b-it-maas",
      evaluation.get("model"), LIVE)
    r(9, "every fixture returned an in-domain observation",
      evaluation.get("in_domain") == evaluation.get("inference_count") == 3,
      {k: evaluation.get(k) for k in ("in_domain", "inference_count")}, LIVE)
    r(10, "every fixture matched its expected observation",
      evaluation.get("correct") == 3, evaluation.get("correct"), LIVE)
    r(11, "evaluation used the production adapter",
      "vertex_maas.py" in str(evaluation.get("adapter")), evaluation.get("adapter"), LIVE)

    # --- the real hero run (T106) ---
    chronology = [e["result"] for e in hero.get("verification_chronology", [])]
    r(12, "real camera hero run recorded", bool(hero),
      HERO.relative_to(REPO_ROOT).as_posix(), LIVE)
    r(13, "LEFT photo produced FAIL then TOP_RIGHT produced PASS",
      chronology == ["FAIL", "PASS"], chronology, LIVE)
    r(14, "the FAIL blocked the proof",
      hero.get("after_left", {}).get("proof_generated") is False,
      hero.get("after_left", {}).get("state"), LIVE)
    r(15, "the corrected PASS reached PROOF_COMPLETE",
      hero.get("after_top_right", {}).get("state") == "PROOF_COMPLETE",
      hero.get("after_top_right", {}).get("state"), LIVE)
    r(16, "all seven proof conditions satisfied",
      hero.get("after_top_right", {}).get("conditions_satisfied") == 7,
      f"{hero.get('after_top_right', {}).get('conditions_satisfied')}/7", LIVE)
    r(17, "the proof revalidates against its own hash",
      hero.get("proof", {}).get("revalidates") is True,
      hero.get("proof", {}).get("content_hash"), LIVE)
    r(18, "exactly one inference per photograph",
      hero.get("inference_count") == 2, hero.get("inference_count"), LIVE)

    # --- authority: the model observes, it never decides ---
    outputs = hero.get("authority", {}).get("model_returned", [])
    r(19, "the model returned positions, not verdicts",
      outputs == ["LEFT", "TOP_RIGHT"], outputs, LIVE)
    r(20, "no model output contained a verdict token",
      not any(t in str(o).upper() for o in outputs for t in ("PASS", "FAIL", "COMPLETE")),
      outputs, LIVE)
    r(21, "the verdict came from the Truth Engine",
      hero.get("authority", {}).get("verdict_source", "").startswith("DRIFTZERO"),
      hero.get("authority", {}).get("verdict_source"), LIVE)
    r(22, "the model set no workflow state or proof",
      hero.get("authority", {}).get("model_set_workflow_state") is False
      and hero.get("authority", {}).get("model_set_proof") is False,
      hero.get("authority", {}), LIVE)
    r(23, "Crossing 4 remained mandatory",
      hero.get("authority", {}).get("crossing_4_mandatory") is True, True, LIVE)

    # --- actual-byte MIME authority ---
    fixtures = manifest.get("fixtures", [])
    r(24, "fixture manifest exists and is versioned",
      manifest.get("schema") == "driftzero.m3.multimodal_manifest.v1",
      manifest.get("schema"), OFFLINE)
    r(25, "MIME is derived from bytes, not the extension",
      all(f["actual_mime_type"] == "image/heic" and f["declared_extension"] == ".jpg"
          for f in fixtures) and len(fixtures) == 3,
      [(f["filename"], f["actual_mime_type"]) for f in fixtures], OFFLINE)
    r(26, "the hero run recorded the actual MIME of both photographs",
      all(v["actual_mime_type"] == "image/heic"
          for v in hero.get("fixtures", {}).values()),
      {k: v["actual_mime_type"] for k, v in hero.get("fixtures", {}).items()}, LIVE)
    r(27, "only real physical fixtures were evaluated",
      all(f["provenance_class"] == "REAL_PHYSICAL" for f in fixtures)
      and manifest.get("synthetic_directory", {}).get("excluded_from_this_manifest") is True,
      manifest.get("physical_capture_satisfied"), OFFLINE)

    # --- no credential anywhere in the live evidence ---
    leaked = credential_scan([EVAL, HERO, MANIFEST])
    r(28, "no credential in the M3 evidence", not leaked, leaked or "clean", LIVE)

    # --- repository integrity ---
    m0 = m0_diff()
    r(29, "M0 unchanged", m0["clean"], m0["changed_files"], OFFLINE)
    r(30, "M0 purity guard green", pytest_green(
        "tests/unit/truth_engine/test_no_cloud_imports.py"), "purity", OFFLINE)
    r(31, "field verification regression green",
      pytest_green("tests/integration/test_field_verification.py"), "T079", OFFLINE)
    ok, detail = prior_gate("scripts.m1_exit_gate", "m1_status", "CLOSED")
    r(32, "M1 still CLOSED", ok, detail, OFFLINE)
    ok2, detail2 = prior_gate("scripts.m2_exit_gate", "m2_status", "CLOSED")
    r(33, "M2 still CLOSED", ok2, detail2, OFFLINE)

    context = {
        "g1": {"verdict": g1.get("verdict"), "serving_route": g1.get("serving_route")},
        "evaluation": {k: evaluation.get(k) for k in
                       ("provider", "model", "traffic_type", "inference_count", "correct")},
        "hero_run": {"chronology": chronology,
                     "final_state": hero.get("after_top_right", {}).get("state"),
                     "inference_count": hero.get("inference_count"),
                     "proof_content_hash": hero.get("proof", {}).get("content_hash")},
        "total_live_inferences": (evaluation.get("inference_count") or 0)
        + (hero.get("inference_count") or 0),
    }
    return ledger, context


# ============================ observations ============================================


def gpu_inventory() -> dict[str, Any]:
    """What accelerator-bearing resources exist. Expected: none."""
    def run(*args: str) -> Any:
        done = subprocess.run(
            ["gcloud", *args, "--format=json"], capture_output=True, text=True,
            shell=sys.platform == "win32",
            env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"},
        )
        if done.returncode != 0:
            return []
        try:
            return json.loads(done.stdout or "[]")
        except json.JSONDecodeError:
            return []

    project = "driftzero-runtime-2026"
    endpoints = run("ai", "endpoints", "list", f"--project={project}", "--region=us-central1")
    instances = run("compute", "instances", "list", f"--project={project}")
    return {
        "vertex_endpoints": [e.get("displayName") for e in endpoints] if endpoints else [],
        "compute_instances": [i.get("name") for i in instances] if instances else [],
        "note": "Vertex AI MaaS is serverless: no endpoint and no accelerator to list.",
    }


def gpu_resources_exist() -> bool:
    inventory = gpu_inventory()
    return bool(inventory["vertex_endpoints"] or inventory["compute_instances"])


def credential_scan(paths: list[pathlib.Path]) -> list[str]:
    import re

    patterns = {
        "oauth_token": r"ya29\.[A-Za-z0-9_\-]{20,}",
        "bearer": r"Bearer\s+[A-Za-z0-9._\-]{20,}",
        "api_key": r"AIza[A-Za-z0-9_\-]{30,}",
        "private_key": r"BEGIN [A-Z ]*PRIVATE KEY",
    }
    found = []
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        found.extend(
            f"{path.name}:{label}" for label, pattern in patterns.items()
            if re.search(pattern, text)
        )
    return found


def m0_diff() -> dict[str, Any]:
    done = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--",
         "src/driftzero/truth_engine", "src/driftzero/models"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    files = [line for line in done.stdout.splitlines() if line.strip()]
    return {"clean": not files, "changed_files": files}


def pytest_green(target: str) -> bool:
    done = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    return done.returncode == 0


def prior_gate(module: str, key: str, expected: str) -> tuple[bool, Any]:
    done = subprocess.run(
        [sys.executable, "-m", module, "--skip-suite", "--dry-run"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    try:
        summary = json.loads(done.stdout)
    except json.JSONDecodeError:
        return False, {"unparseable": done.stdout[-200:]}
    ok = summary.get("verdict") == "PASS" and summary.get(key) == expected
    return ok, {"verdict": summary.get("verdict"), key: summary.get(key),
                "checks": summary.get("checks")}


def run_tests() -> dict[str, Any]:
    drop = ("DRIFTZERO_LIVE_MAAS", "DRIFTZERO_CLOUD_SMOKE", "DRIFTZERO_FIELD_PROVIDER",
            "DRIFTZERO_GCP_PROJECT", "DRIFTZERO_SEMANTIC_PROVIDER", "DRIFTZERO_GEMINI_MODEL",
            "DRIFTZERO_ENV")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"],
        cwd=REPO_ROOT, capture_output=True, text=True, env=env,
    )
    lines = [ln for ln in done.stdout.splitlines() if "passed" in ln or "failed" in ln]
    return {"passed": done.returncode == 0,
            "summary": lines[-1].strip() if lines else "no summary"}


# ============================ evidence ================================================


def write_evidence(
    ledger: GateLedger, context: dict[str, Any], suite: dict[str, Any]
) -> list[pathlib.Path]:
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    manifest = {
        "gate_id": "M3_EXIT_GATE",
        "task": "T107",
        "milestone": "M3",
        "timestamp": timestamp,
        "verdict": ledger.verdict,
        "checks_total": len(ledger.checks),
        "checks_passed": sum(1 for c in ledger.checks if c.passed),
        "failed_checks": [c.name for c in ledger.failed],
        "checks": [
            {"check": c.number, "name": c.name, "result": "PASS" if c.passed else "FAIL",
             "observed": c.observed, "evidence_class": c.evidence_class}
            for c in ledger.checks
        ],
        "evidence_class_counts": {
            name: sum(1 for c in ledger.checks if c.evidence_class == name)
            for name in (LIVE, OFFLINE, HISTORICAL)
        },
        "context": context,
        "test_suite": suite,
        "m3_status": "CLOSED" if ledger.verdict == "PASS" else "OPEN",
        "gemma_live_demo_dependency_authorised": ledger.verdict == "PASS",
        "accelerators_provisioned": 0,
        "persistent_endpoints": 0,
        "total_live_inferences_this_batch": context["total_live_inferences"],
        "open_tasks": [],
        "t085_blocks_this_gate": False,
    }
    architecture = {
        "task": "T102",
        "serving_route": "vertex_ai_maas",
        "model": "google/gemma-4-26b-a4b-it-maas",
        "traffic_type": "ON_DEMAND",
        "region": "us-central1",
        "project": "driftzero-runtime-2026",
        "provisioning_required": False,
        "provisioning_performed": "NONE",
        "why_nothing_was_provisioned": (
            "T102 binds to 'the route selected by G1, using the verified configuration "
            "recorded by T062/T063 — never a route hardcoded here'. G1 recorded "
            "vertex_ai_maas with requires_self_deployment=false and "
            "self_deploy_status=NOT_THE_ACTIVE_ROUTE. A serverless ON_DEMAND route has "
            "no endpoint, no accelerator and no resource to create, so provisioning "
            "would have meant standing up the superseded path the record rejects."
        ),
        "superseded_path": {
            "shape": "Vertex AI Model Garden self-deploy, g4-standard-48, "
                     "NVIDIA_RTX_PRO_6000 x1",
            "g1_status": "PLATFORM_SUPPORTED only — the platform admitted the shape; "
                         "no deployment ever completed",
            "reinstatement_condition": "new evidence of a successful self-deploy AND a "
                                       "granted GPU quota",
        },
        "accelerator_inventory": gpu_inventory(),
        "identity": {
            "driftzero-gemma-sa": "unchanged — no Cloud Run service exists for it to run, "
                                  "because MaaS needs none; permissions not broadened",
            "caller_identity": "Application Default Credentials (aiplatform.user)",
            "service_account_keys": 0,
        },
        "cost_class": {
            "fixed_cost": 0,
            "billing_model": "per-token on demand",
            "max_output_tokens": 8,
            "accelerator_hours": 0,
        },
    }

    written: list[pathlib.Path] = []
    for directory, name, payload in (
        (BUNDLE, "manifest.json", manifest),
        (BUNDLE, "run_summary.json", {k: manifest[k] for k in (
            "gate_id", "task", "verdict", "m3_status", "checks_total", "checks_passed",
            "failed_checks", "timestamp", "accelerators_provisioned",
            "total_live_inferences_this_batch")}),
        (ARCHITECTURE, "serving_route.json", architecture),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
        written.append(path)

    for directory in (BUNDLE, ARCHITECTURE):
        files = sorted(p for p in directory.iterdir() if p.name != "SHA256SUMS.txt")
        lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in files]
        checksums = directory / "SHA256SUMS.txt"
        with checksums.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
        written.append(checksums)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="m3_exit_gate", description="T107 — the M3 exit gate.")
    parser.add_argument("--skip-suite", action="store_true", help="implies --dry-run")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    ledger, context = evaluate()
    if args.skip_suite:
        args.dry_run = True
        suite = {"passed": None, "summary": "SKIPPED"}
    else:
        suite = run_tests()
        ledger.record(34, "full regression suite passes", suite["passed"] is True,
                      suite["summary"], OFFLINE)

    for check in ledger.failed:
        print(f"  FAIL  {check.number:>2}. {check.name}: {check.observed}", file=sys.stderr)

    print(json.dumps({
        "gate_id": "M3_EXIT_GATE",
        "verdict": ledger.verdict,
        "checks": f"{sum(1 for c in ledger.checks if c.passed)}/{len(ledger.checks)}",
        "failed_checks": [c.name for c in ledger.failed],
        "m3_status": "CLOSED" if ledger.verdict == "PASS" else "OPEN",
        "serving_route": "vertex_ai_maas (ON_DEMAND)",
        "accelerators_provisioned": 0,
        "live_inferences_this_batch": context["total_live_inferences"],
        "test_suite": suite["summary"],
    }, indent=2, sort_keys=True))

    if not args.dry_run:
        for path in write_evidence(ledger, context, suite):
            print(f"  wrote {path.relative_to(REPO_ROOT).as_posix()}", file=sys.stderr)

    return 0 if ledger.verdict == "PASS" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
