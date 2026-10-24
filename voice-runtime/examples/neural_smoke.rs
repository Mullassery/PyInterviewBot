use voice_runtime::{AudioFormat, NeuralEngine, SpeechEngine};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let Some(engine) = NeuralEngine::from_env()? else {
        eprintln!("set NEURAL_TTS_MODEL, NEURAL_TTS_TOKENS, NEURAL_ASR_ENCODER, NEURAL_ASR_DECODER, NEURAL_ASR_TOKENS (and optionally NEURAL_TTS_LEXICON)");
        std::process::exit(2);
    };
    let audio = engine
        .synthesize(
            "Hello from the native neural engine.",
            "0",
            AudioFormat::tts(),
        )
        .await?;
    let output = std::env::temp_dir().join("neural_smoke.wav");
    std::fs::write(&output, audio.to_wav())?;
    eprintln!("wrote {}", output.display());
    let text = engine.transcribe(&audio, "en").await?;
    println!("transcript: {text}");
    Ok(())
}
