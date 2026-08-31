"""Planner agent: turns a real task description into an ordered list of
outcome-based milestones (not fixed click-by-click actions)."""

from pydantic import BaseModel, Field
from google.adk.agents import LlmAgent


class Milestone(BaseModel):
    """A single outcome-based checkpoint in a plan."""

    step_number: int = Field(description="1-indexed position of this milestone in the plan")
    goal: str = Field(description="The outcome/goal this milestone represents, not a specific action")
    success_signal: str = Field(description="An observable signal that tells us this milestone is complete")
    requires_approval: bool = Field(
        default=False,
        description=(
            "True only if completing this milestone performs a final, hard-to-reverse, or "
            "consequential real-world action (e.g. submitting a search, completing a purchase, "
            "sending a message). False for every other milestone."
        ),
    )


class MilestonePlan(BaseModel):
    """The full ordered plan the Planner agent returns."""

    milestones: list[Milestone]


# The Planner is a leaf agent (no sub_agents) — it only ever receives a task
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
        "If the task acts on a WEBSITE (Kayak, Google Flights, any web app), the "
        "FIRST milestone must be that the browser is open and showing that site, "
        "naming the site's address - e.g. 'Google Chrome is open with "
        "www.kayak.com loaded'. Never assume the user already has the browser "
        "open or on the right page. Milestones about page elements (typing in a "
        "field, clicking a button) come only after that first milestone.\n\n"
        "Each milestone needs:\n"
        "- step_number: its 1-indexed position in the plan\n"
        "- goal: the outcome to reach\n"
        "- success_signal: an observable signal that tells us the goal was reached\n"
        "- requires_approval: true ONLY if reaching this milestone performs a final, "
        "hard-to-reverse, or consequential real-world action - e.g. submitting a search, "
        "completing a purchase, sending a message. False for every other milestone "
        "(opening an app, filling in a field, navigating, reading information are never "
        "hard-to-reverse). If a task's last step is this kind of action, give it its own "
        "separate final milestone - don't fold it into an earlier one - and mark only that "
        "milestone requires_approval=true, so execution can pause there for approval before "
        "it actually runs.\n\n"
        "Keep the plan minimal — only the milestones actually needed to "
        "accomplish the task, in the order they must happen."
    ),
    output_schema=MilestonePlan,
)
