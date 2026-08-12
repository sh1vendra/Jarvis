"""Action agent: takes a single milestone goal (outcome-based, from the
Planner) and actually executes it using real tools - starting with
create_reminder for Reminders.app.
"""

from google.adk.agents import LlmAgent

from tools.mac_control import create_reminder_tool, open_app_tool
from .verifier_callbacks import log_tool_result_callback

action_agent = LlmAgent(
    name="action_agent",
    model="gemini-flash-lite-latest",
    description="Executes a single milestone goal using real Mac control tools.",
    instruction=(
        "You are given one milestone goal describing an outcome that needs "
        "to happen on the user's Mac.\n\n"
        "Decide which tool (if any) accomplishes that outcome:\n"
        "- open_app: use this when the milestone is about an application "
        "being launched/open/in the foreground (e.g. 'Spotify is open and "
        "in the foreground'). Extract just the app name, e.g. 'Spotify'.\n"
        "- create_reminder: use this when the milestone is about a reminder "
        "existing/being created (e.g. 'a reminder exists to call mom "
        "tomorrow at 5pm'). Extract:\n"
        "  - task: the actual reminder text (e.g. 'Call mom'), not the "
        "whole milestone sentence\n"
        "  - due_date: the date mentioned or implied (e.g. 'tomorrow', "
        "'2026-08-13') - pass it as plain text, don't compute it yourself\n"
        "  - due_time: the time mentioned or implied (e.g. '5pm', '17:00')\n\n"
        "If the milestone doesn't match any available tool (e.g. it "
        "describes clicking/typing inside an app we have no tool for yet), "
        "don't call anything - just say plainly what you would do if you "
        "had the right tool, and that it wasn't executed.\n\n"
        "After calling a tool, report back in one short sentence whether it "
        "actually succeeded, based on the tool's own result - don't claim "
        "success if the tool reported failure."
    ),
    tools=[open_app_tool, create_reminder_tool],
    after_tool_callback=log_tool_result_callback,
)
