// node --test clients/jav3-browser/test/
// The page half of trusted clicks (lib/page.js: measureEl, probeAt, hitIframe,
// frameInfo, armClick / takeClick) against a very small fake window + document.
import test from 'node:test';
import assert from 'node:assert/strict';
import '../lib/dom.js';
import { measureEl, probeAt, hitIframe, frameInfo, armClick, takeClick } from '../lib/page.js';

// --- a tiny fake page ---------------------------------------------------------------------
// elements are stacked in the order given (the last is on top); rect = [left, top, w, h]
function page(specs, { vw = 1000, vh = 800 } = {}) {
  const els = specs.map(s => {
    const [left, top, width, height] = s.rect;
    const e = {
      nodeType: 1, tagName: s.tag.toUpperCase(), attrs: { ...(s.attrs || {}) }, childNodes: [], parentNode: null,
      src: s.src, clientLeft: s.border || 0, clientTop: s.border || 0, clientWidth: width, clientHeight: height,
      getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
      setAttribute(k, v) { this.attrs[k] = String(v); },
      hasAttribute(k) { return k in this.attrs; },
      getBoundingClientRect: () => ({ left, top, width, height }),
      scrollIntoView() {},
      contains(o) { for (let n = o; n; n = n.parentNode) if (n === this) return true; return false; },
      closest() { return null; },
      innerText: s.text || '',
    };
    return e;
  });
  els.forEach((e, i) => { if (specs[i].inside != null) { e.parentNode = els[specs[i].inside]; els[specs[i].inside].childNodes.push(e); } });
  const doc = {
    querySelectorAll: sel => (sel === '*' ? els : []),
    querySelector: sel => {
      const m = /^\[data-jav3-id="(\d+)"\]$/.exec(sel);
      return m ? els.find(e => e.attrs['data-jav3-id'] === m[1]) || null : null;
    },
    elementFromPoint: (x, y) => [...els].reverse().find(e => {
      const r = e.getBoundingClientRect();
      return x >= r.left && x < r.left + r.width && y >= r.top && y < r.top + r.height;
    }) || null,
  };
  const listeners = {};
  const win = {
    innerWidth: vw, innerHeight: vh, frameElement: null,
    addEventListener: (t, f) => { (listeners[t] ||= []).push(f); },
    removeEventListener: (t, f) => { listeners[t] = (listeners[t] || []).filter(x => x !== f); },
  };
  globalThis.window = win;
  globalThis.document = doc;
  const fire = (type, target, x, y) => (listeners[type] || []).slice().forEach(f =>
    f({ type, target, clientX: x, clientY: y, isTrusted: true, composedPath: () => [target] }));
  return { els, doc, win, listeners, fire };
}

const btn = (id, rect, label) => ({ tag: 'button', attrs: { 'data-jav3-id': String(id), 'aria-label': label }, rect });

test('measureEl: the clickable point, stale ids, hidden elements', () => {
  page([btn(1, [100, 100, 80, 40], 'Save')]);
  const m = measureEl(1);
  assert.deepEqual([m.ok, m.pt, m.vp, m.label, m.covered], [true, { x: 140, y: 120 }, { w: 1000, h: 800 }, 'button "Save"', undefined]);
  assert.deepEqual(measureEl(9), { ok: false, code: 'stale' });
  page([btn(1, [100, 900, 80, 40], 'Below')]);
  assert.equal(measureEl(1).code, 'offscreen');
});

test('measureEl: something laid over the centre is named and tagged so it can be clicked instead', () => {
  const p = page([btn(1, [100, 100, 80, 40], 'Save'),
    { tag: 'div', attrs: { role: 'dialog', 'aria-label': 'Accept cookies' }, rect: [0, 90, 1000, 80] }]);
  const m = measureEl(1);
  assert.equal(m.ok, true);
  assert.deepEqual(m.covered, { n: 1 + 1, name: 'Accept cookies' });         // numbered after the highest id in the page
  assert.equal(p.els[1].attrs['data-jav3-id'], '2');
  // a wrapper's child on top of its own centre is not "covered"
  page([{ tag: 'div', attrs: { 'data-jav3-id': '1' }, rect: [100, 100, 200, 50] }, { tag: 'iframe', rect: [100, 100, 200, 50], inside: 0 }]);
  assert.equal(measureEl(1).covered, undefined);
});

test('probeAt: outside, moved, an iframe with its content box, or what is there', () => {
  page([btn(1, [10, 10, 100, 30], 'Go'), { tag: 'iframe', src: 'https://accounts.google.com/gsi', rect: [200, 300, 220, 64], border: 2 }]);
  assert.equal(probeAt(2000, 5, null).code, 'outside');
  assert.equal(probeAt(-1, 5, null).code, 'outside');
  assert.deepEqual(probeAt(50, 20, null), { ok: true, hit: 'button "Go"' });
  assert.deepEqual(probeAt(50, 20, { n: 1, label: 'Go' }), { ok: true, hit: 'button "Go"' });
  assert.equal(probeAt(300, 330, null).iframe.src, 'https://accounts.google.com/gsi');
  assert.deepEqual([probeAt(300, 330, null).iframe.x, probeAt(300, 330, null).iframe.y], [202, 302]);   // border skipped
  const moved = probeAt(50, 20, { n: 7, label: 'Gone' });                    // the element the screenshot showed is not on the page
  assert.deepEqual([moved.ok, moved.code], [false, 'moved']);
  assert.match(moved.err, /"Gone" is no longer at that point/);
  assert.equal(probeAt(900, 700, null).err, 'nothing on the page at that point');
});

test('hitIframe: still on top, or covered', () => {
  page([{ tag: 'iframe', src: 'https://f.example/', rect: [0, 0, 400, 300] }]);
  assert.deepEqual(hitIframe(10, 10, 'https://f.example/'), { ok: true });
  assert.deepEqual(hitIframe(10, 10, ''), { ok: true });                     // same-origin parent: no src to compare
  assert.equal(hitIframe(10, 10, 'https://other.example/').ok, false);
  page([{ tag: 'iframe', src: 'https://f.example/', rect: [0, 0, 400, 300] }, { tag: 'div', attrs: { 'aria-label': 'Banner' }, rect: [0, 0, 400, 100] }]);
  assert.deepEqual(hitIframe(10, 10, 'https://f.example/'), { ok: false, what: 'div "Banner"' });
  assert.deepEqual(hitIframe(5000, 10, 'x'), { ok: false, what: 'nothing' });
});

test('frameInfo: the iframes in this frame and, when same-origin, this frame\'s own box', () => {
  const p = page([{ tag: 'iframe', src: 'https://f.example/', rect: [30, 40, 220, 64], border: 2 }]);
  const i = frameInfo();
  assert.deepEqual(i.vp, { w: 1000, h: 800 });
  assert.equal(i.self, null);
  assert.deepEqual(i.iframes, [{ src: 'https://f.example/', x: 32, y: 42, w: 220, h: 64 }]);
  p.win.frameElement = { getBoundingClientRect: () => ({ left: 5, top: 6 }), clientLeft: 1, clientTop: 1, clientWidth: 300, clientHeight: 200 };
  assert.deepEqual(frameInfo().self, { x: 6, y: 7, w: 300, h: 200 });
  Object.defineProperty(p.win, 'frameElement', { get() { throw new Error('SecurityError'); } });   // cross-origin parent
  assert.equal(frameInfo().self, null);
});

test('armClick / takeClick: what the mouse hit, whether it was the element, and a lost listener', () => {
  const p = page([btn(1, [0, 0, 100, 40], 'Save'), { tag: 'div', attrs: { 'aria-label': 'Overlay' }, rect: [200, 0, 100, 40] }]);
  assert.deepEqual(takeClick(), { ok: true, lost: true });                   // never armed (or the frame navigated)
  armClick(1);
  p.fire('mousedown', p.els[0], 50, 20);
  p.fire('mouseup', p.els[0], 50, 20);
  p.fire('click', p.els[0], 50, 20);
  const got = takeClick();
  assert.deepEqual([got.lost, got.down.hit, got.down.onEl, got.down.trusted, got.click.onEl], [false, 'button "Save"', true, true, true]);
  assert.equal(got.seen, 3);                                                // mousedown, mouseup, click
  assert.deepEqual(Object.values(p.listeners).flat(), []);                  // listeners removed
  assert.deepEqual(takeClick(), { ok: true, lost: true });

  armClick(1);                                                              // the mouse landed on something else
  p.fire('mousedown', p.els[1], 250, 20);
  assert.equal(takeClick().down.onEl, false);

  armClick(null);                                                           // by coordinates: no element to compare
  armClick(null);                                                           // arming again replaces the first listener
  assert.equal(p.listeners.mousedown.length, 1);
  assert.equal(p.listeners.click.length, 1);
  p.fire('mousedown', p.els[1], 250, 20);
  const c = takeClick();
  assert.deepEqual([c.down.hit, c.down.onEl, c.click], ['div "Overlay"', null, null]);
  armClick(1);
  assert.deepEqual(takeClick(), { ok: true, lost: false, seen: 0, down: null, click: null });   // armed, nothing arrived

  armClick(1);                                                              // a page that swallows mousedown / click at the window
  p.fire('mousemove', p.els[0], 50, 20);
  p.fire('mouseup', p.els[0], 50, 20);
  const swallowed = takeClick();
  assert.deepEqual([swallowed.seen, swallowed.down, swallowed.click], [2, null, null]);   // the mouse still reached the page
});
