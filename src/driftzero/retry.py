"""T070 — bounded retry and timeout policy.

Implements plan.md § Retry & Timeout Engineering Policy as executable rules:

* **Semantic calls** — 1 initial attempt + at most 2 retries, transient conditions only.
  Exhaustion fails closed (FR-011 → ``REVIEW_REQUIRED``); it never returns a guess.
* **Malformed structured output** — may consume **one** bounded repair attempt from the
  same budget. It is not an additional allowance, and it is available once per call.
* **Side-effect calls** — never auto-retried here. A timeout after dispatch is an
  ``UNKNOWN`` outcome and must go through reconciliation, so this module refuses to
  retry them rather than offering a flag that could be set wrongly.

Timeouts are enforced by *deadline handoff*: the per-attempt deadline is passed to the
callable, which is the only layer that can actually abort in-flight I/O. Elapsed time is
measured and an overrun is classified as a transient timeout. This module does not claim
to interrupt a blocking call it did not make.

Non-transient failures stop immediately — retrying a schema or authorization error just
burns budget.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, TypeVar

from driftzero.config import SemanticModelConfig

T = TypeVar("T")


class RetryOutcome(StrEnum):
    """How a bounded call sequence ended."""

    SUCCEEDED = "SUCCEEDED"
    EXHAUSTED = "EXHAUSTED"
    NON_TRANSIENT = "NON_TRANSIENT"


class FailureClass(StrEnum):
    """Why one attempt failed. Only ``TRANSIENT`` and ``MALFORMED_OUTPUT`` may retry."""

    TRANSIENT = "TRANSIENT"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    NON_TRANSIENT = "NON_TRANSIENT"
    TIMEOUT = "TIMEOUT"


class SemanticCallError(Exception):
    """Base class for failures raised by a semantic model client."""


class TransientModelError(SemanticCallError):
    """Retry-eligible: request timeout, 429/throttling, eligible 5xx."""


class NonTransientModelError(SemanticCallError):
    """Not retry-eligible: authorization, invalid request, permanent refusal."""


class MalformedStructuredOutput(SemanticCallError):
    """The response did not satisfy the expected structure.

    Eligible for exactly one bounded repair attempt within the existing budget.
    """


@dataclass(frozen=True)
class AttemptRecord:
    """One attempt, retained whether it succeeded or failed."""

    attempt: int
    succeeded: bool
    elapsed_seconds: float
    deadline_seconds: float
    failure_class: FailureClass | None = None
    error: str | None = None
    was_repair_attempt: bool = False


@dataclass
class RetryResult(Generic[T]):
    """The full record of a bounded call sequence.

    ``value`` is only meaningful when ``outcome`` is ``SUCCEEDED``. Callers must check
    the outcome rather than truthiness — a successful call may legitimately return an
    empty structure.
    """

    outcome: RetryOutcome
    value: T | None = None
    attempts: list[AttemptRecord] = field(default_factory=list)
    final_error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is RetryOutcome.SUCCEEDED

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def repair_attempts_used(self) -> int:
        return sum(1 for a in self.attempts if a.was_repair_attempt)


def classify_failure(exc: BaseException) -> FailureClass:
    """Map an exception onto its retry eligibility.

    Unknown exception types are deliberately classified ``NON_TRANSIENT``: failing
    closed on something we do not understand is safer than retrying it blindly.
    """
    if isinstance(exc, MalformedStructuredOutput):
        return FailureClass.MALFORMED_OUTPUT
    if isinstance(exc, TransientModelError):
        return FailureClass.TRANSIENT
    if isinstance(exc, TimeoutError):
        return FailureClass.TIMEOUT
    return FailureClass.NON_TRANSIENT


def run_semantic_call(
    call: Callable[[float], T],
    config: SemanticModelConfig,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> RetryResult[T]:
    """Run ``call`` under the semantic retry/timeout policy.

    ``call`` receives the per-attempt deadline in seconds and must enforce it against
    whatever transport it uses. ``clock`` is injectable so tests measure deterministically
    without sleeping.

    Retries only on transient conditions, plus at most one malformed-output repair drawn
    from the same budget. Returns a result; never raises for an expected failure mode.
    """
    attempts: list[AttemptRecord] = []
    repair_used = False
    deadline = config.timeout_seconds

    for attempt in range(1, config.max_attempts + 1):
        is_repair = bool(attempts) and attempts[-1].failure_class is FailureClass.MALFORMED_OUTPUT
        started = clock()
        try:
            value = call(deadline)
        except BaseException as exc:  # noqa: BLE001 - classified, then re-raised or recorded
            elapsed = clock() - started
            failure = classify_failure(exc)
            attempts.append(
                AttemptRecord(
                    attempt=attempt,
                    succeeded=False,
                    elapsed_seconds=elapsed,
                    deadline_seconds=deadline,
                    failure_class=failure,
                    error=f"{type(exc).__name__}: {exc}",
                    was_repair_attempt=is_repair,
                )
            )

            if failure is FailureClass.NON_TRANSIENT:
                return RetryResult(
                    RetryOutcome.NON_TRANSIENT, None, attempts, attempts[-1].error
                )
            if failure is FailureClass.MALFORMED_OUTPUT:
                # One bounded repair, total, and only if configuration permits it.
                if repair_used or not config.allow_structured_repair:
                    return RetryResult(
                        RetryOutcome.EXHAUSTED, None, attempts, attempts[-1].error
                    )
                repair_used = True
            continue

        elapsed = clock() - started
        if elapsed > deadline:
            # The callable returned, but past its deadline: treat as a transient timeout
            # rather than silently accepting a result the policy did not allow time for.
            attempts.append(
                AttemptRecord(
                    attempt=attempt,
                    succeeded=False,
                    elapsed_seconds=elapsed,
                    deadline_seconds=deadline,
                    failure_class=FailureClass.TIMEOUT,
                    error=f"attempt exceeded {deadline}s deadline",
                    was_repair_attempt=is_repair,
                )
            )
            continue

        attempts.append(
            AttemptRecord(
                attempt=attempt,
                succeeded=True,
                elapsed_seconds=elapsed,
                deadline_seconds=deadline,
                was_repair_attempt=is_repair,
            )
        )
        return RetryResult(RetryOutcome.SUCCEEDED, value, attempts, None)

    return RetryResult(
        RetryOutcome.EXHAUSTED,
        None,
        attempts,
        attempts[-1].error if attempts else "no attempt was made",
    )


def side_effect_is_retryable(*, reconciliation_proved_safe: bool) -> bool:
    """Side-effect retries are permitted only after reconciliation proves it safe.

    Exposed as an explicit predicate so no call site can retry a mutation or delivery
    merely because a timeout occurred. A post-dispatch timeout is UNKNOWN, not failure.
    """
    return reconciliation_proved_safe
