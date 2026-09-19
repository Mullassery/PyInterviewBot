//! Browser-facing WebSocket protocol.
//!
//! Outbound to the browser:
//!   - text (JSON) control frames: {"type":"state","state":"..."} |
//!     {"type":"interrupt"} | {"type":"error","message":"..."}
//!   - binary frames: raw PCM16LE mono 22050Hz TTS audio to play
//! Inbound from the browser:
//!   - binary frames: raw PCM16LE mono 16kHz microphone audio
//!
//! There is deliberately no message type for a transcript, a score, or any
//! evaluation detail — the protocol itself can't leak that to the candidate
//! client, independent of what the UI chooses to render (spec §23).

use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        State,
    },
    response::IntoResponse,
    routing::get,
    Router,
};
use futures_util::{
    stream::{SplitSink, SplitStream},
    SinkExt, StreamExt,
};
use std::time::Duration;

use tracing::{error, info, warn};

use crate::ai_client::{AiClient, AiClientError};
use crate::session::Session;
use crate::state_machine::{transition, Event};
use crate::vad::VadEvent;

const FALLBACK_PHRASE: &str = "Sorry, I didn't quite catch that. Could you say that again?";

/// Must match ai-service's `tts.SAMPLE_RATE`. Piper synthesizes faster than
/// real-time, so `speak()` can't treat "all TTS bytes pushed to the socket"
/// as "the candidate has finished hearing this" — it has to account for how
/// long the audio it already sent will actually take to play out.
const TTS_SAMPLE_RATE_HZ: u64 = 22_050;
const TTS_BYTES_PER_SEC: u64 = TTS_SAMPLE_RATE_HZ * 2; // mono, 16-bit

fn audio_duration(bytes_sent: u64) -> Duration {
    Duration::from_secs_f64(bytes_sent as f64 / TTS_BYTES_PER_SEC as f64)
}

#[derive(Clone)]
pub struct AppState {
    pub ai: AiClient,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/ws/interview", get(ws_upgrade))
        .with_state(state)
}

async fn ws_upgrade(ws: WebSocketUpgrade, State(state): State<AppState>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_socket(socket, state))
}

type Sink = SplitSink<WebSocket, Message>;
type Stream = SplitStream<WebSocket>;

enum SpeakOutcome {
    Finished,
    Interrupted,
    Closed,
}

enum TurnOutcome {
    TurnEnded,
    Closed,
}

async fn handle_socket(socket: WebSocket, app: AppState) {
    let (mut sink, mut stream) = socket.split();

    let start = match app.ai.start_session().await {
        Ok(s) => s,
        Err(e) => {
            error!(?e, "failed to start ai-service session");
            let _ = send_error(&mut sink, "Could not start the interview right now.").await;
            return;
        }
    };

    let mut session = Session::new(start.session_id);
    info!(session_id = %session.id, ai_session_id = %session.ai_session_id, "interview session started");

    session.state = apply(&mut session, Event::StartSpeaking);
    let mut next_text = Some(start.question_text);

    loop {
        let Some(text) = next_text.take() else {
            break;
        };

        send_state_with_text(&mut sink, &session, Some(&text)).await;
        match speak(&app.ai, &mut session, &mut sink, &mut stream, &text).await {
            Ok(SpeakOutcome::Finished) => {
                info!("speak outcome: Finished (no barge-in)");
                session.state = apply(&mut session, Event::FinishedSpeaking);
            }
            Ok(SpeakOutcome::Interrupted) => {
                info!(utterance_buf_len = session.utterance_buf.len(), "speak outcome: Interrupted (barge-in)");
                session.state = apply(&mut session, Event::BargeIn);
                let _ = sink.send(Message::Text(r#"{"type":"interrupt"}"#.into())).await;
                session.state = apply(&mut session, Event::CandidateVoiceDetected);
            }
            Ok(SpeakOutcome::Closed) => return,
            Err(e) => {
                warn!(?e, "tts/ai-service failure while speaking");
                session.state = apply(&mut session, Event::Fault);
                match speak_recovery(&app.ai, &mut session, &mut sink, &mut stream).await {
                    Ok(true) => {}
                    Ok(false) => return,
                    Err(_) => return,
                }
            }
        }
        send_state(&mut sink, &session).await;

        match listen_for_turn(&mut session, &mut stream).await {
            TurnOutcome::Closed => return,
            TurnOutcome::TurnEnded => {}
        }

        session.state = apply(&mut session, Event::CandidateTurnEnded);
        send_state(&mut sink, &session).await;

        let utterance = std::mem::take(&mut session.utterance_buf);
        info!(utterance_bytes = utterance.len(), "sending utterance for transcription");
        session.vad.reset_turn();

        let transcript = match app.ai.transcribe(utterance).await {
            Ok(t) => {
                info!(%t, "transcript");
                t
            }
            Err(e) => {
                warn!(?e, "asr failure");
                session.state = apply(&mut session, Event::Fault);
                match speak_recovery(&app.ai, &mut session, &mut sink, &mut stream).await {
                    Ok(true) => continue,
                    _ => return,
                }
            }
        };

        let decision = match app.ai.agent_turn(session.ai_session_id, &transcript).await {
            Ok(d) => d,
            Err(e) => {
                warn!(?e, "agent turn failure");
                session.state = apply(&mut session, Event::Fault);
                match speak_recovery(&app.ai, &mut session, &mut sink, &mut stream).await {
                    Ok(true) => continue,
                    _ => return,
                }
            }
        };

        session.turn_number += 1;
        session.state = apply(&mut session, Event::TurnDecided); // Processing -> FollowUpDecision

        if decision.is_complete {
            session.state = apply(&mut session, Event::StartSpeaking); // -> AiResponding
            send_state_with_text(&mut sink, &session, Some(&decision.ai_text)).await;
            let _ = speak(&app.ai, &mut session, &mut sink, &mut stream, &decision.ai_text).await;
            session.state = apply(&mut session, Event::InterviewComplete);
            send_state(&mut sink, &session).await;
            return;
        }

        session.state = apply(&mut session, Event::StartSpeaking); // -> AiResponding
        next_text = Some(decision.ai_text);
    }
}

/// Streams `text` to the candidate as synthesized speech while
/// simultaneously watching incoming mic audio for a barge-in. Returns as
/// soon as either the speech finishes, the candidate starts talking over
/// it, or the connection closes.
async fn speak(
    ai: &AiClient,
    session: &mut Session,
    sink: &mut Sink,
    stream: &mut Stream,
    text: &str,
) -> Result<SpeakOutcome, AiClientError> {
    let tts_stream = ai.synthesize_stream(text).await?;
    futures_util::pin_mut!(tts_stream);

    let started_at = tokio::time::Instant::now();
    let mut audio_bytes_sent: u64 = 0;

    // Phase 1: forward TTS bytes as Piper produces them, watching incoming
    // audio for a barge-in the whole time.
    loop {
        tokio::select! {
            biased;

            incoming = stream.next() => {
                match incoming {
                    Some(Ok(Message::Binary(bytes))) => {
                        let events = session.vad.push_pcm16_capturing(&bytes, &mut session.utterance_buf);
                        if events.contains(&VadEvent::SpeechStarted) {
                            return Ok(SpeakOutcome::Interrupted);
                        }
                    }
                    Some(Ok(Message::Close(_))) | None => return Ok(SpeakOutcome::Closed),
                    Some(Err(e)) => {
                        warn!(?e, "ws read error while speaking");
                        return Ok(SpeakOutcome::Closed);
                    }
                    _ => {}
                }
            }

            chunk = tts_stream.next() => {
                match chunk {
                    Some(Ok(bytes)) => {
                        audio_bytes_sent += bytes.len() as u64;
                        if sink.send(Message::Binary(bytes)).await.is_err() {
                            return Ok(SpeakOutcome::Closed);
                        }
                    }
                    Some(Err(e)) => return Err(AiClientError::Request(e)),
                    None => break,
                }
            }
        }
    }

    // Phase 2: all TTS bytes are on the wire, but Piper synthesizes faster
    // than real-time — the candidate is likely still listening. Keep
    // watching for a barge-in until the audio we sent would actually have
    // finished playing.
    loop {
        let elapsed = started_at.elapsed();
        let total = audio_duration(audio_bytes_sent);
        if elapsed >= total {
            return Ok(SpeakOutcome::Finished);
        }
        let remaining = total - elapsed;

        tokio::select! {
            biased;

            incoming = stream.next() => {
                match incoming {
                    Some(Ok(Message::Binary(bytes))) => {
                        let events = session.vad.push_pcm16_capturing(&bytes, &mut session.utterance_buf);
                        if events.contains(&VadEvent::SpeechStarted) {
                            return Ok(SpeakOutcome::Interrupted);
                        }
                    }
                    Some(Ok(Message::Close(_))) | None => return Ok(SpeakOutcome::Closed),
                    Some(Err(e)) => {
                        warn!(?e, "ws read error while speaking");
                        return Ok(SpeakOutcome::Closed);
                    }
                    _ => {}
                }
            }

            _ = tokio::time::sleep(remaining) => {
                return Ok(SpeakOutcome::Finished);
            }
        }
    }
}

/// Buffers candidate audio (continuing to accumulate from any barge-in
/// preroll already captured by `speak`) until the VAD's end-of-turn
/// heuristic fires.
async fn listen_for_turn(session: &mut Session, stream: &mut Stream) -> TurnOutcome {
    while let Some(msg) = stream.next().await {
        match msg {
            Ok(Message::Binary(bytes)) => {
                let events = session
                    .vad
                    .push_pcm16_capturing(&bytes, &mut session.utterance_buf);
                // A barge-in already moved us into CandidateSpeaking before
                // this function was called; a plain (non-interrupted) turn
                // is still sitting in Listening until real speech starts.
                if events.contains(&VadEvent::SpeechStarted)
                    && session.state == crate::state_machine::SessionState::Listening
                {
                    session.state = apply(session, Event::CandidateVoiceDetected);
                }
                if events.contains(&VadEvent::TurnEnded) {
                    return TurnOutcome::TurnEnded;
                }
            }
            Ok(Message::Close(_)) => return TurnOutcome::Closed,
            Err(e) => {
                warn!(?e, "ws read error while listening");
                return TurnOutcome::Closed;
            }
            _ => {}
        }
    }
    TurnOutcome::Closed
}

/// Speaks the fallback phrase per §28 ("never expose raw technical errors")
/// and returns to a normal listening state. `Ok(true)` means recovery
/// succeeded and the caller should keep going; `Ok(false)`/`Err` mean the
/// connection should be closed.
async fn speak_recovery(
    ai: &AiClient,
    session: &mut Session,
    sink: &mut Sink,
    stream: &mut Stream,
) -> Result<bool, ()> {
    send_state(sink, session).await;
    session.state = apply(session, Event::StartSpeaking); // ErrorRecovery -> AiSpeaking
    match speak(ai, session, sink, stream, FALLBACK_PHRASE).await {
        Ok(SpeakOutcome::Finished) => {
            session.state = apply(session, Event::FinishedSpeaking);
            send_state(sink, session).await;
            Ok(true)
        }
        Ok(SpeakOutcome::Interrupted) => {
            session.state = apply(session, Event::BargeIn);
            let _ = sink.send(Message::Text(r#"{"type":"interrupt"}"#.into())).await;
            session.state = apply(session, Event::CandidateVoiceDetected);
            send_state(sink, session).await;
            Ok(true)
        }
        Ok(SpeakOutcome::Closed) => Ok(false),
        Err(e) => {
            error!(?e, "ai-service unavailable during error recovery, closing session");
            let _ = send_error(sink, "We're having trouble right now. Please refresh and try again.").await;
            Err(())
        }
    }
}

/// Applies a state machine event and logs+swallows the (unreachable in
/// practice) illegal-transition case rather than panicking a live session.
fn apply(session: &mut Session, event: Event) -> crate::state_machine::SessionState {
    match transition(session.state, event) {
        Ok(next) => next,
        Err(err) => {
            error!(%err, "illegal state transition — this is a bug, staying in current state");
            session.state
        }
    }
}

async fn send_state(sink: &mut Sink, session: &Session) {
    send_state_with_text(sink, session, None).await;
}

/// Includes the AI's own question/response text when transitioning into
/// AI_SPEAKING. This is the AI's *own* generated speech, already being
/// spoken aloud — not the candidate's transcript — so surfacing it as
/// on-screen captions (per the §22 mockup) doesn't violate §23.
async fn send_state_with_text(sink: &mut Sink, session: &Session, text: Option<&str>) {
    let mut payload = serde_json::json!({
        "type": "state",
        "state": state_label(session.state),
        "turn": session.turn_number,
    });
    if let Some(t) = text {
        payload["text"] = serde_json::Value::String(t.to_string());
    }
    let _ = sink.send(Message::Text(payload.to_string().into())).await;
}

async fn send_error(sink: &mut Sink, message: &str) -> Result<(), axum::Error> {
    let payload = serde_json::json!({ "type": "error", "message": message });
    sink.send(Message::Text(payload.to_string().into())).await
}

fn state_label(state: crate::state_machine::SessionState) -> &'static str {
    use crate::state_machine::SessionState::*;
    match state {
        Initializing => "initializing",
        AiSpeaking | AiResponding => "ai_speaking",
        Listening => "listening",
        CandidateSpeaking | Interrupted => "listening",
        Processing | FollowUpDecision => "processing",
        Paused => "paused",
        Completed => "completed",
        ErrorRecovery => "processing",
    }
}
