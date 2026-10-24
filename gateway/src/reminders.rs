//! Interview reminder planning.
//!
//! Reminders are durable scheduled actions: Twilio WhatsApp/SMS messages and
//! optional reminder calls at configurable offsets before an interview. The
//! scheduler owns delivery and retries; this module owns reminder planning.

use axum::{
    extract::{Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::post,
    Json, Router,
};
use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tracing::warn;

use crate::audit::record_admin;
use crate::scheduler::{ScheduleAction, ScheduleRequest};
use crate::ws_handler::{authorized, AppState};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReminderChannel {
    Whatsapp,
    Sms,
    Voice,
}

impl ReminderChannel {
    fn as_str(self) -> &'static str {
        match self {
            Self::Whatsapp => "whatsapp",
            Self::Sms => "sms",
            Self::Voice => "voice",
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct ReminderRequest {
    pub interview_id: String,
    pub external_id: String,
    pub start_at: DateTime<Utc>,
    #[serde(default = "default_offsets")]
    pub offsets_minutes: Vec<i64>,
    #[serde(default = "default_channels")]
    pub channels: Vec<ReminderChannel>,
    pub phone: Option<String>,
    pub message: Option<String>,
}

fn default_offsets() -> Vec<i64> {
    vec![1440, 60, 10]
}

fn default_channels() -> Vec<ReminderChannel> {
    vec![ReminderChannel::Whatsapp]
}

#[derive(Debug, Clone, Serialize)]
pub struct PlannedReminder {
    pub channel: ReminderChannel,
    pub offset_minutes: i64,
    pub send_at: DateTime<Utc>,
    pub body: String,
}

pub fn plan_reminders(
    request: &ReminderRequest,
    now: DateTime<Utc>,
    message_template: Option<&str>,
) -> Vec<PlannedReminder> {
    let mut planned = Vec::new();
    let mut offsets = request.offsets_minutes.clone();
    offsets.sort_unstable();
    offsets.dedup();
    for offset in offsets {
        if offset < 0 {
            continue;
        }
        let send_at = request.start_at - Duration::minutes(offset);
        if send_at <= now {
            continue;
        }
        let template = message_template
            .unwrap_or("Reminder: your interview starts in {minutes} minutes. Please be ready.");
        let body = template.replace("{minutes}", &offset.to_string());
        for channel in &request.channels {
            planned.push(PlannedReminder {
                channel: *channel,
                offset_minutes: offset,
                send_at,
                body: body.clone(),
            });
        }
    }
    planned
}

fn payload_for(request: &ReminderRequest, reminder: &PlannedReminder) -> (ScheduleAction, Value) {
    let to = request
        .phone
        .clone()
        .unwrap_or_else(|| request.external_id.clone());
    match reminder.channel {
        ReminderChannel::Whatsapp => (
            ScheduleAction::TwilioMessage,
            json!({"to": to, "body": reminder.body, "channel": "whatsapp"}),
        ),
        ReminderChannel::Sms => (
            ScheduleAction::TwilioMessage,
            json!({"to": to, "body": reminder.body, "channel": "sms"}),
        ),
        ReminderChannel::Voice => (ScheduleAction::TwilioCall, json!({"to": to})),
    }
}

pub fn router() -> Router<AppState> {
    Router::new().route("/reminders", post(create_reminders))
}

async fn create_reminders(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
    Json(request): Json<ReminderRequest>,
) -> Response {
    if !authorized(&headers, &query, &state.api_key) {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    if request.interview_id.trim().is_empty() || request.external_id.trim().is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "interview_id and external_id are required"})),
        )
            .into_response();
    }
    let Some(store) = &state.scheduler else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "scheduler disabled"})),
        )
            .into_response();
    };
    let planned = plan_reminders(&request, Utc::now(), request.message.as_deref());
    let mut created = Vec::new();
    for reminder in planned {
        let (action, payload) = payload_for(&request, &reminder);
        let idempotency_key = format!(
            "reminder:{}:{}:{}",
            request.interview_id,
            reminder.offset_minutes,
            reminder.channel.as_str()
        );
        let schedule = ScheduleRequest {
            action,
            external_id: request.external_id.clone(),
            send_at: reminder.send_at,
            payload,
            idempotency_key: Some(idempotency_key),
        };
        match store.insert(&schedule) {
            Ok(schedule) => created.push(json!({
                "id": schedule.id,
                "channel": reminder.channel,
                "offset_minutes": reminder.offset_minutes,
                "send_at": schedule.send_at,
                "status": schedule.status,
            })),
            Err(error) => {
                warn!(%error, "failed to create reminder");
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(json!({"error": error})),
                )
                    .into_response();
            }
        }
    }
    record_admin(
        &state,
        "reminders.create",
        &request.interview_id,
        "success",
        &format!("{} schedules", created.len()),
    );
    (
        StatusCode::CREATED,
        Json(json!({"created": created, "count": created.len()})),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request() -> ReminderRequest {
        ReminderRequest {
            interview_id: "interview-1".to_string(),
            external_id: "+15551234567".to_string(),
            start_at: "2026-10-27T10:00:00Z".parse().unwrap(),
            offsets_minutes: vec![60, 10, 60, -5],
            channels: vec![ReminderChannel::Whatsapp, ReminderChannel::Voice],
            phone: None,
            message: None,
        }
    }

    #[test]
    fn plans_unique_future_offsets_for_each_channel() {
        let now = "2026-10-26T00:00:00Z".parse().unwrap();
        let planned = plan_reminders(&request(), now, None);
        assert_eq!(planned.len(), 4);
        assert_eq!(planned[0].offset_minutes, 10);
        assert_eq!(planned[1].offset_minutes, 10);
        assert_eq!(planned[2].offset_minutes, 60);
        assert!(planned[0].body.contains("10 minutes"));
    }

    #[test]
    fn skips_offsets_in_the_past() {
        let now = "2026-10-27T09:30:00Z".parse().unwrap();
        let planned = plan_reminders(&request(), now, None);
        assert_eq!(planned.len(), 2);
        assert!(planned.iter().all(|reminder| reminder.offset_minutes == 10));
    }

    #[test]
    fn payload_uses_whatsapp_sms_and_voice_actions() {
        let request = request();
        let reminder = PlannedReminder {
            channel: ReminderChannel::Sms,
            offset_minutes: 10,
            send_at: Utc::now(),
            body: "hello".to_string(),
        };
        let (action, payload) = payload_for(&request, &reminder);
        assert_eq!(action, ScheduleAction::TwilioMessage);
        assert_eq!(payload["channel"], "sms");
        let voice = PlannedReminder {
            channel: ReminderChannel::Voice,
            ..reminder
        };
        assert_eq!(payload_for(&request, &voice).0, ScheduleAction::TwilioCall);
    }
}
