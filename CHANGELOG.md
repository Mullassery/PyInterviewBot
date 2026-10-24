# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/). This project is
pre-1.0 and does not yet follow Semantic Versioning strictly.

## [Unreleased]

### Added (2026-10-25)

- Provider-neutral speech/language contracts and registry (`providers/`) with
  `local` and deterministic `mock` implementations. Unknown providers fail closed.
- Self-hosted Rust `voice-runtime/` crate: provider contracts, session
  lifecycle, sequenced events, bounded audio/text, cancellation, capacity,
  idle timeout sweeping, deterministic mock provider, native speech-studio core
  (WAV/PCM, resampling, speech engine contracts), managed-call workflow
  (server-side call API, Bearer/HMAC webhook verification, idempotent event
  normalization), and unit tests.
- Gateway integration for the native runtime at `/native/voice/*` and the
  optional `/native/managed-call/*` workflow, sharing gateway authentication.
- Native studio audio processing (RMS, silence trim, peak normalize,
  concatenate, chunking) and explicit in-memory recording/export/deletion
  endpoints with size bounds.
- Versioned interview templates (`rag_architect`, `backend_engineer`,
  `data_engineer`) with persisted `sessions.template_id` and in-place SQLite
  migration.
- Evidence-linked reports at `GET /sessions/{id}/report` and append-only human
  review at `POST /sessions/{id}/review`; citations flag model notes as
  inference and reports require human review.
- Deterministic Python interview-flow/provider tests that run offline in CI.
- Gateway `AI_SERVICE_API_KEY`/`PYINTERVIEWBOT_API_KEY` forwarding,
  `PYINTERVIEWBOT_TEMPLATE`, configurable VAD silence
  (`GATEWAY_VAD_SILENCE_MS`), per-turn audio caps
  (`GATEWAY_MAX_UTTERANCE_BYTES`), WebSocket frame/message limits, and `/ready`.
- `/asr/transcribe` payload cap (`PYINTERVIEWBOT_MAX_AUDIO_BYTES`, 413 on
  overflow), plus `/providers` and `/templates` discovery endpoints.

### Added (Phase 7 channel domain)

- Rust `interview-domain` crate: channel/event/state contracts, illegal
  transition rejection, bounded deduplication, and cross-channel tests.
- Durable SQLite `channel_sessions` and `channel_events` tables with deletion
  cleanup.
- Authenticated channel-neutral API for session mapping and idempotent messages
  (`POST /channels/{channel}/sessions`, `POST /channels/{channel}/messages`).
- Cross-channel contract test proving equivalent answers produce the same report
  structure.

### Added (Phase 4 neural engine)

- Optional real local neural engine behind the `local-neural` feature:
  sherpa-onnx VITS/Piper TTS and Whisper ASR, operator-supplied model paths,
  CPU default, engine registration/health, and a real TTS→ASR smoke example.
- Gateway feature wiring for `VOICE_ENGINE=neural` with safe mock fallback.

### Added (Phase 4 continuation)

- Chunked TTS delivery at `/native/voice/sessions/{id}/speak/stream`.
- Engine catalogue and health discovery at `/native/voice/engines`.
- Managed-call exponential-backoff retries for retryable transport failures
  (`MANAGED_CALL_MAX_RETRIES`, `MANAGED_CALL_RETRY_BASE_MS`).

### Security (2026-10-25, hardening increment)

- Per-session tokens: `POST /sessions` returns a token, only its hash is
  stored, the gateway forwards it on turns, and
  `PYINTERVIEWBOT_REQUIRE_SESSION_TOKEN=1` enforces it.
- Bounded in-process sliding-window rate limiting for ai-service, keyed by API
  key or client IP (`PYINTERVIEWBOT_RATE_LIMIT`,
  `PYINTERVIEWBOT_RATE_WINDOW_SECONDS`).

### Changed (2026-10-25)

- Removed external trademarked/upstream product names and adapter paths from
  code, configuration, tests, and documentation. Both workflows are implemented
  natively in Rust and exposed through the gateway.

### Documentation (2026-10-25)

- Added `docs/internal/BASELINE_AUDIT.md`, `docs/CAPABILITY_MATRIX.md`,
  `docs/SECURITY_MODEL.md`, `docs/LICENCES.md`, and ADRs 0002–0004. Corrected
  stale auth/tests claims in `README.md` and `SECURITY.md`.

### Security

- Fixed unbounded HTTP client in the gateway's `AiClient`
  (`ai_client.rs:65`): `reqwest::Client::new()` had no timeout, so an
  ai-service hang could block the session's `tokio::select!` loop
  forever instead of reaching `speak_recovery()`. Now built via
  `Client::builder().timeout(Duration::from_secs(20)).build()`. Added a
  targeted test (`ai_client::tests::request_times_out_instead_of_hanging_forever`)
  that proves a request against an unresponsive server errors out with a
  timeout instead of hanging.

### Added

- API authentication for the ai-service (`PYINTERVIEWBOT_API_KEY`, `X-API-Key`
  or `Authorization: Bearer`) and the gateway (`GATEWAY_API_KEY`). When no key
  is configured, only loopback clients are accepted so a network-exposed
  deployment fails closed; `/health` stays unauthenticated.
- `DELETE /sessions/{id}` to remove a session and its transcript/evidence rows
  for retention requests.
- Deterministic, offline resume/JD parsing and grounded rubric generation
  (`pyinterviewbot_ai.documents`, `POST /documents/parse`); optional PDF via
  `pypdf`.
- OSS project scaffolding: `CONTRIBUTING.md`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md`, `CHANGELOG.md`, `.github/dependabot.yml`,
  `.github/ISSUE_TEMPLATE/`, `.github/pull_request_template.md`.

### Documentation

- `docs/internal/ROADMAP_HONEST.md` updated with a security review (CORS wildcard on
  `ai-service`, no HTTP client timeout in the gateway's `AiClient`, no
  `pip-audit` coverage) and confirmation that `cargo test` (19/19),
  `cargo clippy`, `cargo fmt --check`, `cargo audit`, `ruff check`,
  `npm audit`, `tsc --noEmit`, `npm run build`, and the real
  `e2e_ws_test.py` end-to-end pipeline test were all independently
  re-run and passed as part of this pass.

## [0.1.1] - 2026-09-22

### Security

- Fixed wildcard CORS on `ai-service` (`main.py:33`): `allow_origins`
  is now a configurable allowlist (`ALLOWED_ORIGINS` env var,
  comma-separated), defaulting to the candidate-client's actual Vite dev
  server origins (`http://localhost:5173`, `http://127.0.0.1:5173`)
  instead of `"*"`. Verified with a `TestClient` CORS preflight: the
  candidate-client's origin gets `Access-Control-Allow-Origin` back, an
  arbitrary origin is rejected (400, no CORS header).
- `pyinterviewbot-ai` 0.1.1 published to PyPI (manual `twine upload`; no
  automated release workflow exists).

## [0.1.0] - 2026-09-19

### Added

- Initial MVP: voice-first AI interview platform — Rust `gateway`
  (WebSocket audio transport, VAD/barge-in, deterministic session state
  machine, 19 unit tests), Python `ai-service` (faster-whisper ASR,
  Ollama-driven adaptive interview agent, Piper TTS, SQLite persistence),
  and a Vite/TypeScript `candidate-client` (candidate voice UI +
  `/recruiter` view).
- `ai-service/scripts/e2e_ws_test.py`: a real end-to-end integration test
  that drives the actual gateway WebSocket protocol with real synthesized
  audio against real local models (no mocks).
- `pyinterviewbot-ai` 0.1.0 published to PyPI (manual `twine upload`; no
  automated release workflow exists).
- CI pipeline (`.github/workflows/ci.yml`) covering gateway
  (`cargo fmt --check`, `cargo clippy -D warnings`, `cargo test`),
  ai-service (`uv sync`, `ruff check`, an import sanity check), and
  candidate-client (`tsc --noEmit`, `npm run build`).
- `docs/internal/ROADMAP_HONEST.md`: disclosure of what's verified working, built-but-
  untested, and not built at all.

### Fixed

- `ai-service` sdist was missing `LICENSE`, breaking the PyPI publish.
- Two real timing bugs found via the e2e integration test, documented in
  `docs/ARCHITECTURE.md`: `listen_for_turn` never transitioning
  `Listening → CandidateSpeaking` on VAD's `SpeechStarted` event, and
  `speak()` returning `Finished` before TTS playback had actually finished
  (Piper synthesizes faster than real time).

[Unreleased]: https://github.com/Mullassery/PyInterviewBot/compare/4ab3ffe...HEAD
[0.1.1]: https://github.com/Mullassery/PyInterviewBot/compare/49018ce...4ab3ffe
[0.1.0]: https://github.com/Mullassery/PyInterviewBot/commits/49018ce
