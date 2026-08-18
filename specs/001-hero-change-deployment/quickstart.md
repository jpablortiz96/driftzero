# Quickstart & Validation Guide: Hero Change Deployment

**Feature**: `001-hero-change-deployment`
**Date**: 2026-08-17

## Prerequisites

- Python 3.11+
- Google Cloud SDK (`gcloud`) authenticated
- GCP project with billing enabled and hackathon credits applied
- `uv` or `pip` for Python dependency management
- Docker (for Gemma 4 Cloud Run GPU deployment)

## Manual Setup Required

The following actions MUST be performed manually before implementation begins. They cannot be safely automated by repository code.

| # | Action | Why | When | Cost | Verification |
|---|---|---|---|---|---|
| 1 | Create/select GCP project | All cloud resources need a project | Before any cloud work | Free | `gcloud projects describe $PROJECT_ID` |
| 2 | Associate billing account | Required for non-free-tier resources and GPU | Before any cloud work | Free (action itself) | `gcloud billing projects describe $PROJECT_ID` |
| 3 | Redeem hackathon credits | Offset cloud costs | Immediately after project creation | Free | Check billing account credits |
| 4 | Enable required APIs | Firestore, Pub/Sub, Cloud Run, Cloud Storage, Vertex AI, IAM | Before M2 milestone | Free | `gcloud services list --enabled` |
| 5 | Authenticate local CLI | ADC for local development | Before M1 milestone | Free | `gcloud auth application-default print-access-token` |
| 6 | Select region | Consistent region for all services (prefer `us-central1` for GPU) | Before M2 milestone | Free | N/A |
| 7 | Create Firestore database | Standard edition, selected region | Before M2 milestone | Free tier | Firebase Console or `gcloud firestore databases create` |
| 8 | Create Pub/Sub topic | `driftzero-approved-changes` topic | Before M2 milestone | Free tier | `gcloud pubsub topics describe driftzero-approved-changes` |
| 9 | Create GCS bucket | `driftzero-evidence-{project-id}` | Before M2 milestone | ~$0.02/GB/month | `gcloud storage buckets describe gs://...` |
| 10 | Configure IAM service accounts | Per-agent least-privilege identities (if Agent Identity used) | Before M3 milestone | Free | `gcloud iam service-accounts list` |
| 11 | Request Gemma 4 model access | Vertex AI Model Garden or build container | Before M4 milestone | GPU compute cost | Model Garden access confirmation |
| 12 | Deploy Gemma 4 to Cloud Run (GPU) | vLLM container with L4 GPU | Before M4 milestone | ~$0.50-1.00/hr when active, $0 idle | `gcloud run services describe gemma-verification` |
| 13 | Request Veo 3.1 API access | Gemini API access for video generation | Before M5 milestone | Per-generation cost | API key test call |
| 14 | Set budget alerts | $25, $50, $75 thresholds | After billing setup | Free | Cloud Console Billing > Budgets |
| 15 | Prepare physical demo fixture | Real box, printed label, camera-facing orientation | Before M4 milestone | ~$5 materials | Visual inspection |
| 16 | Agent Platform setup (if used) | Agent Runtime, Registry, Gateway, Identity | Before M3 milestone | Varies | Console verification |

## Validation Scenarios

### VS-1: Truth Engine Unit Test Suite
**Proves**: FR-001, FR-006, FR-007, FR-008, FR-009, FR-011; SC-001 through SC-015
```bash
pytest tests/unit/truth_engine/ -v
```
**Expected**: All state transition, idempotency, supersession, completion invariant, and fail-closed tests pass.

### VS-2: End-to-End Hero Flow (Local)
**Proves**: SC-001 → SC-009, SC-014
```bash
# 1. Inject synthetic approved change
python -m driftzero.cli inject-change --fixture fixtures/hero_change.json

# 2. Verify workflow progresses through states
python -m driftzero.cli status --workflow-id $WF_ID

# 3. Submit INCORRECT field evidence (LEFT)
python -m driftzero.cli verify --workflow-id $WF_ID --image fixtures/label_left.jpg

# 4. Confirm FAIL
python -m driftzero.cli status --workflow-id $WF_ID  # VERIFICATION_FAILED

# 5. Submit CORRECT field evidence (TOP_RIGHT)
python -m driftzero.cli verify --workflow-id $WF_ID --image fixtures/label_top_right.jpg

# 6. Confirm PASS and PROOF_COMPLETE
python -m driftzero.cli status --workflow-id $WF_ID  # PROOF_COMPLETE

# 7. Retrieve and validate Change Proof
python -m driftzero.cli proof --workflow-id $WF_ID --validate
```

### VS-3: Duplicate Event Idempotency
**Proves**: SC-010
```bash
python -m driftzero.cli inject-change --fixture fixtures/hero_change.json
python -m driftzero.cli inject-change --fixture fixtures/hero_change.json
# Second injection produces no duplicate workflow or evidence
```

### VS-4: Supersession
**Proves**: SC-015
```bash
# Inject v1 change, progress to AWAITING_FIELD_VERIFICATION
# Inject v2 change for same requirement
# v1 workflow → SUPERSEDED
# v2 workflow → CHANGE_RECEIVED (new workflow)
```

### VS-5: Gemma 4 Multimodal Evaluation
**Proves**: SC-006, SC-007 (deterministic verification layer)
```bash
pytest tests/multimodal/ -v --fixtures fixtures/multimodal/
```
Fixture set:
- `label_left_01.jpg` → expected observation: `LEFT`
- `label_top_right_01.jpg` → expected observation: `TOP_RIGHT`
- `label_ambiguous_01.jpg` → expected observation: `INCONCLUSIVE`

### VS-6: Security — Prompt Injection Resistance
**Proves**: Model Armor / FR-011
```bash
pytest tests/security/test_prompt_injection.py -v
```
Adversarial fixture: downstream artifact containing embedded prompt injection text attempting to override agent instructions.

### VS-7: Cloud Deployment Smoke Test
```bash
# Deploy to Cloud Run
adk deploy cloud_run --project $PROJECT_ID --region $REGION

# Trigger via Pub/Sub
gcloud pubsub topics publish driftzero-approved-changes --message-file fixtures/hero_change.json

# Verify workflow completed in Firestore
python -m driftzero.cli status --workflow-id $WF_ID --remote
```

## Evidence Pack Structure

```
evidence/
├── README.md                    # Judge entry point
├── JUDGES_START_HERE.md         # Quick demo walkthrough
├── LIMITATIONS.md               # Honest known limitations
├── MANIFEST.json                # Evidence index with hashes
├── raw/                         # Unprocessed evidence
│   ├── approved_change.json     # Source change fixture
│   ├── stale_artifact_before.json
│   └── field_images/
├── fixtures/                    # Reproducible test fixtures
│   ├── hero_change.json
│   ├── multimodal/
│   └── security/
├── runs/                        # Recorded execution runs
│   └── hero_run_001/
│       ├── state_transitions.json
│       ├── agent_traces.json
│       └── change_proof.json
├── reports/                     # Test reports
│   ├── unit_test_report.xml
│   └── multimodal_eval.json
├── security/                    # Security evidence
│   └── prompt_injection_blocked.json
└── replays/                     # Reproducible replay data
```
