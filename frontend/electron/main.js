// Electron main process for Jarvis.
//
// Owns the always-on-top overlay window and the one thing the renderer
// cannot do for itself: a system-wide hotkey that works while another app
// (Spotify, Chrome) is focused. Everything else - audio capture, the
// WebSocket conversation with the Python backend - lives in the renderer,
// because that is where the Web Audio API is.
//
// Hotkey is TOGGLE, not hold-to-talk, and that is a real constraint rather
// than a preference: Electron's globalShortcut only fires on key-down and
// exposes no key-up event, so "record while held" is not expressible
// without a native input-monitoring module. Press once to start, once more
// to stop.

// Named ESM imports are supported by Electron's main process (28+) and are
// verified working here. If these ever come back `undefined`, the cause is
// almost certainly ELECTRON_RUN_AS_NODE being set in the environment, which
// makes the Electron binary behave as plain Node - see the `dev:electron`
// script in package.json.
import { app, BrowserWindow, globalShortcut, ipcMain, systemPreferences } from "electron";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const HOTKEY = "CommandOrControl+Shift+Space";
const DEV_SERVER_URL = process.env.VITE_DEV_SERVER_URL || "http://localhost:5173";

let mainWindow = null;
// Mirrors the renderer's recording state so the single hotkey can alternate
// start/stop. The renderer is the source of truth and reports back via
// `jarvis:recording-state`; this is only what the toggle reads to decide
// which edge to send.
let isRecording = false;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 460,
    height: 340,
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: false,
    alwaysOnTop: true,
    // Visual design is explicitly out of scope this session - a later
    // styling pass owns look and feel. This is just a surface that shows
    // state and can be clicked.
    backgroundColor: "#00000000",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  // Float above full-screen apps too, so the overlay is usable while
  // Spotify or Chrome is frontmost.
  mainWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  mainWindow.setAlwaysOnTop(true, "floating");

  // The renderer asks for the mic via getUserMedia; without this handler
  // Electron denies the request silently.
  mainWindow.webContents.session.setPermissionRequestHandler((_wc, permission, callback) => {
    callback(permission === "media" || permission === "audioCapture");
  });

  mainWindow.loadURL(DEV_SERVER_URL);
  mainWindow.webContents.on("did-fail-load", (_e, code, desc) => {
    console.error(`[main] renderer failed to load (${code}): ${desc}`);
  });

  // Renderer console output does not reach the terminal by default, which
  // hides module-load errors completely - a failed import just leaves the
  // previous build running with no visible cause. Mirror it here.
  mainWindow.webContents.on("console-message", (event) => {
    const level = ["debug", "info", "warning", "error"][event.level] ?? event.level;
    console.log(`[renderer:${level}] ${event.message}`);
  });
}

function sendToRenderer(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, payload);
  }
}

function registerHotkey() {
  const ok = globalShortcut.register(HOTKEY, () => {
    // Flip locally and tell the renderer which edge this press is. The
    // renderer confirms the real state back on jarvis:recording-state.
    isRecording = !isRecording;
    console.log(`[main] hotkey ${HOTKEY} -> ${isRecording ? "START" : "STOP"} recording`);
    sendToRenderer("jarvis:hotkey", { action: isRecording ? "start" : "stop" });
  });

  if (!ok) {
    console.error(
      `[main] FAILED to register ${HOTKEY} - another app already owns it. ` +
        `macOS silently refuses contested shortcuts; pick a different accelerator.`
    );
  } else {
    console.log(`[main] registered global hotkey: ${HOTKEY}`);
  }
  return ok;
}

app.whenReady().then(() => {
  createWindow();
  registerHotkey();

  // Triggers the macOS microphone permission prompt for the Electron
  // binary itself, so the renderer's first getUserMedia doesn't silently
  // return a stream of zeroes. Deliberately NOT awaited before
  // createWindow: this promise only settles once the user answers the
  // system dialog, so awaiting it first would leave the window unpainted
  // behind the prompt.
  if (process.platform === "darwin") {
    systemPreferences
      .askForMediaAccess("microphone")
      .then((granted) => {
        console.log(`[main] macOS microphone access granted: ${granted}`);
        if (!granted) {
          console.error(
            "[main] Microphone denied. Enable it under System Settings > Privacy & Security > Microphone."
          );
        }
      })
      .catch((err) => console.error("[main] askForMediaAccess failed:", err));
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// The renderer is authoritative about whether it is actually recording -
// if capture failed to start, this resyncs the toggle so the next hotkey
// press is still the correct edge.
ipcMain.on("jarvis:recording-state", (_event, recording) => {
  isRecording = Boolean(recording);
  console.log(`[main] renderer reports recording=${isRecording}`);
});

ipcMain.on("jarvis:log", (_event, message) => {
  console.log(`[renderer] ${message}`);
});

app.on("will-quit", () => {
  globalShortcut.unregisterAll();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
