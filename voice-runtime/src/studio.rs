//! Self-hosted speech-studio runtime.
//!
//! This is the in-repo Rust replacement for an external desktop speech studio.
//! It implements audio formats, WAV encoding/decoding, resampling, session
//! lifecycle, cancellation, limits, and replaceable TTS/ASR engine contracts.
//! Neural inference stays behind `SpeechEngine`; a deterministic mock engine is
//! included for CI and local development.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde::Serialize;
use thiserror::Error;
use tokio::sync::{broadcast, Mutex, RwLock};
use uuid::Uuid;

pub type StudioFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T, StudioError>> + Send + 'a>>;
pub type StudioSessionId = Uuid;

pub const DEFAULT_TTS_SAMPLE_RATE: u32 = 22_050;
pub const DEFAULT_ASR_SAMPLE_RATE: u32 = 16_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct AudioFormat {
    pub sample_rate: u32,
    pub channels: u16,
}

impl AudioFormat {
    pub fn pcm16(sample_rate: u32) -> Self {
        Self {
            sample_rate,
            channels: 1,
        }
    }

    pub fn tts() -> Self {
        Self::pcm16(DEFAULT_TTS_SAMPLE_RATE)
    }

    pub fn asr() -> Self {
        Self::pcm16(DEFAULT_ASR_SAMPLE_RATE)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct AudioBuffer {
    pub format: AudioFormat,
    pub pcm16: Vec<u8>,
}

impl AudioBuffer {
    pub fn new(format: AudioFormat, pcm16: Vec<u8>) -> Result<Self, StudioError> {
        if format.channels != 1 {
            return Err(StudioError::UnsupportedFormat(
                "only mono PCM16 is supported".to_string(),
            ));
        }
        if format.sample_rate == 0 {
            return Err(StudioError::UnsupportedFormat(
                "sample rate must be positive".to_string(),
            ));
        }
        if !pcm16.len().is_multiple_of(2) {
            return Err(StudioError::InvalidAudio(
                "PCM16 data must have even length".to_string(),
            ));
        }
        Ok(Self { format, pcm16 })
    }

    pub fn silence(format: AudioFormat, duration: Duration) -> Self {
        let samples = (format.sample_rate as f64 * duration.as_secs_f64()).round() as usize;
        Self {
            format,
            pcm16: vec![0u8; samples * 2],
        }
    }

    pub fn duration(&self) -> Duration {
        let samples = self.pcm16.len() / 2;
        Duration::from_secs_f64(samples as f64 / self.format.sample_rate as f64)
    }

    pub fn resampled(&self, target_rate: u32) -> Result<Self, StudioError> {
        if target_rate == self.format.sample_rate {
            return Ok(self.clone());
        }
        Ok(Self {
            format: AudioFormat::pcm16(target_rate),
            pcm16: resample_pcm16(&self.pcm16, self.format.sample_rate, target_rate),
        })
    }

    pub fn samples(&self) -> Vec<i16> {
        self.pcm16
            .as_chunks::<2>()
            .0
            .iter()
            .map(|chunk| i16::from_le_bytes(*chunk))
            .collect()
    }

    pub fn rms(&self) -> f64 {
        let samples = self.samples();
        if samples.is_empty() {
            return 0.0;
        }
        let sum: f64 = samples
            .iter()
            .map(|sample| (*sample as f64) * (*sample as f64))
            .sum();
        (sum / samples.len() as f64).sqrt()
    }

    pub fn trim_silence(&self, threshold: i16) -> Self {
        let samples = self.samples();
        let first = samples.iter().position(|sample| sample.abs() > threshold);
        let last = samples.iter().rposition(|sample| sample.abs() > threshold);
        let trimmed = match (first, last) {
            (Some(first), Some(last)) => &samples[first..=last],
            _ => &[],
        };
        let mut pcm16 = Vec::with_capacity(trimmed.len() * 2);
        for sample in trimmed {
            pcm16.extend_from_slice(&sample.to_le_bytes());
        }
        Self {
            format: self.format,
            pcm16,
        }
    }

    pub fn normalize_peak(&self, target: i16) -> Self {
        let samples = self.samples();
        let peak = samples
            .iter()
            .map(|sample| sample.unsigned_abs())
            .max()
            .unwrap_or(0);
        if peak == 0 {
            return self.clone();
        }
        let scale = target.unsigned_abs() as f64 / peak as f64;
        let mut pcm16 = Vec::with_capacity(self.pcm16.len());
        for sample in samples {
            let value = (sample as f64 * scale)
                .round()
                .clamp(i16::MIN as f64, i16::MAX as f64) as i16;
            pcm16.extend_from_slice(&value.to_le_bytes());
        }
        Self {
            format: self.format,
            pcm16,
        }
    }

    pub fn concatenate(buffers: &[AudioBuffer]) -> Result<Self, StudioError> {
        let Some(first) = buffers.first() else {
            return Err(StudioError::InvalidAudio(
                "cannot concatenate an empty list".to_string(),
            ));
        };
        let mut pcm16 = Vec::new();
        for buffer in buffers {
            if buffer.format != first.format {
                return Err(StudioError::UnsupportedFormat(
                    "all buffers must share one format".to_string(),
                ));
            }
            pcm16.extend_from_slice(&buffer.pcm16);
        }
        Self::new(first.format, pcm16)
    }

    pub fn chunked(&self, max_bytes: usize) -> Vec<Vec<u8>> {
        let max_bytes = (max_bytes.max(2) / 2) * 2;
        self.pcm16.chunks(max_bytes).map(<[u8]>::to_vec).collect()
    }

    pub fn chunked_buffers(&self, max_bytes: usize) -> Vec<AudioBuffer> {
        self.chunked(max_bytes)
            .into_iter()
            .map(|pcm16| AudioBuffer {
                format: self.format,
                pcm16,
            })
            .collect()
    }

    pub fn to_wav(&self) -> Vec<u8> {
        encode_wav(self)
    }

    pub fn from_wav(bytes: &[u8]) -> Result<Self, StudioError> {
        decode_wav(bytes)
    }
}

pub fn resample_pcm16(data: &[u8], source_rate: u32, target_rate: u32) -> Vec<u8> {
    let data = &data[..data.len() - (data.len() % 2)];
    if source_rate == target_rate || data.is_empty() {
        return data.to_vec();
    }
    let source: Vec<i16> = data
        .as_chunks::<2>()
        .0
        .iter()
        .map(|chunk| i16::from_le_bytes(*chunk))
        .collect();
    if source.len() < 2 {
        return data.to_vec();
    }
    let target_count = ((source.len() as f64 * target_rate as f64) / source_rate as f64)
        .round()
        .max(1.0) as usize;
    let mut out = Vec::with_capacity(target_count * 2);
    let ratio = (source.len() - 1) as f64 / (target_count.max(2) - 1) as f64;
    for index in 0..target_count {
        let position = index as f64 * ratio;
        let left = position.floor() as usize;
        let right = (left + 1).min(source.len() - 1);
        let fraction = position - left as f64;
        let value = source[left] as f64 * (1.0 - fraction) + source[right] as f64 * fraction;
        let sample = value.round().clamp(i16::MIN as f64, i16::MAX as f64) as i16;
        out.extend_from_slice(&sample.to_le_bytes());
    }
    out
}

pub fn encode_wav(buffer: &AudioBuffer) -> Vec<u8> {
    let data_len = buffer.pcm16.len() as u32;
    let byte_rate = buffer.format.sample_rate * 2;
    let block_align = 2u16;
    let mut out = Vec::with_capacity(44 + buffer.pcm16.len());
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&(36 + data_len).to_le_bytes());
    out.extend_from_slice(b"WAVE");
    out.extend_from_slice(b"fmt ");
    out.extend_from_slice(&16u32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&buffer.format.channels.to_le_bytes());
    out.extend_from_slice(&buffer.format.sample_rate.to_le_bytes());
    out.extend_from_slice(&byte_rate.to_le_bytes());
    out.extend_from_slice(&block_align.to_le_bytes());
    out.extend_from_slice(&16u16.to_le_bytes());
    out.extend_from_slice(b"data");
    out.extend_from_slice(&data_len.to_le_bytes());
    out.extend_from_slice(&buffer.pcm16);
    out
}

pub fn decode_wav(bytes: &[u8]) -> Result<AudioBuffer, StudioError> {
    if bytes.len() < 44 || &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return Err(StudioError::InvalidAudio(
            "not a RIFF/WAVE file".to_string(),
        ));
    }
    let mut offset = 12usize;
    let mut format: Option<AudioFormat> = None;
    while offset + 8 <= bytes.len() {
        let id = &bytes[offset..offset + 4];
        let size = u32::from_le_bytes(
            bytes[offset + 4..offset + 8]
                .try_into()
                .expect("slice length checked"),
        ) as usize;
        let start = offset + 8;
        let end = start
            .checked_add(size)
            .ok_or_else(|| StudioError::InvalidAudio("WAV chunk length overflow".to_string()))?;
        if end > bytes.len() {
            return Err(StudioError::InvalidAudio("truncated WAV chunk".to_string()));
        }
        if id == b"fmt " {
            if size < 16 {
                return Err(StudioError::InvalidAudio("short fmt chunk".to_string()));
            }
            let audio_format = u16::from_le_bytes([bytes[start], bytes[start + 1]]);
            let channels = u16::from_le_bytes([bytes[start + 2], bytes[start + 3]]);
            let sample_rate = u32::from_le_bytes(
                bytes[start + 4..start + 8]
                    .try_into()
                    .expect("slice length checked"),
            );
            let bits = u16::from_le_bytes([bytes[start + 14], bytes[start + 15]]);
            if audio_format != 1 || bits != 16 {
                return Err(StudioError::UnsupportedFormat(
                    "only PCM16 WAV is supported".to_string(),
                ));
            }
            format = Some(AudioFormat {
                sample_rate,
                channels,
            });
        } else if id == b"data" {
            let format = format.ok_or_else(|| {
                StudioError::InvalidAudio("WAV data chunk before fmt chunk".to_string())
            })?;
            return AudioBuffer::new(format, bytes[start..end].to_vec());
        }
        offset = end + (size % 2);
    }
    Err(StudioError::InvalidAudio(
        "WAV has no data chunk".to_string(),
    ))
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct StudioCapabilities {
    pub engine: String,
    pub local: bool,
    pub streaming_tts: bool,
    pub streaming_asr: bool,
    pub languages: Vec<String>,
    pub tts_sample_rate: u32,
    pub asr_sample_rate: u32,
    pub max_text_bytes: usize,
    pub max_audio_bytes: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum StudioError {
    #[error("unsupported audio format: {0}")]
    UnsupportedFormat(String),
    #[error("invalid audio: {0}")]
    InvalidAudio(String),
    #[error("session not found")]
    SessionNotFound,
    #[error("session is closed")]
    SessionClosed,
    #[error("speech text is empty")]
    InvalidText,
    #[error("speech text exceeds the limit")]
    TextTooLarge,
    #[error("audio exceeds the limit")]
    AudioTooLarge,
    #[error("speech generation was cancelled")]
    Cancelled,
    #[error("session capacity exceeded")]
    CapacityExceeded,
    #[error("engine unavailable: {0}")]
    EngineUnavailable(String),
    #[error("engine timed out")]
    Timeout,
    #[error("engine internal error: {0}")]
    EngineInternal(String),
}

#[derive(Debug, Clone, Serialize)]
pub struct EngineDescriptor {
    pub name: String,
    pub is_default: bool,
    pub healthy: bool,
    pub capabilities: StudioCapabilities,
}

#[derive(Clone)]
pub struct EngineCatalog {
    engines: HashMap<String, Arc<dyn SpeechEngine>>,
    default_engine: String,
}

impl EngineCatalog {
    pub fn new(default_engine: impl Into<String>) -> Self {
        Self {
            engines: HashMap::new(),
            default_engine: default_engine.into(),
        }
    }

    pub fn register(mut self, engine: Arc<dyn SpeechEngine>) -> Self {
        let name = engine.capabilities().engine;
        self.engines.insert(name, engine);
        self
    }

    pub fn default_engine(&self) -> Result<Arc<dyn SpeechEngine>, StudioError> {
        self.engines
            .get(&self.default_engine)
            .cloned()
            .ok_or_else(|| {
                StudioError::EngineUnavailable(format!(
                    "default engine '{}' is not registered",
                    self.default_engine
                ))
            })
    }

    pub fn engine(&self, name: &str) -> Option<Arc<dyn SpeechEngine>> {
        self.engines.get(name).cloned()
    }

    pub fn with_default(mut self, name: impl Into<String>) -> Self {
        self.default_engine = name.into();
        self
    }

    pub fn names(&self) -> Vec<String> {
        let mut names: Vec<String> = self.engines.keys().cloned().collect();
        names.sort();
        names
    }

    pub async fn describe(&self) -> Vec<EngineDescriptor> {
        let mut descriptors = Vec::new();
        for (name, engine) in &self.engines {
            descriptors.push(EngineDescriptor {
                name: name.clone(),
                is_default: name == &self.default_engine,
                healthy: engine.health().await.unwrap_or(false),
                capabilities: engine.capabilities(),
            });
        }
        descriptors.sort_by(|left, right| left.name.cmp(&right.name));
        descriptors
    }
}

pub trait SpeechEngine: Send + Sync + 'static {
    fn capabilities(&self) -> StudioCapabilities;
    fn health(&self) -> StudioFuture<'_, bool>;
    fn synthesize(
        &self,
        text: &str,
        voice: &str,
        format: AudioFormat,
    ) -> StudioFuture<'_, AudioBuffer>;
    fn transcribe(&self, audio: &AudioBuffer, language: &str) -> StudioFuture<'_, String>;
}

#[derive(Debug, Clone, Serialize)]
pub struct StudioConfig {
    pub tts_format: AudioFormat,
    pub asr_format: AudioFormat,
    pub max_text_bytes: usize,
    pub max_audio_bytes: usize,
    pub max_sessions: usize,
    pub idle_timeout: Duration,
}

impl Default for StudioConfig {
    fn default() -> Self {
        Self {
            tts_format: AudioFormat::tts(),
            asr_format: AudioFormat::asr(),
            max_text_bytes: 4096,
            max_audio_bytes: 10 * 1024 * 1024,
            max_sessions: 16,
            idle_timeout: Duration::from_secs(300),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum StudioEvent {
    SessionStarted {
        session_id: StudioSessionId,
        sequence: u64,
        engine: String,
    },
    SpeechGenerated {
        session_id: StudioSessionId,
        sequence: u64,
        bytes: usize,
        sample_rate: u32,
    },
    SpeechCancelled {
        session_id: StudioSessionId,
        sequence: u64,
    },
    Transcript {
        session_id: StudioSessionId,
        sequence: u64,
        text: String,
        language: String,
    },
    SessionClosed {
        session_id: StudioSessionId,
        sequence: u64,
    },
    SessionTimedOut {
        session_id: StudioSessionId,
        sequence: u64,
    },
}

struct StudioSession {
    id: StudioSessionId,
    sequence: std::sync::atomic::AtomicU64,
    cancelled: AtomicBool,
    speaking: AtomicBool,
    recording: Mutex<Vec<u8>>,
    last_activity: Mutex<Instant>,
    closed: AtomicBool,
}

impl StudioSession {
    fn new(id: StudioSessionId) -> Self {
        Self {
            id,
            sequence: std::sync::atomic::AtomicU64::new(0),
            cancelled: AtomicBool::new(false),
            speaking: AtomicBool::new(false),
            recording: Mutex::new(Vec::new()),
            last_activity: Mutex::new(Instant::now()),
            closed: AtomicBool::new(false),
        }
    }

    fn next_sequence(&self) -> u64 {
        self.sequence.fetch_add(1, Ordering::SeqCst) + 1
    }

    async fn touch(&self) {
        *self.last_activity.lock().await = Instant::now();
    }

    async fn idle_for(&self) -> Duration {
        self.last_activity.lock().await.elapsed()
    }
}

pub struct SpeechStudio {
    engine: Arc<dyn SpeechEngine>,
    config: StudioConfig,
    sessions: RwLock<HashMap<StudioSessionId, Arc<StudioSession>>>,
    events: broadcast::Sender<StudioEvent>,
}

impl SpeechStudio {
    pub fn new(engine: Arc<dyn SpeechEngine>, config: StudioConfig) -> Self {
        let (events, _) = broadcast::channel(128);
        Self {
            engine,
            config,
            sessions: RwLock::new(HashMap::new()),
            events,
        }
    }

    pub fn subscribe(&self) -> broadcast::Receiver<StudioEvent> {
        self.events.subscribe()
    }

    pub fn capabilities(&self) -> StudioCapabilities {
        self.engine.capabilities()
    }

    pub async fn health(&self) -> bool {
        self.engine.health().await.unwrap_or(false)
    }

    pub async fn active_sessions(&self) -> usize {
        self.sessions.read().await.len()
    }

    pub async fn start_session(&self) -> Result<StudioSessionId, StudioError> {
        if self.sessions.read().await.len() >= self.config.max_sessions {
            return Err(StudioError::CapacityExceeded);
        }
        let id = Uuid::new_v4();
        let session = Arc::new(StudioSession::new(id));
        {
            let mut sessions = self.sessions.write().await;
            if sessions.len() >= self.config.max_sessions {
                return Err(StudioError::CapacityExceeded);
            }
            sessions.insert(id, session.clone());
        }
        self.publish(&session, |sequence| StudioEvent::SessionStarted {
            session_id: id,
            sequence,
            engine: self.capabilities().engine,
        });
        Ok(id)
    }

    pub async fn speak(
        &self,
        session_id: StudioSessionId,
        text: &str,
        voice: &str,
    ) -> Result<AudioBuffer, StudioError> {
        let session = self.session(session_id).await?;
        if session.closed.load(Ordering::SeqCst) {
            return Err(StudioError::SessionClosed);
        }
        let text = text.trim();
        if text.is_empty() {
            return Err(StudioError::InvalidText);
        }
        if text.len() > self.config.max_text_bytes {
            return Err(StudioError::TextTooLarge);
        }
        session.cancelled.store(false, Ordering::SeqCst);
        session.speaking.store(true, Ordering::SeqCst);
        let result = self
            .engine
            .synthesize(text, voice, self.config.tts_format)
            .await;
        session.speaking.store(false, Ordering::SeqCst);
        let audio = result?;
        if session.cancelled.load(Ordering::SeqCst) {
            self.publish(&session, |sequence| StudioEvent::SpeechCancelled {
                session_id,
                sequence,
            });
            return Err(StudioError::Cancelled);
        }
        session.touch().await;
        self.publish(&session, |sequence| StudioEvent::SpeechGenerated {
            session_id,
            sequence,
            bytes: audio.pcm16.len(),
            sample_rate: audio.format.sample_rate,
        });
        Ok(audio)
    }

    pub async fn speak_stream(
        &self,
        session_id: StudioSessionId,
        text: &str,
        voice: &str,
    ) -> Result<Vec<AudioBuffer>, StudioError> {
        let audio = self.speak(session_id, text, voice).await?;
        Ok(audio.chunked_buffers(8192))
    }

    pub async fn interrupt(&self, session_id: StudioSessionId) -> Result<bool, StudioError> {
        let session = self.session(session_id).await?;
        let was_running = session.speaking.swap(false, Ordering::SeqCst);
        if !was_running {
            return Ok(false);
        }
        session.cancelled.store(true, Ordering::SeqCst);
        self.publish(&session, |sequence| StudioEvent::SpeechCancelled {
            session_id,
            sequence,
        });
        Ok(true)
    }

    pub async fn transcribe(
        &self,
        session_id: StudioSessionId,
        audio: &AudioBuffer,
        language: &str,
    ) -> Result<String, StudioError> {
        let session = self.session(session_id).await?;
        if session.closed.load(Ordering::SeqCst) {
            return Err(StudioError::SessionClosed);
        }
        if audio.pcm16.len() > self.config.max_audio_bytes {
            return Err(StudioError::AudioTooLarge);
        }
        let audio = audio.resampled(self.config.asr_format.sample_rate)?;
        let text = self.engine.transcribe(&audio, language).await?;
        session.touch().await;
        self.publish(&session, |sequence| StudioEvent::Transcript {
            session_id,
            sequence,
            text: text.clone(),
            language: language.to_string(),
        });
        Ok(text)
    }

    pub async fn record_audio(
        &self,
        session_id: StudioSessionId,
        audio: &AudioBuffer,
    ) -> Result<usize, StudioError> {
        let session = self.session(session_id).await?;
        if session.closed.load(Ordering::SeqCst) {
            return Err(StudioError::SessionClosed);
        }
        let audio = audio.resampled(self.config.asr_format.sample_rate)?;
        let total = {
            let mut recording = session.recording.lock().await;
            if recording.len() + audio.pcm16.len() > self.config.max_audio_bytes {
                return Err(StudioError::AudioTooLarge);
            }
            recording.extend_from_slice(&audio.pcm16);
            recording.len()
        };
        session.touch().await;
        Ok(total)
    }

    pub async fn recording_wav(
        &self,
        session_id: StudioSessionId,
    ) -> Result<AudioBuffer, StudioError> {
        let session = self.session(session_id).await?;
        let data = session.recording.lock().await.clone();
        AudioBuffer::new(self.config.asr_format, data)
    }

    pub async fn clear_recording(&self, session_id: StudioSessionId) -> Result<(), StudioError> {
        let session = self.session(session_id).await?;
        session.recording.lock().await.clear();
        Ok(())
    }

    pub async fn end_session(&self, session_id: StudioSessionId) -> Result<(), StudioError> {
        let session = self.session(session_id).await?;
        session.closed.store(true, Ordering::SeqCst);
        session.cancelled.store(true, Ordering::SeqCst);
        session.speaking.store(false, Ordering::SeqCst);
        {
            let mut sessions = self.sessions.write().await;
            sessions.remove(&session_id);
        }
        self.publish(&session, |sequence| StudioEvent::SessionClosed {
            session_id,
            sequence,
        });
        Ok(())
    }

    pub async fn sweep_idle(&self) -> Vec<StudioSessionId> {
        let sessions: Vec<Arc<StudioSession>> = {
            let guard = self.sessions.read().await;
            guard.values().cloned().collect()
        };
        let mut expired = Vec::new();
        for session in sessions {
            if session.idle_for().await < self.config.idle_timeout {
                continue;
            }
            session.closed.store(true, Ordering::SeqCst);
            session.cancelled.store(true, Ordering::SeqCst);
            session.speaking.store(false, Ordering::SeqCst);
            {
                let mut guard = self.sessions.write().await;
                guard.remove(&session.id);
            }
            self.publish(&session, |sequence| StudioEvent::SessionTimedOut {
                session_id: session.id,
                sequence,
            });
            expired.push(session.id);
        }
        expired
    }

    async fn session(
        &self,
        session_id: StudioSessionId,
    ) -> Result<Arc<StudioSession>, StudioError> {
        self.sessions
            .read()
            .await
            .get(&session_id)
            .cloned()
            .ok_or(StudioError::SessionNotFound)
    }

    fn publish(
        &self,
        session: &StudioSession,
        build: impl FnOnce(u64) -> StudioEvent,
    ) -> StudioEvent {
        let event = build(session.next_sequence());
        let _ = self.events.send(event.clone());
        event
    }
}

#[derive(Debug, Clone, Default)]
pub struct MockSpeechEngine {
    pub delay: Option<Duration>,
}

impl MockSpeechEngine {
    pub fn with_delay(delay: Duration) -> Self {
        Self { delay: Some(delay) }
    }
}

impl SpeechEngine for MockSpeechEngine {
    fn capabilities(&self) -> StudioCapabilities {
        StudioCapabilities {
            engine: "mock".to_string(),
            local: true,
            streaming_tts: true,
            streaming_asr: true,
            languages: vec!["en".to_string()],
            tts_sample_rate: DEFAULT_TTS_SAMPLE_RATE,
            asr_sample_rate: DEFAULT_ASR_SAMPLE_RATE,
            max_text_bytes: 4096,
            max_audio_bytes: 10 * 1024 * 1024,
        }
    }

    fn health(&self) -> StudioFuture<'_, bool> {
        Box::pin(async { Ok(true) })
    }

    fn synthesize(
        &self,
        text: &str,
        _voice: &str,
        format: AudioFormat,
    ) -> StudioFuture<'_, AudioBuffer> {
        let delay = self.delay;
        let duration = Duration::from_millis((250 + text.len() * 15).min(3000) as u64);
        Box::pin(async move {
            if let Some(delay) = delay {
                tokio::time::sleep(delay).await;
            }
            Ok(AudioBuffer::silence(format, duration))
        })
    }

    fn transcribe(&self, _audio: &AudioBuffer, _language: &str) -> StudioFuture<'_, String> {
        let delay = self.delay;
        Box::pin(async move {
            if let Some(delay) = delay {
                tokio::time::sleep(delay).await;
            }
            Ok("deterministic mock transcript".to_string())
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn studio() -> SpeechStudio {
        SpeechStudio::new(
            Arc::new(MockSpeechEngine::default()),
            StudioConfig::default(),
        )
    }

    #[test]
    fn wav_round_trip_preserves_audio() {
        let buffer = AudioBuffer::silence(AudioFormat::tts(), Duration::from_millis(50));
        let wav = buffer.to_wav();
        let decoded = AudioBuffer::from_wav(&wav).unwrap();
        assert_eq!(decoded, buffer);
    }

    #[test]
    fn malformed_audio_is_rejected() {
        assert!(AudioBuffer::new(AudioFormat::tts(), vec![0u8; 3]).is_err());
        assert!(AudioBuffer::from_wav(b"not a wav").is_err());
        assert!(AudioBuffer::new(
            AudioFormat {
                sample_rate: 16_000,
                channels: 2,
            },
            vec![0u8; 4],
        )
        .is_err());
    }

    #[test]
    fn resample_changes_length() {
        let source = vec![0u8; 2 * 24_000];
        let resampled = resample_pcm16(&source, 24_000, 22_050);
        assert_eq!(resampled.len(), 2 * 22_050);
    }

    #[tokio::test]
    async fn session_speaks_and_transcribes() {
        let studio = studio();
        let mut events = studio.subscribe();
        let id = studio.start_session().await.unwrap();
        let audio = studio.speak(id, "hello", "default").await.unwrap();
        assert!(!audio.pcm16.is_empty());
        let text = studio
            .transcribe(
                id,
                &AudioBuffer::silence(AudioFormat::pcm16(16_000), Duration::from_millis(20)),
                "en",
            )
            .await
            .unwrap();
        assert_eq!(text, "deterministic mock transcript");
        assert!(matches!(
            tokio::time::timeout(Duration::from_secs(1), events.recv())
                .await
                .unwrap()
                .unwrap(),
            StudioEvent::SessionStarted { .. }
        ));
        studio.end_session(id).await.unwrap();
        assert_eq!(studio.active_sessions().await, 0);
    }

    #[tokio::test]
    async fn interrupt_cancels_in_flight_synthesis() {
        let studio = SpeechStudio::new(
            Arc::new(MockSpeechEngine::with_delay(Duration::from_millis(40))),
            StudioConfig::default(),
        );
        let id = studio.start_session().await.unwrap();
        let studio_ref = &studio;
        let result = tokio::join!(
            async { studio_ref.speak(id, "hello", "default").await },
            async {
                tokio::time::sleep(Duration::from_millis(10)).await;
                studio_ref.interrupt(id).await
            }
        );
        assert!(matches!(result.0, Err(StudioError::Cancelled)));
        assert!(result.1.unwrap());
    }

    #[tokio::test]
    async fn limits_and_unknown_sessions_are_enforced() {
        let studio = SpeechStudio::new(
            Arc::new(MockSpeechEngine::default()),
            StudioConfig {
                max_text_bytes: 4,
                max_audio_bytes: 4,
                ..StudioConfig::default()
            },
        );
        let id = studio.start_session().await.unwrap();
        assert!(matches!(
            studio.speak(id, "toolong", "default").await,
            Err(StudioError::TextTooLarge)
        ));
        assert!(matches!(
            studio.speak(id, "   ", "default").await,
            Err(StudioError::InvalidText)
        ));
        let oversized = AudioBuffer::new(AudioFormat::pcm16(16_000), vec![0u8; 8]).unwrap();
        assert!(matches!(
            studio.transcribe(id, &oversized, "en").await,
            Err(StudioError::AudioTooLarge)
        ));
        let missing = Uuid::new_v4();
        assert!(matches!(
            studio.speak(missing, "hello", "default").await,
            Err(StudioError::SessionNotFound)
        ));
    }

    #[tokio::test]
    async fn idle_sessions_are_swept() {
        let studio = SpeechStudio::new(
            Arc::new(MockSpeechEngine::default()),
            StudioConfig {
                idle_timeout: Duration::from_millis(10),
                ..StudioConfig::default()
            },
        );
        let id = studio.start_session().await.unwrap();
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert_eq!(studio.sweep_idle().await, vec![id]);
        assert_eq!(studio.active_sessions().await, 0);
    }

    #[test]
    fn audio_processing_helpers_are_deterministic() {
        let mut pcm = vec![0u8; 20];
        for index in 0..10 {
            let sample = if !(2..=7).contains(&index) {
                0i16
            } else {
                1000i16
            };
            pcm[index * 2..index * 2 + 2].copy_from_slice(&sample.to_le_bytes());
        }
        let buffer = AudioBuffer::new(AudioFormat::pcm16(16_000), pcm).unwrap();
        assert_eq!(buffer.samples().len(), 10);
        let trimmed = buffer.trim_silence(10);
        assert_eq!(trimmed.samples(), vec![1000; 6]);
        let normalized = trimmed.normalize_peak(20_000);
        assert_eq!(normalized.samples(), vec![20_000; 6]);
        let chunks = normalized.chunked(4);
        assert!(chunks.iter().all(|chunk| chunk.len() <= 4));
        assert_eq!(chunks.concat(), normalized.pcm16);
    }

    #[tokio::test]
    async fn streaming_chunks_match_full_speech() {
        let studio = studio();
        let id = studio.start_session().await.unwrap();
        let full = studio
            .speak(id, "stream this answer", "default")
            .await
            .unwrap();
        let chunks = studio
            .speak_stream(id, "stream this answer", "default")
            .await
            .unwrap();
        assert!(chunks.len() > 1);
        assert_eq!(
            chunks
                .iter()
                .flat_map(|chunk| chunk.pcm16.clone())
                .collect::<Vec<u8>>(),
            full.pcm16
        );
    }

    #[tokio::test]
    async fn engine_catalog_reports_registered_engines() {
        let catalog = EngineCatalog::new("mock").register(Arc::new(MockSpeechEngine::default()));
        assert_eq!(catalog.names(), vec!["mock".to_string()]);
        assert!(catalog.engine("missing").is_none());
        let descriptions = catalog.describe().await;
        assert_eq!(descriptions.len(), 1);
        assert!(descriptions[0].is_default);
        assert!(descriptions[0].healthy);
    }

    #[tokio::test]
    async fn recording_is_explicit_bounded_and_exportable() {
        let studio = SpeechStudio::new(
            Arc::new(MockSpeechEngine::default()),
            StudioConfig {
                max_audio_bytes: 32,
                ..StudioConfig::default()
            },
        );
        let id = studio.start_session().await.unwrap();
        let audio = AudioBuffer::silence(AudioFormat::pcm16(16_000), Duration::from_millis(1));
        assert_eq!(studio.record_audio(id, &audio).await.unwrap(), 32);
        assert!(matches!(
            studio.record_audio(id, &audio).await,
            Err(StudioError::AudioTooLarge)
        ));
        let recorded = studio.recording_wav(id).await.unwrap();
        assert_eq!(recorded.format, AudioFormat::pcm16(16_000));
        assert!(!recorded.to_wav().is_empty());
        studio.clear_recording(id).await.unwrap();
        assert!(studio.recording_wav(id).await.unwrap().pcm16.is_empty());
    }

    #[tokio::test]
    async fn concurrent_sessions_are_isolated() {
        let studio = Arc::new(studio());
        let first = studio.start_session().await.unwrap();
        let second = studio.start_session().await.unwrap();
        let (a, b) = tokio::join!(
            studio.speak(first, "first", "default"),
            studio.speak(second, "second", "default"),
        );
        assert!(a.is_ok());
        assert!(b.is_ok());
        assert_eq!(studio.active_sessions().await, 2);
    }
}
