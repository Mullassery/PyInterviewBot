# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/). This project is
pre-1.0 and does not yet follow Semantic Versioning strictly.

## [Unreleased]

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

- OSS project scaffolding: `CONTRIBUTING.md`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md`, `CHANGELOG.md`, `.github/dependabot.yml`,
  `.github/ISSUE_TEMPLATE/`, `.github/pull_request_template.md`.

### Documentation

- `ROADMAP_HONEST.md` updated with a security review (CORS wildcard on
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
- `ROADMAP_HONEST.md`: disclosure of what's verified working, built-but-
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
