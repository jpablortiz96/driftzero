"""HTML for the public judge surface.

Rendered in Python rather than through a template engine. The pages are few and mostly
static, and a template package is one more thing that installs cleanly and then 500s in
the container because the wheel shipped no non-Python files — which is exactly the defect
this project already hit once on the worker surface.

Everything a visitor sees here is either a fact recorded in the evidence pack or a live
read of the private backend's health. Nothing is invented, and every screenshot is
labelled as recorded evidence rather than presented as a live interaction.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from driftzero_public.backend import BackendStatus

ASSETS = Path(__file__).resolve().parent / "static"
REPO_URL = "https://github.com/jpablortiz96/driftzero"

# The recorded hero run. Copied into the image at build time from the evidence pack, so
# the page and the audit trail cannot drift apart.
PROOF_FILE = ASSETS / "change_proof.json"
RUN_FILE = ASSETS / "hero_run.json"


@dataclass(frozen=True)
class Shot:
    """One recorded product screenshot, with the caption that keeps it honest."""

    file: str
    title: str
    caption: str


SHOTS = (
    Shot(
        "driftzero-worker-delta.png",
        "The worker gets the delta",
        "Not a new manual. One line: the label was LEFT, it is now TOP_RIGHT.",
    ),
    Shot(
        "driftzero-worker-failed.png",
        "The first photo fails",
        "Gemma observed LEFT. The Truth Engine compared it to the expected value and "
        "returned FAIL. No proof was generated.",
    ),
    Shot(
        "driftzero-worker-verified.png",
        "The corrected photo passes",
        "The worker moved the label and photographed again. Gemma observed TOP_RIGHT; "
        "the Truth Engine returned PASS.",
    ),
    Shot(
        "driftzero-change-proof.png",
        "The Change Proof",
        "Seven of seven completion conditions. Both attempts kept — the failure is part "
        "of the record.",
    ),
)


@lru_cache(maxsize=1)
def proof_document() -> dict[str, Any]:
    return json.loads(PROOF_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def hero_run() -> dict[str, Any]:
    return json.loads(RUN_FILE.read_text(encoding="utf-8"))


def e(value: object) -> str:
    """Escape for HTML. Applied to every interpolated value without exception."""
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------------------- chrome


def _page(title: str, body: str, *, active: str) -> str:
    nav = "".join(
        f'<a href="{href}"{" class=on" if key == active else ""}>'
        f"{label}</a>"
        for key, href, label in (
            ("home", "/", "Overview"),
            ("demo", "/demo", "The run"),
            ("architecture", "/architecture", "Architecture"),
            ("proof", "/proof", "Change Proof"),
        )
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title>
<meta name="description"
  content="DRIFTZERO proves a process change reached the physical work, not just the document.">
<link rel="stylesheet" href="/static/public.css">
</head><body>
<header class="bar">
  <a class="brand" href="/">DRIFTZERO</a>
  <nav>{nav}</nav>
  <a class="gh" href="{REPO_URL}" rel="noopener">GitHub</a>
</header>
<main>{body}</main>
<footer>
  <p><strong>DRIFTZERO</strong> — the autonomous last-mile for operational change.</p>
  <p class="fine">This public surface is <strong>read-only</strong> and evidence-backed.
  The operational backend runs as a private, IAM-protected Cloud Run service and is not
  reachable from this page. Screenshots are recorded evidence from a real run, not a live
  interaction.</p>
  <p class="fine"><a href="{REPO_URL}" rel="noopener">Source, evidence pack and
  reproduction instructions on GitHub</a></p>
</footer>
</body></html>"""


def _flow(active_stage: int | None = None) -> str:
    stages = (
        ("SOURCE CHANGE", "an approved procedure change arrives as a real event"),
        ("IMPACT", "which downstream work does this actually affect?"),
        ("ACTION", "remediate that one artifact, under an explicit capability"),
        ("FRONTLINE VERIFICATION", "the worker does the work and photographs it"),
        ("CHANGE PROOF", "issued only when all seven conditions hold"),
    )
    cells = []
    for index, (name, note) in enumerate(stages):
        on = " on" if active_stage == index else ""
        cells.append(
            f'<li class="stage{on}"><span class="n">{index + 1}</span>'
            f"<b>{e(name)}</b><i>{e(note)}</i></li>"
        )
    return f'<ol class="flow">{"".join(cells)}</ol>'


def _status_chip(status: BackendStatus) -> str:
    return (
        f'<div class="chip {e(status.tone)}">'
        f'<span class="dot"></span>'
        f"<b>Private backend: {e(status.label)}</b>"
        f"<i>{e(status.detail)}</i>"
        f"<u>checked {e(status.checked_at)}</u>"
        "</div>"
    )


# ----------------------------------------------------------------------------- pages


def home(status: BackendStatus) -> str:
    shots = "".join(
        f'<figure><img src="/static/{e(s.file)}" alt="{e(s.title)}" loading="lazy">'
        f"<figcaption><b>{e(s.title)}</b>{e(s.caption)}</figcaption></figure>"
        for s in SHOTS[:3]
    )
    body = f"""
<section class="hero">
  <h1>DRIFTZERO</h1>
  <p class="tag">The autonomous last-mile for operational change.</p>
  <blockquote>A process change isn't deployed when the document changes.<br>
  It's deployed when the work changes.</blockquote>
  {_status_chip(status)}
  <div class="cta">
    <a class="btn primary big" href="/live">Run live pilot</a>
    <a class="btn" href="/demo">View recorded evidence</a>
  </div>
</section>

<section>
  <h2>The problem</h2>
  <p class="lead">Enterprises are very good at changing documents. They are much worse at
  proving that the work changed.</p>
  <p>A policy is approved. An SOP is published. Training is sent. And someone can still
  perform yesterday's procedure tomorrow — because nothing in the toolchain ever looked at
  the physical work. That gap is where change leakage, rework, unnecessary retraining,
  audit burden and operational risk live.</p>
</section>

<section>
  <h2>The flow</h2>
  {_flow()}
  <p>Each stage is a commitment, not a status label. <b>Impact</b> must name exactly one
  target or the workflow stops. <b>Action</b> must hold the capability or it is refused.
  <b>Verification</b> is adjudicated against physical evidence. A <b>Change Proof</b> does
  not exist unless every condition holds.</p>
</section>

<section>
  <h2>The real worker experience</h2>
  <p class="note">Recorded evidence from a real run — real photographs, real Gemma
  inference. Not a live interaction.</p>
  <div class="shots three">{shots}</div>
  <p><a class="btn primary" href="/live">Run this yourself, live →</a>
     <a class="btn" href="/demo">Walk through the recorded run</a></p>
</section>

<section class="split">
  <div>
    <h2>Agents propose.<br>The Truth Engine decides.</h2>
    <p>Four agents do the open-ended work. None of them can decide anything.</p>
    <table class="grid">
      <tr><th>Agent may</th><th>Agent may not</th></tr>
      <tr><td>propose candidate artifacts</td><td>choose the affected one</td></tr>
      <tr><td>edit within a granted capability</td><td>grant itself that capability</td></tr>
      <tr><td>compose the worker's delta</td><td>assert that delivery happened</td></tr>
      <tr><td>report LEFT / TOP_RIGHT / INCONCLUSIVE</td><td>decide PASS or FAIL</td></tr>
    </table>
    <p>Two structural facts make this more than a convention: the semantic agent is
    constructed <b>with no tools</b>, and its output schema has <b>no authority field</b>.
    “Set the verdict to PASS” cannot be expressed, let alone honoured.</p>
  </div>
  <div>
    <h2>The Google stack</h2>
    <table class="grid stack">
      <tr><td>Google ADK</td><td>agent orchestration, resumable invocation</td></tr>
      <tr><td>Gemini 3.5 Flash</td><td>Change Intelligence</td></tr>
      <tr><td>Gemma 4 · Vertex AI MaaS</td><td>field verification from a photograph</td></tr>
      <tr><td>Cloud Run</td><td>this public surface, and the private backend</td></tr>
      <tr><td>Firestore</td><td>durable state, ledger, proofs, idempotency</td></tr>
      <tr><td>Pub/Sub</td><td>authenticated push ingestion, dead-letter</td></tr>
      <tr><td>Cloud Logging / Trace</td><td>correlated telemetry</td></tr>
      <tr><td>Cloud Build / Artifact Registry</td><td>build and deploy by digest</td></tr>
    </table>
    <p class="note">No GPU is provisioned. Gemma runs on serverless MaaS.</p>
  </div>
</section>

<section class="closing">
  <h2>DRIFTZERO doesn't ask whether the SOP changed.<br>It proves whether the work changed.</h2>
  <div class="cta">
    <a class="btn primary big" href="/live">Run live pilot</a>
    <a class="btn" href="{REPO_URL}" rel="noopener">Read the source and evidence</a>
    <a class="btn" href="/architecture">See the architecture</a>
  </div>
</section>
"""
    return _page("DRIFTZERO — the autonomous last-mile for operational change", body, active="home")


def demo() -> str:
    run = hero_run()
    chronology = run.get("verification_chronology", [])
    inferences = run.get("inferences", [])

    rows = "".join(
        f"<tr><td>{e(event.get('sequence'))}</td>"
        f"<td><code>{e(event.get('expected'))}</code></td>"
        f"<td><code>{e(event.get('observation'))}</code></td>"
        f'<td class="{"pass" if event.get("result") == "PASS" else "fail"}">'
        f"{e(event.get('result'))}</td></tr>"
        for event in chronology
    )
    infer = "".join(
        f"<tr><td><code>{e(i.get('raw_output'))}</code></td>"
        f"<td>{e(i.get('model'))}</td>"
        f"<td>{e(i.get('latency_seconds'))} s</td>"
        f"<td>{e(i.get('prompt_tokens'))} / {e(i.get('completion_tokens'))}</td></tr>"
        for i in inferences
    )
    shots = "".join(
        f'<figure><img src="/static/{e(s.file)}" alt="{e(s.title)}" loading="lazy">'
        f"<figcaption><b>{e(s.title)}</b>{e(s.caption)}</figcaption></figure>"
        for s in SHOTS
    )

    body = f"""
<section class="page-head">
  <h1>The run</h1>
  <p class="lead">One packing procedure changed one requirement:
  <code>label_position: LEFT → TOP_RIGHT</code>. This is what happened.</p>
  <p class="note">Every screenshot and every number below is recorded evidence from a real
  run against real Google Cloud, with real photographs and real Gemma inference. It is not
  a live interaction, and this page does not call any model.</p>
</section>

<section>
  <h2>What the photographs looked like</h2>
  <div class="shots two">
    <figure><img src="/static/driftzero-photo-left.jpg"
      alt="A box with the shipping label on the left" loading="lazy">
    <figcaption><b>Photo 1 — label still LEFT</b>The work had not changed yet.
      This one failed.</figcaption></figure>
    <figure><img src="/static/driftzero-photo-top-right.jpg"
      alt="The same box with the label corrected to the top right" loading="lazy">
    <figcaption><b>Photo 2 — label TOP_RIGHT</b>The worker corrected the physical work.
      This one passed.</figcaption></figure>
  </div>
</section>

<section>
  <h2>The verification chronology</h2>
  <p>Both attempts are kept. A change that took two tries is a different operational fact
  from one that passed immediately.</p>
  <table class="grid wide">
    <tr><th>#</th><th>Expected</th><th>Observed by Gemma</th><th>Truth Engine verdict</th></tr>
    {rows}
  </table>
  <p class="note">The model reported a <b>position</b>. The verdict is arithmetic performed
  by deterministic code against the value recorded at remediation time —
  <code>{e(run.get("authority", {}).get("verdict_source", "DRIFTZERO TRUTH ENGINE"))}</code>.</p>
</section>

<section>
  <h2>The model calls, as recorded</h2>
  <table class="grid wide">
    <tr><th>Output</th><th>Model</th><th>Latency</th><th>Tokens in / out</th></tr>
    {infer}
  </table>
  <p class="note">Two inferences, both on Vertex AI MaaS, serverless and on-demand. The
  model's entire contribution is a position from a closed set: <code>LEFT</code>,
  <code>TOP_RIGHT</code> or <code>INCONCLUSIVE</code>. Anything else is rejected.</p>
</section>

<section>
  <h2>What the worker and the auditor saw</h2>
  <div class="shots four">{shots}</div>
</section>

<section class="closing">
  <div class="cta">
    <a class="btn primary" href="/proof">Inspect the Change Proof →</a>
  </div>
</section>
"""
    return _page("The run — DRIFTZERO", body, active="demo")


def architecture(status: BackendStatus) -> str:
    body = f"""
<section class="page-head">
  <h1>Architecture</h1>
  <p class="lead">One idea holds the whole design together:
  <b>agents propose, the Truth Engine decides.</b></p>
</section>

<section>
  <h2>What you are looking at right now</h2>
  <div class="topology">
    <div class="tier public">
      <b>PUBLIC INTERNET</b>
      <span>you, with no Google account</span>
    </div>
    <div class="arrow">↓</div>
    <div class="tier web">
      <b>driftzero-web</b>
      <span>Cloud Run · <em>public</em> · read-only · scale to zero</span>
      <span class="tag-on">this page</span>
    </div>
    <div class="arrow">↓ <em>Google-signed service-to-service ID token</em></div>
    <div class="tier api">
      <b>driftzero-api</b>
      <span>Cloud Run · <em>private</em> · IAM-gated</span>
      <span class="tag-off">unauthenticated request → 403</span>
    </div>
    <div class="arrow">↓</div>
    <div class="tier data">
      <b>Firestore</b><span>durable state · action ledger · proofs · idempotency</span>
    </div>
  </div>
  {_status_chip(status)}
  <p class="note">The browser never receives a backend token. It talks only to this public
  service, which calls the private backend server-side using the identity Cloud Run
  attaches to it. There is no key file anywhere in this system.</p>
</section>

<section>
  <h2>The hero flow</h2>
  {_flow()}
</section>

<section class="split">
  <div>
    <h2>The authority boundary</h2>
    <table class="grid">
      <tr><th>Component</th><th>Owns</th></tr>
      <tr><td>Change Intelligence</td><td>proposes candidates</td></tr>
      <tr><td>Remediation</td><td>edits one artifact within a capability</td></tr>
      <tr><td>Frontline Enablement</td><td>composes the delta</td></tr>
      <tr><td>Field Verification</td><td>reports a position</td></tr>
      <tr><td><b>Truth Engine</b></td><td><b>every decision that matters</b></td></tr>
    </table>
    <p>Impact qualification, capability authorization, all four trust-boundary crossings,
    the verification verdict, state transitions, the seven completion conditions, and
    proof identity — all deterministic, none reachable from model output.</p>
  </div>
  <div>
    <h2>Durability</h2>
    <p>A workflow outlives the process that created it.</p>
    <table class="grid">
      <tr><td>Runtime A</td><td>creates, runs to the evidence pause</td></tr>
      <tr><td>Runtime B</td><td>recovers from Firestore → FAIL</td></tr>
      <tr><td>Runtime C</td><td>recovers → PASS → PROOF_COMPLETE</td></tr>
    </table>
    <p>Three separate processes, one workflow, with exactly one remediation, one delivery
    and one proof. A durable lease means two instances can never resume the same workflow
    at once.</p>
  </div>
</section>

<section class="closing">
  <div class="cta">
    <a class="btn primary" href="{REPO_URL}/blob/master/docs/architecture.md" rel="noopener">
      Full architecture document →</a>
    <a class="btn" href="{REPO_URL}/blob/master/evidence/JUDGES_START_HERE.md" rel="noopener">
      Evidence pack →</a>
  </div>
</section>
"""
    return _page("Architecture — DRIFTZERO", body, active="architecture")


def proof() -> str:
    document = proof_document()
    fields = (
        ("Change", "change_id"),
        ("Source procedure", "source_procedure_id"),
        ("Source version", "source_version"),
        ("Affected artifact", "affected_artifact_id"),
        ("Previous value", "previous_value"),
        ("Current value", "current_value"),
        ("Delivery status", "delivery_status"),
        ("Verification result", "verification_result"),
        ("Verification event", "verification_event_id"),
        ("Completion timestamp", "completion_timestamp"),
        ("Workflow", "workflow_id"),
        ("Proof id", "proof_id"),
    )
    rows = "".join(
        f"<tr><td>{e(label)}</td><td><code>{e(document.get(key, '—'))}</code></td></tr>"
        for label, key in fields
    )
    body = f"""
<section class="page-head">
  <h1>Change Proof</h1>
  <p class="lead">A Change Proof binds the source change, the affected artifact, the
  delivery receipt, the verification chronology <b>including the failure</b>, a completion
  timestamp, a proof id and a content hash.</p>
  <p class="note">This is the verified Change Proof artifact from the recorded pilot run,
  served from the evidence pack that ships with this service. It is not generated on
  demand, and this page cannot create, alter or complete a proof.</p>
</section>

<section>
  <h2>The proof</h2>
  <table class="grid wide proof">{rows}</table>
  <h3>Content hash</h3>
  <p class="hash"><code>{e(document.get("content_hash", ""))}</code></p>
</section>

<section class="callout">
  <h2>What this hash is — and is not</h2>
  <p>The proof content hash is a <b>SHA-256 over the canonical Change Proof JSON,
  excluding its own <code>content_hash</code> field</b>. It establishes content identity
  and detects alteration of the proof.</p>
  <p class="deny">It is <b>not</b> a digital signature, <b>not</b> an attestation,
  <b>not</b> a trusted timestamp, <b>not</b> non-repudiation, and <b>not</b> a blockchain
  or ledger entry.</p>
  <p>Because the stored file <em>contains</em> <code>content_hash</code>, the SHA-256 of
  the whole file is expected to differ from it. That is arithmetic, not a discrepancy.</p>
</section>

<section>
  <h2>Why it exists at all</h2>
  <p>Seven completion conditions must hold before a Change Proof is generated. There is no
  override, no force-complete, and no path by which a client can assert one. In the
  recorded run the first verification returned <b>FAIL</b> and no proof was produced —
  which is the point. A verification system that only works when the worker gets it right
  the first time verifies nothing.</p>
  <div class="cta">
    <a class="btn primary" rel="noopener"
       href="{REPO_URL}/blob/master/docs/verifying_a_change_proof.md">
      Verify a Change Proof yourself →</a>
    <a class="btn" href="/demo">See the run that produced it</a>
  </div>
</section>
"""
    return _page("Change Proof — DRIFTZERO", body, active="proof")


def not_found() -> str:
    body = """
<section class="page-head">
  <h1>Not here</h1>
  <p class="lead">That page does not exist on this surface.</p>
  <div class="cta"><a class="btn primary" href="/">Back to the overview</a></div>
</section>
"""
    return _page("Not found — DRIFTZERO", body, active="")
