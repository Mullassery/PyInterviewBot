## What does this change?

<!-- Describe the change. Which component(s): gateway / ai-service / candidate-client? -->

## Why?

<!-- Link an issue if there is one. -->

## How was this tested?

<!-- Be specific and honest — say exactly what you ran, not what "should" work. -->

- [ ] `cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test` (if `gateway/` changed)
- [ ] `uv run ruff check .` + import sanity check (if `ai-service/` changed)
- [ ] `npx tsc --noEmit && npm run build` (if `candidate-client/` changed)
- [ ] Ran `ai-service/scripts/e2e_ws_test.py` against real local models (required if you touched
      `ws_handler.rs`, `asr.py`, `tts.py`, `llm.py`, `agent.py`, or the WebSocket protocol)
- [ ] Manually tested some other way — describe exactly how below

<!-- If you didn't/couldn't test something, say so plainly here instead of leaving it implied. -->

## Checklist

- [ ] No stub/placeholder code left behind — everything here actually works as described
- [ ] Updated `ROADMAP_HONEST.md` if this changes what's verified/untested/not-built
- [ ] Updated `CHANGELOG.md` under `[Unreleased]`
- [ ] No secrets, API keys, or credentials in the diff
