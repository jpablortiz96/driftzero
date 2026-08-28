# Tasks: Hero Change Deployment

**Input**: Design documents from `/specs/001-hero-change-deployment/`
**Prerequisites**: spec.md, plan.md, research.md, data-model.md, contracts/agents.md, quickstart.md — all current. Pre-implementation quality gate `checklists/preimplementation.md` is **approved** (CHK001–CHK039).

## Format: `[ID] [P?] [Milestone] Description`

- **[P]**: Can run in parallel — different files, no unmet dependency
- **[Milestone]**: Binding risk-first phase from plan.md § Implementation Milestones
- **[MANUAL]**: Requires human, cloud console, or account action — not code
- **[OPTIONAL]**: TRACK_ENHANCEMENT / BONUS — MUST NOT block core FR/SC acceptance
- Every task names exact file paths

## Binding Ordering Rules (plan.md § Task Generation Ordering Rules)

1. M0 tasks precede all dependent implementation tasks.
2. G1 Gemma feasibility is scheduled EARLY — before any optional infrastructure work.
3. M1 depends on the relevant M0 truth contracts.
4. M2 depends on stable M0/M1 boundaries.
5. M3 may proceed after M2 and MUST NOT depend on M4.
6. M4 is OPTIONAL and cannot block M3 or core acceptance.
7. M5 is OPTIONAL and cannot block core acceptance.
8. M6 polish may begin only after core hero functionality is stable.
9. Frontend polish may never be P0 ahead of M0–M3 core proof.
10. No task belonging solely to an optional enhancement may become a prerequisite for FR-001–FR-011 acceptance.
11. **Protocol rule**: no Agent Gateway policy-implementation task may begin before T113 records the tool-protocol decision.
12. **Identity rule**: no Agent Identity implementation task may begin before T110 completes the organization/access check.

## Deployment Topology (authoritative — do not inflate)

| Cloud Run service | Contents | Runtime service account |
|---|---|---|
| `driftzero-api` | FastAPI + Truth Engine + ADK orchestrator + **all four agents in one process** | `driftzero-run-sa@…` |
| `gemma-verification` | Gemma 4 GPU inference — **only if G1 selects that route** | `driftzero-gemma-sa@…` |

Four Cloud Run services MUST NOT be created to simulate per-agent identity. Per-agent separation in the fallback is application-level (logical agent context + in-process authorization broker).

**Package note**: `models/artifact.py`, `models/action.py`, `models/remediation.py`, `models/delivery.py`, and `truth_engine/impact.py`, `truth_engine/divergence.py`, `truth_engine/actions.py`, `truth_engine/validation.py` extend the illustrative package tree in plan.md § Project Structure; all other paths match it exactly.

---

## Phase 0: Setup (zero cloud, zero LLM)

**Purpose**: Repository scaffolding required before M0 logic. No cloud account needed.

- [x] T001 Create Python package scaffolding: `pyproject.toml` (Python 3.11+, pydantic>=2.0, pytest, pytest-asyncio), `src/driftzero/__init__.py`, `src/driftzero/models/__init__.py`, `src/driftzero/truth_engine/__init__.py`, `tests/unit/truth_engine/__init__.py`
- [x] T002 [P] Create `.env.example` with placeholder keys only (`GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_REGION`, `FIRESTORE_DATABASE`, `GCS_EVIDENCE_BUCKET`, `PUBSUB_TOPIC`, `GEMINI_MODEL`, `GEMMA_ENDPOINT`) and `.gitignore` ignoring `.env`, per Constitution § Secret Hygiene
- [x] T003 [P] Configure `pytest.ini`/`pyproject.toml` test settings and `ruff.toml` lint config; add `make test` equivalent in `Makefile`
- [x] T004 [P] Create synthetic business fixtures in `fixtures/`: `hero_change.json` (LEFT→TOP_RIGHT), `stale_artifact.json`, `unrelated_artifact.json` (lexical `LEFT` match, different operation), `already_compliant_artifact.json`, `multi_candidate_artifacts.json`, `divergent_artifact.json` (extra `box_size` divergence) — every file labelled `SYNTHETIC`
- [x] T005 [P] Create approved source procedure fixtures `fixtures/source_procedure_v1.json` and `fixtures/source_procedure_v2.json` (structured requirement sets for the divergence comparator and supersession tests)

**Checkpoint**: Repository builds and `pytest` runs with zero tests collected.

---

## Phase 1: M0 — Deterministic Truth Engine 🎯 PRIORITY ZERO

**Goal**: Prove the entire product logic with **zero Google Cloud dependency and zero LLM dependency**.
**Independent Test**: `pytest tests/unit/truth_engine/ -v` passes offline, network disabled.
**Blocks**: All of M1, M2, M3, M6. Nothing in this phase may import a cloud SDK or call a model.

### M0.1 — Typed domain models

- [x] T006 [P] [M0] Implement `DataClassification` (non-exclusive `labels`) and `LineageEntry` (`source_ref`, `source_classification`, `relationship`) in `src/driftzero/models/classification.py` — FR-010
- [x] T007 [P] [M0] Implement `DownstreamArtifact` (incl. `authorized_for_remediation`, `operation_id`, `requirement_id`, `current_value`, `content_ref`) in `src/driftzero/models/artifact.py` — FR-002
- [x] T008 [P] [M0] Implement `ApprovedChange` (all data-model fields incl. `authorized_scope`, `previous_value`, `current_value`, `source_version`, `previous_version`) in `src/driftzero/models/change.py` — FR-001
- [x] T009 [M0] Implement `ChangeSet` and `AffectedArtifactCandidate` (four condition booleans + `is_affected` as a **proposal** field) in `src/driftzero/models/change.py` (depends T008) — FR-002
- [x] T010 [P] [M0] Implement `WorkflowState` enum with all **13 canonical states** plus a category map (`PROGRESSIVE` / `BLOCKING_RECOVERABLE` / `BLOCKING_GATE` / `TERMINAL_SUCCESS` / `TERMINAL_NON_SUCCESS`) in `src/driftzero/models/workflow.py` — spec § State Requirements
- [x] T011 [P] [M0] Implement `MutationEvidence` (`before_ref`, `after_ref`, `before_hash`, `after_hash`, `before_value`, `after_value`, `patch_description`, `reconciled`, `action_id`) in `src/driftzero/models/remediation.py` — FR-003
- [x] T012 [M0] Implement `NoOpEvidence` (`evaluated_artifact_ref`, `evaluated_artifact_hash`, `observed_value`, `expected_value`, `no_op_reason`, `compliance_basis`) and the `RemediationEvidence` discriminated union on `remediation_type` in `src/driftzero/models/remediation.py`; reject `before_ref`/`after_ref` on the NO_OP variant (depends T011) — FR-003, US3-2
- [x] T013 [P] [M0] Implement `ObservedPosition` enum (`LEFT` | `TOP_RIGHT` | `INCONCLUSIVE`, closed) and `FieldObservation` (`submission_id`, `raw_evidence_ref`, `observed_label_position`, `confidence_note` non-authoritative) in `src/driftzero/models/verification.py` — FR-005
- [x] T014 [M0] Implement `VerificationEvent` (`event_id`, `submission_id`, `event_sequence`, `raw_evidence_ref`, `derived_observation`, `expected_value`, `verification_result`) in `src/driftzero/models/verification.py` (depends T013) — FR-005
- [x] T015 [P] [M0] Implement `DeliveryResult` (`worker_id`, `delivery_mechanism`, `delta_content`, `delivered`, `delivery_evidence_ref`, optional `training_video_ref`) in `src/driftzero/models/delivery.py` — FR-004
- [x] T016 [P] [M0] Implement `ActionExecution`, `ActionType` (`REMEDIATE_ARTIFACT`, `DELIVER_DELTA`, `PROCESS_FIELD_EVIDENCE`, `GENERATE_PROOF`) and `ActionStatus` (`PLANNED`, `ATTEMPTED`, `COMPLETED`, `FAILED_OR_UNCERTAIN`) in `src/driftzero/models/action.py` — FR-007, FR-008
- [x] T017 [M0] Implement `Workflow` aggregate incl. `affected_artifact_id`, `candidate_artifact_refs`, `remediation_evidence`, `verification_events`, `latest_verification_status`, `event_sequence` in `src/driftzero/models/workflow.py` (depends T007, T010, T012, T014) — FR-008
- [x] T018 [P] [M0] Implement `EvidenceManifest` (`remediation_evidence_refs`, `rejected_result_refs`, `verification_refs`, `state_transition_refs`, `content_hashes`) in `src/driftzero/models/proof.py` — FR-006
- [x] T019 [M0] Implement `ChangeProof` with `remediation_evidence` union field and `content_hash` in `src/driftzero/models/proof.py` (depends T012, T018) — FR-006

### M0.2 — State machine

- [x] T020 [M0] Implement the legal transition matrix for all 13 states in `src/driftzero/truth_engine/state_machine.py` exactly per data-model.md § State Transitions (depends T010)
- [x] T021 [M0] Implement illegal-transition handling in `src/driftzero/truth_engine/state_machine.py`: raise a typed `IllegalTransitionError`, never silently coerce, never advance state (depends T020)
- [x] T022 [M0] Implement the `REVIEW_REQUIRED` exit set in `src/driftzero/truth_engine/state_machine.py`: legal only `→ SUPERSEDED` and `→ FAILED`; the six progressive transitions are rejected as illegal in S1 (depends T020) — CHK009 ruling
- [x] T023 [M0] Implement no-exit enforcement for `PROOF_COMPLETE`, `SUPERSEDED`, `FAILED` in `src/driftzero/truth_engine/state_machine.py` (depends T020) — FR-006
- [x] T024 [M0] Implement source-version applicability and automatic `SUPERSEDED` transition in `src/driftzero/truth_engine/supersession.py` (depends T020) — FR-009, SC-015

### M0.3 — Impact, divergence, autonomy

- [x] T025 [M0] Implement deterministic affected-artifact qualification (operation match, instruction correspondence, conflicting value, authorized scope) in `src/driftzero/truth_engine/impact.py`; agent `is_affected` is input-only and never decisive (depends T009, T007) — FR-002, SC-002
- [x] T026 [M0] Implement cardinality resolution in `src/driftzero/truth_engine/impact.py`: zero qualified → `REVIEW_REQUIRED` with `candidate_artifact_refs` retained and `affected_artifact_id` unset; exactly one → persist and proceed; more than one → `REVIEW_REQUIRED` with full candidate set, no arbitrary selection (depends T025) — spec § Affected Artifact Cardinality
- [x] T027 [M0] Implement the conflicting/additional-divergence comparator in `src/driftzero/truth_engine/divergence.py`: target requirement must equal `previous_value` (artifact) and `current_value` (source); **every other requirement in the same operational scope must already match the approved source**; flag duplicate target representations, out-of-domain target values, and contradictory scope values (depends T007, T008) — spec § Autonomy Boundaries condition 8
- [x] T028 [M0] Implement the 9-condition autonomy gate in `src/driftzero/truth_engine/autonomy_gate.py`, delegating condition 8 to T027; any unmet condition → `REVIEW_REQUIRED` (depends T026, T027) — FR-003

### M0.4 — Identity, idempotency, reconciliation

- [x] T029 [M0] Implement `change_id` duplicate-event detection in `src/driftzero/truth_engine/idempotency.py` — FR-007, SC-010
- [x] T030 [M0] Implement stable `action_id` derivation from (`workflow_id`, `action_type`, applicable `source_version`/`change_id`, target identity) in `src/driftzero/truth_engine/idempotency.py`; recomputation must be stable (depends T016)
- [x] T031 [M0] Implement `submission_id` handling for field evidence in `src/driftzero/truth_engine/idempotency.py`: same ID re-delivered resolves to the existing `VerificationEvent` with **no** new `event_sequence` and no duplicated evidence; a different ID is a new attempt (depends T014, T030) — FR-007
- [x] T032 [M0] Implement the `ActionExecution` ledger (create `PLANNED`, mark `ATTEMPTED`/`COMPLETED`/`FAILED_OR_UNCERTAIN`, one record per `action_id`) in `src/driftzero/truth_engine/actions.py` (depends T016, T030)
- [x] T033 [M0] Implement retry deduplication in `src/driftzero/truth_engine/actions.py`: completed logical actions are never re-executed (depends T032) — FR-008, SC-011
- [x] T034 [M0] Implement crash reconciliation for `REMEDIATE_ARTIFACT` in `src/driftzero/truth_engine/actions.py`: reconcile as completed only when all four conditions hold (not recorded complete · target already exactly in intended after-state · stored pre-action intent proves this workflow planned it · authorization and source-version invariants still hold); reconstruct evidence as `MutationEvidence` with `reconciled=true` — **never** `NoOpEvidence` (depends T032, T012) — FR-008
- [x] T035 [M0] Implement fail-closed behavior in `src/driftzero/truth_engine/actions.py` when reconciliation cannot safely establish the outcome → `REVIEW_REQUIRED`, no fabricated evidence (depends T034) — FR-011
- [x] T036 [M0] Implement delivery reconciliation for `DELIVER_DELTA` in `src/driftzero/truth_engine/actions.py`: reconcile via mechanism receipt/idempotency key; absent a resolvable receipt the action stays `FAILED_OR_UNCERTAIN` and `DELIVERED` is not recorded (depends T032, T015) — FR-004

### M0.5 — Verification

- [x] T037 [M0] Implement verification chronology in `src/driftzero/truth_engine/verification.py`: monotonic server-assigned `event_sequence`, "latest authoritative verification" selection, older-event-cannot-override rule (depends T014, T031) — spec § Edge Cases
- [x] T038 [M0] Implement the deterministic expected-vs-observed comparator in `src/driftzero/truth_engine/verification.py`: `observed == expected → PASS`; `observed != expected and != INCONCLUSIVE → FAIL`; else `INCONCLUSIVE`. Out-of-enum observations are rejected, never coerced (depends T013, T037) — FR-005, SC-006, SC-007, SC-012

### M0.6 — Evidence, classification, proof

- [x] T039 [P] [M0] Implement SHA-256 content hashing and canonical-JSON serialization helpers (sorted keys, no whitespace) in `src/driftzero/truth_engine/evidence.py` — FR-006
- [x] T040 [M0] Implement data classification and lineage assembly in `src/driftzero/truth_engine/evidence.py` so every evidence item carries labels plus an ordered lineage chain (depends T006, T039) — FR-010, SC-013
- [x] T041 [M0] Implement `EvidenceManifest` assembly in `src/driftzero/truth_engine/evidence.py` including **all** verification attempts (FAIL/INCONCLUSIVE included) and `rejected_result_refs` (depends T018, T040) — FR-006 condition 6
- [x] T042 [M0] Implement the deterministic trust-boundary validation layers for Crossings 1–5 in `src/driftzero/truth_engine/validation.py` (schema · provenance · expected source/artifact/tool identity · authorization scope · source-version applicability · semantic invariants), with rejection recorded to `rejected_result_refs` and no state advance (depends T025, T030, T041) — FR-011
- [x] T043 [M0] Implement the seven `PROOF_COMPLETE` invariants in `src/driftzero/truth_engine/proof_generator.py`, encoding condition 7 exactly per plan.md § Change Proof Technical Design (terminal/blocking/current-state rules; historical FAIL/INCONCLUSIVE non-disqualifying) (depends T017, T038, T041) — FR-006, SC-008, SC-009
- [x] T044 [M0] Implement canonical deterministic Change Proof generation with `content_hash` in `src/driftzero/truth_engine/proof_generator.py`; both `MutationEvidence` and `NoOpEvidence` satisfy condition 3 independently (depends T019, T043) — FR-006, SC-003
- [x] T045 [M0] Implement SHA-256 integrity validation in `ProofValidator` (`src/driftzero/truth_engine/proof_generator.py`): re-check all 7 invariants and every manifest hash; add the docstring stating hashes give content identity/alteration detection only — no signature, timestamp, attestation, or ledger property (depends T044) — CHK015 ruling
- [x] T046 [M0] Implement proof singularity/idempotency in `src/driftzero/truth_engine/proof_generator.py`: repeated `GENERATE_PROOF` resolves to one canonical `proof_id` per workflow (depends T044, T030) — FR-007

### M0.7 — Deterministic test suite (offline)

- [x] T047 [P] [M0] State machine tests in `tests/unit/truth_engine/test_state_machine.py`: all legal transitions, illegal-transition rejection, `REVIEW_REQUIRED` exits limited to `SUPERSEDED`/`FAILED`, no exit from terminal states
- [x] T048 [P] [M0] Supersession tests in `tests/unit/truth_engine/test_supersession.py`: v2 arrival supersedes incomplete v1, superseded workflow can never reach `PROOF_COMPLETE` — SC-015
- [x] T049 [P] [M0] Impact + cardinality tests in `tests/unit/truth_engine/test_impact.py`: authorized stale artifact qualifies, lexical-match artifact does not, zero/one/many outcomes behave per spec — SC-002
- [x] T050 [P] [M0] Divergence comparator tests in `tests/unit/truth_engine/test_divergence.py`: allowed single-target case passes; `box_size` divergence blocks; duplicate target representation blocks; out-of-domain target value blocks
- [x] T051 [P] [M0] Autonomy gate tests in `tests/unit/truth_engine/test_autonomy_gate.py`: each of the 9 conditions independently forces `REVIEW_REQUIRED` — FR-003
- [x] T052 [P] [M0] Idempotency tests in `tests/unit/truth_engine/test_idempotency.py`: duplicate change event, stable `action_id` recomputation, duplicate `submission_id` absorbed without new `event_sequence`, new `submission_id` creates a distinct attempt — SC-010
- [x] T053 [P] [M0] Reconciliation tests in `tests/unit/truth_engine/test_action_idempotency.py`: crash-after-successful-mutation reconciles to `MutationEvidence(reconciled=true)`; NO_OP is never substituted; unsafe reconciliation fails closed to `REVIEW_REQUIRED`; delivery without receipt stays uncertain; proof singularity holds — FR-007, FR-008, SC-011
- [x] T054 [P] [M0] Verification tests in `tests/unit/truth_engine/test_verification.py`: LEFT→FAIL, TOP_RIGHT→PASS, ambiguous→INCONCLUSIVE, late older event cannot override newer, FAIL→corrected PASS retains both attempts — SC-006, SC-007, SC-012
- [x] T055 [P] [M0] No-op path tests in `tests/unit/truth_engine/test_no_op.py`: already-compliant artifact yields `NoOpEvidence` with a single evaluated state, no `before_ref`/`after_ref`, no write, and satisfies completion condition 3(b) — SC-003
- [x] T056 [P] [M0] Data classification and lineage tests in `tests/unit/truth_engine/test_data_lineage.py` covering synthetic fixture, real-call-over-synthetic, derived observation, `SIMULATED` emulated dependency, and Change Proof lineage; emit `evidence/reports/data_lineage.json` — FR-010, SC-013 (quickstart VS-12)
- [x] T057 [P] [M0] Proof tests in `tests/unit/truth_engine/test_proof.py`: all 7 invariants individually block completion; `SUPERSEDED`/`FAILED`/`REVIEW_REQUIRED` block permanently; historical FAIL with later PASS completes; canonical hash stable; manifest hash mismatch fails validation — SC-008, SC-009
- [x] T058 [P] [M0] Trust-boundary rejection tests in `tests/unit/truth_engine/test_validation.py`: schema-valid but unauthorized artifact, inconsistent `source_version`, `before_hash` mismatch, unearned `delivered:true`, out-of-enum observation, observation carrying PASS/FAIL — FR-011
- [x] T059 [M0] FR/SC traceability coverage test in `tests/unit/truth_engine/test_traceability.py` asserting every FR-001–FR-011 and SC-001–SC-015 maps to at least one executed deterministic test or a documented later-milestone scenario, per plan.md § Requirement Traceability Matrix

### M0.8 — HARD EXIT GATE

- [x] T060 [M0] **M0 EXIT GATE** — run `pytest tests/unit/truth_engine/ -v` with **network disabled** and assert: all deterministic tests pass; **zero Google Cloud dependency** (no `google-cloud-*` / `google.adk` import reachable from `src/driftzero/truth_engine/` or `src/driftzero/models/`, enforced by an import-guard test in `tests/unit/truth_engine/test_no_cloud_imports.py`); **zero LLM dependency** (no model client import or call). Record the run to `evidence/reports/m0_gate.xml`. **No M1 core work is considered complete before this gate passes.**

**Checkpoint**: The entire product truth is proven offline. M1 may begin.

---

## Phase 2: G1 — Gemma Feasibility Risk Spike (EARLY, non-blocking)

**Goal**: Retire physical-verification risk with a recorded GO/FALLBACK decision **before** any optional infrastructure investment.
**Runs in parallel with**: M0/M1 (independent of both).
**MUST precede**: all M4 optional GEAP work.

- [x] T061 [MANUAL] [G1] Verify Gemma 4 model access and accept the licence for the project; record the outcome in `evidence/g1_gemma_feasibility.json` (quickstart MS-16)
- [x] T062 [MANUAL] [G1] Evaluate serving routes — Vertex AI Model Garden endpoint vs Cloud Run + vLLM — and record the selected route with its rationale in `evidence/g1_gemma_feasibility.json`. Variant/quantization choice is decided **here**, not assumed (research R-008)
- [x] T063 [MANUAL] [G1] Verify the serving-capacity prerequisite for the route selected by G1. The active route is **Vertex AI MaaS** (`google/gemma-4-26b-a4b-it-maas`, serverless ON_DEMAND), which requires **no GPU quota grant**; capacity is evidenced by successful qualifying inference. Record status in `evidence/g1_gemma_feasibility.json`. **Supersession chain**: *Cloud Run + NVIDIA L4* → *Vertex Model Garden + NVIDIA RTX PRO 6000* (quota preference **denied**, grantedValue 0 — operator-reported) → *Vertex AI MaaS*. GPU quota applied only to the superseded self-deploy route; see `evidence/g1_platform_session.json` § `quota_findings`
- [x] T064 [MANUAL] [G1] Prepare the physical demo fixture (real box, printed high-contrast label) and capture `fixtures/multimodal/label_left_01.jpg`, `label_top_right_01.jpg`, `label_ambiguous_01.jpg` (quickstart MS-18)
- [x] T065 [G1] Implement the feasibility probe harness `scripts/g1_gemma_probe.py` that sends each fixture to the selected serving route and records the raw structured response — probe only, not the production adapter
- [x] T066 [G1] Run the distinguishability evaluation and write per-fixture results (expected vs observed) to `evidence/g1_gemma_feasibility.json` (depends T063, T064, T065) — executed via the MaaS route; per-fixture results in `evidence/g1_maas_inference_run.json`
- [x] T067 [G1] **G1 EXIT GATE** — record an explicit **GO** or **FALLBACK** decision with the justifying fixture results in `evidence/g1_gemma_feasibility.json` (depends T066) — **GO** recorded: 9 qualifying MaaS inferences (3 per fixture), all matching expected, all stable across measured repeats; see `evidence/g1_maas_inference_run.json`
- [ ] T068 [P] [G1] Implement the deterministic/manual observation fallback adapter in `src/driftzero/agents/manual_observation.py` accepting an operator-supplied normalized observation — required only if T067 returns FALLBACK; keeps FR-005 satisfiable without Gemma — **NOT TRIGGERED**: T067 returned GO, so the fallback adapter is not required

**Checkpoint**: Physical-verification risk is retired with evidence. Optional GEAP work is now unblocked (but still optional).

---

## Phase 3: M1 — Gemini + ADK Semantic Workflow

**Goal**: synthetic approved change → structured `ChangeSet` → validated impact → authorized remediation request → delta composition, with **every** agent output passing deterministic trust-boundary validation.
**Depends on**: M0 exit gate (T060).

- [x] T069 [M1] Create the ADK agent package and shared config in `src/driftzero/agents/__init__.py` and `src/driftzero/config.py` (model id, timeouts, retry caps — values from plan.md § Retry & Timeout Engineering Policy, all configurable)
- [x] T070 [M1] Implement the retry/timeout policy module in `src/driftzero/retry.py`: semantic calls 1 + max 2 retries at 60 s default; side-effect calls 30 s with post-dispatch timeout classified **UNKNOWN** (never auto-retry); Gemma 60 s; transient-only retry classification (depends T069)
- [x] T071 [M1] Implement the Change Intelligence Agent (`gemini-3.5-flash`, structured `ChangeSet` output) in `src/driftzero/agents/change_intel.py` with read-only tools `read_approved_change` and `read_artifact_registry` (depends T069)
- [x] T072 [M1] Wire Crossing 1 validation for `ChangeSet` into the orchestration boundary in `src/driftzero/truth_engine/validation.py` call sites; agent `is_affected` remains a proposal (depends T042, T071)
- [x] T073 [M1] Implement the Artifact Mutation Tool as a local in-process authenticated capability in `src/driftzero/tools/artifact_mutation.py` with signature `apply_authorized_artifact_patch(action_id, artifact_id, requirement_id, expected_before_value, expected_before_hash, new_value, source_procedure_id, source_version, change_id, correlation_id)`, idempotent on `action_id` (depends T032, T034)
- [x] T074 [M1] Implement the Remediation Agent in `src/driftzero/agents/remediation.py` — the only logical identity permitted to invoke T073 (depends T073)
- [x] T075 [M1] Implement the generalized in-process authorization authority in `src/driftzero/capabilities.py` keyed on logical agent identity **and target tool**; Enablement, Change Intelligence, Field Verification and the orchestrator are denied the mutation capability; denials are recorded as deterministic evidence (depends T074). **Placement superseded**: originally targeted `src/driftzero/truth_engine/authz_broker.py`; retargeted because the broker must handle `MutationCapability` (an M1 concept), and hosting it in `truth_engine/` would invert the one-way M1 → M0 dependency direction. **Scope split**: T074 already delivered capability issuance, HMAC integrity, registry and verification mechanics; T075 adds only (a) generalized agent→tool policy, (b) explicit tool binding of capabilities, (c) denial recording, (d) denial evidence. Exactly **one** authorization authority exists; `MUTATION_AUTHORIZED_IDENTITIES` survives only as a derived view
- [x] T076 [M1] Wire Crossing 2 validation for `RemediationEvidence` incl. before-hash consistency and discriminated MUTATION/NO_OP adjudication (depends T042, T074)
- [x] T077 [M1] Implement the Frontline Enablement Agent and delta composition in `src/driftzero/agents/enablement.py` (depends T069)
- [x] T078 [M1] Implement the local delivery mechanism with a resolvable receipt in `src/driftzero/delivery/local_channel.py` and wire Crossing 3 validation; an agent asserting `delivered:true` without a receipt must not yield `DELIVERED` (depends T036, T077)
- [x] T079 [M1] Implement the Field Verification Agent wrapper in `src/driftzero/agents/field_verify.py` returning `FieldObservation` only (no PASS/FAIL), with Crossing 4 validation; backed by T068 fallback or the G1-selected route (depends T038, T067)
- [x] T080 [M1] Implement the ADK `SequentialAgent` orchestrator with the 11-step boundary sequence and the async pause after delivery in `src/driftzero/agents/orchestrator.py`; the Truth Engine validates pre/post conditions at every step (depends T072, T076, T078, T079)
- [x] T081 [M1] Implement the CLI entry points `inject-change`, `status`, `verify`, `proof --validate` in `src/driftzero/cli.py` (depends T080)
- [x] T082 [M1] Local end-to-end test in `tests/integration/test_local_hero_flow.py`: synthetic change → ChangeSet → impact → remediation → delivery → FAIL evidence → corrected PASS → `PROOF_COMPLETE`, with the Truth Engine authoritative at every crossing (depends T081)
- [x] T083 [M1] Agent output validation tests in `tests/integration/test_agent_output_validation.py`: hallucinated/malformed structured output, retry exhaustion → `REVIEW_REQUIRED`, tool permission denial for the Enablement Agent (depends T075, T082)
- [x] T084 [M1] **M1 EXIT GATE** — local end-to-end semantic workflow passes with the Truth Engine authoritative at every crossing; record to `evidence/runs/hero_run_local/` (depends T082, T083)

**Checkpoint**: The hero workflow runs locally with real Gemini calls over synthetic fixtures.

---

## Phase 4: M2 — Real Cloud Event + Durable State

**Goal**: real Pub/Sub event → Cloud Run → Firestore authoritative workflow → Cloud Storage evidence → restart/resume → zero duplicate logical actions.
**Depends on**: stable M0/M1 boundaries (T060, T084).

- [ ] T085 [MANUAL] [M2] Create/select the GCP project, link billing, redeem hackathon credits, and configure budget alerts at **$50** internal plus $25/$75 notifications (quickstart MS-1–MS-4)
- [x] T086 [MANUAL] [M2] Enable the 11 core APIs listed in quickstart MS-5 and verify with `gcloud services list --enabled`
- [x] T087 [MANUAL] [M2] Configure ADC (`gcloud auth application-default login`) and record the single selected region in `.env` (quickstart MS-6, MS-7)
- [x] T088 [MANUAL] [M2] Create the local `.env` from `.env.example`; verify `git check-ignore -v .env` and that no secret is committed (quickstart MS-8)
- [x] T089 [MANUAL] [M2] Create the Firestore database, the `driftzero-approved-changes` Pub/Sub topic with a push subscription, and the `driftzero-evidence-$PROJECT_ID` GCS bucket with lifecycle policy (quickstart MS-9–MS-11)
- [x] T090 [MANUAL] [M2] Record the secret-handling decision (local `.env` vs Secret Manager) and apply it per quickstart MS-12
- [x] T091 [MANUAL] [M2] Create the CORE runtime service accounts `driftzero-run-sa` and `driftzero-gemma-sa` with only the scoped roles listed in quickstart MS-12b; verify with `gcloud projects get-iam-policy`. **This is not Agent Identity and not per-agent IAM identity** (quickstart MS-12b)
- [x] T092 [P] [M2] Implement the Firestore persistence adapter (workflows, action ledger, proofs, idempotency keys) in `src/driftzero_cloud/firestore.py` (depends T017, T032)
- [x] T093 [P] [M2] Implement the Cloud Storage evidence adapter (raw evidence, before/after artifacts, rendered proofs) in `src/driftzero_cloud/gcs.py` (depends T039)
- [x] T094 [M2] Implement the FastAPI routes from contracts/agents.md § API Contract in `src/driftzero_api/routes.py`: `POST /api/v1/changes`, `GET /workflows/{id}`, `POST /workflows/{id}/verify` (multipart, carries `submission_id`), `GET /workflows/{id}/proof`, `GET /workflows/{id}/evidence` (depends T081, T092)
- [x] T095 [M2] Implement the Pub/Sub push handler for approved change ingestion in `src/driftzero_api/pubsub.py` with `change_id` idempotency at the boundary (depends T029, T094)
- [x] T096 [M2] Create the `Dockerfile` and deployment configuration for the single `driftzero-api` Cloud Run service — `--service-account=driftzero-run-sa@…`, `--max-instances=2 --min-instances=0` (quickstart MS-13) (depends T094)
- [ ] T097 [M2] Implement pause/resume with ADK `ResumabilityConfig` backed by Firestore as the authoritative store in `src/driftzero/agents/orchestrator.py`; evidence arrival triggers a distinct invocation, not an in-process block (depends T092, T095)
- [ ] T098 [M2] Implement observability in `src/driftzero/observability.py`: OpenTelemetry export to Cloud Trace, structured Cloud Logging, correlation IDs bound to `workflow_id` and `action_id`, retry/timeout attempts observable (quickstart MS-17) (depends T070, T096)
- [ ] T099 [M2] Restart/recovery integration test in `tests/integration/test_restart_recovery.py`: kill the process mid-step, resume, assert zero duplicate logical actions and correct reconciliation classification (depends T097)
- [ ] T100 [M2] Duplicate-event and duplicate-evidence integration test in `tests/integration/test_cloud_idempotency.py` against real Firestore/Pub/Sub (depends T095, T099)
- [ ] T101 [M2] **M2 EXIT GATE** — restart/recovery and duplicate-event evidence recorded from **real Google Cloud execution** into `evidence/runs/hero_run_001/restart_recovery.json` and `idempotency_log.json` (depends T099, T100)

**Checkpoint**: The workflow survives real cloud execution and process death. Core is stable.

---

## Phase 5: M3 — Physical Gemma Verification

**Goal**: real physical image → Gemma-derived normalized observation → deterministic comparator → FAIL / INCONCLUSIVE / PASS.
**Depends on**: M2 (T101) and the G1 decision (T067). **MUST NOT depend on M4.**
**Conditional**: run only if T067 returned **GO**; otherwise the T068 fallback remains in place and this phase is documented as deferred.

- [ ] T102 [M3] Provision/deploy the Gemma verification serving route **selected by G1**, using the verified configuration recorded by T062/T063 — never a route hardcoded here. For the currently selected route this is Vertex AI Model Garden, `google/gemma4@gemma-4-12b-it`, `g4-standard-48`, `NVIDIA_RTX_PRO_6000` ×1, `us-central1`. **Do not provision until T063 is satisfied and billing/cost authorization is explicitly restored.** (quickstart MS-15) (depends T063, T067, T091)
- [ ] T103 [M3] Implement the production Gemma inference adapter with strict structured-observation output in `src/driftzero/agents/gemma_client.py`, 60 s configurable timeout, out-of-enum values rejected (depends T079, T102)
- [ ] T104 [M3] Build the versioned multimodal evaluation fixture set under `fixtures/multimodal/` with a `manifest.json` recording expected observations per image (depends T064)
- [ ] T105 [M3] Multimodal evaluation run in `tests/multimodal/test_gemma_observations.py`; emit results to `evidence/reports/multimodal_eval.json` (depends T103, T104)
- [ ] T106 [M3] Real camera-capture end-to-end test: submit a LEFT photo → FAIL, then a TOP_RIGHT photo → PASS → `PROOF_COMPLETE`; record to `evidence/runs/hero_run_001/` (depends T105)
- [ ] T107 [M3] **M3 EXIT GATE** — empirical results recorded; only now may Gemma become a live-demo dependency (depends T106)

**Checkpoint**: Core hero flow proven end-to-end with real physical evidence. **S1 acceptance is achievable from here — everything below is optional or packaging.**

---

## Phase 6: M4 — OPTIONAL Governed Enterprise Fleet `TRACK_ENHANCEMENT`

**⚠️ OPTIONAL**: No FR-001–FR-011 or SC-001–SC-015 depends on this phase. Every component that fails its access check is **DEFERRED, not faked**. This phase MUST NOT block M3 or core acceptance, and no task here may become a prerequisite for any core task.
**Depends on**: T101 (core stable through M2) and T067 (G1 decided).

- [ ] T108 [OPTIONAL] [M4] Implement the GEAP access-gate probe `scripts/geap_access_gate.py` writing per-component PASS/FAIL plus the fallback taken to `evidence/geap_access_gate.json` (plan.md § GEAP Availability Gate)
- [ ] T109 [OPTIONAL] [MANUAL] [M4] **ACCESS CHECK** — Agent Runtime: enable the 7 documented APIs, deploy a trivial ADK agent, confirm a `reasoningEngines/...` resource; record to `evidence/geap_access_gate.json` (quickstart MS-21)
- [ ] T110 [OPTIONAL] [MANUAL] [M4] **ACCESS CHECK — organization parent** — run `gcloud projects describe $PROJECT_ID --format='value(parent)'`; confirm an organization exists and that an IAM binding with a `principal://` member is accepted. **This task MUST precede every Agent Identity implementation task** (quickstart MS-23)
- [ ] T111 [OPTIONAL] [M4] Provision one Agent Identity per agent (`change-intel`, `remediation`, `enablement`, `field-verify`) and bind the mutation capability to the Remediation principal only (depends T110 — blocked if T110 fails)
- [ ] T112 [OPTIONAL] [MANUAL] [M4] **ACCESS CHECK** — Agent Registry: enable `agentregistry.googleapis.com`, register the mutation-tool endpoint and agents, read back the entries (quickstart MS-22)
- [ ] T113 [OPTIONAL] [M4] **PROTOCOL DECISION TASK** — select and record the Artifact Mutation Tool wire protocol (MCP vs plain HTTP) with rationale in `evidence/geap_access_gate.json`. **No Agent Gateway policy-implementation task (T114–T117) may begin before this task completes** (plan.md protocol-decision rule)
- [ ] T114 [OPTIONAL] [M4] Deploy the Artifact Mutation Tool as a standalone authenticated service on the protocol chosen in T113, with its own runtime service account (depends T113)
- [ ] T115 [OPTIONAL] [MANUAL] [M4] Provision Agent Gateway: enable the 12 documented APIs, import the gateway, import the authz extension in `DRY_RUN`, import the authorization policy (depends T112, T113, T114) (quickstart MS-24)
- [ ] T116 [OPTIONAL] [M4] Configure the ALLOW policy: grant `roles/iap.egressor` on the mutation-tool endpoint to the Remediation Agent principal **only**, plus the tool-name-scoped policy; switch the extension to enforcement (depends T111, T115)
- [ ] T117 [OPTIONAL] [M4] Execute and capture the DENY negative security test: Frontline Enablement Agent → Gateway → mutation tool → denied by IAP enforcement; record the deny log entry, unchanged artifact SHA-256, and absent `REMEDIATION_COMPLETED` to `evidence/security/gateway_deny_enablement_to_mutation_tool.json` (depends T116)
- [ ] T118 [OPTIONAL] [MANUAL] [M4] Create the Model Armor template `driftzero-untrusted-artifact-text` (`INSPECT_AND_BLOCK`) in the routed region and grant `roles/modelarmor.user` to the Vertex AI service agent (quickstart MS-25) — independent of Gateway
- [ ] T119 [OPTIONAL] [M4] Wire `modelArmorConfig.promptTemplateName`/`responseTemplateName` into the Change Intelligence `generateContent` call in `src/driftzero/agents/change_intel.py`; on block record `ScreeningBlocked` and fail closed to `REVIEW_REQUIRED`; on documented outage record `SCREENING_SKIPPED` and never claim screening (depends T118)
- [ ] T120 [OPTIONAL] [M4] Add the adversarial text fixture `fixtures/security/injected_artifact_text.json` and the prompt-injection test in `tests/security/test_prompt_injection.py`; emit `evidence/security/prompt_injection_blocked.json` (depends T119)
- [ ] T121 [OPTIONAL] [M4] Enable advanced Agent Observability export and capture agent/gateway spans into the evidence pack (quickstart MS-26) (depends T109)
- [ ] T122 [OPTIONAL] [M4] **M4 EXIT GATE (per component)** — for each of the six components record ACCESS_CHECK result + real capability evidence + fallback taken in `evidence/geap_access_gate.json`; components failing their gate are marked **DEFERRED**, never simulated as delivered (depends T108)

**Checkpoint**: Governance evidence captured where accessible; every gap honestly documented. Core acceptance is unaffected either way.

---

## Phase 7: M5 — OPTIONAL Veo Training Enhancement `BONUS`

**⚠️ OPTIONAL**: Text delta alone satisfies FR-004. Veo failure cannot prevent `PROOF_COMPLETE` and cannot block core acceptance.
**Depends on**: T078 (text-based delta delivery working and evidenced).

- [ ] T123 [OPTIONAL] [MANUAL] [M5] Verify Veo 3.1 API access with one short test generation; record request/response in the evidence pack (quickstart MS-19)
- [ ] T124 [OPTIONAL] [M5] Implement non-blocking Veo generation in `src/driftzero/agents/enablement.py` behind a feature flag, with a bounded async wait/poll deadline separate from the 60 s core timeout; wire Crossing 5 validation so generation success never implies delivery (depends T123, T070)
- [ ] T125 [OPTIONAL] [M5] Enforce generation caps (≤5 development, ≤2 demo, ≤6 s each) and record every generation to `evidence/cost_model.json` with its `ESTIMATED` cost at ~$0.10/generated second (depends T124)
- [ ] T126 [OPTIONAL] [M5] **M5 EXIT GATE** — at least one real project-generated Veo asset plus generation evidence, with the text fallback still verified working (depends T125)

---

## Phase 8: M6 — Demo Surface & Evidence Packaging

**Goal**: minimal hero UI and final evidence packaging. **Begins only after M0–M3 core paths are stable — frontend work is never P0.**
**Depends on**: T107 (or the documented G1 FALLBACK path) and T101.

- [ ] T127 [M6] Implement the worker delta view (receive the delta, understand the expected change) in `src/driftzero/web/templates/delta.html` and `src/driftzero/web/static/` (depends T094)
- [ ] T128 [M6] Implement the field evidence submission surface in `src/driftzero/web/templates/verify.html` with camera capture **and a normal file-upload fallback**, submitting a stable `submission_id` (depends T094, T127)
- [ ] T129 [M6] Implement the Frontline Surface Minimums in `src/driftzero/web/`: narrow phone viewport usable, `FAIL`/`INCONCLUSIVE`/`PASS` shown as **text** never color alone, accessible text labels on hero-flow controls, textual validation/error feedback, desktop keyboard operability (spec § Frontline Surface Minimums) (depends T128)
- [ ] T130 [M6] Implement the workflow state visualization and Change Proof display in `src/driftzero/web/templates/workflow.html` and `proof.html`, rendered from canonical JSON which remains the source of truth (depends T094)
- [ ] T131 [M6] Frontline minimums verification checklist run against the deployed surface; emit `evidence/reports/frontline_minimums.json`, recording any exception honestly (quickstart VS-14) (depends T129)
- [ ] T132 [M6] Implement the evidence pack assembly script `scripts/build_evidence_pack.py` producing the full `evidence/` tree from quickstart § Evidence Pack Structure (depends T101)
- [ ] T133 [M6] Generate `evidence/MANIFEST.json` with SHA-256 hashes for every referenced artifact, plus the stated hash-guarantee boundary (content identity only — no signature, timestamp, attestation, or ledger claim) (depends T132)
- [ ] T134 [P] [M6] Write `evidence/JUDGES_START_HERE.md`: the reproduction path, which claims are proven by which evidence file, and what is synthetic vs real
- [ ] T135 [P] [M6] Write `evidence/LIMITATIONS.md` covering at minimum: fallback identity is application-level not Agent Identity and not per-agent IAM; any DEFERRED GEAP component; Model Armor absence or `SCREENING_SKIPPED`; images not screened; operational (not ledger) immutability; G1 FALLBACK if taken; non-binding engineering targets never measured as claims
- [ ] T136 [MANUAL] [M6] Execute the cleanup/shutdown procedure and reconcile actual billing into `evidence/cost_model.json` under `actual_cost_observed`, kept separate from `ESTIMATED` (quickstart MS-20)
- [ ] T137 [M6] **M6 EXIT GATE** — end-to-end reproducible hero flow + complete evidence pack + `LIMITATIONS.md` + `JUDGES_START_HERE.md` verified against quickstart VS-1 through VS-14 (depends T132, T133, T134, T135, T136)

---

## Dependencies & Execution Order

### Milestone dependencies

```
Phase 0 Setup (T001–T005)
   └─> M0 (T006–T060)  ── HARD GATE T060 ──┐
                                            ├─> M1 (T069–T084) ──> M2 (T085–T101) ──> M3 (T102–T107)
G1 (T061–T068) ── GATE T067 ───────────────┘                              │              │
   (runs early, parallel with M0/M1)                                      │              │
                                                                          ▼              ▼
                                          M4 OPTIONAL (T108–T122) ◄───────┘         M6 (T127–T137)
                                          M5 OPTIONAL (T123–T126) ◄── T078
```

- **M0 is priority zero.** T060 gates M1/M2/M3/M6.
- **G1 runs early**, in parallel with M0/M1, and gates M3 and precedes M4.
- **M3 does not depend on M4** and must not be sequenced after it.
- **M4 and M5 are terminal branches**: nothing in core depends on them.
- **M6 frontend work depends on M2/M3**, never the reverse.

### Critical intra-phase ordering

- T110 (organization/access check) **before** T111 (Agent Identity provisioning).
- T113 (protocol decision) **before** T114–T117 (mutation tool deployment and all Gateway policy work).
- T067 (G1 GO/FALLBACK) **before** T102 (Gemma deployment) and before all of M4.
- G1 serving-feasibility chain: **T063** (effective GPU quota for the selected route) and **T064** (REAL_PHYSICAL fixtures) **before** T066 (deployment + inference feasibility) **before** T067 (GO/FALLBACK) **before** T102 (M3 provisioning). T063 and T064 are deliberately **parallel**, not sequential: photographing a physical box does not depend on GPU quota. Both must be satisfied before T066.
- T091 (runtime service accounts) **before** T096 and T102 (deploys that bind them).
- T032 (action ledger) **before** T033–T036 (dedup and reconciliation).

### Parallel opportunities

- Phase 0: T002, T003, T004, T005 in parallel.
- M0 models: T006, T007, T008, T010, T011, T013, T015, T016, T018 in parallel (distinct files).
- M0 tests: T047–T058 all in parallel once their targets exist.
- G1 (T061–T068) runs as a whole parallel track alongside M0/M1.
- M2 adapters: T092, T093 in parallel.
- M6 docs: T134, T135 in parallel.

---

## Implementation Strategy

### Minimum viable demo
Phase 0 → **M0 (T060 gate)** → G1 (T067 gate) → M1 (T084 gate) → M2 (T101 gate). At this point the hero workflow is real, durable, and event-driven.

### Full core
Add M3 (T107) for physical Gemma verification, then M6 (T137) for the demo surface and evidence pack.

### Optional, only if access and time permit
M4 (governed fleet) and M5 (Veo). Dropping both entirely leaves FR-001–FR-011 and SC-001–SC-015 fully satisfied.

---

## Notes

- `[P]` = different files, no unmet dependency.
- `[OPTIONAL]` tasks may be dropped wholesale without affecting S1 acceptance.
- `[MANUAL]` tasks need a human, cloud console, or account action; they are not code and must not be automated by repository code.
- Every FR/SC mapping follows plan.md § Requirement Traceability Matrix.
- Commit after each task or logical group; do not skip an EXIT GATE.
- Constitution XIV: a task is DONE only when its verification passes and its evidence artifact exists.
