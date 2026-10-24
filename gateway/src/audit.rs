//! Durable security audit trail for admin and provider actions.
//!
//! Audit details must never contain API keys, message bodies, transcripts, or
//! full phone numbers. Callers record only action/target/outcome metadata.

use std::path::PathBuf;
use std::sync::Arc;

use axum::{
    extract::{Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::get,
    Json, Router,
};
use chrono::Utc;
use rusqlite::{params, Connection};
use serde::Serialize;
use serde_json::json;
use tracing::warn;

use crate::ws_handler::{authorized, AppState};

const MAX_DETAIL_CHARS: usize = 500;

#[derive(Debug, Clone, Serialize)]
pub struct AuditEvent {
    pub id: i64,
    pub created_at: String,
    pub action: String,
    pub actor: String,
    pub target: String,
    pub outcome: String,
    pub detail: String,
}

#[derive(Clone)]
pub struct AuditStore {
    path: PathBuf,
}

impl AuditStore {
    pub fn new(path: impl Into<PathBuf>) -> Result<Self, String> {
        let path = path.into();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
        }
        let store = Self { path };
        store.init()?;
        Ok(store)
    }

    fn connect(&self) -> Result<Connection, String> {
        Connection::open(&self.path).map_err(|error| error.to_string())
    }

    fn init(&self) -> Result<(), String> {
        let db = self.connect()?;
        db.execute_batch(
            "PRAGMA journal_mode=WAL;
             CREATE TABLE IF NOT EXISTS audit_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               created_at TEXT NOT NULL,
               action TEXT NOT NULL,
               actor TEXT NOT NULL,
               target TEXT NOT NULL,
               outcome TEXT NOT NULL,
               detail TEXT NOT NULL
             );
             CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at);",
        )
        .map_err(|error| error.to_string())
    }

    pub fn record(
        &self,
        action: &str,
        actor: &str,
        target: &str,
        outcome: &str,
        detail: &str,
    ) -> Result<(), String> {
        let db = self.connect()?;
        let detail: String = detail.chars().take(MAX_DETAIL_CHARS).collect();
        db.execute(
            "INSERT INTO audit_events
             (created_at, action, actor, target, outcome, detail)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                Utc::now().to_rfc3339(),
                action,
                actor,
                target,
                outcome,
                detail
            ],
        )
        .map_err(|error| error.to_string())?;
        Ok(())
    }

    pub fn list(&self, limit: usize) -> Result<Vec<AuditEvent>, String> {
        let db = self.connect()?;
        let mut statement = db
            .prepare(
                "SELECT id, created_at, action, actor, target, outcome, detail
                 FROM audit_events ORDER BY id DESC LIMIT ?1",
            )
            .map_err(|error| error.to_string())?;
        let rows = statement
            .query_map(params![limit.clamp(1, 500) as i64], |row| {
                Ok(AuditEvent {
                    id: row.get(0)?,
                    created_at: row.get(1)?,
                    action: row.get(2)?,
                    actor: row.get(3)?,
                    target: row.get(4)?,
                    outcome: row.get(5)?,
                    detail: row.get(6)?,
                })
            })
            .map_err(|error| error.to_string())?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|error| error.to_string())
    }
}

pub fn from_env() -> Option<Arc<AuditStore>> {
    let path = std::env::var("AUDIT_DB_PATH").unwrap_or_else(|_| "data/audit.db".to_string());
    match AuditStore::new(path) {
        Ok(store) => Some(Arc::new(store)),
        Err(error) => {
            warn!(%error, "audit log disabled");
            None
        }
    }
}

pub fn record_admin(state: &AppState, action: &str, target: &str, outcome: &str, detail: &str) {
    if let Some(store) = &state.audit {
        if let Err(error) = store.record(action, "admin", target, outcome, detail) {
            warn!(%error, "failed to write audit event");
        }
    }
}

pub fn record_webhook(state: &AppState, path: &str, outcome: &str) {
    if let Some(store) = &state.audit {
        if let Err(error) = store.record("webhook", "provider", path, outcome, "") {
            warn!(%error, "failed to write webhook audit event");
        }
    }
}

pub fn router() -> Router<AppState> {
    Router::new().route("/audit", get(list_audit))
}

async fn list_audit(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
) -> Response {
    if !authorized(&headers, &query, &state.api_key) {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    let Some(store) = &state.audit else {
        return Json(json!({"events": []})).into_response();
    };
    let limit = query
        .get("limit")
        .and_then(|value| value.parse().ok())
        .unwrap_or(100);
    match store.list(limit) {
        Ok(events) => Json(json!({"events": events})).into_response(),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": error})),
        )
            .into_response(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn audit_events_are_durable_and_bounded() {
        let path = std::env::temp_dir().join(format!("audit-{}.db", uuid::Uuid::new_v4()));
        let store = AuditStore::new(&path).unwrap();
        store
            .record(
                "schedule.create",
                "admin",
                "sched-1",
                "success",
                &"x".repeat(900),
            )
            .unwrap();
        let events = store.list(10).unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].action, "schedule.create");
        assert_eq!(events[0].detail.len(), MAX_DETAIL_CHARS);
        let _ = std::fs::remove_file(path);
    }
}
