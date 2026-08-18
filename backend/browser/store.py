"""Snapshot and element storage for the browser bridge - deliberately kept
separate from BrowserBridge (bridge.py) rather than folded into one class.

Bridge owns connection lifecycle, the action queue, and asyncio.Event-based
waiting; store owns the actual snapshot/element data. queue_action reads
from the store (to resolve a ref_id into element metadata) but the store
itself has no idea BrowserBridge exists - a one-way dependency, matching
the reference architecture this was adapted from (see planning.md).
"""

from typing import Dict, Optional

from .models import ElementRef, PageSnapshot


class BrowserStore:
    def __init__(self):
        self._snapshots: Dict[str, PageSnapshot] = {}
        self._refs: Dict[str, Dict[str, ElementRef]] = {}
        self._current_session_id: Optional[str] = None

    def upsert_snapshot(self, snapshot: PageSnapshot) -> None:
        """Replaces this session's snapshot and its entire element table.
        Note this is a full replacement, not a merge: any ElementRef from
        a previous snapshot whose ref_id doesn't appear in the new one is
        simply gone. That's intentional - a stale ref_id should fail to
        resolve rather than silently return outdated element data - but it
        does mean get_element can legitimately miss right after a page
        changes, which is exactly why content_script.js's action execution
        has a tier-3 heuristic fallback instead of only trusting ref_id.
        """
        self._snapshots[snapshot.session_id] = snapshot
        self._refs[snapshot.session_id] = {el.ref_id: el for el in snapshot.elements}
        self._current_session_id = snapshot.session_id

    def get_snapshot(self, session_id: Optional[str] = None) -> Optional[PageSnapshot]:
        sid = session_id or self._current_session_id
        if not sid:
            return None
        return self._snapshots.get(sid)

    def get_element(self, ref_id: str, session_id: Optional[str] = None) -> Optional[ElementRef]:
        sid = session_id or self._current_session_id
        if not sid:
            return None
        return self._refs.get(sid, {}).get(ref_id)

    def current_generation(self, session_id: Optional[str] = None) -> int:
        snapshot = self.get_snapshot(session_id)
        return snapshot.generation if snapshot else 0


browser_store = BrowserStore()
