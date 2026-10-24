# Audit findings — 2026-10-19

This file records the repository-specific findings from the portfolio audit
conducted on 2026-10-19. It is documentation only; no code or release artifacts
were changed in this pass unless explicitly noted under "Implementation status".

Sources: the repository's own README/source/workflows, GitHub metadata and
Actions runs, PyPI metadata, and official competitor documentation. Claims are
marked as verified, inferred, or open questions. "Absent" means not found in the
inspected repository/docs, not proof that it cannot exist.

## What it is

Voice-first AI interview MVP: Rust WebSocket gateway, local ASR/LLM/TTS, evidence extraction, and a recruiter view.

## Verified strengths

- Rust gateway with VAD/barge-in/state machine; faster-whisper + Ollama + Piper; evidence extraction; recruiter view; one real E2E script.
- Local-only models avoid API keys and data egress.

## Verified gaps and user-facing risks

- One hardcoded scenario; no auth/TLS (README; code currently binds loopback); no CI E2E tests.
- No consent/retention/bias/EEOC controls; no multi-scenario configuration; no scale/latency evidence.
- High-compliance category; an MVP without controls is not deployable.

## Direct competitors

- HireVue, Paradox, CodeSignal, Brighthire, Metaview.

## Ranked next actions

- P3 only: auth/TLS, scenario config, consent/retention, bias evaluation, E2E CI.
- Otherwise archive the repo.

## Do not build

- Production hiring features before compliance review.

## Evidence gaps / validation needed

- No legal/privacy review; no candidate/recruiter feedback.

## Recommended disposition

Freeze unless a compliant design partner exists.

## Implementation status

No code changes in this pass.
