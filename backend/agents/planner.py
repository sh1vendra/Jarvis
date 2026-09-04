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
        "MILESTONE GRANULARITY: one milestone per distinct real outcome. Do NOT "
        "split a single atomic action into a 'prepare' milestone and a separate "
        "'commit' milestone. Creating a reminder is ONE atomic action - so 'a "
        "reminder to call mom tomorrow at 5pm exists in the Reminders app' is a "
        "SINGLE milestone. Playing a specific song in Spotify is likewise ONE "
        "milestone - 'Billie Jean by Michael Jackson is playing in Spotify' - "
        "never a separate 'Spotify is open' step before it (the tool that plays "
        "a track launches Spotify itself). Never emit 'the reminder details are "
        "entered' followed by a separate 'the reminder is saved': there is no real in-between state "
        "for a reminder, and two such milestones make the system create the "
        "reminder twice. The exact same rule applies to a calendar event, a "
        "note, and - MOST IMPORTANTLY, for safety, not just tidiness - a text "
        "message: 'a text saying X is sent to +1...' is ONE milestone, never "
        "'the message is composed/drafted/ready to send' followed by 'the "
        "message is sent'. There is no real inspectable draft state for any "
        "of these tools (each is one atomic AppleScript action, exactly like "
        "the reminder case) - and for messages specifically, splitting it "
        "this way is a real safety bug, not just redundant: it produces an "
        "ungated first milestone whose execution actually sends the message "
        "for real, with the requires_approval gate only ever reached on the "
        "now-pointless second milestone, after the real send already "
        "happened with no real approval in the loop at all. Only separate "
        "preparing from committing when there is a "
        "genuine intermediate state someone could inspect or cancel - for example "
        "a web search form that has been filled in but not yet submitted, where "
        "'the destination is entered' and 'the search is submitted' really are two "
        "different states of the world.\n\n"
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
        "- requires_approval: true ONLY for a milestone whose action spends money, "
        "sends something to other people, submits a form or search to an external "
        "website, or is otherwise genuinely hard to undo - e.g. submitting a flight "
        "search, completing a purchase, sending a message or email. Opening an app, "
        "navigating, typing into a field, reading information, and creating a "
        "routine personal item (a reminder, a note, a calendar event) are NEVER in "
        "this category: they are local, private, and trivially reversible, so their "
        "milestones are always requires_approval=false. When a task's final step IS "
        "a consequential action of this kind, give it its own separate final "
        "milestone - don't fold it into an earlier one - and mark only that "
        "milestone requires_approval=true, so execution can pause there for approval "
        "before it actually runs.\n\n"
        "If the task description is followed by a '[Known user preferences ...]' "
        "block, treat those as facts the user has told Jarvis before. Use a "
        "preference only to fill in a detail the command itself left unspecified - "
        "e.g. the command says 'search for a flight' with no destination and a "
        "preference gives a default flight city, so the plan uses that city. Never "
        "let a preference override something the command stated explicitly, and "
        "ignore any preference not relevant to this command.\n\n"
        "Keep the plan minimal — only the milestones actually needed to "
        "accomplish the task, in the order they must happen."
    ),
    output_schema=MilestonePlan,
)
