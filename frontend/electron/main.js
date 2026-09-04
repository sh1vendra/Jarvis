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
import { app, BrowserWindow, globalShortcut, ipcMain, screen, systemPreferences } from "electron";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const HOTKEY = "CommandOrControl+Shift+Space";
const DEV_SERVER_URL = process.env.VITE_DEV_SERVER_URL || "http://localhost:5173";

const WINDOW_WIDTH = 460;
const WINDOW_HEIGHT = 340;
const EDGE_MARGIN = 24; // px kept clear of the screen edge for the default spawn position

// Wake word ("Hey Jarvis") no longer lives here - electron/wakeword.cjs
// (Porcupine) is kept in the repo but deliberately not wired in below;
// openWakeWord replaced it and runs on the Python backend instead, since
// openWakeWord is a Python/onnxruntime library with no Node binding at all
// (Porcupine's prebuilt N-API addon was the whole reason it *could* run in
// this process - openWakeWord has nothing analogous). Its trigger now
// arrives over the WebSocket connection (`wakeword_detected`, handled in
// App.jsx) instead of the `jarvis:hotkey` IPC channel this file still owns
// for the hotkey. See backend/voice/wakeword.py and planning.md.

let mainWindow = null;
// Mirrors the renderer's recording state so the single hotkey can alternate
// start/stop. The renderer is the source of truth and reports back via
// `jarvis:recording-state`; this is only what the toggle reads to decide
// which edge to send.
let isRecording = false;

// ── Window position persistence ──
//
// A frameless, always-on-top window with no config'd default position was
// what let it end up somewhere the user lost track of - real failure mode
// this session, forcing a full restart to get it back. Fix has two parts:
// remember where the user last put it (so a deliberate move sticks across
// launches), and if there's nothing remembered or the remembered spot is no
// longer on any connected display (external monitor unplugged, etc.), fall
// back to a fixed, sane default instead of whatever the OS/Electron would
// otherwise pick.
const windowStatePath = path.join(app.getPath("userData"), "window-state.json");

function loadSavedPosition() {
  try {
    const raw = JSON.parse(fs.readFileSync(windowStatePath, "utf8"));
    if (Number.isFinite(raw.x) && Number.isFinite(raw.y)) return { x: raw.x, y: raw.y };
  } catch {
    // No file yet, or corrupt - fall through to the default.
  }
  return null;
}

function saveWindowPosition(win) {
  try {
    const [x, y] = win.getPosition();
    fs.writeFileSync(windowStatePath, JSON.stringify({ x, y }));
  } catch (err) {
    console.error("[main] failed to save window position:", err);
  }
}

/** True if a window at (x, y) would have at least its top titlebar strip
 * visible on some currently-connected display - not just the primary one,
 * so a position that made sense on a since-reconnected external monitor
 * isn't wrongly rejected. */
function isPositionUsable(x, y) {
  const probe = { x: x + WINDOW_WIDTH / 2, y: y + 10 };
  return screen.getAllDisplays().some((d) => {
    const b = d.bounds;
    return probe.x >= b.x && probe.x < b.x + b.width && probe.y >= b.y && probe.y < b.y + b.height;
  });
}

/** Top-right of the primary display's work area - a fixed, predictable
 * default, never "wherever the OS feels like." */
function defaultPosition() {
  const { workArea } = screen.getPrimaryDisplay();
  return {
    x: workArea.x + workArea.width - WINDOW_WIDTH - EDGE_MARGIN,
    y: workArea.y + EDGE_MARGIN,
  };
}

function resolveStartPosition() {
  const saved = loadSavedPosition();
  if (saved && isPositionUsable(saved.x, saved.y)) return saved;
  return defaultPosition();
}

function createWindow() {
  const { x, y } = resolveStartPosition();

  mainWindow = new BrowserWindow({
    width: WINDOW_WIDTH,
    height: WINDOW_HEIGHT,
    x,
    y,
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: false,
    // Both explicit even though they're Electron's defaults: a frameless
    // window draws no OS titlebar/traffic-lights, so there is no built-in
    // affordance for either regardless of these flags - movement only
    // happens via the renderer's `-webkit-app-region: drag` strip, and
    // minimizing only happens via the explicit button wired below
    // (jarvis:minimize). These flags just make sure neither is disabled.
    movable: true,
    minimizable: true,
    closable: true,
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

  // Persist position on every real move, debounced so a drag doesn't write
  // to disk on every intermediate pixel - and again on close, as a
  // last-write safety net.
  let saveTimer = null;
  mainWindow.on("moved", () => {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => saveWindowPosition(mainWindow), 250);
  });
  mainWindow.on("close", () => {
    clearTimeout(saveTimer);
    if (mainWindow) saveWindowPosition(mainWindow);
  });

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

// The hotkey's own start/stop bookkeeping. Wake word ("Hey Jarvis") is a
// second, independent trigger, but it no longer funnels through here - it
// lives on the Python backend now (voice/wakeword.py) and reaches the
// renderer directly over the WebSocket connection as `wakeword_detected`,
// not through this IPC path. The two triggers share nothing at this layer
// except both eventually calling the renderer's beginCapture(source) - see
// App.jsx. Mic handoff for wake word is the renderer's own `mic_state`
// message to the backend, not anything main.js does.
function startListening(source) {
  if (isRecording) return;
  isRecording = true;
  console.log(`[main] START recording (via ${source})`);
  sendToRenderer("jarvis:hotkey", { action: "start", source });
}

function stopListening(source) {
  if (!isRecording) return;
  isRecording = false;
  console.log(`[main] STOP recording (via ${source})`);
  sendToRenderer("jarvis:hotkey", { action: "stop", source });
}

function registerHotkey() {
  const ok = globalShortcut.register(HOTKEY, () => {
    // The hotkey is a toggle. Wake word only ever starts (it has no "press
    // again" - the renderer auto-stops it on trailing silence).
    if (isRecording) stopListening("hotkey");
    else startListening("hotkey");
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
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
      return;
    }
    // A minimized/hidden window doesn't count as "closed" - clicking the
    // dock icon should bring it back rather than silently no-op.
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
});

// Both driven by the renderer's titlebar strip (App.jsx) - a frameless
// window has no OS-drawn minimize/close buttons, so these are the only way
// either action can happen.
ipcMain.on("jarvis:minimize", () => {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.minimize();
});

ipcMain.on("jarvis:close", () => {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.close();
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
