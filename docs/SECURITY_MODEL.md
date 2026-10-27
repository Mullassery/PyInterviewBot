# Security and privacy model — current state

Last reviewed: 2026-10-26. This complements `SECURITY.md` (reporting and known
gaps) and `docs/CAPABILITY_MATRIX.md` (provider/licence boundaries). Workflow
artifact identity and promotion controls are detailed in
[`docs/WORKFLOWS.md`](WORKFLOWS.md).

## Assets

- Candidate audio in transit, transcripts, evidence, reports, reviewer notes.
- Workflow drafts, immutable artifacts, sandbox evidence, approvals, deployment
  history, and production binding fingerprints.
- Provider credentials: `PYINTERVIEWBOT_API_KEY`,
  `PYINTERVIEWBOT_PRINCIPALS`, `GATEWAY_API_KEY`, `AI_SERVICE_API_KEY`,
  `MANAGED_CALL_API_KEY`, `MANAGED_CALL_WEBHOOK_SECRET`.
- Model files, workflow data, interview data, and the separate Rust call-state
  SQLite database.

## Trust boundaries

1. **Candidate browser → Rust gateway**: untrusted WebSocket input, including
   malformed/oversized binary frames and reconnects.
2. **Gateway → ai-service**: authenticated HTTP; the gateway may only send
   normalized operation fields.
3. **ai-service → model/provider**: local model processes or optional HTTP
   services. Transcripts/model outputs are untrusted input to evaluation.
4. **Recruiter client → ai-service**: privileged transcript/evidence/report
   access.
5. **Workflow author/approver/deployer → ai-service**: privileged artifact and
   production-control access; workflow roles and ownership are enforced
   independently of gateway transport auth.
6. **Optional managed-call provider → service webhook**: unauthenticated unless
   the operator configures a Bearer/HMAC credential and verifies it.

## Implemented controls

| Risk | Control |
|---|---|
| Unauthenticated API access | API key (`X-API-Key` or Bearer) or loopback-only fail-closed; `/health` exempt |
| Session-turn access | Per-session token returned at creation, hash stored at rest, forwarded by the gateway; optional enforcement |
| Request flooding | In-process sliding-window limiter per API key/IP; `/health` exempt |
| Gateway-to-ai auth break | Gateway forwards `AI_SERVICE_API_KEY`/`PYINTERVIEWBOT_API_KEY`; tested |
| Oversized audio / memory growth | Per-turn byte cap, WebSocket frame/message cap, `/asr/transcribe` 413 limit |
| Cross-origin browser reads | Configurable CORS allowlist; no wildcard default |
| Session deletion/retention | `DELETE /sessions/{id}` removes transcript, evidence, reviews |
| PII in reports | Reports contain transcript quotes and rubric evidence only; interview-flow audio is not persisted |
| Unsafe interview questions | Editable compliance policy blocks protected-characteristic/legal-risk patterns and replaces unsafe generated questions |
| Red-flag interpretation | Flags are evidence/process signals only; no dishonesty, personality, or suitability inference |
| Native studio recording | Explicit caller action only; in-memory, size-bounded, clearable, and dropped on session close |
| Channel media downloads | HTTPS + Twilio host allowlist, no userinfo, 10 MiB cap, Basic auth |
| Calendar credentials | Server-side access/refresh tokens only; never returned by the API |
| Biased scoring | Report policy excludes accent, attractiveness, emotion, personality, protected traits; human review required |
| Provider secrets in browser | Provider calls are server-side only; the managed-call client reads its private key on the server |
| Webhook spoofing | Constant-time provider signature checks (Twilio HMAC-SHA1; managed-call Bearer/HMAC) |
| Prompt/model output treating as truth | Evidence notes flagged as model inference; transcript quotes are the observed evidence |
| Unknown provider misconfiguration | Registry fails closed with a clear error |
| Workflow authorization | Principal map plus owner checks and `author`/`approver`/`deployer`/`admin` roles; no client-chosen owner |
| Workflow artifact tampering | Immutable versions with canonical SHA-256 verification; strict schemas reject unknown fields |
| Approval shortcuts | Approval must reference a passing test run and hash; requester cannot self-approve; assigned approver must be the decider |
| Promoting untested artifacts | Promotion requires the exact tested and approved hash plus a capability-compatible production binding |
| Sandbox escaping | Sandbox binding is fixed to mock `llm`/`asr`/`tts`; policy tests forbid external side effects/network |
| Production binding drift | Binding fingerprint is persisted; rollback rejects a target made under a different binding or when binding config is unavailable |
| Failed secret handling | Artifacts and deployment records store no provider credentials; only provider names and a binding fingerprint |

## Open risks

- No TLS/WSS termination in this repo. Run behind a trusted reverse proxy.
- Interview/session routes still accept one global API key; session tokens
  protect `/turn` when enforcement is on, but any global-key holder can still
  read any session UUID. There is no tenant isolation or scoped recruiter key.
  Do not expose the recruiter API across trust boundaries yet. Workflow routes
  use the newer principals/roles map, but it does not isolate non-workflow data.
- Workflow production deployment is a registry action only. No candidate
  session runtime selects the active deployment yet, so a production row must
  not be represented as a live workflow serving interviews.
- Workflow audit covers lifecycle mutations, not reads, and relies on SQLite
  file permissions and backups for tamper resistance.
- Rate limiting is per-process only. Add a shared proxy/gateway limit and
  quotas before network deployment.
- The interview WebSocket flow does not record audio. The native studio has an
  explicit in-memory recording API with clear/session-close deletion, but no
  disk retention policy or consent UI for that endpoint yet.
- No SSRF-heavy URL fetching in the current paths; managed-call base URLs are
  operator-configured. Do not let candidates supply provider URLs.
- The canonical call DB (`GATEWAY_CALL_DB_PATH`) is a separate SQLite store with
  one writer, the gateway. Restrict filesystem permissions to the gateway user;
  do not share it over a network filesystem or open it from Python. Backups
  contain call metadata and provider call IDs, not provider secrets or media.
- `/calls` uses the gateway API key. Twilio status/inbound webhooks use request
  signature verification; managed-call webhooks require Bearer/HMAC
  verification and fail closed without a configured secret. Do not expose raw
  call state to the candidate client.
- `pip-audit` now reports no known vulnerabilities; CI runs dependency audits.
- The gateway audit trail covers provider/scheduler/calendar/webhook actions.
  AI-service reviewer corrections and deletion endpoints are not yet audited.
- The live managed-call path is not exercised in CI; contract tests use a
  deterministic mock transport only.
- Model licences and terms are not enforced in code; operators must verify
  weights before deployment.

## Required before remote deployment

1. TLS/WSS termination and strict network policy.
2. Session-scoped authorization (or per-tenant isolation) for transcript,
   evidence, report, review, and delete operations.
3. Rate limits, request quotas, audit retention, and audit coverage for
   report/summary reads beyond the current review/delete/export events.
4. A documented retention policy and consent flow, including whether audio is
   ever enabled.
5. Legal/privacy review for the target jurisdiction and a bias/validation study
   before any employment decision support.
