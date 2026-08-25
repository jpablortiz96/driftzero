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

  $("fl-footnote").textContent = view.delivery_note;
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

load().then((view) => view && render(view));
