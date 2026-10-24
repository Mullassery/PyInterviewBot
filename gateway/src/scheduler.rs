//! Durable wall-clock scheduler for outbound channel actions.
//!
//! Inspired by the local-first, bounded-retry scheduling model in the sibling
//! PyTaskWireCheck project (MIT). This is a wall-clock action queue, not a DAG
//! runner: SQLite is the source of truth, idempotency keys prevent duplicate
//! actions, and retries use bounded exponential backoff.

use std::path::PathBuf;
use std::time::Duration;

use axum::{
    extract::{Path as AxumPath, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, post},
    Json, Router,
};
use chrono::{DateTime, Utc};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tracing::{error, info, warn};
use uuid::Uuid;

use crate::ws_handler::{authorized, AppState};

const MAX_ATTEMPTS: i64 = 3;
const RETRY_BASE_SECONDS: i64 = 30;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ScheduleAction {
    #[serde(rename = "twilio_call")]
    TwilioCall,
    #[serde(rename = "whatsapp_message")]
    WhatsAppMessage,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Schedule {
    pub id: String,
    pub action: ScheduleAction,
    pub external_id: String,
    pub send_at: DateTime<Utc>,
    pub payload: Value,
    pub status: String,
    pub attempts: i64,
    pub last_error: Option<String>,
    pub idempotency_key: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

#[derive(Debug, Deserialize)]
struct CreateScheduleRequest {
    action: ScheduleAction,
    external_id: String,
    send_at: DateTime<Utc>,
    #[serde(default)]
    payload: Value,
    idempotency_key: Option<String>,
}

#[derive(Clone)]
pub struct SchedulerStore {
    path: PathBuf,
}

impl SchedulerStore {
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
             CREATE TABLE IF NOT EXISTS schedules (
               id TEXT PRIMARY KEY,
               action TEXT NOT NULL,
               external_id TEXT NOT NULL,
               send_at TEXT NOT NULL,
               payload TEXT NOT NULL,
               status TEXT NOT NULL,
               attempts INTEGER NOT NULL DEFAULT 0,
               last_error TEXT,
               idempotency_key TEXT UNIQUE,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
             );
             CREATE INDEX IF NOT EXISTS idx_schedules_due
               ON schedules(status, send_at);",
        )
        .map_err(|error| error.to_string())
    }

    fn insert(&self, request: &CreateScheduleRequest) -> Result<Schedule, String> {
        if request.external_id.trim().is_empty() {
            return Err("external_id is required".to_string());
        }
        let db = self.connect()?;
        let now = Utc::now();
        let id = Uuid::new_v4().to_string();
        let action = match request.action {
            ScheduleAction::TwilioCall => "twilio_call",
            ScheduleAction::WhatsAppMessage => "whatsapp_message",
        };
        db.execute(
            "INSERT OR IGNORE INTO schedules
             (id, action, external_id, send_at, payload, status, attempts,
              idempotency_key, created_at, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, 'pending', 0, ?6, ?7, ?7)",
            params![
                id,
                action,
                request.external_id,
                request.send_at.to_rfc3339(),
                request.payload.to_string(),
                request.idempotency_key,
                now.to_rfc3339(),
            ],
        )
        .map_err(|error| error.to_string())?;
        if let Some(key) = &request.idempotency_key {
            if let Some(existing) = self.by_idempotency(key)? {
                return Ok(existing);
            }
        }
        self.by_id(&id)?
            .ok_or_else(|| "schedule insert failed".to_string())
    }

    fn by_id(&self, id: &str) -> Result<Option<Schedule>, String> {
        let db = self.connect()?;
        db.query_row(
            "SELECT id, action, external_id, send_at, payload, status, attempts,
                    last_error, idempotency_key, created_at, updated_at
             FROM schedules WHERE id = ?1",
            params![id],
            row_to_schedule,
        )
        .optional()
        .map_err(|error| error.to_string())
    }

    fn by_idempotency(&self, key: &str) -> Result<Option<Schedule>, String> {
        let db = self.connect()?;
        db.query_row(
            "SELECT id, action, external_id, send_at, payload, status, attempts,
                    last_error, idempotency_key, created_at, updated_at
             FROM schedules WHERE idempotency_key = ?1",
            params![key],
            row_to_schedule,
        )
        .optional()
        .map_err(|error| error.to_string())
    }

    fn list(&self, status: Option<&str>) -> Result<Vec<Schedule>, String> {
        let db = self.connect()?;
        let mut statement = db
            .prepare(
                "SELECT id, action, external_id, send_at, payload, status, attempts,
                        last_error, idempotency_key, created_at, updated_at
                 FROM schedules
                 WHERE (?1 IS NULL OR status = ?1)
                 ORDER BY send_at ASC LIMIT 200",
            )
            .map_err(|error| error.to_string())?;
        let rows = statement
            .query_map(params![status], row_to_schedule)
            .map_err(|error| error.to_string())?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|error| error.to_string())
    }

    fn cancel(&self, id: &str) -> Result<bool, String> {
        let db = self.connect()?;
        let changed = db
            .execute(
                "UPDATE schedules SET status='cancelled', updated_at=?1
                 WHERE id=?2 AND status='pending'",
                params![Utc::now().to_rfc3339(), id],
            )
            .map_err(|error| error.to_string())?;
        Ok(changed > 0)
    }

    fn claim_due(&self, limit: usize) -> Result<Vec<Schedule>, String> {
        let mut db = self.connect()?;
        let now = Utc::now();
        let now_text = now.to_rfc3339();
        let stale = (now - chrono::Duration::minutes(5)).to_rfc3339();
        db.execute(
            "UPDATE schedules SET status='pending', updated_at=?1
             WHERE status='dispatching' AND updated_at <= ?2",
            params![now_text, stale],
        )
        .map_err(|error| error.to_string())?;
        let transaction = db.transaction().map_err(|error| error.to_string())?;
        let ids: Vec<String> = {
            let mut statement = transaction
                .prepare(
                    "SELECT id FROM schedules
                     WHERE status='pending' AND send_at <= ?1
                     ORDER BY send_at ASC LIMIT ?2",
                )
                .map_err(|error| error.to_string())?;
            let rows = statement
                .query_map(params![now_text, limit as i64], |row| row.get(0))
                .map_err(|error| error.to_string())?;
            rows.collect::<Result<Vec<String>, _>>()
                .map_err(|error| error.to_string())?
        };
        for id in &ids {
            transaction
                .execute(
                    "UPDATE schedules SET status='dispatching', updated_at=?1 WHERE id=?2",
                    params![Utc::now().to_rfc3339(), id],
                )
                .map_err(|error| error.to_string())?;
        }
        transaction.commit().map_err(|error| error.to_string())?;
        ids.into_iter()
            .map(|id| {
                self.by_id(&id)?
                    .ok_or_else(|| "claimed schedule vanished".to_string())
            })
            .collect()
    }

    fn finish(&self, id: &str, error: Option<&str>) -> Result<(), String> {
        let db = self.connect()?;
        let schedule = self
            .by_id(id)?
            .ok_or_else(|| "unknown schedule".to_string())?;
        if error.is_none() {
            db.execute(
                "UPDATE schedules SET status='sent', last_error=NULL, updated_at=?1 WHERE id=?2",
                params![Utc::now().to_rfc3339(), id],
            )
            .map_err(|error| error.to_string())?;
            return Ok(());
        }
        let attempts = schedule.attempts + 1;
        let (status, send_at) = if attempts >= MAX_ATTEMPTS {
            ("failed".to_string(), schedule.send_at)
        } else {
            let delay = RETRY_BASE_SECONDS * (1i64 << attempts.min(6));
            (
                "pending".to_string(),
                Utc::now() + chrono::Duration::seconds(delay),
            )
        };
        db.execute(
            "UPDATE schedules SET status=?1, attempts=?2, last_error=?3,
                    send_at=?4, updated_at=?5 WHERE id=?6",
            params![
                status,
                attempts,
                error,
                send_at.to_rfc3339(),
                Utc::now().to_rfc3339(),
                id
            ],
        )
        .map_err(|error| error.to_string())?;
        Ok(())
    }
}

fn row_to_schedule(row: &rusqlite::Row<'_>) -> rusqlite::Result<Schedule> {
    let action: String = row.get(1)?;
    Ok(Schedule {
        id: row.get(0)?,
        action: match action.as_str() {
            "whatsapp_message" => ScheduleAction::WhatsAppMessage,
            _ => ScheduleAction::TwilioCall,
        },
        external_id: row.get(2)?,
        send_at: row
            .get::<_, String>(3)?
            .parse()
            .unwrap_or_else(|_| Utc::now()),
        payload: serde_json::from_str(&row.get::<_, String>(4)?).unwrap_or(Value::Null),
        status: row.get(5)?,
        attempts: row.get(6)?,
        last_error: row.get(7)?,
        idempotency_key: row.get(8)?,
        created_at: row
            .get::<_, String>(9)?
            .parse()
            .unwrap_or_else(|_| Utc::now()),
        updated_at: row
            .get::<_, String>(10)?
            .parse()
            .unwrap_or_else(|_| Utc::now()),
    })
}

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/schedules", post(create).get(list))
        .route("/schedules/{id}", delete(cancel))
}

fn admin(
    state: &AppState,
    headers: &HeaderMap,
    query: &std::collections::HashMap<String, String>,
) -> bool {
    authorized(headers, query, &state.api_key)
}

async fn create(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
    Json(request): Json<CreateScheduleRequest>,
) -> Response {
    if !admin(&state, &headers, &query) {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    let Some(store) = &state.scheduler else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "scheduler disabled"})),
        )
            .into_response();
    };
    match store.insert(&request) {
        Ok(schedule) => (StatusCode::CREATED, Json(json!({"schedule": schedule}))).into_response(),
        Err(error) => (StatusCode::BAD_REQUEST, Json(json!({"error": error}))).into_response(),
    }
}

async fn list(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
) -> Response {
    if !admin(&state, &headers, &query) {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    let Some(store) = &state.scheduler else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "scheduler disabled"})),
        )
            .into_response();
    };
    match store.list(query.get("status").map(String::as_str)) {
        Ok(schedules) => Json(json!({"schedules": schedules})).into_response(),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": error})),
        )
            .into_response(),
    }
}

async fn cancel(
    State(state): State<AppState>,
    AxumPath(id): AxumPath<String>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
) -> Response {
    if !admin(&state, &headers, &query) {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    let Some(store) = &state.scheduler else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "scheduler disabled"})),
        )
            .into_response();
    };
    match store.cancel(&id) {
        Ok(true) => Json(json!({"cancelled": true, "id": id})).into_response(),
        Ok(false) => (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "pending schedule not found"})),
        )
            .into_response(),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": error})),
        )
            .into_response(),
    }
}

pub fn spawn_worker(state: AppState, interval_seconds: u64) {
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(interval_seconds.max(1))).await;
            let Some(store) = state.scheduler.clone() else {
                continue;
            };
            let due = match store.claim_due(4) {
                Ok(due) => due,
                Err(error) => {
                    warn!(%error, "scheduler claim failed");
                    continue;
                }
            };
            for schedule in due {
                let result = dispatch(&state, &schedule).await;
                match result {
                    Ok(()) => {
                        info!(id=%schedule.id, action=?schedule.action, "scheduled action sent");
                        let _ = store.finish(&schedule.id, None);
                    }
                    Err(error) => {
                        error!(id=%schedule.id, %error, "scheduled action failed");
                        let _ = store.finish(&schedule.id, Some(&error));
                    }
                }
            }
        }
    });
}

async fn dispatch(state: &AppState, schedule: &Schedule) -> Result<(), String> {
    let Some(twilio) = &state.twilio else {
        return Err("Twilio adapter is not configured".to_string());
    };
    match schedule.action {
        ScheduleAction::TwilioCall => {
            let to = schedule
                .payload
                .get("to")
                .and_then(Value::as_str)
                .unwrap_or(&schedule.external_id);
            twilio.create_call(to, &schedule.external_id).await?;
        }
        ScheduleAction::WhatsAppMessage => {
            let to = schedule
                .payload
                .get("to")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .unwrap_or(&schedule.external_id);
            let body = schedule
                .payload
                .get("body")
                .and_then(Value::as_str)
                .ok_or_else(|| "WhatsApp schedule requires payload.body".to_string())?;
            twilio.send_whatsapp(to, body).await?;
        }
    }
    Ok(())
}

pub fn default_db_path() -> PathBuf {
    PathBuf::from(
        std::env::var("SCHEDULER_DB_PATH").unwrap_or_else(|_| "data/schedules.db".to_string()),
    )
}

pub fn init_from_env() -> Option<SchedulerStore> {
    let path = default_db_path();
    match SchedulerStore::new(path) {
        Ok(store) => Some(store),
        Err(error) => {
            warn!(%error, "scheduler disabled");
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn store() -> SchedulerStore {
        let path = std::env::temp_dir().join(format!("sched-{}.db", Uuid::new_v4()));
        SchedulerStore::new(path).unwrap()
    }

    fn request(key: Option<&str>) -> CreateScheduleRequest {
        CreateScheduleRequest {
            action: ScheduleAction::WhatsAppMessage,
            external_id: "candidate-1".to_string(),
            send_at: Utc::now() - chrono::Duration::seconds(5),
            payload: json!({"body": "hello"}),
            idempotency_key: key.map(str::to_string),
        }
    }

    #[test]
    fn idempotency_prevents_duplicate_schedules() {
        let store = store();
        let first = store.insert(&request(Some("k1"))).unwrap();
        let second = store.insert(&request(Some("k1"))).unwrap();
        assert_eq!(first.id, second.id);
        assert_eq!(store.list(None).unwrap().len(), 1);
    }

    #[test]
    fn due_claims_are_atomic_and_cancellation_works() {
        let store = store();
        let schedule = store.insert(&request(None)).unwrap();
        let claimed = store.claim_due(10).unwrap();
        assert_eq!(claimed.len(), 1);
        assert_eq!(claimed[0].status, "dispatching");
        assert!(store.claim_due(10).unwrap().is_empty());
        store.finish(&schedule.id, Some("boom")).unwrap();
        let after = store.by_id(&schedule.id).unwrap().unwrap();
        assert_eq!(after.status, "pending");
        assert_eq!(after.attempts, 1);
        assert!(store.cancel(&schedule.id).unwrap());
    }

    #[test]
    fn retries_become_terminal_after_max_attempts() {
        let store = store();
        let schedule = store.insert(&request(None)).unwrap();
        let _ = store.claim_due(10).unwrap();
        store.finish(&schedule.id, Some("1")).unwrap();
        let _ = store.claim_due(10).unwrap();
        store.finish(&schedule.id, Some("2")).unwrap();
        let _ = store.claim_due(10).unwrap();
        store.finish(&schedule.id, Some("3")).unwrap();
        let final_schedule = store.by_id(&schedule.id).unwrap().unwrap();
        assert_eq!(final_schedule.status, "failed");
        assert_eq!(final_schedule.attempts, 3);
    }
}
