/* DRIFTZERO Hero Console — no framework, no build step, no network beyond this origin.
 *
 * Every label rendered here comes from the server's real result. The UI never decides
 * that something was authorized, mutated, or validated; it only draws what happened.
 */

const $ = (id) => document.getElementById(id);

let state = null;
let selectedEvidence = null;

/* ---------------------------------------------------------------- transport */

async function call(path, method = "GET") {
  const response = await fetch(path, { method, headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${method} ${path} → ${response.status}`);
  return response.json();
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2200);
}

/* ---------------------------------------------------------------- helpers */

const esc = (value) =>
  String(value).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );

const shortHash = (hash) => (hash ? `${hash.slice(0, 12)}…${hash.slice(-6)}` : "—");

function chip(text, kind = "") {
  return `<span class="chip ${kind}">${esc(text)}</span>`;
}

function row(key, value, cls = "") {
  return `<div class="row"><span class="k">${esc(key)}</span>
          <span class="v ${cls}">${value}</span></div>`;
}

/* Highlight the unrelated lexical LEFT so the audience can see it survive. */
function markLexicalLeft(text) {
  return esc(text).replace(/\bLEFT\b/g, "<mark>LEFT</mark>");
}

function highlightJson(value) {
  const json = JSON.stringify(value, null, 2);
  return esc(json)
    .replace(/&quot;([^&]+)&quot;(\s*:)/g, '<span class="k">"$1"</span>$2')
    .replace(/:\s&quot;([^&]*)&quot;/g, ': <span class="s">"$1"</span>')
    .replace(/:\s(-?\d+\.?\d*)/g, ': <span class="n">$1</span>')
    .replace(/:\s(true|false|null)/g, ': <span class="b">$1</span>');
}

/* ---------------------------------------------------------------- renderers */

function renderEnvironment() {
  const env = state.environment;
  $("env-badge").textContent = env.is_production ? "Change Ops" : "Change Ops · Development";
  $("btn-reset").textContent = env.session_action_label;
  $("btn-security").textContent = env.control_verification_label;
  $("roadmap-panel").hidden = !env.show_roadmap;
}

function renderModules() {
  $("modules").innerHTML = state.modules
    .map((m) => {
      const cls =
        m.status === "ACTIVE" ? "active" : m.status === "PARTIAL" ? "partial" : "wired-not";
      return `<span class="module ${cls}"><span class="dot"></span>${esc(m.label)}
              <span class="tag">${esc(m.status)}</span></span>`;
    })
    .join("");
}

function renderScenario() {
  const s = state.scenario;
  $("session-chip").textContent = `CHANGE ${s.change_id}`;
  $("change-id").textContent = s.change_id;
  $("source-name").textContent = `${s.source} · ${s.source_procedure_id}`;
  $("prev-version").textContent = s.previous_version;
  $("curr-version").textContent = s.source_version;
  $("requirement-id").textContent = s.requirement_id;
  $("value-from").textContent = s.previous_value;
  $("value-to").textContent = s.current_value;
}

function renderStages() {
  const r = state.remediation;
  const v = state.validated_execution;
  const c = state.crossing_2;
  const f = state.frontline;
  const dev = state.environment.show_diagnostics;

  /* Implemented stages describe real change progress. Unimplemented capabilities are
     development diagnostics — they are never rendered as operational stages, and never
     counted, because a stage nobody built cannot be "pending" for this change. */
  const implemented = [
    {
      label: "Source Change Approved",
      sub: `${state.scenario.change_id} · ${state.scenario.previous_version} → ${state.scenario.source_version}`,
      chip: chip("APPROVED", "ok"),
      done: true,
    },
    {
      label: "Authorization",
      sub: r ? `${r.identity} → ARTIFACT_MUTATION` : "Awaiting deployment",
      chip: r
        ? r.status === "CAPABILITY_DENIED"
          ? chip("DENIED", "bad")
          : chip("GRANTED", "ok")
        : chip("PENDING"),
      done: Boolean(r) && r.status !== "CAPABILITY_DENIED",
      denied: Boolean(r) && r.status === "CAPABILITY_DENIED",
    },
    {
      label: "Artifact Remediation",
      sub: v
        ? `${v.remediation_type} · dispatch count ${v.dispatch_count}`
        : "Awaiting deployment",
      chip: v ? chip(v.remediation_type, "ok") : chip("PENDING"),
      done: Boolean(v),
    },
    {
      label: "Evidence Validation",
      sub: v ? "Checked against authoritative state" : "Awaiting evidence",
      chip: v ? chip(v.crossing_2, v.accepted ? "ok" : "bad") : chip("PENDING"),
      done: Boolean(v && v.accepted),
    },
    {
      label: "Teach the Delta",
      sub: f && f.available
        ? f.acknowledged
          ? "Operator acknowledged the update"
          : "Operational delta composed — awaiting acknowledgment"
        : "Awaiting validated change",
      chip: f && f.available ? chip(f.acknowledged ? "ACKNOWLEDGED" : "READY", f.acknowledged ? "ok" : "info") : chip("PENDING"),
      done: Boolean(f && f.acknowledged),
    },
  ];

  const notImplemented = [
    { label: "Impact Analysis", sub: "Change Intelligence Agent", chip: chip("NOT IMPLEMENTED", "warn") },
    { label: "Proven Delivery", sub: "Delivery receipt — T078", chip: chip("NOT IMPLEMENTED", "warn") },
    { label: "Physical Verification", sub: "Field observation — M3", chip: chip("NOT IMPLEMENTED", "warn") },
    { label: "Change Proof", sub: "Seven proof invariants", chip: chip("NOT IMPLEMENTED", "warn") },
  ];

  const shown = dev ? implemented.concat(notImplemented) : implemented;
  $("stages").innerHTML = shown
    .map(
      (s, i) => `
      <div class="stage ${s.done ? "done" : ""} ${s.denied ? "denied" : ""}">
        <span class="idx">${String(i + 1).padStart(2, "0")}</span>
        <span><span class="label">${esc(s.label)}</span><div class="sub">${esc(s.sub)}</div></span>
        ${s.chip}
      </div>`,
    )
    .join("");

  /* Counts only real change progress, never software completeness. */
  const done = implemented.filter((s) => s.done).length;
  $("pipeline-chip").textContent = `${done} / ${implemented.length} COMPLETE`;
  $("pipeline-chip").className = `chip ${done === implemented.length ? "ok" : "warn"}`;
  void c;
}

function renderArtifact() {
  const a = state.artifact;
  const target = state.scenario.requirement_id;
  const rows = Object.entries(a.requirements)
    .map(([key, value]) => {
      const changed = key === target && value === state.scenario.current_value;
      const note =
        key === "instructions"
          ? '<span class="note">Unrelated lexical LEFT — must remain untouched</span>'
          : "";
      return `<div class="req-row ${changed ? "changed" : ""}">
                <span class="key">${esc(key)}</span>
                <span class="val">${markLexicalLeft(value)}${note}</span>
              </div>`;
    })
    .join("");

  $("artifact").innerHTML =
    rows +
    `<div class="rows" style="margin-top:12px">
       ${row("Artifact", esc(a.artifact_id), "mono")}
       ${row("Content ref", esc(a.content_ref), "mono")}
       ${row("Content hash", esc(shortHash(a.content_hash)), "mono")}
     </div>`;

  const current = a.requirements[target];
  $("artifact-chip").textContent = current;
  $("artifact-chip").className = `chip ${current === state.scenario.current_value ? "ok" : "warn"}`;
}

function renderEvidenceSummary() {
  const r = state.remediation;
  const v = state.validated_execution;
  const c = state.crossing_2;

  if (!r) {
    $("evidence-summary").innerHTML =
      '<div class="empty">No change executed yet. Press <b>Deploy Change</b>.</div>';
    $("crossing-chip").textContent = "—";
    $("crossing-chip").className = "chip";
    return;
  }

  /* The latest request and the validated execution are different facts. An idempotent
     replay changes the former and must never blank the latter. */
  const lastRequest = `<div class="rows">
    ${row("Last request", chip(r.status, r.status === "MUTATED" ? "ok" : "info"))}
    ${row("Dispatched by this request", String(r.dispatched))}
  </div>`;

  const execution = v
    ? `<div class="rows">
        ${row("Evidence type", chip(v.remediation_type, "info"))}
        ${row("Reconciled", chip(String(v.reconciled), v.reconciled ? "warn" : ""))}
        ${row("Dispatch count", `<b>${v.dispatch_count}</b>`, "hl")}
        ${row("Action ID", esc(v.action_id), "mono")}
        ${row("Authoritative before hash", esc(shortHash(v.authoritative_before_hash)), "mono")}
        ${row("Authoritative after hash", esc(shortHash(v.authoritative_after_hash)), "mono")}
        ${row("Crossing 2", chip(v.crossing_2, v.accepted ? "ok" : "bad"))}
        ${row("Enforcement", esc(r.enforcement_model), "mono")}
      </div>`
    : '<div class="empty">No validated execution recorded.</div>';

  $("evidence-summary").innerHTML =
    `<div class="sub-head">Last request</div>${lastRequest}
     <div class="sub-head">Validated execution evidence</div>${execution}`;

  const chipSource = v || c;
  if (chipSource) {
    const accepted = v ? v.accepted : c.accepted;
    $("crossing-chip").textContent = v ? v.crossing_2 : c.verdict;
    $("crossing-chip").className = `chip ${accepted ? "ok" : "bad"}`;
  }
}

function renderFleet() {
  $("fleet").innerHTML = state.fleet
    .map((a) => {
      const caps = a.capabilities
        .map(
          (c) =>
            `<div class="cap"><span class="cap-name">${esc(c.capability)}</span>
             ${chip(c.permission, c.permission === "ALLOWED" ? "ok" : "bad")}</div>`,
        )
        .join("");
      return `
      <div class="agent ${a.artifact_mutation === "ALLOWED" ? "allowed" : ""}">
        <span>
          <span class="name">${esc(a.name)}</span>
          <div class="id">${esc(a.identity)}</div>
          <div class="role">${esc(a.role)}</div>
        </span>
        <span class="agent-right">
          ${chip(a.status, "info")}
          <div class="caps">${caps}</div>
        </span>
      </div>`;
    })
    .join("");

  const allowed = state.fleet.filter((a) => a.artifact_mutation === "ALLOWED").length;
  $("fleet-count").textContent = `${state.fleet.length} OPERATIONAL`;
  $("enforcement-note").textContent = state.authorization.note;
  void allowed;
}

function renderSecurity() {
  const s = state.security;
  if (!s) {
    $("security-panel").innerHTML =
      '<div class="empty">Frontline Enablement will attempt <b>ARTIFACT_MUTATION</b> through the real policy seam.</div>';
    $("security-chip").textContent = "IDLE";
    $("security-chip").className = "chip";
    return;
  }

  const d = s.denial || {};
  $("security-chip").textContent = s.denied ? "DENIED" : "UNEXPECTED ALLOW";
  $("security-chip").className = `chip ${s.denied ? "bad" : "warn"}`;

  $("security-panel").innerHTML = `<div class="rows">
    ${row("Requested by", esc(d.requested_by || "—"), "mono")}
    ${row("Requested tool", esc(d.requested_tool || "—"), "mono")}
    ${row("Decision", chip(d.decision || s.status, "bad"))}
    ${row("Reason code", esc(d.reason_code || "—"), "mono")}
    ${row("Hash before", esc(shortHash(s.artifact_hash_before)), "mono")}
    ${row("Hash after", esc(shortHash(s.artifact_hash_after)), "mono")}
    ${row("Hash unchanged", chip(String(s.artifact_hash_unchanged), s.artifact_hash_unchanged ? "ok" : "bad"))}
    ${row("Dispatch delta", `<b>${s.dispatch_count_after - s.dispatch_count_before}</b>`, "hl")}
    ${row("No state transition", chip(String(d.no_state_transition), "ok"))}
    ${row("Enforcement", esc(d.enforcement_model || "—"), "mono")}
    ${row("Platform-enforced identity", chip(String(d.platform_enforced_per_agent_identity), "warn"))}
  </div>
  <div class="callout"><b>Policy basis.</b> ${esc(d.policy_basis || "—")}</div>`;
}

function renderTimeline() {
  const bad = new Set(["SECURITY_DENIED", "CROSSING_2_REJECTED", "SECURITY_UNEXPECTED_ALLOW"]);
  $("timeline").innerHTML = state.timeline
    .map(
      (e) => `<div class="tl ${bad.has(e.event) ? "bad" : ""}">
        <span class="n">${String(e.sequence).padStart(2, "0")}</span>
        <span class="e">${esc(e.event)}</span>
        <span class="d">${esc(e.detail)}</span>
        <span class="t">${esc(e.at.split("T")[1] || e.at)}</span>
      </div>`,
    )
    .join("");
}

function renderEvidenceBar() {
  const ids = state.evidence_ids;
  if (!ids.length) {
    $("evidence-bar").innerHTML = '<span class="chip">NO RECORDS YET</span>';
    $("evidence-json").textContent = "Select an evidence record.";
    return;
  }
  $("evidence-bar").innerHTML = ids
    .map(
      (id) =>
        `<button class="ghost" data-evidence="${esc(id)}">${esc(
          id.replace(/-session-\d+$/, ""),
        )}</button>`,
    )
    .join("");
  $("evidence-bar")
    .querySelectorAll("[data-evidence]")
    .forEach((b) => b.addEventListener("click", () => showEvidence(b.dataset.evidence)));
}

function renderFuture() {
  if (!state.environment.show_roadmap) return;
  $("future").innerHTML = state.future_capabilities
    .map((g) => {
      const kind = g.status === "PARTIAL" ? "warn" : "";
      return `<div class="future-group">
        <header><h3>${esc(g.group)}</h3>${chip(g.status, kind)}</header>
        <div class="ms">${esc(g.milestone)}</div>
        <ul>${g.items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>
      </div>`;
    })
    .join("");
}

function renderFrontline() {
  const f = state.frontline;
  const panel = $("frontline");
  if (!f || !f.available) {
    $("frontline-chip").textContent = "AWAITING VALIDATION";
    $("frontline-chip").className = "chip";
    panel.innerHTML =
      '<div class="empty">The operational delta is composed once a change is validated at Crossing 2.</div>';
    return;
  }

  const i = f.instruction;
  const unchanged = Object.entries(i.unchanged_context || {})
    .map(([k, v]) => `<div class="req-row"><span class="key">${esc(k)}</span>
                      <span class="val">${markLexicalLeft(v)}</span></div>`)
    .join("");

  $("frontline-chip").textContent = f.acknowledged ? "ACKNOWLEDGED" : "READY TO TEACH";
  $("frontline-chip").className = `chip ${f.acknowledged ? "ok" : "info"}`;

  panel.innerHTML = `<div class="rows">
      ${row("Requirement", esc(i.requirement_id), "mono")}
      ${row("Change", `${chip(i.before_value, "warn")} → ${chip(i.after_value, "ok")}`)}
      ${row("Instruction", esc(i.concise_instruction))}
      ${row("Rationale", i.rationale ? esc(i.rationale) : "<i>None provided by the source</i>")}
      ${row("Acknowledged", chip(String(f.acknowledged), f.acknowledged ? "ok" : ""))}
      ${row("Delivery established", chip(String(f.delivery_established), "warn"))}
    </div>
    <div class="sub-head">Unchanged context</div>${unchanged}
    <div class="actions" style="margin-top:14px">
      <a class="link-btn" href="/frontline/${encodeURIComponent(i.change_id)}" target="_blank"
         rel="noopener">Open Worker View</a>
    </div>
    <div class="callout">${esc(f.delivery_note)}</div>`;
}

/* ---------------------------------------------------------------- evidence */

async function showEvidence(id) {
  const doc = await call(`/api/hero/evidence/${encodeURIComponent(id)}`);
  selectedEvidence = doc.document;
  $("evidence-json").innerHTML = highlightJson(doc.document);
}

/* ---------------------------------------------------------------- render */

function render() {
  renderEnvironment();
  renderModules();
  renderScenario();
  renderStages();
  renderArtifact();
  renderEvidenceSummary();
  renderFleet();
  renderSecurity();
  renderTimeline();
  renderEvidenceBar();
  renderFrontline();
  renderFuture();
}

async function refresh(promise) {
  const buttons = [$("btn-deploy"), $("btn-reset"), $("btn-security")];
  buttons.forEach((b) => (b.disabled = true));
  try {
    state = await promise;
    render();
  } catch (error) {
    toast(String(error.message || error));
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

$("btn-deploy").addEventListener("click", () => refresh(call("/api/hero/deploy", "POST")));
$("btn-reset").addEventListener("click", () => {
  selectedEvidence = null;
  refresh(call("/api/hero/reset", "POST"));
});
$("btn-security").addEventListener("click", () =>
  refresh(call("/api/hero/security-test", "POST")),
);
$("btn-copy").addEventListener("click", async () => {
  if (!selectedEvidence) return toast("No evidence selected");
  try {
    await navigator.clipboard.writeText(JSON.stringify(selectedEvidence, null, 2));
    toast("Evidence JSON copied");
  } catch {
    toast("Clipboard unavailable");
  }
});

refresh(call("/api/hero/state"));
