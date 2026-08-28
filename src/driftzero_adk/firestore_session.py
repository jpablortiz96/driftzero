"""T097 — a durable ADK ``SessionService`` backed by Firestore.

The ADK ships three session services. ``InMemorySessionService`` dies with the process,
which is precisely the limitation this task removes. ``DatabaseSessionService`` wants a
SQL connection string, which would mean provisioning Cloud SQL — a fixed-cost instance
the cost discipline forbids and Firestore already makes unnecessary.
``VertexAiSessionService`` targets Agent Engine, a different product from the Cloud Run
deployment this system actually runs on. So the remaining honest option is the one the
ADK explicitly supports: implement ``BaseSessionService`` against the store we already
have durable.

The concrete ``append_event`` on the base class is what applies ``state_delta`` and temp
state to the session. This subclass calls ``super().append_event`` first, so that
semantics stays the ADK's, and only then persists the result. Reimplementing it here
would fork the ADK's state machine.

Placement: this is outside ``src/driftzero/``, which the M0 purity guard protects. It
imports ``google.adk`` and reaches Firestore through the T092 client. No domain module
imports it, and it knows nothing about workflow state, verdicts or proofs — it stores an
ADK session, not a DRIFTZERO decision.

Layout::

    adk_sessions/{app_name}__{user_id}__{session_id}
    adk_sessions/{app_name}__{user_id}__{session_id}/events/{event_id}
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from google.adk.events import Event
from google.adk.sessions import BaseSessionService, Session
from google.adk.sessions.base_session_service import ListSessionsResponse

from driftzero_cloud.errors import CloudAdapterError
from driftzero_cloud.serialization import safe_identifier

if TYPE_CHECKING:  # pragma: no cover - typing only
    from google.adk.sessions.base_session_service import GetSessionConfig

SESSIONS = "adk_sessions"
EVENTS = "events"

SESSION_SCHEMA_VERSION = 1
"""Bumped only when a stored session or event document changes shape incompatibly.
A reader that meets a version it does not understand refuses rather than guessing."""


def session_key(app_name: str, user_id: str, session_id: str) -> str:
    """One Firestore document id for the ADK's three-part session identity.

    Each part is checked separately, so a value carrying ``/`` or ``..`` is rejected
    before it can be concatenated into something that escapes the collection.
    """
    parts = [
        safe_identifier(app_name, kind="app_name"),
        safe_identifier(user_id, kind="user_id"),
        safe_identifier(session_id, kind="session_id"),
    ]
    return "__".join(parts)


def _encode_session(session: Session) -> dict[str, Any]:
    """Explicit, versioned, JSON-compatible. No pickle, no Python object encoding."""
    return {
        "schema_version": SESSION_SCHEMA_VERSION,
        "kind": "adk_session",
        "id": session.id,
        "app_name": session.app_name,
        "user_id": session.user_id,
        "state": dict(session.state),
        "last_update_time": session.last_update_time,
    }


def _decode_session(document: dict[str, Any], events: list[Event]) -> Session:
    version = document.get("schema_version")
    if version != SESSION_SCHEMA_VERSION:
        raise CloudAdapterError(
            f"unsupported adk_session schema_version {version!r}; this build reads "
            f"{SESSION_SCHEMA_VERSION}"
        )
    return Session(
        id=document["id"],
        app_name=document["app_name"],
        user_id=document["user_id"],
        state=dict(document.get("state") or {}),
        events=events,
        last_update_time=document.get("last_update_time") or 0.0,
    )


def _encode_event(event: Event) -> dict[str, Any]:
    return {
        "schema_version": SESSION_SCHEMA_VERSION,
        "kind": "adk_event",
        "event_id": event.id,
        "invocation_id": event.invocation_id,
        "author": event.author,
        "timestamp": event.timestamp,
        # model_dump(mode="json") keeps this a plain document a human can read and any
        # process can parse; the ADK model validates it back on the way in.
        "payload": event.model_dump(mode="json", exclude_none=True),
    }


def _decode_event(document: dict[str, Any]) -> Event:
    version = document.get("schema_version")
    if version != SESSION_SCHEMA_VERSION:
        raise CloudAdapterError(
            f"unsupported adk_event schema_version {version!r}; this build reads "
            f"{SESSION_SCHEMA_VERSION}"
        )
    return Event.model_validate(document["payload"])


class FirestoreSessionService(BaseSessionService):
    """An ADK session service whose sessions outlive the process that made them."""

    def __init__(self, client: Any) -> None:
        self._client = client

    # ------------------------------------------------------------------ helpers

    def _doc(self, app_name: str, user_id: str, session_id: str) -> Any:
        return self._client.collection(SESSIONS).document(
            session_key(app_name, user_id, session_id)
        )

    def _events_of(self, ref: Any) -> list[Event]:
        """Event chronology, ordered by the timestamp the ADK recorded.

        Firestore streams by document id, and an id is not a clock, so the ordering is
        re-established explicitly. A replayed sequence in the wrong order is a different
        history.
        """
        documents = [snapshot.to_dict() or {} for snapshot in ref.collection(EVENTS).stream()]
        documents.sort(key=lambda d: (d.get("timestamp") or 0.0, str(d.get("event_id"))))
        return [_decode_event(document) for document in documents]

    # ------------------------------------------------------------------ contract

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Session:
        """Create a session, or return the stored one under the same identity.

        Returning the existing session rather than overwriting is what makes a restart
        safe: instance B calls ``create_session`` with the same id and must receive the
        history instance A wrote, not a blank session that silently discards it.
        """
        import time  # noqa: PLC0415

        if not session_id:
            raise CloudAdapterError(
                "a durable session requires an explicit session_id; a generated id "
                "could not be found again after a restart"
            )
        ref = self._doc(app_name, user_id, session_id)
        snapshot = ref.get()
        if snapshot.exists:
            return _decode_session(snapshot.to_dict() or {}, self._events_of(ref))

        session = Session(
            id=session_id,
            app_name=app_name,
            user_id=user_id,
            state=dict(state or {}),
            events=[],
            last_update_time=time.time(),
        )
        ref.set(_encode_session(session))
        return session

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: GetSessionConfig | None = None,
    ) -> Session | None:
        ref = self._doc(app_name, user_id, session_id)
        snapshot = ref.get()
        if not snapshot.exists:
            return None

        events = self._events_of(ref)
        if config is not None:
            after = getattr(config, "after_timestamp", None)
            if after is not None:
                events = [e for e in events if (e.timestamp or 0.0) >= after]
            recent = getattr(config, "num_recent_events", None)
            if recent:
                events = events[-recent:]
        return _decode_session(snapshot.to_dict() or {}, events)

    async def append_event(self, session: Session, event: Event) -> Event:
        """Apply the event through the ADK, then persist both event and session state."""
        event = await super().append_event(session=session, event=event)
        if event.partial:
            # The base class returns partial events without mutating state; persisting
            # them would store a fragment the ADK never considered part of the session.
            return event

        import time  # noqa: PLC0415

        session.last_update_time = time.time()
        ref = self._doc(session.app_name, session.user_id, session.id)
        ref.set(_encode_session(session))
        ref.collection(EVENTS).document(
            safe_identifier(event.id, kind="event_id")
        ).set(_encode_event(event))
        return event

    def persist_state(self, session: Session) -> None:
        """Write the session's state back without appending an event.

        Used to record the resumable invocation identity. It is a state write, not an
        event: inventing an ADK event to carry it would put a record in the chronology
        that the agent never produced.
        """
        self._doc(session.app_name, session.user_id, session.id).set(
            _encode_session(session)
        )

    async def list_sessions(
        self, *, app_name: str, user_id: str | None = None
    ) -> ListSessionsResponse:
        """Session identities only — the ADK's contract for this call excludes events."""
        sessions: list[Session] = []
        for snapshot in self._client.collection(SESSIONS).stream():
            document = snapshot.to_dict() or {}
            if document.get("app_name") != app_name:
                continue
            if user_id is not None and document.get("user_id") != user_id:
                continue
            sessions.append(_decode_session(document, []))
        return ListSessionsResponse(sessions=sessions)

    async def delete_session(self, *, app_name: str, user_id: str, session_id: str) -> None:
        ref = self._doc(app_name, user_id, session_id)
        for snapshot in ref.collection(EVENTS).stream():
            ref.collection(EVENTS).document(
                snapshot.to_dict().get("event_id") or ""
            ).delete()
        ref.delete()
