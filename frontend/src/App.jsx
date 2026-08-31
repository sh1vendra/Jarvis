import React, { useEffect, useState } from "react";

export default function App() {
  const [hotkeyLog, setHotkeyLog] = useState([]);

  useEffect(() => {
    const off = window.jarvis.onHotkey(({ action }) => {
      window.jarvis.log(`hotkey received: ${action}`);
      setHotkeyLog((prev) => [`${new Date().toLocaleTimeString()} ${action}`, ...prev].slice(0, 5));
      window.jarvis.reportRecordingState(action === "start");
    });
    window.jarvis.log("renderer mounted, hotkey listener attached");
    return off;
  }, []);

  return (
    <div style={{ fontFamily: "monospace", padding: 16, background: "#fff", border: "1px solid #999" }}>
      <div>STAGE 1: hotkey smoke test</div>
      <div>Press Cmd+Shift+Space</div>
      <ul>{hotkeyLog.map((l, i) => <li key={i}>{l}</li>)}</ul>
    </div>
  );
}
