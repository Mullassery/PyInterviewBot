//! Deterministic mock voice provider for CI, tests, and local runtime demos.

use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;

use crate::provider::{
    ProviderCapabilities, ProviderError, ProviderEvent, ProviderFuture, SessionConfig, SessionId,
    VoiceProvider,
};

#[derive(Default)]
pub struct MockVoiceProvider {
    sessions: Mutex<HashMap<SessionId, VecDeque<ProviderEvent>>>,
}

impl MockVoiceProvider {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn pending_events(&self, session_id: SessionId) -> usize {
        self.sessions
            .lock()
            .expect("mock provider lock poisoned")
            .get(&session_id)
            .map_or(0, VecDeque::len)
    }
}

fn locked<T>(value: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    value.lock().expect("mock provider lock poisoned")
}

impl VoiceProvider for MockVoiceProvider {
    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities {
            name: "mock".to_string(),
            local: true,
            streaming_asr: true,
            streaming_tts: true,
            languages: vec!["en".to_string()],
            max_text_bytes: 4096,
        }
    }

    fn health(&self) -> ProviderFuture<'_, bool> {
        Box::pin(async { Ok(true) })
    }

    fn start_session(
        &self,
        session_id: SessionId,
        _config: &SessionConfig,
    ) -> ProviderFuture<'_, ()> {
        Box::pin(async move {
            locked(&self.sessions)
                .entry(session_id)
                .or_insert_with(|| VecDeque::from([ProviderEvent::Ready]));
            Ok(())
        })
    }

    fn send_audio(&self, session_id: SessionId, pcm16: Vec<u8>) -> ProviderFuture<'_, ()> {
        Box::pin(async move {
            if pcm16.is_empty() || !pcm16.len().is_multiple_of(2) {
                return Err(ProviderError::InvalidInput(
                    "PCM16 audio must be non-empty and even-length".to_string(),
                ));
            }
            let mut sessions = locked(&self.sessions);
            let queue = sessions
                .get_mut(&session_id)
                .ok_or_else(|| ProviderError::InvalidInput("unknown session".to_string()))?;
            queue.push_back(ProviderEvent::TranscriptFinal {
                text: "deterministic mock transcript".to_string(),
                confidence: Some(1.0),
            });
            queue.push_back(ProviderEvent::SpeechEnded);
            Ok(())
        })
    }

    fn speak(&self, session_id: SessionId, text: String) -> ProviderFuture<'_, ()> {
        Box::pin(async move {
            let mut sessions = locked(&self.sessions);
            let queue = sessions
                .get_mut(&session_id)
                .ok_or_else(|| ProviderError::InvalidInput("unknown session".to_string()))?;
            let audio_bytes = (text.len().clamp(2, 256) / 2) * 2;
            queue.push_back(ProviderEvent::SpeechStarted);
            queue.push_back(ProviderEvent::Audio(vec![0u8; audio_bytes]));
            queue.push_back(ProviderEvent::SpeechEnded);
            Ok(())
        })
    }

    fn cancel_speech(&self, session_id: SessionId) -> ProviderFuture<'_, ()> {
        Box::pin(async move {
            let mut sessions = locked(&self.sessions);
            let queue = sessions
                .get_mut(&session_id)
                .ok_or_else(|| ProviderError::InvalidInput("unknown session".to_string()))?;
            queue.push_back(ProviderEvent::SpeechInterrupted);
            Ok(())
        })
    }

    fn end_session(&self, session_id: SessionId) -> ProviderFuture<'_, ()> {
        Box::pin(async move {
            let mut sessions = locked(&self.sessions);
            let queue = sessions
                .get_mut(&session_id)
                .ok_or_else(|| ProviderError::InvalidInput("unknown session".to_string()))?;
            queue.push_back(ProviderEvent::Completed);
            Ok(())
        })
    }

    fn poll_event(&self, session_id: SessionId) -> ProviderFuture<'_, Option<ProviderEvent>> {
        Box::pin(async move {
            Ok(locked(&self.sessions)
                .get_mut(&session_id)
                .and_then(VecDeque::pop_front))
        })
    }
}
