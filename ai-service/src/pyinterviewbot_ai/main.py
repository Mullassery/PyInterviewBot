from __future__ import annotations

import os
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from . import agent, asr, db, documents, tts
from .auth import require_auth
from .models import (
    SessionListItem,
    SessionSummary,
    StartSessionRequest,
    StartSessionResponse,
    SynthesizeRequest,
    TranscribeResponse,
    TurnRequest,
    TurnResponse,
)

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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await db.init_db()
    yield


app = FastAPI(title="PyInterviewBot ai-service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/sessions",
    response_model=StartSessionResponse,
    dependencies=[Depends(require_auth)],
)
async def create_session(_req: StartSessionRequest) -> StartSessionResponse:
    session_id, question_text = await agent.start_session()
    return StartSessionResponse(session_id=session_id, question_text=question_text)


@app.post(
    "/sessions/{session_id}/turn",
    response_model=TurnResponse,
    dependencies=[Depends(require_auth)],
)
async def turn(session_id: UUID, req: TurnRequest) -> TurnResponse:
    try:
        return await agent.take_turn(session_id, req.transcript)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
        resume_claim=row["resume_claim"],
        is_complete=bool(row["is_complete"]),
        transcript=await db.get_transcript(session_id),
        evidence=await db.get_evidence(session_id),
    )


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
    pcm16 = await request.body()
    text = await asr.transcribe(pcm16)
    return TranscribeResponse(transcript=text)


@app.post("/tts/synthesize", dependencies=[Depends(require_auth)])
async def synthesize(req: SynthesizeRequest) -> StreamingResponse:
    return StreamingResponse(
        tts.synthesize_stream(req.text),
        media_type=f"audio/L16;rate={tts.SAMPLE_RATE};channels=1",
    )


def main() -> None:
    import uvicorn

    uvicorn.run("pyinterviewbot_ai.main:app", host="127.0.0.1", port=8000, reload=False)
