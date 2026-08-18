# Planning Log

Record of the real decisions made building Jarvis, and why. Updated as part of
finishing each build step, not written after the fact from memory. Each entry
covers: what was decided, why over the alternatives considered, what
constraints shaped it, and what was explicitly not done.

---

## Multi-agent structure over a single monolithic agent

**Decision:** Split the system into three separate ADK `LlmAgent`s -
`orchestrator_agent`, `planner_agent`, `action_agent` - instead of one agent
handling classification, planning, and execution together.

**Why:** Each stage needs a different output shape and a different scope of
responsibility. The orchestrator only needs to decide conversational-vs-task
and hand off. The planner needs to produce a *structured*, validated
`MilestonePlan` (via `output_schema=MilestonePlan`) and nothing else - no
tools, no execution. The action agent needs tools and freedom to make
per-milestone judgment calls, but should never be responsible for deciding
the overall task breakdown. Collapsing these into one agent would mean one
prompt juggling three different jobs and no clean way to force structured
output at just the planning step while still allowing free tool use at the
execution step.

**Constraints:** ADK's `sub_agents` mechanism made this close to free - any
`LlmAgent` with `sub_agents` automatically gets a `transfer_to_agent` tool, so
orchestrator -> planner handoff needed no manual routing code. This tipped
the decision further toward separate agents, since the coordination cost
that would normally argue for a monolith was mostly absorbed by the
framework.

**What we didn't do:** No manual if/else routing logic for
conversational-vs-task classification - that decision is left entirely to
the orchestrator's own model call, per `agents/orchestrator.py`. No planning
logic embedded in the action agent - it only ever receives one milestone
goal at a time and infers context (like which app it's operating on) from
the conversation history of prior milestones in the same session, per the
instruction in `agents/action.py`.

*(Commit: `702d0a1` wire up orchestrator and planner agents through adk)*

---

## Outcome-based milestones, not fixed action sequences

**Decision:** `Milestone` (in `agents/planner.py`) captures a `goal` and a
`success_signal`, never a literal action like "click the Spotify icon at
X,Y".

**Why:** A plan that says "Spotify is open and in the foreground" can be
satisfied by whatever the Action agent decides is the right tool call at
execution time, and can be *verified* independently of how it was achieved.
A plan that hardcodes "click at (240, 72)" is both fragile (coordinates
change) and unverifiable in any meaningful sense (the click succeeding
doesn't mean the goal was reached). This shape is what let verification
become a real, separate concern later (see the pixel-diff decision below)
instead of being conflated with "did the action execute."

**What we didn't do:** No coordinates, tool names, or low-level steps in the
Planner's output schema at all - `agents/planner.py`'s instruction explicitly
tells the model milestones must describe outcomes, with the Spotify example
spelled out directly in the prompt to anchor the model against slipping into
step-by-step output.

*(Commit: `702d0a1` wire up orchestrator and planner agents through adk)*

---

## Accessibility-API-first, vision-fallback for UI targeting

**Decision:** `_locate_element` in `tools/mac_control.py` always tries the
macOS Accessibility API first (`_best_ax_match`, backed by
`perception.get_ui_tree`), and only falls back to a Gemini vision screenshot
call if nothing clears the fuzzy-match threshold (`_FUZZY_MATCH_THRESHOLD =
0.45`).

**Why:** AX queries are near-instant, exact when they work, and cost no LLM
call. But Electron/Chromium apps - Spotify specifically - expose little to
no real accessibility tree, so a vision-only design would pay vision's cost
and imprecision on every single call, and an AX-only design would simply
fail outright on exactly the apps most likely to be used in a demo. Trying
AX first and only reaching for vision when AX comes up empty gets the speed
and precision of AX wherever it's available, without giving up on apps that
don't support it.

**What we didn't do:** No attempt to enumerate or special-case which apps
"support AX" ahead of time - the fallback is driven purely by whether the
AX query actually returns a usable match at call time, so the same code path
works for both AX-rich and AX-poor apps without an app allowlist.

*(Commit: `5d3abf8` add click_ui and type_in_field with accessibility and
vision tiers)*

---

## Fresh `osascript` frontmost-app queries, not cached AppKit/NSWorkspace

**Decision:** `_frontmost_app_name()` in `tools/mac_control.py` queries the
frontmost app via a brand-new `osascript`/System Events subprocess call
every time, rather than AppKit's in-process
`NSWorkspace.sharedWorkspace().frontmostApplication()`.

**Why:** Directly measured root cause of a real, reproducible bug (Bug 1):
in a script issuing many subprocess calls back-to-back - exactly what
`open_app`'s poll loop and a live ADK agent run both do - the in-process
NSWorkspace value goes stale and never updates again for the rest of the
process's life. This was misdiagnosed at first as "the other app fighting
for focus" (with a hide/quit escalation attempted as a fix), but
instrumentation showed the real frontmost app had already changed while
NSWorkspace kept reporting the old one. A fresh subprocess query has no such
cache, so it can't go stale the same way.

**Constraint:** The fix had to actually resolve the exact reproduced
failure - `create_reminder` immediately followed by `open_app('Spotify')`,
4/4 times - not just widen the polling timeout, which had already been tried
and failed at 8 seconds before the real cause was found.

**What we didn't do:** Removed the hide/quit escalation machinery entirely
once the real cause was identified, rather than keeping it around as a
belt-and-suspenders fallback - it was solving a problem that didn't
actually exist.

*(Commit: `3fc9af8` fix stale frontmost app detection causing false
focus-steal failures)*

---

## Guard against acting on the wrong frontmost app

**Decision:** `click_ui` and `type_in_field` both call
`_verify_expected_app_frontmost(expected_app_name)` before doing anything
else, and refuse to act if the actual frontmost app doesn't match.

**Why:** Without this, a stray focus change (the user clicking elsewhere, a
screenshot tool briefly stealing focus) turns "type into Spotify's search
box" into "type into whatever's actually in front." This isn't
hypothetical - it happened for real during testing: a `type_in_field` call
landed a test string in this project's own `.gitignore` because focus had
silently shifted to the editor. The fix was disclosed immediately and the
guard was added afterward specifically to make that class of mistake
impossible rather than just less likely.

**What we didn't do:** Considered (via direct question to the user) softer
alternatives to a hard guard; the harder option - refuse to act at all
rather than warn-and-proceed - was chosen deliberately over anything that
still risks acting on the wrong target.

*(Commit: `5d3abf8` add click_ui and type_in_field with accessibility and
vision tiers)*

---

## Pixel-diff as the primary verification gate, vision only as a tie-breaker

**Decision:** `_verify_text_entered` in `tools/mac_control.py` verifies
typed text landed using, in order: (1) a fresh AX query for actual field
*values* via `perception.get_field_values`, exact when available; (2) if AX
exposes no fields at all (Spotify's case), a before/after pixel diff of the
click region via `_region_pixel_diff_score`, and only if that diff clears
`_NO_CHANGE_DIFF_THRESHOLD` does it ask vision - on the small cropped region
only, not the whole screen - to confirm the *content* of the change.

**Why:** A single vision yes/no call ("is the expected text visible?") was
measured directly to be unreliable enough to gate a success/failure
decision on: it produced a false positive 1 time out of 5 identical calls
against a completely unchanged screen. Pixel diff is deterministic and
can't hallucinate - "nothing visibly changed near the click point" is a
hard, trustworthy signal that the click missed, with no model call needed.
Vision is only asked a much narrower, less ambiguous question (does this
small cropped region show this text) and only once pixel diff has already
confirmed something worth asking about.

**Constraint:** This exists because of a real, disclosed failure -
`type_in_field` had previously reported success on Spotify when the typed
text had actually landed nowhere, because the only check at the time was
"did the paste command exit without error."

**What we didn't do:** Didn't try to make the single vision call more
reliable through prompt tuning alone (e.g. asking it more carefully) - the
decision was that no single vision call should be trusted as the sole gate
for a success claim, regardless of prompt quality, given the measured false
positive rate.

*(Commit: `ac5cd0f` verify typed text lands before reporting
type_in_field success)*

---

## Dedicated "Jarvis Test" Reminders list instead of the user's real list

**Decision:** `create_reminder` takes an optional `list_name` parameter
(default `"Jarvis Test"`), and `_build_applescript` creates that list via
AppleScript if it doesn't already exist, rather than always targeting the
user's default Reminders list.

**Why:** Bug-reproduction test runs (chasing Bug 1) had polluted the user's
real "To do" list with ~24 junk items - duplicate "Call mom" reminders,
"bug1 test/retest/final 1-4", "hide/quit debug test", etc. This is
straightforward test/production data separation, requested directly after
the pollution was noticed.

**What we didn't do:** Didn't make list separation the only default going
forward without an override - `list_name` stays a parameter (defaulting to
`"Jarvis Test"` "for now during development," per the request), not a
hardcoded constant, so it can be pointed at a real list later without a code
change.

*(Commit: `1a1fbbf` route created reminders to a dedicated jarvis test
list)*

---

## Iterative crop-and-zoom vision search, then abandoned in favor of a keyboard-shortcut fallback for Spotify's search icon specifically

**Decision:** Built `_locate_via_vision_zoom` (`tools/mac_control.py`) to
replace a single whole-screen vision coordinate guess with up to two
progressive crop-and-reask iterations, on the theory that a small element
occupies a much larger fraction of a cropped image than the full screen and
should therefore be easier for vision to place precisely. After building
and testing it live against Spotify's collapsed search icon, it did not
reliably fix the problem - so `type_in_field` now uses a keyboard shortcut
(Cmd+L, `_APP_SEARCH_SHORTCUTS`) to open Spotify's search UI directly for
that one case, bypassing vision-based coordinate finding entirely.

**Why the zoom approach first:** The prior single-shot guess was measured
to land ~100pt off the real search icon. Zooming in on a crop is a
reasonable, cheap thing to try before assuming vision simply can't do this -
cropping doesn't require any new capability, just re-asking on a smaller
image, capped at 2 zoom levels (3 vision calls total) to bound latency.

**Why it was abandoned:** Testing against a saved screenshot with a known,
visually-confirmed ground-truth icon location showed the model wasn't
*noisy* around the right answer - it was *consistently, confidently wrong*,
landing on the same incorrect spot 7 times out of 7 across two different
target descriptions (the generic `"search field"` and a much more specific
description naming the magnifying-glass icon explicitly). Zooming in only
helps when a miss is noise that averages out; it does nothing for a
model that's confidently looking at the wrong element in the first place.
Live end-to-end testing of the zoom search against the real failing sequence
(open Spotify, type "lo-fi") also scored 0/3.

**Constraint:** This was a demo-reliability decision, agreed in advance: if
click precision on this specific target stayed unreliable (worse than 1-in-4
consistently, and here it was 0-in-3 plus 0-in-7 in isolated testing), fall
back to a keyboard shortcut rather than keep iterating on vision precision
for one small icon.

**What we didn't do:** Didn't build a general-purpose "any app can register
a shortcut for any field" abstraction - `_APP_SEARCH_SHORTCUTS` is a small,
explicit dict mapping specific apps to specific shortcuts, added only for
the one case that actually needed it. Didn't remove the zoom search
either - it's still the vision fallback for every other target in every
other app, since it strictly subsumes a single-shot guess (same first call,
optional refinement after) and the failure here was specific to this one
small, ambiguous icon, not evidence the zoom approach is wrong in general.

**Three more real bugs surfaced getting the fallback itself to work
reliably, each found by testing live rather than assumed:**

1. The first shortcut guess (Cmd+K) was wrong and silently did nothing -
   caught by reading Spotify's actual Edit menu via
   `AXMenuItemCmdChar`/`AXMenuItemCmdModifiers` instead of continuing to
   guess; the real shortcut is Cmd+L.
2. Re-running the vision zoom search to locate the *now-open* field (for
   building the verification region only, not for clicking) was just as
   unreliable as locating the original icon - measured guessing everywhere
   from mid-window to the window's bottom edge. Fixed by anchoring on the
   app's own AX-reported window frame (`get_frontmost_window_frame`) plus a
   fixed, once-confirmed offset (`_APP_SEARCH_FIELD_OFFSET`) instead of
   asking vision a second time.
3. `perception._pid_for_app` used
   `NSWorkspace.sharedWorkspace().runningApplications()`, which turned out
   to have the exact same staleness failure mode as the
   `_frontmost_app_name` bug from the Bug 1 fix above, just in a different
   NSWorkspace API: after quitting and relaunching an app a few times in
   one long-running process, it kept returning the *previous* instance's
   already-dead PID. Fixed the same way - a fresh `osascript`/System Events
   query every call, no in-process cache to go stale.

**Constraint on all three:** each was found and fixed only because the
result was checked against reality (Spotify's real menu, a real screenshot
after the shortcut fired, a real PID comparison against `pgrep`) rather than
trusted once the code ran without raising an exception - consistent with
the standard set for every fix in this project so far.

*(Commits: `39e0964`, `699b21a`, `f6cb7b5`, `d882fd8`, `7ff149f` - the zoom
search; `3a74a45`, `be12863`, `3ce5bdd`, `f9b2120`, `c60e50f`, `e3b85b4` -
the keyboard-shortcut fallback and its three bug fixes)*

---

## `click_ui` gains outcome verification: OS-level state first, pixel-diff-plus-vision fallback second

**What we found (recap):** Running the full three-milestone Spotify command
through the real agent chain (`open_app` -> `type_in_field` -> `click_ui`),
`click_ui` reported `success: True` for clicking "first search result play
button" - but a screenshot taken immediately afterward showed the previous
track was still the one playing; nothing new had started. `click_ui`
dispatched a click at a vision-guessed coordinate and reported success as
soon as the click was sent, with no equivalent to `type_in_field`'s
`_verify_text_entered` step. This entry covers the fix.

**Decision:** `click_ui` now takes a required `expected_outcome` argument
(the specific, observable effect the click should cause) and verifies it
via `_verify_click_outcome`, in two tiers: (1) an app's real OS-level state,
when one is registered (`_APP_PLAYER_STATE_CHECKS`, currently Spotify's
player state via AppleScript) *and* `expected_outcome` plausibly concerns
that state (`_looks_like_playback_outcome`); (2) otherwise, the same
pixel-diff-then-vision-tiebreaker pattern `_verify_text_entered` already
uses, except the vision question is anchored on the caller's specific
`expected_outcome` rather than a generic "did this work."

**Why click verification needed a different strategy than type verification:**
Typed text has one universal success signal - did the expected text appear
in the field, checkable by diffing/reading the same location before and
after. A click's success signal depends entirely on what the click was
*for* - playing audio changes an OS-level playback state (and maybe an
icon, but not reliably at pixel level - Spotify's play/pause glyph is a
subtle vector-icon swap, not a big visual change on its own); selecting a
search result might navigate to a whole new page; toggling a checkbox
changes one small visual region. There's no single before/after check that
covers all of these, so unlike `_verify_text_entered`, `_verify_click_outcome`
can't assume it knows what "changed" should look like - it has to be told
(`expected_outcome`), and use whatever signal is actually strongest for
that specific outcome.

**Why OS-level state is preferred over vision when available:** A real
state query reads the actual thing that matters (is Spotify actually
playing, and what track) directly from the source of truth, with no
inference step in between - it can't be confused by an unrelated visual
change nearby, can't hallucinate, and doesn't depend on screen layout at
all. Pixel-diff-plus-vision is a good fallback when no such state exists to
query, but it's still inference from pixels; OS state is ground truth.
Measured directly: a real click on Spotify's play/pause control was
confirmed via player state transitioning `paused` -> `playing` with *no
vision call made at all* - the state check resolved it on its own.

**Why the OS-state check is gated by `_looks_like_playback_outcome`, not
just "is this app Spotify":** Spotify's player state has nothing useful to
say about a click whose real goal is unrelated to playback (e.g. selecting
a filter tab) - trusting it unconditionally for every Spotify click would
turn "no playback change" into a false failure for clicks that were never
about playback in the first place. Gating on whether `expected_outcome`
itself plausibly concerns playback keeps the OS-state check from being
applied to outcomes it has no authority to judge, falling through to
pixel-diff/vision instead for those.

**Constraint:** Verified both directions live, not just the happy path -
a genuine positive (a real click on Spotify's play control, confirmed via
`before`/`after` player-state snapshots showing `paused` -> `playing`) and
a genuine negative (a real click on an unrelated element - a playlist tile
- while claiming a playback outcome, confirmed the tool correctly reported
`success: False` because player state genuinely didn't change, not because
the click missed its own real target).

**What we didn't do:** Didn't build a generic "any outcome, any app" state
registry - `_APP_PLAYER_STATE_CHECKS` only has Spotify's player state for
now, added because it's the concrete case that motivated this work. Didn't
try to make the OS-state check universal by inferring intent automatically
across arbitrary apps - `_looks_like_playback_outcome`'s keyword gate is a
narrow, explicit check, not a general outcome-classifier.

---

## Rejected: neither crop-and-zoom vision nor Accessibility API locate Spotify's search results reliably - left as a known, unfixed limitation

**What we tried:** With `click_ui`'s outcome verification now trustworthy,
the remaining gap was locating "first search result" precisely enough to
click it at all. Two things were tested, both against a real, directly
confirmed ground truth (a saved screenshot where clicking point (448, 218)
was independently verified to start real playback - `Billie Jean` -> a
genuinely new track, `paused` -> `playing`, checked via
`_spotify_player_state()` before assuming anything about the image):

1. **`_locate_via_vision_zoom` (the same crop-and-zoom search already
   working for the collapsed search icon).** The hypothesis going in was
   reasonable: search results are large, visually distinct elements (album
   art, title, a literal green play button), not a tiny ambiguous icon, so
   zooming might behave completely differently here. It didn't. Single-shot
   whole-screen guesses were tight and *consistent* (5/5 landed within 13px
   of each other) but confidently wrong - ~360pt from the real target,
   in the left sidebar, not the results area. Running the full zoom search
   (5 attempts with `"first search result"`, 5 more with a much more
   specific description naming the exact playlist and its green play
   button) made things *worse*, not better: errors ranged from 158pt to
   661pt, and unlike the icon case's single consistent wrong answer, these
   were scattered across multiple different wrong locations. Zooming in
   apparently amplifies noise here rather than converging - 10/10 zoom
   attempts, 0 within any usable click tolerance.
2. **The Accessibility API, checked with fresh eyes rather than assumed
   from the earlier icon investigation.** A full unfiltered dump of
   Spotify's AX tree (no role filtering, depth 25) for this exact search
   results window found ~15 total nodes, every single one unlabeled
   (`AXDescription`/`AXTitle` empty or `None`), bottoming out in completely
   empty leaf `AXGroup`s. This isn't `get_ui_tree`'s interesting-roles
   filter hiding something real - the raw tree genuinely has nothing to
   read. Confirms and extends the original finding (previously only
   checked for the collapsed icon, not this view): Spotify's
   Chromium/Electron renderer doesn't expose its content to the
   Accessibility API at all in this window, regardless of what part of the
   UI is being asked about.

**Decision: left as a known, disclosed limitation for the demo, not
patched over.** Per the standard set for this whole investigation (if an
approach doesn't demonstrate real, consistent accuracy, don't wire it in
and don't keep tuning it), neither path is trustworthy enough to hand a
real click through. The keyboard-shortcut fallback that worked for the
collapsed search icon (Cmd+L) doesn't have an equivalent here - there's no
known "select and play the first result" keyboard shortcut in Spotify to
substitute in the same way.

**Why this is worth keeping as a documented limitation rather than forcing
something in:** A forced fix that isn't actually reliable would reintroduce
exactly the failure mode `click_ui`'s outcome verification was just built
to catch and report honestly - and it now does exactly that. Live-testing
the real end-to-end command still ends with `click_ui` correctly reporting
`click_outcome_not_verified`, backed by real Spotify player state showing
no playback change, rather than a false success. The system's honesty
about this specific limitation is itself the deliverable of this step, not
a consolation prize.

**What would actually resolve this, if pursued later:** Full-page
screenshot vision (whole-screen, not cropped) was measurably more *precise
per-guess* here than zoomed crops, even though still not accurate enough
to use directly - suggesting the model does have some real signal about
roughly where results are, just not enough to trust for a single click.
A different strategy entirely (e.g. a much larger click-tolerance
region-click-and-verify loop, or scanning a grid of candidate points and
using `click_ui`'s own outcome verification to detect a hit) wasn't tried
here and might be worth a dedicated follow-up if this limitation turns out
to matter for the actual demo.

---

## Tried the grid-click-and-verify follow-up from the entry above - also rejected, 1/3 hit rate

**What we tried:** `locate_and_click_via_grid_search` (`tools/mac_control.py`)
- the exact follow-up idea from the previous entry. Rationale: instead of
needing vision's single coordinate guess to be *precise*, only need it to
be *roughly in the neighborhood*, then use several candidate points plus
the outcome verification already proven reliable (real Spotify player
state / pixel-diff-plus-vision) to find whichever one, if any, actually
worked. A 5-point cross pattern (center + up/down/left/right, 120pt
spacing) around vision's single whole-screen guess, stopping at the first
verified hit, capped at 5 attempts, with a wide-region diff check after
each miss to detect and stop on an accidental navigation rather than
clicking blindly in a changed context.

**A real structural finding before precision even mattered:** building a
fresh ground truth for the *actual* demo query surfaced something the
earlier "lofi hip hop radio" ground truth had accidentally sidestepped:
the literal query the Action agent actually types (`"lo-fi"`, hyphenated,
matching `type_in_field`'s real call) returns Spotify's genre-landing
layout (a "lofi beats" genre tile + a "Jump in" row of playlist tiles),
not the clean "Top result" card with a persistent, always-visible play
button that a different query happened to return. Confirmed directly:
clicking a playlist tile in this layout navigates to the playlist's own
page - it does not start playback. So even a hypothetically perfect click
locator would not satisfy "lo-fi music is playing" in one click for this
exact query; getting real playback would need a second click (the
playlist page's own prominent Play button) after the first. This is
independent of click precision and worth knowing regardless of how the
grid search performed.

**Two real bugs found and fixed while building the test for this** (both
committed separately, `cbcd87a` and `9c3c752`):
1. `_looks_like_playback_outcome`'s keyword gate used plain substring
   matching, so an outcome like "the playlist page opens" (pure
   navigation, nothing to do with playback) matched `"play"` inside
   `"playlist"` and got wrongly routed to Spotify's player-state check -
   which then reported a false "no playback change" for something player
   state was never able to speak to in the first place. Fixed to match on
   word boundaries only.
2. `_spotify_player_state()` had no handling for `subprocess.TimeoutExpired`
   - under the grid search's rapid repeated queries, `osascript`
   occasionally hung past its 10s timeout, and the uncaught exception
   crashed the entire grid search instead of degrading to "state unknown"
   the way every other failure mode in that function already did.

**Real test result (isolated, 3 trials, same methodology as every other
approach in this project):** 1/3. Trial 1 succeeded on its 3rd candidate.
Trials 2 and 3 both exhausted all 5 candidates without a single verified
hit. The failure pattern explains why: vision's single rough guess for
those two trials landed near the screen's top-left corner, and the cross
pattern's fixed 120pt spacing pushed two of the five candidates to
*negative* coordinates - off-screen, guaranteed no-ops (`diff score 0.00`)
that wasted 2 of the 5-attempt budget on nothing. The grid can only rescue
a guess that's off by roughly one target-width in some direction; it can't
rescue a guess that's off by hundreds of points or landed near a screen
edge, which is exactly what was measured for this target across every
approach so far (crop-zoom: 158-661pt error; single-shot: ~360pt error).

**Decision: also rejected, not wired into `click_ui`.** 1/3 is not a
reliable hit rate by any reasonable bar, and per the standard held
throughout this whole investigation, a clear negative result is a stopping
point, not a tuning opportunity. Widening the grid or increasing the
attempt cap were both considered and deliberately not tried further here -
the previous entry's crop-zoom numbers already show that even a generous
error budget doesn't reliably bring vision's guess back to the true
target for this specific element, so there's no strong reason to expect a
wider grid to convert this from "sometimes" to "reliably."

**Recommendation:** swap the demo command rather than keep chasing this
target, per the fallback already agreed for this situation. Two
independent, real problems now stack against "open Spotify and play some
lo-fi music" specifically: click precision on the search results
(unresolved despite three different approaches), and the query itself
requiring two clicks to actually reach playback, not one. A command that
avoids needing to click a small/ambiguous Electron-rendered result -
e.g. one that stays within already-solid paths (`open_app`,
`type_in_field`'s keyboard-shortcut route, `create_reminder`) - would
demo reliably today; this one specific milestone would not.

---

## Demo command swapped to a specific track query - and a second, more revealing negative result on the retest

**Decision:** the demo command changed from `"open Spotify and play some
lo-fi music"` to `"open Spotify and play Billie Jean by Michael Jackson"`
(`backend/main.py`).

**Why this specific change, not just a different genre term:** a genre or
mood query (`"lo-fi"`) reliably lands Spotify on a genre-landing page,
where the top items are playlist/genre tiles that *navigate* on click,
not play - a structural mismatch with "play some lo-fi music" regardless
of click precision (see the entry above). A specific track/artist query
reliably surfaces a different layout entirely: a "Top result" card with a
persistent, always-visible green play button that starts playback
directly on a single click - confirmed twice now, independently, on two
different queries (`"lofi hip hop radio"` earlier, and this session's
`"Billie Jean Michael Jackson"`), both resolving to the *same* point-space
coordinates, (448, 218) - strong evidence this card's position is a fixed
layout element, not query-dependent placement.

**Retest: does the grid search do better against this cleaner, more
consistent target?** Directly measured, no: **0/5**, worse than the 1/3
scored against the genre-page target. The reason is more revealing than
"still imprecise," though. A fresh single-shot whole-screen guess against
the new ground truth (`"Billie Jean Michael Jackson"` results,
independently confirmed by clicking (448, 218) and watching
`_spotify_player_state()` transition `paused`/`Spanish Castle` ->
`playing`/`Billie Jean`) landed at pixel (190-194, 182-196) across 5
tries - tightly clustered (<15px spread, so not noisy) but ~372pt from the
true target. That's the *same* region (point-space roughly (95,95),
near the left sidebar) that the *previous* target's single-shot guess
also landed in, on a completely different query and a visually different
results layout. The live grid search reproduced this: 4 of 5 trials
guessed within a few pixels of (131, 98) point-space, again in that same
sidebar-adjacent region, regardless of the actual on-screen content. This
looks like a systematic bias in how the model answers "find the first
search result" - not per-image imprecision that a wider search radius
could fix. A grid of any practical size centered on a guess that's
anchored to the wrong part of the screen *by default* won't reach a
target 370+pt away. (Also newly observed, though secondary to the above:
2 of each trial's 5 candidates landed at negative, off-screen coordinates
- guaranteed no-ops that wasted 40% of the attempt budget every trial -
not worth fixing given the deeper problem makes it moot.)

**Decision: not wired into `click_ui`, same as the first grid-search
attempt.** A cleaner, more consistent target was the stated hypothesis for
why this retest might go differently, and it didn't - if anything the
result got worse (0/5 vs 1/3). This rules out "the earlier target was
just unusually hard" as an explanation and points at something more
fundamental about how vision answers this specific kind of question.

**Open question for a pragmatic demo-only fix:** since this Top-result
card's play button position is now confirmed fixed at point (448, 218)
across two independent queries, a hardcoded coordinate (or an offset
computed from `get_frontmost_window_frame`, the same pattern already used
for Spotify's search field after Cmd+L) could reliably hit it *for this
one specific card layout* - not a general solution to search-result
click precision, but a targeted, explicitly-documented simplification for
this one demo command. Not implemented yet - flagged for a decision before
building it, since hardcoding a coordinate is exactly the kind of
technicality this whole project has been careful not to lean on silently.

**User decision: build it.** Asked directly rather than assumed, given
hardcoding a coordinate is exactly the kind of technicality this whole
project has been careful about - confirmed go-ahead to implement, clearly
documented as a known simplification.

---

## Fixed window-offset for Spotify's Top result card - implemented, and four more real bugs found getting the full chain to actually work

**Decision:** `click_ui` now special-cases Spotify's Top result card via
`_APP_TOP_RESULT_OFFSET` (`{"Spotify": (0.56, 179.0)}`, a window-frame
offset in the same shape as `_APP_SEARCH_FIELD_OFFSET`) and
`_looks_like_top_result(target_description)`. When both the app matches
and the description plausibly refers to a top/first result, the location
is resolved via `_locate_via_window_offset` (a new shared helper,
factored out of the retry-loop pattern `type_in_field` already had inline
for its own window-frame lookup) instead of vision - completely
sidestepping the measured, systematic guess bias. Explicitly labeled
`tier: "fixed_offset"` in every result so it's never confused with a real
location strategy in logs or reports.

**Verified in isolation first: 3/3**, each a genuine `paused` ->
`playing` transition confirmed via `_spotify_player_state()`, not just
"a click was dispatched."

**Four more real bugs surfaced getting the *full agent chain* (not just
direct isolated calls) to actually reach and use this reliably - each
found by testing live, not assumed:**

1. **Search-phrasing gap in the Action agent's own instruction.** Every
   live run immediately after the query swap had the agent try `click_ui`
   on a "search button/tab" first - which doesn't exist as a distinct
   clickable element the way the agent imagined - instead of calling
   `type_in_field` directly. Root cause: milestone text phrased as "X is
   searched" or "X is located" pattern-matched the agent's own
   `click_ui` instruction example ("the first search result is
   selected") before it matched `type_in_field`'s. Fixed by clarifying
   `agents/action.py`'s instruction that "X is searched" means *type X
   into the search field*, not click a search icon.
2. **`_looks_like_top_result`'s exact-phrase matching was too brittle.**
   Once the agent correctly called `type_in_field` first, it phrased the
   *next* milestone's target as "the first track result for Billie Jean"
   - which the original exact-phrase check (`"first search result"` /
   `"top result"`) missed entirely, silently falling through to the slow,
   already-proven-unreliable vision path instead of erroring loudly.
   Fixed by matching "a position word (first/top) AND a result-ish word
   (result/track/song)" both present, rather than one fixed phrase -
   robust to the actual paraphrasing an LLM produces in practice.
3. **Search text silently concatenated instead of replacing.** A
   screenshot taken mid-debugging showed the search box containing
   `"billie jean michael jackson billie jean is not my..."` - garbled,
   multi-query leftovers. `type_in_field`'s paste-based typing never
   selected existing field contents first, so if the field wasn't already
   empty (e.g. Spotify resuming a previous session's query on relaunch),
   the new text landed wherever the cursor happened to be instead of
   replacing anything - corrupting the query with no error raised
   anywhere. This is exactly the class of silent-wrong-state bug this
   project has spent most of its effort catching, just in a spot nothing
   was checking yet. Fixed by sending Cmd+A immediately before every
   paste, unconditionally (not just for Spotify) - a select-all-then-paste
   is strictly safer than a bare paste for any field, regardless of app.
4. **Pasting into Spotify's search field never actually submitted the
   search.** A screenshot taken right after a paste (no Return pressed)
   showed only the live autocomplete dropdown - not the full results page
   with the Top result card `_APP_TOP_RESULT_OFFSET` targets. Nothing in
   the Action agent's toolset presses Enter, so the *real* chain would
   click the fixed offset against a page that was never actually
   reached - explaining an otherwise-confusing "fixed_offset tier used,
   correct coordinates, but no playback change" result seen mid-testing.
   Fixed by pressing Return automatically at the end of `type_in_field`'s
   Spotify-shortcut path specifically (`used_shortcut` branch only) -
   submitting is the near-universal correct completion of "type into a
   search field opened via a search shortcut," so this is done for the
   caller rather than left as a capability nothing has.

**Final result, full agent chain, 3 clean runs in a row:** all 3
milestones (`open_app` -> `type_in_field` -> `click_ui`) succeeded every
time, with the final `click_ui` call verified via a genuine
`paused`/`Billie Jean` -> `playing`/`Billie Jean` transition read from
Spotify's real player state each time - not a screenshot guess, not "the
click didn't error." One run also hit a real, unplanned focus interruption
(VS Code became frontmost mid-chain) - the frontmost-app guard correctly
refused the out-of-turn `click_ui` call, and the Action agent recovered on
its own by calling `open_app` again before continuing, exactly the
guard's intended behavior rather than a failure to route around.

**Why this run of bugs matters beyond just "found and fixed":** none of
them were in the fixed-offset mechanism itself, which worked correctly
from its very first isolated test. They were all in the *path to* that
mechanism - agent tool selection, description phrasing robustness, field
state hygiene, and search submission - the connective tissue between a
verified-working primitive and an actually-reliable end-to-end command.
Testing only the primitive in isolation (as the 3/3 isolated result did)
would have missed every one of these.

---

## Browser control: reproducing a real reference architecture instead of designing from scratch

**Decision:** built browser automation (a Chrome MV3 extension + a second
WebSocket server + backend tools) by faithfully adapting a real, working
reference architecture pulled from a separate project (`moonwalk-
reference`), rather than designing a simpler approach from first
principles. Explicitly acknowledged going in as the highest-risk, least-
precedented piece of this whole build - browser extension JavaScript is a
stack nothing else in Jarvis touches, and the failure modes (MV3 service
worker eviction, single-page-app DOM churn, the perceive-act race) are
exactly the kind of thing that's expensive to discover by trial and error
but already-documented lessons in the reference.

**Why fidelity over a simpler design:** every piece of this architecture
that looks like unnecessary complexity turned out, on reading the
reference's own notes, to be a direct fix for a real failure mode that was
presumably hit in production once already:

- **Generation as a raw `Date.now()` timestamp, forwarded verbatim, never
  a backend-incremented counter.** A counter the backend owns would need
  the backend and the content script to agree on when to bump it - the
  timestamp sidesteps that entirely: whichever side asks "is this newer
  than what I had," the browser's own clock is the one source of truth,
  and the comparison is always strict `>`.
- **The "set-then-swap" `asyncio.Event` pattern**
  (`browser/bridge.py`'s `register_snapshot` and `record_action_result`):
  `event.set()` wakes exactly the waiters that already existed;
  immediately replacing it with a fresh, unset `Event()` stops future
  callers from returning instantly on a stale "already set" flag. Skipping
  the swap would make every `wait_for_snapshot` after the very first
  snapshot return immediately with no new snapshot having actually
  arrived - a subtle bug that would look like it worked in a quick test
  and then silently stop verifying anything.
- **Two decoupled singletons (`BrowserBridge` for connection/queue/events,
  `BrowserStore` for snapshot/element data)** rather than one class doing
  both. `queue_action` reads from the store but the store has no idea the
  bridge exists - kept exactly this way rather than merged, since the
  separation is what keeps "how do I wait for things" cleanly separate
  from "what do I actually know about the page."
- **Three-tier element resolution in the content script**
  (`agent_id` map lookup -> `ref_id` parse of the same map -> heuristic
  re-scan) is what makes an action survive a single-page app re-rendering
  the DOM between when a `ref_id` was captured in a snapshot and when the
  action actually executes. Tier 3's scoring hard-disqualifies on
  action-type mismatch rather than treating it as a penalty - verified
  directly in isolation (see below) that this can't be relaxed to "closest
  available match," which matters because a page frequently has a
  visually-similar-but-wrong element (e.g. a link near a button) that
  would otherwise win on label similarity alone.
- **Push-primary delivery**: queued actions are sent immediately over the
  open WebSocket rather than waiting for the extension's next poll cycle,
  because a 5-second poll interval would make every action feel laggy for
  no reason when a live connection already exists to push over.

**Adaptations made, not verbatim copies, per the explicit instruction to
write fresh code against the patterns rather than reuse code directly:**

- **Runtime-state/observability integration dropped.** The reference
  calls into a `runtime_state_store` (connection tracking, action-result
  history, a dashboard-style readability-extraction record) that has no
  equivalent anywhere in Jarvis. Replaced with plain `logger.info` calls
  at the same points - the *shape* of the state machine is unchanged,
  only the "who else gets told about it" integration was cut, since
  building a whole parallel observability subsystem wasn't asked for.
- **Trimmed action surface.** The reference supports `scroll`, `highlight`,
  `scanning_start`/`scanning_stop`, `extract_data`, and
  `extract_readability` (the last needing a vendored Mozilla Readability.js
  library) in addition to `click`/`type`/`select`/`refresh_snapshot`. Only
  the four needed for the actual tools being built
  (`click_web_element`/`type_in_web_field`, plus `refresh_snapshot` as the
  one snapshotless action) were implemented. `Readability.js` isn't
  vendored in, and the manifest doesn't load it. Extending this later is a
  matter of adding more branches to `content_script.js`'s `executeAction`
  switch and `background.js`'s action dispatch, not restructuring anything.
- **No tab ledger in `BrowserStore`.** The reference's own extraction notes
  say this part was "omitted for brevity" even in the source material, so
  there was no concrete pattern to adapt here - `_current_session_id`
  alone covers what Jarvis's single-tab-at-a-time use actually needs.
- **No options page or extension popup UI.** The manifest skips
  `options_page`/`default_popup` entirely; the bridge URL and token are
  hardcoded defaults (`ws://127.0.0.1:8765`, `dev-bridge-token`),
  overridable via `chrome.storage.sync` if ever needed, matching the
  reference's own settings-loading code, just with no UI built yet to
  actually change them.
- **Internal message-type names changed from `moonwalk_*` to `jarvis_*`**
  (`jarvis_snapshot`, `jarvis_execute_action`, `jarvis_collect_snapshot`) -
  these are purely internal `chrome.runtime.onMessage` types between this
  extension's own background script and content script, not part of the
  wire protocol to the backend, so renaming them for this project carries
  zero behavioral risk. The backend-facing WebSocket protocol message
  types (`browser_bridge_hello`, `browser_snapshot`, etc.) were already
  generically named and kept as-is.
- **Token env var renamed** `MOONWALK_BROWSER_BRIDGE_TOKEN` ->
  `JARVIS_BROWSER_BRIDGE_TOKEN` (same for the host/port vars) - purely a
  naming-consistency change with this project's own conventions.

**Deliberately-inherited flaws - reproduced, not fixed, as instructed:**

1. **`_try_push_actions`'s drain-before-confirmed-send ordering.** The
   pending action is removed from `_pending_actions` as soon as the push
   payload is built, *before* the `ws.send()` (itself fire-and-forget via
   `loop.create_task`) has actually completed. If the send fails after
   this point, the action is already gone from the queue - the `except:
   pass` "falls back to polling," but there's nothing left in the queue
   for a poll to find. A real gap: a failed push can silently drop an
   action with no retry path. Kept as-is for fidelity to the reference
   rather than fixed by draining only after a confirmed send.
2. **Push and poll both run unconditionally once authenticated**
   (`background.js`), not poll-only-as-a-true-fallback. Both paths can
   fire close together; `actionExecutionInFlight` is the only guard
   against double-execution, not a real mutual-exclusion mechanism. A
   cleaner design (e.g. only poll if no push has arrived in N seconds)
   was consciously not built, per the instruction to document this as a
   known, inherited limitation rather than improve on the reference at
   this stage.

**A genuine architectural decision the reference itself leaves ambiguous,
resolved for Jarvis:** the reference's own extraction notes flag
uncertainty about whether its bridge server runs as a literal separate OS
process or shares memory with a main agent server via the module-level
singleton pattern - noting these two framings are in tension for actual
separate processes (Python module state genuinely can't be shared across
separate `python x.py` invocations). Jarvis has no persistent "main agent
server" to begin with (`main.py` is a one-shot script), so this had to be
resolved concretely rather than left ambiguous: **`browser_bridge_server
.py`'s `serve_forever()` runs as an in-process `asyncio.create_task()`
alongside wherever the Action agent's tool calls execute, sharing one
event loop** - not a separate subprocess for the wired-in case. This is
required for correctness, not just convenience: `browser_bridge`'s
`asyncio.Event` objects are only safely awaitable from the event loop that
created them, so `browser_tools.py`'s `click_web_element`/
`type_in_web_field` (real `async def` functions - confirmed directly that
ADK's `FunctionTool` supports async callables via
`inspect.iscoroutinefunction`) and the server's WebSocket handler must run
on the same loop to see each other's state at all. A genuinely separate
OS process for `browser_bridge_server.py` is still used, correctly, for
the stage 1-3 isolated protocol test below, since a dumb scripted client
talking pure WebSocket JSON has no need for shared Python state.

**Real test results so far (isolated, before touching the extension):**
a fully in-process test - the bridge server started as a background task,
a scripted fake client connected over a real WebSocket, `queue_action`
called directly from the same process - confirmed the entire pipeline for
real: the fake client received its action via the **pushed**
`browser_actions` message (never polled), with the correct metadata
attached (`dom_path`/`role`/`tag`/`label`/`agent_id`, all pulled from the
snapshot's stored `ElementRef`); `wait_for_result` correctly unblocked
only after the fake client's `browser_action_result` arrived; and
`wait_for_snapshot(min_generation=...)` correctly returned only once a
snapshot with a strictly greater generation existed, with the newly-typed
field's value visible in it. The pure scoring/matching logic (exact-label
match > substring match, action-type mismatch as a hard `-1` disqualifier,
`dom_path` match weighted at 300 vs. 40 for a bare tag/role match) was
verified directly in Node against the real `scoreCandidate` function, not
just read and trusted.

**Not yet tested at the time of this entry:** the real Chrome extension
loaded via Developer Mode, and the live flight-search case - both pending
manual browser interaction. Will be appended once run for real.
