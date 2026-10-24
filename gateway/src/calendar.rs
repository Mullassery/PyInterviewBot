//! Provider-neutral calendar scheduling for Google Calendar and Microsoft Graph.
//!
//! Server-side access tokens or refresh-token OAuth are supported. Calendar
//! providers create interview events and return the provider's join URL.

use std::sync::Mutex;
use std::time::{Duration, Instant};

use axum::{
    extract::{Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tracing::warn;

use crate::ws_handler::{authorized, AppState};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CalendarProvider {
    Google,
    Outlook,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CalendarEventRequest {
    pub provider: CalendarProvider,
    pub subject: String,
    #[serde(default)]
    pub description: String,
    pub start: DateTime<Utc>,
    pub end: DateTime<Utc>,
    #[serde(default = "utc_timezone")]
    pub timezone: String,
    #[serde(default)]
    pub attendees: Vec<String>,
    #[serde(default)]
    pub create_online_meeting: bool,
    pub calendar_id: Option<String>,
    pub user_id: Option<String>,
}

fn utc_timezone() -> String {
    "UTC".to_string()
}

#[derive(Debug, Clone, Serialize)]
pub struct CalendarEventResponse {
    pub provider: CalendarProvider,
    pub event_id: String,
    pub join_url: Option<String>,
    pub raw: Value,
}

struct TokenCache {
    token: String,
    expires_at: Instant,
}

pub struct GoogleCalendarConfig {
    access_token: Option<String>,
    client_id: Option<String>,
    client_secret: Option<String>,
    refresh_token: Option<String>,
    pub base_url: String,
    pub token_url: String,
    pub calendar_id: String,
    cache: Mutex<Option<TokenCache>>,
}

pub struct OutlookCalendarConfig {
    access_token: Option<String>,
    client_id: Option<String>,
    client_secret: Option<String>,
    refresh_token: Option<String>,
    tenant_id: String,
    pub base_url: String,
    pub token_url: Option<String>,
    pub user_id: Option<String>,
    cache: Mutex<Option<TokenCache>>,
}

pub struct CalendarClient {
    http: reqwest::Client,
    pub google: Option<GoogleCalendarConfig>,
    pub outlook: Option<OutlookCalendarConfig>,
}

impl CalendarClient {
    pub fn from_env() -> Option<Self> {
        let google_access = std::env::var("GOOGLE_CALENDAR_ACCESS_TOKEN").ok();
        let google_refresh = std::env::var("GOOGLE_CALENDAR_REFRESH_TOKEN").ok();
        let google = if google_access.is_some() || google_refresh.is_some() {
            Some(GoogleCalendarConfig {
                access_token: google_access.filter(|value| !value.is_empty()),
                client_id: std::env::var("GOOGLE_CLIENT_ID").ok(),
                client_secret: std::env::var("GOOGLE_CLIENT_SECRET").ok(),
                refresh_token: google_refresh.filter(|value| !value.is_empty()),
                base_url: std::env::var("GOOGLE_CALENDAR_BASE_URL")
                    .unwrap_or_else(|_| "https://www.googleapis.com".to_string()),
                token_url: std::env::var("GOOGLE_TOKEN_URL")
                    .unwrap_or_else(|_| "https://oauth2.googleapis.com/token".to_string()),
                calendar_id: std::env::var("GOOGLE_CALENDAR_ID")
                    .unwrap_or_else(|_| "primary".to_string()),
                cache: Mutex::new(None),
            })
        } else {
            None
        };

        let ms_access = std::env::var("MICROSOFT_GRAPH_ACCESS_TOKEN").ok();
        let ms_refresh = std::env::var("MICROSOFT_REFRESH_TOKEN").ok();
        let outlook = if ms_access.is_some() || ms_refresh.is_some() {
            Some(OutlookCalendarConfig {
                access_token: ms_access.filter(|value| !value.is_empty()),
                client_id: std::env::var("MICROSOFT_CLIENT_ID").ok(),
                client_secret: std::env::var("MICROSOFT_CLIENT_SECRET").ok(),
                refresh_token: ms_refresh.filter(|value| !value.is_empty()),
                tenant_id: std::env::var("MICROSOFT_TENANT_ID")
                    .unwrap_or_else(|_| "common".to_string()),
                base_url: std::env::var("MICROSOFT_GRAPH_BASE_URL")
                    .unwrap_or_else(|_| "https://graph.microsoft.com/v1.0".to_string()),
                token_url: std::env::var("MICROSOFT_TOKEN_URL").ok(),
                user_id: std::env::var("MICROSOFT_GRAPH_USER_ID")
                    .ok()
                    .filter(|value| !value.is_empty()),
                cache: Mutex::new(None),
            })
        } else {
            None
        };

        if google.is_none() && outlook.is_none() {
            return None;
        }
        Some(Self {
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(20))
                .build()
                .expect("calendar http client"),
            google,
            outlook,
        })
    }

    pub fn providers(&self) -> Vec<&'static str> {
        let mut providers = Vec::new();
        if self.google.is_some() {
            providers.push("google");
        }
        if self.outlook.is_some() {
            providers.push("outlook");
        }
        providers
    }

    pub async fn create_event(
        &self,
        request: &CalendarEventRequest,
    ) -> Result<CalendarEventResponse, String> {
        if request.subject.trim().is_empty() {
            return Err("subject is required".to_string());
        }
        if request.end <= request.start {
            return Err("end must be after start".to_string());
        }
        match request.provider {
            CalendarProvider::Google => self.create_google(request).await,
            CalendarProvider::Outlook => self.create_outlook(request).await,
        }
    }

    async fn create_google(
        &self,
        request: &CalendarEventRequest,
    ) -> Result<CalendarEventResponse, String> {
        let config = self
            .google
            .as_ref()
            .ok_or("Google Calendar is not configured")?;
        let token = google_token(config, &self.http).await?;
        let calendar_id = request
            .calendar_id
            .as_deref()
            .unwrap_or(&config.calendar_id);
        let url = format!(
            "{}/calendar/v3/calendars/{}/events?conferenceDataVersion=1",
            config.base_url.trim_end_matches('/'),
            urlencoding::encode(calendar_id)
        );
        let body = google_event_body(request);
        let response = self
            .http
            .post(url)
            .bearer_auth(token)
            .json(&body)
            .send()
            .await
            .map_err(|error| error.to_string())?;
        parse_response(CalendarProvider::Google, response, |value| {
            value
                .get("hangoutLink")
                .and_then(Value::as_str)
                .map(str::to_string)
                .or_else(|| {
                    value
                        .get("conferenceData")
                        .and_then(|data| data.get("entryPoints"))
                        .and_then(Value::as_array)
                        .and_then(|entries| entries.first())
                        .and_then(|entry| entry.get("uri"))
                        .and_then(Value::as_str)
                        .map(str::to_string)
                })
        })
        .await
    }

    async fn create_outlook(
        &self,
        request: &CalendarEventRequest,
    ) -> Result<CalendarEventResponse, String> {
        let config = self
            .outlook
            .as_ref()
            .ok_or("Outlook Calendar is not configured")?;
        let token = outlook_token(config, &self.http).await?;
        let user_id = request.user_id.as_deref().or(config.user_id.as_deref());
        let path = match user_id {
            Some(user) => format!("/users/{}/events", urlencoding::encode(user)),
            None => "/me/events".to_string(),
        };
        let url = format!("{}{}", config.base_url.trim_end_matches('/'), path);
        let body = outlook_event_body(request);
        let response = self
            .http
            .post(url)
            .bearer_auth(token)
            .json(&body)
            .send()
            .await
            .map_err(|error| error.to_string())?;
        parse_response(CalendarProvider::Outlook, response, |value| {
            value
                .get("onlineMeeting")
                .and_then(|meeting| meeting.get("joinUrl"))
                .and_then(Value::as_str)
                .map(str::to_string)
                .or_else(|| {
                    value
                        .get("webLink")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                })
        })
        .await
    }
}

fn google_event_body(request: &CalendarEventRequest) -> Value {
    let mut body = json!({
        "summary": request.subject,
        "description": request.description,
        "start": {"dateTime": request.start.to_rfc3339(), "timeZone": request.timezone},
        "end": {"dateTime": request.end.to_rfc3339(), "timeZone": request.timezone},
        "attendees": request.attendees.iter().map(|email| json!({"email": email})).collect::<Vec<_>>(),
    });
    if request.create_online_meeting {
        body["conferenceData"] = json!({
            "createRequest": {
                "requestId": uuid::Uuid::new_v4().to_string(),
                "conferenceSolutionKey": {"type": "hangoutsMeet"}
            }
        });
    }
    body
}

fn outlook_event_body(request: &CalendarEventRequest) -> Value {
    let mut body = json!({
        "subject": request.subject,
        "body": {"contentType": "Text", "content": request.description},
        "start": {"dateTime": request.start.to_rfc3339(), "timeZone": request.timezone},
        "end": {"dateTime": request.end.to_rfc3339(), "timeZone": request.timezone},
        "attendees": request.attendees.iter().map(|email| json!({
            "emailAddress": {"address": email},
            "type": "required"
        })).collect::<Vec<_>>(),
    });
    if request.create_online_meeting {
        body["isOnlineMeeting"] = json!(true);
        body["onlineMeetingProvider"] = json!("teamsForBusiness");
    }
    body
}

async fn parse_response(
    provider: CalendarProvider,
    response: reqwest::Response,
    join_url: impl Fn(&Value) -> Option<String>,
) -> Result<CalendarEventResponse, String> {
    let status = response.status();
    let text = response.text().await.map_err(|error| error.to_string())?;
    if !status.is_success() {
        return Err(format!(
            "calendar provider returned {status}: {}",
            text.chars().take(300).collect::<String>()
        ));
    }
    let value: Value = serde_json::from_str(&text).map_err(|error| error.to_string())?;
    let event_id = value
        .get("id")
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| "calendar response missing id".to_string())?;
    Ok(CalendarEventResponse {
        provider,
        event_id,
        join_url: join_url(&value),
        raw: value,
    })
}

async fn google_token(
    config: &GoogleCalendarConfig,
    http: &reqwest::Client,
) -> Result<String, String> {
    if let Some(token) = &config.access_token {
        return Ok(token.clone());
    }
    if let Some(cache) = config.cache.lock().expect("token cache").as_ref() {
        if cache.expires_at > Instant::now() {
            return Ok(cache.token.clone());
        }
    }
    let (Some(client_id), Some(client_secret), Some(refresh_token)) = (
        config.client_id.as_deref(),
        config.client_secret.as_deref(),
        config.refresh_token.as_deref(),
    ) else {
        return Err("Google Calendar credentials are incomplete".to_string());
    };
    let form = serde_urlencoded::to_string([
        ("client_id", client_id),
        ("client_secret", client_secret),
        ("refresh_token", refresh_token),
        ("grant_type", "refresh_token"),
    ])
    .unwrap_or_default();
    let response = http
        .post(&config.token_url)
        .header(
            axum::http::header::CONTENT_TYPE,
            "application/x-www-form-urlencoded",
        )
        .body(form)
        .send()
        .await
        .map_err(|error| error.to_string())?;
    if !response.status().is_success() {
        return Err(format!(
            "Google token refresh returned {}",
            response.status()
        ));
    }
    let value: Value = response.json().await.map_err(|error| error.to_string())?;
    let token = value
        .get("access_token")
        .and_then(Value::as_str)
        .ok_or("Google token response missing access_token")?
        .to_string();
    let expires = value
        .get("expires_in")
        .and_then(Value::as_u64)
        .unwrap_or(3600);
    *config.cache.lock().expect("token cache") = Some(TokenCache {
        token: token.clone(),
        expires_at: Instant::now() + Duration::from_secs(expires.saturating_sub(60)),
    });
    Ok(token)
}

async fn outlook_token(
    config: &OutlookCalendarConfig,
    http: &reqwest::Client,
) -> Result<String, String> {
    if let Some(token) = &config.access_token {
        return Ok(token.clone());
    }
    if let Some(cache) = config.cache.lock().expect("token cache").as_ref() {
        if cache.expires_at > Instant::now() {
            return Ok(cache.token.clone());
        }
    }
    let (Some(client_id), Some(client_secret), Some(refresh_token)) = (
        config.client_id.as_deref(),
        config.client_secret.as_deref(),
        config.refresh_token.as_deref(),
    ) else {
        return Err("Microsoft Graph credentials are incomplete".to_string());
    };
    let token_url = config.token_url.clone().unwrap_or_else(|| {
        format!(
            "https://login.microsoftonline.com/{}/oauth2/v2.0/token",
            config.tenant_id
        )
    });
    let form = serde_urlencoded::to_string([
        ("client_id", client_id),
        ("client_secret", client_secret),
        ("refresh_token", refresh_token),
        ("grant_type", "refresh_token"),
        ("scope", "https://graph.microsoft.com/.default"),
    ])
    .unwrap_or_default();
    let response = http
        .post(token_url)
        .header(
            axum::http::header::CONTENT_TYPE,
            "application/x-www-form-urlencoded",
        )
        .body(form)
        .send()
        .await
        .map_err(|error| error.to_string())?;
    if !response.status().is_success() {
        return Err(format!(
            "Microsoft token refresh returned {}",
            response.status()
        ));
    }
    let value: Value = response.json().await.map_err(|error| error.to_string())?;
    let token = value
        .get("access_token")
        .and_then(Value::as_str)
        .ok_or("Microsoft token response missing access_token")?
        .to_string();
    let expires = value
        .get("expires_in")
        .and_then(Value::as_u64)
        .unwrap_or(3600);
    *config.cache.lock().expect("token cache") = Some(TokenCache {
        token: token.clone(),
        expires_at: Instant::now() + Duration::from_secs(expires.saturating_sub(60)),
    });
    Ok(token)
}

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/calendars/events", post(create_event))
        .route("/calendars/providers", get(providers))
}

fn admin(
    state: &AppState,
    headers: &HeaderMap,
    query: &std::collections::HashMap<String, String>,
) -> bool {
    authorized(headers, query, &state.api_key)
}

async fn providers(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
) -> Response {
    if !admin(&state, &headers, &query) {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    let Some(calendar) = &state.calendar else {
        return Json(json!({"providers": []})).into_response();
    };
    Json(json!({"providers": calendar.providers()})).into_response()
}

async fn create_event(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
    Json(request): Json<CalendarEventRequest>,
) -> Response {
    if !admin(&state, &headers, &query) {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    let Some(calendar) = &state.calendar else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "calendar providers are not configured"})),
        )
            .into_response();
    };
    match calendar.create_event(&request).await {
        Ok(event) => (StatusCode::CREATED, Json(event)).into_response(),
        Err(error) => {
            warn!(%error, "calendar event creation failed");
            (StatusCode::BAD_GATEWAY, Json(json!({"error": error}))).into_response()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(provider: CalendarProvider) -> CalendarEventRequest {
        CalendarEventRequest {
            provider,
            subject: "Interview".to_string(),
            description: "Senior backend interview".to_string(),
            start: "2026-10-26T10:00:00Z".parse().unwrap(),
            end: "2026-10-26T11:00:00Z".parse().unwrap(),
            timezone: "UTC".to_string(),
            attendees: vec!["candidate@example.com".to_string()],
            create_online_meeting: true,
            calendar_id: None,
            user_id: None,
        }
    }

    #[test]
    fn google_body_contains_meet_and_attendees() {
        let body = google_event_body(&request(CalendarProvider::Google));
        assert_eq!(body["summary"], "Interview");
        assert_eq!(body["attendees"][0]["email"], "candidate@example.com");
        assert_eq!(
            body["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"],
            "hangoutsMeet"
        );
    }

    #[test]
    fn outlook_body_requests_teams_meeting() {
        let body = outlook_event_body(&request(CalendarProvider::Outlook));
        assert_eq!(body["isOnlineMeeting"], true);
        assert_eq!(body["onlineMeetingProvider"], "teamsForBusiness");
        assert_eq!(
            body["attendees"][0]["emailAddress"]["address"],
            "candidate@example.com"
        );
    }

    #[test]
    fn rejects_invalid_time_range() {
        let mut req = request(CalendarProvider::Google);
        req.end = req.start;
        let client = CalendarClient {
            http: reqwest::Client::new(),
            google: Some(GoogleCalendarConfig {
                access_token: Some("token".to_string()),
                client_id: None,
                client_secret: None,
                refresh_token: None,
                base_url: "https://example.test".to_string(),
                token_url: "https://example.test/token".to_string(),
                calendar_id: "primary".to_string(),
                cache: Mutex::new(None),
            }),
            outlook: None,
        };
        assert!(tokio::runtime::Runtime::new()
            .unwrap()
            .block_on(client.create_event(&req))
            .is_err());
    }
}
