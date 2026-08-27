"""T081 — the DRIFTZERO command-line interface.

A transport, and nothing else. Every command is an HTTP call to a running LOCAL_PILOT
runtime, which owns the workflow and runs the real T080 orchestration. This module
computes no impact, no authorization, no verdict, and no proof; a structural test asserts
it names none of that machinery.

Why HTTP
--------
The documented sequence is a series of separate OS processes sharing one workflow id::

    python -m driftzero.cli inject-change --fixture fixtures/hero_change.json
    python -m driftzero.cli status  --workflow-id $WF_ID
    python -m driftzero.cli verify  --workflow-id $WF_ID --image <photo>
    python -m driftzero.cli proof   --workflow-id $WF_ID --validate

Separate processes cannot share memory, so the state lives in one long-lived runtime the
CLI talks to. That state is **process-local to the server**: it survives across CLI
invocations while the runtime is up, and is lost when it restarts. Durable persistence is
T092 (M2), and nothing here claims otherwise.

Import discipline
-----------------
``src/driftzero`` is inside the M0 purity boundary, whose guard asserts the package's
entire third-party surface is pydantic. This module therefore uses the standard library
only and reaches the console over HTTP rather than importing it. That constraint is also
what keeps the CLI honest: it *cannot* accidentally reach past the API into domain
internals.

Output is JSON on stdout, diagnostics on stderr, ``0`` on success and non-zero on
failure — so a shell can drive it.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_API_BASE = "http://127.0.0.1:8080"
API_BASE_ENV = "DRIFTZERO_API_BASE"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
"""Three outcomes, all tested. Not a private vocabulary of numeric codes."""

REQUEST_TIMEOUT_SECONDS = 300.0
"""Generous: ``inject-change`` may drive a live semantic model on a real runtime."""

RUNTIME_HINT = (
    "Is the DRIFTZERO runtime running?  python -m driftzero_console.app\n"
    "Workflow state is process-local to that runtime and is lost when it restarts."
)


class CliError(Exception):
    """A command failed. Carries the message and the exit code to use."""

    def __init__(self, message: str, *, code: int = EXIT_FAILURE) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


def api_base(args: argparse.Namespace) -> str:
    """Resolve the runtime address: ``--api-base``, then the env var, then the default."""
    base = getattr(args, "api_base", None) or os.environ.get(API_BASE_ENV) or DEFAULT_API_BASE
    return base.rstrip("/")


def request_json(
    method: str,
    url: str,
    *,
    payload: Any = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """One JSON round-trip. Raises :class:`CliError` with the server's own detail.

    The server's error text is surfaced verbatim rather than replaced by a generic
    message: "no workflow 'wf-x' in this runtime" tells an operator far more than
    "request failed".
    """
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except (json.JSONDecodeError, AttributeError):
            pass
        raise CliError(f"{method} {url} -> {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise CliError(f"cannot reach the runtime at {url}: {exc.reason}\n{RUNTIME_HINT}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"the runtime returned non-JSON: {exc}") from exc


def read_json_file(path: Path, *, label: str) -> Any:
    """Read a JSON document from disk, failing closed on anything unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"cannot read {label} {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CliError(f"{label} {path} is not valid JSON: {exc}") from exc


# ============================ commands ================================================


def cmd_inject_change(args: argparse.Namespace) -> Any:
    """Submit a source-change fixture and run the workflow to its field-evidence pause.

    The fixture is read here and sent verbatim; the runtime validates it, derives the
    change from the source documents, and decides everything. This command names no
    artifact, no target, and no outcome.
    """
    fixture_path = Path(args.fixture).expanduser()
    payload = read_json_file(fixture_path, label="fixture")
    directory = fixture_path.parent

    headers = {}
    try:
        relative = directory.resolve().relative_to(Path.cwd().resolve())
        headers["X-Fixture-Dir"] = relative.as_posix()
    except ValueError:
        # Outside the working tree: let the runtime use its own default location.
        pass

    return request_json(
        "POST", f"{api_base(args)}/api/cli/workflows", payload=payload, headers=headers
    )


def cmd_status(args: argparse.Namespace) -> Any:
    """Project one workflow's authoritative state. Read-only."""
    return request_json(
        "GET", f"{api_base(args)}/api/cli/workflows/{args.workflow_id}"
    )


def cmd_verify(args: argparse.Namespace) -> Any:
    """Submit physical field evidence and resume the paused workflow.

    The image bytes are the payload. ``filename`` and ``declared_content_type`` travel as
    claims only — the runtime sniffs the real container from the bytes, so a mislabelled
    file (an iPhone HEIC named ``.jpg``) is handled correctly and a lying one changes
    nothing.

    There is deliberately no way to state an expected value or a verdict: the expected
    value comes from approved state, and the observation comes from the validated
    observation boundary.
    """
    image_path = Path(args.image).expanduser()
    try:
        raw = image_path.read_bytes()
    except OSError as exc:
        raise CliError(f"cannot read image {image_path}: {exc}") from exc
    if not raw:
        raise CliError(f"image {image_path} is empty")

    envelope = {
        "filename": image_path.name,
        "declared_content_type": _declared_content_type(image_path),
        "content_base64": base64.b64encode(raw).decode("ascii"),
    }
    return request_json(
        "POST",
        f"{api_base(args)}/api/cli/workflows/{args.workflow_id}/verify",
        payload=envelope,
    )


def cmd_proof(args: argparse.Namespace) -> Any:
    """Validate a workflow's Change Proof through the runtime's authoritative validator."""
    if not args.validate:
        raise CliError("proof requires --validate", code=EXIT_USAGE)
    return request_json(
        "POST",
        f"{api_base(args)}/api/cli/workflows/{args.workflow_id}/proof/validate",
    )


def _declared_content_type(path: Path) -> str:
    """A guess from the extension, sent as a claim the server is expected to ignore."""
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }.get(path.suffix.lower(), "application/octet-stream")


# ============================ argument parsing ========================================


def build_parser() -> argparse.ArgumentParser:
    """The exact T081 command surface. Four commands, no more."""
    parser = argparse.ArgumentParser(
        prog="driftzero.cli",
        description=(
            "DRIFTZERO command-line client. Talks to a running LOCAL_PILOT runtime "
            "(python -m driftzero_console.app); workflow state lives in that process "
            "and is lost when it restarts."
        ),
    )
    parser.add_argument(
        "--api-base",
        default=None,
        help=f"runtime address (default: ${API_BASE_ENV} or {DEFAULT_API_BASE})",
    )
    parser.add_argument(
        "--pretty", action="store_true", help="indent the JSON result for reading"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inject = subparsers.add_parser(
        "inject-change", help="submit a source change and run it to the evidence pause"
    )
    inject.add_argument("--fixture", required=True, help="path to a source-change fixture")
    inject.set_defaults(handler=cmd_inject_change)

    status = subparsers.add_parser("status", help="show one workflow's authoritative state")
    status.add_argument("--workflow-id", required=True)
    status.set_defaults(handler=cmd_status)

    verify = subparsers.add_parser(
        "verify", help="submit physical field evidence for a paused workflow"
    )
    verify.add_argument("--workflow-id", required=True)
    verify.add_argument("--image", required=True, help="path to the field photograph")
    verify.set_defaults(handler=cmd_verify)

    proof = subparsers.add_parser("proof", help="validate a workflow's Change Proof")
    proof.add_argument("--workflow-id", required=True)
    proof.add_argument(
        "--validate", action="store_true", help="run authoritative proof validation"
    )
    proof.set_defaults(handler=cmd_proof)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one command. JSON to stdout, diagnostics to stderr, deterministic exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse already printed usage or help and exited. ``--help`` exits 0 and must
        # stay 0; `exc.code or EXIT_USAGE` would turn that falsy zero into a failure.
        if exc.code is None:
            return EXIT_USAGE
        return int(exc.code)

    try:
        result = args.handler(args)
    except CliError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code

    indent = 2 if getattr(args, "pretty", False) else None
    print(json.dumps(result, indent=indent, sort_keys=True))

    # A command that ran but reported an unfavourable result still exits non-zero, so a
    # shell can branch on it without parsing the payload.
    if isinstance(result, dict) and result.get("authoritative_validation") == "INVALID":
        return EXIT_FAILURE
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised by running the CLI
    raise SystemExit(main())
