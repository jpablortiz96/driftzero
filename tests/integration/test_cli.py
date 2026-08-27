"""T081 — the CLI entry points, driven through the real adapter seam.

The CLI is a transport. These tests exercise it against a live FastAPI test client rather
than mocking the transport away, so the thing under test is the same path an operator's
shell takes: separate invocations sharing one runtime's process-local workflow registry.

Fully offline. The ADK runtime and its agents are real; only the two models are stubs.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import hashlib
import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero import cli  # noqa: E402
from driftzero.agents import field_verify as fv  # noqa: E402
from driftzero.agents import model_client as mc  # noqa: E402
from driftzero.agents.field_verify import ProviderObservation  # noqa: E402
from driftzero.models.workflow import WorkflowState  # noqa: E402
from driftzero_console import app as app_module  # noqa: E402
from driftzero_console.workflows import (  # noqa: E402
    FORBIDDEN_FIXTURE_KEYS,
    FixtureRejected,
    dataset_from_fixture,
    validate_fixture,
)

from ._pilot import clear_change_intelligence, make_stub_llm, proposal_payload  # noqa: E402

FIXTURES = REPO_ROOT / "fixtures"
HERO_FIXTURE = FIXTURES / "hero_change.json"
LEFT_IMG = FIXTURES / "multimodal" / "label_left_01.jpg"
TOP_RIGHT_IMG = FIXTURES / "multimodal" / "label_top_right_01.jpg"
AMBIGUOUS_IMG = FIXTURES / "multimodal" / "label_ambiguous_01.jpg"
LIVE_PILOT = REPO_ROOT / "evidence" / "final_live_pilot_2026_08_26"

FIXTURE_CATALOG = (
    "wi-forklift-turn-014",
    "wi-packing-nightshift-007",
    "wi-packing-standard-001",
    "wi-packing-standard-002",
    "wi-packing-standard-003",
)


class StubGemma:
    """Deterministic observations, in order. No network, no credentials, no cost."""

    name = "stub_gemma"
    calls = 0

    def __init__(self, outputs: list[str]) -> None:
        type(self).calls = 0
        self._outputs = outputs

    def observe(self, **_kwargs: Any) -> ProviderObservation:
        type(self).calls += 1
        output = self._outputs[min(type(self).calls - 1, len(self._outputs) - 1)]
        return ProviderObservation(
            raw_output=output, provider=self.name, model="stub/gemma"
        )


@pytest.fixture(autouse=True)
def _isolate() -> Any:
    clear_change_intelligence()
    fv.clear_field_observation_provider()
    app_module.get_registry().clear()
    yield
    clear_change_intelligence()
    fv.clear_field_observation_provider()
    app_module.get_registry().clear()


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A running LOCAL_PILOT runtime with offline providers, plus a CLI bound to it.

    ``cli.request_json`` is pointed at the FastAPI test client, which is the transport
    seam. Everything above it — argument parsing, envelopes, exit codes — is the real CLI.
    """
    monkeypatch.setenv("DRIFTZERO_FIELD_PROVIDER", "vertex_maas")
    monkeypatch.setenv("DRIFTZERO_GCP_PROJECT", "offline-stub")
    monkeypatch.setenv("DRIFTZERO_SEMANTIC_PROVIDER", "google_adk")
    monkeypatch.setenv("DRIFTZERO_GEMINI_MODEL", "stub-gemini")

    state: dict[str, Any] = {"change": None}
    handle = make_stub_llm(
        lambda _req: proposal_payload(state["change"], artifact_ids=FIXTURE_CATALOG)
    )

    from driftzero.models.change import ChangeSet
    from driftzero_adk.change_intel_runtime import GoogleAdkSemanticClient

    mc.register_model_client_provider(
        lambda cfg: GoogleAdkSemanticClient(
            config=cfg, output_schema=ChangeSet, model_override=handle.llm, use_vertex=False
        )
    )

    original = app_module.HeroConsoleService

    class Tracking(original):  # type: ignore[misc, valid-type]
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            state["change"] = self.current_change

    monkeypatch.setattr(app_module, "HeroConsoleService", Tracking)

    with TestClient(app_module.app) as client:

        def transport(method: str, url: str, *, payload: Any = None, headers: Any = None) -> Any:
            path = url.split("8080", 1)[-1] if "8080" in url else url
            for prefix in ("http://127.0.0.1", "http://testserver"):
                if path.startswith(prefix):
                    path = path[len(prefix) :]
            response = client.request(
                method,
                path,
                content=None if payload is None else json.dumps(payload),
                headers={"Content-Type": "application/json", **(headers or {})},
            )
            if response.status_code >= 400:
                detail = response.json().get("detail", response.text)
                raise cli.CliError(f"{method} {path} -> {response.status_code}: {detail}")
            return response.json()

        monkeypatch.setattr(cli, "request_json", transport)
        yield client


def run_cli(*argv: str) -> tuple[int, Any, str]:
    """Invoke the real ``main`` and capture stdout/stderr, as a shell would see them."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    raw = out.getvalue().strip()
    parsed: Any = None
    if raw:
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(raw)
    return code, parsed, err.getvalue()


def wire_gemma(*outputs: str) -> type[StubGemma]:
    stub = StubGemma(list(outputs) or ["TOP_RIGHT"])
    fv.register_field_observation_provider(lambda _c: stub)
    return StubGemma


def inject(runtime: Any) -> str:
    code, payload, err = run_cli("inject-change", "--fixture", str(HERO_FIXTURE))
    assert code == cli.EXIT_OK, err
    return payload["workflow_id"]


# ============================ 1-3. surface and purity =================================


def test_the_exact_t081_command_surface_exists() -> None:
    parser = cli.build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    commands = sorted(actions[0].choices)
    assert commands == ["inject-change", "proof", "status", "verify"]

    tasks = (REPO_ROOT / "specs" / "001-hero-change-deployment" / "tasks.md").read_text(
        encoding="utf-8"
    )
    line = next(raw for raw in tasks.splitlines() if raw.startswith("- [x] T081"))
    for command in ("inject-change", "status", "verify", "proof --validate"):
        assert command in line
    assert "src/driftzero/cli.py" in line


def test_cli_help_works_and_names_the_runtime() -> None:
    code, _payload, _err = run_cli("--help")
    assert code == cli.EXIT_OK, "--help is a successful invocation"
    # argparse re-wraps the description, so whitespace is normalised before matching.
    text = " ".join(cli.build_parser().format_help().split())
    assert "driftzero_console.app" in text
    assert "lost when it restarts" in text


def test_an_invalid_command_exits_non_zero() -> None:
    code, _payload, _err = run_cli("not-a-command")
    assert code != cli.EXIT_OK


def test_proof_without_validate_is_a_usage_error(runtime: Any) -> None:
    workflow_id = inject(runtime)
    code, _payload, err = run_cli("proof", "--workflow-id", workflow_id)
    assert code == cli.EXIT_USAGE
    assert "--validate" in err


def test_the_cli_imports_only_the_standard_library() -> None:
    """``src/driftzero`` is inside the M0 purity boundary."""
    stdlib = set(sys.stdlib_module_names)
    tree = ast.parse((REPO_ROOT / "src" / "driftzero" / "cli.py").read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    third_party = {r for r in roots if r not in stdlib and r != "driftzero"}
    assert third_party == set(), f"cli.py imports {sorted(third_party)}"
    assert "driftzero_console" not in roots
    for banned in ("google", "httpx", "requests", "fastapi"):
        assert banned not in roots


def test_the_cli_contains_no_business_truth() -> None:
    """Transport only — an AST sweep, so a comment about a rule is not the rule."""
    tree = ast.parse((REPO_ROOT / "src" / "driftzero" / "cli.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list):
            node.body = [  # type: ignore[attr-defined]
                c
                for c in body
                if not (
                    isinstance(c, ast.Expr)
                    and isinstance(c.value, ast.Constant)
                    and isinstance(c.value.value, str)
                )
            ] or [ast.Pass()]
    code = ast.unparse(tree)
    for banned in (
        "qualify_candidates",
        "resolve_cardinality",
        "compare_observation",
        "evaluate_proof_invariants",
        "generate_change_proof",
        "is_authorized",
        "transition",
        "ChangeProof(",
        "PROOF_COMPLETE",
        "VERIFICATION_PASSED",
        "ProofValidator",
    ):
        assert banned not in code, f"cli.py references {banned}"


# ============================ 4-9. inject-change ======================================


def test_inject_change_returns_a_workflow_id(runtime: Any) -> None:
    code, payload, err = run_cli("inject-change", "--fixture", str(HERO_FIXTURE))
    assert code == cli.EXIT_OK, err
    assert payload["workflow_id"]
    assert payload["change_id"] == "chg-2026-0817-0001"
    assert payload["runtime_readiness"] == "LOCAL_PILOT"
    assert "not durable" in payload["registry_note"]


def test_inject_change_runs_the_orchestration_to_the_async_pause(runtime: Any) -> None:
    wire_gemma("TOP_RIGHT")
    code, payload, _err = run_cli("inject-change", "--fixture", str(HERO_FIXTURE))
    assert code == cli.EXIT_OK
    assert payload["paused"] is True
    assert payload["paused_at"] == "s08_await_field_evidence"
    assert payload["pause_reason"] == "awaiting physical field evidence"
    assert payload["state"] == str(WorkflowState.AWAITING_FIELD_VERIFICATION)
    # Steps 9-11 have not run.
    assert "s09_10_field_observation_and_verdict" not in payload["steps_executed"]
    assert "s11_change_proof" not in payload["steps_executed"]


def test_inject_change_uses_the_real_impact_qualification_path(runtime: Any) -> None:
    """The stub proposed all five catalog artifacts; the Truth Engine qualified one."""
    _code, payload, _err = run_cli("inject-change", "--fixture", str(HERO_FIXTURE))
    impact = payload["impact"]
    assert impact["candidate_count"] == len(FIXTURE_CATALOG)
    assert impact["qualified_count"] == 1
    assert impact["outcome"] == "SINGLE_QUALIFIED_TARGET"
    assert impact["affected_artifact_id"] == "wi-packing-standard-001"
    assert impact["authority"] == "DRIFTZERO TRUTH ENGINE"


def test_inject_change_dispatches_remediation_and_delivery_once(runtime: Any) -> None:
    workflow_id = inject(runtime)
    _code, status, _err = run_cli("status", "--workflow-id", workflow_id)
    assert status["remediation"]["request_history"][-1]["dispatch_count"] == 1
    assert status["delivery"]["dispatch_count"] == 1


@pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_FIXTURE_KEYS))
def test_a_fixture_may_not_carry_a_conclusion(forbidden: str) -> None:
    payload = json.loads(HERO_FIXTURE.read_text(encoding="utf-8"))
    payload[forbidden] = "anything"
    with pytest.raises(FixtureRejected) as exc:
        validate_fixture(payload)
    assert forbidden in str(exc.value)


def test_inject_change_rejects_a_fixture_naming_an_affected_artifact(runtime: Any) -> None:
    poisoned = json.loads(HERO_FIXTURE.read_text(encoding="utf-8"))
    poisoned["affected_artifact_id"] = "wi-packing-standard-003"
    path = REPO_ROOT / "fixtures" / "_tmp_poisoned.json"
    path.write_text(json.dumps(poisoned), encoding="utf-8")
    try:
        code, _payload, err = run_cli("inject-change", "--fixture", str(path))
        assert code == cli.EXIT_FAILURE
        assert "affected_artifact_id" in err
        assert len(app_module.get_registry()) == 0, "no workflow was created"
    finally:
        path.unlink()


def test_inject_change_fails_on_an_unreadable_or_invalid_fixture(runtime: Any) -> None:
    code, _payload, err = run_cli("inject-change", "--fixture", str(REPO_ROOT / "nope.json"))
    assert code == cli.EXIT_FAILURE
    assert "cannot read" in err


def test_inject_change_fails_when_the_source_versions_are_missing(runtime: Any) -> None:
    payload = json.loads(HERO_FIXTURE.read_text(encoding="utf-8"))
    payload["source_version"] = "v99"
    with pytest.raises(FixtureRejected) as exc:
        dataset_from_fixture(payload, directory=FIXTURES)
    assert "v99" in str(exc.value)


# ============================ 10-13. status ===========================================


def test_status_from_a_separate_invocation_sees_the_same_server_state(runtime: Any) -> None:
    """The point of the architecture: two CLI processes, one runtime, one workflow."""
    workflow_id = inject(runtime)
    code, status, err = run_cli("status", "--workflow-id", workflow_id)
    assert code == cli.EXIT_OK, err
    assert status["workflow_id"] == workflow_id
    assert status["workflow_state"] == str(WorkflowState.AWAITING_FIELD_VERIFICATION)
    assert status["impact"]["affected_artifact_id"] == "wi-packing-standard-001"
    assert status["remediation"]["state"] == "MUTATED"
    assert status["delivery"]["crossing_3"] == "ACCEPTED"


def test_status_exposes_the_honest_dimensions(runtime: Any) -> None:
    workflow_id = inject(runtime)
    _code, status, _err = run_cli("status", "--workflow-id", workflow_id)
    for key in (
        "workflow_id",
        "change_id",
        "workflow_state",
        "impact",
        "remediation",
        "delivery",
        "field_verification",
        "deterministic_verdict",
        "proof",
        "change_deployed",
        "runtime_readiness",
    ):
        assert key in status, f"status omits {key}"
    assert status["runtime_readiness"] == "LOCAL_PILOT"
    assert status["production_ready"] is False
    assert "PRODUCTION_READY" not in json.dumps(status)


def test_an_unknown_workflow_fails_instead_of_recreating_a_default(runtime: Any) -> None:
    code, payload, err = run_cli("status", "--workflow-id", "wf-never-injected")
    assert code == cli.EXIT_FAILURE
    assert payload is None
    assert "no workflow" in err
    assert len(app_module.get_registry()) == 0


def test_a_simulated_restart_loses_state_honestly(runtime: Any) -> None:
    """Clearing the registry is what a server restart does. State must not survive."""
    workflow_id = inject(runtime)
    assert run_cli("status", "--workflow-id", workflow_id)[0] == cli.EXIT_OK

    app_module.get_registry().clear()

    code, _payload, err = run_cli("status", "--workflow-id", workflow_id)
    assert code == cli.EXIT_FAILURE
    assert "no workflow" in err
    assert "not durable" in err or "restart" in err


def test_status_has_zero_side_effects(runtime: Any) -> None:
    wire_gemma("TOP_RIGHT")
    workflow_id = inject(runtime)
    before = run_cli("status", "--workflow-id", workflow_id)[1]
    for _ in range(3):
        after = run_cli("status", "--workflow-id", workflow_id)[1]
    assert after["workflow_state"] == before["workflow_state"]
    assert after["delivery"]["dispatch_count"] == before["delivery"]["dispatch_count"]
    assert StubGemma.calls == 0


# ============================ 14-21. verify ===========================================


def test_verify_resumes_the_same_workflow_and_fails_on_a_wrong_photo(runtime: Any) -> None:
    wire_gemma("LEFT")
    workflow_id = inject(runtime)
    code, status, err = run_cli(
        "verify", "--workflow-id", workflow_id, "--image", str(LEFT_IMG)
    )
    assert code == cli.EXIT_OK, err
    assert status["workflow_id"] == workflow_id
    assert status["deterministic_verdict"]["result"] == "FAIL"
    assert status["workflow_state"] == str(WorkflowState.VERIFICATION_FAILED)
    assert status["proof"]["generated"] is False
    assert status["change_deployed"] is False


def test_a_corrected_photo_reaches_pass_and_proof_complete(runtime: Any) -> None:
    """The full recovery path, across separate CLI invocations."""
    wire_gemma("LEFT", "TOP_RIGHT")
    workflow_id = inject(runtime)

    failed = run_cli("verify", "--workflow-id", workflow_id, "--image", str(LEFT_IMG))[1]
    assert failed["deterministic_verdict"]["result"] == "FAIL"

    passed = run_cli(
        "verify", "--workflow-id", workflow_id, "--image", str(TOP_RIGHT_IMG)
    )[1]
    assert passed["deterministic_verdict"]["result"] == "PASS"
    assert passed["workflow_state"] == str(WorkflowState.PROOF_COMPLETE)
    assert passed["proof"]["status"] == "PROOF_COMPLETE"
    assert passed["proof"]["satisfied_count"] == 7
    assert passed["change_deployed"] is True
    # The historical failure is retained.
    assert [h["result"] for h in passed["deterministic_verdict"]["history"]] == [
        "FAIL",
        "PASS",
    ]


def test_an_inconclusive_observation_blocks_the_proof(runtime: Any) -> None:
    wire_gemma("INCONCLUSIVE")
    workflow_id = inject(runtime)
    status = run_cli(
        "verify", "--workflow-id", workflow_id, "--image", str(AMBIGUOUS_IMG)
    )[1]
    assert status["deterministic_verdict"]["result"] == "INCONCLUSIVE"
    assert status["workflow_state"] == str(WorkflowState.VERIFICATION_INCONCLUSIVE)
    assert status["proof"]["generated"] is False
    assert status["change_deployed"] is False


def test_verify_preserves_actual_byte_mime_authority(runtime: Any) -> None:
    """A HEIC named ``.jpg`` is detected from the bytes, not the claim."""
    wire_gemma("TOP_RIGHT")
    workflow_id = inject(runtime)
    status = run_cli(
        "verify", "--workflow-id", workflow_id, "--image", str(TOP_RIGHT_IMG)
    )[1]
    field = status["field_verification"]
    assert TOP_RIGHT_IMG.suffix == ".jpg"
    assert field["mime_type"] == "image/heic"
    assert field["container"] == "HEIC"
    assert field["declared_content_type"] == "image/jpeg"
    assert field["declared_type_matched_bytes"] is False
    assert field["mime_authority"] == "DERIVED_FROM_BYTES"


def test_verify_accepts_no_expected_value_or_verdict_flag() -> None:
    parser = cli.build_parser()
    verify = next(
        action.choices["verify"]
        for action in parser._actions
        if hasattr(action, "choices") and action.choices and "verify" in action.choices
    )
    options = {opt for action in verify._actions for opt in action.option_strings}
    for banned in (
        "--expected",
        "--observed",
        "--pass",
        "--fail",
        "--verdict",
        "--workflow-state",
        "--proof-complete",
    ):
        assert banned not in options, f"verify accepts {banned}"
    assert options >= {"--workflow-id", "--image"}


def test_an_identical_image_replay_costs_no_additional_provider_call(runtime: Any) -> None:
    wire_gemma("TOP_RIGHT")
    workflow_id = inject(runtime)
    for _ in range(3):
        status = run_cli(
            "verify", "--workflow-id", workflow_id, "--image", str(TOP_RIGHT_IMG)
        )[1]
    assert StubGemma.calls == 1, "an identical resubmission must not be billable"
    assert len(status["deterministic_verdict"]["history"]) == 1


def test_verify_does_not_duplicate_remediation_or_delivery(runtime: Any) -> None:
    wire_gemma("LEFT", "TOP_RIGHT")
    workflow_id = inject(runtime)
    run_cli("verify", "--workflow-id", workflow_id, "--image", str(LEFT_IMG))
    status = run_cli(
        "verify", "--workflow-id", workflow_id, "--image", str(TOP_RIGHT_IMG)
    )[1]
    assert status["remediation"]["request_history"][-1]["dispatch_count"] == 1
    assert status["delivery"]["dispatch_count"] == 1


def test_verify_fails_closed_on_a_malformed_image(runtime: Any) -> None:
    wire_gemma("TOP_RIGHT")
    workflow_id = inject(runtime)
    bogus = REPO_ROOT / "fixtures" / "_tmp_not_an_image.json"
    bogus.write_text("this is not an image at all, merely prose" * 4, encoding="utf-8")
    try:
        code, status, _err = run_cli(
            "verify", "--workflow-id", workflow_id, "--image", str(bogus)
        )
        assert code == cli.EXIT_OK  # the request succeeded; the evidence was refused
        assert status["field_verification"]["rejected"] is True
        assert status["proof"]["generated"] is False
        assert StubGemma.calls == 0, "a refused image must never reach the model"
    finally:
        bogus.unlink()


def test_verify_fails_on_a_missing_image(runtime: Any) -> None:
    workflow_id = inject(runtime)
    code, _payload, err = run_cli(
        "verify", "--workflow-id", workflow_id, "--image", str(REPO_ROOT / "nope.jpg")
    )
    assert code == cli.EXIT_FAILURE
    assert "cannot read image" in err


def test_verify_on_an_unknown_workflow_fails(runtime: Any) -> None:
    code, _payload, err = run_cli(
        "verify", "--workflow-id", "wf-nope", "--image", str(TOP_RIGHT_IMG)
    )
    assert code == cli.EXIT_FAILURE
    assert "no workflow" in err


# ============================ 22-27. proof --validate =================================


def complete_proof(runtime: Any) -> str:
    wire_gemma("TOP_RIGHT")
    workflow_id = inject(runtime)
    run_cli("verify", "--workflow-id", workflow_id, "--image", str(TOP_RIGHT_IMG))
    return workflow_id


def test_proof_validate_uses_the_frozen_validator(runtime: Any) -> None:
    workflow_id = complete_proof(runtime)
    code, result, err = run_cli("proof", "--workflow-id", workflow_id, "--validate")
    assert code == cli.EXIT_OK, err
    assert result["authoritative_validation"] == "VALID"
    assert result["schema_valid"] is True
    assert result["content_hash_valid"] is True
    assert result["proof_identity_valid"] is True
    assert result["proof_invariants_valid"] is True
    assert result["evidence_manifest_valid"] is True
    assert result["satisfied_conditions"] == 7
    assert result["total_conditions"] == 7
    assert result["failures"] == []


def test_proof_validate_reports_the_self_excluding_preimage(runtime: Any) -> None:
    workflow_id = complete_proof(runtime)
    _code, result, _err = run_cli("proof", "--workflow-id", workflow_id, "--validate")
    assert result["hash_preimage"] == "canonical-json-excluding-content_hash"
    assert "excluding its own content_hash" in result["hash_meaning"]
    assert len(result["content_hash"]) == 64


def test_proof_validate_never_claims_signature_or_attestation(runtime: Any) -> None:
    workflow_id = complete_proof(runtime)
    _code, result, _err = run_cli("proof", "--workflow-id", workflow_id, "--validate")
    blob = json.dumps(result).lower()
    for overclaim in (
        "signature_valid",
        "attestation_valid",
        "non_repudiation",
        "trusted_timestamp",
        "blockchain",
    ):
        assert overclaim not in blob


def test_proof_validate_fails_when_no_proof_exists(runtime: Any) -> None:
    wire_gemma("LEFT")
    workflow_id = inject(runtime)
    run_cli("verify", "--workflow-id", workflow_id, "--image", str(LEFT_IMG))
    code, _payload, err = run_cli("proof", "--workflow-id", workflow_id, "--validate")
    assert code == cli.EXIT_FAILURE
    assert "no Change Proof" in err


def test_proof_validate_detects_a_tampered_proof(runtime: Any) -> None:
    """The frozen validator's own hash check, reached through the CLI."""
    workflow_id = complete_proof(runtime)
    service = app_module.get_registry().get(workflow_id)
    session = service._session
    stored = session.proof_store.find_workflow(session.workflow.workflow_id)
    tampered = stored.proof.model_copy(update={"current_value": "LEFT"})
    object.__setattr__(stored, "proof", tampered)

    code, result, _err = run_cli("proof", "--workflow-id", workflow_id, "--validate")
    assert code == cli.EXIT_FAILURE
    assert result["authoritative_validation"] == "INVALID"
    assert result["content_hash_valid"] is False


def test_proof_validation_has_zero_side_effects(runtime: Any) -> None:
    workflow_id = complete_proof(runtime)
    before = run_cli("status", "--workflow-id", workflow_id)[1]
    for _ in range(3):
        run_cli("proof", "--workflow-id", workflow_id, "--validate")
    after = run_cli("status", "--workflow-id", workflow_id)[1]

    assert after["proof"]["content_hash"] == before["proof"]["content_hash"]
    assert after["proof"]["proof_id"] == before["proof"]["proof_id"]
    assert after["delivery"]["dispatch_count"] == before["delivery"]["dispatch_count"]
    assert StubGemma.calls == 1
    assert len(after["deterministic_verdict"]["history"]) == 1


def test_one_canonical_proof_across_repeated_invocations(runtime: Any) -> None:
    workflow_id = complete_proof(runtime)
    service = app_module.get_registry().get(workflow_id)
    for _ in range(3):
        run_cli("verify", "--workflow-id", workflow_id, "--image", str(TOP_RIGHT_IMG))
    assert len(service._session.proof_store) == 1


# ============================ 28-32. registry, security, hygiene ======================


def test_the_registry_is_process_local_and_never_serialised() -> None:
    source = (REPO_ROOT / "src" / "driftzero_console" / "workflows.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {
            "write_text",
            "write_bytes",
            "dump",
            "dumps",
        }:
            assert node.attr != "write_text", "the registry must not touch disk"
    # Docstrings stripped: naming Firestore as the thing this is *not* is a disclaimer,
    # and must not be read as an implementation of it.
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list):
            node.body = [
                c
                for c in body
                if not (
                    isinstance(c, ast.Expr)
                    and isinstance(c.value, ast.Constant)
                    and isinstance(c.value.value, str)
                )
            ] or [ast.Pass()]
    code = ast.unparse(tree).lower()
    for banned in ("sqlite", "pickle", "shelve", "firestore", "open(", "write_text"):
        assert banned not in code, f"workflows.py implements {banned}"


def test_two_workflows_coexist_in_one_runtime(runtime: Any) -> None:
    first = inject(runtime)
    second = inject(runtime)
    assert first != second
    assert len(app_module.get_registry()) == 2
    assert run_cli("status", "--workflow-id", first)[0] == cli.EXIT_OK
    assert run_cli("status", "--workflow-id", second)[0] == cli.EXIT_OK


def test_the_adapter_endpoints_reject_authoritative_client_input(runtime: Any) -> None:
    workflow_id = inject(runtime)
    hostile = {
        "filename": "x.jpg",
        "declared_content_type": "image/jpeg",
        "content_base64": base64.b64encode(TOP_RIGHT_IMG.read_bytes()).decode(),
        "verification_result": "PASS",
        "workflow_state": "PROOF_COMPLETE",
        "proof_id": "forged",
        "affected_artifact_id": "wi-packing-standard-003",
    }
    wire_gemma("LEFT")
    response = runtime.post(
        f"/api/cli/workflows/{workflow_id}/verify", content=json.dumps(hostile)
    )
    assert response.status_code == 200
    body = response.json()
    # Every injected claim was ignored; the real observation decided.
    assert body["deterministic_verdict"]["result"] == "FAIL"
    assert body["workflow_state"] == str(WorkflowState.VERIFICATION_FAILED)
    assert body["proof"]["generated"] is False
    assert body["impact"]["affected_artifact_id"] == "wi-packing-standard-001"


def test_the_fixture_directory_header_cannot_escape_the_repository(runtime: Any) -> None:
    response = runtime.post(
        "/api/cli/workflows",
        content=HERO_FIXTURE.read_text(encoding="utf-8"),
        headers={"X-Fixture-Dir": "../../../etc"},
    )
    assert response.status_code == 400
    assert "out of bounds" in response.json()["detail"]


def test_no_credentials_are_printed(runtime: Any) -> None:
    workflow_id = complete_proof(runtime)
    outputs = [
        json.dumps(run_cli("status", "--workflow-id", workflow_id)[1]),
        json.dumps(run_cli("proof", "--workflow-id", workflow_id, "--validate")[1]),
    ]
    for blob in outputs:
        for secret in (
            "Bearer ",
            "access_token",
            "refresh_token",
            "client_secret",
            "private_key",
            "grant_token",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ):
            assert secret not in blob


def test_the_cli_never_shells_out() -> None:
    tree = ast.parse((REPO_ROOT / "src" / "driftzero" / "cli.py").read_text(encoding="utf-8"))
    code = ast.unparse(tree)
    for shell in ("subprocess", "os.system", "os.popen", "Popen", "gcloud"):
        assert shell not in code, f"cli.py can spawn a process ({shell})"


def test_no_live_provider_is_reachable_from_this_suite() -> None:
    clear_change_intelligence()
    fv.clear_field_observation_provider()
    assert mc.has_model_client_provider() is False
    assert fv.has_field_observation_provider() is False


def test_the_final_live_evidence_is_byte_identical() -> None:
    if not LIVE_PILOT.exists():
        pytest.skip("final live pilot evidence is not present in this checkout")
    expected = "75925f5ecb14d1cfcd1eeee0c3e8f17a8e7d274131e2eb4bc4118a2b91a80af1"
    for name in ("change_proof_DZ-001.json", "change_proof_DZ-001-api.json"):
        raw = (LIVE_PILOT / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected, name


def test_the_cli_exposes_no_hardcoded_pilot_values() -> None:
    literals = {
        node.value
        for node in ast.walk(
            ast.parse((REPO_ROOT / "src" / "driftzero" / "cli.py").read_text(encoding="utf-8"))
        )
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for pilot in (
        "DZ-001",
        "PACKING-SOP",
        "WI-114",
        "LEFT",
        "TOP_RIGHT",
        "gemini-3.5-flash",
        "wi-packing-standard-001",
    ):
        assert pilot not in literals, f"cli.py hardcodes {pilot}"


def test_the_api_base_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["status", "--workflow-id", "wf-1"])
    monkeypatch.delenv(cli.API_BASE_ENV, raising=False)
    assert cli.api_base(args) == cli.DEFAULT_API_BASE

    monkeypatch.setenv(cli.API_BASE_ENV, "http://10.0.0.5:9000/")
    assert cli.api_base(args) == "http://10.0.0.5:9000"

    explicit = parser.parse_args(
        ["--api-base", "http://host:1234/", "status", "--workflow-id", "wf-1"]
    )
    assert cli.api_base(explicit) == "http://host:1234"


def test_an_unreachable_runtime_reports_the_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cli.urllib.request, "urlopen", boom)
    code, _payload, err = run_cli(
        "--api-base", "http://127.0.0.1:1", "status", "--workflow-id", "wf-1"
    )
    assert code == cli.EXIT_FAILURE
    assert "driftzero_console.app" in err
