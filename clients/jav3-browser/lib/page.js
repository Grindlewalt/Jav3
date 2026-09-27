// Functions injected into Jav3's own tabs with chrome.scripting.executeScript.
// Each must be self-contained (it is serialized, not imported by the page).

export function readPage(maxChars) {
  const ATTR = 'data-jav3-id';
  document.querySelectorAll('[' + ATTR + ']').forEach(e => e.removeAttribute(ATTR));
  const sel = 'a[href], button, input:not([type=hidden]), textarea, select, ' +
    '[role=button], [role=link], [role=textbox], [role=checkbox], [role=tab], ' +
    '[role=menuitem], [contenteditable=""], [contenteditable=true], summary';
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
    const kind = el.getAttribute('role') ||
      (tag === 'input' ? 'input:' + (el.type || 'text') : tag);
    const label = (el.getAttribute('aria-label') || el.innerText || el.value ||
      el.placeholder || el.title || el.name || el.getAttribute('href') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 80);
    els.push([n, kind, label]);
  }
  const text = (document.body ? document.body.innerText : '')
    .replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  return { url: location.href, title: document.title, text: text.slice(0, maxChars + 1),
    elements: els };
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

export function scrollPage(pages) {
  window.scrollBy({ top: pages * window.innerHeight * 0.9, behavior: 'instant' });
  return { ok: true, y: Math.round(window.scrollY),
    max: Math.round(document.documentElement.scrollHeight - window.innerHeight) };
}
