# Full Tenant Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `Principal.subject` the tenant boundary for interview sessions, channels, workflows, compliance policy, and audit events, with strict rejection of the legacy shared key on tenant routes.

**Architecture:** Add row-level `owner_subject`/`tenant_subject` columns, tenant-aware DB APIs, a FastAPI `require_tenant_auth` dependency, and a shared authorized-session loader. Existing SQLite data is migrated in place with deterministic legacy ownership. Workflow resolution checks ownership before status/deployment checks.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite, pytest, TypeScript/Vite, Rust gateway (docs-only), SQLite migrations.

**Spec:** `docs/superpowers/specs/2026-10-28-full-tenant-isolation-design.md`

## Global Constraints

- Tenant identity is `Principal.subject`; `admin` may cross tenants.
- Cross-tenant session and workflow IDs return `404`, never `403` or `409`.
- The legacy `PYINTERVIEWBOT_API_KEY` returns `403` on every tenant route.
- No-key loopback remains a single `local-loopback` tenant.
- A valid session token is not a tenant substitute.
- Audit details never include claims, transcripts, evidence, channel external IDs, or candidate identifiers.
- No physical database or schema per tenant.
- No commits unless the user explicitly approves them.
- Python verification: `cd ai-service && uv run --group dev python -m pytest -q`.
- Client verification: `cd candidate-client && npm run build`.

## Review Focus

- Legacy key on `POST /sessions`, `GET /sessions`, turns, reports, resume, review, or compliance returns `403`, not `401` or `500`.
- A foreign tenant selecting an existing disabled/broken workflow gets `404`, not `409` that confirms the workflow exists.
- Two tenants using the same channel `external_id` and `event_id` get independent sessions and idempotency records.
- Existing routed sessions retain their owner and pinned template after migration; custom global compliance patterns still apply to their owners.
- A valid session token with the wrong tenant key still returns `404`.
- A failed audit insert rolls back session creation.

---

### Task 1: Principal source and tenant dependency

**Files:**
- Modify: `ai-service/src/pyinterviewbot_ai/auth.py`
- Test: `ai-service/tests/test_auth.py`

**Interfaces:**
- Produces: `Principal(subject: str, roles: frozenset[str] = DEFAULT_ROLES, source: str = "principal")`
- Produces: `require_tenant_auth(principal: Annotated[Principal, Depends(require_auth)]) -> Principal`
- Produces: `Principal.source` values `"principal"`, `"legacy"`, `"loopback"`

- [ ] **Step 1: Write failing auth tests**

Add a `/tenant` endpoint to the minimal app in `test_auth.py` using
`Depends(require_tenant_auth)`. Add tests asserting:

```python
def test_legacy_key_is_not_a_tenant(monkeypatch):
    monkeypatch.setenv("PYINTERVIEWBOT_API_KEY", "secret")
    monkeypatch.delenv("PYINTERVIEWBOT_PRINCIPALS", raising=False)
    response = TestClient(app).get("/tenant", headers={"X-API-Key": "secret"})
    assert response.status_code == 403
    assert "PYINTERVIEWBOT_PRINCIPALS" in response.json()["detail"]


def test_principal_key_is_a_tenant(monkeypatch):
    monkeypatch.setenv("PYINTERVIEWBOT_API_KEY", "secret")
    monkeypatch.setenv(
        "PYINTERVIEWBOT_PRINCIPALS",
        '{"alice-key": {"subject": "alice", "roles": ["author"]}}',
    )
    response = TestClient(app).get("/tenant", headers={"X-API-Key": "alice-key"})
    assert response.status_code == 200
    assert response.json()["subject"] == "alice"
```

Also assert `source` on the existing legacy, principal-map, and loopback identity
tests.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ai-service && uv run --group dev python -m pytest tests/test_auth.py -q`
Expected: FAIL because `require_tenant_auth` does not exist.

- [ ] **Step 3: Implement source tagging and tenant dependency**

In `auth.py`:

```python
@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str] = DEFAULT_ROLES
    source: str = "principal"


async def require_tenant_auth(
    principal: Annotated[Principal, Depends(require_auth)],
) -> Principal:
    if principal.source == "legacy":
        raise HTTPException(
            status_code=403,
            detail=(
                "tenant identity required; configure "
                "PYINTERVIEWBOT_PRINCIPALS and use a principal key"
            ),
        )
    return principal
```

Set `source="principal"` for configured principal keys, `source="legacy"` for
`PYINTERVIEWBOT_API_KEY`, and `source="loopback"` for loopback identity. Export
`require_tenant_auth` from `__all__`.

- [ ] **Step 4: Run auth tests**

Run: `cd ai-service && uv run --group dev python -m pytest tests/test_auth.py -q`
Expected: PASS.

---

### Task 2: Session ownership, creation authorization, and start audit

**Files:**
- Modify: `ai-service/src/pyinterviewbot_ai/db.py`
- Modify: `ai-service/src/pyinterviewbot_ai/agent.py`
- Modify: `ai-service/src/pyinterviewbot_ai/channels.py`
- Modify: `ai-service/src/pyinterviewbot_ai/workflow_routing.py`
- Modify: `ai-service/src/pyinterviewbot_ai/main.py`
- Modify: `ai-service/tests/conftest.py`
- Test: `ai-service/tests/test_db.py`
- Test: `ai-service/tests/test_workflow_routing.py`
- Test: `ai-service/tests/test_session_routing.py`
- Test: `ai-service/tests/test_audit.py`

**Interfaces:**
- Produces: `create_session(..., *, owner_subject: str, audit_source: str, ...) -> None`
- Produces: `record_ai_audit(..., tenant_subject: str | None = None) -> None`
- Produces: `list_sessions(*, owner_subject: str | None = None, include_all: bool = False)`
- Produces: `delete_session(session_id, *, owner_subject: str | None = None, include_all: bool = False) -> bool`
- Produces: `WorkflowRoutingError(..., reason: str | None = None)`
- Produces: `WorkflowRoute.owner_subject: str`
- Produces: `resolve_workflow_route(workflow_id, *, tenant_subject: str | None = None, allow_cross_tenant: bool = False)`
- Produces: `agent.start_session(template_id=None, workflow_id=None, *, owner_subject: str, allow_cross_tenant: bool = False, source: str = "api")`
- Produces: `channels.start_or_resume(..., *, owner_subject: str, allow_cross_tenant: bool = False)`

- [ ] **Step 1: Write failing persistence and routing tests**

In `test_db.py`:

```python
async def test_create_session_persists_owner_and_start_audit():
    session_id = uuid4()
    await db.init_db()
    await db.create_session(
        session_id,
        ResumeClaim(competency="x", text="x"),
        owner_subject="alice",
        audit_source="api",
        template_id="backend_engineer",
    )
    row = await db.get_session_row(session_id)
    assert row["owner_subject"] == "alice"
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT actor_subject, tenant_subject, action, outcome, details_json "
            "FROM ai_audit_events WHERE target_id = ?",
            (str(session_id),),
        )
        event = await cursor.fetchone()
    assert event["action"] == "session.start"
    assert event["actor_subject"] == "alice"
    assert event["tenant_subject"] == "alice"
    assert event["outcome"] == "success"
    assert "alice" not in event["details_json"]
```

Add a migration test with an old `sessions` table containing a `workflow_id`
and a workflow owned by `alice`; after `db.init_db()`, the session owner is
`alice`. Add a second only-session row and set
`PYINTERVIEWBOT_LEGACY_SESSION_OWNER=legacy-owner`; assert it receives
`legacy-owner`. Run `init_db()` twice and assert unchanged ownership.

Add an atomicity test: after `db.init_db()`, execute
`DROP TABLE ai_audit_events`, then assert `db.create_session(...)` raises and
`db.get_session_row(session_id)` is `None`.

In `test_workflow_routing.py`, update direct `agent.start_session` calls to pass
`owner_subject="alice"`. Add:

```python
async def test_foreign_tenant_workflow_is_404_before_status_checks(monkeypatch):
    workflow_id, _, _ = await _active_route(monkeypatch)
    await workflow_store.set_status(workflow_id, status="disabled", actor="alice")

    with pytest.raises(routing.WorkflowRoutingError) as exc:
        await agent.start_session("ignored", workflow_id, owner_subject="bob")

    assert exc.value.status_code == 404
    assert exc.value.reason == "workflow_not_owned"
```

Assert a `session.start` denied audit row targets the workflow ID and contains
no claim text.

In `test_session_routing.py`, replace session-route `X-API-Key: "test-key"` with
`workflow_auth["alice"]` for workflows owned by alice. Add a test that the
legacy `test-key` gets `403` on `POST /sessions`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ai-service && uv run --group dev python -m pytest tests/test_db.py tests/test_workflow_routing.py tests/test_session_routing.py -q`
Expected: FAIL on missing `owner_subject`, `tenant_subject`, `reason`, and legacy `403`.

- [ ] **Step 3: Add session owner and tenant audit schema**

In `db.py`:

- Add nullable `owner_subject TEXT` to old-session column migrations and
  `owner_subject TEXT NOT NULL` to fresh `sessions`.
- Add `idx_sessions_owner_created ON sessions(owner_subject, created_at DESC)`.
- Add `tenant_subject TEXT NOT NULL` to fresh `ai_audit_events`.
- Move `await _run_migrations(db)` after the session-column loop so migration
  `002_session_tenant` can read `workflow_id`.
- Add migration `002_session_tenant` to:
  - add `owner_subject` if an old table lacks it;
  - add `tenant_subject` to `ai_audit_events` if missing;
  - backfill routed sessions from `workflows.owner_subject`;
  - backfill remaining null owners with `_legacy_owner_subject()`;
  - backfill audit tenants from `actor_subject`;
  - create the owner index.

`_legacy_owner_subject()` uses `PYINTERVIEWBOT_LEGACY_SESSION_OWNER`, then
`PYINTERVIEWBOT_API_KEY_SUBJECT`, then `local-operator` when
`PYINTERVIEWBOT_API_KEY` is set, else `local-loopback`.

- [ ] **Step 4: Implement owner-aware DB writes and audit**

`create_session` inserts `owner_subject` and, in the same transaction, inserts:

```python
action="session.start",
actor_subject=owner_subject,
tenant_subject=owner_subject,
target_type="session",
target_id=str(session_id),
outcome="success",
details={
    "source": audit_source,
    "template_id": template_id,
    "workflow_id": workflow_id,
    "workflow_version": workflow_version,
    "workflow_deployment_id": workflow_deployment_id,
    "workflow_content_hash": workflow_content_hash,
},
```

Omit null workflow fields from `details`. Do not include the claim.

Update `record_ai_audit` to store `tenant_subject or actor_subject`. Update
`list_sessions` and `delete_session` to filter by owner unless `include_all`.

- [ ] **Step 5: Implement workflow ownership and session creation**

In `workflow_routing.py`, add `owner_subject` to `WorkflowRoute` and accept
`tenant_subject`/`allow_cross_tenant`. Immediately after loading the workflow:

```python
if (
    tenant_subject is not None
    and workflow.owner_subject != tenant_subject
    and not allow_cross_tenant
):
    raise WorkflowRoutingError(
        f"unknown workflow {workflow_id}",
        status_code=404,
        reason="workflow_not_owned",
    )
```

In `agent.start_session`, pass tenant context to the resolver, audit a denied
`workflow_not_owned` start with `db.record_ai_audit` (catch/log audit failure),
and pass `owner_subject`/`audit_source` into `db.create_session`.

In `channels.start_or_resume`, accept the tenant parameters and pass them to
`agent.start_session`. Pass `source=f"channel:{channel}"`.

In `main.py`, switch `POST /sessions` and `POST /channels/{channel}/sessions`
to `require_tenant_auth`, get the `principal`, and pass
`owner_subject=principal.subject` and
`allow_cross_tenant=principal.has_role("admin")`.

- [ ] **Step 6: Update test fixture identity**

In `tests/conftest.py`, configure `PYINTERVIEWBOT_PRINCIPALS` for `test-key` as
subject `test-tenant` with all non-admin roles. Update `test_auth.py` tests that
specifically test the legacy key to delete `PYINTERVIEWBOT_PRINCIPALS`. Update
`test_audit.py` expected actors from `recruiter-7` to `test-tenant` and include
the new `session.start` event.

- [ ] **Step 7: Run targeted tests**

Run: `cd ai-service && uv run --group dev python -m pytest tests/test_db.py tests/test_auth.py tests/test_workflow_routing.py tests/test_session_routing.py tests/test_audit.py tests/test_channels.py -q`
Expected: PASS.

---

### Task 3: Authorize every session read and mutation

**Files:**
- Create: `ai-service/src/pyinterviewbot_ai/authz.py`
- Modify: `ai-service/src/pyinterviewbot_ai/main.py`
- Test: `ai-service/tests/test_tenant_isolation.py`

**Interfaces:**
- Produces: `def session_visible_to(principal: Principal, row) -> bool`
- Produces: `async def authorized_session(session_id: UUID, principal: Principal)`
- Consumes: `Principal.has_role`, `db.get_session_row`, `db.list_sessions`

- [ ] **Step 1: Write failing cross-tenant tests**

Create `test_tenant_isolation.py` with principals:

```python
PRINCIPALS = {
    "alice-key": {"subject": "alice", "roles": ["owner", "author", "deployer"]},
    "bob-key": {"subject": "bob", "roles": ["author"]},
    "admin-key": {"subject": "admin", "roles": ["admin"]},
}
```

Add a test that starts a session as alice and asserts every Bob request returns
`404`:

```python
bob_headers = {"X-API-Key": "bob-key"}
session_id = client.post("/sessions", json={}, headers=alice_headers).json()["session_id"]
for method, path in [
    ("get", f"/sessions/{session_id}/summary"),
    ("get", f"/sessions/{session_id}/report"),
    ("get", f"/sessions/{session_id}/report/export"),
    ("get", f"/sessions/{session_id}/resume-policy"),
    ("get", f"/sessions/{session_id}/priorities"),
    ("post", f"/sessions/{session_id}/turn"),
    ("post", f"/sessions/{session_id}/resume"),
    ("post", f"/sessions/{session_id}/review"),
]:
    response = client.request(method.upper(), path, json={}, headers=bob_headers)
    assert response.status_code == 404
assert client.delete(f"/sessions/{session_id}", headers=bob_headers).status_code == 404
```

Add tests that:

- Alice gets `200` for summary/report/export/priorities/resume-policy/turn.
- Alice's list contains the session; Bob's list is empty; admin's list contains it.
- Admin gets `200` for summary and can delete.
- A valid Alice session token plus Bob's key still returns `404` on turn.
- `test-key` (legacy) returns `403` on `GET /sessions` and
  `GET /sessions/{id}/summary`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ai-service && uv run --group dev python -m pytest tests/test_tenant_isolation.py -q`
Expected: FAIL because Bob can read Alice's session and legacy keys are accepted.

- [ ] **Step 3: Implement the authorized-session helper**

Create `authz.py`:

```python
def session_visible_to(principal: Principal, row) -> bool:
    return principal.has_role("admin") or row["owner_subject"] == principal.subject


async def authorized_session(session_id: UUID, principal: Principal):
    row = await db.get_session_row(session_id)
    if row is None or not session_visible_to(principal, row):
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    return row
```

- [ ] **Step 4: Apply the helper to every session route**

In `main.py`, switch these routes to `require_tenant_auth`, take `principal`,
and use `authorized_session` before any token, policy, report, or delete work:

- `GET /sessions`, `DELETE /sessions/{id}`
- `POST /sessions/{id}/turn`
- `GET /sessions/{id}/summary`, `GET /sessions/{id}/report`
- `GET /sessions/{id}/report/export`
- `GET|PUT /sessions/{id}/resume-policy`, `POST /sessions/{id}/resume`
- `GET|PUT /sessions/{id}/priorities`
- `POST /sessions/{id}/review`

For list: `await db.list_sessions(owner_subject=principal.subject, include_all=principal.has_role("admin"))`.
For delete: pass the same owner/admin values to `db.delete_session`; use the
row owner as `tenant_subject` in the delete audit.

- [ ] **Step 5: Run isolation and existing session tests**

Run: `cd ai-service && uv run --group dev python -m pytest tests/test_tenant_isolation.py tests/test_interview_flow.py tests/test_priorities.py tests/test_reporting.py tests/test_resume.py tests/test_rate_limit.py -q`
Expected: PASS.

---

### Task 4: Non-disclosing workflow ownership

**Files:**
- Modify: `ai-service/src/pyinterviewbot_ai/workflow_api.py`
- Test: `ai-service/tests/test_workflow_safety.py`
- Test: `ai-service/tests/test_tenant_isolation.py`

**Interfaces:**
- Consumes: `_owned_workflow`, `_require_owner`
- Produces: cross-tenant workflow responses `404`

- [ ] **Step 1: Write failing workflow tests**

Update existing cross-tenant assertions from `403` to `404` and add coverage
for read, update, deployments, audit, disable, and rollback:

```python
assert client.get(f"/workflows/{workflow_id}", headers=bob).status_code == 404
assert client.patch(f"/workflows/{workflow_id}", json={"name": "x"}, headers=bob).status_code == 404
assert client.get(f"/workflows/{workflow_id}/audit", headers=bob).status_code == 404
assert client.get(f"/workflows/{workflow_id}/deployments", headers=bob).status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ai-service && uv run --group dev python -m pytest tests/test_workflow_safety.py -q`
Expected: FAIL because foreign workflow access returns `403`.

- [ ] **Step 3: Change owner mismatch to 404**

In `workflow_api._require_owner`:

```python
if record.owner_subject != principal.subject and not principal.has_role("admin"):
    raise HTTPException(status_code=404, detail=f"unknown workflow {record.workflow_id}")
```

- [ ] **Step 4: Run workflow tests**

Run: `cd ai-service && uv run --group dev python -m pytest tests/test_workflow_safety.py tests/test_workflow_api.py tests/test_session_routing.py -q`
Expected: PASS.

---

### Task 5: Tenant-scoped channel mappings and idempotency

**Files:**
- Modify: `ai-service/src/pyinterviewbot_ai/db.py`
- Modify: `ai-service/src/pyinterviewbot_ai/channels.py`
- Modify: `ai-service/src/pyinterviewbot_ai/main.py`
- Test: `ai-service/tests/test_channels.py`
- Test: `ai-service/tests/test_tenant_isolation.py`

**Interfaces:**
- Produces: `get_channel_session(owner_subject: str, channel: str, external_id: str) -> str | None`
- Produces: `bind_channel_session(owner_subject: str, channel: str, external_id: str, session_id: UUID) -> bool`
- Produces: `get_channel_event(owner_subject: str, channel: str, event_id: str) -> dict | None`
- Produces: `record_channel_event(owner_subject: str, channel: str, event_id: str, session_id: UUID, response: dict) -> bool`
- Produces: `channels.submit_message(..., *, owner_subject: str, allow_cross_tenant: bool = False)`

- [ ] **Step 1: Write failing channel isolation and migration tests**

In `test_tenant_isolation.py`:

```python
alice = client.post(
    "/channels/web/sessions",
    json={"external_id": "shared-id"},
    headers=alice_headers,
).json()
bob = client.post(
    "/channels/web/sessions",
    json={"external_id": "shared-id"},
    headers=bob_headers,
).json()
assert alice["session_id"] != bob["session_id"]
```

Post the same `event_id` for both tenants and assert each response is non-duplicate
and each session has one candidate turn. Assert Bob cannot use Alice's
`external_id` to read her session, and Bob's message event does not overwrite
Alice's.

Add a migration test creating old `channel_sessions`/`channel_events` rows,
running `init_db()`, and asserting each row's owner is copied from its session.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ai-service && uv run --group dev python -m pytest tests/test_tenant_isolation.py tests/test_channels.py -q`
Expected: FAIL because the same external ID resolves to one global session.

- [ ] **Step 3: Change channel schema and migration**

Fresh schema:

```sql
CREATE TABLE channel_sessions (
    owner_subject TEXT NOT NULL,
    channel TEXT NOT NULL,
    external_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (owner_subject, channel, external_id)
);
CREATE TABLE channel_events (
    owner_subject TEXT NOT NULL,
    channel TEXT NOT NULL,
    event_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (owner_subject, channel, event_id)
);
```

Migration `003_channel_tenant` detects the old primary key, creates temporary
tables with the new shape, copies `owner_subject` from the referenced session
(falling back to `_legacy_owner_subject()`), drops/renames the old tables, and
recreates no global uniqueness index.

- [ ] **Step 4: Thread the tenant through channel DB and service calls**

Update the four DB functions to include `owner_subject` in every `WHERE` and
`INSERT`. Update `channels.start_or_resume` and `channels.submit_message` to use
the tenant for mapping/event lookup, verify `row["owner_subject"]`, and pass the
tenant to the agent. Update `main.channel_message` to use
`require_tenant_auth` and pass owner/admin.

- [ ] **Step 5: Run channel and isolation tests**

Run: `cd ai-service && uv run --group dev python -m pytest tests/test_channels.py tests/test_tenant_isolation.py tests/test_db.py -q`
Expected: PASS.

---

### Task 6: Tenant-scoped compliance policy

**Files:**
- Modify: `ai-service/src/pyinterviewbot_ai/db.py`
- Modify: `ai-service/src/pyinterviewbot_ai/compliance.py`
- Modify: `ai-service/src/pyinterviewbot_ai/agent.py`
- Modify: `ai-service/src/pyinterviewbot_ai/main.py`
- Test: `ai-service/tests/test_compliance.py`
- Test: `ai-service/tests/test_tenant_isolation.py`

**Interfaces:**
- Produces: `list_compliance_categories(tenant_subject: str) -> list[dict]`
- Produces: `update_compliance_category(tenant_subject: str, category: str, *, ...) -> bool`
- Produces: `reset_compliance_policy(tenant_subject: str) -> None`
- Produces: `load_policy(tenant_subject: str) -> CompliancePolicy`

- [ ] **Step 1: Write failing per-tenant compliance tests**

Add a test that Alice disables `age` and Bob still sees `age` enabled. Add a
reset test scoped per tenant. Add a migration test that old global rows are
copied to the legacy owner and to every distinct existing session owner.

Add an agent test that a session owned by Alice uses Alice's policy after Bob
disables the category.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ai-service && uv run --group dev python -m pytest tests/test_compliance.py tests/test_tenant_isolation.py -q`
Expected: FAIL because policy is global.

- [ ] **Step 3: Change compliance schema and DB functions**

Fresh schema uses `PRIMARY KEY (tenant_subject, category)`. Migration
`004_compliance_tenant` rebuilds the table, copies old rows to the legacy owner
and every distinct `sessions.owner_subject`, and leaves other tenants empty.

`list_compliance_categories(tenant_subject)` seeds `DEFAULT_CATEGORIES` for that
tenant if it has no rows. `update_compliance_category` and
`reset_compliance_policy` filter by tenant. Remove the unconditional global seed
from `init_db`.

- [ ] **Step 4: Pass tenant through policy loading**

Change `compliance.load_policy(tenant_subject)`. In `agent.start_session`, load
`owner_subject`; in `agent.take_turn`, load `row["owner_subject"]`.

In `main.py`, switch compliance GET/PUT/reset routes to `require_tenant_auth`
and pass `principal.subject`.

- [ ] **Step 5: Run compliance and agent tests**

Run: `cd ai-service && uv run --group dev python -m pytest tests/test_compliance.py tests/test_tenant_isolation.py tests/test_workflow_routing.py tests/test_interview_flow.py -q`
Expected: PASS.

---

### Task 7: Recruiter client tenant key

**Files:**
- Modify: `candidate-client/src/recruiter.ts`
- Modify: `README.md` (recruiter auth note only; full docs in Task 8)

**Interfaces:**
- Produces: `apiFetch(path: string): Promise<Response>` using
  `sessionStorage["pyinterviewbot.apiKey"]`
- Produces: one retry after `401`/`403` with a prompted key

- [ ] **Step 1: Implement the key path**

Add:

```ts
const API_KEY_STORAGE = "pyinterviewbot.apiKey";

async function apiFetch(path: string): Promise<Response> {
  let key = sessionStorage.getItem(API_KEY_STORAGE);
  let response = await fetch(`${AI_SERVICE_URL}${path}`, {
    headers: key ? { "X-API-Key": key } : {},
  });
  if (response.status !== 401 && response.status !== 403) return response;

  sessionStorage.removeItem(API_KEY_STORAGE);
  key = window.prompt("AI service tenant API key")?.trim() || "";
  if (!key) return response;
  sessionStorage.setItem(API_KEY_STORAGE, key);
  return fetch(`${AI_SERVICE_URL}${path}`, { headers: { "X-API-Key": key } });
}
```

Replace both direct `fetch` calls in `renderRecruiterView`/`loadSummary` with
`apiFetch`. If the request still fails, show the existing error message. Do not
add a `VITE_*` secret.

- [ ] **Step 2: Build the client**

Run: `cd candidate-client && npm run build`
Expected: PASS with no TypeScript errors.

---

### Task 8: Documentation and final verification

**Files:**
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `docs/SECURITY_MODEL.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/WORKFLOWS.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/SECURITY_AUDIT.md`

**Interfaces:**
- Consumes all prior tasks.

- [ ] **Step 1: Update security and operating docs**

Document:

- Tenant = principal subject; admin cross-tenant.
- Legacy key is rejected on session and compliance routes with `403`.
- Required `PYINTERVIEWBOT_PRINCIPALS` for non-loopback session use.
- `PYINTERVIEWBOT_LEGACY_SESSION_OWNER` migration behavior.
- Cross-tenant IDs return `404`.
- Gateway key subject must match workflow owner; one gateway process per tenant.
- Recruiter view prompts for a tenant key and stores it in `sessionStorage`.
- Session start audit coverage and the remaining audit/retention gaps.
- Remove the old "shared key can read any session" and "routing is not
  tenant-isolated" claims, replacing them with the new boundary and residual
  risks.

- [ ] **Step 2: Run the full Python suite**

Run: `cd ai-service && uv run --group dev python -m pytest -q`
Expected: all tests pass.

- [ ] **Step 3: Run lint and security checks**

Run: `cd ai-service && uv run --group dev ruff check .`
Expected: only the pre-existing violations remain; no new tenant-isolation
violations.

Run: `uvx bandit -r src -f txt`
Expected: no Medium/High findings.

Run: `uv run --with pip-audit pip-audit`
Expected: no known vulnerabilities.

- [ ] **Step 4: Run Rust checks and client build**

Run: `cargo test --workspace`
Expected: 102 passed (unless unrelated changes alter counts).

Run: `cargo clippy --workspace --all-targets -- -D warnings`
Expected: PASS.

Run: `cargo fmt --all -- --check`
Expected: PASS.

Run: `cd candidate-client && npm run build`
Expected: PASS.

Run: `cargo audit`
Expected: exit 0.

- [ ] **Step 5: Update the graph and report**

Run: `graphify update .`
Expected: graph rebuilt without errors.

- [ ] **Step 6: Report results and stop**

Report the test counts, audit results, migration caveat, and any remaining
residual risks. Do not commit unless the user explicitly asks.

---

## Self-Review Checklist

- [ ] Every design goal maps to a task.
- [ ] Every cross-tenant route has a `404` test.
- [ ] Legacy `403` is tested on session and compliance routes.
- [ ] Channel `external_id`/`event_id` isolation is tested.
- [ ] Migration is idempotent and tested with orphaned workflow pins.
- [ ] Audit atomicity and no-PII details are tested.
- [ ] Existing pinned-template and reporting tests are updated, not weakened.
- [ ] No task introduces an unowned session creation path.
- [ ] Gateway/rollout constraints are documented.
