"""Typed contracts for the browser bridge - the RPC-style messages that flow
between the backend, the Chrome extension's background service worker, and
its content script.

Plain dataclasses, not pydantic, matching the reference architecture this
was adapted from (see planning.md's "browser bridge" entry) - there's no
FastAPI/pydantic validation layer here, just structures serialized to/from
JSON by hand in servers/browser_bridge_server.py.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import time


@dataclass
class ElementFingerprint:
    """A lightweight description of an element's identity, captured at the
    moment an action is queued against it. Used only by the content
    script's tier-3 heuristic re-scan (see content_script.js) if the
    element's tagged data-agent-id has gone stale by the time the action
    actually executes - e.g. a single-page app re-rendered and replaced
    the DOM node entirely rather than mutating it in place."""

    role: str = ""
    text: str = ""
    aria_label: str = ""
    name: str = ""
    placeholder: str = ""
    href: str = ""
    ancestor_labels: List[str] = field(default_factory=list)
    frame_path: str = "main"
    dom_path: str = ""
    sibling_index: int = 0
    stable_attributes: Dict[str, str] = field(default_factory=dict)


@dataclass
class ElementRef:
    """One interactive or readable element from a page snapshot. ref_id is
    always "jw_<agent_id>" (see content_script.js's assignAgentId) - the
    stable identifier a caller uses to refer to this element across
    multiple messages, even after the page has re-rendered.
    """

    ref_id: str
    generation: int
    agent_id: int = 0
    role: str = ""
    tag: str = ""
    text: str = ""
    aria_label: str = ""
    name: str = ""
    placeholder: str = ""
    href: str = ""
    value: str = ""
    context_text: str = ""
    frame_path: str = "main"
    dom_path: str = ""
    bounds: Dict[str, int] = field(default_factory=dict)
    visible: bool = True
    enabled: bool = True
    checked: bool = False
    selected: bool = False
    in_viewport: bool = True
    action_types: List[str] = field(default_factory=list)
    fingerprint: ElementFingerprint = field(default_factory=ElementFingerprint)

    def primary_label(self) -> str:
        """The single best human-readable label for this element, in
        priority order - used both for display and as the top signal in
        find_web_element's matching (see tools/browser_tools.py)."""
        return self.text or self.aria_label or self.name or self.placeholder or self.href or self.tag

    def supports(self, action: str) -> bool:
        """False only when action_types was populated and doesn't include
        this action - an element with no recorded action_types is assumed
        to support anything (we'd rather try and let the real click/type
        call fail than refuse based on incomplete metadata)."""
        return action in self.action_types if self.action_types else True


@dataclass
class ViewportMeta:
    """Viewport dimensions and scroll offsets reported by the extension,
    at snapshot time."""

    width: int = 0
    height: int = 0
    scroll_x: int = 0
    scroll_y: int = 0
    scroll_height: int = 0
    page_height: int = 0


@dataclass
class PageSnapshot:
    """A full description of one tab's page at one moment. generation is
    the field this whole bridge is built around - see bridge.py's module
    docstring for why it's a raw JS timestamp, not a backend-assigned
    counter."""

    session_id: str
    tab_id: str
    url: str
    title: str = ""
    generation: int = 1
    timestamp: float = field(default_factory=time.time)
    frame_id: str = "main"
    elements: List[ElementRef] = field(default_factory=list)
    opaque_regions: List[Dict[str, str]] = field(default_factory=list)
    viewport: ViewportMeta = field(default_factory=ViewportMeta)


@dataclass
class ActionRequest:
    """A single browser action queued by a backend tool, to be pushed to
    (or polled by) the extension. metadata carries the element fingerprint
    captured at queue time (dom_path, tag, role, labels, agent_id) - the
    content script's tier-3 fallback matcher scores live candidates
    against these fields if the tagged element itself can't be found."""

    action: str
    ref_id: str
    action_id: str = ""
    session_id: str = ""
    text: str = ""
    option: str = ""
    clear_first: bool = False
    timeout: float = 5.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionResult:
    """What the extension reports back after attempting an action.
    pre_generation/post_generation only become meaningful for verification
    once the caller separately awaits wait_for_snapshot(min_generation=
    pre_generation) - queue_action's own immediate return sets both to the
    same value, since the action hasn't reached the browser yet."""

    ok: bool
    message: str
    action: str
    ref_id: str = ""
    action_id: str = ""
    session_id: str = ""
    pre_generation: int = 0
    post_generation: int = 0
    details: Dict[str, Any] = field(default_factory=dict)
    error: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationReport:
    """Reserved for a future, richer verification step than the plain
    generation-comparison browser_tools.py does today - not populated by
    anything yet, kept for shape-parity with the reference architecture."""

    success: bool
    confidence: float
    message: str
    checks_passed: List[str] = field(default_factory=list)
    pre_generation: int = 0
    post_generation: int = 0
    needs_replan: bool = False


@dataclass
class DomChangeEvent:
    """Pushed by the content script's MutationObserver after an action
    triggers DOM mutations - informational only right now (recorded by
    bridge.record_dom_change), not yet consumed by any verification logic.
    """

    action_id: str
    ref_id: str = ""
    action_type: str = ""
    change_types: List[str] = field(default_factory=list)
    timestamp: float = 0.0
    session_id: str = ""
    tab_id: str = ""
