"""T069/T070 — configuration boundary and bounded retry policy.

Offline and deterministic: the clock is injected, nothing sleeps, and no environment
file or credential is required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.config import (  # noqa: E402
    DEFAULT_SEMANTIC_MODEL_ID,
    MAX_ALLOWED_SEMANTIC_RETRIES,
    ConfigurationError,
    DriftZeroConfig,
    GemmaConfig,
    SemanticModelConfig,
    SideEffectConfig,
)
from driftzero.retry import (  # noqa: E402
    FailureClass,
    MalformedStructuredOutput,
    NonTransientModelError,
    RetryOutcome,
    TransientModelError,
    classify_failure,
    run_semantic_call,
    side_effect_is_retryable,
)

# ============================ configuration defaults ==================================


def test_semantic_model_defaults_to_gemini_3_5_flash() -> None:
    assert DEFAULT_SEMANTIC_MODEL_ID == "gemini-3.5-flash"
    assert DriftZeroConfig().semantic.model_id == "gemini-3.5-flash"


def test_semantic_defaults_match_the_engineering_policy() -> None:
    """60 s per attempt; 1 initial attempt plus at most 2 retries."""
    semantic = DriftZeroConfig().semantic
    assert semantic.timeout_seconds == 60.0
    assert semantic.max_retries == 2
    assert semantic.max_attempts == 3


def test_side_effect_and_gemma_defaults() -> None:
    config = DriftZeroConfig()
    assert config.side_effect.timeout_seconds == 30.0
    assert config.side_effect.auto_retry is False
    assert config.gemma.timeout_seconds == 60.0


def test_config_requires_no_env_file_and_no_credentials() -> None:
    """An empty mapping must yield a fully valid configuration."""
    config = DriftZeroConfig.from_env({})
    assert config.semantic.model_id == "gemini-3.5-flash"
    assert config.semantic.timeout_seconds == 60.0
    assert config.semantic.max_attempts == 3


def test_config_holds_no_credential_fields() -> None:
    """Behavioural configuration only — no place to put a secret."""
    fields = set(SemanticModelConfig.__dataclass_fields__)
    for forbidden in ("api_key", "token", "credentials", "secret", "password"):
        assert forbidden not in fields


# ============================ configuration injection =================================


def test_config_is_injectable_from_a_mapping() -> None:
    config = DriftZeroConfig.from_env(
        {
            "DRIFTZERO_SEMANTIC_MODEL_ID": "gemini-3.5-flash-test",
            "DRIFTZERO_SEMANTIC_TIMEOUT_SECONDS": "12.5",
            "DRIFTZERO_SEMANTIC_MAX_RETRIES": "1",
            "DRIFTZERO_SIDE_EFFECT_TIMEOUT_SECONDS": "5",
            "DRIFTZERO_GEMMA_TIMEOUT_SECONDS": "7",
        }
    )
    assert config.semantic.model_id == "gemini-3.5-flash-test"
    assert config.semantic.timeout_seconds == 12.5
    assert config.semantic.max_retries == 1
    assert config.semantic.max_attempts == 2
    assert config.side_effect.timeout_seconds == 5.0
    assert config.gemma.timeout_seconds == 7.0


def test_config_can_be_constructed_directly_for_tests() -> None:
    config = DriftZeroConfig(semantic=SemanticModelConfig(max_retries=0))
    assert config.semantic.max_attempts == 1


def test_with_semantic_returns_a_modified_copy() -> None:
    base = DriftZeroConfig()
    derived = base.with_semantic(max_retries=1)
    assert base.semantic.max_retries == 2, "the original must be unchanged"
    assert derived.semantic.max_retries == 1


# ============================ configuration bounds ====================================


def test_retry_cap_cannot_be_raised_above_policy() -> None:
    with pytest.raises(ConfigurationError, match="exceeds the binding cap"):
        SemanticModelConfig(max_retries=MAX_ALLOWED_SEMANTIC_RETRIES + 1)


@pytest.mark.parametrize("value", [0, -1.0])
def test_non_positive_timeouts_are_rejected(value: float) -> None:
    with pytest.raises(ConfigurationError):
        SemanticModelConfig(timeout_seconds=value)
    with pytest.raises(ConfigurationError):
        SideEffectConfig(timeout_seconds=value)
    with pytest.raises(ConfigurationError):
        GemmaConfig(timeout_seconds=value)


def test_negative_retries_are_rejected() -> None:
    with pytest.raises(ConfigurationError):
        SemanticModelConfig(max_retries=-1)


def test_empty_model_id_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        SemanticModelConfig(model_id="   ")


def test_side_effect_auto_retry_cannot_be_enabled() -> None:
    """A post-dispatch timeout is UNKNOWN; blind retry must be unrepresentable."""
    with pytest.raises(ConfigurationError, match="never auto-retry"):
        SideEffectConfig(auto_retry=True)


def test_side_effect_retry_requires_reconciliation() -> None:
    assert side_effect_is_retryable(reconciliation_proved_safe=False) is False
    assert side_effect_is_retryable(reconciliation_proved_safe=True) is True


@pytest.mark.parametrize("name", ["SEMANTIC_TIMEOUT_SECONDS", "SEMANTIC_MAX_RETRIES"])
def test_unparseable_env_values_fail_closed(name: str) -> None:
    with pytest.raises(ConfigurationError):
        DriftZeroConfig.from_env({f"DRIFTZERO_{name}": "not-a-number"})


# ============================ failure classification ==================================


def test_failures_are_classified_for_retry_eligibility() -> None:
    assert classify_failure(TransientModelError("429")) is FailureClass.TRANSIENT
    assert classify_failure(MalformedStructuredOutput("bad")) is FailureClass.MALFORMED_OUTPUT
    assert classify_failure(NonTransientModelError("denied")) is FailureClass.NON_TRANSIENT
    assert classify_failure(TimeoutError("slow")) is FailureClass.TIMEOUT


def test_unknown_exceptions_are_treated_as_non_transient() -> None:
    """Failing closed on an unrecognized error beats retrying it blindly."""
    assert classify_failure(ValueError("who knows")) is FailureClass.NON_TRANSIENT


# ============================ bounded retry behaviour =================================


class _Clock:
    """Injected monotonic clock: deterministic, and nothing ever sleeps."""

    def __init__(self, step: float = 0.1) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


def test_a_successful_first_attempt_makes_one_call() -> None:
    calls: list[float] = []

    def call(deadline: float) -> str:
        calls.append(deadline)
        return "ok"

    result = run_semantic_call(call, SemanticModelConfig(), clock=_Clock())
    assert result.outcome is RetryOutcome.SUCCEEDED
    assert result.value == "ok"
    assert result.attempt_count == 1
    assert calls == [60.0], "the configured deadline is handed to the callable"


def test_transient_failures_retry_up_to_the_cap_then_exhaust() -> None:
    attempts = 0

    def call(_: float) -> str:
        nonlocal attempts
        attempts += 1
        raise TransientModelError("429 throttled")

    result = run_semantic_call(call, SemanticModelConfig(), clock=_Clock())
    assert result.outcome is RetryOutcome.EXHAUSTED
    assert attempts == 3, "1 initial attempt + exactly 2 retries"
    assert result.attempt_count == 3
    assert result.value is None


def test_retry_recovers_when_a_later_attempt_succeeds() -> None:
    script: list[object] = [TransientModelError("timeout"), "recovered"]

    def call(_: float) -> object:
        step = script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    result = run_semantic_call(call, SemanticModelConfig(), clock=_Clock())
    assert result.outcome is RetryOutcome.SUCCEEDED
    assert result.value == "recovered"
    assert result.attempt_count == 2


def test_non_transient_failure_stops_immediately() -> None:
    attempts = 0

    def call(_: float) -> str:
        nonlocal attempts
        attempts += 1
        raise NonTransientModelError("permission denied")

    result = run_semantic_call(call, SemanticModelConfig(), clock=_Clock())
    assert result.outcome is RetryOutcome.NON_TRANSIENT
    assert attempts == 1, "a permanent error must not consume the retry budget"


def test_malformed_output_gets_exactly_one_bounded_repair() -> None:
    """The repair draws from the same budget — it is not an extra allowance."""
    attempts = 0

    def call(_: float) -> str:
        nonlocal attempts
        attempts += 1
        raise MalformedStructuredOutput("not the expected shape")

    result = run_semantic_call(call, SemanticModelConfig(), clock=_Clock())
    assert result.outcome is RetryOutcome.EXHAUSTED
    assert attempts == 2, "initial attempt + one repair, then stop"
    assert result.repair_attempts_used == 1


def test_repair_can_be_disabled(  # noqa: D103
) -> None:
    attempts = 0

    def call(_: float) -> str:
        nonlocal attempts
        attempts += 1
        raise MalformedStructuredOutput("bad")

    config = SemanticModelConfig(allow_structured_repair=False)
    result = run_semantic_call(call, config, clock=_Clock())
    assert result.outcome is RetryOutcome.EXHAUSTED
    assert attempts == 1


def test_retry_never_loops_unbounded() -> None:
    """Whatever the failure mix, attempts can never exceed the configured budget."""
    for failure in (TransientModelError("t"), MalformedStructuredOutput("m")):
        attempts = 0

        def call(_: float, exc: BaseException = failure) -> str:
            nonlocal attempts
            attempts += 1
            raise exc

        result = run_semantic_call(call, SemanticModelConfig(), clock=_Clock())
        assert attempts <= SemanticModelConfig().max_attempts
        assert result.outcome is not RetryOutcome.SUCCEEDED


def test_overrunning_the_deadline_is_treated_as_a_timeout() -> None:
    """A late result is not silently accepted."""
    config = SemanticModelConfig(timeout_seconds=0.05)
    result = run_semantic_call(lambda _: "late", config, clock=_Clock(step=1.0))
    assert result.outcome is RetryOutcome.EXHAUSTED
    assert all(a.failure_class is FailureClass.TIMEOUT for a in result.attempts)


def test_every_attempt_is_recorded_including_failures() -> None:
    script: list[object] = [TransientModelError("a"), TransientModelError("b"), "ok"]

    def call(_: float) -> object:
        step = script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    result = run_semantic_call(call, SemanticModelConfig(), clock=_Clock())
    assert result.attempt_count == 3
    assert [a.succeeded for a in result.attempts] == [False, False, True]
    assert result.attempts[0].error is not None
