"""Action agent: takes a single milestone goal (outcome-based, from the
Planner) and actually executes it using real tools - starting with
create_reminder for Reminders.app.
"""

from google.adk.agents import LlmAgent

from tools.mac_control import create_reminder_tool
from .verifier_callbacks import log_tool_result_callback

action_agent = LlmAgent(
    name="action_agent",
    model="gemini-flash-latest",
    description="Executes a single milestone goal using real Mac control tools.",
    instruction=(
        "You are given one milestone goal describing an outcome that needs "
        "to happen on the user's Mac, e.g. 'a reminder exists to call mom "
        "tomorrow at 5pm'.\n\n"
        "Figure out which tool accomplishes that outcome and call it with "
        "the right arguments extracted from the goal text yourself:\n"
        "- task: the actual reminder text (e.g. 'Call mom'), not the whole "
        "milestone sentence\n"
        "- due_date: the date mentioned or implied (e.g. 'tomorrow', "
        "'2026-08-13') - pass it as plain text, don't compute it yourself\n"
        "- due_time: the time mentioned or implied (e.g. '5pm', '17:00')\n\n"
        "After calling the tool, report back in one short sentence whether "
        "it actually succeeded, based on the tool's own result - don't "
        "claim success if the tool reported failure."
    ),
    tools=[create_reminder_tool],
    after_tool_callback=log_tool_result_callback,
)
