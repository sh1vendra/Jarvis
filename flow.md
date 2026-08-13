# System Flow

Technical walkthrough of how a command actually moves through Jarvis, end to
end, referencing real files/functions. Updated whenever the request
lifecycle or a component's behavior changes.

## 1. Entry point

`backend/main.py` is the only entry point right now (no voice/UI layer yet).
`load_dotenv()` pulls `GOOGLE_API_KEY` from the repo-root `.env` before any
ADK/Gemini call is made. `logging.basicConfig(level=logging.INFO, ...)` is
set up here so that `agents/verifier_callbacks.py`'s logger and
`tools/mac_control.py`'s zoom-search logger both actually print.

Two ADK runner types are used, each wrapping one agent:
- `InMemoryRunner(agent=orchestrator_agent, ...)` - drives a text command
  through the Orchestrator (and, via transfer, the Planner).
- `InMemoryRunner(agent=action_agent, ...)` - drives one milestone goal at a
  time through the Action agent.

Each logical run gets its own session (`session_service.create_session`), so
`main()`'s four test cases in the file don't share conversation state with
each other.

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
  `success_signal` (an observable signal that the outcome was reached).
- `MilestonePlan`: `milestones: list[Milestone]`.

`planner_agent` is a leaf agent (no `sub_agents`, no tools) with
`output_schema=MilestonePlan` - this forces Gemini's final reply to be JSON
that validates against `MilestonePlan`, so `main.run_command()` can do
`MilestonePlan.model_validate_json(final_text)` directly instead of parsing
free text. The instruction explicitly contrasts an outcome-shaped milestone
("Spotify is open and in the foreground") against a forbidden
action-shaped one ("click the Spotify icon at position X,Y") to keep the
model from collapsing into step-by-step output.

## 4. Action agent's tool selection and execution loop

`agents/action.py` defines `action_agent`, given four tools -
`open_app_tool`, `click_ui_tool`, `type_in_field_tool`,
`create_reminder_tool` - all `FunctionTool`-wrapped functions from
`tools/mac_control.py`. It receives one milestone goal per turn (driven by
`main.run_action()`, which loops over `plan.milestones` and sends each
`milestone.goal` as a new message in the *same* session). Because milestones
for one task share a session, the agent can infer context across calls -
e.g. if it already called `open_app('Spotify')` for milestone 1, it infers
milestone 2 ("lo-fi is playing") is still about Spotify without being told
again.

The instruction maps milestone shape to tool choice: "app open/foreground"
-> `open_app`; "something clicked" -> `click_ui` (requires
`expected_app_name`); "text entered" -> `type_in_field` (same requirement);
"reminder exists" -> `create_reminder` (extracts `task`/`due_date`/
`due_time` as plain text, no manual date computation by the model). If no
tool fits, or a tool refuses to act, the agent is told to report that
plainly rather than retry blindly.

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
   threshold. `_locate_via_vision_zoom(target_description)` (also in
   `mac_control.py`) is an iterative crop-and-zoom search: it takes a full
   screenshot (`perception.capture_screenshot`), asks Gemini vision
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

   **Known limitation, still unresolved: clicking Spotify's first search
   result.** Unlike the collapsed search icon, there's no keyboard-shortcut
   escape hatch for "select and play the first result." Both location
   strategies were retested specifically for this target, against a real,
   independently-verified ground truth (a saved screenshot where clicking
   a known point was confirmed via `_spotify_player_state()` to start real
   playback): `_locate_via_vision_zoom` scored 0/10 within any usable
   tolerance (errors 158-661pt, worse than the single-shot whole-screen
   guess it's supposed to improve on), and a full unfiltered AX dump of the
   search-results window (depth 25, no role filtering) found every node
   unlabeled with empty leaves - genuinely nothing to read, not a filtering
   artifact. See `planning.md`'s entry for the full numbers. `click_ui` is
   not special-cased for this target - it still tries vision (Tier 2) and
   will very likely mis-click, but its outcome verification (below) is what
   keeps this honest: a live end-to-end run correctly reports
   `click_outcome_not_verified` rather than a false success.

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
