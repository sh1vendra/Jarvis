import React, { useCallback, useEffect, useRef, useState } from "react";
import { PCMRecorder } from "./audio/recorder.js";
import { AgentClient } from "./ws/client.js";
// All visual design lives in styles.css, keyed off data-state on the root
// element - this file owns state and wiring only.
import "./styles.css";

// The state machine contract the stylesheet designs around:
//
//   idle       nothing happening, waiting for the hotkey
//   listening  microphone is open, user is speaking
//   thinking   audio sent; transcribing, then planning
//   doing      Action agent is executing milestones
//   approving  paused at an approval gate, waiting for a real human decision
//   done       run finished (completed / rejected / conversational / error)
const STATE_LABEL = {
  idle: "Jarvis",
  listening: "Listening",
  thinking: "Thinking",
  doing: "Working",
  approving: "Approval needed",
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
    <div className="stage" data-state={state}>
      <div className="glass">
        {/* Frameless windows draw no OS titlebar, so there is no built-in
            way to move or minimize/close this window. The whole glass
            surface is `-webkit-app-region: drag` (Electron reads that CSS
            property to know a mousedown should move the window instead of
            hitting the page); these buttons and every interactive element
            are marked `no-drag` in the stylesheet so clicking them doesn't
            start a drag first. */}
        <div className="controls">
          <button className="winBtn" onClick={() => window.jarvis.minimize()} title="Minimize">
            &#8211;
          </button>
          <button className="winBtn" onClick={() => window.jarvis.closeWindow()} title="Close">
            &#215;
          </button>
        </div>

        <div className="header">
          <span className="orb" />
          <span className="stateLabel">{STATE_LABEL[state] || state}</span>
          {state === "idle" && <span className="hint">&#8984;&#8679;Space</span>}
          <span className="conn" data-conn={connection} title={`backend: ${connection}`} />
        </div>

        {state === "listening" && (
          <div className="wave" aria-hidden="true">
            <span />
            <span />
            <span />
            <span />
            <span />
          </div>
        )}

        <div className="body">
          {transcript && (
            <div className="transcript">
              <div className="caption">Heard</div>
              <div className="quote">"{transcript}"</div>
            </div>
          )}

          {captureInfo && (
            <div className="capture">
              {captureInfo.seconds.toFixed(1)}s &middot; {captureInfo.sampleRate} Hz &middot; peak{" "}
              {captureInfo.peak}
            </div>
          )}

          {reply && <div className="reply">{reply}</div>}

          {plan.length > 0 && (
            <ol className="plan">
              {plan.map((m) => (
                <li key={m.step_number}>
                  <span className="planGoal">{m.goal}</span>
                  {m.requires_approval ? <span className="badge">approval</span> : null}
                </li>
              ))}
            </ol>
          )}

          {pendingApproval && (
            <div className="approval">
              <div className="approvalTitle">Approve this step?</div>
              <div className="approvalGoal">{pendingApproval.goal}</div>
              <div className="approvalSignal">{pendingApproval.success_signal}</div>
              <div className="approvalActions">
                <button className="btn btnApprove" onClick={() => decide(true)}>
                  Approve <kbd>&#9166;</kbd>
                </button>
                <button className="btn btnReject" onClick={() => decide(false)}>
                  Reject <kbd>esc</kbd>
                </button>
              </div>
            </div>
          )}

          {error && <div className="error">{error}</div>}

          {activity.length > 0 && <pre className="activity">{activity.join("\n")}</pre>}
        </div>
      </div>
    </div>
  );
}
