//! Self-hosted Rust voice runtime: session lifecycle, bounded audio, event
//! sequencing, cancellation, timeouts, and provider-neutral event routing.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU32, AtomicU64, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde::Serialize;
use thiserror::Error;
use tokio::sync::{broadcast, Mutex, RwLock};
use uuid::Uuid;

use crate::events::{CompletionReason, SessionId, VoiceEvent};
use crate::provider::{
    ProviderCapabilities, ProviderError, ProviderEvent, SessionConfig, VoiceProvider,
};
use crate::state::{transition, IllegalVoiceTransition, VoiceState, VoiceTransition};

#[derive(Debug, Clone, Serialize)]
pub struct RuntimeConfig {
    pub max_sessions: usize,
    pub event_buffer: usize,
    pub max_audio_bytes: usize,
    pub max_text_bytes: usize,
    pub idle_timeout: Duration,
}

impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            max_sessions: 64,
            event_buffer: 256,
            max_audio_bytes: 3_200_000,
            max_text_bytes: 4096,
            idle_timeout: Duration::from_secs(300),
        }
    }
}

#[derive(Debug, Error)]
pub enum RuntimeError {
    #[error("session not found")]
    SessionNotFound,
    #[error("session is closed")]
    SessionClosed,
    #[error("session capacity exceeded")]
    CapacityExceeded,
    #[error("audio must be non-empty even-length PCM16")]
    InvalidAudio,
    #[error("audio exceeds the per-session limit")]
    AudioTooLarge,
    #[error("speech text is empty")]
    InvalidText,
    #[error("speech text exceeds the per-session limit")]
    TextTooLarge,
    #[error("illegal voice transition: {0}")]
    IllegalTransition(#[from] IllegalVoiceTransition),
    #[error(transparent)]
    Provider(#[from] ProviderError),
}

struct Session {
    id: SessionId,
    state: Mutex<VoiceState>,
    sequence: AtomicU64,
    audio_bytes: AtomicUsize,
    turn: AtomicU32,
    last_activity: Mutex<Instant>,
}

impl Session {
    fn new(id: SessionId) -> Self {
        Self {
            id,
            state: Mutex::new(VoiceState::Created),
            sequence: AtomicU64::new(0),
            audio_bytes: AtomicUsize::new(0),
            turn: AtomicU32::new(0),
            last_activity: Mutex::new(Instant::now()),
        }
    }

    fn next_sequence(&self) -> u64 {
        self.sequence.fetch_add(1, Ordering::SeqCst) + 1
    }

    async fn state(&self) -> VoiceState {
        *self.state.lock().await
    }

    async fn apply(&self, event: VoiceTransition) -> Result<bool, IllegalVoiceTransition> {
        let mut state = self.state.lock().await;
        match transition(*state, event) {
            Ok(next) => {
                let changed = next != *state;
                *state = next;
                Ok(changed)
            }
            Err(err) => Err(err),
        }
    }

    async fn touch(&self) {
        *self.last_activity.lock().await = Instant::now();
    }

    async fn idle_for(&self) -> Duration {
        self.last_activity.lock().await.elapsed()
    }
}

pub struct VoiceRuntime {
    provider: Arc<dyn VoiceProvider>,
    config: RuntimeConfig,
    sessions: RwLock<HashMap<SessionId, Arc<Session>>>,
    events: broadcast::Sender<VoiceEvent>,
}

impl VoiceRuntime {
    pub fn new(provider: Arc<dyn VoiceProvider>, config: RuntimeConfig) -> Self {
        let (events, _) = broadcast::channel(config.event_buffer.max(1));
        Self {
            provider,
            config,
            sessions: RwLock::new(HashMap::new()),
            events,
        }
    }

    pub fn subscribe(&self) -> broadcast::Receiver<VoiceEvent> {
        self.events.subscribe()
    }

    pub fn capabilities(&self) -> ProviderCapabilities {
        self.provider.capabilities()
    }

    pub async fn health(&self) -> Result<bool, RuntimeError> {
        Ok(self.provider.health().await?)
    }

    pub async fn active_sessions(&self) -> usize {
        self.sessions.read().await.len()
    }

    pub async fn session_state(&self, session_id: SessionId) -> Result<VoiceState, RuntimeError> {
        Ok(self.session(session_id).await?.state().await)
    }

    pub async fn start_session(&self, config: SessionConfig) -> Result<SessionId, RuntimeError> {
        {
            let sessions = self.sessions.read().await;
            if sessions.len() >= self.config.max_sessions {
                return Err(RuntimeError::CapacityExceeded);
            }
        }

        let session_id = Uuid::new_v4();
        self.provider.start_session(session_id, &config).await?;
        let session = Arc::new(Session::new(session_id));

        {
            let mut sessions = self.sessions.write().await;
            if sessions.len() >= self.config.max_sessions {
                drop(sessions);
                let _ = self.provider.end_session(session_id).await;
                return Err(RuntimeError::CapacityExceeded);
            }
            sessions.insert(session_id, session.clone());
        }

        session.apply(VoiceTransition::StartListening).await?;
        let provider = self.capabilities().name;
        self.publish(&session, |sequence| VoiceEvent::SessionStarted {
            session_id,
            sequence,
            provider,
            state: VoiceState::Listening,
        });
        tracing::info!(%session_id, "voice session started");
        Ok(session_id)
    }

    pub async fn send_audio(
        &self,
        session_id: SessionId,
        pcm16: Vec<u8>,
    ) -> Result<usize, RuntimeError> {
        let session = self.session(session_id).await?;
        let state = session.state().await;
        if matches!(state, VoiceState::Closed | VoiceState::Failed) {
            return Err(RuntimeError::SessionClosed);
        }
        if pcm16.is_empty() || !pcm16.len().is_multiple_of(2) {
            return Err(RuntimeError::InvalidAudio);
        }

        let previous = session.audio_bytes.fetch_add(pcm16.len(), Ordering::SeqCst);
        let total = previous + pcm16.len();
        if total > self.config.max_audio_bytes {
            session.audio_bytes.fetch_sub(pcm16.len(), Ordering::SeqCst);
            return Err(RuntimeError::AudioTooLarge);
        }

        let bytes = pcm16.len();
        if let Err(err) = self.provider.send_audio(session_id, pcm16).await {
            session.audio_bytes.fetch_sub(bytes, Ordering::SeqCst);
            self.fail(&session, &err).await;
            return Err(err.into());
        }
        session.touch().await;
        self.publish(&session, |sequence| VoiceEvent::AudioAccepted {
            session_id,
            sequence,
            bytes,
            total_bytes: total,
        });
        Ok(total)
    }

    pub async fn speak(&self, session_id: SessionId, text: String) -> Result<(), RuntimeError> {
        let session = self.session(session_id).await?;
        if session.state().await == VoiceState::Closed {
            return Err(RuntimeError::SessionClosed);
        }
        let text = text.trim().to_string();
        if text.is_empty() {
            return Err(RuntimeError::InvalidText);
        }
        if text.len() > self.config.max_text_bytes {
            return Err(RuntimeError::TextTooLarge);
        }

        session.apply(VoiceTransition::StartSpeaking).await?;
        if let Err(err) = self.provider.speak(session_id, text).await {
            self.fail(&session, &err).await;
            return Err(err.into());
        }
        session.touch().await;
        self.publish(&session, |sequence| VoiceEvent::SpeechStarted {
            session_id,
            sequence,
        });
        Ok(())
    }

    pub async fn interrupt(&self, session_id: SessionId) -> Result<bool, RuntimeError> {
        let session = self.session(session_id).await?;
        let changed = match session.apply(VoiceTransition::Interrupt).await {
            Ok(changed) => changed,
            Err(_) => return Ok(false),
        };
        if !changed {
            return Ok(false);
        }
        if let Err(err) = self.provider.cancel_speech(session_id).await {
            self.fail(&session, &err).await;
            return Err(err.into());
        }
        session.touch().await;
        self.publish(&session, |sequence| VoiceEvent::SpeechInterrupted {
            session_id,
            sequence,
        });
        Ok(true)
    }

    pub async fn end_session(
        &self,
        session_id: SessionId,
        reason: CompletionReason,
    ) -> Result<(), RuntimeError> {
        let session = self.session(session_id).await?;
        if let Err(err) = self.provider.end_session(session_id).await {
            self.fail(&session, &err).await;
        }
        let _ = session.apply(VoiceTransition::Close).await;
        {
            let mut sessions = self.sessions.write().await;
            sessions.remove(&session_id);
        }
        self.publish(&session, |sequence| VoiceEvent::SessionCompleted {
            session_id,
            sequence,
            reason: reason.clone(),
        });
        tracing::info!(%session_id, ?reason, "voice session ended");
        Ok(())
    }

    /// Polls up to `max_events` normalized provider events for a session.
    pub async fn poll_events(
        &self,
        session_id: SessionId,
        max_events: usize,
    ) -> Result<Vec<VoiceEvent>, RuntimeError> {
        let session = self.session(session_id).await?;
        let mut emitted = Vec::new();
        for _ in 0..max_events {
            let Some(event) = self.provider.poll_event(session_id).await? else {
                break;
            };
            if let Some(event) = self.handle_provider_event(&session, event).await {
                emitted.push(event);
            }
        }
        session.touch().await;
        Ok(emitted)
    }

    /// Closes sessions that have been idle longer than `idle_timeout`.
    pub async fn sweep_idle(&self) -> Vec<SessionId> {
        let sessions: Vec<Arc<Session>> = {
            let guard = self.sessions.read().await;
            guard.values().cloned().collect()
        };
        let mut expired = Vec::new();
        for session in sessions {
            if session.idle_for().await < self.config.idle_timeout {
                continue;
            }
            let _ = self.provider.end_session(session.id).await;
            let _ = session.apply(VoiceTransition::Close).await;
            {
                let mut guard = self.sessions.write().await;
                guard.remove(&session.id);
            }
            self.publish(&session, |sequence| VoiceEvent::SessionTimedOut {
                session_id: session.id,
                sequence,
            });
            expired.push(session.id);
        }
        expired
    }

    async fn session(&self, session_id: SessionId) -> Result<Arc<Session>, RuntimeError> {
        self.sessions
            .read()
            .await
            .get(&session_id)
            .cloned()
            .ok_or(RuntimeError::SessionNotFound)
    }

    async fn handle_provider_event(
        &self,
        session: &Arc<Session>,
        event: ProviderEvent,
    ) -> Option<VoiceEvent> {
        let session_id = session.id;
        match event {
            ProviderEvent::Ready => {
                let _ = session.apply(VoiceTransition::StartListening).await;
                Some(self.publish(session, |sequence| VoiceEvent::SessionStatus {
                    session_id,
                    sequence,
                    state: VoiceState::Listening,
                }))
            }
            ProviderEvent::SpeechStarted => {
                let _ = session.apply(VoiceTransition::StartSpeaking).await;
                Some(self.publish(session, |sequence| VoiceEvent::SpeechStarted {
                    session_id,
                    sequence,
                }))
            }
            ProviderEvent::SpeechEnded => {
                let _ = session.apply(VoiceTransition::FinishSpeaking).await;
                Some(self.publish(session, |sequence| VoiceEvent::SpeechEnded {
                    session_id,
                    sequence,
                }))
            }
            ProviderEvent::SpeechInterrupted => {
                let _ = session.apply(VoiceTransition::Interrupt).await;
                Some(
                    self.publish(session, |sequence| VoiceEvent::SpeechInterrupted {
                        session_id,
                        sequence,
                    }),
                )
            }
            ProviderEvent::TranscriptPartial(text) => {
                let turn = session.turn.load(Ordering::SeqCst);
                Some(self.publish(session, |sequence| VoiceEvent::Transcript {
                    session_id,
                    sequence,
                    turn,
                    text,
                    is_final: false,
                    confidence: None,
                }))
            }
            ProviderEvent::TranscriptFinal { text, confidence } => {
                let turn = session.turn.fetch_add(1, Ordering::SeqCst) + 1;
                Some(self.publish(session, |sequence| VoiceEvent::Transcript {
                    session_id,
                    sequence,
                    turn,
                    text,
                    is_final: true,
                    confidence,
                }))
            }
            ProviderEvent::Audio(pcm16) => {
                Some(self.publish(session, |sequence| VoiceEvent::AudioChunk {
                    session_id,
                    sequence,
                    pcm16,
                }))
            }
            ProviderEvent::ToolRequest { name, arguments } => {
                Some(self.publish(session, |sequence| VoiceEvent::ToolRequest {
                    session_id,
                    sequence,
                    name,
                    arguments,
                }))
            }
            ProviderEvent::Error {
                code,
                message,
                retryable,
            } => {
                let _ = session.apply(VoiceTransition::Fail).await;
                let provider = self.capabilities().name;
                Some(self.publish(session, |sequence| VoiceEvent::ProviderError {
                    session_id,
                    sequence,
                    provider,
                    code,
                    message,
                    retryable,
                }))
            }
            ProviderEvent::Completed => {
                let _ = session.apply(VoiceTransition::Close).await;
                Some(
                    self.publish(session, |sequence| VoiceEvent::SessionCompleted {
                        session_id,
                        sequence,
                        reason: CompletionReason::ProviderCompleted,
                    }),
                )
            }
        }
    }

    async fn fail(&self, session: &Session, err: &ProviderError) {
        let session_id = session.id;
        let provider = self.capabilities().name;
        let code = err.code().to_string();
        let message = err.to_string();
        let retryable = err.retryable();
        let _ = self.events.send(VoiceEvent::ProviderError {
            session_id,
            sequence: session.next_sequence(),
            provider,
            code,
            message,
            retryable,
        });
        let _ = session.apply(VoiceTransition::Fail).await;
    }

    fn publish(&self, session: &Session, build: impl FnOnce(u64) -> VoiceEvent) -> VoiceEvent {
        let event = build(session.next_sequence());
        let _ = self.events.send(event.clone());
        event
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mock::MockVoiceProvider;

    fn runtime(max_audio_bytes: usize) -> VoiceRuntime {
        VoiceRuntime::new(
            Arc::new(MockVoiceProvider::new()),
            RuntimeConfig {
                max_audio_bytes,
                idle_timeout: Duration::from_millis(20),
                ..RuntimeConfig::default()
            },
        )
    }

    async fn next_event(rx: &mut broadcast::Receiver<VoiceEvent>) -> VoiceEvent {
        tokio::time::timeout(Duration::from_secs(1), rx.recv())
            .await
            .expect("timed out waiting for event")
            .expect("event channel closed")
    }

    #[tokio::test]
    async fn starts_polls_and_ends_session() {
        let runtime = runtime(1024);
        let mut rx = runtime.subscribe();
        let id = runtime
            .start_session(SessionConfig {
                language: "en".to_string(),
                ..SessionConfig::default()
            })
            .await
            .unwrap();
        assert_eq!(runtime.active_sessions().await, 1);
        assert_eq!(
            runtime.session_state(id).await.unwrap(),
            VoiceState::Listening
        );
        let started = next_event(&mut rx).await;
        assert_eq!(started.sequence(), 1);

        runtime.send_audio(id, vec![0u8; 32]).await.unwrap();
        let events = runtime.poll_events(id, 8).await.unwrap();
        assert!(events
            .iter()
            .any(|event| matches!(event, VoiceEvent::Transcript { is_final: true, .. })));
        assert!(events
            .iter()
            .any(|event| matches!(event, VoiceEvent::SpeechEnded { .. })));

        runtime
            .end_session(id, CompletionReason::ClientEnded)
            .await
            .unwrap();
        assert_eq!(runtime.active_sessions().await, 0);
        let mut completed = false;
        for _ in 0..32 {
            if matches!(
                next_event(&mut rx).await,
                VoiceEvent::SessionCompleted {
                    reason: CompletionReason::ClientEnded,
                    ..
                }
            ) {
                completed = true;
                break;
            }
        }
        assert!(completed, "expected a client-ended completion event");
    }

    #[tokio::test]
    async fn audio_limits_and_malformed_audio_are_rejected() {
        let runtime = runtime(4);
        let id = runtime
            .start_session(SessionConfig::default())
            .await
            .unwrap();
        assert!(matches!(
            runtime.send_audio(id, vec![0u8; 3]).await,
            Err(RuntimeError::InvalidAudio)
        ));
        runtime.send_audio(id, vec![0u8; 4]).await.unwrap();
        assert!(matches!(
            runtime.send_audio(id, vec![0u8; 2]).await,
            Err(RuntimeError::AudioTooLarge)
        ));
    }

    #[tokio::test]
    async fn speak_and_interrupt_are_sequenced() {
        let runtime = runtime(1024);
        let mut rx = runtime.subscribe();
        let id = runtime
            .start_session(SessionConfig::default())
            .await
            .unwrap();
        let _ = next_event(&mut rx).await;

        runtime.speak(id, "hello".to_string()).await.unwrap();
        assert_eq!(
            runtime.session_state(id).await.unwrap(),
            VoiceState::Speaking
        );
        assert_eq!(next_event(&mut rx).await.sequence(), 2);

        runtime.interrupt(id).await.unwrap();
        assert_eq!(
            runtime.session_state(id).await.unwrap(),
            VoiceState::Interrupted
        );
        let event = next_event(&mut rx).await;
        assert!(matches!(event, VoiceEvent::SpeechInterrupted { .. }));
        assert!(!runtime.interrupt(id).await.unwrap());
    }

    #[tokio::test]
    async fn concurrent_sessions_do_not_share_state_or_sequence() {
        let runtime = Arc::new(runtime(1024));
        let first = runtime
            .start_session(SessionConfig::default())
            .await
            .unwrap();
        let second = runtime
            .start_session(SessionConfig::default())
            .await
            .unwrap();
        assert_ne!(first, second);

        let runtime_a = runtime.clone();
        let runtime_b = runtime.clone();
        let (a, b) = tokio::join!(
            async move {
                runtime_a.send_audio(first, vec![0u8; 8]).await.unwrap();
                runtime_a.poll_events(first, 8).await.unwrap()
            },
            async move {
                runtime_b.send_audio(second, vec![0u8; 16]).await.unwrap();
                runtime_b.poll_events(second, 8).await.unwrap()
            }
        );
        assert!(a.iter().all(|event| event.session_id() == first));
        assert!(b.iter().all(|event| event.session_id() == second));
        assert_eq!(runtime.active_sessions().await, 2);
    }

    #[tokio::test]
    async fn idle_sessions_are_swept() {
        let runtime = runtime(1024);
        let mut rx = runtime.subscribe();
        let id = runtime
            .start_session(SessionConfig::default())
            .await
            .unwrap();
        let _ = next_event(&mut rx).await;
        tokio::time::sleep(Duration::from_millis(30)).await;
        let expired = runtime.sweep_idle().await;
        assert_eq!(expired, vec![id]);
        assert_eq!(runtime.active_sessions().await, 0);
        let event = next_event(&mut rx).await;
        assert!(matches!(event, VoiceEvent::SessionTimedOut { .. }));
    }

    #[tokio::test]
    async fn session_capacity_is_enforced() {
        let runtime = VoiceRuntime::new(
            Arc::new(MockVoiceProvider::new()),
            RuntimeConfig {
                max_sessions: 1,
                ..RuntimeConfig::default()
            },
        );
        let _ = runtime
            .start_session(SessionConfig::default())
            .await
            .unwrap();
        assert!(matches!(
            runtime.start_session(SessionConfig::default()).await,
            Err(RuntimeError::CapacityExceeded)
        ));
    }

    #[tokio::test]
    async fn unknown_and_closed_sessions_are_rejected() {
        let runtime = runtime(1024);
        let unknown = Uuid::new_v4();
        assert!(matches!(
            runtime.send_audio(unknown, vec![0u8; 2]).await,
            Err(RuntimeError::SessionNotFound)
        ));
        let id = runtime
            .start_session(SessionConfig::default())
            .await
            .unwrap();
        runtime
            .end_session(id, CompletionReason::ClientEnded)
            .await
            .unwrap();
        assert!(matches!(
            runtime.send_audio(id, vec![0u8; 2]).await,
            Err(RuntimeError::SessionNotFound)
        ));
    }

    #[tokio::test]
    async fn text_limits_are_enforced() {
        let runtime = VoiceRuntime::new(
            Arc::new(MockVoiceProvider::new()),
            RuntimeConfig {
                max_text_bytes: 4,
                ..RuntimeConfig::default()
            },
        );
        let id = runtime
            .start_session(SessionConfig::default())
            .await
            .unwrap();
        assert!(matches!(
            runtime.speak(id, "     ".to_string()).await,
            Err(RuntimeError::InvalidText)
        ));
        assert!(matches!(
            runtime.speak(id, "too long".to_string()).await,
            Err(RuntimeError::TextTooLarge)
        ));
    }
}
