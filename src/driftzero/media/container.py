"""T079 — identify an image container from its own bytes.

Neither a filename nor a browser-supplied ``Content-Type`` is evidence of anything. Both
are attacker-controlled in the general case, and both are simply *wrong* in the ordinary
case this product hits first: the real iPhone field fixtures are HEIC bytes carrying a
``.jpg`` extension. A pipeline that trusted either would send a mislabelled payload to
the model and record a false MIME type in the evidence.

So the container is derived from the leading bytes and nothing else.

Scope
-----
Container identification **only**. This module deliberately does not parse EXIF, C2PA,
JUMBF, PNG text chunks, or any other provenance structure. The G1 probe needs those to
adjudicate generated media; a MIME sniffer does not, and duplicating a provenance parser
into the request path would add attack surface for no benefit.

Relationship to G1
------------------
The magic-byte rules here are the ones G1 exercised against the real physical fixtures
(``ftyp`` + brand for ISO-BMFF, ``\x89PNG``, ``\xff\xd8\xff``). ``scripts/`` is frozen
G1 evidence tooling and keeps its own copy; production must not import from it, so the
rules live here and a test pins both to the same answer on the same real bytes.
"""

from __future__ import annotations

from enum import StrEnum

ISO_BMFF_MARKER = b"ftyp"
"""Bytes 4..8 of an ISO base media file. The box length precedes it."""

MIN_SNIFFABLE_BYTES = 12
"""Below this nothing can be identified, so nothing is guessed."""


class ContainerFormat(StrEnum):
    """The containers this product recognizes. ``UNKNOWN`` is a rejection, not a guess."""

    JPEG = "JPEG"
    PNG = "PNG"
    HEIC = "HEIC"
    HEIF = "HEIF"
    AVIF = "AVIF"
    UNKNOWN = "UNKNOWN"


MIME_BY_CONTAINER: dict[ContainerFormat, str] = {
    ContainerFormat.JPEG: "image/jpeg",
    ContainerFormat.PNG: "image/png",
    ContainerFormat.HEIC: "image/heic",
    ContainerFormat.HEIF: "image/heif",
    ContainerFormat.AVIF: "image/avif",
}
"""Authoritative MIME per container. ``UNKNOWN`` is absent on purpose."""

_ISO_BRANDS: dict[bytes, ContainerFormat] = {
    # HEVC-coded HEIF — what an iPhone actually writes.
    b"heic": ContainerFormat.HEIC,
    b"heix": ContainerFormat.HEIC,
    b"heim": ContainerFormat.HEIC,
    b"heis": ContainerFormat.HEIC,
    b"hevc": ContainerFormat.HEIC,
    b"hevx": ContainerFormat.HEIC,
    b"hevm": ContainerFormat.HEIC,
    b"hevs": ContainerFormat.HEIC,
    # Generic HEIF image / image sequence.
    b"mif1": ContainerFormat.HEIF,
    b"msf1": ContainerFormat.HEIF,
    # AV1-coded.
    b"avif": ContainerFormat.AVIF,
    b"avis": ContainerFormat.AVIF,
}


def sniff_container(raw: bytes) -> ContainerFormat:
    """Identify the container from ``raw``'s own leading bytes.

    Returns :attr:`ContainerFormat.UNKNOWN` for anything not positively recognized —
    including empty input, truncated input, and text that merely looks image-adjacent.
    Failing closed is the point: an unrecognized container must be refused, never
    forwarded under an assumed type.
    """
    if len(raw) < MIN_SNIFFABLE_BYTES:
        return ContainerFormat.UNKNOWN
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ContainerFormat.PNG
    if raw[:3] == b"\xff\xd8\xff":
        return ContainerFormat.JPEG
    if raw[4:8] == ISO_BMFF_MARKER:
        return _ISO_BRANDS.get(raw[8:12].lower(), ContainerFormat.UNKNOWN)
    return ContainerFormat.UNKNOWN


def sniff_mime_type(raw: bytes) -> str | None:
    """The authoritative MIME type for ``raw``, or ``None`` when unrecognized."""
    return MIME_BY_CONTAINER.get(sniff_container(raw))
