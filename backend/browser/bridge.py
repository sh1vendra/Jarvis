"""Browser Bridge - connection state, action queue, and event-driven
signaling for the Chrome extension bridge.

Adapted from a reference architecture (see planning.md's "browser bridge"
entry for the full reasoning) rather than written from scratch, since the
hard lessons baked into this design - the perceive-act race in particular -
aren't obvious until you've been burned by them.

## Why "generation" is a raw timestamp, not a counter

The single most important, least obvious thing in this file: `generation`
is never incremented by this backend. It's `Date.now()` (a millisecond
timestamp), stamped by the content script when it builds a snapshot, and
forwarded here completely untouched - see register_snapshot below, which
just reads whatever generation the incoming PageSnapshot already carries.
"Generation" really means "timestamp of the DOM snapshot this data came
from." Every comparison against it is strict `>`, never `>=` or `==`,
because two snapshots built in the same millisecond would otherwise be
indistinguishable, and a >= check would let a caller "succeed" against a
snapshot it already had rather than a genuinely newer one.

## Why waiting is event-driven, not polling

wait_for_result and wait_for_snapshot both await an asyncio.Event instead
of looping on a fixed sleep - this is what actually solves the
perceive-act race: an action's caller doesn't get to decide "the DOM has
probably settled by now" via a guessed delay, it waits until a strictly
newer snapshot has genuinely arrived (or a timeout, as a last resort).

## The "set-then-swap" event pattern

register_snapshot does this on every new snapshot:

    self._snapshot_event.set()
    self._snapshot_event = asyncio.Event()

Any coroutine already blocked on `await old_event.wait()` holds a
reference to that *specific* Event object, so calling .set() on it wakes
them immediately. Immediately replacing self._snapshot_event with a fresh,
unset Event means any *new* waiter that calls wait_for_snapshot after this
point waits on the new object - it won't spuriously return instantly just
because the old event happened to still be in the "set" state. Without the
swap, the event would stay permanently set after the first snapshot and
every future wait would return immediately with no new snapshot having
actually arrived. The same pattern is used per-action-id in
record_action_result/wait_for_result, just keyed by action_id instead of
being a single shared event.
"""

import asyncio
import dataclasses
import json
import logging
import os
import secrets
import time
from typing import Dict, List, Optional

from .models import ActionRequest, ActionResult, DomChangeEvent, PageSnapshot
from .store import browser_store

logger = logging.getLogger(__name__)

# Opt-in investigation aid, off by default (no-op unless
# JARVIS_DEBUG_DUMP_SNAPSHOTS is set): dumps every real snapshot's full
# element list to disk, so investigating a real site's DOM (this is how
# Kayak's destination takeover panel and calendar structure were actually
# confirmed for Stage 3 - see planning.md) doesn't have to guess from
# find_web_element's own pass/fail messages alone. Zero cost and zero
# behavior change when unset, which is every normal run.
_DEBUG_DUMP_DIR = os.environ.get("JARVIS_DEBUG_DUMP_SNAPSHOTS")


def _debug_dump_snapshot(snapshot: PageSnapshot) -> None:
    if not _DEBUG_DUMP_DIR:
        return
    os.makedirs(_DEBUG_DUMP_DIR, exist_ok=True)
    path = os.path.join(_DEBUG_DUMP_DIR, f"snapshot_{snapshot.generation}.json")
    with open(path, "w") as f:
        json.dump(
            {
                "url": snapshot.url,
                "title": snapshot.title,
                "generation": snapshot.generation,
                "element_count": len(snapshot.elements),
                "elements": [dataclasses.asdict(e) for e in snapshot.elements],
            },
            f,
            indent=2,
        )


class BrowserBridge:
    # If no heartbeat/snapshot is received within this window (seconds),
    # consider the extension disconnected even though it never sent an
    # explicit disconnect - MV3 service workers can be killed by Chrome
    # without any chance to notify us first.
    STALE_THRESHOLD: float = 60.0

    def __init__(self):
        configured = os.environ.get("JARVIS_BROWSER_BRIDGE_TOKEN", "").strip()
        self._session_token = configured or "dev-bridge-token"
        self._connected_session_id: Optional[str] = None
        self._last_seen_at: float = 0.0
        self._pending_actions: List[ActionRequest] = []
        self._extension_name: str = ""
        self._latest_result_by_action_id: Dict[str, ActionResult] = {}
        self._latest_dom_change_by_action_id: Dict[str, DomChangeEvent] = {}
        # Event-driven signaling - callers await these instead of polling.
        self._result_events: Dict[str, asyncio.Event] = {}
        self._snapshot_event: asyncio.Event = asyncio.Event()
        self._snapshot_generation: int = 0
        self._extension_ws = None  # WebSocket to the extension, for server-push

    @property
    def session_token(self) -> str:
        return self._session_token

    def is_connected(self) -> bool:
        """True only if a session is registered AND we've heard from the
        extension within the staleness window."""
        if not self._connected_session_id:
            return False
        if self._last_seen_at and (time.time() - self._last_seen_at > self.STALE_THRESHOLD):
            self.disconnect()
            return False
        return True

    def connected_session_id(self) -> Optional[str]:
        return self._connected_session_id

    def last_seen_at(self) -> float:
        return self._last_seen_at

    def extension_name(self) -> str:
        return self._extension_name

    def authenticate(self, token: str) -> bool:
        return bool(token) and token == self._session_token

    def register_connection(self, session_id: str, extension_name: str = "") -> None:
        self._connected_session_id = session_id
        self._extension_name = extension_name
        self._last_seen_at = time.time()
        logger.info("browser bridge: extension connected (session=%s, name=%s)", session_id, extension_name)

    def set_extension_ws(self, ws) -> None:
        self._extension_ws = ws

    def clear_extension_ws(self) -> None:
        self._extension_ws = None

    def touch(self) -> None:
        self._last_seen_at = time.time()

    def register_snapshot(self, snapshot: PageSnapshot) -> None:
        """Stores the snapshot and wakes any coroutine waiting for a newer
        one. Note this always overwrites unconditionally - there's no
        check here that the incoming generation is actually larger than
        what we already had. Every new snapshot replaces the old one."""
        browser_store.upsert_snapshot(snapshot)
        self._connected_session_id = snapshot.session_id
        self._last_seen_at = time.time()
        self._snapshot_generation = getattr(snapshot, "generation", 0)
        logger.info(
            "browser bridge: snapshot registered (session=%s, generation=%s, elements=%d, url=%s)",
            snapshot.session_id,
            self._snapshot_generation,
            len(snapshot.elements),
            snapshot.url,
        )
        _debug_dump_snapshot(snapshot)
        self._snapshot_event.set()
        self._snapshot_event = asyncio.Event()  # set-then-swap - see module docstring

    def queue_action(self, request: ActionRequest) -> ActionResult:
        if not self.is_connected():
            return ActionResult(
                ok=False,
                message="Browser extension bridge is not connected.",
                action=request.action,
                ref_id=request.ref_id,
            )

        snapshot = browser_store.get_snapshot(request.session_id or self._connected_session_id)
        if not snapshot:
            # refresh_snapshot is the one action allowed to queue even
            # before any snapshot has arrived for this session yet -
            # everything else needs an existing snapshot to resolve
            # ref_id -> element metadata below.
            if request.action == "refresh_snapshot" and (request.session_id or self._connected_session_id):
                request.session_id = request.session_id or self._connected_session_id or ""
                request.action_id = request.action_id or f"act_{int(time.time() * 1000)}_{secrets.token_hex(4)}"
                self._result_events[request.action_id] = asyncio.Event()
                self._pending_actions.append(request)
                self._try_push_actions(request.session_id)
                return ActionResult(
                    ok=True,
                    message="refresh_snapshot queued (no snapshot yet).",
                    action=request.action,
                    ref_id=request.ref_id,
                    action_id=request.action_id,
                    session_id=request.session_id,
                    pre_generation=0,
                    post_generation=0,
                )
            return ActionResult(
                ok=False,
                message="No active browser snapshot available.",
                action=request.action,
                ref_id=request.ref_id,
            )

        request.session_id = request.session_id or snapshot.session_id
        request.action_id = request.action_id or f"act_{int(time.time() * 1000)}_{secrets.token_hex(4)}"
        self._result_events[request.action_id] = asyncio.Event()

        if request.ref_id:
            # Attach the element's fingerprint as metadata, captured now,
            # while we still have a snapshot that (probably) contains it.
            # This is exactly what content_script.js's tier-3 heuristic
            # matcher scores live candidates against if the tagged
            # data-agent-id element can't be found anymore by the time the
            # action actually executes.
            element = browser_store.get_element(request.ref_id, request.session_id)
            if element:
                request.metadata = {
                    **request.metadata,
                    "tab_id": snapshot.tab_id,
                    "generation": str(snapshot.generation),
                    "agent_id": str(element.agent_id) if element.agent_id else "",
                    "dom_path": element.dom_path,
                    "role": element.role,
                    "tag": element.tag,
                    "label": element.primary_label(),
                    "text": element.text,
                    "aria_label": element.aria_label,
                    "name": element.name,
                    "placeholder": element.placeholder,
                    "href": element.href,
                }

        self._pending_actions.append(request)
        self._try_push_actions(request.session_id)
        return ActionResult(
            ok=True,
            message="Action queued for browser extension execution.",
            action=request.action,
            ref_id=request.ref_id,
            action_id=request.action_id,
            session_id=request.session_id,
            pre_generation=snapshot.generation,
            post_generation=snapshot.generation,
        )

    def drain_actions(self, session_id: Optional[str] = None) -> List[ActionRequest]:
        """Removes and returns all pending actions for a session - called
        by the extension's poll path (browser_poll_actions)."""
        sid = session_id or self._connected_session_id
        if not sid:
            return []
        drained = [a for a in self._pending_actions if a.session_id == sid]
        self._pending_actions = [a for a in self._pending_actions if a.session_id != sid]
        return drained

    def pending_action_count(self, session_id: Optional[str] = None) -> int:
        sid = session_id or self._connected_session_id
        if not sid:
            return 0
        return sum(1 for action in self._pending_actions if action.session_id == sid)

    def _try_push_actions(self, session_id: str) -> None:
        """Pushes pending actions to the extension over the open
        WebSocket immediately, instead of waiting for the extension's next
        poll cycle.

        Deliberately-inherited known flaw (see planning.md): this drains
        the actions from _pending_actions as soon as the payload is built,
        *before* the send has actually completed (the send itself is
        fire-and-forget via loop.create_task). If ws.send() fails after
        this point, the action is already gone from the queue - the
        except below swallows the failure silently, "falling back to
        polling," but there's nothing left in the queue for a poll to
        find. This is a real gap, kept intentionally for fidelity to the
        reference architecture rather than fixed at this stage.
        """
        ws = self._extension_ws
        if ws is None:
            return
        actions = [a for a in self._pending_actions if a.session_id == session_id]
        if not actions:
            return
        try:
            import json

            payload = json.dumps(
                {
                    "type": "browser_actions",
                    "ok": True,
                    "session_id": session_id,
                    "actions": [a.__dict__ for a in actions],
                }
            )
            self._pending_actions = [a for a in self._pending_actions if a.session_id != session_id]
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(ws.send(payload))
        except Exception:
            pass  # falls back to polling - see docstring above for the real gap this leaves

    def record_action_result(self, result: ActionResult) -> None:
        if result.action_id:
            self._latest_result_by_action_id[result.action_id] = result
            evt = self._result_events.pop(result.action_id, None)
            if evt:
                evt.set()
        self._last_seen_at = time.time()

    def latest_action_result(self, action_id: str) -> Optional[ActionResult]:
        return self._latest_result_by_action_id.get(action_id)

    async def wait_for_result(self, action_id: str, timeout: float = 10.0) -> Optional[ActionResult]:
        """Awaits the action result instead of polling. Returns None on
        timeout with no result ever recorded."""
        existing = self._latest_result_by_action_id.get(action_id)
        if existing:
            self._result_events.pop(action_id, None)
            return existing
        evt = self._result_events.get(action_id)
        if not evt:
            return None
        try:
            await asyncio.wait_for(evt.wait(), timeout=max(0.05, timeout))
        except asyncio.TimeoutError:
            self._result_events.pop(action_id, None)
            return self._latest_result_by_action_id.get(action_id)
        self._result_events.pop(action_id, None)
        return self._latest_result_by_action_id.get(action_id)

    async def wait_for_snapshot(
        self, session_id: str = "", min_generation: int = 0, timeout: float = 2.0
    ) -> Optional[PageSnapshot]:
        """Awaits the next snapshot with generation strictly greater than
        min_generation - the actual mechanism that lets a caller confirm
        "the DOM changed after my action" instead of trusting a fixed
        sleep or the action's own self-reported success."""
        sid = session_id or self._connected_session_id
        current = browser_store.get_snapshot(sid)
        if current and current.generation > min_generation:
            return current
        deadline = time.time() + max(0.05, timeout)
        while time.time() < deadline:
            evt = self._snapshot_event
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(evt.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            current = browser_store.get_snapshot(sid)
            if current and current.generation > min_generation:
                return current
        return browser_store.get_snapshot(sid)

    def record_dom_change(self, event: DomChangeEvent) -> None:
        if event.action_id:
            self._latest_dom_change_by_action_id[event.action_id] = event
        self._last_seen_at = time.time()

    def latest_dom_change(self, action_id: str) -> Optional[DomChangeEvent]:
        return self._latest_dom_change_by_action_id.get(action_id)

    def disconnect(self) -> None:
        self._connected_session_id = None
        self._extension_name = ""
        logger.info("browser bridge: extension disconnected")

    def reset(self) -> None:
        self._connected_session_id = None
        self._last_seen_at = 0.0
        self._pending_actions.clear()
        self._latest_result_by_action_id.clear()
        self._latest_dom_change_by_action_id.clear()
        self._result_events.clear()
        self._snapshot_event = asyncio.Event()
        self._snapshot_generation = 0
        self._extension_name = ""


browser_bridge = BrowserBridge()
