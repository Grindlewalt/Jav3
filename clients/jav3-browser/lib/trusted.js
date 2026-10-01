// Trusted clicks: a click delivered as real mouse input through chrome.debugger
// (CDP Input.dispatchMouseEvent), so pages that need user activation (a sign-in
// popup, a file chooser) or check isTrusted accept it. A script-made click
// (lib/dom.js realClick) is the fallback.
//
// Everything that touches chrome.* arrives in `d`, the dependencies sw.js
// builds, so node can drive the whole flow with fakes (test/trusted.test.mjs):
//
//   d.unavailable()            -> 'no_permission' | 'disabled' | null
//   d.attach(tabId) / d.detach(tabId)
//   d.send(tabId, params)      one Input.dispatchMouseEvent (sw.js refuses any other method)
//   d.inject(frameId, fn, args)  run a lib/page.js function in a frame
//   d.consent(url)             ask the operator to allow a frame's site (throws VerbError)
//   d.isDenied(url)            a frame of the Jav3 server itself
//   d.sleep(ms) d.now() d.cancelled()
//
// The debugger is attached only for the click and detached right after: Chrome
// shows "Jav3 Browser started debugging this browser" for exactly that long
// (the time comes back as debugMs and is reported with every click). Attaching
// puts that bar above the page and resizes the viewport under it, so geometry
// is measured AFTER the attach, once it holds still.
//
// Each function returns one of
//   { fallback: why, detail? }    nothing was clicked; the caller clicks with script events
//   { ok: true, text, ... }       clicked, as real input
//   { ok: false, code, err, ... } refused before the click (stale / covered / moved ...)
// and throws only when the click itself failed part-way (it may have happened).
import { frameConsentNeeded, hostOf, VerbError } from './verbs.js';
import { measureEl, frameInfo, probeAt, hitIframe, armClick, takeClick } from './page.js';

const SETTLE_TRIES = 10;
const STEP_MS = 70;
const BAR_MS = 60;           // the bar slides in and the page resizes under it
const HOVER_MS = 40;         // pointer moves, then presses
const HOLD_MS = 30;          // between press and release
const ARRIVE_MS = 100;       // let the page's click handlers run before reading what hit
const MAX_DEPTH = 5;

export const normUrl = u => { try { const x = new URL(u); x.hash = ''; return x.href; } catch { return String(u || ''); } };
const r1 = n => Math.round(n * 10) / 10;
const errText = e => String((e && e.message) || e).slice(0, 160);

// The <iframe> in a parent's list that holds the child frame at `url`: the same
// src; when several share it, the one the child's viewport size fits. null when
// none or still more than one (the frame is not placed).
export function matchIframe(url, iframes, vp) {
  const want = normUrl(url);
  let c = (iframes || []).filter(i => i.src && normUrl(i.src) === want);
  if (c.length > 1 && vp) {
    const fit = c.filter(i => Math.abs(i.w - vp.w) <= 1 && Math.abs(i.h - vp.h) <= 1);
    if (fit.length) c = fit;
  }
  return c.length === 1 ? { x: c[0].x, y: c[0].y, w: c[0].w, h: c[0].h, src: c[0].src } : null;
}

// A point in a frame's viewport -> the top viewport: add each enclosing
// <iframe>'s content-box offset on the way up. chain[i] = { box, vp, src,
// parentFrameId } for the i-th enclosing frame, innermost first (vp is that
// parent's viewport). `levels[i]` is the point in the i-th parent's viewport;
// inView is false when it falls outside any of them.
export function composeChain(pt, chain) {
  let x = pt.x, y = pt.y, inView = true;
  const levels = [];
  for (const L of chain) {
    x += L.box.x; y += L.box.y;
    levels.push({ frameId: L.parentFrameId, x: r1(x), y: r1(y), src: L.src });
    if (!(x >= 0 && y >= 0 && x < L.vp.w && y < L.vp.h)) inView = false;
  }
  return { x: r1(x), y: r1(y), levels, inView };
}

const close = (a, b) => Math.abs(a - b) <= 0.5;
export function sameMeasure(a, b) {
  return !!(a && b && a.ok && b.ok && close(a.pt.x, b.pt.x) && close(a.pt.y, b.pt.y) &&
            close(a.vp.w, b.vp.w) && close(a.vp.h, b.vp.h));
}
export function sameProbe(a, b) {
  if (!(a && b && a.ok && b.ok)) return false;
  if (!a.iframe || !b.iframe) return !a.iframe && !b.iframe && a.hit === b.hit;
  return a.iframe.src === b.iframe.src && close(a.iframe.x, b.iframe.x) && close(a.iframe.y, b.iframe.y);
}

// Measure until two reads in a row agree (the viewport has finished resizing
// under the bar), or give up after SETTLE_TRIES and use the last one.
async function settled(d, measure, same) {
  let prev = null, cur = null;
  for (let i = 0; i < SETTLE_TRIES; i++) {
    cur = await measure();
    if (!cur || !cur.ok) return cur;
    if (prev && same(prev, cur)) return cur;
    prev = cur;
    await d.sleep(STEP_MS);
  }
  return cur;
}

// The enclosing frames of `frameId`, innermost first: for each, the <iframe>
// that holds the child (its own window.frameElement when the parent is
// same-origin, else the parent's <iframe> with the child's url). err when a
// step cannot be placed.
export async function frameChain(d, frames, frameId) {
  const chain = [];
  let cur = frames.find(f => f.frameId === frameId);
  if (!cur) return { err: 'frame_unplaced' };
  let curInfo = frameId === 0 ? null : await d.inject(frameId, frameInfo, []);
  for (let guard = 0; cur.frameId !== 0; guard++) {
    if (guard >= MAX_DEPTH) return { err: 'frame_unplaced' };
    const parent = frames.find(f => f.frameId === cur.parentFrameId);
    if (!parent || !curInfo) return { err: 'frame_unplaced' };
    const pInfo = await d.inject(parent.frameId, frameInfo, []);
    if (!pInfo) return { err: 'frame_unplaced' };
    const box = curInfo.self ? { ...curInfo.self, src: '' } : matchIframe(cur.url, pInfo.iframes, curInfo.vp);
    if (!box) return { err: 'frame_unplaced' };
    chain.push({ box, vp: pInfo.vp, src: box.src, parentFrameId: parent.frameId });
    cur = parent; curInfo = pInfo;
  }
  return { chain };
}

// Attach, run body(send), detach. The time between attach and detach is when
// Chrome shows its debugging bar. A failed attach is a fallback (DevTools
// attached by someone else, the tab in a state that refuses it).
async function withDebugger(d, tabId, body) {
  const t0 = d.now();
  try { await d.attach(tabId); } catch (e) { return { fallback: 'attach_failed', detail: errText(e) }; }
  let out, err = null;
  try { out = await body(params => d.send(tabId, params)); } catch (e) { err = e; }
  try { await d.detach(tabId); } catch { /* already gone (the operator closed the bar) */ }
  const debugMs = d.now() - t0;
  if (err) { err.debugMs = debugMs; throw err; }
  return { ...out, debugMs };
}

// mouseMoved, then pressed + released (a left click); `arm` ran before. Before
// the press nothing has happened yet, so a failure there is a fallback; after
// it the click may have half happened, so that throws.
async function mouseClick(d, send, x, y) {
  if (d.cancelled()) throw Object.assign(new Error('cancelled'), { cancelled: true });
  try { await send({ type: 'mouseMoved', x, y, button: 'none', buttons: 0 }); }
  catch (e) { return { fallback: 'input_failed', detail: errText(e) }; }
  await d.sleep(HOVER_MS);
  if (d.cancelled()) throw Object.assign(new Error('cancelled'), { cancelled: true });
  try {
    await send({ type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 });
    await d.sleep(HOLD_MS);
    await send({ type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1 });
  } catch (e) {
    try { await send({ type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1 }); } catch { /* gone */ }
    throw Object.assign(new VerbError(`the mouse click failed part-way (${errText(e)}); browser_read_page to see whether it happened`, 'failed'),
      { extra: { via: 'trusted', via_why: 'press_failed' } });
  }
  return null;
}

// What the listener armed in the frame saw. null = a fallback is still safe
// (no mouse event reached the page, so nothing happened).
async function arrived(d, frameId) {
  await d.sleep(ARRIVE_MS);
  let got = null;
  try { got = await d.inject(frameId, takeClick, []); } catch { got = { ok: true, lost: true }; }
  if (!got || got.lost) return { lost: true };            // the frame navigated: the click did something
  if (!got.seen && !got.down && !got.click) return null;   // nothing reached the page
  return got;
}

const noEvent = { fallback: 'no_event', detail: 'the page got no mouse event (is the tab hidden?)' };

// --- a click by element id ------------------------------------------------------------------
// req: { tabId, frameId, n, frames }
export async function clickElementTrusted(d, req) {
  const off = d.unavailable();
  if (off) return { fallback: off };
  const { tabId, frameId, n, frames } = req;
  return withDebugger(d, tabId, async send => {
    await d.sleep(BAR_MS);
    const m = await settled(d, () => d.inject(frameId, measureEl, [n]), sameMeasure);
    if (!m) return { fallback: 'input_failed', detail: 'the frame cannot be scripted right now' };
    if (!m.ok && m.code === 'offscreen') return { fallback: 'offscreen', detail: m.err };   // no area to aim at: a script click may still reach it
    if (!m.ok) return m;                                   // stale
    if (m.covered) {
      return { ok: false, code: 'covered', cover: m.covered.n, coverName: m.covered.name,
               err: 'element is covered by another element' };
    }
    let at = { x: m.pt.x, y: m.pt.y, levels: [] };
    if (frameId !== 0) {
      const ch = await frameChain(d, frames, frameId);
      if (ch.err) return { fallback: ch.err, detail: 'could not tell where that iframe sits on the page' };
      at = composeChain(m.pt, ch.chain);
      if (!at.inView) return { fallback: 'offscreen', detail: 'the element is outside the visible page' };
      for (const L of at.levels) {                         // nothing laid over the iframe itself
        const h = await d.inject(L.frameId, hitIframe, [L.x, L.y, L.src]);
        if (!h || !h.ok) {
          return { ok: false, code: 'covered', cover: null, coverName: (h && h.what) || 'another element',
                   err: 'the iframe holding the element is covered by another element' };
        }
      }
    }
    await d.inject(frameId, armClick, [n]);
    try {
      const fb = await mouseClick(d, send, at.x, at.y);
      if (fb) return fb;
      const got = await arrived(d, frameId);
      if (!got) return noEvent;
      let text = `clicked ${m.label}`;
      if (got.down && got.down.onEl === false) {
        text += `; the mouse landed on ${got.down.hit}, not on it — something is over it`;
      } else if (!got.lost && !got.down && !got.click) {
        text += '; the page took the mouse events itself (no mousedown or click reached Jav3)';
      }
      return { ok: true, text, trusted: true, lost: !!got.lost };
    } finally {
      try { await d.inject(frameId, takeClick, []); } catch { /* frame gone */ }
    }
  });
}

// --- a click by coordinates (top viewport, CSS px) -----------------------------------------------
// Which frame is under (x, y), descending through <iframe>s: { ok, frameId, hit,
// srcs (every iframe on the way), verify (false when the innermost iframe could
// not be matched to a frame: the click still goes through, we just cannot watch it) }.
export async function locate(d, frames, x, y, expect) {
  let frameId = 0, lx = x, ly = y;
  const srcs = [];
  for (let depth = 0; depth < MAX_DEPTH; depth++) {
    let r = null;
    try { r = await d.inject(frameId, probeAt, [lx, ly, depth === 0 ? (expect || null) : null]); } catch { r = null; }
    if (!r) {
      if (depth === 0) return { ok: false, err: 'that page cannot be scripted right now' };
      return { ok: true, frameId: -1, hit: 'an iframe', srcs, verify: false };
    }
    if (!r.ok) return r;
    if (!r.iframe) return { ok: true, frameId, hit: r.hit, srcs, verify: true };
    srcs.push(r.iframe.src);
    const kids = frames.filter(f => f.parentFrameId === frameId && r.iframe.src &&
                                    normUrl(f.url) === normUrl(r.iframe.src));
    if (kids.length !== 1) return { ok: true, frameId: -1, hit: 'an iframe', srcs, verify: false };
    lx -= r.iframe.x; ly -= r.iframe.y;
    frameId = kids[0].frameId;
  }
  return { ok: true, frameId: -1, hit: 'an iframe', srcs, verify: false };
}

// req: { tabId, x, y, expect, frames, topHost }
export async function clickPointTrusted(d, req) {
  const off = d.unavailable();
  if (off) return { fallback: off };
  const { tabId, x, y, expect, frames, topHost } = req;
  // Before the bar goes up: which frames are under the point, and may we go in?
  const first = await locate(d, frames, x, y, expect);
  if (!first.ok) return first;
  for (const src of first.srcs) {
    if (src && d.isDenied(src)) {
      return { ok: false, err: 'that point is inside a frame of the Jav3 server itself; Jav3 never uses it' };
    }
    if (frameConsentNeeded(topHost, hostOf(src))) await d.consent(src);
  }
  return withDebugger(d, tabId, async send => {
    await d.sleep(BAR_MS);
    const loc = await settled(d, () => locate(d, frames, x, y, expect), (a, b) =>
      a.ok && b.ok && a.frameId === b.frameId && a.hit === b.hit && a.srcs.join('|') === b.srcs.join('|'));
    if (!loc.ok) {
      // the bar took the bottom of the window: a point that was on screen is not now
      if (loc.code === 'outside') return { fallback: 'outside_after_attach', detail: loc.err };
      return loc;
    }
    if (loc.frameId !== first.frameId) {
      return { ok: false, code: 'moved', err: 'the page changed while the click was set up; browser_screenshot_tab again' };
    }
    const watch = loc.verify;
    if (watch) await d.inject(loc.frameId, armClick, [null]);
    try {
      const fb = await mouseClick(d, send, x, y);
      if (fb) return fb;
      let landed = loc.hit, lost = false;
      if (watch) {
        const got = await arrived(d, loc.frameId);
        if (!got) return noEvent;
        lost = !!got.lost;
        if (got.down) landed = got.down.hit;
      }
      const inFrame = loc.srcs.length ? ` (inside the iframe from ${hostOf(loc.srcs[loc.srcs.length - 1]) || 'this page'})` : '';
      return { ok: true, text: `clicked ${landed}${inFrame}`, trusted: true, lost, frameId: loc.frameId };
    } finally {
      if (watch) { try { await d.inject(loc.frameId, takeClick, []); } catch { /* frame gone */ } }
    }
  });
}
