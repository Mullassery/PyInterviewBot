# Live workflow routing and pinned interview snapshots

Date: 2026-10-27
Status: Proposed

## Intent

Production workflow deployments are currently an audited registry. Promoting a
workflow records state, but no candidate interview consumes it. This change makes
a promoted workflow selectable by new sessions and pins the approved template
snapshot for the lifetime of each interview.

The caller opts in. A session request without a workflow keeps the existing
template behavior. A workflow-routed session must fail closed when the workflow,
deployment, binding, or artifact no longer matches the approved production state.

## Goals

- Accept an optional `workflow_id` when starting a direct or channel session.
- Resolve only an active production deployment of an active workflow.
- Require the current production binding fingerprint to match the deployment.
- Require the deployment's content hash to match the stored immutable version.
- Persist workflow provenance and the pinned template snapshot on the session.
- Use the pinned snapshot for every turn, including after promotion, rollback,
  disablement, or built-in template changes.
- Keep the legacy `template_id` path unchanged when no workflow is supplied.
- Let the gateway forward a configured workflow ID.
- Rerun the security audit after implementation and update its report.

## Non-goals

- Automatic workflow selection when `workflow_id` is omitted.
- Per-tenant workflow ownership checks on session creation.
- Routing an existing channel session to a different workflow on resume.
- Changing workflow authoring, testing, approval, or promotion semantics.
- Live external provider qualification.
- Audit retention, audit operator UI, or broader read auditing.
- Multi-node SQLite or distributed rate limiting.

## Anti-goals

- An in-flight interview changing because a newer deployment was promoted.
- Silently falling back to a built-in template when a supplied workflow fails.
- Serving a disabled workflow or disabled deployment.
- Serving an artifact under a production binding it was not promoted against.
- Treating the production deployment row alone as proof the artifact is intact.

## Constraints

- Python 3.11, FastAPI, Pydantic, and aiosqlite in `ai-service`.
- The Rust gateway is the live caller and currently forwards only `template_id`.
- Session creation and turns use the shared API key; workflow routes use the
  separate principals map. Session creation has no per-tenant authorization.
- SQLite remains the single system of record and runs single-node.
- Database changes must be additive and migration-safe for existing installs.
- No dynamic SQL identifiers.
- Existing tests and clients must continue working without a workflow ID.

## Approaches considered

### 1. Resolve the active deployment on every turn

Rejected. A promotion or rollback between turns would change the interview
mid-flight. It also makes turn latency depend on deployment state and gives no
stable audit identity for the session.

### 2. Resolve at start but persist only `template_id`

Rejected. The built-in template can change after an artifact was approved. The
interview would run a different template than the one that passed sandbox tests.

### 3. Resolve at start and persist provenance plus the pinned snapshot

Chosen. The session records which workflow, version, deployment, and content hash
it started with, and the interview engine reads the immutable template snapshot.
Promotion and rollback affect only future sessions.

### Selection policy

Rejected: auto-select when exactly one deployment is active. It makes an omitted
field change behavior based on global state. Chosen: optional `workflow_id`;
omitted means the legacy template path.

## Resolution contract

For a supplied `workflow_id`:

1. Load the workflow. Unknown workflow: HTTP 404.
2. Require `workflows.status == "active"`. Disabled workflow: HTTP 409.
3. Load the newest `production` deployment with `status == "active"`.
   None: HTTP 409.
4. Resolve the current production binding. Missing revision, invalid provider, or
   unavailable binding: HTTP 409.
5. Require deployment and current binding fingerprints to match. Mismatch: HTTP
   409. This prevents routing after provider configuration changed until the
   workflow is promoted again under the new binding.
6. Load the immutable version for `deployment.version`. Missing version or
   content-hash mismatch with the deployment: HTTP 409.
7. Use the store's existing artifact hash verification. Invalid stored artifact:
   HTTP 409.
8. Convert the artifact's `TemplateSnapshot` into the interview engine's
   `InterviewTemplate`.

The active-deployment query is ordered by `created_at DESC` and benefits from the
existing unique partial index `uq_workflow_deployments_active_env`.

Error details are non-secret operationally useful messages, for example
`workflow has no active production deployment` or
`production binding does not match the active deployment`.

## Session persistence

Add nullable columns to `sessions`:

| Column | Purpose |
|---|---|
| `workflow_id` | Workflow selected at session creation |
| `workflow_version` | Immutable artifact version |
| `workflow_content_hash` | Promoted artifact hash |
| `workflow_deployment_id` | Production deployment provenance |
| `template_snapshot_json` | Pinned template used by every turn |

New databases get the columns in `SCHEMA`. Existing databases get them through
entries added to `SESSION_COLUMN_MIGRATIONS`, using fixed SQL statements. They
remain `NULL` for legacy sessions.

`db.create_session` gains optional keyword arguments for the pin. Existing calls
continue to compile and behave identically.

## Runtime behavior

`agent.start_session(template_id, workflow_id=None)`:

- With a workflow: resolve the route first, then create the session using the
  route template, snapshot JSON, and provenance. Resolve before insert so an
  invalid workflow cannot create an orphan session.
- Without a workflow: resolve the built-in template exactly as today and leave
  workflow columns `NULL`.
- Return a `SessionStartResult` carrying session ID, opening question, template
  ID, token, and optional workflow provenance.

`agent.take_turn` selects the session template in this order:

1. If `template_snapshot_json` is present, validate and convert the snapshot.
2. Otherwise load the current built-in template by `template_id`.

A corrupt pinned snapshot is an internal error, not a 404. Existing sessions with
no snapshot keep their current behavior.

Channel sessions use the same agent path. A new channel session may pass
`workflow_id`; resuming an existing channel session keeps its original pin and
ignores a newly supplied workflow ID.

## API and gateway

`StartSessionRequest` and `ChannelSessionRequest` gain optional
`workflow_id: UUID`.

`StartSessionResponse` and `ChannelSessionResponse` gain optional
`workflow_id`, `workflow_version`, and `workflow_deployment_id`. They are omitted
or null for legacy sessions.

The Rust `AiClient` gains an optional workflow ID. The gateway reads
`PYINTERVIEWBOT_WORKFLOW_ID` and forwards it for direct and channel session
creation. Unset means legacy behavior. Direct API callers may set the JSON field
without changing gateway configuration.

The raw production binding, provider credentials, and binding fingerprint are
never returned in session responses.

## Security boundaries

- A session caller with the shared API key may select any active production
  workflow by UUID. This matches the current global-key trust model, which
  already permits reading any session. The workflow must still be active and
  promoted under the current binding. Tenant isolation remains out of scope.
- Routing fails closed on every integrity check. It does not fall back to the
  built-in template or a stale deployment.
- In-flight sessions retain their pinned template even if the workflow is
  disabled or the deployment is superseded. Disabling stops new work, not a
  candidate mid-interview.
- The session row stores no credentials or binding fingerprint. It stores public
  workflow identity, the content hash, deployment ID, and the template snapshot.
- Session creation is not added to `ai_audit_events` in this change. Provenance
  is durable on the session row; broader session audit remains a separate gap.

## Compatibility and rollout

- Additive schema migration; no rewrite of existing rows.
- Existing clients omitting `workflow_id` see no behavior change.
- Gateway routing is opt-in through `PYINTERVIEWBOT_WORKFLOW_ID`.
- After a provider binding change, new routed sessions fail until the workflow is
  promoted again under the new binding. Existing pinned sessions continue.
- Docs and security claims that call production deployments registry-only are
  updated.

## Observability

Log a routed session start at INFO with workflow ID, version, content hash, and
deployment ID. Do not log templates, transcript content, provider credentials, or
the binding fingerprint.

## Testing

- Legacy direct and channel session starts without a workflow still work.
- Routed direct session uses the active deployment's template and persists all
  provenance columns and the snapshot.
- Routed channel session works; resume keeps its original workflow pin.
- Unknown workflow returns 404.
- Disabled workflow, no active deployment, missing binding, binding mismatch,
  missing version, and hash mismatch return 409.
- A promotion or rollback after session creation does not change the session's
  pinned template or its turn behavior.
- Corrupt snapshot handling fails without returning a misleading 404.
- Additive schema migration adds the columns to an older database.
- Gateway test asserts `workflow_id` is forwarded for direct and channel starts.
- Full Python suite, targeted Ruff, and the Rust workspace tests pass.

## Security audit rerun

After implementation:

1. Run Bandit over `ai-service/src` using an isolated `uvx` invocation because the
   Homebrew entry point is broken.
2. Run `pip-audit` against the ai-service environment.
3. Perform a manual review of the new routing path for authorization, SQL,
   secret exposure, fail-open behavior, and denial-of-service risks.
4. Update `docs/SECURITY_AUDIT.md` with the date, commands, findings, false
   positives, and residual risks. Record exact blockers if a tool is unavailable.
5. Recheck that no dynamic SQL or user-controlled identifiers remain.

## Left to the builder

- Whether to expose workflow metadata in session list responses.
- Exact logging field names and test fixture organization.
- Whether to refactor the existing tuple return from `agent.start_session` in a
  separate change instead of introducing `SessionStartResult`.
