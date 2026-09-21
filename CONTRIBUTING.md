# Contributing

PyInterviewBot is a working MVP slice, not a mature project with a
governance process — see [`ROADMAP_HONEST.md`](ROADMAP_HONEST.md) for
exactly what's built, tested, and missing before you start.

## Before you open a PR

- Read [`README.md`](README.md) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
  first. The three components (`gateway/`, `ai-service/`, `candidate-client/`)
  have a deliberate protocol boundary — the candidate-facing WebSocket
  protocol has no message type for a transcript, score, or evidence, on
  purpose. Don't add one without reading why.
- Check `ROADMAP_HONEST.md`'s "Not built at all" section before assuming
  something is a small gap — several items there are intentionally out of
  scope for this slice, not oversights.
- Open an issue first for anything beyond a small fix, so we don't duplicate
  effort or build something that conflicts with the architecture above.

## Setup

Follow the "Prerequisites" and "Setup" sections in the README — you'll need
Rust, `uv`, Node.js, a running Ollama with `qwen2.5:7b-instruct`, and
`ffmpeg`.

## Running the checks locally before you push

These are exactly what CI (`.github/workflows/ci.yml`) runs — run them
yourself first:

```bash
# gateway (Rust)
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test

# ai-service (Python)
cd ai-service
uv sync
uv run ruff check .
uv run python -c "from pyinterviewbot_ai.main import app; print('import OK')"

# candidate-client (JS/TS)
cd candidate-client
npm ci
npx tsc --noEmit
npm run build
```

`ai-service` has **zero automated tests today** (no `tests/` directory
exists — see `ROADMAP_HONEST.md`). If you touch `asr.py`, `tts.py`,
`llm.py`, `agent.py`, or `db.py`, at minimum run the real end-to-end
integration test before opening a PR:

```bash
# terminal 1
cd ai-service && uv run uvicorn pyinterviewbot_ai.main:app --host 127.0.0.1 --port 8000
# terminal 2
cd gateway && cargo run --release
# terminal 3
cd ai-service && uv run python scripts/e2e_ws_test.py
```

This drives the real gateway WebSocket protocol with real synthesized
audio against real local models (no mocks) — it's the only thing standing
in for a Python test suite right now. Adding real `pytest` coverage for
`ai-service` is one of the most valuable contributions this project can
currently take (see `ROADMAP_HONEST.md`).

## Code style

- Rust: `rustfmt` defaults, `clippy -D warnings` must be clean.
- Python: `ruff` must be clean (`ruff check .`). No formatter is enforced yet.
- TypeScript: `tsc --noEmit` must pass. No linter is configured yet.

## Commit / PR expectations

- Keep PRs scoped to one concern.
- If you fix a bug, prefer adding or extending a test that would have
  caught it (unit test for gateway logic, an addition to
  `e2e_ws_test.py` for cross-component timing bugs — see
  `docs/ARCHITECTURE.md`'s "Two real bugs found" section for why the
  real-timed e2e test matters more than unit tests for this kind of bug).
- Don't leave placeholder/stub code that pretends to work — either
  implement a feature fully or don't include it.
- Update `ROADMAP_HONEST.md` if your change moves something from
  "not built" / "built but untested" into "verified working," or if it
  introduces a new gap.

## Reporting bugs / requesting features

Use the GitHub issue templates
(`.github/ISSUE_TEMPLATE/bug_report.yml`,
`.github/ISSUE_TEMPLATE/feature_request.yml`).

## Security issues

Do not open a public issue for a security vulnerability — see
[`SECURITY.md`](SECURITY.md).

## License

By contributing, you agree your contributions are licensed under the
Apache-2.0 license that covers this repository (see [`LICENSE`](LICENSE)).
