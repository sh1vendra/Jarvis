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

---

## Real Chrome extension load: connection, an unplanned reconnect test, and a real-world page snapshot

**What happened:** the extension was loaded unpacked in Chrome and, without
any scripted client standing in for it, produced a real
`browser_bridge_hello` handshake against the running bridge server
(`session=chrome-session-<timestamp>, name=jarvis-browser-bridge`). The
active tab at the time was a genuine, unscripted Wikipedia article - not a
page built or chosen to make the test easy.

**Why testing against an arbitrary real page mattered, not just a
controlled fixture:** every isolated test up to this point (the in-process
push/event test, the Node-level scoring checks) used hand-built
`PageSnapshot`/`ElementRef` data - useful for testing the *bridge's* logic
in isolation, but it can't validate `content_script.js`'s actual DOM
tagging and snapshot-building code at all, since that code never ran
against real markup in any of those tests. A real Wikipedia page has none
of the conveniences a purpose-built test fixture would - inconsistent
markup, deeply nested layout, ads/chrome/navigation boilerplate, elements
with no helpful `aria-label`. Getting a clean **89-element snapshot** back
from it on the first real attempt is a materially stronger signal than
any number of passing fixture-based tests that the `INTERACTIVE_SELECTOR`/
`READABLE_SELECTOR` queries, `isVisible`/`isReadableCandidate` filtering,
and `serializeElement` field-mapping all actually hold up against markup
nobody wrote with this project in mind.

**The reconnect logic got a genuine, unplanned real-world test.** The
plan at this step was only to confirm the initial connection and a
snapshot. Adding a logging line to `register_snapshot` (needed because
that method had no visibility into the server log at all - a real, small
gap noticed while trying to confirm the snapshot had arrived) required
restarting the running bridge server process. That restart wasn't a
scripted disconnect test - it killed the extension's live WebSocket out
from under it with no warning, exactly the kind of interruption
`background.js`'s `scheduleReconnect()`/`RECONNECT_DELAY_MS` logic exists
for, but this specific scenario was never deliberately exercised before
now. It worked: the extension's `close` handler fired, it reconnected
within the expected ~1.5s window, and it re-sent a fresh snapshot without
any manual intervention. Worth being honest about exactly what happened
here - this wasn't a planned reconnection test, it was a side effect of
fixing an unrelated logging gap, and it validated the reconnect path
anyway. An accidental real-world validation like this is arguably more
convincing than a deliberately scripted one, precisely because nothing
about the timing or trigger was arranged to make it succeed.

**Fixed along the way:** `register_snapshot` (`browser/bridge.py`) had no
logging at all, unlike every other state-changing method in the class -
made it impossible to confirm from the server log whether a snapshot had
actually landed versus just trusting the client-side `browser_snapshot_ack`
response. Added a `logger.info` call reporting session, generation,
element count, and URL - purely additive, no behavior change.

**A real, recurring operational gotcha, not fixed - documented instead:**
getting from "extension loaded" to "a live test can actually run" required
far more manual intervention than expected, and the cause is now
understood clearly enough to record rather than chase further. Every time
the bridge server process was killed and a new one started in quick
succession (needed repeatedly here, since `browser_tools.py`'s
`click_web_element`/`type_in_web_field` require sharing a process with
the server - see the architecture entry above), the MV3 service worker
would frequently fail to reconnect at all, with zero log evidence it even
attempted to - consistent with Chrome evicting the worker outright rather
than it just being slow. A `chrome://extensions` manual reload reliably
fixed it every single time; simply waiting never did, even over several
minutes. Practical lesson for demoing or testing this further: don't
cycle the bridge server rapidly - start it once, confirm the connection
is live and stable first, and only then run whatever needs the combined
in-process server+tool-call setup, reloading the extension by hand if a
restart was unavoidable.

---

## Google Flights live test: two real, distinct failures, both honestly caught, neither forced past

**Attempt 1 - wrong element found.** `find_web_element("destination")`
matched `ref_id=jw_80`, labeled "Popular flight destinations from United
States" - a real element on the real page, but not the input field. Not a
scoring bug: dumping the actual page structure showed the real destination
field (`jw_17`) has `aria_label="Where to? "` / `placeholder="Where to?"` -
the word "destination" does not appear anywhere on or near it. "destination"
matched jw_80 purely because that unrelated element's text happened to
contain the substring, exactly the class of accidental collision the
priority-ordered, exact-beats-substring scoring is built to minimize but
can't eliminate when the query word genuinely isn't present on the real
target at all. `type_in_web_field` then correctly timed out trying to type
into it (`error: "result_timeout"`) rather than reporting a false success -
`jw_80` isn't a text-input-capable element, so `content_script.js`'s
`executeType` had nothing to act on.

Confirmed the fix needed was query wording, not matcher logic: re-running
`find_web_element` with `"where to"`, `"Where to?"`, and `"Where to"` all
correctly found `jw_17` on the same snapshot. Documented per the standard
set for this whole step: don't force a wrong match through, tell the user
plainly what was tried and what the real page structure actually looks
like, let them decide the next query rather than guessing further alone.

**Attempt 2 - right element, real DOM reaction, but the typed text never
landed.** With `"Where to?"` correctly resolving to `jw_17`,
`type_in_web_field` queued and dispatched the type action for real:
element count jumped from 85 to 102 (+17, almost certainly Google
Flights' own autocomplete dropdown opening in response to focus/typing)
and the snapshot generation genuinely increased
(`1788034508270` -> `1788034514059`). But the field's real `value` in
that newer snapshot was still `""` - the second-layer check
(`type_in_web_field` reads the field's actual value back, not just "did a
newer generation arrive") caught this and correctly reported
`success: False, error: "value_not_verified"` rather than trusting the
generation bump alone as proof of success. This is exactly why that
second check exists, not a redundant belt-and-suspenders addition - a
snapshot with a materially different element count is about as convincing
a "something happened" signal as this architecture can produce, and it
still wasn't sufficient on its own here.

**Diagnosis, not yet fixed:** `content_script.js`'s `executeType` sets
`el.value = text` directly and dispatches plain `input`/`change` events.
Google Flights' destination field is almost certainly a React-controlled
input - React tracks value changes through its own wrapped native setter,
and a raw `.value` assignment is a well-documented case where React's
internal state doesn't observe the change even though the DOM briefly
reacts to it (explaining the dropdown appearing while the bound value
stayed empty). The standard fix for automating a React-controlled input
is calling the *native* `HTMLInputElement.prototype.value` setter
directly (bypassing whatever setter React has patched onto the instance)
before dispatching the `input` event, so React's own change detection
actually fires. Not implemented yet - flagged for a decision before
building it, same as the fixed-offset simplification earlier in this
project: a real, scoped code change, not something to guess into working
silently.

---

## The native-setter fix was built correctly, and it still didn't work - a deeper, likely unfixable-from-a-content-script cause

**What was built:** `content_script.js`'s `executeType` now calls
`Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set`
directly on the element before dispatching real `input`/`change` events,
exactly the standard technique for React-controlled inputs described in
the entry above. Confirmed present and correct on disk at test time (not
assumed - grepped the actual file content immediately before concluding
anything, after the first post-fix test looked suspiciously identical to
the pre-fix failure).

**Retested clean, twice, both still failed identically:** after
confirming (via the field's own `label`/`value` in a fresh snapshot) that
the destination field was genuinely empty before typing - not a
contaminated page state like the earlier false alarm - `type_in_web_field`
still reported `value_not_verified` both times, with the exact same
symptom as before the fix: element count jumps 85 -> 102 (Google's
autocomplete dropdown opening, proving focus/click events land), snapshot
generation genuinely increases, but the field's real `value` in that newer
snapshot stays `""`.

**Real root cause, confirmed directly, not just theorized:** every event a
content script dispatches via `element.dispatchEvent(...)` - regardless of
which value-setting technique preceded it - carries `event.isTrusted ===
false`. This is a read-only browser property; no JavaScript running on the
page (content script or otherwise) can ever set it to `true`. Some Google
products are known to gate input handling on `isTrusted`, specifically to
reject exactly this class of automation. Tested the hypothesis directly
rather than left it as a guess: with the automated attempt still showing
an empty value, the user manually typed "New York" into the same field
with a real keyboard - it worked immediately, and the URL picked up a real
`tfs=` search-state parameter. Same field, same page, same moment in time,
only the input's trust level differed. That's about as close to a
controlled comparison as this could get without instrumenting the page
itself, and it isolates the variable cleanly: **real keystrokes work,
synthetic ones - by any DOM-API technique - do not.**

**Why this isn't fixable from within a content script at all:** the native-
setter-plus-event technique is the correct, complete fix for the class of
problem it targets (a framework's controlled-input state not observing a
plain property write) - it worked as designed in the sense that it does
exactly what it's supposed to do. It just doesn't touch `isTrusted`,
because nothing running in the page's own JS context can. Actually
producing a trusted input event requires simulating input at the OS or
browser-process level (e.g. the Chrome DevTools Protocol's
`Input.dispatchKeyEvent`, which Chrome itself treats as trusted since it
originates outside the page) - a fundamentally different, much larger
mechanism than anything `content_script.js` can do from inside the page,
requiring the `chrome.debugger` permission and a different automation
path entirely, not a refinement of the current one.

**Not pursued further at this stage - a demo-command decision, not a code
decision, is the right next step here**, consistent with how the earlier
Spotify click-precision dead end was handled: when a target genuinely
can't be reached with the current architecture, the answer is to
reconsider which target the demo uses, not to keep forcing the same
approach. Discussion follows in the next planning entry once that decision
is made.

---

## Cheap diagnostic before committing to a CDP-based fix: is `isTrusted` rejection Google-Flights-specific or universal?

**The question:** before investing in the much larger CDP-based
(`chrome.debugger`) input-simulation approach that would actually defeat an
`isTrusted` check, worth first confirming the wall is Google Flights'
own defense and not something the native-setter-plus-event technique
fundamentally can't do on *any* React-controlled input - a cheap test
against a different, similarly React-driven booking site settles that
before any bigger architectural commitment.

**Test: Kayak.com's destination field.** Same methodology as every other
live test in this project - real snapshot, real `find_web_element` call,
real `type_in_web_field` call, real generation numbers.

Same false-positive pattern showed up first, for the same reason as
Google Flights: `find_web_element("destination")` matched a decorative
link ("Group destinations under $238"), not the real field - the word
"destination" simply isn't Kayak's actual field copy either. Its real
field: `aria_label="Destination location"`, `placeholder="To?"`. Using
`"To?"` correctly resolved to it (`ref_id=jw_21`, `tag=input`,
`role=combobox`).

**Result: genuine success, not a false positive.**
`type_in_web_field(jw_21, "New York")` returned `success: True`, generation
went from `1788113466596` to `1788113469633`, and the field's real value
in that fresh snapshot was confirmed as `'New York'` - the same two-layer
verification (newer generation + real value read-back) that correctly
caught the Google Flights failure now correctly confirms a real success
here, on the same code path, same fix, no site-specific special-casing.

**Conclusion:** the `isTrusted` rejection is Google Flights' own defense,
not a universal limitation of the native-setter-plus-event technique or
of this architecture more broadly. The fix built earlier is correct and
general, exactly as intended - it simply cannot clear a specific,
deliberate anti-automation gate that not every site has. This changes the
demo-command calculus: a CDP-based fix is not needed to unblock a working
flight-search demo, since a comparable, real site (Kayak) already works
end-to-end with the current architecture. Whether to still pursue Google
Flights specifically (via CDP) is now a separate, lower-urgency decision
rather than a blocker.

---

## Kayak locked in as the flight-search demo target; wired into main.py; one more real bug found running it for real

**Decision:** the flight-search demo command is now Kayak, not Google
Flights (`backend/main.py`: `"on the Kayak website that's already open,
search for a flight to New York"`, mirroring the Spotify/Reminders demo
cases' pattern - Orchestrator -> Planner -> Action agent, run for real).
Reasoning is entirely the diagnostic from the entry above: Google Flights'
own input handling appears to specifically gate on `event.isTrusted`,
rejecting the standard, correct native-setter-plus-event technique that
every other part of this architecture uses successfully - confirmed via a
direct, controlled comparison (same code path, same fix, no
site-specific special-casing) against Kayak's comparably React-controlled
destination field, which worked cleanly. This is not a flaw in the browser
bridge or in `type_in_web_field`'s verification logic - it's one specific
site's deliberate anti-automation defense, and Kayak doesn't have it (or
at least doesn't apply it to this field).

**A real bug found wiring this into `main.py` itself, not just ad-hoc test
scripts:** `asyncio.create_task(serve_browser_bridge_forever())` without
keeping a reference to the returned `Task` let Python's garbage collector
reap the bridge server mid-run - confirmed directly, the server would die
within seconds every time, well before any browser-tool call could reach
it, throwing `RuntimeError: coroutine ignored GeneratorExit` and `Task was
destroyed but it is pending!`. This is a well-documented asyncio gotcha
(the docs explicitly warn: keep a reference to a task or it can vanish
mid-execution, since `asyncio` itself only holds a weak one). Fixed by
storing it in a module-level `_browser_bridge_task` rather than discarding
the return value - a real, necessary fix, not defensive style.

**Full milestone test, run for real via the actual Orchestrator -> Planner
-> Action chain (not a hand-scripted diagnostic):**

The Planner produced two milestones on its own: (1) destination field set
to New York, (2) the search executed. The Action agent worked through
`find_web_element` on its own, trying several description guesses before
landing on the right one - `"destination input field"` and `"Enter
origin, destination, or hotel"` both correctly failed with `no_match`,
`"To"` matched a wrong element (`"Jump to content"`, a skip-navigation
link) the same way `"destination"` did against Kayak earlier, and
`"Where to?"` finally found the real field (`jw_21`). This is the agent
genuinely doing what `find_web_element`'s design intends - trying
descriptions and accepting honest failures - without any milestone-
specific coaching from this session.

**Milestone 1 result: reported unverified, honestly, not a false
success** - and for a genuinely interesting reason. The field already
showed "New York" *before* this run's own `type_in_web_field` call, left
over from the diagnostic test earlier in this session (a fresh Kayak
navigation had been requested beforehand, but evidently didn't fully
reset the field - Kayak likely persists the last query client-side).
`type_in_web_field` then retyped the identical text "New York" into a
field that already held "New York," and no newer snapshot arrived within
the 5s window - plausibly because writing the same value produces less
DOM disturbance (no new autocomplete suggestions rendering differently,
etc.) than writing a genuinely new value, and never crossed the
MutationObserver's 5-mutations-per-batch threshold. The tool correctly
reported `no_newer_snapshot` rather than declaring success because the
field happened to already show the right text - exactly the right call,
since it did not verify *this action* did anything, regardless of what
the field's contents were for unrelated reasons.

**Milestone 2 result: genuine success, fully verified.**
`find_web_element("Search button")` correctly found the real button
(`jw_25`); `click_web_element` reported success with a real newer
generation (`1788114565576` -> `1788114631578`), confirming the click
genuinely changed the page.

**Worth flagging plainly:** this run had no "stop before submitting"
boundary the way the earlier Google Flights work deliberately did - the
Planner included search execution as its own milestone, and the Action
agent completed it for real, submitting an actual live Kayak flight
search. Low-stakes (a search query, not a purchase or any commitment),
but a real external action nonetheless, worth naming rather than leaving
implicit.

**What this run does and doesn't prove, stated plainly:** it proves the
full agent chain can pick tools, recover from wrong `find_web_element`
guesses, and refuse to claim an unverified success - genuinely valuable,
since none of that was hand-scripted this time. It does *not* cleanly
re-prove `type_in_web_field`'s core mechanism against a truly blank field
through the full chain, since the field wasn't blank going in. That clean
proof already exists from the direct diagnostic test earlier in this
session (blank field confirmed first, single automated `type_in_web_field`
call, real value `'New York'` confirmed after, user independently verified
they had not typed it themselves) - this run adds a different kind of
evidence (real agent autonomy, honest failure reporting under a
non-ideal starting state) rather than repeating that one.

---

## Two real gaps fixed: exact-match short-circuit, and an orchestration-level approval gate

**Gap 1 - exact-match short-circuit in `type_in_web_field`.** The
previous entry's milestone-1 failure (`no_newer_snapshot` on a field that
already held the correct text) traced to a real limitation of
generation-based verification: retyping an already-correct value doesn't
reliably produce a fresh snapshot, since nothing observably changes for
the content script's `MutationObserver` batch threshold to cross. Rather
than loosen the generation check (which would also weaken it for the case
it actually exists to catch - detecting a click/type that silently did
nothing), `type_in_web_field` now checks the field's current real value in
the latest snapshot *before* queuing any action at all, and returns
immediate success with no action dispatched if it already matches. This
is a strictly more honest check than the thing it replaces for this case:
"the value is already correct" is directly observable and doesn't need an
action's side effect to confirm it.

**Verified live, cleanly:** against a real Kayak destination field already
holding `'New York'`, calling `type_in_web_field('jw_21', 'New York')`
returned `success: True` in `0.00s` with `"nothing to type, no action
dispatched"` - not a generation bump, not a dispatched action, an
immediate, honest short-circuit. Also unit-tested the fall-through path
(field holds a *different* value) against a deliberately-disconnected
bridge, confirming it still correctly proceeds to the real dispatch path
(`queue_failed`, not silently short-circuited) rather than this change
accidentally swallowing the case it's not meant to handle.

**Gap 2 - a real approval gate, enforced at the orchestration level, not
inside the Action agent.** `agents/planner.py`'s `Milestone` model gained
a `requires_approval: bool` field, with instruction guidance to give a
task's final, hard-to-reverse, consequential step (submitting a search,
completing a purchase, etc.) its own separate milestone and mark only
that one `requires_approval=true`. `main.py`'s new
`run_milestones_until_approval` is the actual enforcement point: it runs
milestones through the Action agent in order, but the moment it reaches
one with `requires_approval=true`, it returns that milestone *without
ever calling `run_action` on it* - the Action agent never even sees it,
let alone gets a chance to decide whether to run it. This was a
deliberate placement choice: putting the gate inside the Action agent (or
inside `click_web_element`) would mean trusting the same
self-reporting agent this whole project has repeatedly found unreliable
elsewhere (Bug 2's false success, the Google Flights `isTrusted` chase,
etc.) to also police its own execution - the gate needed to sit somewhere
that doesn't depend on the agent's own judgment at all, i.e., the loop
that decides which milestone gets sent to the agent in the first place.

There's no real approval-modal UI yet, so approval is simulated in
`main.py`/test scripts with an explicit follow-up `run_action(...)` call
for the paused milestone, standing in for a future "approve" click -
deliberately reusing the *same* ADK session throughout the pause, so the
mechanics of "does context survive the pause" get tested for real rather
than assumed.

**Verified two ways, honestly, including a real limitation of the current
test setup (not the fix itself):**

1. **Unit-level, unambiguous:** a milestone list where the *first*
   milestone already has `requires_approval=true` was run through
   `run_milestones_until_approval` with a deliberately invalid
   `action_runner`/session (`None`, `"unused-session"`) - it returned the
   pending milestone immediately without erroring, proving the gate
   short-circuits *before* touching the action runner at all, not just
   before completing.
2. **Live, via the real agent chain:** the Planner, unprompted beyond the
   updated instruction, correctly produced `requires_approval: false` for
   "enter the destination" and `requires_approval: true` for "initiate
   the search" as two separate milestones - exactly the intended shape.
   `run_milestones_until_approval` printed the pause banner and did not
   run milestone 2 automatically; only after the simulated approval call
   did the Action agent actually attempt it. This confirms the gate's
   control flow works correctly in a real run, not just in a synthetic
   unit test.

**What this live run does NOT prove, stated plainly rather than rounded
up:** in this specific run, `browser_store` never received a real
snapshot at all (both milestones failed with `no_snapshot`, and a
follow-up retry that waited up to 60s for a live Kayak snapshot before
even issuing the command still never got one - Kayak was very likely no
longer the active tab by that point in a long testing session). So while
the *approval gate's control flow* is proven live, a single continuous
run where milestone 1 actually types into a real field, the gate pauses,
approval is given, and milestone 2 actually clicks Search and lands on a
real results page - all in one unbroken sequence - has not yet happened.
The two things that *are* independently, cleanly proven (the exact-match
short-circuit, live; the gate's control flow, both in a unit test and
live) compose correctly by construction (neither touches the other's code
path), but a single unbroken run tying all of it together is the natural
next verification step, blocked only on Kayak being the live active tab
when it runs - not on anything in the fix itself.

---

## The unbroken run: both fixes proven together, in one continuous live sequence

**With Kayak confirmed as the live active tab and the destination field
genuinely blank, the full sequence ran clean, start to finish, no
simulated steps besides the approval itself:**

1. `find_web_element("the destination field")` failed honestly
   (`no_match`); `find_web_element("Where to?")` correctly found the real
   field (`jw_21`).
2. `type_in_web_field(jw_21, "New York")` went through the *real*
   dispatch-and-verify path this time (the field was genuinely empty, so
   the exact-match short-circuit didn't apply - that path was already
   proven separately, live, in the previous entry) - real generation
   `1788116627948`, real value `'New York'` confirmed.
3. The approval gate held: `run_milestones_until_approval` printed
   `AWAITING APPROVAL` and did not run milestone 2. The real page state at
   the pause point was checked directly - `url=https://www.kayak.com/`,
   no search-result parameters - confirming Search genuinely had not been
   clicked yet, not just that the code path hadn't been reached.
4. Simulated approval issued; the Action agent then found the real
   `Search` button (`jw_25`) and clicked it, verified via a real
   generation increase (`1788116628755` -> `1788116630908`).

**This closes the gap the previous entry left open** - the exact-match
short-circuit and the approval gate are no longer just independently
proven; they're proven composing correctly in one real, continuous run,
which is what actually matters for the demo.

**One honest limitation, not glossed over:** the post-click snapshot's
`url` field stayed the base `https://www.kayak.com/`, with no
search-results query parameters. The generation increase and the click's
own success are real, verified signals that something changed - but
"the search results page is displayed," milestone 2's `success_signal`,
is the Action agent's own text summary of what it expects happened, not
something `click_web_element`'s verification independently confirms.
`click_web_element` was always documented as having no universal
post-click content check (see the earlier click-outcome-verification
entry) - this is that same, already-disclosed limitation showing up here,
not a new gap. A future improvement could have `click_web_element`
accept an optional expected-URL-substring or content check for cases like
this one, mirroring how `mac_control.py`'s `click_ui` takes an
`expected_outcome`.

---

## Push-to-talk hotkey instead of Porcupine wake-word detection

**Decision:** Jarvis will be triggered by a push-to-talk keyboard hotkey,
not by an always-listening "Hey Jarvis" wake word. Porcupine (and
wake-word detection generally) is dropped from scope entirely - not
deferred, not stubbed, just not built.

**Why:** Wake-word detection is a real audio-pipeline subsystem with its
own failure modes - false positives (triggering on background speech or
TV), false negatives (not triggering when the room is noisy), a
continuously-open microphone stream that has to coexist with the OS mic
permission model, and Porcupine's own access-key/model-file setup. None
of that is visible in a recorded demo video: on camera, "user presses a
key, then speaks" and "user says a wake word, then speaks" look
identical - a beat, then a command. The wake word adds a whole class of
things that can go wrong during a take without adding anything the
viewer can see. A hotkey is deterministic: it fires exactly when pressed
and never when not.

**Constraints:** This is a demo-reliability call, made with the same
reasoning as the Kayak-over-Google-Flights swap - pick the path with
fewer live failure modes when the alternative buys no visible value. The
real Electron global-hotkey UI is a frontend concern and comes later;
for now the push-to-talk trigger is a plain Python `input()` prompt
(press Enter to start, Enter again to stop), which is enough to prove
the STT mechanism against real microphone audio.

**What we didn't do:** No Porcupine dependency, no `.ppn` model files, no
always-on `RawInputStream`, no voice-activity-detection tuning. If a
wake word is ever wanted post-demo it's an additive change at the
capture layer - nothing downstream of `transcribe_audio` knows or cares
how recording was triggered.

---

## Voice is the entry point, not an add-on - and "STT works" is not the completion bar

**Decision:** Jarvis has no text-input mode in the real product. Real
spoken voice is the only way a command enters the system. A demo command
(Spotify, Reminders, Kayak) does not count as *done* until it has been
executed end to end from real microphone audio - not typed text, not a
`SimulatedAudio` transcript. All three must clear that bar before any
frontend work starts.

**Why:** Every earlier milestone in this build used typed commands in
`main.py` as the entry point. That was the right call at the time - it
isolated the agent logic (orchestrator classification, planner milestone
shape, action tool selection, verification) from the audio pipeline, so
failures had one obvious cause. But it means everything "proven" so far
was proven against an input path the product will never actually use.
The real path adds real failure modes: mistranscription of proper nouns
("Spotify", "Billie Jean", "Kayak"), the free Google endpoint's latency
and rate limits, background-noise sensitivity, the macOS mic-permission
gate, and device-selection surprises (the default input device changes
when AirPods connect). None of that shows up in a typed test. If the
demo is recorded with real voice - which it will be - then real voice is
what has to be tested.

**Why all three, not one:** "STT technically works" would be satisfied by
transcribing a single easy sentence once. That proves the library call,
not the product. Each demo command stresses STT differently: Spotify has
two proper nouns and a brand name; the reminder is all common words but
carries a time expression ("tomorrow at 5 p.m.") the planner has to
parse out of the transcript; Kayak has a brand name plus the
approval-gate flow, which has to pause and resume correctly even when the
command that triggered it came from voice. Testing one and extrapolating
would hide exactly the failures that matter for a reliable recording.

**What we didn't do:** Did not keep typed commands as an equal entry
point. `main.py`'s default is now the voice session; the typed pass
survives only as `python main.py --typed`, explicitly labelled as
agent-logic regression scaffolding, not a completion check. Did not
auto-approve the approval gate in the voice flow - it waits on a real
Enter press so the pause is visibly real (see the approval-gate entry
above).

---

## Voice pipeline built STT-output-first: simulated transcripts before a real microphone

**Decision:** Build and test the voice -> agent path in stages, starting
from the *output* of speech-to-text (a known text string) and working
backward toward real audio, rather than starting from a microphone.

1. `voice/stt.py` with `transcribe_audio(audio_data)` wrapping Google's
   free endpoint, plus a `SimulatedAudio` object that carries a known
   transcript `transcribe_audio` returns verbatim - no network, no audio.
2. Wire a `SimulatedAudio` transcript through the existing
   Orchestrator -> Planner -> Action chain in `main.py`, as a test case
   shaped exactly like the existing Spotify/Reminders/Kayak ones.
3. Only then build real capture (`sounddevice` push-to-talk) and feed
   real `AudioData` to the same `transcribe_audio`.
4. Record a real spoken command, transcribe it, run it through the full
   chain for real.

**Why:** Two subsystems are new at once - a voice capture/transcription
layer and its handoff into the agent chain. Testing them together from
the start means every failure is ambiguous: did the mic not capture, did
Google mis-transcribe, or did the agent chain choke on the text? Feeding
a known-good transcript through first proves the handoff is structurally
sound in isolation, so that when real audio is introduced the only new
variable is the audio itself. The `SimulatedAudio` path isn't throwaway
test scaffolding either - it's the same seam a future text-input mode
(typing a command instead of speaking it) would use.

**What we didn't do:** No mock of the `speech_recognition` library
internals or Google's HTTP response - the simulation is at the clean
boundary (`transcribe_audio`'s input), not inside it, so the real
recognition code path is exercised untouched the first time real audio
reaches it. No attempt to test real-audio and agent-chain integration
before the simulated handoff was proven.

**Superseded:** the `SimulatedAudio` test case was removed from `main.py`
once the real voice session landed - it had done its job (proving the
handoff) and keeping it would have muddied the "voice is the only entry
point" line. `SimulatedAudio` the class stays in `voice/stt.py` for unit
tests; it's just no longer wired into the default run.

---

## Real-audio capture: built and library-verified, real-microphone run blocked on a macOS permission gate

**What's proven, for real:**
- `SpeechRecognition==3.17.0` installs and imports on this repo's Python
  3.14 (its classifiers stop at 3.13 and comment 3.14 out, but
  `requires-python` is `>=3.9` and the 3.13+ shims it pulls -
  `standard-aifc`, `audioop-lts` - resolve fine on 3.14). `recognize_google`
  is still present and still free (generic key baked into the library,
  `speech_recognition/recognizers/google.py`); signature
  `recognize_google(audio_data, key=None, language="en-US", pfilter=0,
  show_all=False, with_confidence=False)` - not deprecated, not moved.
- `SimulatedAudio` -> `transcribe_audio` -> Orchestrator -> Planner
  handoff runs clean (real Gemini call, real 3-milestone `MilestonePlan`
  parsed back).
- `sounddevice==0.5.6` installs with its own bundled PortAudio (no
  Homebrew `portaudio`) and `sr.AudioData` builds from raw int16 bytes.

**Device selection is resolved fresh on every recording, not cached.**
`sounddevice`'s default input device changes at runtime - between two
sessions here the default went from "MacBook Air Microphone" (48 kHz) to
"Shivendra's AirPods Pro" (24 kHz) just by connecting the AirPods.
`record_push_to_talk` therefore re-resolves `None -> concrete index` on
every call, verifies the device actually has input channels, records at
the device's *native* sample rate (Google's endpoint takes anything
>= 8 kHz, so there's no reason to force-resample on the way in), and
prints a peak-amplitude figure so an all-silent capture (permission
denied, or wrong device) is obvious immediately rather than surfacing as
a mystery mistranscription. `python main.py --list-devices` and
`--device N` exist for when the default isn't the right mic.

**What's NOT yet proven, and why:** actual microphone recordings of the
three commands. `sounddevice.RawInputStream`'s first open blocks
indefinitely on the macOS TCC microphone gate when launched from inside
the IDE/agent process tree - the permission prompt never surfaces (a
headless parent can't show it), so the open hangs instead of erroring.
This is the documented "tell the user what to approve rather than work
around it" case. The real run has to be done by hand from a normal
terminal: `cd backend && python main.py`, approve the Microphone prompt
for the terminal app on the first recording, then speak each command when
prompted.

**All three demo commands are tested by real voice, back to back**, not
one representative command:
- **Spotify** - "open Spotify and play Billie Jean by Michael Jackson":
  three proper nouns, the hardest STT case. Precondition: Spotify app
  running.
- **Reminder** - "set a reminder to call mom tomorrow at 5 p.m.": common
  words, but the planner has to pull a time expression out of the
  transcript. No precondition; side effect is one entry on the "Jarvis
  Test" list.
- **Kayak** - "open Kayak and search for a flight to New York": brand name,
  plus Jarvis has to open Chrome and navigate to Kayak itself (first
  milestone), plus the approval-gate flow, which must pause before
  submitting the search and resume on a (simulated) approval even though
  the command came from voice. No manual browser setup (see the entry
  below); only assumption is the Jarvis extension being installed.

---

## The Kayak command had a hidden manual precondition that contradicted Jarvis's whole design

**Finding:** every version of the Kayak flight-search command up to this
point assumed Chrome was already open with kayak.com as the active tab.
That assumption was baked into `main.py`'s command text ("on the Kayak
website that's already open, ...") and into the milestone plan, which
started at "type New York into the destination field" - there was no
milestone for getting to Kayak at all. So a "working" Kayak demo actually
required the operator to open Chrome and navigate to kayak.com by hand
first. That directly contradicts the core premise: a spoken command
should be sufficient on its own, and Jarvis should execute every real
step, browser included. It was a demo that only worked because the
environment was pre-staged off-camera.

This is different in kind from Spotify's "app already running" assumption.
"Open Spotify" is a real milestone the Planner emits and the Action agent
executes via `open_app` - launching it is handled. The Kayak gap was a
step that no milestone covered and no tool could perform.

**Fix:**
1. New `navigate_to_url` tool (`tools/browser_tools.py`). It runs
   `open -a "Google Chrome" <url>` (launches Chrome if needed, loads the
   URL), forces the foreground switch and polls `_frontmost_app_name()`
   until Chrome is really frontmost (same pattern as `open_app`), then -
   the real verification - waits for the browser bridge to receive a page
   snapshot whose own URL is on the target host. That one snapshot
   confirms three things at once: the page loaded, the extension is
   connected, and its content script is live on the right page. A
   newer-but-wrong-host snapshot (the tab that was already open) just
   raises the generation floor and it keeps waiting; strict `>` on
   generation means it can't pass on a stale snapshot it already had.
2. Planner instruction now requires the first milestone of any website
   task to be "the browser is open showing that site," naming the address
   (e.g. "Google Chrome is open with www.kayak.com loaded"). Verified
   across 3 runs: the Kayak plan now reliably starts with that milestone,
   approval gate still lands on the final submit-search milestone.
3. Action agent instruction maps that milestone shape to `navigate_to_url`
   and tells it to turn a site name into a URL itself (Kayak ->
   `https://www.kayak.com`), and to never call `find_web_element` before
   it.
4. `main.py`'s command is now "open Kayak and search for a flight to New
   York"; the manual precondition is gone.

**Verified for real (non-voice integration test, bridge + real extension +
real Chrome):** `navigate_to_url("https://www.kayak.com")` returned
`success=True` with the confirming snapshot's real URL
(`https://www.kayak.com/`) and generation. Re-run after seeding Chrome
onto a different page first: it correctly waited past the stale snapshot
for a genuinely newer kayak.com one (generation `...167116` -> `...172685`)
rather than passing on the page that was already there. The full
voice-driven chain (navigate -> type -> approval gate -> approved ->
submit) still needs the real voice run to be end-to-end confirmed.

---

## The backend gets a WebSocket server: the CLI harness was never a real entry point

**Decision:** Wrap the existing Orchestrator -> Planner -> Action pipeline
in `backend/servers/agent_server.py`, a loopback WebSocket server, and make
that the interface the Electron UI talks to.

**Why:** The pipeline only ever ran from `main.py`'s command-line harness,
and two things in that harness were test scaffolding standing in for
product behavior:

1. **The approval gate was an `input()` call.** `run_plan_with_approval_gate`
   paused on a keypress in the terminal. That proved the pause-and-resume
   *mechanics*, but a terminal keypress is not a user approving an action in
   a UI - there was no way for a real interface to answer the gate.
2. **State was printed text.** The UI needs to know it is thinking vs doing
   vs waiting for approval. Scraping stdout for that would be a parser
   sitting on top of human-readable prose - brittle, and it would break the
   moment a print statement was reworded.

So the server changes exactly those two things and nothing else about the
pipeline. `main.run_command`/`run_action` gained an optional `on_event`
callback defaulting to `None`, so every existing CLI path behaves
identically; the server passes a callback that forwards each pipeline event
to the client as JSON. The approval gate becomes an `asyncio.Future` the
pipeline awaits, resolved only when the client sends an
`approval_response`.

**Constraints:** It deliberately mirrors `browser_bridge_server.py`'s shape
(loopback `websockets` server, JSON frames, one handler coroutine) so this
codebase has one server pattern rather than two. Both servers run as
asyncio tasks in one process on one event loop - a hard requirement, not a
convenience: `browser/bridge.py`'s `asyncio.Event`s are only awaitable from
the loop that created them, and the Action agent's browser tools await them
from inside this server's request handling. `agent_server.py`'s `_main()`
starts both.

**One real bug this design had to avoid:** the pipeline must run as a
separate `asyncio.Task`, not be awaited inline in the message loop. Awaited
inline, the handler stops reading messages while the pipeline blocks on the
approval Future - and the message it is waiting for is the very
`approval_response` it can no longer receive. Guaranteed deadlock; the task
split is what prevents it.

**Verified for real, over a live socket:** ping/pong; unknown-message error
handling; a conversational command answered by the Orchestrator with no
plan; and the full reminder command running end to end with real tool
results (`open_app success=true`, `create_reminder success=true`, real
reminder created). The approval gate genuinely blocked, and on a **reject**
the milestone was never executed - confirmed by recording every
`tool_call`/`milestone_start` event after the gate and finding none.

**What we didn't do:** No REST/HTTP layer, no session persistence across
connections, no auth. The browser bridge has a shared-token handshake
because a webpage could otherwise reach it; this server is bound to
loopback and talks only to a local Electron process, so a token would be
ceremony. Worth revisiting if the backend ever binds beyond 127.0.0.1.

---

## Audio capture moves to the Electron renderer, and MediaRecorder cannot be used

**Decision:** Capture microphone audio in the Electron renderer via the Web
Audio API's `AudioWorklet`, producing raw int16 PCM - not with
`MediaRecorder`, and no longer in Python.

**Why capture moved out of Python:** The trigger is now a global hotkey, and
the hotkey lives in Electron's main process because that is the only place
that can register a system-wide shortcut that fires while Spotify or Chrome
is focused. Keeping capture in Python would mean the hotkey firing in
Electron, crossing a process boundary to tell Python to start recording,
then crossing back - two processes contending for the same microphone, with
the recording start latency of an IPC round trip. Capturing where the
trigger already is removes the entire problem.

**Why not MediaRecorder - this is a hard constraint, not a preference:**
`MediaRecorder` in Chromium emits WebM/Opus. The backend's
`speech_recognition` library reads WAV, AIFF and FLAC PCM only; it cannot
decode Opus at all, and handing it a WebM blob fails outright. The options
were (a) add an ffmpeg binary to the backend purely to transcode, or (b)
take raw PCM straight off the Web Audio graph, which is *already* the shape
`sr.AudioData` wants. (b) needs no new dependency and makes the Electron
path hand `transcribe_audio` the same kind of object the Python
`sounddevice` path did.

**Why AudioWorklet over ScriptProcessorNode:** `ScriptProcessorNode` has
been deprecated for years and runs on the main thread, where it glitches
under load. The worklet is loaded from an **inline Blob URL** rather than a
file, because a file-backed `audioWorklet.addModule()` has to satisfy
Electron's CSP and `file://` resolution; a Blob sidesteps both and behaves
identically in dev and packaged builds.

**No resampling anywhere - deliberately.** Capture happens at the device's
native rate (48 kHz here) and that rate travels with the audio; Google's
endpoint accepts anything at or above 8 kHz, so nothing resamples on either
side and what reaches transcription is bit-identical to what the microphone
produced. Downsampling to 16 kHz would have cut the payload 3x, but the
payload is crossing loopback, so there was no reason to introduce a
resampling step that could be blamed for a bad transcript later.

**One measured consequence:** `websockets` defaults `max_size` to 1 MiB.
Mono int16 at 48 kHz is ~94 KB/s raw and ~125 KB/s base64-encoded, so any
clip past roughly 8 seconds would have been rejected outright as too large.
Measured before it could bite, and `max_size` is raised to 64 MiB with the
arithmetic recorded in the source.

**Verified for real:** the format contract was proven *before* any Electron
code was written - synthesized int16 PCM through `sr.AudioData` produced
valid FLAC (`fLaC` magic, bundled `flac-mac` binary works on this arch), and
Google accepted the request, returning `UnknownValueError` (well-formed, no
speech) rather than a `RequestError` (rejected). Then proven again with real
hardware: a live capture reported `48000 Hz, 185600 samples (~3.9s), peak
15549/32767` in the renderer and arrived at the backend as `371200 bytes`
(185600 x 2, byte-exact through base64 and the socket), where Google
processed it and correctly found no speech in ambient room noise.

**What we didn't do:** No voice-activity detection, no silence trimming, no
streaming/partial transcription. Capture is whole-clip, push-to-talk,
transcribe-once.

---

## The hotkey is a toggle because Electron cannot express hold-to-talk

**Decision:** `Cmd+Shift+Space` toggles recording - press to start, press
again to stop - rather than recording only while the key is held.

**Why:** Not a preference. Electron's `globalShortcut` fires on key-*down*
only and exposes no key-up event, so "record while held" is not expressible
without a native input-monitoring module and the extra permissions that
implies. A toggle is what the API can actually guarantee.

The main process keeps an `isRecording` flag to decide which edge each press
is, but the renderer is authoritative and reports its real state back over
`jarvis:recording-state` - so if capture fails to start, the toggle resyncs
and the next press is still the correct edge rather than being inverted for
the rest of the session.

**Two real environment findings, both caught by running it rather than
assuming:**
- **`ELECTRON_RUN_AS_NODE=1` is set in this development shell.** That
  variable makes the Electron binary run as plain Node - no `app`, no
  `BrowserWindow`, no window at all, and `require("electron")` returns the
  binary's *path string* instead of the API. It presents as
  `TypeError: Cannot read properties of undefined (reading 'whenReady')`,
  which reads exactly like a broken import. The `dev:electron` script now
  strips it with `env -u`. Named ESM imports from `"electron"` were verified
  working once it was unset - the import style was never the problem.
- **Renderer console output does not reach the terminal by default.** A
  failed module import in the renderer silently leaves the *previous* build
  running with no visible cause - observed directly, and it looked like the
  hotkey handler had stopped working. `main.js` now mirrors
  `webContents.on("console-message")` into the terminal.

---

## UI polish is deliberately deferred to a separate tool

**Decision:** This pass builds functional structure only - a plain state
machine, unstyled controls, no visual design. Styling is a later, separate
pass.

**Why:** The two jobs have different failure modes and different feedback
loops. Wiring is verified by observing real behavior (did the hotkey fire,
did the PCM arrive intact, did the gate actually withhold execution);
styling is verified by looking at it. Interleaving them means every visual
tweak risks disturbing wiring that was already proven, and every wiring fix
invalidates a visual judgment. Doing structure first also means the styling
pass has something real to style rather than mockups.

**What the styling pass can rely on not changing:** the state set (`idle`,
`listening`, `thinking`, `doing`, `approving`, `done`), the fields `App.jsx`
holds (`transcript`, `plan`, `activity`, `pendingApproval`, `captureInfo`,
`error`, `connection`), and the `decide(approved)` callback behind the
approval buttons. All inline styles live in a single `S` object at the
bottom of `App.jsx`, deliberately separate from the component body, so
restyling should not require touching any wiring above it.

**What we didn't do:** No mouse-passthrough behavior on the transparent
window (a polish detail, and easy to get subtly wrong in a way that makes
the window uninteractable). No animations, no layout work, no design system.

---

## The Reminders command created the reminder twice - a Planner granularity bug

**Bug:** "set a reminder to call mom tomorrow at 5pm" produced two identical
entries in Reminders.app, every run. Confirmed against real state during
the Electron approval-gate testing: one command, two "buy milk" reminders.

**Root cause - in the Planner, not the tool.** The Planner was splitting a
reminder task into separate milestones - typically "the reminder details
are entered" (milestone 2) and "the reminder is saved and scheduled"
(milestone 3, marked `requires_approval=true`). The Action agent's
instruction maps any milestone about "a reminder existing/being created" to
`create_reminder`, so *both* of those milestones triggered a
`create_reminder` call, and `create_reminder` is not idempotent - it
unconditionally `make new reminder`s. Two milestones, two AppleScript
`make new reminder` statements, two entries.

Two things in the old instruction fed this:
1. The general rule "give a task's final consequential action its own
   separate milestone" - written for the Kayak case, where "fill the search
   form" and "submit the search" genuinely are different states of the
   world - was being applied to reminders, where they are not.
2. Nothing told the Planner that some actions are atomic. A web form has a
   real inspectable state between "filled" and "submitted"; a reminder does
   not. `create_reminder` builds and commits the reminder in one
   `osascript` call - there is no draft.

**Fix - address both at the source:**
- Added a **milestone-granularity rule**: one milestone per distinct real
  outcome; never split one atomic action into a "prepare" milestone plus a
  "commit" milestone. Creating a reminder is called out by name as a single
  milestone. Splitting is explicitly reserved for genuine intermediate
  states someone could inspect or cancel (the filled-but-unsubmitted web
  form).
- Tightened the **`requires_approval`** definition: it now means an action
  that spends money, contacts other people, submits to an external website,
  or is genuinely hard to undo. Creating a routine local item (reminder,
  note, calendar event) is called out as explicitly *never* approval-worthy
  - local, private, trivially deleted. This removes the spurious approval
  gate the reminder command had been picking up as a side effect of rule 1.

**Why not a downstream fix.** The obvious patch - have the Action agent
skip `create_reminder` if a reminder was already created this session, or
have milestone 3's success check just confirm the milestone-2 reminder
exists - would leave the Planner still emitting a redundant milestone and
still mislabeling reminder creation as consequential. Every future
reminder-like command (a note, a calendar event) would hit the same bug
and need the same patch. Fixing the Planner's model of what a milestone is
fixes the whole class.

**Verified for real - 5 consecutive runs, each queried against
Reminders.app directly (not the tool's own success report):**
- Runs 1-3: plan = 1 milestone ("A reminder to call mom tomorrow at 5pm
  exists in the Reminders app"), `requires_approval=false`, **exactly 1
  entry created** each time.
- Runs 4-5 (after a rate-limit cooldown): same, **1 entry** each.
- Kayak regression check: still 3 milestones, step 3 ("the flight search is
  submitted") still `requires_approval=true`. The tightened approval rule
  did not weaken the gate on the command that actually needs it.

The Planner also dropped the separate "open Reminders" milestone on its own
- `create_reminder` drives AppleScript directly and doesn't need the app
foregrounded, and "keep the plan minimal" plus the new granularity rule
collapsed the whole command to one milestone. Fine, and arguably optimal.

**Housekeeping done at the same time:** the ~19 accumulated test reminders
("Call mom" x N and assorted) in the "Jarvis Test" list from prior sessions
were deleted. List is empty going into demo recording.

---

## Overlay window couldn't be moved or minimized - frameless windows need explicit drag regions

**Bug:** the Electron overlay could not be dragged or minimized, and had no
close button. It once ended up off-screen this session and had to be
recovered by restarting the whole dev process.

**Root cause.** `frame: false` draws no OS titlebar - no drag handle, no
traffic-light buttons - and none of that comes back automatically. It isn't
that `movable`/`minimizable` were set to `false` (they weren't; Electron
defaults both to `true`); a frameless window still needs (a) a page region
explicitly marked `-webkit-app-region: drag` before the OS will treat a
mousedown there as "move the window" instead of "click the page," and (b) an
actual affordance - a button or shortcut - to call `win.minimize()`/
`win.close()`, since there's no OS-drawn control to click. Both were simply
missing. Compounding it: the window had no explicit spawn position and
nothing remembered where the user last put it, so a drag (once possible)
wouldn't even stick across relaunches.

**Fix, three parts, all in service of "the user can always get the window
back":**
1. **Drag region** - `App.jsx` gained a titlebar strip (`S.titlebar`,
   `-webkit-app-region: drag`) with a label and two buttons. The buttons sit
   in a child marked `-webkit-app-region: no-drag`, or clicking them would
   start a window-drag before the click ever reached the button.
   `index.html` also needed a small structural reset (`margin/padding: 0`,
   `height: 100%` on `html`/`body`/`#root`) - without it the default 8px
   body margin left a dead band at the window's actual edge that belonged
   to no element, so a drag started right at the edge (the natural place to
   grab a window) would silently miss the drag region entirely. This is a
   layout correction, not a style choice.
2. **Minimize/close** - `preload.cjs` exposes `minimize()`/`closeWindow()`,
   sending `jarvis:minimize`/`jarvis:close` over IPC to two new
   `ipcMain.on` handlers in `main.js` that call `win.minimize()`/
   `win.close()`. A button, not a shortcut: the window is frameless by
   design, so a visible, always-there control fit the existing pattern
   better than one more shortcut to remember, and it's what the titlebar
   strip needed anyway once it existed for dragging.
3. **Position, chosen deliberately over "remember size too" or "snap to
   grid"** - `main.js` writes `{x, y}` to a small JSON file under
   `app.getPath("userData")` on every real `moved` event (debounced 250ms)
   and again on `close`, as a last-write safety net. On launch: use the
   saved position only if it's still on some *currently connected* display
   (checked against `screen.getAllDisplays()`, not just the primary one -
   an external-monitor position shouldn't be rejected just because the
   external monitor happens to be the primary at save time and isn't at
   load time); otherwise fall back to a fixed default (top-right of the
   primary display's work area, 24px margin) rather than whatever the OS
   would otherwise pick. This directly targets the failure that started
   this entry - a bad or stale position degrades to a known-good spot
   instead of off-screen.

**Verified for real, all via the real code path (Quartz-synthesized HID
mouse events through `kCGHIDEventTap` - the same event pipeline a physical
mouse goes through, not DOM-level simulation, and OS-level `AXMinimized`/
window-position queries via System Events, not application-reported
state):**
- **Drag**: a synthesized mousedown-drag-mouseup on the titlebar strip
  moved the real window by exactly the drag delta (`(1306,85)->(1050,400)`,
  window went `(1226,63)->(970,378)` - a `(-256,+315)` move both times).
- **Minimize**: a real click at the minimize button's computed screen
  position flipped `AXMinimized` `false -> true`; restoring (the same
  mechanism a Dock-icon click uses) brought it back at the same position.
- **Close**: a real click at the close button's position took the process
  from 1 window to 0.
- **Cross-launch persistence**: after the drag, a full process kill and
  fresh `npm run dev` opened the window at exactly `(970, 378)` - the
  dragged-to position, not the default.
- **Off-screen recovery - the actual original bug, reproduced and
  confirmed fixed**: the saved-position file was hand-poisoned to `(9999,
  9999)` (an impossible position, and the general shape of what caused this
  session's real loss); the next launch correctly rejected it and fell back
  to the default top-right position rather than spawning off-screen.

**What we didn't do:** No remembered window *size* (it's fixed and
non-resizable already). No multi-window state. No dock-icon-click test via
UI scripting - the dev-mode unbundled Electron binary doesn't keep a stable
Dock presence once windowless, which is a macOS packaging detail unrelated
to the actual fix; cross-launch persistence (the thing that matters) was
verified by a full process restart instead, which is the more deterministic
test anyway.
