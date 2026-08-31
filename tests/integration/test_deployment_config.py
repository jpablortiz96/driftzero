"""T096 — the container and deployment configuration, validated offline.

These assertions are about the artefacts in the repository, so they run in CI without
credentials. What only the cloud can prove — that the revision is READY, that
unauthenticated calls are refused, that push delivery arrives — is proven by the
deployment evidence bundle and by ``test_cloud_run_smoke.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dockerignore() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


# ============================ the image ===============================================


def test_the_dockerfile_exists_and_targets_the_supported_python(dockerfile: str) -> None:
    assert DOCKERFILE.is_file()
    assert "python:3.13-slim" in dockerfile


def test_the_image_installs_only_the_api_and_cloud_extras(dockerfile: str) -> None:
    """Not the console, and not the live model extras — this serves the API."""
    assert '".[api,cloud,live]"' in dockerfile or '"driftzero[api,cloud,live]"' in dockerfile
    assert "[console]" not in dockerfile
    assert "[live]" not in dockerfile


def test_the_entrypoint_lets_cloud_run_choose_the_port(dockerfile: str) -> None:
    """Cloud Run supplies $PORT at start time; exec form would pass it literally."""
    command = next(line for line in dockerfile.splitlines() if line.startswith("CMD"))
    assert "uvicorn driftzero_api.app:app" in command
    assert "--host 0.0.0.0" in command
    assert "${PORT}" in command
    assert not command.startswith("CMD ["), "exec form would not expand ${PORT}"


def test_the_container_runs_as_a_non_root_user(dockerfile: str) -> None:
    assert re.search(r"^USER driftzero$", dockerfile, re.MULTILINE)
    assert "useradd" in dockerfile
    user_line = dockerfile.index("USER driftzero")
    assert dockerfile.index("CMD") > user_line, "CMD must run after dropping privileges"


def test_the_runtime_stage_ships_no_source_tree(dockerfile: str) -> None:
    """The wheel is installed by name; copying src/ into the final stage would be dead
    weight and would let an editable-install mistake ship uncompiled sources."""
    runtime = dockerfile[dockerfile.index("AS runtime") :]
    assert "COPY src/" not in runtime
    assert 'find-links=/wheels "driftzero[api,cloud,live]"' in runtime


def test_only_the_source_corpus_fixtures_are_shipped(dockerfile: str) -> None:
    """The runtime globs top-level fixtures/*.json; multimodal/ is test input."""
    assert "COPY fixtures/*.json" in dockerfile
    assert "COPY fixtures/ " not in dockerfile
    assert "COPY fixtures ." not in dockerfile


def test_no_credential_can_reach_the_image(dockerignore: list[str]) -> None:
    for pattern in (
        ".env",
        "**/application_default_credentials.json",
        "**/service_account*.json",
        "**/*.pem",
        "**/*.key",
    ):
        assert pattern in dockerignore, f".dockerignore is missing {pattern!r}"


def test_the_image_excludes_evidence_tests_and_git(dockerignore: list[str]) -> None:
    for pattern in ("evidence/", "tests/", ".git", "fixtures/multimodal/"):
        assert pattern in dockerignore, f".dockerignore is missing {pattern!r}"


def test_the_dockerfile_embeds_no_secret(dockerfile: str) -> None:
    for marker in ("ya29.", "AIza", "BEGIN PRIVATE KEY", "service_account.json"):
        assert marker not in dockerfile
    # No credential is baked in as configuration either.
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in dockerfile


def test_the_image_sets_only_non_secret_configuration(dockerfile: str) -> None:
    env_lines = [line for line in dockerfile.splitlines() if line.startswith("ENV ")]
    joined = " ".join(env_lines)
    for secret_shaped in ("TOKEN", "PASSWORD", "SECRET", "CREDENTIAL"):
        assert secret_shaped not in joined.upper(), f"{secret_shaped} in a build-time ENV"


# ============================ the deployed configuration ==============================


def test_the_api_package_declares_its_own_dependencies() -> None:
    """driftzero_api must not inherit FastAPI from the console extra by coincidence."""
    import tomllib

    extras = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    api = extras["project"]["optional-dependencies"]["api"]
    joined = " ".join(api)
    assert "fastapi" in joined
    assert "uvicorn" in joined
    assert "python-multipart" in joined, "multipart verify uploads need this at runtime"


def test_the_core_install_still_pulls_no_cloud_dependency() -> None:
    import tomllib

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"] == ["pydantic>=2.0"], (
        "installing driftzero without extras must not pull FastAPI or a Google SDK"
    )


def test_readiness_reports_deployment_from_the_cloud_run_contract() -> None:
    """K_SERVICE is set only by Cloud Run. Configuration alone must not imply a deploy."""
    from driftzero_api.runtime import ApiRuntime

    runtime = ApiRuntime(fixtures_dir=REPO_ROOT / "fixtures")
    assert runtime.readiness()["deployment"] == "NOT_DEPLOYED"

    import os

    previous = os.environ.get("K_SERVICE"), os.environ.get("K_REVISION")
    os.environ["K_SERVICE"] = "driftzero-api"
    os.environ["K_REVISION"] = "driftzero-api-00001-abc"
    try:
        ready = runtime.readiness()
        assert ready["deployment"] == "CLOUD_RUN"
        assert ready["revision"] == "driftzero-api-00001-abc"
        # Deployed but not durable is still LOCAL_PILOT: a deploy is not persistence.
        assert ready["runtime_mode"] == "LOCAL_PILOT"
    finally:
        for key, value in zip(("K_SERVICE", "K_REVISION"), previous, strict=True):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_a_deployment_never_claims_production_readiness() -> None:
    from driftzero_api.runtime import ApiRuntime

    runtime = ApiRuntime(fixtures_dir=REPO_ROOT / "fixtures")
    ready = runtime.readiness()
    assert ready["production_ready"] is False
    assert any("pilot fixtures" in item for item in ready["pilot_limitations"])


def test_the_pilot_limitations_are_stated_not_hidden() -> None:
    """A deployed pilot must say what is still pilot-shaped about it."""
    import os

    from driftzero_api.runtime import ApiRuntime

    previous = os.environ.get("K_SERVICE")
    os.environ["K_SERVICE"] = "driftzero-api"
    try:
        limitations = ApiRuntime(fixtures_dir=REPO_ROOT / "fixtures").readiness()[
            "pilot_limitations"
        ]
        assert any("source registry" in item for item in limitations)
        # The worker surface sits behind the same IAM boundary as the API. This replaced
        # a stale claim that a recovered workflow could not be resumed in a new instance:
        # resumption is implemented and evidenced in restart_recovery.json, so continuing
        # to publish it would have been a limitation the product no longer has.
        assert any("worker" in item and "mediated" in item for item in limitations)
        assert not any("T097" in item for item in limitations), (
            "readiness must not cite an internal task id to a reader"
        )
        assert not any("not resumed" in item for item in limitations)
    finally:
        if previous is None:
            os.environ.pop("K_SERVICE", None)
        else:
            os.environ["K_SERVICE"] = previous
