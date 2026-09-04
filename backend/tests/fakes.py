"""Lightweight, duck-typed fakes for ADK event objects.

Not real `google.adk`/`google.genai` classes - just plain objects carrying
exactly the attributes `main.run_action`/`main.run_command` actually read
off an event (`event.content.parts`, `part.function_call`,
`part.function_response`, `part.text`, `event.author`). Building real ADK
`Event`/`Content` objects here would pull in the full event-construction
machinery for no real benefit: `run_action` never isinstance-checks these,
it only reads attributes, and `getattr(part, "function_call", None)` /
direct `part.text` access work identically against a `SimpleNamespace`.

This is the deliberate boundary for the "verification logic" unit tests
(see tests/unit/test_run_action.py and test_run_plan.py): mock at the
`runner.run_async()` seam, not deeper. Everything downstream of that seam
- the honest bool derivation in `run_action`, the failed/cancelled state
propagation in `agent_server._run_plan` - is this project's own code, and
is exercised for real, unmocked. Everything upstream of that seam (an
actual Gemini call deciding which tool to invoke) is exactly what the
integration tests exist for instead.
"""

from types import SimpleNamespace


def fake_function_call(name: str, args: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, args=args or {})


def fake_function_response(name: str, response: dict) -> SimpleNamespace:
    return SimpleNamespace(name=name, response=response)


def fake_part(*, function_call=None, function_response=None, text: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(function_call=function_call, function_response=function_response, text=text)


def fake_event(author: str, parts: list) -> SimpleNamespace:
    return SimpleNamespace(author=author, content=SimpleNamespace(parts=parts))


def tool_call_event(author: str, tool_name: str, args: dict | None = None) -> SimpleNamespace:
    """One event carrying a single function_call part - the shape a real
    ADK event has right when the Action agent decides to invoke a tool."""
    return fake_event(author, [fake_part(function_call=fake_function_call(tool_name, args))])


def tool_result_event(author: str, tool_name: str, response: dict) -> SimpleNamespace:
    """One event carrying a single function_response part - the shape a
    real ADK event has right when a tool's own dict return value comes
    back. `response` should be the tool's actual return dict, e.g.
    {"success": True, "message": "...", "error": None} - run_action reads
    `response["success"]` exactly like the real pipeline does."""
    return fake_event(author, [fake_part(function_response=fake_function_response(tool_name, response))])


def text_event(author: str, text: str) -> SimpleNamespace:
    """One event carrying the agent's own reply text - the shape a real
    ADK event has for the Action agent's final "here's what I did" line."""
    return fake_event(author, [fake_part(text=text)])


class FakeRunner:
    """Stands in for `InMemoryRunner`. `run_action`/`run_command` only ever
    call `runner.run_async(...)` and async-iterate what it returns - this
    replays a pre-scripted event sequence instead of making a real call."""

    def __init__(self, events: list):
        self._events = events

    async def run_async(self, **_kwargs):
        for event in self._events:
            yield event


class RecordingSession:
    """A ClientSession stand-in that records every payload sent instead of
    touching a real WebSocket - lets a test assert exactly what the UI
    would have been told, in order, without a real connection."""

    def __init__(self, approval_answers: list[bool] | None = None):
        self.sent: list[dict] = []
        self._approval_answers = list(approval_answers or [])

    async def send(self, payload: dict) -> None:
        self.sent.append(payload)

    async def await_approval(self) -> bool:
        # Real ClientSession.await_approval() returns an awaitable Future
        # resolved later by a client message; here the test already knows
        # the answer up front, so this just returns it directly - `await`
        # on a plain bool works fine since this method is itself async.
        if not self._approval_answers:
            raise AssertionError("await_approval() called more times than the test scripted answers for")
        return self._approval_answers.pop(0)

    def sent_types(self) -> list[str]:
        return [p.get("type") for p in self.sent]
