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
  $("session-chip").textContent = `SESSION ${s.action_id.slice(0, 8).toUpperCase()}`;
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
  const c = state.crossing_2;
  const mutated = r && (r.status === "MUTATED" || r.status === "ALREADY_COMPLETED");

  const stages = [
    {
      label: "Source Change Approved",
      sub: `${state.scenario.change_id} · ${state.scenario.previous_version} → ${state.scenario.source_version}`,
      chip: chip("APPROVED", "ok"),
      done: true,
    },
    {
      label: "Impact Analysis",
      sub: "Change Intelligence Agent — not wired into this slice",
      chip: chip("NOT WIRED", "warn"),
      done: false,
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
      label: "Artifact Mutation",
      sub: r
        ? `${r.status.replace(/_/g, " ").toLowerCase()} · dispatch count ${r.dispatch_count}`
        : "Awaiting deployment",
      chip: r ? chip(r.remediation_type || r.status, mutated ? "ok" : "warn") : chip("PENDING"),
      done: Boolean(mutated),
    },
    {
      label: "Crossing 2 Validation",
      sub: c ? "RemediationEvidence checked against authoritative state" : "Awaiting evidence",
      chip: c ? chip(c.verdict, c.accepted ? "ok" : "bad") : chip("PENDING"),
      done: Boolean(c && c.accepted),
    },
    { label: "Teach the Delta", sub: "Frontline Enablement — T077", chip: chip("COMING NEXT", "warn"), done: false },
    { label: "Physical Verification", sub: "Gemma field observation — M3", chip: chip("NOT WIRED", "warn"), done: false },
    { label: "Change Proof", sub: "Seven proof invariants — not wired", chip: chip("NOT WIRED", "warn"), done: false },
  ];

  $("stages").innerHTML = stages
    .map(
      (s, i) => `
      <div class="stage ${s.done ? "done" : ""} ${s.denied ? "denied" : ""}">
        <span class="idx">${String(i + 1).padStart(2, "0")}</span>
        <span><span class="label">${esc(s.label)}</span><div class="sub">${esc(s.sub)}</div></span>
        ${s.chip}
      </div>`,
    )
    .join("");

  const done = stages.filter((s) => s.done).length;
  $("pipeline-chip").textContent = `${done} / ${stages.length} STAGES`;
  $("pipeline-chip").className = `chip ${done > 3 ? "ok" : "warn"}`;
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
  const c = state.crossing_2;
  if (!r) {
    $("evidence-summary").innerHTML =
      '<div class="empty">No remediation executed yet. Press <b>Deploy Change</b>.</div>';
    $("crossing-chip").textContent = "—";
    $("crossing-chip").className = "chip";
    return;
  }

  const before = c ? c.authoritative_before_hash : null;
  const after = c ? c.authoritative_after_hash : null;

  $("evidence-summary").innerHTML = `<div class="rows">
    ${row("Remediation status", chip(r.status, r.status === "MUTATED" ? "ok" : "info"))}
    ${row("Evidence type", r.remediation_type ? chip(r.remediation_type, "info") : "—")}
    ${row("Reconciled", r.reconciled === null ? "—" : chip(String(r.reconciled), r.reconciled ? "warn" : ""))}
    ${row("Dispatch count", `<b>${r.dispatch_count}</b>`, "hl")}
    ${row("Action ID", esc(state.scenario.action_id), "mono")}
    ${row("Authoritative before hash", esc(shortHash(before)), "mono")}
    ${row("Authoritative after hash", esc(shortHash(after)), "mono")}
    ${row("Crossing 2", c ? chip(c.verdict, c.accepted ? "ok" : "bad") : "—")}
    ${row("Requires review", c ? String(c.requires_review) : "—")}
    ${row("Enforcement", esc(r.enforcement_model), "mono")}
  </div>`;

  if (c) {
    $("crossing-chip").textContent = c.verdict;
    $("crossing-chip").className = `chip ${c.accepted ? "ok" : "bad"}`;
  }
}

function renderFleet() {
  $("fleet").innerHTML = state.fleet
    .map(
      (a) => `
      <div class="agent ${a.artifact_mutation === "ALLOWED" ? "allowed" : ""}">
        <span>
          <span class="name">${esc(a.name)}</span>
          <div class="id">${esc(a.identity)}</div>
          <div class="role">${esc(a.role)}</div>
        </span>
        ${chip(a.artifact_mutation, a.artifact_mutation === "ALLOWED" ? "ok" : "bad")}
      </div>`,
    )
    .join("");

  const allowed = state.fleet.filter((a) => a.artifact_mutation === "ALLOWED").length;
  $("fleet-count").textContent = `${allowed} / ${state.fleet.length} MUTATION`;
  $("enforcement-note").textContent = state.authorization.note;
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

/* ---------------------------------------------------------------- evidence */

async function showEvidence(id) {
  const doc = await call(`/api/hero/evidence/${encodeURIComponent(id)}`);
  selectedEvidence = doc.document;
  $("evidence-json").innerHTML = highlightJson(doc.document);
}

/* ---------------------------------------------------------------- render */

function render() {
  renderModules();
  renderScenario();
  renderStages();
  renderArtifact();
  renderEvidenceSummary();
  renderFleet();
  renderSecurity();
  renderTimeline();
  renderEvidenceBar();
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
