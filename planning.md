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
