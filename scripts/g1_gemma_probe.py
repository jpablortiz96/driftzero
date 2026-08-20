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

    # Probe a live route once fixtures exist and access is granted:
    python scripts/g1_gemma_probe.py \\
        --route cloud_run_vllm --endpoint https://... --repeat 3
"""

from __future__ import annotations

import argparse
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

from driftzero.models.verification import ObservedPosition  # noqa: E402

DEFAULT_EVIDENCE = REPO_ROOT / "evidence" / "g1_gemma_feasibility.json"
DEFAULT_FIXTURES = REPO_ROOT / "fixtures" / "multimodal"

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
    data_classification: dict[str, Any] = field(default_factory=dict)


def _run(cmd: Sequence[str], timeout: int = 30) -> tuple[int, str]:
    """Run a read-only command, returning (exit code, trimmed output)."""
    try:
        proc = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - env dependent
        return 1, f"{type(exc).__name__}: {exc}"


def collect_access_checks() -> dict[str, Any]:
    """T061/T062/T063 access checks. Read-only: configures and deploys nothing."""
    checks: dict[str, Any] = {}

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


def collect_fixture_status(fixtures_dir: Path) -> dict[str, Any]:
    """T064 physical fixture presence. Never fabricates a capture."""
    present = {}
    for name in EXPECTED_BY_FIXTURE:
        path = fixtures_dir / name
        present[name] = {
            "path": str(path.relative_to(REPO_ROOT)) if path.exists() else str(path),
            "present": path.exists(),
            "bytes": path.stat().st_size if path.exists() else 0,
        }
    return {
        "directory": str(fixtures_dir),
        "directory_exists": fixtures_dir.exists(),
        "required": present,
        "capture_type_required": "REAL_PHYSICAL_CAPTURE",
        "note": (
            "Synthetic images may not substitute for the required physical capture. "
            "The observation must come from visible evidence only — no filename, EXIF, "
            "directory, watermark, or prompt hint may encode the answer."
        ),
    }


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
    """One measured inference attempt against a live route."""
    expected = EXPECTED_BY_FIXTURE[image_path.name]
    started = time.perf_counter()
    try:
        response = invoke_route(route, endpoint, image_path)
        latency = time.perf_counter() - started
    except Exception as exc:  # noqa: BLE001 - probe records failures rather than raising
        return ProbeRecord(
            fixture_id=image_path.name,
            fixture_classification="REAL_PHYSICAL",
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
            fixture_classification="REAL_PHYSICAL",
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
        fixture_classification="REAL_PHYSICAL",
        expected_observation=str(expected),
        raw_output=raw,
        normalized_output=str(normalized),
        normalization_succeeded=True,
        latency_seconds=round(latency, 4),
        latency_label=str(AccessState.ACTUAL_OBSERVED),
        matched_expected=normalized is expected,
        attempt=attempt,
    )


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
) -> ProbeReport:
    report = ProbeReport(
        generated_at=datetime.now(UTC).isoformat(),
        serving_route=str(route) if route else None,
        route_config={
            "endpoint": endpoint,
            "prompt": PROMPT,
            "max_tokens": 8,
            "temperature": 0,
            "quantization": str(AccessState.NOT_YET_DECIDABLE),
        },
        environment={
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        access_checks=collect_access_checks(),
        fixtures=collect_fixture_status(fixtures_dir),
        records=[asdict(r) for r in records],
        stability=summarize_stability(records),
        data_classification={
            "labels": ["REAL"],
            "note": (
                "Access-check results are REAL observations of this environment. "
                "No inference was performed unless records are present below."
            ),
        },
    )

    blockers: list[str] = []
    checks = report.access_checks
    if not checks.get("gcloud_authenticated", {}).get("authenticated"):
        blockers.append("T061/T062: no credentialed gcloud account — model access and "
                        "licence acceptance cannot be verified")
    if not checks.get("gcp_project", {}).get("configured"):
        blockers.append("T062/T063: no GCP project configured — serving route and GPU "
                        "quota cannot be evaluated")
    if not all(f["present"] for f in report.fixtures["required"].values()):
        blockers.append("T064: physical fixtures not captured — real box/label photos "
                        "require human capture and cannot be synthesized")
    if not records:
        blockers.append("T066: no inference performed, so distinguishability is unmeasured")

    if blockers:
        report.verdict = "NOT_YET_DECIDABLE"
        report.verdict_reasoning = blockers
    else:  # pragma: no cover - requires live access
        all_matched = all(r.matched_expected for r in records)
        all_stable = all(v["stable"] for v in report.stability.values())
        report.verdict = "GO" if (all_matched and all_stable) else "FALLBACK"
        report.verdict_reasoning = [
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
    args = parser.parse_args(argv)

    records: list[ProbeRecord] = []
    route = ServingRoute(args.route) if args.route else None

    if not args.access_check_only:
        if route is None or not args.endpoint:
            parser.error("--route and --endpoint are required unless --access-check-only")
        missing = [n for n in EXPECTED_BY_FIXTURE if not (args.fixtures / n).exists()]
        if missing:
            parser.error(f"missing physical fixtures: {missing}; capture them first (T064)")
        for name in EXPECTED_BY_FIXTURE:
            for attempt in range(1, args.repeat + 1):
                records.append(probe_fixture(route, args.endpoint, args.fixtures / name, attempt))

    report = build_report(route, args.endpoint, args.fixtures, records)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(asdict(report), indent=2, sort_keys=True), encoding="utf-8")
    print(f"G1 verdict: {report.verdict}")
    for reason in report.verdict_reasoning:
        print(f"  - {reason}")
    print(f"evidence written: {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
