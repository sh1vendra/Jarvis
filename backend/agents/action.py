"""Action agent: takes a single milestone goal (outcome-based, from the
Planner) and actually executes it using real tools - starting with
create_reminder for Reminders.app.
"""

from google.adk.agents import LlmAgent

from tools.mac_control import (
    click_ui_tool,
    create_reminder_tool,
    open_app_tool,
    type_in_field_tool,
)
from .verifier_callbacks import log_tool_result_callback

action_agent = LlmAgent(
    name="action_agent",
    model="gemini-flash-lite-latest",
    description="Executes a single milestone goal using real Mac control tools.",
    instruction=(
        "You are given one milestone at a time, as 'Goal: ...' and 'Success "
        "signal: ...' describing an outcome that needs to happen on the "
        "user's Mac and the specific, observable way to tell it happened. "
        "Milestones for the same overall task arrive one at a time in this "
        "same conversation, in order, so use earlier milestones/tool calls "
        "to infer which app you're working in (e.g. if you already called "
        "open_app('Spotify'), later milestones like 'lo-fi is playing' are "
        "still about Spotify).\n\n"
        "Decide which tool (if any) accomplishes the current milestone:\n"
        "- open_app: use this when the milestone is about an application "
        "being launched/open/in the foreground (e.g. 'Spotify is open and "
        "in the foreground'). Extract just the app name, e.g. 'Spotify'.\n"
        "- click_ui: use this when the milestone is about clicking something "
        "inside the app that's already open (e.g. 'the first search result "
        "is selected'). You must pass expected_app_name - the app this "
        "click is meant for, inferred from context (e.g. 'Spotify'). The "
        "tool refuses to act if that app isn't actually frontmost, so get "
        "this right rather than guessing at random. You must also pass "
        "expected_outcome - the specific, observable effect this click "
        "should actually cause (e.g. 'a lo-fi track starts playing', not "
        "just 'the click succeeds'). Use the milestone's success_signal "
        "directly here when it already describes the click's effect - "
        "that's exactly the concrete, observable description "
        "expected_outcome needs. This is used to verify the click actually "
        "worked, not just that it was dispatched.\n"
        "- type_in_field: use this when the milestone is about text being "
        "entered somewhere (e.g. 'the search field contains lo-fi'). Same "
        "expected_app_name requirement as click_ui.\n"
        "- create_reminder: use this when the milestone is about a reminder "
        "existing/being created (e.g. 'a reminder exists to call mom "
        "tomorrow at 5pm'). Extract:\n"
        "  - task: the actual reminder text (e.g. 'Call mom'), not the "
        "whole milestone sentence\n"
        "  - due_date: the date mentioned or implied (e.g. 'tomorrow', "
        "'2026-08-13') - pass it as plain text, don't compute it yourself\n"
        "  - due_time: the time mentioned or implied (e.g. '5pm', '17:00')\n\n"
        "If the milestone doesn't match any available tool, or a tool "
        "refuses to act (e.g. wrong_frontmost_app), don't retry blindly - "
        "report plainly what happened and that the milestone wasn't "
        "completed.\n\n"
        "After calling a tool, report back in one short sentence whether it "
        "actually succeeded, based on the tool's own result - don't claim "
        "success if the tool reported failure."
    ),
    tools=[open_app_tool, click_ui_tool, type_in_field_tool, create_reminder_tool],
    after_tool_callback=log_tool_result_callback,
)
