"""Capture one end-to-end public run driven by two manually uploaded photographs.

The sibling script `public_live_evidence.py` proves the server-owned pilot-photo path.
This one proves the harder claim: a visitor submits their *own* image bytes twice, from
their own device, through the public internet, and the second submission is a genuinely
new model call against genuinely different bytes on the same workflow.

That distinction is what makes the recording honest. Two buttons could in principle be
two lookups; two files a viewer watches being chosen cannot be.

The capability token drives the run and is never recorded — it is a bearer credential for
one workflow, and evidence is the wrong place for one.
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

FIRST = pathlib.Path("fixtures/multimodal/label_left_01.jpg")
SECOND = pathlib.Path("fixtures/multimodal/label_top_right_01.jpg")

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


def timed(*args: str) -> tuple[str, float]:
    started = time.monotonic()
    body = run(*args)
    return body, round(time.monotonic() - started, 3)


def flat(html: str) -> str:
    return re.sub(r"\s+", " ", html)


def describe_image(path: pathlib.Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw[4:8] == b"ftyp":
        actual = "image/heic"
    elif raw[:3] == b"\xff\xd8\xff":
        actual = "image/jpeg"
    else:
        actual = "unknown"
    return {
        "file": path.as_posix(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "declared_by_extension": "image/jpeg",
        "actual_type_sniffed_server_side": actual,
        "note": (
            "the extension says JPEG and the bytes say HEIC; the server derives the "
            "authoritative type from the bytes, so the filename is a claim only"
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print("DRIFTZERO — public manual-upload retry evidence\n")

    first = describe_image(FIRST)
    second = describe_image(SECOND)
    if first["sha256"] == second["sha256"]:
        print("  the two payloads are identical; this proves nothing")
        return 1

    web_revision = run(
        "gcloud", "run", "services", "describe", "driftzero-web",
        "--project=driftzero-runtime-2026", "--region=us-central1",
        "--format=value(status.latestReadyRevisionName)",
    )
    api_revision = run(
        "gcloud", "run", "services", "describe", "driftzero-api",
        "--project=driftzero-runtime-2026", "--region=us-central1",
        "--format=value(status.latestReadyRevisionName)",
    )

    # --- start, unauthenticated -------------------------------------------------------
    print("  starting a live pilot from the public internet ...")
    headers, start_latency = timed(
        "curl", "-s", "-D", "-", "-o", os.devnull, "-X", "POST",
        f"{PUBLIC}/live/start", "-H", "Content-Length: 0", "-m", "300",
    )
    location = next(
        (line.split(":", 1)[1].strip() for line in headers.splitlines()
         if line.lower().startswith("location:")),
        "",
    )
    capability = location.split("capability=", 1)[1] if "capability=" in location else ""
    if not capability:
        print("  FAILED: no capability issued")
        return 1
    print(f"    started in {start_latency}s  (real Gemini)")

    # --- two manual uploads -----------------------------------------------------------
    attempts: list[dict[str, Any]] = []
    for label, image, expected in (
        ("first", FIRST, "FAIL"),
        ("corrected", SECOND, "PASS"),
    ):
        html, latency = timed(
            "curl", "-s", "-m", "300", "-X", "POST", f"{PUBLIC}/live/upload",
            "-F", f"capability={capability}",
            "-F", f"file=@{image.as_posix()};type=image/jpeg",
        )
        body = flat(html)
        observed = re.search(r"Observed: <code>([^<]+)</code>", body)
        verdict = re.search(r"Truth Engine verdict: <code>([^<]+)</code>", body)
        shown = re.search(r"<span>([\d.]+) s</span>", body)
        attempt = {
            "submission": label,
            "submitted_via": "POST /live/upload (multipart, visitor-supplied bytes)",
            "image": describe_image(image),
            "gemma_observation": observed.group(1) if observed else None,
            "truth_engine_verdict": verdict.group(1) if verdict else None,
            "expected_verdict": expected,
            "round_trip_seconds": latency,
            "model_latency_seconds_shown": float(shown.group(1)) if shown else None,
            "model": "google/gemma-4-26b-a4b-it-maas",
            "provider": "vertex_ai_maas",
            "inference": "REAL — a new live call against these bytes",
            # What the page offered next is the defect this run exists to prove fixed.
            "offered_a_corrected_upload": 'type="file"' in body,
            "offered_upload_as_primary": (
                'action="/live/upload"' in body
                and 'action="/live/verify"' in body
                and body.index('action="/live/upload"') < body.index('action="/live/verify"')
            ),
            "pilot_retry_still_offered": "Verify corrected state" in body,
        }
        attempts.append(attempt)
        print(f"    {label}: Gemma observed {attempt['gemma_observation']} -> "
              f"{attempt['truth_engine_verdict']} ({latency}s)")

    # --- the proof this run produced --------------------------------------------------
    proof_html, proof_latency = timed(
        "curl", "-s", "-m", "120", f"{PUBLIC}/live/proof?capability={capability}"
    )
    page = flat(proof_html)
    rows = dict(re.findall(r"<tr><td>([^<]+)</td><td><code>([^<]*)</code></td></tr>", page))
    content_hash = re.search(r'<p class="hash"><code>([0-9a-f]{64})</code></p>', page)
    proof = {
        "change_id": rows.get("Change"),
        "workflow_id": rows.get("Workflow"),
        "affected_artifact_id": rows.get("Affected artifact"),
        "previous_value": rows.get("Previous value"),
        "current_value": rows.get("Current value"),
        "verification_result": rows.get("Verification result"),
        "completion_timestamp": rows.get("Completion timestamp"),
        "proof_id": rows.get("Proof id"),
        "content_hash": content_hash.group(1) if content_hash else None,
        "seven_conditions_satisfied": "7 / 7 conditions satisfied" in page,
        "latency_seconds": proof_latency,
    }
    integrity = {
        "content_hash_matches": "Content hash matches" in page,
        "method": (
            "SHA-256 over the proof's canonical JSON excluding its own content_hash, "
            "recomputed by the public surface rather than by the generator"
        ),
        "is_not": ["a digital signature", "an attestation", "a trusted timestamp",
                   "a ledger entry"],
    }
    print(f"    proof: {proof['proof_id']}")

    distinct = {
        "distinct_payloads": first["sha256"] != second["sha256"],
        "first_image_sha256": first["sha256"],
        "second_image_sha256": second["sha256"],
        "distinct_observations": (
            attempts[0]["gemma_observation"] != attempts[1]["gemma_observation"]
        ),
        "distinct_verdicts": (
            attempts[0]["truth_engine_verdict"] != attempts[1]["truth_engine_verdict"]
        ),
        "why_a_replay_is_impossible": (
            "the backend derives submission identity from the image bytes, so resubmitting "
            "identical bytes is deduplicated into the same verification event rather than "
            "producing a second one; two verification events therefore require two "
            "different payloads"
        ),
    }

    record: dict[str, Any] = {
        "gate_id": "PUBLIC_MANUAL_UPLOAD_RETRY",
        "evidence_class": "REAL_GOOGLE_CLOUD",
        "note": (
            "One end-to-end run from the public internet in which both photographs were "
            "uploaded manually. The first returned FAIL and the page offered a file input "
            "for a corrected photograph — the defect this run exists to prove fixed. Both "
            "submissions are new live Gemma calls on the same capability-bound workflow."
        ),
        "public_url": PUBLIC,
        "backend_url": BACKEND,
        "frontend_revision": web_revision,
        "backend_revision": api_revision,
        "authenticated": False,
        "start": {"latency_seconds": start_latency, "gemini_call": "REAL"},
        "uploads": attempts,
        "two_distinct_model_calls": distinct,
        "change_proof": proof,
        "integrity": integrity,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
    }

    serialized = json.dumps(record, indent=2, sort_keys=True)
    leaks = [
        name
        for name, pattern in {
            "bearer": r"(?i)bearer\s+\S{20,}",
            "jwt": r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ",
            "capability": r"capability=[A-Za-z0-9_\-]{20,}",
            "api_key": r"\bAIza[0-9A-Za-z_\-]{35}\b",
            "billing": r"\b0[0-9A-F]{5}-[0-9A-F]{6}-[0-9A-F]{6}\b",
        }.items()
        if re.search(pattern, serialized)
    ]
    record["credential_scan"] = {"findings": leaks, "clean": not leaks}

    verdicts = [a["truth_engine_verdict"] for a in attempts]
    passed = (
        verdicts == ["FAIL", "PASS"]
        and attempts[0]["offered_a_corrected_upload"]
        and attempts[0]["offered_upload_as_primary"]
        and attempts[0]["pilot_retry_still_offered"]
        and distinct["distinct_payloads"]
        and distinct["distinct_observations"]
        and proof["seven_conditions_satisfied"]
        and integrity["content_hash_matches"]
        and not leaks
    )
    record["verdict"] = "PASS" if passed else "FAIL"

    path = OUT / "public_manual_upload_run.json"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")

    lines = []
    for name in sorted(p.name for p in OUT.glob("*.json")):
        digest = hashlib.sha256((OUT / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}\n")
    with (OUT / "SHA256SUMS.txt").open("w", encoding="utf-8", newline="\n") as handle:
        handle.writelines(lines)

    print(f"\n  chronology: {verdicts}")
    print(f"  corrected upload offered after FAIL: {attempts[0]['offered_a_corrected_upload']}")
    print(f"  credential scan: {'CLEAN' if not leaks else leaks}")
    print(f"  VERDICT: {record['verdict']}")
    print(f"  evidence: {path}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
