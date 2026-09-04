# Jarvis backend test suite

Real, runnable pytest coverage of this project's actual verification
philosophy - not generic smoke tests. If Jarvis's whole build has one
recurring theme, it's "don't trust that something worked just because
nothing raised" (see the repo root's `planning.md`); this suite is that
theme, made into automated tests.

## Running it

```bash
cd backend
pip install -r requirements.txt -r requirements-dev.txt   # once
make test              # fast suite: ~85 tests, ~1.5s, no real Mac state, no network
make test-integration   # real suite: touches Reminders, Spotify, the real mic/speakers, one real Gemini call
make test-all            # both together
```

No Makefile needed - the equivalent `pytest` commands:

```bash
pytest tests/unit -v                          # fast suite
pytest tests/integration -m integration -v     # real suite
pytest tests/unit tests/integration -m "" -v   # both
```

A bare `pytest` (no args) also just runs the fast suite - `pytest.ini` sets
`testpaths = tests/unit`, so the real, slower, network/Mac-dependent tests
are never collected by accident.

## Unit vs. integration - drawn on what's actually true, not by convenience

**`tests/unit/`** - fast (~1.5s for the whole thing), deterministic, no
real Mac state, no network, no LLM call. Every external boundary that
would otherwise make a test slow or flaky is mocked at a specific,
deliberate seam - documented in each file, not just done silently. The
seams used throughout:

- ADK event objects (`tests/fakes.py`) - `run_action`/`_run_plan` are
  tested against scripted event sequences shaped exactly like real ADK
  events, not real ones. This tests *this project's own logic* on top of
  ADK (the honest bool derivation, the failed/cancelled state
  propagation), not ADK itself.
- `subprocess.run` for `capture_region_unverified`'s happy path, and
  `_real_window_bounds` for `capture_screenshot` - real coordinate math,
  fake OS call.
- A spy in place of `WakeWordListener` for `agent_server`'s dual-flag
  pause logic - tests the *decision* (when does it pause/resume), not the
  real audio pipeline underneath.
- An isolated tmp SQLite file (`JARVIS_MEMORY_DB` + a module reload) for
  every `memory/store.py` test - real SQLite, real file I/O, just never
  the real `jarvis_memory.db`.

**`tests/integration/`** - real, slower, actually touches external state.
Marked `@pytest.mark.integration` and never collected by a bare `pytest`.
Four real things get tested this way, each for a concrete reason a unit
test genuinely couldn't cover:

| Test | What's real | Why it can't be a unit test |
|---|---|---|
| `test_planner_reminder_regression.py` | A real Gemini call through the real Orchestrator/Planner | The bug this guards against was a **prompt** regression, not a code one - mocking the LLM's response would only prove the mock returns what it was told to, never whether the prompt still says the right thing. |
| `test_reminders_real.py` | The real macOS Reminders app, read back independently via AppleScript | `create_reminder`'s `success: true` is only meaningful if something *outside* the function under test confirms it - mocking `osascript` would just be trusting the code to grade its own homework. |
| `test_capture_screenshot_real.py` | The real window server (`CGWindowListCopyWindowInfo`), a real `screencapture` call | The whole point of this function is a real safety contract against real on-screen state; faking the window list would test nothing about whether that contract actually holds. |
| `test_wakeword_real_detection.py` | The real microphone, the real openWakeWord model, real audio through the real speakers | Same reasoning as the Planner test - the thing being verified (does "Hey Jarvis" actually get recognized) *is* the real audio pipeline; mocking it convincingly would mean faking the one fact the test exists to check. |

**Real preconditions these need**, found by the tests themselves failing
honestly rather than assumed up front:

- `test_reminders_real.py` needs this process to already have Automation
  access to Reminders (System Settings -> Privacy & Security ->
  Automation) - same one-time grant the app itself needs.
- `test_wakeword_real_detection.py` needs system output genuinely
  unmuted and audible - checked explicitly at the top of each test
  (`_require_unmuted_output`), skipping with the real reason rather than
  failing confusingly if it isn't. Found this precondition for real: on
  this development machine, something outside this project intermittently
  mutes/zeros system output during a session, and a `say`/`afplay` call
  into a muted output reaches the real microphone with nothing to hear -
  indistinguishable from a real detection bug unless checked for
  explicitly.
- `test_planner_reminder_regression.py` needs `GOOGLE_API_KEY` (already
  required for Jarvis itself, loaded from the repo-root `.env`) and
  network access, and costs a small, real amount of Gemini usage per run.

## What's covered, and why these specifically

Prioritized by what's actually meaningful to this project, not by what's
easiest to reach:

1. **Verification logic itself** (`test_run_action.py`, `test_run_plan.py`,
   `test_click_verification.py`) - the core philosophy. `run_action`'s
   honest bool (last tool's real `success` field, never the agent's own
   summary, never "nothing raised"), `_run_plan`'s honest failed/cancelled
   propagation, and `click_ui`'s real outcome-verification decisions
   (state-check vs. pixel-diff, the real Free-tier-ad false-positive fix).
2. **The Reminders duplicate-milestone regression**
   (`test_planner_reminder_regression.py`) - a real bug, a real fix (a
   Planner prompt change), a real regression test.
3. **Memory** (`test_memory_store.py`) - `command_history` reflecting real
   success/failure (not the old "didn't crash" bug), a stored preference
   surfacing when relevant and *not* leaking into an unrelated command
   (the control case, formalized), persistence across a simulated process
   restart.
4. **The approval gate** (`test_run_plan.py`) - a `requires_approval`
   milestone genuinely never runs before a real decision arrives;
   rejection produces `cancelled`, not silent failure or execution.
5. **Spotify ambiguity detection** (`test_spotify_ambiguity.py`) - the
   deterministic same-title/different-artist check, tested against the
   real candidate data this project actually read back from Spotify
   during its own investigation ("Mad World" vs. "Bohemian Rhapsody").
6. **The `capture_region_unverified` safety fix**
   (`test_capture_region_safety.py`) - the exact old call shape now raises
   before capturing anything; empty/whitespace `reason` is rejected; the
   safe path is unaffected.
7. **Wake-word/TTS mic-contention** (`test_wakeword_listener.py`,
   `test_mic_tts_dual_flag.py`, `test_wakeword_real_detection.py`) - the
   real regression class: clearing *one* of `mic_state`/`tts_state` while
   the other is still true must not resume the listener - only clearing
   *both* does.

Also: `test_speak_text.py` - the personality-flavor rule (confirmations
only, never errors/questions/failed/cancelled) as real, exact logic, not
just something checked by ear.
