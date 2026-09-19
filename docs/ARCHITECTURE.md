# Architecture

PyInterviewBot is a voice-first AI interview platform: the candidate hears
questions, answers by voice, and never sees a transcript, score, or any
part of the evaluation. Everything — ASR, evidence extraction, the
competency graph, adaptive follow-ups — happens silently server-side.

This document describes what's actually implemented in this slice, how the
three languages divide responsibility, and exactly where the implementation
simplifies the full product spec (and why).

## Layers

```
Browser (candidate-client, Vite/TS)
   │  WebSocket — binary: mic audio (candidate → server) / TTS audio (server → candidate)
   │             text (JSON): {"type":"state",...} | {"type":"interrupt"} | {"type":"error",...}
   │  There is no message type for a transcript, score, or evidence — the
   │  protocol itself cannot leak that to the candidate client, independent
   │  of what the UI chooses to render.
   ↓
gateway/ (Rust, axum)
   - Deterministic session state machine (gateway/src/state_machine.rs):
     Initializing → AiSpeaking ⇄ Interrupted → Listening ⇄ CandidateSpeaking
     → Processing → FollowUpDecision → AiResponding → … → Completed /
     ErrorRecovery. Pure function, fully unit tested (19 tests), no IO.
   - VAD + end-of-turn heuristic (gateway/src/vad.rs): wraps `earshot`
     (pure-Rust neural VAD). 200ms of confirmed voice = "speech started"
     (drives barge-in); 700ms of trailing silence after speech = "turn
     ended". A ~300ms preroll ring buffer means the moment speech is
     confirmed, the audio leading up to it is retroactively included —
     the first syllable isn't clipped.
   - Barge-in: while AI audio is streaming out, incoming mic audio is
     watched concurrently (`tokio::select!`). The moment VAD confirms real
     speech, the TTS stream is dropped (Piper synthesis is cancelled
     server-side too, since it's a cancelled HTTP request) and an
     `{"type":"interrupt"}` message tells the browser to stop playback
     immediately.
   - Playback-duration awareness: Piper synthesizes faster than real time,
     so "all TTS bytes have been sent" happens well before "the candidate
     has finished hearing the question." `speak()` tracks total audio
     bytes sent and won't return `Finished` (or stop watching for a
     barge-in) until that much playback time has actually elapsed.
   - Owns only transport/turn-taking state. Domain data (evidence,
     transcript, claims) is not duplicated here — ai-service is the single
     source of truth, avoiding a two-database sync problem.
   ↓ HTTP (POST /sessions, /sessions/{id}/turn, /asr/transcribe, /tts/synthesize)
ai-service/ (Python, FastAPI, uv-managed)
   - asr.py — faster-whisper (local, CPU, int8). Transcribes one complete
     VAD-delimited utterance per call.
   - tts.py — Piper (local). Synthesizes and yields raw PCM16 chunks as
     they're produced (not buffered-then-sent), so the gateway can start
     forwarding audio before the whole utterance is done synthesizing.
   - llm.py — Ollama (local). One narrow function: given a system prompt
     and context, return a JSON object matching a Pydantic schema (Ollama's
     structured-output `format` parameter, not prompt-engineered JSON).
   - agent.py — the interview agent. One LLM call per turn does both
     evidence extraction from the candidate's last answer *and* decides
     the next question/follow-up strategy — genuinely adaptive, not a
     scripted flow. A backend-enforced `MAX_TURNS` cap (deterministic,
     not LLM-decided) guarantees the interview actually ends.
   - evidence.py — the competency/evidence graph for this slice's one
     demo competency (RAG Architecture, 3 evidence nodes).
   - resume.py — a single seeded resume claim (see "Not implemented"
     below — there's no real resume upload/parser yet).
   - db.py — SQLite, the system of record for sessions, transcript
     segments, and evidence. State lives here, not just inside an LLM's
     context window.
   - main.py — FastAPI routes tying it together, plus a read-only
     `/sessions` + `/sessions/{id}/summary` pair the recruiter view reads
     directly (see "Two client surfaces" below).
   ↓
candidate-client/ (Vite/TS)
   - AudioWorklet (public/pcm-worklet.js) downsamples the mic to 16kHz
     mono PCM16 in a separate audio-thread processor.
   - Player (src/player.ts) schedules incoming TTS PCM16 chunks back-to-back
     via Web Audio `AudioBufferSourceNode`s; `interrupt()` stops everything
     immediately on a barge-in signal.
   - UI renders only: the AI's own question text (its own speech, not the
     candidate's — see "Two client surfaces"), a listening/speaking
     animation, and an elapsed timer. No transcript, no score, no
     evidence, ever.
```

## Two client surfaces, deliberately asymmetric

- **Candidate** (`candidate-client`, served behind the gateway's WS
  protocol): can only ever receive state + audio. The AI's own question
  text is included in state messages when it starts speaking — that's the
  AI's own generated speech being captioned, not the candidate's
  transcript, so it doesn't violate "never show the candidate's speech
  recognized as text."
- **Recruiter** (`candidate-client`'s `/recruiter` route): talks to
  ai-service directly (`GET /sessions`, `GET /sessions/{id}/summary`),
  bypassing the gateway entirely. This is the one surface transcript +
  evidence are allowed to reach. There's no auth on this in the current
  slice — see "Not implemented."

## What's real vs. simplified in this slice

Everything below actually runs against real local models — nothing is a
stub that pretends to work. But several pieces are narrower than the full
product spec on purpose:

| Area | What's implemented | Full-spec gap |
|---|---|---|
| ASR | Per-turn transcription (whole VAD-delimited utterance sent to faster-whisper at once) | No token-level streaming partial transcripts. Nothing candidate-facing depends on partials, and faster-whisper has no native incremental-decode mode — a real one would periodically re-decode a growing buffer. |
| End-of-turn detection | Fixed 700ms silence timeout after confirmed speech | Not the cadence/sentence-completion-aware model the spec describes; a candidate who pauses >700ms mid-thought will get cut off. |
| Competencies | One competency (RAG Architecture), 3 evidence nodes, hardcoded to match the demo scenario | No competency graph builder from a real job description. |
| Resume | One hardcoded seed claim | No upload, no parsing, no claim extraction from an arbitrary resume. |
| Languages | English only | No multi-language switching mid-interview. |
| Simulation engine | The one "traffic increased 20x" scenario is baked into the system prompt as an instruction, not a state machine | No general-purpose scenario/state-machine engine (spec §18-19) — no branching system-state tracking, no whiteboard/code artifact modality. |
| Claim verification | Implicit — the agent probes the resume claim conversationally | No explicit "insufficient verification" status distinct from low evidence confidence. |
| Human interviewer copilot | Not built | Spec §25 entirely out of scope. |
| Recruiter auth | None | `/recruiter` and ai-service's session endpoints are open on localhost. Not remotely deployable as-is. |
| Audio recording | Not persisted | Transcript + evidence are persisted; raw audio is not saved anywhere, so the recruiter view has no "listen to answer" playback despite the spec describing one. |
| Provider abstraction | One concrete implementation each for ASR/LLM/TTS (faster-whisper / Ollama / Piper) | The spec's `SpeechRecognizer`/`LanguageModel`/`SpeechSynthesizer` interfaces aren't formalized as Rust/Python traits yet — swapping providers today means editing asr.py/llm.py/tts.py directly, not registering a new implementation. |

## Two real bugs found (and fixed) during integration testing

Worth recording because they're the kind of thing that only shows up under
real timing, not in isolated unit tests:

1. `listen_for_turn` never transitioned `Listening → CandidateSpeaking` on
   VAD's `SpeechStarted` event — only watched for `TurnEnded`. Once a turn
   ended, `CandidateTurnEnded` was an illegal transition from `Listening`,
   silently logged and ignored, desyncing the tracked state from reality.
2. `speak()` originally returned `Finished` as soon as all TTS bytes were
   pushed to the socket — but Piper synthesizes faster than real time, so
   the gateway would move to `Listening` (and start accepting "answers")
   several seconds before the candidate had actually finished hearing the
   question. Fixed by tracking bytes-sent → audio-duration and waiting out
   the remainder (while still watching for barge-in) before finishing.

Both were caught by a real end-to-end test (`ai-service/scripts/e2e_ws_test.py`)
that drives the actual gateway WebSocket protocol with real synthesized
audio (candidate answers generated with the same local Piper voice,
resampled to 16kHz, streamed at real-time pace) rather than mocking
anything — see that file for how to rerun it.
