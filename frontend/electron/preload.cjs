// Narrow contextBridge surface.
//
// Deliberately small: the renderer needs exactly three things from the main
// process, and gets nothing else - no ipcRenderer, no node APIs, no fs.
//
// Audio does NOT travel over IPC. The renderer opens its own WebSocket
// straight to the Python backend (WebSocket is a native renderer API), so
// captured audio goes renderer -> backend directly instead of taking a
// detour through the main process. That keeps the bridge to just the one
// capability the renderer genuinely cannot provide for itself: a hotkey
// that fires while another application is focused.

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

  /**
   * Subscribe to wake-word ("Jarvis") availability/listening status.
   * @param {(payload: {available: boolean, listening: boolean, reason?: string}) => void} handler
   * @returns {() => void} unsubscribe
   */
  onWakeWordStatus(handler) {
    const listener = (_event, payload) => handler(payload);
    ipcRenderer.on("jarvis:wakeword-status", listener);
    return () => ipcRenderer.removeListener("jarvis:wakeword-status", listener);
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
