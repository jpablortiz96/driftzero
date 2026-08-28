"""T094 — the FastAPI routes from contracts/agents.md § API Contract.

Five routes, exactly as the contract names them::

    POST /api/v1/changes
    GET  /api/v1/workflows/{workflow_id}
    POST /api/v1/workflows/{workflow_id}/verify      (multipart, carries submission_id)
    GET  /api/v1/workflows/{workflow_id}/proof
    GET  /api/v1/workflows/{workflow_id}/evidence

plus liveness and readiness, which a Cloud Run deployment needs and which report
configuration rather than aspiration.

The task line writes the last four as ``/workflows/{id}``; contracts/agents.md — which
the task names as the source — writes them under ``/api/v1/``. The contract wins, and
the whole surface is versioned consistently.

Every route is transport. Not one of them decides anything: impact qualification,
authorization, state transitions, the verification comparator, proof invariants and
hash semantics all stay where they already are. A request that tries to state a
conclusion is refused with the offending fields named.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import ValidationError

from driftzero_api.models import (
    ApprovedChangeRequest,
    ChangeAccepted,
    EvidenceListing,
    Health,
    Readiness,
    VerificationResponse,
    WorkflowStatus,
)
from driftzero_api.runtime import (
    ApiRuntime,
    NotResumable,
    ResumeHeldElsewhere,
    WorkflowNotFound,
)
from driftzero_console.workflows import FORBIDDEN_FIXTURE_KEYS, FixtureRejected

router = APIRouter()


def get_runtime(request: Request) -> ApiRuntime:
    """The runtime this app was composed with. Never constructed per request."""
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - a misconfigured app, not a request error
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="the application was created without a runtime",
        )
    return runtime


Runtime = Annotated[ApiRuntime, Depends(get_runtime)]


def _refuse_conclusions(payload: dict[str, Any]) -> None:
    """Refuse a request that tries to submit an answer, naming what it tried.

    ``ApprovedChangeRequest`` already forbids unknown fields, so this is the second
    line rather than the only one. It exists because a caller who sends
    ``verification_result: PASS`` deserves to be told that specifically, not handed a
    generic schema error that leaves them guessing.
    """
    offending = sorted(set(payload) & FORBIDDEN_FIXTURE_KEYS)
    if offending:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "AUTHORITATIVE_FIELD_REFUSED",
                "detail": (
                    "a request may describe what changed at the source; it may never "
                    "state what the system concluded"
                ),
                "refused_fields": offending,
            },
        )


# ============================ operational =============================================


@router.get("/health", response_model=Health, tags=["operational"])
def health() -> Health:
    """Liveness only: this process is running and can answer."""
    return Health()


@router.get("/ready", response_model=Readiness, tags=["operational"])
def ready(runtime: Runtime) -> Readiness:
    """Readiness reflects real configuration.

    ``deployment`` stays ``NOT_DEPLOYED`` because deployment is T096. A durable backend
    being configured is a true statement about this process; running on Cloud Run is
    not, and the two must not be conflated.
    """
    return Readiness(**runtime.readiness())


# ============================ T094 contract routes ====================================


@router.post(
    "/api/v1/changes",
    response_model=ChangeAccepted,
    status_code=status.HTTP_201_CREATED,
    tags=["changes"],
)
async def submit_change(
    request: Request, response: Response, runtime: Runtime
) -> ChangeAccepted:
    """Publish an approved change event.

    The raw body is inspected before model validation so a refused authoritative field
    can be named precisely. Duplicate ``change_id`` resolves to the existing workflow —
    T029's decision — rather than starting a second logical execution.
    """
    try:
        raw = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "MALFORMED_JSON", "detail": str(exc)[:200]},
        ) from exc

    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "MALFORMED_BODY", "detail": "the body must be a JSON object"},
        )

    _refuse_conclusions(raw)
    try:
        payload = ApprovedChangeRequest.model_validate(raw)
    except ValidationError as exc:
        # Validating by hand (so conclusions can be named first) means FastAPI never
        # sees the error, so it must be converted here rather than escaping as a 500.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(include_url=False),
        ) from exc

    try:
        accepted = runtime.accept_change(payload.to_fixture())
    except FixtureRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "FIXTURE_REJECTED", "detail": str(exc)},
        ) from exc

    if accepted["duplicate_of"] is not None:
        # 201 would claim a workflow was created. A transport duplicate resolves to the
        # one that already exists and creates nothing.
        response.status_code = status.HTTP_200_OK

    return ChangeAccepted(
        workflow_id=accepted["workflow_id"],
        state=accepted["state"],
        duplicate_of=accepted["duplicate_of"],
    )


@router.get(
    "/api/v1/workflows/{workflow_id}",
    response_model=WorkflowStatus,
    tags=["workflows"],
)
def read_workflow(workflow_id: str, runtime: Runtime) -> WorkflowStatus:
    """Current workflow state and evidence summary.

    Resolves from the live runtime when the workflow is still held there, otherwise
    from durable storage. An id neither knows is a 404 — never a fabricated default.
    """
    try:
        return WorkflowStatus(**runtime.status(workflow_id))
    except WorkflowNotFound as exc:
        raise _not_found(workflow_id) from exc


@router.post(
    "/api/v1/workflows/{workflow_id}/verify",
    response_model=VerificationResponse,
    tags=["workflows"],
)
async def verify_workflow(
    workflow_id: str,
    runtime: Runtime,
    file: Annotated[UploadFile, File(description="Field evidence image")],
    submission_id: Annotated[str | None, Form()] = None,  # a claim; see below
) -> VerificationResponse:
    """Submit field verification evidence as multipart, carrying ``submission_id``.

    The route transports bytes. It does not observe, normalize, or compare: the field
    provider observes and the frozen deterministic comparator decides PASS/FAIL.

    The contract says the request *carries* ``submission_id``, and it does — but as a
    client **claim**, exactly like the filename and browser Content-Type beside it. The
    authoritative submission identity is derived server-side from the image bytes, so a
    client cannot choose it. Letting them would hand the caller control of the
    idempotency key: a replayed id could suppress a genuine new observation, and a
    fresh one could force a duplicate event for identical evidence.
    """
    service = _live_or_404(runtime, workflow_id)
    raw = await file.read()

    state = service.submit_field_evidence(
        raw,
        declared_filename=file.filename,
        declared_content_type=file.content_type,
    )
    verification = state.get("field_verification") or {}
    workflow = service._session.workflow
    verdict = workflow.latest_verification_status

    return VerificationResponse(
        # The comparator's verdict, never the observation status. An observation that
        # failed to be obtained is INCONCLUSIVE, not a PASS or a FAIL.
        verification_result=str(verdict) if verdict is not None else "INCONCLUSIVE",
        workflow_state=str(workflow.state),
        # Server-derived. A differing client claim is ignored, not honoured.
        submission_id=verification.get("submission_id"),
        accepted=not bool(verification.get("rejected")),
        rejection_reason=verification.get("rejection_reason"),
    )


@router.get("/api/v1/workflows/{workflow_id}/proof", tags=["workflows"])
def read_proof(workflow_id: str, runtime: Runtime) -> dict[str, Any]:
    """The completed Change Proof, or 404 if the workflow has not earned one.

    Returned verbatim as the Truth Engine stored it. The route does not recompute the
    content hash, and does not re-render the canonical bytes.
    """
    try:
        document = runtime.proof_document(workflow_id)
    except WorkflowNotFound as exc:
        raise _not_found(workflow_id) from exc

    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "PROOF_NOT_COMPLETE",
                "detail": (
                    f"workflow {workflow_id} has not produced a Change Proof; a proof "
                    "exists only once all seven completion conditions hold"
                ),
            },
        )
    return document


@router.get(
    "/api/v1/workflows/{workflow_id}/evidence",
    response_model=EvidenceListing,
    tags=["workflows"],
)
def read_evidence(workflow_id: str, runtime: Runtime) -> EvidenceListing:
    """List the evidence artifacts recorded for a workflow."""
    try:
        return EvidenceListing(**runtime.evidence(workflow_id))
    except WorkflowNotFound as exc:
        raise _not_found(workflow_id) from exc


# ============================ helpers =================================================


def _not_found(workflow_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "error": "WORKFLOW_NOT_FOUND",
            "detail": f"no workflow {workflow_id!r} in this runtime or in durable storage",
        },
    )


def _live_or_404(runtime: ApiRuntime, workflow_id: str) -> Any:
    """A workflow that can still be driven, or an honest refusal.

    Since T097 a workflow held only in durable storage is *resumed* rather than refused:
    the same logical execution is reattached under an exclusive lease. The refusals that
    remain are the ones that should remain — a terminal workflow, a workflow parked at a
    human-review gate, an unreadable stored record, and a workflow another instance is
    already resuming.
    """
    try:
        return runtime.resume(workflow_id)
    except NotResumable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "WORKFLOW_NOT_RESUMABLE", "detail": exc.reason},
        ) from exc
    except ResumeHeldElsewhere as exc:
        # Another instance owns the resume. Retrying later is correct; executing here
        # anyway would run the same workflow twice.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "RESUME_IN_PROGRESS_ELSEWHERE",
                "detail": exc.detail,
                "holder": exc.holder,
            },
        ) from exc
    except WorkflowNotFound as exc:
        raise _not_found(workflow_id) from exc
