"""T098 — correlation identity and structured telemetry records.

Deliberately pure: stdlib only, no OpenTelemetry, no Google SDK, no third-party import
at all. This module decides *what* is observable and how a record is shaped; the export
to Cloud Trace and Cloud Logging is a separate concern and lives in
``driftzero_cloud.telemetry``, outside the M0 purity boundary.

The split matters beyond tidiness. Correlation identity is part of the domain's story
about an execution — a ``workflow_id`` and an ``action_id`` are the same identifiers the
Truth Engine reasons about — while a span exporter is a deployment detail. Keeping them
apart means the deterministic core can be traced in a test with no cloud, and that an
export failure can never change what the system recorded.

Nothing here is authoritative. A telemetry record describes a decision; it never makes
one, and no field of it is ever read back as input.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = 1

REDACTED = "[REDACTED]"

SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "token",
        "id_token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "secret",
        "password",
        "credential",
        "credentials",
        "private_key",
        "client_secret",
        "bearer",
    }
)
"""Keys whose value is never emitted. Telemetry is shipped off the machine and retained,
so a credential that reaches a log has effectively been published."""

MAX_VALUE_CHARS = 2048
"""Field values are truncated rather than streamed whole. Raw field evidence is bytes of
someone's workplace; a log is not where it belongs."""


class Severity(StrEnum):
    """Cloud Logging severity names, used verbatim so no mapping table is needed."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class CorrelationContext:
    """The identifiers that tie one execution together.

    ``correlation_id`` is generated once per logical execution and carried across
    process boundaries, so a workflow that spans three Cloud Run instances still reads
    as one story. ``workflow_id`` and ``action_id`` are the domain's own identifiers,
    not telemetry inventions.
    """

    correlation_id: str
    workflow_id: str | None = None
    change_id: str | None = None
    action_id: str | None = None
    proof_id: str | None = None
    instance_id: str | None = None

    @classmethod
    def create(cls, **fields: Any) -> CorrelationContext:
        return cls(correlation_id=fields.pop("correlation_id", None) or new_correlation_id(),
                   **fields)

    def bind(self, **fields: Any) -> CorrelationContext:
        """A new context with more known. Contexts are frozen, so nothing mutates."""
        merged = {**self.as_labels(), **{k: v for k, v in fields.items() if v is not None}}
        return CorrelationContext(**merged)

    def as_labels(self) -> dict[str, str]:
        """Only the identifiers that are set. Absent is absent, never an empty string."""
        return {
            key: value
            for key, value in {
                "correlation_id": self.correlation_id,
                "workflow_id": self.workflow_id,
                "change_id": self.change_id,
                "action_id": self.action_id,
                "proof_id": self.proof_id,
                "instance_id": self.instance_id,
            }.items()
            if value
        }


def new_correlation_id() -> str:
    return f"corr-{uuid.uuid4().hex}"


def redact(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Drop credential-shaped values and truncate long ones, recursively."""
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if str(key).strip().lower().replace("-", "_") in SENSITIVE_KEYS:
            clean[key] = REDACTED
            continue
        if isinstance(value, Mapping):
            clean[key] = redact(value)
        elif isinstance(value, bytes):
            # Never the bytes themselves: length is the observable fact.
            clean[key] = f"<{len(value)} bytes>"
        elif isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
            clean[key] = value[:MAX_VALUE_CHARS] + "…[truncated]"
        else:
            clean[key] = value
    return clean


@dataclass(frozen=True)
class TelemetryRecord:
    """One structured entry, shaped for Cloud Logging's JSON payload conventions."""

    event: str
    severity: Severity
    context: CorrelationContext
    fields: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def as_entry(self) -> dict[str, Any]:
        """The dict a structured logger emits.

        ``severity`` and ``logging.googleapis.com/labels`` are the field names Cloud
        Logging parses out of a JSON line on stdout, so a Cloud Run container gets
        structured, label-indexed entries with no logging client library at all.
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "severity": str(self.severity),
            "message": self.event,
            "event": self.event,
            "timestamp": self.timestamp,
            "logging.googleapis.com/labels": self.context.as_labels(),
            **redact(self.fields),
        }

    def as_json(self) -> str:
        return json.dumps(self.as_entry(), sort_keys=True, default=str)


@dataclass
class Recorder:
    """Collects telemetry records and hands them to a sink.

    The default sink is a list, so a test can assert on exactly what was observed
    without a cloud, a logger, or captured stdout. ``driftzero_cloud.telemetry`` swaps
    in a sink that writes structured JSON and opens Cloud Trace spans.
    """

    context: CorrelationContext
    sink: Any = None
    records: list[TelemetryRecord] = field(default_factory=list)

    def record(
        self,
        event: str,
        *,
        severity: Severity = Severity.INFO,
        **fields: Any,
    ) -> TelemetryRecord:
        entry = TelemetryRecord(
            event=event, severity=severity, context=self.context, fields=dict(fields)
        )
        self.records.append(entry)
        if self.sink is not None:
            self.sink(entry)
        return entry

    def bind(self, **fields: Any) -> Recorder:
        """A recorder for a narrower scope, sharing the same sink and record list."""
        return Recorder(
            context=self.context.bind(**fields), sink=self.sink, records=self.records
        )

    @contextmanager
    def span(self, name: str, **fields: Any) -> Iterator[Recorder]:
        """Time one operation and record its outcome, success or failure.

        The failure path records and re-raises. Telemetry that swallowed the exception
        would be telemetry that changed the program's behaviour.
        """
        started = time.time()
        self.record(f"{name}.started", **fields)
        try:
            yield self
        except Exception as exc:
            self.record(
                f"{name}.failed",
                severity=Severity.ERROR,
                duration_ms=round((time.time() - started) * 1000, 3),
                error_type=type(exc).__name__,
                error=str(exc)[:MAX_VALUE_CHARS],
                **fields,
            )
            raise
        self.record(
            f"{name}.completed",
            duration_ms=round((time.time() - started) * 1000, 3),
            **fields,
        )


def record_attempts(recorder: Recorder, result: Any, *, operation: str) -> None:
    """Make a T070 retry sequence observable, attempt by attempt.

    Every attempt is emitted, not just the last one. A call that succeeded on its third
    try and one that succeeded immediately are different operational facts, and a
    summary that reports only the outcome hides the difference.
    """
    attempts = getattr(result, "attempts", ()) or ()
    for index, attempt in enumerate(attempts, start=1):
        recorder.record(
            f"{operation}.attempt",
            severity=(
                Severity.INFO
                if getattr(attempt, "succeeded", False)
                else Severity.WARNING
            ),
            attempt=index,
            attempts_total=len(attempts),
            failure_class=_as_str(getattr(attempt, "failure_class", None)),
            duration_ms=getattr(attempt, "duration_ms", None),
        )
    recorder.record(
        f"{operation}.outcome",
        severity=(
            Severity.INFO
            if _as_str(getattr(result, "outcome", None)) == "SUCCEEDED"
            else Severity.ERROR
        ),
        outcome=_as_str(getattr(result, "outcome", None)),
        attempts_total=len(attempts),
    )


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)
