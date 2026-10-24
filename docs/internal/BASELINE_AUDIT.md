# Baseline audit — 2026-10-25

Repository: `Mullassery/PyInterviewBot` at `14c7724` (`main`), working tree clean before this pass.
This audit is based on source, tests, CI, and local command output — not README claims alone.

## What exists and works

| Area | Actual implementation | Evidence |
|---|---|---|
| Rust gateway | Axum WebSocket transport, `earshot` VAD, barge-in, playback-duration-aware TTS, deterministic session FSM | `gateway/src/{main,ws_handler,vad,state_machine,session,ai_client}.rs` |
| Python ai-service | FastAPI routes, faster-whisper ASR, Ollama structured-output agent, Piper TTS, SQLite persistence | `ai-service/src/pyinterviewbot_ai/` |
| Candidate client | Vite/TS candidate UI, AudioWorklet mic capture, PCM player, recruiter view | `candidate-client/src/`, `public/pcm-worklet.js` |
| Auth/retention | API key or loopback-only ai-service auth; gateway token; `DELETE /sessions/{id}` | `auth.py`, `main.py`, `ws_handler.rs` |
| Documents | Deterministic resume/JD skill extraction and rubric builder | `documents.py`, `tests/test_documents.py` |
| Tests | Gateway unit tests; Python auth/documents tests; live local E2E script (not CI) | `cargo test`, `ai-service/tests/`, `scripts/e2e_ws_test.py` |

## Baseline commands (before changes)

| Command | Result |
|---|---|
| `cargo fmt --check` | pass |
| `cargo clippy --all-targets -- -D warnings` | pass |
| `cargo test` | 20 passed |
| `uv run ruff check .` | pass |
| `uv run --group dev python -m pytest -q` | 5 passed (auth + documents only) |
| `npm run build` / `npx tsc --noEmit` | pass |

Note: plain `uv run pytest` fails to spawn in this checkout; `uv run --group dev python -m pytest` is the reliable command. CI previously did not run pytest at all.

## Defects and risks found in actual code

1. **Gateway could not call an authenticated ai-service.** `AiClient` never sent `PYINTERVIEWBOT_API_KEY`/`X-API-Key`. Setting the documented ai-service key broke the gateway. Fixed in this pass.
2. **Unbounded candidate audio buffer.** `Session.utterance_buf` grew without a limit; only the VAD preroll ring was bounded. Fixed with a per-turn byte cap and WebSocket frame/message limits.
3. **No provider abstraction.** ASR/TTS/LLM were concrete imports in `asr.py`/`tts.py`/`llm.py`; swapping providers required editing interview code. Fixed with versioned internal contracts and a registry.
4. **No deterministic offline interview path.** Every Python test requiring the agent needed Ollama/models. Fixed with mock providers and a CI vertical slice.
5. **One hardcoded competency/scenario.** `resume.py` and `evidence.py` were fixed to the RAG demo. Fixed with versioned templates and template-scoped evidence.
6. **No structured assessment report.** Recruiter view exposed raw evidence rows only, with no citations/uncertainty/human review. Fixed with `/sessions/{id}/report` and `/sessions/{id}/review`.
7. **No raw audio limit on `/asr/transcribe`.** Fixed with a configurable 10 MiB default and 413 responses.
8. **Stale security documentation.** `SECURITY.md` still claimed “no authentication anywhere” after API-key/loopback auth landed. Corrected in this pass.

## Verified gaps (still open)

- No TLS/WSS configuration.
- One global API key, no per-session capability token or multi-tenant isolation; any authenticated client can read any session UUID.
- No rate limiting/quotas.
- No audio persistence or playback (transcript + evidence only).
- No WebRTC/telephony; raw WebSocket PCM16 remains the transport.
- No CI live-model E2E (Ollama + Piper models unavailable on hosted runners).
- The native managed-call workflow is mock-tested only; it has not been
  exercised against a live managed-call account.

## Upstream research

Two proprietary/trademarked upstream products were inspected as references only.
Their names, URLs, commits, and licence details are intentionally omitted from
this repository. Neither is integrated, vendored, or referenced by code,
configuration, endpoints, or environment variables. The required capabilities
were rebuilt natively in Rust (`voice-runtime/src/studio.rs` and
`voice-runtime/src/managed_call.rs`) and tested with deterministic mocks.

## Test baseline conclusion

The repo was a real working MVP, not a stub. The highest-value safe slice was
provider neutrality + deterministic mock flow + evidence-linked reporting +
transport hardening, followed by native Rust speech-studio and managed-call
workflows. WebRTC/telephony and GPU inference were not attempted without a
design partner.
