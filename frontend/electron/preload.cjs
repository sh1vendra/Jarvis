// Narrow contextBridge surface.
//
// Deliberately small - only what the renderer genuinely cannot do for
// itself, since it has no `nodeIntegration` and is fully context-isolated:
// no `ipcRenderer`, no node APIs, no fs. That's a system-wide hotkey (fires
// while another app is focused) and now real speech output (`say` is a
// subprocess, which a sandboxed Chromium context can't spawn on its own).
//
// Audio input does NOT travel over IPC. The renderer opens its own
// WebSocket straight to the Python backend (WebSocket is a native renderer
// API), so captured audio goes renderer -> backend directly instead of
// taking a detour through the main process.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("jarvis", {
  /**
   * Subscribe to global-hotkey presses.
   * @param {(payload: {action: "start"|"stop"}) => void} handler
   * @returns {() => void} unsubscribe
   */
  onHotkey(handler) {
    const listener = (_event, payload) => handler(payload);
    ipcRenderer.on("jarvis:hotkey", listener);
    return () => ipcRenderer.removeListener("jarvis:hotkey", listener);
  },

  // Wake-word ("Hey Jarvis") status/detection is NOT an IPC channel - it
  // travels over the renderer's own WebSocket connection to the backend
  // (`wakeword_status`/`wakeword_detected` in App.jsx), since detection
  // runs in Python (voice/wakeword.py), not this main process. See
  // planning.md for why.

  /** Speak text via `say -v Daniel` in the main process - the renderer
   * relays the backend's `{"type": "speak"}` WebSocket message here,
   * since only main can spawn a subprocess. */
  speak(text) {
    ipcRenderer.send("jarvis:speak", String(text || ""));
  },

  /**
   * Subscribe to real speech-playback status - true exactly while `say` is
   * actually running, false once it closes (finished or interrupted).
   * @param {(payload: {speaking: boolean}) => void} handler
   * @returns {() => void} unsubscribe
   */
  onSpeakingStatus(handler) {
    const listener = (_event, payload) => handler(payload);
    ipcRenderer.on("jarvis:speaking-status", listener);
    return () => ipcRenderer.removeListener("jarvis:speaking-status", listener);
  },

  /** Report real recording state so the main process's toggle stays in sync. */
  reportRecordingState(isRecording) {
    ipcRenderer.send("jarvis:recording-state", Boolean(isRecording));
  },

  /** Surface a renderer log line in the terminal running Electron. */
  log(message) {
    ipcRenderer.send("jarvis:log", String(message));
  },

  /** Minimize the window - the frameless window draws no OS minimize
   * button, so this is what the titlebar strip's button calls. */
  minimize() {
    ipcRenderer.send("jarvis:minimize");
  },

  /** Close the window - same reasoning as minimize() above. */
  closeWindow() {
    ipcRenderer.send("jarvis:close");
  },
});
