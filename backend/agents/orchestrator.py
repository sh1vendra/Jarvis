"""Orchestrator agent: the first thing every user command hits.

It decides whether the input is simple conversational chatter (answer it
directly) or a real task (hand off to the Planner agent to get a milestone
plan). The hand-off uses ADK's built-in agent transfer mechanism: any
LlmAgent that has sub_agents automatically gets a `transfer_to_agent` tool,
and the model calls that tool itself when it decides the Planner should take
over. We don't write any manual routing/if-else logic for this - the model
makes the call based on the instruction below.
"""

from google.adk.agents import LlmAgent

from .flight_slots import flight_slot_extractor_agent
from .planner import planner_agent

# The single source of truth for "what can Jarvis actually do right now" -
# read verbatim by the model for the CAPABILITY QUESTION branch below, so
# there is exactly one place this list lives, not a copy in the instruction
# string that could drift from this one. MUST be kept in sync BY HAND
# whenever a tool is added, removed, or genuinely changes scope (see
# action.py's tools=[...] list, the real source of truth for what's
# actually wired in) - nothing enforces that automatically, and letting
# this go stale is exactly the "aspirational, not real" failure mode this
# feature exists to avoid. Deliberately excludes anything not actually
# built yet (e.g. the deferred conversational clarification/autonomous
# booking subsystem - see planning.md) and anything that's an
# implementation detail rather than a real user-facing capability (open_app,
# click_ui, type_in_field, find_web_element - these are how tasks below get
# done, not capabilities in their own right).
_CAPABILITIES_ANSWER = (
    "Here's what I can actually do right now: play a specific song or "
    "artist in Spotify - I'll ask if a search is ambiguous, like a cover "
    "versus the original; create reminders, calendar events, and notes; "
    "send a text message, though I'll always ask you to approve it first "
    "since it reaches a real person; and search flights on Kayak, which "
    "I'll also ask you to approve before submitting."
)

orchestrator_agent = LlmAgent(
    name="orchestrator_agent",
    model="gemini-flash-lite-latest",
    description="Front door for user commands. Answers simple chat directly, delegates real tasks to the planner.",
    instruction=(
        "You are the orchestrator for a voice assistant called Jarvis.\n\n"
        "For every incoming user command, decide which of these it is:\n\n"
        "1. SIMPLE CONVERSATIONAL INPUT - greetings, small talk, questions "
        "that don't require doing anything on the user's computer "
        "(e.g. 'hello', 'what's up', 'how are you'). Respond directly and "
        "briefly yourself. Do NOT transfer these.\n\n"
        "2. A CAPABILITY QUESTION - the user is asking what you can do "
        "(e.g. 'what can you do', 'what are you capable of', 'help', "
        "'what can I ask you'). Respond directly with exactly this, "
        "verbatim - you may adjust phrasing slightly to fit naturally as a "
        "spoken reply, but never add, remove, or embellish anything it "
        "doesn't already say, and never guess at a capability not listed "
        "here:\n"
        f'"{_CAPABILITIES_ANSWER}"\n'
        "Do NOT transfer these to the planner - this is a direct answer, "
        "not a task.\n\n"
        "3. A FLIGHT SEARCH OR BOOKING TASK - specifically about searching "
        "for, comparing, or booking a FLIGHT (e.g. 'book me a flight to New "
        "York', 'find flights to Denver next Friday'). For these, transfer "
        "to the flight_slot_extractor_agent - it decides which details are "
        "already given, not you. Do not ask any clarifying question "
        "yourself and do not transfer these to the planner_agent - that "
        "happens automatically once the real details are resolved.\n\n"
        "4. ANY OTHER REAL TASK - anything else that requires taking action "
        "on the user's computer or planning multiple steps (e.g. 'open "
        "Spotify and play some lo-fi music', 'create a reminder to call "
        "mom'). For these, transfer to the planner_agent so it can break "
        "the task into milestones. Do not try to plan it yourself."
    ),
    sub_agents=[planner_agent, flight_slot_extractor_agent],
)
