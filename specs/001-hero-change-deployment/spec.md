# Feature Specification: Hero Change Deployment

**Feature Branch**: `001-hero-change-deployment`

**Created**: 2026-08-17

**Status**: Clarified

## Product Thesis

DRIFTZERO exists because:
**“A process change isn't deployed when the document changes. It's deployed when the work changes.”**

The primary human is not an executive or administrator.
The primary human is a **frontline worker performing the changed procedure**.

**Reference Scenario:**
- A warehouse packing procedure has an APPROVED new version.
- Previous requirement: shipping label must be placed on the **LEFT** side of a package.
- New approved requirement: shipping label must be placed on the **TOP-RIGHT** side.
- One authorized downstream work instruction still contains the obsolete LEFT instruction.
- One frontline worker is affected by this change.
- The worker initially performs the OLD behavior.
- After receiving the change delta, the worker corrects the physical execution.
- DRIFTZERO must produce an auditable **Change Proof** only when the workflow has reached its valid completion conditions (`PROOF_COMPLETE`).

## Core Hero Transformation

The sequence must be:
**approved source change → impact identified → stale artifact remediated → frontline delta delivered → physical execution verified → Change Proof**

## Primary User

**Frontline warehouse packing worker**.
The worker needs to know only the operational delta that affects their work and must be able to determine whether their real execution satisfies the current approved procedure. Secondary actors (approvers, coordinators) are strictly secondary.

**Worker Identity & Privacy**: The workflow requires only a stable worker/demo reference sufficient to associate delivery and verification events. It MUST NOT require real employee PII. Change Proof references an opaque identifier.

## Source of Truth

1. **Approved source procedure**: authoritative, versioned, read-only to the remediation workflow, defines expected behavior. System MUST NEVER modify this.
2. **Derived operational artifact**: may contain stale information, remediated only when explicitly authorized.
3. **Frontline execution evidence**: represents actual worker performance, compared against current approved requirement.
4. **Change Proof**: records evidence that the change reached downstream artifact and frontline execution. Never claim completion based solely on LLM assertion.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Detect and represent an approved operational delta (Priority: P1)
**Acceptance Scenarios**:
1. **Given** an approved procedure changed from LEFT to TOP-RIGHT, **When** the feature receives the approved change, **Then** it must establish previous requirement, current requirement, source version identity, change identity, and affected operation without modifying the authoritative procedure.

### User Story 2 - Identify the stale downstream artifact (Priority: P1)
**Acceptance Scenarios**:
1. **Given** one authorized downstream instruction corresponds to the changed requirement, has a conflicting value, and is explicitly authorized, **When** impact analysis is performed, **Then** that artifact must be identified as affected, providing an auditable reason. Unrelated artifacts with lexical matches (e.g., token `LEFT`) MUST NOT be marked affected solely because of the text match.

### User Story 3 - Remediate only an authorized stale artifact (Priority: P1)
**Acceptance Scenarios**:
1. **Given** the affected artifact satisfies all autonomous remediation conditions, **When** the change is applied, **Then** the artifact must represent TOP-RIGHT rather than LEFT, preserving before/after evidence.
2. **Given** an artifact is already compliant when remediation executes, **When** remediation executes, **Then** no mutation MUST occur, the no-op outcome MUST be recorded as evidence, and workflow MAY continue.
3. **Given** authorization is absent or ambiguous, **When** the change is processed, **Then** the system must NOT perform mutation and must enter `REVIEW_REQUIRED`.

### User Story 4 - Deliver only the operational delta to the affected worker (Priority: P1)
**Acceptance Scenarios**:
1. **Given** the worker operated under LEFT instruction, **When** the changed requirement is ready for frontline delivery, **Then** positive evidence of `DELIVERED` must be recorded. This means the delta was delivered to the intended identity, but MUST NOT imply comprehension, retention, or behavioral compliance.

### User Story 5 - Detect incorrect frontline execution (Priority: P1)
**Acceptance Scenarios**:
1. **Given** the expected requirement is TOP-RIGHT, **When** execution evidence derives an observation of LEFT, **Then** the deterministic verification decision must be FAIL, the workflow enters `VERIFICATION_FAILED`, the workflow MUST NOT reach `PROOF_COMPLETE`, and raw evidence and derived observation remain traceably associated.

### User Story 6 - Verify corrected frontline execution (Priority: P1)
**Acceptance Scenarios**:
1. **Given** a prior verification was FAIL, **When** new execution evidence derives an observation of TOP-RIGHT, **Then** the deterministic verification decision must be PASS, the workflow enters `VERIFICATION_PASSED`, and the workflow progresses. Evidence history must retain both attempts.

### User Story 7 - Generate Change Proof only after valid completion (Priority: P1)
**Acceptance Scenarios**:
1. **Given** all 7 mandatory completion conditions are fully met (valid source change, determined impact, remediated/no-op artifact, delivered delta, latest authoritative field verification is PASS, complete evidence trail, and a current authoritative state compatible with completion), **When** finalization executes, **Then** the workflow enters `PROOF_COMPLETE`.
2. **Given** any mandatory condition is currently unresolved, or the workflow is currently blocked in `VERIFICATION_FAILED` / `VERIFICATION_INCONCLUSIVE`, or has entered `REVIEW_REQUIRED`, `SUPERSEDED`, or `FAILED`, **Then** the workflow MUST NOT reach `PROOF_COMPLETE`.
3. **Given** the evidence history contains earlier `VERIFICATION_FAILED` or `VERIFICATION_INCONCLUSIVE` attempts **and** a later authoritative verification produced `VERIFICATION_PASSED`, **When** finalization executes, **Then** the preserved historical FAIL/INCONCLUSIVE evidence MUST NOT block `PROOF_COMPLETE`, and that history MUST appear in the Change Proof evidence trail.

### User Story 8 - Duplicate change event idempotency (Priority: P2)
**Acceptance Scenarios**:
1. **Given** the same approved change event is received more than once, **Then** the logical change must not produce duplicate remediation, delivery, or completion evidence.

### User Story 9 - Long-running pause, resume, and supersession (Priority: P1)
**Acceptance Scenarios**:
1. **Given** the workflow is interrupted and later resumes, **Then** previously completed logical actions remain recognized and must not be duplicated.
2. **Given** a newer approved source version arrives while the current workflow is incomplete, **Then** the previous workflow must enter `SUPERSEDED` state and MUST NOT reach `PROOF_COMPLETE`. Evidence must be retained, completed logical actions must not be erased, and the newer version starts a distinct workflow.

### User Story 10 - Malformed, insufficient, or ambiguous evidence fail-closed (Priority: P1)
**Acceptance Scenarios**:
1. **Given** the system cannot reliably establish approved change, affected artifact, authorization, or frontline result, **Then** it must fail closed (e.g. `REVIEW_REQUIRED`, `VERIFICATION_INCONCLUSIVE`, `FAILED`) and never silently convert uncertainty into PASS or `PROOF_COMPLETE`.

## Edge Cases

- **Same change received twice**: Treated idempotently; no duplicate actions.
- **Stale artifact already corrected before remediation**: Records no-op evidence, no duplicate mutation fabricated, workflow continues.
- **Unrelated artifact contains lexical match**: Not marked affected without matching requirement/operation scope.
- **Source version changes again while incomplete**: Workflow becomes `SUPERSEDED` and cannot reach `PROOF_COMPLETE`.
- **Failed side effects (mutation/delivery)**: Action NOT represented as completed, workflow MUST NOT reach `PROOF_COMPLETE`, retries allowed without duplicating prior actions, failed attempt kept in evidence.
- **Field evidence missing or inconclusive**: Results in `VERIFICATION_INCONCLUSIVE`. Workflow MUST NOT reach `PROOF_COMPLETE`.
- **First field verification fails and second passes**: Latest valid verification dictates status. Both attempts recorded and retained; the preserved FAIL attempt does not block eventual `PROOF_COMPLETE`.
- **Older verification arrives late**: Must not override a newer valid verification.
- **Required evidence reference becomes unavailable**: Fails closed into `FAILED`.
- **Late events arrive after Change Proof completion**: Duplicate events idempotent; late historical events retained for audit but MUST NOT silently rewrite the completed proof.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST process an approved procedural change event without modifying the authoritative source.
- **FR-002**: System MUST identify affected authorized downstream artifacts using exact semantics (operation match, instruction correspondence, conflicting value, authorized scope) and provide auditable reasoning.
- **FR-003**: System MUST autonomously remediate authorized downstream artifacts only when all 9 autonomous boundary conditions are met, preserving before/after evidence.
- **FR-004**: System MUST record positive evidence of operational delta delivery (`DELIVERED`) to the affected frontline worker identity.
- **FR-005**: System MUST perform deterministic physical execution verification by separating raw evidence, derived observation, and deterministic PASS/FAIL comparison.
- **FR-006**: System MUST generate an immutable, auditable Change Proof and transition to `PROOF_COMPLETE` only when all 7 completion conditions are met.
- **FR-007**: System MUST handle repeated delivery of the same logical change event idempotently so that duplicate receipt does not create duplicate remediation, duplicate frontline delivery, duplicate completion evidence, or a second Change Proof.
- **FR-008**: System MUST persist state for long-running workflows to survive restarts and prevent duplication of completed actions upon retry.
- **FR-009**: System MUST transition an incomplete workflow to `SUPERSEDED` if a newer applicable source version arrives, starting a new workflow for the newer version.
- **FR-010**: System MUST explicitly classify data to capture lineage, supporting multiple non-exclusive dimensions of REAL, SYNTHETIC, DERIVED, and SIMULATED.
- **FR-011**: System MUST fail closed when conditions are ambiguous, malformed, unauthorized, insufficient, or otherwise non-actionable. Specifically: ambiguous or insufficient change interpretation MUST NOT silently proceed; unauthorized remediation targets or unsatisfied autonomous remediation preconditions MUST enter `REVIEW_REQUIRED` (blocking/recoverable); physical evidence that cannot produce a reliable normalized observation MUST enter `VERIFICATION_INCONCLUSIVE` (blocking/recoverable); malformed or invalid required inputs MUST NOT become successful completion; genuinely unrecoverable integrity conditions MUST enter `FAILED` (terminal non-success). None of these states may silently become `PROOF_COMPLETE`.

### State Requirements (Workflow Lifecycle)
Explicitly supported lifecycle states:
- `CHANGE_RECEIVED`
- `IMPACT_DETERMINED`
- `REMEDIATION_PENDING`
- `REVIEW_REQUIRED` (blocking gate; no autonomous exit in this scope — prevents `PROOF_COMPLETE`)
- `REMEDIATION_COMPLETED`
- `FRONTLINE_DELIVERY_COMPLETED`
- `AWAITING_FIELD_VERIFICATION`
- `VERIFICATION_INCONCLUSIVE` (blocking while current; recoverable — permits subsequent corrected verification)
- `VERIFICATION_FAILED` (blocking while current; recoverable — permits subsequent corrected verification)
- `VERIFICATION_PASSED`
- `PROOF_COMPLETE` (canonical successful terminal state, immutable)
- `SUPERSEDED` (terminal non-success state for superseded versions; can never later reach `PROOF_COMPLETE`)
- `FAILED` (terminal non-success state for unrecoverable failures; can never later reach `PROOF_COMPLETE`)

**State occupancy vs. state history**: Completion conditions are evaluated against the workflow's **current authoritative state**. Recoverable states (`VERIFICATION_FAILED`, `VERIFICATION_INCONCLUSIVE`) block completion only while the workflow is currently in them. Terminal non-success states (`SUPERSEDED`, `FAILED`) and the `REVIEW_REQUIRED` gate block completion permanently. Entering any non-success state MUST NOT cause evidence loss — the full attempt history is retained for audit.

### Autonomy Boundaries
Autonomous remediation is permitted ONLY for a narrow atomic change (e.g., `label_position: LEFT → TOP_RIGHT`) when ALL of the following are satisfied:
1. approved source version is known
2. logical requirement being changed is known
3. previous value is known
4. new approved value is known
5. exactly one intended atomic requirement change is being applied
6. target artifact is authorized for remediation
7. corresponding target instruction is uniquely identifiable
8. artifact does not contain conflicting/additional divergence
9. sufficient before/after evidence can be preserved.
Otherwise, the system MUST trigger `REVIEW_REQUIRED`.

### Data Classification
Data classification emphasizes transparency and lineage rather than a mutually exclusive single label. An evidence item MAY require multiple classifications.
- **REAL**: Actual captured event, write, model call, trace, human interaction.
- **SYNTHETIC**: Intentionally fabricated fixture representing an enterprise scenario.
- **DERIVED**: Artifact/observation produced from other evidence.
- **SIMULATED**: Action/dependency emulated instead of actually executed.

### Evidence Requirements
Must traceably answer: What changed? What authorized it? What was affected? What was modified? Was delta delivered? What did worker do? Why did verification fail/pass? What actions were autonomous vs human? What was data classification lineage? Is Proof internally consistent?

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The approved LEFT → TOP-RIGHT delta is correctly represented.
- **SC-002**: The intentionally stale authorized artifact is detected based on semantic constraints, while an unrelated lexical match remains unchanged.
- **SC-003**: The authorized stale artifact ends with TOP-RIGHT.
- **SC-004**: The authoritative master procedure remains unchanged.
- **SC-005**: The affected worker receives the correct operational delta, yielding a `DELIVERED` status.
- **SC-006**: Deliberately incorrect LEFT frontline execution (derived observation) deterministically produces FAIL.
- **SC-007**: Corrected TOP-RIGHT execution deterministically produces PASS, progressing the workflow.
- **SC-008**: Change Proof remains incomplete while the latest authoritative verification is FAIL or `VERIFICATION_INCONCLUSIVE` (a later valid PASS may subsequently complete the proof — see SC-007 and SC-009).
- **SC-009**: Change Proof transitions to `PROOF_COMPLETE` only after all 7 mandatory completion conditions pass, evaluated against the workflow's current authoritative state at proof-evaluation time. Preserved historical FAIL/INCONCLUSIVE attempts do not by themselves block completion once a later authoritative PASS exists; `SUPERSEDED`, `FAILED`, and `REVIEW_REQUIRED` block it permanently.
- **SC-010**: Duplicate delivery of the same logical change produces zero duplicate logical side effects.
- **SC-011**: Pause/resume and retries do not duplicate already completed logical actions.
- **SC-012**: Ambiguous or insufficient evidence never becomes a silent PASS.
- **SC-013**: Every judged evidence item has explicit data classification and lineage.
- **SC-014**: The complete hero flow is reproducible using documented reference fixtures.
- **SC-015**: A workflow superseded by a newer version correctly reaches `SUPERSEDED` without reaching `PROOF_COMPLETE`.

## Change Proof
The primary deliverable artifact, containing: proof ID, change ID, workflow identity, authoritative source ID & version, previous requirement, current requirement, affected artifact ID, before/after remediation references, frontline delivery status, field verification result, timestamps, data classification, completion status, evidence references, and integrity hashes. NOT an LLM summary or confidence score. Completed Change Proofs MUST NOT be silently rewritten.

**Change Proof Mandatory Completion Conditions**:
`PROOF_COMPLETE` is granted ONLY when ALL of the following conditions are simultaneously satisfied:
1. The authoritative approved source change is represented and remains applicable;
2. Impact has been validly determined;
3. The target artifact is either: (a) successfully remediated, or (b) validly recorded as an already-compliant no-op;
4. The operational delta has been successfully `DELIVERED`;
5. The latest authoritative field verification for the applicable source version/change is PASS (`VERIFICATION_PASSED`);
6. All evidence required to establish the above conditions exists and is traceably associated;
7. At proof-evaluation time, the workflow MUST NOT currently be in — and MUST NOT have terminated in — a condition incompatible with completion. This condition is evaluated against the **current authoritative state**, not against the full historical state path:
   - **Terminal non-success states** — `SUPERSEDED` and `FAILED` — permanently prevent `PROOF_COMPLETE`. A superseded or terminally failed workflow can NEVER later recover to `PROOF_COMPLETE`, regardless of any subsequent evidence.
   - **Blocking gate** — `REVIEW_REQUIRED` prevents `PROOF_COMPLETE`. Within the scope of this specification `REVIEW_REQUIRED` has no autonomous exit, so a workflow that has entered `REVIEW_REQUIRED` MUST NOT reach `PROOF_COMPLETE`.
   - **Currently-blocking recoverable states** — a workflow currently in `VERIFICATION_FAILED` or `VERIFICATION_INCONCLUSIVE` MUST NOT reach `PROOF_COMPLETE` while it remains in that state.
   - **Historical recoverable verification attempts** — prior `VERIFICATION_FAILED` and `VERIFICATION_INCONCLUSIVE` occurrences MAY exist in the evidence history and DO NOT disqualify completion, provided a later authoritative verification for the applicable source version/change produced `VERIFICATION_PASSED` (condition 5). All historical FAIL/INCONCLUSIVE evidence MUST remain preserved and traceably associated (condition 6); it MUST NOT be deleted, overwritten, or omitted from the Change Proof evidence trail.

   This condition supports, and MUST NOT be read as contradicting, the required `FAIL → corrected evidence → PASS → PROOF_COMPLETE` recovery path defined in User Story 6.

## Non-Goals
- Generic enterprise SOP management
- Arbitrary document editing
- Management analytics dashboards
- User authentication product flows
- Billing or org administration
- Generalized workflow builder
- Multiple procedure types, workers, sites
- ERP/WMS/HR integrations
- Production enterprise credentials
- Mobile application polish or final UI system
- Agent Registry/Gateway/Identity implementation
- Memory Bank / Model Armor / Final observability implementation
- Generated training media (Veo/Lyria) or Gemma-based verification
- Specific Google Cloud architectures
- Implementation of reviewer UI, escalation, or SLA for `REVIEW_REQUIRED` (fail closed is acceptable).

## Hackathon Alignment
This feature is highly suitable for an autonomous agentic system because:
- It begins from an external change event rather than a chat prompt.
- It coordinates a multi-step workflow asynchronously awaiting field verification.
- It performs bounded authorized actions.
- It preserves state and evidence over time.
- It closes the loop through a real-world verification outcome.
