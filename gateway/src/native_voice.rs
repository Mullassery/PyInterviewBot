//! First-class native voice endpoints.
//!
//! These routes expose the in-repo Rust speech studio and optional managed-call
//! workflow. They are not adapters to an external desktop application.

use std::collections::HashMap;

use axum::{
    body::{Body, Bytes},
    extract::{DefaultBodyLimit, Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Json, Router,
};
use futures_util::stream;
use serde::Deserialize;
use serde_json::{json, Value};
use std::convert::Infallible;
use uuid::Uuid;

use voice_runtime::{
    AudioBuffer, AudioFormat, ManagedCallError, ProviderEvent, StudioError, StudioSessionId,
};

use crate::ws_handler::{authorized, AppState};

const MAX_BODY_BYTES: usize = 10 * 1024 * 1024;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/native/voice/health", get(health))
        .route("/native/voice/capabilities", get(capabilities))
        .route("/native/voice/engines", get(engines))
        .route("/native/voice/sessions", post(create_session))
        .route("/native/voice/sessions/{id}/speak", post(speak))
        .route(
            "/native/voice/sessions/{id}/speak/stream",
            post(speak_stream),
        )
        .route(
            "/native/voice/sessions/{id}/transcribe",
            post(transcribe).layer(DefaultBodyLimit::max(MAX_BODY_BYTES)),
        )
        .route("/native/voice/sessions/{id}/interrupt", post(interrupt))
        .route(
            "/native/voice/sessions/{id}/record",
            post(record).layer(DefaultBodyLimit::max(MAX_BODY_BYTES)),
        )
        .route(
            "/native/voice/sessions/{id}/recording.wav",
            get(download_recording),
        )
        .route(
            "/native/voice/sessions/{id}/recording",
            delete(clear_recording),
        )
        .route("/native/voice/sessions/{id}", delete(close_session))
        .route("/native/managed-call/status", get(managed_status))
        .route("/native/managed-call/calls", post(create_call))
        .route(
            "/native/managed-call/sessions/{id}/events",
            get(drain_managed_events),
        )
        .route(
            "/native/managed-call/webhook",
            post(managed_webhook).layer(DefaultBodyLimit::max(MAX_BODY_BYTES)),
        )
}

fn check_auth(
    state: &AppState,
    headers: &HeaderMap,
    query: &HashMap<String, String>,
) -> Option<Response> {
    if authorized(headers, query, &state.api_key) {
        None
    } else {
        Some(StatusCode::UNAUTHORIZED.into_response())
    }
}

fn studio_error(error: StudioError) -> Response {
    let status = match &error {
        StudioError::SessionNotFound => StatusCode::NOT_FOUND,
        StudioError::SessionClosed | StudioError::Cancelled => StatusCode::CONFLICT,
        StudioError::InvalidText
        | StudioError::InvalidAudio(_)
        | StudioError::UnsupportedFormat(_) => StatusCode::BAD_REQUEST,
        StudioError::TextTooLarge | StudioError::AudioTooLarge => StatusCode::PAYLOAD_TOO_LARGE,
        StudioError::CapacityExceeded => StatusCode::TOO_MANY_REQUESTS,
        StudioError::EngineUnavailable(_) => StatusCode::SERVICE_UNAVAILABLE,
        StudioError::Timeout => StatusCode::GATEWAY_TIMEOUT,
        StudioError::EngineInternal(_) => StatusCode::INTERNAL_SERVER_ERROR,
    };
    (
        status,
        Json(json!({
            "error": error.to_string(),
            "code": studio_error_code(&error),
        })),
    )
        .into_response()
}

fn studio_error_code(error: &StudioError) -> &'static str {
    match error {
        StudioError::UnsupportedFormat(_) => "unsupported_format",
        StudioError::InvalidAudio(_) => "invalid_audio",
        StudioError::SessionNotFound => "session_not_found",
        StudioError::SessionClosed => "session_closed",
        StudioError::InvalidText => "invalid_text",
        StudioError::TextTooLarge => "text_too_large",
        StudioError::AudioTooLarge => "audio_too_large",
        StudioError::Cancelled => "cancelled",
        StudioError::CapacityExceeded => "capacity_exceeded",
        StudioError::EngineUnavailable(_) => "engine_unavailable",
        StudioError::Timeout => "engine_timeout",
        StudioError::EngineInternal(_) => "engine_internal",
    }
}

fn managed_error(error: ManagedCallError) -> Response {
    let status = match &error {
        ManagedCallError::NotConfigured(_) => StatusCode::SERVICE_UNAVAILABLE,
        ManagedCallError::InvalidWebhook(_) => StatusCode::UNAUTHORIZED,
        ManagedCallError::PayloadTooLarge => StatusCode::PAYLOAD_TOO_LARGE,
        ManagedCallError::InvalidPayload(_) => StatusCode::BAD_REQUEST,
        ManagedCallError::Request(_) => StatusCode::BAD_GATEWAY,
        ManagedCallError::Status { status, .. } => {
            StatusCode::from_u16(*status).unwrap_or(StatusCode::BAD_GATEWAY)
        }
    };
    (
        status,
        Json(json!({
            "error": error.to_string(),
            "retryable": error.retryable(),
        })),
    )
        .into_response()
}

async fn health(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    Json(json!({
        "healthy": state.studio.health().await,
        "active_sessions": state.studio.active_sessions().await,
        "managed_call_configured": state.managed_call.is_some(),
    }))
    .into_response()
}

async fn capabilities(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    Json(state.studio.capabilities()).into_response()
}

async fn engines(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    Json(state.engines.describe().await).into_response()
}

async fn create_session(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    match state.studio.start_session().await {
        Ok(session_id) => Json(json!({ "session_id": session_id })).into_response(),
        Err(error) => studio_error(error),
    }
}

#[derive(Debug, Deserialize)]
struct SpeakRequest {
    text: String,
    #[serde(default = "default_voice")]
    voice: String,
}

fn default_voice() -> String {
    "default".to_string()
}

async fn speak(
    State(state): State<AppState>,
    Path(session_id): Path<Uuid>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
    Json(request): Json<SpeakRequest>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    match state
        .studio
        .speak(session_id, &request.text, &request.voice)
        .await
    {
        Ok(audio) => audio_response(&audio),
        Err(error) => studio_error(error),
    }
}

fn audio_response(audio: &AudioBuffer) -> Response {
    let mut headers = HeaderMap::new();
    headers.insert(
        "content-type",
        format!(
            "audio/L16;rate={};channels={}",
            audio.format.sample_rate, audio.format.channels
        )
        .parse()
        .unwrap_or_else(|_| "application/octet-stream".parse().expect("static header")),
    );
    headers.insert(
        "x-sample-rate",
        audio
            .format
            .sample_rate
            .to_string()
            .parse()
            .expect("numeric header"),
    );
    (headers, audio.pcm16.clone()).into_response()
}

async fn speak_stream(
    State(state): State<AppState>,
    Path(session_id): Path<Uuid>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
    Json(request): Json<SpeakRequest>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    match state
        .studio
        .speak_stream(session_id, &request.text, &request.voice)
        .await
    {
        Ok(chunks) => {
            let sample_rate = chunks
                .first()
                .map(|chunk| chunk.format.sample_rate)
                .unwrap_or(22_050);
            let mut response_headers = HeaderMap::new();
            response_headers.insert(
                "content-type",
                format!("audio/L16;rate={sample_rate};channels=1")
                    .parse()
                    .expect("static header"),
            );
            response_headers.insert(
                "x-sample-rate",
                sample_rate.to_string().parse().expect("numeric header"),
            );
            let body = Body::from_stream(stream::iter(
                chunks
                    .into_iter()
                    .map(|chunk| Ok::<Vec<u8>, Infallible>(chunk.pcm16)),
            ));
            (response_headers, body).into_response()
        }
        Err(error) => studio_error(error),
    }
}

async fn transcribe(
    State(state): State<AppState>,
    Path(session_id): Path<Uuid>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
    body: Bytes,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    let format = AudioFormat::pcm16(query_sample_rate(&query));
    let audio = match AudioBuffer::new(format, body.to_vec()) {
        Ok(audio) => audio,
        Err(error) => return studio_error(error),
    };
    let language = query.get("language").map(String::as_str).unwrap_or("en");
    match state.studio.transcribe(session_id, &audio, language).await {
        Ok(text) => Json(json!({ "transcript": text })).into_response(),
        Err(error) => studio_error(error),
    }
}

fn query_sample_rate(query: &HashMap<String, String>) -> u32 {
    query
        .get("sample_rate")
        .and_then(|value| value.parse().ok())
        .unwrap_or(16_000)
}

async fn interrupt(
    State(state): State<AppState>,
    Path(session_id): Path<Uuid>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    match state.studio.interrupt(session_id).await {
        Ok(interrupted) => Json(json!({ "interrupted": interrupted })).into_response(),
        Err(error) => studio_error(error),
    }
}

async fn record(
    State(state): State<AppState>,
    Path(session_id): Path<Uuid>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
    body: Bytes,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    let format = AudioFormat::pcm16(query_sample_rate(&query));
    let audio = match AudioBuffer::new(format, body.to_vec()) {
        Ok(audio) => audio,
        Err(error) => return studio_error(error),
    };
    match state.studio.record_audio(session_id, &audio).await {
        Ok(total_bytes) => Json(json!({ "total_bytes": total_bytes })).into_response(),
        Err(error) => studio_error(error),
    }
}

async fn download_recording(
    State(state): State<AppState>,
    Path(session_id): Path<Uuid>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    match state.studio.recording_wav(session_id).await {
        Ok(audio) => wav_response(&audio.to_wav()),
        Err(error) => studio_error(error),
    }
}

async fn clear_recording(
    State(state): State<AppState>,
    Path(session_id): Path<Uuid>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    match state.studio.clear_recording(session_id).await {
        Ok(()) => Json(json!({ "cleared": true, "session_id": session_id })).into_response(),
        Err(error) => studio_error(error),
    }
}

fn wav_response(wav: &[u8]) -> Response {
    let mut headers = HeaderMap::new();
    headers.insert("content-type", "audio/wav".parse().expect("static header"));
    headers.insert(
        "content-disposition",
        "inline; filename=\"recording.wav\""
            .parse()
            .expect("static header"),
    );
    (headers, wav.to_vec()).into_response()
}

async fn close_session(
    State(state): State<AppState>,
    Path(session_id): Path<Uuid>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    match state.studio.end_session(session_id).await {
        Ok(()) => Json(json!({ "closed": true, "session_id": session_id })).into_response(),
        Err(error) => studio_error(error),
    }
}

async fn managed_status(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    Json(json!({
        "configured": state.managed_call.is_some(),
        "interview_state_owned_by_pyinterviewbot": true,
    }))
    .into_response()
}

#[derive(Debug, Deserialize)]
struct CreateManagedCallRequest {
    session_id: Uuid,
    payload: Value,
}

async fn create_call(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
    Json(request): Json<CreateManagedCallRequest>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    let Some(workflow) = &state.managed_call else {
        return managed_error(ManagedCallError::NotConfigured(
            "MANAGED_CALL_API_KEY and MANAGED_CALL_BASE_URL are required".to_string(),
        ));
    };
    match workflow
        .start_call(request.session_id, request.payload)
        .await
    {
        Ok(call) => (
            StatusCode::CREATED,
            Json(json!({ "call_id": call.id, "status": call.status })),
        )
            .into_response(),
        Err(error) => managed_error(error),
    }
}

async fn drain_managed_events(
    State(state): State<AppState>,
    Path(session_id): Path<StudioSessionId>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    let Some(workflow) = &state.managed_call else {
        return managed_error(ManagedCallError::NotConfigured(
            "managed-call workflow is not configured".to_string(),
        ));
    };
    let events: Vec<ProviderEvent> = workflow.drain_provider_events(session_id);
    Json(json!({ "events": events })).into_response()
}

async fn managed_webhook(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let Some(workflow) = &state.managed_call else {
        return managed_error(ManagedCallError::NotConfigured(
            "managed-call workflow is not configured".to_string(),
        ));
    };
    match workflow.ingest_webhook(&headers, &body) {
        Ok(events) => Json(json!({ "events": events })).into_response(),
        Err(error) => managed_error(error),
    }
}
