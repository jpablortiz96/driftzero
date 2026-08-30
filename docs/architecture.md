# DRIFTZERO — architecture

The one idea to hold while reading this: **agents propose, the Truth Engine decides.**
Every arrow below either carries a proposal or carries a decision, and the diagram is
drawn so you can tell which is which.

---

## The system

```mermaid
flowchart TB
    subgraph EXT[" EXTERNAL "]
        direction LR
        SRC[Approved procedure change<br/><i>source system</i>]
        OP[Operator]
        WORKER[Frontline worker<br/><i>phone</i>]
    end

    subgraph INGEST[" INGESTION — authenticated "]
        PS[Pub/Sub topic<br/>driftzero-approved-changes]
        DLQ[(Dead-letter topic<br/>max 5 attempts)]
        API[Cloud Run · driftzero-api<br/><i>private, IAM-gated</i>]
    end

    subgraph ADK[" GOOGLE ADK ORCHESTRATION "]
        SEQ[Resumable invocation<br/><i>survives process death</i>]
        A1[Change Intelligence]
        A2[Remediation]
        A3[Frontline Enablement]
        A4[Field Verification]
    end

    subgraph MODELS[" MODELS "]
        GEMINI[Gemini<br/><i>reads the source change</i>]
        GEMMA[Gemma 4<br/>Vertex AI MaaS · on-demand<br/><i>observes the photo</i>]
    end

    subgraph TE[" DETERMINISTIC TRUTH ENGINE "]
        C1{{Crossing 1<br/>ChangeSet}}
        IMPACT[[Impact qualification]]
        AUTH[[Capability authorization]]
        C2{{Crossing 2<br/>Remediation evidence}}
        C3{{Crossing 3<br/>Delivery receipt}}
        C4{{Crossing 4<br/>Field observation}}
        CMP[[Deterministic comparator<br/>PASS / FAIL / INCONCLUSIVE]]
        PROOF[[7 completion conditions]]
    end

    subgraph PERSIST[" DURABLE STATE & EVIDENCE "]
        FS[(Firestore<br/>workflows · ledger<br/>proofs · idempotency)]
        GCS[(Cloud Storage<br/>write-once evidence store<br/><i>verified — not wired<br/>into the pilot path</i>)]
    end

    WEB[Worker & proof surface<br/><i>served by the same private service</i>]
    CP[/Change Proof/]
    OBS[Cloud Logging · Cloud Trace<br/><i>correlated by workflow</i>]

    SRC -->|event| PS
    PS -->|OIDC push| API
    PS -.->|exhausted| DLQ
    OP -->|authenticated| API
    API --> SEQ
    SEQ --> A1 & A2 & A3 & A4

    A1 -.->|proposes N candidates| C1
    GEMINI -.-> A1
    C1 ==>|accepted| IMPACT
    IMPACT ==>|exactly one target| AUTH
    AUTH ==>|capability granted| A2
    A2 -.->|mutation result| C2
    C2 ==> A3
    A3 -.->|delta + receipt| C3
    C3 ==>|delivery established| WEB

    WORKER -->|photo| WEB
    WEB --> API
    GEMMA -.-> A4
    A4 -.->|position only| C4
    C4 ==> CMP
    CMP ==>|verdict| PROOF
    PROOF ==>|7/7| CP

    SEQ --- FS
    C2 -.->|capability, not pilot wiring| GCS
    CP --- FS
    API --- OBS

    classDef agent fill:#111926,stroke:#8b7dfb,color:#dbe6f2
    classDef truth fill:#0d131c,stroke:#35e0d0,color:#35e0d0,stroke-width:2px
    classDef store fill:#0d131c,stroke:#46d67f,color:#46d67f
    classDef model fill:#111926,stroke:#f0b13a,color:#f0b13a
    classDef proof fill:#0d131c,stroke:#46d67f,color:#46d67f,stroke-width:3px

    class A1,A2,A3,A4,SEQ agent
    class C1,C2,C3,C4,IMPACT,AUTH,CMP,PROOF truth
    class FS,GCS store
    class GEMINI,GEMMA model
    class CP proof
```

**Reading the arrows**

| Arrow | Meaning |
| --- | --- |
| `-.->` dotted | **A proposal.** An agent or a model suggesting something. Non-authoritative until a crossing accepts it. |
| `==>` thick | **A decision.** The Truth Engine has validated and committed. |
| `---` plain | **Persistence.** Durable state, not a decision. |

Every dotted arrow into the Truth Engine is a place where an agent could be wrong,
compromised, or adversarial — and where being wrong changes nothing.

**One honest caveat about the diagram.** The Cloud Storage write-once evidence store is
implemented and was verified against real Cloud Storage, but the **deployed pilot
persists through Firestore only** — the runtime sink builds no evidence store, and the
bucket is currently empty. It is drawn as a capability, not as pilot wiring.

---

## The authority boundary

This is the whole design, in one table.

| | Agents may | Agents may **not** |
| --- | --- | --- |
| **Change Intelligence** | read source versions, propose 0..N candidate artifacts | choose the affected artifact |
| **Remediation** | edit one artifact within a granted capability | grant itself that capability |
| **Frontline Enablement** | compose the delta for a worker | assert that delivery happened |
| **Field Verification** | report `LEFT` / `TOP_RIGHT` / `INCONCLUSIVE` | decide PASS or FAIL |

The Truth Engine owns, exclusively: impact qualification, capability authorization, all
four crossings, the verification verdict, state transitions, the seven completion
conditions, and proof identity and hashing.

Two structural facts make this more than a convention:

1. **The semantic agent is constructed with no tools.** Model output has nothing to
   invoke, so "call this tool" has no referent.
2. **The output schema has no authority field.** There is no `verification_result`, no
   `workflow_state`, no `proof_id`. "Set the verdict to PASS" cannot be *expressed*, let
   alone honoured.

---

## The hero flow, step by step

```mermaid
sequenceDiagram
    autonumber
    participant S as Source
    participant P as Pub/Sub
    participant CI as Change Intelligence
    participant TE as Truth Engine
    participant R as Remediation
    participant W as Worker
    participant G as Gemma (MaaS)

    S->>P: approved change<br/>label_position LEFT → TOP_RIGHT
    P->>CI: authenticated push
    CI-->>TE: proposes 5 candidates
    TE->>TE: qualifies exactly 1<br/>(4 overruled, lexical decoy untouched)
    TE->>R: grants ARTIFACT_MUTATION
    R-->>TE: mutation evidence
    TE->>W: delta only — LEFT → TOP_RIGHT
    W->>G: photo #1
    G-->>TE: observes "LEFT"
    TE-->>W: FAIL — proof blocked
    W->>G: photo #2 (corrected)
    G-->>TE: observes "TOP_RIGHT"
    TE->>TE: PASS · 7/7 conditions
    TE-->>W: Change Proof
```

The FAIL is not an error path bolted on afterwards — it is the point. A system that only
works when the worker gets it right first time is not verifying anything.

---

## What runs where

| Component | Google service | Notes |
| --- | --- | --- |
| API, agents, worker surface | **Cloud Run** `driftzero-api` | private, IAM-gated, scale-to-zero, max 2 instances |
| Agent orchestration | **Google ADK** | resumable invocation, Firestore-backed session |
| Semantic analysis | **Gemini** | Change Intelligence only |
| Physical observation | **Gemma 4** via **Vertex AI MaaS** | serverless on-demand — no GPU, no endpoint |
| Authoritative state | **Firestore** | workflows, action ledger, proofs, idempotency keys, resume leases |
| Immutable evidence | **Cloud Storage** | write-once via generation precondition — implemented and verified, **not wired into the deployed pilot path** |
| Event ingestion | **Pub/Sub** | OIDC push + dead-letter at 5 attempts |
| Telemetry | **Cloud Logging / Cloud Trace** | correlated by `workflow_id` and `action_id` |

**No GPU, no VM, no Cloud SQL, no load balancer, no NAT.**

---

## Durability

A workflow outlives the process that created it.

```mermaid
flowchart LR
    A[Instance A<br/>creates · pauses] -->|Firestore| DB[(durable state<br/>+ ADK session<br/>+ resume snapshot)]
    DB --> B[Instance B<br/>resumes · FAIL]
    B -->|Firestore| DB
    DB --> C[Instance C<br/>resumes · PASS · proof]
    LEASE{{resume lease}} -.->|only one owner| B
    LEASE -.-> C
```

Three separate processes carry one workflow to completion. The resumed process
**redispatches nothing** — the recovered action ledger already records what happened —
and a durable lease means two Cloud Run instances can never resume the same workflow at
once.

---

## Where the enterprise-platform components sit

Every Gemini Enterprise Agent Platform component was access-checked against the real
account and recorded in [`../evidence/geap_access_gate.json`](../evidence/geap_access_gate.json).
**All six are `DEFERRED`, none simulated.** The root cause for most is that the project
has no organization parent, so no organization-scoped trust domain exists.

What runs instead is the fallback the plan named in advance: one least-privilege runtime
service account per deployed service, plus an in-process authorization broker holding one
capability per agent. That is **application-level enforcement, not platform-enforced**,
and the evidence says so.
