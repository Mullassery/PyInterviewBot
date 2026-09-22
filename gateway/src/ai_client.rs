//! HTTP client for the ai-service (ASR / LLM agent / TTS / evidence engine).
//!
//! The gateway never talks to Ollama, faster-whisper, or Piper directly —
//! it only knows this contract. That keeps the real-time transport layer
//! independent of which AI providers are behind it (spec §31/§30).

use std::time::Duration;

use bytes::Bytes;
use futures_util::Stream;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// Upper bound on a single ai-service round trip (`start_session`,
/// `agent_turn`, `transcribe`, or `synthesize_stream`). Without this,
/// `reqwest::Client::new()` never times out, so an ai-service hang (e.g.
/// Ollama stalling) blocks the gateway's `tokio::select!` session loop
/// forever instead of erroring out into `speak_recovery()`'s fallback
/// phrase (see ws_handler.rs). 20s comfortably covers a full local LLM
/// turn (qwen2.5:7b-instruct generating a multi-sentence reply) plus ASR
/// transcription of a single candidate utterance, while still keeping a
/// hung dependency from stalling the candidate's session indefinitely --
/// generous relative to the VAD's own much shorter fixed-timing constants
/// (`SILENCE_MS_TO_END_TURN` = 700ms in vad.rs) since this bounds a whole
/// AI round trip, not a speech-detection heuristic.
const AI_SERVICE_TIMEOUT: Duration = Duration::from_secs(20);

#[derive(Debug, thiserror::Error)]
pub enum AiClientError {
    #[error("ai-service request failed: {0}")]
    Request(#[from] reqwest::Error),
    #[error("ai-service returned {status}: {body}")]
    Status {
        status: reqwest::StatusCode,
        body: String,
    },
}

#[derive(Clone)]
pub struct AiClient {
    http: reqwest::Client,
    base_url: String,
}

#[derive(Debug, Serialize)]
struct StartSessionRequest {
    resume_seed: bool,
}

#[derive(Debug, Deserialize, Clone)]
pub struct StartSessionResponse {
    pub session_id: Uuid,
    pub question_text: String,
}

#[derive(Debug, Serialize)]
struct TurnRequest {
    transcript: String,
}

#[derive(Debug, Deserialize, Clone)]
pub struct TurnResponse {
    pub ai_text: String,
    pub is_complete: bool,
}

#[derive(Debug, Deserialize)]
struct TranscribeResponse {
    transcript: String,
}

#[derive(Debug, Serialize)]
struct SynthesizeRequest {
    text: String,
}

impl AiClient {
    pub fn new(base_url: impl Into<String>) -> Self {
        Self::with_timeout(base_url, AI_SERVICE_TIMEOUT)
    }

    /// Split out from `new` so tests can use a short timeout instead of
    /// waiting out the real `AI_SERVICE_TIMEOUT` against a deliberately
    /// unresponsive server.
    fn with_timeout(base_url: impl Into<String>, timeout: Duration) -> Self {
        Self {
            http: reqwest::Client::builder()
                .timeout(timeout)
                .build()
                .expect("reqwest client with a fixed timeout should always build"),
            base_url: base_url.into(),
        }
    }

    pub async fn start_session(&self) -> Result<StartSessionResponse, AiClientError> {
        let resp = self
            .http
            .post(format!("{}/sessions", self.base_url))
            .json(&StartSessionRequest { resume_seed: true })
            .send()
            .await?;
        Ok(Self::check_status(resp).await?.json().await?)
    }

    pub async fn agent_turn(
        &self,
        session_id: Uuid,
        transcript: &str,
    ) -> Result<TurnResponse, AiClientError> {
        let resp = self
            .http
            .post(format!("{}/sessions/{}/turn", self.base_url, session_id))
            .json(&TurnRequest {
                transcript: transcript.to_string(),
            })
            .send()
            .await?;
        Ok(Self::check_status(resp).await?.json().await?)
    }

    /// Sends a complete buffered candidate utterance (raw PCM16LE mono
    /// 16kHz) for transcription. Per-turn, not token-streamed — see
    /// docs/ARCHITECTURE.md for why that's an intentional MVP simplification.
    pub async fn transcribe(&self, pcm16_16khz: Vec<u8>) -> Result<String, AiClientError> {
        let resp = self
            .http
            .post(format!("{}/asr/transcribe", self.base_url))
            .header("content-type", "audio/l16;rate=16000")
            .body(pcm16_16khz)
            .send()
            .await?;
        let parsed: TranscribeResponse = Self::check_status(resp).await?.json().await?;
        Ok(parsed.transcript)
    }

    /// Streams synthesized speech (raw PCM16LE mono 22050Hz) chunk by chunk
    /// so the gateway can start forwarding audio to the browser before the
    /// full utterance has finished synthesizing.
    pub async fn synthesize_stream(
        &self,
        text: &str,
    ) -> Result<impl Stream<Item = Result<Bytes, reqwest::Error>>, AiClientError> {
        let resp = self
            .http
            .post(format!("{}/tts/synthesize", self.base_url))
            .json(&SynthesizeRequest {
                text: text.to_string(),
            })
            .send()
            .await?;
        Ok(Self::check_status(resp).await?.bytes_stream())
    }

    async fn check_status(resp: reqwest::Response) -> Result<reqwest::Response, AiClientError> {
        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(AiClientError::Status { status, body });
        }
        Ok(resp)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::TcpListener;
    use tokio::time::Instant;

    /// A hung ai-service must not block the caller forever: with a client
    /// timeout configured, a request to a server that accepts the TCP
    /// connection but never writes a response back should still error out
    /// once the timeout elapses, instead of hanging indefinitely. This is
    /// what lets `ws_handler.rs` reach `speak_recovery()` on an ai-service
    /// stall rather than blocking the whole session loop.
    #[tokio::test]
    async fn request_times_out_instead_of_hanging_forever() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        // Accept connections and hold them open without ever responding.
        tokio::spawn(async move {
            while let Ok((socket, _)) = listener.accept().await {
                // Keep the connection alive; never read/write/close it.
                std::mem::forget(socket);
            }
        });

        let short_timeout = Duration::from_millis(200);
        let client = AiClient::with_timeout(format!("http://{addr}"), short_timeout);

        let started = Instant::now();
        let result = client.start_session().await;
        let elapsed = started.elapsed();

        match &result {
            Err(AiClientError::Request(e)) if e.is_timeout() => {}
            other => panic!("expected a timeout error, got: {other:?}"),
        }
        // Generous upper bound (10x the configured timeout) to keep this
        // robust against slow/loaded CI machines while still proving we
        // didn't hang indefinitely.
        assert!(
            elapsed < short_timeout * 10,
            "request took {elapsed:?}, expected it to time out around {short_timeout:?}"
        );
    }
}
