# Live Workflow Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let new interview sessions opt into an active production workflow deployment and pin its approved template snapshot for the session lifetime.

**Architecture:** Resolve the active deployment at session creation, persist workflow provenance plus the artifact's template snapshot, and use a shared session-template resolver for turns, reports, priorities, and reviews. Legacy sessions without a workflow keep the built-in template path.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, aiosqlite, pytest, Rust gateway with reqwest/axum tests, Ruff.

**Spec:** `docs/superpowers/specs/2026-10-27-live-workflow-routing-design.md`

## Global Constraints

- Python tests run from `ai-service` with `uv run --group dev python -m pytest -q`.
- Rust tests run with `cargo test --workspace`.
- Python lint is `uv run --group dev ruff check .` from `ai-service`.
- No dynamic SQL identifiers.
- Optional `workflow_id`; omitted means the legacy `template_id` path.
- Supplied `workflow_id` wins over `template_id`.
- Only active production deployments of active workflows are routable.
- The current production binding fingerprint must match the deployment fingerprint.
- The deployment content hash must match the stored immutable version.
- A routed session never falls back to a built-in template.
- Existing sessions and clients without `workflow_id` keep current behavior.
- No credentials or binding fingerprints are stored on session rows or returned by session APIs.

## Review Focus

- A workflow is promoted, then rolled back while a candidate is mid-interview. The
  in-flight session must keep the originally pinned template.
- A caller supplies `workflow_id` with a stale production binding after provider
  configuration changed. The request must fail closed, not run under the new binding.
- A caller supplies `template_id` and `workflow_id` together. The workflow template
  must win, and the response must report the pinned template ID.
- A routed session's report, priorities, or review is requested after the built-in
  template changes. Every endpoint must use the pinned snapshot, not the current built-in.
- A corrupted or missing deployed version must not create a session or fall back to
  a template.

---

### Task 1: Persist workflow provenance and pinned snapshots

**Files:**
- Modify: `ai-service/src/pyinterviewbot_ai/db.py`
- Test: `ai-service/tests/test_db.py`

**Interfaces:**
- Produces: `db.create_session(session_id: UUID, claim: ResumeClaim, template_id: str = "rag_architect", token_hash: str = "", *, workflow_id: str | None = None, workflow_version: int | None = None, workflow_content_hash: str | None = None, workflow_deployment_id: str | None = None, template_snapshot_json: str | None = None) -> None`
- Produces columns on `sessions`: `workflow_id`, `workflow_version`, `workflow_content_hash`, `workflow_deployment_id`, `template_snapshot_json`.

- [ ] **Step 1: Write the failing migration test**

Add to `ai-service/tests/test_db.py`:

```python
async def test_init_db_migrates_session_workflow_columns(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "legacy.db"))
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
            "resume_claim TEXT NOT NULL)"
        )
        await conn.commit()
    await db.init_db()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cursor = await conn.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in await cursor.fetchall()}
    assert {
        "workflow_id",
        "workflow_version",
        "workflow_content_hash",
        "workflow_deployment_id",
        "template_snapshot_json",
    } <= columns
```

- [ ] **Step 2: Run it and verify failure**

Run: `uv run --group dev python -m pytest tests/test_db.py::test_init_db_migrates_session_workflow_columns -q`

Expected: FAIL because the workflow columns are absent.

- [ ] **Step 3: Add the schema and migrations**

Add the five nullable columns to the `sessions` table in `SCHEMA` and add fixed `ALTER TABLE sessions ADD COLUMN ...` entries to `SESSION_COLUMN_MIGRATIONS`. Extend `create_session` with the keyword-only parameters above and write them in the insert statement. Convert `session_id` to `str` consistently.

- [ ] **Step 4: Add a persistence test**

Add to `ai-service/tests/test_db.py`:

```python
async def test_create_session_persists_workflow_pin() -> None:
    session_id = uuid4()
    await db.init_db()
    await db.create_session(
        session_id,
        ResumeClaim(competency="x", text="x"),
        template_id="backend_engineer",
        workflow_id="00000000-0000-0000-0000-000000000003",
        workflow_version=2,
        workflow_content_hash="abc",
        workflow_deployment_id="deployment-1",
        template_snapshot_json='{"id": "backend_engineer"}',
    )
    row = await db.get_session_row(session_id)
    assert row["workflow_id"] == "00000000-0000-0000-0000-000000000003"
    assert row["workflow_version"] == 2
    assert row["workflow_content_hash"] == "abc"
    assert row["workflow_deployment_id"] == "deployment-1"
    assert row["template_snapshot_json"] == '{"id": "backend_engineer"}'
```

- [ ] **Step 5: Run the migration and persistence tests plus existing database tests**

Run: `uv run --group dev python -m pytest tests/test_db.py tests/test_workflow_store.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ai-service/src/pyinterviewbot_ai/db.py ai-service/tests/test_db.py
git commit -m "feat: persist workflow routing metadata on sessions"
```

---

### Task 2: Resolve active production routes

**Files:**
- Create: `ai-service/src/pyinterviewbot_ai/workflow_routing.py`
- Modify: `ai-service/src/pyinterviewbot_ai/workflow_lifecycle.py`
- Test: `ai-service/tests/test_workflow_routing.py`

**Interfaces:**
- Produces: `WorkflowRoutingError(Exception)` with `status_code: int`.
- Produces: `WorkflowRoute` frozen dataclass with `workflow_id: UUID`, `deployment_id: str`, `version: int`, `content_hash: str`, `template: InterviewTemplate`, `template_snapshot_json: str`.
- Produces: `resolve_workflow_route(workflow_id: UUID) -> WorkflowRoute`.
- Produces: `session_template(row) -> InterviewTemplate`.
- Produces: `workflow_lifecycle.get_active_production_deployment(workflow_id: UUID) -> DeploymentRecord | None`.

- [ ] **Step 1: Write failing resolver tests**

Create `ai-service/tests/test_workflow_routing.py` with helpers that:
1. Call `db.init_db()`.
2. Create a workflow with `workflow_store.create_workflow` using `WorkflowDraft(name="Routing", template_id="backend_engineer")`.
3. Publish it with `workflow_store.publish_version`.
4. Insert an active production deployment row directly using the `production_binding()` fingerprint and the published content hash.

Add tests:

```python
async def test_resolve_active_route_returns_pinned_backend_template(...) -> None:
    route = await routing.resolve_workflow_route(workflow_id)
    assert route.template.id == "backend_engineer"
    assert route.version == 1
    assert route.content_hash == version.content_hash
    assert route.deployment_id == deployment_id


async def test_resolve_unknown_workflow_is_404(...) -> None:
    with pytest.raises(routing.WorkflowRoutingError) as exc:
        await routing.resolve_workflow_route(uuid4())
    assert exc.value.status_code == 404


async def test_resolve_disabled_workflow_is_409(...) -> None:
    await workflow_store.set_status(workflow_id, status="disabled", actor="alice")
    with pytest.raises(routing.WorkflowRoutingError) as exc:
        await routing.resolve_workflow_route(workflow_id)
    assert exc.value.status_code == 409
```

Also add tests for: no active deployment, missing `PYINTERVIEWBOT_PRODUCTION_BINDING_REVISION`, changed revision, deployment/version hash mismatch, and missing version.

- [ ] **Step 2: Run and verify failure**

Run: `uv run --group dev python -m pytest tests/test_workflow_routing.py -q`

Expected: FAIL with `ModuleNotFoundError: pyinterviewbot_ai.workflow_routing`.

- [ ] **Step 3: Implement the resolver**

Create `workflow_routing.py` with:

```python
class WorkflowRoutingError(Exception):
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code
```

Implement `resolve_workflow_route` with these fail-closed checks in order:

1. `workflow_store.get_workflow`; missing → 404.
2. `workflow.status != "active"` → 409.
3. `lifecycle.get_active_production_deployment`; missing → 409.
4. `production_binding()`; `BindingError` → 409.
5. Deployment fingerprint mismatch → 409.
6. `workflow_store.get_version`; missing → 409.
7. Version content hash mismatch → 409.
8. Convert `version.artifact.interview_template` to `InterviewTemplate` and return `WorkflowRoute`.

Add `get_active_production_deployment` to `workflow_lifecycle.py`, querying `environment = 'production'` and `status = 'active'`, newest first.

Implement `session_template(row) -> InterviewTemplate`: if `template_snapshot_json` is present, validate `TemplateSnapshot.model_validate_json` and convert; otherwise return `get_template(row["template_id"])`. Raise `RuntimeError("stored workflow template snapshot is invalid")` on corrupt pinned data.

Add a corrupt-snapshot test:

```python
def test_session_template_rejects_corrupt_snapshot() -> None:
    with pytest.raises(RuntimeError, match="stored workflow template snapshot is invalid"):
        routing.session_template({"template_id": "rag_architect", "template_snapshot_json": "{"})
```

- [ ] **Step 4: Run resolver tests**

Run: `uv run --group dev python -m pytest tests/test_workflow_routing.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai-service/src/pyinterviewbot_ai/workflow_routing.py ai-service/src/pyinterviewbot_ai/workflow_lifecycle.py ai-service/tests/test_workflow_routing.py
git commit -m "feat: resolve active production workflow routes"
```

---

### Task 3: Route agent session starts and pin turns

**Files:**
- Modify: `ai-service/src/pyinterviewbot_ai/agent.py`
- Test: `ai-service/tests/test_workflow_routing.py`

**Interfaces:**
- Produces: `SessionStartResult` frozen dataclass with `session_id: UUID`, `question_text: str`, `template_id: str`, `session_token: str`, `workflow_id: UUID | None`, `workflow_version: int | None`, `workflow_deployment_id: str | None`.
- Changes: `agent.start_session(template_id: str | None = None, workflow_id: UUID | None = None) -> SessionStartResult`.
- Changes: `agent.take_turn` and all session template lookups use `routing.session_template(row)`.

- [ ] **Step 1: Write failing agent tests**

Add to `test_workflow_routing.py`:

```python
async def test_start_session_with_workflow_pins_snapshot(...) -> None:
    result = await agent.start_session("ignored_template", workflow_id)
    row = await db.get_session_row(result.session_id)
    assert result.template_id == "backend_engineer"
    assert row["workflow_id"] == str(workflow_id)
    assert row["template_snapshot_json"] is not None
    assert row["template_snapshot_json"] != ""


async def test_session_template_uses_pinned_snapshot(monkeypatch, ...) -> None:
    result = await agent.start_session("ignored_template", workflow_id)
    row = await db.get_session_row(result.session_id)
    monkeypatch.setattr(routing, "get_template", lambda *a, **k: (_ for _ in ()).throw(AssertionError("built-in used")))
    template = routing.session_template(row)
    assert template.id == "backend_engineer"
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --group dev python -m pytest tests/test_workflow_routing.py -q -k 'start_session or pinned_snapshot'`

Expected: FAIL because `start_session` does not accept `workflow_id` and does not pin snapshots.

- [ ] **Step 3: Implement session result and pinning**

In `agent.py`, resolve the route before creating the session. With a route, use `route.template` and persist `template_snapshot_json`, `workflow_id`, `workflow_version`, `workflow_content_hash`, and `workflow_deployment_id`. Without a route, use `get_template(template_id)` and leave those fields `None`.

Use `routing.session_template(row)` in `take_turn` instead of `get_template(row["template_id"])`.

Log routed starts at INFO with `workflow_id`, `workflow_version`, `workflow_content_hash`, and `workflow_deployment_id`. Do not log the template body, transcript, credentials, or binding fingerprint.

- [ ] **Step 4: Run agent tests and the full Python suite**

Run: `uv run --group dev python -m pytest tests/test_workflow_routing.py tests/test_interview_flow.py tests/test_resume.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai-service/src/pyinterviewbot_ai/agent.py ai-service/tests/test_workflow_routing.py
git commit -m "feat: pin workflow templates to interview sessions"
```

---

### Task 4: Expose workflow routing on direct session API

**Files:**
- Modify: `ai-service/src/pyinterviewbot_ai/models.py`
- Modify: `ai-service/src/pyinterviewbot_ai/main.py`
- Test: `ai-service/tests/test_session_routing.py`

**Interfaces:**
- Changes: `StartSessionRequest` gains `workflow_id: UUID | None = None`.
- Changes: `StartSessionResponse` gains optional `workflow_id: UUID | None`, `workflow_version: int | None`, `workflow_deployment_id: str | None`.
- Consumes: `agent.start_session`, `routing.session_template`, `routing.WorkflowRoutingError`.

- [ ] **Step 1: Write failing API tests**

Create `ai-service/tests/test_session_routing.py` with a `workflow_auth` fixture and a `_fully_promote` helper using the workflow API. Add tests:

```python
def test_start_session_routes_to_active_workflow(client, workflow_auth) -> None:
    workflow_id = _promoted_workflow(client, workflow_auth)
    response = client.post(
        "/sessions",
        json={"template_id": "rag_architect", "workflow_id": workflow_id},
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["template_id"] == "backend_engineer"
    assert body["workflow_id"] == workflow_id
    assert body["workflow_version"] == 1


def test_start_session_unknown_workflow_is_404(client, workflow_auth) -> None:
    response = client.post(
        "/sessions",
        json={"workflow_id": str(uuid4())},
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 404


def test_start_session_stale_binding_is_409(client, workflow_auth, monkeypatch) -> None:
    workflow_id = _promoted_workflow(client, workflow_auth)
    monkeypatch.setenv("PYINTERVIEWBOT_PRODUCTION_BINDING_REVISION", "prod-test-2")
    response = client.post(
        "/sessions",
        json={"workflow_id": workflow_id},
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 409
```

Add tests for no active deployment, disabled workflow, report/export/priorities/review using the pinned snapshot after patching `routing.get_template` to raise, and promotion of a new version after session creation leaving the original session pin unchanged.

- [ ] **Step 2: Run and verify failure**

Run: `uv run --group dev python -m pytest tests/test_session_routing.py -q`

Expected: FAIL because the API ignores `workflow_id`.

- [ ] **Step 3: Implement API routing and shared template use**

Add the request/response fields. In `create_session`, pass `req.workflow_id` to `agent.start_session`, catch `WorkflowRoutingError`, and raise `HTTPException(status_code=exc.status_code, detail=str(exc))`.

Replace `templates.get_template(row["template_id"])` in report, export, priorities, and review with `routing.session_template(row)`. Keep the existing default-template fallback only in report/export for legacy unknown template IDs.

- [ ] **Step 4: Run routing API tests and the full suite**

Run: `uv run --group dev python -m pytest tests/test_session_routing.py -q && uv run --group dev python -m pytest -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai-service/src/pyinterviewbot_ai/models.py ai-service/src/pyinterviewbot_ai/main.py ai-service/tests/test_session_routing.py
git commit -m "feat: route direct sessions to active workflows"
```

---

### Task 5: Route channel session creation

**Files:**
- Modify: `ai-service/src/pyinterviewbot_ai/models.py`
- Modify: `ai-service/src/pyinterviewbot_ai/channels.py`
- Modify: `ai-service/src/pyinterviewbot_ai/main.py`
- Test: `ai-service/tests/test_channels.py`
- Test: `ai-service/tests/test_session_routing.py`

**Interfaces:**
- Changes: `ChannelSessionRequest` gains `workflow_id: UUID | None = None`.
- Changes: `ChannelSessionResponse` gains optional `workflow_id`, `workflow_version`, `workflow_deployment_id`.
- Changes: `channels.start_or_resume(channel, external_id, template_id, workflow_id=None)`.

- [ ] **Step 1: Write failing channel tests**

Add:

```python
def test_channel_session_routes_to_workflow(client, workflow_auth) -> None:
    workflow_id = _promoted_workflow(client, workflow_auth)
    response = client.post(
        "/channels/web/sessions",
        json={"external_id": "candidate-1", "workflow_id": workflow_id},
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 200
    assert response.json()["template_id"] == "backend_engineer"
    assert response.json()["workflow_id"] == workflow_id


def test_resuming_channel_session_keeps_original_pin(client, workflow_auth, monkeypatch) -> None:
    workflow_id = _promoted_workflow(client, workflow_auth)
    first = client.post("/channels/web/sessions", json={"external_id": "candidate-2", "workflow_id": workflow_id}, headers={"X-API-Key": "test-key"})
    assert first.status_code == 200
    second = client.post("/channels/web/sessions", json={"external_id": "candidate-2", "workflow_id": str(uuid4())}, headers={"X-API-Key": "test-key"})
    assert second.status_code == 200
    assert second.json()["workflow_id"] == workflow_id
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --group dev python -m pytest tests/test_channels.py tests/test_session_routing.py -q -k channel`

Expected: FAIL because channel requests do not accept or forward `workflow_id`.

- [ ] **Step 3: Implement channel routing**

Pass `workflow_id` from `main.channel_session` into `channels.start_or_resume`. On a new session, call `agent.start_session(template_id, workflow_id)`. On an existing session, ignore the supplied workflow ID and return the stored pin. Populate the new optional response fields from the session row.

- [ ] **Step 4: Run channel tests**

Run: `uv run --group dev python -m pytest tests/test_channels.py tests/test_session_routing.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai-service/src/pyinterviewbot_ai/models.py ai-service/src/pyinterviewbot_ai/channels.py ai-service/src/pyinterviewbot_ai/main.py ai-service/tests/test_channels.py ai-service/tests/test_session_routing.py
git commit -m "feat: route channel sessions to active workflows"
```

---

### Task 6: Forward workflow ID from the gateway

**Files:**
- Modify: `gateway/src/ai_client.rs`
- Modify: `gateway/src/main.rs`
- Test: `gateway/src/ai_client.rs` inline tests

**Interfaces:**
- Changes: `AiClient` gains `workflow_id: Option<String>` and `with_workflow(Option<String>) -> Self`.
- Changes: `StartSessionRequest` and `ChannelSessionRequest` serialize optional `workflow_id`.
- Configuration: `PYINTERVIEWBOT_WORKFLOW_ID`.

- [ ] **Step 1: Write failing gateway tests**

Extend the existing `agent_start_forwards_template` test:

```rust
let client = AiClient::new(format!("http://{addr}"))
    .with_api_key(Some("secret".to_string()))
    .with_template(Some("backend_engineer".to_string()))
    .with_workflow(Some("00000000-0000-0000-0000-000000000002".to_string()));
...
assert_eq!(body["workflow_id"], "00000000-0000-0000-0000-000000000002");
```

Add a channel start test that captures the posted body and asserts both `template_id` and `workflow_id`.

- [ ] **Step 2: Run and verify failure**

Run: `cargo test -p pyinterviewbot-gateway ai_client -- --nocapture`

Expected: compile failure because `with_workflow` and the request field do not exist.

- [ ] **Step 3: Implement gateway forwarding**

Add the optional field to `AiClient`, both request structs, and `with_workflow`. In `main.rs`, read `PYINTERVIEWBOT_WORKFLOW_ID`, filter empty values, and call `.with_workflow(workflow_id)`. Include the ID in direct and channel session starts.

- [ ] **Step 4: Run gateway tests**

Run: `cargo test -p pyinterviewbot-gateway ai_client -- --nocapture`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/src/ai_client.rs gateway/src/main.rs
git commit -m "feat: forward workflow id from gateway"
```

---

### Task 7: Update docs, rerun security audit, verify

**Files:**
- Modify: `docs/WORKFLOWS.md`
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `docs/SECURITY_MODEL.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/ROADMAP.md`
- Modify: `docs/internal/BASELINE_AUDIT.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/SECURITY_AUDIT.md`
- Test: all existing suites

- [ ] **Step 1: Update runtime documentation**

Remove registry-only claims. Document optional `workflow_id`, `PYINTERVIEWBOT_WORKFLOW_ID`, pinned snapshots, binding/hash checks, error behavior, and that in-flight sessions survive promotion/rollback/disable. Correct the stale disabled-workflow sentence in `docs/WORKFLOWS.md`: approval decisions are now blocked while disabled.

- [ ] **Step 2: Run the security audit**

Run from `ai-service`:

```bash
uvx bandit -r src -f txt
uv run --with pip-audit pip-audit
```

Run from the repository root:

```bash
cargo audit
```

Perform a manual review of `workflow_routing.py`, `agent.py`, `db.py`, `main.py`, `channels.py`, and the gateway request path for authorization, SQL injection, secret exposure, fail-open behavior, and denial of service.

- [ ] **Step 3: Update the audit report**

Rewrite `docs/SECURITY_AUDIT.md` with the new date, commands, findings, false positives, and residual risks. Explicitly state whether the earlier four SQL findings remain reproducible. Record any tool that could not run and why.

- [ ] **Step 4: Full verification**

Run:

```bash
cd ai-service
uv run --group dev python -m pytest -q
uv run --group dev ruff check .
cd ..
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
```

Expected: Python tests pass; Rust tests pass; no new clippy warnings. Record the exact Ruff baseline if pre-existing violations remain.

- [ ] **Step 5: Commit**

```bash
git add docs README.md SECURITY.md CHANGELOG.md
git commit -m "docs: document live workflow routing and rerun security audit"
```
