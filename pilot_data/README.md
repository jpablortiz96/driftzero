# DRIFTZERO pilot operational data

Real operational records for DRIFTZERO's **first internal pilot**. These describe our own
packing procedure and the work instructions that implement it. They are not a customer's
data, and they are not a mock: the application loads them as the source of truth for the
pilot, exactly as it would load records from a document system.

- `source_procedures/` — versioned approved source procedures. Each version is a complete
  requirement set, so a change is *derived* by comparing two versions rather than being
  asserted by hand.
- `artifact_catalog.json` — the downstream work instructions this deployment knows about.
- `approved_changes.json` — approvals recorded against a version transition.

The catalog deliberately contains artifacts that look relevant and are not. Discovering
which one is actually affected is the work; a catalog with one obvious answer would prove
nothing.
