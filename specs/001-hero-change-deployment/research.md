# Research: Hero Change Deployment

**Feature**: `001-hero-change-deployment`
**Date**: 2026-08-17

## R-001: Core Gemini Model

**Decision**: `gemini-3.5-flash`
**Rationale**: Released 2026-05-19, production-ready GA, full structured output (Pydantic schema) support, multimodal, hackathon-compliant (≥3.5 required). Newer models (3.6 Flash 2026-07-21, 3.7 Flash 2026-08-13) exist but are too recent for a stability-first hackathon demo. Gemini 3.5 Flash has proven API stability, predictable JSON mode, and cost efficiency.
**Alternatives**: Gemini 3.6 Flash (newer, but only ~1 month old), Gemini 3.7 Flash (4 days old, too risky for demo stability).
**Sources**: Google AI Studio model listing, blog.google Gemini 3.5 Flash announcement (2026-05-19)

## R-002: Agent Framework — Google ADK Python

**Decision**: Google ADK Python 2.0+
**Rationale**: ADK 2.0 provides `SequentialAgent`, `ParallelAgent`, `LoopAgent` for deterministic orchestration, plus `LlmAgent` for semantic reasoning. Supports `ResumabilityConfig` for pause/resume with persistent `SessionService` backends (SQLite, asyncpg). Native `adk deploy cloud_run` support. Aligns with Constitution Principle II (Agent Architecture).
**Key capabilities**:
- Deterministic workflow agents (SequentialAgent) for predictable orchestration
- LlmAgent for semantic interpretation tasks
- Custom agents via BaseAgent for specialized logic
- Persistent session state via SessionService (memory://, sqlite://, asyncpg)
- ResumabilityConfig for long-running workflow pause/resume
- Native Cloud Run deployment via `adk deploy cloud_run`
- Unified execution context via `google.adk.Context`
**Alternatives**: LangChain (not Google-native), CrewAI (not Google-native). ADK is the official Google agent framework.
**Sources**: github.com/google/adk-python, adk.dev documentation

## R-003: Gemini Enterprise Agent Platform (GEAP)

All GEAP components below are classified `TRACK_ENHANCEMENT` or `DEFER`. None may become a dependency of the core hero workflow (see plan.md, GEAP Availability Gate). The fallback core is Cloud Run + ADK + Firestore + Pub/Sub + Cloud Storage.

### Agent Runtime
**Status**: GA. Fully managed serverless agent execution with long-running/async support.
**Setup prerequisites (official)**: enable `aiplatform.googleapis.com`, `storage.googleapis.com`, `logging.googleapis.com`, `monitoring.googleapis.com`, `cloudtrace.googleapis.com`, `telemetry.googleapis.com`, `cloudresourcemanager.googleapis.com`; caller needs `roles/aiplatform.user`. The setup guide states no Google Cloud Organization requirement for Agent Runtime itself.
**Identity**: the documentation offers two runtime identity options - Agent Identity (documented as recommended) **or** a service account (the default AI Platform Reasoning Engine service agent, or a custom service account).
**Decision**: `TRACK_ENHANCEMENT`. Use if accessible; otherwise Cloud Run-hosted ADK.
**Source**: [Set up the environment - Agent Runtime](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/runtime/setup)

### Agent Registry
**Status**: GA. Central managed library of approved agents, MCP servers, endpoints, and tools. API: `agentregistry.googleapis.com`.
**Hard coupling**: Agent Gateway blocks egress to any remote MCP server, agent, or tool **not** registered in Agent Registry, so Registry is a prerequisite for the Gateway path, not an independent nice-to-have.
**Decision**: `TRACK_ENHANCEMENT` - required only if Agent Gateway is promoted.
**Source**: [Agent Gateway overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/agent-gateway-overview)

### Memory Bank
**Status**: GA. Long-term memory extraction/retrieval across sessions.
**Decision**: `DEFER`. Authoritative workflow state belongs in Firestore (Constitution Principle VI). Memory Bank could enrich agent reasoning but adds complexity without changing hero workflow truth.

### Agent Identity
**Status**: GA. **Agent Identity is not a service account.** Official IAM documentation: *"Agent Identity provides a strongly attested, cryptographic identity for each agent that is based on the SPIFFE standard."* And explicitly: *"Unlike service accounts, agent identities are not shared by multiple workloads by default, can't be impersonated, and don't allow developers to generate long-lived service account keys."*
**Provisioning**: an agent identity is issued when the agent is deployed to Agent Runtime with the identity type set to agent identity (SDK `types.IdentityType.AGENT_IDENTITY`, CLI `agents-cli deploy --agent-identity`, or ADK `.agent_engine_config.json` with `"identity_type": "AGENT_IDENTITY"`). Google Cloud assigns a unique SPIFFE identity and an X.509 certificate that is rotated automatically (24-hour validity).
**Principal format used in IAM allow policies**:
`principal://agents.global.org-ORGANIZATION_ID.system.id.goog/resources/aiplatform/projects/PROJECT_NUMBER/locations/LOCATION/reasoningEngines/AGENT_ENGINE_ID`
Bindings are granted with the ordinary `gcloud ... add-iam-policy-binding --member="principal://..." --role=ROLE` form.
**Known constraints**: the principal trust domain is organization-scoped (`org-ORGANIZATION_ID`), and agent identities cannot receive Cloud Storage legacy bucket roles (`storage.legacyBucketReader|Writer|Owner`). A hackathon project with no Google Cloud Organization is therefore an explicit access risk - see the ACCESS_CHECK in plan.md.
**Decision**: `TRACK_ENHANCEMENT`. The primary architecture models **one Agent Identity per agent**. Service accounts are documented only as (a) Cloud Run service/runtime identities and (b) the explicit fallback when Agent Identity cannot be provisioned.
**Sources**: [Agent Identity overview (IAM)](https://docs.cloud.google.com/iam/docs/agent-identity-overview), [Use Agent Identity with Agent Runtime](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/agent-identity)

### Agent Gateway
**Status**: GA. *"Agent Gateway is a central policy enforcement point to govern all agent tool calls, manage authentication, and apply security policies."*
**Traffic modes**: **Client-to-Agent (ingress)** and **Agent-to-Anywhere (egress)** - the egress mode governs an agent calling a remote server, tool, or API. It handles *"all HTTP-based traffic, including MCP and A2A traffic."*
**Authorization**: IAM policies restrict agents *"to specific tools or methods based on the agent's identity (SPIFFE ID)"*. For MCP traffic the Gateway parses request attributes, enabling policies that *"grant and deny individual agents or clients access to MCP servers and tools based on the tool name, and whether the tool is read-only or read-write."* Egress enforcement is performed by IAP at runtime; the calling agent principal needs `roles/iap.egressor` (permission `iap.webServiceVersions.egressViaIAP`) on the registered endpoint. Absence of that binding is the deny.
**Setup (official)**: enable `compute.googleapis.com`, `networksecurity.googleapis.com`, `networkservices.googleapis.com`, `dns.googleapis.com`, `iam.googleapis.com`, `agentregistry.googleapis.com`, `aiplatform.googleapis.com`, `discoveryengine.googleapis.com`, `storage.googleapis.com`, `modelarmor.googleapis.com`, `monitoring.googleapis.com`, `logging.googleapis.com`. Resources: gateway (`gcloud network-services agent-gateways import`), authorization extension (`gcloud beta service-extensions authz-extensions import`, where `iamEnforcementMode` supports `DRY_RUN` for audit-only), authorization policy (`gcloud network-security authz-policies import`).
**Observability**: the Gateway *"generates observability telemetry for all agent interactions at the network layer"*, exported to Agent Observability, with Cloud Logging and Cloud Trace visibility. `DRY_RUN` records would-be-denied entries without blocking.
**Conclusion for DRIFTZERO**: the exact path *Remediation Agent identity -> Agent Gateway (egress) -> registered Artifact Mutation Tool (MCP), with the Frontline Enablement Agent identity denied* **is supported by documented capabilities**. It is therefore planned as a concrete path rather than deferred, but it stays gated on account access (Registry + Gateway + Agent Identity all provisionable).
**Decision**: `TRACK_ENHANCEMENT` with one exact governed path (plan.md, Agent Gateway Governed Path).
**Sources**: [Agent Gateway overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/agent-gateway-overview), [Set up Agent Gateway](https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/set-up-agent-gateway)

### Model Armor
**Status**: GA for text screening (image screening is documented as Preview). API: `modelarmor.googleapis.com`.
**Exact supported enforcement points**:
1. **Vertex AI `generateContent` integration** - *"Model Armor provides prompt and response protection within Gemini API in Vertex AI for the `generateContent` method."* A template is applied per request via `modelArmorConfig.promptTemplateName` / `responseTemplateName`, or project-wide via floor settings. Enforcement is `INSPECT_ONLY` (log) or `INSPECT_AND_BLOCK` (block; the response carries `blockReason: "MODEL_ARMOR"`). Requires `roles/modelarmor.user` on the Vertex AI service agent `service-PROJECT_NUMBER@gcp-sa-aiplatform.iam.gserviceaccount.com`.
2. **Agent Gateway integration** - ingress sanitizes `reasoningEngine.streamQuery` requests/responses for ADK agents on Agent Runtime; egress evaluates traffic to external LLMs, third-party agents, and MCP servers (A2A Send Message / Agent Card, MCP tool calls and prompt retrieval).
**Documented limitations**: text only - *"Sanitizing prompts and responses that contain documents isn't supported"*; no non-ADK payloads (for example LangChain), no A2A streaming or gRPC bindings, no MCP resource operations or protocol errors, no OpenAI streaming variants. If the routed region lacks the template the request fails with `Template not found`. During a Model Armor outage the platform *"skips the Model Armor sanitization step and continues processing the request"* - a fail-open behavior DRIFTZERO must compensate for deterministically.
**Billing**: *"Model Armor uses the total number of tokens in AI prompts and responses for pricing purposes."*
**Decision**: `TRACK_ENHANCEMENT` via enforcement point (1) - screening untrusted downstream-artifact **text** before Change Intelligence processing. Path (1) needs no Gateway, no Registry, and no Organization, so it survives independently if Agent Gateway is unavailable.
**Scope boundary**: Model Armor is content screening only. It MUST NOT be described as enforcing artifact authorization, state transitions, proof conditions, or semantic correctness of structured business data - those are Truth Engine responsibilities.
**Sources**: [Integrate Model Armor with Gemini Enterprise Agent Platform](https://docs.cloud.google.com/model-armor/model-armor-vertex-integration), [Model Armor overview](https://docs.cloud.google.com/model-armor/overview), [Configure Model Armor on a gateway](https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/configure-model-armor)

### Agent Observability (advanced)
**Status**: GA as part of the platform; Agent Gateway exports network-layer telemetry to Agent Observability, backed by Cloud Logging and Cloud Trace.
**Decision**: `TRACK_ENHANCEMENT`. Fallback is OpenTelemetry traces plus Cloud Logging / Cloud Trace emitted directly from Cloud Run with correlation IDs, which satisfies Constitution Principle VII without GEAP.

## R-004: Firestore

**Decision**: `CORE`. Firestore Standard Edition.
**Rationale**: Serverless, scale-to-zero, generous free tier (50k reads/20k writes/day, 1 GiB storage). Ideal for authoritative workflow state, evidence metadata, Change Proof records. Satisfies Constitution Principle VI (persistent state != LLM memory).
**Cost**: Near-zero for hackathon volumes. Free tier covers development and demo.
**Sources**: cloud.google.com/firestore/pricing

## R-005: Pub/Sub

**Decision**: `CORE`. Event-driven change ingestion.
**Rationale**: The hero workflow must begin from an external event (not a chatbot prompt). Pub/Sub provides real observable event execution with 10 GiB/month free tier. An approved change is published to a Pub/Sub topic → triggers the workflow. This is a REAL event, not simulated.
**Cost**: Free tier covers hackathon volumes.
**Sources**: cloud.google.com/pubsub/pricing

## R-006: Cloud Run

**Decision**: `CORE`. Primary compute runtime for ADK agents and API surface.
**Rationale**: Serverless, scale-to-zero, native ADK deployment support, container-based. Free tier includes 2M requests/month. If Agent Runtime is not accessible, Cloud Run is the primary runtime.
**GPU for Gemma**: Cloud Run supports NVIDIA L4 (24GB) GPUs with scale-to-zero. Per-second billing. ~5s cold start.
**Sources**: cloud.google.com/run/docs

## R-007: Cloud Storage

**Decision**: `CORE`. Immutable evidence store.
**Rationale**: Store raw evidence artifacts (images, before/after documents, rendered proofs). Cheap, durable, GCS object lifecycle management. Stable object references with integrity hashes.
**Cost**: Minimal for hackathon volumes (standard storage, ~$0.02/GB/month).
**Sources**: cloud.google.com/storage/pricing

## R-008: Gemma 4

**Decision**: `DEMO_CRITICAL`. Field verification vision model.
**Rationale**: Gemma 4 is natively multimodal with image understanding. Used to derive a structured observation (LEFT/TOP_RIGHT/INCONCLUSIVE) from frontline photo evidence. Constitutes a second Google AI model for bonus scoring.
**Serving option**: Vertex AI Model Garden endpoint (simplest one-click deploy) OR Cloud Run with vLLM container (scale-to-zero, cost efficient but more setup).
**Recommended**: Cloud Run + vLLM with NVIDIA L4 GPU for scale-to-zero cost control.
**Variant**: Gemma 4 12B (encoder-free architecture, fits L4 24GB).
**Interface**: Image in → strict JSON structured observation out.
**Critical constraint**: Gemma produces the observation only. PASS/FAIL is deterministic application logic.
**Sources**: ai.google.dev/gemma, Cloud Run GPU documentation

## R-009: Veo 3.1

**Decision**: `BONUS`. Optional microtraining video.
**Rationale**: Generate a short (~4-6s) visual training asset from the approved delta (e.g., "Place label on TOP-RIGHT"). Async API via Gemini API. Text-to-video with native audio.
**Failure behavior**: Non-blocking. Text delta delivery satisfies FR-004 regardless of Veo success.
**Cost risk**: Video generation can be expensive. Budget for ≤5 generations during development + demo.
**Sources**: google.dev Veo API documentation

## R-010: Lyria 3

**Decision**: `DEFER`.
**Rationale**: Lyria 3 Clip can generate 30s audio from text prompts. While it could produce a procedural mnemonic, the frontline use case is weak — a warehouse worker does not need a song about label placement. Integration effort is non-trivial relative to judging bonus value. Audio output does not change Change Proof truth or hero workflow quality.
**Sources**: google.dev Lyria API documentation

## R-011: Frontend / Demo Surface

**Decision**: Small Python-served web app (FastAPI + minimal JS/HTML).
**Rationale**: Hackathon speed. FastAPI serves both the API and a thin HTML interface. No React/Next.js complexity. Mobile-responsive for phone-based field verification photo upload. Consistent with Python-first constitution rule.
**Alternatives**: Separate frontend framework (overhead), Streamlit (limited customization), Google Chat integration (insufficient visual surface for judges).

## R-012: Event Source Strategy

**Decision**: Option A — Pub/Sub with synthetic approved change JSON.
**Rationale**: A synthetic approved change fixture is published to a real Pub/Sub topic. The downstream workflow processes a real event. This is honest: the business content is SYNTHETIC, the event delivery is REAL.
**Alternative rejected**: Google Drive change event (requires OAuth domain setup, Workspace admin consent, impractical for hackathon reproducibility).

## R-013: Agent Identity vs. Service Account (Terminology Rule)

**Decision**: planning artifacts MUST keep these two concepts distinct.

| | Agent Identity | Service account |
|---|---|---|
| What it is | SPIFFE-based cryptographic identity issued per deployed agent | Long-lived Google Cloud principal for a workload |
| Sharing | Not shared across workloads by default | Commonly shared across workloads |
| Impersonation | Cannot be impersonated | Impersonation supported |
| Keys | No long-lived key generation | Long-lived keys can be created |
| Credential binding | X.509 / mTLS + DPoP, Context-Aware Access, un-replayable tokens | Bearer tokens |
| IAM principal | `principal://agents.global.org-ORG_ID.system.id.goog/resources/aiplatform/projects/.../reasoningEngines/AGENT_ID` | `serviceAccount:NAME@PROJECT.iam.gserviceaccount.com` |

**Rule applied across spec / plan / contracts**: a dedicated service account is NEVER called "Agent Identity". Service accounts appear only as (a) Cloud Run service/runtime identities and (b) the named fallback when Agent Identity cannot be provisioned (for example no Google Cloud Organization, or Agent Runtime unavailable). In the fallback the security property is weaker and MUST be documented as such in `LIMITATIONS.md`.
**Sources**: [Agent Identity overview (IAM)](https://docs.cloud.google.com/iam/docs/agent-identity-overview), [Set up the environment - Agent Runtime](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/runtime/setup)

## R-014: Tool Poisoning / Malicious Tool Response

**Threat**: a tool (MCP server, HTTP tool, or a compromised or spoofed endpoint) returns a response that is **schema-valid** but semantically unauthorized - for example it reports mutation of an `artifact_id` that was never in `authorized_scope`, cites a `source_version` different from the one under processing, or returns before/after refs pointing at objects the workflow never wrote. Pydantic validation accepts all of these because the shape is correct.

**Decision**: schema validation is necessary but NOT sufficient, and the defense applies to **every** non-authoritative result crossing into the Truth Engine, not only tool responses (plan.md, Trust-Boundary Validation Policy). Validation is context-appropriate per crossing: ChangeSet, RemediationEvidence, DeliveryResult, FieldObservation, and optional Veo output. The remediation crossing carries the fullest chain: schema -> provenance / expected source -> expected artifact and tool identity -> authorization and scope -> source-version applicability -> before-state hash consistency -> deterministic semantic invariants. All layers beyond schema are deterministic Truth Engine responsibilities; no LLM judgment participates in accepting any result.

**Relationship to Agent Gateway**: the Gateway restricts *which* tools an agent may call (egress allow/deny) and Agent Registry restricts *which* hosts exist at all, but neither validates the *content* of a tool reply against DRIFTZERO business invariants. Gateway and Registry are complementary to - never a substitute for - the Truth Engine validation chain, and that chain must hold in the fallback architecture where no Gateway exists.

**Sources**: [Agent Gateway overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/agent-gateway-overview) (registration and allow/deny scope); Constitution Principles III, IV, VIII.

## R-015: Cost Unit Pricing Discipline

**Decision**: planning artifacts record **unit-price drivers and caps**, not a predicted project total. A predicted total is permitted only when it is derived from a documented unit price plus an explicitly stated usage assumption; otherwise the artifact records the driver plus a LOW / MEDIUM / HIGH relative cost risk.

**Documented unit drivers captured at plan time**:
- **Veo 3.1**: billed on output tokens at 5,792 tokens per second of 720p video, an effective rate of approximately **$0.10 per second of generated video** under Standard pricing. A 6-second clip is therefore about $0.60 ESTIMATED, and the 5-generation development cap bounds development video spend at roughly $3 ESTIMATED.
- **Model Armor**: billed on *"the total number of tokens in AI prompts and responses"*.
- **Gemini (Flash tier)**: billed per 1M input tokens and per 1M output tokens. The exact per-model rate MUST be read from the current pricing page at implementation time and recorded in `evidence/cost_model.json` rather than assumed here.
- **Cloud Run**: billed per CPU-second, GiB-second, and request; **Cloud Run GPU** additionally per GPU-second (NVIDIA L4). Rates MUST be read from the Cloud Run pricing page at deployment time. Scale-to-zero means idle cost is zero; `max-instances` is the primary spend guard.
- **Firestore / Pub/Sub / Cloud Storage**: free-tier-dominated at hackathon volume; Cloud Storage standard storage is the only recurring driver.

**Rule**: every cost figure in planning artifacts is labelled `ESTIMATED`. `ACTUAL COST OBSERVED` values are written only after being read from Google Cloud billing, and are stored in the evidence pack. The two MUST NOT be merged into a single number.

**Sources**: [Agent Platform pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing), [Model Armor overview](https://docs.cloud.google.com/model-armor/overview), [Cloud Run pricing](https://cloud.google.com/run/pricing), [Firestore pricing](https://cloud.google.com/firestore/pricing)
