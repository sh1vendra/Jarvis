import React, { useCallback, useEffect, useRef, useState } from "react";
import { PCMRecorder } from "./audio/recorder.js";
import { AgentClient } from "./ws/client.js";

// Plain, deliberately unstyled state machine. Visual design is out of scope
// for this pass and is owned by a later styling tool - the contract that pass
// depends on is this state set and the props below it, so restyling should
// not require rewiring anything here.
//
//   idle       nothing happening, waiting for the hotkey
//   listening  microphone is open, user is speaking
//   thinking   audio sent; transcribing, then planning
//   doing      Action agent is executing milestones
//   approving  paused at an approval gate, waiting for a real human decision
//   done       run finished (completed / rejected / conversational / error)
const STATE_LABEL = {
  idle: "Idle - press Cmd+Shift+Space to speak",
  listening: "Listening...",
  thinking: "Thinking...",
  doing: "Doing it...",
  approving: "Waiting for your approval",
  done: "Done",
};

export default function App() {
  const [state, setState] = useState("idle");
  const [connection, setConnection] = useState("connecting");
  const [transcript, setTranscript] = useState("");
  const [plan, setPlan] = useState([]);
  const [activity, setActivity] = useState([]);
  const [pendingApproval, setPendingApproval] = useState(null);
  const [reply, setReply] = useState("");
  const [error, setError] = useState("");
  const [captureInfo, setCaptureInfo] = useState(null);

  const recorderRef = useRef(null);
  const clientRef = useRef(null);

  const log = useCallback((line) => {
    window.jarvis.log(line);
    setActivity((prev) => [...prev, line].slice(-12));
  }, []);

  // ── WebSocket: the backend drives every state change after audio is sent ──
  useEffect(() => {
    const client = new AgentClient({
      onStatusChange: setConnection,
      onMessage: (msg) => {
        switch (msg.type) {
          case "pong":
            log(`backend reachable (${msg.server})`);
            break;
          case "transcript":
            setTranscript(msg.text);
            log(`transcript: "${msg.text}"`);
            break;
          case "plan":
            setPlan(msg.milestones);
            log(`plan: ${msg.milestones.length} milestones`);
            break;
          case "state":
            setState(msg.state === "done" ? "done" : msg.state);
            if (msg.reason) log(`state=${msg.state} (${msg.reason})`);
            break;
          case "milestone_start":
            log(`-> milestone ${msg.step_number}: ${msg.goal}`);
            break;
          case "tool_call":
            log(`   calling ${msg.tool}`);
            break;
          case "tool_result":
            log(`   ${msg.tool}: ${msg.success ? "OK" : "FAILED"} - ${msg.message}`);
            break;
          case "approval_required":
            setPendingApproval(msg.milestone);
            setState("approving");
            log(`APPROVAL NEEDED: ${msg.milestone.goal}`);
            break;
          case "approval_result":
            setPendingApproval(null);
            log(`approval ${msg.approved ? "GRANTED" : "REJECTED"}`);
            break;
          case "reply":
            setReply(msg.text);
            break;
          case "error":
            setError(msg.message);
            log(`ERROR: ${msg.message}`);
            break;
          default:
            break;
        }
      },
    });
    clientRef.current = client;
    client.connect();
    return () => client.close();
  }, [log]);

  // ── Hotkey: start/stop capture, then hand the PCM to the backend ──
  useEffect(() => {
    const off = window.jarvis.onHotkey(async ({ action }) => {
      if (action === "start") {
        setError("");
        setTranscript("");
        setPlan([]);
        setReply("");
        setActivity([]);
        setCaptureInfo(null);
        setPendingApproval(null);
        try {
          const recorder = new PCMRecorder();
          recorderRef.current = recorder;
          const { sampleRate } = await recorder.start();
          setState("listening");
          window.jarvis.reportRecordingState(true);
          log(`recording at ${sampleRate} Hz`);
        } catch (err) {
          setError(`microphone failed: ${err.message}`);
          setState("idle");
          // Resync main's toggle, or the next press would send "stop".
          window.jarvis.reportRecordingState(false);
        }
        return;
      }

      const recorder = recorderRef.current;
      if (!recorder) return;
      const captured = await recorder.stop();
      recorderRef.current = null;
      window.jarvis.reportRecordingState(false);
      if (!captured) return;

      setCaptureInfo(captured);
      log(
        `captured ${captured.samples} samples (~${captured.seconds.toFixed(1)}s), peak ${captured.peak}/32767`
      );
      if (captured.peak === 0) {
        setError("Captured audio was completely silent - check microphone permission.");
        setState("idle");
        return;
      }

      setState("thinking");
      const sent = clientRef.current.send({
        type: "audio",
        sample_rate: captured.sampleRate,
        sample_width: captured.sampleWidth,
        pcm_base64: captured.pcmBase64,
      });
      if (!sent) {
        setError("Backend not connected - is agent_server.py running?");
        setState("idle");
      }
    });
    return off;
  }, [log]);

  const decide = useCallback((approved) => {
    clientRef.current.send({ type: "approval_response", approved });
    setPendingApproval(null);
  }, []);

  // Enter approves, Escape rejects - a real keypress, same as a real click.
  useEffect(() => {
    if (!pendingApproval) return;
    const onKey = (e) => {
      if (e.key === "Enter") decide(true);
      if (e.key === "Escape") decide(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pendingApproval, decide]);

  return (
    <div style={S.shell}>
      <div style={S.row}>
        <strong>{STATE_LABEL[state] || state}</strong>
        <span style={S.dim}>backend: {connection}</span>
      </div>

      {transcript && (
        <div style={S.block}>
          <div style={S.dim}>heard</div>
          <div>"{transcript}"</div>
        </div>
      )}

      {captureInfo && (
        <div style={S.dim}>
          {captureInfo.seconds.toFixed(1)}s @ {captureInfo.sampleRate} Hz, peak {captureInfo.peak}
        </div>
      )}

      {reply && <div style={S.block}>{reply}</div>}

      {plan.length > 0 && (
        <ol style={S.list}>
          {plan.map((m) => (
            <li key={m.step_number}>
              {m.goal} {m.requires_approval ? <em>(needs approval)</em> : null}
            </li>
          ))}
        </ol>
      )}

      {pendingApproval && (
        <div style={S.approval}>
          <div>
            <strong>Approve this step?</strong>
          </div>
          <div>{pendingApproval.goal}</div>
          <div style={S.dim}>{pendingApproval.success_signal}</div>
          <div style={S.row}>
            <button onClick={() => decide(true)}>Approve (Enter)</button>
            <button onClick={() => decide(false)}>Reject (Esc)</button>
          </div>
        </div>
      )}

      {error && <div style={S.error}>{error}</div>}

      {activity.length > 0 && (
        <pre style={S.activity}>{activity.join("\n")}</pre>
      )}
    </div>
  );
}

// Minimum needed to see and interact with each state. Not a design.
const S = {
  shell: {
    fontFamily: "ui-monospace, monospace",
    fontSize: 12,
    padding: 12,
    background: "#fff",
    border: "1px solid #888",
    borderRadius: 6,
    height: "100%",
    boxSizing: "border-box",
    overflow: "auto",
  },
  row: { display: "flex", gap: 8, alignItems: "center", justifyContent: "space-between" },
  dim: { color: "#777" },
  block: { marginTop: 6 },
  list: { margin: "6px 0", paddingLeft: 18 },
  approval: { marginTop: 8, padding: 8, border: "2px solid #333", borderRadius: 4 },
  error: { marginTop: 6, color: "#a00" },
  activity: { marginTop: 8, maxHeight: 120, overflow: "auto", background: "#f4f4f4", padding: 6 },
};
