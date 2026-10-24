# Security and privacy model — current state

Last reviewed: 2026-10-25. This complements `SECURITY.md` (reporting and known
gaps) and `docs/CAPABILITY_MATRIX.md` (provider/licence boundaries).

## Assets

- Candidate audio in transit, transcripts, evidence, reports, reviewer notes.
- Provider credentials: `PYINTERVIEWBOT_API_KEY`, `GATEWAY_API_KEY`,
  `AI_SERVICE_API_KEY`, `MANAGED_CALL_API_KEY`,
  `MANAGED_CALL_WEBHOOK_SECRET`.
- Model files and local SQLite data.

## Trust boundaries

1. **Candidate browser → Rust gateway**: untrusted WebSocket input, including
   malformed/oversized binary frames and reconnects.
2. **Gateway → ai-service**: authenticated HTTP; the gateway may only send
   normalized operation fields.
3. **ai-service → model/provider**: local model processes or optional HTTP
   services. Transcripts/model outputs are untrusted input to evaluation.
4. **Recruiter client → ai-service**: privileged transcript/evidence/report
   access.
5. **Optional managed-call provider → service webhook**: unauthenticated unless
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
| Native studio recording | Explicit caller action only; in-memory, size-bounded, clearable, and dropped on session close |
| Channel media downloads | HTTPS + Twilio host allowlist, no userinfo, 10 MiB cap, Basic auth |
| Biased scoring | Report policy excludes accent, attractiveness, emotion, personality, protected traits; human review required |
| Provider secrets in browser | Provider calls are server-side only; the managed-call client reads its private key on the server |
| Webhook spoofing | Constant-time provider signature checks (Twilio HMAC-SHA1; managed-call Bearer/HMAC) |
| Prompt/model output treating as truth | Evidence notes flagged as model inference; transcript quotes are the observed evidence |
| Unknown provider misconfiguration | Registry fails closed with a clear error |

## Open risks

- No TLS/WSS termination in this repo. Run behind a trusted reverse proxy.
- One global API key; session tokens protect `/turn` when enforcement is on, but
  any global-key holder can still read any session UUID. There is no tenant
  isolation or scoped recruiter key. Do not expose the recruiter API across
  trust boundaries yet.
- Rate limiting is per-process only. Add a shared proxy/gateway limit and
  quotas before network deployment.
- The interview WebSocket flow does not record audio. The native studio has an
  explicit in-memory recording API with clear/session-close deletion, but no
  disk retention policy or consent UI for that endpoint yet.
- No SSRF-heavy URL fetching in the current paths; managed-call base URLs are
  operator-configured. Do not let candidates supply provider URLs.
- Python dependency vulnerability scanning (`pip-audit`) remains unverified in
  this environment.
- The live managed-call path is not exercised in CI; contract tests use a
  deterministic mock transport only.
- Model licences and terms are not enforced in code; operators must verify
  weights before deployment.

## Required before remote deployment

1. TLS/WSS termination and strict network policy.
2. Session-scoped authorization (or per-tenant isolation) for transcript,
   evidence, report, review, and delete operations.
3. Rate limits, request quotas, and audit logging for administrative actions.
4. A documented retention policy and consent flow, including whether audio is
   ever enabled.
5. Legal/privacy review for the target jurisdiction and a bias/validation study
   before any employment decision support.
