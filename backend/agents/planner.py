"""Planner agent: turns a real task description into an ordered list of
outcome-based milestones (not fixed click-by-click actions)."""

from pydantic import BaseModel, Field
from google.adk.agents import LlmAgent


class Milestone(BaseModel):
    """A single outcome-based checkpoint in a plan."""

    step_number: int = Field(description="1-indexed position of this milestone in the plan")
    goal: str = Field(description="The outcome/goal this milestone represents, not a specific action")
    success_signal: str = Field(description="An observable signal that tells us this milestone is complete")


class MilestonePlan(BaseModel):
    """The full ordered plan the Planner agent returns."""

    milestones: list[Milestone]


# The Planner is a leaf agent (no sub_agents) - it only ever receives a task
# description and returns a structured plan. output_schema forces Gemini's
# final reply to validate against MilestonePlan, so we can parse it directly
# instead of scraping free-text.
planner_agent = LlmAgent(
    name="planner_agent",
    model="gemini-flash-lite-latest",
    description="Breaks a real task into an ordered list of outcome-based milestones.",
    instruction=(
        "You are a planning agent. You receive a task description and break it "
        "into an ordered sequence of milestones.\n\n"
        "Each milestone must describe a GOAL or OUTCOME, never a fixed low-level "
        "action sequence. For example, for 'open Spotify and play some lo-fi "
        "music', a milestone should look like 'Spotify is open and in the "
        "foreground', NOT 'click the Spotify icon at position X,Y'.\n\n"
        "Each milestone needs:\n"
        "- step_number: its 1-indexed position in the plan\n"
        "- goal: the outcome to reach\n"
        "- success_signal: an observable signal that tells us the goal was reached\n\n"
        "Keep the plan minimal - only the milestones actually needed to "
        "accomplish the task, in the order they must happen."
    ),
    output_schema=MilestonePlan,
)
