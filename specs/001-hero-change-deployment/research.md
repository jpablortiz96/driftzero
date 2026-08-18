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

## R-003: Gemini Enterprise Agent Platform

### Agent Runtime
**Status**: GA (April 2026). Fully managed serverless execution. Supports async execution up to 7 days. Dormancy/event-driven model.
**Decision**: `TRACK_ENHANCEMENT`. Use Agent Runtime if accessible. If not accessible due to preview/quota limitations, fall back to Cloud Run-based ADK deployment.
**Risk**: Access availability for hackathon accounts may be limited.

### Agent Registry
**Status**: GA. Central managed library for discovering approved agents and tools.
**Decision**: `TRACK_ENHANCEMENT`. Register DRIFTZERO agents if API is accessible. Provides corporate agent discovery evidence for judges.
**Risk**: Setup complexity. Non-blocking if unavailable.

### Memory Bank
**Status**: GA. Long-term memory extraction/retrieval across sessions.
**Decision**: `DEFER`. Authoritative workflow state belongs in Firestore (Constitution Principle VI). Memory Bank could enrich agent reasoning but adds complexity without changing hero workflow quality.

### Agent Identity
**Status**: GA. Native IAM type with cryptographic agent identity and least-privilege enforcement.
**Decision**: `TRACK_ENHANCEMENT`. Assign distinct identities to each agent with minimal permissions. Critical for Fortified Fleet judging (least-privilege demonstration).
**Risk**: IAM configuration complexity. Design boundaries now, configure during implementation.

### Agent Gateway
**Status**: GA. Centralized control point for all agent interactions with policy enforcement.
**Decision**: `TRACK_ENHANCEMENT`. Route agent-to-tool traffic through Gateway for policy enforcement evidence.
**Risk**: Setup complexity. Value depends on demonstrable policy enforcement.

### Model Armor
**Status**: GA. AI-native guardrails for prompt injection, jailbreaking, data leakage protection.
**Decision**: `TRACK_ENHANCEMENT`. Protect against prompt injection in downstream artifact content. Design a reproducible adversarial fixture.
**Risk**: Must verify that Model Armor supports the specific traffic path (agent processing downstream artifact content).

**Sources**: Google Cloud blog (April 2026), cloud.google.com/agent-platform documentation

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
