// node --test clients/jav3-browser/test/
// Trusted clicks (lib/trusted.js): the frame-offset math, the choice between real
// mouse input and the script fallback, the order of debugger commands, and the
// words of what happened, driven with a fake chrome (no browser needed).
import test from 'node:test';
import assert from 'node:assert/strict';
import '../lib/dom.js';
import {
  matchIframe, composeChain, sameMeasure, sameProbe, normUrl, frameChain,
  clickElementTrusted, clickPointTrusted,
} from '../lib/trusted.js';
import { VerbError } from '../lib/verbs.js';

const D = globalThis.__jav3Dom;

// --- a fake browser -----------------------------------------------------------------------
// `pages[frameId]` answers the lib/page.js functions by name; `log` records every
// debugger and page call in order.
function fake(pages = {}, opts = {}) {
  const log = [];
  let t = 1000;
  const answers = { ...pages };
  const d = {
    log,
    unavailable: () => opts.unavailable || null,
    attach: async id => { log.push('attach'); if (opts.attachFails) throw new Error(opts.attachFails); },
    detach: async id => { log.push('detach'); },
    send: async (id, params) => {
      log.push(params.type === 'mouseMoved' ? `move ${params.x},${params.y}`
        : params.type === 'mousePressed' ? `press ${params.x},${params.y}` : `release ${params.x},${params.y}`);
      if (opts.failOn === params.type) throw new Error('Detached while handling command');
    },
    inject: async (frameId, fn, args) => {
      log.push(`${fn.name}@${frameId}`);
      const a = answers[frameId] && answers[frameId][fn.name];
      return typeof a === 'function' ? a(...args) : a;
    },
    consent: async url => { log.push('consent ' + url); if (opts.denyConsent) throw new VerbError('the operator denied Jav3 on ' + url); },
    isDenied: url => /jav3\.lan/.test(url),
    sleep: async ms => { t += ms; },
    now: () => t,
    cancelled: () => !!(opts.cancelledAfter != null && log.filter(l => l.startsWith('move')).length >= 0 && opts.cancelledAfter()),
  };
  return d;
}
const cmds = log => log.filter(l => /^(attach|detach|move|press|release)/.test(l));

const topPage = (extra = {}) => ({
  measureEl: () => ({ ok: true, pt: { x: 100, y: 50 }, vp: { w: 1280, h: 800 }, label: 'button "Save"' }),
  armClick: () => ({ ok: true }),
  takeClick: () => ({ ok: true, lost: false, down: { x: 100, y: 50, trusted: true, hit: 'button "Save"', onEl: true }, click: { onEl: true } }),
  ...extra,
});

// --- the math ------------------------------------------------------------------------------

test('clickPoint is the centre of the visible part', () => {
  const r = (left, top, width, height) => ({ left, top, width, height });
  assert.deepEqual(D.clickPoint(r(10, 20, 100, 40), 800, 600), { x: 60, y: 40 });
  assert.deepEqual(D.clickPoint(r(-50, 20, 100, 40), 800, 600), { x: 25, y: 40 });     // half off the left edge
  assert.deepEqual(D.clickPoint(r(700, 500, 200, 200), 800, 600), { x: 750, y: 550 });  // off the corner
  assert.equal(D.clickPoint(r(10, 700, 100, 40), 800, 600), null);                     // below the fold
  assert.equal(D.clickPoint(r(10, 20, 0, 40), 800, 600), null);                        // zero width
});

test('matchIframe: by src, then by size when several share it', () => {
  const list = [
    { src: 'https://accounts.google.com/gsi/button?a=1#x', x: 10, y: 20, w: 200, h: 44 },
    { src: 'https://ads.example.net/', x: 0, y: 500, w: 300, h: 250 },
  ];
  assert.deepEqual(matchIframe('https://accounts.google.com/gsi/button?a=1', list, { w: 200, h: 44 }),
    { x: 10, y: 20, w: 200, h: 44, src: list[0].src });          // the hash is not part of the match
  assert.equal(matchIframe('https://nowhere.example/', list, { w: 1, h: 1 }), null);
  assert.equal(matchIframe('', list, null), null);
  const two = [{ src: 'https://w.example/', x: 0, y: 0, w: 100, h: 100 }, { src: 'https://w.example/', x: 0, y: 300, w: 400, h: 100 }];
  assert.equal(matchIframe('https://w.example/', two, null), null);                           // ambiguous
  assert.deepEqual(matchIframe('https://w.example/', two, { w: 400, h: 100 }).y, 300);       // the size decides
  assert.equal(normUrl('https://a.example/p#frag'), 'https://a.example/p');
});

test('composeChain adds each enclosing iframe offset and checks every level is visible', () => {
  // element at 30,10 in a frame at 100,200 inside a frame at 5,7 in the top page
  const chain = [
    { box: { x: 5, y: 7 }, vp: { w: 400, h: 300 }, src: 'https://b/', parentFrameId: 4 },
    { box: { x: 100, y: 200 }, vp: { w: 1280, h: 800 }, src: 'https://a/', parentFrameId: 0 },
  ];
  const c = composeChain({ x: 30, y: 10 }, chain);
  assert.deepEqual([c.x, c.y, c.inView], [135, 217, true]);
  assert.deepEqual(c.levels.map(l => [l.frameId, l.x, l.y]), [[4, 35, 17], [0, 135, 217]]);
  // the same point where the middle frame's viewport is only 15 px tall: not visible
  assert.equal(composeChain({ x: 30, y: 10 }, [{ ...chain[0], vp: { w: 400, h: 15 } }, chain[1]]).inView, false);
  assert.deepEqual(composeChain({ x: 3.04, y: 4 }, []), { x: 3, y: 4, levels: [], inView: true });
});

test('sameMeasure / sameProbe tolerate half a pixel and nothing more', () => {
  const m = (x, y, w = 1280, h = 800) => ({ ok: true, pt: { x, y }, vp: { w, h } });
  assert.equal(sameMeasure(m(10, 10), m(10.4, 10)), true);
  assert.equal(sameMeasure(m(10, 10), m(10, 11)), false);
  assert.equal(sameMeasure(m(10, 10), m(10, 10, 1280, 760)), false);      // the viewport shrank under the bar
  assert.equal(sameMeasure(m(10, 10), { ok: false }), false);
  const p = (hit, iframe) => ({ ok: true, hit, iframe });
  assert.equal(sameProbe(p('a'), p('a')), true);
  assert.equal(sameProbe(p('a'), p('b')), false);
  assert.equal(sameProbe(p(undefined, { src: 's', x: 1, y: 1 }), p(undefined, { src: 's', x: 1.2, y: 1 })), true);
  assert.equal(sameProbe(p('a'), p(undefined, { src: 's', x: 1, y: 1 })), false);
});

// --- a click by element id --------------------------------------------------------------------

test('top-frame element: attach, measure, arm, move, press, release, read, detach', async () => {
  const d = fake({ 0: topPage() });
  const r = await clickElementTrusted(d, { tabId: 7, frameId: 0, n: 3, frames: [{ frameId: 0, parentFrameId: -1, url: 'https://x.test/' }] });
  assert.equal(r.ok, true);
  assert.equal(r.text, 'clicked button "Save"');
  assert.equal(r.trusted, true);
  assert.ok(r.debugMs > 0);
  assert.deepEqual(cmds(d.log), ['attach', 'move 100,50', 'press 100,50', 'release 100,50', 'detach']);
  // the geometry is measured after the attach, and the listener is armed before the mouse moves
  const i = n => d.log.findIndex(l => l.startsWith(n));
  assert.ok(i('attach') < i('measureEl') && i('measureEl') < i('armClick') && i('armClick') < i('move'));
  assert.ok(i('release') < i('takeClick') && d.log.lastIndexOf('detach') === d.log.length - 1);
});

test('the geometry is measured again until it holds still (the bar resizes the viewport)', async () => {
  const reads = [
    { ok: true, pt: { x: 100, y: 50 }, vp: { w: 1280, h: 800 }, label: 'a' },
    { ok: true, pt: { x: 100, y: 30 }, vp: { w: 1280, h: 760 }, label: 'a' },     // after the bar
    { ok: true, pt: { x: 100, y: 30 }, vp: { w: 1280, h: 760 }, label: 'a' },
  ];
  const d = fake({ 0: topPage({ measureEl: () => reads.shift() || reads[0] }) });
  const r = await clickElementTrusted(d, { tabId: 7, frameId: 0, n: 3, frames: [] });
  assert.equal(r.ok, true);
  assert.deepEqual(cmds(d.log).slice(1, 4), ['move 100,30', 'press 100,30', 'release 100,30']);
  assert.equal(d.log.filter(l => l.startsWith('measureEl')).length, 3);
});

test('an element in a cross-origin iframe: the iframe box from its parent is added', async () => {
  const frames = [
    { frameId: 0, parentFrameId: -1, url: 'https://deltamath.com/sign-in' },
    { frameId: 5, parentFrameId: 0, url: 'https://accounts.google.com/gsi/button?x=1' },
  ];
  const d = fake({
    0: {
      // the GSI iframe's content box in the parent's viewport
      frameInfo: () => ({ vp: { w: 1280, h: 1200 }, self: null,
        iframes: [{ src: 'https://accounts.google.com/gsi/button?x=1', x: 800, y: 640, w: 200, h: 44 }, { src: 'https://ads/', x: 0, y: 0, w: 9, h: 9 }] }),
      hitIframe: () => ({ ok: true }),
    },
    5: {
      measureEl: () => ({ ok: true, pt: { x: 80, y: 22 }, vp: { w: 200, h: 44 }, label: 'div "Sign in with Google"' }),
      frameInfo: () => ({ vp: { w: 200, h: 44 }, self: null, iframes: [] }),
      armClick: () => ({ ok: true }),
      takeClick: () => ({ ok: true, lost: false, down: { trusted: true, hit: 'div "Sign in with Google"', onEl: true } }),
    },
  });
  const r = await clickElementTrusted(d, { tabId: 7, frameId: 5, n: 1, frames });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.text, 'clicked div "Sign in with Google"');
  assert.deepEqual(cmds(d.log), ['attach', 'move 880,662', 'press 880,662', 'release 880,662', 'detach']);
  assert.ok(d.log.includes('hitIframe@0'));                 // nothing laid over the iframe
  assert.ok(d.log.includes('armClick@5') && d.log.includes('takeClick@5'));   // the listener is in the iframe
});

test('a same-origin child frame is placed by its own frameElement, not by src', async () => {
  const frames = [{ frameId: 0, parentFrameId: -1, url: 'https://x.test/' }, { frameId: 2, parentFrameId: 0, url: 'about:srcdoc#' }];
  const d = fake({
    0: { frameInfo: () => ({ vp: { w: 1000, h: 800 }, self: null, iframes: [] }), hitIframe: () => ({ ok: true }) },
    2: { frameInfo: () => ({ vp: { w: 300, h: 200 }, self: { x: 50, y: 60, w: 300, h: 200 }, iframes: [] }),
         measureEl: () => ({ ok: true, pt: { x: 10, y: 20 }, vp: { w: 300, h: 200 }, label: 'a' }),
         armClick: () => ({ ok: true }), takeClick: () => ({ ok: true, lost: false, down: { onEl: true, hit: 'a' } }) },
  });
  const ch = await frameChain(d, frames, 2);
  assert.deepEqual(ch.chain[0].box, { x: 50, y: 60, w: 300, h: 200, src: '' });
  const r = await clickElementTrusted(d, { tabId: 7, frameId: 2, n: 1, frames });
  assert.equal(r.ok, true);
  assert.ok(cmds(d.log).includes('press 60,80'));
});

test('falls back, with the reason, when the debugger cannot be used', async () => {
  const frames = [{ frameId: 0, parentFrameId: -1, url: 'https://x.test/' }];
  for (const why of ['no_permission', 'disabled']) {
    const d = fake({ 0: topPage() }, { unavailable: why });
    assert.deepEqual(await clickElementTrusted(d, { tabId: 7, frameId: 0, n: 1, frames }), { fallback: why });
    assert.deepEqual(d.log, []);                               // nothing attached, nothing measured
  }
  const d = fake({ 0: topPage() }, { attachFails: 'Another debugger is already attached to the tab with id: 7.' });
  const r = await clickElementTrusted(d, { tabId: 7, frameId: 0, n: 1, frames });
  assert.equal(r.fallback, 'attach_failed');
  assert.match(r.detail, /already attached/);
  assert.deepEqual(cmds(d.log), ['attach']);                   // no detach for an attach that failed
});

test('a frame that cannot be placed falls back after detaching', async () => {
  const frames = [{ frameId: 0, parentFrameId: -1, url: 'https://x.test/' }, { frameId: 3, parentFrameId: 0, url: 'https://redirected.example/' }];
  const d = fake({
    0: { frameInfo: () => ({ vp: { w: 1000, h: 800 }, self: null, iframes: [{ src: 'https://orig.example/', x: 0, y: 0, w: 10, h: 10 }] }) },
    3: { frameInfo: () => ({ vp: { w: 10, h: 10 }, self: null, iframes: [] }), measureEl: () => ({ ok: true, pt: { x: 5, y: 5 }, vp: { w: 10, h: 10 }, label: 'b' }) },
  });
  const r = await clickElementTrusted(d, { tabId: 7, frameId: 3, n: 1, frames });
  assert.equal(r.fallback, 'frame_unplaced');
  assert.deepEqual(cmds(d.log), ['attach', 'detach']);
  assert.ok(r.debugMs >= 0);
});

test('an element outside the visible page, or with no area, falls back to the script click', async () => {
  const frames = [{ frameId: 0, parentFrameId: -1, url: 'https://x.test/' }, { frameId: 3, parentFrameId: 0, url: 'https://f.example/' }];
  const d = fake({
    0: { frameInfo: () => ({ vp: { w: 1000, h: 800 }, self: null, iframes: [{ src: 'https://f.example/', x: 0, y: 790, w: 500, h: 400 }] }) },
    3: { frameInfo: () => ({ vp: { w: 500, h: 400 }, self: null, iframes: [] }), measureEl: () => ({ ok: true, pt: { x: 5, y: 100 }, vp: { w: 500, h: 400 }, label: 'b' }) },
  });
  assert.equal((await clickElementTrusted(d, { tabId: 7, frameId: 3, n: 1, frames })).fallback, 'offscreen');
  const hidden = fake({ 0: topPage({ measureEl: () => ({ ok: false, code: 'offscreen', err: 'the element has no visible area to click' }) }) });
  const r = await clickElementTrusted(hidden, { tabId: 7, frameId: 0, n: 1, frames });
  assert.equal(r.fallback, 'offscreen');
  assert.deepEqual(cmds(hidden.log), ['attach', 'detach']);
});

test('a covered element is refused before any mouse event, naming what covers it', async () => {
  const d = fake({ 0: topPage({ measureEl: () => ({ ok: true, pt: { x: 1, y: 1 }, vp: { w: 9, h: 9 }, label: 'a', covered: { n: 9, name: 'Accept cookies' } }) }) });
  const r = await clickElementTrusted(d, { tabId: 7, frameId: 0, n: 1, frames: [] });
  assert.deepEqual([r.ok, r.code, r.cover, r.coverName], [false, 'covered', 9, 'Accept cookies']);
  assert.deepEqual(cmds(d.log), ['attach', 'detach']);
});

test('a banner laid over the iframe is refused before the click', async () => {
  const frames = [{ frameId: 0, parentFrameId: -1, url: 'https://x.test/' }, { frameId: 3, parentFrameId: 0, url: 'https://f.example/' }];
  const d = fake({
    0: { frameInfo: () => ({ vp: { w: 1000, h: 800 }, self: null, iframes: [{ src: 'https://f.example/', x: 10, y: 10, w: 500, h: 400 }] }),
         hitIframe: () => ({ ok: false, what: 'div "We use cookies"' }) },
    3: { frameInfo: () => ({ vp: { w: 500, h: 400 }, self: null, iframes: [] }), measureEl: () => ({ ok: true, pt: { x: 5, y: 5 }, vp: { w: 500, h: 400 }, label: 'b' }) },
  });
  const r = await clickElementTrusted(d, { tabId: 7, frameId: 3, n: 1, frames });
  assert.deepEqual([r.ok, r.code, r.cover, r.coverName], [false, 'covered', null, 'div "We use cookies"']);
  assert.deepEqual(cmds(d.log), ['attach', 'detach']);
});

test('no mouse event reached the page: nothing happened, so the script click may run', async () => {
  const d = fake({ 0: topPage({ takeClick: () => ({ ok: true, lost: false, down: null, click: null }) }) });
  const r = await clickElementTrusted(d, { tabId: 7, frameId: 0, n: 1, frames: [] });
  assert.equal(r.fallback, 'no_event');
  assert.equal(cmds(d.log).at(-1), 'detach');
});

test('a page that navigated on the click (the listener is gone) still counts as clicked', async () => {
  const d = fake({ 0: topPage({ takeClick: () => ({ ok: true, lost: true }) }) });
  const r = await clickElementTrusted(d, { tabId: 7, frameId: 0, n: 1, frames: [] });
  assert.deepEqual([r.ok, r.lost], [true, true]);
});

test('a page that swallows mousedown and click still counts as reached: no second, script click', async () => {
  const d = fake({ 0: topPage({ takeClick: () => ({ ok: true, lost: false, seen: 2, down: null, click: null }) }) });
  const r = await clickElementTrusted(d, { tabId: 7, frameId: 0, n: 1, frames: [] });
  assert.equal(r.ok, true);
  assert.match(r.text, /the page took the mouse events itself/);
});

test('says so when the mouse landed on something else', async () => {
  const d = fake({ 0: topPage({ takeClick: () => ({ ok: true, lost: false, down: { hit: 'div "Overlay"', onEl: false }, click: null }) }) });
  const r = await clickElementTrusted(d, { tabId: 7, frameId: 0, n: 1, frames: [] });
  assert.equal(r.ok, true);
  assert.match(r.text, /landed on div "Overlay", not on it/);
});

test('a mouse command that fails before the press falls back; after it, it throws', async () => {
  const frames = [{ frameId: 0, parentFrameId: -1, url: 'https://x.test/' }];
  const early = fake({ 0: topPage() }, { failOn: 'mouseMoved' });
  assert.equal((await clickElementTrusted(early, { tabId: 7, frameId: 0, n: 1, frames })).fallback, 'input_failed');
  assert.equal(cmds(early.log).at(-1), 'detach');
  const late = fake({ 0: topPage() }, { failOn: 'mouseReleased' });
  await assert.rejects(clickElementTrusted(late, { tabId: 7, frameId: 0, n: 1, frames }), e => {
    assert.ok(e instanceof VerbError);
    assert.deepEqual(e.extra, { via: 'trusted', via_why: 'press_failed' });
    assert.ok(e.debugMs >= 0);
    return true;
  });
  assert.equal(cmds(late.log).filter(l => l.startsWith('release')).length, 2);   // a second release, so no button stays down
  assert.equal(cmds(late.log).at(-1), 'detach');
});

test('cancelled by the operator: no press, debugger released', async () => {
  let n = 0;
  const d = fake({ 0: topPage() }, { cancelledAfter: () => ++n > 0 });
  await assert.rejects(clickElementTrusted(d, { tabId: 7, frameId: 0, n: 1, frames: [] }), e => e.cancelled === true);
  assert.deepEqual(cmds(d.log), ['attach', 'detach']);
});

// --- a click by coordinates ---------------------------------------------------------------------

test('a coordinate click in the top page goes straight to the point', async () => {
  const d = fake({ 0: { probeAt: () => ({ ok: true, hit: 'button "Go"' }), armClick: () => ({ ok: true }),
    takeClick: () => ({ ok: true, lost: false, down: { hit: 'button "Go"' } }) } });
  const r = await clickPointTrusted(d, { tabId: 7, x: 300.5, y: 200, expect: { n: 4, label: 'Go' }, frames: [{ frameId: 0, parentFrameId: -1, url: 'https://x.test/' }], topHost: 'x.test' });
  assert.deepEqual([r.ok, r.text, r.frameId], [true, 'clicked button "Go"', 0]);
  assert.deepEqual(cmds(d.log), ['attach', 'move 300.5,200', 'press 300.5,200', 'release 300.5,200', 'detach']);
});

test('a coordinate click inside a cross-origin iframe is allowed, after asking about its site', async () => {
  const frames = [{ frameId: 0, parentFrameId: -1, url: 'https://deltamath.com/sign-in' },
                  { frameId: 5, parentFrameId: 0, url: 'https://accounts.google.com/gsi/button' }];
  const seen = [];
  const d = fake({
    0: { probeAt: (x, y, ex) => { seen.push(['top', x, y, ex]); return { ok: true, iframe: { src: 'https://accounts.google.com/gsi/button', x: 1200, y: 940, w: 200, h: 44 } }; } },
    5: { probeAt: (x, y) => { seen.push(['gsi', x, y]); return { ok: true, hit: 'div "Sign in with Google"' }; },
         armClick: () => ({ ok: true }), takeClick: () => ({ ok: true, lost: false, down: { hit: 'div "Sign in with Google"' } }) },
  });
  const r = await clickPointTrusted(d, { tabId: 7, x: 1280, y: 974, expect: null, frames, topHost: 'deltamath.com' });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.text, 'clicked div "Sign in with Google" (inside the iframe from accounts.google.com)');
  assert.equal(r.frameId, 5);
  assert.deepEqual(seen[1], ['gsi', 80, 34]);                  // the point in the iframe's own viewport
  assert.deepEqual(cmds(d.log), ['attach', 'move 1280,974', 'press 1280,974', 'release 1280,974', 'detach']);
  assert.ok(d.log.indexOf('consent https://accounts.google.com/gsi/button') < d.log.indexOf('attach'));   // asked first, no bar yet
});

test('a same-site iframe needs no extra question; a denied operator stops the click', async () => {
  const frames = [{ frameId: 0, parentFrameId: -1, url: 'https://mail.google.com/' }, { frameId: 2, parentFrameId: 0, url: 'https://accounts.google.com/x' }];
  const pages = { 0: { probeAt: () => ({ ok: true, iframe: { src: 'https://accounts.google.com/x', x: 0, y: 0, w: 9, h: 9 } }) },
                  2: { probeAt: () => ({ ok: true, hit: 'a' }), armClick: () => ({ ok: true }), takeClick: () => ({ ok: true, lost: false, down: { hit: 'a' } }) } };
  const same = fake(pages);
  assert.equal((await clickPointTrusted(same, { tabId: 7, x: 5, y: 5, frames, topHost: 'mail.google.com' })).ok, true);
  assert.ok(!same.log.some(l => l.startsWith('consent')));
  const denied = fake(pages, { denyConsent: true });
  await assert.rejects(clickPointTrusted(denied, { tabId: 7, x: 5, y: 5, frames, topHost: 'other.example' }), /denied/);
  assert.deepEqual(cmds(denied.log), []);                       // never attached
});

test('a point inside a frame of the Jav3 server itself is refused', async () => {
  const frames = [{ frameId: 0, parentFrameId: -1, url: 'https://x.test/' }];       // tabFrames dropped the server's frame
  const d = fake({ 0: { probeAt: () => ({ ok: true, iframe: { src: 'https://jav3.lan:8000/app', x: 0, y: 0, w: 9, h: 9 } }) } });
  const r = await clickPointTrusted(d, { tabId: 7, x: 5, y: 5, frames, topHost: 'x.test' });
  assert.deepEqual([r.ok, /Jav3 server itself/.test(r.err)], [false, true]);
  assert.deepEqual(cmds(d.log), []);
});

test('an iframe that cannot be matched to a frame is still clicked, just not watched', async () => {
  const frames = [{ frameId: 0, parentFrameId: -1, url: 'https://x.test/' }];
  const d = fake({ 0: { probeAt: () => ({ ok: true, iframe: { src: '', x: 0, y: 0, w: 99, h: 99 } }) } });
  const r = await clickPointTrusted(d, { tabId: 7, x: 5, y: 5, frames, topHost: 'x.test' });
  assert.deepEqual([r.ok, r.text, r.frameId], [true, 'clicked an iframe (inside the iframe from this page)', -1]);
  assert.ok(!d.log.some(l => l.startsWith('armClick') || l.startsWith('takeClick')));
});

test('the page moving between the screenshot and the click is refused, before any mouse event', async () => {
  const d = fake({ 0: { probeAt: () => ({ ok: false, code: 'moved', err: 'the page moved since the screenshot' }) } });
  const r = await clickPointTrusted(d, { tabId: 7, x: 5, y: 5, expect: { n: 1, label: 'x' }, frames: [], topHost: 'x.test' });
  assert.deepEqual([r.ok, r.code], [false, 'moved']);
  assert.deepEqual(cmds(d.log), []);                            // found out before attaching
});

test('a point the bar pushed off the bottom of the window falls back to the script click', async () => {
  let calls = 0;
  const d = fake({ 0: { probeAt: () => (++calls === 1 ? { ok: true, hit: 'a' } : { ok: false, code: 'outside', err: 'outside the page' }) } });
  const r = await clickPointTrusted(d, { tabId: 7, x: 5, y: 790, frames: [], topHost: 'x.test' });
  assert.equal(r.fallback, 'outside_after_attach');
  assert.deepEqual(cmds(d.log), ['attach', 'detach']);
});

test('something different under the point once the bar is up is refused', async () => {
  const frames = [{ frameId: 0, parentFrameId: -1, url: 'https://x.test/' }, { frameId: 4, parentFrameId: 0, url: 'https://f.example/' }];
  let calls = 0;
  const d = fake({ 0: { probeAt: () => (++calls === 1
    ? { ok: true, iframe: { src: 'https://f.example/', x: 0, y: 0, w: 9, h: 9 } } : { ok: true, hit: 'div' }) },
    4: { probeAt: () => ({ ok: true, hit: 'a' }) } });
  const r = await clickPointTrusted(d, { tabId: 7, x: 5, y: 5, frames, topHost: 'x.test' });
  assert.deepEqual([r.ok, r.code], [false, 'moved']);
  assert.ok(!cmds(d.log).some(l => l.startsWith('press')));
});

test('with the debugger unavailable a coordinate click falls back untouched', async () => {
  const d = fake({}, { unavailable: 'no_permission' });
  assert.deepEqual(await clickPointTrusted(d, { tabId: 7, x: 1, y: 1, frames: [], topHost: 'x.test' }), { fallback: 'no_permission' });
  assert.deepEqual(d.log, []);
});
