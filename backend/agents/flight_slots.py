"""Flight-slot extraction agent: the LLM half of the clarification loop's
"is this genuinely ambiguous" decision - and only the extraction half.

This agent's one job is to report which flight-search slots (origin,
destination, dates, trip type) a request already states - never to decide
whether the request is complete enough to act on. That decision is
deterministic Python (see main.py's `_resolve_flight_slots`), checked
against what's actually stated here and then against stored preferences
(memory/store.py) - not an LLM judgment call, per the project's standing
"don't trust the model's own judgment when a real check is possible" rule
(see planning.md).
"""

from typing import Literal

from pydantic import BaseModel, Field
from google.adk.agents import LlmAgent


class FlightSlots(BaseModel):
    """Whichever of these a request actually states - anything not stated
    is left None, never guessed, invented, or defaulted by the model
    itself (that's main.py's job, against real stored preferences)."""

    destination: str | None = Field(default=None, description="The destination city/airport, if stated")
    origin: str | None = Field(default=None, description="The departure city/airport, if stated")
    depart_date: str | None = Field(
        default=None, description="The departure date, exactly as phrased, if stated - do not compute/normalize it"
    )
    return_date: str | None = Field(
        default=None, description="The return date, exactly as phrased, if stated and this is a round trip"
    )
    trip_type: Literal["one_way", "round_trip"] | None = Field(
        default=None,
        description=(
            "one_way or round_trip, only if stated or unambiguously implied (e.g. a return date is mentioned) "
            "- else null, never guessed"
        ),
    )


def build_flight_slot_extractor_agent() -> LlmAgent:
    """Constructs a fresh, independent instance - see planner.py's
    build_planner_agent() for exactly why this needs to be a factory
    rather than a single reused object: ADK permanently sets an agent's
    `.parent_agent` once it joins any `sub_agents` list (here,
    orchestrator_agent's), and a second, separate `InMemoryRunner` around
    the SAME already-parented object was confirmed, directly, to cross-talk
    with that tree instead of running in true isolation - main.py's
    re-extraction call (after a real clarifying answer) needs a genuinely
    fresh instance, not the module-level singleton below."""
    return LlmAgent(
        name="flight_slot_extractor_agent",
        model="gemini-flash-lite-latest",
        description=(
            "Extracts whichever flight-search slots (origin, destination, dates, trip type) a request already "
            "states - never invents, infers a 'likely' value, or decides whether anything is missing."
        ),
        instruction=(
            "You are given a real request about searching for or booking a flight - either the user's original "
            "request, or that same request combined with their answer to a follow-up question. Extract ONLY the "
            "slots the text actually states or unambiguously implies (a mentioned return date implies "
            "trip_type=round_trip). Do NOT guess a 'likely' city, invent a date, or assume one-way/round-trip "
            "when it wasn't actually said - if something genuinely isn't there, leave it null. This is pure "
            "extraction, not planning or judgment: you are not deciding whether the request is complete enough "
            "to act on, only reporting what is actually present in the text.\n\n"
            "Pass dates through as plain text exactly as phrased (e.g. 'next Friday', 'March 3rd', 'the 12th') - "
            "never compute, normalize, or resolve them yourself."
        ),
        output_schema=FlightSlots,
    )


# The Orchestrator's own sub-agent - see build_flight_slot_extractor_agent's
# docstring for why standalone re-invocation needs a fresh instance instead
# of reusing this one.
flight_slot_extractor_agent = build_flight_slot_extractor_agent()
