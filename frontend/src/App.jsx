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

// The setup screen's checklist, in the exact order it's shown - a fixed
// display order independent of the order results actually arrive in (the
// backend streams its checks one at a time over the WebSocket; two are
// purely frontend facts - see below). Every id here must match either a
// backend `setup_check_result.id` (see backend/tools/setup_checks.py) or
// one of the two frontend-only ids handled locally in this file.
const SETUP_CHECK_ORDER = [
  { id: "google_api_key", label: "Gemini API key" },
  { id: "backend_connection", label: "Backend connection" },
  { id: "mic_permission", label: "Microphone" },
  { id: "accessibility", label: "Accessibility" },
  { id: "automation_system_events", label: "Automation – System Events" },
  { id: "automation_reminders", label: "Automation – Reminders" },
  { id: "automation_spotify", label: "Automation – Spotify" },
  { id: "automation_chrome", label: "Automation – Google Chrome" },
  { id: "screen_recording", label: "Screen Recording" },
  { id: "chrome_extension", label: "Chrome extension" },
];

const SETUP_STATUS_TEXT = {
  checking: "checking…",
  passed: "OK",
  failed: "needs attention",
  unknown: "can’t verify yet",
};

const SETUP_INITIAL_CHECKS = Object.fromEntries(
  SETUP_CHECK_ORDER.map((c) => [c.id, { status: "checking", detail: "", fix_url: null }])
);

// Real current macOS mic permission (electron/main.js), not just "was a
// prompt shown at some point" - see preload.cjs. The deep link is the same
// verified x-apple.systempreferences URL scheme the backend's own checks
// use for Accessibility/Automation/Screen Recording (setup_checks.py) -
// duplicated here rather than round-tripped from the backend because this
// is the one check the backend process can't answer: mic permission is
// granted per-process, and the Electron app, not the Python backend, is
// the process that actually opens the microphone.
const MIC_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone";

function micCheckFromStatus(status) {
  if (status === "granted") {
    return { status: "passed", detail: "Microphone access is granted.", fix_url: null };
  }
  if (status === "not-determined") {
    return {
      status: "failed",
      detail: "macOS hasn't recorded a decision yet – grant access when prompted, or enable it directly.",
      fix_url: MIC_SETTINGS_URL,
    };
  }
  return {
    status: "failed",
    detail: `Microphone access is ${status} – Jarvis can't hear you until this is granted.`,
    fix_url: MIC_SETTINGS_URL,
  };
}

const SETUP_DISMISSED_KEY = "jarvis-setup-dismissed";

// Real scroll affordance for any scrollable region in this small, fixed-size
// window - a subtle bottom-edge fade (styles.css, [data-scroll-more="true"])
// that's only ever on when there is genuinely more content below the fold,
// not a static decoration. `el.dataset.scrollMore` is set imperatively
// (not React state) since scroll fires far too often to route through a
// re-render; a MutationObserver covers content growing while already
// mounted (new activity lines, new conversation turns) since none of these
// containers change their own box size when their content overflows it, so
// a ResizeObserver on the element itself would never fire for that.
// `remountKey` re-attaches everything when a conditionally-rendered element
// (`.activity`, `.conversation`) actually mounts - a plain ref doesn't
// trigger a re-run on its own when the element it points to appears.
function useScrollFade(ref, remountKey) {
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const update = () => {
      const hasMore = el.scrollHeight - el.clientHeight - el.scrollTop > 2;
      el.dataset.scrollMore = hasMore ? "true" : "false";
    };
    update();
    el.addEventListener("scroll", update, { passive: true });
    const observer = new MutationObserver(update);
    observer.observe(el, { childList: true, subtree: true, characterData: true });
    return () => {
      el.removeEventListener("scroll", update);
      observer.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [remountKey]);
}

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

  // Full session conversation history - hidden by default (see the toggle
  // button below), accumulated for as long as the app stays open. Every
  // turn is real, already-flowing data reused as-is, not a new capture
  // mechanism: user turns come from the same `transcript` event the single
  // "Heard" block already renders, Jarvis turns come from the same `speak`
  // event that drives real TTS (already trimmed for speech and already
  // carrying the personality-flavor layer where it applies - see
  // agent_server.py). Deliberately never cleared by beginCapture's
  // per-command reset below - it's meant to accumulate across the whole
  // session, not reset per command. No cross-session persistence - a fresh
  // launch is a fresh JS environment, so this is empty again with no code
  // needed to make that true.
  const [conversation, setConversation] = useState([]); // [{role: "user"|"jarvis", text}]
  const [transcriptOpen, setTranscriptOpen] = useState(false);

  // ── Setup-check screen ──
  // See planning.md for why this exists: a cold-start user hits seven
  // scattered, silent failure points with no guidance otherwise. Shown
  // before the normal idle pill on a fresh launch (or whenever something
  // still needs attention and hasn't been explicitly skipped), hidden and
  // remembered once the user dismisses it with real failures still present.
  const [setupChecks, setSetupChecks] = useState(SETUP_INITIAL_CHECKS);
  const [setupVisible, setSetupVisible] = useState(() => {
    try {
      return localStorage.getItem(SETUP_DISMISSED_KEY) !== "1";
    } catch {
      return true; // no localStorage access - default to showing it, never silently skip
    }
  });

  const recorderRef = useRef(null);
  const clientRef = useRef(null);
  const autoStopRef = useRef(null); // { timers, done } for a wake-word capture
  const conversationRef = useRef(null); // scrolled to bottom on new turns, see below
  const bodyRef = useRef(null); // the whole scrollable state-content column
  const activityRef = useRef(null);

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
            setConversation((prev) => [...prev, { role: "user", text: msg.text }]);
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
          case "setup_check_result":
            // One real backend-side setup check's result (see
            // backend/tools/setup_checks.py / agent_server.py's
            // run_setup_checks). setSetupChecks is the useState setter,
            // referentially stable for the component's lifetime, so
            // calling it directly here (rather than through a memoized
            // helper) is still always writing into the current state.
            setSetupChecks((prev) => ({
              ...prev,
              [msg.id]: { status: msg.status, detail: msg.detail || "", fix_url: msg.fix_url || null },
            }));
            break;
          case "setup_checks_complete":
            log("setup checks: all backend checks reported");
            break;
          case "speak":
            // The backend already decided the final text (personality
            // flavor and any trimming-for-speech both applied there, see
            // agent_server.py) - this just relays it to Electron main,
            // which is the only process that can actually spawn `say`.
            // Also the exact text that becomes this turn's conversation
            // entry - real spoken content, not a separate summary of it.
            if (msg.text) {
              log(`speaking: "${msg.text}"`);
              window.jarvis.speak(msg.text);
              setConversation((prev) => [...prev, { role: "jarvis", text: msg.text }]);
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

  // Keep the conversation panel scrolled to the newest turn - only matters
  // while it's actually open and rendered (conversationRef is null while
  // closed, since the panel isn't in the DOM at all then).
  useEffect(() => {
    if (transcriptOpen && conversationRef.current) {
      conversationRef.current.scrollTop = conversationRef.current.scrollHeight;
    }
  }, [conversation, transcriptOpen]);

  // Real "there's more below" scroll cue for every scrollable region in this
  // small, fixed-size window - see useScrollFade above. Declared after the
  // scroll-to-bottom effect above (same commit, same order) so this one
  // reads the conversation panel's scrollTop only after it's already been
  // moved to the bottom, not the stale pre-scroll value.
  useScrollFade(bodyRef, null);
  useScrollFade(activityRef, activity.length > 0);
  useScrollFade(conversationRef, transcriptOpen);

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

  // The voice-activity indicator - reuses the two real signals already
  // tracked for mic/TTS coordination, doesn't introduce a third. "user":
  // state === "listening" is exactly "the renderer's mic is open right
  // now", true regardless of trigger (hotkey or wake word) - the same fact
  // that already drives the mic_state(active=true) send above. "jarvis":
  // `speaking` is the same real `say` process lifecycle that already
  // drives tts_state. Checking "user" first makes the two mutually
  // exclusive by construction in the display itself, not just by relying
  // on the backend's own pause/resume guarantee (agent_server.py's
  // _sync_wakeword_pause_state) - belt and suspenders, since the hotkey
  // path's speech-interrupt-on-new-capture (main.js) has a real, if brief,
  // async gap between killing an in-progress `say` and its close event
  // actually reporting speaking:false.
  const voiceActivity = state === "listening" ? "user" : speaking ? "jarvis" : "neutral";

  // ── Setup-check screen: derived state + real check triggers ──

  // "backend_connection" is not a distinct probe - it's the exact same
  // `connection` state the header's own conn dot already reflects (see
  // ws/client.js), just projected into the checklist's shape so it can sit
  // in the list alongside everything else instead of being a special case.
  useEffect(() => {
    setSetupChecks((prev) => ({
      ...prev,
      backend_connection:
        connection === "connected"
          ? { status: "passed", detail: "Connected to the backend.", fix_url: null }
          : connection === "connecting"
            ? { status: "checking", detail: "", fix_url: null }
            : {
                status: "failed",
                detail: "Backend not reachable - make sure agent_server.py is running (cd backend && python servers/agent_server.py).",
                fix_url: null,
              },
    }));
  }, [connection]);

  // Real mic permission, queried on mount - see preload.cjs/main.js. No OS
  // event fires when this changes, so re-checking happens explicitly (this
  // effect on mount, recheckSetup on demand), never assumed still current.
  useEffect(() => {
    window.jarvis.getMicPermissionStatus().then((status) => {
      setSetupChecks((prev) => ({ ...prev, mic_permission: micCheckFromStatus(status) }));
    });
  }, []);

  // Runs the backend's real checks (google_api_key, accessibility,
  // screen_recording, the automation probes, chrome_extension) every time
  // the connection actually (re)establishes - including a reconnect after
  // the backend restarts, which is exactly when a just-fixed .env or a
  // just-granted permission needs to be seen again.
  useEffect(() => {
    if (connection === "connected") {
      clientRef.current.send({ type: "run_setup_checks" });
    }
  }, [connection]);

  const setupList = SETUP_CHECK_ORDER.map((c) => ({ ...c, ...setupChecks[c.id] }));
  const setupAnyFailed = setupList.some((c) => c.status === "failed");
  const setupAllDone = setupList.every((c) => c.status !== "checking");
  const setupAllGood = setupAllDone && !setupAnyFailed;

  // Once every check has landed on passed/unknown (never "checking") and
  // none are "failed", there's nothing left to show the user - transition
  // to the normal idle pill on its own, after a brief "all set" beat rather
  // than an instant cut.
  useEffect(() => {
    if (!setupVisible || !setupAllGood) return undefined;
    const t = setTimeout(() => {
      setSetupVisible(false);
      try {
        localStorage.removeItem(SETUP_DISMISSED_KEY);
      } catch {
        // No localStorage access - nothing to clear, and setSetupVisible above
        // already took effect for this session regardless.
      }
    }, 900);
    return () => clearTimeout(t);
  }, [setupVisible, setupAllGood]);

  // Re-runs every real check live, without a restart - what makes "fix the
  // .env, come back and see it pass" actually possible instead of forcing a
  // relaunch. Resets everything except backend_connection (that one is
  // driven purely by the live socket state above, not a one-shot probe).
  const recheckSetup = useCallback(() => {
    setSetupChecks((prev) => {
      const next = { ...prev };
      for (const c of SETUP_CHECK_ORDER) {
        if (c.id !== "backend_connection") next[c.id] = { status: "checking", detail: "", fix_url: null };
      }
      return next;
    });
    window.jarvis.getMicPermissionStatus().then((status) => {
      setSetupChecks((prev) => ({ ...prev, mic_permission: micCheckFromStatus(status) }));
    });
    clientRef.current.send({ type: "run_setup_checks" });
  }, []);

  // Explicit skip while real failures remain - remembered so the full
  // screen doesn't force itself on every single launch once acknowledged,
  // but never silently: setupIndicatorVisible below keeps a small dot in
  // the normal header for as long as something is actually still missing.
  const dismissSetup = useCallback(() => {
    setSetupVisible(false);
    if (setupAnyFailed) {
      try {
        localStorage.setItem(SETUP_DISMISSED_KEY, "1");
      } catch {
        // No localStorage access - can't remember the choice, so the setup
        // screen will simply show again next launch. Not worse than before.
      }
    }
  }, [setupAnyFailed]);

  // The persistent "something's still missing" indicator - only real once
  // every check has actually reported (setupAllDone), so a still-in-flight
  // check never flashes it on for a moment before its real result arrives.
  const setupIndicatorVisible = !setupVisible && setupAllDone && setupAnyFailed;

  if (setupVisible) {
    return (
      <div className="stage" data-state="setup">
        <div className="glass glassSetup">
          <div className="header">
            <span className="orb" />
            <span className="stateLabel">Setup check</span>
            <div className="controls">
              <button className="winBtn" onClick={() => window.jarvis.minimize()} title="Minimize">
                &#8211;
              </button>
              <button className="winBtn" onClick={() => window.jarvis.closeWindow()} title="Close">
                &#215;
              </button>
            </div>
          </div>
          <div className="body setupBody">
            <p className="setupIntro">Making sure Jarvis can actually do its job before handing you the mic.</p>
            <ul className="setupList">
              {setupList.map((c) => (
                <li className="setupItem" data-status={c.status} key={c.id}>
                  <span className="setupDot" data-status={c.status} />
                  <div className="setupItemBody">
                    <div className="setupItemHead">
                      <span className="setupItemLabel">{c.label}</span>
                      <span className="setupItemStatus">{SETUP_STATUS_TEXT[c.status]}</span>
                    </div>
                    {c.detail && <div className="setupItemDetail">{c.detail}</div>}
                    {c.status === "failed" && c.fix_url && (
                      <button className="setupFixBtn" onClick={() => window.jarvis.openExternal(c.fix_url)}>
                        Open System Settings
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
            <div className="setupActions">
              <button className="btn" onClick={recheckSetup}>
                Recheck
              </button>
              {setupAnyFailed && (
                <button className="btn btnGhost" onClick={dismissSetup}>
                  Skip for now
                </button>
              )}
            </div>
            {setupAllGood && <div className="setupAllGood">All set — starting Jarvis…</div>}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div
      className="stage"
      data-state={state}
      data-wakeword={wakewordStatus}
      data-voice={voiceActivity}
      data-transcript={transcriptOpen ? "open" : "closed"}
    >
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
          <span
            className="voiceActivity"
            data-voice={voiceActivity}
            title={
              voiceActivity === "user"
                ? "listening to you"
                : voiceActivity === "jarvis"
                  ? "Jarvis is speaking"
                  : "no audio flowing"
            }
          />
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
          {setupIndicatorVisible && (
            // A real, never-silent reminder: shown only once every check
            // has actually reported and at least one genuinely failed - not
            // a static "you skipped setup" badge that outlives the problem.
            // Clicking it reopens the same real checklist, not a summary.
            <button
              className="setupIndicator"
              onClick={() => setSetupVisible(true)}
              title="Setup still has something that needs attention"
            />
          )}
          <div className="controls">
            <button
              className="winBtn"
              onClick={() => setTranscriptOpen((v) => !v)}
              aria-pressed={transcriptOpen}
              title={transcriptOpen ? "Hide conversation" : "Show conversation"}
            >
              <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path
                  d="M3 3h10a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H7l-3 3v-3H3a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"
                  stroke="currentColor"
                  strokeWidth="1.3"
                  strokeLinejoin="round"
                  strokeLinecap="round"
                />
              </svg>
            </button>
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

        <div className="body" ref={bodyRef}>
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

          {activity.length > 0 && (
            <pre className="activity" ref={activityRef}>
              {activity.join("\n")}
            </pre>
          )}

          {/* Hidden by default (see the toggle button in the header) - an
              extra block appended after everything else this state already
              shows, not a replacement for the single "Heard" quote above.
              Works the same way in every state since visibility here is
              driven purely by transcriptOpen, not data-state - idle/
              listening just show this with nothing else above it. */}
          {transcriptOpen && (
            <div className="conversation" ref={conversationRef}>
              {conversation.length === 0 ? (
                <div className="conversationEmpty">Nothing said yet this session.</div>
              ) : (
                conversation.map((turn, i) => (
                  <div key={i} className={`turn turn${turn.role === "user" ? "User" : "Jarvis"}`}>
                    <div className="turnLabel">{turn.role === "user" ? "You" : "Jarvis"}</div>
                    <div className="turnText">{turn.text}</div>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
