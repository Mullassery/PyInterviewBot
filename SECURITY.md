# Security Policy

## Status

PyInterviewBot is a working MVP slice, not a hardened or production
system. It is **not remotely deployable as-is** — see the concrete gaps
below before running it anywhere other than `localhost` on a machine you
trust.

## Supported versions

None. This is pre-1.0 (`0.1.0`), single-branch (`main`), no LTS/patch
policy. Security fixes land on `main` only.

## Local by default; optional provider credentials

ASR (faster-whisper), the LLM (Ollama), and TTS (Piper) all run locally and
need no cloud credentials. The optional OpenRouter provider reads
`OPENROUTER_API_KEY`; managed-call integration reads `MANAGED_CALL_API_KEY`
and its webhook secret. These keys are opt-in, read only from the
environment or a secrets manager, and must never be hardcoded, committed,
or sent to browser code. Selecting a cloud provider adds that provider as a
trust boundary.

## Known, current security gaps (not hypothetical — verified in code)

- **Tenant authorization is principal-based, not account-based.** A tenant is
  the `subject` of an `X-API-Key` entry in `PYINTERVIEWBOT_PRINCIPALS`.
  Session, channel, compliance, and workflow routes bind data to that subject;
  `admin` principals may cross tenant boundaries. With
  `PYINTERVIEWBOT_PRINCIPALS` configured, unknown keys are rejected. The legacy
  `PYINTERVIEWBOT_API_KEY` identity can still authenticate non-tenant utility
  routes but is rejected with `403` on session and compliance routes, and a
  non-loopback deployment must configure principals before interview traffic
  is served. A valid session token does not substitute for a matching tenant.
  Cross-tenant session and workflow IDs return `404`, not `403`.
- **Live workflow routing is tenant-checked.** A new direct or channel session
  that supplies `workflow_id` is pinned to the active production deployment
  only after the workflow owner matches the session owner (or the caller is
  `admin`), and after status, deployment, binding fingerprint, version hash,
  and artifact hash checks. Omitting it keeps the legacy `template_id` path;
  there is no automatic selection. The gateway forwards one ai-service key, so
  its principal subject must match the workflow owner; run one gateway process
  per tenant rather than sharing one process across owners.
- **AI-service audit coverage is partial.** Session starts (including denied
  foreign-workflow starts), review, deletion, and report-export actions append
  actor/tenant/target records to `ai_audit_events` in the same transaction as
  the session write. Read-only summary/report/priority/resume-policy access and
  plain turns are not audited, and neither audit stream has automated retention
  or an operator UI.
- **Existing databases are assigned deterministically.** Migration backfills
  routed sessions from their workflow owner and all remaining sessions from
  `PYINTERVIEWBOT_LEGACY_SESSION_OWNER`, then
  `PYINTERVIEWBOT_API_KEY_SUBJECT`, then `local-operator` (when
  `PYINTERVIEWBOT_API_KEY` is set) or `local-loopback`. Set the legacy owner
  before starting the upgraded service when old sessions belong to a real
  tenant; a wrong default is a data-classification problem, not a boundary
  bypass.
- **CORS wildcard — fixed 2026-09-22.** `ai-service/src/pyinterviewbot_ai/main.py`
  used to set `allow_origins=["*"]`, letting any page in a user's browser
  script a request to `http://127.0.0.1:8000/sessions`. It now reads a
  configurable allowlist from `ALLOWED_ORIGINS` (comma-separated),
  defaulting to the candidate-client's actual dev-server origins
  (`http://localhost:5173`, `http://127.0.0.1:5173`) instead of `*`.
  Verified with a CORS preflight test: the candidate-client's origin gets
  `Access-Control-Allow-Origin` back, an arbitrary origin is rejected.
  This is a mitigation, not authentication — the endpoints are still open
  to any *same-allowed-origin or direct (non-browser)* client, so the
  shared-key authorization gap above still applies.
- **Rate limiting is partial.** ai-service has a bounded in-process sliding
  window (`PYINTERVIEWBOT_RATE_LIMIT`, default 120/60s), but it is per worker
  and not shared. There is no gateway-level limiter, quota, or distributed
  abuse protection yet.
- **No TLS.** Both the gateway (`ws://`) and ai-service (`http://`) default
  to plaintext on `127.0.0.1`. There is no WSS/HTTPS configuration anywhere
  in this repo.
- **Audio size limits are enforced, but memory is still a DoS surface.**
  The gateway caps a turn (`GATEWAY_MAX_UTTERANCE_BYTES`, default ~100s of
  16kHz PCM16) and WebSocket frame/message size; ai-service rejects
  `/asr/transcribe` bodies over `PYINTERVIEWBOT_MAX_AUDIO_BYTES` (default
  10 MiB) with 413. There is still no rate limiting, quota, or global
  concurrency limit.
- **Provider credentials must stay server-side.** The optional managed-call
  workflow reads `MANAGED_CALL_API_KEY` only on the server; webhook verification
  supports a configured Bearer token or HMAC secret. Never place provider
  private keys in browser or mobile code.
- **Model weights keep upstream terms** even though this repo is Apache-2.0.
  See [`docs/LICENCES.md`](docs/LICENCES.md) and
  [`docs/CAPABILITY_MATRIX.md`](docs/CAPABILITY_MATRIX.md).
- Because of the above, the only currently-safe way to run this is fully
  local, on a single trusted machine, with nothing else on that machine
  untrusted enough to hit `127.0.0.1:8000` or `127.0.0.1:8787`, unless you
  add TLS, per-session authorization, and rate limiting.

None of this is "planned to be fixed" on any timeline — it is simply not
built. Treat this software as a local development/demo artifact only
until this section changes.

## Threat model and required controls

See [`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md) for the asset/trust
boundary list, implemented controls, open risks, and the controls required
before any remote deployment (TLS/WSS, session-scoped authorization, rate
limits, consent/retention policy, legal/privacy review).

## Reporting a vulnerability

Please **do not open a public GitHub issue** for a security
vulnerability. Instead, email **mullassery@gmail.com** with:

- A description of the issue and its impact.
- Steps to reproduce (or a PoC).

This is a single-maintainer hobby-scale project — there is no formal SLA,
but reports will be read and acknowledged as soon as possible.

## Dependency security

- `cargo audit` was re-run on 2026-10-25 after dependency updates: no known
  vulnerabilities. The previous `rustls`, `rustls-webpki`,
  `webpki-root-certs`, `tokio`, and yanked `yoke-derive` findings were
  remediated.
- `npm audit` was re-run on 2026-10-25: 0 vulnerabilities. `vite` is 8.3.5 and
  `source-map-js` resolves to 1.2.2.
- `pip-audit` was run on 2026-10-25 against `ai-service`: no known
  vulnerabilities in the resolved environment.
- The live managed-call path is contract-tested against a mock HTTP transport
  only; it has not been security-tested against a live service.
- Dependency updates are tracked via `.github/dependabot.yml` (cargo, npm,
  and uv/pip for `ai-service`, plus GitHub Actions).
