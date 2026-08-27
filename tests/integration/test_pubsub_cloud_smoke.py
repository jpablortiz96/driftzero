"""T095 — publisher connectivity against the real Pub/Sub topic.

Skipped unless ``DRIFTZERO_CLOUD_SMOKE=1``.

Deliberately narrow. It confirms the topic exists, that this identity may publish to it,
and that a uniquely namespaced message is accepted — nothing more.

It is safe precisely because the topic has **no subscription**: a published message has
no consumer and is discarded by Pub/Sub, so nothing can be delivered anywhere and no
production state can change. The test asserts that precondition rather than assuming it,
and skips if a subscription has appeared.

The end-to-end push path is deliberately *not* smoked here. Proving it needs a push
subscription pointing at a deployed Cloud Run URL, which is T096 followed by T089.
Creating a temporary subscriber to tick a box would mean standing up an unauthenticated
consumer of approved changes, which is a worse trade than waiting two tasks.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest

PROJECT = "driftzero-runtime-2026"
TOPIC = "driftzero-approved-changes"

pytestmark = pytest.mark.skipif(
    os.environ.get("DRIFTZERO_CLOUD_SMOKE") != "1",
    reason="set DRIFTZERO_CLOUD_SMOKE=1 to run smoke tests against real Google Cloud",
)


@pytest.fixture(scope="module")
def publisher() -> Any:
    from google.cloud import pubsub_v1

    return pubsub_v1.PublisherClient()


@pytest.fixture(scope="module")
def topic_path(publisher: Any) -> str:
    return publisher.topic_path(PROJECT, TOPIC)


def test_the_topic_exists_and_targets_the_runtime_project(
    publisher: Any, topic_path: str
) -> None:
    topic = publisher.get_topic(request={"topic": topic_path})
    assert topic.name == f"projects/{PROJECT}/topics/{TOPIC}"
    assert "driftzero-agentic-2026" not in topic.name


def test_the_topic_still_has_no_subscription(publisher: Any, topic_path: str) -> None:
    """The safety precondition for publishing at all — asserted, not assumed."""
    subscriptions = list(publisher.list_topic_subscriptions(request={"topic": topic_path}))
    assert subscriptions == [], (
        "a subscription now exists, so a published test message could reach a consumer; "
        "move this smoke to the T096/T089 end-to-end path instead"
    )


def test_a_namespaced_message_can_be_published(publisher: Any, topic_path: str) -> None:
    """Publisher connectivity and IAM. The message has no consumer and is discarded."""
    subscriptions = list(publisher.list_topic_subscriptions(request={"topic": topic_path}))
    if subscriptions:  # pragma: no cover - guarded again at call time
        pytest.skip("a consumer now exists; publishing is no longer inert")

    payload = {
        "_smoke": f"smoke-{uuid.uuid4().hex[:12]}",
        "_note": "connectivity probe only; no subscription exists, so this is discarded",
    }
    future = publisher.publish(topic_path, json.dumps(payload).encode("utf-8"))
    message_id = future.result(timeout=30)
    assert message_id, "Pub/Sub accepted the publish but returned no message id"
