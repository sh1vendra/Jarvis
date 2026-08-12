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

from .planner import planner_agent

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
        "2. A REAL TASK - anything that requires taking action on the user's "
        "computer or planning multiple steps (e.g. 'open Spotify and play "
        "some lo-fi music'). For these, transfer to the planner_agent so it "
        "can break the task into milestones. Do not try to plan it yourself."
    ),
    sub_agents=[planner_agent],
)
