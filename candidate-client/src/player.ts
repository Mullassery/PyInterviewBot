const TTS_SAMPLE_RATE = 22050; // must match ai-service's tts.SAMPLE_RATE

export class Player {
  private ctx: AudioContext;
  private nextStartTime = 0;
  private activeSources: AudioBufferSourceNode[] = [];

  constructor() {
    this.ctx = new AudioContext();
  }

  enqueue(pcm16: ArrayBuffer): void {
    const samples = new Int16Array(pcm16);
    const float32 = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i++) {
      float32[i] = samples[i] / (samples[i] < 0 ? 0x8000 : 0x7fff);
    }

    const buffer = this.ctx.createBuffer(1, float32.length, TTS_SAMPLE_RATE);
    buffer.copyToChannel(float32, 0);

    const source = this.ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.ctx.destination);

    const startAt = Math.max(this.nextStartTime, this.ctx.currentTime);
    source.start(startAt);
    this.nextStartTime = startAt + buffer.duration;

    this.activeSources.push(source);
    source.onended = () => {
      this.activeSources = this.activeSources.filter((s) => s !== source);
    };
  }

  /// Immediately stops all playback — called on barge-in so the candidate
  /// never has to wait for the AI to finish its sentence.
  interrupt(): void {
    for (const source of this.activeSources) {
      try {
        source.onended = null;
        source.stop();
      } catch {
        // already stopped/ended — fine
      }
    }
    this.activeSources = [];
    this.nextStartTime = this.ctx.currentTime;
  }
}
