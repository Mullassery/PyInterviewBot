//! Real local neural speech engine backed by sherpa-onnx (Apache-2.0).
//!
//! Model weights are operator-supplied and are not bundled. The engine is only
//! registered when the required model paths are configured. CPU is the default;
//! a provider such as CoreML can be selected explicitly.

use std::path::PathBuf;
use std::sync::Arc;

use sherpa_onnx::{
    GenerationConfig, OfflineRecognizer, OfflineRecognizerConfig, OfflineTts, OfflineTtsConfig,
    OfflineTtsVitsModelConfig, OfflineWhisperModelConfig,
};

use crate::studio::{
    AudioBuffer, AudioFormat, SpeechEngine, StudioCapabilities, StudioError, StudioFuture,
};

const ASR_SAMPLE_RATE: u32 = 16_000;

#[derive(Debug, Clone)]
struct NeuralPaths {
    tts_model: PathBuf,
    tts_tokens: PathBuf,
    tts_lexicon: Option<PathBuf>,
    asr_encoder: PathBuf,
    asr_decoder: PathBuf,
    asr_tokens: PathBuf,
    num_threads: i32,
    provider: Option<String>,
    speed: f32,
    name: String,
    languages: Vec<String>,
}

impl NeuralPaths {
    fn from_env() -> Result<Option<Self>, StudioError> {
        let get = |key: &str| std::env::var(key).ok().filter(|value| !value.is_empty());
        let tts_model = get("NEURAL_TTS_MODEL");
        let tts_tokens = get("NEURAL_TTS_TOKENS");
        let tts_lexicon = get("NEURAL_TTS_LEXICON");
        let asr_encoder = get("NEURAL_ASR_ENCODER");
        let asr_decoder = get("NEURAL_ASR_DECODER");
        let asr_tokens = get("NEURAL_ASR_TOKENS");

        if tts_model.is_none()
            && tts_tokens.is_none()
            && asr_encoder.is_none()
            && asr_decoder.is_none()
            && asr_tokens.is_none()
        {
            return Ok(None);
        }

        let missing: Vec<&str> = [
            ("NEURAL_TTS_MODEL", &tts_model),
            ("NEURAL_TTS_TOKENS", &tts_tokens),
            ("NEURAL_ASR_ENCODER", &asr_encoder),
            ("NEURAL_ASR_DECODER", &asr_decoder),
            ("NEURAL_ASR_TOKENS", &asr_tokens),
        ]
        .into_iter()
        .filter_map(|(name, value)| if value.is_none() { Some(name) } else { None })
        .collect();

        if !missing.is_empty() {
            return Err(StudioError::EngineUnavailable(format!(
                "incomplete neural model configuration; missing {}",
                missing.join(", ")
            )));
        }

        let paths = Self {
            tts_model: PathBuf::from(tts_model.expect("checked")),
            tts_tokens: PathBuf::from(tts_tokens.expect("checked")),
            tts_lexicon: tts_lexicon.map(PathBuf::from),
            asr_encoder: PathBuf::from(asr_encoder.expect("checked")),
            asr_decoder: PathBuf::from(asr_decoder.expect("checked")),
            asr_tokens: PathBuf::from(asr_tokens.expect("checked")),
            num_threads: std::env::var("NEURAL_NUM_THREADS")
                .ok()
                .and_then(|value| value.parse().ok())
                .unwrap_or(2),
            provider: std::env::var("NEURAL_PROVIDER")
                .ok()
                .filter(|value| !value.is_empty()),
            speed: std::env::var("NEURAL_TTS_SPEED")
                .ok()
                .and_then(|value| value.parse().ok())
                .unwrap_or(1.0),
            name: std::env::var("NEURAL_ENGINE_NAME").unwrap_or_else(|_| "neural".to_string()),
            languages: std::env::var("NEURAL_LANGUAGES")
                .unwrap_or_else(|_| "en".to_string())
                .split(',')
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(str::to_string)
                .collect(),
        };
        paths.validate()?;
        Ok(Some(paths))
    }

    fn validate(&self) -> Result<(), StudioError> {
        for path in [
            Some(&self.tts_model),
            Some(&self.tts_tokens),
            self.tts_lexicon.as_ref(),
            Some(&self.asr_encoder),
            Some(&self.asr_decoder),
            Some(&self.asr_tokens),
        ]
        .into_iter()
        .flatten()
        {
            if !path.is_file() {
                return Err(StudioError::EngineUnavailable(format!(
                    "model file not found: {}",
                    path.display()
                )));
            }
        }
        Ok(())
    }
}

pub struct NeuralEngine {
    tts: Arc<OfflineTts>,
    asr: Arc<OfflineRecognizer>,
    sample_rate: u32,
    speed: f32,
    name: String,
    languages: Vec<String>,
}

impl NeuralEngine {
    pub fn from_env() -> Result<Option<Self>, StudioError> {
        let Some(paths) = NeuralPaths::from_env()? else {
            return Ok(None);
        };

        let mut tts_config = OfflineTtsConfig::default();
        tts_config.model.vits = OfflineTtsVitsModelConfig {
            model: Some(paths.tts_model.to_string_lossy().into_owned()),
            tokens: Some(paths.tts_tokens.to_string_lossy().into_owned()),
            lexicon: paths
                .tts_lexicon
                .as_ref()
                .map(|path| path.to_string_lossy().into_owned()),
            ..Default::default()
        };
        tts_config.model.num_threads = paths.num_threads;
        tts_config.model.provider = paths.provider.clone();
        tts_config.max_num_sentences = 1;
        let tts = OfflineTts::create(&tts_config).ok_or_else(|| {
            StudioError::EngineUnavailable("failed to create neural TTS engine".to_string())
        })?;
        let sample_rate = tts.sample_rate().max(1) as u32;

        let mut asr_config = OfflineRecognizerConfig::default();
        asr_config.model_config.whisper = OfflineWhisperModelConfig {
            encoder: Some(paths.asr_encoder.to_string_lossy().into_owned()),
            decoder: Some(paths.asr_decoder.to_string_lossy().into_owned()),
            language: Some("en".to_string()),
            task: Some("transcribe".to_string()),
            tail_paddings: -1,
            ..Default::default()
        };
        asr_config.model_config.tokens = Some(paths.asr_tokens.to_string_lossy().into_owned());
        asr_config.model_config.num_threads = paths.num_threads;
        asr_config.model_config.provider = paths.provider;
        let asr = OfflineRecognizer::create(&asr_config).ok_or_else(|| {
            StudioError::EngineUnavailable("failed to create neural ASR engine".to_string())
        })?;

        Ok(Some(Self {
            tts: Arc::new(tts),
            asr: Arc::new(asr),
            sample_rate,
            speed: paths.speed,
            name: paths.name,
            languages: paths.languages,
        }))
    }
}

impl SpeechEngine for NeuralEngine {
    fn capabilities(&self) -> StudioCapabilities {
        StudioCapabilities {
            engine: self.name.clone(),
            local: true,
            streaming_tts: false,
            streaming_asr: false,
            languages: self.languages.clone(),
            tts_sample_rate: self.sample_rate,
            asr_sample_rate: ASR_SAMPLE_RATE,
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
        voice: &str,
        format: AudioFormat,
    ) -> StudioFuture<'_, AudioBuffer> {
        let tts = self.tts.clone();
        let text = text.to_string();
        let sid = voice.parse::<i32>().unwrap_or(0);
        let speed = self.speed;
        Box::pin(async move {
            let samples = tokio::task::spawn_blocking(move || {
                let config = GenerationConfig {
                    speed,
                    sid,
                    ..Default::default()
                };
                let audio = tts
                    .generate_with_config(&text, &config, None::<fn(&[f32], f32) -> bool>)
                    .ok_or_else(|| {
                        StudioError::EngineUnavailable("TTS generated no audio".to_string())
                    })?;
                Ok::<_, StudioError>((audio.samples().to_vec(), audio.sample_rate().max(1) as u32))
            })
            .await
            .map_err(|err| StudioError::EngineInternal(err.to_string()))??;
            let (samples, sample_rate) = samples;
            let mut buffer =
                AudioBuffer::new(AudioFormat::pcm16(sample_rate), f32_to_pcm16(&samples))?;
            if format.sample_rate != sample_rate {
                buffer = buffer.resampled(format.sample_rate)?;
            }
            Ok(buffer)
        })
    }

    fn transcribe(&self, audio: &AudioBuffer, _language: &str) -> StudioFuture<'_, String> {
        let asr = self.asr.clone();
        let samples = audio.resampled(ASR_SAMPLE_RATE);
        Box::pin(async move {
            let samples = samples?.pcm16;
            tokio::task::spawn_blocking(move || {
                let stream = asr.create_stream();
                stream.accept_waveform(ASR_SAMPLE_RATE as i32, &pcm16_to_f32(&samples));
                asr.decode(&stream);
                Ok::<_, StudioError>(
                    stream
                        .get_result()
                        .map(|result| result.text)
                        .unwrap_or_default(),
                )
            })
            .await
            .map_err(|err| StudioError::EngineInternal(err.to_string()))?
        })
    }
}

fn f32_to_pcm16(samples: &[f32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(samples.len() * 2);
    for sample in samples {
        let value = (sample * 32767.0)
            .round()
            .clamp(i16::MIN as f32, i16::MAX as f32) as i16;
        out.extend_from_slice(&value.to_le_bytes());
    }
    out
}

fn pcm16_to_f32(pcm16: &[u8]) -> Vec<f32> {
    pcm16
        .as_chunks::<2>()
        .0
        .iter()
        .map(|chunk| i16::from_le_bytes(*chunk) as f32 / 32768.0)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn neural_roundtrip_when_models_are_configured() {
        let Ok(Some(engine)) = NeuralEngine::from_env() else {
            return;
        };
        let session = AudioFormat::tts();
        let audio = engine
            .synthesize("Hello from the native neural engine.", "0", session)
            .await
            .unwrap();
        assert!(!audio.pcm16.is_empty());
        let text = engine.transcribe(&audio, "en").await.unwrap();
        assert!(!text.trim().is_empty());
    }
}
