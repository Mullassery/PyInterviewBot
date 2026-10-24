# PyInterviewBot Phase Roadmap

Last updated: 2026-10-25

This is the execution roadmap for the productionization work. It records what is
actually implemented and verified, what remains, and the evidence for each
status. It complements [`docs/internal/BASELINE_AUDIT.md`](internal/BASELINE_AUDIT.md),
[`docs/ARCHITECTURE.md`](ARCHITECTURE.md), and the ADRs.

## Status legend

- **Complete** — implemented and covered by passing tests or a live smoke test.
- **Partial** — a working slice exists; explicit gaps are listed.
- **Pending** — not implemented.
- **Deferred** — intentionally not pursued without new evidence/authorization.

## Current quality gates

| Gate | Result |
|---|---|
| `cargo fmt --check` | pass |
| `cargo clippy --all-targets -- -D warnings` | pass |
| `cargo test` | **53 passed** (25 gateway + 28 runtime) |
| `uv run ruff check .` | pass |
| `uv run --group dev python -m pytest -q` | **25 passed** |
| `npx tsc --noEmit` | pass |
| `npm run build` | pass |
| Live native gateway smoke | sessions, speech, transcription, recording WAV export, clear, delete, auth rejection verified |

---

## Phase 0 — Baseline audit

**Status: Complete**

- Audited source, tests, CI, dependencies, docs, Git history, and upstream
  references before changing files.
- Recorded actual implemented functionality, stubs, regressions, and risks.
- Established baseline test results.
- Saved in [`docs/internal/BASELINE_AUDIT.md`](internal/BASELINE_AUDIT.md).

## Phase 1 — Design and contracts

**Status: Complete**

- Provider-neutral speech/language contracts and explicit failure semantics.
- Versioned interview templates and evidence-node definitions.
- Session state machines for transport and native voice runtime.
- Normalized event contracts with per-session sequence numbers.
- Evidence-linked report and human-review contract.
- Security threat model, capability/licence matrix, and licence inventory.
- Decision records:
  - [`ADR-0002`](adr/0002-provider-neutral-voice-contracts.md)
  - [`ADR-0003`](adr/0003-evidence-linked-reports.md)
  - [`ADR-0004`](adr/0004-rust-transport-python-domain.md)

## Phase 2 — Minimal vertical slice

**Status: Complete**

- Deterministic mock ASR/TTS/LLM providers with no network or model dependency.
- Complete interview flow: create session → opening question → candidate turns →
  evidence updates → completion → structured report → human review → deletion.
- CI runs the mock-provider flow offline.
- Tests: `ai-service/tests/test_interview_flow.py`,
  `test_providers.py`, `test_rate_limit.py`.

## Phase 3 — Rust voice runtime

**Status: Complete**

- New `voice-runtime/` workspace crate:
  - session lifecycle and pure state transitions,
  - sequenced normalized events,
  - bounded audio/text and session capacity,
  - interruption/cancellation, idle timeout sweeping,
  - provider error normalization and event bus,
  - deterministic mock provider.
- Gateway integration:
  - native studio routes (`/native/voice/*`),
  - optional managed-call workflow routes (`/native/managed-call/*`),
  - shared gateway auth, body limits, and readiness checks.
- Existing WebSocket interview flow and Python AI/evaluation service retained.
- Tests: 28 runtime tests + 25 gateway tests.

## Phase 4 — Native speech workflows

**Status: Partial**

Implemented natively in Rust:

- Speech-studio core: PCM16/WAV codecs, linear resampling, RMS, silence trim,
  peak normalization, concatenation, chunking.
- Session speech generation, transcription, interruption, explicit in-memory
  recording/export/deletion.
- Chunked TTS delivery over the native HTTP endpoint and an engine catalogue
  with capability/health discovery.
- Replaceable `SpeechEngine` contract with a deterministic mock engine.
- Managed-call workflow: server-side call create/get/end with exponential
  backoff for retryable failures, Bearer/HMAC webhook verification, bounded
  idempotent ingestion, normalized provider events.
- No external desktop-application adapter and no vendored upstream source.

Pending:

- Real local neural ASR/TTS engines behind `SpeechEngine` (licence, model
  weights, CPU/GPU support, warm-up, streaming validation).
- Model catalogue/capability discovery beyond static mock capabilities.
- Real engine streaming (current chunks are produced after full synthesis; the
  engine contract is ready for true incremental synthesis).
- Live managed-call account validation and reconnect behavior against the real
  provider.

## Phase 5 — Production hardening

**Status: Partial**

Implemented:

- API-key auth, loopback fail-closed, gateway→ai-service key forwarding.
- Per-session turn tokens with SHA-256-at-rest storage and optional enforcement.
- In-process sliding-window rate limiting per API key/IP.
- Audio size limits, WebSocket frame/message limits, body limits.
- `/health` liveness and `/ready` dependency readiness.
- Session deletion and explicit native recording deletion.
- Evidence-linked reports, uncertainty, and human-review corrections.
- Structured logs via `tracing` and JSON error responses.

Pending:

- TLS/WSS termination and deployment hardening guidance.
- Shared/distributed rate limits and quotas across workers.
- Tenant isolation and scoped admin/recruiter credentials.
- Audit log for administrative actions (delete, review, export).
- Consent/retention policy UI and disk-backed audio retention controls.
- Encryption at rest and key rotation.
- Load/concurrency benchmarks, latency SLOs, resource limits.
- Failure injection and recovery tests for provider disconnects/timeouts.
- Dependency scanning (`pip-audit` or equivalent) in CI.

## Phase 6 — Documentation and release

**Status: Partial**

Implemented:

- Updated architecture, security, capability, licence, and baseline docs.
- Configuration examples for providers, auth, rate limits, native endpoints.
- ADRs for the major decisions.
- CI runs gateway, Python, and client gates; Python tests are offline.

Pending:

- Version bump and release notes after hardening is complete.
- Live end-to-end CI (or documented self-hosted runner) with real models.
- Deployment reference (reverse proxy/TLS, process supervision, backups).
- Final supported-feature matrix sign-off and model-licence review.
- Release artifact/package validation.

---

## Prioritized backlog

### P0 — before any remote deployment

1. TLS/WSS + trusted reverse proxy configuration.
2. Scoped credentials and tenant/session isolation for recruiter endpoints.
3. Shared rate limits/quotas; gateway-level throttling.
4. Consent and retention policy, including native recording endpoint controls.
5. Audit logging for destructive and reviewer actions.
6. Legal/privacy/bias review for the target hiring jurisdiction.

### P1 — production readiness

1. Real local ASR/TTS engine integration behind `SpeechEngine`.
2. Streaming TTS and partial transcript transport validation.
3. Provider disconnect/reconnect and duplicate-event tests.
4. Load, latency, and resource measurements; publish results.
5. Dependency vulnerability scanning in CI.

### P2 — product depth

1. Additional interview templates and rubric configuration.
2. Report export (JSON/CSV/PDF) and reviewer workflow UI.
3. Multi-language support and voice configuration.
4. Recruiter-side playback/export for explicitly recorded sessions.
5. Calibration/validation study before any score-based recommendation.

## Deferred / rejected

- Production hiring recommendations without compliance review.
- Wholesale rewrite of working Python domain logic into Rust.
- Vendor-specific protocol compatibility claims.
- Model training/reimplementation without a documented need and licence review.
