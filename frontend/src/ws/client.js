// WebSocket client for the Python agent server (servers/agent_server.py).
//
// Lives in the renderer rather than the main process because the renderer is
// already where audio capture happens - routing audio through IPC to the main
// process and out again would add a copy and a serialization step for no gain.

const DEFAULT_URL = "ws://127.0.0.1:8766";

export class AgentClient {
  constructor({ url = DEFAULT_URL, onMessage, onStatusChange } = {}) {
    this.url = url;
    this.onMessage = onMessage || (() => {});
    this.onStatusChange = onStatusChange || (() => {});
    this.socket = null;
    this.reconnectTimer = null;
    this.shouldReconnect = true;
  }

  connect() {
    if (this.socket && (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)) {
      return;
    }
    this.onStatusChange("connecting");
    const socket = new WebSocket(this.url);
    this.socket = socket;

    socket.onopen = () => {
      this.onStatusChange("connected");
      this.send({ type: "ping" });
    };

    socket.onmessage = (event) => {
      try {
        this.onMessage(JSON.parse(event.data));
      } catch {
        this.onMessage({ type: "error", message: `unparseable frame: ${event.data}` });
      }
    };

    socket.onclose = () => {
      this.onStatusChange("disconnected");
      // The backend is started and stopped by hand during development, so a
      // dropped socket is expected rather than exceptional - keep retrying
      // instead of forcing a UI restart.
      if (this.shouldReconnect) {
        clearTimeout(this.reconnectTimer);
        this.reconnectTimer = setTimeout(() => this.connect(), 2000);
      }
    };

    socket.onerror = () => this.onStatusChange("error");
  }

  send(payload) {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }

  close() {
    this.shouldReconnect = false;
    clearTimeout(this.reconnectTimer);
    if (this.socket) this.socket.close();
  }
}
