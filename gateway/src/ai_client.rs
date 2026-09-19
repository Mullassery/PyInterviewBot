//! HTTP client for the ai-service (ASR / LLM agent / TTS / evidence engine).
//!
//! The gateway never talks to Ollama, faster-whisper, or Piper directly —
//! it only knows this contract. That keeps the real-time transport layer
//! independent of which AI providers are behind it (spec §31/§30).

use bytes::Bytes;
use futures_util::Stream;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

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
        Self {
            http: reqwest::Client::new(),
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
