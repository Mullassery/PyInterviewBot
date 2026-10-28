# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/). This project is
pre-1.0 and does not yet follow Semantic Versioning strictly.

## [Unreleased]

### Added (workflow artifacts)

- Owner-scoped `/workflows` API with principal/role authorization (`owner`,
  `author`, `approver`, `deployer`, `admin`) and legacy/loopback identities.
- Immutable workflow versions with strict schema v1, pinned template snapshots,
  canonical JSON, and SHA-256 content identity.
- Fixed mock sandbox binding, mandatory deterministic suites (`schema_hash`,
  `template_pinning`, `security_policy`, `sandbox_isolation`,
  `mock_interview`), and a separate assigned-approver step.
- Exact-hash production promotion with dry-run eligibility, durable deployment
  history, failed-promotion rows that preserve the last good deployment,
  binding-fingerprint rollback checks, and append-only workflow audit events.
- Hardened template pinning, operation-scoped idempotency, failed-promotion
  replay, provider validation, artifact-hash verification on read, workflow API
  error mapping, single-active-deployment enforcement, and schema migrations.
- Added AI-service audit events for review, deletion, and report-export actions.
- Replaced dynamic SQL identifier interpolation with fixed statements and
  allowlisted values; fixed the interview-metadata insert placeholder mismatch
  and removed a duplicate reader.
- 41 new Python tests at the workflow-artifact stage. New workflow files pass
  Ruff; 27 pre-existing repository-wide Ruff violations remain.
- Documented the lifecycle, API, configuration, and security model in
  [`docs/WORKFLOWS.md`](docs/WORKFLOWS.md).

### Added (live workflow routing)

- Optional `workflow_id` on direct and channel session creation. Supplied IDs
  resolve to an active production deployment and are pinned on the session;
  omitted IDs preserve the legacy `template_id` path.
- Session rows persist workflow ID, version, content hash, deployment ID, and
  template snapshot. Agent turns, reports, report exports, priorities, and
  evidence review use the shared pinned-snapshot resolver.
- Routing fails closed on unknown/disabled workflows, missing active
  deployments, missing or changed production bindings, missing versions, and
  deployment/content-hash mismatches.
- In-flight sessions keep their original pin across promotion, rollback,
  disablement, and built-in template changes.
- Gateway forwards optional `PYINTERVIEWBOT_WORKFLOW_ID` on direct and channel
  starts.
- 26 new routing tests; full ai-service suite is now 125 passed. Rust workspace
  tests pass and clippy is clean.
- Reran the security audit: no high/medium source findings, earlier SQL
  findings not reproducible, and refreshed `certifi`/`filelock` to clear five
  dependency advisories.

### Changed (dependency policy)

- Documented that Git-sourced dependencies must be forked into the project
  owner's account and pinned to a commit before use; prefer vendoring when the
  fork is not project-controlled. Existing registry dependencies remain pinned
  by their lockfiles. The current build has no Git-URL dependencies.

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

### Added (Twilio messaging API)

- `POST /twilio/messages` for SMS and WhatsApp sends through the Twilio
  Messaging API, with server-side Basic auth, optional sender/media URL, and
  status callback.
- `POST /twilio/calls` alias for call creation.
- Signed `/channels/twilio/message-status` callback.
- Scheduler `twilio_message` action.
- Locally mock-tested SMS, WhatsApp, and call requests.

### Added (recruiter priorities, behavioral interviews, compliance policy)

- Behavioral/STAR interview template with ownership, collaboration, conflict,
  failure-learning, and ambiguity evidence nodes.
- Recruiter-defined per-node priorities and weights, persisted per session and
  reflected in priority-weighted coverage, gaps, and exports.
- Explicit evidence-linked red flags for missing/weak/contradicted must-have
  evidence, human review rejections, and incomplete/no-evidence sessions.
- Editable, authenticated compliance policy (`GET/PUT /compliance/policy`,
  `POST /compliance/policy/reset`) with regex validation.
- Agent replaces non-compliant generated questions with safe template fallbacks.
- Personality/MBTI inference is deliberately excluded from hiring evaluation.

### Added (OpenRouter LLM provider)

- Optional `openrouter` LLM provider using the OpenAI-compatible
  `/chat/completions` API with explicit `OPENROUTER_MODEL` selection.
- Retry/backoff for retryable failures, JSON extraction/validation, and
  normalized provider errors.
- Server-side API key handling and model/capability reporting through
  `/providers`.
- Model ids are arbitrary OpenRouter ids (not OpenAI-specific); models that reject
  `response_format` fall back once to prompt-constrained JSON.
- Contract tests cover non-OpenAI model ids, request shape, auth, retries,
  invalid JSON, health, format fallback, and registry selection.
- Non-conforming action names from non-OpenAI models are normalized.
- Added OpenRouter speech providers using the same key:
  `/audio/transcriptions` (STT) and `/audio/speech` (TTS), with explicit
  `OPENROUTER_STT_MODEL`/`OPENROUTER_TTS_MODEL` and optional voice/sample-rate
  configuration.
- A live free-model OpenRouter LLM request succeeded; speech models and
  production model/region validation remain pending.

### Added (report generation and audit trail)

- Deterministic report renderers for Markdown, HTML, CSV, and JSON over the
  evidence-linked report.
- `GET /sessions/{id}/report/export?format=markdown|html|csv|json` with
  attachment headers and `no-store` caching; HTML escapes candidate/model text.
- Durable gateway audit trail (`GET /audit`) for schedule, reminder, calendar,
  Twilio message/call, and webhook authentication events.
- Python tests cover markdown/HTML/CSV/JSON exports and XSS escaping.

### Added (interview reminders)

- `POST /reminders` creates idempotent WhatsApp/SMS/voice reminders at
  configurable offsets before an interview.
- Reminder actions are durable scheduler entries with bounded retries and
  cancellation.
- Past offsets are skipped; duplicate requests return the existing schedule IDs.
- Live Twilio reminder delivery remains unverified.

### Added (calendar integrations)

- Provider-neutral calendar layer with Google Calendar and Microsoft Graph
  Outlook event creation.
- Google Meet conference requests and Teams online-meeting requests.
- Static token and refresh-token OAuth support with cached tokens.
- `POST /calendars/events`, `GET /calendars/providers`, and scheduler
  `calendar_event` action.
- Live Google/Microsoft validation remains pending.

### Added (scheduled outbound actions)

- Durable SQLite scheduler for delayed WhatsApp messages and Twilio calls.
- Admin API: create/list/cancel schedules, idempotency keys, bounded exponential
  retries, stale-dispatch recovery, and background worker.
- Gateway dispatch uses the Twilio adapter; configuration via
  `SCHEDULER_DB_PATH`, `SCHEDULER_INTERVAL_SECONDS`, and `TWILIO_WHATSAPP_FROM`.

### Added (Phase 9 WhatsApp)

- Signed WhatsApp inbound text webhook and delivery/read status callback.
- TwiML replies through the channel-neutral interview API with first-message
  opening question and idempotent follow-up turns.
- WhatsApp voice-note intake: Twilio-host/HTTPS allowlist, Basic-auth media
  download, 10 MiB cap, and `/asr/transcribe-media`.
- Locally signed webhook smoke tests, including SSRF rejection.
- WhatsApp Business approval, templates/messaging windows, outbound API messages,
  status persistence, and live calling remain pending.

### Added (Phase 8 Twilio voice)

- Twilio Programmable Voice adapter: HMAC-SHA1 signature verification, signed
  inbound/turn/status webhooks, TwiML `<Say>`/`<Gather>` interview loop,
  deterministic idempotency keys, and authenticated outbound call creation.
- Local signed-webhook smoke test against the mock interview engine.
- Twilio Media Streams WebSocket: tokenized TwiML `<Connect><Stream>`, μ-law/PCM
  codecs, 8k/16k resampling, VAD turn detection, transcription, interview turn
  advancement, and μ-law TTS playback.
- Media Streams barge-in sends a Twilio `clear` event and preserves captured
  speech; playback uses 20ms μ-law frames.
- Live-account validation and provider media-limit/backpressure testing remain
  pending.

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

### Security (dependency remediation)

- Updated npm `vite` to `^8.3.5` and lockfile resolution of
  `source-map-js` to 1.2.2, clearing all npm advisories.
- Updated Rust `rustls` 0.23.46, `rustls-webpki` 0.103.16,
  `webpki-root-certs` 1.0.10, `tokio` 1.54.1, and `yoke-derive` 0.8.4.
- `cargo audit`, `npm audit`, and `pip-audit` now report no known
  vulnerabilities.

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

[Unreleased]: ../PyInterviewBot/compare/4ab3ffe...HEAD
[0.1.1]: ../PyInterviewBot/compare/49018ce...4ab3ffe
[0.1.0]: ../PyInterviewBot/commits/49018ce
