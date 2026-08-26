# Verifying a DRIFTZERO Change Proof

A Change Proof carries a `content_hash`. This document is the complete, dependency-free
recipe for reproducing it from a downloaded proof file — no DRIFTZERO code required.

## The one thing that surprises people first

**`sha256` of the downloaded file does not equal `content_hash`, and must not.**

```
sha256(change_proof_DZ-001.json)  75925f5ecb14d1cfcd1eeee0c3e8f17a8e7d274131e2eb4bc4118a2b91a80af1
proof.content_hash                5c66dd80ca882602c7a263cdb6435c66b4462cbc0c24d43ac542511ca95a0c5e
```

The downloaded file is the **complete** proof, `content_hash` included. The digest is
taken over the proof **without** that field. A field cannot contain the hash of a
document that contains that field, so the exclusion is arithmetic rather than a choice.

For the proof above the difference is 82 bytes — exactly the `"content_hash": "…"`
member. Running `Get-FileHash` or `sha256sum` on the whole file is therefore expected to
disagree, and disagreement there is **not** evidence of tampering.

## What is hashed

`content_hash` is SHA-256 over **DRIFTZERO canonical JSON** of the proof document with
its `content_hash` key removed.

DRIFTZERO canonical JSON, exactly as implemented in
`src/driftzero/truth_engine/evidence.py`:

- keys sorted (`sort_keys=True`)
- no insignificant whitespace (`separators=(",", ":")`)
- non-ASCII emitted literally (`ensure_ascii=False`)
- encoded UTF-8 before hashing

This is *a* canonical JSON, not RFC 8785. It is deliberately not described as JCS,
because the implementation does not attempt full RFC 8785 conformance — notably number
normalisation and Unicode escaping rules differ. Reproduce it with the rules above.

## Reference algorithm

```python
import json, hashlib

with open("change_proof_DZ-001.json", encoding="utf-8") as handle:
    doc = json.load(handle)

expected = doc["content_hash"]

material = {k: v for k, v in doc.items() if k != "content_hash"}
canonical = json.dumps(
    material,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
)
actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

assert actual == expected, f"proof hash mismatch: {actual} != {expected}"
print("Change Proof verified:", expected)
```

Standard library only. It runs against the downloaded bytes alone.

### PowerShell

`ConvertTo-Json` does not sort keys and does not match the separator rules, so it cannot
reproduce the canonical form. Use the Python snippet above, or any encoder you have
configured to the four rules listed. `Get-FileHash` on the complete file will not match,
by design.

## What a match proves — and what it does not

A match establishes **content identity and alteration detection**: this document is
byte-identical in meaning to the one DRIFTZERO generated, and no field has been altered
since.

It is **not** a digital signature, an attestation, a trusted timestamp, a
non-repudiation mechanism, or a blockchain proof. Anyone able to alter the proof could
recompute the hash. The hash tells you the document is unchanged; it tells you nothing
about who produced it.

## Response headers

`GET /api/hero/proof/download` returns the stored bytes with:

| Header | Meaning |
|---|---|
| `X-Proof-Content-Hash` | the proof's `content_hash` |
| `X-Proof-Hash-Preimage` | `canonical-json-excluding-content_hash` |

The preimage header exists so the difference above is discoverable from the response
itself, without reading this file first.
