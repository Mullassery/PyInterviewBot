from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from . import agent, channels, db, documents, providers, rate_limit, report, templates
from .auth import hash_session_token, require_auth
from .models import (
    ChannelMessageRequest,
    ChannelMessageResponse,
    ChannelSessionRequest,
    ChannelSessionResponse,
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
        session_id, question_text, template_id, session_token = await agent.start_session(
            req.template_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StartSessionResponse(
        session_id=session_id,
        question_text=question_text,
        template_id=template_id,
        session_token=session_token,
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


@app.delete("/sessions/{session_id}", dependencies=[Depends(require_auth)])
async def delete_session(session_id: UUID) -> dict:
    """Delete a session and its transcript/evidence (retention)."""
    deleted = await db.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="session not found")
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
        template = templates.get_template(row["template_id"])
    except ValueError:
        template = templates.get_template()
    return report.build_report(
        session_id=session_id,
        template=template,
        transcript=await db.get_transcript(session_id),
        evidence=await db.get_evidence(session_id),
        reviews=await db.get_reviews(session_id),
    )


@app.post(
    "/sessions/{session_id}/review",
    dependencies=[Depends(require_auth)],
)
async def review_evidence(session_id: UUID, req: ReviewRequest) -> dict:
    row = await db.get_session_row(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    template = templates.get_template(row["template_id"])
    if req.node not in {node.id for node in template.nodes}:
        raise HTTPException(status_code=400, detail=f"unknown evidence node {req.node}")
    await db.record_review(
        session_id, req.node, req.reviewer, req.decision, req.note
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
