# PyInterviewBot Execution Roadmap

Last revised: 2026-10-25  
Repository snapshot: `b9a6004` (working tree clean after restoring the verified state)

This document records completed work, pending work, external blockers, and the
dependency order for the multi-channel platform. It is evidence-based: a feature
is not marked complete because it is described in a prompt or README.

Companion documents:
- [`docs/internal/BASELINE_AUDIT.md`](internal/BASELINE_AUDIT.md)
- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md)
- [`docs/SECURITY_MODEL.md`](SECURITY_MODEL.md)
- [`docs/CAPABILITY_MATRIX.md`](CAPABILITY_MATRIX.md)
- [`docs/adr/`](adr/)

## Status language

- **Implemented and tested** — code exists and automated tests or a documented smoke test pass.
- **Implemented, not live-verified** — code exists and is mock/contract-tested; no live provider validation.
- **Partial** — a working slice exists, with explicit gaps.
- **Pending** — not implemented.
- **Blocked** — requires external account approval, credentials, tenant permissions, or unsupported provider capability.
- **Deferred** — intentionally not pursued without new evidence/authorization.

## Current verified baseline

| Gate | Result |
|---|---|
| `cargo fmt --check` | pass |
| `cargo clippy --all-targets -- -D warnings` | pass |
| `cargo clippy --all-targets --features local-neural -- -D warnings` | pass |
| `cargo test` | **80 passed** (43 gateway + 32 runtime + 5 domain) |
| `cargo test -p voice-runtime --features local-neural` | **63 passed** |
| `uv run ruff check .` | pass |
| `uv run --group dev python -m pytest -q` | **46 passed** |
| `npx tsc --noEmit` | pass |
| `npm run build` | pass |
| Live native gateway smoke | health, sessions, speech, chunked speech, transcription, recording export/clear/delete, auth rejection |
| Live local neural smoke | real TTS→ASR round-trip produced `"Hello from the native neural engine."` |

---

# Completed phases

## Phase 0 — Repository audit

**Status: Implemented and tested**

- Inspected source, tests, CI, manifests, docs, and Git state before modifying.
- Recorded real capabilities, defects, and baseline test results.
- Artifact: [`docs/internal/BASELINE_AUDIT.md`](internal/BASELINE_AUDIT.md).

## Phase 1 — Core design and contracts

**Status: Implemented and tested**

- Provider-neutral speech/language contracts and normalized voice events.
- Session state machines with invalid-transition rejection.
- Versioned interview templates and evidence-node definitions.
- Evidence-linked reports, uncertainty, and human-review corrections.
- Threat model, capability/licence matrix, and ADRs.

## Phase 2 — Shared interview vertical slice

**Status: Implemented and tested**

- Deterministic mock ASR/TTS/LLM path with no model/network dependency.
- Interview lifecycle: create → question → candidate turns → evidence updates →
  completion → report → human review → deletion.
- Offline CI coverage for the mock path.

## Phase 3 — Rust voice runtime and gateway integration

**Status: Implemented and tested**

- `voice-runtime/`: session lifecycle, event sequencing, bounded audio/text,
  cancellation, idle sweeping, capacity limits, mock provider.
- Gateway native routes for studio speech, transcription, recording, engine
  discovery, and the optional managed-call workflow.
- Existing WebSocket interview flow and Python AI/evaluation service preserved.

## Phase 4 — Native speech workflows

**Status: Partial**

Implemented and tested:

- Native speech-studio core: PCM16/WAV, resampling, RMS, silence trim, peak
  normalization, concatenation, chunking.
- Optional real local neural engine (`local-neural` feature): VITS/Piper TTS and
  Whisper ASR via sherpa-onnx, operator-supplied weights, CPU default.
- Chunked TTS HTTP delivery and engine catalogue/health discovery.
- Explicit in-memory recording/export/deletion with size bounds.
- Managed-call workflow: call create/get/end, exponential-backoff retries,
  Bearer/HMAC webhook verification, bounded idempotent event ingestion.

Pending:

1. True incremental neural synthesis. Current chunks are produced after full
   synthesis; the engine contract is ready for a streaming implementation.
2. Model warm-up/load-time metrics and documented accelerator (GPU/CoreML)
   configuration.
3. Live managed-call provider validation and reconnect behavior.
4. Partial-transcript transport and cross-provider streaming contract tests.

## Phase 5 — Production hardening (existing web/native path)

**Status: Partial**

Implemented and tested:

- API-key auth, loopback fail-closed, gateway→ai-service key forwarding.
- Per-session turn tokens (hash at rest) with optional enforcement.
- In-process rate limiting, audio/body/frame limits, readiness checks.
- Session/recording deletion, structured logs, stable JSON errors.

Pending:

1. TLS/WSS termination and deployment reference.
2. Shared/distributed rate limits and quotas.
3. Tenant isolation and scoped recruiter/admin credentials.
4. Audit log for destructive/review/export actions. Gateway provider/scheduler
   actions are covered; AI-service review/delete auditing remains pending.
5. Recruiter priorities, behavioral templates, editable question-compliance
   policy, and evidence-linked red flags are implemented.
5. Consent/retention policy UI and disk-backed audio controls.
6. Encryption at rest, key rotation, dependency vulnerability scanning.
7. Load/latency/resource measurements and failure injection.

## Phase 6 — Documentation and release

**Status: Partial**

Implemented: architecture, security, capability/licence, ADRs, configuration,
offline CI tests.

Pending: version/release process, live-model CI runner, deployment packaging,
final licence/model review, release artifact validation.

---

# Pending multi-channel phases

These phases extend the existing platform. No Twilio, WhatsApp, or Teams adapter
code exists in the repository today; only provider documentation has been
consulted. Do not describe these channels as implemented until code and tests
exist.

## Phase 7 — Shared channel-independent interview domain

**Status: Implemented and tested**  
**Provider adapters: Pending (start with Phase 8)**

Implemented:

- Rust `interview-domain` crate: channel enum, versioned event envelope,
  normalized inbound/outbound events, channel-session state machine with illegal
  transition rejection, bounded idempotency cache, and cross-channel contract
  tests.
- Durable SQLite `channel_sessions` mapping and `channel_events` idempotency
  records, including deletion cleanup.
- Authenticated channel-neutral API:
  - `POST /channels/{channel}/sessions`
  - `POST /channels/{channel}/messages`
- Idempotent message processing: duplicate provider event IDs return the stored
  logical response without adding another candidate turn.
- Cross-channel test: the same answers through `twilio_voice` and
  `whatsapp_text` produce the same report node/status structure.
- Supported channel identifiers: `web`, `twilio_voice`, `whatsapp_text`,
  `whatsapp_voice_note`, `teams` (adapters not yet implemented).

Provider wire formats remain outside the domain. No Twilio, WhatsApp, or Teams
adapter code exists yet.

## Phase 8 — Twilio voice

**Status: Partial — implemented, not live-verified**  
**Live status: Blocked by Twilio account/credentials and a public HTTPS callback URL**

Implemented:

- `X-Twilio-Signature` HMAC-SHA1 verification with canonical public URL and
  sorted form parameters.
- Signed inbound call, gather-result turn, and call-status webhooks.
- TwiML `<Say>`/`<Gather>` loop through the channel-neutral interview API.
- Deterministic idempotency key from CallSid + speech text.
- Outbound call creation against the Twilio REST API, with tokenized start URL.
- Twilio Messaging API send endpoint for SMS and WhatsApp, plus signed message
  status callbacks.
- Unit tests for signatures, form parsing, TwiML escaping, and start tokens.
- Signed end-to-end local smoke against the mock interview engine.

Pending:

- Live Twilio account validation and callback URL registration.
- Provider-specific Media Streams pacing/backpressure validation under load.
- Durable call-lifecycle event persistence beyond status logging.
- Recording/consent configuration for Twilio.`

Verified provider starting points:
- https://www.twilio.com/docs/voice/api
- https://www.twilio.com/docs/usage/security#validating-requests
- https://www.twilio.com/docs/voice/twiml

Deliverables:

1. Outbound call initiation with session correlation and idempotency.
2. Inbound call webhook handling and interview-session lookup/creation.
3. TwiML gather/transcribe mode using the existing interview engine.
4. Optional Media Streams adapter for real-time audio, with backpressure,
   interruption, disconnect, and timeout handling.
5. Twilio request-signature validation using the exact public URL and sorted
   parameters.
6. Replay/duplicate callback protection and durable call lifecycle metadata.
7. Tests for invalid signatures, duplicate callbacks, failed calls, timeouts,
   and session termination.

Definition of done: deterministic adapter tests pass; live call test is either
performed with an approved account or explicitly reported NOT RUN.

## Phase 9 — WhatsApp

**Status: Partial — implemented, not live-verified**  
**Live status: Blocked by WhatsApp Business approval/sender configuration**

Implemented:

- Signed inbound text webhook and delivery/read status callback.
- Twilio-form parsing, `whatsapp:` identity normalization, TwiML replies.
- First-message opening question and idempotent subsequent turns via the shared
  channel API.
- Voice-note intake with HTTPS/host allowlisting, Twilio Basic auth, 10 MiB cap,
  and media transcription (`/asr/transcribe-media`).
- Locally signed webhook smoke tests, including SSRF rejection.

Pending:

- WhatsApp Business account/sender/template approval and live validation.
- Messaging-window and template enforcement.
- Outbound template messages, opt-out management, and live Messaging API
  validation.
- Durable status-callback event persistence.
- Scheduled outbound actions and interview reminders are implemented; live
  Twilio delivery validation remains pending.
- Distinct voice-note channel metadata/session continuity review.
- WhatsApp live calling capability verification (below).

Verified provider starting points:
- https://www.twilio.com/docs/whatsapp
- https://www.twilio.com/docs/whatsapp/api
- https://www.twilio.com/docs/messaging/guides/webhook-request

### 9A. Text messaging

Deliverables:

1. Webhook signature validation, challenge handling where applicable, and
   bounded payload parsing.
2. Candidate invitation, consent/opt-out, session creation, and deduplication.
3. Text question delivery, adaptive follow-up, completion, and report link.
4. Message status callbacks and safe retry handling.
5. Template/messaging-window constraints documented and enforced where possible.

### 9B. Voice-note interviews

Deliverables:

1. Inbound media event parsing and authorization to the correct session.
2. SSRF-safe media download restricted to expected provider hosts and HTTPS.
3. Audio size/type/duration validation, safe decoding/transcoding path, and
   transcription through the shared speech abstraction.
4. Async turn processing with duplicate/expired-media handling.
5. Same canonical report as text/voice channels.

### 9C. Live WhatsApp calling

**Status: Pending capability verification**  
Implement only after confirming call direction, onboarding, regional support,
user consent, real-time media access, and webhook behavior in current official
documentation. If real-time media is unavailable through the selected provider,
mark the feature **Blocked** and do not simulate it with text messages.

## Phase 10 — Microsoft Teams

**Status: Pending**  
**Live status: Blocked by Microsoft tenant/app registration, admin consent, and meeting policy**

Verified provider starting points:
- https://learn.microsoft.com/en-us/graph/api/resources/communications-api-overview
- https://learn.microsoft.com/en-us/graph/api/resources/onlinemeeting
- https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/calls-meetings-bots-overview

### 10A. Scheduling and coordination

**Status: Partial — implemented, not live-verified**

Implemented:

- Provider-neutral calendar contract with Google Calendar `events.insert` and
  Microsoft Graph `POST /me/events` (or `/users/{id}/events`).
- Google Meet conference requests and Teams `isOnlineMeeting` /
  `teamsForBusiness` requests.
- Static access-token and refresh-token OAuth paths with in-process token cache.
- Admin API (`POST /calendars/events`, `GET /calendars/providers`).
- Scheduler `calendar_event` action.
- Unit tests for provider request bodies and validation.

Pending: live Google/Microsoft OAuth validation, reschedule/cancel APIs,
meeting/session correlation persistence, least-privilege consent documentation,
and tenant setup guide.

### 10B. Transcript-based evaluation

Deliverables: authorized transcript discovery/retrieval, participant-to-role
mapping where reliable, normalized segments with speaker/timing, evaluation
through the shared engine, and structured report. Handle unavailable, delayed,
incomplete, or revoked transcripts.

### 10C. Autonomous live participation

**Status: Blocked / unverified**

Requires supported Teams calling/media bot architecture, app registration,
tenant permissions, media infrastructure, recording/transcription policy
compliance, and live validation. A calendar event or meeting URL is not
sufficient. Do not implement a text-only bot and call it a live interviewer.

## Phase 11 — Multi-channel security and operations

**Status: Pending**

1. Webhook signature validation and replay protection for every provider.
2. SSRF-safe media fetching and safe file processing.
3. Per-channel credential isolation and secret-management guidance.
4. Cross-tenant/candidate isolation and scoped access tokens.
5. Durable provider-event deduplication and audit events.
6. Consent, recording, retention, opt-out, and deletion workflows per channel.
7. Correlation IDs, latency/error metrics, queue depth, and provider failure
   counters without PII in labels.
8. Scheduled outbound actions: implemented in Rust with SQLite, idempotency,
   bounded retries, cancellation, and stale-claim recovery.
8. Public HTTPS/WSS deployment, rate limits, and abuse controls.

## Phase 12 — Packaging, CI, and live validation

**Status: Pending**

1. CI for Twilio/WhatsApp/Teams adapter contract tests (no production secrets).
2. Opt-in live-provider test harness with isolated credentials and fixtures.
3. Docker/deployment documentation for public webhook ingress and media.
4. Packaging validation and supported-environment matrix.
5. Final capability matrix and release notes.

---

# Channel capability matrix (current)

| Capability | Status | Evidence / blocker |
|---|---|---|
| Existing web voice interview | Implemented and tested | Rust gateway + Python AI + client tests/smoke |
| Native Rust speech studio | Implemented and tested | Runtime tests + live engine smoke |
| Optional local neural TTS/ASR | Implemented and tested | Real TTS→ASR round-trip; models not bundled |
| Native chunked TTS | Implemented and tested | Gateway stream smoke; not truly incremental yet |
| Managed-call workflow | Implemented, not live-verified | Mock HTTP transport tests only |
| Shared channel-independent domain | Implemented and tested | Rust contracts + SQLite mapping/idempotency + cross-channel tests |
| Channel-neutral session/message API | Implemented and tested | `/channels/{channel}/sessions` and `/messages` |
| Twilio outbound/inbound voice | Implemented, not live-verified | Signed webhook + TwiML smoke; live account needed |
| Twilio real-time media | Implemented, not live-verified | Signed TwiML + WebSocket media smoke; no live call |
| WhatsApp text interviews | Implemented, not live-verified | Signed webhook smoke; approval needed |
| WhatsApp voice notes | Implemented, not live-verified | Media/transcription path tested with mocks; live media untested |
| WhatsApp live voice | Blocked / unverified | Provider capability and approval must be confirmed |
| Teams scheduling | Partial — implemented, not live-verified | Graph event + Teams join URL; consent needed |
| Teams transcript evaluation | Pending | No Graph/transcript client; meeting policy needed |
| Teams autonomous AI participation | Blocked / unverified | Requires supported calling/media bot architecture |

---

# Honest gap register (2026-10-25)

This is the explicit “not done / not verified” list. Do not describe these as
complete in README, sales material, or release notes.

| Gap | Status | Evidence | Risk / impact | Priority |
|---|---|---|---|---|
| Live Twilio voice/Media Streams validation | Not live-verified | Signed webhook/media smoke with mock engine only | Provider behavior, latency, media limits, call failures | P0 |
| Live WhatsApp text/voice-note validation | Not live-verified | Signed mock webhook + SSRF checks | Approval/template/window behavior, media expiry | P0 |
| Live Google Calendar / Microsoft Graph validation | Not live-verified | Request-body/unit tests and mock transports | OAuth consent, tenant policy, meeting URL correctness | P0 |
| Resume after outage | Partial | Backend policy/resume API is tested; gateway/client reconnect and expiry edge cases are not | Candidate loses progress; scope/security bugs | P0 |
| Teams transcript evaluation | Missing | No Graph transcript client | Cannot evaluate Teams-native meetings | P1 |
| Teams autonomous live participation | Blocked | Requires supported calling/media bot, tenant permissions, media infra | Cannot claim live AI participation | Blocked |
| WhatsApp live calling | Blocked/unverified | Provider capability/region/account approval unconfirmed | Must not fake with messaging | Blocked |
| Scheduling workflow | Partial | Scheduler/reminders/calendar events exist; no availability negotiation, reschedule, no-show, or email | Manual recruiter work; candidate drop-off | P1 |
| Email channel | Missing | Twilio SMS/WhatsApp/voice only | Incomplete outreach coverage | P1 |
| ATS/HRIS integration | Missing | No Workday/SuccessFactors/etc. adapter | Enterprise adoption gap | P2 |
| Sourcing / semantic match | Missing | No candidate search/ranking module | Out of core interview scope | P2 |
| Multilingual interviews | Missing | English-first templates/ASR/TTS | Global hiring limitation | P2 |
| Role benchmarks / scorecard versioning | Missing | Evidence reports exist; no calibrated role bands or rubric version field | Comparability across candidates/roles | P1 |
| PDF/recruiter export UI | Partial | JSON/Markdown/HTML/CSV API; no PDF or UI | Recruiter workflow friction | P2 |
| Tenant isolation / scoped credentials | Missing | One global key; session tokens are partial | Cross-tenant data leakage | P0 |
| TLS/WSS deployment | Missing | Docs only; local HTTP/WS defaults | Remote deployment unsafe | P0 |
| Consent/retention operations | Partial | Delete endpoints; no policy UI/automation or disk audio retention | Privacy/legal exposure | P0 |
| AI-service audit coverage | Partial | Gateway audit only; review/delete/export in ai-service not audited | Weak accountability | P0 |
| Observability/load/cost | Missing | No metrics/tracing export, load tests, or cost-per-interview | No SLO/regression evidence | P1 |
| Proctoring/identity/liveness/emotion/gaze/MBTI | Deliberately excluded | Not implemented; see competitive intelligence | Biometric/high-risk processing without consent/DPIA | Blocked |

---

# Dependency-ordered pending steps

## P0 — correctness, security, and validation

1. Finish and test outage resume end-to-end: gateway reconnect, client token
   persistence, expiry/disabled behavior, replay safety.
2. Run live Twilio, WhatsApp, Google Calendar, and Microsoft Graph validation
   with approved accounts and public HTTPS/WSS endpoints.
3. Tenant isolation, scoped credentials, TLS/WSS, and shared quotas.
4. AI-service audit coverage for reviewer, deletion, and export actions.
5. Consent/retention policy, deletion verification, and recording controls.

## P1 — workflow and evaluation depth

6. Availability negotiation, reschedule/cancel/no-show automation, and email.
7. Role benchmarks, rubric versioning, and scorecard calibration.
8. Teams transcript retrieval/evaluation after Graph permission verification.
9. Multilingual templates, ASR/TTS, and report localization.
10. Observability: correlation IDs, latency/error metrics, load/cost baselines.

## P2 — advanced / out of scope

11. ATS/HRIS integration boundary and sourcing/match evaluation.
12. PDF/recruiter report UI and signed external report links.
13. WhatsApp live calling only after capability verification.
14. Teams autonomous live participation only after platform/permission verification.
15. Proctoring/identity/liveness/emotion/gaze/MBTI only after consent, DPIA,
    and jurisdiction review; otherwise remain blocked.

## P3 — release

16. Docker/deployment reference, backups, and public-webhook hardening.
17. Live-provider CI harness (opt-in), packaging validation, and release notes.

# External blockers and required inputs

| Blocker | Needed for |
|---|---|
| Twilio Account SID/Auth Token and approved sender/caller | Live Twilio voice/WhatsApp tests |
| Public HTTPS webhook URL (tunnel or deployed environment) | Live provider callbacks |
| WhatsApp Business account, sender, templates, opt-in policy | WhatsApp text/voice-note/live |
| Verified WhatsApp calling/media capability for the selected account/region | Live WhatsApp voice |
| Microsoft Entra app registration, tenant admin consent, Graph permissions | Teams scheduling/transcripts |
| Teams meeting/recording/transcript policy | Teams transcript evaluation |
| Supported Teams calling/media bot deployment | Autonomous live Teams participation |
| Legal/privacy review and consent/retention policy | Any production hiring deployment |

No live-provider test has been run. All current provider statements are
documentation-based and must be re-verified against the operating account.

---

# Immediate next milestone

1. Add resume-policy/resume tests and gateway/client reconnect support.
2. Run live-provider validation for the already implemented Twilio/WhatsApp/
   calendar adapters, or record the exact credential/approval blockers.
3. Complete tenant isolation, TLS/WSS, AI-service audit, and consent/retention
   controls before any remote deployment.
4. Add availability/reschedule/no-show workflow on top of the existing scheduler
   and calendar layer.

Do not add proctoring, biometric identity checks, or personality/MBTI inference
without legal/privacy review and explicit candidate consent.
