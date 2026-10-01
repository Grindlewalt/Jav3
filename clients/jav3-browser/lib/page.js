// Functions injected into Jav3's own tabs with chrome.scripting.executeScript.
// Each is serialized (not imported by the page) and run once PER FRAME: the
// service worker enumerates the tab's frames and injects into each, then
// stitches the results together (element numbers are local to a frame; the
// worker prefixes them with the frame index -> "f2:5").
//
// They lean on globalThis.__jav3Dom (lib/dom.js), which the worker injects as
// a file into the same frame and isolated world immediately before each call.
// A missing element returns { ok: false, code: 'stale' }; the server turns that
// into "element fN:M is no longer on the page — browser_read_page again".

// mode: 'auto' (candidates when fewer than 8 interactive elements are in
// view in this frame; the server applies the same rule across all frames),
// 'all' (always), 'interactive' (never).
export function readPage(maxChars, selector, mode) {
  const D = globalThis.__jav3Dom;
  D.deepEach(document, e => { if (e.hasAttribute(D.ATTR)) e.removeAttribute(D.ATTR); });
  const all = D.collect(document, window);
  const kept = D.orderInViewFirst(all, 300);
  const box = r => ({ x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) });
  const els = kept.map((c, i) => {
    const el = c.el, r = c.r, n = i + 1;
    el.setAttribute(D.ATTR, String(n));
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : '';
    const role = el.getAttribute('role') || '';
    const name = D.accessibleName(el);
    let text = '';
    let value;
    if (tag === 'input' || tag === 'textarea') {
      if (type !== 'password' && type !== 'file' && type !== 'checkbox' && type !== 'radio') {
        value = D.clean(el.value, 80);
      }
    } else if (tag !== 'select') {
      text = D.clean(el.innerText || '', 120);
      if (!text && tag === 'a') text = D.clean(el.getAttribute('href'), 120);
    }
    const e = {
      n, tag, type, role, name, text,
      box: box(r), inView: c.inView,
    };
    if (value) e.value = value;
    if (!name && !text) e.icon = true;              // icon-only: kept, flagged
    if (type === 'checkbox' || type === 'radio') e.checked = !!el.checked;
    else if (el.getAttribute('aria-checked')) e.checked = el.getAttribute('aria-checked') === 'true';
    if (tag === 'select') {
      const o = D.selectOptions(el, 20);
      e.options = o.options;
      if (o.more) e.more = o.more;
    }
    return e;
  });
  // Candidates: elements with no button markup that look clickable (a
  // cursor:pointer <div>, an ng-click <span>, short leaf text). Same id space
  // and data-jav3-id tagging, numbered after the interactive ones, so
  // click/type/hover work on them unchanged.
  const inViewCount = kept.filter(c => c.inView).length;
  if (mode === 'all' || (mode !== 'interactive' && inViewCount < 8)) {
    const cands = D.collectCandidates(document, window, all.map(c => c.el));
    cands.forEach((c, i) => {
      const n = kept.length + i + 1;
      c.el.setAttribute(D.ATTR, String(n));
      const name = D.clean(D.accessibleName(c.el), 80);
      const e = { n, kind: 'candidate', tag: c.el.tagName.toLowerCase(), type: '', role: '',
                  name, text: '', box: box(c.r), inView: c.inView, why: c.why };
      if (!name) e.icon = true;
      els.push(e);
    });
  }
  const raw = document.body ? document.body.innerText : '';
  const body = raw.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  let probed = null;
  if (selector) { try { probed = !!D.deepFind(document, selector); } catch { probed = null; } }
  // Where each child frame sits in this frame's viewport, so the worker can
  // place a subframe's elements on a screenshot (content box, borders skipped).
  const iframes = [];
  D.deepEach(document, el => {
    if (el.tagName !== 'IFRAME' && el.tagName !== 'FRAME') return;
    const r = el.getBoundingClientRect();
    iframes.push({ src: el.src || '', x: Math.round(r.left + el.clientLeft), y: Math.round(r.top + el.clientTop) });
  });
  return { url: location.href, title: document.title,
           text: body.slice(0, maxChars + 1), elements: els, probed, iframes,
           sig: D.signature(raw, all.length, D.formState(document)),
           viewport: { w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio || 1 } };
}

// The top frame's viewport, reported with each tab screenshot.
export function viewportInfo() {
  return { w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio || 1 };
}

// The same signature readPage reports, without re-labelling anything.
export function pageSig() {
  const D = globalThis.__jav3Dom;
  return D.signature(document.body ? document.body.innerText : '', D.collect(document, window).length, D.formState(document));
}

// Resolve when the DOM has had no mutations for quietMs, or at timeoutMs.
export function domQuiet(timeoutMs, quietMs) {
  return new Promise(resolve => {
    const start = Date.now();
    let last = start;
    const mo = new MutationObserver(() => { last = Date.now(); });
    mo.observe(document, { subtree: true, childList: true, characterData: true });
    const tick = () => {
      const now = Date.now();
      if (now - last >= quietMs || now - start >= timeoutMs) {
        mo.disconnect();
        resolve({ quiet: now - last >= quietMs, ms: now - start });
      } else setTimeout(tick, 50);
    };
    setTimeout(tick, Math.min(50, quietMs));
  });
}

// A realistic pointer/mouse sequence at the element's centre, dispatched on
// whatever is on top there (lib/dom.js realClick), so framework listeners on
// plain <div>s (pointerdown, mousedown, click) all fire.
export async function clickEl(id) {
  const D = globalThis.__jav3Dom;
  const el = D.findJav3(document, id);
  if (!el) return { ok: false, code: 'stale' };
  el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const hit = D.deepPoint(document, x, y) || el;
  return D.realClick(window, document, hit, x, y, el);
}

// A click at (x, y), CSS px of the top frame's viewport (the server converts
// from screenshot pixels).
// `expect` ({n, label}, a top-frame element) is what the screenshot showed at
// the point: if something else is under it now, refuse instead of clicking.
export async function clickAt(x, y, expect) {
  const D = globalThis.__jav3Dom;
  if (!(x >= 0 && y >= 0 && x < window.innerWidth && y < window.innerHeight)) {
    return { ok: false, err: `${x},${y} is outside the page (${window.innerWidth}x${window.innerHeight} CSS px); take a new browser_screenshot_tab` };
  }
  const hit = D.deepPoint(document, x, y);
  if (expect && D.pointMoved(D.findJav3(document, expect.n), hit)) {
    return { ok: false, code: 'moved', err: 'the page moved since the screenshot — ' + JSON.stringify(expect.label || 'the element') + ' is no longer at that point; browser_screenshot_tab again' };
  }
  if (!hit) return { ok: false, err: 'nothing on the page at that point' };
  if (hit.tagName === 'IFRAME' || hit.tagName === 'FRAME') {
    return { ok: false, code: 'frame', err: 'that point is inside an iframe, which a script-made click cannot reach by coordinates; browser_read_page and click its element by id (f1:…)' };
  }
  return D.realClick(window, document, hit, x, y, null);
}

// --- trusted clicks -------------------------------------------------------------------
// The page half of lib/trusted.js: real mouse input is sent by the service worker
// through chrome.debugger; these functions find where to aim, check what is on
// top there, and see whether the click arrived.

// Where the element is now: scrolled to the middle, the point a real click
// would hit (CSS px of THIS frame's viewport), and whether something else is
// on top of it there (a banner, a transparent iframe).
export function measureEl(id) {
  const D = globalThis.__jav3Dom;
  const el = D.findJav3(document, id);
  if (!el) return { ok: false, code: 'stale' };
  el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
  const vp = { w: window.innerWidth, h: window.innerHeight };
  const pt = D.clickPoint(el.getBoundingClientRect(), vp.w, vp.h);
  if (!pt) return { ok: false, code: 'offscreen', err: 'the element has no visible area to click' };
  const out = { ok: true, pt, vp, label: D.describeEl(el) };
  const hit = D.deepPoint(document, pt.x, pt.y);
  if (hit && D.isCovered(el, hit)) {
    const c = D.tagCover(document, hit);
    out.covered = { n: c.n, name: D.clean(D.accessibleName(c.el), 40) || c.el.tagName.toLowerCase() };
  }
  return out;
}

// What the service worker needs to place this frame in its parent: the frame's
// viewport, its own <iframe> box in the parent's viewport when the parent is
// same-origin (window.frameElement), and the boxes of the <iframe>s inside it
// (content boxes, CSS px of this frame's viewport) with their src.
export function frameInfo() {
  const D = globalThis.__jav3Dom;
  let self = null;
  try {
    const fe = window.frameElement;
    if (fe) {
      const r = fe.getBoundingClientRect();
      self = { x: r.left + fe.clientLeft, y: r.top + fe.clientTop, w: fe.clientWidth, h: fe.clientHeight };
    }
  } catch { self = null; }
  const iframes = [];
  D.deepEach(document, el => {
    if (el.tagName !== 'IFRAME' && el.tagName !== 'FRAME') return;
    const r = el.getBoundingClientRect();
    iframes.push({ src: el.src || '', x: r.left + el.clientLeft, y: r.top + el.clientTop, w: el.clientWidth, h: el.clientHeight });
  });
  return { vp: { w: window.innerWidth, h: window.innerHeight }, self, iframes: iframes.slice(0, 60) };
}

// What is on top at (x, y) of this frame, for a click by coordinates: refuses a
// point outside the page, or one where the page is not what the screenshot
// showed (`expect`, a top-frame element); an <iframe> is reported with its box so
// the worker can look inside.
export function probeAt(x, y, expect) {
  const D = globalThis.__jav3Dom;
  if (!(x >= 0 && y >= 0 && x < window.innerWidth && y < window.innerHeight)) {
    return { ok: false, code: 'outside', err: `${x},${y} is outside the page (${window.innerWidth}x${window.innerHeight} CSS px); take a new browser_screenshot_tab` };
  }
  const hit = D.deepPoint(document, x, y);
  if (expect && D.pointMoved(D.findJav3(document, expect.n), hit)) {
    return { ok: false, code: 'moved', err: 'the page moved since the screenshot — ' + JSON.stringify(expect.label || 'the element') + ' is no longer at that point; browser_screenshot_tab again' };
  }
  if (!hit) return { ok: false, err: 'nothing on the page at that point' };
  if (hit.tagName === 'IFRAME' || hit.tagName === 'FRAME') {
    const r = hit.getBoundingClientRect();
    return { ok: true, iframe: { src: hit.src || '', x: r.left + hit.clientLeft, y: r.top + hit.clientTop, w: hit.clientWidth, h: hit.clientHeight } };
  }
  return { ok: true, hit: D.describeEl(hit) };
}

// In a frame that holds the one we are clicking in: is the <iframe> still the
// topmost thing at (x, y), or has something been put over it?
export function hitIframe(x, y, src) {
  const D = globalThis.__jav3Dom;
  const inside = x >= 0 && y >= 0 && x < window.innerWidth && y < window.innerHeight;
  const hit = inside ? D.deepPoint(document, x, y) : null;
  if (hit && (hit.tagName === 'IFRAME' || hit.tagName === 'FRAME') && (!src || hit.src === src)) return { ok: true };
  return { ok: false, what: hit ? D.describeEl(hit) : 'nothing' };
}

// Listen (capture phase, so the page cannot hide it) for the mouse events of the
// click that is about to arrive in this frame; takeClick reads what they hit.
// `id` is the element a click by id aimed at (null for a click by coordinates).
export function armClick(id) {
  const D = globalThis.__jav3Dom;
  if (globalThis.__jav3Click) globalThis.__jav3Click.stop();
  const el = id ? D.findJav3(document, id) : null;
  const st = { down: null, click: null };
  const rec = e => {
    const t = (e.composedPath && e.composedPath()[0]) || e.target;
    const o = { x: e.clientX, y: e.clientY, trusted: e.isTrusted, hit: D.describeEl(t),
                onEl: el ? !D.pointMoved(el, t) : null };
    if (e.type === 'mousedown') { if (!st.down) st.down = o; } else if (!st.click) st.click = o;
  };
  window.addEventListener('mousedown', rec, true);
  window.addEventListener('click', rec, true);
  st.stop = () => {
    window.removeEventListener('mousedown', rec, true);
    window.removeEventListener('click', rec, true);
  };
  globalThis.__jav3Click = st;
  return { ok: true };
}

// lost: the listener is gone, i.e. the frame navigated (the click did something).
export function takeClick() {
  const st = globalThis.__jav3Click;
  if (!st) return { ok: true, lost: true };
  st.stop();
  globalThis.__jav3Click = null;
  return { ok: true, lost: false, down: st.down, click: st.click };
}

// browser_type with no element: into whatever has focus.
export function typeActive(text, submit) {
  const D = globalThis.__jav3Dom;
  const el = D.deepActive(document);
  if (!el || el === document.body || el === document.documentElement) {
    return { ok: false, code: 'no_focus', err: 'nothing is focused on that page — click the field first (browser_click by id or by x, y), then type' };
  }
  if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') return { ok: true, inFrame: true };
  return D.typeInto(window, document, el, text, submit);
}

export async function typeEl(id, text, submit) {
  const el = globalThis.__jav3Dom.findJav3(document, id);
  if (!el) return { ok: false, code: 'stale' };
  el.scrollIntoView({ block: 'center' });
  el.focus();
  if (el.isContentEditable) {
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, text);
  } else if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
    if (el.type === 'checkbox' || el.type === 'radio') {
      return { ok: false, err: 'that is a ' + el.type + '; use browser_click to change it' };
    }
    if (el.type === 'password' || el.type === 'file') {
      return { ok: false, err: 'Jav3 does not type into password or file fields' };
    }
    const proto = el.tagName === 'INPUT' ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, text);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  } else if (el.tagName === 'SELECT') {
    return { ok: false, err: 'that is a <select>; use browser_select with its value or label' };
  } else {
    return { ok: false, err: 'that element is not a text field' };
  }
  if (submit) await globalThis.__jav3Dom.submitOnce(window, document, el);
  return { ok: true };
}

// A native <select>: set the value the way a user's pick does (the prototype
// setter, so frameworks tracking the value see it) and fire input + change.
export function selectEl(id, value, label) {
  const D = globalThis.__jav3Dom;
  const el = D.findJav3(document, id);
  if (!el) return { ok: false, code: 'stale' };
  if (el.tagName !== 'SELECT') {
    const what = el.getAttribute('role') || el.tagName.toLowerCase();
    return { ok: false, code: 'not_select',
             err: `that element (${what}) is not a native <select>; open it with browser_click and click the option` };
  }
  const i = D.pickOption(el.options, value, label);
  if (i < 0) {
    const have = D.selectOptions(el, 20).options.map(o => o.t).join(', ');
    return { ok: false, err: `no option ${value != null ? 'with value ' + JSON.stringify(value) : JSON.stringify(label)}; options: ${have}` };
  }
  el.scrollIntoView({ block: 'center' });
  el.focus();
  const opt = el.options[i];
  Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(el, opt.value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { ok: true, text: `selected ${JSON.stringify(D.clean(opt.label || opt.text, 60))}` +
           (opt.value !== (opt.label || opt.text) ? ` (value ${JSON.stringify(D.clean(opt.value, 60))})` : '') };
}

export function hoverEl(id) {
  const el = globalThis.__jav3Dom.findJav3(document, id);
  if (!el) return { ok: false, code: 'stale' };
  el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
  const r = el.getBoundingClientRect();
  const at = { bubbles: true, cancelable: true, composed: true, view: window,
               clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
  el.dispatchEvent(new PointerEvent('pointerover', at));
  el.dispatchEvent(new PointerEvent('pointerenter', { ...at, bubbles: false }));
  el.dispatchEvent(new MouseEvent('mouseover', at));
  el.dispatchEvent(new MouseEvent('mouseenter', { ...at, bubbles: false }));
  el.dispatchEvent(new PointerEvent('pointermove', at));
  el.dispatchEvent(new MouseEvent('mousemove', at));
  return { ok: true, text: 'hovering (a page-script hover; CSS :hover does not apply to synthetic events)' };
}

// A key combo to the focused element. Synthetic KeyboardEvents do not carry
// the browser's default actions, so the ones that matter are done here, and
// only when the page did not preventDefault the keydown and no ctrl/alt/meta
// is held: Enter submits the owning form (requestSubmit) or activates a
// focused button/link, Escape closes an open <dialog>/<details> and blurs,
// Tab / shift+Tab moves focus to the next tabbable element, Space activates
// a button/checkbox, printable keys / Backspace / Delete edit a text field.
export function keyPress(combo) {
  const D = globalThis.__jav3Dom;
  const k = D.keySpec(combo);
  let el = D.deepActive(document);
  if (!el || el === document.documentElement) el = document.body;
  if (el && (el.tagName === 'IFRAME' || el.tagName === 'FRAME')) return { ok: true, inFrame: true };
  const init = { key: k.key, code: k.code, keyCode: k.keyCode, which: k.keyCode, ctrlKey: k.ctrlKey,
                 altKey: k.altKey, shiftKey: k.shiftKey, metaKey: k.metaKey,
                 bubbles: true, cancelable: true, composed: true, view: window };
  const tag = el ? el.tagName : '';
  const editable = el && (el.isContentEditable || tag === 'TEXTAREA' ||
    (tag === 'INPUT' && !/^(button|submit|reset|checkbox|radio|file|image|range|color)$/i.test(el.type)));
  const notPrevented = el.dispatchEvent(new KeyboardEvent('keydown', init));
  const plain = !k.ctrlKey && !k.altKey && !k.metaKey;
  const printable = k.key.length === 1 && plain;
  if (notPrevented && (printable || k.key === 'Enter')) {
    el.dispatchEvent(new KeyboardEvent('keypress', { ...init, charCode: k.key === 'Enter' ? 13 : k.key.charCodeAt(0) }));
  }
  let did = '';
  const desc = x => {
    const n = D.accessibleName(x);
    return x.tagName.toLowerCase() + (n ? ' ' + JSON.stringify(n.slice(0, 40)) : '');
  };
  if (notPrevented && plain) {
    if (k.key === 'Enter') {
      if (tag === 'TEXTAREA' || (el && el.isContentEditable)) {
        document.execCommand('insertLineBreak') || document.execCommand('insertText', false, '\n');
        did = 'new line';
      } else if (tag === 'INPUT' && el.form) {
        if (!D.implicitSubmit(el.form)) {
          did = 'no submit (the form has several fields and no submit button)';
        } else {
          if (typeof el.form.requestSubmit === 'function') el.form.requestSubmit(); else el.form.submit();
          did = 'submitted the form';
        }
      } else if (el && el.matches && el.matches('a[href], button, summary, [role=button], [role=link], [role=menuitem], [role=tab], [role=option]')) {
        el.click(); did = 'activated ' + desc(el);
      }
    } else if (k.key === 'Escape') {
      const dlg = el && el.closest && el.closest('dialog[open]');
      const det = el && el.closest && el.closest('details[open]');
      if (dlg && typeof dlg.close === 'function') { dlg.close(); did = 'closed the dialog'; }
      else if (det) { det.open = false; did = 'closed the details'; }
      if (el && el !== document.body && typeof el.blur === 'function') { el.blur(); did = did ? did + ', blurred' : 'blurred ' + desc(el); }
    } else if (k.key === 'Tab') {
      const items = [];
      D.deepEach(document, x => {
        if (!x.matches || !x.matches(D.TABBABLE) || x.disabled) return;
        const r = x.getBoundingClientRect();
        if (!r.width || !r.height) return;
        const st = getComputedStyle(x);
        if (st.visibility === 'hidden' || st.display === 'none') return;
        items.push({ el: x, tabIndex: x.tabIndex });
      });
      const order = D.tabOrder(items);
      const cur = items.findIndex(it => it.el === el);
      const next = D.nextInOrder(order, cur, k.shiftKey ? -1 : 1);
      if (next >= 0) {
        const t = items[next].el;
        t.focus();
        if (typeof t.select === 'function' && t.tagName === 'INPUT') { try { t.select(); } catch { /* not selectable */ } }
        did = 'focus moved to ' + desc(t) + (t.getAttribute('data-jav3-id') ? ` [#${t.getAttribute('data-jav3-id')}]` : '');
      } else did = 'nothing to tab to';
    } else if (k.key === ' ' && !editable && el && el.matches &&
               el.matches('button, summary, input[type=checkbox], input[type=radio], [role=button], [role=checkbox], [role=switch]')) {
      el.click(); did = 'activated ' + desc(el);
    } else if (editable && printable) {
      document.execCommand('insertText', false, k.key); did = 'typed ' + JSON.stringify(k.key);
    } else if (editable && k.key === 'Backspace') {
      document.execCommand('delete'); did = 'deleted back';
    } else if (editable && k.key === 'Delete') {
      document.execCommand('forwardDelete'); did = 'deleted forward';
    }
  }
  el.dispatchEvent(new KeyboardEvent('keyup', init));
  const on = el && el !== document.body ? desc(el) : 'the page';
  return { ok: true, text: `pressed ${k.combo} on ${on}` +
           (notPrevented ? '' : ' (the page handled it)') + (did ? '; ' + did : '') };
}

export function scrollToEl(id) {
  const el = globalThis.__jav3Dom.findJav3(document, id);
  if (!el) return { ok: false, code: 'stale' };
  el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
  const r = el.getBoundingClientRect();
  return { ok: true, inView: r.top < window.innerHeight && r.bottom > 0 };
}

export function scrollPage(pages) {
  window.scrollBy({ top: pages * window.innerHeight * 0.9, behavior: 'instant' });
  return { ok: true, y: Math.round(window.scrollY),
    max: Math.round(document.documentElement.scrollHeight - window.innerHeight) };
}
