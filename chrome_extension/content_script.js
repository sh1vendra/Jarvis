// Jarvis Browser Bridge - content script
//
// Runs in every page (static content_scripts entry in manifest.json,
// document_idle), with an idempotent guard against double-injection since
// background.js's ensureContentScript() can also inject this on demand.
//
// Three jobs: (1) tag interactive/readable elements with a stable
// data-agent-id so they can be referenced across multiple messages even
// after a re-render, (2) build PageSnapshots on request or after DOM
// mutations, (3) resolve and execute actions (click/type/select) sent
// from the background script.

(function () {
  if (window.__jarvis_injected__) return;
  window.__jarvis_injected__ = true;

  // ── data-agent-id tagging ──
  //
  // Assignment is idempotent and monotonic: an element that already
  // carries data-agent-id (common after an SPA re-render that preserves
  // DOM nodes) keeps its existing id rather than getting a new one, and
  // the counter always advances past any id it encounters so ids are
  // never reused or recycled within the page's lifetime. ref_id sent to
  // the backend is always "jw_" + agentId.

  let _nextAgentId = 1;
  const _agentIdMap = new Map(); // agent_id -> Element, for O(1) lookup

  function assignAgentId(el) {
    const existing = el.getAttribute("data-agent-id");
    if (existing) {
      const id = parseInt(existing, 10);
      if (!isNaN(id) && id > 0) {
        _agentIdMap.set(id, el);
        _nextAgentId = Math.max(_nextAgentId, id + 1);
        return id;
      }
    }
    const id = _nextAgentId++;
    el.setAttribute("data-agent-id", String(id));
    _agentIdMap.set(id, el);
    return id;
  }

  function lookupByAgentId(agentId) {
    const fromMap = _agentIdMap.get(agentId);
    if (fromMap && fromMap.isConnected) return fromMap;
    const el = document.querySelector('[data-agent-id="' + agentId + '"]');
    if (el) _agentIdMap.set(agentId, el);
    return el;
  }

  function pruneAgentIdMap(maxEntries) {
    let scanned = 0;
    const limit = maxEntries || 2500;
    for (const [id, el] of _agentIdMap.entries()) {
      scanned++;
      if (!el || !el.isConnected) _agentIdMap.delete(id);
      if (scanned >= limit) break;
    }
  }

  // ── Element selection ──

  const INTERACTIVE_SELECTOR = [
    "a[href]",
    "button",
    "input:not([type=hidden])",
    "textarea",
    "select",
    "[contenteditable=true]",
    "[contenteditable='']",
    "[role=button]",
    "[role=link]",
    "[role=textbox]",
    "[role=searchbox]",
    "[role=combobox]",
    "[role=checkbox]",
    "[role=radio]",
    "[role=switch]",
    "[role=menuitem]",
    "[role=tab]",
    "[onclick]",
  ].join(",");

  const READABLE_SELECTOR = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "label", "td", "th", "pre", "code"].join(",");

  function isVisible(el) {
    if (!el || !el.isConnected) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || parseFloat(style.opacity || "1") === 0) return false;
    if (el.getAttribute("aria-hidden") === "true") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function isReadableCandidate(el) {
    const text = (el.innerText || el.textContent || "").trim();
    const tag = el.tagName.toLowerCase();
    const isHeading = /^h[1-6]$/.test(tag);
    if (isHeading) return text.length >= 2;
    if (text.length < 24) return false;
    // Reject large "container" blocks so we don't duplicate the entire
    // page body as one giant readable node - a real paragraph/list item
    // rarely has more than a handful of children.
    if (el.children.length > 10 && text.length > 120) return false;
    return true;
  }

  function isInViewport(el) {
    const rect = el.getBoundingClientRect();
    return rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth;
  }

  function actionTypesFor(el) {
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role") || "";
    const types = [];
    if (tag === "a" || tag === "button" || role === "button" || role === "link" || role === "menuitem" || role === "tab" || el.hasAttribute("onclick")) {
      types.push("click");
    }
    if (tag === "input" || tag === "textarea" || el.isContentEditable || role === "textbox" || role === "searchbox" || role === "combobox") {
      types.push("type");
    }
    if (tag === "select" || role === "combobox") {
      types.push("select");
    }
    if (tag === "input" && ["checkbox", "radio"].includes((el.getAttribute("type") || "").toLowerCase())) {
      types.push("click");
    }
    return types.length ? types : ["click"]; // unknown interactive elements default to clickable
  }

  // ── dom_path / ancestor labels - the fallback fingerprint used by tier 3 ──

  function domPath(el) {
    const parts = [];
    let node = el;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < 8) {
      const tag = node.tagName.toLowerCase();
      let siblingIndex = 0;
      let sibling = node;
      while ((sibling = sibling.previousElementSibling)) {
        if (sibling.tagName === node.tagName) siblingIndex++;
      }
      parts.unshift(`${tag}:${siblingIndex}`);
      node = node.parentElement;
      depth++;
    }
    return parts.join(">");
  }

  function ancestorLabels(el) {
    const labels = [];
    let node = el.parentElement;
    let depth = 0;
    while (node && depth < 6 && labels.length < 3) {
      const aria = node.getAttribute && node.getAttribute("aria-label");
      if (aria && aria.trim()) {
        labels.push(aria.trim());
      } else {
        const text = (node.innerText || "").trim();
        if (text && text.length <= 60) labels.push(text);
      }
      node = node.parentElement;
      depth++;
    }
    return labels;
  }

  // ── Serialization ──

  function serializeElement(el, sortIndex) {
    const agentId = assignAgentId(el);
    const rect = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    return {
      ref_id: "jw_" + agentId,
      agent_id: agentId,
      role: el.getAttribute("role") || "",
      tag,
      text: (el.innerText || el.value || "").trim().slice(0, 200),
      aria_label: el.getAttribute("aria-label") || "",
      name: el.getAttribute("name") || "",
      placeholder: el.getAttribute("placeholder") || "",
      href: el.getAttribute("href") || "",
      value: "value" in el ? String(el.value || "") : "",
      context_text: ancestorLabels(el).join(" > "),
      frame_path: "main",
      dom_path: domPath(el),
      bounds: { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) },
      visible: isVisible(el),
      enabled: !el.disabled,
      checked: !!el.checked,
      selected: !!el.selected,
      in_viewport: isInViewport(el),
      action_types: actionTypesFor(el),
      fingerprint: {
        role: el.getAttribute("role") || "",
        text: (el.innerText || "").trim().slice(0, 200),
        aria_label: el.getAttribute("aria-label") || "",
        name: el.getAttribute("name") || "",
        placeholder: el.getAttribute("placeholder") || "",
        href: el.getAttribute("href") || "",
        ancestor_labels: ancestorLabels(el),
        frame_path: "main",
        dom_path: domPath(el),
        sibling_index: sortIndex,
        stable_attributes: {},
      },
    };
  }

  function collectElements() {
    pruneAgentIdMap(3000);
    const seen = new Set();
    const results = [];

    const interactiveNodes = document.querySelectorAll(INTERACTIVE_SELECTOR);
    for (const el of interactiveNodes) {
      if (results.length >= 320) break;
      if (seen.has(el)) continue;
      if (!isVisible(el)) continue;
      seen.add(el);
      results.push(serializeElement(el, results.length));
    }

    const readableNodes = document.querySelectorAll(READABLE_SELECTOR);
    for (const el of readableNodes) {
      if (results.length >= 520) break;
      if (seen.has(el)) continue;
      if (!isReadableCandidate(el)) continue;
      seen.add(el);
      results.push(serializeElement(el, results.length));
    }

    return results;
  }

  function buildSnapshot(sessionId, tabId) {
    return {
      session_id: sessionId,
      tab_id: tabId,
      url: window.location.href,
      title: document.title,
      // Generation is the browser's own clock, not a counter we maintain -
      // see backend/browser/bridge.py's module docstring for why this
      // matters: it's what lets the backend detect "a genuinely newer
      // snapshot arrived" with a simple strict-greater-than comparison.
      generation: Date.now(),
      frame_id: "main",
      viewport: {
        width: window.innerWidth,
        height: window.innerHeight,
        scrollX: Math.round(window.scrollX),
        scrollY: Math.round(window.scrollY),
        scrollHeight: document.documentElement.scrollHeight,
        pageHeight: document.documentElement.scrollHeight,
      },
      elements: collectElements(),
      opaque_regions: [],
    };
  }

  // ── Action execution: 3-tier element resolution ──
  //
  // This is the single most important pattern here, and the reason
  // actions survive DOM churn between when a ref_id was captured (in a
  // snapshot) and when the action actually executes - a real gap for any
  // single-page app that re-renders (React/Vue key-diffing routinely
  // replaces DOM nodes rather than mutating them in place).

  function findTargetForAction(action) {
    // Tier 1: agent_id lookup, O(1).
    const agentIdRaw = action?.metadata?.agent_id;
    if (agentIdRaw) {
      const agentId = Number(agentIdRaw);
      const el = lookupByAgentId(agentId);
      if (el && isVisible(el)) {
        return { element: el, score: 10000, matchedBy: "agent_id" };
      }
    }

    // Tier 2: parse "jw_N" out of ref_id - same underlying map, reached a
    // different way. ref_id is always present on an action; agent_id only
    // arrives via metadata when the backend successfully resolved the
    // element from its own snapshot store, so this is a safety net for
    // when that metadata wasn't attached.
    const refId = action?.ref_id || "";
    if (refId.startsWith("jw_")) {
      const parsed = parseInt(refId.slice(3), 10);
      if (!isNaN(parsed)) {
        const el = lookupByAgentId(parsed);
        if (el && isVisible(el)) {
          return { element: el, score: 10000, matchedBy: "ref_id" };
        }
      }
    }

    // Tier 3: heuristic re-scan - only reached if the tagged element is
    // genuinely gone from the live DOM.
    return findTargetByHeuristic(action);
  }

  function normalizeText(value) {
    return String(value || "")
      .trim()
      .replace(/\s+/g, " ")
      .toLowerCase();
  }

  function collectActionCandidates() {
    const seen = new Set();
    const candidates = [];
    for (const el of document.querySelectorAll(INTERACTIVE_SELECTOR)) {
      if (seen.has(el)) continue;
      if (!isVisible(el)) continue;
      seen.add(el);
      candidates.push({ element: el, payload: serializeElement(el, candidates.length) });
    }
    return candidates;
  }

  function scoreCandidate(action, payload) {
    const metadata = action?.metadata || {};
    const requestedAction = String(action?.action || "");
    // Hard disqualifier, never just a scoring penalty: an element that
    // doesn't support the requested action type is never picked, even as
    // a "least bad" fallback - e.g. never click a plain text node because
    // it happened to score highest on label similarity.
    if (requestedAction && Array.isArray(payload.action_types) && payload.action_types.length && !payload.action_types.includes(requestedAction)) {
      return -1;
    }

    let score = 0;
    if (metadata.dom_path && payload.dom_path === metadata.dom_path) score += 300;
    if (metadata.tag && payload.tag === metadata.tag) score += 40;
    if (metadata.role && payload.role === metadata.role) score += 40;

    const expectedLabels = [metadata.label, metadata.text, metadata.aria_label, metadata.name, metadata.placeholder, metadata.href]
      .map(normalizeText)
      .filter(Boolean);
    const actualLabels = [payload.text, payload.aria_label, payload.name, payload.placeholder, payload.href, payload.context_text]
      .map(normalizeText)
      .filter(Boolean);

    for (const expected of expectedLabels) {
      for (const actual of actualLabels) {
        if (actual === expected) score += 120;
        else if (actual.includes(expected) || expected.includes(actual)) score += 60;
      }
    }

    if (payload.in_viewport) score += 15;
    return score;
  }

  function findTargetByHeuristic(action) {
    const candidates = collectActionCandidates();
    let best = null;
    for (const candidate of candidates) {
      const score = scoreCandidate(action, candidate.payload);
      if (score < 0) continue;
      if (!best || score > best.score) {
        best = { element: candidate.element, score, matchedBy: "heuristic" };
      }
    }
    if (!best || best.score <= 0) return null;
    return best;
  }

  // ── Action execution ──

  function focusAndReveal(el) {
    el.scrollIntoView({ block: "center", inline: "center" });
    el.focus();
  }

  function executeClick(el) {
    focusAndReveal(el);
    el.click();
    return { ok: true, message: "Clicked." };
  }

  // Plain `el.value = text` doesn't reliably work on framework-controlled
  // inputs (React and similar reactive frameworks). These frameworks
  // override the `value` property setter on the element *instance* (or
  // intercept it via a synthetic event system) so they can sync their own
  // internal state whenever a value changes - a naive assignment goes
  // through THAT overridden setter, which frequently no-ops or gets
  // clobbered on the framework's next re-render, since as far as the
  // framework's own state is concerned, nothing happened. Confirmed
  // directly: this is exactly what was observed against Google Flights'
  // destination field - the DOM visibly reacted (an autocomplete dropdown
  // opened, proving the input/focus events did fire) but the field's own
  // value stayed empty, because React's change tracking never fired.
  //
  // The fix is the standard technique for this, not a workaround: grab
  // the *native* value setter directly off the element's prototype
  // (HTMLInputElement.prototype / HTMLTextAreaElement.prototype) - this
  // is the browser's own original setter, defined before any framework
  // instance-level override exists - and call it explicitly. That bypasses
  // the framework's interception entirely and writes the real, underlying
  // DOM property. Dispatching a real `input` event afterward is what
  // actually makes React (and similar frameworks) notice the change at
  // all, since they listen for that event rather than polling the
  // property on every tick.
  //
  // This is the default typing mechanism for every plain value-carrying
  // element, not a Google-Flights-specific special case - the same
  // failure mode applies to any framework-controlled input, and a native
  // setter + real events is strictly correct (not just "also works") for
  // plain, non-framework inputs too, since that's what a native controls
  // 'value =' assignment already reduces to.
  function setNativeValue(el, value) {
    const tag = el.tagName.toLowerCase();
    const proto = tag === "textarea" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const nativeSetter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (nativeSetter) {
      nativeSetter.call(el, value);
    } else {
      el.value = value; // fallback, shouldn't be reachable in a real browser
    }
  }

  function executeType(el, text, clearFirst) {
    focusAndReveal(el);
    if (el.isContentEditable) {
      // Rich text editors (Google Docs/Notion-style canvas or
      // virtual-DOM editors) silently ignore direct textContent/value
      // assignment and only respond to the browser's native editing
      // command pipeline.
      if (clearFirst) document.execCommand("selectAll", false, null);
      document.execCommand("insertText", false, text);
      return { ok: true, message: "Typed via execCommand into contenteditable." };
    }
    if ("value" in el) {
      if (clearFirst || el.value) setNativeValue(el, "");
      setNativeValue(el, text);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true, message: "Typed via native value setter + input/change events." };
    }
    return { ok: false, message: "Element does not support text input." };
  }

  function executeSelect(el, option) {
    focusAndReveal(el);
    if (el.tagName.toLowerCase() !== "select") {
      return { ok: false, message: "Element is not a <select>." };
    }
    const optionEls = Array.from(el.options || []);
    const match = optionEls.find(
      (o) => normalizeText(o.value) === normalizeText(option) || normalizeText(o.textContent) === normalizeText(option),
    );
    if (!match) {
      return { ok: false, message: `No option matching "${option}" found.` };
    }
    el.value = match.value;
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true, message: "Selected." };
  }

  function executeAction(action) {
    const target = findTargetForAction(action);
    if (!target || !target.element) {
      return { ok: false, message: `Could not resolve target for action '${action?.action}'.`, matchedBy: "none" };
    }

    let outcome;
    switch (action.action) {
      case "click":
        outcome = executeClick(target.element);
        break;
      case "type":
        outcome = executeType(target.element, action.text || "", !!action.clear_first);
        break;
      case "select":
        outcome = executeSelect(target.element, action.option || "");
        break;
      default:
        outcome = { ok: false, message: `Unsupported action type: ${action.action}` };
    }

    return {
      ...outcome,
      executedRefId: "jw_" + assignAgentId(target.element),
      matchedBy: target.matchedBy,
    };
  }

  // ── MutationObserver: push a fresh snapshot automatically after real DOM changes ──

  let _mutationBuffer = 0;
  let _mutationTimer = null;

  function scheduleSnapshotAfterMutation() {
    _mutationBuffer++;
    if (_mutationBuffer < 5) return; // require a real batch, not every single tiny mutation
    if (_mutationTimer) return;
    _mutationTimer = setTimeout(() => {
      _mutationTimer = null;
      _mutationBuffer = 0;
      sendSnapshotToBackground();
    }, 300);
  }

  const observer = new MutationObserver(() => scheduleSnapshotAfterMutation());
  observer.observe(document.documentElement || document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    characterData: false,
  });

  // ── Messaging with background.js ──

  let _lastSessionId = "";
  let _lastTabId = "";

  function sendSnapshotToBackground() {
    if (!_lastSessionId) return;
    const snapshot = buildSnapshot(_lastSessionId, _lastTabId);
    chrome.runtime.sendMessage({ type: "jarvis_snapshot", snapshot });
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "jarvis_collect_snapshot") {
      _lastSessionId = message.sessionId || _lastSessionId;
      _lastTabId = message.tabId || _lastTabId;
      const snapshot = buildSnapshot(_lastSessionId, _lastTabId);
      chrome.runtime.sendMessage({ type: "jarvis_snapshot", snapshot }).then(
        () => sendResponse({ ok: true }),
        () => sendResponse({ ok: false }),
      );
      return true;
    }

    if (message?.type === "jarvis_execute_action") {
      _lastSessionId = message.sessionId || _lastSessionId;
      _lastTabId = message.tabId || _lastTabId;
      try {
        const result = executeAction(message.action || {});
        sendResponse(result);
      } catch (error) {
        sendResponse({ ok: false, message: String(error?.message || error) });
      }
      return true;
    }

    return false;
  });
})();
