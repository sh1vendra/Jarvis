"""Browser Bridge Server - the Chrome extension's only counterparty.

A WebSocket server bound to a loopback port, separate from anything else
in this project. The extension connects here, authenticates with a shared
secret, and both directions of traffic flow through this one handler:
- extension -> backend: hello, page snapshots, action results, DOM-change events
- backend -> extension: queued actions, pushed as soon as they're queued
  (see browser.bridge.BrowserBridge._try_push_actions) rather than waiting
  for the extension's next poll

This file only talks to the extension over the wire; it does not decide
*what* actions to take - that's backend/tools/browser_tools.py, which
calls into the shared browser_bridge singleton (imported from
browser/bridge.py) to queue actions and await their results. Both this
server and any tool-calling code that imports browser.bridge see the same
BrowserBridge instance as long as they're running in the same Python
process and the same asyncio event loop - see planning.md's "browser
bridge" entry for why that constraint matters and how it's satisfied here
(the server is started as an in-process asyncio task alongside the agent
runner, not launched as a separate OS process, precisely so the
asyncio.Event objects bridge.py relies on are awaitable from both sides).

Protocol (JSON messages, one per line over the WebSocket):
- browser_bridge_hello    (extension -> backend, first message, must include token)
- browser_bridge_hello_ack (backend -> extension)
- browser_snapshot        (extension -> backend, a full PageSnapshot)
- browser_snapshot_ack    (backend -> extension)
- browser_poll_actions    (extension -> backend, "any actions queued for me?")
- browser_actions         (backend -> extension, pushed OR sent in reply to a poll)
- browser_action_result   (extension -> backend, the outcome of executing an action)
- browser_action_result_ack (backend -> extension)
- browser_dom_change      (extension -> backend, MutationObserver fired)
- browser_dom_change_ack  (backend -> extension)
- browser_ping / browser_pong (keepalive)
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict

import websockets

_backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from browser.bridge import browser_bridge  # noqa: E402
from browser.models import ActionResult, DomChangeEvent, ElementFingerprint, ElementRef, PageSnapshot, ViewportMeta  # noqa: E402

logger = logging.getLogger(__name__)

BRIDGE_HOST = os.environ.get("JARVIS_BROWSER_BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT = int(os.environ.get("JARVIS_BROWSER_BRIDGE_PORT", "8765"))


def _element_from_dict(data: Dict[str, Any], generation: int) -> ElementRef:
    fingerprint_data = data.get("fingerprint", {}) or {}
    fingerprint = ElementFingerprint(
        role=fingerprint_data.get("role", data.get("role", "")),
        text=fingerprint_data.get("text", data.get("text", "")),
        aria_label=fingerprint_data.get("aria_label", data.get("aria_label", "")),
        name=fingerprint_data.get("name", data.get("name", "")),
        placeholder=fingerprint_data.get("placeholder", data.get("placeholder", "")),
        href=fingerprint_data.get("href", data.get("href", "")),
        ancestor_labels=list(fingerprint_data.get("ancestor_labels", []) or []),
        frame_path=fingerprint_data.get("frame_path", data.get("frame_path", "main")),
        dom_path=fingerprint_data.get("dom_path", data.get("dom_path", "")),
        sibling_index=int(fingerprint_data.get("sibling_index", 0) or 0),
        stable_attributes=dict(fingerprint_data.get("stable_attributes", {}) or {}),
    )
    return ElementRef(
        ref_id=str(data.get("ref_id", "")),
        generation=int(data.get("generation", generation) or generation),
        agent_id=int(data.get("agent_id", 0) or 0),
        role=str(data.get("role", "")),
        tag=str(data.get("tag", "")),
        text=str(data.get("text", "")),
        aria_label=str(data.get("aria_label", "")),
        name=str(data.get("name", "")),
        placeholder=str(data.get("placeholder", "")),
        href=str(data.get("href", "")),
        value=str(data.get("value", "")),
        context_text=str(data.get("context_text", "")),
        frame_path=str(data.get("frame_path", "main")),
        dom_path=str(data.get("dom_path", "")),
        bounds=dict(data.get("bounds", {}) or {}),
        visible=bool(data.get("visible", True)),
        enabled=bool(data.get("enabled", True)),
        checked=bool(data.get("checked", False)),
        selected=bool(data.get("selected", False)),
        in_viewport=bool(data.get("in_viewport", True)),
        action_types=list(data.get("action_types", []) or []),
        fingerprint=fingerprint,
    )


def _snapshot_from_payload(payload: Dict[str, Any]) -> PageSnapshot:
    # generation flows straight through from whatever the content script
    # sent (Date.now() at snapshot-build time) - see bridge.py's module
    # docstring for why this is deliberate, not a bug.
    generation = int(payload.get("generation", 1) or 1)
    elements = [_element_from_dict(item, generation) for item in list(payload.get("elements", []) or [])]
    vp_raw = payload.get("viewport") or {}
    viewport = ViewportMeta(
        width=int(vp_raw.get("width", 0) or 0),
        height=int(vp_raw.get("height", 0) or 0),
        scroll_x=int(vp_raw.get("scrollX", vp_raw.get("scroll_x", 0)) or 0),
        scroll_y=int(vp_raw.get("scrollY", vp_raw.get("scroll_y", 0)) or 0),
        scroll_height=int(vp_raw.get("scrollHeight", vp_raw.get("scroll_height", 0)) or 0),
        page_height=int(vp_raw.get("pageHeight", vp_raw.get("page_height", 0)) or 0),
    )
    return PageSnapshot(
        session_id=str(payload.get("session_id", "browser-session")),
        tab_id=str(payload.get("tab_id", "tab-1")),
        url=str(payload.get("url", "")),
        title=str(payload.get("title", "")),
        generation=generation,
        frame_id=str(payload.get("frame_id", "main")),
        elements=elements,
        opaque_regions=list(payload.get("opaque_regions", []) or []),
        viewport=viewport,
    )


def _action_result_from_payload(payload: Dict[str, Any]) -> ActionResult:
    return ActionResult(
        ok=bool(payload.get("ok", False)),
        message=str(payload.get("message", "")),
        action=str(payload.get("action", "")),
        ref_id=str(payload.get("ref_id", "")),
        action_id=str(payload.get("action_id", "")),
        session_id=str(payload.get("session_id", "")),
        pre_generation=int(payload.get("pre_generation", 0) or 0),
        post_generation=int(payload.get("post_generation", 0) or 0),
        details=dict(payload.get("details", {}) or {}),
        error=dict(payload.get("error", {}) or {}),
        meta=dict(payload.get("meta", {}) or {}),
    )


async def _send(websocket, payload: Dict[str, Any]) -> None:
    await websocket.send(json.dumps(payload))


async def bridge_handler(websocket):
    logger.info("browser bridge server: client connected")
    authenticated = False
    session_id = ""

    try:
        async for raw in websocket:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await _send(websocket, {"type": "error", "message": "Invalid JSON payload"})
                continue

            msg_type = data.get("type")

            if msg_type == "browser_bridge_hello":
                token = str(data.get("token", ""))
                if not browser_bridge.authenticate(token):
                    await _send(
                        websocket,
                        {"type": "browser_bridge_hello_ack", "ok": False, "message": "Invalid bridge token"},
                    )
                    continue

                session_id = str(data.get("session_id", "browser-session"))
                extension_name = str(data.get("extension_name", "chrome-extension"))
                browser_bridge.register_connection(session_id=session_id, extension_name=extension_name)
                browser_bridge.set_extension_ws(websocket)
                authenticated = True
                await _send(
                    websocket,
                    {
                        "type": "browser_bridge_hello_ack",
                        "ok": True,
                        "session_id": session_id,
                        "extension_name": extension_name,
                        "server": "jarvis-browser-bridge",
                        "pending_actions": browser_bridge.pending_action_count(session_id),
                    },
                )
                continue

            if not authenticated:
                await _send(websocket, {"type": "error", "message": "Authenticate first with browser_bridge_hello"})
                continue

            browser_bridge.touch()

            if msg_type == "browser_ping":
                await _send(websocket, {"type": "browser_pong", "ok": True, "session_id": session_id})
                continue

            if msg_type == "browser_snapshot":
                snapshot = _snapshot_from_payload(data.get("snapshot", {}) or {})
                if not snapshot.session_id:
                    snapshot.session_id = session_id
                browser_bridge.register_snapshot(snapshot)
                await _send(
                    websocket,
                    {
                        "type": "browser_snapshot_ack",
                        "ok": True,
                        "session_id": snapshot.session_id,
                        "generation": snapshot.generation,
                        "elements": len(snapshot.elements),
                        "url": snapshot.url,
                    },
                )
                continue

            if msg_type == "browser_poll_actions":
                actions = browser_bridge.drain_actions(session_id)
                await _send(
                    websocket,
                    {"type": "browser_actions", "ok": True, "session_id": session_id, "actions": [a.__dict__ for a in actions]},
                )
                continue

            if msg_type == "browser_action_result":
                result = _action_result_from_payload(data.get("result", {}) or {})
                if not result.session_id:
                    result.session_id = session_id
                browser_bridge.record_action_result(result)
                await _send(
                    websocket,
                    {
                        "type": "browser_action_result_ack",
                        "ok": True,
                        "session_id": result.session_id,
                        "action_id": result.action_id,
                        "ref_id": result.ref_id,
                    },
                )
                continue

            if msg_type == "browser_dom_change":
                event_data = data.get("event", {}) or {}
                event = DomChangeEvent(
                    action_id=str(event_data.get("action_id", "")),
                    ref_id=str(event_data.get("ref_id", "")),
                    action_type=str(event_data.get("action_type", "")),
                    change_types=list(event_data.get("change_types", []) or []),
                    timestamp=float(event_data.get("timestamp", 0) or 0),
                    session_id=str(event_data.get("session_id", session_id)),
                    tab_id=str(event_data.get("tab_id", "")),
                )
                browser_bridge.record_dom_change(event)
                await _send(
                    websocket,
                    {"type": "browser_dom_change_ack", "ok": True, "action_id": event.action_id, "session_id": event.session_id},
                )
                continue

            await _send(websocket, {"type": "error", "message": f"Unknown message type: {msg_type}"})

    except websockets.exceptions.ConnectionClosed as exc:
        logger.info("browser bridge server: client disconnected (%s)", exc)
    finally:
        browser_bridge.clear_extension_ws()
        browser_bridge.disconnect()


async def serve_forever() -> None:
    """Runs the bridge server until cancelled - the coroutine the agent
    process starts as a background asyncio task (see main.py) so it shares
    an event loop with browser_tools.py's calls into browser_bridge."""
    logger.info("browser bridge server: token=%s", browser_bridge.session_token)
    async with websockets.serve(bridge_handler, BRIDGE_HOST, BRIDGE_PORT, origins=None):
        logger.info("browser bridge server: listening on ws://%s:%s", BRIDGE_HOST, BRIDGE_PORT)
        await asyncio.Future()  # run until cancelled


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(serve_forever())
