# Honest Status / Roadmap

This is the disclosure doc: what's actually verified working, what's built
but unverified, what's flat-out not built yet, and what's broken in CI.
Cross-reference with [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the
architectural rationale behind the scope cuts.

Last updated: 2026-09-19, immediately after the initial MVP slice shipped.

## Built and verified working

- Gateway session state machine (19 unit tests) and VAD/preroll/turn-buffer
  logic (unit tested against synthetic PCM)
- Full real-time pipeline end-to-end: barge-in, VAD end-of-turn, faster-whisper
  transcription, Ollama-driven adaptive questioning + evidence extraction,
  Piper TTS playback-duration-aware turn-taking — validated by a live
  WebSocket integration test using real synthesized audio
  (`ai-service/scripts/e2e_ws_test.py`), not mocks
- SQLite persistence of transcript + evidence, readable via
  `GET /sessions/{id}/summary`
- One full demo interview (RAG Architecture competency, 3 evidence nodes,
  6 turns, correct graceful conclusion) run start to finish

## Built but not independently tested

- **ai-service has zero automated tests.** `pytest`/`pytest-asyncio`/`httpx`
  are dev dependencies and a `tests/` directory doesn't even exist yet — the
  only verification of `asr.py`/`tts.py`/`llm.py`/`agent.py`/`db.py` is the
  one manual e2e script and ad-hoc `curl` calls during development.
- **gateway's async orchestration has zero Rust-level test coverage.** The
  state machine and VAD are unit tested in isolation, but `ws_handler.rs`'s
  `speak()`/`listen_for_turn()`/`speak_recovery()` functions — the actual
  `tokio::select!` concurrency, barge-in race handling, and error-recovery
  flow — are only exercised indirectly through the Python e2e script, never
  with a Rust integration test.
- **The candidate client's real browser flow was never exercised with a
  real microphone.** Verified visually (page loads, mic-test screen matches
  the design) in a sandboxed browser automation environment with no working
  audio input device — the mic-test button gets stuck at "Listening for
  your voice..." there. The actual mic-capture → WS → playback path in a
  real browser has not been human-tested.
- **The recruiter view (`/recruiter`) was never opened in a browser.** Its
  backing API endpoints (`GET /sessions`, `GET /sessions/{id}/summary`)
  were curl-tested directly; the rendered page itself was not.
- **`speak_recovery()` (the ASR/agent-failure fallback path, spec §28) has
  never actually fired.** It's implemented and reachable, but no real ASR
  or Ollama failure occurred during testing to exercise it.
- **`PAUSE`/`RESUME` state transitions are unit tested but unreachable in
  practice** — no UI control, WS message type, or code path ever triggers
  them yet.

## Not built at all

Grouped roughly by the original product spec's sections:

- **Voice persona configuration** (voice/accent/gender/speed/formality/
  personality/language/vocabulary) — one fixed Piper voice, no config surface
- **Multi-language interviews** — English only
- **General scenario/simulation engine** — the one "traffic increased 20x"
  scenario is a hardcoded instruction in the system prompt, not a reusable
  branching state-machine engine with tracked system state
- **Voice + work-artifact hybrid modality** — no whiteboard, code canvas,
  or architecture-diagram interaction; voice-only
- **Multi-competency evidence graph** — the graph exists for exactly one
  hardcoded competency; no competency-graph builder from a real job
  description
- **Resume upload + real parsing + claim extraction** — one hardcoded seed
  claim; no upload UI, no document parser, no LLM-based claim extraction
- **JD parser** — not built
- **Interview planner beyond one competency** — not built
- **Explicit claim-verification status** distinct from evidence confidence
  — verification is currently implicit in evidence status, not a separate
  tracked "insufficient verification" outcome
- **Dedicated technical-depth/expertise-boundary detection** — implicit via
  evidence "weak" status; no standalone boundary-finding mechanism
- **Human interviewer copilot** — not built
- **True WebRTC transport** — raw WebSocket + PCM16 is used instead; works,
  but isn't WebRTC
- **Recording/retention/consent configuration** — no audio is persisted at
  all (transcript + evidence only); no retention policy, no consent screen,
  no candidate/recruiter access toggles
- **Formatted assessment report** — the recruiter view is a raw evidence
  table; no confidence rollup, no hiring recommendation summary, no export
- **Formal provider-abstraction interfaces** — one concrete hardcoded
  implementation each for ASR/LLM/TTS; swapping providers means editing
  `asr.py`/`llm.py`/`tts.py` directly, not registering a new implementation
  against a trait/interface
- **Calibration / evidence-correlates-with-outcome learning loop** —
  aspirational long-term idea from the spec, not started
- **Authentication/authorization** — none anywhere; the gateway WebSocket
  and ai-service HTTP API are both open on localhost
- **Deployment story** — no Dockerfile, no docker-compose, no production
  deployment docs; local-only right now
- **Rate limiting / abuse protection** — none

## CI

- **No CI at all.** There's no `.github/workflows/` directory in this repo.
  `cargo test`, `pytest` (once tests exist), `npm run build`, and
  `tsc --noEmit` all pass locally but nothing runs automatically on push or
  PR yet.
- **No automated PyPI release workflow.** `pyinterviewbot-ai` 0.1.0 was
  published via a manual `twine upload`, not an Actions job (consistent
  with how other Mullassery repos currently handle releases).

## Features present in code but not fully functional

- Recruiter session list is capped at 50 (`ORDER BY created_at DESC LIMIT 50`
  in `db.list_sessions`) with no pagination or search — fine for a demo,
  not for real usage volume
- Candidate client's `onError` text rendering is wired up but has never
  actually been triggered/observed against a real failure
