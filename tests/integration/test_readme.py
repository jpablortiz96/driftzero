"""The root README is judge-facing, so its links and claims are enforced, not trusted.

Three things can rot silently in a README: a relative link can point at a file someone
renamed, an image can point somewhere GitHub cannot render, and a claim can outlive the
thing it described. Each has its own test here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"

MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
HTML_IMAGE = re.compile(r"<img[^>]*>")
IMAGE_SRC = re.compile(r'<img[^>]+src="([^"]+)"')
HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)

# A path shape GitHub cannot resolve for a reader: absolute local paths, editor-internal
# schemes, or a host only the author's machine can reach.
UNRENDERABLE = re.compile(r"^[A-Za-z]:\\|^/|^file://|vscode-webview|localhost|127\.0\.0\.1")

# Claims the constitution forbids asserting, because they are not true of this system.
FORBIDDEN_CLAIMS = (
    "production ready",
    "production-ready",
    "model armor active",
    "model armor screening",
    "agent identity deployed",
    "agent registry enforcing",
    "gateway deployed",
    "gateway enforcing",
    "gpu deployed",
    "gpu provisioned",
    "veo",
    "blockchain",
    "tamper-proof",
    "non-repudiation",
    "digital signature",
    "trusted timestamp",
)

# A sentence that *denies* a forbidden property is exactly what we want the README to
# contain, so a denial disqualifies the sentence from being a violation.
DENIAL = re.compile(
    r"\b(no|not|never|cannot|without|is not|are not|does not|nor|must not|deferred"
    r"|unavailable|false|neither|rather than|instead of)\b",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def readme_text() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sentences(readme_text: str) -> list[str]:
    """README prose split into claim-sized units.

    Table cells carry claims as often as sentences do, so ``|`` splits alongside
    sentence punctuation. Whitespace is flattened first: a claim wrapped across two
    source lines is still one claim.
    """
    flat = re.sub(r"\s+", " ", readme_text)
    return [s for s in re.split(r"(?<=[.!?])\s+|\|", flat) if s.strip()]


def test_readme_exists_and_is_substantial(readme_text: str) -> None:
    assert README.is_file()
    assert len(readme_text) > 5_000, "the root README is the product narrative, not a stub"


def test_every_relative_link_resolves(readme_text: str) -> None:
    broken = []
    for _label, target in MARKDOWN_LINK.findall(readme_text):
        if target.startswith(("http://", "https://", "#")):
            continue
        if not (REPO_ROOT / target.split("#")[0]).exists():
            broken.append(target)
    assert not broken, f"README links point at missing paths: {broken}"


def test_every_anchor_link_matches_a_heading(readme_text: str) -> None:
    slugs = {
        re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")
        for heading in HEADING.findall(readme_text)
    }
    broken = [
        target
        for _label, target in MARKDOWN_LINK.findall(readme_text)
        if target.startswith("#") and target[1:] not in slugs
    ]
    assert not broken, f"README anchors match no heading: {broken}"


def test_every_image_resolves_and_renders_on_github(readme_text: str) -> None:
    missing, unrenderable = [], []
    for src in IMAGE_SRC.findall(readme_text) + [
        t for _, t in MARKDOWN_LINK.findall(readme_text) if t.lower().endswith((".png", ".jpg"))
    ]:
        if src.startswith("https://img.shields.io"):
            continue
        if UNRENDERABLE.search(src):
            unrenderable.append(src)
            continue
        if src.startswith("http"):
            continue
        if not (REPO_ROOT / src).exists():
            missing.append(src)
    assert not missing, f"README images point at missing files: {missing}"
    assert not unrenderable, f"README images use paths GitHub cannot render: {unrenderable}"


def test_every_image_has_alt_text(readme_text: str) -> None:
    """Alt text is an accessibility requirement, and it survives a broken image."""
    without = [tag for tag in HTML_IMAGE.findall(readme_text) if not re.search(r'alt="[^"]+"', tag)]
    assert not without, f"{len(without)} README image(s) carry no alt text"


def test_readme_asserts_no_forbidden_claim(sentences: list[str]) -> None:
    """A forbidden term is allowed only in a sentence that denies or qualifies it."""
    violations = [
        (claim, sentence.strip()[:160])
        for sentence in sentences
        for claim in FORBIDDEN_CLAIMS
        if claim in sentence.lower() and not DENIAL.search(sentence)
    ]
    assert not violations, f"README asserts claims that are not true: {violations}"


def test_forbidden_claim_detector_actually_fires() -> None:
    """Guard against the previous test passing because the detector stopped working."""
    asserted = "This system is production ready and uses blockchain attestation."
    denied = "It is not a digital signature and Model Armor is not active on this route."
    assert any(c in asserted.lower() for c in FORBIDDEN_CLAIMS)
    assert not DENIAL.search(asserted), "an asserting sentence must not read as a denial"
    assert DENIAL.search(denied), "a denying sentence must be recognised as such"


def test_cloud_storage_is_never_shown_as_active_pilot_persistence(sentences: list[str]) -> None:
    """The GCS adapter is verified but unwired; the README must not imply otherwise.

    Every sentence naming Cloud Storage has to carry the qualifier, so a future edit
    cannot quietly promote a capability into a deployed fact.
    """
    qualifier = re.compile(r"not wired|verified|adapter|write-once|integrity", re.IGNORECASE)
    unqualified = [
        sentence.strip()[:160]
        for sentence in sentences
        # A bare table cell is the label, not the claim; the claim sits in its neighbour.
        if re.search(r"cloud storage", sentence, re.IGNORECASE)
        and len(sentence.split()) > 4
        and not qualifier.search(sentence)
    ]
    assert not unqualified, f"Cloud Storage described without its status: {unqualified}"


def test_readme_carries_no_credential_shaped_material(readme_text: str) -> None:
    patterns = {
        "google api key": r"\bAIza[0-9A-Za-z_\-]{35}\b",
        "oauth token": r"\bya29\.[0-9A-Za-z_\-]{20,}",
        "jwt": r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}",
        "private key": r"BEGIN [A-Z ]*PRIVATE KEY",
        "bearer header": r"(?i)authorization:\s*bearer\s+\S{20,}",
        "billing account": r"\b0[0-9A-F]{5}-[0-9A-F]{6}-[0-9A-F]{6}\b",
    }
    found = [name for name, pattern in patterns.items() if re.search(pattern, readme_text)]
    assert not found, f"README contains credential-shaped material: {found}"


def test_quickstart_is_reproducible_without_cloud(readme_text: str) -> None:
    """The official submission requires spin-up instructions a judge can actually run."""
    for required in ("pip install -e", "python -m pytest", "python -m venv"):
        assert required in readme_text, f"README quick start is missing: {required}"


def test_readme_assets_are_byte_identical_to_their_evidence_originals() -> None:
    """README screenshots are copies, so a divergence means one of them was edited."""
    import hashlib

    pairs = {
        "docs/assets/driftzero-worker-delta.png": "evidence/m6/worker_mobile.png",
        "docs/assets/driftzero-worker-failed.png": "evidence/m6/worker_failed.png",
        "docs/assets/driftzero-worker-verified.png": "evidence/m6/worker_verified.png",
        "docs/assets/driftzero-change-proof.png": "evidence/m6/proof_view.png",
        "docs/assets/driftzero-proof-desktop.png": "evidence/m6/proof_desktop.png",
        "docs/assets/driftzero-desktop.png": "evidence/m6/desktop.png",
        "docs/assets/driftzero-photo-left.jpg": "fixtures/multimodal/label_left_01.jpg",
        "docs/assets/driftzero-photo-top-right.jpg": "fixtures/multimodal/label_top_right_01.jpg",
    }

    def digest(relative: str) -> str:
        return hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest()

    diverged = [
        f"{asset} != {original}"
        for asset, original in pairs.items()
        if digest(asset) != digest(original)
    ]
    assert not diverged, f"README assets diverged from their originals: {diverged}"
