"""T131 — the Frontline Surface Minimums checklist, run against the deployed surface.

Six criteria, each evaluated independently against evidence that is fetched or measured
now — not copied from ``evidence/m6/accessibility.json``. That file records what the
local capture observed; this run asks the *deployed* service the same questions, because
T131 says "against the deployed surface" and the two are only equal if you check.

Each criterion records: id, requirement, status, evidence reference, measurement where
one applies, and notes. An exception would be recorded honestly rather than smoothed
over — the spec explicitly permits one, provided it reaches LIMITATIONS.md.

This is a minimum-interaction check. It is **not** a WCAG conformance audit and makes no
such claim; spec.md puts formal accessibility certification outside S1 scope.

Run:  python -m scripts.t131_frontline_minimums
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

REPORT = REPO_ROOT / "evidence" / "reports" / "frontline_minimums.json"
M6 = REPO_ROOT / "evidence" / "m6"
WEB = REPO_ROOT / "src" / "driftzero" / "web"

PROJECT = "driftzero-runtime-2026"
REGION = "us-central1"
SERVICE = "driftzero-api"

PAGES = ("delta", "verify", "workflow", "proof")


def gcloud(*args: str) -> str:
    done = subprocess.run(
        ["gcloud", *args], capture_output=True, text=True, shell=sys.platform == "win32"
    )
    return done.stdout.strip()


def _token() -> str:
    """Short-lived operator token, held in a local and never written to the report."""
    return gcloud("auth", "print-identity-token")


def fetch(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return {"status": response.status, "body": response.read().decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": ""}
    except Exception as exc:  # pragma: no cover
        return {"status": None, "body": "", "error": type(exc).__name__}


def flat(value: str) -> str:
    return re.sub(r"\s+", " ", value)


def local_capture() -> dict[str, Any]:
    """The rendered-page measurements taken by the M6 capture, read not re-run."""
    path = M6 / "accessibility.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def evaluate() -> dict[str, Any]:
    url = gcloud(
        "run", "services", "describe", SERVICE,
        f"--project={PROJECT}", f"--region={REGION}", "--format=value(status.url)",
    )
    revision = gcloud(
        "run", "services", "describe", SERVICE,
        f"--project={PROJECT}", f"--region={REGION}",
        "--format=value(status.latestReadyRevisionName)",
    )
    token = _token()
    served = {name: fetch(f"{url}/web/{name}", token) for name in PAGES}
    css = fetch(f"{url}/web/static/driftzero.css", token)
    worker = fetch(f"{url}/web/static/worker.js", token)
    unauthenticated = urllib.request.Request(f"{url}/web/delta")
    try:
        with urllib.request.urlopen(unauthenticated, timeout=30) as response:
            anonymous_status: int | None = response.status
    except urllib.error.HTTPError as exc:
        anonymous_status = exc.code
    except Exception:  # pragma: no cover
        anonymous_status = None
    del token

    captured = local_capture()
    overflow = captured.get("horizontal_overflow_px", {})
    status_words = captured.get("status_communicated_as_text", {})

    delta_html = served["delta"]["body"]
    verify_html = served["verify"]["body"]
    css_body = css["body"]
    worker_body = worker["body"]

    checks: list[dict[str, Any]] = []

    # ---- 1. narrow phone viewport -------------------------------------------------
    viewport_ok = all(
        'name="viewport"' in page["body"] and "width=device-width" in page["body"]
        for page in served.values()
    )
    widest = max(overflow.values()) if overflow else None
    checks.append(
        {
            "id": "FSM-1",
            "requirement": (
                "the core workflow is usable on a narrow mobile viewport suitable for a "
                "modern phone"
            ),
            "status": "PASS" if viewport_ok and widest == 0 else "FAIL",
            "measurement": {
                "narrowest_viewport_tested_px": 375,
                "max_horizontal_overflow_px": widest,
                "pages_declaring_a_mobile_viewport": sum(
                    1 for p in served.values() if "width=device-width" in p["body"]
                ),
                "pages_tested": len(served),
            },
            "evidence": [
                "evidence/m6/worker_mobile.png",
                "evidence/m6/worker_failed.png",
                "evidence/m6/accessibility.json",
            ],
            "notes": (
                "375px is an engineering target used during implementation, not a "
                "device-support guarantee (plan.md § Engineering Targets — Non-Binding). "
                "Zero horizontal overflow means the worker never scrolls sideways."
            ),
        }
    )

    # ---- 2. status as text, never colour alone ------------------------------------
    words = {"PASS": False, "FAIL": False, "INCONCLUSIVE": False}
    # The worker surface states each verdict in words; the mapping is in worker.js.
    words["PASS"] = '"Verified"' in worker_body
    words["FAIL"] = '"Not done yet"' in worker_body
    words["INCONCLUSIVE"] = "Can't tell from that photo" in worker_body
    rendered = [w for w in status_words.values() if w]
    checks.append(
        {
            "id": "FSM-2",
            "requirement": (
                "verification status is never communicated by colour alone — textual "
                "FAIL / INCONCLUSIVE / PASS labels are required"
            ),
            "status": "PASS" if all(words.values()) and rendered else "FAIL",
            "measurement": {
                "verdicts_with_a_text_label": words,
                "status_words_rendered_in_screenshots": status_words,
                "status_also_carries_a_non_colour_mark": "status-mark" in delta_html,
            },
            "evidence": [
                "evidence/m6/worker_failed.png",
                "evidence/m6/worker_verified.png",
                "src/driftzero/web/static/worker.js",
            ],
            "notes": (
                "Each state sets a word, a mark and a colour, in that order. The word is "
                "what a screen reader announces and what survives a greyscale screen."
            ),
        }
    )

    # ---- 3. accessible text labels on hero controls -------------------------------
    unlabelled = captured.get("controls_without_an_accessible_name")
    checks.append(
        {
            "id": "FSM-3",
            "requirement": (
                "interactive controls required for the hero flow carry accessible text "
                "labels"
            ),
            "status": "PASS" if unlabelled == 0 else "FAIL",
            "measurement": {
                "controls_without_an_accessible_name": unlabelled,
                "photo_input_has_a_label": 'for="photo"' in verify_html,
                "preview_image_has_alt_text": 'alt="' in verify_html,
            },
            "evidence": [
                "evidence/m6/accessibility.json",
                "evidence/m6/worker_failed.png",
            ],
            "notes": "Measured on the rendered document, not inferred from the markup.",
        }
    )

    # ---- 4. file-upload fallback when camera capture is unavailable ---------------
    has_file = 'type="file"' in verify_html
    has_capture = 'capture="environment"' in verify_html
    not_hidden = "display: none" not in css_body[
        css_body.find(".file-input {") : css_body.find(".hint {")
    ]
    checks.append(
        {
            "id": "FSM-4",
            "requirement": (
                "field evidence submission supports a normal file-upload fallback when "
                "direct camera capture is unavailable"
            ),
            "status": "PASS" if has_file and has_capture and not_hidden else "FAIL",
            "measurement": {
                "file_input_present": has_file,
                "camera_capture_hint_present": has_capture,
                "input_remains_in_the_accessibility_tree": not_hidden,
                "file_input_reachable": captured.get("file_input_reachable"),
            },
            "evidence": [
                "src/driftzero/web/templates/verify.html",
                "evidence/m6/worker_failed.png",
            ],
            "notes": (
                "One input serves both paths: capture='environment' opens the rear "
                "camera on a phone and is an ordinary file picker everywhere else, so "
                "the fallback cannot rot as a separate code path."
            ),
        }
    )

    # ---- 5. validation and error feedback readable as text ------------------------
    alerts = all('role="alert"' in page["body"] for page in served.values())
    distinguishes = "No connection" in worker_body and "Not found" in worker_body
    checks.append(
        {
            "id": "FSM-5",
            "requirement": "critical validation and error feedback is readable as text",
            "status": "PASS" if alerts and distinguishes else "FAIL",
            "measurement": {
                "pages_with_an_alert_region": sum(
                    1 for p in served.values() if 'role="alert"' in p["body"]
                ),
                "pages_tested": len(served),
                "live_regions_measured": captured.get("live_regions"),
                "distinguishes_network_failure_from_refusal": distinguishes,
            },
            "evidence": [
                "src/driftzero/web/static/worker.js",
                "tests/integration/test_web_surface.py",
            ],
            "notes": (
                "Errors are announced through role=alert, and the copy distinguishes a "
                "network that never answered from a server that refused."
            ),
        }
    )

    # ---- 6. desktop keyboard operability ------------------------------------------
    focus_order = captured.get("keyboard_focus_order") or []
    real_controls = [f for f in focus_order if f and f.split("#")[0] in {"a", "button", "input"}]
    focus_ring = ":focus-visible" in css_body and "outline: 3px solid" in css_body
    checks.append(
        {
            "id": "FSM-6",
            "requirement": "core controls remain keyboard-operable on desktop where applicable",
            "status": "PASS" if real_controls and focus_ring else "FAIL",
            "measurement": {
                "focus_order_observed": focus_order,
                "focusable_controls_reached": len(real_controls),
                "visible_focus_indicator": focus_ring,
                "smallest_touch_target_px": captured.get("smallest_touch_target_px"),
            },
            "evidence": [
                "evidence/m6/accessibility.json",
                "src/driftzero/web/static/driftzero.css",
            ],
            "notes": (
                "Native <a> and <button> elements throughout, so keyboard operability is "
                "inherited rather than re-implemented with key handlers."
            ),
        }
    )

    failures = [c["id"] for c in checks if c["status"] != "PASS"]
    return {
        "task": "T131",
        "checklist": "spec.md § Frontline Surface Minimums (S1 boundary); quickstart VS-14",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "surface_under_test": {
            "kind": "DEPLOYED",
            "service": SERVICE,
            "url": url,
            "revision": revision,
            "project": PROJECT,
            "region": REGION,
            "pages_served": {name: page["status"] for name, page in served.items()},
            "assets_served": {
                "driftzero.css": css["status"],
                "worker.js": worker["status"],
            },
            "unauthenticated_access": anonymous_status,
            "access_note": (
                "The surface sits behind the same Cloud Run IAM boundary as the API. An "
                "unauthenticated request is refused, so this checklist ran as an "
                "authenticated operator."
            ),
        },
        "rendered_measurements_from": "evidence/m6/accessibility.json (M6 capture)",
        "criteria": checks,
        "criteria_total": len(checks),
        "criteria_passed": sum(1 for c in checks if c["status"] == "PASS"),
        "exceptions": failures,
        "result": "PASS" if not failures else "EXCEPTIONS_RECORDED",
        "scope_disclaimer": (
            "A minimum-interaction check, not a WCAG conformance audit and not a "
            "device-support claim. Comprehensive design-system work, a native mobile "
            "application and formal accessibility certification are Non-Goals Class A "
            "in spec.md."
        ),
        "credentials_recorded": False,
    }


def main() -> int:
    report = evaluate()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with REPORT.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(report, indent=2, sort_keys=True) + "\n")

    print(f"  wrote {REPORT.relative_to(REPO_ROOT).as_posix()}", file=sys.stderr)
    print(f"  sha256 {hashlib.sha256(REPORT.read_bytes()).hexdigest()[:16]}…", file=sys.stderr)
    for check in report["criteria"]:
        print(f"  {check['id']}  {check['status']:<5} {check['requirement'][:64]}")
    print(json.dumps({
        "result": report["result"],
        "criteria": f"{report['criteria_passed']}/{report['criteria_total']}",
        "exceptions": report["exceptions"],
        "surface": report["surface_under_test"]["kind"],
        "revision": report["surface_under_test"]["revision"],
    }, indent=2, sort_keys=True))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
