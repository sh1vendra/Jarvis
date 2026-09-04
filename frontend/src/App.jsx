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
//   done       run finished, every milestone verified
//   failed     run finished but a milestone's tools reported failure - honest,
//              never silently shown as "done" (see planning.md)
//   cancelled  the user rejected an approval gate - the step never ran
const STATE_LABEL = {
  idle: "Jarvis",
  listening: "Listening",
  thinking: "Thinking",
  doing: "Working",
  approving: "Approval needed",
  done: "Done",
  failed: "Couldn’t complete that",
  cancelled: "Cancelled",
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
  const [wakeWord, setWakeWord] = useState({ available: false, listening: false });
  const [failedGoals, setFailedGoals] = useState([]); // goals whose tools didn't verify
  const [cancelledGoal, setCancelledGoal] = useState(""); // the step the user rejected

  const recorderRef = useRef(null);
  const clientRef = useRef(null);
  const autoStopRef = useRef(null); // { timers, done } for a wake-word capture

  // Trailing-silence auto-stop for the wake-word path (the hotkey stays a
  // manual toggle). RMS threshold and windows tuned for a quiet room; a
  // hard cap ensures a stuck capture always ends.
  const VAD_RMS_THRESHOLD = 0.02;
  const VAD_SILENCE_MS = 1200;
  const VAD_MIN_MS = 700;
  const VAD_HARD_CAP_MS = 9000;

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
            setState(msg.state);
            if (msg.state === "failed") {
              setFailedGoals(msg.failed_goals || []);
              log(`FAILED: ${(msg.failed_goals || [msg.reason]).join("; ")}`);
            } else if (msg.state === "cancelled") {
              setCancelledGoal(msg.goal || "");
              log(`CANCELLED: ${msg.goal || "step rejected"}`);
            } else if (msg.reason) {
              log(`state=${msg.state} (${msg.reason})`);
            }
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

  // Stop the recorder and hand the PCM to the backend. Idempotent - the
  // wake-word auto-stop timer and a manual hotkey stop can race, and
  // whichever loses is a no-op.
  const stopAndSend = useCallback(
    async (source) => {
      const timers = autoStopRef.current;
      if (timers) {
        clearTimeout(timers.hardCap);
        autoStopRef.current = null;
      }
      const recorder = recorderRef.current;
      if (!recorder) return;
      recorderRef.current = null;

      const captured = await recorder.stop();
      window.jarvis.reportRecordingState(false);
      if (!captured) return;

      setCaptureInfo(captured);
      log(
        `captured ${captured.samples} samples (~${captured.seconds.toFixed(1)}s), ` +
          `peak ${captured.peak}/32767 [stopped by ${source}]`
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
    },
    [log]
  );

  const beginCapture = useCallback(
    async (source) => {
      if (recorderRef.current) return; // already capturing
      setError("");
      setTranscript("");
      setPlan([]);
      setReply("");
      setActivity([]);
      setCaptureInfo(null);
      setPendingApproval(null);
      setFailedGoals([]);
      setCancelledGoal("");

      const recorder = new PCMRecorder();
      recorderRef.current = recorder;

      // The wake-word path has no "press again to stop", so the renderer
      // ends it: on ~1.2s of trailing silence after speech, or a hard cap.
      let onLevel;
      if (source === "wakeword") {
        const startedAt = performance.now();
        let lastLoud = startedAt;
        let heardSpeech = false;
        onLevel = (rms) => {
          const now = performance.now();
          if (rms >= VAD_RMS_THRESHOLD) {
            lastLoud = now;
            heardSpeech = true;
          }
          if (
            autoStopRef.current &&
            heardSpeech &&
            now - startedAt >= VAD_MIN_MS &&
            now - lastLoud >= VAD_SILENCE_MS
          ) {
            stopAndSend("silence");
          }
        };
        autoStopRef.current = {
          hardCap: setTimeout(() => stopAndSend("hard-cap"), VAD_HARD_CAP_MS),
        };
      }

      try {
        const { sampleRate } = await recorder.start({ onLevel });
        setState("listening");
        window.jarvis.reportRecordingState(true);
        log(`recording at ${sampleRate} Hz [triggered by ${source}]`);
      } catch (err) {
        recorderRef.current = null;
        if (autoStopRef.current) {
          clearTimeout(autoStopRef.current.hardCap);
          autoStopRef.current = null;
        }
        setError(`microphone failed: ${err.message}`);
        setState("idle");
        window.jarvis.reportRecordingState(false);
      }
    },
    [log, stopAndSend]
  );

  // ── Trigger: hotkey (toggle) or wake word (start + auto-stop) ──
  useEffect(() => {
    const off = window.jarvis.onHotkey(({ action, source }) => {
      if (action === "start") beginCapture(source || "hotkey");
      else stopAndSend(source || "hotkey");
    });
    return off;
  }, [beginCapture, stopAndSend]);

  // ── Wake-word availability (a second trigger; the hotkey always works) ──
  useEffect(() => {
    const off = window.jarvis.onWakeWordStatus((status) => {
      setWakeWord(status);
      log(
        status.available
          ? 'wake word: listening for "Jarvis"'
          : `wake word: off (${status.reason || "unavailable"})`
      );
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
    <div className="stage" data-state={state} data-wakeword={wakeWord.available ? "on" : "off"}>
      <div className="glass">
        {/* Frameless windows draw no OS titlebar. The drag region is the
            `.header` row only (see styles.css), NOT the whole glass: a
            `-webkit-app-region: no-drag` element that is `position: absolute`
            over a drag region does NOT actually carve itself out on macOS
            (the region is registered at its in-flow position, not where
            top/right put it), so an absolutely-positioned control chip over
            a full-surface drag region swallows its own clicks. Keeping the
            controls as a normal-flow child of the drag `.header`, marked
            `no-drag`, is what makes them clickable. See planning.md. */}
        <div className="header">
          <span className="orb" />
          <span className="stateLabel">{STATE_LABEL[state] || state}</span>
          {state === "idle" && (
            <span className="hint">{wakeWord.available ? '⌘⇧Space · “Jarvis”' : "⌘⇧Space"}</span>
          )}
          <span className="conn" data-conn={connection} title={`backend: ${connection}`} />
          <div className="controls">
            <button className="winBtn" onClick={() => window.jarvis.minimize()} title="Minimize">
              &#8211;
            </button>
            <button className="winBtn" onClick={() => window.jarvis.closeWindow()} title="Close">
              &#215;
            </button>
          </div>
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

          {state === "failed" && (
            <div className="outcome outcomeFailed">
              <div className="outcomeTitle">Jarvis couldn’t complete this</div>
              {failedGoals.length > 0 && (
                <ul className="outcomeList">
                  {failedGoals.map((g, i) => (
                    <li key={i}>{g}</li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {state === "cancelled" && (
            <div className="outcome outcomeCancelled">
              <div className="outcomeTitle">Cancelled — nothing was done</div>
              {cancelledGoal && <div className="outcomeDetail">{cancelledGoal}</div>}
            </div>
          )}

          {activity.length > 0 && <pre className="activity">{activity.join("\n")}</pre>}
        </div>
      </div>
    </div>
  );
}
