"""T098 — correlation identity, structured records, and the Cloud export.

The pure half is tested without a cloud at all, which is the point of it being pure.
The export half is tested for shape and for failing soft: telemetry that can take the
request path down with it is worse than no telemetry.
"""

from __future__ import annotations

import ast
import dataclasses
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from driftzero.observability import (
    MAX_VALUE_CHARS,
    REDACTED,
    SENSITIVE_KEYS,
    CorrelationContext,
    Recorder,
    Severity,
    new_correlation_id,
    record_attempts,
    redact,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"


@pytest.fixture
def recorder() -> Recorder:
    return Recorder(
        context=CorrelationContext.create(
            workflow_id="wf-1", change_id="chg-1", instance_id="rev-abc"
        )
    )


# ============================ correlation identity ====================================


def test_a_correlation_id_is_generated_once_and_carried(recorder: Recorder) -> None:
    original = recorder.context.correlation_id
    narrowed = recorder.bind(action_id="act-1")
    assert narrowed.context.correlation_id == original, (
        "narrowing scope must not mint a new execution identity"
    )
    assert narrowed.context.action_id == "act-1"
    assert narrowed.context.workflow_id == "wf-1"


def test_correlation_ids_are_bound_to_the_domains_own_identifiers(
    recorder: Recorder,
) -> None:
    labels = recorder.bind(action_id="act-1", proof_id="prf-1").context.as_labels()
    assert labels["workflow_id"] == "wf-1"
    assert labels["action_id"] == "act-1"
    assert labels["proof_id"] == "prf-1"
    assert labels["change_id"] == "chg-1"


def test_an_unset_identifier_is_absent_not_empty(recorder: Recorder) -> None:
    """An empty string in a log label reads as 'known to be blank', which is a lie."""
    assert "action_id" not in recorder.context.as_labels()
    assert "proof_id" not in recorder.context.as_labels()


def test_a_context_is_frozen_so_binding_cannot_mutate_a_shared_one(
    recorder: Recorder,
) -> None:
    before = recorder.context
    recorder.bind(action_id="act-1")
    assert before.action_id is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        before.action_id = "act-2"  # type: ignore[misc]


def test_correlation_ids_are_unique() -> None:
    assert len({new_correlation_id() for _ in range(200)}) == 200


# ============================ record shape ============================================


def test_a_record_is_shaped_for_cloud_logging(recorder: Recorder) -> None:
    entry = recorder.record("impact.qualified", candidates=5, qualified=1).as_entry()
    assert entry["severity"] == "INFO"
    assert entry["message"] == "impact.qualified"
    assert entry["logging.googleapis.com/labels"]["workflow_id"] == "wf-1"
    assert entry["candidates"] == 5


def test_a_record_serialises_to_one_json_line(recorder: Recorder) -> None:
    line = recorder.record("delivery.dispatched").as_json()
    assert "\n" not in line
    assert json.loads(line)["event"] == "delivery.dispatched"


# ============================ redaction ===============================================


@pytest.mark.parametrize("key", sorted(SENSITIVE_KEYS))
def test_no_credential_shaped_value_is_ever_emitted(key: str) -> None:
    assert redact({key: "super-secret-value"})[key] == REDACTED


def test_redaction_is_case_and_separator_insensitive() -> None:
    for key in ("Authorization", "ACCESS_TOKEN", "api-key", "Client-Secret"):
        assert redact({key: "x"})[key] == REDACTED


def test_redaction_reaches_nested_values() -> None:
    cleaned = redact({"outer": {"headers": {"authorization": "Bearer abc"}}})
    assert cleaned["outer"]["headers"]["authorization"] == REDACTED


def test_raw_evidence_bytes_are_never_logged() -> None:
    """Field evidence is a photograph of someone's workplace. Length is the fact."""
    cleaned = redact({"image": b"\x89PNG" + b"x" * 5000})
    assert cleaned["image"] == "<5004 bytes>"
    assert "PNG" not in str(cleaned["image"])


def test_long_values_are_truncated() -> None:
    cleaned = redact({"blob": "y" * (MAX_VALUE_CHARS + 500)})
    assert cleaned["blob"].endswith("[truncated]")
    assert len(cleaned["blob"]) < MAX_VALUE_CHARS + 40


def test_a_token_passed_as_a_field_does_not_reach_the_line(recorder: Recorder) -> None:
    line = recorder.record("auth.checked", authorization="Bearer ya29.real-token").as_json()
    assert "ya29" not in line
    assert REDACTED in line


# ============================ spans ===================================================


def test_a_span_records_start_and_completion(recorder: Recorder) -> None:
    with recorder.span("remediation", artifact_id="wi-1"):
        pass
    events = [r.event for r in recorder.records]
    assert events == ["remediation.started", "remediation.completed"]
    assert recorder.records[-1].fields["duration_ms"] >= 0


def test_a_failing_span_records_the_failure_and_re_raises(recorder: Recorder) -> None:
    """Telemetry that swallowed the exception would change the program's behaviour."""
    with pytest.raises(ValueError, match="boom"), recorder.span("remediation"):
        raise ValueError("boom")

    failure = recorder.records[-1]
    assert failure.event == "remediation.failed"
    assert failure.severity is Severity.ERROR
    assert failure.fields["error_type"] == "ValueError"


def test_a_span_shares_the_correlation_context(recorder: Recorder) -> None:
    with recorder.span("delivery") as scoped:
        scoped.record("delivery.receipt", receipt_ref="r-1")
    assert all(
        r.context.correlation_id == recorder.context.correlation_id
        for r in recorder.records
    )


# ============================ retry observability =====================================


@dataclass(frozen=True)
class _Attempt:
    succeeded: bool
    failure_class: str | None
    duration_ms: float


@dataclass(frozen=True)
class _Result:
    outcome: str
    attempts: tuple[_Attempt, ...]


def test_every_retry_attempt_is_observable(recorder: Recorder) -> None:
    """A call that succeeded on its third try is a different fact from one that did not."""
    result = _Result(
        outcome="SUCCEEDED",
        attempts=(
            _Attempt(False, "TRANSIENT", 61.0),
            _Attempt(False, "TRANSIENT", 60.5),
            _Attempt(True, None, 12.0),
        ),
    )
    record_attempts(recorder, result, operation="semantic")

    attempts = [r for r in recorder.records if r.event == "semantic.attempt"]
    assert len(attempts) == 3
    assert [r.fields["attempt"] for r in attempts] == [1, 2, 3]
    assert [str(r.severity) for r in attempts] == ["WARNING", "WARNING", "INFO"]
    assert recorder.records[-1].fields["outcome"] == "SUCCEEDED"


def test_an_exhausted_retry_sequence_is_recorded_as_an_error(recorder: Recorder) -> None:
    result = _Result(
        outcome="RETRIES_EXHAUSTED",
        attempts=(_Attempt(False, "TRANSIENT", 60.0), _Attempt(False, "TRANSIENT", 60.0)),
    )
    record_attempts(recorder, result, operation="semantic")
    assert recorder.records[-1].severity is Severity.ERROR
    assert recorder.records[-1].fields["attempts_total"] == 2


def test_the_real_retry_result_shape_is_observable() -> None:
    """Against T070's actual types, not a stand-in that happens to match."""
    from driftzero.retry import AttemptRecord, RetryOutcome, RetryResult

    fields = set(AttemptRecord.model_fields) if hasattr(AttemptRecord, "model_fields") else {
        f.name for f in __import__("dataclasses").fields(AttemptRecord)
    }
    assert {"succeeded", "failure_class"} & fields, fields
    assert hasattr(RetryResult, "__dataclass_fields__") or hasattr(RetryResult, "model_fields")
    assert RetryOutcome.SUCCEEDED


# ============================ the cloud export ========================================


def test_the_structured_sink_writes_one_json_line_per_record() -> None:
    from driftzero_cloud.telemetry import structured_sink

    stream = io.StringIO()
    recorder = Recorder(
        context=CorrelationContext.create(workflow_id="wf-1"), sink=structured_sink(stream)
    )
    recorder.record("a.b", value=1)
    recorder.record("c.d", value=2)

    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 2
    assert [json.loads(line)["event"] for line in lines] == ["a.b", "c.d"]
    assert json.loads(lines[0])["logging.googleapis.com/labels"]["workflow_id"] == "wf-1"


def test_logging_needs_no_client_library() -> None:
    """Cloud Run ingests stdout; a second delivery path would add failure modes."""
    source = (SRC / "driftzero_cloud" / "telemetry.py").read_text(encoding="utf-8")
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert "google" not in roots, "the logging client was imported after all"


def test_tracing_failure_never_breaks_the_process() -> None:
    from driftzero_cloud.telemetry import configure, disclosure

    telemetry = configure(project="driftzero-runtime-2026", enable_trace=False,
                          stream=io.StringIO())
    assert telemetry.trace_enabled is False
    assert telemetry.trace_error
    # Logging still works with tracing off.
    with telemetry.span("still.works"):
        pass
    assert disclosure(telemetry)["structured_logging"] is True


def test_the_disclosure_reports_tracing_honestly() -> None:
    from driftzero_cloud.telemetry import configure, disclosure

    reported = disclosure(
        configure(project="p", enable_trace=False, stream=io.StringIO())
    )
    assert reported["trace_enabled"] is False
    assert reported["trace_error"] is not None
    assert reported["logging_transport"].startswith("stdout JSON")


def test_a_configured_tracer_wraps_the_recorder_span() -> None:
    from driftzero_cloud.telemetry import CloudTelemetry

    class FakeSpan:
        def __init__(self) -> None:
            self.attributes: dict[str, Any] = {}

        def set_attribute(self, key: str, value: Any) -> None:
            self.attributes[key] = value

    class FakeCtx:
        def __init__(self, span: FakeSpan) -> None:
            self.span = span

        def __enter__(self) -> FakeSpan:
            return self.span

        def __exit__(self, *exc: Any) -> bool:
            return False

    class FakeTracer:
        def __init__(self) -> None:
            self.span = FakeSpan()
            self.names: list[str] = []

        def start_as_current_span(self, name: str) -> FakeCtx:
            self.names.append(name)
            return FakeCtx(self.span)

    tracer = FakeTracer()
    recorder = Recorder(context=CorrelationContext.create(workflow_id="wf-9"))
    telemetry = CloudTelemetry(project="p", recorder=recorder, tracer=tracer)

    with telemetry.span("impact.qualify"):
        pass

    assert tracer.names == ["impact.qualify"]
    assert tracer.span.attributes["workflow_id"] == "wf-9"
    assert [r.event for r in recorder.records] == [
        "impact.qualify.started",
        "impact.qualify.completed",
    ]


# ============================ boundaries ==============================================


def test_the_pure_half_imports_nothing_third_party() -> None:
    source = (SRC / "driftzero" / "observability.py").read_text(encoding="utf-8")
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    import sys

    third_party = {r for r in roots if r not in sys.stdlib_module_names and r != "driftzero"}
    assert third_party == set(), f"observability.py imports {third_party}"


def test_telemetry_is_never_authoritative() -> None:
    """A record describes a decision; it must never be able to make one."""
    source = (SRC / "driftzero" / "observability.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
            node.body = [
                n
                for n in node.body
                if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
            ] or [ast.Pass()]
    import re

    names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", ast.unparse(tree)))
    for forbidden in (
        "WorkflowState",
        "VerificationResult",
        "ChangeProof",
        "generate_change_proof",
        "transition",
    ):
        assert forbidden not in names, f"observability touches {forbidden!r}"
