<!--
Sync Impact Report:
- Version change: 1.0.0 -> 1.1.0
- Corrections: Corrected Spec Traceability path to specs/<feature-branch>/.
- Workflow Updates: Added /speckit.checklist requirements-quality validation gate to Development Workflow.
- Modified principles: None.
- Deferred items: None.
-->

# DRIFTZERO Project Constitution

**Target Track**: Google All Things Agentic Hackathon — Fortified Enterprise Fleet  
**Product Thesis**: *“A process change isn't deployed when the document changes. It's deployed when the work changes.”*

DRIFTZERO is an autonomous enterprise agent fleet that detects an approved operational procedure change, determines where that change matters, remediates authorized stale downstream artifacts, teaches the delta to affected frontline workers, verifies execution in the physical world, and produces an auditable **Change Proof**.

---

## Core Principles

### I. Spec Before Code
- No production feature may be implemented before its specification (`spec.md`), acceptance criteria, technical plan (`plan.md`), task breakdown (`tasks.md`), and analysis gate (`speckit-analyze`) exist.
- Requirements change the spec first; implementation strictly follows the spec.
- Developers and agents must not silently expand scope.

### II. Evidence Over Claims
- Every important product or hackathon claim must be backed by reproducible evidence.
- Developers must not invent confidence scores, benchmark results, accuracy claims, regulatory claims, cost savings, user metrics, or production claims.
- Raw test inputs and raw outputs must be preserved when they support a judged claim.
- The project must explicitly distinguish REAL, SYNTHETIC, DERIVED, and SIMULATED data.

### III. Deterministic Truth Boundary
- LLMs may interpret, classify, extract, explain, or propose actions.
- Critical state, authorization, workflow transitions, version identity, artifact hashes, verification status, idempotency, and evidence manifests must use deterministic application logic wherever practical.
- Never allow an LLM-generated statement to become its own proof.

### IV. Least Privilege and Separation of Duties
- Every agent or service must receive only the permissions required for its irreducible responsibility.
- Reading, mutating, training, verification, and evidence responsibilities must remain separable when their security boundaries differ.
- A component must not gain additional access merely because it is convenient.

### V. Safe Autonomous Action
- Autonomous actions must be bounded, observable, replayable when practical, and idempotent.
- Consequential or ambiguous actions require an explicit human approval gate unless the specification provides a deterministic policy allowing autonomous execution.
- Retries must not duplicate mutations, notifications, or evidence.

### VI. Persistent Workflow State Is Not LLM Memory
- Authoritative transactional workflow state must live in an explicit deterministic store.
- Semantic memory may enrich reasoning but cannot replace authoritative state.
- Long-running workflows must survive process restarts and resume without corrupting or duplicating completed work.

### VII. Observable by Default
- Agent calls, tool calls, policy decisions, state transitions, retries, failures, and human approvals must produce traceable telemetry.
- Important workflows must have correlation IDs / run IDs.
- No judged workflow may depend on invisible manual intervention.

### VIII. Failure Is a First-Class State
- External API failure, model failure, malformed input, duplicate events, authorization denial, timeout, partial completion, hallucinated output, and unavailable bonus models must have explicit handling behavior.
- Systems must fail closed where safety or data integrity is involved.
- Never silently convert a failed operation into success.

### IX. Google-Native Where It Matters
- Gemini 3.5 or newer, a supported Google agent framework, and Google Cloud infrastructure are mandatory hackathon requirements and must be structurally integrated rather than decorative.
- Additional Google models such as Gemma, Veo, or Lyria may only be added when they serve a defensible product function.
- Gemini Enterprise Agent Platform capabilities should only be used where they improve real runtime, memory, governance, security, discovery, or observability.

### X. Frontline-First Product Design
- The primary human outcome is successful deployment of operational change to the person performing the work.
- Management dashboards are secondary.
- The hero transformation must remain understandable as: **source change → impact → action → frontline verification → Change Proof**.

### XI. Narrow Complete Core
- Prefer one complete hero workflow over multiple incomplete capabilities.
- Authentication systems, billing, generic analytics, generalized enterprise integrations, mobile polish, or broad collaboration features must not be built unless required by the current approved spec.
- Avoid speculative abstractions and premature platform engineering.

### XII. Hackathon Reproducibility
- A judge must eventually be able to understand and reproduce the core flow from the repository.
- Setup instructions, `.env.example`, seed data, smoke tests, architecture documentation, evidence, and known limitations are required before final submission.
- Secrets must never be committed.
- Demo-specific fixtures must be clearly labeled and reproducible.

### XIII. No Hidden Simulation
- Synthetic enterprise fixtures are allowed when clearly labeled.
- Real APIs, real writes, real cloud deployment, real traces, real model calls, and simulated enterprise data must never be presented as something they are not.
- Any mocked dependency must be explicitly documented.

### XIV. Definition of Done Means Verified
- A task is not DONE because code was written.
- DONE requires the relevant test or verification command to pass and the required evidence artifact to exist.
- Tooling and agents must report failures honestly and must not mark blocked work as completed.

---

## Engineering Governance Rules

1. **Backend Technology Stack**: Python-first backend unless a later technical plan provides a strong, explicit reason otherwise.
2. **Agent Architecture**: Favor Google Agent Development Kit (ADK) for the runtime agent architecture unless research demonstrates a superior required Google framework.
3. **Cloud Infrastructure**: Favor managed/serverless Google Cloud services (e.g. Cloud Run, Firestore, BigQuery, Vertex AI) suitable for a hackathon budget.
4. **Secret Hygiene**: Never commit secrets, credentials, service account keys, API keys, tokens, `.env`, generated private user data, or raw PII. `.env.example` must contain placeholder keys only.
5. **Version Control**: Use Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
6. **Spec Traceability**: All meaningful feature work must be linked to a Spec Kit specification (`specs/<feature-branch>/`).
7. **Phase Control**: No application code or feature scaffolding shall begin as part of this constitution task.

---

## Development Workflow & Quality Gates

- **Constitution Phase**: Establish or amend project governance (`speckit-constitution`).
- **Specification Phase**: Create feature specification via Spec Kit (`speckit-specify`).
- **Clarification Gate**: Verify zero underspecified areas (`speckit-clarify`).
- **Planning Phase**: Generate technical architecture, data contracts, and implementation plan (`speckit-plan`).
- **Requirements Quality Gate**: Validate requirements quality (`speckit-checklist`) around autonomy boundaries, human approval boundaries, security and least privilege, failure handling, idempotency, state persistence, evidence/reproducibility, data classification, observability, and accessibility/frontline UX.
- **Task Decomposition Phase**: Break down implementation into actionable, dependency-ordered tasks (`speckit-tasks`).
- **Consistency Analysis Gate**: Perform non-destructive cross-artifact consistency check (`speckit-analyze`) before proceeding to code.
- **Implementation Phase**: Execute tasks sequentially with automated tests and empirical verification (`speckit-implement`).
- **Convergence Verification**: Verify codebase alignment with spec and plan (`speckit-converge`).

---

## Governance

- **Precedence**: This Constitution supersedes all informal team practices and undocumented development decisions.
- **Amendments**: Amendments require updating this document, incrementing `CONSTITUTION_VERSION`, documenting rationale in the Sync Impact Report header, and verifying consistency across existing specs.
- **Semantic Versioning Policy**:
  - **MAJOR**: Backward-incompatible governance changes, principle removals, or redefinitions.
  - **MINOR**: Material expansion of principles or addition of new governance sections.
  - **PATCH**: Wording refinements, clarifications, or non-semantic formatting updates.
- **Compliance Review**: All technical plans, spec reviews, and PRs must explicitly check compliance against these 14 principles.

---

**Version**: 1.1.0 | **Ratified**: 2026-08-17 | **Last Amended**: 2026-08-17
