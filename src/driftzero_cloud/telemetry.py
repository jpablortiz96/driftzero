"""T098 — exporting DRIFTZERO telemetry to Cloud Logging and Cloud Trace.

The pure half lives in ``driftzero.observability``: correlation identity, record shape,
redaction. This is the half that needs Google libraries, so it lives out here where the
purity guard permits them.

**Cloud Logging** needs no client library. Cloud Run ingests stdout, and a single-line
JSON object with ``severity`` and ``logging.googleapis.com/labels`` is parsed into a
structured, label-indexed entry. Adding the logging client would mean a second delivery
path with its own buffering and failure modes for no gain.

**Cloud Trace** does need an exporter. The runtime service account already holds
``roles/cloudtrace.agent`` from T091.

Both are optional at runtime. If tracing cannot be configured the application still
runs and still logs — telemetry that can take the request path down with it is worse
than no telemetry.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any

from driftzero.observability import (
    CorrelationContext,
    Recorder,
    TelemetryRecord,
)

TRACE_LABEL = "logging.googleapis.com/trace"
"""Cloud Logging reads this field to bind a log entry to a trace, which is what makes a
trace in Cloud Trace show the log lines belonging to it."""


def structured_sink(stream: Any = None) -> Any:
    """A sink that writes one JSON object per line for Cloud Logging to parse."""
    target = stream if stream is not None else sys.stdout

    def emit(record: TelemetryRecord) -> None:
        print(record.as_json(), file=target, flush=True)

    return emit


def logging_sink(logger: logging.Logger | None = None) -> Any:
    """A sink that routes through ``logging`` for hosts that capture it."""
    log = logger or logging.getLogger("driftzero")

    def emit(record: TelemetryRecord) -> None:
        entry = record.as_entry()
        log.log(
            getattr(logging, str(record.severity), logging.INFO),
            record.as_json(),
            extra={"driftzero": entry},
        )

    return emit


@dataclass
class CloudTelemetry:
    """Configured telemetry for one deployed process."""

    project: str
    recorder: Recorder
    tracer: Any = None
    trace_enabled: bool = False
    trace_error: str | None = None

    def span(self, name: str, **fields: Any) -> Any:
        """A Cloud Trace span around a recorded DRIFTZERO span, when tracing is on."""
        if self.tracer is None:
            return self.recorder.span(name, **fields)
        return _TracedSpan(self, name, fields)


class _TracedSpan:
    """Bridges one recorder span and one OpenTelemetry span."""

    def __init__(self, telemetry: CloudTelemetry, name: str, fields: dict[str, Any]) -> None:
        self._telemetry = telemetry
        self._name = name
        self._fields = fields
        self._otel: Any = None
        self._recorder_span: Any = None

    def __enter__(self) -> Recorder:
        self._otel = self._telemetry.tracer.start_as_current_span(self._name)
        span = self._otel.__enter__()
        for key, value in self._telemetry.recorder.context.as_labels().items():
            span.set_attribute(key, value)
        self._recorder_span = self._telemetry.recorder.span(self._name, **self._fields)
        return self._recorder_span.__enter__()

    def __exit__(self, *exc: Any) -> bool:
        suppressed = False
        if self._recorder_span is not None:
            suppressed = bool(self._recorder_span.__exit__(*exc))
        if self._otel is not None:
            self._otel.__exit__(*exc)
        return suppressed


def configure(
    *,
    project: str,
    context: CorrelationContext | None = None,
    enable_trace: bool = True,
    stream: Any = None,
) -> CloudTelemetry:
    """Build telemetry for this process.

    Structured logging is always configured — it is just stdout. Tracing is attempted
    and its failure recorded rather than raised: a process that cannot reach Cloud Trace
    must still serve requests, and must still say plainly that it is not tracing.
    """
    recorder = Recorder(
        context=context or CorrelationContext.create(), sink=structured_sink(stream)
    )
    telemetry = CloudTelemetry(project=project, recorder=recorder)

    if not enable_trace:
        telemetry.trace_error = "tracing disabled by configuration"
        return telemetry

    try:
        from opentelemetry import trace  # noqa: PLC0415
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter  # noqa: PLC0415
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

        provider = TracerProvider(
            resource=Resource.create({"service.name": "driftzero-api"})
        )
        provider.add_span_processor(
            BatchSpanProcessor(CloudTraceSpanExporter(project_id=project))
        )
        trace.set_tracer_provider(provider)
        telemetry.tracer = trace.get_tracer("driftzero")
        telemetry.trace_enabled = True
    except Exception as exc:  # pragma: no cover - depends on the deployed environment
        telemetry.trace_error = f"{type(exc).__name__}: {exc}"[:400]

    return telemetry


def disclosure(telemetry: CloudTelemetry) -> dict[str, Any]:
    """What a readiness endpoint may honestly say about telemetry."""
    return {
        "structured_logging": True,
        "logging_transport": "stdout JSON parsed by Cloud Logging",
        "trace_enabled": telemetry.trace_enabled,
        "trace_error": telemetry.trace_error,
        "correlation_labels": sorted(telemetry.recorder.context.as_labels()),
        "project": telemetry.project,
    }
