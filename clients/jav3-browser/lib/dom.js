// Page helpers shared by three places:
//  - the page functions in lib/page.js: the worker injects THIS file into a
//    frame (chrome.scripting files:) right before each page function, because
//    a function passed to executeScript is serialized and cannot import;
//  - the service worker (lib/verbs.js imports it for normalizeCombo);
//  - node --test (pure functions, fed small fake elements).
// No import/export on purpose: it must parse as a classic script (file
// injection) and as an ES module (import). It installs globalThis.__jav3Dom
// once per realm; the IIFE keeps re-injection free of redeclaration errors.
(function () {
  const V = 2;
  if (globalThis.__jav3Dom && globalThis.__jav3Dom.v === V) return;

  const ATTR = 'data-jav3-id';
  const SEL = 'a[href], button, input:not([type=hidden]), textarea, select, summary, ' +
    '[role=button], [role=link], [role=textbox], [role=searchbox], [role=checkbox], ' +
    '[role=radio], [role=switch], [role=tab], [role=menuitem], [role=menuitemcheckbox], ' +
    '[role=menuitemradio], [role=option], [role=combobox], [role=slider], ' +
    '[contenteditable=""], [contenteditable=true]';
  const TABBABLE = 'a[href], button, input:not([type=hidden]), select, textarea, summary, ' +
    'iframe, [tabindex], [contenteditable=""], [contenteditable=true]';
  const NODE_BUDGET = 40000;

  const clean = (s, n = 120) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim().slice(0, n);

  // --- deep (shadow-piercing) traversal -------------------------------------------

  // fn(el) for every element under root in document order, descending into OPEN
  // shadow roots right after their host. Bounded so a huge page cannot hang a read.
  function deepEach(root, fn, budget) {
    budget = budget || { n: NODE_BUDGET };
    const all = root.querySelectorAll('*');
    for (const el of all) {
      if (--budget.n < 0) return false;
      if (fn(el) === false) return false;
      if (el.shadowRoot && deepEach(el.shadowRoot, fn, budget) === false) return false;
    }
    return true;
  }

  function deepFind(root, selector) {
    let hit = root.querySelector(selector);
    if (hit) return hit;
    deepEach(root, el => {
      if (el.shadowRoot) {
        const h = el.shadowRoot.querySelector(selector);
        if (h) { hit = h; return false; }
      }
      return true;
    });
    return hit;
  }

  function findJav3(root, n) {
    return deepFind(root, '[' + ATTR + '="' + String(Number(n)) + '"]');
  }

  function deepActive(doc) {
    let a = doc.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return a;
  }

  // The candidates for the element list: interactive, enabled, with a box and
  // not hidden. readPage and pageSig both count these, so signatures agree.
  function collect(doc, win) {
    const vw = win.innerWidth, vh = win.innerHeight;
    const out = [];
    deepEach(doc, el => {
      if (!el.matches || !el.matches(SEL) || el.disabled) return;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) return;
      const st = win.getComputedStyle(el);
      if (st.visibility === 'hidden' || st.display === 'none') return;
      out.push({ el, r, inView: r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw });
    });
    return out;
  }

  // --- accessible names -------------------------------------------------------------

  function byId(el, id) {
    const r = el.getRootNode ? el.getRootNode() : null;
    let t = r && typeof r.getElementById === 'function' ? r.getElementById(id) : null;
    if (!t && typeof document !== 'undefined' && document.getElementById) t = document.getElementById(id);
    return t;
  }

  // Text of a node, skipping `skip` (the control itself, so a wrapping label
  // around a <select> does not swallow the option texts).
  function textWithout(node, skip) {
    if (!node || node === skip) return '';
    if (node.nodeType === 3) return node.data || '';
    if (node.nodeType !== 1 && node.nodeType !== 11) return '';
    const tag = (node.tagName || '').toLowerCase();
    if (tag === 'script' || tag === 'style') return '';
    const kids = node.childNodes || [];
    if (!kids.length) return node.textContent || '';
    let s = '';
    for (const k of kids) s += ' ' + textWithout(k, skip);
    return s;
  }

  function labelsText(el) {
    const out = [];
    const seen = new Set();
    const add = lab => { if (lab && !seen.has(lab)) { seen.add(lab); const t = clean(textWithout(lab, el)); if (t) out.push(t); } };
    if (el.labels && el.labels.length) for (const l of el.labels) add(l);
    if (!out.length && el.id) {
      const r = el.getRootNode ? el.getRootNode() : null;
      if (r && r.querySelector) {
        try { add(r.querySelector('label[for="' + String(el.id).replace(/["\\]/g, '\\$&') + '"]')); } catch { /* odd id */ }
      }
    }
    if (!out.length && el.closest) add(el.closest('label'));
    return clean(out.join(' '));
  }

  const FIELD_TAGS = new Set(['input', 'select', 'textarea']);

  // The name a screen reader would announce, roughly: aria-labelledby (resolved
  // to text), aria-label, <label for> / wrapping <label>, the control's own
  // visible text, the alt of an image (or <svg><title>) inside it, placeholder,
  // title, the value of a submit button. '' for a truly unlabelled control.
  function accessibleName(el) {
    const get = k => (el.getAttribute ? el.getAttribute(k) : null);
    const lb = get('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).filter(Boolean).map(id => {
        const n = byId(el, id);
        return n ? clean(n.innerText || n.textContent) : '';
      }).filter(Boolean).join(' ');
      if (t) return clean(t);
    }
    const aria = get('aria-label');
    if (aria && aria.trim()) return clean(aria);
    const tag = String(el.tagName || '').toLowerCase();
    const type = String(get('type') || '').toLowerCase();
    if (FIELD_TAGS.has(tag) || tag === 'button') {
      const l = labelsText(el);
      if (l) return l;
    }
    if (!FIELD_TAGS.has(tag)) {
      const own = clean(el.innerText != null && el.innerText !== '' ? el.innerText : el.textContent);
      if (own) return own;
      if (el.querySelector) {
        const img = el.querySelector('img[alt]');
        if (img && clean(img.getAttribute('alt'))) return clean(img.getAttribute('alt'));
        const svgl = el.querySelector('svg[aria-label]');
        if (svgl) return clean(svgl.getAttribute('aria-label'));
        const st = el.querySelector('svg title');
        if (st && clean(st.textContent)) return clean(st.textContent);
      }
    }
    if (tag === 'input' && type === 'image' && clean(get('alt'))) return clean(get('alt'));
    const ph = get('placeholder');
    if (ph && ph.trim()) return clean(ph);
    const title = get('title');
    if (title && title.trim()) return clean(title);
    if (tag === 'input' && (type === 'submit' || type === 'button' || type === 'reset')) return clean(el.value);
    return '';
  }

  // --- ordering and caps --------------------------------------------------------------

  // In-view elements first (document order kept within each group), then cap.
  function orderInViewFirst(items, cap) {
    const a = [], b = [];
    for (const it of items) (it.inView ? a : b).push(it);
    return a.concat(b).slice(0, cap);
  }

  // Sequential focus order: positive tabindex ascending first, then the rest in
  // document order. items: [{tabIndex}] in document order; returns indices.
  function tabOrder(items) {
    const idx = items.map((it, i) => i).filter(i => items[i].tabIndex >= 0);
    const pos = idx.filter(i => items[i].tabIndex > 0).sort((x, y) => items[x].tabIndex - items[y].tabIndex || x - y);
    const zero = idx.filter(i => items[i].tabIndex === 0);
    return pos.concat(zero);
  }

  // Next index in `order` after `current` (an index into items, or -1), wrapping.
  function nextInOrder(order, current, dir) {
    if (!order.length) return -1;
    const at = order.indexOf(current);
    if (at < 0) return dir > 0 ? order[0] : order[order.length - 1];
    return order[(at + (dir > 0 ? 1 : -1) + order.length) % order.length];
  }

  function selectOptions(el, max) {
    const opts = [];
    const all = el.options || [];
    for (let i = 0; i < all.length && opts.length < (max || 20); i++) {
      const o = all[i];
      opts.push({ t: clean(o.label || o.text || o.textContent, 40), v: clean(o.value, 60), s: !!o.selected });
    }
    return { options: opts, more: Math.max(0, all.length - opts.length) };
  }

  // Pick the option to select: exact value, else exact label, else a
  // case-insensitive label match. -> index or -1
  function pickOption(options, value, label) {
    const list = Array.from(options || []);
    const lab = o => clean(o.label || o.text || o.textContent, 500);
    if (value !== undefined && value !== null) return list.findIndex(o => o.value === value);
    const want = clean(label, 500);
    let i = list.findIndex(o => lab(o) === want);
    if (i < 0) i = list.findIndex(o => lab(o).toLowerCase() === want.toLowerCase());
    return i;
  }

  // --- the page signature (did anything change?) -------------------------------------

  function hashText(s) {       // FNV-1a 32-bit, hex
    let h = 0x811c9dc5;
    s = String(s || '');
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return ('0000000' + h.toString(16)).slice(-8);
  }

  function signature(text, count) { return hashText(text) + ':' + (count | 0); }

  // --- key combos (mirrors normalize_combo in backend/browser.py and the desk) ----------

  const MODIFIERS = { ctrl: 'ctrl', control: 'ctrl', shift: 'shift', alt: 'alt', option: 'alt',
    super: 'super', logo: 'super', win: 'super', meta: 'super', cmd: 'super', command: 'super',
    altgr: 'altgr' };
  const MOD_ORDER = ['ctrl', 'alt', 'altgr', 'shift', 'super'];
  const NAMED = ['Return', 'Enter', 'Tab', 'Escape', 'BackSpace', 'Delete', 'Insert', 'Home', 'End',
    'Page_Up', 'Page_Down', 'Prior', 'Next', 'Left', 'Right', 'Up', 'Down', 'space', 'minus', 'equal',
    'comma', 'period', 'slash', 'backslash', 'semicolon', 'apostrophe', 'grave', 'bracketleft',
    'bracketright', 'Print', 'Menu'];
  // Browser-style spellings a model is likely to use, folded onto the desk's names.
  const ALIASES = { esc: 'Escape', backspace: 'BackSpace', del: 'Delete', pageup: 'Page_Up',
    pagedown: 'Page_Down', arrowleft: 'Left', arrowright: 'Right', arrowup: 'Up', arrowdown: 'Down',
    spacebar: 'space' };
  const NAMED_LC = {};
  for (const k of NAMED) NAMED_LC[k.toLowerCase()] = k;

  class ComboError extends Error {}

  function normalizeCombo(combo) {
    if (typeof combo !== 'string' || !/^[A-Za-z0-9_]{1,32}(\+[A-Za-z0-9_]{1,32}){0,4}$/.test(combo.trim())) {
      throw new ComboError('bad key combo (e.g. "Enter", "Tab", "shift+Tab", "ctrl+a")');
    }
    const parts = combo.trim().split('+');
    const key = parts.pop();
    const mods = [];
    for (const m of parts) {
      const c = MODIFIERS[m.toLowerCase()];
      if (!c) throw new ComboError(`unknown modifier ${JSON.stringify(m)}`);
      if (!mods.includes(c)) mods.push(c);
    }
    mods.sort((a, b) => MOD_ORDER.indexOf(a) - MOD_ORDER.indexOf(b));
    let k = key;
    if (!(k.length === 1 && /[A-Za-z0-9]/.test(k))) {
      const f = /^[Ff]([1-9]|1[0-9]|2[0-4])$/.exec(k);
      if (f) k = 'F' + f[1];
      else k = NAMED_LC[k.toLowerCase()] || ALIASES[k.toLowerCase()] || null;
      if (!k) throw new ComboError(`unknown key ${JSON.stringify(key)}`);
    }
    return mods.concat([k]).join('+');
  }

  const KEYS = {
    Return: ['Enter', 'Enter', 13], Enter: ['Enter', 'Enter', 13], Tab: ['Tab', 'Tab', 9],
    Escape: ['Escape', 'Escape', 27], BackSpace: ['Backspace', 'Backspace', 8],
    Delete: ['Delete', 'Delete', 46], Insert: ['Insert', 'Insert', 45], Home: ['Home', 'Home', 36],
    End: ['End', 'End', 35], Page_Up: ['PageUp', 'PageUp', 33], Prior: ['PageUp', 'PageUp', 33],
    Page_Down: ['PageDown', 'PageDown', 34], Next: ['PageDown', 'PageDown', 34],
    Left: ['ArrowLeft', 'ArrowLeft', 37], Up: ['ArrowUp', 'ArrowUp', 38],
    Right: ['ArrowRight', 'ArrowRight', 39], Down: ['ArrowDown', 'ArrowDown', 40],
    space: [' ', 'Space', 32], minus: ['-', 'Minus', 189], equal: ['=', 'Equal', 187],
    comma: [',', 'Comma', 188], period: ['.', 'Period', 190], slash: ['/', 'Slash', 191],
    backslash: ['\\', 'Backslash', 220], semicolon: [';', 'Semicolon', 186],
    apostrophe: ["'", 'Quote', 222], grave: ['`', 'Backquote', 192],
    bracketleft: ['[', 'BracketLeft', 219], bracketright: [']', 'BracketRight', 221],
    Print: ['PrintScreen', 'PrintScreen', 44], Menu: ['ContextMenu', 'ContextMenu', 93],
  };

  // A normalized combo -> the KeyboardEventInit fields for it.
  function keySpec(combo) {
    const norm = normalizeCombo(combo);
    const parts = norm.split('+');
    const name = parts.pop();
    const has = m => parts.includes(m);
    let key, code, keyCode;
    if (KEYS[name]) [key, code, keyCode] = KEYS[name];
    else if (/^F\d+$/.test(name)) { key = name; code = name; keyCode = 111 + Number(name.slice(1)); }
    else if (/^[0-9]$/.test(name)) { key = name; code = 'Digit' + name; keyCode = name.charCodeAt(0); }
    else {
      key = has('shift') ? name.toUpperCase() : name;
      code = 'Key' + name.toUpperCase(); keyCode = name.toUpperCase().charCodeAt(0);
    }
    return { combo: norm, name, key, code, keyCode,
      ctrlKey: has('ctrl'), altKey: has('alt') || has('altgr'), shiftKey: has('shift'), metaKey: has('super') };
  }

  globalThis.__jav3Dom = {
    v: V, ATTR, SEL, TABBABLE, clean, deepEach, collect, deepFind, findJav3, deepActive,
    accessibleName, labelsText, textWithout, orderInViewFirst, tabOrder, nextInOrder,
    selectOptions, pickOption, hashText, signature, normalizeCombo, keySpec, ComboError,
  };
})();
