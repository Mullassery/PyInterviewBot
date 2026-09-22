# Security Policy

## Status

PyInterviewBot is a working MVP slice, not a hardened or production
system. It is **not remotely deployable as-is** — see the concrete gaps
below before running it anywhere other than `localhost` on a machine you
trust.

## Supported versions

None. This is pre-1.0 (`0.1.0`), single-branch (`main`), no LTS/patch
policy. Security fixes land on `main` only.

## No API keys / no cloud credentials involved

ASR (faster-whisper), the LLM (Ollama), and TTS (Piper) all run locally.
There are no API keys, tokens, or cloud LLM credentials anywhere in this
stack today, and none are read from environment variables. If a cloud
provider integration is added later, that provider's key(s) must be read
from environment variables or a secrets manager — never hardcoded or
committed — and this document must be updated to say so explicitly.

## Known, current security gaps (not hypothetical — verified in code)

- **No authentication or authorization anywhere.** The gateway's WebSocket
  endpoint and every ai-service HTTP endpoint (`/sessions`,
  `/sessions/{id}/turn`, `/sessions/{id}/summary`, `/asr/transcribe`,
  `/tts/synthesize`) are open to any client that can reach the port. This
  includes the `/recruiter` view's data endpoints, which expose full
  interview transcripts and evidence.
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
  "no authentication or authorization anywhere" gap below still applies.
- **No rate limiting or abuse protection** on any endpoint, gateway or
  ai-service.
- **No TLS.** Both the gateway (`ws://`) and ai-service (`http://`) default
  to plaintext on `127.0.0.1`. There is no WSS/HTTPS configuration anywhere
  in this repo.
- **No input size limits observed** on the WebSocket audio path or the
  `/asr/transcribe` raw-body endpoint — a malicious or buggy client could
  send an unbounded amount of data per turn.
- Because of the above, the only currently-safe way to run this is fully
  local, on a single trusted machine, with nothing else on that machine
  untrusted enough to hit `127.0.0.1:8000` or `127.0.0.1:8787`.

None of this is "planned to be fixed" on any timeline — it is simply not
built. Treat this software as a local development/demo artifact only
until this section changes.

## Reporting a vulnerability

Please **do not open a public GitHub issue** for a security
vulnerability. Instead, email **mullassery@gmail.com** with:

- A description of the issue and its impact.
- Steps to reproduce (or a PoC).

This is a single-maintainer hobby-scale project — there is no formal SLA,
but reports will be read and acknowledged as soon as possible.

## Dependency security

- `cargo audit` was run against `gateway`'s `Cargo.lock` on 2026-09-22:
  no advisories reported (229 crates scanned against the RustSec advisory
  database).
- `npm audit` was run against `candidate-client`: 0 vulnerabilities
  reported (small dependency surface — `typescript` + `vite` as
  devDependencies only).
- No `pip-audit` / equivalent has been run against `ai-service`'s Python
  dependencies (`faster-whisper`, `ollama`, `piper-tts`, `fastapi`, etc.) —
  the tool wasn't available in the environment this audit was done in.
  This is an open gap, not a clean bill of health.
- Dependency updates are tracked via `.github/dependabot.yml` (cargo, npm,
  and uv/pip for `ai-service`, plus GitHub Actions).
