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

/* Upload transport. The request body is the file itself: no form, no JSON envelope,
 * and therefore no field a page script could smuggle a model, prompt, identity or
 * expected answer through. The server derives everything it needs from the bytes. */
async function upload(path, file) {
  const response = await fetch(path, {
    method: "POST",
    headers: { Accept: "application/json", "X-Filename": file.name || "field-evidence" },
    body: file,
  });
  if (!response.ok) throw new Error(`upload → ${response.status}`);
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
  $("env-badge").textContent = env.is_production
    ? `Change Ops · ${env.runtime_readiness.replace("_", " ")}`
    : `Change Ops · Development · ${env.runtime_readiness.replace("_", " ")}`;
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

/* Mirrors the server's gate. The server refuses an unqualified deploy on its own; this
 * only avoids offering an action that would be rejected. */
function applyGating() {
  const ready = Boolean(state.scenario.remediation_available);
  $("btn-deploy").disabled = !ready;
  $("btn-analyze").classList.toggle("primary", !ready);
  $("btn-deploy").classList.toggle("primary", ready);
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

function renderSource() {
  const src = state.source;
  if (!src) return;
  $("source-refs").innerHTML = `<div class="rows">
      ${row("Previous source", esc(src.previous_content_ref), "mono")}
      ${row("Previous hash", esc(shortHash(src.previous_content_hash)), "mono")}
      ${row("Current source", esc(src.current_content_ref), "mono")}
      ${row("Current hash", esc(shortHash(src.current_content_hash)), "mono")}
      ${row("Derivation", chip(src.derivation, "info"))}
      ${row("Candidate catalog", `<b>${src.catalog_size}</b> artifacts`, "hl")}
    </div>`;
}

/* Change Intelligence: what the model proposed, and — separately — what the Truth
 * Engine decided. The two are never merged into one number. */
function renderIntel() {
  const i = state.intel;
  const imp = state.impact;
  const chipEl = $("intel-chip");
  const out = $("intel");
  if (!i) return;

  if (i.status === "PENDING") {
    chipEl.textContent = "PENDING";
    chipEl.className = "chip";
    out.innerHTML = i.provider_configured
      ? `<div class="empty">Two source versions are loaded and hashed. Press
         <b>Analyze Change</b> to extract the change and search the catalog for
         affected work instructions.</div>`
      : `<div class="empty">Live change intelligence is not configured on this
         instance. Nothing is assumed in its place.</div>`;
    return;
  }

  if (i.status === "PROVIDER_DISABLED" || i.status === "PROVIDER_UNAVAILABLE") {
    chipEl.textContent = "NOT CONFIGURED";
    chipEl.className = "chip warn";
    out.innerHTML = `<div class="callout">${esc(i.detail || "")}</div>`;
    return;
  }

  if (!i.succeeded) {
    chipEl.textContent = "REVIEW REQUIRED";
    chipEl.className = "chip bad";
    out.innerHTML = `<div class="rows">
        ${row("Analysis", chip(i.status, "bad"))}
        ${row("Attempts", `<b>${i.attempts}</b>`, "hl")}
      </div>
      <div class="callout">${esc(i.failure_reason || "The proposal was not usable.")}
      No remediation is available.</div>`;
    return;
  }

  const c1 = state.crossing_1;
  const qualified = Boolean(imp && imp.qualified);
  chipEl.textContent = qualified ? "IMPACT QUALIFIED" : "REVIEW REQUIRED";
  chipEl.className = `chip ${qualified ? "ok" : "bad"}`;

  const evaluated = (imp?.evaluated || [])
    .map(
      (e) => `<div class="attempt-row">
        <span class="hash">${esc(e.artifact_id)}</span>
        ${chip(e.qualified ? "QUALIFIED" : "NOT AFFECTED", e.qualified ? "ok" : "")}
        <span class="dz-sub">${esc(
          e.qualified ? "all four conditions hold" : e.failed_conditions.join(", "),
        )}</span>
      </div>`,
    )
    .join("");

  const disagreed = (imp?.evaluated || []).filter((e) => e.agent_proposal_disagreed).length;

  out.innerHTML = `<div class="rows">
      ${row("Semantic runtime", esc(i.runtime_label || "—"))}
      ${row("Agent", esc(i.identity) + " " + chip(i.authority, "info"), "mono")}
      ${row("Changed requirement", esc(i.requirement_id || "—"), "mono")}
      ${row("Before", chip(i.previous_value, "warn"))}
      ${row("After", chip(i.current_value, "ok"))}
      ${row("Candidates proposed", `<b>${i.candidate_count}</b>`, "hl")}
      ${row("Crossing 1", chip(c1 ? c1.verdict : "—", c1 && c1.accepted ? "ok" : "bad"))}
      ${row("Impact gate", chip(imp ? imp.outcome : "—", qualified ? "ok" : "bad"))}
      ${row("Qualified target", qualified ? chip(imp.affected_artifact_id, "ok") : chip("NONE", "bad"))}
      ${row("Attempts", `<b>${i.attempts}</b>`, "hl")}
      ${i.total_tokens ? row("Tokens", `<b>${i.total_tokens}</b>`, "hl") : ""}
      ${i.adk_version ? row("ADK version", esc(i.adk_version), "mono") : ""}
    </div>
    <div class="sub-head" style="margin-top:14px">Deterministic qualification</div>
    <div class="attempts">${evaluated}</div>
    <div class="callout">${esc(i.authority_note)}
    ${
      disagreed
        ? ` The agent proposed ${disagreed} candidate${disagreed === 1 ? "" : "s"} the
            Truth Engine did not qualify; its opinion was not consulted.`
        : ""
    }</div>`;
}

function renderStages() {
  const auth = state.authorization_stage;
  const rem = state.remediation_state;
  const v = state.validated_execution;
  const c = state.crossing_2;
  const f = state.frontline;
  const dev = state.environment.show_diagnostics;
  void state.crossing_2;

  /* Implemented stages describe real change progress. Unimplemented capabilities are
     development diagnostics — they are never rendered as operational stages, and never
     counted, because a stage nobody built cannot be "pending" for this change. */
  const imp = state.impact;
  const intel = state.intel;
  const implemented = [
    {
      label: "Source Change Received",
      sub: `${state.scenario.change_id} · ${state.scenario.previous_version} → ${state.scenario.source_version} · derived from source`,
      chip: chip("RECEIVED", "ok"),
      done: true,
    },
    {
      label: "Impact Analysis",
      sub: impactStageSub(intel, imp),
      chip: impactStageChip(intel, imp),
      done: Boolean(imp && imp.qualified),
      denied: Boolean(imp && imp.requires_review),
    },
    {
      /* Policy eligibility is not a grant. This stage reflects a capability actually
         obtained and used, which cannot be true before remediation runs. Eligibility
         lives in the Agent Fleet matrix, where it belongs. */
      label: "Authorization",
      sub: auth.granted
        ? `${auth.identity} → ${auth.capability}`
        : auth.status === "DENIED"
          ? `${auth.identity} → ${auth.capability}`
          : "Awaiting remediation request",
      chip: chip(auth.status, auth.granted ? "ok" : auth.status === "DENIED" ? "bad" : ""),
      done: auth.granted,
      denied: auth.status === "DENIED",
    },
    {
      label: "Artifact Remediation",
      sub: v
        ? `${v.remediation_type} · dispatch count ${v.dispatch_count}`
        : remediationStageSub(rem),
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
      label: "Delivery Established",
      sub: state.delivery
        ? `${state.delivery.channel} · receipt ${state.delivery.receipt_id || "—"}`
        : "Awaiting composed delta",
      chip: state.delivery
        ? chip(state.delivery.status, state.delivery.delivery_established ? "ok" : "bad")
        : chip("PENDING"),
      done: Boolean(state.delivery && state.delivery.delivery_established),
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
    {
      label: "Field Evidence Observed",
      sub: fieldStageSub(state.field_verification),
      chip: fieldStageChip(state.field_verification),
      /* Done means "a validated observation exists", never "the change passed".
         The comparison that would decide that is not built. */
      done: Boolean(state.field_verification && state.field_verification.observation),
    },
    {
      label: "Deterministic Verdict",
      sub: verdictStageSub(state.verdict),
      chip: verdictStageChip(state.verdict),
      done: Boolean(state.verdict && state.verdict.result === "PASS"),
      denied: Boolean(state.verdict && state.verdict.result === "FAIL"),
    },
  ];

  implemented.push({
    label: "Change Proof",
    sub: proofStageSub(state.proof),
    chip: proofStageChip(state.proof),
    done: Boolean(state.proof && state.proof.status === "PROOF_COMPLETE"),
  });

  const notImplemented = [];

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

  /* An operational status, not a fraction. "7 / 8 COMPLETE" reads as near-deployment
     even while the change is unverified — a count of finished steps is not a statement
     about the change, and this header should only ever say what is true of the change. */
  const status = changeStatus();
  $("pipeline-chip").textContent = status.label;
  $("pipeline-chip").className = `chip ${status.tone}`;
  void c;
}

/* The single honest headline for the whole change. Derived from deterministic state
 * only — never from how many UI steps happen to be filled in. */
function changeStatus() {
  const v = state.verdict;
  const p = state.proof;
  if (p && p.status === "PROOF_COMPLETE") return { label: "DEPLOYED · VERIFIED", tone: "ok" };
  if (p && p.eligible) return { label: "AWAITING CHANGE PROOF", tone: "warn" };
  if (v && v.change_deployed) return { label: "CHANGE DEPLOYED", tone: "ok" };
  if (v && v.result === "PASS") return { label: "VERIFICATION PASSED", tone: "ok" };
  if (v && v.result === "FAIL") return { label: "VERIFICATION FAILED", tone: "bad" };
  if (v && v.result === "INCONCLUSIVE") return { label: "MORE EVIDENCE REQUIRED", tone: "warn" };
  if (v && v.status === "EVALUATING") return { label: "EVALUATING", tone: "warn" };
  if (state.delivery && state.delivery.delivery_established)
    return { label: "AWAITING FIELD EVIDENCE", tone: "warn" };
  if (state.validated_execution) return { label: "AWAITING DELIVERY", tone: "warn" };
  const imp = state.impact;
  if (imp && imp.requires_review) return { label: "IMPACT REVIEW REQUIRED", tone: "bad" };
  if (imp && imp.qualified) return { label: "IMPACT QUALIFIED", tone: "warn" };
  if (state.intel && state.intel.status !== "PENDING" && !state.intel.succeeded)
    return { label: "IMPACT REVIEW REQUIRED", tone: "bad" };
  return { label: "SOURCE CHANGE RECEIVED", tone: "" };
}

function proofStageSub(p) {
  if (!p) return "Awaiting completion conditions";
  if (p.status === "PROOF_COMPLETE")
    return `${p.satisfied_count} / ${p.total} conditions · ${p.proof_id}`;
  if (p.eligible) return `${p.satisfied_count} / ${p.total} conditions — ready to generate`;
  return `${p.satisfied_count} / ${p.total} conditions — ${(p.blockers || []).join("; ")}`;
}

function proofStageChip(p) {
  if (!p) return chip("PENDING");
  if (p.status === "PROOF_COMPLETE") return chip("PROOF COMPLETE", "ok");
  if (p.eligible) return chip("ELIGIBLE", "warn");
  return chip("BLOCKED", "");
}

function impactStageSub(intel, imp) {
  if (!intel || intel.status === "PENDING") return "Awaiting analysis of the source change";
  if (!intel.provider_configured) return "Live change intelligence is not configured";
  if (!intel.succeeded) return `Analysis did not produce a usable proposal — ${intel.status}`;
  if (imp && imp.qualified)
    return `${imp.candidate_count} candidates evaluated · ${imp.affected_artifact_id} qualified`;
  return `${imp ? imp.qualified_count : 0} qualified of ${imp ? imp.candidate_count : 0} — review required`;
}

function impactStageChip(intel, imp) {
  if (!intel || intel.status === "PENDING") return chip("PENDING");
  if (!intel.succeeded) return chip("REVIEW REQUIRED", "bad");
  if (imp && imp.qualified) return chip("QUALIFIED", "ok");
  return chip("REVIEW REQUIRED", "bad");
}

function verdictStageSub(v) {
  if (!v || !v.result) return "Awaiting a validated field observation";
  if (v.result === "PASS") return `Expected ${v.expected_value} · observed ${v.observed_value}`;
  if (v.result === "FAIL")
    return `Expected ${v.expected_value} but observed ${v.observed_value} — correct the work`;
  return "Evidence was not clear enough to decide";
}

function verdictStageChip(v) {
  if (!v || !v.result) return chip(v && v.status === "EVALUATING" ? "EVALUATING" : "PENDING");
  if (v.result === "PASS") return chip("PASSED", "ok");
  if (v.result === "FAIL") return chip("FAILED", "bad");
  return chip("INCONCLUSIVE", "warn");
}

/* Stage text for field evidence. Reports the observation; never grades it. */
function fieldStageSub(f) {
  if (!f || f.status === "AWAITING_EVIDENCE") return "Awaiting a physical field photo";
  if (f.rejected) return `Submission rejected — ${f.rejection_reason}`;
  if (!f.observation) return "No validated observation yet";
  return f.inconclusive
    ? "Model could not distinguish the position — more evidence required"
    : `Model observed ${f.observation} · Crossing 4 accepted`;
}

function fieldStageChip(f) {
  if (!f || f.status === "AWAITING_EVIDENCE") return chip("PENDING");
  if (f.rejected) return chip("REJECTED", "bad");
  if (!f.observation) return chip("NOT VALIDATED", "bad");
  return f.inconclusive ? chip("INCONCLUSIVE", "warn") : chip(f.observation, "ok");
}

/* A refused request is history, never the current state. */
function remediationStageSub(rem) {
  if (!rem) return "Awaiting deployment";
  if (rem.state === "AWAITING_IMPACT_QUALIFICATION")
    return "Awaiting impact qualification";
  if (rem.blocked_request_count)
    return `Awaiting remediation · ${rem.blocked_request_count} earlier request refused`;
  return "Awaiting remediation";
}

/* The Change Proof module. Every invariant shown is a real deterministic result from
 * the Truth Engine — never a hardcoded 7/7. */
function renderProof() {
  const p = state.proof;
  if (!p) return;
  const chipEl = $("proof-chip");
  const out = $("proof");
  const complete = p.status === "PROOF_COMPLETE";

  chipEl.textContent = complete
    ? "PROOF COMPLETE"
    : p.eligible
      ? "ELIGIBLE"
      : "BLOCKED";
  chipEl.className = `chip ${complete ? "ok" : p.eligible ? "warn" : ""}`;

  $("proof-actions").hidden = false;
  $("btn-proof").disabled = complete || !p.eligible;
  ["btn-proof-json", "btn-proof-download", "btn-proof-replay"].forEach((id) => {
    $(id).disabled = !complete;
  });

  const conditions = (p.conditions || [])
    .map(
      (c, i) => `<div class="attempt-row">
        <span class="idx">${String(i + 1).padStart(2, "0")}</span>
        ${chip(c.satisfied ? "MET" : "NOT MET", c.satisfied ? "ok" : "bad")}
        <span class="dz-sub">${esc(c.label)}</span>
      </div>`,
    )
    .join("");

  const gate = p.conditions && p.conditions.length
    ? `<div class="sub-head" style="margin-top:14px">Completion conditions
       <b>${p.satisfied_count} / ${p.total}</b></div>
       <div class="attempts">${conditions}</div>`
    : "";

  if (!complete) {
    out.innerHTML = `<div class="rows">
        ${row("Change Proof", chip(p.eligible ? "ELIGIBLE" : "BLOCKED", p.eligible ? "warn" : ""))}
        ${row("Change deployed", chip(String(p.change_deployed), "warn"))}
      </div>
      ${gate}
      ${p.blockers && p.blockers.length
        ? `<div class="callout">Blocked by: ${esc(p.blockers.join("; "))}</div>`
        : ""}`;
    return;
  }

  const s = p.summary || {};
  out.innerHTML = `<div class="observation verdict passed">
      <div class="obs-label">Change status</div>
      <div class="obs-value">DEPLOYED · VERIFIED</div>
      <div class="obs-note">All ${p.total} completion conditions hold. Runtime readiness
      is unchanged and separate.</div>
    </div>
    <div class="rows" style="margin-top:14px">
      ${row("Change", esc(s.change_id), "mono")}
      ${row("Source", `${esc(s.source_procedure_id)} ${esc(s.source_version)}`, "mono")}
      ${row("Affected artifact", chip(s.affected_artifact_id, "ok"))}
      ${row("Change", `${chip(s.previous_value, "warn")} → ${chip(s.current_value, "ok")}`)}
      ${row("Physical observation", chip(state.field_verification.observation || "—", "ok"))}
      ${row("Deterministic verdict", chip(s.verification_result, "ok"))}
      ${row("Verification event", esc(s.verification_event_id), "mono")}
      ${row("Delivery receipt", esc(s.delivery_ref), "mono")}
      ${row("Proof ID", esc(s.proof_id), "mono")}
      ${row("Proof content hash", esc(shortHash(s.content_hash)), "mono")}
      ${row("Completed", esc((s.completion_timestamp || "").replace("T", " ").split(".")[0]))}
      ${row("Proof size", `${s.byte_count} bytes`, "mono")}
    </div>
    ${gate}
    <div class="callout">
      <b>Verification.</b> ${esc(p.hash_meaning)}
      This is content integrity, not a signature, attestation, or trusted timestamp.
      <div class="dz-sub" style="margin-top:6px">${esc(p.download_hash_note || "")}</div>
    </div>`;
}

/* Implementation, runtime, and operation are three different questions. Rendering them
 * as one string is what made a wired comparator advertise itself as NOT WIRED. */
function renderCapabilityStatus() {
  const rows = (state.capability_status || [])
    .map((c) => {
      const implTone =
        c.implementation === "IMPLEMENTED" ? "ok" : c.implementation === "NOT_YET_WIRED" ? "warn" : "";
      const runtimeTone =
        c.runtime === "CONFIGURED" || c.runtime === "DETERMINISTIC"
          ? "ok"
          : c.runtime === "UNAVAILABLE"
            ? "warn"
            : "";
      return `<div class="cap-status">
        <div class="cap-status-head">
          <span class="name">${esc(c.label)}</span>
          ${chip(c.implementation.replace(/_/g, " "), implTone)}
        </div>
        <div class="rows">
          ${row("Runtime", chip(c.runtime.replace(/_/g, " "), runtimeTone) +
            ` <span class="dz-sub">${esc(c.runtime_detail)}</span>`)}
          ${row("Operation", chip(c.operation.replace(/_/g, " "), "info"))}
        </div>
      </div>`;
    })
    .join("");
  const el = $("capability-status");
  if (el) el.innerHTML = rows;
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

function renderRequestHistory() {
  const rem = state.remediation_state;
  const el = $("request-history");
  if (!el) return;
  const history = (rem && rem.request_history) || [];
  if (!history.length) {
    el.innerHTML = "";
    return;
  }
  const rows = history
    .map(
      (h) => `<div class="attempt-row">
        <span class="idx">${String(h.sequence).padStart(2, "0")}</span>
        ${chip(h.outcome.replace(/_/g, " "), h.executed ? "ok" : "warn")}
        <span class="dz-sub">${esc(h.reason || h.detail || "")}</span>
      </div>`,
    )
    .join("");
  el.innerHTML = `<div class="sub-head" style="margin-top:14px">Remediation requests</div>
    <div class="attempts">${rows}</div>
    <div class="callout">${esc(rem.note)}</div>`;
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
          ${a.semantic_runtime ? `<div class="dz-sub">${esc(a.semantic_runtime)}</div>` : ""}
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
      `<div class="empty">${esc(state.security_probe.identity)} will attempt
       <b>${esc(state.security_probe.capability)}</b> through the real policy seam.</div>`;
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

function renderDelivery() {
  const d = state.delivery;
  if (!d) {
    $("delivery-chip").textContent = "PENDING";
    $("delivery-chip").className = "chip";
    $("delivery").innerHTML =
      '<div class="empty">Delivery is established by a resolvable mechanism receipt validated at Crossing 3. Compose a change, then press <b>Deliver to Frontline</b>.</div>';
    return;
  }

  $("delivery-chip").textContent = d.status;
  $("delivery-chip").className = `chip ${d.delivery_established ? "ok" : "bad"}`;

  $("delivery").innerHTML = `<div class="rows">
      ${row("Delivery", chip(d.status, d.delivery_established ? "ok" : "bad"))}
      ${row("Last request", chip(d.last_request, d.last_request === "REJECTED" ? "bad" : "info"))}
      ${row("Receipt ID", esc(d.receipt_id || "—"), "mono")}
      ${row("Receipt integrity", chip(d.receipt_integrity, d.delivery_established ? "ok" : "bad"))}
      ${row("Payload hash", esc(shortHash(d.authoritative_payload_hash)), "mono")}
      ${row("Crossing 3", chip(d.crossing_3, d.crossing_3 === "ACCEPTED" ? "ok" : "bad"))}
      ${row("Channel", esc(d.channel), "mono")}
      ${row("Destination", esc(d.destination_ref), "mono")}
      ${row("Dispatch count", `<b>${d.dispatch_count}</b>`, "hl")}
      ${row("Field verified", chip(String(d.field_verified), "warn"))}
      ${row("Change deployed", chip(String(d.change_deployed), "warn"))}
    </div>
    ${d.identity_basis ? `<div class="callout">${esc(d.identity_basis)}</div>` : ""}`;
}

function renderFrontline() {
  const f = state.frontline;
  const panel = $("frontline");
  if (!f || !f.available) {
    const composed = Boolean(f && f.composed);
    $("frontline-chip").textContent = composed ? "AWAITING DELIVERY" : "AWAITING VALIDATION";
    $("frontline-chip").className = `chip ${composed ? "warn" : ""}`;
    panel.innerHTML = composed
      ? '<div class="empty">Delta composed. The worker view opens once delivery is established at Crossing 3.</div>'
      : '<div class="empty">The operational delta is composed once a change is validated at Crossing 2.</div>';
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

function renderFieldVerification() {
  const f = state.field_verification;
  const chipEl = $("field-chip");
  const drop = $("field-drop");
  const out = $("field-result");
  if (!f) return;

  $("field-formats").textContent =
    `or click to choose a file · ${(f.accepted_mime_types || []).join(", ")}`;

  /* A production instance with no provider configured hides the control rather than
     offering an action that cannot work. Development says so plainly instead. */
  const usable = Boolean(f.provider_configured);
  if (!usable && state.environment.is_production) {
    drop.hidden = true;
  } else {
    drop.hidden = false;
    drop.classList.toggle("busy", !usable);
  }

  const status = f.status || "AWAITING_EVIDENCE";
  const provider = f.provider || {};

  if (status === "AWAITING_EVIDENCE") {
    chipEl.textContent = "AWAITING EVIDENCE";
    chipEl.className = "chip";
    out.innerHTML = usable
      ? `<div class="empty">Photograph the physical box and upload it. The model
         reports only what it observes — it is never asked whether the change passed.</div>`
      : `<div class="empty">Live field observation is not configured on this instance.
         Nothing is assumed in its place.</div>`;
    return;
  }

  if (f.rejected) {
    chipEl.textContent = "REJECTED";
    chipEl.className = "chip bad";
    out.innerHTML = `<div class="rows">
        ${row("Submission", chip(f.rejection_reason, "bad"))}
        ${row("Detail", esc(f.detail || ""))}
      </div>
      <div class="callout">No observation was made and nothing was recorded against
      the change. Upload a supported image to try again.</div>`;
    return;
  }

  if (status === "PROVIDER_DISABLED" || status === "PROVIDER_UNAVAILABLE") {
    chipEl.textContent = "NOT CONFIGURED";
    chipEl.className = "chip warn";
    out.innerHTML = `<div class="rows">
        ${row("Image integrity", esc(shortHash(f.image_sha256)), "mono")}
        ${row("Container", chip(f.container || "—", "info"))}
        ${row("Actual MIME", esc(f.mime_type || "—"), "mono")}
        ${row("Observation", chip("NOT ATTEMPTED", "warn"))}
      </div>
      <div class="callout">${esc(f.detail || "")}</div>`;
    return;
  }

  const c4 = f.crossing_4;
  const accepted = Boolean(c4 && c4.accepted);
  const observation = f.observation;

  chipEl.textContent = accepted
    ? f.inconclusive
      ? "MORE EVIDENCE REQUIRED"
      : "OBSERVATION VALIDATED"
    : "NOT VALIDATED";
  chipEl.className = `chip ${accepted ? (f.inconclusive ? "warn" : "ok") : "bad"}`;

  const observationBlock = accepted
    ? `<div class="observation ${f.inconclusive ? "inconclusive" : ""}">
         <div class="obs-label">Model observation · ${esc(f.observation_source || "model")}</div>
         <div class="obs-value">${esc(f.inconclusive ? "MORE EVIDENCE REQUIRED" : observation)}</div>
         <div class="obs-note">${
           f.inconclusive
             ? "The model looked and could not reliably distinguish the label position. That is a valid observation, not an error — submit a clearer photograph to create a new attempt."
             : `Reported by ${esc(provider.model || f.model || "the model")}. This is an observation of what is on the box, not a judgement about whether the change is correct.`
         }</div>
       </div>`
    : `<div class="observation">
         <div class="obs-label">Model observation</div>
         <div class="obs-value">NOT VALIDATED</div>
         <div class="obs-note">Crossing 4 rejected the observation, so nothing downstream
         may read it. ${esc((f.crossing_4?.rejections || []).join(", "))}</div>
       </div>`;

  out.innerHTML = `<div class="rows">
      ${row("Evidence", chip("RECEIVED", "ok"))}
      ${row("Image integrity", esc(shortHash(f.image_sha256)), "mono")}
      ${row("Container", chip(f.container || "—", "info"))}
      ${row("Actual MIME", esc(f.mime_type || "—"), "mono")}
      ${row("Declared type", esc(f.declared_content_type || "—") +
        (f.declared_type_matched_bytes === false
          ? ` ${chip("IGNORED", "warn")}`
          : ""), "mono")}
      ${row("Provider", esc(providerLabel(f, provider)))}
      ${row("Attempts", `<b>${f.attempt_count ?? "—"}</b>`, "hl")}
      ${row("Provider calls", `<b>${f.provider_calls ?? 0}</b>`, "hl")}
      ${row("Crossing 4", chip(c4 ? c4.verdict : "NOT RUN", accepted ? "ok" : "bad"))}
      ${row("Field verified", chip(String(f.field_verified), "warn"))}
      ${row("Change deployed", chip(String(f.change_deployed), "warn"))}
    </div>
    ${observationBlock}
    ${renderVerdictBlock(f)}
    ${renderAttempts(f)}`;
}

/* The deterministic verdict. Visually separate from the model observation, and labelled
 * with a different authority, because they are different kinds of claim: one is what a
 * model reported, the other is what the Truth Engine decided. */
function renderVerdictBlock(f) {
  const v = state.verdict;
  if (!v) return "";

  if (v.status === "AWAITING_EVIDENCE" || v.status === "EVALUATING") {
    return `<div class="observation verdict ${v.status === "EVALUATING" ? "evaluating" : ""}">
      <div class="obs-label">Deterministic verdict · ${esc(v.authority)}</div>
      <div class="obs-value">${v.status === "EVALUATING" ? "EVALUATING" : "AWAITING EVIDENCE"}</div>
      <div class="obs-note">${esc(v.remaining_condition || "")}</div>
    </div>`;
  }

  if (!v.result) {
    return `<div class="observation verdict bad">
      <div class="obs-label">Deterministic verdict · ${esc(v.authority)}</div>
      <div class="obs-value">NOT ADJUDICATED</div>
      <div class="obs-note">${esc(v.rejection_reason || v.status)}</div>
    </div>`;
  }

  const tone = { PASS: "passed", FAIL: "failed", INCONCLUSIVE: "inconclusive" }[v.result];
  const headline = { PASS: "PASSED", FAIL: "FAILED", INCONCLUSIVE: "MORE EVIDENCE REQUIRED" }[
    v.result
  ];
  const action = {
    PASS: "The physical work matches the approved change.",
    FAIL: "CORRECT THE WORK AND PROVIDE NEW EVIDENCE",
    INCONCLUSIVE: "The evidence was not clear enough to decide. Provide a clearer photo.",
  }[v.result];

  return `<div class="observation verdict ${tone}">
      <div class="obs-label">Deterministic verdict · ${esc(v.authority)}</div>
      <div class="obs-value">${esc(headline)}</div>
      <div class="rows verdict-rows">
        ${row("Expected", chip(v.expected_value, "info"))}
        ${row("Observed", chip(v.observed_value, v.result === "PASS" ? "ok" : "warn"))}
        ${row("Decision authority", esc(v.authority))}
        ${row("Workflow state", esc(v.workflow_state || "—"), "mono")}
        ${row("Change verified", chip(String(v.change_verified), v.change_verified ? "ok" : ""))}
        ${row("Change deployed", chip(String(v.change_deployed), "warn"))}
      </div>
      <div class="obs-note action">${esc(action)}</div>
      ${v.remaining_condition ? `<div class="obs-note">${esc(v.remaining_condition)}</div>` : ""}
    </div>`;
}

function providerLabel(f, provider) {
  const model = provider.model || f.model || "";
  const route = f.provider_name === "vertex_ai_maas" ? "Vertex AI MaaS" : f.provider_name || "";
  return [model, route].filter(Boolean).join(" · ") || "—";
}

function renderAttempts(f) {
  const history = f.history || [];
  if (history.length < 2) return "";
  const rows = history
    .map(
      (a, i) => `<div class="attempt-row">
        <span class="idx">${String(i + 1).padStart(2, "0")}</span>
        <span class="hash">${esc(shortHash(a.image_sha256))}</span>
        <span>${esc(a.mime_type || "")}</span>
        ${chip(a.observation || a.status, a.observation === "INCONCLUSIVE" ? "warn" : "info")}
      </div>`,
    )
    .join("");
  return `<div class="sub-head" style="margin-top:14px">Submission history</div>
          <div class="attempts">${rows}</div>
          <div class="callout">Every attempt is kept. A new photograph is a new attempt,
          never a replacement for the last one.</div>`;
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
  renderSource();
  renderIntel();
  renderStages();
  renderArtifact();
  renderEvidenceSummary();
  renderRequestHistory();
  renderCapabilityStatus();
  renderProof();
  renderFleet();
  renderSecurity();
  renderTimeline();
  renderEvidenceBar();
  renderDelivery();
  renderFrontline();
  renderFieldVerification();
  renderFuture();
}

async function refresh(promise) {
  const buttons = [
    $("btn-proof"),
    $("btn-analyze"),
    $("btn-deploy"),
    $("btn-deliver"),
    $("btn-reset"),
    $("btn-security"),
  ];
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

$("btn-analyze").addEventListener("click", () => refresh(call("/api/hero/analyze", "POST")));
$("btn-deploy").addEventListener("click", () => refresh(call("/api/hero/deploy", "POST")));
$("btn-deliver").addEventListener("click", () => refresh(call("/api/hero/deliver", "POST")));
$("btn-reset").addEventListener("click", () => {
  selectedEvidence = null;
  refresh(call("/api/hero/reset", "POST"));
});
$("btn-security").addEventListener("click", () =>
  refresh(call("/api/hero/security-test", "POST")),
);
/* ---------------------------------------------------------------- field upload */

const dropZone = $("field-drop");
const fileInput = $("field-file");

async function submitFieldEvidence(file) {
  if (!file) return;
  dropZone.classList.add("busy");
  try {
    state = await upload("/api/hero/field-evidence", file);
    render();
    const f = state.field_verification || {};
    toast(
      f.rejected
        ? `Rejected: ${f.rejection_reason}`
        : f.observation
          ? `Observation: ${f.observation}`
          : "Evidence submitted",
    );
  } catch (error) {
    toast(String(error.message || error));
  } finally {
    dropZone.classList.remove("busy");
    fileInput.value = "";
  }
}

dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    fileInput.click();
  }
});
["dragenter", "dragover"].forEach((name) =>
  dropZone.addEventListener(name, (e) => {
    e.preventDefault();
    dropZone.classList.add("dragover");
  }),
);
["dragleave", "drop"].forEach((name) =>
  dropZone.addEventListener(name, (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
  }),
);
dropZone.addEventListener("drop", (e) => submitFieldEvidence(e.dataTransfer?.files?.[0]));
fileInput.addEventListener("change", () => submitFieldEvidence(fileInput.files[0]));

/* ---------------------------------------------------------------- proof actions */

$("btn-proof").addEventListener("click", () => refresh(call("/api/hero/proof", "POST")));

$("btn-proof-json").addEventListener("click", async () => {
  try {
    const doc = await call("/api/hero/proof");
    /* The stored canonical bytes, not a re-serialisation of the model. */
    $("proof-json").textContent = doc.canonical_json;
    $("proof-json").hidden = false;
    toast(`Canonical proof · ${doc.content_hash.slice(0, 12)}…`);
  } catch (error) {
    toast(String(error.message || error));
  }
});

$("btn-proof-download").addEventListener("click", async () => {
  try {
    const response = await fetch("/api/hero/proof/download");
    if (!response.ok) throw new Error(`download → ${response.status}`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${state.proof.proof_id}.json`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    toast(String(error.message || error));
  }
});

$("btn-proof-replay").addEventListener("click", async () => {
  try {
    /* Reads recorded evidence. Dispatches nothing, calls no model, mutates nothing. */
    const audit = await call("/api/hero/proof/replay");
    $("proof-json").innerHTML = highlightJson(audit);
    $("proof-json").hidden = false;
    toast(
      `Replayed ${audit.verification_chronology.length} verification attempt(s) · ` +
        `${audit.side_effects_executed} side effects`,
    );
  } catch (error) {
    toast(String(error.message || error));
  }
});

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
