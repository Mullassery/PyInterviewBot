//! Voice activity detection and end-of-turn heuristic.
//!
//! Wraps `earshot` (pure-Rust neural VAD) and turns its per-frame voice
//! probability into two events a session cares about: "the candidate has
//! started talking" (used for barge-in) and "the candidate has stopped
//! talking" (used to close out a turn and send it for transcription).
//!
//! This is a simplified stand-in for the full cadence/sentence-completion
//! aware end-of-turn model described in the product spec (§7) — it is a
//! fixed silence-timeout heuristic, not a linguistic one. That's a
//! deliberate, documented scope cut for this slice, not a hidden shortcut.

use std::collections::VecDeque;

const FRAME_SAMPLES: usize = 256; // 16ms @ 16kHz mono, required by earshot
const FRAME_MS: u32 = 16;
const MIN_SPEECH_MS_TO_COUNT: u32 = 200;
const SILENCE_MS_TO_END_TURN: u32 = 700;
/// How much audio to keep on hand before speech is confirmed, so the first
/// ~300ms of an utterance isn't clipped off while we wait to be sure it's
/// really speech and not a click or breath.
const MAX_PREROLL_BYTES: usize = 9600; // 300ms @ 16kHz mono 16-bit
/// earshot's own docs: "scores over 0.5 can generally be considered voice".
const VOICE_SCORE_THRESHOLD: f32 = 0.5;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VadEvent {
    SpeechStarted,
    TurnEnded,
}

pub struct TurnDetector {
    detector: earshot::Detector,
    frame_buf: Vec<i16>,
    leftover_byte: Option<u8>,
    speech_run_ms: u32,
    silence_run_ms: u32,
    in_speech: bool,
    preroll: VecDeque<u8>,
    /// Whether confirmed speech has been seen this turn — once true, all
    /// further bytes are appended straight to the caller's capture buffer
    /// instead of the preroll ring.
    capturing: bool,
}

impl Default for TurnDetector {
    fn default() -> Self {
        Self {
            detector: earshot::Detector::default(),
            frame_buf: Vec::with_capacity(FRAME_SAMPLES),
            leftover_byte: None,
            speech_run_ms: 0,
            silence_run_ms: 0,
            in_speech: false,
            preroll: VecDeque::with_capacity(MAX_PREROLL_BYTES),
            capturing: false,
        }
    }
}

impl TurnDetector {
    /// Feed raw little-endian PCM16 mono 16kHz bytes as they arrive from the
    /// browser. Returns any VAD events that fired as a result.
    pub fn push_pcm16(&mut self, bytes: &[u8]) -> Vec<VadEvent> {
        let mut events = Vec::new();

        let mut iter = bytes.iter().copied();
        if let Some(lo) = self.leftover_byte.take() {
            if let Some(hi) = iter.next() {
                self.frame_buf.push(i16::from_le_bytes([lo, hi]));
            } else {
                self.leftover_byte = Some(lo);
                return events;
            }
        }
        while let Some(lo) = iter.next() {
            match iter.next() {
                Some(hi) => self.frame_buf.push(i16::from_le_bytes([lo, hi])),
                None => {
                    self.leftover_byte = Some(lo);
                    break;
                }
            }
        }

        while self.frame_buf.len() >= FRAME_SAMPLES {
            let frame: Vec<i16> = self.frame_buf.drain(0..FRAME_SAMPLES).collect();
            let score = self.detector.predict_i16(&frame);
            if let Some(ev) = self.observe_frame(score >= VOICE_SCORE_THRESHOLD) {
                events.push(ev);
            }
        }

        events
    }

    fn observe_frame(&mut self, is_voice: bool) -> Option<VadEvent> {
        if is_voice {
            self.silence_run_ms = 0;
            self.speech_run_ms += FRAME_MS;
            if !self.in_speech && self.speech_run_ms >= MIN_SPEECH_MS_TO_COUNT {
                self.in_speech = true;
                return Some(VadEvent::SpeechStarted);
            }
        } else if self.in_speech {
            self.silence_run_ms += FRAME_MS;
            if self.silence_run_ms >= SILENCE_MS_TO_END_TURN {
                self.in_speech = false;
                self.speech_run_ms = 0;
                self.silence_run_ms = 0;
                return Some(VadEvent::TurnEnded);
            }
        } else {
            self.speech_run_ms = 0;
        }
        None
    }

    /// Reset all running counters for a fresh turn (called after handing a
    /// completed utterance off for transcription, or after a barge-in).
    pub fn reset_turn(&mut self) {
        self.speech_run_ms = 0;
        self.silence_run_ms = 0;
        self.in_speech = false;
        self.capturing = false;
        self.preroll.clear();
    }

    /// Feeds audio in and, once real speech is confirmed, appends bytes
    /// (including the ~300ms preroll leading up to the confirmation) into
    /// `capture`. Before speech is confirmed, bytes are only held in an
    /// internal ring buffer and nothing is written to `capture` — this is
    /// what lets the same call site be used both while listening for a
    /// barge-in during AI speech and while capturing a candidate's answer.
    pub fn push_pcm16_capturing(&mut self, bytes: &[u8], capture: &mut Vec<u8>) -> Vec<VadEvent> {
        if self.capturing {
            capture.extend_from_slice(bytes);
        } else {
            for &b in bytes {
                if self.preroll.len() >= MAX_PREROLL_BYTES {
                    self.preroll.pop_front();
                }
                self.preroll.push_back(b);
            }
        }

        let events = self.push_pcm16(bytes);

        if !self.capturing && events.contains(&VadEvent::SpeechStarted) {
            capture.extend(self.preroll.drain(..));
            self.capturing = true;
        }

        if !events.is_empty() {
            tracing::debug!(?events, capture_len = capture.len(), "vad event");
        }

        events
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn silence_bytes(ms: u32) -> Vec<u8> {
        let samples = (16 * ms) as usize; // 16 samples/ms @ 16kHz
        vec![0u8; samples * 2]
    }

    #[test]
    fn pure_silence_emits_no_events() {
        let mut d = TurnDetector::default();
        let events = d.push_pcm16(&silence_bytes(500));
        assert!(events.is_empty());
    }

    #[test]
    fn odd_byte_count_is_buffered_not_dropped() {
        let mut d = TurnDetector::default();
        // Push a single stray byte, then complete it later.
        let events = d.push_pcm16(&[0x12]);
        assert!(events.is_empty());
        assert_eq!(d.leftover_byte, Some(0x12));
        let events = d.push_pcm16(&silence_bytes(300));
        assert!(events.is_empty()); // still just silence, but no panic/drop
    }

    #[test]
    fn capturing_stays_empty_until_speech_confirmed() {
        let mut d = TurnDetector::default();
        let mut capture = Vec::new();
        d.push_pcm16_capturing(&silence_bytes(500), &mut capture);
        assert!(capture.is_empty(), "pure silence must never be captured");
    }

    #[test]
    fn preroll_ring_is_bounded() {
        let mut d = TurnDetector::default();
        let mut capture = Vec::new();
        // Push far more silence than the preroll window holds; internal ring
        // must not grow unbounded.
        d.push_pcm16_capturing(&silence_bytes(5_000), &mut capture);
        assert!(d.preroll.len() <= MAX_PREROLL_BYTES);
    }

    #[test]
    fn reset_turn_clears_counters() {
        let mut d = TurnDetector::default();
        d.speech_run_ms = 500;
        d.silence_run_ms = 200;
        d.in_speech = true;
        d.reset_turn();
        assert_eq!(d.speech_run_ms, 0);
        assert_eq!(d.silence_run_ms, 0);
        assert!(!d.in_speech);
    }
}
