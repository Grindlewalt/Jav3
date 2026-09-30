// node --test clients/jav3-browser/test/
// browser_type submit=true must submit exactly once, whoever handles Enter.
import test from 'node:test';
import assert from 'node:assert/strict';
import '../lib/dom.js';

const D = globalThis.__jav3Dom;

class KE { constructor(type, init = {}) { this.type = type; Object.assign(this, init); this.defaultPrevented = false; } }

function rig({ preventKeydown = false, keydownSubmits = false, keydownNavigates = false } = {}) {
  const docL = {}, winL = {};
  const add = (m, t, f) => { (m[t] ||= []).push(f); };
  const del = (m, t, f) => { m[t] = (m[t] || []).filter(x => x !== f); };
  const fire = (m, t) => (m[t] || []).slice().forEach(f => f({ type: t }));
  const doc = { addEventListener: (t, f) => add(docL, t, f), removeEventListener: (t, f) => del(docL, t, f) };
  const win = { KeyboardEvent: KE, location: { href: 'https://x/' },
    addEventListener: (t, f) => add(winL, t, f), removeEventListener: (t, f) => del(winL, t, f) };
  const seen = { submits: 0, requestSubmit: 0 };
  const form = { requestSubmit() { seen.requestSubmit++; seen.submits++; fire(docL, 'submit'); } };
  const el = { form, dispatchEvent(ev) {
    if (ev.type === 'keydown') {
      if (keydownSubmits) { seen.submits++; fire(docL, 'submit'); }
      if (keydownNavigates) fire(winL, 'beforeunload');
      if (preventKeydown) return false;
    }
    return true;
  } };
  return { win, doc, el, seen, docL, winL };
}

test('a page that handles Enter itself (preventDefault) is not submitted a second time', async () => {
  const r = rig({ preventKeydown: true, keydownSubmits: true });
  assert.equal((await D.submitOnce(r.win, r.doc, r.el, 5)).by, 'page');
  assert.equal(r.seen.submits, 1);
  assert.equal(r.seen.requestSubmit, 0);
});

test('a submit event during the window means the page submitted; so does a navigation', async () => {
  const a = rig({ keydownSubmits: true });
  await D.submitOnce(a.win, a.doc, a.el, 5);
  assert.equal(a.seen.requestSubmit, 0);
  const b = rig({ keydownNavigates: true });
  await D.submitOnce(b.win, b.doc, b.el, 5);
  assert.equal(b.seen.requestSubmit, 0);
  const c = rig();
  const p = D.submitOnce(c.win, c.doc, c.el, 20);
  c.win.location.href = 'https://x/next';
  await p;
  assert.equal(c.seen.requestSubmit, 0);
});

test('a page that ignores Enter gets exactly one requestSubmit, and listeners are removed', async () => {
  const r = rig();
  assert.equal((await D.submitOnce(r.win, r.doc, r.el, 5)).by, 'form');
  assert.equal(r.seen.requestSubmit, 1);
  assert.equal(r.seen.submits, 1);
  assert.deepEqual([r.docL.submit, r.winL.beforeunload, r.winL.pagehide], [[], [], []]);
});
