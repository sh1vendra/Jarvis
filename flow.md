# System Flow

Technical walkthrough of how a command actually moves through Jarvis, end to
end, referencing real files/functions. Updated whenever the request
lifecycle or a component's behavior changes.

## 0. The two entry points, and which one is the product

**Jarvis is voice-first: real spoken audio is the only entry point the
product has.** There are now two ways to drive the pipeline by voice, and
only the first is the product:

- **The Electron app** (`frontend/`) - global hotkey, audio captured in the
  renderer, WebSocket to `backend/servers/agent_server.py`, UI state
  machine, real approval clicks. This is the real lifecycle; see section 10.
- **`backend/main.py`** - the command-line harness. Same pipeline, but
  capture is `sounddevice` in Python and the approval gate is an `input()`
  keypress. Retained for testing the agent chain without the UI in the loop;
  it is scaffolding, not the product.

Section 1 below describes the CLI harness. Section 10 describes the real
Electron lifecycle.

## 1. Entry point (CLI harness)

`backend/main.py` run with no arguments records each demo command from the
microphone, one at a time, and runs it through the full chain. Typed
commands still exist (`python main.py --typed`) but only as agent-logic
regression scaffolding; a demo command is not considered working until
it has been driven by real voice.

`load_dotenv()` pulls `GOOGLE_API_KEY` from the repo-root `.env` before any
ADK/Gemini call is made. `logging.basicConfig(level=logging.INFO, ...)` is
set up here so that `agents/verifier_callbacks.py`'s logger and
`tools/mac_control.py`'s zoom-search logger both actually print.

**The voice lifecycle, per command:**
1. `voice/capture.py`'s `record_push_to_talk(device=...)` records mic audio
   between two Enter presses and returns a `speech_recognition.AudioData`
   (section 9).
2. `run_voice_command(runner, session_id, audio)` calls
   `voice.stt.transcribe_audio(audio)` - one real HTTP call to Google's
   free endpoint - to turn that audio into a transcript string, prints the
   transcript prominently, then hands it to `run_command` exactly as if it
   had been typed. Nothing downstream of `transcribe_audio` knows the
   command came from voice; the Orchestrator receives an ordinary string.
3. `run_command` -> Orchestrator -> (transfer) -> Planner -> `MilestonePlan`
   (sections 2-3).
4. `run_plan_with_approval_gate(action_runner, session_id, milestones)`
   feeds each milestone to the Action agent in order, pausing at every
   `requires_approval` milestone on a real Enter press that stands in for
   the user approving it in the Electron overlay's approval card, then
   resuming (sections 4, 8).

`run_voice_session(only=None, device=None)` is the loop over the three demo
commands. It starts the in-process browser bridge task first (the Kayak
command needs it; harmless for the others), then for each command prints
its precondition, waits for Enter, and calls `run_spoken_command`. The only
real precondition left is Spotify being installed and running - the Kayak
command opens Chrome and navigates to Kayak itself (first milestone,
`navigate_to_url`); the reminder command has none. `python main.py
spotify|reminder|kayak` runs just one;
`python main.py --list-devices` prints the input devices and exits;
`--device N` forces an input-device index.

`voice.stt.SimulatedAudio` (a known transcript `transcribe_audio` returns
verbatim, no network) is still in the codebase for unit tests but is no
longer wired into any `main.py` run - see `planning.md` for why it was
removed from the default path.

Two ADK runner types are used, each wrapping one agent:
- `InMemoryRunner(agent=orchestrator_agent, ...)` - drives a command
  through the Orchestrator (and, via transfer, the Planner).
- `InMemoryRunner(agent=action_agent, ...)` - drives one milestone goal at a
  time through the Action agent.

Each command gets its own fresh orchestrator and action session
(`session_service.create_session`), so commands in one voice session don't
share conversation state.

## 2. Orchestrator's decision logic

`agents/orchestrator.py` defines `orchestrator_agent`, an ADK `LlmAgent`
with `sub_agents=[planner_agent]`. Giving it `sub_agents` is what causes ADK
to automatically attach a `transfer_to_agent` tool - there's no manual
routing code anywhere; the instruction text tells the model the two cases
("simple conversational input" vs "a real task") and the model itself
decides whether to answer directly or call `transfer_to_agent` to hand off
to `planner_agent`.

`main.run_command()` drives this: it streams every event from
`runner.run_async(...)`, printing each part's text or `function_call` (the
transfer call shows up here), and captures the final response along with
which agent (`event.author`) actually produced it.

## 3. Planner's milestone generation

`agents/planner.py` defines two Pydantic models:
- `Milestone`: `step_number`, `goal` (an outcome, never a literal action),
  `success_signal` (an observable signal that the outcome was reached),
  `requires_approval` (bool, default `False` - true only for a milestone
  whose completion performs a final, hard-to-reverse, consequential
  action; see section 8's plan-approval-pause entry).
- `MilestonePlan`: `milestones: list[Milestone]`.

`planner_agent` is a leaf agent (no `sub_agents`, no tools) with
`output_schema=MilestonePlan` - this forces Gemini's final reply to be JSON
that validates against `MilestonePlan`, so `main.run_command()` can do
`MilestonePlan.model_validate_json(final_text)` directly instead of parsing
free text. The instruction explicitly contrasts an outcome-shaped milestone
("Spotify is open and in the foreground") against a forbidden
action-shaped one ("click the Spotify icon at position X,Y") to keep the
model from collapsing into step-by-step output, and separately instructs
the model to give a task's final consequential action its own milestone
with `requires_approval=true` rather than folding it into an earlier step.

The instruction also carries a **milestone-granularity** rule: one
milestone per distinct real outcome, and never split one atomic action
into a "prepare" milestone plus a "commit" milestone. Splitting is only for
genuine intermediate states someone could inspect or cancel - a web form
filled but not submitted. This exists because the reminder command was
being split into "details entered" + "saved", and each reminder-shaped
milestone triggered its own `create_reminder` call (section 7). The
`requires_approval` guidance was tightened at the same time: creating a
routine local item (reminder, note, calendar event) is explicitly *never*
approval-worthy - only actions that spend money, contact other people, or
submit to an external site are.

**Memory read (Tier 1, see section 11).** Before the Planner ever runs,
`run_command` calls `memory.store.relevant_preferences(text)`. If a stored
preference matches the command by keyword, it's appended to the user
message as a `[Known user preferences ...]` block, and a `preferences_applied`
event is emitted. The instruction has one paragraph telling the Planner to
use such a block only to fill in a detail the command left unspecified
(a default flight city, who "mom" is) and never to override something
stated explicitly. Verified: with `default_flight_destination` stored,
"search Kayak for a flight" (no destination) produces a plan whose
milestone reads "the destination Austin, Texas is entered".

## 4. Action agent's tool selection and execution loop

`agents/action.py` defines `action_agent`, given nine tools -
`open_app_tool`, `search_spotify_candidates_tool`, `click_ui_tool`,
`type_in_field_tool`, `create_reminder_tool` (native macOS control, from
`tools/mac_control.py`) plus `navigate_to_url_tool`, `find_web_element_tool`,
`click_web_element_tool`, `type_in_web_field_tool` (browser control, from
`tools/browser_tools.py`, see section 8). It receives one milestone goal
per turn (driven by
`main.run_action()`, which loops over `plan.milestones` and sends each
`milestone.goal` as a new message in the *same* session). Because milestones
for one task share a session, the agent can infer context across calls -
e.g. if it already called `open_app('Spotify')` for milestone 1, it infers
milestone 2 ("Billie Jean is playing") is still about Spotify without
being told again.

The instruction maps milestone shape to tool choice: "app open/foreground"
-> `open_app`; **"a specific song/track/artist is playing in Spotify"
-> `search_spotify_candidates(query)` then reason, then act.** This
replaced `play_spotify_track` (Web-API-based - still defined in
`mac_control.py`, just no longer wired into the agent's tools, since this
account has no configured `SPOTIFY_CLIENT_ID`/`SECRET` and the Web API is
blocked for it), which itself had replaced the original `type_in_field` +
`click_ui` search-and-click path (documented at ~1/3 reliability, and it
failed the regression test). `search_spotify_candidates` opens Spotify's
search via the same keyboard-shortcut mechanism `type_in_field` uses for
it (no click), types+submits the query, and reads back the visible results
via one vision call on a window-scoped screenshot - it never plays
anything, and its own `success` is hardcoded `False` so it can never be
mistaken for milestone completion on its own. Its result also carries a
*deterministic* `ambiguous`/`ambiguity_reason` pair - true when two or
more visible candidates share a matching (normalized) title under
different artists and the query didn't already name one of them - computed
in Python (`_detect_spotify_ambiguity`), not left to the model's own
judgment: tested directly, the small/fast model this project uses for the
Action agent (`gemini-flash-lite-latest`) missed this exact case twice even
with an explicit prompt instruction to check for it (see planning.md). The
instruction now treats `ambiguous` as a hard rule to check first:
- **`ambiguous: true`** -> the agent calls no further tool at all; it just
  answers in text, naming the real candidates and asking which one was
  meant (or stating which it's about to play, if reasonably confident).
  With no tool call after the read step, `run_action`'s "last tool's
  success" rule naturally makes this milestone report as not completed -
  reusing the existing failed-state machinery to represent "asked instead
  of guessing," rather than adding a new subsystem.
- **`ambiguous: false`** -> the agent still reasons over each candidate's
  `kind` (Spotify's own Live/Remix/Music Video badge, when shown) before
  acting, then calls `click_ui` for "the top search result" - a phrase
  that must include "top"/"first" plus "result"/"track"/"song" to route
  into `click_ui`'s existing `_APP_TOP_RESULT_OFFSET` fixed-offset click
  (a known, pre-verified position, not a fresh vision guess), verified
  afterward via `_spotify_player_state()`. There is still no way to select
  or play any result other than the top one - a genuinely ambiguous case
  with no good top candidate has no third option but to ask.

"something clicked"
-> `click_ui` (requires `expected_app_name`); "text entered" ->
`type_in_field` (same requirement); "reminder exists" -> `create_reminder`
(extracts `task`/`due_date`/`due_time` as plain text, no manual date
computation by the model);
"browser is open at a website" (e.g. "Google Chrome is open with
www.kayak.com loaded") -> `navigate_to_url`, with the agent turning a site
name into a URL itself (Kayak -> `https://www.kayak.com`) - this is always
the first web milestone; a milestone about a specific element on a *web
page* -> `find_web_element` first (to get a `ref_id`), then
`click_web_element`/`type_in_web_field` with that `ref_id`. If no tool
fits, or a tool refuses to act, the agent is told to report that plainly
rather than retry blindly.

`agents/verifier_callbacks.py`'s `log_tool_result_callback` is registered as
`action_agent`'s `after_tool_callback` - ADK calls it after every tool
invocation. It logs success/failure based on the tool's own `success` field
in its returned dict, including which location `tier` resolved the target
when present (`tool_response.get("tier")`). It always returns `None`, i.e.
it only observes - it never overrides what the agent actually sees.

## 5. Two-tier UI targeting: accessibility first, vision fallback

`tools/mac_control.py`'s `_locate_element(app_name, target_description,
roles=None, skip_reveal=False)` is the shared core behind both `click_ui`
and `type_in_field`:

1. **Tier 1 - Accessibility API.** `_best_ax_match()` calls
   `perception.get_ui_tree(app_name)`, which walks the app's AX tree (via
   `ApplicationServices`/pyobjc) and returns interactive elements
   (`AXButton`, `AXTextField`, etc. - see `_INTERESTING_ROLES` in
   `perception.py`). Each element's label is fuzzy-matched
   (`difflib.SequenceMatcher`) against `target_description`; the best match
   above `_FUZZY_MATCH_THRESHOLD = 0.45` wins and its on-screen center is
   returned directly - no LLM call.

2. **Tier 2 - vision fallback**, only reached if tier 1 finds nothing above
   threshold. `_locate_via_vision_zoom(target_description, app_name)` (also
   in `mac_control.py`) is an iterative crop-and-zoom search: it takes a
   screenshot of just `app_name`'s own real, verified on-screen window
   (`perception.capture_screenshot(app_name=app_name)`, scoped via
   `CGWindowListCopyWindowInfo` ground truth - never the whole display
   unless a caller explicitly opts into `allow_full_display=True`, which
   nothing in this codebase currently does; see planning.md's "a real
   privacy incident" entry for why this is scoped by default rather than
   full-screen), asks Gemini vision
   (`_ask_vision_for_coordinates_in_image`) for the target's coordinates,
   then crops tightly around that guess (`_crop_image`) and asks again on
   the smaller image - up to `_MAX_ZOOM_ITERATIONS = 2` times, shrinking the
   crop by `_ZOOM_SHRINK_FACTOR` each round, stopping early once a crop
   would fall below `_ZOOM_TIGHT_THRESHOLD` pixels. Each crop-local answer
   is translated back to full-screenshot pixel space via
   `_translate_crop_coords` before the final result is divided by
   `_screen_scale_factor()` (Retina scaling) to produce point-space
   coordinates ready for a click. Every iteration is logged
   (`logger.info(...)` in `_locate_via_vision_zoom`) showing the crop region
   and guessed coordinates at each level.

   If `target_description` looks like something that might currently be a
   collapsed icon (`_looks_collapsible`, matched against
   `_COLLAPSIBLE_FIELD_HINTS` like "search field"/"search bar"), and
   `skip_reveal` isn't set, the located coordinates are clicked once, the
   UI is given 0.5s to expand, and the zoom search runs again against a
   fresh screenshot rather than trusting the first guess was already the
   real, expanded field.

   **Known limitation:** for Spotify's specific collapsed search icon, the
   zoom search does not reliably improve on a single-shot guess - tested
   directly against a known crop with a visually-confirmed ground-truth
   icon location, vision landed on the same wrong spot 7/7 tries across two
   different descriptions. See `planning.md`'s zoom-search entry. As a
   result, `type_in_field` special-cases apps with a known keyboard
   shortcut for opening a search UI (`_APP_SEARCH_SHORTCUTS`, currently
   `{"Spotify": (37, Quartz.kCGEventFlagMaskCommand)}` for Cmd+L - confirmed
   against Spotify's actual Edit menu via `AXMenuItemCmdChar`, not guessed)
   and uses that instead of clicking, when `_looks_collapsible` matches. The
   field opens already focused, so no click is dispatched. `_locate_element`
   is called afterward only as a fallback (with `skip_reveal=True`, so it
   won't speculatively click again) purely to get a region for
   verification - the primary path anchors that region on the app's own AX
   window frame instead (`get_frontmost_window_frame` +
   `_APP_SEARCH_FIELD_OFFSET`, with a few short retries since the window can
   report itself frontmost slightly before its AX window is queryable right
   after a fresh launch), since re-asking vision to relocate the *now-open*
   field was measured to be just as unreliable as the original icon lookup.

   **Vision-based location of Spotify's search results never became
   reliable, across three different approaches** - `_locate_via_vision_zoom`
   (0/10 against a verified ground truth, errors 158-661pt),
   `locate_and_click_via_grid_search` (1/3, then 0/5 against a cleaner
   target - see below), and a full unfiltered AX dump (genuinely nothing
   to read, not a filtering artifact). All three are documented in
   `planning.md`. `locate_and_click_via_grid_search` still exists in
   `mac_control.py` as a working, tested building block, but is **not
   called from `click_ui` or anywhere else** - its hit rate never cleared
   the bar to hand a real click through.

   **What actually resolved milestone 2, for this specific demo command:**
   two changes together, not vision precision. First, the demo command
   changed (`backend/main.py`) from `"open Spotify and play some lo-fi
   music"` to `"open Spotify and play Billie Jean by Michael Jackson"` - a
   specific track/artist query reliably surfaces Spotify's "Top result"
   card (a persistent, always-visible play button), not a genre page's
   navigate-only tiles. Second, `click_ui` special-cases this exact card:
   `_APP_TOP_RESULT_OFFSET` (`{"Spotify": (0.56, 179.0)}`) plus
   `_looks_like_top_result(target_description)` resolve its location via
   `_locate_via_window_offset` (a window-frame offset, the same shape as
   `_APP_SEARCH_FIELD_OFFSET`) instead of vision entirely, labeled
   `tier: "fixed_offset"`. This card's position was independently verified
   fixed at point (448, 218) across two different queries. **This is an
   explicitly-documented demo-only simplification for one specific,
   confirmed-fixed target, not a general fix for search-result click
   precision** - implemented only after the user was asked and confirmed
   it was an acceptable tradeoff (see `planning.md`).

   Getting the *full agent chain* (not just direct calls) to reliably
   reach and use this took four more fixes, all found live: the Action
   agent's instruction was ambiguous about "X is searched" milestones
   (clarified to mean `type_in_field`, not `click_ui`); the top-result
   matcher was broadened from exact-phrase to position-word-plus-noun-word
   matching after the agent phrased it as "first track result" and missed
   the original exact check; `type_in_field` now sends Cmd+A before every
   paste (not just for Spotify) after a screenshot caught leftover search
   text silently concatenating instead of being replaced; and
   `type_in_field`'s Spotify-shortcut path now presses Return after
   pasting, since a paste alone only populates the autocomplete dropdown,
   never the actual results page the fixed offset targets. Full 3-run
   result: 3/3, every run's final `click_ui` call verified via a genuine
   `paused` -> `playing` transition read from Spotify's real player state.

Every click is dispatched via `_dispatch_click` (raw Quartz
`CGEventCreateMouseEvent`/`CGEventPost` at the HID event tap level, chosen
over AppleScript System Events clicks specifically to avoid focus/routing
issues those can hit when the target app isn't already frontmost).

Both `click_ui` and `type_in_field` call
`_verify_expected_app_frontmost(expected_app_name)` first and refuse to act
if the actual frontmost app (via `_frontmost_app_name()`, a fresh
`osascript`/System Events query - never AppKit's `NSWorkspace`, which was
found to go stale under subprocess-heavy polling) doesn't match. This exists
because a stray focus change once caused a `type_in_field` call to land in
this repo's `.gitignore` instead of Spotify.

## 6. Verification

**`open_app(app_name)`** doesn't trust `open -a`'s exit code as proof of
success. After launching, it repeatedly calls an inner `_activate()`
(AppleScript `tell application "X" to activate`) on every poll tick for up
to 8 seconds, checking `_frontmost_app_name()` each time, so a focus-steal
mid-poll gets corrected rather than just detected-and-failed.

**`type_in_field`'s `_verify_text_entered(app_name, expected_text,
before_region_png, region_center)`** is the harder case, since a paste
command exiting cleanly proves nothing about whether the text landed
anywhere real:

- **Tier A:** a fresh (uncached) call to `perception.get_field_values(app_name)`
  reads actual AX field *values* (not labels). If any field's value
  contains `expected_text`, that's an exact, model-free confirmation.
- **Tier B** (only when tier A finds no text fields at all - Spotify's AX
  tree exposes none): `_region_pixel_diff_score(before_png, after_png)`
  computes a deterministic mean grayscale diff between a screenshot taken
  right before the click/paste and one taken right after, both from the
  same region (`capture_region`, sized `_VERIFY_REGION_SIZE`). If the diff
  is below `_NO_CHANGE_DIFF_THRESHOLD`, that's treated as a confident "the
  click missed" with no model call at all. Only if something visibly
  changed does the code ask Gemini vision to confirm the *content* of that
  change matches `expected_text`, and only on the small cropped region, not
  the whole screen. This two-step design exists because a single
  whole-screen "is the text visible?" vision call was measured to give a
  false positive 1 time in 5 against an unchanged screen - pixel diff is the
  hard gate; vision is only a tie-breaker once diff already proved
  something happened.

`click_ui`/`type_in_field` report which tier (`"accessibility"`, `"vision"`,
or `"keyboard_shortcut"`) actually resolved the target in their returned
`tier` field, and `verifier_callbacks.py` surfaces that in its log line so
it's visible during a live run which path a given interaction actually took.

**`click_ui`'s `_verify_click_outcome(app_name, expected_outcome,
before_player_state, before_region_png, region_center)`** verifies a click
actually produced its intended effect, instead of reporting success as soon
as the click is dispatched (the gap that let `click_ui` once falsely report
success clicking Spotify's "first search result play button" while the
previously-playing track kept playing unchanged - see `planning.md`).
Unlike typed text, a click has no single universal success signal - what
"worked" depends entirely on what the click was for - so `click_ui` now
takes a required `expected_outcome` argument (the specific, observable
effect the click should cause, e.g. "a lo-fi track starts playing") and
verifies it in two tiers:

- **Tier A - OS-level state**, when the target app has one registered
  (`_APP_PLAYER_STATE_CHECKS`, currently `{"Spotify":
  _spotify_player_state}`) *and* `expected_outcome` plausibly concerns what
  that state covers (`_looks_like_playback_outcome`, a keyword gate against
  "play"/"pause"/"track"/"song"/"music"). `_spotify_player_state()` queries
  Spotify's real `player state` and current track via AppleScript;
  `_spotify_playback_changed(before, after)` compares a before/after pair -
  true if playback started (`player_state` transitioned into `"playing"`)
  or the loaded track itself changed. This reads the actual application
  state directly, with no screenshot or inference involved, and is
  strictly more reliable than any vision-based check when available -
  confirmed directly: a real click on Spotify's play control resolved via
  `paused` -> `playing` with no vision call made at all.
  Before trusting that transition, `_verify_click_outcome` also checks
  `_spotify_state_is_ad(after_player_state)` - Spotify's Free tier can
  insert an ad on a play action, which makes `player_state` transition to
  `"playing"` exactly the same way a real track does (confirmed live: a
  click meant to start "Bohemian Rhapsody" briefly played an ad with
  `track_name` "CHRISTUS Health" and a `spotify:ad:...` URI, which the
  plain playback-changed check alone would have called verified). If the
  state right after a click is an ad, it retries briefly (up to 5x, 1s
  apart) before judging, giving a normal short pre-roll ad room to clear;
  confirmed live, the ad cleared within that window and the real requested
  track was what ended up verified.
- **Tier B - pixel-diff-then-vision-tiebreaker** (same base pattern as
  `_verify_text_entered`), used when no state check is registered, the
  outcome doesn't concern what the registered check covers, or the state
  check itself returns nothing usable. The vision call is anchored on the
  caller's specific `expected_outcome` ("does this image show that this
  particular outcome happened") rather than a generic "did this work" -
  asking a vague question is exactly what produced the measured false
  positive that motivated pixel-diff-first for `type_in_field` in the
  first place.

`click_ui` captures `before_player_state` (via the registered state check,
if any) and `before_region_png` *before* dispatching the click, exactly
like `type_in_field` does for its own before/after diff.

`agents/action.py`'s instruction tells the Action agent to derive
`expected_outcome` from the current milestone's `success_signal` when one
already describes the click's effect - `main.run_action` now sends both
`goal` and `success_signal` to the Action agent (previously only `goal`),
specifically so this concrete, observable description is available to fill
in `expected_outcome` rather than the agent having to invent one from the
goal text alone.

## 7. Reminders path (simpler, no perception needed)

`create_reminder(task, due_date, due_time, list_name="Jarvis Test")` in
`tools/mac_control.py` uses `dateparser.parse()` to turn natural-language
date/time text into a `datetime`, then `_build_applescript()` generates an
AppleScript program that creates the named list if it doesn't exist
(`if not (exists list "X") then make new list ...`) and adds the reminder
inside it via `osascript`. Permission denial (`-1743` / "not authorized") is
detected from `stderr` and surfaced as a specific, actionable error message
rather than a generic failure.

**Milestone plan shape (as of the double-creation fix).** The Planner
produces a *single* milestone for a reminder command - e.g. "A reminder to
call mom tomorrow at 5pm exists in the Reminders app" - with
`requires_approval=false`. It used to split this into "details entered" +
"saved" (sometimes plus "open Reminders"), and the Action agent called
`create_reminder` once per reminder-shaped milestone, so two milestones
meant two identical entries. `create_reminder` is one atomic AppleScript
action with no inspectable in-between state, so there is nothing for a
second milestone to represent. See `planning.md`'s "reminder created twice"
entry. Verified: 3 consecutive real runs, each producing exactly one entry
(queried from Reminders.app, not read off the tool's own success report).

## 8. Browser control path

A parallel system to sections 5-6's native-app UI targeting, for web pages
specifically. Where `click_ui`/`type_in_field` have to infer element
locations from screenshots and the Accessibility API (macOS gives no
structured view into an arbitrary app's UI), the browser bridge gets a
real, structured view of the page for free, at the cost of needing a
Chrome extension and a second WebSocket server. Adapted from a real
reference architecture rather than designed from scratch - see
`planning.md`'s "browser control" entry for the full reasoning, the
deliberately-inherited known flaws, and what was trimmed from the
original.

**Demo target: Kayak, not Google Flights.** `backend/main.py`'s browser
demo command is `"open Kayak and search for a flight to New York"`. Google
Flights' destination field rejects
JS-dispatched input specifically - every event a content script dispatches
carries `event.isTrusted === false` (an unspoofable browser property), and
Google Flights' input handling appears to gate on it. Confirmed via a
direct, controlled comparison: the exact same `executeType` code, no
site-specific branching, worked cleanly on Kayak's comparably
React-controlled destination field and did not on Google Flights'. See
`planning.md`'s browser-bridge entries for the full diagnosis - this is a
site-specific defense, not a flaw in the bridge, the verification logic,
or `executeType`'s fix.

**Verified live, end to end, including a real run of the actual
Orchestrator -> Planner -> Action chain (not just hand-scripted
diagnostics):** the extension loads cleanly in Chrome (Developer Mode,
unpacked), performs a real `browser_bridge_hello` handshake against a
running `browser_bridge_server.py`, and real pages (Wikipedia, Google
Flights, Kayak) produce real snapshots correctly tagged and serialized by
`content_script.js`'s actual DOM-walking code. The reconnect path
(`background.js`'s `scheduleReconnect`) was exercised for real, if
unplanned - see `planning.md`. `find_web_element` correctly resolves a
description once it matches the site's actual copy, and correctly reports
`no_match` rather than a wrong guess when a description doesn't - tested
live on both Google Flights and Kayak, where an initial guess
("destination") both times matched an unrelated decorative element before
the real field's own wording ("Where to?"/"To?") was tried.

**Getting to the site is Jarvis's job, not a manual precondition.** The
Kayak plan's first milestone is "Google Chrome is open with www.kayak.com
loaded" (the Planner is instructed to make the browser-open step the first
milestone of any website task, naming the address). The Action agent maps
that to `navigate_to_url("https://www.kayak.com")` in
`tools/browser_tools.py`, which:
1. runs `open -a "Google Chrome" <url>` - launches Chrome if it isn't
   running, loads the URL;
2. re-sends AppleScript `activate` on a poll loop until
   `_frontmost_app_name()` (imported from `mac_control.py` - a fresh
   `osascript` query each call, never a cached value) confirms Chrome is
   frontmost, the same way `open_app` does;
3. waits (up to 30s, for a cold launch + heavy page + extension reconnect)
   for the browser bridge to register a snapshot whose URL is on the
   target host. This one signal proves the page loaded, the extension is
   connected, and its content script is live on the right page - and that
   snapshot is exactly what `find_web_element` reads next. A
   newer-but-wrong-host snapshot only raises the generation floor;
   `wait_for_snapshot`'s strict `>` means the tool can't pass on a stale
   snapshot it already had.

Verified for real (bridge + real extension + real Chrome, non-voice
integration test): `navigate_to_url("https://www.kayak.com")` returned
`success=True` with the confirming snapshot's real URL and generation;
re-run with Chrome seeded on a different page first, it waited past the
stale snapshot for a genuinely newer kayak.com one rather than passing on
the page already open. See `planning.md`'s "hidden manual precondition"
entry for the full finding and fix.

`content_script.js`'s `executeType` now uses the native
`HTMLInputElement`/`HTMLTextAreaElement` value setter (bypassing any
framework's own overridden setter) before dispatching real `input`/
`change` events - confirmed working cleanly on Kayak: `type_in_web_field`
reported `success: True`, a genuinely newer snapshot generation, and the
field's real value read back as `'New York'`, independently confirmed by
the user as coming from the automated call and not their own typing.

**`type_in_web_field`'s verification now has two paths, checked in this
order** (`tools/browser_tools.py`): (1) if the field's real value in the
*current* snapshot already matches the target text, return success
immediately with no action dispatched at all - added after a real failure
mode surfaced live: retyping an already-correct value doesn't reliably
produce a fresh snapshot (nothing observably changes for the content
script's `MutationObserver` batch threshold to cross), which made an
already-correct field report a false `no_newer_snapshot` failure. (2)
Otherwise, the original path: queue the type action, wait for the
extension's result, then wait for a genuinely newer snapshot generation
and read the field's real value back from it. Confirmed live: an
already-`'New York'`-holding field now returns success in `0.00s` with no
action dispatched, and the disconnected-bridge fallback case was unit-
tested to confirm it still correctly falls through to real dispatch
rather than this change accidentally swallowing that path.

**Plan-approval pause, enforced at the orchestration level.**
`agents/planner.py`'s `Milestone` now carries `requires_approval: bool`,
with instruction guidance to give a task's final, consequential step
(submitting a search, completing a purchase, etc.) its own separate
milestone marked `requires_approval=true`. `main.py`'s
`run_milestones_until_approval` is the actual gate: it runs milestones
through the Action agent in order, but the moment it reaches one with
`requires_approval=true`, it returns that milestone *without ever calling
`run_action` on it* - deliberately placed in the orchestration loop
rather than inside the Action agent or any tool, so the pause doesn't
depend on the same self-reporting agent this project has repeatedly found
unreliable elsewhere. There's no real approval-modal UI yet;
`run_plan_with_approval_gate` (which wraps `run_milestones_until_approval`)
simulates approval by waiting on a real Enter press, then calling
`run_action` on the paused milestone and continuing with any milestones
after it - all in the same ADK session, so the pause-and-resume mechanics
(does context survive the pause) get tested for real, not assumed. Both
the Kayak command (submit the search) and the Reminder command (save the
reminder) hit this gate; in the voice session it holds and resumes the
same way regardless of whether the command was typed or spoken.

**Verified as one continuous, real, unbroken run** (see `planning.md` for
the full walkthrough with real generation numbers): destination field
found and typed via the real dispatch-and-verify path (not the
exact-match short-circuit - the field was genuinely blank going in),
verified via a real newer snapshot generation; the gate then held -
confirmed by checking the real page state at the pause point showed no
search-result URL parameters, i.e. Search genuinely had not fired yet;
simulated approval was given; the Action agent then found and clicked the
real Search button, verified via another real generation increase. One
disclosed limitation carried over from `click_web_element`'s own design
(no universal post-click content check, same as documented in the
click-outcome-verification entry above): the confirming snapshot's URL
didn't show search-result parameters, so "the results page is displayed"
is the Action agent's own summary, not something independently verified
beyond "a real DOM change happened after a real click."

**Processes involved, and how they actually connect:**
- `backend/servers/browser_bridge_server.py` - a `websockets` server on
  `ws://127.0.0.1:8765` (env-overridable via `JARVIS_BROWSER_BRIDGE_HOST`/
  `JARVIS_BROWSER_BRIDGE_PORT`). Its `serve_forever()` coroutine runs as
  an in-process background task (`main.py`'s `_browser_bridge_task`)
  alongside wherever the Action agent's tool calls execute - not a
  separate OS process - because the `asyncio.Event` objects
  `browser/bridge.py` relies on are only safely awaitable from the event
  loop that created them. The task's reference is deliberately kept in a
  module-level variable, not discarded - confirmed directly that
  `asyncio.create_task(...)` without keeping a reference lets the
  garbage collector reap the task mid-run (a well-documented asyncio
  gotcha), which silently killed the bridge server within seconds every
  time before this was fixed. A genuinely separate process is only used
  for the isolated protocol test (a dumb scripted client doesn't need
  shared Python state).
- `chrome_extension/background.js` - an MV3 service worker that owns the
  WebSocket connection *to* that server. Authenticates with a shared
  token (`browser_bridge_hello`), then both pushes snapshots up and
  receives queued actions pushed down over the same open connection.
  Also polls every 5s as a fallback path that runs unconditionally
  alongside push (a deliberately-inherited quirk, not a true fallback -
  see `planning.md`).
- `chrome_extension/content_script.js` - injected into every page
  (`document_idle`, plus on-demand via `chrome.scripting.executeScript`
  if the static injection didn't happen for some reason). Does the actual
  DOM work: tagging, snapshotting, and executing actions.

**Request flow, end to end, for e.g. `type_in_web_field`:**

1. `find_web_element(description)` (`tools/browser_tools.py`) - a
   synchronous, no-round-trip lookup against whatever `PageSnapshot` is
   already stored in `browser/store.py`'s `browser_store`, scoring each
   element's label/placeholder/aria-label/name against the query (exact
   match beats substring match, in that field priority order). Returns a
   `ref_id` like `"jw_12"`.
2. `type_in_web_field(ref_id, text)` calls
   `browser_bridge.queue_action(ActionRequest(action="type", ref_id=...,
   text=...))`. `queue_action` attaches the element's captured fingerprint
   (`dom_path`, `tag`, `role`, labels) as `metadata`, appends the request
   to a pending queue, and immediately tries to push it over the open
   extension WebSocket (`_try_push_actions`) rather than waiting for a
   poll.
3. `background.js` receives the pushed `browser_actions` message,
   resolves the target tab, and relays it to `content_script.js` via
   `chrome.tabs.sendMessage({type: "jarvis_execute_action", ...})`.
4. `content_script.js`'s `findTargetForAction` resolves the actual DOM
   node in three tiers: (1) O(1) lookup by the numeric `agent_id` tagged
   onto the element at snapshot time (`data-agent-id`), (2) the same
   lookup reached by parsing `"jw_" + N"` out of `ref_id` as a safety net,
   (3) only if the tagged element is genuinely gone (e.g. an SPA
   re-render replaced it) - a heuristic re-scan of currently-visible
   elements, scoring each against the metadata from step 2
   (`scoreCandidate`: `dom_path` match worth 300, tag/role match 40 each,
   label similarity 60-120, viewport bonus 15 - and an action-type
   mismatch is a hard `-1` disqualifier, never just a penalty). `executeType`
   focuses and scrolls the resolved element into view first, then either
   `document.execCommand("insertText", ...)` for `contenteditable` targets
   (rich-text editors silently ignore direct value assignment) or a plain
   `.value` assignment + synthetic `input`/`change` events otherwise.
5. The result goes back up: content script -> background script ->
   `browser_action_result` message -> `browser_bridge_server.py` ->
   `browser_bridge.record_action_result()`, which sets (and swaps) the
   per-`action_id` `asyncio.Event` that `type_in_web_field`'s
   `await browser_bridge.wait_for_result(action_id)` is blocked on.
6. Separately, `content_script.js`'s `MutationObserver` (debounced 300ms,
   requires 5+ batched mutations) fires a fresh snapshot on its own once
   the DOM actually settles - no explicit request needed.
7. `type_in_web_field` awaits `browser_bridge.wait_for_snapshot(
   min_generation=<the pre-action snapshot's generation>)`, which only
   returns once a snapshot with a **strictly greater** generation exists
   in the store. `generation` is not a backend-assigned counter - it's the
   browser's own `Date.now()` at snapshot-build time
   (`content_script.js`'s `buildSnapshot`), forwarded untouched all the
   way through. This comparison is the actual mechanism that replaces a
   fixed sleep with "wait until the DOM genuinely changed."
8. Even that isn't the final check: `type_in_web_field` reads the fresh
   snapshot's element back out of the store and confirms its real `value`
   field actually contains the typed text, the same "don't trust the
   action's own success report - read the real state back" principle
   `mac_control.py`'s AX-value check uses, just via DOM state instead of
   the Accessibility API.

`click_web_element` follows the same shape without the final value-check
step (a click has no single universal post-condition to verify against, so
"a newer snapshot arrived at all" is the confirmation).

**The `asyncio.Event` "set-then-swap" pattern**, used for both the shared
snapshot event and every per-action result event
(`browser/bridge.py`): `event.set()` wakes whoever was already waiting;
immediately replacing the attribute with a brand new, unset `Event()`
stops any *future* waiter from returning instantly on a stale "already
set" flag. Without the swap, every wait after the first snapshot would
return immediately regardless of whether a new snapshot had actually
arrived.

## 9. Voice path: capture and transcription

`backend/voice/stt.py` is the speech-to-text layer. `transcribe_audio(
audio_data, *, language="en-US")` is the one function the rest of the
system calls:

- Given a real `speech_recognition.AudioData`, it constructs an
  `sr.Recognizer` and calls `recognizer.recognize_google(audio_data,
  language=...)` - the library's free Google endpoint (generic key baked
  into `speech_recognition`, no key of ours, no billing). `UnknownValueError`
  (unintelligible speech) and `RequestError` (network down, rate-limited,
  key revoked) are both re-raised as `voice.stt.TranscriptionError` so
  callers have one exception type to handle.
- Given a `SimulatedAudio` (a small object wrapping a known transcript
  string), it returns `.transcript.strip()` directly - no network, no
  audio. Used only by unit tests now; not wired into any `main.py` run.

`SAMPLE_WIDTH = 2` (int16) is fixed - it's what the capture layer records
and what `AudioData` expects. `SAMPLE_RATE = 16_000` is only a fallback
default: real capture records at the input device's native rate and
Google's endpoint accepts anything >= 8 kHz, so the rate isn't pinned.
`audio_from_wav(path)` loads a saved clip into an `AudioData` for
re-transcription without re-recording; `save_wav(audio_data, path)` writes
one out via the stdlib `wave` module for inspection.

`backend/voice/capture.py` is the real-audio push-to-talk recorder.
`record_push_to_talk(sample_rate=None, device=None)`:
- **Resolves the input device fresh every call.** `None` becomes a concrete
  index via `sounddevice.query_devices(kind="input")`, falling back to the
  first device with input channels. This is re-done per call because the
  system default input changes at runtime (connecting AirPods makes them
  the default, at a different sample rate). `--device N` overrides;
  `list_input_devices()` (`python main.py --list-devices`) prints the
  choices.
- Records at the device's native sample rate (unless `sample_rate` is
  passed), int16 mono, via a `sounddevice.RawInputStream` whose callback
  stashes each buffer on a `queue.Queue` from PortAudio's thread. A second
  Enter stops it; buffers are joined into `sr.AudioData(raw, sample_rate,
  SAMPLE_WIDTH)`.
- Prints captured byte count, duration, and **peak int16 amplitude**. A
  peak of 0 means the capture was completely silent - permission denied or
  wrong device - and is flagged loudly, so it doesn't later masquerade as
  a bad transcription.

`sounddevice`'s wheel bundles PortAudio, so there's no Homebrew dependency.
Running `python -m voice.capture` records one clip and prints what Google
heard, without the agent chain.

**macOS microphone permission:** the first `RawInputStream` open blocks on
the system TCC gate. The permission must be granted to the process hosting
Python - the terminal app (Terminal / iTerm) when run from a shell, or
"Visual Studio Code" / "Code Helper" when run from the IDE - under System
Settings > Privacy & Security > Microphone. If that process can't surface
the prompt (a headless/helper parent), the stream open hangs silently
rather than erroring; the fix is to run `python main.py` once directly from
a normal terminal and approve the prompt on the first recording.

## 10. The Electron lifecycle: hotkey to UI state, end to end

The real request lifecycle. Everything in sections 2-9 still applies
unchanged - this section is what wraps around it.

**Processes.** Two, on one machine:
- **Electron** (`frontend/`) - a main process owning the window and the
  global hotkey only (wake word no longer lives here - see below); and a
  renderer owning audio capture, the WebSocket, and the React UI.
- **Python** (`backend/servers/agent_server.py` run directly) - starts
  *three* things on one event loop: the agent server on
  `ws://127.0.0.1:8766`, the browser bridge on `ws://127.0.0.1:8765`, and
  (only in this non-Cloud-Run entry point - see `_main()`) the wake-word
  listener (`voice/wakeword.py`), a background thread reading the real
  microphone continuously. Agent server + bridge sharing one loop is a hard
  requirement, not tidiness: `browser/bridge.py`'s `asyncio.Event`s are only
  awaitable from the loop that created them, and the Action agent's browser
  tools await them from inside the agent server's request handling.

**Step by step:**

1. **Trigger - two of them, genuinely different transports now, both
   ending at the renderer's `beginCapture(source)`:**
   - **Hotkey** (`Cmd+Shift+Space` via `globalShortcut`, fires while any app
     is focused, still lives entirely in `electron/main.js`). A **toggle** -
     `globalShortcut` exposes key-down only, so hold-to-talk isn't
     expressible without a native module. Press again to stop. Main sets
     `isRecording` and sends `jarvis:hotkey {action, source: "hotkey"}` to
     the renderer over IPC, same as before.
   - **Wake word** ("Hey Jarvis" via openWakeWord, `backend/voice/
     wakeword.py`) - runs on the **Python backend**, not Electron main.
     Real architectural fork, not a detail: openWakeWord is a Python/
     `onnxruntime` library with no Node binding at all, unlike Porcupine
     (which shipped a prebuilt N-API addon and could load directly into
     Electron's main process - `electron/wakeword.cjs`, kept in the repo
     but no longer wired into `main.js` at all, since Picovoice's signup
     turned out broken/restricted for this account). Detection has to run
     wherever Python already runs, so it lives in `agent_server.py`
     alongside the WebSocket server, and its trigger travels over that
     *same WebSocket connection* instead of Electron IPC: the server
     broadcasts `{"type": "wakeword_detected"}` to every connected client,
     and `App.jsx`'s WS message handler calls `beginCapture("wakeword")`
     directly - the exact same convergence point the hotkey path calls via
     `onHotkey`, just reached a different way. `WakeWordListener.start()`
     loads `Model(wakeword_models=["hey_jarvis_v0.1"], inference_framework=
     "onnx")` and runs a background thread on `sd.RawInputStream` (1280-
     sample/80ms frames, the same callback+queue idiom `voice/capture.py`'s
     push-to-talk path already uses), calling `predict()` per frame
     (~1.4ms) against a 0.5 score threshold. Missing `openwakeword`/
     `sounddevice`/`numpy` (guarded imports, exactly like `mac_control.py`'s
     `pyobjc` guard) -> wake word off, hotkey unaffected - this is also
     what keeps it off on Cloud Run, see below. Wake word only ever
     *starts* a capture - the renderer ends it on ~1.2s of trailing silence
     (RMS gate, 9s hard cap), same as it always did.

   **Mic handoff** is now cross-process (renderer <-> backend, not
   renderer <-> Electron main), so it travels over the WebSocket too: the
   renderer sends `{"type": "mic_state", "active": bool}` at the exact two
   points it already calls `window.jarvis.reportRecordingState(...)` (mic
   acquired in `beginCapture`, released in `stopAndSend`) - regardless of
   which trigger started the capture, since the backend needs to know "is
   ANY renderer capture using the mic right now." `agent_server.py`'s
   `_set_mic_active()` is the one place this actually changes hands:
   `active=true` calls `WakeWordListener.pause()`, which fully closes the
   input stream (not just "ignore audio while still open" - the renderer's
   `getUserMedia` must never contend with this process for the same
   device); `active=false` calls `resume()`, reopening it. On the
   detection side, `_on_wakeword_detected` pauses the listener immediately,
   before even broadcasting - closing the contention window as early as
   possible, mirroring how the old Porcupine `startListening()` called
   `pauseCapture()` before sending its trigger. A 3-second watchdog resumes
   the listener if no `mic_state(active=true)` ack ever arrives (a real gap
   the old same-process IPC design didn't have to guard against - a
   WebSocket message can be lost or the renderer can disconnect mid-
   handoff in a way an in-process function call can't); disconnect cleanup
   covers the same failure mode for a connection that dies outright.
   Verified live: a test client that deliberately never acked showed the
   listener correctly self-resume after 3s in the server's own logs.

2. **Bridge.** `electron/preload.cjs` exposes a small `window.jarvis` -
   `onHotkey`, `speak`, `onSpeakingStatus`, `reportRecordingState`, `log`,
   `minimize`, `closeWindow` - and nothing else: no `ipcRenderer`, no node
   APIs. `onWakeWordStatus` was removed along with the rest of the
   Porcupine IPC wiring - wake-word status now arrives as a WebSocket
   message (`wakeword_status`, sent once per connection) instead. `speak`/
   `onSpeakingStatus` are the new speech-output bridge - see section 9.
   Audio does **not** travel over IPC either way; the renderer holds its
   own WebSocket straight to Python, so captured audio never detours
   through the main process.

3. **Capture** (`src/audio/recorder.js`). `getUserMedia` -> `AudioContext` ->
   an `AudioWorkletNode` running a `pcm-collector` processor loaded from an
   **inline Blob URL** (a file-backed `addModule()` would have to satisfy
   Electron's CSP and `file://` resolution; a Blob sidesteps both). The
   worklet posts each render quantum's `Float32Array` (`.slice(0)` - the
   input buffer is reused by the audio thread, so posting it directly sends
   garbage) to the main thread, which accumulates them. The node is
   connected through a zero-gain node to `destination`, because a worklet
   node is only pulled if it reaches the destination - the zero gain stops
   the mic being echoed out of the speakers.

   On stop: Float32 `[-1,1]` -> little-endian int16 -> base64 (chunked, so a
   several-hundred-KB buffer doesn't blow the call stack on
   `String.fromCharCode`). It also computes **peak amplitude**, so a
   permissions failure reads as an explicit "captured audio was completely
   silent" rather than surfacing later as a mysterious mistranscription.

   **No resampling anywhere.** Capture is at the device's native rate
   (48 kHz here) and that rate travels with the audio; Google accepts
   anything >= 8 kHz. What reaches transcription is bit-identical to what
   the microphone produced.

4. **Transport** (`src/ws/client.js` -> `agent_server.py`). One JSON frame:
   `{type: "audio", sample_rate, sample_width, pcm_base64}`. The server
   raises `websockets`' `max_size` from its 1 MiB default to 64 MiB -
   mono int16 at 48 kHz is ~125 KB/s base64-encoded, so clips past roughly
   8 seconds would otherwise be rejected outright as too large.

5. **Transcription.** The server base64-decodes to raw PCM, wraps it in
   `sr.AudioData(pcm, sample_rate, sample_width)` - the same object
   section 9's Python capture path produces - and calls the same
   `voice.stt.transcribe_audio`, via `asyncio.to_thread` so the blocking
   HTTP call to Google does not stall the event loop the browser bridge
   shares. Emits `{type: "transcript", text}`.

   Note `MediaRecorder` is unusable for this: it emits WebM/Opus, and
   `speech_recognition` reads WAV/AIFF/FLAC PCM only. Raw PCM is not an
   optimization here, it is the only format that works without adding an
   ffmpeg dependency.

6. **Pipeline.** The transcript goes into `main.run_command` - the *same*
   function the CLI calls - and on through Orchestrator -> Planner ->
   Action exactly as sections 2-4 describe. The only addition is an optional
   `on_event` callback (defaulting to `None`, so CLI behavior is unchanged),
   which the server passes to forward each pipeline event to the UI:
   `plan`, `milestone_start`, `tool_call`, `tool_result`,
   `milestone_done` (now carrying `success`), `agent_text`, `reply`.
   `tool_result` carries the **tool's own `success` field**, never the
   agent's summary of it - the same "don't trust the self-report" rule the
   rest of the system runs on. `run_action` returns `(milestone_ok,
   last_agent_text)` per milestone - `milestone_ok` True only if its *last*
   tool reported `success: true`, `last_agent_text` the agent's own final
   reply text if it has one (e.g. a clarifying question asked instead of
   guessing at an ambiguous Spotify result - see planning.md). The plan
   loop aggregates `milestone_ok`, and any failed milestone makes the run's
   outcome `failed` rather than `completed`; `last_agent_text` rides along
   into `failed_goals` (below) so the *reason* a milestone didn't complete
   is visible, not just that it didn't. `App.jsx` previously had no case
   for `agent_text` at all - that text reached the client but was silently
   dropped; it's now logged to the activity feed.

   The pipeline runs as a separate `asyncio.Task`, not awaited inline in the
   message loop. Inline, the handler would stop reading messages while the
   pipeline blocks at an approval gate - and the message it is waiting for
   is the very `approval_response` it could no longer receive. The task
   split is what prevents that deadlock.

7. **The approval gate, for real this time** (`agent_server._run_plan`).
   Same placement as the CLI's `run_milestones_until_approval` - in the loop
   that decides which milestone runs next, never inside the Action agent or
   a tool, so the pause does not depend on the agent policing itself. What
   changed is only what the pause waits on. The server sends
   `{type: "approval_required", milestone: {...}}` and then awaits an
   `asyncio.Future`. `App.jsx` renders Approve/Reject (clickable, or Enter /
   Escape) and sends `{type: "approval_response", approved}`, which resolves
   the Future. On **reject the milestone is never executed at all** and the
   run ends with `{state: "cancelled", goal: <the refused step>}`. A client
   that disconnects mid-gate resolves the Future as a rejection, so the
   pipeline unwinds rather than hanging forever.

8. **Terminal states - four, not two** (`agent_server._run_plan` /
   `_handle_command`). `done` (`reason: completed`) means *every* milestone's
   tools verified. `failed` (`{state: "failed", failed_goals: [{goal,
   message}, ...]}`) means a milestone ran but its tools reported `success:
   false` - it is never silently shown as `done` (that was a real integrity
   bug the regression test caught; see planning.md). `message` is that
   milestone's `last_agent_text` (may be empty) - carrying, for example, the
   actual clarifying question Jarvis asked about an ambiguous Spotify
   result, not just the abstract goal that didn't verify. `cancelled` is a
   rejected approval gate.
   An uncaught exception also lands in `failed` now, not a separate silent
   state. `command_history.success` (section 11) is written from this
   outcome - `success == "completed"` - so a run whose tools failed logs
   `success=False`.

9. **Speech output** - real audio, once a genuine terminal state is
   reached, not mid-process. Right after each of the four terminal-state
   sends above, `agent_server.py` also sends `{"type": "speak", "text":
   ...}` - text already finalized for a listener rather than a reader
   (`_speak_text_for_done_plan`/`_conversational`/`_failed`/`_cancelled`/
   `_error`; see planning.md for the concise-vs-transcript reasoning and
   the personality-flavor rules). This is NOT the same text as
   `agent_text`/`reply`/a failed milestone's `message` - those are written
   for the transcript a person reads, this is written to be heard, and
   deliberately does not read back a full multi-milestone plan.

   `App.jsx` relays `speak` straight to Electron main via
   `window.jarvis.speak(text)` - the renderer holds the WebSocket, but only
   main can spawn a subprocess (sandboxed, no `nodeIntegration`). `main.js`
   spawns a real `say -v Daniel` child process (confirmed non-blocking:
   `spawn()` returns in ~2ms, Node's event loop keeps running through the
   several seconds of actual playback) and reports its real lifecycle back
   via `jarvis:speaking-status` IPC (`speaking: true` the instant the
   process starts, `false` only once it actually closes - never an
   estimated duration). `App.jsx` relays that real status back to the
   backend as `{"type": "tts_state", "speaking": bool}`.

   This closes a real feedback loop: `agent_server.py`'s
   `_sync_wakeword_pause_state()` pauses the wake-word listener whenever
   EITHER a renderer capture holds the mic (`mic_state`) OR the renderer is
   playing speech (`tts_state`) - so Jarvis's own voice, through the
   speakers, can never reach its own wake-word mic and false-trigger or
   confuse detection. Two independent flags OR'd together, not one flag
   reused, so an overlap (speech still finishing right as a new capture
   starts) can't cause either signal to incorrectly resume too early -
   verified live: `tts_state(speaking=true)` then `mic_state(active=false)`
   while speech was still genuinely playing left the listener correctly
   paused, only actually resuming once `tts_state(speaking=false)` arrived.
   Separately, `main.js`'s hotkey path now interrupts any in-progress
   speech (`stopSpeaking()`) before starting a new recording, so Jarvis's
   own voice can't bleed into a hotkey-started capture the way it
   structurally cannot for a wake-word one (whose mic stays paused for as
   long as TTS is speaking).

   Runs only where Electron runs - never Cloud Run, and unlike wake word
   this needed no explicit dependency guard at all: the backend's
   `_speak_text_for_*` functions are pure Python with zero Mac-specific
   dependency (confirmed by calling them inside the real Cloud Run image -
   see planning.md), so there was nothing to guard; the actual OS-level
   side effect (running `say`) lives entirely in Electron, which the Cloud
   Run deployment never includes.

10. **UI state** (`src/App.jsx` + `src/styles.css`). A state machine -
   `idle`, `listening`, `thinking`, `doing`, `approving`, `done`, `failed`,
   `cancelled` - driven entirely by server messages after the audio is
   sent. `failed` paints a red orb and a "Jarvis couldn't complete this"
   card listing the milestones that didn't verify; `cancelled` paints a
   neutral "Cancelled - nothing was done" card naming the refused step.
   Everything before the send (`idle` -> `listening`) is local to the
   renderer. All visual design
   lives in `src/styles.css`, keyed off a `data-state` attribute `App.jsx`
   sets on its root element: the glass container morphs between a compact
   pill (idle/listening/thinking) and a full card (doing/approving/done),
   and per-state CSS rules decide which content blocks are visible, so the
   component body carries no presentation logic. The `.header` row is the
   window's drag region (not the whole glass - an absolutely-positioned
   `no-drag` child doesn't carve itself out on macOS; see planning.md), and
   the window controls live in it as normal-flow `no-drag` children. The
   transparent, fixed-size window
   (`main.js`) is what makes the in-page morphing read as the window itself
   changing shape - nothing outside the glass container ever paints.

**Running it:**
```
# terminal 1 - backend (agent server + browser bridge, one process)
cd backend && python servers/agent_server.py

# terminal 2 - Electron + Vite
cd frontend && npm run dev
```

**Two environment gotchas, both found by running it:**
- `ELECTRON_RUN_AS_NODE=1` in the shell makes the Electron binary run as
  plain Node - no window, and `app` comes back `undefined`, which presents
  as a broken import. `dev:electron` strips it with `env -u`.
- Renderer console output does not reach the terminal by default; a failed
  renderer import silently leaves the previous build running. `main.js`
  mirrors `webContents.on("console-message")` into the terminal.

## 11. Memory (Tier 1): SQLite command history + preferences

`backend/memory/store.py` - plain SQLite at `backend/memory/jarvis_memory.db`
(path resolved absolutely; `JARVIS_MEMORY_DB` overrides it for tests). Two
tables, no embeddings - the ChromaDB "Memory Agent" from the plan doc is a
deliberately separate Tier 2 step (see `planning.md`).

**`command_history(id, timestamp, transcript, plan_summary, success)`** - one
row per real task command that runs to a finish, written by the caller that
owns the whole command, not by `run_command`:
- CLI voice path: `main.run_spoken_command`, in a `try/finally` around
  `run_plan_with_approval_gate`.
- Electron path: `agent_server._handle_command`, in a `finally`, using the
  `"completed"` / `"rejected"` / `"failed"` return from `_run_plan`.
- Typed regression: one `log_command` call after each demo command, passing
  the real `run_plan_with_approval_gate` outcome.

`success` = the run's outcome was `"completed"`, i.e. every milestone's own
tools verified. A run where a tool reported `success: false` (no exception),
or where the user rejected an approval gate, logs `success=False` - the
"soft tool failure logged as success" gap the regression test caught is
fixed (see section 8, planning.md). A conversational input that never
produced a plan is not logged (nothing was executed).

**`preferences(key, value, updated_at)`** - explicit user-stated facts, set by
hand for now: `python -m memory.set_preference "default_flight_destination"
"Austin, Texas"`. There is no "remember this" voice command yet (Tier 2).
`relevant_preferences(text)` is the gate wired into the Planner (section 3): a
preference matches if any 3+char, non-stopword token of its key appears as a
whole word in the command. Keyword match, not NL understanding.

**Where it sits in the lifecycle:** the read happens in `run_command` just
before the Orchestrator call (so both the CLI and Electron paths get it for
free); the write happens once per command, at the very end, after execution
settles. Neither touches how a plan is generated or executed - it's a layer
on top. Persistence is just the file: kill the process, restart, the rows
and preferences are still there (verified with a real process restart).
