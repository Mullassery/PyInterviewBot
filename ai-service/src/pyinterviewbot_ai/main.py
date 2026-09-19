from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from . import agent, asr, db, tts
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await db.init_db()
    yield


app = FastAPI(title="PyInterviewBot ai-service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/sessions", response_model=StartSessionResponse)
async def create_session(_req: StartSessionRequest) -> StartSessionResponse:
    session_id, question_text = await agent.start_session()
    return StartSessionResponse(session_id=session_id, question_text=question_text)


@app.post("/sessions/{session_id}/turn", response_model=TurnResponse)
async def turn(session_id: UUID, req: TurnRequest) -> TurnResponse:
    try:
        return await agent.take_turn(session_id, req.transcript)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/sessions", response_model=list[SessionListItem])
async def list_sessions() -> list[SessionListItem]:
    return await db.list_sessions()


@app.get("/sessions/{session_id}/summary", response_model=SessionSummary)
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


@app.post("/asr/transcribe", response_model=TranscribeResponse)
async def transcribe(request: Request) -> TranscribeResponse:
    pcm16 = await request.body()
    text = await asr.transcribe(pcm16)
    return TranscribeResponse(transcript=text)


@app.post("/tts/synthesize")
async def synthesize(req: SynthesizeRequest) -> StreamingResponse:
    return StreamingResponse(
        tts.synthesize_stream(req.text),
        media_type=f"audio/L16;rate={tts.SAMPLE_RATE};channels=1",
    )


def main() -> None:
    import uvicorn

    uvicorn.run("pyinterviewbot_ai.main:app", host="127.0.0.1", port=8000, reload=False)
