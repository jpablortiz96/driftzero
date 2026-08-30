# DRIFTZERO — video storyboard

> **Unverified input:** the official video length is **not recorded anywhere in this
> repository**. This storyboard is cut for **3:00** because that is the most common
> hackathon limit, and every beat carries a cut priority so it can be trimmed to 2:00 or
> extended to 4:00 without rewriting. **Confirm the real limit before recording.**

**Rule for the whole edit:** never show a result before the backend produced it. Waiting
time may be cut. Chronology may not be reordered.

**Vocabulary:** Change Intelligence, Remediation, Frontline Enablement, Field
Verification, Truth Engine, Change Proof. No task numbers, no milestones, no test counts.

---

## The cut

| # | Time | Beat | On screen | Voiceover | Cut priority |
|---|---|---|---|---|---|
| 1 | 0:00–0:18 | **The problem** | Split: a document diff `label_position: LEFT → TOP_RIGHT` beside a photo of a real box with the label still on the **left** | "A packing procedure changed. The document says the label moves to the top right. The box says otherwise. Every enterprise calls this deployed." | **KEEP** |
| 2 | 0:18–0:30 | **The thesis** | Full-bleed text, then the five-stage chain | "A process change isn't deployed when the document changes. It's deployed when the work changes. DRIFTZERO closes that last mile — and proves it." | **KEEP** |
| 3 | 0:30–1:00 | **Change Intelligence** | Two source versions → Gemini call → **5 candidates** → exactly **1** qualified. Hold on the forklift instruction that also says "LEFT", untouched. | "Gemini reads the change and proposes five work instructions that might be affected. The Truth Engine qualifies exactly one — and overrules the other four. This instruction also contains the word LEFT. It's a forklift turn direction. Nothing touches it." | **KEEP** — this is the differentiator |
| 4 | 1:00–1:20 | **Authorized remediation + delta** | Capability granted → artifact mutates `LEFT → TOP_RIGHT` → decoy unchanged → worker's phone shows only the delta | "Remediation holds one capability and edits one artifact. The worker doesn't get a new manual. They get the delta." | TRIM to 12 s if needed |
| 5 | 1:20–2:05 | **Frontline hero** ⭐ | Phone. Photo #1 (label left) → "Checking your photo" → **Not done yet** → retry affordance → photo #2 (top right) → **Verified** | "The worker photographs the finished work. Gemma observes where the label is — LEFT. The Truth Engine compares that to the expected value and returns FAIL. Not the model. The worker corrects it, photographs again. TOP_RIGHT. Now it passes." | **KEEP — never cut. This is the climax.** |
| 6 | 2:05–2:30 | **Change Proof** | Proof page: `7/7`, `PROOF_COMPLETE`, chronology **[FAIL, PASS]**, proof id, content hash, then "Verify integrity" recomputing live | "Seven completion conditions. All seven hold, so a Change Proof exists. Both attempts are kept — the failure is part of the record. The hash establishes content identity and detects alteration. It is not a signature and not a ledger entry." | **KEEP** — hash wording is non-negotiable |
| 7 | 2:30–2:50 | **Real cloud** | Architecture diagram, animated along the path the run just took | "This ran on Cloud Run, private. Firestore holds authoritative state, Cloud Storage holds immutable evidence, Pub/Sub delivers the change authenticated with a dead-letter path. The workflow survives the process that started it — a new instance recovers it and resumes the same execution." | TRIM to 12 s if needed |
| 8 | 2:50–3:00 | **Why it matters** | Back to the worker photo, then the proof | "DRIFTZERO doesn't ask whether the SOP changed. It proves whether the work changed." | **KEEP** |

**If the limit is 2:00** — drop beat 7 to a 5-second diagram hold and compress beat 4
into beat 3. Beats 1, 2, 3, 5, 6, 8 are the film.

**If the limit is 4:00+** — expand beat 7 with the restart/resume sequence, and add 15 s
on the authority boundary table (agents propose / Truth Engine decides).

---

## Two lines that must be said exactly

Both are truth-discipline commitments, not stylistic choices:

1. *"The Truth Engine compares that to the expected value and returns FAIL. **Not the
   model.**"*
2. *"The hash establishes content identity and detects alteration. **It is not a
   signature and not a ledger entry.**"*

## Words that must not appear

`production ready` · `cryptographically signed` · `attested` · `non-repudiation` ·
`blockchain` · `Agent Identity deployed` · `Gateway enforcing` · `Model Armor screening` ·
`Veo` · any task id or milestone name.

---

## Shot sources

Every visual already exists — nothing needs to be staged.

| Beat | Source |
| --- | --- |
| 1, 5 | `fixtures/multimodal/label_left_01.jpg`, `label_top_right_01.jpg` (real photographs) |
| 3 | live run, or `evidence/pilot_live_change_intel_2026_08_26/change_intelligence.json` |
| 4, 5 | the deployed worker surface — `/web/delta`, `/web/verify` |
| 6 | the deployed proof surface — `/web/proof` |
| 7 | `docs/architecture.md` |
| fallback for 5–6 | `evidence/m6/worker_failed.png`, `worker_verified.png`, `proof_view.png` |
