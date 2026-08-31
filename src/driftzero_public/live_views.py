"""HTML for the live pilot.

The whole flow is HTML forms and POST→redirect→GET. No JavaScript, so the strict
``script-src 'none'`` policy the public surface already sends stays exactly as it is, and
nothing on the page can be made to progress on its own.

That constraint is also a truth-telling device. Without a timer or a poller there is no
way to animate a step forward: every completed stage on these pages is a stage the
private backend has already reported. A spinner that advances by itself is the exact lie
this product exists to refuse.
"""

from __future__ import annotations

from typing import Any

from driftzero_public.views import REPO_URL, e

DISCLOSURE = (
    "Live Google Cloud execution using the controlled DRIFTZERO packing pilot."
)


def _shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title>
<link rel="stylesheet" href="/static/public.css">
</head><body>
<header class="bar">
  <a class="brand" href="/">DRIFTZERO</a>
  <nav>
    <a href="/live" class=on>Live pilot</a>
    <a href="/demo">Recorded run</a>
    <a href="/architecture">Architecture</a>
  </nav>
  <a class="gh" href="{REPO_URL}" rel="noopener">GitHub</a>
</header>
<main>{body}</main>
<footer>
  <p class="fine">{e(DISCLOSURE)} Each run creates a new workflow, calls Gemini and
  Gemma on Google Cloud, applies the deterministic Truth Engine, and generates a new
  Change Proof. The operational backend is a private, IAM-protected Cloud Run service;
  your browser never reaches it directly.</p>
  <p class="fine"><a href="{REPO_URL}" rel="noopener">Source and evidence on GitHub</a></p>
</footer>
</body></html>"""


def _steps(reached: dict[str, bool]) -> str:
    """Progress, rendered from backend state only.

    Every tick below is a fact the private API reported. Nothing here is a timer.
    """
    rows = (
        ("Analysing the source change", "analysed", "Gemini reads both versions"),
        ("Determining affected work", "qualified", "the Truth Engine qualifies one target"),
        ("Applying scoped remediation", "remediated", "one artifact, under a capability"),
        ("Delivering the delta", "delivered", "the worker gets what changed"),
        ("Awaiting field verification", "awaiting", "a photograph of the physical work"),
    )
    cells = []
    for label, key, note in rows:
        done = reached.get(key, False)
        mark = "✓" if done else "·"
        cells.append(
            f'<li class="step {"done" if done else "pending"}">'
            f'<span class="tick">{mark}</span>'
            f"<b>{e(label)}</b><i>{e(note)}</i></li>"
        )
    return f'<ol class="steps">{"".join(cells)}</ol>'


def landing(backend_ready: bool, detail: str) -> str:
    """The pre-flight page: what is about to happen, and the button that does it."""
    blocked = (
        ""
        if backend_ready
        else f'<div class="chip warn"><span class="dot"></span>'
        f"<b>The pilot backend is not answering</b><i>{e(detail)}</i>"
        f"<u>the recorded run remains available</u></div>"
    )
    button = (
        '<button type="submit" class="btn primary big">Run live pilot</button>'
        if backend_ready
        else '<a class="btn" href="/demo">View the recorded run instead</a>'
    )
    form_open = '<form method="post" action="/live/start">' if backend_ready else "<div>"
    form_close = "</form>" if backend_ready else "</div>"
    return _shell(
        "Live pilot — DRIFTZERO",
        f"""
<section class="page-head">
  <h1>Run the live pilot</h1>
  <p class="lead">A packing procedure changes one requirement —
  <code>label_position: LEFT → TOP_RIGHT</code> — and DRIFTZERO proves whether the
  physical work actually changed.</p>
  <p class="note">{e(DISCLOSURE)}</p>
  {blocked}
</section>

<section>
  <h2>What happens when you press the button</h2>
  <ol class="flow-list">
    <li><b>Gemini</b> reads both versions of the source procedure and proposes which work
    instructions might be affected.</li>
    <li>The <b>Truth Engine</b> qualifies exactly one — and overrules the rest. A nearby
    instruction that also contains the word LEFT is a forklift turn direction, and it is
    left alone.</li>
    <li><b>Remediation</b> edits that one artifact, under a capability it must hold.</li>
    <li>The worker receives <b>the delta</b>, not a new manual.</li>
    <li>You submit a photograph. <b>Gemma</b> observes where the label is, and the Truth
    Engine decides PASS or FAIL. The model never decides that.</li>
  </ol>
  {form_open}{button}{form_close}
  <p class="note">This calls real models on Google Cloud and creates real durable state.
  It takes about a minute.</p>
</section>

<section class="closing">
  <div class="cta">
    <a class="btn" href="/demo">View a recorded run instead</a>
    <a class="btn" href="/architecture">How it is built</a>
  </div>
</section>
""",
    )


def delta(status: dict[str, Any], token: str, seconds_left: int) -> str:
    """The worker's view: what changed, and the two ways to prove it."""
    change = status.get("delta") or {}
    previous = change.get("previous_value") or status.get("previous_value") or "LEFT"
    current = change.get("current_value") or status.get("current_value") or "TOP_RIGHT"
    artifact = status.get("affected_artifact_id") or "—"
    reached = {
        "analysed": True,
        "qualified": bool(status.get("affected_artifact_id")),
        "remediated": bool(status.get("affected_artifact_id")),
        "delivered": bool(status.get("delivery_established")),
        "awaiting": bool(status.get("delivery_established")),
    }
    return _shell(
        "Your work has changed — DRIFTZERO",
        f"""
<section class="page-head">
  <h1>Your work has changed</h1>
  {_steps(reached)}
</section>

<section>
  <div class="delta-card">
    <span class="delta-label">Label position</span>
    <div class="delta-pair">
      <div class="was"><u>Was</u><b>{e(str(previous).replace("_", " "))}</b></div>
      <div class="arrow-x">→</div>
      <div class="now"><u>Now</u><b>{e(str(current).replace("_", " "))}</b></div>
    </div>
    <span class="delta-meta">Affected work instruction: <code>{e(artifact)}</code></span>
  </div>
</section>

<section>
  <h2>Prove the change</h2>
  <p>Photograph the finished work. Gemma observes the label position; the Truth Engine
  compares it to the expected value and decides.</p>

  <form method="post" action="/live/verify" class="verify-form">
    <input type="hidden" name="capability" value="{e(token)}">
    <input type="hidden" name="photo" value="current">
    <button type="submit" class="btn primary big">Verify current state</button>
    <span class="hint">Runs a new live verification against a real pilot photograph.</span>
  </form>

  <details class="upload">
    <summary>Upload your own photograph instead</summary>
    <form method="post" action="/live/upload" enctype="multipart/form-data">
      <input type="hidden" name="capability" value="{e(token)}">
      <input type="file" name="file" accept="image/*" required>
      <button type="submit" class="btn">Submit my photo</button>
      <span class="hint">Images only, up to 8 MB. The verdict is the Truth Engine's —
      PASS, FAIL or INCONCLUSIVE.</span>
    </form>
  </details>
  <p class="note">This pilot session expires in {seconds_left // 60} minutes.</p>
</section>
""",
    )


def verdict(
    result: str,
    observation: str | None,
    status: dict[str, Any],
    token: str,
    *,
    proof_ready: bool,
    latency: float | None = None,
) -> str:
    """The outcome of one real verification. Never rendered before the backend answers."""
    passed = result == "PASS"
    inconclusive = result == "INCONCLUSIVE"

    if passed:
        headline, tone, blurb = "Verified", "ok", "The physical work matches the change."
    elif inconclusive:
        headline, tone, blurb = (
            "Not readable",
            "warn",
            "Gemma could not determine the label position from that image. "
            "That is an honest outcome, not a failure of the work.",
        )
    else:
        headline, tone, blurb = (
            "Not done yet",
            "fail",
            "The label is still on the left. No Change Proof was generated.",
        )

    observed = (
        f'<div class="observed"><u>Gemma 4 · Vertex AI MaaS · live</u>'
        f'<b>Observed: <code>{e(observation)}</code></b>'
        f'<i>Truth Engine verdict: <code>{e(result)}</code></i>'
        + (f"<span>{latency:.1f} s</span>" if latency else "")
        + "</div>"
        if observation
        else ""
    )

    if passed and proof_ready:
        action = f"""
    <form method="get" action="/live/proof">
      <input type="hidden" name="capability" value="{e(token)}">
      <button type="submit" class="btn primary big">View Change Proof</button>
    </form>"""
    elif passed:
        action = '<p class="note">The proof is still being finalised.</p>'
    else:
        action = f"""
    <form method="post" action="/live/verify">
      <input type="hidden" name="capability" value="{e(token)}">
      <input type="hidden" name="photo" value="corrected">
      <button type="submit" class="btn primary big">Verify corrected state</button>
      <span class="hint">The worker moves the label and photographs again — a second live
      Gemma call against the corrected pilot photograph.</span>
    </form>"""

    chronology = "".join(
        f'<li class="{"pass" if r == "PASS" else "fail"}">Attempt {i + 1}: <b>{e(r)}</b></li>'
        for i, r in enumerate(status.get("verification_results", []))
    )

    return _shell(
        f"{headline} — DRIFTZERO",
        f"""
<section class="page-head verdict {tone}">
  <h1>{e(headline)}</h1>
  <p class="lead">{e(blurb)}</p>
  {observed}
</section>

<section>
  {action}
</section>

<section>
  <h2>This run so far</h2>
  <ul class="chronology">{chronology or "<li>No verification attempts yet.</li>"}</ul>
  <p class="note">Every attempt is kept. A change that took two tries is a different
  operational fact from one that passed immediately.</p>
</section>
""",
    )


def live_proof(document: dict[str, Any], integrity: dict[str, Any]) -> str:
    """The Change Proof this run produced — not a historical one."""
    fields = (
        ("Change", "change_id"),
        ("Source procedure", "source_procedure_id"),
        ("Source version", "source_version"),
        ("Affected artifact", "affected_artifact_id"),
        ("Previous value", "previous_value"),
        ("Current value", "current_value"),
        ("Delivery status", "delivery_status"),
        ("Verification result", "verification_result"),
        ("Completion timestamp", "completion_timestamp"),
        ("Workflow", "workflow_id"),
        ("Proof id", "proof_id"),
    )
    rows = "".join(
        f"<tr><td>{e(label)}</td><td><code>{e(document.get(key, '—'))}</code></td></tr>"
        for label, key in fields
    )
    matches = integrity.get("matches") is True
    verdict_row = (
        '<p class="integrity ok"><b>Content hash matches.</b> Recomputed from the '
        "proof's canonical JSON, excluding its own <code>content_hash</code> field.</p>"
        if matches
        else '<p class="integrity warn"><b>Content hash could not be confirmed.</b> '
        f"{e(integrity.get('detail', ''))}</p>"
    )
    return _shell(
        "Change Proof — DRIFTZERO",
        f"""
<section class="page-head verdict ok">
  <h1>Change deployed</h1>
  <p class="lead"><b>7 / 7 conditions satisfied.</b> This Change Proof was generated by the
  run you just performed.</p>
</section>

<section>
  <h2>The proof</h2>
  <table class="grid wide proof">{rows}</table>
  <h3>Content hash</h3>
  <p class="hash"><code>{e(document.get("content_hash", ""))}</code></p>
  {verdict_row}
</section>

<section class="callout">
  <h2>What this hash is — and is not</h2>
  <p>A <b>SHA-256 over the canonical Change Proof JSON, excluding its own
  <code>content_hash</code> field</b>. It establishes content identity and detects
  alteration.</p>
  <p class="deny">It is <b>not</b> a digital signature, <b>not</b> an attestation,
  <b>not</b> a trusted timestamp, and <b>not</b> a ledger entry.</p>
</section>

<section class="closing">
  <div class="cta">
    <a class="btn primary" href="/live">Run another live pilot</a>
    <a class="btn" href="{REPO_URL}" rel="noopener">Inspect the evidence pack</a>
  </div>
</section>
""",
    )


def refused(headline: str, detail: str) -> str:
    """A dead capability, a refused backend, or a step taken out of order."""
    return _shell(
        f"{headline} — DRIFTZERO",
        f"""
<section class="page-head">
  <h1>{e(headline)}</h1>
  <p class="lead">{e(detail)}</p>
  <div class="cta">
    <a class="btn primary" href="/live">Start a new live pilot</a>
    <a class="btn" href="/demo">View the recorded run</a>
  </div>
</section>
""",
    )
