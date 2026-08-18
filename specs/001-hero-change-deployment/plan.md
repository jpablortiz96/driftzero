# Implementation Plan: Hero Change Deployment

**Branch**: `001-hero-change-deployment` | **Date**: 2026-08-17 | **Spec**: [spec.md](spec.md)

## Summary

DRIFTZERO's hero workflow autonomously processes an approved operational procedure change (LEFT → TOP-RIGHT label placement), identifies the affected downstream artifact, remediates it, delivers the delta to a frontline worker, verifies physical execution via camera evidence, and produces an immutable Change Proof. The implementation uses a hybrid deterministic + LLM architecture: a pure-Python Truth Engine owns all authoritative state/transitions/invariants while Google ADK Python agents handle semantic interpretation tasks (change extraction, remediation composition, delta delivery, vision observation). Gemini 3.5 Flash powers the semantic agents; Gemma 4 provides multimodal field verification; Veo 3.1 optionally generates microtraining video. Firestore is the authoritative state store; Pub/Sub provides event-driven ingestion; Cloud Run hosts the runtime; Cloud Storage holds immutable evidence. Gemini Enterprise Agent Platform (GEAP) capabilities — Agent Runtime, Agent Registry, Agent Identity, Agent Gateway, Model Armor, advanced Agent Observability — are **track enhancements gated on account access**; the core hero workflow runs entirely on Cloud Run + ADK + Firestore + Pub/Sub + Cloud Storage and never depends on successful GEAP provisioning.

## Technical Context

**Language/Version**: Python 3.11+
**Primary Dependencies**: google-adk>=2.0.0, google-cloud-firestore, google-cloud-pubsub, google-cloud-storage, fastapi, uvicorn, pydantic>=2.0
**Storage**: Firestore (Standard Edition) for workflow state; Cloud Storage for raw evidence/artifacts
**Testing**: pytest, pytest-asyncio
**Target Platform**: Cloud Run (serverless containers), optional Agent Runtime
**Project Type**: Multi-agent enterprise workflow service with thin web frontend
**Performance Goals**: Hero workflow completion in <5 minutes (excluding async field verification wait). Field verification response <10 seconds.
**Constraints**: Hackathon credit budget. Single developer. Scale-to-zero when idle.
**Scale/Scope**: 1 concurrent workflow sufficient for demo. Not a production platform.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| # | Principle | Status | Notes |
|---|---|---|---|
| I | Spec Before Code | PASS | Frozen spec exists with 11 FRs, 15 SCs |
| II | Evidence Over Claims | PASS | Evidence pack designed, no invented metrics |
| III | Deterministic Truth Boundary | PASS | Truth Engine owns all authoritative logic |
| IV | Least Privilege | PASS | Per-agent permission boundaries defined; primary model is one Agent Identity per agent, with per-agent service accounts as the explicit fallback runtime identity |
| V | Safe Autonomous Action | PASS | 9-condition autonomy gate, REVIEW_REQUIRED default |
| VI | Persistent State ≠ LLM Memory | PASS | Firestore is authoritative, not ADK session/Memory Bank |
| VII | Observable by Default | PASS | Correlation IDs, state transition logs, agent traces |
| VIII | Failure Is First-Class | PASS | 13 lifecycle states including FAILED, REVIEW_REQUIRED, VERIFICATION_INCONCLUSIVE |
| IX | Google-Native Where It Matters | PASS | Gemini 3.5 Flash + ADK + Firestore + Pub/Sub + Cloud Run + Gemma 4 |
| X | Frontline-First | PASS | Worker receives delta, phone-based verification |
| XI | Narrow Complete Core | PASS | One hero workflow, no dashboard, no auth system |
| XII | Hackathon Reproducibility | PASS | Fixtures, quickstart, evidence pack, JUDGES_START_HERE |
| XIII | No Hidden Simulation | PASS | Data classification with lineage; synthetic fixtures labeled |
| XIV | Definition of Done = Verified | PASS | Test strategy maps to every FR and SC |

## Project Structure

```text
specs/001-hero-change-deployment/
├── spec.md
├── plan.md              # This file
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── agents.md
└── checklists/
    └── requirements.md
```

```text
src/
├── driftzero/
│   ├── __init__.py
│   ├── truth_engine/           # Deterministic core (NO LLM)
│   │   ├── __init__.py
│   │   ├── state_machine.py    # 13 states, legal transitions
│   │   ├── idempotency.py      # Duplicate detection
│   │   ├── supersession.py     # Version applicability
│   │   ├── autonomy_gate.py    # 9-condition remediation check
│   │   ├── verification.py     # Expected vs observed comparator
│   │   ├── proof_generator.py  # 7-invariant check + SHA-256 proof
│   │   └── evidence.py         # Evidence manifest, data classification
│   ├── agents/                 # ADK agent definitions
│   │   ├── __init__.py
│   │   ├── change_intel.py     # Change Intelligence Agent
│   │   ├── remediation.py      # Remediation Agent
│   │   ├── enablement.py       # Frontline Enablement Agent
│   │   ├── field_verify.py     # Field Verification Agent (Gemma)
│   │   └── orchestrator.py     # SequentialAgent workflow
│   ├── models/                 # Pydantic data models
│   │   ├── __init__.py
│   │   ├── change.py           # ApprovedChange, ChangeSet
│   │   ├── workflow.py         # Workflow, WorkflowState
│   │   ├── verification.py     # VerificationEvent, FieldObservation
│   │   ├── proof.py            # ChangeProof, EvidenceManifest
│   │   └── classification.py   # DataClassification, LineageEntry
│   ├── store/                  # Persistence layer
│   │   ├── __init__.py
│   │   ├── firestore.py        # Firestore client
│   │   └── gcs.py              # Cloud Storage client
│   ├── api/                    # FastAPI routes
│   │   ├── __init__.py
│   │   └── routes.py
│   ├── web/                    # Thin HTML/JS demo frontend
│   │   ├── static/
│   │   └── templates/
│   └── cli.py                  # CLI for testing/demo
├── tests/
│   ├── unit/
│   │   └── truth_engine/       # Deterministic logic tests
│   ├── integration/            # Agent + store integration
│   ├── multimodal/             # Gemma fixture evaluation
│   └── security/               # Prompt injection tests
├── fixtures/                   # Reproducible test fixtures
│   ├── hero_change.json
│   ├── stale_artifact.json
│   ├── unrelated_artifact.json
│   └── multimodal/
├── evidence/                   # Evidence pack (see quickstart.md)
├── Dockerfile
├── pyproject.toml
└── .env.example
```

## Architecture

### Component Diagram

```mermaid
graph TB
    subgraph "External"
        PS[Pub/Sub Topic]
        WORKER[Frontline Worker Phone]
    end

    subgraph "Cloud Run"
        API[FastAPI API]
        TE[Truth Engine]
        ORCH[ADK SequentialAgent]

        subgraph "ADK Agents"
            CI[Change Intel Agent]
            RM[Remediation Agent]
            FE[Enablement Agent]
        end
    end

    subgraph "Cloud Run GPU"
        FV[Gemma 4 Verification]
    end

    subgraph "Google Cloud"
        FS[(Firestore)]
        GCS[(Cloud Storage)]
    end

    PS --> API
    API --> TE
    TE --> ORCH
    ORCH --> CI
    ORCH --> RM
    ORCH --> FE
    WORKER --> API
    API --> FV
    FV --> TE
    TE --> FS
    TE --> GCS
    CI --> GCS
    RM --> GCS
```

### Google Service Decision Matrix

| Technology | Classification | Justification |
|---|---|---|
| Gemini 3.5 Flash | `CORE` | Primary LLM for semantic agents. Hackathon-mandatory. |
| Google ADK Python 2.0 | `CORE` | Agent framework with deterministic workflow support. |
| Firestore | `CORE` | Authoritative workflow state, idempotency, proof records. |
| Pub/Sub | `CORE` | Event-driven change ingestion (real event, synthetic content). |
| Cloud Run | `CORE` | Serverless compute runtime for agents and API. |
| Cloud Storage | `CORE` | Immutable evidence store for artifacts and raw evidence. |
| Gemma 4 12B | `DEMO_CRITICAL` | Multimodal field verification (second Google AI model). |
| Veo 3.1 | `BONUS` | Optional microtraining video generation. Non-blocking. |
| Agent Runtime | `TRACK_ENHANCEMENT` | Managed async execution. Use if accessible; Cloud Run is the fallback. |
| Agent Registry | `TRACK_ENHANCEMENT` | Prerequisite for the Agent Gateway path (Gateway blocks egress to unregistered hosts). Also provides agent discovery evidence. |
| Agent Identity | `TRACK_ENHANCEMENT` | One SPIFFE-based Agent Identity per agent, used as the IAM principal in tool authorization policies. NOT a service account. |
| Agent Gateway | `TRACK_ENHANCEMENT` | One exact governed egress path: Remediation Agent → Gateway → Artifact Mutation Tool, with an enforced DENY for the Frontline Enablement Agent. |
| Model Armor | `TRACK_ENHANCEMENT` | Screening of untrusted artifact **text** at the Vertex AI `generateContent` call only. Not an authorization mechanism. |
| Agent Observability (advanced) | `TRACK_ENHANCEMENT` | Platform-level agent/gateway telemetry. Fallback is OpenTelemetry + Cloud Trace/Logging from Cloud Run. |
| Memory Bank | `DEFER` | Adds complexity without improving hero workflow truth. |
| Lyria 3 | `DEFER` | Weak frontline use case, poor effort-to-value ratio. |

### Deterministic Truth Engine

The Truth Engine is pure Python application logic (NO LLM calls). It owns:

- **State machine**: 13 canonical states, validated legal transitions
- **Idempotency**: `change_id`-based duplicate detection
- **Supersession**: Source version applicability check; automatic `SUPERSEDED` transition
- **Autonomy gate**: 9 preconditions evaluated deterministically
- **Verification comparator**: `observed == expected → PASS`, else `FAIL` or `INCONCLUSIVE`
- **Event ordering**: Monotonic `event_sequence` per workflow
- **Proof invariants**: 7 conditions checked before `PROOF_COMPLETE`
- **Evidence manifest**: SHA-256 content hashes for all referenced artifacts
- **Retry deduplication**: Completed logical actions tracked, not re-executed

### Orchestration Strategy

**Hybrid deterministic + LLM**: ADK `SequentialAgent` defines the macro workflow. The Truth Engine validates preconditions/postconditions at each step boundary. LLM agents handle semantic tasks within their bounded scope.

**Agent hallucination handling**: Every agent output is validated against Pydantic schemas by the Truth Engine. Invalid structured output → retry with timeout → `REVIEW_REQUIRED`. Schema validity alone never authorizes a state transition — see § Tool Response Validation Chain.

### Agent Identity and Runtime Identity Model

Two distinct concepts, never conflated (see research.md R-013):

- **Agent Identity** — a SPIFFE-based, per-agent cryptographic identity issued by Google Cloud when an agent is deployed to Agent Runtime with agent identity enabled. It is not shared across workloads, cannot be impersonated, and has no long-lived keys. It is used as an IAM **principal** in allow policies:
  `principal://agents.global.org-ORGANIZATION_ID.system.id.goog/resources/aiplatform/projects/PROJECT_NUMBER/locations/LOCATION/reasoningEngines/AGENT_ENGINE_ID`
- **Service account** — a conventional Google Cloud principal (`serviceAccount:NAME@PROJECT.iam.gserviceaccount.com`). In DRIFTZERO a service account is used **only** as (a) a Cloud Run service/runtime identity, or (b) the explicit fallback identity when Agent Identity cannot be provisioned. A service account is never called an "Agent Identity".

**Primary model (GEAP available) — one Agent Identity per agent:**

| Agent | Agent Identity | Authorized capability boundary |
|---|---|---|
| Change Intelligence Agent | `driftzero-change-intel` agent identity | READ-ONLY: approved source procedure (GCS), artifact registry (Firestore). No mutation capability of any kind. |
| Remediation Agent | `driftzero-remediation` agent identity | **The only identity authorized to invoke the Artifact Mutation Tool.** Read affected artifact; write only authorized derived artifacts. No write to the authoritative source. |
| Frontline Enablement Agent | `driftzero-enablement` agent identity | READ + NOTIFY: read ChangeSet/remediation result, deliver delta, optionally call Veo. **Explicitly NOT authorized for the Artifact Mutation Tool** — this is the negative security test subject. |
| Field Verification Agent | `driftzero-field-verify` agent identity (where the agent is deployed on Agent Runtime; if it remains a Cloud Run GPU inference service, that service runs under its own dedicated Cloud Run service account and is documented as a runtime identity, not an Agent Identity) | READ raw evidence + inference only. No workflow state authority, no PASS/FAIL authority, no mutation capability. |

**Fallback model (Agent Identity not provisionable):** each agent runs under its own dedicated, minimally-scoped Cloud Run **service account**, and the mutation-capability restriction is enforced by a deterministic in-process authorization broker in the Truth Engine keyed on the calling agent identifier. This fallback is weaker (bearer-token principals, impersonation possible, no cryptographic per-agent attestation) and MUST be recorded as such in `LIMITATIONS.md`. No evidence artifact may describe the fallback as platform-enforced Agent Identity.

### Agent Gateway Governed Path

Exactly one governed path is planned for the track enhancement. Official capability review (research.md R-003, Agent Gateway) confirms this path is supported: the Gateway governs **Agent-to-Anywhere (egress)** HTTP traffic including MCP, authorizes on the calling agent's SPIFFE identity, and can restrict access to individual MCP tools by tool name and read-only/read-write character. It is therefore planned concretely rather than deferred, but it stays subject to the GEAP Availability Gate.

| Attribute | ALLOW case | DENY case (negative security test) |
|---|---|---|
| **Traffic direction** | Egress (Agent-to-Anywhere) | Egress (Agent-to-Anywhere) |
| **Caller** | Remediation Agent, authenticating with its Agent Identity (SPIFFE principal) | Frontline Enablement Agent, authenticating with its Agent Identity |
| **Path** | Remediation Agent → Agent Gateway → Artifact Mutation Tool | Frontline Enablement Agent → Agent Gateway → Artifact Mutation Tool → **DENIED** |
| **Target tool** | `artifact-mutation-tool`, registered in Agent Registry, exposing one read-write operation `apply_authorized_artifact_patch` | Same registered tool and operation |
| **Required identity** | `principal://agents.global.org-.../reasoningEngines/driftzero-remediation` | `principal://agents.global.org-.../reasoningEngines/driftzero-enablement` |
| **Allow policy** | `roles/iap.egressor` on the registered mutation-tool endpoint granted to the Remediation Agent principal only, plus an MCP tool-name-scoped authorization policy permitting the read-write tool for that principal | No `roles/iap.egressor` binding and no tool-name allow entry for the Enablement principal → IAP runtime enforcement rejects the call |
| **Expected audit evidence** | Gateway network-layer telemetry (Agent Observability), Cloud Logging entry carrying the caller SPIFFE principal + tool name + allow decision, Cloud Trace span, and the Truth-Engine-recorded before/after artifact refs | Cloud Logging deny entry carrying the caller SPIFFE principal + tool name + deny decision, plus positive proof of non-effect: artifact SHA-256 unchanged and no `REMEDIATION_COMPLETED` transition. Stored as `evidence/security/gateway_deny_enablement_to_mutation_tool.json` |

**Rules:**
- The Artifact Mutation Tool is **not implemented in this correction**. It is specified here only as a planned authenticated tool/service compatible with the Gateway protocol selected at implementation time (MCP is the documented option that supports per-tool authorization).
- If Agent Gateway is promoted to the demo, the DENY must come from **real platform authorization/policy enforcement**, not from an application-level check. `DRY_RUN` (`iamEnforcementMode`) may be used during staging to capture would-be-deny audit entries, but a demo claim of enforcement requires enforcement mode.
- If Agent Gateway is not accessible (see GEAP Availability Gate), the ALLOW/DENY pair still executes against the in-process deterministic authorization broker, and the evidence is labelled **application-level enforcement, not platform-enforced**. No judged claim of Gateway enforcement may be made in that case.

### Model Armor Enforcement Path

Model Armor has one narrow role: screening **untrusted text** before it is processed by an LLM agent. It is deliberately kept separate from domain authorization.

**Exact path:**
`untrusted artifact TEXT → Vertex AI generateContent call carrying modelArmorConfig.promptTemplateName → Model Armor screening (INSPECT_AND_BLOCK) → Change Intelligence Agent processing only if allowed`

- **Screened content**: text extracted from the derived downstream operational artifact and from the approved-change payload, i.e. the untrusted strings interpolated into Change Intelligence Agent prompts. Model responses are screened via `responseTemplateName`.
- **Enforcement point**: the Vertex AI `generateContent` request itself — the only integration Google documents for this traffic (research.md R-003, Model Armor). This path requires neither Agent Gateway nor Agent Registry nor a Google Cloud Organization, so it survives independently if the Gateway path is unavailable.
- **Template**: `driftzero-untrusted-artifact-text`, enforcement `INSPECT_AND_BLOCK`, targeting prompt injection and jailbreak detection.
- **On block**: the call returns `blockReason: "MODEL_ARMOR"`; the Truth Engine records a `ScreeningBlocked` evidence item (classification `REAL`) and the workflow fails closed to `REVIEW_REQUIRED`. It never proceeds on unscreened content silently.
- **On documented fail-open**: Google documents that during a Model Armor outage the sanitization step is skipped and processing continues. The Truth Engine therefore records `SCREENING_SKIPPED` in the evidence manifest so no evidence artifact can claim screening that did not happen, and all Truth Engine scope/authorization/semantic validation still applies.
- **Not screened**: field verification images. Model Armor image screening is Preview and document content is explicitly unsupported; image evidence integrity is handled by hashing plus the deterministic comparator, and this limitation is stated in `LIMITATIONS.md`.
- **Adversarial fixture**: one reproducible text fixture, `fixtures/security/injected_artifact_text.json` — a downstream artifact whose instruction text embeds an injection attempting to redirect the agent (e.g. instructing it to mark an unauthorized artifact as remediated).

**Model Armor MUST NOT be described as enforcing** artifact authorization, workflow state transitions, proof completion conditions, or semantic correctness of structured business data. Those are Truth Engine responsibilities and remain deterministic and local, exactly as they are in the fallback architecture where Model Armor is absent.

### Tool Response Validation Chain (Tool Poisoning Defense)

**Threat — Tool Poisoning / Malicious Tool Response**: a tool or MCP server returns a response that is **schema-valid but semantically unauthorized** — e.g. `RemediationResult` naming an `artifact_id` outside `authorized_scope`, citing a `source_version` inconsistent with the workflow under processing, or referencing before/after objects the workflow never wrote. Pydantic accepts it because the shape is correct. Schema validation alone is therefore explicitly **not sufficient**.

Every tool response passes five deterministic layers in the Truth Engine before it can influence state:

| # | Layer | Check | Rejection outcome |
|---|---|---|---|
| 1 | Schema validation | Pydantic model conformance, enum domains, required fields | Retry, then `REVIEW_REQUIRED` |
| 2 | Provenance / expected-source validation | Response arrived from the tool endpoint this workflow actually invoked, correlated by `correlation_id` + `event_sequence`; unsolicited or out-of-band responses discarded | Discard + record `UNSOLICITED_TOOL_RESPONSE`; `REVIEW_REQUIRED` on repeat |
| 3 | Expected artifact / tool identity validation | Returned `artifact_id` equals the artifact the Truth Engine targeted; returned tool identity equals the registered tool the agent was authorized to call; content hashes of `before_ref` match the pre-state hash the Truth Engine recorded | `REVIEW_REQUIRED`, no state advance |
| 4 | Authorization / scope validation | `artifact_id ∈ authorized_scope`; calling agent identity is the mutation-authorized identity; `source_version` matches the applicable version and is not superseded | `REVIEW_REQUIRED`, no state advance |
| 5 | Deterministic semantic invariants | Exactly one atomic requirement change applied; `after_value` equals the approved `current_value`; no additional divergence introduced; before/after diff is confined to the target instruction; authoritative source untouched | `REVIEW_REQUIRED`, no state advance |

A response failing any layer is recorded as evidence (`REAL`, with the rejecting layer named) and MUST NOT advance the workflow, MUST NOT be treated as remediation, and MUST NOT contribute to proof condition 3. Layers 2–5 are deterministic Truth Engine logic with no LLM participation, and they hold identically in the fallback architecture where no Agent Gateway exists.

**Planned future security test (design only — not implemented in this correction)**:
`tests/security/test_tool_poisoning.py::test_schema_valid_unauthorized_tool_response_rejected` — a stubbed Artifact Mutation Tool returns a fully schema-valid `RemediationResult` that (a) names an artifact outside `authorized_scope` and (b) cites a `source_version` inconsistent with the workflow. Expected: rejection at layer 3/4, workflow enters `REVIEW_REQUIRED`, target artifact SHA-256 unchanged, no `REMEDIATION_COMPLETED` transition, rejection evidence written to `evidence/security/tool_poisoning_rejected.json`.

### GEAP Availability Gate

Every component below stays `TRACK_ENHANCEMENT` until proven accessible in the actual hackathon account. The core hero workflow MUST NOT depend on successful GEAP provisioning; **Cloud Run + ADK + Firestore + Pub/Sub + Cloud Storage remain the fallback core** and are sufficient for FR-001–FR-011 and SC-001–SC-015. Gate results are recorded in `evidence/geap_access_gate.json` with an explicit PASS/FAIL per component.

| Component | ACCESS_CHECK | SUCCESS CONDITION | FALLBACK |
|---|---|---|---|
| Agent Runtime | Enable `aiplatform.googleapis.com` (+ storage/logging/monitoring/cloudtrace/telemetry/cloudresourcemanager); deploy a trivial ADK agent to Agent Runtime | Agent deploys and responds to an invocation; a `reasoningEngines/...` resource exists | Cloud Run-hosted ADK (`adk deploy cloud_run`) with ADK `ResumabilityConfig` for pause/resume |
| Agent Registry | Enable `agentregistry.googleapis.com`; register one dummy endpoint and read it back | Registry entry created and listable | Local `fixtures/agent_registry.json` manifest, explicitly labelled `SIMULATED` — no claim of platform registry |
| Agent Identity | Confirm the project has an organization parent (`gcloud projects describe $PROJECT_ID --format='value(parent)'`); deploy one agent with agent identity enabled; bind a role to the `principal://...` member | IAM binding with the `principal://` member is accepted and the agent obtains a working token | Dedicated per-agent Cloud Run **service accounts** + in-process deterministic authorization broker; documented in `LIMITATIONS.md` as not Agent Identity |
| Agent Gateway | Enable the 12 documented APIs; `gcloud network-services agent-gateways import`; import the authz extension in `DRY_RUN`, then enforcement | Gateway + authz policy created; ALLOW case succeeds and DENY case is rejected by IAP enforcement with a deny log entry | In-process deterministic authorization broker in the Truth Engine; ALLOW/DENY pair still demonstrated, evidence labelled application-level enforcement |
| Model Armor | Enable `modelarmor.googleapis.com`; create template `driftzero-untrusted-artifact-text`; grant `roles/modelarmor.user` to the Vertex AI service agent; call `generateContent` with `modelArmorConfig` on the adversarial fixture | Injection fixture is blocked with `blockReason: "MODEL_ARMOR"`; the clean fixture passes | Deterministic untrusted-content handling only: content quarantining, no-instruction-following prompt contract, and Truth Engine layers 1–5; `LIMITATIONS.md` records that no external guardrail ran |
| Advanced Agent Observability | After Runtime/Gateway provisioning, confirm agent and gateway telemetry appear in Agent Observability | Traces/spans for agent and gateway interactions are visible and exportable | OpenTelemetry instrumentation from Cloud Run into Cloud Trace + Cloud Logging with correlation IDs (satisfies Constitution VII) |

**Scope note**: these components are infrastructure/governance enhancements. They do not add product scope — spec.md Non-Goals still exclude implementing them as product features, and no FR or SC depends on any of them. If every gate fails, the deliverable is unchanged.

### Downstream Artifact Design

**Decision**: JSON-backed work instruction with human-readable rendering.
**Rationale**: JSON enables deterministic patchability with `jq`-style atomic field replacement. Before/after evidence is the raw JSON diff. Human-readable rendering (HTML/Markdown) is generated for the demo surface. Google Drive/Docs deferred due to OAuth/domain complexity.

### Data Classification Design

Non-exclusive lineage model using `DataClassification(labels=["REAL", "SYNTHETIC"], lineage=[...])`. Each evidence item carries its own classification. Example lineage chains:
- Synthetic SOP fixture → `labels: [SYNTHETIC]`
- Real Gemini call processing synthetic fixture → `labels: [REAL], lineage: [{source: fixture, classification: [SYNTHETIC]}]`
- Derived Gemma observation from real photo of demo box → `labels: [DERIVED, REAL], lineage: [{source: photo, classification: [REAL]}, {scenario: demo, classification: [SYNTHETIC]}]`
- Change Proof → `labels: [DERIVED], lineage: [all contributing evidence refs]`

### Change Proof Technical Design

- **Canonical format**: JSON evidence manifest containing all fields from `ChangeProof` model
- **Integrity**: SHA-256 hash of canonical JSON (sorted keys, no whitespace)
- **Validation**: Deterministic `ProofValidator` checks all 7 completion invariants against the manifest
- **Rendering**: Human-readable HTML generated from canonical JSON for demo/judges (NOT the source of truth)
- **Immutability**: Firestore document marked immutable after creation; Cloud Storage object with generation match
- **Condition 7 implementation**: implemented exactly as written in spec.md (Change Proof Mandatory Completion Conditions, condition 7). The plan carries no independent interpretation of this condition. Deterministic encoding in `proof_generator.py`:
  - `workflow.current_state in {SUPERSEDED, FAILED}` → completion permanently denied (terminal non-success; no later evidence can re-open it).
  - `SUPERSEDED` or `FAILED` present anywhere in the state history → completion permanently denied (both are terminal, so occupancy and history are equivalent).
  - `REVIEW_REQUIRED` present anywhere in the state history → completion denied (no autonomous exit in this scope).
  - `workflow.current_state in {VERIFICATION_FAILED, VERIFICATION_INCONCLUSIVE}` → completion denied while current.
  - Historical `VERIFICATION_FAILED` / `VERIFICATION_INCONCLUSIVE` entries → **not** disqualifying, provided condition 5 holds (latest authoritative verification for the applicable source version/change is `VERIFICATION_PASSED`). Their evidence records are required inputs to condition 6 and are emitted into the proof evidence trail; deleting them fails proof validation.
  - This is what makes `FAIL → corrected evidence → PASS → PROOF_COMPLETE` (US6) executable without weakening any other condition.

### Security Threat Analysis

| Threat | Prevention | Detection | Evidence |
|---|---|---|---|
| Prompt injection in artifact text | Model Armor screening at the `generateContent` call (text only, `INSPECT_AND_BLOCK`); untrusted-content prompt contract; Truth Engine validation chain layers 1–5 regardless of screening | `blockReason: "MODEL_ARMOR"` response; `ScreeningBlocked` / `SCREENING_SKIPPED` evidence | `security/prompt_injection_blocked.json` |
| **Tool poisoning / malicious tool response** (schema-valid, semantically unauthorized) | Five-layer Truth Engine validation chain: schema → provenance/expected-source → expected artifact/tool identity → authorization/scope → deterministic semantic invariants. Schema validation alone is insufficient. Agent Gateway tool-scoped allow policy where available | Rejection record naming the failing layer; artifact hash unchanged; no `REMEDIATION_COMPLETED` transition | `security/tool_poisoning_rejected.json` |
| Unauthorized master mutation | No write tool provided to any agent for source procedure | Tool invocation audit | Agent trace logs |
| Cross-agent privilege escalation | Primary: one Agent Identity per agent, with the mutation capability bound to the Remediation Agent principal only (Enablement Agent explicitly denied). Fallback: dedicated per-agent service accounts + deterministic in-process authorization broker | IAM audit logs; Gateway allow/deny decisions carrying the caller SPIFFE principal | Cloud Audit Logs; `security/gateway_deny_enablement_to_mutation_tool.json` |
| Duplicate event replay | `change_id` idempotency in Truth Engine | Duplicate detection log | State transition log |
| Forged evidence reference | SHA-256 content hash verification in proof validator | Hash mismatch alert | Integrity check report |
| PII leakage | Opaque worker IDs; no real employee data in fixtures | Fixture review | Data classification labels |
| Synthetic-as-real misrepresentation | Explicit data classification with lineage | Classification audit | Evidence manifest |
| Stale version completion | Supersession check on every state transition | SUPERSEDED state log | State transition log |
| Malicious field upload | Gemma output schema validation; deterministic comparator | Invalid observation log | Verification event log |

### Test Strategy

| Group | Tests | Covers |
|---|---|---|
| **Unit: Truth Engine** | State transitions (legal/illegal), 7 proof invariants, idempotency, supersession, verification ordering, autonomy gate (9 conditions), no-op remediation, retry deduplication, data classification lineage, hash integrity | FR-001–FR-011, SC-001–SC-015 |
| **Integration: Agent + Store** | Change event → persisted workflow, Gemini ChangeSet extraction, authorized remediation + GCS evidence, delivery receipt, proof generation + Firestore persistence | FR-001–FR-006 |
| **Agent: Output Validation** | Structured output conformance, hallucinated output rejection, tool permission denial, timeout handling | FR-011 |
| **Multimodal: Gemma** | LEFT fixtures → `LEFT` observation, TOP_RIGHT fixtures → `TOP_RIGHT`, ambiguous fixtures → `INCONCLUSIVE` | SC-006, SC-007, SC-012 |
| **Security** | Prompt injection in artifact **text** → blocked at the `generateContent` screening point (or deterministically quarantined in fallback); tool poisoning: schema-valid but unauthorized tool response → rejected at validation layer 3/4 with no mutation; Agent Gateway ALLOW (Remediation Agent → Artifact Mutation Tool) and DENY (Enablement Agent → Artifact Mutation Tool) | FR-002, FR-003, FR-011; Model Armor path; Gateway path; tool poisoning chain |

### Cost Strategy

**Claim discipline**: this project does not publish a precise predicted project total. A monetary figure appears only where it is derived from a documented unit price **and** an explicitly stated usage assumption, and every such figure is labelled `ESTIMATED`. `ACTUAL COST OBSERVED` values are read from Google Cloud billing after the fact and recorded separately in `evidence/cost_model.json`. The two are never merged into one number, and no cost-savings or efficiency claim is made anywhere in judged material (Constitution Principle II).

**Unit-price drivers** (rates are read from the linked official pricing pages at implementation time and recorded in `evidence/cost_model.json`; see research.md R-015):

| Component | Billing unit | Documented rate at plan time | Relative cost risk | Safeguard |
|---|---|---|---|---|
| Firestore | Document reads / writes / stored GiB | Free-tier dominated at demo volume; rate from pricing page | LOW | Budget alert; demo volume is a handful of documents |
| Pub/Sub | Message throughput (GiB) | Free-tier dominated at demo volume | LOW | Budget alert |
| Cloud Storage | Stored GiB/month + operations | Standard-class storage rate from pricing page | LOW | Object lifecycle policy on evidence bucket |
| Cloud Run (CPU) | CPU-second, GiB-second, request | Rate from Cloud Run pricing page | LOW | `--max-instances=2`; scale-to-zero when idle |
| Cloud Run (GPU, Gemma 4) | GPU-second (NVIDIA L4) + CPU/memory | Rate from Cloud Run pricing page | **HIGH** — the dominant driver | `--max-instances=1`; scale-to-zero; GPU service stopped between sessions; per-session wall-clock cap |
| Gemini 3.5 Flash | Per 1M input tokens / 1M output tokens | Rate to be captured from the Gemini pricing page | MEDIUM | Short prompts, structured output, retry cap of 2 per agent step |
| Model Armor (if gate passes) | Total tokens in prompts + responses | Token-based per official overview | LOW | Only screens artifact text on the Change Intelligence path |
| Veo 3.1 (BONUS) | Output tokens: 5,792 tokens per second of 720p video ≈ **$0.10 per generated second** (Standard) | Documented | MEDIUM | Hard cap: ≤5 development generations, ≤2 demo generations, ≤6s each. Cap-derived: ~$0.60 per 6s clip `ESTIMATED`; ≤$3 development video spend `ESTIMATED` |

**Hard caps and guards (the actual budget control):**
- Development budget cap: **$50 internal budget alert** (with earlier notifications at $25 and $75 configured in Cloud Billing).
- Generation-count caps: Veo ≤5 development + ≤2 demo generations, ≤6 seconds each.
- `--max-instances`: 2 for CPU Cloud Run services, **1** for the Cloud Run GPU (Gemma) service.
- GPU service is deployed with scale-to-zero and is explicitly stopped/undeployed outside working sessions.
- Every generation-bearing call (Veo, GPU inference) is counted in `evidence/cost_model.json` so `ACTUAL COST OBSERVED` can be reconciled against the caps.

**Reconciliation**: after the demo, actual billing figures are exported from Cloud Billing into `evidence/cost_model.json` under `actual_cost_observed`, alongside the `estimated` block. Judged material cites only the observed figures for actual spend.

### Implementation Milestones (Risk-First)

**M0 — Truth Engine Proof** (highest priority, zero cloud dependency)
- Pydantic data models
- State machine with all 13 states and legal transitions
- Idempotency, supersession, verification ordering
- 7 proof completion invariants
- SHA-256 proof generation
- Full unit test suite (local, no cloud)
- **Proves**: Core product logic works without any LLM or cloud service

**M1 — Gemini + ADK Integration**
- Change Intelligence Agent with Gemini 3.5 Flash
- Remediation Agent with scoped tools
- Frontline Enablement Agent
- ADK SequentialAgent orchestrator
- Agent output validation against Truth Engine
- Synthetic fixture end-to-end (local)

**M2 — Real Cloud Async**
- Firestore persistence layer
- Cloud Storage evidence store
- Pub/Sub change event ingestion
- Cloud Run deployment
- Pause/resume with ResumabilityConfig
- Restart recovery test

**M3 — Governed Fleet** (every GEAP item is OPTIONAL / TRACK ENHANCEMENT, gated by § GEAP Availability Gate)
- Run the GEAP access checks first; record results in `evidence/geap_access_gate.json`
- Agent Identity: one per-agent Agent Identity (`change-intel`, `remediation`, `enablement`, `field-verify`) if the gate passes; otherwise dedicated per-agent Cloud Run **service accounts** (documented as fallback, not Agent Identity)
- Agent Registry: register the Artifact Mutation Tool endpoint (prerequisite for the Gateway path)
- Agent Gateway: the single governed path — Remediation Agent → Gateway → Artifact Mutation Tool ALLOW, Frontline Enablement Agent → DENIED (`DRY_RUN` first, then enforcement)
- Model Armor: `driftzero-untrusted-artifact-text` template applied at the Change Intelligence `generateContent` call; adversarial text fixture
- Tool Response Validation Chain (layers 1–5) in the Truth Engine — **not gated**, required in the fallback architecture too
- Observability: OpenTelemetry traces, correlation IDs (Cloud Trace/Logging baseline; Agent Observability only if the gate passes)
- Security test suite: prompt injection, tool poisoning, Gateway ALLOW/DENY

**M4 — Physical Gemma Verification**
- Gemma 4 12B deployment (Cloud Run GPU)
- Field observation structured output contract
- Multimodal fixture evaluation (LEFT/TOP_RIGHT/INCONCLUSIVE)
- Real camera capture test with physical demo box
- Deterministic comparator integration

**M5 — Veo Enhancement** (BONUS)
- Veo 3.1 microtraining video generation
- Non-blocking integration (text delta fallback)
- Generated asset stored in evidence pack

**M6 — Demo Surface & Evidence Polish**
- FastAPI web interface
- Mobile-responsive field verification upload
- Workflow state visualization
- Change Proof display
- JUDGES_START_HERE.md
- Evidence pack assembly
- LIMITATIONS.md

### Deferred Scope

- Lyria 3 audio generation
- Memory Bank integration
- Reviewer assignment/UI/SLA for REVIEW_REQUIRED
- Multi-worker/site support
- Generic SOP management
- Analytics dashboard
- Authentication system
- Post-completion drift detection
- Production enterprise credentials
- Mobile app polish

### Risk Register

| Risk | Severity | Mitigation |
|---|---|---|
| Agent Runtime access unavailable | HIGH | Fall back to Cloud Run-based ADK deployment |
| Gemma 4 vision accuracy insufficient for demo | HIGH | Pre-validate with fixture set; use high-contrast labels; fallback to manual observation input |
| GPU quota unavailable in target region | MEDIUM | Try multiple regions; fallback to Vertex AI Model Garden endpoint |
| Veo 3.1 generation too slow for live demo | MEDIUM | Pre-generate and cache; fallback to text-only delta delivery |
| Agent Registry/Gateway/Identity not accessible in hackathon account | MEDIUM | GEAP Availability Gate with per-component ACCESS_CHECK/SUCCESS/FALLBACK; core workflow has zero GEAP dependency |
| Agent Identity requires an organization-scoped trust domain (`org-ORGANIZATION_ID`) that a personal hackathon project may lack | MEDIUM | Access check verifies the project's organization parent early (M3 start); fallback to dedicated per-agent service accounts, documented honestly in `LIMITATIONS.md` |
| Tool poisoning: schema-valid but unauthorized tool response | HIGH | Five-layer Truth Engine validation chain (provenance, artifact/tool identity, authorization/scope, semantic invariants); dedicated security test; independent of Gateway availability |
| Single developer time constraint | HIGH | Risk-first milestones; M0-M2 are the minimum viable demo |
| Cloud credit exhaustion | MEDIUM | $50 internal budget alert (plus $25/$75 notifications); GPU `max-instances=1` + scale-to-zero + stop between sessions; Veo generation caps; reconcile `ACTUAL COST OBSERVED` in `evidence/cost_model.json` |
| Model Armor fail-open during a service outage (documented behavior: sanitization step is skipped) | MEDIUM | Record `SCREENING_SKIPPED` in the evidence manifest so no screening is claimed that did not occur; Truth Engine validation chain still applies |
| Model Armor text-only limitation (documents unsupported, image screening Preview) | LOW | Screen artifact text only; field images covered by hashing + deterministic comparator; limitation stated in `LIMITATIONS.md` |

## Complexity Tracking

No constitution violations require justification. All architecture decisions align with the 14 principles.
