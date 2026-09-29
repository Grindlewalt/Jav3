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
           sig: D.signature(raw, all.length),
           viewport: { w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio || 1 } };
}

// The same signature readPage reports, without re-labelling anything.
export function pageSig() {
  const D = globalThis.__jav3Dom;
  return D.signature(document.body ? document.body.innerText : '', D.collect(document, window).length);
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

export function clickEl(id) {
  const el = globalThis.__jav3Dom.findJav3(document, id);
  if (!el) return { ok: false, code: 'stale' };
  el.scrollIntoView({ block: 'center', inline: 'center' });
  if (typeof el.focus === 'function') el.focus();
  el.click();
  return { ok: true };
}

export function typeEl(id, text, submit) {
  const el = globalThis.__jav3Dom.findJav3(document, id);
  if (!el) return { ok: false, code: 'stale' };
  el.scrollIntoView({ block: 'center' });
  el.focus();
  if (el.isContentEditable) {
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, text);
  } else if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
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
  if (submit) {
    const opts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true };
    el.dispatchEvent(new KeyboardEvent('keydown', opts));
    el.dispatchEvent(new KeyboardEvent('keyup', opts));
    if (el.form) {
      if (typeof el.form.requestSubmit === 'function') el.form.requestSubmit();
      else el.form.submit();
    }
  }
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
        if (typeof el.form.requestSubmit === 'function') el.form.requestSubmit(); else el.form.submit();
        did = 'submitted the form';
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
