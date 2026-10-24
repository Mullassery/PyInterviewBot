//! Twilio Programmable Voice adapter.
//!
//! Inbound and turn webhooks are authenticated with Twilio's documented
//! X-Twilio-Signature HMAC-SHA1 scheme. Interview business logic remains in the
//! channel-neutral ai-service API.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use axum::{
    body::Bytes,
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        DefaultBodyLimit, Query, State,
    },
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use base64::{engine::general_purpose::STANDARD, Engine};
use futures_util::{SinkExt, StreamExt};
use hmac::{Hmac, Mac};
use serde::Deserialize;
use serde_json::{json, Value};
use sha1::{Digest, Sha1};
use tracing::{info, warn};

use crate::ws_handler::{authorized, AppState};

const CHANNEL: &str = "twilio_voice";
const MAX_BODY_BYTES: usize = 256 * 1024;

#[derive(Clone)]
pub struct TwilioConfig {
    pub auth_token: String,
    pub account_sid: Option<String>,
    pub from_number: Option<String>,
    pub public_base_url: String,
    pub media_ws_base_url: String,
    pub api_base_url: String,
    pub http: reqwest::Client,
}

impl TwilioConfig {
    pub fn from_env() -> Option<Self> {
        let auth_token = std::env::var("TWILIO_AUTH_TOKEN").ok()?.trim().to_string();
        let public_base_url = std::env::var("TWILIO_PUBLIC_BASE_URL")
            .ok()?
            .trim()
            .trim_end_matches('/')
            .to_string();
        if auth_token.is_empty() || public_base_url.is_empty() {
            return None;
        }
        let media_ws_base_url = std::env::var("TWILIO_MEDIA_WS_BASE_URL")
            .unwrap_or_else(|_| {
                if let Some(rest) = public_base_url.strip_prefix("https://") {
                    format!("wss://{rest}")
                } else if let Some(rest) = public_base_url.strip_prefix("http://") {
                    format!("ws://{rest}")
                } else {
                    public_base_url.clone()
                }
            })
            .trim_end_matches('/')
            .to_string();
        Some(Self {
            auth_token,
            account_sid: std::env::var("TWILIO_ACCOUNT_SID")
                .ok()
                .filter(|value| !value.is_empty()),
            from_number: std::env::var("TWILIO_FROM_NUMBER")
                .ok()
                .filter(|value| !value.is_empty()),
            public_base_url,
            media_ws_base_url,
            api_base_url: std::env::var("TWILIO_API_BASE_URL")
                .unwrap_or_else(|_| "https://api.twilio.com".to_string()),
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(20))
                .build()
                .expect("twilio http client"),
        })
    }

    fn canonical_url(&self, path_and_query: &str) -> String {
        format!("{}{}", self.public_base_url, path_and_query)
    }

    fn signature(&self, url: &str, params: &[(String, String)]) -> String {
        let mut sorted = params.to_vec();
        sorted.sort_by(|left, right| left.0.cmp(&right.0));
        let mut mac = Hmac::<Sha1>::new_from_slice(self.auth_token.as_bytes())
            .expect("HMAC accepts any key length");
        mac.update(url.as_bytes());
        for (key, value) in sorted {
            mac.update(key.as_bytes());
            mac.update(value.as_bytes());
        }
        STANDARD.encode(mac.finalize().into_bytes())
    }

    fn verify(&self, headers: &HeaderMap, url: &str, params: &[(String, String)]) -> bool {
        let Some(supplied) = headers
            .get("x-twilio-signature")
            .and_then(|value| value.to_str().ok())
        else {
            return false;
        };
        let expected = self.signature(url, params);
        supplied.len() == expected.len()
            && supplied
                .as_bytes()
                .iter()
                .zip(expected.as_bytes())
                .fold(0u8, |acc, (a, b)| acc | (a ^ b))
                == 0
    }

    fn start_token(&self, external_id: &str) -> String {
        let mut mac = Hmac::<Sha1>::new_from_slice(self.auth_token.as_bytes())
            .expect("HMAC accepts any key length");
        mac.update(b"twilio-start:");
        mac.update(external_id.as_bytes());
        format!("{:x}", mac.finalize().into_bytes())
    }

    fn action_url(&self, external_id: &str) -> String {
        format!(
            "{}/channels/twilio/voice/turn?external_id={}",
            self.public_base_url,
            urlencoding::encode(external_id)
        )
    }

    fn start_url(&self, external_id: &str) -> String {
        format!(
            "{}/channels/twilio/voice/start?external_id={}&token={}",
            self.public_base_url,
            urlencoding::encode(external_id),
            self.start_token(external_id)
        )
    }
}

fn parse_form(body: &[u8]) -> Vec<(String, String)> {
    serde_urlencoded::from_bytes(body).unwrap_or_default()
}

fn param(params: &[(String, String)], key: &str) -> String {
    params
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value.clone())
        .unwrap_or_default()
}

fn escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

fn twiml(body: &str) -> Response {
    (
        [(axum::http::header::CONTENT_TYPE, "text/xml; charset=utf-8")],
        format!("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response>{body}</Response>"),
    )
        .into_response()
}

fn ask_twiml(config: &TwilioConfig, external_id: &str, text: &str) -> Response {
    let action = escape(&config.action_url(external_id));
    twiml(&format!(
        "<Say voice=\"alice\">{}</Say><Gather input=\"speech\" speechTimeout=\"auto\" action=\"{action}\"><Say>I did not catch that. Please answer after the tone.</Say></Gather><Redirect>{action}</Redirect>",
        escape(text)
    ))
}

fn finish_twiml(text: &str) -> Response {
    twiml(&format!(
        "<Say voice=\"alice\">{}</Say><Hangup/>",
        escape(text)
    ))
}

fn unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"error": "Twilio adapter is not configured"})),
    )
        .into_response()
}

fn twilio_for(state: &AppState) -> Option<Arc<TwilioConfig>> {
    state.twilio.clone()
}

fn verify_request(
    config: &TwilioConfig,
    state: &AppState,
    headers: &HeaderMap,
    path_and_query: &str,
    body: &[u8],
) -> Option<Vec<(String, String)>> {
    let params = parse_form(body);
    let url = config.canonical_url(path_and_query);
    if !config.verify(headers, &url, &params) {
        warn!(path = path_and_query, "invalid Twilio signature");
        return None;
    }
    let _ = state;
    Some(params)
}

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/channels/twilio/voice/inbound", post(inbound))
        .route("/channels/twilio/voice/turn", post(turn))
        .route("/channels/twilio/voice/status", post(status))
        .route("/channels/twilio/voice/start", post(start_call_twiml))
        .route("/channels/twilio/voice/stream", post(stream_twiml))
        .route("/channels/twilio/voice/media", get(media_upgrade))
        .route("/channels/twilio/voice/calls", post(create_call))
        .layer(DefaultBodyLimit::max(MAX_BODY_BYTES))
}

async fn inbound(
    State(state): State<AppState>,
    headers: HeaderMap,
    uri: axum::http::Uri,
    body: Bytes,
) -> Response {
    let Some(config) = twilio_for(&state) else {
        return unavailable();
    };
    let Some(params) = verify_request(
        &config,
        &state,
        &headers,
        uri.path_and_query()
            .map(|value| value.as_str())
            .unwrap_or(uri.path()),
        &body,
    ) else {
        return StatusCode::FORBIDDEN.into_response();
    };
    let external_id = param(&params, "From");
    if external_id.is_empty() {
        return twiml("<Say>We could not identify this call.</Say><Hangup/>");
    }
    match state
        .ai
        .start_channel_session(CHANNEL, &external_id, None)
        .await
    {
        Ok(session) => {
            info!(
                session_id = %session.session_id,
                template = %session.template_id,
                "Twilio interview started"
            );
            ask_twiml(&config, &external_id, &session.question_text)
        }
        Err(error) => {
            warn!(?error, "failed to start Twilio interview session");
            twiml("<Say>We are having trouble starting the interview. Please try again later.</Say><Hangup/>")
        }
    }
}

async fn turn(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
    uri: axum::http::Uri,
    body: Bytes,
) -> Response {
    let Some(config) = twilio_for(&state) else {
        return unavailable();
    };
    let Some(params) = verify_request(
        &config,
        &state,
        &headers,
        uri.path_and_query()
            .map(|value| value.as_str())
            .unwrap_or(uri.path()),
        &body,
    ) else {
        return StatusCode::FORBIDDEN.into_response();
    };
    let Some(external_id) = query.get("external_id").filter(|value| !value.is_empty()) else {
        return twiml("<Say>This interview link is invalid.</Say><Hangup/>");
    };
    let call_sid = param(&params, "CallSid");
    let text = param(&params, "SpeechResult");
    if text.trim().is_empty() {
        return ask_twiml(
            &config,
            external_id,
            "Sorry, I did not catch that. Could you say that again?",
        );
    }
    let mut hasher = Sha1::new();
    hasher.update(text.as_bytes());
    let event_id = format!("{call_sid}:{:x}", hasher.finalize());
    match state
        .ai
        .channel_message(CHANNEL, external_id, &event_id, &text)
        .await
    {
        Ok(response) if response.is_complete => finish_twiml(&response.ai_text),
        Ok(response) => {
            info!(
                duplicate = response.duplicate,
                "Twilio interview turn accepted"
            );
            ask_twiml(&config, external_id, &response.ai_text)
        }
        Err(error) => {
            warn!(?error, "Twilio interview turn failed");
            twiml("<Say>We are having trouble continuing the interview. Please try again later.</Say><Hangup/>")
        }
    }
}

async fn status(
    State(state): State<AppState>,
    headers: HeaderMap,
    uri: axum::http::Uri,
    body: Bytes,
) -> Response {
    let Some(config) = twilio_for(&state) else {
        return unavailable();
    };
    let Some(params) = verify_request(
        &config,
        &state,
        &headers,
        uri.path_and_query()
            .map(|value| value.as_str())
            .unwrap_or(uri.path()),
        &body,
    ) else {
        return StatusCode::FORBIDDEN.into_response();
    };
    info!(
        call_sid = %param(&params, "CallSid"),
        status = %param(&params, "CallStatus"),
        "Twilio call status"
    );
    StatusCode::NO_CONTENT.into_response()
}

async fn start_call_twiml(
    State(state): State<AppState>,
    Query(query): Query<HashMap<String, String>>,
) -> Response {
    let Some(config) = twilio_for(&state) else {
        return unavailable();
    };
    let Some(external_id) = query.get("external_id").filter(|value| !value.is_empty()) else {
        return twiml("<Say>This interview link is invalid.</Say><Hangup/>");
    };
    let Some(token) = query.get("token") else {
        return StatusCode::FORBIDDEN.into_response();
    };
    let expected = config.start_token(external_id);
    if token.len() != expected.len()
        || token
            .as_bytes()
            .iter()
            .zip(expected.as_bytes())
            .fold(0u8, |acc, (a, b)| acc | (a ^ b))
            != 0
    {
        return StatusCode::FORBIDDEN.into_response();
    }
    match state
        .ai
        .start_channel_session(CHANNEL, external_id, None)
        .await
    {
        Ok(session) => {
            info!(
                session_id = %session.session_id,
                template = %session.template_id,
                "Twilio outbound interview started"
            );
            ask_twiml(&config, external_id, &session.question_text)
        }
        Err(error) => {
            warn!(?error, "failed to start outbound Twilio interview");
            twiml("<Say>We are having trouble starting the interview. Please try again later.</Say><Hangup/>")
        }
    }
}

#[derive(Debug, Deserialize)]
struct CreateCallRequest {
    to: String,
    external_id: String,
    template_id: Option<String>,
}

async fn create_call(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
    Json(request): Json<CreateCallRequest>,
) -> Response {
    if !authorized(&headers, &query, &state.api_key) {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    let Some(config) = twilio_for(&state) else {
        return unavailable();
    };
    let (Some(account_sid), Some(from_number)) =
        (config.account_sid.as_deref(), config.from_number.as_deref())
    else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "TWILIO_ACCOUNT_SID and TWILIO_FROM_NUMBER are required"})),
        )
            .into_response();
    };
    if request.to.trim().is_empty() || request.external_id.trim().is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "to and external_id are required"})),
        )
            .into_response();
    }
    let _ = request.template_id;
    let url = config.start_url(&request.external_id);
    let status_callback = format!("{}/channels/twilio/voice/status", config.public_base_url);
    let endpoint = format!(
        "{}/2010-04-01/Accounts/{}/Calls.json",
        config.api_base_url.trim_end_matches('/'),
        account_sid
    );
    let form = serde_urlencoded::to_string([
        ("To", request.to.as_str()),
        ("From", from_number),
        ("Url", url.as_str()),
        ("StatusCallback", status_callback.as_str()),
        ("StatusCallbackEvent", "initiated completed"),
    ])
    .unwrap_or_default();
    let response = config
        .http
        .post(endpoint)
        .basic_auth(account_sid, Some(&config.auth_token))
        .header(
            axum::http::header::CONTENT_TYPE,
            "application/x-www-form-urlencoded",
        )
        .body(form)
        .send()
        .await;
    match response {
        Ok(response) if response.status().is_success() => match response.json::<Value>().await {
            Ok(value) => (
                StatusCode::CREATED,
                Json(json!({"call_sid": value.get("sid").cloned()})),
            )
                .into_response(),
            Err(error) => (
                StatusCode::BAD_GATEWAY,
                Json(json!({"error": error.to_string()})),
            )
                .into_response(),
        },
        Ok(response) => {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            (
                StatusCode::BAD_GATEWAY,
                Json(json!({"error": "Twilio call creation failed", "status": status.as_u16(), "body": body.chars().take(300).collect::<String>()})),
            )
                .into_response()
        }
        Err(error) => (
            StatusCode::BAD_GATEWAY,
            Json(json!({"error": error.to_string()})),
        )
            .into_response(),
    }
}

fn stream_twiml_for(config: &TwilioConfig, external_id: &str) -> Response {
    let url = format!(
        "{}/channels/twilio/voice/media?external_id={}&token={}",
        config.media_ws_base_url,
        urlencoding::encode(external_id),
        config.start_token(external_id)
    );
    twiml(&format!(
        "<Connect><Stream url=\"{}\" /></Connect>",
        escape(&url)
    ))
}

async fn stream_twiml(
    State(state): State<AppState>,
    headers: HeaderMap,
    uri: axum::http::Uri,
    body: Bytes,
) -> Response {
    let Some(config) = twilio_for(&state) else {
        return unavailable();
    };
    let Some(params) = verify_request(
        &config,
        &state,
        &headers,
        uri.path_and_query()
            .map(|value| value.as_str())
            .unwrap_or(uri.path()),
        &body,
    ) else {
        return StatusCode::FORBIDDEN.into_response();
    };
    let external_id = param(&params, "From");
    if external_id.is_empty() {
        return twiml("<Say>We could not identify this call.</Say><Hangup/>");
    }
    stream_twiml_for(&config, &external_id)
}

async fn media_upgrade(
    ws: WebSocketUpgrade,
    State(state): State<AppState>,
    Query(query): Query<HashMap<String, String>>,
) -> Response {
    let Some(config) = twilio_for(&state) else {
        return unavailable();
    };
    let Some(external_id) = query.get("external_id").filter(|value| !value.is_empty()) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let Some(token) = query.get("token") else {
        return StatusCode::FORBIDDEN.into_response();
    };
    let expected = config.start_token(external_id);
    if token.len() != expected.len()
        || token
            .as_bytes()
            .iter()
            .zip(expected.as_bytes())
            .fold(0u8, |acc, (a, b)| acc | (a ^ b))
            != 0
    {
        return StatusCode::FORBIDDEN.into_response();
    }
    let external_id = external_id.clone();
    ws.max_message_size(1024 * 1024)
        .on_upgrade(move |socket| media_socket(socket, state, config, external_id))
        .into_response()
}

fn pcm16_to_ulaw(pcm: &[u8]) -> Vec<u8> {
    pcm.as_chunks::<2>()
        .0
        .iter()
        .map(|chunk| {
            let sample = i16::from_le_bytes(*chunk);
            linear_to_ulaw(sample)
        })
        .collect()
}

fn ulaw_to_pcm16(ulaw: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(ulaw.len() * 2);
    for byte in ulaw {
        out.extend_from_slice(&ulaw_to_linear(*byte).to_le_bytes());
    }
    out
}

fn linear_to_ulaw(sample: i16) -> u8 {
    const BIAS: i32 = 0x84;
    const CLIP: i32 = 32635;
    let mut sample = sample as i32;
    let sign = if sample < 0 {
        sample = -sample;
        0x80
    } else {
        0
    };
    let sample = sample.min(CLIP) + BIAS;
    let exponent = 7 - (sample | 0xff).leading_zeros() as i32;
    let exponent = exponent.clamp(0, 7);
    let mantissa = (sample >> (exponent + 3)) & 0x0f;
    ((sign | (exponent << 4) | mantissa) ^ 0xff) as u8
}

fn ulaw_to_linear(byte: u8) -> i16 {
    let byte = !byte;
    let sign = byte & 0x80;
    let exponent = ((byte >> 4) & 0x07) as i32;
    let mantissa = (byte & 0x0f) as i32;
    let sample = ((mantissa << 3) + 0x84) << exponent;
    let sample = sample - 0x84;
    if sign != 0 {
        (-sample) as i16
    } else {
        sample as i16
    }
}

fn media_message(stream_sid: &str, payload: &str) -> String {
    json!({
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": payload},
    })
    .to_string()
}

async fn send_pcm_as_ulaw(
    sink: &mut futures_util::stream::SplitSink<WebSocket, Message>,
    stream_sid: &str,
    pcm16_8khz: &[u8],
) -> bool {
    let ulaw = pcm16_to_ulaw(pcm16_8khz);
    for chunk in ulaw.chunks(160) {
        let payload = STANDARD.encode(chunk);
        if sink
            .send(Message::Text(media_message(stream_sid, &payload).into()))
            .await
            .is_err()
        {
            return false;
        }
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    true
}

async fn speak_text(
    state: &AppState,
    sink: &mut futures_util::stream::SplitSink<WebSocket, Message>,
    stream_sid: &str,
    text: &str,
) -> bool {
    let mut tts = match state.ai.synthesize_stream(text).await {
        Ok(stream) => stream,
        Err(error) => {
            warn!(?error, "Twilio media TTS failed");
            return false;
        }
    };
    let mut pcm = Vec::new();
    while let Some(chunk) = tts.next().await {
        match chunk {
            Ok(bytes) => pcm.extend_from_slice(&bytes),
            Err(error) => {
                warn!(?error, "Twilio media TTS stream failed");
                return false;
            }
        }
    }
    let pcm8 = voice_runtime::studio::resample_pcm16(&pcm, 22_050, 8_000);
    send_pcm_as_ulaw(sink, stream_sid, &pcm8).await
}

async fn media_socket(
    socket: WebSocket,
    state: AppState,
    _config: Arc<TwilioConfig>,
    external_id: String,
) {
    let (mut sink, mut stream) = socket.split();
    let mut stream_sid = String::new();
    let mut vad = crate::vad::TurnDetector::default();
    let mut utterance = Vec::new();

    while let Some(message) = stream.next().await {
        let Ok(Message::Text(text)) = message else {
            continue;
        };
        let Ok(value) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        match value.get("event").and_then(Value::as_str) {
            Some("start") => {
                stream_sid = value
                    .get("streamSid")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                match state
                    .ai
                    .start_channel_session(CHANNEL, &external_id, None)
                    .await
                {
                    Ok(session) => {
                        if !speak_text(&state, &mut sink, &stream_sid, &session.question_text).await
                        {
                            return;
                        }
                    }
                    Err(error) => {
                        warn!(?error, "Twilio media session start failed");
                        return;
                    }
                }
            }
            Some("media") => {
                let Some(payload) = value
                    .get("media")
                    .and_then(|media| media.get("payload"))
                    .and_then(Value::as_str)
                else {
                    continue;
                };
                let Ok(ulaw) = STANDARD.decode(payload) else {
                    continue;
                };
                let pcm8 = ulaw_to_pcm16(&ulaw);
                let pcm16 = voice_runtime::studio::resample_pcm16(&pcm8, 8_000, 16_000);
                let events = vad.push_pcm16_capturing(&pcm16, &mut utterance);
                if events.contains(&crate::vad::VadEvent::TurnEnded) {
                    let audio = std::mem::take(&mut utterance);
                    vad.reset_turn();
                    let transcript = match state.ai.transcribe(audio).await {
                        Ok(text) => text,
                        Err(error) => {
                            warn!(?error, "Twilio media transcription failed");
                            continue;
                        }
                    };
                    let mut hasher = Sha1::new();
                    hasher.update(transcript.as_bytes());
                    let event_id = format!("{external_id}:media:{:x}", hasher.finalize());
                    match state
                        .ai
                        .channel_message(CHANNEL, &external_id, &event_id, &transcript)
                        .await
                    {
                        Ok(response) => {
                            if !speak_text(&state, &mut sink, &stream_sid, &response.ai_text).await
                            {
                                return;
                            }
                            if response.is_complete {
                                let _ = sink.send(Message::Close(None)).await;
                                return;
                            }
                        }
                        Err(error) => {
                            warn!(?error, "Twilio media interview turn failed");
                            return;
                        }
                    }
                }
            }
            Some("stop") | Some("closed") => return,
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> TwilioConfig {
        TwilioConfig {
            auth_token: "12345".to_string(),
            account_sid: Some("AC123".to_string()),
            from_number: Some("+15550001111".to_string()),
            public_base_url: "https://example.test".to_string(),
            media_ws_base_url: "wss://example.test".to_string(),
            api_base_url: "https://api.twilio.test".to_string(),
            http: reqwest::Client::new(),
        }
    }

    #[test]
    fn signature_round_trip_and_tamper_rejection() {
        let config = config();
        let url = "https://example.test/channels/twilio/voice/inbound";
        let params = vec![
            ("CallSid".to_string(), "CA123".to_string()),
            ("From".to_string(), "+15551234567".to_string()),
        ];
        let signature = config.signature(url, &params);
        let mut headers = HeaderMap::new();
        headers.insert("x-twilio-signature", signature.parse().unwrap());
        assert!(config.verify(&headers, url, &params));
        let mut tampered = params.clone();
        tampered[1].1 = "+15550000000".to_string();
        assert!(!config.verify(&headers, url, &tampered));
    }

    #[test]
    fn parses_twilio_form_and_escapes_twiml() {
        let params = parse_form(b"CallSid=CA123&From=%2B15551234567&SpeechResult=hello+world");
        assert_eq!(param(&params, "CallSid"), "CA123");
        assert_eq!(param(&params, "From"), "+15551234567");
        assert_eq!(param(&params, "SpeechResult"), "hello world");
        assert_eq!(
            escape("<hello & 'world'>"),
            "&lt;hello &amp; &apos;world&apos;&gt;"
        );
    }

    #[test]
    fn ulaw_round_trip_is_bounded() {
        let samples: Vec<i16> = (-2000..=2000).step_by(40).collect();
        let mut pcm = Vec::new();
        for sample in &samples {
            pcm.extend_from_slice(&sample.to_le_bytes());
        }
        let ulaw = pcm16_to_ulaw(&pcm);
        let decoded = ulaw_to_pcm16(&ulaw);
        assert_eq!(decoded.len(), pcm.len());
        for chunk in decoded.as_chunks::<2>().0 {
            assert!(i16::from_le_bytes(*chunk).abs() <= 4000);
        }
    }

    #[test]
    fn start_token_is_deterministic_and_changes_with_identity() {
        let config = config();
        assert_eq!(config.start_token("a"), config.start_token("a"));
        assert_ne!(config.start_token("a"), config.start_token("b"));
    }
}
