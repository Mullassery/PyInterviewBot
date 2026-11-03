# Ecosystem migration record

Baseline branch `ecosystem-integration` (starting SHA `24e67ca`).
Sister repository: Voxora Studio (branch `ecosystem-integration`, baseline
`ebd1d04`). The canonical ownership/contract ADRs live in Voxora's
`docs/migration/migration-adr.md`; this file records the PyInterviewBot-side
extraction ledger.

## Ownership decision (applies to every module below)

PyInterviewBot owns **only** interview methodology: templates, workflow
lifecycle, competencies/evidence, question generation, adaptive depth,
rubrics/scoring, reports, transcript interpretation. Voice sessions, the
voice runtime, telephony (Twilio / Vapi-compatible), OpenRouter connectivity,
model routing, usage billing, and call scheduling belong to Voxora Studio.

## Phase 4 extraction ledger

| Module (removed) | Replacement location | Notes / rollback |
|---|---|---|
| `ai-service/src/pyinterviewbot_ai/providers/openrouter.py` | Voxora `services/ai-runtime` `OpenRouterAgent`/`OpenRouterLLM` + orchestrator `POST /api/v1/ai/decide` | Archived in git history on this branch. Rollback: restore the file and set `PYINTERVIEWBOT_LLM_PROVIDER=openrouter` only before commit `voxora-redirect-1`. |
| `tests/test_openrouter.py` | `tests/test_voxora_client.py` (consumer side) + Voxora `services/orchestrator/tests/test_ai_decide.py` (producer side) | The two suites pin the same shapes; contract drift fails in either bucket. |
| registry `openrouter` entries (asr/tts/llm) | `providers/voxora.py` LLM-only routing; ASR/TTS remain `local`/`mock` (Voxora owns speech inside voice sessions) | Unknown values fail closed with a migration pointer — no silent behavioral change |

### Redirect sequence executed (per ADR-0004)

1. Identified callers (`providers/registry.py`, `tests/test_openrouter.py`,
   `conftest.openrouter_env`, `workflow_env` fingerprints).
2. Classified as infrastructure (provider connectivity/routing/billing) —
   not interview domain; decision normalization moved with the new provider.
3. Built the replacement first: Voxora `/api/v1/ai/decide` (committed on the
   Voxora branch, `f687123`) with its own contract tests, and
   `providers/voxora.py` + `voxora_client.py` with the producer-consumer
   contract tests (`tests/test_voxora_client.py`).
4. Redirected the registry (`voxora` LLM provider), updated env-consumers
   (`conftest.voxora_env`, workflow fingerprint test).
5. Removed the obsolete module only after the full suite stayed green
   (`138 passed` post-removal vs `149` including the 11 OpenRouter tests).
6. Regression check: `uv run python -m pytest -q` (ai-service) + Voxora
   suite on its branch.

## Operating modes (standalone + integration, per the ADR)

- **local integration** — `VOXORA_BASE_URL=http://127.0.0.1:<voxora-port>` +
  `VOXORA_API_KEY` + `PYINTERVIEWBOT_LLM_PROVIDER=voxora`
- **remote integration** — same contracts against a deployed Voxora
- **simulation** — `PYINTERVIEWBOT_*_PROVIDER=mock` (or `local` for the
  offline Ollama path); zero Voxora contact. The simulation mode must never
  re-implement a voice engine — it drives the interview engine directly.

## Deliberately NOT removed yet (replacement must land + verify first)

- `gateway/src/twilio.rs`, `voice-runtime/`, `interview-domain/call.rs`,
  `gateway/src/calls.rs`, `scheduler.rs`, `reminders.rs`,
  `native_voice.rs`, `managed_call.rs` (Vapi path) — these migrate when the
  gateway consumes the Voxora voice-session contracts end-to-end (browser
  realtime loop is interview-adjacent UX infrastructure and is being
  evaluated as the Media Pipeline handoff, not deleted blindly).
- `providers/local.py` (Ollama/faster-whisper/Piper) and `providers/mock.py`
  — required for the standalone/simulation modes; they are interview-domain
  decision support, not duplicate voice infrastructure.

## Contract inventory consumed by this repo

| Consumer | Contract | Tests |
|---|---|---|
| `providers/voxora.py` | `POST /api/v1/ai/decide` | `tests/test_voxora_client.py::test_decide_contract_*` |
| `voxora_client.py` voice methods | `/api/v1/voice-sessions` (create/get/terminate/transcript/instructions) | `test_voice_session_payload_matches_adr_shape` |
| Webhooks out of Voxora | `voice_session.*` envelope v1 (HMAC `x-voxora-signature`) | verified on the Voxora side; inbound verification planned with the gateway redirect |
