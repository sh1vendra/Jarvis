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
  const [wakeWord, setWakeWord] = useState({ available: false, reason: "" });
  const [speaking, setSpeaking] = useState(false); // true exactly while `say` is actually playing (see main.js)
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
              const goals = msg.failed_goals || [];
              setFailedGoals(goals);
              const lines = goals.length
                ? goals.map((g) => (g.message ? `${g.goal} - ${g.message}` : g.goal))
                : [msg.reason || "unknown"];
              log(`FAILED: ${lines.join("; ")}`);
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
          case "agent_text":
            // The Action agent's own reply text - e.g. a clarifying
            // question it asked instead of guessing at an ambiguous
            // Spotify result, rather than a tool result. Was previously
            // silently dropped (no case here at all) - the only place
            // that text was visible was the backend's own stdout.
            if (msg.text) log(`Jarvis: ${msg.text}`);
            break;
          case "error":
            setError(msg.message);
            log(`ERROR: ${msg.message}`);
            break;
          case "wakeword_status":
            // Backend-driven, not Electron IPC - openWakeWord runs in
            // Python (no Node binding), so this travels over the same
            // WebSocket connection everything else does. Sent once per
            // connection; see agent_server.py.
            setWakeWord({ available: Boolean(msg.available), reason: msg.reason || "" });
            log(
              msg.available
                ? 'wake word: listening for "Hey Jarvis"'
                : `wake word: off (${msg.reason || "unavailable"})`
            );
            break;
          case "wakeword_detected":
            // Reuses the exact same convergence point the hotkey uses -
            // from here on, a wake-word-triggered capture is
            // indistinguishable from a hotkey-triggered one.
            log('wake word: "Hey Jarvis" detected');
            beginCapture("wakeword");
            break;
          case "speak":
            // The backend already decided the final text (personality
            // flavor and any trimming-for-speech both applied there, see
            // agent_server.py) - this just relays it to Electron main,
            // which is the only process that can actually spawn `say`.
            if (msg.text) {
              log(`speaking: "${msg.text}"`);
              window.jarvis.speak(msg.text);
            }
            break;
          default:
            break;
        }
      },
    });
    clientRef.current = client;
    client.connect();
    return () => client.close();
    // beginCapture is intentionally not in this array: it, stopAndSend, and
    // log are all stable for the component's lifetime (log has no deps;
    // stopAndSend and beginCapture only ever depend on stable values), so
    // this effect still only runs once, and wakeword_detected always calls
    // the current (only) implementation via closure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
      // Tells the backend's wake-word listener it can re-acquire the mic -
      // unconditionally, regardless of what triggered this capture, since
      // the backend needs to know "is ANY renderer capture using the mic
      // right now", not just wake-word-triggered ones. See agent_server.py.
      clientRef.current.send({ type: "mic_state", active: false });
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
        // Same mic-handoff signal as stopAndSend's release, the other
        // direction - pauses the backend's wake-word listener so it never
        // contends with this capture for the microphone.
        clientRef.current.send({ type: "mic_state", active: true });
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

  // ── Speaking status: real `say` process lifecycle, reported by main.js ──
  // Relayed straight to the backend as `tts_state` so it can pause the
  // wake-word listener while audio is actually playing through the
  // speakers - otherwise Jarvis's own voice risks being picked up by its
  // own wake-word mic and false-triggering or confusing detection. `speaking`
  // itself is also kept as real component state for the UI - not yet given
  // its own visual treatment (that's the queued Dynamic-Island-style work),
  // but genuinely tracked and available now rather than only inferred.
  useEffect(() => {
    const off = window.jarvis.onSpeakingStatus(({ speaking: isSpeaking }) => {
      setSpeaking(Boolean(isSpeaking));
      clientRef.current.send({ type: "tts_state", speaking: Boolean(isSpeaking) });
    });
    return off;
  }, []);

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

  // Three real states, not just on/off: "unavailable" (openwakeword/mic
  // dependencies missing on the backend - hotkey still works, nothing
  // wrong), "paused" (available, but this exact renderer capture has its
  // mic right now - state "listening" is precisely when that's true, see
  // the mic_state sends above), "listening" (backend is actively listening
  // for "Hey Jarvis"). Derived locally rather than round-tripped from the
  // backend on every pause/resume - mic_state's own send is what drives the
  // backend's pause/resume, so this side already knows the same thing.
  const wakewordStatus = !wakeWord.available ? "unavailable" : state === "listening" ? "paused" : "listening";

  return (
    <div className="stage" data-state={state} data-wakeword={wakewordStatus} data-speaking={speaking}>
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
            <span className="hint">{wakeWord.available ? '⌘⇧Space · “Hey Jarvis”' : "⌘⇧Space"}</span>
          )}
          <span
            className="wakeword"
            data-wakeword={wakewordStatus}
            title={
              wakeWord.available
                ? state === "listening"
                  ? 'wake word paused - this capture has the microphone'
                  : 'wake word listening for "Hey Jarvis"'
                : `wake word unavailable${wakeWord.reason ? `: ${wakeWord.reason}` : ""}`
            }
          />
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
                    <li key={i}>
                      {g.goal}
                      {g.message && <div className="outcomeDetail">{g.message}</div>}
                    </li>
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
