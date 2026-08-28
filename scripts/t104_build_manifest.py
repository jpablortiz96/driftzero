"""T104 — build the versioned multimodal evaluation manifest.

Every value is derived from the files themselves, never copied from prose. The expected
observation is the one field that cannot be derived — it is what a human saw when the
photograph was taken — so it is read from the T064 provenance record, which is where
that judgement was made and evidenced.

The MIME type is sniffed from the actual bytes, not the extension. These fixtures are
HEIC containers carrying ``.jpg`` names, and a manifest that repeated the extension
would be recording the filename's claim rather than the file's content.

Run:  python -m scripts.t104_build_manifest
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.media.container import sniff_container, sniff_mime_type  # noqa: E402
from driftzero.models.verification import ObservedPosition  # noqa: E402

FIXTURES = REPO_ROOT / "fixtures" / "multimodal"
MANIFEST = FIXTURES / "manifest.json"

MANIFEST_SCHEMA = "driftzero.m3.multimodal_manifest.v1"


def build() -> dict[str, object]:
    provenance = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))
    recorded = provenance["fixtures"]

    entries = []
    for path in sorted(FIXTURES.glob("*.jpg")):
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        record = recorded.get(path.name)
        if record is None:
            raise SystemExit(f"{path.name} has no T064 provenance record")
        if record.get("sha256") != digest:
            raise SystemExit(
                f"{path.name} does not match its recorded hash: "
                f"provenance {record.get('sha256')}, actual {digest}"
            )

        expected = record["expected_observation"]
        # The expected value must be a member of the frozen observation domain. A
        # manifest that could name a fourth value would be a second, softer domain.
        ObservedPosition(expected)

        entries.append(
            {
                "filename": path.name,
                "expected_observation": expected,
                "sha256": digest,
                "size_bytes": len(raw),
                "declared_extension": path.suffix,
                "actual_mime_type": sniff_mime_type(raw),
                "actual_container": str(sniff_container(raw)),
                "extension_matches_content": sniff_mime_type(raw) == "image/jpeg",
                "capture_method": record.get("capture_method"),
                "provenance_class": "REAL_PHYSICAL",
            }
        )

    return {
        "schema": MANIFEST_SCHEMA,
        "task": "T104",
        "directory_role": provenance["directory_role"],
        "observation_domain": [str(v) for v in ObservedPosition],
        "domain_note": (
            "The closed domain the comparator adjudicates against. An observation "
            "outside it is rejected, never coerced to the nearest member."
        ),
        "mime_note": (
            "actual_mime_type is sniffed from the file's own bytes. These fixtures are "
            "HEIC containers named .jpg, so the extension is a claim and the bytes are "
            "the authority — the same rule the ingestion path applies at Crossing 4."
        ),
        "expected_observation_source": (
            "fixtures/multimodal/provenance.json (T064). What a human saw when the "
            "photograph was taken cannot be derived from the file, so it is read from "
            "the record where that judgement was made."
        ),
        "physical_capture_satisfied": provenance["physical_capture_satisfied"],
        "fixture_count": len(entries),
        "fixtures": entries,
        "synthetic_directory": {
            "path": "fixtures/multimodal/synthetic/",
            "role": "GENERATED — never evaluated as real physical evidence",
            "excluded_from_this_manifest": True,
        },
    }


def main() -> int:
    manifest = build()
    # newline="\n": a CRLF file cannot be checked by sha256sum -c on POSIX.
    with MANIFEST.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"  wrote {MANIFEST.relative_to(REPO_ROOT).as_posix()}", file=sys.stderr)
    for entry in manifest["fixtures"]:  # type: ignore[index]
        print(
            f"  {entry['filename']:<24} expected={entry['expected_observation']:<13} "
            f"actual_mime={entry['actual_mime_type']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
