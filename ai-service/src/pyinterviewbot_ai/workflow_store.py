"""Transactional workflow lifecycle persistence.

Each mutating operation claims a SQLite write lock (``BEGIN IMMEDIATE``) so
version numbers and idempotency keys cannot race. Artifacts are written once;
later draft edits only affect future versions.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from . import db
from .workflow_models import (
    WorkflowArtifact,
    WorkflowDraft,
    artifact_from_json,
    build_artifact,
)


class WorkflowError(Exception):
    """Base class for workflow lifecycle errors."""


class WorkflowNotFoundError(WorkflowError):
    pass


class WorkflowConflictError(WorkflowError):
    pass


class WorkflowStateError(WorkflowError):
    pass


@dataclass(frozen=True)
class WorkflowRecord:
    workflow_id: UUID
    owner_subject: str
    name: str
    description: str
    template_id: str
    draft: WorkflowDraft
    status: str
    latest_version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WorkflowVersionRecord:
    version_id: str
    workflow_id: UUID
    version: int
    content_hash: str
    artifact: WorkflowArtifact
    created_by: str
    idempotency_key: str | None
    created_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _clean_key(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _workflow_from_row(row: Any, latest_version: int) -> WorkflowRecord:
    return WorkflowRecord(
        workflow_id=UUID(row["id"]),
        owner_subject=row["owner_subject"],
        name=row["name"],
        description=row["description"],
        template_id=row["template_id"],
        draft=WorkflowDraft.model_validate_json(row["draft_json"]),
        status=row["status"],
        latest_version=latest_version,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _version_from_row(row: Any) -> WorkflowVersionRecord:
    artifact = artifact_from_json(row["artifact_json"])
    if (
        not artifact.verify_content_hash()
        or artifact.content_hash != row["content_hash"]
    ):
        raise WorkflowStateError(
            "stored workflow artifact hash does not match its content"
        )
    return WorkflowVersionRecord(
        version_id=row["id"],
        workflow_id=UUID(row["workflow_id"]),
        version=row["version"],
        content_hash=row["content_hash"],
        artifact=artifact,
        created_by=row["created_by"],
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
    )


async def _latest_version(conn: aiosqlite.Connection, workflow_id: UUID) -> int:
    cursor = await conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM workflow_versions WHERE workflow_id = ?",
        (str(workflow_id),),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def _audit(
    conn: aiosqlite.Connection,
    *,
    workflow_id: UUID | str,
    version: int | None,
    action: str,
    actor: str,
    details: dict | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO workflow_audit "
        "(workflow_id, version, action, actor_subject, details_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            str(workflow_id),
            version,
            action,
            actor,
            json.dumps(details or {}, sort_keys=True),
            _now(),
        ),
    )


async def create_workflow(
    *,
    owner_subject: str,
    draft: WorkflowDraft,
    actor: str,
) -> WorkflowRecord:
    workflow_id = uuid4()
    now = _now()
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute(
                "INSERT INTO workflows "
                "(id, owner_subject, name, description, template_id, draft_json, "
                "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    str(workflow_id),
                    owner_subject,
                    draft.name,
                    draft.description,
                    draft.template_id,
                    draft.model_dump_json(),
                    now,
                    now,
                ),
            )
            await _audit(
                conn,
                workflow_id=workflow_id,
                version=None,
                action="workflow.created",
                actor=actor,
                details={"name": draft.name, "template_id": draft.template_id},
            )
            await conn.commit()
        except sqlite3.IntegrityError as exc:
            await conn.rollback()
            raise WorkflowConflictError(
                "a workflow with this name already exists for this owner"
            ) from exc
    return WorkflowRecord(
        workflow_id=workflow_id,
        owner_subject=owner_subject,
        name=draft.name,
        description=draft.description,
        template_id=draft.template_id,
        draft=draft,
        status="active",
        latest_version=0,
        created_at=now,
        updated_at=now,
    )


async def get_workflow(workflow_id: UUID) -> WorkflowRecord | None:
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM workflows WHERE id = ?", (str(workflow_id),))
        row = await cursor.fetchone()
        if row is None:
            return None
        return _workflow_from_row(row, await _latest_version(conn, workflow_id))


async def list_workflows(
    *, owner_subject: str | None = None, include_all: bool = False
) -> list[WorkflowRecord]:
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        if include_all:
            cursor = await conn.execute("SELECT * FROM workflows ORDER BY created_at DESC")
        else:
            cursor = await conn.execute(
                "SELECT * FROM workflows WHERE owner_subject = ? ORDER BY created_at DESC",
                (owner_subject or "",),
            )
        records: list[WorkflowRecord] = []
        for row in await cursor.fetchall():
            latest = await _latest_version(conn, UUID(row["id"]))
            records.append(_workflow_from_row(row, latest))
        return records


async def update_draft(
    workflow_id: UUID,
    *,
    draft: WorkflowDraft,
    actor: str,
) -> WorkflowRecord:
    now = _now()
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (str(workflow_id),)
            )
            if await cursor.fetchone() is None:
                await conn.rollback()
                raise WorkflowNotFoundError(f"unknown workflow {workflow_id}")
            await conn.execute(
                "UPDATE workflows SET name = ?, description = ?, template_id = ?, "
                "draft_json = ?, updated_at = ? WHERE id = ?",
                (
                    draft.name,
                    draft.description,
                    draft.template_id,
                    draft.model_dump_json(),
                    now,
                    str(workflow_id),
                ),
            )
            await _audit(
                conn,
                workflow_id=workflow_id,
                version=None,
                action="workflow.draft_updated",
                actor=actor,
                details={"name": draft.name, "template_id": draft.template_id},
            )
            await conn.commit()
        except sqlite3.IntegrityError as exc:
            await conn.rollback()
            raise WorkflowConflictError(
                "a workflow with this name already exists for this owner"
            ) from exc
    record = await get_workflow(workflow_id)
    if record is None:
        raise WorkflowNotFoundError(f"unknown workflow {workflow_id}")
    return record


async def set_status(
    workflow_id: UUID, *, status: str, actor: str
) -> WorkflowRecord:
    if status not in {"active", "disabled"}:
        raise WorkflowStateError(f"unsupported workflow status {status}")
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            "UPDATE workflows SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), str(workflow_id)),
        )
        if cursor.rowcount == 0:
            await conn.rollback()
            raise WorkflowNotFoundError(f"unknown workflow {workflow_id}")
        await _audit(
            conn,
            workflow_id=workflow_id,
            version=None,
            action=f"workflow.{status}",
            actor=actor,
        )
        await conn.commit()
    record = await get_workflow(workflow_id)
    if record is None:
        raise WorkflowNotFoundError(f"unknown workflow {workflow_id}")
    return record


async def publish_version(
    workflow_id: UUID,
    *,
    actor: str,
    idempotency_key: str | None = None,
) -> WorkflowVersionRecord:
    key = _clean_key(idempotency_key)
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        if key is not None:
            cursor = await conn.execute(
                "SELECT * FROM workflow_versions WHERE workflow_id = ? AND idempotency_key = ?",
                (str(workflow_id), key),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                await conn.rollback()
                return _version_from_row(existing)

        cursor = await conn.execute(
            "SELECT * FROM workflows WHERE id = ?", (str(workflow_id),)
        )
        row = await cursor.fetchone()
        if row is None:
            await conn.rollback()
            raise WorkflowNotFoundError(f"unknown workflow {workflow_id}")

        next_version = await _latest_version(conn, workflow_id) + 1
        draft = WorkflowDraft.model_validate_json(row["draft_json"])
        artifact = build_artifact(
            workflow_id=workflow_id,
            version=next_version,
            owner_subject=row["owner_subject"],
            draft=draft,
        )
        version_id = str(uuid4())
        now = _now()
        try:
            await conn.execute(
                "INSERT INTO workflow_versions "
                "(id, workflow_id, version, content_hash, artifact_json, created_by, "
                "idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    str(workflow_id),
                    next_version,
                    artifact.content_hash or "",
                    artifact.model_dump_json(),
                    actor,
                    key,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            await conn.rollback()
            if key is not None:
                async with db.connect() as retry_conn:
                    retry_conn.row_factory = aiosqlite.Row
                    retry_cursor = await retry_conn.execute(
                        "SELECT * FROM workflow_versions "
                        "WHERE workflow_id = ? AND idempotency_key = ?",
                        (str(workflow_id), key),
                    )
                    retry_row = await retry_cursor.fetchone()
                    if retry_row is not None:
                        return _version_from_row(retry_row)
            raise
        await conn.execute(
            "UPDATE workflows SET name = ?, description = ?, template_id = ?, updated_at = ? "
            "WHERE id = ?",
            (
                draft.name,
                draft.description,
                draft.template_id,
                now,
                str(workflow_id),
            ),
        )
        await _audit(
            conn,
            workflow_id=workflow_id,
            version=next_version,
            action="workflow.version_published",
            actor=actor,
            details={"content_hash": artifact.content_hash},
        )
        await conn.commit()
    return WorkflowVersionRecord(
        version_id=version_id,
        workflow_id=workflow_id,
        version=next_version,
        content_hash=artifact.content_hash or "",
        artifact=artifact,
        created_by=actor,
        idempotency_key=key,
        created_at=now,
    )


async def get_version(workflow_id: UUID, version: int) -> WorkflowVersionRecord | None:
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM workflow_versions WHERE workflow_id = ? AND version = ?",
            (str(workflow_id), version),
        )
        row = await cursor.fetchone()
        return _version_from_row(row) if row is not None else None


async def list_versions(workflow_id: UUID) -> list[WorkflowVersionRecord]:
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM workflow_versions WHERE workflow_id = ? ORDER BY version ASC",
            (str(workflow_id),),
        )
        return [_version_from_row(row) for row in await cursor.fetchall()]


async def list_audit(workflow_id: UUID) -> list[dict]:
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT version, action, actor_subject, details_json, created_at "
            "FROM workflow_audit WHERE workflow_id = ? ORDER BY id ASC",
            (str(workflow_id),),
        )
        return [
            {
                "version": row["version"],
                "action": row["action"],
                "actor_subject": row["actor_subject"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in await cursor.fetchall()
        ]


__all__ = [
    "WorkflowConflictError",
    "WorkflowError",
    "WorkflowNotFoundError",
    "WorkflowRecord",
    "WorkflowStateError",
    "WorkflowVersionRecord",
    "create_workflow",
    "get_version",
    "get_workflow",
    "list_audit",
    "list_versions",
    "list_workflows",
    "publish_version",
    "set_status",
    "update_draft",
]
