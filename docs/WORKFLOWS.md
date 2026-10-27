# Workflow artifacts, testing, and promotion

Last updated: 2026-10-26.

Workflows are versioned interview configurations owned by a principal. A
published version is an immutable artifact identified by its canonical SHA-256
content hash. The same hash must pass the sandbox suites and receive an
approval before it can be promoted to production.

This implements the artifact lifecycle and production deployment registry. It
does not yet route live candidate sessions to the active workflow deployment;
that runtime integration remains pending.

## Invariant

```
approved hash == tested hash == promoted hash
```

The artifact contains the workflow definition and a pinned snapshot of its
interview template. Provider bindings, credentials, test evidence, approval
state, and deployment state are deliberately outside the artifact. A new
production binding therefore does not require republishing the workflow, but a
deployment made under a different binding fingerprint cannot be rolled back to.

## Artifact identity

- Schema version: `1`.
- Strict Pydantic models reject unknown fields.
- The template is pinned by ID, version, title, role, competency, nodes,
  question bank, turn limit, system prompt, and evaluation notes.
- Runtime requirements list required capabilities (`llm`, `asr`, `tts`).
- Security policy defaults to no external side effects, no external network,
  transcript redaction, and mandatory human review.
- Canonical JSON uses sorted keys, compact separators, UTF-8, and
  `content_hash = null` during hashing. The hash field itself is excluded.
- Hash verification uses `hmac.compare_digest`.

Published versions cannot be edited. Updating a workflow draft only changes
future versions.

## Principals and roles

`PYINTERVIEWBOT_PRINCIPALS` is a JSON object mapping API keys to subjects and
roles:

```json
{
  "alice-key": {"subject": "alice", "roles": ["owner", "author", "deployer"]},
  "bob-key": {"subject": "bob", "roles": ["approver"]},
  "admin-key": {"subject": "admin", "roles": ["admin"]}
}
```

- `owner`: can read and operate on their own workflows.
- `author`: can create/update drafts, publish versions, deploy to sandbox, run
  tests, and request approval.
- `approver`: can decide an approval assigned to their subject.
- `deployer`: can promote and roll back production deployments.
- `admin`: can read all workflows and bypass ownership checks.

The legacy `PYINTERVIEWBOT_API_KEY` maps to `local-operator` with all
non-admin roles. Loopback requests map to `local-loopback` with `owner` and
`author`. When `PYINTERVIEWBOT_PRINCIPALS` is configured, unknown API keys are
rejected.

The requester cannot approve their own deployment, and the assigned approver
must match the deciding principal.

## Environment bindings

Sandbox is fixed to mock providers:

```text
llm=mock, asr=mock, tts=mock, revision=sandbox-v1
```

Production requires an explicit binding and fingerprint:

| Variable | Purpose |
|---|---|
| `PYINTERVIEWBOT_PRODUCTION_BINDING_REVISION` | Required non-empty production binding revision |
| `PYINTERVIEWBOT_LLM_PROVIDER` | Production LLM provider name; default `local` |
| `PYINTERVIEWBOT_ASR_PROVIDER` | Production ASR provider name; default `local` |
| `PYINTERVIEWBOT_TTS_PROVIDER` | Production TTS provider name; default `local` |

The fingerprint is SHA-256 over the environment, revision, and resolved provider
map. Only the fingerprint is stored in deployment rows. Credentials are never
stored in artifacts or deployment records.

## Lifecycle

```text
draft
  -> publish immutable version + content hash
  -> deploy version to fixed mock sandbox binding
  -> run mandatory sandbox suites
  -> request approval tied to test_run_id + content_hash
  -> assigned approver approves or rejects
  -> production dry-run reports blockers
  -> promote exact tested + approved hash
  -> active production deployment
  -> rollback to a previous production deployment with the same binding
```

Mandatory suites:

1. `schema_hash`: recompute and compare the canonical content hash.
2. `template_pinning`: verify the pinned template is complete and current.
3. `security_policy`: forbid external side effects/network and require
   redaction, human review, and at least one runtime capability.
4. `sandbox_isolation`: verify the sandbox binding is mock-only and satisfies
   the runtime capabilities.
5. `mock_interview`: run a deterministic interview through opening, evidence
   collection, and completion.

Approval requires a passing test run for the same workflow version and content
hash. Production promotion requires that approval to reference the same test
run and hash.

## Deployment and failure semantics

- Sandbox deployments are idempotent by `Idempotency-Key` within a workflow.
- A successful production promotion supersedes the previous active deployment.
- A failed promotion writes a `failed` deployment row with a sanitized reason
  and a `deployment.production.failed` audit event.
- The previous active production deployment remains active when promotion
  fails.
- Rollback targets a prior production deployment, marks the current deployment
  `superseded`, and records `deployment.production.rollback`.
- Rollback fails closed when the current production binding is unavailable or
  its fingerprint differs from the target deployment.
- A failed deployment cannot be used as a rollback target.
- Disabled workflows cannot be deployed to sandbox, have new approval requests
  created, or be promoted; already-deployed sandbox tests and pending approval
  decisions can still complete. Enable the workflow before resuming new work.

SQLite writes use `BEGIN IMMEDIATE` plus unique constraints for version
allocation, approvals, deployments, and idempotency keys.

## API

All routes require authentication. Workflow reads and mutations are owner-only
unless the caller has `admin`.

| Method | Route | Role / requirement |
|---|---|---|
| `POST` | `/workflows` | `author` |
| `GET` | `/workflows` | Any principal; owner-scoped, admin sees all |
| `GET` | `/workflows/{id}` | Owner or admin |
| `PATCH` | `/workflows/{id}` | `author`, owner |
| `POST` | `/workflows/{id}/versions` | `author`, owner; optional `Idempotency-Key` |
| `GET` | `/workflows/{id}/versions` | Owner or admin |
| `GET` | `/workflows/{id}/versions/{version}` | Owner or admin |
| `POST` | `/workflows/{id}/versions/{version}/sandbox/deploy` | `author`, owner; optional `Idempotency-Key` |
| `POST` | `/workflows/{id}/versions/{version}/tests` | `author`, owner; optional `Idempotency-Key` |
| `GET` | `/workflows/{id}/versions/{version}/tests` | Owner or admin |
| `POST` | `/workflows/{id}/versions/{version}/approvals` | `author`, owner; optional `Idempotency-Key` |
| `GET` | `/workflows/{id}/versions/{version}/approvals` | Owner or admin |
| `POST` | `/workflows/{id}/versions/{version}/approvals/{approval_id}/decision` | `approver`; assigned subject only |
| `POST` | `/workflows/{id}/versions/{version}/production/dry-run` | Owner or admin |
| `POST` | `/workflows/{id}/versions/{version}/production/deploy` | `deployer`, owner; optional `Idempotency-Key` |
| `POST` | `/workflows/{id}/rollback` | `deployer`, owner; optional `Idempotency-Key` |
| `GET` | `/workflows/{id}/deployments` | Owner or admin |
| `GET` | `/workflows/{id}/audit` | Owner or admin |
| `POST` | `/workflows/{id}/disable` | Owner or admin |
| `POST` | `/workflows/{id}/enable` | Owner or admin |

`POST .../tests` takes `{"deployment_id": "..."}`.
`POST .../approvals` takes `{"test_run_id": "...", "approver_subject": "...",
"note": ""}`. A decision takes `{"decision": "approve"|"reject", "note": ""}`.
Production deploy and dry-run take `{"test_run_id": "...", "approval_id": "..."}`.
Rollback takes `{"deployment_id": "..."}`.

## Storage

Workflow data lives in the ai-service SQLite database:

| Table | Contents |
|---|---|
| `workflows` | Owner, current draft, status, latest version |
| `workflow_versions` | Immutable artifact JSON and content hash |
| `workflow_test_runs` | Sandbox evidence, hash, deployment, pass/fail |
| `workflow_approvals` | Assigned approver, hash, test run, decision |
| `workflow_deployments` | Environment, hash, binding fingerprint, status, predecessor, failure |
| `workflow_audit` | Append-only lifecycle/audit events |

This database is distinct from the Rust-owned call-state SQLite database. Python
must not write call state; the gateway remains its sole writer.

## Security notes

- Treat the workflow API as privileged. Gateway authentication is not a
  substitute for ai-service principals and ownership checks.
- Do not put credentials, connection strings, or provider secrets in drafts or
  artifacts; they are hashed and stored verbatim.
- Production dry-run is an eligibility check, not a guarantee; promotion
  re-validates the same evidence atomically.
- Workflow audit currently covers lifecycle actions, not every read.
- Workflow API audit events are append-only at the application layer; SQLite
  file permissions and backups are operator responsibilities.
- No live candidate runtime consumes the active deployment yet. Do not present a
  production deployment row as proof that a workflow is serving interviews.

## Tests

- `tests/test_workflow_models.py`: canonical hash and strict schema.
- `tests/test_workflow_store.py`: immutability, idempotency, unique versions,
  ownership.
- `tests/test_workflow_env.py`: fixed sandbox binding and production fingerprint.
- `tests/test_workflow_api.py`: end-to-end vertical slice and expected-hash
  rejection.
- `tests/test_workflow_safety.py`: ownership, exact-hash promotion, approval
  URL binding, promotion failure preservation, rollback binding checks, and
  disable behavior.
- `tests/test_workflow_store.py`: concurrent version allocation and
  idempotency.
- `tests/test_auth.py`: principals map and role parsing.
