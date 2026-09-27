// Functions injected into Jav3's own tabs with chrome.scripting.executeScript.
// Each must be self-contained (it is serialized, not imported by the page) and
// is run once PER FRAME: the service worker enumerates the tab's frames and
// injects into each, then stitches the results together (element numbers are
// local to a frame; the worker prefixes them with the frame index -> "f2:5").

export function readPage(maxChars, selector) {
  const ATTR = 'data-jav3-id';
  document.querySelectorAll('[' + ATTR + ']').forEach(e => e.removeAttribute(ATTR));
  const sel = 'a[href], button, input:not([type=hidden]), textarea, select, ' +
    '[role=button], [role=link], [role=textbox], [role=checkbox], [role=tab], ' +
    '[role=menuitem], [contenteditable=""], [contenteditable=true], summary';
  const vw = window.innerWidth, vh = window.innerHeight;
  const els = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (els.length >= 300) break;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (!r.width || !r.height || st.visibility === 'hidden' || st.display === 'none') continue;
    if (el.disabled) continue;
    n += 1;
    el.setAttribute(ATTR, String(n));
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : '';
    const role = el.getAttribute('role') || '';
    const aria = el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || '';
    const text = (el.innerText || el.value || el.getAttribute('aria-label') ||
      el.placeholder || el.title || el.name || el.getAttribute('href') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 120);
    const inView = r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;
    els.push({
      n, tag, type, role,
      name: aria.replace(/\s+/g, ' ').trim().slice(0, 120),
      text,
      box: { x: Math.round(r.left), y: Math.round(r.top),
             w: Math.round(r.width), h: Math.round(r.height) },
      inView,
    });
  }
  const body = (document.body ? document.body.innerText : '')
    .replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  let probed = null;
  if (selector) { try { probed = !!document.querySelector(selector); } catch { probed = null; } }
  return { url: location.href, title: document.title,
           text: body.slice(0, maxChars + 1), elements: els, probed };
}

export function clickEl(id) {
  const el = document.querySelector('[data-jav3-id="' + String(Number(id)) + '"]');
  if (!el) return { ok: false, err: 'element ' + id + ' is gone — read the page again' };
  el.scrollIntoView({ block: 'center', inline: 'center' });
  if (typeof el.focus === 'function') el.focus();
  el.click();
  return { ok: true };
}

export function typeEl(id, text, submit) {
  const el = document.querySelector('[data-jav3-id="' + String(Number(id)) + '"]');
  if (!el) return { ok: false, err: 'element ' + id + ' is gone — read the page again' };
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
  } else {
    return { ok: false, err: 'element ' + id + ' is not a text field' };
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

export function scrollToEl(id) {
  const el = document.querySelector('[data-jav3-id="' + String(Number(id)) + '"]');
  if (!el) return { ok: false, err: 'element ' + id + ' is gone — read the page again' };
  el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
  const r = el.getBoundingClientRect();
  return { ok: true, inView: r.top < window.innerHeight && r.bottom > 0 };
}

export function scrollPage(pages) {
  window.scrollBy({ top: pages * window.innerHeight * 0.9, behavior: 'instant' });
  return { ok: true, y: Math.round(window.scrollY),
    max: Math.round(document.documentElement.scrollHeight - window.innerHeight) };
}
