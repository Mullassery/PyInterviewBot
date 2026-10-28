# Full tenant isolation for interviews and workflows

**Date:** 2026-10-28
**Status:** Draft for approval
**Scope:** ai-service, recruiter client, docs, migrations

## Intent

Close the two residual risks from the live-workflow-routing audit:

1. A shared API key can select any active production workflow by UUID.
2. Session creation provenance is not written to `ai_audit_events`.

The selected direction is full logical tenant isolation, not a narrower
workflow allowlist. Each caller identity becomes a tenant. Tenant-owned data is
readable and mutable only by that tenant or an admin, and every new interview
session records who created it.

This is intentionally a breaking authorization change for non-loopback
deployments that still use the single `PYINTERVIEWBOT_API_KEY`.

## Goals

- Make `Principal.subject` the tenant identifier.
- Persist an owner on every interview session.
- Scope every session read, turn, resume, report, export, priority, review, and
  delete by tenant.
- Scope channel mappings and idempotency events by tenant, so two tenants can
  use the same `external_id` or `event_id` without collision.
- Allow a tenant to start sessions only from workflows it owns; admins may
  cross tenant boundaries.
- Make cross-tenant workflow CRUD, audit, deployment, and session-start reads
  return `404` instead of confirming existence with `403`.
- Keep the pinned workflow snapshot behavior unchanged.
- Write successful session starts and denied cross-tenant workflow starts to
  `ai_audit_events`, with tenant provenance and no transcript or claim text.
- Scope the editable compliance policy per tenant.
- Require principal-bound keys for non-loopback session routes. The legacy
  shared key no longer authorizes session or compliance routes.
- Preserve no-key loopback development as a single `local-loopback` tenant.
- Migrate existing data without deleting it, using an explicit legacy owner.

## Non-goals

- Physical isolation (one database file or schema per tenant).
- SSO, user accounts, invitations, tenant self-service, or key rotation.
- Per-tenant encryption at rest.
- Auditing every read. Start/denial/action audit coverage is expanded, not
  exhaustive.
- Multi-worker/distributed rate limiting.
- Changing the gateway's browser authentication. The gateway still uses one
  ai-service tenant key for its deployment.
- Changing global static templates or provider selection.

## Anti-goals

- A workflow allowlist that still lets one key read all sessions.
- Tenant checks that rely on caller-supplied owner fields.
- Returning `403` for cross-tenant session or workflow IDs, which confirms the
  resource exists.
- Writing claim, transcript, evidence, or candidate identifiers to audit
  details.
- Silently assigning legacy data to the first tenant that asks for it.
- Keeping a hidden compatibility path that lets the legacy key start sessions.

## Approaches considered

### 1. Row-level tenant ownership (chosen)

Add `owner_subject` to sessions, channel rows, compliance policy, and audit
events. Enforce ownership in the API and query layer. Reuse the existing
principal map.

- Pros: fits the existing single SQLite database and principals model; one
  migration path; channel idempotency can be scoped cleanly; admin bypass is
  straightforward.
- Cons: a missing check is a cross-tenant bug; requires broad API test
  coverage.

### 2. Physical database per tenant

Route each tenant to its own SQLite file.

- Pros: strongest failure isolation; a query without a tenant predicate cannot
  cross tenants.
- Cons: large operational change; channel webhooks must resolve a tenant before
  touching data; migrations, backups, and workflow promotion all become
  per-tenant; no current configuration source maps requests to a database.

### 3. Capability tokens only

Keep ownerless rows and require workflow-scoped grants for session creation.

- Pros: smallest change for workflow selection.
- Cons: does not protect existing sessions, session lists, reports, or channel
  collisions. It reduces one risk but does not deliver tenant isolation.

Row-level ownership is the only approach that meets the selected goal without
replacing the storage and deployment model.

## Tenant model

### Identity

`Principal.source` identifies how authentication happened:

- `principal`: a key from `PYINTERVIEWBOT_PRINCIPALS`.
- `legacy`: the single `PYINTERVIEWBOT_API_KEY`.
- `loopback`: no auth configured, request from loopback.

`require_tenant_auth` wraps `require_auth` and rejects `legacy` for tenant
routes with `403`. `loopback` and `principal` are allowed. Admin is a role on a
principal, never a replacement for a tenant identity.

Non-loopback session and compliance access therefore requires
`PYINTERVIEWBOT_PRINCIPALS`. The legacy key can still authenticate non-tenant
routes such as static template/provider listing and workflow operations for the
`local-operator` subject, but it cannot create, read, or mutate tenant data.

### Ownership

Every new session stores `owner_subject` from the authenticated principal.
Workflows keep their existing `owner_subject`. A tenant may use a workflow only
when the workflow owner equals the principal subject or the principal has the
`admin` role.

Cross-tenant workflow selection returns `404 unknown workflow`, hiding
existence. Cross-tenant session access also returns `404`.

### Session tokens

A session token remains an additional per-session secret, not a tenant
substitute. A caller must be the session owner or an admin even when it presents
a valid session token. `PYINTERVIEWBOT_REQUIRE_SESSION_TOKEN=1` continues to
require the token for turns/resume.

This keeps the gateway flow working: the gateway's ai-service key is the tenant
key that created the session, and it forwards the session token on turns.

### Admin

`admin` may read, mutate, list, and delete across tenants. Admin actions remain
audited with the actor subject and the target session's tenant subject.

## API behavior

### Tenant routes

The following require `require_tenant_auth` and enforce ownership:

- `POST /sessions`
- `POST /sessions/{id}/turn`
- `POST /channels/{channel}/sessions`
- `POST /channels/{channel}/messages`
- `GET /sessions`
- `DELETE /sessions/{id}`
- `GET /sessions/{id}/summary`
- `GET /sessions/{id}/report`
- `GET /sessions/{id}/report/export`
- `GET|PUT /sessions/{id}/resume-policy`
- `POST /sessions/{id}/resume`
- `GET|PUT /sessions/{id}/priorities`
- `POST /sessions/{id}/review`
- `GET|PUT /compliance/policy...` and `POST /compliance/policy/reset`

`GET /sessions` returns only the principal's sessions unless admin, in which
case it returns all. No owner field is exposed in the list response.

Every session route loads the row and checks ownership before using the session
token or doing work. Missing rows and cross-tenant rows both return `404`.

### Channel behavior

`channel_sessions` becomes unique on
`(owner_subject, channel, external_id)`. `channel_events` becomes unique on
`(owner_subject, channel, event_id)`.

The channel APIs pass the tenant into every mapping/event lookup and bind. A
resume only finds a session for the current tenant. A session row whose owner
does not match the mapping is treated as not found. The same external ID or
event ID can therefore exist independently under different tenants.

### Workflow selection

`WorkflowRoute` gains `owner_subject`. `resolve_workflow_route` accepts the
requesting tenant and an `allow_cross_tenant` flag derived from `admin`.

Ownership is checked immediately after the workflow row is loaded, before
status, deployment, binding, or version checks. This guarantees that a foreign
tenant sees `404` even when the workflow is disabled or broken, and prevents
the route resolver from leaking workflow state.

The routing error carries a `workflow_not_owned` reason. `agent.start_session`
turns that reason into a denied `session.start` audit event and still returns a
`404` to the caller. Only an owned workflow proceeds through the existing
status, deployment, binding, version, and hash checks before the session is
created with the tenant owner and pinned route metadata.

The legacy template path without `workflow_id` remains available to tenant
callers and creates a tenant-owned session.

`workflow_api._require_owner` changes from `403` to `404` to match the same
non-disclosure rule across workflow CRUD, audit, deployment, and rollback
routes.

### Compliance policy

`compliance_policy` becomes tenant-scoped: primary key
`(tenant_subject, category)`. A tenant's default categories are seeded lazily on
first read. Updates and reset touch only the current tenant.

Question filtering during session start and turns loads the policy for the
session owner. Existing sessions keep using their owner's policy after creation.

## Audit

### Schema

`ai_audit_events` gains `tenant_subject`. Existing rows are backfilled from
`actor_subject`. `record_ai_audit` accepts an optional tenant, defaulting to the
actor.

### Session starts

`db.create_session` inserts the session row and its `session.start` audit row in
one transaction. The event contains:

- `action`: `session.start`
- `actor_subject` and `tenant_subject`: the session owner
- `target_type`: `session`
- `target_id`: the new session UUID
- `outcome`: `success`
- `details`: source (`api` or `channel:<name>`), requested template ID, pinned
  workflow ID, workflow version, deployment ID, and content hash when present

No claim, transcript, evidence, channel external ID, or candidate identifier is
recorded.

Denied cross-tenant routed starts write `session.start` with `outcome=denied`
and `details.reason=workflow_not_owned`, targeting the workflow ID. A failed
denial audit does not change the client response and does not leak existence.

Existing delete/review/export audit calls pass the target session's owner as
`tenant_subject`, including when an admin performs them.

## Migration

A new idempotent migration runs from `init_db` after the existing session column
additions and before per-tenant compliance seeding.

### Legacy owner

Existing sessions have no recorded creator. The migration assigns them in this
order:

1. If the session has a workflow, use that workflow's `owner_subject`.
2. Otherwise use `PYINTERVIEWBOT_LEGACY_SESSION_OWNER` when set.
3. Otherwise use the previous effective identity:
   - `PYINTERVIEWBOT_API_KEY_SUBJECT` when set;
   - else `local-operator` when `PYINTERVIEWBOT_API_KEY` is configured;
   - else `local-loopback`.

This is a one-time data assignment, not an access grant. The legacy key still
cannot access sessions after migration. An operator can adopt legacy rows by
setting `PYINTERVIEWBOT_LEGACY_SESSION_OWNER` before first startup, or by
creating a principal key with the assigned subject. Admins can always access
them.

### Tables

- `sessions`: add nullable `owner_subject`, backfill, add an owner/created
  index, and add the column to fresh schema as `NOT NULL`.
- `channel_sessions`: rebuild with tenant in the primary key; copy owner from
  the referenced session or the legacy owner.
- `channel_events`: rebuild with tenant in the primary key; copy owner from the
  referenced session or the legacy owner.
- `compliance_policy`: rebuild with tenant in the primary key. Copy the existing
  global rows to the legacy owner and to every distinct owner of an existing
  session, so workflow-owned sessions keep the operator's custom patterns.
  Other tenants are seeded on first access.
- `ai_audit_events`: add `tenant_subject` and backfill from `actor_subject`.

If a session's `workflow_id` no longer resolves to a workflow, ownership falls
through to `PYINTERVIEWBOT_LEGACY_SESSION_OWNER` and then the identity
fallback. Migration does not fail startup for an orphaned routing pin.

The migration is wrapped in a transaction and records its name in
`schema_migrations`. Fresh databases use the new shapes directly.

## Recruiter client

`candidate-client`'s recruiter view currently calls ai-service without any
credential, so it only works in the no-key loopback mode. Add a tenant-key path:

- Read the key from `sessionStorage` (per tab, cleared on tab close).
- Add `X-API-Key` to recruiter requests.
- On `401` or `403`, clear the stored key, prompt for a new one, and retry once.
- Never embed a key in `VITE_*` variables or source.
- Show a clear message when no key is supplied.

The candidate client continues to use the gateway and never receives an
ai-service key.

## Errors and observability

- Invalid/missing key: `401`.
- Legacy key on a tenant route: `403` with a message to configure
  `PYINTERVIEWBOT_PRINCIPALS`.
- Cross-tenant session or workflow: `404`.
- Admin may cross tenants.
- Startup logs a warning when a legacy key is configured and no principal keys
  are available, because non-loopback session routes will reject that key.
- Audit details use IDs and hashes only, never interview content or candidate
  identifiers.

## Rollout

1. Back up the database.
2. Optionally set `PYINTERVIEWBOT_LEGACY_SESSION_OWNER` before first startup to
   choose who adopts pre-migration sessions.
3. Configure `PYINTERVIEWBOT_PRINCIPALS` with one key per tenant and assign the
   gateway/recruiter deployments their tenant key. The gateway key's subject
   must match the owner of the workflows that gateway will run; there is no
   workflow ownership-transfer endpoint. One gateway process represents one
   tenant.
4. Stop using `PYINTERVIEWBOT_API_KEY` for session or compliance access.
5. Verify old sessions under an admin key or the assigned legacy owner.

## Testing

- Migration from a legacy schema preserves sessions and channel rows, assigns
  owners deterministically, and is idempotent.
- Legacy key gets `403` on every tenant route; principal and loopback keys work.
- Owner can access each session endpoint; another tenant gets `404`; admin can
  access.
- Session list is filtered for tenants and complete for admin.
- Session token plus wrong tenant still returns `404`.
- Cross-tenant workflow selection returns `404` and writes a denied audit.
- Routed sessions persist owner and pinned metadata; pin stability tests still
  pass.
- Channel resume/message with the same external ID/event ID in two tenants
  creates and resolves separate sessions.
- Compliance policy updates and reset affect only the calling tenant.
- `session.start` is atomic with session creation; simulated audit failure does
  not leave an unaudited session.
- Audit details contain no claim/transcript/candidate values.
- Recruiter client build passes and sends the stored key.

## Residual risks after this work

- Tenants sharing one key are one tenant by definition. Key distribution and
  rotation remain operational controls.
- Admins can read everything; admin key compromise is a full breach.
- SQLite and backups are not encrypted per tenant.
- Audit retention, tamper resistance, and an operator UI remain unbuilt.
- Loopback development without auth is intentionally one local tenant.
- Gateway hosts one tenant identity per process; multi-tenant gateway hosting
  needs separate processes or a future tenant-aware gateway.
