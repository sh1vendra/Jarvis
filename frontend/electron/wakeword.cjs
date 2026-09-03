// Wake-word detection ("Jarvis") via Porcupine, in the Electron MAIN process.
//
// This reverses the earlier "no wake word" decision (see planning.md),
// deliberately and with the costs accepted: a continuously-open microphone,
// false-positive risk, and Porcupine's access-key setup.
//
// Why it lives in main, not the renderer:
//  - @picovoice/porcupine-node + @picovoice/pvrecorder-node ship prebuilt
//    N-API .node binaries for mac/arm64 and load directly in Electron 44's
//    main process (verified - no electron-rebuild), and porcupine-node
//    bundles both the acoustic model and the built-in "Jarvis" keyword, so
//    there is no ~2 MB model file to vendor and no renderer CSP/WASM work;
//  - it reuses the EXACT global-hotkey trigger path (main -> jarvis:hotkey
//    -> renderer), so the renderer and the already-verified push-to-talk
//    recorder need zero changes;
//  - one place owns the listen/pause lifecycle, right next to the hotkey.
//
// The hotkey is NOT removed - both triggers are independent and both call
// the same code. If the addon or the access key is missing, wake word is
// simply off and the hotkey still works.

const { EventEmitter } = require("node:events");

let Porcupine = null;
let BuiltinKeyword = null;
let PvRecorder = null;
let loadError = null;
try {
  ({ Porcupine, BuiltinKeyword } = require("@picovoice/porcupine-node"));
  ({ PvRecorder } = require("@picovoice/pvrecorder-node"));
} catch (err) {
  loadError = err;
}

class WakeWord extends EventEmitter {
  constructor({ accessKey, sensitivity = 0.5 } = {}) {
    super();
    this._accessKey = (accessKey || "").trim();
    this._sensitivity = sensitivity;
    this._porcupine = null;
    this._recorder = null;
    this._running = false; // the read loop is alive
    this._capturing = false; // pvrecorder currently owns the mic
  }

  /** Why wake word can't run, or null if it can. */
  get unavailableReason() {
    if (loadError) return `porcupine native addon failed to load: ${loadError.message}`;
    if (!this._accessKey) return "no PICOVOICE_ACCESS_KEY in the repo-root .env";
    return null;
  }

  /** Start listening for "Jarvis". Returns true if it actually started. */
  start() {
    if (this._running) return true;
    const reason = this.unavailableReason;
    if (reason) {
      this.emit("unavailable", reason);
      return false;
    }
    try {
      this._porcupine = new Porcupine(this._accessKey, [BuiltinKeyword.JARVIS], [this._sensitivity]);
      this._recorder = new PvRecorder(this._porcupine.frameLength, -1); // -1 = default input device
      this._recorder.start();
      this._capturing = true;
      this._running = true;
      this.emit("listening", { device: this._safeDevice(), sensitivity: this._sensitivity });
      this._loop();
      return true;
    } catch (err) {
      this.emit("error", err);
      this._teardown();
      return false;
    }
  }

  async _loop() {
    while (this._running) {
      if (!this._capturing) {
        // Mic is handed to the renderer for a command capture - idle here
        // without touching Porcupine, then pick straight back up.
        await new Promise((r) => setTimeout(r, 60));
        continue;
      }
      let frame;
      try {
        frame = await this._recorder.read();
      } catch (err) {
        if (this._running && this._capturing) this.emit("error", err);
        break;
      }
      if (!this._capturing) continue; // handed off mid-read
      let index;
      try {
        index = this._porcupine.process(frame);
      } catch (err) {
        this.emit("error", err);
        continue;
      }
      if (index >= 0) this.emit("detected");
    }
  }

  /** Release the mic so the renderer's getUserMedia has it uncontended
   *  during a command capture. Detection resumes on resumeCapture(). */
  pauseCapture() {
    if (!this._running || !this._capturing) return;
    this._capturing = false;
    try {
      this._recorder.stop();
    } catch (err) {
      this.emit("error", err);
    }
  }

  resumeCapture() {
    if (!this._running || this._capturing) return;
    try {
      this._recorder.start();
      this._capturing = true;
    } catch (err) {
      this.emit("error", err);
    }
  }

  stop() {
    this._running = false;
    this._teardown();
  }

  _teardown() {
    this._running = false;
    this._capturing = false;
    try {
      this._recorder && this._recorder.stop();
    } catch {}
    try {
      this._recorder && this._recorder.release();
    } catch {}
    try {
      this._porcupine && this._porcupine.release();
    } catch {}
    this._recorder = null;
    this._porcupine = null;
  }

  _safeDevice() {
    try {
      return this._recorder.getSelectedDevice();
    } catch {
      return "unknown";
    }
  }
}

module.exports = { WakeWord };
