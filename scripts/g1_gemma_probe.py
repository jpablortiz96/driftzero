"""T065 — G1 Gemma feasibility probe harness.

Sends each physical fixture to the selected serving route and records the **raw**
structured response plus a strictly normalized observation.

This is a probe, not the production adapter. It lives outside ``src/driftzero`` so no
cloud or model dependency can reach the deterministic core, and it imports its serving
SDK lazily so the repository stays installable and the M0 suite stays offline.

Authority boundary — the whole point of the spike:
    The model produces an *observation* only. It never produces PASS, FAIL,
    PROOF_COMPLETE, a workflow state, or an authorization. Anything outside the closed
    ``ObservedPosition`` domain fails normalization and is recorded as such rather than
    guessed at. The deterministic Truth Engine alone compares expected vs observed.

Usage::

    # Record environment/access state only (no route or fixtures needed):
    python scripts/g1_gemma_probe.py --access-check-only

    # Regenerate evidence with a hard guarantee that nothing external was contacted:
    python scripts/g1_gemma_probe.py --access-check-only --offline

    # Probe a live route once fixtures exist and access is granted:
    python scripts/g1_gemma_probe.py \\
        --route cloud_run_vllm --endpoint https://... --repeat 3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.models.classification import ClassificationLabel  # noqa: E402
from driftzero.models.verification import ObservedPosition  # noqa: E402

DEFAULT_EVIDENCE = REPO_ROOT / "evidence" / "g1_gemma_feasibility.json"
DEFAULT_FIXTURES = REPO_ROOT / "fixtures" / "multimodal"
DEFAULT_PLATFORM_SESSION = REPO_ROOT / "evidence" / "g1_platform_session.json"

DEFAULT_SYNTHETIC_FIXTURES = DEFAULT_FIXTURES / "synthetic"
"""Generated engineering fixtures. Never evidence, never a T064 discharge."""

PHYSICAL_CAPTURE_METHOD = "PHYSICAL_CAMERA_CAPTURE"
"""The only capture method that discharges T064."""

SYNTHETIC_COUNTERPARTS = {
    "label_left_01.jpg": "synthetic/label_left_synthetic_01.jpg",
    "label_top_right_01.jpg": "synthetic/label_top_right_synthetic_01.jpg",
    "label_ambiguous_01.jpg": "synthetic/label_ambiguous_synthetic_01.jpg",
}
"""Canonical REAL_PHYSICAL path -> its synthetic stand-in, kept deliberately distinct."""

UNDECLARED_CLASSIFICATION = "UNDECLARED"
"""Applied when a fixture file exists but no provenance entry declares it."""

MODEL_VARIANT = "gemma-4-12b"
"""Authoritative variant from research.md R-008. Not to be silently substituted."""

PROMPT = (
    "Look at the package in the image. Report ONLY where the shipping label is "
    "physically placed. Answer with exactly one word: LEFT, TOP_RIGHT, or "
    "INCONCLUSIVE. Answer INCONCLUSIVE if the label position cannot be determined "
    "from what is visible. Do not explain. Do not judge correctness."
)

EXPECTED_BY_FIXTURE = {
    "label_left_01.jpg": ObservedPosition.LEFT,
    "label_top_right_01.jpg": ObservedPosition.TOP_RIGHT,
    "label_ambiguous_01.jpg": ObservedPosition.INCONCLUSIVE,
}


class ServingRoute(StrEnum):
    """The two serving options approved in research.md R-008."""

    MODEL_GARDEN = "vertex_ai_model_garden"
    CLOUD_RUN_VLLM = "cloud_run_vllm"


class AccessState(StrEnum):
    """Honest labels for what was and was not established."""

    ACTUAL_OBSERVED = "ACTUAL_OBSERVED"
    ESTIMATED = "ESTIMATED"
    NOT_TESTED = "NOT_TESTED"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_YET_DECIDABLE = "NOT_YET_DECIDABLE"


class NormalizationError(ValueError):
    """Raw model output did not map onto the closed observation domain."""


def normalize_observation(raw: object) -> ObservedPosition:
    """Map raw model output onto the closed domain, or raise.

    Accepts only an exact case-insensitive match for ``LEFT``, ``TOP_RIGHT``, or
    ``INCONCLUSIVE`` after trimming surrounding whitespace, quotes, and punctuation.

    Deliberately refuses everything else — ``PASS``, ``FAIL``, ``RIGHT``,
    ``PROBABLY_LEFT``, ``UNKNOWN_BUT_LIKELY_TOP_RIGHT``, ``0.92``, prose, empty
    output. Guessing at an unrecognized verdict is exactly the silent conversion of
    uncertainty the specification forbids.
    """
    if isinstance(raw, ObservedPosition):
        return raw
    if not isinstance(raw, str):
        raise NormalizationError(f"non-string model output: {raw!r}")
    cleaned = raw.strip().strip("\"'`.,!;: \n\t").upper().replace("-", "_").replace(" ", "_")
    try:
        return ObservedPosition(cleaned)
    except ValueError as exc:
        raise NormalizationError(f"out-of-domain model output: {raw!r}") from exc


def extract_raw_observation(response: Any) -> str:
    """Pull the observation string out of a route response without interpreting it."""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        for key in ("observed_label_position", "observation", "text", "output"):
            if key in response:
                return str(response[key])
        return json.dumps(response, sort_keys=True)
    return str(response)


@dataclass
class ProbeRecord:
    """One inference attempt. Failures and inconclusive results are kept, not dropped."""

    fixture_id: str
    fixture_classification: str
    expected_observation: str
    raw_output: str
    normalized_output: str | None
    normalization_succeeded: bool
    latency_seconds: float | None
    latency_label: str
    matched_expected: bool | None
    attempt: int
    error: str | None = None


@dataclass
class ProbeReport:
    """The G1 evidence document. Machine-generated; never handwritten."""

    task: str = "G1 — Gemma feasibility risk spike (T061-T067)"
    generated_at: str = ""
    model_variant: str = MODEL_VARIANT
    serving_route: str | None = None
    route_config: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    access_checks: dict[str, Any] = field(default_factory=dict)
    fixtures: dict[str, Any] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)
    stability: dict[str, Any] = field(default_factory=dict)
    verdict: str = "NOT_YET_DECIDABLE"
    verdict_reasoning: list[str] = field(default_factory=list)
    decision_blockers: list[dict[str, Any]] = field(default_factory=list)
    operational_holds: list[dict[str, Any]] = field(default_factory=list)
    data_classification: dict[str, Any] = field(default_factory=dict)
    # --- platform-session extension (real deploy attempts, quota, billing) ---
    outcome_flags: dict[str, Any] = field(default_factory=dict)
    model_access: dict[str, Any] = field(default_factory=dict)
    route_decision: dict[str, Any] = field(default_factory=dict)
    verified_deployment_configuration: dict[str, Any] = field(default_factory=dict)
    deployment_attempts: list[dict[str, Any]] = field(default_factory=list)
    quota_findings: dict[str, Any] = field(default_factory=dict)
    billing_state: dict[str, Any] = field(default_factory=dict)


def _run(cmd: Sequence[str], timeout: int = 30) -> tuple[int, str]:
    """Run a read-only command, returning (exit code, trimmed output)."""
    try:
        proc = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - env dependent
        return 1, f"{type(exc).__name__}: {exc}"


def collect_access_checks(*, offline: bool = False) -> dict[str, Any]:
    """T061/T062/T063 access checks. Read-only: configures and deploys nothing.

    With ``offline=True`` no subprocess runs at all, so the report can be regenerated
    with a guarantee that nothing external — billable or otherwise — was contacted.
    """
    checks: dict[str, Any] = {}

    if offline:
        session = load_platform_session()
        project = (session.get("project") or {}).get("project_id")
        checks["mode"] = {
            "state": AccessState.ACTUAL_OBSERVED,
            "offline": True,
            "note": "No subprocess or network call was made while generating this report.",
        }
        checks["gcloud_authenticated"] = {
            "state": AccessState.ACTUAL_OBSERVED if project else AccessState.NOT_TESTED,
            "authenticated": bool(project),
            "detail": "sourced from the recorded platform session, not re-queried",
        }
        checks["gcp_project"] = {
            "state": AccessState.ACTUAL_OBSERVED if project else AccessState.NOT_TESTED,
            "configured": bool(project),
            "detail": project or "(unset)",
        }
        checks["application_default_credentials"] = {"state": AccessState.NOT_TESTED}
        checks["local_env_file"] = {
            "state": AccessState.ACTUAL_OBSERVED,
            "present": (REPO_ROOT / ".env").exists(),
        }
        return checks

    gcloud = shutil.which("gcloud")
    checks["gcloud_cli"] = {
        "state": AccessState.ACTUAL_OBSERVED,
        "present": bool(gcloud),
        "path": gcloud,
    }

    if gcloud:
        code, accounts = _run([gcloud, "auth", "list", "--format=value(account)"])
        authenticated = code == 0 and bool(accounts) and "No credentialed" not in accounts
        checks["gcloud_authenticated"] = {
            "state": AccessState.ACTUAL_OBSERVED,
            "authenticated": authenticated,
            "detail": accounts or "no credentialed accounts",
        }
        code, project = _run([gcloud, "config", "get-value", "project"])
        has_project = code == 0 and project not in ("", "(unset)")
        checks["gcp_project"] = {
            "state": AccessState.ACTUAL_OBSERVED,
            "configured": has_project,
            "detail": project or "(unset)",
        }
    else:  # pragma: no cover - environment dependent
        checks["gcloud_authenticated"] = {"state": AccessState.UNAVAILABLE}
        checks["gcp_project"] = {"state": AccessState.UNAVAILABLE}

    checks["application_default_credentials"] = {
        "state": AccessState.ACTUAL_OBSERVED,
        "present": bool(_env("GOOGLE_APPLICATION_CREDENTIALS")),
    }
    checks["local_env_file"] = {
        "state": AccessState.ACTUAL_OBSERVED,
        "present": (REPO_ROOT / ".env").exists(),
    }
    return checks


def _env(name: str) -> str:
    import os

    return os.environ.get(name, "")


def _repo_relative(path: Path) -> str:
    """Repo-relative display path, tolerant of a fixtures dir outside the repo."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


GENERATED_MEDIA_MARKERS = (
    # IPTC digital source type meaning "created by a trained AI model". Written by the
    # generator itself into the C2PA manifest — the decisive marker.
    b"trainedAlgorithmicMedia",
    b"compositeWithTrainedAlgorithmicMedia",
    # Known generative software agents.
    b"gpt-image",
    b"OpenAI Media Service",
    b"dall-e",
    b"midjourney",
    b"stable-diffusion",
    b"firefly",
    b"imagen",
)
"""Producer/source markers. Decisive ONLY inside a recognized provenance structure.

These same strings can legitimately appear in a real photograph — printed on a poster in
frame, in a caption, in an unrelated comment field. Matching them against raw file bytes
would therefore manufacture false positives, so they are only ever searched inside the
metadata structures extracted by :func:`extract_provenance_regions`.
"""

PNG_PROVENANCE_CHUNKS = (b"caBX", b"eXIf", b"iTXt", b"tEXt", b"zTXt")
"""PNG chunks that carry provenance: C2PA/JUMBF, EXIF, and text/XMP metadata."""

JPEG_PROVENANCE_SEGMENTS = {0xE1: "APP1_EXIF_OR_XMP", 0xEB: "APP11_JUMBF_C2PA", 0xED: "APP13_IPTC"}
"""JPEG application segments that carry provenance."""


def extract_provenance_regions(raw: bytes) -> list[tuple[str, bytes]]:
    """Return ``(structure_name, payload)`` for each recognized metadata structure.

    Only these regions may supply generator evidence. Pixel data, entropy-coded scan
    data, and unparsed trailing bytes are deliberately excluded: a producer name in the
    image content is not a provenance claim.

    Malformed files yield fewer regions rather than raising — an unparsable container
    simply provides no positive evidence.
    """
    regions: list[tuple[str, bytes]] = []

    if raw[:4] == bytes([0x89]) + b"PNG":
        offset = 8
        while offset + 8 <= len(raw):
            length = int.from_bytes(raw[offset : offset + 4], "big")
            chunk_type = raw[offset + 4 : offset + 8]
            if offset + 12 + length > len(raw):
                break
            if chunk_type in PNG_PROVENANCE_CHUNKS:
                regions.append(
                    (f"PNG_{chunk_type.decode('latin-1')}", raw[offset + 8 : offset + 8 + length])
                )
            if chunk_type == b"IEND":
                break
            offset += 12 + length
        return regions

    if raw[:3] == bytes([0xFF, 0xD8, 0xFF]):
        offset = 2
        while offset + 4 <= len(raw):
            if raw[offset] != 0xFF:
                break
            marker = raw[offset + 1]
            if marker == 0xDA:  # start of scan: only pixel data follows
                break
            if marker in (0xD8, 0xD9):
                offset += 2
                continue
            length = int.from_bytes(raw[offset + 2 : offset + 4], "big")
            if length < 2 or offset + 2 + length > len(raw):
                break
            if marker in JPEG_PROVENANCE_SEGMENTS:
                regions.append(
                    (JPEG_PROVENANCE_SEGMENTS[marker], raw[offset + 4 : offset + 2 + length])
                )
            offset += 2 + length
    return regions

CAMERA_CAPTURE_MARKERS = (b"digitalCapture", b"capturedWithDevice")
"""C2PA markers that positively corroborate a camera capture."""


def load_known_generated_hashes(exclude_dir: Path | None = None) -> dict[str, str]:
    """Registry of content hashes already authoritatively classified as generated.

    Built from the provenance manifests under ``fixtures/``. ``exclude_dir`` omits the
    directory being inspected, so a match is always *independent* corroboration from
    another manifest rather than a file vouching for its own classification.
    """
    registry: dict[str, str] = {}
    root = DEFAULT_FIXTURES
    if not root.exists():
        return registry
    for manifest_path in sorted(root.rglob("provenance.json")):
        if exclude_dir is not None and manifest_path.parent == exclude_dir:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # pragma: no cover - malformed manifest is ignored
            continue
        for name, entry in (manifest.get("fixtures") or {}).items():
            digest = entry.get("sha256")
            labels = entry.get("classification") or []
            declared_generated = (
                entry.get("generated") is True
                or entry.get("capture_method") == "GENERATED_IMAGE"
                or ClassificationLabel.SYNTHETIC in labels
            )
            if digest and declared_generated:
                registry[digest] = f"{_repo_relative(manifest_path.parent / name)}"
    return registry


def detect_generated_media(
    path: Path, known_generated: dict[str, str] | None = None
) -> dict[str, Any]:
    """Inspect an image's bytes for **positive** evidence that it was machine-generated.

    Only positive generator provenance disqualifies a fixture:

    * **A** — the content hash matches a fixture another manifest already classifies as
      SYNTHETIC / GENERATED_IMAGE.
    * **B** — a **recognized provenance structure** (C2PA/JUMBF, EXIF, XMP/text, IPTC)
      names ``trainedAlgorithmicMedia`` or a known generative-image producer. Producer
      names occurring anywhere else in the file — pixel data, scan data, a poster caught
      in frame, an unparsed trailer — carry no weight at all.

    Absence of camera metadata proves nothing. Real photographs routinely lose EXIF to
    messaging apps, editors, export pipelines, privacy stripping, and format conversion,
    and a container/extension mismatch is a content-type anomaly, not a generator claim.
    Those observations are recorded under ``anomalies`` and never set ``is_generated``.

    The relation stays asymmetric: positive evidence may DISQUALIFY, but clean content
    never qualifies anything. REAL_PHYSICAL still requires the operator declaration and
    the existing physical-capture rules.

    Pure stdlib: no image library, no network, no new dependency.
    """
    finding: dict[str, Any] = {
        "inspected": False,
        "is_generated": False,
        "decisive_signals": [],
        "anomalies": [],
        "sha256": None,
        "markers": [],
        "provenance_structures": [],
        "generator_provenance_matches": [],
        "known_generated_hash_match": None,
        "container_format": None,
        "extension_matches_container": None,
        "exif_present": False,
        "c2pa_present": False,
        "camera_capture_corroborated": False,
    }
    if not path.exists():
        return finding

    raw = path.read_bytes()
    finding["inspected"] = True
    finding["sha256"] = hashlib.sha256(raw).hexdigest()

    if raw[:4] == b"\x89PNG":
        finding["container_format"] = "PNG"
    elif raw[:3] == b"\xff\xd8\xff":
        finding["container_format"] = "JPEG"
    else:
        finding["container_format"] = "UNKNOWN"

    expected = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}.get(path.suffix.lower())
    finding["extension_matches_container"] = (
        None if expected is None else expected == finding["container_format"]
    )
    finding["exif_present"] = (
        b"Exif\x00\x00" in raw[:65536] or b"eXIf" in raw[:65536]
    )
    finding["c2pa_present"] = b"caBX" in raw or b"c2pa" in raw[:200000]
    finding["camera_capture_corroborated"] = any(m in raw for m in CAMERA_CAPTURE_MARKERS)

    # --- Signal A: independently recorded as generated -------------------------------
    registry = load_known_generated_hashes() if known_generated is None else known_generated
    source = registry.get(finding["sha256"])
    if source:
        finding["known_generated_hash_match"] = source
        finding["decisive_signals"].append({
            "signal": "KNOWN_GENERATED_HASH_MATCH",
            "detail": (
                f"SHA-256 {finding['sha256']} is byte-identical to {source}, already "
                "classified SYNTHETIC / GENERATED_IMAGE in another provenance manifest."
            ),
        })

    # --- Signal B: a recognized provenance structure says so -------------------------
    # Scoped deliberately: producer names are searched ONLY inside extracted metadata
    # structures, never across raw bytes. A photograph containing the text "midjourney"
    # on a poster in frame is still a photograph.
    regions = extract_provenance_regions(raw)
    finding["provenance_structures"] = [name for name, _ in regions]
    matches: list[dict[str, str]] = []
    for structure, payload in regions:
        for marker in GENERATED_MEDIA_MARKERS:
            if marker in payload:
                matches.append({"marker": marker.decode("latin-1"), "structure": structure})
    finding["generator_provenance_matches"] = matches
    finding["markers"] = sorted({m["marker"] for m in matches})
    if matches:
        located = ", ".join(f"{m['marker']} in {m['structure']}" for m in matches)
        finding["decisive_signals"].append({
            "signal": "EMBEDDED_GENERATOR_PROVENANCE",
            "detail": (
                "A recognized provenance structure names a generative producer or the "
                f"IPTC trainedAlgorithmicMedia source type: {located}."
            ),
        })

    # --- Diagnostics: recorded, never decisive ---------------------------------------
    if finding["extension_matches_container"] is False:
        finding["anomalies"].append({
            "anomaly": "CONTAINER_EXTENSION_MISMATCH",
            "decisive": False,
            "detail": (
                f"Container is {finding['container_format']} but the filename claims "
                f"{path.suffix}. An integrity/content-type anomaly worth fixing; it does "
                "not indicate AI generation."
            ),
        })
    if not finding["exif_present"]:
        finding["anomalies"].append({
            "anomaly": "NO_EXIF",
            "decisive": False,
            "detail": (
                "No EXIF block found. Real photographs routinely lose EXIF to messaging "
                "apps, editors, export pipelines, privacy stripping, and conversion, so "
                "this is not evidence of generation."
            ),
        })

    finding["is_generated"] = bool(finding["decisive_signals"])
    return finding


def load_fixture_provenance(fixtures_dir: Path) -> dict[str, Any]:
    """Read the fixture provenance declaration, or an empty mapping if absent."""
    manifest = fixtures_dir / "provenance.json"
    if not manifest.exists():
        return {}
    try:
        return json.loads(manifest.read_text(encoding="utf-8")).get("fixtures", {})
    except (OSError, ValueError):  # pragma: no cover - malformed manifest fails closed
        return {}


def classify_fixture(name: str, provenance: dict[str, Any]) -> tuple[str, bool]:
    """Return ``(classification, satisfies_t064)`` for one fixture.

    Fails **closed**: a fixture with no declaration, or one whose declared capture
    method is anything other than a physical camera capture, never counts as the
    physical evidence T064 requires. Presence of a file at the required path proves
    only that a file exists.
    """
    entry = provenance.get(name)
    if not entry:
        return UNDECLARED_CLASSIFICATION, False
    labels = entry.get("classification") or [UNDECLARED_CLASSIFICATION]
    classification = ",".join(str(label) for label in labels)
    satisfies = (
        entry.get("capture_method") == PHYSICAL_CAPTURE_METHOD
        and ClassificationLabel.REAL in labels
        # A SYNTHETIC label is disqualifying on its own. Generated media cannot be
        # promoted by adding a REAL label beside it, nor by where the file sits.
        and ClassificationLabel.SYNTHETIC not in labels
        and entry.get("satisfies_t064_physical_capture") is True
    )
    return classification, satisfies


def collect_fixture_status(fixtures_dir: Path) -> dict[str, Any]:
    """T064 fixture status, gated on declared provenance rather than mere presence.

    A synthetic image sitting at a required path is recorded as exactly that — it does
    not discharge T064 and cannot contribute to a GO verdict.
    """
    provenance = load_fixture_provenance(fixtures_dir)
    # Exclude this directory's own manifest: a hash match must be corroboration from an
    # independent record, never a file vouching for its own classification.
    known_generated = load_known_generated_hashes(exclude_dir=fixtures_dir)
    required = {}
    for name in EXPECTED_BY_FIXTURE:
        path = fixtures_dir / name
        classification, satisfies = classify_fixture(name, provenance)
        content = detect_generated_media(path, known_generated)
        # Asymmetric by design: positive generator provenance can DISQUALIFY, but clean
        # content never qualifies anything. REAL_PHYSICAL still requires the operator
        # declaration and the physical-capture rules in classify_fixture().
        if content["is_generated"]:
            satisfies = False
        required[name] = {
            "path": _repo_relative(path),
            "present": path.exists(),
            "bytes": path.stat().st_size if path.exists() else 0,
            "classification": classification,
            "capture_method": (provenance.get(name) or {}).get(
                "capture_method", UNDECLARED_CLASSIFICATION
            ),
            "content_inspection": content,
            "satisfies_t064_physical_capture": satisfies and path.exists(),
        }
    satisfied = all(f["satisfies_t064_physical_capture"] for f in required.values())
    rejected = sorted(
        name
        for name, f in required.items()
        if f["present"] and f["content_inspection"]["is_generated"]
    )
    return {
        "directory": str(fixtures_dir),
        "directory_exists": fixtures_dir.exists(),
        "required": required,
        "capture_type_required": "REAL_PHYSICAL_CAPTURE",
        "provenance_manifest_present": bool(provenance),
        "all_present": all(f["present"] for f in required.values()),
        "physical_capture_satisfied": satisfied,
        "rejected_as_generated": rejected,
        "note": (
            "Presence is not provenance. Synthetic images may not substitute for the "
            "required physical capture, and an undeclared fixture fails closed as "
            "non-physical. The observation must come from visible evidence only — no "
            "filename, EXIF, directory, watermark, or prompt hint may encode the answer. "
            "Each file is additionally inspected for embedded generator provenance "
            "(C2PA content credentials, IPTC digitalSourceType) and against the registry "
            "of hashes already classified generated; a positive finding disqualifies it "
            "regardless of its declaration. Missing EXIF and container/extension mismatch "
            "are recorded as non-decisive anomalies only."
        ),
    }


def load_platform_session(path: Path = DEFAULT_PLATFORM_SESSION) -> dict[str, Any]:
    """Read recorded real-platform observations, or an empty mapping if absent."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # pragma: no cover - malformed session fails closed
        return {}


def derive_outcome_flags(session: dict[str, Any]) -> dict[str, Any]:
    """The four distinct platform outcomes, never collapsed into one another.

    ``PLATFORM_SUPPORTED`` and ``PLATFORM_ATTEMPTED`` say the platform admitted a
    configuration and that a real attempt was made. Neither implies
    ``DEPLOYMENT_SUCCEEDED``, and a deployment implies nothing about
    ``INFERENCE_SUCCEEDED``. Conflating them is how a failed spike gets reported as a
    working one.
    """
    recorded = session.get("outcome_flags", {})
    flags: dict[str, Any] = {}
    for name in (
        "PLATFORM_SUPPORTED",
        "PLATFORM_ATTEMPTED",
        "DEPLOYMENT_SUCCEEDED",
        "INFERENCE_SUCCEEDED",
    ):
        entry = recorded.get(name) or {}
        flags[name] = {
            "value": bool(entry.get("value", False)),
            "basis": entry.get("basis", "no recorded platform session evidence"),
        }
    return flags


def invoke_route(route: ServingRoute, endpoint: str, image_path: Path) -> Any:
    """Send one image to the selected route.

    The SDK/HTTP client is imported lazily and only here, so nothing in this file
    requires a cloud dependency to be installed for the offline tests to run.
    """
    image_bytes = image_path.read_bytes()

    if route is ServingRoute.CLOUD_RUN_VLLM:
        try:
            import base64

            import httpx  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "cloud_run_vllm route needs an HTTP client (httpx) installed in the "
                "probe environment; it is deliberately not a repository dependency"
            ) from exc
        payload = {
            "model": MODEL_VARIANT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/jpeg;base64,"
                                + base64.b64encode(image_bytes).decode()
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 8,
            "temperature": 0,
        }
        response = httpx.post(endpoint, json=payload, timeout=60)
        response.raise_for_status()
        body = response.json()
        return body["choices"][0]["message"]["content"]

    try:
        from google import genai  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "vertex_ai_model_garden route needs the google-genai SDK installed in the "
            "probe environment; it is deliberately not a repository dependency"
        ) from exc
    client = genai.Client()
    result = client.models.generate_content(
        model=endpoint,
        contents=[PROMPT, {"mime_type": "image/jpeg", "data": image_bytes}],
    )
    return result.text


def probe_fixture(
    route: ServingRoute, endpoint: str, image_path: Path, attempt: int
) -> ProbeRecord:
    """One measured inference attempt against a live route.

    The fixture's classification is read from its provenance declaration. It is never
    assumed to be a physical capture just because the file sits at a required path.
    """
    expected = EXPECTED_BY_FIXTURE[image_path.name]
    provenance = load_fixture_provenance(image_path.parent)
    classification, _ = classify_fixture(image_path.name, provenance)
    started = time.perf_counter()
    try:
        response = invoke_route(route, endpoint, image_path)
        latency = time.perf_counter() - started
    except Exception as exc:  # noqa: BLE001 - probe records failures rather than raising
        return ProbeRecord(
            fixture_id=image_path.name,
            fixture_classification=classification,
            expected_observation=str(expected),
            raw_output="",
            normalized_output=None,
            normalization_succeeded=False,
            latency_seconds=None,
            latency_label=str(AccessState.NOT_TESTED),
            matched_expected=None,
            attempt=attempt,
            error=f"{type(exc).__name__}: {exc}",
        )

    raw = extract_raw_observation(response)
    try:
        normalized = normalize_observation(raw)
    except NormalizationError as exc:
        return ProbeRecord(
            fixture_id=image_path.name,
            fixture_classification=classification,
            expected_observation=str(expected),
            raw_output=raw,
            normalized_output=None,
            normalization_succeeded=False,
            latency_seconds=round(latency, 4),
            latency_label=str(AccessState.ACTUAL_OBSERVED),
            matched_expected=False,
            attempt=attempt,
            error=str(exc),
        )

    return ProbeRecord(
        fixture_id=image_path.name,
        fixture_classification=classification,
        expected_observation=str(expected),
        raw_output=raw,
        normalized_output=str(normalized),
        normalization_succeeded=True,
        latency_seconds=round(latency, 4),
        latency_label=str(AccessState.ACTUAL_OBSERVED),
        matched_expected=normalized is expected,
        attempt=attempt,
    )


def qualifying_records(records: list[ProbeRecord]) -> list[ProbeRecord]:
    """Records that may inform the G1 gate at all.

    A record qualifies only if it came from a fixture declared REAL (never SYNTHETIC or
    UNDECLARED) and normalization succeeded. Results obtained from generated images are
    kept in the evidence for transparency but can never move the verdict.
    """
    return [
        record
        for record in records
        if record.normalization_succeeded
        and ClassificationLabel.REAL in record.fixture_classification.split(",")
        and ClassificationLabel.SYNTHETIC not in record.fixture_classification.split(",")
    ]


def summarize_stability(records: list[ProbeRecord]) -> dict[str, Any]:
    """Report per-fixture variance. Instability is recorded, never hidden."""
    by_fixture: dict[str, list[str | None]] = {}
    for record in records:
        by_fixture.setdefault(record.fixture_id, []).append(record.normalized_output)
    return {
        fixture: {
            "attempts": len(values),
            "distinct_normalized_outputs": sorted({str(v) for v in values}),
            "stable": len({str(v) for v in values}) <= 1,
        }
        for fixture, values in by_fixture.items()
    }


def build_report(
    route: ServingRoute | None,
    endpoint: str | None,
    fixtures_dir: Path,
    records: list[ProbeRecord],
    *,
    session: dict[str, Any] | None = None,
    offline: bool = False,
) -> ProbeReport:
    """Assemble the G1 evidence document and compute the gate verdict.

    ``session`` carries recorded real-platform observations. ``offline=True`` skips
    every subprocess so the report can be regenerated without contacting anything.
    """
    session = load_platform_session() if session is None else session
    verified_config = session.get("verified_deployment_configuration", {})

    report = ProbeReport(
        generated_at=datetime.now(UTC).isoformat(),
        serving_route=str(route) if route else session.get("selected_route"),
        route_config={
            "endpoint": endpoint,
            "prompt": PROMPT,
            "max_tokens": 8,
            "temperature": 0,
            "quantization": verified_config.get(
                "quantization_note", str(AccessState.NOT_YET_DECIDABLE)
            ),
        },
        environment={
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        access_checks=collect_access_checks(offline=offline),
        fixtures=collect_fixture_status(fixtures_dir),
        records=[asdict(r) for r in records],
        stability=summarize_stability(records),
        data_classification={
            "labels": ["REAL"],
            "note": (
                "Access-check and platform-session results are REAL observations. "
                "No inference was performed unless records are present below. Fixture "
                "images currently on disk are SYNTHETIC — see fixtures.required[*]."
            ),
        },
        outcome_flags=derive_outcome_flags(session),
        model_access=session.get("model_access", {}),
        route_decision=session.get("route_decision", {}),
        verified_deployment_configuration=verified_config,
        deployment_attempts=session.get("deployment_attempts", []),
        quota_findings=session.get("quota_findings", {}),
        billing_state=session.get("billing_state", {}),
    )

    blockers: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    flags = report.outcome_flags
    qualifying = qualifying_records(records)

    # --- G1 decision blockers: technical facts that keep the gate undecidable ---
    if not report.quota_findings.get("resolved", False):
        blockers.append({
            "code": "T063_QUOTA",
            "task": "T063",
            "summary": "effective NVIDIA_RTX_PRO_6000 quota unknown",
            "detail": (
                "The quota family for the accelerator required by the G1-selected route "
                "was returned with an empty details object and no numeric value. Neither "
                ">= 1 nor 0 may be asserted."
            ),
        })
    if not report.fixtures["physical_capture_satisfied"]:
        blockers.append({
            "code": "T064_PHYSICAL_FIXTURES",
            "task": "T064",
            "summary": "no REAL_PHYSICAL fixtures",
            "detail": (
                "The canonical REAL_PHYSICAL paths hold no declared physical capture. "
                "Absence fails closed, and generated media in ./synthetic/ can never "
                "substitute — neither by declaration nor by directory placement."
            ),
        })
    if not flags["DEPLOYMENT_SUCCEEDED"]["value"]:
        blockers.append({
            "code": "T066_DEPLOYMENT",
            "task": "T066",
            "summary": "no successful Gemma deployment",
            "detail": (
                "The platform admitted the configuration and real attempts were made, but "
                "no deployed model ever existed. PLATFORM_SUPPORTED and PLATFORM_ATTEMPTED "
                "are true; DEPLOYMENT_SUCCEEDED is false."
            ),
        })
    if not flags["INFERENCE_SUCCEEDED"]["value"] or not qualifying:
        blockers.append({
            "code": "T066_INFERENCE",
            "task": "T066",
            "summary": "zero successful qualifying Gemma inference records",
            "detail": (
                f"{len(records)} inference record(s) present, {len(qualifying)} of them "
                "qualifying. A record qualifies only if it came from a REAL_PHYSICAL "
                "fixture and normalized into the closed observation domain."
            ),
        })

    # --- operational holds: deliberate controls, not technical infeasibility ---
    if report.billing_state.get("billing_enabled") is False:
        holds.append({
            "code": "BILLING_DISABLED",
            "summary": (
                "billing intentionally disabled on "
                f"{report.billing_state.get('project_id', 'the project')}"
            ),
            "intentional": True,
            "financial_safety_control": True,
            "infrastructure_defect": False,
            "blocks_g1_decision": False,
            "detail": (
                "A deliberate cost control adopted to stop an uncancellable GPU "
                "provisioning operation from accruing cost. Vertex calls now return "
                "PERMISSION_DENIED/BILLING_DISABLED. This says nothing about whether "
                "Gemma is technically feasible and is not evidence of model infeasibility."
            ),
        })

    report.decision_blockers = blockers
    report.operational_holds = holds
    report.verdict_reasoning = [f"{b['code']}: {b['summary']}" for b in blockers]

    if blockers:
        report.verdict = "NOT_YET_DECIDABLE"
    else:  # pragma: no cover - requires a live deployed route and physical fixtures
        all_matched = all(r.matched_expected for r in qualifying)
        all_stable = all(v["stable"] for v in report.stability.values())
        report.verdict = "GO" if (all_matched and all_stable) else "FALLBACK"
        report.verdict_reasoning = [
            f"qualifying_records={len(qualifying)}",
            f"all_fixtures_matched_expected={all_matched}",
            f"all_fixtures_stable_across_repeats={all_stable}",
        ]
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="G1 Gemma feasibility probe")
    parser.add_argument("--route", choices=[r.value for r in ServingRoute])
    parser.add_argument("--endpoint", help="Endpoint URL or Model Garden model id")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--out", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--repeat", type=int, default=3, help="Attempts per fixture")
    parser.add_argument(
        "--access-check-only",
        action="store_true",
        help="Record environment/access state without contacting any route",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Regenerate evidence with no subprocess and no network call whatsoever",
    )
    args = parser.parse_args(argv)

    records: list[ProbeRecord] = []
    route = ServingRoute(args.route) if args.route else None

    if not args.access_check_only:
        if args.offline:
            parser.error("--offline cannot probe a live route; use --access-check-only")
        if route is None or not args.endpoint:
            parser.error("--route and --endpoint are required unless --access-check-only")
        missing = [n for n in EXPECTED_BY_FIXTURE if not (args.fixtures / n).exists()]
        if missing:
            parser.error(f"missing physical fixtures: {missing}; capture them first (T064)")
        for name in EXPECTED_BY_FIXTURE:
            for attempt in range(1, args.repeat + 1):
                records.append(probe_fixture(route, args.endpoint, args.fixtures / name, attempt))

    report = build_report(
        route, args.endpoint, args.fixtures, records, offline=args.offline
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(asdict(report), indent=2, sort_keys=True), encoding="utf-8")
    print(f"G1 verdict: {report.verdict}")
    for reason in report.verdict_reasoning:
        print(f"  - {reason}")
    print(f"evidence written: {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
