from __future__ import annotations

import hmac
import os
import re
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from . import (
    agent,
    channels,
    db,
    documents,
    providers,
    rate_limit,
    report,
    reporting,
    resume_policy,
    templates,
    workflow_routing,
)
from .auth import Principal, hash_session_token, require_auth
from .models import (
    ChannelMessageRequest,
    ChannelMessageResponse,
    ChannelSessionRequest,
    ChannelSessionResponse,
    ComplianceCategoryUpdate,
    ComplianceCategoryView,
    CompliancePolicyResponse,
    PrioritiesResponse,
    PrioritySetting,
    PriorityUpdateRequest,
    ResumePolicyRequest,
    ResumePolicyResponse,
    ResumeResponse,
    ReviewRequest,
    SessionListItem,
    SessionReport,
    SessionSummary,
    StartSessionRequest,
    StartSessionResponse,
    SynthesizeRequest,
    TranscribeResponse,
    TurnRequest,
    TurnResponse,
)
from .providers import ProviderError
from .workflow_api import router as workflow_router

# Default to the candidate-client's actual dev server origins (Vite's
# default port, on both `localhost` and `127.0.0.1` since browsers treat
# them as distinct origins). Override with a comma-separated list via
# ALLOWED_ORIGINS for any other deployment (e.g. a different dev port or a
# built/served client). Never falls back to "*" -- combined with zero auth,
# a wildcard would let any page a user has open read transcripts/evidence
# from this service if the port is reachable (see SECURITY.md).
_DEFAULT_ALLOWED_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", _DEFAULT_ALLOWED_ORIGINS).split(",")
    if origin.strip()
]
MAX_AUDIO_BYTES = int(os.environ.get("PYINTERVIEWBOT_MAX_AUDIO_BYTES", str(10 * 1024 * 1024)))
limiter = rate_limit.build_limiter()


def _require_session_token() -> bool:
    return os.environ.get("PYINTERVIEWBOT_REQUIRE_SESSION_TOKEN", "").lower() in {
        "1",
        "true",
        "yes",
    }


def _rate_limit_key(request: Request) -> str:
    api_key = request.headers.get("x-api-key")
    if api_key:
        return f"key:{api_key}"
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return f"key:{authorization[7:].strip()}"
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await db.init_db()
    yield


app = FastAPI(title="PyInterviewBot ai-service", lifespan=lifespan)
app.include_router(workflow_router)


@app.exception_handler(ProviderError)
async def provider_error_handler(_request: Request, exc: ProviderError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": exc.message,
            "provider": exc.provider,
            "retryable": exc.retryable,
        },
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    allowed, retry_after = limiter.allow(_rate_limit_key(request))
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "rate limit exceeded; retry later"},
            headers={"Retry-After": str(retry_after)},
        )
    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/sessions",
    response_model=StartSessionResponse,
    dependencies=[Depends(require_auth)],
)
async def create_session(req: StartSessionRequest) -> StartSessionResponse:
    try:
        result = await agent.start_session(req.template_id, req.workflow_id)
    except workflow_routing.WorkflowRoutingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StartSessionResponse(
        session_id=result.session_id,
        question_text=result.question_text,
        template_id=result.template_id,
        session_token=result.session_token,
        workflow_id=result.workflow_id,
        workflow_version=result.workflow_version,
        workflow_deployment_id=result.workflow_deployment_id,
    )


@app.post(
    "/sessions/{session_id}/turn",
    response_model=TurnResponse,
    dependencies=[Depends(require_auth)],
)
async def turn(
    session_id: UUID,
    req: TurnRequest,
    x_session_token: str | None = Header(default=None),
) -> TurnResponse:
    row = await db.get_session_row(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    stored_hash = (row["token_hash"] or "").strip()
    if x_session_token:
        if not stored_hash or not hmac.compare_digest(
            hash_session_token(x_session_token), stored_hash
        ):
            raise HTTPException(status_code=403, detail="invalid session token")
    elif _require_session_token():
        raise HTTPException(status_code=403, detail="session token required")
    try:
        return await agent.take_turn(session_id, req.transcript)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/channels/{channel}/sessions",
    response_model=ChannelSessionResponse,
    dependencies=[Depends(require_auth)],
)
async def channel_session(channel: str, req: ChannelSessionRequest) -> ChannelSessionResponse:
    try:
        return await channels.start_or_resume(channel, req.external_id, req.template_id)
    except channels.ChannelError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post(
    "/channels/{channel}/messages",
    response_model=ChannelMessageResponse,
    dependencies=[Depends(require_auth)],
)
async def channel_message(channel: str, req: ChannelMessageRequest) -> ChannelMessageResponse:
    try:
        return await channels.submit_message(
            channel,
            req.external_id,
            req.event_id,
            req.text,
            req.template_id,
        )
    except channels.ChannelError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get(
    "/sessions",
    response_model=list[SessionListItem],
    dependencies=[Depends(require_auth)],
)
async def list_sessions() -> list[SessionListItem]:
    return await db.list_sessions()


@app.delete("/sessions/{session_id}")
async def delete_session(
    session_id: UUID,
    principal: Annotated[Principal, Depends(require_auth)],
) -> dict:
    """Delete a session and its transcript/evidence (retention)."""
    deleted = await db.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="session not found")
    await db.record_ai_audit(
        action="session.delete",
        actor_subject=principal.subject,
        target_type="session",
        target_id=str(session_id),
    )
    return {"deleted": True, "session_id": str(session_id)}


@app.get(
    "/sessions/{session_id}/summary",
    response_model=SessionSummary,
    dependencies=[Depends(require_auth)],
)
async def summary(session_id: UUID) -> SessionSummary:
    row = await db.get_session_row(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    return SessionSummary(
        session_id=session_id,
        template_id=row["template_id"] or "rag_architect",
        resume_claim=row["resume_claim"],
        is_complete=bool(row["is_complete"]),
        transcript=await db.get_transcript(session_id),
        evidence=await db.get_evidence(session_id),
    )


@app.get(
    "/sessions/{session_id}/report",
    response_model=SessionReport,
    dependencies=[Depends(require_auth)],
)
async def session_report(session_id: UUID) -> SessionReport:
    row = await db.get_session_row(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    try:
        template = workflow_routing.session_template(row)
    except ValueError:
        template = templates.get_template()
    return report.build_report(
        session_id=session_id,
        template=template,
        transcript=await db.get_transcript(session_id),
        evidence=await db.get_evidence(session_id),
        reviews=await db.get_reviews(session_id),
        priorities=await db.get_session_priorities(session_id),
        is_complete=bool(row["is_complete"]),
    )


@app.get("/sessions/{session_id}/report/export")
async def export_report(
    session_id: UUID,
    principal: Annotated[Principal, Depends(require_auth)],
    format: str = Query(default="markdown"),
) -> Response:
    key = format.strip().lower()
    if key not in reporting.RENDERERS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported format '{format}'; use markdown, html, csv, or json",
        )
    row = await db.get_session_row(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    try:
        template = workflow_routing.session_template(row)
    except ValueError:
        template = templates.get_template()
    transcript = await db.get_transcript(session_id)
    evidence = await db.get_evidence(session_id)
    built = report.build_report(
        session_id=session_id,
        template=template,
        transcript=transcript,
        evidence=evidence,
        reviews=await db.get_reviews(session_id),
        priorities=await db.get_session_priorities(session_id),
        is_complete=bool(row["is_complete"]),
    )
    summary = SessionSummary(
        session_id=session_id,
        template_id=row["template_id"] or "rag_architect",
        resume_claim=row["resume_claim"],
        is_complete=bool(row["is_complete"]),
        transcript=transcript,
        evidence=evidence,
    )
    renderer, media_type, extension = reporting.RENDERERS[key]
    content = renderer(built, summary)
    await db.record_ai_audit(
        action="report.export",
        actor_subject=principal.subject,
        target_type="session",
        target_id=str(session_id),
        details={"format": key},
    )
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="interview-report-{session_id}.{extension}"',
            "Cache-Control": "no-store",
        },
    )


def _compliance_response(rows: list[dict]) -> CompliancePolicyResponse:
    return CompliancePolicyResponse(
        categories=[
            ComplianceCategoryView(
                id=row["category"],
                label=row["label"],
                description=row["description"],
                enabled=row["enabled"],
                patterns=row["patterns"],
                updated_at=row.get("updated_at"),
            )
            for row in rows
        ]
    )


@app.get(
    "/sessions/{session_id}/resume-policy",
    response_model=ResumePolicyResponse,
    dependencies=[Depends(require_auth)],
)
async def get_resume_policy(session_id: UUID) -> ResumePolicyResponse:
    row = await db.get_session_row(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    return resume_policy.policy_for(row)


@app.put(
    "/sessions/{session_id}/resume-policy",
    response_model=ResumePolicyResponse,
    dependencies=[Depends(require_auth)],
)
async def set_resume_policy(
    session_id: UUID, req: ResumePolicyRequest
) -> ResumePolicyResponse:
    if not await db.set_resume_policy(session_id, req.enabled, req.window_minutes):
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    row = await db.get_session_row(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    return resume_policy.policy_for(row)


@app.post(
    "/sessions/{session_id}/resume",
    response_model=ResumeResponse,
    dependencies=[Depends(require_auth)],
)
async def resume_interview(
    session_id: UUID,
    x_session_token: str | None = Header(default=None),
) -> ResumeResponse:
    try:
        return await resume_policy.resume_session(session_id, x_session_token)
    except resume_policy.ResumeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get(
    "/compliance/policy",
    response_model=CompliancePolicyResponse,
    dependencies=[Depends(require_auth)],
)
async def get_compliance_policy() -> CompliancePolicyResponse:
    return _compliance_response(await db.list_compliance_categories())


@app.put(
    "/compliance/policy/{category}",
    response_model=CompliancePolicyResponse,
    dependencies=[Depends(require_auth)],
)
async def update_compliance_policy(
    category: str, req: ComplianceCategoryUpdate
) -> CompliancePolicyResponse:
    existing = {row["category"] for row in await db.list_compliance_categories()}
    if category not in existing:
        raise HTTPException(status_code=404, detail=f"unknown compliance category {category}")
    patterns = req.patterns
    if patterns is not None:
        cleaned = [pattern.strip() for pattern in patterns if pattern.strip()]
        if not cleaned:
            raise HTTPException(status_code=400, detail="patterns cannot be empty")
        if any(len(pattern) > 200 for pattern in cleaned):
            raise HTTPException(status_code=400, detail="each pattern must be <= 200 characters")
        for pattern in cleaned:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise HTTPException(
                    status_code=400, detail=f"invalid regex '{pattern}': {exc}"
                ) from exc
        patterns = cleaned
    updated = await db.update_compliance_category(
        category,
        label=req.label,
        description=req.description,
        enabled=req.enabled,
        patterns=patterns,
    )
    if not updated:
        raise HTTPException(status_code=404, detail=f"unknown compliance category {category}")
    return _compliance_response(await db.list_compliance_categories())


@app.post(
    "/compliance/policy/reset",
    response_model=CompliancePolicyResponse,
    dependencies=[Depends(require_auth)],
)
async def reset_compliance_policy() -> CompliancePolicyResponse:
    await db.reset_compliance_policy()
    return _compliance_response(await db.list_compliance_categories())


@app.get(
    "/sessions/{session_id}/priorities",
    response_model=PrioritiesResponse,
    dependencies=[Depends(require_auth)],
)
async def get_priorities(session_id: UUID) -> PrioritiesResponse:
    row = await db.get_session_row(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    template = workflow_routing.session_template(row)
    stored = {item.node: item for item in await db.get_session_priorities(session_id)}
    priorities = [
        stored.get(node.id)
        or PrioritySetting(node=node.id, priority="important", weight=2)
        for node in template.nodes
    ]
    return PrioritiesResponse(session_id=session_id, priorities=priorities)


@app.put(
    "/sessions/{session_id}/priorities",
    response_model=PrioritiesResponse,
    dependencies=[Depends(require_auth)],
)
async def set_priorities(
    session_id: UUID, req: PriorityUpdateRequest
) -> PrioritiesResponse:
    row = await db.get_session_row(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    template = workflow_routing.session_template(row)
    valid_nodes = {node.id for node in template.nodes}
    seen: set[str] = set()
    cleaned: list[PrioritySetting] = []
    for item in req.priorities:
        if item.node not in valid_nodes:
            raise HTTPException(status_code=400, detail=f"unknown evidence node {item.node}")
        if item.node in seen:
            raise HTTPException(status_code=400, detail=f"duplicate priority for {item.node}")
        seen.add(item.node)
        cleaned.append(item)
    await db.set_session_priorities(session_id, cleaned)
    stored = {item.node: item for item in await db.get_session_priorities(session_id)}
    priorities = [
        stored.get(node.id)
        or PrioritySetting(node=node.id, priority="important", weight=2)
        for node in template.nodes
    ]
    return PrioritiesResponse(session_id=session_id, priorities=priorities)


@app.post("/sessions/{session_id}/review")
async def review_evidence(
    session_id: UUID,
    req: ReviewRequest,
    principal: Annotated[Principal, Depends(require_auth)],
) -> dict:
    row = await db.get_session_row(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    template = workflow_routing.session_template(row)
    if req.node not in {node.id for node in template.nodes}:
        raise HTTPException(status_code=400, detail=f"unknown evidence node {req.node}")
    await db.record_review(
        session_id, req.node, req.reviewer, req.decision, req.note
    )
    await db.record_ai_audit(
        action="session.review",
        actor_subject=principal.subject,
        target_type="session",
        target_id=str(session_id),
        details={
            "node": req.node,
            "decision": req.decision,
            "reviewer": req.reviewer,
        },
    )
    return {"recorded": True, "session_id": str(session_id), "node": req.node}


@app.get("/templates", dependencies=[Depends(require_auth)])
async def list_templates() -> list[dict[str, str]]:
    return templates.list_templates()


@app.get("/providers", dependencies=[Depends(require_auth)])
async def list_providers() -> dict:
    return {
        "configured": providers.configured_providers(),
        "status": await providers.provider_status(),
    }


@app.post("/documents/parse", dependencies=[Depends(require_auth)])
async def parse_document(req: dict) -> dict:
    """Parse resume/JD text and return a deterministic competency rubric."""
    text = str(req.get("text") or "")
    kind = str(req.get("kind") or "resume")
    if req.get("job_description"):
        rubric = documents.build_rubric(str(req["job_description"]), text)
        parsed = documents.parse_document(text, kind=kind)
        return {"document": {"skills": parsed.skills, "kind": kind}, **rubric.to_dict()}
    parsed = documents.parse_document(text, kind=kind)
    return {"document": {"skills": parsed.skills, "kind": kind}}


@app.post(
    "/asr/transcribe",
    response_model=TranscribeResponse,
    dependencies=[Depends(require_auth)],
)
async def transcribe(request: Request) -> TranscribeResponse:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio payload too large")
    pcm16 = await request.body()
    if len(pcm16) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio payload too large")
    text = await providers.get_asr().transcribe(pcm16)
    return TranscribeResponse(transcript=text.text)


@app.post(
    "/asr/transcribe-media",
    response_model=TranscribeResponse,
    dependencies=[Depends(require_auth)],
)
async def transcribe_media(request: Request) -> TranscribeResponse:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio payload too large")
    body = await request.body()
    if len(body) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio payload too large")
    suffix = request.headers.get("x-filename-suffix", ".ogg")
    text = await providers.transcribe_media(body, suffix)
    return TranscribeResponse(transcript=text)


@app.post("/tts/synthesize", dependencies=[Depends(require_auth)])
async def synthesize(req: SynthesizeRequest) -> StreamingResponse:
    provider = providers.get_tts()
    return StreamingResponse(
        provider.synthesize(req.text),
        media_type=f"audio/L16;rate={provider.sample_rate};channels=1",
    )


def main() -> None:
    import uvicorn

    uvicorn.run("pyinterviewbot_ai.main:app", host="127.0.0.1", port=8000, reload=False)
