// AudioWorkletProcessor: downsamples the mic input (at whatever native
// sample rate the browser's AudioContext runs at) to 16kHz mono PCM16 and
// posts fixed-size chunks back to the main thread. Runs on the audio
// rendering thread, so it must stay dependency-free plain JS.

const TARGET_SAMPLE_RATE = 16000;
const CHUNK_MS = 20;
const CHUNK_SAMPLES = (TARGET_SAMPLE_RATE * CHUNK_MS) / 1000; // 320 samples

class PcmDownsampleProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._resampleRatio = TARGET_SAMPLE_RATE / sampleRate; // `sampleRate` is a worklet global
    this._srcPos = 0;
    this._outBuf = new Int16Array(CHUNK_SAMPLES);
    this._outPos = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channel = input[0];
    if (!channel || channel.length === 0) return true;

    // Linear-interpolation resample from the context's native rate down to 16kHz.
    while (this._srcPos < channel.length) {
      const idx = this._srcPos;
      const i0 = Math.floor(idx);
      const i1 = Math.min(i0 + 1, channel.length - 1);
      const frac = idx - i0;
      const sample = channel[i0] * (1 - frac) + channel[i1] * frac;

      const clamped = Math.max(-1, Math.min(1, sample));
      this._outBuf[this._outPos++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;

      if (this._outPos >= CHUNK_SAMPLES) {
        this.port.postMessage(this._outBuf.buffer.slice(0));
        this._outPos = 0;
      }

      this._srcPos += 1 / this._resampleRatio;
    }
    this._srcPos -= channel.length;

    return true;
  }
}

registerProcessor("pcm-downsample-processor", PcmDownsampleProcessor);
