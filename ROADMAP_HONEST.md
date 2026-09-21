# Honest Status / Roadmap

This is the disclosure doc: what's actually verified working, what's built
but unverified, what's flat-out not built yet, and what's broken in CI.
Cross-reference with [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the
architectural rationale behind the scope cuts.

Last updated: 2026-09-22, after an independent OSS-maturity audit pass that
re-ran everything below rather than trusting the 2026-09-19 version of this
document. See "2026-09-22 audit pass" at the bottom for exactly what was
re-verified and what new gaps were found.

## Built and verified working

- Gateway session state machine (19 unit tests) and VAD/preroll/turn-buffer
  logic (unit tested against synthetic PCM)
- Full real-time pipeline end-to-end: barge-in, VAD end-of-turn, faster-whisper
  transcription, Ollama-driven adaptive questioning + evidence extraction,
  Piper TTS playback-duration-aware turn-taking — validated by a live
  WebSocket integration test using real synthesized audio
  (`ai-service/scripts/e2e_ws_test.py`), not mocks
- SQLite persistence of transcript + evidence, readable via
  `GET /sessions/{id}/summary`
- One full demo interview (RAG Architecture competency, 3 evidence nodes,
  6 turns, correct graceful conclusion) run start to finish

## Built but not independently tested

- **ai-service has zero automated tests.** `pytest`/`pytest-asyncio`/`httpx`
  are dev dependencies and a `tests/` directory doesn't even exist yet — the
  only verification of `asr.py`/`tts.py`/`llm.py`/`agent.py`/`db.py` is the
  one manual e2e script and ad-hoc `curl` calls during development.
- **gateway's async orchestration has zero Rust-level test coverage.** The
  state machine and VAD are unit tested in isolation, but `ws_handler.rs`'s
  `speak()`/`listen_for_turn()`/`speak_recovery()` functions — the actual
  `tokio::select!` concurrency, barge-in race handling, and error-recovery
  flow — are only exercised indirectly through the Python e2e script, never
  with a Rust integration test.
- **The candidate client's real browser flow was never exercised with a
  real microphone.** Verified visually (page loads, mic-test screen matches
  the design) in a sandboxed browser automation environment with no working
  audio input device — the mic-test button gets stuck at "Listening for
  your voice..." there. The actual mic-capture → WS → playback path in a
  real browser has not been human-tested.
- **The recruiter view (`/recruiter`) was never opened in a browser.** Its
  backing API endpoints (`GET /sessions`, `GET /sessions/{id}/summary`)
  were curl-tested directly; the rendered page itself was not.
- **`speak_recovery()` (the ASR/agent-failure fallback path, spec §28) has
  never actually fired.** It's implemented and reachable, but no real ASR
  or Ollama failure occurred during testing to exercise it.
- **`PAUSE`/`RESUME` state transitions are unit tested but unreachable in
  practice** — no UI control, WS message type, or code path ever triggers
  them yet.

## Not built at all

Grouped roughly by the original product spec's sections:

- **Voice persona configuration** (voice/accent/gender/speed/formality/
  personality/language/vocabulary) — one fixed Piper voice, no config surface
- **Multi-language interviews** — English only
- **General scenario/simulation engine** — the one "traffic increased 20x"
  scenario is a hardcoded instruction in the system prompt, not a reusable
  branching state-machine engine with tracked system state
- **Voice + work-artifact hybrid modality** — no whiteboard, code canvas,
  or architecture-diagram interaction; voice-only
- **Multi-competency evidence graph** — the graph exists for exactly one
  hardcoded competency; no competency-graph builder from a real job
  description
- **Resume upload + real parsing + claim extraction** — one hardcoded seed
  claim; no upload UI, no document parser, no LLM-based claim extraction
- **JD parser** — not built
- **Interview planner beyond one competency** — not built
- **Explicit claim-verification status** distinct from evidence confidence
  — verification is currently implicit in evidence status, not a separate
  tracked "insufficient verification" outcome
- **Dedicated technical-depth/expertise-boundary detection** — implicit via
  evidence "weak" status; no standalone boundary-finding mechanism
- **Human interviewer copilot** — not built
- **True WebRTC transport** — raw WebSocket + PCM16 is used instead; works,
  but isn't WebRTC
- **Recording/retention/consent configuration** — no audio is persisted at
  all (transcript + evidence only); no retention policy, no consent screen,
  no candidate/recruiter access toggles
- **Formatted assessment report** — the recruiter view is a raw evidence
  table; no confidence rollup, no hiring recommendation summary, no export
- **Formal provider-abstraction interfaces** — one concrete hardcoded
  implementation each for ASR/LLM/TTS; swapping providers means editing
  `asr.py`/`llm.py`/`tts.py` directly, not registering a new implementation
  against a trait/interface
- **Calibration / evidence-correlates-with-outcome learning loop** —
  aspirational long-term idea from the spec, not started
- **Authentication/authorization** — none anywhere; the gateway WebSocket
  and ai-service HTTP API are both open on localhost
- **Deployment story** — no Dockerfile, no docker-compose, no production
  deployment docs; local-only right now
- **Rate limiting / abuse protection** — none

## CI

- **`.github/workflows/ci.yml` runs on every push/PR to `main`:** gateway
  (`cargo fmt --check`, `cargo clippy -D warnings`, `cargo test`),
  ai-service (`uv sync`, `ruff check`, an import sanity check), and
  candidate-client (`tsc --noEmit`, `npm run build`).
- **No `pytest` step** — ai-service still has zero automated tests (see
  above), so there's nothing for pytest to run yet. The workflow has a
  comment marking exactly where to add it once tests exist.
- **`ai-service/scripts/e2e_ws_test.py` does not run in CI** — it needs
  Ollama serving `qwen2.5:7b-instruct` and a downloaded Piper voice, which
  a standard GitHub-hosted runner doesn't have. It's meant to be run
  locally (see README); wiring up a CI runner with those models installed
  is still open.
- **No automated PyPI release workflow.** `pyinterviewbot-ai` 0.1.0 was
  published via a manual `twine upload`, not an Actions job (consistent
  with how other Mullassery repos currently handle releases).

## Features present in code but not fully functional

- Recruiter session list is capped at 50 (`ORDER BY created_at DESC LIMIT 50`
  in `db.list_sessions`) with no pagination or search — fine for a demo,
  not for real usage volume
- Candidate client's `onError` text rendering is wired up but has never
  actually been triggered/observed against a real failure

## 2026-09-22 audit pass

An independent OSS-maturity/documentation pass re-verified the claims in
this document against the actual code and re-ran everything that could be
re-run, rather than trusting the 2026-09-19 version. Nothing above was
found to be false. What follows is new.

### Re-verified, still true

- `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, and
  `cargo test` all pass clean — 19/19 gateway unit tests, same set listed
  above.
- `ai-service`: `uv sync`, `uv run ruff check .`, and the
  `from pyinterviewbot_ai.main import app` import sanity check all pass.
  Confirmed `ai-service` still has zero `tests/` directory and zero
  `pytest` tests.
- `candidate-client`: `npm ci`, `npx tsc --noEmit`, `npm run build` all
  pass clean.
- `ai-service/scripts/e2e_ws_test.py` was actually re-run end-to-end
  against real local Ollama (`qwen2.5:7b-instruct`), real faster-whisper,
  and real Piper (not skipped, not mocked): barge-in fired and was
  acknowledged by the gateway, 5/5 canned candidate turns were answered,
  and the interview reached `is_complete` with a graceful closing remark.
  This independently reconfirms the "Full real-time pipeline end-to-end"
  claim above still holds.
- `actionlint` run against `.github/workflows/ci.yml`: no findings.
- `cargo audit` run against `Cargo.lock` (229 crates, RustSec advisory
  DB): no advisories.
- `npm audit` run against `candidate-client`: 0 vulnerabilities.
- No secrets, API keys, or `.env` files found committed anywhere in the
  repo (`git grep` for key/token/secret/password patterns and `sk-`-style
  strings, plus a check of tracked files for `.env*`) — all clean.
- No merge-conflict markers, no `|| true`/`|| echo`-style CI masking, no
  hardcoded default secrets, no version ceilings blocking security patches
  found in `Cargo.toml` or `pyproject.toml`.

### New findings from this pass

- **CORS is wildcard-open on ai-service.**
  `ai-service/src/pyinterviewbot_ai/main.py:33` sets `allow_origins=["*"]`
  (with `allow_methods=["*"]`, `allow_headers=["*"]`). Combined with the
  already-documented lack of auth, this means any web page a user has open
  can script a cross-origin request to `127.0.0.1:8000/sessions` and read
  interview transcripts/evidence, if that port is reachable from the
  browser. Not previously called out explicitly as its own item — folded
  into "Authentication/authorization: none" above, but this is a distinct,
  concrete line of code worth fixing on its own. Not fixed in this pass
  (real behavior change, needs a decision on what origins to actually
  allow) — needs a dedicated follow-up.
- **No HTTP client timeout in the gateway's `AiClient`.**
  `gateway/src/ai_client.rs:65` constructs `reqwest::Client::new()` with no
  `.timeout(...)` configured. If `ai-service` hangs (e.g. Ollama stalls),
  every outbound call the gateway makes to it — `start_session`,
  `agent_turn`, `transcribe`, `synthesize_stream` — can block
  indefinitely with no client-side cutoff, which would hang the whole
  session's `tokio::select!` loop rather than failing over to
  `speak_recovery()`'s error path. Not fixed in this pass (needs a
  considered timeout value and retry/fallback behavior) — flagged for a
  dedicated follow-up.
- **No `pip-audit` (or equivalent) coverage for ai-service's Python
  dependencies.** `pip-audit` wasn't available in the environment this
  audit ran in, so `faster-whisper`, `ollama`, `piper-tts`, `fastapi`, and
  the rest of `ai-service/pyproject.toml`'s dependencies have not been
  checked against any vulnerability database. This is an open gap in
  dependency-security coverage, not a clean result — do not read the
  `cargo audit`/`npm audit` clean results above as implying the same for
  Python.
- **`gateway/src/state_machine.rs`'s `#[allow(dead_code)]` items are
  legitimate, not lint-suppression debt.** `ProcessingStarted` (line 43),
  `Pause`/`Resume` (lines 53/56), and `Recovered` (line 63) are annotated
  and each has an inline comment explaining exactly why they're currently
  unreachable (matches the `PAUSE`/`RESUME` gap already documented above).
  Called out here only so a future contributor doesn't assume these are
  unexplained lint suppressions needing cleanup — they're deliberate and
  already tracked.
- **`.github/dependabot.yml` did not exist before this pass** — dependency
  updates for `cargo`, the `ai-service` `uv`/`pip` stack, `npm`, and the
  CI workflow's own GitHub Actions versions were not automated at all.
  Added in this pass.
- **No `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`,
  `CHANGELOG.md`, GitHub issue templates, or a PR template existed before
  this pass.** All added in this pass; see each file for specifics
  (`SECURITY.md` in particular documents the CORS/auth/TLS gaps above in
  one place for anyone assessing whether to deploy this beyond localhost).

Nothing above was fixed as code in this pass except the new
documentation/scaffolding files themselves — the CORS and HTTP-timeout
findings are real bugs/gaps that need deliberate follow-up work, not
one-line fixes, per this pass's own ground rules.
