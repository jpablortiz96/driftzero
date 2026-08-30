"""T132/T133 — assemble the judge-facing evidence pack and its manifest.

Builds the ``evidence/`` index from artifacts that **already exist and already verify**.
It runs nothing, calls no model, deploys nothing and fabricates nothing: a slot in
quickstart § Evidence Pack Structure with no real artifact behind it is recorded as
``ABSENT`` with the reason, because an evidence pack that quietly fills its own gaps is
worth less than one that names them.

Two outputs:

* ``evidence/README.md`` — the pack's own map (T132)
* ``evidence/MANIFEST.json`` — the index with SHA-256 file hashes (T133)

Every hash here is over **complete file bytes**. That is a different thing from
``ChangeProof.content_hash``, which is a SHA-256 over the proof's canonical JSON
*excluding its own content_hash field*. The manifest says so in its own text, because
the two being confused is the single most likely misreading of this pack.

Run:  python -m scripts.build_evidence_pack
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from datetime import UTC, datetime
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "evidence"
MANIFEST = EVIDENCE / "MANIFEST.json"
README = EVIDENCE / "README.md"

# ---------------------------------------------------------------- evidence classes

REAL_CLOUD = "REAL_GOOGLE_CLOUD"
REAL_MAAS = "REAL_MAAS_EXECUTION"
HISTORICAL = "HISTORICAL_LIVE_MODEL"
OFFLINE = "OFFLINE_DETERMINISTIC"
PHYSICAL = "REAL_PHYSICAL_EVIDENCE"
DERIVED = "DERIVED"

CLASS_MEANING = {
    REAL_CLOUD: (
        "Observed against live Google Cloud resources in driftzero-runtime-2026 — "
        "Cloud Run, Firestore, Cloud Storage, Pub/Sub. Not a mock and not a fixture."
    ),
    REAL_MAAS: (
        "Produced by real inference on Vertex AI MaaS, google/gemma-4-26b-a4b-it-maas, "
        "serverless ON_DEMAND. Each record carries the input hash and token usage."
    ),
    HISTORICAL: (
        "A live-model run recorded earlier and referenced, never re-executed. Kept "
        "byte-identical so a later batch cannot quietly restate it."
    ),
    OFFLINE: (
        "Deterministic, reproducible and free. The Truth Engine, crossings and proof "
        "generator are production code; only the models are substituted."
    ),
    PHYSICAL: (
        "Photographs of a real box with a printed label, taken with a camera. Nothing "
        "generated, rendered or composited occupies these paths."
    ),
    DERIVED: (
        "Computed from other evidence in this pack rather than observed directly — "
        "an index, a gate result, or a checklist over recorded measurements."
    ),
}

# ---------------------------------------------------------------- the index
#
# Ordered by what a judge needs first. Each entry names the claim it supports, so the
# pack answers "which file proves this?" rather than "here is everything we produced".

ENTRIES: list[dict[str, Any]] = [
    {
        "id": "architecture",
        "path": "evidence/m3/architecture/serving_route.json",
        "task": "T102",
        "evidence_class": REAL_CLOUD,
        "claim": "The Gemma serving route is Vertex AI MaaS, ON_DEMAND, with no "
                 "accelerator and no endpoint provisioned.",
        "judge_relevance": "Start here for what runs where, and why no GPU exists.",
    },
    {
        "id": "cloud_run_deployment",
        "path": "evidence/m2/cloud_run_deployment/cloud_run_service.json",
        "task": "T096",
        "evidence_class": REAL_CLOUD,
        "claim": "A private Cloud Run service runs the API under a least-privilege "
                 "service account, scale-to-zero, max 2 instances.",
        "judge_relevance": "Proof there is a real deployed backend, not a local demo.",
    },
    {
        "id": "cloud_run_authentication",
        "path": "evidence/m2/cloud_run_deployment/authentication.json",
        "task": "T096",
        "evidence_class": REAL_CLOUD,
        "claim": "Unauthenticated invocation is refused on every route; an "
                 "authenticated operator succeeds. allUsers is absent.",
        "judge_relevance": "The service is genuinely private.",
    },
    {
        "id": "pubsub_authenticated_push",
        "path": "evidence/m2/cloud_run_deployment/pubsub.json",
        "task": "T089",
        "evidence_class": REAL_CLOUD,
        "claim": "Approved changes arrive by authenticated Pub/Sub push (OIDC) with a "
                 "dead-letter policy bounded at 5 delivery attempts.",
        "judge_relevance": "The workflow starts from a real external event.",
    },
    {
        "id": "end_to_end_push",
        "path": "evidence/m2/cloud_run_deployment/end_to_end_push.json",
        "task": "T089",
        "evidence_class": REAL_CLOUD,
        "claim": "One change published twice produced exactly one workflow; a "
                 "permanently invalid message was refused and dead-lettered after "
                 "exactly 5 attempts.",
        "judge_relevance": "At-least-once delivery is genuinely safe here.",
    },
    {
        "id": "durable_restart_recovery",
        "path": "evidence/runs/hero_run_001/restart_recovery.json",
        "task": "T101",
        "evidence_class": REAL_CLOUD,
        "claim": "A workflow survives process death and resumes the same logical "
                 "execution, with no duplicated remediation or delivery.",
        "judge_relevance": "The system is restart-safe, not merely long-running.",
    },
    {
        "id": "cloud_idempotency",
        "path": "evidence/runs/hero_run_001/idempotency_log.json",
        "task": "T101",
        "evidence_class": REAL_CLOUD,
        "claim": "Duplicate events and duplicate evidence are refused by real "
                 "Firestore and Cloud Storage preconditions.",
        "judge_relevance": "Nothing is executed twice.",
    },
    {
        "id": "durable_resumability",
        "path": "evidence/m2/durable_resumability/restart_scenario.json",
        "task": "T097",
        "evidence_class": OFFLINE,
        "claim": "Three separate processes carry one workflow from pause through FAIL "
                 "to PASS and PROOF_COMPLETE, sharing only Firestore.",
        "judge_relevance": "Resumption is real, not a replay from step one.",
    },
    {
        "id": "gemini_change_intelligence",
        "path": "evidence/pilot_live_change_intel_2026_08_26/change_intelligence.json",
        "task": "T080",
        "evidence_class": HISTORICAL,
        "claim": "Change Intelligence ran on live Gemini and proposed candidate "
                 "artifacts; the Truth Engine qualified exactly one.",
        "judge_relevance": "Where Gemini is actually used.",
    },
    {
        "id": "gemma_feasibility",
        "path": "evidence/g1_gemma_feasibility.json",
        "task": "T067",
        "evidence_class": HISTORICAL,
        "claim": "The Gemma serving route was selected empirically and recorded GO "
                 "after 9 qualifying MaaS inferences.",
        "judge_relevance": "Why Gemma runs on MaaS rather than a self-deployed GPU.",
    },
    {
        "id": "multimodal_evaluation",
        "path": "evidence/reports/multimodal_eval.json",
        "task": "T105",
        "evidence_class": REAL_MAAS,
        "claim": "The production adapter returned an in-domain observation for every "
                 "real fixture, 3/3 matching the expected position.",
        "judge_relevance": "Where Gemma is actually used, through production code.",
    },
    {
        "id": "real_camera_hero_run",
        "path": "evidence/runs/hero_run_001/real_camera_hero_run.json",
        "task": "T106",
        "evidence_class": REAL_MAAS,
        "claim": "Real photographs of a real box: a LEFT photo produced FAIL, a "
                 "TOP_RIGHT photo produced PASS, 7/7 conditions, PROOF_COMPLETE.",
        "judge_relevance": "The hero scenario, end to end, with real physical evidence.",
    },
    {
        "id": "physical_fixtures",
        "path": "fixtures/multimodal/manifest.json",
        "task": "T104",
        "evidence_class": PHYSICAL,
        "claim": "Three camera-captured fixtures with expected observations, hashes, "
                 "and the MIME type sniffed from their actual bytes.",
        "judge_relevance": "What was photographed, and how it is distinguished from "
                           "generated images.",
    },
    {
        "id": "change_proof",
        "path": "evidence/final_live_pilot_2026_08_26/change_proof_DZ-001.json",
        "task": "T078",
        "evidence_class": HISTORICAL,
        "claim": "A complete Change Proof produced by the frozen generator once all "
                 "seven completion conditions held.",
        "judge_relevance": "The artifact the whole system exists to produce.",
    },
    {
        "id": "worker_surface_mobile",
        "path": "evidence/m6/worker_mobile.png",
        "task": "T127",
        "evidence_class": OFFLINE,
        "claim": "The frontline worker sees only the delta: what changed, from what, "
                 "to what, and one action.",
        "judge_relevance": "What the person doing the work actually sees.",
    },
    {
        "id": "worker_surface_failed",
        "path": "evidence/m6/worker_failed.png",
        "task": "T128",
        "evidence_class": OFFLINE,
        "claim": "A failed verification is shown as recoverable, in words, with a "
                 "clear retry.",
        "judge_relevance": "Failure is a step, not a dead end.",
    },
    {
        "id": "proof_surface",
        "path": "evidence/m6/proof_view.png",
        "task": "T130",
        "evidence_class": OFFLINE,
        "claim": "The Change Proof is explained in plain language before any JSON, "
                 "with exact hash wording and no overclaim.",
        "judge_relevance": "How the proof is presented to a human.",
    },
    {
        "id": "frontline_minimums",
        "path": "evidence/reports/frontline_minimums.json",
        "task": "T131",
        "evidence_class": DERIVED,
        "claim": "All six Frontline Surface Minimums pass against the deployed "
                 "surface. Not a WCAG conformance claim.",
        "judge_relevance": "The interaction floor the spec sets, checked.",
    },
    {
        "id": "m1_exit_gate",
        "path": "evidence/runs/hero_run_local/manifest.json",
        "task": "T084",
        "evidence_class": OFFLINE,
        "claim": "The local end-to-end workflow passes with the Truth Engine "
                 "authoritative at every crossing.",
        "judge_relevance": "The deterministic core, reproducible offline.",
    },
    {
        "id": "m2_exit_gate",
        "path": "evidence/m2/exit_gate/manifest.json",
        "task": "T101",
        "evidence_class": REAL_CLOUD,
        "claim": "M2 closed on 37 checks spanning the real cloud architecture.",
        "judge_relevance": "Cloud milestone result.",
    },
    {
        "id": "m3_exit_gate",
        "path": "evidence/m3/exit_gate/manifest.json",
        "task": "T107",
        "evidence_class": REAL_MAAS,
        "claim": "M3 closed on 34 checks; Gemma is authorised as a live-demo "
                 "dependency; zero accelerators provisioned.",
        "judge_relevance": "Model milestone result.",
    },
    {
        "id": "data_lineage",
        "path": "evidence/reports/data_lineage.json",
        "task": "T040",
        "evidence_class": OFFLINE,
        "claim": "Every evidence item carries a classification and an ordered lineage "
                 "chain.",
        "judge_relevance": "What is synthetic and what is derived.",
    },
    {
        "id": "cloud_foundation",
        "path": "evidence/m2/cloud_foundation/task_status.json",
        "task": "T085-T091",
        "evidence_class": REAL_CLOUD,
        "claim": "Project, billing, APIs, Firestore, Pub/Sub, Cloud Storage and two "
                 "least-privilege service accounts, captured from live gcloud output.",
        "judge_relevance": "The cloud foundation, verified rather than asserted.",
    },
    {
        "id": "geap_access_gate",
        "path": "evidence/geap_access_gate.json",
        "task": "T108, T122",
        "evidence_class": REAL_CLOUD,
        "claim": "Each of the six Gemini Enterprise Agent Platform components was "
                 "access-checked against the real account and recorded DEFERRED with "
                 "its reason and the fallback actually in force. None is simulated.",
        "judge_relevance": "What was attempted, what is not available here, and why.",
    },
    {
        "id": "prompt_injection",
        "path": "evidence/security/prompt_injection_blocked.json",
        "task": "T120",
        "evidence_class": OFFLINE,
        "claim": "Against a model that fully obeys an injected directive, the "
                 "structural boundary holds: no tool to call, and no schema field able "
                 "to carry a verdict, a state or an authorization.",
        "judge_relevance": "Why prompt injection cannot reach authority here.",
    },
    {
        "id": "limitations",
        "path": "evidence/LIMITATIONS.md",
        "task": "T135",
        "evidence_class": DERIVED,
        "claim": "What this pilot does not do, stated plainly.",
        "judge_relevance": "Read this before believing anything else here.",
    },
    {
        "id": "judges_start_here",
        "path": "evidence/JUDGES_START_HERE.md",
        "task": "T134",
        "evidence_class": DERIVED,
        "claim": "The entry point: what DRIFTZERO is, what is real, what to inspect.",
        "judge_relevance": "Start here.",
    },
]

# Structural slots quickstart names that have no real artifact behind them. Recorded
# rather than fabricated — an empty directory would imply evidence that does not exist.
ABSENT_SLOTS = [
    {
        "path": "evidence/cost_model.json",
        "reason": (
            "Owned by T136, which reconciles ACTUAL COST OBSERVED from billing against "
            "the ESTIMATED model. T136 has not been executed."
        ),
    },
    {
        "path": "evidence/replays/",
        "reason": (
            "No replay bundles were produced. Reproduction is by running the recorded "
            "gates, which is documented in JUDGES_START_HERE.md."
        ),
    },
    {
        "path": "evidence/raw/",
        "reason": (
            "Raw inputs live at their source paths rather than being duplicated: the "
            "source change is fixtures/hero_change.json and the field images are "
            "fixtures/multimodal/. Copying them would create a second set of bytes to "
            "keep in sync."
        ),
    },
]


# ---------------------------------------------------------------- assembly


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_bundles() -> list[dict[str, Any]]:
    """Re-check every recorded SHA256SUMS in the tree. Nothing is rewritten."""
    results = []
    for checksums in sorted(EVIDENCE.rglob("SHA256SUMS.txt")):
        directory = checksums.parent
        mismatches, missing = [], []
        for line in checksums.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            digest, _, name = line.partition("  ")
            target = directory / name
            if not target.is_file():
                missing.append(name)
            elif sha256(target) != digest:
                mismatches.append(name)
        listed = [
            ln for ln in checksums.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        results.append(
            {
                "bundle": directory.relative_to(REPO_ROOT).as_posix(),
                "files": len(listed),
                "mismatches": mismatches,
                "missing": missing,
                "verified": not mismatches and not missing,
            }
        )
    return results


def build_index() -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve every entry, hashing what exists and reporting what does not."""
    resolved, broken = [], []
    for entry in ENTRIES:
        path = REPO_ROOT / entry["path"]
        record = dict(entry)
        if path.is_file():
            record["sha256"] = sha256(path)
            record["size_bytes"] = path.stat().st_size
            record["present"] = True
        else:
            record["present"] = False
            record["sha256"] = None
            broken.append(entry["path"])
        record["contains_credentials"] = False
        resolved.append(record)
    return resolved, broken


def write_manifest(index: list[dict[str, Any]], bundles: list[dict[str, Any]]) -> dict[str, Any]:
    manifest = {
        "schema": "driftzero.evidence.manifest.v1",
        "task": "T133",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "project": "driftzero-runtime-2026",
        "runtime_mode": "CLOUD_PILOT",
        "production_ready": False,
        "hash_guarantee": {
            "algorithm": "SHA-256",
            "covers": "complete file bytes of each listed artifact",
            "establishes": "content identity and alteration detection only",
            "does_not_establish": [
                "a digital signature",
                "a trusted timestamp",
                "an attestation",
                "a ledger or blockchain entry",
                "non-repudiation",
            ],
            "not_the_same_as_change_proof_content_hash": (
                "ChangeProof.content_hash is a SHA-256 over the proof's canonical JSON "
                "EXCLUDING its own content_hash field. The hashes in this manifest are "
                "over whole files. The SHA-256 of a proof FILE is therefore expected to "
                "differ from the content_hash recorded inside it."
            ),
        },
        "evidence_classes": CLASS_MEANING,
        "artifacts": index,
        "artifact_count": len(index),
        "bundle_verification": bundles,
        "bundles_verified": sum(1 for b in bundles if b["verified"]),
        "bundles_total": len(bundles),
        "absent_slots": ABSENT_SLOTS,
        "absent_slot_policy": (
            "quickstart § Evidence Pack Structure describes the full intended tree. "
            "Slots with no real artifact are listed above with the reason rather than "
            "populated with a placeholder."
        ),
    }
    with MANIFEST.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def write_readme(manifest: dict[str, Any]) -> None:
    by_class: dict[str, list[dict[str, Any]]] = {}
    for artifact in manifest["artifacts"]:
        by_class.setdefault(artifact["evidence_class"], []).append(artifact)

    lines = [
        "# DRIFTZERO — evidence pack",
        "",
        "Start with [JUDGES_START_HERE.md](JUDGES_START_HERE.md), then",
        "[LIMITATIONS.md](LIMITATIONS.md). This file is the map; "
        "[MANIFEST.json](MANIFEST.json) is the index with hashes.",
        "",
        "Every artifact below already existed before this pack was assembled. Nothing "
        "here was generated to fill the pack, and nothing was re-run to refresh it.",
        "",
        "## What kind of evidence is what",
        "",
        "| Class | Meaning |",
        "| --- | --- |",
    ]
    for name, meaning in CLASS_MEANING.items():
        lines.append(f"| `{name}` | {meaning} |")

    lines += ["", "## Artifacts by class", ""]
    for name in (REAL_CLOUD, REAL_MAAS, HISTORICAL, PHYSICAL, OFFLINE, DERIVED):
        artifacts = by_class.get(name, [])
        if not artifacts:
            continue
        lines += [f"### {name}", ""]
        for artifact in artifacts:
            mark = "" if artifact["present"] else " **(missing)**"
            lines.append(
                f"- [`{artifact['path']}`]({_relative(artifact['path'])}){mark} — "
                f"{artifact['claim']}"
            )
        lines.append("")

    lines += [
        "## Bundle integrity",
        "",
        f"{manifest['bundles_verified']} of {manifest['bundles_total']} recorded "
        "`SHA256SUMS.txt` bundles verify.",
        "",
        "```",
        "cd evidence/<bundle> && sha256sum -c SHA256SUMS.txt",
        "```",
        "",
        "## What is deliberately absent",
        "",
    ]
    for slot in ABSENT_SLOTS:
        lines.append(f"- `{slot['path']}` — {slot['reason']}")

    lines += [
        "",
        "## Hash boundary",
        "",
        "SHA-256 in this pack covers **complete file bytes** and establishes content "
        "identity and alteration detection only. It is not a signature, not a trusted "
        "timestamp, not an attestation and not a ledger entry.",
        "",
        "`ChangeProof.content_hash` is a different hash over a different preimage: the "
        "proof's canonical JSON **excluding its own `content_hash` field**. The SHA-256 "
        "of a proof file is therefore expected to differ from the value inside it.",
        "",
    ]
    with README.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))


def _relative(path: str) -> str:
    """A link that resolves from evidence/README.md."""
    return path[len("evidence/") :] if path.startswith("evidence/") else "../" + path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_evidence_pack")
    parser.add_argument("--check", action="store_true",
                        help="validate without writing; exits non-zero on any problem")
    args = parser.parse_args(argv)

    index, broken = build_index()
    bundles = verify_bundles()
    failed_bundles = [b["bundle"] for b in bundles if not b["verified"]]

    if not args.check:
        manifest = write_manifest(index, bundles)
        write_readme(manifest)
        for path in (MANIFEST, README):
            print(f"  wrote {path.relative_to(REPO_ROOT).as_posix()}", file=sys.stderr)

    summary = {
        "artifacts_indexed": len(index),
        "artifacts_present": sum(1 for a in index if a["present"]),
        "missing_artifacts": broken,
        "bundles_verified": f"{len(bundles) - len(failed_bundles)}/{len(bundles)}",
        "failed_bundles": failed_bundles,
        "absent_slots_declared": len(ABSENT_SLOTS),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not broken and not failed_bundles else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
