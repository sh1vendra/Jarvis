// Microphone capture for Jarvis, producing raw PCM the Python backend can
// actually consume.
//
// Why not MediaRecorder: MediaRecorder in Chromium emits WebM/Opus, and the
// backend's speech_recognition library reads WAV/AIFF/FLAC PCM only - it
// cannot decode Opus at all. Feeding it a WebM blob fails outright. So we
// take raw PCM straight off the Web Audio graph instead, which is also what
// the Python-side sounddevice capture already produced, meaning both paths
// hand `transcribe_audio` the exact same kind of object.
//
// Why AudioWorklet and not ScriptProcessorNode: ScriptProcessorNode has been
// deprecated for years and runs on the main thread, where it glitches under
// load. AudioWorklet runs on the audio thread. The worklet is loaded from an
// inline Blob URL rather than a file, because a file-backed
// `audioWorklet.addModule()` has to satisfy Electron's CSP and file://
// resolution, and a Blob sidesteps both while staying identical in dev and
// packaged builds.
//
// No resampling happens anywhere. We capture at the device's native rate and
// send that rate to the backend; Google's endpoint accepts anything at or
// above 8 kHz, so the audio that reaches transcription is bit-identical to
// what the microphone produced.

const WORKLET_SOURCE = `
class PCMCollector extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    // slice() copies - the input buffer is reused by the audio thread on
    // the next render quantum, so posting it directly would send garbage.
    if (channel && channel.length) this.port.postMessage(channel.slice(0));
    return true;
  }
}
registerProcessor("pcm-collector", PCMCollector);
`;

/** Float32 [-1,1] samples -> little-endian int16, the format AudioData wants. */
function floatToInt16(chunks, totalLength) {
  const out = new Int16Array(totalLength);
  let offset = 0;
  for (const chunk of chunks) {
    for (let i = 0; i < chunk.length; i += 1) {
      const clamped = Math.max(-1, Math.min(1, chunk[i]));
      out[offset + i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    }
    offset += chunk.length;
  }
  return out;
}

/** Base64 without blowing the call stack on a multi-hundred-KB buffer. */
function toBase64(int16) {
  const bytes = new Uint8Array(int16.buffer, int16.byteOffset, int16.byteLength);
  let binary = "";
  const STRIDE = 0x8000;
  for (let i = 0; i < bytes.length; i += STRIDE) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + STRIDE));
  }
  return btoa(binary);
}

/** Largest absolute sample - a cheap "did the mic actually hear anything"
 *  check, so a permissions failure reads as silence rather than as a
 *  mysterious mistranscription. */
function peakAmplitude(int16) {
  let peak = 0;
  for (let i = 0; i < int16.length; i += 1) {
    const v = Math.abs(int16[i]);
    if (v > peak) peak = v;
  }
  return peak;
}

export class PCMRecorder {
  constructor() {
    this.stream = null;
    this.context = null;
    this.node = null;
    this.source = null;
    this.chunks = [];
    this.totalLength = 0;
    this.recording = false;
  }

  /**
   * @param {{onLevel?: (rms: number) => void}} [opts] - onLevel is called
   *   once per audio render quantum with the frame's RMS amplitude (0..1),
   *   used by the wake-word path for trailing-silence auto-stop and by the
   *   voice-activity indicator. Optional; omitting it keeps the original
   *   behaviour exactly.
   */
  async start(opts = {}) {
    if (this.recording) return;
    const onLevel = opts.onLevel;

    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        // Chromium's default processing chain is tuned for speech and
        // measurably helps a noisy room; left on deliberately.
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    this.context = new AudioContext();
    const blobUrl = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "application/javascript" }));
    try {
      await this.context.audioWorklet.addModule(blobUrl);
    } finally {
      URL.revokeObjectURL(blobUrl);
    }

    this.chunks = [];
    this.totalLength = 0;

    this.source = this.context.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.context, "pcm-collector");
    this.node.port.onmessage = (event) => {
      const frame = event.data;
      this.chunks.push(frame);
      this.totalLength += frame.length;
      if (onLevel) {
        let sum = 0;
        for (let i = 0; i < frame.length; i += 1) sum += frame[i] * frame[i];
        onLevel(Math.sqrt(sum / frame.length));
      }
    };

    this.source.connect(this.node);
    // An AudioWorkletNode only gets pulled if it reaches the destination.
    // Zero gain keeps the mic from being echoed back out of the speakers
    // while still driving the graph.
    const mute = this.context.createGain();
    mute.gain.value = 0;
    this.node.connect(mute).connect(this.context.destination);

    this.recording = true;
    return { sampleRate: this.context.sampleRate };
  }

  /** Stops capture and returns {pcmBase64, sampleRate, sampleWidth, samples, seconds, peak}. */
  async stop() {
    if (!this.recording) return null;
    this.recording = false;

    const sampleRate = this.context.sampleRate;
    const int16 = floatToInt16(this.chunks, this.totalLength);

    this.node.port.onmessage = null;
    this.source.disconnect();
    this.node.disconnect();
    this.stream.getTracks().forEach((t) => t.stop());
    await this.context.close();

    this.chunks = [];
    this.totalLength = 0;
    this.context = null;
    this.node = null;
    this.source = null;
    this.stream = null;

    return {
      pcmBase64: toBase64(int16),
      sampleRate,
      sampleWidth: 2,
      samples: int16.length,
      seconds: int16.length / sampleRate,
      peak: peakAmplitude(int16),
    };
  }
}
