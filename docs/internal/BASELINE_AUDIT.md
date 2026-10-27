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
| `uv run ruff check .` | pass (historical baseline) |
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

## 2026-10-26 call-state verification

Canonical call state is now implemented in `gateway/src/calls.rs` with domain
contracts in `interview-domain/src/call.rs`. The gateway is the sole writer of
the dedicated call SQLite database; ai-service must not write it.

Commands run from the repository root unless noted:

| Command | Result |
|---|---|
| `cargo fmt --all` | applied |
| `cargo clippy --workspace --all-targets -- -D warnings` | pass |
| `cargo test --workspace` | **101 passed** (56 gateway + 32 voice-runtime + 13 interview-domain) |
| `uv run ruff check .` (from `ai-service`) | **not clean**: 94 pre-existing violations, mostly `scripts/e2e_ws_test.py` |
| `uv run ruff check <changed files>` | only pre-existing `N818`/`D209` in touched package files; new test code clean |
| `uv run --group dev python -m pytest -q` (from `ai-service`) | **58 passed**, 2 dependency deprecation warnings |

Additional verified behavior:

- SQLite schema v1, WAL, foreign keys, 5s busy timeout, `user_version`.
- Unique provider call IDs and idempotency keys.
- Event dedup, mapped sequences, terminal-state protection, unmatched replay.
- Optimistic versions and shortest-path transitions.
- Twilio signed inbound/status/outbound correlation and Vapi-compatible webhook
  mapping are code/contract tested.
- `vapi` is a supported ai-service channel identifier.

Not verified: live Twilio voice/media, live Vapi calls, live WhatsApp calls,
Teams join/transcripts, backup/restore against production-sized data, and
multi-replica operation. SQLite remains single-node only. See
[`docs/CALL_STATE.md`](../CALL_STATE.md).

## 2026-10-26 workflow verification

The ai-service now has a workflow artifact lifecycle. New workflow source and
tests pass targeted Ruff checks; after removing duplicated test fixtures, the
repository-wide Ruff baseline is 38 pre-existing violations in unrelated files.

| Command | Result |
|---|---|
| `uv run python -c "from pyinterviewbot_ai.main import app; print('import OK')"` | `import OK` |
| `uv run --group dev python -m pytest -q` (from `ai-service`) | **99 passed**, 2 dependency deprecation warnings |
| `uv run --group dev ruff check <workflow files>` | pass |
| `uv run --group dev ruff check .` (from `ai-service`) | **not clean**: 38 pre-existing violations |

Verified behavior: strict canonical hashing, immutable versions, idempotent
publishing, concurrent version allocation, fixed mock sandbox, mandatory suites,
assigned approval, exact-hash promotion, failed-promotion preservation,
ownership/role isolation, approval URL binding, binding-aware rollback,
single-active-deployment enforcement, artifact hash verification on read,
AI-service review/delete/export audit events, and migration behavior. Workflow state is stored in the ai-service interview SQLite
database, separate from the Rust-owned call-state database.

Not verified: live candidate-session routing to an active workflow deployment,
operator UI/CLI, workflow evidence retention/cleanup, and production provider
execution. See [`docs/WORKFLOWS.md`](../WORKFLOWS.md).
