/* Worker-facing frontline view. Renders the real DeltaInstruction; decides nothing. */

const changeId = decodeURIComponent(window.location.pathname.split("/frontline/")[1] || "");
const $ = (id) => document.getElementById(id);

const esc = (v) =>
  String(v).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );

async function load() {
  const response = await fetch(`/api/hero/frontline/${encodeURIComponent(changeId)}`);
  if (!response.ok) {
    document.querySelector(".fl-shell").innerHTML =
      `<div class="fl-error">No process update is available for
       <b>${esc(changeId)}</b> yet.<br />A change must be deployed and validated first.</div>`;
    return null;
  }
  return response.json();
}

function render(view) {
  const i = view.instruction;
  $("fl-version").textContent = `${view.previous_version} → ${view.source_version}`;
  $("fl-source").textContent = `${view.source_name} · ${view.change_id}`;
  $("fl-requirement").textContent = i.requirement_id;
  $("fl-before").textContent = i.before_value;
  $("fl-after").textContent = i.after_value;
  $("fl-instruction").textContent = i.concise_instruction;

  const entries = Object.entries(i.unchanged_context || {});
  $("fl-unchanged").innerHTML = entries.length
    ? entries
        .map(
          ([k, v]) =>
            `<div class="fl-unchanged-row"><span class="k">${esc(k)}</span>
             <span class="v">${esc(v)}</span></div>`,
        )
        .join("")
    : '<div class="fl-unchanged-row"><span class="v">No other requirements recorded.</span></div>';

  if (view.acknowledged) {
    const at = (view.acknowledgment?.acknowledged_at || "").replace("T", " ").split(".")[0];
    $("fl-ack-zone").innerHTML =
      `<div class="fl-acked">✓ Acknowledged</div>
       <p class="fl-acked-note">Recorded ${esc(at)} UTC. This confirms you read the update.
       It does not complete the change — physical verification is a separate step.</p>`;
  }

  renderFieldEvidence(view.field_verification);
  $("fl-footnote").textContent = view.delivery_note;
}

/* The worker sees the observation in plain language. No PASS, no FAIL, no percentage —
 * this surface reports what the model saw and whether another photo is needed. */
function renderFieldEvidence(f) {
  const card = $("fl-evidence-card");
  if (!f || !f.provider_configured) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const out = $("fl-evidence-result");

  if (f.rejected) {
    out.innerHTML = `<div class="fl-obs bad">
      <span class="fl-obs-label">Photo not accepted</span>
      <span class="fl-obs-value">${esc(f.detail || f.rejection_reason)}</span>
      <p class="fl-acked-note">Take another photo and send it again.</p></div>`;
    return;
  }

  if (!f.observation) {
    out.innerHTML = f.status === "AWAITING_EVIDENCE" ? "" :
      `<div class="fl-obs bad"><span class="fl-obs-label">Not recorded</span>
       <span class="fl-obs-value">The photo could not be checked</span>
       <p class="fl-acked-note">Please take another photo.</p></div>`;
    return;
  }

  if (f.inconclusive) {
    out.innerHTML = `<div class="fl-obs warn">
      <span class="fl-obs-label">More evidence required</span>
      <span class="fl-obs-value">Label position unclear</span>
      <p class="fl-acked-note">The photo does not show the label clearly enough.
      Take another one with the labelled face fully visible.</p></div>`;
    $("fl-capture").textContent = "Take Another Photo";
    $("fl-capture").disabled = false;
    return;
  }

  out.innerHTML = `<div class="fl-obs ok">
    <span class="fl-obs-label">Recorded from your photo</span>
    <span class="fl-obs-value">${esc(f.observation)}</span>
    <p class="fl-acked-note">This is what the system saw on the box. It has been
    recorded as evidence — the change is not complete yet.</p></div>`;
  $("fl-capture").textContent = "Send Another Photo";
  $("fl-capture").disabled = false;
}

$("fl-ack").addEventListener("click", async () => {
  $("fl-ack").disabled = true;
  const response = await fetch(
    `/api/hero/frontline/${encodeURIComponent(changeId)}/acknowledge`,
    { method: "POST" },
  );
  if (response.ok) render(await response.json());
  else $("fl-ack").disabled = false;
});

/* The same backend use case Mission Control calls. The body is the photo itself. */
$("fl-capture").addEventListener("click", () => $("fl-file").click());
$("fl-file").addEventListener("change", async () => {
  const file = $("fl-file").files[0];
  if (!file) return;
  $("fl-capture").disabled = true;
  $("fl-capture").textContent = "Sending…";
  try {
    const response = await fetch(
      `/api/hero/frontline/${encodeURIComponent(changeId)}/field-evidence`,
      {
        method: "POST",
        headers: { "X-Filename": file.name || "field-evidence" },
        body: file,
      },
    );
    if (response.ok) render(await response.json());
    else {
      $("fl-capture").textContent = "Take Photo";
      $("fl-capture").disabled = false;
    }
  } finally {
    $("fl-file").value = "";
  }
});

load().then((view) => view && render(view));
