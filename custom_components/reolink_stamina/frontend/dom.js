/**
 * Minimal DOM helpers.
 *
 * The panel is deliberately dependency-free — no build step, no bundler, nothing to
 * install — so these few helpers stand in for a framework. The keyed reconciler is the
 * important one: rebuilding the event list from scratch on every patch would destroy
 * scroll position, focus and the row animations.
 */

/** Create an element with attributes, listeners and children. */
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

/** Append a nested array of children, skipping empties. */
export function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

/** Replace all children of a node. */
export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/** Attach stylesheet text to a shadow root, preferring constructable stylesheets. */
export function adoptStyles(root, cssText) {
  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(cssText);
    root.adoptedStyleSheets = [...root.adoptedStyleSheets, sheet];
    return;
  } catch {
    // Older engines: fall back to an inline <style>.
    root.append(el("style", { text: cssText }));
  }
}

/**
 * Reconcile a list of items into a container, reusing nodes by key.
 *
 * `create` builds a node for a new item; `update` refreshes an existing one. Nodes keep
 * their identity across renders, so CSS transitions and focus survive updates.
 */
export function reconcile(container, items, keyOf, create, update) {
  const existing = new Map();
  for (const child of Array.from(container.children)) {
    if (child.__key !== undefined) existing.set(child.__key, child);
  }

  const seen = new Set();
  items.forEach((item, index) => {
    const key = keyOf(item);
    seen.add(key);
    let node = existing.get(key);
    if (!node) {
      node = create(item, index);
      node.__key = key;
    }
    if (update) update(node, item, index);
    const atIndex = container.children[index];
    if (atIndex !== node) container.insertBefore(node, atIndex || null);
  });

  for (const [key, node] of existing) {
    if (!seen.has(key)) node.remove();
  }
}

/** An icon, using Home Assistant's own <ha-icon> when available. */
export function icon(name, extraClass = "") {
  if (customElements.get("ha-icon")) {
    return el("ha-icon", { icon: name, class: `icon ${extraClass}`.trim() });
  }
  // Graceful degradation: keep the layout, drop the glyph.
  return el("span", { class: `icon icon--missing ${extraClass}`.trim(), "aria-hidden": "true" });
}

/** Debounce onto the next animation frame, coalescing bursts of state changes. */
export function frameDebounce(fn) {
  let handle = null;
  return (...args) => {
    if (handle !== null) return;
    handle = requestAnimationFrame(() => {
      handle = null;
      fn(...args);
    });
  };
}
