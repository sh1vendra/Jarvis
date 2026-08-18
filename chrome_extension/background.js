// Jarvis Browser Bridge - background service worker
//
// Owns the WebSocket connection to the backend bridge server
// (backend/servers/browser_bridge_server.py). Everything else - the
// content script, the backend's tool functions - talks through this file.
//
// MV3 gotcha worth knowing before touching this: Chrome can kill and
// restart this service worker at any time (it's not a persistent
// background page the way MV2's was). sessionId below is generated fresh
// every time this file runs from the top, so it is NOT stable across a
// worker restart - only stable within one "wake cycle." The backend's
// session mapping (browser.bridge.BrowserBridge) handles this fine, since
// a restart just looks like a fresh browser_bridge_hello with a new
// session_id, same as a brand new connection.

const DEFAULT_BRIDGE_URL = "ws://127.0.0.1:8765";
const DEFAULT_BRIDGE_TOKEN = "dev-bridge-token";
const EXTENSION_NAME = "jarvis-browser-bridge";

const ACTION_POLL_INTERVAL_MS = 5000; // Fallback only - actions are pushed via WebSocket first
const KEEPALIVE_INTERVAL_MS = 15000; // ping every 15s so the backend's staleness check doesn't trip
const RECONNECT_DELAY_MS = 1500;

let BRIDGE_URL = DEFAULT_BRIDGE_URL;
let BRIDGE_TOKEN = DEFAULT_BRIDGE_TOKEN;

let bridgeSocket = null;
let authenticated = false;
let sessionId = `chrome-session-${Date.now()}`;
let reconnectTimer = null;
let actionPollTimer = null;
let keepaliveTimer = null;
// Push (inline in the "message" listener below) and the poll timer both
// run concurrently once authenticated - this flag is the only thing
// preventing double-execution if both fire close together. Inherited
// deliberately from the reference architecture rather than replaced with
// a more robust mutual-exclusion mechanism - see planning.md.
let actionExecutionInFlight = false;

function log(...args) {
  console.log("[JarvisBridge]", ...args);
}

async function loadSettings() {
  try {
    const stored = await chrome.storage.sync.get(["bridgeUrl", "bridgeToken"]);
    BRIDGE_URL = stored.bridgeUrl || DEFAULT_BRIDGE_URL;
    BRIDGE_TOKEN = stored.bridgeToken || DEFAULT_BRIDGE_TOKEN;
  } catch (error) {
    log("Failed to load settings, using defaults", error);
  }
}

// ── Bridge Connection ──

function connectBridge() {
  if (bridgeSocket && (bridgeSocket.readyState === WebSocket.OPEN || bridgeSocket.readyState === WebSocket.CONNECTING)) {
    return;
  }

  bridgeSocket = new WebSocket(BRIDGE_URL);

  bridgeSocket.addEventListener("open", () => {
    authenticated = false;
    log("Connected to backend bridge");
    bridgeSocket.send(
      JSON.stringify({
        type: "browser_bridge_hello",
        token: BRIDGE_TOKEN,
        session_id: sessionId,
        extension_name: EXTENSION_NAME,
      }),
    );
  });

  bridgeSocket.addEventListener("message", async (event) => {
    try {
      const message = JSON.parse(event.data);

      if (message.type === "browser_bridge_hello_ack") {
        authenticated = !!message.ok;
        if (authenticated) {
          log("Authenticated, session:", sessionId);
          startActionPolling();
          startKeepalive();
          await pushActiveTabSnapshot();
        } else {
          log("Authentication failed:", message.message);
          try {
            bridgeSocket?.close();
          } catch (_) {}
          scheduleReconnect();
        }
        return;
      }

      if (message.type === "browser_snapshot_ack") return;
      if (message.type === "browser_action_result_ack") return;
      if (message.type === "browser_dom_change_ack") return;
      if (message.type === "browser_pong") return;

      if (message.type === "browser_actions" && Array.isArray(message.actions)) {
        log("Received browser actions", message.actions);
        await processBridgeActions(message.actions);
      }
    } catch (error) {
      log("Failed to process bridge message", error);
    }
  });

  bridgeSocket.addEventListener("close", () => {
    authenticated = false;
    stopActionPolling();
    stopKeepalive();
    log("Bridge connection closed; scheduling reconnect");
    scheduleReconnect();
  });

  bridgeSocket.addEventListener("error", (error) => {
    authenticated = false;
    log("Bridge error", error);
  });
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectBridge();
  }, RECONNECT_DELAY_MS);
}

function sendToBridge(payload) {
  if (!bridgeSocket || bridgeSocket.readyState !== WebSocket.OPEN || !authenticated) {
    return false;
  }
  bridgeSocket.send(JSON.stringify(payload));
  return true;
}

// ── Keepalive ──

function startKeepalive() {
  if (keepaliveTimer) return;
  keepaliveTimer = setInterval(() => {
    if (bridgeSocket && bridgeSocket.readyState === WebSocket.OPEN && authenticated) {
      bridgeSocket.send(JSON.stringify({ type: "browser_ping" }));
    }
  }, KEEPALIVE_INTERVAL_MS);
}

function stopKeepalive() {
  if (keepaliveTimer) {
    clearInterval(keepaliveTimer);
    keepaliveTimer = null;
  }
}

// ── Action Polling (fallback path - see actionExecutionInFlight note above) ──

function startActionPolling() {
  if (actionPollTimer) return;
  actionPollTimer = setInterval(() => {
    pollBridgeActions().catch((error) => log("Action polling failed", error));
  }, ACTION_POLL_INTERVAL_MS);
}

function stopActionPolling() {
  if (actionPollTimer) {
    clearInterval(actionPollTimer);
    actionPollTimer = null;
  }
}

async function pollBridgeActions() {
  if (!bridgeSocket || bridgeSocket.readyState !== WebSocket.OPEN || !authenticated || actionExecutionInFlight) {
    return;
  }
  bridgeSocket.send(JSON.stringify({ type: "browser_poll_actions", session_id: sessionId }));
}

// ── Action Dispatch ──

async function processBridgeActions(actions) {
  if (!Array.isArray(actions) || actions.length === 0 || actionExecutionInFlight) {
    return;
  }

  actionExecutionInFlight = true;
  try {
    for (const action of actions) {
      const result = await executeActionOnTab(action);
      sendToBridge({ type: "browser_action_result", result });
    }
  } finally {
    actionExecutionInFlight = false;
  }
}

async function executeActionOnTab(action) {
  const metadata = action?.metadata || {};
  const targetTabId = Number(metadata.tab_id || 0);
  const tab = targetTabId ? await chrome.tabs.get(targetTabId).catch(() => null) : await getActiveTab();

  if (!tab?.id) {
    return buildActionResult(action, false, "No browser tab available for action execution.", metadata, {
      reason: "missing-tab",
    });
  }
  if (!isInjectableUrl(tab.url)) {
    return buildActionResult(action, false, `Cannot execute browser action on ${tab.url || "this page"}.`, metadata, {
      reason: "unsupported-url",
      tab_id: String(tab.id),
    });
  }

  try {
    await ensureContentScript(tab.id);

    if (action.action === "refresh_snapshot") {
      const snapResult = await requestSnapshotFromTab(tab.id, action.session_id || sessionId);
      return buildActionResult(action, !!snapResult.ok, snapResult.ok ? "Snapshot refreshed." : "Snapshot refresh failed.", metadata, {
        tab_id: String(tab.id),
      });
    }

    // Generic path - click / type / select, relayed straight to the
    // content script's 3-tier element resolution.
    const result = await chrome.tabs.sendMessage(tab.id, {
      type: "jarvis_execute_action",
      action,
      sessionId: action?.session_id || sessionId,
      tabId: String(tab.id),
    });

    // After a successful action, the content script's MutationObserver
    // fires a fresh snapshot on its own - no explicit request needed here.
    return buildActionResult(
      action,
      !!result?.ok,
      result?.message || (result?.ok ? "Browser action executed." : "Browser action failed."),
      metadata,
      { tab_id: String(tab.id), executed_ref_id: result?.executedRefId || action?.ref_id || "", matched_by: result?.matchedBy || "" },
      Date.now(),
    );
  } catch (error) {
    return buildActionResult(action, false, String(error?.message || error), metadata, {
      reason: "execution-error",
      tab_id: String(tab?.id || ""),
    });
  }
}

function buildActionResult(action, ok, message, metadata, details, postGen) {
  const errorCode = ok ? "" : String(details?.error_code || details?.reason || "bridge.action_failed");
  return {
    ok,
    message,
    action: action?.action || "",
    ref_id: action?.ref_id || "",
    action_id: action?.action_id || "",
    session_id: action?.session_id || sessionId,
    pre_generation: Number(metadata?.generation || 0),
    post_generation: postGen ?? Number(metadata?.generation || 0),
    details: details || {},
    error: ok
      ? null
      : {
          code: errorCode,
          message,
          retryable: false,
          source: "bridge.background",
          details: details || {},
        },
    meta: { session_id: action?.session_id || sessionId, provenance: "chrome_extension" },
  };
}

// ── Snapshot Requests ──

async function requestSnapshotFromTab(tabId, sessionIdOverride = sessionId) {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab?.id) return { ok: false, reason: "missing-tab" };
  if (!isInjectableUrl(tab.url)) return { ok: false, reason: "unsupported-url", url: tab.url || "" };

  const payload = { type: "jarvis_collect_snapshot", sessionId: sessionIdOverride, tabId: String(tab.id) };

  try {
    const response = await chrome.tabs.sendMessage(tab.id, payload);
    return { ok: !!response?.ok, injected: false };
  } catch (error) {
    const message = String(error?.message || error);
    if (!/Receiving end does not exist/i.test(message)) {
      return { ok: false, reason: "send-failed", error: message };
    }
  }

  // Content script wasn't already present (e.g. page loaded before this
  // extension was installed/reloaded) - the static content_scripts
  // declaration usually covers this, this is the on-demand fallback.
  try {
    await ensureContentScript(tab.id);
    const response = await chrome.tabs.sendMessage(tab.id, payload);
    return { ok: !!response?.ok, injected: true };
  } catch (error) {
    return { ok: false, reason: "inject-failed", error: String(error?.message || error) };
  }
}

async function ensureContentScript(tabId) {
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["content_script.js"],
  });
}

function isInjectableUrl(url) {
  if (!url || typeof url !== "string") return false;
  return /^(https?|file):/i.test(url);
}

async function getActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tabs[0] || null;
}

async function pushActiveTabSnapshot() {
  const tab = await getActiveTab();
  if (!tab?.id) return;
  await requestSnapshotFromTab(tab.id, sessionId);
}

// ── Chrome Event Listeners ──

chrome.runtime.onInstalled.addListener(() => {
  loadSettings().then(() => connectBridge());
});
chrome.runtime.onStartup.addListener(() => {
  loadSettings().then(() => connectBridge());
});

chrome.tabs.onActivated.addListener(() => {
  if (authenticated) pushActiveTabSnapshot();
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (authenticated && changeInfo.status === "complete") {
    requestSnapshotFromTab(tabId, sessionId).catch((error) => log("Snapshot refresh failed after tab update", error));
  }
});

// ── Internal Message Handling (from content script) ──

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "jarvis_snapshot") {
    const ok = sendToBridge({ type: "browser_snapshot", snapshot: message.snapshot });
    sendResponse({ ok, authenticated });
    return true;
  }
  if (message?.type === "jarvis_dom_change") {
    const ok = sendToBridge({ type: "browser_dom_change", event: message.event });
    sendResponse({ ok, authenticated });
    return true;
  }
  if (message?.type === "jarvis_get_status") {
    sendResponse({ ok: true, authenticated, sessionId, bridgeUrl: BRIDGE_URL });
    return true;
  }
  return false;
});

// ── Boot ──
loadSettings().then(() => connectBridge());
