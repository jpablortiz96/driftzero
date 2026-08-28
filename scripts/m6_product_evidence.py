"""M6 — capture the product surface against real backend state.

Drives the real hero flow through the real API on a local instance of the deployed
application, then screenshots the actual rendered pages at phone and desktop widths.

Every screenshot is of a page bound to real workflow state: the FAIL screenshot is taken
after the deterministic comparator actually returned FAIL, and the verified screenshot
after a real PROOF_COMPLETE. Nothing is staged.

Offline: the two models are deterministic substitutes, so no billable call is made. The
presentation is what is under test here, and T105/T106 already evidenced the live model.

Run:  python -m scripts.m6_product_evidence
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

EVIDENCE = REPO_ROOT / "evidence" / "m6"
FIXTURES = REPO_ROOT / "fixtures"
HERO_FIXTURE = FIXTURES / "hero_change.json"
LEFT_IMG = FIXTURES / "multimodal" / "label_left_01.jpg"
TOP_RIGHT_IMG = FIXTURES / "multimodal" / "label_top_right_01.jpg"

PHONE = {"width": 375, "height": 812}
TABLET = {"width": 768, "height": 1024}
DESKTOP = {"width": 1440, "height": 900}


def _chromium() -> str | None:
    """The newest locally installed Chromium, or None to let playwright decide."""
    root = pathlib.Path.home() / "AppData" / "Local" / "ms-playwright"
    if not root.is_dir():
        return None
    builds = sorted(
        (p for p in root.glob("chromium-*") if p.is_dir()),
        key=lambda p: int(p.name.rsplit("-", 1)[-1]) if p.name.rsplit("-", 1)[-1].isdigit() else 0,
    )
    for build in reversed(builds):
        exe = build / "chrome-win64" / "chrome.exe"
        if exe.is_file():
            return str(exe)
    return None


def build_app() -> Any:
    """A real application instance, with the models substituted deterministically."""
    from driftzero.agents import field_verify as fv
    from driftzero_api.app import create_app
    from driftzero_api.runtime import ApiRuntime
    from tests.integration.test_restart_persistence import OfflineGemma

    os.environ["DRIFTZERO_FIELD_PROVIDER"] = "vertex_maas"
    gemma = OfflineGemma(["LEFT", "TOP_RIGHT"])
    fv.register_field_observation_provider(lambda _c: gemma)

    runtime = ApiRuntime(fixtures_dir=FIXTURES, workflow_namespace="wf-m6")
    return create_app(runtime), runtime, gemma


def serve(app: Any, port: int) -> Any:
    """Run the real ASGI app so a browser can drive it."""
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(80):
        if server.started:
            return server
        time.sleep(0.25)
    raise SystemExit("the application did not start")


def drive(runtime: Any, workflow_id: str, image: pathlib.Path) -> Any:
    service = runtime.live_service(workflow_id)
    service.submit_field_evidence(
        image.read_bytes(), declared_filename=image.name, declared_content_type="image/jpeg"
    )
    return service.generate_proof()


def capture() -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    from tests.integration._pilot import arm_for_service, clear_change_intelligence

    app, runtime, gemma = build_app()
    port = 8731
    server = serve(app, port)
    base = f"http://127.0.0.1:{port}"

    payload = {
        k: v
        for k, v in json.loads(HERO_FIXTURE.read_text(encoding="utf-8")).items()
        if not k.startswith("_")
    }
    workflow_id = runtime.accept_change(payload)["workflow_id"]
    service = runtime.live_service(workflow_id)
    arm_for_service(service)
    service.analyze_change()
    service.deploy_change()
    service.deliver_to_frontline()

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    shots: list[dict[str, Any]] = []
    observations: dict[str, Any] = {}

    def shot(pageobj: Any, name: str, viewport: dict[str, int], url: str) -> None:
        pageobj.set_viewport_size(viewport)
        pageobj.goto(url, wait_until="networkidle")
        pageobj.wait_for_timeout(400)
        path = EVIDENCE / name
        pageobj.screenshot(path=str(path), full_page=True)
        # A page wider than its viewport means the worker has to scroll sideways.
        overflow = pageobj.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        status_word = pageobj.evaluate(
            "() => (document.getElementById('status-word') || {}).textContent || ''"
        ).strip()
        shots.append(
            {
                "file": name,
                "viewport": f"{viewport['width']}x{viewport['height']}",
                "url": url.replace(base, ""),
                "horizontal_overflow_px": overflow,
                "status_word_rendered": status_word,
                "bytes": path.stat().st_size,
            }
        )
        observations[name] = {"overflow": overflow, "status": status_word}

    with sync_playwright() as pw:
        # Prefer whatever Chromium is already installed. Pinning to the exact build this
        # playwright ships with would make the evidence depend on a download rather than
        # on the product, and any recent Chromium renders this page identically.
        browser = pw.chromium.launch(executable_path=_chromium())
        pageobj = browser.new_page()

        delta_url = f"{base}/web/delta?workflow={workflow_id}"
        verify_url = f"{base}/web/verify?workflow={workflow_id}"
        workflow_url = f"{base}/web/workflow?workflow={workflow_id}"
        proof_url = f"{base}/web/proof?workflow={workflow_id}"

        shot(pageobj, "worker_mobile.png", PHONE, delta_url)
        shot(pageobj, "worker_tablet.png", TABLET, delta_url)

        # A real FAIL, produced by the deterministic comparator.
        failed = drive(runtime, workflow_id, LEFT_IMG)
        shot(pageobj, "worker_failed.png", PHONE, verify_url)

        # A real PASS, and a real PROOF_COMPLETE.
        passed = drive(runtime, workflow_id, TOP_RIGHT_IMG)
        shot(pageobj, "worker_verified.png", PHONE, verify_url)
        shot(pageobj, "proof_view.png", PHONE, proof_url)
        shot(pageobj, "desktop.png", DESKTOP, workflow_url)
        shot(pageobj, "proof_desktop.png", DESKTOP, proof_url)

        # Accessibility observations, taken from the rendered document.
        pageobj.set_viewport_size(PHONE)
        pageobj.goto(verify_url, wait_until="networkidle")
        accessibility = pageobj.evaluate(
            """() => {
              const labelled = [...document.querySelectorAll('button, a.action, input')]
                .map(el => ({
                  tag: el.tagName.toLowerCase(),
                  name: (el.textContent || '').trim()
                    || el.getAttribute('aria-label')
                    || (document.querySelector(`label[for="${el.id}"]`) || {}).textContent
                    || '',
                }));
              const tap = [...document.querySelectorAll('button, a.action, label.action')]
                .map(el => Math.round(el.getBoundingClientRect().height));
              return {
                lang: document.documentElement.lang,
                headings: [...document.querySelectorAll('h1,h2')].map(h => h.tagName),
                live_regions: document.querySelectorAll('[aria-live]').length,
                alert_regions: document.querySelectorAll('[role="alert"]').length,
                controls: labelled,
                unlabelled: labelled.filter(c => !c.name).length,
                smallest_target_px: Math.min(...tap),
                file_input_in_a11y_tree:
                  getComputedStyle(document.getElementById('photo')).display !== 'none',
              };
            }"""
        )

        # Keyboard operability: tab through and confirm focus lands on real controls.
        focus_order = []
        for _ in range(6):
            pageobj.keyboard.press("Tab")
            focus_order.append(
                pageobj.evaluate(
                    "() => document.activeElement ? document.activeElement.tagName.toLowerCase()"
                    " + (document.activeElement.id ? '#' + document.activeElement.id : '') : ''"
                )
            )
        browser.close()

    status = runtime.status(workflow_id)
    clear_change_intelligence()
    server.should_exit = True

    return {
        "workflow_id": workflow_id,
        "screenshots": shots,
        "observations": observations,
        "accessibility": {**accessibility, "focus_order": focus_order},
        "backend_state": {
            "final_state": status["state"],
            "verification_results": status["verification_results"],
            "delta_present": status["delta"] is not None,
            "delivery_established": status["delivery_established"],
            "proof_id": status["proof_id"],
        },
        "fail_then_pass": {
            "fail_blocked_proof": failed["proof"]["generated"] is False,
            "pass_generated_proof": passed["proof"]["generated"] is True,
        },
        "provider_calls": gemma.calls,
        "live_model_calls": 0,
    }


def run_tests() -> dict[str, Any]:
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/integration/test_web_surface.py", "-q"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    lines = [ln for ln in done.stdout.splitlines() if "passed" in ln or "failed" in ln]
    return {"passed": done.returncode == 0,
            "summary": lines[-1].strip() if lines else "no summary"}


def credential_scan() -> dict[str, Any]:
    patterns = {
        "oauth": r"ya29\.[A-Za-z0-9_\-]{20,}",
        "bearer": r"Bearer\s+[A-Za-z0-9._\-]{20,}",
        "key": r"AIza[A-Za-z0-9_\-]{30,}",
        "pem": r"BEGIN [A-Z ]*PRIVATE KEY",
    }
    findings = []
    for path in sorted(EVIDENCE.glob("*.json")):
        text = path.read_text(encoding="utf-8", errors="replace")
        findings.extend(
            f"{path.name}:{label}" for label, pattern in patterns.items()
            if re.search(pattern, text)
        )
    return {"findings": findings, "clean": not findings}


def main() -> int:
    result = capture()
    EVIDENCE.mkdir(parents=True, exist_ok=True)

    a11y = result["accessibility"]
    bundle: dict[str, Any] = {
        "product_smoke.json": {
            "task": "M6 (T127-T130)",
            "evidence_class": "OFFLINE_DETERMINISTIC_PRESENTATION",
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "note": (
                "The presentation is bound to real backend state produced by the real "
                "application seam. The two models are deterministic substitutes; the "
                "live model is evidenced by T105/T106, not re-spent here."
            ),
            "workflow_id": result["workflow_id"],
            "backend_state": result["backend_state"],
            "fail_then_pass": result["fail_then_pass"],
            "screenshots": result["screenshots"],
            "provider_calls": result["provider_calls"],
            "live_model_calls": 0,
        },
        "accessibility.json": {
            "standard": "spec § Frontline Surface Minimums (S1 boundary)",
            "not_a_claim": (
                "These are the minimum interaction requirements the spec sets, checked "
                "on the rendered page. They are not a WCAG conformance claim and not a "
                "device-support claim."
            ),
            "document_language": a11y["lang"],
            "heading_structure": a11y["headings"],
            "live_regions": a11y["live_regions"],
            "alert_regions": a11y["alert_regions"],
            "controls_without_an_accessible_name": a11y["unlabelled"],
            "smallest_touch_target_px": a11y["smallest_target_px"],
            "file_input_reachable": a11y["file_input_in_a11y_tree"],
            "keyboard_focus_order": a11y["focus_order"],
            "status_communicated_as_text": {
                shot["file"]: shot["status_word_rendered"]
                for shot in result["screenshots"]
                if shot["status_word_rendered"]
            },
            "horizontal_overflow_px": {
                shot["file"]: shot["horizontal_overflow_px"] for shot in result["screenshots"]
            },
        },
        "test_summary.json": run_tests(),
    }
    bundle["run_summary.json"] = {
        "batch": "M6_PRODUCT",
        "tasks": "T127, T128, T129, T130",
        "timestamp": bundle["product_smoke.json"]["timestamp"],
        "final_state": result["backend_state"]["final_state"],
        "verification_results": result["backend_state"]["verification_results"],
        "screenshots": len(result["screenshots"]),
        "max_horizontal_overflow_px": max(
            s["horizontal_overflow_px"] for s in result["screenshots"]
        ),
        "controls_without_an_accessible_name": a11y["unlabelled"],
        "smallest_touch_target_px": a11y["smallest_target_px"],
        "tests": bundle["test_summary.json"]["summary"],
        "live_model_calls": 0,
    }

    written: list[pathlib.Path] = []
    for name, payload in bundle.items():
        path = EVIDENCE / name
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
        written.append(path)

    scan = credential_scan()
    scan_path = EVIDENCE / "credential_scan.json"
    with scan_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(scan, indent=2, sort_keys=True) + "\n")
    written.append(scan_path)

    files = sorted(p for p in EVIDENCE.iterdir() if p.name != "SHA256SUMS.txt")
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in files]
    with (EVIDENCE / "SHA256SUMS.txt").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")

    print(json.dumps(bundle["run_summary.json"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
